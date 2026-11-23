#pragma once

#include <pybind11/pybind11.h>

namespace py = pybind11;

/// Registers the translator from native exceptions to slideio_processor.errors.
void bindErrors(py::module_& m);
/// Binds PipelineFile and ExportSpec as _PipelineFile and _ExportSpec.
void bindPipelineFile(py::module_& m);
