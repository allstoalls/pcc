from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from pcc.frontends.python import pipeline
from pcc.frontends.python.codegen.runtime_abi import (
    FREESTANDING_GC_RUNTIME_GLOBALS,
    RUNTIME_SIGNATURES,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"
ROOT_OPS_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_root_operations.py"
MANAGED_SOURCE = RUNTIME_DIR / "py" / "py_gc_backend.py"
PIN_CALLER_SOURCE = RUNTIME_DIR / "py" / "py_obj.py"
TRANSFER_CALLER_SOURCE = RUNTIME_DIR / "py" / "py_os_path.py"
MAKEFILE = RUNTIME_DIR / "Makefile"

PIN_SYMBOLS = {"pcc_gc_pin", "pcc_gc_unpin"}
TRANSFER_SYMBOLS = {"pcc_gc_take_pinned_slot"}
LEASE_SYMBOLS = {"pcc_gc_foreign_lease_acquire", "pcc_gc_foreign_lease_release", "pcc_gc_object_is_address_pinned"}
HANDLED_SYMBOLS = {"py_handled_context_push", "py_handled_context_pop", "py_handled_context_swap", "py_handled_exception_slot"}
OWNED_DATA_SYMBOLS = {"pcc_gc_foreign_lease_active", "pcc_tls_handled_exception_context"}
OWNED_SYMBOLS = PIN_SYMBOLS | TRANSFER_SYMBOLS | LEASE_SYMBOLS | HANDLED_SYMBOLS | {
    "pcc_gc_gray_count_decrement_acq_rel",
    "pcc_gc_gray_count_increment_acq_rel",
    "pcc_gc_gray_count_load_acquire",
    "pcc_gc_gray_count_store_release",
    "pcc_gc_mark_root_gray_if_known",
    "pcc_gc_set_mark_color",
    "pcc_gc_object_is_known_no_lock",
    "pcc_gc_resolve_root_slot_unlocked",
}
RAW_ONLY_CROSS_OBJECT_SYMBOLS = {
    "pcc_gc_forwarding_index_find",
    "pcc_gc_object_index_find",
}
RAW_FUNCTION_IMPORTS = RAW_ONLY_CROSS_OBJECT_SYMBOLS | {
    "py_decref", "py_incref", "pcc_gc_config_ensure",
    "pcc_py_gc_minor_graph_lock", "pcc_py_gc_minor_graph_unlock",
    "pcc_gc_granule_is_object_start", "pcc_gc_managed_pointer_index_contains",
}
RAW_GLOBAL_IMPORTS = {"pcc_gc_backend_selected", "pcc_gc_gray_count", "pcc_gc_metric_pin"}


def _exported_symbols(source: str) -> set[str]:
    return set(re.findall(r'@c_abi_export\("([^"]+)"\)', source))


def _compile_object(tmp_path: Path, emitter: str) -> Path:
    llvm_ir = tmp_path / ("freestanding_gc_root_operations_" + emitter + ".ll")
    pipeline.compile_python(
        str(ROOT_OPS_SOURCE),
        str(llvm_ir),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    ir_text = llvm_ir.read_text(encoding="utf-8")
    source = llvm_ir
    if emitter == "self":
        from pcc.backend.self_backend_dispatch import emit_self_asm

        source = tmp_path / "freestanding_gc_root_operations.s"
        source.write_text(emit_self_asm(ir_text), encoding="utf-8")
    obj = tmp_path / ("freestanding_gc_root_operations_" + emitter + ".o")
    result = subprocess.run(
        ["clang", "-c", str(source), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return obj


def test_root_operations_have_one_strict_source_owner():
    strict = ROOT_OPS_SOURCE.read_text(encoding="utf-8")
    managed = MANAGED_SOURCE.read_text(encoding="utf-8")
    pin_caller = PIN_CALLER_SOURCE.read_text(encoding="utf-8")
    transfer_caller = TRANSFER_CALLER_SOURCE.read_text(encoding="utf-8")
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "__pcc_freestanding__ = True" in strict
    assert _exported_symbols(strict) == OWNED_SYMBOLS
    assert _exported_symbols(managed).isdisjoint(OWNED_SYMBOLS)
    assert RAW_ONLY_CROSS_OBJECT_SYMBOLS.isdisjoint(RUNTIME_SIGNATURES)
    assert "freestanding_gc_root_operations" in makefile
    assert _exported_symbols(pin_caller).isdisjoint(PIN_SYMBOLS)
    for symbol in OWNED_SYMBOLS:
        if symbol == "pcc_gc_set_mark_color":
            caller = strict
        elif symbol in LEASE_SYMBOLS:
            caller = (REPO_ROOT / "pcc/frontends/python/codegen/extern_lowering.py").read_text()
            if symbol == "pcc_gc_object_is_address_pinned":
                caller = managed
        elif symbol in HANDLED_SYMBOLS:
            caller = (REPO_ROOT / "pcc/frontends/python/codegen/exception_lowering.py").read_text()
            caller += (RUNTIME_DIR / "py/py_virtual_thread_runtime.py").read_text()
        else:
            caller = pin_caller if symbol in PIN_SYMBOLS else transfer_caller if symbol in TRANSFER_SYMBOLS else managed
        assert f'"{symbol}"' in caller


def test_strict_gc3_known_object_gate_rejects_deallocating_header_state():
    strict = ROOT_OPS_SOURCE.read_text(encoding="utf-8")
    body = strict.split("def pcc_gc_object_is_known_no_lock(", 1)[1].split(
        '@c_abi_export("pcc_gc_mark_root_gray_if_known")', 1
    )[0]
    assert "524288" in body
    assert (
        body.index("pcc_gc_object_index_find(obj)")
        < body.index("load_i64(node, 32)")
        < body.index("flags: i64 = load_i32(obj, 12)")
        < body.index("524288")
    )


@pytest.mark.parametrize("emitter", ["llvm", "self"])
def test_root_operations_object_has_exact_raw_closure(tmp_path: Path, emitter: str):
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
    assert defined == OWNED_SYMBOLS | OWNED_DATA_SYMBOLS


def _root_operations_harness_source() -> str:
    return r'''
#include "py_runtime.h"
#include "py_internal.h"
#include <stdint.h>
#include <sys/mman.h>
#include <stdio.h>

extern int64_t pcc_gc_gray_count_load_acquire(void);
extern void pcc_gc_gray_count_store_release(int64_t value);
extern void pcc_gc_gray_count_increment_acq_rel(void);
extern void pcc_gc_gray_count_decrement_acq_rel(void);
extern void pcc_gc_mark_root_gray_if_known(PyObject *obj);
extern int64_t pcc_gc_object_is_known_no_lock(PyObject *obj);
extern PyObject *pcc_gc_resolve_root_slot_unlocked(PyObject **base, int64_t offset);

typedef struct {
    PyObjectHeader h;
    int64_t length;
    int64_t capacity;
    PyObject **items;
} ProbeListObject;

int main(void) {
    if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
    ProbeListObject *obj = (ProbeListObject *)pcc_gc_alloc(64, PY_TYPE_LIST, 0);
    if (obj == NULL) return 3;
    obj->length = 0;
    obj->capacity = 0;
    obj->items = NULL;
    /* Publish like a real constructor: GC4 never relocates FRESH_ALLOC objects. */
    pcc_gc_publish_initialized((PyObject *)obj);

    pcc_gc_gray_count_store_release(0);
    pcc_gc_mark_root_gray_if_known((PyObject *)obj);
    pcc_gc_mark_root_gray_if_known((PyObject *)obj);
    int64_t gray = pcc_gc_gray_count_load_acquire();
    int64_t known = pcc_gc_object_is_known_no_lock((PyObject *)obj);

    if (pcc_gc_select_relocation_set(1) != 1) return 4;
    PyObject *moved = pcc_gc_relocate_copy((PyObject *)obj, 64);
    if (moved == NULL) return 5;
    PyObject *slot[1] = {(PyObject *)obj};
    PyObject *resolved = pcc_gc_resolve_root_slot_unlocked(slot, 0);
    printf("root:%lld,%lld,%lld,%lld\n",
           (long long)gray,
           (long long)known,
           (long long)(slot[0] != (PyObject *)obj),
           (long long)(resolved == moved));

    pcc_gc_gray_count_store_release(0);
    pcc_gc_gray_count_increment_acq_rel();
    pcc_gc_gray_count_increment_acq_rel();
    pcc_gc_gray_count_decrement_acq_rel();
    pcc_gc_gray_count_decrement_acq_rel();
    pcc_gc_gray_count_decrement_acq_rel();
    printf("counter:%lld\n", (long long)pcc_gc_gray_count_load_acquire());

    pcc_gc_release((PyObject *)obj);
    pcc_gc_release(moved);
    return 0;
}
'''


def _counter_thread_harness_source() -> str:
    return r'''
#include "py_runtime.h"
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>

enum { THREADS = 4, ROUNDS = 4096 };
extern int64_t pcc_gc_gray_count_load_acquire(void);
extern void pcc_gc_gray_count_store_release(int64_t value);
extern void pcc_gc_gray_count_increment_acq_rel(void);
extern void pcc_gc_gray_count_decrement_acq_rel(void);

static void *mutate(void *raw) {
    (void)raw;
    for (int i = 0; i < ROUNDS; i++) {
        pcc_gc_gray_count_increment_acq_rel();
        pcc_gc_gray_count_decrement_acq_rel();
    }
    return NULL;
}

int main(void) {
    pcc_gc_gray_count_store_release(0);
    pthread_t workers[THREADS];
    for (int i = 0; i < THREADS; i++) {
        if (pthread_create(&workers[i], NULL, mutate, NULL) != 0) return 2;
    }
    for (int i = 0; i < THREADS; i++) pthread_join(workers[i], NULL);
    printf("final:%lld\n", (long long)pcc_gc_gray_count_load_acquire());
    return 0;
}
'''


def _gc3_deallocating_known_harness_source() -> str:
    return r'''
#define _GNU_SOURCE
#include "py_runtime.h"
#include "py_internal.h"
#include <stdint.h>
#include <sys/mman.h>

extern int64_t pcc_gc_object_is_known_no_lock(PyObject *obj);

typedef struct {
    PyObjectHeader h;
    int64_t length;
    int64_t capacity;
    PyObject **items;
} ProbeListObject;

int main(void) {
    if (pcc_gc_set_backend(PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR) != 0) {
        return 2;
    }
    void *guard = mmap(
        0, 4096, PROT_NONE, MAP_PRIVATE | MAP_ANON, -1, 0
    );
    if (guard == MAP_FAILED) return 3;
    if (pcc_gc_object_is_known_no_lock((PyObject *)guard) != 0) return 4;
    if (munmap(guard, 4096) != 0) return 5;
    ProbeListObject *obj = (ProbeListObject *)pcc_gc_alloc(
        64, PY_TYPE_LIST, 0
    );
    if (obj == 0) return 6;
    obj->length = 0;
    obj->capacity = 0;
    obj->items = 0;
    if (pcc_gc_object_is_known_no_lock((PyObject *)obj) != 1) return 7;
    py_header_flags_or(&obj->h, PY_FLAG_GC_DEALLOCATING);
    if (pcc_gc_object_is_known_no_lock((PyObject *)obj) != 0) return 8;
    py_header_flags_and(&obj->h, ~PY_FLAG_GC_DEALLOCATING);
    pcc_gc_release((PyObject *)obj);
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
            f"-I{RUNTIME_DIR / 'src'}",
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


def test_archive_owns_root_operations_and_executes_semantics(
    tmp_path: Path, pcc_runtime_archive: Path
):
    symbols_result = subprocess.run(
        ["nm", "-A", "-g", str(pcc_runtime_archive)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert symbols_result.returncode == 0, symbols_result.stdout + symbols_result.stderr
    owners: dict[str, list[str]] = {symbol: [] for symbol in OWNED_SYMBOLS}
    for line in symbols_result.stdout.splitlines():
        symbol = line.split()[-1].lstrip("_") if line.strip() else ""
        if symbol in owners and " U " not in line:
            owners[symbol].append(line)
    assert all(len(lines) == 1 for lines in owners.values())
    assert all(
        ":freestanding_gc_root_operations.o:" in lines[0]
        for lines in owners.values()
    )

    executable = _link_harness(
        tmp_path,
        "root_operations_pcc_python",
        _root_operations_harness_source(),
        pcc_runtime_archive,
    )
    result = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "root:1,1,1,1\ncounter:0\n"


def test_gray_counter_survives_threaded_increment_decrement(
    tmp_path: Path, pcc_runtime_archive: Path
):
    executable = _link_harness(
        tmp_path,
        "root_operations_counter_threads",
        _counter_thread_harness_source(),
        pcc_runtime_archive,
    )
    result = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "final:0\n"


def test_strict_gc3_known_object_gate_executes_deallocating_rejection(
    tmp_path: Path,
    pcc_runtime_archive: Path,
):
    executable = _link_harness(
        tmp_path,
        "root_operations_gc3_deallocating",
        _gc3_deallocating_known_harness_source(),
        pcc_runtime_archive,
    )
    result = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
