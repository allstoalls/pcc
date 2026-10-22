"""Bounded host codegen batches over the established worker protocol."""

from __future__ import annotations

import os


HOST_BATCH_ENV = "PCC_HOST_CODEGEN_BATCH"
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def host_codegen_batch_limit(env, *, native_worker, indexed_split,
                             checkpoint_root, action_cache_plan,
                             libpython_mode, artifact_dir):
    """Select only an explicitly requested host direct-object batch of two."""
    raw = str(env.get(HOST_BATCH_ENV, "") or "").strip()
    if raw in ("", "0", "1"):
        return 1
    if raw != "2":
        raise ValueError("host codegen batch size must be 1 or 2")
    if native_worker or indexed_split or checkpoint_root or action_cache_plan is not None:
        raise ValueError("host codegen batch cannot share native, split, checkpoint or action-cache work")
    if libpython_mode != "off" or not artifact_dir:
        raise ValueError("host codegen batch requires no-libpython and an artifact root")
    for name in ("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "PCC_DIRECT_INDEXED_KERNEL_EMIT",
                 "PCC_DIRECT_INDEXED_KERNEL_RELEASE_FRONTEND", "PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK"):
        if str(env.get(name, "") or "").strip().lower() not in _TRUE:
            raise ValueError("host codegen batch requires " + name)
    for name in ("PCC_DIRECT_INDEXED_KERNEL_VALIDATE", "PCC_TEXT_INDEXED_KERNEL_EMIT",
                 "PCC_DIRECT_INDEXED_SIDECAR", "PCC_PY_FRONTEND_IN_PROCESS_CODEGEN"):
        if str(env.get(name, "") or "").strip().lower() in _TRUE:
            raise ValueError("host codegen batch does not support " + name)
    if str(env.get("PCC_DIRECT_INDEXED_NATIVE_OBJECT", "1") or "1").strip().lower() in _FALSE:
        raise ValueError("host codegen batch requires native object output")
    if str(env.get("PCC_DEFER_FRONTEND_CODEGEN_PLAN", "") or "").strip():
        raise ValueError("host codegen batch cannot share a deferred plan")
    from pcc.frontends.python.pipeline_frontend_workers import worker_tree_budget_bytes

    if worker_tree_budget_bytes(env.get("PCC_WORKER_TREE_BUDGET_BYTES", "")) <= 0:
        raise ValueError("host codegen batch requires measured tree admission")
    return 2


def bounded_host_codegen_batches(src_paths, ast_dir, chunks):
    """Pair safe singleton assignments without turning a count into a cap.

    Large source/AST modules keep the existing singleton shape. The resource
    planner orders whole batches afterwards, using summed work and separately
    conservative batch-memory priors; this helper never increases concurrency.
    """
    from pcc.frontends.python.pipeline_frontend_workers import (
        SOURCE_WORKER_AST_OVERSIZED_BYTES,
        SOURCE_WORKER_OVERSIZED_BYTES,
    )

    result = []
    pending = []
    seen = set()
    for chunk in chunks:
        if len(chunk) != 1:
            raise ValueError("host codegen batching requires singleton input assignments")
        index = chunk[0]
        if index in seen or index < 0 or index >= len(src_paths):
            raise ValueError("host codegen batch assignment differs")
        seen.add(index)
        source_bytes = os.path.getsize(src_paths[index])
        ast_bytes = os.path.getsize(os.path.join(ast_dir, "module_" + str(index) + ".json"))
        oversized = (source_bytes >= SOURCE_WORKER_OVERSIZED_BYTES
                     or ast_bytes >= SOURCE_WORKER_AST_OVERSIZED_BYTES)
        if oversized:
            if pending:
                result.append(pending)
                pending = []
            result.append([index])
            continue
        pending.append(index)
        if len(pending) == 2:
            result.append(pending)
            pending = []
    if pending:
        result.append(pending)
    return result


def require_empty_host_codegen_context():
    """Named types must not leak into another module's direct capture."""
    from pcc.ir.compat import ir

    if ir.global_context.identified_types:
        raise ValueError("host codegen batch cannot reuse a shared identified-type context")


def retire_host_codegen_backend_state(target):
    """End one synchronous batch module after PCO and IR have been written.

    Deferred x86 stack-map encoding still reads target symbols after assembly
    emission. Calling this at the assembly boundary would invalidate it.
    """
    from pcc.backend.self_backend_target_match import (
        is_aarch64_darwin_triple, is_aarch64_linux_triple,
        is_x86_64_linux_triple, is_x86_64_windows_triple,
    )
    from pcc.backend.self_backend_ir import retire_module_text_key_cache
    from pcc.backend.self_backend_parse import retire_module_parse_caches

    if is_x86_64_linux_triple(target) or is_x86_64_windows_triple(target):
        from pcc.backend.self_backend_x86_64_linux import retire_x86_module_state

        retire_x86_module_state()
    elif is_aarch64_linux_triple(target) or is_aarch64_darwin_triple(target):
        from pcc.backend.self_backend_aarch64_darwin import retire_aarch64_module_state

        retire_aarch64_module_state()
    else:
        raise ValueError("unsupported host codegen batch target: " + str(target))
    retire_module_parse_caches()
    retire_module_text_key_cache()


def validate_host_batch_result(text, assigned_indices, module_names, ir_dir):
    """Validate the complete batch before the collector accepts any row."""
    expected = set(assigned_indices)
    if not expected or len(expected) != len(assigned_indices) or len(expected) > 2:
        raise ValueError("invalid host codegen batch result assignment")
    seen = set()
    for raw_line in text.splitlines():
        fields = raw_line.split("\t")
        if fields and fields[0] == "ERR":
            raise ValueError(fields[1] if len(fields) > 1 else "worker error")
        if len(fields) not in (9, 12) or fields[0] != "OK":
            raise ValueError("invalid host codegen batch result row")
        try:
            index = int(fields[1])
            ir_bytes = int(fields[5])
        except ValueError as exc:
            raise ValueError("invalid host codegen batch result integer") from exc
        if (index not in expected or index in seen or index < 0
                or index >= len(module_names) or fields[2] != module_names[index]
                or fields[3] not in ("0", "1") or fields[4] not in ("0", "1")
                or ir_bytes < 0 or fields[-2] != "PCO"):
            raise ValueError("host codegen batch result binding differs")
        stem = os.path.join(ir_dir, "module_" + str(index))
        if (os.path.abspath(fields[6]) != os.path.abspath(stem + ".ll")
                or os.path.abspath(fields[-1]) != os.path.abspath(stem + ".direct.pco")
                or not os.path.isfile(fields[6]) or not os.path.isfile(fields[-1])):
            raise ValueError("host codegen batch result artifact differs")
        if len(fields) == 12:
            try:
                if any(int(value) < 0 for value in fields[7:10]):
                    raise ValueError("negative timing")
            except ValueError as exc:
                raise ValueError("invalid host codegen batch result timing") from exc
        seen.add(index)
    if seen != expected:
        raise ValueError("host codegen batch result omitted an assigned module")
