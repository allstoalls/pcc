"""Regression: a MODULE-level ``dict([...])`` must balance its temp GC root
on nested error edges.

Root cause (fixed 2026-08-15): ``_emit_dict_builtin`` entered the dict's
container temp root and then emitted the source expression bare
(``self._emit_expr(src_expr)``). A nested list literal's allocation error
check therefore branched to the OUTER cleanup block, which does not leave the
just-entered dict root. The self backend's precise stack-map analysis then
rejected the whole module with "managed root state disagrees at block join
'err.exit'" — this is what broke stage1 on
``pcc.frontends.python.codegen.layer1_support`` (its module-level
``dict([("name", export), ...])`` static tables). Function-local ``dict([...])``
did not reproduce; module top is the failing shape.

See docs/investigations/dict-builtin-module-top-stackmap-err-exit-join.md.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


def _run_pcc_program(tmp_path: Path, source: str, backend: str | None) -> str:
    src = tmp_path / "prog.py"
    src.write_text(source, encoding="utf-8")
    exe = tmp_path / ("prog_" + (backend or "default"))
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            "uv", "run", "pcc", "--backend", backend, "--python-libpython=off",
            "--ir-scaffold=on", str(src), "-o", str(exe),
        ],
        text=True, capture_output=True, timeout=420, env=env,
    )
    assert build.returncode == 0, build.stderr
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=30, env=env
    )
    assert run.returncode == 0, run.stderr
    return run.stdout


_PROG = (
    "TABLE = dict(\n"
    "    [\n"
    "        ('a', [1, 2]),\n"
    "        ('b', [3, 4]),\n"
    "    ]\n"
    ")\n"
    "def main() -> int:\n"
    "    row = TABLE['b']\n"
    "    print(row[1])\n"
    "    print(len(TABLE))\n"
    "    return 0\n"
    "main()\n"
)


@pytest.mark.parametrize("backend", [pytest.param(None, id="default-self"), pytest.param("self", id="explicit-self")])
def test_module_level_dict_builtin_list_source(tmp_path, backend):
    out = _run_pcc_program(tmp_path, _PROG, backend)
    assert out.splitlines() == ["4", "2"]
