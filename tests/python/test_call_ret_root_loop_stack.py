"""Rooted-call hot loops must not grow the stack per iteration.

Regression for docs/investigations/
py-frontend-call-ret-root-alloca-loop-stack-overflow.md: `_call_user`
emitted the call.ret.root GC slot as a call-site alloca, so a loop body
containing `x = f(...)` re-executed the alloca every iteration and leaked
16 bytes of stack per pass — any rooted hot loop SIGSEGV'd on the stack
guard page after ~500K iterations (8MB / 16B). The slot now lives in the
entry block; 5M iterations must run to completion with correct results.
"""
from __future__ import annotations

import subprocess

import pytest


pytestmark = pytest.mark.xdist_group(name="pcc_heavy_llvm")


HOT_LOOP_SRC = '''
class Shared:
    def __init__(self, v: int) -> None:
        self.v = v


SHARED = Shared(7)


def touch(o: Shared) -> Shared:
    return o


def main() -> None:
    acc = 0
    i = 0
    while i < 5000000:
        s = touch(SHARED)
        acc = acc + s.v
        i = i + 1
    print("acc=" + str(acc))
    print("DONE")


if __name__ == "__main__":
    main()
'''


def test_rooted_call_loop_survives_5m_iterations(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "rooted_hot_loop.py"
    src.write_text(HOT_LOOP_SRC)
    exe = tmp_path / "rooted_hot_loop.out"
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, (
        f"hot loop crashed (rc={result.returncode}) — call.ret.root alloca "
        f"is growing the stack per iteration again\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    lines = [ln.strip() for ln in result.stdout.strip().splitlines()]
    assert "DONE" in lines, f"missing DONE:\n{result.stdout}"
    assert "acc=35000000" in lines, f"wrong result:\n{result.stdout}"
