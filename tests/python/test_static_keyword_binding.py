"""Keyword and default calls to known callees bind statically when exact.

A call that names keywords or omits defaulted formals used to go through the
published callable's runtime signature binder (argument tuple, keyword dict,
merge and name matching) even when its binding was known at compile time.
``_statically_bindable_call`` now admits a direct call when every keyword
names a distinct keyword-capable formal in formal order (direct formal-order
evaluation is then Python's left-to-right order) and every omitted formal's
default is one object wherever it is produced: None, a bool or a tagged small
int. Everything else stays with the binder: reordered keywords, unknown or
duplicate names, missing formals, and str/bytes/tuple/heap-int defaults whose
identity a call site cannot reproduce.

Slot-call tests that need the binder as their consumer give it a ``**extras``
formal: a callee with a keyword dictionary always binds at runtime.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest


PROVIDER = '''def remote(a, b=2, *, scale=1):
    return (a + b) * scale
'''

CONSUMER = '''from dataclasses import dataclass
from kwbind_provider import remote


def trace(label, value):
    print("eval", label)
    return value


def pair(a, b):
    return (a, b)


def mutable(x, bucket=[]):
    bucket.append(x)
    return bucket


def kwonly(a, *, sep="-", times=2):
    return sep.join([str(a)] * times)


def scalar_defaults(a, b=None, c=True, d=-5, e=7):
    return (a, b, c, d, e)


def tuple_default(x=(1, (2, "x"))):
    return x


def heap_int_default(x=123456789012345678901234567890):
    return x


def bytes_default(x=b"bytes"):
    return x


def str_default(x="a longer default string"):
    return x


class Box:
    def __init__(self, value, *, label="box"):
        self.value = value
        self.label = label

    def show(self, prefix, suffix=">", name=None):
        return prefix + self.label + ":" + str(self.value) + suffix + str(name)


@dataclass
class Point:
    x: int
    y: int = 0
    tag: str = "p"


def in_order():
    return pair(a=1, b=2)


def reordered():
    return pair(b=2, a=1)


def scalar_omitted():
    return scalar_defaults(1, d=3)


def keyword_only():
    return kwonly(7, sep="+", times=3)


def tuple_omitted():
    return tuple_default()


def str_omitted():
    return str_default()


def pick(flag, first, second):
    return pair(a=trace("left", first) if flag else trace("right", second), b=flag)


def main():
    print(pair(b=trace("b", 2), a=trace("a", 1)))
    print(pair(a=trace("a", 1), b=trace("b", 2)))
    first = mutable(1)
    second = mutable(2)
    print(first is second, second)
    try:
        pair(1, c=3)
    except TypeError as exc:
        print("TypeError", "unexpected keyword" in str(exc))
    try:
        pair(1)
    except TypeError as exc:
        print("TypeError missing", "missing" in str(exc))
    try:
        pair(1, a=2)
    except TypeError as exc:
        print(type(exc).__name__)
    print(kwonly(7), kwonly(7, times=3), keyword_only(), kwonly(7, sep="+", times=1))
    print(scalar_defaults(1), scalar_omitted(), scalar_defaults(a=2, e=9))
    print(in_order(), reordered())
    print(tuple_omitted() is tuple_default(), tuple_default() is tuple_default.__defaults__[0])
    print(heap_int_default() is heap_int_default(),
          heap_int_default() is heap_int_default.__defaults__[0])
    print(bytes_default() is bytes_default(), bytes_default() is bytes_default.__defaults__[0])
    print(str_omitted() is str_default(), str_default() is str_default.__defaults__[0])
    box = Box(4, label="crate")
    print(box.show("<"), box.show("<", name="n"), box.show(prefix="[", suffix="]"))
    print(Box(5).label)
    print(Point(1), Point(x=2, tag="q"), Point(3, y=4))
    print(remote(1), remote(1, b=5), remote(a=2, scale=3))
    print(pick(True, [1], [2]), pick(False, [1], [2]))


main()
'''

EXPECTED = '''eval b
eval a
(1, 2)
eval a
eval b
(1, 2)
True [1, 2]
TypeError True
TypeError missing True
TypeError
7-7 7-7-7 7+7+7 7
(1, None, True, -5, 7) (1, None, True, 3, 7) (2, None, True, -5, 9)
(1, 2) (1, 2)
True True
True True
True True
True True
<crate:4>None <crate:4>n [crate:4]None
box
Point(x=1, y=0, tag='p') Point(x=2, y=0, tag='q') Point(x=3, y=4, tag='p')
3 6 12
eval left
eval right
([1], True) ([2], False)
'''


def _write_sources(directory):
    paths, names = [], []
    for name, source in (("kwbind_provider", PROVIDER), ("kwbind", CONSUMER)):
        path = directory / (name + ".py")
        path.write_text(source, encoding="utf-8")
        paths.append(str(path))
        names.append(name)
    return paths, names


def _body(text, name):
    match = re.search(r"^define [^\n]*@" + re.escape(name) + r"\([^\n]*\).*?^}", text, re.M | re.S)
    assert match, name
    return match.group(0)


def test_reference_output(tmp_path):
    paths, _names = _write_sources(tmp_path)
    environment = dict(os.environ)
    environment.pop("LC_ALL", None)
    result = subprocess.run(
        [sys.executable, paths[-1]], cwd=tmp_path, env=environment,
        capture_output=True, text=True, timeout=20,
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, EXPECTED, "")


def test_exact_keyword_calls_lower_to_direct_calls(tmp_path, monkeypatch):
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.pipeline_context import build_closed_world_context
    from pcc.frontends.python.type_infer import infer_module

    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    paths, names = _write_sources(tmp_path)
    modules, exports, derived = build_closed_world_context(paths, names)
    external = {"kwbind_provider": exports["kwbind_provider"]}
    typed = infer_module(modules[-1], external_exports=external, derived_class_map=derived)
    codegen = L1CodeGen(typed, ir_scaffold_mode="on")
    codegen._native_module_exports = external
    codegen._sibling_module_inits = ("kwbind_provider",)
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    text = str(codegen.generate(typed))
    (tmp_path / "kwbind.ll").write_text(text)
    for probe, callee in (
        ("in_order", "pair"),
        ("scalar_omitted", "scalar_defaults"),
        ("keyword_only", "kwonly"),
        # The module's pooled literal is the def's own default object; the
        # native run checks ``str_omitted() is str_default.__defaults__[0]``.
        ("str_omitted", "str_default"),
    ):
        lowered = _body(text, "user_kwbind_" + probe)
        assert re.search(r"call [^\n]*@user_kwbind_" + callee + r"\(", lowered), probe
        assert "@py_obj_call_slots" not in lowered, probe
    for probe, callee in (
        ("reordered", "pair"),
        ("tuple_omitted", "tuple_default"),
    ):
        lowered = _body(text, "user_kwbind_" + probe)
        assert "@py_obj_call_slots" in lowered, probe
        assert not re.search(r"call [^\n]*@user_kwbind_" + callee + r"\(", lowered), probe


@pytest.mark.integration
def test_keyword_binding_matches_cpython_on_five_gcs(tmp_path, monkeypatch, pcc_runtime_archive):
    from pcc.frontends.python.pipeline import compile_python_multi
    from pcc.tools.runtime_archive_provenance import manifest_is_stale_for_current_codegen

    manifest = json.loads(Path(str(pcc_runtime_archive) + ".provenance.json").read_text())
    assert not manifest_is_stale_for_current_codegen(manifest), "runtime/codegen identity mismatch"
    for name, value in {
        "PCC_PYTHON_IR_PASSES": "off",
        "PCC_PY_FRONTEND_JOBS": "1",
        "PCC_SELF_BACKEND_JOBS": "1",
        "PCC_NO_AUTO_PCC1": "1",
        "PCC_SELF_LINK": "pcc",
        "PCC_SELF_BACKEND_OBJECT_CACHE": "0",
        "PCC_PY_FRONTEND_IR_CACHE": "0",
        "PCC_RUNTIME_ARCHIVE": str(pcc_runtime_archive),
    }.items():
        monkeypatch.setenv(name, value)
    paths, names = _write_sources(tmp_path)
    binary = tmp_path / "kwbind"
    compile_python_multi(
        paths, str(binary), module_names=names, entry_module="kwbind",
        backend="self", libpython_mode="off", ir_scaffold_mode="on",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        environment = dict(os.environ, PCC_GC_BACKEND=str(backend), PATH="/nonexistent")
        environment.pop("LC_ALL", None)
        result = subprocess.run(
            [str(binary)], cwd=tmp_path, env=environment,
            capture_output=True, text=True, timeout=30,
        )
        (tmp_path / ("gc" + str(backend) + ".stdout")).write_text(result.stdout)
        (tmp_path / ("gc" + str(backend) + ".stderr")).write_text(result.stderr)
        assert (result.returncode, result.stdout, result.stderr) == (0, EXPECTED, ""), (
            backend, result.stdout, result.stderr,
        )
