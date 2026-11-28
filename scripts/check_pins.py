"""Checks that the slideio pin and the nested SlideIO submodule agree (D7).

Offline: the exact `slideio==X.Y.Z` pin in pyproject.toml equals SLIDEIO_VERSION in the
nested submodule's slideio_def.hpp, the version the native module is compiled against.

--online: also, the slideio wheels come from https://github.com/Booritas/slideio-dist, branch
X.Y.Z, folder X.Y.Z/<platform>/. That branch must exist and its last commit must be no older
than the nested submodule extern/slideio-processor/extern/slideio commit: wheels published
before that commit were built from older sources than the ones the native module is compiled
against. The repository is public; the GitHub API call needs no token.

Exit code 0 when everything matches, 1 on a mismatch (both values are printed).
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYPROJECT = os.path.join(ROOT, "pyproject.toml")
PROCESSOR_DIR = os.path.join(ROOT, "extern", "slideio-processor")
SLIDEIO_DEF = os.path.join(PROCESSOR_DIR, "extern", "slideio", "src", "slideio", "slideio", "slideio_def.hpp")
NESTED_SLIDEIO_DIR = os.path.join(PROCESSOR_DIR, "extern", "slideio")
DIST_REPOSITORY = "Booritas/slideio-dist"


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


def _parse_time(text: str) -> datetime.datetime:
    # fromisoformat accepts a trailing "Z" only from Python 3.11.
    return datetime.datetime.fromisoformat(text.strip().replace("Z", "+00:00"))


def dist_is_current(dist_time: str, nested_time: str) -> bool:
    """Whether the slideio-dist commit (ISO 8601) is no older than the nested SlideIO commit."""
    return _parse_time(dist_time) >= _parse_time(nested_time)


def _dist_commit_time(version):
    """The committer time of branch <version> of slideio-dist, or "" if there is no such branch."""
    request = urllib.request.Request(f"https://api.github.com/repos/{DIST_REPOSITORY}/commits/{version}",
                                     headers={"Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)["commit"]["committer"]["date"]
    except urllib.error.HTTPError as e:
        if e.code in (404, 422):
            return ""
        raise


def _submodule_commit():
    return subprocess.check_output(["git", "-C", PROCESSOR_DIR, "rev-parse", "HEAD:extern/slideio"],
                                   text=True).strip()


def _commit_time(commit):
    return subprocess.check_output(["git", "-C", NESTED_SLIDEIO_DIR, "show", "-s", "--format=%cI", commit],
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
        dist_time = _dist_commit_time(pin)
        if not dist_time:
            print(f"check_pins: {DIST_REPOSITORY} has no branch {pin}")
            return 1
        pinned = _submodule_commit()
        nested_time = _commit_time(pinned)
        if not dist_is_current(dist_time, nested_time):
            print(f"check_pins: {DIST_REPOSITORY} branch {pin} was last updated {dist_time}, "
                  f"before the nested SlideIO submodule commit {pinned} ({nested_time})")
            return 1
        print(f"check_pins: {DIST_REPOSITORY} branch {pin} ({dist_time}) is no older than "
              f"the nested SlideIO submodule commit {pinned} ({nested_time})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
