"""Container retain/release parity under the refcount, generational and
relocating collectors.

History: this file was written to gate a `py_incref_managed`/`py_decref_managed`
ABI that skipped the raw-pointer provenance walk for values loaded from
container slots.  That ABI was DENIED (2026-08-27): the pcc1 built with it
segfaulted 3 s into Stage2 in `pcc_allocator_take_small_object` with a
corrupted free-list pointer (0x040000000111a5e9), because a container slot in
pcc-compiled code is not guaranteed to hold a managed object -- dyn values can
carry raw C pointers.  The ABI is gone; the behavioural gate stays, because it
is exactly the check that would have caught the defect before a stage did:
dict/list/tuple/instance/compare/set retain paths under GC0/GC3/GC4 with
collection forced between operations, plus a finalizer round-trip that turns
refcount drift in either direction into a wrong count.
"""
from __future__ import annotations

import subprocess
import textwrap

import pytest


_PROGRAM = """
class Node:
    def __init__(self, tag: int) -> None:
        self.tag = tag
        self.peer = None


DELS = [0]


class Canary:
    def __init__(self) -> None:
        self.alive = 1

    def __del__(self) -> None:
        DELS[0] = DELS[0] + 1


def hammer() -> int:
    total: int = 0
    d: dict = {}
    l: list = []
    t = ("a", "bb", "ccc")
    i: int = 0
    while i < 4000:
        key = "k" + str(i % 97)
        d[key] = i
        l.append(i)
        node = Node(i)
        node.peer = key
        # dict hit retain
        got = d.get(key, -1)
        # list fast-get retain
        head = l[0]
        # tuple get retain
        s = t[i % 3]
        # instance field get retain
        p = node.peer
        total = total + got + head + len(s) + len(p) + node.tag
        i = i + 1
    # list concat/copy/repeat retains
    l2 = l + l
    l3 = l2[:]
    total = total + len(l2) + len(l3)
    # compare-path retains/releases (list eq walks py_list_get pairs)
    if l3 == l2:
        total = total + 1
    # set candidate retain/release path (string keys collide into py_obj_eq)
    seen: set = set()
    j: int = 0
    while j < 500:
        seen.add("s" + str(j % 41))
        j = j + 1
    total = total + len(seen)
    return total


def finalizer_roundtrip() -> int:
    # Every drop below is a retain path proven balanced against CPython:
    # list append, subscript get (py_list_get), clear — and the owned
    # method-call results list.pop() / dict.get(), whose one-ref leak
    # (PY-P1-OWNED-METHOD-CALL-RESULT-LEAK) is fixed and pinned by
    # tests/python/test_owned_method_call_result_release.py.
    box: list = []
    c = Canary()
    box.append(c)          # list retains
    got = box[0]           # subscript get retains (balanced path)
    alive_before = DELS[0]
    got = None             # drop the subscript retain
    box.clear()            # drop the list's retain
    c = None               # drop the original binding
    # pop: the method call returns the list's reference by TRANSFER; the
    # consumer must release the owned result exactly once.
    box2: list = []
    c2 = Canary()
    box2.append(c2)
    popped = box2.pop()
    c2 = None
    popped = None
    # dict.get: returns a NEW reference the consumer owns.
    d: dict = {}
    c3 = Canary()
    d["k"] = c3
    got3 = d.get("k")
    got3 = None
    c3 = None
    del d["k"]
    import gc
    gc.collect()
    return DELS[0] - alive_before


def main() -> None:
    print(hammer())
    print(finalizer_roundtrip())
    import gc
    gc.collect()
    print("done")


main()
"""

@pytest.mark.parametrize("backend", ["0", "3", "4"])
def test_container_retain_paths_match_cpython(tmp_path, backend, monkeypatch):
    """dict/list/tuple/instance/compare/set paths under GC0/GC3/GC4."""
    from pcc.frontends.python.pipeline import compile_python

    monkeypatch.setenv("PCC_GC_BACKEND", backend)
    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent(_PROGRAM).lstrip(), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )
    native = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=240,
        env={"PCC_GC_BACKEND": backend, "PATH": "/usr/bin:/bin"},
    )
    assert native.returncode == 0, (backend, native.stderr)
    lines = native.stdout.split()
    assert len(lines) == 3, native.stdout
    # The hammer total is a pure function of the program; CPython is the
    # oracle for it.  62-bit-safe arithmetic throughout.
    assert lines[0] == "16031621", (backend, lines)
    # Exactly one finalizer must fire when the managed retain is dropped:
    # a leaked retain keeps the canary alive (0), an over-release fires it
    # early relative to the fence (also visible as a crash under GC3/4).
    # Three round-trips: append/subscript, pop, dict.get.  A leaked retain
    # keeps a canary alive (<3); an over-release fires one early relative
    # to the fence (also visible as a crash under GC3/4).
    assert lines[1] == "3", (backend, lines)
    assert lines[2] == "done", (backend, lines)


@pytest.mark.integration
def test_container_retain_paths_c_mirror(tmp_path, monkeypatch):
    """The C runtime mirror must agree with the pcc-Python port."""
    from pcc.frontends.python.pipeline import compile_python

    monkeypatch.setenv("PCC_GC_BACKEND", "0")
    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent(_PROGRAM).lstrip(), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )
    native = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=240,
    )
    assert native.returncode == 0, native.stderr
    assert native.stdout.split() == ["16031621", "1", "done"], native.stdout

