"""Live os.environ values retain mapping, module, and receiver authority.

These ordinary-Python programs are shared unchanged by the CPython reference
and both maintained compiler fixtures. Native qualification requires an
explicit source-matched owned runtime and independent observed GC0--4 runs.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from tests.python.owned_regression_support import (
    _record_execution,
    assert_reference_program,
    explicit_owned_runtime,
)
from tests.python.test_native_namespace_projection_behaviors import (
    _error_output,
    _native_format,
    _observed_collectors,
)


MAPPING = '''import gc
import os
import os as system
from os import environ as imported_environ
from os import getenv as imported_getenv

def main():
    key = 'PCC_LIVE_ENVIRON_91A7_' + str(os.getpid()) + '_VALUE'
    missing = key + '_MISSING'
    view_key = key + '_VIEW'
    token = ['default identity']
    events = []
    assert imported_environ is os.environ
    assert system.environ is os.environ
    assert missing not in os.environ
    os.environ[key] = 'controlled-value-91a7'
    try:
        assert os.environ[key] == 'controlled-value-91a7'
        assert system.environ.get(key) == 'controlled-value-91a7'
        assert imported_environ.get(key, token) == 'controlled-value-91a7'
        assert os.getenv(key) == 'controlled-value-91a7'
        assert imported_getenv(key) == 'controlled-value-91a7'
        assert os.environ.get(missing) is None
        assert system.environ.get(missing, token) is token
        assert imported_environ.get(missing, token) is token
        assert os.getenv(missing) is None
        assert imported_getenv(missing, token) is token
        assert key in imported_environ
        snapshot = os.environ.copy()
        assert snapshot is not os.environ
        assert snapshot[key] == 'controlled-value-91a7'
        assert key in system.environ.keys()
        assert (key, 'controlled-value-91a7') in imported_environ.items()
        assert 'controlled-value-91a7' in os.environ.values()
        seen = 0
        for name in imported_environ:
            if name == key:
                seen += 1
        assert seen == 1

        retained_keys = system.environ.keys()
        retained_values = os.environ.values()
        retained_items = imported_environ.items()
        initial_sizes = (len(retained_keys), len(retained_values), len(retained_items))
        added_value = view_key + '_added'
        updated_value = view_key + '_updated'

        def check_retained_views(value, added):
            gc.collect()
            assert (len(retained_keys), len(retained_values), len(retained_items)) == (
                initial_sizes[0] + added,
                initial_sizes[1] + added,
                initial_sizes[2] + added,
            )
            iterated_keys = list(retained_keys)
            iterated_values = list(retained_values)
            iterated_items = list(retained_items)
            if value is None:
                assert view_key not in retained_keys and view_key not in iterated_keys
                for previous in (added_value, updated_value):
                    assert previous not in retained_values and previous not in iterated_values
                    assert (view_key, previous) not in retained_items
                    assert (view_key, previous) not in iterated_items
            else:
                assert view_key in retained_keys and view_key in iterated_keys
                assert value in retained_values and value in iterated_values
                assert (view_key, value) in retained_items
                assert (view_key, value) in iterated_items

        check_retained_views(None, 0)
        os.environ[view_key] = added_value
        check_retained_views(added_value, 1)
        system.environ[view_key] = updated_value
        check_retained_views(updated_value, 1)
        assert added_value not in retained_values and added_value not in list(retained_values)
        assert (view_key, added_value) not in retained_items
        assert (view_key, added_value) not in list(retained_items)
        del imported_environ[view_key]
        check_retained_views(None, 0)

        replacement_value = view_key + '_replacement'
        os.environ = {view_key: replacement_value}
        try:
            check_retained_views(None, 0)
            imported_environ[view_key] = added_value
            check_retained_views(added_value, 1)
            assert os.environ[view_key] == replacement_value
            assert replacement_value not in retained_values
            assert (view_key, replacement_value) not in retained_items
            del imported_environ[view_key]
            check_retained_views(None, 0)
            assert os.environ[view_key] == replacement_value
        finally:
            os.environ = imported_environ
        assert system.environ is imported_environ

        def evaluated_key():
            events.append('key')
            gc.collect()
            return key
        def evaluated_default():
            events.append('default')
            gc.collect()
            return token
        assert os.environ.get(evaluated_key(), evaluated_default()) == 'controlled-value-91a7'
        assert events == ['key', 'default']
        os.environ[key] = ''
        assert os.environ.get(key, token) == ''
        assert os.getenv(key, token) == ''
        assert snapshot[key] == 'controlled-value-91a7'
        try:
            os.environ[key] = 17
        except TypeError:
            pass
        else:
            raise AssertionError('non-string environment value accepted')
        assert os.environ[key] == ''
        try:
            os.environ[17]
        except TypeError:
            pass
        else:
            raise AssertionError('non-string subscript accepted')
        try:
            system.environ.get(17, token)
        except TypeError:
            pass
        else:
            raise AssertionError('non-string get key accepted')
        try:
            17 in imported_environ
        except TypeError:
            pass
        else:
            raise AssertionError('non-string membership key accepted')
        try:
            imported_environ[missing]
        except KeyError as error:
            assert error.args == (missing,)
        else:
            raise AssertionError('missing environment subscript did not raise')
        del system.environ[key]
        assert key not in os.environ
        assert imported_environ.get(key, token) is token
        assert snapshot[key] == 'controlled-value-91a7'
        try:
            del os.environ[key]
        except KeyError as error:
            assert error.args == (key,)
        else:
            raise AssertionError('missing environment deletion did not raise')
    finally:
        if view_key in imported_environ:
            del imported_environ[view_key]
        if key in imported_environ:
            del imported_environ[key]
    gc.collect()
    print('LIVE_OS_ENVIRON_MAPPING_OK')

main()
'''


MODULE_AUTHORITY = '''import gc
import os
import os as system
from os import environ as imported_environ
from os import getenv as imported_getenv

module = os
original = os.environ
key = 'PCC_LIVE_ENVIRON_6F29_' + str(os.getpid()) + '_AUTHORITY'
events = []
first_token = ['first mapping']
second_token = ['second mapping']
default_token = ['unused default']

class Mapping:
    def __init__(self, name, value):
        self.name = name
        self.value = value
    def get(self, name, default=None):
        events.append(self.name)
        gc.collect()
        assert name == key
        assert default is default_token
        return self.value

class Receiver:
    def __init__(self, mapping):
        self.environ = mapping

def read():
    return os.environ.get(key, default_token)

def read_alias():
    return system.environ.get(key, default_token)

def read_getenv():
    return os.getenv(key, default_token)

def read_alias_getenv():
    return system.getenv(key, default_token)

def read_imported_getenv():
    return imported_getenv(key, default_token)

def bind(value):
    global os
    os = value

def erase():
    global os
    del os

def changed_key():
    events.append('key')
    vars(module)['environ'] = second
    gc.collect()
    return key

def changed_default():
    events.append('default')
    vars(module)['environ'] = original
    gc.collect()
    return default_token

def read_with_effects():
    return os.environ.get(changed_key(), changed_default())

first = Mapping('first', first_token)
second = Mapping('second', second_token)
# Deliberately declared after every reader: function source order cannot make
# the original environment authoritative when those functions later execute.
system.environ = first

def main():
    original[key] = 'real-process-value-6f29'
    try:
        assert read() is first_token
        assert read_alias() is first_token
        assert read_getenv() is first_token
        assert read_alias_getenv() is first_token
        assert read_imported_getenv() is first_token
        assert events == ['first', 'first', 'first', 'first', 'first']
        assert imported_environ is original
        assert imported_environ.get(key) == 'real-process-value-6f29'
        events.clear()
        vars(os)['environ'] = second
        assert read() is second_token
        assert read_alias() is second_token
        assert read_getenv() is second_token
        assert read_alias_getenv() is second_token
        assert read_imported_getenv() is second_token
        assert events == ['second', 'second', 'second', 'second', 'second']
        events.clear()
        vars(system)['environ'] = first
        assert read_with_effects() is first_token
        assert events == ['key', 'default', 'first']
        assert module.environ is original
        events.clear()
        del vars(os)['environ']
        for reader in (read, read_alias, read_with_effects):
            try:
                reader()
            except AttributeError:
                pass
            else:
                raise AssertionError('deleted environ consulted process environment')
        for reader in (read_getenv, read_alias_getenv, read_imported_getenv):
            try:
                reader()
            except NameError:
                pass
            else:
                raise AssertionError('getenv ignored its deleted provider global')
        assert events == []
        vars(system)['environ'] = None
        for reader in (read, read_alias, read_with_effects,
                       read_getenv, read_alias_getenv, read_imported_getenv):
            try:
                reader()
            except AttributeError:
                pass
            else:
                raise AssertionError('None environ was confused with a missing provider global')
        assert events == []
        vars(system)['environ'] = original
        assert read() == 'real-process-value-6f29'
        assert read_getenv() == 'real-process-value-6f29'
        assert read_imported_getenv() == 'real-process-value-6f29'
        assert module.environ is imported_environ

        bind(Receiver(first))
        assert read() is first_token
        assert read_alias() == 'real-process-value-6f29'
        namespace = globals()
        namespace['os'] = Receiver(second)
        assert read() is second_token
        namespace['system'] = Receiver(first)
        assert read_alias() is first_token
        erase()
        try:
            read()
        except NameError:
            pass
        else:
            raise AssertionError('deleted module global remained readable')
        namespace['os'] = module
        namespace['system'] = module
        assert read() == 'real-process-value-6f29'
        assert read_alias() == 'real-process-value-6f29'
        assert events == ['first', 'second', 'first']
    finally:
        globals()['os'] = module
        globals()['system'] = module
        vars(module)['environ'] = original
        if key in original:
            del original[key]
    gc.collect()
    print('LIVE_OS_ENVIRON_MODULE_AUTHORITY_OK')

main()
'''


RECEIVER_EFFECTS = '''import gc
import os
import os as system

events = []
first_token = ['first result']
second_token = ['second result']
default_token = ['default']
key_failure = None
default_failure = None

class GetDescriptor:
    def __get__(self, instance, owner):
        events.append('get:' + instance.name)
        gc.collect()
        if instance.lookup_failure is not None:
            raise instance.lookup_failure
        return instance.invoke

class Mapping:
    get = GetDescriptor()
    def __init__(self, name, value):
        self.name = name
        self.value = value
        self.lookup_failure = None
        self.call_failure = None
    def invoke(self, key, default=None):
        events.append('call:' + self.name)
        gc.collect()
        assert key == 'controlled-key'
        assert default is default_token
        if self.call_failure is not None:
            raise self.call_failure
        return self.value

class EnvironDescriptor:
    def __get__(self, instance, owner):
        events.append('environ')
        gc.collect()
        if instance.failure is not None:
            raise instance.failure
        return instance.mapping

class Receiver:
    environ = EnvironDescriptor()
    def __init__(self, mapping):
        self.mapping = mapping
        self.failure = None

first = Mapping('first', first_token)
second = Mapping('second', second_token)
receiver = Receiver(first)

def evaluated_key():
    events.append('key')
    receiver.mapping = second
    gc.collect()
    if key_failure is not None:
        raise key_failure
    return 'controlled-key'

def evaluated_default():
    events.append('default')
    receiver.mapping = second
    gc.collect()
    if default_failure is not None:
        raise default_failure
    return default_token

def parameter(os):
    return os.environ.get(evaluated_key(), evaluated_default())

def alias_parameter(system):
    return system.environ.get(evaluated_key(), evaluated_default())

def assigned(replacement):
    os = replacement
    return os.environ.get(evaluated_key(), evaluated_default())

def unbound(replacement):
    try:
        os.environ.get(evaluated_key(), evaluated_default())
    except UnboundLocalError:
        return True
    os = replacement
    return False

def main():
    global key_failure, default_failure
    for reader in (parameter, alias_parameter, assigned):
        receiver.mapping = first
        events.clear()
        assert reader(receiver) is first_token
        assert events == ['environ', 'get:first', 'key', 'default', 'call:first']
        assert receiver.mapping is second
    receiver.mapping = first
    events.clear()
    captured = receiver.environ.get
    assert events == ['environ', 'get:first']
    receiver.mapping = second
    events.clear()
    assert captured(evaluated_key(), evaluated_default()) is first_token
    assert events == ['key', 'default', 'call:first']
    events.clear()
    assert unbound(receiver)
    assert events == []

    failure = RuntimeError('receiver failure identity')
    receiver.mapping = first
    receiver.failure = failure
    try:
        parameter(receiver)
    except RuntimeError as error:
        assert error is failure
    else:
        raise AssertionError('environ descriptor error disappeared')
    assert events == ['environ']
    receiver.failure = None
    events.clear()
    first.lookup_failure = failure
    try:
        parameter(receiver)
    except RuntimeError as error:
        assert error is failure
    else:
        raise AssertionError('get descriptor error disappeared')
    assert events == ['environ', 'get:first']
    first.lookup_failure = None
    events.clear()
    key_failure = failure
    try:
        parameter(receiver)
    except RuntimeError as error:
        assert error is failure
    else:
        raise AssertionError('key evaluation error disappeared')
    assert events == ['environ', 'get:first', 'key']
    key_failure = None
    receiver.mapping = first
    events.clear()
    default_failure = failure
    try:
        parameter(receiver)
    except RuntimeError as error:
        assert error is failure
    else:
        raise AssertionError('default evaluation error disappeared')
    assert events == ['environ', 'get:first', 'key', 'default']
    default_failure = None
    receiver.mapping = first
    events.clear()
    first.call_failure = failure
    try:
        parameter(receiver)
    except RuntimeError as error:
        assert error is failure
    else:
        raise AssertionError('custom get error disappeared')
    assert events == ['environ', 'get:first', 'key', 'default', 'call:first']
    gc.collect()
    print('LIVE_OS_ENVIRON_RECEIVER_EFFECTS_OK')

main()
'''


SCENARIOS = {
    'mapping': MAPPING,
    'module-authority': MODULE_AUTHORITY,
    'receiver-effects': RECEIVER_EFFECTS,
}


def _expected(scenario):
    return 'LIVE_OS_ENVIRON_' + scenario.upper().replace('-', '_') + '_OK\n'


def test_owned_os_environ_export_authority():
    from pcc.frontends.python import pipeline
    from pcc.frontends.python.pipeline_context import build_closed_world_context

    root = Path(__file__).resolve().parents[2]
    provider = Path(pipeline._locate_native_stdlib_module_source('os')).resolve(strict=True)
    assert provider == root / 'pcc/stdlib/os.py'
    _, exports, _ = build_closed_world_context(
        [str(provider)], ['os'], merge_exports=False,
    )
    binding = exports['os']['environ']
    assert binding['kind'] == 'module_global'
    assert binding['owning_module'] == 'os'
    assert binding['export_name'] == 'environ'


@pytest.mark.parametrize('scenario', SCENARIOS)
def test_live_os_environ_reference(tmp_path, scenario, monkeypatch):
    monkeypatch.setenv('PYTHONDONTWRITEBYTECODE', '1')
    assert_reference_program(SCENARIOS[scenario], _expected(scenario), tmp_path)


@pytest.mark.integration
@pytest.mark.parametrize('scenario', SCENARIOS)
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_live_os_environ_native_five_gc(
    tmp_path, scenario, request, monkeypatch, explicit_owned_runtime,
    python_program_compiler, capfd,
):
    monkeypatch.setenv('PYTHONDONTWRITEBYTECODE', '1')
    source, reference = assert_reference_program(
        SCENARIOS[scenario], _expected(scenario), tmp_path,
    )
    binary = tmp_path / 'live-os-environ.out'
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
        'expected_native_stdout': _expected(scenario),
        'executions': [],
    }
    receipt_path = tmp_path / 'live-os-environ.json'
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
    if not binary.is_file():
        receipt['status'] = 'MISSING_NATIVE_EXECUTABLE'
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
        pytest.fail('Compiler did not emit an executable')
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
                0, _expected(scenario), '',
            ), result
        except Exception as error:
            row['errors'].append(type(error).__name__ + ': ' + str(error))
            # Preserve each collector's original assertions and execute every
            # other collector even after a failure or timeout.
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
