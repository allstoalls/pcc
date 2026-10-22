"""Native execution control for the exact production Linux metadata packer.

This is deliberately separate from the fixture-free emission tests: it needs
an explicitly qualified runtime and a compiler, and runs the result under all
five collectors. Source copying follows test_precise_stackmap_record_sort.py.
"""

import inspect
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from pcc.backend import precise_stackmap as wire
from pcc.backend import self_backend_precise_stackmaps as maps
from pcc.backend import self_backend_value_arena as arena_module
from pcc.backend import x86_64_asm_driver as assembler
from tests.python.test_x86_64_packed_stackmaps import _fixture, _symbol, _block, NOTE


def _native_cases():
    from dataclasses import replace

    assembly, plans = _fixture()
    plan = plans[0]
    # Equal PCs are invalid at publication, but the unsigned tie order before
    # validation must still survive native signed-i64 arena storage exactly.
    # Each tied record holds a root: a rootless call record is not written,
    # and the tie must reach the payload validator.
    root = next(location for record in plan.records for location in record.locations)
    tied = replace(plan, records=(
        maps.PlannedSafepoint((1 << 63) + 5, ".Lprobe_call", wire.SAFEPOINT_CALL, (root,)),
        maps.PlannedSafepoint(5, ".Lprobe_call", wire.SAFEPOINT_CALL, (root,)),
        maps.PlannedSafepoint((1 << 63) - 1, ".Lprobe_call", wire.SAFEPOINT_CALL, (root,)),
    ))
    # The minimum signed location offset is valid when its owning frame fits.
    extreme = replace(plan, frame_size=0x80000000, records=(
        maps.PlannedSafepoint(9, ".Lprobe_call", wire.SAFEPOINT_CALL,
                             (maps.PlannedRootLocation(-0x80000000, True),)),
    ))
    # The second function uses distinct but equal root tuples. The same wire
    # slice must be shared across functions, retaining the per-frame checks.
    other_records = tuple(replace(
        record,
        safepoint_id=(1 << 63) + 1 if record.kind == wire.SAFEPOINT_ENTRY else record.safepoint_id + 10,
        label=record.label.replace("probe", "other"),
        locations=tuple(maps.PlannedRootLocation(root.offset, root.owned) for root in record.locations),
    ) for record in plan.records)
    other = replace(plan, function_name="other", function_id=wire.function_id("other"),
                    end_label=".Lother_end", records=other_records)
    both_assembly = assembly + assembly.split(".text\n", 1)[1].replace("probe", "other")
    return (
        ("shared", both_assembly, (plan, other), True),
        ("signed-location", assembly, (extreme,), True),
        ("unsigned-equal-pc", assembly, (tied,), False),
    )


def _text_oracle_payload(assembly, plans, *, valid):
    rendered = maps.render_x86_64_stack_map_section(
        assembly.splitlines(), plans, function_symbol=_symbol, block_label=_block,
    )
    # Intercept only construction to inspect the malformed equal-PC corpus's
    # raw bytes. No unchecked object is serialized, linked or published. Both
    # host and native controls then require the real validator to reject it.
    with pytest.MonkeyPatch.context() as context:
        context.setattr(assembler, "ElfObject", lambda sections, symbols: sections)
        sections = assembler.assemble_file(assembly + "\n".join(rendered) + "\n" + NOTE)
    payload = next(section.data for section in sections if section.name == ".pcc_stackmaps")
    if valid:
        wire.validate_stack_map_payload(payload, expected_arch=wire.ARCH_X86_64)
    else:
        with pytest.raises(wire.PreciseStackMapError, match="ordered in-function"):
            wire.validate_stack_map_payload(payload, expected_arch=wire.ARCH_X86_64)
    return payload


def _copied_native_source():
    """Materialize unchanged production algorithms and finite fixture inputs."""
    # Keep the production arena class, its value records and imports verbatim.
    # The unrelated persistent-span arena is not needed by this control.
    arena_source = Path(arena_module.__file__).read_text()
    text = arena_source.split("class CompilerRecordSpanArena:", 1)[0]
    # ABI constants/codecs/native-read selection and the exact complete packed
    # validator are production source, rather than a test imitation of checks.
    wire_prefix = Path(wire.__file__).read_text().split("@dataclass(frozen=True)", 1)[0]
    text += "\n" + wire_prefix.replace("from __future__ import annotations\n", "")
    for function in (wire._check_uint, wire._register_limit, wire._take,
                     wire.validate_stack_map_payload):
        text += "\n" + inspect.getsource(function)
    text += '''
from dataclasses import dataclass
from pcc.unsafe import store_i8

class BackendUnavailable(Exception):
    pass

# Annotation-only fixture shell. No packed planner is instantiated here.
class PackedPlannedSafepoints:
    pass

_STACK_MAP_HEADER_CODEC = _HEADER
_STACK_MAP_FUNCTION_CODEC = _FUNCTION
_STACK_MAP_RECORD_CODEC = _RECORD
_STACK_MAP_LOCATION_CODEC = _LOCATION
_STACK_MAP_RECORD_SCALAR_COUNT = 10
'''
    for cls in (maps.PlannedRootLocation, maps.PlannedManagedReload, maps.PlannedSafepoint):
        text += "\n" + inspect.getsource(cls)
    # Copy the real immutable plan field declaration, without unrelated code
    # emission methods that consume IR. The tested builder owns only fields.
    text += "\n" + inspect.getsource(maps.FunctionStackMapPlan).split(
        "    def diagnostic_records(", 1,
    )[0]
    text += (
        "\n_ROOTLESS_PRUNABLE_KINDS = (SAFEPOINT_LOOP, SAFEPOINT_CALL)\n"
    )
    for function in (
        maps._planned_record_is_rootless,
        maps._pack_stack_map_record_arena,
        maps.build_x86_64_stack_map_payload,
    ):
        text += "\n" + inspect.getsource(function)
    text += '''
def _symbol(name):
    return name

def _block(function, name):
    return ".L" + function + "_" + name

def _share_first_plan_roots(plans):
    plan = plans[0]
    shared = None
    records = []
    for record in plan.records:
        locations = record.locations
        if locations:
            if shared is None:
                shared = locations
            locations = shared
        records.append(PlannedSafepoint(
            record.safepoint_id, record.label, record.kind, locations,
            record.flags, record.exceptional_block, record.continuation_id,
            record.reloads,
        ))
    first = FunctionStackMapPlan(
        plan.function_name, plan.function_id, plan.frame_size, plan.end_label,
        tuple(records), (), (), (), plan.target,
    )
    return (first,) + plans[1:]

def _assert_rejected(plans, offsets):
    rejected = False
    try:
        payload, relocations = build_x86_64_stack_map_payload(
            plans, offsets, function_symbol=_symbol, block_label=_block,
        )
        validate_stack_map_payload(payload, expected_arch=ARCH_X86_64)
    except (PreciseStackMapError, BackendUnavailable):
        rejected = True
    assert rejected

def main():
    arena = CompilerIntArena()
    assert arena.uses_native_storage
    assert arena.native_address() != 0
    # Raw scalar-packer limits, independent of metadata semantics: all uint32
    # bits, a maximum uint16 count and a signed-i64 representation of uint64.
    arena.append4(-1, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF)
    arena.append4(0xFFFF, 0, 0xFF, 0xFF)
    arena.append2(0, 0xFFFFFFFF)
    scalar_bytes = _pack_stack_map_record_arena(arena)
    assert scalar_bytes == b"\\xff" * 22 + b"\\x00\\x00\\xff\\xff\\x00\\x00" + b"\\xff" * 4
    arena.close()
'''
    for index, (name, assembly, plans, valid) in enumerate(_native_cases()):
        _sections, _order, _symbols, labels = assembler._encode_file(
            assembly, assembler._OperandParseCache(),
        )
        offsets = {name: location[1] for name, location in labels.items() if location[0] == ".text"}
        expected = _text_oracle_payload(assembly, plans, valid=valid)
        # repr of the production immutable plans invokes the copied, exact
        # constructors and preserves shared-content semantics under pcc1.
        text += "\n    # " + name + "\n"
        text += "    plans = " + repr(plans) + "\n"
        if name == "shared":
            # Exercise both identity reuse and cross-function content reuse.
            text += "    plans = _share_first_plan_roots(plans)\n"
        text += "    offsets = " + repr(offsets) + "\n"
        text += '''    payload, relocations = build_x86_64_stack_map_payload(
        plans, offsets, function_symbol=_symbol, block_label=_block,
    )
'''
        text += "    assert payload == " + repr(expected) + "\n"
        expected_relocations = []
        cursor = wire.HEADER_SIZE
        for plan in sorted(plans, key=lambda plan: plan.function_id):
            expected_relocations.append((cursor + 8, plan.function_name))
            written = sum(
                1 for record in plan.records if not maps._planned_record_is_rootless(record)
            )
            cursor += wire.FUNCTION_SIZE + written * wire.RECORD_SIZE
        text += "    assert relocations == " + repr(tuple(expected_relocations)) + "\n"
        if valid:
            text += "    validate_stack_map_payload(payload, expected_arch=ARCH_X86_64)\n"
            if name == "shared":
                text += '''    plan = plans[1]
    too_small = FunctionStackMapPlan(
        plan.function_name, plan.function_id, 0, plan.end_label,
        plan.records, (), (), (), plan.target,
    )
    _assert_rejected((plans[0], too_small), offsets)
'''
        else:
            text += "    _assert_rejected(plans, offsets)\n"
            # Bytes prove unsigned tie ordering even though equal PCs reject.
            text += "    assert payload[56:64] == (5).to_bytes(8, 'little')\n"
            text += "    assert payload[88:96] == ((1 << 63) - 1).to_bytes(8, 'little')\n"
            text += "    assert payload[120:128] == ((1 << 63) + 5).to_bytes(8, 'little')\n"

    # Invalid inputs must be rejected before fixed-width stores can wrap them.
    text += '''
    offsets = {"probe": 0, ".Lprobe_end": 32, ".Lprobe_call": 8, ".Lprobe_failure": -1}
    bad_values = (-1, 0x10000000000000000)
    for invalid in bad_values:
        record = PlannedSafepoint(invalid, ".Lprobe_call", SAFEPOINT_CALL, ())
        plan = FunctionStackMapPlan("probe", 1, 32, ".Lprobe_end", (record,), (), (), (), "x86_64-linux")
        _assert_rejected((plan,), offsets)
        valid_record = PlannedSafepoint(1, ".Lprobe_call", SAFEPOINT_CALL, ())
        invalid_function = FunctionStackMapPlan("probe", invalid, 32, ".Lprobe_end", (valid_record,), (), (), (), "x86_64-linux")
        _assert_rejected((invalid_function,), offsets)
    for invalid in (-1, 256):
        record = PlannedSafepoint(1, ".Lprobe_call", invalid, ())
        plan = FunctionStackMapPlan("probe", 1, 32, ".Lprobe_end", (record,), (), (), (), "x86_64-linux")
        _assert_rejected((plan,), offsets)
    for invalid in (-1, 256, 128):
        record = PlannedSafepoint(1, ".Lprobe_call", SAFEPOINT_CALL, (), invalid)
        plan = FunctionStackMapPlan("probe", 1, 32, ".Lprobe_end", (record,), (), (), (), "x86_64-linux")
        _assert_rejected((plan,), offsets)
    for invalid in (-1, 0x100000000):
        record = PlannedSafepoint(1, ".Lprobe_call", SAFEPOINT_CONTINUATION, (), RECORD_SUSPENDED, "", invalid)
        plan = FunctionStackMapPlan("probe", 1, 32, ".Lprobe_end", (record,), (), (), (), "x86_64-linux")
        _assert_rejected((plan,), offsets)
    for invalid in (-0x80000001, 0x80000000, -1, -40):
        roots = (PlannedRootLocation(invalid, True),)
        record = PlannedSafepoint(1, ".Lprobe_call", SAFEPOINT_CALL, roots)
        plan = FunctionStackMapPlan("probe", 1, 32, ".Lprobe_end", (record,), (), (), (), "x86_64-linux")
        _assert_rejected((plan,), offsets)
    for invalid in (-16, 24, 0x100000000):
        record = PlannedSafepoint(1, ".Lprobe_call", SAFEPOINT_CALL, ())
        plan = FunctionStackMapPlan("probe", 1, invalid, ".Lprobe_end", (record,), (), (), (), "x86_64-linux")
        _assert_rejected((plan,), offsets)
    # -1 is not the uint32 NO_OFFSET sentinel and cannot silently wrap to it.
    record = PlannedSafepoint(1, ".Lprobe_call", SAFEPOINT_EXCEPTION, (), RECORD_HAS_EXCEPTION_EDGE, "failure")
    plan = FunctionStackMapPlan("probe", 1, 32, ".Lprobe_end", (record,), (), (), (), "x86_64-linux")
    _assert_rejected((plan,), offsets)
    # Maximum representable code size and final valid PC are wire boundaries,
    # so test them directly without allocating a four-gigabyte code section.
    offsets = {"probe": 0, ".Lprobe_end": 0xFFFFFFFF, ".Lprobe_call": 0xFFFFFFFE}
    # A root keeps the call record in the payload (rootless ones are omitted).
    record = PlannedSafepoint(1, ".Lprobe_call", SAFEPOINT_CALL, (PlannedRootLocation(-16, True),))
    plan = FunctionStackMapPlan("probe", 1, 32, ".Lprobe_end", (record,), (), (), (), "x86_64-linux")
    payload, relocations = build_x86_64_stack_map_payload(
        (plan,), offsets, function_symbol=_symbol, block_label=_block,
    )
    assert payload[40:44] == b"\\xff\\xff\\xff\\xff"
    assert payload[64:68] == b"\\xfe\\xff\\xff\\xff"
    validate_stack_map_payload(payload, expected_arch=ARCH_X86_64)
    offsets[".Lprobe_end"] = 0x100000000
    _assert_rejected((plan,), offsets)
    print("native-packed-stackmaps-ok")

main()
'''
    return text


def test_production_packed_builder_executes_with_native_arenas_under_all_collectors(
    tmp_path, monkeypatch, python_program_compiler, pcc_runtime_archive, request,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "native_packed_stackmaps.py"
    source.write_text(_copied_native_source())
    binary = tmp_path / "native_packed_stackmaps"
    receipt = {
        "compiler_mode": request.node.callspec.params["python_program_compiler"],
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "runtime_archive": str(pcc_runtime_archive),
        "runtime_sha256": hashlib.sha256(Path(pcc_runtime_archive).read_bytes()).hexdigest(),
        "copied_production_sources": {
            str(Path(module.__file__)):
            hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
            for module in (arena_module, wire, maps)
        },
        "gc_runs": [],
    }
    receipt_path = tmp_path / "native_packed_stackmaps.receipt.json"
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    python_program_compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    receipt["binary_sha256"] = hashlib.sha256(binary.read_bytes()).hexdigest()
    for backend in range(5):
        completed = subprocess.run(
            [str(binary)], capture_output=True, text=True, timeout=20,
            env=dict(os.environ, PATH="/nonexistent", PCC_GC_BACKEND=str(backend)),
        )
        (tmp_path / f"gc{backend}.stdout").write_text(completed.stdout)
        (tmp_path / f"gc{backend}.stderr").write_text(completed.stderr)
        receipt["gc_runs"].append({"backend": backend, "returncode": completed.returncode})
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
        assert completed.returncode == 0, (backend, completed.stdout, completed.stderr)
        assert completed.stdout.strip() == "native-packed-stackmaps-ok"
