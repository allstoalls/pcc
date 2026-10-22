"""Execution protocol for isolated multi-module frontend workers."""

from __future__ import annotations

import os
import sys
import time

from pcc.frontends.python.pipeline_closed_world import _closed_world_boxed_int_functions
from pcc.frontends.python.worker_resource_plan import publish_worker_resource


def _worker_failure(message: str) -> Exception:
    """Use a bootstrap-safe exception inside the isolated worker boundary."""
    return Exception(message)


def _concrete_direct_indexed_target(module, host_target: str):
    """Bind only a placeholder capture to the selected host ABI."""
    if module.triple != "unknown-unknown-unknown":
        return module
    if host_target == "unknown-unknown-unknown":
        raise _worker_failure("direct indexed module has no supported host target")
    from pcc.backend.self_backend_ir import ParsedModule

    return ParsedModule(
        host_target, module.globals_, module.functions, module.type_context
    )


def _direct_owned_pass_names(module_name: str) -> list[str]:
    """Select the same owned tier as ordinary self compilation."""
    from pcc.frontends.python.codegen.debug_info_lowering import debug_info_requested
    from pcc.frontends.python.compiled_owned_passes import owns_passes
    from pcc.frontends.python.pipeline_pass_config import python_ir_pass_should_skip_module, resolve_python_ir_pass_names

    if debug_info_requested() or python_ir_pass_should_skip_module(module_name):
        return []
    names = resolve_python_ir_pass_names(default_raw="default")
    if names and not owns_passes(names):
        raise _worker_failure("self optimizer does not own requested passes: " + ", ".join(names))
    return names


def _release_direct_frontend_state(codegen) -> None:
    """Release frontend-only graphs after the direct module is frozen."""
    frontend_module = codegen.module
    frontend_module._functions.clear()
    frontend_module._globals.clear()
    frontend_module.globals.clear()
    codegen.functions.clear()
    codegen.runtime.clear()
    codegen.env.clear()
    codegen._module_globals.clear()
    codegen._module_global_init_flags.clear()
    codegen._funcdef_functions.clear()
    codegen._native_symbol_funcdefs.clear()
    codegen._fn_err_exit_blocks.clear()
    codegen._direct_indexed_module = None
    # Function <-> Block and codegen <-> ClassLowering are intentional host
    # object cycles.  The direct backend owns only compact seed/kernel data;
    # collect the now-unreachable frontend graph before it competes for cache.
    import gc

    gc.collect()


def _freeze_worker_survivors() -> None:
    """Exclude what outlives the module loop from later collections.

    Exports for every module, the imported compiler and its caches survive
    each module; re-traversing them in each per-module ``gc.collect()`` was a
    quarter of a host worker's CPU.  Callers freeze only once no module-local
    graph is referenced.  pcc's runtime records the call without changing
    what it collects.
    """
    import gc

    gc.freeze()


def _validate_stage1_checkpoint_worker(manifest, native_worker_executable):
    """Check the original full worker context; a skip mask cannot replace it."""
    from pcc.frontends.python import pipeline_stage1_checkpoint as checkpoint
    from pcc.frontends.python.pipeline_targets import host_target_triple

    root = manifest["checkpoint_root"]
    build_digest = manifest["checkpoint_build_digest"]
    graph_digest = manifest["checkpoint_graph_digest"]
    if (
        native_worker_executable()
        or os.environ.get("PCC_STAGE1_CHECKPOINT_STAGE") != "1"
        or os.environ.get("PCC_STAGE1_CHECKPOINT_DIR") != root
        or os.environ.get("PCC_STAGE1_CHECKPOINT_BUILD") != build_digest
    ):
        raise _worker_failure("Stage1 checkpoint worker owner or identity differs")
    if str(os.environ.get("PCC_DIRECT_INDEXED_KERNEL_RELEASE_FRONTEND", "")).strip().lower() not in (
        "1", "true", "yes", "on",
    ):
        raise _worker_failure("Stage1 checkpoint requires frontend release before object validation")
    for name in (
        "PCC_DIRECT_INDEXED_KERNEL_VALIDATE", "PCC_TEXT_INDEXED_KERNEL_EMIT",
        "PCC_DIRECT_INDEXED_SIDECAR",
    ):
        if str(os.environ.get(name, "")).strip().lower() in ("1", "true", "yes", "on"):
            raise _worker_failure("unsupported Stage1 checkpoint route: " + name)
    graph = checkpoint.load_graph(root, build_digest, graph_digest)
    if (
        graph["target"] != host_target_triple()
        or graph["module_names"] != manifest["module_names"]
        or manifest["assigned_indices"] not in graph["original_chunks"]
        or graph["entry_module"] != manifest["entry_module"]
        or graph["sibling_inits"] != list(manifest["sibling_inits"])
        or graph["libpython_mode"] != manifest["libpython_mode"]
        or graph["ir_scaffold_mode"] != manifest["ir_scaffold_mode"]
        or graph["exports_sha256"] != checkpoint.file_sha256(manifest["exports_path"])
        or graph["runtime_path"] != os.path.abspath(str(os.environ.get("PCC_RUNTIME_ARCHIVE", "") or ""))
        or graph["runtime_sha256"] != checkpoint.file_sha256(graph["runtime_path"])
    ):
        raise _worker_failure("Stage1 checkpoint worker graph differs")
    sources = graph["sources"]
    if len(sources) != len(manifest["src_paths"]):
        raise _worker_failure("Stage1 checkpoint source count differs")
    for index, source in enumerate(sources):
        if (
            source["index"] != index
            or source["module_name"] != manifest["module_names"][index]
            or source["source_path"] != os.path.abspath(manifest["src_paths"][index])
            or source["source_sha256"] != checkpoint.file_sha256(source["source_path"])
        ):
            raise _worker_failure("Stage1 checkpoint source identity differs")
    return graph


def _validate_stage1_checkpoint_module_context(manifest, graph, index) -> None:
    from pcc.frontends.python import pipeline_stage1_checkpoint as checkpoint
    from pcc.ir.compat import ir

    # Named struct declarations otherwise survive an earlier module in the
    # shared Context and enter direct capture. Do not silently skip warming it.
    if ir.global_context.identified_types:
        raise _worker_failure(
            "Stage1 checkpoint cannot reuse a shared identified-type context"
        )
    source = graph["sources"][index]
    ast_path = os.path.join(manifest["ast_dir"], "module_" + str(index) + ".json")
    if (
        checkpoint.file_sha256(ast_path) != source["ast_sha256"]
        or checkpoint.file_sha256(source["source_path"]) != source["source_sha256"]
        or _direct_owned_pass_names(manifest["module_names"][index]) != source["passes"]
    ):
        raise _worker_failure("Stage1 checkpoint module inputs changed")


def _stage1_checkpoint_result_line(record, index, module_name, ir_dir, timing):
    ir_path = os.path.join(ir_dir, "module_" + str(index) + ".ll")
    with open(ir_path, "w", encoding="utf-8") as stream:
        stream.write("")
    line = (
        "OK\t" + str(index) + "\t" + module_name
        + "\t" + ("1" if record["needs_libpython"] else "0")
        + "\t" + ("1" if record["needs_native_extension_exports"] else "0")
        + "\t" + str(record["ir_bytes_before_passes"]) + "\t" + ir_path
    )
    if timing:
        # Historical costs stay in the receipt; they are not new attempt CPU.
        line += "\t0\t0\t0"
    return line + "\tPCO\t" + record["artifact_path"] + "\tREUSED\t1"


def run_export_worker(
    manifest,
    *,
    worker_timing_enabled,
    build_closed_world_context,
    write_ast_wire,
    closed_world_reexport_edges,
    closed_world_module_dependencies,
    mark_function_object_exports,
    write_native_exports_wire,
    write_reexport_edges_wire,
) -> int:
    import gc

    # Every lifted module stays alive until the worker exits, so automatic
    # collections only re-traversed a growing, acyclic heap: 43% of a host
    # worker's CPU, to reclaim a few hundred objects over 394 modules.
    collector_was_enabled = gc.isenabled()
    gc.disable()
    try:
        return _run_export_worker(
            manifest,
            worker_timing_enabled=worker_timing_enabled,
            build_closed_world_context=build_closed_world_context,
            write_ast_wire=write_ast_wire,
            closed_world_reexport_edges=closed_world_reexport_edges,
            closed_world_module_dependencies=closed_world_module_dependencies,
            mark_function_object_exports=mark_function_object_exports,
            write_native_exports_wire=write_native_exports_wire,
            write_reexport_edges_wire=write_reexport_edges_wire,
        )
    finally:
        if collector_was_enabled:
            gc.enable()


def _retain_stage2_export_result(module_name: str, result_path: str) -> None:
    """Preserve the actual EXPORT row after its ordinary result write."""
    try:
        directory = str(os.environ.get("PCC_STAGE2_EXPORT_EVIDENCE_DIR", "") or "")
        if (
            not directory
            or module_name != "pcc.frontends.c.parse.c_parsetab"
        ):
            return
        # Bind the copy to the exact manifest claimed by this coordinator.
        # Reading two header lines does not deserialize or shrink its module table.
        with open(os.path.join(directory, "selected.manifest"), "r", encoding="utf-8") as stream:
            stream.readline()
            selected_result = stream.readline().rstrip("\r\n")
        if selected_result != result_path:
            return
        destination = os.path.join(directory, "result.tsv")
        if os.path.exists(destination):
            return
        with open(result_path, "rb") as stream:
            payload: bytes = stream.read()
        with open(destination + ".pending", "wb") as stream:
            if stream.write(payload) != len(payload):
                return
            stream.flush()
        os.replace(destination + ".pending", destination)
    except Exception:
        # Compilation and its existing result/error protocol remain authoritative.
        return


def _run_export_worker(
    manifest,
    *,
    worker_timing_enabled,
    build_closed_world_context,
    write_ast_wire,
    closed_world_reexport_edges,
    closed_world_module_dependencies,
    mark_function_object_exports,
    write_native_exports_wire,
    write_reexport_edges_wire,
) -> int:
    worker_timing = worker_timing_enabled()
    total_started = time.monotonic() if worker_timing else 0.0
    result_path = str(manifest["result_path"])
    ir_dir = str(manifest["ir_dir"])
    ast_dir = str(manifest.get("ast_dir", "") or "")
    src_paths = manifest["src_paths"]
    module_names = manifest["module_names"]
    assigned_indices = manifest["assigned_indices"]
    subset_srcs = []
    subset_names = []
    for index in assigned_indices:
        subset_srcs.append(src_paths[index])
        subset_names.append(module_names[index])
    parsed_modules, native_exports, _derived_class_map = build_closed_world_context(
        subset_srcs,
        subset_names,
        profile=None,
        lift_indices=None,
        merge_exports=False,
        allow_local_int_abi_proofs=len(module_names) == 1,
    )
    if ast_dir:
        for local_index, ast_module in enumerate(parsed_modules):
            index = assigned_indices[local_index]
            ast_path = os.path.join(ast_dir, "module_" + str(index) + ".json")
            write_ast_wire(ast_path, ast_module)
    edges = closed_world_reexport_edges(
        parsed_modules,
        subset_names,
        subset_srcs,
        module_names,
    )
    module_dependencies = closed_world_module_dependencies(
        parsed_modules,
        subset_names,
        subset_srcs,
        module_names,
    )
    function_object_uses = mark_function_object_exports(
        parsed_modules,
        subset_names,
        subset_srcs,
        native_exports,
        known_module_names=module_names,
    )
    exports_path = os.path.join(
        ir_dir,
        "exports_" + os.path.basename(result_path) + ".json",
    )
    edges_path = os.path.join(
        ir_dir,
        "reexports_" + os.path.basename(result_path) + ".json",
    )
    write_native_exports_wire(
        exports_path,
        native_exports,
        {},
        function_object_uses=function_object_uses,
    )
    write_reexport_edges_wire(
        edges_path,
        edges,
        module_dependencies=module_dependencies,
    )
    with open(result_path, "w", encoding="utf-8") as stream:
        line = "EXPORT\t" + exports_path + "\t" + edges_path
        if worker_timing:
            total_ms = int((time.monotonic() - total_started) * 1000)
            line += "\t" + str(total_ms)
        stream.write(line + "\n")
    if worker_timing and len(subset_names) == 1:
        _retain_stage2_export_result(str(subset_names[0]), result_path)
    return 0


def _note_worker_module(module_name) -> None:
    """Record the module about to be lowered, keyed by pid.

    Under pcc1 the exception state is not survivable -- `raise ... from` does
    not set `__cause__` and a wrapped message can arrive empty -- so the module
    identity has to be written down BEFORE the work, not recovered afterwards.
    One file per pid, overwritten each time: the last value is where that worker
    died.
    """
    base = ""
    try:
        base = str(os.environ.get("PCC_COMPILE_PROGRESS_FILE", "") or "")
    except Exception:
        base = ""
    if not base:
        return
    try:
        with open(base + "." + str(os.getpid()), "w", encoding="utf-8") as stream:
            stream.write(str(module_name) + "\n")
    except Exception:
        pass


def _write_one_effect_summary(
    index,
    module_name,
    ast_dir,
    ir_dir,
    native_exports,
    read_ast_wire,
    build_effect_summary,
    write_effect_summary,
):
    # Keep AST and analysis temporaries within one invocation. A process can
    # amortize imports/export decoding without owning a batch of live ASTs.
    ast_path = os.path.join(ast_dir, "module_" + str(index) + ".json")
    ast_module = read_ast_wire(ast_path)
    summary = build_effect_summary(ast_module, module_name, native_exports)
    summary_path = os.path.join(ir_dir, "summary_" + str(index) + ".wire")
    write_effect_summary(summary_path, summary)
    return "SUMMARY\t" + str(index) + "\t" + module_name + "\t" + summary_path + "\n"


def run_summary_worker(
    manifest,
    *,
    read_native_exports_wire,
    read_ast_wire,
    build_effect_summary,
    write_effect_summary,
) -> int:
    assigned_indices = manifest["assigned_indices"]
    module_names = manifest["module_names"]
    if not assigned_indices:
        raise ValueError("frontend summary worker requires at least one module")
    seen = set()
    for index in assigned_indices:
        if index < 0 or index >= len(module_names) or index in seen:
            raise ValueError("frontend summary worker index is invalid or repeated")
        seen.add(index)
    ast_dir = str(manifest.get("ast_dir", "") or "")
    exports_path = str(manifest.get("exports_path", "") or "")
    if not ast_dir or not exports_path:
        raise ValueError("frontend summary worker inputs are missing")
    native_exports, _derived = read_native_exports_wire(exports_path)
    ir_dir = str(manifest["ir_dir"])
    with open(str(manifest["result_path"]), "w", encoding="utf-8") as stream:
        for index in assigned_indices:
            stream.write(_write_one_effect_summary(
                index, module_names[index], ast_dir, ir_dir, native_exports,
                read_ast_wire, build_effect_summary, write_effect_summary,
            ))
    return 0


def run_codegen_worker(
    manifest_path: str,
    *,
    read_manifest,
    run_export_worker_callback,
    run_summary_worker_callback,
    worker_timing_enabled,
    native_worker_executable,
    read_native_exports_wire,
    read_native_exports_wire_for_module,
    read_ast_wire,
    build_closed_world_context,
    module_imports_native_extension,
    contextual_host_params_for_module,
    module_uses_default_native_exports,
    copy_native_module_exports,
    closed_world_function_object_exports,
    log,
    ir_needs_libpython,
    safe_exception_text,
    write_worker_error,
    pipeline_error,
) -> int:
    result_path = ""
    try:
        manifest = read_manifest(manifest_path)
        publish_worker_resource("manifest")
        result_path = str(manifest["result_path"])
        job_kind = str(manifest.get("job_kind", "codegen"))
        if job_kind == "export":
            status = run_export_worker_callback(manifest)
            publish_worker_resource("complete" if status == 0 else "failed")
            return status
        if job_kind == "summary":
            status = run_summary_worker_callback(manifest)
            publish_worker_resource("complete" if status == 0 else "failed")
            return status
        from pcc.frontends.python.type_infer import infer_module
        from pcc.frontends.python.codegen.layer1 import L1CodeGen

        src_paths = manifest["src_paths"]
        module_names = manifest["module_names"]
        entry_module = str(manifest["entry_module"])
        sibling_inits = tuple(manifest["sibling_inits"])
        libpython_mode = str(manifest["libpython_mode"])
        ir_scaffold_mode = str(manifest["ir_scaffold_mode"])
        verbose = bool(manifest["verbose"])
        assigned_indices = manifest["assigned_indices"]
        indexed_sidecar_requested = str(
            os.environ.get("PCC_DIRECT_INDEXED_SIDECAR", "") or ""
        ).strip().lower() in ("1", "true", "yes", "on")
        handoff_request = str(os.environ.get("PCC_INDEXED_HANDOFF_REQUEST", "") or "")
        if handoff_request and (native_worker_executable() or not indexed_sidecar_requested
                                or libpython_mode != "off" or len(assigned_indices) != 1):
            raise _worker_failure("host indexed handoff requires a host singleton no-libpython sidecar worker")
        if indexed_sidecar_requested and len(assigned_indices) != 1:
            raise _worker_failure(
                "indexed sidecar output requires a singleton worker manifest"
            )
        ir_dir = str(manifest["ir_dir"])
        exports_path = str(manifest.get("exports_path", "") or "")
        ast_dir = str(manifest.get("ast_dir", "") or "")
        worker_timing = worker_timing_enabled()
        checkpoint_root = str(manifest.get("checkpoint_root", "") or "")
        checkpoint_graph = None
        checkpoint_skip_indices = []
        if checkpoint_root:
            from pcc.frontends.python import pipeline_stage1_checkpoint as checkpoint

            checkpoint_graph = _validate_stage1_checkpoint_worker(
                manifest, native_worker_executable,
            )
            checkpoint_skip_indices = manifest["checkpoint_skip_indices"]
        from pcc.frontends.python.pipeline_frontend_host_batch import (
            host_codegen_batch_limit, require_empty_host_codegen_context,
            retire_host_codegen_backend_state,
        )

        host_batch_limit = host_codegen_batch_limit(
            os.environ, native_worker=bool(native_worker_executable()),
            indexed_split=bool(handoff_request), checkpoint_root=checkpoint_root,
            action_cache_plan=None, libpython_mode=libpython_mode, artifact_dir=ir_dir,
        )
        host_batch = host_batch_limit == 2 and len(assigned_indices) > 1
        if host_batch and (len(assigned_indices) > 2 or not ast_dir or not exports_path):
            raise _worker_failure("host codegen batch requires at most two lazy-AST assignments")
        unique_external_class_preload = None
        indexed_exports = False
        lazy_ast_dir = ""
        if exports_path:
            # One view of the exports whichever interpreter runs the worker:
            # the root's dependency closure plus its contextual host surface.
            # Host workers used to read the whole graph, so for one module and
            # one exports file the host compiler and pcc1 inferred different
            # things (a codegen mixin's ``self`` method call was dynamic under
            # the host and direct under pcc1) and emitted different IR.
            if len(assigned_indices) == 1 or host_batch:
                root_module = module_names[assigned_indices[0]]
                (
                    native_exports,
                    derived_class_map,
                    unique_external_class_preload,
                    indexed_exports,
                ) = read_native_exports_wire_for_module(
                    exports_path,
                    root_module,
                )
                if host_batch and indexed_exports:
                    # Batch2 reuses the current host legacy/full graph only.
                    # A sparse singleton view has separate global-provider
                    # obligations and cannot silently become a full batch view.
                    raise _worker_failure("host codegen batch requires full-graph export input")
            else:
                native_exports, derived_class_map = read_native_exports_wire(
                    exports_path
                )
            parsed_modules = [None for _source in src_paths]
            parse_ms_by_index = {}
            if ast_dir:
                # Each module's AST is read when the loop reaches it: it must
                # not be alive, or frozen, while other modules are processed.
                lazy_ast_dir = ast_dir
            else:
                from pcc.frontends.python.py_lift import parse_and_lift

                for index in assigned_indices:
                    source_path = src_paths[index]
                    module_name = module_names[index]
                    parse_started = time.monotonic() if worker_timing else 0.0
                    with open(source_path, "r", encoding="utf-8") as stream:
                        source = stream.read()
                    parsed_modules[index] = parse_and_lift(
                        source,
                        source_path,
                        module_name,
                    )
                    if worker_timing:
                        parse_ms_by_index[index] = int(
                            (time.monotonic() - parse_started) * 1000
                        )
        else:
            parse_ms_by_index = {}
            parsed_modules, native_exports, derived_class_map = (
                build_closed_world_context(
                    src_paths,
                    module_names,
                    profile=None,
                    lift_indices=assigned_indices,
                )
            )

        result_lines: list[str] = []
        publish_worker_resource("exports")
        if lazy_ast_dir:
            _freeze_worker_survivors()
        for index in assigned_indices:
            module_name = module_names[index]
            if host_batch:
                require_empty_host_codegen_context()
            if checkpoint_root:
                _validate_stage1_checkpoint_module_context(manifest, checkpoint_graph, index)
                if index in checkpoint_skip_indices:
                    restored = checkpoint.load_module(
                        checkpoint_root, manifest["checkpoint_build_digest"],
                        manifest["checkpoint_graph_digest"], index, module_name,
                        assigned_indices, ir_dir,
                    )
                    if restored is not None:
                        result_lines.append(_stage1_checkpoint_result_line(
                            restored, index, module_name, ir_dir, worker_timing,
                        ))
                        sys.stderr.write(
                            "pcc Stage1 checkpoint reused index=" + str(index)
                            + " module=" + module_name + "\n"
                        )
                        continue
            if lazy_ast_dir:
                parse_started = time.monotonic() if worker_timing else 0.0
                parsed_modules[index] = read_ast_wire(
                    os.path.join(lazy_ast_dir, "module_" + str(index) + ".json")
                )
                if worker_timing:
                    parse_ms_by_index[index] = int(
                        (time.monotonic() - parse_started) * 1000
                    )
            if worker_timing:
                sys.stderr.write(
                    "pcc frontend worker start index="
                    + str(index)
                    + " module="
                    + module_name
                    + " indexed_exports="
                    + ("1" if indexed_exports else "0")
                    + " export_modules="
                    + str(len(native_exports))
                    + "\n"
                )
            ast_module = parsed_modules[index]
            publish_worker_resource("ast:" + str(index))
            needs_native_extension_exports = module_imports_native_extension(
                ast_module,
                native_modules=module_names,
                ir_scaffold_mode=ir_scaffold_mode,
            )
            external_for_this = {}
            for owner_name, exports in native_exports.items():
                if owner_name != module_name:
                    external_for_this[owner_name] = exports
            if host_batch and unique_external_class_preload is None:
                # Reproduce the ordinary legacy singleton's root-excluded
                # preload. Only file/JSON decoding is shared across modules.
                from pcc.frontends.python.type_infer import build_unique_external_class_preload

                unique_external_class_preload = build_unique_external_class_preload(external_for_this)
            infer_ms = 0
            try:
                infer_started = time.monotonic() if worker_timing else 0.0
                typed_module = infer_module(
                    ast_module,
                    external_exports=external_for_this,
                    derived_class_map=derived_class_map,
                    unique_external_class_preload=(
                        unique_external_class_preload
                    ),
                    contextual_host_params=contextual_host_params_for_module(
                        ast_module,
                        module_name,
                    ),
                )
                publish_worker_resource("infer:" + str(index))
                if worker_timing:
                    infer_ms = int((time.monotonic() - infer_started) * 1000)
                    sys.stderr.write(
                        "pcc frontend worker inferred index="
                        + str(index)
                        + " module="
                        + module_name
                        + " infer_ms="
                        + str(infer_ms)
                        + "\n"
                    )
            except Exception as exc:
                raise _worker_failure(
                    "type_infer["
                    + module_name
                    + "]: "
                    + type(exc).__name__
                    + ": "
                    + safe_exception_text(exc)
                ) from exc
            try:
                _note_worker_module(module_name)
                codegen = L1CodeGen(
                    typed_module,
                    libpython_mode == "on",
                    ir_scaffold_mode,
                )
                codegen._strict_no_libpython = libpython_mode == "off"
                codegen._prefer_native_callable_values = libpython_mode == "off"
                codegen._module_source_path = os.path.abspath(src_paths[index])
                codegen._skip_program_main = module_name != entry_module
                codegen._sibling_module_inits = sibling_inits
                if module_uses_default_native_exports(module_name):
                    codegen_exports = copy_native_module_exports(
                        codegen._native_module_exports
                    )
                else:
                    codegen_exports = {}
                for owner_name, exports in native_exports.items():
                    if owner_name != module_name:
                        codegen_exports[owner_name] = exports
                codegen._native_module_exports = codegen_exports
                codegen._native_function_object_exports = (
                    closed_world_function_object_exports(
                        native_exports,
                        module_name,
                    )
                )
                codegen._native_boxed_int_functions = _closed_world_boxed_int_functions(
                    native_exports, module_name,
                )
            except Exception as exc:
                raise _worker_failure(
                    "codegen_prepare["
                    + module_name
                    + "]: "
                    + type(exc).__name__
                    + ": "
                    + safe_exception_text(exc)
                ) from exc
            if verbose:
                log(verbose, "worker codegen[" + module_name + "]")
            codegen_ms = 0
            phase_timing = None
            phase_timing_complete = False
            if worker_timing:
                try:
                    from pcc.backend.indexed_phase_timing import ModulePhaseTiming

                    phase_timing = ModulePhaseTiming(module_name)
                except Exception:
                    # Optional diagnostics must not change compiler outcomes.
                    pass
            try:
                codegen_started = time.monotonic() if worker_timing else 0.0
                direct_path = ""
                direct_marker = ""
                direct_needs_libpython = False
                direct_asm = ""
                direct_lines = []
                direct_lines_output = False
                direct_stack_map_plans = []
                direct_packed_stack_maps = False
                direct_x86_stream = None
                validate_direct = str(
                    os.environ.get("PCC_DIRECT_INDEXED_KERNEL_VALIDATE", "") or ""
                ).strip().lower() in ("1", "true", "yes", "on")
                emit_direct = str(
                    os.environ.get("PCC_DIRECT_INDEXED_KERNEL_EMIT", "") or ""
                ).strip().lower() in ("1", "true", "yes", "on")
                emit_text_control = str(
                    os.environ.get("PCC_TEXT_INDEXED_KERNEL_EMIT", "") or ""
                ).strip().lower() in ("1", "true", "yes", "on")
                require_zero_direct_fallback = str(
                    os.environ.get(
                        "PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK",
                        "",
                    )
                    or ""
                ).strip().lower() in ("1", "true", "yes", "on")
                release_direct_frontend = str(
                    os.environ.get(
                        "PCC_DIRECT_INDEXED_KERNEL_RELEASE_FRONTEND",
                        "",
                    )
                    or ""
                ).strip().lower() in ("1", "true", "yes", "on")
                native_object_output = str(
                    os.environ.get(
                        "PCC_DIRECT_INDEXED_NATIVE_OBJECT", "1"
                    )
                    or "1"
                ).strip().lower() not in (
                    "0",
                    "false",
                    "no",
                    "off",
                )
                indexed_sidecar_output = indexed_sidecar_requested
                direct_passes = (
                    _direct_owned_pass_names(module_name)
                    if emit_direct or emit_text_control else []
                )
                codegen.module._direct_indexed_retain_text = bool(direct_passes)
                # With passes off, publish directly without retaining a text
                # graph. Selected owned passes need their canonical text input;
                # their transformed result is the indexed emitter's input.
                render_ir_text = not (
                    emit_direct
                    and not validate_direct
                    and not emit_text_control
                    and not direct_passes
                )
                phase_started = phase_timing.start() if phase_timing is not None else 0
                # This worker owns a fresh instance and has only configured
                # flags/exports since construction; keep its initial IR.
                generated_module = codegen._generate_initial_module()
                if phase_timing is not None:
                    phase_timing.add(0, phase_started)
                publish_worker_resource("codegen:" + str(index))
                ir_text = str(generated_module) if render_ir_text else ""
                if direct_passes:
                    from pcc.frontends.python.compiled_owned_passes import run_owned_passes

                    pass_started = time.monotonic()
                    ir_text = run_owned_passes(
                        ir_text, direct_passes, libpython_mode == "off",
                    )
                    publish_worker_resource("passes:" + str(index))
                    if worker_timing:
                        sys.stderr.write(
                            "pcc direct owned passes module=" + module_name
                            + " passes=" + ",".join(direct_passes)
                            + " elapsed_ms=" + str(int((time.monotonic() - pass_started) * 1000))
                            + "\n"
                        )
                if validate_direct or emit_direct or emit_text_control:
                    from pcc.backend.self_backend_aarch64_darwin import (
                        emit_aarch64_darwin_asm,
                        emit_aarch64_darwin_indexed_module,
                        emit_aarch64_darwin_indexed_transport,
                    )

                    from pcc.backend.target_objects import emit_indexed_assembly, encode_assembly_object
                    from pcc.backend.self_backend_target_match import (
                        is_aarch64_darwin_triple,
                        is_aarch64_linux_triple,
                        is_x86_64_linux_triple,
                        is_x86_64_windows_triple,
                    )
                    from pcc.backend.self_backend_dispatch import emit_self_asm
                    if validate_direct or emit_direct:
                        if direct_passes:
                            from pcc.backend.self_backend_parse import parse_self_backend_module

                            # Only the transformed program needs an indexed
                            # representation. Do not also build/discard the
                            # pre-pass capture of the same module.
                            direct_module = parse_self_backend_module(ir_text)
                        else:
                            direct_module = codegen._direct_indexed_module
                        if direct_module is None:
                            raise _worker_failure(
                                "direct indexed kernel output requested without capture"
                            )
                        if direct_module.triple == "unknown-unknown-unknown":
                            # The capture precedes the text path's host-target
                            # normalization. Resolve only its placeholder;
                            # an explicit module target remains authoritative.
                            from pcc.frontends.python.pipeline_targets import host_target_triple

                            direct_module = _concrete_direct_indexed_target(
                                direct_module, host_target_triple(),
                            )
                        direct_target = direct_module.triple
                        if phase_timing is not None:
                            phase_timing.target = direct_target
                            phase_timing.route = "indexed-sidecar" if indexed_sidecar_output else "indexed-assembly"
                        from pcc.ir.direct_indexed_kernel import (
                            direct_indexed_module_first_libpython_edge,
                        )

                        direct_libpython_edge = (
                            direct_indexed_module_first_libpython_edge(
                                direct_module
                            )
                        )
                        direct_needs_libpython = bool(direct_libpython_edge)
                        if libpython_mode == "off" and direct_needs_libpython:
                            raise _worker_failure(
                                "direct indexed kernel still has libpython edge "
                                + direct_libpython_edge
                            )
                        if (
                            (require_zero_direct_fallback or handoff_request)
                            and codegen.module._direct_indexed_fallback_records != 0
                        ):
                            raise _worker_failure(
                                "direct indexed kernel used text fallback records: "
                                + str(
                                    codegen.module._direct_indexed_fallback_records
                                )
                            )
                        if indexed_sidecar_output:
                            if validate_direct or emit_text_control:
                                raise _worker_failure(
                                    "indexed sidecar output cannot run a text oracle"
                                )
                            from pcc.backend.self_backend_indexed_codec import (
                                encode_indexed_module_file,
                            )

                            direct_path = os.path.join(
                                ir_dir,
                                "module_" + str(index) + ".direct.pidx",
                            )
                            if handoff_request:
                                from pcc.frontends.python.pipeline_indexed_handoff import publish_handoff

                                # The indexed module owns its immutable kernels.
                                # The rendered canonical pass input is retained
                                # for the ordinary IR/result protocol, but AST,
                                # frontend owners and their cycles can die now.
                                phase_started = phase_timing.start() if phase_timing is not None else 0
                                _release_direct_frontend_state(codegen)
                                parsed_modules[index] = None
                                del ast_module
                                del typed_module
                                del external_for_this
                                del codegen_exports
                                del generated_module
                                del codegen
                                import gc

                                gc.collect()
                                if phase_timing is not None:
                                    phase_timing.add(1, phase_started)
                                direct_path = publish_handoff(
                                    handoff_request, manifest_path, index, module_name,
                                    direct_module, direct_passes, direct_needs_libpython,
                                    needs_native_extension_exports, encode_indexed_module_file,
                                )
                                del direct_module
                                publish_worker_resource("indexed-handoff:" + str(index))
                            else:
                                encode_indexed_module_file(direct_path, direct_module)
                            direct_marker = "PIDX"
                        if (
                            release_direct_frontend
                            and emit_direct
                            and not validate_direct
                            and not indexed_sidecar_output
                        ):
                            phase_started = phase_timing.start() if phase_timing is not None else 0
                            if host_batch:
                                # These provenance Values can root cyclic IR.
                                # Clear them before collection, not next generate.
                                from pcc.frontends.python.codegen.marshal import reset_boxed_i64_constants

                                reset_boxed_i64_constants()
                            _release_direct_frontend_state(codegen)
                            if phase_timing is not None:
                                phase_timing.add(1, phase_started)
                        direct_emit_started = (
                            time.monotonic() if worker_timing else 0.0
                        )
                        direct_lines_output = bool(
                            emit_direct
                            and not indexed_sidecar_output
                            and native_object_output
                            and not validate_direct
                            and not emit_text_control
                            and (
                                is_aarch64_darwin_triple(direct_target)
                                or is_aarch64_linux_triple(direct_target)
                            )
                        )
                        direct_packed_stack_maps = bool(
                            emit_direct
                            and not indexed_sidecar_output
                            and native_object_output
                            and not validate_direct
                            and not emit_text_control
                            and (
                                is_x86_64_linux_triple(direct_target)
                                or is_x86_64_windows_triple(direct_target)
                            )
                        )
                        if direct_lines_output:
                            if phase_timing is not None:
                                phase_timing.route = "indexed-native-sections"
                            phase_started = phase_timing.start() if phase_timing is not None else 0
                            direct_transport = (
                                emit_aarch64_darwin_indexed_transport(
                                    direct_module,
                                    optimize=False,
                                    # Both host and native workers emit final
                                    # machine records through the owned encoder.
                                    structured_instructions=True,
                                    phase_timing=phase_timing,
                                )
                            )
                            if phase_timing is not None:
                                phase_timing.add(2, phase_started)
                            if worker_timing:
                                sys.stderr.write(
                                    "pcc structured instructions module="
                                    + module_name
                                    + " unscaled="
                                    + str(
                                        direct_transport.structured_unscaled_count
                                    )
                                    + " move="
                                    + str(direct_transport.structured_move_count)
                                    + " call="
                                    + str(direct_transport.structured_call_count)
                                    + " fallback="
                                    + str(
                                        direct_transport.fallback_instruction_count
                                    )
                                    + "\n"
                                )
                        elif direct_packed_stack_maps:
                            # x86-64 streams each finished function into its
                            # sections, as AArch64 does with its structured
                            # records; no module text is joined and reparsed.
                            if phase_timing is not None:
                                phase_timing.route = "indexed-native-sections"
                            phase_started = phase_timing.start() if phase_timing is not None else 0
                            from pcc.backend.target_objects import emit_x86_64_streamed_module

                            direct_x86_stream = emit_x86_64_streamed_module(
                                direct_module, phase_timing=phase_timing,
                            )
                            if phase_timing is not None:
                                phase_timing.add(2, phase_started)
                        elif not indexed_sidecar_output:
                            phase_started = phase_timing.start() if phase_timing is not None else 0
                            direct_asm = emit_indexed_assembly(
                                direct_module,
                                optimize=False,
                                stack_map_plans_out=(
                                    direct_stack_map_plans
                                    if direct_packed_stack_maps else None
                                ),
                                phase_timing=phase_timing,
                            )
                            if phase_timing is not None:
                                phase_timing.add(2, phase_started)
                        if (
                            release_direct_frontend
                            and emit_direct
                            and not validate_direct
                            and not indexed_sidecar_output
                        ):
                            # The indexed module is frozen and the canonical IR
                            # has already been serialized. These products are
                            # terminal for this module; a bounded host batch
                            # releases them before reading the next AST.
                            # Release them before the assembler builds its own
                            # Section/Relocation/NativeObject graph; pcc's
                            # allocator can reuse freed cells even though it
                            # cannot yet unmap whole slabs.
                            phase_started = phase_timing.start() if phase_timing is not None else 0
                            parsed_modules[index] = None
                            del ast_module
                            del typed_module
                            del external_for_this
                            del codegen_exports
                            del generated_module
                            del codegen
                            del direct_module
                            import gc

                            gc.collect()
                            if lazy_ast_dir and not host_batch:
                                _freeze_worker_survivors()
                            if phase_timing is not None:
                                phase_timing.add(8, phase_started)
                        if worker_timing and not handoff_request:
                            sys.stderr.write(
                                "pcc direct indexed emit module="
                                + module_name
                                + " elapsed_ms="
                                + str(
                                    int(
                                        (time.monotonic() - direct_emit_started)
                                        * 1000
                                    )
                                )
                                + "\n"
                            )
                        if emit_direct:
                            publish_worker_resource("emit:" + str(index))
                            if indexed_sidecar_output:
                                pass
                            elif native_object_output:
                                from pcc.backend.arm64_asm_driver import (
                                    assemble_file,
                                )
                                from pcc.backend.native_object import (
                                    encode_native_object_from_sections,
                                )

                                direct_path = os.path.join(
                                    ir_dir,
                                    "module_" + str(index) + ".direct.pco",
                                )
                                if direct_lines_output and is_aarch64_linux_triple(direct_target):
                                    from pcc.backend.arm64_elf_driver import from_sections
                                    from pcc.backend.elf_x86_64 import emit_relocatable

                                    # The shared transport owns final words and
                                    # named fixups. Project them through the same
                                    # ELF adapter as the text assembler, without
                                    # rendering/reparsing a module-wide .s file.
                                    phase_started = phase_timing.start() if phase_timing is not None else 0
                                    sections, undefined = direct_transport.assemble_sections()
                                    if direct_transport.encoded_line_records is not None:
                                        direct_transport.encoded_line_records.close()
                                    del direct_transport
                                    elf_object = from_sections(sections, undefined)
                                    del sections
                                    del undefined
                                    if phase_timing is not None:
                                        phase_timing.add(9, phase_started)
                                    phase_started = phase_timing.start() if phase_timing is not None else 0
                                    encoded = emit_relocatable(elf_object)
                                    if phase_timing is not None:
                                        phase_timing.add(10, phase_started)
                                    del elf_object
                                elif direct_x86_stream is not None:
                                    encoded = direct_x86_stream.encode_object(phase_timing)
                                    direct_x86_stream = None
                                    direct_stack_map_plans.clear()
                                elif not is_aarch64_darwin_triple(direct_target):
                                    encoded = encode_assembly_object(
                                        direct_asm, direct_target,
                                        stack_map_plans=(
                                            direct_stack_map_plans
                                            if direct_packed_stack_maps else None
                                        ),
                                        consume_stack_map_plans=direct_packed_stack_maps,
                                        phase_timing=phase_timing,
                                    )
                                    direct_stack_map_plans.clear()
                                    if not validate_direct:
                                        direct_asm = ""
                                else:
                                    if direct_lines_output:
                                        phase_started = phase_timing.start() if phase_timing is not None else 0
                                        sections, undefined = direct_transport.assemble_sections()
                                        if phase_timing is not None:
                                            phase_timing.add(9, phase_started)
                                        if direct_transport.encoded_line_records is not None:
                                            direct_transport.encoded_line_records.close()
                                        del direct_transport
                                    else:
                                        phase_started = phase_timing.start() if phase_timing is not None else 0
                                        sections, undefined = assemble_file(direct_asm)
                                        if phase_timing is not None:
                                            phase_timing.add(9, phase_started)
                                    # Parsing is complete. Unless the text oracle
                                    # still needs it, drop the assembly text before
                                    # validating and encoding the Section graph
                                    # directly. The codec revalidates final packed
                                    # bytes without materializing a duplicate
                                    # NativeSymbol/NativeSection/NativeRelocation
                                    # graph.
                                    if not validate_direct:
                                        direct_asm = ""
                                    phase_started = phase_timing.start() if phase_timing is not None else 0
                                    encoded = encode_native_object_from_sections(
                                        sections,
                                        undefined=undefined,
                                    )
                                    if phase_timing is not None:
                                        phase_timing.add(10, phase_started)
                                    del sections
                                    del undefined
                                phase_started = phase_timing.start() if phase_timing is not None else 0
                                with open(direct_path, "wb") as stream:
                                    stream.write(encoded)
                                if phase_timing is not None:
                                    phase_timing.add(11, phase_started)
                                if checkpoint_root or host_batch:
                                    # A large Stage1 object can itself be 174 MB.
                                    # Release the emitter buffer before the
                                    # durable writer reads and validates it.
                                    del encoded
                                direct_marker = "PCO"
                                publish_worker_resource("object:" + str(index))
                            else:
                                direct_path = os.path.join(
                                    ir_dir,
                                    "module_" + str(index) + ".direct.s",
                                )
                                with open(
                                    direct_path, "w", encoding="utf-8"
                                ) as stream:
                                    stream.write(direct_asm)
                                direct_marker = "ASM"
                    if validate_direct or emit_text_control:
                        text_emit_started = (
                            time.monotonic() if worker_timing else 0.0
                        )
                        from pcc.backend.self_backend_parse import parse_self_backend_target_triple
                        text_target = parse_self_backend_target_triple(ir_text)
                        if is_aarch64_darwin_triple(text_target) or text_target == "unknown-unknown-unknown":
                            text_asm = emit_aarch64_darwin_asm(ir_text, optimize=False)
                        else:
                            text_asm = emit_self_asm(ir_text)
                        if worker_timing:
                            sys.stderr.write(
                                "pcc text oracle emit module="
                                + module_name
                                + " elapsed_ms="
                                + str(
                                    int(
                                        (time.monotonic() - text_emit_started)
                                        * 1000
                                    )
                                )
                                + "\n"
                            )
                        if emit_text_control:
                            text_path = os.path.join(
                                ir_dir,
                                "module_" + str(index) + ".text.s",
                            )
                            with open(
                                text_path, "w", encoding="utf-8"
                            ) as stream:
                                stream.write(text_asm)
                    if validate_direct:
                        if direct_asm != text_asm:
                            raise _worker_failure(
                                "direct indexed kernel assembly differs from text oracle"
                            )
                        # The native object is already encoded. Validation was
                        # the final consumer of its assembly text.
                        if emit_direct and native_object_output:
                            direct_asm = ""
                if worker_timing:
                    codegen_ms = int((time.monotonic() - codegen_started) * 1000)
                phase_timing_complete = True
            except Exception as exc:
                raise _worker_failure(
                    "codegen["
                    + module_name
                    + "]: "
                    + type(exc).__name__
                    + ": "
                    + safe_exception_text(exc)
                ) from exc
            finally:
                if phase_timing is not None:
                    # Outside timed scopes; never replace the original error.
                    phase_timing.report(sys.stderr, phase_timing_complete)
            ir_path = os.path.join(ir_dir, "module_" + str(index) + ".ll")
            with open(ir_path, "w", encoding="utf-8") as stream:
                stream.write(ir_text)
            result_line = (
                "OK\t"
                + str(index)
                + "\t"
                + module_name
                + "\t"
                + (
                    "1"
                    if direct_needs_libpython or ir_needs_libpython(ir_text)
                    else "0"
                )
                + "\t"
                + ("1" if needs_native_extension_exports else "0")
                + "\t"
                + str(len(ir_text))
                + "\t"
                + ir_path
            )
            if worker_timing:
                result_line += (
                    "\t"
                    + str(parse_ms_by_index.get(index, 0))
                    + "\t"
                    + str(infer_ms)
                    + "\t"
                    + str(codegen_ms)
                )
                sys.stderr.write(
                    "pcc frontend worker done index="
                    + str(index)
                    + " module="
                    + module_name
                    + " infer_ms="
                    + str(infer_ms)
                    + " codegen_ms="
                    + str(codegen_ms)
                    + (" artifact=PIDX" if handoff_request else "")
                    + "\n"
                )
            if direct_path:
                result_line += "\t" + direct_marker + "\t" + direct_path
            if checkpoint_root:
                _validate_stage1_checkpoint_module_context(manifest, checkpoint_graph, index)
                if direct_marker != "PCO":
                    raise _worker_failure("Stage1 checkpoint requires a direct object")
                checkpoint_record = {
                    "needs_libpython": bool(
                        direct_needs_libpython or ir_needs_libpython(ir_text)
                    ),
                    "needs_native_extension_exports": bool(needs_native_extension_exports),
                    "ir_bytes_before_passes": len(ir_text),
                    "ir_text": "",
                    "artifact_kind": "PCO",
                    "artifact_path": direct_path,
                    "parse_ms": parse_ms_by_index.get(index, 0),
                    "infer_ms": infer_ms,
                    "codegen_ms": codegen_ms,
                }
                # The normal IR sidecar and TSV metadata are already complete.
                # Owned passes can leave a large canonical text buffer; it is
                # not an input to direct-object linking or receipt validation.
                ir_text = ""
                checkpoint.publish_module(
                    checkpoint_root, manifest["checkpoint_build_digest"],
                    manifest["checkpoint_graph_digest"], index, module_name,
                    assigned_indices,
                    checkpoint_record,
                )
                sys.stderr.write(
                    "pcc Stage1 checkpoint committed index=" + str(index)
                    + " module=" + module_name + "\n"
                )
            result_lines.append(result_line)
            if host_batch:
                # The restricted direct-release path already retired all AST,
                # typed, codegen and indexed owners. Drop remaining products
                # before the next AST is read; only result paths/metadata stay.
                ir_text = ""
                unique_external_class_preload = None
                direct_asm = ""
                direct_lines = []
                direct_stack_map_plans = []
                direct_x86_stream = None
                retire_host_codegen_backend_state(direct_target)
                import gc

                gc.collect()
                require_empty_host_codegen_context()
                publish_worker_resource("module-retired:" + str(index))
        publication_path = result_path + ".batch.partial" if host_batch else result_path
        with open(publication_path, "w", encoding="utf-8") as stream:
            for line in result_lines:
                stream.write(line + "\n")
        if host_batch:
            os.replace(publication_path, result_path)
        publish_worker_resource("complete")
        return 0
    except Exception as exc:
        exc_type = type(exc).__name__
        if exc_type is None:
            exc_type = "Exception"
        detail = safe_exception_text(exc)
        if not detail:
            # An empty message is worse than no error at all: the caller
            # reports "PyPipelineError: " and the real failure surfaces much
            # later as an unrelated "linker has no inputs".  Fall back to the
            # repr.  Importing ``traceback`` here would add a dynamic
            # libpython-only edge to the strict pcc1 worker; the module marker
            # written before codegen and the durable failure file below retain
            # the actionable location without weakening that closure.
            try:
                detail = repr(exc)
            except Exception:
                detail = "<no message>"
        message = exc_type + ": " + detail
        if result_path:
            try:
                write_worker_error(result_path, message)
            except Exception:
                pass
        try:
            sys.stderr.write("pcc frontend worker failed: " + message + "\n")
        except Exception:
            pass
        # Land the failure on disk too.  The stderr line above and the result
        # file both travel back through machinery that has already been
        # observed to lose the text entirely under pcc1, and the caller then
        # reports a bare "compile failed".  A file written here cannot be
        # degraded by anything downstream.
        try:
            base = str(os.environ.get("PCC_COMPILE_PROGRESS_FILE", "") or "")
            if base:
                with open(base + ".fail." + str(os.getpid()), "w",
                          encoding="utf-8") as stream:
                    stream.write(message + "\n")
        except Exception:
            pass
        if str(
            os.environ.get("PCC_DEBUG_WORKER_RERAISE", "") or ""
        ).strip() not in ("", "0"):
            # The handler above deliberately avoids ``traceback`` so the strict
            # worker keeps its libpython closure, which also means a worker
            # failure arrives as a bare "Type: message" with no location. Set
            # this to let the exception reach the process handler instead: it
            # prints file and line, and the durable failure file and stderr
            # line have already been written above, so the parent still sees
            # the same report.
            raise
        return 1
