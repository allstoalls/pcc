"""Closure conversion must preserve isinstance's classinfo Name semantics."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest


PROVIDER = '''class Token:
    pass
class Other:
    pass
'''

PROGRAM = '''import gc
from classinfo_provider import Token, Other
events = []

def imported(value):
    from classinfo_provider import Token as Kind
    def read():
        return Kind
    assert read() is Kind
    return isinstance(value, Kind)

def tuple_imported(value):
    from classinfo_provider import Token as Kind
    def read():
        return Kind
    assert read() is Kind
    return isinstance(value, (Kind, int))

def rebound():
    from classinfo_provider import Token as Kind
    def check(value):
        return isinstance(value, Kind)
    assert check(Token())
    assert not check(Other())
    Kind = Other
    gc.collect()
    assert check(Other())
    assert not check(Token())
    Kind = (Token, (Other, int))
    assert check(Token())
    assert check(Other())
    assert check(1)
    assert not check('wrong')

def unbound():
    Kind: object
    def operand(label):
        events.append(label)
        gc.collect()
        return Token()
    def check_free():
        return isinstance(operand('free'), Kind)
    try:
        isinstance(operand('local'), Kind)
    except UnboundLocalError:
        events.append('local-error')
    else:
        raise AssertionError('owning cell was bound')
    try:
        check_free()
    except UnboundLocalError:
        raise AssertionError('free read raised local error')
    except NameError:
        events.append('free-error')
    else:
        raise AssertionError('free cell was bound')
    try:
        isinstance(operand('tuple'), (Token, Kind))
    except UnboundLocalError:
        events.append('tuple-error')
    else:
        raise AssertionError('tuple construction skipped its unbound member')
    Kind = Token
    assert check_free()
    del Kind
    try:
        check_free()
    except UnboundLocalError:
        raise AssertionError('deleted free read raised local error')
    except NameError:
        events.append('deleted-error')
    else:
        raise AssertionError('deleted cell was bound')

def evaluation_order():
    from classinfo_provider import Other as Kind
    def operand():
        nonlocal Kind
        events.append('operand')
        Kind = Token
        gc.collect()
        return Token()
    assert isinstance(operand(), Kind)
    Kind = Other
    assert isinstance(operand(), (Kind, int))

def main():
    assert imported(Token())
    assert not imported(Other())
    assert tuple_imported(Token())
    assert tuple_imported(1)
    assert not tuple_imported(Other())
    rebound()
    unbound()
    evaluation_order()
    gc.collect()
    assert events == ['local', 'local-error', 'free', 'free-error', 'tuple',
                      'tuple-error', 'free', 'free', 'deleted-error',
                      'operand', 'operand']
    print('ISINSTANCE_CLOSURE_CLASSINFO_OK')
main()
'''


def _sources(tmp_path):
    provider = tmp_path / 'classinfo_provider.py'
    provider.write_text(PROVIDER)
    main = tmp_path / 'classinfo_main.py'
    main.write_text(PROGRAM)
    return [str(provider), str(main)]


def _compile(tmp_path, output, **options):
    from pcc.frontends.python.pipeline import compile_python_multi

    compile_python_multi(
        _sources(tmp_path), str(output),
        module_names=['classinfo_provider', 'classinfo_main'],
        entry_module='classinfo_main', backend='self',
        libpython_mode='off', ir_scaffold_mode='on', **options,
    )


def test_closure_classinfo_reference(tmp_path):
    paths = _sources(tmp_path)
    result = subprocess.run(
        [sys.executable, paths[1]],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'ISINSTANCE_CLOSURE_CLASSINFO_OK\n'


def test_closure_classinfo_owned_ir(tmp_path, monkeypatch):
    monkeypatch.setenv('PCC_PYTHON_IR_PASSES', 'off')
    monkeypatch.setenv('PCC_PY_FRONTEND_JOBS', '1')
    output = tmp_path / 'closure_classinfo.ll'
    _compile(tmp_path, output, emit_llvm_only=True)
    text = output.read_text()
    assert '@py_obj_isinstance(' in text
    assert 'cell.bound.error' in text
    assert 'isinstance.classinfo' in text
    assert 'extern.argument.cleanup' in text
    assert '@pcc_gc_take_pinned_slot(' in text


def test_source_factory_call_is_not_admitted_by_cell_repair():
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.type_infer import infer_module

    source = '''def factory():
    return int
def check(value):
    return isinstance(value, factory())
'''
    module = infer_module(parse_and_lift(source, '<classinfo-factory>', 'factory'))
    with pytest.raises(NotImplementedError, match='second argument.*got Call'):
        L1CodeGen(module, ir_scaffold_mode='on').generate(module)


@pytest.mark.parametrize('malformed', ('arity', 'name', 'free', 'keyword'))
def test_classinfo_cell_marker_keeps_normal_validation(malformed):
    from dataclasses import replace

    from pcc.frontends.python.codegen.closure_cell_lowering import emit_closure_cell_expr
    from pcc.frontends.python.codegen.hoist_boxing import _cell_read
    from pcc.frontends.python.py_ast import SourceSpan

    marker = _cell_read('Kind', SourceSpan('<classinfo-cell>', 1, 1, 1, 1))
    if malformed == 'arity':
        marker = replace(marker, args=marker.args[:2])
    elif malformed == 'name':
        marker = replace(marker, args=(marker.args[0], marker.args[2], marker.args[2]))
    elif malformed == 'free':
        marker = replace(marker, args=(marker.args[0], marker.args[1], marker.args[1]))
    else:
        marker = replace(marker, kwargs=(('extra', marker.args[0]),))
    with pytest.raises(ValueError, match='malformed compiler closure-cell read'):
        emit_closure_cell_expr(None, marker)


@pytest.mark.integration
def test_closure_classinfo_executes_all_collectors(
    tmp_path, monkeypatch, pcc_runtime_archive,
):
    monkeypatch.setenv('PCC_PYTHON_IR_PASSES', 'off')
    monkeypatch.setenv('PCC_PY_FRONTEND_JOBS', '1')
    output = tmp_path / 'closure_classinfo'
    _compile(tmp_path, output, runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run(
            [str(output)], capture_output=True, text=True, timeout=30,
            env=dict(os.environ, PATH='/nonexistent', PCC_GC_BACKEND=str(backend)),
        )
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout == 'ISINSTANCE_CLOSURE_CLASSINFO_OK\n'
