"""A name only an instance dict answers is cached as the class's ABSENT outcome.

Reads of such names (module attributes, dynamic instance attributes) repeated
the whole class MRO lookup every time: the attribute-resolution cache recorded
ABSENT only when the instance dict also missed. It now also records ABSENT on
a dict hit, but only when nothing on the class could answer the name for
another instance: no class attribute, field, method, ``__getattr__`` or
dict-subclass fallback. Class mutation retires the entry through the epoch.
Native output under every collector must match CPython.
"""
from __future__ import annotations

import os
import subprocess
import sys

_SOURCE = """
import helper


class Bag:
    pass


class Lazy:
    def __getattr__(self, name):
        return 'lazy:' + name


class Shadowed:
    def label(self):
        return 'method'


def read(obj, name):
    try:
        return getattr(obj, name)
    except AttributeError:
        return 'missing'


def main():
    first = Bag()
    second = Bag()
    first.color = 'red'
    print(first.color, first.color, read(second, 'color'))
    second.color = 'blue'
    print(second.color, first.color)
    del first.color
    print(read(first, 'color'), second.color)

    Bag.color = 'class'
    print(read(first, 'color'), second.color)

    lazy = Lazy()
    other = Lazy()
    lazy.size = 3
    print(lazy.size, lazy.size, other.size)

    shadow = Shadowed()
    plain = Shadowed()
    shadow.label = 'instance'
    print(shadow.label, shadow.label, plain.label())

    total = 0
    i = 0
    while i < 2000:
        total += helper.STEP + helper.BASE
        i += 1
    print(total, helper.describe())
    helper.STEP = 5
    print(helper.STEP, helper.describe())


main()
"""

_HELPER = """
STEP = 2
BASE = 1


def describe():
    return 'step=' + str(STEP)
"""


def test_dict_only_names_use_the_absent_outcome_safely(tmp_path):
    source = tmp_path / "absent.py"
    source.write_text(_SOURCE, encoding="utf-8")
    (tmp_path / "helper.py").write_text(_HELPER, encoding="utf-8")
    exe = tmp_path / "absent_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    built = subprocess.run(
        ["uv", "run", "pcc", "--backend", "self", "--python-libpython=off",
         "--ir-scaffold=on", str(source), "-o", str(exe)],
        text=True, capture_output=True, timeout=600, env=env,
    )
    assert built.returncode == 0, built.stderr
    expected = subprocess.run([sys.executable, str(source)], text=True, capture_output=True,
                              timeout=60, env=env, cwd=tmp_path)
    assert expected.returncode == 0, expected.stderr
    for backend in range(5):
        native = subprocess.run([str(exe)], text=True, capture_output=True, timeout=60,
                                env=dict(env, PCC_GC_BACKEND=str(backend)))
        assert native.returncode == 0, (backend, native.stderr)
        assert native.stdout == expected.stdout, backend
