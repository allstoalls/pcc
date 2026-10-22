"""Namespace projections retain value, lookup, and evaluation semantics.

CPython and PCC have different implementation identities.  The same program
must satisfy its mode-specific identity output and shared semantic assertions.
Native cases require an explicitly selected, source-matched owned runtime.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

import pytest

from tests.python.owned_regression_support import (
    _record_execution,
    assert_reference_program,
    explicit_owned_runtime,
)


CONSUMERS = '''import gc
import sys
import sys as system

def positional(value):
    gc.collect()
    return value

def keyword(*, value):
    gc.collect()
    return value

def direct_name():
    return sys.implementation.name

def direct_tag():
    return sys.implementation.cache_tag

def defaults(name=sys.implementation.name, tag=sys.implementation.cache_tag,
             nested=(sys.implementation.name, (sys.implementation.cache_tag,))):
    gc.collect()
    return name, tag, nested

def main():
    name = direct_name()
    tag = direct_tag()
    assert positional(sys.implementation.name) == name
    assert positional(sys.implementation.cache_tag) == tag
    assert keyword(value=sys.implementation.name) == name
    assert keyword(value=sys.implementation.cache_tag) == tag
    pair = keyword(value=(sys.implementation.name, (sys.implementation.cache_tag,)))
    assert pair == (name, (tag,))
    assert defaults() == (name, tag, pair)
    def nested_default(value=(sys.implementation.name, (sys.implementation.cache_tag,))):
        gc.collect()
        return value
    assert nested_default() == pair
    assert positional(system.implementation.name) == name
    assert keyword(value=system.implementation.cache_tag) == tag
    assert keyword(value=(system.implementation.name, (system.implementation.cache_tag,))) == pair
    def alias_default(value=system.implementation.name):
        gc.collect()
        return value
    assert alias_default() == name
    print('implementation=' + name)
    print('cache_tag=' + str(tag))
    print('NAMESPACE_PROJECTION_CONSUMERS_OK')

main()
'''


RECEIVER_EFFECTS = '''import gc

events = []
name_token = ['owned name']
tag_token = ['owned tag']

class NameDescriptor:
    def __get__(self, instance, owner):
        events.append('name')
        gc.collect()
        return name_token

class TagDescriptor:
    def __get__(self, instance, owner):
        events.append('tag')
        gc.collect()
        return tag_token

class Implementation:
    name = NameDescriptor()
    cache_tag = TagDescriptor()

implementation = Implementation()

class ImplementationDescriptor:
    def __get__(self, instance, owner):
        events.append('implementation')
        gc.collect()
        return implementation

class Receiver:
    implementation = ImplementationDescriptor()

receiver = Receiver()

def make_receiver():
    events.append('receiver')
    gc.collect()
    return receiver

def later():
    events.append('later')
    gc.collect()
    return None

def take(value, *, after=None):
    events.append('take')
    gc.collect()
    return value

def take_keyword(*, value, after=None, **extras):
    return take(value, after=after)

def main():
    value = take(make_receiver().implementation.name, after=later())
    assert value is name_token
    assert events == ['receiver', 'implementation', 'name', 'later', 'take']
    events.clear()
    value = take_keyword(value=make_receiver().implementation.cache_tag, after=later())
    assert value is tag_token
    assert events == ['receiver', 'implementation', 'tag', 'later', 'take']
    events.clear()
    def captured(value=(make_receiver().implementation.name,
                        (make_receiver().implementation.cache_tag,))):
        gc.collect()
        return value
    assert events == ['receiver', 'implementation', 'name',
                      'receiver', 'implementation', 'tag']
    events.clear()
    first = captured()
    second = captured()
    assert first is second
    assert first[0] is name_token
    assert first[1][0] is tag_token
    assert events == []
    print('NAMESPACE_PROJECTION_RECEIVER_EFFECTS_OK')

main()
'''


SHADOWING = '''import gc
import sys
import sys as system

class Implementation:
    def __init__(self, name, tag):
        self.name = name
        self.cache_tag = tag

class Receiver:
    def __init__(self, implementation):
        self.implementation = implementation

def take(*, value, **extras):
    gc.collect()
    return value

def parameter(sys):
    return take(value=(sys.implementation.name, sys.implementation.cache_tag))

def alias_parameter(system):
    return take(value=(system.implementation.name, system.implementation.cache_tag))

def assignment(replacement):
    sys = replacement
    return take(value=(sys.implementation.name, sys.implementation.cache_tag))

def unbound(replacement):
    try:
        take(value=sys.implementation.name)
    except UnboundLocalError:
        return True
    sys = replacement
    return False

def main():
    name = ['shadow name']
    tag = ['shadow tag']
    replacement = Receiver(Implementation(name, tag))
    for result in (parameter(replacement), alias_parameter(replacement), assignment(replacement)):
        assert result[0] is name
        assert result[1] is tag
    assert unbound(replacement)
    print('NAMESPACE_PROJECTION_SHADOWING_OK')

main()
'''


REBINDING = '''import gc
import sys

class Implementation:
    def __init__(self, name, tag):
        self.name = name
        self.cache_tag = tag

class Receiver:
    def __init__(self, implementation):
        self.implementation = implementation

def take(*, value, **extras):
    gc.collect()
    return value

def read():
    return take(value=(sys.implementation.name, sys.implementation.cache_tag))

def bind(value):
    global sys
    sys = value

def erase():
    global sys
    del sys

def expect_missing():
    try:
        read()
    except NameError:
        return
    raise AssertionError('deleted module name remained readable')

def main():
    first_name, first_tag = ['first name'], ['first tag']
    second_name, second_tag = ['second name'], ['second tag']
    first = Receiver(Implementation(first_name, first_tag))
    second = Receiver(Implementation(second_name, second_tag))
    bind(first)
    result = read()
    assert result[0] is first_name and result[1] is first_tag
    def frozen(value=read()):
        return value
    namespace = globals()
    namespace['sys'] = second
    result = read()
    assert result[0] is second_name and result[1] is second_tag
    assert namespace['sys'] is second
    assert frozen()[0] is first_name and frozen()[1] is first_tag
    erase()
    expect_missing()
    namespace['sys'] = first
    assert read()[0] is first_name
    del namespace['sys']
    expect_missing()
    bind(second)
    assert read()[1] is second_tag
    print('NAMESPACE_PROJECTION_REBINDING_OK')

main()
'''


MODULE_OVERRIDE = '''import gc
import sys
import sys as system

events = []

class NameDescriptor:
    def __get__(self, instance, owner):
        events.append('name')
        gc.collect()
        return instance.name_value

class TagDescriptor:
    def __get__(self, instance, owner):
        events.append('tag')
        gc.collect()
        return instance.tag_value

class Implementation:
    name = NameDescriptor()
    cache_tag = TagDescriptor()
    def __init__(self, name, tag):
        self.name_value = name
        self.tag_value = tag

def take(*, value, **extras):
    gc.collect()
    return value

def direct():
    return system.implementation.name

def read():
    return take(value=(system.implementation.name, sys.implementation.cache_tag))

def main():
    first_name, first_tag = ['first name'], ['first tag']
    second_name, second_tag = ['second name'], ['second tag']
    first = Implementation(first_name, first_tag)
    second = Implementation(second_name, second_tag)
    system.implementation = first
    result = read()
    assert result[0] is first_name and result[1] is first_tag
    assert events == ['name', 'tag']
    events.clear()
    assert direct() is first_name
    assert events == ['name']
    events.clear()
    sys.implementation = second
    result = read()
    assert result[0] is second_name and result[1] is second_tag
    assert events == ['name', 'tag']
    events.clear()
    del system.implementation
    try:
        read()
    except AttributeError:
        pass
    else:
        raise AssertionError('deleted implementation remained readable')
    assert events == []
    sys.implementation = first
    assert direct() is first_name
    assert events == ['name']
    print('NAMESPACE_PROJECTION_MODULE_OVERRIDE_OK')

main()
'''


SCENARIOS = {
    'consumers': CONSUMERS,
    'receiver-effects': RECEIVER_EFFECTS,
    'shadowing': SHADOWING,
    'rebinding': REBINDING,
    'module-override': MODULE_OVERRIDE,
}


def _expected(scenario, implementation):
    marker = 'NAMESPACE_PROJECTION_' + scenario.upper().replace('-', '_') + '_OK\n'
    if scenario != 'consumers':
        return marker
    if implementation == 'native':
        return 'implementation=pcc\ncache_tag=None\n' + marker
    return ('implementation=' + sys.implementation.name + '\n'
            + 'cache_tag=' + str(sys.implementation.cache_tag) + '\n' + marker)


def _native_format(path):
    with path.open('rb') as stream:
        magic = stream.read(4)
        if magic == b'\x7fELF':
            return 'ELF'
        if magic in (
            b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf',
            b'\xce\xfa\xed\xfe', b'\xfe\xed\xfa\xce',
            b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca',
            b'\xca\xfe\xba\xbf', b'\xbf\xba\xfe\xca',
        ):
            return 'Mach-O'
        if magic[:2] == b'MZ':
            stream.seek(0x3c)
            offset = stream.read(4)
            if len(offset) == 4:
                stream.seek(int.from_bytes(offset, 'little'))
                if stream.read(4) == b'PE\x00\x00':
                    return 'PE'
    return None


def _observed_collectors(path):
    from pcc.diagnostics.gc_log import parse_log_lines

    assert path.is_file(), 'Native execution produced no GC log'
    events = parse_log_lines(path.read_text(encoding='utf-8').splitlines())
    return sorted({
        event.fields['value1']
        for event in events
        if event.fields.get('category') == 'gc'
        and event.event in ('collect_start', 'collect_stop', 'collect_end')
    })


def _error_output(value):
    if value is None:
        return ''
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    return str(value)


@pytest.mark.parametrize('scenario', SCENARIOS)
def test_namespace_projection_reference(tmp_path, scenario, monkeypatch):
    monkeypatch.setenv('PYTHONDONTWRITEBYTECODE', '1')
    assert_reference_program(SCENARIOS[scenario], _expected(scenario, 'reference'), tmp_path)


@pytest.mark.integration
@pytest.mark.parametrize('scenario', SCENARIOS)
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_namespace_projection_native_five_gc(
    tmp_path, scenario, request, monkeypatch, explicit_owned_runtime,
    python_program_compiler, capfd,
):
    monkeypatch.setenv('PYTHONDONTWRITEBYTECODE', '1')
    source, reference = assert_reference_program(
        SCENARIOS[scenario], _expected(scenario, 'reference'), tmp_path,
    )
    binary = tmp_path / 'namespace-projection.out'
    receipt = {
        'status': 'RUNNING',
        'scenario': scenario,
        'compiler_fixture_parameter': request.node.callspec.params['python_program_compiler'],
        'compiler_mode': ('pcc1-routed' if python_program_compiler.__module__ == 'tests.pcc1_route'
                          else request.node.callspec.params['python_program_compiler']),
        'backend': 'self',
        'libpython': 'off',
        'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'runtime_archive': str(explicit_owned_runtime),
        'runtime_sha256': hashlib.sha256(explicit_owned_runtime.read_bytes()).hexdigest(),
        'reference': reference,
        'expected_native_stdout': _expected(scenario, 'native'),
        'executions': [],
    }
    receipt_path = tmp_path / 'namespace-projection.json'
    receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
    try:
        python_program_compiler(
            str(source), str(binary), backend='self', libpython_mode='off',
            ir_scaffold_mode='on', runtime_archive=str(explicit_owned_runtime),
        )
    except Exception as error:
        receipt['status'] = 'COMPILE_FAILED'
        receipt['error'] = type(error).__name__ + ': ' + str(error)
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
        raise
    finally:
        captured = capfd.readouterr()
        (tmp_path / 'compiler-wrapper.stdout').write_text(captured.out)
        (tmp_path / 'compiler-wrapper.stderr').write_text(captured.err)
    assert binary.is_file(), 'Compiler did not emit an executable'
    receipt['binary_sha256'] = hashlib.sha256(binary.read_bytes()).hexdigest()
    receipt['binary_format'] = _native_format(binary)
    if receipt['binary_format'] is None:
        receipt['status'] = 'INVALID_NATIVE_EXECUTABLE'
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
        pytest.fail('Compiler output has no ELF, Mach-O, or PE executable magic')
    receipt['status'] = 'NATIVE_RUNNING'
    receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
    for backend in range(5):
        log_path = tmp_path / ('gc' + str(backend) + '.gc.jsonl')
        environment = dict(
            os.environ, PCC_GC_BACKEND=str(backend), PATH='',
            PCC_LOG='gc', PCC_LOG_FORMAT='json', PCC_LOG_FILE=str(log_path),
            PCC_TEST_NO_NATIVE_PROVISIONING='1', PCC_NO_AUTO_PCC1='1',
            PCC_HOST_PYTHON='/nonexistent/host-python', PCC_HOST_PCC='/nonexistent/host-pcc',
            PCC_GC_REFCOUNT_PROVENANCE_PROBE='2',
        )
        environment.pop('LC_ALL', None)
        row = {
            'requested_gc': backend,
            'observed_gc': [],
            'gc_log': str(log_path),
            'status': 'RUNNING',
            'errors': [],
        }
        try:
            result = _record_execution(tmp_path, 'gc' + str(backend), [str(binary)], environment)
            row.update(result)
            assert (result['returncode'], result['stdout'], result['stderr']) == (
                0, _expected(scenario, 'native'), '',
            ), result
        except Exception as error:
            row['errors'].append(type(error).__name__ + ': ' + str(error))
            # A failed or timed-out process must leave the other collectors
            # available to produce their own execution evidence.
            if 'returncode' not in row:
                row.update(
                    command=[str(binary)], returncode=None,
                    stdout=_error_output(getattr(error, 'stdout', None)),
                    stderr=_error_output(getattr(error, 'stderr', None)),
                )
                (tmp_path / ('gc' + str(backend) + '.stdout')).write_text(row['stdout'])
                (tmp_path / ('gc' + str(backend) + '.stderr')).write_text(row['stderr'])
        try:
            row['observed_gc'] = _observed_collectors(log_path)
            row['gc_log_sha256'] = hashlib.sha256(log_path.read_bytes()).hexdigest()
            assert row['observed_gc'] == [backend], row['observed_gc']
        except Exception as error:
            row['errors'].append(type(error).__name__ + ': ' + str(error))
        row['status'] = 'FAIL' if row['errors'] else 'PASS'
        (tmp_path / ('gc' + str(backend) + '.json')).write_text(json.dumps(row, indent=2) + '\n')
        receipt['executions'].append(row)
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
    failures = [row for row in receipt['executions'] if row['status'] != 'PASS']
    receipt['status'] = 'NATIVE_EXECUTION_FAILED' if failures else 'PASS'
    receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
    assert not failures, {'receipt': str(receipt_path), 'failures': failures}
