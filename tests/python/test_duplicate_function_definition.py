"""Repeated ``def`` statements keep distinct bodies and live bindings."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest


_SOURCE = """\
def choose(value: int = 1) -> int:
    return value

saved = choose
print(saved())

def choose(value: int = 2) -> int:
    return value + 10

print(saved(), choose())
"""

_CONTROL_FLOW_SOURCE = """\
if True:
    def choose(value: int = 3) -> int:
        return value
else:
    def choose(value: int = 30) -> int:
        return value

saved = choose
print(saved())

if False:
    def choose(value: int = 40) -> int:
        return value
else:
    def choose(value: int = 4) -> int:
        return value + 20

print(saved(), choose())
"""


def _defined_function_names(ir_text: str) -> list[str]:
    names: list[str] = []
    for match in re.finditer(
        r'^define\b[^\n]*@(?:"([^"]+)"|([^\s(]+))',
        ir_text,
        flags=re.MULTILINE,
    ):
        names.append(match.group(1) or match.group(2))
    return names


def test_duplicate_function_definitions_emit_distinct_verified_bodies(
    tmp_path: Path,
) -> None:
    from tests.owned_ir_validation import verify_ir_text

    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / "duplicate_definition_probe.py"
    output = tmp_path / "duplicate_definition_probe.ll"
    source.write_text(_SOURCE, encoding="utf-8")

    compile_python(
        str(source),
        str(output),
        emit_llvm_only=True,
        libpython_mode="off",
    )
    ir_text = output.read_text(encoding="utf-8")
    verify_ir_text(ir_text)

    choose_bodies = [
        name
        for name in _defined_function_names(ir_text)
        if re.search(r"_choose\.definition\.\d+$", name)
    ]
    assert choose_bodies == [
        "user_duplicate_definition_probe_choose.definition.0",
        "user_duplicate_definition_probe_choose.definition.1",
    ]
    choose_adapters = [
        name
        for name in _defined_function_names(ir_text)
        if re.search(r"_choose\.definition\.\d+_native_adapter$", name)
    ]
    assert choose_adapters == [
        "user_duplicate_definition_probe_choose.definition.0_native_adapter",
        "user_duplicate_definition_probe_choose.definition.1_native_adapter",
    ]
    for ordinal in (0, 1):
        assert (
            "__pcc_native_func_value_cache_duplicate_definition_probe_choose."
            f"user_duplicate_definition_probe_choose.definition.{ordinal}"
        ) in ir_text


@pytest.mark.parametrize("backend", [pytest.param(None, id="default-self"), pytest.param("self", id="explicit-self")])
def test_duplicate_function_definition_rebind_preserves_escaped_callable(
    tmp_path: Path,
    monkeypatch,
    pcc_runtime_archive,
    backend: str,
) -> None:
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / f"duplicate_definition_{backend}.py"
    output = tmp_path / f"duplicate_definition_{backend}.out"
    source.write_text(_SOURCE, encoding="utf-8")
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(pcc_runtime_archive))

    compile_python(
        str(source),
        str(output),
        backend=backend,
        libpython_mode="off",
    )
    result = subprocess.run(
        [str(output)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "1\n1 12\n"


@pytest.mark.parametrize("backend", [pytest.param(None, id="default-self"), pytest.param("self", id="explicit-self")])
def test_control_flow_duplicate_definition_rebinds_only_on_executed_path(
    tmp_path: Path,
    monkeypatch,
    pcc_runtime_archive,
    backend: str,
) -> None:
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / f"control_flow_duplicate_definition_{backend}.py"
    output = tmp_path / f"control_flow_duplicate_definition_{backend}.out"
    source.write_text(_CONTROL_FLOW_SOURCE, encoding="utf-8")
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(pcc_runtime_archive))

    compile_python(
        str(source),
        str(output),
        backend=backend,
        libpython_mode="off",
    )
    result = subprocess.run(
        [str(output)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "3\n3 24\n"


def test_duplicate_definition_codegen_state_is_in_closed_world_host_contract() -> None:
    from pcc.frontends.python.codegen.host_contract import L1_CODEGEN_HOST_ATTRS

    assert {
        "_duplicate_module_function_names",
        "_funcdef_functions",
        "_function_definition_ordinals",
        "_module_block_funcdef_ids",
        "_native_symbol_funcdefs",
    } <= set(L1_CODEGEN_HOST_ATTRS)
