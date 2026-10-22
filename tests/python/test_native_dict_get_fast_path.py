"""dict.get's backend-0 shortcut answers only what needs no Python code.

On one thread under the refcount collector, py_dict_get_default_slots probes
an exact dict for a str or small-int key directly and publishes the value or
the default without the rooted slot transaction. Everything else must still
reach that transaction: keys that need ``__eq__`` (``True``/``1.0`` against
``1``, a user key against a str), other key types, dict subclasses. The
program covers each, plus tombstones and misses, and must print what CPython
prints under every collector.
"""
from __future__ import annotations

import os
import subprocess
import sys

_SOURCE = """
class Key:
    def __init__(self, text):
        self.text = text

    def __hash__(self):
        return hash(self.text)

    def __eq__(self, other):
        if isinstance(other, Key):
            return other.text == self.text
        return other == self.text


class Table(dict):
    pass


def main():
    table = {'alpha': 1, 'beta': [2], 3: 'three', True: 'flag'}
    print(table.get('alpha'), table.get('beta', 0), table.get(3), table.get(4, 'four'))
    print(table.get('gamma'), table.get('gamma', None), table.get('gamma', 'g'))
    print(table.get(1), table.get(1.0), table.get(True), table.get(0, 'zero'))
    print(table.get(Key('alpha'), 'no key'), table.get(Key('delta'), 'no delta'))
    keyed = {Key('alpha'): 'user'}
    print(keyed.get('alpha'), keyed.get('omega', 'none'))
    del table['alpha']
    print(table.get('alpha', 'deleted'), table.get('beta'))
    table['alpha'] = 'back'
    print(table.get('alpha'))

    sub = Table()
    sub['x'] = 1
    print(sub.get('x'), sub.get('y', 'default'))

    numbers = {}
    i = 0
    while i < 3000:
        numbers[i] = i * 2
        i += 1
    total = 0
    i = 0
    while i < 3200:
        total += numbers.get(i, -1)
        i += 1
    print(total)

    words = {}
    for word in ['a', 'b', 'c', 'a', 'b', 'a']:
        words[word] = words.get(word, 0) + 1
    print(sorted(words.items()))

    big = {-(2 ** 70): 'big'}
    print(big.get(-(2 ** 70)), big.get(2 ** 70, 'small'))


main()
"""


def test_dict_get_shortcut_matches_cpython(tmp_path):
    source = tmp_path / "dict_get.py"
    source.write_text(_SOURCE, encoding="utf-8")
    exe = tmp_path / "dict_get_bin"
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
