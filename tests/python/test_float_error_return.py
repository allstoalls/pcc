"""Floating Python error exits return to the caller's TLS error check."""
from pathlib import Path
import os
import re
import subprocess
from types import SimpleNamespace

import pytest

from pcc.frontends.python.codegen.exception_lowering import ExceptionLoweringMixin
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.py_lift import parse_and_lift
from pcc.frontends.python.type_infer import infer_module
from pcc.ir.compat import ir

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'tests/fixtures/float_error_return.py'


@pytest.mark.parametrize('return_type', (ir.FloatType(), ir.DoubleType()), ids=('f32', 'f64'))
def test_floating_error_epilogue_returns_exact_abi_type(return_type):
    module = ir.Module(name='float_error_abi')
    fn = ir.Function(module, ir.FunctionType(return_type, []), name='may_fail')
    builder = ir.IRBuilder(fn.append_basic_block('entry'))
    state = SimpleNamespace(current_function=fn, builder=builder,
                            _fn_err_exit_blocks={}, _fn_err_exit_finish_blocks={},
                            _fn_err_exit_owned_slots={}, _generator_ctx_stack=[])
    target = ExceptionLoweringMixin._ensure_fn_err_exit(state)
    builder.branch(target)
    finish = state._fn_err_exit_finish_blocks['may_fail']
    assert finish.is_terminated
    assert finish.instructions[-1].opname == 'ret'
    assert finish.instructions[-1].text == 'ret ' + str(return_type) + ' 0x0000000000000000'
    assert 'py_clear_exception' not in str(module)
    assert 'unreachable' not in str(module)


def test_float_raise_keeps_cleanup_and_caller_error_dispatch(tmp_path):
    module = infer_module(parse_and_lift(SOURCE.read_text(), str(SOURCE), 'float_error_return'))
    generator = L1CodeGen(module, emit_cpy_main_exitcode=False, ir_scaffold_mode='on')
    generator._strict_no_libpython = True
    text = str(generator.generate())
    (tmp_path / 'float_error_return.ll').write_text(text)
    body = re.search(r'^define external double @user_float_error_return_maybe_fail\([^\n]*\n.*?^}',
                     text, re.M | re.S).group()
    # The success value reaches the caller through the shared return exit.
    stored = re.search(r'store double 0x3FF4000000000000, ptr (%ret\.shared[\w.]*)', body)
    assert stored is not None
    loaded = re.search(r'(%[\w.]+) = load double, ptr ' + re.escape(stored[1])
                       + r'\n(?:[^\n:]*\n)*?\s*ret double \1\b', body)
    assert loaded is not None
    assert '@py_raise(' in body
    exit_body = re.search(r'^err.exit:\n(.*?)(?=\n\n)', body, re.M | re.S).group(1)
    assert '@pcc_gc_store_root(' in exit_body
    finish = re.search(r'^err.finish:\n(.*?)(?=\n\n)', body, re.M | re.S).group(1)
    assert '@pcc_gc_frame_leave(' in finish
    assert 'ret double 0x0000000000000000' in finish
    assert finish.index('@pcc_gc_frame_leave(') < finish.index('ret double ')
    assert 'unreachable' not in finish
    assert 'py_clear_exception' not in body
    main = re.search(r'^define [^\n]*@user_float_error_return_main\([^\n]*\n.*?^}',
                     text, re.M | re.S).group()
    assert re.search(r'call double[^\n]*@user_float_error_return_maybe_fail\(i1 1\)\n'
                     r'[^\n]*@py_err_occurred\(\)', main)
    assert '@py_exc_match_handler(' in main
    assert '@py_obj_call_slots(' in main
    assert not re.search(r'call[^\n]*@py_cpy_', text)


@pytest.mark.integration
def test_native_float_error_return_and_adapter(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    output = tmp_path / 'float_error_return'
    python_program_compiler(str(SOURCE), str(output), backend='self',
                            libpython_mode='off', ir_scaffold_mode='on',
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=30,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout == 'FLOAT_CAUGHT\nADAPTER_FLOAT_CAUGHT\n'
