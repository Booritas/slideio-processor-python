#include "bindings.hpp"
#include "progress.hpp"

#include <slideio/processor/config/configuration.hpp>
#include <slideio/processor/core/control.hpp>
#include <slideio/processor/run/pipeline_file.hpp>
#include <slideio/processor/run/slide_run.hpp>
#include <slideio/processor/run/slide_run_result.hpp>

#include <cstdint>
#include <exception>
#include <memory>
#include <string>
#include <utility>

namespace {

namespace sp = slideio::processor;

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
                         const std::string& workspace, const std::string& out, const std::string& slideId,
                         const sp::Configuration& configuration) {
                 auto slideRun = std::make_unique<sp::SlideRun>(file, slide.c_str(), scene, workspace.c_str(),
                                                                out.c_str(), configuration);
                 slideRun->slideId(slideId.c_str());
                 return slideRun;
             }),
             py::arg("file"), py::arg("slide"), py::arg("scene"), py::arg("workspace"), py::arg("out"),
             py::arg("slide_id"), py::arg("configuration"))
        .def("run", &run, py::arg("progress"), py::arg("cancellation"));
}
