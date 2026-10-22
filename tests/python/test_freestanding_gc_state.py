from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path

import pytest

from pcc.frontends.python import pipeline
from pcc.frontends.python.codegen import runtime_abi
from pcc.frontends.python.codegen.runtime_abi import (
    FREESTANDING_GC_I32_GLOBALS,
    FREESTANDING_GC_I64_GLOBALS,
    FREESTANDING_GC_PTR_GLOBALS,
    RUNTIME_GLOBALS,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"
STATE_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_state.py"
SELECTOR_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_relocation_selector.py"
ROOT_OPERATIONS_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_root_operations.py"
SUBSTRATE_SOURCE = RUNTIME_DIR / "py" / "py_substrate.py"


def _state_definitions(path: Path) -> dict[str, tuple[str, int | None]]:
    definitions: dict[str, tuple[str, int | None]] = {}
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in {
            "define_global_i32",
            "define_global_i64",
            "define_global_ptr_null",
        }:
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        name = node.args[0].value
        if not isinstance(name, str) or not name.startswith(("pcc_gc_", "py_gc_")):
            continue
        assert name not in definitions
        value = None
        if node.func.id in {"define_global_i32", "define_global_i64"}:
            value = ast.literal_eval(node.args[1])
        definitions[name] = (node.func.id, value)
    return definitions


def _build_object(tmp_path: Path, emitter: str) -> Path:
    llvm_ir = tmp_path / "freestanding_gc_state.ll"
    pipeline.compile_python(
        str(STATE_SOURCE),
        str(llvm_ir),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    source = llvm_ir
    if emitter == "self":
        from pcc.backend.self_backend_dispatch import emit_self_asm

        source = tmp_path / "freestanding_gc_state.s"
        source.write_text(
            emit_self_asm(llvm_ir.read_text(encoding="utf-8")),
            encoding="utf-8",
        )
    obj = tmp_path / ("freestanding_gc_state_" + emitter + ".o")
    build = subprocess.run(
        ["clang", "-c", str(source), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return obj


def _harness_source() -> str:
    return r"""
#include <stdint.h>

extern int32_t py_gc_enabled;
extern int32_t py_gc_threshold0;
extern int32_t py_gc_threshold1;
extern int32_t py_gc_threshold2;
extern int32_t pcc_gc_backend_selected;
extern int32_t pcc_gc_pause;
extern int32_t pcc_gc_stepmul;
extern int32_t pcc_gc_minor_heap_size;
extern int32_t pcc_gc_minor_alloc_max;
extern int32_t pcc_gc_next_object_id;
extern int64_t pcc_gc_table_lock_owner_token;
extern void *py_gc_head;
extern void *pcc_gc_root_slots;
extern void *pcc_gc_backend4_zpage_head;

int main(void) {
    int marker = 0;
    if (py_gc_enabled != 1) return 1;
    if (py_gc_threshold0 != 2000) return 2;
    if (py_gc_threshold1 != 10 || py_gc_threshold2 != 10) return 3;
    if (pcc_gc_backend_selected != 0) return 4;
    if (pcc_gc_pause != 1000 || pcc_gc_stepmul != 10000) return 5;
    if (pcc_gc_minor_heap_size != 1048576) return 6;
    if (pcc_gc_minor_alloc_max != 256) return 7;
    if (pcc_gc_next_object_id != 1) return 8;
    if (py_gc_head || pcc_gc_root_slots || pcc_gc_backend4_zpage_head) return 9;
    if (pcc_gc_table_lock_owner_token != 0) return 13;

    pcc_gc_backend_selected = 4;
    pcc_gc_next_object_id = 91;
    pcc_gc_root_slots = &marker;
    if (pcc_gc_backend_selected != 4) return 10;
    if (pcc_gc_next_object_id != 91) return 11;
    if (pcc_gc_root_slots != &marker) return 12;
    return 0;
}
"""


def _build_and_run_harness(
    tmp_path: Path, name: str, implementation: list[str]
) -> subprocess.CompletedProcess[str]:
    harness = tmp_path / (name + ".c")
    executable = tmp_path / name
    harness.write_text(_harness_source(), encoding="utf-8")
    build = subprocess.run(
        ["clang", "-std=c11", str(harness), *implementation, "-o", str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )


@pytest.mark.parametrize("emitter", ["llvm", "self"])
def test_freestanding_gc_state_has_complete_raw_abi_and_initial_values(
    tmp_path: Path, emitter: str
):
    expected = _state_definitions(STATE_SOURCE)
    # Recycled GC-tracking nodes add one pointer plus one bounded-pool count;
    # the refcount provenance probe adds its mode, first-miss report flag and
    # unmanaged-operation counter, the in-place refcount gate
    # (pcc_gc_refcount_fast) one more, the GC4 cross-thread medium-buffer
    # flush counters (flushes, flushed entries) two more, and the GC4
    # remembered-page table (head, entries, slot entries, high water) four.
    # The source already held 201 against this 200 pin; the GC4 evacuation
    # page find memo (page, node, revision) adds three, the published
    # precise stack-map table pointer one, and the differential walk's
    # mapped-object buffer (values, count, capacity) three, and the table's
    # address index (entries, count, location base) three, and the cached
    # stack-map rooting setting one; GC0's inline atomic refcount gate
    # (pcc_gc_refcount_atomic) one more, GC0's automatic collection the
    # survivors of the last collection, and tracing pacing the heap marked.
    assert len(expected) == 215
    assert expected["pcc_gc_refcount_atomic"] == ("define_global_i32", 0)
    assert expected["pcc_gc_tracked_after_collect"] == ("define_global_i64", 0)
    assert expected["pcc_gc_tracing_marked_bytes"] == ("define_global_i64", 0)
    assert expected["pcc_gc_precise_stackmap_table"] == ("define_global_ptr_null", None)
    assert expected["pcc_gc_backend4_deferred_recycle_pages"] == (
        "define_global_i64",
        0,
    )
    assert _state_definitions(SUBSTRATE_SOURCE) == {}

    obj = _build_object(tmp_path, emitter)
    undefined = subprocess.run(
        ["nm", "-u", str(obj)], capture_output=True, text=True, timeout=30
    )
    assert undefined.returncode == 0, undefined.stdout + undefined.stderr
    assert undefined.stdout.strip() == ""

    symbols = subprocess.run(
        ["nm", "-g", str(obj)], capture_output=True, text=True, timeout=30
    )
    assert symbols.returncode == 0, symbols.stdout + symbols.stderr
    defined = {
        line.split()[-1].lstrip("_")
        for line in symbols.stdout.splitlines()
        if line.strip() and " U " not in line
    }
    assert defined == set(expected)

    result = _build_and_run_harness(
        tmp_path, "gc_state_" + emitter, [str(obj)]
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == ""


def test_production_archive_plan_includes_freestanding_gc_state():
    makefile = (RUNTIME_DIR / "Makefile").read_text(encoding="utf-8")
    freestanding_line = makefile.split("FREESTANDING_PY_MODULES =", 1)[1].splitlines()[0]
    assert "freestanding_gc_state" in freestanding_line

    plan = subprocess.run(
        ["make", "-B", "-n", "libpy_runtime_pcc_py.a"],
        cwd=RUNTIME_DIR,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert plan.returncode == 0, plan.stdout + plan.stderr
    archive_lines = [
        line
        for line in plan.stdout.splitlines()
        if "ar rcs libpy_runtime_pcc_py.a.tmp" in line
    ]
    assert len(archive_lines) == 1
    assert "build_py/freestanding_gc_state.o" in archive_lines[0]


def test_gc_state_storage_types_are_registered_in_runtime_abi():
    definitions = _state_definitions(STATE_SOURCE)
    for owner_source in (SELECTOR_SOURCE, ROOT_OPERATIONS_SOURCE):
        owner_definitions = _state_definitions(owner_source)
        assert set(definitions).isdisjoint(owner_definitions)
        definitions.update(owner_definitions)
    assert definitions["pcc_gc_foreign_lease_active"] == ("define_global_i64", 0)
    expected = {
        name: (
            "i32" if kind == "define_global_i32"
            else "i64" if kind == "define_global_i64"
            else "ptr"
        )
        for name, (kind, _value) in definitions.items()
    }
    registered = {name: "i32" for name in FREESTANDING_GC_I32_GLOBALS}
    registered.update({name: "i64" for name in FREESTANDING_GC_I64_GLOBALS})
    registered.update({name: "ptr" for name in FREESTANDING_GC_PTR_GLOBALS})
    registered = {
        name: kind
        for name, kind in registered.items()
        if name.startswith(("pcc_gc_", "py_gc_"))
    }
    assert registered == expected


def test_raw_gc_state_uses_string_kinds_not_managed_llvm_type_registry():
    expected = {
        name: (
            "i32" if kind == "define_global_i32"
            else "i64" if kind == "define_global_i64"
            else "ptr"
        )
        for name, (kind, _value) in _state_definitions(STATE_SOURCE).items()
    }
    # The application safepoint gate intentionally imports this existing
    # raw flag through declare_runtime_global; no other raw state may leak.
    assert set(expected) & set(RUNTIME_GLOBALS) == {"pcc_gc_backend4_auto_step_pending"}
    assert expected["pcc_gc_backend4_auto_step_pending"] == "i32"
    assert str(RUNTIME_GLOBALS["pcc_gc_backend4_auto_step_pending"]) == "i32"
    assert all(isinstance(name, str) for name in runtime_abi.FREESTANDING_GC_RUNTIME_GLOBALS)


def test_built_production_archive_owns_and_runs_freestanding_gc_state(
    tmp_path: Path, pcc_runtime_archive: Path
):
    members = subprocess.run(
        ["ar", "-t", str(pcc_runtime_archive)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert members.returncode == 0, members.stdout + members.stderr
    assert "freestanding_gc_state.o" in members.stdout.splitlines()

    symbols = subprocess.run(
        ["nm", "-A", "-g", str(pcc_runtime_archive)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert symbols.returncode == 0, symbols.stdout + symbols.stderr
    expected = set(_state_definitions(STATE_SOURCE))
    owners: dict[str, list[str]] = {name: [] for name in expected}
    for line in symbols.stdout.splitlines():
        name = line.split()[-1].lstrip("_") if line.strip() else ""
        if name in owners and " U " not in line:
            owners[name].append(line)
    assert all(len(lines) == 1 for lines in owners.values())
    assert all(
        ":freestanding_gc_state.o:" in lines[0] for lines in owners.values()
    )

    result = _build_and_run_harness(
        tmp_path, "gc_state_production_archive", [str(pcc_runtime_archive)]
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == ""


def test_current_production_archive_gc_state_drives_all_five_collectors(
    tmp_path: Path, pcc_runtime_archive: Path
):
    source = tmp_path / "gc_state_runtime.py"
    executable = tmp_path / "gc_state_runtime"
    source.write_text(
        "import gc\n"
        "def main() -> None:\n"
        "    print(gc.get_threshold())\n"
        "    gc.set_threshold(701, 11, 12)\n"
        "    print(gc.get_threshold())\n"
        "    gc.disable()\n"
        "    print(gc.isenabled())\n"
        "    gc.enable()\n"
        "    print(gc.isenabled())\n"
        "    values = [1, 2, 3]\n"
        "    gc.collect()\n"
        "    print(values[0] + values[1] + values[2])\n"
        "if __name__ == '__main__':\n"
        "    main()\n",
        encoding="utf-8",
    )
    pipeline.compile_python(
        str(source),
        str(executable),
        backend="self",
        ir_scaffold_mode="on",
        libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )

    expected = "(2000, 10, 10)\n(701, 11, 12)\nFalse\nTrue\n6\n"
    for backend in range(5):
        env = dict(os.environ)
        env.pop("LC_ALL", None)
        env["PCC_GC_BACKEND"] = str(backend)
        result = subprocess.run(
            [str(executable)],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        assert result.returncode == 0, (
            f"GC backend {backend}: " + result.stdout + result.stderr
        )
        assert result.stdout == expected
