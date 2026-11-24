# slideio-processor workspaces and results — Implementation Plan (plan 3 of 4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** From Python, a researcher opens a workspace over a slide (a path or an open `slideio.Scene`), declares layers, rasters and stages, runs stages, reads the objects back as records, NumPy batches, DataFrames or summaries, writes the four export kinds, and saves what they built as a pipeline file for `SlideRun`.

**Architecture:**
- Each library class gets a thin native binding and a Pythonic wrapper, as plans 1 and 2 did. Python code resolves nothing by id: every public call takes and returns names, and the native layer turns names into `LayerId`, `StageId` and `RasterId`.
- Every native call that can wait on a library lock runs with the GIL released. Long calls (stage runs, exports) run on a helper thread so Ctrl-C cancels them; plan 1's `SlideRun` loop moves into a shared helper.
- Results stream: a cursor fetches a batch of records with the GIL released, then converts it to Python. Nothing materialises the whole result set.
- One small library change (Task 1) makes the export code `SlideRun` uses callable on any open workspace, so Python exports behave exactly like a run's.

**Tech Stack:** C++17, pybind11 3.1.0, NumPy, pandas (optional extra, tests install it), pytest. The build and environment are those of plans 1 and 2.

**Spec:** slideio-processor repository, `docs/superpowers/specs/2026-11-22-python-bindings-design.md`. This plan covers §5.2 "config", "storage and workspace", "queries and results", "pipeline and algorithm" and "reporting and masks"; D3 (a workspace over a `slideio.Scene`); §7 (runs that do not block, result summaries). Plans 1 and 2 are `docs/superpowers/plans/2026-11-23-core.md` and `2026-11-24-python-operations.md` in this repository. Plan 4 is release wheels (`auditwheel`, `delocate`, every Python version).

**Two repositories:**
- Task 1 is in `D:/Projects/slideio/slideio-processor`, on a new branch `feat/workspace-export` cut from `main` (3ec7eb4).
- Tasks 2–7 are in this repository, on a new branch `feat/workspace` cut from `main` (90a40ac).
- Task 2 points `extern/slideio-processor` at Task 1's head commit: `git -C extern/slideio-processor fetch D:/Projects/slideio/slideio-processor feat/workspace-export`, check that commit out, then `git submodule update --init --recursive` inside the submodule only.
- Pushing, PRs and the final re-pin to the library's merge commit are for the finishing step, on the user's instruction. This repository's `origin` is HTTPS with no push URL; push by URL `git@github.com-personal:Booritas/slideio-processor-python.git` with `--recurse-submodules=no`.

## Decisions this plan makes (beyond the spec)

1. **Names, not ids, in Python.** Layers, stages, rasters and labels are named by `str` everywhere in the public API. An unknown name raises `InvalidArgumentError` naming it. `ObjectId` is its string form (`ObjectId::toString` / `fromString`).
2. **Stages are built with the library's builders, not from stage-record JSON.** The record parser (`detail::stageFromRecord`) is a test-only export. `StageDefinition` in Python is a frozen dataclass of names; the native layer resolves the names and calls `StageDefinition::readsLayer(...)` and so on. A pipeline's operations travel as `PipelineSpec` JSON (`PipelineSpec::fromJson`), which already validates every field.
3. **Exports go through one library function** (Task 1), `writeExport`, which `SlideRun` also uses. The Python `Workspace.export` takes the plan-1 `ExportSpec`, so a workspace export and a run's export of the same spec write the same file.
4. **A workspace over a `slideio.Scene`** is recognised by duck typing: an object with `_native_handle` and `file_path`. The slide path given to the library is `scene.file_path`. Because a `slideio.Scene` cannot tell its own index, `scene=` is required with a `Scene` and optional (default 0) with a path.
   - A transformed scene (`slideio.transform_scene`) reports its origin's file path, so a workspace accepts it. Reopening that workspace by path binds the untransformed slide; README says so.
5. **Configuration** is a frozen dataclass whose `None` fields keep the library default. It is accepted by `Workspace.create/open/open_read_only` and now by `SlideRun` (`configuration=`).
6. **Logging** has one entry point, `configure_logging(level, *, file=None, python=False)`. `python=True` installs a library log sink that forwards to the standard `logging` logger `"slideio_processor"`. The sink enters Python only through plan 2's exit gate (`enterPython`/`leavePython`), so it never takes the GIL in an exiting interpreter, and holds no Python object.
7. **A closed workspace** answers `path`, `slide_path`, `scene_index`, `read_only` and `closed`; every other member raises `IoError("Workspace: the workspace is closed")`. `close()` while a stage run or an export of the same object is in progress raises `InvalidArgumentError`. A cursor that outlives its workspace's `close()` raises the library's `IoError` on its next fetch.
8. **One long call per `Workspace` object at a time** (a stage run or an export), guarded by a Python lock, as `SlideRun` does. Queries may run alongside it from other threads.
9. **`PipelineFile.from_workspace(ws)`** also calls `require()` for the module of every Python operation in the workspace's stages, read from each stage record's `"operationOrigins"` (`"<module>:<qualname>[ <version>]"`). The module `__main__` is skipped: an operation defined in a notebook or script must move to a module before a pipeline file can run it, and the docstring says so.
10. **Measurement values only.** Records, batches and DataFrames carry measurement values as `float`; units are not exposed in this plan (the CSV export carries them). A measurement an object lacks is `NaN` in batches and DataFrames and absent from the record's dict.
11. **No raster read-back to NumPy** in this plan. Rasters are declared, written by stages and exported as TIFF. Reading a region of a raster into NumPy is left for a later plan.
12. **The aux-image route of the project-2 consumer test is not repeated.** The test slides here have no auxiliary image; project 2's tests cover `Slide.get_aux_image` keeping its slide alive. This plan's consumer test covers `Slide.get_scene` and `slideio.transform_scene` (Task 6).

## Global Constraints

- Everything in plans 1 and 2's Global Constraints still holds: D2 loading, `SLIDEIO_PROCESSOR_TEST_SEAMS=OFF`, tests against the installed wheel from a directory outside the checkout, conda env `slideio`. Messages may name paths and argument values (decision of 2026-11-23); exports still carry no slide path, and `slide_id` defaults to `""`.
- **GIL rule.** Every native call that reaches the store, the workspace, a cursor, a stage run or an export runs with the GIL released (`py::gil_scoped_release` or `py::call_guard<py::gil_scoped_release>`). Conversion to and from Python objects happens with the GIL held, before or after. Waiting on a library lock with the GIL held can deadlock against a worker thread that logs through the Python sink.
- New public names, re-exported from `slideio_processor` and added to `__all__`:
  `Configuration`, `LogLevel`, `configure_logging`, `Workspace`, `Layer`, `LayerKind`, `Raster`, `PipelineSpec`, `StageDefinition`, `StageInfo`, `StageStatus`, `RunBlocker`, `LayerRasterContent`, `StageRunResult`, `ObjectQuery`, `SpatialPredicate`, `ObjectRecord`, `ObjectBatch`, `MeasurementSummary`, `ResultSummary`.
- Python enums mirror the C++ enums' integer values (`enum.IntEnum`), with upper-case member names: `LogLevel` (TRACE, DEBUG, INFO, WARNING, ERROR, OFF), `LayerKind` (SEMANTIC, PRIMITIVE), `StageStatus` (PENDING, RUNNING, COMPLETE, FAILED), `RunBlocker` (NONE, UNREADABLE, UNRESOLVED_DEPENDENCY, DEPENDENCY_INCOMPLETE, DEPENDENCY_STALE), `LayerRasterContent` (LABEL, MEMBERSHIP, OBJECT_ANCHOR), `SpatialPredicate` (INTERSECTS, CONTAINED_BY).
- DataType ↔ NumPy dtype: plan 2's table (Byte↔uint8 … Float64↔float64). Use plan 2's `numpyName` in `src/python_runtime.hpp` for the native direction.
- Coordinates are level-0 pixels. A rectangle is `(x, y, width, height)` of ints. A polygon is a sequence of rings, each `(points, hole)`: `points` an array-like of shape `(n, 2)` of ints, `hole` a bool; the first ring is the outer one.
- Every error message this plan's Python code writes starts with the class or function name and `": "`: `Workspace: `, `PipelineSpec: `, `StageDefinition: `, `ObjectQuery: `, `Configuration: `, `configure_logging: `. Argument validation runs before native calls (plan 1's `_text`, `_path_string`), so a bad argument gives `InvalidArgumentError` or `TypeError` naming the value, never pybind11's overload text.
- Native code: one file per area, each declared in `src/bindings.hpp` and called from `src/module.cpp`: `src/configuration.cpp` (`bindConfiguration`), `src/workspace.cpp` (`bindWorkspace`), `src/stages.cpp` (`bindStages`), `src/objects.cpp` (`bindObjects`), `src/exports.cpp` (`bindExports`), and `src/progress.hpp/.cpp` (the progress observer shared by runs, stage runs and exports). Add each to `CMakeLists.txt`.
- Python code: `slideio_processor/configuration.py`, `workspace.py`, `stages.py`, `objects.py`, `_runner.py`, `_geometry.py`.
- Tests that need pandas use `pytest.importorskip("pandas")`; CI installs pandas (Task 6 adds it to the workflow's test dependencies).

## Review Focus

1. **The workspace is closed, or its last Python reference dropped, while a cursor or a batch iterator is still in use.** Expect `IoError` on the next fetch, never a crash or a hang. Tests: Task 6 `test_cursor_after_close_raises`, `test_cursor_keeps_workspace_alive`.
2. **The Python `Slide` and `Scene` are gone before the workspace reads pixels** (D3). Expect the stage to read the scene through the handle and measure the true pixel values. Test: Task 6 `test_scene_handle_outlives_python_objects`.
3. **A second writer opens the same workspace** (a second notebook, a second `Workspace.open`). Expect `IoError` from the second, and the first keeps working. Test: Task 3 `test_second_writer_refused`.
4. **Ctrl-C or a cancellation during a stage run or an export.** Expect `CancelledError` (or `KeyboardInterrupt` for Ctrl-C) once the work has stopped, and no partial export file. Tests: Task 5 `test_run_stage_cancelled`, `test_run_stage_keyboard_interrupt`, Task 7 `test_export_cancelled_leaves_no_file`.
5. **Python logging is on while worker threads log and the main thread queries, and the interpreter then exits.** Expect log records, no deadlock, exit code 0. Tests: Task 2 `test_python_logging_receives_records`, `test_exit_with_python_logging_is_clean`; Task 6 `test_query_during_stage_run_with_python_logging`.

---

### Task 1: Library — `writeExport` on an open workspace

Repository `D:/Projects/slideio/slideio-processor`, branch `feat/workspace-export` from `main` (3ec7eb4).

**Files:**
- Create: `include/slideio/processor/run/workspace_export.hpp`, `src/run/workspace_export.cpp`
- Modify: `src/run/slide_run.cpp` (`SlideRun::Impl::writeExport` and its helpers move out), `src/run/CMakeLists.txt`, `include/slideio/processor/run/run.hpp` (include the new header), the run module's SDS and `traceability.md` where `SlideRun`'s exports are described
- Test: `tests/run/test_workspace_export.cpp` (add to the run tests' CMake list)

**Interfaces:**
- Produces:
  ```cpp
  namespace slideio::processor {
  /// Writes one export of `workspace` to `path` (UTF-8, the full file path), as SlideRun's step 5
  /// does: through the export's writer, which writes "<path>.partial" and renames it.
  SLIDEIO_PROCESSOR_ENGINE_EXPORTS ExportResult writeExport(Workspace& workspace, const ExportSpec& spec,
                                                            const char* path, const char* slideId,
                                                            ProgressObserver* observer,
                                                            const CancellationToken& token);
  }
  ```
  - `slideId` null is `""`. The parent directory of `path` must exist; the function makes no directory.
  - Refusals are `InvalidArgumentError` starting `"writeExport: "`: `path` null or empty; the layer of an object export not in the workspace; the raster of a raster export not in the workspace, or not yet written by any stage. Each names the layer or raster.
  - A cancelled write returns the writer's `ExportResult` with `wasCancelled() == true` and leaves no file; the caller decides what to raise. Works on a read-only workspace.
- `SlideRun` calls it and keeps its own messages and behaviour: existing SlideRun tests pass unchanged.

- [ ] **Step 1: Write the failing tests** in `tests/run/test_workspace_export.cpp`, fixture: a workspace from the run tests' existing slide fixture with the three-stage pipeline file run by `SlideRun` (reuse the helpers `test_slide_run.cpp` uses):
  - `WorkspaceExport.CsvMatchesSlideRun`: `writeExport` of the file's CSV spec on the reopened workspace gives the same bytes as the run's CSV, after dropping lines that start with `#`.
  - `WorkspaceExport.EachKindWrites`: GeoJSON, label mask and raster TIFF exports each return `bytesWritten() > 0` and the file exists with no `.partial` beside it.
  - `WorkspaceExport.ReadOnlyWorkspace`: the CSV export from `Workspace::openReadOnly` succeeds.
  - `WorkspaceExport.UnknownLayerRefused` and `UnknownRasterRefused`: `InvalidArgumentError`, `what()` starts with `"writeExport: "` and contains the name.
  - `WorkspaceExport.UnwrittenRasterRefused`: a raster declared but never written → `InvalidArgumentError` naming it.
  - `WorkspaceExport.CancelledLeavesNoFile`: a cancelled token → `wasCancelled()`, neither `path` nor `path + ".partial"` exists.
- [ ] **Step 2: Build and run; expect compile failure** (`writeExport` undeclared).
- [ ] **Step 3: Move the export code.** `SlideRun::Impl::writeExport`, `layerOf` and the level-table helpers it uses move to `src/run/workspace_export.cpp` behind the new function; `SlideRun::Impl::writeExports` calls it. Keep SlideRun's directory creation, its `CancelledError` texts and its pre-run validation where they are.
- [ ] **Step 4: Run the whole library suite** (`ctest` in the release build, as in `CLAUDE.md`). Expect all tests to pass: the 1354 existing ones plus the 7 new ones.
- [ ] **Step 5: Docs.** Add `writeExport` to the run module's SDS section on exports and a traceability line next to SlideRun's export requirement.
- [ ] **Step 6: Commit** `feat(run): writeExport writes one export of an open workspace`.

---

### Task 2: Configuration and logging

**Files:**
- Modify: `extern/slideio-processor` (pin to Task 1's head), `src/bindings.hpp`, `src/module.cpp`, `CMakeLists.txt`, `src/slide_run.cpp` (take a configuration), `slideio_processor/run.py` (`configuration=`), `slideio_processor/__init__.py`
- Create: `src/configuration.cpp`, `slideio_processor/configuration.py`
- Test: `tests/test_configuration.py`, `tests/test_logging.py`

**Interfaces:**
- Produces (Python):
  ```python
  class LogLevel(enum.IntEnum): TRACE = 0; DEBUG = 1; INFO = 2; WARNING = 3; ERROR = 4; OFF = 5

  @dataclasses.dataclass(frozen=True)
  class Configuration:
      memory_ceiling_bytes: Optional[int] = None
      worker_count: Optional[int] = None
      tile_size: Optional[Tuple[int, int]] = None      # (width, height)
      tile_size_from_slide: Optional[bool] = None
      log_file: Optional[Union[str, os.PathLike]] = None
      log_level: Optional[LogLevel] = None
      object_batch_size: Optional[int] = None
      scratch_directory: Optional[Union[str, os.PathLike]] = None
      def _native(self) -> "_native._Configuration"   # ConfigurationBuilder(defaults) + set fields + build()

  def configure_logging(level: LogLevel, *, file=None, python: bool = False) -> None
  ```
  - `Configuration` validates types in `__post_init__` (bool before int; `tile_size` a pair of ints) and raises `TypeError` naming the field. Value ranges are the library's: `_native()` calls `ConfigurationBuilder::build()`, whose `InvalidArgumentError` passes through.
  - `configure_logging`: `file` and `python=True` together → `InvalidArgumentError`. With `file`, it calls `configureLogging(Configuration)` with that log file and level. With `python=True`, it calls `configureLogging(sink, level)` with the Python sink. With neither, `configureLogging(Configuration)` with only the level, which restores the library's default sink.
  - `SlideRun(..., configuration=None)`: `None` is `Configuration.defaults()` as today.
- Produces (native, `src/configuration.cpp`): `_Configuration` (opaque), `_build_configuration(dict) -> _Configuration`, `_configure_logging_file(_Configuration)`, `_configure_logging_python(int level)`.
- Python log sink: a `LogSink` whose `write` returns at once unless `enterPython()` succeeds; then it takes the GIL, calls `logging.getLogger("slideio_processor").log(pyLevel, "%s", message)`, swallows (and clears) any Python error, releases the GIL and calls `leavePython()`. Level map: TRACE→5, DEBUG→10, INFO→20, WARNING→30, ERROR→40. It holds no Python object.

- [ ] **Step 1: Pin the submodule** to Task 1's head (see "Two repositories"), rebuild with `build_deps.py` and the wheel as in `CLAUDE.md`, and run the existing suite: 115 pass. Commit `chore: pin extern/slideio-processor to <sha7>`.
- [ ] **Step 2: Write the failing tests.**
  `tests/test_configuration.py`:
  ```python
  def test_defaults_build(): Configuration()._native()      # no error
  def test_fields_reach_the_library():
      c = Configuration(worker_count=2, tile_size=(256, 512), object_batch_size=100)
      n = c._native()
      assert (n.worker_count(), n.tile_width(), n.tile_height(), n.object_batch_size()) == (2, 256, 512, 100)
  def test_wrong_type_refused():
      with pytest.raises(TypeError, match="worker_count"): Configuration(worker_count=True)
      with pytest.raises(TypeError, match="tile_size"): Configuration(tile_size=(1, 2, 3))
  def test_library_range_check(): # ConfigurationBuilder::build: "workerCount must be >= 1"
      with pytest.raises(InvalidArgumentError, match="workerCount"): Configuration(worker_count=0)._native()
  def test_slide_run_takes_configuration(blob_slide, tmp_path, three_stage_json):
      # SlideRun with Configuration(worker_count=1) runs and gives the same stage results as the default
  ```
  (The native `_Configuration` exposes read accessors `worker_count()`, `tile_width()`, `tile_height()`, `object_batch_size()`, `memory_ceiling_bytes()` for these tests.)
  `tests/test_logging.py` (a fixture restores `configure_logging(LogLevel.WARNING)` after each test):
  - `test_python_logging_receives_records(caplog, blob_slide, tmp_path, three_stage_json)`: `configure_logging(LogLevel.DEBUG, python=True)`; run the three-stage `SlideRun` twice on one workspace (the reopen logs the scratch and raster sweep); `caplog` has at least one record with `name == "slideio_processor"`.
  - `test_file_logging_writes(tmp_path, ...)`: `configure_logging(LogLevel.DEBUG, file=tmp_path / "log.txt")`, run twice, the file is non-empty.
  - `test_file_and_python_refused`: `InvalidArgumentError`, message starts `configure_logging: `.
  - `test_exit_with_python_logging_is_clean`: a subprocess (as plan 2's exit tests) enables Python logging at DEBUG, runs the three-stage `SlideRun`, and exits without cleanup; exit code 0.
- [ ] **Step 3: Run them; expect failures** (`ImportError` on the new names).
- [ ] **Step 4: Implement** `src/configuration.cpp`, `slideio_processor/configuration.py`, the `SlideRun` change (native constructor takes a `_Configuration`), exports from `__init__.py`.
- [ ] **Step 5: Build, install, run the whole suite.** Expect all to pass.
- [ ] **Step 6: Commit** `feat: Configuration and configure_logging, with a Python log sink`.

---

### Task 3: Workspace, layers and rasters

**Files:**
- Create: `src/workspace.cpp`, `slideio_processor/workspace.py`
- Modify: `src/bindings.hpp`, `src/module.cpp`, `CMakeLists.txt`, `slideio_processor/__init__.py`
- Test: `tests/test_workspace.py`

**Interfaces:**
- Consumes: `Configuration._native()` (Task 2); `_text`, `_path_string` (`pipeline_file.py`).
- Produces (Python, `workspace.py`):
  ```python
  class LayerKind(enum.IntEnum): SEMANTIC = 0; PRIMITIVE = 1

  @dataclasses.dataclass(frozen=True)
  class Layer: name: str; kind: LayerKind; parent: Optional[str]; dropped: bool; object_count: int

  @dataclasses.dataclass(frozen=True)
  class Raster: name: str; dtype: numpy.dtype; planes: int; populated: bool

  class Workspace:
      @classmethod
      def create(cls, path, slide, *, scene=None, configuration=None) -> "Workspace"
      @classmethod
      def open(cls, path, *, slide=None, scene=None, configuration=None) -> "Workspace"
      @classmethod
      def open_read_only(cls, path, *, configuration=None) -> "Workspace"
      path: str; slide_path: str; scene_index: int; read_only: bool; closed: bool   # properties
      def close(self) -> None
      def __enter__(self) -> "Workspace"; def __exit__(self, *exc) -> None          # closes
      layers: Tuple[Layer, ...]           # declaration order, the pixel layer excluded
      def declare_layer(self, name, *, parent=None) -> None        # semantic; parent None = top level
      def declare_primitive_layer(self, name) -> None
      def drop_layer(self, name) -> None                           # primitive layers only (library rule)
      rasters: Tuple[Raster, ...]
      def declare_raster(self, name, dtype, *, planes=1) -> None
  ```
  - `create`: `slide` is a path (str or `os.PathLike`) or a `slideio.Scene` (decision 4). With a path, `scene=None` means 0. With a `Scene`, `scene=None` raises `InvalidArgumentError("Workspace: give scene=, the index of this scene in its slide")`.
  - `open`: `slide=None` reopens the recorded slide by path; with a `Scene`, `scene` is required as in `create`. A path given as `slide` to `open` is refused (`InvalidArgumentError`): the workspace records its slide.
  - A semantic layer with `parent=None` hangs from the pixel layer, as the pipeline file does. Declaring a layer or raster that exists with the same definition returns without error (the library's idempotence, which `SlideRun` relies on).
- Produces (native, `src/workspace.cpp`): `_Workspace` bound as `py::class_<Workspace, std::shared_ptr<Workspace>>`, with static `create_from_path`, `create_from_scene(path, capsule, slide_path, scene, cfg)`, `open`, `open_with_scene(path, capsule, scene, cfg)`, `open_read_only`; accessors; `layers()` returning a list of `(name, kind, parent_or_None, dropped, object_count)`; `declare_layer`, `declare_primitive_layer`, `drop_layer`, `rasters()` returning `(name, numpy dtype name, planes, populated)`, `declare_raster(name, dtype_name, planes)`.
  - The capsule is checked with `PyCapsule_IsValid(capsule, "slideio.Scene")`; its pointer is a `std::shared_ptr<slideio::Scene>*`, copied (never moved or freed). An invalid capsule → `InvalidArgumentError("Workspace: the slide must be a path or a slideio.Scene")`.
  - Every call that reaches the library releases the GIL (Global Constraints).
- Shared helper for later tasks: `Workspace._handle()` returns the native `_Workspace` after the closed check.

- [ ] **Step 1: Write the failing tests** in `tests/test_workspace.py` (a `slide_scene(blob_slide)` helper opens `slideio.open_slide(str(blob_slide), "AUTO")` and returns `(slide, slide.get_scene(0))`; use the driver id plan 1's tests use):
  - `test_create_from_path(blob_slide, tmp_path)`: properties: `path`, `slide_path == os.path.abspath(blob_slide)` (normalised), `scene_index == 0`, `read_only is False`, `closed is False`; `layers == ()`, `rasters == ()`.
  - `test_create_from_scene(blob_slide, tmp_path)`: `Workspace.create(p, scene, scene=0)` works and `slide_path` equals the path form.
  - `test_create_from_scene_requires_scene_index`: `InvalidArgumentError` matching `"give scene="`.
  - `test_create_from_scene_of_other_file_refused(blob_slide, seam_slide, tmp_path)`: a scene of `blob_slide` with `Workspace.open` of a workspace made on `seam.svs` → `InvalidArgumentError`.
  - `test_open_with_scene_index_mismatch_refused`: open with `scene=1` → `InvalidArgumentError`.
  - `test_existing_nonempty_directory_refused`: `IoError`.
  - `test_second_writer_refused`: a second `Workspace.open` of an open workspace → `IoError`; the first still declares a layer afterwards.
  - `test_read_only_alongside_writer`: `open_read_only` while a writer is open sees the writer's committed layers; `read_only is True`.
  - `test_layers_round_trip`: declare `tissue`, `nuclei` (parent `tissue`), primitive `fragments`; `layers` gives the three with kinds and parents; declaring `nuclei` again with parent `tissue` is accepted; `drop_layer("fragments")` → its `dropped` is True.
  - `test_unknown_parent_refused`: `InvalidArgumentError` naming the parent.
  - `test_rasters_declared`: `declare_raster("prob", "float32")` → `Raster("prob", numpy.dtype("float32"), 1, False)`; an unsupported dtype (`"complex64"`) → `InvalidArgumentError` naming it.
  - `test_closed_workspace_refuses`: after `close()`, `path` answers and `layers` raises `IoError` matching `"closed"`; a second `close()` does nothing; `with Workspace.create(...) as ws:` closes on exit.
  - `test_not_a_scene_refused`: `Workspace.create(p, object(), scene=0)` → `TypeError` or `InvalidArgumentError` naming what is accepted.
- [ ] **Step 2: Build, install, run; expect failures.**
- [ ] **Step 3: Implement** `src/workspace.cpp` and `slideio_processor/workspace.py`; export the names.
- [ ] **Step 4: Build, install, run the whole suite; all pass.**
- [ ] **Step 5: Commit** `feat: Workspace over a path or a slideio.Scene, with layers and rasters`.

---

### Task 4: Pipeline specs, stage definitions and the stage registry

**Files:**
- Create: `src/stages.cpp`, `slideio_processor/stages.py`, `slideio_processor/_geometry.py`, `tests/support/workspaces.py` (holding `nuclei_spec()`)
- Modify: `slideio_processor/workspace.py`, `src/bindings.hpp`, `src/module.cpp`, `CMakeLists.txt`, `slideio_processor/__init__.py`
- Test: `tests/test_stages.py`

**Interfaces:**
- Consumes: `Workspace._handle()` (Task 3); `Operation.stage_parameters(**values)` (plan 2).
- Produces (`_geometry.py`): `rings(polygon) -> list[tuple[numpy.ndarray, bool]]` (int64 arrays of shape `(n, 2)`; refuses fewer than 3 points, a wrong shape or a non-bool hole with `InvalidArgumentError`), `rect(value) -> tuple[int, int, int, int]` (four ints, width and height > 0).
- Produces (`stages.py`):
  ```python
  class PipelineSpec:
      def __init__(self, *, channels="all", analysis_level=-1, roi=None, polygon=None)
      def add(self, operation, **parameters) -> "PipelineSpec"   # operation: str or an Operation subclass
      operations: List[Tuple[str, dict]]
      def to_json(self) -> str                                   # the library's canonical JSON
      @classmethod
      def from_json(cls, text) -> "PipelineSpec"
      def __eq__(self, other) -> bool                            # PipelineSpec::equals

  class LayerRasterContent(enum.IntEnum): LABEL = 0; MEMBERSHIP = 1; OBJECT_ANCHOR = 2
  class StageStatus(enum.IntEnum): PENDING = 0; RUNNING = 1; COMPLETE = 2; FAILED = 3
  class RunBlocker(enum.IntEnum): NONE = 0; UNREADABLE = 1; UNRESOLVED_DEPENDENCY = 2; DEPENDENCY_INCOMPLETE = 3; DEPENDENCY_STALE = 4

  @dataclasses.dataclass(frozen=True)
  class StageDefinition:
      name: str
      pipeline: PipelineSpec
      reads_layers: Tuple[str, ...] = ()
      writes_layers: Tuple[str, ...] = ()
      annotates_layers: Tuple[str, ...] = ()
      depends_on: Tuple[str, ...] = ()
      writes_raster: Optional[str] = None
      reads_rasters: Mapping[str, str] = {}                                  # binding -> raster
      reads_layers_as_raster: Mapping[str, Tuple[str, LayerRasterContent]] = {}   # binding -> (layer, content)
      restrict_to_layer: Optional[str] = None

  @dataclasses.dataclass(frozen=True)
  class StageInfo:
      name: str; status: StageStatus; stale: bool; deterministic: bool
      completed_at: Optional[datetime.datetime]       # UTC; None when never completed
      dependencies: Tuple[str, ...]; unresolved_dependencies: Tuple[str, ...]
      runnable: bool; blocker: RunBlocker; blocker_name: Optional[str]
      record: str                                     # the stage's configuration JSON
  ```
  - `PipelineSpec` keeps a Python dict in the JSON layout `PipelineSpec::fromJson` reads (`specVersion` 1, `channels` `"all"` or a list of ints, `analysisLevel`, optional `roi` `[x, y, w, h]`, optional `polygon` `[{"hole": bool, "points": [[x, y], ...]}, ...]`, `operations` `[{"name", "parameters"}]`). `to_json`, `__eq__` and declaring build the native spec with `fromJson`, which validates. Parameter values may be `bool`, `int`, `float`, `str` or a list/tuple of numbers; anything else → `TypeError` naming the key. `add(cls, **values)` uses `cls.name` and `cls.stage_parameters(**values)`.
  - Mutable defaults: store mappings as `types.MappingProxyType` of a copy and sequences as tuples in `__post_init__`.
- Produces (`Workspace`, in `workspace.py`):
  ```python
  def declare_stage(self, definition: StageDefinition) -> StageInfo
  stages: Tuple[StageInfo, ...]              # registry order
  def stage(self, name) -> StageInfo         # unknown name → InvalidArgumentError
  def mark_stale(self, name, *, downstream=False) -> None   # markStale / markAffectedStale
  ```
- Produces (native, `src/stages.cpp`): `_PipelineSpec` (`from_json`, `to_json`, `equals`); `_declare_stage(ws, name, spec, reads_layers, writes_layers, annotates_layers, depends_on, writes_raster_or_None, reads_rasters: list[(binding, raster)], reads_layers_as_raster: list[(binding, layer, content)], restrict_to_layer_or_None) -> str`, resolving every name through `layers().find` and `store()->rasters().find` with `InvalidArgumentError("Workspace: no layer named '<x>'")` / `"... no raster named '<x>'"`; `_stage_names(ws)`, `_stage_info(ws, name) -> tuple`, `_mark_stale(ws, name, downstream)`.

- [ ] **Step 1: Write the failing tests** in `tests/test_stages.py`. Helper `nuclei_spec()` in `tests/support/workspaces.py` = `PipelineSpec().add("pixel.colour_convert", **{"from": "Rgb", "to": "Grey"}).add("pixel.threshold_fixed", value=128.0, direction="Below").add("segment.connected_components")`.
  - `test_spec_json_round_trip`: `PipelineSpec.from_json(s.to_json()) == s`; the JSON parses with `json.loads` and has `"operations"` of length 3.
  - `test_spec_options`: `channels=[0]`, `analysis_level=0`, `roi=(0, 0, 300, 300)`, `polygon=[([(0, 0), (300, 0), (300, 300)], False)]` all appear in `json.loads(to_json())`.
  - `test_spec_bad_parameter_type`: `add("pixel.threshold_fixed", value=object())` → `TypeError` matching `"value"`.
  - `test_spec_bad_roi`: `roi=(0, 0, -1, 5)` → `InvalidArgumentError`.
  - `test_spec_python_operation`: with a test `Operation` subclass `name="test.grey"`, `version="1"`, `add(Cls)` gives parameters containing `"version": "1"`.
  - `test_declare_stage(blob_slide, tmp_path)`: declare layer `nuclei`, then `StageDefinition("nuclei", nuclei_spec(), writes_layers=("nuclei",))`; the returned `StageInfo` has `status is StageStatus.PENDING`, `runnable is True`, `blocker is RunBlocker.NONE`, `completed_at is None`, `json.loads(info.record)["stage"] == "nuclei"`; `ws.stages` names `("nuclei",)`; `ws.stage("nuclei") == info`.
  - `test_redeclare_identical_is_accepted`: declaring the same definition twice gives equal `StageInfo`.
  - `test_unknown_layer_refused`: `writes_layers=("missing",)` → `InvalidArgumentError` matching `"missing"`.
  - `test_unresolved_dependency_blocks`: `depends_on=("later",)` → `unresolved_dependencies == ("later",)`, `runnable is False`, `blocker is RunBlocker.UNRESOLVED_DEPENDENCY`, `blocker_name == "later"`.
  - `test_raster_stage`: declare raster `prob` float32, stage with `writes_raster="prob"` and `PipelineSpec().add("pixel.colour_convert", **{"from": "Rgb", "to": "Grey"}).add("pixel.gaussian_blur")` is accepted.
  - `test_unknown_stage_refused`: `ws.stage("x")` → `InvalidArgumentError`.
- [ ] **Step 2: Build, install, run; expect failures.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Build, install, run the whole suite; all pass.**
- [ ] **Step 5: Commit** `feat: PipelineSpec, StageDefinition and the stage registry`.

---

### Task 5: Running stages, and pipeline files from a workspace

**Files:**
- Create: `src/progress.hpp`, `src/progress.cpp`, `slideio_processor/_runner.py`
- Modify: `tests/support/workspaces.py`, `src/slide_run.cpp` (observer and `raiseKept` move to `progress.*`), `src/stages.cpp` (`_run_stage`), `slideio_processor/run.py` (uses `_runner`), `slideio_processor/workspace.py`, `slideio_processor/pipeline_file.py` (`from_workspace`), `src/pipeline_file.cpp`, `tests/conftest.py`, `slideio_processor/__init__.py`, `CMakeLists.txt`
- Test: `tests/test_run_stage.py`, `tests/test_pipeline_file.py` (add)

**Interfaces:**
- Consumes: Tasks 3–4; plan 2's `_operation._error_mark()` / `_take_errors()`.
- Produces (`_runner.py`):
  ```python
  def run_interruptibly(call: Callable[[], T], cancellation: Cancellation) -> T
      # plan 1's helper-thread loop from SlideRun._run_locked, unchanged in behaviour:
      # Ctrl-C cancels and raises KeyboardInterrupt after the call stops; the call's exception
      # is re-raised with its __context__ kept.
  @contextlib.contextmanager
  def operation_causes():   # takes _error_mark(); on a ProcessorError with operation_name sets
                            # __cause__ from _take_errors(name, mark); always clears in finally
  ```
  `SlideRun.run` is rewritten on these two; its tests pass unchanged.
- Produces (`src/progress.hpp`): `class PythonProgress : public ProgressObserver` and `[[noreturn]] void raiseKept(const py::object&, const std::exception_ptr&)`, moved verbatim from `src/slide_run.cpp`.
- Produces (Python):
  ```python
  @dataclasses.dataclass(frozen=True)
  class StageRunResult: name: str; rerun: bool; objects_written: int; edges_asserted: int; truncated_objects: int

  # Workspace
  def run_stage(self, name, *, progress=None, cancellation=None) -> StageRunResult

  # PipelineFile
  @classmethod
  def from_workspace(cls, workspace) -> "PipelineFile"
  ```
  - `run_stage`: refused with `InvalidArgumentError` on a read-only workspace, or while another long call of the same object runs (decision 8). `progress(name, fraction, completed, total)` as `SlideRun`'s. Runs through `run_interruptibly` inside `operation_causes()`.
  - `from_workspace`: `PipelineFile::fromWorkspace`, then `require()` each module from the stages' `"operationOrigins"` (decision 9).
- Produces (`tests/support/workspaces.py` and a `conftest.py` fixture):
  ```python
  def nuclei_definition() -> StageDefinition   # stage "nuclei" writing layer "nuclei" with Task 4's nuclei_spec()
  @pytest.fixture
  def nuclei_workspace(blob_slide, tmp_path)   # creates the workspace, declares layer and stage, runs it,
                                               # yields the open Workspace, closes it after the test
  ```

- [ ] **Step 1: Write the failing tests** in `tests/test_run_stage.py`:
  - `test_run_stage_writes_objects(blob_slide, tmp_path)`: `r = run_stage("nuclei")` has `r.name == "nuclei"`, `r.objects_written == 9`, `r.rerun is False`, `r.truncated_objects == 0` (the PNG is one tile); then `stage("nuclei").status is StageStatus.COMPLETE` and `completed_at` is not None.
  - `test_rerun_reports_rerun(nuclei_workspace)`: `run_stage("nuclei").rerun is True`.
  - `test_run_stage_progress(blob_slide, tmp_path)`: the callable sees `"nuclei"` and a last `fraction == 1.0`.
  - `test_run_stage_progress_raises`: the callable's exception is raised, with a `CancelledError` as `__context__`.
  - `test_run_stage_cancelled`: a pre-cancelled `Cancellation` → `CancelledError`; the stage is not `COMPLETE`.
  - `test_run_stage_keyboard_interrupt`: as plan 1's SlideRun Ctrl-C test (signal from a timer thread with a slow Python operation): `KeyboardInterrupt`, and the stage is not `COMPLETE`.
  - `test_python_operation_failure_is_the_cause`: a `RASTER` op whose `apply` raises `ValueError("boom")`, in a raster stage → `StageError` with `__cause__` that `ValueError`.
  - `test_read_only_refuses_run`: `InvalidArgumentError`.
  - `test_concurrent_long_call_refused`: while a slow stage runs on a thread, a second `run_stage` on the same object → `InvalidArgumentError`; `close()` meanwhile → `InvalidArgumentError`.
  In `tests/test_pipeline_file.py`:
  - `test_from_workspace(nuclei_workspace)`: `stage_names == ["nuclei"]`, `layer_names == ["nuclei"]`, `requires == []`.
  - `test_from_workspace_requires_python_operation_modules`: a stage using `sample_ops`'s operation (plan 2's `tests/data/opmodule`, on `sys.path` as its tests arrange) → `requires == ["sample_ops"]`; an operation whose class body sets `__module__ = "__main__"` is used in a second stage and adds nothing to `requires`.
  - `test_from_workspace_runs_with_slide_run(nuclei_workspace, blob_slide, tmp_path)`: the file runs in `SlideRun` on a fresh workspace and its stage writes 9 objects.
- [ ] **Step 2: Build, install, run; expect failures.**
- [ ] **Step 3: Implement**: move the observer, add `_run_stage` (GIL released around `StageRunner::run`), `_runner.py`, rewrite `SlideRun.run` on it, `Workspace.run_stage`, `from_workspace` (native `_PipelineFile.from_workspace(_Workspace)` with the GIL released), the fixture.
- [ ] **Step 4: Build, install, run the whole suite; all pass, including every existing SlideRun test.**
- [ ] **Step 5: Commit** `feat: run stages of a workspace; PipelineFile.from_workspace`.

---

### Task 6: Queries, records, batches, DataFrames and summaries

**Files:**
- Create: `src/objects.cpp`, `slideio_processor/objects.py`
- Modify: `slideio_processor/workspace.py`, `src/bindings.hpp`, `src/module.cpp`, `CMakeLists.txt`, `slideio_processor/__init__.py`, `.github/workflows/build.yml` (install `pandas` for the tests)
- Test: `tests/test_objects.py`, `tests/test_scene_handle.py`

**Interfaces:**
- Consumes: `nuclei_workspace` (Task 5), `_geometry.rings/rect` (Task 4).
- Produces (`objects.py`):
  ```python
  class SpatialPredicate(enum.IntEnum): INTERSECTS = 0; CONTAINED_BY = 1

  @dataclasses.dataclass(frozen=True)
  class ObjectQuery:
      layer: Optional[str] = None
      stage: Optional[str] = None
      intersecting: Optional[Tuple[int, int, int, int]] = None
      within: Optional[Sequence] = None                 # a polygon (Global Constraints)
      within_predicate: SpatialPredicate = SpatialPredicate.INTERSECTS
      label: Optional[str] = None
      at_least: Mapping[str, float] = {}                # measurementAtLeast
      at_most: Mapping[str, float] = {}                 # measurementAtMost
      include_in_flight: bool = False                   # StageVisibility.IncludeInFlight
      limit: Optional[int] = None

  @dataclasses.dataclass(frozen=True)
  class ObjectRecord:
      id: str; layer: str; stage: str
      bbox: Tuple[int, int, int, int]
      label: Optional[str]
      measurements: Mapping[str, float]
      geometry: Tuple[Tuple[numpy.ndarray, bool], ...]  # rings, int64 (n, 2)

  @dataclasses.dataclass(frozen=True)
  class ObjectBatch:
      ids: Tuple[str, ...]; layers: Tuple[str, ...]; stages: Tuple[str, ...]
      bbox: numpy.ndarray                                # (n, 4) int64
      labels: Tuple[Optional[str], ...]
      measurements: Mapping[str, numpy.ndarray]          # name -> (n,) float64, NaN when absent
      geometry: Optional[Tuple[Tuple[Tuple[numpy.ndarray, bool], ...], ...]]   # when asked for

  @dataclasses.dataclass(frozen=True)
  class MeasurementSummary: count: int; mean: float; std: float; min: float; max: float   # std: population

  @dataclasses.dataclass(frozen=True)
  class ResultSummary: count: int; measurements: Mapping[str, MeasurementSummary]
  ```
- Produces (`Workspace`): each takes `query=None, **filters`, where `filters` are `ObjectQuery`'s fields; both given → `InvalidArgumentError`.
  ```python
  measurement_names: Tuple[str, ...]                         # ObjectStore::measurementNameAt
  def count(self, query=None, **filters) -> int
  def objects(self, query=None, **filters) -> Iterator[ObjectRecord]          # fetches in batches of 1024
  def object(self, object_id: str) -> Optional[ObjectRecord]                  # bad id string → InvalidArgumentError
  def batches(self, query=None, *, batch_size=10000, measurements=None, geometry=False, **filters) -> Iterator[ObjectBatch]
  def dataframe(self, query=None, *, measurements=None, **filters) -> "pandas.DataFrame"
  def summary(self, query=None, *, measurements=None, **filters) -> ResultSummary
  ```
  - `measurements=None` means every name in `measurement_names`; a name not there → `InvalidArgumentError` naming it.
  - `dataframe` columns, in order: `id`, `layer`, `stage`, `x`, `y`, `width`, `height`, `label`, then the measurements. Without pandas: `ImportError("Workspace.dataframe needs pandas: pip install slideio-processor[pandas]")`.
  - `summary` streams over `batches`; NaN values are skipped per measurement; a measurement with no values has `count == 0` and NaN statistics.
  - Iterators hold a reference to the `Workspace` (decision 7), and close their native cursor when exhausted or garbage-collected.
- Produces (native, `src/objects.cpp`): `_query(ws, query_tuple) -> _Cursor`, `_count(ws, query_tuple) -> int`, `_get(ws, id_str) -> record_tuple or None`, `_Cursor.fetch(n, names, with_geometry) -> batch tuple or None at the end`, `_Cursor.close()`. `fetch` reads up to `n` records with the GIL released into C++ vectors (ids as strings, layer and stage ids, bboxes, label ids, measurement values by name, rings), then, with the GIL, resolves names and builds the NumPy arrays. The `_Cursor` keeps the `WorkspacePtr` alive.

- [ ] **Step 1: Write the failing tests** in `tests/test_objects.py` (area constant `DISC = math.pi * 40.5 ** 2`, plan 2's ruling for Pillow's 81-px disc):
  - `test_count(nuclei_workspace)`: `count(layer="nuclei") == 9`; `count(layer="nuclei", intersecting=(0, 0, 200, 200)) == 1`; `count(layer="nuclei", limit=4) == 4`.
  - `test_objects_records(nuclei_workspace)`: 9 records, each `layer == "nuclei"`, `stage == "nuclei"`, `abs(measurements["area"] - DISC) / DISC < 0.02`, bbox width and height 81, `geometry[0][1] is False` and its array has shape `(n, 2)`; `object(r.id) == r` for the first.
  - `test_measurement_bounds(nuclei_workspace)`: `count(layer="nuclei", at_least={"area": DISC * 2}) == 0`, `at_most={"area": DISC * 2}` gives 9.
  - `test_within_polygon(nuclei_workspace)`: a triangle covering only the disc at (100, 100) with `CONTAINED_BY` gives 1.
  - `test_batches(nuclei_workspace)`: `batch_size=2` gives sizes `[2, 2, 2, 2, 1]`; `bbox.dtype == numpy.int64`, shape `(n, 4)`; `measurements["area"].dtype == numpy.float64`; `geometry is None` unless `geometry=True`.
  - `test_unknown_names_refused(nuclei_workspace)`: `layer="missing"`, `stage="missing"`, `measurements=["missing"]` and `object("not-an-id")` each → `InvalidArgumentError` naming it.
  - `test_query_and_filters_together_refused`.
  - `test_dataframe(nuclei_workspace)`: `pytest.importorskip("pandas")`; 9 rows, the first eight columns as listed, `area` present.
  - `test_summary(nuclei_workspace)`: `count == 9`; `area` summary `count == 9`, mean within 2% of `DISC`, `min <= mean <= max`, `std >= 0`; equals NumPy's `mean`, `std(ddof=0)`, `min`, `max` over `batches` within 1e-9 relative.
  - `test_cursor_after_close_raises(blob_slide, tmp_path)`: start `objects()`, take one record, `close()` the workspace → the next `next()` raises `IoError`.
  - `test_cursor_keeps_workspace_alive(blob_slide, tmp_path)`: start `batches(batch_size=2)`, drop the last `Workspace` reference and `gc.collect()`; the iterator still yields all 9.
  - `test_query_during_stage_run_with_python_logging(blob_slide, tmp_path)`: `configure_logging(LogLevel.DEBUG, python=True)`; a stage with a slow Python operation runs on a thread; meanwhile the main thread calls `count()` 20 times; both finish within 60 s (`threading.Thread.join(timeout)` then `assert not t.is_alive()`); restore logging afterwards.
  In `tests/test_scene_handle.py` (the project-2 consumer test, decision 12):
  - `test_scene_handle_outlives_python_objects(blob_slide, tmp_path)`: `slide = slideio.open_slide(...)`; `scene = slide.get_scene(0)`; `ws = Workspace.create(p, scene, scene=0)`; `del scene, slide`; `gc.collect()`; declare and run the nuclei stage; 9 objects, and every object's `intensity.0.mean`, `intensity.1.mean` and `intensity.2.mean` is within 0.5 of 20 (the discs' fill; segmenters measure the scene's samples).
  - `test_transformed_scene_is_accepted(blob_slide, tmp_path)`: a scene from `slideio.transform_scene(scene, <a Gaussian blur transformation>)` binds; after dropping every Python slide object and `gc.collect()`, the nuclei stage runs and writes 9 objects.
- [ ] **Step 2: Build, install, run; expect failures.**
- [ ] **Step 3: Implement** `src/objects.cpp`, `objects.py`, the `Workspace` methods; add `pandas` to the workflow's test install step.
- [ ] **Step 4: Build, install, run the whole suite; all pass.**
- [ ] **Step 5: Commit** `feat: object queries as records, NumPy batches, DataFrames and summaries`.

---

### Task 7: Exports, and the documentation

**Files:**
- Create: `src/exports.cpp`
- Modify: `slideio_processor/workspace.py`, `src/bindings.hpp`, `src/module.cpp`, `CMakeLists.txt`, `README.md`, `CLAUDE.md`
- Test: `tests/test_workspace_export.py`

**Interfaces:**
- Consumes: Task 1's `writeExport`; plan 1's `ExportSpec` and `ExportResult` (`run.py`); `_runner.run_interruptibly`; `src/progress.hpp`.
- Produces (`Workspace`):
  ```python
  def export(self, specs, out, *, slide_id="", progress=None, cancellation=None) -> Tuple[ExportResult, ...]
  ```
  - `specs` is one `ExportSpec` or a sequence of them; `out` a directory (str or `os.PathLike`), made with its parents if missing. Each spec writes `os.path.join(out, spec.file)` in order.
  - A cancelled export raises `CancelledError("Workspace: the <kind> export was cancelled")`, `<kind>` being the JSON spelling (`csv`, `geojson`, `labelMask`, `rasterTiff`). Refusals are the library's `InvalidArgumentError`.
  - Runs through `run_interruptibly`; one long call per object (decision 8). Allowed on a read-only workspace.
- Produces (native, `src/exports.cpp`): `_export(ws, spec, path, slide_id, progress, cancellation_source) -> (bytes, cancelled)`, GIL released around `writeExport`.

- [ ] **Step 1: Write the failing tests** in `tests/test_workspace_export.py`:
  - `test_csv_export(nuclei_workspace, tmp_path)`: `export(ExportSpec.csv("n.csv", "nuclei"), tmp_path / "out")` → one `ExportResult("n.csv", b)` with `b > 0`; the file has 9 data rows (lines not starting with `#`, minus the header).
  - `test_matches_slide_run(nuclei_workspace, blob_slide, tmp_path)`: `PipelineFile.from_workspace(ws).add_export(ExportSpec.csv("n.csv", "nuclei"))` run by `SlideRun` on a fresh workspace, and `ws.export` of the same spec: the non-`#` lines are equal.
  - `test_each_kind(nuclei_workspace, tmp_path)`: declare raster `blur` float32, a stage writing it (Task 4's raster spec), run it; export `geojson`, `label_mask` and `raster_tiff`; each file exists with no `.partial`; `json.load` of the GeoJSON has 9 features; `slideio.open_slide` of the label mask gives a scene 600×600 whose pixels hold exactly 9 distinct non-zero values.
  - `test_slide_id(nuclei_workspace, tmp_path)`: with `slide_id="S-1"`, the CSV contains `S-1`; with the default it contains no slide path (`str(blob_slide)` absent).
  - `test_read_only_export(nuclei_workspace, tmp_path)`: `open_read_only` of the same path exports the CSV.
  - `test_unknown_layer_refused`: `InvalidArgumentError` matching the name.
  - `test_export_cancelled_leaves_no_file(nuclei_workspace, tmp_path)`: a pre-cancelled `Cancellation` → `CancelledError`; neither the file nor `<file>.partial` exists.
- [ ] **Step 2: Build, install, run; expect failures.**
- [ ] **Step 3: Implement** `src/exports.cpp` and `Workspace.export`.
- [ ] **Step 4: Documentation.**
  - `README.md`: a "Workspaces" section with one worked example in this order: create from a `slideio.Scene`, declare a layer and a stage, `run_stage`, `count` / `dataframe` / `summary`, `export`, `PipelineFile.from_workspace(...).to_json()` saved for `SlideRun`. State decisions 4 (transformed scenes reopen untransformed by path), 7, 8, 9 (`__main__`), 10 and 11 in a sentence each. State that logs and exports may carry slide paths when the caller passes them (log file, `slide_id`), so their storage and access fall under the deployment's HIPAA Security Rule and GDPR controls.
  - `CLAUDE.md`: a "Workspaces and results" section listing the new files, the GIL rule, the long-call lock and the log sink's use of the exit gate.
- [ ] **Step 5: Build, install, run the whole suite; all pass.** Also `python scripts/check_pins.py`.
- [ ] **Step 6: Commit** `feat: Workspace.export; docs for workspaces and results`.
