"""Execution protocol for isolated multi-module frontend workers."""

from __future__ import annotations

import os
import sys
import time


def _worker_failure(message: str) -> Exception:
    """Use a bootstrap-safe exception inside the isolated worker boundary."""
    return Exception(message)


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


def run_summary_worker(
    manifest,
    *,
    read_native_exports_wire,
    read_ast_wire,
    build_effect_summary,
    write_effect_summary,
) -> int:
    assigned_indices = manifest["assigned_indices"]
    if len(assigned_indices) != 1:
        raise ValueError("frontend summary worker requires exactly one module")
    index = assigned_indices[0]
    module_names = manifest["module_names"]
    if index < 0 or index >= len(module_names):
        raise ValueError("frontend summary worker index is out of range")
    ast_dir = str(manifest.get("ast_dir", "") or "")
    exports_path = str(manifest.get("exports_path", "") or "")
    if not ast_dir or not exports_path:
        raise ValueError("frontend summary worker inputs are missing")
    native_exports, _derived = read_native_exports_wire(exports_path)
    ast_path = os.path.join(ast_dir, "module_" + str(index) + ".json")
    module_name = module_names[index]
    ast_module = read_ast_wire(ast_path)
    summary = build_effect_summary(ast_module, module_name, native_exports)
    summary_path = os.path.join(
        str(manifest["ir_dir"]),
        "summary_" + str(index) + ".wire",
    )
    write_effect_summary(summary_path, summary)
    with open(str(manifest["result_path"]), "w", encoding="utf-8") as stream:
        stream.write(
            "SUMMARY\t"
            + str(index)
            + "\t"
            + module_name
            + "\t"
            + summary_path
            + "\n"
        )
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
        result_path = str(manifest["result_path"])
        job_kind = str(manifest.get("job_kind", "codegen"))
        if job_kind == "export":
            return run_export_worker_callback(manifest)
        if job_kind == "summary":
            return run_summary_worker_callback(manifest)
        from .type_infer import infer_module
        from .codegen.layer1 import L1CodeGen
        is_native_worker = bool(native_worker_executable())

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
        if indexed_sidecar_requested and len(assigned_indices) != 1:
            raise _worker_failure(
                "indexed sidecar output requires a singleton worker manifest"
            )
        ir_dir = str(manifest["ir_dir"])
        exports_path = str(manifest.get("exports_path", "") or "")
        ast_dir = str(manifest.get("ast_dir", "") or "")
        worker_timing = worker_timing_enabled()
        unique_external_class_preload = None
        indexed_exports = False
        if exports_path:
            if is_native_worker and len(assigned_indices) == 1:
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
            else:
                native_exports, derived_class_map = read_native_exports_wire(
                    exports_path
                )
            parsed_modules = [None for _source in src_paths]
            parse_ms_by_index = {}
            if ast_dir:
                for index in assigned_indices:
                    parse_started = time.monotonic() if worker_timing else 0.0
                    ast_path = os.path.join(
                        ast_dir,
                        "module_" + str(index) + ".json",
                    )
                    parsed_modules[index] = read_ast_wire(ast_path)
                    if worker_timing:
                        parse_ms_by_index[index] = int(
                            (time.monotonic() - parse_started) * 1000
                        )
            else:
                from ..parse.py_lift import parse_and_lift

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
        for index in assigned_indices:
            module_name = module_names[index]
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
            needs_native_extension_exports = module_imports_native_extension(
                ast_module,
                native_modules=module_names,
                ir_scaffold_mode=ir_scaffold_mode,
            )
            external_for_this = {}
            for owner_name, exports in native_exports.items():
                if owner_name != module_name:
                    external_for_this[owner_name] = exports
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
            try:
                codegen_started = time.monotonic() if worker_timing else 0.0
                direct_path = ""
                direct_marker = ""
                direct_needs_libpython = False
                direct_asm = ""
                direct_lines = []
                direct_lines_output = False
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
                # Direct native-object publication consumes the indexed
                # module, not LLVM text.  Rendering that module first retained
                # a second multi-megabyte graph in each worker and was the
                # missing half of the old text-round-trip removal.  The text
                # remains authoritative for validation, text-control and all
                # non-PCO modes.
                render_ir_text = not (
                    emit_direct
                    and not validate_direct
                    and not emit_text_control
                )
                generated_module = codegen.generate(typed_module)
                ir_text = str(generated_module) if render_ir_text else ""
                if validate_direct or emit_direct or emit_text_control:
                    from pcc.backend.self_backend_aarch64_darwin import (
                        emit_aarch64_darwin_asm,
                        emit_aarch64_darwin_indexed_module,
                        emit_aarch64_darwin_indexed_transport,
                    )

                    if validate_direct or emit_direct:
                        direct_module = codegen._direct_indexed_module
                        if direct_module is None:
                            raise _worker_failure(
                                "direct indexed kernel output requested without capture"
                            )
                        from pcc.llvm_capi.direct_indexed_kernel import (
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
                            require_zero_direct_fallback
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
                            encode_indexed_module_file(direct_path, direct_module)
                            direct_marker = "PIDX"
                        if (
                            release_direct_frontend
                            and emit_direct
                            and not validate_direct
                            and not indexed_sidecar_output
                        ):
                            _release_direct_frontend_state(codegen)
                        direct_emit_started = (
                            time.monotonic() if worker_timing else 0.0
                        )
                        direct_lines_output = bool(
                            emit_direct
                            and not indexed_sidecar_output
                            and native_object_output
                            and not validate_direct
                            and not emit_text_control
                        )
                        if direct_lines_output:
                            direct_transport = (
                                emit_aarch64_darwin_indexed_transport(
                                    direct_module,
                                    optimize=False,
                                    # Both host and native workers emit final
                                    # machine records through the owned encoder.
                                    structured_instructions=True,
                                )
                            )
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
                        elif not indexed_sidecar_output:
                            direct_asm = emit_aarch64_darwin_indexed_module(
                                direct_module,
                                optimize=False,
                            )
                        if (
                            release_direct_frontend
                            and emit_direct
                            and not validate_direct
                            and not indexed_sidecar_output
                        ):
                            # The indexed module is frozen and the canonical IR
                            # has already been serialized.  A direct native-
                            # object worker handles one module, so no later
                            # iteration can consume these frontend graphs.
                            # Release them before the assembler builds its own
                            # Section/Relocation/NativeObject graph; pcc's
                            # allocator can reuse freed cells even though it
                            # cannot yet unmap whole slabs.
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
                        if worker_timing:
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
                                if direct_lines_output:
                                    sections, undefined = direct_transport.assemble_sections()
                                    if direct_transport.encoded_line_records is not None:
                                        direct_transport.encoded_line_records.close()
                                    del direct_transport
                                else:
                                    sections, undefined = assemble_file(direct_asm)
                                # Parsing is complete. Drop the assembly text,
                                # then validate and encode the Section graph
                                # directly. The codec revalidates final packed
                                # bytes without materializing a duplicate
                                # NativeSymbol/NativeSection/NativeRelocation
                                # graph.
                                direct_asm = ""
                                encoded = encode_native_object_from_sections(
                                    sections,
                                    undefined=undefined,
                                )
                                del sections
                                del undefined
                                with open(direct_path, "wb") as stream:
                                    stream.write(encoded)
                                direct_marker = "PCO"
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
                        text_asm = emit_aarch64_darwin_asm(
                            ir_text,
                            optimize=False,
                        )
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
                if worker_timing:
                    codegen_ms = int((time.monotonic() - codegen_started) * 1000)
            except Exception as exc:
                raise _worker_failure(
                    "codegen["
                    + module_name
                    + "]: "
                    + type(exc).__name__
                    + ": "
                    + safe_exception_text(exc)
                ) from exc
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
                    + "\n"
                )
            if direct_path:
                result_line += "\t" + direct_marker + "\t" + direct_path
            result_lines.append(result_line)
        with open(result_path, "w", encoding="utf-8") as stream:
            for line in result_lines:
                stream.write(line + "\n")
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
