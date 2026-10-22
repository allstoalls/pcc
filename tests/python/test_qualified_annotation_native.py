"""Execute defining-owner annotation identity across a real native package."""
from __future__ import annotations

import os
import subprocess

import pytest


RECORD = """class Payload:
    def __init__(self, value: int):
        self.value = value
    def read(self) -> int:
        return self.value
"""

PROVIDER = """from records import model as origin

def make(value: int) -> origin.Payload:
    return origin.Payload(value)
"""

CONSUMER = """from alternatives import model as origin
from records import model as actual
from provider import make

class Payload:
    def __init__(self, value: int):
        self.value = value

def relay(value: int) -> actual.Payload:
    return make(value)

def exercise() -> None:
    record = relay(37)
    local = Payload(13)
    other = origin.Payload(91)
    print("record", record.value, record.read())
    print("identity", isinstance(record, actual.Payload), isinstance(record, origin.Payload), isinstance(record, Payload))
    print("collisions", local.value, other.value)

exercise()
"""

WRONG_CONSUMER = """from records import model as actual
from alternatives import model as origin

def wrong() -> actual.Payload:
    return origin.Payload(37)
"""

EXPECTED = "record 37 37\nidentity True False False\ncollisions 13 91\n"
MODULES = ["records", "records.model", "alternatives", "alternatives.model", "provider", "consumer"]


def package_sources(directory, *, wrong=False):
    """Materialize the exact package used by both controls."""
    files = (
        ("records/__init__.py", ""),
        ("records/model.py", RECORD),
        ("alternatives/__init__.py", ""),
        ("alternatives/model.py", RECORD),
        ("provider.py", PROVIDER),
        ("consumer.py", WRONG_CONSUMER if wrong else CONSUMER),
    )
    paths = []
    for relative, source in files:
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
        paths.append(str(path))
    return paths


def test_provider_private_qualified_return_executes_natively(tmp_path, pcc_runtime_archive):
    from pcc.frontends.python.pipeline import compile_python_multi

    executable = tmp_path / "qualified_annotation"
    compile_python_multi(
        package_sources(tmp_path), str(executable), module_names=MODULES,
        entry_module="consumer", backend="self", libpython_mode="off",
        ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        result = subprocess.run(
            [str(executable)], cwd=tmp_path, capture_output=True, text=True,
            timeout=20, env=dict(os.environ, PCC_GC_BACKEND=str(backend), PATH=""),
        )
        (tmp_path / ("gc" + str(backend) + ".stdout")).write_text(result.stdout)
        (tmp_path / ("gc" + str(backend) + ".stderr")).write_text(result.stderr)
        assert result.returncode == 0, f"GC{backend}: {result.stdout}{result.stderr}"
        assert result.stderr == "", f"GC{backend}: {result.stderr}"
        assert result.stdout == EXPECTED, f"GC{backend}: {result.stdout}"


def test_wrong_class_qualified_return_is_rejected(tmp_path):
    from pcc.frontends.python.pipeline_context import build_closed_world_context
    from pcc.frontends.python.type_infer import infer_module
    from pcc.frontends.python.types import PyFrontendError

    parsed, exports, derived = build_closed_world_context(package_sources(tmp_path, wrong=True), MODULES)
    with pytest.raises(PyFrontendError, match="return type mismatch"):
        infer_module(
            parsed[-1],
            external_exports={name: info for name, info in exports.items() if name != "consumer"},
            derived_class_map=derived,
        )
