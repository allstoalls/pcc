"""Counted int(bytes/bytearray) parsing and native argument ownership."""
from __future__ import annotations

import ast
import operator
import os
from pathlib import Path
import re
import struct
import subprocess
import sys

import pytest

from pcc.runtime.py import py_abi_constants as abi


ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "pcc/runtime/py/py_int_parse.py"


class _Pointer:
    def __init__(self, size=0, *, owner=None, offset=0):
        self.owner = owner if owner is not None else bytearray(size)
        self.offset = offset
        self.fields = {} if owner is None else None

    def __eq__(self, other):
        return isinstance(other, _Pointer) and self.owner is other.owner and self.offset == other.offset


class _Input:
    def __init__(self, value):
        self.value = value
        self.tag = (abi.PY_TYPE_MEMORYVIEW if isinstance(value, memoryview) else
                    abi.PY_TYPE_BYTEARRAY if isinstance(value, bytearray) else abi.PY_TYPE_BYTES)
        self.base = _Input(value.obj) if isinstance(value, memoryview) else None
        self.alive = True


class _IntTextModel:
    """Execute the actual port functions with counted, checked raw buffers."""
    def __init__(self):
        self.error = None
        self.locked = 0
        self.roots = []
        self.allocations = []
        self.index_hook = None
        self.arithmetic_hook = None
        self.boxed_none = object()
        self.ns = {name: getattr(abi, name) for name in dir(abi) if name.startswith("PY")}
        self.ns.update({
            "c_ptr": object, "c_abi_export": lambda _name: lambda fn: fn,
            "null": lambda: None, "ptr_is_null": lambda value: value is None,
            "malloc": self.allocate, "free": self.free, "stack_alloc": _Pointer,
            "cstr": lambda value: _Pointer(owner=bytearray(value.encode() + b"\0")),
            "strlen": self.strlen, "ptr_add": self.add,
            "load_i8": self.byte, "store_i8": self.set_byte,
            "load_i32": lambda p, o: self.integer(p, o, "i"),
            "store_i32": lambda p, o, v: self.set_integer(p, o, v, "i"),
            "load_i64": lambda p, o: self.integer(p, o, "q"),
            "store_i64": lambda p, o, v: self.set_integer(p, o, v, "q"),
            "store_ptr": self.set_pointer,
            "py_obj_type_tag": self.tag,
            "pcc_gc_load_ptr": lambda _owner, slot: self.get_pointer(slot),
            "pcc_py_gc_minor_graph_lock": self.lock,
            "pcc_py_gc_minor_graph_unlock": self.unlock,
            "py_int_from_i64": lambda value: value,
            "py_int_mul": self.multiply, "py_int_add": lambda a, b: a + b,
            "py_incref": lambda _obj: None, "py_decref": lambda _obj: None,
            "py_obj_truthy": bool,
            "py_int_to_i64": self.to_i64, "py_obj_index": self.index,
            "py_exc_new": self.exception, "py_raise_owned": self.raise_owned,
            "py_err_occurred": lambda: int(self.error is not None),
            "_int_open_root": self.open_root, "_int_close_root": self.close_root,
            "_int_save_result": self.save_result, "_int_take_result": self.take_result,
        })
        tree = ast.parse(RUNTIME.read_text(), filename=str(RUNTIME))
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(RUNTIME), "exec"), self.ns)

    def allocate(self, count):
        result = _Pointer(count)
        self.allocations.append(result)
        return result

    def free(self, pointer):
        assert pointer in self.allocations
        self.allocations.remove(pointer)

    def add(self, pointer, offset):
        if isinstance(pointer, _Pointer):
            result = _Pointer(owner=pointer.owner, offset=pointer.offset + offset)
            result.fields = pointer.fields
            return result
        return _Pointer(owner=pointer, offset=offset)

    def byte(self, pointer, offset):
        if isinstance(pointer, _Input):
            assert self.locked and pointer.alive
            return pointer.value[offset - abi.PYBYTESOBJECT_DATA_OFFSET]
        return pointer.owner[pointer.offset + offset]

    def set_byte(self, pointer, offset, value):
        pointer.owner[pointer.offset + offset] = value & 255

    def integer(self, pointer, offset, fmt):
        if isinstance(pointer, _Input):
            assert self.locked and pointer.alive
            assert offset == abi.PYBYTESOBJECT_BYTE_LEN_OFFSET
            return len(pointer.value)
        return struct.unpack_from("<" + fmt, pointer.owner, pointer.offset + offset)[0]

    def set_integer(self, pointer, offset, value, fmt):
        struct.pack_into("<" + fmt, pointer.owner, pointer.offset + offset, value)

    def set_pointer(self, pointer, offset, value):
        pointer.fields[pointer.offset + offset] = value

    def get_pointer(self, pointer):
        if isinstance(pointer.owner, _Input):
            assert self.locked and pointer.owner.alive
            assert pointer.offset == abi.PYMEMORYVIEWOBJECT_BASE_OFFSET
            return pointer.owner.base
        return pointer.fields.get(pointer.offset)

    def strlen(self, pointer):
        return pointer.owner.index(0, pointer.offset) - pointer.offset

    def tag(self, value):
        if isinstance(value, _Input):
            assert value.alive
            return value.tag
        if isinstance(value, bool):
            return abi.PY_TYPE_BOOL
        if isinstance(value, int):
            return abi.PY_TYPE_INT
        return abi.PY_TYPE_INSTANCE

    def lock(self):
        self.locked += 1

    def unlock(self):
        self.locked -= 1
        assert self.locked == 0

    def multiply(self, left, right):
        assert not self.locked, "managed integer allocation under graph lock"
        if self.arithmetic_hook:
            self.arithmetic_hook()
        return left * right

    def exception(self, kind, message):
        assert not self.locked
        text = bytes(message.owner[message.offset:]).split(b"\0", 1)[0].decode("utf-8")
        return {2: ValueError, 3: TypeError, 7: MemoryError}[kind](text)

    def raise_owned(self, exception):
        self.error = exception

    def index(self, value):
        if self.index_hook:
            self.index_hook(self)
        try:
            if value is self.boxed_none:
                raise TypeError("'NoneType' object cannot be interpreted as an integer")
            return operator.index(value)
        except Exception as error:
            self.error = error
            return None

    def to_i64(self, value, overflow):
        bad = not -(2 ** 63) <= value < 2 ** 63
        self.set_integer(overflow, 0, int(bad), "i")
        return 0 if bad else value

    def open_root(self, slot, value):
        self.set_pointer(slot, 0, value)
        self.roots.append(slot)
        return slot

    def close_root(self, slot, _handle):
        self.roots.remove(slot)
        self.set_pointer(slot, 0, None)

    def save_result(self, state, result):
        self.set_pointer(state, 0, result)
        return state

    def take_result(self, state, _handle):
        return self.get_pointer(state)

    def convert(self, value, *base):
        argument = (self.boxed_none if base[0] is None else base[0]) if base else None
        result = self.ns["py_obj_as_int_object_args"](_Input(value), argument)
        assert not self.allocations and not self.roots and not self.locked
        if self.error:
            raise self.error
        return result


CASES = [
    (b"20", ()), (b"/123"[1:], ()), (b"\t\n\v\f\r +123 \v", ()),
    (b"-123", ()), (b"1_234_567", ()), (b"0x_Ff", (0,)),
    (b"-0b_101", (0,)), (b"+0o_77", (8,)), (b"000_00", (0,)),
    (b"010", (0,)), (b"08", (0,)), (b"0_1", (0,)),
    (b"0x", (0,)), (b"0x__1", (0,)), (b"_1", ()), (b"1__2", ()),
    (b"1_", ()), (b"", ()), (b"+", ()), (b"1\x002", ()),
    (b"\x001", ()), (b"12\x00", ()), (b"1 2", ()), (b"\xff1", ()),
    (b"0xff", (10,)), (b"123", (1,)), (b"123", (37,)),
    (b"123", (2 ** 100,)), (b"123", (-(2 ** 100),)),
    (b"123", (1.5,)), (b"123", (None,)),
    (b"1234567890123456789012345678901234567890", ()),
    (b"-123_456_789_012_345_678_901_234_567_890", ()),
    (b"-92233720368547758080", ()), (b"-0x80000000000000000", (0,)),
    (b"0x1234_5678_90ab_cdef_ffff_eeee_dddd_cccc", (0,)),
]


@pytest.mark.parametrize("factory", [bytes, bytearray])
@pytest.mark.parametrize("payload,base", CASES)
def test_counted_bytes_text_model_matches_python(payload, base, factory):
    value = factory(payload)
    try:
        expected = int(value, *base)
    except Exception as expected_error:
        with pytest.raises(type(expected_error)) as caught:
            _IntTextModel().convert(value, *base)
        # __index__ diagnostics remain owned by the shared protocol helper.
        assert str(caught.value) == str(expected_error)
    else:
        assert _IntTextModel().convert(value, *base) == expected


@pytest.mark.parametrize("factory", [bytes, bytearray])
def test_memoryview_omitted_and_explicit_base_model(factory):
    view = memoryview(factory(b"123"))
    assert _IntTextModel().convert(view) == int(view)
    with pytest.raises(TypeError, match="explicit base"):
        _IntTextModel().convert(view, 10)
    invalid = memoryview(factory(b"12\0"))
    with pytest.raises(ValueError) as expected:
        int(invalid)
    with pytest.raises(ValueError) as actual:
        _IntTextModel().convert(invalid)
    assert str(actual.value) == str(expected.value)


def test_base_callback_reloads_root_and_snapshot_outlives_source_model():
    class Base:
        def __index__(self):
            return 10
    model = _IntTextModel()
    payload = bytearray(b"123456789012345678901234567890")
    def relocate_inputs(current):
        for slot in current.roots:
            original = current.get_pointer(slot)
            if isinstance(original, _Input):
                replacement = _Input(original.value)
                original.alive = False
                current.set_pointer(slot, 0, replacement)
    model.index_hook = relocate_inputs
    model.arithmetic_hook = lambda: payload.__setitem__(slice(None), b"9" * len(payload))
    assert model.convert(payload, Base()) == 123456789012345678901234567890
    class BrokenBase:
        def __index__(self):
            raise RuntimeError("base callback")
    failed = _IntTextModel()
    failed.index_hook = relocate_inputs
    with pytest.raises(RuntimeError, match="base callback"):
        failed.convert(bytearray(b"123"), BrokenBase())
    assert not failed.roots and not failed.allocations


def test_typed_and_dynamic_int_text_ir_uses_counted_roots(tmp_path):
    from pcc.frontends.python.pipeline import compile_python
    from tests.owned_ir_validation import verify_ir_text
    source = tmp_path / "int_text.py"
    source.write_text('''def typed_bytes(value: bytes) -> object:
    return int(value)
def typed_array(value: bytearray) -> object:
    return int(value)
def typed_view(value: memoryview) -> object:
    return int(value)
def dynamic(value: object, base: object) -> object:
    return int(value, base)
def scalar(value: bytes) -> int:
    return int(value)
''')
    target = tmp_path / "int_text.ll"
    compile_python(str(source), str(target), backend="self", libpython_mode="off",
                   python_library=True, emit_llvm_only=True)
    text = target.read_text()
    verify_ir_text(text)
    for name in ("typed_bytes", "typed_array", "typed_view", "dynamic", "scalar"):
        body = re.search(r"^define[^\n]*@user_int_text_" + name + r"\([^\n]*\)[^\n]*\{\n(.*?)^\}", text, re.M | re.S)
        assert body, name
        assert "@py_obj_as_int_object_args(" in body[1], name
        assert "@pcc_gc_foreign_lease_acquire(" in body[1], name
        assert "@pcc_gc_foreign_lease_release(" in body[1], name
        assert "strict.nolib.stub" not in body[1] and "@py_cpy_" not in body[1], name


def test_int_text_runtime_ir_preserves_existing_abi(tmp_path):
    from pcc.frontends.python.pipeline import compile_python
    from tests.owned_ir_validation import verify_ir_text
    target = tmp_path / "py_int_parse.ll"
    compile_python(str(RUNTIME), str(target), backend="self", libpython_mode="off",
                   python_library=True, emit_llvm_only=True)
    text = target.read_text()
    verify_ir_text(text)
    assert re.search(r"^define[^\n]*@py_obj_as_int_object\(", text, re.M)
    assert re.search(r"^define[^\n]*@py_obj_as_int_object_args\(", text, re.M)
    assert "strict.nolib.stub" not in text
    assert not re.search(r"\bcall\b[^\n]*@py_cpy_", text)


PROGRAM = r'''
import gc
trace = []
destroyed = 0
anchor = bytearray(b"123456789012345678901234567890")
class Base:
    def __init__(self, value):
        self.value = value
    def __index__(self):
        trace.append("index")
        gc.collect()
        return self.value
    def __del__(self):
        global destroyed
        destroyed += 1
        gc.collect()
class BrokenBase:
    def __index__(self):
        gc.collect()
        raise ValueError("base callback")
    def __del__(self):
        global destroyed
        destroyed += 1
        gc.collect()
def mark(name, value):
    trace.append(name)
    return value
def rebind():
    global anchor
    anchor = bytearray(b"different")
    gc.collect()
    return Base(10)
def dynamic(value, base):
    return int(value, base)
def main():
    print(int(b"20"), int(bytearray(b" +12 ")), int(memoryview(b"123")))
    print(int(b"1234567890123456789012345678901234567890"))
    print(dynamic(b"0x_ffff_ffff_ffff_ffff_ffff", 0))
    print(int(anchor, rebind()))
    print(int(mark("value", b"32"), mark("base", Base(10))))
    print(trace)
    caught = 0
    for value in (b"1\0", bytearray(b"1\0"), b"_1", b"1__2", b"1_"):
        try:
            int(value)
        except ValueError:
            caught += 1
    try:
        int(memoryview(b"123"), 10)
    except TypeError:
        caught += 1
    for index in range(12):
        try:
            int(bytearray(b"123"), BrokenBase())
        except ValueError:
            caught += 1
    gc.collect()
    print(caught, destroyed)
main()
'''


def test_int_text_executes_all_collectors(tmp_path, pcc_diagnostic_runtime_archive,
                                        python_program_compiler, monkeypatch):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "int_text_program.py"
    source.write_text(PROGRAM)
    oracle = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=15)
    assert oracle.returncode == 0, oracle.stderr
    binary = tmp_path / "int_text_program"
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_diagnostic_runtime_archive))
    for collector in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30,
                                env=dict(os.environ, PCC_GC_BACKEND=str(collector)))
        assert result.returncode == 0, (collector, result.stdout, result.stderr)
        assert result.stdout == oracle.stdout, (collector, result.stdout, oracle.stdout)
