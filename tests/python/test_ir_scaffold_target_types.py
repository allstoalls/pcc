"""All scalar IR formats keep their constructor and class identity natively."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

from pcc.ir import ir
from pcc.frontends.python import type_infer
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.py_lift import parse_and_lift


_SCALAR_TYPES = ('VoidType', 'HalfType', 'FloatType', 'DoubleType', 'X86FP80Type', 'FP128Type')


@pytest.mark.parametrize('symbol', _SCALAR_TYPES)
def test_scalar_constructor_and_class_expression_lowering(symbol):
    source = f'''from pcc.ir.compat import ir_c as ir
def main():
    value = ir.{symbol}()
    cls = ir.{symbol}
    return isinstance(value, ir.{symbol}) and type(value) is cls
'''
    module = type_infer.infer_module(parse_and_lift(source, 'scalar.py', 'scalar'))
    codegen = L1CodeGen(module, ir_scaffold_mode='on')
    emitted = str(codegen.generate(module))
    assert f'@user_pcc_ir_ir_{symbol}___init__' in emitted
    assert f'@.class.pcc_ir_ir.{symbol}' in emitted
    assert not re.search(r'\bcall [^\n]*@py_cpy_', emitted)


def test_all_exported_singleton_type_constructors_are_registered():
    from pcc.frontends.python.codegen.ir_scaffold_lowering import (
        _IR_MODULE_SYMBOLS, _IR_SCAFFOLD_SIMPLE_SYMBOLS, _IR_SCAFFOLD_SYMBOL_IMPL,
    )
    exported = {name for name in ir.__all__
                if isinstance(getattr(ir, name), type)
                and issubclass(getattr(ir, name), ir._SingletonType)}
    assert exported == set(_SCALAR_TYPES)
    for name in exported:
        assert name in _IR_MODULE_SYMBOLS
        assert name in _IR_SCAFFOLD_SYMBOL_IMPL
        assert _IR_SCAFFOLD_SIMPLE_SYMBOLS[name] == (0, False)
        assert callable(getattr(ir, name + '___init__'))


NATIVE_SOURCE = '''from pcc.ir.compat import ir_c as ir
from pcc.ir.ir import X86FP80Type, FP128Type
from pcc.frontends.c.codegen.c_types import long_double_type
from pcc.frontends.c.codegen.c_layout import floating_ir_width, ir_type_size, ir_type_align

def main():
    x87 = ir.X86FP80Type()
    quad = ir.FP128Type()
    assert str(x87) == 'x86_fp80'
    assert str(quad) == 'fp128'
    assert isinstance(x87, ir.X86FP80Type)
    assert isinstance(quad, ir.FP128Type)
    assert isinstance(x87, (ir.X86FP80Type, ir.FP128Type))
    assert not isinstance(x87, ir.FP128Type)
    assert not isinstance(quad, ir.X86FP80Type)
    assert type(x87) is ir.X86FP80Type
    assert type(quad) is ir.FP128Type
    assert ir.X86FP80Type is X86FP80Type
    assert ir.FP128Type is FP128Type
    alias = ir.X86FP80Type
    assert type(alias()) is X86FP80Type
    alias = ir.FP128Type
    assert type(alias()) is FP128Type
    assert x87 is ir.X86FP80Type()
    assert quad is ir.FP128Type()
    assert x87 is not quad
    assert type(ir.HalfType()) is ir.HalfType
    assert type(ir.FloatType()) is ir.FloatType
    assert type(ir.DoubleType()) is ir.DoubleType
    linux_x87 = long_double_type('x86_64-unknown-linux-gnu')
    linux_quad = long_double_type('aarch64-unknown-linux-gnu')
    windows_x87 = long_double_type('x86_64-w64-windows-gnu')
    windows_double = long_double_type('x86_64-pc-windows-msvc')
    darwin_double = long_double_type('aarch64-apple-darwin')
    assert type(linux_x87) is ir.X86FP80Type
    assert type(windows_x87) is ir.X86FP80Type
    assert type(linux_quad) is ir.FP128Type
    assert type(windows_double) is ir.DoubleType
    assert type(darwin_double) is ir.DoubleType
    for value in [linux_x87, linux_quad, windows_x87]:
        assert ir_type_size(value) == 16
        assert ir_type_align(value) == 16
    assert floating_ir_width(linux_x87) == 80
    assert floating_ir_width(linux_quad) == 128
    assert ir_type_size(windows_double) == 8
    assert ir_type_align(darwin_double) == 8
    print('IR_TARGET_TYPES_OK')
main()
'''


def test_native_target_type_identity_and_layout(tmp_path, monkeypatch, pcc_runtime_archive, python_program_compiler):
    # These imports are source inputs to the native program. CPython's sys.path
    # does not admit them to a native closure rooted at pytest's tmp_path.
    repo = Path(__file__).resolve().parents[2]
    monkeypatch.setenv('PCC_PACKAGE_SITE', str(repo))
    source = tmp_path / 'target_types.py'
    output = tmp_path / 'target_types'
    source.write_text(NATIVE_SOURCE)
    reference = subprocess.run([sys.executable, str(source)],
                               env=dict(os.environ, PYTHONPATH=str(repo)),
                               capture_output=True, text=True, timeout=20)
    assert reference.returncode == 0, reference.stderr
    assert reference.stdout == 'IR_TARGET_TYPES_OK\n'
    python_program_compiler(str(source), str(output), backend='self',
                            libpython_mode='off', ir_scaffold_mode='on',
                            runtime_archive=str(pcc_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(output)], env=dict(os.environ, PCC_GC_BACKEND=str(gc)),
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, f'GC {gc}: {result.stdout}\n{result.stderr}'
        assert result.stdout == reference.stdout, f'GC {gc}: {result.stdout}'
