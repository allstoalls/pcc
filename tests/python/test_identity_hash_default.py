"""Instances without ``__hash__`` and ``__eq__`` hash by identity.

``py_user_hash_dispatch`` returned 0 for every instance whose class defines
neither dunder, so a set or dict of such objects kept all of them in one probe
chain: each lookup compared the key against every entry, dispatching a
``__eq__`` lookup per comparison.  A native Stage2 frontend worker spent 13% of
its time in one such ``value in set`` check.  CPython's ``object.__hash__``
hashes by identity; the forwarding collectors (GC3/GC4) move objects, so they
hash the stable object id instead of the address.  A class with ``__eq__`` but
no ``__hash__`` keeps the old hash of 0.
"""

from __future__ import annotations

import subprocess
import textwrap

import pytest

from pcc.frontends.python.pipeline import compile_python

BACKENDS = ("0", "1", "2", "3", "4")

PROGRAM = '''
import gc


class Plain:
    def __init__(self, n):
        self.n = n


class Child(Plain):
    pass


class Keyed:
    def __init__(self, v):
        self.v = v

    def __eq__(self, other):
        return isinstance(other, Keyed) and self.v == other.v

    def __hash__(self):
        return self.v


def main() -> None:
    items = []
    i = 0
    while i < 3000:
        if i % 2 == 0:
            items.append(Plain(i))
        else:
            items.append(Child(i))
        i = i + 1
    members = set(items)
    table = {}
    for item in items:
        table[item] = item.n
    before = [hash(item) for item in items]
    gc.collect()
    after = [hash(item) for item in items]
    print(before == after)
    print(len(set(after)) == len(items))
    found = 0
    total = 0
    for item in items:
        if item in members:
            found = found + 1
        total = total + table[item]
    print(found, total)
    print(Plain(-1) in members, len(members), len(table))
    ordered = [table[key] for key in table]
    print(ordered[:3], ordered[-1])
    print(Keyed(7) in {Keyed(7), Keyed(8)}, {Keyed(1): "a"}[Keyed(1)])


main()
'''

EXPECTED = "\n".join([
    "True",
    "True",
    f"3000 {sum(range(3000))}",
    "False 3000 3000",
    "[0, 1, 2] 2999",
    "True a",
])


@pytest.mark.parametrize("backend", BACKENDS)
def test_identity_hashed_instances_in_sets_and_dicts(tmp_path, backend):
    src = tmp_path / f"identity_hash_{backend}.py"
    exe = tmp_path / f"identity_hash_{backend}.out"
    src.write_text(textwrap.dedent(PROGRAM).lstrip(), encoding="utf-8")
    compile_python(
        str(src), str(exe),
        libpython_mode="off", ir_scaffold_mode="on", backend="self",
    )
    done = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=240,
        env={"PCC_GC_BACKEND": backend, "PATH": "/usr/bin:/bin"},
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.strip() == EXPECTED
