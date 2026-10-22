"""Native int.to_bytes binding, conversion order, width and signedness."""
from __future__ import annotations
import os
from pathlib import Path
import re
import subprocess
import sys
from pcc.frontends.python.pipeline import compile_python
from tests.owned_ir_validation import verify_ir_text


def _ir(tmp_path, body):
    source = tmp_path / "to_bytes_binding.py"
    source.write_text(body)
    target = tmp_path / "to_bytes_binding.ll"
    compile_python(str(source), str(target), backend="self", libpython_mode="off",
                   emit_llvm_only=True, python_library=True)
    text = target.read_text()
    verify_ir_text(text)
    return text


def test_bound_forms_and_errors_are_native(tmp_path):
    text = _ir(tmp_path, '''
def positional(value: int):
    return value.to_bytes(8, "little", signed=True)
def dynamic(value, signed):
    return value.to_bytes(byteorder="little", length=8, signed=signed)
def default(value: int):
    return value.to_bytes()
def default_length(value: int):
    return value.to_bytes(byteorder="big", signed=False)
def boolean():
    return True.to_bytes()
def duplicate(value: int):
    return value.to_bytes(1, length=1)
def unknown(value: int):
    return value.to_bytes(unknown=1)
def excess(value: int):
    return value.to_bytes(1, "big", False)
''')
    for name in ("positional", "dynamic", "default", "default_length", "boolean", "duplicate", "unknown", "excess"):
        body = re.search(r"^define[^\n]*@user_to_bytes_binding_" + name + r"\([^\n]*\)[^\n]*\{\n(.*?)^\}", text, re.M | re.S)
        assert body, name
        assert "strict.nolib.stub" not in body[1], name
        assert "@py_cpy_" not in body[1], name
        if name in ("duplicate", "unknown", "excess"):
            assert "@py_raise(" in body[1], name
        else:
            assert "@py_int_to_bytes_args(" in body[1], name


def test_runtime_encoder_exports_keep_unsigned_abi(tmp_path):
    source = Path(__file__).resolve().parents[2] / "pcc/runtime/py/py_int_convert.py"
    target = tmp_path / "py_int_convert.ll"
    compile_python(str(source), str(target), backend="self", libpython_mode="off",
                   emit_llvm_only=True, python_library=True)
    text = target.read_text()
    verify_ir_text(text)
    assert re.search(r"^define[^\n]*@py_int_to_bytes\(ptr[^,]*, i64[^,]*, ptr[^)]*\)", text, re.M)
    assert re.search(r"^define[^\n]*@py_int_to_bytes_args\(ptr[^,]*, ptr[^,]*, ptr[^,]*, ptr[^)]*\)", text, re.M)
    assert "strict.nolib.stub" not in text
    assert not re.search(r"\bcall\b[^\n]*@py_cpy_", text)


PROGRAM = r'''
trace = []
class Index:
    def __init__(self, value):
        self.value = value
    def __index__(self):
        trace.append("index")
        return self.value
class Signed:
    def __init__(self, value):
        self.value = value
    def __bool__(self):
        trace.append("truth")
        return self.value
class BadIndex:
    def __index__(self):
        return "wrong"
class BrokenSigned:
    def __bool__(self):
        raise RuntimeError("signed")
def mark(name, value):
    trace.append(name)
    return value
def dynamic(value, length, order, signed):
    return value.to_bytes(length=length, byteorder=order, signed=signed)
def main():
    print((65).to_bytes(), (65).to_bytes(byteorder="big"), True.to_bytes())
    print((0).to_bytes(0, signed=True))
    print(mark("receiver", -129).to_bytes(
        signed=mark("signed", Signed(True)),
        byteorder=mark("order", "big"),
        length=mark("length", Index(2))))
    print(trace)
    for width in (1, 2, 3, 4, 7, 8, 9, 16, 17, 33):
        bound = 2 ** (width * 8 - 1)
        for value in (-bound - 1, -bound, -bound + 1, -129, -128, -1, 0,
                      1, 127, 128, bound - 1, bound, 2 * bound - 1, 2 * bound):
            for order in ("big", "little"):
                for signed in (False, True):
                    try:
                        print(dynamic(value, width, order, signed))
                    except OverflowError:
                        print("overflow")
    caught = 0
    try:
        (1).to_bytes(1, length=mark("duplicate", 1))
    except TypeError:
        caught += 1
    try:
        (1).to_bytes(unknown=mark("unknown", 1))
    except TypeError:
        caught += 1
    try:
        (1).to_bytes(1, "big", True)
    except TypeError:
        caught += 1
    for length in (-1, -2 ** 63):
        try:
            dynamic(1, length, "big", True)
        except ValueError:
            caught += 1
    for length in (2 ** 63, -(2 ** 63) - 1, 2 ** 100, Index(2 ** 100), 2 ** 63 - 1):
        try:
            dynamic(1, length, "big", True)
        except OverflowError:
            caught += 1
    for length in (1.5, "1", BadIndex()):
        try:
            dynamic(1, length, "big", True)
        except TypeError:
            caught += 1
    try:
        dynamic(1, Index(1), 7, Signed(True))
    except TypeError:
        caught += 1
    try:
        dynamic(1, Index(1), "invalid", Signed(True))
    except ValueError:
        caught += 1
    try:
        dynamic(1, Index(1), "big", BrokenSigned())
    except RuntimeError:
        caught += 1
    for value in (-1, 1):
        try:
            dynamic(value, 0, "little", True)
        except OverflowError:
            caught += 1
    print(caught, trace)
main()
'''


def test_bindings_and_signed_widths_execute_all_collectors(
    tmp_path, pcc_diagnostic_runtime_archive, python_program_compiler, monkeypatch,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "to_bytes_binding.py"
    source.write_text(PROGRAM)
    oracle = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=30)
    assert oracle.returncode == 0, oracle.stderr
    target = tmp_path / "to_bytes_binding"
    python_program_compiler(str(source), str(target), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_diagnostic_runtime_archive))
    for collector in range(5):
        result = subprocess.run([str(target)], capture_output=True, text=True, timeout=30,
                                env=dict(os.environ, PCC_GC_BACKEND=str(collector)))
        assert result.returncode == 0, (collector, result.stdout, result.stderr)
        assert result.stdout == oracle.stdout, (collector, result.stdout, oracle.stdout)


CLEANUP_PROGRAM = r'''
import gc
destroyed = 0
anchor = 2 ** 127 - 1
class Index:
    def __init__(self, value):
        self.value = value
    def __index__(self):
        return self.value
    def __del__(self):
        global destroyed
        destroyed += 1
        gc.collect()
class Flag:
    def __bool__(self):
        return True
    def __del__(self):
        global destroyed
        destroyed += 1
        gc.collect()
class Broken:
    def __bool__(self):
        raise ValueError("truth")
    def __del__(self):
        global destroyed
        destroyed += 1
        gc.collect()
def fail():
    raise ValueError("argument")
def rebind():
    global anchor
    anchor = 7
    gc.collect()
    return 16
def main():
    print(anchor.to_bytes(rebind(), "big"))
    caught = 0
    for index in range(24):
        assert (-128).to_bytes(length=Index(1), signed=Flag()) == b"\x80"
        try:
            (1).to_bytes(length=Index(1), byteorder=fail())
        except ValueError:
            caught += 1
        try:
            (1).to_bytes(unknown=Index(1))
        except TypeError:
            caught += 1
        try:
            (1).to_bytes(length=Index(1), signed=Broken())
        except ValueError:
            caught += 1
        try:
            (1).to_bytes(length=Index("wrong"), signed=Flag())
        except TypeError:
            caught += 1
        try:
            (-1).to_bytes(length=Index(1), signed=False)
        except OverflowError:
            caught += 1
    gc.collect()
    print(caught, destroyed)
main()
'''


def test_argument_owners_and_result_survive_callbacks_all_collectors(
    tmp_path, pcc_diagnostic_runtime_archive, python_program_compiler, monkeypatch,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "to_bytes_cleanup.py"
    source.write_text(CLEANUP_PROGRAM)
    oracle = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=30)
    assert oracle.returncode == 0, oracle.stderr
    target = tmp_path / "to_bytes_cleanup"
    python_program_compiler(str(source), str(target), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_diagnostic_runtime_archive))
    for collector in range(5):
        result = subprocess.run([str(target)], capture_output=True, text=True, timeout=30,
                                env=dict(os.environ, PCC_GC_BACKEND=str(collector)))
        assert result.returncode == 0, (collector, result.stdout, result.stderr)
        assert result.stdout == oracle.stdout, (collector, result.stdout, oracle.stdout)
