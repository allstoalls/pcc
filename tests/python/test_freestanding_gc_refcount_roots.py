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
STRICT_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_refcount_roots.py"
MANAGED_SOURCE = RUNTIME_DIR / "py" / "py_gc_backend.py"
MARK_CYCLE_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_common_mark_cycle.py"
MAKEFILE = RUNTIME_DIR / "Makefile"

OWNED_SYMBOLS = {
    "pcc_gc_gray_refcount_external_roots",
    "pcc_gc_object_node_is_active",
}
MANAGED_PROVIDER_SYMBOLS = {"pcc_gc_subtract_referent_refs"}
RAW_FUNCTION_IMPORTS = {
    "pcc_gc_mark_root_gray_if_known",
    "pcc_gc_subtract_referent_refs",
}
RAW_GLOBAL_IMPORTS = {"pcc_gc_object_head"}


def _exported_symbols(source: str) -> set[str]:
    return set(re.findall(r'@c_abi_export\("([^"]+)"\)', source))


def _compile_object(tmp_path: Path, emitter: str) -> Path:
    llvm_ir = tmp_path / ("freestanding_gc_refcount_roots_" + emitter + ".ll")
    pipeline.compile_python(
        str(STRICT_SOURCE),
        str(llvm_ir),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    ir_text = llvm_ir.read_text(encoding="utf-8")
    source = llvm_ir
    if emitter == "self":
        from pcc.backend.self_backend_dispatch import emit_self_asm

        source = tmp_path / "freestanding_gc_refcount_roots.s"
        source.write_text(emit_self_asm(ir_text), encoding="utf-8")
    obj = tmp_path / ("freestanding_gc_refcount_roots_" + emitter + ".o")
    result = subprocess.run(
        ["clang", "-c", str(source), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return obj


def test_refcount_root_scan_has_split_strict_and_managed_owners():
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    managed = MANAGED_SOURCE.read_text(encoding="utf-8")
    mark_cycle = MARK_CYCLE_SOURCE.read_text(encoding="utf-8")
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "__pcc_freestanding__ = True" in strict
    assert _exported_symbols(strict) == OWNED_SYMBOLS
    assert MANAGED_PROVIDER_SYMBOLS <= _exported_symbols(managed)
    assert _exported_symbols(managed).isdisjoint(OWNED_SYMBOLS)
    assert (OWNED_SYMBOLS | MANAGED_PROVIDER_SYMBOLS).isdisjoint(RUNTIME_SIGNATURES)
    assert "freestanding_gc_refcount_roots" in makefile
    assert "pcc_gc_gray_refcount_external_roots()" in mark_cycle


@pytest.mark.parametrize("emitter", ["llvm", "self"])
def test_refcount_root_scan_object_has_exact_raw_closure(
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


def _harness_source() -> str:
    return r'''
#include "py_runtime.h"
#include "py_internal.h"
#include <stdint.h>
#include <stdio.h>

extern void pcc_gc_prepare_object_list_mark(int64_t explicit_collect);
extern void pcc_gc_gray_refcount_external_roots(void);
extern int64_t pcc_gc_gray_count_load_acquire(void);

int main(void) {
    if (pcc_gc_set_backend(PCC_GC_KIND_INCREMENTAL_TRICOLOR) != 0) return 2;
    PyObject *parent = py_list_new(0);
    PyObject *child = py_list_new(0);
    if (parent == NULL || child == NULL) return 3;
    py_list_append(parent, child);
    pcc_gc_release(child);  /* child is now held only by parent */

    pcc_gc_prepare_object_list_mark(1);
    pcc_gc_gray_refcount_external_roots();
    printf("internal:%d,%d,%lld\n",
           (((PyObjectHeader *)parent)->flags & PY_FLAG_GC_GRAY) != 0,
           (((PyObjectHeader *)child)->flags & PY_FLAG_GC_WHITE) != 0,
           (long long)pcc_gc_gray_count_load_acquire());

    pcc_gc_retain(child);
    pcc_gc_prepare_object_list_mark(1);
    pcc_gc_gray_refcount_external_roots();
    printf("external:%d,%d,%lld\n",
           (((PyObjectHeader *)parent)->flags & PY_FLAG_GC_GRAY) != 0,
           (((PyObjectHeader *)child)->flags & PY_FLAG_GC_GRAY) != 0,
           (long long)pcc_gc_gray_count_load_acquire());

    pcc_gc_release(child);
    pcc_gc_release(parent);
    return 0;
}
'''


def test_archive_owns_refcount_scan_and_executes_external_root_semantics(
    tmp_path: Path, pcc_runtime_archive: Path
):
    symbols_result = subprocess.run(
        ["nm", "-A", "-g", str(pcc_runtime_archive)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert symbols_result.returncode == 0, symbols_result.stdout + symbols_result.stderr
    expected_owners = {
        "pcc_gc_gray_refcount_external_roots": "freestanding_gc_refcount_roots.o",
        "pcc_gc_object_node_is_active": "freestanding_gc_refcount_roots.o",
        "pcc_gc_subtract_referent_refs": "py_gc_backend.o",
    }
    for symbol, owner in expected_owners.items():
        lines = [
            line
            for line in symbols_result.stdout.splitlines()
            if line.strip()
            and line.split()[-1].lstrip("_") == symbol
            and " U " not in line
        ]
        assert len(lines) == 1, (symbol, lines)
        assert ":" + owner + ":" in lines[0]

    source = tmp_path / "refcount_roots.c"
    executable = tmp_path / "refcount_roots"
    source.write_text(_harness_source(), encoding="utf-8")
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
    assert result.stdout == "internal:1,1,1\nexternal:1,1,2\n"
