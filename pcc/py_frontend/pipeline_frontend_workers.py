"""Pure worker-budget and chunking policy for multi-module frontend codegen."""

from __future__ import annotations

import os
import subprocess


_TRUE_VALUES = ("1", "true", "yes", "on")
_AUTO_VALUES = ("auto", "on", "true", "yes")
WORKER_MANIFEST_V1 = "pcc.py_frontend.codegen_worker.v1"
WORKER_MANIFEST_V2 = "pcc.py_frontend.codegen_worker.v2"
WORKER_MANIFEST_V3 = "pcc.py_frontend.codegen_worker.v3"
WORKER_MANIFEST_V4 = "pcc.py_frontend.codegen_worker.v4"

# Source-Python workers retain the decoded ASTs, inferred types, LLVM builder
# state, and generated IR for every module assigned to their process.  One
# process per concurrency slot therefore turns a large compiler closure into a
# handful of long-lived heaps and leaves the other slots idle behind the
# slowest shard.  A small number of sequential shards per slot bounds that
# retained state while keeping interpreter/import startup amortized.  This is
# a chunk-count policy only: ``jobs`` remains the hard concurrency ceiling.
SOURCE_WORKER_CHUNKS_PER_JOB = 4

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
HOST_SOURCE_WORKER_AUTO_CAP = 10

# Export workers parse/lift one module and summary workers decode one AST and
# publish one compact effect wire.  They are short-lived one-module processes,
# unlike codegen workers which retain inferred types, builder state and IR.
# Same-source compiled checkpoint measurements put width-7 export+summary at
# 7.49 GB tree peak versus 7.01 GB for width 2.  Keep 7 GiB unavailable to
# these light lanes (the measured coordinator plus >1 GiB headroom), then
# charge 512 MiB per worker.  At the production 8 GiB envelope both lanes stay
# at the proven width 2; a larger shared envelope derives wider light lanes
# without changing the 3 GiB codegen risk class.
COMPILED_EXPORT_WORKER_PEAK_BYTES = 536870912
COMPILED_EXPORT_COORDINATOR_RESERVE_BYTES = 7516192768
COMPILED_EXPORT_AUTO_CAP = 10
COMPILED_SUMMARY_WORKER_PEAK_BYTES = 536870912
COMPILED_SUMMARY_COORDINATOR_RESERVE_BYTES = 7516192768
COMPILED_SUMMARY_AUTO_CAP = 10


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


def budget_jobs(
    cpu_budget: int,
    memory_budget_bytes: int,
    per_worker_peak_bytes: int,
    hard_cap: int,
) -> int:
    jobs = int(cpu_budget)
    cap = int(hard_cap)
    if jobs > cap:
        jobs = cap
    budget = int(memory_budget_bytes)
    if budget > 0:
        by_memory = (budget - WORKER_COORDINATOR_RESERVE_BYTES) // int(
            per_worker_peak_bytes
        )
        if by_memory < jobs:
            jobs = by_memory
    if jobs < 1:
        return 1
    return jobs


def frontend_jobs(job_count_hint: int, raw: str, cpu_budget: int) -> int:
    normalized = str(raw or "").strip().lower()
    if not normalized:
        normalized = "auto"
    if normalized in ("0", "off", "false", "no"):
        return 1
    if normalized in _AUTO_VALUES:
        jobs = budget_jobs(
            int(cpu_budget),
            worker_tree_budget_bytes(os.environ.get(WORKER_TREE_BUDGET_ENV, "")),
            HOST_SOURCE_WORKER_PEAK_BYTES,
            HOST_SOURCE_WORKER_AUTO_CAP,
        )
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
    selected = int(jobs)
    if selected < 1:
        return 1
    return budget_jobs(
        selected,
        worker_tree_budget_bytes(os.environ.get(WORKER_TREE_BUDGET_ENV, "")),
        COMPILED_SAFE_WORKER_PEAK_BYTES,
        SOURCE_WORKER_AUTO_SAFE_JOBS,
    )


def _compiled_native_light_jobs(
    jobs: int,
    worker_peak_bytes: int,
    coordinator_reserve_bytes: int,
    hard_cap: int,
) -> int:
    selected = int(jobs)
    if selected < 1:
        return 1
    if selected > int(hard_cap):
        selected = int(hard_cap)
    budget = worker_tree_budget_bytes(
        os.environ.get(WORKER_TREE_BUDGET_ENV, "")
    )
    if budget <= 0:
        if selected > SOURCE_WORKER_AUTO_SAFE_JOBS:
            return SOURCE_WORKER_AUTO_SAFE_JOBS
        return selected
    by_memory = (budget - int(coordinator_reserve_bytes)) // int(
        worker_peak_bytes
    )
    if by_memory < selected:
        selected = by_memory
    if selected < 1:
        return 1
    return selected


def compiled_native_export_jobs(jobs: int) -> int:
    """Derive the compiled export width from its measured memory class."""

    return _compiled_native_light_jobs(
        jobs,
        COMPILED_EXPORT_WORKER_PEAK_BYTES,
        COMPILED_EXPORT_COORDINATOR_RESERVE_BYTES,
        COMPILED_EXPORT_AUTO_CAP,
    )


def compiled_native_summary_jobs(jobs: int) -> int:
    """Derive the compiled summary width from its measured memory class."""

    return _compiled_native_light_jobs(
        jobs,
        COMPILED_SUMMARY_WORKER_PEAK_BYTES,
        COMPILED_SUMMARY_COORDINATOR_RESERVE_BYTES,
        COMPILED_SUMMARY_AUTO_CAP,
    )


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
    if worker_prefix:
        worker_executable = str(worker_prefix[0])
        worker_base = os.path.basename(worker_executable).lower()
        if not worker_base.startswith("python") and native_predicate(
            worker_executable
        ):
            return src_count
    if jobs < 1:
        jobs = 1
    chunk_count = jobs * SOURCE_WORKER_CHUNKS_PER_JOB
    if chunk_count > src_count:
        return src_count
    return chunk_count


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
) -> None:
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(WORKER_MANIFEST_V4 + "\n")
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


def read_worker_manifest(path: str):
    with open(path, "r", encoding="utf-8") as stream:
        lines = stream.read().splitlines()
    position = 0
    if not lines or lines[0] not in (
        WORKER_MANIFEST_V1,
        WORKER_MANIFEST_V2,
        WORKER_MANIFEST_V3,
        WORKER_MANIFEST_V4,
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
        if version == WORKER_MANIFEST_V4:
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

    from .worker_process_pool import run_worker_processes

    run_worker_processes(commands, max_parallel)
