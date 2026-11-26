#!/usr/bin/env bash
# Builds the slideio-processor wheels, one per Python interpreter:
#   1. prepares SlideIO and the Conan dependencies (build_deps.py, once),
#   2. runs `pip wheel . --no-deps` with each interpreter, into the out directory.
# Each interpreter gets its own CMake tree (build/cmake-cpython-3XY, see setup.py).
#
# Usage: ./build.sh [options]
#   -p, --python <X.Y|path>  Build only for this Python: a version (3.12) or the path
#                            of an interpreter. Repeatable. Default: every version of
#                            3.9 to 3.15 found on this machine (the versions the pinned
#                            slideio wheel is published for); missing ones are skipped.
#   -o, --out-dir <dir>      Where the wheels go (default: dist).
#       --test-helpers       Build with the native test helpers
#                            (SLIDEIO_PROCESSOR_PYTHON_TEST_HELPERS=1), as CI does.
#                            Leave off for release wheels.
#       --skip-deps          Do not run build_deps.py; build/deps.json must exist.
#   -h, --help               Show this help.
#
# A version is looked up, in order, as: /opt/python/cpXY-cpXY/bin/python (manylinux),
# pythonX.Y on PATH, `py -X.Y` (Windows launcher), conda environments
# (~/anaconda3, ~/miniconda3, ~/miniforge3) and pyenv versions.
#
# Environment:
#   SLIDEIO_INSTALL_DIR  An installed SlideIO prefix to use instead of building SlideIO
#                        (passed through to build_deps.py).
#   CONAN                Path to the Conan 2 executable, when `conan` is not on PATH.
#   PYTHON               Interpreter that runs build_deps.py (and SlideIO's install.py);
#                        default: the first interpreter selected. It needs `distro`.
#
# Works in Git Bash on Windows and in bash on Linux and macOS.

set -euo pipefail

usage() { sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; }
die() { echo "build.sh: $*" >&2; exit 1; }
step() { echo; echo "==> $*"; }

DEFAULT_VERSIONS="3.9 3.10 3.11 3.12 3.13 3.14 3.15"

requested=()
out_dir=dist
test_helpers=0
skip_deps=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        -p|--python) [[ $# -ge 2 ]] || die "$1 needs a value"; requested+=("$2"); shift 2 ;;
        -o|--out-dir) [[ $# -ge 2 ]] || die "$1 needs a value"; out_dir="$2"; shift 2 ;;
        --test-helpers) test_helpers=1; shift ;;
        --skip-deps) skip_deps=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option '$1' (see --help)" ;;
    esac
done

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$root"

# --- Interpreters ----------------------------------------------------------------

# Runs Python code with an interpreter: a path or command name, or "py -X.Y" (the Windows
# launcher). Paths may hold spaces, so only the launcher form is split.
py_run() {
    if [[ "$1" == "py -"* ]]; then py "${1#py }" -c "$2"; else "$1" -c "$2"; fi
}

# Prints "X.Y" for an interpreter, or nothing. The Windows Store aliases exist on PATH
# but do not run, hence the probe.
py_version() {
    py_run "$1" 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null | tr -d '\r' || true
}

# Prints the absolute executable of an interpreter.
py_executable() {
    py_run "$1" 'import sys; print(sys.executable)' 2>/dev/null | tr -d '\r'
}

# Prints the executable of a Python X.Y, or fails if none is found.
find_version() {
    local version="$1" tag="${1/./}" candidate
    local candidates=("/opt/python/cp$tag-cp$tag/bin/python" "python$version")
    if command -v py >/dev/null 2>&1; then candidates+=("py -$version"); fi
    for candidate in "$HOME"/{anaconda3,miniconda3,miniforge3} \
                     "$HOME"/{anaconda3,miniconda3,miniforge3}/envs/*; do
        candidates+=("$candidate/python.exe" "$candidate/bin/python")
    done
    for candidate in "$HOME"/.pyenv/versions/"$version".* "$HOME"/.pyenv/pyenv-win/versions/"$version".*; do
        candidates+=("$candidate/bin/python" "$candidate/python.exe")
    done
    for candidate in "${candidates[@]}"; do
        # A path that does not exist is skipped without running it.
        if [[ "$candidate" == */* && ! -x "$candidate" ]]; then continue; fi
        if [[ "$(py_version "$candidate")" == "$version" ]]; then
            py_executable "$candidate"
            return 0
        fi
    done
    return 1
}

pythons=()
if [[ ${#requested[@]} -gt 0 ]]; then
    for spec in "${requested[@]}"; do
        if [[ "$spec" =~ ^3\.[0-9]+$ ]]; then
            exe="$(find_version "$spec")" || die "Python $spec not found (see --help for where it is looked up)"
        else
            [[ -n "$(py_version "$spec")" ]] || die "'$spec' is neither a version (3.12) nor a working Python"
            exe="$(py_executable "$spec")"
        fi
        pythons+=("$exe")
    done
else
    for version in $DEFAULT_VERSIONS; do
        if exe="$(find_version "$version")"; then
            pythons+=("$exe")
        else
            echo "build.sh: Python $version not found, skipped"
        fi
    done
    [[ ${#pythons[@]} -gt 0 ]] || die "no Python of $DEFAULT_VERSIONS found; name one with --python"
fi

step "Interpreters"
for exe in "${pythons[@]}"; do
    echo "  $(py_version "$exe"): $exe"
done

# --- 1. Dependencies ----------------------------------------------------------------

if [[ $skip_deps -eq 1 ]]; then
    step "Skipping build_deps.py (--skip-deps)"
    [[ -f build/deps.json ]] || die "build/deps.json is missing; run without --skip-deps"
else
    deps_python="${PYTHON:-${pythons[0]}}"
    step "Preparing dependencies with $deps_python"
    "$deps_python" build_deps.py
fi

# --- 2. Wheels ------------------------------------------------------------------------

if [[ $test_helpers -eq 1 ]]; then
    export SLIDEIO_PROCESSOR_PYTHON_TEST_HELPERS=1
else
    unset SLIDEIO_PROCESSOR_PYTHON_TEST_HELPERS
fi

mkdir -p "$out_dir"
built=()
for exe in "${pythons[@]}"; do
    version="$(py_version "$exe")"
    step "Building the wheel for Python $version"
    tag="cp${version/./}"
    "$exe" -m pip wheel . --no-deps -w "$out_dir"
    # The newest wheel of this tag: the out directory may hold older builds.
    wheel="$(ls -t "$out_dir"/slideio_processor-*-"$tag"-"$tag"-*.whl 2>/dev/null | head -n 1)"
    [[ -n "$wheel" ]] || die "pip wheel produced no $tag wheel in $out_dir"
    built+=("$wheel")
done

step "Built"
for wheel in "${built[@]}"; do
    echo "  $wheel"
done
