"""A collection over a deep live structure must not recurse once per level.

GC0 marked reachable objects recursively (mark_reachable -> slot visitor ->
mark_reachable), so gc.collect() with a 50,000-deep linked list alive
overflowed the C stack (SIGSEGV on 82070e303). CPython's collector is
iterative. Automatic GC0 collections run while such structures are being
built, so the depth is now walked with an explicit worklist.

The tracing collectors walk gray lists and do not recurse, but on this
program GC1, GC2 and GC4 take more than a minute and GC3 about 30 seconds
(as on 82070e303; docs/status.md), so this test runs GC0.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.python.pcc1_gate import repo_root

PROGRAM = '''import gc


class N:
    def __init__(self, v):
        self.v = v
        self.next = None


def linked(depth):
    head = N(0)
    cur = head
    i = 1
    while i < depth:
        cur.next = N(i)
        cur = cur.next
        i = i + 1
    return head


def nested(depth):
    value = []
    i = 0
    while i < depth:
        value = [value]
        i = i + 1
    return value


def main():
    head = linked(50000)
    lists = nested(50000)
    print("live", gc.collect())
    length = 0
    cur = head
    while cur is not None:
        length = length + 1
        cur = cur.next
    print("length", length, len(lists))
    head = None
    lists = None
    cur = None
    print("dropped", gc.collect())


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
def test_gc0_collect_over_deep_live_structures_matches_cpython(tmp_path: Path) -> None:
    src = tmp_path / "deep.py"
    src.write_text(PROGRAM, encoding="utf-8")
    exe = tmp_path / "deep"
    build = subprocess.run(
        ["uv", "run", "pcc", "--backend", "self", "--python-libpython=off", str(src), "-o", str(exe)],
        cwd=repo_root(), text=True, capture_output=True, timeout=900, env=_environment(),
    )
    assert build.returncode == 0, build.stderr
    reference = subprocess.run([sys.executable, "-c", PROGRAM], text=True, capture_output=True, timeout=120)
    assert reference.returncode == 0, reference.stderr
    run = subprocess.run([str(exe)], text=True, capture_output=True, timeout=120, env=_environment("0"))
    assert run.returncode == 0, (run.returncode, run.stderr[-2000:])
    assert run.stdout == reference.stdout, (run.stdout, reference.stdout)
