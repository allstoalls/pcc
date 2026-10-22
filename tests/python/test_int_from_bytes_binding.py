"""Native int.from_bytes argument binding and arbitrary-width signed conversion."""
from __future__ import annotations

import os
import re
import subprocess
import sys

from pcc.frontends.python.pipeline import compile_python
from tests.owned_ir_validation import verify_ir_text


def _compile_ir(tmp_path, source):
    path = tmp_path / "from_bytes_binding.py"
    path.write_text(source)
    output = tmp_path / "from_bytes_binding.ll"
    compile_python(str(path), str(output), backend="self", libpython_mode="off",
                   python_library=True, emit_llvm_only=True)
    text = output.read_text()
    verify_ir_text(text)
    return text


def _body(text, name):
    match = re.search(r"^define[^\n]*@user_from_bytes_binding_" + name +
                      r"\([^\n]*\)[^\n]*\{\n(.*?)^\}", text, re.M | re.S)
    assert match, name
    return match[1]


def test_keyword_and_signed_calls_keep_native_bodies(tmp_path):
    text = _compile_ir(tmp_path, '''
def positional(payload):
    return int.from_bytes(payload, "little", signed=False)
def keywords(payload):
    return int.from_bytes(bytes=payload, byteorder="little", signed=False)
def default_order(payload):
    return int.from_bytes(payload)
def signed_constant(payload):
    return int.from_bytes(payload, byteorder="little", signed=True)
def signed_dynamic(payload, sign):
    return int.from_bytes(byteorder="little", signed=sign, bytes=payload)
''')
    for name in ("positional", "keywords", "default_order", "signed_constant", "signed_dynamic"):
        body = _body(text, name)
        assert "strict.nolib.stub" not in body, name
        assert "@py_cpy_" not in body, name
        helper = "py_int_from_bytes_signed" if name.startswith("signed_") else "py_int_from_bytes"
        assert "@" + helper + "(" in body, name
    assert "@py_obj_truthy(" in _body(text, "signed_dynamic")


def test_binding_errors_keep_native_bodies(tmp_path):
    text = _compile_ir(tmp_path, '''
def missing():
    return int.from_bytes()
def excess(payload):
    return int.from_bytes(payload, "little", True)
def duplicate(payload):
    return int.from_bytes(payload, bytes=payload)
def unknown(payload):
    return int.from_bytes(payload, surprise=True)
''')
    for name in ("missing", "excess", "duplicate", "unknown"):
        body = _body(text, name)
        assert "strict.nolib.stub" not in body, name
        assert "@py_cpy_" not in body, name
        assert "@py_exc_new(" in body and "@py_raise(" in body, name


PROGRAM = r'''
trace = []
def mark(name, value):
    trace.append(name)
    return value
class Signed:
    def __init__(self, value):
        self.value = value
    def __bool__(self):
        trace.append("truth")
        return self.value
class Broken:
    def __bool__(self):
        raise ValueError("signed truth")
def main():
    payload = b"\x80\xff"
    print(int.from_bytes(payload, "little", signed=False))
    print(int.from_bytes(payload, byteorder="big", signed=False))
    print(int.from_bytes(bytes=payload))
    print(int.from_bytes(payload, signed=True))
    print(int.from_bytes(byteorder=mark("order", "little"),
                         signed=mark("signed", Signed(True)),
                         bytes=mark("bytes", payload)))
    print(trace)
    for order in ("big", "little"):
        for value in (b"", b"\x00", b"\x7f", b"\x80", b"\xff", b"\x00\x80",
                      b"\x80\x00", b"\xff\x00\x00", b"\x00\x00\x80",
                      b"\xff" * 8, b"\x80" + b"\x00" * 15, b"\xff" * 17,
                      b"\x01" + b"\x00" * 32):
            for signed in (False, True):
                print(int.from_bytes(value, order, signed=signed))
    print(int.from_bytes(bytearray(b"\x80"), signed=True))
    print(int.from_bytes(memoryview(bytearray(b"\xff\x80")), "big", signed=True))
    caught = 0
    try:
        int.from_bytes()
    except TypeError:
        caught += 1
    try:
        int.from_bytes(b"x", "big", True)
    except TypeError:
        caught += 1
    try:
        int.from_bytes(mark("arg", b"x"), bytes=mark("duplicate", b"y"))
    except TypeError:
        caught += 1
    try:
        int.from_bytes(b"x", surprise=mark("unknown", True))
    except TypeError:
        caught += 1
    try:
        int.from_bytes(b"x", "big", byteorder="little")
    except TypeError:
        caught += 1
    try:
        int.from_bytes(b"x", "invalid", signed=True)
    except ValueError:
        caught += 1
    try:
        int.from_bytes(b"x", signed=Broken())
    except ValueError:
        caught += 1
    try:
        int.from_bytes(b"x", 7)
    except TypeError:
        caught += 1
    try:
        int.from_bytes(b"x", None)
    except TypeError:
        caught += 1
    print(caught)
    print(trace)
main()
'''


def test_binding_and_signed_conversion_execute_all_collectors(
    tmp_path, pcc_diagnostic_runtime_archive, python_program_compiler, monkeypatch,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    path = tmp_path / "from_bytes_binding.py"
    path.write_text(PROGRAM)
    oracle = subprocess.run([sys.executable, str(path)], capture_output=True, text=True, timeout=15)
    assert oracle.returncode == 0, oracle.stderr
    binary = tmp_path / "from_bytes_binding"
    python_program_compiler(str(path), str(binary), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_diagnostic_runtime_archive))
    for backend in range(5):
        actual = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
                                capture_output=True, text=True, timeout=30)
        assert actual.returncode == 0, (backend, actual.stdout, actual.stderr)
        assert actual.stdout == oracle.stdout, (backend, actual.stdout, oracle.stdout)


def test_signed_runtime_helper_is_owned_and_preserves_unsigned_abi(tmp_path):
    from pathlib import Path
    source = Path(__file__).resolve().parents[2] / "pcc/runtime/py/py_int_convert.py"
    output = tmp_path / "py_int_convert.ll"
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   python_library=True, emit_llvm_only=True)
    text = output.read_text()
    verify_ir_text(text)
    assert re.search(r"^define[^\n]*@py_int_from_bytes\(", text, re.M)
    assert re.search(r"^define[^\n]*@py_int_from_bytes_signed\(", text, re.M)
    assert "strict.nolib.stub" not in text
    assert not re.search(r"\bcall\b[^\n]*@py_cpy_", text)


CLEANUP_PROGRAM = '''
import gc
destroyed = 0
anchor = bytearray(b"\\x80")
class Token:
    def __del__(self):
        global destroyed
        destroyed += 1
    def __bool__(self):
        raise ValueError("truth")
def fail():
    raise ValueError("argument")
def rebind():
    global anchor
    anchor = bytearray(b"changed")
    gc.collect()
    return True
def main():
    caught = 0
    for index in range(24):
        try:
            int.from_bytes(bytes=Token(), signed=fail())
        except ValueError:
            caught += 1
        try:
            int.from_bytes(byteorder=Token(), bytes=fail())
        except ValueError:
            caught += 1
        try:
            int.from_bytes(b"x", signed=Token())
        except ValueError:
            caught += 1
        try:
            int.from_bytes(Token(), signed=False)
        except TypeError:
            caught += 1
        try:
            int.from_bytes(Token(), unknown=Token())
        except TypeError:
            caught += 1
    print(int.from_bytes(anchor, signed=rebind()))
    gc.collect()
    print(caught, destroyed)
main()
'''


def test_keyword_argument_errors_release_owners(
    tmp_path, pcc_diagnostic_runtime_archive, python_program_compiler, monkeypatch,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    path = tmp_path / "from_bytes_cleanup.py"
    path.write_text(CLEANUP_PROGRAM)
    oracle = subprocess.run([sys.executable, str(path)], capture_output=True, text=True, timeout=15)
    assert oracle.returncode == 0, oracle.stderr
    binary = tmp_path / "from_bytes_cleanup"
    python_program_compiler(str(path), str(binary), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_diagnostic_runtime_archive))
    for backend in range(5):
        actual = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
                                capture_output=True, text=True, timeout=30)
        assert actual.returncode == 0, (backend, actual.stdout, actual.stderr)
        assert actual.stdout == oracle.stdout, (backend, actual.stdout, oracle.stdout)
