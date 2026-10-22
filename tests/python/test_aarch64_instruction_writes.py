"""Host differential controls for the final AArch64 instruction-word write.

Both struct providers below execute under host Python, not a native compiler.
The exact-primitive path intentionally bypasses internal struct packing hooks;
supplied-object callbacks, read hooks and generic fallback behavior are retained.
"""

from array import array
from types import SimpleNamespace

import pytest
import struct

from pcc.backend import elf_aarch64_relocations as arm
from pcc.stdlib import struct as owned_struct


BASE = (1 << 100) + 0x40000000
ORIGINAL = bytes.fromhex("1f2003d5 67007ff9 c0035fd6")


def _generic_write(image, offset, word):
    arm.struct.pack_into("<I", image, offset, word)


def _outcome(operation, image):
    try:
        result = operation()
    except Exception as error:
        return type(error), error.args, bytes(image)
    return result, bytes(image)


@pytest.fixture(params=[struct, owned_struct], ids=["cpython", "owned-on-host"])
def provider(monkeypatch, request):
    monkeypatch.setattr(arm, "struct", request.param)
    return request.param


@pytest.mark.parametrize("offset", [0, 1, 4, 8])
def test_exact_words_use_fixed_size_copy_without_generic_pack(monkeypatch, offset):
    def forbidden(*args):
        raise AssertionError("exact primitive write called generic pack_into")

    # This is an explicit implementation boundary: callers cannot observe
    # internal pack_into hooks for exact primitive values on an admitted site.
    monkeypatch.setattr(arm, "struct", SimpleNamespace(pack_into=forbidden))
    words = [0, 1, 255, 256, 65535, 65536, 0x7FFFFFFF, 0x80000000, 0xFFFFFFFF]
    words += [((index + 1) * 0x9E3779B9) & 0xFFFFFFFF for index in range(32)]
    for word in words:
        expected = bytearray(ORIGINAL)
        struct.pack_into("<I", expected, offset, word)
        image = bytearray(ORIGINAL)
        # An exported view forbids resizing, but permits the equal-size copy.
        view = memoryview(image)
        assert arm._write_instruction_word(image, offset, word) is None
        assert image == expected
        assert bytes(view) == expected
        assert len(image) == len(ORIGINAL)
        view.release()


_INSTRUCTION_CASES = [
    (kind, [BASE + delta for delta in (-(1 << 27), -4, 0, 4, (1 << 27) - 4)])
    for kind in (282, 283)
] + [
    (274, [BASE + delta for delta in (-(1 << 20), -1, 0, 1, (1 << 20) - 1)]),
] + [
    (kind, [BASE + delta * 4096 + 0xABC
            for delta in (-(1 << 20), -1, 0, 1, (1 << 20) - 1)])
    for kind in (275, 311, 541)
] + [
    (277, [0, 0xFFF, 0x1000, -1, (1 << 120) + 0xABC, -(1 << 120) + 0xABC]),
    (549, [0, 0xFFF, 0x1000, (1 << 24) - 1]),
    (551, [0, 0xFFF, 0x1000, (1 << 24) - 1]),
] + [
    (kind, [0, 1 << scale, 4096 - (1 << scale), 4096,
            -(1 << scale), (1 << 120) + (1 << scale),
            -(1 << 120) + (1 << scale)])
    for kind, scale in ((278, 0), (284, 1), (285, 2), (286, 3),
                        (299, 4), (312, 3), (542, 3))
]


@pytest.mark.parametrize("kind,targets", _INSTRUCTION_CASES)
@pytest.mark.parametrize("initial", [0, 0xFFFFFFFF, 0xF97FFC67])
def test_instruction_bytes_equal_original_final_pack(
    monkeypatch, provider, kind, targets, initial,
):
    def forbidden(*args):
        raise AssertionError("exact instruction relocation used generic pack_into")

    for target in targets:
        initial_image = bytearray(ORIGINAL)
        struct.pack_into("<I", initial_image, 4, initial)
        expected = bytearray(initial_image)
        with monkeypatch.context() as patch:
            patch.setattr(arm, "_write_instruction_word", _generic_write)
            arm.apply_relocation(expected, 4, kind, target, BASE)
        image = bytearray(initial_image)
        with monkeypatch.context() as patch:
            patch.setattr(arm, "struct", SimpleNamespace(
                unpack_from=provider.unpack_from, pack_into=forbidden,
            ))
            arm.apply_relocation(image, 4, kind, target, BASE)
        assert image == expected
        assert image[:4] == initial_image[:4]
        assert image[8:] == initial_image[8:]


@pytest.mark.parametrize("offset,kind,target,message", [
    (100, 999, 1, "unsupported AArch64 ELF relocation: 999"),
    (-4, 283, BASE + 1, "AArch64 relocation outside image"),
    (12, 283, BASE + 1, "AArch64 relocation outside image"),
    (10, 283, BASE + 1, "AArch64 relocation outside image"),
    (2, 283, BASE + 1, "unaligned AArch64 instruction relocation"),
    (4, 283, BASE + 1, "unaligned AArch64 branch target"),
    (4, 283, BASE + (1 << 27), "AArch64 relocation out of range: 33554432; signed_bits=26"),
    (4, 282, BASE - (1 << 27) - 4, "AArch64 relocation out of range: -33554433; signed_bits=26"),
    (4, 274, BASE + (1 << 20), "AArch64 relocation out of range: 1048576; signed_bits=21"),
    (4, 275, BASE - (1 << 32) - 4096, "AArch64 relocation out of range: -1048577; signed_bits=21"),
    (4, 549, 1 << 24, "AArch64 local-exec TLS offset exceeds 24 bits"),
    (4, 551, -1, "AArch64 local-exec TLS offset exceeds 24 bits"),
    (4, 286, 1, "misaligned AArch64 load/store relocation"),
])
def test_original_validation_error_order_and_no_write(
    monkeypatch, provider, offset, kind, target, message,
):
    del provider
    expected = bytearray(ORIGINAL)
    with monkeypatch.context() as patch:
        patch.setattr(arm, "_write_instruction_word", _generic_write)
        old = _outcome(lambda: arm.apply_relocation(expected, offset, kind, target, BASE), expected)
    image = bytearray(ORIGINAL)
    new = _outcome(lambda: arm.apply_relocation(image, offset, kind, target, BASE), image)
    assert new == old == (ValueError, (message,), ORIGINAL)


@pytest.mark.parametrize("offset,word", [
    (-16, 7), (-4, 7), (9, 7), (12, 7), (1 << 100, 7),
    (4, -1), (4, 1 << 32), (4, 1 << 100), (12, -1), (12, 1 << 100),
])
def test_generic_bounds_and_value_errors_are_provider_exact(provider, offset, word):
    expected = bytearray(ORIGINAL)
    old = _outcome(lambda: provider.pack_into("<I", expected, offset, word), expected)
    image = bytearray(ORIGINAL)
    new = _outcome(lambda: arm._write_instruction_word(image, offset, word), image)
    assert new == old


@pytest.mark.parametrize("shape", ["subclass", "memoryview", "array", "bytes", "custom"])
def test_nonexact_images_keep_generic_write_protocol(monkeypatch, provider, shape):
    events = []

    class ObservedImage(bytearray):
        def __len__(self):
            events.append("len")
            return super().__len__()

        def __setitem__(self, key, value):
            events.append(("store", key, value))
            return super().__setitem__(key, value)

    class CustomImage:
        def __init__(self):
            self.data = bytearray(ORIGINAL)

        def __len__(self):
            events.append("len")
            return len(self.data)

        def __setitem__(self, key, value):
            events.append(("store", key, value))
            self.data[key] = value

        def __bytes__(self):
            return bytes(self.data)

    factories = {
        "subclass": lambda: ObservedImage(ORIGINAL),
        "memoryview": lambda: memoryview(bytearray(ORIGINAL)),
        "array": lambda: array("B", ORIGINAL),
        "bytes": lambda: ORIGINAL,
        "custom": CustomImage,
    }
    old_image = factories[shape]()
    old = _outcome(lambda: provider.pack_into("<I", old_image, 4, 0x12345678), old_image)
    old_events = events[:]
    events.clear()
    image = factories[shape]()
    new = _outcome(lambda: arm._write_instruction_word(image, 4, 0x12345678), image)
    assert (new, events) == (old, old_events)


@pytest.mark.parametrize("argument", ["offset", "word"])
@pytest.mark.parametrize("shape", ["subclass", "protocol", "bool"])
def test_nonexact_integers_keep_generic_conversion(provider, argument, shape):
    events = []

    class ObservedInt(int):
        def __int__(self):
            events.append("int")
            return int.__int__(self)

        def __index__(self):
            events.append("index")
            return int.__index__(self)

        def to_bytes(self, *args):
            raise AssertionError("subclass to_bytes must not replace generic conversion")

    class ProtocolInt:
        def __int__(self):
            events.append("int")
            return 4

        def __index__(self):
            events.append("index")
            return 4

    value = {"subclass": ObservedInt(4), "protocol": ProtocolInt(), "bool": False}[shape]
    offset, word = (value, 0x12345678) if argument == "offset" else (4, value)
    expected = bytearray(ORIGINAL)
    old = _outcome(lambda: provider.pack_into("<I", expected, offset, word), expected)
    old_events = events[:]
    events.clear()
    image = bytearray(ORIGINAL)
    new = _outcome(lambda: arm._write_instruction_word(image, offset, word), image)
    assert (new, events) == (old, old_events)


@pytest.mark.parametrize("callback", ["target", "read", "kind"])
@pytest.mark.parametrize("size", [0, 4, 7, 8, 20])
def test_callback_resize_preserves_final_error_and_bytes(monkeypatch, provider, callback, size):
    writer = arm._write_instruction_word

    def run(selected_writer):
        image = bytearray(ORIGINAL)
        events = []

        def resize():
            events.append("resize")
            image[:] = b"x" * size

        class Target:
            def __and__(self, mask):
                assert mask == 0xFFF
                resize()
                return 0xABC

        class Kind(int):
            def __hash__(self):
                resize()
                return int.__hash__(self)

        def unpack(fmt, buffer, offset):
            events.append("read")
            result = provider.unpack_from(fmt, buffer, offset)
            if callback == "read":
                resize()
            return result

        with monkeypatch.context() as patch:
            patch.setattr(arm, "_write_instruction_word", selected_writer)
            patch.setattr(arm, "struct", SimpleNamespace(
                unpack_from=unpack, pack_into=provider.pack_into,
            ))
            target = Target() if callback == "target" else 0xABC
            kind = Kind(278) if callback == "kind" else 277
            result = _outcome(lambda: arm.apply_relocation(image, 4, kind, target, BASE), image)
        return result, events

    expected = run(_generic_write)
    actual = run(writer)
    assert actual == expected
    assert actual[1] == ["read", "resize"]
    assert len(actual[0][-1]) == size
    if size < 8:
        assert actual[0][0] is provider.error


def test_custom_word_from_read_hook_keeps_generic_pack(monkeypatch, provider):
    events = []

    class Word(int):
        def __and__(self, other):
            events.append("and")
            return self

        def __or__(self, other):
            events.append("or")
            return self

        def to_bytes(self, *args):
            raise AssertionError("custom word to_bytes called")

    def unpack(*args):
        events.append("read")
        return (Word(0x12345678),)

    def pack(fmt, image, offset, word):
        events.append("pack")
        assert type(word) is Word
        return provider.pack_into(fmt, image, offset, word)

    monkeypatch.setattr(arm, "struct", SimpleNamespace(unpack_from=unpack, pack_into=pack))
    image = bytearray(ORIGINAL)
    arm.apply_relocation(image, 4, 277, 0, BASE)
    assert events == ["read", "and", "or", "pack"]
    assert image == ORIGINAL[:4] + bytes.fromhex("78563412") + ORIGINAL[8:]


def test_custom_offset_callbacks_keep_original_read_then_write(monkeypatch, provider):
    writer = arm._write_instruction_word

    def run(selected_writer):
        events = []
        image = bytearray(ORIGINAL)

        class Offset:
            def __lt__(self, other):
                events.append(("less", other))
                return False

            def __add__(self, other):
                events.append(("add", other))
                return 4 + other

            def __and__(self, other):
                events.append(("and", other))
                return 4 & other

            def __index__(self):
                events.append("index")
                return 4

            def __int__(self):
                events.append("int")
                return 4

        with monkeypatch.context() as patch:
            patch.setattr(arm, "_write_instruction_word", selected_writer)
            result = _outcome(lambda: arm.apply_relocation(image, Offset(), 277, 0xABC, BASE), image)
        return result, events

    actual = run(writer)
    assert actual == run(_generic_write)
    conversion = "index" if provider is struct else "int"
    assert actual[1] == [("less", 0), ("add", 4), ("and", 3), conversion, conversion]
