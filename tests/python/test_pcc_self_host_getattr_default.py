"""Regression: pcc self-host must not constant-fold ``getattr(expr, "attr", default)``
to the default when ``expr`` is dyn-typed.

The 2026-05-11 pcc1 self-host failure traced to ``_call_ident(expr)`` inside
``compute_free_names`` (layer1.py):

    def _call_ident(expr):
        return getattr(expr, "ident", None)

pcc compiled this as "ignore expr, return None". Caller chain:
``_call_ident(call.func)`` always returned None → comprehension-sentinel
match at ``walk()`` never fired → genexpr target name was leaked as a
captured free var → codegen raised ``reference to unbound name h``.

LLDB evidence on pcc1 ``___nested__call_ident`` showed a body that loads a
fixed global and returns it, without touching x0 (the ``expr`` arg) or
calling ``py_obj_getattr``.

This test runs an end-to-end probe that fails only when the codegen bug
is present. It needs ``pcc1`` in repo root (built by
``scripts/bootstrap.py`` or by ``uv run pcc --backend self
--python-libpython=off --ir-scaffold=on pcc/__main__.py -o pcc1``).
"""
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest


REPO = Path(__file__).absolute().parents[2]
PCC1 = REPO / "pcc1"
pytestmark = pytest.mark.pcc_gate(
    unavailable=None
    if PCC1.exists() and os.access(PCC1, os.X_OK)
    else f"repo-root pcc1 artifact not present at {PCC1} (manual build target)"
)


def _require_pcc1():
    if not PCC1.exists() or not os.access(PCC1, os.X_OK):
        pytest.fail(
            f"pcc1 binary not available at {PCC1}; build via "
            f"`uv run pcc --backend self --python-libpython=off --ir-scaffold=on "
            f"pcc/__main__.py -o pcc1`"
        )


def _compile_with_pcc1(tmp_path: Path, source: str) -> Path:
    src = tmp_path / "probe.py"
    exe = tmp_path / "probe.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    proc = subprocess.run(
        [
            str(PCC1),
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(src),
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(REPO),
    )
    assert proc.returncode == 0, (
        f"pcc1 compile failed (rc={proc.returncode}):\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    return exe


def _run(exe: Path) -> list[str]:
    proc = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout.strip().splitlines()


def test_self_host_compiles_nested_genexpr_without_unbound_target(tmp_path):
    """Direct user-facing repro of the 2026-05-11 self-host failure."""
    _require_pcc1()
    exe = _compile_with_pcc1(
        tmp_path,
        """
        def outer(xs):
            offset = 1

            def inner():
                return tuple(h + offset for h in xs)

            return inner()

        vals = outer([1, 2, 3])
        print(vals[0], vals[1], vals[2])
        """,
    )
    assert _run(exe) == ["2 3 4"]


def test_self_host_getattr_default_returns_actual_attr_for_dyn_obj(tmp_path):
    """Minimal repro of the codegen bug.

    ``getattr(expr, "ident", None)`` on a dyn-typed argument that DOES have
    ``ident`` must return the attribute value, not the default. The 2026-05-11
    pcc1 bug constant-folded this to None.
    """
    _require_pcc1()
    exe = _compile_with_pcc1(
        tmp_path,
        """
        class Node:
            def __init__(self, ident):
                self.ident = ident

        def call_ident(expr):
            return getattr(expr, "ident", None)

        def passthrough(x):
            return x

        n = Node("hello")
        dyn = passthrough(n)
        result = call_ident(dyn)
        print("ok" if result == "hello" else "bug:" + repr(result))
        """,
    )
    assert _run(exe) == ["ok"]


def test_self_host_nested_def_with_getattr_default(tmp_path):
    """Closer match to the pcc1 ``compute_free_names`` shape: a nested ``def``
    whose body calls ``getattr(x, ATTR, None)`` against a dyn arg.
    """
    _require_pcc1()
    exe = _compile_with_pcc1(
        tmp_path,
        """
        class Name:
            def __init__(self, ident):
                self.ident = ident

        def outer():
            def _call_ident(expr):
                return getattr(expr, "ident", None)

            n = Name("xyz")
            return _call_ident(n)

        print(outer())
        """,
    )
    assert _run(exe) == ["xyz"]
