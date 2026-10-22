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
STRICT_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_relocation_drain.py"
MANAGED_SOURCE = RUNTIME_DIR / "py" / "py_gc_backend.py"
MAKEFILE = RUNTIME_DIR / "Makefile"

OWNED_SYMBOLS = {
    "pcc_gc_backend4_evacuation_drain",
    "pcc_gc_backend4_evacuation_page_drain",
    "pcc_gc_relocation_drain_evacuation_page_head",
    "pcc_gc_relocation_drain_note_incomplete_batch",
    "pcc_gc_relocation_drain_relocation_set_head",
    "pcc_gc_relocation_drain_selected",
    "pcc_gc_relocation_drain_selected_page",
}
RAW_FUNCTION_IMPORTS = {
    "pcc_gc_backend",
    "pcc_gc_backend4_remap_and_retire_stopped_world",
    "pcc_gc_backend4_zpage_find",
    "pcc_gc_config_ensure",
    "pcc_gc_object_known_size",
    "pcc_gc_relocate_copy",
    "pcc_py_gc_minor_graph_lock",
    "pcc_py_gc_minor_graph_unlock",
    "pcc_thread_safepoint",
    "py_decref",
}
RAW_GLOBAL_IMPORTS = {
    "pcc_gc_backend4_evacuation_incomplete_batches_count",
    "pcc_gc_backend4_evacuation_page_head",
    "pcc_gc_backend4_remap_active",
    "pcc_gc_relocation_set_head",
    "pcc_gc_forwarding_population",
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


def test_relocation_drain_has_one_strict_source_owner() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    managed = MANAGED_SOURCE.read_text(encoding="utf-8")
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "__pcc_freestanding__ = True" in strict
    assert _exported_symbols(strict) == OWNED_SYMBOLS
    assert _exported_symbols(managed).isdisjoint(OWNED_SYMBOLS)
    assert "freestanding_gc_relocation_drain" in makefile
    assert "def _relocate_selected(" not in managed
    assert "def _relocate_selected_page(" not in managed
    assert 'pcc_gc_backend4_evacuation_drain = extern(' in managed
    assert 'pcc_gc_backend4_evacuation_page_drain = extern(' in managed


@pytest.mark.parametrize("emitter", ["llvm", "self"])
def test_relocation_drain_has_exact_strict_object_closure(
    tmp_path: Path, emitter: str
) -> None:
    llvm_ir = tmp_path / ("relocation_drain_" + emitter + ".ll")
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

        source = tmp_path / "relocation_drain.s"
        source.write_text(
            emit_self_asm(llvm_ir.read_text(encoding="utf-8")), encoding="utf-8"
        )
    obj = tmp_path / ("relocation_drain_" + emitter + ".o")
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


def test_relocation_drain_preserves_budget_lock_and_handoff_contract() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")

    object_body = strict.split("def _relocate_selected(budget: i64)", 1)[1].split(
        '@c_abi_export("pcc_gc_backend4_evacuation_drain")', 1
    )[0]
    assert "sources = stack_alloc(128)" in object_body
    assert "while moved < budget and stalled == 0:" in object_body
    assert "capacity: i64 = budget - moved" in object_body
    assert "if capacity > 16:" in object_body
    assert "capacity = 16" in object_body
    assert "while ptr_is_null(node) == 0 and captured < capacity:" in object_body
    assert "store_ptr(sources, captured * 8, load_ptr(node, 0))" in object_body
    assert "node = load_ptr(node, 8)" in object_body
    snapshot_unlock = object_body.index("pcc_py_gc_minor_graph_unlock()")
    public_copy = object_body.index("to_obj = pcc_gc_relocate_copy(")
    tail_drop = object_body.index("py_decref(to_obj)", public_copy)
    tail_poll = object_body.index("pcc_thread_safepoint()", tail_drop)
    assert snapshot_unlock < public_copy < tail_drop < tail_poll
    assert "nxt = load_ptr(node, 8)" not in object_body
    assert object_body.count("pcc_py_gc_minor_graph_lock()") == 2
    assert object_body.count("pcc_py_gc_minor_graph_unlock()") == 2
    final_lock = object_body.rindex("pcc_py_gc_minor_graph_lock()")
    note_incomplete = object_body.index("_note_incomplete_batch(moved)")
    final_unlock = object_body.rindex("pcc_py_gc_minor_graph_unlock()")
    remap = object_body.index(
        "pcc_gc_backend4_remap_and_retire_stopped_world()", final_unlock
    )
    assert tail_poll < final_lock < note_incomplete < final_unlock < remap

    assert "sources = stack_alloc(128)" in strict
    assert "and examined < 16" in strict
    assert "and captured < 16" in strict
    assert "store_ptr(sources, captured * 8, obj)" in strict
    assert "znode = pcc_gc_backend4_zpage_find(obj)" in strict
    assert "ptr_eq(load_ptr(znode, 8), page)" in strict
    assert "pcc_gc_backend4_zpage_page_for_owner" not in strict
    page_body = strict.split("def _relocate_selected_page(page)", 1)[1].split(
        '@c_abi_export("pcc_gc_backend4_evacuation_page_drain")', 1
    )[0]
    snapshot_unlock = page_body.index("pcc_py_gc_minor_graph_unlock()")
    copy_call = page_body.index("to_obj = pcc_gc_relocate_copy(")
    tail_drop = page_body.index("py_decref(to_obj)")
    tail_poll = page_body.index("pcc_thread_safepoint()")
    assert snapshot_unlock < copy_call < tail_drop < tail_poll
    assert "while pages < page_budget:" in strict
    assert "pcc_py_gc_minor_graph_lock()" in strict
    assert "pcc_py_gc_minor_graph_unlock()" in strict
    assert "pcc_gc_backend4_remap_and_retire_stopped_world()" in strict
    assert "pcc_gc_backend4_evacuation_incomplete_batches_count" in strict


def test_production_archive_has_one_relocation_drain_owner(
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
        assert ":freestanding_gc_relocation_drain.o:" in owners[0]
        assert ":py_gc_backend.o:" not in owners[0]


def _link_drain_probe(tmp_path: Path, name: str, archive: Path) -> Path:
    source = tmp_path / (name + ".c")
    executable = tmp_path / name
    source.write_text(
        textwrap.dedent(
            r'''
            #include "py_runtime.h"
            #include <stdio.h>

            enum { PY_FLAG_GC_OLD = 0x100 };

            /* Runtime-internal: not in the public header. */
            extern void pcc_gc_publish_initialized(PyObject *obj);

            int main(int argc, char **argv) {
                if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) {
                    return 2;
                }
                PyObject *a = pcc_gc_alloc(128, PY_TYPE_LIST, PY_FLAG_GC_OLD);
                PyObject *b = pcc_gc_alloc(128, PY_TYPE_LIST, PY_FLAG_GC_OLD);
                if (a == 0 || b == 0) return 3;
                /* pcc_gc_alloc sets PY_FLAG_GC_FRESH_ALLOC for container tags
                 * and pcc_gc_relocation_set_add_preallocated refuses to
                 * relocate an object that still carries it -- moving a
                 * half-initialized object is not safe.  Real constructors clear
                 * it by publishing once initialization completes, so a raw
                 * allocation must do the same or it can never be selected. */
                pcc_gc_publish_initialized(a);
                pcc_gc_publish_initialized(b);
                pcc_gc_telemetry_reset();
                if (pcc_gc_select_relocation_set(8) != 2) return 4;

                long long first = 0;
                long long second = 0;
                long long third = 0;
                if (argc > 1 && argv[1][0] == 's') {
                    first = pcc_gc_step(1);
                } else if (argc > 1 && argv[1][0] == 'p') {
                    first = pcc_gc_backend4_evacuation_page_drain(1);
                    second = pcc_gc_backend4_evacuation_page_drain(1);
                } else {
                    first = pcc_gc_backend4_evacuation_drain(1);
                    second = pcc_gc_backend4_evacuation_drain(1);
                    third = pcc_gc_backend4_evacuation_drain(1);
                }
                printf("%lld,%lld,%lld,%lld,%lld,%lld,%lld\n",
                       first,
                       second,
                       third,
                       (long long)pcc_gc_relocation_set_size(),
                       (long long)pcc_gc_backend4_evacuation_page_candidate_score(),
                       (long long)pcc_gc_backend4_evacuation_incomplete_batches(),
                       (long long)pcc_gc_backend4_evacuation_efficiency_per_mille());
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
            f"-I{RUNTIME_DIR / 'include'}",
            f"-I{RUNTIME_DIR / 'src'}",
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


@pytest.mark.parametrize(
    ("argument", "expected"),
    [
        ("object", "1,1,0,0,0,1,1000\n"),
        ("page", "2,0,0,0,0,0,1000\n"),
        ("step", "2,0,0,0,0,0,1000\n"),
    ],
)
def test_relocation_drain_for_object_page_and_step_budgets(
    tmp_path: Path,
    pcc_runtime_archive: Path,
    argument: str,
    expected: str,
) -> None:
    # ``expected`` is what the retired C runtime oracle printed.
    implementation = _link_drain_probe(
        tmp_path, "drain_pcc_python_" + argument, pcc_runtime_archive
    )
    result = subprocess.run(
        [str(implementation), argument], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == expected
