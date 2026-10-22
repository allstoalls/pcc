from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from pcc.frontends.python import pipeline
from pcc.frontends.python.pipeline_targets import host_target_triple
from pcc.frontends.python.codegen.runtime_abi import (
    FREESTANDING_GC_CROSS_OBJECT_SIGNATURES,
    FREESTANDING_GC_RUNTIME_GLOBALS,
    FREESTANDING_GC_THREAD_LOCAL_GLOBALS,
    RUNTIME_SIGNATURES,
)
from tests.runtime_build_cache import cached_threaded_pcc_python_runtime


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"
FRAME_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_frame_registry.py"
MANAGED_SOURCE = RUNTIME_DIR / "py" / "py_gc_backend.py"
WRAPPER_SOURCE = RUNTIME_DIR / "py" / "py_obj.py"
MAKEFILE = RUNTIME_DIR / "Makefile"
STATE_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_state.py"

PUBLIC_SYMBOLS = {
    "pcc_gc_note_frame_enter",
    "pcc_gc_note_frame_enter_lifo",
    "pcc_gc_note_frame_leave",
    "pcc_gc_note_frame_leave_lifo",
}
INTERNAL_SYMBOLS = {
    "pcc_gc_frame_node_alloc",
    "pcc_gc_frame_node_bucket",
    "pcc_gc_frame_node_create",
    "pcc_gc_frame_node_pool_counts_get",
    "pcc_gc_frame_node_pool_heads_get",
    "pcc_gc_frame_node_release",
    "pcc_gc_frame_node_size",
    "pcc_gc_frame_node_tls_pool_cached_count",
    "pcc_gc_frame_node_tls_pool_drain",
    "pcc_gc_frame_node_unlink",
    "pcc_gc_frame_roots_disabled_fast",
    "pcc_gc_should_track_frame_roots",
}
TLS_STORAGE_SYMBOLS = {
    "pcc_gc_frame_node_pool_counts",
    "pcc_gc_frame_node_pool_heads",
    "pcc_gc_frame_node_pool_total",
}
RAW_ONLY_CROSS_OBJECT_SYMBOLS = {
    "pcc_gc_index_slot_size",
    "pcc_gc_cycle_requested_store_release",
    "pcc_gc_frame_index_find",
    "pcc_gc_frame_index_plan_capacity",
    "pcc_gc_frame_index_plan_commit",
    "pcc_gc_frame_index_remove",
    "pcc_gc_frame_index_replace_preallocated",
    "pcc_gc_root_map_is_borrowed",
    "pcc_gc_root_registry_note_mutation_locked",
    "pcc_gc_root_slot_count_from_map",
}
RAW_FUNCTION_IMPORTS = RAW_ONLY_CROSS_OBJECT_SYMBOLS | {
    "free",
    "malloc",
    "memset",
    "pcc_gc_backend",
    "pcc_py_gc_minor_graph_lock",
    "pcc_py_gc_minor_graph_unlock",
}
RAW_GLOBAL_IMPORTS = {
    "pcc_gc_backend0_frame_roots_enabled",
    "pcc_gc_backend3_frame_root_scan_cursor",
    "pcc_gc_backend3_frame_root_scan_slot",
    "pcc_gc_backend_selected",
    "pcc_gc_config_initialized",
    "pcc_gc_explicit_collect_active",
    "pcc_gc_frame_head",
}


def _exported_symbols(source: str) -> set[str]:
    return set(re.findall(r'@c_abi_export\("([^"]+)"\)', source))


def _compile_object(tmp_path: Path, emitter: str) -> Path:
    llvm_ir = tmp_path / ("freestanding_gc_frame_registry_" + emitter + ".ll")
    pipeline.compile_python(
        str(FRAME_SOURCE),
        str(llvm_ir),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
        target_triple=host_target_triple(),
    )
    ir_text = llvm_ir.read_text(encoding="utf-8")
    source = llvm_ir
    if emitter == "self":
        from pcc.backend.self_backend_dispatch import emit_self_asm

        source = tmp_path / "freestanding_gc_frame_registry.s"
        source.write_text(emit_self_asm(ir_text), encoding="utf-8")
    obj = tmp_path / ("freestanding_gc_frame_registry_" + emitter + ".o")
    result = subprocess.run(
        ["clang", "-c", str(source), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return obj


def test_frame_registry_has_one_strict_source_owner():
    strict = FRAME_SOURCE.read_text(encoding="utf-8")
    managed = MANAGED_SOURCE.read_text(encoding="utf-8")
    wrappers = WRAPPER_SOURCE.read_text(encoding="utf-8")
    state = STATE_SOURCE.read_text(encoding="utf-8")
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "__pcc_freestanding__ = True" in strict
    assert "return 64 + root_count * 8" in strict
    assert _exported_symbols(strict) == PUBLIC_SYMBOLS | INTERNAL_SYMBOLS
    assert _exported_symbols(managed).isdisjoint(PUBLIC_SYMBOLS | INTERNAL_SYMBOLS)
    assert RAW_ONLY_CROSS_OBJECT_SYMBOLS.isdisjoint(RUNTIME_SIGNATURES)
    assert FREESTANDING_GC_CROSS_OBJECT_SIGNATURES[
        "pcc_gc_frame_node_tls_pool_drain"
    ] == ((), "c_void")
    assert FREESTANDING_GC_CROSS_OBJECT_SIGNATURES[
        "pcc_gc_frame_index_plan_capacity"
    ] == (("c_int64",), "c_int64")
    assert FREESTANDING_GC_CROSS_OBJECT_SIGNATURES[
        "pcc_gc_frame_index_plan_commit"
    ] == (("c_ptr", "c_int64", "c_int64"), "c_int64")
    assert FREESTANDING_GC_CROSS_OBJECT_SIGNATURES[
        "pcc_gc_frame_index_replace_preallocated"
    ] == (("c_ptr", "c_ptr"), "c_ptr")
    internal_header = (RUNTIME_DIR / "src" / "py_internal.h").read_text(
        encoding="utf-8"
    )
    for symbol in (
        "pcc_gc_frame_index_plan_capacity",
        "pcc_gc_frame_index_plan_commit",
        "pcc_gc_frame_index_replace_preallocated",
    ):
        assert symbol in internal_header
    assert FREESTANDING_GC_THREAD_LOCAL_GLOBALS == TLS_STORAGE_SYMBOLS
    assert 'define_thread_local_ptr_null("pcc_gc_frame_node_pool_heads")' in strict
    assert 'define_thread_local_ptr_null("pcc_gc_frame_node_pool_counts")' in strict
    assert 'define_thread_local_i32("pcc_gc_frame_node_pool_total", 0)' in strict
    for symbol in TLS_STORAGE_SYMBOLS:
        assert f'"{symbol}"' not in state
    unregister = managed.split(
        '@c_abi_export("pcc_gc_thread_unregister_buffers")', 1
    )[1].split("\n@c_abi_export", 1)[0]
    assert "pcc_gc_frame_node_tls_pool_drain()" in unregister
    assert "freestanding_gc_frame_registry" in makefile
    for symbol in PUBLIC_SYMBOLS:
        assert f'"{symbol}"' in wrappers


def test_frame_registry_allocator_and_release_tails_are_outside_graph_lock():
    strict = FRAME_SOURCE.read_text(encoding="utf-8")

    for name in ("pcc_gc_note_frame_enter", "pcc_gc_note_frame_enter_lifo"):
        strict_body = strict.split(f"def {name}", 1)[1].split("\n\n@", 1)[0]
        assert strict_body.index("pcc_gc_frame_node_create(") < strict_body.index(
            "pcc_py_gc_minor_graph_lock()"
        )

    for name in ("pcc_gc_note_frame_leave", "pcc_gc_note_frame_leave_lifo"):
        strict_body = strict.split(f"def {name}", 1)[1].split("\n\n@", 1)[0]
        assert strict_body.rindex("pcc_py_gc_minor_graph_unlock()") < (
            strict_body.rindex("pcc_gc_frame_node_release(")
        )

    strict_enter = strict.split("def pcc_gc_note_frame_enter", 1)[1].split(
        "\n\n@", 1
    )[0]
    assert "pcc_gc_frame_index_plan_capacity(1)" in strict_enter
    assert "pcc_gc_frame_index_plan_commit(" in strict_enter
    assert "pcc_gc_frame_index_replace_preallocated(" in strict_enter
    assert "pcc_gc_frame_index_replace(" not in strict_enter
    for forbidden in ("malloc(", "free("):
        start = 0
        while (position := strict_enter.find(forbidden, start)) >= 0:
            preceding_lock = strict_enter.rfind(
                "pcc_py_gc_minor_graph_lock()", 0, position
            )
            preceding_unlock = strict_enter.rfind(
                "pcc_py_gc_minor_graph_unlock()", 0, position
            )
            assert preceding_lock < preceding_unlock
            start = position + len(forbidden)


@pytest.mark.parametrize("emitter", ["llvm", "self"])
def test_frame_registry_object_has_exact_raw_closure(tmp_path: Path, emitter: str):
    obj = _compile_object(tmp_path, emitter)
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
    expected = PUBLIC_SYMBOLS | INTERNAL_SYMBOLS | TLS_STORAGE_SYMBOLS
    tls_initializers = {symbol + "$tlv$init" for symbol in TLS_STORAGE_SYMBOLS}
    assert expected <= defined
    assert defined <= expected | tls_initializers


def _frame_harness_source() -> str:
    return r'''
#include "py_runtime.h"
#include <stdint.h>
#include <stdio.h>

extern void *pcc_gc_frame_index_find(void *slots);
extern int64_t pcc_gc_frame_node_tls_pool_cached_count(void);
extern void pcc_gc_frame_node_tls_pool_drain(void);
extern int64_t pcc_gc_slot_is_runtime_root(PyObject **slot);

int main(void) {
    int64_t backend = pcc_gc_backend();
    if (pcc_gc_set_backend(backend) != 0) return 2;
    int pool_enabled = backend == 3 || backend == 4;

    int32_t owned_map[1] = {2};
    int32_t borrowed_map[1] = {-2};
    int32_t zero_map[1] = {0};
    int32_t min_map[1] = {(int32_t)0x80000000u};
    int32_t huge_map[1] = {100001};
    PyObject *slots[2] = {NULL, NULL};
    PyObject *other[2] = {NULL, NULL};

    pcc_gc_frame_enter(owned_map, slots);
    void *first = pcc_gc_frame_index_find(slots);
    pcc_gc_frame_enter(borrowed_map, slots);
    void *second = pcc_gc_frame_index_find(slots);
    printf("duplicate:%lld,%d,%d\n",
           (long long)pcc_gc_frame_root_slot_count(),
           first != NULL && second != NULL && first != second,
           pcc_gc_slot_is_runtime_root(slots));
    pcc_gc_frame_leave(slots);
    printf("duplicate-pop:%lld,%d\n",
           (long long)pcc_gc_frame_root_slot_count(),
           pcc_gc_frame_index_find(slots) == first);
    pcc_gc_frame_leave(slots);
    printf("duplicate-empty:%lld\n",
           (long long)pcc_gc_frame_root_slot_count());

    pcc_gc_frame_enter_lifo(owned_map, slots);
    pcc_gc_frame_enter_lifo(owned_map, other);
    printf("lifo:%lld\n", (long long)pcc_gc_frame_root_slot_count());
    pcc_gc_frame_leave_lifo(slots);
    printf("lifo-nonhead:%lld\n",
           (long long)pcc_gc_frame_root_slot_count());
    pcc_gc_frame_leave_lifo(other);
    printf("lifo-empty:%lld\n",
           (long long)pcc_gc_frame_root_slot_count());

    pcc_gc_frame_enter(zero_map, slots);
    pcc_gc_frame_enter(min_map, slots);
    pcc_gc_frame_enter(huge_map, slots);
    printf("invalid:%lld\n", (long long)pcc_gc_frame_root_slot_count());

    pcc_gc_frame_node_tls_pool_drain();
    int small_pool_ok = 1;
    int32_t small_map[1] = {0};
    PyObject *small_slots[16] = {NULL};
    for (int root_count = 0; root_count <= 16; root_count++) {
        small_map[0] = root_count;
        pcc_gc_frame_enter(small_map, small_slots);
        pcc_gc_frame_leave(small_slots);
        int64_t expected = pool_enabled && root_count > 0 ? 1 : 0;
        if (pcc_gc_frame_node_tls_pool_cached_count() != expected) {
            small_pool_ok = 0;
        }
        pcc_gc_frame_node_tls_pool_drain();
    }
    printf("pool-small:%d\n", small_pool_ok);
    if (!small_pool_ok) return 10;

    int32_t wide_map[1] = {17};
    PyObject *wide_slots[17] = {NULL};
    pcc_gc_frame_enter(wide_map, wide_slots);
    pcc_gc_frame_leave(wide_slots);
    int64_t large_cached = pcc_gc_frame_node_tls_pool_cached_count();
    printf("pool-large:%lld\n", (long long)large_cached);
    if (large_cached != 0) return 11;

    enum { POOL_LIMIT = 1024, POOL_PROBE_FRAMES = 1031 };
    int32_t cap_maps[POOL_PROBE_FRAMES][1];
    PyObject *cap_slots[POOL_PROBE_FRAMES][16] = {{NULL}};
    for (int i = 0; i < POOL_PROBE_FRAMES; i++) {
        cap_maps[i][0] = (i % 16) + 1;
        pcc_gc_frame_enter(cap_maps[i], cap_slots[i]);
    }
    for (int i = 0; i < POOL_PROBE_FRAMES; i++) {
        pcc_gc_frame_leave(cap_slots[i]);
    }
    int64_t cap_cached = pcc_gc_frame_node_tls_pool_cached_count();
    printf("pool-cap:%lld\n", (long long)cap_cached);
    if (cap_cached != (pool_enabled ? POOL_LIMIT : 0)) return 12;
    pcc_gc_frame_node_tls_pool_drain();
    int64_t drain_cached = pcc_gc_frame_node_tls_pool_cached_count();
    printf("pool-drain:%lld\n", (long long)drain_cached);
    if (drain_cached != 0) return 13;
    return 0;
}
'''


def _thread_harness_source() -> str:
    return r'''
#include "py_runtime.h"
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>

extern int64_t pcc_gc_frame_node_tls_pool_cached_count(void);
extern void pcc_gc_thread_unregister_buffers(void);

enum { THREADS = 4, ROUNDS = 512 };
static int32_t maps[THREADS][1] = {{-2}, {-2}, {-2}, {-2}};
static PyObject *slots[THREADS][2];
static pthread_mutex_t pool_gate_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t pool_gate_cond = PTHREAD_COND_INITIALIZER;
static int pool_gate_arrived = 0;

static void pool_gate_wait(void) {
    pthread_mutex_lock(&pool_gate_lock);
    pool_gate_arrived++;
    if (pool_gate_arrived == THREADS) {
        pthread_cond_broadcast(&pool_gate_cond);
    }
    while (pool_gate_arrived < THREADS) {
        pthread_cond_wait(&pool_gate_cond, &pool_gate_lock);
    }
    pthread_mutex_unlock(&pool_gate_lock);
}

static void *mutate(void *raw) {
    intptr_t index = (intptr_t)raw;
    for (int i = 0; i < ROUNDS; i++) {
        pcc_gc_frame_enter(maps[index], slots[index]);
        pcc_gc_frame_leave(slots[index]);
    }
    int32_t extra_maps[THREADS][1];
    PyObject *extra_slots[THREADS][2] = {{NULL}};
    for (int i = 0; i <= index; i++) {
        extra_maps[i][0] = 2;
        pcc_gc_frame_enter(extra_maps[i], extra_slots[i]);
    }
    for (int i = 0; i <= index; i++) {
        pcc_gc_frame_leave(extra_slots[i]);
    }
    pool_gate_wait();
    int64_t backend = pcc_gc_backend();
    int64_t expected = (backend == 3 || backend == 4) ? index + 1 : 0;
    if (pcc_gc_frame_node_tls_pool_cached_count() != expected) {
        return (void *)(intptr_t)101;
    }
    pcc_gc_thread_unregister_buffers();
    if (pcc_gc_frame_node_tls_pool_cached_count() != 0) {
        return (void *)(intptr_t)102;
    }
    return NULL;
}

static void *observe(void *raw) {
    (void)raw;
    /* Worker i holds up to i + 1 two-slot frames at once in its extra-frame
     * phase, so every worker there together is 2 * (1 + ... + THREADS). */
    for (int i = 0; i < THREADS * ROUNDS; i++) {
        int64_t count = pcc_gc_frame_root_slot_count();
        if (count < 0 || count > THREADS * (THREADS + 1)) return (void *)1;
    }
    return NULL;
}

int main(void) {
    int64_t backend = pcc_gc_backend();
    if (pcc_gc_set_backend(backend) != 0) return 2;
    pthread_t workers[THREADS];
    pthread_t observer;
    for (intptr_t i = 0; i < THREADS; i++) {
        if (pthread_create(&workers[i], NULL, mutate, (void *)i) != 0) return 3;
    }
    if (pthread_create(&observer, NULL, observe, NULL) != 0) return 4;
    for (int i = 0; i < THREADS; i++) {
        void *worker_result = NULL;
        pthread_join(workers[i], &worker_result);
        if (worker_result != NULL) return (int)(intptr_t)worker_result;
    }
    void *result = NULL;
    pthread_join(observer, &result);
    if (result != NULL) return 5;
    printf("final:%lld\n", (long long)pcc_gc_frame_root_slot_count());
    return 0;
}
'''


def _link_harness(tmp_path: Path, name: str, source_text: str, archive: Path) -> Path:
    source = tmp_path / (name + ".c")
    executable = tmp_path / name
    source.write_text(source_text, encoding="utf-8")
    result = subprocess.run(
        [
            "clang",
            "-std=c11",
            f"-I{RUNTIME_DIR / 'include'}",
            str(source),
            str(archive),
            "-pthread",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return executable


def _run_ok(implementation: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        [str(implementation)], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def _frame_expected(pool_cap: int) -> str:
    # What the harness printed against the retired C runtime oracle; GC3/GC4
    # keep a thread-local pool of 1024 cached frame nodes.
    return (
        "duplicate:4,1,1\n"
        "duplicate-pop:2,1\n"
        "duplicate-empty:0\n"
        "lifo:4\n"
        "lifo-nonhead:2\n"
        "lifo-empty:0\n"
        "invalid:0\n"
        "pool-small:1\n"
        "pool-large:0\n"
        f"pool-cap:{pool_cap}\n"
        "pool-drain:0\n"
    )


def test_archive_owns_frame_registry_gc0_to_gc4(
    tmp_path: Path,
    pcc_runtime_archive: Path,
):
    symbols_result = subprocess.run(
        ["nm", "-A", "-g", str(pcc_runtime_archive)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert symbols_result.returncode == 0, symbols_result.stdout + symbols_result.stderr
    owners: dict[str, list[str]] = {
        symbol: [] for symbol in PUBLIC_SYMBOLS | INTERNAL_SYMBOLS
    }
    for line in symbols_result.stdout.splitlines():
        symbol = line.split()[-1].lstrip("_") if line.strip() else ""
        if symbol in owners and " U " not in line:
            owners[symbol].append(line)
    assert all(len(lines) == 1 for lines in owners.values())
    assert all(
        ":freestanding_gc_frame_registry.o:" in lines[0]
        for lines in owners.values()
    )

    implementation = _link_harness(
        tmp_path,
        "frame_registry_pcc_python",
        _frame_harness_source(),
        pcc_runtime_archive,
    )
    for backend in range(5):
        output = _run_ok(implementation, {**os.environ, "PCC_GC_BACKEND": str(backend)})
        assert output == _frame_expected(1024 if backend in {3, 4} else 0)


def test_frame_registry_survives_threaded_mutation_and_observation(
    tmp_path: Path,
):
    threaded_pcc_python_archive = (
        cached_threaded_pcc_python_runtime() / "libpy_runtime_pcc_py.a"
    )
    implementation = _link_harness(
        tmp_path,
        "frame_registry_threads_pcc_python",
        _thread_harness_source(),
        threaded_pcc_python_archive,
    )
    for backend in range(5):
        output = _run_ok(implementation, {**os.environ, "PCC_GC_BACKEND": str(backend)})
        assert output == "final:0\n"
