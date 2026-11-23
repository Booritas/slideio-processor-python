#include "bindings.hpp"

#include <slideio/processor/core/error.hpp>

#include <cstdint>
#include <exception>
#include <new>
#include <stdexcept>
#include <string>

namespace {

namespace sp = slideio::processor;

// Never what(): a third-party message can hold a path (C-6).
constexpr const char* kUnexpected = "an unexpected native error occurred";

// Builds the exception with slideio_processor.errors._from_native and sets it as the
// current Python error.
void raise(int32_t code, const char* message, const char* stage, const char* operation, const char* cause,
           bool hasTile, int64_t tileX, int64_t tileY) {
    try {
        py::object exc = py::module_::import("slideio_processor.errors")
                             .attr("_from_native")(code, message, stage, operation, cause, hasTile, tileX, tileY);
        PyErr_SetObject(reinterpret_cast<PyObject*>(Py_TYPE(exc.ptr())), exc.ptr());
    } catch (py::error_already_set& e) {
        e.restore();
    }
}

void raiseInternal() {
    raise(static_cast<int32_t>(sp::ErrorCode::Internal), kUnexpected, "", "", "", false, 0, 0);
}

} // namespace

void bindErrors(py::module_& m) {
    // Local: it applies only to functions bound in this module. A global translator would
    // live in the pybind11 internals the slideio module shares and take over its errors.
    py::register_local_exception_translator([](std::exception_ptr p) {
        try {
            if (p) std::rethrow_exception(p);
        } catch (const py::error_already_set&) {
            throw; // A Python exception passing through C++: pybind11 restores it.
        } catch (const py::builtin_exception&) {
            throw; // pybind11's own errors (cast_error, value_error, ...): pybind11 maps them.
        } catch (const sp::ProcessorError& e) {
            raise(static_cast<int32_t>(e.code()), e.what(), e.stageName(), e.operationName(), e.cause(),
                  e.hasTile(), e.tileX(), e.tileY());
        } catch (const std::bad_alloc&) {
            PyErr_NoMemory();
        } catch (const std::exception&) {
            raiseInternal();
        } catch (...) {
            raiseInternal();
        }
    });

#ifdef SLIDEIO_PROCESSOR_PYTHON_TEST_HELPERS
    m.def("_raise_std_exception_for_testing",
          [](const std::string& message) { throw std::runtime_error(message); },
          "Test build only: throws std::runtime_error with the message.");
#else
    (void)m;
#endif
}
