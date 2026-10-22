"""Bounded host frontend -> indexed-object process handoffs.

The feature only changes process ownership.  It uses the ordinary frontend,
post-pass PIDX codec, target emitter, and measured resource admission rules.
No native fitted memory coefficients or cached historical peaks are used.
"""

import os
import sys


HOST_SPLIT_ENV = "PCC_HOST_INDEXED_PROCESS_SPLIT"
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def require_host_indexed_split(env, *, checkpoint_root, action_cache_plan,
                               libpython_mode, artifact_dir):
    """Fail before building exports when an explicit split cannot be owned."""
    if checkpoint_root or action_cache_plan is not None:
        raise ValueError("host indexed process split cannot share a checkpoint or action cache")
    if libpython_mode != "off" or not artifact_dir:
        raise ValueError("host indexed process split requires no-libpython and an artifact root")
    for name in ("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "PCC_DIRECT_INDEXED_KERNEL_EMIT",
                 "PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK"):
        if str(env.get(name, "") or "").strip().lower() not in _TRUE:
            raise ValueError("host indexed process split requires " + name)
    for name in ("PCC_DIRECT_INDEXED_KERNEL_VALIDATE", "PCC_TEXT_INDEXED_KERNEL_EMIT",
                 "PCC_DIRECT_INDEXED_SIDECAR", "PCC_PY_FRONTEND_IN_PROCESS_CODEGEN"):
        if str(env.get(name, "") or "").strip().lower() in _TRUE:
            raise ValueError("host indexed process split does not support " + name)
    if str(env.get("PCC_DIRECT_INDEXED_NATIVE_OBJECT", "1") or "1").strip().lower() in _FALSE:
        raise ValueError("host indexed process split requires native object output")
    if str(env.get("PCC_DEFER_FRONTEND_CODEGEN_PLAN", "") or "").strip():
        raise ValueError("host indexed process split cannot share a native deferred plan")
    from pcc.frontends.python.pipeline_frontend_workers import worker_tree_budget_bytes

    if worker_tree_budget_bytes(env.get("PCC_WORKER_TREE_BUDGET_BYTES", "")) <= 0:
        raise ValueError("host indexed process split requires measured tree admission")


def run_host_indexed_stage(commands, manifest_paths, result_paths, chunks,
                           module_names, ir_dir, worker_prefix, jobs,
                           worker_environment, shell_quote_arg):
    """Keep at most ``jobs`` not-yet-consumed modules in dynamic slots.

    Each FE acquires a slot until its own dependent BE is reaped, its final
    RSS/attempt is verified, and its task-owned intermediate is removed.
    Free slots admit any fitting FE; cancellation never releases a slot.
    """
    from pcc.frontends.python.pipeline_frontend_workers import (
        resource_tasks_for_commands, worker_tree_budget_bytes,
    )
    from pcc.frontends.python.pipeline_frontend_worker_execution import _direct_owned_pass_names
    from pcc.frontends.python.pipeline_indexed_handoff import ENV_REQUEST, write_request
    from pcc.frontends.python.pipeline_stage1_checkpoint import file_sha256
    from pcc.frontends.python.pipeline_targets import host_target_triple
    from pcc.frontends.python.worker_process_pool import run_resource_worker_processes
    from pcc.frontends.python.worker_resource_plan import resource_task_order

    count = len(commands)
    if not (len(manifest_paths) == len(result_paths) == len(chunks) == count):
        raise ValueError("host indexed process split inventory differs")
    if not count:
        return
    if jobs < 1 or any(len(chunk) != 1 for chunk in chunks):
        raise ValueError("host indexed process split requires positive width and singleton workers")
    target = host_target_triple()
    if target == "unknown-unknown-unknown":
        raise ValueError("host indexed process split requires a concrete host target")
    frontend_tasks = resource_tasks_for_commands(commands)
    if frontend_tasks is None or len(frontend_tasks) != count:
        raise ValueError("host indexed process split requires complete worker resource identities")
    frontend_tasks = [dict(task) for task in frontend_tasks]
    for task in frontend_tasks:
        if task.get("startup_prior_model", "") == "host-codegen-v1":
            task["startup_prior_model"] = "host-indexed-frontend-v1"
    backend_host_prior = (sys.implementation.name == "cpython"
                          and list(worker_prefix) == [sys.executable, "-m", "pcc"])
    order, _bands = resource_task_order(frontend_tasks)
    width = min(jobs, count)
    all_commands = []
    all_tasks = []
    outputs = {}
    for position, source_position in enumerate(order):
        index = chunks[source_position][0]
        if index < 0 or index >= len(module_names) or index in outputs:
            raise ValueError("host indexed process split module assignment differs")
        module_name = module_names[index]
        manifest = os.path.abspath(manifest_paths[source_position])
        sidecar = os.path.abspath(os.path.join(ir_dir, "module_" + str(index) + ".direct.pidx"))
        output = os.path.abspath(os.path.join(ir_dir, "module_" + str(index) + ".direct.pco"))
        request_path = sidecar + ".request.json"
        task = dict(frontend_tasks[source_position])
        identity = task["source_identity"]
        request = {
            "schema": "pcc.indexed-handoff.request.v1",
            "manifest_path": manifest,
            "manifest_sha256": file_sha256(manifest),
            "source_identity": identity,
            "index": index,
            "module": module_name,
            "target": target,
            "passes": _direct_owned_pass_names(module_name),
            "sidecar_path": sidecar,
            "output_path": output,
            "seal_path": sidecar + ".seal.json",
            "receipt_path": output + ".receipt.json",
        }
        write_request(request_path, request)
        task["class"] += "|host-indexed-frontend-v1"
        task["diagnostic_phase"] = "indexed-frontend"
        task["calibrate_before_peers"] = True
        task["depends_on"] = -1
        task["handoff_slot"] = 2 * position
        all_commands.append(ENV_REQUEST + "=" + shell_quote_arg(request_path) + " " + commands[source_position])
        all_tasks.append(task)
        backend_command = worker_environment + " " + ENV_REQUEST + "=" + shell_quote_arg(request_path)
        backend_command += " " + " ".join(shell_quote_arg(str(part)) for part in worker_prefix)
        backend_command += " --pcc-self-backend-indexed-emit-worker "
        backend_command += " ".join(shell_quote_arg(part) for part in (sidecar, output, "PCO"))
        all_commands.append(backend_command)
        all_tasks.append({
            "class": task["class"] + "|host-indexed-backend-v2|PCO|optimize=0",
            "inputs": [0] * 47,
            "estimate_bytes": 0,
            "report_path": output + ".rss",
            "depends_on": 2 * position,
            "handoff_slot": 2 * position,
            "restartable": True,
            "diagnostic_phase": "indexed-backend",
            "calibrate_before_peers": True,
            "diagnostic_modules": [module_name],
            "diagnostic_indices": [index],
            "handoff_request": request_path,
            "handoff_source_identity": identity,
            "input_ready": False,
        })
        if backend_host_prior:
            all_tasks[-1]["startup_prior_model"] = "host-indexed-backend-v2"
        outputs[index] = (sidecar, output)
    budget = worker_tree_budget_bytes(os.environ.get("PCC_WORKER_TREE_BUDGET_BYTES", ""))
    run_resource_worker_processes(
        all_commands, all_tasks, width, budget,
        trace_path=manifest_paths[0] + ".indexed-stages.tsv",
    )
    # Keep the established downstream collector/link API.  No object is
    # exposed until the complete pool has retired and verified all receipts.
    for position, result_path in enumerate(result_paths):
        index = chunks[position][0]
        sidecar, output = outputs[index]
        with open(result_path, "r", encoding="utf-8") as stream:
            rows = stream.read().splitlines()
        if len(rows) != 1:
            raise ValueError("host indexed process split requires one result row")
        fields = rows[0].split("\t")
        if (len(fields) not in (9, 12) or fields[:3] != ["OK", str(index), module_names[index]]
                or fields[-2:] != ["PIDX", sidecar] or not os.path.isfile(output)):
            raise ValueError("host indexed process split result publication differs")
        fields[-2:] = ["PCO", output]
        temporary = result_path + ".indexed.tmp"
        try:
            with open(temporary, "w", encoding="utf-8") as stream:
                stream.write("\t".join(fields) + "\n")
            os.replace(temporary, result_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
