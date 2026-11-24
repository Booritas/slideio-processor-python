// Configuration and logging: _Configuration (the library's Configuration, opaque),
// _build_configuration, and the two configureLogging routes, one with the Python log sink.
#include "bindings.hpp"
#include "python_runtime.hpp"

#include <slideio/processor/config/configuration.hpp>
#include <slideio/processor/config/logging.hpp>
#include <slideio/processor/core/log.hpp>

#include <cstdint>
#include <cstring>
#include <memory>
#include <string>
#include <utility>

namespace {

namespace sp = slideio::processor;
using slideio_processor_python::enterPython;
using slideio_processor_python::leavePython;

// The standard logging level of a library level: TRACE has no standard level and maps to 5.
int pythonLevel(sp::LogLevel level) noexcept {
    switch (level) {
    case sp::LogLevel::Trace: return 5;
    case sp::LogLevel::Debug: return 10;
    case sp::LogLevel::Info: return 20;
    case sp::LogLevel::Warning: return 30;
    default: return 40;
    }
}

// Ends a successful enterPython() however the scope is left.
struct LeaveOnExit {
    ~LeaveOnExit() { leavePython(); }
};

// Decision 6: forwards library messages to logging.getLogger("slideio_processor"). Called
// from any thread, with or without the GIL. It enters Python only through the exit gate, so
// it never takes the GIL in an exiting interpreter, and it holds no Python object, so it can
// be destroyed anywhere. A Python error raised while logging is cleared and dropped.
class PythonLogSink final : public sp::LogSink {
public:
    void write(sp::LogLevel level, const char* message) noexcept override {
        if (message == nullptr || !enterPython()) return;
        LeaveOnExit leave;
        try {
            py::gil_scoped_acquire gil;
            try {
                // A message that is not valid UTF-8 is still delivered, with replacement characters.
                py::object text = py::reinterpret_steal<py::object>(
                    PyUnicode_DecodeUTF8(message, static_cast<Py_ssize_t>(std::strlen(message)), "replace"));
                if (!text) throw py::error_already_set();
                py::module_::import("logging")
                    .attr("getLogger")("slideio_processor")
                    .attr("log")(pythonLevel(level), "%s", text);
            } catch (py::error_already_set&) {
                // Fetched (and so cleared) by error_already_set; dropped with the GIL held.
            } catch (...) {
            }
            if (PyErr_Occurred() != nullptr) PyErr_Clear();
        } catch (...) {
        }
    }
};

sp::Configuration buildConfiguration(const py::dict& fields) {
    sp::ConfigurationBuilder builder(sp::Configuration::defaults());
    if (fields.contains("memory_ceiling_bytes"))
        builder.memoryCeilingBytes(fields["memory_ceiling_bytes"].cast<int64_t>());
    if (fields.contains("worker_count")) builder.workerCount(fields["worker_count"].cast<int32_t>());
    if (fields.contains("tile_size")) {
        const auto size = fields["tile_size"].cast<std::pair<int32_t, int32_t>>();
        builder.tileSize(size.first, size.second);
    }
    if (fields.contains("tile_size_from_slide"))
        builder.tileSizeFromSlide(fields["tile_size_from_slide"].cast<bool>());
    if (fields.contains("log_file")) builder.logFilePath(fields["log_file"].cast<std::string>().c_str());
    if (fields.contains("log_level"))
        builder.logLevel(static_cast<sp::LogLevel>(fields["log_level"].cast<int32_t>()));
    if (fields.contains("object_batch_size")) builder.objectBatchSize(fields["object_batch_size"].cast<int64_t>());
    if (fields.contains("scratch_directory"))
        builder.scratchDirectory(fields["scratch_directory"].cast<std::string>().c_str());
    return builder.build();
}

} // namespace

void bindConfiguration(py::module_& m) {
    py::class_<sp::Configuration>(m, "_Configuration")
        .def("memory_ceiling_bytes", &sp::Configuration::memoryCeilingBytes)
        .def("worker_count", &sp::Configuration::workerCount)
        .def("tile_width", &sp::Configuration::tileWidth)
        .def("tile_height", &sp::Configuration::tileHeight)
        .def("object_batch_size", &sp::Configuration::objectBatchSize);

    m.def("_build_configuration", &buildConfiguration, py::arg("fields"),
          "Builds a Configuration from the library defaults and the given fields; the library validates.");
    m.def(
        "_configure_logging_file",
        [](const sp::Configuration& configuration) { sp::configureLogging(configuration); },
        py::arg("configuration"), py::call_guard<py::gil_scoped_release>(),
        "Installs the library's file or stderr sink per the configuration's log file and level.");
    m.def(
        "_configure_logging_python",
        [](int32_t level) {
            auto sink = std::make_shared<PythonLogSink>();
            py::gil_scoped_release release;
            sp::configureLogging(std::move(sink), static_cast<sp::LogLevel>(level));
        },
        py::arg("level"), "Installs the sink that forwards to the logger \"slideio_processor\".");
}
