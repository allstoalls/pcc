from __future__ import annotations

import ast
import inspect
import re
import subprocess
import textwrap

from pcc.frontends.python.codegen.binary_op_lowering import BinaryOpLoweringMixin
from pcc.frontends.python.codegen.host_contract import L1_CODEGEN_HOST_METHODS
from pcc.frontends.python.pipeline import compile_python


def _source() -> str:
    return textwrap.dedent(
        """
        def pick(flag: bool):
            if flag:
                return 4
            return "a"

        def parse_int(token: str) -> int | None:
            return int(token, 0)

        def opaque(value):
            return value

        def main():
            x = pick(True)
            y = pick(True)
            print(x + y)
            print(y - x)
            print(x * y)
            s = pick(False)
            print(s + "b")
            wide = parse_int("2891786578161389964")
            width = opaque(8)
            if wide is not None:
                encoded = (wide & ((1 << (width * 8)) - 1)).to_bytes(
                    width, "little"
                )
                print(int.from_bytes(encoded, "little"))
            left = opaque({1, 2})
            right = opaque({2, 3})
            print(sorted(left & right))
            print(sorted(left | right))
            print(sorted(left ^ right))
            merged = opaque({"a": 1}) | opaque({"b": 2})
            print(merged["a"], merged["b"])

        main()
        """
    ).lstrip()


def test_dyn_tagged_int_binop_preserves_object_semantics(tmp_path):
    src = tmp_path / "dyn_tagged_binop.py"
    src.write_text(_source(), encoding="utf-8")
    exe = tmp_path / "dyn_tagged_binop.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)

    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == [
        "8",
        "0",
        "16",
        "ab",
        "2891786578161389964",
        "[2]",
        "[1, 2, 3]",
        "[1, 3]",
        "1 2",
    ]


def test_dyn_tagged_int_binop_emits_fast_path(tmp_path):
    src = tmp_path / "dyn_tagged_binop.py"
    src.write_text(_source(), encoding="utf-8")
    ll = tmp_path / "dyn_tagged_binop.ll"

    compile_python(
        str(src),
        str(ll),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
        emit_llvm_only=True,
    )
    ir_text = ll.read_text(encoding="utf-8")

    assert "int.tag.fast" in ir_text
    for callee in ("py_obj_add", "py_obj_and", "py_obj_or", "py_obj_xor"):
        assert re.search(
            r"call ptr(?: \(ptr, ptr\))? @" + callee + r"\(ptr [^,]+, ptr [^)]+\)",
            ir_text,
        ), callee
    assert "@py_obj_lshift" in ir_text


def _string_constants(fn) -> set[str]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def test_dyn_tagged_int_binop_has_one_wrapper_owner():
    helper = BinaryOpLoweringMixin._emit_dyn_tagged_int_object_binop
    helper_source = inspect.getsource(helper)
    # ``_emit_binop_value`` is the pin-deferring wrapper; the routing body
    # that owns the four DynType call sites is ``_emit_binop_value_routed``.
    caller = BinaryOpLoweringMixin._emit_binop_value_routed
    caller_source = inspect.getsource(caller)

    runtime_owners = {
        "py_obj_add",
        "py_obj_sub",
        "py_obj_mul",
        "py_obj_and",
        "py_obj_or",
        "py_obj_xor",
        "py_obj_lshift",
        "py_obj_rshift",
        "py_obj_pow",
    }
    assert runtime_owners <= _string_constants(helper)
    assert not (runtime_owners & _string_constants(caller))
    # +, -, *, the bitwise/shift group and ** (ae6ecaae4 gave ** its own).
    assert caller_source.count("self._emit_dyn_tagged_int_object_binop(") == 5
    assert helper_source.count("marshal.marshal_to_object(") == 2
    assert "self._emit_inline_tagged_int_binop_or_call(" in helper_source
    assert "self._emit_post_call_err_check(" in helper_source
    assert "_emit_dyn_tagged_int_object_binop" in L1_CODEGEN_HOST_METHODS
