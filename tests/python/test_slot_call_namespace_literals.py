"""Predefined strings acquire real owners; shadowed Names retain their slots."""
from __future__ import annotations
import re
import textwrap
import pytest
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.py_lift import parse_and_lift
from pcc.frontends.python.type_infer import infer_module
from tests.python.owned_regression_support import assert_owned_program, explicit_owned_runtime


def _emit(source):
    module = infer_module(parse_and_lift(source, 'namespace_literal.py', 'namespace_literal'))
    codegen = L1CodeGen(module, ir_scaffold_mode='on')
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    return str(codegen.generate(module))


def _probe(text):
    body = re.search(r'^define [^\n]*@user_namespace_literal_probe\([^\n]*\).*?^}', text, re.M | re.S)
    assert body is not None
    return body.group(0)


@pytest.mark.parametrize('name', ('__file__', '__name__', '__package__', '__doc__'))
def test_namespace_string_operand_has_an_owner(name):
    body = _probe(_emit('"module documentation"\ndef take(*, value, **extras):\n    return value\n'
                        'def probe():\n    return take(value=' + name + ')\n'))
    assert '@py_obj_call_slots(' in body
    assert '@pcc_gc_foreign_lease_acquire(' in body
    assert '@py_cpy_' not in body


@pytest.mark.parametrize('name', ('__file__', '__name__', '__package__', '__doc__'))
@pytest.mark.parametrize('scope', ('local', 'global'))
def test_shadowed_namespace_uses_authoritative_slot(name, scope):
    prefix = name + ' = {}\n' if scope == 'global' else ''
    parameter = name if scope == 'local' else ''
    body = _probe(_emit(prefix + 'def take(*, value, **extras):\n    return value\n'
                        'def probe(' + parameter + '):\n    return take(value=' + name + ')\n'))
    assert re.search(r'@pcc_gc_root_copy_(?:borrowed_)?lease\(', body)


PROGRAM = textwrap.dedent('''\
    "module documentation"
    import gc
    def take(*, value, **extras):
        gc.collect()
        return value
    def predefined():
        assert take(value=__file__).endswith('program.py')
        assert take(value=__name__) == '__main__'
        assert take(value=__package__) is __package__
        assert take(value=__doc__) == 'module documentation'
    def local(__file__, __name__, __package__, __doc__):
        assert take(value=__file__) is __file__
        assert take(value=__name__) is __name__
        assert take(value=__package__) is __package__
        assert take(value=__doc__) is __doc__
    def unbound():
        try:
            take(value=__file__)
        except UnboundLocalError:
            return True
        __file__ = 'local'
        return False
    def main():
        predefined()
        state = {'value': 42}
        local(state, state, state, state)
        assert unbound()
    main()
    print('NAMESPACE_LITERAL_OK')
''')
GLOBAL_SHADOW_PROGRAM = textwrap.dedent('''\
    import gc
    __file__ = {'file': 1}
    __name__ = {'name': 2}
    __package__ = {'package': 3}
    __doc__ = {'doc': 4}
    def take(*, value, **extras):
        gc.collect()
        return value
    assert take(value=__file__) == {'file': 1}
    assert take(value=__name__) == {'name': 2}
    assert take(value=__package__) == {'package': 3}
    assert take(value=__doc__) == {'doc': 4}
    print('NAMESPACE_LITERAL_OK')
''')


@pytest.mark.integration
@pytest.mark.parametrize('program', (PROGRAM, GLOBAL_SHADOW_PROGRAM), ids=('pooled', 'global-shadow'))
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_namespace_literals_native_five_gc(program, python_program_compiler, request,
                                           explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(program, 'NAMESPACE_LITERAL_OK\n', tmp_path, python_program_compiler,
                         mode, explicit_owned_runtime, capfd, provenance_probe='2')
    assert (tmp_path/'compiler-wrapper.stderr').read_text() == ''


def test_repo_root_uses_real_pathlib_export_context(tmp_path):
    from pathlib import Path
    from pcc.frontends.python.pipeline_context import build_closed_world_context
    provider = Path(__file__).resolve().parents[2]/'pcc/stdlib/pathlib.py'
    entry = tmp_path/'repo_root.py'
    entry.write_text('from pathlib import Path\ndef _repo_root() -> Path:\n'
                     '    return Path(__file__).resolve().parents[2]\n')
    modules, exports, _derived = build_closed_world_context(
        [str(provider), str(entry)], ['pathlib', 'repo_root'])
    typed = infer_module(modules[1], external_exports={'pathlib': exports['pathlib']})
    codegen = L1CodeGen(typed, ir_scaffold_mode='on')
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    codegen._native_module_exports = exports
    text = str(codegen.generate(typed))
    assert '@py_obj_call_slots(' in text
    assert 'define' in text and '@user_repo_root__repo_root(' in text
