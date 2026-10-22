from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

from pcc.frontends.python import pipeline
from pcc.frontends.python.codegen.runtime_abi import FREESTANDING_GC_RUNTIME_GLOBALS


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"
STRICT_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_public_collection.py"
MANAGED_SOURCE = RUNTIME_DIR / "py" / "py_gc_backend.py"
MAKEFILE = RUNTIME_DIR / "Makefile"

OWNED_SYMBOLS = {
    "pcc_gc_is_cms_worker",
    "pcc_gc_begin_explicit_tracing_collect",
    "pcc_gc_collect_tracing",
    "pcc_gc_config_ensure",
    "pcc_gc_config_parse_env_i32",
    "pcc_gc_end_explicit_tracing_collect",
    "pcc_gc_has_tracing_sweep",
    "pcc_gc_maybe_start_cms_worker",
    "pcc_gc_cms_worker_main_py",
    "pcc_gc_config_abort_bad_backend",
    "pcc_gc_config_parse_backend",
}
OWNED_TLS_GLOBALS = {"pcc_gc_cms_worker_current"}
RAW_FUNCTION_IMPORTS = {
    # The collector thread begins a cycle once debt reaches the threshold
    # and marks in fixed batches.
    "pcc_gc_tracing_debt_threshold",
    # The collector thread discharges the debt it traced (d58306406).
    "pcc_gc_tracing_discharge_debt",
    "pcc_gc_tracing_has_sweep_candidate",
    "pcc_gc_tracing_step_cycle",
    "pcc_gc_tracing_sweep_unreachable",
    "pcc_platform_getenv",
    "pcc_platform_abort",
    "pcc_platform_write",
    "pcc_platform_sleep_ns",
    "pcc_resume_world",
    "pcc_stop_the_world",
    "pcc_threads_enabled",
    "pcc_thread_start",
    "pcc_thread_safepoint",
}
RAW_GLOBAL_IMPORTS = {
    "pcc_gc_backend_selected",
    "pcc_gc_refcount_fast",
    # GC0's inline atomic counts on every thread (b14e3f316).
    "pcc_gc_refcount_atomic",
    # The collector thread continues an open cycle (d58306406) and pauses
    # while gc.disable() holds.
    "pcc_gc_mark_active",
    "py_gc_enabled",
    # It pauses with threshold0 == 0 too, begins a cycle only once debt or
    # tracking growth is due, and has a mutator sweep each completed mark.
    "py_gc_threshold0",
    "py_gc_tracked_count",
    "pcc_gc_tracked_after_collect",
    "pcc_gc_debt_bytes",
    "pcc_gc_tracing_finish_commits",
    "pcc_gc_backend4_auto_step_pending",
    "pcc_gc_cms_worker_started",
    "pcc_gc_cms_worker_starts",
    "pcc_gc_cms_worker_handle",
    "pcc_gc_cms_worker_stop_requested",
    "pcc_gc_cms_queue_pushes",
    "pcc_gc_cms_worker_drains",
    "pcc_gc_cms_worker_stops",
    "pcc_gc_cms_worker_traces",
    "pcc_gc_config_initialized",
    "pcc_gc_cycle_requested",
    "pcc_gc_debt_threshold_override",
    "pcc_gc_explicit_collect_active",
    "pcc_gc_minor_alloc_max",
    "pcc_gc_minor_heap_size",
    "pcc_gc_pause",
    "pcc_gc_read_barrier_enabled",
    "pcc_gc_refcount_provenance_probe",
    "pcc_gc_stepmul",
}


def _exported_symbols(source: str) -> set[str]:
    return set(re.findall(r'@c_abi_export\("([^"]+)"\)', source))


def _literal_global_imports() -> set[str]:
    globals_: set[str] = set()
    tree = ast.parse(STRICT_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "global_addr" or not node.args:
            continue
        value = node.args[0]
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            globals_.add(value.value)
    return globals_ - OWNED_TLS_GLOBALS


def _compile_object(tmp_path: Path, emitter: str) -> Path:
    llvm_ir = tmp_path / ("freestanding_gc_public_collection_" + emitter + ".ll")
    pipeline.compile_python(
        str(STRICT_SOURCE),
        str(llvm_ir),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    source = llvm_ir
    if emitter == "self":
        from pcc.backend.self_backend_dispatch import emit_self_asm

        source = tmp_path / "freestanding_gc_public_collection.s"
        source.write_text(
            emit_self_asm(llvm_ir.read_text(encoding="utf-8")),
            encoding="utf-8",
        )
    obj = tmp_path / ("freestanding_gc_public_collection_" + emitter + ".o")
    result = subprocess.run(
        ["clang", "-c", str(source), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return obj


def _compile_llvm_object(tmp_path: Path, source_path: Path, stem: str) -> Path:
    llvm_ir = tmp_path / (stem + ".ll")
    pipeline.compile_python(
        str(source_path),
        str(llvm_ir),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    obj = tmp_path / (stem + ".o")
    result = subprocess.run(
        ["clang", "-c", str(llvm_ir), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return obj


def test_public_collection_has_one_strict_source_owner():
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    managed = MANAGED_SOURCE.read_text(encoding="utf-8")
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "__pcc_freestanding__ = True" in strict
    assert _exported_symbols(strict) == OWNED_SYMBOLS
    assert _exported_symbols(managed).isdisjoint(OWNED_SYMBOLS)
    assert "freestanding_gc_public_collection" in makefile
    assert '_init_config = extern("pcc_gc_config_ensure"' in managed
    assert '_maybe_start_cms_worker = extern("pcc_gc_maybe_start_cms_worker"' in managed
    for name in ("_parse_env_i32", "_init_config", "_maybe_start_cms_worker"):
        assert f"def {name}(" not in managed


def test_public_collection_preserves_config_and_collection_order():
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    worker = strict.split(
        '@c_abi_export("pcc_gc_cms_worker_main_py")', 1
    )[1].split("\n@c_abi_export", 1)[0]
    # The collector thread marks concurrently with mutators (d58306406): only
    # the step's root seeding and final remark stop the world, inside the
    # step. It traces in batches of 512, discharges the debt it traced,
    # polls every round and is paused by gc.disable() and threshold0 == 0.
    # A cycle begins only when due (the debt threshold or tracking growth)
    # and not while a completed mark waits for the sweep a mutator's
    # application gate runs; a completed mark requests that sweep.
    assert "pcc_stop_the_world()" not in worker
    assert "pcc_resume_world()" not in worker
    worker_enabled = worker.index('global_addr("py_gc_enabled")')
    worker_threshold0 = worker.index('global_addr("py_gc_threshold0")')
    worker_pending = worker.index('atomic_load_i32(pending, 0, "acquire") == 0')
    worker_due = worker.index("pcc_gc_tracing_debt_threshold()")
    worker_growth = worker.index('global_addr("py_gc_tracked_count")')
    worker_trace = worker.index("pcc_gc_tracing_step_cycle(512)")
    worker_discharge = worker.index("pcc_gc_tracing_discharge_debt(traced)")
    worker_sweep = worker.index('atomic_store_i32(pending, 0, 1, "release")')
    assert max(worker_enabled, worker_threshold0) < worker_pending
    assert worker_pending < worker_due < worker_growth < worker_trace
    assert worker_trace < worker_discharge < worker_sweep
    assert "pcc_thread_safepoint()" in worker

    start = strict.split(
        '@c_abi_export("pcc_gc_maybe_start_cms_worker")', 1
    )[1].split("\n@c_abi_export", 1)[0]
    assert 'function_addr("pcc_gc_cms_worker_main_py")' in start
    assert start.index("pcc_thread_start(") < start.index(
        'global_addr("pcc_gc_cms_worker_starts")'
    )

    config = strict.split('@c_abi_export("pcc_gc_config_ensure")', 1)[1].split(
        "\n@c_abi_export", 1
    )[0]
    for env_name in (
        "PCC_GC_BACKEND",
        "PCC_GC_PAUSE",
        "PCC_GC_STEPMUL",
        "PCC_GC_STEP_MUL",
        "PCC_GC_DEBT_THRESHOLD",
        "PCC_GC_MINOR_HEAP_SIZE",
        "PCC_GC_MINOR_ALLOC_MAX",
        "PCC_GC_REFCOUNT_PROVENANCE_PROBE",
    ):
        assert env_name in config
    assert "33554432, 256, 1099511627776" in config
    assert "16, 16, 1073741824" in config
    assert config.index('store_i32(global_addr("pcc_gc_backend_selected")') < (
        config.index("pcc_gc_maybe_start_cms_worker()")
    )

    collect = strict.split('@c_abi_export("pcc_gc_collect_tracing")', 1)[1].split(
        "\n@c_abi_export", 1
    )[0]
    full_sweep = "pcc_gc_tracing_sweep_unreachable(9223372036854775807)"
    assert collect.index("pcc_stop_the_world()") < collect.index(full_sweep)
    assert collect.index(full_sweep) < collect.index("pcc_resume_world()")
    assert "pcc_gc_tracing_sweep_unreachable(1024)" not in collect

    begin = strict.split(
        '@c_abi_export("pcc_gc_begin_explicit_tracing_collect")', 1
    )[1].split("\n@c_abi_export", 1)[0]
    assert begin.index('store_i32(global_addr("pcc_gc_explicit_collect_active")') < (
        begin.index('store_i32(global_addr("pcc_gc_cycle_requested")')
    )


@pytest.mark.parametrize("emitter", ["llvm", "self"])
def test_public_collection_object_has_exact_raw_closure(
    tmp_path: Path, emitter: str
):
    obj = _compile_object(tmp_path, emitter)
    assert _literal_global_imports() == RAW_GLOBAL_IMPORTS
    assert RAW_GLOBAL_IMPORTS <= FREESTANDING_GC_RUNTIME_GLOBALS
    undefined_result = subprocess.run(
        ["nm", "-u", str(obj)], capture_output=True, text=True, timeout=30
    )
    assert undefined_result.returncode == 0, (
        undefined_result.stdout + undefined_result.stderr
    )
    undefined = {
        line.split()[-1].lstrip("_")
        for line in undefined_result.stdout.splitlines()
        if line.strip()
    }
    expected_undefined = RAW_FUNCTION_IMPORTS | RAW_GLOBAL_IMPORTS
    # The collector thread's TLS marker (bcdd222c2) is a thread local.
    if sys.platform == "darwin":
        expected_undefined = expected_undefined | {"tlv_bootstrap"}
    assert undefined == expected_undefined

    symbols_result = subprocess.run(
        ["nm", "-g", str(obj)], capture_output=True, text=True, timeout=30
    )
    assert symbols_result.returncode == 0, symbols_result.stdout + symbols_result.stderr
    defined = {
        line.split()[-1].lstrip("_")
        for line in symbols_result.stdout.splitlines()
        if line.strip() and " U " not in line
    }
    assert defined == OWNED_SYMBOLS | OWNED_TLS_GLOBALS


def test_pcc_python_cms_worker_starts_drains_and_joins(tmp_path: Path) -> None:
    public_obj = _compile_object(tmp_path, "llvm")
    scheduler_obj = _compile_llvm_object(
        tmp_path,
        RUNTIME_DIR / "py" / "freestanding_gc_incremental_concurrent_scheduler.py",
        "freestanding_gc_incremental_concurrent_scheduler",
    )
    state_obj = _compile_llvm_object(
        tmp_path,
        RUNTIME_DIR / "py" / "freestanding_gc_state.py",
        "freestanding_gc_state",
    )
    harness = tmp_path / "cms_worker.c"
    harness.write_text(
        r'''
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

typedef struct WorkerHandle { pthread_t thread; } WorkerHandle;

extern void pcc_gc_maybe_start_cms_worker(void);
extern void pcc_gc_cms_note_alloc(int64_t bytes);
extern int64_t pcc_gc_cms_stop_worker(void);
extern int32_t pcc_gc_backend_selected;
extern int32_t pcc_gc_config_initialized;
extern int32_t pcc_gc_cycle_requested;
extern int32_t pcc_gc_mark_active;
extern int64_t pcc_gc_tracing_cycle_epoch;
extern int64_t pcc_gc_tracing_finish_claim_epoch;
extern int64_t pcc_gc_tracing_finish_claim_backend;
extern int64_t pcc_gc_tracing_finish_commits;
extern int32_t pcc_gc_backend4_auto_step_pending;
extern int32_t pcc_gc_cms_worker_started;
extern int32_t pcc_gc_cms_worker_starts;
extern int32_t pcc_gc_cms_worker_stops;
extern int32_t pcc_gc_cms_worker_drains;
extern void *pcc_gc_cms_worker_handle;
extern int32_t pcc_gc_trace_extension_roots_pending;
extern int64_t pcc_gc_trace_extension_roots_epoch;
extern int64_t pcc_gc_trace_extension_roots_backend;

void *pcc_platform_getenv(void *name) { (void)name; return NULL; }
int64_t pcc_platform_write(int64_t fd, void *data, int64_t length) {
    (void)fd;
    (void)data;
    return length;
}
void pcc_platform_abort(void) { abort(); }
int64_t pcc_threads_enabled(void) { return 1; }
int64_t pcc_thread_start(void **out, void *entry, void *arg) {
    WorkerHandle *handle = (WorkerHandle *)calloc(1, sizeof(*handle));
    if (handle == NULL) return -1;
    int rc = pthread_create(
        &handle->thread, NULL, (void *(*)(void *))entry, arg
    );
    if (rc != 0) { free(handle); return -1; }
    *out = handle;
    return 0;
}
int64_t pcc_thread_join(void *raw, void *result_out) {
    (void)result_out;
    WorkerHandle *handle = (WorkerHandle *)raw;
    if (handle == NULL) return -1;
    int rc = pthread_join(handle->thread, NULL);
    free(handle);
    return rc;
}
void pcc_thread_safepoint(void) {}
int64_t pcc_platform_sleep_ns(int64_t ns) {
    struct timespec delay = { ns / 1000000000, ns % 1000000000 };
    return nanosleep(&delay, NULL);
}
int64_t pcc_platform_monotonic_us(void) {
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) return 0;
    return (int64_t)now.tv_sec * 1000000 + now.tv_nsec / 1000;
}
static _Thread_local int64_t world_owned;
static int64_t stop_calls;
static int64_t resume_calls;
int64_t pcc_stop_the_world(void) {
    if (world_owned != 0) return -1;
    world_owned = 1;
    stop_calls++;
    return 0;
}
int64_t pcc_resume_world(void) {
    if (world_owned == 0) return -1;
    world_owned = 0;
    resume_calls++;
    return 0;
}
int64_t pcc_thread_owns_stopped_world(void) { return world_owned; }
int64_t pcc_gc_tracing_has_sweep_candidate(void) { return 0; }
int64_t pcc_gc_tracing_sweep_unreachable(int64_t budget) {
    (void)budget;
    return 0;
}
void pcc_py_gc_minor_graph_lock(void) {}
void pcc_py_gc_minor_graph_unlock(void) {}
void pcc_gc_begin_mark_cycle(void) {
    /* freestanding_gc_common_mark_cycle.py: advance the epoch and stage the
     * extension-root seed; pcc_gc_complete_mark_cycle_seed activates marking. */
    if (pcc_gc_trace_extension_roots_pending != 0) return;
    pcc_gc_tracing_cycle_epoch++;
    pcc_gc_trace_extension_roots_epoch = pcc_gc_tracing_cycle_epoch;
    pcc_gc_trace_extension_roots_backend = pcc_gc_backend_selected;
    pcc_gc_trace_extension_roots_pending = 4;
}
void pcc_gc_tracing_finish_claim_clear_unlocked(
    int64_t claim_epoch, int64_t claim_backend
) {
    if (
        pcc_gc_tracing_finish_claim_epoch == claim_epoch
        && pcc_gc_tracing_finish_claim_backend == claim_backend
    ) {
        pcc_gc_tracing_finish_claim_epoch = 0;
        pcc_gc_tracing_finish_claim_backend = -1;
    }
}
int64_t pcc_gc_finish_tracing_cycle(
    int64_t claim_epoch, int64_t claim_backend
) {
    if (
        pcc_gc_tracing_finish_claim_epoch != claim_epoch
        || pcc_gc_tracing_finish_claim_backend != claim_backend
        || pcc_gc_tracing_cycle_epoch != claim_epoch
        || pcc_gc_backend_selected != claim_backend
    ) return 0;
    pcc_gc_mark_active = 0;
    pcc_gc_tracing_finish_commits++;
    pcc_gc_tracing_finish_claim_clear_unlocked(claim_epoch, claim_backend);
    return 1;
}
void pcc_gc_trace_referents(void *obj) { (void)obj; }
int64_t pcc_gc_gray_count_load_acquire(void) { return 0; }
void pcc_gc_gray_count_decrement_acq_rel(void) {}
void pcc_gc_gray_count_store_release(int64_t value) { (void)value; }
/* The scheduler object also links its root seeding, extension tracing and
 * GC4 automatic-step entry points; with no heap they do nothing. */
void pcc_gc_seed_roots(void) {}
void pcc_gc_gray_current_roots(void) {}
int64_t pcc_gc_drain_all_gray_locked_slice(void) { return 0; }
int64_t pcc_capi_is_cext_type_tag(int64_t tag) { (void)tag; return 0; }
int64_t pcc_gc_object_is_known_no_lock(void *obj) { (void)obj; return 0; }
void pcc_gc_trace_mark_gray_if_known(void *obj) { (void)obj; }
void pcc_gc_trace_cext_referents_unlocked(void *obj, void *ctx) {
    (void)obj;
    (void)ctx;
}
void pcc_capi_visit_extension_module_state_roots(void *visit, void *ctx) {
    (void)visit;
    (void)ctx;
}
int64_t pcc_gc_step(int64_t budget) { (void)budget; return 0; }
void pcc_gc_set_mark_color(void *obj, int64_t color) {
    (void)obj;
    (void)color;
}
int64_t pcc_py_gc_minor_graph_lock_depth(void) { return 0; }
int64_t pcc_thread_no_park_depth(void) { return 0; }
void py_incref(void *obj) { (void)obj; }
void py_decref(void *obj) { (void)obj; }

int main(void) {
    pcc_gc_backend_selected = 2;
    pcc_gc_config_initialized = 1;
    pcc_gc_cycle_requested = 1;
    pcc_gc_maybe_start_cms_worker();
    if (
        pcc_gc_cms_worker_started != 1
        || pcc_gc_cms_worker_starts != 1
        || pcc_gc_cms_worker_handle == NULL
    ) return 10;
    /* Paced: 64 bytes of debt and no tracking growth begin no cycle. The
     * worker publishes the drain after deciding. */
    pcc_gc_cms_note_alloc(64);
    for (
        int i = 0;
        i < 1000
            && __atomic_load_n(
                &pcc_gc_cms_worker_drains, __ATOMIC_ACQUIRE
            ) == 0;
        i++
    ) {
        pcc_platform_sleep_ns(1000000);
    }
    if (
        __atomic_load_n(&pcc_gc_cms_worker_drains, __ATOMIC_ACQUIRE)
        == 0
    ) return 11;
    if (
        __atomic_load_n(&pcc_gc_tracing_finish_commits, __ATOMIC_ACQUIRE)
        != 0
    ) return 15;
    /* Debt at the pacing threshold (no live bytes: 65536) begins one; the
     * completed mark asks the next application gate to sweep it. */
    pcc_gc_cms_note_alloc(65536);
    for (
        int i = 0;
        i < 1000
            && __atomic_load_n(
                &pcc_gc_backend4_auto_step_pending, __ATOMIC_ACQUIRE
            ) == 0;
        i++
    ) {
        pcc_platform_sleep_ns(1000000);
    }
    if (
        __atomic_load_n(&pcc_gc_backend4_auto_step_pending, __ATOMIC_ACQUIRE)
        != 1
    ) return 16;
    if (pcc_gc_cms_stop_worker() != 0) return 14;
    if (
        pcc_gc_tracing_finish_commits != 1
        || stop_calls != resume_calls
        || pcc_gc_tracing_finish_claim_epoch != 0
        || pcc_gc_tracing_finish_claim_backend != -1
    ) return 13;
    if (
        pcc_gc_cms_worker_started != 4
        || pcc_gc_cms_worker_handle != NULL
        || pcc_gc_cms_worker_stops != 1
    ) return 12;
    puts("cms-worker:ok");
    return 0;
}
''',
        encoding="utf-8",
    )
    executable = tmp_path / "cms_worker"
    build = subprocess.run(
        [
            "clang",
            "-std=c11",
            "-pthread",
            str(harness),
            str(public_obj),
            str(scheduler_obj),
            str(state_obj),
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=10
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout == "cms-worker:ok\n"


def test_production_archive_has_one_public_collection_owner(
    pcc_runtime_archive: Path,
):
    symbols_result = subprocess.run(
        ["nm", "-A", "-g", str(pcc_runtime_archive)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert symbols_result.returncode == 0, symbols_result.stdout + symbols_result.stderr
    for symbol in OWNED_SYMBOLS:
        owners = [
            line
            for line in symbols_result.stdout.splitlines()
            if line.strip()
            and line.split()[-1].lstrip("_") == symbol
            and " U " not in line
        ]
        assert len(owners) == 1, (symbol, owners)
        assert ":freestanding_gc_public_collection.o:" in owners[0]
        assert ":py_gc_backend.o:" not in owners[0]
