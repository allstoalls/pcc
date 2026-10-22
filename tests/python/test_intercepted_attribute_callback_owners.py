"""Custom attribute callback ABIs keep temporary and returned owners alive."""
import subprocess
import sys
import textwrap

import pytest

from tests.python.owned_regression_support import (
    assert_owned_program,
    explicit_owned_runtime,
)
from tests.python.test_shared_call_binding import _emit


PROGRAM = textwrap.dedent('''\
import gc

events = []

class Receiver:
    def __init__(self):
        self.value = 7
    def __getattribute__(self, name):
        events.append(name)
        gc.collect()
        if name == 'alias':
            return self
        if name == 'missing':
            raise AttributeError(name)
        if name == 'error':
            raise ValueError('hook failed')
        return object.__getattribute__(self, name)
    def __getattr__(self, name):
        gc.collect()
        if name == 'missing':
            return self
        raise AttributeError(name)
    def take(self, value):
        gc.collect()
        return value
    def error(self):
        raise AssertionError('attribute hook bypassed')

def argument(value):
    events.append('argument')
    gc.collect()
    return value

def main():
    receiver = Receiver()
    assert receiver.take(argument(receiver)) is receiver
    assert events == ['take', 'argument']
    assert getattr(receiver, 'value') == 7
    assert getattr(receiver, 'alias') is receiver
    assert getattr(receiver, 'missing') is receiver
    try:
        receiver.error()
    except ValueError as error:
        assert str(error) == 'hook failed'
    else:
        raise AssertionError('callback exception lost')
    stored = receiver.take
    receiver = None
    gc.collect()
    assert stored(41) == 41
    print('INTERCEPTED_ATTRIBUTE_OWNERS_OK')

main()
''')


def test_intercepted_attribute_callback_reference(tmp_path):
    program = tmp_path / 'program.py'
    program.write_text(PROGRAM)
    result = subprocess.run(
        [sys.executable, '-B', str(program)], capture_output=True,
        text=True, timeout=30,
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        0, 'INTERCEPTED_ATTRIBUTE_OWNERS_OK\n', '',
    )


def test_intercepted_attribute_callbacks_compile_without_stub():
    assert 'strict.nolib.stub' not in _emit(PROGRAM)


@pytest.mark.integration
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_intercepted_attribute_callback_owners_native_five_gc(
    python_program_compiler, request, explicit_owned_runtime, tmp_path, capfd,
):
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(
        PROGRAM, 'INTERCEPTED_ATTRIBUTE_OWNERS_OK\n', tmp_path,
        python_program_compiler, mode, explicit_owned_runtime, capfd,
        provenance_probe='2',
    )
    assert (tmp_path / 'compiler-wrapper.stderr').read_text() == ''
