"""``len()`` and ``str()`` must dispatch to a C-extension object's own slots.

Regression for docs/investigations/
numpy-dyn-reachability-selfbackend-link-gap.md: ``py_obj_getitem`` had a
C-extension branch but its neighbours did not, so on a cext object

  * ``len(obj)`` fell through to the user-class ``__len__`` lookup, found
    nothing, and silently returned **0** (``len(np.array([7,8,9]))`` == 0
    while ``a[0]`` was correct), and
  * ``str(obj)`` returned NULL, so ``"x=" + str(obj)`` rendered ``<null>``
    even though ``print(obj)`` was fine — the print formatter carried the
    tp_repr fallback that ``py_obj_str`` itself lacked.

The fixture is a synthetic extension type exposing ``sq_length`` and
``tp_repr`` — no NumPy and no package site needed, so this stays a fast
unit-level gate for the generic mechanism rather than a package test.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).absolute().parents[2]

pytestmark = pytest.mark.xdist_group(name="pcc_heavy_llvm")


LENDEMO_SOURCE = """
#define PY_SSIZE_T_CLEAN
#include <Python.h>

typedef struct { PyObject_HEAD long n; } LenObject;

static Py_ssize_t len_length(PyObject *self) {
    return (Py_ssize_t)((LenObject *)self)->n;
}

static PyObject *len_repr(PyObject *self) {
    (void)self;
    return PyUnicode_FromString("lendemo-repr");
}

static PySequenceMethods LenSeq = { .sq_length = len_length };

static PyTypeObject LenType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "lendemo.Box",
    .tp_basicsize = sizeof(LenObject),
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_new = PyType_GenericNew,
    .tp_repr = len_repr,
    .tp_as_sequence = &LenSeq,
};

static PyObject *make_box(PyObject *self, PyObject *args) {
    (void)self;
    (void)args;
    PyObject *obj = PyType_GenericNew(&LenType, NULL, NULL);
    if (obj == NULL) return NULL;
    ((LenObject *)obj)->n = 3;
    return obj;
}

static PyMethodDef LenMethods[] = {
    {"make_box", make_box, METH_VARARGS, NULL},
    {NULL, NULL, 0, NULL},
};

static PyModuleDef lenmodule = {
    PyModuleDef_HEAD_INIT, "lendemo", NULL, -1, LenMethods,
};

PyMODINIT_FUNC PyInit_lendemo(void) {
    if (PyType_Ready(&LenType) < 0) return NULL;
    return PyModule_Create(&lenmodule);
}
"""


MAIN_SOURCE = """
import lendemo

box = lendemo.make_box()
print("len=" + str(len(box)))
print("str=" + str(box))
print("DONE")
"""


def _compile_extension(tmp_path: Path) -> Path:
    site = tmp_path / "site"
    site.mkdir(exist_ok=True)
    src = tmp_path / "lendemo.c"
    src.write_text(LENDEMO_SOURCE, encoding="utf-8")
    cmd = [
        os.environ.get("CC", "cc"),
        "-shared",
        "-fPIC",
        "-I",
        str(REPO / "utils" / "fake_libc_include"),
        "-I",
        str(REPO / "pcc" / "runtime" / "include"),
        str(src),
        "-o",
        str(site / "lendemo.so"),
    ]
    if sys.platform == "darwin":
        cmd.extend(["-undefined", "dynamic_lookup"])
    subprocess.run(cmd, check=True, text=True, capture_output=True, timeout=60)
    return site


def test_len_and_str_use_cext_slots(tmp_path, pcc_runtime_archive):
    site = _compile_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(MAIN_SOURCE, encoding="utf-8")
    exe = tmp_path / "main_bin"

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    env["PCC_RUNTIME_ARCHIVE"] = str(pcc_runtime_archive)
    # Keep the program self-backend/no-libpython while selecting the explicit
    # system final-link oracle for native-extension export anchors.  The
    # pcc-owned final linker correctly rejects that public surface until it is
    # implemented; this regression is about runtime len/str slot dispatch.
    env["PCC_SELF_LINK"] = "cc"

    compile_proc = subprocess.run(
        [
            "uv", "run", "pcc",
            "--backend", "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(main),
            "-o", str(exe),
        ],
        text=True,
        capture_output=True,
        timeout=300,
        env=env,
    )
    assert compile_proc.returncode == 0, compile_proc.stderr

    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=60, env=env,
    )
    assert run.returncode == 0, run.stderr
    lines = [ln.strip() for ln in run.stdout.strip().splitlines()]
    assert "DONE" in lines, run.stdout
    # sq_length, not a Python __len__ lookup that silently yields 0.
    assert "len=3" in lines, f"len() ignored the cext length slot:\n{run.stdout}"
    # tp_repr through str(), not NULL rendered as <null> by concatenation.
    assert "str=lendemo-repr" in lines, (
        f"str() ignored the cext repr slot:\n{run.stdout}"
    )
