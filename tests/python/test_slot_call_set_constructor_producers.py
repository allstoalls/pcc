"""Set constructor result ownership; distinct frozenset semantics stay separate."""
from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

from tests.python.test_shared_call_binding import _emit
from tests.python.test_slot_call_subscript_producers import _assert_immediate_publication
from tests.python.owned_regression_support import assert_owned_program, explicit_owned_runtime


@pytest.mark.parametrize('expression,runtime', (
    ('set()', 'py_set_new'), ('set(item)', 'py_set_from_iterable'),
    ('frozenset()', 'py_set_new'), ('frozenset(item)', 'py_set_from_iterable'),
))
@pytest.mark.parametrize('site', ('return', 'argument', 'default', 'attribute', 'later-error'))
def test_set_constructor_publishes_before_cleanup(expression, runtime, site):
    prefix = 'def take(*, value, later=None, **extras):\n    return value\ndef fail():\n    raise ValueError("later")\n'
    if site == 'return':
        body = '    return ' + expression + '\n'
    elif site == 'default':
        body = '    def target(value=' + expression + '):\n        return value\n    return target()\n'
    elif site == 'attribute':
        body = '    class Holder:\n        value = ' + expression + '\n    return Holder\n'
    else:
        body = '    return take(value=' + expression + (', later=fail()' if site == 'later-error' else '') + ')\n'
    text = _emit(prefix + 'def probe(item):\n' + body)
    _assert_immediate_publication(text, runtime)


class _ConstructorModel:
    def __init__(self, failure, relocate):
        self.failure, self.relocate = failure, relocate
        self.memory, self.frames, self.leases = {}, {}, {}
        self.refs, self.pins = {'source': 1}, set()
        self.next_address = 100
        self.pending = None
        self.primary = None
        self.result_name = None
        self.disposal_observed = False
        self.fresh = None

    def alloc(self, size):
        self.next_address += 100
        return self.next_address

    def load(self, base, offset):
        return self.memory.get(base + offset)

    def store(self, base, offset, value):
        self.memory[base + offset] = value
        if self.fresh is not None and self.fresh == value:
            assert self.owned(base + offset), 'NEW result must be stored into a registered owner'
            self.fresh = None

    def owned(self, slot):
        return any(count > 0 and base <= slot < base + count * 8
                   for base, count in self.frames.items())

    def boundary(self):
        assert self.fresh is None, 'parking boundary before NEW-result publication'

    def enter(self, count, base):
        self.boundary()
        if count > 0:
            assert all(self.load(base, index * 8) is None for index in range(count))
            if self.relocate:
                self.refs['moved-source'] = self.refs.pop('source')
                for address, value in list(self.memory.items()):
                    if value == 'source':
                        self.memory[address] = 'moved-source'
        self.frames[base] = count

    def acquire(self, slot):
        self.boundary()
        assert self.owned(slot)
        value = self.load(slot, 0)
        if value == self.result_name and self.failure == 'lease':
            return -1
        if value is None:
            return 0
        if value == 'result' and self.relocate:
            self.refs['moved-result'] = self.refs.pop(value)
            self.memory[slot] = 'moved-result'
            self.result_name = 'moved-result'
        self.leases[slot] = 1
        return 1

    def release(self, slot, token):
        self.boundary()
        if token:
            assert self.leases.pop(slot) == 1
        return 0

    def copy(self, destination, source):
        self.boundary()
        if self.failure == 'copy':
            return -1
        value = self.load(source, 0)
        assert value == ('moved-source' if self.relocate else 'source')
        self.refs[value] += 1
        self.store(destination, 0, value)
        return self.acquire(destination)

    def new(self):
        self.boundary()
        if self.failure == 'allocation':
            return None
        self.refs['result'] = 1
        self.result_name = self.fresh = 'result'
        return 'result'

    def update(self, result, source):
        self.boundary()
        assert result == self.result_name
        assert any(self.load(slot, 0) == result for slot in self.leases)
        assert any(self.load(slot, 0) == source for slot in self.leases)
        # Iterator disposal may collect/reenter while the returned set is live.
        self.disposal_observed = True
        assert self.refs[result] == 1
        if self.failure == 'iteration':
            self.pending = self.primary = 'iteration-error'

    def drop(self, slot, empty):
        self.boundary()
        assert empty is None and slot not in self.leases
        value = self.load(slot, 0)
        if self.primary is not None:
            assert any(self.owned(address) and held == self.primary
                       for address, held in self.memory.items())
        self.store(slot, 0, None)
        if value is not None:
            self.refs[value] -= 1
            # A cleanup safepoint may run another object's finalizer and replace
            # TLS. The constructor must already have saved its primary error.
            self.pending = 'cleanup-error'

    def swap(self, slot):
        self.boundary()
        self.pending, self.memory[slot] = self.load(slot, 0), self.pending

    def environment(self):
        def memset(base, value, size):
            for offset in range(0, size, 8):
                self.store(base, offset, None)
        def leave(base):
            self.boundary()
            if self.result_name and self.refs.get(self.result_name) == 1:
                assert self.result_name in self.pins
            del self.frames[base]
        def take(slot, prior):
            self.boundary()
            assert not self.frames and not self.leases
            value = self.load(slot, 0)
            if value is not None:
                assert value in self.pins and self.refs[value] == 1
                self.pins.remove(value)
            self.store(slot, 0, None)
            return value
        def error(kind, message):
            self.boundary()
            if self.pending is None:
                self.pending = self.primary = message
        return dict(c_ptr=object, c_abi_export=lambda name: lambda fn: fn,
                    _SET_CONSTRUCTOR_SOURCE=0, _SET_CONSTRUCTOR_RESULT=1,
                    _SET_CONSTRUCTOR_ERROR=2, _SET_CONSTRUCTOR_SLOT_COUNT=3,
                    _SET_CONSTRUCTOR_SLOT_BYTES=8, PYOBJECTHEADER_FLAGS_OFFSET=12,
                    stack_alloc=self.alloc, memset=memset, store_ptr=self.store,
                    load_ptr=self.load, store_i64=self.store, load_i64=self.load,
                    load_i32=lambda value, offset: 64 if value in self.pins else 0,
                    ptr_add=lambda base, offset: base + offset,
                    ptr_is_null=lambda value: int(value is None),
                    is_tagged_int=lambda value: 0, null=lambda: None, cstr=lambda text: text,
                    global_addr=lambda name: -1 if 'borrowed' in name else 3,
                    pcc_gc_frame_enter=self.enter, pcc_gc_frame_leave=leave,
                    pcc_gc_root_copy_borrowed_lease=self.copy,
                    pcc_gc_foreign_lease_acquire=self.acquire,
                    pcc_gc_foreign_lease_release=self.release,
                    pcc_gc_store_root=self.drop,
                    pcc_gc_load_ptr=lambda owner, slot: self.load(slot, 0),
                    pcc_gc_pin=lambda value: self.pins.add(value),
                    pcc_gc_take_pinned_slot=take,
                    pcc_py_gc_minor_graph_lock=self.boundary,
                    pcc_py_gc_minor_graph_unlock=self.boundary,
                    py_tls_exc_swap_slot=self.swap,
                    py_clear_exception=lambda: setattr(self, 'pending', None),
                    py_err_occurred=lambda: int(self.pending is not None),
                    py_set_new=self.new, py_set_update=self.update,
                    _set_call_error=error,
                    pcc_platform_abort=lambda: pytest.fail('constructor owner invariant failed'))


@pytest.mark.parametrize('relocate', (False, True))
@pytest.mark.parametrize('failure', (None, 'copy', 'allocation', 'lease', 'iteration'))
def test_actual_set_constructor_result_lifetime(failure, relocate):
    path = Path(__file__).parents[2] / 'pcc/runtime/py/py_set.py'
    tree = ast.parse(path.read_text())
    names = {'_set_constructor_drop', '_set_constructor_pin', 'py_set_from_iterable'}
    selected = ast.Module(body=[node for node in tree.body
                               if isinstance(node, ast.FunctionDef) and node.name in names],
                          type_ignores=[])
    assert len(selected.body) == 3
    model = _ConstructorModel(failure, relocate)
    env = model.environment()
    constants = {node.targets[0].id: ast.literal_eval(node.value)
                 for node in tree.body if isinstance(node, ast.Assign)
                 and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                 and node.targets[0].id.startswith('_SET_CONSTRUCTOR_')}
    maps = {ast.literal_eval(node.value.args[0]): ast.literal_eval(node.value.args[1])
            for node in tree.body if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
            and node.value.func.id == 'define_global_i32'
            and ast.literal_eval(node.value.args[0]).startswith('pcc_set_constructor_')}
    assert maps['pcc_set_constructor_owned_map'] == constants['_SET_CONSTRUCTOR_SLOT_COUNT']
    env.update(constants)
    env['global_addr'] = lambda name: maps[name]
    exec(compile(selected, str(path), 'exec'), env)
    result = env['py_set_from_iterable']('source')
    assert not model.frames and not model.leases and not model.pins
    assert model.refs['moved-source' if relocate else 'source'] == 1
    if failure is None:
        assert result == ('moved-result' if relocate else 'result')
        assert model.refs[result] == 1 and model.pending is None
        assert model.disposal_observed
    else:
        assert result is None
        if model.result_name is not None:
            assert model.refs[model.result_name] == 0
        expected = {'copy': 'cannot retain set constructor input',
                    'allocation': 'cannot allocate set',
                    'lease': 'cannot lease set constructor result',
                    'iteration': 'iteration-error'}
        assert model.pending == expected[failure]


PROGRAM = textwrap.dedent('''\
    import gc

    events = []
    class Token:
        def __hash__(self):
            gc.collect()
            return 17

    class Source:
        def __init__(self, token):
            self.token = token
        def __iter__(self):
            gc.collect()
            events.append('iter')
            return iter((self.token,))
        def __del__(self):
            events.append('source-disposed')
            gc.collect()

    class Bad:
        def __iter__(self):
            gc.collect()
            raise ValueError('iter-error')

    def take(*, value, later=None, **extras):
        gc.collect()
        return value

    def later():
        events.append('later')
        raise TypeError('later-error')

    def returned(token):
        return set(Source(token))

    def main():
        token = Token()
        result = take(value=set(Source(token)))
        gc.collect()
        assert len(result) == 1 and next(iter(result)) is token
        assert events == ['iter', 'source-disposed']
        assert next(iter(returned(token))) is token
        def saved(value=set(Source(token))):
            gc.collect()
            return value
        assert saved() is saved()
        assert next(iter(saved())) is token
        assert len(set()) == 0
        before = events.count('later')
        try:
            take(value=set(Bad()), later=later())
        except ValueError as error:
            assert str(error) == 'iter-error'
        else:
            raise AssertionError('iterator failure was lost')
        assert events.count('later') == before
        try:
            take(value=set(Source(token)), later=later())
        except TypeError as error:
            assert str(error) == 'later-error'
        else:
            raise AssertionError('later argument failure was lost')
        gc.collect()
        assert events.count('source-disposed') == 4
        print('SET_CONSTRUCTOR_OWNERS_OK')

    main()
''')


@pytest.mark.integration
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_set_constructor_owners_native_five_gc(python_program_compiler, request,
                                              explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(PROGRAM, 'SET_CONSTRUCTOR_OWNERS_OK\n', tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime,
                         capfd, provenance_probe='2')
    assert (tmp_path / 'compiler-wrapper.stderr').read_text() == ''
