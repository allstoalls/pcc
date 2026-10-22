"""Binary set-difference owners around the actual algebra core.

The moving model executes production functions; it is not native-GC proof.
"""
import ast
from pathlib import Path

import pytest

from tests.python.test_set_call_slot_roots import Memory, Block, Object, Collision
from tests.python.test_shared_call_binding import _emit
from tests.python.owned_regression_support import explicit_owned_runtime
from tests.python.test_slot_call_subscript_producers import _assert_immediate_publication

PORT = Path(__file__).parents[2] / 'pcc/runtime/py/py_set.py'


class BinaryMemory(Memory):
    def __init__(self, phase='register', **kwargs):
        super().__init__(phase, **kwargs)
        self.frames = {}
        self.pins = set()
        self.cleanup_error = False
        tree = ast.parse(PORT.read_text())
        names = {'_set_binary_owned', 'py_set_difference', '_set_constructor_drop', '_set_constructor_pin'}
        body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        assert len(body) == 4
        for node in body:
            node.decorator_list = []
        constants = {node.targets[0].id: ast.literal_eval(node.value)
                     for node in tree.body if isinstance(node, ast.Assign)
                     and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                     and node.targets[0].id.startswith(('_SET_BINARY_', '_SET_CONSTRUCTOR_'))}
        maps = {ast.literal_eval(node.value.args[0]): ast.literal_eval(node.value.args[1])
                for node in tree.body if isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
                and node.value.func.id == 'define_global_i32'
                and ast.literal_eval(node.value.args[0]).startswith('pcc_set_binary_')}
        assert maps['pcc_set_binary_owned_map'] == constants['_SET_BINARY_SLOT_COUNT']
        assert maps['pcc_set_binary_borrowed_map'] == -constants['_SET_BINARY_INCOMING_COUNT']
        assert constants['_SET_BINARY_ERROR'] >= 10  # Core slot9 is a live equality candidate.
        self.ns.update(constants)
        self.ns.update(c_ptr=object, PYOBJECTHEADER_FLAGS_OFFSET=12,
                       global_addr=lambda name: maps[name],
                       load_i32=lambda ptr, offset: self.read(ptr, offset) or 0,
                       pcc_gc_frame_enter=self.frame_enter, pcc_gc_frame_leave=self.frame_leave,
                       pcc_gc_root_copy_borrowed_lease=self.copy,
                       pcc_gc_pin=self.pin, pcc_gc_take_pinned_slot=self.take,
                       py_tls_exc_swap_slot=self.swap,
                       pcc_gc_store_root=self.drop,
                       pcc_platform_abort=lambda: pytest.fail('invalid set owner lease'))
        exec(compile(ast.Module(body=body, type_ignores=[]), str(PORT), 'exec'), self.ns)

    def frame_enter(self, count, base):
        assert base not in self.frames
        handles = []
        if count > 0:
            assert all(self.read(base, index * 8) is None for index in range(count))
        # Registration publishes the full incoming frame before its first poll.
        for index in range(abs(count)):
            handle = object()
            self.roots[handle] = self.add(base, index * 8)
            handles.append(handle)
        self.frames[base] = handles
        self.collect('register')

    def frame_leave(self, base):
        self.collect('unregister')
        for handle in self.frames.pop(base):
            value = self.read(self.roots.pop(handle), 0)
            assert value is None or value in self.pins

    def pin(self, value):
        assert value.alive
        if value not in self.pins:
            value.leases += 1
            self.pins.add(value)
        value.fields[12] = 64

    def take(self, slot, prior):
        assert not self.frames and self.depth == 0
        value = self.read(slot, 0)
        self.write(slot, 0, None)
        if value is not None:
            assert value.alive and value.refs == 1 and value in self.pins
            if not prior:
                self.pins.remove(value)
                value.leases -= 1
                value.fields[12] = 0
        return value

    def swap(self, slot):
        value = self.read(slot, 0)
        self.write(slot, 0, self.error)
        self.error = value

    def drop(self, slot, value):
        super().store_root(slot, value)
        if self.cleanup_error:
            self.error = (7, 'cleanup error')

    def binary(self, left, right, operation=2):
        caller = Block()
        for index, value in enumerate((self.wrap(left), self.wrap(right))):
            self.write(caller, index * 8, value)
            self.roots[object()] = self.add(caller, index * 8)
        self.caller = caller
        result = self.ns['_set_binary_owned'](self.read(caller, 0), self.read(caller, 8), operation)
        assert len(self.roots) == 2 and not self.frames and self.depth == 0 and not self.pins
        assert all(obj.leases == (1000 if obj is self.none else 0) for obj in self.objects if obj.alive)
        assert self.read(caller, 0).refs == 1 and self.read(caller, 8).refs == 1
        return result


@pytest.mark.parametrize('phase', ('register', 'copy', 'acquire', 'callback', 'release', 'drop', 'unregister'))
@pytest.mark.parametrize('values', (({1, 2, 3}, {2}), ({Collision(1), Collision(2)}, {Collision(2)})))
def test_actual_difference_owners_survive_movement(phase, values):
    memory = BinaryMemory(phase)
    result = memory.binary(*values)
    assert memory.error is None
    assert memory.values(result) == values[0] - values[1]
    assert memory.values(memory.read(memory.caller, 0)) == values[0]
    assert memory.values(memory.read(memory.caller, 8)) == values[1]
    assert result is not memory.read(memory.caller, 0)
    if phase != 'callback':
        assert memory.moves > 0


@pytest.mark.parametrize('which', (1, 2))
def test_difference_partial_operand_copy_failure_retires_owners(which):
    memory = BinaryMemory('copy', fail_copy=which)
    assert memory.binary({1, 2}, {2}) is None
    assert memory.error[0] == 7
    assert all(obj.refs == 1 for obj in (memory.read(memory.caller, 0), memory.read(memory.caller, 8)))


def test_difference_result_allocation_failure_preserves_inputs():
    memory = BinaryMemory('release', fail_allocation=3)
    assert memory.binary({1, 2}, {2}) is None
    assert memory.error[0] == 19


def test_difference_pending_error_survives_cleanup_reentrancy():
    memory = BinaryMemory('drop')
    original = memory.ns['_set_call_apply']
    def failed(*args):
        assert original(*args) == 1
        memory.error = (3, 'equality error')
        memory.cleanup_error = True
        return 0
    memory.ns['_set_call_apply'] = failed
    assert memory.binary({1, 2}, {2}) is None
    assert memory.error == (3, 'equality error')


def test_difference_revalidates_after_equality_rehash():
    memory = BinaryMemory('callback')
    def rehash():
        # Mutate the selected input table while equality runs; leases protect
        # addresses, while the algebra core reloads the actual table owner.
        right = memory.read(memory.caller, 8)
        replacement = Block()
        replacement.fields = right.fields[40].fields.copy()
        right.fields[40] = replacement
    memory.on_equal = rehash
    result = memory.binary({Collision(1), Collision(2)}, {Collision(2)})
    assert memory.error is None and memory.on_equal is None
    assert {value.value for value in memory.values(result)} == {1}


def test_difference_preserves_right_candidate_equality_direction():
    memory = BinaryMemory('callback')
    left, right = Collision(1), Collision(1)
    observed = []
    equal = memory.ns['py_obj_eq']
    def record(lhs, rhs):
        observed.append((lhs.value, rhs.value))
        return equal(lhs, rhs)
    memory.ns['py_obj_eq'] = record
    result = memory.binary({left}, {right})
    assert memory.values(result) == set()
    assert observed and all(lhs is right and rhs is left for lhs, rhs in observed)


@pytest.mark.parametrize('left,right,expected', (([1], {1}, set()), ({1, 2}, [1], {1, 2})))
def test_raw_difference_preserves_existing_invalid_input_contract(left, right, expected):
    memory = BinaryMemory('release')
    result = memory.binary(left, right)
    assert memory.error is None and memory.values(result) == expected


def test_unqualified_algebra_operation_is_rejected():
    memory = BinaryMemory('drop')
    assert memory.binary({1}, {2}, operation=7) is None
    assert memory.error[0] == 7


@pytest.mark.parametrize('site', ('argument', 'return', 'default'))
def test_set_subtraction_uses_actual_generic_new_result(site):
    prefix = 'def take(*, value, **extras):\n    return value\n'
    if site == 'argument':
        body = '    return take(value=left - right)\n'
    elif site == 'default':
        body = '    def saved(value=left - right):\n        return value\n    return saved()\n'
    else:
        body = '    return left - right\n'
    text = _emit(prefix + 'def probe(left: set, right: set):\n' + body)
    _assert_immediate_publication(text, 'py_obj_sub')


PROGRAM = '''\
import gc

events = []
class Key:
    def __init__(self, number):
        self.number = number
    def __hash__(self):
        return 7
    def __eq__(self, other):
        gc.collect()
        return self.number == other.number

class BadKey:
    def __hash__(self):
        return 7
    def __eq__(self, other):
        gc.collect()
        raise ValueError('equality-error')

def take(*, value, later=None, **extras):
    gc.collect()
    return value

def later():
    events.append('later')
    raise TypeError('later-error')

def returned(left, right):
    return left - right

def main():
    first, second = Key(1), Key(2)
    left, right = {first, second}, {Key(2)}
    result = take(value=left - right)
    gc.collect()
    assert len(result) == 1 and next(iter(result)) is first
    assert len(left) == 2 and len(right) == 1
    assert next(iter(returned(left, right))) is first
    def saved(value=left - right):
        gc.collect()
        return value
    assert saved() is saved() and next(iter(saved())) is first
    assert len(left - left) == 0 and len(left - set()) == 2
    before = len(events)
    try:
        take(value={BadKey()} - {BadKey()}, later=later())
    except ValueError as error:
        assert str(error) == 'equality-error'
    else:
        raise AssertionError('lost equality error')
    assert len(events) == before
    try:
        take(value=left - right, later=later())
    except TypeError as error:
        assert str(error) == 'later-error'
    else:
        raise AssertionError('lost later error')
    gc.collect()
    assert len(events) == before + 1 and len(left) == 2
    print('SET_DIFFERENCE_OWNERS_OK')

main()
'''


def test_set_difference_reference_program(capsys):
    exec(compile(PROGRAM, '<set-difference-reference>', 'exec'), {})
    assert capsys.readouterr().out == 'SET_DIFFERENCE_OWNERS_OK\n'


@pytest.mark.integration
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_set_difference_native_five_gc(python_program_compiler, request,
                                        explicit_owned_runtime, tmp_path, capfd):
    from tests.python.owned_regression_support import assert_owned_program
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(PROGRAM, 'SET_DIFFERENCE_OWNERS_OK\n', tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime,
                         capfd, provenance_probe='2')
    assert (tmp_path / 'compiler-wrapper.stderr').read_text() == ''
