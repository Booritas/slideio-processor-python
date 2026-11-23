#include "bindings.hpp"

#include <slideio/processor/core/version.hpp>
#include <slideio/slideio/slideio_def.hpp>

#include <string>

PYBIND11_MODULE(slideio_processor_native, m) {
    m.doc() = "Native bindings of the slideio-processor library";

    m.def(
        "library_version", []() { return std::string(slideio::processor::libraryVersion()); },
        "The version of the slideio-processor C++ library, as \"major.minor.patch\".");
    m.def(
        "slideio_build_version", []() { return std::string(::SLIDEIO_VERSION); },  // global, not in namespace slideio
        "The SlideIO version this module was built against.");

    bindErrors(m);
    bindPipelineFile(m);
    bindRun(m);
}
