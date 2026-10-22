"""Native unary file results publish their actual owner before cleanup."""
from __future__ import annotations

import pytest

from tests.python.owned_regression_support import (
    assert_owned_program,
    assert_reference_program,
    explicit_owned_runtime,
)
from tests.python.test_shared_call_binding import _emit, _function
from tests.python.test_slot_call_subscript_producers import _assert_immediate_publication


@pytest.mark.parametrize("method", ("fileno", "tell", "flush"))
@pytest.mark.parametrize("site", ("argument", "list", "later-error", "return", "statement"))
def test_file_unary_producer_publishes_before_cleanup(method, site, tmp_path):
    sites = {
        "argument": "return take(value=stream.PRODUCER())",
        "list": "return take(value=[stream.PRODUCER()])",
        "later-error": "return take(value=stream.PRODUCER(), later=fail())",
        "return": "return stream.PRODUCER()",
        "statement": "stream.PRODUCER()",
    }
    text = _emit(
        "def take(*, value, later=None, **extras):\n    return value\n"
        "def fail():\n    raise ValueError('later')\n"
        "def probe(path):\n    with open(path, 'w+') as stream:\n        "
        + sites[site].replace("PRODUCER", method) + "\n"
    )
    (tmp_path / "program.ll").write_text(text)
    body = _function(text)
    _assert_immediate_publication(body, "py_file_" + method)
    assert "@py_cpy_" not in body
    assert "strict.nolib.stub" not in body


@pytest.mark.parametrize("method", ("fileno", "tell", "flush"))
def test_shadowed_file_receiver_keeps_dynamic_dispatch(method):
    body = _function(_emit(
        "def probe(stream):\n    return stream." + method + "()\n"
    ))
    assert "@py_file_" + method + "(" not in body
    assert "@py_obj_load_method(" in body


@pytest.mark.parametrize("method", ("fileno", "tell", "flush"))
def test_effectful_receiver_is_evaluated_once_with_dynamic_dispatch(method):
    import re
    body = _function(_emit(
        "def receiver():\n    return open('payload.txt', 'w+')\n"
        "def probe():\n    return receiver()." + method + "()\n"
    ))
    assert len(re.findall(r"call [^\n]*@user_binding_receiver\(", body)) == 1
    assert "@py_file_" + method + "(" not in body
    assert "@py_obj_load_method(" in body


PROGRAM = '''import gc
import os
events = []
def take(*, value, later=None, **extras):
    events.append('take')
    gc.collect()
    return value
def later():
    events.append('later')
    gc.collect()
    raise ValueError('later operand')
def main():
    with open(PATH, 'w+') as stream:
        stream.write('x' * 4096)
        assert take(value=stream.tell()) == 4096
        assert take(value=stream.flush()) is None
        descriptor = take(value=stream.fileno())
        assert descriptor >= 0
        os.fsync(stream.fileno())
        assert take(value=[stream.tell(), stream.flush(), stream.fileno()]) == [4096, None, descriptor]
        try:
            take(value=stream.tell(), later=later())
        except ValueError as error:
            assert str(error) == 'later operand'
        else:
            raise AssertionError('missing later error')
        assert events == ['take', 'take', 'take', 'take', 'later']
        stream.close()
        caught = 0
        try:
            take(value=stream.fileno())
        except ValueError:
            caught += 1
        try:
            take(value=stream.tell())
        except ValueError:
            caught += 1
        try:
            take(value=stream.flush())
        except ValueError:
            caught += 1
        assert caught == 3
        assert events == ['take', 'take', 'take', 'take', 'later']
    os.unlink(PATH)
    print('FILE_UNARY_RESULT_OWNERSHIP_OK')
main()
'''


def _program(tmp_path):
    return PROGRAM.replace('PATH', repr(str(tmp_path / 'payload.txt')))


def test_file_unary_native_control_reference(tmp_path):
    assert_reference_program(_program(tmp_path), "FILE_UNARY_RESULT_OWNERSHIP_OK\n", tmp_path)


def test_file_unary_native_control_emits_owned_ir(tmp_path):
    text = _emit(_program(tmp_path))
    (tmp_path / "program.ll").write_text(text)
    for method in ("fileno", "tell", "flush"):
        _assert_immediate_publication(text, "py_file_" + method)
    assert "strict.nolib.stub" not in text


@pytest.mark.integration
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_file_unary_native_five_gc(python_program_compiler, request,
                                   explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params["python_program_compiler"]
    assert_owned_program(_program(tmp_path), "FILE_UNARY_RESULT_OWNERSHIP_OK\n", tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime, capfd)
