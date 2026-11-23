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
def three_stage_json():
    """The C++ test's three-stage pipeline file: tissue, nuclei within tissue, and a prob raster."""
    with open(os.path.join(_tests_dir, "data", "three_stage.json"), encoding="utf-8") as f:
        return f.read()
