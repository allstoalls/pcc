"""Constructor owners/borrowed inputs survive GC at their actual runtime calls."""
from __future__ import annotations

import ast
from pathlib import Path
import pytest

from pcc.runtime.py.py_abi_constants import PY_TYPE_CLASS, PY_TYPE_EXC, PY_TYPE_LIST, PY_TYPE_NONE, PY_TYPE_STR

PORT = Path(__file__).resolve().parents[2] / "pcc/runtime/py/py_exc_objects.py"


class _Object:
    def __init__(self, tag, flags=0):
        self.tag, self.flags, self.references, self.alive = tag, flags, 1, True
        self.fields = {}


class _Slots:
    def __init__(self, size):
        self.fields = {offset: None for offset in range(0, size, 8)}


class _Memory:
    def __init__(self, phase, fail=None):
        self.phase, self.fail = phase, fail
        self.objects, self.frames = [], {}
        self.graph_depth = self.pin_metric = self.moves = 0
        self.cls = self.make(PY_TYPE_CLASS, 1)
        self.value = self.make(PY_TYPE_LIST)
        self.none = self.make(PY_TYPE_NONE, 1)
        self.latest = {id(self.cls): self.cls, id(self.value): self.value}
        self.maps = {"pcc_exc_construct_borrowed_frame_map": -2,
                     "pcc_exc_construct_owned_frame_map": 2}
        self.namespace = self._namespace()
        parsed = ast.parse(PORT.read_text(), filename=str(PORT))
        functions = [node for node in parsed.body if isinstance(node, ast.FunctionDef)]
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(PORT), "exec"), self.namespace)

    def make(self, tag, flags=0):
        value = _Object(tag, flags)
        self.objects.append(value)
        return value

    def gc(self, phase):
        if self.graph_depth or phase != self.phase:
            return
        for old in list(self.objects):
            if not old.alive or old.flags & (64 | 16384) or old is self.none:
                continue
            new = self.make(old.tag, old.flags)
            new.references, new.fields = old.references, old.fields.copy()
            for slots in self.frames.values():
                for offset, item in list(slots.fields.items()):
                    if item is old: slots.fields[offset] = new
            for owner in self.objects:
                if owner.alive:
                    owner.fields = {offset: new if item is old else item for offset,item in owner.fields.items()}
            for key, item in list(self.latest.items()):
                if item is old: self.latest[key] = new
            if self.cls is old: self.cls = new
            if self.value is old: self.value = new
            old.alive = False
            self.moves += 1

    def read(self, value, offset):
        if isinstance(value, _Object):
            assert value.alive, "stale object address"
            if offset == 0: return value.references
            if offset == 8: return value.tag
            if offset == 12: return value.flags
        return value.fields.get(offset)

    def write(self, value, offset, item):
        if isinstance(value, _Object):
            assert value.alive, "stale object address"
            if offset == 0: value.references = item; return
            if offset == 8: value.tag = item; return
            if offset == 12: value.flags = item; return
        value.fields[offset] = item

    def pointer(self, slot):
        return slot if isinstance(slot, tuple) else (slot, 0)

    def incref(self, value):
        if isinstance(value, _Object):
            assert value.alive, "stale class/value argument"
            if not value.flags & 1: value.references += 1

    def decref(self, value):
        if isinstance(value, _Object):
            assert value.alive, "stale owner during release"
            if not value.flags & 1:
                value.references -= 1
                if value.references == 0:
                    for item in value.fields.values(): self.decref(item)
                    value.alive = False

    def frame_enter(self, frame_map, slots):
        assert frame_map in (-2, 2)
        self.frames[id(slots)] = slots
        self.gc("frame_enter")

    def frame_leave(self, slots):
        del self.frames[id(slots)]
        self.gc("frame_leave")

    def alloc(self, _size, tag, _flags):
        self.gc("allocation")
        return None if self.fail == "allocation" else self.make(tag, 16384)

    def string(self, _text, _length):
        self.gc("string")
        return None if self.fail == "string" else self.make(PY_TYPE_STR, 16384)

    def store_ref(self, owner, slot, value):
        self.gc("field_store")
        assert owner.alive
        target, offset = self.pointer(slot)
        assert target is owner
        self.incref(value)
        old = self.read(owner, offset)
        self.write(owner, offset, value)
        self.decref(old)

    def store_root(self, slot, value):
        target, offset = self.pointer(slot)
        self.incref(value)
        old = self.read(target, offset)
        self.write(target, offset, value)
        self.decref(old)

    def publish(self, value):
        assert value.alive
        value.flags &= ~16384
        self.gc("publish")

    def pin(self, value):
        assert value.alive
        value.flags |= 64
        self.pin_metric += 1

    def take(self, slots, prior):
        value = slots.fields[0]
        slots.fields[0] = None
        assert value.alive and value.flags & 64
        value.flags = (value.flags & ~64) | prior
        self.pin_metric -= 1
        return value

    def _namespace(self):
        def zero(pointer, _byte, size):
            value, start = self.pointer(pointer)
            for offset in range(start, start + size, 8): self.write(value, offset, None)
        def lookup(_tag):
            self.gc("lookup")
            return self.cls
        def gc_load(_owner, pointer):
            self.gc("load_root")
            return self.read(*self.pointer(pointer))
        def lock(): self.gc("graph_lock"); self.graph_depth += 1
        def unlock(): self.graph_depth -= 1; self.gc("graph_unlock")
        return {
            "PY_TYPE_CLASS": PY_TYPE_CLASS, "PY_TYPE_EXC": PY_TYPE_EXC,
            "PY_TYPE_INT": 2, "c_abi_export": lambda _name: lambda fn: fn,
            "null": lambda: None, "ptr_is_null": lambda value: value is None,
            "is_tagged_int": lambda value: isinstance(value, int),
            "load_ptr": self.read, "load_i32": self.read,
            "store_ptr": self.write, "store_i32": self.write, "store_i64": self.write,
            "ptr_add": lambda value, offset: (value, offset),
            "stack_alloc": _Slots, "memset": zero, "strlen": len,
            "global_addr": self.maps.__getitem__,
            "global_load_ptr": lambda _name: self.none,
            "pcc_gc_frame_enter": self.frame_enter, "pcc_gc_frame_leave": self.frame_leave,
            "pcc_gc_load_ptr": gc_load, "pcc_gc_store_root": self.store_root,
            "pcc_gc_alloc": self.alloc, "py_str_new": self.string,
            "pcc_gc_store_ptr": self.store_ref, "py_exc_builtin_class": lookup,
            "pcc_gc_publish_initialized": self.publish,
            "pcc_gc_note_write_barrier": lambda _owner, _value: self.gc("barrier"),
            "pcc_py_gc_minor_graph_lock": lock, "pcc_py_gc_minor_graph_unlock": unlock,
            "pcc_gc_pin": self.pin, "pcc_gc_take_pinned_slot": self.take,
            "py_incref": self.incref, "py_decref": self.decref,
            "pcc_diagnostics_runtime_log_event_code": lambda *_args: self.gc("log"),
        }


@pytest.mark.parametrize("phase", ["allocation", "frame_enter", "load_root", "field_store",
                                    "string", "publish", "log", "frame_leave", "graph_unlock"])
def test_exception_constructs_from_healed_inputs_and_returns_one_owner(phase):
    memory = _Memory(phase)
    result = memory.namespace["py_exc_alloc"](memory.cls, "message")
    assert result.alive and result.tag == PY_TYPE_EXC and result.references == 1
    assert result.fields[16] is memory.cls
    assert result.fields[24].alive and result.fields[24].references == 1
    assert not memory.frames and not memory.graph_depth and memory.pin_metric == 0
    if phase != "field_store": assert memory.moves > 0


@pytest.mark.parametrize("fail", ["allocation", "string"])
def test_exception_constructor_failure_balances_owned_and_borrowed_slots(fail):
    memory = _Memory("allocation", fail)
    result = memory.namespace["py_exc_alloc"](memory.cls, "message")
    assert result is None
    assert memory.cls.alive and memory.value.alive and memory.value.references == 1
    assert not memory.frames and not memory.graph_depth and memory.pin_metric == 0
    assert not any(value.alive and value.tag == PY_TYPE_EXC for value in memory.objects)


def test_exception_value_is_rooted_before_class_lookup_and_object_allocation():
    memory = _Memory("lookup")
    result = memory.namespace["py_exc_new_with_value"](8, memory.value)
    assert result.alive and result.fields[16] is memory.cls
    assert result.fields[24] is memory.value and memory.value.references == 2
    assert not memory.frames and memory.pin_metric == 0


@pytest.mark.parametrize("phase", ["allocation", "publish", "frame_leave", "load_root"])
def test_exception_value_and_class_constructors_preserve_aliases_and_borrows(phase):
    memory = _Memory(phase)
    result = memory.namespace["py_exc_new_with_value"](8, memory.cls)
    assert result.fields[16] is result.fields[24] is memory.cls
    assert result.alive and result.references == 1 and memory.cls.alive
    assert not memory.frames and memory.pin_metric == 0
    memory = _Memory(phase)
    result = memory.namespace["py_exc_new_with_class"](memory.cls, None)
    assert result.fields[16] is memory.cls and result.fields[24] is None
    assert result.alive and result.references == 1
    assert not memory.frames and memory.pin_metric == 0


def test_constructor_final_handoff_restores_existing_pin_after_relocation():
    memory = _Memory("publish")
    original_alloc = memory.alloc
    def allocate_pinned(*args):
        value = original_alloc(*args)
        value.flags |= 64
        return value
    memory.namespace["pcc_gc_alloc"] = allocate_pinned
    result = memory.namespace["py_exc_alloc"](memory.cls, None)
    assert result.alive and result.references == 1 and result.flags & 64
    assert memory.pin_metric == 0 and not memory.frames


def test_default_class_and_none_value_remain_normal_constructor_paths():
    memory = _Memory("lookup")
    result = memory.namespace["py_exc_alloc"](None, None)
    assert result.fields[16] is memory.cls and result.fields[24] is None
    assert result.references == 1 and not memory.frames and memory.pin_metric == 0
    memory = _Memory("lookup")
    result = memory.namespace["py_exc_new_with_value"](8, None)
    assert result.fields[16] is memory.cls and result.fields[24] is None
    assert result.references == 1 and not memory.frames and memory.pin_metric == 0


def test_value_constructor_allocation_failure_keeps_caller_reference():
    memory = _Memory("lookup", "allocation")
    assert memory.namespace["py_exc_new_with_value"](8, memory.value) is None
    assert memory.value.alive and memory.value.references == 1
    assert not memory.frames and memory.pin_metric == 0


@pytest.mark.parametrize("triple", ["arm64-apple-darwin", "aarch64-unknown-linux-gnu",
                                    "x86_64-unknown-linux-gnu", "x86_64-pc-windows-msvc"])
def test_exception_constructor_roots_reach_owned_emitter(tmp_path, monkeypatch, triple):
    from pcc.backend.owned_object_emit import emit_owned_object
    from pcc.frontends.python.owned_runtime_build import runtime_ir_passes
    from pcc.frontends.python.pipeline import compile_python
    source = tmp_path / "py_runtime_exc" / "py" / "py_exc_objects.py"
    source.parent.mkdir(parents=True)
    source.write_text(PORT.read_text())
    output = tmp_path / "py_exc_objects.ll"
    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", runtime_ir_passes(str(PORT.parent.parent)))
    compile_python(str(source), str(output), emit_llvm_only=True, python_library=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode="on", target_triple=triple)
    text = output.read_text()
    assert "@pcc_exc_construct_borrowed_frame_map" in text
    assert "@pcc_exc_construct_owned_frame_map" in text
    assert "@pcc_gc_take_pinned_slot(" in text
    payload = emit_owned_object(text, triple)
    assert len(payload) > 64
    (tmp_path / "py_exc_objects.o").write_bytes(payload)


@pytest.mark.parametrize("phase", ["allocation", "publish", "frame_leave", "load_root"])
def test_exception_explicit_none_is_a_real_argument_and_owned_alias(phase):
    memory = _Memory(phase)
    result = memory.namespace["py_exc_new_with_value"](2, memory.none)
    assert result.alive and result.references == 1
    assert result.fields[16] is memory.cls
    assert result.fields[24] is memory.none and memory.none.alive
    assert not memory.frames and memory.pin_metric == 0
    memory = _Memory(phase)
    result = memory.namespace["py_exc_new_with_value"](4, None)
    assert result.alive and result.references == 1
    assert result.fields[16] is memory.cls and result.fields[24] is None
    assert not memory.frames and memory.pin_metric == 0
