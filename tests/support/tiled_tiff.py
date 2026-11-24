"""Writes a tiled, uncompressed RGB TIFF, for slides whose tiles the library follows.

SlideIO's GDAL driver, which opens PNG, reports the whole image as one tile, and the run
takes its tile size from the slide. A tiled TIFF named *.svs opens with the SVS driver,
which reports the TIFF's own tiles.
"""
import struct

import numpy

_SHORT, _LONG = 3, 4


def write_tiled_rgb(path, pixels, tile=1024):
    """Writes `pixels`, a (height, width, 3) uint8 array, as a TIFF with tile x tile tiles."""
    pixels = numpy.ascontiguousarray(pixels, dtype=numpy.uint8)
    height, width, planes = pixels.shape
    assert planes == 3
    rows, columns = -(-height // tile), -(-width // tile)
    padded = numpy.zeros((rows * tile, columns * tile, 3), numpy.uint8)
    padded[:height, :width] = pixels
    tiles = [padded[r * tile:(r + 1) * tile, c * tile:(c + 1) * tile].tobytes()
             for r in range(rows) for c in range(columns)]

    data_offset = 8
    offsets, position = [], data_offset
    for t in tiles:
        offsets.append(position)
        position += len(t)
    bits_offset = position
    tile_offsets_offset = bits_offset + 6
    counts_offset = tile_offsets_offset + 4 * len(tiles)
    ifd_offset = counts_offset + 4 * len(tiles)

    entries = [
        (256, _LONG, 1, width),
        (257, _LONG, 1, height),
        (258, _SHORT, 3, bits_offset),
        (259, _SHORT, 1, 1),  # no compression
        (262, _SHORT, 1, 2),  # RGB
        (277, _SHORT, 1, 3),
        (284, _SHORT, 1, 1),  # interleaved
        (322, _LONG, 1, tile),
        (323, _LONG, 1, tile),
        (324, _LONG, len(tiles), offsets[0] if len(tiles) == 1 else tile_offsets_offset),
        (325, _LONG, len(tiles), len(tiles[0]) if len(tiles) == 1 else counts_offset),
    ]
    with open(path, "wb") as f:
        f.write(b"II*\x00" + struct.pack("<I", ifd_offset))
        for t in tiles:
            f.write(t)
        f.write(struct.pack("<3H", 8, 8, 8))
        f.write(struct.pack(f"<{len(tiles)}I", *offsets))
        f.write(struct.pack(f"<{len(tiles)}I", *(len(t) for t in tiles)))
        f.write(struct.pack("<H", len(entries)))
        for tag, kind, count, value in entries:
            packed = struct.pack("<H", value) + b"\x00\x00" if kind == _SHORT and count == 1 else struct.pack(
                "<I", value)
            f.write(struct.pack("<HHI", tag, kind, count) + packed)
        f.write(struct.pack("<I", 0))
