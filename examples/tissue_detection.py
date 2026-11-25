"""Tissue detection on a whole-slide image with slideio-processor.

Finds the tissue regions of one slide scene with the built-in operation
segment.tissue_detect, at a coarse pyramid level, and writes:

  <out>/tissue.geojson   the tissue regions as polygons (level-0 pixel coordinates)
  <out>/tissue.csv       one row per region: area and perimeter (and micrometre values
                         when the slide reports its pixel size)
  <out>/tissue_pipeline.json
                         the same stage as a pipeline file, to run on other slides with
                         `slideio-processor run --pipeline ... --slide ...`
  <out>/tissue_overlay.png
                         (with --overlay, needs Pillow) the regions drawn over a
                         thumbnail of the scene

segment.tissue_detect marks a pixel as tissue when its colour is saturated enough
((max(R,G,B) - min(R,G,B)) / max(R,G,B) > saturation threshold), closes small gaps
with a disc of --closing-radius pixels at the analysis level, and returns each
connected region as an object with `area` and `perimeter` in level-0 pixels.

Example:
    python tissue_detection.py slide.svs --workspace ws --out out --overlay

Slide paths and the slide id end up in logs and export files; keep them in storage
covered by the deployment's HIPAA Security Rule and GDPR controls, and choose a
--slide-id that is not patient-identifying.
"""

import argparse
import os
import sys

import slideio
from slideio_processor import (ExportSpec, PipelineFile, PipelineSpec, StageDefinition,
                               Workspace)

# SlideIO has no automatic driver: pick one from the file name.
_DRIVERS = [
    (".ome.tiff", "OMETIFF"), (".ome.tif", "OMETIFF"),
    (".svs", "SVS"), (".afi", "AFI"), (".scn", "SCN"), (".czi", "CZI"), (".zvi", "ZVI"),
    (".dcm", "DCM"), (".ndpi", "NDPI"), (".vsi", "VSI"), (".qptiff", "QPTIFF"),
    (".tif", "GDAL"), (".tiff", "GDAL"), (".png", "GDAL"), (".jpg", "GDAL"), (".jpeg", "GDAL"),
]

LAYER = "tissue"
STAGE = "tissue"


def driver_for(path):
    lower = path.lower()
    for suffix, driver in _DRIVERS:
        if lower.endswith(suffix):
            return driver
    raise SystemExit(f"cannot tell the SlideIO driver of {path!r}; pass --driver")


def choose_level(scene, target_downsample):
    """The finest pyramid level whose downsample is at least target_downsample, or the
    coarsest level when none is. Tissue detection needs no cellular detail, and a coarse
    level runs in seconds instead of minutes."""
    chosen = 0
    for index in range(scene.num_zoom_levels):
        info = scene.get_zoom_level_info(index)
        chosen = index
        if 1.0 / info.scale >= target_downsample:
            break
    return chosen


def pixel_size_um(scene):
    """The level-0 pixel width in micrometres, or None when the slide does not report it."""
    width_m = scene.resolution[0]
    return width_m * 1e6 if width_m > 0 else None


def tissue_stage(level, saturation, closing_radius, minimum_area_px):
    spec = PipelineSpec(analysis_level=level).add(
        "segment.tissue_detect",
        saturationThreshold=saturation,
        closingRadiusPx=closing_radius,         # analysis-level pixels
        minimumRegionAreaPx=minimum_area_px)    # square level-0 pixels
    return StageDefinition(STAGE, spec, writes_layers=(LAYER,))


def print_progress(name, fraction, done, total):
    print(f"\r{name}: {fraction:6.1%} ({done}/{total} tiles)", end="", file=sys.stderr, flush=True)


def save_overlay(scene, geojson_path, png_path, width=1500):
    """Draws the tissue polygons of the GeoJSON export over a thumbnail of the scene:
    outer boundaries green, holes yellow."""
    import json

    from PIL import Image, ImageDraw

    scale = width / scene.rect[2]
    image = Image.fromarray(scene.read_block(size=(width, 0))).convert("RGB")
    draw = ImageDraw.Draw(image)
    with open(geojson_path, encoding="utf-8") as f:
        features = json.load(f)["features"]
    for feature in features:
        geometry = feature["geometry"]
        polygons = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
        for polygon in polygons:
            for ring_index, ring in enumerate(polygon):
                points = [(x * scale, y * scale) for x, y in ring]
                draw.line(points + points[:1], fill=(255, 255, 0) if ring_index else (0, 255, 0), width=2)
    image.save(png_path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("slide", help="slide file")
    parser.add_argument("--driver", help="SlideIO driver (default: from the file extension)")
    parser.add_argument("--scene", type=int, default=0, help="scene index (default 0)")
    parser.add_argument("--workspace", required=True,
                        help="workspace directory; created when missing, reused when it exists")
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument("--downsample", type=float, default=32.0,
                        help="target downsample of the analysis level (default 32)")
    parser.add_argument("--level", type=int, help="analysis level; overrides --downsample")
    parser.add_argument("--saturation", type=float, default=0.07,
                        help="saturation threshold from 0 to 1 (default 0.07)")
    parser.add_argument("--closing-radius", type=int, default=2,
                        help="closing radius in analysis-level pixels, 0 to 127 (default 2)")
    parser.add_argument("--min-area-mm2", type=float, default=0.05,
                        help="drop regions smaller than this, in mm^2 (default 0.05); "
                             "needs the slide's pixel size, else no regions are dropped")
    parser.add_argument("--slide-id", default="",
                        help="id written in the exports (default none); not patient-identifying")
    parser.add_argument("--overlay", action="store_true", help="write tissue_overlay.png (needs Pillow)")
    args = parser.parse_args(argv)

    slide = slideio.open_slide(args.slide, args.driver or driver_for(args.slide))
    scene = slide.get_scene(args.scene)
    if scene.num_channels != 3:
        raise SystemExit(f"segment.tissue_detect needs an RGB scene; this one has {scene.num_channels} channels")

    level = args.level if args.level is not None else choose_level(scene, args.downsample)
    pixel_um = pixel_size_um(scene)
    minimum_area_px = 0
    if pixel_um is not None:
        minimum_area_px = int(args.min_area_mm2 * 1e6 / (pixel_um * pixel_um))
    else:
        print("the slide reports no pixel size: --min-area-mm2 is ignored", file=sys.stderr)

    definition = tissue_stage(level, args.saturation, args.closing_radius, minimum_area_px)
    if os.path.isdir(args.workspace) and os.listdir(args.workspace):
        ws = Workspace.open(args.workspace, slide=scene, scene=args.scene)
    else:
        ws = Workspace.create(args.workspace, scene, scene=args.scene)

    with ws:
        ws.declare_layer(LAYER)
        # Unchanged parameters keep an earlier run; changed ones replace the stage and
        # mark it stale, so run_stage below recomputes it.
        ws.declare_stage(definition)
        info = scene.get_zoom_level_info(level)
        print(f"analysis level {level}: {info.size.width} x {info.size.height} pixels "
              f"(downsample {1.0 / info.scale:.1f})", flush=True)

        result = ws.run_stage(STAGE, progress=print_progress)
        print(file=sys.stderr)
        print(f"stage {result.name}: {result.objects_written} regions written")

        summary = ws.summary(layer=LAYER, measurements=["area"])
        area = summary.measurements["area"]
        print(f"{summary.count} tissue regions")
        if summary.count:
            total_px = area.mean * area.count
            print(f"total tissue area: {total_px:,.0f} level-0 px", end="")
            if pixel_um is not None:
                print(f" = {total_px * pixel_um * pixel_um / 1e6:.2f} mm^2", end="")
            scene_px = scene.rect[2] * scene.rect[3]
            print(f" ({total_px / scene_px:.1%} of the scene)")

            table = ws.dataframe(layer=LAYER).sort_values("area", ascending=False)
            columns = [c for c in ("id", "x", "y", "width", "height", "area", "area.um2") if c in table]
            print(table[columns].head(10).to_string(index=False))

        os.makedirs(args.out, exist_ok=True)
        ws.export([ExportSpec.geojson("tissue.geojson", LAYER),
                   ExportSpec.csv("tissue.csv", LAYER)], args.out, slide_id=args.slide_id)
        with open(os.path.join(args.out, "tissue_pipeline.json"), "w", encoding="utf-8") as f:
            f.write(PipelineFile.from_workspace(ws)
                    .add_export(ExportSpec.geojson("tissue.geojson", LAYER))
                    .add_export(ExportSpec.csv("tissue.csv", LAYER)).to_json())
        print(f"wrote {args.out}/tissue.geojson, tissue.csv, tissue_pipeline.json")

    if args.overlay:
        save_overlay(scene, os.path.join(args.out, "tissue.geojson"),
                     os.path.join(args.out, "tissue_overlay.png"))
        print(f"wrote {args.out}/tissue_overlay.png")


if __name__ == "__main__":
    main()
