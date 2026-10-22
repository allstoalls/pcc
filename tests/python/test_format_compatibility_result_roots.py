"""Foreign-origin format operands retain the shared managed result contract."""
from __future__ import annotations

import re

import pytest

from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.py_lift import parse_and_lift
from pcc.frontends.python.type_infer import infer_module
from tests.owned_ir_validation import verify_ir_text


def _emit(source, *, strict=False):
    module = infer_module(parse_and_lift(source, 'format_bridge.py', 'format_bridge'))
    codegen = L1CodeGen(module, emit_cpy_main_exitcode=False, ir_scaffold_mode='off')
    codegen._strict_no_libpython = strict
    text = str(codegen.generate(module))
    verify_ir_text(text)
    return text


def _body(text):
    match = re.search(r'^define [^\n]*@user_format_bridge_probe\([^\n]*\) \{\n(.*?)^\}',
                      text, re.M | re.S)
    assert match is not None
    return match.group(1)


def _published(text, runtime):
    calls = list(re.finditer(r'(%[^\s]+) = call ptr [^\n]*@' + runtime
                            + r'\([^\n]*\)\n', text))
    assert calls, runtime
    roots = []
    for call in calls:
        store = re.match(r'\s*store ptr ' + re.escape(call[1]) + r', ptr (%[^\s,]+)',
                         text[call.end():])
        assert store is not None, text[call.end():call.end() + 300]
        roots.append(store[1])
    return calls, roots


@pytest.mark.parametrize('expression', [
    'format(make_value())', 'format(make_value(), "")',
    'format(item)', 'format(item, spec)',
    'format(17, make_spec())', 'format(make_value(), make_spec())',
    'f"prefix:{item}:suffix"',
])
@pytest.mark.parametrize('site', ['argument', 'return', 'default', 'later-error'])
def test_foreign_format_result_uses_shared_output_slot(tmp_path, expression, site):
    prefix = ('from compatibility_provider import make_value, make_spec\n'
              'def take(*, value, later=None, **extras):\n    return value\n'
              'def fail():\n    raise ValueError("later")\n')
    setup = '    item = make_value()\n    spec = make_spec()\n'
    if site == 'return':
        statement = '    return ' + expression + '\n'
    elif site == 'default':
        statement = ('    def target(value=' + expression + '):\n'
                     '        return value\n    return target()\n')
    elif site == 'later-error':
        statement = '    return take(value=' + expression + ', later=fail())\n'
    else:
        statement = '    return take(value=' + expression + ')\n'
    text = _emit(prefix + 'def probe():\n' + setup + statement)
    (tmp_path / 'probe.ll').write_text(text)
    body = _body(text)
    calls, _roots = _published(body, 'py_obj_format')
    _published(body, 'py_cpy_to_pcc_obj')
    # The selected compatibility producers still own foreign evaluation.
    assert '@py_cpy_call_noargs' in body
    # The raw format result is consumed only by immediate publication. All
    # subsequent users must reload or transfer the output slot's owner.
    for call in calls:
        assert len(re.findall(re.escape(call[1]) + r'(?![\w.])', body)) == 2


def test_format_keeps_foreign_value_then_spec_order_and_live_roots(tmp_path):
    text = _emit('''from compatibility_provider import make_value, make_spec
def probe():
    return format(make_value(), make_spec())
''')
    (tmp_path / 'order.ll').write_text(text)
    body = _body(text)
    value = re.search(r'%cpy\.call0\.make_value[^\s]* = call [^\n]*@py_cpy_call_noargs', body)
    spec = re.search(r'%cpy\.call0\.make_spec[^\s]* = call [^\n]*@py_cpy_call_noargs', body)
    calls, roots = _published(body, 'py_obj_format')
    bridges, operands = _published(body, 'py_cpy_to_pcc_obj')
    assert value and spec and len(calls) == 1 and len(bridges) == 2
    assert value.start() < bridges[0].start() < spec.start() < bridges[1].start() < calls[0].start()
    assert '@pcc_gc_take_pinned_slot' not in body[bridges[0].end():calls[0].start()]
    assert operands[0] != operands[1] != roots[0]
    tail = body[calls[0].end():]
    assert '@pcc_gc_foreign_lease_release' in tail
    assert '@pcc_gc_store_root' in tail
    assert '@pcc_gc_take_pinned_slot' in tail


def test_format_preserves_strict_no_libpython_import_failure(tmp_path):
    text = _emit('''def probe():
    from unavailable_compatibility_provider import make_value
    return format(make_value())
''', strict=True)
    (tmp_path / 'strict.ll').write_text(text)
    assert '@py_raise' in _body(text)
    assert not re.search(r'\bcall [^\n]*@py_cpy_', text)


def test_format_does_not_treat_foreign_spelling_as_native_owner(tmp_path):
    text = _emit('''from compatibility_provider import item, spec
def probe():
    return format(item, spec)
''')
    (tmp_path / 'globals.ll').write_text(text)
    body = _body(text)
    bridges, _roots = _published(body, 'py_cpy_to_pcc_obj')
    assert len(bridges) == 2
    # Imported globals are borrowed CPython references, so converting them
    # creates a distinct PCC owner without consuming the foreign binding.
    for bridge in bridges:
        source = re.search(r'@py_cpy_to_pcc_obj\(ptr (%[^\s)]+)\)', bridge[0])[1]
        assert not re.search(r'@py_cpy_decref\(ptr ' + re.escape(source) + r'\)', body)


def test_original_class_global_name_formatting_source(tmp_path):
    import ast
    import inspect
    from pathlib import Path

    from pcc.frontends.python.codegen.class_gen import ClassLowering

    path = Path(inspect.getsourcefile(ClassLowering))
    original = path.read_text()
    tree = ast.parse(original)
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                 and node.name == 'ClassLowering')
    method = next(node for node in owner.body if isinstance(node, ast.FunctionDef)
                  and node.name == '_class_global_name')
    # Preserve this original method's exact text and its original import.
    lines = original.splitlines(keepends=True)
    method_source = ''.join(lines[method.lineno - 1:method.end_lineno])
    source = ('from pcc.frontends.python.codegen.module_name_lowering import module_symbol_suffix\n'
              'class ClassLowering:\n' + method_source)
    module = infer_module(parse_and_lift(source, str(path), 'pcc.frontends.python.codegen.class_gen'))
    codegen = L1CodeGen(module, emit_cpy_main_exitcode=False, ir_scaffold_mode='off')
    text = str(codegen.generate(module))
    verify_ir_text(text)
    (tmp_path / 'original-method.py').write_text(source)
    (tmp_path / 'original-method.ll').write_text(text)
    method_ir = re.search(r'^define [^\n]*@user_pcc_frontends_python_codegen_class_gen_ClassLowering__class_global_name\([^\n]*\) \{\n(.*?)^\}', text, re.M | re.S)
    assert method_ir is not None
    _published(method_ir[1], 'py_obj_format')
    _published(method_ir[1], 'py_cpy_to_pcc_obj')
