"""A user method named like a container method must not be hijacked.

`list`/`dict`/`set` method fast paths on a `dyn` receiver are selected by
method *name* alone -- the static type says nothing.  That is sound only
while nothing else answers to the name, and user classes routinely define
`update`, `add`, `copy`, `get`, `keys`, `values`, `items`, `setdefault`,
`discard`, `pop` and `remove`.

Without a runtime type-tag test the call reached `py_set_update` /
`py_dict_get` with a user instance, which those helpers ignore: the method
body never ran and nothing was raised, so the call silently evaporated.
`pcc/py_stdlib/hashlib.py` is the worked example -- `clone.update(...)` in
`_SHA256.digest` did nothing and the `while len(clone._buf) != 56` padding
loop spun forever, so `hashlib.sha256(x).hexdigest()` never returned.

A `dyn` receiver is the ordinary case: any unannotated factory
(`def sha256(data=b""): return _SHA256(data)`) produces one.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]

_PROGRAM = textwrap.dedent(
    '''\
    class Recorder:
        def __init__(self):
            self._n = 0

        def update(self, x):
            self._n += 1

        def add(self, x):
            self._n += 2

        def copy(self):
            self._n += 4

        def get(self, k):
            self._n += 8
            return self._n

        def keys(self):
            self._n += 16
            return None

        def values(self):
            self._n += 32
            return None

        def items(self):
            self._n += 64
            return None

        def setdefault(self, k, v):
            self._n += 128
            return None

        def discard(self, x):
            self._n += 256

        def pop(self, k):
            self._n += 512
            return None

        def remove(self, x):
            self._n += 1024


    def make_recorder():
        """No return annotation, so the receiver below is statically dyn."""
        return Recorder()


    def main() -> int:
        r = make_recorder()
        r.update(1)
        r.add(1)
        r.copy()
        r.get(1)
        r.keys()
        r.values()
        r.items()
        r.setdefault(1, 2)
        r.discard(1)
        r.pop(1)
        r.remove(1)
        print("user methods:", r._n)

        d = {}
        d.update({"a": 1})
        d.setdefault("b", 2)
        dc = d.copy()
        dc["c"] = 3
        print("dict:", sorted(d.keys()), d.get("a"), len(d), len(dc))

        s = {1, 2}
        s.add(3)
        s.update({4})
        s.discard(1)
        sc = s.copy()
        sc.add(9)
        print("set:", sorted(s), len(s), len(sc))

        lst = [1, 2]
        lst.append(3)
        lst.extend([4])
        popped = lst.pop()
        print("list:", lst, popped)
        return 0


    main()
    '''
)

# 2047 == every one of the eleven bodies ran exactly once.
_EXPECTED = (
    "user methods: 2047\n"
    "dict: ['a', 'b'] 1 2 3\n"
    "set: [2, 3, 4] 3 4\n"
    "list: [1, 2, 3] 4\n"
)


@pytest.mark.pcc_gate(probe="self_backend")
def test_user_container_named_methods_are_not_hijacked(tmp_path):
    source = tmp_path / "guard.py"
    source.write_text(_PROGRAM, encoding="utf-8")
    binary = tmp_path / "guard"

    build = subprocess.run(
        [
            sys.executable, "-m", "pcc",
            "--ir-scaffold=on", "--backend", "self",
            "--python-libpython", "off",
            str(source), "-o", str(binary),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=1800,
    )
    assert build.returncode == 0, build.stdout + build.stderr

    run = subprocess.run(
        [str(binary)], capture_output=True, text=True, timeout=120,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == _EXPECTED, (
        "a missing method body scores 0 for its bit: "
        f"got {run.stdout!r}"
    )


def test_cpython_reference_behaviour(tmp_path):
    """The same program under CPython, so the contract is not self-defined."""
    source = tmp_path / "guard.py"
    source.write_text(_PROGRAM, encoding="utf-8")
    run = subprocess.run(
        [sys.executable, str(source)], capture_output=True, text=True, timeout=120,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == _EXPECTED
