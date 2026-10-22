"""Re-run backend-specific GC test files under explicit GC/backend env.

The underlying test files spawn subprocesses; those subprocesses inherit
``PCC_GC_BACKEND`` from the pytest process. Running plain ``pytest <file>``
without setting that env exercises only the default backend (#0). This
wrapper parametrizes over the GC backends each gate originally covered and
over the Python native backend where that environment can affect the test:

* The default self lane retains the complete target sets.
* The explicit self lane retains the compiler-sensitive target sets.

Most pure C probes select their GC algorithm through ``pcc_gc_set_backend`` and
do not invoke the Python compiler, so the explicit self lane keeps only
compiler and pcc-Python-runtime nodes.  The GC4 production contract is retained
in both lanes deliberately: its task-board claim requires the complete
127-probe contract at both mode boundaries.  Each independent
file/configuration runs in a bounded subprocess.  Node ids
that share one file and build configuration stay in one inner pytest process
so they can reuse the same immutable runtime archive; pytest still executes
and reports every node independently.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest


def _node(path: str, name: str) -> str:
    return path + "::" + name


_GENERATIONAL_FILE = "tests/python/test_gc_backend_generational.py"
_GENERATIONAL_TARGETS = (
    _node(_GENERATIONAL_FILE, "test_generational_backend_small_alloc_uses_minor_fast_path"),
    _node(_GENERATIONAL_FILE, "test_generational_backend_minor_heap_pressure_triggers_collection"),
    _node(_GENERATIONAL_FILE, "test_generational_backend_c_runtime_uses_minor_bump_arena"),
    _node(_GENERATIONAL_FILE, "test_generational_backend_pcc_python_runtime_uses_minor_bump_arena"),
    _node(_GENERATIONAL_FILE, "test_generational_backend_pcc_python_runtime_threaded_minor_blocks"),
    _node(_GENERATIONAL_FILE, "test_generational_backend_pcc_python_runtime_class_instances_deallocate_from_minor_arena"),
    _node(_GENERATIONAL_FILE, "test_generational_backend_pcc_python_runtime_string_constructor_preserves_minor_flags"),
    _node(_GENERATIONAL_FILE, "test_generational_backend_pcc_python_runtime_minor_refill_promotes_remembered_young_child"),
    _node(_GENERATIONAL_FILE, "test_generational_backend_minor_refill_promotes_remembered_young_child"),
    _node(_GENERATIONAL_FILE, "test_generational_backend_minor_refill_oldifies_copy_for_remembered_child"),
    _node(_GENERATIONAL_FILE, "test_generational_backend_minor_refill_rewrites_remembered_list_slot_to_oldified_copy"),
    _node(_GENERATIONAL_FILE, "test_generational_backend_minor_refill_rewrites_non_list_owned_slots_to_oldified_copy"),
    _node(_GENERATIONAL_FILE, "test_generational_backend_minor_refill_rewrites_frame_root_slot_to_oldified_copy"),
    _node(_GENERATIONAL_FILE, "test_generational_backend_minor_refill_rewrites_suspended_generator_frame_slot_to_oldified_copy"),
    _node(_GENERATIONAL_FILE, "test_generational_backend_minor_refill_rewrites_generator_coroutine_state_slots_to_oldified_copy"),
    _node(_GENERATIONAL_FILE, "test_generational_backend_minor_refill_rewrites_task_state_slots_to_oldified_copy"),
    _node(_GENERATIONAL_FILE, "test_generational_backend_minor_refill_rewrites_scheduler_root_slot_to_oldified_copy"),
    _node(_GENERATIONAL_FILE, "test_generational_backend_minor_refill_rewrites_scheduler_queue_entry_to_oldified_copy"),
    _node(_GENERATIONAL_FILE, "test_generational_backend_minor_refill_rewrites_class_metadata_slots_to_oldified_copy"),
    _node(_GENERATIONAL_FILE, "test_generational_backend_forwarded_minor_source_is_inactive_after_oldify"),
    _node(_GENERATIONAL_FILE, "test_generational_backend_pcc_python_runtime_minor_refill_oldifies_copy_for_remembered_child"),
    _node(_GENERATIONAL_FILE, "test_generational_backend_pcc_python_runtime_minor_refill_rewrites_remembered_list_slot_to_oldified_copy"),
    _node(_GENERATIONAL_FILE, "test_generational_backend_pcc_python_runtime_minor_refill_rewrites_non_list_owned_slots_to_oldified_copy"),
    _node(_GENERATIONAL_FILE, "test_generational_backend_pcc_python_runtime_minor_refill_rewrites_frame_root_slot_to_oldified_copy"),
    _node(_GENERATIONAL_FILE, "test_generational_backend_pcc_python_runtime_minor_refill_rewrites_suspended_generator_frame_slot_to_oldified_copy"),
    _node(_GENERATIONAL_FILE, "test_generational_backend_pcc_python_runtime_minor_refill_rewrites_generator_coroutine_state_slots_to_oldified_copy"),
    _node(_GENERATIONAL_FILE, "test_generational_backend_pcc_python_runtime_minor_refill_rewrites_task_state_slots_to_oldified_copy"),
    _node(_GENERATIONAL_FILE, "test_generational_backend_pcc_python_runtime_minor_refill_rewrites_scheduler_root_slot_to_oldified_copy"),
    _node(_GENERATIONAL_FILE, "test_generational_backend_pcc_python_runtime_minor_refill_rewrites_scheduler_queue_entry_to_oldified_copy"),
    _node(_GENERATIONAL_FILE, "test_generational_backend_pcc_python_runtime_minor_refill_rewrites_class_metadata_slots_to_oldified_copy"),
    _node(_GENERATIONAL_FILE, "test_generational_backend_pcc_python_runtime_forwarded_minor_source_is_inactive_after_oldify"),
)

_RELOCATING_FILE = "tests/python/test_gc_backend_relocating.py"
_RELOCATING_TARGETS = (
    _node(_RELOCATING_FILE, "test_colored_relocating_task_and_scheduler_queue_follow_forwarding"),
    _node(_RELOCATING_FILE, "test_pcc_python_colored_relocating_task_and_scheduler_queue_follow_forwarding"),
    _node(_RELOCATING_FILE, "test_colored_relocating_list_copy_owns_item_array"),
    _node(_RELOCATING_FILE, "test_pcc_python_colored_relocating_list_copy_owns_item_array"),
    _node(_RELOCATING_FILE, "test_colored_relocating_tuple_copy_retains_owned_items"),
    _node(_RELOCATING_FILE, "test_pcc_python_colored_relocating_tuple_copy_retains_owned_items"),
    _node(_RELOCATING_FILE, "test_colored_relocating_task_copy_retains_state_slots"),
    _node(_RELOCATING_FILE, "test_pcc_python_colored_relocating_task_copy_retains_state_slots"),
    _node(_RELOCATING_FILE, "test_colored_relocating_set_copy_retains_owned_entries"),
    _node(_RELOCATING_FILE, "test_pcc_python_colored_relocating_set_copy_retains_owned_entries"),
    _node(_RELOCATING_FILE, "test_colored_relocating_dict_copy_retains_owned_tables"),
    _node(_RELOCATING_FILE, "test_pcc_python_colored_relocating_dict_copy_retains_owned_tables"),
    _node(_RELOCATING_FILE, "test_colored_relocating_instance_copy_retains_owned_fields"),
    _node(_RELOCATING_FILE, "test_pcc_python_colored_relocating_instance_copy_retains_owned_fields"),
    _node(_RELOCATING_FILE, "test_colored_relocating_targets_wait_for_phase_reset"),
    _node(_RELOCATING_FILE, "test_pcc_python_colored_relocating_targets_wait_for_phase_reset"),
    _node(_RELOCATING_FILE, "test_colored_relocating_load_barrier_follows_forwarding_entry"),
    _node(_RELOCATING_FILE, "test_colored_relocating_rejects_forwarding_for_pinned_objects"),
    _node(_RELOCATING_FILE, "test_colored_relocating_stable_id_survives_forwarding"),
    _node(_RELOCATING_FILE, "test_colored_relocating_selects_unpinned_relocation_set"),
    _node(_RELOCATING_FILE, "test_colored_relocating_copy_forwards_selected_payload_object"),
    _node(_RELOCATING_FILE, "test_colored_relocating_copy_consumes_relocation_entry"),
    _node(_RELOCATING_FILE, "test_colored_relocating_step_forwards_selected_payload_object"),
)

_CONCURRENT_COLLECTION_FILE = "tests/python/test_gc_concurrent_collection.py"
_CONCURRENT_COLLECTION_PCC_PYTHON_TARGET = _node(
    _CONCURRENT_COLLECTION_FILE,
    "test_pcc_python_runtime_object_graph_threadsanitizer_or_skip",
)
_COROUTINE_ROOTS_FILE = "tests/python/test_gc_coroutine_roots.py"
_COROUTINE_ROOTS_PCC_PYTHON_TARGETS = (
    _node(
        _COROUTINE_ROOTS_FILE,
        "test_pcc_python_runtime_suspended_heap_frame_local_survives_collect_across_backends",
    ),
    _node(
        _COROUTINE_ROOTS_FILE,
        "test_pcc_python_runtime_task_completion_releases_waiter_cycle_across_backends",
    ),
    _node(
        _COROUTINE_ROOTS_FILE,
        "test_pcc_python_runtime_virtual_thread_scheduler_queues_keep_continuation_roots_across_backends",
    ),
)


_BACKEND_TEST_GROUPS = {
    "2": (
        "tests/python/test_gc_backend23_production.py",
        "tests/python/test_gc_backend_concurrent.py",
        _CONCURRENT_COLLECTION_FILE,
        "tests/python/test_gc_threading_substrate.py",
    ),
    "3": (
        "tests/python/test_gc_backend23_production.py",
        _GENERATIONAL_TARGETS,
        _CONCURRENT_COLLECTION_FILE,
        _COROUTINE_ROOTS_FILE,
    ),
    "4": (
        _RELOCATING_TARGETS,
        "tests/python/test_gc_backend4_production.py",
        "tests/python/test_gc_abstraction_surface.py",
        _COROUTINE_ROOTS_FILE,
    ),
}


_FRONTEND_BACKENDS = (None, "self")
_FRONTEND_INDEPENDENT_TARGETS = {
    "tests/python/test_gc_backend23_production.py",
    "tests/python/test_gc_backend_concurrent.py",
}
# These cases each spawn a full inner `pytest` that compiles + runs a GC suite
# (some multi-core). A distinct xdist group per GC backend allowed every group
# to occupy a worker at once; together with the runtime oracle, that starved
# normally-fast inner compiles past their 240s/300s subprocess timeouts. Keep
# two frontend-shaped heavy lanes instead. The default self lane owns complete
# target sets while the explicit self lane owns reduced targets plus the runtime oracle, so
# `--dist=loadgroup` retains useful overlap without launching one nested pytest
# per GC backend concurrently.
_SUBPROCESS_TIMEOUT_SECONDS = 240
_SLOW_SUBPROCESS_TIMEOUT_SECONDS = 300


def _timeout_for_target(test_target: str | tuple[str, ...]) -> int:
    if "tests/python/test_gc_backend4_production.py" in _target_args(test_target):
        return _SLOW_SUBPROCESS_TIMEOUT_SECONDS
    return _SUBPROCESS_TIMEOUT_SECONDS


def _target_args(test_target: str | tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(test_target, str):
        return (test_target,)
    return test_target


def _self_frontend_target(
    test_target: str | tuple[str, ...],
) -> str | tuple[str, ...] | None:
    """Return only nodes whose behavior can change under PCC_BACKEND=self."""

    if isinstance(test_target, str):
        if test_target in _FRONTEND_INDEPENDENT_TARGETS:
            return None
        if test_target == _CONCURRENT_COLLECTION_FILE:
            return _CONCURRENT_COLLECTION_PCC_PYTHON_TARGET
        if test_target == _COROUTINE_ROOTS_FILE:
            return _COROUTINE_ROOTS_PCC_PYTHON_TARGETS
        return test_target
    if test_target is _GENERATIONAL_TARGETS:
        compiler_nodes = set(_GENERATIONAL_TARGETS[:2])
        return tuple(
            node
            for node in test_target
            if node in compiler_nodes or "pcc_python" in node
        )
    if test_target is _RELOCATING_TARGETS:
        return tuple(node for node in test_target if "pcc_python" in node)
    return test_target


def _timeout_output_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return ""


def _iter_cases():
    for gc_backend, targets in _BACKEND_TEST_GROUPS.items():
        for frontend_backend in _FRONTEND_BACKENDS:
            selected_targets = []
            for complete_target in targets:
                selected = (
                    complete_target
                    if frontend_backend is None
                    else _self_frontend_target(complete_target)
                )
                if selected is None:
                    continue
                selected_targets.extend(_target_args(selected))
            test_target = tuple(selected_targets)
            yield pytest.param(
                frontend_backend,
                gc_backend,
                test_target,
                # One inner pytest owns the complete frontend/GC slice. This
                # keeps module caches alive and avoids dozens of repeated
                # pytest startup/teardown cycles.
                marks=pytest.mark.xdist_group(
                    name="pcc_heavy_self_full"
                    if frontend_backend is None
                    else "pcc_heavy_self"
                ),
                # Keep the public node id independent of batch membership so
                # exact task-board gates cannot silently select zero tests when
                # a target is added, removed, or consolidated.
                id="frontend=" + (frontend_backend or "default-self") + "-gc=" + gc_backend,
            )


def _run_file_under_backends(
    frontend_backend: str | None,
    gc_backend: str,
    test_target: str | tuple[str, ...],
) -> None:
    env = {
        **os.environ,
        "PCC_BACKEND": frontend_backend or "",
        "PCC_GC_BACKEND": gc_backend,
    }
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-x",
        "-vv",
        "--tb=short",
        "-n0",
        *_target_args(test_target),
    ]
    timeout_seconds = _timeout_for_target(test_target)
    try:
        proc = subprocess.run(
            env=env,
            args=cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = _timeout_output_text(exc.stdout)
        stderr = _timeout_output_text(exc.stderr)
        raise AssertionError(
            "GC subset timed out after "
            + str(timeout_seconds)
            + "s:\n"
            + f"frontend backend: {frontend_backend}\n"
            + f"PCC_GC_BACKEND={gc_backend}\n"
            + f"cmd: {' '.join(cmd)}\n"
            + f"stdout:\n{stdout}\n"
            + f"stderr:\n{stderr}"
        ) from exc
    assert proc.returncode == 0, (
        f"frontend backend {frontend_backend!r} with "
        f"PCC_GC_BACKEND={gc_backend} failed (exit {proc.returncode}):\n"
        f"cmd: {' '.join(cmd)}\n"
        f"stdout:\n{proc.stdout}\n"
        f"stderr:\n{proc.stderr}"
    )


@pytest.mark.parametrize(
    ("frontend_backend", "gc_backend", "test_target"),
    tuple(_iter_cases()),
)
def test_gc_backend_subset_under_frontend_backend(
    frontend_backend: str | None,
    gc_backend: str,
    test_target: str | tuple[str, ...],
) -> None:
    _run_file_under_backends(frontend_backend, gc_backend, test_target)
