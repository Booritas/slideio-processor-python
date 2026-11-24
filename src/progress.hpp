// The progress observer shared by slide runs, stage runs and exports, and the helper that
// raises the exception a progress callable raised.
#pragma once

#include "bindings.hpp"

#include <slideio/processor/core/control.hpp>

#include <atomic>
#include <cstdint>
#include <exception>

// Forwards progress to a Python callable. Called from the stages' worker threads while
// the run holds no GIL: each call takes the GIL for itself. The first exception the
// callable raises is kept, the run is cancelled, and every later call is ignored.
class PythonProgress final : public slideio::processor::ProgressObserver {
public:
    PythonProgress(py::object callback, slideio::processor::CancellationSource& source);

    // No exception leaves this function: the library's workers do not expect one.
    void onProgress(const char* stageName, double fraction, int64_t completed, int64_t total) noexcept override;

    // The kept exception, or a null object. Call with the GIL held, after the run.
    py::object kept() const { return kept_; }

private:
    void stop() noexcept;

    py::object callback_;
    slideio::processor::CancellationSource& source_;
    std::atomic<bool> failed_{false};
    py::object kept_; // written and read with the GIL held
};

// Raises the exception the progress callable raised. When the run threw, the run's error
// (normally the CancelledError the callable caused) becomes its __context__.
[[noreturn]] void raiseKept(const py::object& kept, const std::exception_ptr& failure);
