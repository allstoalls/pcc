"""Ownership ratchets for the production no-libpython runtime archive."""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import textwrap
from functools import lru_cache
from pathlib import Path

from pcc.tools.runtime_archive_provenance import (
    PRODUCTION_POLICY,
    verify_runtime_archive_manifest,
)


REPO = Path(__file__).resolve().parents[2]
REPO_ROOT = REPO
RUNTIME_MAKEFILE = REPO / "pcc" / "py_runtime" / "Makefile"


def _make_variable_tokens(makefile: str, variable: str) -> set[str]:
    prefix = variable + " ="
    for line in makefile.splitlines():
        if line.startswith(prefix):
            return set(line.removeprefix(prefix).split())
    raise AssertionError(f"missing Makefile variable: {variable}")


@lru_cache(maxsize=None)
def _archive_members(archive: Path) -> set[str]:
    result = subprocess.run(
        ["ar", "-t", str(archive)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return set(result.stdout.splitlines())


@lru_cache(maxsize=None)
def _defined_symbol_owners(archive: Path) -> dict[str, set[str]]:
    result = subprocess.run(
        ["nm", "-A", str(archive)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    prefix = str(archive) + ":"
    owners: dict[str, set[str]] = {}
    for line in result.stdout.splitlines():
        if not line.startswith(prefix):
            continue
        member, separator, body = line[len(prefix) :].partition(":")
        if not separator:
            continue
        fields = body.split()
        if not fields or fields[0] in {"U", "u"} or "(undefined)" in body:
            continue
        symbol = fields[-1].lstrip("_")
        owners.setdefault(symbol, set()).add(member)
    return owners


@lru_cache(maxsize=None)
def _undefined_symbol_users(archive: Path) -> dict[str, set[str]]:
    result = subprocess.run(
        ["nm", "-A", str(archive)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    prefix = str(archive) + ":"
    users: dict[str, set[str]] = {}
    for line in result.stdout.splitlines():
        if not line.startswith(prefix):
            continue
        member, separator, body = line[len(prefix) :].partition(":")
        if not separator:
            continue
        fields = body.split()
        if not fields or not (fields[0] in {"U", "u"} or "(undefined)" in body):
            continue
        symbol = fields[-1].lstrip("_")
        users.setdefault(symbol, set()).add(member)
    return users


def test_thread_runtime_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """The production archive must not retain the mixed C thread runtime."""
    members = _archive_members(pcc_py_runtime_archive)
    assert "pcc_threads.o" not in members

    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected = {
        "pcc_threads_enabled": "freestanding_thread_kernel.o",
        "pcc_thread_stop_requested_acquire": "freestanding_thread_kernel.o",
        "pcc_thread_no_park_enter": "freestanding_thread_kernel.o",
        "pcc_thread_no_park_exit": "freestanding_thread_kernel.o",
        "pcc_thread_no_park_depth": "freestanding_thread_kernel.o",
        "pcc_thread_owns_stopped_world": "freestanding_thread_kernel.o",
        "pcc_thread_registration_waiter_count": "freestanding_thread_kernel.o",
        "pcc_thread_unregister_current": "freestanding_thread_kernel.o",
        "pcc_refcount_incref": "freestanding_thread_kernel.o",
        "pcc_mutex_new": "freestanding_thread_kernel.o",
        "py_virtual_thread_new": "py_virtual_thread_runtime.o",
        "py_virtual_thread_run_until_idle": "py_virtual_thread_runtime.o",
        "py_virtual_thread_effect_count": "py_virtual_thread_runtime.o",
        "pcc_debug_check_release": "freestanding_runtime_debug.o",
        "pcc_debug_check_tuple_slot": "freestanding_runtime_debug.o",
    }
    for symbol, owner in expected.items():
        assert owners[symbol] == {owner}


def test_c_oracle_thread_quiescence_symbols_have_exact_owner() -> None:
    from tests.runtime_build_cache import cached_c_runtime, cached_threaded_c_runtime

    symbols = {
        "pcc_thread_stop_requested_acquire",
        "pcc_thread_no_park_enter",
        "pcc_thread_no_park_exit",
        "pcc_thread_no_park_depth",
        "pcc_thread_owns_stopped_world",
        "pcc_thread_registration_waiter_count",
        "pcc_thread_unregister_current",
    }
    for runtime in (cached_c_runtime(), cached_threaded_c_runtime()):
        owners = _defined_symbol_owners(runtime / "libpy_runtime.a")
        for symbol in symbols:
            assert owners[symbol] == {"pcc_threads.o"}


def test_production_archive_has_no_handwritten_c_runtime_helpers(
    pcc_py_runtime_archive: Path,
) -> None:
    """Every member is bound to its pcc-Python source and object emitter."""
    manifest = verify_runtime_archive_manifest(
        pcc_py_runtime_archive,
        runtime_root=pcc_py_runtime_archive.parent,
    )
    records = manifest["members"]

    assert manifest["policy"] == PRODUCTION_POLICY
    assert manifest["member_count"] == len(records)
    assert {record["source_kind"] for record in records} == {"pcc-python"}
    assert {record["producer_kind"] for record in records} == {
        "pcc-python-library-ir-to-obj"
    }
    assert {record["uses_host_cc"] for record in records} == {False}


def test_c_api_core_runtime_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_core_runtime.o"}
    for symbol in {
        "Py_INCREF",
        "Py_DECREF",
        "pcc_capi_refcnt",
        "pcc_capi_set_refcnt",
        "PyTraceMalloc_Track",
        "PyTraceMalloc_Untrack",
        "PyEval_SaveThread",
        "PyEval_RestoreThread",
        "Py_IsInitialized",
        "PyUnstable_Object_IsUniqueReferencedTemporary",
        "PyUnstable_Object_IsUniquelyReferenced",
    }:
        assert owners[symbol] == expected_owner


def test_c_api_core_runtime_preserves_refcount_and_noop_contracts(
    tmp_path: Path,
    pcc_py_runtime_archive: Path,
) -> None:
    source = tmp_path / "capi_core_probe.c"
    executable = tmp_path / "capi_core_probe"
    source.write_text(
        r'''#include <stdint.h>
#include <stddef.h>

void Py_INCREF(void *);
void Py_DECREF(void *);
long pcc_capi_refcnt(void *);
void pcc_capi_set_refcnt(void *, long);
int PyTraceMalloc_Track(unsigned int, uintptr_t, size_t);
int PyTraceMalloc_Untrack(unsigned int, uintptr_t);
void *PyEval_SaveThread(void);
void PyEval_RestoreThread(void *);
int Py_IsInitialized(void);
int PyUnstable_Object_IsUniqueReferencedTemporary(void *);
int PyUnstable_Object_IsUniquelyReferenced(void *);
void *PyFloat_FromDouble(double);

int main(void) {
    if (Py_IsInitialized() != 1) return 1;
    if (PyEval_SaveThread() != 0) return 2;
    PyEval_RestoreThread((void *)0x1234);
    if (PyTraceMalloc_Track(1, 0x2000, 64) != 0) return 3;
    if (PyTraceMalloc_Untrack(1, 0x2000) != 0) return 4;

    void *obj = PyFloat_FromDouble(1.5);
    if (obj == 0) return 5;
    if (pcc_capi_refcnt(0) != 0) return 6;
    if (pcc_capi_refcnt((void *)1) != 0) return 7;
    if (pcc_capi_refcnt(obj) != 1) return 8;
    if (PyUnstable_Object_IsUniqueReferencedTemporary(obj) != 0) return 9;
    if (PyUnstable_Object_IsUniquelyReferenced(obj) != 1) return 10;

    Py_INCREF(obj);
    if (pcc_capi_refcnt(obj) != 2) return 11;
    if (PyUnstable_Object_IsUniquelyReferenced(obj) != 0) return 12;
    Py_DECREF(obj);
    pcc_capi_set_refcnt(obj, 3);
    if (pcc_capi_refcnt(obj) != 3) return 13;
    pcc_capi_set_refcnt(obj, 1);
    Py_DECREF(obj);
    return 0;
}
''',
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            os.environ.get("CC", "cc"),
            "-std=c11",
            str(source),
            str(pcc_py_runtime_archive),
            "-lm",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_c_api_memory_allocators_are_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """The PyMem/PyObject allocation facade is not owned by the C shim."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected = {
        "PyMem_Malloc",
        "PyMem_RawMalloc",
        "PyMem_Calloc",
        "PyMem_RawCalloc",
        "PyMem_Realloc",
        "PyMem_RawRealloc",
        "PyMem_Free",
        "PyMem_RawFree",
        "PyObject_Malloc",
        "PyObject_Calloc",
        "PyObject_Realloc",
        "PyObject_Free",
    }
    for symbol in expected:
        assert owners[symbol] == {"py_capi_memory_runtime.o"}


def test_c_api_memory_runtime_preserves_zero_size_realloc_and_overflow(
    tmp_path: Path,
    pcc_py_runtime_archive: Path,
) -> None:
    source = tmp_path / "capi_memory_probe.c"
    executable = tmp_path / "capi_memory_probe"
    source.write_text(
        r'''#include <stddef.h>
#include <stdint.h>

void *PyMem_Malloc(size_t);
void *PyMem_RawMalloc(size_t);
void *PyMem_Calloc(size_t, size_t);
void *PyMem_RawCalloc(size_t, size_t);
void *PyMem_Realloc(void *, size_t);
void *PyMem_RawRealloc(void *, size_t);
void PyMem_Free(void *);
void PyMem_RawFree(void *);
void *PyObject_Malloc(size_t);
void *PyObject_Calloc(size_t, size_t);
void *PyObject_Realloc(void *, size_t);
void PyObject_Free(void *);
int64_t py_err_occurred(void);
void py_clear_exception(void);

int main(void) {
    unsigned char *p = (unsigned char *)PyMem_Malloc(0);
    if (p == 0) return 1;
    p[0] = 0x5a;
    p = (unsigned char *)PyMem_Realloc(p, 16);
    if (p == 0 || p[0] != 0x5a) return 2;
    p = (unsigned char *)PyMem_Realloc(p, 0);
    if (p == 0) return 3;
    PyMem_Free(p);

    p = (unsigned char *)PyMem_Calloc(3, 4);
    if (p == 0) return 4;
    for (int i = 0; i < 12; i++) if (p[i] != 0) return 5;
    PyMem_Free(p);
    if (PyMem_Calloc((size_t)-1, 2) != 0) return 6;
    if (py_err_occurred() == 0) return 7;
    py_clear_exception();

    p = (unsigned char *)PyMem_RawMalloc(1);
    if (p == 0) return 8;
    p = (unsigned char *)PyMem_RawRealloc(p, 2);
    if (p == 0) return 9;
    PyMem_RawFree(p);
    p = (unsigned char *)PyMem_RawCalloc(0, 0);
    if (p == 0 || p[0] != 0) return 10;
    PyMem_RawFree(p);

    p = (unsigned char *)PyObject_Calloc(2, 2);
    if (p == 0) return 11;
    p[0] = 0x33;
    p = (unsigned char *)PyObject_Realloc(p, 8);
    if (p == 0 || p[0] != 0x33) return 12;
    PyObject_Free(p);
    p = (unsigned char *)PyObject_Malloc(1);
    if (p == 0) return 13;
    PyObject_Free(p);
    return 0;
}
''',
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            os.environ.get("CC", "cc"),
            "-std=c11",
            str(source),
            str(pcc_py_runtime_archive),
            "-lm",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_c_api_stdio_wrappers_are_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    assert owners["PyOS_snprintf"] == {"py_capi_stdio_runtime.o"}
    assert owners["PyOS_vsnprintf"] == {"py_capi_stdio_runtime.o"}


def test_c_api_stdio_wrappers_preserve_variadic_c_abi(
    tmp_path: Path,
    pcc_py_runtime_archive: Path,
) -> None:
    source = tmp_path / "capi_stdio_probe.c"
    executable = tmp_path / "capi_stdio_probe"
    source.write_text(
        r'''#include <stdarg.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>

int PyOS_snprintf(char *, size_t, const char *, ...);
int PyOS_vsnprintf(char *, size_t, const char *, va_list);

static int call_v(char *out, size_t size, const char *format, ...) {
    va_list ap;
    va_start(ap, format);
    int result = PyOS_vsnprintf(out, size, format, ap);
    va_end(ap);
    return result;
}

int main(void) {
    char first[32];
    char second[32];
    int n1 = PyOS_snprintf(first, sizeof(first), "%s:%d", "value", 17);
    int n2 = call_v(second, sizeof(second), "%04x", 255);
    if (n1 != 8 || strcmp(first, "value:17") != 0) {
        fprintf(stderr, "n1=%d first=%s\n", n1, first);
        return 1;
    }
    if (n2 != 4 || strcmp(second, "00ff") != 0) {
        fprintf(stderr, "n2=%d second=%s\n", n2, second);
        return 2;
    }
    return 0;
}
''',
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            os.environ.get("CC", "cc"),
            "-std=c11",
            str(source),
            str(pcc_py_runtime_archive),
            "-lm",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_c_api_bool_float_scalars_are_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    for symbol in {
        "PyBool_FromLong",
        "PyBool_Check",
        "PyFloat_FromDouble",
        "PyFloat_AsDouble",
        "PyFloat_Check",
        "PyFloat_CheckExact",
    }:
        assert owners[symbol] == {"py_capi_numeric_runtime.o"}


def test_c_api_bool_float_scalars_preserve_values_tags_and_errors(
    tmp_path: Path,
    pcc_py_runtime_archive: Path,
) -> None:
    source = tmp_path / "capi_bool_float_probe.c"
    executable = tmp_path / "capi_bool_float_probe"
    source.write_text(
        r'''#include <stdint.h>

void *PyBool_FromLong(long);
int PyBool_Check(void *);
void *PyFloat_FromDouble(double);
double PyFloat_AsDouble(void *);
int PyFloat_Check(void *);
int PyFloat_CheckExact(void *);
void *PyLong_FromLong(long);
void py_decref(void *);
int64_t py_err_occurred(void);
void py_clear_exception(void);
extern void *const py_None;

int main(void) {
    void *truth = PyBool_FromLong(-3);
    void *falsehood = PyBool_FromLong(0);
    if (!PyBool_Check(truth) || !PyBool_Check(falsehood)) return 1;
    if (PyBool_Check(py_None)) return 2;

    void *value = PyFloat_FromDouble(3.25);
    if (!PyFloat_Check(value) || !PyFloat_CheckExact(value)) return 3;
    if (PyFloat_AsDouble(value) != 3.25) return 4;
    py_decref(value);

    value = PyLong_FromLong(7);
    if (PyFloat_AsDouble(value) != 7.0) return 5;
    py_decref(value);
    if (PyFloat_AsDouble(py_None) != -1.0) return 6;
    if (py_err_occurred() == 0) return 7;
    py_clear_exception();
    return 0;
}
''',
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            os.environ.get("CC", "cc"),
            "-std=c11",
            str(source),
            str(pcc_py_runtime_archive),
            "-lm",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_c_api_signed_long_scalars_are_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    for symbol in {
        "PyLong_FromLong",
        "PyLong_FromLongLong",
        "PyLong_FromInt32",
        "PyLong_FromInt64",
        "PyLong_FromSsize_t",
        "PyLong_FromDouble",
        "PyLong_AsLong",
        "PyLong_AsInt",
        "PyLong_AsInt32",
        "PyLong_AsInt64",
        "PyLong_AsLongAndOverflow",
        "PyLong_AsLongLong",
        "PyLong_AsDouble",
        "PyLong_AsSsize_t",
        "PyLong_Check",
        "PyLong_CheckExact",
        "PyLong_IsZero",
    }:
        assert owners[symbol] == {"py_capi_numeric_runtime.o"}


def test_c_api_signed_long_scalars_preserve_bounds_and_overflow_direction(
    tmp_path: Path,
    pcc_py_runtime_archive: Path,
) -> None:
    source = tmp_path / "capi_signed_long_probe.c"
    executable = tmp_path / "capi_signed_long_probe"
    source.write_text(
        r'''#include <limits.h>
#include <math.h>
#include <stdint.h>

void *PyLong_FromLong(long);
void *PyLong_FromLongLong(long long);
void *PyLong_FromInt32(int32_t);
void *PyLong_FromInt64(int64_t);
void *PyLong_FromSsize_t(long);
void *PyLong_FromDouble(double);
void *PyLong_FromUnsignedLongLong(unsigned long long);
long PyLong_AsLong(void *);
int PyLong_AsInt(void *);
int PyLong_AsInt32(void *, int32_t *);
int PyLong_AsInt64(void *, int64_t *);
long PyLong_AsLongAndOverflow(void *, int *);
long long PyLong_AsLongLong(void *);
double PyLong_AsDouble(void *);
long PyLong_AsSsize_t(void *);
int PyLong_Check(void *);
int PyLong_CheckExact(void *);
int PyLong_IsZero(void *);
void py_decref(void *);
int64_t py_err_occurred(void);
void py_clear_exception(void);

int main(void) {
    void *value = PyLong_FromLong(-42);
    int32_t out32 = 0;
    int64_t out64 = 0;
    if (!PyLong_Check(value) || !PyLong_CheckExact(value)) return 1;
    if (PyLong_AsLong(value) != -42 || PyLong_AsLongLong(value) != -42) return 2;
    if (PyLong_AsInt(value) != -42 || PyLong_AsSsize_t(value) != -42) return 3;
    if (PyLong_AsInt32(value, &out32) != 0 || out32 != -42) return 4;
    if (PyLong_AsInt64(value, &out64) != 0 || out64 != -42) return 5;
    if (PyLong_AsDouble(value) != -42.0) return 6;
    py_decref(value);

    value = PyLong_FromInt32(INT32_MIN);
    if (PyLong_AsLong(value) != INT32_MIN) return 7;
    py_decref(value);
    value = PyLong_FromInt64(INT64_MIN);
    if (PyLong_AsLongLong(value) != INT64_MIN) return 8;
    py_decref(value);
    value = PyLong_FromLongLong(0);
    if (PyLong_IsZero(value) != 1) return 9;
    py_decref(value);
    value = PyLong_FromSsize_t(19);
    if (PyLong_AsSsize_t(value) != 19) return 10;
    py_decref(value);

    value = PyLong_FromDouble(12.75);
    if (PyLong_AsLong(value) != 12) return 11;
    py_decref(value);
    if (PyLong_FromDouble(NAN) != 0 || py_err_occurred() == 0) return 12;
    py_clear_exception();

    value = PyLong_FromUnsignedLongLong(ULLONG_MAX);
    int direction = 0;
    if (PyLong_AsLongAndOverflow(value, &direction) != -1) return 13;
    if (direction != 1 || py_err_occurred() != 0) return 14;
    py_decref(value);
    return 0;
}
''',
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            os.environ.get("CC", "cc"),
            "-std=c11",
            str(source),
            str(pcc_py_runtime_archive),
            "-lm",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_c_api_unsigned_long_scalars_are_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    for symbol in {
        "PyLong_FromUnsignedLong",
        "PyLong_FromUnsignedLongLong",
        "PyLong_FromUInt32",
        "PyLong_FromUInt64",
        "PyLong_FromVoidPtr",
        "pcc_py_long_from_void_ptr",
        "PyLong_FromSize_t",
        "PyLong_AsUInt32",
        "PyLong_AsUInt64",
        "PyLong_AsUnsignedLong",
        "PyLong_AsUnsignedLongLong",
        "PyLong_AsUnsignedLongLongMask",
        "PyLong_AsVoidPtr",
        "pcc_py_long_as_void_ptr",
        "PyLong_AsSize_t",
    }:
        assert owners[symbol] == {"py_capi_numeric_runtime.o"}


def test_c_api_unsigned_long_scalars_preserve_all_64_bits(
    tmp_path: Path,
    pcc_py_runtime_archive: Path,
) -> None:
    source = tmp_path / "capi_unsigned_long_probe.c"
    executable = tmp_path / "capi_unsigned_long_probe"
    source.write_text(
        r'''#include <limits.h>
#include <stddef.h>
#include <stdint.h>

void *PyLong_FromLong(long);
void *PyLong_FromUnsignedLong(unsigned long);
void *PyLong_FromUnsignedLongLong(unsigned long long);
void *PyLong_FromUInt32(uint32_t);
void *PyLong_FromUInt64(uint64_t);
void *PyLong_FromVoidPtr(void *);
void *PyLong_FromSize_t(size_t);
int PyLong_AsUInt32(void *, uint32_t *);
int PyLong_AsUInt64(void *, uint64_t *);
unsigned long PyLong_AsUnsignedLong(void *);
unsigned long long PyLong_AsUnsignedLongLong(void *);
unsigned long long PyLong_AsUnsignedLongLongMask(void *);
void *PyLong_AsVoidPtr(void *);
size_t PyLong_AsSize_t(void *);
void py_decref(void *);
int64_t py_err_occurred(void);
void py_clear_exception(void);

int main(void) {
    void *value = PyLong_FromUnsignedLongLong(ULLONG_MAX);
    uint64_t out64 = 0;
    if (PyLong_AsUnsignedLongLong(value) != ULLONG_MAX) return 1;
    if (PyLong_AsUInt64(value, &out64) != 0 || out64 != UINT64_MAX) return 2;
    py_decref(value);

    value = PyLong_FromUInt32(UINT32_MAX);
    uint32_t out32 = 0;
    if (PyLong_AsUInt32(value, &out32) != 0 || out32 != UINT32_MAX) return 3;
    py_decref(value);
    value = PyLong_FromUInt64(UINT64_C(0x8000000000000001));
    if (PyLong_AsUnsignedLong(value) != UINT64_C(0x8000000000000001)) return 4;
    py_decref(value);

    uintptr_t raw = UINT64_C(0xfedcba9876543210);
    value = PyLong_FromVoidPtr((void *)raw);
    if ((uintptr_t)PyLong_AsVoidPtr(value) != raw) return 5;
    py_decref(value);
    value = PyLong_FromSize_t((size_t)UINT64_C(0x9000000000000000));
    if (PyLong_AsSize_t(value) != (size_t)UINT64_C(0x9000000000000000)) return 6;
    py_decref(value);

    value = PyLong_FromLong(-1);
    if (PyLong_AsUnsignedLongLong(value) != ULLONG_MAX) return 7;
    if (py_err_occurred() == 0) return 8;
    py_clear_exception();
    if (PyLong_AsUnsignedLongLongMask(value) != ULLONG_MAX) return 9;
    py_decref(value);

    value = PyLong_FromUnsignedLongLong(ULLONG_MAX);
    if (PyLong_AsUInt32(value, &out32) != -1) return 10;
    if (py_err_occurred() == 0) return 11;
    py_clear_exception();
    py_decref(value);
    return 0;
}
''',
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            os.environ.get("CC", "cc"),
            "-std=c11",
            str(source),
            str(pcc_py_runtime_archive),
            "-lm",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_c_api_tuple_list_and_bytes_are_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    for symbol in {
        "PyTuple_Size",
        "PyTuple_GetItem",
        "PyTuple_New",
        "PyTuple_SetItem",
        "PyTuple_Pack",
        "PyTuple_Check",
        "PyTuple_CheckExact",
        "PyList_New",
        "PyList_SetItem",
        "PyList_GetItem",
        "PyList_GetItemRef",
        "PyList_Size",
        "PyList_Append",
        "PyList_AsTuple",
        "PyList_Check",
        "PyList_CheckExact",
        "PyBytes_FromStringAndSize",
        "PyBytes_FromString",
        "PyBytes_AsString",
        "PyBytes_AsStringAndSize",
        "PyBytes_Size",
        "PyBytes_Check",
        "PyBytes_CheckExact",
    }:
        assert owners[symbol] == {"py_capi_collections_runtime.o"}


def test_c_api_tuple_list_and_bytes_preserve_layout_refs_and_variadic_abi(
    tmp_path: Path,
    pcc_py_runtime_archive: Path,
) -> None:
    source = tmp_path / "capi_collections_probe.c"
    executable = tmp_path / "capi_collections_probe"
    source.write_text(
        r'''#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

typedef long Py_ssize_t;

Py_ssize_t PyTuple_Size(void *);
void *PyTuple_GetItem(void *, Py_ssize_t);
void *PyTuple_New(Py_ssize_t);
int PyTuple_SetItem(void *, Py_ssize_t, void *);
void *PyTuple_Pack(Py_ssize_t, ...);
int PyTuple_Check(void *);
int PyTuple_CheckExact(void *);

void *PyList_New(Py_ssize_t);
int PyList_SetItem(void *, Py_ssize_t, void *);
void *PyList_GetItem(void *, Py_ssize_t);
void *PyList_GetItemRef(void *, Py_ssize_t);
Py_ssize_t PyList_Size(void *);
int PyList_Append(void *, void *);
void *PyList_AsTuple(void *);
int PyList_Check(void *);
int PyList_CheckExact(void *);

void *PyBytes_FromStringAndSize(const char *, Py_ssize_t);
void *PyBytes_FromString(const char *);
char *PyBytes_AsString(void *);
int PyBytes_AsStringAndSize(void *, char **, Py_ssize_t *);
Py_ssize_t PyBytes_Size(void *);
int PyBytes_Check(void *);
int PyBytes_CheckExact(void *);

void py_decref(void *);
int64_t py_err_occurred(void);
void py_clear_exception(void);

int main(void) {
    void *alpha = PyBytes_FromString("alpha");
    void *beta = PyBytes_FromString("beta");
    if (alpha == 0 || beta == 0) return 1;

    void *tuple = PyTuple_New(2);
    if (tuple == 0 || !PyTuple_Check(tuple) || !PyTuple_CheckExact(tuple)) return 2;
    if (PyTuple_SetItem(tuple, 0, alpha) != 0) return 3;
    if (PyTuple_SetItem(tuple, 1, beta) != 0) return 4;
    if (PyTuple_Size(tuple) != 2) return 5;
    if (PyTuple_GetItem(tuple, 0) != alpha || PyTuple_GetItem(tuple, 1) != beta)
        return 6;

    void *packed = PyTuple_Pack(2, alpha, beta);
    if (packed == 0 || PyTuple_Size(packed) != 2) return 7;
    if (PyTuple_GetItem(packed, 0) != alpha || PyTuple_GetItem(packed, 1) != beta)
        return 8;

    void *list = PyList_New(2);
    void *left = PyBytes_FromString("left");
    void *right = PyBytes_FromString("right");
    if (list == 0 || left == 0 || right == 0) return 9;
    if (!PyList_Check(list) || !PyList_CheckExact(list)) return 10;
    if (PyList_SetItem(list, 0, left) != 0) return 11;
    if (PyList_SetItem(list, 1, right) != 0) return 12;
    if (PyList_Size(list) != 2 || PyList_GetItem(list, 0) != left) return 13;
    void *owned_left = PyList_GetItemRef(list, 0);
    if (owned_left != left) return 14;
    py_decref(owned_left);

    void *tail = PyBytes_FromString("tail");
    if (tail == 0 || PyList_Append(list, tail) != 0) return 15;
    py_decref(tail);
    if (PyList_Size(list) != 3) return 16;
    void *from_list = PyList_AsTuple(list);
    if (from_list == 0 || PyTuple_Size(from_list) != 3) return 17;
    if (PyTuple_GetItem(from_list, 1) != right) return 18;

    const char raw[3] = {'a', '\0', 'b'};
    void *bytes = PyBytes_FromStringAndSize(raw, 3);
    char *buffer = 0;
    Py_ssize_t length = -1;
    if (bytes == 0 || !PyBytes_Check(bytes) || !PyBytes_CheckExact(bytes)) return 19;
    if (PyBytes_Size(bytes) != 3) return 20;
    if (PyBytes_AsStringAndSize(bytes, &buffer, &length) != 0) return 21;
    if (length != 3 || memcmp(buffer, raw, 3) != 0 || buffer[3] != '\0') return 22;
    if (PyBytes_AsString(bytes) != buffer) return 23;
    if (PyBytes_AsStringAndSize(bytes, &buffer, 0) != -1) return 24;
    if (py_err_occurred() == 0) return 25;
    py_clear_exception();

    if (PyTuple_New(-1) != 0 || py_err_occurred() == 0) return 26;
    py_clear_exception();
    if (PyList_New(-1) != 0 || py_err_occurred() == 0) return 27;
    py_clear_exception();
    if (PyBytes_FromStringAndSize(raw, -1) != 0 || py_err_occurred() == 0)
        return 28;
    py_clear_exception();

    py_decref(bytes);
    py_decref(from_list);
    py_decref(list);
    py_decref(packed);
    py_decref(tuple);
    return 0;
}
''',
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            os.environ.get("CC", "cc"),
            "-std=c11",
            str(source),
            str(pcc_py_runtime_archive),
            "-lm",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_explicit_thread_runtime_is_owned_by_pcc_python() -> None:
    from tests.runtime_build_cache import cached_threaded_pcc_python_runtime

    runtime = cached_threaded_pcc_python_runtime()
    archive = runtime / "libpy_runtime_pcc_py.a"
    members = _archive_members(archive)
    assert "pcc_threads.o" not in members

    owners = _defined_symbol_owners(archive)
    assert owners["pcc_threads_enabled"] == {
        "freestanding_thread_kernel_pthread.o"
    }
    assert owners["pcc_thread_start"] == {
        "freestanding_thread_kernel_pthread.o"
    }
    for symbol in [
        "pcc_thread_no_park_enter",
        "pcc_thread_stop_requested_acquire",
        "pcc_thread_no_park_exit",
        "pcc_thread_no_park_depth",
        "pcc_thread_owns_stopped_world",
        "pcc_thread_registration_waiter_count",
        "pcc_thread_unregister_current",
    ]:
        assert owners[symbol] == {"freestanding_thread_kernel_pthread.o"}
    assert owners["py_virtual_thread_new"] == {"py_virtual_thread_runtime.o"}
    assert owners["pcc_debug_check_release"] == {
        "freestanding_runtime_debug.o"
    }

    thread_ir = (
        runtime / "build_py" / "freestanding_thread_kernel_pthread.ll"
    ).read_text(encoding="utf-8")
    safepoint_body = thread_ir.split("@pcc_thread_safepoint() {", 1)[1].split(
        "\n}\n", 1
    )[0]
    assert "call void @pcc_thread_safepoint()" not in safepoint_body


def test_explicit_pcc_python_thread_kernel_starts_and_joins(
    tmp_path: Path,
) -> None:
    from tests.runtime_build_cache import cached_threaded_pcc_python_runtime

    runtime = cached_threaded_pcc_python_runtime()
    archive = runtime / "libpy_runtime_pcc_py.a"
    source = tmp_path / "thread_kernel_probe.c"
    executable = tmp_path / "thread_kernel_probe"
    source.write_text(
        r'''#include "py_internal.h"
#include <stdint.h>

static PccMutex *lock;
static int64_t value;
static int64_t safepoint_started;
static int64_t safepoint_release;

static void *worker(void *arg) {
    if (pcc_mutex_lock(lock) != 0) return (void *)(intptr_t)2;
    value += (int64_t)(intptr_t)arg;
    if (pcc_mutex_unlock(lock) != 0) return (void *)(intptr_t)3;
    return (void *)(intptr_t)7;
}

static void *safepoint_worker(void *arg) {
    (void)arg;
    __atomic_store_n(&safepoint_started, 1, __ATOMIC_RELEASE);
    while (__atomic_load_n(&safepoint_release, __ATOMIC_ACQUIRE) == 0) {
        pcc_thread_safepoint();
    }
    return 0;
}

int main(void) {
    if (pcc_threads_enabled() != 1) return 10;
    if (pcc_refcount_strategy() != PCC_REFCOUNT_STRATEGY_ATOMIC) return 11;
    lock = pcc_mutex_new();
    if (lock == 0) return 12;
    PccThreadHandle *thread = 0;
    if (pcc_thread_start(&thread, worker, (void *)(intptr_t)5) != 0) return 13;
    void *result = 0;
    if (pcc_thread_join(thread, &result) != 0) return 14;
    if ((int64_t)(intptr_t)result != 7 || value != 5) return 15;
    if (pcc_thread_start(&thread, safepoint_worker, 0) != 0) return 16;
    while (__atomic_load_n(&safepoint_started, __ATOMIC_ACQUIRE) == 0) {}
    if (pcc_stop_the_world() != 0) return 17;
    if (pcc_resume_world() != 0) return 18;
    __atomic_store_n(&safepoint_release, 1, __ATOMIC_RELEASE);
    if (pcc_thread_join(thread, &result) != 0 || result != 0) return 19;
    pcc_mutex_free(lock);
    return 0;
}
''',
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            os.environ.get("CC", "cc"),
            "-std=c11",
            "-pthread",
            f"-I{runtime / 'include'}",
            f"-I{runtime / 'src'}",
            str(source),
            str(archive),
            "-lm",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_explicit_pcc_python_thread_kernel_serializes_stw_requesters(
    tmp_path: Path,
) -> None:
    from tests.runtime_build_cache import cached_threaded_pcc_python_runtime

    runtime = cached_threaded_pcc_python_runtime()
    source = tmp_path / "thread_kernel_concurrent_stw.c"
    executable = tmp_path / "thread_kernel_concurrent_stw"
    source.write_text(
        textwrap.dedent(
            r'''
            #include "py_internal.h"
            #include <stdint.h>

            static int64_t worker_started;
            static int64_t worker_owned_stop;

            static void *worker(void *arg) {
                (void)arg;
                __atomic_store_n(&worker_started, 1, __ATOMIC_RELEASE);
                if (pcc_stop_the_world() != 0) return (void *)(intptr_t)2;
                __atomic_store_n(&worker_owned_stop, 1, __ATOMIC_RELEASE);
                if (pcc_resume_world() != 0) return (void *)(intptr_t)3;
                return 0;
            }

            int main(void) {
                (void)pcc_current_thread_id();
                PccThreadHandle *thread = 0;
                if (pcc_thread_start(&thread, worker, 0) != 0) return 10;
                while (__atomic_load_n(&worker_started, __ATOMIC_ACQUIRE) == 0) {}
                while (__atomic_load_n(
                    &pcc_thread_stop_requested, __ATOMIC_ACQUIRE
                ) == 0) {}
                if (pcc_resume_world() != -1) return 11;
                if (pcc_stop_the_world() != 0) return 12;
                if (__atomic_load_n(&worker_owned_stop, __ATOMIC_ACQUIRE) != 1) {
                    return 13;
                }
                if (pcc_resume_world() != 0) return 14;
                void *result = 0;
                if (pcc_thread_join(thread, &result) != 0) return 15;
                if (result != 0) return 16;
                return 0;
            }
            '''
        ).lstrip(),
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            os.environ.get("CC", "cc"),
            "-std=c11",
            "-pthread",
            f"-I{runtime / 'include'}",
            f"-I{runtime / 'src'}",
            str(source),
            str(runtime / "libpy_runtime_pcc_py.a"),
            "-lm",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_small_semantic_helpers_are_owned_by_existing_pcc_python_modules(
    pcc_py_runtime_archive: Path,
) -> None:
    members = _archive_members(pcc_py_runtime_archive)
    assert {
        "py_enumerate.o",
        "py_obj_min_max.o",
        "py_tuple_methods.o",
    }.isdisjoint(members)

    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    assert owners["py_enumerate_list"] == {"py_iter.o"}
    assert owners["py_obj_min_max"] == {"py_obj_ops_compare.o"}
    assert owners["py_tuple_count"] == {"py_tuple.o"}
    assert owners["py_tuple_index"] == {"py_tuple.o"}
    assert owners["py_tuple_index_range"] == {"py_tuple.o"}


def test_os_native_path_semantics_are_owned_by_pcc_python_path_module(
    pcc_py_runtime_archive: Path,
) -> None:
    members = _archive_members(pcc_py_runtime_archive)
    assert "py_os_native.o" not in members
    assert "py_os_path.o" in members

    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_os_path.o"}
    assert owners["py_os_path_commonpath"] == expected_owner
    assert owners["py_os_path_expandvars"] == expected_owner
    assert owners["py_os_path_relpath"] == expected_owner


def test_complex_power_is_owned_by_existing_pcc_python_complex_module(
    pcc_py_runtime_archive: Path,
) -> None:
    members = _archive_members(pcc_py_runtime_archive)
    assert "py_complex_pow.o" not in members
    assert "py_obj_stubs.o" in members

    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    assert owners["py_complex_pow"] == {"py_obj_stubs.o"}


def test_float_fromhex_is_owned_by_existing_pcc_python_float_module(
    pcc_py_runtime_archive: Path,
) -> None:
    members = _archive_members(pcc_py_runtime_archive)
    assert "py_float_fromhex.o" not in members
    assert "py_obj_stubs.o" in members

    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    assert owners["py_float_fromhex"] == {"py_obj_stubs.o"}


def test_rss_sampling_is_owned_by_freestanding_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    members = _archive_members(pcc_py_runtime_archive)
    assert "py_os_rss.o" not in members
    assert "freestanding_platform_rss.o" in members

    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"freestanding_platform_rss.o"}
    assert owners["pcc_os_current_rss_bytes"] == expected_owner
    assert owners["pcc_os_peak_rss_bytes"] == expected_owner


def test_metal_prebuilt_bridge_is_owned_by_freestanding_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    members = _archive_members(pcc_py_runtime_archive)
    assert "pcc_metal_runtime.o" not in members
    assert "freestanding_metal_runtime.o" in members

    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"freestanding_metal_runtime.o"}
    for symbol in (
        "pcc_metal_source_runtime_call_prebuilt",
        "pcc_metal_metallib_runtime_call_prebuilt",
        "pcc_metal_buffer_runtime_create_prebuilt",
        "pcc_metal_buffer_runtime_length_prebuilt",
        "pcc_metal_buffer_runtime_write_prebuilt",
        "pcc_metal_buffer_runtime_read_prebuilt",
        "pcc_metal_buffer_runtime_release_prebuilt",
    ):
        assert owners[symbol] == expected_owner


def test_json_runtime_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    members = _archive_members(pcc_py_runtime_archive)
    assert "py_json.o" not in members
    assert "py_json_runtime.o" in members

    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_json_runtime.o"}
    assert owners["py_json_loads"] == expected_owner
    assert owners["py_json_dumps_ex"] == expected_owner
    assert owners["py_json_dumps"] == expected_owner


def test_copy_pickle_runtime_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    members = _archive_members(pcc_py_runtime_archive)
    assert "py_pickle_copy.o" not in members
    assert "py_pickle_copy_runtime.o" in members

    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_pickle_copy_runtime.o"}
    assert owners["py_copy_copy"] == expected_owner
    assert owners["py_copy_deepcopy"] == expected_owner
    assert owners["py_pickle_dumps"] == expected_owner
    assert owners["py_pickle_loads"] == expected_owner


def test_user_protocol_runtime_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    members = _archive_members(pcc_py_runtime_archive)
    assert "py_protocol.o" not in members
    assert "py_protocol_runtime.o" in members

    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_protocol_runtime.o"}
    for symbol in (
        "py_user_len_dispatch",
        "py_user_abs_dispatch",
        "py_user_bool_dispatch",
        "py_obj_index_i64",
        "py_user_contains_dispatch",
        "py_user_eq_dispatch",
        "py_user_getitem_dispatch",
        "py_user_matmul_dispatch",
        "py_user_binop_dispatch",
        "py_obj_floordiv",
        "py_obj_inplace_op",
        "py_user_setitem_dispatch",
        "py_user_delitem_dispatch",
        "py_dict_subclass_getattr",
        "py_dict_subclass_getitem",
    ):
        assert owners[symbol] == expected_owner


def test_extension_loader_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    members = _archive_members(pcc_py_runtime_archive)
    assert "py_extension_loader.o" not in members
    assert "py_extension_loader_runtime.o" in members

    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_extension_loader_runtime.o"}
    assert owners["py_native_extension_import"] == expected_owner
    assert owners["py_native_extension_import_by_name"] == expected_owner


def test_io_waitset_is_owned_by_freestanding_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    members = _archive_members(pcc_py_runtime_archive)
    assert "py_io_waitset.o" not in members
    assert "freestanding_io_waitset.o" in members

    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"freestanding_io_waitset.o"}
    for symbol in (
        "pcc_io_waitset_init",
        "pcc_io_waitset_dispose",
        "pcc_io_waitset_add",
        "pcc_io_waitset_remove",
        "pcc_io_waitset_count",
        "pcc_io_waitset_set_ready",
        "pcc_io_waitset_clear_ready",
        "pcc_io_waitset_wait",
        "pcc_io_waitset_wait_until",
        "pcc_io_waitset_interrupt",
        "pcc_io_waitset_wait_prepare",
        "pcc_io_waitset_wait_block",
        "pcc_io_waitset_wait_finish",
        "pcc_io_waitset_wait_discard",
        "pcc_io_waitset_kqueue_available",
        "pcc_io_waitset_real_kqueue_skip",
        "pcc_io_waitset_epoll_available",
        "pcc_io_waitset_real_epoll_skip",
        "pcc_io_waitset_backend_label",
        "pcc_io_waitset_default_backend",
    ):
        assert owners[symbol] == expected_owner


def test_http_and_sha256_runtime_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    members = _archive_members(pcc_py_runtime_archive)
    assert "py_http.o" not in members
    assert "py_hash.o" not in members
    assert "py_http_runtime.o" in members
    assert "py_hash_runtime.o" in members

    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    for symbol in (
        "py_sha256_file_hex", "py_sha256_file_hex_bounded",
        "py_sha256_bytes_digest", "py_sha256_state_new",
        "py_sha256_state_update", "py_sha256_state_digest",
    ):
        assert owners[symbol] == {"py_hash_runtime.o"}
    assert owners["py_http_download_to_file"] == {"py_http_runtime.o"}


def test_asyncio_socket_runtime_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    members = _archive_members(pcc_py_runtime_archive)
    assert "py_asyncio_io.o" not in members
    assert "py_asyncio_io_runtime.o" in members

    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_asyncio_io_runtime.o"}
    for symbol in (
        "py_asyncio_tcp_listen",
        "py_asyncio_tcp_accept",
        "py_asyncio_tcp_connect",
        "py_asyncio_fd_recv",
        "py_asyncio_fd_send_all",
        "py_asyncio_fd_relay",
        "py_asyncio_fd_relay_step",
        "py_asyncio_fd_relay_step_last_progress",
        "py_asyncio_fd_close",
        "py_asyncio_fd_sockname",
        "py_asyncio_fd_peername",
        "py_asyncio_io_waitset_backend",
    ):
        assert owners[symbol] == expected_owner


def test_class_attribute_runtime_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    members = _archive_members(pcc_py_runtime_archive)
    assert "py_class_attrs.o" not in members
    assert "py_class.o" in members

    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_class.o"}
    for symbol in (
        "py_instance_bind_method",
        "py_classmethod_new",
        "py_property_new",
        "py_class_attrs_dict",
        "py_class_getattr",
        "py_class_setattr_raw",
        "py_class_setattr",
        "py_class_apply_namespace_dict",
        "py_class_new_from_objects",
        "py_class_delattr",
        "py_class_attrs_dispose",
        "py_class_attrs_retarget",
    ):
        assert owners[symbol] == expected_owner


def test_format_runtime_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    members = _archive_members(pcc_py_runtime_archive)
    assert "py_format.o" not in members
    assert "py_format_runtime.o" in members

    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_format_runtime.o"}
    for symbol in (
        "pcc_float_round_fixed_f64",
        "py_bytes_mod",
        "py_complex_abs",
        "py_complex_conjugate",
        "py_complex_div",
        "py_complex_mul",
        "py_complex_neg",
        "py_complex_repr",
        "py_complex_sub",
        "py_exc_repr",
        "py_float_repr_shortest",
        "py_float_value_of",
        "py_format_cpy_object_hook",
        "py_format_try_cpy_object_into_fd",
        "py_obj_format",
        "py_str_mod",
    ):
        assert owners[symbol] == expected_owner


def test_residual_numeric_libc_is_owned_by_freestanding_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    members = _archive_members(pcc_py_runtime_archive)
    assert {
        "vendor_atoi.o",
        "vendor_strtod.o",
        "vendor_isdigit.o",
        "vendor_isspace.o",
        "vendor_floatscan.o",
        "vendor_pcc_scan_uflow.o",
        "vendor_shgetc.o",
        "vendor___math_invalid.o",
        "vendor___math_oflow.o",
        "vendor___math_uflow.o",
        "vendor___math_xflow.o",
        "vendor_copysign.o",
        "vendor_exp_data.o",
        "vendor_fabs.o",
        "vendor_fma.o",
        "vendor_fmod.o",
        "vendor_pow_data.o",
        "vendor_pow.o",
        "vendor_scalbn.o",
        "py_libc_fortify.o",
    }.isdisjoint(members)

    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"freestanding_libc_numeric.o"}
    for symbol in {
        "atan2",
        "cos",
        "exp",
        "fabs",
        "floor",
        "fmod",
        "hypot",
        "log",
        "pow",
        "rint",
        "scalbn",
        "sin",
        "sqrt",
        "strtod",
    }:
        assert owners[symbol] == expected_owner, symbol


def test_regex_object_bridge_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    members = _archive_members(pcc_py_runtime_archive)
    assert "py_re_engine_obj.o" not in members
    assert "py_re_engine_runtime.o" in members

    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_re_engine_runtime.o"}
    for symbol in (
        "py_re_engine_findall",
        "py_re_engine_sub",
        "py_re_engine_split",
        "py_re_engine_truth",
        "py_re_engine_truth_flags",
        "py_re_engine_truth_flags_from",
        "py_re_engine_fullmatch_flags",
        "py_re_compile_obj",
        "py_re_engine_pattern_supported",
    ):
        assert owners[symbol] == expected_owner


def test_regex_core_is_owned_by_freestanding_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    members = _archive_members(pcc_py_runtime_archive)
    assert "py_re_engine.o" not in members
    assert "freestanding_re_engine.o" in members

    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"freestanding_re_engine.o"}
    for symbol in (
        "pcc_re_engine_supported",
        "pcc_re_engine_supported_flags",
        "pcc_re_engine_compile_count",
        "pcc_re_engine_run_flags",
        "pcc_re_engine_run_from",
        "pcc_re_engine_run",
        "pcc_re_engine_group_names_flags",
        "pcc_re_engine_group_names",
    ):
        assert owners[symbol] == expected_owner


def test_freestanding_regex_core_reuses_compiled_program(
    tmp_path: Path,
    pcc_py_runtime_archive: Path,
) -> None:
    source = tmp_path / "freestanding_re_cache_probe.c"
    executable = tmp_path / "freestanding_re_cache_probe"
    source.write_text(
        r'''
#include <stdint.h>

int pcc_re_engine_supported(const char *pattern);
int64_t pcc_re_engine_compile_count(void);
int pcc_re_engine_run(const char *pattern, const char *text, int64_t text_len,
                      int is_search, int64_t *caps, int caps_len,
                      int64_t *ngroups_out);

int main(void) {
    int64_t caps[64];
    int64_t groups = -1;
    int64_t before = pcc_re_engine_compile_count();
    if (!pcc_re_engine_supported("a+")) return 1;
    if (!pcc_re_engine_supported("a+")) return 2;
    if (pcc_re_engine_run("a+", "aa", 2, 0, caps, 64, &groups) != 1) return 3;
    if (groups != 0 || caps[0] != 0 || caps[1] != 2) return 4;
    if (pcc_re_engine_compile_count() != before + 1) return 5;
    return 0;
}
''',
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            "clang",
            str(source),
            str(pcc_py_runtime_archive),
            "-pthread",
            "-lm",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_pcc_python_path_semantics_cover_components_environment_and_relpath(
    tmp_path: Path,
    pcc_py_runtime_archive: Path,
) -> None:
    source = tmp_path / "os_path_pcc_python_probe.c"
    executable = tmp_path / "os_path_pcc_python_probe"
    source.write_text(
        r'''
#include "py_runtime.h"
#include <stdint.h>

int64_t pcc_platform_setenv(
    const char *name, const char *value, int64_t overwrite
);

static int expect_text(PyObject *value, const char *text, int64_t length) {
    if (value == 0) return 0;
    PyObject *expected = py_str_new(text, length);
    if (expected == 0) return 0;
    int ok = py_str_eq(value, expected);
    py_decref(expected);
    py_decref(value);
    return ok;
}

int main(void) {
    PyObject *paths = py_tuple_new(2);
    if (paths == 0) return 1;
    py_tuple_set_item(paths, 0, py_str_new("/srv/pkg/a", 10));
    py_tuple_set_item(paths, 1, py_str_new("/srv/pkg/b", 10));
    if (!expect_text(py_os_path_commonpath(paths), "/srv/pkg", 8)) return 2;
    py_decref(paths);

    PyObject *boundary = py_list_new(2);
    if (boundary == 0) return 3;
    PyObject *left = py_str_new("/srv/pkg", 8);
    PyObject *right = py_str_new("/srv/package", 12);
    if (left == 0 || right == 0) return 4;
    py_list_append(boundary, left);
    py_list_append(boundary, right);
    py_decref(left);
    py_decref(right);
    if (!expect_text(py_os_path_commonpath(boundary), "/srv", 4)) return 5;
    py_decref(boundary);

    PyObject *empty = py_tuple_new(0);
    if (empty == 0) return 6;
    if (!expect_text(py_os_path_commonpath(empty), "", 0)) return 7;
    py_decref(empty);

    if (pcc_platform_setenv("PCC_PATH_VALUE", "alpha/beta", 1) != 0) return 8;
    PyObject *expanded_input = py_str_new(
        "$PCC_PATH_VALUE/${PCC_PATH_VALUE}/$PCC_PATH_MISSING/"
        "${PCC_PATH_VALUE",
        68
    );
    if (!expect_text(
            py_os_path_expandvars(expanded_input),
            "alpha/beta/alpha/beta/$PCC_PATH_MISSING/${PCC_PATH_VALUE",
            56
        )) return 9;
    py_decref(expanded_input);

    PyObject *dollars = py_str_new("$$PCC_PATH_VALUE", 16);
    if (!expect_text(py_os_path_expandvars(dollars), "$alpha/beta", 11)) return 10;
    py_decref(dollars);

    PyObject *path = py_str_new("/a/./b/../c", 11);
    PyObject *start = py_str_new("/a", 2);
    if (!expect_text(py_os_path_relpath(path, start), "c", 1)) return 11;
    py_decref(path);
    py_decref(start);

    path = py_str_new("/a/x", 4);
    start = py_str_new("/a/b/c", 6);
    if (!expect_text(py_os_path_relpath(path, start), "../../x", 7)) return 12;
    py_decref(path);
    py_decref(start);

    path = py_str_new("/same/path", 10);
    start = py_str_new("/same/path", 10);
    if (!expect_text(py_os_path_relpath(path, start), ".", 1)) return 13;
    py_decref(path);
    py_decref(start);
    return 0;
}
''',
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            "clang",
            "-std=c11",
            f"-I{REPO / 'pcc' / 'py_runtime' / 'include'}",
            str(source),
            str(pcc_py_runtime_archive),
            "-pthread",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    result = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_int_bytes_is_owned_by_pcc_python_int_conversion(
    pcc_py_runtime_archive: Path,
) -> None:
    members = _archive_members(pcc_py_runtime_archive)
    assert "py_int_bytes.o" not in members

    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    assert owners["py_int_to_bytes"] == {"py_int_convert.o"}
    assert owners["py_int_from_bytes"] == {"py_int_convert.o"}


def test_modexp_and_isqrt_are_owned_by_pcc_python_int_ops(
    pcc_py_runtime_archive: Path,
) -> None:
    members = _archive_members(pcc_py_runtime_archive)
    assert "py_int_modexp.o" not in members

    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    assert owners["py_int_pow_mod"] == {"py_int_ops.o"}
    assert owners["py_int_isqrt"] == {"py_int_ops.o"}


def test_cpy_handle_is_owned_by_pcc_python_object_deallocation(
    pcc_py_runtime_archive: Path,
) -> None:
    members = _archive_members(pcc_py_runtime_archive)
    assert "py_cpy_handle.o" not in members

    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_obj_dealloc.o"}
    assert owners["py_cpy_handle_set_release_fn"] == expected_owner
    assert owners["py_cpy_handle_new"] == expected_owner
    assert owners["py_cpy_handle_get"] == expected_owner
    assert owners["pcc_cpy_handle_move_owned_ref"] == expected_owner
    assert owners["py_dealloc_cpy_handle"] == expected_owner


def test_pcc_python_cpy_handle_releases_foreign_reference_once(
    tmp_path: Path,
    pcc_py_runtime_archive: Path,
) -> None:
    repo = Path(__file__).resolve().parents[2]
    source = tmp_path / "cpy_handle_probe.c"
    executable = tmp_path / "cpy_handle_probe"
    source.write_text(
        r'''
#include "py_runtime.h"
#include <stdint.h>

static int release_hits = 0;

static void release_foreign(void *ptr) {
    if (ptr == (void *)(uintptr_t)0x2000) release_hits++;
}

int main(void) {
    py_cpy_handle_set_release_fn(release_foreign);
    PyObject *handle = py_cpy_handle_new((void *)(uintptr_t)0x2000);
    if (handle == 0) return 2;
    if (py_cpy_handle_get(handle) != (void *)(uintptr_t)0x2000) return 3;
    py_decref(handle);
    if (release_hits != 1) return 4;
    return 0;
}
''',
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            "clang",
            "-std=c11",
            f"-I{repo / 'pcc' / 'py_runtime' / 'include'}",
            f"-I{repo / 'pcc' / 'py_runtime' / 'src'}",
            str(source),
            str(pcc_py_runtime_archive),
            "-pthread",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    result = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_context_runtime_recipe_uses_only_the_pcc_python_owner() -> None:
    makefile = RUNTIME_MAKEFILE.read_text(encoding="utf-8")
    helper_lines = [
        line
        for line in makefile.splitlines()
        if line.startswith("OBJ_PY_CC_HELPERS")
    ]
    assert not any("py_context.o" in line for line in helper_lines)
    assert "py_context_runtime" in _make_variable_tokens(makefile, "PY_MODULES")
    assert "py_context" in _make_variable_tokens(makefile, "PY_REPLACED_C_MODULES")


def test_context_runtime_symbols_are_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    members = _archive_members(pcc_py_runtime_archive)
    assert "py_context.o" not in members
    assert "py_context_runtime.o" in members

    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    assert owners["py_context_enter"] == {"py_context_runtime.o"}
    assert owners["py_context_exit"] == {"py_context_runtime.o"}


def test_call_splat_recipe_uses_only_the_pcc_python_owner() -> None:
    makefile = RUNTIME_MAKEFILE.read_text(encoding="utf-8")
    helper_lines = [
        line
        for line in makefile.splitlines()
        if line.startswith("OBJ_PY_CC_HELPERS")
    ]
    assert not any("py_call_splat.o" in line for line in helper_lines)
    assert "py_call_splat_runtime" in _make_variable_tokens(makefile, "PY_MODULES")
    assert "py_call_splat" in _make_variable_tokens(
        makefile, "PY_REPLACED_C_MODULES"
    )


def test_call_splat_symbols_are_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    members = _archive_members(pcc_py_runtime_archive)
    assert "py_call_splat.o" not in members
    assert "py_call_splat_runtime.o" in members

    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_call_splat_runtime.o"}
    assert owners["py_call_merge_posargs"] == expected_owner
    assert owners["py_zip_star"] == expected_owner
    assert owners["py_call_merge_kwargs"] == expected_owner
    assert owners["py_obj_call_splat"] == expected_owner


def test_module_attrs_recipe_uses_only_the_pcc_python_owner() -> None:
    makefile = RUNTIME_MAKEFILE.read_text(encoding="utf-8")
    helper_lines = [
        line
        for line in makefile.splitlines()
        if line.startswith("OBJ_PY_CC_HELPERS")
    ]
    assert not any("py_module_attrs.o" in line for line in helper_lines)
    assert "py_module_attrs_runtime" in _make_variable_tokens(
        makefile, "PY_MODULES"
    )
    assert "py_module_attrs" in _make_variable_tokens(
        makefile, "PY_REPLACED_C_MODULES"
    )


def test_module_attrs_symbols_are_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    members = _archive_members(pcc_py_runtime_archive)
    assert "py_module_attrs.o" not in members
    assert "py_module_attrs_runtime.o" in members

    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_module_attrs_runtime.o"}
    assert owners["py_module_attrs_dict"] == expected_owner
    assert owners["py_module_attr_set"] == expected_owner
    assert owners["py_module_attr_get"] == expected_owner
    assert owners["py_module_import_star"] == expected_owner
    assert owners["py_module_attr_value_or_default"] == expected_owner
    assert owners["py_module_attr_del"] == expected_owner
    assert owners["py_module_attr_len"] == expected_owner
    assert owners["py_func_code_class_cache"] == expected_owner


def test_pcc_python_module_attrs_side_table_roundtrip(
    tmp_path: Path,
    pcc_py_runtime_archive: Path,
) -> None:
    source = tmp_path / "module_attrs_pcc_python_probe.c"
    executable = tmp_path / "module_attrs_pcc_python_probe"
    source.write_text(
        r'''
#include "py_runtime.h"

int main(void) {
    if (py_module_attr_len("m") != 0) return 1;
    PyObject *value = py_int_from_i64(42);
    if (py_module_attr_set("m", "x", value) != 0) return 2;
    py_decref(value);
    if (py_module_attr_len("m") != 1) return 3;
    PyObject *got = py_module_attr_get("m", "x");
    if (got == 0 || py_int_to_i64(got, 0) != 42) return 4;
    py_decref(got);
    if (py_module_attr_del("m", "x") != 0) return 5;
    if (py_module_attr_len("m") != 0) return 6;
    return 0;
}
''',
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            "clang",
            "-std=c11",
            f"-I{REPO / 'pcc' / 'py_runtime' / 'include'}",
            f"-I{REPO / 'pcc' / 'py_runtime' / 'src'}",
            str(source),
            str(pcc_py_runtime_archive),
            "-pthread",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    result = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_compiled_module_recipe_uses_only_the_pcc_python_owner() -> None:
    makefile = RUNTIME_MAKEFILE.read_text(encoding="utf-8")
    helper_lines = [
        line
        for line in makefile.splitlines()
        if line.startswith("OBJ_PY_CC_HELPERS")
    ]
    assert not any("py_compiled_module.o" in line for line in helper_lines)
    assert "py_compiled_module_runtime" in _make_variable_tokens(
        makefile, "PY_MODULES"
    )
    assert "py_compiled_module" in _make_variable_tokens(
        makefile, "PY_REPLACED_C_MODULES"
    )


def test_compiled_module_symbols_are_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    members = _archive_members(pcc_py_runtime_archive)
    assert "py_compiled_module.o" not in members
    assert "py_compiled_module_runtime.o" in members

    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_compiled_module_runtime.o"}
    assert owners["pcc_runtime_module_class"] == expected_owner
    assert owners["py_compiled_module_register_init"] == expected_owner
    assert owners["py_compiled_module_ensure_parent_packages"] == expected_owner
    assert owners["py_compiled_module_import_by_name"] == expected_owner


def test_heap_metrics_are_owned_by_the_freestanding_allocator(
    pcc_py_runtime_archive: Path,
) -> None:
    members = _archive_members(pcc_py_runtime_archive)
    assert "py_os_heap.o" not in members

    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"freestanding_allocator.o"}
    assert owners["pcc_os_heap_in_use_bytes"] == expected_owner
    assert owners["pcc_os_heap_capacity_bytes"] == expected_owner


def test_timer_heap_is_owned_by_freestanding_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    members = _archive_members(pcc_py_runtime_archive)
    assert "py_timer_heap.o" not in members
    assert "freestanding_timer_heap.o" in members

    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"freestanding_timer_heap.o"}
    for symbol in (
        "pcc_timer_heap_init",
        "pcc_timer_heap_dispose",
        "pcc_timer_heap_insert",
        "pcc_timer_heap_cancel",
        "pcc_timer_heap_is_registered",
        "pcc_timer_heap_size",
        "pcc_timer_heap_peek",
        "pcc_timer_heap_pop_expired",
    ):
        assert owners[symbol] == expected_owner


def test_runtime_high_substrate_is_owned_by_freestanding_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    members = _archive_members(pcc_py_runtime_archive)
    assert "py_runtime_high_substrate.o" not in members
    assert "freestanding_runtime_high_substrate.o" in members

    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"freestanding_runtime_high_substrate.o"}
    for symbol in (
        "pcc_py_gc_minor_current_get",
        "pcc_py_gc_minor_current_set",
        "pcc_py_gc_pending_minor_block_get",
        "pcc_py_gc_pending_minor_block_set",
        "pcc_py_gc_minor_graph_lock",
        "pcc_py_gc_minor_graph_unlock",
    ):
        assert owners[symbol] == expected_owner


def test_runtime_high_substrate_tls_and_reentrant_lock_behavior(
    tmp_path: Path,
    pcc_py_runtime_archive: Path,
) -> None:
    source = tmp_path / "runtime_high_substrate_probe.c"
    executable = tmp_path / "runtime_high_substrate_probe"
    source.write_text(
        r'''
#include <pthread.h>
#include <stdint.h>
#include <stddef.h>

void *pcc_py_gc_minor_current_get(void);
void pcc_py_gc_minor_current_set(void *block);
void *pcc_py_gc_pending_minor_block_get(void);
void pcc_py_gc_pending_minor_block_set(void *block);
void pcc_py_gc_minor_graph_lock(void);
void pcc_py_gc_minor_graph_unlock(void);

static int in_critical = 0;
static int counter = 0;
static int errors = 0;

static void *worker(void *raw) {
    intptr_t token = (intptr_t)raw;
    if (pcc_py_gc_minor_current_get() != NULL) errors++;
    if (pcc_py_gc_pending_minor_block_get() != NULL) errors++;
    pcc_py_gc_minor_current_set((void *)token);
    pcc_py_gc_pending_minor_block_set((void *)(token + 100));
    for (int i = 0; i < 1000; i++) {
        pcc_py_gc_minor_graph_lock();
        pcc_py_gc_minor_graph_lock();
        if (in_critical != 0) errors++;
        in_critical = 1;
        counter++;
        in_critical = 0;
        pcc_py_gc_minor_graph_unlock();
        pcc_py_gc_minor_graph_unlock();
    }
    if (pcc_py_gc_minor_current_get() != (void *)token) errors++;
    if (pcc_py_gc_pending_minor_block_get() != (void *)(token + 100)) errors++;
    return NULL;
}

int main(void) {
    pthread_t threads[4];
    pcc_py_gc_minor_current_set((void *)(intptr_t)11);
    pcc_py_gc_pending_minor_block_set((void *)(intptr_t)22);
    for (intptr_t i = 0; i < 4; i++) {
        if (pthread_create(&threads[i], NULL, worker, (void *)(1000 + i)) != 0)
            return 1;
    }
    for (int i = 0; i < 4; i++) {
        if (pthread_join(threads[i], NULL) != 0) return 2;
    }
    if (errors != 0 || counter != 4000 || in_critical != 0) return 3;
    if (pcc_py_gc_minor_current_get() != (void *)(intptr_t)11) return 4;
    if (pcc_py_gc_pending_minor_block_get() != (void *)(intptr_t)22) return 5;
    return 0;
}
''',
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            "clang",
            "-std=c11",
            str(source),
            str(pcc_py_runtime_archive),
            "-pthread",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    result = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_dlpack_runtime_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    members = _archive_members(pcc_py_runtime_archive)
    assert "pcc_dlpack_runtime.o" not in members
    assert "py_dlpack_runtime.o" in members

    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_dlpack_runtime.o"}
    for symbol in (
        "pcc_dlpack_buffer_handle_packet_size",
        "pcc_dlpack_metal_capsule_new",
        "pcc_dlpack_capsule_name_code",
        "pcc_dlpack_capsule_consume",
        "pcc_dlpack_managed_tensor_release",
    ):
        assert owners[symbol] == expected_owner


def test_pcc_python_dlpack_capsule_roundtrip_and_one_shot_release(
    tmp_path: Path,
    pcc_py_runtime_archive: Path,
) -> None:
    source = tmp_path / "dlpack_runtime_probe.c"
    executable = tmp_path / "dlpack_runtime_probe"
    source.write_text(
        r'''
#include "py_runtime.h"
#include <stdint.h>

static int release_calls = 0;
static uint64_t released_handle = 0;

static int64_t release_handle(uint64_t native_handle, void *context) {
    (void)context;
    release_calls++;
    released_handle = native_handle;
    return 0;
}

int main(void) {
    uint64_t resource_id = pcc_gc_external_resource_register(
        PCC_GC_EXTERNAL_RESOURCE_GPU_BUFFER, 0x1234, release_handle, 0, 0
    );
    if (resource_id == 0) return 1;
    int64_t shape[2] = {2, 3};
    PyObject *capsule = pcc_dlpack_metal_capsule_new(
        resource_id, 0x5678, 2, 32, 1, 2, shape
    );
    if (capsule == 0) return 2;
    if (pcc_dlpack_buffer_handle_packet_size() != 120) return 3;
    if (pcc_dlpack_capsule_name_code(capsule) != 1) return 4;

    PccDlpackBufferHandlePacket packet;
    void *managed = 0;
    if (pcc_dlpack_capsule_consume(capsule, &packet, &managed) != 0) return 5;
    if (managed == 0 || pcc_dlpack_capsule_name_code(capsule) != 2) return 6;
    if (packet.native_handle != 0x5678) return 7;
    if (packet.external_resource_id != resource_id || packet.nbytes != 24) return 8;
    if (packet.ndim != 2 || packet.shape[0] != 2 || packet.shape[1] != 3) return 9;
    if (packet.device_type != 8 || packet.device_id != 0) return 10;
    if (packet.dtype_code != 2 || packet.dtype_bits != 32 || packet.dtype_lanes != 1)
        return 11;
    void *owned_managed = managed;
    if (pcc_dlpack_capsule_consume(capsule, &packet, &managed) != 2) return 12;
    if (managed != 0) return 17;

    if (pcc_dlpack_managed_tensor_release(owned_managed) != 0) return 13;
    if (pcc_gc_external_resource_mark_fence_complete(resource_id) != 0) return 14;
    if (pcc_gc_external_resource_poll() != 1) return 15;
    if (release_calls != 1 || released_handle != 0x1234) return 16;
    py_decref(capsule);

    uint64_t destructor_id = pcc_gc_external_resource_register(
        PCC_GC_EXTERNAL_RESOURCE_GPU_BUFFER, 0x2222, release_handle, 0, 0
    );
    PyObject *unconsumed = pcc_dlpack_metal_capsule_new(
        destructor_id, 0x3333, 2, 32, 1, 2, shape
    );
    if (destructor_id == 0 || unconsumed == 0) return 18;
    if (pcc_gc_external_resource_mark_fence_complete(destructor_id) != 0) return 19;
    py_decref(unconsumed);
    if (pcc_gc_external_resource_poll() != 1) return 20;
    if (release_calls != 2 || released_handle != 0x2222) return 21;
    return 0;
}
''',
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            "clang",
            "-std=c11",
            f"-I{REPO / 'pcc' / 'py_runtime' / 'include'}",
            f"-I{REPO / 'pcc' / 'py_runtime' / 'src'}",
            str(source),
            str(pcc_py_runtime_archive),
            "-pthread",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    result = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_pcc_python_dlpack_nbytes_preserves_u64_overflow_rules(
    tmp_path: Path,
    pcc_py_runtime_archive: Path,
) -> None:
    source = tmp_path / "dlpack_nbytes_probe.c"
    executable = tmp_path / "dlpack_nbytes_probe"
    source.write_text(
        r'''
#include <stdint.h>
#include <limits.h>

int64_t pcc_dlpack_compute_nbytes_runtime(
    int64_t bits, int64_t lanes, int64_t ndim,
    const int64_t *shape, uint64_t *out_nbytes
);

int main(void) {
    uint64_t nbytes = 0;
    int64_t largest_valid[2] = {INT64_MAX, 2};
    if (pcc_dlpack_compute_nbytes_runtime(
            8, 1, 2, largest_valid, &nbytes) != 0) return 1;
    if (nbytes != UINT64_MAX - 1) return 2;

    int64_t overflow[2] = {INT64_MAX, 3};
    if (pcc_dlpack_compute_nbytes_runtime(
            8, 1, 2, overflow, &nbytes) != -1) return 3;
    int64_t one[1] = {1};
    if (pcc_dlpack_compute_nbytes_runtime(
            8, 65535, 1, one, &nbytes) != 0) return 4;
    if (nbytes != 65535) return 5;
    if (pcc_dlpack_compute_nbytes_runtime(
            0, 1, 1, one, &nbytes) != -1) return 6;
    if (pcc_dlpack_compute_nbytes_runtime(
            8, 1, 1, 0, &nbytes) != -1) return 7;
    return 0;
}
''',
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            "clang",
            "-std=c11",
            str(source),
            str(pcc_py_runtime_archive),
            "-pthread",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    result = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_runtime_log_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    members = _archive_members(pcc_py_runtime_archive)
    assert "pcc_runtime_log.o" not in members
    assert "py_runtime_log.o" in members

    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_runtime_log.o"}
    for symbol in (
        "pcc_runtime_log_fast_state",
        "pcc_runtime_now_us",
        "pcc_runtime_monotonic_us",
        "pcc_runtime_sleep_ns",
        "pcc_runtime_log_enabled",
        "pcc_runtime_log_event",
        "pcc_runtime_log_event_code",
        "pcc_runtime_tripwire_fail",
    ):
        assert owners[symbol] == expected_owner


def test_pcc_python_runtime_log_json_text_and_channel_filtering(
    tmp_path: Path,
    pcc_py_runtime_archive: Path,
) -> None:
    source = tmp_path / "runtime_log_pcc_python_probe.c"
    executable = tmp_path / "runtime_log_pcc_python_probe"
    source.write_text(
        r'''
#include "py_runtime.h"
#include <stdint.h>

void pcc_runtime_log_event_code(
    int32_t category, int32_t event, int64_t value0, int64_t value1,
    const void *pointer
);

int main(void) {
    pcc_runtime_log_event_code(1, 2, INT64_MIN, 17, (void *)(uintptr_t)0x1234);
    pcc_runtime_log_event_code(2, 3, 5, -9, 0);
    pcc_runtime_log_event_code(6, 3, 99, 100, 0);
    return 0;
}
''',
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            "clang",
            "-std=c11",
            f"-I{REPO / 'pcc' / 'py_runtime' / 'include'}",
            str(source),
            str(pcc_py_runtime_archive),
            "-pthread",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr

    json_path = tmp_path / "runtime-log.jsonl"
    json_env = {
        **os.environ,
        "PCC_LOG": "alloc, gc",
        "PCC_LOG_FORMAT": "json",
        "PCC_LOG_FILE": str(json_path),
    }
    json_run = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
        env=json_env,
    )
    assert json_run.returncode == 0, json_run.stdout + json_run.stderr
    events = [json.loads(line) for line in json_path.read_text().splitlines()]
    assert [(row["category"], row["event"]) for row in events] == [
        ("alloc", "alloc_object"),
        ("gc", "store_ptr"),
    ]
    assert events[0]["value0"] == -(1 << 63)
    assert events[0]["value1"] == 17
    assert events[0]["ptr"] == "0x1234"
    assert events[1]["value1"] == -9
    assert events[1]["ptr"] == "0x0"

    text_path = tmp_path / "runtime-log.txt"
    text_env = {
        **os.environ,
        "PCC_LOG": "gc",
        "PCC_LOG_FORMAT": "text",
        "PCC_LOG_FILE": str(text_path),
    }
    text_run = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
        env=text_env,
    )
    assert text_run.returncode == 0, text_run.stdout + text_run.stderr
    text_lines = text_path.read_text().splitlines()
    assert len(text_lines) == 1
    assert text_lines[0].startswith("[pcc.gc] ts=")
    assert " event=store_ptr value0=5 value1=-9 ptr=0x0" in text_lines[0]


def test_c_api_exception_runtime_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """The PyExc_* singletons and the PyErr_* error surface must be owned by
    the pcc-Python exception module, not by the C shim."""
    members = _archive_members(pcc_py_runtime_archive)
    assert "py_capi_exc_runtime.o" in members
    assert "py_capi_compat.o" not in members

    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_exc_runtime.o"}
    for symbol in (
        "PyExc_ValueError",
        "PyExc_TypeError",
        "PyExc_RuntimeError",
        "PyExc_KeyError",
        "PyExc_IndexError",
        "PyExc_AttributeError",
        "PyExc_MemoryError",
        "PyExc_OverflowError",
        "PyExc_SystemError",
        "PyExc_NameError",
        "PyExc_NotImplementedError",
        "PyExc_BaseException",
        "PyExc_Exception",
        "PyExc_ArithmeticError",
        "PyExc_LookupError",
        "PyExc_OSError",
        "PyExc_IOError",
        "PyExc_AssertionError",
        "PyExc_StopIteration",
        "PyExc_StopAsyncIteration",
        "PyExc_ZeroDivisionError",
        "PyExc_ReferenceError",
        "PyExc_BufferError",
        "PyExc_ImportError",
        "PyExc_ModuleNotFoundError",
        "PyExc_ImportWarning",
        "PyExc_FloatingPointError",
        "PyExc_RecursionError",
        "PyExc_UnicodeDecodeError",
        "PyExc_UnicodeEncodeError",
        "PyExc_UnicodeError",
        "PyExc_Warning",
        "PyExc_UserWarning",
        "PyExc_RuntimeWarning",
        "PyExc_DeprecationWarning",
        "PyExc_FutureWarning",
        "Py_Ellipsis",
        "PyErr_SetString",
        "PyErr_SetNone",
        "PyErr_SetObject",
        "PyErr_NoMemory",
        "PyErr_BadInternalCall",
        "PyErr_Occurred",
        "PyErr_Clear",
        "PyErr_GivenExceptionMatches",
        "PyErr_ExceptionMatches",
        "PyErr_Fetch",
        "PyErr_Restore",
        "PyErr_NewException",
        "PyErr_WarnEx",
        "PyErr_WarnFormat",
        "PyErr_WriteUnraisable",
        "PyErr_Print",
        "PyErr_CheckSignals",
        "PyErr_Format",
        "PyErr_FormatV",
        "PyErr_NormalizeException",
        "PyUnicode_FromFormat",
        "PyUnicode_FromFormatV",
    ):
        assert owners[symbol] == expected_owner, symbol


def test_c_api_exception_singletons_are_distinct_data_symbols(
    pcc_py_runtime_archive: Path,
) -> None:
    """The PyExc_* pointer globals must be linker-visible data symbols whose
    values are distinct sentinel addresses.  A C-extension's
    ``extern PyObject *PyExc_ValueError;`` resolves to them, so the shim's
    pointer-equality chain in pcc_capi_exception_tag stays correct."""
    result = subprocess.run(
        ["nm", "-A", "-g", str(pcc_py_runtime_archive)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    defined: dict[str, str] = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 3 or not fields[0].endswith(":"):
            continue
        kind = fields[2]
        if kind in {"U", "u"}:
            continue
        defined.setdefault(fields[-1].lstrip("_"), kind)
    for symbol in (
        "PyExc_ValueError",
        "PyExc_TypeError",
        "PyExc_KeyError",
        "PyExc_IndexError",
        "PyExc_StopIteration",
        "PyExc_ZeroDivisionError",
        "PyExc_MemoryError",
    ):
        assert defined.get(symbol) == "D", (symbol, defined.get(symbol))
    # IOError deliberately aliases OSError's sentinel (CPython aliases the
    # type), but distinct singletons must not collapse.
    assert "PyExc_IOError" in defined


def _build_exception_probe(tmp_path: Path, archive: Path, name: str) -> Path:
    source = tmp_path / f"{name}.c"
    executable = tmp_path / name
    source.write_text(
        r'''
#include <stdint.h>
#include <string.h>

typedef struct PyObject PyObject;

extern PyObject *PyExc_ValueError;
extern PyObject *PyExc_TypeError;
extern PyObject *PyExc_RuntimeError;
extern PyObject *PyExc_KeyError;
extern PyObject *PyExc_IndexError;
extern PyObject *PyExc_AttributeError;
extern PyObject *PyExc_MemoryError;
extern PyObject *PyExc_OverflowError;
extern PyObject *PyExc_SystemError;
extern PyObject *PyExc_NameError;
extern PyObject *PyExc_NotImplementedError;
extern PyObject *PyExc_BaseException;
extern PyObject *PyExc_Exception;
extern PyObject *PyExc_ArithmeticError;
extern PyObject *PyExc_LookupError;
extern PyObject *PyExc_OSError;
extern PyObject *PyExc_IOError;
extern PyObject *PyExc_AssertionError;
extern PyObject *PyExc_StopIteration;
extern PyObject *PyExc_StopAsyncIteration;
extern PyObject *PyExc_ZeroDivisionError;
extern PyObject *PyExc_ReferenceError;
extern PyObject *PyExc_BufferError;
extern PyObject *PyExc_ImportError;
extern PyObject *PyExc_ModuleNotFoundError;
extern PyObject *PyExc_FloatingPointError;
extern PyObject *PyExc_RecursionError;
extern PyObject *PyExc_UnicodeDecodeError;
extern PyObject *PyExc_ImportWarning;
extern PyObject *PyExc_Warning;
extern PyObject *PyExc_UserWarning;
extern PyObject *PyExc_RuntimeWarning;
extern PyObject *PyExc_DeprecationWarning;
extern PyObject *PyExc_FutureWarning;
extern PyObject *Py_Ellipsis;

void PyErr_SetString(PyObject *type, const char *message);
void PyErr_SetNone(PyObject *type);
void PyErr_SetObject(PyObject *type, PyObject *value);
PyObject *PyErr_NoMemory(void);
void PyErr_BadInternalCall(void);
PyObject *PyErr_Occurred(void);
void PyErr_Clear(void);
int PyErr_GivenExceptionMatches(PyObject *given, PyObject *exc);
int PyErr_ExceptionMatches(PyObject *exc);
void PyErr_Fetch(PyObject **ptype, PyObject **pvalue, PyObject **ptraceback);
void PyErr_Restore(PyObject *type, PyObject *value, PyObject *traceback);
PyObject *PyErr_NewException(const char *name, PyObject *base, PyObject *dict);
int PyErr_WarnEx(PyObject *category, const char *message, long stack_level);
int PyErr_WarnFormat(PyObject *category, long stack_level, const char *format, ...);
void PyErr_WriteUnraisable(PyObject *obj);
void PyErr_Print(void);
int PyErr_CheckSignals(void);
PyObject *PyErr_Format(PyObject *type, const char *format, ...);
PyObject *PyErr_FormatV(PyObject *type, const char *format, void *vargs);
void PyErr_NormalizeException(PyObject **exc, PyObject **val, PyObject **tb);
PyObject *PyUnicode_FromFormat(const char *format, ...);
PyObject *PyUnicode_FromFormatV(const char *format, void *vargs);

PyObject *py_current_exception(void);
void py_clear_exception(void);
int64_t py_exc_matches(PyObject *exc, PyObject *type);
int64_t py_err_occurred(void);
void py_raise(PyObject *exc);
void py_incref(PyObject *obj);
void py_decref(PyObject *obj);
PyObject *py_exc_new(int64_t type_tag, const char *msg);
PyObject *py_str_new(const char *utf8, int64_t byte_len);
const char *py_str_utf8(PyObject *s);
int64_t py_str_byte_len(PyObject *s);

static int failures = 0;

static void check(int condition, const char *label) {
    if (!condition) {
        failures += 1;
        const char *msg = "FAIL: ";
        extern int write(int, const void *, unsigned long);
        (void)write(2, msg, 6);
        while (*label) { (void)write(2, label, 1); label++; }
        (void)write(2, "\n", 1);
    }
}

int main(void) {
    /* SetString -> Occurred -> ExceptionMatches round trip. */
    PyErr_SetString(PyExc_ValueError, "boom from extension");
    check(PyErr_Occurred() != 0, "occurred nonnull");
    check(PyErr_ExceptionMatches(PyExc_ValueError) != 0, "matches ValueError");
    check(PyErr_ExceptionMatches(PyExc_TypeError) == 0, "no match TypeError");
    check(PyErr_ExceptionMatches(PyExc_Exception) != 0, "matches Exception base");
    check(py_err_occurred() != 0, "TLS error latched");
    PyErr_Clear();
    check(PyErr_Occurred() == 0, "cleared");

    /* SetObject with a value keeps the exception object as the value. */
    PyObject *value = py_str_new("detail", 6);
    PyErr_SetObject(PyExc_KeyError, value);
    check(PyErr_ExceptionMatches(PyExc_KeyError) != 0, "KeyError matches");
    check(PyErr_ExceptionMatches(PyExc_LookupError) != 0, "KeyError is LookupError");
    py_decref(value);
    PyErr_Clear();

    /* NoMemory / BadInternalCall / SetNone. */
    check(PyErr_NoMemory() == 0, "NoMemory returns NULL");
    check(PyErr_ExceptionMatches(PyExc_MemoryError) != 0, "NoMemory MemoryError");
    PyErr_Clear();
    PyErr_BadInternalCall();
    check(PyErr_ExceptionMatches(PyExc_SystemError) != 0, "BadInternalCall SystemError");
    PyErr_Clear();
    PyErr_SetNone(PyExc_TypeError);
    check(PyErr_ExceptionMatches(PyExc_TypeError) != 0, "SetNone TypeError");
    PyErr_Clear();

    /* Fetch/Restore round trip. */
    PyErr_SetString(PyExc_OverflowError, "too big");
    PyObject *ptype = 0;
    PyObject *pvalue = 0;
    PyObject *ptb = 0;
    PyErr_Fetch(&ptype, &pvalue, &ptb);
    check(ptype != 0, "fetch type nonnull");
    check(pvalue != 0, "fetch value nonnull");
    check(ptb == 0, "fetch tb null");
    check(PyErr_Occurred() == 0, "fetch cleared");
    PyErr_Restore(ptype, pvalue, ptb);
    check(PyErr_ExceptionMatches(PyExc_OverflowError) != 0, "restore OverflowError");
    PyErr_Clear();

    /* Format: %s, %d, %x, %f lanes raise the right class. */
    PyErr_Format(PyExc_RuntimeError, "n=%d s=%s x=%x f=%.1f", 42, "abc", 255, 2.5);
    check(PyErr_ExceptionMatches(PyExc_RuntimeError) != 0, "format RuntimeError");
    PyErr_Clear();
    PyErr_Format(PyExc_KeyError, "%d", 1);
    check(PyErr_ExceptionMatches(PyExc_KeyError) != 0, "format KeyError");
    PyErr_Clear();

    /* NewException creates a real class usable as a raise target. */
    PyObject *exc_cls = PyErr_NewException("pkg.MyError", PyExc_ValueError, 0);
    check(exc_cls != 0, "NewException created");
    if (exc_cls != 0) {
        PyObject *v2 = py_str_new("custom", 6);
        PyErr_SetObject(exc_cls, v2);
        check(PyErr_ExceptionMatches(exc_cls) != 0, "custom matches its own class");
        py_decref(v2);
        PyErr_Clear();
    }

    /* Warning and signal helpers are no-ops that must not corrupt state. */
    check(PyErr_WarnEx(PyExc_Warning, "w", 1) == 0, "WarnEx no-op");
    check(PyErr_WarnFormat(PyExc_DeprecationWarning, 1, "w %d", 7) == 0, "WarnFormat no-op");
    check(PyErr_CheckSignals() == 0, "CheckSignals no-op");
    PyErr_WriteUnraisable(0);
    check(PyErr_Occurred() == 0, "WriteUnraisable clears");

    /* Ellipsis is a stable non-NULL sentinel. */
    check(Py_Ellipsis != 0, "Ellipsis non-null");

    /* Unicode_FromFormat produces real str objects with the right bytes. */
    PyObject *s1 = PyUnicode_FromFormat("a=%d b=%s", 3, "xy");
    if (s1 != 0) {
        check(py_str_byte_len(s1) == 8, "FromFormat length");
        const char *raw = py_str_utf8(s1);
        check(raw != 0 && strcmp(raw, "a=3 b=xy") == 0, "FromFormat text");
        py_decref(s1);
    }

    if (failures == 0) {
        return 0;
    }
    return 100 + failures;
}
''',
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            "clang",
            "-std=c11",
            f"-I{REPO / 'pcc' / 'py_runtime' / 'include'}",
            str(source),
            str(archive),
            "-pthread",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return executable


def test_c_api_exception_surface_raises_and_catches_from_native(
    tmp_path: Path,
    pcc_py_runtime_archive: Path,
) -> None:
    """A C probe linked against the production pcc-Python archive must be able
    to raise via the PyErr_* ABI and match/catch it, proving the pcc-Python
    owner is behaviorally correct, not just symbol-correct."""
    executable = _build_exception_probe(tmp_path, pcc_py_runtime_archive, "exc_probe")
    run = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_c_api_exception_surface_format_text_is_exact(
    tmp_path: Path,
    pcc_py_runtime_archive: Path,
) -> None:
    """PyErr_Format / PyUnicode_FromFormat must reproduce the C shim's
    %-mini-language byte-for-byte (integers, pointers, floats, objects)."""
    source = tmp_path / "exc_format_probe.c"
    executable = tmp_path / "exc_format_probe"
    source.write_text(
        r'''
#include <stdint.h>
#include <string.h>
typedef struct PyObject PyObject;
extern PyObject *PyExc_ValueError;
void PyErr_SetString(PyObject *type, const char *message);
void PyErr_Clear(void);
PyObject *PyErr_Format(PyObject *type, const char *format, ...);
PyObject *PyUnicode_FromFormat(const char *format, ...);
PyObject *py_current_exception(void);
const char *py_str_utf8(PyObject *s);
int64_t py_str_byte_len(PyObject *s);
void py_decref(PyObject *obj);
int64_t py_exc_get_message(PyObject *exc, PyObject **out);
PyObject *py_obj_str(PyObject *o);

static int failures = 0;
static void check(int condition, const char *label) {
    if (!condition) {
        failures += 1;
        const char *msg = "FAIL: ";
        extern int write(int, const void *, unsigned long);
        (void)write(2, msg, 6);
        while (*label) { (void)write(2, label, 1); label++; }
        (void)write(2, "\n", 1);
    }
}

int main(void) {
    /* %R object formatting through the exception message. */
    PyObject *s = PyUnicode_FromFormat("%R", PyExc_ValueError);
    if (s != 0) {
        check(py_str_byte_len(s) > 0, "obj repr nonempty");
        py_decref(s);
    }
    /* signed/unsigned/pointer/float lanes. */
    s = PyUnicode_FromFormat("d=%d u=%u x=%x X=%X p=%p f=%.2f e=%e",
                             -7, 3000000000u, 255, 255, (void *)(uintptr_t)0x1234,
                             3.14159, 2.5e3);
    if (s != 0) {
        const char *raw = py_str_utf8(s);
        check(raw != 0 && strcmp(raw,
            "d=-7 u=3000000000 x=ff X=FF p=0x1234 f=3.14 e=2.500000e+03") == 0,
            "format lanes");
        py_decref(s);
    }
    /* %d negative and INT64_MIN boundary. */
    s = PyUnicode_FromFormat("lo=%lld hi=%lld", (long long)123, (long long)0x8000000000000000ULL);
    if (s != 0) {
        const char *raw = py_str_utf8(s);
        check(raw != 0 && strcmp(raw, "lo=123 hi=-9223372036854775808") == 0,
              "int64 boundary");
        py_decref(s);
    }
    /* %% escaping and unknown conversions. */
    s = PyUnicode_FromFormat("100%% done %q", 1);
    if (s != 0) {
        const char *raw = py_str_utf8(s);
        check(raw != 0 && strcmp(raw, "100% done %q") == 0, "escape and unknown");
        py_decref(s);
    }
    if (failures == 0) { return 0; }
    return 100 + failures;
}
''',
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            "clang",
            "-std=c11",
            f"-I{REPO / 'pcc' / 'py_runtime' / 'include'}",
            str(source),
            str(pcc_py_runtime_archive),
            "-pthread",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_c_api_dict_runtime_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """The PyDict_* surface must be owned by the pcc-Python dict module."""
    members = _archive_members(pcc_py_runtime_archive)
    assert "py_capi_dict_runtime.o" in members
    assert "py_capi_compat.o" not in members

    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_dict_runtime.o"}
    for symbol in (
        "PyDict_New",
        "PyDict_SetItem",
        "PyDict_SetItemString",
        "PyDict_GetItem",
        "PyDict_GetItemString",
        "PyDict_GetItemWithError",
        "PyDict_GetItemRef",
        "PyDict_GetItemStringRef",
        "PyDict_SetDefaultRef",
        "PyDict_Pop",
        "PyDict_PopString",
        "PyDict_DelItem",
        "PyDict_DelItemString",
        "PyDict_Size",
        "PyDict_Contains",
        "PyDict_ContainsString",
        "PyDict_Next",
        "PyDict_Keys",
        "PyDict_Values",
        "PyDict_Items",
        "PyDict_Clear",
        "PyDict_Check",
        "PyDict_CheckExact",
        "PyDict_Copy",
        "PyDict_Merge",
    ):
        assert owners[symbol] == expected_owner, symbol


def test_c_api_dict_surface_inserts_looks_up_and_iterates(
    tmp_path: Path,
    pcc_py_runtime_archive: Path,
) -> None:
    """A native probe must insert, look up (borrowed and owned), default,
    pop, delete, iterate, merge, copy, and clear through the pcc-Python
    PyDict_* owner linked against the production archive."""
    source = tmp_path / "dict_probe.c"
    executable = tmp_path / "dict_probe"
    source.write_text(
        r'''
#include <stdint.h>
#include <string.h>
typedef struct PyObject PyObject;
PyObject *PyDict_New(void);
int PyDict_SetItem(PyObject *d, PyObject *k, PyObject *v);
int PyDict_SetItemString(PyObject *d, const char *k, PyObject *v);
PyObject *PyDict_GetItem(PyObject *d, PyObject *k);
PyObject *PyDict_GetItemString(PyObject *d, const char *k);
int PyDict_GetItemRef(PyObject *d, PyObject *k, PyObject **r);
int PyDict_GetItemStringRef(PyObject *d, const char *k, PyObject **r);
int PyDict_SetDefaultRef(PyObject *d, PyObject *k, PyObject *dv, PyObject **r);
int PyDict_Pop(PyObject *d, PyObject *k, PyObject **r);
int PyDict_PopString(PyObject *d, const char *k, PyObject **r);
int PyDict_DelItem(PyObject *d, PyObject *k);
int PyDict_DelItemString(PyObject *d, const char *k);
long PyDict_Size(PyObject *d);
int PyDict_Contains(PyObject *d, PyObject *k);
int PyDict_ContainsString(PyObject *d, const char *k);
int PyDict_Next(PyObject *d, long *pos, PyObject **key, PyObject **value);
PyObject *PyDict_Keys(PyObject *d);
PyObject *PyDict_Values(PyObject *d);
PyObject *PyDict_Items(PyObject *d);
void PyDict_Clear(PyObject *d);
int PyDict_Check(PyObject *o);
int PyDict_CheckExact(PyObject *o);
PyObject *PyDict_Copy(PyObject *mp);
int PyDict_Merge(PyObject *a, PyObject *b, int override);
void py_decref(PyObject *obj);
PyObject *py_str_new(const char *utf8, int64_t byte_len);
int64_t py_list_len(PyObject *l);
int64_t py_int_value_i64(PyObject *o);
PyObject *py_int_from_i64(int64_t v);

static int failures = 0;
static void check(int c, const char *label) {
    if (!c) { failures += 1; }
}
static PyObject *S(const char *s) { return py_str_new(s, strlen(s)); }

int main(void) {
    PyObject *d = PyDict_New();
    check(d != 0, "new");
    check(PyDict_Check(d) == 1, "check");
    PyObject *k1 = S("a"), *v1 = py_int_from_i64(1);
    PyObject *k2 = S("b"), *v2 = py_int_from_i64(2);
    check(PyDict_SetItem(d, k1, v1) == 0, "setitem a");
    check(PyDict_SetItemString(d, "b", v2) == 0, "setitem b");
    check(PyDict_Size(d) == 2, "size 2");
    check(PyDict_Contains(d, k1) == 1, "contains a");
    check(PyDict_ContainsString(d, "b") == 1, "contains b");
    check(PyDict_ContainsString(d, "zz") == 0, "no contains zz");
    PyObject *g = PyDict_GetItem(d, k1);
    check(g != 0 && py_int_value_i64(g) == 1, "getitem a");
    PyObject *gs = PyDict_GetItemString(d, "b");
    check(gs != 0 && py_int_value_i64(gs) == 2, "getitem b str");
    check(PyDict_GetItemString(d, "nope") == 0, "getitem missing null");
    PyObject *r = 0;
    check(PyDict_GetItemRef(d, k1, &r) == 1 && r != 0, "getitemref");
    py_decref(r);
    check(PyDict_GetItemStringRef(d, "b", &r) == 1 && r != 0, "getitemref str");
    py_decref(r);
    PyObject *dv = S("def");
    PyObject *dr = 0;
    check(PyDict_SetDefaultRef(d, k1, dv, &dr) == 1, "setdefault existing");
    py_decref(dr);
    PyObject *k3 = S("c");
    check(PyDict_SetDefaultRef(d, k3, dv, &dr) == 0 && dr != 0, "setdefault new");
    py_decref(dr);
    check(PyDict_Size(d) == 3, "size after default");
    PyObject *pop = 0;
    check(PyDict_Pop(d, k2, &pop) == 1 && pop != 0, "pop b");
    py_decref(pop);
    check(PyDict_Size(d) == 2, "size after pop");
    check(PyDict_DelItem(d, k1) == 0, "del a");
    check(PyDict_Size(d) == 1, "size after del");
    PyObject *keys = PyDict_Keys(d);
    check(keys != 0 && py_list_len(keys) == 1, "keys");
    py_decref(keys);
    PyObject *vals = PyDict_Values(d);
    check(vals != 0 && py_list_len(vals) == 1, "values");
    py_decref(vals);
    PyObject *items = PyDict_Items(d);
    check(items != 0 && py_list_len(items) == 1, "items");
    py_decref(items);
    PyObject *cp = PyDict_Copy(d);
    check(cp != 0 && PyDict_Size(cp) == 1, "copy");
    py_decref(cp);
    PyObject *d2 = PyDict_New();
    PyObject *k4 = S("x"), *v4 = py_int_from_i64(9);
    PyDict_SetItem(d2, k4, v4);
    check(PyDict_Merge(d, d2, 1) == 0 && PyDict_Size(d) == 2, "merge override");
    long pos = 0;
    PyObject *key = 0, *value = 0;
    int n = 0;
    while (PyDict_Next(d, &pos, &key, &value)) { n += 1; }
    check(n == 2, "next iter count");
    PyDict_Clear(d);
    check(PyDict_Size(d) == 0, "clear");
    check(PyDict_Check(v1) == 0, "check non-dict");
    py_decref(k1); py_decref(v1); py_decref(k2); py_decref(v2);
    py_decref(k3); py_decref(dv); py_decref(d); py_decref(d2);
    py_decref(k4); py_decref(v4);
    if (failures == 0) { return 0; }
    return 100 + failures;
}
''',
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            "clang",
            "-std=c11",
            f"-I{REPO / 'pcc' / 'py_runtime' / 'include'}",
            str(source),
            str(pcc_py_runtime_archive),
            "-pthread",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_c_api_object_runtime_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """The simple PyObject_* basics must be owned by the pcc-Python object
    module, not by the C shim."""
    members = _archive_members(pcc_py_runtime_archive)
    assert "py_capi_object_runtime.o" in members
    assert "py_capi_compat.o" not in members

    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_object_runtime.o"}
    for symbol in (
        "PyObject_Type",
        "PyObject_IsTrue",
        "PyObject_Not",
        "PyObject_Str",
        "PyObject_Repr",
        "PyObject_Bytes",
        "PyObject_Format",
        "PyObject_Hash",
        "PyObject_Size",
        "PyObject_Length",
        "PyObject_GetItem",
        "PyObject_SetItem",
        "PyObject_DelItem",
        "PyObject_GetIter",
        "PyObject_SelfIter",
        "PyObject_RichCompareBool",
        "PyObject_RichCompare",
        "PyObject_IsInstance",
        "PyObject_ClearWeakRefs",
        "PyObject_GC_Track",
        "PyObject_GC_UnTrack",
        "PyObject_GC_Del",
        "PyObject_AsFileDescriptor",
        "PyObject_LengthHint",
    ):
        assert owners[symbol] == expected_owner, symbol


def test_c_api_object_basics_operate_on_native_objects(
    tmp_path: Path,
    pcc_py_runtime_archive: Path,
) -> None:
    """A native probe must exercise type, truth, str/repr, hash, size, item
    access, iteration, rich comparison, isinstance, weakref invalidation,
    GC no-ops, file-descriptor coercion, and length hinting through the
    pcc-Python PyObject_* owner."""
    source = tmp_path / "object_probe.c"
    executable = tmp_path / "object_probe"
    source.write_text(
        r'''
#include <stdint.h>
#include <string.h>
typedef struct PyObject PyObject;
PyObject *PyObject_Type(PyObject *o);
int PyObject_IsTrue(PyObject *o);
int PyObject_Not(PyObject *o);
PyObject *PyObject_Str(PyObject *o);
PyObject *PyObject_Repr(PyObject *o);
PyObject *PyObject_Bytes(PyObject *o);
PyObject *PyObject_Format(PyObject *o, PyObject *spec);
long PyObject_Hash(PyObject *o);
long PyObject_Size(PyObject *o);
long PyObject_Length(PyObject *o);
PyObject *PyObject_GetItem(PyObject *o, PyObject *k);
int PyObject_SetItem(PyObject *o, PyObject *k, PyObject *v);
int PyObject_DelItem(PyObject *o, PyObject *k);
PyObject *PyObject_GetIter(PyObject *o);
PyObject *PyObject_SelfIter(PyObject *o);
int PyObject_RichCompareBool(PyObject *a, PyObject *b, int opid);
PyObject *PyObject_RichCompare(PyObject *a, PyObject *b, int opid);
int PyObject_IsInstance(PyObject *o, PyObject *cls);
void PyObject_ClearWeakRefs(PyObject *o);
void PyObject_GC_Track(void *op);
void PyObject_GC_UnTrack(void *op);
void PyObject_GC_Del(void *op);
int PyObject_AsFileDescriptor(PyObject *o);
long PyObject_LengthHint(PyObject *o, long dv);
void py_decref(PyObject *obj);
PyObject *py_str_new(const char *utf8, int64_t byte_len);
const char *py_str_utf8(PyObject *s);
PyObject *py_int_from_i64(int64_t v);
int64_t py_int_value_i64(PyObject *o);
PyObject *py_list_new(int64_t n);
int64_t py_list_len(PyObject *l);
void py_list_append(PyObject *l, PyObject *v);
PyObject *py_dict_new(void);

static int failures = 0;
static void check(int c, const char *label) {
    if (!c) { failures += 1; }
}
static PyObject *S(const char *s) { return py_str_new(s, strlen(s)); }

int main(void) {
    PyObject *s = S("hello");
    PyObject *t = py_int_from_i64(7);
    PyObject *lst = py_list_new(2);
    PyObject *i0 = py_int_from_i64(10);
    PyObject *i1 = py_int_from_i64(20);
    py_list_append(lst, i0);
    py_list_append(lst, i1);

    check(PyObject_Type(s) != 0, "type nonnull");
    check(PyObject_IsTrue(t) == 1, "true int");
    check(PyObject_IsTrue(i0) == 1, "true 10");
    check(PyObject_Not(i0) == 0, "not 10 is false");
    check(PyObject_IsTrue(py_int_from_i64(0)) == 0, "zero is false");
    PyObject *st = PyObject_Str(t);
    check(st != 0 && strcmp(py_str_utf8(st), "7") == 0, "str int");
    py_decref(st);
    PyObject *rp = PyObject_Repr(s);
    check(rp != 0 && strcmp(py_str_utf8(rp), "'hello'") == 0, "repr str");
    py_decref(rp);
    check(PyObject_Hash(t) != -1, "hash");
    check(PyObject_Size(s) == 5 && PyObject_Length(s) == 5, "str len");
    check(PyObject_Size(lst) == 2, "list len");
    PyObject *k0 = py_int_from_i64(0);
    PyObject *item = PyObject_GetItem(lst, k0);
    check(item != 0 && py_int_value_i64(item) == 10, "getitem");
    py_decref(item);
    PyObject *i9 = py_int_from_i64(9);
    check(PyObject_SetItem(lst, k0, i9) == 0, "setitem");
    item = PyObject_GetItem(lst, k0);
    check(item != 0 && py_int_value_i64(item) == 9, "setitem readback");
    py_decref(item);
    check(PyObject_DelItem(lst, k0) == 0, "delitem");
    PyObject *it = PyObject_GetIter(lst);
    check(it != 0, "getiter");
    py_decref(it);
    check(PyObject_SelfIter(s) == s, "selfiter identity");
    py_decref(PyObject_SelfIter(s));
    check(PyObject_RichCompareBool(t, py_int_from_i64(7), 2) == 1, "eq");
    check(PyObject_RichCompareBool(t, py_int_from_i64(8), 0) == 1, "lt");
    check(PyObject_RichCompareBool(t, py_int_from_i64(8), 3) == 1, "ne");
    PyObject *rc = PyObject_RichCompare(t, py_int_from_i64(7), 2);
    check(rc != 0, "richcompare eq obj");
    py_decref(rc);
    check(PyObject_IsInstance(t, PyObject_Type(t)) == 1, "isinstance");
    PyObject_GC_Track(s); PyObject_GC_UnTrack(s);
    PyObject_GC_Del(NULL);
    PyObject_ClearWeakRefs(s);
    check(PyObject_AsFileDescriptor(t) == 7, "as fd");
    check(PyObject_LengthHint(s, 99) == 5, "length hint str");
    PyObject *d = py_dict_new();
    check(PyObject_LengthHint(d, 99) == 0, "length hint dict");

    py_decref(s); py_decref(t); py_decref(lst); py_decref(i0); py_decref(i1);
    py_decref(k0); py_decref(i9); py_decref(d);
    if (failures == 0) { return 0; }
    return 100 + failures;
}
''',
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            "clang",
            "-std=c11",
            f"-I{REPO / 'pcc' / 'py_runtime' / 'include'}",
            str(source),
            str(pcc_py_runtime_archive),
            "-pthread",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_c_api_type_tokens_are_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """The 24 builtin Py*_Type recognition tokens must be pcc-Python data
    symbols in py_capi_type_runtime.o, not C shim globals."""
    members = _archive_members(pcc_py_runtime_archive)
    assert "py_capi_type_runtime.o" in members
    assert "py_capi_compat.o" not in members

    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_type_runtime.o"}
    for symbol in (
        "PyType_Type",
        "PyBaseObject_Type",
        "PyTuple_Type",
        "PyList_Type",
        "PyDict_Type",
        "PyUnicode_Type",
        "PyLong_Type",
        "PyFloat_Type",
        "PyBool_Type",
        "PyBytes_Type",
        "PyByteArray_Type",
        "PySet_Type",
        "PyFrozenSet_Type",
        "PySlice_Type",
        "PyComplex_Type",
        "PyModule_Type",
        "PyFunction_Type",
        "PyCFunction_Type",
        "PyMemberDescr_Type",
        "PyGetSetDescr_Type",
        "PyMethodDescr_Type",
        "PyDictProxy_Type",
        "PyMemoryView_Type",
    ):
        assert owners[symbol] == expected_owner, symbol


def test_c_api_type_bridge_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """The pcc_capi_type family must be owned by py_capi_type_runtime.o."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_type_runtime.o"}
    for symbol in (
        "pcc_capi_type",
        "pcc_capi_type_addr",
        "pcc_capi_typecheck",
        "pcc_capi_is_type_object",
        "pcc_capi_is_type_object_value",
        "pcc_capi_builtin_type_token",
        "pcc_capi_is_cext_type_tag",
        "pcc_capi_cext_tag_for",
        "pcc_capi_cext_type_for_object",
        "pcc_capi_size",
        "pcc_capi_set_size",
        "PyType_IsSubtype",
        "pcc_capi_type_object_issubclass",
    ):
        assert owners[symbol] == expected_owner, symbol


def test_c_api_type_tokens_are_distinct_and_ready(
    tmp_path: Path,
    pcc_py_runtime_archive: Path,
) -> None:
    """A native probe must read the pcc-Python token globals: distinct
    addresses, tp_name pointing at the right string, tp_flags carrying READY,
    and the tag->token / subtype mapping behaving like the C shim."""
    source = tmp_path / "type_token_probe.c"
    executable = tmp_path / "type_token_probe"
    source.write_text(
        r'''
#include <stdint.h>
#include <string.h>
typedef struct PyObject PyObject;
typedef struct _typeobject PyTypeObject;
struct _typeobject {
    int64_t refcount;   /* 0 */
    int32_t type_tag;   /* 8 */
    int32_t flags;      /* 12 */
    void *ob_type;      /* 16 */
    int64_t ob_size;    /* 24 */
    const char *tp_name;  /* 32 */
    int64_t tp_basicsize;  /* 40 */
    int64_t tp_itemsize;   /* 48 */
    void *tp_dealloc;      /* 56 */
    int64_t tp_vectorcall_offset;  /* 64 */
    void *tp_getattr;  void *tp_setattr;  void *tp_as_async;  void *tp_repr;
    void *tp_as_number;  void *tp_as_sequence;  void *tp_as_mapping;
    void *tp_hash;  void *tp_call;  void *tp_str;  void *tp_getattro;
    void *tp_setattro;  void *tp_as_buffer;  /* 72..168 */
    unsigned long tp_flags;  /* 176 */
};

extern PyTypeObject PyType_Type, PyBaseObject_Type, PyLong_Type, PyList_Type,
    PyDict_Type, PyUnicode_Type, PyFloat_Type, PyBool_Type, PyBytes_Type,
    PyTuple_Type, PySet_Type, PyComplex_Type, PyModule_Type,
    PyMemoryView_Type;
PyObject *pcc_capi_type(PyObject *o);
int pcc_capi_typecheck(PyObject *o, PyTypeObject *t);
int pcc_capi_is_type_object(PyObject *o);
int PyType_IsSubtype(PyTypeObject *a, PyTypeObject *b);

int main(void) {
    if (&PyLong_Type == &PyList_Type) return 1;
    if (&PyType_Type == &PyBaseObject_Type) return 2;
    if (&PyBool_Type == &PyFloat_Type) return 3;
    if (strcmp(PyLong_Type.tp_name, "int") != 0) return 4;
    if (strcmp(PyUnicode_Type.tp_name, "str") != 0) return 5;
    if (strcmp(PyList_Type.tp_name, "list") != 0) return 6;
    if (strcmp(PyType_Type.tp_name, "type") != 0) return 7;
    if ((PyLong_Type.tp_flags & 0x1000) == 0) return 8;
    if (PyLong_Type.refcount != 1 || PyLong_Type.type_tag != 0) return 9;

    if (!pcc_capi_is_type_object((PyObject *)&PyLong_Type)) return 10;
    if (!pcc_capi_is_type_object((PyObject *)&PyMemoryView_Type)) return 11;
    if (pcc_capi_type((PyObject *)&PyLong_Type) != (PyObject *)&PyType_Type)
        return 12;
    if (!pcc_capi_typecheck((PyObject *)&PyLong_Type, &PyType_Type)) return 13;
    if (pcc_capi_typecheck((PyObject *)&PyLong_Type, &PyList_Type)) return 14;
    if (!PyType_IsSubtype(&PyLong_Type, &PyLong_Type)) return 15;
    if (PyType_IsSubtype(&PyLong_Type, &PyList_Type)) return 16;
    return 0;
}
''',
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            "clang",
            "-std=c11",
            f"-I{REPO / 'pcc' / 'py_runtime' / 'include'}",
            str(source),
            str(pcc_py_runtime_archive),
            "-pthread",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_c_api_complex_surface_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """All scalar and two-f64 aggregate PyComplex_* symbols are pcc-Python."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_type_runtime.o"}
    for symbol in (
        "PyComplex_FromDoubles",
        "PyComplex_RealAsDouble",
        "PyComplex_ImagAsDouble",
        "PyComplex_Check",
        "PyComplex_CheckExact",
        "PyComplex_AsCComplex",
        "PyComplex_FromCComplex",
    ):
        assert owners[symbol] == expected_owner, symbol


def test_c_api_trivial_bridge_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """PyMutex/PyGILState/PyOS trivial bridge must be owned by
    py_capi_type_runtime.o."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_type_runtime.o"}
    for symbol in (
        "PyMutex_Lock",
        "PyMutex_Unlock",
        "PyGILState_Ensure",
        "PyGILState_Release",
        "PyGILState_Check",
        "PyOS_strtol",
        "PyOS_strtoul",
        "PyOS_string_to_double",
    ):
        assert owners[symbol] == expected_owner, symbol


def test_c_api_unicode_thin_wrappers_are_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """The thin PyUnicode_* wrappers must be owned by py_capi_unicode_runtime.o.
    The decoding/search/format engines stay in the C shim for later slices."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_unicode_runtime.o"}
    for symbol in (
        "PyUnicode_FromString",
        "PyUnicode_FromStringAndSize",
        "PyUnicode_FromObject",
        "PyUnicode_InternFromString",
        "PyUnicode_AsUTF8",
        "PyUnicode_AsUTF8AndSize",
        "PyUnicode_AsUTF8String",
        "PyUnicode_AsASCIIString",
        "PyUnicode_GetLength",
        "PyUnicode_Check",
        "PyUnicode_CheckExact",
        "PyUnicode_Concat",
        "PyUnicode_Contains",
        "PyUnicode_Substring",
        "PyUnicode_Replace",
        "PyUnicode_Tailmatch",
        "PyUnicode_EqualToUTF8",
        "PyUnicode_EqualToUTF8AndSize",
        "PyUnicode_Decode",
        "PyUnicode_DecodeUTF8",
        "PyUnicode_FromEncodedObject",
        "PyUnicode_FromKindAndData",
        "PyUnicode_FromOrdinal",
        "PyUnicode_AsUCS4",
        "PyUnicode_AsUCS4Copy",
        "PyUnicode_AsLatin1String",
        "PyUnicode_AsEncodedString",
    ):
        assert owners[symbol] == expected_owner, symbol


def test_c_api_capsule_surface_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """The PyCapsule_* surface must be owned by py_capi_capsule_runtime.o."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_capsule_runtime.o"}
    for symbol in (
        "PyCapsule_New",
        "PyCapsule_CheckExact",
        "PyCapsule_IsValid",
        "PyCapsule_GetName",
        "PyCapsule_GetContext",
        "PyCapsule_GetDestructor",
        "PyCapsule_GetPointer",
        "PyCapsule_SetPointer",
        "PyCapsule_SetContext",
        "PyCapsule_SetDestructor",
        "PyCapsule_SetName",
        "PyCapsule_Import",
        "pcc_py_capsule_new",
        "pcc_py_capsule_get_pointer",
        "pcc_py_capsule_get_name",
        "pcc_py_capsule_is_valid",
        "pcc_py_capsule_set_name",
    ):
        assert owners[symbol] == expected_owner, symbol


def test_c_api_set_surface_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """The PySet_* / PyAnySet_* surface must be owned by py_capi_set_runtime.o."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_set_runtime.o"}
    for symbol in (
        "PySet_New",
        "PySet_Add",
        "PySet_Contains",
        "PySet_Discard",
        "PySet_Size",
        "PySet_Check",
        "PySet_CheckExact",
        "PyAnySet_Check",
        "PyAnySet_CheckExact",
    ):
        assert owners[symbol] == expected_owner, symbol


def test_c_api_misc_surface_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """The small misc C-API surface must be owned by py_capi_misc_runtime.o."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_misc_runtime.o"}
    for symbol in (
        "PyException_SetCause",
        "PyException_SetContext",
        "PyException_SetTraceback",
        "PyInterpreterState_Main",
        "PyThreadState_Get",
        "PyDictProxy_New",
        "PyBuffer_Release",
    ):
        assert owners[symbol] == expected_owner, symbol


def test_c_api_cfunction_accessors_are_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """The PyCFunction_* accessors must be owned by py_capi_cfunction_runtime.o."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_cfunction_runtime.o"}
    for symbol in (
        "PyCFunction_GetFunction",
        "PyCFunction_GetSelf",
        "PyCFunction_GetFlags",
    ):
        assert owners[symbol] == expected_owner, symbol


def test_c_api_type_core_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """The PyType core (Ready/Alloc/New/FromSpec/GetSlot/GetFlags/Modified)
    must be owned by py_capi_type_runtime.o.  The module-state pair stays in
    the C shim until the PyModule registry migrates."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_type_runtime.o"}
    for symbol in (
        "PyType_Ready",
        "PyType_Modified",
        "PyType_GenericAlloc",
        "PyType_GenericNew",
        "PyType_FromSpec",
        "PyType_GetSlot",
        "PyType_GetFlags",
    ):
        assert owners[symbol] == expected_owner, symbol


def test_c_api_object_call_core_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """The non-variadic PyObject_Call core must be owned by
    py_capi_object_call_runtime.o.  The variadic CallFunctionObjArgs /
    CallMethod* family stays in the C shim."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_object_call_runtime.o"}
    for symbol in (
        "PyObject_Call",
        "PyObject_CallObject",
        "PyObject_CallNoArgs",
        "PyObject_CallOneArg",
    ):
        assert owners[symbol] == expected_owner, symbol


def test_c_api_method_bridge_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """The C-extension method bridge (call entry, wrapper, list.sort bridge)
    must be owned by py_capi_method_bridge_runtime.o."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_method_bridge_runtime.o"}
    for symbol in (
        "pcc_capi_builtin_object_getattr",
        "pcc_capi_method_func_new",
        "pcc_capi_method_call_entry",
        "pcc_capi_prepare_call_args",
    ):
        assert owners[symbol] == expected_owner, symbol


def test_c_api_object_attr_surface_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """The PyObject attr surface must be owned by py_capi_object_attr_runtime.o."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_object_attr_runtime.o"}
    for symbol in (
        "PyObject_GetAttr",
        "PyObject_GetAttrString",
        "PyObject_SetAttr",
        "PyObject_SetAttrString",
        "PyObject_HasAttr",
        "PyObject_HasAttrString",
        "PyObject_HasAttrWithError",
        "PyObject_HasAttrStringWithError",
        "PyObject_GetOptionalAttr",
        "PyObject_GetOptionalAttrString",
    ):
        assert owners[symbol] == expected_owner, symbol


def test_c_api_number_surface_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """The PyNumber_* surface must be owned by py_capi_number_runtime.o."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_number_runtime.o"}
    for symbol in (
        "PyNumber_Check",
        "PyNumber_Long",
        "PyNumber_Float",
        "PyNumber_Index",
        "PyNumber_Absolute",
        "PyNumber_Negative",
        "PyNumber_Positive",
        "PyNumber_Invert",
        "PyNumber_Add",
        "PyNumber_Subtract",
        "PyNumber_Multiply",
        "PyNumber_Remainder",
        "PyNumber_Divmod",
        "PyNumber_Power",
        "PyNumber_FloorDivide",
        "PyNumber_TrueDivide",
        "PyNumber_Lshift",
        "PyNumber_Rshift",
        "PyNumber_And",
        "PyNumber_Xor",
        "PyNumber_Or",
        "PyNumber_AsSsize_t",
    ):
        assert owners[symbol] == expected_owner, symbol


def test_c_api_sequence_surface_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """The PySequence_* surface must be owned by py_capi_sequence_runtime.o."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_sequence_runtime.o"}
    for symbol in (
        "PySequence_Check",
        "PySequence_Size",
        "PySequence_Length",
        "PySequence_GetItem",
        "PySequence_SetItem",
        "PySequence_Contains",
        "PySequence_Concat",
        "PySequence_InPlaceConcat",
        "PySequence_Repeat",
        "PySequence_InPlaceRepeat",
        "PySequence_Fast",
        "PySequence_Fast_GET_SIZE",
        "PySequence_Fast_ITEMS",
        "PySequence_List",
        "PySequence_Tuple",
    ):
        assert owners[symbol] == expected_owner, symbol


def test_c_api_mapping_surface_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """The PyMapping_* surface must be owned by py_capi_mapping_runtime.o."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_mapping_runtime.o"}
    for symbol in (
        "PyMapping_Check",
        "PyMapping_Size",
        "PyMapping_Length",
        "PyMapping_Keys",
        "PyMapping_Values",
        "PyMapping_Items",
        "PyMapping_GetItemString",
        "PyMapping_SetItemString",
        "PyMapping_GetOptionalItem",
        "PyMapping_GetOptionalItemString",
        "PyMapping_HasKey",
        "PyMapping_HasKeyString",
        "PyMapping_HasKeyWithError",
        "PyMapping_HasKeyStringWithError",
    ):
        assert owners[symbol] == expected_owner, symbol


def test_c_api_import_surface_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """The PyImport_* surface must be owned by py_capi_import_runtime.o."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_import_runtime.o"}
    for symbol in ("PyImport_ImportModule", "PyImport_Import"):
        assert owners[symbol] == expected_owner, symbol


def test_c_api_slice_adjust_indices_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """PySlice_AdjustIndices must be owned by py_capi_sequence_runtime.o."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    assert owners["PySlice_AdjustIndices"] == {"py_capi_sequence_runtime.o"}


def test_c_api_unicode_search_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """The PyUnicode_Count/Find/FindChar/ReadChar surface must be owned by
    py_capi_unicode_search_runtime.o."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_unicode_search_runtime.o"}
    for symbol in (
        "PyUnicode_Count",
        "PyUnicode_Find",
        "PyUnicode_FindChar",
        "PyUnicode_ReadChar",
    ):
        assert owners[symbol] == expected_owner, symbol


def test_c_api_unicode_new_kind_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """PyUnicode_New/KIND must be owned by py_capi_unicode_runtime.o."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_unicode_runtime.o"}
    for symbol in ("PyUnicode_New", "PyUnicode_KIND"):
        assert owners[symbol] == expected_owner, symbol


def test_c_api_unicode_writer_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """The PyUnicodeWriter_* surface must be owned by
    py_capi_unicode_writer_runtime.o."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_unicode_writer_runtime.o"}
    for symbol in (
        "PyUnicodeWriter_Create",
        "PyUnicodeWriter_Finish",
        "PyUnicodeWriter_Discard",
        "PyUnicodeWriter_WriteChar",
        "PyUnicodeWriter_WriteUTF8",
        "PyUnicodeWriter_WriteStr",
        "PyUnicodeWriter_WriteSubstring",
    ):
        assert owners[symbol] == expected_owner, symbol


def test_c_api_buffer_surface_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """The PyObject_CheckBuffer/GetBuffer + PyMemoryView_* surface must be
    owned by py_capi_buffer_runtime.o."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_buffer_runtime.o"}
    for symbol in (
        "PyObject_CheckBuffer",
        "PyObject_GetBuffer",
        "PyMemoryView_Check",
        "PyMemoryView_FromObject",
        "PyMemoryView_FromMemory",
        "pcc_PyMemoryView_GET_BASE",
        "pcc_PyMemoryView_GET_BUFFER",
    ):
        assert owners[symbol] == expected_owner, symbol
    for symbol in (
        "pcc_gc_memoryview_initialize_owned_buffer",
        "pcc_gc_memoryview_refresh_owned_buffer",
    ):
        assert owners[symbol] == {"freestanding_gc_object_slots.o"}, symbol


def test_c_api_generic_alias_and_tuple_slice_are_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """Py_GenericAlias / PyTuple_GetSlice must be owned by
    py_capi_misc_runtime.o."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_misc_runtime.o"}
    for symbol in ("Py_GenericAlias", "PyTuple_GetSlice"):
        assert owners[symbol] == expected_owner, symbol


def test_c_api_module_attr_surface_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """The PyModule_GetDict/Add* surface must be owned by
    py_capi_module_runtime.o."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_module_runtime.o"}
    for symbol in (
        "PyModule_GetDict",
        "PyModule_AddObject",
        "PyModule_AddObjectRef",
        "PyModule_Add",
        "PyModule_AddIntConstant",
        "PyModule_AddStringConstant",
    ):
        assert owners[symbol] == expected_owner, symbol


def test_c_api_cext_dispatch_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """The C-extension slot dispatch helpers must be owned by
    py_capi_cext_runtime.o."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_cext_runtime.o"}
    for symbol in (
        "pcc_capi_cext_object_iter",
        "pcc_capi_cext_object_repr",
        "pcc_capi_cext_object_next",
        "pcc_capi_cext_object_is_iterator",
        "pcc_capi_cext_object_getitem",
        "pcc_capi_cext_object_getattr",
        "pcc_capi_cext_object_setattr",
        "pcc_capi_cext_object_is_callable",
        "pcc_capi_call_cext_object",
        "pcc_capi_cext_truthy",
        "pcc_capi_cext_richcompare_bool",
        "pcc_capi_cext_absolute",
        "pcc_capi_cext_binary_number",
        "pcc_capi_cext_subtract",
        "pcc_capi_type_object_is_callable",
    ):
        assert owners[symbol] == expected_owner, symbol


def test_c_api_sys_getobject_and_unicode_format_are_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """PySys_GetObject / PyUnicode_Format must be owned by
    py_capi_misc_runtime.o."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_misc_runtime.o"}
    for symbol in ("PySys_GetObject", "PyUnicode_Format"):
        assert owners[symbol] == expected_owner, symbol


def test_c_api_module_state_registry_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """The module-state registry + PyModule_Create2/GetState +
    PyType_GetModule(ByDef)/FromModuleAndSpec must be owned by
    py_capi_module_state_runtime.o."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_module_state_runtime.o"}
    for symbol in (
        "pcc_capi_find_module_state",
        "pcc_capi_register_module_state",
        "PyModule_Create2",
        "PyModule_GetState",
        "PyType_FromModuleAndSpec",
        "PyType_GetModule",
        "PyType_GetModuleByDef",
    ):
        assert owners[symbol] == expected_owner, symbol


def test_c_api_module_loader_and_misc_helpers_are_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """Module loader + dealloc/set_type/unicode_read/GET_BASE helpers must be
    owned by their pcc-Python modules."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    assert owners["pcc_capi_module_from_def"] == {"py_capi_module_state_runtime.o"}
    assert owners["pcc_capi_module_run_exec_slots"] == {"py_capi_module_state_runtime.o"}
    assert owners["pcc_capi_module_exec"] == {"py_capi_module_state_runtime.o"}
    assert owners["PyModuleDef_Init"] == {"py_capi_module_runtime.o"}
    assert owners["pcc_capi_is_moduledef"] == {"py_capi_module_runtime.o"}
    assert owners["pcc_capi_dealloc_cext_object"] == {"py_capi_cext_runtime.o"}
    assert owners["pcc_capi_set_type"] == {"py_capi_cext_runtime.o"}
    assert owners["pcc_capi_unicode_read"] == {"py_capi_unicode_search_runtime.o"}
    assert owners["pcc_PyMemoryView_GET_BASE"] == {"py_capi_buffer_runtime.o"}


def test_c_api_private_helpers_are_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """The _PyObject_New/NewVar/GC_New/_PyDict_GetItem_KnownHash helpers must
    be owned by py_capi_private_runtime.o."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_private_runtime.o"}
    for symbol in (
        "PyObject_New",
        "PyObject_NewVar",
        "PyObject_GC_New",
        "PyDict_GetItem_KnownHash",
    ):
        assert owners[symbol] == expected_owner, symbol


def test_c_api_slice_surface_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """PySlice_New / PySlice_GetIndicesEx + the slice type callbacks must be
    owned by py_capi_slice_runtime.o."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_slice_runtime.o"}
    for symbol in (
        "PySlice_New",
        "PySlice_GetIndicesEx",
        "pcc_capi_slice_dealloc",
        "pcc_capi_slice_traverse",
    ):
        assert owners[symbol] == expected_owner, symbol


def test_c_api_seqiter_surface_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """PySeqIter_New + seqiter type callbacks must be owned by
    py_capi_seqiter_runtime.o."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_seqiter_runtime.o"}
    for symbol in (
        "PySeqIter_New",
        "pcc_capi_is_seqiter",
        "pcc_capi_seqiter_next",
        "pcc_capi_seqiter_dealloc",
        "pcc_capi_seqiter_traverse",
    ):
        assert owners[symbol] == expected_owner, symbol


def test_c_api_contextvar_surface_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """PyContextVar_New/Get/Set must be owned by
    py_capi_contextvar_runtime.o."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_contextvar_runtime.o"}
    for symbol in (
        "PyContextVar_New",
        "PyContextVar_Get",
        "PyContextVar_Set",
        "pcc_capi_contextvar_dealloc",
        "pcc_capi_contextvar_traverse",
    ):
        assert owners[symbol] == expected_owner, symbol


def test_c_api_call_surface_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """The PyObject_CallMethod/CallFunction family must be owned by
    py_capi_call_runtime.o."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_call_runtime.o"}
    for symbol in (
        "PyObject_CallMethodNoArgs",
        "PyObject_CallMethodOneArg",
        "PyObject_CallFunctionObjArgs",
        "PyObject_CallMethodObjArgs",
        "PyObject_CallFunction",
        "PyObject_CallMethod",
    ):
        assert owners[symbol] == expected_owner, symbol


def test_c_api_arg_surface_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """The PyArg_* surface must be owned by py_capi_arg_runtime.o."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_arg_runtime.o"}
    for symbol in (
        "PyArg_ParseTuple",
        "PyArg_ParseTupleAndKeywords",
        "PyArg_UnpackTuple",
        "PyArg_VaParseTupleAndKeywords",
    ):
        assert owners[symbol] == expected_owner, symbol


def test_c_api_str_conv_surface_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """PyFloat_FromString / PyLong_FromUnicodeObject must be owned by
    py_capi_str_conv_runtime.o."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_str_conv_runtime.o"}
    for symbol in ("PyFloat_FromString", "PyLong_FromUnicodeObject"):
        assert owners[symbol] == expected_owner, symbol


def test_c_api_import_helpers_are_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """py_builtin_import must be owned by py_capi_import_runtime.o."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    assert owners["py_builtin_import"] == {"py_capi_import_runtime.o"}


def test_c_api_generic_attr_surface_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """PyObject_GenericGetAttr/SetAttr/GetDict must be owned by
    py_capi_cext_runtime.o."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_cext_runtime.o"}
    for symbol in (
        "PyObject_GenericGetAttr",
        "PyObject_GenericSetAttr",
        "PyObject_GenericGetDict",
    ):
        assert owners[symbol] == expected_owner, symbol


def test_c_api_member_slot_helpers_are_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """pcc_capi_member_get / pcc_capi_object_dict_slot must be owned by
    py_capi_cext_runtime.o."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_cext_runtime.o"}
    for symbol in ("pcc_capi_member_get", "pcc_capi_object_dict_slot"):
        assert owners[symbol] == expected_owner, symbol


def test_c_api_hash_double_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """_Py_HashDouble must be owned by py_capi_cext_runtime.o."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    assert owners["Py_HashDouble"] == {"py_capi_cext_runtime.o"}


def test_c_api_buildvalue_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """Py_BuildValue must be owned by py_capi_buildvalue_runtime.o."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    assert owners["Py_BuildValue"] == {"py_capi_buildvalue_runtime.o"}


def test_c_api_type_descriptor_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """The type-descriptor walk + call entries must be owned by
    py_capi_type_descriptor_runtime.o."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_type_descriptor_runtime.o"}
    for symbol in (
        "pcc_capi_type_object_getattr",
        "pcc_capi_unbound_method_call_entry",
        "pcc_capi_data_descriptor_call_entry",
        "pcc_capi_richcompare_descriptor_call_entry",
    ):
        assert owners[symbol] == expected_owner, symbol


def test_c_api_visit_surface_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """The GC object-slot visit surface must be owned by py_capi_visit_runtime.o."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_visit_runtime.o"}
    for symbol in (
        "pcc_capi_visit_slot",
        "pcc_capi_visit_cext_object_slots",
        "pcc_capi_visit_cext_object_slots_i64",
        "pcc_capi_visit_cext_object_slot_i64_adapter",
        "pcc_capi_visit_cext_object_slot_ref",
    ):
        assert owners[symbol] == expected_owner, symbol


def test_c_api_errno_surface_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """PyErr_SetFromErrno must be owned by py_capi_misc_runtime.o."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_misc_runtime.o"}
    for symbol in (
        "PyErr_SetFromErrno",
        "PyErr_SetFromErrnoWithFilenameObject",
    ):
        assert owners[symbol] == expected_owner, symbol
    assert owners["pcc_errno_get"] == {"freestanding_errno.o"}
    assert owners["pcc_errno_set"] == {"freestanding_errno.o"}
    assert owners["pcc_errno_message_into"] == {"freestanding_errno.o"}
    for symbol in (
        "pcc_errno_location",
        "pcc_errno_linux_c_locale_message",
        "pcc_errno_copy_message",
        "pcc_errno_write_unknown_error",
    ):
        assert owners[symbol] == {"freestanding_errno.o"}, symbol

    undefined = _undefined_symbol_users(pcc_py_runtime_archive)
    assert owners.get("pcc_capi_errno_message", set()) == set()
    assert undefined.get("pcc_capi_errno_message", set()) == set()


def test_c_api_print_surface_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """PyObject_Print must be owned by py_capi_misc_runtime.o."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected_owner = {"py_capi_misc_runtime.o"}
    assert owners["PyObject_Print"] == expected_owner
    assert owners["pcc_capi_file_write"] == expected_owner
    assert owners["pcc_capi_file_flush"] == expected_owner


def test_c_api_getbuffer_is_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """The pcc-Python owner must replace the transitional TLS helper."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    undefined = _undefined_symbol_users(pcc_py_runtime_archive)
    assert owners["pcc_PyMemoryView_GET_BUFFER"] == {"py_capi_buffer_runtime.o"}
    assert owners.get("pcc_capi_memoryview_tls_buffer", set()) == set()
    assert undefined.get("pcc_capi_memoryview_tls_buffer", set()) == set()


def test_c_api_compat_object_is_absent(
    pcc_py_runtime_archive: Path,
) -> None:
    """The C shim remains an oracle source, never a production member."""
    assert "py_capi_compat.o" not in _archive_members(pcc_py_runtime_archive)


def test_c_api_recovered_drift_symbols_are_owned_by_pcc_python(
    pcc_py_runtime_archive: Path,
) -> None:
    """C-API reachability additions must not silently regrow compat C."""
    owners = _defined_symbol_owners(pcc_py_runtime_archive)
    expected = {
        "pcc_py_type_of": "py_capi_core_runtime.o",
        "PyEval_GetBuiltins": "py_capi_core_runtime.o",
        "PyDateTimeAPI": "py_capi_core_runtime.o",
        "pcc_capi_call_type_object": "py_capi_cext_runtime.o",
        "PyUnicode_Compare": "py_capi_unicode_runtime.o",
        "PyUnicode_CompareWithASCIIString": "py_capi_unicode_runtime.o",
        "PyObject_Vectorcall": "py_capi_object_call_runtime.o",
        "PyObject_VectorcallMethod": "py_capi_object_call_runtime.o",
        "PyVectorcall_Call": "py_capi_object_call_runtime.o",
        "PyVectorcall_NARGS": "py_capi_object_call_runtime.o",
        "PyIter_Check": "py_capi_seqiter_runtime.o",
        "PyIter_Next": "py_capi_seqiter_runtime.o",
        "PyIter_NextItem": "py_capi_seqiter_runtime.o",
    }
    for symbol, owner in expected.items():
        assert owners.get(symbol) == {owner}, symbol
    undefined = _undefined_symbol_users(pcc_py_runtime_archive)
    assert owners.get("pcc_capi_call_int_conversion_slot", set()) == set()
    assert undefined.get("pcc_capi_call_int_conversion_slot", set()) == set()


def test_capi_runtime_modules_have_no_unimported_unsafe_intrinsics() -> None:
    """Every pcc.unsafe intrinsic used by a py_capi_*_runtime module must be
    imported by it.  The pcc frontend silently compiles an unimported name into
    an external symbol reference (link-time undefined or a zeroed global load
    at runtime), which has burned us repeatedly (wrapping_mul_i64 in the
    exception formatter turned every negative %d into 0).  This lint closes
    that class of regression."""
    unsafe_tree = ast.parse(
        (REPO_ROOT / "pcc" / "unsafe" / "__init__.py").read_text(
            encoding="utf-8"
        )
    )
    intrinsics = {
        node.name for node in unsafe_tree.body if isinstance(node, ast.FunctionDef)
    }
    bad: list[str] = []
    modules = sorted(
        (REPO_ROOT / "pcc" / "py_runtime" / "py").glob("py_capi_*_runtime.py")
    )
    for module in modules:
        tree = ast.parse(module.read_text(encoding="utf-8"))
        imported: set[str] = set()
        locally_bound: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == "pcc.unsafe":
                imported.update(alias.asname or alias.name for alias in node.names)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                locally_bound.add(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                locally_bound.update(
                    target.id for target in targets if isinstance(target, ast.Name)
                )
        used = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        missing = sorted((used & intrinsics) - imported - locally_bound)
        if missing:
            bad.append(f"{module.name}: missing pcc.unsafe imports {missing}")
    assert not bad, "\n".join(bad)


def test_capi_runtime_modules_import_every_extern_marker_they_use() -> None:
    """An unimported C-ABI marker must not silently become a global lookup."""
    extern_src = (REPO_ROOT / "pcc" / "extern" / "__init__.py").read_text(
        encoding="utf-8"
    )
    extern_tree = ast.parse(extern_src)
    exported: set[str] = set()
    for node in extern_tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and (
                target.id.startswith("c_") or target.id == "extern"
            ):
                exported.add(target.id)
    exported.update({"extern", "c_abi_export", "c_abi_typed_export", "c_abi_variadic_export"})

    bad: list[str] = []
    modules = sorted(
        (REPO_ROOT / "pcc" / "py_runtime" / "py").glob("py_capi_*_runtime.py")
    )
    for module in modules:
        tree = ast.parse(module.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "pcc.extern":
                imported.update(alias.asname or alias.name for alias in node.names)
        used = {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in exported
        }
        missing = sorted(used - imported)
        if missing:
            bad.append(f"{module.name}: missing pcc.extern imports {missing}")
    assert not bad, "\n".join(bad)


def test_libpython_variant_has_no_capi_compat_shadow(
    pcc_py_runtime_archive: Path,
) -> None:
    """The variant retains pcc internals but never the removed C compat shim.

    Public pcc C-API definitions are made non-exported at the final libpython
    link boundary; deleting their archive members would also delete internal
    pcc object-model behavior.
    """
    variant = pcc_py_runtime_archive.parent / "libpy_runtime_pcc_py_libpython.a"
    if not variant.is_file():
        return  # variant not built in this test run
    members = _archive_members(variant)
    assert "py_capi_compat.o" not in members
    assert {member for member in members if member.startswith("py_capi_")}
    owners = _defined_symbol_owners(variant)
    assert {
        symbol: symbol_owners
        for symbol, symbol_owners in owners.items()
        if symbol.startswith("Py") or symbol.startswith("_Py")
    }

    users = _undefined_symbol_users(variant)
    capsule_internal = {
        "pcc_py_capsule_new",
        "pcc_py_capsule_get_pointer",
        "pcc_py_capsule_get_name",
        "pcc_py_capsule_is_valid",
        "pcc_py_capsule_set_name",
    }
    for symbol in capsule_internal:
        assert owners[symbol] == {"py_capi_capsule_runtime.o"}
        assert "py_dlpack_runtime.o" in users[symbol]
    for symbol in {
        "PyCapsule_New",
        "PyCapsule_GetPointer",
        "PyCapsule_GetName",
        "PyCapsule_IsValid",
        "PyCapsule_SetName",
    }:
        assert "py_dlpack_runtime.o" not in users.get(symbol, set())
    assert owners["pcc_py_long_from_void_ptr"] == {
        "py_capi_numeric_runtime.o"
    }
    assert owners["pcc_py_long_as_void_ptr"] == {
        "py_capi_numeric_runtime.o"
    }
