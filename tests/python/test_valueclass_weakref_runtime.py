"""Dynamic-path weakref-on-valueclass rejection (runtime tiers).

The compile-time diagnostic catches `weakref.ref(<valueclass expr>)`
statically; a ValueBox reaching `weakref.ref` through a Dyn variable is
rejected at RUNTIME with TypeError (CPython analogue:
``weakref.ref(3)``), in BOTH runtime tiers. The fix also added the
missing post-call err-check at the native weakref.ref/proxy emission
sites — without it the pending TypeError skipped the enclosing
try/except and surfaced as an uncaught traceback later.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

from pcc.frontends.python.pipeline import compile_python

_SOURCE = """
import weakref

import pcc


@pcc.valueclass
class Pt:
    x: int
    y: int


def take(obj):
    try:
        weakref.ref(obj)
        print("weakref-ok")
    except TypeError:
        print("typeerror")


def main() -> int:
    p = Pt(x=1, y=2)
    take(p)
    return 0


main()
"""


@pytest.mark.parametrize("runtime_cc", [False], ids=["port"])
def test_dyn_valuebox_weakref_raises_typeerror(tmp_path, monkeypatch, runtime_cc):
    src = tmp_path / "weak_dyn_probe.py"
    exe = tmp_path / "weak_dyn_probe"
    src.write_text(dedent(_SOURCE), encoding="utf-8")
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
    assert proc.stdout.strip() == "typeerror"


_WEAK_DICT_SOURCE = """
import weakref

import pcc


@pcc.valueclass
class Pt:
    x: int
    y: int


def main() -> int:
    d = weakref.WeakKeyDictionary()
    p = Pt(x=1, y=2)
    try:
        d[p] = 5
        print("set-ok")
    except TypeError:
        print("typeerror")
    v = weakref.WeakValueDictionary()
    try:
        v["k"] = p
        print("vset-ok")
    except TypeError:
        print("vtypeerror")
    return 0


main()
"""


@pytest.mark.parametrize("runtime_cc", [False], ids=["port"])
def test_weak_dicts_reject_valueclass_payloads(tmp_path, monkeypatch, runtime_cc):
    # Weak*Dictionary set paths inherit the py_weakref_new rejection;
    # the subscript-store emission sites needed the same post-call
    # err-check as weakref.ref (the TypeError otherwise skipped the
    # enclosing try/except). CPython analogue: d[3] = 5 on a
    # WeakKeyDictionary raises TypeError.
    src = tmp_path / "weak_dict_probe.py"
    exe = tmp_path / "weak_dict_probe"
    src.write_text(dedent(_WEAK_DICT_SOURCE), encoding="utf-8")
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
    assert [l for l in proc.stdout.splitlines() if l] == [
        "typeerror",
        "vtypeerror",
    ]
