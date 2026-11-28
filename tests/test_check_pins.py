"""scripts/check_pins.py: the slideio pin and the nested SlideIO submodule agree.

These tests need no wheel: they import the script from the checkout's scripts/.
"""
import os
import sys

import pytest

_SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import check_pins  # noqa: E402


def test_pinned_slideio():
    assert check_pins.pinned_slideio('dependencies = ["slideio==2.11.1", "numpy"]') == "2.11.1"
    with pytest.raises(ValueError):
        check_pins.pinned_slideio('dependencies = ["slideio>=2.11.1", "numpy"]')


def test_nested_version():
    assert check_pins.nested_slideio_version('constexpr const char* SLIDEIO_VERSION = "2.11.1";') == "2.11.1"


def test_dist_is_current():
    nested = "2026-11-27T21:31:37+01:00"
    assert check_pins.dist_is_current("2026-11-28T11:37:37Z", nested)
    assert check_pins.dist_is_current("2026-11-27T20:31:37Z", nested), "the same instant"
    assert not check_pins.dist_is_current("2026-11-23T14:40:18Z", nested)


def test_repository_pins_agree():
    assert check_pins.main([]) == 0
