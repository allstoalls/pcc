"""An object-ABI integer argument must be emitted in its object projection."""
import contextlib
import io
import os
import re
import subprocess

import pytest

from pcc.backend.owned_object_emit import emit_owned_object
from pcc.frontends.python.pipeline import compile_python


SHAPES = {
    "field": ("row.value", "18446744073709551615"),
    "subscript": ("items[0]", "18446744073709551615"),
    "sum": ("row.value + 1", "18446744073709551616"),
    "negated": ("-row.value", "-18446744073709551615"),
}

ROOTS = '''from pcc.unsafe import null
import gc
def function(value: int) -> int:
    gc.collect()
    assert value == 1208925819614629174706176
    return value
class Methods:
    @staticmethod
    def static(value: int) -> int:
        gc.collect()
        assert value == 1208925819614629174706176
        return value
    def instance(self, value: int) -> int:
        gc.collect()
        assert value == 1208925819614629174706176
        return value
def rebound(value: int) -> int:
    gc.collect()
    value = 9223372036854775807
    gc.collect()
    return value
def main():
    value = 1 << 80
    assert function(value) == value
    assert Methods.static(value) == value
    assert Methods().instance(value) == value
    assert rebound(value) == 9223372036854775807
    print("BOXED_PARAMETER_ROOTS_OK")
main()
'''


def test_boxed_parameter_roots_owned_ir(tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "boxed_roots.py"
    source.write_text(ROOTS)
    output = source.with_suffix(".ll")
    compile_python(str(source), str(output), emit_llvm_only=True, backend="self",
                   libpython_mode="off", ir_scaffold_mode="on")
    text = output.read_text()
    for name in ("function", "Methods_static", "Methods_instance"):
        body = re.search(r"(?ms)^define [^\n]*@user_boxed_roots_" + name + r"\(.*?\n\}", text).group(0)
        slot = re.search(r"(%value\.addr(?:\.\d+)?) = alloca ptr", body).group(1)
        before_collection = body[:body.index("@pcc_gc_collect")]
        # The slot is registered and retired directly, or through identity aliases.
        roots = [slot] + re.findall(r"(%gc\.frame\.slots\.ptr[^ ]*) = bitcast ptr " + re.escape(slot) + r" to ptr", before_collection)
        assert any(re.search(r"@pcc_gc_frame_enter\([^\n]*ptr " + re.escape(root) + r"\)", before_collection)
                   for root in roots), name
        assert ".pcc.gc.frame.map.borrowed" in before_collection, name
        assert "@pcc_gc_load_borrowed_ptr" in body, name
        leaves = [slot] + re.findall(r"(%gc\.frame\.leave\.ptr[^ ]*) = bitcast ptr " + re.escape(slot) + r" to ptr", body)
        assert any(re.search(r"@pcc_gc_frame_leave\(ptr " + re.escape(leave) + r"\)", body) for leave in leaves), name
    body = re.search(r"(?ms)^define [^\n]*@user_boxed_roots_rebound\(.*?\n\}", text).group(0)
    assert "value.param.retain" in body
    assert ".pcc.gc.frame.map.borrowed" not in body
    assert re.search(r"%value\.owned[^ ]* = alloca i1", body)
    assert "@pcc_gc_store_root" in body
    assert emit_owned_object(text, "x86_64-unknown-linux-gnu")[:4] == b"\x7fELF"


@pytest.mark.integration
def test_boxed_parameter_roots_native(tmp_path, monkeypatch, pcc_runtime_archive,
                                     python_program_compiler):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "boxed_roots.py"
    source.write_text(ROOTS)
    binary = source.with_suffix("")
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                             env=dict(os.environ, PATH="/nonexistent", PCC_GC_BACKEND=str(backend)))
        assert ran.returncode == 0, (backend, ran.stdout, ran.stderr)
        assert ran.stdout == "BOXED_PARAMETER_ROOTS_OK\n"


def _source(shape):
    expression, expected = SHAPES[shape]
    return '''from pcc.unsafe import null
from dataclasses import dataclass
@dataclass
class Row:
    value: int
def consume(value: int, bits: int, label: str, *, nonzero: bool = False) -> int:
    if nonzero and value == 0:
        raise ValueError(label)
    return value
def probe(row: Row, items: tuple[int, ...]):
    return consume(''' + expression + ''', 64, "safepoint id", nonzero=True)
def main():
    row = Row(18446744073709551615)
    items = (18446744073709551615,)
    assert probe(row, items) == ''' + expected + '''
    print("BOXED_ARGUMENT_PROJECTION_OK")
main()
'''


@pytest.mark.parametrize("shape", SHAPES)
def test_boxed_argument_reference(shape):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exec(_source(shape), {})
    assert output.getvalue() == "BOXED_ARGUMENT_PROJECTION_OK\n"


@pytest.mark.parametrize("shape", SHAPES)
def test_boxed_argument_owned_ir(tmp_path, monkeypatch, shape):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / ("boxed_argument_" + shape + ".py")
    source.write_text(_source(shape))
    output = source.with_suffix(".ll")
    compile_python(str(source), str(output), emit_llvm_only=True, backend="self",
                   libpython_mode="off", ir_scaffold_mode="on")
    text = output.read_text()
    body = re.search(r"(?ms)^define [^\n]*@user_[^\n]*_probe\(.*?\n\}", text).group(0)
    assert "@py_int_to_i64_lane" not in body
    assert emit_owned_object(text, "x86_64-unknown-linux-gnu")[:4] == b"\x7fELF"


@pytest.mark.integration
@pytest.mark.parametrize("shape", SHAPES)
def test_boxed_argument_native(tmp_path, monkeypatch, pcc_runtime_archive,
                               python_program_compiler, shape):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / ("boxed_argument_" + shape + ".py")
    source.write_text(_source(shape))
    binary = source.with_suffix("")
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                             env=dict(os.environ, PATH="/nonexistent", PCC_GC_BACKEND=str(backend)))
        assert ran.returncode == 0, (backend, ran.stdout, ran.stderr)
        assert ran.stdout == "BOXED_ARGUMENT_PROJECTION_OK\n"
