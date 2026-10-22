"""Execute the production iterator-extension transaction at moving boundaries."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.python.test_foreign_address_leases import _functions


@dataclass(eq=False)
class _Object:
    name: str
    refs: int = 1
    generation: int = 0
    immortal: bool = False

    def pointer(self):
        return self, self.generation


class _ExtensionModel:
    def __init__(self, failure, relocate, alias):
        self.failure, self.relocate = failure, relocate
        self.memory, self.frames, self.leases = {}, {}, {}
        self.objects, self.items, self.events = [], [], []
        self.pending, self.fresh = None, None
        self.next_address, self.next_count, self.copy_count = 100, 0, 0
        self.destination = self.new('destination', fresh=False)
        self.source = self.destination if alias else self.new('source', fresh=False)
        self.stop = self.new('StopIterationClass', fresh=False, immortal=True)
        self.ns = {
            '_LIST_EXTEND_DESTINATION': 0, '_LIST_EXTEND_SOURCE': 1,
            '_LIST_EXTEND_ITERATOR': 2, '_LIST_EXTEND_ITEM': 3,
            '_LIST_EXTEND_ERROR': 4, '_LIST_EXTEND_SLOT_COUNT': 5,
            '_LIST_EXTEND_SLOT_BYTES': 8,
            'null': lambda: None, 'ptr_is_null': lambda value: value is None,
            'stack_alloc': self.alloc, 'memset': self.zero,
            'ptr_add': lambda base, offset: base + offset,
            'load_ptr': self.load, 'load_i64': self.load,
            'store_ptr': self.store, 'store_i64': self.store,
            'global_addr': lambda name: -2 if 'borrowed' in name else 5,
            'cstr': lambda value: value,
            'pcc_gc_frame_enter': self.enter, 'pcc_gc_frame_leave': self.leave,
            'pcc_gc_root_copy_borrowed_lease': self.copy,
            'pcc_gc_foreign_lease_acquire': self.acquire,
            'pcc_gc_foreign_lease_release': self.release,
            'pcc_gc_note_slot_write_barrier': self.barrier,
            'pcc_gc_store_root': self.clear,
            'py_obj_iter': self.iterator, 'py_obj_next': self.next,
            'py_list_append': self.append,
            'py_err_occurred': lambda: self.pending is not None,
            'py_exc_builtin_class': self.stop_class,
            'py_exc_matches': self.matches,
            'py_exc_new': lambda kind, text: self.new(text, fresh=False),
            'py_raise_owned': self.raise_owned,
            'py_tls_exc_swap_slot': self.swap,
            'py_clear_exception': self.clear_error,
            'pcc_platform_abort': lambda: pytest.fail('invariant abort'),
        }
        _functions(Path(__file__).resolve().parents[2] / 'pcc/runtime/py/py_list.py',
                   {'_list_extend_drop', '_list_extend_acquire', '_list_extend_iterable'}, self.ns)

    def new(self, name, *, fresh=True, immortal=False):
        obj = _Object(name, immortal=immortal)
        self.objects.append(obj)
        value = obj.pointer()
        if fresh:
            assert self.fresh is None
            self.fresh = value
        return value

    def check(self, value):
        if value is not None:
            obj, generation = value
            assert obj.refs > 0 and generation == obj.generation, 'stale or disposed raw pointer'
        return value

    def boundary(self):
        assert self.fresh is None, 'runtime boundary before NEW result publication'
        if not self.relocate:
            return
        pinned = {self.load(slot, 0)[0] for slot in self.leases}
        moved = {obj for obj in self.objects if obj.refs and not obj.immortal and obj not in pinned}
        for obj in moved:
            obj.generation += 1
        def update(value):
            if isinstance(value, tuple) and value[0] in moved:
                return value[0].pointer()
            return value
        # Authoritative frame/TLS/container slots are rewritten, SSA is not.
        for base, count in self.frames.items():
            for offset in range(abs(count)):
                address = base + offset * 8
                self.memory[address] = update(self.memory.get(address))
        self.pending = update(self.pending)
        self.items = [update(value) for value in self.items]

    def alloc(self, size):
        self.next_address += 100
        return self.next_address

    def zero(self, base, value, size):
        for offset in range(0, size, 8):
            self.memory[base + offset] = None

    def load(self, base, offset):
        return self.memory.get(base + offset)

    def store(self, base, offset, value):
        self.memory[base + offset] = value
        if self.fresh is not None and value == self.fresh:
            assert any(count > 0 and address <= base + offset < address + count * 8
                       for address, count in self.frames.items())
            self.fresh = None

    def enter(self, count, base):
        if count > 0:
            assert all(self.load(base, index * 8) is None for index in range(count))
        self.frames[base] = count
        self.boundary()

    def leave(self, base):
        self.boundary()
        assert all(self.load(base, index * 8) is None for index in range(abs(self.frames[base])))
        self.frames.pop(base)

    def acquire(self, slot):
        self.boundary()
        value = self.check(self.load(slot, 0))
        if value is None:
            return 0
        if self.failure == 'lease-' + value[0].name:
            return -1
        assert slot not in self.leases
        self.leases[slot] = 1
        return 1

    def release(self, slot, token):
        self.boundary()
        if token:
            assert self.leases.pop(slot) == 1
        return 0

    def copy(self, destination, source):
        self.boundary()
        self.copy_count += 1
        if self.failure == 'copy-' + str(self.copy_count):
            return -1
        value = self.check(self.load(source, 0))
        value[0].refs += 1
        self.store(destination, 0, value)
        return self.acquire(destination)

    def barrier(self, owner, slot, value):
        self.boundary()
        assert self.check(value) == self.load(slot, 0)

    def drop(self, value):
        if value is None:
            return
        self.check(value)
        obj = value[0]
        obj.refs -= 1
        assert obj.refs >= 0
        if not obj.refs:
            self.events.append('dispose-' + obj.name)
            if obj.name == 'iterator':
                # A finalizer may collect and replace TLS while primary error
                # and all surviving owners must remain available.
                self.boundary()
                self.clear_error()
                self.pending = self.new('disposal-error', fresh=False)

    def clear(self, slot, value):
        assert value is None
        self.boundary()
        prior = self.load(slot, 0)
        self.store(slot, 0, None)
        self.drop(prior)

    def stop_class(self, tag):
        self.boundary()
        assert tag == 8
        return self.stop

    def iterator(self, source):
        self.boundary()
        self.check(source)
        if self.failure == 'iter':
            self.pending = self.new('iter-error', fresh=False)
            return None
        return self.new('iterator')

    def next(self, iterator):
        self.boundary()
        self.check(iterator)
        self.next_count += 1
        if self.next_count == 1:
            return self.new('item')
        self.pending = self.new('next-error' if self.failure == 'next' else 'StopIteration', fresh=False)
        return None

    def append(self, destination, item):
        self.boundary()
        self.check(destination)
        self.check(item)
        if self.failure == 'append':
            self.pending = self.new('append-error', fresh=False)
            return
        item[0].refs += 1
        self.items.append(item)

    def matches(self, error, cls):
        self.boundary()
        self.check(error)
        self.check(cls)
        return int(error[0].name == 'StopIteration')

    def raise_owned(self, error):
        self.clear_error()
        self.pending = error

    def clear_error(self):
        self.boundary()
        value, self.pending = self.pending, None
        self.drop(value)

    def swap(self, slot):
        self.boundary()
        value = self.load(slot, 0)
        self.store(slot, 0, self.pending)
        self.pending = value

    def run(self):
        self.ns['_list_extend_iterable'](self.destination, self.source)


@pytest.mark.parametrize('relocate', (False, True))
@pytest.mark.parametrize('alias', (False, True))
@pytest.mark.parametrize('failure', (
    None, 'copy-1', 'copy-2', 'iter', 'next', 'append',
    'lease-iterator', 'lease-item', 'lease-StopIteration',
))
def test_list_extend_iteration_owns_every_live_value(failure, relocate, alias):
    model = _ExtensionModel(failure, relocate, alias)
    model.run()
    assert model.frames == {} and model.leases == {} and model.fresh is None
    assert model.destination[0].refs == model.source[0].refs == 1
    if failure is None:
        assert model.pending is None
        assert len(model.items) == 1 and model.items[0][0].refs == 1
        assert 'dispose-iterator' in model.events
    else:
        expected = {
            'copy-1': 'cannot retain list extension input',
            'copy-2': 'cannot retain list extension input',
            'iter': 'iter-error', 'next': 'next-error', 'append': 'append-error',
            'lease-iterator': 'cannot lease list extension value',
            'lease-item': 'cannot lease list extension value',
            'lease-StopIteration': 'StopIteration',
        }
        assert model.pending[0].name == expected[failure]
    survivors = {obj.name for obj in model.objects if obj.refs and not obj.immortal}
    allowed = {'destination', 'source'}
    allowed.update(value[0].name for value in model.items)
    if model.pending:
        allowed.add(model.pending[0].name)
    assert survivors <= allowed


_EXTEND_CALLER_CASES = {
    "literal": "def probe(source):\n    target = []\n    return target.extend(source)\n",
    "annotated": "def probe(target: list[int], source):\n    return target.extend(source)\n",
    "dynamic": "def probe(target, source):\n    return target.extend(source)\n",
    "raising_iterator": """def values():
    yield 1
    raise ValueError("iterator failed")
def probe():
    target = []
    try:
        target.extend(values())
    except ValueError:
        return target
    else:
        raise AssertionError("void call swallowed iterator failure")
""",
}


def _extend_caller_ir(source, target):
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.type_infer import infer_module
    module = infer_module(parse_and_lift(source, "extend_caller.py", "extend_caller"))
    codegen = L1CodeGen(module, ir_scaffold_mode="on")
    codegen._target_triple = target
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    return str(codegen.generate(module))


@pytest.mark.parametrize("name", _EXTEND_CALLER_CASES)
@pytest.mark.parametrize("target", (
    "x86_64-unknown-linux-gnu", "arm64-apple-darwin",
    "aarch64-unknown-linux-gnu", "x86_64-pc-windows-msvc",
))
def test_extend_caller_owner_boundaries_reach_owned_objects(name, target):
    """Backend validation covers both static and dynamic call/result owners.

    This emits objects only. The unchanged scalar_predicates native control
    in test_owned_walk_native.py executes the iterator exception regression.
    """
    from pcc.backend.owned_object_emit import emit_owned_object
    text = _extend_caller_ir(_EXTEND_CALLER_CASES[name], target)
    assert "strict.nolib.stub:" not in text
    assert emit_owned_object(text, target)


def test_extend_exception_reference_retains_partial_mutation():
    namespace = {}
    exec(_EXTEND_CALLER_CASES["raising_iterator"], namespace)
    assert namespace["probe"]() == [1]


@pytest.mark.parametrize("name", _EXTEND_CALLER_CASES)
def test_extend_checks_pending_error_before_any_cleanup_or_next_call(name):
    from pcc.backend.self_backend_parse import parse_self_backend_module
    from pcc.backend.self_backend_kernel import get_indexed_function_kernel
    module = parse_self_backend_module(_extend_caller_ir(
        _EXTEND_CALLER_CASES[name], "x86_64-unknown-linux-gnu",
    ))
    probe = next(function for function in module.functions
                 if function.name == "user_extend_caller_probe")
    matched = 0
    blocks = get_indexed_function_kernel(probe).materialize_legacy_blocks(probe)
    for block in blocks:
        calls = [instruction for instruction in block.instructions
                 if instruction.kind == "call"]
        for index, call in enumerate(calls):
            if call.data[2] != "py_list_extend":
                continue
            matched += 1
            # Any lease/root retirement may run a finalizer, and any later
            # user call can overwrite the iterator's TLS exception. Inspect
            # the pending error first and branch before either can occur.
            assert index + 1 < len(calls), "extend does not inspect its exception"
            error_check = calls[index + 1]
            assert error_check.data[2] == "py_err_occurred"
            assert index + 2 == len(calls), "effectful call precedes the error edge"
            assert block.terminator.kind == "br_cond"
    assert matched == 1
