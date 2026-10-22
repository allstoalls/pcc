"""Registered aggregate returns retain their semantic type at dynamic consumers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys

import pytest

from tests.python.owned_regression_support import (
    _record_execution,
    explicit_owned_runtime,
)


PROVIDER = '''import pcc

events = []

class Token:
    def __init__(self, label):
        self.label = label
    def __del__(self):
        events.append(self.label)

@pcc.valueclass
class Alpha:
    token: Token
    values: list

@pcc.valueclass
class Beta:
    token: Token
    values: list

def make_alpha(label: str) -> Alpha:
    return Alpha(Token(label), [3])

def make_beta(label: str) -> Beta:
    return Beta(Token(label), [7])
'''


CONSUMER = '''import gc
from provider import Alpha as AliasAlpha, Beta, events, make_alpha, make_beta

class AlphaHolder:
    def read(self, label: str) -> AliasAlpha:
        return make_alpha(label)

class BetaHolder:
    def read(self, label: str) -> Beta:
        return make_beta(label)

def alpha(label, fail=False):
    holder = AlphaHolder()
    value = holder.read(label)
    alias = value
    del value
    gc.collect()
    assert alias.token.label == label
    assert alias.values == [3]
    assert isinstance(alias, AliasAlpha)
    assert not isinstance(alias, Beta)
    if fail:
        raise ValueError(label)
    return alias

def beta(label):
    holder = BetaHolder()
    value = holder.read(label)
    alias = value
    gc.collect()
    assert alias.token.label == label
    assert alias.values == [7]
    assert isinstance(alias, Beta)
    assert not isinstance(alias, AliasAlpha)
    return alias

def main():
    first = alpha('alpha')
    second = beta('beta')
    assert events == []
    copies = [first, second]
    del first
    gc.collect()
    assert copies[0].token.label == 'alpha'
    copies[0].values.append(11)
    assert copies[0].values == [3, 11]
    assert copies[1] is second
    assert type(copies[0]) is not type(copies[1])
    try:
        alpha('failed', True)
    except ValueError as error:
        assert str(error) == 'failed'
    else:
        raise AssertionError('dynamic return cleanup lost the exception')
    gc.collect()
    assert events == ['failed']
    del copies[0]
    gc.collect()
    assert events == ['failed', 'alpha']
    copies.clear()
    gc.collect()
    assert events == ['failed', 'alpha']
    assert second.token.label == 'beta'
    del second
    gc.collect()
    assert events == ['failed', 'alpha', 'beta']
    print('REGISTERED_DYNAMIC_PAYLOAD_OK')

main()
'''

EXPECTED = 'REGISTERED_DYNAMIC_PAYLOAD_OK\n'


def _write_sources(directory):
    provider = directory / 'provider.py'
    consumer = directory / 'consumer.py'
    provider.write_text(PROVIDER)
    consumer.write_text(CONSUMER)
    return [str(consumer), str(provider)]


def _compile(paths, output, **options):
    from pcc.frontends.python.pipeline import compile_python_multi
    from pcc.frontends.python.pipeline_targets import host_target_triple

    compile_python_multi(
        paths, str(output), entry_module='consumer',
        module_names=['consumer', 'provider'], backend='self',
        libpython_mode='off', ir_scaffold_mode='on', recursive_stdlib=False,
        target_triple=host_target_triple(), **options,
    )
    return ('pcc1-routed' if compile_python_multi.__module__ == 'tests.pcc1_route'
            else 'host-pcc0')


def test_dynamic_payload_reference_retention_types_and_disposal(tmp_path):
    paths = _write_sources(tmp_path)
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    environment.pop('LC_ALL', None)
    result = _record_execution(tmp_path, 'reference', [sys.executable, '-B', paths[0]], environment)
    assert (result['returncode'], result['stdout'], result['stderr']) == (0, EXPECTED, '')


def test_distinct_semantic_payload_types_with_equal_layout_lower_strictly(tmp_path):
    paths = _write_sources(tmp_path)
    output = tmp_path / 'program.ll'
    _compile(paths, output, emit_llvm_only=True)
    text = output.read_text()
    assert 'strict.nolib.stub' not in text
    assert not re.search(r'\bcall\b[^\n]*@py_cpy_', text)
    results = []
    for kind in ('alpha', 'beta'):
        match = re.search(r'^define external (.+) @user_provider_make_' + kind + r'\(', text, re.M)
        assert match, kind
        results.append(match.group(1))
    assert results[0] == results[1]
    assert results[0].startswith('{ '), results
    # The runtime program requires distinct class identities despite equal
    # ABI layouts, and the Alpha import alias must retain provider identity.
    assert '.class.provider.Alpha' in text
    assert '.class.provider.Beta' in text


@pytest.mark.integration
def test_dynamic_payload_native_retention_types_and_disposal_five_gc(
    tmp_path, explicit_owned_runtime, capfd,
):
    from pcc.diagnostics.gc_log import parse_log_lines
    from pcc.tools.runtime_archive_provenance import codegen_checksum

    paths = _write_sources(tmp_path)
    binary = tmp_path / 'dynamic-payload.out'
    receipt = {
        'status': 'RUNNING', 'compiler': codegen_checksum(),
        'compiler_mode': 'pending', 'backend': 'self', 'libpython': 'off',
        'runtime': str(explicit_owned_runtime),
        'runtime_sha256': hashlib.sha256(explicit_owned_runtime.read_bytes()).hexdigest(),
        'sources': {name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
                    for name in ('provider.py', 'consumer.py')},
        'executions': [],
    }
    output = tmp_path / 'dynamic-payload.json'
    output.write_text(json.dumps(receipt, indent=2) + '\n')
    try:
        receipt['compiler_mode'] = _compile(
            paths, binary, runtime_archive=str(explicit_owned_runtime),
        )
    except Exception as error:
        receipt.update(status='COMPILE_FAILED', error=type(error).__name__ + ': ' + str(error))
        output.write_text(json.dumps(receipt, indent=2) + '\n')
        raise
    finally:
        captured = capfd.readouterr()
        (tmp_path / 'compiler.stdout').write_text(captured.out)
        (tmp_path / 'compiler.stderr').write_text(captured.err)
    with binary.open('rb') as stream:
        magic = stream.read(4)
    assert magic in (b'\x7fELF', b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf') or magic[:2] == b'MZ'
    receipt['binary_sha256'] = hashlib.sha256(binary.read_bytes()).hexdigest()
    for collector in range(5):
        log = tmp_path / ('gc' + str(collector) + '.gc.jsonl')
        environment = dict(os.environ, PATH='', PCC_GC_BACKEND=str(collector),
                           PCC_LOG='gc', PCC_LOG_FORMAT='json', PCC_LOG_FILE=str(log))
        environment.pop('LC_ALL', None)
        row = {'requested_gc': collector, 'observed_gc': [], 'status': 'FAIL'}
        try:
            row.update(_record_execution(tmp_path, 'gc' + str(collector), [str(binary)], environment))
            events = parse_log_lines(log.read_text().splitlines())
            row['observed_gc'] = sorted({event.fields['value1'] for event in events
                if event.fields.get('category') == 'gc'
                and event.event in ('collect_start', 'collect_stop', 'collect_end')})
            assert row['observed_gc'] == [collector]
            assert (row['returncode'], row['stdout'], row['stderr']) == (0, EXPECTED, '')
            row['status'] = 'PASS'
        except Exception as error:
            row['error'] = type(error).__name__ + ': ' + str(error)
            if 'returncode' not in row:
                row['returncode'] = None
                for label in ('stdout', 'stderr'):
                    value = getattr(error, label, '') or ''
                    if isinstance(value, bytes):
                        value = value.decode('utf-8', errors='replace')
                    row[label] = value
                    (tmp_path / ('gc' + str(collector) + '.' + label)).write_text(value)
        receipt['executions'].append(row)
        (tmp_path / ('gc' + str(collector) + '.json')).write_text(json.dumps(row, indent=2) + '\n')
        output.write_text(json.dumps(receipt, indent=2) + '\n')
    receipt['status'] = 'PASS' if all(row['status'] == 'PASS' for row in receipt['executions']) else 'NATIVE_EXECUTION_FAILED'
    output.write_text(json.dumps(receipt, indent=2) + '\n')
    assert receipt['status'] == 'PASS', receipt
