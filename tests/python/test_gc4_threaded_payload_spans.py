"""GC4 container payload spans are registered under the object-graph lock.

A list, dict or set constructor registers its payload as a span of the
owner's zpage, which advances the page's bump offset (page+64). The
registration read and wrote that offset without the graph lock, while
another thread's zpage allocation moved it under the lock: the span and
the other thread's new object then claimed the same offset, and the span's
tail rewind later handed that live object's block to a third allocation.
Four threads that only build private containers died with [BAD_INCREF]
under GC4 in most runs on 6cc663bd4; every collector must print CPython's
output.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.python.pcc1_gate import repo_root

PROGRAM = '''import threading

totals = [0, 0, 0, 0]


def churn(index, rounds):
    total = 0
    for i in range(rounds):
        item = [i, i]
        table = {i: index}
        seen = {i, index}
        grown = []
        for value in range(6):
            grown.append(value)
        total += len(item) + len(table) + len(seen) + len(grown)
    totals[index] = total


def main():
    threads = [threading.Thread(target=churn, args=(index, 40000)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    print(totals)


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
def test_threads_building_private_containers_match_cpython_under_every_collector(tmp_path: Path) -> None:
    src = tmp_path / "spans.py"
    src.write_text(PROGRAM, encoding="utf-8")
    exe = tmp_path / "spans"
    build = subprocess.run(
        ["uv", "run", "pcc", "--backend", "self", "--python-libpython=off", str(src), "-o", str(exe)],
        cwd=repo_root(), text=True, capture_output=True, timeout=900, env=_environment(),
    )
    assert build.returncode == 0, build.stderr
    reference = subprocess.run([sys.executable, "-c", PROGRAM], text=True, capture_output=True, timeout=120)
    assert reference.returncode == 0, reference.stderr
    # GC4 runs several times: the unlocked registration failed in most runs, not all.
    for backend, runs in (("0", 1), ("1", 1), ("2", 1), ("3", 1), ("4", 4)):
        for _ in range(runs):
            run = subprocess.run([str(exe)], text=True, capture_output=True, timeout=300, env=_environment(backend))
            assert run.returncode == 0, (backend, run.returncode, run.stderr[-2000:])
            assert run.stdout == reference.stdout, (backend, run.stdout, reference.stdout)
