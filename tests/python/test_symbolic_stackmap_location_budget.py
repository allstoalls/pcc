"""Shared symbolic maps obey the v2 stored-location and validation budgets."""
from dataclasses import replace
import platform
import struct
import sys

import pytest

from pcc.backend import precise_stackmap as wire
from pcc.backend import self_backend_precise_stackmaps as plans
from pcc.backend import owned_elf_link as linker, elf_x86_64 as elf


def plan_for(roots, *, count=4, frame=32, identity=1, first_id=1, name='f'):
    records = tuple(plans.PlannedSafepoint(first_id + i, name + str(i), wire.SAFEPOINT_CALL, roots)
                    for i in range(count))
    return plans.FunctionStackMapPlan(name, identity, frame, name + 'end', records, (), (), (), 'x86_64-linux')


def roots_for(count=3):
    return tuple(plans.PlannedRootLocation(-8 * (i + 1), True) for i in range(count))


def model_for(roots, count=4):
    return wire.PreciseStackMap(wire.ARCH_X86_64, (wire.FunctionStackMap(
        1, 0, count + 1, 32, tuple(wire.SafepointRecord(i + 1, i, wire.SAFEPOINT_CALL, roots)
                                 for i in range(count))),))


def test_symbolic_shared_roots_fit_the_physical_budget(monkeypatch):
    monkeypatch.setattr(wire, 'MAX_LOCATIONS', 3)
    plans._validate_symbolic_plans((plan_for(roots_for()),), arch=wire.ARCH_X86_64)


def test_materialized_model_keeps_logical_allocation_budget(monkeypatch):
    monkeypatch.setattr(wire, 'MAX_LOCATIONS', 3)
    value = model_for(plans._stack_locations(roots_for(), arch=wire.ARCH_X86_64))
    with pytest.raises(wire.PreciseStackMapError, match='too many stack-map locations'):
        wire.validate_stack_map(value)


def test_shared_model_and_v2_encoder_use_bounded_validation_work(monkeypatch):
    value = model_for(plans._stack_locations(roots_for(), arch=wire.ARCH_X86_64))
    expected = wire.encode_stack_map(value)
    monkeypatch.setattr(wire, 'MAX_LOCATIONS', 3)
    wire.validate_stack_map(value, shared_locations=True)
    actual = wire.encode_stack_map(value)
    assert actual == expected
    wire.validate_stack_map_payload(actual)
    assert struct.unpack_from('<I', actual, 16)[0] == 3


def test_same_tuple_in_distinct_frames_is_rechecked_and_charged(monkeypatch):
    roots = roots_for()
    first = plan_for(roots, count=1)
    second = plan_for(roots, count=1, frame=48, identity=2, first_id=9, name='g')
    monkeypatch.setattr(wire, 'MAX_LOCATIONS', 5)
    with pytest.raises(wire.PreciseStackMapError, match='location validation work'):
        plans._validate_symbolic_plans((first, second), arch=wire.ARCH_X86_64)
    monkeypatch.setattr(wire, 'MAX_LOCATIONS', 6)
    plans._validate_symbolic_plans((first, second), arch=wire.ARCH_X86_64)
    with pytest.raises(wire.PreciseStackMapError, match='exceeds frame size'):
        plans._validate_symbolic_plans((first, replace(second, frame_size=16)), arch=wire.ARCH_X86_64)


def test_distinct_location_owners_are_charged(monkeypatch):
    first = plan_for(roots_for(), count=1)
    second = plan_for(roots_for(), count=1, identity=2, first_id=9, name='g')
    assert first.records[0].locations is not second.records[0].locations
    monkeypatch.setattr(wire, 'MAX_LOCATIONS', 5)
    with pytest.raises(wire.PreciseStackMapError, match='location validation work'):
        plans._validate_symbolic_plans((first, second), arch=wire.ARCH_X86_64)


@pytest.mark.parametrize('field,value,message', [
    ('safepoint_id', 1, 'duplicate safepoint'),
    ('kind', 99, 'unknown safepoint kind'),
    ('flags', 128, 'record flags'),
    ('flags', wire.RECORD_SUSPENDED, 'suspended flag'),
    ('continuation_id', 1, 'continuation record'),
    ('exceptional_block', 'bad', 'exception-edge flag'),
])
def test_every_record_is_checked_after_shared_roots(monkeypatch, field, value, message):
    plan = plan_for(roots_for())
    plan = replace(plan, records=plan.records[:-1] + (replace(plan.records[-1], **{field: value}),))
    monkeypatch.setattr(wire, 'MAX_LOCATIONS', 3)
    with pytest.raises(wire.PreciseStackMapError, match=message):
        plans._validate_symbolic_plans((plan,), arch=wire.ARCH_X86_64)


def test_shared_locations_reject_per_record_wire_overflow():
    roots = (plans.PlannedRootLocation(-8, True),) * 65536
    with pytest.raises(wire.PreciseStackMapError, match='location count is outside uint16'):
        plans._validate_symbolic_plans((plan_for(roots, count=1),), arch=wire.ARCH_X86_64)


def test_shared_model_keeps_location_semantics():
    root = plans._stack_locations(roots_for(1), arch=wire.ARCH_X86_64)[0]
    for fields, message in [({'kind': 99}, 'unknown location kind'),
                            ({'flags': 0}, 'raw pointer'),
                            ({'offset': -40}, 'exceeds frame size'),
                            ({'offset': -7}, 'not aligned'),
                            ({'register': 1}, 'frame/stack register'),
                            ({'base_index': 0}, 'non-derived'),
                            ({'flags': wire.LOCATION_MANAGED | wire.LOCATION_DERIVED}, 'earlier base')]:
        with pytest.raises(wire.PreciseStackMapError, match=message):
            wire.validate_stack_map(model_for((replace(root, **fields),)), shared_locations=True)


def test_text_and_packed_routes_are_equal_with_shared_budget(monkeypatch):
    plan = plan_for(roots_for(), identity=wire.function_id('f'))
    emitted = ['.intel_syntax noprefix', '.text', '.globl f', 'f:']
    for i in range(4):
        emitted.extend(['f' + str(i) + ':', 'nop'])
    emitted.extend(['fend:', 'ret'])
    symbol = lambda name: name
    block = lambda name, label: name + label
    expected = plans.render_x86_64_stack_map_section(emitted, (plan,), function_symbol=symbol, block_label=block)
    monkeypatch.setattr(wire, 'MAX_LOCATIONS', 3)
    actual = plans.render_x86_64_stack_map_section(emitted, (plan,), function_symbol=symbol, block_label=block)
    assert actual == expected
    obj = linker.assemble('\n'.join(emitted + actual) + '\n', 'x86_64-unknown-linux-gnu')
    actual_payload = next(section.data for section in obj.sections if section.name == '.pcc_stackmaps')
    offsets = {'f': 0, 'fend': 4, **{'f' + str(i): i for i in range(4)}}
    packed, _ = plans.build_x86_64_stack_map_payload((plan,), offsets, function_symbol=symbol, block_label=block)
    assert actual_payload == packed
    assert elf.emit_relocatable(obj)


def test_unmodified_default_budget_accepts_real_shared_capacity():
    roots = (plans.PlannedRootLocation(-8, True),) * 65535
    plan = plan_for(roots, count=2048)
    assert len(roots) * len(plan.records) == 134215680 > wire.MAX_LOCATIONS
    plans._validate_symbolic_plans((plan,), arch=wire.ARCH_X86_64)


# It links and runs an x86-64 Linux executable.
@pytest.mark.pcc_gate(probe=lambda: sys.platform == "linux" and platform.machine().lower() in ("x86_64", "amd64"))
def test_shared_budget_object_links_and_executes(tmp_path, monkeypatch):
    import subprocess
    plan = plan_for(roots_for(), identity=wire.function_id('f'))
    lines = ['.intel_syntax noprefix', '.text', '.globl f', '.type f, @function', 'f:']
    for i in range(4):
        lines.extend(['f' + str(i) + ':', 'nop'])
    lines.extend(['mov eax, 42', 'ret', 'fend:', '.size f, .-f',
                  '.globl _start', '.type _start, @function', '_start:',
                  'call f', 'mov edi, eax', 'mov eax, 60', 'syscall', '.size _start, .-_start'])
    monkeypatch.setattr(wire, 'MAX_LOCATIONS', 3)
    section = plans.render_x86_64_stack_map_section(
        lines, (plan,), function_symbol=lambda name: name, block_label=lambda name, label: name + label)
    obj = linker.assemble('\n'.join(lines + section) + '\n', 'x86_64-unknown-linux-gnu')
    image = elf.link_static_executable([obj])
    binary = tmp_path / 'shared-budget'
    binary.write_bytes(image)
    binary.chmod(0o755)
    result = subprocess.run([str(binary)], capture_output=True, timeout=5)
    assert result.returncode == 42, result.stderr


@pytest.mark.parametrize('owner_type', [list, type('TupleOwner', (tuple,), {})])
def test_mutable_or_subclass_owners_do_not_bypass_work_budget(monkeypatch, owner_type):
    roots = owner_type(plans._stack_locations(roots_for(), arch=wire.ARCH_X86_64))
    monkeypatch.setattr(wire, 'MAX_LOCATIONS', 3)
    with pytest.raises(wire.PreciseStackMapError, match='location validation work'):
        wire.validate_stack_map(model_for(roots), shared_locations=True)


def test_text_rendering_preserves_tuple_sharing_through_interning(monkeypatch):
    plan = plan_for(roots_for())
    lines = ['f:', 'f0:', 'nop', 'f1:', 'nop', 'f2:', 'nop', 'f3:', 'nop', 'fend:']
    original = plans._stack_locations
    conversions = []

    def convert(roots, **kwargs):
        conversions.append(roots)
        return original(roots, **kwargs)

    monkeypatch.setattr(plans, '_stack_locations', convert)
    plans.render_x86_64_stack_map_section(
        lines, (plan,), function_symbol=lambda name: name, block_label=lambda name, label: name + label)
    # One conversion validates the owner, one serializes its v2 table slice.
    assert len(conversions) == 2
    assert all(owner is plan.records[0].locations for owner in conversions)


@pytest.mark.parametrize('root_kind', ['subclass', 'int-subclass'])
def test_mutable_location_lookalikes_do_not_bypass_work_budget(monkeypatch, root_kind):
    root = plans._stack_locations(roots_for(1), arch=wire.ARCH_X86_64)[0]
    if root_kind == 'subclass':
        class LocationSubclass(wire.StackMapLocation):
            pass
        root = LocationSubclass(root.kind, root.flags, register=root.register, offset=root.offset)
    else:
        class OffsetSubclass(int):
            pass
        root = replace(root, offset=OffsetSubclass(root.offset))
    monkeypatch.setattr(wire, 'MAX_LOCATIONS', 1)
    with pytest.raises(wire.PreciseStackMapError, match='location validation work'):
        wire.validate_stack_map(model_for((root,)), shared_locations=True)



def test_v2_encoding_preserves_sharing_and_bounds_actual_work(monkeypatch):
    roots = plans._stack_locations(roots_for(), arch=wire.ARCH_X86_64)
    original = wire._location_key
    converted = []

    def key(location):
        converted.append(location)
        return original(location)

    monkeypatch.setattr(wire, '_location_key', key)
    monkeypatch.setattr(wire, 'MAX_LOCATIONS', 3)
    payload = wire.encode_stack_map(model_for(roots))
    assert converted == list(roots)
    wire.validate_stack_map_payload(payload)



def test_unhashable_frame_subclass_keeps_materialized_behavior(monkeypatch):
    class FrameSize(int):
        __hash__ = None

    roots = plans._stack_locations(roots_for(), arch=wire.ARCH_X86_64)
    value = model_for(roots)
    expected = wire.encode_stack_map(value)
    value = replace(value, functions=(replace(value.functions[0], frame_size=FrameSize(32)),))
    wire.validate_stack_map(value)
    wire.validate_stack_map(value, shared_locations=True)
    assert wire.encode_stack_map(value) == expected
    monkeypatch.setattr(wire, 'MAX_LOCATIONS', 3)
    with pytest.raises(wire.PreciseStackMapError, match='location validation work'):
        wire.validate_stack_map(value, shared_locations=True)


def test_frame_subclass_equality_cannot_hide_smaller_frame():
    class FrameSize(int):
        def __hash__(self):
            return 0

        def __eq__(self, other):
            return True

    root = plans._stack_locations((plans.PlannedRootLocation(-24, True),), arch=wire.ARCH_X86_64)
    value = model_for(root, count=1)
    first = replace(value.functions[0], frame_size=FrameSize(32))
    second = replace(first, function_id=2, frame_size=FrameSize(16),
                     records=(replace(first.records[0], safepoint_id=2),))
    value = replace(value, functions=(first, second))
    with pytest.raises(wire.PreciseStackMapError, match='exceeds frame size'):
        wire.validate_stack_map(value)
    with pytest.raises(wire.PreciseStackMapError, match='exceeds frame size'):
        wire.validate_stack_map(value, shared_locations=True)
    with pytest.raises(wire.PreciseStackMapError, match='exceeds frame size'):
        wire.encode_stack_map(value)
