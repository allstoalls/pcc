from __future__ import annotations

import ast
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

from pcc.frontends.python import pipeline
from pcc.frontends.python.codegen.runtime_abi import FREESTANDING_GC_RUNTIME_GLOBALS


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"
STRICT_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_zpage_mechanics.py"
MANAGED_SOURCE = RUNTIME_DIR / "py" / "py_gc_backend.py"
ALLOCATION_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_zpage_allocation.py"
MAKEFILE = RUNTIME_DIR / "Makefile"

OWNED_SYMBOLS = {
    "pcc_gc_backend4_zpage_active_page",
    "pcc_gc_backend4_zpage_clear_active_page",
    "pcc_gc_backend4_zpage_find_page_for_addr",
    "pcc_gc_backend4_zpage_find_reusable_page",
    "pcc_gc_backend4_zpage_find_reusable_page_for_gen",
    "pcc_gc_backend4_zpage_link_node",
    "pcc_gc_backend4_zpage_link_node_preallocated",
    "pcc_gc_backend4_zpage_node_alloc",
    "pcc_gc_backend4_zpage_node_plan_requires_prepare",
    "pcc_gc_backend4_zpage_node_prepare",
    "pcc_gc_backend4_zpage_node_release",
    "pcc_gc_backend4_zpage_node_take_prepared",
    "pcc_gc_backend4_zpage_pop_free_page",
    "pcc_gc_backend4_zpage_reset",
    "pcc_gc_backend4_zpage_set_active_page",
}
RAW_FUNCTION_IMPORTS = {
    "free",
    "malloc",
    "memset",
    "pcc_gc_backend4_evacuation_page_find",
    "pcc_gc_zpage_owner_index_upsert",
    "pcc_gc_zpage_owner_index_upsert_preallocated",
}
RAW_GLOBAL_IMPORTS = {
    "pcc_gc_backend4_active_medium_old_page",
    "pcc_gc_backend4_active_medium_young_page",
    "pcc_gc_backend4_active_small_old_page",
    "pcc_gc_backend4_active_small_young_page",
    "pcc_gc_backend4_evacuation_page_head",
    "pcc_gc_backend4_free_page_head",
    "pcc_gc_backend4_page_head",
    "pcc_gc_backend4_zpage_head",
    "pcc_gc_backend4_zpage_node_free_count",
    "pcc_gc_backend4_zpage_node_free_head",
}


def _exported_symbols(source: str) -> set[str]:
    return set(re.findall(r'@c_abi_export\("([^"]+)"\)', source))


def _literal_global_imports() -> set[str]:
    globals_: set[str] = set()
    tree = ast.parse(STRICT_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in {"global_addr", "global_load_ptr", "global_store_ptr"}:
            continue
        if not node.args:
            continue
        value = node.args[0]
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            globals_.add(value.value)
    return globals_


def test_zpage_mechanics_has_one_strict_source_owner() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    managed = MANAGED_SOURCE.read_text(encoding="utf-8")
    consumers = managed + ALLOCATION_SOURCE.read_text(encoding="utf-8")
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "__pcc_freestanding__ = True" in strict
    assert _exported_symbols(strict) == OWNED_SYMBOLS
    assert _exported_symbols(managed).isdisjoint(OWNED_SYMBOLS)
    assert "freestanding_gc_zpage_mechanics" in makefile
    for symbol in OWNED_SYMBOLS:
        assert f'"{symbol}"' in consumers
        assert f'@c_abi_export("{symbol}")' not in managed


@pytest.mark.parametrize("emitter", ["llvm", "self"])
def test_zpage_mechanics_has_exact_strict_object_closure(
    tmp_path: Path, emitter: str
) -> None:
    llvm_ir = tmp_path / ("zpage_mechanics_" + emitter + ".ll")
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

        source = tmp_path / "zpage_mechanics.s"
        source.write_text(
            emit_self_asm(llvm_ir.read_text(encoding="utf-8")), encoding="utf-8"
        )
    obj = tmp_path / ("zpage_mechanics_" + emitter + ".o")
    result = subprocess.run(
        ["clang", "-c", str(source), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
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


def test_zpage_mechanics_preserves_layout_and_bounded_reuse_contract() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")

    assert "if size > 4096:" in strict
    assert "if size > 65536:" in strict
    assert "(size + 7) & -8" in strict
    assert "capacity - allocated >= alloc_size" in strict
    assert "load_i64(page, 80)" in strict
    assert "delta + alloc_size <= span_capacity" in strict
    assert "if count >= 8192:" in strict
    assert "return malloc(80)" in strict
    assert 'global_store_ptr("pcc_gc_backend4_zpage_node_free_head", node)' in strict
    assert "pcc_gc_zpage_owner_index_upsert(load_ptr(node, 0), node)" in strict
    assert "store_ptr(page, 112, node)" in strict


def test_production_archive_has_one_zpage_mechanics_owner(
    pcc_runtime_archive: Path,
) -> None:
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
        assert ":freestanding_gc_zpage_mechanics.o:" in owners[0]
        assert ":py_gc_backend.o:" not in owners[0]


def _link_zpage_mechanics_probe(tmp_path: Path, archive: Path) -> Path:
    source = tmp_path / "zpage_mechanics_probe.c"
    executable = tmp_path / "zpage_mechanics_probe"
    source.write_text(
        textwrap.dedent(
            r'''
            #include <stdint.h>
            #include <stdio.h>
            #include <stdlib.h>

            extern void *pcc_gc_backend4_zpage_active_page(int64_t, int64_t);
            extern void pcc_gc_backend4_zpage_set_active_page(void *);
            extern void pcc_gc_backend4_zpage_clear_active_page(void *);
            extern void *pcc_gc_backend4_zpage_find_reusable_page_for_gen(
                int64_t, int64_t
            );
            extern void *pcc_gc_backend4_zpage_pop_free_page(int64_t);
            extern void pcc_gc_backend4_zpage_reset(void *, void *, int64_t);
            extern void *pcc_gc_backend4_zpage_find_page_for_addr(void *, int64_t);
            extern void *pcc_gc_backend4_zpage_node_alloc(void);
            extern void pcc_gc_backend4_zpage_node_release(void *);
            extern void pcc_gc_backend4_zpage_link_node(void *);

            extern void *pcc_gc_backend4_free_page_head;
            extern void *pcc_gc_backend4_zpage_head;
            extern int32_t pcc_gc_backend4_zpage_node_free_count;

            static void put_ptr(void *base, int64_t offset, void *value) {
                *(void **)((unsigned char *)base + offset) = value;
            }
            static void *get_ptr(void *base, int64_t offset) {
                return *(void **)((unsigned char *)base + offset);
            }

            int main(void) {
                unsigned char *page = (unsigned char *)calloc(1, 120);
                if (page == NULL) return 2;
                pcc_gc_backend4_zpage_reset(page, NULL, 128);
                void *span = get_ptr(page, 72);
                if (span == NULL) return 3;

                pcc_gc_backend4_zpage_set_active_page(page);
                int active = pcc_gc_backend4_zpage_active_page(0, 1) == page;
                int reusable =
                    pcc_gc_backend4_zpage_find_reusable_page_for_gen(128, 1)
                    == page;
                int found =
                    pcc_gc_backend4_zpage_find_page_for_addr(
                        (unsigned char *)span + 64, 128
                    ) == page;
                pcc_gc_backend4_zpage_clear_active_page(page);
                int cleared = pcc_gc_backend4_zpage_active_page(0, 1) == NULL;

                pcc_gc_backend4_free_page_head = page;
                put_ptr(page, 56, NULL);
                int popped = pcc_gc_backend4_zpage_pop_free_page(128) == page;
                int free_empty = pcc_gc_backend4_free_page_head == NULL;

                void *node0 = pcc_gc_backend4_zpage_node_alloc();
                if (node0 == NULL) return 4;
                pcc_gc_backend4_zpage_node_release(node0);
                int pooled = pcc_gc_backend4_zpage_node_free_count == 1;
                void *node1 = pcc_gc_backend4_zpage_node_alloc();
                int reused_node = node1 == node0;
                int pool_empty = pcc_gc_backend4_zpage_node_free_count == 0;

                void *owner = calloc(1, 16);
                if (owner == NULL || node1 == NULL) return 5;
                put_ptr(node1, 0, owner);
                put_ptr(node1, 8, page);
                pcc_gc_backend4_zpage_link_node(node1);
                int linked =
                    pcc_gc_backend4_zpage_head == node1
                    && get_ptr(page, 112) == node1
                    && get_ptr(page, 0) == owner;

                printf("%d,%d,%d,%d,%d,%d,%d,%d,%d,%d\n",
                       active, reusable, found, cleared, popped, free_empty,
                       pooled, reused_node, pool_empty, linked);
                return 0;
            }
            '''
        ).lstrip(),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            "clang",
            "-std=c11",
            str(source),
            str(archive),
            "-pthread",
            "-lm",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return executable


def test_zpage_mechanics_archive_runs_active_free_and_node_state_machines(
    tmp_path: Path,
    pcc_runtime_archive: Path,
) -> None:
    executable = _link_zpage_mechanics_probe(tmp_path, pcc_runtime_archive)
    result = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "1,1,1,1,1,1,1,1,1,1\n"
