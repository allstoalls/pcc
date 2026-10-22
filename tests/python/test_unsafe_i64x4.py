from __future__ import annotations

import os
import subprocess
from pathlib import Path

from pcc1_gate import find_current_pcc1, skip_or_fail_no_current_pcc1
from pcc.backend.self_backend_dispatch import emit_self_asm
from pcc.frontends.python.pipeline import compile_python


REPO = Path(__file__).resolve().parents[2]
_I64X4_SOURCE = (
    "from pcc import i64\n"
    "from pcc.extern import c_abi_export\n"
    "from pcc.unsafe import load_i64x4, load_i64x4_strided, stack_alloc, store_i64\n"
    "__pcc_freestanding__ = True\n"
    "@c_abi_export('pcc_i64x4_sum')\n"
    "def pcc_i64x4_sum() -> i64:\n"
    "    raw = stack_alloc(32)\n"
    "    store_i64(raw, 0, 1)\n"
    "    store_i64(raw, 8, 2)\n"
    "    store_i64(raw, 16, 3)\n"
    "    store_i64(raw, 24, 4)\n"
    "    lanes = load_i64x4(raw, 0)\n"
    "    return lanes.first + lanes.second + lanes.third + lanes.fourth\n"
    "@c_abi_export('pcc_i64x4_stride_sum')\n"
    "def pcc_i64x4_stride_sum() -> i64:\n"
    "    raw = stack_alloc(64)\n"
    "    store_i64(raw, 0, 1)\n"
    "    store_i64(raw, 16, 2)\n"
    "    store_i64(raw, 32, 3)\n"
    "    store_i64(raw, 48, 4)\n"
    "    lanes = load_i64x4_strided(raw, 0, 16)\n"
    "    return lanes.first + lanes.second + lanes.third + lanes.fourth\n"
)


def _assert_i64x4_ir(ir_text: str) -> None:
    for symbol in ("pcc_i64x4_sum", "pcc_i64x4_stride_sum"):
        body = ir_text.split("define external i64 @" + symbol, 1)[1].split(
            "\n}\n", 1
        )[0]
        assert body.count("load i64") == 4
        assert "@load_i64x4" not in body
        assert "py_tuple" not in body
        assert "py_valuebox" not in body
        assert body.count("extractvalue { i64, i64, i64, i64 }") == 4


def test_i64x4_intrinsic_type_survives_closed_world_stub_exports() -> None:
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.py_ast import Assign, FuncDef, ValueClassType
    from pcc.frontends.python.type_infer import infer_module

    source = (
        "from pcc.unsafe import load_i64x4\n"
        "def probe(raw):\n"
        "    lanes = load_i64x4(raw, 0)\n"
        "    return lanes.first\n"
    )
    external_exports = {
        "pcc.unsafe": {
            "load_i64x4": {
                "kind": "function",
                "param_types": (("dyn",), ("int",)),
                "return_ty": ("dyn",),
            }
        }
    }
    typed = infer_module(
        parse_and_lift(source, "<i64x4-closed-world>", "i64x4_closed_world"),
        external_exports=external_exports,
    )
    function = next(stmt for stmt in typed.body if isinstance(stmt, FuncDef))
    assignment = function.body[0]
    assert isinstance(assignment, Assign)
    assert isinstance(assignment.value.ty, ValueClassType)
    assert assignment.value.ty.module == "pcc.unsafe"
    assert tuple(name for name, _field_ty in assignment.value.ty.fields) == (
        "first",
        "second",
        "third",
        "fourth",
    )


def test_unsafe_load_i64x4_is_an_unboxed_callsite_aggregate(tmp_path: Path) -> None:
    source = tmp_path / "i64x4.py"
    llvm_ir = tmp_path / "i64x4.ll"
    assembly = tmp_path / "i64x4.s"
    obj = tmp_path / "i64x4.o"
    harness = tmp_path / "i64x4_harness.c"
    executable = tmp_path / "i64x4_harness"
    source.write_text(_I64X4_SOURCE, encoding="utf-8")

    compile_python(
        str(source),
        str(llvm_ir),
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
        target_triple="arm64-apple-darwin23.6.0",
        python_library=True,
        recursive_stdlib=False,
    )
    ir_text = llvm_ir.read_text(encoding="utf-8")
    _assert_i64x4_ir(ir_text)

    assembly.write_text(emit_self_asm(ir_text), encoding="utf-8")
    assembled = subprocess.run(
        ["clang", "-c", str(assembly), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert assembled.returncode == 0, assembled.stdout + assembled.stderr
    harness.write_text(
        "long pcc_i64x4_sum(void);\n"
        "long pcc_i64x4_stride_sum(void);\n"
        "int main(void) { return pcc_i64x4_sum() == 10 && "
        "pcc_i64x4_stride_sum() == 10 ? 0 : 1; }\n",
        encoding="utf-8",
    )
    linked = subprocess.run(
        ["clang", str(harness), str(obj), "-o", str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert linked.returncode == 0, linked.stdout + linked.stderr
    ran = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr


def test_current_pcc1_emits_i64x4_aggregate_without_libpython(
    tmp_path: Path,
) -> None:
    pcc1 = find_current_pcc1(REPO)
    if pcc1 is None:
        skip_or_fail_no_current_pcc1(
            "no current pcc1 binary for load_i64x4 aggregate regression"
        )
    source = tmp_path / "i64x4_pcc1.py"
    llvm_ir = tmp_path / "i64x4_pcc1.ll"
    source.write_text(_I64X4_SOURCE, encoding="utf-8")
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    result = subprocess.run(
        [
            str(pcc1),
            "--python-library",
            "--python-libpython=off",
            "--ir-scaffold=on",
            "--backend",
            "self",
            "--emit-llvm=" + str(llvm_ir),
            str(source),
        ],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    _assert_i64x4_ir(llvm_ir.read_text(encoding="utf-8"))
