"""GC-P0-CEXT-STRICT-DECREF-TAG-PARITY: a dynamic C-extension object must be
retained and released through the strict refcount protocol exactly like the C
runtime, so its tp_dealloc dispatches once at refcount zero.

The strict pcc-Python `_py_incref_prepare`/`_py_decref_prepare` reject every
tag > 500 before touching the refcount; dynamic C-extension tags start above
the builtin range, so before the fix `py_incref` cannot retain such an object
(refcount stays 1 through a container retain) and the terminal `py_decref`
never runs the deallocator. The C runtime already exempts registry-proven
C-extension tags. This differential proves both mirrors agree.
"""
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from tests.runtime_build_cache import cached_pcc_python_runtime

REPO_ROOT = Path(__file__).resolve().parents[2]


def _runtime(kind: str) -> Path:
    return cached_pcc_python_runtime()


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_dynamic_cext_object_retains_and_deallocs_through_strict_refcount(
    kind: str, tmp_path: Path
) -> None:
    runtime = _runtime(kind)
    archive = runtime / (
        "libpy_runtime_pcc_py.a"
    )
    src = tmp_path / f"cext_strict_lifecycle_{kind}.c"
    exe = tmp_path / f"cext_strict_lifecycle_{kind}.out"
    src.write_text(
        textwrap.dedent("""
        #define PY_SSIZE_T_CLEAN
        #include "Python.h"
        #include <stdint.h>

        void py_incref(PyObject *o);
        void py_decref(PyObject *o);

        typedef struct ProbeManagedObject {
            PyObject_HEAD
            long value;
        } ProbeManagedObject;

        static long dealloc_hits = 0;

        static void probe_managed_dealloc(PyObject *self) {
            (void)self;
            dealloc_hits += 1;
        }

        static PyTypeObject ProbeManagedType = {
            PyVarObject_HEAD_INIT(NULL, 0)
            .tp_name = "pcc_probe.Managed",
            .tp_basicsize = sizeof(ProbeManagedObject),
            .tp_flags = Py_TPFLAGS_DEFAULT | PCC_TPFLAGS_MANAGED_DEALLOC,
            .tp_dealloc = probe_managed_dealloc,
            .tp_new = PyType_GenericNew,
        };

        int main(void) {
            if (PyType_Ready(&ProbeManagedType) != 0) return 2;
            PyObject *obj = PyType_GenericNew(&ProbeManagedType, NULL, NULL);
            if (obj == NULL) return 3;
            PyObjectHeader *h = (PyObjectHeader *)obj;

            if (h->refcount != 1) return 4;    /* allocation owns one ref */
            py_incref(obj);                     /* container retain -> 2 */
            if (h->refcount != 2) return 5;
            py_decref(obj);                     /* caller release -> 1 */
            if (h->refcount != 1) return 6;
            if (dealloc_hits != 0) return 7;
            py_decref(obj);                     /* terminal -> 0, dealloc once */
            if (dealloc_hits != 1) return 8;
            return 0;
        }
        """).lstrip(),
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            os.environ.get("CC", "cc"),
            "-std=c11",
            f"-I{REPO_ROOT / 'utils' / 'fake_libc_include'}",
            f"-I{runtime / 'include'}",
            f"-I{runtime / 'src'}",
            str(src),
            str(archive),
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, (
        f"[{kind}] probe exit {result.returncode} "
        f"(4=alloc rc,5=retain rc,6=release rc,7=early dealloc,8=terminal dealloc)"
        f"\n{result.stdout}{result.stderr}"
    )


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_dynamic_cext_object_list_retain_release_deallocs(
    kind: str, tmp_path: Path
) -> None:
    """Terminal split-store release: a list retains a dynamic C-extension
    object (append), the caller drops its reference, and releasing the list
    runs the object's deallocator exactly once -- the original list-clear
    scenario, proven equal on both mirrors."""
    runtime = _runtime(kind)
    archive = runtime / (
        "libpy_runtime_pcc_py.a"
    )
    src = tmp_path / f"cext_list_split_store_{kind}.c"
    exe = tmp_path / f"cext_list_split_store_{kind}.out"
    src.write_text(
        textwrap.dedent("""
        #define PY_SSIZE_T_CLEAN
        #include "Python.h"
        #include <stdint.h>

        void py_decref(PyObject *o);
        PyObject *py_list_new(long initial_capacity);
        void py_list_append(PyObject *lst, PyObject *item);

        typedef struct ProbeManagedObject {
            PyObject_HEAD
            long value;
        } ProbeManagedObject;

        static long dealloc_hits = 0;
        static void probe_managed_dealloc(PyObject *self) {
            (void)self;
            dealloc_hits += 1;
        }
        static PyTypeObject ProbeManagedType = {
            PyVarObject_HEAD_INIT(NULL, 0)
            .tp_name = "pcc_probe.Managed",
            .tp_basicsize = sizeof(ProbeManagedObject),
            .tp_flags = Py_TPFLAGS_DEFAULT | PCC_TPFLAGS_MANAGED_DEALLOC,
            .tp_dealloc = probe_managed_dealloc,
            .tp_new = PyType_GenericNew,
        };

        int main(void) {
            if (PyType_Ready(&ProbeManagedType) != 0) return 2;
            PyObject *lst = py_list_new(1);
            if (lst == NULL) return 3;
            PyObject *obj = PyType_GenericNew(&ProbeManagedType, NULL, NULL);
            if (obj == NULL) return 4;
            PyObjectHeader *h = (PyObjectHeader *)obj;
            py_list_append(lst, obj);           /* list retains -> 2 */
            if (h->refcount != 2) return 5;
            py_decref(obj);                      /* caller drops -> 1 */
            if (h->refcount != 1) return 6;
            if (dealloc_hits != 0) return 7;
            py_decref(lst);                      /* list release frees obj */
            if (dealloc_hits != 1) return 8;
            return 0;
        }
        """).lstrip(),
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            os.environ.get("CC", "cc"),
            "-std=c11",
            f"-I{REPO_ROOT / 'utils' / 'fake_libc_include'}",
            f"-I{runtime / 'include'}",
            f"-I{runtime / 'src'}",
            str(src),
            str(archive),
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, (
        f"[{kind}] list split-store probe exit {result.returncode} "
        f"(5=retain rc,6=drop rc,7=early dealloc,8=terminal dealloc)"
        f"\n{result.stdout}{result.stderr}"
    )


def test_strict_refcount_guards_accept_only_registry_proven_cext_tags() -> None:
    """Parity/negative contract: strict incref and decref prepare gate the
    >500 rejection on the C-extension registry, mirroring the C owner, so
    registry-proven dynamic tags are accepted while unmanaged/unknown high
    tags stay fail-closed. Prevents a regression back to a blanket >500 reject
    (which drops C-extension owners) or a tag-only exemption without the
    registry authority."""
    py_obj = (REPO_ROOT / "pcc" / "runtime" / "py" / "py_obj.py").read_text(
        encoding="utf-8"
    )
    # The registry extern is the single acceptance authority.
    assert 'pcc_capi_is_cext_type_tag = extern(' in py_obj
    # Both refcount-prepare guards accept ONLY registry-proven high tags.
    assert (
        "or (tag > 500 and pcc_capi_is_cext_type_tag(tag) == 0)" in py_obj
    )
    assert (
        "or (tag_dbg > 500 and pcc_capi_is_cext_type_tag(tag_dbg) == 0)"
        in py_obj
    )
    # No blanket ">500: return" reject survives in the prepare guards.
    assert "\n        or tag > 500\n    ):" not in py_obj
    assert "\n        or tag_dbg > 500\n    ):" not in py_obj
