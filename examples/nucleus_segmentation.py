"""Nucleus segmentation on an H&E whole-slide image with slideio-processor.

Runs two stages in one workspace, both from the library's built-in operations:

  tissue   segment.tissue_detect at a coarse level, as in tissue_detection.py
  nuclei   at full resolution, restricted to the tissue:
             pixel.stain_deconvolution   hematoxylin and eosin amounts (Ruifrok and Johnston)
             pixel.gaussian_blur         light smoothing
             pixel.threshold_fixed       hematoxylin above --hematoxylin-threshold is nucleus
             pixel.fill_holes            pale nuclear centres filled
             pixel.morph_open            specks, thin lines and spurs removed
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
spacing covers the largest nucleus. A stained area larger than --max-nucleus-size-um that is
not a clump of nuclei (a fold, a dark artefact) still fails the run, and the message names a
pixel in it: look at it, then exclude it with --roi or raise --max-nucleus-size-um.

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


def tissue_stage(level, minimum_area_px):
    spec = PipelineSpec(analysis_level=level).add(
        "segment.tissue_detect", saturationThreshold=0.07, closingRadiusPx=2,
        minimumRegionAreaPx=minimum_area_px)
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


def nuclei_stage(roi, pixel_um, args):
    p = nuclei_parameters(pixel_um, args)
    side = 2 * p["opening_radius"] + 1
    spec = (PipelineSpec(analysis_level=0, roi=roi)
            # Two planes, hematoxylin then eosin; the chain carries both, and watershed
            # floods plane 0, the hematoxylin.
            .add("pixel.stain_deconvolution")
            .add("pixel.gaussian_blur", sigma=1.0)
            .add("pixel.threshold_fixed", value=args.hematoxylin_threshold, direction="Above")
            .add("pixel.fill_holes", maximumHoleSizePx=p["hole_size"])
            .add("pixel.morph_open", shape="Ellipse", elementWidth=side, elementHeight=side,
                 iterations=1)
            .add("pixel.distance_transform", maximumDistancePx=p["distance_cap"])
            .add("segment.watershed", minimumMarkerDistancePx=p["marker_distance"],
                 maximumMarkerSpacingPx=p["spacing"], minimumMarkerValue=p["marker_floor"],
                 minimumAreaPx=p["minimum_area"], measurements="Area,Perimeter"))
    # Restricted to the tissue regions: tiles outside them are never read.
    return StageDefinition(NUCLEI, spec, writes_layers=(NUCLEI,), restrict_to_layer=TISSUE)


def print_progress(name, fraction, done, total):
    print(f"\r{name}: {fraction:6.1%} ({done}/{total} tiles)", end="", file=sys.stderr, flush=True)


def save_overlay(scene, roi, geojson_path, png_path, max_width=2000):
    """Draws the outlines of the GeoJSON export over the region of interest."""
    import json

    from PIL import Image, ImageDraw

    x0, y0, w, _ = roi
    scale = min(1.0, max_width / w)
    image = Image.fromarray(scene.read_block(roi, size=(int(w * scale), 0))).convert("RGB")
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
    parser.add_argument("slide", help="H&E slide file")
    parser.add_argument("--driver", help="SlideIO driver (default: from the file extension)")
    parser.add_argument("--scene", type=int, default=0, help="scene index (default 0)")
    parser.add_argument("--workspace", required=True,
                        help="workspace directory; created when missing, reused when it exists")
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument("--roi", help="x,y,width,height in level-0 pixels (default: all the tissue)")
    parser.add_argument("--hematoxylin-threshold", type=float, default=0.3,
                        help="hematoxylin amount (optical density) of a nucleus pixel "
                             "(default %(default)s)")
    parser.add_argument("--nucleus-diameter-um", type=float, default=3.0,
                        help="diameter of the smaller nuclei to find; sets the opening, which "
                             "removes narrower objects, and how close two seeds may be "
                             "(default %(default)s um)")
    parser.add_argument("--max-nucleus-size-um", type=float, default=15.0,
                        help="largest width or height of a nucleus; a larger stained area fails "
                             "the run (default %(default)s um)")
    parser.add_argument("--min-area-um2", type=float, default=5.0,
                        help="drop nuclei smaller than this (default %(default)s um^2)")
    parser.add_argument("--slide-id", default="",
                        help="id written in the exports (default none); not patient-identifying")
    parser.add_argument("--overlay", action="store_true",
                        help="write nuclei_overlay.png of the --roi region (needs Pillow)")
    args = parser.parse_args(argv)

    slide = slideio.open_slide(args.slide, args.driver or driver_for(args.slide))
    scene = slide.get_scene(args.scene)
    if scene.num_channels != 3:
        raise SystemExit(f"colour deconvolution needs an RGB scene; this one has {scene.num_channels} channels")
    roi = parse_roi(args.roi) if args.roi else None
    if args.overlay and roi is None:
        raise SystemExit("--overlay needs --roi: a whole-slide overlay of every nucleus is not readable")

    pixel_um = pixel_size_um(scene)
    if pixel_um is None:
        print(f"the slide reports no pixel size: assuming {ASSUMED_PIXEL_UM} um", file=sys.stderr)
        pixel_um = ASSUMED_PIXEL_UM

    tissue_minimum_px = int(0.05 * 1e6 / (pixel_um * pixel_um))     # 0.05 mm^2
    definitions = [tissue_stage(coarse_level(scene, 32.0), tissue_minimum_px),
                   nuclei_stage(roi, pixel_um, args)]

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
                     os.path.join(args.out, "nuclei_overlay.png"))
        print(f"wrote {args.out}/nuclei_overlay.png")


if __name__ == "__main__":
    main()
