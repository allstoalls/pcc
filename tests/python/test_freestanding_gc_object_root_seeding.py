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
SEED_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_object_root_seeding.py"
MANAGED_SOURCE = RUNTIME_DIR / "py" / "py_gc_backend.py"
MAKEFILE = RUNTIME_DIR / "Makefile"

OWNED_SYMBOLS = {
    "pcc_gc_gray_current_roots",
    "pcc_gc_prepare_object_list_mark",
}
RAW_FUNCTION_IMPORTS = {
    "pcc_gc_object_is_address_pinned",
    "pcc_gc_gray_count_store_release",
    "pcc_gc_mark_root_gray_if_known",
    "pcc_gc_visit_registered_root_slots",
}
RAW_GLOBAL_IMPORTS = {"pcc_gc_object_head"}


def _exported_symbols(source: str) -> set[str]:
    return set(re.findall(r'@c_abi_export\("([^"]+)"\)', source))


def _compile_object(tmp_path: Path, emitter: str) -> Path:
    llvm_ir = tmp_path / ("freestanding_gc_object_root_seeding_" + emitter + ".ll")
    pipeline.compile_python(
        str(SEED_SOURCE),
        str(llvm_ir),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    ir_text = llvm_ir.read_text(encoding="utf-8")
    source = llvm_ir
    if emitter == "self":
        from pcc.backend.self_backend_dispatch import emit_self_asm

        source = tmp_path / "freestanding_gc_object_root_seeding.s"
        source.write_text(emit_self_asm(ir_text), encoding="utf-8")
    obj = tmp_path / ("freestanding_gc_object_root_seeding_" + emitter + ".o")
    result = subprocess.run(
        ["clang", "-c", str(source), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return obj


def test_object_root_seeding_has_one_strict_source_owner():
    strict = SEED_SOURCE.read_text(encoding="utf-8")
    managed = MANAGED_SOURCE.read_text(encoding="utf-8")
    mark_cycle = (
        RUNTIME_DIR / "py" / "freestanding_gc_common_mark_cycle.py"
    ).read_text(encoding="utf-8")
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "__pcc_freestanding__ = True" in strict
    assert _exported_symbols(strict) == OWNED_SYMBOLS
    assert _exported_symbols(managed).isdisjoint(OWNED_SYMBOLS)
    assert OWNED_SYMBOLS.isdisjoint(RUNTIME_SIGNATURES)
    assert "freestanding_gc_object_root_seeding" in makefile
    for symbol in OWNED_SYMBOLS:
        assert f'"{symbol}"' in mark_cycle
        assert f'"{symbol}"' not in managed
    assert "pcc_gc_gray_refcount_external_roots()" in mark_cycle


@pytest.mark.parametrize("emitter", ["llvm", "self"])
def test_object_root_seeding_object_has_exact_raw_closure(
    tmp_path: Path, emitter: str
):
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
    assert defined == OWNED_SYMBOLS


def _seeding_harness_source() -> str:
    return r'''
#include "py_runtime.h"
#include "py_internal.h"
#include <stdint.h>
#include <stdio.h>

extern void pcc_gc_prepare_object_list_mark(int64_t explicit_collect);
extern void pcc_gc_gray_current_roots(void);
extern int64_t pcc_gc_gray_count_load_acquire(void);

typedef struct {
    PyObjectHeader h;
    int64_t length;
    int64_t capacity;
    PyObject **items;
} ProbeListObject;

static ProbeListObject *new_probe(void) {
    ProbeListObject *obj = (
        (ProbeListObject *)pcc_gc_alloc(64, PY_TYPE_LIST, 0)
    );
    if (obj != NULL) {
        obj->length = 0;
        obj->capacity = 0;
        obj->items = NULL;
    }
    return obj;
}

int main(void) {
    if (pcc_gc_set_backend(PCC_GC_KIND_INCREMENTAL_TRICOLOR) != 0) return 2;
    ProbeListObject *pinned = new_probe();
    ProbeListObject *fresh = new_probe();
    ProbeListObject *registered = new_probe();
    if (pinned == NULL || fresh == NULL || registered == NULL) return 3;

    pinned->h.flags = PY_FLAG_GC_TRACKED | PY_FLAG_GC_PINNED;
    fresh->h.flags = PY_FLAG_GC_TRACKED | PY_FLAG_GC_FRESH_ALLOC;
    registered->h.flags = PY_FLAG_GC_TRACKED;

    int32_t frame_map[1] = {1};
    PyObject *frame_slots[1] = {(PyObject *)registered};
    pcc_gc_frame_enter(frame_map, frame_slots);

    pcc_gc_prepare_object_list_mark(0);
    printf("prepare:%d,%d,%d,%d\n",
           (pinned->h.flags & PY_FLAG_GC_WHITE) != 0,
           (fresh->h.flags & PY_FLAG_GC_BLACK) != 0,
           (fresh->h.flags & PY_FLAG_GC_FRESH_ALLOC) != 0,
           (registered->h.flags & PY_FLAG_GC_WHITE) != 0);
    pcc_gc_gray_current_roots();
    printf("gray:%d,%d,%lld\n",
           (pinned->h.flags & PY_FLAG_GC_GRAY) != 0,
           (registered->h.flags & PY_FLAG_GC_GRAY) != 0,
           (long long)pcc_gc_gray_count_load_acquire());

    fresh->h.flags = PY_FLAG_GC_TRACKED | PY_FLAG_GC_FRESH_ALLOC;
    pcc_gc_prepare_object_list_mark(1);
    printf("explicit:%d,%d\n",
           (fresh->h.flags & PY_FLAG_GC_WHITE) != 0,
           (fresh->h.flags & PY_FLAG_GC_FRESH_ALLOC) != 0);

    pcc_gc_frame_leave(frame_slots);
    pcc_gc_release((PyObject *)pinned);
    pcc_gc_release((PyObject *)fresh);
    pcc_gc_release((PyObject *)registered);
    return 0;
}
'''


def test_archive_owns_object_root_seeding_and_executes_semantics(
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
        ":freestanding_gc_object_root_seeding.o:" in lines[0]
        for lines in owners.values()
    )

    source = tmp_path / "object_root_seeding.c"
    executable = tmp_path / "object_root_seeding"
    source.write_text(_seeding_harness_source(), encoding="utf-8")
    build = subprocess.run(
        [
            "clang",
            "-std=c11",
            f"-I{RUNTIME_DIR / 'include'}",
            f"-I{RUNTIME_DIR / 'src'}",
            str(source),
            str(pcc_runtime_archive),
            "-pthread",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    result = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == (
        "prepare:1,1,0,1\n"
        "gray:1,1,2\n"
        "explicit:1,0\n"
    )
