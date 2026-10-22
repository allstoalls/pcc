"""Module except-as owns the shared global binding until automatic deletion."""
import textwrap

import pytest

from pcc.backend.owned_object_emit import emit_owned_object
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.py_lift import parse_and_lift
from pcc.frontends.python.type_infer import infer_module
from tests.python.owned_regression_support import assert_owned_program, explicit_owned_runtime


PROGRAM = textwrap.dedent('''\
    import gc
    error = False
    def lookup():
        gc.collect()
        return error
    try:
        raise ValueError('module binding')
    except ValueError as error:
        assert lookup() is error
        assert str(error) == 'module binding'
        del error
    try:
        lookup()
    except NameError:
        pass
    else:
        raise AssertionError('handler binding survived normal exit')
    try:
        try:
            raise ValueError('replacement')
        except ValueError as error:
            assert lookup() is error
            error = {'changed': 1}
            assert lookup() is error
            raise TypeError('outer')
    except TypeError:
        try:
            lookup()
        except NameError:
            pass
        else:
            raise AssertionError('handler binding survived exceptional exit')
    print('MODULE_EXCEPTION_BINDING_OK')
''')


def test_module_handler_uses_predeclared_global_and_checked_function_reads():
    module = infer_module(parse_and_lift(PROGRAM, '<module-binding>', 'module_binding'))
    codegen = L1CodeGen(module, ir_scaffold_mode='on')
    codegen._target_triple = 'x86_64-unknown-linux-gnu'
    codegen._strict_no_libpython = True
    text = codegen.generate(module)
    slot, _ = codegen._module_globals['error']
    assert str(slot.value_type) == 'ptr' or str(slot.value_type).endswith('*')
    assert 'error' in codegen._module_del_target_names
    assert '.modvar.module_binding.error_initialized' in text
    assert 'except.global.current' in text
    assert '@py_module_attr_del(' in text
    assert '@pcc_gc_root_copy_lease(' in text
    assert len(emit_owned_object(text, 'x86_64-unknown-linux-gnu')) > 0


@pytest.mark.integration
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_module_handler_global_binding_native_five_gc(python_program_compiler, request,
                                                     explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(PROGRAM, 'MODULE_EXCEPTION_BINDING_OK\n', tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime,
                         capfd, provenance_probe='2')
    assert (tmp_path / 'compiler-wrapper.stderr').read_text() == ''
