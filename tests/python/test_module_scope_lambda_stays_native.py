"""A module-scope lambda is a pcc function, not a CPython ``operator`` object.

``lambda x: x.attr``, ``lambda x: x[0]`` and ``lambda x: x.method()`` were
lowered to ``operator.attrgetter`` / ``itemgetter`` / ``methodcaller`` by
importing CPython's ``operator`` module.  That is a reasonable shortcut when a
CPython callable is what the consumer wants, but it claimed the shape even
under ``--python-libpython=off``, where a native function object
(``_maybe_emit_native_lambda_func``) was already available and was simply
tried second.

``pcc/passes/base.py`` builds a module-scope dict of ten
``lambda pm: pm.add_<x>_pass()`` values.  Each became a
``py_cpy_import``/``py_cpy_getattr``/``py_cpy_from_pccstr``/``py_cpy_call1``
sequence in that module's top-level code, and module-level code is outside the
strict no-libpython stub projection -- so the self-host compile failed with
"module pcc.passes.base generated IR still calls py_cpy_* helpers".

The native path is now tried first whenever native callables are preferred.
"""

from __future__ import annotations

import re
import subprocess

import pytest


_SOURCE = '''
class Box:
    def __init__(self, value: int) -> None:
        self.value = value

    def doubled(self) -> int:
        return self.value * 2


_INVOKERS = {
    "doubled": lambda b: b.doubled(),
    "value": lambda b: b.value,
}
_FIRST = {"head": lambda xs: xs[0]}


def main() -> None:
    box = Box(21)
    print(_INVOKERS["doubled"](box))
    print(_INVOKERS["value"](box))
    print(_FIRST["head"]([7, 8, 9]))


main()
'''


def test_module_scope_lambdas_run_and_match_cpython(tmp_path):
    from pcc.py_frontend.pipeline import compile_python

    src = tmp_path / "module_lambdas.py"
    exe = tmp_path / "module_lambdas.out"
    src.write_text(_SOURCE, encoding="utf-8")
    compile_python(str(src), str(exe), libpython_mode="off", backend="self")
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=180, check=True
    )
    reference = subprocess.run(
        ["python3", str(src)], text=True, capture_output=True, timeout=180, check=True
    )
    assert run.stdout == reference.stdout
    assert run.stdout.splitlines() == ["42", "21", "7"]


def test_module_scope_lambdas_emit_no_cpython_operator(tmp_path):
    from pcc.py_frontend.pipeline import compile_python

    src = tmp_path / "module_lambdas.py"
    out = tmp_path / "module_lambdas.ll"
    src.write_text(_SOURCE, encoding="utf-8")
    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    text = out.read_text(encoding="utf-8")
    assert "cpy.operator_modname" not in text, "operator module still imported"
    assert re.search(r"\bcall [^\n]*@py_cpy_", text) is None, text[:2000]


def test_passes_base_lowers_with_no_fallbacks():
    """The closure module the shortcut blocked, compiled the way pcc1 does."""
    import os
    from pathlib import Path

    from pcc.parse.py_lift import parse_and_lift
    from pcc.py_frontend import type_infer
    from pcc.py_frontend.codegen.layer1 import L1CodeGen

    path = Path(__file__).resolve().parents[2] / "pcc/passes/base.py"
    typed = type_infer.infer_module(
        parse_and_lift(path.read_text(encoding="utf-8"), str(path), "pcc.passes.base")
    )
    codegen = L1CodeGen(typed, False, "on")
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    codegen._module_source_path = str(path)
    codegen._target_triple = ""
    ir_text = str(codegen.generate(typed))
    assert re.search(r"\bcall [^\n]*@py_cpy_", ir_text) is None
    assert "cpy.operator_modname" not in ir_text
    assert os.path.basename(str(path)) == "base.py"


@pytest.mark.parametrize("mode", ["auto", "on"])
def test_operator_shortcut_is_kept_when_cpython_callables_are_wanted(tmp_path, mode):
    """The shortcut still applies where a CPython callable is the point."""
    from pcc.py_frontend.pipeline import compile_python

    src = tmp_path / "sort_key.py"
    out = tmp_path / "sort_key.ll"
    src.write_text(
        '''
def ranked(rows: list) -> list:
    return sorted(rows, key=lambda row: row[1])
''',
        encoding="utf-8",
    )
    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        libpython_mode=mode,
        ir_scaffold_mode="on",
    )
    # Nothing is asserted about which lowering wins here -- only that the
    # non-strict modes still compile the shape at all, so the reordering did
    # not narrow them.
    assert out.read_text(encoding="utf-8").count("define ") > 0
