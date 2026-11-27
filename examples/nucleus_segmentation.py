"""Nucleus segmentation on an H&E or fluorescence whole-slide image with slideio-processor.

Runs two stages in one workspace, both from the library's built-in operations:

  tissue   segment.tissue_detect at a coarse level, as in tissue_detection.py
  nuclei   at full resolution, restricted to the tissue:
           brightfield (H&E):
             pixel.stain_deconvolution   hematoxylin and eosin amounts (Ruifrok and Johnston)
             pixel.gaussian_blur         light smoothing
             pixel.threshold_fixed       hematoxylin above --hematoxylin-threshold is nucleus
           fluorescence (the nuclear channel alone, DAPI or Hoechst):
             pixel.gaussian_blur         light smoothing
             pixel.threshold_adaptive    brighter than the mean of its neighbourhood by
                                         --nuclear-offset (pixel.threshold_fixed with
                                         --nuclear-threshold)
           then, either way:
             pixel.fill_holes            pale nuclear centres filled
             pixel.morph_open            specks, thin lines and spurs removed
             pixel.remove_large_objects  brightfield only (--keep-large-areas leaves it out):
                                         stained areas wider or taller than the largest
                                         nucleus removed
             pixel.distance_transform    a peak inside each nucleus, a dip where two touch
             segment.watershed           one marker per peak, touching nuclei split at the dips
           The library runs the chain tile by tile on all cores and delivers each nucleus
           whole, also where it crosses a tile seam, identical at every tile size.

and writes:

  <out>/nuclei.geojson   the nuclei as polygons (level-0 pixel coordinates), which
                         QuPath and slideio-view can import
  <out>/nuclei.csv       one row per nucleus: area and perimeter (and micrometre values
                         when the slide reports its pixel size)
  <out>/nuclei_pipeline.json
                         both stages and the exports as a pipeline file, to run on other
                         slides with `slideio-processor run --pipeline ... --slide ...`
  <out>/nuclei_overlay.png
                         (with --overlay and --roi, needs Pillow) the nuclei drawn over
                         the region of interest

The parameters in pixels follow from the slide's pixel size and three sizes in micrometres:
the typical nucleus diameter, the largest nucleus, and the smallest kept. segment.watershed
refuses, and so fails the whole run, any foreground no marker within maximumMarkerSpacingPx
reaches, since a tile could not decide it. The chain is set up so that this does not happen
on nuclei (see nuclei_parameters): the opening removes what would get no marker, and the
spacing covers the largest nucleus, so a clump of touching nuclei, each with its own peak,
is split rather than refused.

A stained area larger than --max-nucleus-size-um that is not a clump of nuclei (a fold, a
keratin band, a dark artefact) is another matter. In brightfield mode
pixel.remove_large_objects removes every connected stained area wider or taller than that
before the distance transform. Such an area is removed whole, with any nucleus touching it,
even at a corner, and so is a clump of touching nuclei wider than that: if the threshold is
far too low, nuclei merge into large areas and disappear rather than failing the run.
Compare the nucleus count on an overlay when changing it. --keep-large-areas leaves the step
out. Fluorescence mode leaves it out, since there packed nuclei often touch: a large bright
area there fails the run, and the message names a pixel in it; look at it, then exclude it
with --roi or raise --max-nucleus-size-um.

--mode picks brightfield or fluorescence, by default from the channel count as in
tissue_detection.py: 3 channels are brightfield, any other count fluorescence (a 3-channel
fluorescence slide needs --mode fluorescence). In fluorescence mode the nuclei come from one
channel, --nuclear-channel (an index or a channel name; by default the channel named DAPI or
Hoechst). A pixel is nucleus when it is brighter than the mean of the window around it,
as wide as the largest nucleus, by --nuclear-offset (by default 2% of the channel type's
range: 5 for 8-bit, 1311 for 16-bit; other types need it given). A local cut-off keeps
packed nuclei apart: where cells crowd, the space between nuclei is often brighter than the
nuclei of sparse tissue, so one cut-off for the whole slide fuses them into regions too large
to split. A fixed cut-off, --nuclear-threshold in the scene's pixel units, replaces it for
slides with even staining.

The hematoxylin threshold is fixed, not computed per slide: Otsu over a whole slide can drop
low on tissue with diffuse stain (smooth muscle, for one) and merge it into one region. Check
it on an overlay of a few regions per staining batch; --hematoxylin-threshold changes it.
Black pixels inside the slide (scanner padding) read as dense stain; the tissue restriction
keeps the run away from them on most slides.

A full-resolution run over a whole 40x slide reads every pixel once and takes minutes; try
--roi on a small region first.

Example:
    python nucleus_segmentation.py slide.svs --workspace ws --out out \\
        --roi 2200,4500,1000,1000 --overlay
    python nucleus_segmentation.py fluorescence.qptiff --workspace ws --out out \\
        --roi 6000,6000,1000,1000 --overlay

Slide paths and the slide id end up in logs and export files; keep them in storage covered
by the deployment's HIPAA Security Rule and GDPR controls, and choose a --slide-id that is
not patient-identifying.
"""

import argparse
import math
import os
import sys

import slideio
from slideio_processor import (ExportSpec, PipelineFile, PipelineSpec, StageDefinition,
                               StageStatus, Workspace)

# SlideIO has no automatic driver: pick one from the file name.
_DRIVERS = [
    (".ome.tiff", "OMETIFF"), (".ome.tif", "OMETIFF"),
    (".svs", "SVS"), (".afi", "AFI"), (".scn", "SCN"), (".czi", "CZI"), (".zvi", "ZVI"),
    (".dcm", "DCM"), (".ndpi", "NDPI"), (".vsi", "VSI"), (".qptiff", "QPTIFF"),
    (".tif", "GDAL"), (".tiff", "GDAL"), (".png", "GDAL"), (".jpg", "GDAL"), (".jpeg", "GDAL"),
]

NUCLEAR_STAINS = ("dapi", "hoechst")
TYPE_RANGE = {"uint8": 255.0, "uint16": 65535.0}
DEFAULT_OFFSET = 0.02       # of the channel type's range
TISSUE = "tissue"
NUCLEI = "nuclei"
ASSUMED_PIXEL_UM = 0.25


def driver_for(path):
    lower = path.lower()
    for suffix, driver in _DRIVERS:
        if lower.endswith(suffix):
            return driver
    raise SystemExit(f"cannot tell the SlideIO driver of {path!r}; pass --driver")


def coarse_level(scene, target_downsample):
    """The finest level whose downsample is at least target_downsample, or the coarsest."""
    chosen = 0
    for index in range(scene.num_zoom_levels):
        chosen = index
        if 1.0 / scene.get_zoom_level_info(index).scale >= target_downsample:
            break
    return chosen


def pixel_size_um(scene):
    """The level-0 pixel width in micrometres, or None when the slide does not report it."""
    width_m = scene.resolution[0]
    return width_m * 1e6 if width_m > 0 else None


def parse_roi(text):
    try:
        x, y, w, h = (int(v) for v in text.split(","))
    except ValueError:
        raise SystemExit(f"--roi must be x,y,width,height in level-0 pixels, got {text!r}") from None
    return x, y, w, h


def nuclear_channel(scene, text):
    """The index of the nuclear channel: --nuclear-channel as an index or a name, else the
    one channel whose name contains DAPI or Hoechst."""
    names = [scene.get_channel_name(i) or "" for i in range(scene.num_channels)]
    listed = ", ".join(f"{i}: {n or '(unnamed)'}" for i, n in enumerate(names))
    if text is None:
        found = [i for i, n in enumerate(names) if any(s in n.lower() for s in NUCLEAR_STAINS)]
        if len(found) != 1:
            raise SystemExit(f"cannot tell the nuclear channel ({listed}); pass --nuclear-channel")
        return found[0]
    if text.isdigit():
        if int(text) >= scene.num_channels:
            raise SystemExit(f"--nuclear-channel {text} is out of range ({listed})")
        return int(text)
    found = [i for i, n in enumerate(names) if n.lower() == text.lower()]
    if not found:
        raise SystemExit(f"no channel named {text!r} ({listed})")
    return found[0]


def tissue_stage(level, mode, minimum_area_px):
    parameters = {"closingRadiusPx": 2, "minimumRegionAreaPx": minimum_area_px}
    if mode == "fluorescence":
        parameters["mode"] = "Fluorescence"     # the triangle cut-off of the channel maximum
    else:
        parameters["saturationThreshold"] = 0.07
    spec = PipelineSpec(analysis_level=level).add("segment.tissue_detect", **parameters)
    return StageDefinition(TISSUE, spec, writes_layers=(TISSUE,))


def nuclei_parameters(pixel_um, args):
    """The chain's pixel parameters, from the sizes in micrometres."""
    radius = max(2.0, args.nucleus_diameter_um / pixel_um / 2)
    largest = max(2 * radius, args.max_nucleus_size_um / pixel_um)
    # Two peaks closer than this are one nucleus.
    marker_distance = max(1, int(0.6 * radius))
    # A peak below this is a ragged edge or a thin bridge, not a nucleus.
    marker_floor = 0.3 * radius
    return {
        "hole_size": math.ceil(2 * radius),
        # segment.watershed's rule for a distance transform: an Ellipse of radius
        # k >= max(minimumMarkerValue, minimumMarkerDistancePx + 1) leaves only objects that
        # get a marker; the opening also drops nuclei narrower than 2k + 1.
        "opening_radius": max(math.ceil(marker_floor), marker_distance + 1),
        # Above every nucleus's peak, so the distance has no plateau inside one.
        "distance_cap": math.ceil(largest / 2) + 1,
        "marker_distance": marker_distance,
        "marker_floor": marker_floor,
        # At least the largest width or height of a nucleus, which its one marker must reach.
        "spacing": math.ceil(largest),
        "minimum_area": max(0, round(args.min_area_um2 / (pixel_um * pixel_um))),
    }


def nuclei_stage(roi, pixel_um, args, mode, channel=None, offset=None):
    p = nuclei_parameters(pixel_um, args)
    side = 2 * p["opening_radius"] + 1
    if mode == "fluorescence":
        spec = (PipelineSpec(channels=[channel], analysis_level=0, roi=roi)
                .add("pixel.gaussian_blur", sigma=1.0))
        if args.nuclear_threshold is not None:
            spec = spec.add("pixel.threshold_fixed", value=args.nuclear_threshold, direction="Above")
        else:
            # threshold = window mean - offset, so a negative offset asks for a pixel
            # brighter than its surroundings. The window spans the largest nucleus (the
            # operation takes at most 1025).
            spec = spec.add("pixel.threshold_adaptive", method="Mean",
                            blockSize=min(p["spacing"] | 1, 1025), offset=-offset, direction="Above")
    else:
        spec = (PipelineSpec(analysis_level=0, roi=roi)
                # Two planes, hematoxylin then eosin; the chain carries both, and watershed
                # floods plane 0, the hematoxylin.
                .add("pixel.stain_deconvolution")
                .add("pixel.gaussian_blur", sigma=1.0)
                .add("pixel.threshold_fixed", value=args.hematoxylin_threshold, direction="Above"))
    spec = (spec
            .add("pixel.fill_holes", maximumHoleSizePx=p["hole_size"])
            .add("pixel.morph_open", shape="Ellipse", elementWidth=side, elementHeight=side,
                 iterations=1))
    if mode == "brightfield" and args.large_areas != "keep":
        # A stained area wider or taller than the largest nucleus (a fold, a keratin band)
        # would get one marker it cannot be covered by, and fail the run; removing it keeps
        # every remaining object within the watershed's marker spacing. It also removes a
        # clump of touching nuclei that wide, which is why fluorescence leaves it out.
        spec = spec.add("pixel.remove_large_objects", maximumObjectSizePx=p["spacing"])
    spec = (spec
            .add("pixel.distance_transform", maximumDistancePx=p["distance_cap"])
            .add("segment.watershed", minimumMarkerDistancePx=p["marker_distance"],
                 maximumMarkerSpacingPx=p["spacing"], minimumMarkerValue=p["marker_floor"],
                 minimumAreaPx=p["minimum_area"], measurements="Area,Perimeter"))
    # Restricted to the tissue regions: tiles outside them are never read.
    return StageDefinition(NUCLEI, spec, writes_layers=(NUCLEI,), restrict_to_layer=TISSUE)


def print_progress(name, fraction, done, total):
    print(f"\r{name}: {fraction:6.1%} ({done}/{total} tiles)", end="", file=sys.stderr, flush=True)


def save_overlay(scene, roi, geojson_path, png_path, channel=None, max_width=2000):
    """Draws the outlines of the GeoJSON export over the region of interest. With a nuclear
    channel, the background is that channel stretched to its 99.5th percentile, as grey."""
    import json

    import numpy as np
    from PIL import Image, ImageDraw

    x0, y0, w, _ = roi
    scale = min(1.0, max_width / w)
    size = (int(w * scale), 0)
    if channel is None:
        image = Image.fromarray(scene.read_block(roi, size=size)).convert("RGB")
    else:
        grey = scene.read_block(roi, size=size, channel_indices=[channel]).astype(np.float64)
        top = np.percentile(grey, 99.5)
        if top <= 0:
            top = max(grey.max(), 1.0)
        image = Image.fromarray((np.clip(grey / top, 0, 1) * 255).astype(np.uint8)).convert("RGB")
    draw = ImageDraw.Draw(image)
    with open(geojson_path, encoding="utf-8") as f:
        features = json.load(f)["features"]
    for feature in features:
        geometry = feature["geometry"]
        polygons = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
        for polygon in polygons:
            points = [((px - x0) * scale, (py - y0) * scale) for px, py in polygon[0]]
            draw.line(points + points[:1], fill=(0, 255, 0), width=1)
    image.save(png_path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("slide", help="H&E or fluorescence slide file")
    parser.add_argument("--driver", help="SlideIO driver (default: from the file extension)")
    parser.add_argument("--scene", type=int, default=0, help="scene index (default 0)")
    parser.add_argument("--workspace", required=True,
                        help="workspace directory; created when missing, reused when it exists")
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument("--roi", help="x,y,width,height in level-0 pixels (default: all the tissue)")
    parser.add_argument("--mode", choices=("brightfield", "fluorescence"),
                        help="brightfield (H&E) or fluorescence (default: fluorescence when the "
                             "scene does not have 3 channels, else brightfield)")
    parser.add_argument("--hematoxylin-threshold", type=float, default=0.3,
                        help="brightfield only: hematoxylin amount (optical density) of a nucleus "
                             "pixel (default %(default)s)")
    parser.add_argument("--nuclear-channel",
                        help="fluorescence only: the nuclear stain's channel, an index or a name "
                             "(default: the channel named DAPI or Hoechst)")
    parser.add_argument("--nuclear-threshold", type=float,
                        help="fluorescence only: a fixed cut-off on the nuclear channel, in the "
                             "scene's pixel units, instead of the local cut-off")
    parser.add_argument("--nuclear-offset", type=float,
                        help="fluorescence only: how much brighter than the mean of its "
                             "neighbourhood a nucleus pixel is, in the scene's pixel units "
                             "(default: 2%% of the channel type's range)")
    parser.add_argument("--nucleus-diameter-um", type=float, default=3.0,
                        help="diameter of the smaller nuclei to find; sets the opening, which "
                             "removes narrower objects, and how close two seeds may be "
                             "(default %(default)s um)")
    parser.add_argument("--max-nucleus-size-um", type=float, default=15.0,
                        help="largest width or height of a nucleus; a larger stained area is "
                             "removed in brightfield mode and fails the run in fluorescence "
                             "mode (default %(default)s um)")
    large = parser.add_mutually_exclusive_group()
    large.add_argument("--remove-large-areas", dest="large_areas", action="store_const",
                       const="remove",
                       help="brightfield only: remove stained areas larger than "
                            "--max-nucleus-size-um, with nuclei touching them (the default)")
    large.add_argument("--keep-large-areas", dest="large_areas", action="store_const",
                       const="keep",
                       help="brightfield only: keep them; such an area fails the run")
    parser.add_argument("--min-area-um2", type=float, default=5.0,
                        help="drop nuclei smaller than this (default %(default)s um^2)")
    parser.add_argument("--slide-id", default="",
                        help="id written in the exports (default none); not patient-identifying")
    parser.add_argument("--overlay", action="store_true",
                        help="write nuclei_overlay.png of the --roi region (needs Pillow)")
    args = parser.parse_args(argv)

    slide = slideio.open_slide(args.slide, args.driver or driver_for(args.slide))
    scene = slide.get_scene(args.scene)
    mode = args.mode or ("brightfield" if scene.num_channels == 3 else "fluorescence")
    channel = offset = None
    if mode == "brightfield":
        if scene.num_channels != 3:
            raise SystemExit(f"colour deconvolution needs an RGB scene; this one has "
                             f"{scene.num_channels} channels: use --mode fluorescence")
        if (args.nuclear_channel, args.nuclear_threshold, args.nuclear_offset) != (None, None, None):
            raise SystemExit("--nuclear-channel, --nuclear-threshold and --nuclear-offset apply "
                             "to --mode fluorescence only")
        print("mode: brightfield", flush=True)
    else:
        if args.large_areas is not None:
            raise SystemExit("--remove-large-areas and --keep-large-areas apply to "
                             "--mode brightfield only")
        channel = nuclear_channel(scene, args.nuclear_channel)
        dtype = str(scene.get_channel_data_type(channel))
        if args.nuclear_threshold is not None and args.nuclear_offset is not None:
            raise SystemExit("--nuclear-threshold replaces the local cut-off; drop --nuclear-offset")
        offset = args.nuclear_offset
        if offset is None and args.nuclear_threshold is None:
            if dtype not in TYPE_RANGE:
                raise SystemExit(f"the nuclear channel is {dtype}: pass --nuclear-offset or "
                                 f"--nuclear-threshold in its pixel units")
            offset = DEFAULT_OFFSET * TYPE_RANGE[dtype]
        print(f"mode: fluorescence, nuclear channel {channel} "
              f"({scene.get_channel_name(channel) or 'unnamed'}, {dtype})", flush=True)
    roi = parse_roi(args.roi) if args.roi else None
    if args.overlay and roi is None:
        raise SystemExit("--overlay needs --roi: a whole-slide overlay of every nucleus is not readable")

    pixel_um = pixel_size_um(scene)
    if pixel_um is None:
        print(f"the slide reports no pixel size: assuming {ASSUMED_PIXEL_UM} um", file=sys.stderr)
        pixel_um = ASSUMED_PIXEL_UM

    tissue_minimum_px = int(0.05 * 1e6 / (pixel_um * pixel_um))     # 0.05 mm^2
    definitions = [tissue_stage(coarse_level(scene, 32.0), mode, tissue_minimum_px),
                   nuclei_stage(roi, pixel_um, args, mode, channel, offset)]

    if os.path.isdir(args.workspace) and os.listdir(args.workspace):
        ws = Workspace.open(args.workspace, slide=scene, scene=args.scene)
    else:
        ws = Workspace.create(args.workspace, scene, scene=args.scene)

    with ws:
        for definition in definitions:
            ws.declare_layer(definition.writes_layers[0])
            # Unchanged parameters keep an earlier run; changed ones replace the stage and
            # mark it, and the stages after it, stale.
            ws.declare_stage(definition)

        for name in (TISSUE, NUCLEI):
            stage = ws.stage(name)
            if stage.status is StageStatus.COMPLETE and not stage.stale:
                print(f"stage {name}: up to date")
                continue
            result = ws.run_stage(name, progress=print_progress)
            print(file=sys.stderr)
            truncated = f", {result.truncated_objects} cut at the halo" if result.truncated_objects else ""
            print(f"stage {result.name}: {result.objects_written} objects written{truncated}", flush=True)

        summary = ws.summary(layer=NUCLEI, measurements=["area"])
        print(f"{summary.count} nuclei")
        if summary.count:
            area = summary.measurements["area"]
            to_um2 = pixel_um * pixel_um
            print(f"area: mean {area.mean * to_um2:.1f} um^2, min {area.min * to_um2:.1f}, "
                  f"max {area.max * to_um2:.1f}")
            if roi is not None:
                print(f"density: {summary.count / (roi[2] * roi[3] * to_um2 / 1e6):,.0f} "
                      f"nuclei per mm^2 of the region")

        os.makedirs(args.out, exist_ok=True)
        exports = [ExportSpec.geojson("nuclei.geojson", NUCLEI), ExportSpec.csv("nuclei.csv", NUCLEI)]
        ws.export(exports, args.out, slide_id=args.slide_id)
        pipeline = PipelineFile.from_workspace(ws)
        for spec in exports:
            pipeline = pipeline.add_export(spec)
        with open(os.path.join(args.out, "nuclei_pipeline.json"), "w", encoding="utf-8") as f:
            f.write(pipeline.to_json())
        print(f"wrote {args.out}/nuclei.geojson, nuclei.csv, nuclei_pipeline.json")

    if args.overlay:
        save_overlay(scene, roi, os.path.join(args.out, "nuclei.geojson"),
                     os.path.join(args.out, "nuclei_overlay.png"), channel)
        print(f"wrote {args.out}/nuclei_overlay.png")


if __name__ == "__main__":
    main()
