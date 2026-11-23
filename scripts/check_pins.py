"""Checks that the slideio pin and the nested SlideIO submodule agree (D7).

Offline: the exact `slideio==X.Y.Z` pin in pyproject.toml equals SLIDEIO_VERSION in the
nested submodule's slideio_def.hpp, the version the native module is compiled against.

--online: also, the tag vX.Y.Z of https://github.com/Booritas/slideio names the commit
the nested submodule extern/slideio-processor/extern/slideio is pinned to.

Exit code 0 when everything matches, 1 on a mismatch (both values are printed).
"""
import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYPROJECT = os.path.join(ROOT, "pyproject.toml")
PROCESSOR_DIR = os.path.join(ROOT, "extern", "slideio-processor")
SLIDEIO_DEF = os.path.join(PROCESSOR_DIR, "extern", "slideio", "src", "slideio", "slideio", "slideio_def.hpp")
SLIDEIO_REPOSITORY = "https://github.com/Booritas/slideio"


def pinned_slideio(pyproject_text: str) -> str:
    """The X.Y.Z of the exact `slideio==X.Y.Z` dependency."""
    m = re.search(r'"slideio==(\d+\.\d+\.\d+)"', pyproject_text)
    if not m:
        raise ValueError("no exact slideio==X.Y.Z pin found")
    return m.group(1)


def nested_slideio_version(slideio_def_text: str) -> str:
    """The X.Y.Z of SLIDEIO_VERSION in slideio_def.hpp."""
    m = re.search(r'SLIDEIO_VERSION\s*=\s*"(\d+\.\d+\.\d+)"', slideio_def_text)
    if not m:
        raise ValueError("no SLIDEIO_VERSION found")
    return m.group(1)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _tag_commit(version):
    """The commit tag v<version> of the SlideIO repository names, or "" if there is no such tag."""
    ref = "refs/tags/v" + version
    output = subprocess.check_output(["git", "ls-remote", SLIDEIO_REPOSITORY, ref, ref + "^{}"], text=True)
    commits = {}
    for line in output.splitlines():
        sha, name = line.split("\t", 1)
        commits[name] = sha
    # An annotated tag lists the tag object and, under ^{}, the commit it points to.
    return commits.get(ref + "^{}") or commits.get(ref, "")


def _submodule_commit():
    return subprocess.check_output(["git", "-C", PROCESSOR_DIR, "rev-parse", "HEAD:extern/slideio"],
                                   text=True).strip()


def main(argv) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--online", action="store_true",
                        help="also check the SlideIO tag against the nested submodule commit")
    args = parser.parse_args(argv)

    pin = pinned_slideio(_read(PYPROJECT))
    nested = nested_slideio_version(_read(SLIDEIO_DEF))
    if pin != nested:
        print(f"check_pins: pyproject.toml pins slideio=={pin}, the nested SlideIO is {nested}")
        return 1
    print(f"check_pins: slideio=={pin} matches the nested SlideIO version")

    if args.online:
        tagged = _tag_commit(pin)
        pinned = _submodule_commit()
        if tagged != pinned:
            print(f"check_pins: tag v{pin} is commit {tagged or '<missing>'}, "
                  f"the nested SlideIO submodule is commit {pinned}")
            return 1
        print(f"check_pins: tag v{pin} is the nested SlideIO submodule commit {pinned}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
