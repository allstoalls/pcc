"""dict lookups agree with CPython across key types, and numeric hashing is
CPython's modular hash on every backend and in the C runtime mirror.

Two defects behind the ``dict_get_str`` / ``dict_set_str`` per-op rows
(evidence PERF-P0-PCC1-WORKER-OBJECT-PROTOCOL-TAX/001):

- ``py_obj_hash`` hashed a float by its raw IEEE bits, so ``d[1.0]`` missed
  the entry stored under ``d[1]`` and ``{1: a, 1.0: b}`` held two keys.  The
  C mirror special-cased integral floats and the port did not: mirror drift.
  Both now implement ``long_hash`` / ``_Py_HashDouble`` (reduction modulo
  ``2**61 - 1``), so ``hash(1) == hash(1.0) == hash(True)`` and
  ``hash(0.5)``, ``hash(1 << 61)`` print CPython's values.
- ``py_dict_get`` / ``py_dict_set`` ran every lookup through the rooted,
  locked, plan-committing operation.  On the refcount backend a str or
  tagged-int key now probes directly; anything that needs ``py_obj_eq`` (a
  bool or float equal to an int, a user ``__eq__``) still takes the rooted
  path.  This program crosses every one of those boundaries.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

DICT_SEMANTICS = (
    "class K:\n"
    "    def __init__(self, n: int):\n"
    "        self.n = n\n"
    "    def __hash__(self) -> int:\n"
    "        return self.n % 7\n"
    "    def __eq__(self, other) -> bool:\n"
    "        return isinstance(other, K) and self.n == other.n\n"
    "\n"
    "class S:\n"
    "    def __init__(self, s: str):\n"
    "        self.s = s\n"
    "    def __hash__(self) -> int:\n"
    "        return hash(self.s)\n"
    "    def __eq__(self, other) -> bool:\n"
    "        if isinstance(other, S):\n"
    "            return self.s == other.s\n"
    "        return other == self.s\n"
    "\n"
    "def main() -> None:\n"
    "    print(hash(1), hash(1.0), hash(True), hash(-1), hash(-1.0), hash(0.5), hash(2.5), hash(-2.5))\n"
    "    print(hash(1e300), hash(1e-300), hash(5e-324), hash(0.0), hash(-0.0), hash(123456.789))\n"
    "    print(hash(1 << 61), hash((1 << 61) - 1), hash((1 << 62) - 1), hash(-(1 << 62)), hash(float(1 << 61)))\n"
    "    print(hash((1, 2.0, 'x')) == hash((1.0, 2, 'x')), hash((1 << 61, 0.5)) == hash((float(1 << 61), 0.5)))\n"
    "    d = {}\n"
    "    p = 'pha'\n"
    "    a = 'alpha'\n"
    "    b = 'al' + p\n"
    "    d[a] = 1\n"
    "    print(d[b], d.get(b), d.get('zzz', -1), b in d, 'zzz' in d, a is b)\n"
    "    d[1] = 'one'\n"
    "    d[-1] = 'minus'\n"
    "    d[-2] = 'minus2'\n"
    "    print(d[1], d[True], d[1.0], d[-1], d[-2], len(d))\n"
    "    d[True] = 'bool'\n"
    "    d[2.0] = 'two'\n"
    "    print(d[1], d[2], len(d))\n"
    "    big = 1 << 70\n"
    "    d[big] = 'big'\n"
    "    print(d[big], d[1 << 70], d.get(1 << 71, 'none'), len(d))\n"
    "    ks = []\n"
    "    i = 0\n"
    "    while i < 20:\n"
    "        ks.append(K(i))\n"
    "        i += 1\n"
    "    i = 0\n"
    "    while i < 20:\n"
    "        d[ks[i]] = i\n"
    "        i += 1\n"
    "    total = 0\n"
    "    i = 0\n"
    "    while i < 20:\n"
    "        total += d[ks[i]]\n"
    "        i += 1\n"
    "    print(total, K(5) in d, d[K(12)], d.get(K(99), 'none'), len(d))\n"
    "    d[S('alpha')] = 'S'\n"
    "    print(d['alpha'], len(d))\n"
    "    try:\n"
    "        print(d['missing'])\n"
    "    except KeyError:\n"
    "        print('KeyError')\n"
    "    del d['alpha']\n"
    "    print('alpha' in d, d.get('alpha', 'gone'), len(d))\n"
    "    d['alpha'] = 2\n"
    "    print(d['alpha'], len(d))\n"
    "    j = 0\n"
    "    while j < 200:\n"
    "        d['k' + str(j)] = j\n"
    "        j += 1\n"
    "    s = 0\n"
    "    j = 0\n"
    "    while j < 200:\n"
    "        s += d['k' + str(j)]\n"
    "        j += 1\n"
    "    print(s, len(d))\n"
    "    d[None] = 'n'\n"
    "    print(d[None], None in d)\n"
    "    j = 0\n"
    "    while j < 200:\n"
    "        del d['k' + str(j)]\n"
    "        j += 2\n"
    "    print(len(d), d.get('k0', 'gone'), d['k1'], d['k199'])\n"
    "    d['k0'] = 'back'\n"
    "    print(d['k0'], len(d))\n"
    "\n"
    "main()\n"
)


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    return env


def _compile(tmp_path: Path, name: str, source: str) -> tuple[Path, Path]:
    src = tmp_path / f"{name}.py"
    src.write_text(source, encoding="utf-8")
    out = tmp_path / f"{name}.bin"
    cmd = ["uv", "run", "pcc", "--backend", "self", "--python-libpython=off", "--ir-scaffold=on", str(src), "-o", str(out)]
    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=600, env=_env())
    assert proc.returncode == 0, proc.stderr
    return src, out


def test_dict_semantics_and_numeric_hash_match_cpython_on_every_backend(tmp_path):
    src, exe = _compile(tmp_path, "dict_semantics", DICT_SEMANTICS)
    expected = subprocess.run([sys.executable, str(src)], text=True, capture_output=True, timeout=60).stdout
    env = _env()
    for backend in ("0", "1", "2", "3", "4"):
        env["PCC_GC_BACKEND"] = backend
        run = subprocess.run([str(exe)], text=True, capture_output=True, timeout=60, env=env)
        assert run.returncode == 0, (backend, run.stdout, run.stderr)
        assert run.stdout == expected, (backend, run.stdout, expected)


def test_dict_semantics_match_cpython_with_the_c_runtime_mirror(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "dict_semantics_cc.py"
    exe = tmp_path / "dict_semantics_cc.out"
    src.write_text(DICT_SEMANTICS, encoding="utf-8")
    compile_python(str(src), str(exe), ir_scaffold_mode="on", libpython_mode="off", backend="self")
    expected = subprocess.run([sys.executable, str(src)], text=True, capture_output=True, timeout=60).stdout
    env = _env()
    for backend in ("0", "3", "4"):
        env["PCC_GC_BACKEND"] = backend
        run = subprocess.run([str(exe)], text=True, capture_output=True, timeout=60, env=env)
        assert run.returncode == 0, (backend, run.stdout, run.stderr)
        assert run.stdout == expected, (backend, run.stdout, expected)
