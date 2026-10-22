from __future__ import annotations

import re
import subprocess
import textwrap

from pcc.frontends.python.pipeline import compile_python


def _source() -> str:
    return textwrap.dedent(
        """
        def pick(flag: bool):
            if flag:
                return "load"
            return 7

        def main():
            x = pick(True)
            y = pick(False)
            print(x == "load")
            print("store" != x)
            print(y == "load")

        main()
        """
    ).lstrip()


def test_dyn_str_compare_fastpath_preserves_semantics(tmp_path):
    src = tmp_path / "dyn_str_compare.py"
    src.write_text(_source(), encoding="utf-8")
    exe = tmp_path / "dyn_str_compare.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["True", "True", "False"]


def test_dyn_str_compare_fastpath_uses_type_guarded_str_eq(tmp_path):
    src = tmp_path / "dyn_str_compare.py"
    src.write_text(_source(), encoding="utf-8")
    ll = tmp_path / "dyn_str_compare.ll"

    compile_python(
        str(src),
        str(ll),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
        emit_llvm_only=True,
    )
    ir_text = ll.read_text(encoding="utf-8")
    # An exact str tag compares natively; anything else keeps the generic
    # (``__eq__``-aware) comparison behind the guard.
    assert "str.predicate.tag" in ir_text
    assert "@py_obj_type_tag" in ir_text
    assert re.search(r"str\.predicate\.eq[.\w]* = call [^\n]*@py_str_eq\(", ir_text)
    assert "obj.str.eq" not in ir_text
