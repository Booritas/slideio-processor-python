#include "bindings.hpp"

#include <slideio/processor/config/configuration.hpp>
#include <slideio/processor/core/control.hpp>
#include <slideio/processor/run/pipeline_file.hpp>
#include <slideio/processor/run/slide_run.hpp>
#include <slideio/processor/run/slide_run_result.hpp>

#include <atomic>
#include <cstdint>
#include <exception>
#include <memory>
#include <string>
#include <utility>

namespace {

namespace sp = slideio::processor;

// Forwards progress to a Python callable. Called from the stages' worker threads while
// the run holds no GIL: each call takes the GIL for itself. The first exception the
// callable raises is kept, the run is cancelled, and every later call is ignored.
class PythonProgress final : public sp::ProgressObserver {
public:
    PythonProgress(py::object callback, sp::CancellationSource& source)
        : callback_(std::move(callback)), source_(source) {}

    // No exception leaves this function: the library's workers do not expect one.
    void onProgress(const char* stageName, double fraction, int64_t completed, int64_t total) noexcept override {
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

    // The kept exception, or a null object. Call with the GIL held, after the run.
    py::object kept() const { return kept_; }

private:
    void stop() noexcept {
        failed_.store(true, std::memory_order_release);
        source_.cancel();
    }

    py::object callback_;
    sp::CancellationSource& source_;
    std::atomic<bool> failed_{false};
    py::object kept_; // written and read with the GIL held
};

// Raises the exception the progress callable raised. When the run threw, the run's error
// (normally the CancelledError the callable caused) becomes its __context__.
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

py::tuple toPython(const sp::SlideRunResult& result) {
    py::list stages;
    for (int32_t i = 0; i < result.stageCount(); ++i) {
        stages.append(py::make_tuple(std::string(result.stageName(i)), result.stageRan(i),
                                     result.stageObjectsWritten(i), result.stageTruncatedObjects(i)));
    }
    py::list exports;
    for (int32_t i = 0; i < result.exportCount(); ++i) {
        exports.append(py::make_tuple(std::string(result.exportFile(i)), result.exportBytes(i)));
    }
    return py::make_tuple(stages, exports);
}

// Runs with the GIL released for the whole C++ run. Returns (stages, exports): a list of
// (name, ran, objects_written, truncated_objects) and a list of (file, bytes).
py::tuple run(sp::SlideRun& self, const py::object& progress, sp::CancellationSource& source) {
    std::unique_ptr<PythonProgress> observer;
    if (!progress.is_none()) observer = std::make_unique<PythonProgress>(progress, source);
    self.progress(observer.get()).cancellation(source.token());

    sp::SlideRunResult result;
    std::exception_ptr failure;
    {
        py::gil_scoped_release release;
        try {
            result = self.run();
        } catch (...) {
            failure = std::current_exception();
        }
    }
    self.progress(nullptr);

    if (observer) {
        const py::object kept = observer->kept();
        if (kept) raiseKept(kept, failure);
    }
    if (failure) std::rethrow_exception(failure);
    return toPython(result);
}

} // namespace

void bindRun(py::module_& m) {
    py::class_<sp::CancellationSource>(m, "_CancellationSource")
        .def(py::init<>())
        .def("cancel", &sp::CancellationSource::cancel)
        .def("is_cancelled", &sp::CancellationSource::isCancelled);

    py::class_<sp::SlideRun>(m, "_SlideRun")
        .def(py::init([](const sp::PipelineFile& file, const std::string& slide, int32_t scene,
                         const std::string& workspace, const std::string& out, const std::string& slideId) {
                 auto slideRun = std::make_unique<sp::SlideRun>(file, slide.c_str(), scene, workspace.c_str(),
                                                                out.c_str(), sp::Configuration::defaults());
                 slideRun->slideId(slideId.c_str());
                 return slideRun;
             }),
             py::arg("file"), py::arg("slide"), py::arg("scene"), py::arg("workspace"), py::arg("out"),
             py::arg("slide_id"))
        .def("run", &run, py::arg("progress"), py::arg("cancellation"));
}
