"""The binary protocol callback publishes owners before parking and disposal."""
from __future__ import annotations

import ast
from pathlib import Path
import textwrap

import pytest

from tests.python.owned_regression_support import (
    assert_owned_program,
    explicit_owned_runtime,
)


class BinaryOwnerModel:
    """Execute real helper bodies against moving roots and observable decrefs."""

    def __init__(self, result_kind, failure, relocate, prior_pin):
        self.result_kind = result_kind
        self.failure = failure
        self.relocate = relocate
        self.memory = {}
        self.frames = {}
        self.address = 100
        self.objects = {name: {'refs': 1, 'pin': 0, 'leases': 0, 'name': name}
                        for name in ('method', 'self', 'arg')}
        self.objects['method']['pin'] = prior_pin
        self.current = {name: name for name in self.objects}
        self.tokens = {}
        self.items = {}
        self.pending = None
        self.result = None
        self.moves = []
        self.disposed = []
        self.observed_result_during_disposal = False
        self.callback_count = 0
        self.copy_count = 0
        self.method_owner_removed = False

    def alloc(self, size):
        self.address += 100
        return self.address

    def load(self, base, offset):
        return self.memory.get(base + offset)

    def store(self, base, offset, value):
        self.memory[base + offset] = value

    def registered(self, address, owning=False):
        return any(base <= address < base + abs(count) * 8
                   for base, count in self.frames.items() if not owning or count > 0)

    def park(self, reason):
        if not self.relocate:
            return
        # Only currently registered roots and actual heap fields get updated.
        # A C/Python local copied before this operation must not be reused.
        for old, obj in list(self.objects.items()):
            if obj['refs'] <= 0 or obj['pin'] or obj['leases']:
                continue
            slots = [address for address, value in self.memory.items()
                     if value == old and self.registered(address)]
            if not slots:
                continue
            new = obj['name'] + '@' + str(len(self.moves) + 1)
            self.objects[new] = self.objects.pop(old)
            self.current[obj['name']] = new
            for slot in slots:
                self.memory[slot] = new
            for items in self.items.values():
                for index, value in list(items.items()):
                    if value == old:
                        items[index] = new
            if old in self.items:
                self.items[new] = self.items.pop(old)
            if self.pending == old:
                self.pending = new
            if self.result == old:
                self.result = new
            self.moves.append((reason, old, new))

    def new(self, name):
        self.park('allocation-' + name)
        if self.failure == name:
            self.pending = name + '-error'
            return None
        self.objects[name] = {'refs': 1, 'pin': 0, 'leases': 0, 'name': name}
        self.current[name] = name
        return name

    def acquire(self, slot):
        assert self.registered(slot, owning=True)
        # The first operation after NEW is allowed to relocate it. Without the
        # preceding producer store, the result has no authoritative owner.
        value = self.load(slot, 0)
        if value is not None:
            assert value in self.objects and self.objects[value]['refs'] > 0
        self.park('lease-acquire')
        value = self.load(slot, 0)
        if value is None:
            return 0
        if self.failure == 'lease' and self.objects[value]['name'] == 'result':
            return -1
        self.objects[value]['leases'] += 1
        self.tokens[slot] = 1
        return 1

    def release(self, slot, token):
        self.park('lease-release')
        if token:
            assert self.tokens.pop(slot) == token
            value = self.load(slot, 0)
            self.objects[value]['leases'] -= 1
            assert self.objects[value]['leases'] >= 0
        return 0

    def copy(self, destination, source):
        assert self.registered(source) and self.registered(destination, owning=True)
        assert self.load(destination, 0) is None
        self.park('copy')
        self.copy_count += 1
        if self.failure == 'copy' and self.copy_count == 2:
            return -1
        value = self.load(source, 0)
        self.objects[value]['refs'] += 1
        self.store(destination, 0, value)
        return self.acquire(destination)

    def decrement(self, value):
        obj = self.objects[value]
        obj['refs'] -= 1
        assert obj['refs'] >= 0
        if obj['refs']:
            return
        assert obj['leases'] == 0
        self.disposed.append(obj['name'])
        for child in self.items.pop(value, {}).values():
            self.decrement(child)
        if obj['name'] in ('args', 'method'):
            if self.result is not None and self.failure not in ('lease',):
                assert self.objects[self.result]['refs'] > 0
                assert any(self.load(base, 4 * 8) == self.result
                           for base, count in self.frames.items() if count == 6)
                self.observed_result_during_disposal = True
            self.pending = 'disposal-error'

    def drop(self, slot, replacement):
        assert replacement is None
        assert self.registered(slot, owning=True)
        self.park('drop')
        value = self.load(slot, 0)
        self.store(slot, 0, None)
        if value is not None:
            self.decrement(value)

    def set_item(self, args, index, value):
        assert self.objects[args]['leases'] and self.objects[value]['leases']
        self.park('tuple-set')
        if self.failure == 'set' and index == 1:
            self.pending = 'set-error'
            return
        self.items.setdefault(args, {})[index] = value
        self.objects[value]['refs'] += 1

    def call(self, method, args):
        assert self.objects[method]['leases'] and self.objects[args]['leases']
        self.callback_count += 1
        self.park('callback')
        # A callback may remove its own class method while executing. The
        # callee owner alone then protects it through method disposal.
        self.objects[method]['refs'] -= 1
        self.method_owner_removed = True
        if self.failure in ('result', 'missing-error'):
            if self.failure == 'result':
                self.pending = 'result-error'
            return None
        if self.result_kind == 'new':
            result = self.new('result')
        elif self.result_kind == 'args':
            result = args
            self.objects[result]['refs'] += 1
        else:
            result = self.current[self.result_kind]
            self.objects[result]['refs'] += 1
        self.result = result
        return result

    def environment(self):
        def memset(base, value, size):
            for offset in range(0, size, 8):
                self.store(base, offset, None)
        def enter(count, base):
            # Caller-owned input addresses are valid on entry. Interruption
            # while registering the owning frame moves the borrowed operands.
            if count > 0:
                self.park('owning-frame-registration')
            self.frames[base] = count
        def leave(base):
            self.park('frame-leave')
            assert base in self.frames
            del self.frames[base]
        def require(value, helper, message):
            if value is None and self.pending is None:
                self.pending = message
            return value
        def swap(slot):
            self.park('tls-swap')
            self.pending, self.memory[slot] = self.load(slot, 0), self.pending
        def pin(value):
            self.objects[value]['pin'] = 64
        def take(slot, prior):
            value = self.load(slot, 0)
            if value is not None:
                self.objects[value]['pin'] = prior
            self.store(slot, 0, None)
            return value
        def load_gc(owner, slot):
            self.park('root-read')
            return self.load(slot, 0)
        env = dict(c_ptr=object, C_POINTER_SIZE=8, PYOBJECTHEADER_FLAGS_OFFSET=12,
                   _PROTOCOL_BINARY_METHOD=0, _PROTOCOL_BINARY_SELF=1,
                   _PROTOCOL_BINARY_ARG=2, _PROTOCOL_BINARY_ARGS=3,
                   _PROTOCOL_BINARY_RESULT=4, _PROTOCOL_BINARY_ERROR=5,
                   _PROTOCOL_BINARY_SLOT_COUNT=6, _PROTOCOL_BINARY_BORROWED_COUNT=3,
                   stack_alloc=self.alloc, store_ptr=self.store, load_ptr=self.load,
                   store_i64=self.store, load_i64=lambda b, o: self.load(b, o) or 0,
                   memset=memset, null=lambda: None, cstr=lambda value: value,
                   ptr_add=lambda base, offset: base + offset,
                   ptr_is_null=lambda value: int(value is None), is_tagged_int=lambda value: 0,
                   load_i32=lambda value, offset: self.objects[value]['pin'],
                   global_addr=lambda name: -3 if 'borrowed' in name else 6,
                   pcc_gc_frame_enter=enter, pcc_gc_frame_leave=leave,
                   pcc_gc_foreign_lease_acquire=self.acquire, pcc_gc_foreign_lease_release=self.release,
                   pcc_gc_root_copy_borrowed_lease=self.copy, pcc_gc_store_root=self.drop,
                   pcc_py_gc_minor_graph_lock=lambda: self.park('graph-lock'),
                   pcc_py_gc_minor_graph_unlock=lambda: None,
                   pcc_gc_note_slot_write_barrier=lambda owner, slot, value: None,
                   pcc_gc_load_ptr=load_gc, pcc_gc_pin=pin,
                   pcc_gc_take_pinned_slot=take, py_tls_exc_swap_slot=swap,
                   py_clear_exception=lambda: setattr(self, 'pending', None),
                   py_runtime_error_if_unset=lambda helper, message: require(None, helper, message),
                   py_err_occurred=lambda: int(self.pending is not None),
                   py_tuple_new=lambda count: self.new('args'), py_tuple_set_item=self.set_item,
                   py_func_call=self.call, _protocol_require_result=require,
                   pcc_platform_abort=lambda: pytest.fail('binary owner protocol abort'))
        return env


def _runtime_helpers():
    path = Path(__file__).parents[2] / 'pcc/runtime/py/py_protocol_runtime.py'
    tree = ast.parse(path.read_text())
    counts = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign) and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in (
            '_PROTOCOL_BINARY_SLOT_COUNT', '_PROTOCOL_BINARY_BORROWED_COUNT',
        )
    }
    maps = {
        node.value.args[0].value: ast.literal_eval(node.value.args[1])
        for node in tree.body
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == 'define_global_i32'
        and node.value.args[0].value.startswith('pcc_protocol_binary_')
    }
    assert maps == {
        'pcc_protocol_binary_borrowed_map': -counts['_PROTOCOL_BINARY_BORROWED_COUNT'],
        'pcc_protocol_binary_owned_map': counts['_PROTOCOL_BINARY_SLOT_COUNT'],
    }
    names = {'_protocol_binary_adopt', '_protocol_binary_drop', '_protocol_binary_body',
             '_protocol_binary_pin_result', '_call_binary_function', '_call_binary'}
    selected = ast.Module(body=[node for node in tree.body
                               if isinstance(node, ast.FunctionDef) and node.name in names],
                          type_ignores=[])
    assert len(selected.body) == len(names)
    helpers = {node.name: node for node in selected.body}
    managed = helpers['_call_binary_function']
    # The selected method and Python operands use the surrounding runtime's
    # object-pointer ABI. c_ptr would assert raw-address semantics here.
    assert managed.returns is None
    assert all(argument.annotation is None for argument in managed.args.args)
    for name, pointer_count in (
        ('_protocol_binary_adopt', 2), ('_protocol_binary_drop', 2),
        ('_protocol_binary_body', 3), ('_protocol_binary_pin_result', 1),
    ):
        assert all(isinstance(argument.annotation, ast.Name)
                   and argument.annotation.id == 'c_ptr'
                   for argument in helpers[name].args.args[:pointer_count])
    return compile(selected, str(path), 'exec')


@pytest.mark.parametrize('prior_pin', (0, 64))
@pytest.mark.parametrize('relocate', (False, True))
@pytest.mark.parametrize('result_kind', ('new', 'args', 'method', 'self', 'arg'))
def test_binary_callback_registered_owner_aliases(result_kind, relocate, prior_pin):
    model = BinaryOwnerModel(result_kind, None, relocate, prior_pin)
    env = model.environment()
    exec(_runtime_helpers(), env)
    result = env['_call_binary_function']('method', 'self', 'arg')
    expected = 'result' if result_kind == 'new' else result_kind
    assert result == model.current[expected]
    assert model.frames == {} and model.tokens == {} and model.pending is None
    assert model.objects[result]['refs'] > 0
    assert model.objects[model.current['method']]['pin'] == prior_pin
    assert model.observed_result_during_disposal
    if relocate:
        assert any(reason == 'owning-frame-registration' for reason, _, _ in model.moves)
        if result_kind == 'new':
            assert any(reason == 'lease-acquire' and old == 'result' for reason, old, _ in model.moves)


@pytest.mark.parametrize('failure', ('copy', 'args', 'set', 'result', 'missing-error', 'lease'))
@pytest.mark.parametrize('relocate', (False, True))
def test_binary_callback_cleanup_preserves_original_error(failure, relocate):
    model = BinaryOwnerModel('new', failure, relocate, 0)
    env = model.environment()
    exec(_runtime_helpers(), env)
    assert env['_call_binary_function']('method', 'self', 'arg') is None
    assert model.frames == {} and model.tokens == {}
    expected = {'copy': 'binary input owner copy failed',
                'missing-error': 'user protocol callback returned NULL without an exception',
                'lease': 'binary result owner lease failed'}.get(failure, failure + '-error')
    assert model.pending == expected
    for name in ('self', 'arg'):
        assert model.objects[model.current[name]]['refs'] == 1
    assert model.objects[model.current['method']]['refs'] == (0 if model.method_owner_removed else 1)
    assert model.objects[model.current['method']]['pin'] == 0
    if 'result' in model.current:
        assert model.objects[model.current['result']]['refs'] == 0


def test_binary_raw_method_remains_outside_managed_frames():
    seen = []
    env = dict(null=lambda: None, ptr_is_null=lambda value: int(value is None),
               _ptr_can_have_header=lambda value: 0,
               call_ptr2=lambda method, left, right: seen.append((method, left, right)) or 'raw-result',
               _protocol_require_result=lambda value, helper, message: value, cstr=lambda value: value)
    exec(_runtime_helpers(), dict(c_ptr=object, **env), env)
    assert env['_call_binary']('raw-code-address', 'left', 'right') == 'raw-result'
    assert seen == [('raw-code-address', 'left', 'right')]
    assert env['_call_binary'](None, 'left', 'right') is None


PROGRAM = textwrap.dedent('''\
    import gc
    events = []
    class Item:
        def __init__(self, value):
            self.value = value
        def __del__(self):
            events.append(self.value)
            gc.collect()
    class Left:
        def __add__(self, other):
            gc.collect()
            return other
    class Self:
        def __add__(self, other):
            gc.collect()
            return self
    class Decline:
        def __add__(self, other):
            gc.collect()
            return NotImplemented
    class Reflected:
        def __radd__(self, other):
            gc.collect()
            return other
    class Failure:
        def __add__(self, other):
            gc.collect()
            raise ValueError('binary-callback')
    def take(*, value, later=None, **extras):
        gc.collect()
        return value
    def later():
        gc.collect()
        raise ValueError('later')
    def main():
        item = Item('alias')
        assert take(value=Left() + item) is item
        own = Self()
        assert take(value=own + item) is own
        declined = Decline()
        assert take(value=declined + Reflected()) is declined
        try:
            take(value=Left() + Item('temporary'), later=later())
        except ValueError as error:
            assert str(error) == 'later'
        else:
            raise AssertionError('missing later error')
        gc.collect()
        assert events.count('temporary') == 1
        try:
            take(value=Failure() + Item('failed'))
        except ValueError as error:
            assert str(error) == 'binary-callback'
        else:
            raise AssertionError('missing callback error')
        gc.collect()
        assert events.count('failed') == 1
        print('BINARY_PROTOCOL_OWNERS_OK')
    main()
''')


@pytest.mark.integration
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_binary_protocol_owner_native_five_gc(
    python_program_compiler, request, explicit_owned_runtime, tmp_path, capfd,
):
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(PROGRAM, 'BINARY_PROTOCOL_OWNERS_OK\n', tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime, capfd,
                         provenance_probe='2')
    assert (tmp_path / 'compiler-wrapper.stderr').read_text() == ''
