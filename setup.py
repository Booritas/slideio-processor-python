"""Builds the slideio_processor_native extension with CMake.

Run `python build_deps.py` first: it prepares SlideIO and the Conan dependencies and
writes build/deps.json, which this file reads.

D2: the wheel ships no SlideIO library and no MSVC runtime. slideio_processor/_libs gets
exactly the native module and the processor's two shared libraries; SlideIO comes from
the installed slideio package at run time.
"""
import json
import os
import platform
import re
import subprocess
import sys

from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext

ROOT = os.path.dirname(os.path.abspath(__file__))
BUILD_DIR = os.path.join(ROOT, "build")
DEPS_FILE = os.path.join(BUILD_DIR, "deps.json")
# One CMake tree per interpreter (build/cmake-cpython-312, ...): FindPython caches what it
# found, so a tree configured for one Python must not be reused for another.
CMAKE_BUILD_DIR = os.path.join(BUILD_DIR, "cmake-" + sys.implementation.cache_tag)
SLIDEIO_DEF = os.path.join(ROOT, "extern", "slideio-processor", "extern", "slideio", "src", "slideio",
                           "slideio", "slideio_def.hpp")
NATIVE_TARGET = "slideio_processor_native"
CONFIG = "Release"


def _read_cmake_version():
    """Reads the base version (MAJOR.MINOR) from CMakeLists.txt."""
    with open(os.path.join(ROOT, "CMakeLists.txt"), encoding="utf-8") as f:
        for line in f:
            m = re.search(r"set\s*\(\s*projectVersion\s+(\d+\.\d+)", line)
            if m:
                return m.group(1)
    raise RuntimeError("Could not determine the version from CMakeLists.txt")


def _read_slideio_version():
    """Reads the SlideIO version the build compiles against from slideio_def.hpp."""
    with open(SLIDEIO_DEF, encoding="utf-8") as f:
        m = re.search(r'SLIDEIO_VERSION\s*=\s*"(\d+\.\d+\.\d+)"', f.read())
    if not m:
        raise RuntimeError("Could not determine the SlideIO version from slideio_def.hpp")
    return m.group(1)


def _version():
    # slideio-python's scheme: MAJOR.MINOR from CMakeLists.txt, the patch from CI.
    return _read_cmake_version() + "." + (os.environ.get("CI_PIPELINE_IID") or "0")


def _processor_library_names():
    if platform.system() == "Windows":
        return ["slideio-processor-data.dll", "slideio-processor-engine.dll"]
    suffix = ".dylib" if platform.system() == "Darwin" else ".so"
    return ["libslideio-processor-data" + suffix, "libslideio-processor-engine" + suffix]


def _find_exactly_one(directory, file_name):
    matches = []
    for root, _dirs, files in os.walk(directory):
        if file_name in files:
            matches.append(os.path.join(root, file_name))
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one {file_name} in the CMake build tree, found {len(matches)}")
    return matches[0]


class CMakeExtension(Extension):
    def __init__(self, name):
        Extension.__init__(self, name, sources=[])


class CMakeBuild(build_ext):
    def run(self):
        try:
            subprocess.check_output(["cmake", "--version"])
        except OSError:
            raise RuntimeError("CMake must be installed to build slideio_processor_native")
        for ext in self.extensions:
            self.build_extension(ext)

    def build_extension(self, ext):
        if not os.path.isfile(DEPS_FILE):
            raise RuntimeError("build/deps.json is missing: run `python build_deps.py` first")
        with open(DEPS_FILE, encoding="utf-8") as f:
            deps = json.load(f)

        import pybind11

        cmake_args = [
            "-DCMAKE_TOOLCHAIN_FILE=" + deps["toolchain"],
            "-DCMAKE_PREFIX_PATH=" + deps["slideio_prefix"],
            "-DCMAKE_BUILD_TYPE=" + CONFIG,
            "-DPython_EXECUTABLE=" + sys.executable,
            "-Dpybind11_DIR=" + pybind11.get_cmake_dir(),
            # Always passed, so a build directory configured for tests does not keep the
            # helpers in a later release build.
            "-DSLIDEIO_PROCESSOR_PYTHON_TEST_HELPERS="
            + ("ON" if os.environ.get("SLIDEIO_PROCESSOR_PYTHON_TEST_HELPERS") == "1" else "OFF"),
        ]
        if platform.system() == "Windows":
            cmake_args += ["-G", "Visual Studio 17 2022", "-A", "x64"]

        os.makedirs(CMAKE_BUILD_DIR, exist_ok=True)
        subprocess.check_call(["cmake", "-S", ROOT, "-B", CMAKE_BUILD_DIR] + cmake_args)
        subprocess.check_call(["cmake", "--build", CMAKE_BUILD_DIR, "--config", CONFIG,
                               "--target", NATIVE_TARGET, "--parallel"])

        # Exactly three files, each found by its exact name: nothing else, and above all
        # no SlideIO library, may reach the wheel.
        module_name = os.path.basename(self.get_ext_filename(ext.name))
        sources = [_find_exactly_one(CMAKE_BUILD_DIR, name)
                   for name in [module_name] + _processor_library_names()]

        package_dir = os.path.join(self.build_lib, "slideio_processor")
        libs_dir = os.path.join(package_dir, "_libs")
        os.makedirs(libs_dir, exist_ok=True)
        # A warm build_lib may hold files from an earlier build; keep only the package marker.
        for name in os.listdir(libs_dir):
            path = os.path.join(libs_dir, name)
            if name != "__init__.py" and os.path.isfile(path):
                os.remove(path)
        for source in sources:
            self.copy_file(source, os.path.join(libs_dir, os.path.basename(source)))

        with open(os.path.join(package_dir, "_build_info.py"), "w", encoding="utf-8") as f:
            f.write("# Generated by setup.py.\n")
            f.write(f'VERSION = "{self.distribution.get_version()}"\n')
            f.write(f'SLIDEIO_VERSION = "{_read_slideio_version()}"\n')


setup(
    version=_version(),
    ext_modules=[CMakeExtension("slideio_processor._libs." + NATIVE_TARGET)],
    cmdclass={"build_ext": CMakeBuild},
    zip_safe=False,
)
