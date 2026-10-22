"""Packed Linux metadata must preserve the text assembler's complete object."""

from dataclasses import replace
import platform
import subprocess
import sys

import pytest

from pcc.backend import BackendUnavailable
from pcc.backend import elf_x86_64 as elf
from pcc.backend import self_backend_precise_stackmaps as maps
from pcc.backend import self_backend_x86_64_linux as emitter
from pcc.backend import x86_64_asm_driver as assembler
from pcc.backend.precise_stackmap import (
    ARCH_X86_64, PreciseStackMapError, RECORD_HAS_EXCEPTION_EDGE,
    RECORD_SUSPENDED, SAFEPOINT_CALL, SAFEPOINT_CONTINUATION, SAFEPOINT_ENTRY,
    SAFEPOINT_EXCEPTION, SAFEPOINT_LOOP, decode_stack_map, function_id,
)
from pcc.backend.self_backend_parse import parse_self_backend_module
from pcc.backend.target_objects import emit_indexed_assembly, encode_assembly_object
from tests.python.test_precise_stackmap_abi import _target_final_stackmap_ir


# Checks the serialized precise stack-map table, which objects carry only on request.
pytestmark = pytest.mark.usefixtures("precise_stackmap_tables")


TARGET = "x86_64-unknown-linux-gnu"
MARKER = '.section .pcc_stackmaps,"a",@progbits\n.p2align 3\n'
NOTE = '.section .note.GNU-stack,"",@progbits\n'


def _symbol(name):
    return name


def _block(function, name):
    return ".L" + function + "_" + name


def _fixture():
    roots = (maps.PlannedRootLocation(-8, True), maps.PlannedRootLocation(-16, False))
    records = (
        maps.PlannedSafepoint(0xFFFFFFFFFFFFFFFF, ".Lprobe_entry", SAFEPOINT_ENTRY, ()),
        maps.PlannedSafepoint(2, ".Lprobe_call", SAFEPOINT_CALL, roots),
        maps.PlannedSafepoint(3, ".Lprobe_exception", SAFEPOINT_EXCEPTION, roots,
                             RECORD_HAS_EXCEPTION_EDGE, "failure"),
        maps.PlannedSafepoint(4, ".Lprobe_continuation", SAFEPOINT_CONTINUATION,
                             roots, RECORD_SUSPENDED, "", 0xFFFFFFFF),
        maps.PlannedSafepoint(5, ".Lprobe_loop", SAFEPOINT_LOOP, roots),
    )
    plan = maps.FunctionStackMapPlan(
        "probe", function_id("probe"), 32, ".Lprobe_end", tuple(reversed(records)),
        (), (), (), "x86_64-linux",
    )
    assembly = '''.intel_syntax noprefix
.text
.globl probe
.type probe, @function
.p2align 4
probe:
.Lprobe_entry:
  nop
.Lprobe_call:
  mov rax, 1234567890123
.Lprobe_exception:
  add rax, 7
.Lprobe_continuation:
  nop
.Lprobe_loop:
  nop
.Lprobe_failure:
  ret
.Lprobe_end:
.size probe, .-probe
'''
    return assembly, (plan,)


def _assemble_pair(assembly, plans):
    rendered = maps.render_x86_64_stack_map_section(
        assembly.splitlines(), plans, function_symbol=_symbol, block_label=_block,
    )
    oracle = assembler.assemble_file(assembly + "\n".join(rendered) + "\n" + NOTE)
    packed = assembler.assemble_file_with_stack_maps(
        assembly + MARKER + NOTE, plans,
        function_symbol=_symbol, block_label=_block,
    )
    return oracle, packed


@pytest.mark.parametrize("ir", [
    _target_final_stackmap_ir(TARGET),
    f'target triple = "{TARGET}"\ndefine i64 @rax() {{\nentry:\n ret i64 42\n}}',
    f'target triple = "{TARGET}"\n@only_data = global i64 42',
])
def test_production_packed_object_matches_public_text_bytes(ir, monkeypatch):
    oracle = encode_assembly_object(emitter.emit_x86_64_linux_asm(ir), TARGET)
    plans = []

    def forbidden(*_args, **_kwargs):
        raise AssertionError("packed object route projected textual stack maps")

    monkeypatch.setattr(emitter, "render_x86_64_stack_map_section", forbidden)
    monkeypatch.setattr(maps, "_stack_locations", forbidden)
    assembly = emit_indexed_assembly(
        parse_self_backend_module(ir), stack_map_plans_out=plans,
    )
    encoded = encode_assembly_object(assembly, TARGET, stack_map_plans=plans)
    assert encoded == oracle
    if plans:
        assert assembly.split(MARKER, 1)[1] == NOTE
        obj = elf.parse_relocatable(encoded)
        section = next(section for section in obj.sections if section.name == ".pcc_stackmaps")
        decoded = decode_stack_map(section.data, expected_arch=ARCH_X86_64)
        assert len(decoded.functions) == len(plans)
        assert len(section.relocations) == len(plans)
        assert all(rel.type == elf.R_X86_64_64 and rel.addend == 0 for rel in section.relocations)


def test_all_record_kinds_shared_roots_high_uint64_and_local_labels_match():
    assembly, plans = _fixture()
    oracle, packed = _assemble_pair(assembly, plans)
    assert packed == oracle
    assert elf.emit_relocatable(packed) == elf.emit_relocatable(oracle)
    assert all(not symbol.name.startswith(".L") for symbol in packed.symbols)
    section = next(section for section in packed.sections if section.name == ".pcc_stackmaps")
    decoded = decode_stack_map(section.data, expected_arch=ARCH_X86_64)
    records = decoded.functions[0].records
    assert {record.kind for record in records} == {
        SAFEPOINT_CALL, SAFEPOINT_CONTINUATION, SAFEPOINT_ENTRY,
        SAFEPOINT_EXCEPTION, SAFEPOINT_LOOP,
    }
    assert records[0].safepoint_id == 0xFFFFFFFFFFFFFFFF
    assert records[3].continuation_id == 0xFFFFFFFF
    assert records[1].locations == records[2].locations == records[3].locations
    assert [root.offset for root in records[1].locations] == [-8, -16]
    assert records[1].instruction_offset == 1
    assert records[2].instruction_offset > records[1].instruction_offset + 1


def test_cross_function_root_interning_keeps_each_frame_validation():
    assembly, plans = _fixture()
    plan = plans[0]
    second_records = tuple(replace(
        record,
        safepoint_id=record.safepoint_id - 1 if record.kind == SAFEPOINT_ENTRY else record.safepoint_id + 10,
        label=record.label.replace("probe", "other"),
        locations=tuple(maps.PlannedRootLocation(root.offset, root.owned) for root in record.locations),
    ) for record in plan.records)
    second = replace(plan, function_name="other", function_id=function_id("other"),
                     end_label=".Lother_end", records=second_records)
    other_assembly = assembly.split(".text\n", 1)[1].replace("probe", "other")
    assembly += other_assembly
    oracle, packed = _assemble_pair(assembly, (plan, second))
    assert elf.emit_relocatable(packed) == elf.emit_relocatable(oracle)
    section = next(section for section in packed.sections if section.name == ".pcc_stackmaps")
    assert maps._STACK_MAP_HEADER_CODEC.unpack_from(section.data)[5] == 2
    # The same interned root set must still be rejected in a smaller frame.
    with pytest.raises(elf.ElfError, match="exceeds frame size"):
        assembler.assemble_file_with_stack_maps(
            assembly + MARKER + NOTE, (plan, replace(second, frame_size=0)),
            function_symbol=_symbol, block_label=_block,
        )


@pytest.mark.parametrize("field,value", [
    ("safepoint_id", -1), ("safepoint_id", 1 << 64), ("safepoint_id", 0),
    ("kind", 256), ("kind", 99), ("flags", 256), ("flags", 128),
    ("flags", RECORD_SUSPENDED), ("continuation_id", -1),
    ("continuation_id", 1 << 32), ("continuation_id", 1),
])
def test_packed_fields_fail_closed_before_wrapping(field, value):
    assembly, plans = _fixture()
    plan = plans[0]
    records = tuple(replace(record, **{field: value}) if record.kind == SAFEPOINT_CALL else record
                    for record in plan.records)
    with pytest.raises((PreciseStackMapError, elf.ElfError)):
        assembler.assemble_file_with_stack_maps(
            assembly + MARKER + NOTE, (replace(plan, records=records),),
            function_symbol=_symbol, block_label=_block,
        )


@pytest.mark.parametrize("field,value", [
    ("function_id", -1), ("function_id", 1 << 64),
    ("frame_size", -16), ("frame_size", 1 << 32), ("frame_size", 24),
])
def test_packed_function_fields_fail_closed(field, value):
    assembly, plans = _fixture()
    with pytest.raises((PreciseStackMapError, elf.ElfError)):
        assembler.assemble_file_with_stack_maps(
            assembly + MARKER + NOTE, (replace(plans[0], **{field: value}),),
            function_symbol=_symbol, block_label=_block,
        )


@pytest.mark.parametrize("offset", [True, 0, -1, -40, -(1 << 31) - 1, 1 << 31])
def test_packed_roots_preserve_signedness_alignment_and_frame_bounds(offset):
    assembly, plans = _fixture()
    plan = plans[0]
    records = tuple(replace(record, locations=(maps.PlannedRootLocation(offset, True),))
                    if record.kind == SAFEPOINT_CALL else record for record in plan.records)
    with pytest.raises((PreciseStackMapError, elf.ElfError)):
        assembler.assemble_file_with_stack_maps(
            assembly + MARKER + NOTE, (replace(plan, records=records),),
            function_symbol=_symbol, block_label=_block,
        )


@pytest.mark.parametrize("label", ["probe", ".Lprobe_end", ".Lprobe_call", ".Lprobe_failure"])
def test_packed_path_rejects_missing_final_labels(label):
    assembly, plans = _fixture()
    assembly = assembly.replace(label + ":\n", "")
    with pytest.raises((BackendUnavailable, assembler.X86EncodeError)):
        assembler.assemble_file_with_stack_maps(
            assembly + MARKER + NOTE, plans, function_symbol=_symbol, block_label=_block,
        )


def test_packed_path_rejects_duplicate_or_outside_function_pc():
    assembly, plans = _fixture()
    plan = plans[0]
    for label in (".Lprobe_entry", ".Lprobe_end"):
        records = tuple(replace(record, label=label) if record.kind == SAFEPOINT_CALL else record
                        for record in plan.records)
        with pytest.raises(elf.ElfError, match="ordered in-function"):
            assembler.assemble_file_with_stack_maps(
                assembly + MARKER + NOTE, (replace(plan, records=records),),
                function_symbol=_symbol, block_label=_block,
            )


def test_packed_path_rejects_nonempty_section_marker():
    assembly, plans = _fixture()
    with pytest.raises(assembler.X86EncodeError, match="empty section marker"):
        assembler.assemble_file_with_stack_maps(
            assembly + MARKER + ".byte 1\n" + NOTE, plans,
            function_symbol=_symbol, block_label=_block,
        )


def test_sidecar_pco_selects_packed_path_and_asm_keeps_text(tmp_path, monkeypatch):
    from pcc.backend.self_backend_indexed_codec import encode_indexed_module_file
    from pcc.backend.self_backend_indexed_emit import emit_indexed_module_file

    ir = _target_final_stackmap_ir(TARGET)
    sidecar = tmp_path / "probe.pidx"
    encode_indexed_module_file(str(sidecar), parse_self_backend_module(ir))
    expected = encode_assembly_object(emitter.emit_x86_64_linux_asm(ir), TARGET)
    output = tmp_path / "probe.pco"
    with monkeypatch.context() as context:
        def forbidden(*_args, **_kwargs):
            raise AssertionError("sidecar PCO used textual stack-map renderer")
        context.setattr(emitter, "render_x86_64_stack_map_section", forbidden)
        emit_indexed_module_file(str(sidecar), str(output), "PCO")
    assert output.read_bytes() == expected
    asm = tmp_path / "probe.s"
    emit_indexed_module_file(str(sidecar), str(asm), "ASM")
    assert " - probe" in asm.read_text()


def test_frontend_pco_selects_packed_path_after_releasing_ir(tmp_path, monkeypatch):
    from pcc.frontends.python import pipeline
    from tests.python.test_direct_indexed_worker_validation import _validation_worker

    observed = []
    encode = assembler.assemble_builder

    # The worker streams the module into its sections; the packed maps reach
    # the owned packer after an empty stack-map section and the closing note,
    # where the text route places them.
    def traced(builder, keep_labels, plans, **kwargs):
        stack_maps = builder.sections.get(".pcc_stackmaps")
        observed.append((len(plans), tuple(builder.order[-2:]),
                         None if stack_maps is None else stack_maps.size, tuple(keep_labels)))
        return encode(builder, keep_labels, plans, **kwargs)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("frontend PCO used textual stack-map renderer")

    with monkeypatch.context() as context:
        manifest, result = _validation_worker(tmp_path, context, TARGET, False, False)
        context.setattr(emitter, "render_x86_64_stack_map_section", forbidden)
        context.setattr(assembler, "assemble_builder", traced)
        assert pipeline.run_python_multi_codegen_worker(str(manifest)) == 0, result.read_text()
    assert observed and observed[0][0] > 0
    assert observed[0][1:] == ((".pcc_stackmaps", ".note.GNU-stack"), 0, ())
    packed = (tmp_path / "module_0.direct.pco").read_bytes()
    # The production worker deliberately does not serialize canonical IR.
    # Rerun the same source/path with explicit text-control publication instead
    # of treating its intentionally empty .ll artifact as an oracle input.
    with monkeypatch.context() as context:
        manifest, result = _validation_worker(tmp_path, context, TARGET, True, False)
        assert pipeline.run_python_multi_codegen_worker(str(manifest)) == 0, result.read_text()
    assert packed == encode_assembly_object(
        (tmp_path / "module_0.text.s").read_text(), TARGET,
    )


# It links and runs an x86-64 Linux executable.
@pytest.mark.pcc_gate(probe=lambda: sys.platform == "linux" and platform.machine().lower() in ("x86_64", "amd64"))
def test_packed_object_links_and_executes_owned_machine_code(tmp_path):
    assembly, plans = _fixture()
    assembly += '''.globl _start
.type _start, @function
_start:
  mov edi, 42
  mov eax, 60
  syscall
.size _start, .-_start
'''
    oracle, packed = _assemble_pair(assembly, plans)
    image = elf.link_static_executable([packed])
    assert image == elf.link_static_executable([oracle])
    assert elf.parse_static_executable(image)["entry"] != 0
    binary = tmp_path / "packed-stackmaps"
    binary.write_bytes(image)
    binary.chmod(0o755)
    completed = subprocess.run([str(binary)], capture_output=True, timeout=5)
    assert completed.returncode == 42, completed.stderr
