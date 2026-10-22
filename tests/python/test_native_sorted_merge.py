"""sorted() stable-merge regressions (G-P2-GCPERF).

Both runtime tiers replaced O(n^2) insertion sort in ``py_obj_sorted``
with a bottom-up STABLE merge sort (codegen-worker profiles showed
``cmp_threeway`` as the hottest shared symbol via sorted symbol lists).
The sort moves elements borrowed between ``out`` and a scratch py_list,
so refcount balance and GC slot visibility (tracing/relocating
backends) are the regression surface, plus CPython-equal ordering and
stability. Both tiers are exercised: default mode links the pcc-Python
port, PCC_RUNTIME_CC=cc links the C runtime.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from textwrap import dedent


from pcc.frontends.python.pipeline import compile_python

_SOURCE = """
def main() -> int:
    xs = []
    i = 0
    while i < 500:
        xs.append("k_" + str((i * 7919) % 257) + "_" + str(i))
        i = i + 1
    s = sorted(xs)
    print(s[0], s[1], s[249], s[499])
    ys = [5, 1, 4, 1, 5, 9, 2, 6, 5, 3, 5]
    print(sorted(ys))
    zs = [(2, "b"), (1, "z"), (2, "a"), (1, "a")]
    print(sorted(zs))
    es = []
    print(sorted(es))
    one = [42]
    print(sorted(one))
    f = [2.5, 1, 3.5, 2, True]
    print(sorted(f))
    return 0


main()
"""

_EXPECTED = [
    "k_0_0 k_0_257 k_215_290 k_9_48",
    "[1, 1, 2, 3, 4, 5, 5, 5, 5, 6, 9]",
    "[(1, 'a'), (1, 'z'), (2, 'a'), (2, 'b')]",
    "[]",
    "[42]",
    "[1, True, 2, 2.5, 3.5]",
]


def _build(tmp_path: Path) -> Path:
    src = tmp_path / "sorted_probe.py"
    exe = tmp_path / "sorted_probe"
    src.write_text(dedent(_SOURCE), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
    )
    return exe


def test_sorted_merge_matches_cpython(tmp_path):
    # _EXPECTED is the captured CPython output of _SOURCE; the boolean
    # line also pins STABILITY (1 == True keeps input order 1, True).
    exe = _build(tmp_path)
    proc = subprocess.run(
        [str(exe)], text=True, capture_output=True, check=True, timeout=30
    )
    assert [l for l in proc.stdout.splitlines() if l] == _EXPECTED


def test_sorted_merge_gc_relocating_backends(tmp_path, monkeypatch):
    # The ping-pong move design must keep every element GC-visible in a
    # live list slot; backends 3 (generational forwarding) and 4
    # (colored relocating) are the slot-visibility-sensitive ones.
    exe = _build(tmp_path)
    for backend in ("3", "4"):
        proc = subprocess.run(
            [str(exe)],
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
            env={"PCC_GC_BACKEND": backend, "PATH": "/usr/bin:/bin"},
        )
        assert [l for l in proc.stdout.splitlines() if l] == _EXPECTED


_LEAK_SOURCE = """
count = 0


class T:
    def __init__(self, k: int):
        self.k = k

    def __del__(self):
        global count
        count = count + 1


def main() -> int:
    items = []
    i = 0
    while i < 64:
        items.append((63 - i, T(63 - i)))
        i = i + 1
    s = sorted(items)
    first = s[0]
    k0 = first[0]
    items = []
    s = []
    first = ()
    print(k0, count)
    return 0


main()
"""


def test_sorted_merge_releases_all_elements(tmp_path):
    # Refcount-balance regression for the merge sort: the ping-pong
    # reset and the C fill path MUST go through balanced slot stores
    # (pcc_gc_store_ptr increfs new / decrefs old). The original cut
    # reset via a bare length=0 (leaking one ref per element per pass)
    # and the C fill path never dropped its owned getitem ref —
    # detected by this __del__-count probe (count must reach 64 once
    # the lists are cleared). Tuple keys keep CPython comparability
    # (unique first elements never compare the T payloads).
    src = tmp_path / "sorted_leak_probe.py"
    exe = tmp_path / "sorted_leak_probe"
    src.write_text(dedent(_LEAK_SOURCE), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
    )
    proc = subprocess.run(
        [str(exe)], text=True, capture_output=True, check=True, timeout=30
    )
    assert proc.stdout.strip() == "0 64"
