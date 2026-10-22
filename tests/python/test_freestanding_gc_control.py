from __future__ import annotations

import ast
import os
import re
import subprocess
from pathlib import Path

import pytest

from pcc.frontends.python import pipeline
from pcc.frontends.python.codegen.runtime_abi import FREESTANDING_GC_RUNTIME_GLOBALS


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"
CONTROL_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_control.py"
MANAGED_SOURCE = RUNTIME_DIR / "py" / "py_obj_gc.py"
MAKEFILE = RUNTIME_DIR / "Makefile"

PUBLIC_SYMBOLS = {
    "py_gc_init",
    "py_gc_enable",
    "py_gc_disable",
    "py_gc_is_enabled",
    "py_gc_is_tracked",
    "py_gc_get_count",
    "py_gc_get_threshold",
    "py_gc_set_threshold",
    "py_gc_freeze",
    "py_gc_unfreeze",
    "py_gc_get_freeze_count",
}


def _exported_symbols(source: str) -> set[str]:
    return set(re.findall(r'@c_abi_export\("([^"]+)"\)', source))


def _literal_global_imports() -> set[str]:
    globals_: set[str] = set()
    tree = ast.parse(CONTROL_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "global_addr" or not node.args:
            continue
        value = node.args[0]
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            globals_.add(value.value)
    return globals_


def _compile_control_object(tmp_path: Path, emitter: str) -> Path:
    llvm_ir = tmp_path / "freestanding_gc_control.ll"
    pipeline.compile_python(
        str(CONTROL_SOURCE),
        str(llvm_ir),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    source = llvm_ir
    if emitter == "self":
        from pcc.backend.self_backend_dispatch import emit_self_asm

        source = tmp_path / "freestanding_gc_control.s"
        source.write_text(
            emit_self_asm(llvm_ir.read_text(encoding="utf-8")),
            encoding="utf-8",
        )
    obj = tmp_path / ("freestanding_gc_control_" + emitter + ".o")
    result = subprocess.run(
        ["clang", "-c", str(source), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return obj


def _control_harness_source() -> str:
    return r"""
#include "py_runtime.h"
#include <stdint.h>
#include <stdio.h>

int main(void) {
    union {
        uint64_t align[2];
        unsigned char bytes[16];
    } object = {{0, 0}};

    py_gc_init();
    printf("enabled:%lld\n", (long long)py_gc_is_enabled());
    py_gc_disable();
    printf("disabled:%lld\n", (long long)py_gc_is_enabled());
    py_gc_enable();
    printf("reenabled:%lld\n", (long long)py_gc_is_enabled());

    printf("threshold-default:%lld,%lld,%lld,%lld\n",
           (long long)py_gc_get_threshold(0),
           (long long)py_gc_get_threshold(1),
           (long long)py_gc_get_threshold(2),
           (long long)py_gc_get_threshold(3));
    py_gc_set_threshold(17, 18, 19);
    printf("threshold-set:%lld,%lld,%lld\n",
           (long long)py_gc_get_threshold(0),
           (long long)py_gc_get_threshold(1),
           (long long)py_gc_get_threshold(2));
    py_gc_set_threshold(-1, 20, -1);
    printf("threshold-negative-preserve:%lld,%lld,%lld\n",
           (long long)py_gc_get_threshold(0),
           (long long)py_gc_get_threshold(1),
           (long long)py_gc_get_threshold(2));

    printf("count:%lld,%lld\n",
           (long long)py_gc_get_count(0),
           (long long)py_gc_get_count(1));
    py_gc_freeze();
    printf("freeze:%lld\n", (long long)py_gc_get_freeze_count());
    py_gc_unfreeze();
    printf("unfreeze:%lld\n", (long long)py_gc_get_freeze_count());

    printf("tracked-null:%lld\n", (long long)py_gc_is_tracked(NULL));
    printf("tracked-tagged:%lld\n",
           (long long)py_gc_is_tracked((PyObject *)(uintptr_t)1));
    *((int32_t *)(object.bytes + 12)) = 0;
    printf("tracked-clear:%lld\n",
           (long long)py_gc_is_tracked((PyObject *)object.bytes));
    *((int32_t *)(object.bytes + 12)) = 2;
    printf("tracked-set:%lld\n",
           (long long)py_gc_is_tracked((PyObject *)object.bytes));
    return 0;
}
"""


def _link_control_harness(tmp_path: Path, name: str, archive: Path) -> Path:
    source = tmp_path / (name + ".c")
    executable = tmp_path / name
    source.write_text(_control_harness_source(), encoding="utf-8")
    result = subprocess.run(
        [
            "clang",
            "-std=c11",
            f"-I{RUNTIME_DIR / 'include'}",
            str(source),
            str(archive),
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return executable


def test_gc_control_exports_have_one_strict_freestanding_source_owner():
    control = CONTROL_SOURCE.read_text(encoding="utf-8")
    managed = MANAGED_SOURCE.read_text(encoding="utf-8")
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "__pcc_freestanding__ = True" in control
    assert _exported_symbols(control) == PUBLIC_SYMBOLS
    assert _exported_symbols(managed).isdisjoint(PUBLIC_SYMBOLS)
    assert "freestanding_gc_control" in makefile


@pytest.mark.parametrize("emitter", ["llvm", "self"])
def test_gc_control_object_has_exact_raw_cross_object_closure(
    tmp_path: Path, emitter: str
):
    obj = _compile_control_object(tmp_path, emitter)
    expected_globals = _literal_global_imports()
    assert expected_globals == {
        "py_gc_enabled",
        "py_gc_freeze_count",
        "py_gc_threshold0",
        "py_gc_threshold1",
        "py_gc_threshold2",
        "py_gc_tracked_count",
    }
    assert expected_globals <= FREESTANDING_GC_RUNTIME_GLOBALS

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
    assert undefined == expected_globals

    symbols_result = subprocess.run(
        ["nm", "-g", str(obj)], capture_output=True, text=True, timeout=30
    )
    assert symbols_result.returncode == 0, symbols_result.stdout + symbols_result.stderr
    defined = {
        line.split()[-1].lstrip("_")
        for line in symbols_result.stdout.splitlines()
        if line.strip() and " U " not in line
    }
    assert defined == PUBLIC_SYMBOLS


# What the harness printed against the retired C runtime oracle.
CONTROL_EXPECTED = (
    "enabled:1\n"
    "disabled:0\n"
    "reenabled:1\n"
    "threshold-default:2000,10,10,0\n"
    "threshold-set:17,18,19\n"
    "threshold-negative-preserve:17,20,19\n"
    "count:0,0\n"
    "freeze:1\n"
    "unfreeze:0\n"
    "tracked-null:0\n"
    "tracked-tagged:0\n"
    "tracked-clear:0\n"
    "tracked-set:1\n"
)


def test_production_archive_uniquely_owns_gc_control_gc0_to_gc4(
    tmp_path: Path,
    pcc_runtime_archive: Path,
):
    members_result = subprocess.run(
        ["ar", "-t", str(pcc_runtime_archive)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert members_result.returncode == 0, (
        members_result.stdout + members_result.stderr
    )
    assert "freestanding_gc_control.o" in members_result.stdout.splitlines()

    symbols_result = subprocess.run(
        ["nm", "-A", "-g", str(pcc_runtime_archive)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert symbols_result.returncode == 0, (
        symbols_result.stdout + symbols_result.stderr
    )
    owners: dict[str, list[str]] = {symbol: [] for symbol in PUBLIC_SYMBOLS}
    for line in symbols_result.stdout.splitlines():
        symbol = line.split()[-1].lstrip("_") if line.strip() else ""
        if symbol in owners and " U " not in line:
            owners[symbol].append(line)
    assert all(len(lines) == 1 for lines in owners.values())
    assert all(
        ":freestanding_gc_control.o:" in lines[0] for lines in owners.values()
    )
    assert not any(
        ":py_obj_gc.o:" in line
        for lines in owners.values()
        for line in lines
    )

    implementation = _link_control_harness(
        tmp_path, "gc_control_pcc_python", pcc_runtime_archive
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
        assert result.stdout == CONTROL_EXPECTED
