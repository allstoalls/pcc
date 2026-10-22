"""Execute real set bitwise bodies against counted owners and moving slots.

The host model establishes causal ownership at each boundary. Native tests need
an explicitly source-matched archive and are separate five-GC qualification.
Threaded entry polls additionally require the enclosing dispatcher's address
leases; the entry tests model that precondition explicitly.
"""

import ast
from pathlib import Path

import pytest

from tests.python.test_set_binary_result_roots import BinaryMemory
from tests.python.test_set_call_slot_roots import Block, Collision, Object, PORT
from tests.python.owned_regression_support import explicit_owned_runtime


OPERATIONS = {
    'intersection': (1, lambda left, right: left & right),
    'union': (0, lambda left, right: left | right),
    'symmetric_difference': (6, lambda left, right: left ^ right),
}


class BitwiseMemory(BinaryMemory):
    def __init__(self, phase='register', source=PORT, **kwargs):
        self.operation_active = False
        super().__init__(phase, **kwargs)
        names = {'_set_binary_owned', 'py_set_intersection', 'py_set_union',
                 'py_set_symmetric_difference', '_set_call_apply'}
        body = [node for node in ast.parse(Path(source).read_text()).body
                if isinstance(node, ast.FunctionDef) and node.name in names]
        for node in body:
            node.decorator_list = []
        exec(compile(ast.Module(body=body, type_ignores=[]), str(source), 'exec'), self.ns)

    def new_set(self):
        if self.operation_active:
            self.collect('allocation')
        return super().new_set()

    def binary(self, left, right, operation='intersection', alias=False,
               entry_poll=False, caller_leases=False):
        caller = Block()
        first = self.wrap(left)
        second = first if alias else self.wrap(right)
        if alias and isinstance(first, Object):
            first.refs += 1
        for index, value in enumerate((first, second)):
            self.write(caller, index * 8, value)
            self.roots[object()] = self.add(caller, index * 8)
        self.caller = caller
        lease_tokens = []
        if caller_leases:
            lease_tokens = [self.acquire(self.add(caller, offset)) for offset in (0, 8)]
        if entry_poll:
            self.collect('entry')
        self.operation_active = True
        try:
            result = self.ns['py_set_' + operation](first, second)
        finally:
            self.operation_active = False
        for offset, token in zip((0, 8), lease_tokens):
            assert self.release(self.add(caller, offset), token) == 0
        assert len(self.roots) == 2 and not self.frames and self.depth == 0 and not self.pins
        assert all(obj.leases == (1000 if obj is self.none else 0)
                   for obj in self.objects if obj.alive)
        assert self.read(caller, 0).refs == (2 if alias else 1)
        assert self.read(caller, 8).refs == (2 if alias else 1)
        return result


@pytest.mark.parametrize('operation', OPERATIONS)
@pytest.mark.parametrize('phase', ('allocation', 'register', 'copy', 'acquire', 'lock',
                                   'callback', 'release', 'drop', 'unregister'))
@pytest.mark.parametrize('values', (({1, 2, 3}, {2, 4}),
                                   ({Collision(1), Collision(2)}, {Collision(2), Collision(3)})))
def test_actual_bitwise_exports_survive_movement(operation, phase, values):
    memory = BitwiseMemory(phase)
    result = memory.binary(*values, operation)
    assert memory.error is None
    assert memory.values(result) == OPERATIONS[operation][1](*values)
    for index, value in enumerate(values):
        assert memory.values(memory.read(memory.caller, index * 8)) == value
        assert result is not memory.read(memory.caller, index * 8)
    if phase not in ('allocation', 'callback'):
        assert memory.moves > 0


@pytest.mark.parametrize('operation', OPERATIONS)
def test_threaded_entry_requires_dispatcher_address_leases(operation):
    unleased = BitwiseMemory('entry')
    with pytest.raises(AssertionError):
        unleased.binary({1, 2}, {2, 3}, operation, entry_poll=True)
    memory = BitwiseMemory('entry')
    result = memory.binary({1, 2}, {2, 3}, operation,
                           entry_poll=True, caller_leases=True)
    assert memory.error is None
    assert memory.values(result) == OPERATIONS[operation][1]({1, 2}, {2, 3})


@pytest.mark.parametrize('operation', OPERATIONS)
@pytest.mark.parametrize('phase', ('register', 'release', 'drop', 'unregister'))
def test_bitwise_aliases_retain_each_source_reference(operation, phase):
    memory = BitwiseMemory(phase)
    source = {1, 2, 3}
    result = memory.binary(source, source, operation, alias=True)
    assert memory.error is None
    assert memory.values(result) == OPERATIONS[operation][1](source, source)
    assert memory.read(memory.caller, 0) is memory.read(memory.caller, 8)
    assert memory.values(memory.read(memory.caller, 0)) == source


@pytest.mark.parametrize('operation', OPERATIONS)
@pytest.mark.parametrize('which', (1, 2, 3, 4))
def test_bitwise_partial_copy_failure_retires_all_owners(operation, which):
    memory = BitwiseMemory('copy', fail_copy=which)
    result = memory.binary({Collision(1), Collision(2)}, {Collision(2)}, operation)
    assert result is None and memory.error is not None


@pytest.mark.parametrize('operation', OPERATIONS)
def test_bitwise_allocation_failure_preserves_sources(operation):
    memory = BitwiseMemory('release', fail_allocation=3)
    assert memory.binary({1, 2}, {2}, operation) is None
    assert memory.error[0] == 19


@pytest.mark.parametrize('operation', OPERATIONS)
def test_bitwise_pending_error_identity_survives_cleanup_reentrancy(operation):
    memory = BitwiseMemory('drop')
    error = (3, 'original equality error')
    original = memory.ns['_set_call_apply']

    def failed(*args):
        assert original(*args) == 1
        memory.error = error
        memory.cleanup_error = True
        return 0

    memory.ns['_set_call_apply'] = failed
    assert memory.binary({1, 2}, {2}, operation) is None
    assert memory.error is error


@pytest.mark.parametrize('operation', OPERATIONS)
def test_bitwise_reloads_tables_after_equality_mutation(operation):
    memory = BitwiseMemory('callback')

    def rehash():
        for offset in (0, 8):
            source = memory.read(memory.caller, offset)
            replacement = Block()
            replacement.fields = source.fields[40].fields.copy()
            source.fields[40] = replacement

    memory.on_equal = rehash
    left, right = {Collision(1), Collision(2)}, {Collision(2), Collision(3)}
    result = memory.binary(left, right, operation)
    assert memory.error is None and memory.on_equal is None
    assert memory.values(result) == OPERATIONS[operation][1](left, right)


@pytest.mark.parametrize('operation', OPERATIONS)
def test_bitwise_uses_cached_source_hashes(operation):
    memory = BitwiseMemory('callback')
    memory.ns['py_obj_hash'] = lambda _value: pytest.fail('exact set key was rehashed')
    result = memory.binary({Collision(1), Collision(2)}, {Collision(2)}, operation)
    assert memory.error is None and result is not None


@pytest.mark.parametrize('operation', ('intersection', 'symmetric_difference'))
def test_bitwise_preserves_operand_equality_direction(operation):
    memory = BitwiseMemory('callback')
    left, right = Collision(1), Collision(1)
    seen = []
    original = memory.ns['py_obj_eq']

    def equal(lhs, rhs):
        seen.append((lhs.value, rhs.value))
        return original(lhs, rhs)

    memory.ns['py_obj_eq'] = equal
    result = memory.binary({left}, {right}, operation)
    assert memory.error is None
    assert seen[0] == (right, left) and seen[0][0] is right and seen[0][1] is left
    if operation == 'symmetric_difference':
        assert seen[-1][0] is left and seen[-1][1] is right
        assert memory.values(result) == set()
    else:
        selected = next(iter(memory.values(result)))
        assert selected is left


def test_binary_intersection_keeps_full_left_scan_after_saturation():
    memory = BitwiseMemory('callback')
    lookups = []
    original = memory.ns['_set_call_lookup']

    def lookup(slots, tokens, target, hash_value, mode):
        if target == memory.ns['_SET_BINARY_RIGHT'] and mode == 0:
            lookups.append(memory.read(slots, 7 * 8))
        return original(slots, tokens, target, hash_value, mode)

    memory.ns['_set_call_lookup'] = lookup
    result = memory.binary({1, 2}, {1}, 'intersection')
    assert memory.error is None and memory.values(result) == {1}
    assert lookups == [1, 2]


@pytest.mark.parametrize('operation', ('intersection', 'symmetric_difference'))
@pytest.mark.parametrize('left,right', (([1], {1, 2}), ({1, 2}, [1]), ([1], [2])))
def test_existing_raw_bitwise_invalid_input_contract(operation, left, right):
    memory = BitwiseMemory('release')
    result = memory.binary(left, right, operation)
    expected = set() if operation == 'intersection' else (left if isinstance(left, set) else right if isinstance(right, set) else set())
    assert memory.error is None and memory.values(result) == expected


@pytest.mark.parametrize('left,right', (([1], {1}), ({1}, [1])))
def test_new_raw_union_rejects_nonset_operands(left, right):
    memory = BitwiseMemory('drop')
    assert memory.binary(left, right, 'union') is None
    assert memory.error[0] == 3


PROGRAM = '''\
import gc
import weakref

events = []
saved_error = ValueError('same-error')
class Key:
    def __init__(self, number):
        self.number = number
    def __hash__(self):
        return 7
    def __eq__(self, other):
        gc.collect()
        return self.number == other.number
    def __del__(self):
        events.append(self.number)
        gc.collect()

class BadKey:
    def __hash__(self):
        return 7
    def __eq__(self, other):
        gc.collect()
        raise saved_error

class Reflected:
    def __rand__(self, other):
        return other
    def __ror__(self, other):
        return other
    def __rxor__(self, other):
        return other

class SetSubclass(set):
    def __and__(self, other):
        return 'sub-and'
    def __or__(self, other):
        return 'sub-or'
    def __xor__(self, other):
        return 'sub-xor'

def take(*, value, later=None, **extras):
    gc.collect()
    return value

def later():
    events.append('later')
    raise TypeError('later-error')

def returned(left, right):
    return left & right, left | right, left ^ right

def exercise():
    first, second, third = Key(1), Key(2), Key(3)
    watched = weakref.ref(first)
    left, right = {first, second}, {second, third}
    both = take(value=left & right)
    joined = take(value=left | right)
    either = take(value=left ^ right)
    assert both == {second} and joined == {first, second, third}
    assert either == {first, third}
    assert len(left) == len(right) == 2
    assert both is not left and joined is not left and either is not left
    assert returned(left, right) == (both, joined, either)
    def saved(a=left & right, b=left | right, c=left ^ right):
        gc.collect()
        return a, b, c
    assert saved() == (both, joined, either)
    assert saved()[0] is saved()[0]
    assert left & left == left and left | left == left and left ^ left == set()
    assert left & Reflected() is left
    assert left | Reflected() is left
    assert left ^ Reflected() is left
    child = SetSubclass()
    assert child & left == 'sub-and'
    assert child | left == 'sub-or'
    assert child ^ left == 'sub-xor'
    left & right
    left | right
    left ^ right
    try:
        take(value=left ^ right, later=later())
    except TypeError as error:
        assert str(error) == 'later-error'
    else:
        raise AssertionError('lost later error')
    return watched

def check_errors():
    for operation in range(3):
        a, b = {BadKey()}, {BadKey()}
        before = len(events)
        try:
            if operation == 0:
                take(value=a & b, later=later())
            elif operation == 1:
                take(value=a | b, later=later())
            else:
                take(value=a ^ b, later=later())
        except ValueError as error:
            assert error is saved_error
        else:
            raise AssertionError('lost equality error')
        assert len(events) == before

def main():
    watched = exercise()
    gc.collect()
    gc.collect()
    assert watched() is None
    assert events.count(1) == events.count(2) == events.count(3) == 1
    assert events.count('later') == 1
    check_errors()
    print('SET_BITWISE_OWNERS_OK')

main()
'''


def test_bitwise_reference_program(capsys):
    exec(compile(PROGRAM, '<set-bitwise-reference>', 'exec'), {})
    assert capsys.readouterr().out == 'SET_BITWISE_OWNERS_OK\n'


@pytest.mark.integration
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_bitwise_native_five_gc(python_program_compiler, request,
                                explicit_owned_runtime, tmp_path, capfd):
    from tests.python.owned_regression_support import assert_owned_program
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(PROGRAM, 'SET_BITWISE_OWNERS_OK\n', tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime,
                         capfd, provenance_probe='2')
    assert (tmp_path / 'compiler-wrapper.stderr').read_text() == ''
