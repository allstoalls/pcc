"""C-extension subscript stores must reach assignment slots.

This is the package-independent regression for the first silent numerical
corruption in NumPy's Vandermonde construction.  ``vander`` initializes an
empty array with stores shaped like ``tmp[:, 0] = 1`` and
``tmp[:, 1:] = values``.  The generic runtime used to return ``-1`` without an
exception for every such C-extension store, so compilation continued with an
uninitialized matrix and later LAPACK scaling received invalid data.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).absolute().parents[2]

pytestmark = pytest.mark.xdist_group(name="pcc_heavy_llvm")


STOREDEMO_SOURCE = r"""
#define PY_SSIZE_T_CLEAN
#include <Python.h>

typedef struct {
    PyObject_HEAD
    long flags;
    long total;
} StoreObject;

static PyTypeObject StoreType;

static int store_assign(PyObject *object, PyObject *key, PyObject *value) {
    StoreObject *self = (StoreObject *)object;
    long incoming = PyLong_AsLong(value);
    if (PyErr_Occurred() != NULL) return -1;
    if (incoming == 99) return -1;  /* exercise fail-closed slot handling */

    if (PyTuple_Check(key)) {
        PyObject *first = NULL;
        PyObject *second = NULL;
        if (PyTuple_Size(key) != 2) {
            PyErr_SetString(PyExc_TypeError, "expected a two-dimensional key");
            return -1;
        }
        first = PyTuple_GetItem(key, 0);
        second = PyTuple_GetItem(key, 1);
        if (first == NULL || second == NULL || !PySlice_Check(first)) {
            PyErr_SetString(PyExc_TypeError, "expected a leading slice");
            return -1;
        }
        if (PySlice_Check(second)) self->flags |= 2;
        else self->flags |= 1;
    } else if (PySlice_Check(key)) {
        self->flags |= 4;
    } else {
        self->flags |= 8;
    }
    self->total += incoming;
    return 0;
}

static PyMappingMethods StoreMapping = {
    .mp_ass_subscript = store_assign,
};

static PyTypeObject StoreType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "storedemo.Store",
    .tp_basicsize = sizeof(StoreObject),
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_as_mapping = &StoreMapping,
    .tp_new = PyType_GenericNew,
};

static PyObject *make_store(PyObject *self, PyObject *args) {
    (void)self;
    (void)args;
    return PyType_GenericNew(&StoreType, NULL, NULL);
}

static PyObject *store_flags(PyObject *self, PyObject *args) {
    PyObject *object = NULL;
    (void)self;
    if (!PyArg_ParseTuple(args, "O", &object)) return NULL;
    if (!PyObject_TypeCheck(object, &StoreType)) {
        PyErr_SetString(PyExc_TypeError, "expected Store");
        return NULL;
    }
    return PyLong_FromLong(((StoreObject *)object)->flags);
}

static PyObject *store_total(PyObject *self, PyObject *args) {
    PyObject *object = NULL;
    (void)self;
    if (!PyArg_ParseTuple(args, "O", &object)) return NULL;
    if (!PyObject_TypeCheck(object, &StoreType)) {
        PyErr_SetString(PyExc_TypeError, "expected Store");
        return NULL;
    }
    return PyLong_FromLong(((StoreObject *)object)->total);
}

static PyMethodDef StoreMethods[] = {
    {"make", make_store, METH_VARARGS, NULL},
    {"flags", store_flags, METH_VARARGS, NULL},
    {"total", store_total, METH_VARARGS, NULL},
    {NULL, NULL, 0, NULL},
};

static PyModuleDef storemodule = {
    PyModuleDef_HEAD_INIT, "storedemo", NULL, -1, StoreMethods,
};

PyMODINIT_FUNC PyInit_storedemo(void) {
    if (PyType_Ready(&StoreType) < 0) return NULL;
    return PyModule_Create(&storemodule);
}
"""


MAIN_SOURCE = """
import storedemo

matrix = storedemo.make()
matrix[:, 0] = 1
matrix[:, 1:] = 2
matrix[1:4] = 4
matrix[3] = 8
print("assign", storedemo.flags(matrix), storedemo.total(matrix))

try:
    matrix[:, 0] = 99
except RuntimeError:
    print("silent-tuple-failure RuntimeError")

try:
    matrix[1:4] = 99
except RuntimeError:
    print("silent-slice-failure RuntimeError")

print("DONE")
"""


def _compile_extension(tmp_path: Path) -> Path:
    site = tmp_path / "site"
    site.mkdir(exist_ok=True)
    source = tmp_path / "storedemo.c"
    source.write_text(STOREDEMO_SOURCE, encoding="utf-8")
    command = [
        os.environ.get("CC", "cc"),
        "-shared",
        "-fPIC",
        "-I",
        str(REPO / "utils" / "fake_libc_include"),
        "-I",
        str(REPO / "pcc" / "runtime" / "include"),
        str(source),
        "-o",
        str(site / "storedemo.so"),
    ]
    if sys.platform == "darwin":
        command.extend(["-undefined", "dynamic_lookup"])
    subprocess.run(command, check=True, text=True, capture_output=True, timeout=60)
    return site


def test_cext_mapping_assignment_for_vander_shaped_keys(
    tmp_path,
    pcc_runtime_archive,
):
    site = _compile_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(MAIN_SOURCE, encoding="utf-8")
    executable = tmp_path / "main_bin"

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    # The program is self-backend/no-libpython, but loading a native extension
    # requires export anchors that the pcc-owned final linker deliberately
    # rejects until it implements that public link surface.  Select the system
    # linker explicitly so this regression remains about runtime slot dispatch.
    env["PCC_SELF_LINK"] = "cc"
    env["PCC_RUNTIME_CC"] = "pcc"
    env["PCC_RUNTIME_ARCHIVE"] = str(pcc_runtime_archive)

    compile_proc = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(main),
            "-o",
            str(executable),
        ],
        text=True,
        capture_output=True,
        timeout=300,
        env=env,
    )
    assert compile_proc.returncode == 0, compile_proc.stderr

    run = subprocess.run(
        [str(executable)], text=True, capture_output=True, timeout=60, env=env
    )
    assert run.returncode == 0, run.stderr
    lines = [line.strip() for line in run.stdout.splitlines() if line.strip()]
    assert "assign 15 15" in lines, run.stdout
    assert "silent-tuple-failure RuntimeError" in lines, run.stdout
    assert "silent-slice-failure RuntimeError" in lines, run.stdout
    assert "DONE" in lines, run.stdout
