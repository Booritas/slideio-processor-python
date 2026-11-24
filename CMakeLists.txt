cmake_minimum_required(VERSION 3.21)
set(projectVersion 0.1)
project(slideio-processor-python VERSION ${projectVersion} LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# D2: the wheel ships no SlideIO library. The native module and the processor's two
# libraries sit together in slideio_processor/_libs and find SlideIO in the installed
# slideio package, slideio/core/libs, two directories up. The install run path is used
# in the build tree as well, so no build-tree SlideIO is ever on the run path. Windows
# has no run path: the loader calls os.add_dll_directory instead.
if(APPLE)
    set(CMAKE_OSX_DEPLOYMENT_TARGET 12.0)
    set(CMAKE_INSTALL_RPATH "@loader_path;@loader_path/../../slideio/core/libs")
else()
    set(CMAKE_INSTALL_RPATH "$ORIGIN:$ORIGIN/../../slideio/core/libs")
endif()
set(CMAKE_BUILD_WITH_INSTALL_RPATH ON)

# One output directory for the module and the processor's libraries. The processor sets
# the plain variables to the same directory; the per-config ones stop Visual Studio
# from appending a Release/ subdirectory.
set(outputDir ${CMAKE_BINARY_DIR}/bin)
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY ${outputDir})
set(CMAKE_LIBRARY_OUTPUT_DIRECTORY ${outputDir})
foreach(config Debug Release RelWithDebInfo MinSizeRel)
    string(TOUPPER ${config} configUpper)
    set(CMAKE_RUNTIME_OUTPUT_DIRECTORY_${configUpper} ${outputDir})
    set(CMAKE_LIBRARY_OUTPUT_DIRECTORY_${configUpper} ${outputDir})
endforeach()

# No test seams in a shipped wheel.
set(SLIDEIO_PROCESSOR_TEST_SEAMS OFF CACHE BOOL "" FORCE)
add_subdirectory(extern/slideio-processor)

find_package(Python 3.9 COMPONENTS Interpreter Development.Module REQUIRED)
find_package(pybind11 CONFIG REQUIRED)

# Native helpers that only the tests call, such as one that throws a plain std::exception.
# setup.py turns this on when SLIDEIO_PROCESSOR_PYTHON_TEST_HELPERS=1; release builds leave it off.
option(SLIDEIO_PROCESSOR_PYTHON_TEST_HELPERS "Build the native helpers the tests use" OFF)

pybind11_add_module(slideio_processor_native
    src/module.cpp
    src/errors.cpp
    src/pipeline_file.cpp
    src/python_operation.cpp
    src/python_runtime.cpp
    src/slide_run.cpp)
target_link_libraries(slideio_processor_native PRIVATE
    slideio-processor-engine
    slideio-processor-data)
if(SLIDEIO_PROCESSOR_PYTHON_TEST_HELPERS)
    target_compile_definitions(slideio_processor_native PRIVATE SLIDEIO_PROCESSOR_PYTHON_TEST_HELPERS)
endif()
