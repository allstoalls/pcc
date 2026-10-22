"""Imported-call return domains must agree with the executing value binding."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest


NATIVE_IMPORT = "import mixed_package.native\n"
MISSING_IMPORT = "try:\n    import mixed_package.unavailable\nexcept ImportError:\n    pass\n"
FUNCTIONS = '''def produce(value):
    return mixed_package.native.produce(value)

def consume(value):
    produced = produce(value)
    return mixed_package.native.accept(produced)

def rebind(replacement):
    globals()['mixed_package'] = replacement
'''
PROVIDER = '''def produce(value):
    return 'native:' + value

def accept(value):
    return value
'''
REPLACEMENT = PROVIDER.replace("'native:'", "'replacement:'")


def _write_sources(tmp_path, reverse=False):
    files = {
        'mixed_package': ('mixed_package/__init__.py', ''),
        'mixed_package.native': ('mixed_package/native.py', PROVIDER),
        'replacement_package': ('replacement_package/__init__.py', 'from . import native\n'),
        'replacement_package.native': ('replacement_package/native.py', REPLACEMENT),
        'consumer': ('consumer.py', (MISSING_IMPORT + NATIVE_IMPORT if reverse else NATIVE_IMPORT + MISSING_IMPORT) + FUNCTIONS),
    }
    paths = []
    names = []
    for name, (relative, source) in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
        paths.append(str(path))
        names.append(name)
    return paths, names


def _body(text, name):
    match = re.search(r'^define [^\n]*@' + re.escape(name) + r'\([^\n]*\).*?^}', text, re.M | re.S)
    assert match, name
    return match.group(0)


def _codegen(paths, names, *, strict=True):
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.pipeline_context import build_closed_world_context
    from pcc.frontends.python.type_infer import infer_module

    modules, exports, derived = build_closed_world_context(paths, names)
    external = {name: value for name, value in exports.items() if name != names[-1]}
    typed = infer_module(modules[-1], external_exports=external, derived_class_map=derived)
    codegen = L1CodeGen(typed, ir_scaffold_mode='on')
    codegen._native_module_exports = external
    codegen._sibling_module_inits = tuple(names[:-1])
    codegen._strict_no_libpython = strict
    codegen._prefer_native_callable_values = strict
    return codegen, typed


@pytest.mark.parametrize('reverse', (False, True))
def test_live_package_result_is_managed_despite_foreign_sibling_metadata(tmp_path, reverse):
    paths, names = _write_sources(tmp_path, reverse)
    codegen, typed = _codegen(paths, names)
    text = str(codegen.generate(typed))
    (tmp_path / 'consumer.ll').write_text(text)
    consume = _body(text, 'user_consumer_consume')
    produce = _body(text, 'user_consumer_produce')
    assert bool('@py_obj_call_slots' in produce)
    assert bool('@py_obj_call_slots' in consume)
    assert not re.search(r'call [^\n]*@py_cpy_', produce + consume)
    # The unavailable sibling retains its explicit failed-import route.
    error_message = "No module named 'mixed_package.unavailable'"
    encoded_error = ''.join('\\' + format(byte, '02X') for byte in error_message.encode())
    assert bool(encoded_error in text), 'unavailable sibling lost its explicit ImportError'
    assert bool(re.search(r'call [^\n]*@py_exc_new', _body(text, 'main')))
    assert 'mixed_package' in codegen._cpy_module_env


@pytest.mark.parametrize('prefix', (
    'import urllib.request as request\n',
    'import urllib.request\n',
))
def test_foreign_request_provider_keeps_cpython_return_domain(tmp_path, prefix):
    receiver = 'request' if ' as ' in prefix else 'urllib.request'
    path = tmp_path / 'foreign.py'
    path.write_text(prefix + 'def produce(value):\n    return ' + receiver + '.Request(value)\n')
    codegen, typed = _codegen([str(path)], ['foreign'], strict=False)
    text = str(codegen.generate(typed))
    assert codegen._user_func_returns_cpython(codegen._find_user_funcdef('produce'))
    assert re.search(r'call [^\n]*@py_cpy_', _body(text, 'user_foreign_produce'))


def test_lexical_foreign_slot_does_not_inherit_live_global_domain(tmp_path):
    from pcc.frontends.python.py_ast import Name, DynType
    from pcc.ir.compat import ir
    from pcc.frontends.python.codegen.errors import L1CodegenError

    paths, names = _write_sources(tmp_path)
    codegen, typed = _codegen(paths, names)
    codegen.generate(typed)
    expr = Name(span=typed.body[0].span, ty=DynType(name='dyn'), ident='mixed_package')
    # A different physical local slot shadows the live imported global and
    # owns a real foreign value. It must retain both the CPython domain and
    # the fail-closed slot-call check.
    foreign_slot = object()
    codegen.env['mixed_package'] = (foreign_slot, ir.IntType(8).as_pointer(), expr.ty)
    codegen._cpy_env_flags['mixed_package'] = True
    assert codegen._expr_looks_cpython(expr)
    with pytest.raises(L1CodegenError, match='CPython name requires an output-slot bridge'):
        codegen._slot_call_name_source(expr)


@pytest.mark.integration
@pytest.mark.parametrize('reverse', (False, True))
def test_live_package_return_domain_executes_rebinding_five_gc(tmp_path, monkeypatch, pcc_runtime_archive, reverse):
    from pcc.frontends.python.pipeline import compile_python_multi
    from pcc.tools.runtime_archive_provenance import manifest_is_stale_for_current_codegen
    from pcc.diagnostics.gc_log import parse_log_lines

    manifest = json.loads(Path(str(pcc_runtime_archive) + '.provenance.json').read_text())
    assert not manifest_is_stale_for_current_codegen(manifest), 'runtime/codegen identity mismatch'
    for name, value in {
        'PCC_PY_FRONTEND_JOBS': '1', 'PCC_SELF_BACKEND_JOBS': '1',
        'PCC_NO_AUTO_PCC1': '1', 'PCC_SELF_LINK': 'pcc',
        'PCC_SELF_BACKEND_OBJECT_CACHE': '0', 'PCC_PY_FRONTEND_IR_CACHE': '0',
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv('LC_ALL', raising=False)
    paths, names = _write_sources(tmp_path, reverse)
    entry = tmp_path / 'entry.py'
    entry.write_text('''import gc
import consumer
import replacement_package
def main():
    assert consumer.consume('before') == 'native:before'
    consumer.rebind(replacement_package)
    assert consumer.consume('after') == 'replacement:after'
    gc.collect()
    print('LIVE_IMPORT_RETURN_DOMAIN_OK')
main()
''')
    paths.append(str(entry))
    names.append('entry')
    reference = subprocess.run([sys.executable, str(entry)], capture_output=True, text=True, timeout=20)
    assert (reference.returncode, reference.stdout, reference.stderr) == (0, 'LIVE_IMPORT_RETURN_DOMAIN_OK\n', '')
    binary = tmp_path / 'native'
    compile_python_multi(paths, str(binary), module_names=names, entry_module='entry',
                         backend='self', libpython_mode='off', ir_scaffold_mode='on',
                         runtime_archive=str(pcc_runtime_archive))
    for collector in range(5):
        log = tmp_path / ('gc' + str(collector) + '.jsonl')
        environment = dict(os.environ, PCC_GC_BACKEND=str(collector), PATH='/nonexistent',
                           PCC_HOST_PYTHON='/unavailable/host-python', PCC_LOG='gc',
                           PCC_LOG_FORMAT='json', PCC_LOG_FILE=str(log))
        actual = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30, env=environment)
        assert (actual.returncode, actual.stdout, actual.stderr) == (0, reference.stdout, reference.stderr)
        events = parse_log_lines(log.read_text().splitlines())
        observed = {event.fields['value1'] for event in events if event.fields.get('category') == 'gc'
                    and event.event in ('collect_start', 'collect_stop', 'collect_end')}
        assert observed == {collector}
