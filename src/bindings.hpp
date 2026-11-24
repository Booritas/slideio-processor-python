#pragma once

#include <pybind11/pybind11.h>

#include <exception>

namespace py = pybind11;

/// Registers the translator from native exceptions to slideio_processor.errors.
void bindErrors(py::module_& m);
/// Binds PipelineFile and ExportSpec as _PipelineFile and _ExportSpec.
void bindPipelineFile(py::module_& m);
/// Binds SlideRun and CancellationSource as _SlideRun and _CancellationSource.
void bindRun(py::module_& m);
/// Binds the registration of Python operations and the catalog queries.
void bindOperations(py::module_& m);

/// Sets the current Python error for a native exception that is not a pybind11 one, as the
/// module's exception translator does: a ProcessorError by its code, anything else as an
/// InternalError whose message does not echo what(). Call with the GIL held.
void setNativeError(std::exception_ptr p);
