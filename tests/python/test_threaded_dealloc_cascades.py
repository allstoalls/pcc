"""Deallocation cascades are per thread, as CPython's trashcan.

Four threads each drop 400-deep chains (``Node`` objects with ``__del__``
and nested lists) thirty times, so every thread runs past the inline depth
into its trash queue while the others do the same. One process-wide depth
and queue let a thread drain another's queue and lose depth updates: on
095b286f0 GC0 died with [BAD_INCREF] and GC0..3 lost finalizers in most runs.
The program's output must equal CPython's under every collector.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.python.pcc1_gate import repo_root

PROGRAM = '''import threading

counts = [0, 0, 0, 0]


class Node:
    def __init__(self, child, index):
        self.child = child
        self.index = index

    def __del__(self):
        counts[self.index] += 1


def churn(index, rounds, depth):
    for _ in range(rounds):
        node = None
        for _ in range(depth):
            node = Node(node, index)
        chain = [None]
        for _ in range(depth):
            chain = [chain]
        node = None
        chain = None


def main():
    threads = [threading.Thread(target=churn, args=(index, 30, 400)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    print(counts)


main()
'''


def _environment(backend=None):
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env.pop("PCC_WITH_THREADS", None)
    if backend is not None:
        env["PCC_GC_BACKEND"] = backend
    return env


def _compile(tmp_path: Path) -> Path:
    src = tmp_path / "cascades.py"
    src.write_text(PROGRAM, encoding="utf-8")
    exe = tmp_path / "cascades"
    build = subprocess.run(
        ["uv", "run", "pcc", "--backend", "self", "--python-libpython=off", str(src), "-o", str(exe)],
        cwd=repo_root(), text=True, capture_output=True, timeout=900, env=_environment(),
    )
    assert build.returncode == 0, build.stderr
    return exe


def _expected() -> str:
    run = subprocess.run([sys.executable, "-c", PROGRAM], text=True, capture_output=True, timeout=120)
    assert run.returncode == 0, run.stderr
    return run.stdout


def _run(exe: Path, backends) -> None:
    expected = _expected()
    for backend in backends:
        # Several runs: the old shared cascade state failed in most, not all.
        for _ in range(3):
            run = subprocess.run([str(exe)], text=True, capture_output=True, timeout=300, env=_environment(backend))
            assert run.returncode == 0, (backend, run.stderr[-2000:])
            assert run.stdout == expected, (backend, run.stdout, expected)


@pytest.mark.integration
def test_concurrent_trash_cascades_keep_every_finalizer(tmp_path: Path) -> None:
    _run(_compile(tmp_path), ("0", "1", "2", "3"))


@pytest.mark.integration
def test_concurrent_trash_cascades_keep_every_finalizer_under_gc4(tmp_path: Path) -> None:
    # On 6cc663bd4 GC4 lost a chain's finalizers, ran one twice or died with
    # [BAD_INCREF] in most runs: a zpage death probed its own address after
    # its bytes could already hold another thread's new object.
    _run(_compile(tmp_path), ("4",))
