"""Workspace: create and open over a path or a slideio.Scene, layers and rasters."""
import gc
import os

import numpy
import pytest
import slideio

from slideio_processor import (
    InvalidArgumentError,
    IoError,
    Layer,
    LayerKind,
    Raster,
    Workspace,
)


def slide_scene(path):
    """Opens a test slide and returns (slide, its scene 0). The slide is returned too, so
    the caller keeps it open while it uses the scene."""
    driver = "SVS" if str(path).endswith(".svs") else "GDAL"
    slide = slideio.open_slide(str(path), driver)
    return slide, slide.get_scene(0)


def test_create_from_path(blob_slide, tmp_path):
    path = str(tmp_path / "ws")
    with Workspace.create(path, blob_slide) as ws:
        assert ws.path == path
        assert os.path.normcase(ws.slide_path) == os.path.normcase(os.path.abspath(blob_slide))
        assert ws.scene_index == 0
        assert ws.read_only is False
        assert ws.closed is False
        assert ws.layers == ()
        assert ws.rasters == ()


def test_create_from_scene(blob_slide, tmp_path):
    slide, scene = slide_scene(blob_slide)
    with Workspace.create(tmp_path / "ws", scene, scene=0) as ws:
        assert os.path.normcase(ws.slide_path) == os.path.normcase(os.path.abspath(blob_slide))
        assert ws.scene_index == 0
        ws.declare_layer("tissue")
        assert [layer.name for layer in ws.layers] == ["tissue"]


def test_create_from_scene_requires_scene_index(blob_slide, tmp_path):
    slide, scene = slide_scene(blob_slide)
    with pytest.raises(InvalidArgumentError, match="give scene="):
        Workspace.create(tmp_path / "ws", scene)
    assert not (tmp_path / "ws").exists()


def test_open_with_scene_requires_scene_index(blob_slide, tmp_path):
    Workspace.create(tmp_path / "ws", blob_slide).close()
    slide, scene = slide_scene(blob_slide)
    with pytest.raises(InvalidArgumentError, match="give scene="):
        Workspace.open(tmp_path / "ws", slide=scene)


def test_open_reopens_recorded_slide(blob_slide, tmp_path):
    with Workspace.create(tmp_path / "ws", blob_slide) as ws:
        ws.declare_layer("tissue")
    with Workspace.open(tmp_path / "ws") as ws:
        assert ws.read_only is False
        assert [layer.name for layer in ws.layers] == ["tissue"]
    slide, scene = slide_scene(blob_slide)
    with Workspace.open(tmp_path / "ws", slide=scene, scene=0) as ws:
        assert [layer.name for layer in ws.layers] == ["tissue"]


def test_open_with_path_slide_refused(blob_slide, tmp_path):
    Workspace.create(tmp_path / "ws", blob_slide).close()
    with pytest.raises(InvalidArgumentError, match="Workspace: "):
        Workspace.open(tmp_path / "ws", slide=str(blob_slide))


def test_create_from_scene_of_other_file_refused(blob_slide, seam_slide, tmp_path):
    Workspace.create(tmp_path / "ws", seam_slide).close()
    slide, scene = slide_scene(blob_slide)
    with pytest.raises(InvalidArgumentError):
        Workspace.open(tmp_path / "ws", slide=scene, scene=0)
    # The refusal changed nothing: the workspace still opens over its own slide.
    with Workspace.open(tmp_path / "ws") as ws:
        assert ws.scene_index == 0


def test_open_with_scene_index_mismatch_refused(blob_slide, tmp_path):
    Workspace.create(tmp_path / "ws", blob_slide).close()
    slide, scene = slide_scene(blob_slide)
    with pytest.raises(InvalidArgumentError):
        Workspace.open(tmp_path / "ws", slide=scene, scene=1)


def test_existing_nonempty_directory_refused(blob_slide, tmp_path):
    path = tmp_path / "ws"
    path.mkdir()
    (path / "keep.txt").write_text("x")
    with pytest.raises(IoError):
        Workspace.create(path, blob_slide)
    assert (path / "keep.txt").exists()


def test_second_writer_refused(blob_slide, tmp_path):
    with Workspace.create(tmp_path / "ws", blob_slide) as first:
        with pytest.raises(IoError):
            Workspace.open(tmp_path / "ws")
        first.declare_layer("tissue")
        assert [layer.name for layer in first.layers] == ["tissue"]


def test_read_only_alongside_writer(blob_slide, tmp_path):
    with Workspace.create(tmp_path / "ws", blob_slide) as writer:
        writer.declare_layer("tissue")
        with Workspace.open_read_only(tmp_path / "ws") as reader:
            assert reader.read_only is True
            assert [layer.name for layer in reader.layers] == ["tissue"]
            with pytest.raises(IoError):
                reader.declare_layer("nuclei")


def test_layers_round_trip(blob_slide, tmp_path):
    with Workspace.create(tmp_path / "ws", blob_slide) as ws:
        ws.declare_layer("tissue")
        ws.declare_layer("nuclei", parent="tissue")
        ws.declare_primitive_layer("fragments")
        assert ws.layers == (
            Layer("tissue", LayerKind.SEMANTIC, None, False, 0),
            Layer("nuclei", LayerKind.SEMANTIC, "tissue", False, 0),
            Layer("fragments", LayerKind.PRIMITIVE, None, False, 0),
        )
        assert isinstance(ws.layers[0].kind, LayerKind)
        # The same declaration again is accepted.
        ws.declare_layer("nuclei", parent="tissue")
        ws.declare_layer("tissue")
        ws.declare_primitive_layer("fragments")
        assert len(ws.layers) == 3
        # Another definition under an existing name is refused.
        with pytest.raises(InvalidArgumentError, match="nuclei"):
            ws.declare_layer("nuclei")
        with pytest.raises(InvalidArgumentError, match="fragments"):
            ws.declare_layer("fragments")
        ws.drop_layer("fragments")
        assert ws.layers[2] == Layer("fragments", LayerKind.PRIMITIVE, None, True, 0)
        with pytest.raises(InvalidArgumentError, match="tissue"):
            ws.drop_layer("tissue")
        with pytest.raises(InvalidArgumentError, match="nowhere"):
            ws.drop_layer("nowhere")


def test_unknown_parent_refused(blob_slide, tmp_path):
    with Workspace.create(tmp_path / "ws", blob_slide) as ws:
        with pytest.raises(InvalidArgumentError, match="stroma"):
            ws.declare_layer("nuclei", parent="stroma")
        assert ws.layers == ()


def test_bad_names_refused(blob_slide, tmp_path):
    with Workspace.create(tmp_path / "ws", blob_slide) as ws:
        with pytest.raises(TypeError):
            ws.declare_layer(3)
        with pytest.raises(InvalidArgumentError):
            ws.declare_layer("a\0b")


def test_rasters_declared(blob_slide, tmp_path):
    with Workspace.create(tmp_path / "ws", blob_slide) as ws:
        ws.declare_raster("prob", "float32")
        assert ws.rasters == (Raster("prob", numpy.dtype("float32"), 1, False),)
        ws.declare_raster("rgb", numpy.uint8, planes=3)
        assert ws.rasters[1] == Raster("rgb", numpy.dtype("uint8"), 3, False)
        ws.declare_raster("prob", numpy.float32)  # the same again is accepted
        assert len(ws.rasters) == 2
        with pytest.raises(InvalidArgumentError, match="prob"):
            ws.declare_raster("prob", "float64")
        with pytest.raises(InvalidArgumentError, match="complex64"):
            ws.declare_raster("c", "complex64")
        with pytest.raises(InvalidArgumentError, match="planes"):
            ws.declare_raster("p0", "uint8", planes=0)


def test_closed_workspace_refuses(blob_slide, tmp_path):
    path = str(tmp_path / "ws")
    ws = Workspace.create(path, blob_slide)
    ws.close()
    assert ws.closed is True
    assert ws.path == path
    assert ws.scene_index == 0
    assert ws.read_only is False
    assert ws.slide_path
    with pytest.raises(IoError, match="closed"):
        ws.layers
    with pytest.raises(IoError, match="closed"):
        ws.declare_raster("prob", "float32")
    ws.close()  # a second close does nothing
    with Workspace.create(tmp_path / "ws2", blob_slide) as other:
        pass
    assert other.closed is True
    # The writer lock was released: the workspace opens again.
    Workspace.open(tmp_path / "ws2").close()


def test_not_a_scene_refused(tmp_path):
    with pytest.raises((TypeError, InvalidArgumentError), match="path or a slideio.Scene"):
        Workspace.create(tmp_path / "ws", object(), scene=0)


def test_invalid_capsule_refused(blob_slide, tmp_path):
    class Fake:
        file_path = str(blob_slide)

        def _native_handle(self):
            return object()

    with pytest.raises(InvalidArgumentError, match="path or a slideio.Scene"):
        Workspace.create(tmp_path / "ws", Fake(), scene=0)


def test_unclosed_workspace_dropped_releases_lock(blob_slide, tmp_path):
    ws = Workspace.create(tmp_path / "ws", blob_slide)
    ws.declare_layer("tissue")
    del ws
    gc.collect()
    # The destructor closed the store (with the GIL released): the lock is free again.
    with Workspace.open(tmp_path / "ws") as again:
        assert [layer.name for layer in again.layers] == ["tissue"]


def test_read_only_refuses_every_declare(blob_slide, tmp_path):
    with Workspace.create(tmp_path / "ws", blob_slide) as ws:
        ws.declare_layer("tissue")
        ws.declare_primitive_layer("fragments")
        ws.declare_raster("prob", "float32")
    with Workspace.open_read_only(tmp_path / "ws") as reader:
        # Identical to what exists, yet refused: a read-only workspace declares nothing.
        with pytest.raises(IoError, match="read-only"):
            reader.declare_layer("tissue")
        with pytest.raises(IoError, match="read-only"):
            reader.declare_primitive_layer("fragments")
        with pytest.raises(IoError, match="read-only"):
            reader.declare_raster("prob", "float32")
        with pytest.raises(IoError, match="read-only"):
            reader.drop_layer("fragments")
        with pytest.raises(IoError, match="read-only"):
            reader.drop_layer("nowhere")


def test_raster_dtype_messages(blob_slide, tmp_path):
    with Workspace.create(tmp_path / "ws", blob_slide) as ws:
        with pytest.raises(InvalidArgumentError, match=">f4"):
            ws.declare_raster("big", ">f4")
        with pytest.raises(InvalidArgumentError, match="not a NumPy dtype"):
            ws.declare_raster("bad", "not-a-dtype")
        with pytest.raises(InvalidArgumentError, match="not a NumPy dtype"):
            ws.declare_raster("bad", (numpy.float32, -1))
        assert ws.rasters == ()
