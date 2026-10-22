"""Byte reservations shared by host and native compiler worker schedulers.

A reservation is an estimate, never an RSS limit. The enclosing process-tree
watchdog remains the enforcement owner. Recognized host tasks have empirical
size priors; unsupported execution classes still calibrate exclusively. Completed
tasks supply measured envelopes for later, no-larger inputs in the same class. Estimates may rise while a worker is alive, but never
fall until that process and its descendants have retired.
"""

import os
import time


TREE_STATE_ENV = "PCC_WORKER_TREE_STATE_PATH"
TREE_RECOVERY_ENV = "PCC_WORKER_TREE_RECOVERY"
# v1 timestamps use the same-machine CPython monotonic clock (Python >=3.13):
# Darwin mach_absolute_time / CLOCK_UPTIME_RAW, Linux CLOCK_MONOTONIC, and
# Windows QueryPerformanceCounter. The native platform clock must share both
# its epoch and suspend behavior. Wall time and process-relative clocks are
# not compatible; not_before is in this same domain. State is run-local.
TREE_STATE_SCHEMA = "pcc.worker-tree-rss.v1"
RESOURCE_REPORT_ENV = "PCC_WORKER_RESOURCE_REPORT"
RESOURCE_TOKEN_ENV = "PCC_WORKER_RESOURCE_TOKEN"
RESOURCE_REPORT_SCHEMA = "pcc.worker-resource.v2"
RSS_HEADROOM_BYTES = 134217728
STATE_MAX_AGE_SECONDS = 2.0


class WorkerMemoryError(ValueError):
    pass


def is_indexed_backend_prior_model(model) -> bool:
    """Backend priors sized from a sealed PIDX handoff (v1 bytes, v2 shape)."""
    return model == "host-indexed-backend-v1" or model == "host-indexed-backend-v2"


def peak_reservation(peak_bytes: int) -> int:
    if peak_bytes <= 0:
        raise WorkerMemoryError("worker peak RSS is unavailable")
    # The margin is the existing measured-worker calibration contract.
    return (int(peak_bytes) * 5 + 3) // 4 + RSS_HEADROOM_BYTES


def input_envelope_covers(observed_inputs, pending_inputs) -> bool:
    if len(observed_inputs) != len(pending_inputs):
        return False
    for before, after in zip(observed_inputs, pending_inputs):
        if before < after or after < 0:
            return False
    return True



def task_startup_prior_bytes(task) -> int:
    """Return a soft host forecast, never an explicit or measured lower bound.

    The coefficients use completed cc5 samples; 137 completions are held out.
    They are not upper bounds: both runs censor their largest unfinished work.
    Export bytes were constant within each sampled reader, so their multiplier
    is a provisional scaling assumption. pcc's own native workers share these
    models: in the compiled prior-gate graph their padded peaks reached 0.70
    (export), 0.60 (summary) and 1.08 (codegen) of the forecast, an excess the
    completed-sample scale below absorbs.
    Live growth, the tree watchdog and bounded exclusive retries remain owners
    of memory safety. Completed modeled samples can only raise this forecast's
    scale; unsupported workers retain the legacy absolute-envelope maximum.
    """
    model = str(task.get("startup_prior_model", "") or "")
    if not model:
        return 0
    mib = 1024 * 1024
    if is_indexed_backend_prior_model(model):
        if not task.get("input_ready", False):
            return 0
        packed_bytes = task.get("prior_input_bytes", 0)
        if type(packed_bytes) is not int or packed_bytes <= 0:
            raise WorkerMemoryError("indexed worker prior needs verified packed bytes")
        if model == "host-indexed-backend-v1":
            return peak_reservation(64 * mib + 7 * packed_bytes)
        # v2 reads the sealed kernel shape: [header bytes, functions,
        # globals] then (total, largest-function) entry counts per 8-byte
        # arena. Emission state is per function, so the largest function
        # drives the peak: the module holding pcc's 2,000-line call
        # dispatcher outgrew the v1 forecast's margin by up to 1.47x, and the
        # completed-sample scale then raised every other backend by that
        # factor. Of 5,052 macOS CI backend completions (48 runs), 28
        # exceeded v1; none exceed v2, whose worst padded peak is 1.27x.
        shape = task["inputs"]
        if (len(shape) < 5 or len(shape) % 2 == 0
                or any(type(value) is not int or value < 0 for value in shape)):
            raise WorkerMemoryError("indexed worker prior needs the sealed kernel shape")
        return peak_reservation(64 * mib + 40 * sum(shape[3::2]) + 48 * sum(shape[4::2]))
    inputs = task["inputs"]
    if len(inputs) != 6 or any(type(value) is not int or value < 0 for value in inputs):
        raise WorkerMemoryError("host worker prior needs nonnegative source/AST/export bytes")
    source_bytes, ast_bytes, export_bytes = inputs[0], inputs[2], inputs[4]
    if model == "host-export-v1":
        working_bytes = 160 * mib + 160 * source_bytes
    elif model == "host-summary-v1":
        working_bytes = 192 * mib + 5 * (ast_bytes + export_bytes)
    elif model == "host-codegen-v1":
        if inputs[5] > 1:
            # Bounded host batches reserve the SUM of singleton forecasts.
            # Keep every member's fixed/export cost and margin; do not assume
            # that shared imports or sequential cleanup reduce peak demand.
            # 224 * 5 is divisible by 4, so the sum is exact for integer bytes.
            return (inputs[5] * peak_reservation(128 * mib + 43 * export_bytes)
                    + 280 * (source_bytes + ast_bytes))
        working_bytes = 128 * mib + 224 * (source_bytes + ast_bytes) + 43 * export_bytes
    elif model == "host-indexed-frontend-v1":
        working_bytes = 128 * mib + 80 * (source_bytes + ast_bytes) + 11 * export_bytes
    else:
        raise WorkerMemoryError("unknown worker startup prior model: " + model)
    return peak_reservation(working_bytes)


def completed_task_observation(task, peak):
    """Bind a verified completion to its original model, never a live sample."""
    sample = (task["class"], list(task["inputs"]), peak)
    model = str(task.get("startup_prior_model", "") or "")
    if not model:
        return sample
    prior = task_startup_prior_bytes(task)
    if prior <= 0:
        raise WorkerMemoryError("completed modeled worker has no startup prior")
    return sample + ((model, prior),)


def estimated_task_bytes(task, observations) -> int:
    """Return a soft estimate or zero for an uncalibrated task.

    Samples are local to this invocation and execution class. They cannot be
    reused across machines, compilers, collectors, options, or export readers.
    Modeled samples raise the entire size predictor by the greatest measured
    padded-peak/original-prior ratio. A large task's absolute peak is not copied
    to every smaller task. This remains extrapolation, not an RSS upper bound.
    Legacy three-field samples retain their old covering-envelope maximum.
    """
    prior = task_startup_prior_bytes(task)
    model = str(task.get("startup_prior_model", "") or "")
    estimate = max(0, int(task["estimate_bytes"]), prior)
    incomplete_peak = int(task.get("incomplete_peak_bytes", 0))
    if incomplete_peak > 0:
        # A cancelled attempt establishes only a lower bound. It must never
        # become a completed-task sample that authorizes concurrent retries.
        estimate = max(estimate, peak_reservation(incomplete_peak))
    for sample in observations:
        if len(sample) not in (3, 4):
            raise WorkerMemoryError("worker observation shape differs")
        key, inputs, peak = sample[:3]
        if key != task["class"]:
            continue
        if model and len(sample) == 4:
            fitted = sample[3]
            if (type(fitted) is not tuple or len(fitted) != 2
                    or fitted[0] != model or type(fitted[1]) is not int
                    or fitted[1] <= 0):
                raise WorkerMemoryError("worker observation prior binding differs")
            # Arbitrary-precision integer ceil avoids float precision loss,
            # division by zero, and fixed-width intermediate overflow.
            scaled = (prior * peak_reservation(peak) + fitted[1] - 1) // fitted[1]
            estimate = max(estimate, scaled)
        elif input_envelope_covers(inputs, task["inputs"]):
            estimate = max(estimate, peak_reservation(peak))
    return estimate


def minimum_task_bytes(task) -> int:
    """Retain explicit reservations and this task's observed lower bound.

    Completed peaks borrowed from other inputs and their safety margins are
    forecasts. They cannot prove that this unmeasured task exceeds the budget.
    An incomplete same-task peak is actual evidence and remains a floor.
    """
    return max(0, int(task["estimate_bytes"]),
               int(task.get("incomplete_peak_bytes", 0)))


def available_worker_bytes(tree_budget: int, owner_rss: int,
                           outside_owner_rss: int = 0) -> int:
    if tree_budget <= 0 or owner_rss <= 0 or outside_owner_rss < 0:
        raise WorkerMemoryError("positive tree budget and measured owner RSS are required")
    reserve = int(owner_rss) + int(outside_owner_rss) + RSS_HEADROOM_BYTES
    available = int(tree_budget) - reserve
    if available <= 0:
        raise WorkerMemoryError(
            "worker memory budget cannot admit one task: budget=" + str(tree_budget)
            + " owner=" + str(owner_rss) + " other_processes=" + str(outside_owner_rss)
            + " headroom=" + str(RSS_HEADROOM_BYTES)
            + " owner_reservation_bytes=" + str(reserve)
            + "; task demand is not yet known"
        )
    return available


def require_task_fits(task_index: int, demand: int, available: int,
                      tree_budget: int) -> None:
    if demand <= 0:
        raise WorkerMemoryError("worker byte reservation must be positive")
    if demand > available:
        raise WorkerMemoryError(
            "worker memory budget cannot admit task=" + str(task_index)
            + " worker_reservation_bytes=" + str(demand)
            + " available_worker_bytes=" + str(available)
            + " estimated_minimum_budget_bytes=" + str(tree_budget - available + demand)
            + " budget=" + str(tree_budget)
            + "; this reservation is not a guaranteed full-task peak"
        )


def _sparse_native_cover_order(tasks, pending, bands) -> bool:
    """Recognize cold cohorts with no measured-peer parallelism to preserve.

    This only selects an order. Full completed-sample coverage, byte admission,
    growth handling and exclusive retries remain the scheduler's responsibility.
    A large completed peak can still overcharge small inputs; this is not a
    bound on memory or an elapsed-time guarantee.
    """
    frontier = []
    cohorts = {}
    for index in pending:
        task = tasks[index]
        if (task["estimate_bytes"] != 0
                or task.get("incomplete_peak_bytes", 0) != 0
                or task.get("retry_calibration", False)
                or task.get("calibrate_before_peers", False)
                or any(type(value) is not int for value in task["inputs"])):
            return False
        # Descending total input size means a later input cannot strictly
        # cover an earlier one. Scan the whole inventory, retaining at most
        # two incomparable witnesses, with exact six-component coverage.
        if not any(input_envelope_covers(tasks[peer]["inputs"], task["inputs"])
                   for peer in frontier):
            frontier.append(index)
            if len(frontier) > 2:
                return False
        cohorts.setdefault(bands[index], []).append(index)
    if len(frontier) >= len(tasks):
        return False
    for cohort in cohorts.values():
        # Source and AST are the only variable components in this already
        # validated singleton class. Sorting source first lets the largest
        # preceding AST witness any earlier cover in O(n log n), not O(n^2).
        cohort.sort(key=lambda index: (
            -tasks[index]["inputs"][0], -tasks[index]["inputs"][2], index,
        ))
        best = -1
        covered = 0
        for position, index in enumerate(cohort):
            inputs = tasks[index]["inputs"]
            earlier = best >= 0 and input_envelope_covers(tasks[best]["inputs"], inputs)
            # Equal vectors cover each other: both tasks count, not just the
            # later tie. A later unequal vector cannot cover this input.
            later = position + 1 < len(cohort) and input_envelope_covers(
                tasks[cohort[position + 1]]["inputs"], inputs,
            )
            if earlier or later:
                covered += 1
                if covered > 1:
                    return False
            if best < 0 or inputs[2] > tasks[best]["inputs"][2]:
                best = index
    return True


def resource_task_order(tasks, guarded_calibration: bool = False,
                        native_cold_cover: bool = False):
    """Schedule modeled host work largest-first; retain legacy native cohorts.

    Source/AST bytes vary per singleton; full-export bytes and module count
    do not. Split each power-of-two size band at its arithmetic midpoint,
    limiting a cohort to a size ratio below 1.5 instead of 2. The largest
    input in each half-band calibrates first, with the existing componentwise
    coverage and maximum-peak estimator unchanged. Task indices never move.
    Other phases, mixed classes and dependency-bearing tasks keep their old
    priority and readiness rules. native_cold_cover is set only by a native
    caller with no completed observation in this execution class.
    """
    pending = sorted(range(len(tasks)), key=lambda index: (
        -sum(tasks[index]["inputs"]), index,
    ))
    if not tasks:
        return pending, []
    if any(task.get("startup_prior_model", "") for task in tasks):
        # Host priors replace the small-cohort drain. Indices and dependency
        # edges stay intact; lazy backend inputs are re-ranked when ready.
        return sorted(pending, key=lambda index: (
            -task_startup_prior_bytes(tasks[index]),
            -sum(tasks[index]["inputs"]), index,
        )), []
    execution_class = tasks[0]["class"]
    bands = []
    for task in tasks:
        inputs = task["inputs"]
        if (task.get("diagnostic_phase", "") != "codegen"
                or task["class"] != execution_class
                or len(task.get("diagnostic_indices", [])) != 1
                or len(inputs) != 6 or inputs[5] != 1
                or inputs[0] != inputs[1] or inputs[2] != inputs[3]
                or inputs[4] != tasks[0]["inputs"][4]
                or any(value < 0 for value in inputs)
                or task.get("depends_on", -1) >= 0
                or task.get("input_path", "")):
            return pending, []
        size = inputs[0] + inputs[2]
        size_total = size
        band = 0
        upper = 1
        while size > 0:
            size //= 2
            upper *= 2
            band += 1
        if size == 0 and size_total > 0:
            # [2**k, 2**(k+1)) splits at 3*2**(k-1). Keep the
            # original quotient loop and skip this split if it ends in NaN.
            band = 2 * band + (1 if 4 * size_total >= 3 * upper else 0)
        bands.append(band)
    # A singleton-only inventory has no within-band peers to amortize its
    # calibration: smallest-first would calibrate every input exclusively.
    # If one input covers every other input under the existing estimator,
    # calibrate it first and let that same-run completed observation admit
    # peers. No live/incomplete sample or foreign invocation is reused.
    # Only the guarded path permits oversized borrowed forecasts to calibrate
    # exclusively against current RSS instead of rejecting an otherwise-fit
    # task. Unguarded inventories retain their original ordering.
    # Repeated bands keep the small-first policy that avoids charging a large
    # module's peak to the many small modules of a bootstrap build.
    if guarded_calibration and len(tasks) > 1 and len(set(bands)) == len(tasks):
        largest = pending[0]
        if all(input_envelope_covers(tasks[largest]["inputs"], task["inputs"])
               for task in tasks):
            return pending, []
    # Sparse repeated cohorts can otherwise require almost every input to
    # calibrate alone. Do not disturb dense cohorts with two known peers,
    # warm invocations, explicit reservations, or the established host path.
    if (guarded_calibration and native_cold_cover
            and len(set(bands)) < len(tasks)
            and _sparse_native_cover_order(tasks, pending, bands)):
        return pending, []
    pending = sorted(pending, key=lambda index: (
        bands[index], -sum(tasks[index]["inputs"]), index,
    ))
    return pending, bands


def ready_resource_cohort(ready, pending, active_indices, bands):
    """Drain the smallest unfinished cohort, including its live workers."""
    if not bands:
        return ready
    current = min(bands[index] for index in pending + active_indices)
    return [index for index in ready if bands[index] == current]


def validate_handoff_slots(tasks):
    """Opt-in slots pair one unchained FE with its own sealed-handoff BE.

    Legacy tasks without slot metadata keep their dependency semantics.
    Reject malformed pairs before starting any child, rather than leaking
    capacity or letting an unrelated completion release another FE's slot.
    """
    members = {}
    for index, task in enumerate(tasks):
        if "handoff_slot" not in task:
            continue
        slot = task["handoff_slot"]
        if type(slot) is not int or slot < 0 or slot >= len(tasks):
            raise WorkerMemoryError("invalid resource handoff slot owner")
        members.setdefault(slot, []).append(index)
    for slot, pair in members.items():
        if (len(pair) != 2 or slot not in pair
                or tasks[slot].get("depends_on", -1) != -1
                or tasks[slot].get("handoff_request", "")):
            raise WorkerMemoryError("invalid resource handoff slot pair")
        backend = pair[1] if pair[0] == slot else pair[0]
        dependency = tasks[backend].get("depends_on", -1)
        if (type(dependency) is not int or dependency != slot
                or not tasks[backend].get("handoff_request", "")):
            raise WorkerMemoryError("invalid resource handoff slot release")


def choose_task(pending, tasks, observations, active_reservations,
                width: int, available: int, guarded_calibration: bool = False):
    """Pick a fitting task or reserve idle capacity for a calibration.

    Modeled host tasks use empirical byte priors, refreshed for each ready
    input. Unsupported shapes retain exclusive calibration; its measured peak
    can admit later workers whose full input envelope it covers.
    A cancelled attempt needs the same exclusive space for its bounded retry;
    stop admitting peers until the active workers have drained. Preserve any
    known minimum above that space so the caller refuses before launching.
    """
    if len(active_reservations) >= width:
        return -1, 0, False
    for index in pending:
        if tasks[index].get("retry_calibration", False):
            if active_reservations:
                return -1, 0, False
            return index, max(available, estimated_task_bytes(tasks[index], observations)), True
    if any(tasks[index].get("startup_prior_model", "") for index in pending):
        # Re-evaluate after dependency/input preparation and after every live
        # observation. Select the largest fitting ready forecast, not a stale
        # initial ordering with zero-sized backend placeholders.
        pending = sorted(pending, key=lambda index: (
            -estimated_task_bytes(tasks[index], observations), index,
        ))
        if (pending and guarded_calibration and available > 0
                and tasks[pending[0]].get("startup_prior_model", "")
                and estimated_task_bytes(tasks[pending[0]], observations) > available):
            if active_reservations:
                return -1, 0, False
            index = pending[0]
            return index, estimated_task_bytes(tasks[index], observations), True
    # Split dependency heads must not wait behind an indefinitely refilled
    # known-work lane. Only ready, explicitly marked, genuinely unknown tasks
    # participate; retry calibration above keeps its original priority.
    calibration = -1
    for index in pending:
        if (tasks[index].get("calibrate_before_peers", False)
                and estimated_task_bytes(tasks[index], observations) == 0
                and (calibration < 0 or index < calibration)):
            calibration = index
    if calibration >= 0:
        if active_reservations or available <= 0:
            return -1, 0, False
        return calibration, available, True
    remaining = available - sum(active_reservations)
    unknown = -1
    forecast_only = -1
    for index in pending:
        demand = estimated_task_bytes(tasks[index], observations)
        if demand == 0:
            if unknown < 0:
                unknown = index
        elif demand <= remaining:
            return index, demand, False
        elif (guarded_calibration and demand > available and forecast_only < 0
              and minimum_task_bytes(tasks[index]) <= available):
            forecast_only = index
    if not active_reservations and unknown >= 0 and available > 0:
        return unknown, available, True
    if not active_reservations and forecast_only >= 0 and available > 0:
        # Keep the complete forecast visible while reserving the entire idle
        # lane. Fresh guard accounting must supervise this calibration.
        return forecast_only, estimated_task_bytes(tasks[forecast_only], observations), True
    return -1, 0, False


def _open_worker_resource(path: str):
    # The adjacent local binding and simple implementation guard are also the
    # native dependency scanner's boundary. Keep the Win32 adapter host-only.
    import sys as execution_runtime
    if execution_runtime.implementation.name == "cpython":
        if execution_runtime.platform == "win32":
            from pcc.frontends.python.worker_resource_host_windows import (
                open_worker_resource,
            )
            return open_worker_resource(path)
    return open(path, "r", encoding="utf-8")


def read_worker_resource(path: str, expected_pid: int, expected_token: str = ""):
    if not path:
        return None
    try:
        with _open_worker_resource(path) as stream:
            rows = stream.read().splitlines()
        if len(rows) != 6 or rows[0] != RESOURCE_REPORT_SCHEMA or rows[5] != expected_token:
            return None
        pid, current, peak = int(rows[1]), int(rows[3]), int(rows[4])
        if pid != expected_pid or current <= 0 or peak < current:
            return None
        return rows[2], current, peak
    except (OSError, ValueError):
        return None


def _set_tree_state_diagnostic(
    diagnostic, reason, sampled_at, checked_at, age, not_before, tree_budget,
    observed_budget, owner_pid, owner_parent, owner_rss, active_pid,
    active_parent, active_rss, line_count, row_number,
):
    # Keep this publication body out of the reader's finally AST: every early
    # return otherwise regenerates its dict construction and method calls.
    # The caller retains the original optional-evidence exception boundary.
    diagnostic.clear()
    diagnostic.update({
        "reason": reason, "sampled_at": sampled_at,
        "checked_at": checked_at, "age": age, "not_before": not_before,
        "expected_budget": tree_budget, "observed_budget": observed_budget,
        "owner_pid": owner_pid, "owner_parent": owner_parent, "owner_rss": owner_rss,
        "active_pid": active_pid, "active_parent": active_parent, "active_rss": active_rss,
        "line_count": line_count, "row_number": row_number,
    })


def read_tree_state(path: str, tree_budget: int, owner_pid: int, active_pids,
                    not_before: float = 0.0, include_owner: bool = False,
                    diagnostic=None):
    """Read the guard's synchronized accounting without shelling out.

    Return (outside-owner-and-worker bytes, worker-subtree bytes). Missing,
    stale, malformed and mismatched state is unavailable, never zero RSS.
    This optional observation transport does not provide the RSS guard.
    With include_owner, the RSS map also contains the owner's current RSS;
    all active roots must be positively measured direct children. Every value
    comes from this one atomically published process-table sample, not from
    independently timed high-water marks or a second file read. Optional
    diagnostic output describes this read; -1 marks fields not yet parsed.
    """
    reason = "empty_path"
    sampled_at = checked_at = age = -1.0
    observed_budget = owner_parent = owner_rss = -1
    active_pid = active_parent = active_rss = -1
    row_number = line_count = 0
    try:
        if not path:
            return None
        reason = "read"
        with open(path, "r", encoding="utf-8") as stream:
            lines = stream.read().splitlines()
        line_count = len(lines)
        reason = "header"
        if len(lines) < 4 or lines[0] != TREE_STATE_SCHEMA:
            return None
        reason = "timestamp"
        sampled_at = float(lines[1])
        reason = "clock"
        checked_at = time.monotonic()
        age = checked_at - sampled_at
        reason = "age"
        if not (0 <= age <= STATE_MAX_AGE_SECONDS):
            return None
        reason = "before_barrier"
        if sampled_at < not_before:
            return None
        reason = "budget"
        observed_budget = int(lines[2])
        if observed_budget != tree_budget:
            return None
        rows = {}
        for line in lines[3:]:
            row_number += 1
            values = line.split("\t")
            reason = "row_fields"
            if len(values) != 3:
                return None
            reason = "row_integer"
            pid, parent, rss = int(values[0]), int(values[1]), int(values[2])
            reason = "row_identity"
            if pid <= 0 or parent < 0 or rss < 0 or pid in rows:
                return None
            rows[pid] = (parent, rss)
        reason = "owner_missing"
        if owner_pid not in rows:
            return None
        owner_parent, owner_rss = rows[owner_pid]
        reason = "owner_rss"
        if owner_rss <= 0:
            return None
        children = {}
        for pid in active_pids:
            if include_owner:
                active_pid = pid
                active_parent = active_rss = -1
                reason = "active_owner"
                if pid == owner_pid:
                    return None
                reason = "active_missing"
                if pid not in rows:
                    return None
                active_parent, active_rss = rows[pid]
                reason = "active_parent"
                if active_parent != owner_pid:
                    return None
                reason = "active_rss"
                if active_rss <= 0:
                    return None
            children[pid] = 0
        reason = "accounting"
        outside = 0
        for pid, row in rows.items():
            if pid == owner_pid:
                continue
            cursor = pid
            seen = set()
            worker = 0
            while cursor in rows and cursor not in seen:
                if cursor in children:
                    worker = cursor
                    break
                seen.add(cursor)
                cursor = rows[cursor][0]
            if worker:
                children[worker] += row[1]
            else:
                outside += row[1]
        if include_owner:
            children[owner_pid] = rows[owner_pid][1]
        reason = "accepted"
        return outside, children
    except (OSError, ValueError):
        return None
    finally:
        if diagnostic is not None:
            try:
                _set_tree_state_diagnostic(
                    diagnostic, reason, sampled_at, checked_at, age, not_before,
                    tree_budget, observed_budget, owner_pid, owner_parent,
                    owner_rss, active_pid, active_parent, active_rss,
                    line_count, row_number,
                )
            except Exception:
                pass  # Optional evidence must never change the reader outcome.


def _replace_worker_resource(temporary: str, path: str) -> None:
    # The host adapter needs extended Windows replacement semantics. Preserve
    # the adjacent local binding/simple guard for native dependency discovery.
    import sys as execution_runtime
    if execution_runtime.implementation.name == "cpython":
        if execution_runtime.platform == "win32":
            from pcc.frontends.python.worker_resource_host_windows import (
                replace_worker_resource,
            )
            replace_worker_resource(temporary, path)
            return
    if execution_runtime.implementation.name != "pcc":
        os.replace(temporary, path)
        return
    # This internal native operation deliberately selects the owned primitive,
    # not a mutable os-provider attribute. Its inputs are report path strings.
    if not isinstance(temporary, str) or not isinstance(path, str):
        raise TypeError("worker RSS publication paths must be strings")
    if "\0" in temporary or "\0" in path:
        raise ValueError("worker RSS publication path contains a null character")
    from pcc.extern import c_int64, c_obj, c_str, extern

    if execution_runtime.platform == "win32":
        # c_str leases both owners before exposing either UTF-8 view. Ordinary
        # MoveFileEx does not request extended replace-open-destination
        # semantics, even when the reader grants all three sharing modes.
        from pcc.unsafe import load_i64, stack_alloc

        # This record belongs to this call, even if string-lease cleanup reenters.
        # Runtime layout: failure stage@0, primary DWORD@8, close DWORD@16.
        # Application stack_alloc yields an integer address; c_int64 unboxes
        # that address. c_ptr would instead pass the managed integer object.
        diagnostic = stack_alloc(24)
        native_publish = extern("pcc_worker_resource_replace_detailed", (c_str, c_str, c_int64), c_int64)
        status = native_publish(temporary, path, diagnostic)
        detail = ""
        if status != 0:
            stage_code = load_i64(diagnostic, 0)
            stage = "unknown"
            if stage_code == 1:
                stage = "destination_full_path"
            elif stage_code == 2:
                stage = "source_full_path"
            elif stage_code == 3:
                stage = "source_validation"
            elif stage_code == 4:
                stage = "rename_buffer_allocation"
            elif stage_code == 5:
                stage = "open_source"
            elif stage_code == 6:
                stage = "rename"
            elif stage_code == 7:
                stage = "close"
            elif stage_code == 8:
                stage = "source_information"
            detail = (" stage=" + stage
                      + " winerror=" + str(load_i64(diagnostic, 8))
                      + " handle_cleanup_winerror=" + str(load_i64(diagnostic, 16)))
        if status < 0:
            # Keep the errno/type and absent winerror attribute used by the
            # existing native retry policy; raw DWORDs are diagnostic text only.
            raise OSError(-status, "worker RSS atomic replacement failed:" + detail,
                          temporary, None, path)
        if status > 0:
            primary_errno = status // 65536
            cleanup_errno = status % 65536
            committed = "True" if primary_errno == 0 else "False"
            raise RuntimeError(
                "worker RSS handle cleanup failed: publication_committed=" + committed
                + " primary_errno=" + str(primary_errno)
                + " handle_cleanup_errno=" + str(cleanup_errno) + detail)
        return

    native_replace = extern("py_os_replace", (c_obj, c_obj), c_obj)
    native_error_pending = extern("py_err_occurred", (), c_int64)
    result = native_replace(temporary, path)
    if native_error_pending():
        raise
    # c_obj owns a managed result (the immortal None on success). Keep it
    # rooted until the pending-error check; never use a raw address lane.
    del result


def publish_worker_resource(phase: str) -> None:
    """Publish self high-water RSS at existing worker phase boundaries."""
    path = str(os.environ.get(RESOURCE_REPORT_ENV, "") or "")
    if not path:
        return
    from pcc.frontends.python.pipeline_frontend_workers import (
        _coordinator_rss_bytes, _worker_peak_rss_bytes,
    )

    current = _coordinator_rss_bytes()
    peak = max(current, _worker_peak_rss_bytes())
    if current <= 0 or peak <= 0:
        raise WorkerMemoryError("worker RSS observation is unavailable")
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        stream.write(
            RESOURCE_REPORT_SCHEMA + "\n" + str(os.getpid()) + "\n" + phase
            + "\n" + str(current) + "\n" + str(peak)
            + "\n" + str(os.environ.get(RESOURCE_TOKEN_ENV, "") or "") + "\n"
        )
    # CPython's Windows reader can deny delete sharing while reading the old
    # snapshot. Keep that valid snapshot until atomic replacement succeeds;
    # never truncate it or treat a failed final publication as completion.
    # Bound contention below STATE_MAX_AGE_SECONDS. The independent tree RSS
    # guard and the reader's PID/token/completion checks remain authoritative.
    deadline = time.monotonic() + 1.0
    try:
        while True:
            try:
                _replace_worker_resource(temporary, path)
                break
            except PermissionError as error:
                remaining = deadline - time.monotonic()
                # CPython reports the sharing violation as winerror 5 or 32;
                # any other PermissionError is unrelated. The pcc runtime has
                # no winerror and raises the errno its Windows layer maps those
                # codes to (EACCES), so only there does errno decide.
                import sys as execution_runtime
                winerror = getattr(error, "winerror", None)
                if winerror is None and execution_runtime.implementation.name == "pcc":
                    retryable = execution_runtime.platform == "win32" and error.errno == 13
                else:
                    retryable = winerror in (5, 32)
                if not retryable or remaining <= 0:
                    raise
                time.sleep(min(0.01, remaining))
    except BaseException:
        # Do not leave a failed attempt's unpublished snapshot for a retry.
        # Preserve the publication error if cleanup itself is unavailable.
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
