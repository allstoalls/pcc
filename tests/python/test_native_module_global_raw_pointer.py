"""Regression: a RAW pcc.unsafe pointer stored in a MODULE-level variable must
not be treated as a GC-managed object.

Root cause (fixed 2026-08-08): module_global_lowering stored any pointer-typed
module-global value via the GC pin/unpin/store_root path. ``pcc_gc_pin`` ORs
``PY_FLAG_GC_PINNED`` (0x40) into the object flags word at ``obj+12``. For a raw
``ptr_add``/``stack_alloc``/``calloc`` buffer that write lands in ordinary
memory: it OR'd 0x40 into the HIGH 32 bits of a stored 64-bit field, so ``2``
read back as ``0x4000000002``. Used as an index it produced a bit-42 wild
pointer and SIGSEGV under --backend self.

See docs/investigations/self-backend-large-frame-pointer-bit42-spill.md.
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
    "from pcc.unsafe import calloc, load_i64, store_i64, ptr_add\n"
    "BUF = calloc(256, 1)\n"
    "store_i64(BUF, 8, 2)\n"      # BUF[8] = 2; its high 32 bits sit at BUF+12
    "P = ptr_add(BUF, 0)\n"       # module-level RAW pointer -> must NOT be GC-pinned
    "C = calloc(128, 1)\n"        # module-level calloc pointer, same hazard
    "store_i64(C, 8, 7)\n"
    "def main():\n"
    "    print('P', load_i64(P, 8))\n"   # expect 2, not 0x4000000002 (=274877906946)
    "    print('C', load_i64(C, 8))\n"   # expect 7
    "main()\n"
)


@pytest.mark.parametrize("backend", [pytest.param(None, id="default-self"), pytest.param("self", id="explicit-self")])
def test_module_global_raw_pointer_not_pinned(tmp_path, backend):
    out = _run_pcc_program(tmp_path, _PROG, backend)
    lines = out.strip().splitlines()
    assert "P 2" in lines, out       # 274877906946 (0x40_00000002) => the pin bug
    assert "C 7" in lines, out
