"""``print(*args, file=sys.stderr|sys.stdout)`` lowers natively to
``py_sys_X_write`` instead of falling back through CPython's print.

Restrictions: ``sep`` / ``end`` must be string literals when present
(non-literal forms still fall back). A bool-literal ``flush=`` is native for
the built-in stdout/stderr streams because their helpers write directly to
file descriptors; a dynamic flush expression still falls back.
"""
from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).absolute().parents[2]
_BUILD = _REPO_ROOT / "build"
_BUILD.mkdir(parents=True, exist_ok=True)


def _compile_to_ll(
    source: str,
    name: str,
    *,
    mode: str,
    libpython_mode: str = "off",
) -> str:
    from pcc.frontends.python.pipeline import compile_python

    src = _BUILD / f"{name}.py"
    out = _BUILD / f"{name}.ll"
    src.write_text(source, encoding="utf-8")
    compile_python(
        str(src), str(out),
        emit_llvm_only=True,
        ir_scaffold_mode=mode,
        libpython_mode=libpython_mode,
    )
    return out.read_text(encoding="utf-8")


def _function_body(ir_text: str, fn_name_suffix: str) -> str | None:
    pattern = re.compile(
        r"define\s+[^\n]*?@[A-Za-z0-9_]*"
        + re.escape(fn_name_suffix)
        + r"\s*\([^)]*\)[^{]*\{(.+?)\n\}",
        re.DOTALL,
    )
    m = pattern.search(ir_text)
    return m.group(1) if m else None


@pytest.mark.parametrize("mode", ["off", "on"])
def test_print_to_stderr_dispatches_natively(mode):
    program = textwrap.dedent(
        """
        import sys

        def f(msg: str) -> None:
            print(msg, file=sys.stderr)
        """
    )
    ir = _compile_to_ll(program, f"perr_{mode}", mode=mode)
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_sys_stderr_write" in body, body
    assert "cpy.builtin.print" not in body, body


@pytest.mark.parametrize("mode", ["off", "on"])
def test_print_to_stdout_dispatches_natively(mode):
    program = textwrap.dedent(
        """
        import sys

        def f(msg: str) -> None:
            print(msg, file=sys.stdout)
        """
    )
    ir = _compile_to_ll(program, f"pout_{mode}", mode=mode)
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_sys_stdout_write" in body, body
    assert "cpy.builtin.print" not in body, body


@pytest.mark.parametrize("mode", ["off", "on"])
def test_print_with_literal_flush_kwarg_dispatches_natively(mode):
    """Direct fd writes have no userspace buffer for literal flush to drain."""
    program = textwrap.dedent(
        """
        import sys

        def f(msg: str) -> None:
            print(msg, file=sys.stderr, flush=True)
        """
    )
    ir = _compile_to_ll(program, f"pflush_{mode}", mode=mode)
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_sys_stderr_write" in body, body
    assert "cpy.builtin.print" not in body, body


@pytest.mark.parametrize("libpython_mode", ["auto", "off"])
def test_print_with_dynamic_flush_kwarg_writes_through_the_stream(libpython_mode):
    # A non-literal flush= used to need CPython's print (and was stubbed out
    # under --python-libpython=off).  It now lowers natively: the stream is a
    # file object and py_print_to_file writes through it, calling flush()
    # when the flag is true.
    program = textwrap.dedent(
        """
        import sys

        def f(msg: str, should_flush: bool) -> None:
            print(msg, file=sys.stderr, flush=should_flush)
        """
    )
    ir = _compile_to_ll(
        program,
        f"pflush_dynamic_{libpython_mode}",
        mode="off",
        libpython_mode=libpython_mode,
    )
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_print_to_file" in body, body
    assert "cpy.builtin.print" not in body, body
    assert "strict.nolib.stub" not in body, body


@pytest.mark.parametrize("mode", ["off", "on"])
def test_sys_stream_flush_dispatches_natively(mode):
    program = textwrap.dedent(
        """
        import sys
        from sys import stderr, stdout

        def f() -> None:
            sys.stdout.flush()
            stdout.flush()
            stderr.flush()
        """
    )
    ir = _compile_to_ll(program, f"sflush_{mode}", mode=mode)
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_cpy_" not in body, body
