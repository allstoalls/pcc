"""Memory-aware width-2 admission waves for the oversized emit lane.

The oversized lane ran its multi-GiB workers strictly serially (164.8 s over
7 items on the 2026-08-27 receipts).  Input bytes track peak footprint at
~1.3-1.4 GB/MB, so a 7 MB concurrent-input-byte cap admits every measured
pair at <= 7.2 GB while keeping the two giants (and the giant with anything)
apart.  ``pack_admission_waves`` is first-fit-decreasing over per-command
byte weights; ``run_emit_worker_pool`` weighs each command from its OWN
batch contents because ``_pack_batches`` reorders items by descending bytes
(the caller's ``item_bytes`` list does not align with command order).

Receipts and schedule:
docs/goal/evidence/2026-08-27-oversized-lane-pairing-schedule-design.md
"""
from __future__ import annotations

import os

from pcc.frontends.python.pipeline_self_backend_emit import (
    pack_admission_waves,
    run_emit_worker_pool,
)

# The seven oversized items from the 2026-08-27 receipts, input bytes.
_RECEIPT_BYTES = {
    "call_expression_lowering": 5_100_000,
    "method_call_expression_lowering": 4_600_000,
    "cli_bootstrap": 4_500_000,
    "port_abi_exports": 2_200_000,
    "attr_load_lowering": 2_200_000,
    "type_infer": 2_100_000,
    "cli_bootstrap_array_core": 2_100_000,
}
_CAP = 7_000_000


def test_receipt_bytes_pack_into_the_measured_safe_schedule():
    weights = list(_RECEIPT_BYTES.values())
    waves = pack_admission_waves(weights, _CAP, 2)
    assert len(waves) == 4, waves
    # The giant is alone: nothing pairs with 5.1 MB under a 7 MB cap.
    giant = weights.index(5_100_000)
    assert [giant] in waves, waves
    for wave in waves:
        assert len(wave) <= 2, waves
        assert sum(weights[i] for i in wave) <= _CAP, waves
    # Every command appears exactly once.
    flat = sorted(i for wave in waves for i in wave)
    assert flat == list(range(len(weights))), waves


def test_cap_zero_or_singleton_never_engages_wave_math():
    # cap respected even for degenerate shapes
    assert pack_admission_waves([10], _CAP, 2) == [[0]]
    waves = pack_admission_waves([3, 3, 3], 1, 2)
    assert waves == [[0], [1], [2]], waves


def test_pool_weighs_commands_from_batch_contents(tmp_path):
    """run_emit_worker_pool must never co-schedule two commands whose
    batch-content bytes exceed the cap, regardless of _pack_batches'
    descending reorder — and serial callers (cap 0) get one call."""
    items = []
    weights = []
    for name, size in _RECEIPT_BYTES.items():
        items.append((
            str(tmp_path / (name + ".result")),
            str(tmp_path / (name + ".o")),
            str(tmp_path / (name + ".ll")),
        ))
        weights.append(size)

    calls: list[list[str]] = []
    parallels: list[int] = []

    def record_worker_commands(commands, max_parallel=None):
        calls.append(list(commands))
        parallels.append(max_parallel)

    def small_int_decimal(value: int) -> str:
        return str(int(value))

    def shell_quote_arg(value: str) -> str:
        return "'" + value.replace("'", "'\\''") + "'"

    count = run_emit_worker_pool(
        ["pcc1"],
        items,
        "",
        str(tmp_path),
        "oversized",
        1,
        batch_max_items=4,
        manifest_version="v1",
        item_bytes=weights,
        worker_arg="--emit-batch",
        small_int_decimal=small_int_decimal,
        shell_quote_arg=shell_quote_arg,
        run_worker_commands=record_worker_commands,
        admission_byte_cap=_CAP,
    )
    assert count == len(items)
    assert len(calls) == 4, [len(c) for c in calls]
    # Each wave runs wide enough for all of its members at once.
    assert parallels == [len(c) for c in calls], (parallels, calls)
    # Reconstruct each wave's byte weight through the written manifests:
    # every command names its manifest, whose lines carry the item paths.
    ir_to_bytes = {items[i][2]: weights[i] for i in range(len(items))}
    seen_paths: list[str] = []
    for wave in calls:
        assert len(wave) <= 2, calls
        wave_bytes = 0
        for command in wave:
            manifest_path = command.split("'")[-2]
            lines = (
                open(manifest_path, encoding="utf-8").read().splitlines()
            )
            ir_path = lines[1]
            wave_bytes += ir_to_bytes[ir_path]
            seen_paths.append(ir_path)
        assert wave_bytes <= _CAP, (wave_bytes, calls)
    assert sorted(seen_paths) == sorted(ir_to_bytes), seen_paths

    calls.clear()
    serial_dir = tmp_path / "serial"
    serial_dir.mkdir()
    count = run_emit_worker_pool(
        ["pcc1"],
        items,
        "",
        str(serial_dir),
        "oversized",
        1,
        batch_max_items=4,
        manifest_version="v1",
        item_bytes=weights,
        worker_arg="--emit-batch",
        small_int_decimal=small_int_decimal,
        shell_quote_arg=shell_quote_arg,
        run_worker_commands=record_worker_commands,
        admission_byte_cap=0,
    )
    assert count == len(items)
    assert len(calls) == 1 and len(calls[0]) == len(items)
