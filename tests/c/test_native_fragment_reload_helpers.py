from __future__ import annotations

import ast
import inspect
import textwrap
from dataclasses import replace

import pytest

from pcc.backend import BackendUnavailable, arm64_encode
from pcc.backend import self_backend_aarch64_darwin_regs as regs
from pcc.backend import self_backend_aarch64_darwin_slots as slots
from pcc.backend import self_backend_precise_stackmaps as stackmaps
from pcc.backend.self_backend_aarch64_fragments import AArch64EmissionFragments, EMISSION_RECORD_LABEL
from pcc.backend.self_backend_ir import TypeDesc
from pcc.backend.self_backend_value_arena import CompilerInt2


class RecordingFragments:
    """Record canonical words while rejecting any helper-created fragment."""

    def __init__(self):
        self.fragment = CompilerInt2(7, 1)
        self.words = []
        self.labels = {}

    def _word(self, fragment, word):
        assert fragment == self.fragment
        self.words.append(word)

    def append_memory(self, fragment, mnemonic, reg, base, offset=0):
        self._word(fragment, arm64_encode.encode_emitted_load_store_parts(mnemonic, reg, base, offset))

    def append_move(self, fragment, dest, src):
        self._word(fragment, arm64_encode.encode_emitted_move_register_parts(dest, src))

    def append_movewide(self, fragment, mnemonic, dest, immediate, shift=0):
        self._word(fragment, arm64_encode.encode_emitted_movewide_parts(mnemonic, dest, immediate, shift))

    def append_addsub_immediate(self, fragment, mnemonic, dest, left, immediate):
        self._word(fragment, arm64_encode.encode_emitted_addsub_immediate_parts(mnemonic, dest, left, immediate))

    def append_addsub_register(self, fragment, mnemonic, dest, left, right):
        self._word(fragment, arm64_encode.encode_emitted_addsub_register_parts(mnemonic, dest, left, right))

    def append_label(self, fragment, name):
        assert fragment == self.fragment
        assert name not in self.labels
        self.labels[name] = len(self.words) * 4

    def append_nop(self, fragment):
        encoded = arm64_encode.assemble_text_lines(["  nop"]).code
        self._word(fragment, int.from_bytes(encoded, "little"))

    def assert_matches(self, lines):
        oracle = arm64_encode.assemble_text_lines(lines)
        assert b"".join(word.to_bytes(4, "little") for word in self.words) == oracle.code
        assert self.labels == oracle.labels


@pytest.mark.parametrize("bits,reg,value", (
    (1, "w0", 1), (8, "w1", -1), (16, "w2", 0xABCD),
    (32, "w3", 0x12345678), (64, "x16", 0), (64, "x16", -1),
    (64, "x16", 0x1234000056780000), (64, "x16", 1 << 48),
))
def test_fragment_immediates_match_text_oracle_without_chunk_containers(bits, reg, value):
    owner = RecordingFragments()
    assert regs.append_const_to_reg_bits(owner, owner.fragment, bits, reg, value) is None
    owner.assert_matches(regs.emit_const_to_reg_bits(bits, reg, value))


@pytest.mark.parametrize("kind,width,reg,value", (
    ("ptr", 64, "x16", 0x123456780000),
    ("int", 8, "w1", -1), ("int", 64, "x10", -0x12345678),
    ("float", 64, "w12", 0x12345678),
))
def test_fragment_scalar_type_facts_preserve_constant_width(kind, width, reg, value):
    owner = RecordingFragments()
    regs.append_const_to_reg(
        owner, owner.fragment, kind == "ptr", kind == "int", width, reg, value,
    )
    owner.assert_matches(regs.emit_const_to_reg(TypeDesc(kind, width), reg, value))


@pytest.mark.parametrize("dest,base,offset", (
    ("x16", "x16", 0), ("x15", "x14", 0), ("x16", "x16", 4095),
    ("x16", "x16", -4095), ("x15", "x14", 4096),
    ("x15", "x14", -(0x12345678 << 16)),
))
def test_fragment_offset_preserves_scratch_and_large_immediate_choices(dest, base, offset):
    owner = RecordingFragments()
    regs.append_add_offset(owner, owner.fragment, dest, base, offset)
    owner.assert_matches(regs.emit_add_offset(dest, base, offset))


@pytest.mark.parametrize("offset", (0, 255, 256, 4096, 0x123456))
@pytest.mark.parametrize("kind,width,reg", (
    ("ptr", 64, "x16"), ("int", 8, "w15"),
    ("int", 32, "w13"), ("int", 64, "x12"), ("float", 32, "s0"),
))
def test_fragment_slot_load_store_matches_text_oracle(offset, kind, width, reg):
    owner = RecordingFragments()
    value_type = TypeDesc(kind, width)
    slots.append_load_slot_to_reg_parts(owner, owner.fragment, offset, kind == "int", width, reg)
    slots.append_store_reg_to_slot_parts(owner, owner.fragment, reg, offset, kind == "int", width)
    owner.assert_matches(
        slots.load_slot_to_reg_parts(offset, value_type, reg)
        + slots.store_reg_to_slot_parts(reg, offset, value_type)
    )


# Encodings verified against the host toolchain, not derived from pcc:
#   $ llvm-mc --disassemble -triple=arm64
#   785f03ae -> ldurh w14, [x29, #-16]
#   781f03ae -> sturh w14, [x29, #-16]
#   d282000e -> mov   x14, #4096
#   cb0e03af -> sub   x15, x29, x14
#   784001ee -> ldurh w14, [x15]
#   780001ee -> sturh w14, [x15]
_HALFWORD_SLOT_ENCODINGS = {
    (16, "load"): (0x785F03AE,),
    (16, "store"): (0x781F03AE,),
    (4096, "load"): (0xD282000E, 0xCB0E03AF, 0x784001EE),
    (4096, "store"): (0xD282000E, 0xCB0E03AF, 0x780001EE),
}


@pytest.mark.parametrize("offset", (16, 4096))
@pytest.mark.parametrize("operation", ("load", "store"))
def test_halfword_native_slot_emits_the_ldurh_sturh_encoding(offset, operation):
    """16-bit managed slots encode as ldurh/sturh.

    This used to assert `EncodeError("unsupported emitted load/store
    mnemonic")` -- the fail-closed guard from before halfword slots were
    lowered.  The lowering landed; the guard did not, so the test kept
    demanding a refusal the encoder no longer owes.  Pin the encoding instead,
    which is the stronger contract: a wrong halfword encoding silently
    corrupts a managed reload.
    """
    owner = RecordingFragments()
    owner.append_nop(owner.fragment)
    before = len(owner.words)

    if operation == "load":
        slots.append_load_slot_to_reg_parts(
            owner, owner.fragment, offset, True, 16, "w14",
        )
    else:
        slots.append_store_reg_to_slot_parts(
            owner, owner.fragment, "w14", offset, True, 16,
        )

    emitted = tuple(owner.words[before:])
    assert emitted == _HALFWORD_SLOT_ENCODINGS[(offset, operation)]


@pytest.fixture
def packed_plan():
    records = stackmaps.PackedPlannedSafepoints(4, ("entry", "other"))
    reloads = (
        stackmaps.PlannedManagedReload(-8, -16, 0),
        stackmaps.PlannedManagedReload(-256, -4096, 17),
        stackmaps.PlannedManagedReload(-0x123456, -0x123478, -0x123400005678),
    )
    records.append(1, "Lentry", 0, (), 0, "", 0, ())
    records.append(2, "Lreload", 1, (), 0, "", 0, reloads)
    records.append(3, "Lterm", 2, (), 0, "", 0, ())
    records.add_entry_route(0, 0)
    records.add_terminator_route(0, 2, True)
    records.finish_build()
    plan = stackmaps.FunctionStackMapPlan(
        "probe", 1, 4096, "Lend", (), (), (), (), "aarch64-darwin", records,
    )
    try:
        yield plan
    finally:
        records.close()


def test_packed_stackmap_fragment_chain_matches_labels_reloads_and_padding(packed_plan, monkeypatch):
    lines = []
    packed_plan.append_packed_entry_lines(lines, 0)
    packed_plan.append_packed_record_lines(lines, 1)
    packed_plan.append_packed_terminator_lines(lines, 0)

    def forbidden(*args, **kwargs):
        pytest.fail("native packed reload constructed a TypeDesc or entered the text oracle")

    monkeypatch.setattr(stackmaps, "TypeDesc", forbidden)
    monkeypatch.setattr(stackmaps.FunctionStackMapPlan, "_reload_asm_lines_packed", forbidden)
    for _repeat in range(2):
        owner = RecordingFragments()
        packed_plan.append_packed_entry_span(owner, owner.fragment, 0)
        packed_plan.append_packed_record_span(owner, owner.fragment, 1)
        packed_plan.append_packed_terminator_span(owner, owner.fragment, 0)
        owner.assert_matches(lines)


def _native_fragment_contents(owner, fragment):
    code = bytearray()
    labels = {}
    owner.start_cursor(fragment)
    record_id = owner.next_record_id()
    while record_id >= 0:
        record = owner.records.get4_unchecked(record_id)
        if record.second == EMISSION_RECORD_LABEL:
            labels[owner.symbol_names[record.first]] = len(code)
        else:
            code.extend(record.first.to_bytes(4, "little"))
        record_id = owner.next_record_id()
    return bytes(code), labels


def test_real_fragment_owner_replays_independent_reload_fragments(packed_plan):
    oracle_lines = []
    packed_plan.append_packed_entry_lines(oracle_lines, 0)
    packed_plan.append_packed_record_lines(oracle_lines, 1)
    packed_plan.append_packed_terminator_lines(oracle_lines, 0)
    owner = AArch64EmissionFragments()
    try:
        first = owner.new_fragment()
        second = owner.new_fragment()
        owner.append_nop(first)
        for fragment in (first, second):
            packed_plan.append_packed_entry_span(owner, fragment, 0)
            packed_plan.append_packed_record_span(owner, fragment, 1)
            packed_plan.append_packed_terminator_span(owner, fragment, 0)
        for fragment, prefix in ((first, ["  nop"]), (second, [])):
            oracle = arm64_encode.assemble_text_lines(prefix + oracle_lines)
            assert _native_fragment_contents(owner, fragment) == (oracle.code, oracle.labels)
        assert len(owner.spans.spans) // 2 == 2
        assert owner.spans.projection_count == 0
    finally:
        owner.close()


def test_absent_packed_plan_preserves_populated_fragment(packed_plan):
    plan = replace(packed_plan, packed_records=None)
    owner = RecordingFragments()
    owner.append_nop(owner.fragment)
    initial = list(owner.words)
    plan.append_packed_entry_span(owner, owner.fragment, -999)
    plan.append_packed_record_span(owner, owner.fragment, -999)
    plan.append_packed_terminator_span(owner, owner.fragment, -999)
    assert owner.words == initial and owner.labels == {}


def test_empty_routes_and_other_targets_do_not_mutate_populated_fragment(packed_plan):
    owner = RecordingFragments()
    owner.append_nop(owner.fragment)
    initial = list(owner.words)
    packed_plan.append_packed_entry_span(owner, owner.fragment, 1)
    packed_plan.append_packed_terminator_span(owner, owner.fragment, 1)
    assert owner.words == initial and owner.labels == {}
    packed_plan = replace(packed_plan, target="x86_64-linux")
    for method, index in (
        (packed_plan.append_packed_entry_span, 0),
        (packed_plan.append_packed_record_span, 1),
        (packed_plan.append_packed_terminator_span, 0),
    ):
        with pytest.raises(BackendUnavailable, match="AArch64"):
            method(owner, owner.fragment, index)
        assert owner.words == initial and owner.labels == {}


def test_native_reload_helper_chain_has_no_list_text_or_type_projection():
    functions = (
        regs.append_const_to_reg_bits, regs.append_const_to_reg, regs.append_add_offset,
        slots.append_slot_base_address_parts, slots.append_load_slot_to_reg_parts,
        slots.append_store_reg_to_slot_parts,
        stackmaps.FunctionStackMapPlan._append_reload_span_packed,
        stackmaps.FunctionStackMapPlan.append_packed_entry_span,
        stackmaps.FunctionStackMapPlan.append_packed_record_span,
        stackmaps.FunctionStackMapPlan.append_packed_terminator_span,
    )
    for function in functions:
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
        assert not any(isinstance(node, (ast.List, ast.ListComp)) for node in ast.walk(tree)), function.__name__
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            assert name not in ("TypeDesc", "new_fragment", "_reload_asm_lines_packed"), function.__name__
            assert not name.startswith(("emit_", "emitted_")), function.__name__
