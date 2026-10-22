"""Indexed record IDs and legacy tuples share an honest payload interface."""
import ast
import os
from pathlib import Path
import re
import subprocess

import pytest

from pcc.backend.owned_object_emit import emit_owned_object
from pcc.frontends.python import type_infer
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.py_lift import parse_and_lift


ROOT = Path(__file__).resolve().parents[2]
RECEIVERS = (
    ("self_backend_aarch64_darwin.py", "_emit_indexed_instruction_core"),
    ("self_backend_aarch64_darwin.py", "_emit_indexed_instruction_parts"),
    ("self_backend_aarch64_darwin.py", "_emit_dense_indexed_instruction_parts"),
    ("self_backend_aarch64_darwin_memory.py", "emit_memory_instruction_by_id"),
    ("self_backend_aarch64_darwin_compute.py", "emit_compute_instruction_by_id"),
)


PAYLOAD_PROGRAM = '''def classify(data: int | tuple) -> int:
    if isinstance(data, int):
        return data + 1
    return data[0] + 2
def forward(data: int | tuple) -> int:
    return classify(data)
def main():
    assert forward(41) == 42
    assert forward((40,)) == 42
    print("INDEXED_PAYLOAD_CONTRACT_OK")
main()
'''


def _parameter_annotation(filename, function_name):
    tree = ast.parse((ROOT / "pcc/backend" / filename).read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == function_name)
    parameter = next(parameter for parameter in function.args.args if parameter.arg == "data")
    return ast.unparse(parameter.annotation)


def _generate(source, target=None):
    module = type_infer.infer_module(parse_and_lift(source, "<payload>", "payload"))
    codegen = L1CodeGen(module, ir_scaffold_mode="on")
    if target is not None:
        codegen._target_triple = target
        codegen.module.triple = target
    return str(codegen.generate(module))


@pytest.mark.parametrize("filename,function_name", RECEIVERS)
def test_actual_receiver_annotation_preserves_integer_id_branch(filename, function_name):
    annotation = _parameter_annotation(filename, function_name)
    text = _generate("def classify(data: " + annotation + ") -> bool:\n    return isinstance(data, int)\n")
    # The actual consumer's data annotation must admit the record-ID branch;
    # a tuple-only declaration previously emitted a constant False here.
    body = re.search(r"^define[^\n]*@user_payload_classify\([^\n]*\)[^\n]*\{\n(.*?)^\}", text, re.M | re.S).group(1)
    assert "@py_obj_type_tag(" in body, (filename, function_name, annotation, body)


def test_legacy_text_payload_interfaces_remain_tuple_only():
    for filename, function_name in (
        ("self_backend_aarch64_darwin.py", "_emit_instruction_parts"),
        ("self_backend_aarch64_darwin_memory.py", "emit_memory_instruction"),
        ("self_backend_aarch64_darwin_compute.py", "emit_compute_instruction"),
    ):
        assert _parameter_annotation(filename, function_name) == "tuple"


@pytest.mark.parametrize("target", ("arm64-apple-darwin", "x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu", "x86_64-pc-windows-msvc"))
def test_both_payload_forms_reach_the_owned_emitter(target):
    text = _generate(PAYLOAD_PROGRAM, target=target)
    assert "@py_obj_type_tag(" in text
    assert len(emit_owned_object(text, target)) > 0


@pytest.mark.integration
def test_integer_and_tuple_payload_calls_execute_all_collectors(tmp_path, monkeypatch, pcc_runtime_archive, python_program_compiler):
    source = tmp_path / "payload.py"
    source.write_text(PAYLOAD_PROGRAM)
    output = tmp_path / "payload"
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    python_program_compiler(str(source), str(output), backend="self", libpython_mode="off", ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend), PCC_GC_REFCOUNT_PROVENANCE_PROBE="2", PATH="/nonexistent"))
        assert result.returncode == 0 and result.stdout == "INDEXED_PAYLOAD_CONTRACT_OK\n" and result.stderr == "", (backend, result.returncode, result.stdout, result.stderr)
