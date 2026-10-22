"""Execute the cache's current runtime source against relocating root slots."""

import ast
from pathlib import Path

import pytest

from test_exception_constructor_roots import _Memory, _Slots

PORT = Path(__file__).resolve().parents[2] / "pcc/runtime/py/py_func.py"


class CacheMemory(_Memory):
    def __init__(self, phase, fail=None):
        super().__init__(phase)
        self.fail_cache = fail
        self.maps["pcc_native_callable_result_frame_map"] = 11
        self.globals = {name: _Slots(8) for name in (
            "pcc_builtin_function_cache", "pcc_builtin_function_cache_mutex_bits",
            "pcc_builtin_function_cache_registered",
        )}
        self.globals["pcc_builtin_function_cache_mutex_bits"].fields[0] = 0
        self.globals["pcc_builtin_function_cache_registered"].fields[0] = 0
        self.error = _Slots(8)
        self.frames[id(self.error)] = self.error
        self.locked = False
        self.creations = 0
        ns = self.namespace
        ns.update({
            "cstr": lambda text: text,
            "global_addr": lambda name: self.globals[name] if name in self.globals else self.maps[name],
            "load_i32": lambda value, offset: self.read(value, offset) or 0,
            "load_i64": lambda value, offset: self.read(value, offset) or 0,
            "atomic_load_i64": lambda value, offset, order: self.read(value, offset) or 0,
            "atomic_cas_i64": self.cas,
            "int_to_ptr": lambda value: value, "ptr_to_int": lambda value: value,
            "pcc_mutex_new": lambda: 1234, "pcc_mutex_free": lambda value: None,
            "pcc_mutex_lock": self.lock, "pcc_mutex_unlock": self.unlock,
            "pcc_gc_scheduler_root_register_handle": self.register,
            "py_str_new": self.key, "py_dict_new": self.dict_new,
            "py_dict_get": self.dict_get, "py_dict_set": self.dict_set,
            "py_tuple_new": self.tuple_new, "py_func_new_named": self.function,
            "py_current_exception": lambda: self.error.fields[0],
            "py_err_occurred": lambda: self.error.fields[0] is not None,
            "py_runtime_error_if_unset": self.raise_error,
            "pcc_gc_unpin": self.unpin,
            "pcc_gc_take_pinned_slot": self.take_slot,
        })
        tree = ast.parse(PORT.read_text())
        names = {"_func_keep_call_error", "_func_clear_call_root", "_func_runtime_error_if_unset", "py_builtin_function_value"}
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and (node.name in names or node.name.startswith("_builtin_"))]
        exec(compile(ast.Module(functions, []), str(PORT), "exec"), ns)

    def frame_enter(self, frame_map, slots):
        assert frame_map == 11
        self.frames[id(slots)] = slots
        self.gc("frame_enter")

    def cas(self, value, offset, expected, new, success, failure):
        old = self.read(value, offset) or 0
        if old == expected:
            self.write(value, offset, new)
        return old

    def lock(self, mutex):
        assert not self.locked
        self.locked = True
        return 0

    def unlock(self, mutex):
        assert self.locked
        self.locked = False
        return 0

    def register(self, slot):
        base, offset = self.pointer(slot)
        assert offset == 0
        self.frames[id(base)] = base
        return object()

    def key(self, text, length):
        self.gc("allocation")
        value = self.make(4)
        value.fields[16] = text
        return value

    def dict_new(self):
        self.gc("allocation")
        return None if self.fail_cache == "dict" else self.make(6)

    def tuple_new(self, count):
        self.gc("allocation")
        return None if self.fail_cache == "tuple" else self.make(7)

    def function(self, entry, captures, name):
        self.gc("allocation")
        if self.fail_cache == "function":
            return None
        assert captures.alive
        self.creations += 1
        value = self.make(9)
        value.fields[64] = captures
        self.incref(captures)
        return value

    def dict_get(self, cache, key):
        self.gc("dictionary")
        assert cache.alive and key.alive
        value = cache.fields.get(key.fields[16])
        self.incref(value)
        return value

    def dict_set(self, cache, key, value):
        self.gc("dictionary")
        assert cache.alive and key.alive and value.alive
        self.incref(value)
        cache.fields[key.fields[16]] = value

    def raise_error(self, helper, message):
        if self.error.fields[0] is None:
            self.error.fields[0] = self.make(12)
        return None

    def unpin(self, value):
        if value is not None:
            assert value.alive
            value.flags &= ~64
            self.pin_metric -= 1

    def take_slot(self, slot, prior):
        base, offset = self.pointer(slot)
        value = base.fields[offset]
        base.fields[offset] = None
        self.unpin(value)
        if value is not None:
            value.flags |= prior
        return value


@pytest.mark.parametrize("phase", ("allocation", "load_root", "dictionary", "frame_leave"))
def test_cache_identity_and_owners_survive_movement_without_permanent_pins(phase):
    memory = CacheMemory(phase)
    result = memory.namespace["py_builtin_function_value"](111, "abs")
    caller = _Slots(8)
    caller.fields[0] = result
    memory.frames[id(caller)] = caller
    again = memory.namespace["py_builtin_function_value"](222, "abs")
    assert again is caller.fields[0]
    assert again.references == 3  # cache, first return, second return
    assert memory.creations == 1
    assert memory.pin_metric == 0 and not memory.locked
    cache = memory.globals["pcc_builtin_function_cache"].fields[0]
    assert cache.flags & 64 == 0 and again.flags & 64 == 0
    assert all(not obj.alive or obj.flags & 64 == 0 for obj in memory.objects if obj is not memory.cls)
    memory.decref(again)
    memory.decref(caller.fields[0])
    caller.fields[0] = None
    assert cache.fields["abs"].references == 1
    assert memory.moves > 0


@pytest.mark.parametrize("failure", ("dict", "tuple", "function"))
def test_cache_construction_failure_releases_temporaries_and_unlocks(failure):
    memory = CacheMemory("dictionary", failure)
    result = memory.namespace["py_builtin_function_value"](111, "abs")
    assert result is None and memory.error.fields[0] is not None
    assert not memory.locked and memory.pin_metric == 0
    assert len(memory.frames) == 2  # error TLS and registered cache global


def test_competing_initializer_wins_once_and_losing_candidate_is_released():
    memory = CacheMemory("load_root")
    original = memory.function
    other = _Slots(8)
    memory.frames[id(other)] = other
    entering = True

    def competing(entry, captures, name):
        nonlocal entering
        if entering:
            entering = False
            other.fields[0] = memory.namespace["py_builtin_function_value"](222, name)
        return original(entry, captures, name)

    memory.namespace["py_func_new_named"] = competing
    value = memory.namespace["py_builtin_function_value"](111, "abs")
    assert value is other.fields[0]
    assert memory.creations == 2
    assert len([obj for obj in memory.objects if obj.alive and obj.tag == 9]) == 1
    assert value.references == 3
    assert not memory.locked and memory.pin_metric == 0
