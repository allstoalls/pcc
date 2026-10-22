from __future__ import annotations

import re
import subprocess
import textwrap
from pathlib import Path

import pytest

from pcc.frontends.python import pipeline


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"
STRICT_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_backend0_slots.py"
MANAGED_SOURCE = RUNTIME_DIR / "py" / "py_obj_gc.py"
COLLECTOR_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_backend0_collector.py"
CLEAR_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_sweep_slots.py"
MAKEFILE = RUNTIME_DIR / "Makefile"

OWNED_SYMBOLS = {
    "pcc_gc_backend0_is_unreachable",
    "pcc_gc_backend0_mark_reachable",
    "pcc_gc_backend0_mark_slot",
    "pcc_gc_backend0_subtract_slot",
    "pcc_gc_backend0_visit_subtract",
}
RAW_FUNCTION_IMPORTS = {
    # The iterative mark's worklist moves to the heap once it fills.
    "free",
    "malloc",
    "memmove",
    "pcc_gc_load_ptr",
    "pcc_gc_visit_object_slots",
    "py_gc_index_find",
}


def _exported_symbols(source: str) -> set[str]:
    return set(re.findall(r'@c_abi_export\("([^"]+)"\)', source))


def _compile_object(tmp_path: Path, emitter: str) -> Path:
    llvm_ir = tmp_path / ("freestanding_gc_backend0_slots_" + emitter + ".ll")
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

        source = tmp_path / "freestanding_gc_backend0_slots.s"
        source.write_text(
            emit_self_asm(llvm_ir.read_text(encoding="utf-8")),
            encoding="utf-8",
        )
    obj = tmp_path / ("freestanding_gc_backend0_slots_" + emitter + ".o")
    result = subprocess.run(
        ["clang", "-c", str(source), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return obj


def test_backend0_slot_actions_have_one_strict_source_owner():
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    managed = MANAGED_SOURCE.read_text(encoding="utf-8")
    collector = COLLECTOR_SOURCE.read_text(encoding="utf-8")
    clear_source = CLEAR_SOURCE.read_text(encoding="utf-8")
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "__pcc_freestanding__ = True" in strict
    assert _exported_symbols(strict) == OWNED_SYMBOLS
    assert _exported_symbols(managed).isdisjoint(OWNED_SYMBOLS)
    assert _exported_symbols(clear_source).isdisjoint(OWNED_SYMBOLS)
    assert "freestanding_gc_backend0_slots" in makefile
    assert "pcc_gc_visit_object_slots = extern(" in strict
    assert "def _py_obj_gc_visit_subtract_slot(" not in managed
    assert "def _py_obj_gc_visit_mark_slot(" not in managed
    assert "def _py_obj_gc_visit_clear_slot(" not in managed
    assert "def _py_obj_gc_clear_container_metadata(" not in managed
    for symbol in (
        "pcc_gc_backend0_visit_subtract",
        "pcc_gc_backend0_mark_reachable",
    ):
        assert f"{symbol} = extern(" in collector
        assert f'"{symbol}"' in collector
    assert "pcc_gc_backend0_clear_referents = extern(" in collector
    assert '@c_abi_export("pcc_gc_backend0_clear_referents")' in clear_source


@pytest.mark.parametrize("emitter", ["llvm", "self"])
def test_backend0_slot_actions_have_exact_raw_object_closure(
    tmp_path: Path, emitter: str
):
    obj = _compile_object(tmp_path, emitter)
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
    assert undefined == RAW_FUNCTION_IMPORTS

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


def _link_cycle_harness(tmp_path: Path, name: str, archive: Path) -> Path:
    source = tmp_path / (name + ".c")
    executable = tmp_path / name
    source.write_text(
        r'''
#include "py_runtime.h"
#include <stdio.h>

int main(void) {
    py_gc_init();
    if (pcc_gc_set_backend(PCC_GC_KIND_REFCOUNT_CYCLE) != 0) return 2;
    PyObject *left = py_list_new(0);
    PyObject *right = py_list_new(0);
    if (left == NULL || right == NULL) return 3;
    py_list_append(left, right);
    py_list_append(right, left);
    pcc_gc_release(left);
    pcc_gc_release(right);
    printf("before:%lld\n", (long long)py_gc_get_count(0));
    printf("collected:%lld\n", (long long)py_gc_collect());
    printf("after:%lld\n", (long long)py_gc_get_count(0));
    return 0;
}
''',
        encoding="utf-8",
    )
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


def test_production_archive_uniquely_owns_backend0_actions_and_collects_cycle(
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
    for symbol in OWNED_SYMBOLS:
        owners = [
            line
            for line in symbols_result.stdout.splitlines()
            if line.strip()
            and line.split()[-1].lstrip("_") == symbol
            and " U " not in line
        ]
        assert len(owners) == 1, (symbol, owners)
        assert ":freestanding_gc_backend0_slots.o:" in owners[0]
        assert ":py_obj_gc.o:" not in owners[0]

    implementation = _link_cycle_harness(
        tmp_path, "backend0_cycle_pcc_python", pcc_runtime_archive
    )
    result = subprocess.run(
        [str(implementation)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "before:2\ncollected:2\nafter:0\n"


def test_backend0_finalizer_may_track_temporaries_without_table_lock_deadlock(
    tmp_path: Path,
    pcc_runtime_archive: Path,
):
    source = tmp_path / "backend0_finalizer_reentry.py"
    source.write_text(
        textwrap.dedent(
            """
            import gc

            runs = []

            class Finalized:
                def __del__(self):
                    runs.append("ran")
                    self.peer = None

            def make_cycle():
                value = Finalized()
                value.peer = value

            def main() -> None:
                before = gc.get_count()[0]
                make_cycle()
                gc.collect()
                print(gc.get_count()[0] == before)
                print(runs)

            if __name__ == "__main__":
                main()
            """
        ).lstrip(),
        encoding="utf-8",
    )

    executable = tmp_path / "backend0_finalizer_reentry"
    pipeline.compile_python(
        str(source),
        str(executable),
        libpython_mode="off",
        ir_scaffold_mode="on",
        runtime_archive=str(pcc_runtime_archive),
    )
    result = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "True\n['ran']\n"
