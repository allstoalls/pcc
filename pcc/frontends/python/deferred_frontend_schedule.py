"""Memory-bounded scheduling after the native frontend coordinator exits.

Frontend and PCO publication use their existing phase-specific admission
estimates. The combined frontend/emitter's old two-slot limit does not
describe these fresh-process phases.
"""

import os

from pcc.frontends.python.pipeline_exports import _indexed_native_export_metadata, _indexed_native_export_rows
from pcc.frontends.python.pipeline_pass_config import parallel_cpu_budget
from pcc.frontends.python.pipeline_frontend_workers import (
    compiled_native_auto_jobs, compiled_native_worker_budget,
    resource_tasks_for_commands,
)
from pcc.frontends.python.worker_process_pool import (
    run_chained_worker_processes, run_resource_worker_processes,
    run_weighted_worker_processes, run_worker_processes,
)


_GIB = 1073741824
_DRIVER_RESERVE = _GIB
_FRONTEND_BASE = 3 * _GIB // 4
_FRONTEND_GC0_PER_AST_MB = _GIB // 10
_FRONTEND_LEGACY_PER_AST_MB = 19 * _GIB // 100
# GC0 with the module's export-dependency closure known.  A worker loads the
# exports of that closure, so small modules with deep imports carry the high
# peaks that forced the AST-only base up to 768 MiB for every module.
_MIB = 1048576
_FRONTEND_GC0_CLOSURE_BASE = 482 * _MIB
_FRONTEND_GC0_CLOSURE_PER_AST_MB = 125 * _MIB
_FRONTEND_GC0_PER_CLOSURE_MB = 17 * _MIB
_PCO_BASE = 320 * 1048576
_PCO_PER_SIDECAR_MB = 21 * 1048576
_PCO_LEGACY_BASE = _GIB // 4
_PCO_LEGACY_PER_SIDECAR_MB = 13 * _GIB // 100
_PCO_CAP = 6 * _GIB
_MAX_WIDTH = 12


def _call_node_score(payload: bytes) -> int:
    """Count the exact AST wire marker.

    pcc1 lowers ``bytes.count`` to the runtime's raw-counter scan.  The former
    pcc-Python byte loop advanced a boxed exact-int index per byte: 27 s of the
    native deferred driver scoring all 427 MB of AST before the first worker.
    """
    return payload.count(b'"Call"')


def indexed_frontend_floor_bytes(ast_bytes, gc_backend=0, closure_bytes=-1):
    # GC0: all 392 frozen compiler workers, measured with system-reported RSS
    # and footprint high watermarks. Reserve 25% plus 128 MiB over the larger
    # observed peak. Other collectors retain the uncalibrated legacy floor.
    # With the export-dependency closure size the same envelope holds at a
    # 482 MiB base (tests/data/frontend_gc0_worker_closures.json).
    if gc_backend == 0 and int(closure_bytes) >= 0:
        return (
            _FRONTEND_GC0_CLOSURE_BASE
            + max(0, int(ast_bytes)) * _FRONTEND_GC0_CLOSURE_PER_AST_MB // 1000000
            + int(closure_bytes) * _FRONTEND_GC0_PER_CLOSURE_MB // 1000000
        )
    per_mb = (
        _FRONTEND_GC0_PER_AST_MB if gc_backend == 0
        else _FRONTEND_LEGACY_PER_AST_MB
    )
    return _FRONTEND_BASE + max(0, int(ast_bytes)) * per_mb // 1000000


def export_closure_bytes(exports_path, module_names):
    """Size of each module's export-dependency closure, or None.

    Sums the indexed export payloads over the module's ``P`` dependency
    closure, itself included -- the exports its frontend worker loads.  Any
    unreadable or non-indexed exports file keeps the AST-only estimate.
    """
    try:
        with open(exports_path, "r", encoding="utf-8") as stream:
            text = stream.read()
        _metadata, _preloads, payloads, _order = _indexed_native_export_rows(text)
        dependencies = _indexed_native_export_metadata(_metadata, "P")
    except (OSError, ValueError):
        return None
    if not isinstance(dependencies, dict):
        return None
    sizes = {}
    for name in payloads:
        sizes[name] = len(payloads[name])
    out = []
    for root in module_names:
        if root not in sizes:
            return None
        seen = set()
        pending = [root]
        total = 0
        while pending:
            name = pending.pop()
            if name in seen:
                continue
            seen.add(name)
            total += sizes.get(name, 0)
            for dependency in dependencies.get(name, ()):
                pending.append(dependency)
        out.append(total)
    return out


def indexed_pco_floor_bytes(sidecar_bytes, gc_backend=0):
    # GC0: all 392 frozen compiler PIDX inputs, system-reported per-process
    # RSS AND footprint maxima, each covered by 25% plus 128 MiB headroom.
    # The regression fixture retains the complete envelope. Other collectors
    # keep the previous v57 estimate until their own corpus is calibrated.
    base = _PCO_BASE if gc_backend == 0 else _PCO_LEGACY_BASE
    per_mb = _PCO_PER_SIDECAR_MB if gc_backend == 0 else _PCO_LEGACY_PER_SIDECAR_MB
    return min(_PCO_CAP, base + max(0, int(sidecar_bytes)) * per_mb // 1000000)


def _admission_groups(floors, tree_budget, cpu_budget):
    """Group equal-admission-width jobs without wave straggler serialization.

    Every member's reservation fits ``available // width``. Consequently any
    sliding window of ``width`` members in that group fits the same budget;
    no live-RSS subprocess or untracked GC pointer is needed in the scheduler.
    The external tree-RSS watchdog remains the backstop for estimate errors.
    An individually oversized job runs alone, as in the existing scheduler.
    """
    available = max(0, int(tree_budget) - _DRIVER_RESERVE)
    cap = max(1, min(int(cpu_budget), _MAX_WIDTH))
    groups = [[] for _ in range(cap + 1)]
    for index, floor in enumerate(floors):
        width = max(1, min(cap, available // floor))
        groups[width].append(index)
    return [(items, width) for width, items in enumerate(groups) if items]


def frontend_groups(ast_sizes, tree_budget, cpu_budget, gc_backend=0):
    return _admission_groups(
        [indexed_frontend_floor_bytes(size, gc_backend) for size in ast_sizes],
        tree_budget, cpu_budget,
    )


def pco_groups(sidecar_sizes, tree_budget, cpu_budget, gc_backend=0):
    return _admission_groups(
        [indexed_pco_floor_bytes(size, gc_backend) for size in sidecar_sizes], tree_budget, cpu_budget,
    )


def _manifest_module_name(lines, index):
    """Module name at ``index`` in a v4 worker manifest's module table, or ""."""
    try:
        sibling_count = int(lines[10])
        table = 12 + sibling_count
        if int(lines[table - 1]) <= index:
            return ""
        parts = lines[table + index].split("\t", 2)
    except (IndexError, ValueError):
        return ""
    if len(parts) != 3 or parts[0] != str(index):
        return ""
    return parts[1]


def run_pco_commands(commands, sidecars, oversized, safe_jobs):
    if len(commands) != len(sidecars):
        raise ValueError("PCO command/sidecar inventory mismatch")
    if not commands:
        return
    resource_pool = _resource_width_and_budget(safe_jobs)
    if resource_pool is not None:
        tasks = _pco_resource_tasks(sidecars)
        run_resource_worker_processes(commands, tasks, resource_pool[0], resource_pool[1],
                                      trace_path=sidecars[0] + ".pco-admission.tsv")
        return
    raw = str(os.environ.get("PCC_PY_FRONTEND_JOBS", "") or "").strip().lower()
    budget_raw = str(os.environ.get("PCC_WORKER_TREE_BUDGET_BYTES", "") or "")
    budget = int(budget_raw) if budget_raw.isdigit() else 0
    if raw not in ("auto", "on", "true", "yes") or budget <= 0:
        run_worker_processes(commands[:oversized], 1)
        safe_commands = commands[oversized:]
        if safe_commands:
            if budget > 0:
                safe_jobs = compiled_native_auto_jobs(safe_jobs)
            run_worker_processes(safe_commands, safe_jobs)
        return
    sizes = [os.path.getsize(path) for path in sidecars]
    raw_gc = str(os.environ.get("PCC_GC_BACKEND", "0"))
    gc_backend = 0 if raw_gc == "0" else -1
    floors = [indexed_pco_floor_bytes(size, gc_backend) for size in sizes]
    order = sorted(range(len(commands)), key=lambda index: (-sizes[index], index))
    run_weighted_worker_processes(
        [commands[index] for index in order],
        [floors[index] for index in order],
        min(parallel_cpu_budget(), _MAX_WIDTH),
        compiled_native_worker_budget(budget, _DRIVER_RESERVE),
    )


def _requested_width_and_budget():
    """(width, budget) for the weighted pools, or None without auto width."""
    raw = str(os.environ.get("PCC_PY_FRONTEND_JOBS", "") or "").strip().lower()
    budget_raw = str(os.environ.get("PCC_WORKER_TREE_BUDGET_BYTES", "") or "")
    budget = int(budget_raw) if budget_raw.isdigit() else 0
    if raw not in ("auto", "on", "true", "yes") or budget <= 0:
        return None
    return min(parallel_cpu_budget(), _MAX_WIDTH), budget


def _resource_width_and_budget(fallback_width):
    raw_budget = str(os.environ.get("PCC_WORKER_TREE_BUDGET_BYTES", "") or "")
    budget = int(raw_budget) if raw_budget.isdigit() else 0
    if budget <= 0:
        return None
    raw = str(os.environ.get("PCC_PY_FRONTEND_JOBS", "auto") or "auto").strip().lower()
    if raw in ("auto", "on", "true", "yes"):
        width = parallel_cpu_budget()
    else:
        try:
            width = int(raw)
        except ValueError:
            width = fallback_width
    return max(1, min(width, _MAX_WIDTH)), budget


def _pco_resource_tasks(sidecars, primary_count=0):
    raw_gc = str(os.environ.get("PCC_GC_BACKEND", "0") or "0")
    cost = ([_PCO_BASE, _PCO_PER_SIDECAR_MB] if raw_gc == "0"
            else [_PCO_LEGACY_BASE, _PCO_LEGACY_PER_SIDECAR_MB])
    tasks = []
    for index, path in enumerate(sidecars):
        tasks.append({
            "class": os.path.dirname(path) + "|native-pco|" + raw_gc,
            "inputs": [0], "estimate_bytes": 0,
            "input_path": path, "input_cost": cost, "input_ready": False,
            "report_path": path + ".pco-rss", "restartable": True,
            "depends_on": index if primary_count else -1,
        })
    return tasks



def _budgeted_width_and_budget():
    requested = _requested_width_and_budget()
    if requested is None:
        return None
    # RSS belongs to this lightweight execution driver, never the departed
    # frontend coordinator whose plan we consume.
    width, budget = requested
    return width, compiled_native_worker_budget(budget, _DRIVER_RESERVE)


def run_frontend_commands(commands, manifests, oversized, safe_jobs):
    if len(commands) != len(manifests):
        raise ValueError("frontend command/manifest inventory mismatch")
    if not commands:
        return
    resource_pool = _resource_width_and_budget(safe_jobs)
    if resource_pool is not None:
        tasks = resource_tasks_for_commands(commands)
        if tasks is None:
            raise ValueError("resource admission requires frontend worker manifests")
        run_resource_worker_processes(commands, tasks, resource_pool[0], resource_pool[1],
                                      trace_path=manifests[0] + ".admission.tsv")
        return
    pool = _requested_width_and_budget()
    if pool is None:
        # Explicit worker counts and callers without a resource budget retain
        # the plan's conservative policy. Never infer spare physical memory.
        run_worker_processes(commands[:oversized], 1)
        safe_commands = commands[oversized:]
        if safe_commands:
            budget_raw = str(os.environ.get("PCC_WORKER_TREE_BUDGET_BYTES", "") or "")
            if budget_raw.isdigit() and int(budget_raw) > 0:
                safe_jobs = compiled_native_auto_jobs(safe_jobs)
            run_worker_processes(safe_commands, safe_jobs)
        return
    floors, order = _frontend_floors_and_order(commands, manifests)
    pool = _budgeted_width_and_budget()
    run_weighted_worker_processes(
        [commands[index] for index in order],
        [floors[index] for index in order],
        pool[0],
        pool[1],
    )


def run_frontend_pco_commands(
    frontend_commands, manifests, pco_commands, sidecars, oversized, safe_jobs,
):
    """Each module's PCO job chained after its frontend job, in one pool.

    Frontend jobs keep priority; a module's ready PCO job takes a slot only
    when no remaining frontend job fits, so PCO work fills the frontend ramp
    and tail and the gap before the PCO phase (r8: 0-5 workers for ~20 s)
    instead of displacing frontend jobs.  PCC_FRONTEND_PCO_CHAIN=0, or no
    budgeted auto width, runs the phases one after the other, as before.
    """
    count = len(frontend_commands)
    if len(manifests) != count or len(pco_commands) != count or len(sidecars) != count:
        raise ValueError("frontend/PCO command inventory mismatch")
    if count == 0:
        return
    resource_pool = _resource_width_and_budget(safe_jobs)
    chain = str(os.environ.get("PCC_FRONTEND_PCO_CHAIN", "1") or "1").strip().lower()
    if resource_pool is not None and chain not in ("0", "off", "no", "false"):
        tasks = resource_tasks_for_commands(frontend_commands)
        if tasks is None:
            raise ValueError("resource admission requires frontend worker manifests")
        tasks.extend(_pco_resource_tasks(sidecars, count))
        run_resource_worker_processes(
            list(frontend_commands) + list(pco_commands), tasks,
            resource_pool[0], resource_pool[1], trace_path=manifests[0] + ".chained-admission.tsv",
        )
        return
    pool = _requested_width_and_budget()
    chain = str(os.environ.get("PCC_FRONTEND_PCO_CHAIN", "1") or "1").strip().lower()
    if pool is None or chain in ("0", "off", "no", "false"):
        run_frontend_commands(frontend_commands, manifests, oversized, safe_jobs)
        run_pco_commands(pco_commands, sidecars, oversized, safe_jobs)
        return
    floors, order = _frontend_floors_and_order(frontend_commands, manifests)
    pool = _budgeted_width_and_budget()
    raw_gc = str(os.environ.get("PCC_GC_BACKEND", "0"))
    if raw_gc == "0":
        pco_floor = (_PCO_BASE, _PCO_PER_SIDECAR_MB, _PCO_CAP)
    else:
        pco_floor = (_PCO_LEGACY_BASE, _PCO_LEGACY_PER_SIDECAR_MB, _PCO_CAP)
    run_chained_worker_processes(
        [frontend_commands[index] for index in order],
        [floors[index] for index in order],
        [pco_commands[index] for index in order],
        [sidecars[index] for index in order],
        pco_floor,
        pool[0],
        pool[1],
    )


def _frontend_floors_and_order(commands, manifests):
    if len(commands) != len(manifests):
        raise ValueError("frontend command/manifest inventory mismatch")
    sizes = []
    scores = []
    module_names = []
    exports_paths = []
    for manifest in manifests:
        with open(manifest, "r", encoding="utf-8") as stream:
            lines = stream.read().splitlines()
        if len(lines) < 12 or lines[0] != "pcc.frontends.python.codegen_worker.v4" or lines[-2] != "1":
            raise ValueError("indexed frontend scheduling requires singleton v4 manifests")
        index = int(lines[-1])
        if index < 0:
            raise ValueError("negative frontend module index")
        ast_path = os.path.join(lines[5], "module_" + str(index) + ".json")
        with open(ast_path, "rb") as stream:
            payload = stream.read()
        sizes.append(len(payload))
        # Literal-heavy ASTs can be large but cheap to lower. Call nodes are
        # a better cheap first-work estimate for this independent module queue.
        scores.append(_call_node_score(payload))
        module_names.append(_manifest_module_name(lines, index))
        exports_paths.append(lines[3])
    raw_gc = str(os.environ.get("PCC_GC_BACKEND", "0"))
    gc_backend = 0 if raw_gc == "0" else -1
    closures = None
    if gc_backend == 0 and exports_paths and all(
        path == exports_paths[0] for path in exports_paths
    ) and all(module_names):
        closures = export_closure_bytes(exports_paths[0], module_names)
    if closures is None:
        floors = [indexed_frontend_floor_bytes(size, gc_backend) for size in sizes]
    else:
        floors = [
            indexed_frontend_floor_bytes(sizes[position], gc_backend, closures[position])
            for position in range(len(sizes))
        ]
    order = sorted(
        range(len(commands)),
        key=lambda index: (-scores[index], -floors[index], index),
    )
    return floors, order
