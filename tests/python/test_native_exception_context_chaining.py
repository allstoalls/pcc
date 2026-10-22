"""PEP 3134 implicit ``__context__`` chaining, no-libpython (both runtime tiers).

When a new exception is raised inside an ``except`` handler, CPython sets the
new exception's ``__context__`` to the exception being handled (implicit
chaining). pcc clears the TLS exception slot at handler entry
(``py_clear_exception``) and tracks the active handler exception only in the
frontend's ``_active_handler_excs`` stack, so the runtime ``py_raise``
auto-chain (which reads TLS) never fires. The raise-statement lowering
(``exception_lowering.py``) now sets ``__context__`` explicitly from the active
handler exception via ``py_exc_set_context``.

Cases covered:
  - new exception raised inside a handler -> __context__ is the handled exc
  - ``raise Y from Z`` still sets __context__ (CPython flips __suppress_context__
    only; __context__ is still populated)
  - ``raise e`` re-raising the SAME caught exception by name must NOT set
    __context__ to itself (self-cycle guard)
  - raising with no active handler leaves __context__ as None
"""
from __future__ import annotations

import inspect
import subprocess
import sys
import textwrap

import pytest

PROGRAM = textwrap.dedent("""
    def main() -> None:
        # 1. implicit chaining: a new exception raised inside a handler.
        try:
            try:
                raise ValueError("v")
            except ValueError:
                raise KeyError("k")
        except KeyError as k:
            print(type(k.__context__).__name__)
        # 2. explicit `raise ... from ...` still sets __context__.
        try:
            try:
                raise ValueError("v2")
            except ValueError:
                raise KeyError("k2") from RuntimeError("r")
        except KeyError as k2:
            print(type(k2.__context__).__name__)
            print(type(k2.__cause__).__name__)
        # 3. re-raising the SAME caught exception by name: __context__ must
        #    stay None (no self-reference).
        try:
            try:
                raise TypeError("t")
            except TypeError as e:
                raise e
        except TypeError as e2:
            print(e2.__context__ is e2)
        # 4. raising outside any active handler: __context__ is None.
        try:
            raise IndexError("i")
        except IndexError as e3:
            print(e3.__context__)

    if __name__ == "__main__":
        main()
    """).lstrip()


def test_handler_codegen_scopes_active_context_to_function_boundary():
    """A handled exception must not leak into a later unrelated raise.

    The host compiler's ``list.pop`` masked this bootstrap-only failure.  pcc1
    could miss that mutation and retain an IR value from the completed handler,
    eventually emitting it as a bare cross-function value token.
    """
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python import type_infer
    from pcc.frontends.python.codegen.class_gen import ClassLowering
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.codegen.user_function_lowering import (
        UserFunctionLoweringMixin,
    )

    for lowering in (
        UserFunctionLoweringMixin._emit_user_function,
        ClassLowering._emit_method_body,
    ):
        lowering_source = inspect.getsource(lowering)
        assert "saved_active_handler_excs" in lowering_source
        assert "_active_handler_excs = []" in lowering_source
        assert "_active_handler_excs = saved_active_handler_excs" in lowering_source

    exception_source = inspect.getsource(
        __import__(
            "pcc.frontends.python.codegen.exception_lowering",
            fromlist=["ExceptionLoweringMixin"],
        ).ExceptionLoweringMixin
    )
    assert 'getattr(self, "_active_handler_excs"' not in exception_source
    assert "active_excs.append((self.current_function, handler_exc))" in exception_source

    source = textwrap.dedent(
        """
        def probe() -> None:
            try:
                raise ValueError("inner")
            except ValueError:
                raise KeyError("wrapped")
            raise RuntimeError("outside")
        """
    ).lstrip()
    ast_mod = parse_and_lift(source, "<handler-stack-probe>", "handler_stack_probe")
    typed = type_infer.infer_module(ast_mod)
    ir_text = str(L1CodeGen(typed, ir_scaffold_mode="on").generate(typed))
    context_calls = [
        line
        for line in ir_text.splitlines()
        if "call " in line and "py_exc_set_context" in line
    ]
    assert len(context_calls) == 1


@pytest.mark.parametrize("runtime_cc", [None], ids=["port"])
def test_exception_context_chaining_matches_cpython(tmp_path, monkeypatch, runtime_cc):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "exc_ctx.py"
    exe = tmp_path / "exc_ctx.out"
    src.write_text(PROGRAM, encoding="utf-8")
    compile_python(
        str(src), str(exe),
        ir_scaffold_mode="on", libpython_mode="off", backend="self",
    )
    cpython = subprocess.run(
        [sys.executable, str(src)], capture_output=True, text=True, timeout=30,
    ).stdout
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == cpython
