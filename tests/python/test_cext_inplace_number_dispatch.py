"""C-extension augmented assignment must use ``nb_inplace_*`` slots.

Regression for the generic mechanism reached by NumPy's ``polyfit`` scaling
step (``lhs /= scale``).  Dynamic extension tags live above ``PY_TYPE_USER``;
``py_obj_inplace_op`` used to misclassify them as pcc user instances and read
the extension object with the incompatible ``PyInstanceObject`` layout.

The synthetic extension keeps this package-independent.  Its true-division
slot models array scaling: the in-place slot mutates and preserves identity,
while a deliberate ``NotImplemented`` result must fall back to ordinary
``nb_true_divide`` and produce a distinct result.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).absolute().parents[2]

pytestmark = pytest.mark.xdist_group(name="pcc_heavy_llvm")


SCALEDEMO_SOURCE = r"""
#define PY_SSIZE_T_CLEAN
#include <Python.h>

typedef struct {
    PyObject_HEAD
    long value;
    int mode;
} ScaleObject;

static PyTypeObject ScaleType;

static PyObject *scale_new_value(long value, int mode) {
    PyObject *obj = PyType_GenericNew(&ScaleType, NULL, NULL);
    if (obj == NULL) return NULL;
    ((ScaleObject *)obj)->value = value;
    ((ScaleObject *)obj)->mode = mode;
    return obj;
}

static int scale_divisor(PyObject *right, long *out) {
    long divisor = PyLong_AsLong(right);
    if (PyErr_Occurred() != NULL) return -1;
    if (divisor == 0) {
        PyErr_SetString(PyExc_ZeroDivisionError, "division by zero");
        return -1;
    }
    *out = divisor;
    return 0;
}

static PyObject *scale_true_divide(PyObject *left, PyObject *right) {
    long divisor = 0;
    if (!PyObject_TypeCheck(left, &ScaleType)) Py_RETURN_NOTIMPLEMENTED;
    if (scale_divisor(right, &divisor) < 0) return NULL;
    return scale_new_value(((ScaleObject *)left)->value / divisor, 0);
}

static PyObject *scale_inplace_true_divide(PyObject *left, PyObject *right) {
    ScaleObject *self = (ScaleObject *)left;
    long divisor = 0;
    if (!PyObject_TypeCheck(left, &ScaleType)) Py_RETURN_NOTIMPLEMENTED;
    if (self->mode == 1) Py_RETURN_NOTIMPLEMENTED;
    if (self->mode == 2) return NULL;  /* fail-closed slot contract */
    if (scale_divisor(right, &divisor) < 0) return NULL;
    self->value /= divisor;
    Py_INCREF(left);
    return left;
}

static PyNumberMethods ScaleNumber = {
    .nb_true_divide = scale_true_divide,
    .nb_inplace_true_divide = scale_inplace_true_divide,
};

static PyTypeObject ScaleType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "scaledemo.Scale",
    .tp_basicsize = sizeof(ScaleObject),
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_as_number = &ScaleNumber,
    .tp_new = PyType_GenericNew,
};

static PyObject *make_scale(PyObject *self, PyObject *args) {
    long value = 0;
    int mode = 0;
    (void)self;
    if (!PyArg_ParseTuple(args, "li", &value, &mode)) return NULL;
    return scale_new_value(value, mode);
}

static PyObject *scale_value(PyObject *self, PyObject *args) {
    PyObject *obj = NULL;
    (void)self;
    if (!PyArg_ParseTuple(args, "O", &obj)) return NULL;
    if (!PyObject_TypeCheck(obj, &ScaleType)) {
        PyErr_SetString(PyExc_TypeError, "expected Scale");
        return NULL;
    }
    return PyLong_FromLong(((ScaleObject *)obj)->value);
}

static PyMethodDef ScaleMethods[] = {
    {"make", make_scale, METH_VARARGS, NULL},
    {"value", scale_value, METH_VARARGS, NULL},
    {NULL, NULL, 0, NULL},
};

static PyModuleDef scalemodule = {
    PyModuleDef_HEAD_INIT, "scaledemo", NULL, -1, ScaleMethods,
};

PyMODINIT_FUNC PyInit_scaledemo(void) {
    if (PyType_Ready(&ScaleType) < 0) return NULL;
    return PyModule_Create(&scalemodule);
}
"""


MAIN_SOURCE = """
import scaledemo

lhs = scaledemo.make(12, 0)
alias = lhs
lhs /= 3
print("inplace", scaledemo.value(lhs), scaledemo.value(alias), lhs is alias)

fallback = scaledemo.make(20, 1)
fallback_alias = fallback
fallback /= 4
print(
    "fallback",
    scaledemo.value(fallback),
    scaledemo.value(fallback_alias),
    fallback is fallback_alias,
)

silent = scaledemo.make(8, 2)
try:
    silent /= 2
except RuntimeError:
    print("silent-null RuntimeError")

print("DONE")
"""


def _compile_extension(tmp_path: Path) -> Path:
    site = tmp_path / "site"
    site.mkdir(exist_ok=True)
    source = tmp_path / "scaledemo.c"
    source.write_text(SCALEDEMO_SOURCE, encoding="utf-8")
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
        str(site / "scaledemo.so"),
    ]
    if sys.platform == "darwin":
        command.extend(["-undefined", "dynamic_lookup"])
    subprocess.run(command, check=True, text=True, capture_output=True, timeout=60)
    return site


def test_cext_inplace_true_divide_and_fallback(
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
    assert "inplace 4 4 True" in lines, run.stdout
    assert "fallback 5 20 False" in lines, run.stdout
    assert "silent-null RuntimeError" in lines, run.stdout
    assert "DONE" in lines, run.stdout
