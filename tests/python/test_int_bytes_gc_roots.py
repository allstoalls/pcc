"""Byte conversion roots are checked with forced relocation, not pin folklore."""
from __future__ import annotations

import ast
from pathlib import Path
import re

import pytest

from pcc.runtime.py import py_abi_constants as abi
from test_exception_constructor_roots import _Object, _Slots


ROOT = Path(__file__).resolve().parents[2]
PORT = ROOT / "pcc/runtime/py/py_int_convert.py"


class _BytesMemory:
    def __init__(self, phase="index", fail_register=0, pending=None, fail_release=False):
        self.phase, self.fail_register = phase, fail_register
        self.pending, self.fail_release = pending, fail_release
        self.objects, self.handles, self.latest = [], {}, {}
        self.error = None
        self.moves = self.pins = self.depth = self.registrations = 0
        self.events = []
        self.value = self.make("value", abi.PY_TYPE_INT, movable=False)
        self.length = self.make("length", abi.PY_TYPE_CLASS)
        self.order = self.make("order", abi.PY_TYPE_STR)
        self.signed = self.make("signed", abi.PY_TYPE_CLASS)
        ns = dict(vars(abi))
        ns.update({"c_ptr": object, "c_abi_export": lambda name: lambda fn: fn,
            "null": lambda: None, "cstr": lambda s: s,
            "ptr_is_null": lambda value: value is None,
            "is_tagged_int": lambda value: isinstance(value, int),
            "ptr_eq": lambda a, b: a is b,
            "stack_alloc": _Slots, "ptr_add": self.add,
            "load_i32": self.read, "load_i64": self.read,
            "store_i64": self.write, "store_ptr": self.write,
            "pcc_gc_load_ptr": lambda owner, slot: self.read(slot, 0),
            "pcc_gc_store_root": self.store_root,
            "pcc_gc_scheduler_root_register_handle": self.register,
            "pcc_gc_scheduler_root_unregister_handle": self.unregister,
            "pcc_gc_pin": self.pin, "pcc_gc_unpin": self.unpin,
            "pcc_gc_take_pinned_slot": self.take,
            "pcc_py_gc_minor_graph_lock": self.lock,
            "pcc_py_gc_minor_graph_unlock": self.unlock,
            "pcc_gc_foreign_lease_acquire": self.acquire,
            "pcc_gc_foreign_lease_release": self.release,
            "pcc_gc_retain_plan_prepare_locked": self.retain,
            "pcc_gc_retain_plan_finish": lambda plan: self.gc("retain_finish"),
            "py_decref": self.decref,
            "py_index_i64_checked": lambda obj: self.callback("index", obj),
            "py_obj_truthy": lambda obj: self.callback("truth", obj),
            "py_err_occurred": lambda: int(self.error is not None),
            "py_exc_new": lambda kind, message: (kind, message),
            "py_raise_owned": self.raise_error,
        })
        tree = ast.parse(PORT.read_text())
        functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(PORT), "exec"), ns)
        # Allocation hooks execute at the same source-body boundaries while
        # refusing stale input copies. The arithmetic has separate regressions.
        ns["_int_to_bytes"] = self.encode
        ns["_int_from_bytes_leased_base"] = self.decode
        ns["_byteorder_is_big"] = lambda order: self.read(order, abi.PYOBJECTHEADER_TYPE_TAG_OFFSET) * 0 + 1
        self.namespace = ns

    def make(self, name, tag, movable=True):
        value = _Object(tag)
        value.name, value.movable, value.leases = name, movable, 0
        self.objects.append(value)
        self.latest[name] = value
        return value

    def pointer(self, pointer):
        return pointer if isinstance(pointer, tuple) else (pointer, 0)

    def add(self, pointer, offset):
        owner, start = self.pointer(pointer)
        return owner, start + offset

    def read(self, pointer, offset):
        owner, start = self.pointer(pointer)
        offset += start
        if isinstance(owner, _Object):
            assert owner.alive, "stale managed object read"
            if offset == abi.PYOBJECTHEADER_TYPE_TAG_OFFSET:
                return owner.tag
            if offset == abi.PYOBJECTHEADER_FLAGS_OFFSET:
                return owner.flags
        return owner.fields.get(offset)

    def write(self, pointer, offset, value):
        owner, start = self.pointer(pointer)
        if isinstance(owner, _Object):
            assert owner.alive, "stale managed object write"
        owner.fields[start + offset] = value

    def gc(self, phase):
        if self.depth or phase != self.phase:
            return
        for old in list(self.objects):
            if not old.alive or not old.movable or old.flags & abi.PY_FLAG_GC_PINNED or old.leases:
                continue
            moved = self.make(old.name, old.tag)
            moved.references, moved.fields = old.references, old.fields.copy()
            moved.leases = old.leases
            for slot in self.handles.values():
                if self.read(slot, 0) is old:
                    self.write(slot, 0, moved)
            for obj in self.objects:
                if obj.alive:
                    obj.fields = {key: moved if value is old else value for key, value in obj.fields.items()}
            old.alive, old.references = False, 0
            self.moves += 1

    def register(self, slot):
        self.registrations += 1
        if self.registrations == self.fail_register:
            return None
        self.gc("register")
        handle = object()
        self.handles[handle] = slot
        return handle

    def unregister(self, handle):
        self.gc("unregister")
        del self.handles[handle]

    def store_root(self, slot, value):
        old = self.read(slot, 0)
        if value is not None:
            assert value.alive
            value.references += 1
        self.write(slot, 0, value)
        if old is not None:
            self.decref(old)
            if self.phase == "close":
                for obj in self.objects:
                    if obj.alive:
                        obj.flags &= ~abi.PY_FLAG_GC_PINNED
                self.gc("close")

    def pin(self, value):
        if value is not None and not isinstance(value, int):
            assert value.alive
            value.flags |= abi.PY_FLAG_GC_PINNED
            self.pins += 1

    def unpin(self, value):
        if value is not None and not isinstance(value, int):
            assert value.alive
            value.flags &= ~abi.PY_FLAG_GC_PINNED
            self.pins -= 1

    def take(self, slot, prior):
        value = self.read(slot, 0)
        self.write(slot, 0, None)
        self.unpin(value)
        if value is not None:
            value.flags |= prior & abi.PY_FLAG_GC_PINNED
        return value

    def decref(self, value):
        if value is not None:
            assert value.alive, "stale owner cleanup"
            assert value.references > 0, "duplicate owner cleanup"
            value.references -= 1
            if value.references == 0:
                value.alive = False

    def lock(self):
        self.gc("lock")
        self.depth += 1

    def unlock(self):
        self.depth -= 1

    def raise_error(self, error):
        self.error = error

    def callback(self, name, value):
        assert value.alive, "stale protocol receiver"
        for obj in self.objects:
            if obj.alive:
                obj.flags &= ~abi.PY_FLAG_GC_PINNED
        self.gc(name)
        if name == self.pending:
            self.error = (2, name)
        return 1

    def encode(self, value, length, order, signed):
        assert value.alive and order.alive, "stale encoder arguments"
        assert order is self.latest["order"]
        return self.make("result", abi.PY_TYPE_BYTES)

    def retain(self, plan, value):
        assert self.depth > 0
        if value is not None:
            assert value.alive
            value.references += 1
        return value

    def acquire(self, slot):
        self.gc("acquire")
        value = self.read(slot, 0)
        if value is None:
            return 0
        assert value.alive
        value.leases += 1
        return 1

    def release(self, slot, acquired):
        if acquired:
            value = self.read(slot, 0)
            assert value.alive and value.leases > 0
            value.leases -= 1
        return -3 if self.fail_release else 0

    def decode(self, base, big, signed):
        if base is None:
            self.error = (3, "buffer")
            return None
        assert base.alive and base.leases > 0
        self.events.append(("base", base))
        self.gc("allocate")
        assert base.alive, "buffer moved across bigint allocation"
        return self.make("result", abi.PY_TYPE_INT, movable=False)

    def encode_call(self):
        return self.namespace["py_int_to_bytes_args"](self.value, self.length, self.order, self.signed)

    def assert_closed(self):
        assert not self.handles
        assert self.depth == self.pins == 0
        for name in ("value", "length", "order", "signed"):
            assert self.latest[name].references == 1, name


@pytest.mark.parametrize("phase", ["index", "truth", "close"])
def test_to_bytes_source_reloads_callback_and_teardown_roots(phase):
    memory = _BytesMemory(phase=phase)
    result = memory.encode_call()
    assert result is memory.latest["result"] and result.alive and result.references == 1
    assert memory.moves > 0
    assert memory.error is None
    memory.assert_closed()


@pytest.mark.parametrize("failure", [1, 2, 3, 4, 5])
def test_to_bytes_source_closes_partial_registration(failure):
    memory = _BytesMemory(phase=None, fail_register=failure)
    assert memory.encode_call() is None
    assert memory.error[0] == 7
    memory.assert_closed()
    if "result" in memory.latest:
        assert memory.latest["result"].references == 0


@pytest.mark.parametrize("pending", ["index", "truth"])
def test_to_bytes_source_preserves_callback_error(pending):
    memory = _BytesMemory(phase=pending, pending=pending)
    assert memory.encode_call() is None
    assert memory.error == (2, pending)
    memory.assert_closed()


@pytest.mark.parametrize("phase", ["retain_finish", "acquire", "allocate"])
def test_from_bytes_source_leases_resolved_memoryview_base(phase):
    memory = _BytesMemory(phase=phase)
    base = memory.make("base", abi.PY_TYPE_BYTES)
    view = memory.make("view", abi.PY_TYPE_MEMORYVIEW, movable=False)
    view.fields[abi.PYMEMORYVIEWOBJECT_BASE_OFFSET] = base
    memory.make("movement-control", abi.PY_TYPE_STR)
    result = memory.namespace["py_int_from_bytes_signed"](view, memory.order, 1)
    assert result is memory.latest["result"] and result.references == 1
    assert memory.moves > 0
    assert memory.latest["base"].leases == 0
    assert memory.latest["base"].references == 1
    memory.assert_closed()


def test_from_bytes_source_releases_result_on_lease_cleanup_failure():
    memory = _BytesMemory(phase=None, fail_release=True)
    base = memory.make("base", abi.PY_TYPE_BYTES)
    assert memory.namespace["py_int_from_bytes_signed"](base, memory.order, 0) is None
    assert memory.error[0] == 7
    assert memory.latest["base"].leases == 0 and memory.latest["base"].references == 1
    assert memory.latest["result"].references == 0
    memory.assert_closed()


def test_byte_binders_use_rooted_leases_and_owned_result_transfer(tmp_path):
    from pcc.frontends.python.pipeline import compile_python
    from tests.owned_ir_validation import verify_ir_text
    source = tmp_path / "byte_roots.py"
    source.write_text('''
def encode(value, length, order, sign):
    return value.to_bytes(length, byteorder=order, signed=sign)
def decode(value, order, sign):
    return int.from_bytes(value, byteorder=order, signed=sign)
''')
    target = tmp_path / "byte_roots.ll"
    compile_python(str(source), str(target), backend="self", libpython_mode="off",
                   emit_llvm_only=True, python_library=True)
    text = target.read_text()
    verify_ir_text(text)
    for name, helper, count in (("encode", "py_int_to_bytes_args", 4), ("decode", "py_int_from_bytes_signed", 3)):
        body = re.search(r"^define[^\n]*@user_byte_roots_" + name + r"\([^\n]*\)[^\n]*\{\n(.*?)^\}", text, re.M | re.S)[1]
        call = next(line for line in body.splitlines() if "call " in line and "@" + helper + "(" in line)
        assert "extern.argument.current" in call
        assert body.count("@pcc_gc_foreign_lease_acquire(") == count
        assert "@pcc_gc_foreign_lease_release(" in body
        assert "@pcc_gc_take_pinned_slot(" in body
        assert ".result.root" in body and "extern.pin.prior" in body
        assert not re.search(r"@pcc_gc_unpin\(ptr %(?:value|length|order|sign)\)", body)
        assert "strict.nolib.stub" not in body


_GC3_SOURCE = r'''
import gc
from pcc.extern import c_abi_export, c_obj
from pcc.unsafe import ptr_to_int, load_i32, store_i32, load_i64, global_addr

@c_abi_export("bytes_gc_address")
def address(value: c_obj) -> int:
    return ptr_to_int(value)

@c_abi_export("bytes_gc_flags")
def flags(value: c_obj) -> int:
    return load_i32(value, 12)

@c_abi_export("bytes_gc_set_legacy_pin")
def legacy_pin(value: c_obj, enabled: int) -> None:
    old = load_i32(value, 12)
    if enabled:
        store_i32(value, 12, old | 64)
    else:
        store_i32(value, 12, old & ~64)

@c_abi_export("bytes_gc_active_leases")
def active_leases() -> int:
    return load_i64(global_addr("pcc_gc_foreign_lease_active"), 0)

def fresh_order(word):
    return ("@" + word)[1:]

def alias_and_collect(holder):
    alias = [holder[0]]
    # Make the legacy alias-unpin condition deterministic. This changes only
    # the Boolean flag; the counted lease must independently prevent movement.
    legacy_pin(alias[0], 0)
    gc.collect()
    return address(holder[0])

def verify_released(holder):
    # A pinned nursery survivor is promoted in place by GC3. The nursery
    # allocation flag persists even though YOUNG is cleared, so requiring that
    # same OLD object to copy on the next collection is not a lease oracle.
    # Check the actual counted lease ledger, restore the Boolean pin, then
    # prove that a fresh eligible unleased object still really moves.
    assert active_leases() == 0, "address lease survived the call"
    assert flags(holder[0]) & 256, "leased survivor was not promoted in place"
    assert not (flags(holder[0]) & 128), "old survivor is still young"
    legacy_pin(holder[0], 0)
    assert not (flags(holder[0]) & 64), "legacy pin could not be released"
    gc.collect()
    assert active_leases() == 0
    fresh = [fresh_order("big")]
    assert flags(fresh[0]) & 4096 and flags(fresh[0]) & 128
    before = address(fresh[0])
    assert alias_and_collect(fresh) != before, "fresh unleased control did not move"

def moving_width(holder):
    before = address(holder[0])
    assert alias_and_collect(holder) != before, "rooted operand did not move"
    return 2

def moving_failure(holder):
    before = address(holder[0])
    assert alias_and_collect(holder) != before, "error operand did not move"
    raise ValueError("later operand")

class Width:
    def __init__(self, holder):
        self.holder = holder
    def __index__(self):
        before = address(self.holder[0])
        assert alias_and_collect(self.holder) == before
        assert int.from_bytes(b"\x80", byteorder=self.holder[0], signed=True) == -128
        assert alias_and_collect(self.holder) == before
        return 2

class Sign:
    def __init__(self, holder, fail=False):
        self.holder = holder
        self.fail = fail
    def __bool__(self):
        before = address(self.holder[0])
        assert alias_and_collect(self.holder) == before
        if self.fail:
            raise ValueError("truth")
        return True

def main():
    control = [fresh_order("little")]
    assert flags(control[0]) & 4096, "control is not nursery-backed"
    before = address(control[0])
    assert alias_and_collect(control) != before, "GC3 control did not move"
    for word in ("little", "big"):
        holder = [fresh_order(word)]
        expected = b"\x80\xff" if word == "little" else b"\xff\x80"
        assert (-128).to_bytes(byteorder=holder[0], length=moving_width(holder), signed=True) == expected
        assert holder[0] == word
        holder = [fresh_order(word)]
        try:
            int.from_bytes(b"\x80", byteorder=holder[0], signed=moving_failure(holder))
        except ValueError:
            pass
        else:
            raise AssertionError("missing later-operand error")
        assert holder[0] == word
        holder = [fresh_order(word)]
        legacy_pin(holder[0], 1)
        width = Width(holder)
        sign = Sign(holder)
        before = address(holder[0])
        expected = b"\x80\xff" if word == "little" else b"\xff\x80"
        assert (-128).to_bytes(length=width, byteorder=holder[0], signed=sign) == expected
        assert flags(holder[0]) & 64, "prior Boolean pin was lost"
        assert flags(holder[0]) & 4096, "leased nursery object moved"
        assert address(holder[0]) == before
        verify_released(holder)
        holder = [fresh_order(word)]
        legacy_pin(holder[0], 1)
        before = address(holder[0])
        try:
            (1).to_bytes(length=1, byteorder=holder[0], signed=Sign(holder, True))
        except ValueError:
            pass
        else:
            raise AssertionError("missing truth error")
        assert flags(holder[0]) & 64
        assert address(holder[0]) == before
        verify_released(holder)
        holder = [fresh_order(word)]
        legacy_pin(holder[0], 1)
        before = address(holder[0])
        assert int.from_bytes(memoryview(b"\xff" * 17), byteorder=holder[0], signed=Sign(holder)) == -1
        assert flags(holder[0]) & 64
        assert address(holder[0]) == before
        verify_released(holder)
    print("byte-gc3-ok")
main()
'''


def test_forced_gc3_program_emits_lease_protected_ir(tmp_path):
    from pcc.frontends.python.pipeline import compile_python
    from tests.owned_ir_validation import verify_ir_text
    source = tmp_path / "byte_gc3.py"
    source.write_text(_GC3_SOURCE)
    output = tmp_path / "byte_gc3.ll"
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   emit_llvm_only=True)
    text = output.read_text()
    verify_ir_text(text)
    assert "strict.nolib.stub" not in text
    assert "@pcc_gc_foreign_lease_acquire(" in text
    assert "@pcc_gc_foreign_lease_release(" in text
    assert "@bytes_gc_address(" in text


def test_forced_gc3_movement_and_byte_callback_leases(
    tmp_path, pcc_diagnostic_runtime_archive, python_program_compiler, monkeypatch,
):
    import os
    import subprocess
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "byte_gc3.py"
    source.write_text(_GC3_SOURCE)
    output = tmp_path / "byte_gc3"
    python_program_compiler(str(source), str(output), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_diagnostic_runtime_archive))
    result = subprocess.run([str(output)], capture_output=True, text=True, timeout=30,
                            env=dict(os.environ, PCC_GC_BACKEND="3", PCC_GC_MINOR_ALLOC_MAX="4096",
                                     PATH="", PCC_HOST_PYTHON="/nonexistent/host-python"))
    (tmp_path / "gc3.stdout").write_text(result.stdout)
    (tmp_path / "gc3.stderr").write_text(result.stderr)
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    assert result.stdout == "byte-gc3-ok\n" and result.stderr == ""
