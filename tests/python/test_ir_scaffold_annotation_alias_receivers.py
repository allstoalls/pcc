"""Qualified provider aliases must not erase independent receiver proofs."""
from __future__ import annotations

import re

import pytest

from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.codegen.self_module_contracts import (
    CLASS_LOWERING_RECEIVER_CONTRACT,
    IR_SCAFFOLD_CONTRACT,
    SELF_MODULE_CONTRACTS,
)
from pcc.frontends.python.py_lift import parse_and_lift
from pcc.frontends.python.type_infer import infer_module
from tests.owned_ir_validation import verify_ir_text


def _generate(source, module_name):
    module = infer_module(parse_and_lift(source, 'receiver_alias.py', module_name))
    codegen = L1CodeGen(module, emit_cpy_main_exitcode=False, ir_scaffold_mode='on')
    codegen._strict_no_libpython = True
    text = str(codegen.generate(module))
    verify_ir_text(text)
    return text


def _method(text, module_name, method='_emit_method_body'):
    symbol = 'user_' + module_name.replace('.', '_') + '_ClassLowering_' + method
    match = re.search(r'^define [^\n]*@' + re.escape(symbol) + r'\([^\n]*\) \{\n(.*?)^\}', text, re.M | re.S)
    assert match
    return match[1]


@pytest.mark.parametrize('import_line', [
    'from pcc.ir.compat import ir',
    'from pcc.ir.compat import ir_py as ir',
    'from pcc.ir.compat import ir_c as ir',
    'from pcc.ir import ir',
    'import pcc.ir.ir as ir',
])
@pytest.mark.parametrize('module_name', ['pcc.frontends.python.codegen.class_gen', 'relocated.class_lowering'])
@pytest.mark.parametrize('capability', [False, True])
def test_annotation_alias_retains_only_existing_receiver_capability(tmp_path, monkeypatch, import_line, module_name, capability):
    monkeypatch.setitem(SELF_MODULE_CONTRACTS, module_name,
                        (IR_SCAFFOLD_CONTRACT, CLASS_LOWERING_RECEIVER_CONTRACT)
                        if capability else (IR_SCAFFOLD_CONTRACT,))
    text = _generate(import_line + '''
class ClassLowering:
    def _emit_method_body(self, fn: ir.Function):
        return fn.append_basic_block(name='entry')
''', module_name)
    (tmp_path / 'alias.ll').write_text(text)
    body = _method(text, module_name)
    direct = re.search(r'\bcall [^\n]*@user_pcc_ir_ir_scaffold_Function_append_basic_block\(', body)
    assert bool(direct) is capability
    assert not re.search(r'\bcall [^\n]*@py_cpy_', body)
    if not capability:
        assert '@py_obj_' in body


@pytest.mark.parametrize('shadow', [
    'ir = None', 'class ir:\n    pass', 'from unrelated import ir',
    'from unrelated import *',
])
def test_shadowed_alias_does_not_select_provider(tmp_path, shadow):
    module_name = 'pcc.frontends.python.codegen.class_gen'
    text = _generate('from pcc.ir.compat import ir\n' + shadow + '''
class ClassLowering:
    def _emit_method_body(self, fn: ir.Function):
        return fn.append_basic_block('entry')
''', module_name)
    (tmp_path / 'shadow.ll').write_text(text)
    body = _method(text, module_name)
    assert '@user_pcc_ir_ir_scaffold_Function_append_basic_block(' not in body


@pytest.mark.parametrize('annotation', ['foreign.Function', 'foreign.IRBuilder'])
def test_ordinary_class_annotation_is_still_rejected(tmp_path, annotation):
    module_name = 'pcc.frontends.python.codegen.class_gen'
    text = _generate('from pcc.ir.compat import ir\nimport ordinary_provider as foreign\n'
                     'class ClassLowering:\n'
                     '    def _emit_method_body(self, fn: ' + annotation + '):\n'
                     '        return fn.append_basic_block("entry")\n', module_name)
    (tmp_path / 'ordinary.ll').write_text(text)
    assert '@user_pcc_ir_ir_scaffold_Function_append_basic_block(' not in _method(text, module_name)


def test_provider_annotation_alone_does_not_prove_unknown_parameter(tmp_path):
    module_name = 'pcc.frontends.python.codegen.class_gen'
    text = _generate('''from pcc.ir.compat import ir
class ClassLowering:
    def ordinary(self, fn: ir.Function):
        return fn.append_basic_block('entry')
''', module_name)
    (tmp_path / 'unproved.ll').write_text(text)
    assert '@user_pcc_ir_ir_scaffold_Function_append_basic_block(' not in _method(text, module_name, 'ordinary')


@pytest.mark.parametrize('capability', [False, True])
def test_hoisted_builder_receiver_keeps_existing_fact(tmp_path, monkeypatch, capability):
    from pcc.frontends.python.codegen.hoist_boxing import CELL_READ
    from pcc.frontends.python.py_ast import Attr, Call, Name

    module_name = 'relocated.class_lowering'
    monkeypatch.setitem(SELF_MODULE_CONTRACTS, module_name,
                        (IR_SCAFFOLD_CONTRACT, CLASS_LOWERING_RECEIVER_CONTRACT)
                        if capability else (IR_SCAFFOLD_CONTRACT,))
    source = '''from pcc.ir.compat import ir
class ClassLowering:
    def _emit_method_body(self, fn: ir.Function):
        parent = self.parent
        def bind_method_arg(ir_arg: ir.Value, ir_ty: ir.Type):
            cell = parent.builder.call(ir_arg, [ir_arg])
            slot = parent.builder.alloca(ir_ty)
            parent.builder.store(cell, slot)
            return True
        return bind_method_arg(fn, fn)
'''
    observed = []
    original = L1CodeGen._maybe_emit_ir_scaffold_call
    def record(codegen, expr):
        if codegen.current_func_def.name == '__nested_bind_method_arg':
            receiver = expr.func.obj if isinstance(expr.func, Attr) else None
            if isinstance(receiver, Attr) and receiver.name == 'builder':
                base = receiver.obj
                assert isinstance(base, Call) and isinstance(base.func, Name)
                assert base.func.ident == CELL_READ
                assert 'parent' in codegen._hoisted_capture_params[codegen.current_func_def.name]
                observed.append(str(base))
        return original(codegen, expr)
    monkeypatch.setattr(L1CodeGen, '_maybe_emit_ir_scaffold_call', record)
    text = _generate(source, module_name)
    (tmp_path / 'nested.ll').write_text(text)
    (tmp_path / 'transformed-receivers.txt').write_text('\n'.join(observed))
    assert len(observed) == 3
    nested = re.search(r'^define [^\n]*@user_relocated_class_lowering___nested_bind_method_arg\([^\n]*\) \{\n(.*?)^\}', text, re.M | re.S)
    assert nested
    for symbol in ('IRBuilder_call1', 'IRBuilder_alloca', 'IRBuilder_store'):
        assert ('@user_pcc_ir_ir_' + symbol + '(' in nested[1]) is capability
    assert not re.search(r'\bcall [^\n]*@py_cpy_', nested[1])


@pytest.mark.parametrize('invalid', [
    'ordinary-call', 'wrong-label', 'not-free', 'missing-capture',
    'wrong-owner', 'missing-capability', 'annotation-only', 'keyword',
])
def test_cell_read_shape_does_not_manufacture_receiver_provenance(monkeypatch, invalid):
    from dataclasses import replace
    from types import SimpleNamespace
    from pcc.frontends.python.codegen.hoist_boxing import _cell_read
    from pcc.frontends.python.py_ast import Attr, BoolLit, DynType, Name, StrLit, StrType

    module_name = 'relocated.class_lowering'
    monkeypatch.setitem(SELF_MODULE_CONTRACTS, module_name,
                        () if invalid == 'missing-capability'
                        else (IR_SCAFFOLD_CONTRACT, CLASS_LOWERING_RECEIVER_CONTRACT))
    typed = infer_module(parse_and_lift('from pcc.ir.compat import ir\n', 'cell.py', module_name))
    codegen = L1CodeGen(typed, ir_scaffold_mode='on')
    codegen.current_class = SimpleNamespace(name='Ordinary' if invalid == 'wrong-owner' else 'ClassLowering',
                                           owning_module=module_name, export_class_name=None)
    name = 'unknown' if invalid == 'annotation-only' else 'parent'
    codegen.current_func_def = SimpleNamespace(name='__nested_bind_method_arg',
                                              args=(SimpleNamespace(name=name),))
    codegen._hoisted_capture_params['__nested_bind_method_arg'] = () if invalid == 'missing-capture' else (name,)
    read = _cell_read(name, None, free=True)
    if invalid == 'ordinary-call':
        read = replace(read, func=Name(span=None, ty=DynType('dyn'), ident='cell_read'))
    elif invalid == 'wrong-label':
        read = replace(read, args=(read.args[0], StrLit(span=None, ty=StrType('str'), value='other'), read.args[2]))
    elif invalid == 'not-free':
        read = replace(read, args=(read.args[0], read.args[1], replace(read.args[2], value=False)))
    elif invalid == 'keyword':
        read = replace(read, kwargs=(('unexpected', read.args[0]),))
    receiver = Attr(span=None, ty=DynType('dyn'), obj=read, name='builder')
    assert not codegen._scaffold_receiver_admitted(receiver, 'store')
