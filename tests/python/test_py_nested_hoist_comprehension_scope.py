"""Regression: comprehensions inside a nested ``def`` must not leak their
target names into the enclosing-function free-var set.

When the nested-def hoister mishandles comprehension scope, it captures the
comprehension target (``x``, ``h``, ...) as a free variable, then synthesizes
a wrapper that tries to bind that name from the outer scope — which fails at
codegen time with ``reference to unbound name <target>``.

Reproducer for the 2026-05-11 pcc1 self-host failure on
``def outer(xs): def inner(...): tuple(x + ... for x in xs)`` and friends.
"""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path


def _compile_and_run(tmp_path: Path, source: str) -> list[str]:
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "probe.py"
    exe = tmp_path / "probe.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    proc = subprocess.run([str(exe)], text=True, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout.strip().splitlines()


def test_nested_tuple_genexpr_target_is_not_captured(tmp_path):
    lines = _compile_and_run(
        tmp_path,
        """
        def outer(xs):
            offset = 10

            def inner():
                return tuple(x + offset for x in xs)

            return inner()

        vals = outer([1, 2, 3])
        print(vals[0], vals[1], vals[2])
        """,
    )
    assert lines == ["11 12 13"]


def test_nested_all_any_sum_genexpr_targets_are_not_captured(tmp_path):
    lines = _compile_and_run(
        tmp_path,
        """
        def outer(xs):
            threshold = 0

            def inner():
                print(all(x > threshold for x in xs))
                print(any(x == 2 for x in xs))
                print(sum(x for x in xs))

            inner()

        outer([1, 2, 3])
        """,
    )
    assert lines == ["True", "True", "6"]


def test_nested_list_dict_set_comp_targets_are_not_captured(tmp_path):
    lines = _compile_and_run(
        tmp_path,
        """
        def outer(xs):
            scale = 2

            def inner():
                ys = [x * scale for x in xs]
                d = {x: x * scale for x in xs}
                s = {x % 2 for x in xs}
                print(ys[0], ys[1], ys[2])
                print(d[3])
                print(len(s))

            inner()

        outer([1, 2, 3])
        """,
    )
    assert lines == ["2 4 6", "6", "2"]
