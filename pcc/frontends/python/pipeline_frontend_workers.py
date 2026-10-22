"""Worker admission, resident-memory observations, and frontend chunking policy."""

from __future__ import annotations

import os
import subprocess
import sys

from pcc.extern import c_int64, extern


_native_current_rss_bytes = extern("pcc_os_current_rss_bytes", (), c_int64)
_native_peak_rss_bytes = extern("pcc_os_peak_rss_bytes", (), c_int64)
_native_gc_backend = extern("pcc_gc_backend", (), c_int64)


_TRUE_VALUES = ("1", "true", "yes", "on")
_AUTO_VALUES = ("auto", "on", "true", "yes")
WORKER_MANIFEST_V1 = "pcc.frontends.python.codegen_worker.v1"
WORKER_MANIFEST_V2 = "pcc.frontends.python.codegen_worker.v2"
WORKER_MANIFEST_V3 = "pcc.frontends.python.codegen_worker.v3"
WORKER_MANIFEST_V4 = "pcc.frontends.python.codegen_worker.v4"
WORKER_MANIFEST_V5 = "pcc.frontends.python.codegen_worker.v5"

# Export batches retain all lifted ASTs until publication. Keep their size
# independent of the number of active processes. Codegen is one module per
# task on both execution owners, so both use the existing indexed export view
# and no completed module's allocator/IR state survives into the next task.
SOURCE_WORKER_MODULES_PER_CHUNK = 8

# Source size is a deliberately cheap, pre-codegen proxy for the retained
# frontend heap.  In the compiler closure, sources at or above this boundary
# include the large type-inference/codegen tables whose isolated codegen takes
# a few seconds but whose wall time grows by an order of magnitude when ten of
# them start together.  Run those short-lived processes without overlapping
# their peak heaps; the remaining shards can use a small bounded pool.
SOURCE_WORKER_OVERSIZED_BYTES = 200_000
SOURCE_WORKER_AUTO_SAFE_JOBS = 2
SOURCE_WORKER_AST_OVERSIZED_BYTES = 6_000_000

# Unified worker admission: jobs = min(cpu budget, hard risk cap, memory
# budget / measured per-worker peak).  Executor differences enter only through
# the measured peak constants; there is no per-stage special case.  Peaks were
# measured on the 224-module compiler closure (docs/goal/evidence/
# HARNESS-P0-STAGE2-MEMORY-SAFE-DEFAULT/002-stage2-critical-path-prediction.md):
# host CPython frontend worker 1.7 GiB, compiled pcc1 safe-band worker
# <=2.5 GiB (oversized-band pcc1 workers reach 6.0 GiB and stay serialized by
# the source/AST split above this pool).  The budget arrives through
# PCC_WORKER_TREE_BUDGET_BYTES; an absent or unparsable value means "unknown"
# and preserves the historical cpu/hard-cap behavior.
WORKER_TREE_BUDGET_ENV = "PCC_WORKER_TREE_BUDGET_BYTES"
HOST_SOURCE_WORKER_PEAK_BYTES = 2147483648
COMPILED_SAFE_WORKER_PEAK_BYTES = 3221225472
WORKER_COORDINATOR_RESERVE_BYTES = 1073741824
# Reuse the existing frontend calibration's 128 MiB fixed launch/allocator
# allowance. A live parent is quiescent while the fixed-width pool runs; this
# covers pool command/env vectors and allocator rounding, not child memory.
WORKER_COORDINATOR_RSS_HEADROOM_BYTES = 134217728
HOST_SOURCE_WORKER_AUTO_CAP = 10

# Export workers parse/lift one module and summary workers decode one AST and
# publish one compact effect wire.  They are short-lived one-module processes,
# unlike codegen workers which retain inferred types, builder state and IR.
# All 392 compiler modules as singleton pcc1 export workers peaked at 310 MB
# RSS (pcc.frontends.c.parse.c_parsetab); 512 MiB per worker covers that by 25% + 128 MiB.
# The earlier 7 GiB reserve came from a coordinator that itself held ~6 GiB.
# In the current 392-module Stage2 the pcc1 coordinator stayed <= 1.51 GiB
# while export workers ran and <= 1.75 GiB during summaries (tree <= 2.50
# GiB), so 3 GiB covers it by 70%.  The 8 GiB envelope now admits 10 light
# workers without changing the 3 GiB codegen risk class.
COMPILED_EXPORT_WORKER_PEAK_BYTES = 536870912
COMPILED_EXPORT_COORDINATOR_RESERVE_BYTES = 3221225472
COMPILED_EXPORT_AUTO_CAP = 10
COMPILED_SUMMARY_WORKER_PEAK_BYTES = 536870912
COMPILED_SUMMARY_COORDINATOR_RESERVE_BYTES = 3221225472
COMPILED_SUMMARY_AUTO_CAP = 10
# Preload deltas decode the complete export graph in every worker. GC1 reached
# 3.3 GiB in the coordinator and 6.78 GiB with two children, while six children
# crossed the 8 GiB guard. Keep this lane separate from 512 MiB export workers.
COMPILED_PRELOAD_WORKER_PEAK_BYTES = 1879048192
COMPILED_PRELOAD_COORDINATOR_RESERVE_BYTES = 3758096384
COMPILED_PRELOAD_AUTO_CAP = 6


class FrontendWorkerContractError(ValueError):
    """A worker manifest or result violated the frontend process contract."""


def worker_tree_budget_bytes(raw: str) -> int:
    normalized = str(raw or "").strip()
    if not normalized:
        return 0
    try:
        budget = int(normalized)
    except ValueError:
        return 0
    if budget < 0:
        return 0
    return budget


def _host_coordinator_rss_bytes() -> int:
    """Keep the host compatibility entry outside native dependency discovery."""
    import sys as execution_runtime
    if execution_runtime.implementation.name != "pcc":
        from pcc.frontends.python.worker_resource_host_rss import coordinator_rss_bytes

        return coordinator_rss_bytes(sys, _host_windows_rss_bytes)
    raise RuntimeError("host RSS projection is unavailable under native pcc")


def _host_windows_rss_bytes(peak: bool) -> int:
    """Keep the host compatibility entry outside native dependency discovery."""
    import sys as execution_runtime
    if execution_runtime.implementation.name != "pcc":
        from pcc.frontends.python.worker_resource_host_rss import windows_rss_bytes

        return windows_rss_bytes(peak)
    raise RuntimeError("host RSS projection is unavailable under native pcc")


def _coordinator_rss_bytes() -> int:
    if sys.implementation.name == "pcc":
        # This is the existing owned Darwin/Linux boundary, not a host oracle.
        return _native_current_rss_bytes()
    return _host_coordinator_rss_bytes()


def _worker_peak_rss_bytes() -> int:
    if sys.implementation.name == "pcc":
        return _native_peak_rss_bytes()
    return _host_coordinator_rss_bytes()


def _worker_collector() -> int:
    if sys.implementation.name == "pcc":
        return _native_gc_backend()
    # Host projection for a command which will start a native worker. Native
    # callers above read the actual runtime selector, not its initial env hint.
    value = str(os.environ.get("PCC_GC_BACKEND", "0"))
    if value not in ("0", "1", "2", "3", "4"):
        raise FrontendWorkerContractError("invalid worker collector identity")
    return int(value)


def _resident_coordinator_reserve(configured_floor: int, observed_rss: int) -> int:
    if observed_rss <= 0:
        raise FrontendWorkerContractError(
            "budgeted worker admission requires coordinator RSS; "
            "RSS observation is unavailable on this platform"
        )
    return max(int(configured_floor), int(observed_rss) + WORKER_COORDINATOR_RSS_HEADROOM_BYTES)



def compiled_native_worker_budget(tree_budget_bytes: int, coordinator_floor_bytes: int) -> int:
    """Available bytes observed by the process which will actually spawn."""
    if tree_budget_bytes <= 0:
        return 0
    reserve = _resident_coordinator_reserve(coordinator_floor_bytes, _coordinator_rss_bytes())
    available = int(tree_budget_bytes) - reserve
    if available <= 0:
        raise FrontendWorkerContractError(
            "worker tree budget leaves no child memory: budget=" + str(tree_budget_bytes)
            + " coordinator=" + str(reserve)
        )
    return available


def _phase_budget_jobs(cpu_budget: int, memory_budget_bytes: int,
                       worker_peak_bytes: int, coordinator_reserve_bytes: int,
                       hard_cap: int) -> int:
    if worker_peak_bytes <= 0 or coordinator_reserve_bytes < 0:
        raise FrontendWorkerContractError("worker memory reservations must be positive")
    jobs = max(1, min(int(cpu_budget), int(hard_cap)))
    if memory_budget_bytes > 0:
        remaining = int(memory_budget_bytes) - int(coordinator_reserve_bytes)
        by_memory = remaining // int(worker_peak_bytes)
        if by_memory < 1:
            raise FrontendWorkerContractError(
                "worker memory budget cannot admit one task: budget="
                + str(memory_budget_bytes) + " coordinator="
                + str(coordinator_reserve_bytes) + " worker=" + str(worker_peak_bytes)
            )
        jobs = min(jobs, by_memory)
    return jobs


def budget_jobs(
    cpu_budget: int,
    memory_budget_bytes: int,
    per_worker_peak_bytes: int,
    hard_cap: int,
) -> int:
    return _phase_budget_jobs(
        cpu_budget, memory_budget_bytes, per_worker_peak_bytes,
        WORKER_COORDINATOR_RESERVE_BYTES, hard_cap,
    )


def frontend_jobs(job_count_hint: int, raw: str, cpu_budget: int) -> int:
    normalized = str(raw or "").strip().lower()
    if not normalized:
        normalized = "auto"
    if normalized in ("0", "off", "false", "no"):
        return 1
    if normalized in _AUTO_VALUES:
        # This is a CPU ceiling. Real admission belongs to the spawning
        # process, once actual graph/AST inputs and its resident RSS are known.
        jobs = max(1, min(int(cpu_budget), HOST_SOURCE_WORKER_AUTO_CAP))
    else:
        try:
            jobs = int(normalized)
        except ValueError:
            jobs = 1
    if jobs < 2 or job_count_hint < 2:
        return 1
    if jobs > job_count_hint:
        jobs = job_count_hint
    return jobs


def compiled_native_auto_jobs(jobs: int) -> int:
    """Bound an automatic compiled-worker lane by its memory contract."""
    budget = worker_tree_budget_bytes(os.environ.get(WORKER_TREE_BUDGET_ENV, ""))
    reserve = WORKER_COORDINATOR_RESERVE_BYTES
    if budget > 0:
        reserve = _resident_coordinator_reserve(reserve, _coordinator_rss_bytes())
    return _phase_budget_jobs(
        jobs, budget, COMPILED_SAFE_WORKER_PEAK_BYTES,
        reserve, SOURCE_WORKER_AUTO_SAFE_JOBS,
    )


def _compiled_native_light_jobs(
    jobs: int,
    worker_peak_bytes: int,
    coordinator_reserve_bytes: int,
    hard_cap: int,
) -> int:
    selected = max(1, min(int(jobs), int(hard_cap)))
    budget = worker_tree_budget_bytes(os.environ.get(WORKER_TREE_BUDGET_ENV, ""))
    if budget <= 0:
        return min(selected, SOURCE_WORKER_AUTO_SAFE_JOBS)
    reserve = _resident_coordinator_reserve(
        coordinator_reserve_bytes, _coordinator_rss_bytes(),
    )
    # Fixed-width admission uses estimates, not enforceable RSS leases. The
    # enclosing process-tree guard remains required if a worker exceeds them.
    return _phase_budget_jobs(selected, budget, worker_peak_bytes, reserve, hard_cap)


def compiled_native_export_jobs(jobs: int) -> int:
    """Derive the compiled export width from its measured memory class."""

    return _compiled_native_light_jobs(
        jobs,
        COMPILED_EXPORT_WORKER_PEAK_BYTES,
        COMPILED_EXPORT_COORDINATOR_RESERVE_BYTES,
        COMPILED_EXPORT_AUTO_CAP,
    )


def compiled_native_summary_jobs(jobs: int) -> int:
    """Baseline summary cap; the input-aware path uses summary_plan below."""

    return _compiled_native_light_jobs(
        jobs,
        COMPILED_SUMMARY_WORKER_PEAK_BYTES,
        COMPILED_SUMMARY_COORDINATOR_RESERVE_BYTES,
        COMPILED_SUMMARY_AUTO_CAP,
    )


def compiled_native_preload_jobs(jobs: int) -> int:
    """Bound whole-export-graph preload workers by the compiler tree budget."""
    return _compiled_native_light_jobs(
        jobs,
        COMPILED_PRELOAD_WORKER_PEAK_BYTES,
        COMPILED_PRELOAD_COORDINATOR_RESERVE_BYTES,
        COMPILED_PRELOAD_AUTO_CAP,
    )



def summary_worker_peak_bytes(ast_bytes: int, export_bytes: int, collector: int) -> int:
    """Sample-calibrated reservation, not an enforced resident-memory limit.

    The same largest eight-module batch was replayed under GC0..4. Coefficients
    20/40/40/26/44 bytes per input byte cover all observed peaks before the
    existing 25% + 128 MiB margin. Include both AST and shared effect wire;
    exports-size extrapolation remains an estimate. See the retained fixture.
    """
    if ast_bytes < 0 or export_bytes < 0:
        raise FrontendWorkerContractError("negative summary input size")
    if collector == 0:
        expansion = 20
    elif collector == 1 or collector == 2:
        expansion = 40
    elif collector == 3:
        expansion = 26
    elif collector == 4:
        expansion = 44
    else:
        raise FrontendWorkerContractError("invalid summary collector identity")
    working = 134217728 + (int(ast_bytes) + int(export_bytes)) * expansion
    reservation = (working * 5 + 3) // 4 + 134217728
    return max(COMPILED_SUMMARY_WORKER_PEAK_BYTES, reservation)


def _summary_memory_chunks(ast_sizes, export_bytes: int, collector: int,
                           available_bytes: int):
    chunks = []
    chunk = []
    ast_bytes = 0
    for index in range(len(ast_sizes)):
        size = int(ast_sizes[index])
        if size < 0:
            raise FrontendWorkerContractError("negative summary AST size")
        peak = summary_worker_peak_bytes(ast_bytes + size, export_bytes, collector)
        if chunk and (len(chunk) >= 8 or (available_bytes >= 0 and peak > available_bytes)):
            chunks.append(chunk)
            chunk = []
            ast_bytes = 0
            peak = summary_worker_peak_bytes(size, export_bytes, collector)
        if available_bytes >= 0 and peak > available_bytes:
            raise FrontendWorkerContractError(
                "summary budget cannot admit one module: index=" + str(index)
                + " ast_bytes=" + str(size) + " worker=" + str(peak)
                + " available=" + str(available_bytes)
            )
        chunk.append(index)
        ast_bytes += size
    if chunk:
        chunks.append(chunk)
    return chunks


def compiled_native_summary_plan(jobs: int, ast_sizes, export_bytes: int):
    """Plan semantic-preserving batches and one conservative fixed pool width."""
    budget = worker_tree_budget_bytes(os.environ.get(WORKER_TREE_BUDGET_ENV, ""))
    collector = _worker_collector()
    reserve = COMPILED_SUMMARY_COORDINATOR_RESERVE_BYTES
    observed_rss = -1
    available = -1
    if budget > 0:
        observed_rss = _coordinator_rss_bytes()
        reserve = _resident_coordinator_reserve(reserve, observed_rss)
        available = max(0, budget - reserve)
    chunks = _summary_memory_chunks(ast_sizes, export_bytes, collector, available)
    largest = 0
    for chunk in chunks:
        size = 0
        for index in chunk:
            size += int(ast_sizes[index])
        largest = max(largest, summary_worker_peak_bytes(size, export_bytes, collector))
    selected = min(max(1, int(jobs)), COMPILED_SUMMARY_AUTO_CAP)
    if budget > 0 and chunks:
        selected = _phase_budget_jobs(selected, budget, largest, reserve, COMPILED_SUMMARY_AUTO_CAP)
    elif budget <= 0:
        selected = min(selected, SOURCE_WORKER_AUTO_SAFE_JOBS)
    details = {
        "schema": "pcc.summary-worker-admission.v1", "collector": collector,
        # The same snapshot must select both cost model and child runtime.
        "worker_env": "PCC_GC_BACKEND=" + str(collector),
        "tree_budget_bytes": budget, "coordinator_rss_bytes": observed_rss,
        "coordinator_reserve_bytes": reserve, "worker_peak_reservation_bytes": largest,
        "effect_wire_bytes": int(export_bytes), "chunks": len(chunks), "jobs": selected,
        "rss_enforcement": "outer-process-tree-guard", "pool": "fixed-width",
        "rss_source": "owned-current" if sys.implementation.name == "pcc" else "host-high-water",
    }
    return chunks, selected, details


def numeric_jobs_override(raw: str) -> bool:
    normalized = str(raw or "").strip().lower()
    return bool(normalized and normalized not in _AUTO_VALUES)


def worker_timing_enabled(raw: str) -> bool:
    return str(raw or "").strip().lower() in _TRUE_VALUES


def worker_env_prefix(*, timing_enabled: bool) -> str:
    prefix = "PCC_PY_FRONTEND_JOBS=1"
    if timing_enabled:
        prefix += " PCC_PY_FRONTEND_WORKER_TIMING=1"
    return prefix


def ast_wire_enabled(raw: str) -> bool:
    return str(raw or "").strip().lower() in _TRUE_VALUES


def is_native_worker_executable(path: str) -> bool:
    try:
        with open(path, "rb") as stream:
            magic = stream.read(4)
    except OSError:
        return True
    if magic == b"\x7fELF":
        return True
    if magic in (
        b"\xfe\xed\xfa\xce",
        b"\xce\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf",
        b"\xcf\xfa\xed\xfe",
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
    ):
        return True
    if magic.startswith(b"#!"):
        return False
    return True


def worker_executable_candidates(
    sys_executable: str,
    argv_zero: str,
) -> tuple[str, ...]:
    candidates = []
    for candidate in (sys_executable, argv_zero):
        text = str(candidate or "")
        if not text or text in candidates:
            continue
        candidates.append(text)
    return tuple(candidates)


def own_worker_prefixes(implementation=None, executable=None, argv_zero=None) -> list:
    """Command prefixes this compiler spawns for its own frontend workers.

    CPython runs ``python -m pcc``; a native pcc runs its own executable, which
    the pipeline selects from ``sys.executable`` and ``argv[0]``. Arguments
    default to the running process.
    """
    if implementation is None:
        implementation = sys.implementation.name
    if executable is None:
        try:
            executable = str(getattr(sys, "executable", "") or "")
        except Exception:
            executable = ""
    if argv_zero is None:
        argv_zero = ""
        try:
            if len(sys.argv) > 0:
                argv_zero = str(sys.argv[0] or "")
        except Exception:
            pass
    if implementation == "cpython":
        return [[executable, "-m", "pcc"]] if executable else []
    if implementation != "pcc":
        return []
    return [[candidate] for candidate in worker_executable_candidates(executable, argv_zero)]


def select_native_worker_executable(
    candidates: tuple[str, ...],
    *,
    native_predicate,
) -> str:
    for executable in candidates:
        if executable.endswith(".py"):
            continue
        base = os.path.basename(executable).lower()
        if base.startswith("python"):
            continue
        if os.path.isfile(executable) and native_predicate(executable):
            return executable
    return ""


def codegen_chunks(src_paths, jobs: int):
    weighted = []
    index = 0
    while index < len(src_paths):
        try:
            with open(src_paths[index], "r", encoding="utf-8") as stream:
                weight = len(stream.read())
        except OSError:
            weight = 1
        insert_at = 0
        while insert_at < len(weighted) and weighted[insert_at][0] >= weight:
            insert_at += 1
        weighted.insert(insert_at, (weight, index))
        index += 1

    chunks = []
    totals = []
    index = 0
    while index < jobs:
        chunks.append([])
        totals.append(0)
        index += 1
    for weight, source_index in weighted:
        target = 0
        scan = 1
        while scan < len(totals):
            if totals[scan] < totals[target]:
                target = scan
            scan += 1
        chunks[target].append(source_index)
        totals[target] += weight

    result = []
    for chunk in chunks:
        if chunk:
            chunk.sort()
            result.append(chunk)
    return result


def split_codegen_chunks_by_source_size(
    src_paths,
    chunks,
    threshold_bytes: int = SOURCE_WORKER_OVERSIZED_BYTES,
    *,
    sidecar_dir: str = "",
    sidecar_threshold_bytes: int = SOURCE_WORKER_AST_OVERSIZED_BYTES,
):
    """Extract oversized source/AST inputs into descending singleton chunks.

    ``chunks`` already owns stable, source-order indices.  Safe residual
    chunks retain that order; oversized modules are largest-first so the
    highest peak is released before any safe worker starts.  Once export has
    published AST sidecars, their byte size supplements source size: generated
    compact source can expand into a much larger compiler object graph.  A
    missing source/sidecar remains a normal worker error later rather than
    being hidden by scheduling policy.
    """

    threshold = int(threshold_bytes)
    if threshold < 1:
        threshold = 1
    oversized = []
    safe_chunks = []
    for chunk in chunks:
        safe_chunk = []
        for source_index in chunk:
            try:
                source_bytes = os.path.getsize(src_paths[source_index])
            except OSError:
                source_bytes = 0
            sidecar_bytes = 0
            if sidecar_dir:
                sidecar_path = os.path.join(
                    sidecar_dir,
                    "module_" + str(source_index) + ".json",
                )
                try:
                    sidecar_bytes = os.path.getsize(sidecar_path)
                except OSError:
                    sidecar_bytes = 0
            oversized_weight = source_bytes
            is_oversized = source_bytes >= threshold
            if sidecar_bytes >= int(sidecar_threshold_bytes):
                is_oversized = True
                if sidecar_bytes > oversized_weight:
                    oversized_weight = sidecar_bytes
            if is_oversized:
                oversized.append((oversized_weight, int(source_index)))
            else:
                safe_chunk.append(source_index)
        if safe_chunk:
            safe_chunks.append(safe_chunk)
    oversized.sort(key=lambda item: (-item[0], item[1]))
    return [[source_index] for _size, source_index in oversized], safe_chunks


def codegen_chunk_count(
    src_count: int,
    jobs: int,
    worker_prefix,
    *,
    native_predicate,
) -> int:
    src_count = int(src_count)
    jobs = int(jobs)
    if src_count <= 1:
        return 1
    # A smaller machine must not receive larger heaps or a different export
    # view. Singleton host/native workers share the same dependency closure.
    return src_count


def write_worker_manifest(
    path: str,
    result_path: str,
    ir_dir: str,
    exports_path: str,
    ast_dir: str,
    src_paths,
    module_names,
    assigned_indices,
    *,
    entry_module: str,
    sibling_inits,
    libpython_mode: str,
    ir_scaffold_mode: str,
    verbose: bool,
    job_kind: str = "codegen",
    checkpoint_root: str = "",
    checkpoint_build_digest: str = "",
    checkpoint_graph_digest: str = "",
    checkpoint_skip_indices=(),
) -> None:
    if checkpoint_root:
        _validate_checkpoint_manifest(
            checkpoint_root,
            checkpoint_build_digest,
            checkpoint_graph_digest,
            assigned_indices,
            checkpoint_skip_indices,
            len(module_names),
            job_kind,
        )
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(
            (WORKER_MANIFEST_V5 if checkpoint_root else WORKER_MANIFEST_V4)
            + "\n"
        )
        stream.write(result_path + "\n")
        stream.write(ir_dir + "\n")
        stream.write(exports_path + "\n")
        stream.write(job_kind + "\n")
        stream.write(ast_dir + "\n")
        stream.write(entry_module + "\n")
        stream.write(libpython_mode + "\n")
        stream.write(ir_scaffold_mode + "\n")
        stream.write("1\n" if verbose else "0\n")
        stream.write(str(len(sibling_inits)) + "\n")
        for module_name in sibling_inits:
            stream.write(str(module_name) + "\n")
        stream.write(str(len(src_paths)) + "\n")
        index = 0
        while index < len(src_paths):
            stream.write(
                str(index)
                + "\t"
                + str(module_names[index])
                + "\t"
                + str(src_paths[index])
                + "\n"
            )
            index += 1
        stream.write(str(len(assigned_indices)) + "\n")
        for index in assigned_indices:
            stream.write(str(index) + "\n")
        if checkpoint_root:
            stream.write(checkpoint_root + "\n")
            stream.write(checkpoint_build_digest + "\n")
            stream.write(checkpoint_graph_digest + "\n")
            stream.write(str(len(checkpoint_skip_indices)) + "\n")
            for index in checkpoint_skip_indices:
                stream.write(str(index) + "\n")


def _validate_checkpoint_manifest(
    root, build_digest, graph_digest, assigned_indices, skip_indices,
    module_count, job_kind,
) -> None:
    if (
        job_kind != "codegen"
        or not os.path.isabs(root)
        or "\n" in root
        or "\r" in root
    ):
        raise FrontendWorkerContractError("invalid Stage1 checkpoint worker root")
    for digest in (build_digest, graph_digest):
        if len(digest) != 64 or any(
            char not in "0123456789abcdef" for char in digest
        ):
            raise FrontendWorkerContractError("invalid Stage1 checkpoint identity")
    seen = set()
    for index in assigned_indices:
        if type(index) is not int or index < 0 or index >= module_count or index in seen:
            raise FrontendWorkerContractError("invalid Stage1 checkpoint assignment")
        seen.add(index)
    if not seen:
        raise FrontendWorkerContractError("empty Stage1 checkpoint assignment")
    skipped = set()
    for index in skip_indices:
        if type(index) is not int or index not in seen or index in skipped:
            raise FrontendWorkerContractError("invalid Stage1 checkpoint skip mask")
        skipped.add(index)


def read_worker_manifest(path: str):
    with open(path, "r", encoding="utf-8") as stream:
        lines = stream.read().splitlines()
    position = 0
    if not lines or lines[0] not in (
        WORKER_MANIFEST_V1,
        WORKER_MANIFEST_V2,
        WORKER_MANIFEST_V3,
        WORKER_MANIFEST_V4,
        WORKER_MANIFEST_V5,
    ):
        raise FrontendWorkerContractError(
            "invalid frontend codegen worker manifest"
        )
    version = lines[position]
    position += 1
    try:
        result_path = lines[position]
        position += 1
        ir_dir = lines[position]
        position += 1
        exports_path = ""
        if version == WORKER_MANIFEST_V2:
            exports_path = lines[position]
            position += 1
        job_kind = "codegen"
        ast_dir = ""
        if version == WORKER_MANIFEST_V3:
            exports_path = lines[position]
            position += 1
            job_kind = lines[position]
            position += 1
        if version in (WORKER_MANIFEST_V4, WORKER_MANIFEST_V5):
            exports_path = lines[position]
            position += 1
            job_kind = lines[position]
            position += 1
            ast_dir = lines[position]
            position += 1
        entry_module = lines[position]
        position += 1
        libpython_mode = lines[position]
        position += 1
        ir_scaffold_mode = lines[position]
        position += 1
        verbose = lines[position] == "1"
        position += 1
        sibling_count = int(lines[position])
        position += 1
        sibling_inits = []
        index = 0
        while index < sibling_count:
            sibling_inits.append(lines[position])
            position += 1
            index += 1
        module_count = int(lines[position])
        position += 1
        src_paths = []
        module_names = []
        index = 0
        while index < module_count:
            parts = lines[position].split("\t", 2)
            if len(parts) != 3:
                raise FrontendWorkerContractError(
                    "invalid frontend worker module entry"
                )
            if version == WORKER_MANIFEST_V5 and parts[0] != str(index):
                raise FrontendWorkerContractError("invalid Stage1 checkpoint module ordinal")
            src_paths.append(parts[2])
            module_names.append(parts[1])
            position += 1
            index += 1
        assigned_count = int(lines[position])
        position += 1
        assigned_indices = []
        index = 0
        while index < assigned_count:
            assigned_indices.append(int(lines[position]))
            position += 1
            index += 1
        checkpoint_root = ""
        checkpoint_build_digest = ""
        checkpoint_graph_digest = ""
        checkpoint_skip_indices = []
        if version == WORKER_MANIFEST_V5:
            checkpoint_root = lines[position]
            checkpoint_build_digest = lines[position + 1]
            checkpoint_graph_digest = lines[position + 2]
            position += 3
            skip_count = int(lines[position])
            position += 1
            if skip_count < 0:
                raise ValueError("negative checkpoint skip count")
            index = 0
            while index < skip_count:
                checkpoint_skip_indices.append(int(lines[position]))
                position += 1
                index += 1
            _validate_checkpoint_manifest(
                checkpoint_root,
                checkpoint_build_digest,
                checkpoint_graph_digest,
                assigned_indices,
                checkpoint_skip_indices,
                len(module_names),
                job_kind,
            )
    except (IndexError, ValueError) as exc:
        raise FrontendWorkerContractError(
            "truncated or malformed frontend codegen worker manifest"
        ) from exc
    if position != len(lines):
        raise FrontendWorkerContractError(
            "frontend codegen worker manifest has trailing records"
        )
    return {
        "result_path": result_path,
        "ir_dir": ir_dir,
        "exports_path": exports_path,
        "ast_dir": ast_dir,
        "job_kind": job_kind,
        "entry_module": entry_module,
        "libpython_mode": libpython_mode,
        "ir_scaffold_mode": ir_scaffold_mode,
        "verbose": verbose,
        "sibling_inits": tuple(sibling_inits),
        "src_paths": src_paths,
        "module_names": module_names,
        "assigned_indices": assigned_indices,
        "checkpoint_root": checkpoint_root,
        "checkpoint_build_digest": checkpoint_build_digest,
        "checkpoint_graph_digest": checkpoint_graph_digest,
        "checkpoint_skip_indices": checkpoint_skip_indices,
    }


def write_worker_error(result_path: str, message: str) -> None:
    safe = str(message).replace("\t", " ").replace("\n", " ")
    with open(result_path, "w", encoding="utf-8") as stream:
        stream.write("ERR\t" + safe + "\n")


def read_worker_ir(ir_path: str, module_name: str) -> str:
    with open(ir_path, "r", encoding="utf-8") as stream:
        ir_text = stream.read()
    if len(ir_text) == 0:
        raise FrontendWorkerContractError(
            "frontend codegen worker produced empty LLVM IR for module "
            + module_name
        )
    return ir_text


def safe_exception_text(exc) -> str:
    try:
        text = str(exc)
    except Exception:
        text = ""
    if text is None:
        return ""
    return text


def shell_quote_arg(text: str) -> str:
    text = str(text)
    if text == "":
        return "''"
    safe = True
    index = 0
    while index < len(text):
        char = text[index]
        ok = (
            ("a" <= char <= "z")
            or ("A" <= char <= "Z")
            or ("0" <= char <= "9")
            or char in "/._-+=:,@%"
        )
        if not ok:
            safe = False
            break
        index += 1
    if safe:
        return text
    output = "'"
    index = 0
    while index < len(text):
        char = text[index]
        if char == "'":
            output += "'\"'\"'"
        else:
            output += char
        index += 1
    return output + "'"


def run_worker_commands(commands, max_parallel=None) -> None:
    commands = list(commands)
    if not commands:
        return
    if max_parallel is None:
        max_parallel = len(commands)
    try:
        max_parallel = int(max_parallel)
    except (TypeError, ValueError):
        max_parallel = 1
    if max_parallel < 1:
        max_parallel = 1
    if max_parallel > len(commands):
        max_parallel = len(commands)

    from pcc.frontends.python.worker_process_pool import (
        run_resource_worker_processes, run_worker_processes,
    )

    budget = worker_tree_budget_bytes(os.environ.get(WORKER_TREE_BUDGET_ENV, ""))
    if budget > 0:
        tasks = resource_tasks_for_commands(commands)
        if tasks is not None:
            run_resource_worker_processes(
                commands, tasks, max_parallel, budget,
                trace_path=str(tasks[0]["report_path"]) + ".admission.tsv",
            )
            return
    run_worker_processes(commands, max_parallel)


def _artifact_size(path: str) -> int:
    with open(path, "rb") as stream:
        stream.seek(0, 2)
        return int(stream.tell())


def resource_tasks_for_commands(commands):
    """Describe the actual worker inputs without changing their export view.

    Source, AST and export sizes are demand features, not RSS bytes. The
    compiler's own worker commands, CPython or native pcc, get a soft empirical
    prior, separate from explicit reservations. Unknown owners retain exclusive
    measured calibration.
    """
    import shlex
    from pcc.frontends.python.pipeline_stage1_checkpoint import file_sha256
    from pcc.frontends.python.worker_process_pool import _command_spec

    worker_arg = "--pcc-python-multi-codegen-worker"
    preload_arg = "--pcc-preload-delta-worker"
    own_prefixes = own_worker_prefixes()
    tasks = []
    input_hashes = {}
    for command in commands:
        argv = shlex.split(command)
        if preload_arg in argv:
            position = argv.index(preload_arg)
            if worker_arg in argv or argv.count(preload_arg) != 1:
                raise FrontendWorkerContractError("ambiguous frontend worker command")
            if position < 1 or len(argv) != position + 4:
                raise FrontendWorkerContractError(
                    "preload delta worker requires exports, roots and output paths"
                )
            exports_path, roots_path, out_path = argv[position + 1:]
            with open(roots_path, "r", encoding="utf-8") as stream:
                roots = [line for line in stream.read().splitlines() if line]
            identities = []
            for input_path in (exports_path, roots_path):
                if input_path not in input_hashes:
                    input_hashes[input_path] = file_sha256(input_path)
                identities.append(input_hashes[input_path])
            # Every preload task uses this complete graph. Keep its reader
            # class separate from indexed codegen and bind measurements to
            # this graph, execution owner and collector in the private state.
            owner = " ".join(argv[:position])
            tasks.append({
                "class": (
                    os.path.dirname(roots_path) + "|" + owner
                    + "|preload-delta|full-graph|"
                    + str(os.environ.get("PCC_GC_BACKEND", "0"))
                    + "|" + identities[0]
                ),
                "inputs": [_artifact_size(exports_path), _artifact_size(roots_path), len(roots)],
                "estimate_bytes": 0,
                "report_path": out_path + ".rss",
                "diagnostic_phase": "preload-delta",
                "diagnostic_modules": roots,
                "diagnostic_indices": [],
                "restartable": True,
                "source_identity": "|".join(identities),
            })
            continue
        if worker_arg not in argv:
            return None
        position = argv.index(worker_arg)
        if position < 1 or argv.count(worker_arg) != 1 or len(argv) != position + 2:
            raise FrontendWorkerContractError("frontend worker requires one manifest argument")
        path = argv[position + 1]
        manifest = read_worker_manifest(path)
        indices = manifest["assigned_indices"]
        source_sizes = [_artifact_size(manifest["src_paths"][index]) for index in indices]
        ast_sizes = []
        ast_dir = str(manifest.get("ast_dir", "") or "")
        if manifest["job_kind"] != "export" and ast_dir:
            ast_sizes = [_artifact_size(os.path.join(ast_dir, "module_" + str(index) + ".json"))
                         for index in indices]
        exports_path = str(manifest.get("exports_path", "") or "")
        export_bytes = _artifact_size(exports_path) if exports_path else 0
        identity_paths = [path] + [manifest["src_paths"][index] for index in indices]
        if manifest["job_kind"] != "export" and ast_dir:
            identity_paths.extend(os.path.join(ast_dir, "module_" + str(index) + ".json") for index in indices)
        if exports_path:
            identity_paths.append(exports_path)
        identities = []
        for input_path in identity_paths:
            if input_path not in input_hashes:
                input_hashes[input_path] = file_sha256(input_path)
            identities.append(input_hashes[input_path])
        # The command prefix identifies the execution owner, including a
        # selected native compiler; the private manifest directory isolates
        # samples from another compile with different source/options.
        owner = " ".join(argv[:position])
        execution_class = (
            os.path.dirname(path) + "|" + owner + "|" + manifest["job_kind"]
            + "|" + ("module-indexed" if len(indices) == 1 else "full-graph")
            + "|" + str(os.environ.get("PCC_GC_BACKEND", "0"))
            + "|" + str(os.environ.get("PCC_PYTHON_IR_PASSES", ""))
            + "|" + str(os.environ.get("PCC_DIRECT_INDEXED_SIDECAR", ""))
            + "|" + str(os.environ.get("PCC_DIRECT_INDEXED_KERNEL_RELEASE_FRONTEND", ""))
        )
        tasks.append({
            "class": execution_class,
            "inputs": [sum(source_sizes), max(source_sizes, default=0),
                       sum(ast_sizes), max(ast_sizes, default=0), export_bytes, len(indices)],
            "estimate_bytes": 0,
            "report_path": path + ".rss",
            "diagnostic_phase": manifest["job_kind"],
            "diagnostic_modules": [manifest["module_names"][index] for index in indices],
            "diagnostic_indices": list(indices),
            "restartable": True,
            "source_identity": "|".join(identities),
        })
        # Match the exact prefix this compiler spawns for its own workers. A
        # wrapper, custom command, other executable or different interpreter
        # cannot inherit these peak assumptions just because its manifest has
        # the same size.
        if (_command_spec(command)[0] in [prefix + [worker_arg, path] for prefix in own_prefixes]
                and manifest["job_kind"] in ("export", "summary", "codegen")):
            tasks[-1]["startup_prior_model"] = "host-" + manifest["job_kind"] + "-v1"
            if manifest["job_kind"] == "codegen" and len(indices) > 1:
                tasks[-1]["class"] += "|host-codegen-batch:" + str(len(indices))
    return tasks
