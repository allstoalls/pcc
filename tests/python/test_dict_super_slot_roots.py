"""Execute production dict/init bodies with relocation at boundary events.

This deterministic model is not a native GC qualification. It rejects stale
pointers, nonempty root registration, missing leases and finish-under-lock.
"""
import ast
from pathlib import Path

import pytest

from pcc.runtime.py import py_abi_constants as abi
from tests.python.test_set_call_slot_roots import Block, Object, Memory


ROOT = Path(__file__).resolve().parents[2] / "pcc/runtime/py"


EXPECTED_SCRATCH_LAYOUT = {
    '_BUILTIN_INIT_OLD_EXCEPTION_SLOT': 0,
    '_BUILTIN_INIT_RECEIVER_SLOT': 1,
    '_BUILTIN_INIT_FROM_CLASS_SLOT': 2,
    '_BUILTIN_INIT_ARGS_SLOT': 3,
    '_BUILTIN_INIT_KWARGS_SLOT': 4,
    '_BUILTIN_INIT_CLASS_SLOT': 5,
    '_BUILTIN_INIT_ENV_SLOT': 6,
    '_BUILTIN_INIT_KEY_SLOT': 7,
    '_BUILTIN_INIT_DICT_SLOT': 8,
    '_BUILTIN_INIT_SOURCE_SLOT': 9,
    '_BUILTIN_INIT_TEMP_SLOT': 10,
    '_BUILTIN_INIT_ERROR_SLOT': 11,
    '_BUILTIN_INIT_SLOT_COUNT': 12,
}


def _scratch_layout(tree):
    values = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in EXPECTED_SCRATCH_LAYOUT):
            value = ast.literal_eval(node.value)
            assert type(value) is int
            values[node.targets[0].id] = value
    assert values == EXPECTED_SCRATCH_LAYOUT
    return values


class DictMemory(Memory):
    def __init__(self, phase="register", **kwargs):
        super().__init__(phase, **kwargs)
        self.env_reentry = None
        self.global_cells = {}
        self.ns.update({name: getattr(abi, name) for name in dir(abi) if name.isupper()})
        self.ns.update({
            "i64": int,
            "global_addr": self.global_addr,
            "pcc_gc_root_copy_borrowed_lease": self.copy,
            "pcc_gc_note_slot_write_barrier": lambda *_: None,
            "pcc_gc_pointer_is_managed": lambda obj: int(isinstance(obj, Object)),
            "pcc_capi_is_cext_type_tag": lambda _: 0,
            "pcc_gc_store_ptr_plan_init": self.plan_init,
            "pcc_gc_store_ptr_plan_commit_locked": lambda plan, owner, slot, value: self.plan_commit(plan, owner, slot, value, None),
            "pcc_gc_store_ptr_plan_finish": self.plan_finish,
            "pcc_gc_store_ptr": self.store_field,
            "py_tls_exc_swap_slot": self.swap_error,
            "py_runtime_error_if_unset": self.runtime_error,
            "py_dict_new": self.new_dict,
            "py_dict_len": lambda obj: self.read(obj, abi.PYDICTOBJECT_ITEM_COUNT_OFFSET),
            "py_tuple_new": self.new_tuple, "py_tuple_len": lambda obj: self.read(obj, abi.PYTUPLEOBJECT_LEN_OFFSET),
            "py_tuple_set_item": self.set_tuple,
            "py_list_new": self.new_list, "py_list_append": self.append_list,
            "py_list_len": lambda obj: self.read(obj, abi.PYLISTOBJECT_LENGTH_OFFSET),
            "py_str_new": lambda value, count: self.wrap(value[:count]),
            "load_i8": lambda value, offset: ord(value[offset]) if isinstance(value, str) and offset < len(value) else 0,
            "py_obj_issubclass": lambda cls, base: int(cls is base),
            "py_obj_getattr": self.getattr, "py_obj_getitem": self.getitem,
            "py_obj_call_slots": self.call_slots,
            "_maybe_grow": lambda _: 0,
            "pcc_platform_abort": lambda: (_ for _ in ()).throw(AssertionError("lease invariant")),
        })
        for filename, prefix, additional in (
            ("py_dict.py", "_dict_slot_", {"_ptr_is_dict", "_ptr_can_have_header", "_dict_insert_rooted_slot", "_dict_read_reload_root", "_perturb_shift5", "py_dict_set_slots", "py_dict_setdefault_slots", "py_dict_update_slots"}),
            ("py_protocol_runtime.py", "_builtin_init_", {"_type_of", "_is_user_instance", "_dict_items_key", "_cstr_equal", "py_dict_subclass_init_slots", "py_exception_subclass_init_slots", "py_builtin_super_validate_slots"}),
        ):
            path = ROOT / filename
            tree = ast.parse(path.read_text())
            if filename == "py_protocol_runtime.py":
                self.ns.update(_scratch_layout(tree))
            if filename == "py_dict.py":
                for node in tree.body:
                    if (isinstance(node, ast.Assign) and len(node.targets) == 1
                            and isinstance(node.targets[0], ast.Name)
                            and node.targets[0].id.startswith("_DICT_SLOT_")):
                        self.ns[node.targets[0].id] = ast.literal_eval(node.value)
            body = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and (node.name.startswith(prefix) or node.name in additional)]
            for node in body:
                node.decorator_list = []
            exec(compile(ast.Module(body=body, type_ignores=[]), str(path), "exec"), self.ns)

    def global_addr(self, name):
        # Runtime switches read 0: the backend-0 fast gates stay closed, so
        # these models keep executing the slot transactions they check.
        cell = self.global_cells.get(name)
        if cell is None:
            cell = self.global_cells[name] = Block()
            cell.fields[0] = 0
        return cell

    def make(self, value, tag):
        obj = super().make(value, tag)
        obj.fields[abi.PYOBJECTHEADER_FLAGS_OFFSET] = 0
        return obj

    def wrap(self, value):
        if isinstance(value, Object):
            return value
        if isinstance(value, int):
            return value
        if value is None:
            return self.none
        if isinstance(value, dict):
            obj = self.new_dict()
            for key, item in value.items():
                self.insert_fixture(obj, self.wrap(key), self.wrap(item))
            return obj
        if isinstance(value, tuple):
            obj = self.new_tuple(len(value))
            for index, item in enumerate(value):
                self.set_tuple(obj, index, self.wrap(item))
            return obj
        return self.make(value, abi.PY_TYPE_STR if isinstance(value, str) else abi.PY_TYPE_LIST)

    def new_dict(self):
        obj = self.make({}, abi.PY_TYPE_DICT)
        capacity = 64
        indices, entries = Block(), Block()
        indices.fields = {index * 8: -1 for index in range(capacity)}
        obj.fields.update({abi.PYDICTOBJECT_CAPACITY_OFFSET: capacity,
            abi.PYDICTOBJECT_ITEM_COUNT_OFFSET: 0, abi.PYDICTOBJECT_ENTRIES_USED_OFFSET: 0,
            abi.PYDICTOBJECT_INDICES_OFFSET: indices, abi.PYDICTOBJECT_ENTRIES_OFFSET: entries})
        if self.env_reentry is not None:
            callback, self.env_reentry = self.env_reentry, None
            callback()
        return obj

    def insert_fixture(self, obj, key, value):
        hash_value = hash(self.unwrap(key))
        indices = obj.fields[abi.PYDICTOBJECT_INDICES_OFFSET]
        entries = obj.fields[abi.PYDICTOBJECT_ENTRIES_OFFSET]
        bucket, perturb = hash_value & 63, hash_value
        while indices.fields[bucket * 8] != -1:
            perturb = (perturb & ((1 << 64) - 1)) >> 5
            bucket = (bucket * 5 + perturb + 1) & 63
        index = obj.fields[abi.PYDICTOBJECT_ENTRIES_USED_OFFSET]
        indices.fields[bucket * 8] = index
        entries.fields.update({index * abi.DICTENTRY_SIZE: hash_value,
            index * abi.DICTENTRY_SIZE + abi.DICTENTRY_KEY_OFFSET: key,
            index * abi.DICTENTRY_SIZE + abi.DICTENTRY_VALUE_OFFSET: value})
        obj.fields[abi.PYDICTOBJECT_ENTRIES_USED_OFFSET] += 1
        obj.fields[abi.PYDICTOBJECT_ITEM_COUNT_OFFSET] += 1
        for child in (key, value):
            if isinstance(child, Object):
                child.refs += 1

    def new_list(self, size):
        obj = self.make([], abi.PY_TYPE_LIST)
        obj.fields[abi.PYLISTOBJECT_ITEMS_OFFSET] = Block()
        obj.fields[abi.PYLISTOBJECT_LENGTH_OFFSET] = 0
        return obj

    def append_list(self, obj, value):
        assert obj.alive and obj.leases > 0
        count = obj.fields[abi.PYLISTOBJECT_LENGTH_OFFSET]
        self.write(obj.fields[abi.PYLISTOBJECT_ITEMS_OFFSET], count * 8, value)
        obj.fields[abi.PYLISTOBJECT_LENGTH_OFFSET] += 1
        if isinstance(value, Object):
            assert value.alive and value.leases > 0
            value.refs += 1

    def new_tuple(self, size):
        obj = self.make((), abi.PY_TYPE_TUPLE)
        obj.fields[abi.PYTUPLEOBJECT_LEN_OFFSET] = size
        return obj

    def set_tuple(self, obj, index, value):
        assert obj.alive
        self.write(obj, abi.PYTUPLEOBJECT_ITEMS_OFFSET + index * 8, value)
        if isinstance(value, Object):
            value.refs += 1

    def unwrap(self, value):
        if not isinstance(value, Object):
            return value
        assert value.alive
        if value.tag == abi.PY_TYPE_TUPLE:
            return tuple(self.unwrap(self.read(value, abi.PYTUPLEOBJECT_ITEMS_OFFSET + index * 8))
                         for index in range(self.read(value, abi.PYTUPLEOBJECT_LEN_OFFSET)))
        if value.tag == abi.PY_TYPE_LIST and abi.PYLISTOBJECT_ITEMS_OFFSET in value.fields:
            return [self.unwrap(self.read(value.fields[abi.PYLISTOBJECT_ITEMS_OFFSET], index * 8))
                    for index in range(value.fields[abi.PYLISTOBJECT_LENGTH_OFFSET])]
        if value.tag == abi.PY_TYPE_DICT:
            entries = value.fields[abi.PYDICTOBJECT_ENTRIES_OFFSET]
            return {self.unwrap(self.read(entries, index * abi.DICTENTRY_SIZE + abi.DICTENTRY_KEY_OFFSET)):
                    self.unwrap(self.read(entries, index * abi.DICTENTRY_SIZE + abi.DICTENTRY_VALUE_OFFSET))
                    for index in range(value.fields[abi.PYDICTOBJECT_ENTRIES_USED_OFFSET])
                    if self.read(entries, index * abi.DICTENTRY_SIZE + abi.DICTENTRY_KEY_OFFSET) is not None}
        return value.value

    def iterate(self, obj):
        assert obj.alive and obj.leases > 0
        try:
            return self.make(iter(self.unwrap(obj)), abi.PY_TYPE_ITER)
        except Exception as error:
            self.error = (3, str(error))
            return None

    def getattr(self, obj, name):
        self.collect("callback")
        assert obj.alive and obj.leases > 0
        try:
            return self.wrap(getattr(self.unwrap(obj), name))
        except AttributeError as error:
            self.error = (6, str(error))
        except Exception as error:
            self.error = (2, str(error))
        return None

    def getitem(self, obj, key):
        self.collect("callback")
        assert obj.alive and obj.leases > 0
        try:
            return self.wrap(self.unwrap(obj)[self.unwrap(key)])
        except Exception as error:
            self.error = (2, str(error))
            return None

    def call_slots(self, callable_slot, args_slot, kwargs_slot, result_slot):
        self.collect("callback")
        fn = self.read(callable_slot, 0)
        assert fn.alive and fn.leases > 0
        try:
            self.write(result_slot, 0, self.wrap(self.unwrap(fn)(*self.unwrap(self.read(args_slot, 0)))))
            return 0
        except Exception as error:
            self.error = (2, str(error))
            return -1

    def runtime_error(self, helper, message):
        if self.error is None:
            self.error = (15, message)

    def swap_error(self, slot):
        error = self.read(slot, 0)
        self.write(slot, 0, self.error)
        self.error = error

    def store_field(self, owner, slot, value):
        old = self.read(slot, 0)
        self.write(slot, 0, value)
        if isinstance(old, Object):
            old.refs -= 1
        if isinstance(value, Object):
            value.refs += 1

    def input_roots(self, values):
        caller = Block()
        for index, value in enumerate(values):
            slot = self.add(caller, index * 8)
            self.roots[object()] = slot
            self.write(slot, 0, value)
        return caller

    def init(self, arguments=(), keywords=None, flags=4, exception=False, reenter=False):
        cls = self.make(None, abi.PY_TYPE_CLASS)
        cls.fields.update({abi.PYOBJECTHEADER_FLAGS_OFFSET: flags,
                          abi.PYCLASSOBJECT_N_FIELDS_OFFSET: 0,
                          abi.PYCLASSOBJECT_FIELD_NAMES_OFFSET: Block()})
        instance = self.make(None, abi.PY_TYPE_USER_CLASS_START)
        instance.fields[abi.PYINSTANCEOBJECT_CLS_OFFSET] = cls
        caller = self.input_roots((instance, cls, self.wrap(tuple(arguments)), self.wrap(keywords)))
        if reenter:
            def publish_existing_env():
                env = self.wrap({"\x00pcc.dict.items": {"reentered": 9}})
                receiver = self.read(caller, 0)
                self.store_field(receiver, self.add(receiver, abi.PYINSTANCEOBJECT_FIELDS_OFFSET), env)
            self.env_reentry = publish_existing_env
        name = "py_exception_subclass_init_slots" if exception else "py_dict_subclass_init_slots"
        status = self.ns[name](caller, self.add(caller, 8), self.add(caller, 16), self.add(caller, 24))
        assert len(self.roots) == 4 and self.depth == 0
        assert all(obj.leases == (1000 if obj is self.none else 0) for obj in self.objects if obj.alive)
        env = self.read(self.read(caller, 0), abi.PYINSTANCEOBJECT_FIELDS_OFFSET)
        return status, {} if env is None else self.unwrap(env), caller


@pytest.mark.parametrize("phase", ("register", "copy", "acquire", "callback", "release", "move", "drop", "unregister", "lock"))
def test_dict_initializer_real_bodies_move_every_boundary(phase):
    memory = DictMemory(phase)
    status, env, _ = memory.init(([('first', 1), ('second', 2)],), {"third": 3})
    assert status == 0 and memory.error is None
    assert env["\x00pcc.dict.items"] == dict(first=1, second=2, third=3)
    assert memory.collections > 0


@pytest.mark.parametrize("flags", (4, 6))
def test_dict_initializer_repeated_and_slots_storage(flags):
    memory = DictMemory("none")
    status, env, caller = memory.init(({"first": 1},), {"second": 2}, flags=flags)
    assert status == 0
    receiver = memory.read(caller, 0)
    env_before = memory.read(receiver, abi.PYINSTANCEOBJECT_FIELDS_OFFSET)
    # Calling again with no args keeps existing content and storage identity.
    memory.write(caller, 16, memory.wrap(()))
    memory.write(caller, 24, memory.none)
    assert memory.ns["py_dict_subclass_init_slots"](caller, memory.add(caller, 8), memory.add(caller, 16), memory.add(caller, 24)) == 0
    env = memory.read(memory.read(caller, 0), abi.PYINSTANCEOBJECT_FIELDS_OFFSET)
    assert env is env_before
    assert memory.unwrap(env)["\x00pcc.dict.items"] == {"first": 1, "second": 2}


def test_dict_initializer_partial_pair_failure_and_keyword_omission():
    memory = DictMemory("callback")
    status, env, _ = memory.init(([('first', 1), ('bad', 2, 3)],), {"later": 4})
    assert status == -1 and memory.error[0] == 2
    assert env["\x00pcc.dict.items"] == {"first": 1}


def test_exception_initializer_expands_only_exception_args():
    memory = DictMemory("release")
    status, env, _ = memory.init((1, "two"), {}, exception=True, flags=0)
    assert status == 0 and env == {"args": (1, "two")}
    memory = DictMemory("release")
    status, env, _ = memory.init((1,), {"bad": 2}, exception=True, flags=0)
    assert status == -1 and memory.error[0] == 3 and env == {}


@pytest.mark.parametrize("phase", ("register", "callback", "release"))
def test_initializer_preserves_environment_created_by_allocation_callback(phase):
    memory = DictMemory(phase)
    status, env, _ = memory.init(((('first', 1),),), {"later": 2}, reenter=True)
    assert status == 0 and memory.error is None
    assert env["\x00pcc.dict.items"] == {"reentered": 9, "first": 1, "later": 2}


def test_exact_dict_update_reuses_cached_hashes():
    calls = []
    class Key:
        def __hash__(self):
            calls.append(1)
            return 5
    key = Key()
    memory = DictMemory("callback")
    destination, source = memory.wrap({}), memory.wrap({key: 7})
    caller = memory.input_roots((destination, source))
    calls.clear()
    assert memory.ns["py_dict_update_slots"](caller, memory.add(caller, 8)) == 0
    assert calls == []
    assert memory.unwrap(memory.read(caller, 0))[key] == 7


@pytest.mark.parametrize("failure", (1, 2, 3, 4, 5, 6))
def test_initializer_partial_source_copy_failure_keeps_original_error(failure):
    memory = DictMemory("release", fail_copy=failure)
    status, env, _ = memory.init()
    assert status == -1 and memory.error[0] == 15


def test_initializer_restores_preexisting_exception_on_success():
    memory = DictMemory("release")
    memory.error = (7, "outer")
    status, env, _ = memory.init()
    assert status == 0 and memory.error == (7, "outer")


@pytest.mark.parametrize("fail", (False, True))
def test_mapping_keys_lookup_and_materialization_order(fail):
    events = []
    class Mapping:
        @property
        def keys(self):
            events.append("lookup")
            return self.produce
        def produce(self):
            events.append("first")
            yield "a"
            events.append("second")
            if fail:
                raise ValueError("keys failed")
            yield "b"
        def __getitem__(self, key):
            events.append("get:" + key)
            return 7
    memory = DictMemory("release")
    # Model a generator distinctly from an exact list, as the runtime does.
    original_wrap = memory.wrap
    def wrap(value):
        import types
        if isinstance(value, types.GeneratorType):
            return memory.make(value, abi.PY_TYPE_ITER)
        return original_wrap(value)
    memory.wrap = wrap
    status, env, _ = memory.init((Mapping(),))
    if fail:
        assert status == -1 and memory.error[1] == "keys failed"
        assert env["\x00pcc.dict.items"] == {}
        assert events == ["lookup", "lookup", "first", "second"]
    else:
        assert status == 0 and memory.error is None
        assert env["\x00pcc.dict.items"] == {"a": 7, "b": 7}
        assert events == ["lookup", "lookup", "first", "second", "get:a", "get:b"]



def test_pair_conversion_retains_extra_owner_until_iterator_finishes():
    memory = DictMemory("release")
    token = object()
    resumed = []
    def pair():
        yield "a"
        yield 1
        yield token
        owners = [obj for obj in memory.objects if obj.alive and obj.value is token]
        assert owners and all(obj.refs > 0 for obj in owners)
        resumed.append(1)
    status, env, _ = memory.init(((pair(),),))
    assert resumed == [1]
    assert status == -1 and memory.error[0] == 2
    assert env["\x00pcc.dict.items"] == {}


def test_full_entry_storage_growth_failure_does_not_retry_forever():
    memory = DictMemory("none")
    target = memory.wrap({})
    target.fields[abi.PYDICTOBJECT_ENTRIES_USED_OFFSET] = target.fields[abi.PYDICTOBJECT_CAPACITY_OFFSET]
    caller = memory.input_roots((target, memory.wrap("new"), 7))
    growths = []
    memory.ns["_maybe_grow"] = lambda owner: growths.append(owner) or -1
    assert memory.ns["py_dict_set_slots"](caller, memory.add(caller, 8), memory.add(caller, 16)) == -1
    assert growths == [target] and memory.error[0] == 19


def test_builtin_initializer_scratch_layout_preserves_slot_numbers():
    tree = ast.parse((ROOT / "py_protocol_runtime.py").read_text())
    layout = _scratch_layout(tree)
    assert layout["_BUILTIN_INIT_SLOT_COUNT"] * abi.C_POINTER_SIZE == 12 * abi.C_POINTER_SIZE
    assert layout["_BUILTIN_INIT_ERROR_SLOT"] == layout["_BUILTIN_INIT_SLOT_COUNT"] - 1
