"""Transitive captures cross lexical ancestors and retain private ABI provenance."""
from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
import textwrap

import pytest

from pcc.frontends.python.codegen.hoist_lowering import hoist_nested_funcdefs
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.py_ast import Call, DynType, Name
from pcc.frontends.python.py_lift import parse_and_lift
from pcc.frontends.python.type_infer import infer_module
from tests.python.owned_regression_support import assert_owned_program, explicit_owned_runtime


FORMS = {
    'plain': '{leaf}',
    'if': 'if True:\n{indented}',
    'else': 'if False:\n    pass\nelse:\n{indented}',
    'for': 'for item in (1,):\n{indented}',
    'for_else': 'for item in ():\n    pass\nelse:\n{indented}',
    'while': 'while True:\n{indented}\n    break',
    'while_else': 'while False:\n    pass\nelse:\n{indented}',
    'try': 'try:\n{indented}\nexcept ValueError:\n    pass',
    'except': 'try:\n    raise ValueError()\nexcept ValueError:\n{indented}',
    'try_else': 'try:\n    pass\nexcept ValueError:\n    pass\nelse:\n{indented}',
    'finally': 'try:\n    pass\nfinally:\n{indented}',
    'with': 'with Manager():\n{indented}',
    'nested_if': 'if True:\n    if True:\n{twice}',
    'nested_for': 'for item in (1,):\n    for other in (1,):\n{twice}',
    'if_for': 'if True:\n    for item in (1,):\n{twice}',
    'for_if': 'for item in (1,):\n    if True:\n{twice}',
    'try_if': 'try:\n    if True:\n{twice}\nexcept ValueError:\n    pass',
    'with_if': 'with Manager():\n    if True:\n{twice}',
}
MANAGER = '''class Manager:
    def __enter__(self):
        return self
    def __exit__(self, kind, value, traceback):
        return False
'''


def _form_source(form):
    leaf = 'def leaf():\n    return token'
    block = FORMS[form].format(leaf=leaf, indented=textwrap.indent(leaf, '    '),
                              twice=textwrap.indent(leaf, '        '))
    return (MANAGER + 'def outer(token):\n    def factory():\n'
            + textwrap.indent(block, '        ') + '\n        return leaf()\n    return factory()\n')


def _hoist(source):
    module = infer_module(parse_and_lift(source, 'transitive.py', 'transitive'))
    codegen = L1CodeGen(module, ir_scaffold_mode='on')
    codegen._strict_no_libpython = True
    hoist_nested_funcdefs(codegen)
    return codegen


def _walk(value):
    if isinstance(value, (tuple, list)):
        for item in value:
            yield from _walk(item)
    elif is_dataclass(value):
        yield value
        for field in fields(value):
            if field.name not in ('ty', 'span'):
                yield from _walk(getattr(value, field.name))


@pytest.mark.parametrize('form', FORMS)
def test_direct_child_transitive_capture_crosses_control_flow(form):
    codegen = _hoist(_form_source(form))
    captured = [captures for name, captures in codegen._hoisted_capture_params.items()
                if name.endswith('factory')]
    assert captured and any('token' in names for names in captured)
    calls = [node for node in _walk(codegen.ast_module) if isinstance(node, Call)]
    synthetic = [(call, index) for call in calls for kind, index in call.operand_order
                 if kind == 'capture']
    assert synthetic
    assert any(call.kwargs[index][0] == 'token' for call, index in synthetic)


def test_global_declaration_stops_transitive_capture():
    codegen = _hoist('token = 99\ndef outer(token):\n'
                    '    def factory():\n        global token\n'
                    '        def leaf():\n            return token\n'
                    '        return leaf()\n    return factory()\n')
    for name, captures in codegen._hoisted_capture_params.items():
        if name.endswith(('factory', 'leaf')):
            assert 'token' not in captures


def test_capture_marker_filters_only_synthetic_keywords():
    module = infer_module(parse_and_lift('', 'transport.py', 'transport'))
    codegen = L1CodeGen(module, ir_scaffold_mode='on')
    ty = DynType(name='dyn')
    value = Name(span=None, ty=ty, ident='value')
    call = Call(span=None, ty=ty, func=Name(span=None, ty=ty, ident='factory'),
                args=(), kwargs=(('token', value), ('token', value)),
                operand_order=(('kw', 0), ('capture', 1)))
    args, kwargs = codegen._slot_call_split_operands(call)
    assert args == () and kwargs == (('token', value),)
    # An unmarked source keyword remains a source keyword even if its name
    # matches a captured cell. AST/wire schema does not gain a new field.
    unmarked = replace(call, kwargs=(('token', value),), operand_order=(('kw', 0),))
    assert codegen._slot_call_split_operands(unmarked)[1] == unmarked.kwargs


def test_capture_markers_preserve_mapping_keyword_order():
    module = infer_module(parse_and_lift('', 'transport.py', 'transport'))
    codegen = L1CodeGen(module, ir_scaffold_mode='on')
    ty = DynType(name='dyn')
    value = Name(span=None, ty=ty, ident='value')
    mapping = Call(span=None, ty=ty, func=Name(span=None, ty=ty, ident='**'), args=(value,), kwargs=())
    call = Call(span=None, ty=ty, func=Name(span=None, ty=ty, ident='factory'),
                args=(mapping,), kwargs=(('first', value), ('last', value), ('token', value)),
                operand_order=(('kw', 0), ('arg', 0), ('kw', 1), ('capture', 2)))
    args, kwargs = codegen._slot_call_split_operands(call)
    assert args == ()
    assert tuple(name for name, _ in kwargs) == ('first', '**', 'last')


PROGRAMS = {
    'transitive_factory.py': '''def outer():
    analyze = {'answer': 42}
    def factory():
        def leaf():
            return analyze
        return leaf()
    assert factory() is analyze
outer()
print('TRANSITIVE_FACTORY_OK')
''',
    'lifetime_identity.py': '''import gc
import weakref
class Token:
    pass
def outer():
    token = Token()
    reference = weakref.ref(token)
    def factory():
        def leaf():
            return token
        return leaf
    return factory(), reference
leaf, reference = outer()
gc.collect()
assert leaf() is reference()
del leaf
gc.collect()
assert reference() is None
print('LIFETIME_IDENTITY_OK')
''',
    'unbound_lifetime.py': '''import gc
def outer():
    token: object
    def factory():
        def leaf():
            return token
        return leaf
    leaf = factory()
    try:
        leaf()
    except NameError:
        pass
    else:
        raise AssertionError('missing unbound capture error')
    token = {'answer': 42}
    gc.collect()
    assert leaf() is token
    del token
    return leaf
leaf = outer()
gc.collect()
try:
    leaf()
except NameError:
    pass
else:
    raise AssertionError('deleted capture survived')
print('UNBOUND_LIFETIME_OK')
''',
    'capture_binding.py': '''import gc
def outer():
    analyze = {'answer': 42}
    def factory():
        def leaf():
            return analyze
        return leaf()
    assert factory() is analyze
    try:
        factory(analyze=99)
    except TypeError:
        pass
    else:
        raise AssertionError('explicit capture keyword was discarded')
    def accepts(**kwargs):
        def leaf():
            return analyze
        return leaf(), kwargs
    value, kwargs = accepts(analyze=99)
    assert value is analyze and kwargs == {'analyze': 99}
    def defaults(value=analyze, *, flag=True):
        gc.collect()
        return value
    assert defaults(*(), **{'flag': True}) is analyze
    try:
        defaults(flag=True, **{'flag': False})
    except TypeError:
        pass
    else:
        raise AssertionError('duplicate keyword was accepted')
    return factory
escaped = outer()
gc.collect()
assert escaped() == {'answer': 42}
print('CAPTURE_BINDING_OK')
''',
}
EXPECTED = {
    'transitive_factory.py': 'TRANSITIVE_FACTORY_OK\n',
    'lifetime_identity.py': 'LIFETIME_IDENTITY_OK\n',
    'unbound_lifetime.py': 'UNBOUND_LIFETIME_OK\n',
    'capture_binding.py': 'CAPTURE_BINDING_OK\n',
}


def _capture_forms_program():
    parts = [MANAGER]
    for index, form in enumerate(FORMS):
        source = _form_source(form)[len(MANAGER):].replace('def outer(', 'def outer_' + str(index) + '(')
        parts.append(source + '\nstate = {"value": 42}\nassert outer_' + str(index) + '(state) is state\n')
    return '\n'.join(parts) + "print('CAPTURE_FORMS_OK')\n"


PROGRAMS['capture_forms.py'] = _capture_forms_program()
EXPECTED['capture_forms.py'] = 'CAPTURE_FORMS_OK\n'


@pytest.mark.integration
@pytest.mark.parametrize('case', PROGRAMS)
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_transitive_capture_execution(case, python_program_compiler, request,
                                      explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(PROGRAMS[case], EXPECTED[case], tmp_path, python_program_compiler,
                         mode, explicit_owned_runtime, capfd, provenance_probe='2')
    assert (tmp_path/'compiler-wrapper.stderr').read_text() == ''
