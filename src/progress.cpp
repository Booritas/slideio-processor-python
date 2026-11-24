#include "progress.hpp"

#include <utility>

namespace sp = slideio::processor;

PythonProgress::PythonProgress(py::object callback, sp::CancellationSource& source)
    : callback_(std::move(callback)), source_(source) {}

void PythonProgress::onProgress(const char* stageName, double fraction, int64_t completed, int64_t total) noexcept {
    if (failed_.load(std::memory_order_acquire)) return;
    try {
        py::gil_scoped_acquire gil;
        if (failed_.load(std::memory_order_acquire)) return; // another worker failed meanwhile
        try {
            callback_(stageName != nullptr ? stageName : "", fraction, completed, total);
        } catch (py::error_already_set& e) {
            kept_ = e.value();
            stop();
        }
    } catch (...) {
        stop();
    }
}

void PythonProgress::stop() noexcept {
    failed_.store(true, std::memory_order_release);
    source_.cancel();
}

[[noreturn]] void raiseKept(const py::object& kept, const std::exception_ptr& failure) {
    if (failure) {
        setNativeError(failure);
        py::error_already_set native; // takes the error just set
        PyException_SetContext(kept.ptr(), native.value().inc_ref().ptr()); // steals the reference
    }
    // PyErr_Restore, not PyErr_SetObject: the latter would replace the context with an
    // exception the calling Python code may be handling.
    PyErr_Restore(py::type::of(kept).inc_ref().ptr(), kept.inc_ref().ptr(),
                  PyException_GetTraceback(kept.ptr()));
    throw py::error_already_set();
}
