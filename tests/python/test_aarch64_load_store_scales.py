"""Host-only AArch64 scale dispatch regressions; no native execution.

This separate file is not registered in the fixed-count CI preflight inventory.
"""

import ast
import inspect
import struct

import pytest

from pcc.backend import elf_aarch64_relocations as arm


NOP = 0xD503201F
RET = 0xD65F03C0
BASE = 0x40000000


def _words(*words):
    return b"".join(struct.pack("<I", word) for word in words)


_LOAD_STORE_SCALES = (
    (278, 0), (284, 1), (285, 2), (286, 3), (299, 4), (312, 3), (542, 3),
)


@pytest.mark.parametrize("kind,scale", _LOAD_STORE_SCALES)
@pytest.mark.parametrize("initial", [0xF9400067, 0xF97FFC67])
def test_load_store_scale_boundaries_preserve_other_bits(kind, scale, initial):
    quantum = 1 << scale
    targets = (0, quantum, 4096 - quantum, 4096, 4096 + quantum,
               -quantum, -4096, -4096 - quantum,
               (1 << 64) + quantum, -(1 << 80) + quantum)
    mask = 0x003FFC00
    for target in targets:
        image = bytearray(_words(NOP, initial, RET))
        arm.apply_relocation(image, 4, kind, target, BASE + 4)
        expected = (initial & ~mask) | (((target & 0xFFF) >> scale) << 10)
        assert image == _words(NOP, expected, RET)


@pytest.mark.parametrize("kind,scale", _LOAD_STORE_SCALES[1:])
def test_load_store_scale_rejects_misalignment_without_writing(kind, scale):
    quantum = 1 << scale
    for target in (1, quantum - 1, 4095, 4097, -1, -4095):
        image = bytearray(_words(NOP, 0xF97FFC67, RET))
        before = bytes(image)
        with pytest.raises(ValueError) as caught:
            arm.apply_relocation(image, 4, kind, target, BASE + 4)
        assert str(caught.value) == "misaligned AArch64 load/store relocation"
        assert bytes(image) == before


@pytest.mark.parametrize("kind,scale", _LOAD_STORE_SCALES)
@pytest.mark.parametrize("offset,message", [
    (-4, "AArch64 relocation outside image"),
    (12, "AArch64 relocation outside image"),
    (10, "AArch64 relocation outside image"),
    (2, "unaligned AArch64 instruction relocation"),
])
def test_load_store_site_validation_precedes_target_alignment(kind, scale, offset, message):
    del scale
    image = bytearray(_words(NOP, 0xF97FFC67, RET))
    before = bytes(image)
    with pytest.raises(ValueError) as caught:
        arm.apply_relocation(image, offset, kind, 1, BASE)
    assert str(caught.value) == message
    assert bytes(image) == before


@pytest.mark.parametrize("kind", [-1, 1, 256, 259, 276, 279, 300, 543, 552])
def test_unsupported_relocation_kind_fails_before_image_access(kind):
    class NoImageAccess:
        def __len__(self):
            raise AssertionError("unsupported relocation accessed image")

    with pytest.raises(ValueError) as caught:
        arm.apply_relocation(NoImageAccess(), 0, kind, 0, BASE)
    assert str(caught.value) == "unsupported AArch64 ELF relocation: " + str(kind)


@pytest.mark.parametrize("kind,scale", _LOAD_STORE_SCALES)
def test_load_store_nonexact_kinds_keep_dictionary_lookup(kind, scale):
    events = []

    class ObservedKind(int):
        def __hash__(self):
            events.append("hash")
            return int.__hash__(self)

        def __eq__(self, other):
            events.append(("equal", other))
            return int.__eq__(self, other)

    class UnhashableKind(int):
        __hash__ = None

    class RaisingHashKind(int):
        def __hash__(self):
            raise RuntimeError("kind hash failure")

    target = 4096 - (1 << scale)
    expected = bytearray(_words(0xF97FFC67))
    arm.apply_relocation(expected, 0, kind, target, BASE)
    for value in (float(kind), ObservedKind(kind)):
        image = bytearray(_words(0xF97FFC67))
        arm.apply_relocation(image, 0, value, target, BASE)
        assert image == expected
    assert events[-2:] == ["hash", ("equal", kind)]

    for value, error, message in (
        (UnhashableKind(kind), TypeError, "unhashable type"),
        (RaisingHashKind(kind), RuntimeError, "kind hash failure"),
    ):
        image = bytearray(_words(0xF97FFC67))
        before = bytes(image)
        with pytest.raises(error, match=message):
            arm.apply_relocation(image, 0, value, target, BASE)
        assert bytes(image) == before


def test_exact_integer_scale_dispatch_fails_closed_for_new_width_kind(monkeypatch):
    kind = 300
    original_width = arm.relocation_width
    monkeypatch.setattr(
        arm, "relocation_width",
        lambda value: 4 if value == kind else original_width(value),
    )
    image = bytearray(_words(NOP, 0xF97FFC67, RET))
    before = bytes(image)
    with pytest.raises(KeyError) as caught:
        arm.apply_relocation(image, 4, kind, 0, BASE + 4)
    assert caught.value.args == (kind,)
    assert bytes(image) == before


def test_only_seven_load_store_kinds_reach_exact_integer_scale_dispatch(monkeypatch):
    # Keep the scalar dispatch in sync if the accepted relocation set expands.
    # The width whitelist is finite; inspect every integer it admits, including
    # a newly added literal without requiring a separate test-side inventory.
    width_tree = ast.parse(inspect.getsource(arm.relocation_width))
    kinds = {node.value for node in ast.walk(width_tree)
             if isinstance(node, ast.Constant) and type(node.value) is int}
    observed = []

    def observe_type(value):
        observed.append(value)
        return type(value)

    # This inventory observes scale dispatch only; the independent final-word
    # writer also checks exact primitive types and has its own regressions.
    monkeypatch.setattr(arm, "_write_instruction_word", lambda image, offset, word:
                        struct.pack_into("<I", image, offset, word))
    monkeypatch.setattr(arm, "type", observe_type, raising=False)
    for kind in sorted(kinds):
        try:
            arm.relocation_width(kind)
        except ValueError:
            continue
        arm.apply_relocation(bytearray(8), 0, kind, 0, 0)
    assert observed == sorted(kind for kind, _scale in _LOAD_STORE_SCALES)
