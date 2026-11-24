# slideio-processor Python operations — Implementation Plan (plan 2 of 4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A researcher subclasses `slideio_processor.Operation`, and a pipeline file that names the operation runs it tile by tile inside `SlideRun`. The operation produces either a raster or a label image that the library turns into objects.

**Architecture:**
- Defining a subclass registers a C++ stand-in factory in the process-wide `OperationCatalog`.
- The factory answers every catalog question from a C++ snapshot of the class's declarations, so it never needs the GIL. It validates parameters in C++.
- A stand-in operation creates the Python instance on its first tile. It copies the tile into a NumPy array, calls `apply` under the GIL, checks the returned array, and either crops it into the raster output or hands it to the library's `LabelImageOperation` for objects.
- One small library change (Task 1) records where each operation came from in the stage record.

**Tech Stack:** C++17, pybind11 3.1.0, NumPy (C API through pybind11's `py::array`), pytest. The build and environment are the same as plan 1's.

**Spec:** slideio-processor repository, `docs/superpowers/specs/2026-11-22-python-bindings-design.md`. This plan covers §5.3 "Python operations", D6 and the provenance line of §5.3. §4.3 (label-image ownership) and §4.4 (nondeterminism flag) are already in the library: `LabelImageOperation` (`include/slideio/processor/algorithm/segment/label_image.hpp`) and `OperationFactory::isDeterministic()`. Plan 1 is `docs/superpowers/plans/2026-11-23-core.md` in this repository.

**Two repositories:**
- Task 1 is in `D:/Projects/slideio/slideio-processor`, on a new branch `feat/operation-origin` cut from `main` (a8c3b57).
- Tasks 2–5 are in this repository, on a new branch `feat/python-operations` cut from `main` (c9738a4).
- Task 2 points `extern/slideio-processor` at Task 1's head commit. Fetch it from the local clone: `git -C extern/slideio-processor fetch D:/Projects/slideio/slideio-processor feat/operation-origin`, then check out that commit and run `git submodule update --init --recursive` inside the submodule only.
- CI on this branch cannot pass until the library branch is pushed. Pushing, PRs and the final re-pin to the library's merge commit are for the finishing step, on the user's instruction.

## Decisions this plan makes (beyond the spec)

1. **Registration is automatic.** It happens in `Operation.__init_subclass__` for a class whose own body sets `name`. A class without its own `name` is an abstract intermediate and is not registered.
2. **Redefinition replaces.** Defining a class again under a registered Python operation name replaces the class, for example when a notebook cell is re-run. The C++ factory stays and its snapshot is swapped. A run already in progress keeps the class it started with.
   - Changing `output` between `LABELS` and `RASTER` on redefinition is refused.
   - A name that a built-in (non-Python) factory holds is refused.
3. **`version` is a reserved parameter.**
   - Every stage parameter set for a Python operation must carry `"version"` equal to the class's `version`. A missing or different version is refused when the run validates its operations, before any stage runs.
   - This is how a version bump makes the pipeline file's declaration differ, so the stage becomes stale (spec §5.3 "Staleness"). The file, not the code, is what gets declared.
   - `Operation.stage_parameters(**values)` builds the dict to put in a stage record.
4. **One Python instance per stand-in operation, created on its first tile.**
   - The library calls `OperationCatalog::create` several times per run: validation, then the run plan. Only the stand-in that receives tiles builds the instance, so `__init__` (a model load) runs once per stage that uses the operation.
   - Construction: a worker takes a `std::mutex` *without* the GIL, then takes the GIL, builds the instance, and releases both. Later calls see an atomic flag and skip the mutex. Taking the mutex before the GIL is what prevents deadlock.
5. **The tile is a copy, not a view.**
   - It is a C-contiguous `numpy.ndarray` of shape `(height, width, planes)`, always 3-D, over the halo rect, with the planes' common dtype.
   - The library's planes are separate buffers that live only during the call. A copy cannot dangle if the researcher keeps it, and costs little next to a model.
   - Python operations therefore require uniform input types (`requiresUniformTypes() == true`).
   - *This differs from the spec's "tile: NumPy view".*
6. **Returned arrays are checked strictly, never cast.**
   - `LABELS`: dtype `int32`, shape `(H, W)` or `(H, W, 1)`, where `H` and `W` are the halo rect's height and width.
   - `RASTER`: dtype `output_dtype`, shape `(H, W, output_planes)`, or `(H, W)` when `output_planes == 1`.
7. **The cause is chained through a Python-side record.**
   - When `__init__` or `apply` raises, the stand-in records `(operation name, exception)` in a process-wide list in `slideio_processor.operation`, under the GIL. It then throws a C++ exception whose `what()` is `"<ExceptionType>: <str(exception)>"`, and the library wraps that as a `StageError` naming the operation (the library's StageRunner wraps tile failures as `StageError`, which carries `operation_name` and the chained cause).
   - `SlideRun.run` sets `error.__cause__` to the first exception recorded for `error.operation_name` since that run started, then clears the run's records.
   - Two runs of the same operation at once in one process may attach the other run's exception. That is documented; the design runs one slide per process.
8. **`max_concurrency` is per registered name, not per instance.** It is a counting semaphore waited on *without* the GIL. A GPU is shared by every stage that uses the operation.
9. **Process exit.**
   - The catalog never removes a factory, and its static destructor runs after Python has finalized. The factory therefore holds its class as a strong reference that it never releases.
   - A stand-in that is destroyed while the interpreter is alive takes the GIL to drop its instance. When `Py_IsInitialized()` is false or the interpreter is finalizing, it leaks the instance instead.
   - An atexit exit gate (`closePython`) closes the entry to Python: once it has run, stand-ins no longer enter Python and leak their instances. It waits, with the GIL released, for threads inside Python to leave.
10. **`requires` is imported by `SlideRun.run`** on the calling thread, before the native run starts. A module that fails to import raises `InvalidArgumentError` naming the module, chained to the `ImportError`. The CLI adds nothing to `sys.path`; modules must be importable, for example installed or on `PYTHONPATH`.
11. **Origin** (provenance) is `"<module>:<qualname>"`, plus `" <version>"` when the module, or failing that its top-level package, has a `__version__`. The library writes it to the stage record (Task 1).

## Global Constraints

- Everything in plan 1's Global Constraints still holds: D2 loading, `SLIDEIO_PROCESSOR_TEST_SEAMS=OFF`, tests against the installed wheel from a directory outside the checkout, and the conda env `slideio`. The exception is its C-6 line, revoked 2026-11-23: messages may name paths, and exports still carry no slide path.
- Public Python names: `Operation`, `Output` (`Output.LABELS`, `Output.RASTER`), `TileContext`, `registered_operations()`. Re-export them from `slideio_processor` and add them to `__all__`.
- Class attributes and their rules:

  | Attribute | Rule |
  |---|---|
  | `name` | Required. Non-empty `str` without NUL. |
  | `version` | Required. Non-empty `str` without NUL. |
  | `output` | Required. An `Output`. |
  | `halo` | `int >= 0`, default `0`, in level pixels on all four sides. |
  | `deterministic` | `bool`, default `True`. |
  | `max_concurrency` | `None` or `int >= 1`, default `None`. |
  | `parameters` | `dict`, default `{}`; see below. |
  | `output_dtype` | `RASTER` only. Default `"float32"`; anything `numpy.dtype()` accepts that maps to a library DataType (see below). |
  | `output_planes` | `RASTER` only. `int >= 1`, default `1`. |
  | `working_set_bytes_per_pixel` | `int >= 0`, default `32`. Added to the base's per-level-pixel working set; it covers the tile copy and the returned array. |

- `parameters` (the attribute) maps each key to a default:
  - The default may be a `bool`, `int`, `float`, `str`, or a `list`/`tuple` of numbers (a double array). Check `bool` before `int`.
  - The keys `version` and `targetLayer` are reserved and refused.
  - A `LABELS` operation also accepts `targetLayer` (a str) in a stage's parameters, as built-in segmenters do.
- Parameter checks when the run validates operations (`OperationFactory::create`). Each message names the key, never the value:
  - An unknown key is refused.
  - A value whose type differs from the default's is refused. The one exception: a `float` parameter accepts an `int` value, because JSON writes `1` as an integer.
  - Missing keys take their defaults.
- Every error message this plan adds starts with `operation '<name>': `.
- DataType ↔ NumPy dtype:

  | Library DataType | NumPy dtype |
  |---|---|
  | Byte | uint8 |
  | Int8 | int8 |
  | UInt16 | uint16 |
  | Int16 | int16 |
  | UInt32 | uint32 |
  | Int32 | int32 |
  | UInt64 | uint64 |
  | Int64 | int64 |
  | Float16 | float16 |
  | Float32 | float32 |
  | Float64 | float64 |

- **GIL rules:**
  - No `OperationFactory` method touches Python.
  - A stand-in acquires the GIL only around `__init__`, `apply`, the array conversions, and recording an error.
  - A stand-in waits on the construction mutex and on the `max_concurrency` semaphore with the GIL released.
- Native code lives in `src/python_operation.cpp`, declared in `src/bindings.hpp` as `void bindOperations(py::module_& m)`. Python code lives in `slideio_processor/operation.py`.

## Review Focus

1. **The interpreter exits after an operation was registered and run.** Expect exit code 0: no crash from the catalog's static destructor or a late stand-in releasing Python objects. Tests: Task 2 `test_exit_after_registration_is_clean`, Task 3 `test_exit_after_run_is_clean`, both in a subprocess.
2. **A notebook cell defining the operation is re-run.** Expect the new class to replace the old one, with no "already registered" error, and the next run to use the new class. Test: Task 2 `test_redefinition_replaces_class`.
3. **`apply` returns the wrong array.** Examples: `int64` labels from `skimage.measure.label`, or a tile-sized array without the halo. Expect a `StageError` naming the operation and stating the expected and actual dtype or shape. Never a crash, a silent cast, or a misaligned raster. Tests: Task 3 `test_raster_wrong_shape_or_dtype_fails`, Task 4 `test_labels_must_be_int32`.
4. **Ctrl-C while a Python operation is in a long `apply`.** Expect `KeyboardInterrupt` in the caller, the run cancelled and no hang, even though worker threads keep taking the GIL. Test: Task 3 `test_keyboard_interrupt_during_python_apply`.
5. **A pipeline file whose stage carries an old `version`, or none.** Expect `InvalidArgumentError` naming the operation and both versions, raised before the workspace directory is created. Test: Task 2 `test_version_mismatch_refused_before_run`.

---

### Task 1 (slideio-processor): record each operation's origin in the stage record

**Files (in `D:/Projects/slideio/slideio-processor`, branch `feat/operation-origin`):**
- Modify:
  - `include/slideio/processor/pipeline/operation_catalog.hpp`
  - `src/pipeline/operation_catalog.cpp`
  - `src/workspace/detail/stage_record.cpp`, next to `nondeterministicOperations`
  - `docs/superpowers/specs/sds/04-workspace.md`, the paragraph after "Nondeterministic operations"
  - `docs/superpowers/specs/sds/05-pipeline.md`, §5.1 `OperationFactory`
  - `docs/traceability.md`
- Test: the test file that already covers `nondeterministicOperations`; find it with `grep -rln nondeterministicOperations tests`.

**Interfaces:**
- Produces:
  - `virtual const char* OperationFactory::origin() const;`: default `""`, never null, safe to call concurrently. Doxygen: "where the operation's code comes from, for provenance; empty for the library's own operations".
  - `const char* OperationCatalog::origin(const char* name) const;`: throws `InvalidArgumentError` for a null or unknown name, as its siblings do.
  - Stage record key `"operationOrigins"`:
    - An object mapping each operation of the stage's pipeline that is registered and has a non-empty origin to that origin.
    - Written in pipeline order, each name once, immediately after `nondeterministicOperations`, and only when non-empty.
    - Not part of `sameDeclaration`; the record parser ignores it.

- [ ] **Step 1: Write the failing tests.** Use a test factory whose `origin()` returns `"lab.models:Seg 1.0"`.
  - `OperationOrigin.RecordNamesOrigin`: declaring a stage that uses it writes a record containing `"operationOrigins":{"<name>":"lab.models:Seg 1.0"}`.
  - `OperationOrigin.BuiltInsWriteNoKey`: a stage of built-ins has no `operationOrigins` in its record.
  - `OperationOrigin.NotPartOfDeclaration`: redeclaring the same stage after the factory's origin changes is a no-op, and the stage is not stale.
  - `OperationOrigin.CatalogReportsOrigin`: `catalog.origin(name)` returns the text. It is `""` for `"segment.contours"`, and an unknown name throws `InvalidArgumentError`.
- [ ] **Step 2: Run them; expect a compile failure** (`origin` is not a member).
- [ ] **Step 3: Implement as the Interfaces block states.** Update SDS 04 and SDS 05 and add the traceability rows.
- [ ] **Step 4: Build and run the whole suite** (`ctest --test-dir build --output-on-failure -C Release`). Expect every test to pass (1350 before this task, plus the new ones).
- [ ] **Step 5: Commit** `feat(pipeline): OperationFactory::origin, recorded in stage records`.

### Task 2 (bindings): the `Operation` class, declarations and registration

**Files:**
- Create:
  - `slideio_processor/operation.py`
  - `src/python_operation.cpp`
  - `tests/test_operation_declaration.py`
  - `tests/support/pipelines.py`
- Modify:
  - `src/bindings.hpp`, `src/module.cpp`: call `bindOperations`
  - `CMakeLists.txt`: add the source
  - `slideio_processor/__init__.py`
  - `extern/slideio-processor`: the pin, to Task 1's head

**Interfaces:**
- Produces (Python):
  - `class Output(enum.Enum)`: `LABELS = "labels"`, `RASTER = "raster"`.
  - `class Operation`:
    - The class attributes from Global Constraints.
    - `__init__(self, parameters: dict)`: the base does nothing.
    - `apply(self, tile, context)`: abstract. A subclass that registers without overriding it raises `InvalidArgumentError` at class creation.
    - `@classmethod stage_parameters(cls, **values) -> dict`: the declared defaults updated with `values`, plus `"version": cls.version`. An unknown key raises `InvalidArgumentError`. `targetLayer` is accepted for `LABELS`.
  - `registered_operations() -> list[str]`: the catalog's names in its order.
  - `slideio_processor.operation._take_errors(name: str, since: int) -> list[BaseException]` and `_error_mark() -> int`: used by Task 3's chaining (decision 7).
- Produces (native, private):
  - `_native._register_python_operation(cls, snapshot: dict) -> None`. The snapshot keys are `name`, `version`, `output` (`"labels"`/`"raster"`), `halo`, `deterministic`, `max_concurrency` (0 = unlimited), `parameter_kinds` (`{key: "bool"|"int"|"double"|"string"|"doubleArray"}`), `output_type` (a library DataType name such as `"Float32"`), `output_planes`, `working_set_bytes_per_pixel` and `origin`.
  - `_native._catalog_names() -> list[str]`.
  - `_native._is_python_operation(name) -> bool`.
- `tests/support/pipelines.py`: `one_stage_pipeline(operation: str, parameters: dict, *, writes_layer: str | None = None, writes_raster: str | None = None, raster_type: str = "Float32", raster_planes: int = 1, requires: tuple = (), exports: tuple = ()) -> str`. It returns pipeline-file JSON in the form of `tests/data/three_stage.json`: one stage named `"python"`, `channels "all"`, `analysisLevel -1`, and the layer or raster declared.

- [ ] **Step 1: Write the failing tests** in `tests/test_operation_declaration.py`. Every test class uses a unique name, `"test.<test name>"`.
  - `test_subclass_registers`: after defining a `RASTER` class, its name is in `registered_operations()`.
  - `test_abstract_intermediate_not_registered`: a subclass without its own `name` registers nothing.
  - `test_declaration_errors`: parametrised. Each case raises `InvalidArgumentError` whose message contains the class name and the attribute:
    - missing `version`;
    - empty `name`;
    - `output = "labels"` (a str, not `Output`);
    - `halo = -1`;
    - `max_concurrency = 0`;
    - `parameters = {"version": 1}`;
    - `parameters = {"x": object()}`;
    - `output_dtype = "complex64"`;
    - `output_planes = 0`;
    - no `apply`.
  - `test_builtin_name_refused`: `name = "segment.contours"` raises `InvalidArgumentError`.
  - `test_redefinition_replaces_class`: define class A with `version = "1"`, then class B under the same name with `version = "2"`. Neither definition raises. A run whose stage carries `version "2"` passes validation and calls B's `apply`. A run whose stage carries `"1"` is refused.
  - `test_redefinition_cannot_change_output`: the same name with `LABELS` after `RASTER` raises `InvalidArgumentError`.
  - `test_stage_parameters`: `parameters = {"k": 0.5}` gives `stage_parameters(k=2.0) == {"k": 2.0, "version": "1"}`. `stage_parameters(nope=1)` raises `InvalidArgumentError`.
  - `test_unknown_parameter_refused_before_run`: a `SlideRun` whose stage parameters have an extra key raises `InvalidArgumentError` naming the key. The workspace directory does not exist afterwards.
  - `test_parameter_type_refused_before_run`: `{"k": "text"}` for a float `k` is refused, naming `k`. `{"k": 1}` (an int) for a float `k` is accepted. (Accepted means the run passes validation. With no `apply` behaviour yet, have `apply` return the right raster so the run completes.)
  - `test_version_mismatch_refused_before_run`: stage `version "0"` against class `version "1"` raises `InvalidArgumentError`. The message contains `'0'` and `'1'`. A missing `version` is refused too. The workspace directory does not exist afterwards.
  - `test_exit_after_registration_is_clean`: in `subprocess.run([sys.executable, "-c", ...])`, import, define and register a class, then exit. Expect `returncode == 0` and empty stderr.

  Tests that run need a minimal `apply`. Task 2 must therefore implement enough of the RASTER stand-in for a correct return to complete a run. Task 3 then adds the remaining behaviour and its tests.
- [ ] **Step 2: Build the wheel and run the tests; expect failures** (`Operation` is not defined).
- [ ] **Step 3: Implement.**
  - `__init_subclass__` validates the class and builds the snapshot.
  - The C++ `PythonOperationFactory` keeps the class with `Py_INCREF` and never releases it. Its snapshot is a `std::shared_ptr<const Snapshot>` swapped under a `std::mutex`, for redefinition.
  - `arity()` is Joint. `requiresUniformTypes()` is true. `supportedTypes()` is `DataTypeSet::all()`. `producesObjects()` is true for `LABELS` only. `isDeterministic()`, `halo()` and `origin()` come from the snapshot.
  - `create(parameters)` checks the parameters (Global Constraints, decision 3) in C++. It returns a stand-in that holds the snapshot and the class, and constructs no Python object.
  - Raster stand-in for this task: copy the tile, call `apply` under the GIL, and copy the returned array's tile region into `context.output()`, with the minimal checks needed not to read out of bounds. Task 3 hardens it.
- [ ] **Step 4: Rebuild, reinstall, run the whole suite.** Expect plan 1's 64 tests plus the new ones to pass.
- [ ] **Step 5: Commit** `feat: Python operations register in the catalog`. The submodule bump is a separate commit before it: `chore: pin extern/slideio-processor to feat/operation-origin`.

### Task 3 (bindings): raster operations run, with the GIL, concurrency and error rules

**Files:**
- Modify:
  - `src/python_operation.cpp`
  - `slideio_processor/operation.py`
  - `slideio_processor/run.py`: chaining the cause (decision 7)
- Create:
  - `tests/test_operation_raster.py`
  - the `seam_slide` fixture in `tests/conftest.py`

**Interfaces:**
- Consumes: Task 2's `Operation`, snapshot, stand-in and `tests/support/pipelines.py`.
- Produces:
  - `class TileContext`, passed as `apply`'s `context`, read-only:
    - `level: int`
    - `tile_rect`, `halo_rect: tuple[int, int, int, int]`: `(x, y, width, height)` at the analysis level
    - `level_scale: float`
    - `pixel_size_um: float`: 0 when unknown
    - `cancelled: bool`: reads the run's token. After `apply` returns it is `True`, since the token is detached.
  - Fixture `seam_slide(tmp_path) -> Path`: a 2200×400 RGB image, white `(255, 255, 255)`, with three dark `(20, 20, 20)` discs of radius 40 centred at `(300, 200)`, `(1024, 200)` and `(1700, 200)`. It is a 1024-pixel tiled TIFF `seam.svs` (built with `tests/support/tiled_tiff.py`), with `seam.png` beside it as the pixel reference: a PNG opens as one tile and `SlideRun` does not expose the tile size. It has three tiles, and the middle disc straddles the first seam.

- [ ] **Step 1: Write the failing tests** in `tests/test_operation_raster.py`. The raster operation `Grey` returns `tile.mean(axis=2, dtype=float32)`, has `halo = 16`, and writes raster `"grey"`, exported as `grey.ome.tif`.
  - `test_raster_matches_numpy`: read the exported TIFF with `slideio.open_slide(path, "OMETIFF")`, `get_scene(0).read_block()`. It equals the PNG's channel mean computed in NumPy, exactly, over the whole 2200×400. This proves the halo is cropped and aligned in every tile.
  - `test_tile_is_halo_extended_copy`: `apply` records `tile.shape`, `tile.dtype`, `tile.flags.c_contiguous` and `context.halo_rect`. For the middle tile (x 1024–2048), the shape is `(432, 1056, 3)`: the halo rect is not clipped to the level, and pixels beyond it read as zero. Also assert `tile.shape[:2] == (halo_rect[3], halo_rect[2])`, `dtype == uint8`, and `c_contiguous`.
  - `test_init_once_per_stage`: `__init__` increments a class-level counter. After one run over three tiles, the counter is 1.
  - `test_parameters_reach_init`: `parameters = {"scale": 1.0}`, stage `scale = 2.0`. `__init__` receives `{"scale": 2.0}`, without `version`.
  - `test_max_concurrency_one`: `max_concurrency = 1`. `apply` tracks concurrent calls under a `threading.Lock` and sleeps 0.05 s. The maximum seen is 1.
  - `test_apply_exception_chained`: `apply` raises `ValueError("boom")`. `SlideRun.run` raises `StageError` with `operation_name == "test.apply_exception_chained"`, `isinstance(e.__cause__, ValueError)` and `"boom" in str(e.__cause__)`. A second run of the same `SlideRun` with a fixed class carries no stale cause.
  - `test_init_exception_chained`: the same, for `__init__` raising `RuntimeError`.
  - `test_raster_wrong_shape_or_dtype_fails`: parametrised over a tile-sized array (no halo), a `float64` array, and a list. Each gives `StageError` whose message contains the operation name and both the expected and the actual shape or dtype.
  - `test_nondeterministic_flag_recorded`: `deterministic = False`. After the run, the workspace store's stage record contains `"nondeterministicOperations"`. Read the stage records table with `sqlite3`; look up the table and column names in the library's `src/storage` schema.
  - `test_origin_recorded`: the stage record's `operationOrigins` maps the operation to `"<test module>:<qualname>"`.
  - `test_keyboard_interrupt_during_python_apply`: on its first call `apply` calls `_thread.interrupt_main()`, then sleeps 0.2 s. `SlideRun.run` raises `KeyboardInterrupt` within 10 s, and no `.partial` file remains in `out`.
  - `test_exit_after_run_is_clean`: in a subprocess, run `Grey` on a small slide, then exit. Expect `returncode == 0`.
- [ ] **Step 2: Rebuild and run; expect failures.**
- [ ] **Step 3: Implement** decisions 4–9 for the raster stand-in, plus `TileContext`.
  - `outputType()` returns the snapshot's type; `outputPlaneCount()` returns `output_planes`.
  - The crop offset is `tileRectAtLevel() - haloRectAtLevel()`.
  - `workingSetBytesPerLevelPixel()` is `working_set_bytes_per_pixel`.
- [ ] **Step 4: Rebuild, reinstall, run the whole suite; expect it to pass.**
- [ ] **Step 5: Commit** `feat: raster Python operations run tile by tile`.

### Task 4 (bindings): label-image operations

**Files:**
- Modify: `src/python_operation.cpp`
- Create: `tests/test_operation_labels.py`

**Interfaces:**
- Consumes: Task 3's stand-in machinery (instance, GIL, semaphore, errors, `TileContext`).
- Produces: the `LABELS` stand-in.
  - It derives from `LabelImageOperation`, overrides `requiresUniformTypes()` to true, and implements `predictLabels(context, labels)`.
  - `predictLabels` calls `apply` and copies the returned `int32` array into `labels` (decision 6).
  - Objects, measurements, ownership and truncation counting are the base's.

- [ ] **Step 1: Write the failing tests.** The operation `Dark` returns `(tile.mean(axis=2) < 128).astype(numpy.int32)`, writes layer `"dark"`, and is exported as CSV `dark.csv`.
  - `test_labels_objects_whole`: with `halo = 96` on `seam_slide`, the stage writes 3 objects, `truncated_objects == 0`, and `dark.csv` has 3 rows. Each row's `area` is within 2% of π·40.5² (PIL draws the disc 81 px across). The middle disc is one object, not two.
  - `test_labels_small_halo_truncates`: with `halo = 8`, `truncated_objects >= 1` and fewer than 3 objects are written.
  - `test_labels_must_be_int32`: returning `int64` gives `StageError` naming the operation, `int32` and `int64`.
  - `test_labels_accepts_trailing_plane`: an `(H, W, 1)` `int32` return works like `(H, W)`.
  - `test_target_layer_parameter`: stage parameters `{"version": "1", "targetLayer": "dark"}` are accepted.
- [ ] **Step 2: Rebuild and run; expect failures.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Rebuild, reinstall, run the whole suite; expect it to pass.**
- [ ] **Step 5: Commit** `feat: label-image Python operations produce objects`.

### Task 5 (bindings): `requires`, CLI and documentation

**Files:**
- Modify:
  - `slideio_processor/run.py`: import `requires` (decision 10)
  - `README.md`: a "Python operations" section with the spec's `NucleiSegmenter` example, adapted to this API
  - `CLAUDE.md`: a short "Python operations" section on the GIL rules, decisions 4, 7 and 9, and where the code is
- Create:
  - `tests/test_operation_requires.py`
  - `tests/data/opmodule/sample_ops.py`: a module defining `Grey` as in Task 3, with `__version__ = "0.3"`

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Write the failing tests.**
  - `test_requires_imports_module`: with `tests/data/opmodule` on `sys.path` (via `monkeypatch.syspath_prepend`), a pipeline file with `requires: ["sample_ops"]` and a stage using the module's operation runs, although the test never imports `sample_ops` itself. The record's origin ends with `" 0.3"`.
  - `test_requires_missing_module`: `requires: ["no_such_module_xyz"]` raises `InvalidArgumentError` containing `no_such_module_xyz`, with `__cause__` an `ImportError`. The workspace directory does not exist afterwards.
  - `test_cli_runs_python_operation`: `slideio-processor run ...` in a subprocess, with `PYTHONPATH` set to `tests/data/opmodule`, exits 0 and writes `grey.ome.tif`.
  - `test_cli_missing_module_exit_1`: the same with a missing module exits 1, and stderr names the module.
- [ ] **Step 2: Run; expect failures.**
- [ ] **Step 3: Implement**, and write the README and CLAUDE.md sections. The README states that changing code without bumping `version` does not make a stage stale (spec §5.3). It also gives the cause-chaining limitation of decision 7.
- [ ] **Step 4: Rebuild, reinstall, run the whole suite; expect it to pass.** Also run `python scripts/check_pins.py`. It must still pass, because the nested SlideIO is unchanged.
- [ ] **Step 5: Commit** `feat: pipeline requires are imported before a run; docs`.
