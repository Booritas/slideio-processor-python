"""Workspace: the directory that binds one slide scene to a store, with its layers and rasters."""
import dataclasses
import enum
import os
from typing import Optional, Tuple

import numpy

from ._loader import load_native
from .configuration import Configuration
from .errors import InvalidArgumentError, IoError
from .pipeline_file import _path_string, _text
from .stages import StageDefinition, StageInfo, _declare, _info

_native = load_native()

_INT32_MAX = 2**31 - 1
_MAX_PLANES = 65535

# The NumPy dtypes a raster may have, as the library's DataType values.
_RASTER_DTYPES = tuple(numpy.dtype(name) for name in (
    "uint8", "int8", "uint16", "int16", "uint32", "int32",
    "uint64", "int64", "float16", "float32", "float64"))


class LayerKind(enum.IntEnum):
    """Where a layer sits: in the semantic hierarchy, or outside it (primitive)."""

    SEMANTIC = 0
    PRIMITIVE = 1


@dataclasses.dataclass(frozen=True)
class Layer:
    """A declared layer. parent is None for a primitive layer and for a semantic layer
    directly above the pixel layer. object_count counts the objects stored in the layer,
    whatever their stage's status; dropped tells whether the (primitive) layer was dropped."""

    name: str
    kind: LayerKind
    parent: Optional[str]
    dropped: bool
    object_count: int


@dataclasses.dataclass(frozen=True)
class Raster:
    """A declared raster: its sample type, its plane count, and whether a stage has written it."""

    name: str
    dtype: numpy.dtype
    planes: int
    populated: bool


def _name(value, what="the name"):
    try:
        return _text(value, what)
    except (TypeError, InvalidArgumentError) as error:
        raise type(error)(f"Workspace: {error}") from None


def _path(value, what):
    if not isinstance(value, (str, os.PathLike)):
        raise TypeError(f"Workspace: {what} must be a str or os.PathLike, got {value!r}")
    try:
        return _path_string(value)
    except (TypeError, InvalidArgumentError) as error:
        raise type(error)(f"Workspace: {what}: {error}") from None


def _configuration(configuration):
    if configuration is None:
        configuration = Configuration()
    if not isinstance(configuration, Configuration):
        raise TypeError(f"Workspace: configuration must be a Configuration or None, got {configuration!r}")
    return configuration._native()


def _is_scene(slide):
    # Decision 4: a slideio.Scene is recognised by duck typing.
    return hasattr(slide, "_native_handle") and hasattr(slide, "file_path")


def _scene_index(scene):
    if not isinstance(scene, int) or isinstance(scene, bool):
        raise TypeError(f"Workspace: scene must be an int, got {scene!r}")
    if not -_INT32_MAX - 1 <= scene <= _INT32_MAX:
        raise InvalidArgumentError(f"Workspace: scene {scene} is out of range")
    return int(scene)


def _require_scene_index(scene):
    if scene is None:
        raise InvalidArgumentError("Workspace: give scene=, the index of this scene in its slide")
    return _scene_index(scene)


class Workspace:
    """A directory holding one slide scene's analysis: its store, the binding to the slide
    and scene, and a scratch area.

    create() and open() give a writable workspace, which holds the store's writer lock
    until close(): a second writer, in this process or another, raises IoError.
    open_read_only() takes no lock and runs alongside a writer. Use it as a context
    manager, or call close().

    Over a slideio.Scene, the workspace shares the scene and keeps it, and with it its
    slide, open; scene= must then say which scene of its slide it is, since a Scene cannot
    tell. Reopening the workspace without a scene reopens the slide by its recorded path.

    Layers and rasters are named by str. Declaring one that exists with the same definition
    does nothing; with another definition it raises InvalidArgumentError. In a read-only
    workspace every declare, drop and mark_stale raises IoError, whatever it names. A closed
    workspace still answers path, slide_path, scene_index, read_only and closed; every other
    member raises IoError.
    """

    __slots__ = ("_native", "_closed", "_path", "_slide_path", "_scene_index", "_read_only")

    def __init__(self, native):
        """Not for direct use: call create(), open() or open_read_only()."""
        self._native = native
        self._closed = False
        self._path = native.path()
        self._slide_path = native.slide_path()
        self._scene_index = native.scene_index()
        self._read_only = native.is_read_only()

    @classmethod
    def create(cls, path, slide, *, scene=None, configuration=None) -> "Workspace":
        """Creates a workspace at path, which must not exist or be an empty directory, over
        slide: a slide path (str or os.PathLike; scene defaults to 0) or a slideio.Scene
        (scene= required)."""
        path = _path(path, "the workspace path")
        if _is_scene(slide):
            index = _require_scene_index(scene)
            slide_path = _path(slide.file_path, "the scene's file path")
            native_configuration = _configuration(configuration)
            return cls(_native._Workspace.create_from_scene(
                path, slide._native_handle(), slide_path, index, native_configuration))
        if isinstance(slide, (str, os.PathLike)):
            slide_path = _path(slide, "the slide path")
            index = 0 if scene is None else _scene_index(scene)
            return cls(_native._Workspace.create_from_path(path, slide_path, index, _configuration(configuration)))
        raise TypeError(f"Workspace: the slide must be a path or a slideio.Scene, got {slide!r}")

    @classmethod
    def open(cls, path, *, slide=None, scene=None, configuration=None) -> "Workspace":
        """Opens an existing workspace for writing. Without slide, the slide is reopened from
        the path the workspace recorded; with a slideio.Scene of that slide, the scene is
        bound instead, and scene= must equal the recorded index."""
        path = _path(path, "the workspace path")
        if slide is None:
            if scene is not None:
                raise InvalidArgumentError("Workspace: scene= is given only with a slideio.Scene")
            return cls(_native._Workspace.open(path, _configuration(configuration)))
        if _is_scene(slide):
            index = _require_scene_index(scene)
            native_configuration = _configuration(configuration)
            return cls(_native._Workspace.open_with_scene(path, slide._native_handle(), index, native_configuration))
        if isinstance(slide, (str, os.PathLike)):
            raise InvalidArgumentError(
                f"Workspace: open takes no slide path ({slide!r}): the workspace records its slide; "
                "give a slideio.Scene or None")
        raise TypeError(f"Workspace: the slide must be a slideio.Scene or None, got {slide!r}")

    @classmethod
    def open_read_only(cls, path, *, configuration=None) -> "Workspace":
        """Opens an existing workspace without the writer lock: everything is readable,
        nothing may be declared, run or written."""
        path = _path(path, "the workspace path")
        return cls(_native._Workspace.open_read_only(path, _configuration(configuration)))

    @property
    def path(self) -> str:
        return self._path

    @property
    def slide_path(self) -> str:
        """The slide's path, absolute and normalised, as the workspace recorded it."""
        return self._slide_path

    @property
    def scene_index(self) -> int:
        return self._scene_index

    @property
    def read_only(self) -> bool:
        return self._read_only

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """Closes the store, releasing the writer lock, and releases the slide. A second
        call does nothing. When closing the store fails, IoError is raised and the
        workspace is closed even so."""
        if self._closed:
            return
        self._closed = True
        self._native.close()

    def __enter__(self) -> "Workspace":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __repr__(self):
        state = "closed" if self._closed else ("read-only" if self._read_only else "writable")
        return f"Workspace({self._path!r}, slide={self._slide_path!r}, scene={self._scene_index}, {state})"

    def _handle(self):
        """The native workspace; raises IoError once the workspace is closed."""
        if self._closed:
            raise IoError("Workspace: the workspace is closed")
        return self._native

    def _writable_handle(self):
        """The native workspace; raises IoError once it is closed or when it is read-only,
        before anything is looked up."""
        handle = self._handle()
        if self._read_only:
            raise IoError("Workspace: the workspace is read-only")
        return handle

    @property
    def layers(self) -> Tuple[Layer, ...]:
        """The declared layers in declaration order, the pixel layer excluded."""
        rows = self._handle().layers()
        return tuple(Layer(name, LayerKind(kind), parent, dropped, count)
                     for name, kind, parent, dropped, count in rows)

    def declare_layer(self, name, *, parent=None) -> None:
        """Declares a semantic layer directly above parent, a semantic layer's name; None
        puts it directly above the pixel layer."""
        handle = self._writable_handle()
        name = _name(name, "the layer name")
        if parent is not None:
            parent = _name(parent, "the parent name")
        handle.declare_layer(name, parent)

    def declare_primitive_layer(self, name) -> None:
        """Declares a primitive layer, outside the semantic hierarchy."""
        handle = self._writable_handle()
        handle.declare_primitive_layer(_name(name, "the layer name"))

    def drop_layer(self, name) -> None:
        """Discards every object of a primitive layer, keeping the layer. Objects derived
        from it stay; no stage becomes stale. Dropping a dropped layer does nothing."""
        handle = self._writable_handle()
        handle.drop_layer(_name(name, "the layer name"))

    @property
    def rasters(self) -> Tuple[Raster, ...]:
        """The declared rasters in declaration order."""
        return tuple(Raster(name, numpy.dtype(dtype), planes, populated)
                     for name, dtype, planes, populated in self._handle().rasters())

    def declare_raster(self, name, dtype, *, planes=1) -> None:
        """Declares a raster of planes planes of dtype (anything numpy.dtype() accepts that
        is an 8- to 64-bit integer or a 16-, 32- or 64-bit float)."""
        handle = self._writable_handle()
        name = _name(name, "the raster name")
        try:
            resolved = numpy.dtype(dtype)
        except (TypeError, ValueError):
            raise InvalidArgumentError(f"Workspace: {dtype!r} is not a NumPy dtype") from None
        if resolved not in _RASTER_DTYPES:
            # repr shows a non-native byte order (dtype('>f4')), where .name would say float32.
            raise InvalidArgumentError(
                f"Workspace: {resolved!r} is not a raster dtype; use one of "
                f"{[d.name for d in _RASTER_DTYPES]}, in native byte order")
        if not isinstance(planes, int) or isinstance(planes, bool):
            raise TypeError(f"Workspace: planes must be an int, got {planes!r}")
        if not 1 <= planes <= _MAX_PLANES:
            raise InvalidArgumentError(f"Workspace: planes must be from 1 to {_MAX_PLANES}, got {planes}")
        handle.declare_raster(name, resolved.name, int(planes))

    @property
    def stages(self) -> Tuple[StageInfo, ...]:
        """The declared stages in the registry's order (ascending id)."""
        handle = self._handle()
        return tuple(_info(_native._stage_info(handle, name)) for name in _native._stage_names(handle))

    def stage(self, name) -> StageInfo:
        """The stage named name; InvalidArgumentError when there is none."""
        handle = self._handle()
        return _info(_native._stage_info(handle, _name(name, "the stage name")))

    def declare_stage(self, definition: StageDefinition) -> StageInfo:
        """Declares a stage, resolving its layer and raster names, and returns its state.
        Declaring the same definition again changes nothing; a changed definition replaces
        the stored one and marks the stage and its dependents stale. An unknown layer or
        raster raises InvalidArgumentError naming it; a cycle or a stage depending on
        itself raises StageError."""
        handle = self._writable_handle()
        name = _declare(handle, definition)
        return _info(_native._stage_info(handle, name))

    def mark_stale(self, name, *, downstream=False) -> None:
        """Marks the stage and every stage its re-run would invalidate as stale; with
        downstream=True, only those other stages, not the stage itself."""
        handle = self._writable_handle()
        _native._mark_stale(handle, _name(name, "the stage name"), bool(downstream))
