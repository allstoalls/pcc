"""Finite host differential coverage, not native or full bootstrap evidence."""

import random
import struct

import pytest

from pcc.backend import precise_stackmap as wire
from pcc.backend import macho_host_runtime_validation as bounded
from pcc.backend import macho_host_validation as host


def _budget(limit=10_000_000):
    return wire.CompactStackMapBudget(
        limit, 1_000_000, set_header_bytes=256, set_entry_bytes=16,
        integer_bytes=32, metadata_entry_bytes=512, allocation_quantum=16,
    )


def _payload(*, functions=None, records=None, sets=None, words=None, rare=()):
    words = [-8, -15] if words is None else words
    sets = [(0, len(words))] if sets is None else sets
    records = [(index, index % len(sets)) for index in range(max(1, len(sets)))] if records is None else records
    functions = [(1, 4096, 1000, 32, 0, len(records))] if functions is None else functions
    chunks = [wire._RUNTIME_HEADER.pack(
        wire.MAGIC, 3, wire.ARCH_AARCH64, 8, len(functions), len(records),
        len(sets), len(words), len(rare), 0, 0,
    )]
    for codec, rows in ((wire._RUNTIME_FUNCTION, functions),
                        (wire._RUNTIME_RECORD, records),
                        (wire._RUNTIME_LIVE_SET, sets),
                        (wire._RUNTIME_LOCATION, [(word,) for word in words]),
                        (wire._RUNTIME_RARE, rare)):
        chunks.extend(codec.pack(*row) for row in rows)
    return b"".join(chunks)


def _outcome(validator, payload, **options):
    try:
        result = validator(payload, **options)
    except wire.PreciseStackMapError as exc:
        return str(exc)
    assert result is None
    return None


def _compare(payload, **options):
    expected = _outcome(wire.validate_runtime_table, payload, **options)
    actual = _outcome(bounded.validate_runtime_table, payload,
                      compact_budget=_budget(), **options)
    assert actual == expected
    return actual


@pytest.mark.parametrize("words,sets", [
    ([-8, -15], [(0, 2)]),
    ([-8, -15], [(0, 2), (0, 2), (1, 1), (2, 0)]),
    ([0, -3, -8], [(2, 1), (3, 0)]),
    ([-2147483648, -2147483647], [(0, 2)]),
    ([2147483647], [(0, 1)]),
    ([], [(0, 0)]),
])
def test_summary_edge_words_and_overlap(words, sets):
    _compare(_payload(words=words, sets=sets))


def test_unused_invalid_set_and_shared_smaller_frame():
    assert _compare(_payload(words=[-8, 0], sets=[(0, 1), (1, 1)],
                             records=[(0, 0), (1, 0)])) is None
    payload = _payload(words=[-32], records=[(0, 0), (0, 0)],
                       functions=[(1, 4096, 10, 32, 0, 1), (2, 8192, 10, 16, 1, 1)])
    assert _compare(payload) == "runtime location exceeds its frame"
    assert _compare(_payload(words=[-2147483648, -2147483647],
                             functions=[(1, 4096, 10, 2147483648, 0, 1)])) is None


@pytest.mark.parametrize("rare", [
    [(0, wire.SAFEPOINT_EXCEPTION, wire.RECORD_HAS_EXCEPTION_EDGE, 0, 2, 0)],
    [(0, wire.SAFEPOINT_CONTINUATION, wire.RECORD_SUSPENDED, 0, wire.NO_OFFSET, 1)],
    [(0, 255, 0, 0, wire.NO_OFFSET, 0)],
    [(0, wire.SAFEPOINT_CALL, 128, 0, wire.NO_OFFSET, 0)],
    [(0, wire.SAFEPOINT_CALL, 0, 1, wire.NO_OFFSET, 0)],
    [(0, wire.SAFEPOINT_CALL, 0, 0, 2, 0)],
    [(0, wire.SAFEPOINT_CONTINUATION, 0, 0, wire.NO_OFFSET, 0)],
    [(0, wire.SAFEPOINT_EXCEPTION, wire.RECORD_HAS_EXCEPTION_EDGE, 0, 1000, 0)],
    [(1, wire.SAFEPOINT_CALL, 0, 0, wire.NO_OFFSET, 0)],
    [(0, wire.SAFEPOINT_CALL, 0, 0, wire.NO_OFFSET, 0)] * 2,
])
def test_rare_checks(rare):
    _compare(_payload(records=[(0, 0), (1, 0)], rare=rare))


def test_original_error_precedence():
    bad_rare = [(0, 255, 0, 0, wire.NO_OFFSET, 0)]
    assert _compare(_payload(words=[0], rare=bad_rare)) == "runtime rare record has an unknown kind or flag"
    assert _compare(_payload(sets=[(0, 100)], rare=bad_rare)) == "runtime live set exceeds the location table"
    assert _compare(_payload(words=[0], functions=[(0, 4096, 1000, 32, 0, 1)])) == "runtime functions are not ordered by stable id"


def test_many_small_random_accepted_and_malformed_ranges():
    rng = random.Random(493)
    for _ in range(150):
        words = [rng.choice([-8, -16, -31, 0, 2147483647]) for _ in range(rng.randrange(0, 700))]
        sets = []
        for _ in range(rng.randrange(1, 12)):
            first = rng.randrange(len(words) + 1)
            sets.append((first, rng.randrange(len(words) - first + 1)))
        _compare(_payload(words=words, sets=sets))
    payload = _payload()
    for _ in range(200):
        altered = bytearray(payload)
        position = rng.randrange(len(altered))
        altered[position] ^= rng.randrange(1, 256)
        _compare(bytes(altered))
    for cut in range(len(payload)):
        _compare(payload[:cut])


def test_block_tree_large_overlaps_match_without_root_expansion():
    words = [-8 - 8 * (index % 4) for index in range(2049)]
    sets = [(0, 2049), (255, 1793), (256, 1536), (512, 0)] * 20
    assert _compare(_payload(words=words, sets=sets)) is None


def test_architecture_addresses_and_empty_table():
    _compare(_payload(), expected_arch=wire.ARCH_X86_64)
    _compare(_payload(functions=[(1, 0, 10, 32, 0, 1)]), final_image=True)
    assert _compare(_payload(functions=[], records=[], sets=[], words=[])) is None


def test_admission_precedes_packed_allocation(monkeypatch):
    def forbidden(*args):
        pytest.fail("allocated packed state before budget admission")
    monkeypatch.setattr(bounded, "bytearray", forbidden, raising=False)
    with pytest.raises(wire.PreciseStackMapError, match="byte budget exceeded"):
        bounded.validate_runtime_table(_payload(), compact_budget=_budget(1))


def test_instrumented_validator_and_walker_fall_back(monkeypatch):
    calls = []
    original = wire.validate_runtime_table
    def observed(payload, **options):
        calls.append(payload)
        return original(payload, **options)
    monkeypatch.setattr(wire, "validate_runtime_table", observed)
    host.validate_stack_map_payload(_payload(), compact_budget=_budget())
    assert len(calls) == 1
    monkeypatch.setattr(wire, "validate_runtime_table", original)
    walker = wire._walk_runtime_table
    def observed_walk(*args):
        calls.append(args[0])
        return walker(*args)
    monkeypatch.setattr(wire, "_walk_runtime_table", observed_walk)
    host.validate_stack_map_payload(_payload(), compact_budget=_budget())
    assert len(calls) == 2


def test_exact_admission_boundary_and_retained_payload():
    payload = _payload()
    checks = []
    budget = _budget()
    original = budget.check_peak
    def observed(size, **options):
        checks.append(size)
        return original(size, **options)
    budget.check_peak = observed
    bounded.validate_runtime_table(payload, compact_budget=budget)
    assert len(checks) == 1
    required = checks[0]
    bounded.validate_runtime_table(payload, compact_budget=_budget(required))
    with pytest.raises(wire.PreciseStackMapError, match="byte budget exceeded"):
        bounded.validate_runtime_table(payload, compact_budget=_budget(required - 1))
    # An exact retained owner charges its payload once, not zero or twice.
    from pcc.backend.macho_host_output import _ValidationBudget
    budget = _budget(required)
    budget.retained_bytes = len(payload)
    bounded.validate_runtime_table(payload, compact_budget=_ValidationBudget(budget, payload))


def test_root_work_bound_does_not_scale_with_expanded_overlap():
    payload = _payload(words=[-8] * 1024, sets=[(0, 1024)] * 32)
    header = wire._RUNTIME_HEADER.unpack_from(payload)
    _, _, _, _, functions, records, sets, locations, rare, _, _ = header
    sets_base = wire._RUNTIME_HEADER.size + functions * 32 + records * 8
    locations_base = sets_base + sets * 8
    summary = bounded._Summaries(payload, sets_base, locations_base, sets,
                                 locations, records, rare, functions, _budget())
    assert len(summary.rows) == 8 * sets
    assert all(summary.get(index) == 8 for index in range(sets))
    assert summary.remaining > 0


def _forbid_bounded_dispatch(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("custom or instrumented input reached bounded dispatch")
    monkeypatch.setattr(bounded, "validate_runtime_table", forbidden)


def test_plain_budget_dispatch_and_unchanged_record_counter(monkeypatch):
    payload, budget = _payload(), _budget()
    assert bounded.available(payload, budget)
    called = []
    original = bounded.validate_runtime_table
    def observed(*args, **kwargs):
        called.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(bounded, "validate_runtime_table", observed)
    host.validate_stack_map_payload(payload, compact_budget=budget)
    assert called == [True]
    assert budget.record_visits == 0


@pytest.mark.parametrize("kind", ["object", "subclass", "instance", "class", "constructor"])
def test_custom_budget_keeps_ignored_argument(monkeypatch, kind):
    budget = _budget()
    def forbidden(*args, **kwargs):
        pytest.fail("previously ignored budget callback was invoked")
    if kind == "object":
        budget = object()
    elif kind == "subclass":
        class Custom(wire.CompactStackMapBudget):
            pass
        budget.__class__ = Custom
    elif kind == "instance":
        budget.check_peak = forbidden
    elif kind == "class":
        monkeypatch.setattr(wire.CompactStackMapBudget, "_rounded", forbidden)
    else:
        monkeypatch.setattr(wire.CompactStackMapBudget, "__init__", forbidden)
    _forbid_bounded_dispatch(monkeypatch)
    host.validate_stack_map_payload(_payload(), compact_budget=budget)


@pytest.mark.parametrize("kind", ["take", "codec", "trace", "profile", "monitoring"])
def test_wire_and_instrumentation_preserve_call_order(monkeypatch, kind):
    from types import SimpleNamespace
    payload = _payload(words=[-8, 0], sets=[(0, 1)], records=[(0, 0)])
    reads = []
    if kind == "take":
        original = wire._take
        def observed(*args):
            reads.append(args[3])
            return original(*args)
        monkeypatch.setattr(wire, "_take", observed)
    elif kind == "codec":
        original = wire._RUNTIME_LOCATION
        class Observed:
            size = original.size
            def unpack_from(self, *args):
                reads.append(args[1])
                return original.unpack_from(*args)
        monkeypatch.setattr(wire, "_RUNTIME_LOCATION", Observed())
    elif kind == "trace":
        monkeypatch.setattr(bounded.sys, "gettrace", lambda: object())
    elif kind == "profile":
        monkeypatch.setattr(bounded.sys, "getprofile", lambda: object())
    else:
        monkeypatch.setattr(bounded.sys, "monitoring", SimpleNamespace(get_tool=lambda tool: "local-only" if tool == 5 else None))
    _forbid_bounded_dispatch(monkeypatch)
    host.validate_stack_map_payload(payload, compact_budget=_budget())
    if kind == "take":
        assert reads == ["runtime stack-map header", "runtime function"]
    elif kind == "codec":
        assert len(reads) == 1


@pytest.mark.parametrize("kind", ["instance", "class", "inner", "payload", "registration"])
def test_adapter_requires_exact_provenance(monkeypatch, kind):
    from pcc.backend.macho_host_output import _ValidationBudget
    payload = _payload()
    adapter = _ValidationBudget(_budget(), payload)
    assert bounded.available(payload, adapter)
    def forbidden(*args, **kwargs):
        pytest.fail("custom adapter callback was invoked")
    if kind == "instance":
        adapter.check_peak = forbidden
    elif kind == "class":
        monkeypatch.setattr(_ValidationBudget, "unretained_payload_bytes", forbidden)
    elif kind == "inner":
        adapter.budget = object()
    elif kind == "payload":
        adapter.payload = bytes(bytearray(payload))
    else:
        class Reloaded(_ValidationBudget):
            pass
        bounded._register_owned_adapter(Reloaded)
        adapter = Reloaded(_budget(), payload)
    _forbid_bounded_dispatch(monkeypatch)
    host.validate_stack_map_payload(payload, compact_budget=adapter)


def test_function_ownership_and_unsigned_frame_boundaries():
    assert _compare(_payload(functions=[(1, 1, 10, 32, 0, 0)])) == "runtime records are not all owned by functions"
    _compare(_payload(functions=[(2, 1, 10, 32, 0, 1), (1, 1, 10, 32, 1, 1)], records=[(0, 0), (0, 0)]))
    frame = 0xFFFFFFF0
    assert _compare(_payload(words=[-2147483648], functions=[(1, 1, 10, frame, 0, 1)])) is None
    assert _compare(_payload(words=[0], functions=[(1, 1, 10, frame, 0, 1)])) == "runtime location exceeds its frame"


@pytest.mark.parametrize("first", ["macho_host_output", "macho_host_validation"])
def test_adapter_import_cycle_and_reload_fallback(first):
    import subprocess
    import sys
    code = '''
import importlib
from pcc.backend import FIRST
from pcc.backend import macho_host_output as output
from pcc.backend import macho_host_validation as host
from pcc.backend import macho_host_runtime_validation as bounded
from tests.python.test_macho_runtime_validation import _payload, _budget
payload = _payload()
old_type = output._ValidationBudget
assert bounded.available(payload, old_type(_budget(), payload))
importlib.reload(output)
assert output._ValidationBudget is not old_type
adapter = output._ValidationBudget(_budget(), payload)
assert not bounded.available(payload, adapter)
def forbidden(*args, **kwargs):
    raise AssertionError("reloaded adapter reached bounded validator")
bounded.validate_runtime_table = forbidden
host.validate_stack_map_payload(payload, compact_budget=adapter)
'''.replace("FIRST", first)
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("kind", ["property", "getattribute", "field", "defaults", "kwdefaults", "mutated_kwdefaults"])
def test_budget_class_additions_and_default_changes_fall_back(monkeypatch, kind):
    budget = _budget()
    def forbidden(*args, **kwargs):
        pytest.fail("custom class descriptor or default was invoked")
    owner = wire.CompactStackMapBudget
    if kind == "property":
        monkeypatch.setattr(owner, "budget", property(forbidden), raising=False)
    elif kind == "getattribute":
        monkeypatch.setattr(owner, "__getattribute__", forbidden, raising=False)
    elif kind == "field":
        monkeypatch.setattr(owner, "allocation_quantum", property(forbidden), raising=False)
    elif kind == "defaults":
        monkeypatch.setattr(owner.check_peak, "__defaults__", (123,))
    elif kind == "kwdefaults":
        monkeypatch.setattr(owner.check_peak, "__kwdefaults__", {"records": 12, "entries": 0, "label": "custom"})
    else:
        monkeypatch.setitem(owner.check_peak.__kwdefaults__, "records", 12)
    _forbid_bounded_dispatch(monkeypatch)
    host.validate_stack_map_payload(_payload(), compact_budget=budget)


def test_adapter_added_descriptor_falls_back_before_instance_access(monkeypatch):
    from pcc.backend.macho_host_output import _ValidationBudget
    payload = _payload()
    adapter = _ValidationBudget(_budget(), payload)
    def forbidden(*args):
        pytest.fail("custom adapter attribute access was invoked")
    monkeypatch.setattr(_ValidationBudget, "__getattribute__", forbidden, raising=False)
    _forbid_bounded_dispatch(monkeypatch)
    host.validate_stack_map_payload(payload, compact_budget=adapter)
