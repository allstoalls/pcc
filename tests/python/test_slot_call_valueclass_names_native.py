"""Execute whole payload Name boxing, GC survival and disposal on a matched runtime."""
from __future__ import annotations

import pytest

from tests.python.owned_regression_support import (
    assert_owned_program,
    assert_reference_program,
    explicit_owned_runtime,
)
from tests.python.test_shared_call_binding import _emit


PROGRAM = '''import gc
import pcc

events = []

class Token:
    def __init__(self, label):
        self.label = label
    def __del__(self):
        events.append(self.label)

@pcc.valueclass
class Pair:
    first: int
    second: int

@pcc.valueclass
class Leaf:
    token: Token
    values: list
    number: int

@pcc.valueclass
class Packet:
    leaf: Leaf
    flag: bool

def receive(*, value, **extras):
    gc.collect()
    return value

def fail_later():
    gc.collect()
    raise ValueError('later argument')

def accept(*, value, other, **extras):
    raise AssertionError('later argument did not raise')

def check_pair(pair: Pair):
    boxed = receive(value=pair)
    gc.collect()
    assert boxed.first == 17
    assert boxed.second == 29
    assert pair.first == 17
    assert pair.second == 29

def check_packet(packet: Packet):
    boxed = receive(value=packet)
    gc.collect()
    assert boxed.leaf.token.label == 'owned'
    assert boxed.leaf.number == 11
    assert boxed.flag is True
    boxed.leaf.values.append(9)
    gc.collect()
    assert packet.leaf.values == [3, 9]
    assert packet.leaf.token.label == 'owned'
    del boxed
    gc.collect()
    assert events == []
    try:
        accept(value=packet, other=fail_later())
    except ValueError as error:
        assert str(error) == 'later argument'
    else:
        raise AssertionError('exception was lost')
    gc.collect()
    assert packet.leaf.token.label == 'owned'
    assert packet.leaf.values == [3, 9]
    assert events == []

def run_once():
    pair = Pair(17, 29)
    check_pair(pair)
    leaf = Leaf(Token('owned'), [3], 11)
    packet = Packet(leaf, True)
    check_packet(packet)

def main():
    run_once()
    gc.collect()
    assert events == ['owned']
    print('VALUECLASS_NAME_OWNERS_OK')

main()
'''


def test_valueclass_name_native_program_reference(tmp_path):
    assert_reference_program(PROGRAM, "VALUECLASS_NAME_OWNERS_OK\n", tmp_path)


def test_valueclass_name_native_program_strict_codegen():
    text = _emit(PROGRAM)
    assert "strict.nolib.stub" not in text
    assert "@py_valuebox_new(" in text
    assert "@py_obj_call_slots(" in text


@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_valueclass_name_native_five_gc(
    python_program_compiler, request, explicit_owned_runtime, tmp_path, capfd,
):
    mode = request.node.callspec.params["python_program_compiler"]
    assert_owned_program(
        PROGRAM, "VALUECLASS_NAME_OWNERS_OK\n", tmp_path,
        python_program_compiler, mode, explicit_owned_runtime, capfd,
    )
