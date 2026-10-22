"""Self-backend worker budgets and large-module splitting policy."""

from __future__ import annotations

import os

from pcc.frontends.python.pipeline_frontend_workers import (
    WORKER_COORDINATOR_RESERVE_BYTES,
    WORKER_TREE_BUDGET_ENV,
    FrontendWorkerContractError,
    compiled_native_worker_budget,
    worker_tree_budget_bytes,
)
from pcc.frontends.python.pipeline_ir_split import split_self_backend_ir_module_for_object_shards
from pcc.frontends.python.pipeline_pass_config import parallel_cpu_budget, positive_int_env, small_int_decimal


# Emit-worker default.  This was 2 while a compiled emitter needed several GiB
# for one multi-megabyte module; with the dynamic-method-call reference leaks and
# the O(n**2) keyed sort in stack-map planning both fixed, a measured cold stage1
# peaks at 1.27 GB across ALL emit workers on a 96 GB host.  The frontend has
# always used ~10 here, and the emit lane was the only phase still 12x off its
# recorded baseline (434 s vs 35 s).  Measured cold stage1, fresh cache identity:
#
#     emit jobs   2 -> 589 s   (emit phase 434 s)
#     emit jobs   8 -> 404 s   (emit phase 272 s, peak 1.27 GB)
#     emit jobs  16 -> 406 s   (emit phase 272 s, peak 1.19 GB)
#
# 16 buys nothing over 8: at peak only 3 workers are alive at once, so past 8 the
# limit is elsewhere, not the worker budget.  Raise this only with a measurement
# that shows both a shorter emit phase and a peak the host can hold.
SELF_BACKEND_DEFAULT_JOBS = 8
SELF_BACKEND_JOBS_ENV = "PCC_SELF_BACKEND_JOBS"
# The eight-worker default above is retained for host/source Stage1, where its
# 1.27 GiB whole-pool peak was actually measured.  A compiled pcc1 worker has a
# different allocator/object cost model and has reached tens of GiB per item;
# automatic Stage2+ emission must therefore use the separately proven bound.
COMPILED_NATIVE_SAFE_JOBS = 2
# One compiled emit worker on a sub-threshold (< 2 MB) shard, measured
# 2026-10-18 with a macOS arm64 Stage1 pcc1 from 012ec3265: 149-218 MB peak RSS
# for 0.8-2.2 MB shards of the worker_size_priors program and of the Stage2
# module pcc.frontends.python.codegen.call_object_lowering (~80 MB plus ~0.1 GB
# per input MB; a 7.3 MB shard peaked at 593 MB).  Under a declared worker tree
# budget the safe lane admits workers at this class, which covers the largest
# sub-threshold shard by more than 2x.
COMPILED_EMIT_SAFE_WORKER_PEAK_BYTES = 536870912
# Peak model for one compiled emit worker on an oversized (>= split threshold)
# shard, from the same replays (593 MB for 7.3 MB, 218 MB for 2.2 MB): a
# worker is charged 128 MiB plus 128 bytes per input byte, about 1.8x the
# largest replay.
COMPILED_EMIT_WORKER_BASE_BYTES = 134217728
COMPILED_EMIT_PEAK_BYTES_PER_INPUT_BYTE = 128
# How many >=threshold modules may be emitted concurrently.  See
# jobs_for_input_sizes for the measurement behind this.
LARGE_INPUT_CONCURRENCY = 4
SELF_BACKEND_SKIP_LL_TEMP_ENV = "PCC_SELF_BACKEND_SKIP_LL_TEMP"
SELF_BACKEND_SPLIT_LARGE_MODULES_ENV = "PCC_SELF_BACKEND_SPLIT_LARGE_MODULES"
SELF_BACKEND_SPLIT_THRESHOLD_BYTES_ENV = "PCC_SELF_BACKEND_SPLIT_THRESHOLD_BYTES"
SELF_BACKEND_SPLIT_SHARD_BYTES_ENV = "PCC_SELF_BACKEND_SPLIT_SHARD_BYTES"


def jobs(n_modules: int) -> int:
    n_modules = int(n_modules)
    if n_modules <= 1:
        return 1
    raw = str(os.environ.get(SELF_BACKEND_JOBS_ENV, "") or "").strip()
    if raw:
        try:
            selected = int(raw)
        except ValueError:
            selected = 1
        return max(1, min(n_modules, selected))
    cpu_count = parallel_cpu_budget()
    return max(1, min(n_modules, cpu_count, SELF_BACKEND_DEFAULT_JOBS))


def jobs_for_ir_texts(ir_texts, *, native_worker: bool) -> int:
    return jobs_for_input_sizes(
        [len(str(ir_text)) for ir_text in ir_texts],
        native_worker=native_worker,
    )


def jobs_for_input_sizes(input_sizes, *, native_worker: bool) -> int:
    selected = jobs(len(input_sizes))
    if selected <= 1 or not native_worker:
        return selected
    raw = str(os.environ.get(SELF_BACKEND_JOBS_ENV, "") or "").strip()
    if raw:
        return selected
    threshold = split_threshold_bytes()
    large_inputs = 0
    for input_bytes in input_sizes:
        if int(input_bytes) >= threshold:
            large_inputs += 1
    if not large_inputs:
        return compiled_native_safe_jobs(selected)
    # Bound the lane by how many large modules can be in flight at once, not by
    # collapsing the whole batch to one worker.  The old rule returned 1 as soon
    # as ANY input crossed the threshold, so a single large module serialized all
    # 525 objects of a cold stage1 -- 434 s in the emit phase against a recorded
    # 35 s baseline.  That rule was written when one multi-megabyte module could
    # take several GiB; a measured cold stage1 now peaks at 1.27 GB across every
    # worker combined.  Keeping a cap (rather than removing it) preserves the
    # protection for a host with genuinely little memory and for a future
    # regression that makes a single module expensive again.
    return max(
        1,
        min(selected, LARGE_INPUT_CONCURRENCY, COMPILED_NATIVE_SAFE_JOBS),
    )


def compiled_native_safe_jobs(selected: int) -> int:
    """Width for compiled emit workers whose inputs are all below the split threshold.

    Without a declared worker tree budget no outer guard enforces an estimate,
    so the fixed proven bound stays.  With one, the budget pays for the
    resident coordinator first, then one measured worker class per worker.
    """
    budget = worker_tree_budget_bytes(os.environ.get(WORKER_TREE_BUDGET_ENV, ""))
    if budget <= 0:
        return min(selected, COMPILED_NATIVE_SAFE_JOBS)
    available = compiled_native_worker_budget(budget, WORKER_COORDINATOR_RESERVE_BYTES)
    by_memory = available // COMPILED_EMIT_SAFE_WORKER_PEAK_BYTES
    if by_memory < 1:
        raise FrontendWorkerContractError(
            "worker tree budget cannot admit one compiled emit worker: available="
            + str(available) + " worker=" + str(COMPILED_EMIT_SAFE_WORKER_PEAK_BYTES)
        )
    return min(selected, by_memory)


def skip_ll_temp() -> bool:
    normalized = str(
        os.environ.get(SELF_BACKEND_SKIP_LL_TEMP_ENV, "") or ""
    ).strip().lower()
    if normalized in ("0", "false", "no", "off", "legacy"):
        return False
    if normalized in ("1", "true", "yes", "on"):
        return True
    return True


def split_large_modules_enabled() -> bool:
    normalized = str(
        os.environ.get(SELF_BACKEND_SPLIT_LARGE_MODULES_ENV, "") or ""
    ).strip().lower()
    if normalized in ("0", "false", "no", "off", "legacy"):
        return False
    return True


def split_threshold_bytes() -> int:
    return positive_int_env(
        SELF_BACKEND_SPLIT_THRESHOLD_BYTES_ENV,
        2_000_000,
    )


def split_shard_bytes() -> int:
    return positive_int_env(
        SELF_BACKEND_SPLIT_SHARD_BYTES_ENV,
        1_000_000,
    )


def split_large_ir_modules(ir_texts: list[str]) -> list[str]:
    if not split_large_modules_enabled():
        return ir_texts
    threshold = split_threshold_bytes()
    shard_bytes = split_shard_bytes()
    out: list[str] = []
    for index, text in enumerate(ir_texts):
        text = str(text)
        if len(text) < threshold:
            out.append(text)
            continue
        shards = split_self_backend_ir_module_for_object_shards(
            text,
            export_prefix="__pco" + small_int_decimal(index) + "_",
            shard_bytes=shard_bytes,
        )
        out.extend(shards)
    return out


def compiled_native_oversized_admission(item_count: int):
    """``(width, input-byte cap)`` for oversized compiled emit waves, or None.

    None without a declared worker tree budget: the caller keeps its fixed
    waves.  With one, a wave may hold up to the CPU width of workers whose
    modeled peaks (base plus a per-input-byte charge) fit beside the resident
    coordinator.  When not even one oversized input fits, waves are serial.
    """
    budget = worker_tree_budget_bytes(os.environ.get(WORKER_TREE_BUDGET_ENV, ""))
    if budget <= 0:
        return None
    available = compiled_native_worker_budget(budget, WORKER_COORDINATOR_RESERVE_BYTES)
    width = min(jobs(item_count), available // COMPILED_EMIT_WORKER_BASE_BYTES)
    if width < 1:
        raise FrontendWorkerContractError(
            "worker tree budget cannot admit one oversized compiled emit worker: available="
            + str(available) + " worker=" + str(COMPILED_EMIT_WORKER_BASE_BYTES)
        )
    byte_cap = (available - width * COMPILED_EMIT_WORKER_BASE_BYTES) // COMPILED_EMIT_PEAK_BYTES_PER_INPUT_BYTE
    if byte_cap < split_threshold_bytes():
        width = 1
        byte_cap = (available - COMPILED_EMIT_WORKER_BASE_BYTES) // COMPILED_EMIT_PEAK_BYTES_PER_INPUT_BYTE
    return width, byte_cap
