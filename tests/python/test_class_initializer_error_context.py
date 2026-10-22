"""Nested class literal errors unwind the active class roots."""

import contextlib
import io
import os
import re
import subprocess

import pytest

from pcc.backend.owned_object_emit import emit_owned_object
from pcc.frontends.python.pipeline import compile_python


DICT_ATTRIBUTE = '''class Prior:
    sentinel = object()
    def method(self, value=sentinel):
        return value
class Bounds:
    targets = {("int",): (-2**31, 2**31 - 1), ("unsigned", "int"): (0, 2**32 - 1)}
def main():
    assert Bounds.targets[("int",)] == (-2147483648, 2147483647)
    assert Bounds.targets[("unsigned", "int")] == (0, 4294967295)
    assert Prior().method() is Prior.sentinel
    print("CLASS_DICT_INIT_OK")
main()
'''

KEY_FAILURE = '''import gc
import weakref
finalized = []
references = []
class Prior:
    sentinel = object()
    def method(self, value=sentinel):
        return value
class Key:
    def __hash__(self):
        raise ValueError("key hash")
    def __del__(self):
        finalized.append(1)
def key():
    value = Key()
    references.append(weakref.ref(value))
    return value
def main():
    try:
        class Broken:
            table = {key(): 7}
    except ValueError as error:
        assert str(error) == "key hash"
    else:
        raise AssertionError("class key error lost")
    gc.collect()
    assert references[0]() is None
    assert finalized == [1]
    assert Prior().method() is Prior.sentinel
    print("CLASS_DICT_KEY_FAILURE_OK")
main()
'''

NUMERIC_ATTRIBUTE = '''class Prior:
    sentinel = object()
    def method(self, value=sentinel):
        return value
class Bounds:
    kinds = frozenset({"int", "signed", "signed int"})
    minimum = -(2 ** 31)
    maximum = 2 ** 31 - 1
    unsigned_maximum = 2 ** 32 - 1
    @classmethod
    def valid(cls, value):
        return cls.minimum <= value <= cls.maximum
def main():
    assert Bounds.minimum == -2147483648
    assert Bounds.maximum == 2147483647
    assert Bounds.unsigned_maximum == 4294967295
    assert "signed int" in Bounds.kinds
    assert Bounds.valid(-2147483648)
    assert not Bounds.valid(2147483648)
    assert Prior().method() is Prior.sentinel
    print("CLASS_NUMERIC_INIT_OK")
main()
'''

POW_FAILURE = '''class Prior:
    sentinel = object()
    def method(self, value=sentinel):
        return value
def exponent():
    raise ValueError("pow exponent")
def main():
    try:
        class Broken:
            marker = object()
            def method(self, value=marker):
                return value
            minimum = -(2 ** exponent())
    except ValueError as error:
        assert str(error) == "pow exponent"
    else:
        raise AssertionError("class pow error lost")
    assert Prior().method() is Prior.sentinel
    print("CLASS_POW_FAILURE_OK")
main()
'''

PROGRAMS = ((DICT_ATTRIBUTE, "CLASS_DICT_INIT_OK\n"),
            (KEY_FAILURE, "CLASS_DICT_KEY_FAILURE_OK\n"),
            (NUMERIC_ATTRIBUTE, "CLASS_NUMERIC_INIT_OK\n"),
            (POW_FAILURE, "CLASS_POW_FAILURE_OK\n"))
PROGRAM_IDS = ("dict-attr", "key-error", "numeric-bounds", "pow-error")


@pytest.mark.parametrize("source_text,expected", PROGRAMS, ids=PROGRAM_IDS)
def test_class_initializer_reference(source_text, expected):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exec(source_text, {})
    assert output.getvalue() == expected


@pytest.mark.parametrize("direct", [False, True], ids=["text", "indexed"])
@pytest.mark.parametrize("source_text,expected", PROGRAMS, ids=PROGRAM_IDS)
def test_class_initializer_nested_errors_reach_owned_emitter(tmp_path, monkeypatch,
                                                           direct, source_text, expected):
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1" if direct else "0")
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "class_initializers.py"
    output = tmp_path / "class_initializers.ll"
    source.write_text(source_text)
    compile_python(str(source), str(output), emit_llvm_only=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode="on")
    text = output.read_text()
    assert "class.body.unwind" in text
    bodies = re.findall(r'^define[^\n]*\{\n(.*?)^\}', text, re.M | re.S)
    if source_text in (DICT_ATTRIBUTE, KEY_FAILURE):
        assert any("@py_dict_set(" in body for body in bodies)
    elif source_text == NUMERIC_ATTRIBUTE:
        assert any("@py_int_neg(" in body for body in bodies)
    else:
        assert any("@user_class_initializers_exponent(" in body for body in bodies)
    data = emit_owned_object(text, "arm64-apple-darwin")
    assert data[:4] == b"\xcf\xfa\xed\xfe"


@pytest.mark.parametrize("kind", ["tuple", "list"])
def test_class_literal_controls_reach_owned_emitter(tmp_path, monkeypatch, kind):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "class_literal_control.py"
    output = tmp_path / "class_literal_control.ll"
    expression = "(object(), object())" if kind == "tuple" else "[object(), object()]"
    source.write_text("class Holder:\n    values = " + expression +
                      "\ndef main():\n    assert len(Holder.values) == 2\nmain()\n")
    compile_python(str(source), str(output), emit_llvm_only=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode="on")
    data = emit_owned_object(output.read_text(), "arm64-apple-darwin")
    assert data[:4] == b"\xcf\xfa\xed\xfe"


@pytest.mark.integration
@pytest.mark.parametrize("source_text,expected", PROGRAMS, ids=PROGRAM_IDS)
def test_native_class_initializer_error_context(tmp_path, pcc_runtime_archive,
                                               python_program_compiler, source_text, expected):
    source = tmp_path / "class_initializers.py"
    binary = tmp_path / "class_initializers"
    source.write_text(source_text)
    python_program_compiler(str(source), str(binary), backend="self",
                            libpython_mode="off", runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True,
                                timeout=30, env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout == expected, (backend, result.stdout, result.stderr)
