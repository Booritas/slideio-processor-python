// What the Python-operation stand-ins share at run time: the gate that keeps worker threads
// out of an exiting interpreter, the GIL scope built on it, the per-name concurrency limit,
// the array conversions and the error text and record.
#pragma once

#include "bindings.hpp"

#include <pybind11/numpy.h>

#include <slideio/processor/core/block_view.hpp>
#include <slideio/processor/core/data_type.hpp>

#include <condition_variable>
#include <cstdint>
#include <mutex>
#include <optional>
#include <string>

namespace slideio_processor_python {

namespace sp = slideio::processor;

// Decision 9. A worker that takes the GIL once finalization has begun blocks forever (or,
// before 3.14, exits the thread mid-call). The stand-ins therefore enter Python only through
// enterPython(), and an atexit handler, closePython(), which runs before finalization begins,
// closes the gate: it marks the process as exiting and waits, without the GIL, until every
// thread that entered has left.

/// True when the caller may take the GIL; it must then call leavePython() once it has
/// released the GIL again.
bool enterPython() noexcept;
/// Ends what a successful enterPython() began.
void leavePython() noexcept;
/// The atexit handler; called with the GIL held.
void closePython();

/// Holds the GIL for a stand-in; throws std::runtime_error naming the operation when the
/// interpreter is exiting.
class PythonScope {
public:
    explicit PythonScope(const std::string& name);
    ~PythonScope();
    PythonScope(const PythonScope&) = delete;
    PythonScope& operator=(const PythonScope&) = delete;

private:
    std::optional<py::gil_scoped_acquire> gil_;
};

/// Decision 8: at most `limit` apply calls at once per registered name; 0 is no limit.
/// Waited on without the GIL.
class ConcurrencyGate {
public:
    void enter(int32_t limit);
    void leave(int32_t limit) noexcept;

private:
    std::mutex mutex_;
    std::condition_variable free_;
    int32_t active_ = 0;
};

/// One pass through a ConcurrencyGate, for the life of the object.
class GatePass {
public:
    GatePass(ConcurrencyGate& gate, int32_t limit) : gate_(gate), limit_(limit) { gate_.enter(limit_); }
    ~GatePass() { gate_.leave(limit_); }
    GatePass(const GatePass&) = delete;
    GatePass& operator=(const GatePass&) = delete;

private:
    ConcurrencyGate& gate_;
    const int32_t limit_;
};

/// The NumPy dtype name of a library data type; throws std::runtime_error for Unknown.
const char* numpyName(sp::DataType type);

/// Interleaves the planes of `in`, which share one type, into a C-contiguous
/// (height, width, planes) buffer. Needs no GIL.
void fillTile(const sp::BlockView& in, char* dst) noexcept;

/// Decision 6: checks that `returned` is an ndarray of `type` shaped (height, width, planes),
/// or (height, width) when planes is 1, then copies its region that starts at
/// (offsetX, offsetY) into `out`. Returns a failure text starting with "operation '<name>': ",
/// or "" on success. Call with the GIL held.
std::string copyResult(const std::string& name, const py::object& returned, sp::DataType type, int32_t planes,
                       int64_t height, int64_t width, int64_t offsetX, int64_t offsetY, sp::BlockView& out);

/// The text of a Python exception, "<Type>: <str>". Never throws a Python error: a type name
/// or str() that fails is replaced by a placeholder. Call with the GIL held.
std::string describe(const py::error_already_set& e);

/// Decision 7: records the exception for SlideRun.run to chain as the cause. A record that
/// cannot be made is dropped; the error's text still names the exception. Call with the GIL
/// held.
void recordError(const std::string& name, const py::error_already_set& e) noexcept;

} // namespace slideio_processor_python
