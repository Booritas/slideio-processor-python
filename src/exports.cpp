// Exports: writes one export of a _Workspace through the library's writeExport, the function
// SlideRun's exports use. The library call runs with the GIL released.
#include "bindings.hpp"
#include "progress.hpp"

#include <slideio/processor/core/control.hpp>
#include <slideio/processor/core/error.hpp>
#include <slideio/processor/reporting/export_result.hpp>
#include <slideio/processor/run/export_spec.hpp>
#include <slideio/processor/run/workspace_export.hpp>
#include <slideio/processor/workspace/workspace.hpp>

#include <cstdint>
#include <exception>
#include <memory>
#include <string>

namespace {

namespace sp = slideio::processor;

// The JSON spelling of an export kind, as ExportKind's values in Python.
const char* kindName(sp::ExportKind kind) {
    switch (kind) {
    case sp::ExportKind::Csv: return "csv";
    case sp::ExportKind::GeoJson: return "geojson";
    case sp::ExportKind::LabelMask: return "labelMask";
    case sp::ExportKind::RasterTiff: return "rasterTiff";
    }
    return "unknown";
}

// Writes spec to path. Returns (bytes, cancelled); the caller raises for a cancelled export.
// When the progress callable raised, its exception is raised instead, with the
// cancellation (or the library's error) as its __context__.
py::tuple exportOne(sp::Workspace& workspace, const sp::ExportSpec& spec, const std::string& path,
                    const std::string& slideId, const py::object& progress, sp::CancellationSource& source) {
    std::unique_ptr<PythonProgress> observer;
    if (!progress.is_none()) observer = std::make_unique<PythonProgress>(progress, source);

    int64_t bytes = 0;
    bool cancelled = false;
    std::exception_ptr failure;
    {
        py::gil_scoped_release release;
        try {
            const sp::ExportResult result =
                sp::writeExport(workspace, spec, path.c_str(), slideId.c_str(), observer.get(), source.token());
            bytes = result.bytesWritten();
            cancelled = result.wasCancelled();
        } catch (...) {
            failure = std::current_exception();
        }
    }

    if (observer) {
        const py::object kept = observer->kept();
        if (kept) {
            if (!failure && cancelled) {
                const std::string text = std::string("Workspace: the ") + kindName(spec.kind()) +
                                         " export was cancelled";
                failure = std::make_exception_ptr(sp::CancelledError(text.c_str()));
            }
            raiseKept(kept, failure);
        }
    }
    if (failure) std::rethrow_exception(failure);
    return py::make_tuple(bytes, cancelled);
}

} // namespace

void bindExports(py::module_& m) {
    // Releases the GIL itself, around the library call only.
    m.def("_export", &exportOne, py::arg("workspace"), py::arg("spec"), py::arg("path"), py::arg("slide_id"),
          py::arg("progress"), py::arg("cancellation"));
}
