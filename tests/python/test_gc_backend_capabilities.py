from __future__ import annotations

from pcc.diagnostics.gc_backend_capabilities import all_backends, by_id, production_backends, validate_capabilities


def test_gc_backend_capabilities_cover_all_five_backends():
    validate_capabilities()
    assert [b.backend_id for b in all_backends()] == [0, 1, 2, 3, 4]


def test_gc_backend_capability_flags_match_design_intent():
    assert by_id(0).production_default
    assert by_id(2).concurrent
    assert by_id(4).moving
    assert by_id(4).uses_read_barrier
    assert by_id(1).uses_write_barrier
    assert by_id(3).uses_write_barrier


def test_only_refcount_cycle_is_default_production_backend():
    assert [b.name for b in production_backends()] == ["refcount-cycle"]
