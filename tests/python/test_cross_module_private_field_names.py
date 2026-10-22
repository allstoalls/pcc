"""Closed-world field names retain their defining class's private spelling."""
from __future__ import annotations

import os
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest


FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures'
EXPECTED = (
    'default None None None\n'
    'callback base base child\n'
    'stored base base child\n'
    'callback None None child\n'
    "descriptors ('data-descriptor', 'instance-override', 'stored-through-descriptor')\n"
)


def _inputs(tmp_path):
    names = ['inherited_private_fields_main', 'inherited_private_fields_provider']
    paths = []
    for name in names:
        path = tmp_path / (name + '.py')
        path.write_bytes((FIXTURES / path.name).read_bytes())
        paths.append(str(path))
    return paths, names


def _compile(paths, names, output, **options):
    from pcc.frontends.python.pipeline import compile_python_multi
    compile_python_multi(
        paths, str(output), entry_module=names[0], module_names=names,
        backend='self', libpython_mode='off', ir_scaffold_mode='on',
        recursive_stdlib=False, **options,
    )


@pytest.mark.parametrize('class_name, expected', [
    ('Base', '_Base__value'), ('_Base', '_Base__value'), ('___', '__value'),
])
def test_private_field_exports_share_lexical_spelling(tmp_path, class_name, expected):
    from pcc.frontends.python.pipeline_context import build_closed_world_context
    source = tmp_path / 'owner.py'
    source.write_text(f'''class {class_name}:
    __slots__ = ('__value', '__dunder__')
    __value: int
    def __init__(self, value: int):
        self.__value = value
    def update(self, value: int):
        self.__value = value
class Child({class_name}):
    def update_child(self, value: int):
        self.__value = value
''')
    _, exports, _ = build_closed_world_context([str(source)], ['owner'])
    base = exports['owner'][class_name]
    child = exports['owner']['Child']
    assert base['field_names'] == (expected, '__dunder__')
    assert child['field_names'] == (expected, '__dunder__', '_Child__value')
    assert expected in dict(base['field_types'])


def test_imported_base_writer_and_reader_use_same_private_field(tmp_path):
    paths, names = _inputs(tmp_path)
    output = tmp_path / 'private_fields.ll'
    _compile(paths, names, output, emit_llvm_only=True)
    text = output.read_text()
    body = re.search(
        r'^define [^\n]*@user_inherited_private_fields_provider_Base_write\([^\n]*\).*?^}',
        text, re.M | re.S,
    ).group(0)
    assert re.search(r'@py_instance_set_field\(ptr %[^,]+, i32 1, ptr ', body)
    assert '@py_obj_setattr(' not in body
    assert '@.class_name.__value.' not in text
    assert '@.class_name._Base__value.' in text
    assert '@.class_name._Child__value.' in text


def test_inherited_private_field_reference_output(tmp_path):
    paths, _ = _inputs(tmp_path)
    result = subprocess.run([sys.executable, paths[0]], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout == EXPECTED


def test_dynamic_inherited_private_field_uses_lexical_runtime_name(tmp_path):
    provider = tmp_path / 'provider.py'
    consumer = tmp_path / 'consumer.py'
    provider.write_text('''class Meta(type):
    pass
class Base(metaclass=Meta):
    def write(self, value):
        self.__value = value
    def read(self):
        return self.__value
''')
    consumer.write_text('''from provider import Base
class Child(Base):
    pass
def main():
    child = Child()
    child.write('stored')
    print(child.read())
main()
''')
    output = tmp_path / 'dynamic.ll'
    _compile([str(consumer), str(provider)], ['consumer', 'provider'], output,
             emit_llvm_only=True)
    text = output.read_text()
    for method, operation in [('write', 'setattr'), ('read', 'getattr')]:
        body = re.search(r'^define [^\n]*@user_provider_Base_' + method
                         + r'\([^\n]*\).*?^}', text, re.M | re.S).group(0)
        assert '@py_obj_' + operation + '(' in body
        assert '_Base__value' in body
        assert '_Child__value' not in body


@pytest.mark.integration
def test_inherited_private_fields_native_five_gc(tmp_path, pcc_runtime_archive):
    from pcc.diagnostics.gc_log import parse_log_lines
    paths, names = _inputs(tmp_path)
    executable = tmp_path / 'private_fields'
    _compile(paths, names, executable, runtime_archive=str(pcc_runtime_archive))
    binary_hash = hashlib.sha256(executable.read_bytes()).hexdigest()
    receipts = []
    for collector in range(5):
        log = tmp_path / ('private-fields-gc' + str(collector) + '.jsonl')
        result = subprocess.run(
            [str(executable)], capture_output=True, text=True, timeout=30,
            env=dict(os.environ, PCC_GC_BACKEND=str(collector),
                     PCC_LOG='gc', PCC_LOG_FORMAT='json', PCC_LOG_FILE=str(log),
                     PCC_HOST_PYTHON='/unavailable/host-python',
                     PATH=str(tmp_path / 'no-host-tools')),
        )
        assert result.returncode == 0, (collector, result.stderr)
        assert result.stdout == EXPECTED, collector
        assert result.stderr == '', (collector, result.stderr)
        assert log.is_file(), 'collector selection requires an observed witness'
        events = parse_log_lines(log.read_text().splitlines())
        observed = sorted({event.fields['value1'] for event in events
                           if event.fields.get('category') == 'gc'
                           and event.event in ('collect_start', 'collect_stop', 'collect_end')})
        receipts.append({'requested_backend': collector, 'observed_backends': observed,
                         'binary_sha256': binary_hash})
        (tmp_path / 'private-fields-native-receipts.json').write_text(
            json.dumps(receipts, indent=2) + '\n',
        )
        assert observed == [collector], receipts[-1]
        assert hashlib.sha256(executable.read_bytes()).hexdigest() == binary_hash
