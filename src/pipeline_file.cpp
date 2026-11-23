#include "bindings.hpp"

#include <slideio/processor/run/export_spec.hpp>
#include <slideio/processor/run/pipeline_file.hpp>

#include <cstdint>
#include <string>

namespace {

namespace sp = slideio::processor;

// A C++ name that may be null, as a Python str or None.
py::object optionalString(const char* text) {
    if (text == nullptr) return py::none();
    return py::str(text);
}

std::string toJson(const sp::PipelineFile& file) {
    const int32_t length = file.toJson(nullptr, 0);
    std::string text(static_cast<size_t>(length) + 1, '\0');
    file.toJson(text.data(), length + 1);
    text.resize(static_cast<size_t>(length));
    return text;
}

} // namespace

void bindPipelineFile(py::module_& m) {
    // The native classes mirror C++ one to one; slideio_processor.pipeline_file wraps them.
    py::class_<sp::ExportSpec>(m, "_ExportSpec")
        .def_static("csv", &sp::ExportSpec::csv)
        .def_static("geo_json", &sp::ExportSpec::geoJson)
        .def_static("label_mask", &sp::ExportSpec::labelMask)
        .def_static("raster_tiff", &sp::ExportSpec::rasterTiff)
        .def("measurement", [](sp::ExportSpec& self, const char* name) { self.measurement(name); })
        .def("kind", [](const sp::ExportSpec& self) { return static_cast<int32_t>(self.kind()); })
        .def("file", [](const sp::ExportSpec& self) { return std::string(self.file()); })
        .def("layer", [](const sp::ExportSpec& self) { return optionalString(self.layer()); })
        .def("raster", [](const sp::ExportSpec& self) { return optionalString(self.raster()); })
        .def("measurement_count", &sp::ExportSpec::measurementCount)
        .def("measurement_at", [](const sp::ExportSpec& self, int32_t i) { return std::string(self.measurementAt(i)); })
        .def("equals", &sp::ExportSpec::equals);

    py::class_<sp::PipelineFile>(m, "_PipelineFile")
        .def(py::init<>())
        .def_static("from_json", &sp::PipelineFile::fromJson)
        .def_static("load", &sp::PipelineFile::load)
        .def("to_json", &toJson)
        .def("require", [](sp::PipelineFile& self, const char* module) { self.require(module); })
        .def("add_export", [](sp::PipelineFile& self, const sp::ExportSpec& spec) { self.addExport(spec); })
        .def("require_count", &sp::PipelineFile::requireCount)
        .def("require_at", [](const sp::PipelineFile& self, int32_t i) { return std::string(self.requireAt(i)); })
        .def("layer_count", &sp::PipelineFile::layerCount)
        .def("layer_name", [](const sp::PipelineFile& self, int32_t i) { return std::string(self.layerName(i)); })
        .def("raster_count", &sp::PipelineFile::rasterCount)
        .def("raster_name",
             [](const sp::PipelineFile& self, int32_t i) { return std::string(self.rasterAt(i).name()); })
        .def("stage_count", &sp::PipelineFile::stageCount)
        .def("stage_name", [](const sp::PipelineFile& self, int32_t i) { return std::string(self.stageName(i)); })
        .def("export_count", &sp::PipelineFile::exportCount)
        .def("export_at", [](const sp::PipelineFile& self, int32_t i) { return sp::ExportSpec(self.exportAt(i)); })
        .def("equals", &sp::PipelineFile::equals);
}
