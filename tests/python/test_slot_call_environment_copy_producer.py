"""Environment copies are native, independent dict owners at call boundaries."""
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


@pytest.mark.parametrize("producer", ("os.environ.copy()", "dict(os.environ)"))
@pytest.mark.parametrize("site", ("named", "argument", "default", "later-error"))
def test_environment_snapshot_has_an_authoritative_native_owner(producer, site):
    bodies = {
        "named": "    env = PRODUCER\n    env['PCC_COPY'] = 'child'\n    return take(env=env)\n",
        "argument": "    return take(env=PRODUCER)\n",
        "default": "    def target(env=PRODUCER):\n        return env\n    return target()\n",
        "later-error": "    return take(env=PRODUCER, later=fail())\n",
    }
    text = _emit(
        "import os\ndef take(*, env, later=None, **extras):\n    return env\n"
        "def fail():\n    raise ValueError('later')\n"
        "def probe():\n" + bodies[site].replace("PRODUCER", producer)
    )
    _assert_immediate_publication(text, "py_os_environ_snapshot")
    body = _function(text)
    assert "strict.nolib.stub" not in text
    assert "@py_cpy_" not in body
    if site == "named":
        assert "@pcc_gc_root_copy_lease(" in body


def test_environment_copy_keeps_shadowed_receiver_dispatch():
    text = _emit(
        "class Mapping:\n    def copy(self):\n        return {'copy': 'custom'}\n"
        "class Owner:\n    def __init__(self):\n        self.environ = Mapping()\n"
        "def probe(os):\n    return os.environ.copy()\n"
    )
    assert "@py_os_environ_snapshot(" not in _function(text)


PROGRAM = textwrap.dedent('''\
    import gc
    import os
    events = []
    class Marker:
        def __del__(self):
            events.append('released')
    def take(*, env, later=None, **extras):
        gc.collect()
        return env
    def fail():
        gc.collect()
        raise ValueError('later')
    def snapshot_with_marker():
        snapshot = os.environ.copy()
        snapshot['marker'] = Marker()
        return snapshot
    def main():
        os.environ['PCC_COPY_PRODUCER_VALUE'] = 'parent'
        env = os.environ.copy()
        assert type(env) is dict
        assert env['PCC_COPY_PRODUCER_VALUE'] == 'parent'
        env['PCC_COPY_PRODUCER_VALUE'] = 'child'
        env['PCC_COPY_PRODUCER_LOCAL'] = 'only-copy'
        result = take(env=env)
        assert result is env
        assert os.environ['PCC_COPY_PRODUCER_VALUE'] == 'parent'
        assert 'PCC_COPY_PRODUCER_LOCAL' not in os.environ
        os.environ['PCC_COPY_PRODUCER_VALUE'] = 'changed'
        assert result['PCC_COPY_PRODUCER_VALUE'] == 'child'
        copied = take(env=os.environ.copy())
        assert copied['PCC_COPY_PRODUCER_VALUE'] == 'changed'
        copied['PCC_COPY_PRODUCER_VALUE'] = 'independent'
        assert dict(os.environ)['PCC_COPY_PRODUCER_VALUE'] == 'changed'
        try:
            take(env=snapshot_with_marker(), later=fail())
        except ValueError as error:
            assert str(error) == 'later'
        else:
            raise AssertionError('missing later error')
        gc.collect()
        assert events == ['released']
        def frozen(env=os.environ.copy()):
            return env
        os.environ['PCC_COPY_PRODUCER_VALUE'] = 'after-default'
        assert frozen()['PCC_COPY_PRODUCER_VALUE'] == 'changed'
        assert frozen() is frozen()
        del os.environ['PCC_COPY_PRODUCER_VALUE']
        print('ENVIRONMENT_COPY_OWNERSHIP_OK')
    main()
''')


def test_environment_copy_native_program_reference(tmp_path):
    assert_reference_program(PROGRAM, "ENVIRONMENT_COPY_OWNERSHIP_OK\n", tmp_path)


@pytest.mark.integration
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_environment_copy_native_five_gc(python_program_compiler, request,
                                         explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(PROGRAM, "ENVIRONMENT_COPY_OWNERSHIP_OK\n", tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime, capfd)
