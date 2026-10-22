"""Native system producers publish NEW owners before later calls or cleanup."""
from __future__ import annotations

import textwrap

import pytest

from tests.python.owned_regression_support import (
    assert_owned_program,
    assert_reference_program,
    explicit_owned_runtime,
)
from tests.python.test_shared_call_binding import _emit, _function
from tests.python.test_slot_call_subscript_producers import _assert_immediate_publication


@pytest.mark.parametrize('producer,runtime', (
    ('shlex.split(value)', 'py_shlex_split'),
    ('subprocess.check_output(value)', 'py_subprocess_check_output'),
    ('subprocess.check_output(value, text=True)', 'py_subprocess_check_output'),
))
@pytest.mark.parametrize('site', ('argument', 'conversion', 'default', 'later-error'))
def test_system_result_publication_precedes_all_cleanup(producer, runtime, site):
    bodies = {
        'argument': '    return take(value=PRODUCER)\n',
        'conversion': ('    return list(PRODUCER)\n' if runtime == 'py_shlex_split'
                       else '    return str(PRODUCER).strip()\n'),
        'default': '    def target(item=PRODUCER):\n        return item\n    return target()\n',
        'later-error': '    return take(value=PRODUCER, later=fail())\n',
    }
    text = _emit(
        'import shlex\nimport subprocess\n'
        'def take(*, value, later=None, **extras):\n    return value\n'
        "def fail():\n    raise ValueError('later')\n"
        'def probe(value):\n' + bodies[site].replace('PRODUCER', producer)
    )
    _assert_immediate_publication(text, runtime)
    if 'text=True' in producer:
        _assert_immediate_publication(text, 'py_bytes_decode')
    assert 'strict.nolib.stub' not in text
    assert '@py_cpy_' not in _function(text)


PROGRAM = textwrap.dedent('''\
    import gc
    import shlex
    import subprocess
    events = []
    def take(*, value, later=None, **extras):
        gc.collect()
        return value
    def command():
        events.append('command')
        gc.collect()
        return ['/bin/echo', '  producer-value  ']
    def later():
        events.append('later')
        gc.collect()
        return 7
    def fail():
        gc.collect()
        raise ValueError('later-error')
    def main():
        parsed = take(value=shlex.split('first "two words" third'), later=later())
        assert parsed == ['first', 'two words', 'third']
        assert list(shlex.split("one 'two words'")) == ['one', 'two words']
        assert shlex.split('') == []
        assert list(shlex.split('"" middle ""', posix=True)) == ['', 'middle', '']
        events.clear()
        text = take(value=subprocess.check_output(command(), text=True), later=later())
        assert events == ['command', 'later']
        assert text == '  producer-value  \\n'
        assert type(text) is str
        assert str(subprocess.check_output(command(), text=True)).strip() == 'producer-value'
        raw = take(value=subprocess.check_output(command()))
        assert type(raw) is bytes
        assert raw == b'  producer-value  \\n'
        for i in range(3):
            try:
                take(value=shlex.split('held through error'), later=fail())
            except ValueError as error:
                assert str(error) == 'later-error'
            else:
                raise AssertionError('missing later error')
            gc.collect()
        def frozen(value=shlex.split('default value')):
            return value
        assert frozen() == ['default', 'value']
        assert frozen() is frozen()
        print('SYSTEM_RESULT_OWNERSHIP_OK')
    main()
''')


def test_system_result_native_program_reference(tmp_path):
    assert_reference_program(PROGRAM, 'SYSTEM_RESULT_OWNERSHIP_OK\n', tmp_path)


@pytest.mark.integration
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_system_result_native_five_gc(python_program_compiler, request,
                                     explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(PROGRAM, 'SYSTEM_RESULT_OWNERSHIP_OK\n', tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime, capfd)
