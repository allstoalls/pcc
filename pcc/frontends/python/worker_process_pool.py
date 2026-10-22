"""Compiler workers share an argv/environment protocol across execution owners."""

import os
import shlex
import subprocess
import sys
import time

from pcc.extern import extern, c_int64, c_ptr


_native_pool = extern("pcc_worker_process_pool", (c_ptr, c_int64), c_int64)
_native_weighted_pool = extern(
    "pcc_weighted_worker_process_pool",
    (c_ptr, c_ptr, c_int64, c_int64), c_int64,
)
_native_chained_pool = extern(
    "pcc_chained_worker_process_pool",
    (c_ptr, c_ptr, c_ptr, c_ptr, c_ptr, c_int64, c_int64), c_int64,
)
_native_worker_start = extern("pcc_worker_process_start", (c_ptr, c_int64), c_int64)
_native_worker_poll = extern("pcc_worker_process_poll", (c_int64,), c_int64)
_native_worker_stop = extern("pcc_worker_process_stop", (c_int64,), c_int64)
_WORKER_RUNNING = 2147483647
_HOST_WORKERS = {}
_NATIVE_LIVE_WORKERS = set()
_NATIVE_COMPLETED_WORKERS = {}
_RESOURCE_OBSERVATIONS = []


def _command_spec(command):
    argv = shlex.split(command)
    env = dict(os.environ)
    while argv:
        key, separator, value = argv[0].partition("=")
        if not separator or not key or not (key[0].isalpha() or key[0] == "_"):
            break
        if not all(ch.isalnum() or ch == "_" for ch in key):
            break
        env[key] = value
        argv.pop(0)
    if not argv:
        raise ValueError("worker command has no executable")
    return argv, [key + "=" + value for key, value in env.items()]


def _start_resource_worker(specs, index):
    if sys.implementation.name == "pcc":
        pid = _native_worker_start(specs, index)
        if pid > 0:
            _NATIVE_LIVE_WORKERS.add(pid)
        return pid
    argv, vector = specs[index]
    env = dict(item.split("=", 1) for item in vector)
    if sys.platform == "win32" and sys.implementation.name == "cpython":
        base = str(getattr(sys, "_base_executable", "") or "")
        executable = sys.executable
        if (base and os.path.normcase(base) != os.path.normcase(executable)
                and os.path.normcase(argv[0]) == os.path.normcase(executable)):
            # Windows venv python.exe is a redirector with a different PID
            # from the interpreter publishing RSS. Use CPython's own spawn
            # protocol to retain the venv prefix, imports and sys.executable
            # while owning the actual interpreter PID, including for retries.
            argv = [base, *argv[1:]]
            env["__PYVENV_LAUNCHER__"] = executable
    process = (subprocess.Popen(argv, env=env, creationflags=512)
               if sys.platform == "win32"
               else subprocess.Popen(argv, env=env, process_group=0))
    _HOST_WORKERS[process.pid] = process
    return process.pid


def _poll_resource_worker(pid):
    if sys.implementation.name == "pcc":
        if pid in _NATIVE_COMPLETED_WORKERS:
            return _NATIVE_COMPLETED_WORKERS[pid]
        if pid not in _NATIVE_LIVE_WORKERS:
            raise ValueError("cannot poll an unowned worker")
        result = _native_worker_poll(pid)
        if result != _WORKER_RUNNING:
            _NATIVE_LIVE_WORKERS.remove(pid)
            _NATIVE_COMPLETED_WORKERS[pid] = result
        return result
    result = _HOST_WORKERS[pid].poll()
    return _WORKER_RUNNING if result is None else result


def _retire_resource_worker(pid):
    if sys.implementation.name == "pcc":
        if pid not in _NATIVE_COMPLETED_WORKERS:
            raise ValueError("cannot retire a live or unowned worker")
        del _NATIVE_COMPLETED_WORKERS[pid]
        return
    process = _HOST_WORKERS[pid]
    if process.returncode is None:
        raise ValueError("cannot retire a live worker")
    del _HOST_WORKERS[pid]
    # poll already reaped the child. Never signal this PID/group now: it can
    # have been reused. Detached descendants remain charged by the tree guard.
    process.wait(timeout=2)


def _stop_resource_worker(pid):
    if sys.implementation.name == "pcc":
        if pid in _NATIVE_COMPLETED_WORKERS:
            del _NATIVE_COMPLETED_WORKERS[pid]
        elif pid in _NATIVE_LIVE_WORKERS:
            _native_worker_stop(pid)
            _NATIVE_LIVE_WORKERS.remove(pid)
        return
    process = _HOST_WORKERS.get(pid)
    if process is None:
        return
    if process.returncode is not None:
        _retire_resource_worker(pid)
        return
    try:
        if sys.platform == "win32":
            process.kill()
        else:
            os.killpg(pid, 9)
    except (ProcessLookupError, PermissionError):
        pass
    process.wait(timeout=2)
    _retire_resource_worker(pid)


def _resource_event(path, event, index, pid, reservation, available, peak):
    if path:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(
                event + "\t" + str(index) + "\t" + str(pid) + "\t"
                + str(reservation) + "\t" + str(available) + "\t" + str(peak)
                + "\t" + str(time.monotonic()) + "\n"
            )


def _resource_diagnostic(task, event, index, pid, reservation, available, peak, cohort=-1):
    """Keep safe task identity and admission evidence after temp files unwind."""
    if str(os.environ.get("PCC_PY_FRONTEND_WORKER_TIMING", "") or "").strip().lower() not in (
        "1", "true", "yes", "on",
    ):
        return
    # Never log the command, environment, source, AST, report path or token.
    # repr keeps unusual module names from forging extra diagnostic lines.
    # Bound each batch line while preserving every assigned module/index.
    import hashlib

    modules = task.get("diagnostic_modules", [])
    indices = task.get("diagnostic_indices", [])
    inputs = task.get("inputs", [])
    class_digest = hashlib.sha256(str(task.get("class", "")).encode("utf-8")).hexdigest()
    count = max(1, len(modules), len(indices))
    offset = 0
    while offset < count:
        sys.stderr.write(
            "pcc frontend admission event=" + event
            + " task=" + str(index) + " pid=" + str(pid)
            + " phase=" + repr(task.get("diagnostic_phase", "unknown"))
            + " indices=" + repr(indices[offset:offset + 16])
            + " modules=" + repr(modules[offset:offset + 16])
            + " module_count=" + str(len(modules))
            + " mapping_offset=" + str(offset)
            + " reservation_bytes=" + str(reservation)
            + " available_bytes=" + str(available)
            + " peak_bytes=" + str(peak)
            + " monotonic_s=" + str(time.monotonic())
            + " input_count=" + str(len(inputs))
            + " inputs=" + repr(inputs[:47] if task.get("handoff_request", "") else inputs[:8])
            + " class_sha256=" + class_digest
            + " cohort=" + str(cohort) + "\n"
        )
        offset += 16
    sys.stderr.flush()


def run_resource_worker_processes(commands, tasks, width, tree_budget,
                                 observations=None, trace_path=""):
    """Use one byte-admission loop on CPython and owned native process APIs.

    Fresh-process task reports carry measured high-water RSS. The optional
    guard snapshot adds wrappers, ancestors, and worker descendants to the
    accounting. Without that transport the stated budget has local process
    scope (this driver and its children); no ancestor RSS is invented.
    """
    from pcc.frontends.python.pipeline_frontend_workers import _coordinator_rss_bytes
    from pcc.frontends.python.worker_resource_plan import (
        RESOURCE_REPORT_ENV, RESOURCE_TOKEN_ENV, STATE_MAX_AGE_SECONDS, TREE_STATE_ENV,
        WorkerMemoryError, available_worker_bytes, choose_task, completed_task_observation,
        estimated_task_bytes, minimum_task_bytes, peak_reservation, read_tree_state,
        read_worker_resource, ready_resource_cohort, require_task_fits,
        resource_task_order, validate_handoff_slots,
    )

    if len(commands) != len(tasks) or width <= 0 or tree_budget <= 0:
        raise WorkerMemoryError("invalid resource worker inventory or budget")
    if not commands:
        return
    validate_handoff_slots(tasks)
    if observations is None:
        observations = _RESOURCE_OBSERVATIONS
    specs = []
    for command, task in zip(commands, tasks):
        argv, vector = _command_spec(command)
        report = str(task["report_path"])
        if not report:
            raise WorkerMemoryError("resource worker needs a private RSS report path")
        vector = [entry for entry in vector
                  if not entry.startswith(RESOURCE_REPORT_ENV + "=")]
        vector.append(RESOURCE_REPORT_ENV + "=" + report)
        specs.append((argv, vector))
    tree_path = str(os.environ.get(TREE_STATE_ENV, "") or "")
    recovery_allowance = STATE_MAX_AGE_SECONDS
    guard_deadline = -1.0
    configured_recovery = False
    # Host recovery and native admission retain their separate ownership.
    # Snapshot freshness and the observation schema are shared and unchanged.
    import sys as execution_runtime
    if execution_runtime.implementation.name == "cpython":
        if tree_path:
            from pcc.frontends.python.worker_resource_plan import TREE_RECOVERY_ENV

            raw_recovery = os.environ.get(TREE_RECOVERY_ENV)
            if raw_recovery is not None:
                from pcc.frontends.python.worker_resource_host_recovery import (
                    read_tree_recovery,
                )
                recovery_allowance, guard_deadline = read_tree_recovery(raw_recovery)
                configured_recovery = True
    owner_pid = os.getpid()
    pending, bands = resource_task_order(
        tasks, guarded_calibration=bool(tree_path),
        native_cold_cover=(
            execution_runtime.implementation.name == "pcc"
            and not any(len(sample) not in (3, 4) or sample[0] == tasks[0]["class"]
                        for sample in observations)
        ),
    )
    active = []
    attempt_tokens = {}
    retries = [0 for _task in tasks]
    completed = set()
    completed_tokens = {}
    handoff_slots = set()
    unavailable_since = -1.0
    outage_expiry = -1.0
    fresh_after = time.monotonic()
    state_diagnostic = {}
    preflight_done = False
    available = 0
    try:
        while pending or active:
            survivors = []
            for index, pid, reservation, exclusive, observed_peak in active:
                report = read_worker_resource(str(tasks[index]["report_path"]), pid, attempt_tokens[pid])
                if report is not None:
                    observed_peak = max(observed_peak, report[2])
                    if not exclusive:
                        reservation = max(reservation, peak_reservation(observed_peak))
                result = _poll_resource_worker(pid)
                if result == _WORKER_RUNNING:
                    survivors.append((index, pid, reservation, exclusive, observed_peak))
                    continue
                _retire_resource_worker(pid)
                fresh_after = time.monotonic()
                active = [item for item in active if item[1] != pid]
                if result:
                    _resource_event(trace_path, "failed", index, pid, reservation, 0, observed_peak)
                    _resource_diagnostic(tasks[index], "failed", index, pid, reservation, 0, observed_peak,
                                         bands[index] if bands else -1)
                    if trace_path:
                        # Failure-only evidence: peers earlier in this scan
                        # may have a newer accepted peak than active contains.
                        # Record the charge actually seen, never resample or
                        # change a scheduling decision to explain the failure.
                        try:
                            charge = reservation
                            for sibling in active:
                                for seen in survivors:
                                    if seen[1] == sibling[1]:
                                        sibling = seen
                                        break
                                charge += sibling[2]
                                _resource_event(trace_path, "failure-active", sibling[0], sibling[1],
                                                sibling[2], available, sibling[4])
                            _resource_event(trace_path, "failure-budget", -1, 0, charge, available, 0)
                        except OSError:
                            pass  # Extra evidence must not hide the child failure.
                    raise subprocess.CalledProcessError(result, commands[index])
                # The child may publish complete and exit between the earlier
                # live read and poll. Only a fresh post-success read can bind
                # completion to this PID and this exact scheduled attempt.
                report = read_worker_resource(str(tasks[index]["report_path"]), pid, attempt_tokens[pid])
                if report is None or report[0] != "complete":
                    _resource_event(trace_path, "unverified-retire", index, pid, reservation, 0, observed_peak)
                    _resource_diagnostic(tasks[index], "unverified-retire", index, pid, reservation, 0, observed_peak,
                                         bands[index] if bands else -1)
                    raise WorkerMemoryError("completed worker did not publish final peak RSS: task=" + str(index))
                observed_peak = max(observed_peak, report[2])
                _resource_event(trace_path, "retire", index, pid, reservation, 0, observed_peak)
                _resource_diagnostic(tasks[index], "retire", index, pid, reservation, 0, observed_peak,
                                         bands[index] if bands else -1)
                if tasks[index].get("handoff_request", ""):
                    from pcc.frontends.python.pipeline_indexed_handoff import retire_handoff_task

                    retire_handoff_task(tasks[index], attempt_tokens[pid])
                observations.append(completed_task_observation(tasks[index], observed_peak))
                completed_tokens[index] = attempt_tokens[pid]
                if tasks[index].get("retry_calibration", False):
                    tasks[index]["retry_calibration"] = False
                completed.add(index)
                slot = tasks[index].get("handoff_slot", -1)
                if slot >= 0 and slot != index:
                    handoff_slots.remove(slot)
            active = survivors
            if not pending and not active:
                break
            owner_rss = _coordinator_rss_bytes()
            read_diagnostic = {} if unavailable_since >= 0.0 else None
            state = read_tree_state(tree_path, tree_budget, owner_pid,
                                    [item[1] for item in active], fresh_after, include_owner=True,
                                    diagnostic=read_diagnostic)
            if state is None and read_diagnostic is not None:
                state_diagnostic = read_diagnostic
            if tree_path and (state is None or configured_recovery):
                now = time.monotonic()
                if state is None and unavailable_since < 0.0:
                    unavailable_since = now
                    if configured_recovery:
                        # Fix this outage once. Reaps, reports, new barriers
                        # and changing rejection reasons cannot extend it.
                        outage_expiry = min(now + recovery_allowance, guard_deadline)
                if ((configured_recovery and (
                        now >= guard_deadline
                        or (outage_expiry >= 0.0 and now >= outage_expiry)))
                        or (not configured_recovery and state is None
                            and now - unavailable_since > STATE_MAX_AGE_SECONDS)):
                    raise WorkerMemoryError(
                        "worker tree RSS state is missing, stale, or incompatible"
                        + "; last_rejected_snapshot=" + repr(state_diagnostic)
                        + "; active_tasks=" + repr([(item[0], item[1]) for item in active[:16]])
                    )
                if state is None:
                    # There is no usable available capacity during recovery.
                    # Poll/reap and valid peak collection continue above.
                    if configured_recovery:
                        available = 0
                    time.sleep(0.01)
                    continue
            # Processing/launch time before the first rejected observation
            # is not a continuous state outage. Reader freshness is separate.
            unavailable_since = -1.0
            outage_expiry = -1.0
            outside = 0 if state is None else state[0]
            available = available_worker_bytes(tree_budget, owner_rss, outside)
            if state is not None:
                updated = []
                for index, pid, reservation, exclusive, peak in active:
                    current_subtree = state[1].get(pid, 0)
                    peak = max(peak, current_subtree)
                    if not exclusive and peak > 0:
                        reservation = max(reservation, peak_reservation(peak))
                    updated.append((index, pid, reservation, exclusive, peak))
                active = updated
            if len(active) == 1 and active[0][4] > 0:
                measured_demand = peak_reservation(active[0][4])
                if state is not None:
                    index, pid, reservation, exclusive, peak = active[0]
                    # Historical owner/worker peaks need not overlap. Keep
                    # them for admission, floors and monotonic reservations;
                    # continuation requires the current synchronized sample
                    # to fit with the unchanged fixed headroom and tree cap.
                    if state[1].get(pid, 0) <= 0:
                        raise WorkerMemoryError("live worker subtree RSS is unavailable: task=" + str(index))
                    current_owner = state[1].get(owner_pid, 0)
                    current_available = available_worker_bytes(tree_budget, current_owner, outside)
                    if state[1][pid] > current_available:
                        # Manifests live in temporary directories that unwind
                        # after failure. Preserve only task identity and RSS
                        # evidence here, never argv, environment or input text.
                        task = tasks[index]
                        report = read_worker_resource(
                            str(task["report_path"]), pid, attempt_tokens[pid],
                        )
                        context = (
                            " phase=" + str(task.get("diagnostic_phase", "unknown"))
                            + " modules=" + repr(task.get("diagnostic_modules", []))
                            + " owner_reservation_rss_bytes=" + str(owner_rss)
                            + " current_owner_rss_bytes=" + str(current_owner)
                            + " admission_available_worker_bytes=" + str(available)
                            + " outside_owner_rss_bytes=" + str(outside)
                            + " current_worker_subtree_rss_bytes=" + str(state[1][pid])
                            + " worker_report_phase=" + (str(report[0]) if report else "unavailable")
                        )
                        raise WorkerMemoryError(
                            "live worker current subtree exceeds safe worker space: task=" + str(index)
                            + " observed_peak_so_far_bytes=" + str(peak)
                            + " available_worker_bytes=" + str(current_available)
                            + " budget=" + str(tree_budget)
                            + context
                            + "; full-task peak is unknown; tree cap is unchanged"
                        )
                if measured_demand > available and state is not None:
                    # A padded forecast controls admission of peers, not the
                    # current RSS ceiling. No new peer may share this worker.
                    index, pid, reservation, exclusive, peak = active[0]
                    reservation = max(reservation, measured_demand)
                    active = [(index, pid, reservation, True, peak)]
                    if not exclusive:
                        _resource_event(trace_path, "exclusive", index, pid,
                                        reservation, available, peak)
                        _resource_diagnostic(tasks[index], "exclusive", index, pid,
                                             reservation, available, peak,
                                             bands[index] if bands else -1)
                else:
                    if active[0][3] and measured_demand > available:
                        raise WorkerMemoryError(
                            "exclusive calibration exceeded safe worker space: task=" + str(active[0][0])
                            + " observed_peak_so_far_bytes=" + str(active[0][4])
                            + " available_worker_bytes=" + str(available)
                            + " budget=" + str(tree_budget)
                            + "; full-task peak is unknown; tree cap is unchanged"
                        )
                    require_task_fits(active[0][0], measured_demand, available, tree_budget)
            # Reservations can increase while peers are alive. Do not merely
            # stop launching and wait for the outer breaker to kill the tree:
            # retire a restartable peer and queue it once for exclusive
            # calibration. Its unfinished peak is only a lower bound, not a
            # completed demand estimate. Drain peers before that retry and
            # keep the process-tree cap and one-retry limit unchanged.
            while len(active) > 1 and sum(item[2] for item in active) > available:
                index, pid, reservation, exclusive, observed_peak = active[-1]
                if not tasks[index].get("restartable", False):
                    raise WorkerMemoryError("live worker reservations exceed the budget; task cannot be restarted")
                _stop_resource_worker(pid)
                active.pop()
                fresh_after = time.monotonic()
                _resource_event(trace_path, "cancel", index, pid, reservation, available, observed_peak)
                _resource_diagnostic(tasks[index], "cancel", index, pid, reservation, available, observed_peak,
                                     bands[index] if bands else -1)
                if retries[index] >= 1:
                    raise WorkerMemoryError("worker peak estimate remains unstable after one bounded retry: task=" + str(index))
                retries[index] += 1
                tasks[index]["retry_calibration"] = True
                if observed_peak > 0:
                    tasks[index]["incomplete_peak_bytes"] = max(
                        int(tasks[index].get("incomplete_peak_bytes", 0)), observed_peak,
                    )
                pending.insert(0, index)
            if not preflight_done:
                for index in pending:
                    demand = (minimum_task_bytes(tasks[index]) if state is not None
                              else estimated_task_bytes(tasks[index], observations))
                    if demand:
                        require_task_fits(index, demand, available, tree_budget)
                preflight_done = True
            while pending and len(active) < width:
                reservations = [item[2] for item in active]
                if any(item[3] for item in active):
                    break
                ready = []
                for item in pending:
                    slot = tasks[item].get("handoff_slot", -1)
                    if (slot == item and slot not in handoff_slots
                            and len(handoff_slots) >= width):
                        continue
                    dependency = tasks[item].get("depends_on", -1)
                    if dependency >= 0 and dependency not in completed:
                        continue
                    if tasks[item].get("handoff_request", "") and not tasks[item].get("input_ready", False):
                        from pcc.frontends.python.pipeline_indexed_handoff import prepare_handoff_task

                        prepare_handoff_task(tasks[item], completed_tokens[dependency])
                    input_path = str(tasks[item].get("input_path", "") or "")
                    if input_path and not tasks[item].get("input_ready", False):
                        with open(input_path, "rb") as stream:
                            stream.seek(0, 2)
                            size = int(stream.tell())
                        tasks[item]["inputs"] = [size]
                        cost = tasks[item].get("input_cost", [])
                        if cost:
                            tasks[item]["estimate_bytes"] = cost[0] + size * cost[1] // 1000000
                        from pcc.frontends.python.pipeline_stage1_checkpoint import file_sha256
                        tasks[item]["source_identity"] = file_sha256(input_path)
                        tasks[item]["input_ready"] = True
                    ready.append(item)
                ready = ready_resource_cohort(
                    ready, pending, [item[0] for item in active], bands,
                )
                index, demand, exclusive = choose_task(
                    ready, tasks, observations, reservations, width, available,
                    guarded_calibration=state is not None,
                )
                if index < 0:
                    if not active:
                        if not ready:
                            raise WorkerMemoryError("resource worker dependency graph cannot make progress")
                        first = ready[0]
                        require_task_fits(first, estimated_task_bytes(tasks[first], observations),
                                          available, tree_budget)
                    _resource_event(trace_path, "backoff", -1, 0, sum(reservations), available, 0)
                    break
                admission_demand = (max(1, minimum_task_bytes(tasks[index]))
                                    if exclusive and state is not None else demand)
                require_task_fits(index, admission_demand, available, tree_budget)
                report_path = str(tasks[index]["report_path"])
                if os.path.exists(report_path):
                    os.unlink(report_path)
                import hashlib

                attempt = (
                    str(owner_pid) + "|" + str(index) + "|" + str(retries[index])
                    + "|" + str(time.monotonic()) + "|" + tasks[index]["class"]
                    + "|" + repr(tasks[index]["inputs"])
                    + "|" + str(tasks[index].get("source_identity", ""))
                )
                token = hashlib.sha256(attempt.encode("utf-8")).hexdigest()
                argv, vector = specs[index]
                vector = [entry for entry in vector if not entry.startswith(RESOURCE_TOKEN_ENV + "=")]
                vector.append(RESOURCE_TOKEN_ENV + "=" + token)
                if tasks[index].get("handoff_request", ""):
                    from pcc.frontends.python.pipeline_indexed_handoff import ENV_SEAL, reset_handoff_output

                    reset_handoff_output(tasks[index])
                    vector = [entry for entry in vector if not entry.startswith(ENV_SEAL + "=")]
                    vector.append(ENV_SEAL + "=" + tasks[index]["handoff_seal_sha256"])
                specs[index] = (argv, vector)
                pid = _start_resource_worker(specs, index)
                if pid <= 0:
                    raise subprocess.CalledProcessError(127, commands[index])
                if tasks[index].get("handoff_slot", -1) == index:
                    handoff_slots.add(index)
                pending.remove(index)
                attempt_tokens[pid] = token
                active.append((index, pid, demand, exclusive, 0))
                # Keep the established retry launch event; exclusivity is
                # the reservation and active state, not the event's name.
                _resource_event(trace_path, "calibrate" if exclusive and retries[index] == 0 else "start",
                                index, pid, demand, available, 0)
                _resource_diagnostic(tasks[index], "calibrate" if exclusive and retries[index] == 0 else "start",
                                     index, pid, demand, available, 0,
                                     bands[index] if bands else -1)
                if exclusive:
                    break
                # Refresh live reports and the synchronized tree before the
                # next launch; startup can invalidate an earlier reservation.
                break
            if active:
                time.sleep(0.01)
    finally:
        for _index, pid, _reservation, _exclusive, _peak in active:
            _stop_resource_worker(pid)


def _host_pool(specs, width):
    """Keep the host compatibility entry outside native dependency discovery."""
    import sys as execution_runtime
    if execution_runtime.implementation.name != "pcc":
        from pcc.frontends.python.worker_process_host_pool import host_pool

        return host_pool(
            specs, width, os=os, subprocess=subprocess, sys=sys, time=time,
        )
    raise RuntimeError("host worker pool is unavailable under native pcc")


def run_worker_processes(commands, width):
    specs = [_command_spec(command) for command in commands]
    if sys.implementation.name == "pcc":
        result = _native_pool(specs, width)
    else:
        result = _host_pool(specs, width)
    if result:
        index = (result >> 32) - 1
        code = result & 4294967295
        if code >= 2147483648:
            code -= 4294967296
        raise subprocess.CalledProcessError(code, commands[index])


def _host_weighted_pool(specs, reservations, width, budget):
    """Keep the host compatibility entry outside native dependency discovery."""
    import sys as execution_runtime
    if execution_runtime.implementation.name != "pcc":
        from pcc.frontends.python.worker_process_host_pool import host_weighted_pool

        return host_weighted_pool(
            specs, reservations, width, budget,
            os=os, subprocess=subprocess, sys=sys, time=time,
        )
    raise RuntimeError("host worker pool is unavailable under native pcc")


def run_weighted_worker_processes(commands, reservations, width, budget):
    if len(commands) != len(reservations):
        raise ValueError("worker command/reservation inventory mismatch")
    if budget <= 0 or any(weight <= 0 for weight in reservations):
        raise ValueError("weighted worker reservations require positive bytes")
    specs = [_command_spec(command) for command in commands]
    if sys.implementation.name == "pcc":
        result = _native_weighted_pool(specs, reservations, width, budget)
    else:
        result = _host_weighted_pool(specs, reservations, width, budget)
    if result:
        index = (result >> 32) - 1
        code = result & 4294967295
        if code >= 2147483648:
            code -= 4294967296
        raise subprocess.CalledProcessError(code, commands[index])


def _followup_reservation(path, floor):
    base, per_mb, cap = floor
    size = os.path.getsize(path)
    return max(1, min(cap, base + size * per_mb // 1000000))


def _host_chained_pool(specs, reservations, followups, followup_paths, floor,
                       width, budget):
    """Keep the host compatibility entry outside native dependency discovery."""
    import sys as execution_runtime
    if execution_runtime.implementation.name != "pcc":
        from pcc.frontends.python.worker_process_host_pool import host_chained_pool

        return host_chained_pool(
            specs, reservations, followups, followup_paths, floor, width, budget,
            os=os, subprocess=subprocess, sys=sys, time=time,
            followup_reservation=_followup_reservation,
        )
    raise RuntimeError("host worker pool is unavailable under native pcc")


def run_chained_worker_processes(commands, reservations, followups,
                                 followup_paths, followup_floor, width, budget):
    """Run ``commands`` as a weighted pool; ``followups[i]`` runs after
    ``commands[i]`` exits 0.

    A follow-up's reservation is ``min(cap, base + size * per_mb // 10**6)``
    over the size of ``followup_paths[i]`` -- the file ``commands[i]`` wrote --
    with ``followup_floor = (base, per_mb, cap)``.  Primaries keep priority;
    a ready follow-up starts only when no remaining primary fits.
    """
    count = len(commands)
    if not (len(reservations) == len(followups) == len(followup_paths) == count):
        raise ValueError("chained worker inventory mismatch")
    if budget <= 0 or any(weight <= 0 for weight in reservations):
        raise ValueError("weighted worker reservations require positive bytes")
    floor = tuple(int(value) for value in followup_floor)
    if len(floor) != 3 or any(value < 0 for value in floor):
        raise ValueError("follow-up floor must be (base, per_mb, cap)")
    specs = [_command_spec(command) for command in commands]
    followup_specs = [_command_spec(command) for command in followups]
    paths = [str(path) for path in followup_paths]
    if sys.implementation.name == "pcc":
        result = _native_chained_pool(
            specs, list(reservations), followup_specs, paths, floor, width, budget,
        )
    else:
        result = _host_chained_pool(
            specs, list(reservations), followup_specs, paths, floor, width, budget,
        )
    if result:
        index = (result >> 32) - 1
        code = result & 4294967295
        if code >= 2147483648:
            code -= 4294967296
        command = commands[index] if index < count else followups[index - count]
        raise subprocess.CalledProcessError(code, command)
