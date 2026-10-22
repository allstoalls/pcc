"""GC0 collects cyclic garbage automatically, as CPython's collector does.

GC0 (refcounting plus trial-deletion cycle collection, the default backend)
collected cycles only on an explicit gc.collect(): a loop that drops 200,000
self-referencing objects with __del__ finalized none of them before that
collect, and with 2,000,000 the process grew to 2.2 GB where CPython stays
near 18 MB. Like CPython 3.12+, an allocation past the threshold only
schedules the collection; the next application gate of compiled code runs
it. gc.disable() and gc.set_threshold(0) keep it off.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.python.pcc1_gate import repo_root

PROGRAM = '''import gc
import sys

finalized = [0]


class Node:
    def __del__(self):
        finalized[0] += 1


def churn(rounds):
    for _ in range(rounds):
        node = Node()
        node.self = node
        node = None


def phase(name, rounds):
    gc.collect()
    start = finalized[0]
    churn(rounds)
    automatic = finalized[0] - start
    gc.collect()
    total = finalized[0] - start
    return name, automatic, total


def main():
    rounds = 200000
    results = [phase("enabled", rounds)]
    gc.disable()
    results.append(phase("disabled", rounds))
    gc.enable()
    threshold = gc.get_threshold()
    gc.set_threshold(0)
    results.append(phase("threshold0", rounds))
    gc.set_threshold(threshold[0], threshold[1], threshold[2])
    for name, automatic, total in results:
        # Automatic collection reclaims most cycles before the explicit
        # collect when enabled, none when disabled; every cycle is finalized.
        if name == "enabled":
            print(name, automatic > rounds // 2, total == rounds)
        else:
            print(name, automatic == 0, total == rounds)


main()
'''


def _environment(backend=None):
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env.pop("PCC_WITH_THREADS", None)
    if backend is not None:
        env["PCC_GC_BACKEND"] = backend
    return env


@pytest.mark.integration
def test_gc0_collects_cycles_automatically_unless_disabled(tmp_path: Path) -> None:
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
    run = subprocess.run([str(exe)], text=True, capture_output=True, timeout=300, env=_environment("0"))
    assert run.returncode == 0, run.stderr[-2000:]
    assert run.stdout == reference.stdout
