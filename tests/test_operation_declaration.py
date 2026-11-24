import subprocess
import sys

import numpy
import pytest
from slideio_processor import (
    InvalidArgumentError,
    Operation,
    Output,
    PipelineFile,
    SlideRun,
    registered_operations,
)
from support.pipelines import one_stage_pipeline


def _mean(tile):
    return tile.mean(axis=2, dtype=numpy.float32)


def _run(blob_slide, tmp_path, name, parameters, sub="run"):
    text = one_stage_pipeline(name, parameters, writes_raster="r")
    (tmp_path / sub).mkdir(exist_ok=True)
    return SlideRun(PipelineFile.from_json(text), blob_slide, tmp_path / sub / "ws", tmp_path / sub / "out")


def test_subclass_registers():
    class Grey(Operation):
        name = "test.subclass_registers"
        version = "1"
        output = Output.RASTER

        def apply(self, tile, context):
            return _mean(tile)

    assert "test.subclass_registers" in registered_operations()


def test_abstract_intermediate_not_registered():
    before = registered_operations()

    class Base(Operation):
        version = "1"
        output = Output.RASTER

    assert registered_operations() == before

    class Concrete(Base):
        name = "test.abstract_intermediate_not_registered"

        def apply(self, tile, context):
            return _mean(tile)

    assert set(registered_operations()) - set(before) == {"test.abstract_intermediate_not_registered"}


def _apply(self, tile, context):
    return _mean(tile)


_DECLARATION_CASES = {
    "missing_version": ({"version": None}, "version"),
    "empty_name": ({"name": ""}, "name"),
    "output_str": ({"output": "labels"}, "output"),
    "negative_halo": ({"halo": -1}, "halo"),
    "zero_concurrency": ({"max_concurrency": 0}, "max_concurrency"),
    "reserved_parameter": ({"parameters": {"version": 1}}, "parameters"),
    "object_parameter": ({"parameters": {"x": object()}}, "parameters"),
    "complex_dtype": ({"output_dtype": "complex64"}, "output_dtype"),
    "zero_planes": ({"output_planes": 0}, "output_planes"),
    "no_apply": ({"apply": None}, "apply"),
}


@pytest.mark.parametrize("case", sorted(_DECLARATION_CASES))
def test_declaration_errors(case):
    changes, attribute = _DECLARATION_CASES[case]
    body = {"name": f"test.declaration_errors.{case}", "version": "1", "output": Output.RASTER, "apply": _apply}
    body.update(changes)
    body = {k: v for k, v in body.items() if v is not None}
    with pytest.raises(InvalidArgumentError) as info:
        type("BadDeclaration", (Operation,), body)
    assert "BadDeclaration" in str(info.value)
    assert attribute in str(info.value)
    assert f"test.declaration_errors.{case}" not in registered_operations()


def test_builtin_name_refused():
    with pytest.raises(InvalidArgumentError, match="segment.contours"):
        class Contours(Operation):
            name = "segment.contours"
            version = "1"
            output = Output.LABELS

            def apply(self, tile, context):
                return tile


def test_redefinition_replaces_class(blob_slide, tmp_path):
    calls = []

    class A(Operation):
        name = "test.redefinition_replaces_class"
        version = "1"
        output = Output.RASTER

        def apply(self, tile, context):
            calls.append("A")
            return _mean(tile)

    class B(Operation):
        name = "test.redefinition_replaces_class"
        version = "2"
        output = Output.RASTER

        def apply(self, tile, context):
            calls.append("B")
            return _mean(tile)

    assert registered_operations().count("test.redefinition_replaces_class") == 1
    _run(blob_slide, tmp_path, "test.redefinition_replaces_class", {"version": "2"}, "two").run()
    assert calls and set(calls) == {"B"}
    with pytest.raises(InvalidArgumentError):
        _run(blob_slide, tmp_path, "test.redefinition_replaces_class", {"version": "1"}, "one").run()


def test_redefinition_cannot_change_output():
    class A(Operation):
        name = "test.redefinition_cannot_change_output"
        version = "1"
        output = Output.RASTER

        def apply(self, tile, context):
            return _mean(tile)

    with pytest.raises(InvalidArgumentError, match="output"):
        class B(Operation):
            name = "test.redefinition_cannot_change_output"
            version = "1"
            output = Output.LABELS

            def apply(self, tile, context):
                return tile


def test_stage_parameters():
    class K(Operation):
        name = "test.stage_parameters"
        version = "1"
        output = Output.RASTER
        parameters = {"k": 0.5}

        def apply(self, tile, context):
            return _mean(tile)

    assert K.stage_parameters(k=2.0) == {"k": 2.0, "version": "1"}
    assert K.stage_parameters() == {"k": 0.5, "version": "1"}
    with pytest.raises(InvalidArgumentError, match="nope"):
        K.stage_parameters(nope=1)
    with pytest.raises(InvalidArgumentError, match="targetLayer"):
        K.stage_parameters(targetLayer="x")

    class L(Operation):
        name = "test.stage_parameters.labels"
        version = "3"
        output = Output.LABELS

        def apply(self, tile, context):
            return tile

    assert L.stage_parameters(targetLayer="dark") == {"targetLayer": "dark", "version": "3"}


def test_unknown_parameter_refused_before_run(blob_slide, tmp_path):
    class K(Operation):
        name = "test.unknown_parameter_refused_before_run"
        version = "1"
        output = Output.RASTER
        parameters = {"k": 0.5}

        def apply(self, tile, context):
            return _mean(tile)

    with pytest.raises(InvalidArgumentError, match="extra_key"):
        _run(blob_slide, tmp_path, K.name, {"version": "1", "k": 0.5, "extra_key": 1}).run()
    assert not (tmp_path / "run" / "ws").exists()


def test_parameter_type_refused_before_run(blob_slide, tmp_path):
    class K(Operation):
        name = "test.parameter_type_refused_before_run"
        version = "1"
        output = Output.RASTER
        parameters = {"k": 0.5}

        def apply(self, tile, context):
            return _mean(tile)

    with pytest.raises(InvalidArgumentError, match="'k'"):
        _run(blob_slide, tmp_path, K.name, {"version": "1", "k": "text"}, "text").run()
    assert not (tmp_path / "text" / "ws").exists()
    result = _run(blob_slide, tmp_path, K.name, {"version": "1", "k": 1}, "int").run()
    assert result.stages[0].ran


def test_version_mismatch_refused_before_run(blob_slide, tmp_path):
    class K(Operation):
        name = "test.version_mismatch_refused_before_run"
        version = "1"
        output = Output.RASTER

        def apply(self, tile, context):
            return _mean(tile)

    with pytest.raises(InvalidArgumentError) as info:
        _run(blob_slide, tmp_path, K.name, {"version": "0"}, "old").run()
    message = str(info.value)
    assert K.name in message and "'0'" in message and "'1'" in message
    assert not (tmp_path / "old" / "ws").exists()

    with pytest.raises(InvalidArgumentError, match="version"):
        _run(blob_slide, tmp_path, K.name, {}, "none").run()
    assert not (tmp_path / "none" / "ws").exists()


def test_exit_after_registration_is_clean(tmp_path):
    code = (
        "import slideio_processor as sp\n"
        "class Grey(sp.Operation):\n"
        "    name = 'test.exit_after_registration_is_clean'\n"
        "    version = '1'\n"
        "    output = sp.Output.RASTER\n"
        "    def apply(self, tile, context):\n"
        "        return tile\n"
        "assert 'test.exit_after_registration_is_clean' in sp.registered_operations()\n"
    )
    done = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""


def test_output_dtype_none_refused():
    with pytest.raises(InvalidArgumentError, match="output_dtype"):
        class NoDtype(Operation):
            name = "test.output_dtype_none_refused"
            version = "1"
            output = Output.RASTER
            output_dtype = None

            def apply(self, tile, context):
                return _mean(tile)

    assert "test.output_dtype_none_refused" not in registered_operations()


def test_stage_parameters_without_version_refused():
    class Base(Operation):
        output = Output.RASTER

    with pytest.raises(InvalidArgumentError, match="version"):
        Base.stage_parameters()
