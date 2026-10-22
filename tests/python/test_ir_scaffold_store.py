"""Phase 2 Task 7: scaffold lowering for ``builder.store``.

Verifies the first real Path A method — ``self.builder.store(v, p)``
in user source becomes ``call void @user_pcc_ir_ir_IRBuilder_store(i8*, i8*, i8*, i8*)``
in the emitted IR (no ``py_cpy_*`` dispatch). The final operand is the
explicit Python default for ``align``.

What's tested:
- ON mode IR contains the expected extern call.
- ON mode IR has zero ``py_cpy_*`` calls for the migrated function.
- OFF mode still produces the original ``py_cpy_*`` dispatch (regression
  check — Path A must not change OFF behaviour).
- The extern function is declared exactly once even with multiple
  store call sites in the same module.
"""
from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).absolute().parents[2]
_BUILD = _REPO_ROOT / "build"
_BUILD.mkdir(parents=True, exist_ok=True)


def _compile_to_ll(source: str, name: str, *, mode: str) -> str:
    """Compile ``source`` with the given ir_scaffold_mode and return
    the resulting IR text."""
    from pcc.frontends.python.pipeline import compile_python

    src = _BUILD / f"{name}.py"
    out = _BUILD / f"{name}.ll"
    src.write_text(source, encoding="utf-8")
    compile_python(
        str(src), str(out),
        emit_llvm_only=True,
        ir_scaffold_mode=mode,
    )
    return out.read_text(encoding="utf-8")


_USE_STORE_PROGRAM = textwrap.dedent(
    """
    from pcc.ir.compat import ir

    def use_store(val, ptr) -> None:
        builder = ir.IRBuilder()
        builder.store(val, ptr)
    """
)


# Modern LLVM emits opaque ``ptr`` rather than typed ``i8*``. Allow both.
_PTR = r"(?:ptr|i8\s*\*)"


def test_on_mode_emits_extern_call():
    ir_text = _compile_to_ll(_USE_STORE_PROGRAM, "store_on", mode="on")
    assert "@user_pcc_ir_ir_IRBuilder_store" in ir_text, (
        "ON mode must emit the scaffold extern call; got IR without it:\n"
        + ir_text
    )
    decl_pattern = re.compile(
        r"declare[^\n]+void\s+@user_pcc_ir_ir_IRBuilder_store\s*\(\s*"
        + _PTR + r"\s*,\s*" + _PTR + r"\s*,\s*"
        + _PTR + r"\s*,\s*" + _PTR + r"\s*\)"
    )
    assert decl_pattern.search(ir_text), (
        f"extern declaration must match `void ({_PTR}, {_PTR}, {_PTR}, {_PTR})`:\n"
        + ir_text
    )


def test_on_mode_use_store_body_has_no_py_cpy():
    """The user_*__use_store function body must call the extern and
    contain zero py_cpy_* — Path A's per-callsite contract for the
    ``store`` method."""
    ir_text = _compile_to_ll(_USE_STORE_PROGRAM, "store_on_clean", mode="on")
    body = _function_body(ir_text, "use_store")
    assert body is not None, (
        "could not locate use_store function body in IR:\n" + ir_text
    )
    assert "@user_pcc_ir_ir_IRBuilder_store" in body, (
        "use_store body must call the scaffold extern; got:\n" + body
    )
    assert "py_cpy_" not in body, (
        "ON mode use_store body must have ZERO py_cpy_*; got:\n" + body
    )


def test_off_mode_still_uses_py_cpy_for_store_callsite():
    """Historical guard. ``builder.store(...)`` in OFF mode no longer
    falls through to libpython dispatch — pcc's OFF-side native
    runtime + typed-int dispatch closed the gap so both modes now
    emit zero ``py_cpy_*`` for this idiom. The durable invariants
    remain: the ON-mode-only scaffold extern stays out of OFF-mode
    IR (so the two modes don't accidentally share scaffold
    references), and OFF mode is never *worse* than ON."""
    ir_text = _compile_to_ll(_USE_STORE_PROGRAM, "store_off", mode="off")
    assert "@user_pcc_ir_ir_IRBuilder_store" not in ir_text, (
        "OFF mode must NOT emit scaffold extern; got:\n" + ir_text
    )
    body = _function_body(ir_text, "use_store")
    assert body is not None, (
        "could not locate use_store function body in IR:\n" + ir_text
    )
    on_text = _compile_to_ll(
        _USE_STORE_PROGRAM, "store_off_cmp_on", mode="on",
    )
    on_body = _function_body(on_text, "use_store")
    assert on_body is not None, on_text
    off_cpy = body.count("py_cpy_")
    on_cpy = on_body.count("py_cpy_")
    assert off_cpy <= on_cpy, (
        f"OFF mode regressed on builder.store: off py_cpy_ count "
        f"{off_cpy} > on count {on_cpy}"
    )


def test_extern_declared_once_for_multiple_call_sites():
    """The extern declaration is module-level; multiple call sites
    must not duplicate it."""
    program = textwrap.dedent(
        """
        from pcc.ir.compat import ir

        def two_stores(v1, p1, v2, p2) -> None:
            builder = ir.IRBuilder()
            builder.store(v1, p1)
            builder.store(v2, p2)
        """
    )
    ir_text = _compile_to_ll(program, "store_dup", mode="on")
    decl_count = len(re.findall(
        r"declare[^\n]+void\s+@user_pcc_ir_ir_IRBuilder_store", ir_text,
    ))
    assert decl_count == 1, (
        f"expected exactly 1 extern decl, got {decl_count}:\n" + ir_text
    )
    call_count = len(re.findall(
        r"call[^\n]+void[^\n]+@user_pcc_ir_ir_IRBuilder_store", ir_text,
    ))
    assert call_count == 2, (
        f"expected 2 call sites, got {call_count}:\n" + ir_text
    )


def _function_body(ir_text: str, fn_name_suffix: str) -> str | None:
    """Return the body of a function whose mangled name ends with
    ``fn_name_suffix`` (e.g. ``use_store``). Mangling looks like
    ``user_<module>__<fn>``."""
    pattern = re.compile(
        r"define\s+[^\n]*?@[A-Za-z0-9_]*"
        + re.escape(fn_name_suffix)
        + r"\s*\([^)]*\)[^{]*\{(.+?)\n\}",
        re.DOTALL,
    )
    m = pattern.search(ir_text)
    if not m:
        return None
    return m.group(1)
