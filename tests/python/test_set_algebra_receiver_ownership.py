"""Shared update dispatch and direct set-algebra result publication.

Host lowering proves owner/dispatch structure. Native execution is a separate
integration gate; the reference program is not native-GC qualification.
"""
import re

import pytest

from pcc.frontends.python.codegen.call_object_lowering import CallObjectLoweringMixin
from pcc.frontends.python.codegen.set_lowering import SetLoweringMixin
from tests.python.test_shared_call_binding import _emit, _function
from tests.python.owned_regression_support import assert_owned_program, explicit_owned_runtime


def test_shared_update_static_host_signatures_include_slot_owners():
    from pcc.frontends.python.codegen._l1_codegen_static_methods import L1_CODEGEN_STATIC_METHODS
    from pcc.frontends.python.codegen.layer1_support import _default_native_module_exports
    static = {entry['name']: entry for entry in L1_CODEGEN_STATIC_METHODS}
    exports = _default_native_module_exports('pcc.frontends.python.codegen.layer1')
    native = {entry['name']: entry for entry in
              exports['pcc.frontends.python.codegen.layer1']['L1CodeGen']['methods']}
    for name, parameters in (
        ('_emit_shared_container_update', ('self', 'expr')),
        ('_set_call_operands', ('self', 'expr')),
        ('_emit_set_algebra_call', ('self', 'expr', 'dynamic', '', 'receiver_slot', 'output_slot',
                                    'algebra_method')),
    ):
        assert tuple(entry['name'] for entry in static[name]['call_sig']) == parameters
        assert static[name]['call_sig'] == native[name]['call_sig']
    signature = static['_emit_set_algebra_call']['call_sig']
    assert signature[3]['kind'] == 'kw_only'
    assert all(signature[index]['has_default'] for index in (2, 4, 5, 6))


@pytest.mark.parametrize('method', (
    'union', 'intersection', 'difference', 'update', 'intersection_update',
    'difference_update', 'symmetric_difference', 'symmetric_difference_update',
))
@pytest.mark.parametrize('annotation', ('', ': set'))
@pytest.mark.parametrize('site', ('argument', 'default', 'return'))
def test_algebra_publishes_into_caller_sink(method, annotation, site):
    expression = 'receiver.' + method + '(source)'
    prefix = 'def take(*, value, **extras):\n    return value\n'
    if site == 'default':
        body = '    def saved(value=' + expression + '):\n        return value\n    return saved()\n'
    elif site == 'return':
        body = '    return ' + expression + '\n'
    else:
        body = '    return take(value=' + expression + ')\n'
    text = _emit(prefix + 'def probe(receiver' + annotation + ', source):\n' + body)
    function = _function(text)
    assert re.search(r'call i64[^\n]*@py_set_call_method_slots\(', function)
    # Operand/default contexts supply a sink. A plain return currently uses
    # the producer's own result root and takes it after operand destruction.
    if site != 'return':
        assert 'set.call.result.operand' not in function
        assert 'set.call.current' in function
    else:
        assert '@pcc_gc_take_pinned_slot(' in function
    if not annotation:
        assert 'set.call.generic.status' in function
    if method == 'update' and not annotation:
        assert re.search(r'call i64[^\n]*@py_dict_update_slots\(', function)
        assert not re.search(r'call void[^\n]*@py_dict_update\(', function)


def test_dynamic_update_evaluates_receiver_once_and_passes_registered_owners(monkeypatch):
    evaluations = []
    transfers = []
    emit_operand = CallObjectLoweringMixin._emit_slot_call_operand
    emit_algebra = SetLoweringMixin._emit_set_algebra_call

    def observed_operand(self, expr, label):
        if label == 'update.receiver':
            evaluations.append(expr)
        if evaluations and expr is evaluations[0]:
            assert label == 'update.receiver', 'receiver was evaluated again'
        return emit_operand(self, expr, label)

    def observed_algebra(self, expr, dynamic=False, *, receiver_slot=None, output_slot=None):
        if receiver_slot is not None:
            assert dynamic and output_slot is not None
            assert receiver_slot is not output_slot
            self._slot_call_root_record(receiver_slot)
            self._slot_call_root_record(output_slot)
            transfers.append((receiver_slot, output_slot))
        return emit_algebra(self, expr, dynamic, receiver_slot=receiver_slot, output_slot=output_slot)

    monkeypatch.setattr(CallObjectLoweringMixin, '_emit_slot_call_operand', observed_operand)
    monkeypatch.setattr(SetLoweringMixin, '_emit_set_algebra_call', observed_algebra)
    text = _emit('def probe(receiver, source):\n    return receiver.update(source)\n')
    assert len(evaluations) == len(transfers) == 1
    function = _function(text)
    receiver, output = map(str, transfers[0])
    assert function.count(' = alloca ptr') > 0
    assert 'load ptr, ptr ' + output in function
    # The receiver slot itself reaches the dynamic method call, directly or
    # through an identity bitcast alias.
    aliases = {receiver} | set(re.findall(
        r'(%[-\w.]+) = bitcast ptr ' + re.escape(receiver) + r' to ptr', function,
    ))
    passed = re.findall(r'@py_set_call_method_slots\(ptr (%[-\w.]+),', function)
    assert passed and set(passed) <= aliases


PROGRAM = '''\
import gc

events = []
class Other:
    def update(self, source):
        events.append('old')
        gc.collect()
        return source

def replacement(source):
    events.append('new')
    return source

def mutate_method(receiver, source):
    receiver.update = replacement
    events.append('argument')
    gc.collect()
    return source

def call_update(receiver, source):
    return receiver.update(source)

def ordered_update(receiver, source):
    return receiver.update(mutate_method(receiver, source))

def fail():
    events.append('fail')
    gc.collect()
    raise ValueError('argument failed')

def failed_update(receiver):
    return receiver.update(fail())

def broken_values():
    yield 3
    gc.collect()
    raise ValueError('iteration failed')

class Token:
    def __del__(self):
        events.append('disposed')

def main():
    target = {1, 2}
    alias = target
    assert call_update(target, [2, 3]) is None
    assert alias is target and alias == {1, 2, 3}
    mapping = {'before': 1}
    assert call_update(mapping, [('after', 2)]) is None
    assert mapping == {'before': 1, 'after': 2}
    other = Other()
    payload = [Token()]
    result = ordered_update(other, payload)
    gc.collect()
    assert result is payload and events == ['argument', 'old']
    assert call_update(other, payload) is payload
    assert events[-1] == 'new'
    try:
        failed_update(target)
    except ValueError as error:
        assert str(error) == 'argument failed'
    else:
        raise AssertionError('lost argument error')
    assert alias == {1, 2, 3}
    before = len(events)
    try:
        failed_update(7)
    except AttributeError:
        pass
    else:
        raise AssertionError('missing method accepted')
    assert len(events) == before
    target = {1, 2}
    try:
        call_update(target, broken_values())
    except ValueError as error:
        assert str(error) == 'iteration failed'
    else:
        raise AssertionError('lost iteration error')
    assert target == {1, 2, 3}
    del result
    del payload
    gc.collect()
    assert events.count('disposed') == 1
    print('SET_RECEIVER_OWNERS_OK')

main()
'''


def test_shared_update_reference_semantics(capsys):
    exec(compile(PROGRAM, '<shared-update-owners>', 'exec'), {})
    assert capsys.readouterr().out == 'SET_RECEIVER_OWNERS_OK\n'


def test_shared_update_semantics_owned_ir():
    text = _emit(PROGRAM)
    function = _function(text, 'user_binding_call_update')
    assert '@py_dict_update_slots(' in function
    assert '@py_set_call_method_slots(' in function
    assert '@py_obj_call_slots(' in function
    assert 'set.call.generic.status' in function
    assert 'set.call.current' in function


@pytest.mark.integration
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_shared_update_native_five_gc(python_program_compiler, request,
                                     explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(PROGRAM, 'SET_RECEIVER_OWNERS_OK\n', tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime,
                         capfd, provenance_probe='2')
