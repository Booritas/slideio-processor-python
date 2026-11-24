import os
import sys

import pytest

# Puts tests/ on sys.path so the helpers in tests/support import as "support".
_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

_SIZE = 600
_RADIUS = 40
_BACKGROUND = (255, 255, 255)
_BLOB = (20, 20, 20)


@pytest.fixture
def blob_slide(tmp_path):
    """A 600x600 RGB PNG: white, with nine dark discs of radius 40 on a 200-pixel grid."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (_SIZE, _SIZE), _BACKGROUND)
    draw = ImageDraw.Draw(image)
    for i in range(3):
        for j in range(3):
            cx, cy = 100 + 200 * i, 100 + 200 * j
            draw.ellipse((cx - _RADIUS, cy - _RADIUS, cx + _RADIUS, cy + _RADIUS), fill=_BLOB)
    path = tmp_path / "slide.png"
    image.save(path)
    return path


@pytest.fixture
def seam_slide(tmp_path):
    """A 2200x400 RGB slide: white, with three dark discs of radius 40 centred at (300, 200),
    (1024, 200) and (1700, 200). It is a TIFF with 1024x1024 tiles, named seam.svs, so the
    run has three tiles and the middle disc straddles the first seam; a PNG would be one tile
    (see support/tiled_tiff.py). seam.png beside it holds the same pixels."""
    import numpy
    from PIL import Image, ImageDraw
    from support.tiled_tiff import write_tiled_rgb

    image = Image.new("RGB", (2200, 400), _BACKGROUND)
    draw = ImageDraw.Draw(image)
    for cx in (300, 1024, 1700):
        draw.ellipse((cx - _RADIUS, 200 - _RADIUS, cx + _RADIUS, 200 + _RADIUS), fill=_BLOB)
    image.save(tmp_path / "seam.png")
    path = tmp_path / "seam.svs"
    write_tiled_rgb(path, numpy.asarray(image), tile=1024)
    return path


@pytest.fixture
def three_stage_json():
    """The C++ test's three-stage pipeline file: tissue, nuclei within tissue, and a prob raster."""
    with open(os.path.join(_tests_dir, "data", "three_stage.json"), encoding="utf-8") as f:
        return f.read()
