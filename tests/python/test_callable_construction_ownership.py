"""Real construction helper bodies under deterministic moving/callback models.

Host models and IR contracts are not native five-collector qualification.
"""
from __future__ import annotations
import ast
from pathlib import Path
import pytest
from test_foreign_address_leases import _functions
from test_runtime_entry_handoff import SpecialCallModel

RUNTIME = Path(__file__).resolve().parents[2] / 'pcc/runtime/py/py_func.py'

class ConstructionModel(SpecialCallModel):
    def __init__(self, failure=None, cleanup_effect=False):
        super().__init__()
        self.failure = failure
        self.cleanup_effect = cleanup_effect
        self.original_error = 0
        self.captures_value = self.tuple(())
        self.signature_value = self.tuple(())
        self.captures = self.own(self.captures_value)
        self.signature = self.own(self.signature_value)
        self.output = self.empty(self.allocate(8))
        self.new_values = []
        self.sets = []
        self.callback = None
        self.cleanup_errors = []
        self.copy_count = 0
        self.new_lease_count = 0
        self.ns.update(py_tuple_new=self.new_wrapper, py_tuple_set_item=self.set_item,
                       pcc_gc_alloc=self.new_function, pcc_gc_store_ptr=self.member_store,
                       py_gc_track=lambda value: None, pcc_gc_publish_initialized=lambda value: None,
                       py_clear_exception=self.clear_error, pcc_gc_store_root=self.clear_root)
        acquire = self.ns['pcc_gc_foreign_lease_acquire']
        def acquire_new(slot):
            value = self.load(slot)
            if value in self.new_values:
                self.new_lease_count += 1
                assert slot in self.roots, 'NEW must be published before lease callback'
                if self.failure == 'lease' + str(self.new_lease_count):
                    return -1
            return acquire(slot)
        self.ns['pcc_gc_foreign_lease_acquire'] = acquire_new
        copy = self.ns['pcc_gc_root_copy_lease']
        def copy_source(dest, source):
            self.copy_count += 1
            self.fail_commit = failure == 'copy' + str(self.copy_count)
            return copy(dest, source)
        self.ns['pcc_gc_root_copy_lease'] = copy_source
        if failure and failure.startswith('register'):
            self.fail_registration = int(failure[-1])
        tree = ast.parse(RUNTIME.read_text())
        constants = [n for n in tree.body if isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id.startswith('_FUNC_CONSTRUCTION_') for t in n.targets)]
        exec(compile(ast.Module(body=constants, type_ignores=[]), str(RUNTIME), 'exec'), self.ns)
        _functions(RUNTIME, {'_func_construction_close', '_func_construction_lease_new',
                   'py_func_new_signature_slots', 'py_func_new_named', 'py_func_new_bound'}, self.ns)
        self.ns['py_func_new_named_raw'] = self.ns['py_func_new_named']

    def fail(self):
        self.error = self.exception('original construction failure')
        self.original_error = self.error

    def clear_error(self):
        if self.error:
            self.drop_reference(self.error)
        self.error = 0

    def clear_root(self, slot, value):
        old = self.load(slot)
        self.root_store(slot, value)
        if old and not value and self.cleanup_effect:
            self.clear_error()
            self.error = self.exception('cleanup replaced TLS')
            self.cleanup_errors.append(self.error)

    def new_wrapper(self, length):
        assert length == 2
        assert self.count(self.load(self.captures)) == self.count(self.load(self.signature)) == 1
        if self.failure == 'wrapper':
            self.fail()
            return 0
        value = self.tuple_new(length)
        self.new_values.append(value)
        return value

    def set_item(self, wrapper, index, value):
        assert self.count(wrapper) == self.count(value) == 1
        self.sets.append(index)
        if self.callback:
            self.callback()
        if self.failure == 'store' + str(index):
            self.fail()
            return
        self.tuple_set(wrapper, index, value)

    def new_function(self, size, tag, flags):
        assert size == 96 and tag == self.abi.PY_TYPE_FUNC
        if self.failure == 'function':
            self.fail()
            return 0
        value = self.make(tag)
        self.new_values.append(value)
        if self.failure == 'function_tls':
            self.fail()
        return value

    def member_store(self, owner, slot, value):
        self.root_store(slot, value)

    def drop_reference(self, value):
        if self.references[value] == 1 and self.load(value, 8) == self.abi.PY_TYPE_FUNC:
            captures = self.load(value, 64)
            if captures:
                super().drop_reference(captures)
        super().drop_reference(value)

    def invoke(self):
        return self.ns['py_func_new_signature_slots'](self.captures, self.signature, 1234, 'target', self.output)

    def balanced(self):
        assert not self.registered and self.active() == 0 and self.depth == 0
        self.root_store(self.output, 0)
        assert self.references[self.load(self.captures)] == self.references[self.load(self.signature)] == 1
        assert all(self.references[value] == 0 for value in self.new_values)
        assert all(self.references[value] == 0 for value in self.cleanup_errors)


def test_construction_preserves_wrapper_and_named_function_layout():
    m = ConstructionModel()
    assert m.invoke() == 0 and not m.error
    fn = m.load(m.output)
    assert m.load(fn, 56) == 1234 and m.load(fn, 72) == 'target'
    assert m.tuple_values(m.load(fn, 64)) == (m.load(m.captures), m.load(m.signature))
    assert m.sets == [0, 1]
    m.balanced()

@pytest.mark.parametrize('failure', ['register1', 'register2', 'register3', 'register4',
    'copy1', 'copy2', 'wrapper', 'lease1', 'store0', 'store1', 'function', 'function_tls', 'lease2'])
def test_construction_unwinds_partial_owners_and_preserves_error(failure):
    m = ConstructionModel(failure)
    assert m.invoke() == -1 and m.error
    if m.original_error:
        assert m.error == m.original_error
    if failure in ('function_tls', 'lease2'):
        assert m.load(m.output), 'failure may publish an owned result; caller must retire it'
    m.balanced()

@pytest.mark.parametrize('failure', ['store1', 'function_tls'])
def test_construction_preserves_original_tls_across_reentrant_cleanup(failure):
    m = ConstructionModel(failure, cleanup_effect=True)
    assert m.invoke() == -1 and m.error == m.original_error
    m.cleanup_effect = False
    m.balanced()


def test_construction_reloads_inputs_after_registration_and_allows_reentry():
    m = ConstructionModel()
    replacement = m.tuple(())
    m.after_registration = lambda: m.store(m.captures, 0, replacement)
    def callback():
        other = ConstructionModel()
        assert other.invoke() == 0
        other.balanced()
    m.callback = callback
    assert m.invoke() == 0
    assert m.tuple_values(m.load(m.load(m.output), 64))[0] == replacement
    m.balanced()


def test_construction_preserves_preexisting_error_without_side_effects():
    m = ConstructionModel()
    m.fail()
    assert m.invoke() == -1 and m.error == m.original_error
    assert m.registration_count == 0 and not m.new_values
    m.balanced()


@pytest.mark.parametrize('failure', ['function_tls', 'lease2'])
def test_partial_output_is_retired_exactly_once_by_caller(failure):
    m = ConstructionModel(failure)
    assert m.invoke() == -1
    output = m.load(m.output)
    original = m.error
    assert output and m.references[output] == 1
    assert m.references[m.load(m.captures)] == m.references[m.load(m.signature)] == 2
    m.root_store(m.output, 0)
    assert m.error == original and m.references[output] == 0
    assert m.references[m.load(m.captures)] == m.references[m.load(m.signature)] == 1
    assert not m.registered and not m.active()


def test_invalid_nonempty_output_is_not_an_unwind_owner():
    m = ConstructionModel()
    unrelated = m.make(m.abi.PY_TYPE_FUNC)
    m.store(m.output, 0, unrelated)
    assert m.invoke() == -1
    assert m.load(m.output) == unrelated and m.references[unrelated] == 1
    assert m.registration_count == 0
