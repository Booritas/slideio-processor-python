"""Shared machinery of the long calls (slide runs, stage runs, exports): a helper thread so
that Ctrl-C cancels the call, and the chaining of a Python operation's exception as the cause."""
import contextlib
import threading
from typing import Callable, TypeVar

from . import operation as _operation
from .errors import ProcessorError

T = TypeVar("T")


def run_interruptibly(call: Callable[[], T], cancellation) -> T:
    """Runs call() on a helper thread and returns its result. cancellation is the
    Cancellation call() watches.

    Ctrl-C cancels it and raises KeyboardInterrupt once call() has stopped, chained from
    the exception call() then raised (normally a CancelledError). A further Ctrl-C while
    call() is stopping does not end the wait: this function returns only once call() has
    finished, so the caller's cleanup never runs under a live native call. When call()
    raises, that exception is raised here with the __context__ call() gave it.
    """
    outcome = {}
    finished = []  # appended to, before done is released, once outcome is final
    done = threading.Lock()
    done.acquire()

    def target():
        try:
            outcome["result"] = call()
        except BaseException as error:  # noqa: B036  (handed to the calling thread)
            outcome["error"] = error
        finally:
            finished.append(True)
            done.release()

    # The call happens on its own thread so that this one stays in an interruptible
    # wait: Ctrl-C reaches Python here and cancels the call. The wait is on a plain
    # lock, not Thread.join(timeout): when Ctrl-C interrupts that join, CPython before
    # 3.13 marks the still-running thread as stopped, and a later join() returns at once.
    t = threading.Thread(target=target, name="slideio-processor-run", daemon=True)
    t.start()
    try:
        while not done.acquire(timeout=0.1):
            pass
    except KeyboardInterrupt:
        # Waits for call() to stop, whatever further Ctrl-Cs arrive: each one is swallowed
        # and the cancellation asserted again. The flag, not the lock, ends the loop, so an
        # interrupt that lands just after the lock was taken cannot make it wait forever.
        # No join(): it is interruptible on POSIX, and call() has finished already.
        while not finished:
            try:
                cancellation.cancel()
                done.acquire(timeout=0.1)
            except KeyboardInterrupt:
                pass
        raise KeyboardInterrupt from outcome.get("error")
    t.join()
    if "error" in outcome:
        error = outcome.pop("error")
        # Raising here chains implicitly to any exception the caller is handling and
        # would replace the context the call set (the CancelledError); put it back.
        context = error.__context__
        try:
            raise error
        finally:
            error.__context__ = context
    return outcome["result"]


@contextlib.contextmanager
def operation_causes():
    """Around a native call that runs Python operations: a ProcessorError naming an
    operation gets, as its __cause__, the first exception that operation recorded since
    the block began (decision 7). The records the block left are cleared on the way out;
    the block must leave only once the native call has finished."""
    mark = _operation._error_mark()
    try:
        yield
    except ProcessorError as error:
        if error.operation_name is not None:
            causes = _operation._take_errors(error.operation_name, mark)
            if causes:
                error.__cause__ = causes[0]
        raise
    finally:
        _operation._take_errors(None, mark)
