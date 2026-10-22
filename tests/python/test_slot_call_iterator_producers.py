"""Iterator calls preserve eager arguments, owned results and exact exceptions."""
from __future__ import annotations

import re
import textwrap

import pytest

from tests.python.owned_regression_support import (
    assert_owned_program,
    assert_reference_program,
    explicit_owned_runtime,
)
from tests.python.test_shared_call_binding import _emit, _function


@pytest.mark.parametrize('expression', (
    'iter(item)', 'iter(producer, sentinel)', 'next(item)', 'next(item, sentinel)',
    'next(iter(item))', 'next(iter(item), sentinel)', 'f"value-{next(item)}"',
    'next(filter(None, item), sentinel)', 'next(filter(producer, item), sentinel)',
))
@pytest.mark.parametrize('site', ('return', 'nested'))
def test_iterator_results_publish_before_cleanup(expression, site):
    result = expression if site == 'return' else 'take(value=' + expression + ')'
    text = _emit(
        'def take(*, value, **extras):\n    return value\n'
        'def probe(item, producer, sentinel):\n    return ' + result + '\n'
    )
    body = _function(text)
    assert 'strict.nolib.stub' not in text
    calls = list(re.finditer(
        r'(%[^ ]+) = call ptr [^\n]*@py_(?:obj_iter|obj_next|iter_callable_new)\([^\n]*\)\n', body,
    ))
    assert calls, body
    for call in calls:
        assert body[call.end():].lstrip().startswith('store ptr ' + call.group(1) + ',')
    if 'next(' in expression:
        assert '@py_tls_exc_swap_slot(' in body
        assert 'next.cur_exc' not in body


def test_next_evaluates_default_before_advancing():
    body = _function(_emit(
        'def source():\n    return iter([1])\n'
        'def default():\n    return 2\n'
        'def probe():\n    return next(source(), default())\n'
    ))
    assert body.index('@user_binding_source(') < body.index('@user_binding_default(')
    assert body.index('@user_binding_default(') < body.index('@py_obj_next(')


def test_filter_evaluates_predicate_then_iterable_then_default():
    body = _function(_emit(
        'def predicate(item):\n    return item\n'
        'def factory():\n    return predicate\n'
        'def source():\n    return [1]\n'
        'def default():\n    return 2\n'
        'def probe():\n    return next(filter(factory(), source()), default())\n'
    ))
    positions = [body.index(symbol) for symbol in (
        '@user_binding_factory(', '@user_binding_source(', '@py_obj_iter(',
        '@user_binding_default(', '@py_obj_next(', '@py_obj_call(',
    )]
    assert positions == sorted(positions)


PROGRAMS = {
    'argument_order': '''
        import gc
        events = []
        class Cursor:
            def __iter__(self):
                events.append('iter')
                return self
            def __next__(self):
                events.append('next')
                gc.collect()
                return 17
        def source():
            events.append('source')
            return Cursor()
        def default():
            events.append('default')
            gc.collect()
            return 29
        def failing_default():
            events.append('default-error')
            gc.collect()
            raise ValueError('default failed')
        def main():
            assert next(source(), default()) == 17
            assert events == ['source', 'default', 'next']
            events.clear()
            try:
                next(source(), failing_default())
            except ValueError as error:
                assert str(error) == 'default failed'
            else:
                raise AssertionError('default exception was lost')
            assert events == ['source', 'default-error']
            iterator = iter([1, 2])
            try:
                next(iterator, failing_default())
            except ValueError:
                pass
            assert next(iterator) == 1
            print('ITERATOR_ARGUMENT_ORDER_OK')
        main()
    ''',
    'exceptions': '''
        import gc
        events = []
        selected = ValueError('advance failed')
        class Cursor:
            def __init__(self, exhausted):
                self.exhausted = exhausted
            def __next__(self):
                gc.collect()
                if self.exhausted:
                    raise StopIteration('finished')
                raise ValueError('advance failed')
            def __del__(self):
                events.append('disposed')
                gc.collect()
        class Raising:
            def __next__(self):
                raise selected
        def fallback():
            events.append('default')
            return 31
        def main():
            assert next(Cursor(True), fallback()) == 31
            assert events == ['default', 'disposed']
            try:
                next(Cursor(False), fallback())
            except ValueError as error:
                assert str(error) == 'advance failed'
            else:
                raise AssertionError('non-exhaustion exception was swallowed')
            assert events == ['default', 'disposed', 'default', 'disposed']
            try:
                next(Cursor(True))
            except StopIteration as error:
                assert str(error) == 'finished'
            else:
                raise AssertionError('exhaustion was swallowed')
            assert next(iter([]), None) is None
            try:
                next(Raising(), None)
            except ValueError as error:
                assert error is selected
            else:
                raise AssertionError('selected exception was lost')
            print('ITERATOR_EXCEPTIONS_OK')
        main()
    ''',
    'ownership': '''
        import gc
        events = []
        class Item:
            def __init__(self, label):
                self.label = label
            def __del__(self):
                events.append(self.label)
                gc.collect()
        def direct():
            return next(iter([Item('live')]), Item('unused'))
        def take(*, value, later=None, **extras):
            gc.collect()
            return value
        def later():
            gc.collect()
            return 1
        def fail():
            raise ValueError('later')
        def main():
            result = take(value=direct(), later=later())
            gc.collect()
            assert result.label == 'live'
            assert events == ['unused']
            dropped = next(iter([]), Item('fallback'))
            gc.collect()
            assert dropped.label == 'fallback'
            assert events == ['unused']
            try:
                take(value=next(iter([Item('abandoned')])), later=fail())
            except ValueError:
                pass
            gc.collect()
            assert events == ['unused', 'abandoned']
            item = Item('alias')
            assert next(iter([item]), item) is item
            it = iter([item])
            assert iter(it) is it
            assert next(it) is item
            print('ITERATOR_OWNERSHIP_OK')
        main()
    ''',
    'callable_filter_generator': '''
        import gc
        events = []
        class Source:
            def __init__(self):
                self.value = 0
            def __call__(self):
                self.value += 1
                gc.collect()
                return self.value
        def callable_factory():
            events.append('callable')
            return Source()
        def sentinel_factory():
            events.append('sentinel')
            return 2
        def predicate(item):
            events.append('predicate')
            gc.collect()
            return item
        def predicate_factory():
            events.append('factory')
            return predicate
        def iterable():
            events.append('iterable')
            return [0, 4, 5]
        def default():
            events.append('default')
            return 9
        class TemporaryIterable:
            def __iter__(self):
                events.append('iter')
                return iter([3])
            def __del__(self):
                events.append('source-disposed')
                gc.collect()
        def failing_sentinel():
            raise ValueError('sentinel failed')
        def stopping_predicate(item):
            gc.collect()
            raise StopIteration('predicate stopped')
        class StoppingTruth:
            def __bool__(self):
                gc.collect()
                raise StopIteration('truth stopped')
        def bad_predicate(item):
            raise ValueError('predicate failed')
        def main():
            iterator = iter(callable_factory(), sentinel_factory())
            assert events == ['callable', 'sentinel']
            assert next(iterator) == 1
            assert next(iterator, 8) == 8
            assert next(iterator, 7) == 7
            try:
                iter(42, sentinel_factory())
            except TypeError:
                pass
            else:
                raise AssertionError('non-callable accepted by iter')
            assert events == ['callable', 'sentinel', 'sentinel']
            try:
                iter(42, failing_sentinel())
            except ValueError as error:
                assert str(error) == 'sentinel failed'
            else:
                raise AssertionError('callable validation preceded sentinel evaluation')
            events.clear()
            assert next(filter(predicate_factory(), iterable()), default()) == 4
            assert events == ['factory', 'iterable', 'default', 'predicate', 'predicate']
            events.clear()
            assert next(filter(None, TemporaryIterable()), default()) == 3
            assert events == ['iter', 'source-disposed', 'default']
            assert next(filter(None, [0, 0]), 19) == 19
            nothing = None
            assert next(filter(nothing, [0, 8]), 19) == 8
            assert next(filter(stopping_predicate, [1]), 43) == 43
            assert next(filter(None, [StoppingTruth()]), 47) == 47
            try:
                next(filter(stopping_predicate, [1]))
            except StopIteration as error:
                assert str(error) == 'predicate stopped'
            else:
                raise AssertionError('predicate exhaustion was lost')
            try:
                next(filter(bad_predicate, [1]), 0)
            except ValueError as error:
                assert str(error) == 'predicate failed'
            else:
                raise AssertionError('predicate exception was swallowed')
            assert next((x for x in [5, 6]), 0) == 5
            assert next((x for x in []), 9) == 9
            try:
                next((x for x in []))
            except StopIteration:
                pass
            else:
                raise AssertionError('empty generator expression did not exhaust')
            print('ITERATOR_CALLABLE_FILTER_GENERATOR_OK')
        main()
    ''',
}


@pytest.mark.parametrize('name', tuple(PROGRAMS))
def test_iterator_native_program_reference(name, tmp_path):
    program = textwrap.dedent(PROGRAMS[name]).lstrip()
    assert_reference_program(program, 'ITERATOR_' + name.upper() + '_OK\n', tmp_path)


@pytest.mark.integration
@pytest.mark.parametrize('name', tuple(PROGRAMS))
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_iterator_native_five_gc(name, python_program_compiler, request,
                                 explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params['python_program_compiler']
    program = textwrap.dedent(PROGRAMS[name]).lstrip()
    assert_owned_program(program, 'ITERATOR_' + name.upper() + '_OK\n', tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime, capfd)


@pytest.mark.parametrize('name', tuple(PROGRAMS))
def test_iterator_native_program_strict_codegen(name):
    text = _emit(textwrap.dedent(PROGRAMS[name]).lstrip())
    assert 'strict.nolib.stub' not in text


@pytest.mark.parametrize('name', ('iter', 'next'))
@pytest.mark.parametrize('binding', ('parameter', 'function'))
def test_shadowed_iterator_builtin_uses_ordinary_call(name, binding):
    prefix = 'def ' + name + '(item):\n    return item\n' if binding == 'function' else ''
    parameters = 'item' if binding == 'function' else name + ', item'
    body = _function(_emit(prefix + 'def probe(' + parameters + '):\n    return ' + name + '(item)\n'))
    assert '@py_obj_' + name + '(' not in body


@pytest.mark.parametrize('expression', (
    'iter(lambda: item, sentinel)',
    'next(filter(lambda value: value is item, source), sentinel)',
))
def test_iterator_callable_closures_keep_owned_captures(expression):
    text = _emit('def probe(item, source, sentinel):\n    return ' + expression + '\n')
    assert 'strict.nolib.stub' not in text
    assert '@py_iter_callable_new(' in text or '@py_obj_next(' in text


@pytest.mark.parametrize('source', (
    'item = iter([1])\nvalue = next(item, 0)\n',
    'def probe(item, default):\n    try:\n        return next(item, default)\n    except Exception:\n        return None\n',
    'def probe(item, predicate):\n    return next(filter(predicate, item), None)\n',
    'def probe(item):\n    try:\n        yield next(item, None)\n    except Exception:\n        yield None\n',
))
def test_iterator_roots_balance_real_stackmap_joins(source):
    from pcc.backend.self_backend_precise_stackmaps import build_stack_map_plans
    from pcc.backend.self_backend_prepare import prepare_module_for_target
    from pcc.backend.self_backend_x86_64_linux import _aggregate_returned_indirect
    text = 'target triple = "x86_64-unknown-linux-gnu"\n' + _emit(source)
    prepared = prepare_module_for_target(text, aggregate_returned_indirect=_aggregate_returned_indirect)
    plans = build_stack_map_plans(prepared.functions, prepared.globals_, target='x86_64-linux')
    assert len(plans) == len(prepared.functions)


@pytest.mark.parametrize('name', (
    '_iterator_builtin_is_shadowed', '_emit_iterator_callable_operand',
    '_iterator_install_cleanup', '_emit_next_owned_step',
    '_emit_next_pending_or_default', '_emit_next_filter_truth',
))
def test_iterator_helpers_match_static_host_signatures_and_call_arity(name):
    from inspect import signature
    from pcc.frontends.python.codegen._l1_codegen_static_methods import L1_CODEGEN_STATIC_METHODS
    from pcc.frontends.python.codegen.host_contract import L1_CODEGEN_HOST_METHODS
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.codegen.layer1_support import _default_native_module_exports
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.type_infer import infer_module

    static = {entry['name']: entry for entry in L1_CODEGEN_STATIC_METHODS}
    exports = _default_native_module_exports('pcc.frontends.python.codegen.layer1')
    methods = exports['pcc.frontends.python.codegen.layer1']['L1CodeGen']['methods']
    native = {entry['name']: entry for entry in methods}
    assert tuple(entry['name'] for entry in L1_CODEGEN_STATIC_METHODS) == L1_CODEGEN_HOST_METHODS
    assert len(static) == len(L1_CODEGEN_STATIC_METHODS)
    assert name in L1_CODEGEN_HOST_METHODS and static[name] == native[name]
    parameters = tuple(signature(getattr(L1CodeGen, name)).parameters)
    assert tuple(entry['name'] for entry in static[name]['call_sig']) == parameters
    assert not any(entry['has_default'] for entry in static[name]['call_sig'])
    arguments = ', '.join('argument' + str(index) for index in range(len(parameters) - 1))
    source = (
        'from pcc.frontends.python.codegen.layer1 import L1CodeGen\n'
        'def probe(codegen: L1CodeGen, ' + arguments + '):\n'
        '    return codegen.' + name + '(' + arguments + ')\n'
    )
    module = infer_module(parse_and_lift(source, 'binding.py', 'binding'), external_exports=exports)
    codegen = L1CodeGen(module, ir_scaffold_mode='on')
    codegen._strict_no_libpython = True
    codegen._native_module_exports = exports
    text = str(codegen.generate(module))
    symbol = 'user_pcc_frontends_python_codegen_layer1_L1CodeGen_' + name
    call = re.search(r'call [^\n]*@' + symbol + r'\(([^\n]*)\)', text)
    assert call and len(call[1].split(',')) == len(parameters)
    assert not re.search(r'\bcall [^\n]*@py_cpy_', text)
