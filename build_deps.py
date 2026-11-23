"""Prepares the native dependencies of the slideio-processor Python package.

A Python port of the dependency steps (2 and 3) of extern/slideio-processor/build.sh:

  1. SlideIO: when SLIDEIO_INSTALL_DIR is set, that prefix is used as given and nothing
     is built. Otherwise SlideIO is built and installed from the nested submodule
     (extern/slideio-processor/extern/slideio) into build/slideio-install, unless the
     install there already matches the submodule commit and the submodule tree is clean.
  2. Conan: installs the processor's own conanfile into build/conan, so this build and
     the processor's own build cannot drift apart.

Writes build/deps.json: {"toolchain": <conan_toolchain.cmake>, "slideio_prefix": <prefix>},
which setup.py reads. Configures and builds nothing else; run it before building the wheel.

Environment:
  SLIDEIO_INSTALL_DIR  An installed SlideIO prefix (holding lib/ and include/) to use.
  CONAN                Path to the Conan 2 executable, when `conan` is not on PATH.
  PYTHON               Python 3 interpreter for SlideIO's install.py (default: this one).
"""
import glob
import json
import os
import platform
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
PROCESSOR_DIR = os.path.join(ROOT, "extern", "slideio-processor")
SLIDEIO_SRC = os.path.join(PROCESSOR_DIR, "extern", "slideio")
BUILD_DIR = os.path.join(ROOT, "build")
SLIDEIO_INSTALL = os.path.join(BUILD_DIR, "slideio-install")
SLIDEIO_STAMP = os.path.join(SLIDEIO_INSTALL, ".slideio-commit")
CONAN_DIR = os.path.join(BUILD_DIR, "conan")
DEPS_FILE = os.path.join(BUILD_DIR, "deps.json")


def die(message):
    print("build_deps.py: " + message, file=sys.stderr)
    sys.exit(1)


def step(message):
    print()
    print("==> " + message, flush=True)


def find_conan():
    conan = os.environ.get("CONAN")
    if conan:
        if not (os.path.isfile(conan) and os.access(conan, os.X_OK)):
            die("CONAN is set but is not an executable")
        return conan
    conan = shutil.which("conan")
    if conan:
        return conan
    # A conda environment holding Conan, as on the reference Windows setup.
    home = os.path.expanduser("~")
    for pattern in ("anaconda3/envs/*/Scripts/conan.exe", "miniconda3/envs/*/Scripts/conan.exe",
                    "anaconda3/envs/*/bin/conan", "miniconda3/envs/*/bin/conan"):
        for candidate in sorted(glob.glob(os.path.join(home, pattern))):
            if os.access(candidate, os.X_OK):
                return candidate
    die("Conan 2 not found; put it on PATH or set CONAN=/path/to/conan")


def find_python():
    return os.environ.get("PYTHON") or sys.executable


def slideio_commit():
    commit = subprocess.check_output(["git", "-C", SLIDEIO_SRC, "rev-parse", "HEAD"], text=True).strip()
    status = subprocess.check_output(["git", "-C", SLIDEIO_SRC, "status", "--porcelain"], text=True)
    if status.strip():
        commit += "-dirty"  # local edits: never trust a previous install
    return commit


def slideio_prefix(conan):
    given = os.environ.get("SLIDEIO_INSTALL_DIR")
    if given:
        step("Using the SlideIO install named by SLIDEIO_INSTALL_DIR")
        return os.path.abspath(given)

    prefix = os.path.join(SLIDEIO_INSTALL, "release")
    commit = slideio_commit()
    installed = ""
    if os.path.isfile(SLIDEIO_STAMP):
        with open(SLIDEIO_STAMP, encoding="utf-8") as f:
            installed = f.read().strip()
    if installed == commit and not commit.endswith("-dirty") and os.path.isdir(prefix):
        step(f"SlideIO {commit[:12]} is already installed in build/slideio-install")
        return prefix

    step(f"Building and installing SlideIO {commit[:12]} (this takes a while)")
    env = os.environ.copy()
    # SlideIO's install.py runs `conan` by name.
    env["PATH"] = os.path.dirname(conan) + os.pathsep + env.get("PATH", "")
    # install.py resolves a relative prefix against its own directory, so pass an absolute one.
    subprocess.check_call([find_python(), "install.py", "-a", "install", "-c", "release",
                           "--prefix", SLIDEIO_INSTALL], cwd=SLIDEIO_SRC, env=env)
    if not os.path.isdir(prefix):
        die("the SlideIO install did not produce build/slideio-install/release")
    with open(SLIDEIO_STAMP, "w", encoding="utf-8") as f:
        f.write(commit + "\n")
    return prefix


def conan_install(conan):
    step("Installing Conan dependencies (Release)")
    # A fresh machine (a CI runner, a new container) has no default profile yet.
    if subprocess.call([conan, "profile", "path", "default"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
        subprocess.check_call([conan, "profile", "detect"])
    # The default profile's arch is whatever the first `profile detect` saw, and a restored
    # ~/.conan2 cache can carry the wrong one; SlideIO's install.py picks its profile from
    # the host, so on macOS name the host arch here or the two will not link.
    arch_setting = []
    if platform.system() == "Darwin":
        machine = platform.machine()
        if machine == "arm64":
            arch_setting = ["-s", "arch=armv8"]
        elif machine == "x86_64":
            arch_setting = ["-s", "arch=x86_64"]
    subprocess.check_call([conan, "install", PROCESSOR_DIR, "-of", CONAN_DIR,
                           "-s", "build_type=Release", "-s", "compiler.cppstd=17",
                           *arch_setting, "--build=missing"])
    toolchain = os.path.join(CONAN_DIR, "conan_toolchain.cmake")
    if not os.path.isfile(toolchain):
        die("conan install did not produce build/conan/conan_toolchain.cmake")
    return toolchain


def main():
    if not os.path.isfile(os.path.join(PROCESSOR_DIR, "CMakeLists.txt")):
        die("extern/slideio-processor is missing; run `git submodule update --init --recursive`")
    conan = find_conan()
    print("conan: " + subprocess.check_output([conan, "--version"], text=True).strip())
    prefix = slideio_prefix(conan)
    toolchain = conan_install(conan)
    os.makedirs(BUILD_DIR, exist_ok=True)
    with open(DEPS_FILE, "w", encoding="utf-8") as f:
        json.dump({"toolchain": toolchain, "slideio_prefix": prefix}, f, indent=2)
        f.write("\n")
    step("Wrote build/deps.json")


if __name__ == "__main__":
    main()
