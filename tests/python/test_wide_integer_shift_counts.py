"""Wide shift counts execute the production bodies without native provisioning.

The model checks arithmetic and ownership calls, not native GC safety. The
separate integration node requires a source-matched pcc-Python runtime archive.
"""
from __future__ import annotations

import ast
import operator
import os
from pathlib import Path
import re
import subprocess

import pytest

from pcc.runtime.py import py_abi_constants as abi


ROOT = Path(__file__).resolve().parents[2]
OPS = ROOT / "pcc/runtime/py/py_int_ops.py"
SHIFT = ROOT / "pcc/runtime/py/py_int_shift.py"
HUGE = 1 << 100
MAX_I64 = (1 << 63) - 1


class _Heap:
    def __init__(self, value=0, digits=None, boolean=False):
        self.fields = {
            abi.PYOBJECTHEADER_TYPE_TAG_OFFSET: abi.PY_TYPE_BOOL if boolean else abi.PY_TYPE_INT,
            abi.PYINTOBJECT_SIGN_OFFSET: (value > 0) - (value < 0),
            abi.PYINTOBJECT_NDIGITS_OFFSET: (abs(value).bit_length() + 31) // 32,
        }
        if digits is not None:
            self.fields[abi.PYINTOBJECT_NDIGITS_OFFSET] = digits
        for i in range((abs(value).bit_length() + 31) // 32):
            self.fields[abi.PYINTOBJECT_DIGITS_OFFSET + i * 4] = (abs(value) >> (32 * i)) & 0xffffffff
        self.references = 1
        self.freed = False


class _ShiftMemory:
    def __init__(self, fail_allocation=None):
        self.error = None
        self.fail_allocation = fail_allocation
        self.allocations = []
        self.copies = []
        self.true, self.false = _Heap(1, boolean=True), _Heap(0, boolean=True)
        common = dict(vars(abi))
        common.update({
            "c_abi_export": lambda name: lambda fn: fn,
            "null": lambda: None, "ptr_is_null": lambda p: p is None,
            "cstr": lambda s: s, "ptr_eq": lambda a, b: a is b,
            "global_load_ptr": lambda name: self.true if name == "py_True" else self.false,
            "is_tagged_int": lambda p: type(p) is int,
            "untag_int": lambda p: p,
            "load_i32": self.load, "store_i32": self.store,
            "logical_shift_left_i64": lambda n, shift: n << shift,
            "py_int_from_i64": self.obj,
            "py_int_bit_length": lambda p: abs(self.value(p)).bit_length(),
            "py_bigint_alloc": self.allocate,
            "py_bigint_from_i64": lambda n: _Heap(n),
            "py_bigint_from_any": self.copy,
            "py_bigint_to_pyobject": self.wrap,
            "py_incref": self.incref, "free": self.free,
            "py_exc_new": lambda kind, message: (kind, message),
            "py_raise_owned": self.raise_error,
            "py_err_occurred": lambda: int(self.error is not None),
        })
        shift = self.load_bodies(SHIFT, dict(common))
        common.update({name: shift[name] for name in ("py_bigint_shl", "py_bigint_shr")})
        self.ns = self.load_bodies(OPS, common)
        self.shift = shift

    @staticmethod
    def load_bodies(path, namespace):
        tree = ast.parse(path.read_text())
        functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"), namespace)
        return namespace

    def obj(self, value):
        if value is True:
            return self.true
        if value is False:
            return self.false
        return value if -(1 << 62) <= value < (1 << 62) else _Heap(value)

    def value(self, obj):
        if type(obj) is int or obj is None:
            return obj
        assert not obj.freed
        if obj is self.true or obj is self.false:
            return int(obj is self.true)
        count = obj.fields[abi.PYINTOBJECT_NDIGITS_OFFSET]
        return obj.fields[abi.PYINTOBJECT_SIGN_OFFSET] * sum(
            (obj.fields.get(abi.PYINTOBJECT_DIGITS_OFFSET + i * 4, 0) & 0xffffffff) << (i * 32)
            for i in range(count)
        )

    @staticmethod
    def load(obj, offset):
        assert not obj.freed
        # bool has no integer payload; detecting such a read is important.
        if obj.fields[abi.PYOBJECTHEADER_TYPE_TAG_OFFSET] == abi.PY_TYPE_BOOL:
            assert offset == abi.PYOBJECTHEADER_TYPE_TAG_OFFSET
        value = obj.fields.get(offset, 0)
        return value - (1 << 32) if value >= (1 << 31) else value

    @staticmethod
    def store(obj, offset, value):
        assert not obj.freed
        obj.fields[offset] = value & 0xffffffff if value >= (1 << 31) else value

    def allocate(self, digits):
        assert 0 <= digits <= 1000, "unbounded shift reached bigint allocation"
        self.allocations.append(digits)
        return None if self.fail_allocation == "allocate" else _Heap(digits=digits)

    def copy(self, obj):
        self.copies.append(obj)
        return None if self.fail_allocation == "copy" else _Heap(self.value(obj))

    def wrap(self, obj):
        value = self.value(obj)
        self.free(obj)
        if self.fail_allocation == "wrap":
            return None
        return self.obj(value)

    @staticmethod
    def free(obj):
        if obj is not None:
            assert not obj.freed
            obj.freed = True

    @staticmethod
    def incref(obj):
        if type(obj) is not int:
            obj.references += 1

    def raise_error(self, error):
        assert self.error is None, "shift overwrote an existing exception"
        self.error = error


def _outcome(function, a, b):
    try:
        return ("value", function(a, b))
    except (ValueError, OverflowError) as exc:
        return (type(exc).__name__, str(exc))


@pytest.mark.parametrize("op", ["shr", "shl"])
@pytest.mark.parametrize("count", [HUGE, MAX_I64 + 1, MAX_I64, MAX_I64 - 36,
                                   -HUGE, -MAX_I64 - 2, -MAX_I64 - 1])
@pytest.mark.parametrize("value", [1, -1, 0, HUGE, -HUGE, True, False])
def test_wide_shift_matches_qualified_python_body_model(op, count, value):
    memory = _ShiftMemory()
    left, right = memory.obj(value), memory.obj(count)
    before = [(obj, obj.fields.copy(), obj.references) for obj in (left, right) if isinstance(obj, _Heap)]
    result = memory.ns["py_int_" + op](left, right)
    expected = _outcome(operator.rshift if op == "shr" else operator.lshift, value, count)
    if memory.error:
        kind, message = memory.error
        actual = ({2: "ValueError", 15: "OverflowError", 19: "MemoryError"}[kind], message)
        assert result is None
    else:
        actual = ("value", memory.value(result))
    assert actual == expected
    # Huge-count decisions require neither copying operands nor big buffers.
    assert memory.allocations == [] and memory.copies == []
    for obj, fields, references in before:
        assert not obj.freed and obj.fields == fields and obj.references == references


@pytest.mark.parametrize("op", ["shr", "shl"])
@pytest.mark.parametrize("count", [False, True, 0, 1, 31, 63, 100])
@pytest.mark.parametrize("value", [0, 1, -1, HUGE + 3, -HUGE - 3, True, False])
def test_small_shift_and_bool_regression_body_model(op, count, value):
    memory = _ShiftMemory()
    result = memory.ns["py_int_" + op](memory.obj(value), memory.obj(count))
    assert memory.error is None
    assert memory.value(result) == (value >> count if op == "shr" else value << count)
    assert type(result) is int or result.fields[abi.PYOBJECTHEADER_TYPE_TAG_OFFSET] == abi.PY_TYPE_INT


@pytest.mark.parametrize("op", ["shl", "shr"])
def test_zero_shift_keeps_existing_heap_ownership(op):
    memory = _ShiftMemory()
    value = memory.obj(HUGE)
    result = memory.ns["py_int_" + op](value, 0)
    assert result is value and value.references == 2 and not value.freed


@pytest.mark.parametrize("digits,bits", [(1, MAX_I64), (2, (2147483647 - 1) * 32),
                                        (1, (2147483647 - 1) * 32 + 1)])
def test_bigint_left_shift_rejects_digit_count_overflow_before_allocation(digits, bits):
    memory = _ShiftMemory()
    value = _Heap(1, digits=digits)
    result = memory.shift["py_bigint_shl"](value, bits)
    assert result is None and memory.error == (19, "")
    assert memory.allocations == []


def test_public_left_shift_reports_storage_capacity_as_memory_error():
    memory = _ShiftMemory()
    result = memory.ns["py_int_shl"](1, (1 << 36))
    assert result is None and memory.error == (19, "")
    assert memory.allocations == []


@pytest.mark.parametrize("value", [1, -1, HUGE, -HUGE])
def test_python_enormous_size_boundary_is_separate_from_pcc_capacity(value):
    # CPython 3.15 long_lshift1/long_alloc, with the qualified oracle's
    # 30-bit digits. Only execute the provably overflowing side in CPython;
    # the other side would request exbibytes and is source-derived instead.
    oldsize = (abs(value).bit_length() + 29) // 30
    last_count_with_fitting_python_layout = ((MAX_I64 - 1) // 30 - oldsize) * 30
    for count, expected in ((last_count_with_fitting_python_layout, (19, "")),
                            (last_count_with_fitting_python_layout + 1,
                             (15, "too many digits in integer"))):
        memory = _ShiftMemory()
        result = memory.ns["py_int_shl"](memory.obj(value), memory.obj(count))
        assert result is None and memory.error == expected
        assert memory.allocations == [] and memory.copies == []
    assert _outcome(operator.lshift, value, last_count_with_fitting_python_layout + 1) == (
        "OverflowError", "too many digits in integer")


@pytest.mark.parametrize("bits", [(2147483647 - 1) * 32,
                                  (2147483647 - 2) * 32 + 1])
def test_bigint_shift_capacity_guard_does_not_reject_fitting_layout(bits):
    memory = _ShiftMemory()
    requested = []

    def refuse_allocation(digits):
        requested.append(digits)
        return None

    memory.shift["py_bigint_alloc"] = refuse_allocation
    result = memory.shift["py_bigint_shl"](_Heap(1), bits)
    assert result is None and memory.error is None
    assert requested == [2147483647]


@pytest.mark.parametrize("op", ["shl", "shr"])
@pytest.mark.parametrize("phase", ["copy", "allocate", "wrap"])
def test_shift_allocation_failure_raises_memory_error(op, phase):
    memory = _ShiftMemory(fail_allocation=phase)
    result = memory.ns["py_int_" + op](memory.obj(HUGE * HUGE), 65)
    assert result is None and memory.error == (19, "")


@pytest.mark.parametrize("op", ["shl", "shr"])
def test_heap_zero_with_huge_count_needs_no_copy_or_buffer(op):
    memory = _ShiftMemory(fail_allocation="copy")
    zero = _Heap(0)
    result = memory.ns["py_int_" + op](zero, memory.obj(HUGE))
    assert result == 0 and memory.error is None
    assert memory.allocations == [] and memory.copies == []
    assert zero.references == 1 and not zero.freed


@pytest.mark.parametrize("path", [OPS, SHIFT])
def test_shift_runtime_owned_library_ir(path, tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python
    from tests.owned_ir_validation import verify_ir_text

    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    output = tmp_path / (path.stem + ".ll")
    compile_python(str(path), str(output), emit_llvm_only=True, python_library=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode="on",
                   target_triple="x86_64-unknown-linux-gnu")
    text = output.read_text()
    verify_ir_text(text)
    assert "@py_raise_owned" in text


PROGRAM = '''def left(value: int, count: int):
    return value << count
def right(value: int, count: int):
    return value >> count
def inplace_left(value: int, count: int):
    value <<= count
    return value
def inplace_right(value: int, count: int):
    value >>= count
    return value
def check(value, count):
    original_value = value
    original_count = count
    assert right(value, count) == (0 if value >= 0 else -1)
    assert inplace_right(value, count) == (0 if value >= 0 else -1)
    if value == 0:
        assert left(value, count) == 0
        assert inplace_left(value, count) == 0
    else:
        try:
            left(value, count)
        except OverflowError as error:
            assert str(error) == 'too many digits in integer'
        else:
            assert False
        try:
            inplace_left(value, count)
        except OverflowError as error:
            assert str(error) == 'too many digits in integer'
        else:
            assert False
    negative = -count
    try:
        left(value, negative)
    except ValueError as error:
        assert str(error) == 'negative shift count'
    else:
        assert False
    try:
        right(value, negative)
    except ValueError as error:
        assert str(error) == 'negative shift count'
    else:
        assert False
    assert value is original_value
    assert count is original_count
def main():
    huge = 1267650600228229401496703205376
    for count in [huge, 9223372036854775808, 9223372036854775807, 9223372036854775771]:
        for value in [1, -1, 0, huge, -huge, True, False]:
            check(value, count)
    assert left(huge, 31) == 2722258935367507707706996859454145691648
    assert right(-huge - 3, 31) == -590295810358705651713
    assert left(huge, False) == huge
    assert right(huge, True) == huge // 2
    assert type(left(True, 0)) is int
    assert type(right(False, 0)) is int
    print('WIDE_SHIFT_COUNTS_OK')
main()
'''


def test_wide_shift_fixture_qualified_python_oracle(tmp_path):
    import sys
    source = tmp_path / "wide_shifts.py"
    source.write_text(PROGRAM)
    ran = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=15)
    assert ran.returncode == 0, (ran.stdout, ran.stderr)
    assert ran.stdout == "WIDE_SHIFT_COUNTS_OK\n" and ran.stderr == ""


def test_wide_shift_fixture_owned_ir_keeps_count_boxed(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python
    from tests.owned_ir_validation import verify_ir_text

    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "wide_shifts.py"
    output = source.with_suffix(".ll")
    source.write_text(PROGRAM)
    compile_python(str(source), str(output), emit_llvm_only=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode="on",
                   target_triple="x86_64-unknown-linux-gnu")
    text = output.read_text()
    verify_ir_text(text)
    for name, operation in (("left", "lshift"), ("right", "rshift"),
                            ("inplace_left", "lshift"), ("inplace_right", "rshift")):
        body = re.search(r"(?ms)^define [^\n]*@user_wide_shifts_" + name + r"\([^\n]*\n.*?^\}", text)
        assert body is not None
        body = body.group(0)
        assert body.splitlines()[0].count("ptr %") >= 2
        assert "@py_int_to_i64_lane" not in body
        assert "@py_obj_" + operation in body or "@py_int_" + ("shl" if operation == "lshift" else "shr") in body


@pytest.mark.integration
def test_wide_shift_counts_native_all_gc(tmp_path, monkeypatch, pcc_runtime_archive, python_program_compiler):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "wide_shifts.py"
    executable = source.with_suffix("")
    source.write_text(PROGRAM)
    python_program_compiler(str(source), str(executable), backend="self", libpython_mode="off",
                            ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(executable)], capture_output=True, text=True, timeout=30,
                             env=dict(os.environ, PATH="/nonexistent", PCC_GC_BACKEND=str(backend)))
        (tmp_path / ("gc" + str(backend) + ".stdout")).write_text(ran.stdout)
        (tmp_path / ("gc" + str(backend) + ".stderr")).write_text(ran.stderr)
        assert ran.returncode == 0, (backend, ran.stdout, ran.stderr)
        assert ran.stdout == "WIDE_SHIFT_COUNTS_OK\n" and ran.stderr == ""
