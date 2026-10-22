"""Native byte accounting at 32-bit boundaries, without allocating large heaps."""

import os
import subprocess
import textwrap

from pcc.frontends.python.pipeline import compile_python


ACCOUNTING_PROBE = r'''
import gc

from pcc.extern import extern, c_int64, c_ptr, c_rawptr, c_void
from pcc.unsafe import global_addr, load_i32, load_i64, store_i32, store_i64, ptr_is_null

backend = extern("pcc_gc_backend", (), c_int64)
note_alloc = extern("pcc_gc_note_alloc", (c_int64,), c_void)
telemetry = extern("pcc_gc_telemetry", (c_int64,), c_int64)
threshold = extern("pcc_gc_tracing_debt_threshold", (), c_int64)
budget = extern("pcc_gc_tracing_budget_from_debt", (), c_int64)
discharge = extern("pcc_gc_tracing_discharge_debt", (c_int64,), c_void)
subtract_live = extern("pcc_gc_live_bytes_subtract", (c_int64,), c_void)
new_list = extern("py_list_new", (c_int64,), c_rawptr)
decref = extern("py_decref", (c_ptr,), c_void)
known_size = extern("pcc_gc_object_known_size", (c_ptr,), c_int64)


def probe() -> int:
    selected: int = int(backend())
    if selected != 1 and selected != 2:
        return 100
    auto_cell = global_addr("pcc_gc_in_auto_step")
    live_cell = global_addr("pcc_gc_live_bytes")
    # The pacing threshold applies the pause to the heap the last mark found.
    marked_cell = global_addr("pcc_gc_tracing_marked_bytes")
    debt_cell = global_addr("pcc_gc_debt_bytes")
    override_cell = global_addr("pcc_gc_debt_threshold_override")
    pause_cell = global_addr("pcc_gc_pause")
    multiplier_cell = global_addr("pcc_gc_stepmul")
    saved_auto: int = int(load_i32(auto_cell, 0))
    saved_live: int = int(load_i64(live_cell, 0))
    saved_marked: int = int(load_i64(marked_cell, 0))
    saved_debt: int = int(load_i64(debt_cell, 0))
    saved_override: int = int(load_i64(override_cell, 0))
    saved_pause: int = int(load_i32(pause_cell, 0))
    saved_multiplier: int = int(load_i32(multiplier_cell, 0))
    # The notifications below test accounting, not a real multi-GiB heap.
    # No automatic collection or CMS assist may consume this synthetic debt.
    store_i32(auto_cell, 0, 1)
    store_i64(override_cell, 0, 1099511627776)
    store_i32(pause_cell, 0, 1000)
    store_i32(multiplier_cell, 0, 10000)
    status: int = 0

    store_i64(debt_cell, 0, 2147483616)
    note_alloc(96)
    crossed_signed: int = int(telemetry(6))
    if crossed_signed != 2147483712:
        status = status | 1
    note_alloc(2147483648)
    crossed_unsigned: int = int(telemetry(6))
    if crossed_unsigned != 4294967360:
        status = status | 2
    if budget() != 65536:
        status = status | 4
    discharge(1)
    credited: int = int(telemetry(6))
    if credited != 4294960960:
        status = status | 8
    store_i64(debt_cell, 0, 63)
    discharge(1)
    if telemetry(6) != 0:
        status = status | 16
    discharge(1)
    if telemetry(6) != 0:
        status = status | 32

    store_i64(override_cell, 0, 0)
    store_i64(marked_cell, 0, 238609295)
    first_wide_threshold: int = int(threshold())
    if first_wide_threshold != 2147483655:
        status = status | 64
    store_i64(marked_cell, 0, 2147483712)
    large_threshold: int = int(threshold())
    if large_threshold != 19327353408:
        status = status | 128
    # Intermediate marked*900 would overflow, but the final *9 still fits.
    store_i64(marked_cell, 0, 1000000000000000001)
    if threshold() != 9000000000000000009:
        status = status | 16384
    store_i64(marked_cell, 0, 9223372036854775807)
    if threshold() != 9223372036854775807:
        status = status | 32768
    store_i32(pause_cell, 0, 137)
    if threshold() != 3412647653636267048:
        status = status | 65536
    store_i32(pause_cell, 0, 100)
    if threshold() != 65536:
        status = status | 131072
    store_i32(pause_cell, 0, 1000)
    store_i64(debt_cell, 0, 9223372036854775807)
    if budget() != 65536:
        status = status | 262144
    # The early cap must also be exact at the smallest legal multiplier.
    store_i32(multiplier_cell, 0, 1)
    store_i64(debt_cell, 0, 419430399)  # floor(debt/64) == 6553599
    if budget() != 65535:
        status = status | 524288
    store_i64(debt_cell, 0, 419430400)  # floor(debt/64) == 6553600
    if budget() != 65536:
        status = status | 1048576
    store_i32(multiplier_cell, 0, 10000)
    store_i64(debt_cell, 0, 0)
    store_i64(live_cell, 0, 2147483712)
    subtract_live(64)
    if load_i64(live_cell, 0) != 2147483648:
        status = status | 256
    subtract_live(2147483649)
    if load_i64(live_cell, 0) != 0:
        status = status | 512
    subtract_live(-1)
    if load_i64(live_cell, 0) != 0:
        status = status | 1024

    # Exercise the real allocation/free writers across the signed boundary.
    # Only one tiny list and its normal payload are actually allocated.
    store_i64(override_cell, 0, 1099511627776)
    store_i64(live_cell, 0, 2147483632)
    obj = new_list(0)
    object_bytes: int = 0
    allocated_live: int = int(load_i64(live_cell, 0))
    if ptr_is_null(obj) != 0:
        status = status | 2048
    else:
        object_bytes = int(known_size(obj))
        if object_bytes <= 16 or allocated_live != 2147483632 + object_bytes:
            status = status | 4096
        decref(obj)
        if load_i64(live_cell, 0) != 2147483632:
            status = status | 8192

    # Restore all synthetic state before print or other managed allocation.
    store_i64(live_cell, 0, saved_live)
    store_i64(marked_cell, 0, saved_marked)
    store_i64(debt_cell, 0, saved_debt)
    store_i64(override_cell, 0, saved_override)
    store_i32(pause_cell, 0, saved_pause)
    store_i32(multiplier_cell, 0, saved_multiplier)
    store_i32(auto_cell, 0, saved_auto)
    print("debt", crossed_signed, crossed_unsigned, credited)
    print("threshold", first_wide_threshold, large_threshold)
    print("live", object_bytes, allocated_live)
    return status


def main() -> None:
    # The runtime is threaded: the GC1/GC2 collector thread and mutator
    # assists would begin cycles on, and pay, the synthetic debt below.
    # Both are automatic collection, which gc.disable() pauses.
    gc.disable()
    print("status", probe())

main()
'''


OVERRIDE_PROBE = r'''
from pcc.extern import extern, c_int64
backend = extern("pcc_gc_backend", (), c_int64)
threshold = extern("pcc_gc_tracing_debt_threshold", (), c_int64)

def main() -> None:
    print("backend", backend())
    print("threshold", threshold())

main()
'''


def _compile(tmp_path, runtime_archive, source):
    script = tmp_path / "byte_accounting.py"
    executable = tmp_path / "byte_accounting"
    script.write_text(textwrap.dedent(source), encoding="utf-8")
    compile_python(
        str(script), str(executable), backend="self", libpython_mode="off",
        runtime_archive=str(runtime_archive),
    )
    return executable


def _environment(backend):
    env = dict(os.environ)
    env.pop("LC_ALL", None)
    for name in ("PCC_GC_DEBT_THRESHOLD", "PCC_GC_PAUSE", "PCC_GC_STEPMUL",
                 "PCC_GC_STEP_MUL"):
        env.pop(name, None)
    env["PCC_GC_BACKEND"] = str(backend)
    return env


def test_native_gc12_byte_counters_cross_32_bit_boundaries(
    tmp_path, pcc_runtime_archive,
):
    executable = _compile(tmp_path, pcc_runtime_archive, ACCOUNTING_PROBE)
    for backend in (1, 2):
        result = subprocess.run(
            [str(executable)], env=_environment(backend), capture_output=True,
            text=True, timeout=20,
        )
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        lines = result.stdout.strip().splitlines()
        assert len(lines) == 4, (backend, result.stdout, result.stderr)
        assert lines[-1] == "status 0", (backend, result.stdout, result.stderr)
        assert lines[0] == "debt 2147483712 4294967360 4294960960", result.stdout
        assert lines[1] == "threshold 2147483655 19327353408", result.stdout


def test_native_gc_debt_override_retains_values_above_32_bits(
    tmp_path, pcc_runtime_archive,
):
    executable = _compile(tmp_path, pcc_runtime_archive, OVERRIDE_PROBE)
    for backend in (1, 2):
        for value in (2147483648, 3221225472, 4294967360, 1099511627776):
            env = _environment(backend)
            env["PCC_GC_DEBT_THRESHOLD"] = str(value)
            result = subprocess.run(
                [str(executable)], env=env, capture_output=True, text=True,
                timeout=20,
            )
            assert result.returncode == 0, (backend, value, result.stderr)
            assert result.stdout.strip().splitlines() == [
                "backend " + str(backend), "threshold " + str(value),
            ]
