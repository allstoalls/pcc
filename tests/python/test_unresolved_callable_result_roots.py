"""Live namespace callable factories use the shared owning call transaction."""
import re
import textwrap

import pytest

from tests.python.test_shared_call_binding import _emit, _function
from tests.python.owned_regression_support import (
    assert_owned_program, assert_reference_program, explicit_owned_runtime,
)


@pytest.mark.parametrize('call', (
    'dataclass(frozen=True)', 'factory(second=right(), first=left())',
    'factory(**options)', 'factory(value, frozen=True)',
))
@pytest.mark.parametrize('site', ('return', 'callee', 'argument', 'default', 'later-error'))
def test_namespace_factory_keeps_callable_options_and_output_owned(call, site):
    prefix = ('def take(*, value, later=None, **extras):\n    return value\n'
              'def fail():\n    raise ValueError("later")\n')
    if site == 'return':
        statement = '    return ' + call + '\n'
    elif site == 'callee':
        statement = '    return (' + call + ')(value)\n'
    elif site == 'default':
        statement = '    def target(value=' + call + '):\n        return value\n    return target()\n'
    else:
        statement = '    return take(value=' + call + (', later=fail()' if site == 'later-error' else '') + ')\n'
    local_import = '    from dataclasses import dataclass\n' if call.startswith('dataclass') else ''
    text = _function(_emit(prefix + 'def probe(value, options, left, right):\n' + local_import + statement))
    name = 'dataclass' if call.startswith('dataclass') else 'factory'
    assert name + '.dyn.call.callable' in text
    assert name + '.dyn.call.invoke' in text
    assert re.search(r'\bcall [^\n]*@py_module_attr_get\(', text)
    assert re.search(r'\bcall [^\n]*@py_obj_call_slots\(', text)
    assert not re.search(r'\bcall [^\n]*@py_obj_call\(', text)


def test_original_valueclass_decorator_factory_shape_reaches_owned_ir():
    text = _emit('''def valueclass(cls):
    from dataclasses import dataclass
    result = dataclass(frozen=True)(cls)
    result.__pcc_valueclass__ = True
    return result
''')
    body = _function(text, 'user_binding_valueclass')
    assert 'dataclass.dyn.call.invoke' in body
    assert 'expr.obj.call.invoke' in body
    assert not re.search(r'\bcall [^\n]*@py_obj_call\(', body)


PROGRAM = textwrap.dedent('''\
    import gc
    events = []
    shared = []
    class Option:
        def __init__(self, name):
            self.name = name
        def __del__(self):
            events.append('drop:' + self.name)
            gc.collect()
    class Decorator:
        def __init__(self, first, second, default):
            self.first = first
            self.second = second
            self.default = default
        def __call__(self, value):
            gc.collect()
            assert self.default is shared
            return value
    def option(name):
        events.append(name)
        gc.collect()
        return Option(name)
    def construct(first=None, second=None, default=shared):
        gc.collect()
        return Decorator(first, second, default)
    def take(*, value, later=None, **extras):
        return value
    def fail():
        raise ValueError('later')
    globals()['factory'] = construct
    sentinel = []
    assert factory(second=option('second'), first=option('first'))(sentinel) is sentinel
    assert events[:2] == ['second', 'first']
    assert sorted(events[2:]) == ['drop:first', 'drop:second']
    events.clear()
    try:
        take(value=factory(first=option('cleanup')), later=fail())
    except ValueError as error:
        assert str(error) == 'later'
    else:
        raise AssertionError('missing error')
    assert events == ['cleanup', 'drop:cleanup']
    def default_target(value=factory(first=option('default'))):
        return value
    assert default_target() is default_target()
    del default_target
    gc.collect()
    assert events[-1] == 'drop:default'
    events.clear()
    try:
        factory(first=option('duplicate'), **{'first': option('mapping')})
    except TypeError:
        pass
    else:
        raise AssertionError('duplicate keyword accepted')
    assert events[:2] == ['duplicate', 'mapping']
    assert sorted(events[2:]) == ['drop:duplicate', 'drop:mapping']
    events.clear()
    try:
        absent_factory(first=option('must-not-run'))
    except NameError:
        pass
    else:
        raise AssertionError('missing factory accepted')
    assert events == []
    print('NAMESPACE_FACTORY_OWNERS_OK')
''')


def test_namespace_factory_reference_program(tmp_path):
    assert_reference_program(PROGRAM, 'NAMESPACE_FACTORY_OWNERS_OK\n', tmp_path)


@pytest.mark.integration
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_namespace_factory_native_five_gc(python_program_compiler, request,
                                         explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(PROGRAM, 'NAMESPACE_FACTORY_OWNERS_OK\n', tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime,
                         capfd, provenance_probe='2')
