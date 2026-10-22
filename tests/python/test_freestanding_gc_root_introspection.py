from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from pcc.frontends.python import pipeline
from pcc.frontends.python.codegen.runtime_abi import FREESTANDING_GC_RUNTIME_GLOBALS
from tests.runtime_build_cache import cached_threaded_pcc_python_runtime


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"
ROOT_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_root_introspection.py"
MANAGED_SOURCE = RUNTIME_DIR / "py" / "py_gc_backend.py"
MAKEFILE = RUNTIME_DIR / "Makefile"

PUBLIC_SYMBOLS = {
    "pcc_gc_scheduler_root_count",
    "pcc_gc_frame_root_slot_count",
    "pcc_gc_continuation_root_slot_count",
    "pcc_gc_coroutine_root_score",
    "pcc_gc_slot_is_runtime_root",
}
INTERNAL_SYMBOLS = {"pcc_gc_root_slot_in_span"}
RAW_FUNCTION_IMPORTS = {
    "pcc_gc_backend",
    "pcc_py_gc_minor_graph_lock",
    "pcc_py_gc_minor_graph_unlock",
}
RAW_GLOBAL_IMPORTS = {
    "pcc_gc_scheduler_root_head",
    "pcc_gc_frame_head",
    "pcc_gc_continuation_root_head",
}


def _exported_symbols(source: str) -> set[str]:
    return set(re.findall(r'@c_abi_export\("([^"]+)"\)', source))


def _compile_object(tmp_path: Path, emitter: str) -> tuple[Path, str]:
    llvm_ir = tmp_path / ("freestanding_gc_root_introspection_" + emitter + ".ll")
    pipeline.compile_python(
        str(ROOT_SOURCE),
        str(llvm_ir),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    ir_text = llvm_ir.read_text(encoding="utf-8")
    source = llvm_ir
    if emitter == "self":
        from pcc.backend.self_backend_dispatch import emit_self_asm

        source = tmp_path / "freestanding_gc_root_introspection.s"
        source.write_text(emit_self_asm(ir_text), encoding="utf-8")
    obj = tmp_path / ("freestanding_gc_root_introspection_" + emitter + ".o")
    result = subprocess.run(
        ["clang", "-c", str(source), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return obj, ir_text


def test_root_introspection_has_one_strict_source_owner_and_shared_lock():
    strict = ROOT_SOURCE.read_text(encoding="utf-8")
    managed = MANAGED_SOURCE.read_text(encoding="utf-8")
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "__pcc_freestanding__ = True" in strict
    assert _exported_symbols(strict) == PUBLIC_SYMBOLS | INTERNAL_SYMBOLS
    assert _exported_symbols(managed).isdisjoint(PUBLIC_SYMBOLS | INTERNAL_SYMBOLS)
    assert "freestanding_gc_root_introspection" in makefile
    for name in (
        "pcc_gc_scheduler_root_count",
        "pcc_gc_frame_root_slot_count",
        "pcc_gc_continuation_root_slot_count",
        "pcc_gc_slot_is_runtime_root",
    ):
        strict_body = strict.split("def " + name, 1)[1].split("\n\n@", 1)[0]
        assert "pcc_py_gc_minor_graph_lock()" in strict_body
        assert "pcc_py_gc_minor_graph_unlock()" in strict_body


@pytest.mark.parametrize("emitter", ["llvm", "self"])
def test_root_introspection_object_has_exact_raw_closure(
    tmp_path: Path, emitter: str
):
    obj, _ = _compile_object(tmp_path, emitter)
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
    assert defined == PUBLIC_SYMBOLS | INTERNAL_SYMBOLS


def _harness_source(concurrent: bool) -> str:
    if concurrent:
        return r"""
#include "py_runtime.h"
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>

enum { THREADS = 4, ROUNDS = 512 };
static PyObject *slots[THREADS];

static void *mutate(void *raw) {
    intptr_t index = (intptr_t)raw;
    for (int i = 0; i < ROUNDS; i++) {
        void *handle = pcc_gc_scheduler_root_register_handle(&slots[index]);
        if (handle == NULL) return (void *)1;
        (void)pcc_gc_scheduler_root_count();
        pcc_gc_scheduler_root_unregister_handle(handle);
    }
    return NULL;
}

static void *observe(void *raw) {
    (void)raw;
    for (int i = 0; i < THREADS * ROUNDS; i++) {
        int64_t count = pcc_gc_scheduler_root_count();
        if (count < 0 || count > THREADS) return (void *)1;
    }
    return NULL;
}

int main(void) {
    pthread_t workers[THREADS];
    pthread_t observer;
    for (intptr_t i = 0; i < THREADS; i++) {
        if (pthread_create(&workers[i], NULL, mutate, (void *)i) != 0) return 2;
    }
    if (pthread_create(&observer, NULL, observe, NULL) != 0) return 3;
    for (int i = 0; i < THREADS; i++) {
        void *result = NULL;
        pthread_join(workers[i], &result);
        if (result != NULL) return 4;
    }
    void *result = NULL;
    pthread_join(observer, &result);
    if (result != NULL) return 5;
    printf("final:%lld\n", (long long)pcc_gc_scheduler_root_count());
    return 0;
}
"""
    return r"""
#include "py_runtime.h"
#include "py_internal.h"
#include <stdint.h>
#include <stdio.h>

int main(void) {
    int32_t frame_map[] = {2, 0, 8};
    int32_t continuation_map[] = {3, 0, 8, 16};
    PyObject *scheduler_slot = NULL;
    PyObject *frame_slots[2] = {NULL, NULL};
    PyObject *continuation_slots[3] = {NULL, NULL, NULL};
    void *handle = pcc_gc_scheduler_root_register_handle(&scheduler_slot);
    if (handle == NULL) return 1;
    pcc_gc_note_frame_enter(frame_map, frame_slots);
    pcc_gc_register_continuation_root(continuation_map, continuation_slots);
    printf("counts:%lld,%lld,%lld,%lld\n",
           (long long)pcc_gc_scheduler_root_count(),
           (long long)pcc_gc_frame_root_slot_count(),
           (long long)pcc_gc_continuation_root_slot_count(),
           (long long)pcc_gc_coroutine_root_score());
    printf("roots:%lld,%lld,%lld,%lld\n",
           (long long)pcc_gc_slot_is_runtime_root(&scheduler_slot),
           (long long)pcc_gc_slot_is_runtime_root(&frame_slots[1]),
           (long long)pcc_gc_slot_is_runtime_root(&continuation_slots[2]),
           (long long)pcc_gc_slot_is_runtime_root(NULL));
    pcc_gc_unregister_continuation_root(continuation_slots);
    pcc_gc_note_frame_leave(frame_slots);
    pcc_gc_scheduler_root_unregister_handle(handle);
    printf("empty:%lld,%lld,%lld,%lld\n",
           (long long)pcc_gc_scheduler_root_count(),
           (long long)pcc_gc_frame_root_slot_count(),
           (long long)pcc_gc_continuation_root_slot_count(),
           (long long)pcc_gc_coroutine_root_score());
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
        f"-I{RUNTIME_DIR / 'src'}",
        str(source),
        str(archive),
    ]
    if concurrent:
        command.append("-pthread")
    command.extend(["-o", str(executable)])
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return executable


# What the harness printed against the retired C runtime oracle.
ROOTS_EXPECTED = (
    # GC0 keeps no frame roots unless backend0 frame roots are enabled.
    "counts:1,0,3,4\nroots:1,0,1,0\nempty:0,0,0,0\n",
) + ("counts:1,2,3,6\nroots:1,1,1,0\nempty:0,0,0,0\n",) * 4


def test_production_archive_uniquely_owns_root_introspection(
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
    assert "freestanding_gc_root_introspection.o" in members_result.stdout.splitlines()

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
        ":freestanding_gc_root_introspection.o:" in lines[0]
        for lines in owners.values()
    )
    assert not any(
        ":py_gc_backend.o:" in line for lines in owners.values() for line in lines
    )

    implementation = _link_harness(
        tmp_path, "gc_roots_pcc_python", pcc_runtime_archive
    )
    for backend in range(5):
        env = {**os.environ, "PCC_GC_BACKEND": str(backend)}
        result = subprocess.run(
            [str(implementation)], env=env, capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout == ROOTS_EXPECTED[backend]


def test_root_introspection_survives_pthread_registry_contention(
    tmp_path: Path,
):
    threaded_pcc_python_archive = (
        cached_threaded_pcc_python_runtime() / "libpy_runtime_pcc_py.a"
    )
    implementation = _link_harness(
        tmp_path,
        "gc_roots_threads_pcc_python",
        threaded_pcc_python_archive,
        concurrent=True,
    )
    for backend in range(5):
        env = {**os.environ, "PCC_GC_BACKEND": str(backend)}
        result = subprocess.run(
            [str(implementation)], env=env, capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout == "final:0\n"
