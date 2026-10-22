"""Symbolic validation preserves shared roots and every record/frame check."""

from dataclasses import replace

import pytest

from pcc.backend import self_backend_precise_stackmaps as maps
from pcc.backend.precise_stackmap import (
    ARCH_AARCH64, ARCH_X86_64, PreciseStackMapError, SAFEPOINT_CALL,
)


def _plan(name, identity, roots, count=4, first_id=1, frame_size=2048):
    records = tuple(maps.PlannedSafepoint(
        first_id + index, ".L" + name + str(index), SAFEPOINT_CALL, roots,
    ) for index in range(count))
    return maps.FunctionStackMapPlan(
        name, identity, frame_size, ".L" + name + "end", records,
        (), (), (), "x86_64-linux",
    )


@pytest.mark.parametrize("arch", [ARCH_AARCH64, ARCH_X86_64])
def test_repeated_roots_materialize_once_and_retain_all_records(monkeypatch, arch):
    roots = tuple(maps.PlannedRootLocation(-8 * (index + 1), True)
                  for index in range(256))
    plan = _plan("many", 1, roots, count=4096)
    real_locations, real_validate = maps._stack_locations, maps.validate_stack_map
    materialized = []

    def convert(locations, **kwargs):
        result = real_locations(locations, **kwargs)
        materialized.append(result)
        return result

    def validate(value, **kwargs):
        records = value.functions[0].records
        assert len(records) == 4096
        assert len({id(record.locations) for record in records}) == 1
        assert all(record.locations == materialized[0] for record in records)
        real_validate(value, **kwargs)

    monkeypatch.setattr(maps, "_stack_locations", convert)
    monkeypatch.setattr(maps, "validate_stack_map", validate)
    maps._validate_symbolic_plans((plan,), arch=arch)
    assert len(materialized) == 1


def test_shared_roots_still_validate_each_function_frame():
    roots = (maps.PlannedRootLocation(-32, True),)
    good = _plan("good", 1, roots, frame_size=32)
    bad = _plan("small", 2, roots, first_id=10, frame_size=16)
    with pytest.raises(PreciseStackMapError, match="exceeds frame size"):
        maps._validate_symbolic_plans((good, bad), arch=ARCH_X86_64)


@pytest.mark.parametrize("failure", ["duplicate-id", "invalid-kind"])
def test_repeated_roots_preserve_record_rejections(failure):
    roots = (maps.PlannedRootLocation(-8, True),)
    plan = _plan("bad", 1, roots)
    records = list(plan.records)
    if failure == "duplicate-id":
        records[-1] = replace(records[-1], safepoint_id=records[0].safepoint_id)
        expected = "duplicate safepoint id"
    elif failure == "invalid-kind":
        records[-1] = replace(records[-1], kind=99)
        expected = "unknown safepoint kind"
    with pytest.raises(PreciseStackMapError, match=expected):
        maps._validate_symbolic_plans((replace(plan, records=tuple(records)),), arch=ARCH_X86_64)


def test_equal_distinct_roots_do_not_alias_by_address_or_value(monkeypatch):
    roots = (maps.PlannedRootLocation(-8, True),)
    other = (maps.PlannedRootLocation(-8, True),)
    plan = _plan("distinct", 1, roots, count=2)
    plan = replace(plan, records=(plan.records[0], replace(plan.records[1], locations=other)))
    real_locations = maps._stack_locations
    seen = []

    def convert(locations, **kwargs):
        seen.append(locations)
        return real_locations(locations, **kwargs)

    monkeypatch.setattr(maps, "_stack_locations", convert)
    maps._validate_symbolic_plans((plan,), arch=ARCH_X86_64)
    assert len(seen) == 2
    assert seen[0] is roots and seen[1] is other
