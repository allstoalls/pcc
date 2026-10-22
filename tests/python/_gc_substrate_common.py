"""Shared helpers and constants for the gc-threading-substrate split.

Consumed via star-import by the gcsubstrate_* modules; re-exported by
the ``test_gc_threading_substrate`` facade so external importers and
pytest node ids stay unchanged.
"""
from pathlib import Path
import os
import signal
import subprocess
import textwrap

import pytest

from pcc.frontends.python.codegen.runtime_abi import (
    FREESTANDING_GC_CROSS_OBJECT_SIGNATURES,
    FREESTANDING_GC_I64_GLOBALS,
    RUNTIME_SIGNATURES,
)
from tests.runtime_build_cache import (
    cached_pcc_python_runtime,
    cached_threaded_pcc_python_runtime,
)


REPO_ROOT = Path(__file__).absolute().parents[2]
RUNTIME_HEADER = REPO_ROOT / "pcc" / "runtime" / "include" / "py_runtime.h"
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"
RUNTIME_MAKEFILE = REPO_ROOT / "pcc" / "runtime" / "Makefile"
PY_OBJ_PORT = REPO_ROOT / "pcc" / "runtime" / "py" / "py_obj.py"
THREAD_KERNEL = (
    REPO_ROOT / "pcc" / "runtime" / "py" / "freestanding_thread_kernel.py"
)
THREAD_KERNEL_PTHREAD = (
    REPO_ROOT
    / "pcc" / "runtime"
    / "py"
    / "freestanding_thread_kernel_pthread.py"
)
RUNTIME_LOG_PORT = REPO_ROOT / "pcc" / "runtime" / "py" / "py_runtime_log.py"
PY_OBJ_GC_PORT = (
    REPO_ROOT
    / "pcc" / "runtime"
    / "py"
    / "freestanding_gc_backend0_collector.py"
)
PY_GC_BACKEND_PORT = REPO_ROOT / "pcc" / "runtime" / "py" / "py_gc_backend.py"
PY_GC_BARRIER_DISPATCHER = (
    REPO_ROOT
    / "pcc" / "runtime"
    / "py"
    / "freestanding_gc_barrier_dispatcher.py"
)
PY_GC_GENERATIONAL_SCHEDULER = (
    REPO_ROOT
    / "pcc" / "runtime"
    / "py"
    / "freestanding_gc_generational_scheduler.py"
)
PY_GC_GENERATIONAL_PROMOTION = (
    REPO_ROOT
    / "pcc" / "runtime"
    / "py"
    / "freestanding_gc_generational_promotion.py"
)
PY_GC_OBJECT_SLOTS = (
    REPO_ROOT
    / "pcc" / "runtime"
    / "py"
    / "freestanding_gc_object_slots.py"
)
PY_GC_RELOCATION_SELECTOR = (
    REPO_ROOT
    / "pcc" / "runtime"
    / "py"
    / "freestanding_gc_relocation_selector.py"
)
PY_GC_STATE = (
    REPO_ROOT / "pcc" / "runtime" / "py" / "freestanding_gc_state.py"
)
PY_GC_COMMON_MARK_CYCLE = (
    REPO_ROOT
    / "pcc" / "runtime"
    / "py"
    / "freestanding_gc_common_mark_cycle.py"
)
PY_GC_INCREMENTAL_CONCURRENT_SCHEDULER = (
    REPO_ROOT
    / "pcc" / "runtime"
    / "py"
    / "freestanding_gc_incremental_concurrent_scheduler.py"
)
CORE_HELPERS = REPO_ROOT / "pcc" / "frontends" / "python" / "codegen" / "core_helpers.py"
CONTROL_FLOW_LOWERING = (
    REPO_ROOT / "pcc" / "frontends" / "python" / "codegen" / "control_flow_lowering.py"
)
FOR_LOOP_LOWERING = (
    REPO_ROOT / "pcc" / "frontends" / "python" / "codegen" / "for_loop_lowering.py"
)
USER_FUNCTION_LOWERING = (
    REPO_ROOT / "pcc" / "frontends" / "python" / "codegen" / "user_function_lowering.py"
)


def _build_threaded_runtime(tmp_path: Path) -> Path:
    del tmp_path
    return cached_threaded_pcc_python_runtime()


def _runtime_variant(kind: str, *, threaded: bool) -> tuple[Path, Path]:
    runtime = (
        cached_threaded_pcc_python_runtime()
        if threaded
        else cached_pcc_python_runtime()
    )
    return runtime, runtime / "libpy_runtime_pcc_py.a"


def _compile_runtime_probe(
    tmp_path: Path,
    *,
    kind: str,
    threaded: bool,
    stem: str,
    source_text: str,
    extra_include_dirs: tuple[Path, ...] = (),
    extra_sources: tuple[Path, ...] = (),
    extra_compile_args: tuple[str, ...] = (),
) -> Path:
    runtime, archive = _runtime_variant(kind, threaded=threaded)
    source = tmp_path / f"{stem}_{kind}.c"
    executable = tmp_path / f"{stem}_{kind}"
    source.write_text(textwrap.dedent(source_text).lstrip(), encoding="utf-8")
    command = [
        os.environ.get("CC", "cc"),
        "-std=c11",
    ]
    if threaded:
        command.append("-pthread")
    command.extend(extra_compile_args)
    for include_dir in extra_include_dirs:
        command.append(f"-I{include_dir}")
    command.extend(str(extra_source) for extra_source in extra_sources)
    command.extend(
        [
            f"-I{runtime / 'include'}",
            f"-I{runtime / 'src'}",
            str(source),
            str(archive),
            "-lm",
            "-o",
            str(executable),
        ]
    )
    build = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert build.returncode == 0, build.stdout + build.stderr
    return executable


THREADING_SURFACE = [
    "pcc_threads_enabled",
    "pcc_current_thread_id",
    "pcc_current_native_thread_token",
    "pcc_refcount_strategy",
    "pcc_thread_safepoint",
    "pcc_thread_stop_requested_acquire",
    "pcc_thread_no_park_enter",
    "pcc_thread_no_park_exit",
    "pcc_thread_no_park_depth",
    "pcc_thread_owns_stopped_world",
    "pcc_thread_registration_waiter_count",
    "pcc_thread_unregister_current",
    "pcc_stop_the_world",
    "pcc_resume_world",
]

__all__ = [
    "Path",
    "os",
    "signal",
    "subprocess",
    "textwrap",
    "pytest",
    "FREESTANDING_GC_CROSS_OBJECT_SIGNATURES",
    "FREESTANDING_GC_I64_GLOBALS",
    "RUNTIME_SIGNATURES",
    "cached_pcc_python_runtime",
    "cached_threaded_pcc_python_runtime",
    "cached_threaded_pcc_python_runtime",
    "REPO_ROOT",
    "RUNTIME_HEADER",
    "RUNTIME_DIR",
    "RUNTIME_MAKEFILE",
    "PY_OBJ_PORT",
    "THREAD_KERNEL",
    "THREAD_KERNEL_PTHREAD",
    "RUNTIME_LOG_PORT",
    "PY_OBJ_GC_PORT",
    "PY_GC_BACKEND_PORT",
    "PY_GC_BARRIER_DISPATCHER",
    "PY_GC_GENERATIONAL_SCHEDULER",
    "PY_GC_GENERATIONAL_PROMOTION",
    "PY_GC_OBJECT_SLOTS",
    "PY_GC_RELOCATION_SELECTOR",
    "PY_GC_STATE",
    "PY_GC_COMMON_MARK_CYCLE",
    "PY_GC_INCREMENTAL_CONCURRENT_SCHEDULER",
    "CORE_HELPERS",
    "CONTROL_FLOW_LOWERING",
    "FOR_LOOP_LOWERING",
    "USER_FUNCTION_LOWERING",
    "_build_threaded_runtime",
    "_runtime_variant",
    "_compile_runtime_probe",
    "THREADING_SURFACE",
    "ALL_GC_KINDS",
    "TRACER_RACE_GC_KINDS",
]

ALL_GC_KINDS = (
    "PCC_GC_KIND_REFCOUNT_CYCLE",
    "PCC_GC_KIND_INCREMENTAL_TRICOLOR",
    "PCC_GC_KIND_CONCURRENT_MARK_SWEEP",
    "PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR",
    "PCC_GC_KIND_COLORED_RELOCATING",
)

TRACER_RACE_GC_KINDS = tuple(
    kind
    for kind in ALL_GC_KINDS
    if kind != "PCC_GC_KIND_COLORED_RELOCATING"
)
