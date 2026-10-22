"""Snapshot-only control for existing real allocator/native CI comparisons.

No new compiler program or invocation is introduced. This helper is used only
inside an existing reference arm; candidate arms retain production guards.
"""

import ast
from importlib import import_module
import json
from pathlib import Path


PROJECTION_MODULES = (
    "pcc.backend.self_backend_kernel",
    "pcc.backend.self_backend_precise_stackmaps",
    "pcc.backend.self_backend_verify",
    "pcc.backend.self_backend_aarch64_darwin_regalloc",
    "pcc.backend.self_backend_aarch64_darwin_flow",
    "pcc.backend.self_backend_aarch64_darwin_memory",
    "pcc.backend.self_backend_aarch64_darwin_calls",
    "pcc.backend.self_backend_aarch64_darwin_ops",
    "pcc.backend.self_backend_aarch64_darwin_prologue",
    "pcc.backend.self_backend_aarch64_darwin_returns",
)


def disable_scalar_projections(patch, allocator=None):
    def snapshot_only(arena, record_id, stride, window_offset):
        return False

    for name in PROJECTION_MODULES:
        patch.setattr(import_module(name), "_scalar_window_is_complete", snapshot_only)
    if allocator is not None:
        # old_scan_allocator deliberately has its own copied global namespace.
        patch.setitem(allocator.__globals__, "_scalar_window_is_complete", snapshot_only)

    # This separate successor retires scratch earlier. Existing reference arms
    # retain the exact pre-retirement functions, without a new program/run.
    stackmaps = import_module("pcc.backend.self_backend_precise_stackmaps")
    reference = json.loads((Path(__file__).parent / "managed_scratch_retirement_reference.json").read_text())
    names = ("_native_managed_liveness", "_build_function_stack_map_plan_native")
    nodes = [ast.parse(reference["functions"][name]).body[0] for name in names]
    tree = ast.Module(body=[ast.ImportFrom(module="__future__",
                      names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])
    namespace = dict(vars(stackmaps))
    exec(compile(ast.fix_missing_locations(tree), "<pre-retirement-stackmap-reference>", "exec"), namespace)
    reference_plan = namespace["_build_function_stack_map_plan_native"]

    def per_function_plan(func, globals_, identity_name, _globals_index=None):
        # The current emitter passes a shared-index holder during its initial
        # batch. Keep the frozen three-argument oracle unchanged: it rebuilds
        # the globals index for every function, including tail-call replans.
        # The reference must neither read nor populate the candidate's holder.
        return reference_plan(func, globals_, identity_name)

    patch.setattr(stackmaps, "_native_managed_liveness", namespace["_native_managed_liveness"])
    patch.setattr(stackmaps, "_build_function_stack_map_plan_native", per_function_plan)
