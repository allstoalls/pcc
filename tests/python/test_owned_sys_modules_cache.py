"""Execute the live-cache owner and compiled registry with bounded memory ABIs.

These component checks execute the actual runtime Python bodies. Native
execution, collector relocation and extension loading require separate gates.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.python.test_compiled_module_parent_publication import _Block, _Memory


CACHE = Path(__file__).resolve().parents[2] / "pcc/runtime/py/py_module_attrs_runtime.py"
EXTENSION = CACHE.with_name("py_extension_loader_runtime.py")


class CacheMemory(_Memory):
    def __init__(self):
        super().__init__()
        self.frames = []
        self.leases = {}
        self.next_lease = 1
        self.registered_globals = set()
        self.mutex_bits = 0
        self.globals["py_None"] = self.none
        self.ns.update(
            C_POINTER_SIZE=8, _SM_KEY=0, _SM_VALUE=1, _SM_CURRENT=2,
            _SM_ERROR=3, _SM_OUTPUT=4, _SM_CACHE=5, _SM_COUNT=6,
            ptr_eq=lambda a, b: a is b, is_tagged_int=lambda value: isinstance(value, int),
            load_i64=self.read_integer, store_i64=self.write, stack_alloc=self.malloc,
            memset=lambda ptr, value, size: ptr.bytes.__setitem__(slice(0, size), bytes([value])*size),
            global_addr=lambda name: name,
            atomic_load_i64=lambda *_args: self.mutex_bits,
            atomic_cas_i64=self.cas_mutex,
            ptr_to_int=lambda value: value, int_to_ptr=lambda value: value,
            pcc_mutex_new=lambda: 1, pcc_mutex_free=lambda _value: None,
            pcc_mutex_lock=lambda _value: 0, pcc_mutex_unlock=lambda _value: 0,
            py_dict_new=lambda: self.object("dict"), py_dict_get=self.dict_get,
            py_dict_set=lambda d, k, v: self.setdict(d, k.text, v), py_dict_del=self.dict_del,
            pcc_gc_frame_enter=self.frame_enter,
            pcc_gc_frame_leave=self.frame_leave, pcc_gc_store_root=self.store_root,
            pcc_gc_note_write_barrier=lambda *_args: None,
            pcc_gc_note_slot_write_barrier=lambda *_args: None,
            pcc_gc_take_pinned_slot=self.take_slot, py_tls_exc_swap_slot=self.swap_error,
            pcc_gc_load_ptr=lambda _owner, slot: self.read(slot, 0),
            pcc_gc_scheduler_root_register_handle=self.register_global,
            pcc_gc_root_copy_lease=self.copy_lease,
            pcc_gc_foreign_lease_acquire=self.acquire,
            pcc_gc_foreign_lease_release=self.release,
            pcc_py_gc_minor_graph_lock=lambda: None,
            pcc_py_gc_minor_graph_unlock=lambda: None,
            pcc_platform_abort=lambda: (_ for _ in ()).throw(AssertionError("lease corruption")),
            py_obj_setattr=self.current_setattr,
            py_exc_new=lambda tag, message: (tag, self.text(message)),
        )
        tree = ast.parse(CACHE.read_text(), filename=str(CACHE))
        nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and (node.name.startswith(("_sys_modules", "py_sys_modules"))
                      or node.name == "_module_name_key")]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(CACHE), "exec"), self.ns)
        self.ns.update(
            py_sys_modules_owner_adopt=self.ns["_sys_modules_adopt"],
            py_sys_modules_owner_drop=self.ns["_sys_modules_drop"],
            py_sys_modules_owner_finish=self.ns["_sys_modules_finish"],
        )
        slot = self.ns["_sys_modules_cache_slot"]()
        self.module_cache = self.read(slot, 0)

    def register_global(self, slot):
        self.registered_globals.add(slot)
        return self.malloc(8)

    def acquire(self, slot):
        value = self.read(slot, 0)
        if not isinstance(value, _Block):
            return 0
        assert value.alive
        token = self.next_lease
        self.next_lease += 1
        self.leases[token] = value
        return token

    def release(self, slot, token):
        if token == 0:
            return 0
        value = self.leases.pop(token)
        assert value is self.read(slot, 0) and value.alive
        return 0

    def copy_lease(self, dest, source):
        value = self.read(source, 0)
        assert self.read(dest, 0) is None
        self.incref(value)
        self.write(dest, 0, value)
        return self.acquire(dest)

    def cas_mutex(self, slot, offset, expected, value, *_orders):
        old = self.mutex_bits
        if old == expected:
            self.mutex_bits = value
        return old

    def pin(self, value):
        assert value.alive
        value.pin = True

    def write(self, value, offset, item):
        if isinstance(value, str):
            assert offset == 0
            self.globals[value] = item
            return
        obj, base = self.pointer(value)
        if obj.kind != "raw" and base + offset == 12:
            obj.pin = bool(item & 64)
        else:
            super().write(value, offset, item)

    def read(self, value, offset):
        if isinstance(value, str):
            assert offset == 0
            return self.globals.get(value)
        return super().read(value, offset)

    def read_integer(self, value, offset):
        obj, base = self.pointer(value)
        if base + offset in obj.fields:
            return obj.fields[base + offset]
        return int.from_bytes(obj.bytes[base + offset:base + offset + 8], "little", signed=True)

    def dict_get(self, mapping, key):
        value = mapping.fields.get(key.text)
        self.incref(value)
        return value

    def dict_del(self, mapping, key):
        value = mapping.fields.pop(key.text, None)
        self.decref(value)
        return 0 if value is not None else -1

    def frame_leave(self, slots):
        assert self.frames.pop() is slots

    def frame_enter(self, mapping, slots):
        assert mapping in ("pcc_sys_modules_frame_map", "pcc_compiled_module_frame_map", "pcc_extension_module_frame_map")
        assert isinstance(slots, _Block) and len(slots.bytes) == 48
        self.frames.append(slots)

    def store_root(self, slot, value):
        old = self.read(slot, 0)
        self.write(slot, 0, value)
        self.decref(old)

    def take_slot(self, slot, prior):
        value = self.read(slot, 0)
        self.write(slot, 0, None)
        if isinstance(value, _Block):
            assert value.alive and value.pin
            value.pin = bool(prior)
        return value

    def swap_error(self, slot):
        current = self.read(slot, 0)
        self.write(slot, 0, self.error)
        self.error = current

    def current_setattr(self, module, name, value):
        self.gate("parent")
        if self.fail == "publication":
            self.raise_error("publication failed")
            return -1
        if module.kind == "module":
            self.setdict(module.fields[24], self.text(name), value)
        else:
            self.setdict(module, self.text(name), value)
        return 0


def test_live_map_identity_lookup_never_initializes_missing_module():
    m = CacheMemory()
    called = []
    m.register("provider", lambda: called.append(True))
    first = m.ns["py_sys_modules"]()
    second = m.ns["py_sys_modules"]()
    assert first is second is m.module_cache
    assert m.ns["py_sys_modules_find"](m.cstr("provider")) is None
    assert called == [] and m.attrs == {} and not m.frames


def test_replacement_and_none_entries_are_authoritative_for_import():
    m = CacheMemory()
    m.register("provider")
    original = m.import_module("provider")
    replacement = m.object("replacement")
    m.setdict(m.module_cache, "provider", replacement)
    assert m.import_module("provider") is replacement
    assert original.alive and not original.pin
    m.decref(original)
    assert not original.alive
    m.setdict(m.module_cache, "provider", m.none)
    assert m.ns["py_sys_modules_find"](m.cstr("provider")) is m.none
    assert m.import_module("provider") is None
    assert m.error.text == (21, "import of provider halted; None in sys.modules")
    assert not m.frames


def test_deletion_releases_cache_owner_and_reinitialization_fails_explicitly():
    m = CacheMemory()
    calls = []
    m.register("provider", lambda: calls.append("init"))
    module = m.import_module("provider")
    m.decref(module)
    m.decref(m.module_cache.fields.pop("provider"))
    assert not module.alive
    assert m.ns["py_sys_modules_find"](m.cstr("provider")) is None
    assert m.import_module("provider") is None
    assert calls == ["init"]
    assert "per-instance globals" in m.error.text[1]


@pytest.mark.parametrize("replacement", (False, True))
def test_circular_import_and_failure_rollback_remove_failed_name(replacement):
    m = CacheMemory()
    escaped = []
    other = m.object("replacement")
    def initialize():
        escaped.append(m.import_module("provider"))
        if replacement:
            m.setdict(m.module_cache, "provider", other)
        m.raise_error("original initializer error")
    m.register("provider", initialize)
    assert m.import_module("provider") is None
    assert m.error.text == "original initializer error"
    assert "provider" not in m.module_cache.fields
    assert escaped[0].alive and not escaped[0].pin
    assert not m.frames


def test_success_returns_replacement_created_during_initialization():
    m = CacheMemory()
    replacement = m.object("replacement")
    m.register("provider", lambda: m.setdict(m.module_cache, "provider", replacement))
    assert m.import_module("provider") is replacement
    assert not m.live_modules() and not replacement.pin and not m.frames


def test_parent_namespace_publishes_the_live_replacement():
    m = CacheMemory()
    replacement = m.object("replacement")
    m.register("pkg")
    m.register("pkg.child", lambda: m.setdict(m.module_cache, "pkg.child", replacement))
    assert m.import_module("pkg.child") is replacement
    assert m.attrs["pkg"].fields["child"] is replacement
    assert not replacement.pin and not m.frames


def test_replaced_parent_receives_child_in_its_current_namespace():
    m = CacheMemory()
    m.register("pkg")
    m.register("pkg.child")
    original = m.import_module("pkg")
    replacement = m.object("replacement")
    m.setdict(m.module_cache, "pkg", replacement)
    child = m.import_module("pkg.child")
    assert replacement.fields["child"] is child
    assert "child" not in m.namespace(original)
    assert not m.frames and not m.leases


def test_removal_during_successful_initialization_raises_lookup_error():
    m = CacheMemory()
    m.register("provider", lambda: m.decref(m.module_cache.fields.pop("provider")))
    assert m.import_module("provider") is None
    assert m.error.text == (4, "provider")
    assert not m.live_modules() and not m.frames


def test_parent_publication_failure_keeps_cache_and_original_error():
    m = CacheMemory()
    m.register("pkg")
    m.register("pkg.child")
    m.fail = "publication"
    assert m.import_module("pkg.child") is None
    assert m.error.text == "publication failed"
    child = m.module_cache.fields["pkg.child"]
    assert child.refs == 1 and not child.pin and not m.frames


def test_key_allocation_failure_is_not_misreported_as_cache_miss():
    m = CacheMemory()
    m.fail = "string"
    assert m.ns["py_sys_modules_find"](m.cstr("missing")) is None
    assert m.error.text[0] == 19 and not m.frames


def test_pending_exception_survives_finalizer_during_cache_rollback():
    m = CacheMemory()
    original = []
    def initialize():
        m.raise_error("initializer")
        original.append(m.error)
    m.register("provider", initialize)
    m.cleanup_callback = lambda: m.raise_error("finalizer")
    assert m.import_module("provider") is None
    assert m.error is original[0] and m.error.text == "initializer"
    assert not m.live_modules() and not m.frames


class ExtensionMemory(CacheMemory):
    def __init__(self, callback=None):
        super().__init__()
        self.exec_callback = callback
        self.loads = 0
        self.closed = []
        self.definition = self.malloc(32)
        self.definition.fields[0] = "pcc_capi_moduledef_marker"
        self.ns.update(
            pcc_platform_getenv=lambda _name: None,
            pcc_platform_write=lambda *_args: 0,
            dynamic_library_open_global=self.open_library,
            dynamic_library_close=lambda handle: self.closed.append(handle),
            dynamic_library_symbol=lambda _handle, _symbol: lambda: self.definition,
            dlerror=lambda: None, call_ptr0=lambda fn: fn(),
            pcc_capi_is_moduledef=lambda value: int(value is self.definition),
            pcc_capi_module_from_def=lambda _definition: self.new_module(),
            pcc_capi_module_run_exec_slots=self.run_exec,
        )
        tree = ast.parse(EXTENSION.read_text(), filename=str(EXTENSION))
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(EXTENSION), "exec"), self.ns)

    def open_library(self, path):
        self.loads += 1
        return self.malloc(8)

    def run_exec(self, definition, module):
        assert self.module_cache.fields["extension"] is module
        if self.exec_callback:
            self.exec_callback(self, module)
        return -1 if self.error else 0

    def load_extension(self):
        return self.ns["py_native_extension_import"](self.cstr("extension"), self.cstr("test-extension.so"))


def test_extension_cache_uses_same_identity_replacements_and_none():
    m = ExtensionMemory()
    module = m.load_extension()
    assert m.module_cache.fields["extension"] is module and not module.pin
    assert m.load_extension() is module and m.loads == 1
    replacement = m.object("replacement")
    m.setdict(m.module_cache, "extension", replacement)
    assert m.ns["py_native_extension_import_by_name"](m.cstr("extension")) is replacement
    m.setdict(m.module_cache, "extension", m.none)
    assert m.load_extension() is None and m.error.text[0] == 21
    assert m.loads == 1 and not m.frames


@pytest.mark.parametrize("replace", (False, True))
def test_extension_exec_rollback_preserves_partial_owner_and_removes_replacement(replace):
    escaped = []
    replacement = []
    def execute(m, module):
        escaped.append(m.ns["py_native_extension_import_by_name"](m.cstr("extension")))
        assert escaped[0] is module
        if replace:
            replacement.append(m.object("replacement"))
            m.setdict(m.module_cache, "extension", replacement[0])
        m.raise_error("extension exec failed")
    m = ExtensionMemory(execute)
    assert m.load_extension() is None
    assert m.error.text == "extension exec failed"
    assert escaped[0].alive and not escaped[0].pin
    assert "extension" not in m.module_cache.fields
    assert m.closed == [] and not m.frames
    assert m.ns["_find_code_owner"](m.cstr("extension")) is not None


def test_extension_deletion_does_not_reuse_a_hidden_private_module_owner():
    m = ExtensionMemory()
    module = m.load_extension()
    m.decref(module)
    m.decref(m.module_cache.fields.pop("extension"))
    assert not module.alive
    assert m.load_extension() is None
    assert "reinitialization unavailable" in m.error.text[1]
    assert m.loads == 1 and not m.frames


def test_parent_importing_then_deleting_child_cannot_bypass_reinitialization_guard():
    m = CacheMemory()
    calls = []
    escaped = []
    m.register("pkg.child", lambda: calls.append("child"))
    def initialize_parent():
        escaped.append(m.import_module("pkg.child"))
        m.decref(m.module_cache.fields.pop("pkg.child"))
    m.register("pkg", initialize_parent)
    assert m.import_module("pkg.child") is None
    assert calls == ["child"]
    assert "per-instance globals" in m.error.text[1]
    assert escaped[0].alive and not m.frames and not m.leases


def test_initializing_compiled_creator_survives_alias_header_unpin_and_collection():
    m = CacheMemory()
    seen = []
    def initialize():
        alias = m.import_module("provider")
        seen.append(alias)
        # A consumer retiring its own temporary header pin cannot retire the
        # initializer's independent counted lease.
        alias.pin = False
        m.phase = "initializer"
        m.gate("initializer")
        assert alias.alive
    m.register("provider", initialize)
    result = m.import_module("provider")
    assert result is seen[0] and result.alive and not result.pin
    assert m.moves > 0 and not m.frames and not m.leases


def test_extension_creator_keeps_independent_lease_through_exec_callback():
    def execute(m, module):
        module.pin = False
        m.phase = "extension-exec"
        m.gate("extension-exec")
        assert module.alive
    m = ExtensionMemory(execute)
    module = m.load_extension()
    assert module.alive and not module.pin
    assert m.moves > 0 and not m.frames and not m.leases
    assert all(value.kind != "raw" for value in m.leases.values())


def test_public_cache_reloads_registered_owner_after_alias_unpin_and_relocation():
    m = CacheMemory()
    alias = m.ns['py_sys_modules']()
    alias.pin = True
    m.unpin(alias)
    m.decref(alias)
    old = m.globals['pcc_sys_modules_cache']
    assert 'pcc_sys_modules_cache' in m.registered_globals
    copied = []
    original_copy = m.copy_lease
    def move_before_copy(destination, source):
        assert source == 'pcc_sys_modules_cache'
        assert old not in m.leases.values() and not old.pin
        moved = m.object('dict')
        moved.fields, moved.refs = old.fields.copy(), old.refs
        for name in m.registered_globals:
            if m.globals.get(name) is old:
                m.globals[name] = moved
        old.alive = False
        copied.append(moved)
        return original_copy(destination, source)
    m.ns['pcc_gc_root_copy_lease'] = move_before_copy
    result = m.ns['py_sys_modules']()
    assert result is copied[0] and result.alive and not old.alive
    assert not m.frames and not m.leases


def test_registered_dotted_module_without_compiled_parent_remains_importable():
    m = CacheMemory()
    m.register('filtered_parent.child')
    module = m.import_module('filtered_parent.child')
    assert module is m.module_cache.fields['filtered_parent.child']
    assert m.error is None
    assert 'filtered_parent' not in m.module_cache.fields
    assert not m.frames and not m.leases


def test_missing_parent_probe_preserves_real_allocation_error():
    m = CacheMemory()
    m.register('filtered_parent.child')
    original = m.ns['py_sys_modules_find']
    errors = []
    def find(name):
        if m.text(name) == 'filtered_parent':
            m.raise_error('parent lookup allocation failure')
            errors.append(m.error)
            return None
        return original(name)
    m.ns['py_sys_modules_find'] = find
    assert m.import_module('filtered_parent.child') is None
    assert m.error is errors[-1]
    assert m.error.text == 'parent lookup allocation failure'
    assert not m.frames and not m.leases


def test_absent_parent_does_not_replace_initializer_error():
    m = CacheMemory()
    seen = []
    def initialize():
        m.raise_error('child initializer failure')
        seen.append(m.error)
    m.register('filtered_parent.child', initialize)
    assert m.import_module('filtered_parent.child') is None
    assert m.error is seen[0]
    assert 'filtered_parent.child' not in m.module_cache.fields
    assert not m.frames and not m.leases
