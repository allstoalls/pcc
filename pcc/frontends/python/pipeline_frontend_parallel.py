"""Parallel multi-module frontend scheduling behind the pipeline facade."""

from __future__ import annotations

import os
import json
import subprocess
import tempfile
import time
from typing import Optional

from pcc.frontends.python import module_action_dag as _module_action_dag
from pcc.frontends.python.pipeline_frontend_workers import SOURCE_WORKER_AUTO_SAFE_JOBS as _SOURCE_WORKER_AUTO_SAFE_JOBS, WORKER_TREE_BUDGET_ENV as _WORKER_TREE_BUDGET_ENV, compiled_native_auto_jobs as _compiled_native_auto_jobs, compiled_native_export_jobs as _compiled_native_export_jobs, compiled_native_summary_jobs as _compiled_native_summary_jobs, compiled_native_summary_plan as _compiled_native_summary_plan, numeric_jobs_override as _numeric_jobs_override, split_codegen_chunks_by_source_size as _split_codegen_chunks_by_source_size, worker_tree_budget_bytes as _worker_tree_budget_bytes
from pcc.frontends.python.pipeline_pass_config import parallel_cpu_budget as _parallel_cpu_budget


_MODULE_IR_ARTIFACT_SCHEMA = "pcc.python-module-ir-action.v1"
_DEFERRED_CODEGEN_SCHEMA = "pcc.frontend-codegen-plan.v2"
_DEFERRED_INDEXED_PROCESS_SPLIT = "pidx-pco-v1"
_DEFERRED_CODEGEN_RESULT = "PCC_DEFERRED_FRONTEND_CODEGEN"


def _copy_frontend_artifact_file(source: str, destination: str) -> None:
    """Copy generated compiler inputs without a shell or external utility."""
    with open(source, "rb") as reader:
        with open(destination, "wb") as writer:
            while True:
                chunk = reader.read(1048576)
                if not chunk:
                    break
                offset = 0
                while offset < len(chunk):
                    written = writer.write(chunk[offset:])
                    if written <= 0:
                        raise OSError("short write copying frontend artifact: " + destination)
                    offset += written


def _copy_frontend_artifact_tree(source: str, destination: str) -> None:
    """Copy the compiler-created AST tree into its persistent plan directory."""
    os.makedirs(destination, exist_ok=False)
    for name in sorted(os.listdir(source)):
        child_source = os.path.join(source, name)
        child_destination = os.path.join(destination, name)
        if os.path.isdir(child_source):
            _copy_frontend_artifact_tree(child_source, child_destination)
        else:
            _copy_frontend_artifact_file(child_source, child_destination)


def _write_deferred_codegen_plan(
    plan_path: str,
    *,
    worker_executable: str,
    output_path: str,
    runtime_archive: str,
    artifact_root: str,
    module_count: int,
    oversized_count: int,
    safe_jobs: int,
    manifests,
) -> None:
    with open(plan_path, "w", encoding="utf-8") as stream:
        stream.write(_DEFERRED_CODEGEN_SCHEMA + "\n")
        stream.write(os.path.abspath(worker_executable) + "\n")
        stream.write(os.path.abspath(output_path) + "\n")
        stream.write(os.path.abspath(runtime_archive) + "\n")
        stream.write(os.path.abspath(plan_path + ".link-profile.json") + "\n")
        stream.write(os.path.abspath(plan_path + ".internal-inputs") + "\n")
        stream.write(os.path.abspath(artifact_root) + "\n")
        stream.write(str(module_count) + "\n")
        stream.write(str(oversized_count) + "\n")
        stream.write(str(safe_jobs) + "\n")
        stream.write(str(len(manifests)) + "\n")
        stream.write(_DEFERRED_INDEXED_PROCESS_SPLIT + "\n")
        for manifest_path in manifests:
            stream.write(os.path.abspath(str(manifest_path)) + "\n")


def _module_ir_action(state, module_name: str):
    return _module_action_dag.Action(
        module=str(module_name),
        stage="module-ir",
        key=_module_action_dag.action_key(state, str(module_name), "module-ir"),
        reason="content-addressed-reuse",
    )


def _encode_module_ir_artifact(
    module_name: str,
    ir_text: str,
    needs_libpython: bool,
    needs_native_exports: bool,
    ir_bytes_before_passes: int,
) -> bytes:
    body = str(ir_text).encode("utf-8")
    header = {
        "body_bytes": len(body),
        "ir_bytes_before_passes": int(ir_bytes_before_passes),
        "module": str(module_name),
        "needs_libpython": bool(needs_libpython),
        "needs_native_extension_exports": bool(needs_native_exports),
        "schema": _MODULE_IR_ARTIFACT_SCHEMA,
    }
    return (
        json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
        + body
    )


def _decode_module_ir_artifact(payload: bytes, module_name: str):
    try:
        header_raw, body = payload.split(b"\n", 1)
        header = json.loads(header_raw.decode("utf-8"))
        if not isinstance(header, dict) or set(header) != {
            "body_bytes",
            "ir_bytes_before_passes",
            "module",
            "needs_libpython",
            "needs_native_extension_exports",
            "schema",
        }:
            return None
        if (
            header.get("schema") != _MODULE_IR_ARTIFACT_SCHEMA
            or header.get("module") != str(module_name)
            or type(header.get("needs_libpython")) is not bool
            or type(header.get("needs_native_extension_exports")) is not bool
            or int(header.get("body_bytes", -1)) != len(body)
        ):
            return None
        ir_size = int(header.get("ir_bytes_before_passes", -1))
        if ir_size < 1 or not body:
            return None
        ir_text = body.decode("utf-8")
        if not ir_text:
            return None
        return (
            ir_text,
            bool(header["needs_libpython"]),
            bool(header["needs_native_extension_exports"]),
            ir_size,
        )
    except (UnicodeDecodeError, ValueError, TypeError):
        return None


def _load_noop_action_result(action_cache_plan, src_paths, module_names):
    """Load a graph whose source/global identities are exactly unchanged."""

    if action_cache_plan is None:
        return None
    action_root = str(action_cache_plan.get("action_root", ""))
    if not action_root:
        return None
    try:
        state = _module_action_dag.load_graph_state_file(action_root)
        if state is None:
            return None
        if (
            state.compiler_digest
            != str(action_cache_plan.get("compiler_digest", ""))
            or state.runtime_abi_digest
            != str(action_cache_plan.get("runtime_abi_digest", ""))
            or state.target != str(action_cache_plan.get("target", ""))
            or state.options_digest
            != str(action_cache_plan.get("options_digest", ""))
        ):
            return None
        modules = state.module_map()
        expected_modules: list[str] = []
        for name in module_names:
            clean_name = str(name)
            if clean_name not in expected_modules:
                expected_modules.append(clean_name)
        if len(modules) != len(expected_modules):
            return None
        for module_name in expected_modules:
            if module_name not in modules:
                return None
        for src_path, module_name in zip(src_paths, module_names):
            module = modules.get(str(module_name))
            if (
                module is None
                or module.source_digest
                != _module_action_dag.source_digest(str(src_path))
            ):
                return None

        module_ir_texts = []
        any_needs_libpython = False
        any_needs_native = False
        total_ir_bytes = 0
        libpython_modules: list[str] = []
        for module_name in module_names:
            action = _module_ir_action(state, str(module_name))
            payload = _module_action_dag.load_action_artifact(action_root, action)
            if payload is None:
                return None
            decoded = _decode_module_ir_artifact(payload, str(module_name))
            if decoded is None:
                return None
            ir_text, needs_libpython, needs_native, ir_size = decoded
            module_ir_texts.append((str(module_name), ir_text))
            total_ir_bytes += int(ir_size)
            if needs_libpython:
                any_needs_libpython = True
                libpython_modules.append(str(module_name))
            if needs_native:
                any_needs_native = True
        return (
            module_ir_texts,
            any_needs_libpython,
            any_needs_native,
            total_ir_bytes,
            libpython_modules,
        )
    except Exception:
        return None


def _summary_input_size_bytes(path: str) -> int:
    # The older getsize provider reads the entire file. These are owned regular
    # artifacts; seek/tell keeps observation memory independent of file size.
    with open(path, "rb") as stream:
        stream.seek(0, 2)
        return int(stream.tell())


def _build_vthread_effect_summaries(
    tmp: str,
    src_paths,
    module_names,
    worker_prefix,
    *,
    entry_module: str,
    sibling_inits,
    libpython_mode: str,
    ir_scaffold_mode: str,
    verbose: bool,
    max_parallel: int,
    ast_dir: str,
    exports_path: str,
    write_manifest,
    shell_quote_arg,
    worker_arg: str,
    worker_env_prefix,
    join_strings,
    run_worker_commands,
    pipeline_error,
) -> tuple[list[str], int]:
    summary_dir = os.path.join(tmp, "summaries")
    os.makedirs(summary_dir, exist_ok=True)
    result_paths: list[str] = []
    commands: list[str] = []
    assignments: list[list[int]] = []
    summary_parallel = _summary_worker_parallelism(max_parallel, worker_prefix)
    summary_chunks = [list(range(index, min(index + 8, len(module_names))))
                      for index in range(0, len(module_names), 8)]
    selected_worker_environment = ""
    if len(worker_prefix) == 1 and _worker_tree_budget_bytes(
        os.environ.get(_WORKER_TREE_BUDGET_ENV, "")
    ) <= 0:
        ast_sizes = [
            _summary_input_size_bytes(os.path.join(ast_dir, "module_" + str(index) + ".json"))
            for index in range(len(module_names))
        ]
        summary_chunks, summary_parallel, admission = _compiled_native_summary_plan(
            summary_parallel, ast_sizes, _summary_input_size_bytes(exports_path),
        )
        selected_worker_environment = str(admission["worker_env"])
        with open(os.path.join(tmp, "summary-admission.json"), "w", encoding="utf-8") as stream:
            json.dump(admission, stream, sort_keys=True)
    for assigned in summary_chunks:
        # Summaries are independent per module; splitting a batch changes only
        # process lifetime, not module order, effect wires, or the fixed point.
        index = assigned[0]
        manifest_path = os.path.join(tmp, "summary_" + str(index) + ".manifest")
        result_path = os.path.join(tmp, "summary_" + str(index) + ".tsv")
        write_manifest(
            manifest_path,
            result_path,
            summary_dir,
            exports_path,
            ast_dir,
            src_paths,
            module_names,
            assigned,
            entry_module=entry_module,
            sibling_inits=sibling_inits,
            libpython_mode=libpython_mode,
            ir_scaffold_mode=ir_scaffold_mode,
            verbose=verbose,
            job_kind="summary",
        )
        command_parts = [shell_quote_arg(part) for part in worker_prefix]
        command_parts.append(shell_quote_arg(worker_arg))
        command_parts.append(shell_quote_arg(manifest_path))
        command_environment = worker_env_prefix()
        if selected_worker_environment:
            command_environment += " " + selected_worker_environment
        commands.append(command_environment + " " + join_strings(command_parts, " "))
        result_paths.append(result_path)
        assignments.append(assigned)
    run_worker_commands(commands, max_parallel=summary_parallel)
    summary_by_index: list[Optional[str]] = [None for _name in module_names]
    for result_index, result_path in enumerate(result_paths):
        if not os.path.isfile(result_path):
            raise pipeline_error(
                "frontend summary worker produced no result: " + result_path
            )
        with open(result_path, "r", encoding="utf-8") as stream:
            lines = stream.read().splitlines()
        if lines and lines[0].startswith("ERR\t"):
            raise pipeline_error(lines[0].split("\t", 1)[1])
        assigned = assignments[result_index]
        if len(lines) != len(assigned):
            raise pipeline_error("invalid frontend summary worker result")
        for row_index, line in enumerate(lines):
            parts = line.split("\t")
            if len(parts) != 4 or parts[0] != "SUMMARY":
                raise pipeline_error("invalid frontend summary worker result")
            try:
                index = int(parts[1])
            except ValueError as exc:
                raise pipeline_error("invalid frontend summary worker index") from exc
            expected_path = os.path.join(
                summary_dir,
                "summary_" + str(index) + ".wire",
            )
            if (
                index != assigned[row_index]
                or parts[2] != module_names[index]
                or summary_by_index[index] is not None
                or parts[3] != expected_path
                or not os.path.isfile(parts[3])
            ):
                raise pipeline_error("frontend summary worker ownership mismatch")
            summary_by_index[index] = parts[3]
    summaries: list[str] = []
    for index, path in enumerate(summary_by_index):
        if path is None:
            raise pipeline_error(
                "frontend summary worker missed module " + str(index)
            )
        summaries.append(path)
    return summaries, summary_parallel


def _summary_worker_parallelism(max_parallel: int, worker_prefix) -> int:
    """Choose bounded summary width without multiplying native worker memory."""
    if _worker_tree_budget_bytes(os.environ.get(_WORKER_TREE_BUDGET_ENV, "")) > 0:
        raw = str(os.environ.get("PCC_PY_FRONTEND_SUMMARY_JOBS", "") or "").strip()
        try:
            return max(1, min(max_parallel, int(raw))) if raw else max(1, max_parallel)
        except ValueError:
            return 1
    if max_parallel <= 1:
        return _compiled_native_summary_jobs(1) if len(worker_prefix) == 1 else 1
    raw = str(
        os.environ.get("PCC_PY_FRONTEND_SUMMARY_JOBS", "") or ""
    ).strip()
    if raw:
        try:
            requested = int(raw)
        except ValueError:
            return 1
        if requested <= 0:
            return 1
        selected = min(max_parallel, requested)
        return _compiled_native_summary_jobs(selected) if len(worker_prefix) == 1 else selected
    # Source-mode workers are ``python -m pcc`` and use the host allocator;
    # their private pycache makes startup cheap, so use the existing frontend
    # width.  Compiled short-lived summary workers use their measured light
    # memory class; codegen workers keep the independent width-two risk cap.
    if len(worker_prefix) > 1:
        return max_parallel
    requested = max_parallel
    if not _numeric_jobs_override(
        str(os.environ.get("PCC_PY_FRONTEND_JOBS", "") or "")
    ):
        requested = max(max_parallel, _parallel_cpu_budget())
    return _compiled_native_summary_jobs(requested)


def _retain_stage2_export_manifest(manifest_path: str, module_names, chunk, worker_prefix) -> None:
    """Best-effort copy of one normal native export's unmodified input."""
    try:
        directory = str(os.environ.get("PCC_STAGE2_EXPORT_EVIDENCE_DIR", "") or "")
        if (
            not directory
            or len(worker_prefix) != 1
            or len(chunk) != 1
            or module_names[chunk[0]] != "pcc.frontends.c.parse.c_parsetab"
        ):
            return
        # A previous claim, including an incomplete one, must not be replaced.
        os.makedirs(directory, exist_ok=False)
        destination = os.path.join(directory, "selected.manifest")
        with open(manifest_path, "rb") as reader:
            payload: bytes = reader.read()
        with open(destination + ".pending", "wb") as writer:
            if writer.write(payload) != len(payload):
                return
            writer.flush()
        os.replace(destination + ".pending", destination)
    except Exception:
        # Evidence I/O must not replace a worker/compilation failure.
        return


def build_shared_exports(
    tmp: str,
    src_paths,
    module_names,
    chunks,
    worker_prefix,
    *,
    entry_module: str,
    sibling_inits,
    libpython_mode: str,
    ir_scaffold_mode: str,
    verbose: bool,
    max_parallel: int,
    oversized_chunk_count: int,
    safe_parallel: int,
    ast_dir: str = "",
    profile: Optional[dict] = None,
    module_dependency_map=None,
    build_unique_class_preload_index=None,
    contextual_host_for_module=None,
    build_contextual_host_export_surface=None,
    write_manifest,
    shell_quote_arg,
    worker_arg: str,
    worker_env_prefix,
    join_strings,
    run_worker_commands,
    profiled_gc_collect,
    read_native_exports_wire,
    read_reexport_edges_wire,
    merge_reexport_edges,
    flatten_class_export_fields,
    repair_default_global_owners,
    merge_mixin_stack_methods,
    merge_codegen_methods,
    expand_valueclass_export_refs,
    apply_function_object_uses,
    read_ast_wire,
    annotate_vthread_effects,
    annotate_vthread_effect_summaries=None,
    vthread_effect_export_surface=None,
    derived_class_map,
    write_native_exports_wire,
    profile_counter,
    pipeline_error,
) -> str:
    export_dir = os.path.join(tmp, "exports")
    os.makedirs(export_dir, exist_ok=True)
    result_paths: list[str] = []
    commands: list[str] = []
    for worker_index, chunk in enumerate(chunks):
        manifest_path = os.path.join(
            tmp,
            "export_" + str(worker_index) + ".manifest",
        )
        result_path = os.path.join(
            tmp,
            "export_" + str(worker_index) + ".tsv",
        )
        write_manifest(
            manifest_path,
            result_path,
            export_dir,
            "",
            ast_dir,
            src_paths,
            module_names,
            chunk,
            entry_module=entry_module,
            sibling_inits=sibling_inits,
            libpython_mode=libpython_mode,
            ir_scaffold_mode=ir_scaffold_mode,
            verbose=verbose,
            job_kind="export",
        )
        _retain_stage2_export_manifest(manifest_path, module_names, chunk, worker_prefix)
        result_paths.append(result_path)
        command_parts = [shell_quote_arg(part) for part in worker_prefix]
        command_parts.append(shell_quote_arg(worker_arg))
        command_parts.append(shell_quote_arg(manifest_path))
        commands.append(
            worker_env_prefix() + " " + join_strings(command_parts, " ")
        )

    if oversized_chunk_count:
        run_worker_commands(
            commands[:oversized_chunk_count],
            max_parallel=1,
        )
    safe_commands = commands[oversized_chunk_count:]
    if safe_commands:
        run_worker_commands(safe_commands, max_parallel=safe_parallel)
    # An export worker that produced no result file is a hard failure, not a
    # cache miss.  The caller wraps this whole region in `except Exception` to
    # fall back when the ACTION CACHE is unusable, so a bare FileNotFoundError
    # from the loop below was swallowed there: stage2 reported zero IR modules
    # and the first visible symptom was the linker saying it had no inputs.
    missing = []
    for result_path in result_paths:
        if not os.path.exists(result_path):
            missing.append(os.path.basename(result_path))
    if missing:
        raise pipeline_error(
            "frontend export worker produced no result for "
            + join_strings(missing, ", ")
        )
    profiled_gc_collect(
        profile,
        "multi_frontend_export_worker_collect",
        allocations_owned_by_current_process=False,
    )

    native_exports = {}
    reexport_edges = []
    worker_module_dependencies = {}
    function_object_uses = []
    export_worker_sum_ms = 0
    export_worker_max_ms = 0
    for result_path in result_paths:
        with open(result_path, "r", encoding="utf-8") as stream:
            result_text = stream.read()
        for raw_line in result_text.splitlines():
            parts = raw_line.split("\t")
            if not parts:
                continue
            if parts[0] == "ERR":
                message = parts[1] if len(parts) > 1 else "worker error"
                raise pipeline_error(message)
            if parts[0] != "EXPORT" or len(parts) < 3:
                raise pipeline_error("invalid frontend export worker result")
            shard_exports, _derived, shard_object_uses = read_native_exports_wire(
                parts[1],
                include_function_object_uses=True,
            )
            for module_name, exports in shard_exports.items():
                native_exports[module_name] = exports
            function_object_uses.extend(shard_object_uses)
            shard_edges, shard_dependencies = read_reexport_edges_wire(
                parts[2],
                include_module_dependencies=True,
            )
            reexport_edges.extend(shard_edges)
            for dependency_row in shard_dependencies:
                if (
                    not isinstance(dependency_row, tuple)
                    or len(dependency_row) != 2
                    or not isinstance(dependency_row[0], str)
                    or not isinstance(dependency_row[1], tuple)
                ):
                    raise pipeline_error(
                        "invalid frontend module dependency row"
                    )
                dependency_module = dependency_row[0]
                if dependency_module in worker_module_dependencies:
                    raise pipeline_error(
                        "duplicate frontend module dependency row"
                    )
                worker_module_dependencies[dependency_module] = (
                    dependency_row[1]
                )
            if len(parts) >= 4:
                try:
                    worker_ms = int(parts[3])
                except ValueError:
                    worker_ms = 0
                export_worker_sum_ms += worker_ms
                if worker_ms > export_worker_max_ms:
                    export_worker_max_ms = worker_ms

    merge_reexport_edges(module_names, native_exports, reexport_edges)
    flatten_class_export_fields(native_exports)
    repair_default_global_owners(native_exports)
    merge_mixin_stack_methods(native_exports)
    merge_codegen_methods(native_exports)
    for visible_module_name, visible_exports in native_exports.items():
        expand_valueclass_export_refs(
            visible_module_name,
            visible_exports,
        )
    apply_function_object_uses(native_exports, function_object_uses)

    # Export workers initially see only their own shard.  Recompute the
    # virtual-thread effect closure after the public re-export graph has
    # converged, otherwise a caller in another shard can retain the ordinary
    # function ABI while its leaf already exposes the resumable generator ABI.
    # Consume the AST sidecars produced by those same native workers: reparsing
    # here would introduce a second frontend owner and, for pcc1, risk crossing
    # back into a host/CPython-only path.
    if not ast_dir:
        raise pipeline_error(
            "parallel frontend may_park closure requires worker AST wire"
        )
    if annotate_vthread_effect_summaries is None:
        parsed_modules = []
        for index in range(len(module_names)):
            ast_path = os.path.join(ast_dir, "module_" + str(index) + ".json")
            parsed_modules.append(read_ast_wire(ast_path))
        annotate_vthread_effects(parsed_modules, module_names, native_exports)
    else:
        pre_effect_exports_path = os.path.join(
            tmp,
            "native_exports_pre_effect.json",
        )
        worker_exports = (
            vthread_effect_export_surface(native_exports)
            if vthread_effect_export_surface is not None
            else native_exports
        )
        write_native_exports_wire(
            pre_effect_exports_path,
            worker_exports,
            {},
        )
        worker_exports = None
        summary_paths, summary_parallel = _build_vthread_effect_summaries(
            tmp,
            src_paths,
            module_names,
            worker_prefix,
            entry_module=entry_module,
            sibling_inits=sibling_inits,
            libpython_mode=libpython_mode,
            ir_scaffold_mode=ir_scaffold_mode,
            verbose=verbose,
            max_parallel=max_parallel,
            ast_dir=ast_dir,
            exports_path=pre_effect_exports_path,
            write_manifest=write_manifest,
            shell_quote_arg=shell_quote_arg,
            worker_arg=worker_arg,
            worker_env_prefix=worker_env_prefix,
            join_strings=join_strings,
            run_worker_commands=run_worker_commands,
            pipeline_error=pipeline_error,
        )
        summary_count, node_count, edge_count = (
            annotate_vthread_effect_summaries(summary_paths, native_exports)
        )
        profile_counter(
            profile,
            "multi_frontend_summary_worker_jobs",
            len(summary_paths),
        )
        profile_counter(
            profile,
            "multi_frontend_summary_worker_parallel",
            summary_parallel,
        )
        profile_counter(profile, "multi_frontend_summary_count", summary_count)
        profile_counter(profile, "multi_frontend_summary_nodes", node_count)
        profile_counter(profile, "multi_frontend_summary_edges", edge_count)

    # The final indexed export publication must coexist with the merged export
    # graph, but it no longer needs worker command/result paths, re-export
    # edges, function-object use rows, or summary paths.  Under pcc1, leaving
    # those dead containers for the next debt-triggered collection forced the
    # preload index to acquire fresh allocator slabs and left the coordinator
    # within kilobytes of the 8 GiB process-tree breaker.  Collect once at the
    # phase boundary so the index reuses those slots; native_exports remains
    # live and unchanged.
    commands = []
    result_paths = []
    reexport_edges = []
    function_object_uses = []
    if annotate_vthread_effect_summaries is not None:
        summary_paths = []
    profiled_gc_collect(
        profile,
        "multi_frontend_pre_index_collect",
        allocations_owned_by_current_process=True,
    )

    class_map = derived_class_map(native_exports)
    exports_path = os.path.join(tmp, "native_exports.json")
    unique_class_preload_index = None
    contextual_modules = []
    contextual_host_exports = None
    if module_dependency_map is not None:
        if len(worker_module_dependencies) != len(module_names):
            raise pipeline_error(
                "frontend module dependency rows are incomplete"
            )
        module_dependency_map.clear()
        for module_name in module_names:
            dependencies = worker_module_dependencies.get(module_name)
            if not isinstance(dependencies, tuple):
                raise pipeline_error(
                    "frontend module dependency row is missing"
                )
            module_dependency_map[module_name] = dependencies
        if build_unique_class_preload_index is None:
            raise pipeline_error(
                "indexed native exports require a class preload builder"
            )
        preload_started = time.monotonic()
        unique_class_preload_index = build_unique_class_preload_index(
            native_exports, tmp
        )
        profile_counter(
            profile,
            "multi_frontend_unique_class_preload_ms",
            int((time.monotonic() - preload_started) * 1000),
        )
        if (
            contextual_host_for_module is None
            or build_contextual_host_export_surface is None
        ):
            raise pipeline_error(
                "indexed native exports require contextual host builders"
            )
        for module_name in module_names:
            if contextual_host_for_module(module_name):
                contextual_modules.append(module_name)
        if contextual_modules:
            contextual_host_exports = build_contextual_host_export_surface(
                native_exports
            )
        else:
            contextual_host_exports = {}
    write_native_exports_wire(
        exports_path,
        native_exports,
        class_map,
        module_dependencies=module_dependency_map,
        unique_class_preload_index=unique_class_preload_index,
        contextual_modules=tuple(contextual_modules),
        contextual_host_exports=contextual_host_exports,
    )
    profile_counter(
        profile,
        "multi_frontend_export_worker_sum_ms",
        export_worker_sum_ms,
    )
    profile_counter(
        profile,
        "multi_frontend_export_worker_max_ms",
        export_worker_max_ms,
    )
    return exports_path



def _codegen_safe_worker_jobs(jobs: int, native_owned_lanes: bool,
                              safe_chunk_count: int, deferred_plan: str) -> int:
    if _worker_tree_budget_bytes(os.environ.get(_WORKER_TREE_BUDGET_ENV, "")) > 0:
        return max(1, jobs)
    if not native_owned_lanes or safe_chunk_count == 0:
        return jobs
    if deferred_plan:
        # Nominal ceiling only. The publishing coordinator exits before work;
        # the execution driver applies its own memory admission at launch.
        return min(max(1, jobs), _SOURCE_WORKER_AUTO_SAFE_JOBS)
    return _compiled_native_auto_jobs(jobs)


def _prepare_stage1_checkpoint_graph(
    root, build_digest, src_paths, module_names, chunks, exports_path,
    ast_dir, entry_module, sibling_inits, libpython_mode, ir_scaffold_mode,
    worker_prefix, jobs,
):
    """Bind the newly generated full graph before admitting any old object."""
    from pcc.frontends.python import pipeline_stage1_checkpoint as checkpoint
    from pcc.frontends.python.pipeline_frontend_worker_execution import (
        _direct_owned_pass_names,
    )
    from pcc.frontends.python.pipeline_targets import host_target_triple

    sources = []
    for index, module_name in enumerate(module_names):
        source_path = os.path.abspath(src_paths[index])
        ast_path = os.path.join(ast_dir, "module_" + str(index) + ".json")
        sources.append({
            "index": index,
            "module_name": module_name,
            "source_path": source_path,
            "source_sha256": checkpoint.file_sha256(source_path),
            "ast_sha256": checkpoint.file_sha256(ast_path),
            "ast_size": os.path.getsize(ast_path),
            "passes": _direct_owned_pass_names(module_name),
        })
    runtime_path = str(os.environ.get("PCC_RUNTIME_ARCHIVE", "") or "")
    if not runtime_path:
        raise checkpoint.CheckpointError("Stage1 checkpoint runtime is missing")
    graph = {
        "stage": 1,
        "producer_role": "host-pcc0",
        "owner": "cpython",
        "backend": "self",
        "target": host_target_triple(),
        "module_names": list(module_names),
        "original_chunks": [list(chunk) for chunk in chunks],
        "sources": sources,
        "entry_module": entry_module,
        "sibling_inits": list(sibling_inits),
        "libpython_mode": libpython_mode,
        "ir_scaffold_mode": ir_scaffold_mode,
        "worker_prefix": list(worker_prefix),
        "jobs": jobs,
        "exports_sha256": checkpoint.file_sha256(exports_path),
        "exports_size": os.path.getsize(exports_path),
        "runtime_path": os.path.abspath(runtime_path),
        "runtime_sha256": checkpoint.file_sha256(runtime_path),
        "worker_schema": "pcc.frontends.python.codegen_worker.v5",
        "ast_schema": "pcc.frontends.python.py_ast.v1",
        "export_reader_by_chunk": [
            "module_indexed" if len(chunk) == 1 else "full_graph"
            for chunk in chunks
        ],
    }
    return checkpoint.prepare_graph(root, build_digest, graph)


def compile_parallel(
    src_paths,
    module_names,
    *,
    jobs: int,
    entry_module: str,
    sibling_inits,
    libpython_mode: str,
    ir_scaffold_mode: str,
    verbose: bool,
    profile: Optional[dict] = None,
    artifact_dir: str = "",
    coordinator_timing=None,
    can_spawn_worker,
    worker_command_prefix,
    compile_uncached,
    plan_cache,
    load_cache,
    acquire_cache,
    wait_cache,
    publish_cache,
    release_cache,
    host_python_command,
    source_root,
    profile_begin,
    profile_end,
    profile_counter,
):
    """Run parallel frontend codegen with a pre-pass content cache."""
    direct_assembly = str(
        os.environ.get("PCC_DIRECT_INDEXED_KERNEL_EMIT", "") or ""
    ).strip().lower() in ("1", "true", "yes", "on")
    if direct_assembly:
        if coordinator_timing is None:
            return compile_uncached(
                src_paths,
                module_names,
                jobs=jobs,
                entry_module=entry_module,
                sibling_inits=sibling_inits,
                libpython_mode=libpython_mode,
                ir_scaffold_mode=ir_scaffold_mode,
                verbose=verbose,
                profile=profile,
                action_cache_plan=None,
                artifact_dir=artifact_dir,
            )
        else:
            return compile_uncached(
                src_paths,
                module_names,
                jobs=jobs,
                entry_module=entry_module,
                sibling_inits=sibling_inits,
                libpython_mode=libpython_mode,
                ir_scaffold_mode=ir_scaffold_mode,
                verbose=verbose,
                profile=profile,
                action_cache_plan=None,
                artifact_dir=artifact_dir,
                coordinator_timing=coordinator_timing,
            )
    if jobs < 1 or not can_spawn_worker():
        return compile_uncached(
            src_paths,
            module_names,
            jobs=jobs,
            entry_module=entry_module,
            sibling_inits=sibling_inits,
            libpython_mode=libpython_mode,
            ir_scaffold_mode=ir_scaffold_mode,
            verbose=verbose,
            profile=profile,
            artifact_dir=artifact_dir,
        )
    worker_prefix = worker_command_prefix()
    if not worker_prefix:
        return compile_uncached(
            src_paths,
            module_names,
            jobs=jobs,
            entry_module=entry_module,
            sibling_inits=sibling_inits,
            libpython_mode=libpython_mode,
            ir_scaffold_mode=ir_scaffold_mode,
            verbose=verbose,
            profile=profile,
            artifact_dir=artifact_dir,
        )

    cache_started = profile_begin(profile)
    cache_plan = plan_cache(
        src_paths,
        module_names,
        compiler_executable=str(worker_prefix[0]),
        host_python=host_python_command(),
        entry_module=entry_module,
        sibling_inits=sibling_inits,
        libpython_mode=libpython_mode,
        ir_scaffold_mode=ir_scaffold_mode,
        source_root=source_root(),
    )
    cached_result = load_cache(cache_plan, module_names)
    profile_end(profile, "multi_frontend_ir_cache_lookup", cache_started)
    if cached_result is not None:
        profile_counter(profile, "multi_frontend_ir_cache_hits", 1)
        profile_counter(profile, "multi_frontend_ir_cache_misses", 0)
        return cached_result

    cache_owner = acquire_cache(cache_plan)
    if cache_plan is not None and not cache_owner:
        wait_started = profile_begin(profile)
        cached_result = wait_cache(cache_plan, module_names)
        profile_end(profile, "multi_frontend_ir_cache_wait", wait_started)
        if cached_result is not None:
            profile_counter(profile, "multi_frontend_ir_cache_hits", 1)
            profile_counter(profile, "multi_frontend_ir_cache_misses", 0)
            return cached_result

    profile_counter(profile, "multi_frontend_ir_cache_hits", 0)
    profile_counter(
        profile,
        "multi_frontend_ir_cache_misses",
        1 if cache_plan is not None else 0,
    )
    try:
        result = compile_uncached(
            src_paths,
            module_names,
            jobs=jobs,
            entry_module=entry_module,
            sibling_inits=sibling_inits,
            libpython_mode=libpython_mode,
            ir_scaffold_mode=ir_scaffold_mode,
            verbose=verbose,
            profile=profile,
            action_cache_plan=cache_plan,
            artifact_dir=artifact_dir,
        )
        if cache_owner and result is not None:
            publish_started = profile_begin(profile)
            published = publish_cache(cache_plan, result)
            profile_end(
                profile,
                "multi_frontend_ir_cache_publish",
                publish_started,
            )
            profile_counter(
                profile,
                "multi_frontend_ir_cache_publish_ok",
                1 if published else 0,
            )
        return result
    finally:
        if cache_owner:
            release_cache(cache_plan)


def compile_parallel_uncached(
    src_paths,
    module_names,
    *,
    jobs: int,
    entry_module: str,
    sibling_inits,
    libpython_mode: str,
    ir_scaffold_mode: str,
    verbose: bool,
    profile: Optional[dict] = None,
    action_cache_plan=None,
    artifact_dir: str = "",
    coordinator_timing=None,
    can_spawn_worker,
    worker_command_prefix,
    chunk_count_for_workers,
    codegen_chunks,
    ast_wire_enabled,
    build_shared_exports_callback,
    write_manifest,
    shell_quote_arg,
    worker_arg: str,
    worker_env_prefix,
    join_strings,
    run_worker_commands,
    profiled_gc_collect,
    read_worker_ir,
    profile_begin,
    profile_end,
    profile_counter,
    pipeline_error,
    build_action_state=None,
    module_dependency_map=None,
    auto_source_lanes: bool = True,
    run_worker_manifest_in_process=None,
):
    checkpoint_root = str(
        os.environ.get("PCC_STAGE1_CHECKPOINT_DIR", "") or ""
    )
    checkpoint_build_digest = str(
        os.environ.get("PCC_STAGE1_CHECKPOINT_BUILD", "") or ""
    )
    checkpoint_graph_digest = ""
    if jobs < 1 or not can_spawn_worker():
        if checkpoint_root:
            raise pipeline_error("Stage1 checkpoint requires source workers")
        return None
    worker_prefix = worker_command_prefix()
    if not worker_prefix:
        if checkpoint_root:
            raise pipeline_error("Stage1 checkpoint requires a worker interpreter")
        return None
    if checkpoint_root and action_cache_plan is not None:
        raise pipeline_error("Stage1 checkpoint cannot share the frontend action cache")
    noop_result = _load_noop_action_result(
        action_cache_plan,
        src_paths,
        module_names,
    )
    if noop_result is not None:
        profile_counter(profile, "multi_frontend_action_cache_hits", len(src_paths))
        profile_counter(profile, "multi_frontend_action_cache_misses", 0)
        profile_counter(profile, "multi_frontend_action_modules_compiled", 0)
        profile_counter(profile, "multi_frontend_action_plan_actions", 0)
        return noop_result
    compiled_native_worker = len(worker_prefix) == 1
    worker_base = ""
    if compiled_native_worker:
        worker_base = os.path.basename(str(worker_prefix[0])).lower()
        if worker_base.startswith("python") or worker_base.endswith(".py"):
            compiled_native_worker = False
    # A numeric worker count limits concurrency; it must not turn off the
    # native export/codegen ownership and memory-safe deferred pipeline.
    native_owned_lanes = compiled_native_worker
    host_indexed_split = False
    if not native_owned_lanes and str(
        os.environ.get("PCC_HOST_INDEXED_PROCESS_SPLIT", "") or ""
    ).strip().lower() in ("1", "true", "yes", "on"):
        from pcc.frontends.python.pipeline_frontend_indexed_stage import require_host_indexed_split

        require_host_indexed_split(
            os.environ, checkpoint_root=checkpoint_root,
            action_cache_plan=action_cache_plan, libpython_mode=libpython_mode,
            artifact_dir=artifact_dir,
        )
        host_indexed_split = True
    from pcc.frontends.python.pipeline_frontend_host_batch import (
        bounded_host_codegen_batches, host_codegen_batch_limit,
        validate_host_batch_result,
    )

    host_batch_limit = host_codegen_batch_limit(
        os.environ, native_worker=native_owned_lanes, indexed_split=host_indexed_split,
        checkpoint_root=checkpoint_root, action_cache_plan=action_cache_plan,
        libpython_mode=libpython_mode, artifact_dir=artifact_dir,
    )
    if checkpoint_root:
        if (
            native_owned_lanes
            or os.environ.get("PCC_STAGE1_CHECKPOINT_STAGE") != "1"
            or not checkpoint_build_digest
            or not artifact_dir
            or libpython_mode != "off"
            or action_cache_plan is not None
            or str(os.environ.get("PCC_DIRECT_INDEXED_KERNEL_EMIT", ""))
            .strip().lower() not in ("1", "true", "yes", "on")
            or str(os.environ.get("PCC_DIRECT_INDEXED_NATIVE_OBJECT", "1"))
            .strip().lower() in ("0", "false", "no", "off")
            or str(os.environ.get("PCC_DIRECT_INDEXED_KERNEL_RELEASE_FRONTEND", ""))
            .strip().lower() not in ("1", "true", "yes", "on")
        ):
            raise pipeline_error("unsupported Stage1 checkpoint worker route")
        for name in (
            "PCC_DIRECT_INDEXED_KERNEL_VALIDATE", "PCC_TEXT_INDEXED_KERNEL_EMIT",
            "PCC_DIRECT_INDEXED_SIDECAR",
        ):
            if str(os.environ.get(name, "")).strip().lower() in ("1", "true", "yes", "on"):
                raise pipeline_error("unsupported Stage1 checkpoint route: " + name)
    native_auto_source_lanes = auto_source_lanes and compiled_native_worker
    chunk_count = chunk_count_for_workers(len(src_paths), jobs, worker_prefix)
    chunks = codegen_chunks(src_paths, chunk_count)
    if host_indexed_split and any(len(chunk) != 1 for chunk in chunks):
        raise pipeline_error("host indexed process split requires singleton codegen chunks")
    # Export startup is amortized over a fixed-size batch, independently of
    # admission width. Codegen remains singleton unless the restricted host
    # batch mode explicitly pairs two assignments after AST export completes.
    from pcc.frontends.python.pipeline_frontend_workers import SOURCE_WORKER_MODULES_PER_CHUNK

    export_chunks = [
        list(range(index, min(index + SOURCE_WORKER_MODULES_PER_CHUNK, len(src_paths))))
        for index in range(0, len(src_paths), SOURCE_WORKER_MODULES_PER_CHUNK)
    ]
    export_oversized_chunk_count = 0
    export_safe_jobs = jobs
    if native_owned_lanes:
        export_chunks = [[index] for index in range(len(src_paths))]
        export_oversized, export_safe = (
            _split_codegen_chunks_by_source_size(src_paths, export_chunks)
        )
        export_chunks = export_oversized + export_safe
        export_oversized_chunk_count = len(export_oversized)
        # Light one-module export workers have their own memory class. Auto
        # mode can use the CPU budget; an explicit numeric width remains a cap.
        export_requested = jobs
        if native_auto_source_lanes:
            export_requested = max(jobs, _parallel_cpu_budget())
        export_safe_jobs = (
            export_requested if _worker_tree_budget_bytes(
                os.environ.get(_WORKER_TREE_BUDGET_ENV, "")
            ) > 0 else _compiled_native_export_jobs(export_requested)
        )
        if _worker_tree_budget_bytes(
            os.environ.get(_WORKER_TREE_BUDGET_ENV, "")
        ) > 0:
            # The source-size "oversized" band is a codegen memory class; as
            # export workers those modules peaked at 310 MB.  Keep them first
            # in the same budgeted pool instead of a serial prefix.
            export_oversized_chunk_count = 0
    if not chunks or not export_chunks:
        return None
    profile_counter(profile, "multi_frontend_chunks", len(chunks))
    profile_counter(profile, "multi_frontend_export_chunks", len(export_chunks))
    profile_counter(
        profile,
        "multi_frontend_export_oversized_chunks",
        export_oversized_chunk_count,
    )
    profile_counter(
        profile,
        "multi_frontend_export_safe_chunks",
        len(export_chunks) - export_oversized_chunk_count,
    )
    profile_counter(
        profile,
        "multi_frontend_export_safe_jobs",
        export_safe_jobs,
    )
    profile_counter(profile, "multi_frontend_worker_requested_concurrency", jobs)
    profile_counter(
        profile,
        "multi_frontend_worker_concurrency",
        export_safe_jobs if native_owned_lanes else jobs,
    )
    with tempfile.TemporaryDirectory(prefix="pcc_frontends_python_workers_") as tmp:
        ir_dir = artifact_dir if artifact_dir else os.path.join(tmp, "ir")
        if host_indexed_split:
            ir_dir = os.path.abspath(ir_dir)
        os.makedirs(ir_dir, exist_ok=True)
        # The merged may_park fixed point is a semantic prerequisite for
        # parallel codegen, so every export worker must publish its lifted AST.
        # The same sidecars are then reused by codegen workers; no parent/host
        # reparse owns a second copy of the frontend state.  Keep recording the
        # old opt-in signal separately while making the correctness transport
        # unconditional.
        ast_wire_requested = ast_wire_enabled()
        ast_dir = os.path.join(tmp, "ast")
        os.makedirs(ast_dir, exist_ok=True)
        profile_counter(profile, "multi_frontend_ast_wire_enabled", 1)
        profile_counter(
            profile,
            "multi_frontend_ast_wire_requested",
            1 if ast_wire_requested else 0,
        )
        started = profile_begin(profile)
        exports_path = build_shared_exports_callback(
            tmp,
            src_paths,
            module_names,
            export_chunks,
            worker_prefix,
            entry_module=entry_module,
            sibling_inits=sibling_inits,
            libpython_mode=libpython_mode,
            ir_scaffold_mode=ir_scaffold_mode,
            verbose=verbose,
            max_parallel=jobs,
            oversized_chunk_count=export_oversized_chunk_count,
            safe_parallel=export_safe_jobs,
            ast_dir=ast_dir,
            profile=profile,
            module_dependency_map=module_dependency_map,
        )
        profile_end(profile, "multi_frontend_export_parallel", started)

        action_state = None
        action_root = ""
        action_plan = None
        module_ir_by_index: list[Optional[tuple[str, str]]] = [
            None for _source in src_paths
        ]
        module_meta_by_index: list[Optional[tuple[bool, bool, int]]] = [
            None for _source in src_paths
        ]
        module_asm_by_index: list[Optional[tuple[str, str]]] = [
            None for _source in src_paths
        ]
        module_native_object_by_index: list[Optional[tuple[str, str]]] = [
            None for _source in src_paths
        ]
        action_hits = 0
        if action_cache_plan is not None and build_action_state is not None:
            try:
                if module_dependency_map is not None:
                    action_state = build_action_state(
                        src_paths,
                        module_names,
                        exports_path,
                        action_cache_plan,
                        module_dependency_map,
                    )
                else:
                    action_state = build_action_state(
                        src_paths,
                        module_names,
                        exports_path,
                        action_cache_plan,
                    )
                action_root = str(action_cache_plan.get("action_root", ""))
                previous_state = _module_action_dag.load_graph_state_file(
                    action_root
                )
                if action_state is not None and previous_state is not None:
                    action_plan = _module_action_dag.plan_actions(
                        previous_state,
                        action_state,
                    )
                    if not action_plan.full_rebuild:
                        for index, module_name in enumerate(module_names):
                            action = _module_ir_action(action_state, module_name)
                            artifact = _module_action_dag.load_action_artifact(
                                action_root,
                                action,
                            )
                            if artifact is None:
                                continue
                            decoded = _decode_module_ir_artifact(
                                artifact,
                                module_name,
                            )
                            if decoded is None:
                                continue
                            ir_text, needs_libpython, needs_native, ir_size = decoded
                            module_ir_by_index[index] = (module_name, ir_text)
                            module_meta_by_index[index] = (
                                needs_libpython,
                                needs_native,
                                ir_size,
                            )
                            action_hits += 1
            except Exception:
                # The action cache is an optimization boundary.  Unknown or
                # corrupt graph/export state falls back to the complete,
                # already-correct worker path and is never partially trusted.
                action_state = None
                action_root = ""
                action_plan = None
                module_ir_by_index = [None for _source in src_paths]
                module_meta_by_index = [None for _source in src_paths]
                module_asm_by_index = [None for _source in src_paths]
                module_native_object_by_index = [None for _source in src_paths]
                action_hits = 0

        pending_indices = []
        for index, item in enumerate(module_ir_by_index):
            if item is None:
                pending_indices.append(index)
        pending_set = set(pending_indices)
        pending_chunks = []
        for chunk in chunks:
            pending_chunk = []
            for index in chunk:
                if index in pending_set:
                    pending_chunk.append(index)
            if pending_chunk:
                pending_chunks.append(pending_chunk)
        oversized_chunks = []
        safe_chunks = pending_chunks
        # The oversized split and the safe-lane memory clamp are the compiled
        # pcc1 worker contract (measured multi-GiB per-worker peaks).  Host
        # CPython workers are memory-cheap and keep the full chunked width;
        # their budget is already applied by frontend_jobs.  Mirroring the
        # export lane's native-only predicate here is what keeps the Stage2
        # memory policy from throttling host Stage1.
        if native_owned_lanes:
            oversized_chunks, safe_chunks = (
                _split_codegen_chunks_by_source_size(
                    src_paths,
                    pending_chunks,
                    sidecar_dir=ast_dir,
                )
            )
        if host_batch_limit == 2:
            safe_chunks = bounded_host_codegen_batches(src_paths, ast_dir, safe_chunks)
        scheduled_chunks = oversized_chunks + safe_chunks
        checkpoint_skip_masks = []
        if checkpoint_root:
            from pcc.frontends.python import pipeline_stage1_checkpoint as checkpoint

            # An action-cache hit must never filter these assignments: the
            # original chunk length selects the worker's export reader.
            if scheduled_chunks != chunks:
                raise pipeline_error("Stage1 checkpoint assignments changed")
            checkpoint_graph_digest = _prepare_stage1_checkpoint_graph(
                checkpoint_root, checkpoint_build_digest, src_paths,
                module_names, chunks, exports_path, ast_dir, entry_module,
                sibling_inits, libpython_mode, ir_scaffold_mode,
                worker_prefix, jobs,
            )
            for chunk in scheduled_chunks:
                skip_mask = []
                for index in chunk:
                    record = checkpoint.load_module(
                        checkpoint_root, checkpoint_build_digest,
                        checkpoint_graph_digest, index, module_names[index],
                        chunk, ir_dir,
                    )
                    if record is not None:
                        skip_mask.append(index)
                checkpoint_skip_masks.append(skip_mask)
        oversized_chunk_count = len(oversized_chunks)
        safe_jobs = jobs
        deferred_codegen_plan = str(
            os.environ.get("PCC_DEFER_FRONTEND_CODEGEN_PLAN", "") or ""
        ).strip()
        safe_jobs = _codegen_safe_worker_jobs(
            safe_jobs, native_owned_lanes, len(safe_chunks), deferred_codegen_plan,
        )
        profile_counter(
            profile,
            "multi_frontend_codegen_oversized_chunks",
            oversized_chunk_count,
        )
        profile_counter(
            profile,
            "multi_frontend_codegen_safe_chunks",
            len(safe_chunks),
        )
        profile_counter(
            profile,
            "multi_frontend_codegen_oversized_jobs",
            1 if oversized_chunks else 0,
        )
        profile_counter(profile, "multi_frontend_codegen_safe_jobs", safe_jobs)
        profile_counter(profile, "multi_frontend_action_cache_hits", action_hits)
        profile_counter(
            profile,
            "multi_frontend_action_cache_misses",
            len(pending_indices),
        )
        profile_counter(
            profile,
            "multi_frontend_action_modules_compiled",
            len(pending_indices),
        )
        profile_counter(
            profile,
            "multi_frontend_action_plan_actions",
            len(action_plan.actions) if action_plan is not None else len(src_paths) * 5,
        )
        result_paths: list[str] = []
        manifest_paths: list[str] = []
        commands: list[str] = []
        oversized_assembly_handoff = (
            native_owned_lanes
            and oversized_chunk_count > 0
            and str(
                os.environ.get("PCC_DIRECT_INDEXED_KERNEL_EMIT", "") or ""
            ).strip().lower()
            in ("1", "true", "yes", "on")
            and str(
                os.environ.get("PCC_DIRECT_INDEXED_NATIVE_OBJECT", "1") or "1"
            ).strip().lower()
            not in ("0", "false", "no", "off")
        )
        profile_counter(
            profile,
            "multi_frontend_codegen_oversized_assembly_handoff",
            oversized_chunk_count if oversized_assembly_handoff else 0,
        )
        for worker_index, chunk in enumerate(scheduled_chunks):
            manifest_path = os.path.join(
                tmp,
                "worker_" + str(worker_index) + ".manifest",
            )
            result_path = os.path.join(
                tmp,
                "worker_" + str(worker_index) + ".tsv",
            )
            write_manifest(
                manifest_path,
                result_path,
                ir_dir,
                exports_path,
                ast_dir,
                src_paths,
                module_names,
                chunk,
                entry_module=entry_module,
                sibling_inits=sibling_inits,
                libpython_mode=libpython_mode,
                ir_scaffold_mode=ir_scaffold_mode,
                verbose=verbose,
                checkpoint_root=checkpoint_root,
                checkpoint_build_digest=checkpoint_build_digest,
                checkpoint_graph_digest=checkpoint_graph_digest,
                checkpoint_skip_indices=(
                    checkpoint_skip_masks[worker_index]
                    if checkpoint_root else ()
                ),
            )
            result_paths.append(result_path)
            manifest_paths.append(manifest_path)
            command_parts = [shell_quote_arg(part) for part in worker_prefix]
            command_parts.append(shell_quote_arg(worker_arg))
            command_parts.append(shell_quote_arg(manifest_path))
            command_environment = worker_env_prefix()
            if host_indexed_split:
                command_environment += " PCC_DIRECT_INDEXED_SIDECAR=1"
            if oversized_assembly_handoff and worker_index < oversized_chunk_count:
                # The frontend, indexed emitter and assembler have different
                # high-water shapes.  Keep the direct indexed emitter here,
                # but let process exit reclaim its allocator slabs before a
                # short-lived linker worker assembles the oversized text.
                command_environment += " PCC_DIRECT_INDEXED_NATIVE_OBJECT=0"
            commands.append(
                command_environment + " " + join_strings(command_parts, " ")
            )

        if deferred_codegen_plan and native_owned_lanes:
            output_path = str(
                os.environ.get("PCC_DEFER_FRONTEND_OUTPUT", "") or ""
            ).strip()
            runtime_archive = str(
                os.environ.get("PCC_RUNTIME_ARCHIVE", "") or ""
            ).strip()
            if (
                len(worker_prefix) != 1
                or not output_path
                or not runtime_archive
                or not artifact_dir
            ):
                raise pipeline_error(
                    "deferred frontend codegen requires native worker, output, "
                    "runtime and artifact roots"
                )
            state_root = (
                os.path.abspath(deferred_codegen_plan)
                + ".state."
                + str(os.getpid())
            )
            persistent_ast = os.path.join(state_root, "ast")
            persistent_results = os.path.join(state_root, "results")
            persistent_manifests = os.path.join(state_root, "manifests")
            os.makedirs(persistent_results, exist_ok=True)
            os.makedirs(persistent_manifests, exist_ok=True)
            _copy_frontend_artifact_tree(ast_dir, persistent_ast)
            persistent_exports = os.path.join(state_root, "native_exports.json")
            _copy_frontend_artifact_file(exports_path, persistent_exports)
            persistent_manifest_paths = []
            for worker_index, chunk in enumerate(scheduled_chunks):
                if len(chunk) != 1:
                    raise pipeline_error(
                        "deferred native codegen requires singleton manifests"
                    )
                persistent_manifest = os.path.join(
                    persistent_manifests,
                    "worker_" + str(worker_index) + ".manifest",
                )
                persistent_result = os.path.join(
                    persistent_results,
                    "worker_" + str(worker_index) + ".tsv",
                )
                write_manifest(
                    persistent_manifest,
                    persistent_result,
                    artifact_dir,
                    persistent_exports,
                    persistent_ast,
                    src_paths,
                    module_names,
                    chunk,
                    entry_module=entry_module,
                    sibling_inits=sibling_inits,
                    libpython_mode=libpython_mode,
                    ir_scaffold_mode=ir_scaffold_mode,
                    verbose=verbose,
                )
                persistent_manifest_paths.append(persistent_manifest)
            _write_deferred_codegen_plan(
                os.path.abspath(deferred_codegen_plan),
                worker_executable=str(worker_prefix[0]),
                output_path=output_path,
                runtime_archive=runtime_archive,
                artifact_root=artifact_dir,
                module_count=len(module_names),
                oversized_count=oversized_chunk_count,
                safe_jobs=safe_jobs,
                manifests=persistent_manifest_paths,
            )
            profile_counter(profile, "multi_frontend_codegen_deferred", 1)
            return (_DEFERRED_CODEGEN_RESULT,)

        try:
            started = profile_begin(profile)
            in_process_codegen = (
                native_owned_lanes
                and run_worker_manifest_in_process is not None
                and str(
                    os.environ.get(
                        "PCC_PY_FRONTEND_IN_PROCESS_CODEGEN", ""
                    )
                    or ""
                ).strip().lower()
                in ("1", "true", "yes", "on")
            )
            profile_counter(
                profile,
                "multi_frontend_codegen_in_process",
                1 if in_process_codegen else 0,
            )
            if host_indexed_split:
                from pcc.frontends.python.pipeline_frontend_indexed_stage import run_host_indexed_stage

                run_host_indexed_stage(
                    commands, manifest_paths, result_paths, scheduled_chunks,
                    module_names, ir_dir, worker_prefix, safe_jobs,
                    worker_env_prefix(), shell_quote_arg,
                )
            elif in_process_codegen:
                old_native_object = os.environ.get(
                    "PCC_DIRECT_INDEXED_NATIVE_OBJECT"
                )
                old_frontend_jobs = os.environ.get("PCC_PY_FRONTEND_JOBS")
                os.environ["PCC_DIRECT_INDEXED_NATIVE_OBJECT"] = "0"
                os.environ["PCC_PY_FRONTEND_JOBS"] = "1"
                try:
                    for manifest_path in manifest_paths:
                        result_code = run_worker_manifest_in_process(
                            manifest_path
                        )
                        if int(result_code) != 0:
                            raise pipeline_error(
                                "in-process frontend codegen worker failed"
                            )
                finally:
                    if old_native_object is None:
                        os.environ.pop("PCC_DIRECT_INDEXED_NATIVE_OBJECT", None)
                    else:
                        os.environ[
                            "PCC_DIRECT_INDEXED_NATIVE_OBJECT"
                        ] = old_native_object
                    if old_frontend_jobs is None:
                        os.environ.pop("PCC_PY_FRONTEND_JOBS", None)
                    else:
                        os.environ["PCC_PY_FRONTEND_JOBS"] = old_frontend_jobs
            elif oversized_chunk_count:
                oversized_started = profile_begin(profile)
                run_worker_commands(
                    commands[:oversized_chunk_count],
                    max_parallel=1,
                )
                profile_end(
                    profile,
                    "multi_frontend_codegen_oversized_workers",
                    oversized_started,
                )
            safe_commands = commands[oversized_chunk_count:]
            if safe_commands and not in_process_codegen and not host_indexed_split:
                safe_started = profile_begin(profile)
                run_worker_commands(safe_commands, max_parallel=safe_jobs)
                profile_end(
                    profile,
                    "multi_frontend_codegen_safe_workers",
                    safe_started,
                )
            profile_end(
                profile,
                "multi_frontend_codegen_worker_commands",
                started,
            )
            profiled_gc_collect(
                profile,
                "multi_frontend_codegen_worker_collect",
                allocations_owned_by_current_process=False,
            )
        except subprocess.CalledProcessError as exc:
            raise pipeline_error("parallel frontend codegen worker failed") from exc

        result_started = 0
        if coordinator_timing is not None:
            result_started = coordinator_timing.begin("result_read")
        started = profile_begin(profile)
        worker_parse_sum_ms = 0
        worker_parse_max_ms = 0
        worker_parse_max_index = -1
        worker_infer_sum_ms = 0
        worker_infer_max_ms = 0
        worker_infer_max_index = -1
        worker_codegen_sum_ms = 0
        worker_codegen_max_ms = 0
        worker_codegen_max_index = -1
        checkpoint_hits = 0
        for result_position, result_path in enumerate(result_paths):
            with open(result_path, "r", encoding="utf-8") as stream:
                result_text = stream.read()
            if host_batch_limit == 2:
                try:
                    validate_host_batch_result(
                        result_text, scheduled_chunks[result_position], module_names, ir_dir,
                    )
                except ValueError as exc:
                    raise pipeline_error(str(exc)) from exc
            for raw_line in result_text.splitlines():
                parts = raw_line.split("\t")
                if not parts:
                    continue
                if parts[0] == "ERR":
                    message = parts[1] if len(parts) > 1 else "worker error"
                    raise pipeline_error(message)
                if parts[0] != "OK" or len(parts) < 7:
                    raise pipeline_error("invalid frontend worker result")
                if "REUSED" in parts:
                    checkpoint_hits += 1
                index = int(parts[1])
                module_name = parts[2]
                if checkpoint_root and (
                    index < 0
                    or index >= len(module_names)
                    or module_name != module_names[index]
                    or module_ir_by_index[index] is not None
                    or parts[3] not in ("0", "1")
                    or parts[4] not in ("0", "1")
                ):
                    raise pipeline_error("invalid Stage1 checkpoint worker result binding")
                needs_libpython = parts[3] == "1"
                needs_native_exports = parts[4] == "1"
                try:
                    ir_bytes_before_passes = int(parts[5])
                except ValueError:
                    if checkpoint_root:
                        raise pipeline_error("invalid Stage1 checkpoint IR byte count")
                    ir_bytes_before_passes = 0
                if checkpoint_root and ir_bytes_before_passes < 0:
                    raise pipeline_error("negative Stage1 checkpoint IR byte count")
                assembly_path = ""
                native_object_path = ""
                marker_index = 7
                while marker_index + 1 < len(parts):
                    if parts[marker_index] == "ASM":
                        assembly_path = parts[marker_index + 1]
                        break
                    if parts[marker_index] == "PCO":
                        native_object_path = parts[marker_index + 1]
                        break
                    marker_index += 1
                ir_text = (
                    ""
                    if assembly_path or native_object_path
                    else read_worker_ir(parts[6], module_name)
                )
                module_ir_by_index[index] = (module_name, ir_text)
                module_meta_by_index[index] = (
                    needs_libpython,
                    needs_native_exports,
                    ir_bytes_before_passes,
                )
                if len(parts) >= 10:
                    try:
                        parse_ms = int(parts[7])
                        infer_ms = int(parts[8])
                        codegen_ms = int(parts[9])
                    except ValueError:
                        parse_ms = 0
                        infer_ms = 0
                        codegen_ms = 0
                    worker_parse_sum_ms += parse_ms
                    worker_infer_sum_ms += infer_ms
                    worker_codegen_sum_ms += codegen_ms
                    if parse_ms > worker_parse_max_ms:
                        worker_parse_max_ms = parse_ms
                        worker_parse_max_index = index
                    if infer_ms > worker_infer_max_ms:
                        worker_infer_max_ms = infer_ms
                        worker_infer_max_index = index
                    if codegen_ms > worker_codegen_max_ms:
                        worker_codegen_max_ms = codegen_ms
                        worker_codegen_max_index = index
                if assembly_path:
                    if not os.path.isfile(assembly_path):
                        raise pipeline_error(
                            "parallel direct frontend worker lost assembly "
                            + str(index)
                        )
                    module_asm_by_index[index] = (
                        module_name,
                        assembly_path,
                    )
                if native_object_path:
                    if not os.path.isfile(native_object_path):
                        raise pipeline_error(
                            "parallel direct frontend worker lost native object "
                            + str(index)
                        )
                    module_native_object_by_index[index] = (
                        module_name,
                        native_object_path,
                    )
        module_ir_texts = []
        for index, item in enumerate(module_ir_by_index):
            if item is None:
                raise pipeline_error(
                    "parallel frontend worker missed module " + str(index)
                )
            module_ir_texts.append(item)
            if module_meta_by_index[index] is None:
                raise pipeline_error(
                    "parallel frontend worker missed module metadata " + str(index)
                )

        module_assembly_paths = None
        module_native_object_paths = None
        module_direct_artifacts = None
        has_direct_assembly = any(
            item is not None for item in module_asm_by_index
        )
        has_direct_native_object = any(
            item is not None for item in module_native_object_by_index
        )
        if has_direct_assembly:
            module_assembly_paths = [
                item for item in module_asm_by_index if item is not None
            ]
        if has_direct_native_object:
            module_native_object_paths = [
                item
                for item in module_native_object_by_index
                if item is not None
            ]
        if has_direct_assembly or has_direct_native_object:
            module_direct_artifacts = []
            for index, module_name in enumerate(module_names):
                assembly_item = module_asm_by_index[index]
                native_item = module_native_object_by_index[index]
                if assembly_item is not None and native_item is not None:
                    raise pipeline_error(
                        "parallel direct frontend worker duplicated module artifact "
                        + str(index)
                    )
                if assembly_item is not None:
                    module_direct_artifacts.append(
                        (module_name, "ASM", assembly_item[1])
                    )
                elif native_item is not None:
                    module_direct_artifacts.append(
                        (module_name, "PCO", native_item[1])
                    )
                else:
                    raise pipeline_error(
                        "parallel direct frontend worker missed module artifact "
                        + str(index)
                    )

        if (
            module_assembly_paths is None
            and module_native_object_paths is None
            and action_state is not None
            and action_root
        ):
            all_artifacts_published = True
            for index in pending_indices:
                module_ir_item = module_ir_by_index[index]
                module_meta_item = module_meta_by_index[index]
                if module_ir_item is None or module_meta_item is None:
                    raise pipeline_error(
                        "parallel frontend action artifact lost module "
                        + str(index)
                    )
                module_name = module_ir_item[0]
                ir_text = module_ir_item[1]
                needs_libpython = module_meta_item[0]
                needs_native = module_meta_item[1]
                ir_size = module_meta_item[2]
                action = _module_ir_action(action_state, module_name)
                artifact = _encode_module_ir_artifact(
                    module_name,
                    ir_text,
                    needs_libpython,
                    needs_native,
                    ir_size,
                )
                if not _module_action_dag.publish_action_artifact(
                    action_root,
                    action,
                    artifact,
                ):
                    all_artifacts_published = False
            if all_artifacts_published:
                _module_action_dag.publish_graph_state_file(
                    action_root,
                    action_state,
                )

        any_needs_libpython = False
        any_needs_native_extension_exports = False
        total_ir_bytes_before_passes = 0
        libpython_modules: list[str] = []
        for index, module_name in enumerate(module_names):
            module_meta_item = module_meta_by_index[index]
            if module_meta_item is None:
                raise pipeline_error(
                    "parallel frontend worker lost module metadata " + str(index)
                )
            needs_libpython = module_meta_item[0]
            needs_native = module_meta_item[1]
            ir_size = module_meta_item[2]
            total_ir_bytes_before_passes += int(ir_size)
            if needs_libpython:
                any_needs_libpython = True
                libpython_modules.append(str(module_name))
            if needs_native:
                any_needs_native_extension_exports = True
        counters = (
            ("multi_frontend_worker_parse_sum_ms", worker_parse_sum_ms),
            ("multi_frontend_worker_parse_max_ms", worker_parse_max_ms),
            ("multi_frontend_worker_parse_max_index", worker_parse_max_index),
            ("multi_frontend_worker_infer_sum_ms", worker_infer_sum_ms),
            ("multi_frontend_worker_infer_max_ms", worker_infer_max_ms),
            ("multi_frontend_worker_infer_max_index", worker_infer_max_index),
            ("multi_frontend_worker_codegen_sum_ms", worker_codegen_sum_ms),
            ("multi_frontend_worker_codegen_max_ms", worker_codegen_max_ms),
            ("multi_frontend_worker_codegen_max_index", worker_codegen_max_index),
        )
        for name, value in counters:
            profile_counter(profile, name, value)
        if checkpoint_root:
            profile_counter(profile, "multi_frontend_checkpoint_hits", checkpoint_hits)
            profile_counter(
                profile, "multi_frontend_checkpoint_compiled",
                len(module_names) - checkpoint_hits,
            )
        profile_end(profile, "multi_frontend_codegen_result_read", started)
        result = (
            module_ir_texts,
            any_needs_libpython,
            any_needs_native_extension_exports,
            total_ir_bytes_before_passes,
            libpython_modules,
        )
        if coordinator_timing is not None:
            coordinator_timing.complete("result_read", result_started)
        if module_direct_artifacts is None:
            return result
        return result + (
            module_assembly_paths,
            module_native_object_paths,
            module_direct_artifacts,
        )
