from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from pcc.frontends.python import pipeline
from pcc.frontends.python.codegen.runtime_abi import FREESTANDING_GC_RUNTIME_GLOBALS


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"
TRACKING_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_tracking.py"
MANAGED_SOURCE = RUNTIME_DIR / "py" / "py_obj_gc.py"
COLLECTOR_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_backend0_collector.py"
MAKEFILE = RUNTIME_DIR / "Makefile"

PUBLIC_SYMBOLS = {"py_gc_track", "py_gc_untrack"}
INTERNAL_SYMBOLS = {
    "pcc_gc_default_drain_deferred_nodes",
    "pcc_gc_default_table_lock",
    "pcc_gc_default_table_unlock",
    "pcc_gc_default_unlink_tracked_node",
    "pcc_gc_tracked_node_pool_cached_count",
    "pcc_gc_tracked_node_pool_drain",
    "pcc_gc_tracked_node_recycle",
}
RAW_FUNCTION_IMPORTS = {
    "free",
    "malloc",
    "pcc_current_native_thread_token",
    "pcc_gc_backend",
    "pcc_thread_safepoint",
    "pcc_threads_enabled",
    "py_gc_index_insert",
    "py_gc_index_remove",
}
RAW_GLOBAL_IMPORTS = {
    # py_gc_track schedules GC0's automatic cycle collection.
    "pcc_gc_tracked_after_collect",
    "pcc_gc_backend4_auto_step_pending",
    "py_gc_enabled",
    "py_gc_threshold0",
    "pcc_gc_deferred_node_free_head",
    "pcc_gc_table_lock_owner_token",
    "pcc_gc_tracked_node_pool",
    "pcc_gc_tracked_node_pool_count",
    "py_gc_head",
    "py_gc_tracked_count",
}


def _exported_symbols(source: str) -> set[str]:
    return set(re.findall(r'@c_abi_export\("([^"]+)"\)', source))


def _compile_tracking_object(tmp_path: Path, emitter: str) -> tuple[Path, str]:
    llvm_ir = tmp_path / ("freestanding_gc_tracking_" + emitter + ".ll")
    pipeline.compile_python(
        str(TRACKING_SOURCE),
        str(llvm_ir),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    ir_text = llvm_ir.read_text(encoding="utf-8")
    source = llvm_ir
    if emitter == "self":
        from pcc.backend.self_backend_dispatch import emit_self_asm

        source = tmp_path / "freestanding_gc_tracking.s"
        source.write_text(emit_self_asm(ir_text), encoding="utf-8")
    obj = tmp_path / ("freestanding_gc_tracking_" + emitter + ".o")
    result = subprocess.run(
        ["clang", "-c", str(source), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return obj, ir_text


def _harness_source(concurrent: bool) -> str:
    if concurrent:
        return r"""
#include "py_runtime.h"
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

enum { THREADS = 4, PER_THREAD = 256 };

typedef union {
    uint64_t align[2];
    unsigned char bytes[16];
} ObjectStorage;

static ObjectStorage objects[THREADS * PER_THREAD];
static void *tokens[THREADS];

static void *track_range(void *raw) {
    intptr_t thread = (intptr_t)raw;
    tokens[thread] = pcc_current_native_thread_token();
    int start = (int)thread * PER_THREAD;
    int end = start + PER_THREAD;
    for (int i = start; i < end; i++) {
        py_gc_track((PyObject *)objects[i].bytes);
    }
    return NULL;
}

static void *untrack_range(void *raw) {
    intptr_t thread = (intptr_t)raw;
    int start = (int)thread * PER_THREAD;
    int end = start + PER_THREAD;
    for (int i = start; i < end; i++) {
        py_gc_untrack((PyObject *)objects[i].bytes);
    }
    return NULL;
}

static void *collect_rounds(void *raw) {
    (void)raw;
    for (int i = 0; i < 256; i++) py_gc_collect();
    return NULL;
}

int main(void) {
    pthread_t threads[THREADS];
    pthread_t collector;
    memset(objects, 0, sizeof(objects));
    py_gc_init();
    if (pthread_create(&collector, NULL, collect_rounds, NULL) != 0) return 1;
    for (intptr_t i = 0; i < THREADS; i++) {
        if (pthread_create(&threads[i], NULL, track_range, (void *)i) != 0) return 2;
    }
    for (int i = 0; i < THREADS; i++) pthread_join(threads[i], NULL);
    pthread_join(collector, NULL);
    for (int i = 0; i < THREADS; i++) {
        if (tokens[i] == NULL) return 5;
        for (int j = i + 1; j < THREADS; j++) {
            if (tokens[i] == tokens[j]) return 6;
        }
    }
    int tracked = 0;
    for (int i = 0; i < THREADS * PER_THREAD; i++) {
        tracked += (int)py_gc_is_tracked((PyObject *)objects[i].bytes);
    }
    printf("tracked:%lld,%d\n", (long long)py_gc_get_count(0), tracked);

    if (pthread_create(&collector, NULL, collect_rounds, NULL) != 0) return 4;
    for (intptr_t i = 0; i < THREADS; i++) {
        if (pthread_create(&threads[i], NULL, untrack_range, (void *)i) != 0) return 3;
    }
    for (int i = 0; i < THREADS; i++) pthread_join(threads[i], NULL);
    pthread_join(collector, NULL);
    int remaining = 0;
    for (int i = 0; i < THREADS * PER_THREAD; i++) {
        remaining += (int)py_gc_is_tracked((PyObject *)objects[i].bytes);
    }
    printf("untracked:%lld,%d\n", (long long)py_gc_get_count(0), remaining);
    return 0;
}
"""
    return r"""
#include "py_runtime.h"
#include <stdint.h>
#include <stdio.h>

typedef union {
    uint64_t align[2];
    unsigned char bytes[16];
} ObjectStorage;

int main(void) {
    ObjectStorage first = {{0, 0}};
    ObjectStorage second = {{0, 0}};
    py_gc_init();
    py_gc_track(NULL);
    py_gc_track((PyObject *)(uintptr_t)1);
    printf("ignored:%lld\n", (long long)py_gc_get_count(0));
    py_gc_track((PyObject *)first.bytes);
    printf("first:%lld,%lld\n", (long long)py_gc_get_count(0),
           (long long)py_gc_is_tracked((PyObject *)first.bytes));
    py_gc_track((PyObject *)first.bytes);
    printf("duplicate:%lld\n", (long long)py_gc_get_count(0));
    py_gc_track((PyObject *)second.bytes);
    printf("second:%lld,%lld\n", (long long)py_gc_get_count(0),
           (long long)py_gc_is_tracked((PyObject *)second.bytes));
    py_gc_untrack((PyObject *)first.bytes);
    printf("untrack-first:%lld,%lld,%lld\n", (long long)py_gc_get_count(0),
           (long long)py_gc_is_tracked((PyObject *)first.bytes),
           (long long)py_gc_is_tracked((PyObject *)second.bytes));
    py_gc_untrack((PyObject *)first.bytes);
    py_gc_untrack((PyObject *)second.bytes);
    printf("empty:%lld\n", (long long)py_gc_get_count(0));
    return 0;
}
"""


def _link_harness(
    tmp_path: Path, name: str, archive: Path, *, concurrent: bool = False
) -> Path:
    source = tmp_path / (name + ".c")
    executable = tmp_path / name
    source.write_text(_harness_source(concurrent), encoding="utf-8")
    command = [
        "clang",
        "-std=c11",
        f"-I{RUNTIME_DIR / 'include'}",
        str(source),
        str(archive),
    ]
    if concurrent:
        command.append("-pthread")
    command.extend(["-o", str(executable)])
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return executable


def test_gc_tracking_has_one_strict_source_owner_and_one_unlink_rule():
    tracking = TRACKING_SOURCE.read_text(encoding="utf-8")
    managed = MANAGED_SOURCE.read_text(encoding="utf-8")
    collector = COLLECTOR_SOURCE.read_text(encoding="utf-8")
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "__pcc_freestanding__ = True" in tracking
    assert _exported_symbols(tracking) == PUBLIC_SYMBOLS | INTERNAL_SYMBOLS
    assert _exported_symbols(managed).isdisjoint(PUBLIC_SYMBOLS | INTERNAL_SYMBOLS)
    assert "def _unlink_node(" not in managed
    assert "pcc_gc_default_unlink_tracked_node = extern(" in collector
    assert "freestanding_gc_tracking" in makefile

    assert 'define_global_i8("pcc_py_gc_table_lock", 0)' in tracking
    assert 'atomic_test_and_set(lock, 0, "acquire")' in tracking
    assert 'atomic_clear(global_addr("pcc_py_gc_table_lock"), 0, "release")' in tracking
    assert 'global_addr("pcc_gc_table_lock_owner_token")' in tracking
    assert "pcc_current_native_thread_token()" in tracking
    assert "pcc_thread_safepoint()" in tracking
    assert "count >= 4096" in tracking
    for name in ("py_gc_track", "py_gc_untrack"):
        port_body = tracking.split("def " + name, 1)[1].split("\n\n@", 1)[0]
        assert "collector_owns_lock" in port_body
        assert "pcc_gc_default_table_lock()" in port_body
        assert "pcc_gc_default_table_unlock()" in port_body


@pytest.mark.parametrize("emitter", ["llvm", "self"])
def test_gc_tracking_object_has_exact_raw_closure_and_atomic_lock(
    tmp_path: Path, emitter: str
):
    obj, ir_text = _compile_tracking_object(tmp_path, emitter)
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
    assert undefined == RAW_FUNCTION_IMPORTS | RAW_GLOBAL_IMPORTS

    symbols_result = subprocess.run(
        ["nm", "-g", str(obj)], capture_output=True, text=True, timeout=30
    )
    assert symbols_result.returncode == 0, symbols_result.stdout + symbols_result.stderr
    defined = {
        line.split()[-1].lstrip("_")
        for line in symbols_result.stdout.splitlines()
        if line.strip() and " U " not in line
    }
    assert defined == PUBLIC_SYMBOLS | INTERNAL_SYMBOLS | {"pcc_py_gc_table_lock"}
    assert "atomicrmw xchg" in ir_text
    assert "acquire" in ir_text
    assert "store atomic i8 0" in ir_text
    assert "release" in ir_text


# What the harness printed under every collector against the retired C
# runtime oracle.
TRACKING_EXPECTED = (
    "ignored:0\n"
    "first:1,1\n"
    "duplicate:1\n"
    "second:2,1\n"
    "untrack-first:1,0,1\n"
    "empty:0\n"
)


def test_production_archive_uniquely_owns_tracking_gc0_to_gc4(
    tmp_path: Path,
    pcc_runtime_archive: Path,
):
    members_result = subprocess.run(
        ["ar", "-t", str(pcc_runtime_archive)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert members_result.returncode == 0, members_result.stdout + members_result.stderr
    assert "freestanding_gc_tracking.o" in members_result.stdout.splitlines()

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
        ":freestanding_gc_tracking.o:" in lines[0] for lines in owners.values()
    )
    assert not any(
        ":py_obj_gc.o:" in line for lines in owners.values() for line in lines
    )

    implementation = _link_harness(
        tmp_path, "gc_tracking_pcc_python", pcc_runtime_archive
    )
    for backend in range(5):
        env = {**os.environ, "PCC_GC_BACKEND": str(backend)}
        result = subprocess.run(
            [str(implementation)],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout == TRACKING_EXPECTED


def test_production_archive_tracking_lock_survives_real_pthread_contention(
    tmp_path: Path,
    pcc_runtime_archive: Path,
):
    implementation = _link_harness(
        tmp_path,
        "gc_tracking_threads_pcc_python",
        pcc_runtime_archive,
        concurrent=True,
    )
    env = {**os.environ, "PCC_GC_BACKEND": "0"}
    result = subprocess.run(
        [str(implementation)], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "tracked:1024,1024\nuntracked:0,0\n"
