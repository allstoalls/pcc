"""Format and str outputs stay owned through callbacks and operand cleanup."""
from __future__ import annotations

import ast
from pathlib import Path
import re
import textwrap

import pytest

from tests.python.test_shared_call_binding import _emit
from tests.python.test_slot_call_subscript_producers import _assert_immediate_publication
from tests.python.owned_regression_support import assert_owned_program, explicit_owned_runtime


@pytest.mark.parametrize('expression,runtime', (
    ('str(item)', 'py_obj_str'),
    ('format(item)', 'py_obj_format'),
    ('format(item, spec)', 'py_obj_format'),
    ('f"{item}"', 'py_obj_format'),
))
@pytest.mark.parametrize('site', ('argument', 'default', 'return', 'later-error'))
def test_format_producer_contexts(expression, runtime, site):
    prefix = ('from pcc.unsafe import null\n'
              'def take(*, value, later=None, **extras):\n    return value\n'
              'def fail():\n    raise ValueError("later")\n')
    if site == 'default':
        body = '    def target(value=' + expression + '):\n        return value\n    return target()\n'
    elif site == 'return':
        body = '    return ' + expression + '\n'
    elif site == 'later-error':
        body = '    return take(value=' + expression + ', later=fail())\n'
    else:
        body = '    return take(value=' + expression + ')\n'
    text = _emit(prefix + 'def probe(item, spec):\n' + body)
    _assert_immediate_publication(text, runtime)
    if site == 'default':
        publication = re.search(r'(%[^ ]+) = call [^\n]*@' + runtime +
                                r'\([^\n]*\)\n  store ptr \1, ptr (%[^ ,\n]+)', text)
        assert publication is not None
        root = publication.group(2)
        later = text[publication.end():]
        kind = later.index('func.sig.kind')
        uses = list(re.finditer(r'(%[^ ]+) = load ptr, ptr ' + re.escape(root) + r'(?:,|\n)', later))
        insertions = [m for m in uses if re.search(r'@py_tuple_set_item\([^\n]*' + re.escape(m.group(1)) + r'\)', later[m.end():])]
        assert insertions and kind < insertions[0].start()
        assert '@pcc_gc_take_pinned_slot(' not in later[:insertions[0].end()]


class _CallbackModel:
    """Execute actual runtime helper bodies with observable owner disposal."""
    def __init__(self, failure, relocate=False):
        self.failure = failure
        self.memory = {}
        self.frames = set()
        self.next_address = 100
        self.refs = {'value': 1, 'spec': 1}
        self.relocate = relocate
        self.lookup_after_registration = False
        self.lease = {}
        self.pending = None
        self.callback_count = 0
        self.disposed = []
        self.result_seen_during_disposal = False

    def alloc(self, size):
        self.next_address += 100
        return self.next_address

    def store(self, base, offset, value):
        self.memory[base + offset] = value

    def load(self, base, offset):
        return self.memory.get(base + offset)

    def new(self, name):
        if self.failure == name:
            self.pending = name + '-error'
            return None
        self.refs[name] = 1
        return name

    def acquire(self, slot):
        assert any(base <= slot < base + count * 8 for base, count in self.frames if count > 0)
        value = self.load(slot, 0)
        if value is not None:
            assert self.refs[value] > 0
            self.lease[slot] = self.lease.get(slot, 0) + 1
            return 1
        return 0

    def release(self, slot, token):
        if token:
            assert self.lease[slot] == 1
            del self.lease[slot]
        return 0

    def drop(self, slot, value):
        assert value is None
        old = self.load(slot, 0)
        self.store(slot, 0, None)
        if old is not None:
            self.refs[old] -= 1
            if self.refs[old] == 0:
                self.disposed.append(old)
                if old == 'args':
                    self.refs[self.argument] -= 1
                if old == 'method':
                    # Disposal may reenter and replace TLS. The original error
                    # must already have an owner and the result must stay live.
                    if self.failure is None:
                        assert any(self.memory.get(base + 3 * 8) == 'result'
                                   for base, count in self.frames if count == 6)
                        assert self.refs['result'] == 1
                        self.result_seen_during_disposal = True
                    self.pending = 'disposal-error'

    def copy(self, destination, source):
        value = self.load(source, 0)
        self.refs[value] += 1
        self.store(destination, 0, value)
        return self.acquire(destination)

    def swap(self, slot):
        self.pending, self.memory[slot] = self.load(slot, 0), self.pending

    def call(self, method, args, kwargs):
        assert method == 'method' and args == 'args'
        self.callback_count += 1
        return self.new('result')

    def environment(self):
        def memset(base, value, size):
            for offset in range(0, size, 8):
                self.store(base, offset, None)
        def enter(count, base):
            if count > 0:
                # Registration may relocate borrowed inputs. The method must
                # not have been allocated as a borrowed pseudo-owner yet.
                assert 'method' not in self.refs
                if self.relocate:
                    old = 'value'
                    self.refs['moved-value'] = self.refs.pop(old)
                    for address, value in list(self.memory.items()):
                        if value == old:
                            self.memory[address] = 'moved-value'
            self.frames.add((base, count))
        def leave(base):
            self.frames = {(b, c) for b, c in self.frames if b != base}
        def getattr_(value, attribute):
            assert value == ('moved-value' if self.relocate else 'value')
            assert any(count == 6 for _, count in self.frames)
            self.lookup_after_registration = True
            return self.new('method')
        def set_item(args, index, spec):
            self.argument = spec
            self.refs[spec] += 1
        def require(result, helper, message):
            if result is None and self.pending is None:
                self.pending = message
            return result
        def take(slot, prior):
            value = self.load(slot, 0)
            self.store(slot, 0, None)
            return value
        def check_string(value):
            # The callback model represents its owned string by this token.
            # Validation must observe the live rooted result before disposal.
            assert value == 'result' and self.refs[value] == 1
            assert any(self.memory.get(base + 3 * 8) == value
                       for base, count in self.frames if count == 6)
            return 1
        return dict(c_ptr=object, C_POINTER_SIZE=8,
                    _FORMAT_METHOD=0, _FORMAT_ARGS=1, _FORMAT_SPEC=2,
                    _FORMAT_RESULT=3, _FORMAT_ERROR=4, _FORMAT_VALUE=5, _FORMAT_SLOT_COUNT=6,
                    stack_alloc=self.alloc, store_ptr=self.store, load_ptr=self.load,
                    store_i64=self.store, load_i64=lambda b, o: self.load(b, o) or 0,
                    memset=memset, null=lambda: None, cstr=lambda s: s,
                    ptr_add=lambda b, o: b + o, ptr_is_null=lambda x: int(x is None),
                    global_addr=lambda name: -2 if 'borrowed' in name else 6,
                    global_load_ptr=lambda name: None,
                    pcc_gc_frame_enter=enter, pcc_gc_frame_leave=leave,
                    pcc_gc_foreign_lease_acquire=self.acquire,
                    pcc_gc_foreign_lease_release=self.release,
                    pcc_gc_root_copy_borrowed_lease=self.copy,
                    pcc_gc_store_root=self.drop,
                    pcc_py_gc_minor_graph_lock=lambda: None,
                    pcc_py_gc_minor_graph_unlock=lambda: None,
                    pcc_gc_note_slot_write_barrier=lambda *args: None,
                    py_runtime_error_if_unset=lambda helper, message: require(None, helper, message),
                    py_tls_exc_swap_slot=self.swap,
                    py_clear_exception=lambda: setattr(self, 'pending', None),
                    py_err_occurred=lambda: int(self.pending is not None),
                    py_tuple_new=lambda count: self.new('args'),
                    py_tuple_set_item=set_item, py_str_new=lambda text, size: self.new('empty'),
                    py_str_check=check_string,
                    py_obj_call=self.call, py_obj_getattr=getattr_, _format_require_result=require,
                    _unicode_format_pin=lambda slot: 0,
                    pcc_gc_take_pinned_slot=take,
                    pcc_platform_abort=lambda: pytest.fail('owner protocol abort'))


@pytest.mark.parametrize('relocate', (False, True))
@pytest.mark.parametrize('failure', (None, 'args', 'result', 'empty'))
def test_actual_format_callback_owner_disposal_and_error(failure, relocate):
    path = Path(__file__).parents[2] / 'pcc/runtime/py/py_format_runtime.py'
    tree = ast.parse(path.read_text())
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    for name in ('_call_object_format', '_format_value'):
        # Managed PyObject values use the runtime's object-pointer ABI;
        # explicit c_ptr is reserved for the slot-address helper parameters.
        assert all(arg.annotation is None for arg in functions[name].args.args)
        assert functions[name].returns is None
    names = {'_format_callback_adopt', '_format_callback_drop',
             '_format_callback_body', '_call_object_format'}
    selected = ast.Module(body=[node for node in tree.body
                               if isinstance(node, ast.FunctionDef) and node.name in names],
                          type_ignores=[])
    model = _CallbackModel(failure, relocate)
    env = model.environment()
    exec(compile(selected, str(path), 'exec'), env)
    result = env['_call_object_format']('value', None if failure == 'empty' else 'spec')
    assert model.frames == set() and model.lease == {}
    assert model.refs['method'] == 0 and model.refs['spec'] == 1
    assert model.refs['moved-value' if relocate else 'value'] == 1
    assert model.lookup_after_registration
    if failure is None:
        assert result == 'result' and model.refs['result'] == 1
        assert model.result_seen_during_disposal
        assert model.pending is None
    else:
        assert result is None and model.pending == failure + '-error'
        assert model.callback_count == (1 if failure == 'result' else 0)


PROGRAM = textwrap.dedent('''\
    import gc
    events = []
    class Token:
        def __format__(self, spec):
            gc.collect()
            events.append('format')
            return 'TOKEN'
        def __str__(self):
            gc.collect()
            events.append('str')
            return 'TOKEN'
        def __del__(self):
            events.append('disposed')
            assert format(17) == '17'
            gc.collect()
    class Bad:
        def __format__(self, spec):
            gc.collect()
            raise ValueError('format-error')
        def __str__(self):
            gc.collect()
            raise ValueError('str-error')
    def take(*, value, later=None, **extras):
        gc.collect()
        return value
    def returned(value):
        return format(value)
    def spec():
        gc.collect()
        events.append('spec')
        return ''
    def later():
        events.append('later')
        gc.collect()
        raise ValueError('later-error')
    def main():
        assert take(value=format(Token(), spec())) == 'TOKEN'
        assert events[0] == 'spec' and events[1] == 'format'
        gc.collect()
        assert events.count('disposed') == 1
        assert take(value=str(Token())) == 'TOKEN'
        gc.collect()
        assert events.count('disposed') == 2
        item = Token()
        assert returned(item) == 'TOKEN'
        def target(value=format(item)):
            gc.collect()
            return value
        assert target() == 'TOKEN'
        assert f'{item}' == 'TOKEN'
        try:
            take(value=format(Token()), later=later())
        except ValueError as error:
            assert str(error) == 'later-error'
        else:
            raise AssertionError('missing later error')
        gc.collect()
        assert events.count('disposed') == 3
        before = events.count('format')
        try:
            format(item, later())
        except ValueError:
            pass
        else:
            raise AssertionError('missing spec error')
        assert events.count('format') == before
        before = events.count('later')
        try:
            take(value=format(Bad()), later=later())
        except ValueError as error:
            assert str(error) == 'format-error'
        else:
            raise AssertionError('missing format error')
        try:
            take(value=str(Bad()), later=later())
        except ValueError as error:
            assert str(error) == 'str-error'
        else:
            raise AssertionError('missing str error')
        assert events.count('later') == before
        print('FORMAT_PRODUCERS_OK')
    main()
''')


@pytest.mark.integration
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_format_producers_native_five_gc(python_program_compiler, request,
                                        explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(PROGRAM, 'FORMAT_PRODUCERS_OK\n', tmp_path, python_program_compiler,
                         mode, explicit_owned_runtime, capfd, provenance_probe='2')
    assert (tmp_path / 'compiler-wrapper.stderr').read_text() == ''
