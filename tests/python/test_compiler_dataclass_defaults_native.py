"""Execute original compiler module defaults, not rewritten caller substitutes."""
from __future__ import annotations

import importlib
import os
import subprocess

import pytest

from pcc.frontends.python.pipeline import compile_python_multi


PY_AST_CONSUMER = '''from pcc.frontends.python.py_ast import ClassType, ValueClassType

def main():
    base = ClassType('Base', 'model')
    value = ValueClassType('Value', 'model')
    assert base.fields == ()
    assert base.bases == ()
    assert base.properties == ()
    assert value.fields == ()
    assert value.bases == ()
    assert value.properties == ()
    assert value.valueclass is False
    assert value.flattened is True
    assert value.nullable_fields is False
    print('ORIGINAL_PY_AST_INHERITANCE_OK')
main()
'''


C_SSA_CONSUMER = '''from pcc.frontends.c.ssa.ir import (
    SSAParam, SSAConstant, SSAStringConstant, SSAGlobalRef, SSAUndef,
    SSAInstruction, SSAUnaryOp, SSACast, SSALoad, SSAFieldAddr,
    SSAFieldExtract, SSAStore, SSAStackAlloc, SSABinaryOp, SSACall, SSAPhi,
)

def main():
    values = [SSAParam('p'), SSAConstant('c'), SSAStringConstant('s'),
              SSAGlobalRef('g'), SSAUndef('u')]
    instructions = [SSAInstruction('i'), SSAUnaryOp('unary'), SSACast('cast'),
                    SSALoad('load'), SSAFieldAddr('addr'), SSAFieldExtract('extract'),
                    SSAStore('store'), SSAStackAlloc('alloc'), SSABinaryOp('binary'),
                    SSACall('call'), SSAPhi('phi')]
    for value in values:
        assert value.type_name == 'int'
    for instruction in instructions:
        assert instruction.type_name == 'int'
        assert instruction.source_coord is None
        assert instruction.available_bindings == ()
    left = SSAPhi('left')
    right = SSAPhi('right')
    assert left.incomings is not right.incomings
    supplied = (('x', 'y'),)
    custom = SSAUnaryOp('custom', type_name='long', source_coord='input.c:7', available_bindings=supplied)
    assert custom.type_name == 'long'
    assert custom.source_coord == 'input.c:7'
    assert custom.available_bindings is supplied
    print('ORIGINAL_C_SSA_DEFAULTS_OK')
main()
'''


@pytest.mark.parametrize('module_name,program,expected', [
    pytest.param('pcc.frontends.python.py_ast', PY_AST_CONSUMER,
                 'ORIGINAL_PY_AST_INHERITANCE_OK\n', id='py_ast'),
    pytest.param('pcc.frontends.c.ssa.ir', C_SSA_CONSUMER,
                 'ORIGINAL_C_SSA_DEFAULTS_OK\n', id='c_ssa'),
])
def test_original_compiler_dataclass_defaults_native(module_name, program, expected,
                                                     tmp_path, pcc_runtime_archive):
    module_source = importlib.import_module(module_name).__file__
    consumer = tmp_path / 'default_consumer.py'
    binary = tmp_path / 'default_consumer'
    consumer.write_text(program)
    compile_python_multi([str(consumer), module_source], str(binary),
                         module_names=['default_consumer', module_name],
                         entry_module='default_consumer', backend='self',
                         libpython_mode='off', ir_scaffold_mode='on',
                         runtime_archive=str(pcc_runtime_archive))
    for collector in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10,
                                env=dict(os.environ, PCC_GC_BACKEND=str(collector),
                                         PCC_GC_REFCOUNT_PROVENANCE_PROBE='2'))
        assert (result.returncode, result.stdout, result.stderr) == (0, expected, ''), (collector, result)
