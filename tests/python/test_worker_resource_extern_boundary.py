"""The resource fixture's raw worker probes use the real, local C ABI.

This is an IR regression, not native qualification. The complete native driver
test remains responsible for invalid inputs and the unowned/reaped witnesses.
"""

import ast
from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parents[2]
DRIVER = ROOT / "tests/fixtures/native/worker_resource_admission.py"
POOL = ROOT / "pcc/frontends/python/worker_process_pool.py"


@pytest.fixture(scope="module")
def resource_driver_ir(tmp_path_factory):
    from pcc.frontends.python import pipeline
    from pcc.frontends.python.pipeline_context import (
        compile_contextual_per_module_fallback_counts,
    )

    module = pipeline._module_name_from_src(str(DRIVER))
    sources, modules, recursive = pipeline._prepare_single_source_compile_closure(
        str(DRIVER), module, emit_llvm_only=False, libpython_mode="off",
        ir_scaffold_mode="on", recursive_stdlib=False, python_library=False,
    )
    sources, modules = pipeline._prepare_multi_source_compile_closure(
        sources, modules, recursive_stdlib=recursive, ir_scaffold_mode="on",
    )
    output = tmp_path_factory.mktemp("resource-worker-abi")
    counts = compile_contextual_per_module_fallback_counts(
        sources, modules, [module], ir_scaffold_mode="on",
        strict_no_libpython=True, emit_ir_dir=str(output), entry_module=module,
    )
    assert counts == {module: 0}
    return (output / (module.replace(".", "_") + ".ll")).read_text()


@pytest.mark.parametrize("function, expected", (
    ("spawn_failure", {"start": 2, "poll": 1, "stop": 1}),
    ("raw_poll", {"poll": 1}),
    ("handles", {"start": 1}),
))
def test_complete_resource_probe_calls_raw_worker_abi(
    resource_driver_ir, function, expected,
):
    # Use the complete original functions and production import/export
    # context. A locally rewritten expression could hide this exact failure.
    match = re.search(
        r"^define[^\n]*@user_[^(\n]*_" + function
        + r"\([^\n]*\{\n(.*?)^\}",
        resource_driver_ir, re.MULTILINE | re.DOTALL,
    )
    assert match is not None, function
    body = match.group(1)
    assert "strict.nolib.stub" not in body
    assert "@py_cpy_" not in body
    for operation, count in expected.items():
        signature = "ptr, i64" if operation == "start" else "i64"
        argument = r"ptr [^,]+, i64 " if operation == "start" else r"i64 "
        calls = re.findall(
            r"\bcall i64(?: \(" + signature + r"\))? @pcc_worker_process_"
            + operation + r"\(" + argument, body,
        )
        assert len(calls) == count, (function, operation, len(calls), count)
    assert ".pyattr._native_worker_" not in body


def test_fixture_raw_worker_declarations_match_the_production_abi():
    def declarations(path):
        return {
            target.id: node.value
            for node in ast.parse(path.read_text()).body
            if isinstance(node, ast.Assign)
            for target in node.targets if isinstance(target, ast.Name)
        }

    fixture, production = declarations(DRIVER), declarations(POOL)
    for operation in ("start", "poll", "stop"):
        raw = fixture["_raw_worker_" + operation]
        native = production["_native_worker_" + operation]
        assert ast.dump(raw) == ast.dump(native), operation
