from __future__ import annotations

from dataclasses import replace
import struct

from pathlib import Path
from pcc.backend import self_backend_precise_stackmaps as precise_stackmaps
from pcc.backend import precise_stackmap as wire_stackmaps

import pytest

from pcc.backend import macho_obj, macho_spec as spec
from pcc.backend import elf_x86_64
from pcc.backend import BackendUnavailable
from pcc.backend.arm64_asm_driver import assemble_file
from pcc.backend.macho_link import LinkError, link_relocatable
from pcc.backend.precise_stackmap import (
    HEADER_SIZE,
    FUNCTION_SIZE,
    ARCH_AARCH64,
    ARCH_X86_64,
    FunctionStackMap,
    LOCATION_DERIVED,
    LOCATION_MANAGED,
    LOCATION_OWNED,
    LOCATION_REGISTER,
    LOCATION_RELOAD_REQUIRED,
    LOCATION_STACK_INDIRECT,
    NO_BASE,
    NO_OFFSET,
    PreciseStackMap,
    PreciseStackMapError,
    RECORD_HAS_EXCEPTION_EDGE,
    RECORD_SIZE,
    RECORD_SUSPENDED,
    SAFEPOINT_CALL,
    SAFEPOINT_CONTINUATION,
    SAFEPOINT_ENTRY,
    SAFEPOINT_EXCEPTION,
    SAFEPOINT_LOOP,
    SafepointRecord,
    StackMapLocation,
    decode_stack_map,
    encode_stack_map,
    function_address_offsets,
    function_id,
    render_stack_map_assembly,
    safepoint_id,
    scoped_stable_id,
    stable_id,
    validate_stack_map_payload,
)
from pcc.backend.self_backend_aarch64_darwin import emit_aarch64_darwin_asm
from pcc.backend.self_backend_ir import (
    GlobalDef,
    ParsedBlock,
    ParsedFunction,
    ParsedInstr,
    TypeDesc,
)
from pcc.backend.self_backend_precise_stackmaps import (
    PackedManagedLiveness,
    PackedRootStatePlane,
    PlannedRootLocation,
    _stack_locations,
    build_stack_map_plans,
)
from pcc.backend.self_backend_prepare import prepare_module_for_target
from pcc.backend.self_backend_value_arena import CompilerIntArena
from pcc.backend.self_backend_x86_64_linux import emit_x86_64_linux_asm


def _location(arch: int, offset: int = -8) -> StackMapLocation:
    return StackMapLocation(
        kind=LOCATION_STACK_INDIRECT,
        flags=LOCATION_MANAGED | LOCATION_OWNED,
        register=29 if arch == ARCH_AARCH64 else 6,
        base_index=NO_BASE,
        offset=offset,
    )


def test_packed_stack_map_record_arena_matches_public_codec() -> None:
    fields = (
        0x123456789ABCDEF,
        44,
        NO_OFFSET,
        7,
        3,
        0,
        SAFEPOINT_CALL,
        RECORD_HAS_EXCEPTION_EDGE,
        0,
        9,
    )
    records = CompilerIntArena()
    records.append4(*fields[:4])
    records.append4(*fields[4:8])
    records.append2(*fields[8:])

    assert precise_stackmaps._pack_stack_map_record_arena(records) == (
        precise_stackmaps._STACK_MAP_RECORD_CODEC.pack(*fields)
    )
    records.close()


def test_packed_managed_liveness_uses_batch_records_and_sparse_overflow():
    live = PackedManagedLiveness()
    empty_id = live.append_state(set())
    pair_id = live.append_state({9, 2})
    overflow_id = live.append_state({9, 2, 5})

    empty_record = live.record(empty_id)
    pair = live.record(pair_id)
    overflow = live.record(overflow_id)
    assert (
        empty_record.first,
        empty_record.second,
        empty_record.third,
        empty_record.fourth,
    ) == (0, -1, -1, 0)
    assert (pair.first, pair.second, pair.third, pair.fourth) == (2, 2, 9, 0)
    assert [pair.second, pair.third] == [2, 9]
    overflow_start = -overflow.third - 2
    assert [
        overflow.second,
        live.overflow_ids.get_unchecked(overflow_start),
        live.overflow_ids.get_unchecked(overflow_start + 1),
    ] == [
        2,
        5,
        9,
    ]
    live.close()

    words = CompilerIntArena()
    words.append((1 << 0) | (1 << 29))
    words.append((1 << 0) | (1 << 4))
    tracked = CompilerIntArena()
    for value_id in range(100, 135):
        tracked.append(value_id)
    packed = PackedManagedLiveness()
    state_id = packed.append_state_words(words, tracked, 2)
    state = packed.record(state_id)
    overflow_start = -state.third - 2
    assert (state.first, state.second) == (4, 100)
    assert [
        packed.overflow_ids.get_unchecked(overflow_start + index)
        for index in range(3)
    ] == [129, 130, 134]
    packed.close()
    tracked.close()
    words.close()


def test_packed_root_state_reuses_transitions_and_sorts_locations() -> None:
    roots = PackedRootStatePlane(block_count=1, protocol_hint=4)
    near_group = roots.intern_group(
        base_ref=1,
        origin_offset=0,
        count=2,
        owned=True,
        alloca_offset=32,
        frame_size=64,
    )
    far_group = roots.intern_group(
        base_ref=2,
        origin_offset=0,
        count=2,
        owned=False,
        alloca_offset=64,
        frame_size=64,
    )

    near_state = roots.transition(0, near_group, True)
    both_state = roots.transition(near_state, far_group, True)
    state_count = len(roots.state_spans) // 4
    assert roots.transition(near_state, far_group, True) == both_state
    assert len(roots.state_spans) // 4 == state_count

    locations = roots.ensure_state_locations(both_state)
    offsets = [
        roots.state_locations.get_unchecked((locations.first + index) * 2)
        for index in range(locations.second)
    ]
    assert offsets == [-64, -56, -32, -24]
    assert roots.has_location_offset(both_state, -64)
    assert roots.has_location_offset(both_state, -24)
    assert not roots.has_location_offset(both_state, -40)
    assert roots.transition(both_state, far_group, False) == near_state
    with pytest.raises(BackendUnavailable, match="registered twice"):
        roots.transition(both_state, near_group, True)
    roots.close()


def test_aarch64_label_offsets_skip_normalizing_ordinary_instructions():
    class InstructionText(str):
        def strip(self, *_args, **_kwargs):
            raise AssertionError("ordinary instruction was normalized")

    lines = [
        ".section __TEXT,__text,regular,pure_instructions",
        ".p2align 2",
        "_entry:",
        InstructionText("  add x0, x0, #1"),
        InstructionText("  ret"),
        ".p2align 3",
        "L_next:",
        "  .long 1, 2",
        ".space 4",
        ".section __DATA,__data",
        "_ignored:",
    ]

    assert precise_stackmaps._aarch64_text_label_offsets(lines) == {
        "_entry": 0,
        "L_next": 8,
    }


def test_stackmap_cfg_recovers_equal_block_labels_with_different_hashes():
    """Native bootstrap may hash equal parsed/terminator strings differently."""

    class HashSkewText(str):
        def __new__(cls, value: str):
            instance = super().__new__(cls, value)
            instance.hash_calls = 0
            return instance

        def __hash__(self) -> int:
            self.hash_calls += 1
            return super().__hash__() ^ self.hash_calls

    defined_target = HashSkewText("err.frame.791")
    edge_target = "err.frame.791"
    assert defined_target == edge_target

    void = TypeDesc("void")
    entry = ParsedBlock(
        name="entry",
        terminator=ParsedInstr("br", (edge_target,)),
    )
    target = ParsedBlock(
        name=defined_target,
        terminator=ParsedInstr("ret", (void, "null")),
    )
    func = ParsedFunction(
        name="hash_skew_cfg",
        ret_type=void,
        args=[],
        is_global=True,
        is_vararg=False,
        blocks=[entry, target],
    )

    plan = precise_stackmaps.build_function_stack_map_plan(
        func,
        [],
        target="aarch64-darwin",
    )
    assert plan.function_name == "hash_skew_cfg"


def test_stackmap_pointer_alias_recovers_a_changed_hash_name():
    class ChangingHashText(str):
        def __new__(cls, value: str):
            instance = super().__new__(cls, value)
            instance.hash_calls = 0
            return instance

        def __hash__(self) -> int:
            self.hash_calls += 1
            return super().__hash__() ^ self.hash_calls

    stored_name = ChangingHashText("gc.frame.slots.ptr.5272.8")
    func = ParsedFunction(
        name="hash_skew_alias",
        ret_type=TypeDesc("void"),
        args=[],
        is_global=True,
        is_vararg=False,
        blocks=[],
    )
    aliases = {}
    precise_stackmaps._pointer_alias_set(
        aliases,
        stored_name,
        precise_stackmaps._PointerOrigin("root.addr", 8),
    )

    origin = precise_stackmaps._resolve_pointer(
        func,
        aliases,
        "gc.frame.slots.ptr.5272.8",
    )
    assert origin.base == "root.addr"
    assert origin.offset == 8


def test_stackmap_identity_fields_are_domain_separated_and_validated():
    assert function_id("probe") != safepoint_id("probe", 0, SAFEPOINT_ENTRY)
    assert scoped_stable_id("continuation", "probe") != function_id("probe")
    assert function_id("probe") == function_id("probe")
    # Known lower-63 FNV-1a vectors lock the two-limb implementation used by
    # the self-hosted emitter.  In particular, the first symbol has bit 63 set
    # in ordinary FNV-1a and therefore exercises the signed ABI projection.
    assert function_id("_user_pcc_multi_toy_module_main") == 0x34D094A09262C374
    assert function_id("__pcc_py_module_init_pcc_multi_toy_module") == (
        0x499AA204ABE7BB32
    )
    assert 0 < function_id("probe") <= 0x7FFFFFFFFFFFFFFF
    with pytest.raises(PreciseStackMapError, match="contain no NUL"):
        stable_id("function\0probe")
    with pytest.raises(PreciseStackMapError, match="identity fields"):
        scoped_stable_id("function", "bad\0symbol")


def test_planned_roots_materialize_complete_positional_location_fields():
    locations = _stack_locations(
        (
            PlannedRootLocation(-8, True),
            PlannedRootLocation(-16, False),
        ),
        arch=ARCH_AARCH64,
    )

    assert [location.kind for location in locations] == [
        LOCATION_STACK_INDIRECT,
        LOCATION_STACK_INDIRECT,
    ]
    assert locations[0].flags == LOCATION_MANAGED | LOCATION_OWNED
    assert locations[1].flags == LOCATION_MANAGED
    assert [location.register for location in locations] == [29, 29]
    assert [location.offset for location in locations] == [-8, -16]


def _map(
    arch: int,
    symbol: str,
    *,
    address: int = 0,
    ordinal: int = 0,
) -> PreciseStackMap:
    base = _location(arch)
    derived = StackMapLocation(
        kind=LOCATION_REGISTER,
        flags=(
            LOCATION_MANAGED
            | LOCATION_DERIVED
            | LOCATION_RELOAD_REQUIRED
        ),
        register=9 if arch == ARCH_AARCH64 else 3,
        base_index=0,
    )
    entry = SafepointRecord(
        safepoint_id=safepoint_id(symbol, ordinal, SAFEPOINT_ENTRY),
        instruction_offset=4,
        kind=SAFEPOINT_ENTRY,
        locations=(base,),
    )
    call = SafepointRecord(
        safepoint_id=safepoint_id(symbol, ordinal + 1, SAFEPOINT_CALL),
        instruction_offset=16,
        kind=SAFEPOINT_CALL,
        locations=(base, derived),
        flags=RECORD_HAS_EXCEPTION_EDGE,
        exceptional_offset=24,
    )
    continuation = SafepointRecord(
        safepoint_id=safepoint_id(
            symbol, ordinal + 2, SAFEPOINT_CONTINUATION,
        ),
        instruction_offset=32,
        kind=SAFEPOINT_CONTINUATION,
        locations=(base,),
        flags=RECORD_SUSPENDED,
        exceptional_offset=NO_OFFSET,
        continuation_id=ordinal + 1,
    )
    return PreciseStackMap(
        arch=arch,
        functions=(FunctionStackMap(
            function_id=function_id(symbol),
            function_address=address,
            code_size=48,
            frame_size=32,
            records=(entry, call, continuation),
        ),),
    )


@pytest.mark.parametrize("arch", [ARCH_AARCH64, ARCH_X86_64])
def test_precise_stackmap_v1_round_trips_both_self_targets(arch: int):
    expected = _map(arch, "probe", address=0x1000)
    payload = encode_stack_map(expected, final_image=True)
    assert decode_stack_map(
        payload,
        expected_arch=arch,
        final_image=True,
    ) == expected
    # The first function's address field sits 8 bytes into the first
    # function record, which follows the header.  Derive it rather than
    # hardcoding, so a header change cannot silently drift.
    assert function_address_offsets(payload) == (HEADER_SIZE + 8,)


def test_precise_stackmap_rejects_truncation_trailing_and_wrong_target():
    payload = encode_stack_map(_map(ARCH_AARCH64, "probe"))
    with pytest.raises(PreciseStackMapError, match="truncated"):
        decode_stack_map(payload[:-1])
    with pytest.raises(PreciseStackMapError, match="truncated"):
        validate_stack_map_payload(payload[:-1])
    with pytest.raises(PreciseStackMapError, match="trailing bytes"):
        decode_stack_map(payload + b"x")
    with pytest.raises(PreciseStackMapError, match="trailing bytes"):
        validate_stack_map_payload(payload + b"x")
    with pytest.raises(PreciseStackMapError, match="does not match target"):
        decode_stack_map(payload, expected_arch=ARCH_X86_64)
    with pytest.raises(PreciseStackMapError, match="does not match target"):
        validate_stack_map_payload(payload, expected_arch=ARCH_X86_64)


def test_wire_stackmap_validator_matches_final_decode_semantics():
    first_map = _map(ARCH_AARCH64, "first", address=0x1000)
    second_map = _map(ARCH_AARCH64, "second", address=0x2000)
    value = PreciseStackMap(
        arch=ARCH_AARCH64,
        functions=tuple(sorted(
            first_map.functions + second_map.functions,
            key=lambda function: function.function_id,
        )),
    )
    payload = encode_stack_map(value, final_image=True)
    assert validate_stack_map_payload(
        payload,
        expected_arch=ARCH_AARCH64,
        final_image=True,
    ) is None

    # The two functions share interned location shapes.  Shrinking only the
    # second frame must still validate that function's slice: the raw fast
    # path may reuse a slice only when frame_size is part of the key.
    first = value.functions[0]
    second_function_offset = (
        HEADER_SIZE + FUNCTION_SIZE + len(first.records) * RECORD_SIZE
    )
    too_small = bytearray(payload)
    struct.pack_into("<I", too_small, second_function_offset + 20, 0)
    for validator in (decode_stack_map, validate_stack_map_payload):
        with pytest.raises(
            PreciseStackMapError,
            match="exceeds frame size 0",
        ):
            validator(bytes(too_small), final_image=True)

    raw_pointer = bytearray(payload)
    _count, _functions, table_start, _table_count = (
        wire_stackmaps._scan_stack_map_payload(payload)
    )
    raw_pointer[table_start + 1] = 0
    for validator in (decode_stack_map, validate_stack_map_payload):
        with pytest.raises(PreciseStackMapError, match="raw pointer"):
            validator(bytes(raw_pointer), final_image=True)


def test_precise_stackmap_rejects_duplicate_ids_and_nonfinal_offsets():
    value = _map(ARCH_AARCH64, "probe")
    function = value.functions[0]
    first = function.records[0]
    duplicate = replace(function.records[1], safepoint_id=first.safepoint_id)
    with pytest.raises(PreciseStackMapError, match="duplicate safepoint id"):
        encode_stack_map(replace(
            value,
            functions=(replace(
                function,
                records=(first, duplicate, function.records[2]),
            ),),
        ))
    backwards = replace(function.records[1], instruction_offset=2)
    with pytest.raises(PreciseStackMapError, match="ordered in-function"):
        encode_stack_map(replace(
            value,
            functions=(replace(
                function,
                records=(first, backwards, function.records[2]),
            ),),
        ))
    with pytest.raises(PreciseStackMapError, match="address is unresolved"):
        encode_stack_map(value, final_image=True)
    with pytest.raises(PreciseStackMapError, match="function id is outside uint64"):
        encode_stack_map(replace(
            value,
            functions=(replace(function, function_id=1 << 64),),
        ))


def test_precise_stackmap_rejects_raw_frame_register_and_derived_ambiguity():
    value = _map(ARCH_AARCH64, "probe")
    function = value.functions[0]
    record = function.records[0]

    def rejected(location: StackMapLocation, message: str) -> None:
        changed = replace(
            value,
            functions=(replace(
                function,
                records=(replace(record, locations=(location,)),),
            ),),
        )
        with pytest.raises(PreciseStackMapError, match=message):
            encode_stack_map(changed)

    rejected(replace(_location(ARCH_AARCH64), flags=0), "raw pointer")
    rejected(replace(_location(ARCH_AARCH64), offset=-40), "frame size")
    rejected(StackMapLocation(
        kind=LOCATION_REGISTER,
        flags=LOCATION_MANAGED,
        register=9,
    ), "stale post-safepoint")
    rejected(StackMapLocation(
        kind=LOCATION_REGISTER,
        flags=LOCATION_MANAGED | LOCATION_DERIVED | LOCATION_RELOAD_REQUIRED,
        register=9,
        base_index=0,
    ), "earlier base")


def test_target_assembly_uses_one_byte_abi_and_native_section_names():
    value = _map(ARCH_AARCH64, "probe")
    macho = render_stack_map_assembly(
        value, ("_probe",), target="aarch64-darwin",
    )
    assert macho.startswith(".section __DATA,__pcc_stackmaps,regular\n")
    assert "  .quad _probe" in macho
    elf = render_stack_map_assembly(
        _map(ARCH_X86_64, "probe"),
        ("probe",),
        target="x86_64-linux",
    )
    assert elf.startswith('.section .pcc_stackmaps,"a",@progbits\n')
    assert "  .quad probe" in elf

    sections, undefined = assemble_file(
        ".section __TEXT,__text,regular,pure_instructions\n"
        ".globl _probe\n"
        "_probe:\n"
        "  ret\n"
        + macho
        + "\n.subsections_via_symbols\n"
    )
    assert undefined == []
    stack_section = next(
        section for section in sections
        if (section.segname, section.sectname)
        == ("__DATA", "__pcc_stackmaps")
    )
    assert decode_stack_map(
        stack_section.data,
        expected_arch=ARCH_AARCH64,
    ) == value
    assert len(stack_section.relocations) == 1


def _macho_stackmap_object(
    symbol: str,
    ordinal: int,
    *,
    identity_symbol: str | None = None,
) -> bytes:
    value = _map(
        ARCH_AARCH64,
        identity_symbol or symbol,
        ordinal=ordinal,
    )
    payload = encode_stack_map(value)
    address_offset = function_address_offsets(payload)[0]
    return macho_obj.emit_object([
        macho_obj.Section(
            sectname="__text",
            segname="__TEXT",
            data=b"\xc0\x03\x5f\xd6",
            align_log2=2,
            flags=macho_obj.TEXT_SECTION_FLAGS,
            symbols=(macho_obj.TextSymbol(symbol, 0),),
        ),
        macho_obj.Section(
            sectname="__pcc_stackmaps",
            segname="__DATA",
            data=payload,
            align_log2=3,
            flags=macho_obj.PCC_STACKMAP_SECTION_FLAGS,
            relocations=(macho_obj.Relocation(
                offset=address_offset,
                symbol=symbol,
                type=spec.ARM64_RELOC_UNSIGNED,
                pcrel=False,
                length=3,
            ),),
        ),
    ])


def test_macho_stackmap_object_requires_exact_function_relocations():
    value = _map(ARCH_AARCH64, "_probe")
    payload = encode_stack_map(value)
    section = macho_obj.Section(
        sectname="__pcc_stackmaps",
        segname="__DATA",
        data=payload,
        align_log2=3,
        flags=macho_obj.PCC_STACKMAP_SECTION_FLAGS,
    )
    with pytest.raises(macho_obj.MachOEmitError, match="exactly one relocation"):
        macho_obj.emit_object([
            macho_obj.Section(
                sectname="__text",
                segname="__TEXT",
                data=b"\xc0\x03\x5f\xd6",
                flags=macho_obj.TEXT_SECTION_FLAGS,
                symbols=(macho_obj.TextSymbol("_probe", 0),),
            ),
            section,
        ])


def test_macho_relocatable_link_semantically_merges_stackmap_tables():
    merged = spec.parse_object(link_relocatable([
        _macho_stackmap_object("_alpha", 0),
        _macho_stackmap_object("_beta", 10),
    ]))
    section = next(
        section for section in merged.sections()
        if (
            section["segname_str"], section["sectname_str"]
        ) == ("__DATA", "__pcc_stackmaps")
    )
    payload = bytes(
        merged.data[section["offset"]:section["offset"] + section["size"]]
    )
    decoded = decode_stack_map(payload, expected_arch=ARCH_AARCH64)
    assert len(decoded.functions) == 2
    assert [
        function.function_id for function in decoded.functions
    ] == sorted((function_id("_alpha"), function_id("_beta")))
    assert len(merged.relocations(section)) == 2


def test_macho_relocatable_link_rejects_duplicate_stackmap_function_id():
    with pytest.raises(LinkError, match="duplicate stable function id"):
        link_relocatable([
            _macho_stackmap_object("_alpha", 0, identity_symbol="_same"),
            _macho_stackmap_object("_beta", 10, identity_symbol="_same"),
        ])


def _target_final_stackmap_ir(triple: str) -> str:
    return f'''
target triple = "{triple}"

@frame_map = internal constant i32 1

declare void @pcc_gc_frame_enter(ptr, ptr)
declare void @pcc_gc_frame_leave(ptr)
declare void @pcc_thread_safepoint()
declare void @opaque_call(ptr)
declare ptr @py_continuation_new_typed(ptr, ptr, ptr)
declare i64 @py_err_occurred()
declare void @resume()

define i64 @probe(ptr %obj, i64 %limit) {{
entry:
  %root = alloca ptr, align 8
  store ptr %obj, ptr %root, align 8
  call void @pcc_gc_frame_enter(ptr @frame_map, ptr %root)
  call void @opaque_call(ptr %obj)
  br label %loop

loop:
  %i = phi i64 [ 0, %entry ], [ %next, %body ]
  call void @pcc_thread_safepoint()
  %cont = call ptr @py_continuation_new_typed(ptr @frame_map, ptr %root, ptr @resume)
  %err = call i64 @py_err_occurred()
  %failed = icmp ne i64 %err, 0
  br i1 %failed, label %failure, label %body

body:
  %next = add i64 %i, 1
  %again = icmp slt i64 %next, %limit
  br i1 %again, label %loop, label %done

failure:
  call void @pcc_gc_frame_leave(ptr %root)
  ret i64 -1

done:
  call void @pcc_gc_frame_leave(ptr %root)
  ret i64 %i
}}
'''.strip()


@pytest.mark.parametrize(
    ("triple", "target"),
    [
        ("arm64-apple-darwin23.6.0", "aarch64-darwin"),
        ("x86_64-unknown-linux-gnu", "x86_64-linux"),
    ],
)
def test_target_final_planner_maps_only_explicit_registered_stack_roots(
    triple: str, target: str
):
    prepared = prepare_module_for_target(
        _target_final_stackmap_ir(triple),
        aggregate_returned_indirect=lambda _ty: False,
    )
    plans = build_stack_map_plans(
        prepared.functions,
        prepared.globals_,
        target=target,
    )
    assert len(plans) == 1
    plan = plans[0]
    records = plan.diagnostic_records()
    assert {record.kind for record in records} == {
        SAFEPOINT_ENTRY,
        SAFEPOINT_LOOP,
        SAFEPOINT_CALL,
        SAFEPOINT_EXCEPTION,
        SAFEPOINT_CONTINUATION,
    }
    entry = next(
        record for record in records if record.kind == SAFEPOINT_ENTRY
    )
    assert entry.locations == ()
    rooted = [record for record in records if record.kind != SAFEPOINT_ENTRY]
    assert rooted
    assert all(len(record.locations) == 1 for record in rooted)
    assert all(record.locations[0].owned for record in rooted)
    assert all(record.locations[0].offset < 0 for record in rooted)
    exception = next(
        record for record in records if record.kind == SAFEPOINT_EXCEPTION
    )
    assert exception.exceptional_block == "failure"
    assert exception.flags == RECORD_HAS_EXCEPTION_EDGE


def test_aarch64_emitter_finalizes_stackmap_after_machine_peepholes():
    assembly = emit_aarch64_darwin_asm(
        _target_final_stackmap_ir("arm64-apple-darwin23.6.0")
    )
    assert ".section __DATA,__pcc_stackmaps,regular" in assembly
    sections, _undefined = assemble_file(assembly)
    section = next(
        item for item in sections
        if (item.segname, item.sectname) == ("__DATA", "__pcc_stackmaps")
    )
    decoded = decode_stack_map(
        section.data,
        expected_arch=ARCH_AARCH64,
    )
    assert len(decoded.functions) == 1
    function = decoded.functions[0]
    assert function.frame_size % 16 == 0
    assert [record.instruction_offset for record in function.records] == sorted(
        record.instruction_offset for record in function.records
    )
    assert {record.kind for record in function.records} == {
        SAFEPOINT_ENTRY,
        SAFEPOINT_LOOP,
        SAFEPOINT_CALL,
        SAFEPOINT_EXCEPTION,
        SAFEPOINT_CONTINUATION,
    }
    assert len(section.relocations) == 1


def test_aarch64_loop_safepoint_keeps_distinct_pc_after_fallthrough_call():
    assembly = emit_aarch64_darwin_asm(r'''
target triple = "arm64-apple-darwin23.6.0"

declare void @opaque_call()

define void @probe(i1 %take_call, i64 %limit) {
entry:
  br label %loop

loop:
  %index = phi i64 [ 0, %entry ], [ 0, %latch ]
  %again = icmp slt i64 %index, %limit
  br i1 %again, label %dispatch, label %done

dispatch:
  br i1 %take_call, label %call, label %latch

call:
  call void @opaque_call()
  br label %latch

latch:
  br label %loop

done:
  ret void
}
'''.strip())
    sections, _undefined = assemble_file(assembly)
    section = next(
        item for item in sections
        if (item.segname, item.sectname) == ("__DATA", "__pcc_stackmaps")
    )
    function = decode_stack_map(
        section.data,
        expected_arch=ARCH_AARCH64,
    ).functions[0]
    call = next(record for record in function.records if record.kind == SAFEPOINT_CALL)
    loop = next(record for record in function.records if record.kind == SAFEPOINT_LOOP)
    assert call.instruction_offset < loop.instruction_offset
    assert loop.instruction_offset - call.instruction_offset == 4


def test_x86_emitter_delegates_variable_length_pc_finalization_to_assembler():
    assembly = emit_x86_64_linux_asm(
        _target_final_stackmap_ir("x86_64-unknown-linux-gnu")
    )
    assert '.section .pcc_stackmaps,"a",@progbits' in assembly
    assert "  .quad probe" in assembly
    assert ".Lpcc_smap_end_" in assembly
    assert " - probe" in assembly
    assert f"  .byte {SAFEPOINT_CONTINUATION}" in assembly
    assert f"  .byte {SAFEPOINT_EXCEPTION}" in assembly


def _stale_managed_ssa_ir(triple: str, *, ambiguous: bool = False) -> str:
    selected = (
        "  %selected = select i1 %pick, ptr %derived, ptr %raw\n"
        if ambiguous
        else "  %selected = getelementptr i8, ptr %derived, i64 0\n"
    )
    return f'''
target triple = "{triple}"

@frame_map = internal constant i32 1

declare void @pcc_gc_frame_enter(ptr, ptr)
declare void @pcc_gc_frame_leave(ptr)
declare void @pcc_thread_safepoint()
declare void @opaque_call(ptr)

define void @refresh(ptr %obj, ptr %raw, i1 %pick) {{
entry:
  %root = alloca ptr, align 8
  store ptr %obj, ptr %root, align 8
  call void @pcc_gc_frame_enter(ptr @frame_map, ptr %root)
  %before = load ptr, ptr %root, align 8
  %derived = getelementptr i8, ptr %before, i64 24
{selected.rstrip()}
  call void @pcc_thread_safepoint()
  call void @opaque_call(ptr %selected)
  call void @pcc_gc_frame_leave(ptr %root)
  ret void
}}
'''.strip()


@pytest.mark.parametrize(
    ("triple", "target"),
    [
        ("arm64-apple-darwin23.6.0", "aarch64-darwin"),
        ("x86_64-unknown-linux-gnu", "x86_64-linux"),
    ],
)
def test_safepoint_reloads_live_root_derived_ssa_from_rewritten_slot(
    triple: str, target: str
):
    prepared = prepare_module_for_target(
        _stale_managed_ssa_ir(triple),
        aggregate_returned_indirect=lambda _ty: False,
    )
    plan = build_stack_map_plans(
        prepared.functions, prepared.globals_, target=target,
    )[0]
    safepoint = next(
        record
        for record in plan.diagnostic_records()
        if record.kind == SAFEPOINT_LOOP
    )
    assert len(safepoint.locations) == 1
    assert len(safepoint.reloads) == 1
    reload = safepoint.reloads[0]
    assert reload.source_offset == safepoint.locations[0].offset
    assert reload.destination_offset < 0
    assert reload.destination_offset != reload.source_offset
    assert reload.derived_offset == 24


def test_structured_parsed_aarch64_reloads_keep_final_instruction_order():
    from pcc.backend.arm64_asm_driver import assemble_file, assemble_lines
    from pcc.backend.self_backend_aarch64_darwin import (
        emit_aarch64_darwin_indexed_transport,
    )
    from pcc.backend.self_backend_parse import parse_self_backend_module

    source = _stale_managed_ssa_ir("arm64-apple-darwin23.6.0")
    expected = assemble_file(emit_aarch64_darwin_asm(source, optimize=False))
    transport = emit_aarch64_darwin_indexed_transport(
        parse_self_backend_module(source), optimize=False,
    )
    try:
        assert transport.direct_instruction_count > 0
        assert assemble_lines(
            transport.line_chunks, transport.structured_sections,
            transport.encoded_line_records, transport.structured_symbol_names,
        ) == expected
    finally:
        assert transport.native_finalized
        assert transport.encoded_line_records is None


def test_both_target_emitters_refresh_live_managed_ssa_after_safepoint():
    aarch64 = emit_aarch64_darwin_asm(
        _stale_managed_ssa_ir("arm64-apple-darwin23.6.0")
    )
    aarch64_after_safepoint = aarch64.split(
        "bl _pcc_thread_safepoint", 1
    )[1].split("bl _opaque_call", 1)[0]
    assert "x16" in aarch64_after_safepoint
    assert "#24" in aarch64_after_safepoint

    x86 = emit_x86_64_linux_asm(
        _stale_managed_ssa_ir("x86_64-unknown-linux-gnu")
    )
    x86_after_safepoint = x86.split(
        "call pcc_thread_safepoint", 1
    )[1].split("call opaque_call", 1)[0]
    assert "mov r11, QWORD PTR [rbp - " in x86_after_safepoint
    assert "add r11, 24" in x86_after_safepoint
    assert "mov QWORD PTR [rbp - " in x86_after_safepoint


@pytest.mark.parametrize(
    ("triple", "emitter"),
    [
        ("arm64-apple-darwin23.6.0", emit_aarch64_darwin_asm),
        ("x86_64-unknown-linux-gnu", emit_x86_64_linux_asm),
    ],
)
def test_stackmap_planner_rejects_live_managed_raw_pointer_join(
    triple: str, emitter
):
    with pytest.raises(
        BackendUnavailable, match="ambiguous root provenance"
    ):
        emitter(_stale_managed_ssa_ir(triple, ambiguous=True))


def test_stackmap_planner_rejects_unclassified_stack_pointer_selection():
    ir_text = '''
target triple = "arm64-apple-darwin23.6.0"
@frame_map = internal constant i32 1
declare void @pcc_gc_frame_enter(ptr, ptr)
declare void @pcc_gc_frame_leave(ptr)

define void @ambiguous(i1 %pick) {
entry:
  %left = alloca ptr, align 8
  %right = alloca ptr, align 8
  %slot = select i1 %pick, ptr %left, ptr %right
  call void @pcc_gc_frame_enter(ptr @frame_map, ptr %slot)
  call void @pcc_gc_frame_leave(ptr %slot)
  ret void
}
'''.strip()
    with pytest.raises(BackendUnavailable, match="cannot be resolved"):
        emit_aarch64_darwin_asm(ir_text)


def test_stackmap_planner_ignores_persistent_global_registry_roots_at_join():
    ir_text = '''
target triple = "arm64-apple-darwin23.6.0"
@frame_map = internal constant i32 1
@module_root = global ptr null
@module_initialized = global i1 false
declare void @pcc_gc_frame_enter(ptr, ptr)

define void @module_top() {
entry:
  %seen = load i1, ptr @module_initialized
  br i1 %seen, label %done, label %body

body:
  store i1 true, ptr @module_initialized
  call void @pcc_gc_frame_enter(ptr @frame_map, ptr @module_root)
  br label %done

done:
  ret void
}
'''.strip()
    assembly = emit_aarch64_darwin_asm(ir_text)
    assert "_module_top:" in assembly


def _elf_stackmap_object() -> elf_x86_64.ElfObject:
    value = _map(ARCH_X86_64, "_start")
    payload = encode_stack_map(value)
    address_offset = function_address_offsets(payload)[0]
    return elf_x86_64.ElfObject(
        sections=(
            elf_x86_64.ElfSection(
                ".text",
                elf_x86_64.SHT_PROGBITS,
                elf_x86_64.SHF_ALLOC | elf_x86_64.SHF_EXECINSTR,
                16,
                b"\x90" * 47 + b"\xc3",
            ),
            elf_x86_64.ElfSection(
                ".pcc_stackmaps",
                elf_x86_64.SHT_PROGBITS,
                elf_x86_64.SHF_ALLOC,
                8,
                payload,
                relocations=(elf_x86_64.ElfRelocation(
                    address_offset,
                    1,
                    elf_x86_64.R_X86_64_64,
                ),),
            ),
        ),
        symbols=(
            elf_x86_64.ElfSymbol.null(),
            elf_x86_64.ElfSymbol(
                "_start",
                1,
                0,
                48,
                elf_x86_64.STB_GLOBAL,
                elf_x86_64.STT_FUNC,
            ),
        ),
    )


def test_owned_elf_object_and_static_link_validate_stackmap_publication():
    obj = _elf_stackmap_object()
    encoded = elf_x86_64.emit_relocatable(obj)
    reparsed = elf_x86_64.parse_relocatable(encoded)
    assert next(
        section for section in reparsed.sections
        if section.name == ".pcc_stackmaps"
    ).relocations[0].type == elf_x86_64.R_X86_64_64
    executable = elf_x86_64.link_static_executable([reparsed], entry="_start")
    assert elf_x86_64.parse_static_executable(executable)["entry"] != 0


def test_owned_elf_stackmap_rejects_missing_function_relocation():
    obj = _elf_stackmap_object()
    stackmap = obj.sections[1]
    with pytest.raises(
        elf_x86_64.ElfError,
        match="exactly one relocation",
    ):
        elf_x86_64.emit_relocatable(replace(
            obj,
            sections=(obj.sections[0], replace(stackmap, relocations=())),
        ))


def test_stable_id_prefix_streaming_matches_one_shot():
    """The streaming split must stay bit-identical to the one-shot hash.

    The string-based `stable_id_resume` is not on the hot path: concatenating
    and encoding each suffix was a large pcc1 loss.  The compiler instead
    passes two 32-bit prefix limbs and feeds decimal digits numerically.  Pin
    both continuations to the public one-shot identity.
    """
    from pcc.backend.precise_stackmap import (
        SAFEPOINT_KINDS,
        safepoint_id,
        safepoint_id_from_prefix_limbs,
        stable_id_prefix_limb,
        stable_id_prefix_state,
        stable_id_resume,
    )

    symbols = (
        "_main",
        "user_pcc_backend_self_backend_precise_stackmaps___nested_add_record",
        "s",
        "x" * 200,
        "sym.with.dots$and-dashes",
    )
    for symbol in symbols:
        state = stable_id_prefix_state("safepoint", symbol)
        high = state >> 32
        low = state & 0xFFFFFFFF
        assert stable_id_prefix_limb("safepoint", symbol, True) == high
        assert stable_id_prefix_limb("safepoint", symbol, False) == low
        for ordinal in (0, 1, 7, 38539):
            for kind in sorted(SAFEPOINT_KINDS):
                assert stable_id_resume(state, str(ordinal), str(kind)) == (
                    safepoint_id(symbol, ordinal, kind)
                )
                assert safepoint_id_from_prefix_limbs(
                    high, low, ordinal, kind
                ) == safepoint_id(symbol, ordinal, kind)


def test_location_dedup_uses_identity_before_equality():
    """The consecutive-record fast path must not depend on dataclass `==`.

    `==` on a tuple of frozen dataclasses answers False under pcc1 even when
    the contents are equal (probed directly: host True, pcc1 False). This
    emitter is compiled into the self-host closure, so relying on `==` alone
    silently disabled the fast path there and made every record rebuild and
    hash a key string ~2 kB long -- and pcc never caches a str hash. Measured
    on one 18 MB module: 31946 of 38540 consecutive records share the interned
    tuple object, so `is` recovers the fast path; a further 111 are
    distinct-but-equal, which is why `==` has to stay as the fallback.
    """
    source = Path(
        precise_stackmaps.__file__
    ).read_text(encoding="utf-8")
    marker = "candidate[0] is locations"
    assert marker in source, (
        "the consecutive-record fast path must test identity first; "
        "`==` alone is a no-op under pcc1"
    )
    identity_at = source.index(marker)
    equality_at = source.index("content_key in self.location_content_index")
    assert identity_at < equality_at, (
        "identity must be tested before equality so pcc1 short-circuits "
        "before building the key string"
    )


def test_interned_locations_pin_their_key_objects():
    """An id()-keyed memo must keep the keyed objects alive.

    A freed `_RootGroup` whose address is reused makes a stale fingerprint HIT
    and return an unrelated root set, which is a wrong stack map rather than a
    lost optimization. This is what left pcc1 unable to compile any program
    containing a function definition.
    """
    source = Path(
        precise_stackmaps.__file__
    ).read_text(encoding="utf-8")
    assert "(_locations(active_groups), active_groups)" in source, (
        "each interned-locations entry must store the groups it was keyed on "
        "so their id() cannot be recycled while the entry is alive"
    )


def test_main_planner_materializes_mutable_root_state_only_for_protocol_blocks():
    """Ordinary blocks must consume their shared immutable entry state.

    Item311 has 9,474 reachable blocks and 1,626,262 entry-state group
    references, but only 1,278 blocks execute frame enter/leave. Rebuilding a
    string-keyed dict for every block duplicates those references without a
    mutation consumer.
    """

    source = Path(precise_stackmaps.__file__).read_text(encoding="utf-8")
    assert "active_groups = entry_state" in source
    assert "active = {group.key: group for group in entry_state}" not in source
    assert "if call_header.third & CALL_FLAG_FRAME_PROTOCOL:" in source
    assert "active_groups = tuple(active.values())" in source


# ---------------------------------------------------------------------------
# A frame the runtime registry owns, not this function's machine frame.
#
# ``pcc_gc_frame_enter`` is public runtime API reachable from ``pcc.extern``,
# and the GC probes under ``tests/python/test_gc_backend_*.py`` use it that
# way: they ``malloc`` a frame map and a slots array and register them by
# hand.  The precise stack-map analysis read the frame map's root count before
# it had established whose frame this was, so a run-time-computed map hit
# ``frame map ... is not one direct global`` and the whole program failed to
# compile.  Both the slot-group builder and the frame-leave walker already
# declined to model registry-owned slot arrays; only the enter path did not.
#
# The compiler's own frame maps are always internal constant globals built by
# ``ownership_lowering._gc_one_slot_frame_map``, so keying the decision on the
# map keeps every compiler-emitted frame on the unchanged fail-closed path.
# ``test_..._still_fails_closed`` is the half that locks that down.
# ---------------------------------------------------------------------------

_STACKMAP_TARGETS = ("aarch64-darwin", "x86_64-linux")

_SM_PTR = TypeDesc(kind="ptr")
_SM_I64 = TypeDesc(kind="int", width=64)
_SM_VOID = TypeDesc(kind="void")


def _sm_call(callee, args, dest=None, ret_type=None):
    return ParsedInstr(
        "call",
        (
            dest,
            _SM_VOID if ret_type is None else ret_type,
            callee,
            False,
            tuple(args),
            len(args),
            False,
            tuple(0 for _ in args),
        ),
    )


def _sm_frame_function(name, frame_map_operand, *, with_leave=True):
    instructions = [
        _sm_call("malloc", ((_SM_I64, "4"),), dest="%heap.map", ret_type=_SM_PTR),
        _sm_call("malloc", ((_SM_I64, "8"),), dest="%heap.slots", ret_type=_SM_PTR),
        _sm_call(
            "pcc_gc_frame_enter",
            ((_SM_PTR, frame_map_operand), (_SM_PTR, "%heap.slots")),
        ),
    ]
    if with_leave:
        instructions.append(
            _sm_call("pcc_gc_frame_leave", ((_SM_PTR, "%heap.slots"),))
        )
    return ParsedFunction(
        name=name,
        ret_type=_SM_VOID,
        args=[],
        is_global=True,
        is_vararg=False,
        blocks=[
            ParsedBlock(
                name="entry",
                instructions=instructions,
                terminator=ParsedInstr("ret", (_SM_VOID, "")),
            )
        ],
    )


_SM_FRAME_MAP_GLOBAL = GlobalDef(
    name=".pcc.gc.frame.map.1",
    type=TypeDesc(kind="int", width=32),
    initializer="1",
    is_constant=True,
    is_internal=True,
)


@pytest.mark.parametrize("target", _STACKMAP_TARGETS)
def test_a_heap_frame_map_is_registry_owned_and_does_not_stop_the_build(target):
    func = _sm_frame_function("user_registers_its_own_frame", "%heap.map")
    plan = precise_stackmaps.build_function_stack_map_plan(
        func,
        [_SM_FRAME_MAP_GLOBAL],
        target=target,
    )
    assert plan.function_name == "user_registers_its_own_frame"
    # Registry-owned roots are not locations in this machine frame, so the
    # plan must claim none of them rather than inventing an offset.
    for record in plan.records:
        assert record.locations == ()


@pytest.mark.parametrize("target", _STACKMAP_TARGETS)
def test_a_compiler_shaped_frame_with_an_untraceable_slots_pointer_still_fails_closed(
    target,
):
    """The half that must not become permissive.

    A global frame map means the compiler emitted this frame, so its slots
    array is an alloca by construction.  If the alias resolver cannot get
    there, the analysis has lost roots and must say so -- silently ignoring it
    would drop them from the stack map and let a moving collector miss them.
    """

    func = _sm_frame_function(
        "compiler_frame_with_lost_slots",
        "@.pcc.gc.frame.map.1",
        with_leave=False,
    )
    with pytest.raises(BackendUnavailable, match="stack alloca"):
        precise_stackmaps.build_function_stack_map_plan(
            func,
            [_SM_FRAME_MAP_GLOBAL],
            target=target,
        )
