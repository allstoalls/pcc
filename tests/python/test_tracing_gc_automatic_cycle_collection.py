"""GC1..GC4 reclaim cyclic garbage automatically, as tracing collectors do.

Their automatic work only marked: reclamation (the sweep) ran only inside an
explicit gc.collect(), so a loop dropping self-referencing __del__ objects
finalized none of them until that collect, and GC1/GC2's collector thread
began one full marking cycle after another (every allocation requested
one): 200,000 such objects took 85 s where gc.disable() takes 5 s.

As for GC0, automatic collection now runs where compiled code reaches an
application gate, with the world stopped, exactly as safe as an explicit
gc.collect(): GC1..GC3 schedule it from tracking growth, GC4's automatic
step sweeps once its mark completes, and the collector thread begins cycles
only past the allocation-debt threshold. gc.disable() and
gc.set_threshold(0) keep automatic collection off.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.python.pcc1_gate import repo_root
from tests.python.test_gc0_automatic_cycle_collection import PROGRAM as GC0_PROGRAM

# The GC0 program with fewer rounds per phase.
PROGRAM = GC0_PROGRAM.replace("rounds = 200000", "rounds = 50000")


def _environment(backend=None):
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env.pop("PCC_WITH_THREADS", None)
    if backend is not None:
        env["PCC_GC_BACKEND"] = backend
    return env


@pytest.mark.integration
def test_tracing_collectors_collect_cycles_automatically_unless_disabled(tmp_path: Path) -> None:
    assert "rounds = 50000" in PROGRAM
    src = tmp_path / "auto_cycles.py"
    src.write_text(PROGRAM, encoding="utf-8")
    exe = tmp_path / "auto_cycles"
    build = subprocess.run(
        ["uv", "run", "pcc", "--backend", "self", "--python-libpython=off", str(src), "-o", str(exe)],
        cwd=repo_root(), text=True, capture_output=True, timeout=900, env=_environment(),
    )
    assert build.returncode == 0, build.stderr
    reference = subprocess.run([sys.executable, "-c", PROGRAM], text=True, capture_output=True, timeout=300)
    assert reference.returncode == 0, reference.stderr
    assert reference.stdout == "enabled True True\ndisabled True True\nthreshold0 True True\n"
    for backend in ("1", "2", "3", "4"):
        run = subprocess.run([str(exe)], text=True, capture_output=True, timeout=120, env=_environment(backend))
        assert run.returncode == 0, (backend, run.returncode, run.stderr[-2000:])
        assert run.stdout == reference.stdout, (backend, run.stdout)
