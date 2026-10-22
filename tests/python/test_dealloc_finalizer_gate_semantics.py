"""Instance deallocation skips the ``__del__`` MRO lookup until some class
defines one, without changing finalizer semantics.

Every instance free ran ``py_class_lookup(cls, "__del__")`` (string hash plus
a dict probe per MRO entry) even though almost no program defines a
finalizer; together with a provenance probe, two backend queries and an empty
identity-index probe it was the bulk of the ``alloc_small_object`` per-op row
(evidence PERF-P0-PCC1-WORKER-OBJECT-PROTOCOL-TAX/001).  The runtime now
counts classes that ever installed ``__del__`` (class body or a later
``cls.__del__ = f`` through the runtime class-attribute store) and skips the
lookup while the count is zero.  This program covers a class-body ``__del__``,
an inherited one, and plain instances churned before and after those classes
exist.

Found en route, pre-existing and not changed here: ``Plain.__del__ = f``
assigned after class creation is lowered to a compile-time ``.classattr``
global store that the runtime class object never sees, so such a finalizer
never runs (with or without the gate; CPython runs it).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROGRAM = (
    "class Plain:\n"
    "    def __init__(self, v: int):\n"
    "        self.v = v\n"
    "\n"
    "def churn(n: int) -> int:\n"
    "    total = 0\n"
    "    i = 0\n"
    "    while i < n:\n"
    "        p = Plain(i)\n"
    "        total += p.v\n"
    "        i += 1\n"
    "    return total\n"
    "\n"
    "class WithDel:\n"
    "    def __init__(self, v: int):\n"
    "        self.v = v\n"
    "    def __del__(self):\n"
    "        print('del', self.v)\n"
    "\n"
    "class Sub(WithDel):\n"
    "    pass\n"
    "\n"
    "def main() -> None:\n"
    "    print(churn(1000))\n"
    "    keep = Plain(7)\n"
    "    keep = None\n"
    "    w = WithDel(1)\n"
    "    w = None\n"
    "    s = Sub(2)\n"
    "    s = None\n"
    "    print(churn(10))\n"
    "    print('end')\n"
    "\n"
    "main()\n"
)


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    return env


def _expected(src: Path) -> str:
    out = subprocess.run([sys.executable, str(src)], text=True, capture_output=True, timeout=60).stdout
    assert "499500\ndel 1\ndel 2\n45\nend\n" == out, out
    return out


def test_finalizers_still_run_on_every_backend(tmp_path):
    src = tmp_path / "finalizer_gate.py"
    src.write_text(PROGRAM, encoding="utf-8")
    exe = tmp_path / "finalizer_gate.bin"
    cmd = ["uv", "run", "pcc", "--backend", "self", "--python-libpython=off", "--ir-scaffold=on", str(src), "-o", str(exe)]
    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=600, env=_env())
    assert proc.returncode == 0, proc.stderr
    expected = _expected(src)
    env = _env()
    for backend in ("0", "1", "2", "3", "4"):
        env["PCC_GC_BACKEND"] = backend
        run = subprocess.run([str(exe)], text=True, capture_output=True, timeout=60, env=env)
        assert run.returncode == 0, (backend, run.stdout, run.stderr)
        assert run.stdout == expected, (backend, run.stdout, expected)


def test_finalizers_still_run_with_the_c_runtime_mirror(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "finalizer_gate_cc.py"
    exe = tmp_path / "finalizer_gate_cc.out"
    src.write_text(PROGRAM, encoding="utf-8")
    compile_python(str(src), str(exe), ir_scaffold_mode="on", libpython_mode="off", backend="self")
    expected = _expected(src)
    env = _env()
    for backend in ("0", "3", "4"):
        env["PCC_GC_BACKEND"] = backend
        run = subprocess.run([str(exe)], text=True, capture_output=True, timeout=60, env=env)
        assert run.returncode == 0, (backend, run.stdout, run.stderr)
        assert run.stdout == expected, (backend, run.stdout, expected)
