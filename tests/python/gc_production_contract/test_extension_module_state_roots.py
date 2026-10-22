"""5-GC common production contract: pcc-native extension module state roots.

Part of the 5-GC Production Equality Rule (docs/goal/goal-prompt.md G-track).
An imported pcc-native extension may own PyObject references only through its
``PyModuleDef.m_size`` module state. When it exposes those references through
``m_traverse``, every GC backend must treat them as roots.

The extension below stores a list only in module state, never as a module
attribute. The compiled program imports the extension, drops through repeated
``gc.collect()`` calls, then calls back into the extension to append through the
same state pointer. The expected increasing sizes prove the state-held list
survived under PCC_GC_BACKEND 0..4.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).absolute().parents[3]


_EXTENSION_SOURCE = r"""
#define PY_SSIZE_T_CLEAN
#include <Python.h>

typedef struct {
    PyObject *items;
} StateDemoState;

static int statedemo_traverse(PyObject *module, visitproc visit, void *arg) {
    StateDemoState *state = (StateDemoState *)PyModule_GetState(module);
    if (state == NULL) return 0;
    Py_VISIT(state->items);
    return 0;
}

static int statedemo_exec(PyObject *module) {
    StateDemoState *state = (StateDemoState *)PyModule_GetState(module);
    if (state == NULL) return -1;
    state->items = PyList_New(0);
    if (state->items == NULL) return -1;
    PyObject *seed = PyLong_FromLong(41);
    if (seed == NULL) return -1;
    if (PyList_Append(state->items, seed) != 0) return -1;
    Py_DECREF(seed);
    return 0;
}

static PyObject *statedemo_push(PyObject *self, PyObject *args) {
    (void)args;
    StateDemoState *state = (StateDemoState *)PyModule_GetState(self);
    if (state == NULL || state->items == NULL) return NULL;
    PyObject *next = PyLong_FromLong(PyList_Size(state->items));
    if (next == NULL) return NULL;
    if (PyList_Append(state->items, next) != 0) return NULL;
    Py_DECREF(next);
    return PyLong_FromLong(PyList_Size(state->items));
}

static PyMethodDef StateDemoMethods[] = {
    {"push", statedemo_push, METH_VARARGS, "append through module state"},
    {NULL, NULL, 0, NULL},
};

static PyModuleDef_Slot StateDemoSlots[] = {
    {Py_mod_exec, (void *)statedemo_exec},
    {0, NULL},
};

static PyModuleDef StateDemoModule = {
    PyModuleDef_HEAD_INIT,
    "statedemo",
    NULL,
    sizeof(StateDemoState),
    StateDemoMethods,
    StateDemoSlots,
    statedemo_traverse,
    NULL,
    NULL,
};

PyMODINIT_FUNC PyInit_statedemo(void) {
    return PyModuleDef_Init(&StateDemoModule);
}
"""


_PROGRAM = (
    "import gc\n"
    "import statedemo\n"
    "\n"
    "print(statedemo.push())\n"
    "gc.collect()\n"
    "print(statedemo.push())\n"
    "gc.collect()\n"
    "print(statedemo.push())\n"
)


@pytest.fixture(scope="module")
def _extension_module_state_exe(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("gc_extension_module_state")
    site = tmp / "site"
    site.mkdir()
    src = tmp / "statedemo.c"
    src.write_text(_EXTENSION_SOURCE, encoding="utf-8")
    ext = site / "statedemo.so"
    cc = os.environ.get("CC", "cc")
    ext_cmd = [
        cc,
        "-shared",
        "-fPIC",
        "-I",
        str(REPO_ROOT / "utils" / "fake_libc_include"),
        "-I",
        str(REPO_ROOT / "pcc" / "runtime" / "include"),
        str(src),
        "-o",
        str(ext),
    ]
    if sys.platform == "darwin":
        ext_cmd.extend(["-undefined", "dynamic_lookup"])
    build_ext = subprocess.run(
        ext_cmd,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert build_ext.returncode == 0, build_ext.stdout + build_ext.stderr

    main = tmp / "main.py"
    main.write_text(_PROGRAM, encoding="utf-8")
    exe = tmp / "extension_module_state_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    build = subprocess.run(
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
            str(exe),
        ],
        text=True,
        capture_output=True,
        timeout=420,
        env=env,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return str(exe), str(site)


@pytest.mark.parametrize("backend", ["0", "1", "2", "3", "4"])
def test_extension_module_state_roots(_extension_module_state_exe, backend):
    exe, site = _extension_module_state_exe
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = site
    env["PCC_GC_BACKEND"] = backend
    run = subprocess.run(
        [exe],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert run.returncode == 0, (
        f"backend #{backend} rc={run.returncode}: {run.stderr.strip()[:200]}"
    )
    assert run.stdout.splitlines()[:3] == ["2", "3", "4"], run.stdout
