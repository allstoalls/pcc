"""Unbound ``KnownClass.method(self)`` call regressions.

numpy ``distutils/fcompiler/gnu.py`` failed to compile with "too many
positional args: got 1, expected at most 0" on
``GnuFCompiler.get_flags(self)``: ``get_flags`` is inherited from the
CPython-backed ``FCompiler`` base, so the native MRO walk found nothing
and the lowering fell through to the "any class declaring the method"
name-scan fallback — which matched the SUBCLASS's same-named method and
treated the class object as a bound instance receiver. The fix routes
unresolvable ``KnownClass.method(...)`` calls through dynamic
getattr-on-the-class + call with all explicit args (CPython
unbound-call semantics).
"""
from __future__ import annotations

import subprocess
import re
from pathlib import Path
from textwrap import dedent

from pcc.py_frontend.pipeline import compile_python


def _build_and_run(tmp_path: Path, source: str) -> list[str]:
    src = tmp_path / "unbound_call_probe.py"
    exe = tmp_path / "unbound_call_probe"
    src.write_text(dedent(source), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
    )
    proc = subprocess.run(
        [str(exe)], text=True, capture_output=True, check=True, timeout=30
    )
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def test_unbound_call_cpy_base_compiles_via_dynamic_getattr(tmp_path):
    """gnu.py shape: the method lives only on a CPython-backed base.

    Must compile (library/auto mode, like the numpy per-module sweep)
    and must lower to a dynamic getattr on the class object — NOT to a
    direct call of the subclass's same-named method.
    """
    src = tmp_path / "mid_leaf.py"
    out = tmp_path / "mid_leaf.ll"
    src.write_text(
        dedent(
            """
            from argparse import ArgumentParser

            class Mid(ArgumentParser):
                pass

            class Leaf(Mid):
                def format_usage(self):
                    return Mid.format_usage(self)
            """
        ),
        encoding="utf-8",
    )
    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        libpython_mode="auto",
        ir_scaffold_mode="on",
        python_library=True,
    )
    ir_text = out.read_text(encoding="utf-8")
    assert "py_obj_getattr" in ir_text
    # the buggy fallback dispatched the subclass method directly with the
    # class object as receiver; pin that the unbound base reference
    # `Mid.format_usage(self)` lowers to a dynamic getattr on the class
    # object and is NOT emitted as a direct call of Leaf's own method in the
    # body. Count actual calls independently of the emitter's explicit
    # linkage spelling, then inspect the method body for recursive dispatch.
    symbol = "@user_mid_leaf_Leaf_format_usage"
    calls = re.findall(r"(?m)^\s*[^\n]*\bcall\b[^\n]*" + symbol + r"\(", ir_text)
    body = re.search(r"(?m)^define[^\n]*" + symbol + r"\([^\n]*\{\n([\s\S]*?)^\}", ir_text)
    assert body is not None
    assert len(calls) == 1, calls
    assert symbol + "(" not in body[1], body[1]


def test_unbound_call_missing_method_raises_attribute_error(tmp_path):
    """``A.nope(1)`` on a class without the method: AttributeError, like
    CPython — exercised end-to-end under strict no-libpython self-backend
    (the new dynamic path, not a compile-time arg-count error)."""
    out = _build_and_run(
        tmp_path,
        """
        class A:
            pass


        def main() -> int:
            try:
                A.nope(1)
                print("no-raise")
            except AttributeError:
                print("attr-error")
            return 0


        main()
        """,
    )
    assert out == ["attr-error"]


def test_unbound_call_native_mro_still_direct(tmp_path):
    """The natively-resolvable ``Base.method(self)`` path is unchanged."""
    out = _build_and_run(
        tmp_path,
        """
        class A:
            def get(self):
                return 1

        class B(A):
            def get(self):
                return A.get(self) + 1


        def main() -> int:
            b = B()
            print(b.get())
            return 0


        main()
        """,
    )
    assert out == ["2"]
