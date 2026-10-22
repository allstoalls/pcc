"""Behavioral aggregate ownership contracts on an explicitly matched runtime.

Each case compiles separately; each emitted binary runs in a fresh process for
every collector. A failed collector still leaves all five execution receipts.
These programs assert Python-visible lifetime and aliasing, not compiler IR.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sys
import textwrap
import time

import pytest

from pcc.diagnostics.gc_log import parse_log_lines
from pcc.tools.runtime_archive_provenance import codegen_checksum
from tests.python.gc_production_contract.test_valuebox_roots import (
    _EXPECTED as VALUEBOX_EXPECTED,
    _PROGRAM as VALUEBOX_PROGRAM,
)
from tests.python.gc_production_contract.test_valueclass_direct_payload_roots import (
    _PROGRAM as DIRECT_PAYLOAD_PROGRAM,
)
from tests.python.owned_regression_support import (
    _record_execution,
    explicit_owned_runtime,
)
from tests.python.test_slot_call_valueclass_names_native import (
    PROGRAM as ORIGINAL_NAME_PROGRAM,
)


# The existing declarations and check_pair/check_packet bodies are preserved.
# This is source reuse only; no test asserts an emitted compiler shape.
_CONTEXT = ORIGINAL_NAME_PROGRAM.split("\ndef run_once():", 1)[0]


@dataclass(frozen=True)
class Case:
    name: str
    program: str
    expected_stdout: str
    required_stderr: str = ""


def _scenario(name, body, final_events):
    expected = sorted(final_events)
    program = _CONTEXT + "\n\n" + textwrap.dedent(body).strip() + "\n\n"
    program += (
        "def main():\n"
        "    run_case()\n"
        "    gc.collect()\n"
        f"    assert sorted(events) == {expected!r}, events\n"
        f"    print('AGGREGATE_OWNERS_OK:{name}')\n\n"
        "main()\n"
    )
    return Case(name, program, f"AGGREGATE_OWNERS_OK:{name}\n")


SCENARIOS = (
    # Bodies of the six small original diagnostics remain unchanged. The full
    # original sequence is the separate original-name control below.
    _scenario("token-control", """
        def run_token():
            token = Token('owned')

        def run_case():
            run_token()
    """, ["owned"]),
    _scenario("leaf-scope", """
        def run_leaf():
            leaf = Leaf(Token('owned'), [3], 11)

        def run_case():
            run_leaf()
    """, ["owned"]),
    _scenario("packet-scope", """
        def run_packet():
            leaf = Leaf(Token('owned'), [3], 11)
            packet = Packet(leaf, True)

        def run_case():
            run_packet()
    """, ["owned"]),
    _scenario("name-box-disposal", """
        def run_box():
            leaf = Leaf(Token('owned'), [3], 11)
            packet = Packet(leaf, True)
            boxed = receive(value=packet)
            del boxed

        def run_case():
            run_box()
    """, ["owned"]),
    _scenario("nested-read-disposal", """
        def run_read():
            leaf = Leaf(Token('owned'), [3], 11)
            packet = Packet(leaf, True)
            assert packet.leaf.token.label == 'owned'

        def run_case():
            run_read()
    """, ["owned"]),
    _scenario("name-later-error-disposal", """
        def run_error():
            leaf = Leaf(Token('owned'), [3], 11)
            packet = Packet(leaf, True)
            try:
                accept(value=packet, other=fail_later())
            except ValueError:
                pass

        def run_case():
            run_error()
    """, ["owned"]),
    _scenario("leaf-delete", """
        def run_case():
            leaf = Leaf(Token('owned'), [3], 11)
            gc.collect()
            assert events == []
            del leaf
            gc.collect()
            assert events == ['owned']
    """, ["owned"]),
    _scenario("leaf-rebind", """
        def run_case():
            leaf = Leaf(Token('first'), [3], 11)
            gc.collect()
            assert events == []
            leaf = Leaf(Token('second'), [4], 12)
            gc.collect()
            assert events == ['first']
            assert leaf.token.label == 'second'
            assert leaf.values == [4]
            del leaf
            gc.collect()
            assert events == ['first', 'second']
    """, ["first", "second"]),
    _scenario("dynamic-rebind-nested-payload", """
        calls = []

        class Producer:
            def metadata(self, label):
                calls.append(label)
                gc.collect()
                if label == 'fail':
                    failed = Token('failed-producer')
                    raise ValueError('producer failed')
                return Packet(Leaf(Token(label), [Token(label + '-list')], 11), True)

        def replace_payload(producer):
            packet: Packet = producer.metadata('first')
            gc.collect()
            assert events == []
            for label in ['second', 'third']:
                packet = producer.metadata(label)
                gc.collect()
                assert packet.leaf.token.label == label
                assert packet.leaf.values[0].label == label + '-list'
                assert packet.leaf.number == 11
                assert packet.flag is True
            assert sorted(events) == ['first', 'first-list', 'second', 'second-list']
            try:
                packet = producer.metadata('fail')
            except ValueError as error:
                assert str(error) == 'producer failed'
            else:
                raise AssertionError('producer exception was lost')
            gc.collect()
            assert packet.leaf.token.label == 'third'
            assert packet.leaf.values[0].label == 'third-list'
            assert sorted(events) == [
                'failed-producer', 'first', 'first-list', 'second', 'second-list',
            ]
            del packet

        def run_case():
            replace_payload(Producer())
            assert calls == ['first', 'second', 'third', 'fail']
    """, [
        "failed-producer", "first", "first-list", "second", "second-list",
        "third", "third-list",
    ]),
    _scenario("mixed-nested-copies-delete", """
        def run_case():
            token = Token('borrowed-token')
            values = [Token('shared-list')]
            leaf = Leaf(token, values, 11)
            packet = Packet(leaf, True)
            alias = packet
            mixed = Packet(Leaf(Token('fresh-token'), values, 12), False)
            del token
            del values
            del leaf
            del packet
            gc.collect()
            assert events == []
            assert alias.leaf.token.label == 'borrowed-token'
            assert mixed.leaf.token.label == 'fresh-token'
            alias.leaf.values.append(9)
            gc.collect()
            assert mixed.leaf.values[0].label == 'shared-list'
            assert mixed.leaf.values[1] == 9
            del alias
            gc.collect()
            assert events == ['borrowed-token']
            assert mixed.leaf.values[0].label == 'shared-list'
            del mixed
            gc.collect()
            assert sorted(events) == ['borrowed-token', 'fresh-token', 'shared-list']
    """, ["borrowed-token", "fresh-token", "shared-list"]),
    _scenario("mixed-nested-copies-rebind", """
        def run_case():
            token = Token('borrowed-token')
            values = [Token('shared-list')]
            leaf = Leaf(token, values, 11)
            packet = Packet(leaf, True)
            alias = packet
            leaf = Leaf(Token('replacement'), [], 12)
            packet = Packet(leaf, False)
            del token
            del values
            gc.collect()
            assert events == []
            assert alias.leaf.token.label == 'borrowed-token'
            assert alias.leaf.values[0].label == 'shared-list'
            assert packet.leaf.token.label == 'replacement'
            del alias
            gc.collect()
            assert sorted(events) == ['borrowed-token', 'shared-list']
            del leaf
            gc.collect()
            assert sorted(events) == ['borrowed-token', 'shared-list']
            assert packet.leaf.token.label == 'replacement'
            del packet
    """, ["borrowed-token", "replacement", "shared-list"]),
    _scenario("constructor-pair-keyword", """
        def run_case():
            boxed = receive(value=Pair(17, 29))
            gc.collect()
            assert boxed.first == 17
            assert boxed.second == 29
            del boxed
    """, []),
    _scenario("constructor-packet-keyword", """
        def run_case():
            boxed = receive(value=Packet(Leaf(Token('owned'), [3], 11), True))
            gc.collect()
            assert events == []
            assert boxed.leaf.token.label == 'owned'
            assert boxed.leaf.number == 11
            assert boxed.flag is True
            boxed.leaf.values.append(9)
            gc.collect()
            assert boxed.leaf.values == [3, 9]
            del boxed
    """, ["owned"]),
    _scenario("constructor-packet-star", """
        def positional(value):
            gc.collect()
            return value

        def run_case():
            boxed = positional(*(Packet(Leaf(Token('owned'), [3], 11), True),))
            gc.collect()
            assert events == []
            assert boxed.leaf.token.label == 'owned'
            assert boxed.leaf.values == [3]
            del boxed
    """, ["owned"]),
    _scenario("constructor-packet-kwstar", """
        def run_case():
            boxed = receive(**{'value': Packet(Leaf(Token('owned'), [3], 11), True)})
            gc.collect()
            assert events == []
            assert boxed.leaf.token.label == 'owned'
            assert boxed.leaf.values == [3]
            del boxed
    """, ["owned"]),
    _scenario("constructor-list-binding", """
        def run_case():
            items = receive(value=[Packet(Leaf(Token('owned'), [3], 11), True)])
            gc.collect()
            assert events == []
            assert items[0].leaf.token.label == 'owned'
            items[0].leaf.values.append(9)
            gc.collect()
            assert items[0].leaf.values == [3, 9]
            del items
    """, ["owned"]),
    _scenario("constructor-tuple-binding", """
        def run_case():
            items = receive(value=(Packet(Leaf(Token('owned'), [3], 11), True),))
            gc.collect()
            assert events == []
            assert items[0].leaf.token.label == 'owned'
            items[0].leaf.values.append(9)
            gc.collect()
            assert items[0].leaf.values == [3, 9]
            del items
    """, ["owned"]),
    _scenario("constructor-dict-binding", """
        def run_case():
            items = receive(value={'item': Packet(Leaf(Token('owned'), [3], 11), True)})
            gc.collect()
            assert events == []
            assert items['item'].leaf.token.label == 'owned'
            items['item'].leaf.values.append(9)
            gc.collect()
            assert items['item'].leaf.values == [3, 9]
            del items
    """, ["owned"]),
    _scenario("constructor-later-argument-error", """
        def run_case():
            try:
                accept(
                    value=Packet(Leaf(Token('owned'), [3], 11), True),
                    other=fail_later(),
                )
            except ValueError as error:
                assert str(error) == 'later argument'
            else:
                raise AssertionError('exception was lost')
            gc.collect()
            assert events == ['owned']
    """, ["owned"]),
    _scenario("later-field-collection", """
        def later_values():
            gc.collect()
            assert events == []
            return [Token('list-leaf')]

        def later_number():
            gc.collect()
            assert events == []
            return 11

        def later_flag():
            gc.collect()
            assert events == []
            return True

        def run_case():
            packet = Packet(
                Leaf(Token('token-leaf'), later_values(), later_number()),
                later_flag(),
            )
            gc.collect()
            assert events == []
            assert packet.leaf.token.label == 'token-leaf'
            assert packet.leaf.values[0].label == 'list-leaf'
            assert packet.leaf.number == 11
            assert packet.flag is True
            del packet
    """, ["list-leaf", "token-leaf"]),
    _scenario("partial-pointer-field-error", """
        def run_case():
            try:
                leaf = Leaf(Token('owned'), fail_later(), 11)
            except ValueError as error:
                assert str(error) == 'later argument'
            else:
                raise AssertionError('exception was lost')
            gc.collect()
            assert events == ['owned']
    """, ["owned"]),
    _scenario("partial-scalar-field-error", """
        def run_case():
            try:
                leaf = Leaf(Token('token-leaf'), [Token('list-leaf')], fail_later())
            except ValueError as error:
                assert str(error) == 'later argument'
            else:
                raise AssertionError('exception was lost')
            gc.collect()
            assert sorted(events) == ['list-leaf', 'token-leaf']
    """, ["list-leaf", "token-leaf"]),
    _scenario("partial-borrowed-field-error", """
        def run_case():
            token = Token('borrowed-token')
            values = [Token('borrowed-list')]
            try:
                leaf = Leaf(token, values, fail_later())
            except ValueError as error:
                assert str(error) == 'later argument'
            else:
                raise AssertionError('exception was lost')
            gc.collect()
            assert events == []
            assert token.label == 'borrowed-token'
            assert values[0].label == 'borrowed-list'
            del token
            gc.collect()
            assert events == ['borrowed-token']
            del values
            gc.collect()
            assert sorted(events) == ['borrowed-list', 'borrowed-token']
    """, ["borrowed-list", "borrowed-token"]),
    _scenario("partial-nested-field-error", """
        def run_case():
            try:
                packet = Packet(
                    Leaf(Token('token-leaf'), [Token('list-leaf')], 11),
                    fail_later(),
                )
            except ValueError as error:
                assert str(error) == 'later argument'
            else:
                raise AssertionError('exception was lost')
            gc.collect()
            assert sorted(events) == ['list-leaf', 'token-leaf']
    """, ["list-leaf", "token-leaf"]),
    _scenario("typed-constructor-return", """
        def make(token: Token) -> Leaf:
            gc.collect()
            return Leaf(token, [Token('fresh-list')], 11)

        def run_case():
            token = Token('borrowed-token')
            leaf = make(token)
            del token
            gc.collect()
            assert events == []
            assert leaf.token.label == 'borrowed-token'
            assert leaf.values[0].label == 'fresh-list'
            del leaf
    """, ["borrowed-token", "fresh-list"]),
    _scenario("typed-return-finally", """
        def make() -> Leaf:
            leaf = Leaf(Token('owned'), [3], 11)
            try:
                return leaf
            finally:
                gc.collect()
                assert events == []
                assert leaf.token.label == 'owned'
                leaf.values.append(9)

        def run_case():
            leaf = make()
            gc.collect()
            assert events == []
            assert leaf.token.label == 'owned'
            assert leaf.values == [3, 9]
            del leaf
    """, ["owned"]),
    _scenario("object-constructor-return", """
        from typing import Any

        def make(token: Token) -> Any:
            gc.collect()
            return Leaf(token, [Token('fresh-list')], 11)

        def run_case():
            token = Token('borrowed-token')
            leaf = make(token)
            del token
            gc.collect()
            assert events == []
            assert leaf.token.label == 'borrowed-token'
            assert leaf.values[0].label == 'fresh-list'
            del leaf
    """, ["borrowed-token", "fresh-list"]),
    _scenario("first-class-typed-adapter", """
        def echo(value: Packet) -> Packet:
            try:
                return value
            finally:
                gc.collect()
                assert events == []
                assert value.leaf.token.label == 'owned'
                value.leaf.values.append(9)

        def invoke(callback, value):
            gc.collect()
            return callback(value)

        def run_case():
            token = Token('owned')
            leaf = Leaf(token, [3], 11)
            packet = Packet(leaf, True)
            result = invoke(echo, packet)
            del token
            del leaf
            del packet
            gc.collect()
            assert events == []
            assert result.leaf.token.label == 'owned'
            assert result.leaf.values == [3, 9]
            del result
    """, ["owned"]),
    _scenario("typed-return-later-error", """
        def make() -> Leaf:
            leaf = Leaf(Token('owned'), [3], 11)
            try:
                return leaf
            finally:
                gc.collect()
                assert events == []

        def run_case():
            try:
                accept(value=make(), other=fail_later())
            except ValueError as error:
                assert str(error) == 'later argument'
            else:
                raise AssertionError('exception was lost')
            gc.collect()
            assert events == ['owned']
    """, ["owned"]),
    _scenario("name-default", """
        def build_default():
            leaf = Leaf(Token('default'), [3], 11)
            def saved(value=leaf):
                gc.collect()
                return value
            leaf = Leaf(Token('rebound'), [4], 12)
            del leaf
            gc.collect()
            assert events == ['rebound']
            return saved

        def run_case():
            saved = build_default()
            first = saved()
            second = saved()
            first.values.append(9)
            gc.collect()
            assert first.token.label == 'default'
            assert second.values == [3, 9]
            assert events == ['rebound']
            del first
            del second
            gc.collect()
            assert events == ['rebound']
            del saved
            gc.collect()
            assert events == ['rebound', 'default']
    """, ["default", "rebound"]),
    _scenario("attribute-default", """
        def build_default():
            packet = Packet(Leaf(Token('default'), [3], 11), True)
            def saved(value=packet.leaf):
                gc.collect()
                return value
            del packet
            gc.collect()
            assert events == []
            return saved

        def run_case():
            saved = build_default()
            first = saved()
            second = saved()
            first.values.append(9)
            gc.collect()
            assert first.token.label == 'default'
            assert second.values == [3, 9]
            assert events == []
            del first
            del second
            gc.collect()
            assert events == []
            del saved
            gc.collect()
            assert events == ['default']
    """, ["default"]),
    _scenario("constructor-default", """
        def build_default():
            def saved(value=Leaf(Token('default'), [3], 11)):
                gc.collect()
                return value
            return saved

        def run_case():
            saved = build_default()
            first = saved()
            second = saved()
            first.values.append(9)
            gc.collect()
            assert first.token.label == 'default'
            assert second.values == [3, 9]
            assert events == []
            del first
            del second
            gc.collect()
            assert events == []
            del saved
            gc.collect()
            assert events == ['default']
    """, ["default"]),
    _scenario("typed-list-unbox", """
        def run_case():
            items: list[Packet] = [Packet(Leaf(Token('owned'), [3], 11), True)]
            selected: Packet = items[0]
            del items
            gc.collect()
            assert events == []
            assert selected.leaf.token.label == 'owned'
            selected.leaf.values.append(9)
            gc.collect()
            assert selected.leaf.values == [3, 9]
            del selected
            gc.collect()
            assert events == ['owned']
    """, ["owned"]),
    _scenario("typed-loop-unbox", """
        def run_case():
            items: list[Packet] = [
                Packet(Leaf(Token('first'), [3], 11), True),
                Packet(Leaf(Token('second'), [4], 12), False),
            ]
            labels = []
            for current in items:
                gc.collect()
                assert events == []
                labels.append(current.leaf.token.label)
            assert labels == ['first', 'second']
            del items
            gc.collect()
            assert events == ['first']
            assert current.leaf.token.label == 'second'
            assert current.leaf.values == [4]
            del current
            gc.collect()
            assert events == ['first', 'second']
    """, ["first", "second"]),
    _scenario("typed-comprehension-unbox", """
        def keep():
            gc.collect()
            assert events == []
            return True

        def run_case():
            items: list[Packet] = [
                Packet(Leaf(Token('first'), [3], 11), True),
                Packet(Leaf(Token('second'), [4], 12), False),
            ]
            selected: list[Leaf] = [current.leaf for current in items if keep()]
            del items
            gc.collect()
            assert events == []
            assert selected[0].token.label == 'first'
            assert selected[1].token.label == 'second'
            del selected[0]
            gc.collect()
            assert events == ['first']
            del selected
            gc.collect()
            assert events == ['first', 'second']
    """, ["first", "second"]),
    _scenario("global-source-first-self-rebind", """
        global_packet = Packet(Leaf(Token('seed'), [0], 0), False)

        def run_case():
            global global_packet
            local = Packet(Leaf(Token('owned'), [3], 11), True)
            global_packet = local
            gc.collect()
            assert events == ['seed']
            global_packet = global_packet
            del local
            gc.collect()
            assert events == ['seed']
            assert global_packet.leaf.token.label == 'owned'
            assert global_packet.leaf.values == [3]
            del global_packet
            gc.collect()
            assert events == ['seed', 'owned']
    """, ["owned", "seed"]),
    _scenario("global-destination-first-alias", """
        global_packet = Packet(Leaf(Token('seed'), [0], 0), False)

        def run_case():
            global global_packet
            local = Packet(Leaf(Token('owned'), [3], 11), True)
            global_packet = local
            alias = global_packet
            gc.collect()
            assert events == ['seed']
            global_packet = Packet(Leaf(Token('replacement'), [4], 12), False)
            gc.collect()
            assert events == ['seed']
            assert local.leaf.token.label == 'owned'
            del local
            gc.collect()
            assert events == ['seed']
            assert alias.leaf.values == [3]
            del global_packet
            gc.collect()
            assert events == ['seed', 'replacement']
            assert alias.leaf.token.label == 'owned'
            del alias
            gc.collect()
            assert events == ['seed', 'replacement', 'owned']
    """, ["owned", "replacement", "seed"]),
    _scenario("global-to-global-alias", """
        global_packet = Packet(Leaf(Token('left'), [3], 11), True)
        global_other = Packet(Leaf(Token('right'), [4], 12), False)

        def run_case():
            global global_packet, global_other
            global_packet = global_other
            gc.collect()
            assert events == ['left']
            global_packet = global_packet
            del global_other
            gc.collect()
            assert events == ['left']
            assert global_packet.leaf.token.label == 'right'
            assert global_packet.leaf.values == [4]
            del global_packet
            gc.collect()
            assert events == ['left', 'right']
    """, ["left", "right"]),
)


# DIRECT_PAYLOAD_EXPECTED is inserted below from the original, complete
# test_direct_valueclass_pointer_payload_survives_gc stdout oracle. Its source
# identity and exact extraction provenance are in witness-source-map.json.
DIRECT_PAYLOAD_EXPECTED = (
    '4\n'
    '4\n'
    'bag\n'
    '4\n'
    'bag\n'
    '5\n'
    '5\n'
    'bag\n'
    '5\n'
    'bag\n'
    '2\n'
    '7\n'
    'nested\n'
    '2\n'
    '8\n'
    'holder\n'
    '2\n'
    'nested\n'
    '2\n'
    'holder\n'
    '3\n'
    '8\n'
    'nested\n'
    '3\n'
    '9\n'
    'holder\n'
    '3\n'
    '3\n'
    'holder\n'
    '2\n'
    '7\n'
    'ret-nested\n'
    '2\n'
    '8\n'
    'ret-holder\n'
    '2\n'
    'ret-nested\n'
    '2\n'
    'ret-holder\n'
    '3\n'
    '8\n'
    'ret-nested\n'
    '3\n'
    '9\n'
    'ret-holder\n'
    '3\n'
    '3\n'
    'ret-holder\n'
    '2\n'
    '7\n'
    'arg-nested\n'
    '2\n'
    '8\n'
    'arg-holder\n'
    '2\n'
    '7\n'
    'kw-arg-nested\n'
    '2\n'
    '8\n'
    'kw-arg-holder\n'
    '2\n'
    '7\n'
    'kw-local-nested\n'
    '2\n'
    '8\n'
    'kw-local-holder\n'
    '2\n'
    'kw-local-nested\n'
    '2\n'
    'kw-local-holder\n'
    '2\n'
    '7\n'
    'method-kw-arg-nested\n'
    '2\n'
    '8\n'
    'method-kw-arg-holder\n'
    '2\n'
    '7\n'
    'method-kw-local-nested\n'
    '2\n'
    '8\n'
    'method-kw-local-holder\n'
    '2\n'
    'method-kw-local-nested\n'
    '2\n'
    'method-kw-local-holder\n'
    '3\n'
    '8\n'
    'method-nested\n'
    '3\n'
    '9\n'
    'method-holder\n'
    '2\n'
    '7\n'
    'walrus-nested\n'
    '2\n'
    '8\n'
    'walrus-holder\n'
    '2\n'
    'walrus-nested\n'
    '2\n'
    'walrus-holder\n'
    '2\n'
    '7\n'
    'reassigned-nested\n'
    '2\n'
    '8\n'
    'reassigned-holder\n'
    '2\n'
    'reassigned-nested\n'
    '2\n'
    'reassigned-holder\n'
    '2\n'
    '7\n'
    'cond-true-nested\n'
    '2\n'
    '8\n'
    'cond-true-holder\n'
    '2\n'
    'cond-true-nested\n'
    '2\n'
    'cond-true-holder\n'
    '2\n'
    '7\n'
    'cond-false-nested\n'
    '2\n'
    '8\n'
    'cond-false-holder\n'
    '2\n'
    'cond-false-nested\n'
    '2\n'
    'cond-false-holder\n'
    '2\n'
    '7\n'
    'loop-second-nested\n'
    '2\n'
    '8\n'
    'loop-second-holder\n'
    '2\n'
    'loop-second-nested\n'
    '2\n'
    'loop-second-holder\n'
    '2\n'
    '7\n'
    'try-live-nested\n'
    '2\n'
    '8\n'
    'try-live-holder\n'
    '2\n'
    'try-live-nested\n'
    '2\n'
    'try-live-holder\n'
    '2\n'
    '7\n'
    'closure-nested\n'
    '2\n'
    '8\n'
    'closure-holder\n'
    '2\n'
    'closure-nested\n'
    '2\n'
    'closure-holder\n'
    '2\n'
    '7\n'
    'unpack-left-nested\n'
    '2\n'
    '8\n'
    'unpack-left-holder\n'
    '2\n'
    '7\n'
    'unpack-right-nested\n'
    '2\n'
    '8\n'
    'unpack-right-holder\n'
    '2\n'
    'unpack-left-nested\n'
    '2\n'
    'unpack-left-holder\n'
    '2\n'
    'unpack-right-nested\n'
    '2\n'
    'unpack-right-holder\n'
    '2\n'
    '7\n'
    'for-first-nested\n'
    '2\n'
    '8\n'
    'for-first-holder\n'
    '2\n'
    '7\n'
    'for-second-nested\n'
    '2\n'
    '8\n'
    'for-second-holder\n'
    '2\n'
    'for-second-nested\n'
    '2\n'
    'for-second-holder\n'
    'comp-first-nested\n'
    'comp-second-nested\n'
    '2\n'
    'True\n'
    'True\n'
    'dict-first-nested\n'
    'dict-second-nested\n'
    '2\n'
    '7\n'
    'sub-second-nested\n'
    '2\n'
    '8\n'
    'sub-second-holder\n'
    '2\n'
    'sub-second-nested\n'
    '2\n'
    'sub-second-holder\n'
    '2\n'
    '7\n'
    'tuple-sub-second-nested\n'
    '2\n'
    '8\n'
    'tuple-sub-second-holder\n'
    '2\n'
    'tuple-sub-second-nested\n'
    '2\n'
    'tuple-sub-second-holder\n'
    '2\n'
    '7\n'
    'bool-left-nested\n'
    '2\n'
    '8\n'
    'bool-left-holder\n'
    '2\n'
    'bool-left-nested\n'
    '2\n'
    'bool-left-holder\n'
    '2\n'
    '7\n'
    'bool-second-nested\n'
    '2\n'
    '8\n'
    'bool-second-holder\n'
    '2\n'
    'bool-second-nested\n'
    '2\n'
    'bool-second-holder\n'
    '2\n'
    '7\n'
    'global-nested\n'
    '2\n'
    '8\n'
    'global-holder\n'
    '2\n'
    'global-nested\n'
    '2\n'
    'global-holder\n'
    'new-holder\n'
    'new\n'
    '1\n'
    'del:old\n'
)


# This is the original constructor-temporary attribute probe, with a driver
# that keeps and mutates its result across explicit collections.
CONSTRUCTOR_ATTRIBUTE_PROGRAM = """import pcc
@pcc.valueclass
class Record:
    value: list

def take(*, value, **extras):
    return value
def probe():
    return take(value=Record([1]).value)

import gc
value = probe()
gc.collect()
assert value == [1]
value.append(2)
gc.collect()
assert value == [1, 2]
print('CONSTRUCTOR_ATTRIBUTE_OWNER_OK')
"""


CASES = SCENARIOS[:2] + (
    Case("original-name", ORIGINAL_NAME_PROGRAM, "VALUECLASS_NAME_OWNERS_OK\n"),
) + SCENARIOS[2:] + (
    Case(
        "original-direct-payload",
        DIRECT_PAYLOAD_PROGRAM,
        DIRECT_PAYLOAD_EXPECTED,
        "del:shutdown",
    ),
    Case("original-valuebox", VALUEBOX_PROGRAM, "\n".join(VALUEBOX_EXPECTED) + "\n"),
    Case("original-constructor-attribute", CONSTRUCTOR_ATTRIBUTE_PROGRAM, "CONSTRUCTOR_ATTRIBUTE_OWNER_OK\n"),
)


@pytest.fixture(autouse=True)
def _explicit_inputs_before_compiler(explicit_owned_runtime, monkeypatch):
    # Run this dependency before resolving the compiler fixture. Missing native
    # inputs are failures; neither fixture may provision or silently skip them.
    monkeypatch.setenv("PCC_TEST_COMPILER_STRICT", "1")
    return explicit_owned_runtime


def _digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _write_receipt(path, receipt):
    path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")


def _check_program_result(case, result):
    assert result["returncode"] == 0, result
    assert result["stdout"] == case.expected_stdout, result
    if case.required_stderr:
        assert case.required_stderr in result["stderr"], result
    else:
        assert result["stderr"] == "", result


def _observed_collectors(path):
    assert path.is_file(), "Native execution produced no GC log"
    events = parse_log_lines(path.read_text(encoding="utf-8").splitlines())
    return sorted({
        event.fields["value1"]
        for event in events
        if event.fields.get("category") == "gc"
        and event.event in ("collect_start", "collect_stop", "collect_end")
    })


def _native_magic(path):
    with Path(path).open("rb") as stream:
        magic = stream.read(4)
    return magic in (
        b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf",
    ) or magic[:2] == b"MZ"


def _error_output(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_aggregate_ownership_native_five_gc(
    case, python_program_compiler, request, explicit_owned_runtime, tmp_path, capfd,
):
    """Compile one isolated case; preserve all five runs before asserting."""
    requested_mode = request.node.callspec.params["python_program_compiler"]
    actual_mode = requested_mode
    compiler = python_program_compiler
    if compiler.__module__ == "tests.pcc1_route":
        actual_mode = "pcc1-routed"
    source = tmp_path / "program.py"
    source.write_text(case.program, encoding="utf-8")
    binary = tmp_path / "program.out"
    archive = explicit_owned_runtime
    receipt_path = tmp_path / "ownership-regression.json"
    receipt = {
        "schema": "pcc.aggregate-ownership-regression.v1",
        "case": case.name,
        "status": "REFERENCE_RUNNING",
        "compiler_fixture_parameter": requested_mode,
        "compiler_mode": actual_mode,
        "host_codegen_source_sha256": codegen_checksum(),
        "backend": "self",
        "libpython": "off",
        "source_sha256": _digest(source),
        "runtime_archive": str(archive),
        "runtime_sha256": _digest(archive),
        "runtime_provenance_sha256": _digest(str(archive) + ".provenance.json"),
        "requested_collectors": list(range(5)),
        "executions": [],
    }
    if actual_mode == "pcc1-routed":
        compiler_path = Path(os.environ["PCC_TEST_COMPILER"])
    elif actual_mode == "pcc1":
        compiler_path = request.getfixturevalue("native_pcc1_compiler")
    else:
        compiler_path = None
    if compiler_path is not None:
        assert _native_magic(compiler_path), "Selected compiler is not native"
        receipt["compiler_binary"] = str(compiler_path)
        receipt["compiler_binary_sha256"] = _digest(compiler_path)
    _write_receipt(receipt_path, receipt)

    reference_environment = dict(os.environ)
    reference_environment.pop("LC_ALL", None)
    for key in ("PCC_LOG", "PCC_LOG_FORMAT", "PCC_LOG_FILE"):
        reference_environment.pop(key, None)
    try:
        reference = _record_execution(
            tmp_path, "reference", [sys.executable, str(source)], reference_environment,
        )
        receipt["reference"] = reference
        _check_program_result(case, reference)
    except Exception as error:
        receipt["status"] = "REFERENCE_FAILED"
        receipt["error"] = type(error).__name__ + ": " + str(error)
        _write_receipt(receipt_path, receipt)
        raise

    compile_options = {}
    if actual_mode == "pcc0":
        from pcc.frontends.python.pipeline import compile_python

        if (
            compiler is compile_python
            and compiler.__module__ == "pcc.frontends.python.pipeline"
        ):
            receipt["compile_profile"] = {}
            compile_options["profile"] = receipt["compile_profile"]
    receipt["status"] = "COMPILE_RUNNING"
    _write_receipt(receipt_path, receipt)
    try:
        start_ns = time.monotonic_ns()
        try:
            compiler(
                str(source), str(binary), backend="self", libpython_mode="off",
                ir_scaffold_mode="on", runtime_archive=str(archive), **compile_options,
            )
        finally:
            end_ns = time.monotonic_ns()
            receipt["compile_timing"] = {
                "start_ns": start_ns,
                "end_ns": end_ns,
                "elapsed_ns": end_ns - start_ns,
            }
            if "compile_profile" in receipt:
                try:
                    json.dumps(receipt["compile_profile"])
                except (TypeError, ValueError, RecursionError) as error:
                    # Diagnostic data must not replace a compiler failure.
                    del receipt["compile_profile"]
                    receipt["compile_profile_error"] = type(error).__name__ + ": " + str(error)
        assert binary.is_file(), "Compiler did not emit an executable"
        assert _native_magic(binary), "Compiler emitted a non-native launcher"
        receipt["binary_sha256"] = _digest(binary)
    except Exception as error:
        receipt["status"] = "COMPILE_FAILED"
        receipt["error"] = type(error).__name__ + ": " + str(error)
        _write_receipt(receipt_path, receipt)
        raise
    finally:
        captured = capfd.readouterr()
        (tmp_path / "compiler-wrapper.stdout").write_text(captured.out, encoding="utf-8")
        (tmp_path / "compiler-wrapper.stderr").write_text(captured.err, encoding="utf-8")

    receipt["status"] = "NATIVE_RUNNING"
    _write_receipt(receipt_path, receipt)
    for backend in range(5):
        log_path = tmp_path / f"gc{backend}.gc.jsonl"
        environment = dict(
            os.environ,
            PCC_GC_BACKEND=str(backend),
            PCC_LOG="gc",
            PCC_LOG_FORMAT="json",
            PCC_LOG_FILE=str(log_path),
            PCC_TEST_NO_NATIVE_PROVISIONING="1",
            PCC_NO_AUTO_PCC1="1",
            PCC_TEST_COMPILER_STRICT="1",
            PCC_HOST_PYTHON="/nonexistent/host-python",
            PCC_HOST_PCC="/nonexistent/host-pcc",
            PATH="",
        )
        environment.pop("LC_ALL", None)
        row = {
            "requested_gc": backend,
            "observed_gc": [],
            "gc_log": str(log_path),
            "status": "RUNNING",
            "errors": [],
        }
        try:
            result = _record_execution(tmp_path, f"gc{backend}", [str(binary)], environment)
            row.update(result)
            _check_program_result(case, result)
            assert all(result[key] == reference[key] for key in (
                "returncode", "stdout", "stderr",
            )), result
        except Exception as error:
            row["errors"].append(type(error).__name__ + ": " + str(error))
            # Timeouts/process errors remain failures and do not prevent the
            # other collector processes from producing independent evidence.
            if "returncode" not in row:
                row.update(
                    command=[str(binary)],
                    returncode=None,
                    stdout=_error_output(getattr(error, "stdout", None)),
                    stderr=_error_output(getattr(error, "stderr", None)),
                )
                (tmp_path / f"gc{backend}.stdout").write_text(row["stdout"], encoding="utf-8")
                (tmp_path / f"gc{backend}.stderr").write_text(row["stderr"], encoding="utf-8")
        try:
            row["observed_gc"] = _observed_collectors(log_path)
            row["gc_log_sha256"] = _digest(log_path)
            assert row["observed_gc"] == [backend], row["observed_gc"]
        except Exception as error:
            row["errors"].append(type(error).__name__ + ": " + str(error))
        row["status"] = "FAIL" if row["errors"] else "PASS"
        _write_receipt(tmp_path / f"gc{backend}.json", row)
        receipt["executions"].append(row)
        _write_receipt(receipt_path, receipt)

    failures = [row for row in receipt["executions"] if row["status"] != "PASS"]
    receipt["status"] = "FAIL" if failures else "PASS"
    _write_receipt(receipt_path, receipt)
    assert not failures, {"receipt": str(receipt_path), "failures": failures}
