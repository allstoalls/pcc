"""pcc-native CPython extension loading under strict no-libpython.

Every build here goes through the platform linker (``PCC_SELF_LINK=cc``).
Loading a CPython extension needs C-API export anchors in the executable, and
pcc's own Mach-O link mode states it does not synthesize them
(``pipeline_self_link.validate_pcc_self_link_surface``). On Darwin AArch64
``pcc`` is the default mode, so without declaring ``cc`` every test in this
file fails on that capability boundary rather than on anything the extension
loader does.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from pcc1_gate import repo_root

REPO = repo_root()
if not (REPO / "utils" / "fake_libc_include" / "Python.h").exists():
    REPO = REPO / "pcc"


def _compile_extension(tmp_path: Path, module_name: str, source: str) -> Path:
    site = tmp_path / "site"
    site.mkdir(exist_ok=True)
    src = tmp_path / f"{module_name}.c"
    src.write_text(source, encoding="utf-8")
    return _compile_extension_source(tmp_path, module_name, src)


def _compile_extension_source(tmp_path: Path, module_name: str, source: Path) -> Path:
    site = tmp_path / "site"
    site.mkdir(exist_ok=True)
    out = site / f"{module_name}.so"
    cmd = [
        os.environ.get("CC", "cc"),
        "-shared",
        "-fPIC",
        "-I",
        str(REPO / "utils" / "fake_libc_include"),
        "-I",
        str(REPO / "pcc" / "py_runtime" / "include"),
        str(source),
        "-o",
        str(out),
    ]
    if sys.platform == "darwin":
        cmd.extend(["-undefined", "dynamic_lookup"])
    subprocess.run(cmd, check=True, text=True, capture_output=True, timeout=30)
    return site


def _compile_demo_extension(tmp_path: Path) -> Path:
    return _compile_extension(
        tmp_path,
        "demo",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "\n"
        "static PyObject *demo_add(PyObject *self, PyObject *args) {\n"
        "    long a = 0;\n"
        "    long b = 0;\n"
        "    (void)self;\n"
        '    if (!PyArg_ParseTuple(args, "ll", &a, &b)) return NULL;\n'
        "    return PyLong_FromLong(a + b);\n"
        "}\n"
        "\n"
        "static PyMethodDef DemoMethods[] = {\n"
        '    {"add", demo_add, METH_VARARGS, "add two ints"},\n'
        "    {NULL, NULL, 0, NULL},\n"
        "};\n"
        "\n"
        "static PyModuleDef demomodule = {\n"
        '    PyModuleDef_HEAD_INIT, "demo", NULL, -1, DemoMethods,\n'
        "};\n"
        "\n"
        "PyMODINIT_FUNC PyInit_demo(void) {\n"
        "    return PyModule_Create(&demomodule);\n"
        "}\n",
    )


def _compile_methodflag_extension(tmp_path: Path) -> Path:
    return _compile_extension(
        tmp_path,
        "flagdemo",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "\n"
        "static PyObject *flag_sum(PyObject *self, PyObject *args) {\n"
        "    long a = 0;\n"
        "    long b = 0;\n"
        "    (void)self;\n"
        '    if (!PyArg_ParseTuple(args, "ll", &a, &b)) return NULL;\n'
        "    return PyLong_FromLong(a + b);\n"
        "}\n"
        "\n"
        "static PyObject *flag_noargs(PyObject *self, PyObject *ignored) {\n"
        "    (void)ignored;\n"
        "    return PyLong_FromLong(self != NULL ? 41 : -41);\n"
        "}\n"
        "\n"
        "static PyObject *flag_one(PyObject *self, PyObject *arg) {\n"
        "    long value = PyLong_AsLong(arg);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(value + (self != NULL ? 100 : -100));\n"
        "}\n"
        "\n"
        "static PyMethodDef FlagMethods[] = {\n"
        '    {"sum", flag_sum, METH_VARARGS, "add two ints"},\n'
        '    {"noargs", flag_noargs, METH_NOARGS, "return a constant"},\n'
        '    {"one", flag_one, METH_O, "add to one int"},\n'
        "    {NULL, NULL, 0, NULL},\n"
        "};\n"
        "\n"
        "static PyModuleDef flagmodule = {\n"
        '    PyModuleDef_HEAD_INIT, "flagdemo", NULL, -1, FlagMethods,\n'
        "};\n"
        "\n"
        "PyMODINIT_FUNC PyInit_flagdemo(void) {\n"
        "    return PyModule_Create(&flagmodule);\n"
        "}\n",
    )


def _compile_fastcall_extension(tmp_path: Path) -> Path:
    return _compile_extension(
        tmp_path,
        "fastdemo",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "\n"
        "static PyObject *fast_sum(PyObject *self, PyObject *const *args, Py_ssize_t nargs) {\n"
        "    long a = 0;\n"
        "    long b = 0;\n"
        "    (void)self;\n"
        "    if (nargs != 2) {\n"
        '        PyErr_SetString(PyExc_TypeError, "fast_sum takes two arguments");\n'
        "        return NULL;\n"
        "    }\n"
        "    a = PyLong_AsLong(args[0]);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    b = PyLong_AsLong(args[1]);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(a + b);\n"
        "}\n"
        "\n"
        "static PyObject *fast_kw_score(PyObject *self, PyObject *const *args,\n"
        "                               Py_ssize_t nargs, PyObject *kwnames) {\n"
        "    long positional = 0;\n"
        "    long keyword = 0;\n"
        "    Py_ssize_t nkwargs = kwnames == NULL ? 0 : PyTuple_Size(kwnames);\n"
        "    (void)self;\n"
        "    if (nargs != 1 || nkwargs != 1) {\n"
        '        PyErr_SetString(PyExc_TypeError, "expected one positional and one keyword");\n'
        "        return NULL;\n"
        "    }\n"
        "    positional = PyLong_AsLong(args[0]);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    keyword = PyLong_AsLong(args[1]);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(positional * 100 + keyword * 10 + nkwargs);\n"
        "}\n"
        "\n"
        "static PyObject *method_name(PyObject *self, PyObject *args) {\n"
        "    PyObject *obj = NULL;\n"
        "    PyCFunctionObject *fn = NULL;\n"
        "    (void)self;\n"
        '    if (!PyArg_ParseTuple(args, "O", &obj)) return NULL;\n'
        "    if (Py_TYPE(obj) != &PyCFunction_Type) {\n"
        '        return PyUnicode_FromString("not-cfunc");\n'
        "    }\n"
        "    fn = (PyCFunctionObject *)obj;\n"
        "    if (fn->m_ml == NULL || fn->m_ml->ml_name == NULL) {\n"
        '        PyErr_SetString(PyExc_RuntimeError, "missing C method definition");\n'
        "        return NULL;\n"
        "    }\n"
        "    return PyUnicode_FromString(fn->m_ml->ml_name);\n"
        "}\n"
        "\n"
        "static PyMethodDef FastMethods[] = {\n"
        '    {"fast_sum", (PyCFunction)fast_sum, METH_FASTCALL, "add two ints"},\n'
        '    {"fast_kw_score", (PyCFunction)fast_kw_score, METH_FASTCALL | METH_KEYWORDS, "score vector layout"},\n'
        '    {"method_name", method_name, METH_VARARGS, "read C function ABI"},\n'
        "    {NULL, NULL, 0, NULL},\n"
        "};\n"
        "\n"
        "static PyModuleDef fastmodule = {\n"
        '    PyModuleDef_HEAD_INIT, "fastdemo", NULL, -1, FastMethods,\n'
        "};\n"
        "\n"
        "PyMODINIT_FUNC PyInit_fastdemo(void) {\n"
        "    return PyModule_Create(&fastmodule);\n"
        "}\n",
    )


def _compile_keywordflag_extension(tmp_path: Path) -> Path:
    return _compile_extension(
        tmp_path,
        "kwdemo",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "\n"
        "static PyObject *kwdemo_mix(PyObject *self, PyObject *args, PyObject *kwargs) {\n"
        "    long value = 0;\n"
        '    const char *name = "";\n'
        "    long name_len = 0;\n"
        '    static char *kwlist[] = {"value", "name", NULL};\n'
        "    (void)self;\n"
        '    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "l|s", kwlist, &value, &name)) return NULL;\n'
        "    while (name[name_len] != '\\0') name_len++;\n"
        "    return PyLong_FromLong(value + name_len);\n"
        "}\n"
        "\n"
        "static int kwdemo_keep_object(PyObject *obj, void *out) {\n"
        "    *(PyObject **)out = obj;\n"
        "    return 1;\n"
        "}\n"
        "\n"
        "static PyObject *kwdemo_options(PyObject *self, PyObject *args, PyObject *kwargs) {\n"
        "    int coerce = 1;\n"
        "    PyObject *converted = NULL;\n"
        '    static char *kwlist[] = {"coerce", "na_object", NULL};\n'
        "    (void)self;\n"
        '    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "|$pO&:options", kwlist,\n'
        "                                     &coerce, kwdemo_keep_object, &converted)) return NULL;\n"
        "    long score = coerce ? 100 : 0;\n"
        "    if (converted != NULL) score += PyLong_AsLong(converted);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *kwdemo_typed_long(PyObject *self, PyObject *args) {\n"
        "    PyObject *value = NULL;\n"
        "    (void)self;\n"
        '    if (!PyArg_ParseTuple(args, "O!:typed_long", &PyLong_Type, &value)) return NULL;\n'
        "    return PyLong_FromLong(PyLong_AsLong(value));\n"
        "}\n"
        "\n"
        "static PyMethodDef KwMethods[] = {\n"
        '    {"mix", (PyCFunction)kwdemo_mix, METH_VARARGS | METH_KEYWORDS, "mix args and kwargs"},\n'
        '    {"options", (PyCFunction)kwdemo_options, METH_VARARGS | METH_KEYWORDS, "parse optional p and O&"},\n'
        '    {"typed_long", kwdemo_typed_long, METH_VARARGS, "parse O!"},\n'
        "    {NULL, NULL, 0, NULL},\n"
        "};\n"
        "\n"
        "static PyModuleDef kwmodule = {\n"
        '    PyModuleDef_HEAD_INIT, "kwdemo", NULL, -1, KwMethods,\n'
        "};\n"
        "\n"
        "PyMODINIT_FUNC PyInit_kwdemo(void) {\n"
        "    return PyModule_Create(&kwmodule);\n"
        "}\n",
    )


def _compile_typedemo_extension(tmp_path: Path) -> Path:
    # A minimal custom-type extension exercising the PyType_Ready bridge: the
    # static PyTypeObject is registered via PyType_Ready, exposed on the module,
    # and instantiated through tp_new=PyType_GenericNew. This is the reduced
    # numpy type-registration pattern; it must import + instantiate under strict
    # no-libpython (--python-libpython=off --backend self).
    return _compile_extension(
        tmp_path,
        "typedemo",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "#include <structmember.h>\n"
        "\n"
        "typedef struct { PyObject_HEAD long val; PyObject *dict; } FooObject;\n"
        "\n"
        "static PyObject *foo_get_base(PyObject *self, void *closure) {\n"
        "    (void)self;\n"
        "    (void)closure;\n"
        "    return PyLong_FromLong(7);\n"
        "}\n"
        "\n"
        "static PyObject *foo_richcompare(PyObject *lhs, PyObject *rhs, int op) {\n"
        "    (void)lhs;\n"
        "    (void)rhs;\n"
        "    return PyBool_FromLong(op == Py_GE);\n"
        "}\n"
        "\n"
        "static PyGetSetDef FooGetSet[] = {\n"
        '    {"base", foo_get_base, NULL, NULL, NULL},\n'
        "    {NULL, NULL, NULL, NULL, NULL},\n"
        "};\n"
        "\n"
        "static PyTypeObject BaseType = {\n"
        "    PyVarObject_HEAD_INIT(NULL, 0)\n"
        '    .tp_name = "typedemo.Base",\n'
        "    .tp_basicsize = sizeof(FooObject),\n"
        "    .tp_flags = Py_TPFLAGS_DEFAULT,\n"
        "    .tp_new = PyType_GenericNew,\n"
        "};\n"
        "\n"
        "static PyMemberDef FooMembers[] = {\n"
        '    {"itemsize", T_LONG, __builtin_offsetof(FooObject, val), READONLY, NULL},\n'
        "    {NULL, 0, 0, 0, NULL},\n"
        "};\n"
        "\n"
        "static PyTypeObject FooType = {\n"
        "    PyVarObject_HEAD_INIT(NULL, 0)\n"
        '    .tp_name = "typedemo.Foo",\n'
        "    .tp_basicsize = sizeof(FooObject),\n"
        "    .tp_flags = Py_TPFLAGS_DEFAULT,\n"
        "    .tp_getattro = PyObject_GenericGetAttr,\n"
        "    .tp_setattro = PyObject_GenericSetAttr,\n"
        "    .tp_members = FooMembers,\n"
        "    .tp_getset = FooGetSet,\n"
        "    .tp_richcompare = foo_richcompare,\n"
        "    .tp_dictoffset = __builtin_offsetof(FooObject, dict),\n"
        "    .tp_base = &BaseType,\n"
        "    .tp_new = PyType_GenericNew,\n"
        "};\n"
        "\n"
        "static PyModuleDef typemodule = {\n"
        '    PyModuleDef_HEAD_INIT, "typedemo", NULL, -1, NULL,\n'
        "};\n"
        "\n"
        "PyMODINIT_FUNC PyInit_typedemo(void) {\n"
        "    if (PyType_Ready(&BaseType) < 0) return NULL;\n"
        "    if (PyType_Ready(&FooType) < 0) return NULL;\n"
        "    PyObject *m = PyModule_Create(&typemodule);\n"
        "    if (m == NULL) return NULL;\n"
        "    Py_INCREF((PyObject *)&BaseType);\n"
        '    PyModule_AddObject(m, "Base", (PyObject *)&BaseType);\n'
        "    Py_INCREF((PyObject *)&FooType);\n"
        '    PyModule_AddObject(m, "Foo", (PyObject *)&FooType);\n'
        "    return m;\n"
        "}\n",
    )


def _compile_typespec_extension(tmp_path: Path) -> Path:
    return _compile_extension(
        tmp_path,
        "specdemo",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "\n"
        "typedef struct { PyObject_HEAD long value; } SpecObject;\n"
        "\n"
        "static PyObject *spec_marker(PyObject *self, PyObject *arg) {\n"
        "    if (self == NULL || arg == NULL) return NULL;\n"
        "    Py_INCREF(arg);\n"
        "    return arg;\n"
        "}\n"
        "\n"
        "static PyMethodDef SpecTypeMethods[] = {\n"
        '    {"marker", spec_marker, METH_O, "return the marker argument"},\n'
        "    {NULL, NULL, 0, NULL},\n"
        "};\n"
        "\n"
        "static PyType_Slot SpecSlots[] = {\n"
        "    {Py_tp_new, PyType_GenericNew},\n"
        '    {Py_tp_doc, "spec-created type"},\n'
        "    {Py_tp_methods, SpecTypeMethods},\n"
        "    {0, NULL},\n"
        "};\n"
        "\n"
        "static PyType_Spec Spec = {\n"
        '    "specdemo.Spec",\n'
        "    sizeof(SpecObject),\n"
        "    0,\n"
        "    Py_TPFLAGS_DEFAULT,\n"
        "    SpecSlots,\n"
        "};\n"
        "\n"
        "static PyTypeObject MetaType = {\n"
        "    PyVarObject_HEAD_INIT(NULL, 0)\n"
        '    .tp_name = "specdemo.Meta",\n'
        "    .tp_basicsize = sizeof(PyTypeObject),\n"
        "    .tp_flags = Py_TPFLAGS_DEFAULT,\n"
        "    .tp_new = PyType_GenericNew,\n"
        "};\n"
        "\n"
        "static PyTypeObject MetaInstanceType = {\n"
        "    PyVarObject_HEAD_INIT(NULL, 0)\n"
        '    .tp_name = "specdemo.MetaInstance",\n'
        "    .tp_basicsize = sizeof(PyObject),\n"
        "    .tp_flags = Py_TPFLAGS_DEFAULT,\n"
        "    .tp_new = PyType_GenericNew,\n"
        "};\n"
        "\n"
        "static PyObject *specdemo_check(PyObject *self, PyObject *args) {\n"
        "    long score = 0;\n"
        "    (void)self; (void)args;\n"
        "    PyObject *type = PyType_FromSpec(&Spec);\n"
        "    if (type == NULL) return NULL;\n"
        "    PyObject *obj = PyType_GenericNew((PyTypeObject *)type, NULL, NULL);\n"
        "    if (type != NULL) score += 1;\n"
        "    if (Py_TYPE(type) == &PyType_Type) score += 2;\n"
        "    if (obj != NULL) score += 4;\n"
        "    if (obj != NULL && Py_TYPE(obj) == (PyTypeObject *)type) score += 8;\n"
        "    if (obj != NULL && PyObject_TypeCheck(obj, (PyTypeObject *)type)) score += 16;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *specdemo_slots(PyObject *self, PyObject *args) {\n"
        "    long score = 0;\n"
        "    (void)self; (void)args;\n"
        "    PyObject *type = PyType_FromSpec(&Spec);\n"
        "    if (type == NULL) return NULL;\n"
        "    if (PyType_GetSlot((PyTypeObject *)type, Py_tp_new) == PyType_GenericNew) score += 1;\n"
        "    const char *doc = (const char *)PyType_GetSlot((PyTypeObject *)type, Py_tp_doc);\n"
        "    if (doc != NULL && doc[0] == 's') score += 2;\n"
        "    if (PyType_GetSlot((PyTypeObject *)type, 9999) == NULL) score += 4;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *specdemo_metatype_check(PyObject *self, PyObject *args) {\n"
        "    long score = 0;\n"
        "    (void)self; (void)args;\n"
        "    if (Py_TYPE(&MetaInstanceType) == &MetaType) score += 1;\n"
        "    if (PyObject_TypeCheck((PyObject *)&MetaInstanceType, &MetaType)) score += 2;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyMethodDef SpecMethods[] = {\n"
        '    {"check", specdemo_check, METH_VARARGS, "check PyType_FromSpec"},\n'
        '    {"slots", specdemo_slots, METH_VARARGS, "check PyType_GetSlot"},\n'
        '    {"metatype_check", specdemo_metatype_check, METH_VARARGS, "check custom metaclass"},\n'
        "    {NULL, NULL, 0, NULL},\n"
        "};\n"
        "\n"
        "static PyModuleDef specmodule = {\n"
        '    PyModuleDef_HEAD_INIT, "specdemo", NULL, -1, SpecMethods,\n'
        "};\n"
        "\n"
        "PyMODINIT_FUNC PyInit_specdemo(void) {\n"
        "    MetaType.tp_base = &PyType_Type;\n"
        "    if (PyType_Ready(&MetaType) < 0) return NULL;\n"
        "    Py_SET_TYPE(&MetaInstanceType, &MetaType);\n"
        "    if (PyType_Ready(&MetaInstanceType) < 0) return NULL;\n"
        "    PyObject *m = PyModule_Create(&specmodule);\n"
        "    if (m == NULL) return NULL;\n"
        "    PyObject *type = PyType_FromSpec(&Spec);\n"
        "    if (type == NULL) return NULL;\n"
        '    if (PyModule_AddObject(m, "Spec", type) < 0) return NULL;\n'
        "    return m;\n"
        "}\n",
    )


def _compile_typecheck_extension(tmp_path: Path) -> Path:
    # Exercises the builtin-tag Py_TYPE mapping: Py_TYPE of pcc builtin objects
    # must resolve to the matching &PyXxx_Type token the shim provides (numpy
    # does this constantly). Returns a bitset so a partial mapping is visible.
    return _compile_extension(
        tmp_path,
        "tcdemo",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "static PyObject *tc_check(PyObject *self, PyObject *args) {\n"
        "    (void)self; (void)args;\n"
        "    PyObject *i = PyLong_FromLong(5);\n"
        '    PyObject *s = PyUnicode_FromString("x");\n'
        "    long ok = (Py_TYPE(i) == &PyLong_Type) ? 1 : 0;\n"
        "    ok += (Py_TYPE(s) == &PyUnicode_Type) ? 2 : 0;\n"
        "    return PyLong_FromLong(ok);\n"
        "}\n"
        "static PyMethodDef M[] = {\n"
        '    {"check", tc_check, METH_VARARGS, ""}, {NULL, NULL, 0, NULL}\n'
        "};\n"
        "static PyModuleDef mod = {\n"
        '    PyModuleDef_HEAD_INIT, "tcdemo", NULL, -1, M,\n'
        "};\n"
        "PyMODINIT_FUNC PyInit_tcdemo(void) { return PyModule_Create(&mod); }\n",
    )


def _compile_subtype_extension(tmp_path: Path) -> Path:
    # Exercises PyObject_TypeCheck across a tp_base inheritance chain (numpy's
    # scalar/array type hierarchy relies on subtype checks). check() returns 7
    # when: derived is-a base, derived is-a derived, base is-NOT-a derived.
    return _compile_extension(
        tmp_path,
        "stdemo",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "typedef struct { PyObject_HEAD long v; } BaseObj;\n"
        "typedef struct { BaseObj base; long w; } DerivedObj;\n"
        "static PyTypeObject BaseType = {\n"
        "    PyVarObject_HEAD_INIT(NULL, 0)\n"
        '    .tp_name = "stdemo.Base", .tp_basicsize = sizeof(BaseObj),\n'
        "    .tp_flags = Py_TPFLAGS_DEFAULT, .tp_new = PyType_GenericNew,\n"
        "};\n"
        "static PyTypeObject DerivedType = {\n"
        "    PyVarObject_HEAD_INIT(NULL, 0)\n"
        '    .tp_name = "stdemo.Derived", .tp_basicsize = sizeof(DerivedObj),\n'
        "    .tp_flags = Py_TPFLAGS_DEFAULT, .tp_base = &BaseType,\n"
        "    .tp_new = PyType_GenericNew,\n"
        "};\n"
        "static PyObject *st_check(PyObject *self, PyObject *args) {\n"
        "    (void)self; (void)args;\n"
        "    PyObject *d = PyType_GenericNew(&DerivedType, NULL, NULL);\n"
        "    PyObject *b = PyType_GenericNew(&BaseType, NULL, NULL);\n"
        "    long r = 0;\n"
        "    if (PyObject_TypeCheck(d, &BaseType)) r += 1;\n"
        "    if (PyObject_TypeCheck(d, &DerivedType)) r += 2;\n"
        "    if (!PyObject_TypeCheck(b, &DerivedType)) r += 4;\n"
        "    if (PyType_IsSubtype(&DerivedType, &BaseType)) r += 8;\n"
        "    if (!PyType_IsSubtype(&BaseType, &DerivedType)) r += 16;\n"
        "    BaseObj *n = PyObject_New(BaseObj, &BaseType);\n"
        "    if (n != NULL) r += 32;\n"
        "    return PyLong_FromLong(r);\n"
        "}\n"
        "static PyObject *st_alloc_slots(PyObject *self, PyObject *args) {\n"
        "    (void)self; (void)args;\n"
        "    long r = 0;\n"
        "    if (BaseType.tp_alloc != NULL) r += 1;\n"
        "    if (DerivedType.tp_alloc != NULL) r += 2;\n"
        "    if (DerivedType.tp_alloc == BaseType.tp_alloc) r += 4;\n"
        "    return PyLong_FromLong(r);\n"
        "}\n"
        "static PyMethodDef M[] = {\n"
        '    {"check", st_check, METH_VARARGS, ""},\n'
        '    {"alloc_slots", st_alloc_slots, METH_VARARGS, ""},\n'
        "    {NULL, NULL, 0, NULL}\n"
        "};\n"
        "static PyModuleDef mod = {\n"
        '    PyModuleDef_HEAD_INIT, "stdemo", NULL, -1, M,\n'
        "};\n"
        "PyMODINIT_FUNC PyInit_stdemo(void) {\n"
        "    if (PyType_Ready(&BaseType) < 0) return NULL;\n"
        "    if (PyType_Ready(&DerivedType) < 0) return NULL;\n"
        "    return PyModule_Create(&mod);\n"
        "}\n",
    )


def _compile_number_slot_extension(tmp_path: Path) -> Path:
    return _compile_extension(
        tmp_path,
        "numslotdemo",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "typedef struct { PyObject_HEAD long value; } NumObject;\n"
        "static PyObject *num_int(PyObject *self) {\n"
        "    return PyLong_FromLong(((NumObject *)self)->value);\n"
        "}\n"
        "static PyObject *num_index(PyObject *self) {\n"
        "    return PyLong_FromLong(((NumObject *)self)->value);\n"
        "}\n"
        "static PyNumberMethods IntMethods = { .nb_int = num_int };\n"
        "static PyNumberMethods IndexMethods = { .nb_index = num_index };\n"
        "static PyTypeObject IntType = {\n"
        "    PyVarObject_HEAD_INIT(NULL, 0)\n"
        '    .tp_name = "numslotdemo.IntLike",\n'
        "    .tp_basicsize = sizeof(NumObject),\n"
        "    .tp_flags = Py_TPFLAGS_DEFAULT,\n"
        "    .tp_as_number = &IntMethods,\n"
        "    .tp_new = PyType_GenericNew,\n"
        "};\n"
        "static PyTypeObject IndexType = {\n"
        "    PyVarObject_HEAD_INIT(NULL, 0)\n"
        '    .tp_name = "numslotdemo.IndexLike",\n'
        "    .tp_basicsize = sizeof(NumObject),\n"
        "    .tp_flags = Py_TPFLAGS_DEFAULT,\n"
        "    .tp_as_number = &IndexMethods,\n"
        "    .tp_new = PyType_GenericNew,\n"
        "};\n"
        "static PyObject *check(PyObject *self, PyObject *args) {\n"
        "    (void)self; (void)args;\n"
        "    NumObject *int_obj = (NumObject *)PyType_GenericNew(&IntType, NULL, NULL);\n"
        "    NumObject *index_obj = (NumObject *)PyType_GenericNew(&IndexType, NULL, NULL);\n"
        "    if (int_obj == NULL || index_obj == NULL) return NULL;\n"
        "    int_obj->value = 40;\n"
        "    index_obj->value = 41;\n"
        "    PyObject *from_int = PyNumber_Long((PyObject *)int_obj);\n"
        "    if (from_int == NULL) return NULL;\n"
        "    PyObject *from_index_long = PyNumber_Long((PyObject *)index_obj);\n"
        "    if (from_index_long == NULL) return NULL;\n"
        "    PyObject *from_index = PyNumber_Index((PyObject *)index_obj);\n"
        "    if (from_index == NULL) return NULL;\n"
        "    long score = 0;\n"
        "    if (PyLong_AsLong(from_int) == 40) score += 1;\n"
        "    if (PyLong_AsLong(from_index_long) == 41) score += 2;\n"
        "    if (PyLong_AsLong(from_index) == 41) score += 4;\n"
        "    if (PyIndex_Check((PyObject *)index_obj)) score += 8;\n"
        "    if (PyNumber_Check((PyObject *)int_obj)) score += 16;\n"
        "    if (PyNumber_Check((PyObject *)index_obj)) score += 32;\n"
        "    Py_DECREF(from_index);\n"
        "    Py_DECREF(from_index_long);\n"
        "    Py_DECREF(from_int);\n"
        "    Py_DECREF(index_obj);\n"
        "    Py_DECREF(int_obj);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "static PyMethodDef M[] = {\n"
        '    {"check", check, METH_VARARGS, ""}, {NULL, NULL, 0, NULL}\n'
        "};\n"
        "static PyModuleDef mod = {\n"
        '    PyModuleDef_HEAD_INIT, "numslotdemo", NULL, -1, M,\n'
        "};\n"
        "PyMODINIT_FUNC PyInit_numslotdemo(void) {\n"
        "    if (PyType_Ready(&IntType) < 0) return NULL;\n"
        "    if (PyType_Ready(&IndexType) < 0) return NULL;\n"
        "    return PyModule_Create(&mod);\n"
        "}\n",
    )


def _compile_managed_dealloc_extension(tmp_path: Path) -> Path:
    return _compile_extension(
        tmp_path,
        "mdemo",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "typedef struct { PyObject_HEAD long value; } DemoObj;\n"
        "static long dealloc_hits = 0;\n"
        "static void managed_dealloc(PyObject *self) { (void)self; dealloc_hits += 1; }\n"
        "static void plain_dealloc(PyObject *self) { (void)self; dealloc_hits += 100; }\n"
        "static PyTypeObject ManagedType = {\n"
        "    PyVarObject_HEAD_INIT(NULL, 0)\n"
        '    .tp_name = "mdemo.Managed", .tp_basicsize = sizeof(DemoObj),\n'
        "    .tp_flags = Py_TPFLAGS_DEFAULT | PCC_TPFLAGS_MANAGED_DEALLOC,\n"
        "    .tp_dealloc = managed_dealloc, .tp_new = PyType_GenericNew,\n"
        "};\n"
        "static PyTypeObject PlainType = {\n"
        "    PyVarObject_HEAD_INIT(NULL, 0)\n"
        '    .tp_name = "mdemo.Plain", .tp_basicsize = sizeof(DemoObj),\n'
        "    .tp_flags = Py_TPFLAGS_DEFAULT,\n"
        "    .tp_dealloc = plain_dealloc, .tp_new = PyType_GenericNew,\n"
        "};\n"
        "static PyObject *md_check(PyObject *self, PyObject *args) {\n"
        "    (void)self; (void)args;\n"
        "    dealloc_hits = 0;\n"
        "    PyObject *managed = PyType_GenericNew(&ManagedType, NULL, NULL);\n"
        "    PyObject *plain = PyType_GenericNew(&PlainType, NULL, NULL);\n"
        "    if (managed == NULL || plain == NULL) {\n"
        "        Py_XDECREF(managed); Py_XDECREF(plain); return NULL;\n"
        "    }\n"
        "    Py_DECREF(managed);\n"
        "    Py_DECREF(plain);\n"
        "    return PyLong_FromLong(dealloc_hits);\n"
        "}\n"
        "static PyMethodDef M[] = {\n"
        '    {"check", md_check, METH_VARARGS, ""}, {NULL, NULL, 0, NULL}\n'
        "};\n"
        "static PyModuleDef mod = {\n"
        '    PyModuleDef_HEAD_INIT, "mdemo", NULL, -1, M,\n'
        "};\n"
        "PyMODINIT_FUNC PyInit_mdemo(void) {\n"
        "    if (PyType_Ready(&ManagedType) < 0) return NULL;\n"
        "    if (PyType_Ready(&PlainType) < 0) return NULL;\n"
        "    return PyModule_Create(&mod);\n"
        "}\n",
    )


def _compile_linksym_extension(tmp_path: Path) -> Path:
    # Behavioral regression for the batch 7-9 link symbols added to
    # py_capi_shim.c for numpy's no-libpython link surface. These were
    # link-validated (the shim compiles + ext-abi gate) but not exercised at
    # runtime; this locks their BEHAVIOR under strict no-libpython. check()
    # returns a bitset; 31 = all five correct:
    #   1  PyTuple_GetSlice((10,20,30,40)[1:3]) -> (20,30)
    #   2  PyObject_AsFileDescriptor(PyLong(7)) -> 7
    #   4  Py_GenericAlias(origin=PyLong(99), NULL) -> origin (value 99 preserved)
    #   8  PyUnicode_AsLatin1String("Ab") -> b"Ab"
    #   16 PyErr_NormalizeException(NULL triple) -> no-op, error state stays clear
    return _compile_extension(
        tmp_path,
        "lsdemo",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "static PyObject *ls_check(PyObject *self, PyObject *args) {\n"
        "    (void)self; (void)args;\n"
        "    long r = 0;\n"
        "    PyObject *t = PyTuple_New(4);\n"
        "    PyTuple_SetItem(t, 0, PyLong_FromLong(10));\n"
        "    PyTuple_SetItem(t, 1, PyLong_FromLong(20));\n"
        "    PyTuple_SetItem(t, 2, PyLong_FromLong(30));\n"
        "    PyTuple_SetItem(t, 3, PyLong_FromLong(40));\n"
        "    PyObject *sl = PyTuple_GetSlice(t, 1, 3);\n"
        "    if (sl != NULL && PyTuple_Size(sl) == 2 &&\n"
        "        PyLong_AsLong(PyTuple_GetItem(sl, 0)) == 20 &&\n"
        "        PyLong_AsLong(PyTuple_GetItem(sl, 1)) == 30) r += 1;\n"
        "    PyObject *fd = PyLong_FromLong(7);\n"
        "    if (PyObject_AsFileDescriptor(fd) == 7) r += 2;\n"
        "    PyObject *origin = PyLong_FromLong(99);\n"
        "    PyObject *ga = Py_GenericAlias(origin, Py_None);\n"
        "    if (ga != NULL && PyLong_AsLong(ga) == 99) r += 4;\n"
        '    PyObject *u = PyUnicode_FromString("Ab");\n'
        "    PyObject *b = PyUnicode_AsLatin1String(u);\n"
        "    if (b != NULL && PyBytes_Size(b) == 2) {\n"
        "        const char *bs = PyBytes_AsString(b);\n"
        "        if (bs != NULL && bs[0] == 'A' && bs[1] == 'b') r += 8;\n"
        "    }\n"
        "    PyObject *e = NULL, *v = NULL, *tb = NULL;\n"
        "    PyErr_NormalizeException(&e, &v, &tb);\n"
        "    if (!PyErr_Occurred()) r += 16;\n"
        "    return PyLong_FromLong(r);\n"
        "}\n"
        "static PyMethodDef M[] = {\n"
        '    {"check", ls_check, METH_VARARGS, ""}, {NULL, NULL, 0, NULL}\n'
        "};\n"
        "static PyModuleDef mod = {\n"
        '    PyModuleDef_HEAD_INIT, "lsdemo", NULL, -1, M,\n'
        "};\n"
        "PyMODINIT_FUNC PyInit_lsdemo(void) { return PyModule_Create(&mod); }\n",
    )


def _compile_contextvar_extension(tmp_path: Path) -> Path:
    # Behavioral regression for the batch 10 single-context contextvar
    # (PyContextVar_New + PyContextVar_Get) added to py_capi_shim.c. numpy.errstate
    # creates a ContextVar AT import, so this is a real (not stub) object; check()
    # verifies correct CPython single-context Get semantics:
    #   1  Get(cv, NULL, &v) on an unset var -> the var's own default (5)
    #   2  Get(cv, PyLong(9), &v) on an unset var -> the explicit default arg (9)
    #   4  after Set(cv, 7) (batch 14): Get(cv, NULL, &v) -> the set value (7),
    #      and Set returns a non-NULL token (numpy set-and-discards it).
    # 7 = all correct.
    return _compile_extension(
        tmp_path,
        "cvdemo",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "static PyObject *cv_check(PyObject *self, PyObject *args) {\n"
        "    (void)self; (void)args;\n"
        "    long r = 0;\n"
        '    PyObject *cv = PyContextVar_New("x", PyLong_FromLong(5));\n'
        "    if (cv == NULL) return PyLong_FromLong(-1);\n"
        "    PyObject *v = NULL;\n"
        "    if (PyContextVar_Get(cv, NULL, &v) == 0 && v != NULL &&\n"
        "        PyLong_AsLong(v) == 5) r += 1;\n"
        "    PyObject *v2 = NULL;\n"
        "    if (PyContextVar_Get(cv, PyLong_FromLong(9), &v2) == 0 && v2 != NULL &&\n"
        "        PyLong_AsLong(v2) == 9) r += 2;\n"
        "    PyObject *tok = PyContextVar_Set(cv, PyLong_FromLong(7));\n"
        "    PyObject *v3 = NULL;\n"
        "    if (tok != NULL && PyContextVar_Get(cv, NULL, &v3) == 0 && v3 != NULL &&\n"
        "        PyLong_AsLong(v3) == 7) r += 4;\n"
        "    if (tok != NULL) Py_DECREF(tok);\n"
        "    return PyLong_FromLong(r);\n"
        "}\n"
        "static PyObject *cv_make(PyObject *self, PyObject *unused) {\n"
        "    (void)self; (void)unused;\n"
        '    return PyContextVar_New("python-visible", PyLong_FromLong(5));\n'
        "}\n"
        "static PyMethodDef M[] = {\n"
        '    {"check", cv_check, METH_VARARGS, ""},\n'
        '    {"make", cv_make, METH_NOARGS, ""},\n'
        "    {NULL, NULL, 0, NULL}\n"
        "};\n"
        "static PyModuleDef mod = {\n"
        '    PyModuleDef_HEAD_INIT, "cvdemo", NULL, -1, M,\n'
        "};\n"
        "PyMODINIT_FUNC PyInit_cvdemo(void) { return PyModule_Create(&mod); }\n",
    )


def _compile_buildvalue_dict_extension(tmp_path: Path) -> Path:
    return _compile_extension(
        tmp_path,
        "buildvaluedict",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "static PyObject *check(PyObject *self, PyObject *args) {\n"
        "    (void)self; (void)args;\n"
        '    PyObject *key = PyUnicode_FromString("__signature__");\n'
        "    PyObject *value = PyLong_FromLong(42);\n"
        '    PyObject *dict = Py_BuildValue("{ON}", key, value);\n'
        "    Py_DECREF(key);\n"
        "    if (dict == NULL) return NULL;\n"
        '    PyObject *got = PyDict_GetItemString(dict, "__signature__");\n'
        "    long result = got == NULL ? -1 : PyLong_AsLong(got);\n"
        "    Py_DECREF(dict);\n"
        "    return PyLong_FromLong(result);\n"
        "}\n"
        "static PyObject *nested(PyObject *self, PyObject *args) {\n"
        "    (void)self; (void)args;\n"
        '    PyObject *built = Py_BuildValue("{s, [(i), (i, i)]}",\n'
        '                                    "axes", -1, -2, -1);\n'
        "    if (built == NULL) return NULL;\n"
        '    PyObject *axes = PyDict_GetItemString(built, "axes");\n'
        "    if (axes == NULL) return NULL;\n"
        "    PyObject *first = PyList_GetItem(axes, 0);\n"
        "    PyObject *second = PyList_GetItem(axes, 1);\n"
        "    long score = 0;\n"
        "    if (PyList_Check(axes)) score += 1;\n"
        "    if (PyList_Size(axes) == 2) score += 2;\n"
        "    if (first != NULL && PyTuple_Check(first)) score += 4;\n"
        "    if (first != NULL && PyLong_AsLong(PyTuple_GetItem(first, 0)) == -1) score += 8;\n"
        "    if (second != NULL && PyTuple_Size(second) == 2) score += 16;\n"
        "    if (second != NULL && PyLong_AsLong(PyTuple_GetItem(second, 0)) == -2) score += 32;\n"
        "    if (second != NULL && PyLong_AsLong(PyTuple_GetItem(second, 1)) == -1) score += 64;\n"
        "    Py_DECREF(built);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "static PyMethodDef M[] = {\n"
        '    {"check", check, METH_VARARGS, ""},\n'
        '    {"nested", nested, METH_VARARGS, ""},\n'
        "    {NULL, NULL, 0, NULL}\n"
        "};\n"
        "static PyModuleDef mod = {\n"
        '    PyModuleDef_HEAD_INIT, "buildvaluedict", NULL, -1, M,\n'
        "};\n"
        "PyMODINIT_FUNC PyInit_buildvaluedict(void) {\n"
        "    return PyModule_Create(&mod);\n"
        "}\n",
    )


def _compile_vaparse_extension(tmp_path: Path) -> Path:
    # Behavioral regression for the PyArg_ParseTupleAndKeywords -> (new)
    # PyArg_VaParseTupleAndKeywords va_list-core delegation (the canonical CPython
    # refactor; PyArg_VaParseTupleAndKeywords is the symbol numpy's C core
    # references). The extension does NOT call va_start itself (the curated
    # fake-libc dir's stdarg shim is build-only); instead it calls the `...`
    # wrapper, which now routes through the va_list core. kwargs=NULL because the
    # no-libpython loader dispatches METH_VARARGS (positional). check(10, 20)
    # parses "ll" via the core and returns a*100+b = 1020.
    return _compile_extension(
        tmp_path,
        "vademo",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "static PyObject *va_check(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    long a = 0, b = 0;\n"
        '    char *kwlist[] = {"a", "b", NULL};\n'
        '    if (!PyArg_ParseTupleAndKeywords(args, NULL, "ll", kwlist, &a, &b))\n'
        "        return PyLong_FromLong(-1);\n"
        "    return PyLong_FromLong(a * 100 + b);\n"
        "}\n"
        "static PyMethodDef M[] = {\n"
        '    {"check", va_check, METH_VARARGS, ""}, {NULL, NULL, 0, NULL}\n'
        "};\n"
        "static PyModuleDef mod = {\n"
        '    PyModuleDef_HEAD_INIT, "vademo", NULL, -1, M,\n'
        "};\n"
        "PyMODINIT_FUNC PyInit_vademo(void) { return PyModule_Create(&mod); }\n",
    )


def _compile_getbuiltins_extension(tmp_path: Path) -> Path:
    # Behavioral regression for PyEval_GetBuiltins (numpy's sole consumer is
    # npy_PyFile_OpenFile in npy_3kcompat.h: PyDict_GetItemString(
    # PyEval_GetBuiltins(), "open"), which returns NULL gracefully if absent).
    # check() returns 7:
    #   1  PyEval_GetBuiltins() is non-NULL (a real dict, not NULL/fake)
    #   2  numpy's pattern: an absent key ("open") -> NULL, no crash (import-safe)
    #   4  it is a real mutable dict AND a persistent singleton (a second call
    #      returns the same pointer; set/get roundtrips) — i.e. borrowed-ref
    #      semantics, not a throwaway.
    return _compile_extension(
        tmp_path,
        "ebdemo",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "static PyObject *eb_check(PyObject *self, PyObject *args) {\n"
        "    (void)self; (void)args;\n"
        "    long r = 0;\n"
        "    PyObject *b = PyEval_GetBuiltins();\n"
        "    if (b != NULL) r += 1;\n"
        '    if (PyDict_GetItemString(b, "open") == NULL) r += 2;\n'
        '    PyDict_SetItemString(b, "answer", PyLong_FromLong(42));\n'
        "    PyObject *b2 = PyEval_GetBuiltins();\n"
        '    PyObject *v = PyDict_GetItemString(b2, "answer");\n'
        "    if (b2 == b && v != NULL && PyLong_AsLong(v) == 42) r += 4;\n"
        "    return PyLong_FromLong(r);\n"
        "}\n"
        "static PyMethodDef M[] = {\n"
        '    {"check", eb_check, METH_VARARGS, ""}, {NULL, NULL, 0, NULL}\n'
        "};\n"
        "static PyModuleDef mod = {\n"
        '    PyModuleDef_HEAD_INIT, "ebdemo", NULL, -1, M,\n'
        "};\n"
        "PyMODINIT_FUNC PyInit_ebdemo(void) { return PyModule_Create(&mod); }\n",
    )


def _compile_uniqueref_extension(tmp_path: Path) -> Path:
    # Behavioral regression for PyUnstable_Object_IsUniquelyReferenced (referenced
    # by the full numpy _core for safe in-place mutation). check() returns 3:
    #   1  a freshly-created object (refcount 1) is uniquely referenced
    #   2  after Py_INCREF (refcount 2) it is NOT uniquely referenced
    return _compile_extension(
        tmp_path,
        "urdemo",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "static PyObject *ur_check(PyObject *self, PyObject *args) {\n"
        "    (void)self; (void)args;\n"
        "    long r = 0;\n"
        "    PyObject *o = PyTuple_New(1);\n"
        "    PyTuple_SetItem(o, 0, PyLong_FromLong(0));\n"
        "    if (PyUnstable_Object_IsUniquelyReferenced(o)) r += 1;\n"
        "    Py_INCREF(o);\n"
        "    if (!PyUnstable_Object_IsUniquelyReferenced(o)) r += 2;\n"
        "    Py_DECREF(o);\n"
        "    return PyLong_FromLong(r);\n"
        "}\n"
        "static PyMethodDef M[] = {\n"
        '    {"check", ur_check, METH_VARARGS, ""}, {NULL, NULL, 0, NULL}\n'
        "};\n"
        "static PyModuleDef mod = {\n"
        '    PyModuleDef_HEAD_INIT, "urdemo", NULL, -1, M,\n'
        "};\n"
        "PyMODINIT_FUNC PyInit_urdemo(void) { return PyModule_Create(&mod); }\n",
    )


def _compile_import_vectorcall_extension(tmp_path: Path) -> Path:
    # Behavioral regression for PyImport_Import (-> PyImport_ImportModule) and
    # PyVectorcall_Call (-> PyObject_Call), both batch 14. The caller imports the
    # sibling `demo` extension by name and calls demo.add(2, 3) via the vectorcall
    # entry. Compiled into the SAME site as demo (so it is importable). check()
    # returns 7:
    #   1  PyImport_Import("demo") is non-NULL
    #   2  demo.add is reachable
    #   4  PyVectorcall_Call(add, (2,3), NULL) == 5
    return _compile_extension(
        tmp_path,
        "imdemo",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "static PyObject *im_check(PyObject *self, PyObject *args) {\n"
        "    (void)self; (void)args;\n"
        "    long r = 0;\n"
        '    PyObject *m = PyImport_Import(PyUnicode_FromString("demo"));\n'
        "    if (m == NULL) return PyLong_FromLong(r);\n"
        "    r += 1;\n"
        '    PyObject *add = PyObject_GetAttrString(m, "add");\n'
        "    if (add == NULL) return PyLong_FromLong(r);\n"
        "    r += 2;\n"
        "    PyObject *t = PyTuple_New(2);\n"
        "    PyTuple_SetItem(t, 0, PyLong_FromLong(2));\n"
        "    PyTuple_SetItem(t, 1, PyLong_FromLong(3));\n"
        "    PyObject *res = PyVectorcall_Call(add, t, NULL);\n"
        "    if (res != NULL && PyLong_AsLong(res) == 5) r += 4;\n"
        "    return PyLong_FromLong(r);\n"
        "}\n"
        "static PyMethodDef M[] = {\n"
        '    {"check", im_check, METH_VARARGS, ""}, {NULL, NULL, 0, NULL}\n'
        "};\n"
        "static PyModuleDef mod = {\n"
        '    PyModuleDef_HEAD_INIT, "imdemo", NULL, -1, M,\n'
        "};\n"
        "PyMODINIT_FUNC PyInit_imdemo(void) { return PyModule_Create(&mod); }\n",
    )


def _compile_type_vectorcall_extension(tmp_path: Path) -> Path:
    # Reduced numpy ufunc call layout: tp_call points at PyVectorcall_Call and
    # tp_vectorcall_offset points at a per-instance vectorcallfunc slot.  The
    # shim must dispatch that slot directly instead of recursing through
    # PyObject_Call -> tp_call -> PyVectorcall_Call.
    return _compile_extension(
        tmp_path,
        "typevcdemo",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "#include <stddef.h>\n"
        "\n"
        "typedef struct {\n"
        "    PyObject_HEAD\n"
        "    vectorcallfunc vectorcall;\n"
        "} VectorObject;\n"
        "\n"
        "static PyObject *vector_impl(PyObject *self, PyObject *const *args,\n"
        "                             size_t nargsf, PyObject *kwnames) {\n"
        "    Py_ssize_t nargs = (Py_ssize_t)PyVectorcall_NARGS(nargsf);\n"
        "    Py_ssize_t nkwargs = kwnames == NULL ? 0 : PyTuple_Size(kwnames);\n"
        "    long offset_bonus = (nargsf & PY_VECTORCALL_ARGUMENTS_OFFSET) ? 1000 : 0;\n"
        "    long first = 0;\n"
        "    long second = 0;\n"
        "    (void)self;\n"
        "    if (nargs < 1 || args == NULL || (nargs + nkwargs) != 2) {\n"
        '        PyErr_SetString(PyExc_TypeError, "expected two values");\n'
        "        return NULL;\n"
        "    }\n"
        "    first = PyLong_AsLong(args[0]);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    second = PyLong_AsLong(args[1]);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    if (nkwargs == 0) return PyLong_FromLong(first * 10 + second + offset_bonus);\n"
        "    return PyLong_FromLong(first * 100 + second * 10 + nkwargs + offset_bonus);\n"
        "}\n"
        "\n"
        "static PyObject *vector_new(PyTypeObject *type, PyObject *args,\n"
        "                            PyObject *kwargs) {\n"
        "    VectorObject *self = (VectorObject *)PyType_GenericNew(type, args, kwargs);\n"
        "    if (self != NULL) self->vectorcall = vector_impl;\n"
        "    return (PyObject *)self;\n"
        "}\n"
        "\n"
        "static PyObject *vector_iter(PyObject *self) {\n"
        "    PyObject *items = PyTuple_New(2);\n"
        "    PyObject *iter = NULL;\n"
        "    (void)self;\n"
        "    if (items == NULL) return NULL;\n"
        "    PyTuple_SetItem(items, 0, PyLong_FromLong(5));\n"
        "    PyTuple_SetItem(items, 1, PyLong_FromLong(6));\n"
        "    iter = PySeqIter_New(items);\n"
        "    Py_DECREF(items);\n"
        "    return iter;\n"
        "}\n"
        "\n"
        "static PyObject *vector_subscript(PyObject *self, PyObject *key) {\n"
        "    PyObject *first = NULL;\n"
        "    PyObject *second = NULL;\n"
        "    Py_ssize_t start = 0, stop = 0, step = 0, length = 0;\n"
        "    (void)self;\n"
        "    if (!PyTuple_Check(key) || PyTuple_Size(key) != 3) {\n"
        '        PyErr_SetString(PyExc_TypeError, "expected a 3-item index");\n'
        "        return NULL;\n"
        "    }\n"
        "    first = PyTuple_GetItem(key, 0);\n"
        "    second = PyTuple_GetItem(key, 1);\n"
        "    if (!PySlice_Check(first) || !PySlice_Check(second)\n"
        "        || PyTuple_GetItem(key, 2) != Py_None) {\n"
        '        PyErr_SetString(PyExc_TypeError, "expected slice, slice, None");\n'
        "        return NULL;\n"
        "    }\n"
        "    if (PySlice_GetIndicesEx(first, 10, &start, &stop, &step, &length) < 0\n"
        "        || start != 0 || stop != 10 || step != 1 || length != 10) {\n"
        "        return NULL;\n"
        "    }\n"
        "    if (PySlice_GetIndicesEx(second, 10, &start, &stop, &step, &length) < 0\n"
        "        || start != 1 || stop != 5 || step != 2 || length != 2) {\n"
        "        return NULL;\n"
        "    }\n"
        "    return PyLong_FromLong(73);\n"
        "}\n"
        "\n"
        "static PyMappingMethods VectorMapping = {\n"
        "    .mp_subscript = vector_subscript,\n"
        "};\n"
        "\n"
        "static PyTypeObject VectorType = {\n"
        "    PyVarObject_HEAD_INIT(NULL, 0)\n"
        '    .tp_name = "typevcdemo.Vector",\n'
        "    .tp_basicsize = sizeof(VectorObject),\n"
        "    .tp_vectorcall_offset = __builtin_offsetof(VectorObject, vectorcall),\n"
        "    .tp_call = PyVectorcall_Call,\n"
        "    .tp_iter = vector_iter,\n"
        "    .tp_as_mapping = &VectorMapping,\n"
        "    .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_VECTORCALL,\n"
        "    .tp_new = vector_new,\n"
        "};\n"
        "\n"
        "static PyObject *make_vector(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    return vector_new(&VectorType, args, NULL);\n"
        "}\n"
        "\n"
        "static PyObject *call_keyword(PyObject *self, PyObject *args) {\n"
        "    PyObject *callable = NULL;\n"
        "    PyObject *values[2] = {NULL, NULL};\n"
        "    PyObject *kwnames = NULL;\n"
        "    PyObject *result = NULL;\n"
        "    (void)self;\n"
        '    if (!PyArg_ParseTuple(args, "O", &callable)) return NULL;\n'
        "    if (PyLong_AsLong(Py_True) != 1 || PyLong_AsLong(Py_False) != 0) {\n"
        "        return NULL;\n"
        "    }\n"
        "    if (PyLong_AsLongLong(Py_True) != 1 || "
        "PyLong_AsLongLong(Py_False) != 0) {\n"
        "        return NULL;\n"
        "    }\n"
        "    values[0] = PyLong_FromLong(2);\n"
        "    values[1] = PyLong_FromLong(4);\n"
        "    kwnames = PyTuple_New(1);\n"
        "    if (values[0] == NULL || values[1] == NULL || kwnames == NULL) {\n"
        "        Py_XDECREF(values[0]);\n"
        "        Py_XDECREF(values[1]);\n"
        "        Py_XDECREF(kwnames);\n"
        "        return NULL;\n"
        "    }\n"
        '    PyTuple_SetItem(kwnames, 0, PyUnicode_FromString("scale"));\n'
        "    result = PyObject_Vectorcall(callable, values, 1, kwnames);\n"
        "    Py_DECREF(values[0]);\n"
        "    Py_DECREF(values[1]);\n"
        "    Py_DECREF(kwnames);\n"
        "    return result;\n"
        "}\n"
        "\n"
        "static PyObject *call_method_offset(PyObject *self, PyObject *args) {\n"
        "    PyObject *storage[4] = {NULL, NULL, NULL, NULL};\n"
        "    PyObject **values = &storage[1];\n"
        '    PyObject *name = PyUnicode_FromString("offset_target");\n'
        "    PyObject *result = NULL;\n"
        "    (void)args;\n"
        "    values[0] = self;\n"
        "    values[1] = PyLong_FromLong(2);\n"
        "    values[2] = PyLong_FromLong(4);\n"
        "    if (name == NULL || values[1] == NULL || values[2] == NULL) {\n"
        "        Py_XDECREF(name);\n"
        "        Py_XDECREF(values[1]);\n"
        "        Py_XDECREF(values[2]);\n"
        "        return NULL;\n"
        "    }\n"
        "    result = PyObject_VectorcallMethod(\n"
        "        name, values, ((size_t)3) | PY_VECTORCALL_ARGUMENTS_OFFSET, NULL\n"
        "    );\n"
        "    Py_DECREF(values[1]);\n"
        "    Py_DECREF(values[2]);\n"
        "    Py_DECREF(name);\n"
        "    return result;\n"
        "}\n"
        "\n"
        "static PyObject *check_builtin_type(PyObject *self, PyObject *arg) {\n"
        "    (void)self;\n"
        "    return PyLong_FromLong(\n"
        "        PyType_Check(arg) && arg == (PyObject *)&PyLong_Type\n"
        "    );\n"
        "}\n"
        "\n"
        "static PyMethodDef Methods[] = {\n"
        '    {"make", make_vector, METH_VARARGS, "make a vectorcall object"},\n'
        '    {"call_keyword", call_keyword, METH_VARARGS, "call with keyword"},\n'
        '    {"call_method_offset", call_method_offset, METH_VARARGS, "preserve vectorcall offset bit"},\n'
        '    {"check_builtin_type", check_builtin_type, METH_O, "check int type token"},\n'
        "    {NULL, NULL, 0, NULL},\n"
        "};\n"
        "\n"
        "static PyModuleDef module = {\n"
        '    PyModuleDef_HEAD_INIT, "typevcdemo", NULL, -1, Methods,\n'
        "};\n"
        "\n"
        "PyMODINIT_FUNC PyInit_typevcdemo(void) {\n"
        "    PyObject *module_obj = NULL;\n"
        "    PyObject *empty = NULL;\n"
        "    PyObject *target = NULL;\n"
        "    if (PyType_Ready(&VectorType) < 0) return NULL;\n"
        "    module_obj = PyModule_Create(&module);\n"
        "    if (module_obj == NULL) return NULL;\n"
        "    empty = PyTuple_New(0);\n"
        "    if (empty == NULL) return NULL;\n"
        "    target = vector_new(&VectorType, empty, NULL);\n"
        "    Py_DECREF(empty);\n"
        "    if (target == NULL) return NULL;\n"
        '    if (PyObject_SetAttrString(module_obj, "offset_target", target) != 0) return NULL;\n'
        "    Py_DECREF(target);\n"
        "    return module_obj;\n"
        "}\n",
    )


def _compile_type_number_slot_extension(tmp_path: Path) -> Path:
    return _compile_extension(
        tmp_path,
        "typenumdemo",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "\n"
        "typedef struct { PyObject_HEAD long value; } NumberObject;\n"
        "static PyTypeObject NumberType;\n"
        "\n"
        "static PyObject *number_from_long(long value) {\n"
        "    NumberObject *number = (NumberObject *)PyType_GenericNew(\n"
        "        &NumberType, NULL, NULL\n"
        "    );\n"
        "    if (number != NULL) number->value = value;\n"
        "    return (PyObject *)number;\n"
        "}\n"
        "\n"
        "static int number_as_long(PyObject *value, long *out) {\n"
        "    if (PyObject_TypeCheck(value, &NumberType)) {\n"
        "        *out = ((NumberObject *)value)->value;\n"
        "        return 1;\n"
        "    }\n"
        "    if (PyLong_Check(value)) {\n"
        "        *out = PyLong_AsLong(value);\n"
        "        return PyErr_Occurred() == NULL ? 1 : -1;\n"
        "    }\n"
        "    return 0;\n"
        "}\n"
        "\n"
        "static PyObject *number_add(PyObject *left, PyObject *right) {\n"
        "    if (PyObject_TypeCheck(left, &NumberType) && PyFloat_Check(right)) {\n"
        "        return PyFloat_FromDouble(\n"
        "            (double)((NumberObject *)left)->value + PyFloat_AsDouble(right)\n"
        "        );\n"
        "    }\n"
        "    if (PyFloat_Check(left) && PyObject_TypeCheck(right, &NumberType)) {\n"
        "        return PyFloat_FromDouble(\n"
        "            PyFloat_AsDouble(left) + (double)((NumberObject *)right)->value\n"
        "        );\n"
        "    }\n"
        "    long a = 0;\n"
        "    long b = 0;\n"
        "    int a_ok = number_as_long(left, &a);\n"
        "    int b_ok = number_as_long(right, &b);\n"
        "    if (a_ok < 0 || b_ok < 0) return NULL;\n"
        "    if (!a_ok || !b_ok) Py_RETURN_NOTIMPLEMENTED;\n"
        "    return PyLong_FromLong(a + b);\n"
        "}\n"
        "\n"
        "static PyObject *number_subtract(PyObject *left, PyObject *right) {\n"
        "    long a = 0;\n"
        "    long b = 0;\n"
        "    int a_ok = number_as_long(left, &a);\n"
        "    int b_ok = number_as_long(right, &b);\n"
        "    if (a_ok < 0 || b_ok < 0) return NULL;\n"
        "    if (!a_ok || !b_ok) Py_RETURN_NOTIMPLEMENTED;\n"
        "    return PyLong_FromLong(a - b);\n"
        "}\n"
        "\n"
        "static PyObject *number_multiply(PyObject *left, PyObject *right) {\n"
        "    long a = 0;\n"
        "    long b = 0;\n"
        "    int a_ok = number_as_long(left, &a);\n"
        "    int b_ok = number_as_long(right, &b);\n"
        "    if (a_ok < 0 || b_ok < 0) return NULL;\n"
        "    if (!a_ok || !b_ok) Py_RETURN_NOTIMPLEMENTED;\n"
        "    return PyLong_FromLong(a * b);\n"
        "}\n"
        "\n"
        "static PyObject *number_true_divide(PyObject *left, PyObject *right) {\n"
        "    long a = 0;\n"
        "    long b = 0;\n"
        "    int a_ok = number_as_long(left, &a);\n"
        "    int b_ok = number_as_long(right, &b);\n"
        "    if (a_ok < 0 || b_ok < 0) return NULL;\n"
        "    if (!a_ok || !b_ok) Py_RETURN_NOTIMPLEMENTED;\n"
        "    if (b == 0) {\n"
        '        PyErr_SetString(PyExc_ZeroDivisionError, "division by zero");\n'
        "        return NULL;\n"
        "    }\n"
        "    return PyFloat_FromDouble((double)a / (double)b);\n"
        "}\n"
        "\n"
        "static PyObject *number_absolute(PyObject *value) {\n"
        "    long raw = 0;\n"
        "    if (!PyObject_TypeCheck(value, &NumberType)) {\n"
        "        Py_RETURN_NOTIMPLEMENTED;\n"
        "    }\n"
        "    raw = ((NumberObject *)value)->value;\n"
        "    return PyLong_FromLong(raw < 0 ? -raw : raw);\n"
        "}\n"
        "\n"
        "static int number_bool(PyObject *value) {\n"
        "    return ((NumberObject *)value)->value != 0;\n"
        "}\n"
        "\n"
        "static PyObject *number_richcompare(\n"
        "    PyObject *left, PyObject *right, int op\n"
        ") {\n"
        "    long a = 0;\n"
        "    long b = 0;\n"
        "    int result = 0;\n"
        "    if (!PyObject_TypeCheck(left, &NumberType)\n"
        "        || !PyObject_TypeCheck(right, &NumberType)) {\n"
        "        Py_RETURN_NOTIMPLEMENTED;\n"
        "    }\n"
        "    a = ((NumberObject *)left)->value;\n"
        "    b = ((NumberObject *)right)->value;\n"
        "    if (op == Py_LT) result = a < b;\n"
        "    else if (op == Py_LE) result = a <= b;\n"
        "    else if (op == Py_EQ) result = a == b;\n"
        "    else if (op == Py_NE) result = a != b;\n"
        "    else if (op == Py_GT) result = a > b;\n"
        "    else if (op == Py_GE) result = a >= b;\n"
        "    return number_from_long(result);\n"
        "}\n"
        "\n"
        "static PyNumberMethods NumberMethods = {\n"
        "    .nb_add = number_add,\n"
        "    .nb_subtract = number_subtract,\n"
        "    .nb_multiply = number_multiply,\n"
        "    .nb_absolute = number_absolute,\n"
        "    .nb_bool = number_bool,\n"
        "    .nb_true_divide = number_true_divide,\n"
        "};\n"
        "\n"
        "static PyTypeObject NumberType = {\n"
        "    PyVarObject_HEAD_INIT(NULL, 0)\n"
        '    .tp_name = "typenumdemo.Number",\n'
        "    .tp_basicsize = sizeof(NumberObject),\n"
        "    .tp_as_number = &NumberMethods,\n"
        "    .tp_richcompare = number_richcompare,\n"
        "    .tp_flags = Py_TPFLAGS_DEFAULT,\n"
        "    .tp_new = PyType_GenericNew,\n"
        "};\n"
        "\n"
        "static PyObject *make_number(PyObject *self, PyObject *args) {\n"
        "    long value = 0;\n"
        "    (void)self;\n"
        '    if (!PyArg_ParseTuple(args, "l", &value)) return NULL;\n'
        "    return number_from_long(value);\n"
        "}\n"
        "\n"
        "static PyMethodDef Methods[] = {\n"
        '    {"make", make_number, METH_VARARGS, "make a number"},\n'
        "    {NULL, NULL, 0, NULL},\n"
        "};\n"
        "static PyModuleDef module = {\n"
        '    PyModuleDef_HEAD_INIT, "typenumdemo", NULL, -1, Methods,\n'
        "};\n"
        "PyMODINIT_FUNC PyInit_typenumdemo(void) {\n"
        "    if (PyType_Ready(&NumberType) < 0) return NULL;\n"
        "    return PyModule_Create(&module);\n"
        "}\n",
    )


def _compile_sysflags_extension(tmp_path: Path) -> Path:
    # Behavioral regression for PySys_GetObject (batch 15). Mirrors numpy's exact
    # import-time usage (npy_static_data.c:222): PySys_GetObject("flags") then
    # PyObject_GetAttrString(flags, "optimize"). check() returns 7:
    #   1  PySys_GetObject("flags") is non-NULL (a real namespace, not NULL)
    #   2  flags.optimize == 0 (accurate for pcc's no-O compile)
    #   4  an unprovided sys attr returns NULL (honest, not a fake object)
    return _compile_extension(
        tmp_path,
        "sfdemo",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "static PyObject *sf_check(PyObject *self, PyObject *args) {\n"
        "    (void)self; (void)args;\n"
        "    long r = 0;\n"
        '    PyObject *flags = PySys_GetObject("flags");\n'
        "    if (flags == NULL) return PyLong_FromLong(r);\n"
        "    r += 1;\n"
        '    PyObject *opt = PyObject_GetAttrString(flags, "optimize");\n'
        "    if (opt != NULL && PyLong_AsLong(opt) == 0) r += 2;\n"
        '    if (PySys_GetObject("nonexistent_attr_xyz") == NULL) r += 4;\n'
        "    return PyLong_FromLong(r);\n"
        "}\n"
        "static PyMethodDef M[] = {\n"
        '    {"check", sf_check, METH_VARARGS, ""}, {NULL, NULL, 0, NULL}\n'
        "};\n"
        "static PyModuleDef mod = {\n"
        '    PyModuleDef_HEAD_INIT, "sfdemo", NULL, -1, M,\n'
        "};\n"
        "PyMODINIT_FUNC PyInit_sfdemo(void) { return PyModule_Create(&mod); }\n",
    )


def _compile_genericgetdict_extension(tmp_path: Path) -> Path:
    # Behavioral regression for PyObject_GenericGetDict (batch 16). A module-level
    # METH_VARARGS function receives the module as `self`; its __dict__ is the
    # module namespace, which holds the function "check". check() returns 3:
    #   1  PyObject_GenericGetDict(self, NULL) is a non-NULL dict
    #   2  that dict contains this module's own "check" entry
    return _compile_extension(
        tmp_path,
        "gddemo",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "static PyObject *gd_check(PyObject *self, PyObject *args) {\n"
        "    (void)args;\n"
        "    long r = 0;\n"
        "    PyObject *d = PyObject_GenericGetDict(self, NULL);\n"
        "    if (d == NULL) return PyLong_FromLong(r);\n"
        "    r += 1;\n"
        '    if (PyDict_GetItemString(d, "check") != NULL) r += 2;\n'
        "    return PyLong_FromLong(r);\n"
        "}\n"
        "static PyMethodDef M[] = {\n"
        '    {"check", gd_check, METH_VARARGS, ""}, {NULL, NULL, 0, NULL}\n'
        "};\n"
        "static PyModuleDef mod = {\n"
        '    PyModuleDef_HEAD_INIT, "gddemo", NULL, -1, M,\n'
        "};\n"
        "PyMODINIT_FUNC PyInit_gddemo(void) { return PyModule_Create(&mod); }\n",
    )


def _compile_seqiter_extension(tmp_path: Path) -> Path:
    # Behavioral regression for PySeqIter_New (batch 17). Build a real sequence
    # iterator over a tuple and drive it through the C-API PyIter_Next path.
    # check() returns 1 when iterating (10,20,30) yields exactly those 3 values
    # (sum 60), then NULL.
    return _compile_extension(
        tmp_path,
        "sidemo",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "static PyObject *si_check(PyObject *self, PyObject *args) {\n"
        "    (void)self; (void)args;\n"
        "    PyObject *t = PyTuple_New(3);\n"
        "    PyTuple_SetItem(t, 0, PyLong_FromLong(10));\n"
        "    PyTuple_SetItem(t, 1, PyLong_FromLong(20));\n"
        "    PyTuple_SetItem(t, 2, PyLong_FromLong(30));\n"
        "    PyObject *it = PySeqIter_New(t);\n"
        "    if (it == NULL) return PyLong_FromLong(-1);\n"
        "    long sum = 0; int count = 0; PyObject *item;\n"
        "    while ((item = PyIter_Next(it)) != NULL) {\n"
        "        sum += PyLong_AsLong(item);\n"
        "        if (++count > 10) break;\n"
        "    }\n"
        "    return PyLong_FromLong((sum == 60 && count == 3) ? 1 : 0);\n"
        "}\n"
        "static PyMethodDef M[] = {\n"
        '    {"check", si_check, METH_VARARGS, ""}, {NULL, NULL, 0, NULL}\n'
        "};\n"
        "static PyModuleDef mod = {\n"
        '    PyModuleDef_HEAD_INIT, "sidemo", NULL, -1, M,\n'
        "};\n"
        "PyMODINIT_FUNC PyInit_sidemo(void) { return PyModule_Create(&mod); }\n",
    )


def _compile_pymethod_extension(tmp_path: Path) -> Path:
    # Behavioral regression for PyMethod_New (batch 17). A module-level function
    # gets the module as self; PyMethod_New binds the module's own "helper"
    # function to self and the result must be a real callable. check() returns 7:
    #   1  the func object is reachable
    #   2  PyMethod_New(func, self) is non-NULL
    #   4  the bound method is callable
    return _compile_extension(
        tmp_path,
        "mndemo",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "static PyObject *mn_helper(PyObject *self, PyObject *args) {\n"
        "    (void)self; (void)args; return PyLong_FromLong(42);\n"
        "}\n"
        "static PyObject *mn_check(PyObject *self, PyObject *args) {\n"
        "    (void)args;\n"
        "    long r = 0;\n"
        '    PyObject *h = PyObject_GetAttrString(self, "helper");\n'
        "    if (h == NULL) return PyLong_FromLong(r);\n"
        "    r += 1;\n"
        "    PyObject *bm = PyMethod_New(h, self);\n"
        "    if (bm != NULL) r += 2;\n"
        "    if (bm != NULL && PyCallable_Check(bm)) r += 4;\n"
        "    return PyLong_FromLong(r);\n"
        "}\n"
        "static PyMethodDef M[] = {\n"
        '    {"helper", mn_helper, METH_VARARGS, ""},\n'
        '    {"check", mn_check, METH_VARARGS, ""}, {NULL, NULL, 0, NULL}\n'
        "};\n"
        "static PyModuleDef mod = {\n"
        '    PyModuleDef_HEAD_INIT, "mndemo", NULL, -1, M,\n'
        "};\n"
        "PyMODINIT_FUNC PyInit_mndemo(void) { return PyModule_Create(&mod); }\n",
    )


def _compile_batch18_extension(tmp_path: Path) -> Path:
    # Behavioral regression for batch 18 host symbols (PyDict_Copy, PyDict_Merge,
    # PyUnicode_Format, PyObject_GenericGetAttr/SetAttr). check() returns 15:
    #   1  PyDict_Copy is a distinct dict carrying the original's key
    #   2  PyDict_Merge(override=1) adds the other dict's key
    #   4  PyUnicode_Format("%d", (42,)) == "42"
    #   8  PyObject_GenericSetAttr then GenericGetAttr round-trips on self
    return _compile_extension(
        tmp_path,
        "b18demo",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "static PyObject *b18_check(PyObject *self, PyObject *args) {\n"
        "    (void)args;\n"
        "    long r = 0;\n"
        "    PyObject *d = PyDict_New();\n"
        '    PyDict_SetItemString(d, "a", PyLong_FromLong(1));\n'
        "    PyObject *c = PyDict_Copy(d);\n"
        '    if (c != NULL && c != d && PyDict_GetItemString(c, "a") != NULL) r += 1;\n'
        "    PyObject *e = PyDict_New();\n"
        '    PyDict_SetItemString(e, "b", PyLong_FromLong(2));\n'
        '    if (PyDict_Merge(d, e, 1) == 0 && PyDict_GetItemString(d, "b") != NULL) r += 2;\n'
        '    PyObject *fmt = PyUnicode_FromString("%d");\n'
        "    PyObject *ta = PyTuple_New(1);\n"
        "    PyTuple_SetItem(ta, 0, PyLong_FromLong(42));\n"
        "    PyObject *s = PyUnicode_Format(fmt, ta);\n"
        "    if (s != NULL) {\n"
        "        const char *cs = PyUnicode_AsUTF8(s);\n"
        "        if (cs != NULL && cs[0]=='4' && cs[1]=='2' && cs[2]=='\\0') r += 4;\n"
        "    }\n"
        '    PyObject *nm = PyUnicode_FromString("zz");\n'
        "    if (PyObject_GenericSetAttr(self, nm, PyLong_FromLong(9)) == 0) {\n"
        "        PyObject *got = PyObject_GenericGetAttr(self, nm);\n"
        "        if (got != NULL && PyLong_AsLong(got) == 9) r += 8;\n"
        "    }\n"
        "    return PyLong_FromLong(r);\n"
        "}\n"
        "static PyMethodDef M[] = {\n"
        '    {"check", b18_check, METH_VARARGS, ""}, {NULL, NULL, 0, NULL}\n'
        "};\n"
        "static PyModuleDef mod = {\n"
        '    PyModuleDef_HEAD_INIT, "b18demo", NULL, -1, M,\n'
        "};\n"
        "PyMODINIT_FUNC PyInit_b18demo(void) { return PyModule_Create(&mod); }\n",
    )


def _compile_batch19_extension(tmp_path: Path) -> Path:
    # Behavioral regression for batch 19 host symbols (PySlice_New,
    # PySlice_GetIndicesEx, PyArg_UnpackTuple, PyDictProxy_New, PyObject_Init).
    # check() returns 31:
    #   1  slice(1,8,2) over len 10 -> start1 stop8 step2 slicelen4
    #   2  slice(None,None,None) over len 5 -> 0,5,1,5
    #   4  PyArg_UnpackTuple((10,20)) -> 10,20
    #   8  PyDictProxy_New(d) is a readable mapping
    #  16  PyObject_Init returns its op (passthrough on a fresh alloc)
    return _compile_extension(
        tmp_path,
        "b19demo",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "static PyObject *b19_check(PyObject *self, PyObject *args) {\n"
        "    (void)self; (void)args;\n"
        "    long r = 0;\n"
        "    Py_ssize_t st, sp, stp, sln;\n"
        "    PyObject *sl = PySlice_New(PyLong_FromLong(1), PyLong_FromLong(8), PyLong_FromLong(2));\n"
        "    if (sl != NULL && PySlice_GetIndicesEx(sl, 10, &st, &sp, &stp, &sln) == 0\n"
        "        && st==1 && sp==8 && stp==2 && sln==4) r += 1;\n"
        "    PyObject *sl2 = PySlice_New(NULL, NULL, NULL);\n"
        "    if (sl2 != NULL && PySlice_GetIndicesEx(sl2, 5, &st, &sp, &stp, &sln) == 0\n"
        "        && st==0 && sp==5 && stp==1 && sln==5) r += 2;\n"
        "    PyObject *t = PyTuple_New(2);\n"
        "    PyTuple_SetItem(t, 0, PyLong_FromLong(10));\n"
        "    PyTuple_SetItem(t, 1, PyLong_FromLong(20));\n"
        "    PyObject *a = NULL, *b = NULL;\n"
        '    if (PyArg_UnpackTuple(t, "x", 2, 2, &a, &b) && a && b\n'
        "        && PyLong_AsLong(a)==10 && PyLong_AsLong(b)==20) r += 4;\n"
        "    PyObject *d = PyDict_New();\n"
        '    PyDict_SetItemString(d, "k", PyLong_FromLong(99));\n'
        "    PyObject *proxy = PyDictProxy_New(d);\n"
        '    if (proxy != NULL && PyDict_GetItemString(proxy, "k") != NULL) r += 8;\n'
        "    PyObject *fresh = _PyObject_New(&PyType_Type);\n"
        "    if (fresh != NULL && PyObject_Init(fresh, &PyType_Type) == fresh) r += 16;\n"
        "    return PyLong_FromLong(r);\n"
        "}\n"
        "static PyMethodDef M[] = {\n"
        '    {"check", b19_check, METH_VARARGS, ""}, {NULL, NULL, 0, NULL}\n'
        "};\n"
        "static PyModuleDef mod = {\n"
        '    PyModuleDef_HEAD_INIT, "b19demo", NULL, -1, M,\n'
        "};\n"
        "PyMODINIT_FUNC PyInit_b19demo(void) { return PyModule_Create(&mod); }\n",
    )


def _compile_batch2021_extension(tmp_path: Path) -> Path:
    # Behavioral regression for batch 20/21 host surface (PyUnicode_KIND,
    # PyUnicode_READ_CHAR, PyUnicode_1BYTE_DATA, PyNumber_Divmod, _Py_HashDouble) —
    # the symbols that let all 98 numpy _core files compile + link. check()==15:
    #   1  KIND==1 and READ_CHAR reads the right ASCII codepoints
    #   2  1BYTE_DATA is the UTF-8 byte buffer
    #   4  PyNumber_Divmod(17,5) == (3,2)
    #   8  _Py_HashDouble(NULL,2.0)==2 (CPython congruence: hash(2.0)==hash(2))
    return _compile_extension(
        tmp_path,
        "b21demo",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "static PyObject *b21_check(PyObject *self, PyObject *args) {\n"
        "    (void)self; (void)args;\n"
        "    long r = 0;\n"
        '    PyObject *s = PyUnicode_FromString("Abc");\n'
        "    if (PyUnicode_KIND(s)==1 && PyUnicode_READ_CHAR(s,0)=='A'\n"
        "        && PyUnicode_READ_CHAR(s,2)=='c') r += 1;\n"
        "    const char *data = (const char *)PyUnicode_1BYTE_DATA(s);\n"
        "    if (data != NULL && data[0]=='A' && data[1]=='b') r += 2;\n"
        "    PyObject *dm = PyNumber_Divmod(PyLong_FromLong(17), PyLong_FromLong(5));\n"
        "    if (dm != NULL && PyLong_AsLong(PyTuple_GetItem(dm,0))==3\n"
        "        && PyLong_AsLong(PyTuple_GetItem(dm,1))==2) r += 4;\n"
        "    if (_Py_HashDouble(NULL, 2.0) == 2) r += 8;\n"
        "    return PyLong_FromLong(r);\n"
        "}\n"
        "static PyMethodDef M[] = {\n"
        '    {"check", b21_check, METH_VARARGS, ""}, {NULL, NULL, 0, NULL}\n'
        "};\n"
        "static PyModuleDef mod = {\n"
        '    PyModuleDef_HEAD_INIT, "b21demo", NULL, -1, M,\n'
        "};\n"
        "PyMODINIT_FUNC PyInit_b21demo(void) { return PyModule_Create(&mod); }\n",
    )


def _compile_unicode_writer_extension(tmp_path: Path) -> Path:
    return _compile_extension(
        tmp_path,
        "writerdemo",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "#include <string.h>\n"
        "static PyObject *writer_check(PyObject *self, PyObject *args) {\n"
        "    (void)self; (void)args;\n"
        "    long score = 0;\n"
        "    PyUnicodeWriter *w = PyUnicodeWriter_Create(2);\n"
        '    PyObject *emoji = PyUnicode_FromString("\\xf0\\x9f\\x99\\x82z");\n'
        "    if (w != NULL\n"
        "        && PyUnicodeWriter_WriteChar(w, '\"') == 0\n"
        '        && PyUnicodeWriter_WriteUTF8(w, "h\\xc3\\xa9", 3) == 0\n'
        "        && PyUnicodeWriter_WriteSubstring(w, emoji, 0, 1) == 0\n"
        "        && PyUnicodeWriter_WriteStr(w, PyLong_FromLong(42)) == 0\n"
        "        && PyUnicodeWriter_WriteChar(w, '\"') == 0) {\n"
        "        PyObject *text = PyUnicodeWriter_Finish(w);\n"
        "        Py_ssize_t n = 0;\n"
        "        const char *raw = PyUnicode_AsUTF8AndSize(text, &n);\n"
        '        const char expected[] = "\\"h\\xc3\\xa9\\xf0\\x9f\\x99\\x82" "42\\"";\n'
        "        if (raw != NULL && n == 11 && strcmp(raw, expected) == 0) score += 1;\n"
        "    } else {\n"
        "        PyUnicodeWriter_Discard(w);\n"
        "    }\n"
        "    PyUnicodeWriter *stable = PyUnicodeWriter_Create(0);\n"
        '    if (stable != NULL && PyUnicodeWriter_WriteUTF8(stable, "ok", 2) == 0\n'
        '        && PyUnicodeWriter_WriteUTF8(stable, "\\xff", 1) == -1) {\n'
        "        PyErr_Clear();\n"
        "        PyObject *text = PyUnicodeWriter_Finish(stable);\n"
        "        const char *raw = PyUnicode_AsUTF8(text);\n"
        '        if (raw != NULL && strcmp(raw, "ok") == 0) score += 2;\n'
        "    } else {\n"
        "        PyUnicodeWriter_Discard(stable);\n"
        "    }\n"
        "    const char embedded[] = {'A', '\\0', (char)0xc3, (char)0xa9};\n"
        "    PyObject *mixed = PyUnicode_FromStringAndSize(embedded, 4);\n"
        "    int kind = PyUnicode_KIND(mixed);\n"
        "    void *data = PyUnicode_DATA(mixed);\n"
        "    if (PyUnicode_GET_LENGTH(mixed) == 3\n"
        "        && PyUnicode_READ(kind, data, 0) == 'A'\n"
        "        && PyUnicode_READ(kind, data, 1) == 0\n"
        "        && PyUnicode_READ(kind, data, 2) == 0xe9) score += 4;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "static PyMethodDef M[] = {\n"
        '    {"check", writer_check, METH_VARARGS, ""}, {NULL, NULL, 0, NULL}\n'
        "};\n"
        "static PyModuleDef mod = {\n"
        '    PyModuleDef_HEAD_INIT, "writerdemo", NULL, -1, M,\n'
        "};\n"
        "PyMODINIT_FUNC PyInit_writerdemo(void) { return PyModule_Create(&mod); }\n",
    )


def _compile_decode_heaptype_extension(tmp_path: Path) -> Path:
    return _compile_extension(
        tmp_path,
        "decodeheapdemo",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "#include <string.h>\n"
        'PyDoc_STRVAR(heap_doc, "module-associated heap type");\n'
        "static PyModuleDef module;\n"
        "static PyObject *HeapType = NULL;\n"
        "static int heap_traverse(PyObject *self, visitproc visit, void *arg) {\n"
        "    Py_VISIT(Py_TYPE(self));\n"
        "    return 0;\n"
        "}\n"
        "static PyType_Slot heap_slots[] = {\n"
        "    {Py_tp_doc, (void *)heap_doc},\n"
        "    {Py_tp_traverse, (void *)heap_traverse},\n"
        "    {Py_tp_new, (void *)PyType_GenericNew},\n"
        "    {0, NULL}\n"
        "};\n"
        "static PyType_Spec heap_spec = {\n"
        '    "decodeheapdemo.Heap", sizeof(PyObject), 0,\n'
        "    Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC, heap_slots\n"
        "};\n"
        "static PyObject *decode_check(PyObject *self, PyObject *args) {\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        '    PyObject *utf8 = PyUnicode_DecodeUTF8("h\\xc3\\xa9", 3, NULL);\n'
        "    if (utf8 != NULL && PyUnicode_GetLength(utf8) == 2\n"
        '        && strcmp(PyUnicode_AsUTF8(utf8), "h\\xc3\\xa9") == 0) score += 1;\n'
        "    const char latin1[] = {(char)0xe9};\n"
        '    PyObject *latin = PyUnicode_Decode(latin1, 1, "latin-1", NULL);\n'
        '    if (latin != NULL && strcmp(PyUnicode_AsUTF8(latin), "\\xc3\\xa9") == 0) score += 2;\n'
        '    if (PyUnicode_DecodeUTF8("\\xff", 1, NULL) == NULL\n'
        "        && PyErr_ExceptionMatches(PyExc_UnicodeDecodeError)) {\n"
        "        score += 4;\n"
        "        PyErr_Clear();\n"
        "    }\n"
        "    PyObject *dict = PyDict_New();\n"
        '    PyDict_SetItemString(dict, "x", PyLong_FromLong(1));\n'
        "    PyDict_Clear(dict);\n"
        "    if (PyDict_Size(dict) == 0\n"
        "        && PyType_GetModuleByDef((PyTypeObject *)HeapType, &module) == self) score += 8;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "static PyMethodDef M[] = {\n"
        '    {"check", decode_check, METH_VARARGS, ""}, {NULL, NULL, 0, NULL}\n'
        "};\n"
        "static int module_exec(PyObject *m) {\n"
        "    HeapType = PyType_FromModuleAndSpec(m, &heap_spec, NULL);\n"
        "    if (HeapType == NULL) return -1;\n"
        '    return PyModule_AddObjectRef(m, "Heap", HeapType);\n'
        "}\n"
        "static PyModuleDef_Slot module_slots[] = {\n"
        "    {Py_mod_exec, module_exec}, {0, NULL}\n"
        "};\n"
        "static PyModuleDef module = {\n"
        '    PyModuleDef_HEAD_INIT, "decodeheapdemo", NULL, 1, M, module_slots\n'
        "};\n"
        "PyMODINIT_FUNC PyInit_decodeheapdemo(void) {\n"
        "    return PyModuleDef_Init(&module);\n"
        "}\n",
    )


def _compile_simplejson_final_api_extension(tmp_path: Path) -> Path:
    return _compile_extension(
        tmp_path,
        "sjfinaldemo",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "static PyObject *final_check(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    PyObject *target = NULL;\n"
        '    if (!PyArg_ParseTuple(args, "O", &target)) return NULL;\n'
        "    long score = 0;\n"
        '    PyObject *name = PyUnicode_FromString("combine");\n'
        "    PyObject *a = PyLong_FromLong(2);\n"
        "    PyObject *b = PyLong_FromLong(3);\n"
        "    PyObject *called = PyObject_CallMethodObjArgs(target, name, a, b, NULL);\n"
        "    if (called != NULL && PyLong_AsLong(called) == 23) score += 1;\n"
        "    Py_XDECREF(called); Py_DECREF(b); Py_DECREF(a); Py_DECREF(name);\n"
        "    PyObject *empty = PyUnicode_New(0, 127);\n"
        "    if (empty != NULL && PyUnicode_GetLength(empty) == 0) score += 2;\n"
        "    Py_XDECREF(empty);\n"
        "    if (PyUnicode_New(1, 127) == NULL\n"
        "        && PyErr_ExceptionMatches(PyExc_NotImplementedError)) {\n"
        "        score += 4;\n"
        "        PyErr_Clear();\n"
        "    }\n"
        "    if (Py_IS_FINITE(1.0) && !Py_IS_FINITE(INFINITY)) score += 8;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "static PyMethodDef M[] = {\n"
        '    {"check", final_check, METH_VARARGS, ""}, {NULL, NULL, 0, NULL}\n'
        "};\n"
        "static PyModuleDef mod = {\n"
        '    PyModuleDef_HEAD_INIT, "sjfinaldemo", NULL, -1, M,\n'
        "};\n"
        "PyMODINIT_FUNC PyInit_sjfinaldemo(void) { return PyModule_Create(&mod); }\n",
    )


def _compile_compiled_module_import_extension(tmp_path: Path) -> Path:
    site = _compile_extension(
        tmp_path,
        "compiledimportdemo",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "static PyObject *lookup(PyObject *self, PyObject *args) {\n"
        "    (void)self; (void)args;\n"
        '    PyObject *module = PyImport_ImportModule("depmod");\n'
        "    if (module == NULL) return NULL;\n"
        '    PyObject *again = PyImport_ImportModule("depmod");\n'
        "    if (again == NULL || again != module) {\n"
        "        Py_XDECREF(again); Py_DECREF(module);\n"
        '        PyErr_SetString(PyExc_RuntimeError, "module identity changed");\n'
        "        return NULL;\n"
        "    }\n"
        "    Py_DECREF(again);\n"
        '    PyObject *function = PyObject_GetAttrString(module, "value");\n'
        "    Py_DECREF(module);\n"
        "    if (function == NULL) return NULL;\n"
        "    PyObject *value = PyObject_CallNoArgs(function);\n"
        "    Py_DECREF(function);\n"
        "    return value;\n"
        "}\n"
        "static PyMethodDef M[] = {\n"
        '    {"lookup", lookup, METH_VARARGS, ""}, {NULL, NULL, 0, NULL}\n'
        "};\n"
        "static PyModuleDef mod = {\n"
        '    PyModuleDef_HEAD_INIT, "compiledimportdemo", NULL, -1, M,\n'
        "};\n"
        "PyMODINIT_FUNC PyInit_compiledimportdemo(void) {\n"
        "    return PyModule_Create(&mod);\n"
        "}\n",
    )
    (site / "depmod.py").write_text(
        "VALUE = 41\n\n" "def value():\n" "    return VALUE\n",
        encoding="utf-8",
    )
    return site


def _compile_builtin_module_import_extension(tmp_path: Path) -> Path:
    return _compile_extension(
        tmp_path,
        "builtinimportdemo",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "static PyObject *lookup(PyObject *self, PyObject *args) {\n"
        "    (void)self; (void)args;\n"
        '    PyObject *module = PyImport_ImportModule("math");\n'
        "    if (module == NULL) return NULL;\n"
        '    PyObject *function = PyObject_GetAttrString(module, "floor");\n'
        "    Py_DECREF(module);\n"
        "    if (function == NULL) return NULL;\n"
        "    PyObject *argument = PyFloat_FromDouble(7.75);\n"
        "    if (argument == NULL) { Py_DECREF(function); return NULL; }\n"
        "    PyObject *value = PyObject_CallOneArg(function, argument);\n"
        "    Py_DECREF(argument); Py_DECREF(function);\n"
        "    return value;\n"
        "}\n"
        "static PyMethodDef M[] = {\n"
        '    {"lookup", lookup, METH_VARARGS, ""}, {NULL, NULL, 0, NULL}\n'
        "};\n"
        "static PyModuleDef mod = {\n"
        '    PyModuleDef_HEAD_INIT, "builtinimportdemo", NULL, -1, M,\n'
        "};\n"
        "PyMODINIT_FUNC PyInit_builtinimportdemo(void) {\n"
        "    return PyModule_Create(&mod);\n"
        "}\n",
    )


def _compile_builtin_module_graph_extension(tmp_path: Path) -> Path:
    return _compile_extension(
        tmp_path,
        "builtinmodulegraphdemo",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "static PyObject *check(PyObject *self, PyObject *args) {\n"
        "    (void)self; (void)args;\n"
        '    const char *modules[] = {"math", "sys", "time", "gc", "copy"};\n'
        '    const char *attrs[] = {"floor", "getdefaultencoding", "perf_counter", "collect", "copy"};\n'
        "    long found = 0;\n"
        "    for (long i = 0; i < 5; i++) {\n"
        "        PyObject *module = PyImport_ImportModule(modules[i]);\n"
        "        if (module == NULL) return NULL;\n"
        "        PyObject *attr = PyObject_GetAttrString(module, attrs[i]);\n"
        "        Py_DECREF(module);\n"
        "        if (attr == NULL) return NULL;\n"
        "        found += 1;\n"
        "        Py_DECREF(attr);\n"
        "    }\n"
        "    return PyLong_FromLong(found);\n"
        "}\n"
        "static PyMethodDef M[] = {\n"
        '    {"check", check, METH_VARARGS, ""}, {NULL, NULL, 0, NULL}\n'
        "};\n"
        "static PyModuleDef mod = {\n"
        '    PyModuleDef_HEAD_INIT, "builtinmodulegraphdemo", NULL, -1, M,\n'
        "};\n"
        "PyMODINIT_FUNC PyInit_builtinmodulegraphdemo(void) {\n"
        "    return PyModule_Create(&mod);\n"
        "}\n",
    )


def _compile_batch22_extension(tmp_path: Path) -> Path:
    # Behavioral regression for batch 22 host symbols (PyLong_FromUnicodeObject,
    # PyFloat_FromString, PyLong_AsLongLongAndOverflow, PySlice_AdjustIndices) —
    # introduced by numpy's now-compiling C++ umath layer. check()==15.
    return _compile_extension(
        tmp_path,
        "b22demo",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "static PyObject *b22_check(PyObject *self, PyObject *args) {\n"
        "    (void)self; (void)args;\n"
        "    long r = 0;\n"
        '    PyObject *li = PyLong_FromUnicodeObject(PyUnicode_FromString("42"), 10);\n'
        "    if (li != NULL && PyLong_AsLong(li) == 42) r += 1;\n"
        '    PyObject *fl = PyFloat_FromString(PyUnicode_FromString("3.5"));\n'
        "    if (fl != NULL && PyFloat_AsDouble(fl) == 3.5) r += 2;\n"
        "    int ov = 5;\n"
        "    long long v = PyLong_AsLongLongAndOverflow(PyLong_FromLong(7), &ov);\n"
        "    if (v == 7 && ov == 0) r += 4;\n"
        "    Py_ssize_t st = 2, sp = 8;\n"
        "    Py_ssize_t sln = PySlice_AdjustIndices(10, &st, &sp, 2);\n"
        "    if (sln == 3 && st == 2 && sp == 8) r += 8;\n"
        "    return PyLong_FromLong(r);\n"
        "}\n"
        "static PyMethodDef M[] = {\n"
        '    {"check", b22_check, METH_VARARGS, ""}, {NULL, NULL, 0, NULL}\n'
        "};\n"
        "static PyModuleDef mod = {\n"
        '    PyModuleDef_HEAD_INIT, "b22demo", NULL, -1, M,\n'
        "};\n"
        "PyMODINIT_FUNC PyInit_b22demo(void) { return PyModule_Create(&mod); }\n",
    )


def _compile_settype_extension(tmp_path: Path) -> Path:
    # Behavioral regression for Py_SET_TYPE -> pcc_capi_set_type (batch 23) — the
    # sole symbol that was undefined when link-testing numpy's full _core.
    # check() returns 3: a fresh Foo instance reports type Foo, then after
    # Py_SET_TYPE(o, &BarType) reports type Bar.
    return _compile_extension(
        tmp_path,
        "stdemo2",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "typedef struct { PyObject_HEAD long v; } FooObject;\n"
        "static PyTypeObject FooType = {\n"
        "    PyVarObject_HEAD_INIT(NULL, 0)\n"
        '    .tp_name = "stdemo2.Foo", .tp_basicsize = sizeof(FooObject),\n'
        "    .tp_flags = Py_TPFLAGS_DEFAULT, .tp_new = PyType_GenericNew,\n"
        "};\n"
        "static PyTypeObject BarType = {\n"
        "    PyVarObject_HEAD_INIT(NULL, 0)\n"
        '    .tp_name = "stdemo2.Bar", .tp_basicsize = sizeof(FooObject),\n'
        "    .tp_flags = Py_TPFLAGS_DEFAULT, .tp_new = PyType_GenericNew,\n"
        "};\n"
        "static PyObject *st2_check(PyObject *self, PyObject *args) {\n"
        "    (void)self; (void)args;\n"
        "    long r = 0;\n"
        "    PyObject *o = PyType_GenericNew(&FooType, NULL, NULL);\n"
        "    if (o == NULL) return PyLong_FromLong(-1);\n"
        "    if (Py_TYPE(o) == &FooType) r += 1;\n"
        "    Py_SET_TYPE(o, &BarType);\n"
        "    if (Py_TYPE(o) == &BarType) r += 2;\n"
        "    return PyLong_FromLong(r);\n"
        "}\n"
        "static PyMethodDef M[] = {\n"
        '    {"check", st2_check, METH_VARARGS, ""}, {NULL, NULL, 0, NULL}\n'
        "};\n"
        "static PyModuleDef mod = {\n"
        '    PyModuleDef_HEAD_INIT, "stdemo2", NULL, -1, M,\n'
        "};\n"
        "PyMODINIT_FUNC PyInit_stdemo2(void) {\n"
        "    if (PyType_Ready(&FooType) < 0) return NULL;\n"
        "    if (PyType_Ready(&BarType) < 0) return NULL;\n"
        "    return PyModule_Create(&mod);\n"
        "}\n",
    )


def _compile_multiphase_extension(tmp_path: Path) -> Path:
    # Behavioral regression for PEP 489 multi-phase init: PyInit returns
    # PyModuleDef_Init(&def) with a Py_mod_exec slot; the loader must build the
    # module + run the exec slot (where numpy's _multiarray_umath registers its
    # types). The exec slot here registers `answer` = 42. main asserts
    # mpdemo.answer == 42, proving the slot ran.
    return _compile_extension(
        tmp_path,
        "mpdemo",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "static int mp_exec(PyObject *m) {\n"
        '    PyModule_AddObject(m, "answer", PyLong_FromLong(42));\n'
        "    return 0;\n"
        "}\n"
        "static PyModuleDef_Slot mp_slots[] = {\n"
        "    {Py_mod_exec, (void *)mp_exec}, {0, NULL}\n"
        "};\n"
        "static PyModuleDef mpmod = {\n"
        '    PyModuleDef_HEAD_INIT, "mpdemo", NULL, 0, NULL, mp_slots, NULL, NULL, NULL\n'
        "};\n"
        "PyMODINIT_FUNC PyInit_mpdemo(void) { return PyModuleDef_Init(&mpmod); }\n",
    )


def _compile_capsule_extension(tmp_path: Path) -> Path:
    return _compile_extension(
        tmp_path,
        "capsdemo",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "\n"
        "static long secret = 37;\n"
        "static long context_secret = 11;\n"
        "static long destructor_secret = 13;\n"
        "static long destructor_hits = 0;\n"
        "\n"
        "static void capsdemo_destructor(PyObject *capsule) {\n"
        '    long *ptr = (long *)PyCapsule_GetPointer(capsule, "capsdemo.destruct");\n'
        "    if (ptr != NULL) destructor_hits += *ptr;\n"
        "}\n"
        "\n"
        "static PyObject *capsdemo_value(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        '    PyObject *capsule = PyCapsule_New(&secret, "capsdemo.secret", NULL);\n'
        "    if (capsule == NULL) return NULL;\n"
        '    if (!PyCapsule_IsValid(capsule, "capsdemo.secret")) {\n'
        "        Py_DECREF(capsule);\n"
        "        return NULL;\n"
        "    }\n"
        '    long *ptr = (long *)PyCapsule_GetPointer(capsule, "capsdemo.secret");\n'
        "    Py_DECREF(capsule);\n"
        "    if (ptr == NULL) return NULL;\n"
        "    return PyLong_FromLong(*ptr);\n"
        "}\n"
        "\n"
        "static PyObject *capsdemo_context_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        '    PyObject *capsule = PyCapsule_New(&secret, "capsdemo.secret", NULL);\n'
        "    if (capsule == NULL) return NULL;\n"
        "    if (!PyCapsule_CheckExact(capsule)) {\n"
        "        Py_DECREF(capsule);\n"
        '        PyErr_SetString(PyExc_RuntimeError, "capsule check failed");\n'
        "        return NULL;\n"
        "    }\n"
        "    if (PyCapsule_GetContext(capsule) != NULL || PyErr_Occurred()) {\n"
        "        Py_DECREF(capsule);\n"
        '        PyErr_SetString(PyExc_RuntimeError, "unexpected initial capsule context");\n'
        "        return NULL;\n"
        "    }\n"
        "    if (PyCapsule_SetContext(capsule, &context_secret) != 0) {\n"
        "        Py_DECREF(capsule);\n"
        "        return NULL;\n"
        "    }\n"
        "    long *ctx = (long *)PyCapsule_GetContext(capsule);\n"
        "    if (ctx == NULL || *ctx != 11) {\n"
        "        Py_DECREF(capsule);\n"
        '        PyErr_SetString(PyExc_RuntimeError, "capsule context mismatch");\n'
        "        return NULL;\n"
        "    }\n"
        '    if (PyCapsule_SetName(capsule, "capsdemo.renamed") != 0) {\n'
        "        Py_DECREF(capsule);\n"
        "        return NULL;\n"
        "    }\n"
        '    if (PyCapsule_IsValid(capsule, "capsdemo.secret")) {\n'
        "        Py_DECREF(capsule);\n"
        '        PyErr_SetString(PyExc_RuntimeError, "old capsule name still valid");\n'
        "        return NULL;\n"
        "    }\n"
        '    long *ptr = (long *)PyCapsule_GetPointer(capsule, "capsdemo.renamed");\n'
        "    if (ptr == NULL || *ptr != 37) {\n"
        "        Py_DECREF(capsule);\n"
        '        PyErr_SetString(PyExc_RuntimeError, "renamed capsule pointer mismatch");\n'
        "        return NULL;\n"
        "    }\n"
        "    const char *name = PyCapsule_GetName(capsule);\n"
        "    if (name == NULL || name[9] != 'r') {\n"
        "        Py_DECREF(capsule);\n"
        '        PyErr_SetString(PyExc_RuntimeError, "renamed capsule name mismatch");\n'
        "        return NULL;\n"
        "    }\n"
        "    Py_DECREF(capsule);\n"
        "    return PyLong_FromLong(*ptr + *ctx);\n"
        "}\n"
        "\n"
        "static PyObject *capsdemo_destructor_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    destructor_hits = 0;\n"
        '    PyObject *capsule = PyCapsule_New(&destructor_secret, "capsdemo.destruct", capsdemo_destructor);\n'
        "    if (capsule == NULL) return NULL;\n"
        "    Py_DECREF(capsule);\n"
        "    return PyLong_FromLong(destructor_hits);\n"
        "}\n"
        "\n"
        "static PyObject *capsdemo_pointer_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        '    PyObject *capsule = PyCapsule_New(&secret, "capsdemo.ptr", capsdemo_destructor);\n'
        "    if (capsule == NULL) return NULL;\n"
        "    if (PyCapsule_GetDestructor(capsule) == capsdemo_destructor) score += 1;\n"
        "    if (PyCapsule_SetPointer(capsule, &context_secret) == 0) score += 2;\n"
        '    long *ptr = (long *)PyCapsule_GetPointer(capsule, "capsdemo.ptr");\n'
        "    if (ptr != NULL && *ptr == 11) score += 4;\n"
        "    if (PyCapsule_SetDestructor(capsule, NULL) == 0) score += 8;\n"
        "    if (PyCapsule_GetDestructor(capsule) == NULL && PyErr_Occurred() == NULL) score += 16;\n"
        "    Py_DECREF(capsule);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyMethodDef CapsDemoMethods[] = {\n"
        '    {"value", capsdemo_value, METH_VARARGS, "read through a capsule"},\n'
        '    {"context_score", capsdemo_context_score, METH_VARARGS, "read capsule context"},\n'
        '    {"destructor_score", capsdemo_destructor_score, METH_VARARGS, "run capsule destructor"},\n'
        '    {"pointer_score", capsdemo_pointer_score, METH_VARARGS, "set/get capsule pointer and destructor"},\n'
        "    {NULL, NULL, 0, NULL},\n"
        "};\n"
        "\n"
        "static PyModuleDef capsmodule = {\n"
        '    PyModuleDef_HEAD_INIT, "capsdemo", NULL, -1, CapsDemoMethods,\n'
        "};\n"
        "\n"
        "PyMODINIT_FUNC PyInit_capsdemo(void) {\n"
        "    return PyModule_Create(&capsmodule);\n"
        "}\n",
    )


def _compile_capsule_import_extensions(tmp_path: Path) -> Path:
    site = _compile_extension(
        tmp_path,
        "capiprov",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "\n"
        "static long secret = 73;\n"
        "static long capiprov_add_bias(long value) { return value + 78; }\n"
        "static void *api_table[] = {(void *)capiprov_add_bias, &secret};\n"
        "\n"
        "static PyMethodDef ProviderMethods[] = {\n"
        "    {NULL, NULL, 0, NULL},\n"
        "};\n"
        "\n"
        "static PyModuleDef providermodule = {\n"
        '    PyModuleDef_HEAD_INIT, "capiprov", NULL, -1, ProviderMethods,\n'
        "};\n"
        "\n"
        "PyMODINIT_FUNC PyInit_capiprov(void) {\n"
        "    PyObject *module = PyModule_Create(&providermodule);\n"
        "    if (module == NULL) return NULL;\n"
        '    PyObject *capsule = PyCapsule_New(&secret, "capiprov._API", NULL);\n'
        "    if (capsule == NULL) return NULL;\n"
        '    if (PyModule_AddObject(module, "_API", capsule) != 0) return NULL;\n'
        '    capsule = PyCapsule_New(api_table, "capiprov._TABLE", NULL);\n'
        "    if (capsule == NULL) return NULL;\n"
        '    if (PyModule_AddObject(module, "_TABLE", capsule) != 0) return NULL;\n'
        "    return module;\n"
        "}\n",
    )
    _compile_extension(
        tmp_path,
        "capicons",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "\n"
        "static PyObject *capicons_value(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        '    long *ptr = (long *)PyCapsule_Import("capiprov._API", 0);\n'
        "    if (ptr == NULL) return NULL;\n"
        "    return PyLong_FromLong(*ptr);\n"
        "}\n"
        "\n"
        "static PyObject *capicons_table_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        '    void **api = (void **)PyCapsule_Import("capiprov._TABLE", 0);\n'
        "    if (api == NULL) return NULL;\n"
        "    long (*add_bias)(long) = (long (*)(long))api[0];\n"
        "    long *ptr = (long *)api[1];\n"
        "    if (add_bias == NULL || ptr == NULL) return NULL;\n"
        "    return PyLong_FromLong(add_bias(*ptr));\n"
        "}\n"
        "\n"
        "static PyMethodDef ConsumerMethods[] = {\n"
        '    {"value", capicons_value, METH_VARARGS, "read provider API capsule"},\n'
        '    {"table_score", capicons_table_score, METH_VARARGS, "read provider API table"},\n'
        "    {NULL, NULL, 0, NULL},\n"
        "};\n"
        "\n"
        "static PyModuleDef consumermodule = {\n"
        '    PyModuleDef_HEAD_INIT, "capicons", NULL, -1, ConsumerMethods,\n'
        "};\n"
        "\n"
        "PyMODINIT_FUNC PyInit_capicons(void) {\n"
        "    return PyModule_Create(&consumermodule);\n"
        "}\n",
    )
    return site


def _compile_numpy_capi_provider_extensions(tmp_path: Path) -> Path:
    site = _compile_extension_source(
        tmp_path,
        "pccnpapi",
        REPO / "utils" / "pcc_numpy_capi_provider" / "pccnpapi.c",
    )
    _compile_extension(
        tmp_path,
        "pccnpcons",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <stdint.h>\n"
        "#include <numpy/arrayobject.h>\n"
        "#include <numpy/ufuncobject.h>\n"
        "\n"
        "static PyObject *pccnpcons_table_shape_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    if (PyArray_API == NULL || PyUFunc_API == NULL) {\n"
        '        PyErr_SetString(PyExc_RuntimeError, "NumPy C API tables were not imported");\n'
        "        return NULL;\n"
        "    }\n"
        "    long count = 0;\n"
        "    for (int i = 0; i < 23; i++) {\n"
        "        if (PyArray_API[i] != NULL) count++;\n"
        "    }\n"
        "    if (PyUFunc_API[0] != NULL) count += 100;\n"
        "    return PyLong_FromLong(count);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_type_object_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    PyTypeObject *array_type = &PyArray_Type;\n"
        "    PyTypeObject *descr_type = &PyArrayDescr_Type;\n"
        "    long score = 0;\n"
        "    if (array_type != NULL && descr_type != NULL && array_type != descr_type) score += 1;\n"
        "    if (Py_TYPE((PyObject *)array_type) == &PyType_Type && Py_TYPE((PyObject *)descr_type) == &PyType_Type) score += 10;\n"
        "    if ((PyType_GetFlags(array_type) & Py_TPFLAGS_READY) && (PyType_GetFlags(descr_type) & Py_TPFLAGS_READY)) score += 100;\n"
        '    if (array_type->tp_name != NULL && strcmp(array_type->tp_name, "numpy.ndarray") == 0) score += 1000;\n'
        '    if (descr_type->tp_name != NULL && strcmp(descr_type->tp_name, "numpy.dtype") == 0) score += 10000;\n'
        "    if (PyType_IsSubtype(array_type, &PyBaseObject_Type) && PyType_IsSubtype(descr_type, &PyBaseObject_Type)) score += 100000;\n"
        "    npy_intp dims[1] = {1};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (arr == NULL) return NULL;\n"
        "    if (PyObject_TypeCheck(arr, array_type)) score += 1000000;\n"
        "    if (Py_TYPE(arr) == array_type) score += 10000000;\n"
        "    PyArray_Descr *descr = PyArray_DescrFromType(NPY_INT);\n"
        "    if (descr != NULL && PyObject_TypeCheck((PyObject *)descr, descr_type)) score += 100000000;\n"
        "    PyArray_Descr *arr_descr = PyArray_DESCR((PyArrayObject *)arr);\n"
        "    if (arr_descr != NULL && Py_TYPE((PyObject *)arr_descr) == descr_type) score += 1000000000;\n"
        "    Py_DECREF(arr);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_type_object_from_type_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    PyObject *bool_type = PyArray_TypeObjectFromType(NPY_BOOL);\n"
        "    PyObject *int_type = PyArray_TypeObjectFromType(NPY_INT);\n"
        "    PyObject *uint_type = PyArray_TypeObjectFromType(NPY_UINT);\n"
        "    PyObject *double_type = PyArray_TypeObjectFromType(NPY_DOUBLE);\n"
        "    PyObject *complex_type = PyArray_TypeObjectFromType(NPY_CDOUBLE);\n"
        "    PyObject *string_type = PyArray_TypeObjectFromType(NPY_STRING);\n"
        "    PyObject *object_type = PyArray_TypeObjectFromType(NPY_OBJECT);\n"
        "    long score = 0;\n"
        "    if (bool_type == (PyObject *)&PyBool_Type) score += 1;\n"
        "    if (int_type == (PyObject *)&PyLong_Type && uint_type == (PyObject *)&PyLong_Type) score += 10;\n"
        "    if (double_type == (PyObject *)&PyFloat_Type) score += 100;\n"
        "    if (complex_type == (PyObject *)&PyComplex_Type) score += 1000;\n"
        "    if (string_type == (PyObject *)&PyBytes_Type) score += 10000;\n"
        "    if (object_type == (PyObject *)&PyBaseObject_Type) score += 100000;\n"
        "    PyObject *bad = PyArray_TypeObjectFromType(9999);\n"
        "    if (bad == NULL && PyErr_ExceptionMatches(PyExc_NotImplementedError)) score += 1000000;\n"
        "    Py_XDECREF(bad);\n"
        "    PyErr_Clear();\n"
        "    Py_XDECREF(bool_type);\n"
        "    Py_XDECREF(int_type);\n"
        "    Py_XDECREF(uint_type);\n"
        "    Py_XDECREF(double_type);\n"
        "    Py_XDECREF(complex_type);\n"
        "    Py_XDECREF(string_type);\n"
        "    Py_XDECREF(object_type);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_unsupported_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    PyArray_Descr *descr = PyArray_DescrFromType(NPY_VOID);\n"
        "    if (descr != NULL) {\n"
        '        PyErr_SetString(PyExc_RuntimeError, "PyArray_DescrFromType unexpectedly succeeded");\n'
        "        return NULL;\n"
        "    }\n"
        "    if (PyErr_ExceptionMatches(PyExc_NotImplementedError)) score += 1;\n"
        "    PyErr_Clear();\n"
        '    PyObject *result = PyUFunc_FromFuncAndData(NULL, NULL, NULL, 0, 1, 1, 0, "stub", "stub", 0);\n'
        "    if (result != NULL) return result;\n"
        "    if (PyErr_ExceptionMatches(PyExc_NotImplementedError)) score += 100;\n"
        "    PyErr_Clear();\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static void pccnpcons_add_double_loop(char **args, const npy_intp *dimensions, const npy_intp *steps, void *data) {\n"
        "    (void)data;\n"
        "    char *lhs = args[0];\n"
        "    char *rhs = args[1];\n"
        "    char *out = args[2];\n"
        "    for (npy_intp i = 0; i < dimensions[0]; i++) {\n"
        "        *(double *)out = *(double *)lhs + *(double *)rhs;\n"
        "        lhs += steps[0];\n"
        "        rhs += steps[1];\n"
        "        out += steps[2];\n"
        "    }\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_ufunc_registration_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    PyUFuncGenericFunction funcs[1] = {pccnpcons_add_double_loop};\n"
        "    void *loop_data[1] = {NULL};\n"
        "    char types[3] = {NPY_DOUBLE, NPY_DOUBLE, NPY_DOUBLE};\n"
        '    PyObject *ufunc = PyUFunc_FromFuncAndData(funcs, loop_data, types, 1, 2, 1, 0, "pccadd", "doc", 0);\n'
        "    if (ufunc == NULL) return NULL;\n"
        "    long score = 0;\n"
        "    if (!PyCapsule_CheckExact(ufunc)) score += 1;\n"
        "    npy_intp dims[1] = {3};\n"
        "    PyObject *lhs_obj = PyArray_SimpleNew(1, dims, NPY_DOUBLE);\n"
        "    PyObject *rhs_obj = PyArray_SimpleNew(1, dims, NPY_DOUBLE);\n"
        "    if (lhs_obj == NULL || rhs_obj == NULL) {\n"
        "        Py_XDECREF(lhs_obj);\n"
        "        Py_XDECREF(rhs_obj);\n"
        "        Py_DECREF(ufunc);\n"
        "        return NULL;\n"
        "    }\n"
        "    double *lhs = (double *)PyArray_DATA((PyArrayObject *)lhs_obj);\n"
        "    double *rhs = (double *)PyArray_DATA((PyArrayObject *)rhs_obj);\n"
        "    if (lhs != NULL && rhs != NULL) {\n"
        "        lhs[0] = 1.5;\n"
        "        lhs[1] = 2.5;\n"
        "        lhs[2] = 3.5;\n"
        "        rhs[0] = 10.0;\n"
        "        rhs[1] = 20.0;\n"
        "        rhs[2] = 30.0;\n"
        "        score += 10;\n"
        "    }\n"
        "    PyObject *out_obj = PyObject_CallFunctionObjArgs(ufunc, lhs_obj, rhs_obj, NULL);\n"
        "    if (out_obj == NULL) {\n"
        "        Py_DECREF(lhs_obj);\n"
        "        Py_DECREF(rhs_obj);\n"
        "        Py_DECREF(ufunc);\n"
        "        return NULL;\n"
        "    }\n"
        "    if (PyArray_Check(out_obj) && PyArray_SIZE((PyArrayObject *)out_obj) == 3) score += 100;\n"
        "    PyArray_Descr *out_descr = PyArray_DESCR((PyArrayObject *)out_obj);\n"
        "    if (out_descr != NULL && out_descr->type_num == NPY_DOUBLE) score += 1000;\n"
        "    double *out = (double *)PyArray_DATA((PyArrayObject *)out_obj);\n"
        "    if (out != NULL && out[0] == 11.5 && out[1] == 22.5 && out[2] == 33.5) score += 10000;\n"
        "    Py_DECREF(out_obj);\n"
        "    PyObject *scalar = PyFloat_FromDouble(7.25);\n"
        "    if (scalar == NULL) {\n"
        "        Py_DECREF(lhs_obj);\n"
        "        Py_DECREF(rhs_obj);\n"
        "        Py_DECREF(ufunc);\n"
        "        return NULL;\n"
        "    }\n"
        "    out_obj = PyObject_CallFunctionObjArgs(ufunc, lhs_obj, scalar, NULL);\n"
        "    if (out_obj == NULL) {\n"
        "        Py_DECREF(scalar);\n"
        "        Py_DECREF(lhs_obj);\n"
        "        Py_DECREF(rhs_obj);\n"
        "        Py_DECREF(ufunc);\n"
        "        return NULL;\n"
        "    }\n"
        "    out = (double *)PyArray_DATA((PyArrayObject *)out_obj);\n"
        "    if (out != NULL && out[0] == 8.75 && out[1] == 9.75 && out[2] == 10.75) score += 100000;\n"
        "    Py_DECREF(out_obj);\n"
        "    out_obj = PyObject_CallFunctionObjArgs(ufunc, scalar, rhs_obj, NULL);\n"
        "    if (out_obj == NULL) {\n"
        "        Py_DECREF(scalar);\n"
        "        Py_DECREF(lhs_obj);\n"
        "        Py_DECREF(rhs_obj);\n"
        "        Py_DECREF(ufunc);\n"
        "        return NULL;\n"
        "    }\n"
        "    out = (double *)PyArray_DATA((PyArrayObject *)out_obj);\n"
        "    if (out != NULL && out[0] == 17.25 && out[1] == 27.25 && out[2] == 37.25) score += 1000000;\n"
        "    Py_DECREF(out_obj);\n"
        "    PyObject *scalar2 = PyFloat_FromDouble(0.75);\n"
        "    if (scalar2 == NULL) {\n"
        "        Py_DECREF(scalar);\n"
        "        Py_DECREF(lhs_obj);\n"
        "        Py_DECREF(rhs_obj);\n"
        "        Py_DECREF(ufunc);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *scalar_result = PyObject_CallFunctionObjArgs(ufunc, scalar, scalar2, NULL);\n"
        "    if (scalar_result == NULL) {\n"
        "        Py_DECREF(scalar2);\n"
        "        Py_DECREF(scalar);\n"
        "        Py_DECREF(lhs_obj);\n"
        "        Py_DECREF(rhs_obj);\n"
        "        Py_DECREF(ufunc);\n"
        "        return NULL;\n"
        "    }\n"
        "    if (PyFloat_Check(scalar_result) && PyFloat_AsDouble(scalar_result) == 8.0 && PyErr_Occurred() == NULL) score += 10000000;\n"
        "    Py_DECREF(scalar_result);\n"
        "    Py_DECREF(scalar2);\n"
        "    PyObject *int_scalar = PyLong_FromLong(4);\n"
        "    if (int_scalar == NULL) {\n"
        "        Py_DECREF(scalar);\n"
        "        Py_DECREF(lhs_obj);\n"
        "        Py_DECREF(rhs_obj);\n"
        "        Py_DECREF(ufunc);\n"
        "        return NULL;\n"
        "    }\n"
        "    out_obj = PyObject_CallFunctionObjArgs(ufunc, lhs_obj, int_scalar, NULL);\n"
        "    if (out_obj == NULL) {\n"
        "        Py_DECREF(int_scalar);\n"
        "        Py_DECREF(scalar);\n"
        "        Py_DECREF(lhs_obj);\n"
        "        Py_DECREF(rhs_obj);\n"
        "        Py_DECREF(ufunc);\n"
        "        return NULL;\n"
        "    }\n"
        "    out = (double *)PyArray_DATA((PyArrayObject *)out_obj);\n"
        "    if (out != NULL && out[0] == 5.5 && out[1] == 6.5 && out[2] == 7.5) score += 100000000;\n"
        "    Py_DECREF(out_obj);\n"
        "    scalar_result = PyObject_CallFunctionObjArgs(ufunc, scalar, int_scalar, NULL);\n"
        "    if (scalar_result == NULL) {\n"
        "        Py_DECREF(int_scalar);\n"
        "        Py_DECREF(scalar);\n"
        "        Py_DECREF(lhs_obj);\n"
        "        Py_DECREF(rhs_obj);\n"
        "        Py_DECREF(ufunc);\n"
        "        return NULL;\n"
        "    }\n"
        "    if (PyFloat_Check(scalar_result) && PyFloat_AsDouble(scalar_result) == 11.25 && PyErr_Occurred() == NULL) score += 1000000000;\n"
        "    Py_DECREF(scalar_result);\n"
        "    PyObject *int_arr_obj = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (int_arr_obj == NULL) {\n"
        "        Py_DECREF(int_scalar);\n"
        "        Py_DECREF(scalar);\n"
        "        Py_DECREF(lhs_obj);\n"
        "        Py_DECREF(rhs_obj);\n"
        "        Py_DECREF(ufunc);\n"
        "        return NULL;\n"
        "    }\n"
        "    int *int_arr = (int *)PyArray_DATA((PyArrayObject *)int_arr_obj);\n"
        "    if (int_arr != NULL) {\n"
        "        int_arr[0] = 4;\n"
        "        int_arr[1] = 5;\n"
        "        int_arr[2] = 6;\n"
        "    }\n"
        "    out_obj = PyObject_CallFunctionObjArgs(ufunc, int_arr_obj, rhs_obj, NULL);\n"
        "    if (out_obj == NULL) {\n"
        "        Py_DECREF(int_arr_obj);\n"
        "        Py_DECREF(int_scalar);\n"
        "        Py_DECREF(scalar);\n"
        "        Py_DECREF(lhs_obj);\n"
        "        Py_DECREF(rhs_obj);\n"
        "        Py_DECREF(ufunc);\n"
        "        return NULL;\n"
        "    }\n"
        "    out = (double *)PyArray_DATA((PyArrayObject *)out_obj);\n"
        "    if (out != NULL && out[0] == 14.0 && out[1] == 25.0 && out[2] == 36.0) score += 10000000000L;\n"
        "    Py_DECREF(out_obj);\n"
        "    out_obj = PyObject_CallFunctionObjArgs(ufunc, lhs_obj, int_arr_obj, NULL);\n"
        "    if (out_obj == NULL) {\n"
        "        Py_DECREF(int_arr_obj);\n"
        "        Py_DECREF(int_scalar);\n"
        "        Py_DECREF(scalar);\n"
        "        Py_DECREF(lhs_obj);\n"
        "        Py_DECREF(rhs_obj);\n"
        "        Py_DECREF(ufunc);\n"
        "        return NULL;\n"
        "    }\n"
        "    out = (double *)PyArray_DATA((PyArrayObject *)out_obj);\n"
        "    if (out != NULL && out[0] == 5.5 && out[1] == 7.5 && out[2] == 9.5) score += 100000000000L;\n"
        "    Py_DECREF(out_obj);\n"
        "    npy_intp one_dim[1] = {1};\n"
        "    PyObject *one_obj = PyArray_SimpleNew(1, one_dim, NPY_DOUBLE);\n"
        "    if (one_obj == NULL) {\n"
        "        Py_DECREF(int_arr_obj);\n"
        "        Py_DECREF(int_scalar);\n"
        "        Py_DECREF(scalar);\n"
        "        Py_DECREF(lhs_obj);\n"
        "        Py_DECREF(rhs_obj);\n"
        "        Py_DECREF(ufunc);\n"
        "        return NULL;\n"
        "    }\n"
        "    double *one = (double *)PyArray_DATA((PyArrayObject *)one_obj);\n"
        "    if (one != NULL) one[0] = 100.0;\n"
        "    out_obj = PyObject_CallFunctionObjArgs(ufunc, one_obj, rhs_obj, NULL);\n"
        "    if (out_obj == NULL) {\n"
        "        Py_DECREF(one_obj);\n"
        "        Py_DECREF(int_arr_obj);\n"
        "        Py_DECREF(int_scalar);\n"
        "        Py_DECREF(scalar);\n"
        "        Py_DECREF(lhs_obj);\n"
        "        Py_DECREF(rhs_obj);\n"
        "        Py_DECREF(ufunc);\n"
        "        return NULL;\n"
        "    }\n"
        "    out = (double *)PyArray_DATA((PyArrayObject *)out_obj);\n"
        "    if (out != NULL && out[0] == 110.0 && out[1] == 120.0 && out[2] == 130.0) score += 1000000000000L;\n"
        "    Py_DECREF(out_obj);\n"
        "    out_obj = PyObject_CallFunctionObjArgs(ufunc, lhs_obj, one_obj, NULL);\n"
        "    if (out_obj == NULL) {\n"
        "        Py_DECREF(one_obj);\n"
        "        Py_DECREF(int_arr_obj);\n"
        "        Py_DECREF(int_scalar);\n"
        "        Py_DECREF(scalar);\n"
        "        Py_DECREF(lhs_obj);\n"
        "        Py_DECREF(rhs_obj);\n"
        "        Py_DECREF(ufunc);\n"
        "        return NULL;\n"
        "    }\n"
        "    out = (double *)PyArray_DATA((PyArrayObject *)out_obj);\n"
        "    if (out != NULL && out[0] == 101.5 && out[1] == 102.5 && out[2] == 103.5) score += 10000000000000L;\n"
        "    Py_DECREF(out_obj);\n"
        "    npy_intp two_dim[1] = {2};\n"
        "    PyObject *two_obj = PyArray_SimpleNew(1, two_dim, NPY_DOUBLE);\n"
        "    if (two_obj == NULL) {\n"
        "        Py_DECREF(one_obj);\n"
        "        Py_DECREF(int_arr_obj);\n"
        "        Py_DECREF(int_scalar);\n"
        "        Py_DECREF(scalar);\n"
        "        Py_DECREF(lhs_obj);\n"
        "        Py_DECREF(rhs_obj);\n"
        "        Py_DECREF(ufunc);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *bad_obj = PyObject_CallFunctionObjArgs(ufunc, lhs_obj, two_obj, NULL);\n"
        "    if (bad_obj == NULL && PyErr_ExceptionMatches(PyExc_ValueError)) {\n"
        "        PyErr_Clear();\n"
        "        score += 100000000000000L;\n"
        "    } else {\n"
        "        Py_XDECREF(bad_obj);\n"
        "        Py_DECREF(two_obj);\n"
        "        Py_DECREF(one_obj);\n"
        "        Py_DECREF(int_arr_obj);\n"
        "        Py_DECREF(int_scalar);\n"
        "        Py_DECREF(scalar);\n"
        "        Py_DECREF(lhs_obj);\n"
        "        Py_DECREF(rhs_obj);\n"
        "        Py_DECREF(ufunc);\n"
        '        PyErr_SetString(PyExc_AssertionError, "incompatible ufunc shapes should fail");\n'
        "        return NULL;\n"
        "    }\n"
        "    Py_DECREF(two_obj);\n"
        "    Py_DECREF(one_obj);\n"
        "    Py_DECREF(int_arr_obj);\n"
        "    Py_DECREF(int_scalar);\n"
        "    Py_DECREF(scalar);\n"
        "    Py_DECREF(lhs_obj);\n"
        "    Py_DECREF(rhs_obj);\n"
        "    Py_DECREF(ufunc);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_inferred_dtype_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    PyObject *i0 = PyLong_FromLong(14);\n"
        "    PyObject *i1 = PyLong_FromLong(15);\n"
        "    PyObject *i2 = PyLong_FromLong(16);\n"
        "    if (i0 == NULL || i1 == NULL || i2 == NULL) {\n"
        "        Py_XDECREF(i0);\n"
        "        Py_XDECREF(i1);\n"
        "        Py_XDECREF(i2);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *int_seq = PyTuple_Pack(3, i0, i1, i2);\n"
        "    Py_DECREF(i0);\n"
        "    Py_DECREF(i1);\n"
        "    Py_DECREF(i2);\n"
        "    if (int_seq == NULL) return NULL;\n"
        "    PyObject *int_arr_obj = PyArray_FromAny(int_seq, NULL, 1, 1, 0, NULL);\n"
        "    Py_DECREF(int_seq);\n"
        "    if (int_arr_obj == NULL) return NULL;\n"
        "    PyArrayObject *int_arr = (PyArrayObject *)int_arr_obj;\n"
        "    npy_intp *int_dims = PyArray_DIMS(int_arr);\n"
        "    PyArray_Descr *int_descr = PyArray_DESCR(int_arr);\n"
        "    long *int_data = (long *)PyArray_DATA(int_arr);\n"
        "    if (PyArray_NDIM(int_arr) == 1 && int_dims != NULL && int_dims[0] == 3) score += 1;\n"
        "    if (int_descr != NULL && int_descr->type_num == NPY_LONG && int_descr->elsize == (int)sizeof(long)) score += 10;\n"
        "    if (int_data != NULL && int_data[0] == 14 && int_data[2] == 16) score += 100;\n"
        "    PyObject *int_item = int_data == NULL ? NULL : PyArray_GETITEM(int_arr, &int_data[2]);\n"
        "    if (int_item == NULL) {\n"
        "        Py_DECREF(int_arr_obj);\n"
        "        return NULL;\n"
        "    }\n"
        "    if (PyLong_AsLong(int_item) == 16 && PyErr_Occurred() == NULL) score += 1000;\n"
        "    Py_DECREF(int_item);\n"
        "    Py_DECREF(int_arr_obj);\n"
        "    unsigned long max_ulong = (unsigned long)-1;\n"
        "    PyObject *u0 = PyLong_FromUnsignedLong(max_ulong);\n"
        "    PyObject *u1 = PyLong_FromUnsignedLong(7UL);\n"
        "    if (u0 == NULL || u1 == NULL) {\n"
        "        Py_XDECREF(u0);\n"
        "        Py_XDECREF(u1);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *ulong_seq = PyTuple_Pack(2, u0, u1);\n"
        "    Py_DECREF(u0);\n"
        "    Py_DECREF(u1);\n"
        "    if (ulong_seq == NULL) return NULL;\n"
        "    PyObject *ulong_arr_obj = PyArray_FromAny(ulong_seq, NULL, 1, 1, 0, NULL);\n"
        "    Py_DECREF(ulong_seq);\n"
        "    if (ulong_arr_obj == NULL) return NULL;\n"
        "    PyArrayObject *ulong_arr = (PyArrayObject *)ulong_arr_obj;\n"
        "    PyArray_Descr *ulong_descr = PyArray_DESCR(ulong_arr);\n"
        "    unsigned long *ulong_data = (unsigned long *)PyArray_DATA(ulong_arr);\n"
        "    if (ulong_descr != NULL && ulong_descr->type_num == NPY_ULONG && ulong_descr->elsize == (int)sizeof(unsigned long)) score += 100000000L;\n"
        "    if (ulong_data != NULL && ulong_data[0] == max_ulong && ulong_data[1] == 7UL) score += 1000000000L;\n"
        "    PyObject *ulong_item = ulong_data == NULL ? NULL : PyArray_GETITEM(ulong_arr, &ulong_data[0]);\n"
        "    if (ulong_item == NULL) {\n"
        "        Py_DECREF(ulong_arr_obj);\n"
        "        return NULL;\n"
        "    }\n"
        "    if (PyLong_AsUnsignedLong(ulong_item) == max_ulong && PyErr_Occurred() == NULL) score += 10000000000L;\n"
        "    Py_DECREF(ulong_item);\n"
        "    Py_DECREF(ulong_arr_obj);\n"
        "    PyObject *n0 = PyLong_FromLong(-1);\n"
        "    PyObject *n1 = PyLong_FromUnsignedLong(max_ulong);\n"
        "    if (n0 == NULL || n1 == NULL) {\n"
        "        Py_XDECREF(n0);\n"
        "        Py_XDECREF(n1);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *mixed_int_seq = PyTuple_Pack(2, n0, n1);\n"
        "    Py_DECREF(n0);\n"
        "    Py_DECREF(n1);\n"
        "    if (mixed_int_seq == NULL) return NULL;\n"
        "    PyObject *mixed_int_arr_obj = PyArray_FromAny(mixed_int_seq, NULL, 1, 1, 0, NULL);\n"
        "    Py_DECREF(mixed_int_seq);\n"
        "    if (mixed_int_arr_obj == NULL) return NULL;\n"
        "    PyArrayObject *mixed_int_arr = (PyArrayObject *)mixed_int_arr_obj;\n"
        "    PyArray_Descr *mixed_int_descr = PyArray_DESCR(mixed_int_arr);\n"
        "    PyObject **mixed_int_data = (PyObject **)PyArray_DATA(mixed_int_arr);\n"
        "    if (mixed_int_descr != NULL && mixed_int_descr->type_num == NPY_OBJECT) score += 100000000000L;\n"
        "    PyObject *mixed_neg_item = mixed_int_data == NULL ? NULL : PyArray_GETITEM(mixed_int_arr, &mixed_int_data[0]);\n"
        "    if (mixed_neg_item == NULL) {\n"
        "        Py_DECREF(mixed_int_arr_obj);\n"
        "        return NULL;\n"
        "    }\n"
        "    if (PyLong_AsLong(mixed_neg_item) == -1 && PyErr_Occurred() == NULL) score += 1000000000000L;\n"
        "    Py_DECREF(mixed_neg_item);\n"
        "    PyObject *mixed_uint_item = mixed_int_data == NULL ? NULL : PyArray_GETITEM(mixed_int_arr, &mixed_int_data[1]);\n"
        "    if (mixed_uint_item == NULL) {\n"
        "        Py_DECREF(mixed_int_arr_obj);\n"
        "        return NULL;\n"
        "    }\n"
        "    if (PyLong_AsUnsignedLong(mixed_uint_item) == max_ulong && PyErr_Occurred() == NULL) score += 10000000000000L;\n"
        "    Py_DECREF(mixed_uint_item);\n"
        "    Py_DECREF(mixed_int_arr_obj);\n"
        "    PyObject *z0 = PyComplex_FromDoubles(1.5, 2.5);\n"
        "    PyObject *z1 = PyLong_FromLong(3);\n"
        "    if (z0 == NULL || z1 == NULL) {\n"
        "        Py_XDECREF(z0);\n"
        "        Py_XDECREF(z1);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *complex_seq = PyTuple_Pack(2, z0, z1);\n"
        "    Py_DECREF(z0);\n"
        "    Py_DECREF(z1);\n"
        "    if (complex_seq == NULL) return NULL;\n"
        "    PyObject *complex_arr_obj = PyArray_FromAny(complex_seq, NULL, 1, 1, 0, NULL);\n"
        "    Py_DECREF(complex_seq);\n"
        "    if (complex_arr_obj == NULL) return NULL;\n"
        "    PyArrayObject *complex_arr = (PyArrayObject *)complex_arr_obj;\n"
        "    PyArray_Descr *complex_descr = PyArray_DESCR(complex_arr);\n"
        "    Py_complex *complex_data = (Py_complex *)PyArray_DATA(complex_arr);\n"
        "    if (complex_descr != NULL && complex_descr->type_num == NPY_CDOUBLE && complex_descr->elsize == (int)sizeof(Py_complex)) score += 100000000000000L;\n"
        "    if (complex_data != NULL && complex_data[0].real == 1.5 && complex_data[0].imag == 2.5 && complex_data[1].real == 3.0 && complex_data[1].imag == 0.0) score += 1000000000000000L;\n"
        "    PyObject *complex_item = complex_data == NULL ? NULL : PyArray_GETITEM(complex_arr, &complex_data[0]);\n"
        "    if (complex_item == NULL) {\n"
        "        Py_DECREF(complex_arr_obj);\n"
        "        return NULL;\n"
        "    }\n"
        "    if (PyComplex_RealAsDouble(complex_item) == 1.5 && PyComplex_ImagAsDouble(complex_item) == 2.5 && PyErr_Occurred() == NULL) score += 10000000000000000L;\n"
        "    Py_DECREF(complex_item);\n"
        "    Py_DECREF(complex_arr_obj);\n"
        "    PyObject *m0 = PyLong_FromLong(2);\n"
        "    PyObject *m1 = PyFloat_FromDouble(2.5);\n"
        "    if (m0 == NULL || m1 == NULL) {\n"
        "        Py_XDECREF(m0);\n"
        "        Py_XDECREF(m1);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *mixed_seq = PyTuple_Pack(2, m0, m1);\n"
        "    Py_DECREF(m0);\n"
        "    Py_DECREF(m1);\n"
        "    if (mixed_seq == NULL) return NULL;\n"
        "    PyObject *mixed_arr_obj = PyArray_FromAny(mixed_seq, NULL, 1, 1, 0, NULL);\n"
        "    Py_DECREF(mixed_seq);\n"
        "    if (mixed_arr_obj == NULL) return NULL;\n"
        "    PyArrayObject *mixed_arr = (PyArrayObject *)mixed_arr_obj;\n"
        "    PyArray_Descr *mixed_descr = PyArray_DESCR(mixed_arr);\n"
        "    double *mixed_data = (double *)PyArray_DATA(mixed_arr);\n"
        "    if (mixed_descr != NULL && mixed_descr->type_num == NPY_DOUBLE) score += 10000;\n"
        "    if (mixed_data != NULL && mixed_data[0] == 2.0 && mixed_data[1] == 2.5) score += 100000;\n"
        "    Py_DECREF(mixed_arr_obj);\n"
        '    PyObject *s0 = PyUnicode_FromString("auto");\n'
        '    PyObject *s1 = PyBytes_FromString("bytes");\n'
        "    if (s0 == NULL || s1 == NULL) {\n"
        "        Py_XDECREF(s0);\n"
        "        Py_XDECREF(s1);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *str_seq = PyTuple_Pack(2, s0, s1);\n"
        "    Py_DECREF(s0);\n"
        "    Py_DECREF(s1);\n"
        "    if (str_seq == NULL) return NULL;\n"
        "    PyObject *str_arr_obj = PyArray_FromAny(str_seq, NULL, 1, 1, 0, NULL);\n"
        "    Py_DECREF(str_seq);\n"
        "    if (str_arr_obj == NULL) return NULL;\n"
        "    PyArrayObject *str_arr = (PyArrayObject *)str_arr_obj;\n"
        "    PyArray_Descr *str_descr = PyArray_DESCR(str_arr);\n"
        "    char *str_data = (char *)PyArray_DATA(str_arr);\n"
        "    if (str_descr != NULL && str_descr->type_num == NPY_STRING && str_descr->elsize == 5 && PyArray_ITEMSIZE(str_arr) == 5) score += 1000000;\n"
        "    PyObject *str_item = str_data == NULL ? NULL : PyArray_GETITEM(str_arr, &str_data[0]);\n"
        "    if (str_item == NULL) {\n"
        "        Py_DECREF(str_arr_obj);\n"
        "        return NULL;\n"
        "    }\n"
        "    char *str_raw = NULL;\n"
        "    Py_ssize_t str_len = 0;\n"
        "    if (PyBytes_AsStringAndSize(str_item, &str_raw, &str_len) == 0 && str_len == 4 && str_raw[0] == 'a' && str_raw[1] == 'u' && str_raw[2] == 't' && str_raw[3] == 'o' && str_data[5] == 'b' && str_data[9] == 's') score += 10000000;\n"
        "    Py_DECREF(str_item);\n"
        "    Py_DECREF(str_arr_obj);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_descr_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    PyArray_Descr *descr = PyArray_DescrFromType(NPY_INT);\n"
        "    if (descr != NULL && descr->type_num == NPY_INT) score += 1;\n"
        "    if (descr != NULL && descr->elsize == (int)sizeof(int)) score += 10;\n"
        "    if (descr != NULL && descr->kind == 'i') score += 100;\n"
        "    if (descr != NULL && descr->type == 'i') score += 1000;\n"
        "    npy_intp dims[1] = {1};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_DOUBLE);\n"
        "    if (arr == NULL) return NULL;\n"
        "    PyArray_Descr *arr_descr = PyArray_DESCR((PyArrayObject *)arr);\n"
        "    if (arr_descr != NULL && arr_descr->type_num == NPY_DOUBLE) score += 10000;\n"
        "    if (arr_descr != NULL && arr_descr->elsize == (int)sizeof(double)) score += 100000;\n"
        "    if (arr_descr != NULL && arr_descr->kind == 'f' && arr_descr->type == 'd') score += 1000000;\n"
        "    Py_DECREF(arr);\n"
        "    PyArray_Descr *obj_descr = PyArray_DescrFromType(NPY_OBJECT);\n"
        "    if (obj_descr != NULL && obj_descr->type_num == NPY_OBJECT && obj_descr->kind == 'O' && obj_descr->type == 'O' && obj_descr->elsize == (int)sizeof(PyObject *)) score += 10000000;\n"
        "    PyArray_Descr *cfloat_descr = PyArray_DescrFromType(NPY_CFLOAT);\n"
        "    if (cfloat_descr != NULL && cfloat_descr->type_num == NPY_CFLOAT && cfloat_descr->kind == 'c' && cfloat_descr->type == 'F' && cfloat_descr->elsize == (int)sizeof(npy_cfloat)) score += 100000000L;\n"
        "    PyArray_Descr *clongdouble_descr = PyArray_DescrFromType(NPY_CLONGDOUBLE);\n"
        "    if (clongdouble_descr != NULL && clongdouble_descr->type_num == NPY_CLONGDOUBLE && clongdouble_descr->kind == 'c' && clongdouble_descr->type == 'G' && clongdouble_descr->elsize == (int)sizeof(npy_clongdouble)) score += 1000000000L;\n"
        "    npy_intp complex_dims[1] = {1};\n"
        "    PyObject *cfloat_arr_obj = PyArray_SimpleNew(1, complex_dims, NPY_CFLOAT);\n"
        "    PyObject *clongdouble_arr_obj = PyArray_SimpleNew(1, complex_dims, NPY_CLONGDOUBLE);\n"
        "    if (cfloat_arr_obj == NULL || clongdouble_arr_obj == NULL) {\n"
        "        Py_XDECREF(cfloat_arr_obj);\n"
        "        Py_XDECREF(clongdouble_arr_obj);\n"
        "        return NULL;\n"
        "    }\n"
        "    npy_cfloat *cfloat_data = (npy_cfloat *)PyArray_DATA((PyArrayObject *)cfloat_arr_obj);\n"
        "    npy_clongdouble *clongdouble_data = (npy_clongdouble *)PyArray_DATA((PyArrayObject *)clongdouble_arr_obj);\n"
        "    PyObject *cfloat_value = PyComplex_FromDoubles(1.25, -2.5);\n"
        "    PyObject *clongdouble_value = PyComplex_FromDoubles(4.5, -5.5);\n"
        "    if (cfloat_data == NULL || clongdouble_data == NULL || cfloat_value == NULL || clongdouble_value == NULL) {\n"
        "        Py_XDECREF(cfloat_value);\n"
        "        Py_XDECREF(clongdouble_value);\n"
        "        Py_DECREF(cfloat_arr_obj);\n"
        "        Py_DECREF(clongdouble_arr_obj);\n"
        "        return NULL;\n"
        "    }\n"
        "    if (PyArray_SETITEM((PyArrayObject *)cfloat_arr_obj, cfloat_data, cfloat_value) == 0 && cfloat_data[0].real == 1.25f && cfloat_data[0].imag == -2.5f) score += 10000000000L;\n"
        "    if (PyArray_SETITEM((PyArrayObject *)clongdouble_arr_obj, clongdouble_data, clongdouble_value) == 0 && clongdouble_data[0].real == (long double)4.5 && clongdouble_data[0].imag == (long double)-5.5) score += 100000000000L;\n"
        "    Py_DECREF(cfloat_value);\n"
        "    Py_DECREF(clongdouble_value);\n"
        "    PyObject *cfloat_item = PyArray_GETITEM((PyArrayObject *)cfloat_arr_obj, cfloat_data);\n"
        "    PyObject *clongdouble_item = PyArray_GETITEM((PyArrayObject *)clongdouble_arr_obj, clongdouble_data);\n"
        "    if (cfloat_item == NULL || clongdouble_item == NULL) {\n"
        "        Py_XDECREF(cfloat_item);\n"
        "        Py_XDECREF(clongdouble_item);\n"
        "        Py_DECREF(cfloat_arr_obj);\n"
        "        Py_DECREF(clongdouble_arr_obj);\n"
        "        return NULL;\n"
        "    }\n"
        "    if (PyComplex_RealAsDouble(cfloat_item) == 1.25 && PyComplex_ImagAsDouble(cfloat_item) == -2.5 && PyErr_Occurred() == NULL) score += 1000000000000L;\n"
        "    if (PyComplex_RealAsDouble(clongdouble_item) == 4.5 && PyComplex_ImagAsDouble(clongdouble_item) == -5.5 && PyErr_Occurred() == NULL) score += 10000000000000L;\n"
        "    Py_DECREF(cfloat_item);\n"
        "    Py_DECREF(clongdouble_item);\n"
        "    Py_DECREF(cfloat_arr_obj);\n"
        "    Py_DECREF(clongdouble_arr_obj);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_fromany_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[2] = {2, 2};\n"
        "    PyObject *arr = PyArray_SimpleNew(2, dims, NPY_INT);\n"
        "    if (arr == NULL) return NULL;\n"
        "    PyArray_Descr *int_descr = PyArray_DescrFromType(NPY_INT);\n"
        "    PyArray_Descr *double_descr = PyArray_DescrFromType(NPY_DOUBLE);\n"
        "    PyArray_Descr *object_descr = PyArray_DescrFromType(NPY_OBJECT);\n"
        "    PyObject *same = PyArray_FromAny(arr, int_descr, 1, 2, 0, NULL);\n"
        "    if (same == arr) score += 1;\n"
        "    Py_XDECREF(same);\n"
        "    same = PyArray_FromAny(arr, NULL, 0, 0, 0, NULL);\n"
        "    if (same == arr) score += 10;\n"
        "    Py_XDECREF(same);\n"
        "    int *arr_data = (int *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    if (arr_data == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    arr_data[3] = 33;\n"
        "    PyObject *casted = PyArray_FromAny(arr, double_descr, 0, 0, 0, NULL);\n"
        "    if (casted == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyArrayObject *casted_arr = (PyArrayObject *)casted;\n"
        "    npy_intp *casted_dims = PyArray_DIMS(casted_arr);\n"
        "    double *casted_data = (double *)PyArray_DATA(casted_arr);\n"
        "    if (casted != arr && PyArray_NDIM(casted_arr) == 2 && casted_dims != NULL && casted_dims[0] == 2 && casted_dims[1] == 2 && casted_data != NULL && casted_data[3] == 33.0) score += 100;\n"
        "    Py_DECREF(casted);\n"
        "    if (PyArray_FromAny(arr, int_descr, 3, 0, 0, NULL) == NULL && PyErr_ExceptionMatches(PyExc_ValueError)) score += 1000;\n"
        "    PyErr_Clear();\n"
        "    if (PyArray_FromAny(Py_None, int_descr, 0, 0, 0, NULL) == NULL && PyErr_ExceptionMatches(PyExc_NotImplementedError)) score += 10000;\n"
        "    PyErr_Clear();\n"
        "    PyObject *v0 = PyLong_FromLong(4);\n"
        "    PyObject *v1 = PyLong_FromLong(5);\n"
        "    PyObject *v2 = PyLong_FromLong(6);\n"
        "    if (v0 == NULL || v1 == NULL || v2 == NULL) {\n"
        "        Py_XDECREF(v0);\n"
        "        Py_XDECREF(v1);\n"
        "        Py_XDECREF(v2);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *seq = PyTuple_Pack(3, v0, v1, v2);\n"
        "    Py_DECREF(v0);\n"
        "    Py_DECREF(v1);\n"
        "    Py_DECREF(v2);\n"
        "    if (seq == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *coerced = PyArray_FromAny(seq, int_descr, 1, 1, 0, NULL);\n"
        "    Py_DECREF(seq);\n"
        "    if (coerced == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyArrayObject *coerced_arr = (PyArrayObject *)coerced;\n"
        "    npy_intp *coerced_dims = PyArray_DIMS(coerced_arr);\n"
        "    int *coerced_data = (int *)PyArray_DATA(coerced_arr);\n"
        "    if (PyArray_NDIM(coerced_arr) == 1 && coerced_dims != NULL && coerced_dims[0] == 3) score += 100000;\n"
        "    if (coerced_data == NULL) {\n"
        "        Py_DECREF(coerced);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    if (coerced_data != NULL && coerced_data[0] == 4 && coerced_data[1] == 5 && coerced_data[2] == 6) score += 1000000;\n"
        "    PyObject *coerced_item = PyArray_GETITEM(coerced_arr, &coerced_data[1]);\n"
        "    if (coerced_item == NULL) {\n"
        "        Py_DECREF(coerced);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    if (PyLong_AsLong(coerced_item) == 5 && PyErr_Occurred() == NULL) score += 10000000;\n"
        "    Py_DECREF(coerced_item);\n"
        "    Py_DECREF(coerced);\n"
        "    PyObject *m00 = PyLong_FromLong(11);\n"
        "    PyObject *m01 = PyLong_FromLong(12);\n"
        "    PyObject *m10 = PyLong_FromLong(21);\n"
        "    PyObject *m11 = PyLong_FromLong(22);\n"
        "    if (m00 == NULL || m01 == NULL || m10 == NULL || m11 == NULL) {\n"
        "        Py_XDECREF(m00);\n"
        "        Py_XDECREF(m01);\n"
        "        Py_XDECREF(m10);\n"
        "        Py_XDECREF(m11);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *row0 = PyTuple_Pack(2, m00, m01);\n"
        "    PyObject *row1 = PyTuple_Pack(2, m10, m11);\n"
        "    Py_DECREF(m00);\n"
        "    Py_DECREF(m01);\n"
        "    Py_DECREF(m10);\n"
        "    Py_DECREF(m11);\n"
        "    if (row0 == NULL || row1 == NULL) {\n"
        "        Py_XDECREF(row0);\n"
        "        Py_XDECREF(row1);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *matrix_seq = PyTuple_Pack(2, row0, row1);\n"
        "    Py_DECREF(row0);\n"
        "    Py_DECREF(row1);\n"
        "    if (matrix_seq == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *matrix = PyArray_FromAny(matrix_seq, int_descr, 2, 2, 0, NULL);\n"
        "    Py_DECREF(matrix_seq);\n"
        "    if (matrix == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyArrayObject *matrix_arr = (PyArrayObject *)matrix;\n"
        "    npy_intp *matrix_dims = PyArray_DIMS(matrix_arr);\n"
        "    int *matrix_data = (int *)PyArray_DATA(matrix_arr);\n"
        "    if (matrix_data == NULL) {\n"
        "        Py_DECREF(matrix);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    if (PyArray_NDIM(matrix_arr) == 2 && matrix_dims != NULL && matrix_dims[0] == 2 && matrix_dims[1] == 2 && matrix_data[0] == 11 && matrix_data[3] == 22) score += 100000000;\n"
        "    PyObject *matrix_item = PyArray_GETITEM(matrix_arr, &matrix_data[3]);\n"
        "    if (matrix_item == NULL) {\n"
        "        Py_DECREF(matrix);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    if (PyLong_AsLong(matrix_item) == 22 && PyErr_Occurred() == NULL) score += 1000000000;\n"
        "    Py_DECREF(matrix_item);\n"
        "    Py_DECREF(matrix);\n"
        "    PyObject *c0 = PyLong_FromLong(31);\n"
        "    PyObject *c1 = PyLong_FromLong(32);\n"
        "    PyObject *c2 = PyLong_FromLong(41);\n"
        "    PyObject *c3 = PyLong_FromLong(42);\n"
        "    if (c0 == NULL || c1 == NULL || c2 == NULL || c3 == NULL) {\n"
        "        Py_XDECREF(c0);\n"
        "        Py_XDECREF(c1);\n"
        "        Py_XDECREF(c2);\n"
        "        Py_XDECREF(c3);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *cube_row0 = PyTuple_Pack(2, c0, c1);\n"
        "    PyObject *cube_row1 = PyTuple_Pack(2, c2, c3);\n"
        "    Py_DECREF(c0);\n"
        "    Py_DECREF(c1);\n"
        "    Py_DECREF(c2);\n"
        "    Py_DECREF(c3);\n"
        "    if (cube_row0 == NULL || cube_row1 == NULL) {\n"
        "        Py_XDECREF(cube_row0);\n"
        "        Py_XDECREF(cube_row1);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *cube_plane0 = PyTuple_Pack(1, cube_row0);\n"
        "    PyObject *cube_plane1 = PyTuple_Pack(1, cube_row1);\n"
        "    Py_DECREF(cube_row0);\n"
        "    Py_DECREF(cube_row1);\n"
        "    if (cube_plane0 == NULL || cube_plane1 == NULL) {\n"
        "        Py_XDECREF(cube_plane0);\n"
        "        Py_XDECREF(cube_plane1);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *cube_seq = PyTuple_Pack(2, cube_plane0, cube_plane1);\n"
        "    Py_DECREF(cube_plane0);\n"
        "    Py_DECREF(cube_plane1);\n"
        "    if (cube_seq == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *cube = PyArray_FromAny(cube_seq, int_descr, 3, 3, 0, NULL);\n"
        "    Py_DECREF(cube_seq);\n"
        "    if (cube == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyArrayObject *cube_arr = (PyArrayObject *)cube;\n"
        "    npy_intp *cube_dims = PyArray_DIMS(cube_arr);\n"
        "    int *cube_data = (int *)PyArray_DATA(cube_arr);\n"
        "    if (cube_data == NULL) {\n"
        "        Py_DECREF(cube);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *cube_item = PyArray_GETITEM(cube_arr, &cube_data[3]);\n"
        "    if (cube_item == NULL) {\n"
        "        Py_DECREF(cube);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    if (PyArray_NDIM(cube_arr) == 3 && cube_dims != NULL && cube_dims[0] == 2 && cube_dims[1] == 1 && cube_dims[2] == 2 && cube_data[0] == 31 && cube_data[3] == 42 && PyLong_AsLong(cube_item) == 42 && PyErr_Occurred() == NULL) score += 10000000000L;\n"
        "    Py_DECREF(cube_item);\n"
        "    Py_DECREF(cube);\n"
        "    PyObject *obj0 = PyLong_FromLong(123);\n"
        "    PyObject *obj1 = PyLong_FromLong(456);\n"
        "    if (obj0 == NULL || obj1 == NULL) {\n"
        "        Py_XDECREF(obj0);\n"
        "        Py_XDECREF(obj1);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *obj_seq = PyTuple_Pack(2, obj0, obj1);\n"
        "    Py_DECREF(obj0);\n"
        "    Py_DECREF(obj1);\n"
        "    if (obj_seq == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *obj_arr_obj = PyArray_FromAny(obj_seq, object_descr, 1, 1, 0, NULL);\n"
        "    Py_DECREF(obj_seq);\n"
        "    if (obj_arr_obj == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyArrayObject *obj_arr = (PyArrayObject *)obj_arr_obj;\n"
        "    npy_intp *obj_dims = PyArray_DIMS(obj_arr);\n"
        "    PyObject **obj_data = (PyObject **)PyArray_DATA(obj_arr);\n"
        "    if (PyArray_NDIM(obj_arr) == 1 && obj_dims != NULL && obj_dims[0] == 2 && obj_data != NULL && obj_data[0] != NULL && obj_data[1] != NULL) score += 100000000000L;\n"
        "    PyObject *obj_item = obj_data == NULL ? NULL : PyArray_GETITEM(obj_arr, &obj_data[1]);\n"
        "    if (obj_item == NULL) {\n"
        "        Py_DECREF(obj_arr_obj);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    if (PyLong_AsLong(obj_item) == 456 && PyErr_Occurred() == NULL) score += 1000000000000L;\n"
        "    Py_DECREF(obj_item);\n"
        "    Py_DECREF(obj_arr_obj);\n"
        '    PyObject *str0 = PyUnicode_FromString("left");\n'
        '    PyObject *str1 = PyUnicode_FromString("right");\n'
        "    if (str0 == NULL || str1 == NULL) {\n"
        "        Py_XDECREF(str0);\n"
        "        Py_XDECREF(str1);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *str_seq = PyTuple_Pack(2, str0, str1);\n"
        "    Py_DECREF(str0);\n"
        "    Py_DECREF(str1);\n"
        "    if (str_seq == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *str_arr_obj = PyArray_FromAny(str_seq, object_descr, 1, 1, 0, NULL);\n"
        "    Py_DECREF(str_seq);\n"
        "    if (str_arr_obj == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyArrayObject *str_arr = (PyArrayObject *)str_arr_obj;\n"
        "    npy_intp *str_dims = PyArray_DIMS(str_arr);\n"
        "    PyObject **str_data = (PyObject **)PyArray_DATA(str_arr);\n"
        "    if (PyArray_NDIM(str_arr) == 1 && str_dims != NULL && str_dims[0] == 2 && str_data != NULL && str_data[0] != NULL && str_data[1] != NULL) score += 10000000000000L;\n"
        "    PyObject *str_item = str_data == NULL ? NULL : PyArray_GETITEM(str_arr, &str_data[1]);\n"
        "    if (str_item == NULL) {\n"
        "        Py_DECREF(str_arr_obj);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    const char *str_raw = PyUnicode_AsUTF8(str_item);\n"
        "    if (str_raw != NULL && str_raw[0] == 'r' && str_raw[1] == 'i' && str_raw[2] == 'g' && str_raw[3] == 'h' && str_raw[4] == 't' && str_raw[5] == '\\0') score += 100000000000000L;\n"
        "    Py_DECREF(str_item);\n"
        "    Py_DECREF(str_arr_obj);\n"
        "    PyObject *rg0 = PyLong_FromLong(7);\n"
        "    PyObject *rg1 = PyLong_FromLong(8);\n"
        "    PyObject *rg2 = PyLong_FromLong(9);\n"
        "    if (rg0 == NULL || rg1 == NULL || rg2 == NULL) {\n"
        "        Py_XDECREF(rg0);\n"
        "        Py_XDECREF(rg1);\n"
        "        Py_XDECREF(rg2);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *rag_row0 = PyTuple_Pack(2, rg0, rg1);\n"
        "    PyObject *rag_row1 = PyTuple_Pack(1, rg2);\n"
        "    Py_DECREF(rg0);\n"
        "    Py_DECREF(rg1);\n"
        "    Py_DECREF(rg2);\n"
        "    if (rag_row0 == NULL || rag_row1 == NULL) {\n"
        "        Py_XDECREF(rag_row0);\n"
        "        Py_XDECREF(rag_row1);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *rag_seq = PyTuple_Pack(2, rag_row0, rag_row1);\n"
        "    Py_DECREF(rag_row0);\n"
        "    Py_DECREF(rag_row1);\n"
        "    if (rag_seq == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *rag_arr_obj = PyArray_FromAny(rag_seq, object_descr, 1, 1, 0, NULL);\n"
        "    Py_DECREF(rag_seq);\n"
        "    if (rag_arr_obj == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyArrayObject *rag_arr = (PyArrayObject *)rag_arr_obj;\n"
        "    npy_intp *rag_dims = PyArray_DIMS(rag_arr);\n"
        "    PyObject **rag_data = (PyObject **)PyArray_DATA(rag_arr);\n"
        "    if (PyArray_NDIM(rag_arr) == 1 && rag_dims != NULL && rag_dims[0] == 2 && rag_data != NULL && rag_data[0] != NULL && rag_data[1] != NULL) score += 1000000000000000L;\n"
        "    PyObject *rag_item = rag_data == NULL ? NULL : PyArray_GETITEM(rag_arr, &rag_data[1]);\n"
        "    if (rag_item == NULL) {\n"
        "        Py_DECREF(rag_arr_obj);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    if (PySequence_Size(rag_item) == 1 && PyErr_Occurred() == NULL) score += 10000000000000000L;\n"
        "    Py_DECREF(rag_item);\n"
        "    Py_DECREF(rag_arr_obj);\n"
        "    static char mv_raw[3] = {1, 2, 3};\n"
        "    PyObject *mv = PyMemoryView_FromMemory(mv_raw, 3, PyBUF_READ);\n"
        "    if (mv == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *mv_seq = PyTuple_Pack(1, mv);\n"
        "    Py_DECREF(mv);\n"
        "    if (mv_seq == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *mv_arr_obj = PyArray_FromAny(mv_seq, object_descr, 1, 1, 0, NULL);\n"
        "    Py_DECREF(mv_seq);\n"
        "    if (mv_arr_obj == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyArrayObject *mv_arr = (PyArrayObject *)mv_arr_obj;\n"
        "    npy_intp *mv_dims = PyArray_DIMS(mv_arr);\n"
        "    PyObject **mv_data = (PyObject **)PyArray_DATA(mv_arr);\n"
        "    if (PyArray_NDIM(mv_arr) == 1 && mv_dims != NULL && mv_dims[0] == 1 && mv_data != NULL && mv_data[0] != NULL) score += 100000000000000000L;\n"
        "    PyObject *mv_item = mv_data == NULL ? NULL : PyArray_GETITEM(mv_arr, &mv_data[0]);\n"
        "    if (mv_item == NULL) {\n"
        "        Py_DECREF(mv_arr_obj);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    if (PyMemoryView_Check(mv_item) && PySequence_Size(mv_item) == 3 && PyErr_Occurred() == NULL) score += 1000000000000000000L;\n"
        "    Py_DECREF(mv_item);\n"
        "    Py_DECREF(mv_arr_obj);\n"
        "    Py_DECREF(arr);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_array_metadata_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    npy_intp dims[2] = {2, 3};\n"
        "    PyObject *owned = PyArray_SimpleNew(2, dims, NPY_INT);\n"
        "    if (owned == NULL) return NULL;\n"
        "    PyObject *value = NULL;\n"
        "    PyArrayObject *arr = (PyArrayObject *)owned;\n"
        "    long score = 0;\n"
        "    npy_intp *owned_dims = PyArray_DIMS(arr);\n"
        "    npy_intp *owned_strides = PyArray_STRIDES(arr);\n"
        "    int *owned_data = (int *)PyArray_DATA(arr);\n"
        "    if (PyArray_NDIM(arr) == 2) score += 1;\n"
        "    if (owned_dims != NULL && owned_dims[0] == 2 && owned_dims[1] == 3) score += 10;\n"
        "    if (PyArray_DIM(arr, 1) == 3) score += 10000000000L;\n"
        "    if (owned_strides != NULL && owned_strides[0] == 3 * (npy_intp)sizeof(int) && owned_strides[1] == (npy_intp)sizeof(int)) score += 100;\n"
        "    if (PyArray_SIZE(arr) == 6) score += 100000000000L;\n"
        "    if (PyArray_ITEMSIZE(arr) == (int)sizeof(int)) score += 1000000000000L;\n"
        "    if (PyArray_TYPE(arr) == NPY_INT) score += 1000000000000000L;\n"
        "    if (PyArray_NBYTES(arr) == 6 * (npy_intp)sizeof(int)) score += 10000000000000000L;\n"
        "    if ((PyArray_FLAGS(arr) & NPY_ARRAY_OWNDATA) && PyArray_CHKFLAGS(arr, NPY_ARRAY_CARRAY)) score += 100000000000000000L;\n"
        "    if (PyArray_ISCONTIGUOUS(arr) && PyArray_IS_C_CONTIGUOUS(arr) && PyArray_ISCARRAY(arr) && PyArray_ISALIGNED(arr) && PyArray_ISWRITEABLE(arr)) score += 1000000000000000000L;\n"
        "    if (PyArray_Check(owned) && PyArray_CheckExact(owned) && !PyArray_Check(Py_None)) score += 10000000000000L;\n"
        "    if (owned_data != NULL) {\n"
        "        owned_data[5] = 41;\n"
        "        if (((int *)PyArray_DATA(arr))[5] == 41) score += 1000;\n"
        "        if (PyArray_BYTES(arr) == owned_data) score += 100000000000000L;\n"
        "        value = PyArray_GETITEM(arr, &owned_data[5]);\n"
        "        if (value == NULL) {\n"
        "            Py_DECREF(owned);\n"
        "            return NULL;\n"
        "        }\n"
        "        if (PyLong_AsLong(value) == 41) score += 1000000;\n"
        "        Py_DECREF(value);\n"
        "        value = PyLong_FromLong(77);\n"
        "        if (value == NULL) {\n"
        "            Py_DECREF(owned);\n"
        "            return NULL;\n"
        "        }\n"
        "        if (PyArray_SETITEM(arr, &owned_data[0], value) == 0 && owned_data[0] == 77) score += 10000000;\n"
        "        Py_DECREF(value);\n"
        "        if (PyErr_Occurred() != NULL) {\n"
        "            Py_DECREF(owned);\n"
        "            return NULL;\n"
        "        }\n"
        "    }\n"
        "    int raw[6] = {1, 2, 3, 4, 5, 6};\n"
        "    PyObject *borrowed = PyArray_SimpleNewFromData(2, dims, NPY_INT, raw);\n"
        "    if (borrowed == NULL) {\n"
        "        Py_DECREF(owned);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyArrayObject *borrowed_arr = (PyArrayObject *)borrowed;\n"
        "    if (PyArray_DATA(borrowed_arr) == raw) score += 10000;\n"
        "    if (((int *)PyArray_DATA(borrowed_arr))[4] == 5) score += 100000;\n"
        "    value = PyLong_FromLong(99);\n"
        "    if (value == NULL) {\n"
        "        Py_DECREF(borrowed);\n"
        "        Py_DECREF(owned);\n"
        "        return NULL;\n"
        "    }\n"
        "    if (PyArray_SETITEM(borrowed_arr, &raw[2], value) == 0 && raw[2] == 99) score += 100000000;\n"
        "    Py_DECREF(value);\n"
        "    if (PyErr_Occurred() != NULL) {\n"
        "        Py_DECREF(borrowed);\n"
        "        Py_DECREF(owned);\n"
        "        return NULL;\n"
        "    }\n"
        "    value = PyArray_GETITEM(borrowed_arr, &raw[2]);\n"
        "    if (value == NULL) {\n"
        "        Py_DECREF(borrowed);\n"
        "        Py_DECREF(owned);\n"
        "        return NULL;\n"
        "    }\n"
        "    if (PyLong_AsLong(value) == 99) score += 1000000000;\n"
        "    Py_DECREF(value);\n"
        "    Py_DECREF(borrowed);\n"
        "    Py_DECREF(owned);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_pointer_metadata_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    npy_intp dims[2] = {2, 3};\n"
        "    PyObject *owned = PyArray_SimpleNew(2, dims, NPY_INT);\n"
        "    if (owned == NULL) return NULL;\n"
        "    PyArrayObject *arr = (PyArrayObject *)owned;\n"
        "    int *owned_data = (int *)PyArray_DATA(arr);\n"
        "    long score = 0;\n"
        "    if (PyArray_STRIDE(arr, 0) == 3 * (npy_intp)sizeof(int)) score += 1;\n"
        "    if (PyArray_STRIDE(arr, 1) == (npy_intp)sizeof(int)) score += 10;\n"
        "    if (owned_data != NULL && PyArray_GETPTR1(arr, 5) == &owned_data[5]) score += 100;\n"
        "    if (owned_data != NULL && PyArray_GETPTR2(arr, 1, 2) == &owned_data[5]) score += 1000;\n"
        "    if (owned_data != NULL && PyArray_GETPTR2(arr, 0, 0) == &owned_data[0]) score += 10000;\n"
        "    Py_DECREF(owned);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_dtype_field_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    npy_intp dims[1] = {4};\n"
        "    PyObject *owned = PyArray_SimpleNew(1, dims, NPY_DOUBLE);\n"
        "    if (owned == NULL) return NULL;\n"
        "    PyArrayObject *arr = (PyArrayObject *)owned;\n"
        "    PyArray_Descr *descr = PyArray_DTYPE(arr);\n"
        "    long score = 0;\n"
        "    if (descr != NULL && descr == PyArray_DESCR(arr)) score += 1;\n"
        "    if (descr != NULL && PyDataType_TYPE(descr) == NPY_DOUBLE) score += 10;\n"
        "    if (descr != NULL && PyDataType_KIND(descr) == 'f') score += 100;\n"
        "    if (descr != NULL && PyDataType_ELSIZE(descr) == (int)sizeof(double)) score += 1000;\n"
        "    if (descr != NULL && PyDataType_ALIGNMENT(descr) == (int)sizeof(double)) score += 10000;\n"
        "    Py_DECREF(owned);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_dtype_classification_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    npy_intp dims[1] = {1};\n"
        "    PyObject *double_obj = PyArray_SimpleNew(1, dims, NPY_DOUBLE);\n"
        "    PyObject *int_obj = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    PyObject *obj_obj = PyArray_SimpleNew(1, dims, NPY_OBJECT);\n"
        "    PyArray_Descr *cdouble_descr = PyArray_DescrFromType(NPY_CDOUBLE);\n"
        "    PyArray_Descr *string_descr = PyArray_DescrFromType(NPY_STRING);\n"
        "    PyArray_Descr *bool_descr = PyArray_DescrFromType(NPY_BOOL);\n"
        "    if (double_obj == NULL || int_obj == NULL || obj_obj == NULL || cdouble_descr == NULL || string_descr == NULL || bool_descr == NULL) {\n"
        "        Py_XDECREF(double_obj);\n"
        "        Py_XDECREF(int_obj);\n"
        "        Py_XDECREF(obj_obj);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyArrayObject *double_arr = (PyArrayObject *)double_obj;\n"
        "    PyArrayObject *int_arr = (PyArrayObject *)int_obj;\n"
        "    PyArrayObject *obj_arr = (PyArrayObject *)obj_obj;\n"
        "    PyArray_Descr *double_descr = PyArray_DTYPE(double_arr);\n"
        "    PyArray_Descr *int_descr = PyArray_DTYPE(int_arr);\n"
        "    PyArray_Descr *obj_descr = PyArray_DTYPE(obj_arr);\n"
        "    long score = 0;\n"
        "    if (PyTypeNum_ISFLOAT(NPY_DOUBLE) && PyDataType_ISFLOAT(double_descr) && PyArray_ISFLOAT(double_arr)) score += 1;\n"
        "    if (PyTypeNum_ISNUMBER(NPY_DOUBLE) && PyDataType_ISNUMBER(double_descr) && PyArray_ISNUMBER(double_arr)) score += 10;\n"
        "    if (PyTypeNum_ISINTEGER(NPY_INT) && PyDataType_ISINTEGER(int_descr) && PyArray_ISINTEGER(int_arr)) score += 100;\n"
        "    if (PyTypeNum_ISSIGNED(NPY_INT) && PyDataType_ISSIGNED(int_descr) && PyArray_ISSIGNED(int_arr)) score += 1000;\n"
        "    if (PyTypeNum_ISUNSIGNED(NPY_UINT) && !PyDataType_ISUNSIGNED(int_descr) && !PyArray_ISUNSIGNED(int_arr)) score += 10000;\n"
        "    if (PyTypeNum_ISCOMPLEX(NPY_CDOUBLE) && PyDataType_ISCOMPLEX(cdouble_descr)) score += 100000;\n"
        "    if (PyTypeNum_ISOBJECT(NPY_OBJECT) && PyDataType_ISOBJECT(obj_descr) && PyArray_ISOBJECT(obj_arr)) score += 1000000;\n"
        "    if (PyTypeNum_ISSTRING(NPY_STRING) && PyDataType_ISSTRING(string_descr) && PyTypeNum_ISFLEXIBLE(NPY_STRING)) score += 10000000;\n"
        "    if (PyTypeNum_ISBOOL(NPY_BOOL) && PyDataType_ISBOOL(bool_descr)) score += 100000000;\n"
        "    Py_DECREF(double_obj);\n"
        "    Py_DECREF(int_obj);\n"
        "    Py_DECREF(obj_obj);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_scalar_kind_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    npy_intp dims[1] = {1};\n"
        "    PyObject *pos_obj = PyArray_SimpleNew(1, dims, NPY_LONG);\n"
        "    PyObject *neg_obj = PyArray_SimpleNew(1, dims, NPY_LONG);\n"
        "    if (pos_obj == NULL || neg_obj == NULL) {\n"
        "        Py_XDECREF(pos_obj);\n"
        "        Py_XDECREF(neg_obj);\n"
        "        return NULL;\n"
        "    }\n"
        "    long *pos_data = (long *)PyArray_DATA((PyArrayObject *)pos_obj);\n"
        "    long *neg_data = (long *)PyArray_DATA((PyArrayObject *)neg_obj);\n"
        "    pos_data[0] = 7;\n"
        "    neg_data[0] = -3;\n"
        "    PyArrayObject *pos_arr = (PyArrayObject *)pos_obj;\n"
        "    PyArrayObject *neg_arr = (PyArrayObject *)neg_obj;\n"
        "    long score = 0;\n"
        "    if (PyArray_ScalarKind(NPY_BOOL, NULL) == NPY_BOOL_SCALAR) score += 1;\n"
        "    if (PyArray_ScalarKind(NPY_LONG, NULL) == NPY_INTPOS_SCALAR) score += 10;\n"
        "    if (PyArray_ScalarKind(NPY_LONG, &pos_arr) == NPY_INTPOS_SCALAR) score += 100;\n"
        "    if (PyArray_ScalarKind(NPY_LONG, &neg_arr) == NPY_INTNEG_SCALAR) score += 1000;\n"
        "    if (PyArray_ScalarKind(NPY_DOUBLE, NULL) == NPY_FLOAT_SCALAR) score += 10000;\n"
        "    if (PyArray_ScalarKind(NPY_CDOUBLE, NULL) == NPY_COMPLEX_SCALAR) score += 100000;\n"
        "    if (PyArray_ScalarKind(NPY_OBJECT, NULL) == NPY_OBJECT_SCALAR) score += 1000000;\n"
        "    if (PyArray_ScalarKind(9999, NULL) == NPY_NOSCALAR) score += 10000000;\n"
        "    Py_DECREF(neg_obj);\n"
        "    Py_DECREF(pos_obj);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_check_any_scalar_exact_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    PyObject *py_int = PyLong_FromLong(7);\n"
        "    PyObject *py_float = PyFloat_FromDouble(2.5);\n"
        "    npy_intp dims[1] = {1};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_LONG);\n"
        "    PyObject *zero_dim = PyArray_SimpleNew(0, NULL, NPY_LONG);\n"
        "    if (py_int == NULL || py_float == NULL || arr == NULL || zero_dim == NULL) {\n"
        "        Py_XDECREF(zero_dim);\n"
        "        Py_XDECREF(arr);\n"
        "        Py_XDECREF(py_float);\n"
        "        Py_XDECREF(py_int);\n"
        "        return NULL;\n"
        "    }\n"
        "    if (PyArray_CheckAnyScalarExact(py_int) == 0 && PyErr_Occurred() == NULL) score += 1;\n"
        "    if (PyArray_CheckAnyScalarExact(py_float) == 0 && PyErr_Occurred() == NULL) score += 10;\n"
        "    if (PyArray_CheckAnyScalarExact(arr) == 0 && PyErr_Occurred() == NULL) score += 100;\n"
        "    if (PyArray_CheckAnyScalarExact(zero_dim) == 0 && PyErr_Occurred() == NULL) score += 1000;\n"
        "    if (PyArray_CheckAnyScalarExact(NULL) == 0 && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    Py_DECREF(zero_dim);\n"
        "    Py_DECREF(arr);\n"
        "    Py_DECREF(py_float);\n"
        "    Py_DECREF(py_int);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_can_coerce_scalar_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    if (PyArray_CanCoerceScalar(NPY_INT, NPY_LONG, NPY_INTPOS_SCALAR)) score += 1;\n"
        "    if (PyArray_CanCoerceScalar(NPY_INT, NPY_LONG, NPY_INTNEG_SCALAR)) score += 10;\n"
        "    if (!PyArray_CanCoerceScalar(NPY_INT, NPY_UINT, NPY_INTNEG_SCALAR)) score += 100;\n"
        "    if (PyArray_CanCoerceScalar(NPY_INT, NPY_DOUBLE, NPY_NOSCALAR)) score += 1000;\n"
        "    if (!PyArray_CanCoerceScalar(NPY_DOUBLE, NPY_INT, NPY_NOSCALAR)) score += 10000;\n"
        "    if (!PyArray_CanCoerceScalar(NPY_OBJECT, NPY_DOUBLE, NPY_OBJECT_SCALAR)) score += 100000;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_can_cast_scalar_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    PyTypeObject *bool_type = (PyTypeObject *)PyArray_TypeObjectFromType(NPY_BOOL);\n"
        "    PyTypeObject *long_type = (PyTypeObject *)PyArray_TypeObjectFromType(NPY_LONG);\n"
        "    PyTypeObject *double_type = (PyTypeObject *)PyArray_TypeObjectFromType(NPY_DOUBLE);\n"
        "    PyTypeObject *complex_type = (PyTypeObject *)PyArray_TypeObjectFromType(NPY_CDOUBLE);\n"
        "    PyTypeObject *string_type = (PyTypeObject *)PyArray_TypeObjectFromType(NPY_STRING);\n"
        "    PyTypeObject *object_type = (PyTypeObject *)PyArray_TypeObjectFromType(NPY_OBJECT);\n"
        "    if (bool_type == NULL || long_type == NULL || double_type == NULL ||\n"
        "        complex_type == NULL || string_type == NULL || object_type == NULL) {\n"
        "        Py_XDECREF((PyObject *)object_type);\n"
        "        Py_XDECREF((PyObject *)string_type);\n"
        "        Py_XDECREF((PyObject *)complex_type);\n"
        "        Py_XDECREF((PyObject *)double_type);\n"
        "        Py_XDECREF((PyObject *)long_type);\n"
        "        Py_XDECREF((PyObject *)bool_type);\n"
        "        return NULL;\n"
        "    }\n"
        "    long score = 0;\n"
        "    if (PyArray_CanCastScalar(bool_type, long_type)) score += 1;\n"
        "    if (!PyArray_CanCastScalar(long_type, double_type)) score += 10;\n"
        "    if (PyArray_CanCastScalar(double_type, complex_type)) score += 100;\n"
        "    if (!PyArray_CanCastScalar(complex_type, double_type)) score += 1000;\n"
        "    if (PyArray_CanCastScalar(string_type, object_type)) score += 10000;\n"
        "    if (!PyArray_CanCastScalar(&PyList_Type, double_type) && PyErr_Occurred() == NULL) score += 100000;\n"
        "    Py_DECREF((PyObject *)object_type);\n"
        "    Py_DECREF((PyObject *)string_type);\n"
        "    Py_DECREF((PyObject *)complex_type);\n"
        "    Py_DECREF((PyObject *)double_type);\n"
        "    Py_DECREF((PyObject *)long_type);\n"
        "    Py_DECREF((PyObject *)bool_type);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_convert_common_type_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    npy_intp dims[1] = {2};\n"
        "    PyObject *int_obj = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    PyObject *double_obj = PyArray_SimpleNew(1, dims, NPY_DOUBLE);\n"
        "    PyObject *seq = NULL;\n"
        "    PyObject *empty = NULL;\n"
        "    PyArrayObject **converted = NULL;\n"
        "    PyArrayObject **empty_converted = NULL;\n"
        "    if (int_obj == NULL || double_obj == NULL) goto fail;\n"
        "    int *int_data = (int *)PyArray_DATA((PyArrayObject *)int_obj);\n"
        "    double *double_data = (double *)PyArray_DATA((PyArrayObject *)double_obj);\n"
        "    int_data[0] = 1;\n"
        "    int_data[1] = 2;\n"
        "    double_data[0] = 1.5;\n"
        "    double_data[1] = 2.5;\n"
        "    seq = PyTuple_Pack(2, int_obj, double_obj);\n"
        "    if (seq == NULL) goto fail;\n"
        "    int n = -1;\n"
        "    converted = PyArray_ConvertToCommonType(seq, &n);\n"
        "    if (converted == NULL) goto fail;\n"
        "    long score = 0;\n"
        "    if (n == 2 && converted[0] != NULL && converted[1] != NULL) score += 1;\n"
        "    if (PyArray_TYPE(converted[0]) == NPY_DOUBLE && PyArray_TYPE(converted[1]) == NPY_DOUBLE) score += 10;\n"
        "    if (PyArray_ISCARRAY(converted[0]) && PyArray_ISCARRAY(converted[1])) score += 100;\n"
        "    double *conv0 = (double *)PyArray_DATA(converted[0]);\n"
        "    double *conv1 = (double *)PyArray_DATA(converted[1]);\n"
        "    if (conv0[0] == 1.0 && conv0[1] == 2.0) score += 1000;\n"
        "    if (conv1[0] == 1.5 && conv1[1] == 2.5) score += 10000;\n"
        "    empty = PyTuple_New(0);\n"
        "    if (empty == NULL) goto fail;\n"
        "    int empty_n = 123;\n"
        "    empty_converted = PyArray_ConvertToCommonType(empty, &empty_n);\n"
        "    if (empty_converted == NULL && empty_n == 0 && PyErr_ExceptionMatches(PyExc_ValueError)) {\n"
        "        score += 100000;\n"
        "        PyErr_Clear();\n"
        "    } else {\n"
        "        if (empty_converted != NULL) PyDataMem_FREE(empty_converted);\n"
        '        PyErr_SetString(PyExc_AssertionError, "empty ConvertToCommonType should fail");\n'
        "        goto fail;\n"
        "    }\n"
        "    Py_DECREF(converted[0]);\n"
        "    Py_DECREF(converted[1]);\n"
        "    PyDataMem_FREE(converted);\n"
        "    Py_DECREF(empty);\n"
        "    Py_DECREF(seq);\n"
        "    Py_DECREF(double_obj);\n"
        "    Py_DECREF(int_obj);\n"
        "    return PyLong_FromLong(score);\n"
        "fail:\n"
        "    if (empty_converted != NULL) PyDataMem_FREE(empty_converted);\n"
        "    if (converted != NULL) {\n"
        "        if (converted[0] != NULL) Py_DECREF(converted[0]);\n"
        "        if (converted[1] != NULL) Py_DECREF(converted[1]);\n"
        "        PyDataMem_FREE(converted);\n"
        "    }\n"
        "    Py_XDECREF(empty);\n"
        "    Py_XDECREF(seq);\n"
        "    Py_XDECREF(double_obj);\n"
        "    Py_XDECREF(int_obj);\n"
        "    return NULL;\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_state_byteorder_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    npy_intp two_dims[2] = {2, 3};\n"
        "    npy_intp one_dim[1] = {3};\n"
        "    PyObject *two_obj = PyArray_SimpleNew(2, two_dims, NPY_DOUBLE);\n"
        "    PyObject *one_obj = PyArray_SimpleNew(1, one_dim, NPY_DOUBLE);\n"
        '    PyObject *s0 = PyUnicode_FromString("abc");\n'
        '    PyObject *s1 = PyBytes_FromString("de");\n'
        "    if (two_obj == NULL || one_obj == NULL || s0 == NULL || s1 == NULL) {\n"
        "        Py_XDECREF(two_obj);\n"
        "        Py_XDECREF(one_obj);\n"
        "        Py_XDECREF(s0);\n"
        "        Py_XDECREF(s1);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *str_seq = PyTuple_Pack(2, s0, s1);\n"
        "    Py_DECREF(s0);\n"
        "    Py_DECREF(s1);\n"
        "    if (str_seq == NULL) {\n"
        "        Py_DECREF(two_obj);\n"
        "        Py_DECREF(one_obj);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *str_obj = PyArray_FromAny(str_seq, NULL, 1, 1, 0, NULL);\n"
        "    Py_DECREF(str_seq);\n"
        "    if (str_obj == NULL) {\n"
        "        Py_DECREF(two_obj);\n"
        "        Py_DECREF(one_obj);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyArrayObject *two_arr = (PyArrayObject *)two_obj;\n"
        "    PyArrayObject *one_arr = (PyArrayObject *)one_obj;\n"
        "    PyArrayObject *str_arr = (PyArrayObject *)str_obj;\n"
        "    PyArray_Descr *two_descr = PyArray_DESCR(two_arr);\n"
        "    PyArray_Descr *str_descr = PyArray_DESCR(str_arr);\n"
        "    long score = 0;\n"
        "    if (PyArray_ISONESEGMENT(two_arr)) score += 1;\n"
        "    if (!PyArray_ISFORTRAN(two_arr) && PyArray_FORTRAN_IF(two_arr) == 0) score += 10;\n"
        "    if (PyArray_SAFEALIGNEDCOPY(two_arr)) score += 100;\n"
        "    if (PyArray_IS_F_CONTIGUOUS(one_arr) && PyArray_ISFARRAY(one_arr) && PyArray_ISFARRAY_RO(one_arr)) score += 1000;\n"
        "    if (PyArray_ISCARRAY_RO(two_arr) && PyArray_ISBEHAVED(two_arr) && PyArray_ISBEHAVED_RO(two_arr)) score += 10000;\n"
        "    if (PyArray_ISNBO(two_descr->byteorder) && PyArray_IsNativeByteOrder('=')) score += 100000;\n"
        "    if (PyArray_ISNOTSWAPPED(two_arr) && !PyArray_ISBYTESWAPPED(two_arr)) score += 1000000;\n"
        "    if (PyDataType_ISNOTSWAPPED(two_descr) && !PyDataType_ISBYTESWAPPED(two_descr)) score += 10000000;\n"
        "    if (PyArray_ISVARIABLE(str_arr) && !PyArray_SAFEALIGNEDCOPY(str_arr)) score += 100000000;\n"
        "    if (PyDataType_ISNOTSWAPPED(str_descr) && !PyDataType_ISBYTESWAPPED(str_descr)) score += 1000000000L;\n"
        "    Py_DECREF(str_obj);\n"
        "    Py_DECREF(two_obj);\n"
        "    Py_DECREF(one_obj);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_from_macro_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    PyObject *v0 = PyLong_FromLong(4);\n"
        "    PyObject *v1 = PyLong_FromLong(5);\n"
        "    PyObject *v2 = PyLong_FromLong(6);\n"
        "    if (v0 == NULL || v1 == NULL || v2 == NULL) {\n"
        "        Py_XDECREF(v0);\n"
        "        Py_XDECREF(v1);\n"
        "        Py_XDECREF(v2);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *seq = PyTuple_Pack(3, v0, v1, v2);\n"
        "    Py_DECREF(v0);\n"
        "    Py_DECREF(v1);\n"
        "    Py_DECREF(v2);\n"
        "    if (seq == NULL) return NULL;\n"
        "    long score = 0;\n"
        "    PyObject *from_o = PyArray_FROM_O(seq);\n"
        "    if (from_o != NULL && PyArray_NDIM((PyArrayObject *)from_o) == 1 && PyArray_TYPE((PyArrayObject *)from_o) == NPY_LONG) score += 1;\n"
        "    Py_XDECREF(from_o);\n"
        "    PyObject *from_of = PyArray_FROM_OF(seq, NPY_ARRAY_ENSUREARRAY);\n"
        "    if (from_of != NULL && PyArray_NDIM((PyArrayObject *)from_of) == 1 && PyArray_TYPE((PyArrayObject *)from_of) == NPY_LONG) score += 10;\n"
        "    Py_XDECREF(from_of);\n"
        "    PyObject *from_ot = PyArray_FROM_OT(seq, NPY_DOUBLE);\n"
        "    if (from_ot != NULL && PyArray_TYPE((PyArrayObject *)from_ot) == NPY_DOUBLE) score += 100;\n"
        "    Py_XDECREF(from_ot);\n"
        "    PyObject *from_otf = PyArray_FROM_OTF(seq, NPY_INT, NPY_ARRAY_ENSURECOPY);\n"
        "    if (from_otf != NULL && PyArray_TYPE((PyArrayObject *)from_otf) == NPY_INT) score += 1000;\n"
        "    Py_XDECREF(from_otf);\n"
        "    PyObject *fromany = PyArray_FROMANY(seq, NPY_INT, 1, 1, 0);\n"
        "    if (fromany != NULL && PyArray_NDIM((PyArrayObject *)fromany) == 1 && PyArray_SIZE((PyArrayObject *)fromany) == 3) score += 10000;\n"
        "    Py_XDECREF(fromany);\n"
        "    PyObject *contig_any = PyArray_ContiguousFromAny(seq, NPY_DOUBLE, 1, 1);\n"
        "    if (contig_any != NULL && PyArray_TYPE((PyArrayObject *)contig_any) == NPY_DOUBLE && PyArray_ISCARRAY((PyArrayObject *)contig_any)) score += 100000;\n"
        "    Py_XDECREF(contig_any);\n"
        "    PyObject *from_object = PyArray_FromObject(seq, NPY_INT, 1, 1);\n"
        "    if (from_object != NULL && PyArray_TYPE((PyArrayObject *)from_object) == NPY_INT) score += 1000000;\n"
        "    Py_XDECREF(from_object);\n"
        "    PyObject *contig_object = PyArray_ContiguousFromObject(seq, NPY_DOUBLE, 1, 1);\n"
        "    if (contig_object != NULL && PyArray_TYPE((PyArrayObject *)contig_object) == NPY_DOUBLE) score += 10000000;\n"
        "    Py_XDECREF(contig_object);\n"
        "    PyObject *copy_object = PyArray_CopyFromObject(seq, NPY_INT, 1, 1);\n"
        "    if (copy_object != NULL && PyArray_TYPE((PyArrayObject *)copy_object) == NPY_INT) score += 100000000;\n"
        "    Py_XDECREF(copy_object);\n"
        "    PyObject *check_any = PyArray_CheckFromAny(seq, PyArray_DescrFromType(NPY_DOUBLE), 1, 1, NPY_ARRAY_ALIGNED, NULL);\n"
        "    if (check_any != NULL && PyArray_TYPE((PyArrayObject *)check_any) == NPY_DOUBLE && PyArray_NDIM((PyArrayObject *)check_any) == 1) score += 1000000000L;\n"
        "    Py_XDECREF(check_any);\n"
        "    Py_DECREF(seq);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_fill_byteorder_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    npy_intp dims[1] = {4};\n"
        "    PyObject *arr_obj = PyArray_SimpleNew(1, dims, NPY_BYTE);\n"
        "    if (arr_obj == NULL) return NULL;\n"
        "    PyArrayObject *arr = (PyArrayObject *)arr_obj;\n"
        "    signed char *data = (signed char *)PyArray_DATA(arr);\n"
        "    PyArray_Descr *descr = PyArray_DESCR(arr);\n"
        "    long score = 0;\n"
        "    if (data != NULL) {\n"
        "        PyArray_FILLWBYTE(arr, 0x7f);\n"
        "        if (data[0] == 0x7f && data[1] == 0x7f && data[2] == 0x7f && data[3] == 0x7f) score += 1;\n"
        "        PyArray_FILLWBYTE(arr, 0);\n"
        "        if (data[0] == 0 && data[1] == 0 && data[2] == 0 && data[3] == 0) score += 10;\n"
        "    }\n"
        "    if (descr != NULL && PyArray_EquivByteorders(descr->byteorder, NPY_NATBYTE)) score += 100;\n"
        "    if (PyArray_EquivByteorders('|', NPY_NATBYTE)) score += 1000;\n"
        "    if (!PyArray_EquivByteorders(NPY_NATBYTE, NPY_OPPBYTE)) score += 10000;\n"
        "    Py_DECREF(arr_obj);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_shape_compare_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    npy_intp dims_a[2] = {2, 3};\n"
        "    npy_intp dims_b[2] = {2, 3};\n"
        "    npy_intp dims_c[2] = {3, 2};\n"
        "    npy_intp dims_d[1] = {6};\n"
        "    PyObject *a_obj = PyArray_SimpleNew(2, dims_a, NPY_INT);\n"
        "    PyObject *b_obj = PyArray_SimpleNew(2, dims_b, NPY_INT);\n"
        "    PyObject *c_obj = PyArray_SimpleNew(2, dims_c, NPY_INT);\n"
        "    PyObject *d_obj = PyArray_SimpleNew(1, dims_d, NPY_INT);\n"
        "    if (a_obj == NULL || b_obj == NULL || c_obj == NULL || d_obj == NULL) {\n"
        "        Py_XDECREF(a_obj);\n"
        "        Py_XDECREF(b_obj);\n"
        "        Py_XDECREF(c_obj);\n"
        "        Py_XDECREF(d_obj);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyArrayObject *a = (PyArrayObject *)a_obj;\n"
        "    PyArrayObject *b = (PyArrayObject *)b_obj;\n"
        "    PyArrayObject *c = (PyArrayObject *)c_obj;\n"
        "    PyArrayObject *d = (PyArrayObject *)d_obj;\n"
        "    npy_intp *shape = PyArray_SHAPE(a);\n"
        "    long score = 0;\n"
        "    if (shape != NULL && shape[0] == 2 && shape[1] == 3) score += 1;\n"
        "    if (PyArray_CompareLists(PyArray_DIMS(a), PyArray_DIMS(b), PyArray_NDIM(a))) score += 10;\n"
        "    if (!PyArray_CompareLists(PyArray_DIMS(a), PyArray_DIMS(c), PyArray_NDIM(a))) score += 100;\n"
        "    if (PyArray_SAMESHAPE(a, b)) score += 1000;\n"
        "    if (!PyArray_SAMESHAPE(a, c)) score += 10000;\n"
        "    if (!PyArray_SAMESHAPE(a, d)) score += 100000;\n"
        "    Py_DECREF(a_obj);\n"
        "    Py_DECREF(b_obj);\n"
        "    Py_DECREF(c_obj);\n"
        "    Py_DECREF(d_obj);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_empty_zeros_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    npy_intp dims[2] = {2, 2};\n"
        "    PyObject *empty_obj = PyArray_EMPTY(2, dims, NPY_INT, 0);\n"
        "    PyObject *zeros_obj = PyArray_ZEROS(2, dims, NPY_BYTE, 0);\n"
        "    if (empty_obj == NULL || zeros_obj == NULL) {\n"
        "        Py_XDECREF(empty_obj);\n"
        "        Py_XDECREF(zeros_obj);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyArrayObject *empty_arr = (PyArrayObject *)empty_obj;\n"
        "    PyArrayObject *zeros_arr = (PyArrayObject *)zeros_obj;\n"
        "    signed char *zeros_data = (signed char *)PyArray_DATA(zeros_arr);\n"
        "    long score = 0;\n"
        "    if (PyArray_TYPE(empty_arr) == NPY_INT && PyArray_NDIM(empty_arr) == 2 && PyArray_SIZE(empty_arr) == 4) score += 1;\n"
        "    if (PyArray_TYPE(zeros_arr) == NPY_BYTE && PyArray_NDIM(zeros_arr) == 2 && PyArray_SIZE(zeros_arr) == 4) score += 10;\n"
        "    if (zeros_data != NULL && zeros_data[0] == 0 && zeros_data[1] == 0 && zeros_data[2] == 0 && zeros_data[3] == 0) score += 100;\n"
        "    if (PyArray_SAMESHAPE(empty_arr, zeros_arr)) score += 1000;\n"
        "    PyObject *fortran_obj = PyArray_EMPTY(2, dims, NPY_INT, 1);\n"
        "    if (fortran_obj == NULL && PyErr_ExceptionMatches(PyExc_NotImplementedError)) score += 10000;\n"
        "    Py_XDECREF(fortran_obj);\n"
        "    PyErr_Clear();\n"
        "    if (PyArray_CompareLists(PyArray_SHAPE(empty_arr), dims, 2)) score += 100000;\n"
        "    Py_DECREF(empty_obj);\n"
        "    Py_DECREF(zeros_obj);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_equiv_types_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    npy_intp dims[1] = {3};\n"
        "    PyObject *int_a_obj = PyArray_EMPTY(1, dims, NPY_INT, 0);\n"
        "    PyObject *int_b_obj = PyArray_ZEROS(1, dims, NPY_INT, 0);\n"
        "    PyObject *double_obj = PyArray_EMPTY(1, dims, NPY_DOUBLE, 0);\n"
        "    if (int_a_obj == NULL || int_b_obj == NULL || double_obj == NULL) {\n"
        "        Py_XDECREF(int_a_obj);\n"
        "        Py_XDECREF(int_b_obj);\n"
        "        Py_XDECREF(double_obj);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyArrayObject *int_a = (PyArrayObject *)int_a_obj;\n"
        "    PyArrayObject *int_b = (PyArrayObject *)int_b_obj;\n"
        "    PyArrayObject *dbl = (PyArrayObject *)double_obj;\n"
        "    long score = 0;\n"
        "    if (PyArray_EquivTypes(PyArray_DESCR(int_a), PyArray_DESCR(int_b))) score += 1;\n"
        "    if (!PyArray_EquivTypes(PyArray_DESCR(int_a), PyArray_DESCR(dbl))) score += 10;\n"
        "    if (PyArray_EquivArrTypes(int_a, int_b)) score += 100;\n"
        "    if (!PyArray_EquivArrTypes(int_a, dbl)) score += 1000;\n"
        "    if (!PyArray_EquivTypes(PyArray_DESCR(int_a), NULL)) score += 10000;\n"
        "    if (PyArray_EquivTypenums(NPY_INT, NPY_INT)) score += 100000;\n"
        "    if (!PyArray_EquivTypenums(NPY_INT, NPY_DOUBLE)) score += 1000000;\n"
        "    if (!PyArray_EquivTypenums(9999, NPY_INT) && PyErr_Occurred() == NULL) score += 10000000;\n"
        "    Py_DECREF(int_a_obj);\n"
        "    Py_DECREF(int_b_obj);\n"
        "    Py_DECREF(double_obj);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_new_from_descr_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    npy_intp dims[1] = {3};\n"
        "    PyObject *descr_obj = PyArray_SimpleNewFromDescr(1, dims, PyArray_DescrFromType(NPY_DOUBLE));\n"
        "    if (descr_obj == NULL) return NULL;\n"
        "    PyArrayObject *descr_arr = (PyArrayObject *)descr_obj;\n"
        "    signed char raw[3] = {4, 5, 6};\n"
        "    PyObject *data_obj = PyArray_NewFromDescr((PyTypeObject *)PyArray_API[0], PyArray_DescrFromType(NPY_BYTE), 1, dims, NULL, raw, 0, NULL);\n"
        "    if (data_obj == NULL) {\n"
        "        Py_DECREF(descr_obj);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyArrayObject *data_arr = (PyArrayObject *)data_obj;\n"
        "    long score = 0;\n"
        "    if (PyArray_TYPE(descr_arr) == NPY_DOUBLE && PyArray_NDIM(descr_arr) == 1 && PyArray_SIZE(descr_arr) == 3) score += 1;\n"
        "    if (PyArray_ITEMSIZE(descr_arr) == (int)sizeof(double)) score += 10;\n"
        "    if (PyArray_TYPE(data_arr) == NPY_BYTE && PyArray_DATA(data_arr) == raw) score += 100;\n"
        "    npy_intp strides[1] = {1};\n"
        "    PyObject *strided_obj = PyArray_NewFromDescr((PyTypeObject *)PyArray_API[0], PyArray_DescrFromType(NPY_BYTE), 1, dims, strides, NULL, 0, NULL);\n"
        "    if (strided_obj == NULL && PyErr_ExceptionMatches(PyExc_NotImplementedError)) score += 1000;\n"
        "    Py_XDECREF(strided_obj);\n"
        "    PyErr_Clear();\n"
        "    PyObject *null_descr_obj = PyArray_NewFromDescr((PyTypeObject *)PyArray_API[0], NULL, 1, dims, NULL, NULL, 0, NULL);\n"
        "    if (null_descr_obj == NULL && PyErr_ExceptionMatches(PyExc_ValueError)) score += 10000;\n"
        "    Py_XDECREF(null_descr_obj);\n"
        "    PyErr_Clear();\n"
        "    Py_DECREF(descr_obj);\n"
        "    Py_DECREF(data_obj);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_new_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    npy_intp dims[2] = {2, 2};\n"
        "    PyObject *owned_obj = PyArray_New(&PyArray_Type, 2, dims, NPY_INT, NULL, NULL, 0, 0, NULL);\n"
        "    if (owned_obj == NULL) return NULL;\n"
        "    PyArrayObject *owned = (PyArrayObject *)owned_obj;\n"
        "    int *owned_data = (int *)PyArray_DATA(owned);\n"
        "    signed char raw[3] = {1, 2, 3};\n"
        "    npy_intp raw_dims[1] = {3};\n"
        "    PyObject *external_obj = PyArray_New(&PyArray_Type, 1, raw_dims, NPY_BYTE, NULL, raw, 0, 0, NULL);\n"
        "    if (external_obj == NULL) {\n"
        "        Py_DECREF(owned_obj);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyArrayObject *external = (PyArrayObject *)external_obj;\n"
        "    long score = 0;\n"
        "    if (PyArray_TYPE(owned) == NPY_INT && PyArray_NDIM(owned) == 2 && PyArray_SIZE(owned) == 4) score += 1;\n"
        "    if (PyArray_ITEMSIZE(owned) == (int)sizeof(int) && owned_data != NULL) {\n"
        "        owned_data[0] = 7;\n"
        "        owned_data[3] = 10;\n"
        "        if (owned_data[0] == 7 && owned_data[3] == 10) score += 10;\n"
        "    }\n"
        "    if (PyArray_TYPE(external) == NPY_BYTE && PyArray_DATA(external) == raw && PyArray_SIZE(external) == 3) score += 100;\n"
        "    raw[1] = 9;\n"
        "    if (((signed char *)PyArray_DATA(external))[1] == 9) score += 1000;\n"
        "    npy_intp strides[1] = {1};\n"
        "    PyObject *strided_obj = PyArray_New(&PyArray_Type, 1, raw_dims, NPY_BYTE, strides, NULL, 0, 0, NULL);\n"
        "    if (strided_obj == NULL && PyErr_ExceptionMatches(PyExc_NotImplementedError)) score += 10000;\n"
        "    Py_XDECREF(strided_obj);\n"
        "    PyErr_Clear();\n"
        "    PyObject *null_subtype_obj = PyArray_New(NULL, 1, raw_dims, NPY_BYTE, NULL, NULL, 0, 0, NULL);\n"
        "    if (null_subtype_obj == NULL && PyErr_ExceptionMatches(PyExc_ValueError)) score += 100000;\n"
        "    Py_XDECREF(null_subtype_obj);\n"
        "    PyErr_Clear();\n"
        "    Py_DECREF(external_obj);\n"
        "    Py_DECREF(owned_obj);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_array_check_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    npy_intp dims[1] = {2};\n"
        "    PyObject *arr_obj = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    PyObject *non_arr = PyLong_FromLong(7);\n"
        "    if (arr_obj == NULL || non_arr == NULL) {\n"
        "        Py_XDECREF(arr_obj);\n"
        "        Py_XDECREF(non_arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    long score = 0;\n"
        "    if (PyArray_Check(arr_obj)) score += 1;\n"
        "    if (PyArray_CheckExact(arr_obj)) score += 10;\n"
        "    if (!PyArray_Check(non_arr)) score += 100;\n"
        "    if (!PyArray_CheckExact(non_arr)) score += 1000;\n"
        "    Py_DECREF(arr_obj);\n"
        "    Py_DECREF(non_arr);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_size_itemsize_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    npy_intp int_dims[2] = {2, 3};\n"
        "    npy_intp double_dims[1] = {4};\n"
        "    PyObject *int_obj = PyArray_SimpleNew(2, int_dims, NPY_INT);\n"
        "    PyObject *double_obj = PyArray_SimpleNew(1, double_dims, NPY_DOUBLE);\n"
        "    if (int_obj == NULL || double_obj == NULL) {\n"
        "        Py_XDECREF(int_obj);\n"
        "        Py_XDECREF(double_obj);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyArrayObject *int_arr = (PyArrayObject *)int_obj;\n"
        "    PyArrayObject *double_arr = (PyArrayObject *)double_obj;\n"
        "    long score = 0;\n"
        "    if (PyArray_SIZE(int_arr) == 6) score += 1;\n"
        "    if (PyArray_ITEMSIZE(int_arr) == (int)sizeof(int)) score += 10;\n"
        "    if (PyArray_SIZE(double_arr) == 4) score += 100;\n"
        "    if (PyArray_ITEMSIZE(double_arr) == (int)sizeof(double)) score += 1000;\n"
        "    Py_DECREF(int_obj);\n"
        "    Py_DECREF(double_obj);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_array_size_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    npy_intp dims[2] = {2, 3};\n"
        "    PyObject *arr = PyArray_SimpleNew(2, dims, NPY_INT);\n"
        "    PyObject *list = PyList_New(2);\n"
        "    if (arr == NULL || list == NULL) {\n"
        "        Py_XDECREF(arr);\n"
        "        Py_XDECREF(list);\n"
        "        return NULL;\n"
        "    }\n"
        "    long score = 0;\n"
        "    if (PyArray_Size(arr) == 6) score += 1;\n"
        "    if (PyArray_Size(Py_None) == 0) score += 10;\n"
        "    if (PyArray_Size(list) == 0) score += 100;\n"
        "    if (PyArray_Size(NULL) == 0) score += 1000;\n"
        "    Py_DECREF(list);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_accessor_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    npy_intp dims[2] = {2, 3};\n"
        "    PyObject *obj = PyArray_SimpleNew(2, dims, NPY_INT);\n"
        "    if (obj == NULL) return NULL;\n"
        "    PyArrayObject *arr = (PyArrayObject *)obj;\n"
        "    npy_intp *arr_dims = PyArray_DIMS(arr);\n"
        "    npy_intp *arr_strides = PyArray_STRIDES(arr);\n"
        "    int *arr_data = (int *)PyArray_DATA(arr);\n"
        "    PyArray_Descr *descr = PyArray_DESCR(arr);\n"
        "    long score = 0;\n"
        "    if (PyArray_NDIM(arr) == 2) score += 1;\n"
        "    if (arr_dims != NULL && arr_dims[0] == 2 && arr_dims[1] == 3) score += 10;\n"
        "    if (arr_strides != NULL && arr_strides[0] == 3 * (npy_intp)sizeof(int) && arr_strides[1] == (npy_intp)sizeof(int)) score += 100;\n"
        "    if (arr_data != NULL) score += 1000;\n"
        "    if (descr != NULL && descr->type_num == NPY_INT) score += 10000;\n"
        "    if (PyArray_DIM(arr, 0) == 2 && PyArray_DIM(arr, 1) == 3) score += 100000;\n"
        "    if (PyArray_BYTES(arr) == (void *)arr_data) score += 1000000;\n"
        "    Py_DECREF(obj);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_core_provider_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    npy_intp dims[1] = {3};\n"
        "    PyArray_Descr *descr = PyArray_DescrFromType(NPY_INT);\n"
        "    PyObject *owned = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    int raw[3] = {1, 2, 3};\n"
        "    PyObject *borrowed = PyArray_SimpleNewFromData(1, dims, NPY_INT, raw);\n"
        "    PyObject *v0 = PyLong_FromLong(5);\n"
        "    PyObject *v1 = PyLong_FromLong(6);\n"
        "    PyObject *seq = NULL;\n"
        "    PyObject *from_any = NULL;\n"
        "    PyObject *item = NULL;\n"
        "    PyObject *replacement = NULL;\n"
        "    if (owned == NULL || borrowed == NULL || v0 == NULL || v1 == NULL) {\n"
        "        Py_XDECREF(owned);\n"
        "        Py_XDECREF(borrowed);\n"
        "        Py_XDECREF(v0);\n"
        "        Py_XDECREF(v1);\n"
        "        return NULL;\n"
        "    }\n"
        "    seq = PyTuple_Pack(2, v0, v1);\n"
        "    Py_DECREF(v0);\n"
        "    Py_DECREF(v1);\n"
        "    if (seq == NULL) {\n"
        "        Py_DECREF(owned);\n"
        "        Py_DECREF(borrowed);\n"
        "        return NULL;\n"
        "    }\n"
        "    from_any = PyArray_FromAny(seq, NULL, 1, 1, 0, NULL);\n"
        "    Py_DECREF(seq);\n"
        "    if (from_any == NULL) {\n"
        "        Py_DECREF(owned);\n"
        "        Py_DECREF(borrowed);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyArrayObject *owned_arr = (PyArrayObject *)owned;\n"
        "    PyArrayObject *borrowed_arr = (PyArrayObject *)borrowed;\n"
        "    PyArrayObject *from_any_arr = (PyArrayObject *)from_any;\n"
        "    long score = 0;\n"
        "    if (descr != NULL && descr->type_num == NPY_INT) score += 1;\n"
        "    if (PyArray_NDIM(owned_arr) == 1 && PyArray_SIZE(owned_arr) == 3 && PyArray_TYPE(owned_arr) == NPY_INT) score += 10;\n"
        "    if (PyArray_DATA(borrowed_arr) == raw && PyArray_SIZE(borrowed_arr) == 3) score += 100;\n"
        "    if (PyArray_NDIM(from_any_arr) == 1 && PyArray_SIZE(from_any_arr) == 2 && PyArray_TYPE(from_any_arr) == NPY_LONG) score += 1000;\n"
        "    item = PyArray_GETITEM(borrowed_arr, &raw[1]);\n"
        "    if (item != NULL && PyLong_AsLong(item) == 2) score += 10000;\n"
        "    Py_XDECREF(item);\n"
        "    replacement = PyLong_FromLong(9);\n"
        "    if (replacement == NULL) {\n"
        "        Py_DECREF(from_any);\n"
        "        Py_DECREF(owned);\n"
        "        Py_DECREF(borrowed);\n"
        "        return NULL;\n"
        "    }\n"
        "    if (PyArray_SETITEM(borrowed_arr, &raw[2], replacement) == 0 && raw[2] == 9) score += 100000;\n"
        "    Py_DECREF(replacement);\n"
        "    if (PyErr_Occurred() != NULL) {\n"
        "        Py_DECREF(from_any);\n"
        "        Py_DECREF(owned);\n"
        "        Py_DECREF(borrowed);\n"
        "        return NULL;\n"
        "    }\n"
        "    Py_DECREF(from_any);\n"
        "    Py_DECREF(owned);\n"
        "    Py_DECREF(borrowed);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_base_object_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    npy_intp dims[1] = {2};\n"
        "    int raw[2] = {4, 5};\n"
        "    PyObject *owned = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    PyObject *borrowed = PyArray_SimpleNewFromData(1, dims, NPY_INT, raw);\n"
        '    PyObject *base = PyBytes_FromString("owner");\n'
        "    if (owned == NULL || borrowed == NULL || base == NULL) {\n"
        "        Py_XDECREF(owned);\n"
        "        Py_XDECREF(borrowed);\n"
        "        Py_XDECREF(base);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyArrayObject *owned_arr = (PyArrayObject *)owned;\n"
        "    PyArrayObject *borrowed_arr = (PyArrayObject *)borrowed;\n"
        "    long score = 0;\n"
        "    if (PyArray_BASE(owned_arr) == NULL) score += 1;\n"
        "    if (PyArray_SetBaseObject(borrowed_arr, base) == 0) score += 10;\n"
        "    if (PyArray_BASE(borrowed_arr) == base) score += 100;\n"
        "    if (Py_REFCNT(base) == 1) score += 1000;\n"
        "    PyObject *base2 = PyLong_FromLong(99);\n"
        "    PyObject *with_base = PyArray_NewFromDescr((PyTypeObject *)PyArray_API[0], PyArray_DescrFromType(NPY_BYTE), 1, dims, NULL, raw, 0, base2);\n"
        "    if (with_base == NULL) {\n"
        "        Py_DECREF(owned);\n"
        "        Py_DECREF(borrowed);\n"
        "        Py_XDECREF(base2);\n"
        "        return NULL;\n"
        "    }\n"
        "    if (PyArray_BASE((PyArrayObject *)with_base) == base2) score += 10000;\n"
        "    Py_DECREF(with_base);\n"
        "    Py_DECREF(owned);\n"
        "    Py_DECREF(borrowed);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_return_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    npy_intp dims[1] = {2};\n"
        "    PyObject *arr_obj = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    PyObject *scalar_arr_obj = PyArray_SimpleNew(0, NULL, NPY_INT);\n"
        "    if (arr_obj == NULL || scalar_arr_obj == NULL) {\n"
        "        Py_XDECREF(arr_obj);\n"
        "        Py_XDECREF(scalar_arr_obj);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyArrayObject *arr = (PyArrayObject *)arr_obj;\n"
        "    PyArrayObject *scalar_arr = (PyArrayObject *)scalar_arr_obj;\n"
        "    int *arr_data = (int *)PyArray_DATA(arr);\n"
        "    int *scalar_data = (int *)PyArray_DATA(scalar_arr);\n"
        "    if (arr_data == NULL || scalar_data == NULL) {\n"
        "        Py_DECREF(arr_obj);\n"
        "        Py_DECREF(scalar_arr_obj);\n"
        "        return NULL;\n"
        "    }\n"
        "    arr_data[0] = 7;\n"
        "    arr_data[1] = 8;\n"
        "    scalar_data[0] = 42;\n"
        "    long score = 0;\n"
        "    PyObject *arr_ret = PyArray_Return(arr);\n"
        "    if (arr_ret == arr_obj) score += 1;\n"
        "    if (arr_ret != NULL && PyArray_Check(arr_ret) && PyArray_SIZE((PyArrayObject *)arr_ret) == 2) score += 10;\n"
        "    Py_DECREF(arr_ret);\n"
        "    PyObject *scalar_ret = PyArray_Return(scalar_arr);\n"
        "    if (scalar_ret != scalar_arr_obj) score += 100;\n"
        "    if (scalar_ret != NULL && PyLong_Check(scalar_ret) && PyLong_AsLong(scalar_ret) == 42) score += 1000;\n"
        "    Py_DECREF(scalar_ret);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_flags_mutation_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    npy_intp dims[1] = {2};\n"
        "    PyObject *obj = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (obj == NULL) return NULL;\n"
        "    PyArrayObject *arr = (PyArrayObject *)obj;\n"
        "    long score = 0;\n"
        "    if (PyArray_ISWRITEABLE(arr)) score += 1;\n"
        "    PyArray_CLEARFLAGS(arr, NPY_ARRAY_WRITEABLE);\n"
        "    if (!PyArray_ISWRITEABLE(arr)) score += 10;\n"
        "    PyArray_ENABLEFLAGS(arr, NPY_ARRAY_WRITEABLE);\n"
        "    if (PyArray_ISWRITEABLE(arr)) score += 100;\n"
        "    PyArray_CLEARFLAGS(arr, NPY_ARRAY_ALIGNED);\n"
        "    if (!PyArray_ISALIGNED(arr)) score += 1000;\n"
        "    PyArray_UpdateFlags(arr, NPY_ARRAY_ALIGNED);\n"
        "    if (PyArray_ISALIGNED(arr)) score += 10000;\n"
        "    PyArray_CLEARFLAGS(arr, NPY_ARRAY_C_CONTIGUOUS | NPY_ARRAY_F_CONTIGUOUS);\n"
        "    if (!PyArray_ISCONTIGUOUS(arr) && !PyArray_IS_F_CONTIGUOUS(arr)) score += 100000;\n"
        "    PyArray_UpdateFlags(arr, NPY_ARRAY_C_CONTIGUOUS | NPY_ARRAY_F_CONTIGUOUS);\n"
        "    if (PyArray_ISCONTIGUOUS(arr) && PyArray_IS_F_CONTIGUOUS(arr)) score += 1000000;\n"
        "    Py_DECREF(obj);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_copy_into_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    npy_intp dims[1] = {3};\n"
        "    npy_intp mismatch_dims[1] = {2};\n"
        "    PyObject *src_obj = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    PyObject *dst_obj = PyArray_ZEROS(1, dims, NPY_INT, 0);\n"
        "    PyObject *mismatch_obj = PyArray_ZEROS(1, mismatch_dims, NPY_INT, 0);\n"
        "    if (src_obj == NULL || dst_obj == NULL || mismatch_obj == NULL) {\n"
        "        Py_XDECREF(src_obj);\n"
        "        Py_XDECREF(dst_obj);\n"
        "        Py_XDECREF(mismatch_obj);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyArrayObject *src = (PyArrayObject *)src_obj;\n"
        "    PyArrayObject *dst = (PyArrayObject *)dst_obj;\n"
        "    PyArrayObject *mismatch = (PyArrayObject *)mismatch_obj;\n"
        "    int *src_data = (int *)PyArray_DATA(src);\n"
        "    int *dst_data = (int *)PyArray_DATA(dst);\n"
        "    if (src_data == NULL || dst_data == NULL) {\n"
        "        Py_DECREF(src_obj);\n"
        "        Py_DECREF(dst_obj);\n"
        "        Py_DECREF(mismatch_obj);\n"
        "        return NULL;\n"
        "    }\n"
        "    src_data[0] = 3;\n"
        "    src_data[1] = 4;\n"
        "    src_data[2] = 5;\n"
        "    long score = 0;\n"
        "    if (PyArray_CopyInto(dst, src) == 0) score += 1;\n"
        "    if (dst_data[0] == 3 && dst_data[1] == 4 && dst_data[2] == 5) score += 10;\n"
        "    src_data[0] = 6;\n"
        "    src_data[1] = 7;\n"
        "    src_data[2] = 8;\n"
        "    if (PyArray_CopyAnyInto(dst, src) == 0) score += 100;\n"
        "    if (dst_data[0] == 6 && dst_data[1] == 7 && dst_data[2] == 8) score += 1000;\n"
        "    if (PyArray_CopyInto(mismatch, src) != 0 && PyErr_ExceptionMatches(PyExc_ValueError)) score += 10000;\n"
        "    PyErr_Clear();\n"
        "    Py_DECREF(src_obj);\n"
        "    Py_DECREF(dst_obj);\n"
        "    Py_DECREF(mismatch_obj);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_to_scalar_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    npy_intp dims[1] = {2};\n"
        "    PyObject *obj = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (obj == NULL) return NULL;\n"
        "    PyArrayObject *arr = (PyArrayObject *)obj;\n"
        "    int *data = (int *)PyArray_DATA(arr);\n"
        "    if (data == NULL) {\n"
        "        Py_DECREF(obj);\n"
        "        return NULL;\n"
        "    }\n"
        "    data[0] = 11;\n"
        "    data[1] = 12;\n"
        "    PyObject *scalar0 = PyArray_ToScalar((void *)data, arr);\n"
        "    PyObject *scalar1 = PyArray_ToScalar((void *)(data + 1), arr);\n"
        "    if (scalar0 == NULL || scalar1 == NULL) {\n"
        "        Py_XDECREF(scalar0);\n"
        "        Py_XDECREF(scalar1);\n"
        "        Py_DECREF(obj);\n"
        "        return NULL;\n"
        "    }\n"
        "    long score = 0;\n"
        "    if (PyLong_Check(scalar0) && PyLong_AsLong(scalar0) == 11) score += 1;\n"
        "    if (PyLong_Check(scalar1) && PyLong_AsLong(scalar1) == 12) score += 10;\n"
        "    Py_DECREF(scalar0);\n"
        "    Py_DECREF(scalar1);\n"
        "    PyObject *bad = PyArray_ToScalar(NULL, arr);\n"
        "    if (bad == NULL && PyErr_ExceptionMatches(PyExc_ValueError)) score += 100;\n"
        "    Py_XDECREF(bad);\n"
        "    PyErr_Clear();\n"
        "    Py_DECREF(obj);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_copy_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    npy_intp dims[1] = {2};\n"
        "    PyObject *src_obj = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    PyObject *obj_obj = PyArray_SimpleNew(1, dims, NPY_OBJECT);\n"
        "    if (src_obj == NULL || obj_obj == NULL) {\n"
        "        Py_XDECREF(src_obj);\n"
        "        Py_XDECREF(obj_obj);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyArrayObject *src = (PyArrayObject *)src_obj;\n"
        "    int *src_data = (int *)PyArray_DATA(src);\n"
        "    if (src_data == NULL) {\n"
        "        Py_DECREF(src_obj);\n"
        "        Py_DECREF(obj_obj);\n"
        "        return NULL;\n"
        "    }\n"
        "    src_data[0] = 21;\n"
        "    src_data[1] = 22;\n"
        "    PyObject *copy_obj = PyArray_Copy(src);\n"
        "    if (copy_obj == NULL) {\n"
        "        Py_DECREF(src_obj);\n"
        "        Py_DECREF(obj_obj);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyArrayObject *copy = (PyArrayObject *)copy_obj;\n"
        "    int *copy_data = (int *)PyArray_DATA(copy);\n"
        "    long score = 0;\n"
        "    if (copy_obj != src_obj && PyArray_CheckExact(copy) && PyArray_SIZE(copy) == 2) score += 1;\n"
        "    if (copy_data != NULL && copy_data[0] == 21 && copy_data[1] == 22) score += 10;\n"
        "    src_data[0] = 99;\n"
        "    if (copy_data != NULL && copy_data[0] == 21) score += 100;\n"
        "    PyObject *obj_copy = PyArray_Copy((PyArrayObject *)obj_obj);\n"
        "    if (obj_copy == NULL && PyErr_ExceptionMatches(PyExc_NotImplementedError)) score += 1000;\n"
        "    Py_XDECREF(obj_copy);\n"
        "    PyErr_Clear();\n"
        "    Py_DECREF(copy_obj);\n"
        "    Py_DECREF(src_obj);\n"
        "    Py_DECREF(obj_obj);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_new_copy_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    npy_intp dims[1] = {2};\n"
        "    PyObject *src_obj = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    PyObject *obj_obj = PyArray_SimpleNew(1, dims, NPY_OBJECT);\n"
        "    if (src_obj == NULL || obj_obj == NULL) {\n"
        "        Py_XDECREF(src_obj);\n"
        "        Py_XDECREF(obj_obj);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyArrayObject *src = (PyArrayObject *)src_obj;\n"
        "    int *src_data = (int *)PyArray_DATA(src);\n"
        "    if (src_data == NULL) {\n"
        "        Py_DECREF(src_obj);\n"
        "        Py_DECREF(obj_obj);\n"
        "        return NULL;\n"
        "    }\n"
        "    src_data[0] = 41;\n"
        "    src_data[1] = 42;\n"
        "    PyObject *copy_obj = PyArray_NewCopy(src, NPY_KEEPORDER);\n"
        "    if (copy_obj == NULL) {\n"
        "        Py_DECREF(src_obj);\n"
        "        Py_DECREF(obj_obj);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyArrayObject *copy = (PyArrayObject *)copy_obj;\n"
        "    int *copy_data = (int *)PyArray_DATA(copy);\n"
        "    long score = 0;\n"
        "    if (copy_obj != src_obj && PyArray_CheckExact(copy) && PyArray_SIZE(copy) == 2) score += 1;\n"
        "    if (copy_data != NULL && copy_data[0] == 41 && copy_data[1] == 42) score += 10;\n"
        "    src_data[0] = 99;\n"
        "    if (copy_data != NULL && copy_data[0] == 41) score += 100;\n"
        "    PyObject *obj_copy = PyArray_NewCopy((PyArrayObject *)obj_obj, NPY_KEEPORDER);\n"
        "    if (obj_copy == NULL && PyErr_ExceptionMatches(PyExc_NotImplementedError)) score += 1000;\n"
        "    Py_XDECREF(obj_copy);\n"
        "    PyErr_Clear();\n"
        "    PyObject *null_copy = PyArray_NewCopy(NULL, NPY_CORDER);\n"
        "    if (null_copy == NULL && PyErr_ExceptionMatches(PyExc_ValueError)) score += 10000;\n"
        "    Py_XDECREF(null_copy);\n"
        "    PyErr_Clear();\n"
        "    Py_DECREF(copy_obj);\n"
        "    Py_DECREF(src_obj);\n"
        "    Py_DECREF(obj_obj);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_copy_object_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[1] = {3};\n"
        "    PyObject *dest_obj = PyArray_SimpleNew(1, dims, NPY_LONG);\n"
        "    PyObject *src_obj = PyArray_SimpleNew(1, dims, NPY_LONG);\n"
        "    if (dest_obj == NULL || src_obj == NULL) {\n"
        "        Py_XDECREF(dest_obj);\n"
        "        Py_XDECREF(src_obj);\n"
        "        return NULL;\n"
        "    }\n"
        "    long *src = (long *)PyArray_DATA((PyArrayObject *)src_obj);\n"
        "    long *dest = (long *)PyArray_DATA((PyArrayObject *)dest_obj);\n"
        "    src[0] = 1; src[1] = 2; src[2] = 3;\n"
        "    if (PyArray_CopyObject((PyArrayObject *)dest_obj, src_obj) == 0 && dest[0] == 1 && dest[1] == 2 && dest[2] == 3) score += 1;\n"
        "    PyObject *list = PyList_New(3);\n"
        "    if (list == NULL) { Py_DECREF(dest_obj); Py_DECREF(src_obj); return NULL; }\n"
        "    if (PyList_SetItem(list, 0, PyLong_FromLong(4)) != 0 ||\n"
        "        PyList_SetItem(list, 1, PyLong_FromLong(5)) != 0 ||\n"
        "        PyList_SetItem(list, 2, PyLong_FromLong(6)) != 0) {\n"
        "        Py_DECREF(list);\n"
        "        Py_DECREF(dest_obj);\n"
        "        Py_DECREF(src_obj);\n"
        "        return NULL;\n"
        "    }\n"
        "    if (PyArray_CopyObject((PyArrayObject *)dest_obj, list) == 0 && dest[0] == 4 && dest[1] == 5 && dest[2] == 6) score += 10;\n"
        "    Py_DECREF(list);\n"
        "    PyObject *scalar = PyLong_FromLong(9);\n"
        "    if (scalar == NULL) { Py_DECREF(dest_obj); Py_DECREF(src_obj); return NULL; }\n"
        "    if (PyArray_CopyObject((PyArrayObject *)dest_obj, scalar) == 0 && dest[0] == 9 && dest[1] == 9 && dest[2] == 9) score += 100;\n"
        "    Py_DECREF(scalar);\n"
        "    PyObject *short_list = PyList_New(2);\n"
        "    if (short_list == NULL) { Py_DECREF(dest_obj); Py_DECREF(src_obj); return NULL; }\n"
        "    if (PyList_SetItem(short_list, 0, PyLong_FromLong(7)) != 0 ||\n"
        "        PyList_SetItem(short_list, 1, PyLong_FromLong(8)) != 0) {\n"
        "        Py_DECREF(short_list);\n"
        "        Py_DECREF(dest_obj);\n"
        "        Py_DECREF(src_obj);\n"
        "        return NULL;\n"
        "    }\n"
        "    if (PyArray_CopyObject((PyArrayObject *)dest_obj, short_list) != 0 && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000; }\n"
        "    Py_DECREF(short_list);\n"
        "    Py_DECREF(dest_obj);\n"
        "    Py_DECREF(src_obj);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_resize_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[1] = {3};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (arr == NULL) return NULL;\n"
        "    int *data = (int *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    data[0] = 1; data[1] = 2; data[2] = 3;\n"
        "    npy_intp grow_dims[1] = {5};\n"
        "    PyArray_Dims grow = {grow_dims, 1};\n"
        "    PyObject *ret = PyArray_Resize((PyArrayObject *)arr, &grow, 0, NPY_CORDER);\n"
        "    data = (int *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    if (ret == Py_None && PyArray_SIZE((PyArrayObject *)arr) == 5 && data[0] == 1 && data[1] == 2 && data[2] == 3 && data[3] == 0 && data[4] == 0) score += 1;\n"
        "    Py_XDECREF(ret);\n"
        "    npy_intp shrink_dims[1] = {2};\n"
        "    PyArray_Dims shrink = {shrink_dims, 1};\n"
        "    ret = PyArray_Resize((PyArrayObject *)arr, &shrink, 0, NPY_CORDER);\n"
        "    data = (int *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    if (ret == Py_None && PyArray_SIZE((PyArrayObject *)arr) == 2 && data[0] == 1 && data[1] == 2) score += 10;\n"
        "    Py_XDECREF(ret);\n"
        "    npy_intp matrix_dims[2] = {2, 2};\n"
        "    PyArray_Dims matrix = {matrix_dims, 2};\n"
        "    ret = PyArray_Resize((PyArrayObject *)arr, &matrix, 0, NPY_CORDER);\n"
        "    data = (int *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    npy_intp *shape = PyArray_DIMS((PyArrayObject *)arr);\n"
        "    npy_intp *strides = PyArray_STRIDES((PyArrayObject *)arr);\n"
        "    if (ret == Py_None && PyArray_NDIM((PyArrayObject *)arr) == 2 && shape[0] == 2 && shape[1] == 2 &&\n"
        "        strides[0] == 2 * (npy_intp)sizeof(int) && strides[1] == (npy_intp)sizeof(int) &&\n"
        "        data[0] == 1 && data[1] == 2 && data[2] == 0 && data[3] == 0) score += 100;\n"
        "    Py_XDECREF(ret);\n"
        "    int raw[2] = {7, 8};\n"
        "    npy_intp view_dims[1] = {2};\n"
        "    PyObject *view = PyArray_SimpleNewFromData(1, view_dims, NPY_INT, raw);\n"
        "    if (view == NULL) { Py_DECREF(arr); return NULL; }\n"
        "    npy_intp reject_dims[1] = {3};\n"
        "    PyArray_Dims reject = {reject_dims, 1};\n"
        "    ret = PyArray_Resize((PyArrayObject *)view, &reject, 0, NPY_CORDER);\n"
        "    if (ret == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000; }\n"
        "    Py_XDECREF(ret);\n"
        "    Py_DECREF(view);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_new_like_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[2] = {2, 3};\n"
        "    PyObject *proto = PyArray_SimpleNew(2, dims, NPY_INT);\n"
        "    if (proto == NULL) return NULL;\n"
        "    PyObject *same = PyArray_NewLikeArray((PyArrayObject *)proto, NPY_CORDER, NULL, 0);\n"
        "    if (same != NULL && same != proto && PyArray_NDIM((PyArrayObject *)same) == 2 &&\n"
        "        PyArray_DIMS((PyArrayObject *)same)[0] == 2 && PyArray_DIMS((PyArrayObject *)same)[1] == 3 &&\n"
        "        PyArray_TYPE((PyArrayObject *)same) == NPY_INT && PyArray_ITEMSIZE((PyArrayObject *)same) == (npy_intp)sizeof(int) &&\n"
        "        PyArray_DATA((PyArrayObject *)same) != PyArray_DATA((PyArrayObject *)proto)) score += 1;\n"
        "    Py_XDECREF(same);\n"
        "    PyArray_Descr *descr = PyArray_DescrNewFromType(NPY_DOUBLE);\n"
        "    if (descr == NULL) { Py_DECREF(proto); return NULL; }\n"
        "    PyObject *typed = PyArray_NewLikeArray((PyArrayObject *)proto, NPY_ANYORDER, descr, 0);\n"
        "    if (typed != NULL && PyArray_NDIM((PyArrayObject *)typed) == 2 &&\n"
        "        PyArray_DIMS((PyArrayObject *)typed)[0] == 2 && PyArray_DIMS((PyArrayObject *)typed)[1] == 3 &&\n"
        "        PyArray_TYPE((PyArrayObject *)typed) == NPY_DOUBLE && PyArray_ITEMSIZE((PyArrayObject *)typed) == (npy_intp)sizeof(double)) score += 10;\n"
        "    Py_XDECREF(typed);\n"
        "    npy_intp one_dims[1] = {4};\n"
        "    PyObject *one = PyArray_SimpleNew(1, one_dims, NPY_INT);\n"
        "    if (one == NULL) { Py_DECREF(proto); return NULL; }\n"
        "    PyObject *one_like = PyArray_NewLikeArray((PyArrayObject *)one, NPY_FORTRANORDER, NULL, 0);\n"
        "    if (one_like != NULL && PyArray_NDIM((PyArrayObject *)one_like) == 1 &&\n"
        "        PyArray_DIMS((PyArrayObject *)one_like)[0] == 4 && PyArray_TYPE((PyArrayObject *)one_like) == NPY_INT) score += 100;\n"
        "    Py_XDECREF(one_like);\n"
        "    PyObject *fortran_reject = PyArray_NewLikeArray((PyArrayObject *)proto, NPY_FORTRANORDER, NULL, 0);\n"
        "    if (fortran_reject == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000; }\n"
        "    Py_XDECREF(fortran_reject);\n"
        "    Py_DECREF(one);\n"
        "    Py_DECREF(proto);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_view_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[2] = {2, 2};\n"
        "    PyObject *arr = PyArray_SimpleNew(2, dims, NPY_INT);\n"
        "    if (arr == NULL) return NULL;\n"
        "    int *data = (int *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    data[0] = 9;\n"
        "    PyObject *view = PyArray_View((PyArrayObject *)arr, NULL, NULL);\n"
        "    if (view != NULL && view != arr && PyArray_DATA((PyArrayObject *)view) == PyArray_DATA((PyArrayObject *)arr) &&\n"
        "        PyArray_BASE((PyArrayObject *)view) == arr && PyArray_NDIM((PyArrayObject *)view) == 2 &&\n"
        "        PyArray_DIMS((PyArrayObject *)view)[0] == 2 && PyArray_DIMS((PyArrayObject *)view)[1] == 2 &&\n"
        "        PyArray_STRIDES((PyArrayObject *)view)[0] == 2 * (npy_intp)sizeof(int) &&\n"
        "        PyArray_STRIDES((PyArrayObject *)view)[1] == (npy_intp)sizeof(int) &&\n"
        "        PyArray_TYPE((PyArrayObject *)view) == NPY_INT && ((int *)PyArray_DATA((PyArrayObject *)view))[0] == 9) score += 1;\n"
        "    Py_XDECREF(view);\n"
        "    PyArray_Descr *descr = PyArray_DescrNewFromType(NPY_DOUBLE);\n"
        "    if (descr == NULL) { Py_DECREF(arr); return NULL; }\n"
        "    PyObject *typed = PyArray_View((PyArrayObject *)arr, descr, NULL);\n"
        "    if (typed == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10; }\n"
        "    Py_XDECREF(typed);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_squeeze_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[3] = {1, 2, 1};\n"
        "    PyObject *arr = PyArray_SimpleNew(3, dims, NPY_INT);\n"
        "    if (arr == NULL) return NULL;\n"
        "    int *data = (int *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    data[0] = 4; data[1] = 5;\n"
        "    PyObject *sq = PyArray_Squeeze((PyArrayObject *)arr);\n"
        "    if (sq != NULL && sq != arr && PyArray_BASE((PyArrayObject *)sq) == arr &&\n"
        "        PyArray_DATA((PyArrayObject *)sq) == PyArray_DATA((PyArrayObject *)arr) &&\n"
        "        PyArray_NDIM((PyArrayObject *)sq) == 1 && PyArray_DIMS((PyArrayObject *)sq)[0] == 2 &&\n"
        "        PyArray_STRIDES((PyArrayObject *)sq)[0] == (npy_intp)sizeof(int) &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)sq))[1] == 5) score += 1;\n"
        "    Py_XDECREF(sq);\n"
        "    npy_intp scalar_dims[1] = {1};\n"
        "    PyObject *scalar = PyArray_SimpleNew(1, scalar_dims, NPY_INT);\n"
        "    if (scalar == NULL) { Py_DECREF(arr); return NULL; }\n"
        "    ((int *)PyArray_DATA((PyArrayObject *)scalar))[0] = 7;\n"
        "    PyObject *scalar_sq = PyArray_Squeeze((PyArrayObject *)scalar);\n"
        "    if (scalar_sq != NULL && PyArray_NDIM((PyArrayObject *)scalar_sq) == 0 &&\n"
        "        PyArray_BASE((PyArrayObject *)scalar_sq) == scalar &&\n"
        "        PyArray_DATA((PyArrayObject *)scalar_sq) == PyArray_DATA((PyArrayObject *)scalar) &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)scalar_sq))[0] == 7) score += 10;\n"
        "    Py_XDECREF(scalar_sq);\n"
        "    npy_intp full_dims[2] = {2, 3};\n"
        "    PyObject *full = PyArray_SimpleNew(2, full_dims, NPY_INT);\n"
        "    if (full == NULL) { Py_DECREF(scalar); Py_DECREF(arr); return NULL; }\n"
        "    PyObject *same = PyArray_Squeeze((PyArrayObject *)full);\n"
        "    if (same == full) score += 100;\n"
        "    Py_XDECREF(same);\n"
        "    Py_DECREF(full);\n"
        "    Py_DECREF(scalar);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_transpose_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[2] = {2, 3};\n"
        "    PyObject *arr = PyArray_SimpleNew(2, dims, NPY_INT);\n"
        "    if (arr == NULL) return NULL;\n"
        "    int *data = (int *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    data[0] = 1; data[1] = 2; data[2] = 3; data[3] = 4; data[4] = 5; data[5] = 6;\n"
        "    PyObject *tr = PyArray_Transpose((PyArrayObject *)arr, NULL);\n"
        "    if (tr != NULL && tr != arr && PyArray_BASE((PyArrayObject *)tr) == arr &&\n"
        "        PyArray_DATA((PyArrayObject *)tr) == PyArray_DATA((PyArrayObject *)arr) &&\n"
        "        PyArray_NDIM((PyArrayObject *)tr) == 2 && PyArray_DIMS((PyArrayObject *)tr)[0] == 3 &&\n"
        "        PyArray_DIMS((PyArrayObject *)tr)[1] == 2 &&\n"
        "        PyArray_STRIDES((PyArrayObject *)tr)[0] == (npy_intp)sizeof(int) &&\n"
        "        PyArray_STRIDES((PyArrayObject *)tr)[1] == 3 * (npy_intp)sizeof(int) &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)tr))[0] == 1) score += 1;\n"
        "    Py_XDECREF(tr);\n"
        "    npy_intp identity_axes[2] = {0, 1};\n"
        "    PyArray_Dims identity = {identity_axes, 2};\n"
        "    PyObject *same_order = PyArray_Transpose((PyArrayObject *)arr, &identity);\n"
        "    if (same_order != NULL && same_order != arr && PyArray_BASE((PyArrayObject *)same_order) == arr &&\n"
        "        PyArray_DIMS((PyArrayObject *)same_order)[0] == 2 && PyArray_DIMS((PyArrayObject *)same_order)[1] == 3 &&\n"
        "        PyArray_STRIDES((PyArrayObject *)same_order)[0] == 3 * (npy_intp)sizeof(int) &&\n"
        "        PyArray_STRIDES((PyArrayObject *)same_order)[1] == (npy_intp)sizeof(int)) score += 10;\n"
        "    Py_XDECREF(same_order);\n"
        "    npy_intp repeated_axes[2] = {0, 0};\n"
        "    PyArray_Dims repeated = {repeated_axes, 2};\n"
        "    PyObject *bad = PyArray_Transpose((PyArrayObject *)arr, &repeated);\n"
        "    if (bad == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100; }\n"
        "    Py_XDECREF(bad);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_swap_axes_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[2] = {2, 3};\n"
        "    PyObject *arr = PyArray_SimpleNew(2, dims, NPY_INT);\n"
        "    if (arr == NULL) return NULL;\n"
        "    int *data = (int *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    if (data == NULL) { Py_DECREF(arr); return NULL; }\n"
        "    data[0] = 1; data[1] = 2; data[2] = 3; data[3] = 4; data[4] = 5; data[5] = 6;\n"
        "    PyObject *swapped = PyArray_SwapAxes((PyArrayObject *)arr, 0, 1);\n"
        "    if (swapped != NULL && swapped != arr && PyArray_BASE((PyArrayObject *)swapped) == arr &&\n"
        "        PyArray_DATA((PyArrayObject *)swapped) == data && PyArray_NDIM((PyArrayObject *)swapped) == 2 &&\n"
        "        PyArray_DIMS((PyArrayObject *)swapped)[0] == 3 && PyArray_DIMS((PyArrayObject *)swapped)[1] == 2 &&\n"
        "        PyArray_STRIDES((PyArrayObject *)swapped)[0] == (npy_intp)sizeof(int) &&\n"
        "        PyArray_STRIDES((PyArrayObject *)swapped)[1] == 3 * (npy_intp)sizeof(int)) score += 1;\n"
        "    Py_XDECREF(swapped);\n"
        "    swapped = PyArray_SwapAxes((PyArrayObject *)arr, -1, 0);\n"
        "    if (swapped != NULL && PyArray_DIMS((PyArrayObject *)swapped)[0] == 3 &&\n"
        "        PyArray_DIMS((PyArrayObject *)swapped)[1] == 2 && PyArray_BASE((PyArrayObject *)swapped) == arr) score += 10;\n"
        "    Py_XDECREF(swapped);\n"
        "    swapped = PyArray_SwapAxes((PyArrayObject *)arr, 1, 1);\n"
        "    if (swapped != NULL && swapped != arr && PyArray_BASE((PyArrayObject *)swapped) == arr &&\n"
        "        PyArray_DIMS((PyArrayObject *)swapped)[0] == 2 && PyArray_DIMS((PyArrayObject *)swapped)[1] == 3 &&\n"
        "        PyArray_STRIDES((PyArrayObject *)swapped)[0] == 3 * (npy_intp)sizeof(int) &&\n"
        "        PyArray_STRIDES((PyArrayObject *)swapped)[1] == (npy_intp)sizeof(int)) score += 100;\n"
        "    Py_XDECREF(swapped);\n"
        "    npy_intp dims3[3] = {2, 3, 4};\n"
        "    PyObject *arr3 = PyArray_SimpleNew(3, dims3, NPY_INT);\n"
        "    if (arr3 == NULL) { Py_DECREF(arr); return NULL; }\n"
        "    swapped = PyArray_SwapAxes((PyArrayObject *)arr3, 0, 2);\n"
        "    if (swapped != NULL && PyArray_NDIM((PyArrayObject *)swapped) == 3 &&\n"
        "        PyArray_DIMS((PyArrayObject *)swapped)[0] == 4 && PyArray_DIMS((PyArrayObject *)swapped)[1] == 3 && PyArray_DIMS((PyArrayObject *)swapped)[2] == 2 &&\n"
        "        PyArray_STRIDES((PyArrayObject *)swapped)[0] == (npy_intp)sizeof(int) &&\n"
        "        PyArray_STRIDES((PyArrayObject *)swapped)[1] == 4 * (npy_intp)sizeof(int) &&\n"
        "        PyArray_STRIDES((PyArrayObject *)swapped)[2] == 12 * (npy_intp)sizeof(int) &&\n"
        "        PyArray_BASE((PyArrayObject *)swapped) == arr3) score += 1000;\n"
        "    Py_XDECREF(swapped);\n"
        "    swapped = PyArray_SwapAxes((PyArrayObject *)arr, 2, 0);\n"
        "    if (swapped == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    Py_XDECREF(swapped);\n"
        "    swapped = PyArray_SwapAxes((PyArrayObject *)Py_None, 0, 1);\n"
        "    if (swapped == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_XDECREF(swapped);\n"
        "    PyObject *tr = PyArray_Transpose((PyArrayObject *)arr, NULL);\n"
        "    if (tr == NULL) { Py_DECREF(arr3); Py_DECREF(arr); return NULL; }\n"
        "    swapped = PyArray_SwapAxes((PyArrayObject *)tr, 0, 1);\n"
        "    if (swapped == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000000; }\n"
        "    Py_XDECREF(swapped);\n"
        "    Py_DECREF(tr);\n"
        "    Py_DECREF(arr3);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_ravel_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[2] = {2, 3};\n"
        "    PyObject *arr = PyArray_SimpleNew(2, dims, NPY_INT);\n"
        "    if (arr == NULL) return NULL;\n"
        "    int *data = (int *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    data[0] = 1; data[1] = 2; data[2] = 3; data[3] = 4; data[4] = 5; data[5] = 6;\n"
        "    PyObject *flat = PyArray_Ravel((PyArrayObject *)arr, NPY_CORDER);\n"
        "    if (flat != NULL && flat != arr && PyArray_BASE((PyArrayObject *)flat) == arr &&\n"
        "        PyArray_DATA((PyArrayObject *)flat) == PyArray_DATA((PyArrayObject *)arr) &&\n"
        "        PyArray_NDIM((PyArrayObject *)flat) == 1 && PyArray_DIMS((PyArrayObject *)flat)[0] == 6 &&\n"
        "        PyArray_STRIDES((PyArrayObject *)flat)[0] == (npy_intp)sizeof(int) &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)flat))[5] == 6) score += 1;\n"
        "    Py_XDECREF(flat);\n"
        "    PyObject *any = PyArray_Ravel((PyArrayObject *)arr, NPY_ANYORDER);\n"
        "    if (any != NULL && PyArray_BASE((PyArrayObject *)any) == arr &&\n"
        "        PyArray_NDIM((PyArrayObject *)any) == 1 && PyArray_DIMS((PyArrayObject *)any)[0] == 6 &&\n"
        "        PyArray_STRIDES((PyArrayObject *)any)[0] == (npy_intp)sizeof(int)) score += 10;\n"
        "    Py_XDECREF(any);\n"
        "    PyObject *bad = PyArray_Ravel((PyArrayObject *)arr, NPY_FORTRANORDER);\n"
        "    if (bad == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100; }\n"
        "    Py_XDECREF(bad);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_flatten_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[2] = {2, 3};\n"
        "    PyObject *arr = PyArray_SimpleNew(2, dims, NPY_INT);\n"
        "    if (arr == NULL) return NULL;\n"
        "    int *data = (int *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    data[0] = 1; data[1] = 2; data[2] = 3; data[3] = 4; data[4] = 5; data[5] = 6;\n"
        "    PyObject *flat = PyArray_Flatten((PyArrayObject *)arr, NPY_CORDER);\n"
        "    if (flat != NULL && flat != arr && PyArray_BASE((PyArrayObject *)flat) == NULL &&\n"
        "        PyArray_DATA((PyArrayObject *)flat) != PyArray_DATA((PyArrayObject *)arr) &&\n"
        "        PyArray_NDIM((PyArrayObject *)flat) == 1 && PyArray_DIMS((PyArrayObject *)flat)[0] == 6 &&\n"
        "        PyArray_STRIDES((PyArrayObject *)flat)[0] == (npy_intp)sizeof(int) &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)flat))[0] == 1 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)flat))[5] == 6) score += 1;\n"
        "    data[0] = 99;\n"
        "    if (flat != NULL && ((int *)PyArray_DATA((PyArrayObject *)flat))[0] == 1) score += 10;\n"
        "    Py_XDECREF(flat);\n"
        "    PyObject *any = PyArray_Flatten((PyArrayObject *)arr, NPY_ANYORDER);\n"
        "    if (any != NULL && PyArray_BASE((PyArrayObject *)any) == NULL &&\n"
        "        PyArray_NDIM((PyArrayObject *)any) == 1 && PyArray_DIMS((PyArrayObject *)any)[0] == 6 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)any))[0] == 99) score += 100;\n"
        "    Py_XDECREF(any);\n"
        "    PyObject *bad = PyArray_Flatten((PyArrayObject *)arr, NPY_FORTRANORDER);\n"
        "    if (bad == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000; }\n"
        "    Py_XDECREF(bad);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_take_from_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[1] = {4};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (arr == NULL) return NULL;\n"
        "    int *data = (int *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    data[0] = 10; data[1] = 20; data[2] = 30; data[3] = 40;\n"
        "    npy_intp idx_dims[1] = {3};\n"
        "    PyObject *idx = PyArray_SimpleNew(1, idx_dims, NPY_LONG);\n"
        "    if (idx == NULL) { Py_DECREF(arr); return NULL; }\n"
        "    long *idx_data = (long *)PyArray_DATA((PyArrayObject *)idx);\n"
        "    idx_data[0] = 2; idx_data[1] = 0; idx_data[2] = -1;\n"
        "    PyObject *taken = PyArray_TakeFrom((PyArrayObject *)arr, idx, 0, NULL, NPY_RAISE);\n"
        "    if (taken != NULL && taken != arr && PyArray_BASE((PyArrayObject *)taken) == NULL &&\n"
        "        PyArray_DATA((PyArrayObject *)taken) != PyArray_DATA((PyArrayObject *)arr) &&\n"
        "        PyArray_NDIM((PyArrayObject *)taken) == 1 && PyArray_DIMS((PyArrayObject *)taken)[0] == 3 &&\n"
        "        PyArray_TYPE((PyArrayObject *)taken) == NPY_INT &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)taken))[0] == 30 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)taken))[1] == 10 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)taken))[2] == 40) score += 1;\n"
        "    data[2] = 99;\n"
        "    if (taken != NULL && ((int *)PyArray_DATA((PyArrayObject *)taken))[0] == 30) score += 10;\n"
        "    Py_XDECREF(taken);\n"
        "    idx_data[0] = 4; idx_data[1] = 0; idx_data[2] = 1;\n"
        "    PyObject *bad = PyArray_TakeFrom((PyArrayObject *)arr, idx, 0, NULL, NPY_RAISE);\n"
        "    if (bad == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100; }\n"
        "    Py_XDECREF(bad);\n"
        "    idx_data[0] = 1; idx_data[1] = 2; idx_data[2] = 3;\n"
        "    PyObject *clip = PyArray_TakeFrom((PyArrayObject *)arr, idx, 0, NULL, NPY_CLIP);\n"
        "    if (clip == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000; }\n"
        "    Py_XDECREF(clip);\n"
        "    Py_DECREF(idx);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_put_to_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[1] = {4};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (arr == NULL) return NULL;\n"
        "    int *data = (int *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    data[0] = 10; data[1] = 20; data[2] = 30; data[3] = 40;\n"
        "    npy_intp idx_dims[1] = {3};\n"
        "    PyObject *idx = PyArray_SimpleNew(1, idx_dims, NPY_LONG);\n"
        "    PyObject *vals = PyArray_SimpleNew(1, idx_dims, NPY_INT);\n"
        "    if (idx == NULL || vals == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        Py_XDECREF(idx);\n"
        "        Py_XDECREF(vals);\n"
        "        return NULL;\n"
        "    }\n"
        "    long *idx_data = (long *)PyArray_DATA((PyArrayObject *)idx);\n"
        "    int *val_data = (int *)PyArray_DATA((PyArrayObject *)vals);\n"
        "    idx_data[0] = 2; idx_data[1] = 0; idx_data[2] = -1;\n"
        "    val_data[0] = 300; val_data[1] = 100; val_data[2] = 400;\n"
        "    PyObject *ret = PyArray_PutTo((PyArrayObject *)arr, vals, idx, NPY_RAISE);\n"
        "    if (ret == Py_None && data[0] == 100 && data[1] == 20 && data[2] == 300 && data[3] == 400) score += 1;\n"
        "    Py_XDECREF(ret);\n"
        "    PyObject *scalar = PyLong_FromLong(7);\n"
        "    if (scalar == NULL) {\n"
        "        Py_DECREF(vals);\n"
        "        Py_DECREF(idx);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    ret = PyArray_PutTo((PyArrayObject *)arr, scalar, idx, NPY_RAISE);\n"
        "    if (ret == Py_None && data[0] == 7 && data[2] == 7 && data[3] == 7) score += 10;\n"
        "    Py_XDECREF(ret);\n"
        "    Py_DECREF(scalar);\n"
        "    idx_data[0] = 4; idx_data[1] = 0; idx_data[2] = 1;\n"
        "    ret = PyArray_PutTo((PyArrayObject *)arr, vals, idx, NPY_RAISE);\n"
        "    if (ret == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100; }\n"
        "    Py_XDECREF(ret);\n"
        "    idx_data[0] = 1; idx_data[1] = 2; idx_data[2] = 3;\n"
        "    ret = PyArray_PutTo((PyArrayObject *)arr, vals, idx, NPY_CLIP);\n"
        "    if (ret == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000; }\n"
        "    Py_XDECREF(ret);\n"
        "    Py_DECREF(vals);\n"
        "    Py_DECREF(idx);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_put_mask_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[1] = {5};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    PyObject *mask = PyArray_SimpleNew(1, dims, NPY_BOOL);\n"
        "    npy_intp val_dims[1] = {2};\n"
        "    PyObject *vals = PyArray_SimpleNew(1, val_dims, NPY_INT);\n"
        "    if (arr == NULL || mask == NULL || vals == NULL) {\n"
        "        Py_XDECREF(arr);\n"
        "        Py_XDECREF(mask);\n"
        "        Py_XDECREF(vals);\n"
        "        return NULL;\n"
        "    }\n"
        "    int *data = (int *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    unsigned char *mask_data = (unsigned char *)PyArray_DATA((PyArrayObject *)mask);\n"
        "    int *val_data = (int *)PyArray_DATA((PyArrayObject *)vals);\n"
        "    data[0] = 1; data[1] = 2; data[2] = 3; data[3] = 4; data[4] = 5;\n"
        "    mask_data[0] = 1; mask_data[1] = 1; mask_data[2] = 1; mask_data[3] = 0; mask_data[4] = 1;\n"
        "    val_data[0] = 10; val_data[1] = 20;\n"
        "    PyObject *ret = PyArray_PutMask((PyArrayObject *)arr, vals, mask);\n"
        "    if (ret == Py_None && data[0] == 10 && data[1] == 20 && data[2] == 10 && data[3] == 4 && data[4] == 10) score += 1;\n"
        "    Py_XDECREF(ret);\n"
        "    mask_data[0] = 0; mask_data[1] = 1; mask_data[2] = 0; mask_data[3] = 1; mask_data[4] = 0;\n"
        "    PyObject *scalar = PyLong_FromLong(7);\n"
        "    if (scalar == NULL) {\n"
        "        Py_DECREF(vals);\n"
        "        Py_DECREF(mask);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    ret = PyArray_PutMask((PyArrayObject *)arr, scalar, mask);\n"
        "    if (ret == Py_None && data[0] == 10 && data[1] == 7 && data[2] == 10 && data[3] == 7 && data[4] == 10) score += 10;\n"
        "    Py_XDECREF(ret);\n"
        "    Py_DECREF(scalar);\n"
        "    npy_intp bad_dims[1] = {4};\n"
        "    PyObject *bad_mask = PyArray_SimpleNew(1, bad_dims, NPY_BOOL);\n"
        "    ret = PyArray_PutMask((PyArrayObject *)arr, vals, bad_mask);\n"
        "    if (ret == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100; }\n"
        "    Py_XDECREF(ret);\n"
        "    Py_XDECREF(bad_mask);\n"
        "    Py_DECREF(vals);\n"
        "    Py_DECREF(mask);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_repeat_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[1] = {3};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    PyObject *reps = PyArray_SimpleNew(1, dims, NPY_LONG);\n"
        "    if (arr == NULL || reps == NULL) {\n"
        "        Py_XDECREF(arr);\n"
        "        Py_XDECREF(reps);\n"
        "        return NULL;\n"
        "    }\n"
        "    int *data = (int *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    long *rep_data = (long *)PyArray_DATA((PyArrayObject *)reps);\n"
        "    data[0] = 3; data[1] = 4; data[2] = 5;\n"
        "    rep_data[0] = 1; rep_data[1] = 2; rep_data[2] = 0;\n"
        "    PyObject *out = PyArray_Repeat((PyArrayObject *)arr, reps, 0);\n"
        "    if (out != NULL && out != arr && PyArray_BASE((PyArrayObject *)out) == NULL &&\n"
        "        PyArray_NDIM((PyArrayObject *)out) == 1 && PyArray_DIMS((PyArrayObject *)out)[0] == 3 &&\n"
        "        PyArray_TYPE((PyArrayObject *)out) == NPY_INT &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[0] == 3 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[1] == 4 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[2] == 4) score += 1;\n"
        "    data[1] = 99;\n"
        "    if (out != NULL && ((int *)PyArray_DATA((PyArrayObject *)out))[1] == 4) score += 10;\n"
        "    Py_XDECREF(out);\n"
        "    PyObject *scalar = PyLong_FromLong(2);\n"
        "    if (scalar == NULL) {\n"
        "        Py_DECREF(reps);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    out = PyArray_Repeat((PyArrayObject *)arr, scalar, -1);\n"
        "    if (out != NULL && PyArray_NDIM((PyArrayObject *)out) == 1 && PyArray_DIMS((PyArrayObject *)out)[0] == 6 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[0] == 3 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[1] == 3 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[2] == 99 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[3] == 99 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[4] == 5 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[5] == 5) score += 100;\n"
        "    Py_XDECREF(out);\n"
        "    Py_DECREF(scalar);\n"
        "    rep_data[0] = -1; rep_data[1] = 1; rep_data[2] = 1;\n"
        "    out = PyArray_Repeat((PyArrayObject *)arr, reps, 0);\n"
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000; }\n"
        "    Py_XDECREF(out);\n"
        "    Py_DECREF(reps);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_choose_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[1] = {4};\n"
        "    PyObject *idx = PyArray_SimpleNew(1, dims, NPY_LONG);\n"
        "    PyObject *left = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    PyObject *right = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (idx == NULL || left == NULL || right == NULL) {\n"
        "        Py_XDECREF(idx);\n"
        "        Py_XDECREF(left);\n"
        "        Py_XDECREF(right);\n"
        "        return NULL;\n"
        "    }\n"
        "    long *idx_data = (long *)PyArray_DATA((PyArrayObject *)idx);\n"
        "    int *left_data = (int *)PyArray_DATA((PyArrayObject *)left);\n"
        "    int *right_data = (int *)PyArray_DATA((PyArrayObject *)right);\n"
        "    idx_data[0] = 0; idx_data[1] = 1; idx_data[2] = 0; idx_data[3] = 1;\n"
        "    left_data[0] = 10; left_data[1] = 20; left_data[2] = 30; left_data[3] = 40;\n"
        "    right_data[0] = 100; right_data[1] = 200; right_data[2] = 300; right_data[3] = 400;\n"
        "    PyObject *choices = PyTuple_Pack(2, left, right);\n"
        "    if (choices == NULL) {\n"
        "        Py_DECREF(right);\n"
        "        Py_DECREF(left);\n"
        "        Py_DECREF(idx);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *out = PyArray_Choose((PyArrayObject *)idx, choices, NULL, NPY_RAISE);\n"
        "    if (out != NULL && out != left && out != right && PyArray_BASE((PyArrayObject *)out) == NULL &&\n"
        "        PyArray_NDIM((PyArrayObject *)out) == 1 && PyArray_DIMS((PyArrayObject *)out)[0] == 4 &&\n"
        "        PyArray_TYPE((PyArrayObject *)out) == NPY_INT &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[0] == 10 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[1] == 200 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[2] == 30 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[3] == 400) score += 1;\n"
        "    right_data[1] = 999;\n"
        "    if (out != NULL && ((int *)PyArray_DATA((PyArrayObject *)out))[1] == 200) score += 10;\n"
        "    Py_XDECREF(out);\n"
        "    idx_data[1] = 2;\n"
        "    out = PyArray_Choose((PyArrayObject *)idx, choices, NULL, NPY_RAISE);\n"
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100; }\n"
        "    Py_XDECREF(out);\n"
        "    idx_data[1] = 1;\n"
        "    out = PyArray_Choose((PyArrayObject *)idx, choices, NULL, NPY_CLIP);\n"
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000; }\n"
        "    Py_XDECREF(out);\n"
        "    Py_DECREF(choices);\n"
        "    Py_DECREF(right);\n"
        "    Py_DECREF(left);\n"
        "    Py_DECREF(idx);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_concatenate_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims2[1] = {2};\n"
        "    npy_intp dims3[1] = {3};\n"
        "    PyObject *left = PyArray_SimpleNew(1, dims2, NPY_INT);\n"
        "    PyObject *right = PyArray_SimpleNew(1, dims3, NPY_INT);\n"
        "    if (left == NULL || right == NULL) {\n"
        "        Py_XDECREF(left);\n"
        "        Py_XDECREF(right);\n"
        "        return NULL;\n"
        "    }\n"
        "    int *left_data = (int *)PyArray_DATA((PyArrayObject *)left);\n"
        "    int *right_data = (int *)PyArray_DATA((PyArrayObject *)right);\n"
        "    left_data[0] = 1; left_data[1] = 2;\n"
        "    right_data[0] = 3; right_data[1] = 4; right_data[2] = 5;\n"
        "    PyObject *seq = PyTuple_Pack(2, left, right);\n"
        "    if (seq == NULL) {\n"
        "        Py_DECREF(right);\n"
        "        Py_DECREF(left);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *out = PyArray_Concatenate(seq, 0);\n"
        "    if (out != NULL && out != left && out != right && PyArray_BASE((PyArrayObject *)out) == NULL &&\n"
        "        PyArray_NDIM((PyArrayObject *)out) == 1 && PyArray_DIMS((PyArrayObject *)out)[0] == 5 &&\n"
        "        PyArray_TYPE((PyArrayObject *)out) == NPY_INT &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[0] == 1 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[1] == 2 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[2] == 3 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[3] == 4 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[4] == 5) score += 1;\n"
        "    left_data[1] = 99;\n"
        "    right_data[0] = 88;\n"
        "    if (out != NULL &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[1] == 2 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[2] == 3) score += 10;\n"
        "    Py_XDECREF(out);\n"
        "    Py_DECREF(seq);\n"
        "    Py_DECREF(right);\n"
        "    Py_DECREF(left);\n"
        "\n"
        "    npy_intp row_dims[2] = {1, 2};\n"
        "    npy_intp matrix_dims[2] = {2, 2};\n"
        "    PyObject *top = PyArray_SimpleNew(2, row_dims, NPY_LONG);\n"
        "    PyObject *bottom = PyArray_SimpleNew(2, matrix_dims, NPY_LONG);\n"
        "    if (top == NULL || bottom == NULL) {\n"
        "        Py_XDECREF(top);\n"
        "        Py_XDECREF(bottom);\n"
        "        return NULL;\n"
        "    }\n"
        "    long *top_data = (long *)PyArray_DATA((PyArrayObject *)top);\n"
        "    long *bottom_data = (long *)PyArray_DATA((PyArrayObject *)bottom);\n"
        "    top_data[0] = 1; top_data[1] = 2;\n"
        "    bottom_data[0] = 3; bottom_data[1] = 4; bottom_data[2] = 5; bottom_data[3] = 6;\n"
        "    seq = PyTuple_Pack(2, top, bottom);\n"
        "    if (seq == NULL) {\n"
        "        Py_DECREF(bottom);\n"
        "        Py_DECREF(top);\n"
        "        return NULL;\n"
        "    }\n"
        "    out = PyArray_Concatenate(seq, 0);\n"
        "    if (out != NULL && PyArray_NDIM((PyArrayObject *)out) == 2 &&\n"
        "        PyArray_DIMS((PyArrayObject *)out)[0] == 3 && PyArray_DIMS((PyArrayObject *)out)[1] == 2 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)out))[0] == 1 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)out))[1] == 2 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)out))[2] == 3 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)out))[3] == 4 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)out))[4] == 5 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)out))[5] == 6) score += 100;\n"
        "    Py_XDECREF(out);\n"
        "    Py_DECREF(seq);\n"
        "    Py_DECREF(bottom);\n"
        "    Py_DECREF(top);\n"
        "\n"
        "    npy_intp wide_dims[2] = {2, 2};\n"
        "    npy_intp col_dims[2] = {2, 1};\n"
        "    PyObject *wide = PyArray_SimpleNew(2, wide_dims, NPY_INT);\n"
        "    PyObject *col = PyArray_SimpleNew(2, col_dims, NPY_INT);\n"
        "    if (wide == NULL || col == NULL) {\n"
        "        Py_XDECREF(wide);\n"
        "        Py_XDECREF(col);\n"
        "        return NULL;\n"
        "    }\n"
        "    int *wide_data = (int *)PyArray_DATA((PyArrayObject *)wide);\n"
        "    int *col_data = (int *)PyArray_DATA((PyArrayObject *)col);\n"
        "    wide_data[0] = 1; wide_data[1] = 2; wide_data[2] = 3; wide_data[3] = 4;\n"
        "    col_data[0] = 10; col_data[1] = 20;\n"
        "    seq = PyTuple_Pack(2, wide, col);\n"
        "    if (seq == NULL) {\n"
        "        Py_DECREF(col);\n"
        "        Py_DECREF(wide);\n"
        "        return NULL;\n"
        "    }\n"
        "    out = PyArray_Concatenate(seq, 1);\n"
        "    if (out != NULL && PyArray_NDIM((PyArrayObject *)out) == 2 &&\n"
        "        PyArray_DIMS((PyArrayObject *)out)[0] == 2 && PyArray_DIMS((PyArrayObject *)out)[1] == 3 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[0] == 1 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[1] == 2 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[2] == 10 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[3] == 3 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[4] == 4 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[5] == 20) score += 1000;\n"
        "    Py_XDECREF(out);\n"
        "    out = PyArray_Concatenate(seq, -1);\n"
        "    if (out != NULL && PyArray_NDIM((PyArrayObject *)out) == 2 &&\n"
        "        PyArray_DIMS((PyArrayObject *)out)[0] == 2 && PyArray_DIMS((PyArrayObject *)out)[1] == 3 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[2] == 10 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[5] == 20) score += 10000;\n"
        "    Py_XDECREF(out);\n"
        "    out = PyArray_Concatenate(seq, 0);\n"
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_XDECREF(out);\n"
        "    Py_DECREF(seq);\n"
        "\n"
        "    PyObject *double_arr = PyArray_SimpleNew(2, wide_dims, NPY_DOUBLE);\n"
        "    seq = PyTuple_Pack(2, wide, double_arr);\n"
        "    out = PyArray_Concatenate(seq, 0);\n"
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000000; }\n"
        "    Py_XDECREF(out);\n"
        "    Py_XDECREF(seq);\n"
        "    Py_XDECREF(double_arr);\n"
        "    PyObject *empty = PyTuple_New(0);\n"
        "    out = PyArray_Concatenate(empty, 0);\n"
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000000; }\n"
        "    Py_XDECREF(out);\n"
        "    Py_XDECREF(empty);\n"
        "    Py_DECREF(col);\n"
        "    Py_DECREF(wide);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_arange_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    PyObject *out = PyArray_Arange(1.0, 6.0, 2.0, NPY_INT);\n"
        "    if (out != NULL && PyArray_BASE((PyArrayObject *)out) == NULL &&\n"
        "        PyArray_NDIM((PyArrayObject *)out) == 1 && PyArray_DIMS((PyArrayObject *)out)[0] == 3 &&\n"
        "        PyArray_TYPE((PyArrayObject *)out) == NPY_INT &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[0] == 1 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[1] == 3 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[2] == 5) score += 1;\n"
        "    Py_XDECREF(out);\n"
        "    out = PyArray_Arange(0.0, 1.0, 0.25, NPY_DOUBLE);\n"
        "    if (out != NULL && PyArray_NDIM((PyArrayObject *)out) == 1 && PyArray_DIMS((PyArrayObject *)out)[0] == 4 &&\n"
        "        PyArray_TYPE((PyArrayObject *)out) == NPY_DOUBLE &&\n"
        "        ((double *)PyArray_DATA((PyArrayObject *)out))[0] == 0.0 &&\n"
        "        ((double *)PyArray_DATA((PyArrayObject *)out))[1] == 0.25 &&\n"
        "        ((double *)PyArray_DATA((PyArrayObject *)out))[2] == 0.5 &&\n"
        "        ((double *)PyArray_DATA((PyArrayObject *)out))[3] == 0.75) score += 10;\n"
        "    Py_XDECREF(out);\n"
        "    out = PyArray_Arange(5.0, 1.0, -2.0, NPY_LONG);\n"
        "    if (out != NULL && PyArray_NDIM((PyArrayObject *)out) == 1 && PyArray_DIMS((PyArrayObject *)out)[0] == 2 &&\n"
        "        PyArray_TYPE((PyArrayObject *)out) == NPY_LONG &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)out))[0] == 5 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)out))[1] == 3) score += 100;\n"
        "    Py_XDECREF(out);\n"
        "    out = PyArray_Arange(5.0, 1.0, 1.0, NPY_INT);\n"
        "    if (out != NULL && PyArray_NDIM((PyArrayObject *)out) == 1 && PyArray_DIMS((PyArrayObject *)out)[0] == 0 &&\n"
        "        PyArray_TYPE((PyArrayObject *)out) == NPY_INT) score += 1000;\n"
        "    Py_XDECREF(out);\n"
        "    out = PyArray_Arange(0.0, 1.0, 0.0, NPY_INT);\n"
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    Py_XDECREF(out);\n"
        "    out = PyArray_Arange(0.0, 2.0, 1.0, NPY_OBJECT);\n"
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_XDECREF(out);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_arange_obj_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    PyObject *start = PyLong_FromLong(2);\n"
        "    PyObject *stop = PyLong_FromLong(7);\n"
        "    PyObject *step = PyLong_FromLong(2);\n"
        "    if (start == NULL || stop == NULL || step == NULL) {\n"
        "        Py_XDECREF(start);\n"
        "        Py_XDECREF(stop);\n"
        "        Py_XDECREF(step);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *out = PyArray_ArangeObj(start, stop, step, PyArray_DescrFromType(NPY_INT));\n"
        "    if (out != NULL && PyArray_BASE((PyArrayObject *)out) == NULL &&\n"
        "        PyArray_NDIM((PyArrayObject *)out) == 1 && PyArray_DIMS((PyArrayObject *)out)[0] == 3 &&\n"
        "        PyArray_TYPE((PyArrayObject *)out) == NPY_INT &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[0] == 2 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[1] == 4 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[2] == 6) score += 1;\n"
        "    Py_XDECREF(out);\n"
        "    Py_DECREF(step);\n"
        "    Py_DECREF(stop);\n"
        "    Py_DECREF(start);\n"
        "\n"
        "    start = PyLong_FromLong(2);\n"
        "    stop = PyLong_FromLong(5);\n"
        "    if (start == NULL || stop == NULL) {\n"
        "        Py_XDECREF(start);\n"
        "        Py_XDECREF(stop);\n"
        "        return NULL;\n"
        "    }\n"
        "    out = PyArray_ArangeObj(start, stop, NULL, PyArray_DescrFromType(NPY_LONG));\n"
        "    if (out != NULL && PyArray_NDIM((PyArrayObject *)out) == 1 && PyArray_DIMS((PyArrayObject *)out)[0] == 3 &&\n"
        "        PyArray_TYPE((PyArrayObject *)out) == NPY_LONG &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)out))[0] == 2 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)out))[1] == 3 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)out))[2] == 4) score += 10;\n"
        "    Py_XDECREF(out);\n"
        "    Py_DECREF(stop);\n"
        "    Py_DECREF(start);\n"
        "\n"
        "    start = PyFloat_FromDouble(0.0);\n"
        "    stop = PyFloat_FromDouble(1.0);\n"
        "    step = PyFloat_FromDouble(0.5);\n"
        "    if (start == NULL || stop == NULL || step == NULL) {\n"
        "        Py_XDECREF(start);\n"
        "        Py_XDECREF(stop);\n"
        "        Py_XDECREF(step);\n"
        "        return NULL;\n"
        "    }\n"
        "    out = PyArray_ArangeObj(start, stop, step, NULL);\n"
        "    if (out != NULL && PyArray_NDIM((PyArrayObject *)out) == 1 && PyArray_DIMS((PyArrayObject *)out)[0] == 2 &&\n"
        "        PyArray_TYPE((PyArrayObject *)out) == NPY_DOUBLE &&\n"
        "        ((double *)PyArray_DATA((PyArrayObject *)out))[0] == 0.0 &&\n"
        "        ((double *)PyArray_DATA((PyArrayObject *)out))[1] == 0.5) score += 100;\n"
        "    Py_XDECREF(out);\n"
        "    Py_DECREF(step);\n"
        "    Py_DECREF(stop);\n"
        "    Py_DECREF(start);\n"
        "\n"
        "    start = PyLong_FromLong(5);\n"
        "    stop = PyLong_FromLong(1);\n"
        "    step = PyLong_FromLong(-2);\n"
        "    if (start == NULL || stop == NULL || step == NULL) {\n"
        "        Py_XDECREF(start);\n"
        "        Py_XDECREF(stop);\n"
        "        Py_XDECREF(step);\n"
        "        return NULL;\n"
        "    }\n"
        "    out = PyArray_ArangeObj(start, stop, step, NULL);\n"
        "    if (out != NULL && PyArray_NDIM((PyArrayObject *)out) == 1 && PyArray_DIMS((PyArrayObject *)out)[0] == 2 &&\n"
        "        PyArray_TYPE((PyArrayObject *)out) == NPY_LONG &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)out))[0] == 5 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)out))[1] == 3) score += 1000;\n"
        "    Py_XDECREF(out);\n"
        "    Py_DECREF(step);\n"
        "    Py_DECREF(stop);\n"
        "    Py_DECREF(start);\n"
        "\n"
        "    start = PyLong_FromLong(0);\n"
        "    stop = PyLong_FromLong(3);\n"
        "    step = PyLong_FromLong(0);\n"
        "    if (start == NULL || stop == NULL || step == NULL) {\n"
        "        Py_XDECREF(start);\n"
        "        Py_XDECREF(stop);\n"
        "        Py_XDECREF(step);\n"
        "        return NULL;\n"
        "    }\n"
        "    out = PyArray_ArangeObj(start, stop, step, NULL);\n"
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    Py_XDECREF(out);\n"
        "    Py_DECREF(step);\n"
        "    Py_DECREF(stop);\n"
        "    Py_DECREF(start);\n"
        "\n"
        "    start = PyLong_FromLong(0);\n"
        "    stop = PyLong_FromLong(2);\n"
        "    if (start == NULL || stop == NULL) {\n"
        "        Py_XDECREF(start);\n"
        "        Py_XDECREF(stop);\n"
        "        return NULL;\n"
        "    }\n"
        "    out = PyArray_ArangeObj(start, stop, NULL, PyArray_DescrFromType(NPY_OBJECT));\n"
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_XDECREF(out);\n"
        "    Py_DECREF(stop);\n"
        "    Py_DECREF(start);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_inner_product_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims3[1] = {3};\n"
        "    PyObject *lhs = PyArray_SimpleNew(1, dims3, NPY_INT);\n"
        "    PyObject *rhs = PyArray_SimpleNew(1, dims3, NPY_INT);\n"
        "    if (lhs == NULL || rhs == NULL) {\n"
        "        Py_XDECREF(lhs);\n"
        "        Py_XDECREF(rhs);\n"
        "        return NULL;\n"
        "    }\n"
        "    int *lhs_i = (int *)PyArray_DATA((PyArrayObject *)lhs);\n"
        "    int *rhs_i = (int *)PyArray_DATA((PyArrayObject *)rhs);\n"
        "    lhs_i[0] = 1; lhs_i[1] = 2; lhs_i[2] = 3;\n"
        "    rhs_i[0] = 4; rhs_i[1] = 5; rhs_i[2] = 6;\n"
        "    PyObject *out = PyArray_InnerProduct(lhs, rhs);\n"
        "    if (out != NULL && PyLong_AsLong(out) == 32) score += 1;\n"
        "    Py_XDECREF(out);\n"
        "\n"
        "    PyObject *dlhs = PyArray_SimpleNew(1, dims3, NPY_DOUBLE);\n"
        "    PyObject *drhs = PyArray_SimpleNew(1, dims3, NPY_DOUBLE);\n"
        "    if (dlhs == NULL || drhs == NULL) {\n"
        "        Py_XDECREF(dlhs);\n"
        "        Py_XDECREF(drhs);\n"
        "        Py_DECREF(rhs);\n"
        "        Py_DECREF(lhs);\n"
        "        return NULL;\n"
        "    }\n"
        "    double *lhs_d = (double *)PyArray_DATA((PyArrayObject *)dlhs);\n"
        "    double *rhs_d = (double *)PyArray_DATA((PyArrayObject *)drhs);\n"
        "    lhs_d[0] = 1.5; lhs_d[1] = 2.0; lhs_d[2] = -1.0;\n"
        "    rhs_d[0] = 2.0; rhs_d[1] = 3.0; rhs_d[2] = 4.0;\n"
        "    out = PyArray_InnerProduct(dlhs, drhs);\n"
        "    if (out != NULL && PyFloat_AsDouble(out) > 4.99 && PyFloat_AsDouble(out) < 5.01) score += 10;\n"
        "    Py_XDECREF(out);\n"
        "\n"
        "    npy_intp dims2[1] = {2};\n"
        "    PyObject *short_arr = PyArray_SimpleNew(1, dims2, NPY_INT);\n"
        "    out = short_arr != NULL ? PyArray_InnerProduct(lhs, short_arr) : NULL;\n"
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100; }\n"
        "    Py_XDECREF(out);\n"
        "    Py_XDECREF(short_arr);\n"
        "\n"
        "    npy_intp dims2d[2] = {1, 3};\n"
        "    PyObject *matrix = PyArray_SimpleNew(2, dims2d, NPY_INT);\n"
        "    out = matrix != NULL ? PyArray_InnerProduct(matrix, rhs) : NULL;\n"
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000; }\n"
        "    Py_XDECREF(out);\n"
        "    Py_XDECREF(matrix);\n"
        "\n"
        "    PyObject *objarr = PyArray_SimpleNew(1, dims3, NPY_OBJECT);\n"
        "    out = objarr != NULL ? PyArray_InnerProduct(objarr, rhs) : NULL;\n"
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    Py_XDECREF(out);\n"
        "    Py_XDECREF(objarr);\n"
        "\n"
        "    PyObject *carr = PyArray_SimpleNew(1, dims3, NPY_CDOUBLE);\n"
        "    out = carr != NULL ? PyArray_InnerProduct(carr, rhs) : NULL;\n"
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_XDECREF(out);\n"
        "    Py_XDECREF(carr);\n"
        "\n"
        "    Py_DECREF(drhs);\n"
        "    Py_DECREF(dlhs);\n"
        "    Py_DECREF(rhs);\n"
        "    Py_DECREF(lhs);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_matrix_product_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims3[1] = {3};\n"
        "    PyObject *vec_lhs = PyArray_SimpleNew(1, dims3, NPY_INT);\n"
        "    PyObject *vec_rhs = PyArray_SimpleNew(1, dims3, NPY_INT);\n"
        "    if (vec_lhs == NULL || vec_rhs == NULL) {\n"
        "        Py_XDECREF(vec_lhs);\n"
        "        Py_XDECREF(vec_rhs);\n"
        "        return NULL;\n"
        "    }\n"
        "    int *vec_lhs_i = (int *)PyArray_DATA((PyArrayObject *)vec_lhs);\n"
        "    int *vec_rhs_i = (int *)PyArray_DATA((PyArrayObject *)vec_rhs);\n"
        "    vec_lhs_i[0] = 1; vec_lhs_i[1] = 2; vec_lhs_i[2] = 3;\n"
        "    vec_rhs_i[0] = 4; vec_rhs_i[1] = 5; vec_rhs_i[2] = 6;\n"
        "    PyObject *out = PyArray_MatrixProduct(vec_lhs, vec_rhs);\n"
        "    if (out != NULL && PyLong_AsLong(out) == 32) score += 1;\n"
        "    Py_XDECREF(out);\n"
        "\n"
        "    npy_intp lhs_dims[2] = {2, 3};\n"
        "    npy_intp rhs_dims[2] = {3, 2};\n"
        "    PyObject *mat_lhs = PyArray_SimpleNew(2, lhs_dims, NPY_INT);\n"
        "    PyObject *mat_rhs = PyArray_SimpleNew(2, rhs_dims, NPY_INT);\n"
        "    if (mat_lhs == NULL || mat_rhs == NULL) {\n"
        "        Py_XDECREF(mat_lhs);\n"
        "        Py_XDECREF(mat_rhs);\n"
        "        Py_DECREF(vec_rhs);\n"
        "        Py_DECREF(vec_lhs);\n"
        "        return NULL;\n"
        "    }\n"
        "    int *mat_lhs_i = (int *)PyArray_DATA((PyArrayObject *)mat_lhs);\n"
        "    int *mat_rhs_i = (int *)PyArray_DATA((PyArrayObject *)mat_rhs);\n"
        "    for (int i = 0; i < 6; i++) { mat_lhs_i[i] = i + 1; mat_rhs_i[i] = i + 7; }\n"
        "    out = PyArray_MatrixProduct(mat_lhs, mat_rhs);\n"
        "    if (out != NULL && PyArray_NDIM((PyArrayObject *)out) == 2 &&\n"
        "        PyArray_DIMS((PyArrayObject *)out)[0] == 2 && PyArray_DIMS((PyArrayObject *)out)[1] == 2 &&\n"
        "        PyArray_TYPE((PyArrayObject *)out) == NPY_LONG) {\n"
        "        long *prod = (long *)PyArray_DATA((PyArrayObject *)out);\n"
        "        if (prod[0] == 58 && prod[1] == 64 && prod[2] == 139 && prod[3] == 154) score += 10;\n"
        "    }\n"
        "    Py_XDECREF(out);\n"
        "\n"
        "    out = PyArray_MatrixProduct(vec_lhs, mat_rhs);\n"
        "    if (out != NULL && PyArray_NDIM((PyArrayObject *)out) == 1 &&\n"
        "        PyArray_DIMS((PyArrayObject *)out)[0] == 2 && PyArray_TYPE((PyArrayObject *)out) == NPY_LONG) {\n"
        "        long *prod = (long *)PyArray_DATA((PyArrayObject *)out);\n"
        "        if (prod[0] == 58 && prod[1] == 64) score += 1000000;\n"
        "    }\n"
        "    Py_XDECREF(out);\n"
        "\n"
        "    out = PyArray_MatrixProduct(mat_lhs, vec_rhs);\n"
        "    if (out != NULL && PyArray_NDIM((PyArrayObject *)out) == 1 &&\n"
        "        PyArray_DIMS((PyArrayObject *)out)[0] == 2 && PyArray_TYPE((PyArrayObject *)out) == NPY_LONG) {\n"
        "        long *prod = (long *)PyArray_DATA((PyArrayObject *)out);\n"
        "        if (prod[0] == 32 && prod[1] == 77) score += 10000000;\n"
        "    }\n"
        "    Py_XDECREF(out);\n"
        "\n"
        "    PyObject *mat_rhs_d = PyArray_SimpleNew(2, rhs_dims, NPY_DOUBLE);\n"
        "    if (mat_rhs_d == NULL) {\n"
        "        Py_DECREF(mat_rhs);\n"
        "        Py_DECREF(mat_lhs);\n"
        "        Py_DECREF(vec_rhs);\n"
        "        Py_DECREF(vec_lhs);\n"
        "        return NULL;\n"
        "    }\n"
        "    double *mat_rhs_double = (double *)PyArray_DATA((PyArrayObject *)mat_rhs_d);\n"
        "    mat_rhs_double[0] = 0.5; mat_rhs_double[1] = 1.0; mat_rhs_double[2] = 1.5;\n"
        "    mat_rhs_double[3] = 2.0; mat_rhs_double[4] = 2.5; mat_rhs_double[5] = 3.0;\n"
        "    out = PyArray_MatrixProduct(mat_lhs, mat_rhs_d);\n"
        "    if (out != NULL && PyArray_TYPE((PyArrayObject *)out) == NPY_DOUBLE) {\n"
        "        double *prod_d = (double *)PyArray_DATA((PyArrayObject *)out);\n"
        "        if (prod_d[0] > 10.9 && prod_d[0] < 11.1 && prod_d[3] > 31.9 && prod_d[3] < 32.1) score += 100;\n"
        "    }\n"
        "    Py_XDECREF(out);\n"
        "\n"
        "    npy_intp bad_rhs_dims[2] = {2, 2};\n"
        "    PyObject *bad_rhs = PyArray_SimpleNew(2, bad_rhs_dims, NPY_INT);\n"
        "    out = bad_rhs != NULL ? PyArray_MatrixProduct(mat_lhs, bad_rhs) : NULL;\n"
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000; }\n"
        "    Py_XDECREF(out);\n"
        "    Py_XDECREF(bad_rhs);\n"
        "\n"
        "    npy_intp dims3d[3] = {1, 1, 3};\n"
        "    PyObject *tensor = PyArray_SimpleNew(3, dims3d, NPY_INT);\n"
        "    out = tensor != NULL ? PyArray_MatrixProduct(tensor, vec_rhs) : NULL;\n"
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    Py_XDECREF(out);\n"
        "    Py_XDECREF(tensor);\n"
        "\n"
        "    PyObject *objarr = PyArray_SimpleNew(2, lhs_dims, NPY_OBJECT);\n"
        "    out = objarr != NULL ? PyArray_MatrixProduct(objarr, mat_rhs) : NULL;\n"
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_XDECREF(out);\n"
        "    Py_XDECREF(objarr);\n"
        "\n"
        "    Py_DECREF(mat_rhs_d);\n"
        "    Py_DECREF(mat_rhs);\n"
        "    Py_DECREF(mat_lhs);\n"
        "    Py_DECREF(vec_rhs);\n"
        "    Py_DECREF(vec_lhs);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_matrix_product2_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims3[1] = {3};\n"
        "    PyObject *vec_lhs = PyArray_SimpleNew(1, dims3, NPY_INT);\n"
        "    PyObject *vec_rhs = PyArray_SimpleNew(1, dims3, NPY_INT);\n"
        "    if (vec_lhs == NULL || vec_rhs == NULL) {\n"
        "        Py_XDECREF(vec_lhs);\n"
        "        Py_XDECREF(vec_rhs);\n"
        "        return NULL;\n"
        "    }\n"
        "    int *vec_lhs_i = (int *)PyArray_DATA((PyArrayObject *)vec_lhs);\n"
        "    int *vec_rhs_i = (int *)PyArray_DATA((PyArrayObject *)vec_rhs);\n"
        "    vec_lhs_i[0] = 1; vec_lhs_i[1] = 2; vec_lhs_i[2] = 3;\n"
        "    vec_rhs_i[0] = 4; vec_rhs_i[1] = 5; vec_rhs_i[2] = 6;\n"
        "    PyObject *out = PyArray_MatrixProduct2(vec_lhs, vec_rhs, NULL);\n"
        "    if (out != NULL && PyLong_AsLong(out) == 32) score += 1;\n"
        "    Py_XDECREF(out);\n"
        "\n"
        "    npy_intp lhs_dims[2] = {2, 3};\n"
        "    npy_intp rhs_dims[2] = {3, 2};\n"
        "    PyObject *mat_lhs = PyArray_SimpleNew(2, lhs_dims, NPY_INT);\n"
        "    PyObject *mat_rhs = PyArray_SimpleNew(2, rhs_dims, NPY_INT);\n"
        "    if (mat_lhs == NULL || mat_rhs == NULL) {\n"
        "        Py_XDECREF(mat_lhs);\n"
        "        Py_XDECREF(mat_rhs);\n"
        "        Py_DECREF(vec_rhs);\n"
        "        Py_DECREF(vec_lhs);\n"
        "        return NULL;\n"
        "    }\n"
        "    int *mat_lhs_i = (int *)PyArray_DATA((PyArrayObject *)mat_lhs);\n"
        "    int *mat_rhs_i = (int *)PyArray_DATA((PyArrayObject *)mat_rhs);\n"
        "    for (int i = 0; i < 6; i++) { mat_lhs_i[i] = i + 1; mat_rhs_i[i] = i + 7; }\n"
        "    out = PyArray_MatrixProduct2(mat_lhs, mat_rhs, NULL);\n"
        "    if (out != NULL && PyArray_NDIM((PyArrayObject *)out) == 2 &&\n"
        "        PyArray_DIMS((PyArrayObject *)out)[0] == 2 && PyArray_DIMS((PyArrayObject *)out)[1] == 2 &&\n"
        "        PyArray_TYPE((PyArrayObject *)out) == NPY_LONG) {\n"
        "        long *prod = (long *)PyArray_DATA((PyArrayObject *)out);\n"
        "        if (prod[0] == 58 && prod[1] == 64 && prod[2] == 139 && prod[3] == 154) score += 10;\n"
        "    }\n"
        "    Py_XDECREF(out);\n"
        "\n"
        "    out = PyArray_MatrixProduct2(vec_lhs, mat_rhs, NULL);\n"
        "    if (out != NULL && PyArray_NDIM((PyArrayObject *)out) == 1 &&\n"
        "        PyArray_DIMS((PyArrayObject *)out)[0] == 2 && PyArray_TYPE((PyArrayObject *)out) == NPY_LONG) {\n"
        "        long *prod = (long *)PyArray_DATA((PyArrayObject *)out);\n"
        "        if (prod[0] == 58 && prod[1] == 64) score += 1000000;\n"
        "    }\n"
        "    Py_XDECREF(out);\n"
        "\n"
        "    out = PyArray_MatrixProduct2(mat_lhs, vec_rhs, NULL);\n"
        "    if (out != NULL && PyArray_NDIM((PyArrayObject *)out) == 1 &&\n"
        "        PyArray_DIMS((PyArrayObject *)out)[0] == 2 && PyArray_TYPE((PyArrayObject *)out) == NPY_LONG) {\n"
        "        long *prod = (long *)PyArray_DATA((PyArrayObject *)out);\n"
        "        if (prod[0] == 32 && prod[1] == 77) score += 10000000;\n"
        "    }\n"
        "    Py_XDECREF(out);\n"
        "\n"
        "    PyObject *mat_rhs_d = PyArray_SimpleNew(2, rhs_dims, NPY_DOUBLE);\n"
        "    if (mat_rhs_d == NULL) {\n"
        "        Py_DECREF(mat_rhs);\n"
        "        Py_DECREF(mat_lhs);\n"
        "        Py_DECREF(vec_rhs);\n"
        "        Py_DECREF(vec_lhs);\n"
        "        return NULL;\n"
        "    }\n"
        "    double *mat_rhs_double = (double *)PyArray_DATA((PyArrayObject *)mat_rhs_d);\n"
        "    mat_rhs_double[0] = 0.5; mat_rhs_double[1] = 1.0; mat_rhs_double[2] = 1.5;\n"
        "    mat_rhs_double[3] = 2.0; mat_rhs_double[4] = 2.5; mat_rhs_double[5] = 3.0;\n"
        "    out = PyArray_MatrixProduct2(mat_lhs, mat_rhs_d, NULL);\n"
        "    if (out != NULL && PyArray_TYPE((PyArrayObject *)out) == NPY_DOUBLE) {\n"
        "        double *prod_d = (double *)PyArray_DATA((PyArrayObject *)out);\n"
        "        if (prod_d[0] > 10.9 && prod_d[0] < 11.1 && prod_d[3] > 31.9 && prod_d[3] < 32.1) score += 100;\n"
        "    }\n"
        "    Py_XDECREF(out);\n"
        "\n"
        "    npy_intp out_dims[2] = {2, 2};\n"
        "    PyObject *out_arr = PyArray_SimpleNew(2, out_dims, NPY_LONG);\n"
        "    out = out_arr != NULL ? PyArray_MatrixProduct2(mat_lhs, mat_rhs, (PyArrayObject *)out_arr) : NULL;\n"
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000; }\n"
        "    Py_XDECREF(out);\n"
        "    Py_XDECREF(out_arr);\n"
        "\n"
        "    npy_intp bad_rhs_dims[2] = {2, 2};\n"
        "    PyObject *bad_rhs = PyArray_SimpleNew(2, bad_rhs_dims, NPY_INT);\n"
        "    out = bad_rhs != NULL ? PyArray_MatrixProduct2(mat_lhs, bad_rhs, NULL) : NULL;\n"
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    Py_XDECREF(out);\n"
        "    Py_XDECREF(bad_rhs);\n"
        "\n"
        "    PyObject *objarr = PyArray_SimpleNew(2, lhs_dims, NPY_OBJECT);\n"
        "    out = objarr != NULL ? PyArray_MatrixProduct2(objarr, mat_rhs, NULL) : NULL;\n"
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_XDECREF(out);\n"
        "    Py_XDECREF(objarr);\n"
        "\n"
        "    Py_DECREF(mat_rhs_d);\n"
        "    Py_DECREF(mat_rhs);\n"
        "    Py_DECREF(mat_lhs);\n"
        "    Py_DECREF(vec_rhs);\n"
        "    Py_DECREF(vec_lhs);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_einstein_sum_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims3[1] = {3};\n"
        "    PyObject *vec_lhs = PyArray_SimpleNew(1, dims3, NPY_INT);\n"
        "    PyObject *vec_rhs = PyArray_SimpleNew(1, dims3, NPY_INT);\n"
        "    if (vec_lhs == NULL || vec_rhs == NULL) {\n"
        "        Py_XDECREF(vec_lhs);\n"
        "        Py_XDECREF(vec_rhs);\n"
        "        return NULL;\n"
        "    }\n"
        "    int *vec_lhs_i = (int *)PyArray_DATA((PyArrayObject *)vec_lhs);\n"
        "    int *vec_rhs_i = (int *)PyArray_DATA((PyArrayObject *)vec_rhs);\n"
        "    vec_lhs_i[0] = 1; vec_lhs_i[1] = 2; vec_lhs_i[2] = 3;\n"
        "    vec_rhs_i[0] = 10; vec_rhs_i[1] = 20; vec_rhs_i[2] = 30;\n"
        "    PyArrayObject *ops[2] = {(PyArrayObject *)vec_lhs, (PyArrayObject *)vec_rhs};\n"
        '    PyObject *out = (PyObject *)PyArray_EinsteinSum("i,i->", 2, ops, NULL, NPY_KEEPORDER, NPY_SAFE_CASTING, NULL);\n'
        "    if (out != NULL && PyLong_AsLong(out) == 140) score += 1;\n"
        "    Py_XDECREF(out);\n"
        "\n"
        "    npy_intp lhs_dims[2] = {2, 2};\n"
        "    npy_intp rhs_dims[2] = {2, 2};\n"
        "    PyObject *mat_lhs = PyArray_SimpleNew(2, lhs_dims, NPY_INT);\n"
        "    PyObject *mat_rhs = PyArray_SimpleNew(2, rhs_dims, NPY_INT);\n"
        "    if (mat_lhs == NULL || mat_rhs == NULL) {\n"
        "        Py_XDECREF(mat_lhs);\n"
        "        Py_XDECREF(mat_rhs);\n"
        "        Py_DECREF(vec_rhs);\n"
        "        Py_DECREF(vec_lhs);\n"
        "        return NULL;\n"
        "    }\n"
        "    int *mat_lhs_i = (int *)PyArray_DATA((PyArrayObject *)mat_lhs);\n"
        "    int *mat_rhs_i = (int *)PyArray_DATA((PyArrayObject *)mat_rhs);\n"
        "    mat_lhs_i[0] = 1; mat_lhs_i[1] = 2; mat_lhs_i[2] = 3; mat_lhs_i[3] = 4;\n"
        "    mat_rhs_i[0] = 5; mat_rhs_i[1] = 6; mat_rhs_i[2] = 7; mat_rhs_i[3] = 8;\n"
        "    ops[0] = (PyArrayObject *)mat_lhs;\n"
        "    ops[1] = (PyArrayObject *)mat_rhs;\n"
        '    out = (PyObject *)PyArray_EinsteinSum("ij,jk->ik", 2, ops, NULL, NPY_KEEPORDER, NPY_SAFE_CASTING, NULL);\n'
        "    if (out != NULL && PyArray_NDIM((PyArrayObject *)out) == 2 &&\n"
        "        PyArray_DIMS((PyArrayObject *)out)[0] == 2 && PyArray_DIMS((PyArrayObject *)out)[1] == 2 &&\n"
        "        PyArray_TYPE((PyArrayObject *)out) == NPY_LONG) {\n"
        "        long *prod = (long *)PyArray_DATA((PyArrayObject *)out);\n"
        "        if (prod[0] == 19 && prod[1] == 22 && prod[2] == 43 && prod[3] == 50) score += 10;\n"
        "    }\n"
        "    Py_XDECREF(out);\n"
        "\n"
        "    npy_intp dims2[1] = {2};\n"
        "    PyObject *vec2 = PyArray_SimpleNew(1, dims2, NPY_INT);\n"
        "    if (vec2 == NULL) {\n"
        "        Py_DECREF(mat_rhs);\n"
        "        Py_DECREF(mat_lhs);\n"
        "        Py_DECREF(vec_rhs);\n"
        "        Py_DECREF(vec_lhs);\n"
        "        return NULL;\n"
        "    }\n"
        "    int *vec2_i = (int *)PyArray_DATA((PyArrayObject *)vec2);\n"
        "    vec2_i[0] = 1; vec2_i[1] = 2;\n"
        "    ops[0] = (PyArrayObject *)vec2;\n"
        "    ops[1] = (PyArrayObject *)mat_rhs;\n"
        '    out = (PyObject *)PyArray_EinsteinSum("i,ij->j", 2, ops, NULL, NPY_KEEPORDER, NPY_SAFE_CASTING, NULL);\n'
        "    if (out != NULL && PyArray_NDIM((PyArrayObject *)out) == 1 &&\n"
        "        PyArray_DIMS((PyArrayObject *)out)[0] == 2 && PyArray_TYPE((PyArrayObject *)out) == NPY_LONG) {\n"
        "        long *prod = (long *)PyArray_DATA((PyArrayObject *)out);\n"
        "        if (prod[0] == 19 && prod[1] == 22) score += 100000;\n"
        "    }\n"
        "    Py_XDECREF(out);\n"
        "\n"
        "    ops[0] = (PyArrayObject *)mat_lhs;\n"
        "    ops[1] = (PyArrayObject *)vec2;\n"
        '    out = (PyObject *)PyArray_EinsteinSum("ij,j->i", 2, ops, NULL, NPY_KEEPORDER, NPY_SAFE_CASTING, NULL);\n'
        "    if (out != NULL && PyArray_NDIM((PyArrayObject *)out) == 1 &&\n"
        "        PyArray_DIMS((PyArrayObject *)out)[0] == 2 && PyArray_TYPE((PyArrayObject *)out) == NPY_LONG) {\n"
        "        long *prod = (long *)PyArray_DATA((PyArrayObject *)out);\n"
        "        if (prod[0] == 5 && prod[1] == 11) score += 1000000;\n"
        "    }\n"
        "    Py_XDECREF(out);\n"
        "\n"
        "    ops[0] = (PyArrayObject *)vec_lhs;\n"
        '    out = (PyObject *)PyArray_EinsteinSum("i->", 1, ops, NULL, NPY_KEEPORDER, NPY_SAFE_CASTING, NULL);\n'
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100; }\n"
        "    Py_XDECREF(out);\n"
        "\n"
        "    PyObject *out_arr = PyArray_SimpleNew(2, lhs_dims, NPY_LONG);\n"
        "    ops[0] = (PyArrayObject *)mat_lhs;\n"
        "    ops[1] = (PyArrayObject *)mat_rhs;\n"
        '    out = out_arr != NULL ? (PyObject *)PyArray_EinsteinSum("ij,jk->ik", 2, ops, NULL, NPY_KEEPORDER, NPY_SAFE_CASTING, (PyArrayObject *)out_arr) : NULL;\n'
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000; }\n"
        "    Py_XDECREF(out);\n"
        "    Py_XDECREF(out_arr);\n"
        "\n"
        "    PyArray_Descr *descr = PyArray_DescrFromType(NPY_DOUBLE);\n"
        "    ops[0] = (PyArrayObject *)vec_lhs;\n"
        "    ops[1] = (PyArrayObject *)vec_rhs;\n"
        '    out = descr != NULL ? (PyObject *)PyArray_EinsteinSum("i,i->", 2, ops, descr, NPY_KEEPORDER, NPY_SAFE_CASTING, NULL) : NULL;\n'
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    Py_XDECREF(out);\n"
        "    Py_XDECREF(descr);\n"
        "\n"
        "    Py_DECREF(vec2);\n"
        "    Py_DECREF(mat_rhs);\n"
        "    Py_DECREF(mat_lhs);\n"
        "    Py_DECREF(vec_rhs);\n"
        "    Py_DECREF(vec_lhs);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_correlate_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims3[1] = {3};\n"
        "    npy_intp dims2[1] = {2};\n"
        "    PyObject *a = PyArray_SimpleNew(1, dims3, NPY_INT);\n"
        "    PyObject *b = PyArray_SimpleNew(1, dims2, NPY_INT);\n"
        "    if (a == NULL || b == NULL) {\n"
        "        Py_XDECREF(a);\n"
        "        Py_XDECREF(b);\n"
        "        return NULL;\n"
        "    }\n"
        "    int *ai = (int *)PyArray_DATA((PyArrayObject *)a);\n"
        "    int *bi = (int *)PyArray_DATA((PyArrayObject *)b);\n"
        "    ai[0] = 1; ai[1] = 2; ai[2] = 3;\n"
        "    bi[0] = 4; bi[1] = 5;\n"
        "    PyObject *out = PyArray_Correlate(a, b, NPY_VALID);\n"
        "    if (out != NULL && PyArray_NDIM((PyArrayObject *)out) == 1 &&\n"
        "        PyArray_DIMS((PyArrayObject *)out)[0] == 2 && PyArray_TYPE((PyArrayObject *)out) == NPY_LONG) {\n"
        "        long *vals = (long *)PyArray_DATA((PyArrayObject *)out);\n"
        "        if (vals[0] == 14 && vals[1] == 23) score += 1;\n"
        "    }\n"
        "    Py_XDECREF(out);\n"
        "\n"
        "    out = PyArray_Correlate(a, b, NPY_SAME);\n"
        "    if (out != NULL && PyArray_DIMS((PyArrayObject *)out)[0] == 3) {\n"
        "        long *vals = (long *)PyArray_DATA((PyArrayObject *)out);\n"
        "        if (vals[0] == 5 && vals[1] == 14 && vals[2] == 23) score += 10;\n"
        "    }\n"
        "    Py_XDECREF(out);\n"
        "\n"
        "    out = PyArray_Correlate(a, b, NPY_FULL);\n"
        "    if (out != NULL && PyArray_DIMS((PyArrayObject *)out)[0] == 4) {\n"
        "        long *vals = (long *)PyArray_DATA((PyArrayObject *)out);\n"
        "        if (vals[0] == 5 && vals[1] == 14 && vals[2] == 23 && vals[3] == 12) score += 100;\n"
        "    }\n"
        "    Py_XDECREF(out);\n"
        "\n"
        "    PyObject *short_a = PyArray_SimpleNew(1, dims2, NPY_INT);\n"
        "    if (short_a == NULL) {\n"
        "        Py_DECREF(b);\n"
        "        Py_DECREF(a);\n"
        "        return NULL;\n"
        "    }\n"
        "    int *short_i = (int *)PyArray_DATA((PyArrayObject *)short_a);\n"
        "    short_i[0] = 1; short_i[1] = 2;\n"
        "    out = PyArray_Correlate(short_a, a, NPY_VALID);\n"
        "    if (out != NULL && PyArray_DIMS((PyArrayObject *)out)[0] == 2) {\n"
        "        long *vals = (long *)PyArray_DATA((PyArrayObject *)out);\n"
        "        if (vals[0] == 5 && vals[1] == 8) score += 1000;\n"
        "    }\n"
        "    Py_XDECREF(out);\n"
        "\n"
        "    PyObject *da = PyArray_SimpleNew(1, dims2, NPY_DOUBLE);\n"
        "    PyObject *db = PyArray_SimpleNew(1, dims2, NPY_DOUBLE);\n"
        "    if (da == NULL || db == NULL) {\n"
        "        Py_XDECREF(da);\n"
        "        Py_XDECREF(db);\n"
        "        Py_DECREF(short_a);\n"
        "        Py_DECREF(b);\n"
        "        Py_DECREF(a);\n"
        "        return NULL;\n"
        "    }\n"
        "    double *dai = (double *)PyArray_DATA((PyArrayObject *)da);\n"
        "    double *dbi = (double *)PyArray_DATA((PyArrayObject *)db);\n"
        "    dai[0] = 1.5; dai[1] = -2.0;\n"
        "    dbi[0] = 2.0; dbi[1] = 0.5;\n"
        "    out = PyArray_Correlate(da, db, NPY_FULL);\n"
        "    if (out != NULL && PyArray_TYPE((PyArrayObject *)out) == NPY_DOUBLE && PyArray_DIMS((PyArrayObject *)out)[0] == 3) {\n"
        "        double *vals = (double *)PyArray_DATA((PyArrayObject *)out);\n"
        "        if (vals[0] > 0.74 && vals[0] < 0.76 && vals[1] > 1.99 && vals[1] < 2.01 && vals[2] > -4.01 && vals[2] < -3.99) score += 10000;\n"
        "    }\n"
        "    Py_XDECREF(out);\n"
        "\n"
        "    npy_intp dim0[1] = {0};\n"
        "    PyObject *empty = PyArray_SimpleNew(1, dim0, NPY_INT);\n"
        "    out = empty != NULL ? PyArray_Correlate(empty, b, NPY_VALID) : NULL;\n"
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_XDECREF(out);\n"
        "    Py_XDECREF(empty);\n"
        "\n"
        "    out = PyArray_Correlate(a, b, 99);\n"
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000000; }\n"
        "    Py_XDECREF(out);\n"
        "\n"
        "    npy_intp dims2d[2] = {1, 2};\n"
        "    PyObject *matrix = PyArray_SimpleNew(2, dims2d, NPY_INT);\n"
        "    out = matrix != NULL ? PyArray_Correlate(matrix, b, NPY_VALID) : NULL;\n"
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000000; }\n"
        "    Py_XDECREF(out);\n"
        "    Py_XDECREF(matrix);\n"
        "\n"
        "    PyObject *objarr = PyArray_SimpleNew(1, dims2, NPY_OBJECT);\n"
        "    out = objarr != NULL ? PyArray_Correlate(objarr, b, NPY_VALID) : NULL;\n"
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000000; }\n"
        "    Py_XDECREF(out);\n"
        "    Py_XDECREF(objarr);\n"
        "\n"
        "    Py_DECREF(db);\n"
        "    Py_DECREF(da);\n"
        "    Py_DECREF(short_a);\n"
        "    Py_DECREF(b);\n"
        "    Py_DECREF(a);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_correlate2_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims3[1] = {3};\n"
        "    npy_intp dims2[1] = {2};\n"
        "    PyObject *a = PyArray_SimpleNew(1, dims3, NPY_INT);\n"
        "    PyObject *b = PyArray_SimpleNew(1, dims2, NPY_INT);\n"
        "    if (a == NULL || b == NULL) {\n"
        "        Py_XDECREF(a);\n"
        "        Py_XDECREF(b);\n"
        "        return NULL;\n"
        "    }\n"
        "    int *ai = (int *)PyArray_DATA((PyArrayObject *)a);\n"
        "    int *bi = (int *)PyArray_DATA((PyArrayObject *)b);\n"
        "    ai[0] = 2; ai[1] = -1; ai[2] = 3;\n"
        "    bi[0] = 4; bi[1] = 5;\n"
        "    PyObject *out = PyArray_Correlate2(a, b, NPY_VALID);\n"
        "    if (out != NULL && PyArray_NDIM((PyArrayObject *)out) == 1 &&\n"
        "        PyArray_DIMS((PyArrayObject *)out)[0] == 2 && PyArray_TYPE((PyArrayObject *)out) == NPY_LONG) {\n"
        "        long *vals = (long *)PyArray_DATA((PyArrayObject *)out);\n"
        "        if (vals[0] == 3 && vals[1] == 11) score += 1;\n"
        "    }\n"
        "    Py_XDECREF(out);\n"
        "\n"
        "    out = PyArray_Correlate2(a, b, NPY_FULL);\n"
        "    if (out != NULL && PyArray_DIMS((PyArrayObject *)out)[0] == 4) {\n"
        "        long *vals = (long *)PyArray_DATA((PyArrayObject *)out);\n"
        "        if (vals[0] == 10 && vals[1] == 3 && vals[2] == 11 && vals[3] == 12) score += 10;\n"
        "    }\n"
        "    Py_XDECREF(out);\n"
        "\n"
        "    PyObject *da = PyArray_SimpleNew(1, dims2, NPY_DOUBLE);\n"
        "    PyObject *db = PyArray_SimpleNew(1, dims2, NPY_DOUBLE);\n"
        "    if (da == NULL || db == NULL) {\n"
        "        Py_XDECREF(da);\n"
        "        Py_XDECREF(db);\n"
        "        Py_DECREF(b);\n"
        "        Py_DECREF(a);\n"
        "        return NULL;\n"
        "    }\n"
        "    double *dai = (double *)PyArray_DATA((PyArrayObject *)da);\n"
        "    double *dbi = (double *)PyArray_DATA((PyArrayObject *)db);\n"
        "    dai[0] = -1.0; dai[1] = 2.0;\n"
        "    dbi[0] = 0.5; dbi[1] = 4.0;\n"
        "    out = PyArray_Correlate2(da, db, NPY_SAME);\n"
        "    if (out != NULL && PyArray_TYPE((PyArrayObject *)out) == NPY_DOUBLE && PyArray_DIMS((PyArrayObject *)out)[0] == 2) {\n"
        "        double *vals = (double *)PyArray_DATA((PyArrayObject *)out);\n"
        "        if (vals[0] > -4.01 && vals[0] < -3.99 && vals[1] > 7.49 && vals[1] < 7.51) score += 100;\n"
        "    }\n"
        "    Py_XDECREF(out);\n"
        "\n"
        "    out = PyArray_Correlate2(a, b, 99);\n"
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000; }\n"
        "    Py_XDECREF(out);\n"
        "\n"
        "    npy_intp dims2d[2] = {1, 2};\n"
        "    PyObject *matrix = PyArray_SimpleNew(2, dims2d, NPY_INT);\n"
        "    out = matrix != NULL ? PyArray_Correlate2(matrix, b, NPY_VALID) : NULL;\n"
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    Py_XDECREF(out);\n"
        "    Py_XDECREF(matrix);\n"
        "\n"
        "    PyObject *complex_arr = PyArray_SimpleNew(1, dims2, NPY_CDOUBLE);\n"
        "    out = complex_arr != NULL ? PyArray_Correlate2(complex_arr, b, NPY_VALID) : NULL;\n"
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_XDECREF(out);\n"
        "    Py_XDECREF(complex_arr);\n"
        "\n"
        "    Py_DECREF(db);\n"
        "    Py_DECREF(da);\n"
        "    Py_DECREF(b);\n"
        "    Py_DECREF(a);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_lexsort_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims4[1] = {4};\n"
        "    PyObject *key0 = PyArray_SimpleNew(1, dims4, NPY_INT);\n"
        "    PyObject *key1 = PyArray_SimpleNew(1, dims4, NPY_INT);\n"
        "    if (key0 == NULL || key1 == NULL) {\n"
        "        Py_XDECREF(key0);\n"
        "        Py_XDECREF(key1);\n"
        "        return NULL;\n"
        "    }\n"
        "    int *k0 = (int *)PyArray_DATA((PyArrayObject *)key0);\n"
        "    int *k1 = (int *)PyArray_DATA((PyArrayObject *)key1);\n"
        "    k0[0] = 2; k0[1] = 1; k0[2] = 2; k0[3] = 1;\n"
        "    k1[0] = 0; k1[1] = 0; k1[2] = 1; k1[3] = 1;\n"
        "    PyObject *keys = PyTuple_Pack(2, key0, key1);\n"
        "    PyObject *idx = keys != NULL ? PyArray_LexSort(keys, 0) : NULL;\n"
        "    if (idx != NULL && idx != key0 && idx != key1 && PyArray_BASE((PyArrayObject *)idx) == NULL &&\n"
        "        PyArray_NDIM((PyArrayObject *)idx) == 1 && PyArray_DIMS((PyArrayObject *)idx)[0] == 4 &&\n"
        "        PyArray_TYPE((PyArrayObject *)idx) == NPY_LONG &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[0] == 1 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[1] == 0 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[2] == 3 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[3] == 2) score += 1;\n"
        "    Py_XDECREF(idx);\n"
        "    Py_XDECREF(keys);\n"
        "\n"
        "    npy_intp dims3[1] = {3};\n"
        "    PyObject *single = PyArray_SimpleNew(1, dims3, NPY_LONG);\n"
        "    if (single == NULL) {\n"
        "        Py_DECREF(key1);\n"
        "        Py_DECREF(key0);\n"
        "        return NULL;\n"
        "    }\n"
        "    long *sk = (long *)PyArray_DATA((PyArrayObject *)single);\n"
        "    sk[0] = 3; sk[1] = 1; sk[2] = 2;\n"
        "    keys = PyTuple_Pack(1, single);\n"
        "    idx = keys != NULL ? PyArray_LexSort(keys, -1) : NULL;\n"
        "    if (idx != NULL && PyArray_NDIM((PyArrayObject *)idx) == 1 && PyArray_DIMS((PyArrayObject *)idx)[0] == 3 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[0] == 1 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[1] == 2 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[2] == 0) score += 10;\n"
        "    Py_XDECREF(idx);\n"
        "    Py_XDECREF(keys);\n"
        "\n"
        "    PyObject *empty_keys = PyTuple_New(0);\n"
        "    idx = empty_keys != NULL ? PyArray_LexSort(empty_keys, 0) : NULL;\n"
        "    if (idx == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100; }\n"
        "    Py_XDECREF(idx);\n"
        "    Py_XDECREF(empty_keys);\n"
        "\n"
        "    keys = PyTuple_Pack(2, key0, single);\n"
        "    idx = keys != NULL ? PyArray_LexSort(keys, 0) : NULL;\n"
        "    if (idx == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000; }\n"
        "    Py_XDECREF(idx);\n"
        "    Py_XDECREF(keys);\n"
        "\n"
        "    npy_intp dims2d[2] = {1, 4};\n"
        "    PyObject *matrix = PyArray_SimpleNew(2, dims2d, NPY_INT);\n"
        "    keys = matrix != NULL ? PyTuple_Pack(1, matrix) : NULL;\n"
        "    idx = keys != NULL ? PyArray_LexSort(keys, 0) : NULL;\n"
        "    if (idx == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    Py_XDECREF(idx);\n"
        "    Py_XDECREF(keys);\n"
        "    Py_XDECREF(matrix);\n"
        "\n"
        "    PyObject *objarr = PyArray_SimpleNew(1, dims4, NPY_OBJECT);\n"
        "    keys = objarr != NULL ? PyTuple_Pack(1, objarr) : NULL;\n"
        "    idx = keys != NULL ? PyArray_LexSort(keys, 0) : NULL;\n"
        "    if (idx == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_XDECREF(idx);\n"
        "    Py_XDECREF(keys);\n"
        "    Py_XDECREF(objarr);\n"
        "\n"
        "    Py_DECREF(single);\n"
        "    Py_DECREF(key1);\n"
        "    Py_DECREF(key0);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_sort_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[1] = {5};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (arr == NULL) return NULL;\n"
        "    int *data = (int *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    data[0] = 5; data[1] = 1; data[2] = 3; data[3] = 1; data[4] = 4;\n"
        "    if (PyArray_Sort((PyArrayObject *)arr, 0, NPY_QUICKSORT) == 0 &&\n"
        "        data[0] == 1 && data[1] == 1 && data[2] == 3 && data[3] == 4 && data[4] == 5) score += 1;\n"
        "    data[0] = 2; data[1] = -1; data[2] = 7; data[3] = 2; data[4] = 0;\n"
        "    if (PyArray_Sort((PyArrayObject *)arr, -1, NPY_STABLESORT) == 0 &&\n"
        "        data[0] == -1 && data[1] == 0 && data[2] == 2 && data[3] == 2 && data[4] == 7) score += 10;\n"
        "    if (PyArray_Sort((PyArrayObject *)arr, 0, NPY_SORT_DESCENDING) == -1 && PyErr_Occurred() != NULL) {\n"
        "        PyErr_Clear();\n"
        "        score += 100;\n"
        "    }\n"
        "    npy_intp dims2[2] = {1, 2};\n"
        "    PyObject *matrix = PyArray_SimpleNew(2, dims2, NPY_INT);\n"
        "    PyObject *objarr = PyArray_SimpleNew(1, dims, NPY_OBJECT);\n"
        "    if (matrix == NULL || objarr == NULL) {\n"
        "        Py_XDECREF(matrix);\n"
        "        Py_XDECREF(objarr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    if (PyArray_Sort((PyArrayObject *)matrix, 0, NPY_QUICKSORT) == -1 && PyErr_Occurred() != NULL) {\n"
        "        PyErr_Clear();\n"
        "        score += 1000;\n"
        "    }\n"
        "    if (PyArray_Sort((PyArrayObject *)objarr, 0, NPY_QUICKSORT) == -1 && PyErr_Occurred() != NULL) {\n"
        "        PyErr_Clear();\n"
        "        score += 10000;\n"
        "    }\n"
        "    Py_DECREF(objarr);\n"
        "    Py_DECREF(matrix);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_argsort_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[1] = {5};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (arr == NULL) return NULL;\n"
        "    int *data = (int *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    data[0] = 5; data[1] = 1; data[2] = 3; data[3] = 1; data[4] = 4;\n"
        "    PyObject *idx = PyArray_ArgSort((PyArrayObject *)arr, 0, NPY_QUICKSORT);\n"
        "    if (idx != NULL && idx != arr && PyArray_BASE((PyArrayObject *)idx) == NULL &&\n"
        "        PyArray_NDIM((PyArrayObject *)idx) == 1 && PyArray_DIMS((PyArrayObject *)idx)[0] == 5 &&\n"
        "        PyArray_TYPE((PyArrayObject *)idx) == NPY_LONG &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[0] == 1 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[1] == 3 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[2] == 2 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[3] == 4 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[4] == 0) score += 1;\n"
        "    if (data[0] == 5 && data[1] == 1 && data[2] == 3 && data[3] == 1 && data[4] == 4) score += 10;\n"
        "    Py_XDECREF(idx);\n"
        "    data[0] = 2; data[1] = -1; data[2] = 7; data[3] = 2; data[4] = 0;\n"
        "    idx = PyArray_ArgSort((PyArrayObject *)arr, -1, NPY_STABLESORT);\n"
        "    if (idx != NULL &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[0] == 1 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[1] == 4 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[2] == 0 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[3] == 3 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[4] == 2) score += 100;\n"
        "    Py_XDECREF(idx);\n"
        "    idx = PyArray_ArgSort((PyArrayObject *)arr, 0, NPY_SORT_DESCENDING);\n"
        "    if (idx == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000; }\n"
        "    Py_XDECREF(idx);\n"
        "    npy_intp dims2[2] = {1, 2};\n"
        "    PyObject *matrix = PyArray_SimpleNew(2, dims2, NPY_INT);\n"
        "    PyObject *objarr = PyArray_SimpleNew(1, dims, NPY_OBJECT);\n"
        "    if (matrix == NULL || objarr == NULL) {\n"
        "        Py_XDECREF(matrix);\n"
        "        Py_XDECREF(objarr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    idx = PyArray_ArgSort((PyArrayObject *)matrix, 0, NPY_QUICKSORT);\n"
        "    if (idx == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    Py_XDECREF(idx);\n"
        "    idx = PyArray_ArgSort((PyArrayObject *)objarr, 0, NPY_QUICKSORT);\n"
        "    if (idx == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_XDECREF(idx);\n"
        "    Py_DECREF(objarr);\n"
        "    Py_DECREF(matrix);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_partition_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[1] = {5};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    npy_intp kth_dims[1] = {1};\n"
        "    PyObject *kth = PyArray_SimpleNew(1, kth_dims, NPY_LONG);\n"
        "    PyObject *scalar_kth = PyArray_SimpleNew(0, NULL, NPY_LONG);\n"
        "    if (arr == NULL || kth == NULL || scalar_kth == NULL) {\n"
        "        Py_XDECREF(scalar_kth);\n"
        "        Py_XDECREF(kth);\n"
        "        Py_XDECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    int *data = (int *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    long *kth_data = (long *)PyArray_DATA((PyArrayObject *)kth);\n"
        "    long *scalar_kth_data = (long *)PyArray_DATA((PyArrayObject *)scalar_kth);\n"
        "    data[0] = 5; data[1] = 1; data[2] = 4; data[3] = 2; data[4] = 3;\n"
        "    kth_data[0] = 2;\n"
        "    if (PyArray_Partition((PyArrayObject *)arr, (PyArrayObject *)kth, 0, NPY_INTROSELECT) == 0 &&\n"
        "        data[0] == 1 && data[1] == 2 && data[2] == 3 && data[3] == 4 && data[4] == 5) score += 1;\n"
        "    data[0] = 9; data[1] = 7; data[2] = 8; data[3] = 6; data[4] = 5;\n"
        "    kth_data[0] = -1;\n"
        "    if (PyArray_Partition((PyArrayObject *)arr, (PyArrayObject *)kth, -1, NPY_INTROSELECT) == 0 &&\n"
        "        data[0] == 5 && data[1] == 6 && data[2] == 7 && data[3] == 8 && data[4] == 9) score += 10;\n"
        "    data[0] = 30; data[1] = 10; data[2] = 20; data[3] = 40; data[4] = 0;\n"
        "    kth_data[0] = 1;\n"
        "    PyObject *idx = PyArray_ArgPartition((PyArrayObject *)arr, (PyArrayObject *)kth, 0, NPY_INTROSELECT);\n"
        "    if (idx != NULL && idx != arr && PyArray_BASE((PyArrayObject *)idx) == NULL &&\n"
        "        PyArray_NDIM((PyArrayObject *)idx) == 1 && PyArray_DIMS((PyArrayObject *)idx)[0] == 5 &&\n"
        "        PyArray_TYPE((PyArrayObject *)idx) == NPY_LONG &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[0] == 4 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[1] == 1 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[2] == 2 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[3] == 0 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[4] == 3) score += 100;\n"
        "    Py_XDECREF(idx);\n"
        "    idx = PyArray_ArgPartition((PyArrayObject *)arr, (PyArrayObject *)kth, 0, (NPY_SELECTKIND)99);\n"
        "    if (idx == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000; }\n"
        "    Py_XDECREF(idx);\n"
        "    kth_data[0] = 5;\n"
        "    if (PyArray_Partition((PyArrayObject *)arr, (PyArrayObject *)kth, 0, NPY_INTROSELECT) == -1 &&\n"
        "        PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    data[0] = 4; data[1] = 3; data[2] = 2; data[3] = 1; data[4] = 0;\n"
        "    scalar_kth_data[0] = 3;\n"
        "    if (PyArray_Partition((PyArrayObject *)arr, (PyArrayObject *)scalar_kth, 0, NPY_INTROSELECT) == 0 &&\n"
        "        data[0] == 0 && data[1] == 1 && data[2] == 2 && data[3] == 3 && data[4] == 4) score += 100000;\n"
        "    data[0] = 5; data[1] = 4; data[2] = 3; data[3] = 2; data[4] = 1;\n"
        "    scalar_kth_data[0] = -2;\n"
        "    idx = PyArray_ArgPartition((PyArrayObject *)arr, (PyArrayObject *)scalar_kth, 0, NPY_INTROSELECT);\n"
        "    if (idx != NULL &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[0] == 4 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[1] == 3 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[2] == 2 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[3] == 1 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[4] == 0) score += 1000000;\n"
        "    Py_XDECREF(idx);\n"
        "    scalar_kth_data[0] = 5;\n"
        "    if (PyArray_ArgPartition((PyArrayObject *)arr, (PyArrayObject *)scalar_kth, 0, NPY_INTROSELECT) == NULL &&\n"
        "        PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000000; }\n"
        "    Py_DECREF(kth);\n"
        "    Py_DECREF(scalar_kth);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_searchsorted_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp hay_dims[1] = {4};\n"
        "    npy_intp key_dims[1] = {4};\n"
        "    PyObject *hay = PyArray_SimpleNew(1, hay_dims, NPY_INT);\n"
        "    PyObject *keys = PyArray_SimpleNew(1, key_dims, NPY_INT);\n"
        "    if (hay == NULL || keys == NULL) {\n"
        "        Py_XDECREF(hay);\n"
        "        Py_XDECREF(keys);\n"
        "        return NULL;\n"
        "    }\n"
        "    int *hay_data = (int *)PyArray_DATA((PyArrayObject *)hay);\n"
        "    int *key_data = (int *)PyArray_DATA((PyArrayObject *)keys);\n"
        "    hay_data[0] = 1; hay_data[1] = 3; hay_data[2] = 3; hay_data[3] = 5;\n"
        "    key_data[0] = 0; key_data[1] = 3; key_data[2] = 4; key_data[3] = 8;\n"
        "    PyObject *idx = PyArray_SearchSorted((PyArrayObject *)hay, keys, NPY_SEARCHLEFT, NULL);\n"
        "    if (idx != NULL && idx != hay && idx != keys && PyArray_BASE((PyArrayObject *)idx) == NULL &&\n"
        "        PyArray_NDIM((PyArrayObject *)idx) == 1 && PyArray_DIMS((PyArrayObject *)idx)[0] == 4 &&\n"
        "        PyArray_TYPE((PyArrayObject *)idx) == NPY_LONG &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[0] == 0 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[1] == 1 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[2] == 3 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[3] == 4) score += 1;\n"
        "    Py_XDECREF(idx);\n"
        "    idx = PyArray_SearchSorted((PyArrayObject *)hay, keys, NPY_SEARCHRIGHT, NULL);\n"
        "    if (idx != NULL &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[0] == 0 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[1] == 3 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[2] == 3 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[3] == 4) score += 10;\n"
        "    Py_XDECREF(idx);\n"
        "    if (hay_data[0] == 1 && hay_data[1] == 3 && hay_data[2] == 3 && hay_data[3] == 5) score += 100;\n"
        "    idx = PyArray_SearchSorted((PyArrayObject *)hay, keys, (NPY_SEARCHSIDE)7, NULL);\n"
        "    if (idx == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000; }\n"
        "    Py_XDECREF(idx);\n"
        "    PyObject *unsorted = PyArray_SimpleNew(1, hay_dims, NPY_INT);\n"
        "    PyObject *sorter = PyArray_SimpleNew(1, hay_dims, NPY_LONG);\n"
        "    if (unsorted == NULL || sorter == NULL) {\n"
        "        Py_XDECREF(unsorted);\n"
        "        Py_XDECREF(sorter);\n"
        "        Py_DECREF(keys);\n"
        "        Py_DECREF(hay);\n"
        "        return NULL;\n"
        "    }\n"
        "    int *unsorted_data = (int *)PyArray_DATA((PyArrayObject *)unsorted);\n"
        "    long *sorter_data = (long *)PyArray_DATA((PyArrayObject *)sorter);\n"
        "    unsorted_data[0] = 30; unsorted_data[1] = 10; unsorted_data[2] = 20; unsorted_data[3] = 20;\n"
        "    sorter_data[0] = 1; sorter_data[1] = 2; sorter_data[2] = 3; sorter_data[3] = 0;\n"
        "    key_data[0] = 5; key_data[1] = 20; key_data[2] = 25; key_data[3] = 40;\n"
        "    idx = PyArray_SearchSorted((PyArrayObject *)unsorted, keys, NPY_SEARCHLEFT, sorter);\n"
        "    if (idx != NULL &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[0] == 0 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[1] == 1 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[2] == 3 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[3] == 4) score += 10000000;\n"
        "    Py_XDECREF(idx);\n"
        "    idx = PyArray_SearchSorted((PyArrayObject *)unsorted, keys, NPY_SEARCHRIGHT, sorter);\n"
        "    if (idx != NULL &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[0] == 0 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[1] == 3 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[2] == 3 &&\n"
        "        ((long *)PyArray_DATA((PyArrayObject *)idx))[3] == 4) score += 100000000;\n"
        "    Py_XDECREF(idx);\n"
        "    idx = PyArray_SearchSorted((PyArrayObject *)hay, keys, NPY_SEARCHLEFT, keys);\n"
        "    if (idx == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    Py_XDECREF(idx);\n"
        "    Py_DECREF(sorter);\n"
        "    Py_DECREF(unsorted);\n"
        "    npy_intp dims2[2] = {1, 2};\n"
        "    PyObject *matrix = PyArray_SimpleNew(2, dims2, NPY_INT);\n"
        "    PyObject *objarr = PyArray_SimpleNew(1, hay_dims, NPY_OBJECT);\n"
        "    if (matrix == NULL || objarr == NULL) {\n"
        "        Py_XDECREF(matrix);\n"
        "        Py_XDECREF(objarr);\n"
        "        Py_DECREF(keys);\n"
        "        Py_DECREF(hay);\n"
        "        return NULL;\n"
        "    }\n"
        "    idx = PyArray_SearchSorted((PyArrayObject *)matrix, keys, NPY_SEARCHLEFT, NULL);\n"
        "    if (idx == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_XDECREF(idx);\n"
        "    idx = PyArray_SearchSorted((PyArrayObject *)objarr, keys, NPY_SEARCHLEFT, NULL);\n"
        "    if (idx == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000000; }\n"
        "    Py_XDECREF(idx);\n"
        "    Py_DECREF(objarr);\n"
        "    Py_DECREF(matrix);\n"
        "    Py_DECREF(keys);\n"
        "    Py_DECREF(hay);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_nonzero_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[1] = {6};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (arr == NULL) return NULL;\n"
        "    int *data = (int *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    data[0] = 0; data[1] = 2; data[2] = 0; data[3] = -3; data[4] = 4; data[5] = 0;\n"
        "    PyObject *tuple = PyArray_Nonzero((PyArrayObject *)arr);\n"
        "    if (tuple != NULL && PyTuple_Check(tuple) && PyTuple_Size(tuple) == 1) {\n"
        "        PyObject *idx = PyTuple_GetItem(tuple, 0);\n"
        "        if (idx != NULL && PyArray_Check(idx) && PyArray_BASE((PyArrayObject *)idx) == NULL &&\n"
        "            PyArray_NDIM((PyArrayObject *)idx) == 1 && PyArray_DIMS((PyArrayObject *)idx)[0] == 3 &&\n"
        "            PyArray_TYPE((PyArrayObject *)idx) == NPY_LONG &&\n"
        "            ((long *)PyArray_DATA((PyArrayObject *)idx))[0] == 1 &&\n"
        "            ((long *)PyArray_DATA((PyArrayObject *)idx))[1] == 3 &&\n"
        "            ((long *)PyArray_DATA((PyArrayObject *)idx))[2] == 4) score += 1;\n"
        "    }\n"
        "    if (data[0] == 0 && data[1] == 2 && data[2] == 0 && data[3] == -3 && data[4] == 4 && data[5] == 0) score += 10;\n"
        "    Py_XDECREF(tuple);\n"
        "    data[1] = 0; data[3] = 0; data[4] = 0;\n"
        "    tuple = PyArray_Nonzero((PyArrayObject *)arr);\n"
        "    if (tuple != NULL && PyTuple_Check(tuple) && PyTuple_Size(tuple) == 1) {\n"
        "        PyObject *idx = PyTuple_GetItem(tuple, 0);\n"
        "        if (idx != NULL && PyArray_Check(idx) &&\n"
        "            PyArray_NDIM((PyArrayObject *)idx) == 1 && PyArray_DIMS((PyArrayObject *)idx)[0] == 0 &&\n"
        "            PyArray_TYPE((PyArrayObject *)idx) == NPY_LONG) score += 100;\n"
        "    }\n"
        "    Py_XDECREF(tuple);\n"
        "    npy_intp dims2[2] = {1, 2};\n"
        "    PyObject *matrix = PyArray_SimpleNew(2, dims2, NPY_INT);\n"
        "    PyObject *objarr = PyArray_SimpleNew(1, dims, NPY_OBJECT);\n"
        "    if (matrix == NULL || objarr == NULL) {\n"
        "        Py_XDECREF(matrix);\n"
        "        Py_XDECREF(objarr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    int *matrix_data = (int *)PyArray_DATA((PyArrayObject *)matrix);\n"
        "    matrix_data[0] = 0; matrix_data[1] = 7;\n"
        "    tuple = PyArray_Nonzero((PyArrayObject *)matrix);\n"
        "    if (tuple != NULL && PyTuple_Check(tuple) && PyTuple_Size(tuple) == 2) {\n"
        "        PyObject *rows = PyTuple_GetItem(tuple, 0);\n"
        "        PyObject *cols = PyTuple_GetItem(tuple, 1);\n"
        "        if (rows != NULL && cols != NULL && PyArray_Check(rows) && PyArray_Check(cols) &&\n"
        "            PyArray_NDIM((PyArrayObject *)rows) == 1 && PyArray_DIMS((PyArrayObject *)rows)[0] == 1 &&\n"
        "            PyArray_NDIM((PyArrayObject *)cols) == 1 && PyArray_DIMS((PyArrayObject *)cols)[0] == 1 &&\n"
        "            PyArray_TYPE((PyArrayObject *)rows) == NPY_LONG && PyArray_TYPE((PyArrayObject *)cols) == NPY_LONG &&\n"
        "            ((long *)PyArray_DATA((PyArrayObject *)rows))[0] == 0 &&\n"
        "            ((long *)PyArray_DATA((PyArrayObject *)cols))[0] == 1) score += 1000;\n"
        "    } else if (PyErr_Occurred() != NULL) {\n"
        "        PyErr_Clear();\n"
        "    }\n"
        "    Py_XDECREF(tuple);\n"
        "    tuple = PyArray_Nonzero((PyArrayObject *)objarr);\n"
        "    if (tuple == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    Py_XDECREF(tuple);\n"
        "    Py_DECREF(objarr);\n"
        "    Py_DECREF(matrix);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_count_nonzero_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[1] = {5};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (arr == NULL) return NULL;\n"
        "    int *data = (int *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    data[0] = 0; data[1] = -2; data[2] = 0; data[3] = 7; data[4] = 9;\n"
        "    if (PyArray_CountNonzero((PyArrayObject *)arr) == 3) score += 1;\n"
        "    npy_intp ddims[2] = {2, 3};\n"
        "    PyObject *darr = PyArray_SimpleNew(2, ddims, NPY_DOUBLE);\n"
        "    if (darr == NULL) { Py_DECREF(arr); return NULL; }\n"
        "    double *ddata = (double *)PyArray_DATA((PyArrayObject *)darr);\n"
        "    ddata[0] = 0.0; ddata[1] = 1.5; ddata[2] = 0.0; ddata[3] = -2.0; ddata[4] = 3.25; ddata[5] = 0.0;\n"
        "    if (PyArray_CountNonzero((PyArrayObject *)darr) == 3) score += 10;\n"
        "    npy_intp bdims[1] = {4};\n"
        "    PyObject *barr = PyArray_SimpleNew(1, bdims, NPY_BOOL);\n"
        "    if (barr == NULL) { Py_DECREF(darr); Py_DECREF(arr); return NULL; }\n"
        "    unsigned char *bdata = (unsigned char *)PyArray_DATA((PyArrayObject *)barr);\n"
        "    bdata[0] = 1; bdata[1] = 0; bdata[2] = 1; bdata[3] = 1;\n"
        "    if (PyArray_CountNonzero((PyArrayObject *)barr) == 3) score += 100;\n"
        "    npy_intp edims[1] = {0};\n"
        "    PyObject *empty = PyArray_SimpleNew(1, edims, NPY_INT);\n"
        "    if (empty == NULL) { Py_DECREF(barr); Py_DECREF(darr); Py_DECREF(arr); return NULL; }\n"
        "    if (PyArray_CountNonzero((PyArrayObject *)empty) == 0) score += 1000;\n"
        "    if (PyArray_CountNonzero((PyArrayObject *)Py_None) == -1 && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    PyObject *objarr = PyArray_SimpleNew(1, dims, NPY_OBJECT);\n"
        "    if (objarr == NULL) { Py_DECREF(empty); Py_DECREF(barr); Py_DECREF(darr); Py_DECREF(arr); return NULL; }\n"
        "    if (PyArray_CountNonzero((PyArrayObject *)objarr) == -1 && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    PyObject *carr = PyArray_SimpleNew(1, dims, NPY_CDOUBLE);\n"
        "    if (carr == NULL) { Py_DECREF(objarr); Py_DECREF(empty); Py_DECREF(barr); Py_DECREF(darr); Py_DECREF(arr); return NULL; }\n"
        "    if (PyArray_CountNonzero((PyArrayObject *)carr) == -1 && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000000; }\n"
        "    PyObject *strided = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (strided != NULL) {\n"
        "        PyArray_STRIDES((PyArrayObject *)strided)[0] = 2 * (npy_intp)sizeof(int);\n"
        "        if (PyArray_CountNonzero((PyArrayObject *)strided) == -1 && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000000; }\n"
        "    }\n"
        "    Py_XDECREF(strided);\n"
        "    Py_DECREF(carr);\n"
        "    Py_DECREF(objarr);\n"
        "    Py_DECREF(empty);\n"
        "    Py_DECREF(barr);\n"
        "    Py_DECREF(darr);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_min_scalar_type_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[1] = {3};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (arr == NULL) return NULL;\n"
        "    PyArray_Descr *descr = PyArray_MinScalarType((PyArrayObject *)arr);\n"
        "    if (descr != NULL && descr->type_num == NPY_INT) score += 1;\n"
        "    Py_XDECREF(descr);\n"
        "    PyObject *pos = PyArray_SimpleNew(0, NULL, NPY_LONG);\n"
        "    PyObject *neg = PyArray_SimpleNew(0, NULL, NPY_LONG);\n"
        "    PyObject *uns = PyArray_SimpleNew(0, NULL, NPY_ULONGLONG);\n"
        "    PyObject *flt = PyArray_SimpleNew(0, NULL, NPY_DOUBLE);\n"
        "    PyObject *objarr = PyArray_SimpleNew(1, dims, NPY_OBJECT);\n"
        "    PyObject *carr = PyArray_SimpleNew(1, dims, NPY_CDOUBLE);\n"
        "    if (pos == NULL || neg == NULL || uns == NULL || flt == NULL || objarr == NULL || carr == NULL) {\n"
        "        Py_XDECREF(carr);\n"
        "        Py_XDECREF(objarr);\n"
        "        Py_XDECREF(flt);\n"
        "        Py_XDECREF(uns);\n"
        "        Py_XDECREF(neg);\n"
        "        Py_XDECREF(pos);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    *((long *)PyArray_DATA((PyArrayObject *)pos)) = 7;\n"
        "    descr = PyArray_MinScalarType((PyArrayObject *)pos);\n"
        "    if (descr != NULL && descr->type_num == NPY_UBYTE) score += 10;\n"
        "    Py_XDECREF(descr);\n"
        "    *((long *)PyArray_DATA((PyArrayObject *)neg)) = -2;\n"
        "    descr = PyArray_MinScalarType((PyArrayObject *)neg);\n"
        "    if (descr != NULL && descr->type_num == NPY_BYTE) score += 100;\n"
        "    Py_XDECREF(descr);\n"
        "    *((unsigned long long *)PyArray_DATA((PyArrayObject *)uns)) = 255ULL;\n"
        "    descr = PyArray_MinScalarType((PyArrayObject *)uns);\n"
        "    if (descr != NULL && descr->type_num == NPY_UBYTE) score += 1000;\n"
        "    Py_XDECREF(descr);\n"
        "    *((double *)PyArray_DATA((PyArrayObject *)flt)) = 1.5;\n"
        "    descr = PyArray_MinScalarType((PyArrayObject *)flt);\n"
        "    if (descr != NULL && descr->type_num == NPY_FLOAT) score += 10000;\n"
        "    Py_XDECREF(descr);\n"
        "    descr = PyArray_MinScalarType((PyArrayObject *)objarr);\n"
        "    if (descr != NULL && descr->type_num == NPY_OBJECT) score += 100000;\n"
        "    Py_XDECREF(descr);\n"
        "    descr = PyArray_MinScalarType((PyArrayObject *)carr);\n"
        "    if (descr != NULL && descr->type_num == NPY_CDOUBLE) score += 1000000;\n"
        "    Py_XDECREF(descr);\n"
        "    descr = PyArray_MinScalarType((PyArrayObject *)Py_None);\n"
        "    if (descr == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000000; }\n"
        "    Py_XDECREF(descr);\n"
        "    Py_DECREF(carr);\n"
        "    Py_DECREF(objarr);\n"
        "    Py_DECREF(flt);\n"
        "    Py_DECREF(uns);\n"
        "    Py_DECREF(neg);\n"
        "    Py_DECREF(pos);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_sorted_stride_perm_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp strides[3] = {4, -2, 12};\n"
        "    npy_stride_sort_item items[3];\n"
        "    PyArray_CreateSortedStridePerm(3, strides, items);\n"
        "    if (items[0].perm == 2 && items[0].stride == 12 &&\n"
        "        items[1].perm == 0 && items[1].stride == 4 &&\n"
        "        items[2].perm == 1 && items[2].stride == -2) score += 1;\n"
        "    npy_intp tied[4] = {8, -8, 4, 0};\n"
        "    npy_stride_sort_item tied_items[4];\n"
        "    PyArray_CreateSortedStridePerm(4, tied, tied_items);\n"
        "    if (tied_items[0].perm == 0 && tied_items[0].stride == 8 &&\n"
        "        tied_items[1].perm == 1 && tied_items[1].stride == -8 &&\n"
        "        tied_items[2].perm == 2 && tied_items[2].stride == 4 &&\n"
        "        tied_items[3].perm == 3 && tied_items[3].stride == 0) score += 10;\n"
        "    npy_intp neg[3] = {-1, -9, 3};\n"
        "    PyArray_CreateSortedStridePerm(3, neg, items);\n"
        "    if (items[0].perm == 1 && items[0].stride == -9 &&\n"
        "        items[1].perm == 2 && items[1].stride == 3 &&\n"
        "        items[2].perm == 0 && items[2].stride == -1) score += 100;\n"
        "    npy_stride_sort_item untouched[2];\n"
        "    untouched[0].perm = 90; untouched[0].stride = 91;\n"
        "    untouched[1].perm = 92; untouched[1].stride = 93;\n"
        "    PyArray_CreateSortedStridePerm(0, NULL, untouched);\n"
        "    if (untouched[0].perm == 90 && untouched[0].stride == 91 &&\n"
        "        untouched[1].perm == 92 && untouched[1].stride == 93) score += 1000;\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_remove_axes_in_place_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[3] = {2, 1, 3};\n"
        "    PyObject *arr_obj = PyArray_SimpleNew(3, dims, NPY_INT);\n"
        "    if (arr_obj == NULL) return NULL;\n"
        "    PyArrayObject *arr = (PyArrayObject *)arr_obj;\n"
        "    void *data_before = PyArray_DATA(arr);\n"
        "    npy_intp stride0 = PyArray_STRIDES(arr)[0];\n"
        "    npy_intp stride2 = PyArray_STRIDES(arr)[2];\n"
        "    npy_bool drop_middle[3] = {0, 1, 0};\n"
        "    PyArray_RemoveAxesInPlace(arr, drop_middle);\n"
        "    if (PyArray_NDIM(arr) == 2 && PyArray_DIMS(arr)[0] == 2 &&\n"
        "        PyArray_DIMS(arr)[1] == 3 && PyArray_DATA(arr) == data_before &&\n"
        "        PyArray_TYPE(arr) == NPY_INT) score += 1;\n"
        "    if (PyArray_STRIDES(arr)[0] == stride0 && PyArray_STRIDES(arr)[1] == stride2) score += 10;\n"
        "    npy_bool keep_all[2] = {0, 0};\n"
        "    PyArray_RemoveAxesInPlace(arr, keep_all);\n"
        "    if (PyArray_NDIM(arr) == 2 && PyArray_DIMS(arr)[0] == 2 &&\n"
        "        PyArray_DIMS(arr)[1] == 3 && PyArray_DATA(arr) == data_before) score += 100;\n"
        "    Py_DECREF(arr_obj);\n"
        "    npy_intp scalar_dims[2] = {1, 1};\n"
        "    PyObject *scalar_obj = PyArray_SimpleNew(2, scalar_dims, NPY_INT);\n"
        "    if (scalar_obj == NULL) return NULL;\n"
        "    void *scalar_data = PyArray_DATA((PyArrayObject *)scalar_obj);\n"
        "    npy_bool drop_all[2] = {1, 1};\n"
        "    PyArray_RemoveAxesInPlace((PyArrayObject *)scalar_obj, drop_all);\n"
        "    if (PyArray_NDIM((PyArrayObject *)scalar_obj) == 0 &&\n"
        "        PyArray_DATA((PyArrayObject *)scalar_obj) == scalar_data) score += 1000;\n"
        "    Py_DECREF(scalar_obj);\n"
        "    PyArray_RemoveAxesInPlace((PyArrayObject *)Py_None, drop_all);\n"
        "    if (PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_debug_print_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[2] = {2, 3};\n"
        "    PyObject *arr_obj = PyArray_SimpleNew(2, dims, NPY_INT);\n"
        "    if (arr_obj == NULL) return NULL;\n"
        "    PyArray_DebugPrint((PyArrayObject *)arr_obj);\n"
        "    if (PyErr_Occurred() == NULL) score += 1;\n"
        "    PyArray_DebugPrint(NULL);\n"
        "    if (PyErr_Occurred() == NULL) score += 10;\n"
        "    PyArray_DebugPrint((PyArrayObject *)Py_None);\n"
        "    if (PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100; }\n"
        "    Py_DECREF(arr_obj);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_where_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[1] = {4};\n"
        "    PyObject *cond = PyArray_SimpleNew(1, dims, NPY_BOOL);\n"
        "    PyObject *left = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    PyObject *right = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (cond == NULL || left == NULL || right == NULL) {\n"
        "        Py_XDECREF(cond);\n"
        "        Py_XDECREF(left);\n"
        "        Py_XDECREF(right);\n"
        "        return NULL;\n"
        "    }\n"
        "    unsigned char *cond_data = (unsigned char *)PyArray_DATA((PyArrayObject *)cond);\n"
        "    int *left_data = (int *)PyArray_DATA((PyArrayObject *)left);\n"
        "    int *right_data = (int *)PyArray_DATA((PyArrayObject *)right);\n"
        "    cond_data[0] = 1; cond_data[1] = 0; cond_data[2] = 1; cond_data[3] = 0;\n"
        "    left_data[0] = 10; left_data[1] = 20; left_data[2] = 30; left_data[3] = 40;\n"
        "    right_data[0] = 100; right_data[1] = 200; right_data[2] = 300; right_data[3] = 400;\n"
        "    PyObject *out = PyArray_Where(cond, left, right);\n"
        "    if (out != NULL && out != left && out != right && PyArray_BASE((PyArrayObject *)out) == NULL &&\n"
        "        PyArray_NDIM((PyArrayObject *)out) == 1 && PyArray_DIMS((PyArrayObject *)out)[0] == 4 &&\n"
        "        PyArray_TYPE((PyArrayObject *)out) == NPY_INT &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[0] == 10 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[1] == 200 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[2] == 30 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[3] == 400) score += 1;\n"
        "    right_data[1] = 999;\n"
        "    if (out != NULL && ((int *)PyArray_DATA((PyArrayObject *)out))[1] == 200) score += 10;\n"
        "    Py_XDECREF(out);\n"
        "    PyObject *nz = PyArray_Where(cond, NULL, NULL);\n"
        "    if (nz != NULL && PyTuple_Check(nz) && PyTuple_Size(nz) == 1) {\n"
        "        PyObject *idx = PyTuple_GetItem(nz, 0);\n"
        "        if (idx != NULL && PyArray_Check(idx) && PyArray_TYPE((PyArrayObject *)idx) == NPY_LONG &&\n"
        "            PyArray_DIMS((PyArrayObject *)idx)[0] == 2 &&\n"
        "            ((long *)PyArray_DATA((PyArrayObject *)idx))[0] == 0 &&\n"
        "            ((long *)PyArray_DATA((PyArrayObject *)idx))[1] == 2) score += 100;\n"
        "    }\n"
        "    Py_XDECREF(nz);\n"
        "    out = PyArray_Where(cond, left, NULL);\n"
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000; }\n"
        "    Py_XDECREF(out);\n"
        "    npy_intp short_dims[1] = {2};\n"
        "    PyObject *short_arr = PyArray_SimpleNew(1, short_dims, NPY_INT);\n"
        "    if (short_arr == NULL) {\n"
        "        Py_DECREF(right);\n"
        "        Py_DECREF(left);\n"
        "        Py_DECREF(cond);\n"
        "        return NULL;\n"
        "    }\n"
        "    out = PyArray_Where(cond, left, short_arr);\n"
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    Py_XDECREF(out);\n"
        "    npy_intp dims2[2] = {2, 2};\n"
        "    PyObject *cond2 = PyArray_SimpleNew(2, dims2, NPY_BOOL);\n"
        "    PyObject *left2 = PyArray_SimpleNew(2, dims2, NPY_INT);\n"
        "    PyObject *right2 = PyArray_SimpleNew(2, dims2, NPY_INT);\n"
        "    if (cond2 == NULL || left2 == NULL || right2 == NULL) {\n"
        "        Py_XDECREF(cond2);\n"
        "        Py_XDECREF(left2);\n"
        "        Py_XDECREF(right2);\n"
        "        Py_DECREF(short_arr);\n"
        "        Py_DECREF(right);\n"
        "        Py_DECREF(left);\n"
        "        Py_DECREF(cond);\n"
        "        return NULL;\n"
        "    }\n"
        "    unsigned char *cond2_data = (unsigned char *)PyArray_DATA((PyArrayObject *)cond2);\n"
        "    int *left2_data = (int *)PyArray_DATA((PyArrayObject *)left2);\n"
        "    int *right2_data = (int *)PyArray_DATA((PyArrayObject *)right2);\n"
        "    cond2_data[0] = 1; cond2_data[1] = 0; cond2_data[2] = 0; cond2_data[3] = 1;\n"
        "    left2_data[0] = 1; left2_data[1] = 2; left2_data[2] = 3; left2_data[3] = 4;\n"
        "    right2_data[0] = 10; right2_data[1] = 20; right2_data[2] = 30; right2_data[3] = 40;\n"
        "    out = PyArray_Where(cond2, left2, right2);\n"
        "    if (out != NULL && PyArray_NDIM((PyArrayObject *)out) == 2 &&\n"
        "        PyArray_DIMS((PyArrayObject *)out)[0] == 2 && PyArray_DIMS((PyArrayObject *)out)[1] == 2 &&\n"
        "        PyArray_TYPE((PyArrayObject *)out) == NPY_INT &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[0] == 1 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[1] == 20 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[2] == 30 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[3] == 4) score += 1000000;\n"
        "    Py_XDECREF(out);\n"
        "    PyObject *objarr = PyArray_SimpleNew(1, dims, NPY_OBJECT);\n"
        "    if (objarr == NULL) {\n"
        "        Py_DECREF(right2);\n"
        "        Py_DECREF(left2);\n"
        "        Py_DECREF(cond2);\n"
        "        Py_DECREF(short_arr);\n"
        "        Py_DECREF(right);\n"
        "        Py_DECREF(left);\n"
        "        Py_DECREF(cond);\n"
        "        return NULL;\n"
        "    }\n"
        "    out = PyArray_Where(cond, objarr, right);\n"
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_XDECREF(out);\n"
        "    Py_DECREF(objarr);\n"
        "    Py_DECREF(right2);\n"
        "    Py_DECREF(left2);\n"
        "    Py_DECREF(cond2);\n"
        "    Py_DECREF(short_arr);\n"
        "    Py_DECREF(right);\n"
        "    Py_DECREF(left);\n"
        "    Py_DECREF(cond);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_compress_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[1] = {5};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    PyObject *cond = PyArray_SimpleNew(1, dims, NPY_BOOL);\n"
        "    if (arr == NULL || cond == NULL) {\n"
        "        Py_XDECREF(arr);\n"
        "        Py_XDECREF(cond);\n"
        "        return NULL;\n"
        "    }\n"
        "    int *data = (int *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    unsigned char *cond_data = (unsigned char *)PyArray_DATA((PyArrayObject *)cond);\n"
        "    data[0] = 10; data[1] = 20; data[2] = 30; data[3] = 40; data[4] = 50;\n"
        "    cond_data[0] = 1; cond_data[1] = 0; cond_data[2] = 1; cond_data[3] = 0; cond_data[4] = 1;\n"
        "    PyObject *out = PyArray_Compress((PyArrayObject *)arr, cond, 0, NULL);\n"
        "    if (out != NULL && out != arr && PyArray_BASE((PyArrayObject *)out) == NULL &&\n"
        "        PyArray_NDIM((PyArrayObject *)out) == 1 && PyArray_DIMS((PyArrayObject *)out)[0] == 3 &&\n"
        "        PyArray_TYPE((PyArrayObject *)out) == NPY_INT &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[0] == 10 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[1] == 30 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[2] == 50) score += 1;\n"
        "    data[2] = 99;\n"
        "    if (out != NULL && ((int *)PyArray_DATA((PyArrayObject *)out))[1] == 30) score += 10;\n"
        "    Py_XDECREF(out);\n"
        "    cond_data[0] = 0; cond_data[2] = 0; cond_data[4] = 0;\n"
        "    out = PyArray_Compress((PyArrayObject *)arr, cond, -1, NULL);\n"
        "    if (out != NULL && PyArray_NDIM((PyArrayObject *)out) == 1 &&\n"
        "        PyArray_DIMS((PyArrayObject *)out)[0] == 0 && PyArray_TYPE((PyArrayObject *)out) == NPY_INT) score += 100;\n"
        "    Py_XDECREF(out);\n"
        "    PyObject *out_arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (out_arr == NULL) {\n"
        "        Py_DECREF(cond);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    out = PyArray_Compress((PyArrayObject *)arr, cond, 0, (PyArrayObject *)out_arr);\n"
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000; }\n"
        "    Py_XDECREF(out);\n"
        "    npy_intp short_dims[1] = {3};\n"
        "    PyObject *short_cond = PyArray_SimpleNew(1, short_dims, NPY_BOOL);\n"
        "    if (short_cond == NULL) {\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(cond);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    out = PyArray_Compress((PyArrayObject *)arr, short_cond, 0, NULL);\n"
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    Py_XDECREF(out);\n"
        "    npy_intp matrix_dims[2] = {2, 3};\n"
        "    npy_intp cond0_dims[1] = {2};\n"
        "    npy_intp cond1_dims[1] = {3};\n"
        "    PyObject *matrix = PyArray_SimpleNew(2, matrix_dims, NPY_INT);\n"
        "    PyObject *cond0 = PyArray_SimpleNew(1, cond0_dims, NPY_BOOL);\n"
        "    PyObject *cond1 = PyArray_SimpleNew(1, cond1_dims, NPY_BOOL);\n"
        "    if (matrix == NULL || cond0 == NULL || cond1 == NULL) {\n"
        "        Py_XDECREF(cond1);\n"
        "        Py_XDECREF(cond0);\n"
        "        Py_XDECREF(matrix);\n"
        "        Py_DECREF(short_cond);\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(cond);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    int *matrix_data = (int *)PyArray_DATA((PyArrayObject *)matrix);\n"
        "    unsigned char *cond0_data = (unsigned char *)PyArray_DATA((PyArrayObject *)cond0);\n"
        "    unsigned char *cond1_data = (unsigned char *)PyArray_DATA((PyArrayObject *)cond1);\n"
        "    matrix_data[0] = 1; matrix_data[1] = 2; matrix_data[2] = 3;\n"
        "    matrix_data[3] = 4; matrix_data[4] = 5; matrix_data[5] = 6;\n"
        "    cond0_data[0] = 1; cond0_data[1] = 0;\n"
        "    cond1_data[0] = 0; cond1_data[1] = 1; cond1_data[2] = 1;\n"
        "    out = PyArray_Compress((PyArrayObject *)matrix, cond0, 0, NULL);\n"
        "    if (out != NULL && PyArray_NDIM((PyArrayObject *)out) == 2 &&\n"
        "        PyArray_DIMS((PyArrayObject *)out)[0] == 1 && PyArray_DIMS((PyArrayObject *)out)[1] == 3 &&\n"
        "        PyArray_TYPE((PyArrayObject *)out) == NPY_INT &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[0] == 1 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[1] == 2 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[2] == 3) score += 1000000;\n"
        "    Py_XDECREF(out);\n"
        "    out = PyArray_Compress((PyArrayObject *)matrix, cond1, 1, NULL);\n"
        "    if (out != NULL && PyArray_NDIM((PyArrayObject *)out) == 2 &&\n"
        "        PyArray_DIMS((PyArrayObject *)out)[0] == 2 && PyArray_DIMS((PyArrayObject *)out)[1] == 2 &&\n"
        "        PyArray_TYPE((PyArrayObject *)out) == NPY_INT &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[0] == 2 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[1] == 3 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[2] == 5 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[3] == 6) score += 10000000;\n"
        "    Py_XDECREF(out);\n"
        "    Py_DECREF(cond1);\n"
        "    Py_DECREF(cond0);\n"
        "    Py_DECREF(matrix);\n"
        "    PyObject *objarr = PyArray_SimpleNew(1, dims, NPY_OBJECT);\n"
        "    if (objarr == NULL) {\n"
        "        Py_DECREF(short_cond);\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(cond);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    out = PyArray_Compress((PyArrayObject *)objarr, cond, 0, NULL);\n"
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_XDECREF(out);\n"
        "    Py_DECREF(objarr);\n"
        "    Py_DECREF(short_cond);\n"
        "    Py_DECREF(out_arr);\n"
        "    Py_DECREF(cond);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_diagonal_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[2] = {2, 3};\n"
        "    PyObject *matrix = PyArray_SimpleNew(2, dims, NPY_INT);\n"
        "    if (matrix == NULL) return NULL;\n"
        "    int *data = (int *)PyArray_DATA((PyArrayObject *)matrix);\n"
        "    data[0] = 1; data[1] = 2; data[2] = 3; data[3] = 4; data[4] = 5; data[5] = 6;\n"
        "    PyObject *diag = PyArray_Diagonal((PyArrayObject *)matrix, 0, 0, 1);\n"
        "    if (diag != NULL && diag != matrix && PyArray_BASE((PyArrayObject *)diag) == NULL &&\n"
        "        PyArray_NDIM((PyArrayObject *)diag) == 1 && PyArray_DIMS((PyArrayObject *)diag)[0] == 2 &&\n"
        "        PyArray_TYPE((PyArrayObject *)diag) == NPY_INT &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)diag))[0] == 1 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)diag))[1] == 5) score += 1;\n"
        "    data[4] = 99;\n"
        "    if (diag != NULL && ((int *)PyArray_DATA((PyArrayObject *)diag))[1] == 5) score += 10;\n"
        "    Py_XDECREF(diag);\n"
        "    diag = PyArray_Diagonal((PyArrayObject *)matrix, 1, 0, 1);\n"
        "    if (diag != NULL && PyArray_NDIM((PyArrayObject *)diag) == 1 &&\n"
        "        PyArray_DIMS((PyArrayObject *)diag)[0] == 2 && PyArray_TYPE((PyArrayObject *)diag) == NPY_INT &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)diag))[0] == 2 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)diag))[1] == 6) score += 100;\n"
        "    Py_XDECREF(diag);\n"
        "    diag = PyArray_Diagonal((PyArrayObject *)matrix, -1, 0, 1);\n"
        "    if (diag != NULL && PyArray_NDIM((PyArrayObject *)diag) == 1 &&\n"
        "        PyArray_DIMS((PyArrayObject *)diag)[0] == 1 && PyArray_TYPE((PyArrayObject *)diag) == NPY_INT &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)diag))[0] == 4) score += 1000;\n"
        "    Py_XDECREF(diag);\n"
        "    diag = PyArray_Diagonal((PyArrayObject *)matrix, 0, 1, 0);\n"
        "    if (diag != NULL && PyArray_NDIM((PyArrayObject *)diag) == 1 &&\n"
        "        PyArray_DIMS((PyArrayObject *)diag)[0] == 2 && PyArray_TYPE((PyArrayObject *)diag) == NPY_INT &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)diag))[0] == 1 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)diag))[1] == 99) score += 10000;\n"
        "    else if (PyErr_Occurred() != NULL) { PyErr_Clear(); }\n"
        "    Py_XDECREF(diag);\n"
        "    diag = PyArray_Diagonal((PyArrayObject *)matrix, 0, -1, -2);\n"
        "    if (diag != NULL && PyArray_NDIM((PyArrayObject *)diag) == 1 &&\n"
        "        PyArray_DIMS((PyArrayObject *)diag)[0] == 2 && PyArray_TYPE((PyArrayObject *)diag) == NPY_INT &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)diag))[0] == 1 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)diag))[1] == 99) score += 1000000;\n"
        "    else if (PyErr_Occurred() != NULL) { PyErr_Clear(); }\n"
        "    Py_XDECREF(diag);\n"
        "    diag = PyArray_Diagonal((PyArrayObject *)matrix, 0, 1, 1);\n"
        "    if (diag == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000000; }\n"
        "    Py_XDECREF(diag);\n"
        "    PyObject *objarr = PyArray_SimpleNew(2, dims, NPY_OBJECT);\n"
        "    if (objarr == NULL) {\n"
        "        Py_DECREF(matrix);\n"
        "        return NULL;\n"
        "    }\n"
        "    diag = PyArray_Diagonal((PyArrayObject *)objarr, 0, 0, 1);\n"
        "    if (diag == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_XDECREF(diag);\n"
        "    Py_DECREF(objarr);\n"
        "    Py_DECREF(matrix);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_trace_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[2] = {2, 3};\n"
        "    PyObject *matrix = PyArray_SimpleNew(2, dims, NPY_INT);\n"
        "    if (matrix == NULL) return NULL;\n"
        "    int *data = (int *)PyArray_DATA((PyArrayObject *)matrix);\n"
        "    data[0] = 1; data[1] = 2; data[2] = 3; data[3] = 4; data[4] = 5; data[5] = 6;\n"
        "    PyObject *trace = PyArray_Trace((PyArrayObject *)matrix, 0, 0, 1, NPY_NOTYPE, NULL);\n"
        "    if (trace != NULL && PyLong_AsLong(trace) == 6) score += 1;\n"
        "    Py_XDECREF(trace);\n"
        "    trace = PyArray_Trace((PyArrayObject *)matrix, 1, 0, 1, NPY_NOTYPE, NULL);\n"
        "    if (trace != NULL && PyLong_AsLong(trace) == 8) score += 10;\n"
        "    Py_XDECREF(trace);\n"
        "    trace = PyArray_Trace((PyArrayObject *)matrix, -1, 0, 1, NPY_NOTYPE, NULL);\n"
        "    if (trace != NULL && PyLong_AsLong(trace) == 4) score += 100;\n"
        "    Py_XDECREF(trace);\n"
        "    trace = PyArray_Trace((PyArrayObject *)matrix, 0, 1, 0, NPY_NOTYPE, NULL);\n"
        "    if (trace != NULL && PyLong_AsLong(trace) == 6) score += 1000000;\n"
        "    else if (PyErr_Occurred() != NULL) { PyErr_Clear(); }\n"
        "    Py_XDECREF(trace);\n"
        "    trace = PyArray_Trace((PyArrayObject *)matrix, 0, -1, -2, NPY_NOTYPE, NULL);\n"
        "    if (trace != NULL && PyLong_AsLong(trace) == 6) score += 10000000;\n"
        "    else if (PyErr_Occurred() != NULL) { PyErr_Clear(); }\n"
        "    Py_XDECREF(trace);\n"
        "    PyObject *out_arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (out_arr == NULL) {\n"
        "        Py_DECREF(matrix);\n"
        "        return NULL;\n"
        "    }\n"
        "    trace = PyArray_Trace((PyArrayObject *)matrix, 0, 0, 1, NPY_NOTYPE, (PyArrayObject *)out_arr);\n"
        "    if (trace == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000; }\n"
        "    Py_XDECREF(trace);\n"
        "    trace = PyArray_Trace((PyArrayObject *)matrix, 0, 0, 1, NPY_DOUBLE, NULL);\n"
        "    if (trace != NULL && PyFloat_Check(trace) && PyFloat_AsDouble(trace) == 6.0) score += 10000;\n"
        "    else if (PyErr_Occurred() != NULL) { PyErr_Clear(); }\n"
        "    Py_XDECREF(trace);\n"
        "    PyObject *objarr = PyArray_SimpleNew(2, dims, NPY_OBJECT);\n"
        "    if (objarr == NULL) {\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(matrix);\n"
        "        return NULL;\n"
        "    }\n"
        "    trace = PyArray_Trace((PyArrayObject *)objarr, 0, 0, 1, NPY_NOTYPE, NULL);\n"
        "    if (trace == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_XDECREF(trace);\n"
        "    Py_DECREF(objarr);\n"
        "    Py_DECREF(out_arr);\n"
        "    Py_DECREF(matrix);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_clip_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[1] = {5};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (arr == NULL) return NULL;\n"
        "    int *data = (int *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    data[0] = -5; data[1] = 0; data[2] = 5; data[3] = 10; data[4] = 15;\n"
        "    PyObject *lo = PyLong_FromLong(0);\n"
        "    PyObject *hi = PyLong_FromLong(9);\n"
        "    if (lo == NULL || hi == NULL) {\n"
        "        Py_XDECREF(lo);\n"
        "        Py_XDECREF(hi);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *out = PyArray_Clip((PyArrayObject *)arr, lo, hi, NULL);\n"
        "    if (out != NULL && out != arr && PyArray_BASE((PyArrayObject *)out) == NULL &&\n"
        "        PyArray_NDIM((PyArrayObject *)out) == 1 && PyArray_DIMS((PyArrayObject *)out)[0] == 5 &&\n"
        "        PyArray_TYPE((PyArrayObject *)out) == NPY_INT &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[0] == 0 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[1] == 0 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[2] == 5 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[3] == 9 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[4] == 9) score += 1;\n"
        "    data[2] = 99;\n"
        "    if (out != NULL && ((int *)PyArray_DATA((PyArrayObject *)out))[2] == 5) score += 10;\n"
        "    Py_XDECREF(out);\n"
        "    Py_DECREF(hi);\n"
        "    hi = NULL;\n"
        "    Py_DECREF(lo);\n"
        "    lo = PyLong_FromLong(7);\n"
        "    if (lo == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    out = PyArray_Clip((PyArrayObject *)arr, lo, NULL, NULL);\n"
        "    if (out != NULL && ((int *)PyArray_DATA((PyArrayObject *)out))[0] == 7 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[1] == 7 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[2] == 99 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[3] == 10 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[4] == 15) score += 100;\n"
        "    Py_XDECREF(out);\n"
        "    Py_DECREF(lo);\n"
        "    hi = PyLong_FromLong(3);\n"
        "    if (hi == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    out = PyArray_Clip((PyArrayObject *)arr, NULL, hi, NULL);\n"
        "    if (out != NULL && ((int *)PyArray_DATA((PyArrayObject *)out))[0] == -5 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[1] == 0 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[2] == 3 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[3] == 3 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[4] == 3) score += 1000;\n"
        "    Py_XDECREF(out);\n"
        "    Py_DECREF(hi);\n"
        "    out = PyArray_Clip((PyArrayObject *)arr, NULL, NULL, NULL);\n"
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    Py_XDECREF(out);\n"
        "    PyObject *out_arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (out_arr == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    lo = PyLong_FromLong(0);\n"
        "    if (lo == NULL) {\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    out = PyArray_Clip((PyArrayObject *)arr, lo, NULL, (PyArrayObject *)out_arr);\n"
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_XDECREF(out);\n"
        "    Py_DECREF(lo);\n"
        "    PyObject *objarr = PyArray_SimpleNew(1, dims, NPY_OBJECT);\n"
        "    if (objarr == NULL) {\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    hi = PyLong_FromLong(1);\n"
        "    if (hi == NULL) {\n"
        "        Py_DECREF(objarr);\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    out = PyArray_Clip((PyArrayObject *)objarr, NULL, hi, NULL);\n"
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000000; }\n"
        "    Py_XDECREF(out);\n"
        "    Py_DECREF(hi);\n"
        "    npy_intp ddims[1] = {2};\n"
        "    PyObject *darr = PyArray_SimpleNew(1, ddims, NPY_DOUBLE);\n"
        "    if (darr == NULL) {\n"
        "        Py_DECREF(objarr);\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    double *ddata = (double *)PyArray_DATA((PyArrayObject *)darr);\n"
        "    ddata[0] = -1.5; ddata[1] = 3.5;\n"
        "    lo = PyFloat_FromDouble(-1.0);\n"
        "    hi = PyFloat_FromDouble(2.0);\n"
        "    if (lo == NULL || hi == NULL) {\n"
        "        Py_XDECREF(lo);\n"
        "        Py_XDECREF(hi);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(objarr);\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    out = PyArray_Clip((PyArrayObject *)darr, lo, hi, NULL);\n"
        "    if (out != NULL && PyArray_TYPE((PyArrayObject *)out) == NPY_DOUBLE &&\n"
        "        ((double *)PyArray_DATA((PyArrayObject *)out))[0] == -1.0 &&\n"
        "        ((double *)PyArray_DATA((PyArrayObject *)out))[1] == 2.0) score += 10000000;\n"
        "    Py_XDECREF(out);\n"
        "    Py_DECREF(hi);\n"
        "    Py_DECREF(lo);\n"
        "    Py_DECREF(darr);\n"
        "    Py_DECREF(objarr);\n"
        "    Py_DECREF(out_arr);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_conjugate_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[1] = {3};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (arr == NULL) return NULL;\n"
        "    int *data = (int *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    data[0] = 2; data[1] = -3; data[2] = 5;\n"
        "    PyObject *out = PyArray_Conjugate((PyArrayObject *)arr, NULL);\n"
        "    if (out == arr && PyArray_NDIM((PyArrayObject *)out) == 1 &&\n"
        "        PyArray_DIMS((PyArrayObject *)out)[0] == 3 && PyArray_TYPE((PyArrayObject *)out) == NPY_INT &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[0] == 2 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[1] == -3 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out))[2] == 5) score += 1;\n"
        "    Py_XDECREF(out);\n"
        "    PyObject *out_arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (out_arr == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    out = PyArray_Conjugate((PyArrayObject *)arr, (PyArrayObject *)out_arr);\n"
        "    if (out == out_arr && ((int *)PyArray_DATA((PyArrayObject *)out_arr))[0] == 2 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out_arr))[1] == -3 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)out_arr))[2] == 5) score += 10;\n"
        "    data[0] = 7;\n"
        "    if (((int *)PyArray_DATA((PyArrayObject *)out_arr))[0] == 2) score += 100;\n"
        "    Py_XDECREF(out);\n"
        "    npy_intp ddims[1] = {2};\n"
        "    PyObject *darr = PyArray_SimpleNew(1, ddims, NPY_DOUBLE);\n"
        "    if (darr == NULL) {\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    double *ddata = (double *)PyArray_DATA((PyArrayObject *)darr);\n"
        "    ddata[0] = -1.5; ddata[1] = 2.5;\n"
        "    out = PyArray_Conjugate((PyArrayObject *)darr, NULL);\n"
        "    if (out == darr && PyArray_TYPE((PyArrayObject *)out) == NPY_DOUBLE &&\n"
        "        ((double *)PyArray_DATA((PyArrayObject *)out))[0] == -1.5 &&\n"
        "        ((double *)PyArray_DATA((PyArrayObject *)out))[1] == 2.5) score += 1000;\n"
        "    Py_XDECREF(out);\n"
        "    PyObject *wrong_out = PyArray_SimpleNew(1, dims, NPY_DOUBLE);\n"
        "    if (wrong_out == NULL) {\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    out = PyArray_Conjugate((PyArrayObject *)arr, (PyArrayObject *)wrong_out);\n"
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    Py_XDECREF(out);\n"
        "    PyObject *objarr = PyArray_SimpleNew(1, dims, NPY_OBJECT);\n"
        "    if (objarr == NULL) {\n"
        "        Py_DECREF(wrong_out);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    out = PyArray_Conjugate((PyArrayObject *)objarr, NULL);\n"
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_XDECREF(out);\n"
        "    PyObject *carr = PyArray_SimpleNew(1, ddims, NPY_CDOUBLE);\n"
        "    if (carr == NULL) {\n"
        "        Py_DECREF(objarr);\n"
        "        Py_DECREF(wrong_out);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    out = PyArray_Conjugate((PyArrayObject *)carr, NULL);\n"
        "    if (out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000000; }\n"
        "    Py_XDECREF(out);\n"
        "    Py_DECREF(carr);\n"
        "    Py_DECREF(objarr);\n"
        "    Py_DECREF(wrong_out);\n"
        "    Py_DECREF(darr);\n"
        "    Py_DECREF(out_arr);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_std_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[1] = {2};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (arr == NULL) return NULL;\n"
        "    int *data = (int *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    data[0] = 1; data[1] = 3;\n"
        "    PyObject *stdv = PyArray_Std((PyArrayObject *)arr, 0, NPY_NOTYPE, NULL, 0);\n"
        "    if (stdv != NULL && PyFloat_AsDouble(stdv) == 1.0) score += 1;\n"
        "    Py_XDECREF(stdv);\n"
        "    stdv = PyArray_Std((PyArrayObject *)arr, -1, NPY_NOTYPE, NULL, 0);\n"
        "    if (stdv != NULL && PyFloat_AsDouble(stdv) == 1.0) score += 10;\n"
        "    Py_XDECREF(stdv);\n"
        "    npy_intp ddims[1] = {2};\n"
        "    PyObject *darr = PyArray_SimpleNew(1, ddims, NPY_DOUBLE);\n"
        "    if (darr == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    double *ddata = (double *)PyArray_DATA((PyArrayObject *)darr);\n"
        "    ddata[0] = 1.0; ddata[1] = 5.0;\n"
        "    stdv = PyArray_Std((PyArrayObject *)darr, 0, NPY_NOTYPE, NULL, 0);\n"
        "    if (stdv != NULL && PyFloat_AsDouble(stdv) == 2.0) score += 100;\n"
        "    Py_XDECREF(stdv);\n"
        "    stdv = PyArray_Std((PyArrayObject *)darr, 0, NPY_NOTYPE, NULL, 1);\n"
        "    if (stdv != NULL && PyFloat_AsDouble(stdv) == 4.0) score += 1000;\n"
        "    Py_XDECREF(stdv);\n"
        "    PyObject *out_arr = PyArray_SimpleNew(0, NULL, NPY_DOUBLE);\n"
        "    PyObject *bad_out_arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (out_arr == NULL || bad_out_arr == NULL) {\n"
        "        Py_XDECREF(bad_out_arr);\n"
        "        Py_XDECREF(out_arr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    stdv = PyArray_Std((PyArrayObject *)arr, 0, NPY_NOTYPE, (PyArrayObject *)out_arr, 0);\n"
        "    if (stdv == out_arr && ((double *)PyArray_DATA((PyArrayObject *)out_arr))[0] == 1.0) score += 10000;\n"
        "    else if (PyErr_Occurred() != NULL) { PyErr_Clear(); }\n"
        "    Py_XDECREF(stdv);\n"
        "    stdv = PyArray_Std((PyArrayObject *)arr, 0, NPY_NOTYPE, (PyArrayObject *)bad_out_arr, 0);\n"
        "    if (stdv == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000000000; }\n"
        "    Py_XDECREF(stdv);\n"
        "    stdv = PyArray_Std((PyArrayObject *)arr, 1, NPY_NOTYPE, NULL, 0);\n"
        "    if (stdv == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_XDECREF(stdv);\n"
        "    PyObject *objarr = PyArray_SimpleNew(1, dims, NPY_OBJECT);\n"
        "    if (objarr == NULL) {\n"
        "        Py_DECREF(bad_out_arr);\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    stdv = PyArray_Std((PyArrayObject *)objarr, 0, NPY_NOTYPE, NULL, 0);\n"
        "    if (stdv == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000000; }\n"
        "    Py_XDECREF(stdv);\n"
        "    PyObject *carr = PyArray_SimpleNew(1, ddims, NPY_CDOUBLE);\n"
        "    if (carr == NULL) {\n"
        "        Py_DECREF(objarr);\n"
        "        Py_DECREF(bad_out_arr);\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    stdv = PyArray_Std((PyArrayObject *)carr, 0, NPY_NOTYPE, NULL, 0);\n"
        "    if (stdv == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000000; }\n"
        "    Py_XDECREF(stdv);\n"
        "    stdv = PyArray_Std((PyArrayObject *)arr, 0, NPY_DOUBLE, NULL, 0);\n"
        "    if (stdv != NULL && PyFloat_AsDouble(stdv) == 1.0) score += 100000000;\n"
        "    else if (PyErr_Occurred() != NULL) { PyErr_Clear(); }\n"
        "    Py_XDECREF(stdv);\n"
        "    npy_intp edims[1] = {0};\n"
        "    PyObject *empty = PyArray_SimpleNew(1, edims, NPY_INT);\n"
        "    if (empty == NULL) {\n"
        "        Py_DECREF(carr);\n"
        "        Py_DECREF(objarr);\n"
        "        Py_DECREF(bad_out_arr);\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    stdv = PyArray_Std((PyArrayObject *)empty, 0, NPY_NOTYPE, NULL, 0);\n"
        "    if (stdv == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000000000; }\n"
        "    Py_XDECREF(stdv);\n"
        "    Py_DECREF(empty);\n"
        "    Py_DECREF(carr);\n"
        "    Py_DECREF(objarr);\n"
        "    Py_DECREF(bad_out_arr);\n"
        "    Py_DECREF(out_arr);\n"
        "    Py_DECREF(darr);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_round_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[1] = {3};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (arr == NULL) return NULL;\n"
        "    int *data = (int *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    data[0] = 1; data[1] = -3; data[2] = 5;\n"
        "    PyObject *rounded = PyArray_Round((PyArrayObject *)arr, 3, NULL);\n"
        "    if (rounded != NULL && rounded != arr && PyArray_TYPE((PyArrayObject *)rounded) == NPY_INT &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)rounded))[0] == 1 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)rounded))[1] == -3 &&\n"
        "        ((int *)PyArray_DATA((PyArrayObject *)rounded))[2] == 5) score += 1;\n"
        "    data[0] = 9;\n"
        "    if (rounded != NULL && ((int *)PyArray_DATA((PyArrayObject *)rounded))[0] == 1) score += 10;\n"
        "    Py_XDECREF(rounded);\n"
        "    npy_intp ddims[1] = {3};\n"
        "    PyObject *darr = PyArray_SimpleNew(1, ddims, NPY_DOUBLE);\n"
        "    if (darr == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    double *ddata = (double *)PyArray_DATA((PyArrayObject *)darr);\n"
        "    ddata[0] = 1.2; ddata[1] = -2.7; ddata[2] = 3.0;\n"
        "    rounded = PyArray_Round((PyArrayObject *)darr, 0, NULL);\n"
        "    if (rounded != NULL && PyArray_TYPE((PyArrayObject *)rounded) == NPY_DOUBLE) {\n"
        "        double *out = (double *)PyArray_DATA((PyArrayObject *)rounded);\n"
        "        if (out[0] == 1.0 && out[1] == -3.0 && out[2] == 3.0) score += 100;\n"
        "    }\n"
        "    Py_XDECREF(rounded);\n"
        "    npy_intp edims[1] = {0};\n"
        "    PyObject *empty = PyArray_SimpleNew(1, edims, NPY_INT);\n"
        "    if (empty == NULL) {\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    rounded = PyArray_Round((PyArrayObject *)empty, 0, NULL);\n"
        "    if (rounded != NULL && PyArray_TYPE((PyArrayObject *)rounded) == NPY_INT && PyArray_SIZE((PyArrayObject *)rounded) == 0) score += 1000;\n"
        "    Py_XDECREF(rounded);\n"
        "    PyObject *out_arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (out_arr == NULL) {\n"
        "        Py_DECREF(empty);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    rounded = PyArray_Round((PyArrayObject *)arr, 0, (PyArrayObject *)out_arr);\n"
        "    if (rounded == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    Py_XDECREF(rounded);\n"
        "    rounded = PyArray_Round((PyArrayObject *)darr, 1, NULL);\n"
        "    if (rounded == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_XDECREF(rounded);\n"
        "    PyObject *objarr = PyArray_SimpleNew(1, dims, NPY_OBJECT);\n"
        "    if (objarr == NULL) {\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(empty);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    rounded = PyArray_Round((PyArrayObject *)objarr, 0, NULL);\n"
        "    if (rounded == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000000; }\n"
        "    Py_XDECREF(rounded);\n"
        "    PyObject *carr = PyArray_SimpleNew(1, ddims, NPY_CDOUBLE);\n"
        "    if (carr == NULL) {\n"
        "        Py_DECREF(objarr);\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(empty);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    rounded = PyArray_Round((PyArrayObject *)carr, 0, NULL);\n"
        "    if (rounded == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000000; }\n"
        "    Py_XDECREF(rounded);\n"
        "    Py_DECREF(carr);\n"
        "    Py_DECREF(objarr);\n"
        "    Py_DECREF(out_arr);\n"
        "    Py_DECREF(empty);\n"
        "    Py_DECREF(darr);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_sum_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[1] = {3};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (arr == NULL) return NULL;\n"
        "    int *data = (int *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    data[0] = 2; data[1] = -3; data[2] = 5;\n"
        "    PyObject *sum = PyArray_Sum((PyArrayObject *)arr, 0, NPY_NOTYPE, NULL);\n"
        "    if (sum != NULL && PyLong_AsLong(sum) == 4) score += 1;\n"
        "    Py_XDECREF(sum);\n"
        "    sum = PyArray_Sum((PyArrayObject *)arr, -1, NPY_NOTYPE, NULL);\n"
        "    if (sum != NULL && PyLong_AsLong(sum) == 4) score += 10;\n"
        "    Py_XDECREF(sum);\n"
        "    npy_intp ddims[1] = {2};\n"
        "    PyObject *darr = PyArray_SimpleNew(1, ddims, NPY_DOUBLE);\n"
        "    if (darr == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    double *ddata = (double *)PyArray_DATA((PyArrayObject *)darr);\n"
        "    ddata[0] = -1.5; ddata[1] = 2.5;\n"
        "    sum = PyArray_Sum((PyArrayObject *)darr, 0, NPY_NOTYPE, NULL);\n"
        "    if (sum != NULL && PyFloat_AsDouble(sum) == 1.0) score += 100;\n"
        "    Py_XDECREF(sum);\n"
        "    sum = PyArray_Sum((PyArrayObject *)arr, 0, NPY_DOUBLE, NULL);\n"
        "    if (sum != NULL && PyFloat_AsDouble(sum) == 4.0) score += 1000;\n"
        "    else if (PyErr_Occurred() != NULL) { PyErr_Clear(); }\n"
        "    Py_XDECREF(sum);\n"
        "    PyObject *out_arr = PyArray_SimpleNew(0, NULL, NPY_DOUBLE);\n"
        "    if (out_arr == NULL) {\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    sum = PyArray_Sum((PyArrayObject *)arr, 0, NPY_NOTYPE, (PyArrayObject *)out_arr);\n"
        "    if (sum == out_arr && PyArray_SIZE((PyArrayObject *)out_arr) == 1) {\n"
        "        double *out_data = (double *)PyArray_DATA((PyArrayObject *)out_arr);\n"
        "        if (out_data != NULL && out_data[0] == 4.0) score += 10000;\n"
        "    } else if (sum == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); }\n"
        "    Py_XDECREF(sum);\n"
        "    PyObject *bad_out_arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (bad_out_arr == NULL) {\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    sum = PyArray_Sum((PyArrayObject *)arr, 0, NPY_NOTYPE, (PyArrayObject *)bad_out_arr);\n"
        "    if (sum == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000000; }\n"
        "    Py_XDECREF(sum);\n"
        "    sum = PyArray_Sum((PyArrayObject *)arr, 1, NPY_NOTYPE, NULL);\n"
        "    if (sum == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_XDECREF(sum);\n"
        "    PyObject *objarr = PyArray_SimpleNew(1, dims, NPY_OBJECT);\n"
        "    if (objarr == NULL) {\n"
        "        Py_DECREF(bad_out_arr);\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    sum = PyArray_Sum((PyArrayObject *)objarr, 0, NPY_NOTYPE, NULL);\n"
        "    if (sum == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000000; }\n"
        "    Py_XDECREF(sum);\n"
        "    PyObject *carr = PyArray_SimpleNew(1, ddims, NPY_CDOUBLE);\n"
        "    if (carr == NULL) {\n"
        "        Py_DECREF(objarr);\n"
        "        Py_DECREF(bad_out_arr);\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    sum = PyArray_Sum((PyArrayObject *)carr, 0, NPY_NOTYPE, NULL);\n"
        "    if (sum == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000000; }\n"
        "    Py_XDECREF(sum);\n"
        "    Py_DECREF(carr);\n"
        "    Py_DECREF(objarr);\n"
        "    Py_DECREF(bad_out_arr);\n"
        "    Py_DECREF(out_arr);\n"
        "    Py_DECREF(darr);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_prod_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[1] = {3};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (arr == NULL) return NULL;\n"
        "    int *data = (int *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    data[0] = 2; data[1] = -3; data[2] = 5;\n"
        "    PyObject *prod = PyArray_Prod((PyArrayObject *)arr, 0, NPY_NOTYPE, NULL);\n"
        "    if (prod != NULL && PyLong_AsLong(prod) == -30) score += 1;\n"
        "    Py_XDECREF(prod);\n"
        "    prod = PyArray_Prod((PyArrayObject *)arr, -1, NPY_NOTYPE, NULL);\n"
        "    if (prod != NULL && PyLong_AsLong(prod) == -30) score += 10;\n"
        "    Py_XDECREF(prod);\n"
        "    npy_intp ddims[1] = {2};\n"
        "    PyObject *darr = PyArray_SimpleNew(1, ddims, NPY_DOUBLE);\n"
        "    if (darr == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    double *ddata = (double *)PyArray_DATA((PyArrayObject *)darr);\n"
        "    ddata[0] = -1.5; ddata[1] = 2.0;\n"
        "    prod = PyArray_Prod((PyArrayObject *)darr, 0, NPY_NOTYPE, NULL);\n"
        "    if (prod != NULL && PyFloat_AsDouble(prod) == -3.0) score += 100;\n"
        "    Py_XDECREF(prod);\n"
        "    prod = PyArray_Prod((PyArrayObject *)arr, 0, NPY_DOUBLE, NULL);\n"
        "    if (prod != NULL && PyFloat_AsDouble(prod) == -30.0) score += 1000;\n"
        "    else if (PyErr_Occurred() != NULL) { PyErr_Clear(); }\n"
        "    Py_XDECREF(prod);\n"
        "    PyObject *out_arr = PyArray_SimpleNew(0, NULL, NPY_DOUBLE);\n"
        "    PyObject *bad_out_arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (out_arr == NULL || bad_out_arr == NULL) {\n"
        "        Py_XDECREF(bad_out_arr);\n"
        "        Py_XDECREF(out_arr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    prod = PyArray_Prod((PyArrayObject *)arr, 0, NPY_NOTYPE, (PyArrayObject *)out_arr);\n"
        "    if (prod == out_arr && ((double *)PyArray_DATA((PyArrayObject *)out_arr))[0] == -30.0) score += 10000;\n"
        "    else if (PyErr_Occurred() != NULL) { PyErr_Clear(); }\n"
        "    Py_XDECREF(prod);\n"
        "    prod = PyArray_Prod((PyArrayObject *)arr, 0, NPY_NOTYPE, (PyArrayObject *)bad_out_arr);\n"
        "    if (prod == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000000; }\n"
        "    Py_XDECREF(prod);\n"
        "    prod = PyArray_Prod((PyArrayObject *)arr, 1, NPY_NOTYPE, NULL);\n"
        "    if (prod == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_XDECREF(prod);\n"
        "    PyObject *objarr = PyArray_SimpleNew(1, dims, NPY_OBJECT);\n"
        "    if (objarr == NULL) {\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    prod = PyArray_Prod((PyArrayObject *)objarr, 0, NPY_NOTYPE, NULL);\n"
        "    if (prod == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000000; }\n"
        "    Py_XDECREF(prod);\n"
        "    PyObject *carr = PyArray_SimpleNew(1, ddims, NPY_CDOUBLE);\n"
        "    if (carr == NULL) {\n"
        "        Py_DECREF(objarr);\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    prod = PyArray_Prod((PyArrayObject *)carr, 0, NPY_NOTYPE, NULL);\n"
        "    if (prod == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000000; }\n"
        "    Py_XDECREF(prod);\n"
        "    Py_DECREF(carr);\n"
        "    Py_DECREF(objarr);\n"
        "    Py_DECREF(bad_out_arr);\n"
        "    Py_DECREF(out_arr);\n"
        "    Py_DECREF(darr);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_cumsum_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[1] = {3};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (arr == NULL) return NULL;\n"
        "    int *data = (int *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    data[0] = 1; data[1] = 2; data[2] = 3;\n"
        "    PyObject *cum = PyArray_CumSum((PyArrayObject *)arr, 0, NPY_NOTYPE, NULL);\n"
        "    if (cum != NULL && PyArray_TYPE((PyArrayObject *)cum) == NPY_INT && PyArray_SIZE((PyArrayObject *)cum) == 3) {\n"
        "        int *out = (int *)PyArray_DATA((PyArrayObject *)cum);\n"
        "        if (out[0] == 1 && out[1] == 3 && out[2] == 6) score += 1;\n"
        "    }\n"
        "    Py_XDECREF(cum);\n"
        "    cum = PyArray_CumSum((PyArrayObject *)arr, -1, NPY_NOTYPE, NULL);\n"
        "    if (cum != NULL) {\n"
        "        int *out = (int *)PyArray_DATA((PyArrayObject *)cum);\n"
        "        if (out[0] == 1 && out[1] == 3 && out[2] == 6) score += 10;\n"
        "    }\n"
        "    Py_XDECREF(cum);\n"
        "    npy_intp ddims[1] = {2};\n"
        "    PyObject *darr = PyArray_SimpleNew(1, ddims, NPY_DOUBLE);\n"
        "    if (darr == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    double *ddata = (double *)PyArray_DATA((PyArrayObject *)darr);\n"
        "    ddata[0] = 0.5; ddata[1] = 1.5;\n"
        "    cum = PyArray_CumSum((PyArrayObject *)darr, 0, NPY_NOTYPE, NULL);\n"
        "    if (cum != NULL && PyArray_TYPE((PyArrayObject *)cum) == NPY_DOUBLE) {\n"
        "        double *out = (double *)PyArray_DATA((PyArrayObject *)cum);\n"
        "        if (out[0] == 0.5 && out[1] == 2.0) score += 100;\n"
        "    }\n"
        "    Py_XDECREF(cum);\n"
        "    npy_intp edims[1] = {0};\n"
        "    PyObject *empty = PyArray_SimpleNew(1, edims, NPY_INT);\n"
        "    if (empty == NULL) {\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    cum = PyArray_CumSum((PyArrayObject *)empty, 0, NPY_NOTYPE, NULL);\n"
        "    if (cum != NULL && PyArray_TYPE((PyArrayObject *)cum) == NPY_INT && PyArray_SIZE((PyArrayObject *)cum) == 0) score += 1000;\n"
        "    Py_XDECREF(cum);\n"
        "    PyObject *out_arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    PyObject *bad_out_arr = PyArray_SimpleNew(1, ddims, NPY_INT);\n"
        "    if (out_arr == NULL || bad_out_arr == NULL) {\n"
        "        Py_XDECREF(bad_out_arr);\n"
        "        Py_XDECREF(out_arr);\n"
        "        Py_DECREF(empty);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    cum = PyArray_CumSum((PyArrayObject *)arr, 0, NPY_NOTYPE, (PyArrayObject *)out_arr);\n"
        "    if (cum == out_arr && PyArray_TYPE((PyArrayObject *)out_arr) == NPY_INT) {\n"
        "        int *out = (int *)PyArray_DATA((PyArrayObject *)out_arr);\n"
        "        if (out[0] == 1 && out[1] == 3 && out[2] == 6) score += 10000;\n"
        "    } else if (PyErr_Occurred() != NULL) { PyErr_Clear(); }\n"
        "    Py_XDECREF(cum);\n"
        "    cum = PyArray_CumSum((PyArrayObject *)arr, 0, NPY_NOTYPE, (PyArrayObject *)bad_out_arr);\n"
        "    if (cum == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000000000; }\n"
        "    Py_XDECREF(cum);\n"
        "    cum = PyArray_CumSum((PyArrayObject *)arr, 1, NPY_NOTYPE, NULL);\n"
        "    if (cum == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_XDECREF(cum);\n"
        "    PyObject *objarr = PyArray_SimpleNew(1, dims, NPY_OBJECT);\n"
        "    if (objarr == NULL) {\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(empty);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    cum = PyArray_CumSum((PyArrayObject *)objarr, 0, NPY_NOTYPE, NULL);\n"
        "    if (cum == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000000; }\n"
        "    Py_XDECREF(cum);\n"
        "    PyObject *carr = PyArray_SimpleNew(1, ddims, NPY_CDOUBLE);\n"
        "    if (carr == NULL) {\n"
        "        Py_DECREF(objarr);\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(empty);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    cum = PyArray_CumSum((PyArrayObject *)carr, 0, NPY_NOTYPE, NULL);\n"
        "    if (cum == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000000; }\n"
        "    Py_XDECREF(cum);\n"
        "    cum = PyArray_CumSum((PyArrayObject *)arr, 0, NPY_DOUBLE, NULL);\n"
        "    if (cum != NULL && PyArray_TYPE((PyArrayObject *)cum) == NPY_DOUBLE && PyArray_SIZE((PyArrayObject *)cum) == 3) {\n"
        "        double *out = (double *)PyArray_DATA((PyArrayObject *)cum);\n"
        "        if (out[0] == 1.0 && out[1] == 3.0 && out[2] == 6.0) score += 100000000;\n"
        "    } else if (PyErr_Occurred() != NULL) { PyErr_Clear(); }\n"
        "    Py_XDECREF(cum);\n"
        "    Py_DECREF(carr);\n"
        "    Py_DECREF(objarr);\n"
        "    Py_DECREF(bad_out_arr);\n"
        "    Py_DECREF(out_arr);\n"
        "    Py_DECREF(empty);\n"
        "    Py_DECREF(darr);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_cumprod_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[1] = {3};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (arr == NULL) return NULL;\n"
        "    int *data = (int *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    data[0] = 2; data[1] = 3; data[2] = 4;\n"
        "    PyObject *cum = PyArray_CumProd((PyArrayObject *)arr, 0, NPY_NOTYPE, NULL);\n"
        "    if (cum != NULL && PyArray_TYPE((PyArrayObject *)cum) == NPY_INT && PyArray_SIZE((PyArrayObject *)cum) == 3) {\n"
        "        int *out = (int *)PyArray_DATA((PyArrayObject *)cum);\n"
        "        if (out[0] == 2 && out[1] == 6 && out[2] == 24) score += 1;\n"
        "    }\n"
        "    Py_XDECREF(cum);\n"
        "    cum = PyArray_CumProd((PyArrayObject *)arr, -1, NPY_NOTYPE, NULL);\n"
        "    if (cum != NULL) {\n"
        "        int *out = (int *)PyArray_DATA((PyArrayObject *)cum);\n"
        "        if (out[0] == 2 && out[1] == 6 && out[2] == 24) score += 10;\n"
        "    }\n"
        "    Py_XDECREF(cum);\n"
        "    npy_intp ddims[1] = {2};\n"
        "    PyObject *darr = PyArray_SimpleNew(1, ddims, NPY_DOUBLE);\n"
        "    if (darr == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    double *ddata = (double *)PyArray_DATA((PyArrayObject *)darr);\n"
        "    ddata[0] = 0.5; ddata[1] = 4.0;\n"
        "    cum = PyArray_CumProd((PyArrayObject *)darr, 0, NPY_NOTYPE, NULL);\n"
        "    if (cum != NULL && PyArray_TYPE((PyArrayObject *)cum) == NPY_DOUBLE) {\n"
        "        double *out = (double *)PyArray_DATA((PyArrayObject *)cum);\n"
        "        if (out[0] == 0.5 && out[1] == 2.0) score += 100;\n"
        "    }\n"
        "    Py_XDECREF(cum);\n"
        "    npy_intp edims[1] = {0};\n"
        "    PyObject *empty = PyArray_SimpleNew(1, edims, NPY_INT);\n"
        "    if (empty == NULL) {\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    cum = PyArray_CumProd((PyArrayObject *)empty, 0, NPY_NOTYPE, NULL);\n"
        "    if (cum != NULL && PyArray_TYPE((PyArrayObject *)cum) == NPY_INT && PyArray_SIZE((PyArrayObject *)cum) == 0) score += 1000;\n"
        "    Py_XDECREF(cum);\n"
        "    PyObject *out_arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    PyObject *bad_out_arr = PyArray_SimpleNew(1, ddims, NPY_INT);\n"
        "    if (out_arr == NULL || bad_out_arr == NULL) {\n"
        "        Py_XDECREF(bad_out_arr);\n"
        "        Py_XDECREF(out_arr);\n"
        "        Py_DECREF(empty);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    cum = PyArray_CumProd((PyArrayObject *)arr, 0, NPY_NOTYPE, (PyArrayObject *)out_arr);\n"
        "    if (cum == out_arr && PyArray_TYPE((PyArrayObject *)out_arr) == NPY_INT) {\n"
        "        int *out = (int *)PyArray_DATA((PyArrayObject *)out_arr);\n"
        "        if (out[0] == 2 && out[1] == 6 && out[2] == 24) score += 10000;\n"
        "    } else if (PyErr_Occurred() != NULL) { PyErr_Clear(); }\n"
        "    Py_XDECREF(cum);\n"
        "    cum = PyArray_CumProd((PyArrayObject *)arr, 0, NPY_NOTYPE, (PyArrayObject *)bad_out_arr);\n"
        "    if (cum == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000000000; }\n"
        "    Py_XDECREF(cum);\n"
        "    cum = PyArray_CumProd((PyArrayObject *)arr, 1, NPY_NOTYPE, NULL);\n"
        "    if (cum == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_XDECREF(cum);\n"
        "    PyObject *objarr = PyArray_SimpleNew(1, dims, NPY_OBJECT);\n"
        "    if (objarr == NULL) {\n"
        "        Py_DECREF(bad_out_arr);\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(empty);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    cum = PyArray_CumProd((PyArrayObject *)objarr, 0, NPY_NOTYPE, NULL);\n"
        "    if (cum == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000000; }\n"
        "    Py_XDECREF(cum);\n"
        "    PyObject *carr = PyArray_SimpleNew(1, ddims, NPY_CDOUBLE);\n"
        "    if (carr == NULL) {\n"
        "        Py_DECREF(objarr);\n"
        "        Py_DECREF(bad_out_arr);\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(empty);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    cum = PyArray_CumProd((PyArrayObject *)carr, 0, NPY_NOTYPE, NULL);\n"
        "    if (cum == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000000; }\n"
        "    Py_XDECREF(cum);\n"
        "    cum = PyArray_CumProd((PyArrayObject *)arr, 0, NPY_DOUBLE, NULL);\n"
        "    if (cum != NULL && PyArray_TYPE((PyArrayObject *)cum) == NPY_DOUBLE && PyArray_SIZE((PyArrayObject *)cum) == 3) {\n"
        "        double *out = (double *)PyArray_DATA((PyArrayObject *)cum);\n"
        "        if (out[0] == 2.0 && out[1] == 6.0 && out[2] == 24.0) score += 100000000;\n"
        "    } else if (PyErr_Occurred() != NULL) { PyErr_Clear(); }\n"
        "    Py_XDECREF(cum);\n"
        "    Py_DECREF(carr);\n"
        "    Py_DECREF(objarr);\n"
        "    Py_DECREF(bad_out_arr);\n"
        "    Py_DECREF(out_arr);\n"
        "    Py_DECREF(empty);\n"
        "    Py_DECREF(darr);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_max_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[1] = {3};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (arr == NULL) return NULL;\n"
        "    int *data = (int *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    data[0] = -7; data[1] = 5; data[2] = 2;\n"
        "    PyObject *maxv = PyArray_Max((PyArrayObject *)arr, 0, NULL);\n"
        "    if (maxv != NULL && PyLong_AsLong(maxv) == 5) score += 1;\n"
        "    Py_XDECREF(maxv);\n"
        "    maxv = PyArray_Max((PyArrayObject *)arr, -1, NULL);\n"
        "    if (maxv != NULL && PyLong_AsLong(maxv) == 5) score += 10;\n"
        "    Py_XDECREF(maxv);\n"
        "    npy_intp ddims[1] = {2};\n"
        "    PyObject *darr = PyArray_SimpleNew(1, ddims, NPY_DOUBLE);\n"
        "    if (darr == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    double *ddata = (double *)PyArray_DATA((PyArrayObject *)darr);\n"
        "    ddata[0] = -1.5; ddata[1] = 2.0;\n"
        "    maxv = PyArray_Max((PyArrayObject *)darr, 0, NULL);\n"
        "    if (maxv != NULL && PyFloat_AsDouble(maxv) == 2.0) score += 100;\n"
        "    Py_XDECREF(maxv);\n"
        "    PyObject *out_arr = PyArray_SimpleNew(0, NULL, NPY_INT);\n"
        "    if (out_arr == NULL) {\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    maxv = PyArray_Max((PyArrayObject *)arr, 0, (PyArrayObject *)out_arr);\n"
        "    if (maxv == out_arr && PyArray_SIZE((PyArrayObject *)out_arr) == 1) {\n"
        "        int *out_data = (int *)PyArray_DATA((PyArrayObject *)out_arr);\n"
        "        if (out_data != NULL && out_data[0] == 5) score += 1000;\n"
        "    } else if (maxv == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); }\n"
        "    Py_XDECREF(maxv);\n"
        "    PyObject *bad_out_arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (bad_out_arr == NULL) {\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    maxv = PyArray_Max((PyArrayObject *)arr, 0, (PyArrayObject *)bad_out_arr);\n"
        "    if (maxv == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000000; }\n"
        "    Py_XDECREF(maxv);\n"
        "    maxv = PyArray_Max((PyArrayObject *)arr, 1, NULL);\n"
        "    if (maxv == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    Py_XDECREF(maxv);\n"
        "    PyObject *objarr = PyArray_SimpleNew(1, dims, NPY_OBJECT);\n"
        "    if (objarr == NULL) {\n"
        "        Py_DECREF(bad_out_arr);\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    maxv = PyArray_Max((PyArrayObject *)objarr, 0, NULL);\n"
        "    if (maxv == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_XDECREF(maxv);\n"
        "    PyObject *carr = PyArray_SimpleNew(1, ddims, NPY_CDOUBLE);\n"
        "    if (carr == NULL) {\n"
        "        Py_DECREF(objarr);\n"
        "        Py_DECREF(bad_out_arr);\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    maxv = PyArray_Max((PyArrayObject *)carr, 0, NULL);\n"
        "    if (maxv == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000000; }\n"
        "    Py_XDECREF(maxv);\n"
        "    npy_intp edims[1] = {0};\n"
        "    PyObject *empty = PyArray_SimpleNew(1, edims, NPY_INT);\n"
        "    if (empty == NULL) {\n"
        "        Py_DECREF(carr);\n"
        "        Py_DECREF(objarr);\n"
        "        Py_DECREF(bad_out_arr);\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    maxv = PyArray_Max((PyArrayObject *)empty, 0, NULL);\n"
        "    if (maxv == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000000; }\n"
        "    Py_XDECREF(maxv);\n"
        "    Py_DECREF(empty);\n"
        "    Py_DECREF(carr);\n"
        "    Py_DECREF(objarr);\n"
        "    Py_DECREF(bad_out_arr);\n"
        "    Py_DECREF(out_arr);\n"
        "    Py_DECREF(darr);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_min_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[1] = {3};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (arr == NULL) return NULL;\n"
        "    int *data = (int *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    data[0] = -7; data[1] = 5; data[2] = 2;\n"
        "    PyObject *minv = PyArray_Min((PyArrayObject *)arr, 0, NULL);\n"
        "    if (minv != NULL && PyLong_AsLong(minv) == -7) score += 1;\n"
        "    Py_XDECREF(minv);\n"
        "    minv = PyArray_Min((PyArrayObject *)arr, -1, NULL);\n"
        "    if (minv != NULL && PyLong_AsLong(minv) == -7) score += 10;\n"
        "    Py_XDECREF(minv);\n"
        "    npy_intp ddims[1] = {2};\n"
        "    PyObject *darr = PyArray_SimpleNew(1, ddims, NPY_DOUBLE);\n"
        "    if (darr == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    double *ddata = (double *)PyArray_DATA((PyArrayObject *)darr);\n"
        "    ddata[0] = -1.5; ddata[1] = 2.0;\n"
        "    minv = PyArray_Min((PyArrayObject *)darr, 0, NULL);\n"
        "    if (minv != NULL && PyFloat_AsDouble(minv) == -1.5) score += 100;\n"
        "    Py_XDECREF(minv);\n"
        "    PyObject *out_arr = PyArray_SimpleNew(0, NULL, NPY_INT);\n"
        "    if (out_arr == NULL) {\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    minv = PyArray_Min((PyArrayObject *)arr, 0, (PyArrayObject *)out_arr);\n"
        "    if (minv == out_arr && PyArray_SIZE((PyArrayObject *)out_arr) == 1) {\n"
        "        int *out_data = (int *)PyArray_DATA((PyArrayObject *)out_arr);\n"
        "        if (out_data != NULL && out_data[0] == -7) score += 1000;\n"
        "    } else if (minv == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); }\n"
        "    Py_XDECREF(minv);\n"
        "    PyObject *bad_out_arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (bad_out_arr == NULL) {\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    minv = PyArray_Min((PyArrayObject *)arr, 0, (PyArrayObject *)bad_out_arr);\n"
        "    if (minv == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000000; }\n"
        "    Py_XDECREF(minv);\n"
        "    minv = PyArray_Min((PyArrayObject *)arr, 1, NULL);\n"
        "    if (minv == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    Py_XDECREF(minv);\n"
        "    PyObject *objarr = PyArray_SimpleNew(1, dims, NPY_OBJECT);\n"
        "    if (objarr == NULL) {\n"
        "        Py_DECREF(bad_out_arr);\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    minv = PyArray_Min((PyArrayObject *)objarr, 0, NULL);\n"
        "    if (minv == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_XDECREF(minv);\n"
        "    PyObject *carr = PyArray_SimpleNew(1, ddims, NPY_CDOUBLE);\n"
        "    if (carr == NULL) {\n"
        "        Py_DECREF(objarr);\n"
        "        Py_DECREF(bad_out_arr);\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    minv = PyArray_Min((PyArrayObject *)carr, 0, NULL);\n"
        "    if (minv == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000000; }\n"
        "    Py_XDECREF(minv);\n"
        "    npy_intp edims[1] = {0};\n"
        "    PyObject *empty = PyArray_SimpleNew(1, edims, NPY_INT);\n"
        "    if (empty == NULL) {\n"
        "        Py_DECREF(carr);\n"
        "        Py_DECREF(objarr);\n"
        "        Py_DECREF(bad_out_arr);\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    minv = PyArray_Min((PyArrayObject *)empty, 0, NULL);\n"
        "    if (minv == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000000; }\n"
        "    Py_XDECREF(minv);\n"
        "    Py_DECREF(empty);\n"
        "    Py_DECREF(carr);\n"
        "    Py_DECREF(objarr);\n"
        "    Py_DECREF(bad_out_arr);\n"
        "    Py_DECREF(out_arr);\n"
        "    Py_DECREF(darr);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_ptp_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[1] = {4};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (arr == NULL) return NULL;\n"
        "    int *data = (int *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    data[0] = -3; data[1] = 7; data[2] = 2; data[3] = -1;\n"
        "    PyObject *ptp = PyArray_Ptp((PyArrayObject *)arr, 0, NULL);\n"
        "    if (ptp != NULL && PyLong_AsLong(ptp) == 10) score += 1;\n"
        "    Py_XDECREF(ptp);\n"
        "    ptp = PyArray_Ptp((PyArrayObject *)arr, -1, NULL);\n"
        "    if (ptp != NULL && PyLong_AsLong(ptp) == 10) score += 10;\n"
        "    Py_XDECREF(ptp);\n"
        "    npy_intp ddims[1] = {3};\n"
        "    PyObject *darr = PyArray_SimpleNew(1, ddims, NPY_DOUBLE);\n"
        "    if (darr == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    double *ddata = (double *)PyArray_DATA((PyArrayObject *)darr);\n"
        "    ddata[0] = -1.5; ddata[1] = 2.25; ddata[2] = 0.25;\n"
        "    ptp = PyArray_Ptp((PyArrayObject *)darr, 0, NULL);\n"
        "    if (ptp != NULL) {\n"
        "        double got = PyFloat_AsDouble(ptp);\n"
        "        if (got > 3.749 && got < 3.751) score += 100;\n"
        "    }\n"
        "    Py_XDECREF(ptp);\n"
        "    PyObject *out_arr = PyArray_SimpleNew(0, NULL, NPY_INT);\n"
        "    if (out_arr == NULL) {\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    ptp = PyArray_Ptp((PyArrayObject *)arr, 0, (PyArrayObject *)out_arr);\n"
        "    if (ptp == out_arr && PyArray_SIZE((PyArrayObject *)out_arr) == 1) {\n"
        "        int *out_data = (int *)PyArray_DATA((PyArrayObject *)out_arr);\n"
        "        if (out_data != NULL && out_data[0] == 10) score += 1000;\n"
        "    } else if (ptp == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); }\n"
        "    Py_XDECREF(ptp);\n"
        "    PyObject *bad_out_arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (bad_out_arr == NULL) {\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    ptp = PyArray_Ptp((PyArrayObject *)arr, 0, (PyArrayObject *)bad_out_arr);\n"
        "    if (ptp == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000000; }\n"
        "    Py_XDECREF(ptp);\n"
        "    ptp = PyArray_Ptp((PyArrayObject *)arr, 1, NULL);\n"
        "    if (ptp == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    Py_XDECREF(ptp);\n"
        "    PyObject *objarr = PyArray_SimpleNew(1, dims, NPY_OBJECT);\n"
        "    if (objarr == NULL) {\n"
        "        Py_DECREF(bad_out_arr);\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    ptp = PyArray_Ptp((PyArrayObject *)objarr, 0, NULL);\n"
        "    if (ptp == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_XDECREF(ptp);\n"
        "    PyObject *carr = PyArray_SimpleNew(1, ddims, NPY_CDOUBLE);\n"
        "    if (carr == NULL) {\n"
        "        Py_DECREF(objarr);\n"
        "        Py_DECREF(bad_out_arr);\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    ptp = PyArray_Ptp((PyArrayObject *)carr, 0, NULL);\n"
        "    if (ptp == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000000; }\n"
        "    Py_XDECREF(ptp);\n"
        "    npy_intp edims[1] = {0};\n"
        "    PyObject *empty = PyArray_SimpleNew(1, edims, NPY_INT);\n"
        "    if (empty == NULL) {\n"
        "        Py_DECREF(carr);\n"
        "        Py_DECREF(objarr);\n"
        "        Py_DECREF(bad_out_arr);\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    ptp = PyArray_Ptp((PyArrayObject *)empty, 0, NULL);\n"
        "    if (ptp == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000000; }\n"
        "    Py_XDECREF(ptp);\n"
        "    Py_DECREF(empty);\n"
        "    Py_DECREF(carr);\n"
        "    Py_DECREF(objarr);\n"
        "    Py_DECREF(bad_out_arr);\n"
        "    Py_DECREF(out_arr);\n"
        "    Py_DECREF(darr);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_mean_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[1] = {4};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (arr == NULL) return NULL;\n"
        "    int *data = (int *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    data[0] = -3; data[1] = 7; data[2] = 2; data[3] = -2;\n"
        "    PyObject *mean = PyArray_Mean((PyArrayObject *)arr, 0, NPY_NOTYPE, NULL);\n"
        "    if (mean != NULL && PyFloat_AsDouble(mean) == 1.0) score += 1;\n"
        "    Py_XDECREF(mean);\n"
        "    mean = PyArray_Mean((PyArrayObject *)arr, -1, NPY_NOTYPE, NULL);\n"
        "    if (mean != NULL && PyFloat_AsDouble(mean) == 1.0) score += 10;\n"
        "    Py_XDECREF(mean);\n"
        "    npy_intp ddims[1] = {3};\n"
        "    PyObject *darr = PyArray_SimpleNew(1, ddims, NPY_DOUBLE);\n"
        "    if (darr == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    double *ddata = (double *)PyArray_DATA((PyArrayObject *)darr);\n"
        "    ddata[0] = -1.5; ddata[1] = 2.25; ddata[2] = 0.75;\n"
        "    mean = PyArray_Mean((PyArrayObject *)darr, 0, NPY_NOTYPE, NULL);\n"
        "    if (mean != NULL) {\n"
        "        double got = PyFloat_AsDouble(mean);\n"
        "        if (got > 0.499 && got < 0.501) score += 100;\n"
        "    }\n"
        "    Py_XDECREF(mean);\n"
        "    PyObject *out_arr = PyArray_SimpleNew(0, NULL, NPY_DOUBLE);\n"
        "    if (out_arr == NULL) {\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    mean = PyArray_Mean((PyArrayObject *)arr, 0, NPY_NOTYPE, (PyArrayObject *)out_arr);\n"
        "    if (mean == out_arr && PyArray_SIZE((PyArrayObject *)out_arr) == 1) {\n"
        "        double *out_data = (double *)PyArray_DATA((PyArrayObject *)out_arr);\n"
        "        if (out_data != NULL && out_data[0] == 1.0) score += 1000;\n"
        "    } else if (mean == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); }\n"
        "    Py_XDECREF(mean);\n"
        "    PyObject *bad_out_arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (bad_out_arr == NULL) {\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    mean = PyArray_Mean((PyArrayObject *)arr, 0, NPY_NOTYPE, (PyArrayObject *)bad_out_arr);\n"
        "    if (mean == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000000000; }\n"
        "    Py_XDECREF(mean);\n"
        "    mean = PyArray_Mean((PyArrayObject *)arr, 1, NPY_NOTYPE, NULL);\n"
        "    if (mean == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    Py_XDECREF(mean);\n"
        "    PyObject *objarr = PyArray_SimpleNew(1, dims, NPY_OBJECT);\n"
        "    if (objarr == NULL) {\n"
        "        Py_DECREF(bad_out_arr);\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    mean = PyArray_Mean((PyArrayObject *)objarr, 0, NPY_NOTYPE, NULL);\n"
        "    if (mean == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_XDECREF(mean);\n"
        "    PyObject *carr = PyArray_SimpleNew(1, ddims, NPY_CDOUBLE);\n"
        "    if (carr == NULL) {\n"
        "        Py_DECREF(objarr);\n"
        "        Py_DECREF(bad_out_arr);\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    mean = PyArray_Mean((PyArrayObject *)carr, 0, NPY_NOTYPE, NULL);\n"
        "    if (mean == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000000; }\n"
        "    Py_XDECREF(mean);\n"
        "    mean = PyArray_Mean((PyArrayObject *)arr, 0, NPY_DOUBLE, NULL);\n"
        "    if (mean != NULL && PyFloat_AsDouble(mean) == 1.0) score += 10000000;\n"
        "    else if (PyErr_Occurred() != NULL) { PyErr_Clear(); }\n"
        "    Py_XDECREF(mean);\n"
        "    npy_intp edims[1] = {0};\n"
        "    PyObject *empty = PyArray_SimpleNew(1, edims, NPY_INT);\n"
        "    if (empty == NULL) {\n"
        "        Py_DECREF(carr);\n"
        "        Py_DECREF(objarr);\n"
        "        Py_DECREF(bad_out_arr);\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    mean = PyArray_Mean((PyArrayObject *)empty, 0, NPY_NOTYPE, NULL);\n"
        "    if (mean == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000000; }\n"
        "    Py_XDECREF(mean);\n"
        "    Py_DECREF(empty);\n"
        "    Py_DECREF(carr);\n"
        "    Py_DECREF(objarr);\n"
        "    Py_DECREF(bad_out_arr);\n"
        "    Py_DECREF(out_arr);\n"
        "    Py_DECREF(darr);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_any_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[1] = {3};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (arr == NULL) return NULL;\n"
        "    int *data = (int *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    data[0] = 0; data[1] = 0; data[2] = 3;\n"
        "    PyObject *anyv = PyArray_Any((PyArrayObject *)arr, 0, NULL);\n"
        "    if (anyv != NULL && PyObject_IsTrue(anyv) == 1) score += 1;\n"
        "    Py_XDECREF(anyv);\n"
        "    anyv = PyArray_Any((PyArrayObject *)arr, -1, NULL);\n"
        "    if (anyv != NULL && PyObject_IsTrue(anyv) == 1) score += 10;\n"
        "    Py_XDECREF(anyv);\n"
        "    data[2] = 0;\n"
        "    anyv = PyArray_Any((PyArrayObject *)arr, 0, NULL);\n"
        "    if (anyv != NULL && PyObject_IsTrue(anyv) == 0 && PyErr_Occurred() == NULL) score += 100;\n"
        "    Py_XDECREF(anyv);\n"
        "    npy_intp ddims[1] = {2};\n"
        "    PyObject *darr = PyArray_SimpleNew(1, ddims, NPY_DOUBLE);\n"
        "    if (darr == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    double *ddata = (double *)PyArray_DATA((PyArrayObject *)darr);\n"
        "    ddata[0] = 0.0; ddata[1] = 0.25;\n"
        "    anyv = PyArray_Any((PyArrayObject *)darr, 0, NULL);\n"
        "    if (anyv != NULL && PyObject_IsTrue(anyv) == 1) score += 1000;\n"
        "    Py_XDECREF(anyv);\n"
        "    npy_intp bdims[1] = {2};\n"
        "    PyObject *barr = PyArray_SimpleNew(1, bdims, NPY_BOOL);\n"
        "    if (barr == NULL) {\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    unsigned char *bdata = (unsigned char *)PyArray_DATA((PyArrayObject *)barr);\n"
        "    bdata[0] = 0; bdata[1] = 0;\n"
        "    anyv = PyArray_Any((PyArrayObject *)barr, 0, NULL);\n"
        "    if (anyv != NULL && PyObject_IsTrue(anyv) == 0 && PyErr_Occurred() == NULL) score += 10000;\n"
        "    Py_XDECREF(anyv);\n"
        "    PyObject *out_arr = PyArray_SimpleNew(1, dims, NPY_BOOL);\n"
        "    if (out_arr == NULL) {\n"
        "        Py_DECREF(barr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    anyv = PyArray_Any((PyArrayObject *)arr, 0, (PyArrayObject *)out_arr);\n"
        "    if (anyv == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_XDECREF(anyv);\n"
        "    anyv = PyArray_Any((PyArrayObject *)arr, 1, NULL);\n"
        "    if (anyv == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000000; }\n"
        "    Py_XDECREF(anyv);\n"
        "    PyObject *objarr = PyArray_SimpleNew(1, dims, NPY_OBJECT);\n"
        "    if (objarr == NULL) {\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(barr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    anyv = PyArray_Any((PyArrayObject *)objarr, 0, NULL);\n"
        "    if (anyv == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000000; }\n"
        "    Py_XDECREF(anyv);\n"
        "    PyObject *carr = PyArray_SimpleNew(1, ddims, NPY_CDOUBLE);\n"
        "    if (carr == NULL) {\n"
        "        Py_DECREF(objarr);\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(barr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    anyv = PyArray_Any((PyArrayObject *)carr, 0, NULL);\n"
        "    if (anyv == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000000; }\n"
        "    Py_XDECREF(anyv);\n"
        "    npy_intp edims[1] = {0};\n"
        "    PyObject *empty = PyArray_SimpleNew(1, edims, NPY_INT);\n"
        "    if (empty == NULL) {\n"
        "        Py_DECREF(carr);\n"
        "        Py_DECREF(objarr);\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(barr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    anyv = PyArray_Any((PyArrayObject *)empty, 0, NULL);\n"
        "    if (anyv != NULL && PyObject_IsTrue(anyv) == 0 && PyErr_Occurred() == NULL) score += 1000000000;\n"
        "    Py_XDECREF(anyv);\n"
        "    Py_DECREF(empty);\n"
        "    Py_DECREF(carr);\n"
        "    Py_DECREF(objarr);\n"
        "    Py_DECREF(out_arr);\n"
        "    Py_DECREF(barr);\n"
        "    Py_DECREF(darr);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_all_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[1] = {3};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (arr == NULL) return NULL;\n"
        "    int *data = (int *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    data[0] = 1; data[1] = 2; data[2] = 3;\n"
        "    PyObject *allv = PyArray_All((PyArrayObject *)arr, 0, NULL);\n"
        "    if (allv != NULL && PyObject_IsTrue(allv) == 1) score += 1;\n"
        "    Py_XDECREF(allv);\n"
        "    allv = PyArray_All((PyArrayObject *)arr, -1, NULL);\n"
        "    if (allv != NULL && PyObject_IsTrue(allv) == 1) score += 10;\n"
        "    Py_XDECREF(allv);\n"
        "    data[1] = 0;\n"
        "    allv = PyArray_All((PyArrayObject *)arr, 0, NULL);\n"
        "    if (allv != NULL && PyObject_IsTrue(allv) == 0 && PyErr_Occurred() == NULL) score += 100;\n"
        "    Py_XDECREF(allv);\n"
        "    npy_intp ddims[1] = {2};\n"
        "    PyObject *darr = PyArray_SimpleNew(1, ddims, NPY_DOUBLE);\n"
        "    if (darr == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    double *ddata = (double *)PyArray_DATA((PyArrayObject *)darr);\n"
        "    ddata[0] = 1.0; ddata[1] = 0.25;\n"
        "    allv = PyArray_All((PyArrayObject *)darr, 0, NULL);\n"
        "    if (allv != NULL && PyObject_IsTrue(allv) == 1) score += 1000;\n"
        "    Py_XDECREF(allv);\n"
        "    npy_intp bdims[1] = {2};\n"
        "    PyObject *barr = PyArray_SimpleNew(1, bdims, NPY_BOOL);\n"
        "    if (barr == NULL) {\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    unsigned char *bdata = (unsigned char *)PyArray_DATA((PyArrayObject *)barr);\n"
        "    bdata[0] = 1; bdata[1] = 0;\n"
        "    allv = PyArray_All((PyArrayObject *)barr, 0, NULL);\n"
        "    if (allv != NULL && PyObject_IsTrue(allv) == 0 && PyErr_Occurred() == NULL) score += 10000;\n"
        "    Py_XDECREF(allv);\n"
        "    PyObject *out_arr = PyArray_SimpleNew(1, dims, NPY_BOOL);\n"
        "    if (out_arr == NULL) {\n"
        "        Py_DECREF(barr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    allv = PyArray_All((PyArrayObject *)arr, 0, (PyArrayObject *)out_arr);\n"
        "    if (allv == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_XDECREF(allv);\n"
        "    allv = PyArray_All((PyArrayObject *)arr, 1, NULL);\n"
        "    if (allv == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000000; }\n"
        "    Py_XDECREF(allv);\n"
        "    PyObject *objarr = PyArray_SimpleNew(1, dims, NPY_OBJECT);\n"
        "    if (objarr == NULL) {\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(barr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    allv = PyArray_All((PyArrayObject *)objarr, 0, NULL);\n"
        "    if (allv == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000000; }\n"
        "    Py_XDECREF(allv);\n"
        "    PyObject *carr = PyArray_SimpleNew(1, ddims, NPY_CDOUBLE);\n"
        "    if (carr == NULL) {\n"
        "        Py_DECREF(objarr);\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(barr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    allv = PyArray_All((PyArrayObject *)carr, 0, NULL);\n"
        "    if (allv == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000000; }\n"
        "    Py_XDECREF(allv);\n"
        "    npy_intp edims[1] = {0};\n"
        "    PyObject *empty = PyArray_SimpleNew(1, edims, NPY_INT);\n"
        "    if (empty == NULL) {\n"
        "        Py_DECREF(carr);\n"
        "        Py_DECREF(objarr);\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(barr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    allv = PyArray_All((PyArrayObject *)empty, 0, NULL);\n"
        "    if (allv != NULL && PyObject_IsTrue(allv) == 1) score += 1000000000;\n"
        "    Py_XDECREF(allv);\n"
        "    Py_DECREF(empty);\n"
        "    Py_DECREF(carr);\n"
        "    Py_DECREF(objarr);\n"
        "    Py_DECREF(out_arr);\n"
        "    Py_DECREF(barr);\n"
        "    Py_DECREF(darr);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_argmax_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[1] = {3};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (arr == NULL) return NULL;\n"
        "    int *data = (int *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    data[0] = -7; data[1] = 5; data[2] = 5;\n"
        "    PyObject *idx = PyArray_ArgMax((PyArrayObject *)arr, 0, NULL);\n"
        "    if (idx != NULL && PyArray_NDIM((PyArrayObject *)idx) == 0 && PyArray_TYPE((PyArrayObject *)idx) == NPY_LONG && *((long *)PyArray_DATA((PyArrayObject *)idx)) == 1) score += 1;\n"
        "    Py_XDECREF(idx);\n"
        "    idx = PyArray_ArgMax((PyArrayObject *)arr, -1, NULL);\n"
        "    if (idx != NULL && *((long *)PyArray_DATA((PyArrayObject *)idx)) == 1) score += 10;\n"
        "    Py_XDECREF(idx);\n"
        "    npy_intp ddims[1] = {2};\n"
        "    PyObject *darr = PyArray_SimpleNew(1, ddims, NPY_DOUBLE);\n"
        "    if (darr == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    double *ddata = (double *)PyArray_DATA((PyArrayObject *)darr);\n"
        "    ddata[0] = -1.5; ddata[1] = 2.0;\n"
        "    idx = PyArray_ArgMax((PyArrayObject *)darr, 0, NULL);\n"
        "    if (idx != NULL && *((long *)PyArray_DATA((PyArrayObject *)idx)) == 1) score += 100;\n"
        "    Py_XDECREF(idx);\n"
        "    PyObject *out_arr = PyArray_SimpleNew(1, dims, NPY_LONG);\n"
        "    if (out_arr == NULL) {\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    idx = PyArray_ArgMax((PyArrayObject *)arr, 0, (PyArrayObject *)out_arr);\n"
        "    if (idx == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000; }\n"
        "    Py_XDECREF(idx);\n"
        "    idx = PyArray_ArgMax((PyArrayObject *)arr, 1, NULL);\n"
        "    if (idx == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    Py_XDECREF(idx);\n"
        "    PyObject *objarr = PyArray_SimpleNew(1, dims, NPY_OBJECT);\n"
        "    if (objarr == NULL) {\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    idx = PyArray_ArgMax((PyArrayObject *)objarr, 0, NULL);\n"
        "    if (idx == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_XDECREF(idx);\n"
        "    PyObject *carr = PyArray_SimpleNew(1, ddims, NPY_CDOUBLE);\n"
        "    if (carr == NULL) {\n"
        "        Py_DECREF(objarr);\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    idx = PyArray_ArgMax((PyArrayObject *)carr, 0, NULL);\n"
        "    if (idx == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000000; }\n"
        "    Py_XDECREF(idx);\n"
        "    npy_intp edims[1] = {0};\n"
        "    PyObject *empty = PyArray_SimpleNew(1, edims, NPY_INT);\n"
        "    if (empty == NULL) {\n"
        "        Py_DECREF(carr);\n"
        "        Py_DECREF(objarr);\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    idx = PyArray_ArgMax((PyArrayObject *)empty, 0, NULL);\n"
        "    if (idx == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000000; }\n"
        "    Py_XDECREF(idx);\n"
        "    Py_DECREF(empty);\n"
        "    Py_DECREF(carr);\n"
        "    Py_DECREF(objarr);\n"
        "    Py_DECREF(out_arr);\n"
        "    Py_DECREF(darr);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_argmin_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[1] = {3};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (arr == NULL) return NULL;\n"
        "    int *data = (int *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    data[0] = -7; data[1] = -7; data[2] = 2;\n"
        "    PyObject *idx = PyArray_ArgMin((PyArrayObject *)arr, 0, NULL);\n"
        "    if (idx != NULL && PyArray_NDIM((PyArrayObject *)idx) == 0 && PyArray_TYPE((PyArrayObject *)idx) == NPY_LONG && *((long *)PyArray_DATA((PyArrayObject *)idx)) == 0) score += 1;\n"
        "    Py_XDECREF(idx);\n"
        "    idx = PyArray_ArgMin((PyArrayObject *)arr, -1, NULL);\n"
        "    if (idx != NULL && *((long *)PyArray_DATA((PyArrayObject *)idx)) == 0) score += 10;\n"
        "    Py_XDECREF(idx);\n"
        "    npy_intp ddims[1] = {2};\n"
        "    PyObject *darr = PyArray_SimpleNew(1, ddims, NPY_DOUBLE);\n"
        "    if (darr == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    double *ddata = (double *)PyArray_DATA((PyArrayObject *)darr);\n"
        "    ddata[0] = -1.5; ddata[1] = -2.0;\n"
        "    idx = PyArray_ArgMin((PyArrayObject *)darr, 0, NULL);\n"
        "    if (idx != NULL && *((long *)PyArray_DATA((PyArrayObject *)idx)) == 1) score += 100;\n"
        "    Py_XDECREF(idx);\n"
        "    PyObject *out_arr = PyArray_SimpleNew(1, dims, NPY_LONG);\n"
        "    if (out_arr == NULL) {\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    idx = PyArray_ArgMin((PyArrayObject *)arr, 0, (PyArrayObject *)out_arr);\n"
        "    if (idx == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000; }\n"
        "    Py_XDECREF(idx);\n"
        "    idx = PyArray_ArgMin((PyArrayObject *)arr, 1, NULL);\n"
        "    if (idx == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    Py_XDECREF(idx);\n"
        "    PyObject *objarr = PyArray_SimpleNew(1, dims, NPY_OBJECT);\n"
        "    if (objarr == NULL) {\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    idx = PyArray_ArgMin((PyArrayObject *)objarr, 0, NULL);\n"
        "    if (idx == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_XDECREF(idx);\n"
        "    PyObject *carr = PyArray_SimpleNew(1, ddims, NPY_CDOUBLE);\n"
        "    if (carr == NULL) {\n"
        "        Py_DECREF(objarr);\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    idx = PyArray_ArgMin((PyArrayObject *)carr, 0, NULL);\n"
        "    if (idx == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000000; }\n"
        "    Py_XDECREF(idx);\n"
        "    npy_intp edims[1] = {0};\n"
        "    PyObject *empty = PyArray_SimpleNew(1, edims, NPY_INT);\n"
        "    if (empty == NULL) {\n"
        "        Py_DECREF(carr);\n"
        "        Py_DECREF(objarr);\n"
        "        Py_DECREF(out_arr);\n"
        "        Py_DECREF(darr);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    idx = PyArray_ArgMin((PyArrayObject *)empty, 0, NULL);\n"
        "    if (idx == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000000; }\n"
        "    Py_XDECREF(idx);\n"
        "    Py_DECREF(empty);\n"
        "    Py_DECREF(carr);\n"
        "    Py_DECREF(objarr);\n"
        "    Py_DECREF(out_arr);\n"
        "    Py_DECREF(darr);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_reshape_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[2] = {2, 3};\n"
        "    PyObject *arr = PyArray_SimpleNew(2, dims, NPY_INT);\n"
        "    if (arr == NULL) return NULL;\n"
        "    int *data = (int *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    if (data == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    for (int i = 0; i < 6; i++) data[i] = i + 1;\n"
        "    PyObject *shape0 = PyLong_FromLong(3);\n"
        "    PyObject *shape1 = PyLong_FromLong(2);\n"
        "    PyObject *shape = NULL;\n"
        "    if (shape0 == NULL || shape1 == NULL) {\n"
        "        Py_XDECREF(shape0);\n"
        "        Py_XDECREF(shape1);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    shape = PyTuple_Pack(2, shape0, shape1);\n"
        "    Py_DECREF(shape0);\n"
        "    Py_DECREF(shape1);\n"
        "    if (shape == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *reshaped = PyArray_Reshape((PyArrayObject *)arr, shape);\n"
        "    if (reshaped != NULL) {\n"
        "        PyArrayObject *reshaped_arr = (PyArrayObject *)reshaped;\n"
        "        npy_intp *rdims = PyArray_DIMS(reshaped_arr);\n"
        "        int *rdata = (int *)PyArray_DATA(reshaped_arr);\n"
        "        if (PyArray_NDIM(reshaped_arr) == 2 && rdims != NULL && rdims[0] == 3 && rdims[1] == 2 && rdata == data && PyArray_BASE(reshaped_arr) == arr) score += 1;\n"
        "        if (rdata != NULL) {\n"
        "            rdata[3] = 44;\n"
        "            if (data[3] == 44) score += 10;\n"
        "        }\n"
        "    }\n"
        "    Py_XDECREF(reshaped);\n"
        "    PyObject *flat_shape = PyLong_FromLong(6);\n"
        "    if (flat_shape == NULL) {\n"
        "        Py_DECREF(shape);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    reshaped = PyArray_Reshape((PyArrayObject *)arr, flat_shape);\n"
        "    if (reshaped != NULL) {\n"
        "        npy_intp *rdims = PyArray_DIMS((PyArrayObject *)reshaped);\n"
        "        if (PyArray_NDIM((PyArrayObject *)reshaped) == 1 && rdims != NULL && rdims[0] == 6 && PyArray_DATA((PyArrayObject *)reshaped) == data) score += 100;\n"
        "    }\n"
        "    Py_XDECREF(reshaped);\n"
        "    PyObject *list_shape = PyList_New(2);\n"
        "    if (list_shape == NULL || PyList_SetItem(list_shape, 0, PyLong_FromLong(1)) != 0 || PyList_SetItem(list_shape, 1, PyLong_FromLong(6)) != 0) {\n"
        "        Py_XDECREF(list_shape);\n"
        "        Py_DECREF(flat_shape);\n"
        "        Py_DECREF(shape);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    reshaped = PyArray_Reshape((PyArrayObject *)arr, list_shape);\n"
        "    if (reshaped != NULL) {\n"
        "        npy_intp *rdims = PyArray_DIMS((PyArrayObject *)reshaped);\n"
        "        if (PyArray_NDIM((PyArrayObject *)reshaped) == 2 && rdims != NULL && rdims[0] == 1 && rdims[1] == 6 && PyArray_DATA((PyArrayObject *)reshaped) == data) score += 1000;\n"
        "    }\n"
        "    Py_XDECREF(reshaped);\n"
        "    PyObject *bad0 = PyLong_FromLong(4);\n"
        "    PyObject *bad1 = PyLong_FromLong(2);\n"
        "    PyObject *bad_shape = bad0 == NULL || bad1 == NULL ? NULL : PyTuple_Pack(2, bad0, bad1);\n"
        "    Py_XDECREF(bad0);\n"
        "    Py_XDECREF(bad1);\n"
        "    if (bad_shape == NULL) {\n"
        "        Py_DECREF(list_shape);\n"
        "        Py_DECREF(flat_shape);\n"
        "        Py_DECREF(shape);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    reshaped = PyArray_Reshape((PyArrayObject *)arr, bad_shape);\n"
        "    if (reshaped == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    Py_XDECREF(reshaped);\n"
        "    PyObject *neg0 = PyLong_FromLong(-1);\n"
        "    PyObject *neg1 = PyLong_FromLong(6);\n"
        "    PyObject *neg_shape = neg0 == NULL || neg1 == NULL ? NULL : PyTuple_Pack(2, neg0, neg1);\n"
        "    Py_XDECREF(neg0);\n"
        "    Py_XDECREF(neg1);\n"
        "    if (neg_shape == NULL) {\n"
        "        Py_DECREF(bad_shape);\n"
        "        Py_DECREF(list_shape);\n"
        "        Py_DECREF(flat_shape);\n"
        "        Py_DECREF(shape);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    reshaped = PyArray_Reshape((PyArrayObject *)arr, neg_shape);\n"
        "    if (reshaped == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_XDECREF(reshaped);\n"
        "    reshaped = PyArray_Reshape((PyArrayObject *)arr, Py_None);\n"
        "    if (reshaped == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000000; }\n"
        "    Py_XDECREF(reshaped);\n"
        "    npy_intp axes[2] = {1, 0};\n"
        "    PyArray_Dims permute = {axes, 2};\n"
        "    PyObject *transposed = PyArray_Transpose((PyArrayObject *)arr, &permute);\n"
        "    if (transposed == NULL) {\n"
        "        Py_DECREF(neg_shape);\n"
        "        Py_DECREF(bad_shape);\n"
        "        Py_DECREF(list_shape);\n"
        "        Py_DECREF(flat_shape);\n"
        "        Py_DECREF(shape);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    reshaped = PyArray_Reshape((PyArrayObject *)transposed, flat_shape);\n"
        "    if (reshaped == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000000; }\n"
        "    Py_XDECREF(reshaped);\n"
        "    reshaped = PyArray_Reshape((PyArrayObject *)Py_None, shape);\n"
        "    if (reshaped == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000000; }\n"
        "    Py_XDECREF(reshaped);\n"
        "    Py_DECREF(transposed);\n"
        "    Py_DECREF(neg_shape);\n"
        "    Py_DECREF(bad_shape);\n"
        "    Py_DECREF(list_shape);\n"
        "    Py_DECREF(flat_shape);\n"
        "    Py_DECREF(shape);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_newshape_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[2] = {2, 3};\n"
        "    PyObject *arr = PyArray_SimpleNew(2, dims, NPY_INT);\n"
        "    if (arr == NULL) return NULL;\n"
        "    int *data = (int *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    if (data == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    for (int i = 0; i < 6; i++) data[i] = i + 1;\n"
        "    npy_intp view_dims[2] = {3, 2};\n"
        "    PyArray_Dims newshape = {view_dims, 2};\n"
        "    PyObject *view = PyArray_Newshape((PyArrayObject *)arr, &newshape, NPY_CORDER);\n"
        "    if (view != NULL) {\n"
        "        PyArrayObject *view_arr = (PyArrayObject *)view;\n"
        "        npy_intp *rdims = PyArray_DIMS(view_arr);\n"
        "        int *rdata = (int *)PyArray_DATA(view_arr);\n"
        "        if (PyArray_NDIM(view_arr) == 2 && rdims != NULL && rdims[0] == 3 && rdims[1] == 2 && rdata == data && PyArray_BASE(view_arr) == arr) score += 1;\n"
        "        if (rdata != NULL) {\n"
        "            rdata[4] = 55;\n"
        "            if (data[4] == 55) score += 10;\n"
        "        }\n"
        "    }\n"
        "    Py_XDECREF(view);\n"
        "    view = PyArray_Newshape((PyArrayObject *)arr, &newshape, NPY_ANYORDER);\n"
        "    if (view != NULL && PyArray_DATA((PyArrayObject *)view) == data) score += 100;\n"
        "    Py_XDECREF(view);\n"
        "    view = PyArray_Newshape((PyArrayObject *)arr, &newshape, NPY_KEEPORDER);\n"
        "    if (view != NULL && PyArray_DATA((PyArrayObject *)view) == data) score += 1000;\n"
        "    Py_XDECREF(view);\n"
        "    npy_intp flat_dims[1] = {6};\n"
        "    PyArray_Dims flatshape = {flat_dims, 1};\n"
        "    PyObject *flat = PyArray_SimpleNew(1, flat_dims, NPY_INT);\n"
        "    if (flat == NULL) {\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    view = PyArray_Newshape((PyArrayObject *)flat, &flatshape, NPY_FORTRANORDER);\n"
        "    if (view != NULL && PyArray_NDIM((PyArrayObject *)view) == 1 && PyArray_DATA((PyArrayObject *)view) == PyArray_DATA((PyArrayObject *)flat)) score += 10000;\n"
        "    Py_XDECREF(view);\n"
        "    view = PyArray_Newshape((PyArrayObject *)arr, &newshape, NPY_FORTRANORDER);\n"
        "    if (view == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_XDECREF(view);\n"
        "    npy_intp bad_dims[2] = {4, 2};\n"
        "    PyArray_Dims badshape = {bad_dims, 2};\n"
        "    view = PyArray_Newshape((PyArrayObject *)arr, &badshape, NPY_CORDER);\n"
        "    if (view == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000000; }\n"
        "    Py_XDECREF(view);\n"
        "    npy_intp neg_dims[2] = {-1, 6};\n"
        "    PyArray_Dims negshape = {neg_dims, 2};\n"
        "    view = PyArray_Newshape((PyArrayObject *)arr, &negshape, NPY_CORDER);\n"
        "    if (view == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000000; }\n"
        "    Py_XDECREF(view);\n"
        "    PyArray_Dims invalid_ptr = {NULL, 1};\n"
        "    view = PyArray_Newshape((PyArrayObject *)arr, &invalid_ptr, NPY_CORDER);\n"
        "    if (view == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000000; }\n"
        "    Py_XDECREF(view);\n"
        "    view = PyArray_Newshape((PyArrayObject *)arr, NULL, NPY_CORDER);\n"
        "    if (view == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000000000; }\n"
        "    Py_XDECREF(view);\n"
        "    npy_intp axes[2] = {1, 0};\n"
        "    PyArray_Dims permute = {axes, 2};\n"
        "    PyObject *transposed = PyArray_Transpose((PyArrayObject *)arr, &permute);\n"
        "    if (transposed == NULL) {\n"
        "        Py_DECREF(flat);\n"
        "        Py_DECREF(arr);\n"
        "        return NULL;\n"
        "    }\n"
        "    view = PyArray_Newshape((PyArrayObject *)transposed, &newshape, NPY_CORDER);\n"
        "    if (view == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000000000; }\n"
        "    Py_XDECREF(view);\n"
        "    view = PyArray_Newshape((PyArrayObject *)Py_None, &newshape, NPY_CORDER);\n"
        "    if (view == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000000000; }\n"
        "    Py_XDECREF(view);\n"
        "    Py_DECREF(transposed);\n"
        "    Py_DECREF(flat);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_ensure_array_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    npy_intp dims[1] = {2};\n"
        "    PyObject *src_obj = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    PyObject *list = PyList_New(2);\n"
        "    if (src_obj == NULL || list == NULL) {\n"
        "        Py_XDECREF(src_obj);\n"
        "        Py_XDECREF(list);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyArrayObject *src = (PyArrayObject *)src_obj;\n"
        "    int *src_data = (int *)PyArray_DATA(src);\n"
        "    if (src_data == NULL) {\n"
        "        Py_DECREF(src_obj);\n"
        "        Py_DECREF(list);\n"
        "        return NULL;\n"
        "    }\n"
        "    src_data[0] = 31;\n"
        "    src_data[1] = 32;\n"
        "    if (PyList_SetItem(list, 0, PyLong_FromLong(41)) != 0) {\n"
        "        Py_DECREF(src_obj);\n"
        "        Py_DECREF(list);\n"
        "        return NULL;\n"
        "    }\n"
        "    if (PyList_SetItem(list, 1, PyLong_FromLong(42)) != 0) {\n"
        "        Py_DECREF(src_obj);\n"
        "        Py_DECREF(list);\n"
        "        return NULL;\n"
        "    }\n"
        "    Py_INCREF(src_obj);\n"
        "    PyObject *same_obj = PyArray_EnsureArray(src_obj);\n"
        "    PyObject *from_list = PyArray_EnsureArray(list);\n"
        "    Py_INCREF(src_obj);\n"
        "    PyObject *any_obj = PyArray_EnsureAnyArray(src_obj);\n"
        "    if (same_obj == NULL || from_list == NULL || any_obj == NULL) {\n"
        "        Py_XDECREF(same_obj);\n"
        "        Py_XDECREF(from_list);\n"
        "        Py_XDECREF(any_obj);\n"
        "        Py_DECREF(src_obj);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyArrayObject *list_arr = (PyArrayObject *)from_list;\n"
        "    long score = 0;\n"
        "    if (same_obj == src_obj && PyArray_CheckExact((PyArrayObject *)same_obj)) score += 1;\n"
        "    if (any_obj == src_obj && PyArray_Check((PyArrayObject *)any_obj)) score += 10;\n"
        "    if (from_list != list && PyArray_CheckExact(list_arr) && PyArray_SIZE(list_arr) == 2) score += 100;\n"
        "    PyObject *item = PyArray_ToScalar(PyArray_DATA(list_arr), list_arr);\n"
        "    if (item != NULL && PyLong_Check(item) && PyLong_AsLong(item) == 41) score += 1000;\n"
        "    Py_XDECREF(item);\n"
        "    Py_DECREF(same_obj);\n"
        "    Py_DECREF(from_list);\n"
        "    Py_DECREF(any_obj);\n"
        "    Py_DECREF(src_obj);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_descr_check_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    npy_intp dims[1] = {1};\n"
        "    PyArray_Descr *descr = PyArray_DescrFromType(NPY_INT);\n"
        "    PyObject *arr_obj = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (descr == NULL || arr_obj == NULL) {\n"
        "        Py_XDECREF(arr_obj);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyArrayObject *arr = (PyArrayObject *)arr_obj;\n"
        "    long score = 0;\n"
        "    if (PyArray_DescrCheck((PyObject *)descr)) score += 1;\n"
        "    if (PyArray_DescrCheck((PyObject *)PyArray_DESCR(arr))) score += 10;\n"
        "    if (!PyArray_DescrCheck(arr_obj)) score += 100;\n"
        "    if (!PyArray_DescrCheck(Py_None)) score += 1000;\n"
        "    Py_DECREF(arr_obj);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_descr_new_from_type_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    PyArray_Descr *descr = PyArray_DescrNewFromType(NPY_DOUBLE);\n"
        "    long score = 0;\n"
        "    if (descr != NULL && PyArray_DescrCheck((PyObject *)descr)) score += 1;\n"
        "    if (descr != NULL && PyDataType_TYPE(descr) == NPY_DOUBLE) score += 10;\n"
        "    if (descr != NULL && PyDataType_KIND(descr) == 'f') score += 100;\n"
        "    if (descr != NULL && PyDataType_ELSIZE(descr) == (int)sizeof(double)) score += 1000;\n"
        "    Py_XDECREF((PyObject *)descr);\n"
        "    PyArray_Descr *bad = PyArray_DescrNewFromType(9999);\n"
        "    if (bad == NULL && PyErr_ExceptionMatches(PyExc_NotImplementedError)) score += 10000;\n"
        "    Py_XDECREF((PyObject *)bad);\n"
        "    PyErr_Clear();\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_descr_new_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    PyArray_Descr *base = PyArray_DescrFromType(NPY_DOUBLE);\n"
        "    PyArray_Descr *copy = PyArray_DescrNew(base);\n"
        "    long score = 0;\n"
        "    if (copy != NULL && PyArray_DescrCheck((PyObject *)copy)) score += 1;\n"
        "    if (copy != NULL && PyDataType_TYPE(copy) == NPY_DOUBLE) score += 10;\n"
        "    if (copy != NULL && PyDataType_KIND(copy) == 'f') score += 100;\n"
        "    if (copy != NULL && PyDataType_ELSIZE(copy) == (int)sizeof(double)) score += 1000;\n"
        "    if (copy != NULL && copy != base) score += 10000;\n"
        "    Py_XDECREF((PyObject *)copy);\n"
        "    PyArray_Descr *bad = PyArray_DescrNew(NULL);\n"
        "    if (bad == NULL && PyErr_ExceptionMatches(PyExc_ValueError)) score += 100000;\n"
        "    Py_XDECREF((PyObject *)bad);\n"
        "    PyErr_Clear();\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_descr_new_byteorder_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    PyArray_Descr *base = PyArray_DescrFromType(NPY_INT);\n"
        "    PyArray_Descr *big = PyArray_DescrNewByteorder(base, NPY_BIG);\n"
        "    PyArray_Descr *little = PyArray_DescrNewByteorder(base, NPY_LITTLE);\n"
        "    PyArray_Descr *native = PyArray_DescrNewByteorder(base, NPY_NATBYTE);\n"
        "    long score = 0;\n"
        "    if (big != NULL && PyArray_DescrCheck((PyObject *)big)) score += 1;\n"
        "    if (big != NULL && PyDataType_TYPE(big) == NPY_INT) score += 10;\n"
        "    if (big != NULL && PyDataType_KIND(big) == 'i') score += 100;\n"
        "    if (big != NULL && PyDataType_ELSIZE(big) == (int)sizeof(int)) score += 1000;\n"
        "    if (big != NULL && big != base && big->byteorder == NPY_BIG) score += 10000;\n"
        "    if (little != NULL && little != base && little->byteorder == NPY_LITTLE) score += 100000;\n"
        "    if (native != NULL && native != base && native->byteorder == NPY_NATBYTE) score += 1000000;\n"
        "    Py_XDECREF((PyObject *)big);\n"
        "    Py_XDECREF((PyObject *)little);\n"
        "    Py_XDECREF((PyObject *)native);\n"
        "    PyArray_Descr *bad = PyArray_DescrNewByteorder(NULL, NPY_NATBYTE);\n"
        "    if (bad == NULL && PyErr_ExceptionMatches(PyExc_ValueError)) score += 10000000;\n"
        "    Py_XDECREF((PyObject *)bad);\n"
        "    PyErr_Clear();\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_can_cast_safely_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    if (PyArray_CanCastSafely(NPY_BOOL, NPY_INT)) score += 1;\n"
        "    if (PyArray_CanCastSafely(NPY_SHORT, NPY_LONGLONG)) score += 10;\n"
        "    if (PyArray_CanCastSafely(NPY_UBYTE, NPY_SHORT)) score += 100;\n"
        "    if (PyArray_CanCastSafely(NPY_UINT, NPY_LONGLONG)) score += 1000;\n"
        "    if (PyArray_CanCastSafely(NPY_INT, NPY_DOUBLE)) score += 10000;\n"
        "    if (!PyArray_CanCastSafely(NPY_DOUBLE, NPY_INT)) score += 100000;\n"
        "    if (!PyArray_CanCastSafely(NPY_DOUBLE, NPY_FLOAT)) score += 1000000;\n"
        "    if (!PyArray_CanCastSafely(NPY_INT, NPY_UINT)) score += 10000000;\n"
        "    if (!PyArray_CanCastSafely(9999, NPY_INT) && PyErr_Occurred() == NULL) score += 100000000;\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_can_cast_to_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    PyArray_Descr *bool_descr = PyArray_DescrFromType(NPY_BOOL);\n"
        "    PyArray_Descr *int_descr = PyArray_DescrFromType(NPY_INT);\n"
        "    PyArray_Descr *double_descr = PyArray_DescrFromType(NPY_DOUBLE);\n"
        "    if (bool_descr == NULL || int_descr == NULL || double_descr == NULL) return NULL;\n"
        "    long score = 0;\n"
        "    if (PyArray_CanCastTo(bool_descr, int_descr)) score += 1;\n"
        "    if (PyArray_CanCastTo(int_descr, double_descr)) score += 10;\n"
        "    if (!PyArray_CanCastTo(double_descr, int_descr)) score += 100;\n"
        "    if (!PyArray_CanCastTo(NULL, int_descr) && PyErr_Occurred() == NULL) score += 1000;\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_can_cast_type_array_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    PyArray_Descr *int_descr = PyArray_DescrFromType(NPY_INT);\n"
        "    PyArray_Descr *double_descr = PyArray_DescrFromType(NPY_DOUBLE);\n"
        "    npy_intp dims[1] = {2};\n"
        "    PyObject *int_arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    PyObject *double_arr = PyArray_SimpleNew(1, dims, NPY_DOUBLE);\n"
        "    if (int_descr == NULL || double_descr == NULL || int_arr == NULL || double_arr == NULL) return NULL;\n"
        "    long score = 0;\n"
        "    if (PyArray_CanCastTypeTo(int_descr, double_descr, NPY_SAFE_CASTING)) score += 1;\n"
        "    if (!PyArray_CanCastTypeTo(double_descr, int_descr, NPY_SAFE_CASTING)) score += 10;\n"
        "    if (PyArray_CanCastTypeTo(int_descr, int_descr, NPY_NO_CASTING)) score += 100;\n"
        "    if (!PyArray_CanCastTypeTo(int_descr, double_descr, NPY_NO_CASTING)) score += 1000;\n"
        "    if (PyArray_CanCastArrayTo((PyArrayObject *)int_arr, double_descr, NPY_SAFE_CASTING)) score += 10000;\n"
        "    if (!PyArray_CanCastArrayTo((PyArrayObject *)double_arr, int_descr, NPY_SAFE_CASTING)) score += 100000;\n"
        "    if (PyArray_CanCastArrayTo((PyArrayObject *)double_arr, int_descr, NPY_UNSAFE_CASTING)) score += 1000000;\n"
        "    Py_DECREF(int_arr);\n"
        "    Py_DECREF(double_arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_casting_converter_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    NPY_CASTING casting = NPY_UNSAFE_CASTING;\n"
        '    PyObject *no_obj = PyUnicode_FromString("no");\n'
        '    PyObject *equiv_obj = PyUnicode_FromString("equiv");\n'
        '    PyObject *safe_obj = PyUnicode_FromString("safe");\n'
        '    PyObject *same_kind_obj = PyUnicode_FromString("same_kind");\n'
        '    PyObject *unsafe_obj = PyUnicode_FromString("unsafe");\n'
        '    PyObject *safe_bytes = PyBytes_FromString("safe");\n'
        '    PyObject *same_value_obj = PyUnicode_FromString("same_value");\n'
        '    PyObject *bad_obj = PyUnicode_FromString("wild");\n'
        "    if (no_obj == NULL || equiv_obj == NULL || safe_obj == NULL || same_kind_obj == NULL ||\n"
        "        unsafe_obj == NULL || safe_bytes == NULL || same_value_obj == NULL || bad_obj == NULL) {\n"
        "        goto done;\n"
        "    }\n"
        "    if (PyArray_CastingConverter(no_obj, &casting) == NPY_SUCCEED && casting == NPY_NO_CASTING) score += 1;\n"
        "    if (PyArray_CastingConverter(equiv_obj, &casting) == NPY_SUCCEED && casting == NPY_EQUIV_CASTING) score += 10;\n"
        "    if (PyArray_CastingConverter(safe_obj, &casting) == NPY_SUCCEED && casting == NPY_SAFE_CASTING) score += 100;\n"
        "    if (PyArray_CastingConverter(same_kind_obj, &casting) == NPY_SUCCEED && casting == NPY_SAME_KIND_CASTING) score += 1000;\n"
        "    if (PyArray_CastingConverter(unsafe_obj, &casting) == NPY_SUCCEED && casting == NPY_UNSAFE_CASTING) score += 10000;\n"
        "    if (PyArray_CastingConverter(safe_bytes, &casting) == NPY_SUCCEED && casting == NPY_SAFE_CASTING) score += 100000;\n"
        "    if (PyArray_CastingConverter(same_value_obj, &casting) == NPY_FAIL && PyErr_ExceptionMatches(PyExc_ValueError)) score += 1000000;\n"
        "    PyErr_Clear();\n"
        "    if (PyArray_CastingConverter(bad_obj, &casting) == NPY_FAIL && PyErr_ExceptionMatches(PyExc_ValueError)) score += 10000000;\n"
        "    PyErr_Clear();\n"
        "    if (PyArray_CastingConverter(Py_None, &casting) == NPY_FAIL && PyErr_ExceptionMatches(PyExc_TypeError)) score += 100000000;\n"
        "    PyErr_Clear();\n"
        "done:\n"
        "    Py_XDECREF(no_obj);\n"
        "    Py_XDECREF(equiv_obj);\n"
        "    Py_XDECREF(safe_obj);\n"
        "    Py_XDECREF(same_kind_obj);\n"
        "    Py_XDECREF(unsafe_obj);\n"
        "    Py_XDECREF(safe_bytes);\n"
        "    Py_XDECREF(same_value_obj);\n"
        "    Py_XDECREF(bad_obj);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_zero_one_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    npy_intp dims[1] = {1};\n"
        "    PyObject *int_arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    PyObject *double_arr = PyArray_SimpleNew(1, dims, NPY_DOUBLE);\n"
        "    PyObject *bool_arr = PyArray_SimpleNew(1, dims, NPY_BOOL);\n"
        "    if (int_arr == NULL || double_arr == NULL || bool_arr == NULL) return NULL;\n"
        "    char *int_zero = PyArray_Zero((PyArrayObject *)int_arr);\n"
        "    char *int_one = PyArray_One((PyArrayObject *)int_arr);\n"
        "    char *double_zero = PyArray_Zero((PyArrayObject *)double_arr);\n"
        "    char *double_one = PyArray_One((PyArrayObject *)double_arr);\n"
        "    char *bool_one = PyArray_One((PyArrayObject *)bool_arr);\n"
        "    long score = 0;\n"
        "    if (int_zero != NULL && *(int *)int_zero == 0) score += 1;\n"
        "    if (int_one != NULL && *(int *)int_one == 1) score += 10;\n"
        "    if (double_zero != NULL && *(double *)double_zero == 0.0) score += 100;\n"
        "    if (double_one != NULL && *(double *)double_one == 1.0) score += 1000;\n"
        "    PyObject *scalar = int_one != NULL ? PyArray_ToScalar(int_one, (PyArrayObject *)int_arr) : NULL;\n"
        "    if (scalar != NULL && PyLong_AsLong(scalar) == 1) score += 10000;\n"
        "    Py_XDECREF(scalar);\n"
        "    if (bool_one != NULL && *(unsigned char *)bool_one == 1) score += 100000;\n"
        "    PyArray_free(int_zero);\n"
        "    PyArray_free(int_one);\n"
        "    PyArray_free(double_zero);\n"
        "    PyArray_free(double_one);\n"
        "    PyArray_free(bool_one);\n"
        "    Py_DECREF(bool_arr);\n"
        "    Py_DECREF(double_arr);\n"
        "    Py_DECREF(int_arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_object_type_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    npy_intp dims[1] = {2};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    PyObject *ival = PyLong_FromLong(7);\n"
        "    PyObject *fval = PyFloat_FromDouble(1.5);\n"
        "    PyObject *cval = PyComplex_FromDoubles(1.0, 2.0);\n"
        '    PyObject *sval = PyUnicode_FromString("abc");\n'
        "    PyObject *seq = PyList_New(2);\n"
        "    PyObject *seq0 = PyLong_FromLong(1);\n"
        "    PyObject *seq1 = PyFloat_FromDouble(2.5);\n"
        "    if (seq != NULL && seq0 != NULL && seq1 != NULL) {\n"
        "        if (PyList_SetItem(seq, 0, seq0) == 0) seq0 = NULL;\n"
        "        if (PyList_SetItem(seq, 1, seq1) == 0) seq1 = NULL;\n"
        "    }\n"
        "    long score = 0;\n"
        "    if (ival != NULL && PyArray_ObjectType(ival, NPY_NOTYPE) == NPY_LONG) score += 1;\n"
        "    if (fval != NULL && PyArray_ObjectType(fval, NPY_NOTYPE) == NPY_DOUBLE) score += 10;\n"
        "    if (cval != NULL && PyArray_ObjectType(cval, NPY_NOTYPE) == NPY_CDOUBLE) score += 100;\n"
        "    if (sval != NULL && PyArray_ObjectType(sval, NPY_NOTYPE) == NPY_STRING) score += 1000;\n"
        "    if (arr != NULL && PyArray_ObjectType(arr, NPY_NOTYPE) == NPY_INT) score += 10000;\n"
        "    if (arr != NULL && PyArray_ObjectType(arr, NPY_DOUBLE) == NPY_DOUBLE) score += 100000;\n"
        "    if (ival != NULL && PyArray_ObjectType(ival, NPY_DOUBLE) == NPY_DOUBLE) score += 1000000;\n"
        "    if (seq != NULL && PyArray_ObjectType(seq, NPY_NOTYPE) == NPY_DOUBLE) score += 10000000;\n"
        "    if (PyArray_ObjectType(Py_None, NPY_NOTYPE) == NPY_OBJECT) score += 100000000;\n"
        "    Py_XDECREF(arr);\n"
        "    Py_XDECREF(ival);\n"
        "    Py_XDECREF(fval);\n"
        "    Py_XDECREF(cval);\n"
        "    Py_XDECREF(sval);\n"
        "    Py_XDECREF(seq);\n"
        "    Py_XDECREF(seq0);\n"
        "    Py_XDECREF(seq1);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_descr_from_object_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    npy_intp dims[1] = {1};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    PyObject *fval = PyFloat_FromDouble(1.5);\n"
        "    PyObject *ival = PyLong_FromLong(7);\n"
        "    PyObject *seq = PyList_New(2);\n"
        "    PyObject *seq0 = PyLong_FromLong(1);\n"
        "    PyObject *seq1 = PyFloat_FromDouble(2.5);\n"
        "    if (seq != NULL && seq0 != NULL && seq1 != NULL) {\n"
        "        if (PyList_SetItem(seq, 0, seq0) == 0) seq0 = NULL;\n"
        "        if (PyList_SetItem(seq, 1, seq1) == 0) seq1 = NULL;\n"
        "    }\n"
        "    PyArray_Descr *min_double = PyArray_DescrFromType(NPY_DOUBLE);\n"
        "    PyArray_Descr *float_descr = fval != NULL ? PyArray_DescrFromObject(fval, NULL) : NULL;\n"
        "    PyArray_Descr *int_min_descr = (ival != NULL && min_double != NULL) ? PyArray_DescrFromObject(ival, min_double) : NULL;\n"
        "    PyArray_Descr *array_descr = arr != NULL ? PyArray_DescrFromObject(arr, NULL) : NULL;\n"
        "    PyArray_Descr *seq_descr = seq != NULL ? PyArray_DescrFromObject(seq, NULL) : NULL;\n"
        "    PyArray_Descr *none_descr = PyArray_DescrFromObject(Py_None, NULL);\n"
        "    long score = 0;\n"
        "    if (float_descr != NULL && PyArray_DescrCheck((PyObject *)float_descr) && PyDataType_TYPE(float_descr) == NPY_DOUBLE) score += 1;\n"
        "    if (int_min_descr != NULL && PyDataType_TYPE(int_min_descr) == NPY_DOUBLE) score += 10;\n"
        "    if (array_descr != NULL && PyDataType_TYPE(array_descr) == NPY_INT) score += 100;\n"
        "    if (seq_descr != NULL && PyDataType_TYPE(seq_descr) == NPY_DOUBLE) score += 1000;\n"
        "    if (none_descr != NULL && PyDataType_TYPE(none_descr) == NPY_OBJECT) score += 10000;\n"
        "    PyArray_Descr *bad = PyArray_DescrFromObject(NULL, NULL);\n"
        "    if (bad == NULL && PyErr_ExceptionMatches(PyExc_ValueError)) score += 100000;\n"
        "    Py_XDECREF((PyObject *)bad);\n"
        "    PyErr_Clear();\n"
        "    Py_XDECREF((PyObject *)none_descr);\n"
        "    Py_XDECREF((PyObject *)seq_descr);\n"
        "    Py_XDECREF((PyObject *)array_descr);\n"
        "    Py_XDECREF((PyObject *)int_min_descr);\n"
        "    Py_XDECREF((PyObject *)float_descr);\n"
        "    Py_XDECREF((PyObject *)min_double);\n"
        "    Py_XDECREF(arr);\n"
        "    Py_XDECREF(fval);\n"
        "    Py_XDECREF(ival);\n"
        "    Py_XDECREF(seq);\n"
        "    Py_XDECREF(seq0);\n"
        "    Py_XDECREF(seq1);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_descr_converter_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    PyObject *fval = PyFloat_FromDouble(1.5);\n"
        "    PyObject *ival = PyLong_FromLong(7);\n"
        "    npy_intp dims[1] = {1};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (fval == NULL || ival == NULL || arr == NULL) return NULL;\n"
        "    PyArray_Descr *descr = NULL;\n"
        "    if (PyArray_DescrConverter(fval, &descr) == NPY_SUCCEED && descr != NULL && PyDataType_TYPE(descr) == NPY_DOUBLE) score += 1;\n"
        "    Py_XDECREF((PyObject *)descr);\n"
        "    descr = NULL;\n"
        "    if (PyArray_DescrConverter2(Py_None, &descr) == NPY_SUCCEED && descr == NULL) score += 10;\n"
        "    Py_XDECREF((PyObject *)descr);\n"
        "    descr = NULL;\n"
        "    if (PyArray_DescrConverter2(ival, &descr) == NPY_SUCCEED && descr != NULL && PyDataType_TYPE(descr) == NPY_LONG) score += 100;\n"
        "    Py_XDECREF((PyObject *)descr);\n"
        "    descr = NULL;\n"
        "    if (PyArray_DescrConverter(arr, &descr) == NPY_SUCCEED && descr != NULL && PyDataType_TYPE(descr) == NPY_INT) score += 1000;\n"
        "    Py_XDECREF((PyObject *)descr);\n"
        "    descr = NULL;\n"
        "    if (PyArray_DescrConverter(NULL, &descr) == NPY_FAIL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    Py_XDECREF((PyObject *)descr);\n"
        "    if (PyArray_DescrConverter(fval, NULL) == NPY_FAIL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_DECREF(arr);\n"
        "    Py_DECREF(ival);\n"
        "    Py_DECREF(fval);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_descr_align_converter_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    PyObject *fval = PyFloat_FromDouble(1.5);\n"
        "    PyObject *ival = PyLong_FromLong(7);\n"
        "    npy_intp dims[1] = {1};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        "    if (fval == NULL || ival == NULL || arr == NULL) return NULL;\n"
        "    PyArray_Descr *descr = NULL;\n"
        "    if (PyArray_DescrAlignConverter(fval, &descr) == NPY_SUCCEED && descr != NULL &&\n"
        "        PyDataType_TYPE(descr) == NPY_DOUBLE && PyDataType_ALIGNMENT(descr) >= (int)sizeof(double)) score += 1;\n"
        "    Py_XDECREF((PyObject *)descr);\n"
        "    descr = NULL;\n"
        "    if (PyArray_DescrAlignConverter2(Py_None, &descr) == NPY_SUCCEED && descr == NULL) score += 10;\n"
        "    Py_XDECREF((PyObject *)descr);\n"
        "    descr = NULL;\n"
        "    if (PyArray_DescrAlignConverter2(ival, &descr) == NPY_SUCCEED && descr != NULL &&\n"
        "        PyDataType_TYPE(descr) == NPY_LONG && PyDataType_ALIGNMENT(descr) >= (int)sizeof(long)) score += 100;\n"
        "    Py_XDECREF((PyObject *)descr);\n"
        "    descr = NULL;\n"
        "    if (PyArray_DescrAlignConverter(arr, &descr) == NPY_SUCCEED && descr != NULL &&\n"
        "        PyDataType_TYPE(descr) == NPY_INT && PyDataType_ALIGNMENT(descr) >= (int)sizeof(int)) score += 1000;\n"
        "    Py_XDECREF((PyObject *)descr);\n"
        "    descr = NULL;\n"
        "    if (PyArray_DescrAlignConverter(NULL, &descr) == NPY_FAIL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    Py_XDECREF((PyObject *)descr);\n"
        "    Py_DECREF(arr);\n"
        "    Py_DECREF(ival);\n"
        "    Py_DECREF(fval);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_descr_from_scalar_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    PyObject *ival = PyLong_FromLong(7);\n"
        "    PyObject *fval = PyFloat_FromDouble(1.5);\n"
        "    PyObject *cval = PyComplex_FromDoubles(1.0, 2.0);\n"
        '    PyObject *sval = PyUnicode_FromString("abc");\n'
        "    if (ival == NULL || fval == NULL || cval == NULL || sval == NULL) return NULL;\n"
        "    PyArray_Descr *int_descr = PyArray_DescrFromScalar(ival);\n"
        "    PyArray_Descr *float_descr = PyArray_DescrFromScalar(fval);\n"
        "    PyArray_Descr *complex_descr = PyArray_DescrFromScalar(cval);\n"
        "    PyArray_Descr *string_descr = PyArray_DescrFromScalar(sval);\n"
        "    PyArray_Descr *object_descr = PyArray_DescrFromScalar(Py_None);\n"
        "    long score = 0;\n"
        "    if (int_descr != NULL && PyArray_DescrCheck((PyObject *)int_descr) && PyDataType_TYPE(int_descr) == NPY_LONG) score += 1;\n"
        "    if (float_descr != NULL && PyDataType_TYPE(float_descr) == NPY_DOUBLE) score += 10;\n"
        "    if (complex_descr != NULL && PyDataType_TYPE(complex_descr) == NPY_CDOUBLE) score += 100;\n"
        "    if (string_descr != NULL && PyDataType_TYPE(string_descr) == NPY_STRING) score += 1000;\n"
        "    if (object_descr != NULL && PyDataType_TYPE(object_descr) == NPY_OBJECT) score += 10000;\n"
        "    Py_XDECREF((PyObject *)object_descr);\n"
        "    Py_XDECREF((PyObject *)string_descr);\n"
        "    Py_XDECREF((PyObject *)complex_descr);\n"
        "    Py_XDECREF((PyObject *)float_descr);\n"
        "    Py_XDECREF((PyObject *)int_descr);\n"
        "    Py_DECREF(sval);\n"
        "    Py_DECREF(cval);\n"
        "    Py_DECREF(fval);\n"
        "    Py_DECREF(ival);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_descr_from_type_object_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    PyArray_Descr *bool_descr = PyArray_DescrFromTypeObject((PyObject *)&PyBool_Type);\n"
        "    PyArray_Descr *int_descr = PyArray_DescrFromTypeObject((PyObject *)&PyLong_Type);\n"
        "    PyArray_Descr *float_descr = PyArray_DescrFromTypeObject((PyObject *)&PyFloat_Type);\n"
        "    PyArray_Descr *complex_descr = PyArray_DescrFromTypeObject((PyObject *)&PyComplex_Type);\n"
        "    PyArray_Descr *string_descr = PyArray_DescrFromTypeObject((PyObject *)&PyBytes_Type);\n"
        "    PyArray_Descr *object_descr = PyArray_DescrFromTypeObject((PyObject *)&PyBaseObject_Type);\n"
        "    long score = 0;\n"
        "    if (bool_descr != NULL && PyArray_DescrCheck((PyObject *)bool_descr) && PyDataType_TYPE(bool_descr) == NPY_BOOL) score += 1;\n"
        "    if (int_descr != NULL && PyDataType_TYPE(int_descr) == NPY_LONG) score += 10;\n"
        "    if (float_descr != NULL && PyDataType_TYPE(float_descr) == NPY_DOUBLE) score += 100;\n"
        "    if (complex_descr != NULL && PyDataType_TYPE(complex_descr) == NPY_CDOUBLE) score += 1000;\n"
        "    if (string_descr != NULL && PyDataType_TYPE(string_descr) == NPY_STRING) score += 10000;\n"
        "    if (object_descr != NULL && PyDataType_TYPE(object_descr) == NPY_OBJECT) score += 100000;\n"
        "    Py_XDECREF((PyObject *)object_descr);\n"
        "    Py_XDECREF((PyObject *)string_descr);\n"
        "    Py_XDECREF((PyObject *)complex_descr);\n"
        "    Py_XDECREF((PyObject *)float_descr);\n"
        "    Py_XDECREF((PyObject *)int_descr);\n"
        "    Py_XDECREF((PyObject *)bool_descr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_scalar_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    PyArray_Descr *int_descr = PyArray_DescrFromType(NPY_INT);\n"
        "    PyArray_Descr *double_descr = PyArray_DescrFromType(NPY_DOUBLE);\n"
        "    PyArray_Descr *complex_descr = PyArray_DescrFromType(NPY_CDOUBLE);\n"
        "    PyArray_Descr *object_descr = PyArray_DescrFromType(NPY_OBJECT);\n"
        "    int iv = -7;\n"
        "    double dv = 2.5;\n"
        "    npy_cdouble cv;\n"
        "    cv.real = 2.0;\n"
        "    cv.imag = 3.0;\n"
        "    PyObject *obj = PyLong_FromLong(42);\n"
        "    if (obj == NULL) return NULL;\n"
        "    PyObject *obj_slot = obj;\n"
        "    PyObject *is = int_descr != NULL ? PyArray_Scalar(&iv, int_descr, NULL) : NULL;\n"
        "    PyObject *ds = double_descr != NULL ? PyArray_Scalar(&dv, double_descr, NULL) : NULL;\n"
        "    PyObject *cs = complex_descr != NULL ? PyArray_Scalar(&cv, complex_descr, NULL) : NULL;\n"
        "    PyObject *os = object_descr != NULL ? PyArray_Scalar(&obj_slot, object_descr, NULL) : NULL;\n"
        "    long score = 0;\n"
        "    if (is != NULL && PyLong_AsLong(is) == -7) score += 1;\n"
        "    if (ds != NULL && PyFloat_AsDouble(ds) > 2.49 && PyFloat_AsDouble(ds) < 2.51) score += 10;\n"
        "    if (cs != NULL) {\n"
        "        Py_complex got = PyComplex_AsCComplex(cs);\n"
        "        if (got.real > 1.99 && got.real < 2.01 && got.imag > 2.99 && got.imag < 3.01) score += 100;\n"
        "    }\n"
        "    if (os != NULL && PyLong_AsLong(os) == 42) score += 1000;\n"
        "    if (int_descr != NULL && PyArray_Scalar(NULL, int_descr, NULL) == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    if (PyArray_Scalar(&iv, NULL, NULL) == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_XDECREF(os);\n"
        "    Py_XDECREF(cs);\n"
        "    Py_XDECREF(ds);\n"
        "    Py_XDECREF(is);\n"
        "    Py_DECREF(obj);\n"
        "    Py_XDECREF((PyObject *)object_descr);\n"
        "    Py_XDECREF((PyObject *)complex_descr);\n"
        "    Py_XDECREF((PyObject *)double_descr);\n"
        "    Py_XDECREF((PyObject *)int_descr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_scalar_as_ctype_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    PyObject *ival = PyLong_FromLong(-7);\n"
        "    PyObject *fval = PyFloat_FromDouble(1.5);\n"
        "    PyObject *cval = PyComplex_FromDoubles(2.0, 3.0);\n"
        '    PyObject *sval = PyBytes_FromString("xy");\n'
        "    if (ival == NULL || fval == NULL || cval == NULL || sval == NULL) return NULL;\n"
        "    long out_i = 0;\n"
        "    double out_d = 0.0;\n"
        "    npy_cdouble out_c;\n"
        "    char *out_s = NULL;\n"
        "    out_c.real = 0.0;\n"
        "    out_c.imag = 0.0;\n"
        "    PyArray_ScalarAsCtype(ival, &out_i);\n"
        "    PyArray_ScalarAsCtype(fval, &out_d);\n"
        "    PyArray_ScalarAsCtype(cval, &out_c);\n"
        "    PyArray_ScalarAsCtype(sval, &out_s);\n"
        "    long score = 0;\n"
        "    if (out_i == -7) score += 1;\n"
        "    if (out_d > 1.49 && out_d < 1.51) score += 10;\n"
        "    if (out_c.real > 1.99 && out_c.real < 2.01 && out_c.imag > 2.99 && out_c.imag < 3.01) score += 100;\n"
        "    if (out_s != NULL && out_s[0] == 'x' && out_s[1] == 'y') score += 1000;\n"
        "    Py_DECREF(sval);\n"
        "    Py_DECREF(cval);\n"
        "    Py_DECREF(fval);\n"
        "    Py_DECREF(ival);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_from_scalar_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    PyObject *ival = PyLong_FromLong(11);\n"
        "    PyObject *fval = PyFloat_FromDouble(2.5);\n"
        "    PyObject *int_arr_obj = NULL;\n"
        "    PyObject *double_arr_obj = NULL;\n"
        "    PyObject *int_item = NULL;\n"
        "    PyObject *double_item = NULL;\n"
        "    if (ival == NULL || fval == NULL) return NULL;\n"
        "    int_arr_obj = PyArray_FromScalar(ival, NULL);\n"
        "    double_arr_obj = PyArray_FromScalar(fval, PyArray_DescrFromType(NPY_DOUBLE));\n"
        "    if (int_arr_obj != NULL) int_item = PyArray_GETITEM((PyArrayObject *)int_arr_obj, PyArray_DATA((PyArrayObject *)int_arr_obj));\n"
        "    if (double_arr_obj != NULL) double_item = PyArray_GETITEM((PyArrayObject *)double_arr_obj, PyArray_DATA((PyArrayObject *)double_arr_obj));\n"
        "    long score = 0;\n"
        "    if (int_arr_obj != NULL && PyArray_NDIM((PyArrayObject *)int_arr_obj) == 0 && PyArray_SIZE((PyArrayObject *)int_arr_obj) == 1) score += 1;\n"
        "    if (int_arr_obj != NULL && PyArray_TYPE((PyArrayObject *)int_arr_obj) == NPY_LONG) score += 10;\n"
        "    if (int_item != NULL && PyLong_AsLong(int_item) == 11) score += 100;\n"
        "    if (double_arr_obj != NULL && PyArray_NDIM((PyArrayObject *)double_arr_obj) == 0 && PyArray_TYPE((PyArrayObject *)double_arr_obj) == NPY_DOUBLE) score += 1000;\n"
        "    if (double_item != NULL && PyFloat_AsDouble(double_item) > 2.49 && PyFloat_AsDouble(double_item) < 2.51) score += 10000;\n"
        "    Py_XDECREF(double_item);\n"
        "    Py_XDECREF(int_item);\n"
        "    Py_XDECREF(double_arr_obj);\n"
        "    Py_XDECREF(int_arr_obj);\n"
        "    Py_DECREF(fval);\n"
        "    Py_DECREF(ival);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_cast_scalar_to_ctype_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    PyObject *ival = PyLong_FromLong(7);\n"
        "    PyObject *fval = PyFloat_FromDouble(3.5);\n"
        "    PyObject *cval = PyComplex_FromDoubles(4.0, 5.0);\n"
        "    if (ival == NULL || fval == NULL || cval == NULL) return NULL;\n"
        "    long out_l = 0;\n"
        "    double out_d_from_int = 0.0;\n"
        "    double out_d = 0.0;\n"
        "    npy_cdouble out_c;\n"
        "    out_c.real = 0.0;\n"
        "    out_c.imag = 0.0;\n"
        "    PyArray_Descr *long_descr = PyArray_DescrFromType(NPY_LONG);\n"
        "    PyArray_Descr *double_descr_a = PyArray_DescrFromType(NPY_DOUBLE);\n"
        "    PyArray_Descr *double_descr_b = PyArray_DescrFromType(NPY_DOUBLE);\n"
        "    PyArray_Descr *complex_descr = PyArray_DescrFromType(NPY_CDOUBLE);\n"
        "    int rc_l = long_descr != NULL ? PyArray_CastScalarToCtype(ival, &out_l, long_descr) : -1;\n"
        "    int rc_di = double_descr_a != NULL ? PyArray_CastScalarToCtype(ival, &out_d_from_int, double_descr_a) : -1;\n"
        "    int rc_d = double_descr_b != NULL ? PyArray_CastScalarToCtype(fval, &out_d, double_descr_b) : -1;\n"
        "    int rc_c = complex_descr != NULL ? PyArray_CastScalarToCtype(cval, &out_c, complex_descr) : -1;\n"
        "    long score = 0;\n"
        "    if (rc_l == 0 && out_l == 7) score += 1;\n"
        "    if (rc_di == 0 && out_d_from_int > 6.99 && out_d_from_int < 7.01) score += 10;\n"
        "    if (rc_d == 0 && out_d > 3.49 && out_d < 3.51) score += 100;\n"
        "    if (rc_c == 0 && out_c.real > 3.99 && out_c.real < 4.01 && out_c.imag > 4.99 && out_c.imag < 5.01) score += 1000;\n"
        "    Py_XDECREF((PyObject *)complex_descr);\n"
        "    Py_XDECREF((PyObject *)double_descr_b);\n"
        "    Py_XDECREF((PyObject *)double_descr_a);\n"
        "    Py_XDECREF((PyObject *)long_descr);\n"
        "    Py_DECREF(cval);\n"
        "    Py_DECREF(fval);\n"
        "    Py_DECREF(ival);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_cast_scalar_direct_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    PyObject *ival = PyLong_FromLong(13);\n"
        "    PyObject *fval = PyFloat_FromDouble(6.25);\n"
        "    PyObject *cval = PyComplex_FromDoubles(2.0, -3.0);\n"
        "    if (ival == NULL || fval == NULL || cval == NULL) return NULL;\n"
        "    long out_l = 0;\n"
        "    double out_d_from_int = 0.0;\n"
        "    double out_d = 0.0;\n"
        "    npy_cdouble out_c;\n"
        "    out_c.real = 0.0;\n"
        "    out_c.imag = 0.0;\n"
        "    PyArray_Descr *long_descr = PyArray_DescrFromType(NPY_LONG);\n"
        "    PyArray_Descr *double_descr = PyArray_DescrFromType(NPY_DOUBLE);\n"
        "    PyArray_Descr *complex_descr = PyArray_DescrFromType(NPY_CDOUBLE);\n"
        "    int rc_l = long_descr != NULL ? PyArray_CastScalarDirect(ival, long_descr, &out_l, NPY_LONG) : -1;\n"
        "    int rc_di = long_descr != NULL ? PyArray_CastScalarDirect(ival, long_descr, &out_d_from_int, NPY_DOUBLE) : -1;\n"
        "    int rc_d = double_descr != NULL ? PyArray_CastScalarDirect(fval, double_descr, &out_d, NPY_DOUBLE) : -1;\n"
        "    int rc_c = complex_descr != NULL ? PyArray_CastScalarDirect(cval, complex_descr, &out_c, NPY_CDOUBLE) : -1;\n"
        "    long score = 0;\n"
        "    if (rc_l == 0 && out_l == 13) score += 1;\n"
        "    if (rc_di == 0 && out_d_from_int > 12.99 && out_d_from_int < 13.01) score += 10;\n"
        "    if (rc_d == 0 && out_d > 6.24 && out_d < 6.26) score += 100;\n"
        "    if (rc_c == 0 && out_c.real > 1.99 && out_c.real < 2.01 && out_c.imag < -2.99 && out_c.imag > -3.01) score += 1000;\n"
        "    Py_XDECREF((PyObject *)complex_descr);\n"
        "    Py_XDECREF((PyObject *)double_descr);\n"
        "    Py_XDECREF((PyObject *)long_descr);\n"
        "    Py_DECREF(cval);\n"
        "    Py_DECREF(fval);\n"
        "    Py_DECREF(ival);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_pack_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    PyArray_Descr *long_descr = PyArray_DescrFromType(NPY_LONG);\n"
        "    PyArray_Descr *double_descr = PyArray_DescrFromType(NPY_DOUBLE);\n"
        "    PyArray_Descr *object_descr = PyArray_DescrFromType(NPY_OBJECT);\n"
        "    PyObject *ival = PyLong_FromLong(9);\n"
        "    PyObject *jval = PyLong_FromLong(5);\n"
        '    PyObject *text = PyUnicode_FromString("packed");\n'
        "    PyObject *seed = PyLong_FromLong(23);\n"
        "    if (long_descr == NULL || double_descr == NULL || object_descr == NULL || ival == NULL || jval == NULL || text == NULL || seed == NULL) return NULL;\n"
        "    long out_l = 0;\n"
        "    double out_d = 0.0;\n"
        "    PyObject *slot = NULL;\n"
        "    PyObject *scalar_arr = PyArray_FromScalar(seed, NULL);\n"
        "    Py_DECREF(seed);\n"
        "    long out_from_array = 0;\n"
        "    int rc_l = PyArray_Pack(long_descr, &out_l, ival);\n"
        "    int rc_d = PyArray_Pack(double_descr, &out_d, jval);\n"
        "    int rc_obj = PyArray_Pack(object_descr, &slot, text);\n"
        "    Py_DECREF(text);\n"
        "    int rc_arr = scalar_arr != NULL ? PyArray_Pack(long_descr, &out_from_array, scalar_arr) : -1;\n"
        "    long score = 0;\n"
        "    if (rc_l == 0 && out_l == 9) score += 1;\n"
        "    if (rc_d == 0 && out_d > 4.99 && out_d < 5.01) score += 10;\n"
        '    if (rc_obj == 0 && slot != NULL && strcmp(PyUnicode_AsUTF8(slot), "packed") == 0) score += 100;\n'
        "    if (rc_arr == 0 && out_from_array == 23) score += 1000;\n"
        "    Py_XDECREF(scalar_arr);\n"
        "    Py_XDECREF(slot);\n"
        "    Py_DECREF(jval);\n"
        "    Py_DECREF(ival);\n"
        "    Py_XDECREF((PyObject *)object_descr);\n"
        "    Py_XDECREF((PyObject *)double_descr);\n"
        "    Py_XDECREF((PyObject *)long_descr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_from_array_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    npy_intp dims[1] = {3};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_LONG);\n"
        "    if (arr == NULL) return NULL;\n"
        "    long *data = (long *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    data[0] = 7; data[1] = 8; data[2] = 9;\n"
        "    long score = 0;\n"
        "    PyObject *same = PyArray_FromArray((PyArrayObject *)arr, NULL, 0);\n"
        "    if (same == arr && PyArray_TYPE((PyArrayObject *)same) == NPY_LONG) score += 1;\n"
        "    Py_XDECREF(same);\n"
        "    PyObject *copy = PyArray_FromArray((PyArrayObject *)arr, NULL, NPY_ARRAY_ENSURECOPY);\n"
        "    if (copy != NULL && copy != arr && PyArray_TYPE((PyArrayObject *)copy) == NPY_LONG && ((long *)PyArray_DATA((PyArrayObject *)copy))[1] == 8) score += 10;\n"
        "    Py_XDECREF(copy);\n"
        "    PyObject *cast = PyArray_FromArray((PyArrayObject *)arr, PyArray_DescrFromType(NPY_DOUBLE), 0);\n"
        "    if (cast != NULL && PyArray_TYPE((PyArrayObject *)cast) == NPY_DOUBLE && ((double *)PyArray_DATA((PyArrayObject *)cast))[2] == 9.0) score += 100;\n"
        "    Py_XDECREF(cast);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_cast_to_type_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    npy_intp dims[1] = {3};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_LONG);\n"
        "    if (arr == NULL) return NULL;\n"
        "    long *data = (long *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    data[0] = 2; data[1] = 4; data[2] = 6;\n"
        "    long score = 0;\n"
        "    PyObject *cast_macro = PyArray_Cast((PyArrayObject *)arr, NPY_DOUBLE);\n"
        "    if (cast_macro != NULL && cast_macro != arr && PyArray_TYPE((PyArrayObject *)cast_macro) == NPY_DOUBLE && ((double *)PyArray_DATA((PyArrayObject *)cast_macro))[1] == 4.0) score += 1;\n"
        "    Py_XDECREF(cast_macro);\n"
        "    PyObject *cast_direct = PyArray_CastToType((PyArrayObject *)arr, PyArray_DescrFromType(NPY_DOUBLE), 0);\n"
        "    if (cast_direct != NULL && cast_direct != arr && PyArray_TYPE((PyArrayObject *)cast_direct) == NPY_DOUBLE && ((double *)PyArray_DATA((PyArrayObject *)cast_direct))[2] == 6.0) score += 10;\n"
        "    Py_XDECREF(cast_direct);\n"
        "    PyObject *same_type = PyArray_CastToType((PyArrayObject *)arr, PyArray_DescrFromType(NPY_LONG), 0);\n"
        "    if (same_type != NULL && same_type != arr && PyArray_TYPE((PyArrayObject *)same_type) == NPY_LONG && ((long *)PyArray_DATA((PyArrayObject *)same_type))[0] == 2) score += 100;\n"
        "    Py_XDECREF(same_type);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_fill_with_scalar_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    npy_intp dims[1] = {4};\n"
        "    PyObject *long_arr = PyArray_SimpleNew(1, dims, NPY_LONG);\n"
        "    PyObject *double_arr = PyArray_SimpleNew(1, dims, NPY_DOUBLE);\n"
        "    PyObject *seven = PyLong_FromLong(7);\n"
        "    PyObject *two_half = PyFloat_FromDouble(2.5);\n"
        "    if (long_arr == NULL || double_arr == NULL || seven == NULL || two_half == NULL) return NULL;\n"
        "    long score = 0;\n"
        "    if (PyArray_FillWithScalar((PyArrayObject *)long_arr, seven) == 0) {\n"
        "        long *data = (long *)PyArray_DATA((PyArrayObject *)long_arr);\n"
        "        if (data[0] == 7 && data[1] == 7 && data[2] == 7 && data[3] == 7) score += 1;\n"
        "    }\n"
        "    if (PyArray_FillWithScalar((PyArrayObject *)double_arr, two_half) == 0) {\n"
        "        double *data = (double *)PyArray_DATA((PyArrayObject *)double_arr);\n"
        "        if (data[0] == 2.5 && data[1] == 2.5 && data[2] == 2.5 && data[3] == 2.5) score += 10;\n"
        "    }\n"
        "    Py_DECREF(two_half);\n"
        "    Py_DECREF(seven);\n"
        "    Py_DECREF(double_arr);\n"
        "    Py_DECREF(long_arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_to_list_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    npy_intp dims[2] = {2, 3};\n"
        "    PyObject *arr = PyArray_SimpleNew(2, dims, NPY_LONG);\n"
        "    PyObject *scalar_value = PyFloat_FromDouble(3.5);\n"
        "    PyObject *scalar_arr = scalar_value != NULL ? PyArray_FromScalar(scalar_value, NULL) : NULL;\n"
        "    Py_XDECREF(scalar_value);\n"
        "    if (arr == NULL || scalar_arr == NULL) return NULL;\n"
        "    long *data = (long *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    data[0] = 1; data[1] = 2; data[2] = 3; data[3] = 4; data[4] = 5; data[5] = 6;\n"
        "    long score = 0;\n"
        "    PyObject *list = PyArray_ToList((PyArrayObject *)arr);\n"
        "    if (list != NULL && PyList_Check(list) && PyList_Size(list) == 2) {\n"
        "        PyObject *row0 = PyList_GetItem(list, 0);\n"
        "        PyObject *row1 = PyList_GetItem(list, 1);\n"
        "        if (PyList_Check(row0) && PyList_Check(row1) && PyList_Size(row0) == 3 && PyList_Size(row1) == 3 &&\n"
        "            PyLong_AsLong(PyList_GetItem(row0, 0)) == 1 && PyLong_AsLong(PyList_GetItem(row0, 2)) == 3 &&\n"
        "            PyLong_AsLong(PyList_GetItem(row1, 0)) == 4 && PyLong_AsLong(PyList_GetItem(row1, 2)) == 6) score += 1;\n"
        "    }\n"
        "    Py_XDECREF(list);\n"
        "    PyObject *scalar = PyArray_ToList((PyArrayObject *)scalar_arr);\n"
        "    if (scalar != NULL && !PyList_Check(scalar) && PyFloat_AsDouble(scalar) == 3.5) score += 10;\n"
        "    Py_XDECREF(scalar);\n"
        "    Py_DECREF(scalar_arr);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_to_string_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    npy_intp dims[1] = {3};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_LONG);\n"
        "    if (arr == NULL) return NULL;\n"
        "    long *data = (long *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    data[0] = 11; data[1] = 22; data[2] = 33;\n"
        "    long score = 0;\n"
        "    PyObject *bytes = PyArray_ToString((PyArrayObject *)arr, NPY_CORDER);\n"
        "    if (bytes != NULL && PyBytes_Check(bytes) && PyBytes_Size(bytes) == (Py_ssize_t)(3 * sizeof(long))) {\n"
        "        char *raw = PyBytes_AsString(bytes);\n"
        "        if (raw != NULL && ((long *)raw)[0] == 11 && ((long *)raw)[1] == 22 && ((long *)raw)[2] == 33) score += 1;\n"
        "    }\n"
        "    Py_XDECREF(bytes);\n"
        "    bytes = PyArray_ToString((PyArrayObject *)arr, NPY_ANYORDER);\n"
        "    if (bytes != NULL && PyBytes_Check(bytes) && PyBytes_Size(bytes) == (Py_ssize_t)(3 * sizeof(long))) {\n"
        "        char *raw = PyBytes_AsString(bytes);\n"
        "        if (raw != NULL && ((long *)raw)[0] == 11 && ((long *)raw)[1] == 22 && ((long *)raw)[2] == 33) score += 10;\n"
        "    }\n"
        "    Py_XDECREF(bytes);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_byteswap_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    npy_intp dims[1] = {2};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_UINT);\n"
        "    if (arr == NULL) return NULL;\n"
        "    uint32_t *data = (uint32_t *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    data[0] = 0x01020304u;\n"
        "    data[1] = 0x0a0b0c0du;\n"
        "    long score = 0;\n"
        "    PyObject *copy = PyArray_Byteswap((PyArrayObject *)arr, 0);\n"
        "    if (copy != NULL && copy != arr && PyArray_TYPE((PyArrayObject *)copy) == NPY_UINT) {\n"
        "        uint32_t *copy_data = (uint32_t *)PyArray_DATA((PyArrayObject *)copy);\n"
        "        if (copy_data[0] == 0x04030201u && copy_data[1] == 0x0d0c0b0au && data[0] == 0x01020304u) score += 1;\n"
        "    }\n"
        "    Py_XDECREF(copy);\n"
        "    PyObject *same = PyArray_Byteswap((PyArrayObject *)arr, 1);\n"
        "    if (same == arr && data[0] == 0x04030201u && data[1] == 0x0d0c0b0au) score += 10;\n"
        "    Py_XDECREF(same);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_from_string_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    uint32_t raw[3] = {0x11223344u, 0x55667788u, 0x99aabbccu};\n"
        "    long score = 0;\n"
        "    PyArray_Descr *descr = PyArray_DescrNewFromType(NPY_UINT);\n"
        "    if (descr == NULL) return NULL;\n"
        "    PyObject *all = PyArray_FromString((char *)raw, (npy_intp)sizeof(raw), descr, -1, NULL);\n"
        "    if (all != NULL && PyArray_NDIM((PyArrayObject *)all) == 1 && PyArray_SIZE((PyArrayObject *)all) == 3 && PyArray_TYPE((PyArrayObject *)all) == NPY_UINT) {\n"
        "        uint32_t *data = (uint32_t *)PyArray_DATA((PyArrayObject *)all);\n"
        "        if (data[0] == raw[0] && data[1] == raw[1] && data[2] == raw[2]) score += 1;\n"
        "    }\n"
        "    Py_XDECREF(all);\n"
        "    descr = PyArray_DescrNewFromType(NPY_UINT);\n"
        "    if (descr == NULL) return NULL;\n"
        '    PyObject *two = PyArray_FromString((char *)raw, (npy_intp)sizeof(raw), descr, 2, "");\n'
        "    if (two != NULL && PyArray_SIZE((PyArrayObject *)two) == 2) {\n"
        "        uint32_t *data = (uint32_t *)PyArray_DATA((PyArrayObject *)two);\n"
        "        if (data[0] == raw[0] && data[1] == raw[1]) score += 10;\n"
        "    }\n"
        "    Py_XDECREF(two);\n"
        "    descr = PyArray_DescrNewFromType(NPY_OBJECT);\n"
        "    if (descr == NULL) return NULL;\n"
        "    PyObject *object_arr = PyArray_FromString((char *)raw, (npy_intp)sizeof(raw), descr, -1, NULL);\n"
        "    if (object_arr == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100; }\n"
        "    Py_XDECREF(object_arr);\n"
        "    descr = PyArray_DescrNewFromType(NPY_UINT);\n"
        "    if (descr == NULL) return NULL;\n"
        '    PyObject *text_arr = PyArray_FromString((char *)raw, (npy_intp)sizeof(raw), descr, -1, ",");\n'
        "    if (text_arr == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000; }\n"
        "    Py_XDECREF(text_arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_from_buffer_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    uint16_t raw[4] = {10, 20, 30, 40};\n"
        "    long score = 0;\n"
        "    PyObject *mv = PyMemoryView_FromMemory((char *)raw, (Py_ssize_t)sizeof(raw), PyBUF_WRITE);\n"
        "    if (mv == NULL) return NULL;\n"
        "    PyArray_Descr *descr = PyArray_DescrNewFromType(NPY_USHORT);\n"
        "    if (descr == NULL) { Py_DECREF(mv); return NULL; }\n"
        "    PyObject *arr = PyArray_FromBuffer(mv, descr, -1, 0);\n"
        "    if (arr != NULL && PyArray_NDIM((PyArrayObject *)arr) == 1 && PyArray_SIZE((PyArrayObject *)arr) == 4 &&\n"
        "        PyArray_TYPE((PyArrayObject *)arr) == NPY_USHORT && PyArray_BASE((PyArrayObject *)arr) == mv &&\n"
        "        !(PyArray_FLAGS((PyArrayObject *)arr) & NPY_ARRAY_OWNDATA) && PyArray_ISWRITEABLE((PyArrayObject *)arr)) {\n"
        "        uint16_t *data = (uint16_t *)PyArray_DATA((PyArrayObject *)arr);\n"
        "        if (data[0] == 10 && data[3] == 40) score += 1;\n"
        "        data[1] = 222;\n"
        "        Py_buffer after;\n"
        "        if (PyObject_GetBuffer(mv, &after, PyBUF_SIMPLE) == 0) {\n"
        "            uint16_t *view_data = (uint16_t *)after.buf;\n"
        "            if (view_data[1] == 222) score += 10;\n"
        "            PyBuffer_Release(&after);\n"
        "        }\n"
        "    }\n"
        "    Py_XDECREF(arr);\n"
        "    Py_DECREF(mv);\n"
        "    uint16_t raw_ro[4] = {10, 20, 30, 40};\n"
        "    PyObject *bytes = PyBytes_FromStringAndSize((const char *)raw_ro, (Py_ssize_t)sizeof(raw_ro));\n"
        "    if (bytes == NULL) return NULL;\n"
        "    descr = PyArray_DescrNewFromType(NPY_USHORT);\n"
        "    if (descr == NULL) { Py_DECREF(bytes); return NULL; }\n"
        "    arr = PyArray_FromBuffer(bytes, descr, 2, (npy_intp)sizeof(uint16_t));\n"
        "    if (arr != NULL && PyArray_SIZE((PyArrayObject *)arr) == 2 && PyArray_BASE((PyArrayObject *)arr) == bytes && !PyArray_ISWRITEABLE((PyArrayObject *)arr)) {\n"
        "        uint16_t *data = (uint16_t *)PyArray_DATA((PyArrayObject *)arr);\n"
        "        if (data[0] == 20 && data[1] == 30) score += 100;\n"
        "    }\n"
        "    Py_XDECREF(arr);\n"
        "    descr = PyArray_DescrNewFromType(NPY_OBJECT);\n"
        "    if (descr == NULL) { Py_DECREF(bytes); return NULL; }\n"
        "    arr = PyArray_FromBuffer(bytes, descr, -1, 0);\n"
        "    if (arr == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000; }\n"
        "    Py_XDECREF(arr);\n"
        "    descr = PyArray_DescrNewFromType(NPY_USHORT);\n"
        "    if (descr == NULL) { Py_DECREF(bytes); return NULL; }\n"
        "    arr = PyArray_FromBuffer(bytes, descr, -1, 99);\n"
        "    if (arr == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    Py_XDECREF(arr);\n"
        "    descr = PyArray_DescrNewFromType(NPY_USHORT);\n"
        "    if (descr == NULL) { Py_DECREF(bytes); return NULL; }\n"
        "    arr = PyArray_FromBuffer(bytes, descr, -1, 1);\n"
        "    if (arr == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_XDECREF(arr);\n"
        "    Py_DECREF(bytes);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_buffer_converter_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    PyArray_Chunk chunk;\n"
        "    memset(&chunk, 0, sizeof(chunk));\n"
        "    if (PyArray_BufferConverter(Py_None, &chunk) == NPY_SUCCEED && chunk.ptr == NULL &&\n"
        "        chunk.base == NULL && chunk.len == 0 && (chunk.flags & NPY_ARRAY_BEHAVED) == NPY_ARRAY_BEHAVED) score += 1;\n"
        "    char raw[4] = {1, 2, 3, 4};\n"
        "    PyObject *mv = PyMemoryView_FromMemory(raw, 4, PyBUF_WRITE);\n"
        "    if (mv == NULL) return NULL;\n"
        "    memset(&chunk, 0, sizeof(chunk));\n"
        "    if (PyArray_BufferConverter(mv, &chunk) == NPY_SUCCEED && chunk.ptr != NULL &&\n"
        "        chunk.len == 4 && chunk.base == mv && (chunk.flags & NPY_ARRAY_WRITEABLE)) score += 10;\n"
        "    Py_DECREF(mv);\n"
        "    PyObject *ro = PyMemoryView_FromMemory(raw, 4, PyBUF_READ);\n"
        "    if (ro == NULL) return NULL;\n"
        "    memset(&chunk, 0, sizeof(chunk));\n"
        "    if (PyArray_BufferConverter(ro, &chunk) == NPY_SUCCEED && chunk.ptr != NULL &&\n"
        "        chunk.len == 4 && chunk.base == ro && !(chunk.flags & NPY_ARRAY_WRITEABLE)) score += 100;\n"
        "    Py_DECREF(ro);\n"
        "    if (PyArray_BufferConverter(Py_None, NULL) != NPY_SUCCEED && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000; }\n"
        "    PyObject *bad = PyLong_FromLong(5);\n"
        "    if (bad == NULL) return NULL;\n"
        "    memset(&chunk, 0, sizeof(chunk));\n"
        "    if (PyArray_BufferConverter(bad, &chunk) != NPY_SUCCEED && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    Py_DECREF(bad);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_from_iter_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    PyObject *list = PyList_New(3);\n"
        "    if (list == NULL) return NULL;\n"
        "    if (PyList_SetItem(list, 0, PyLong_FromLong(3)) != 0 ||\n"
        "        PyList_SetItem(list, 1, PyLong_FromLong(4)) != 0 ||\n"
        "        PyList_SetItem(list, 2, PyLong_FromLong(5)) != 0) {\n"
        "        Py_DECREF(list);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyArray_Descr *descr = PyArray_DescrNewFromType(NPY_LONG);\n"
        "    if (descr == NULL) { Py_DECREF(list); return NULL; }\n"
        "    PyObject *arr = PyArray_FromIter(list, descr, -1);\n"
        "    if (arr != NULL && PyArray_NDIM((PyArrayObject *)arr) == 1 && PyArray_SIZE((PyArrayObject *)arr) == 3 && PyArray_TYPE((PyArrayObject *)arr) == NPY_LONG) {\n"
        "        long *data = (long *)PyArray_DATA((PyArrayObject *)arr);\n"
        "        if (data[0] == 3 && data[1] == 4 && data[2] == 5) score += 1;\n"
        "    }\n"
        "    Py_XDECREF(arr);\n"
        "    PyObject *tuple = PyTuple_New(3);\n"
        "    if (tuple == NULL) { Py_DECREF(list); return NULL; }\n"
        "    if (PyTuple_SetItem(tuple, 0, PyLong_FromLong(7)) != 0 ||\n"
        "        PyTuple_SetItem(tuple, 1, PyLong_FromLong(8)) != 0 ||\n"
        "        PyTuple_SetItem(tuple, 2, PyLong_FromLong(9)) != 0) {\n"
        "        Py_DECREF(tuple);\n"
        "        Py_DECREF(list);\n"
        "        return NULL;\n"
        "    }\n"
        "    descr = PyArray_DescrNewFromType(NPY_LONG);\n"
        "    if (descr == NULL) { Py_DECREF(tuple); Py_DECREF(list); return NULL; }\n"
        "    arr = PyArray_FromIter(tuple, descr, 2);\n"
        "    if (arr != NULL && PyArray_SIZE((PyArrayObject *)arr) == 2) {\n"
        "        long *data = (long *)PyArray_DATA((PyArrayObject *)arr);\n"
        "        if (data[0] == 7 && data[1] == 8) score += 10;\n"
        "    }\n"
        "    Py_XDECREF(arr);\n"
        "    descr = PyArray_DescrNewFromType(NPY_LONG);\n"
        "    if (descr == NULL) { Py_DECREF(tuple); Py_DECREF(list); return NULL; }\n"
        "    arr = PyArray_FromIter(tuple, descr, 5);\n"
        "    if (arr == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100; }\n"
        "    Py_XDECREF(arr);\n"
        "    descr = PyArray_DescrNewFromType(NPY_STRING);\n"
        "    if (descr == NULL) { Py_DECREF(tuple); Py_DECREF(list); return NULL; }\n"
        "    arr = PyArray_FromIter(list, descr, -1);\n"
        "    if (arr == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000; }\n"
        "    Py_XDECREF(arr);\n"
        "    Py_DECREF(tuple);\n"
        "    Py_DECREF(list);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_converter_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    PyObject *list = PyList_New(2);\n"
        "    if (list == NULL) return NULL;\n"
        "    if (PyList_SetItem(list, 0, PyLong_FromLong(3)) != 0 ||\n"
        "        PyList_SetItem(list, 1, PyLong_FromLong(4)) != 0) {\n"
        "        Py_DECREF(list);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *converted = NULL;\n"
        "    if (PyArray_Converter(list, &converted) == NPY_SUCCEED && converted != NULL && PyArray_Check(converted) && PyArray_SIZE((PyArrayObject *)converted) == 2) {\n"
        "        score += 1;\n"
        "    }\n"
        "    Py_XDECREF(converted);\n"
        "    Py_DECREF(list);\n"
        "    PyObject *a = PyLong_FromLong(7);\n"
        "    PyObject *b = PyLong_FromLong(8);\n"
        "    if (a == NULL || b == NULL) { Py_XDECREF(a); Py_XDECREF(b); return NULL; }\n"
        "    PyObject *tuple = PyTuple_Pack(2, a, b);\n"
        "    Py_DECREF(a);\n"
        "    Py_DECREF(b);\n"
        "    if (tuple == NULL) return NULL;\n"
        "    PyObject *arr = PyArray_FromAny(tuple, NULL, 1, 1, 0, NULL);\n"
        "    Py_DECREF(tuple);\n"
        "    if (arr == NULL) return NULL;\n"
        "    converted = NULL;\n"
        "    if (PyArray_Converter(arr, &converted) == NPY_SUCCEED && converted == arr && PyArray_Check(converted)) score += 10;\n"
        "    Py_XDECREF(converted);\n"
        "    Py_DECREF(arr);\n"
        "    converted = (PyObject *)1;\n"
        "    if (PyArray_Converter(Py_None, &converted) != NPY_SUCCEED && converted == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100; }\n"
        "    if (PyArray_Converter(Py_None, NULL) != NPY_SUCCEED && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000; }\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_pyint_converter_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    PyObject *small = PyLong_FromLong(42);\n"
        "    if (small == NULL) return NULL;\n"
        "    if (PyArray_PyIntAsInt(small) == 42 && PyArray_PyIntAsIntp(small) == (npy_intp)42) score += 1;\n"
        "    Py_DECREF(small);\n"
        "    PyObject *negative = PyLong_FromLong(-7);\n"
        "    if (negative == NULL) return NULL;\n"
        "    if (PyArray_PyIntAsInt(negative) == -7 && PyArray_PyIntAsIntp(negative) == (npy_intp)-7) score += 10;\n"
        "    Py_DECREF(negative);\n"
        "    if (PyArray_PyIntAsInt(Py_True) == -1 && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100; }\n"
        "    if (PyArray_PyIntAsIntp(Py_False) == (npy_intp)-1 && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000; }\n"
        "    PyObject *flt = PyFloat_FromDouble(3.0);\n"
        "    if (flt == NULL) return NULL;\n"
        "    if (PyArray_PyIntAsInt(flt) == -1 && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    Py_DECREF(flt);\n"
        "    PyObject *large = PyLong_FromLongLong(2147483648LL);\n"
        "    if (large == NULL) return NULL;\n"
        "    if (PyArray_PyIntAsInt(large) == -1 && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    if (PyArray_PyIntAsIntp(large) == (npy_intp)2147483648LL && PyErr_Occurred() == NULL) score += 1000000;\n"
        "    Py_DECREF(large);\n"
        "    if (PyArray_PyIntAsInt(NULL) == -1 && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000000; }\n"
        "    int parsed = 0;\n"
        "    PyObject *arg_small = PyLong_FromLong(123);\n"
        "    if (arg_small == NULL) return NULL;\n"
        "    if (PyArray_PythonPyIntFromInt(arg_small, &parsed) && parsed == 123) score += 100000000L;\n"
        "    Py_DECREF(arg_small);\n"
        "    PyObject *arg_negative = PyLong_FromLong(-9);\n"
        "    if (arg_negative == NULL) return NULL;\n"
        "    parsed = 0;\n"
        "    if (PyArray_PythonPyIntFromInt(arg_negative, &parsed) && parsed == -9) score += 1000000000L;\n"
        "    Py_DECREF(arg_negative);\n"
        "    parsed = 77;\n"
        "    if (!PyArray_PythonPyIntFromInt(Py_True, &parsed) && parsed == 77 && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000000000L; }\n"
        "    PyObject *arg_too_large = PyLong_FromLongLong(2147483648LL);\n"
        "    if (arg_too_large == NULL) return NULL;\n"
        "    parsed = 88;\n"
        "    if (!PyArray_PythonPyIntFromInt(arg_too_large, &parsed) && parsed == 88 && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000000000L; }\n"
        "    Py_DECREF(arg_too_large);\n"
        "    parsed = 99;\n"
        "    if (!PyArray_PythonPyIntFromInt(NULL, &parsed) && parsed == 99 && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000000000000L; }\n"
        "    if (!PyArray_PythonPyIntFromInt(Py_None, NULL) && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000000000000L; }\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_intp_from_sequence_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp vals[4] = {-99, -99, -99, -99};\n"
        "    PyObject *scalar = PyLong_FromLong(5);\n"
        "    if (scalar == NULL) return NULL;\n"
        "    int n = PyArray_IntpFromSequence(scalar, vals, 4);\n"
        "    if (n == 1 && vals[0] == 5 && PyErr_Occurred() == NULL) score += 1;\n"
        "    Py_DECREF(scalar);\n"
        "    vals[0] = vals[1] = vals[2] = vals[3] = -99;\n"
        "    PyObject *tuple = PyTuple_New(3);\n"
        "    if (tuple == NULL) return NULL;\n"
        "    if (PyTuple_SetItem(tuple, 0, PyLong_FromLong(7)) != 0 ||\n"
        "        PyTuple_SetItem(tuple, 1, PyLong_FromLong(8)) != 0 ||\n"
        "        PyTuple_SetItem(tuple, 2, PyLong_FromLong(9)) != 0) {\n"
        "        Py_DECREF(tuple);\n"
        "        return NULL;\n"
        "    }\n"
        "    n = PyArray_IntpFromSequence(tuple, vals, 4);\n"
        "    if (n == 3 && vals[0] == 7 && vals[1] == 8 && vals[2] == 9 && PyErr_Occurred() == NULL) score += 10;\n"
        "    Py_DECREF(tuple);\n"
        "    vals[0] = vals[1] = vals[2] = vals[3] = -99;\n"
        "    PyObject *list = PyList_New(3);\n"
        "    if (list == NULL) return NULL;\n"
        "    if (PyList_SetItem(list, 0, PyLong_FromLong(4)) != 0 ||\n"
        "        PyList_SetItem(list, 1, PyLong_FromLong(5)) != 0 ||\n"
        "        PyList_SetItem(list, 2, PyLong_FromLong(6)) != 0) {\n"
        "        Py_DECREF(list);\n"
        "        return NULL;\n"
        "    }\n"
        "    n = PyArray_IntpFromSequence(list, vals, 2);\n"
        "    if (n == 3 && vals[0] == 4 && vals[1] == 5 && vals[2] == -99 && PyErr_Occurred() == NULL) score += 100;\n"
        "    Py_DECREF(list);\n"
        "    if (PyArray_IntpFromSequence(Py_True, vals, 4) == -1 && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000; }\n"
        "    PyObject *flt = PyFloat_FromDouble(3.5);\n"
        "    if (flt == NULL) return NULL;\n"
        "    if (PyArray_IntpFromSequence(flt, vals, 4) == -1 && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    Py_DECREF(flt);\n"
        "    if (PyArray_IntpFromSequence(Py_None, NULL, 4) == -1 && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_intp_converter_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    PyArray_Dims dims = {NULL, 123};\n"
        "    PyObject *scalar = PyLong_FromLong(5);\n"
        "    if (scalar == NULL) return NULL;\n"
        "    if (PyArray_IntpConverter(scalar, &dims) == NPY_SUCCEED && dims.len == 1 && dims.ptr != NULL && dims.ptr[0] == 5) score += 1;\n"
        "    PyDimMem_FREE(dims.ptr);\n"
        "    dims.ptr = NULL;\n"
        "    dims.len = 0;\n"
        "    Py_DECREF(scalar);\n"
        "    PyObject *tuple = PyTuple_New(2);\n"
        "    if (tuple == NULL) return NULL;\n"
        "    if (PyTuple_SetItem(tuple, 0, PyLong_FromLong(7)) != 0 ||\n"
        "        PyTuple_SetItem(tuple, 1, PyLong_FromLong(8)) != 0) {\n"
        "        Py_DECREF(tuple);\n"
        "        return NULL;\n"
        "    }\n"
        "    if (PyArray_IntpConverter(tuple, &dims) == NPY_SUCCEED && dims.len == 2 && dims.ptr != NULL && dims.ptr[0] == 7 && dims.ptr[1] == 8) score += 10;\n"
        "    PyDimMem_FREE(dims.ptr);\n"
        "    dims.ptr = NULL;\n"
        "    dims.len = 0;\n"
        "    Py_DECREF(tuple);\n"
        "    PyObject *empty = PyTuple_New(0);\n"
        "    if (empty == NULL) return NULL;\n"
        "    if (PyArray_IntpConverter(empty, &dims) == NPY_SUCCEED && dims.len == 0 && dims.ptr == NULL) score += 100;\n"
        "    PyDimMem_FREE(dims.ptr);\n"
        "    dims.ptr = NULL;\n"
        "    dims.len = 0;\n"
        "    Py_DECREF(empty);\n"
        "    dims.ptr = (npy_intp *)1;\n"
        "    dims.len = 99;\n"
        "    if (PyArray_IntpConverter(Py_None, &dims) != NPY_SUCCEED && dims.ptr == NULL && dims.len == 0 && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000; }\n"
        "    dims.ptr = (npy_intp *)1;\n"
        "    dims.len = 99;\n"
        "    if (PyArray_IntpConverter(Py_True, &dims) != NPY_SUCCEED && dims.ptr == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_optional_intp_converter_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    PyArray_Dims dims;\n"
        "    npy_intp sentinel[1] = {77};\n"
        "    dims.ptr = sentinel;\n"
        "    dims.len = 77;\n"
        "    if (PyArray_OptionalIntpConverter(Py_None, &dims) == NPY_SUCCEED && dims.ptr == sentinel && dims.len == 77) score += 1;\n"
        "    dims.ptr = NULL;\n"
        "    dims.len = 0;\n"
        "    PyObject *tuple = PyTuple_New(2);\n"
        "    if (tuple == NULL) return NULL;\n"
        "    if (PyTuple_SetItem(tuple, 0, PyLong_FromLong(2)) != 0 ||\n"
        "        PyTuple_SetItem(tuple, 1, PyLong_FromLong(4)) != 0) {\n"
        "        Py_DECREF(tuple);\n"
        "        return NULL;\n"
        "    }\n"
        "    if (PyArray_OptionalIntpConverter(tuple, &dims) == NPY_SUCCEED && dims.len == 2 && dims.ptr != NULL && dims.ptr[0] == 2 && dims.ptr[1] == 4) score += 10;\n"
        "    PyDimMem_FREE(dims.ptr);\n"
        "    dims.ptr = NULL;\n"
        "    dims.len = 0;\n"
        "    Py_DECREF(tuple);\n"
        "    PyObject *scalar = PyLong_FromLong(9);\n"
        "    if (scalar == NULL) return NULL;\n"
        "    if (PyArray_OptionalIntpConverter(scalar, &dims) == NPY_SUCCEED && dims.len == 1 && dims.ptr != NULL && dims.ptr[0] == 9) score += 100;\n"
        "    PyDimMem_FREE(dims.ptr);\n"
        "    dims.ptr = NULL;\n"
        "    dims.len = 0;\n"
        "    Py_DECREF(scalar);\n"
        "    dims.ptr = sentinel;\n"
        "    dims.len = 77;\n"
        "    if (PyArray_OptionalIntpConverter(Py_False, &dims) != NPY_SUCCEED && dims.ptr == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000; }\n"
        "    if (PyArray_OptionalIntpConverter(Py_None, NULL) != NPY_SUCCEED && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_promote_types_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    PyArray_Descr *bool_descr = PyArray_DescrFromType(NPY_BOOL);\n"
        "    PyArray_Descr *long_descr = PyArray_DescrFromType(NPY_LONG);\n"
        "    PyArray_Descr *double_descr = PyArray_DescrFromType(NPY_DOUBLE);\n"
        "    PyArray_Descr *cdouble_descr = PyArray_DescrFromType(NPY_CDOUBLE);\n"
        "    PyArray_Descr *object_descr = PyArray_DescrFromType(NPY_OBJECT);\n"
        "    if (bool_descr == NULL || long_descr == NULL || double_descr == NULL || cdouble_descr == NULL || object_descr == NULL) return NULL;\n"
        "    PyArray_Descr *res = PyArray_PromoteTypes(bool_descr, long_descr);\n"
        "    if (res != NULL && PyDataType_TYPE(res) == NPY_LONG) score += 1;\n"
        "    Py_XDECREF((PyObject *)res);\n"
        "    res = PyArray_PromoteTypes(long_descr, double_descr);\n"
        "    if (res != NULL && PyDataType_TYPE(res) == NPY_DOUBLE) score += 10;\n"
        "    Py_XDECREF((PyObject *)res);\n"
        "    res = PyArray_PromoteTypes(double_descr, cdouble_descr);\n"
        "    if (res != NULL && PyDataType_TYPE(res) == NPY_CDOUBLE) score += 100;\n"
        "    Py_XDECREF((PyObject *)res);\n"
        "    res = PyArray_PromoteTypes(long_descr, object_descr);\n"
        "    if (res != NULL && PyDataType_TYPE(res) == NPY_OBJECT) score += 1000;\n"
        "    Py_XDECREF((PyObject *)res);\n"
        "    if (PyArray_PromoteTypes(NULL, long_descr) == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_result_type_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims1[1] = {2};\n"
        "    PyObject *arr_long_obj = PyArray_SimpleNew(1, dims1, NPY_LONG);\n"
        "    PyObject *arr_double_obj = PyArray_SimpleNew(1, dims1, NPY_DOUBLE);\n"
        "    PyObject *arr_bool_obj = PyArray_SimpleNew(1, dims1, NPY_BOOL);\n"
        "    if (arr_long_obj == NULL || arr_double_obj == NULL || arr_bool_obj == NULL) {\n"
        "        Py_XDECREF(arr_long_obj);\n"
        "        Py_XDECREF(arr_double_obj);\n"
        "        Py_XDECREF(arr_bool_obj);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyArrayObject *arrays2[2] = {(PyArrayObject *)arr_long_obj, (PyArrayObject *)arr_double_obj};\n"
        "    PyArray_Descr *res = PyArray_ResultType(2, arrays2, 0, NULL);\n"
        "    if (res != NULL && PyDataType_TYPE(res) == NPY_DOUBLE) score += 1;\n"
        "    Py_XDECREF((PyObject *)res);\n"
        "    PyArray_Descr *long_descr = PyArray_DescrFromType(NPY_LONG);\n"
        "    PyArray_Descr *cdouble_descr = PyArray_DescrFromType(NPY_CDOUBLE);\n"
        "    if (long_descr == NULL || cdouble_descr == NULL) {\n"
        "        Py_DECREF(arr_long_obj);\n"
        "        Py_DECREF(arr_double_obj);\n"
        "        Py_DECREF(arr_bool_obj);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyArray_Descr *descrs2[2] = {long_descr, cdouble_descr};\n"
        "    res = PyArray_ResultType(0, NULL, 2, descrs2);\n"
        "    if (res != NULL && PyDataType_TYPE(res) == NPY_CDOUBLE) score += 10;\n"
        "    Py_XDECREF((PyObject *)res);\n"
        "    PyArrayObject *arrays1[1] = {(PyArrayObject *)arr_bool_obj};\n"
        "    res = PyArray_ResultType(1, arrays1, 1, &long_descr);\n"
        "    if (res != NULL && PyDataType_TYPE(res) == NPY_LONG) score += 100;\n"
        "    Py_XDECREF((PyObject *)res);\n"
        "    if (PyArray_ResultType(0, NULL, 0, NULL) == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000; }\n"
        "    if (PyArray_ResultType(1, NULL, 0, NULL) == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    Py_DECREF(arr_long_obj);\n"
        "    Py_DECREF(arr_double_obj);\n"
        "    Py_DECREF(arr_bool_obj);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_priority_score(PyObject *self, PyObject *args) {\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    double got = 0.0;\n"
        "    PyObject *plain = PyList_New(0);\n"
        "    if (plain == NULL) return NULL;\n"
        "    got = PyArray_GetPriority(plain, 4.25);\n"
        "    if (got > 4.24 && got < 4.26 && PyErr_Occurred() == NULL) score += 1;\n"
        "    Py_DECREF(plain);\n"
        "    PyObject *flt = PyFloat_FromDouble(7.5);\n"
        "    if (flt == NULL) return NULL;\n"
        '    if (PyObject_SetAttrString(self, "__array_priority__", flt) != 0) { Py_DECREF(flt); return NULL; }\n'
        "    Py_DECREF(flt);\n"
        "    got = PyArray_GetPriority(self, 1.0);\n"
        "    if (got > 7.49 && got < 7.51 && PyErr_Occurred() == NULL) score += 10;\n"
        "    PyObject *ival = PyLong_FromLong(9);\n"
        "    if (ival == NULL) return NULL;\n"
        '    if (PyObject_SetAttrString(self, "__array_priority__", ival) != 0) { Py_DECREF(ival); return NULL; }\n'
        "    Py_DECREF(ival);\n"
        "    got = PyArray_GetPriority(self, 1.0);\n"
        "    if (got > 8.99 && got < 9.01 && PyErr_Occurred() == NULL) score += 100;\n"
        '    PyObject *bad = PyUnicode_FromString("bad");\n'
        "    if (bad == NULL) return NULL;\n"
        '    if (PyObject_SetAttrString(self, "__array_priority__", bad) != 0) { Py_DECREF(bad); return NULL; }\n'
        "    Py_DECREF(bad);\n"
        "    got = PyArray_GetPriority(self, 3.25);\n"
        "    if (got > 3.24 && got < 3.26 && PyErr_Occurred() == NULL) score += 1000;\n"
        "    npy_intp dims[1] = {1};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_LONG);\n"
        "    if (arr == NULL) return NULL;\n"
        "    got = PyArray_GetPriority(arr, 5.5);\n"
        "    if (got > -0.01 && got < 0.01 && PyErr_Occurred() == NULL) score += 10000;\n"
        "    Py_DECREF(arr);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_check_strides_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims2x3[2] = {2, 3};\n"
        "    npy_intp cstrides[2] = {(npy_intp)(3 * sizeof(long)), (npy_intp)sizeof(long)};\n"
        "    if (PyArray_CheckStrides((int)sizeof(long), 2, (npy_intp)(6 * sizeof(long)), 0, dims2x3, cstrides)) score += 1;\n"
        "    npy_intp dims4[1] = {4};\n"
        "    npy_intp reverse[1] = {-(npy_intp)sizeof(long)};\n"
        "    if (PyArray_CheckStrides((int)sizeof(long), 1, (npy_intp)(4 * sizeof(long)), (npy_intp)(3 * sizeof(long)), dims4, reverse)) score += 10;\n"
        "    npy_intp bad_forward[1] = {(npy_intp)sizeof(long)};\n"
        "    if (!PyArray_CheckStrides((int)sizeof(long), 1, (npy_intp)(4 * sizeof(long)), (npy_intp)(2 * sizeof(long)), dims4, bad_forward)) score += 100;\n"
        "    npy_intp zero_dim[1] = {0};\n"
        "    if (PyArray_CheckStrides((int)sizeof(long), 1, 0, 0, zero_dim, bad_forward)) score += 1000;\n"
        "    if (!PyArray_CheckStrides((int)sizeof(long), 1, 0, 99, zero_dim, bad_forward)) score += 10000;\n"
        "    if (!PyArray_CheckStrides((int)sizeof(long), 2, (npy_intp)(6 * sizeof(long)), 0, dims2x3, NULL)) score += 100000;\n"
        "    if (!PyArray_CheckStrides((int)sizeof(long), -1, (npy_intp)(6 * sizeof(long)), 0, dims2x3, cstrides)) score += 1000000;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_broadcast_to_shape_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims2x1[2] = {2, 1};\n"
        "    PyObject *arr = PyArray_SimpleNew(2, dims2x1, NPY_LONG);\n"
        "    if (arr == NULL) return NULL;\n"
        "    long *data = (long *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    data[0] = 11; data[1] = 22;\n"
        "    PyObject *plain_iter_obj = PyArray_IterNew(arr);\n"
        "    if (plain_iter_obj != NULL) {\n"
        "        PyArrayIterObject *plain = (PyArrayIterObject *)plain_iter_obj;\n"
        "        if (plain->size == 2 && plain->nd_m1 == 1 &&\n"
        "            *(long *)PyArray_ITER_DATA(plain) == 11) {\n"
        "            PyArray_ITER_NEXT(plain);\n"
        "            if (plain->index == 1 && *(long *)PyArray_ITER_DATA(plain) == 22) score += 1;\n"
        "        }\n"
        "    }\n"
        "    Py_XDECREF(plain_iter_obj);\n"
        "    npy_intp target2x3[2] = {2, 3};\n"
        "    PyObject *iter_obj = PyArray_BroadcastToShape(arr, target2x3, 2);\n"
        "    if (iter_obj != NULL) {\n"
        "        PyArrayIterObject *it = (PyArrayIterObject *)iter_obj;\n"
        "        if (it->size == 6 && it->nd_m1 == 1 && it->dims_m1[0] == 1 &&\n"
        "            it->dims_m1[1] == 2 && it->strides[0] == (npy_intp)sizeof(long) &&\n"
        "            it->strides[1] == 0 && *(long *)PyArray_ITER_DATA(it) == 11) score += 10;\n"
        "        PyArray_ITER_NEXT(it);\n"
        "        if (it->index == 1 && it->coordinates[0] == 0 && it->coordinates[1] == 1 &&\n"
        "            *(long *)PyArray_ITER_DATA(it) == 11) score += 100;\n"
        "        PyArray_ITER_NEXT(it);\n"
        "        PyArray_ITER_NEXT(it);\n"
        "        if (it->index == 3 && it->coordinates[0] == 1 && it->coordinates[1] == 0 &&\n"
        "            *(long *)PyArray_ITER_DATA(it) == 22) score += 1000;\n"
        "    }\n"
        "    Py_XDECREF(iter_obj);\n"
        "    npy_intp dims3[1] = {3};\n"
        "    PyObject *row = PyArray_SimpleNew(1, dims3, NPY_LONG);\n"
        "    if (row == NULL) { Py_DECREF(arr); return NULL; }\n"
        "    long *row_data = (long *)PyArray_DATA((PyArrayObject *)row);\n"
        "    row_data[0] = 3; row_data[1] = 4; row_data[2] = 5;\n"
        "    iter_obj = PyArray_BroadcastToShape(row, target2x3, 2);\n"
        "    if (iter_obj != NULL) {\n"
        "        PyArrayIterObject *it = (PyArrayIterObject *)iter_obj;\n"
        "        if (it->size == 6 && it->strides[0] == 0 && it->strides[1] == (npy_intp)sizeof(long) &&\n"
        "            *(long *)PyArray_ITER_DATA(it) == 3) {\n"
        "            PyArray_ITER_NEXT(it);\n"
        "            if (*(long *)PyArray_ITER_DATA(it) == 4) {\n"
        "                PyArray_ITER_NEXT(it);\n"
        "                if (*(long *)PyArray_ITER_DATA(it) == 5) {\n"
        "                    PyArray_ITER_NEXT(it);\n"
        "                    if (*(long *)PyArray_ITER_DATA(it) == 3) score += 10000;\n"
        "                }\n"
        "            }\n"
        "        }\n"
        "    }\n"
        "    Py_XDECREF(iter_obj);\n"
        "    npy_intp bad_target[2] = {3, 3};\n"
        "    iter_obj = PyArray_BroadcastToShape(arr, bad_target, 2);\n"
        "    if (iter_obj == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_XDECREF(iter_obj);\n"
        "    Py_DECREF(row);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_broadcast_multi_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims2x3[2] = {2, 3};\n"
        "    npy_intp dims3[1] = {3};\n"
        "    PyObject *grid = PyArray_SimpleNew(2, dims2x3, NPY_LONG);\n"
        "    PyObject *row = PyArray_SimpleNew(1, dims3, NPY_LONG);\n"
        "    if (grid == NULL || row == NULL) {\n"
        "        Py_XDECREF(grid);\n"
        "        Py_XDECREF(row);\n"
        "        return NULL;\n"
        "    }\n"
        "    long *grid_data = (long *)PyArray_DATA((PyArrayObject *)grid);\n"
        "    long *row_data = (long *)PyArray_DATA((PyArrayObject *)row);\n"
        "    for (long i = 0; i < 6; i++) grid_data[i] = i + 1;\n"
        "    row_data[0] = 10;\n"
        "    row_data[1] = 20;\n"
        "    row_data[2] = 30;\n"
        "    PyObject *multi_obj = PyArray_MultiIterNew(2, grid, row);\n"
        "    if (multi_obj == NULL) {\n"
        "        Py_DECREF(row);\n"
        "        Py_DECREF(grid);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyArrayMultiIterObject *multi = (PyArrayMultiIterObject *)multi_obj;\n"
        "    PyArray_MultiIter_GOTO1D(multi, 4);\n"
        "    if (PyArray_Broadcast(multi) == NPY_SUCCEED && PyArray_MultiIter_NDIM(multi) == 2 &&\n"
        "        PyArray_MultiIter_SIZE(multi) == 6 && PyArray_MultiIter_DIMS(multi)[0] == 2 &&\n"
        "        PyArray_MultiIter_DIMS(multi)[1] == 3 && PyArray_MultiIter_INDEX(multi) == 0) score += 1;\n"
        "    if (*(long *)PyArray_MultiIter_DATA(multi, 0) == 1 &&\n"
        "        *(long *)PyArray_MultiIter_DATA(multi, 1) == 10) score += 10;\n"
        "    PyArray_MultiIter_GOTO1D(multi, 5);\n"
        "    if (*(long *)PyArray_MultiIter_DATA(multi, 0) == 6 &&\n"
        "        *(long *)PyArray_MultiIter_DATA(multi, 1) == 30) score += 100;\n"
        "    if (PyArray_Broadcast(NULL) != NPY_SUCCEED && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000; }\n"
        "    Py_DECREF(multi_obj);\n"
        "    npy_intp bad_dims[1] = {4};\n"
        "    PyObject *bad = PyArray_SimpleNew(1, bad_dims, NPY_LONG);\n"
        "    if (bad == NULL) {\n"
        "        Py_DECREF(row);\n"
        "        Py_DECREF(grid);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *bad_multi = PyArray_MultiIterNew(2, grid, bad);\n"
        "    if (bad_multi == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    Py_XDECREF(bad_multi);\n"
        "    Py_DECREF(bad);\n"
        "    Py_DECREF(row);\n"
        "    Py_DECREF(grid);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_multi_iter_from_objects_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims2x3[2] = {2, 3};\n"
        "    npy_intp dims3[1] = {3};\n"
        "    PyObject *grid = PyArray_SimpleNew(2, dims2x3, NPY_LONG);\n"
        "    PyObject *row = PyArray_SimpleNew(1, dims3, NPY_LONG);\n"
        "    if (grid == NULL || row == NULL) {\n"
        "        Py_XDECREF(grid);\n"
        "        Py_XDECREF(row);\n"
        "        return NULL;\n"
        "    }\n"
        "    long *grid_data = (long *)PyArray_DATA((PyArrayObject *)grid);\n"
        "    long *row_data = (long *)PyArray_DATA((PyArrayObject *)row);\n"
        "    for (long i = 0; i < 6; i++) grid_data[i] = i + 1;\n"
        "    row_data[0] = 10;\n"
        "    row_data[1] = 20;\n"
        "    row_data[2] = 30;\n"
        "    PyObject *mps_one[1] = {grid};\n"
        "    PyObject *multi_obj = PyArray_MultiIterFromObjects(mps_one, 1, 1, row);\n"
        "    if (multi_obj != NULL) {\n"
        "        PyArrayMultiIterObject *multi = (PyArrayMultiIterObject *)multi_obj;\n"
        "        if (PyArray_MultiIter_NUMITER(multi) == 2 && PyArray_MultiIter_NDIM(multi) == 2 &&\n"
        "            PyArray_MultiIter_SIZE(multi) == 6 && PyArray_MultiIter_DIMS(multi)[0] == 2 &&\n"
        "            PyArray_MultiIter_DIMS(multi)[1] == 3) score += 1;\n"
        "        if (*(long *)PyArray_MultiIter_DATA(multi, 0) == 1 &&\n"
        "            *(long *)PyArray_MultiIter_DATA(multi, 1) == 10) score += 10;\n"
        "        PyArray_MultiIter_GOTO1D(multi, 5);\n"
        "        if (*(long *)PyArray_MultiIter_DATA(multi, 0) == 6 &&\n"
        "            *(long *)PyArray_MultiIter_DATA(multi, 1) == 30) score += 100;\n"
        "    }\n"
        "    Py_XDECREF(multi_obj);\n"
        "    PyObject *mps_two[2] = {grid, row};\n"
        "    multi_obj = PyArray_MultiIterFromObjects(mps_two, 2, 0);\n"
        "    if (multi_obj != NULL) {\n"
        "        PyArrayMultiIterObject *multi = (PyArrayMultiIterObject *)multi_obj;\n"
        "        if (PyArray_MultiIter_NUMITER(multi) == 2 && PyArray_MultiIter_SIZE(multi) == 6 &&\n"
        "            *(long *)PyArray_MultiIter_DATA(multi, 0) == 1 &&\n"
        "            *(long *)PyArray_MultiIter_DATA(multi, 1) == 10) score += 1000;\n"
        "    }\n"
        "    Py_XDECREF(multi_obj);\n"
        "    npy_intp bad_dims[1] = {4};\n"
        "    PyObject *bad = PyArray_SimpleNew(1, bad_dims, NPY_LONG);\n"
        "    if (bad == NULL) {\n"
        "        Py_DECREF(row);\n"
        "        Py_DECREF(grid);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *bad_multi = PyArray_MultiIterFromObjects(mps_one, 1, 1, bad);\n"
        "    if (bad_multi == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    Py_XDECREF(bad_multi);\n"
        "    bad_multi = PyArray_MultiIterFromObjects(NULL, 1, 0);\n"
        "    if (bad_multi == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_XDECREF(bad_multi);\n"
        "    Py_DECREF(bad);\n"
        "    Py_DECREF(row);\n"
        "    Py_DECREF(grid);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_remove_smallest_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims2x3[2] = {2, 3};\n"
        "    npy_intp dims3[1] = {3};\n"
        "    PyObject *grid = PyArray_SimpleNew(2, dims2x3, NPY_LONG);\n"
        "    PyObject *row = PyArray_SimpleNew(1, dims3, NPY_LONG);\n"
        "    if (grid == NULL || row == NULL) {\n"
        "        Py_XDECREF(grid);\n"
        "        Py_XDECREF(row);\n"
        "        return NULL;\n"
        "    }\n"
        "    long *grid_data = (long *)PyArray_DATA((PyArrayObject *)grid);\n"
        "    long *row_data = (long *)PyArray_DATA((PyArrayObject *)row);\n"
        "    for (long i = 0; i < 6; i++) grid_data[i] = i + 1;\n"
        "    row_data[0] = 10;\n"
        "    row_data[1] = 20;\n"
        "    row_data[2] = 30;\n"
        "    PyObject *multi_obj = PyArray_MultiIterNew(2, grid, row);\n"
        "    if (multi_obj == NULL) {\n"
        "        Py_DECREF(row);\n"
        "        Py_DECREF(grid);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyArrayMultiIterObject *multi = (PyArrayMultiIterObject *)multi_obj;\n"
        "    int axis = PyArray_RemoveSmallest(multi);\n"
        "    if (axis == 1 && PyArray_MultiIter_SIZE(multi) == 2 &&\n"
        "        multi->iters[0]->dims_m1[1] == 0 && multi->iters[0]->backstrides[1] == 0 &&\n"
        "        multi->iters[1]->dims_m1[1] == 0 && multi->iters[1]->backstrides[1] == 0) score += 1;\n"
        "    if (*(long *)PyArray_MultiIter_DATA(multi, 0) == 1 &&\n"
        "        *(long *)PyArray_MultiIter_DATA(multi, 1) == 10) score += 10;\n"
        "    PyArray_MultiIter_NEXT(multi);\n"
        "    if (PyArray_MultiIter_INDEX(multi) == 1 &&\n"
        "        *(long *)PyArray_MultiIter_DATA(multi, 0) == 4 &&\n"
        "        *(long *)PyArray_MultiIter_DATA(multi, 1) == 10) score += 100;\n"
        "    Py_DECREF(multi_obj);\n"
        "    npy_intp dims0[1] = {1};\n"
        "    PyObject *scalar = PyArray_SimpleNew(0, dims0, NPY_LONG);\n"
        "    if (scalar == NULL) {\n"
        "        Py_DECREF(row);\n"
        "        Py_DECREF(grid);\n"
        "        return NULL;\n"
        "    }\n"
        "    multi_obj = PyArray_MultiIterNew(1, scalar);\n"
        "    if (multi_obj != NULL) {\n"
        "        if (PyArray_RemoveSmallest((PyArrayMultiIterObject *)multi_obj) == -1 && PyErr_Occurred() == NULL) score += 1000;\n"
        "    }\n"
        "    Py_XDECREF(multi_obj);\n"
        "    if (PyArray_RemoveSmallest(NULL) == -1 && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    Py_DECREF(scalar);\n"
        "    Py_DECREF(row);\n"
        "    Py_DECREF(grid);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_multi_iter_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims2x3[2] = {2, 3};\n"
        "    npy_intp dims3[1] = {3};\n"
        "    PyObject *grid = PyArray_SimpleNew(2, dims2x3, NPY_LONG);\n"
        "    PyObject *row = PyArray_SimpleNew(1, dims3, NPY_LONG);\n"
        "    if (grid == NULL || row == NULL) {\n"
        "        Py_XDECREF(grid);\n"
        "        Py_XDECREF(row);\n"
        "        return NULL;\n"
        "    }\n"
        "    long *grid_data = (long *)PyArray_DATA((PyArrayObject *)grid);\n"
        "    long *row_data = (long *)PyArray_DATA((PyArrayObject *)row);\n"
        "    for (long i = 0; i < 6; i++) grid_data[i] = i + 1;\n"
        "    row_data[0] = 10;\n"
        "    row_data[1] = 20;\n"
        "    row_data[2] = 30;\n"
        "    PyObject *multi_obj = PyArray_MultiIterNew(2, grid, row);\n"
        "    if (multi_obj == NULL) {\n"
        "        Py_DECREF(row);\n"
        "        Py_DECREF(grid);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyArrayMultiIterObject *multi = (PyArrayMultiIterObject *)multi_obj;\n"
        "    if (PyArray_MultiIter_NUMITER(multi) == 2 && PyArray_MultiIter_NDIM(multi) == 2 &&\n"
        "        PyArray_MultiIter_SIZE(multi) == 6 && PyArray_MultiIter_DIMS(multi)[0] == 2 &&\n"
        "        PyArray_MultiIter_DIMS(multi)[1] == 3) score += 1;\n"
        "    if (PyArray_MultiIter_INDEX(multi) == 0 && PyArray_MultiIter_NOTDONE(multi) &&\n"
        "        *(long *)PyArray_MultiIter_DATA(multi, 0) == 1 &&\n"
        "        *(long *)PyArray_MultiIter_DATA(multi, 1) == 10) score += 10;\n"
        "    PyArray_MultiIter_NEXT(multi);\n"
        "    if (PyArray_MultiIter_INDEX(multi) == 1 &&\n"
        "        *(long *)PyArray_MultiIter_DATA(multi, 0) == 2 &&\n"
        "        *(long *)PyArray_MultiIter_DATA(multi, 1) == 20) score += 100;\n"
        "    PyArray_MultiIter_GOTO1D(multi, 3);\n"
        "    if (PyArray_MultiIter_INDEX(multi) == 3 &&\n"
        "        *(long *)PyArray_MultiIter_DATA(multi, 0) == 4 &&\n"
        "        *(long *)PyArray_MultiIter_DATA(multi, 1) == 10) score += 1000;\n"
        "    PyArray_MultiIter_RESET(multi);\n"
        "    if (PyArray_MultiIter_INDEX(multi) == 0 &&\n"
        "        *(long *)PyArray_MultiIter_DATA(multi, 0) == 1 &&\n"
        "        *(long *)PyArray_MultiIter_DATA(multi, 1) == 10) score += 10000;\n"
        "    Py_DECREF(multi_obj);\n"
        "    npy_intp bad_dims[1] = {4};\n"
        "    PyObject *bad = PyArray_SimpleNew(1, bad_dims, NPY_LONG);\n"
        "    if (bad == NULL) {\n"
        "        Py_DECREF(row);\n"
        "        Py_DECREF(grid);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *bad_multi = PyArray_MultiIterNew(2, grid, bad);\n"
        "    if (bad_multi == NULL && PyErr_Occurred() != NULL) {\n"
        "        PyErr_Clear();\n"
        "        score += 100000;\n"
        "    }\n"
        "    Py_XDECREF(bad_multi);\n"
        "    Py_DECREF(bad);\n"
        "    Py_DECREF(row);\n"
        "    Py_DECREF(grid);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_iter_all_but_axis_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims2x3[2] = {2, 3};\n"
        "    PyObject *arr = PyArray_SimpleNew(2, dims2x3, NPY_LONG);\n"
        "    if (arr == NULL) return NULL;\n"
        "    long *data = (long *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    for (long i = 0; i < 6; i++) data[i] = i + 1;\n"
        "    int axis = 1;\n"
        "    PyObject *iter_obj = PyArray_IterAllButAxis(arr, &axis);\n"
        "    if (iter_obj != NULL) {\n"
        "        PyArrayIterObject *it = (PyArrayIterObject *)iter_obj;\n"
        "        if (axis == 1 && it->size == 2 && it->dims_m1[1] == 0 &&\n"
        "            it->backstrides[1] == 0 && *(long *)PyArray_ITER_DATA(it) == 1) {\n"
        "            PyArray_ITER_NEXT(it);\n"
        "            if (it->index == 1 && *(long *)PyArray_ITER_DATA(it) == 4) score += 1;\n"
        "        }\n"
        "    }\n"
        "    Py_XDECREF(iter_obj);\n"
        "    axis = -1;\n"
        "    iter_obj = PyArray_IterAllButAxis(arr, &axis);\n"
        "    if (iter_obj != NULL) {\n"
        "        PyArrayIterObject *it = (PyArrayIterObject *)iter_obj;\n"
        "        if (axis == 1 && it->size == 2 && *(long *)PyArray_ITER_DATA(it) == 1) {\n"
        "            PyArray_ITER_NEXT(it);\n"
        "            if (*(long *)PyArray_ITER_DATA(it) == 4) score += 10;\n"
        "        }\n"
        "    }\n"
        "    Py_XDECREF(iter_obj);\n"
        "    axis = 0;\n"
        "    iter_obj = PyArray_IterAllButAxis(arr, &axis);\n"
        "    if (iter_obj != NULL) {\n"
        "        PyArrayIterObject *it = (PyArrayIterObject *)iter_obj;\n"
        "        if (axis == 0 && it->size == 3 && it->dims_m1[0] == 0 &&\n"
        "            *(long *)PyArray_ITER_DATA(it) == 1) {\n"
        "            PyArray_ITER_NEXT(it);\n"
        "            if (*(long *)PyArray_ITER_DATA(it) == 2) score += 100;\n"
        "        }\n"
        "    }\n"
        "    Py_XDECREF(iter_obj);\n"
        "    axis = 2;\n"
        "    iter_obj = PyArray_IterAllButAxis(arr, &axis);\n"
        "    if (iter_obj == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000; }\n"
        "    Py_XDECREF(iter_obj);\n"
        "    iter_obj = PyArray_IterAllButAxis(arr, NULL);\n"
        "    if (iter_obj == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    Py_XDECREF(iter_obj);\n"
        "    PyObject *scalar = PyArray_SimpleNew(0, NULL, NPY_LONG);\n"
        "    if (scalar == NULL) { Py_DECREF(arr); return NULL; }\n"
        "    long *scalar_data = (long *)PyArray_DATA((PyArrayObject *)scalar);\n"
        "    *scalar_data = 99;\n"
        "    axis = -1;\n"
        "    iter_obj = PyArray_IterAllButAxis(scalar, &axis);\n"
        "    if (iter_obj != NULL) {\n"
        "        PyArrayIterObject *it = (PyArrayIterObject *)iter_obj;\n"
        "        if (it->size == 1 && it->nd_m1 == -1 && *(long *)PyArray_ITER_DATA(it) == 99) score += 100000;\n"
        "    }\n"
        "    Py_XDECREF(iter_obj);\n"
        "    Py_DECREF(scalar);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_list_pointer_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims3[3] = {2, 3, 4};\n"
        "    int idims3[3] = {2, 3, 4};\n"
        "    if (PyArray_MultiplyList(dims3, 3) == 24) score += 1;\n"
        "    if (PyArray_MultiplyIntList(idims3, 3) == 24) score += 10;\n"
        "    PyObject *tuple = PyArray_IntTupleFromIntp(3, dims3);\n"
        "    if (tuple != NULL && PyTuple_Size(tuple) == 3 &&\n"
        "        PyLong_AsLong(PyTuple_GetItem(tuple, 0)) == 2 &&\n"
        "        PyLong_AsLong(PyTuple_GetItem(tuple, 1)) == 3 &&\n"
        "        PyLong_AsLong(PyTuple_GetItem(tuple, 2)) == 4) score += 1000;\n"
        "    Py_XDECREF(tuple);\n"
        "    PyObject *empty_tuple = PyArray_IntTupleFromIntp(0, dims3);\n"
        "    if (empty_tuple != NULL && PyTuple_Size(empty_tuple) == 0) score += 10000;\n"
        "    Py_XDECREF(empty_tuple);\n"
        "    npy_intp dims2[2] = {2, 3};\n"
        "    PyObject *arr = PyArray_SimpleNew(2, dims2, NPY_LONG);\n"
        "    if (arr == NULL) return NULL;\n"
        "    long *data = (long *)PyArray_DATA((PyArrayObject *)arr);\n"
        "    data[0] = 10; data[1] = 20; data[2] = 30; data[3] = 40; data[4] = 50; data[5] = 60;\n"
        "    npy_intp ind[2] = {1, 2};\n"
        "    long *ptr = (long *)PyArray_GetPtr((PyArrayObject *)arr, ind);\n"
        "    if (ptr == &data[5] && *ptr == 60) score += 100;\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_overflow_multiply_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims3[3] = {2, 3, 4};\n"
        "    if (PyArray_OverflowMultiplyList(dims3, 3) == 24) score += 1;\n"
        "    npy_intp max_intp = (npy_intp)(((size_t)-1) >> 1);\n"
        "    npy_intp zero_first[3] = {2, 0, max_intp};\n"
        "    if (PyArray_OverflowMultiplyList(zero_first, 3) == 0) score += 10;\n"
        "    npy_intp overflow[2] = {max_intp, 2};\n"
        "    if (PyArray_OverflowMultiplyList(overflow, 2) == -1) score += 100;\n"
        "    if (PyArray_OverflowMultiplyList(dims3, 0) == 1) score += 1000;\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_endianness_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    const union { uint32_t i; unsigned char c[4]; } marker = {0x01020304u};\n"
        "    int expected = NPY_CPU_UNKNOWN_ENDIAN;\n"
        "    if (marker.c[0] == 1) expected = NPY_CPU_BIG;\n"
        "    else if (marker.c[0] == 4) expected = NPY_CPU_LITTLE;\n"
        "    int got = PyArray_GetEndianness();\n"
        "    if (got == expected) score += 1;\n"
        "    if (got == NPY_CPU_LITTLE || got == NPY_CPU_BIG || got == NPY_CPU_UNKNOWN_ENDIAN) score += 10;\n"
        "    if (expected != NPY_CPU_UNKNOWN_ENDIAN && got != NPY_CPU_UNKNOWN_ENDIAN) score += 100;\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_feature_version_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    unsigned int got = PyArray_GetNDArrayCFeatureVersion();\n"
        "    if (got == (unsigned int)NPY_API_VERSION) score += 1;\n"
        "    if (got >= (unsigned int)NPY_1_7_API_VERSION) score += 10;\n"
        "    if (got <= (unsigned int)NPY_2_4_API_VERSION) score += 100;\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_check_axis_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims2[2] = {2, 3};\n"
        "    PyObject *arr = PyArray_SimpleNew(2, dims2, NPY_INT);\n"
        "    if (arr == NULL) return NULL;\n"
        "    int axis = 1;\n"
        "    PyObject *checked = PyArray_CheckAxis((PyArrayObject *)arr, &axis, 0);\n"
        "    if (checked == arr && axis == 1) score += 1;\n"
        "    Py_XDECREF(checked);\n"
        "    axis = -1;\n"
        "    checked = PyArray_CheckAxis((PyArrayObject *)arr, &axis, 0);\n"
        "    if (checked == arr && axis == 1) score += 10;\n"
        "    Py_XDECREF(checked);\n"
        "    axis = 0;\n"
        "    checked = PyArray_CheckAxis((PyArrayObject *)arr, &axis, NPY_ARRAY_CARRAY);\n"
        "    if (checked == arr && axis == 0) score += 100;\n"
        "    Py_XDECREF(checked);\n"
        "    axis = 2;\n"
        "    checked = PyArray_CheckAxis((PyArrayObject *)arr, &axis, 0);\n"
        "    if (checked == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000; }\n"
        "    Py_XDECREF(checked);\n"
        "    axis = NPY_RAVEL_AXIS;\n"
        "    checked = PyArray_CheckAxis((PyArrayObject *)arr, &axis, 0);\n"
        "    if (checked != NULL && checked != arr && axis == 0 &&\n"
        "        PyArray_NDIM((PyArrayObject *)checked) == 1 &&\n"
        "        PyArray_SIZE((PyArrayObject *)checked) == 6 &&\n"
        "        PyArray_BASE((PyArrayObject *)checked) == arr) score += 10000;\n"
        "    Py_XDECREF(checked);\n"
        "    PyObject *scalar = PyArray_SimpleNew(0, NULL, NPY_INT);\n"
        "    if (scalar == NULL) { Py_DECREF(arr); return NULL; }\n"
        "    axis = 0;\n"
        "    checked = PyArray_CheckAxis((PyArrayObject *)scalar, &axis, 0);\n"
        "    if (checked != NULL && axis == 0 && PyArray_NDIM((PyArrayObject *)checked) == 1 &&\n"
        "        PyArray_SIZE((PyArrayObject *)checked) == 1) score += 100000;\n"
        "    Py_XDECREF(checked);\n"
        "    checked = PyArray_CheckAxis((PyArrayObject *)arr, NULL, 0);\n"
        "    if (checked == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000000; }\n"
        "    Py_XDECREF(checked);\n"
        "    Py_DECREF(scalar);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_clipmode_converter_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    NPY_CLIPMODE mode = (NPY_CLIPMODE)-1;\n"
        "    if (PyArray_ClipmodeConverter(Py_None, &mode) == 0 && mode == NPY_RAISE) score += 1;\n"
        '    PyObject *clip = PyUnicode_FromString("clip");\n'
        "    if (clip == NULL) return NULL;\n"
        "    mode = (NPY_CLIPMODE)-1;\n"
        "    if (PyArray_ClipmodeConverter(clip, &mode) == 0 && mode == NPY_CLIP) score += 10;\n"
        "    Py_DECREF(clip);\n"
        '    PyObject *wrap = PyBytes_FromString("wrap");\n'
        "    if (wrap == NULL) return NULL;\n"
        "    mode = (NPY_CLIPMODE)-1;\n"
        "    if (PyArray_ClipmodeConverter(wrap, &mode) == 0 && mode == NPY_WRAP) score += 100;\n"
        "    Py_DECREF(wrap);\n"
        "    PyObject *raise_int = PyLong_FromLong((long)NPY_RAISE);\n"
        "    if (raise_int == NULL) return NULL;\n"
        "    mode = (NPY_CLIPMODE)-1;\n"
        "    if (PyArray_ClipmodeConverter(raise_int, &mode) == 0 && mode == NPY_RAISE) score += 1000;\n"
        "    Py_DECREF(raise_int);\n"
        "    PyObject *clip_int = PyLong_FromLong((long)NPY_CLIP);\n"
        "    if (clip_int == NULL) return NULL;\n"
        "    mode = (NPY_CLIPMODE)-1;\n"
        "    if (PyArray_ClipmodeConverter(clip_int, &mode) == 0 && mode == NPY_CLIP) score += 10000;\n"
        "    Py_DECREF(clip_int);\n"
        '    PyObject *bad_case = PyUnicode_FromString("Clip");\n'
        "    if (bad_case == NULL) return NULL;\n"
        "    if (PyArray_ClipmodeConverter(bad_case, &mode) != 0 && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_DECREF(bad_case);\n"
        "    PyObject *bad_int = PyLong_FromLong(99);\n"
        "    if (bad_int == NULL) return NULL;\n"
        "    if (PyArray_ClipmodeConverter(bad_int, &mode) != 0 && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000000; }\n"
        "    Py_DECREF(bad_int);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_clipmode_sequence_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    NPY_CLIPMODE modes[3] = {(NPY_CLIPMODE)-1, (NPY_CLIPMODE)-1, (NPY_CLIPMODE)-1};\n"
        '    PyObject *wrap = PyUnicode_FromString("wrap");\n'
        "    if (wrap == NULL) return NULL;\n"
        "    if (PyArray_ConvertClipmodeSequence(wrap, modes, 3) == NPY_SUCCEED &&\n"
        "        modes[0] == NPY_WRAP && modes[1] == NPY_WRAP && modes[2] == NPY_WRAP) score += 1;\n"
        "    Py_DECREF(wrap);\n"
        "    PyObject *seq = PyTuple_New(3);\n"
        "    if (seq == NULL) return NULL;\n"
        '    PyObject *clip = PyUnicode_FromString("clip");\n'
        '    PyObject *raise_s = PyBytes_FromString("raise");\n'
        "    PyObject *wrap_i = PyLong_FromLong((long)NPY_WRAP);\n"
        "    if (clip == NULL || raise_s == NULL || wrap_i == NULL) { Py_XDECREF(clip); Py_XDECREF(raise_s); Py_XDECREF(wrap_i); Py_DECREF(seq); return NULL; }\n"
        "    PyTuple_SET_ITEM(seq, 0, clip);\n"
        "    PyTuple_SET_ITEM(seq, 1, raise_s);\n"
        "    PyTuple_SET_ITEM(seq, 2, wrap_i);\n"
        "    modes[0] = modes[1] = modes[2] = (NPY_CLIPMODE)-1;\n"
        "    if (PyArray_ConvertClipmodeSequence(seq, modes, 3) == NPY_SUCCEED &&\n"
        "        modes[0] == NPY_CLIP && modes[1] == NPY_RAISE && modes[2] == NPY_WRAP) score += 10;\n"
        "    Py_DECREF(seq);\n"
        "    modes[0] = modes[1] = (NPY_CLIPMODE)-1;\n"
        "    if (PyArray_ConvertClipmodeSequence(NULL, modes, 2) == NPY_SUCCEED &&\n"
        "        modes[0] == NPY_RAISE && modes[1] == NPY_RAISE) score += 100;\n"
        "    PyObject *bad_len = PyTuple_New(1);\n"
        "    if (bad_len == NULL) return NULL;\n"
        '    PyObject *clip2 = PyUnicode_FromString("clip");\n'
        "    if (clip2 == NULL) { Py_DECREF(bad_len); return NULL; }\n"
        "    PyTuple_SET_ITEM(bad_len, 0, clip2);\n"
        "    if (PyArray_ConvertClipmodeSequence(bad_len, modes, 2) != NPY_SUCCEED && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000; }\n"
        "    Py_DECREF(bad_len);\n"
        "    PyObject *bad_item = PyTuple_New(1);\n"
        "    if (bad_item == NULL) return NULL;\n"
        '    PyObject *case_bad = PyUnicode_FromString("Clip");\n'
        "    if (case_bad == NULL) { Py_DECREF(bad_item); return NULL; }\n"
        "    PyTuple_SET_ITEM(bad_item, 0, case_bad);\n"
        "    if (PyArray_ConvertClipmodeSequence(bad_item, modes, 1) != NPY_SUCCEED && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    Py_DECREF(bad_item);\n"
        "    if (PyArray_ConvertClipmodeSequence(Py_None, NULL, 2) != NPY_SUCCEED && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_output_converter_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    PyArrayObject *out = (PyArrayObject *)0x1;\n"
        "    if (PyArray_OutputConverter(Py_None, &out) == 0 && out == NULL) score += 1;\n"
        "    npy_intp dims[1] = {2};\n"
        "    PyObject *arr = PyArray_SimpleNew(1, dims, NPY_LONG);\n"
        "    if (arr == NULL) return NULL;\n"
        "    out = NULL;\n"
        "    if (PyArray_OutputConverter(arr, &out) == 0 && out == (PyArrayObject *)arr) score += 10;\n"
        "    PyObject *bad = PyLong_FromLong(7);\n"
        "    if (bad == NULL) { Py_DECREF(arr); return NULL; }\n"
        "    out = (PyArrayObject *)0x1;\n"
        "    if (PyArray_OutputConverter(bad, &out) != 0 && out == NULL && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100; }\n"
        "    Py_DECREF(bad);\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_searchside_converter_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    NPY_SEARCHSIDE side = (NPY_SEARCHSIDE)-1;\n"
        '    PyObject *left = PyUnicode_FromString("left");\n'
        "    if (left == NULL) return NULL;\n"
        "    if (PyArray_SearchsideConverter(left, &side) == 0 && side == NPY_SEARCHLEFT) score += 1;\n"
        "    Py_DECREF(left);\n"
        "    side = (NPY_SEARCHSIDE)-1;\n"
        '    PyObject *right = PyBytes_FromString("right");\n'
        "    if (right == NULL) return NULL;\n"
        "    if (PyArray_SearchsideConverter(right, &side) == 0 && side == NPY_SEARCHRIGHT) score += 10;\n"
        "    Py_DECREF(right);\n"
        '    PyObject *bad_case = PyUnicode_FromString("Left");\n'
        "    if (bad_case == NULL) return NULL;\n"
        "    if (PyArray_SearchsideConverter(bad_case, &side) != 0 && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100; }\n"
        "    Py_DECREF(bad_case);\n"
        '    PyObject *bad = PyUnicode_FromString("middle");\n'
        "    if (bad == NULL) return NULL;\n"
        "    if (PyArray_SearchsideConverter(bad, &side) != 0 && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000; }\n"
        "    Py_DECREF(bad);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_version_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    unsigned int abi = PyArray_GetNDArrayCVersion();\n"
        "    unsigned int feature = PyArray_GetNDArrayCFeatureVersion();\n"
        "    long score = 0;\n"
        "    if (abi == (unsigned int)NPY_VERSION) score += 1;\n"
        "    if (abi == (unsigned int)NPY_ABI_VERSION) score += 10;\n"
        "    if (feature == (unsigned int)NPY_API_VERSION) score += 100;\n"
        "    if (feature >= (unsigned int)NPY_FEATURE_VERSION) score += 1000;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_byteorder_converter_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    char endian = 0;\n"
        '    PyObject *big = PyUnicode_FromString("big");\n'
        "    if (big == NULL) return NULL;\n"
        "    if (PyArray_ByteorderConverter(big, &endian) == NPY_SUCCEED && endian == NPY_BIG) score += 1;\n"
        "    Py_DECREF(big);\n"
        '    PyObject *little = PyBytes_FromString("<");\n'
        "    if (little == NULL) return NULL;\n"
        "    endian = 0;\n"
        "    if (PyArray_ByteorderConverter(little, &endian) == NPY_SUCCEED && endian == NPY_LITTLE) score += 10;\n"
        "    Py_DECREF(little);\n"
        '    PyObject *native = PyUnicode_FromString("=");\n'
        "    if (native == NULL) return NULL;\n"
        "    endian = 0;\n"
        "    if (PyArray_ByteorderConverter(native, &endian) == NPY_SUCCEED && endian == NPY_NATIVE) score += 100;\n"
        "    Py_DECREF(native);\n"
        '    PyObject *ignore = PyUnicode_FromString("ignore");\n'
        "    if (ignore == NULL) return NULL;\n"
        "    endian = 0;\n"
        "    if (PyArray_ByteorderConverter(ignore, &endian) == NPY_SUCCEED && endian == NPY_IGNORE) score += 1000;\n"
        "    Py_DECREF(ignore);\n"
        '    PyObject *pipe = PyBytes_FromString("|");\n'
        "    if (pipe == NULL) return NULL;\n"
        "    endian = 0;\n"
        "    if (PyArray_ByteorderConverter(pipe, &endian) == NPY_SUCCEED && endian == NPY_IGNORE) score += 10000;\n"
        "    Py_DECREF(pipe);\n"
        '    PyObject *swap = PyUnicode_FromString("swap");\n'
        "    if (swap == NULL) return NULL;\n"
        "    endian = 0;\n"
        "    if (PyArray_ByteorderConverter(swap, &endian) == NPY_SUCCEED && endian == NPY_SWAP) score += 100000;\n"
        "    Py_DECREF(swap);\n"
        '    PyObject *bad = PyUnicode_FromString("middle");\n'
        "    if (bad == NULL) return NULL;\n"
        "    if (PyArray_ByteorderConverter(bad, &endian) != NPY_SUCCEED && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000000; }\n"
        "    Py_DECREF(bad);\n"
        "    if (PyArray_ByteorderConverter(Py_None, NULL) != NPY_SUCCEED && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000000; }\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_sortkind_converter_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    NPY_SORTKIND kind = NPY_HEAPSORT;\n"
        "    if (PyArray_SortkindConverter(Py_None, &kind) == NPY_SUCCEED && kind == NPY_HEAPSORT) score += 1;\n"
        '    PyObject *quick = PyUnicode_FromString("quicksort");\n'
        "    if (quick == NULL) return NULL;\n"
        "    kind = (NPY_SORTKIND)-99;\n"
        "    if (PyArray_SortkindConverter(quick, &kind) == NPY_SUCCEED && kind == NPY_QUICKSORT) score += 10;\n"
        "    Py_DECREF(quick);\n"
        '    PyObject *heap = PyBytes_FromString("heapsort");\n'
        "    if (heap == NULL) return NULL;\n"
        "    kind = (NPY_SORTKIND)-99;\n"
        "    if (PyArray_SortkindConverter(heap, &kind) == NPY_SUCCEED && kind == NPY_HEAPSORT) score += 100;\n"
        "    Py_DECREF(heap);\n"
        '    PyObject *merge = PyUnicode_FromString("mergesort");\n'
        "    if (merge == NULL) return NULL;\n"
        "    kind = (NPY_SORTKIND)-99;\n"
        "    if (PyArray_SortkindConverter(merge, &kind) == NPY_SUCCEED && kind == NPY_MERGESORT) score += 1000;\n"
        "    Py_DECREF(merge);\n"
        '    PyObject *stable = PyBytes_FromString("stable");\n'
        "    if (stable == NULL) return NULL;\n"
        "    kind = (NPY_SORTKIND)-99;\n"
        "    if (PyArray_SortkindConverter(stable, &kind) == NPY_SUCCEED && kind == NPY_STABLESORT) score += 10000;\n"
        "    Py_DECREF(stable);\n"
        '    PyObject *bad = PyUnicode_FromString("radix");\n'
        "    if (bad == NULL) return NULL;\n"
        "    if (PyArray_SortkindConverter(bad, &kind) != NPY_SUCCEED && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_DECREF(bad);\n"
        "    PyObject *bad_type = PyLong_FromLong(1);\n"
        "    if (bad_type == NULL) return NULL;\n"
        "    if (PyArray_SortkindConverter(bad_type, &kind) != NPY_SUCCEED && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000000; }\n"
        "    Py_DECREF(bad_type);\n"
        "    if (PyArray_SortkindConverter(Py_None, NULL) != NPY_SUCCEED && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000000; }\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_selectkind_converter_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    NPY_SELECTKIND kind = (NPY_SELECTKIND)-99;\n"
        '    PyObject *intro = PyUnicode_FromString("introselect");\n'
        "    if (intro == NULL) return NULL;\n"
        "    if (PyArray_SelectkindConverter(intro, &kind) == NPY_SUCCEED && kind == NPY_INTROSELECT) score += 1;\n"
        "    Py_DECREF(intro);\n"
        '    PyObject *intro_bytes = PyBytes_FromString("introselect");\n'
        "    if (intro_bytes == NULL) return NULL;\n"
        "    kind = (NPY_SELECTKIND)-99;\n"
        "    if (PyArray_SelectkindConverter(intro_bytes, &kind) == NPY_SUCCEED && kind == NPY_INTROSELECT) score += 10;\n"
        "    Py_DECREF(intro_bytes);\n"
        '    PyObject *bad_case = PyUnicode_FromString("Introselect");\n'
        "    if (bad_case == NULL) return NULL;\n"
        "    if (PyArray_SelectkindConverter(bad_case, &kind) != NPY_SUCCEED && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100; }\n"
        "    Py_DECREF(bad_case);\n"
        '    PyObject *bad_value = PyUnicode_FromString("intro");\n'
        "    if (bad_value == NULL) return NULL;\n"
        "    if (PyArray_SelectkindConverter(bad_value, &kind) != NPY_SUCCEED && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000; }\n"
        "    Py_DECREF(bad_value);\n"
        "    PyObject *bad_type = PyLong_FromLong(1);\n"
        "    if (bad_type == NULL) return NULL;\n"
        "    if (PyArray_SelectkindConverter(bad_type, &kind) != NPY_SUCCEED && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    Py_DECREF(bad_type);\n"
        "    if (PyArray_SelectkindConverter(Py_None, NULL) != NPY_SUCCEED && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_order_converter_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    NPY_ORDER order = NPY_KEEPORDER;\n"
        "    if (PyArray_OrderConverter(Py_None, &order) == NPY_SUCCEED && order == NPY_KEEPORDER) score += 1;\n"
        '    PyObject *c_order = PyUnicode_FromString("C");\n'
        "    if (c_order == NULL) return NULL;\n"
        "    order = (NPY_ORDER)-99;\n"
        "    if (PyArray_OrderConverter(c_order, &order) == NPY_SUCCEED && order == NPY_CORDER) score += 10;\n"
        "    Py_DECREF(c_order);\n"
        '    PyObject *f_order = PyBytes_FromString("f");\n'
        "    if (f_order == NULL) return NULL;\n"
        "    order = (NPY_ORDER)-99;\n"
        "    if (PyArray_OrderConverter(f_order, &order) == NPY_SUCCEED && order == NPY_FORTRANORDER) score += 100;\n"
        "    Py_DECREF(f_order);\n"
        '    PyObject *a_order = PyUnicode_FromString("A");\n'
        "    if (a_order == NULL) return NULL;\n"
        "    order = (NPY_ORDER)-99;\n"
        "    if (PyArray_OrderConverter(a_order, &order) == NPY_SUCCEED && order == NPY_ANYORDER) score += 1000;\n"
        "    Py_DECREF(a_order);\n"
        '    PyObject *k_order = PyBytes_FromString("k");\n'
        "    if (k_order == NULL) return NULL;\n"
        "    order = (NPY_ORDER)-99;\n"
        "    if (PyArray_OrderConverter(k_order, &order) == NPY_SUCCEED && order == NPY_KEEPORDER) score += 10000;\n"
        "    Py_DECREF(k_order);\n"
        '    PyObject *bad_len = PyUnicode_FromString("CF");\n'
        "    if (bad_len == NULL) return NULL;\n"
        "    if (PyArray_OrderConverter(bad_len, &order) != NPY_SUCCEED && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    Py_DECREF(bad_len);\n"
        '    PyObject *bad_value = PyUnicode_FromString("Z");\n'
        "    if (bad_value == NULL) return NULL;\n"
        "    if (PyArray_OrderConverter(bad_value, &order) != NPY_SUCCEED && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000000; }\n"
        "    Py_DECREF(bad_value);\n"
        "    PyObject *bad_type = PyLong_FromLong(1);\n"
        "    if (bad_type == NULL) return NULL;\n"
        "    if (PyArray_OrderConverter(bad_type, &order) != NPY_SUCCEED && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000000; }\n"
        "    Py_DECREF(bad_type);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_bool_converter_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_bool value = 0;\n"
        "    if (PyArray_BoolConverter(Py_True, &value) == NPY_SUCCEED && value == 1) score += 1;\n"
        "    value = 1;\n"
        "    if (PyArray_BoolConverter(Py_False, &value) == NPY_SUCCEED && value == 0) score += 10;\n"
        "    value = 1;\n"
        "    if (PyArray_BoolConverter(Py_None, &value) == NPY_SUCCEED && value == 0) score += 100;\n"
        "    PyObject *empty = PyTuple_New(0);\n"
        "    if (empty == NULL) return NULL;\n"
        "    value = 1;\n"
        "    if (PyArray_BoolConverter(empty, &value) == NPY_SUCCEED && value == 0) score += 1000;\n"
        "    Py_DECREF(empty);\n"
        "    PyObject *full = PyTuple_New(1);\n"
        "    if (full == NULL) return NULL;\n"
        "    if (PyTuple_SetItem(full, 0, PyLong_FromLong(7)) != 0) { Py_DECREF(full); return NULL; }\n"
        "    value = 0;\n"
        "    if (PyArray_BoolConverter(full, &value) == NPY_SUCCEED && value == 1) score += 10000;\n"
        "    Py_DECREF(full);\n"
        "    if (PyArray_BoolConverter(Py_True, NULL) != NPY_SUCCEED && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_optional_bool_converter_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    int value = 7;\n"
        "    if (PyArray_OptionalBoolConverter(Py_None, &value) == NPY_SUCCEED && value == 7) score += 1;\n"
        "    value = 0;\n"
        "    if (PyArray_OptionalBoolConverter(Py_True, &value) == NPY_SUCCEED && value == 1) score += 10;\n"
        "    value = 1;\n"
        "    if (PyArray_OptionalBoolConverter(Py_False, &value) == NPY_SUCCEED && value == 0) score += 100;\n"
        "    PyObject *empty = PyTuple_New(0);\n"
        "    if (empty == NULL) return NULL;\n"
        "    value = 1;\n"
        "    if (PyArray_OptionalBoolConverter(empty, &value) == NPY_SUCCEED && value == 0) score += 1000;\n"
        "    Py_DECREF(empty);\n"
        "    PyObject *full = PyTuple_New(1);\n"
        "    if (full == NULL) return NULL;\n"
        "    PyObject *item = PyLong_FromLong(1);\n"
        "    if (item == NULL) { Py_DECREF(full); return NULL; }\n"
        "    PyTuple_SET_ITEM(full, 0, item);\n"
        "    value = 0;\n"
        "    if (PyArray_OptionalBoolConverter(full, &value) == NPY_SUCCEED && value == 1) score += 10000;\n"
        "    Py_DECREF(full);\n"
        "    if (PyArray_OptionalBoolConverter(Py_True, NULL) != NPY_SUCCEED && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 100000; }\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_axis_converter_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    int axis = 99;\n"
        "    if (PyArray_AxisConverter(Py_None, &axis) == NPY_SUCCEED && axis == NPY_RAVEL_AXIS) score += 1;\n"
        "    PyObject *two = PyLong_FromLong(2);\n"
        "    if (two == NULL) return NULL;\n"
        "    axis = 99;\n"
        "    if (PyArray_AxisConverter(two, &axis) == NPY_SUCCEED && axis == 2) score += 10;\n"
        "    Py_DECREF(two);\n"
        "    PyObject *neg = PyLong_FromLong(-1);\n"
        "    if (neg == NULL) return NULL;\n"
        "    axis = 99;\n"
        "    if (PyArray_AxisConverter(neg, &axis) == NPY_SUCCEED && axis == -1) score += 100;\n"
        "    Py_DECREF(neg);\n"
        '    PyObject *bad = PyUnicode_FromString("axis");\n'
        "    if (bad == NULL) return NULL;\n"
        "    if (PyArray_AxisConverter(bad, &axis) != NPY_SUCCEED && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 1000; }\n"
        "    Py_DECREF(bad);\n"
        "    if (PyArray_AxisConverter(Py_None, NULL) != NPY_SUCCEED && PyErr_Occurred() != NULL) { PyErr_Clear(); score += 10000; }\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_element_strides_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    npy_intp dims[2] = {2, 3};\n"
        "    PyObject *arr = PyArray_SimpleNew(2, dims, NPY_LONG);\n"
        "    if (arr == NULL) return NULL;\n"
        "    long score = 0;\n"
        "    if (PyArray_ElementStrides(arr)) score += 1;\n"
        "    npy_intp *strides = PyArray_STRIDES((PyArrayObject *)arr);\n"
        "    strides[0] = (npy_intp)PyArray_ITEMSIZE((PyArrayObject *)arr) + 1;\n"
        "    if (!PyArray_ElementStrides(arr)) score += 10;\n"
        "    if (!PyArray_ElementStrides(Py_None)) score += 100;\n"
        "    Py_DECREF(arr);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_valid_type_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    if (PyArray_ValidType(NPY_INT)) score += 1;\n"
        "    if (PyArray_ValidType(NPY_DOUBLE)) score += 10;\n"
        "    if (!PyArray_ValidType(9999) && PyErr_Occurred() == NULL) score += 100;\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_item_refcount_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    PyArray_Descr *obj_descr = PyArray_DescrFromType(NPY_OBJECT);\n"
        "    PyArray_Descr *int_descr = PyArray_DescrFromType(NPY_INT);\n"
        '    PyObject *value = PyUnicode_FromString("held");\n'
        "    if (obj_descr == NULL || int_descr == NULL || value == NULL) return NULL;\n"
        "    Py_ssize_t before = Py_REFCNT(value);\n"
        "    PyObject *slot = value;\n"
        "    long score = 0;\n"
        "    PyArray_Item_INCREF((char *)&slot, obj_descr);\n"
        "    if (Py_REFCNT(value) == before + 1) score += 1;\n"
        "    Py_DECREF(value);\n"
        "    const char *held = PyUnicode_AsUTF8(slot);\n"
        '    if (held != NULL && strcmp(held, "held") == 0) score += 10;\n'
        "    PyArray_Item_XDECREF((char *)&slot, obj_descr);\n"
        "    slot = NULL;\n"
        "    PyArray_Item_INCREF((char *)&slot, obj_descr);\n"
        "    PyArray_Item_XDECREF((char *)&slot, obj_descr);\n"
        "    if (PyErr_Occurred() == NULL) score += 100;\n"
        "    int raw = 7;\n"
        "    PyArray_Item_INCREF((char *)&raw, int_descr);\n"
        "    PyArray_Item_XDECREF((char *)&raw, int_descr);\n"
        "    if (raw == 7 && PyErr_Occurred() == NULL) score += 1000;\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_array_refcount_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    npy_intp dims[1] = {2};\n"
        "    PyObject *arr_obj = PyArray_SimpleNew(1, dims, NPY_OBJECT);\n"
        "    PyObject *int_arr = PyArray_SimpleNew(1, dims, NPY_INT);\n"
        '    PyObject *left = PyUnicode_FromString("array-inc-left");\n'
        '    PyObject *right = PyUnicode_FromString("array-inc-right");\n'
        "    if (arr_obj == NULL || int_arr == NULL || left == NULL || right == NULL) return NULL;\n"
        "    PyObject **items = (PyObject **)PyArray_DATA((PyArrayObject *)arr_obj);\n"
        "    if (items == NULL) return NULL;\n"
        "    items[0] = left;\n"
        "    items[1] = right;\n"
        "    Py_ssize_t left_before = Py_REFCNT(left);\n"
        "    Py_ssize_t right_before = Py_REFCNT(right);\n"
        "    long score = 0;\n"
        "    if (PyArray_INCREF((PyArrayObject *)arr_obj) == 0 && Py_REFCNT(left) == left_before + 1 && Py_REFCNT(right) == right_before + 1) score += 1;\n"
        "    Py_DECREF(left);\n"
        "    Py_DECREF(right);\n"
        "    const char *left_text = PyUnicode_AsUTF8(items[0]);\n"
        "    const char *right_text = PyUnicode_AsUTF8(items[1]);\n"
        '    if (left_text != NULL && right_text != NULL && strcmp(left_text, "array-inc-left") == 0 && strcmp(right_text, "array-inc-right") == 0) score += 10;\n'
        "    if (PyArray_XDECREF((PyArrayObject *)arr_obj) == 0 && PyErr_Occurred() == NULL) score += 100;\n"
        "    items[0] = NULL;\n"
        "    items[1] = NULL;\n"
        "    if (PyArray_INCREF((PyArrayObject *)int_arr) == 0 && PyArray_XDECREF((PyArrayObject *)int_arr) == 0 && PyErr_Occurred() == NULL) score += 1000;\n"
        "    Py_DECREF(int_arr);\n"
        "    Py_DECREF(arr_obj);\n"
        "    if (PyErr_Occurred() != NULL) return NULL;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_allocator_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    char *buf = (char *)PyArray_malloc(3);\n"
        "    if (buf == NULL) return PyErr_NoMemory();\n"
        "    buf[0] = 'a'; buf[1] = 'b'; buf[2] = '\\0';\n"
        "    char *grown = (char *)PyArray_realloc(buf, 5);\n"
        "    if (grown == NULL) {\n"
        "        PyArray_free(buf);\n"
        "        return PyErr_NoMemory();\n"
        "    }\n"
        "    buf = grown;\n"
        "    buf[2] = 'c'; buf[3] = 'd'; buf[4] = '\\0';\n"
        "    if (buf[0] == 'a' && buf[1] == 'b' && buf[3] == 'd') score += 1;\n"
        "    PyArray_free(buf);\n"
        "    npy_intp *dims = PyDimMem_NEW(2);\n"
        "    if (dims == NULL) return PyErr_NoMemory();\n"
        "    dims[0] = 3; dims[1] = 4;\n"
        "    npy_intp *grown_dims = PyDimMem_RENEW(dims, 3);\n"
        "    if (grown_dims == NULL) {\n"
        "        PyDimMem_FREE(dims);\n"
        "        return PyErr_NoMemory();\n"
        "    }\n"
        "    dims = grown_dims;\n"
        "    dims[2] = 5;\n"
        "    if (dims[0] == 3 && dims[1] == 4 && dims[2] == 5) score += 10;\n"
        "    PyDimMem_FREE(dims);\n"
        "    char *from_null = (char *)PyArray_realloc(NULL, 2);\n"
        "    if (from_null == NULL) return PyErr_NoMemory();\n"
        "    from_null[0] = 'z'; from_null[1] = '\\0';\n"
        "    if (from_null[0] == 'z') score += 100;\n"
        "    PyArray_free(from_null);\n"
        "    PyArray_free(NULL);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_datamem_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    char *buf = (char *)PyDataMem_NEW(3);\n"
        "    if (buf == NULL) return PyErr_NoMemory();\n"
        "    buf[0] = 'x'; buf[1] = 'y'; buf[2] = '\\0';\n"
        "    if (buf[0] == 'x' && buf[1] == 'y') score += 1;\n"
        "    char *grown = (char *)PyDataMem_RENEW(buf, 5);\n"
        "    if (grown == NULL) {\n"
        "        PyDataMem_FREE(buf);\n"
        "        return PyErr_NoMemory();\n"
        "    }\n"
        "    buf = grown;\n"
        "    if (buf[0] == 'x' && buf[1] == 'y') score += 10;\n"
        "    buf[3] = 'z';\n"
        "    if (buf[3] == 'z') score += 100;\n"
        "    PyDataMem_FREE(buf);\n"
        "    PyDataMem_FREE(NULL);\n"
        "    unsigned char *zeroed = (unsigned char *)PyDataMem_NEW_ZEROED(4, sizeof(unsigned char));\n"
        "    if (zeroed == NULL) return PyErr_NoMemory();\n"
        "    if (zeroed[0] == 0 && zeroed[1] == 0 && zeroed[2] == 0 && zeroed[3] == 0) score += 1000;\n"
        "    PyDataMem_FREE(zeroed);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_datamem_user_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    PyObject *handler = PyDataMem_GetHandler();\n"
        "    if (handler != NULL) score += 1;\n"
        "    char *buf = (char *)PyDataMem_UserNEW(3, handler);\n"
        "    if (buf == NULL) return PyErr_NoMemory();\n"
        "    buf[0] = 'u'; buf[1] = 'v'; buf[2] = '\\0';\n"
        "    if (buf[0] == 'u' && buf[1] == 'v') score += 10;\n"
        "    char *grown = (char *)PyDataMem_UserRENEW(buf, 5, handler);\n"
        "    if (grown == NULL) {\n"
        "        PyDataMem_UserFREE(buf, 3, handler);\n"
        "        return PyErr_NoMemory();\n"
        "    }\n"
        "    buf = grown;\n"
        "    if (buf[0] == 'u' && buf[1] == 'v') score += 100;\n"
        "    buf[3] = 'w';\n"
        "    if (buf[3] == 'w') score += 1000;\n"
        "    PyDataMem_UserFREE(buf, 5, handler);\n"
        "    PyDataMem_UserFREE(NULL, 0, handler);\n"
        "    unsigned char *zeroed = (unsigned char *)PyDataMem_UserNEW_ZEROED(4, sizeof(unsigned char), handler);\n"
        "    if (zeroed == NULL) return PyErr_NoMemory();\n"
        "    if (zeroed[0] == 0 && zeroed[1] == 0 && zeroed[2] == 0 && zeroed[3] == 0) score += 10000;\n"
        "    PyDataMem_UserFREE(zeroed, 4, handler);\n"
        "    Py_XDECREF(handler);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_array_free_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims2[2] = {2, 2};\n"
        "    PyObject *arr2 = PyArray_SimpleNew(2, dims2, NPY_LONG);\n"
        "    char **ptr2 = (char **)PyArray_malloc(2 * sizeof(char *));\n"
        "    if (arr2 == NULL || ptr2 == NULL) { Py_XDECREF(arr2); PyArray_free(ptr2); return PyErr_NoMemory(); }\n"
        "    if (PyArray_Free(arr2, ptr2) == 0 && PyErr_Occurred() == NULL) score += 1;\n"
        "    npy_intp dims1[1] = {1};\n"
        "    PyObject *arr1 = PyArray_SimpleNew(1, dims1, NPY_LONG);\n"
        "    if (arr1 == NULL) return NULL;\n"
        "    if (PyArray_Free(arr1, NULL) == 0 && PyErr_Occurred() == NULL) score += 10;\n"
        "    PyObject *arr0 = PyArray_SimpleNew(0, NULL, NPY_LONG);\n"
        "    if (arr0 == NULL) return NULL;\n"
        "    if (PyArray_Free(arr0, NULL) != 0 && PyErr_Occurred() == NULL) score += 100;\n"
        "    Py_DECREF(arr0);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_as_carray_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    PyObject *seq1 = PyTuple_New(3);\n"
        "    if (seq1 == NULL) return NULL;\n"
        "    PyTuple_SetItem(seq1, 0, PyLong_FromLong(4));\n"
        "    PyTuple_SetItem(seq1, 1, PyLong_FromLong(5));\n"
        "    PyTuple_SetItem(seq1, 2, PyLong_FromLong(6));\n"
        "    void *ptr1 = NULL;\n"
        "    npy_intp out_dims1[1] = {-1};\n"
        "    PyArray_Descr *descr1 = PyArray_DescrFromType(NPY_LONG);\n"
        "    if (descr1 == NULL) { Py_DECREF(seq1); return NULL; }\n"
        "    if (PyArray_AsCArray(&seq1, &ptr1, out_dims1, 1, descr1) == 0) {\n"
        "        long *data1 = (long *)ptr1;\n"
        "        if (out_dims1[0] == 3 && data1 != NULL && data1[0] == 4 && data1[2] == 6) score += 1;\n"
        "        PyArray_Free(seq1, ptr1);\n"
        "    } else {\n"
        "        Py_DECREF(seq1);\n"
        "        return NULL;\n"
        "    }\n"
        "    npy_intp dims2[2] = {2, 2};\n"
        "    PyObject *arr2 = PyArray_SimpleNew(2, dims2, NPY_LONG);\n"
        "    if (arr2 == NULL) return NULL;\n"
        "    long *data2 = (long *)PyArray_DATA((PyArrayObject *)arr2);\n"
        "    data2[0] = 10; data2[1] = 11; data2[2] = 12; data2[3] = 13;\n"
        "    PyObject *op2 = arr2;\n"
        "    void *ptr2 = NULL;\n"
        "    npy_intp out_dims2[2] = {-1, -1};\n"
        "    PyArray_Descr *descr2 = PyArray_DescrFromType(NPY_LONG);\n"
        "    if (descr2 == NULL) { Py_DECREF(arr2); return NULL; }\n"
        "    if (PyArray_AsCArray(&op2, &ptr2, out_dims2, 2, descr2) == 0) {\n"
        "        char **rows = (char **)ptr2;\n"
        "        if (out_dims2[0] == 2 && out_dims2[1] == 2 && rows != NULL\n"
        "            && ((long *)rows[0])[0] == 10 && ((long *)rows[1])[1] == 13) score += 10;\n"
        "        PyArray_Free(op2, ptr2);\n"
        "        Py_DECREF(arr2);\n"
        "    } else {\n"
        "        Py_DECREF(arr2);\n"
        "        return NULL;\n"
        "    }\n"
        "    PyObject *bad = PyTuple_New(0);\n"
        "    if (bad == NULL) return NULL;\n"
        "    void *bad_ptr = NULL;\n"
        "    npy_intp bad_dims[1] = {-1};\n"
        "    PyArray_Descr *bad_descr = PyArray_DescrFromType(NPY_LONG);\n"
        "    if (bad_descr == NULL) { Py_DECREF(bad); return NULL; }\n"
        "    if (PyArray_AsCArray(&bad, &bad_ptr, bad_dims, 0, bad_descr) != 0 && PyErr_Occurred() != NULL) {\n"
        "        PyErr_Clear();\n"
        "        score += 100;\n"
        "    }\n"
        "    Py_DECREF(bad);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_fail_writeable_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[1] = {2};\n"
        "    PyObject *obj = PyArray_SimpleNew(1, dims, NPY_LONG);\n"
        "    if (obj == NULL) return NULL;\n"
        "    PyArrayObject *arr = (PyArrayObject *)obj;\n"
        '    if (PyArray_FailUnlessWriteable(arr, "write target") == 0 && PyErr_Occurred() == NULL) score += 1;\n'
        "    PyArray_CLEARFLAGS(arr, NPY_ARRAY_WRITEABLE);\n"
        '    if (PyArray_FailUnlessWriteable(arr, "write target") != 0 && PyErr_Occurred() != NULL) {\n'
        "        PyErr_Clear();\n"
        "        score += 10;\n"
        "    }\n"
        "    PyArray_ENABLEFLAGS(arr, NPY_ARRAY_WRITEABLE);\n"
        "    if (PyArray_FailUnlessWriteable(arr, NULL) == 0 && PyErr_Occurred() == NULL) score += 100;\n"
        "    Py_DECREF(obj);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_writeback_if_copy_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[1] = {3};\n"
        "    PyObject *base_obj = PyArray_SimpleNew(1, dims, NPY_LONG);\n"
        "    if (base_obj == NULL) return NULL;\n"
        "    long *base_data = (long *)PyArray_DATA((PyArrayObject *)base_obj);\n"
        "    base_data[0] = 1;\n"
        "    base_data[1] = 2;\n"
        "    base_data[2] = 3;\n"
        "    PyObject *copy_obj = PyArray_NewCopy((PyArrayObject *)base_obj, NPY_CORDER);\n"
        "    if (copy_obj == NULL) { Py_DECREF(base_obj); return NULL; }\n"
        "    Py_INCREF(base_obj);\n"
        "    if (PyArray_SetWritebackIfCopyBase((PyArrayObject *)copy_obj, (PyArrayObject *)base_obj) == 0) score += 1;\n"
        "    if (PyArray_BASE((PyArrayObject *)copy_obj) == base_obj) score += 10;\n"
        "    if (PyArray_CHKFLAGS((PyArrayObject *)copy_obj, NPY_ARRAY_WRITEBACKIFCOPY)) score += 100;\n"
        "    if (!PyArray_ISWRITEABLE((PyArrayObject *)base_obj)) score += 1000;\n"
        "    ((long *)PyArray_DATA((PyArrayObject *)copy_obj))[1] = 42;\n"
        "    if (PyArray_ResolveWritebackIfCopy((PyArrayObject *)copy_obj) == 1 && base_data[1] == 42 &&\n"
        "        PyArray_ISWRITEABLE((PyArrayObject *)base_obj) && PyArray_BASE((PyArrayObject *)copy_obj) == NULL &&\n"
        "        !PyArray_CHKFLAGS((PyArrayObject *)copy_obj, NPY_ARRAY_WRITEBACKIFCOPY)) score += 10000;\n"
        "    if (PyArray_ResolveWritebackIfCopy((PyArrayObject *)copy_obj) == 0) score += 100000;\n"
        "    if (PyArray_SetWritebackIfCopyBase((PyArrayObject *)copy_obj, NULL) != 0 && PyErr_Occurred() != NULL) {\n"
        "        PyErr_Clear();\n"
        "        score += 1000000;\n"
        "    }\n"
        "    Py_INCREF(base_obj);\n"
        "    if (PyArray_SetBaseObject((PyArrayObject *)copy_obj, base_obj) == 0) {\n"
        "        Py_INCREF(base_obj);\n"
        "        if (PyArray_SetWritebackIfCopyBase((PyArrayObject *)copy_obj, (PyArrayObject *)base_obj) != 0 && PyErr_Occurred() != NULL) {\n"
        "            PyErr_Clear();\n"
        "            score += 10000000;\n"
        "        }\n"
        "    }\n"
        "    if (PyArray_SetUpdateIfCopyBase((PyArrayObject *)copy_obj, (PyArrayObject *)base_obj) != 0 && PyErr_Occurred() != NULL) {\n"
        "        PyErr_Clear();\n"
        "        score += 100000000;\n"
        "    }\n"
        "    Py_DECREF(copy_obj);\n"
        "    Py_DECREF(base_obj);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *pccnpcons_discard_writeback_if_copy_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    npy_intp dims[1] = {3};\n"
        "    PyObject *base_obj = PyArray_SimpleNew(1, dims, NPY_LONG);\n"
        "    if (base_obj == NULL) return NULL;\n"
        "    long *base_data = (long *)PyArray_DATA((PyArrayObject *)base_obj);\n"
        "    base_data[0] = 1;\n"
        "    base_data[1] = 2;\n"
        "    base_data[2] = 3;\n"
        "    PyObject *copy_obj = PyArray_NewCopy((PyArrayObject *)base_obj, NPY_CORDER);\n"
        "    if (copy_obj == NULL) { Py_DECREF(base_obj); return NULL; }\n"
        "    Py_INCREF(base_obj);\n"
        "    if (PyArray_SetWritebackIfCopyBase((PyArrayObject *)copy_obj, (PyArrayObject *)base_obj) == 0) score += 1;\n"
        "    ((long *)PyArray_DATA((PyArrayObject *)copy_obj))[1] = 99;\n"
        "    PyArray_DiscardWritebackIfCopy((PyArrayObject *)copy_obj);\n"
        "    if (base_data[1] == 2) score += 10;\n"
        "    if (PyArray_ISWRITEABLE((PyArrayObject *)base_obj)) score += 100;\n"
        "    if (PyArray_BASE((PyArrayObject *)copy_obj) == NULL) score += 1000;\n"
        "    if (!PyArray_CHKFLAGS((PyArrayObject *)copy_obj, NPY_ARRAY_WRITEBACKIFCOPY)) score += 10000;\n"
        "    PyArray_DiscardWritebackIfCopy((PyArrayObject *)copy_obj);\n"
        "    if (base_data[1] == 2 && PyArray_ISWRITEABLE((PyArrayObject *)base_obj) &&\n"
        "        PyArray_BASE((PyArrayObject *)copy_obj) == NULL &&\n"
        "        !PyArray_CHKFLAGS((PyArrayObject *)copy_obj, NPY_ARRAY_WRITEBACKIFCOPY)) score += 100000;\n"
        "    PyArray_DiscardWritebackIfCopy((PyArrayObject *)base_obj);\n"
        "    if (PyErr_Occurred() == NULL && base_data[1] == 2) score += 1000000;\n"
        "    PyArray_DiscardWritebackIfCopy(NULL);\n"
        "    if (PyErr_Occurred() == NULL) score += 10000000;\n"
        "    Py_DECREF(copy_obj);\n"
        "    Py_DECREF(base_obj);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyMethodDef ConsumerMethods[] = {\n"
        '    {"table_shape_score", pccnpcons_table_shape_score, METH_VARARGS, "import pcc NumPy C API stub capsules"},\n'
        '    {"type_object_score", pccnpcons_type_object_score, METH_VARARGS, "minimal pcc NumPy C API type-object tokens"},\n'
        '    {"type_object_from_type_score", pccnpcons_type_object_from_type_score, METH_VARARGS, "minimal pcc NumPy scalar type-object lookup"},\n'
        '    {"unsupported_score", pccnpcons_unsupported_score, METH_VARARGS, "unsupported stubs fail visibly"},\n'
        '    {"ufunc_registration_score", pccnpcons_ufunc_registration_score, METH_VARARGS, "minimal pcc NumPy ufunc registration"},\n'
        '    {"inferred_dtype_score", pccnpcons_inferred_dtype_score, METH_VARARGS, "minimal pcc NumPy descriptor-less FromAny inference"},\n'
        '    {"descr_score", pccnpcons_descr_score, METH_VARARGS, "minimal pcc NumPy descriptor metadata"},\n'
        '    {"fromany_score", pccnpcons_fromany_score, METH_VARARGS, "minimal pcc NumPy FromAny coercion"},\n'
        '    {"array_metadata_score", pccnpcons_array_metadata_score, METH_VARARGS, "minimal pcc NumPy array metadata"},\n'
        '    {"pointer_metadata_score", pccnpcons_pointer_metadata_score, METH_VARARGS, "minimal pcc NumPy pointer metadata"},\n'
        '    {"dtype_field_score", pccnpcons_dtype_field_score, METH_VARARGS, "minimal pcc NumPy dtype field macros"},\n'
        '    {"dtype_classification_score", pccnpcons_dtype_classification_score, METH_VARARGS, "minimal pcc NumPy dtype classification macros"},\n'
        '    {"scalar_kind_score", pccnpcons_scalar_kind_score, METH_VARARGS, "minimal pcc NumPy scalar kind helper"},\n'
        '    {"check_any_scalar_exact_score", pccnpcons_check_any_scalar_exact_score, METH_VARARGS, "minimal pcc NumPy exact scalar checker"},\n'
        '    {"can_coerce_scalar_score", pccnpcons_can_coerce_scalar_score, METH_VARARGS, "minimal pcc NumPy scalar coercion helper"},\n'
        '    {"can_cast_scalar_score", pccnpcons_can_cast_scalar_score, METH_VARARGS, "minimal pcc NumPy scalar type cast query helper"},\n'
        '    {"convert_common_type_score", pccnpcons_convert_common_type_score, METH_VARARGS, "minimal pcc NumPy common dtype conversion helper"},\n'
        '    {"state_byteorder_score", pccnpcons_state_byteorder_score, METH_VARARGS, "minimal pcc NumPy state and byteorder macros"},\n'
        '    {"from_macro_score", pccnpcons_from_macro_score, METH_VARARGS, "minimal pcc NumPy FromAny convenience macros"},\n'
        '    {"fill_byteorder_score", pccnpcons_fill_byteorder_score, METH_VARARGS, "minimal pcc NumPy fill and byteorder macros"},\n'
        '    {"shape_compare_score", pccnpcons_shape_compare_score, METH_VARARGS, "minimal pcc NumPy shape comparison helpers"},\n'
        '    {"empty_zeros_score", pccnpcons_empty_zeros_score, METH_VARARGS, "minimal pcc NumPy C-order allocation helpers"},\n'
        '    {"equiv_types_score", pccnpcons_equiv_types_score, METH_VARARGS, "minimal pcc NumPy dtype equivalence helpers"},\n'
        '    {"new_from_descr_score", pccnpcons_new_from_descr_score, METH_VARARGS, "minimal pcc NumPy descriptor allocation helpers"},\n'
        '    {"new_score", pccnpcons_new_score, METH_VARARGS, "minimal pcc NumPy generic array constructor helper"},\n'
        '    {"array_check_score", pccnpcons_array_check_score, METH_VARARGS, "minimal pcc NumPy array check helpers"},\n'
        '    {"size_itemsize_score", pccnpcons_size_itemsize_score, METH_VARARGS, "minimal pcc NumPy size and itemsize helpers"},\n'
        '    {"array_size_score", pccnpcons_array_size_score, METH_VARARGS, "minimal pcc NumPy object-safe array size helper"},\n'
        '    {"accessor_score", pccnpcons_accessor_score, METH_VARARGS, "minimal pcc NumPy accessor helpers"},\n'
        '    {"core_provider_score", pccnpcons_core_provider_score, METH_VARARGS, "minimal pcc NumPy core provider helpers"},\n'
        '    {"base_object_score", pccnpcons_base_object_score, METH_VARARGS, "minimal pcc NumPy base-object ownership helpers"},\n'
        '    {"return_score", pccnpcons_return_score, METH_VARARGS, "minimal pcc NumPy array return helper"},\n'
        '    {"flags_mutation_score", pccnpcons_flags_mutation_score, METH_VARARGS, "minimal pcc NumPy flag mutation helpers"},\n'
        '    {"copy_into_score", pccnpcons_copy_into_score, METH_VARARGS, "minimal pcc NumPy copy helpers"},\n'
        '    {"to_scalar_score", pccnpcons_to_scalar_score, METH_VARARGS, "minimal pcc NumPy scalar extraction helper"},\n'
        '    {"copy_score", pccnpcons_copy_score, METH_VARARGS, "minimal pcc NumPy array copy helper"},\n'
        '    {"new_copy_score", pccnpcons_new_copy_score, METH_VARARGS, "minimal pcc NumPy ordered array copy helper"},\n'
        '    {"copy_object_score", pccnpcons_copy_object_score, METH_VARARGS, "minimal pcc NumPy object-to-array assignment helper"},\n'
        '    {"resize_score", pccnpcons_resize_score, METH_VARARGS, "minimal pcc NumPy resize helper"},\n'
        '    {"new_like_score", pccnpcons_new_like_score, METH_VARARGS, "minimal pcc NumPy new-like-array helper"},\n'
        '    {"view_score", pccnpcons_view_score, METH_VARARGS, "minimal pcc NumPy same-dtype view helper"},\n'
        '    {"squeeze_score", pccnpcons_squeeze_score, METH_VARARGS, "minimal pcc NumPy squeeze view helper"},\n'
        '    {"transpose_score", pccnpcons_transpose_score, METH_VARARGS, "minimal pcc NumPy transpose view helper"},\n'
        '    {"swap_axes_score", pccnpcons_swap_axes_score, METH_VARARGS, "minimal pcc NumPy swap-axes view helper"},\n'
        '    {"ravel_score", pccnpcons_ravel_score, METH_VARARGS, "minimal pcc NumPy ravel view helper"},\n'
        '    {"flatten_score", pccnpcons_flatten_score, METH_VARARGS, "minimal pcc NumPy flatten copy helper"},\n'
        '    {"take_from_score", pccnpcons_take_from_score, METH_VARARGS, "minimal pcc NumPy take-from copy helper"},\n'
        '    {"put_to_score", pccnpcons_put_to_score, METH_VARARGS, "minimal pcc NumPy indexed put helper"},\n'
        '    {"put_mask_score", pccnpcons_put_mask_score, METH_VARARGS, "minimal pcc NumPy masked put helper"},\n'
        '    {"repeat_score", pccnpcons_repeat_score, METH_VARARGS, "minimal pcc NumPy repeat helper"},\n'
        '    {"choose_score", pccnpcons_choose_score, METH_VARARGS, "minimal pcc NumPy choose helper"},\n'
        '    {"concatenate_score", pccnpcons_concatenate_score, METH_VARARGS, "minimal pcc NumPy concatenate helper"},\n'
        '    {"arange_score", pccnpcons_arange_score, METH_VARARGS, "minimal pcc NumPy arange helper"},\n'
        '    {"arange_obj_score", pccnpcons_arange_obj_score, METH_VARARGS, "minimal pcc NumPy object-scalar arange helper"},\n'
        '    {"inner_product_score", pccnpcons_inner_product_score, METH_VARARGS, "minimal pcc NumPy inner-product helper"},\n'
        '    {"matrix_product_score", pccnpcons_matrix_product_score, METH_VARARGS, "minimal pcc NumPy matrix-product helper"},\n'
        '    {"matrix_product2_score", pccnpcons_matrix_product2_score, METH_VARARGS, "minimal pcc NumPy matrix-product2 helper"},\n'
        '    {"einstein_sum_score", pccnpcons_einstein_sum_score, METH_VARARGS, "minimal pcc NumPy Einstein-sum helper"},\n'
        '    {"correlate_score", pccnpcons_correlate_score, METH_VARARGS, "minimal pcc NumPy correlate helper"},\n'
        '    {"correlate2_score", pccnpcons_correlate2_score, METH_VARARGS, "minimal pcc NumPy correlate2 helper"},\n'
        '    {"lexsort_score", pccnpcons_lexsort_score, METH_VARARGS, "minimal pcc NumPy lexsort helper"},\n'
        '    {"sort_score", pccnpcons_sort_score, METH_VARARGS, "minimal pcc NumPy sort helper"},\n'
        '    {"argsort_score", pccnpcons_argsort_score, METH_VARARGS, "minimal pcc NumPy argsort helper"},\n'
        '    {"partition_score", pccnpcons_partition_score, METH_VARARGS, "minimal pcc NumPy partition helper"},\n'
        '    {"searchsorted_score", pccnpcons_searchsorted_score, METH_VARARGS, "minimal pcc NumPy searchsorted helper"},\n'
        '    {"nonzero_score", pccnpcons_nonzero_score, METH_VARARGS, "minimal pcc NumPy nonzero helper"},\n'
        '    {"count_nonzero_score", pccnpcons_count_nonzero_score, METH_VARARGS, "minimal pcc NumPy count-nonzero helper"},\n'
        '    {"min_scalar_type_score", pccnpcons_min_scalar_type_score, METH_VARARGS, "minimal pcc NumPy min-scalar-type helper"},\n'
        '    {"sorted_stride_perm_score", pccnpcons_sorted_stride_perm_score, METH_VARARGS, "minimal pcc NumPy sorted-stride-permutation helper"},\n'
        '    {"remove_axes_in_place_score", pccnpcons_remove_axes_in_place_score, METH_VARARGS, "minimal pcc NumPy in-place axis-removal helper"},\n'
        '    {"debug_print_score", pccnpcons_debug_print_score, METH_VARARGS, "minimal pcc NumPy debug printer helper"},\n'
        '    {"where_score", pccnpcons_where_score, METH_VARARGS, "minimal pcc NumPy where helper"},\n'
        '    {"compress_score", pccnpcons_compress_score, METH_VARARGS, "minimal pcc NumPy compress helper"},\n'
        '    {"diagonal_score", pccnpcons_diagonal_score, METH_VARARGS, "minimal pcc NumPy diagonal helper"},\n'
        '    {"trace_score", pccnpcons_trace_score, METH_VARARGS, "minimal pcc NumPy trace helper"},\n'
        '    {"clip_score", pccnpcons_clip_score, METH_VARARGS, "minimal pcc NumPy clip helper"},\n'
        '    {"conjugate_score", pccnpcons_conjugate_score, METH_VARARGS, "minimal pcc NumPy conjugate helper"},\n'
        '    {"std_score", pccnpcons_std_score, METH_VARARGS, "minimal pcc NumPy standard-deviation helper"},\n'
        '    {"round_score", pccnpcons_round_score, METH_VARARGS, "minimal pcc NumPy round helper"},\n'
        '    {"sum_score", pccnpcons_sum_score, METH_VARARGS, "minimal pcc NumPy sum helper"},\n'
        '    {"cumsum_score", pccnpcons_cumsum_score, METH_VARARGS, "minimal pcc NumPy cumulative-sum helper"},\n'
        '    {"prod_score", pccnpcons_prod_score, METH_VARARGS, "minimal pcc NumPy product helper"},\n'
        '    {"cumprod_score", pccnpcons_cumprod_score, METH_VARARGS, "minimal pcc NumPy cumulative-product helper"},\n'
        '    {"max_score", pccnpcons_max_score, METH_VARARGS, "minimal pcc NumPy max helper"},\n'
        '    {"min_score", pccnpcons_min_score, METH_VARARGS, "minimal pcc NumPy min helper"},\n'
        '    {"ptp_score", pccnpcons_ptp_score, METH_VARARGS, "minimal pcc NumPy peak-to-peak helper"},\n'
        '    {"mean_score", pccnpcons_mean_score, METH_VARARGS, "minimal pcc NumPy mean helper"},\n'
        '    {"any_score", pccnpcons_any_score, METH_VARARGS, "minimal pcc NumPy any helper"},\n'
        '    {"all_score", pccnpcons_all_score, METH_VARARGS, "minimal pcc NumPy all helper"},\n'
        '    {"argmax_score", pccnpcons_argmax_score, METH_VARARGS, "minimal pcc NumPy argmax helper"},\n'
        '    {"argmin_score", pccnpcons_argmin_score, METH_VARARGS, "minimal pcc NumPy argmin helper"},\n'
        '    {"reshape_score", pccnpcons_reshape_score, METH_VARARGS, "minimal pcc NumPy reshape helper"},\n'
        '    {"newshape_score", pccnpcons_newshape_score, METH_VARARGS, "minimal pcc NumPy newshape helper"},\n'
        '    {"ensure_array_score", pccnpcons_ensure_array_score, METH_VARARGS, "minimal pcc NumPy ensure-array helpers"},\n'
        '    {"descr_check_score", pccnpcons_descr_check_score, METH_VARARGS, "minimal pcc NumPy descriptor type check helper"},\n'
        '    {"descr_new_from_type_score", pccnpcons_descr_new_from_type_score, METH_VARARGS, "minimal pcc NumPy descriptor copy helper"},\n'
        '    {"descr_new_score", pccnpcons_descr_new_score, METH_VARARGS, "minimal pcc NumPy descriptor copy-from-descriptor helper"},\n'
        '    {"descr_new_byteorder_score", pccnpcons_descr_new_byteorder_score, METH_VARARGS, "minimal pcc NumPy descriptor byteorder-copy helper"},\n'
        '    {"can_cast_safely_score", pccnpcons_can_cast_safely_score, METH_VARARGS, "minimal pcc NumPy safe-cast query helper"},\n'
        '    {"can_cast_to_score", pccnpcons_can_cast_to_score, METH_VARARGS, "minimal pcc NumPy descriptor safe-cast query helper"},\n'
        '    {"can_cast_type_array_score", pccnpcons_can_cast_type_array_score, METH_VARARGS, "minimal pcc NumPy casting-policy query helpers"},\n'
        '    {"casting_converter_score", pccnpcons_casting_converter_score, METH_VARARGS, "minimal pcc NumPy casting string converter helper"},\n'
        '    {"zero_one_score", pccnpcons_zero_one_score, METH_VARARGS, "minimal pcc NumPy zero/one scalar-buffer helpers"},\n'
        '    {"object_type_score", pccnpcons_object_type_score, METH_VARARGS, "minimal pcc NumPy object-to-dtype inference helper"},\n'
        '    {"descr_from_object_score", pccnpcons_descr_from_object_score, METH_VARARGS, "minimal pcc NumPy object-to-descriptor inference helper"},\n'
        '    {"descr_converter_score", pccnpcons_descr_converter_score, METH_VARARGS, "minimal pcc NumPy object-to-descriptor converter helpers"},\n'
        '    {"descr_align_converter_score", pccnpcons_descr_align_converter_score, METH_VARARGS, "minimal pcc NumPy aligned descriptor converter helpers"},\n'
        '    {"descr_from_scalar_score", pccnpcons_descr_from_scalar_score, METH_VARARGS, "minimal pcc NumPy scalar-to-descriptor helper"},\n'
        '    {"descr_from_type_object_score", pccnpcons_descr_from_type_object_score, METH_VARARGS, "minimal pcc NumPy type-object-to-descriptor helper"},\n'
        '    {"scalar_score", pccnpcons_scalar_score, METH_VARARGS, "minimal pcc NumPy raw item-to-scalar helper"},\n'
        '    {"scalar_as_ctype_score", pccnpcons_scalar_as_ctype_score, METH_VARARGS, "minimal pcc NumPy scalar-to-C-buffer helper"},\n'
        '    {"from_scalar_score", pccnpcons_from_scalar_score, METH_VARARGS, "minimal pcc NumPy scalar-to-0d-array helper"},\n'
        '    {"cast_scalar_to_ctype_score", pccnpcons_cast_scalar_to_ctype_score, METH_VARARGS, "minimal pcc NumPy scalar-to-typed-C-buffer helper"},\n'
        '    {"cast_scalar_direct_score", pccnpcons_cast_scalar_direct_score, METH_VARARGS, "minimal pcc NumPy direct scalar cast helper"},\n'
        '    {"pack_score", pccnpcons_pack_score, METH_VARARGS, "minimal pcc NumPy scalar/0d-array-to-C-buffer pack helper"},\n'
        '    {"from_array_score", pccnpcons_from_array_score, METH_VARARGS, "minimal pcc NumPy FromArray helper"},\n'
        '    {"cast_to_type_score", pccnpcons_cast_to_type_score, METH_VARARGS, "minimal pcc NumPy array cast-to-type helper"},\n'
        '    {"fill_with_scalar_score", pccnpcons_fill_with_scalar_score, METH_VARARGS, "minimal pcc NumPy fill-with-scalar helper"},\n'
        '    {"to_list_score", pccnpcons_to_list_score, METH_VARARGS, "minimal pcc NumPy array to-list helper"},\n'
        '    {"to_string_score", pccnpcons_to_string_score, METH_VARARGS, "minimal pcc NumPy array to-string helper"},\n'
        '    {"byteswap_score", pccnpcons_byteswap_score, METH_VARARGS, "minimal pcc NumPy byteswap helper"},\n'
        '    {"from_string_score", pccnpcons_from_string_score, METH_VARARGS, "minimal pcc NumPy from-string helper"},\n'
        '    {"from_buffer_score", pccnpcons_from_buffer_score, METH_VARARGS, "minimal pcc NumPy from-buffer helper"},\n'
        '    {"buffer_converter_score", pccnpcons_buffer_converter_score, METH_VARARGS, "minimal pcc NumPy buffer converter helper"},\n'
        '    {"from_iter_score", pccnpcons_from_iter_score, METH_VARARGS, "minimal pcc NumPy from-iter helper"},\n'
        '    {"converter_score", pccnpcons_converter_score, METH_VARARGS, "minimal pcc NumPy generic array converter helper"},\n'
        '    {"pyint_converter_score", pccnpcons_pyint_converter_score, METH_VARARGS, "minimal pcc NumPy PyInt converters"},\n'
        '    {"intp_from_sequence_score", pccnpcons_intp_from_sequence_score, METH_VARARGS, "minimal pcc NumPy intp-from-sequence helper"},\n'
        '    {"intp_converter_score", pccnpcons_intp_converter_score, METH_VARARGS, "minimal pcc NumPy intp converter helper"},\n'
        '    {"optional_intp_converter_score", pccnpcons_optional_intp_converter_score, METH_VARARGS, "minimal pcc NumPy optional intp converter helper"},\n'
        '    {"promote_types_score", pccnpcons_promote_types_score, METH_VARARGS, "minimal pcc NumPy dtype promotion helper"},\n'
        '    {"result_type_score", pccnpcons_result_type_score, METH_VARARGS, "minimal pcc NumPy result-type helper"},\n'
        '    {"priority_score", pccnpcons_priority_score, METH_VARARGS, "minimal pcc NumPy array-priority helper"},\n'
        '    {"check_strides_score", pccnpcons_check_strides_score, METH_VARARGS, "minimal pcc NumPy stride bounds helper"},\n'
        '    {"broadcast_to_shape_score", pccnpcons_broadcast_to_shape_score, METH_VARARGS, "minimal pcc NumPy broadcast iterator helper"},\n'
        '    {"broadcast_multi_score", pccnpcons_broadcast_multi_score, METH_VARARGS, "minimal pcc NumPy multi-iterator broadcast recompute helper"},\n'
        '    {"multi_iter_from_objects_score", pccnpcons_multi_iter_from_objects_score, METH_VARARGS, "minimal pcc NumPy multi-iterator pointer-vector helper"},\n'
        '    {"remove_smallest_score", pccnpcons_remove_smallest_score, METH_VARARGS, "minimal pcc NumPy multi-iterator remove-smallest helper"},\n'
        '    {"multi_iter_score", pccnpcons_multi_iter_score, METH_VARARGS, "minimal pcc NumPy multi-iterator broadcast helper"},\n'
        '    {"iter_all_but_axis_score", pccnpcons_iter_all_but_axis_score, METH_VARARGS, "minimal pcc NumPy all-but-axis iterator helper"},\n'
        '    {"list_pointer_score", pccnpcons_list_pointer_score, METH_VARARGS, "minimal pcc NumPy list/pointer helpers"},\n'
        '    {"overflow_multiply_score", pccnpcons_overflow_multiply_score, METH_VARARGS, "minimal pcc NumPy overflow multiply helper"},\n'
        '    {"endianness_score", pccnpcons_endianness_score, METH_VARARGS, "minimal pcc NumPy endianness helper"},\n'
        '    {"feature_version_score", pccnpcons_feature_version_score, METH_VARARGS, "minimal pcc NumPy feature-version helper"},\n'
        '    {"version_score", pccnpcons_version_score, METH_VARARGS, "minimal pcc NumPy ABI/API version helpers"},\n'
        '    {"check_axis_score", pccnpcons_check_axis_score, METH_VARARGS, "minimal pcc NumPy CheckAxis helper"},\n'
        '    {"clipmode_converter_score", pccnpcons_clipmode_converter_score, METH_VARARGS, "minimal pcc NumPy clipmode converter helper"},\n'
        '    {"clipmode_sequence_score", pccnpcons_clipmode_sequence_score, METH_VARARGS, "minimal pcc NumPy clipmode sequence helper"},\n'
        '    {"output_converter_score", pccnpcons_output_converter_score, METH_VARARGS, "minimal pcc NumPy output converter helper"},\n'
        '    {"searchside_converter_score", pccnpcons_searchside_converter_score, METH_VARARGS, "minimal pcc NumPy searchside converter helper"},\n'
        '    {"byteorder_converter_score", pccnpcons_byteorder_converter_score, METH_VARARGS, "minimal pcc NumPy byteorder converter helper"},\n'
        '    {"sortkind_converter_score", pccnpcons_sortkind_converter_score, METH_VARARGS, "minimal pcc NumPy sortkind converter helper"},\n'
        '    {"selectkind_converter_score", pccnpcons_selectkind_converter_score, METH_VARARGS, "minimal pcc NumPy selectkind converter helper"},\n'
        '    {"order_converter_score", pccnpcons_order_converter_score, METH_VARARGS, "minimal pcc NumPy order converter helper"},\n'
        '    {"bool_converter_score", pccnpcons_bool_converter_score, METH_VARARGS, "minimal pcc NumPy bool converter helper"},\n'
        '    {"optional_bool_converter_score", pccnpcons_optional_bool_converter_score, METH_VARARGS, "minimal pcc NumPy optional bool converter helper"},\n'
        '    {"axis_converter_score", pccnpcons_axis_converter_score, METH_VARARGS, "minimal pcc NumPy axis converter helper"},\n'
        '    {"element_strides_score", pccnpcons_element_strides_score, METH_VARARGS, "minimal pcc NumPy element-strides helper"},\n'
        '    {"valid_type_score", pccnpcons_valid_type_score, METH_VARARGS, "minimal pcc NumPy dtype validity helper"},\n'
        '    {"item_refcount_score", pccnpcons_item_refcount_score, METH_VARARGS, "minimal pcc NumPy object item refcount helpers"},\n'
        '    {"array_refcount_score", pccnpcons_array_refcount_score, METH_VARARGS, "minimal pcc NumPy object array refcount helpers"},\n'
        '    {"allocator_score", pccnpcons_allocator_score, METH_VARARGS, "minimal pcc NumPy allocator macros"},\n'
        '    {"datamem_score", pccnpcons_datamem_score, METH_VARARGS, "minimal pcc NumPy data-memory helpers"},\n'
        '    {"datamem_user_score", pccnpcons_datamem_user_score, METH_VARARGS, "minimal pcc NumPy handler data-memory helpers"},\n'
        '    {"array_free_score", pccnpcons_array_free_score, METH_VARARGS, "minimal pcc NumPy public array free helper"},\n'
        '    {"as_carray_score", pccnpcons_as_carray_score, METH_VARARGS, "minimal pcc NumPy public C-array adapter"},\n'
        '    {"fail_writeable_score", pccnpcons_fail_writeable_score, METH_VARARGS, "minimal pcc NumPy writeable guard"},\n'
        '    {"writeback_if_copy_score", pccnpcons_writeback_if_copy_score, METH_VARARGS, "minimal pcc NumPy writeback-if-copy lifecycle"},\n'
        '    {"discard_writeback_if_copy_score", pccnpcons_discard_writeback_if_copy_score, METH_VARARGS, "minimal pcc NumPy discard writeback-if-copy lifecycle"},\n'
        "    {NULL, NULL, 0, NULL},\n"
        "};\n"
        "\n"
        "static PyModuleDef consumermodule = {\n"
        '    PyModuleDef_HEAD_INIT, "pccnpcons", NULL, -1, ConsumerMethods,\n'
        "};\n"
        "\n"
        "PyMODINIT_FUNC PyInit_pccnpcons(void) {\n"
        "    import_array();\n"
        "    import_umath();\n"
        "    return PyModule_Create(&consumermodule);\n"
        "}\n",
    )
    return site


def _compile_feature_extension(tmp_path: Path) -> Path:
    return _compile_extension(
        tmp_path,
        "featuredemo",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "\n"
        "static long init_count = 0;\n"
        "\n"
        "static PyObject *feature_count(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    return PyLong_FromLong(init_count);\n"
        "}\n"
        "\n"
        "static PyObject *feature_echo(PyObject *self, PyObject *args) {\n"
        "    const char *value = NULL;\n"
        "    (void)self;\n"
        '    if (!PyArg_ParseTuple(args, "s", &value)) return NULL;\n'
        "    return PyUnicode_FromString(value);\n"
        "}\n"
        "\n"
        "static PyObject *feature_add(PyObject *self, PyObject *args) {\n"
        "    long a = 0;\n"
        "    long b = 0;\n"
        "    (void)self;\n"
        '    if (!PyArg_ParseTuple(args, "ll", &a, &b)) return NULL;\n'
        "    return PyLong_FromLong(a + b);\n"
        "}\n"
        "\n"
        "static PyObject *feature_return_none(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    Py_RETURN_NONE;\n"
        "}\n"
        "\n"
        "static PyObject *feature_return_true(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    Py_RETURN_TRUE;\n"
        "}\n"
        "\n"
        "static PyObject *feature_return_false(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    Py_RETURN_FALSE;\n"
        "}\n"
        "\n"
        "static PyObject *feature_return_notimplemented(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    Py_RETURN_NOTIMPLEMENTED;\n"
        "}\n"
        "\n"
        "static PyObject *feature_identity_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    score += Py_IsNone(Py_None) ? 1 : 0;\n"
        "    score += Py_IsTrue(Py_True) ? 10 : 0;\n"
        "    score += Py_IsFalse(Py_False) ? 100 : 0;\n"
        "    score += Py_Is(Py_NotImplemented, Py_NotImplemented) ? 1000 : 0;\n"
        "    score += !Py_Is(Py_None, Py_False) ? 10000 : 0;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *feature_fail(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        '    PyErr_SetString(PyExc_RuntimeError, "feature method failed");\n'
        "    return NULL;\n"
        "}\n"
        "\n"
        "static PyMethodDef FeatureMethods[] = {\n"
        '    {"count", feature_count, METH_VARARGS, "return init count"},\n'
        '    {"echo", feature_echo, METH_VARARGS, "echo a string"},\n'
        '    {"add", feature_add, METH_VARARGS, "add two ints"},\n'
        '    {"return_none", feature_return_none, METH_VARARGS, "return None"},\n'
        '    {"return_true", feature_return_true, METH_VARARGS, "return True"},\n'
        '    {"return_false", feature_return_false, METH_VARARGS, "return False"},\n'
        '    {"return_notimplemented", feature_return_notimplemented, METH_VARARGS, "return NotImplemented"},\n'
        '    {"identity_score", feature_identity_score, METH_VARARGS, "Py_Is macro smoke"},\n'
        '    {"fail", feature_fail, METH_VARARGS, "raise a RuntimeError"},\n'
        "    {NULL, NULL, 0, NULL},\n"
        "};\n"
        "\n"
        "static PyModuleDef featuremodule = {\n"
        '    PyModuleDef_HEAD_INIT, "featuredemo", NULL, -1, FeatureMethods,\n'
        "};\n"
        "\n"
        "PyMODINIT_FUNC PyInit_featuredemo(void) {\n"
        "    init_count += 1;\n"
        "    return PyModule_Create(&featuremodule);\n"
        "}\n",
    )


def _compile_buffer_extension(tmp_path: Path) -> Path:
    return _compile_extension(
        tmp_path,
        "bufdemo",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "\n"
        "static PyObject *buffer_sum(PyObject *self, PyObject *args) {\n"
        "    PyObject *obj = NULL;\n"
        "    Py_buffer view;\n"
        "    long total = 0;\n"
        "    (void)self;\n"
        '    if (!PyArg_ParseTuple(args, "O", &obj)) return NULL;\n'
        "    if (PyObject_GetBuffer(obj, &view, PyBUF_SIMPLE) != 0) return NULL;\n"
        "    unsigned char *data = (unsigned char *)view.buf;\n"
        "    for (Py_ssize_t i = 0; i < view.len; i++) {\n"
        "        total += data[i];\n"
        "    }\n"
        "    PyBuffer_Release(&view);\n"
        "    return PyLong_FromLong(total);\n"
        "}\n"
        "\n"
        "static PyObject *buffer_writable(PyObject *self, PyObject *args) {\n"
        "    PyObject *obj = NULL;\n"
        "    Py_buffer view;\n"
        "    int ok = 0;\n"
        "    (void)self;\n"
        '    if (!PyArg_ParseTuple(args, "O", &obj)) return NULL;\n'
        "    if (PyObject_GetBuffer(obj, &view, PyBUF_WRITABLE) != 0) {\n"
        "        PyErr_Clear();\n"
        "        return PyLong_FromLong(0);\n"
        "    }\n"
        "    ok = view.readonly == 0;\n"
        "    PyBuffer_Release(&view);\n"
        "    return PyLong_FromLong(ok);\n"
        "}\n"
        "\n"
        "static PyObject *buffer_metadata_score(PyObject *self, PyObject *args) {\n"
        "    PyObject *obj = NULL;\n"
        "    Py_buffer view;\n"
        "    long score = 0;\n"
        "    (void)self;\n"
        '    if (!PyArg_ParseTuple(args, "O", &obj)) return NULL;\n'
        "    if (PyObject_GetBuffer(obj, &view, PyBUF_STRIDES | PyBUF_FORMAT) != 0) return NULL;\n"
        "    score += view.ndim == 1 ? 1 : 0;\n"
        "    score += view.itemsize == 1 ? 10 : 0;\n"
        "    score += view.shape != NULL ? view.shape[0] * 100 : 0;\n"
        "    score += view.strides != NULL ? view.strides[0] * 1000 : 0;\n"
        "    score += view.format != NULL && view.format[0] == 'B' ? 10000 : 0;\n"
        "    score += view.readonly ? 100000 : 200000;\n"
        "    score += view.len * 1000000;\n"
        "    PyBuffer_Release(&view);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *buffer_view_sum(PyObject *self, PyObject *args) {\n"
        "    PyObject *obj = NULL;\n"
        "    PyObject *mv = NULL;\n"
        "    Py_buffer view;\n"
        "    long total = 0;\n"
        "    (void)self;\n"
        '    if (!PyArg_ParseTuple(args, "O", &obj)) return NULL;\n'
        "    mv = PyMemoryView_FromObject(obj);\n"
        "    if (mv == NULL) return NULL;\n"
        "    if (PyObject_GetBuffer(mv, &view, PyBUF_SIMPLE) != 0) {\n"
        "        Py_DECREF(mv);\n"
        "        return NULL;\n"
        "    }\n"
        "    unsigned char *data = (unsigned char *)view.buf;\n"
        "    for (Py_ssize_t i = 0; i < view.len; i++) {\n"
        "        total += data[i];\n"
        "    }\n"
        "    PyBuffer_Release(&view);\n"
        "    Py_DECREF(mv);\n"
        "    return PyLong_FromLong(total);\n"
        "}\n"
        "\n"
        "static PyObject *buffer_from_memory_sum(PyObject *self, PyObject *args) {\n"
        "    static char raw[] = {7, 8, 9};\n"
        "    PyObject *mv = NULL;\n"
        "    Py_buffer view;\n"
        "    long total = 0;\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    mv = PyMemoryView_FromMemory(raw, 3, PyBUF_READ);\n"
        "    if (mv == NULL) return NULL;\n"
        "    if (PyObject_GetBuffer(mv, &view, PyBUF_SIMPLE) != 0) {\n"
        "        Py_DECREF(mv);\n"
        "        return NULL;\n"
        "    }\n"
        "    unsigned char *data = (unsigned char *)view.buf;\n"
        "    for (Py_ssize_t i = 0; i < view.len; i++) {\n"
        "        total += data[i];\n"
        "    }\n"
        "    PyBuffer_Release(&view);\n"
        "    Py_DECREF(mv);\n"
        "    return PyLong_FromLong(total);\n"
        "}\n"
        "\n"
        "static PyObject *buffer_from_memory_write_sum(PyObject *self, PyObject *args) {\n"
        "    static char raw[] = {1, 2, 3};\n"
        "    PyObject *mv = NULL;\n"
        "    Py_buffer view;\n"
        "    long total = 0;\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    mv = PyMemoryView_FromMemory(raw, 3, PyBUF_WRITE);\n"
        "    if (mv == NULL) return NULL;\n"
        "    if (PyObject_GetBuffer(mv, &view, PyBUF_WRITABLE) != 0) {\n"
        "        Py_DECREF(mv);\n"
        "        return NULL;\n"
        "    }\n"
        "    unsigned char *data = (unsigned char *)view.buf;\n"
        "    data[0] = 10;\n"
        "    for (Py_ssize_t i = 0; i < view.len; i++) {\n"
        "        total += data[i];\n"
        "    }\n"
        "    PyBuffer_Release(&view);\n"
        "    Py_DECREF(mv);\n"
        "    return PyLong_FromLong(total);\n"
        "}\n"
        "\n"
        "static PyObject *buffer_memoryview_inspect_score(PyObject *self, PyObject *args) {\n"
        "    PyObject *obj = NULL;\n"
        "    PyObject *mv = NULL;\n"
        "    PyObject *base = NULL;\n"
        "    Py_buffer *view = NULL;\n"
        "    long score = 0;\n"
        "    long total = 0;\n"
        "    (void)self;\n"
        '    if (!PyArg_ParseTuple(args, "O", &obj)) return NULL;\n'
        "    mv = PyMemoryView_FromObject(obj);\n"
        "    if (mv == NULL) return NULL;\n"
        "    score += PyMemoryView_Check(mv) ? 1 : 0;\n"
        "    score += !PyMemoryView_Check(obj) ? 10 : 0;\n"
        "    base = PyMemoryView_GET_BASE(mv);\n"
        "    if (base == NULL) {\n"
        "        Py_DECREF(mv);\n"
        "        return NULL;\n"
        "    }\n"
        "    score += base == obj ? 100 : 0;\n"
        "    view = PyMemoryView_GET_BUFFER(mv);\n"
        "    if (view == NULL) {\n"
        "        Py_DECREF(mv);\n"
        "        return NULL;\n"
        "    }\n"
        "    unsigned char *data = (unsigned char *)view->buf;\n"
        "    for (Py_ssize_t i = 0; i < view->len; i++) {\n"
        "        total += data[i];\n"
        "    }\n"
        "    score += view->len * 1000;\n"
        "    score += view->ndim == 1 ? 10000 : 0;\n"
        "    score += view->shape != NULL && view->shape[0] == view->len ? 100000 : 0;\n"
        "    score += view->strides != NULL && view->strides[0] == 1 ? 1000000 : 0;\n"
        "    score += view->format != NULL && view->format[0] == 'B' ? 10000000 : 0;\n"
        "    score += view->readonly == 0 ? 100000000 : 0;\n"
        "    score += total * 100000000;\n"
        "    Py_DECREF(mv);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *buffer_two_live_views_score(PyObject *self, PyObject *args) {\n"
        "    PyObject *left = NULL;\n"
        "    PyObject *right = NULL;\n"
        "    PyObject *left_mv = NULL;\n"
        "    PyObject *right_mv = NULL;\n"
        "    Py_buffer *left_view = NULL;\n"
        "    Py_buffer *right_view = NULL;\n"
        "    long score = 0;\n"
        "    (void)self;\n"
        "    if (!PyArg_ParseTuple(args, \"OO\", &left, &right)) return NULL;\n"
        "    left_mv = PyMemoryView_FromObject(left);\n"
        "    if (left_mv == NULL) return NULL;\n"
        "    right_mv = PyMemoryView_FromObject(right);\n"
        "    if (right_mv == NULL) {\n"
        "        Py_DECREF(left_mv);\n"
        "        return NULL;\n"
        "    }\n"
        "    left_view = PyMemoryView_GET_BUFFER(left_mv);\n"
        "    right_view = PyMemoryView_GET_BUFFER(right_mv);\n"
        "    if (left_view != NULL && right_view != NULL) score += 1;\n"
        "    if (left_view != right_view) score += 2;\n"
        "    if (left_view != NULL && left_view->obj == left) score += 4;\n"
        "    if (right_view != NULL && right_view->obj == right) score += 8;\n"
        "    if (left_view != NULL && left_view->len == 3\n"
        "            && ((unsigned char *)left_view->buf)[0] == 'A'\n"
        "            && ((unsigned char *)left_view->buf)[2] == 'C') score += 16;\n"
        "    if (right_view != NULL && right_view->len == 3\n"
        "            && ((unsigned char *)right_view->buf)[0] == 'x'\n"
        "            && ((unsigned char *)right_view->buf)[2] == 'z') score += 32;\n"
        "    if (PyMemoryView_GET_BASE(left_mv) == left\n"
        "            && PyMemoryView_GET_BASE(right_mv) == right) score += 64;\n"
        "    Py_DECREF(right_mv);\n"
        "    Py_DECREF(left_mv);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *buffer_check_score(PyObject *self, PyObject *args) {\n"
        "    PyObject *bytes_obj = NULL;\n"
        "    PyObject *bytearray_obj = NULL;\n"
        "    PyObject *memoryview_obj = NULL;\n"
        "    PyObject *other = NULL;\n"
        "    long score = 0;\n"
        "    (void)self;\n"
        '    if (!PyArg_ParseTuple(args, "OOOO", &bytes_obj, &bytearray_obj, &memoryview_obj, &other)) return NULL;\n'
        "    score += PyObject_CheckBuffer(bytes_obj) ? 1 : 0;\n"
        "    score += PyObject_CheckBuffer(bytearray_obj) ? 10 : 0;\n"
        "    score += PyObject_CheckBuffer(memoryview_obj) ? 100 : 0;\n"
        "    score += !PyObject_CheckBuffer(other) ? 1000 : 0;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyMethodDef BufferMethods[] = {\n"
        '    {"sum", buffer_sum, METH_VARARGS, "sum bytes through the buffer API"},\n'
        '    {"writable", buffer_writable, METH_VARARGS, "request a writable buffer"},\n'
        '    {"metadata_score", buffer_metadata_score, METH_VARARGS, "buffer metadata smoke"},\n'
        '    {"view_sum", buffer_view_sum, METH_VARARGS, "sum through PyMemoryView_FromObject"},\n'
        '    {"from_memory_sum", buffer_from_memory_sum, METH_VARARGS, "sum PyMemoryView_FromMemory"},\n'
        '    {"from_memory_write_sum", buffer_from_memory_write_sum, METH_VARARGS, "writable PyMemoryView_FromMemory"},\n'
        '    {"memoryview_inspect_score", buffer_memoryview_inspect_score, METH_VARARGS, "memoryview inspect macro smoke"},\n'
        '    {"two_live_views_score", buffer_two_live_views_score, METH_VARARGS, "two live memoryviews keep distinct buffers"},\n'
        '    {"check_score", buffer_check_score, METH_VARARGS, "PyObject_CheckBuffer smoke"},\n'
        "    {NULL, NULL, 0, NULL},\n"
        "};\n"
        "\n"
        "static PyModuleDef buffermodule = {\n"
        '    PyModuleDef_HEAD_INIT, "bufdemo", NULL, -1, BufferMethods,\n'
        "};\n"
        "\n"
        "PyMODINIT_FUNC PyInit_bufdemo(void) {\n"
        "    return PyModule_Create(&buffermodule);\n"
        "}\n",
    )


def _compile_call_extension(tmp_path: Path) -> Path:
    return _compile_extension(
        tmp_path,
        "calldemo",
        "#include <Python.h>\n"
        "\n"
        "static PyObject *call_noargs(PyObject *self, PyObject *args) {\n"
        "    PyObject *callable = NULL;\n"
        "    (void)self;\n"
        '    if (!PyArg_ParseTuple(args, "O", &callable)) return NULL;\n'
        "    return PyObject_Call(callable, NULL, NULL);\n"
        "}\n"
        "\n"
        "static PyObject *call_object_noargs(PyObject *self, PyObject *args) {\n"
        "    PyObject *callable = NULL;\n"
        "    (void)self;\n"
        '    if (!PyArg_ParseTuple(args, "O", &callable)) return NULL;\n'
        "    return PyObject_CallObject(callable, NULL);\n"
        "}\n"
        "\n"
        "static PyObject *call_one_arg(PyObject *self, PyObject *args) {\n"
        "    PyObject *callable = NULL;\n"
        "    PyObject *arg = NULL;\n"
        "    PyObject *result = NULL;\n"
        "    (void)self;\n"
        '    if (!PyArg_ParseTuple(args, "O", &callable)) return NULL;\n'
        "    arg = PyLong_FromLong(8);\n"
        "    if (arg == NULL) return NULL;\n"
        "    result = PyObject_CallFunctionObjArgs(callable, arg, NULL);\n"
        "    Py_DECREF(arg);\n"
        "    return result;\n"
        "}\n"
        "\n"
        "static PyObject *call_bad_args(PyObject *self, PyObject *args) {\n"
        "    PyObject *callable = NULL;\n"
        "    PyObject *bad_args = NULL;\n"
        "    PyObject *result = NULL;\n"
        "    int ok = 0;\n"
        "    (void)self;\n"
        '    if (!PyArg_ParseTuple(args, "O", &callable)) return NULL;\n'
        "    bad_args = PyLong_FromLong(1);\n"
        "    result = PyObject_Call(callable, bad_args, NULL);\n"
        "    if (result != NULL) {\n"
        "        Py_DECREF(result);\n"
        "        Py_DECREF(bad_args);\n"
        "        return PyLong_FromLong(0);\n"
        "    }\n"
        "    ok = PyErr_Occurred() != NULL;\n"
        "    PyErr_Clear();\n"
        "    Py_DECREF(bad_args);\n"
        "    return PyLong_FromLong(ok);\n"
        "}\n"
        "\n"
        "static PyObject *call_is_none(PyObject *self, PyObject *args) {\n"
        "    PyObject *callable = NULL;\n"
        "    PyObject *result = NULL;\n"
        "    int is_none = 0;\n"
        "    (void)self;\n"
        '    if (!PyArg_ParseTuple(args, "O", &callable)) return NULL;\n'
        "    result = PyObject_Call(callable, NULL, NULL);\n"
        "    if (result == NULL) return NULL;\n"
        "    is_none = result == Py_None;\n"
        "    Py_DECREF(result);\n"
        "    return PyLong_FromLong(is_none);\n"
        "}\n"
        "\n"
        "static PyMethodDef CallMethods[] = {\n"
        '    {"call0", call_noargs, METH_VARARGS, "call with no args"},\n'
        '    {"call_object0", call_object_noargs, METH_VARARGS, "call object with no args"},\n'
        '    {"call_one", call_one_arg, METH_VARARGS, "call with one object arg"},\n'
        '    {"bad_args", call_bad_args, METH_VARARGS, "call with invalid args"},\n'
        '    {"call_is_none", call_is_none, METH_VARARGS, "call and check for None"},\n'
        "    {NULL, NULL, 0, NULL},\n"
        "};\n"
        "\n"
        "static PyModuleDef callmodule = {\n"
        '    PyModuleDef_HEAD_INIT, "calldemo", NULL, -1, CallMethods,\n'
        "};\n"
        "\n"
        "PyMODINIT_FUNC PyInit_calldemo(void) {\n"
        "    return PyModule_Create(&callmodule);\n"
        "}\n",
    )


def _compile_generic_vectorcall_extension(tmp_path: Path) -> Path:
    return _compile_extension(
        tmp_path,
        "genericvcdemo",
        "#include <Python.h>\n"
        "static PyObject *target(PyObject *self, PyObject *args) {\n"
        "    PyObject *arg = NULL;\n"
        '    if (!PyArg_ParseTuple(args, "O", &arg)) return NULL;\n'
        "    return PyLong_FromLong(arg == self);\n"
        "}\n"
        "static PyObject *call_one(PyObject *self, PyObject *args) {\n"
        "    PyObject *callable = PyObject_GetAttrString(self, \"target\");\n"
        "    PyObject *result = NULL;\n"
        "    (void)args;\n"
        "    if (callable == NULL) return NULL;\n"
        "    result = PyObject_CallOneArg(callable, self);\n"
        "    Py_DECREF(callable);\n"
        "    return result;\n"
        "}\n"
        "static PyObject *vectorcall_one(PyObject *self, PyObject *args) {\n"
        "    PyObject *callable = PyObject_GetAttrString(self, \"target\");\n"
        "    PyObject *values[1] = {self};\n"
        "    PyObject *result = NULL;\n"
        "    (void)args;\n"
        "    if (callable == NULL) return NULL;\n"
        "    result = PyObject_Vectorcall(callable, values, 1, NULL);\n"
        "    Py_DECREF(callable);\n"
        "    return result;\n"
        "}\n"
        "static PyObject *vectorcall_method_one(PyObject *self, PyObject *args) {\n"
        "    PyObject *name = PyUnicode_FromString(\"target\");\n"
        "    PyObject *values[2] = {self, self};\n"
        "    PyObject *result = NULL;\n"
        "    (void)args;\n"
        "    if (name == NULL) return NULL;\n"
        "    result = PyObject_VectorcallMethod(name, values, 2, NULL);\n"
        "    Py_DECREF(name);\n"
        "    return result;\n"
        "}\n"
        "static PyMethodDef Methods[] = {\n"
        '    {"target", target, METH_VARARGS, "one-argument target"},\n'
        '    {"call_one", call_one, METH_NOARGS, "PyObject_CallOneArg reference"},\n'
        '    {"vectorcall_one", vectorcall_one, METH_NOARGS, "generic vectorcall"},\n'
        '    {"vectorcall_method_one", vectorcall_method_one, METH_NOARGS, "generic method vectorcall"},\n'
        "    {NULL, NULL, 0, NULL},\n"
        "};\n"
        "static PyModuleDef module = {\n"
        '    PyModuleDef_HEAD_INIT, "genericvcdemo", NULL, -1, Methods,\n'
        "};\n"
        "PyMODINIT_FUNC PyInit_genericvcdemo(void) { return PyModule_Create(&module); }\n",
    )


def _compile_object_api_extension(tmp_path: Path) -> Path:
    return _compile_extension(
        tmp_path,
        "objectapi",
        "#include <Python.h>\n"
        "extern FILE *tmpfile(void);\n"
        "extern int fclose(FILE *stream);\n"
        "extern int fflush(FILE *stream);\n"
        "extern int fseek(FILE *stream, long offset, int whence);\n"
        "extern size_t fread(void *ptr, size_t size, size_t nmemb, FILE *stream);\n"
        "\n"
        "static PyObject *objectapi_tuple_sum(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    PyObject *tuple = PyTuple_New(2);\n"
        "    if (tuple == NULL) return NULL;\n"
        "    PyTuple_SET_ITEM(tuple, 0, PyLong_FromLong(11));\n"
        "    PyTuple_SET_ITEM(tuple, 1, PyLong_FromLong(31));\n"
        "    if (PyTuple_GET_SIZE(tuple) != 2) {\n"
        "        Py_DECREF(tuple);\n"
        "        return PyLong_FromLong(-1);\n"
        "    }\n"
        "    PyObject *a = PyTuple_GET_ITEM(tuple, 0);\n"
        "    PyObject *b = PyTuple_GetItem(tuple, 1);\n"
        "    long total = PyLong_AsLong(a) + PyLong_AsLong(b);\n"
        "    Py_DECREF(tuple);\n"
        "    return PyLong_FromLong(total);\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_pack_sum(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    PyObject *a = PyLong_FromLong(5);\n"
        "    PyObject *b = PyLong_FromLong(9);\n"
        "    PyObject *tuple = PyTuple_Pack(2, a, b);\n"
        "    Py_DECREF(a);\n"
        "    Py_DECREF(b);\n"
        "    if (tuple == NULL) return NULL;\n"
        "    long total = PyLong_AsLong(PyTuple_GET_ITEM(tuple, 0))\n"
        "        + PyLong_AsLong(PyTuple_GET_ITEM(tuple, 1));\n"
        "    Py_DECREF(tuple);\n"
        "    return PyLong_FromLong(total);\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_dict_answer(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    PyObject *dict = PyDict_New();\n"
        "    PyObject *value = PyLong_FromLong(41);\n"
        "    if (dict == NULL || value == NULL) return NULL;\n"
        '    if (PyDict_SetItemString(dict, "answer", value) != 0) return NULL;\n'
        "    Py_DECREF(value);\n"
        '    PyObject *found = PyDict_GetItemString(dict, "answer");\n'
        "    if (found == NULL) return NULL;\n"
        "    long answer = PyLong_AsLong(found) + 1;\n"
        "    Py_DECREF(dict);\n"
        "    return PyLong_FromLong(answer);\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_list_sum(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    PyObject *list = PyList_New(2);\n"
        "    if (list == NULL) return NULL;\n"
        "    PyList_SET_ITEM(list, 0, PyLong_FromLong(3));\n"
        "    PyList_SET_ITEM(list, 1, PyLong_FromLong(4));\n"
        "    PyObject *extra = PyLong_FromLong(5);\n"
        "    if (extra == NULL) return NULL;\n"
        "    if (PyList_Append(list, extra) != 0) return NULL;\n"
        "    Py_DECREF(extra);\n"
        "    if (PyList_GET_SIZE(list) != 3) {\n"
        "        Py_DECREF(list);\n"
        "        return PyLong_FromLong(-1);\n"
        "    }\n"
        "    long total = PyLong_AsLong(PyList_GET_ITEM(list, 0))\n"
        "        + PyLong_AsLong(PyList_GetItem(list, 1))\n"
        "        + PyLong_AsLong(PyList_GetItem(list, 2));\n"
        "    Py_DECREF(list);\n"
        "    return PyLong_FromLong(total);\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_bytes_sum(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        '    PyObject *bytes = PyBytes_FromString("ABC");\n'
        "    if (bytes == NULL) return NULL;\n"
        "    char *data = PyBytes_AsString(bytes);\n"
        "    Py_ssize_t n = PyBytes_Size(bytes);\n"
        "    if (data == NULL || n < 0) return NULL;\n"
        "    long total = 0;\n"
        "    for (Py_ssize_t i = 0; i < n; i++) total += (unsigned char)data[i];\n"
        "    Py_DECREF(bytes);\n"
        "    return PyLong_FromLong(total);\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_bytes_size_sum(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        '    PyObject *bytes = PyBytes_FromStringAndSize("DEF", 3);\n'
        "    if (bytes == NULL) return NULL;\n"
        "    char *data = NULL;\n"
        "    Py_ssize_t n = 0;\n"
        "    if (PyBytes_AsStringAndSize(bytes, &data, &n) != 0) return NULL;\n"
        "    long total = n;\n"
        "    for (Py_ssize_t i = 0; i < n; i++) total += (unsigned char)data[i];\n"
        "    total += PyBytes_GET_SIZE(bytes) == 3 ? 1000 : 0;\n"
        "    total += PyBytes_AS_STRING(bytes)[0] == 'D' ? 2000 : 0;\n"
        "    Py_DECREF(bytes);\n"
        "    return PyLong_FromLong(total);\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_unicode_prefix(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        '    return PyUnicode_FromStringAndSize("abcdef", 3);\n'
        "}\n"
        "\n"
        "static PyObject *objectapi_unicode_utf8_size_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    const char payload[] = {'a', 'b', '\\0', 'c', 'd'};\n"
        "    PyObject *text = PyUnicode_FromStringAndSize(payload, 5);\n"
        "    Py_ssize_t n = 0;\n"
        "    long score = 0;\n"
        "    if (text == NULL) return NULL;\n"
        "    const char *raw = PyUnicode_AsUTF8AndSize(text, &n);\n"
        "    if (raw == NULL) return NULL;\n"
        "    score += n == 5 ? 100 : 0;\n"
        "    score += raw[0] == 'a' ? 1 : 0;\n"
        "    score += raw[2] == '\\0' ? 2 : 0;\n"
        "    score += raw[4] == 'd' ? 4 : 0;\n"
        "    Py_DECREF(text);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_unicode_decode_length_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        '    PyObject *bytes = PyBytes_FromStringAndSize("hello", 5);\n'
        "    PyObject *text = NULL;\n"
        "    PyObject *same = NULL;\n"
        "    PyObject *bad = NULL;\n"
        "    const char *raw = NULL;\n"
        "    long score = 0;\n"
        "    if (bytes == NULL) return NULL;\n"
        '    text = PyUnicode_FromEncodedObject(bytes, "utf-8", NULL);\n'
        "    Py_DECREF(bytes);\n"
        "    if (text == NULL) return NULL;\n"
        "    score += PyUnicode_GetLength(text) == 5 ? 1 : 0;\n"
        "    raw = PyUnicode_AsUTF8(text);\n"
        "    if (raw == NULL) return NULL;\n"
        "    score += raw[0] == 'h' && raw[4] == 'o' ? 10 : 0;\n"
        "    same = PyUnicode_FromEncodedObject(text, NULL, NULL);\n"
        "    if (same == NULL) return NULL;\n"
        "    score += same == text ? 100 : 0;\n"
        "    Py_DECREF(same);\n"
        "    bad = PyLong_FromLong(1);\n"
        "    if (bad == NULL) return NULL;\n"
        "    score += PyUnicode_FromEncodedObject(bad, NULL, NULL) == NULL && PyErr_ExceptionMatches(PyExc_TypeError) ? 1000 : 0;\n"
        "    PyErr_Clear();\n"
        "    Py_DECREF(bad);\n"
        "    Py_DECREF(text);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_unicode_encode_concat_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        '    PyObject *left = PyUnicode_FromString("ab");\n'
        '    PyObject *right = PyUnicode_FromString("CD");\n'
        "    PyObject *joined = NULL;\n"
        "    PyObject *utf8 = NULL;\n"
        "    PyObject *ascii = NULL;\n"
        "    PyObject *encoded_utf8 = NULL;\n"
        "    PyObject *latin1 = NULL;\n"
        "    PyObject *non_ascii = NULL;\n"
        "    PyObject *replacement = NULL;\n"
        "    PyObject *replaced = NULL;\n"
        "    PyObject *substring = NULL;\n"
        "    PyObject *bad_ascii = NULL;\n"
        "    PyObject *bad_encoded_ascii = NULL;\n"
        "    char *raw = NULL;\n"
        "    Py_ssize_t n = 0;\n"
        "    long score = 0;\n"
        "    if (left == NULL || right == NULL) return NULL;\n"
        "    joined = PyUnicode_Concat(left, right);\n"
        "    if (joined == NULL) return NULL;\n"
        "    score += PyUnicode_GetLength(joined) == 4 ? 1 : 0;\n"
        "    const char *joined_raw = PyUnicode_AsUTF8(joined);\n"
        "    score += joined_raw != NULL && joined_raw[0] == 'a' && joined_raw[2] == 'C' ? 10 : 0;\n"
        "    utf8 = PyUnicode_AsUTF8String(joined);\n"
        "    ascii = PyUnicode_AsASCIIString(joined);\n"
        "    if (utf8 == NULL || ascii == NULL) return NULL;\n"
        "    if (PyBytes_AsStringAndSize(utf8, &raw, &n) != 0) return NULL;\n"
        "    score += n == 4 ? 100 : 0;\n"
        "    score += raw[2] == 'C' ? 1000 : 0;\n"
        "    if (PyBytes_AsStringAndSize(ascii, &raw, &n) != 0) return NULL;\n"
        "    score += n == 4 ? 10000 : 0;\n"
        "    score += raw[3] == 'D' ? 100000 : 0;\n"
        '    encoded_utf8 = PyUnicode_AsEncodedString(joined, "utf-8", NULL);\n'
        "    if (encoded_utf8 == NULL) return NULL;\n"
        "    if (PyBytes_AsStringAndSize(encoded_utf8, &raw, &n) != 0) return NULL;\n"
        "    score += n == 4 && raw[1] == 'b' ? 2000000 : 0;\n"
        "    score += PyUnicode_Tailmatch(joined, right, 0, -1, -1) == 1 && PyUnicode_Tailmatch(joined, left, 0, -1, 1) == 1 ? 32000000 : 0;\n"
        '    replacement = PyUnicode_FromString("xy");\n'
        "    if (replacement == NULL) return NULL;\n"
        "    replaced = PyUnicode_Replace(joined, right, replacement, 1);\n"
        "    if (replaced == NULL) return NULL;\n"
        "    const char *replaced_raw = PyUnicode_AsUTF8(replaced);\n"
        "    score += replaced_raw != NULL && replaced_raw[2] == 'x' && PyUnicode_GetLength(replaced) == 4 ? 64000000 : 0;\n"
        "    substring = PyUnicode_Substring(replaced, 1, 3);\n"
        "    if (substring == NULL) return NULL;\n"
        "    const char *substring_raw = PyUnicode_AsUTF8(substring);\n"
        "    score += substring_raw != NULL && substring_raw[0] == 'b' && substring_raw[1] == 'x' ? 128000000 : 0;\n"
        "    score += PyUnicode_Contains(joined, right) == 1 ? 256000000 : 0;\n"
        "    const char nonascii_raw[] = {'x', (char)0xc3, (char)0xa9};\n"
        "    non_ascii = PyUnicode_FromStringAndSize(nonascii_raw, 3);\n"
        "    if (non_ascii == NULL) return NULL;\n"
        '    latin1 = PyUnicode_AsEncodedString(non_ascii, "latin1", NULL);\n'
        "    if (latin1 == NULL) return NULL;\n"
        "    if (PyBytes_AsStringAndSize(latin1, &raw, &n) != 0) return NULL;\n"
        "    score += n == 2 ? 4000000 : 0;\n"
        "    score += (unsigned char)raw[1] == 0xe9 ? 8000000 : 0;\n"
        "    bad_ascii = PyUnicode_AsASCIIString(non_ascii);\n"
        "    score += bad_ascii == NULL && PyErr_Occurred() != NULL ? 1000000 : 0;\n"
        "    PyErr_Clear();\n"
        '    bad_encoded_ascii = PyUnicode_AsEncodedString(non_ascii, "ascii", NULL);\n'
        "    score += bad_encoded_ascii == NULL && PyErr_Occurred() != NULL ? 16000000 : 0;\n"
        "    PyErr_Clear();\n"
        "    Py_XDECREF(bad_encoded_ascii);\n"
        "    Py_XDECREF(bad_ascii);\n"
        "    Py_DECREF(non_ascii);\n"
        "    Py_DECREF(substring);\n"
        "    Py_DECREF(replaced);\n"
        "    Py_DECREF(replacement);\n"
        "    Py_DECREF(latin1);\n"
        "    Py_DECREF(encoded_utf8);\n"
        "    Py_DECREF(ascii);\n"
        "    Py_DECREF(utf8);\n"
        "    Py_DECREF(joined);\n"
        "    Py_DECREF(right);\n"
        "    Py_DECREF(left);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_unicode_macro_score(PyObject *Py_UNUSED(self), PyObject *Py_UNUSED(args)) {\n"
        "    Py_UCS4 space = ' ';\n"
        "    Py_UCS4 digit = '7';\n"
        "    Py_UCS4 lower = 'a';\n"
        "    Py_UCS4 upper = 'A';\n"
        "    Py_UCS4 other = '?';\n"
        '    PyObject *text = PyUnicode_FromString("abcd");\n'
        "    long score = 0;\n"
        "    if (text == NULL) return NULL;\n"
        "    score += PyUnicode_GET_LENGTH(text) == 4 ? 1 : 0;\n"
        "    score += Py_UNICODE_ISSPACE(space) ? 10 : 0;\n"
        "    score += Py_UNICODE_ISDIGIT(digit) ? 100 : 0;\n"
        "    score += Py_UNICODE_ISDECIMAL(digit) ? 1000 : 0;\n"
        "    score += Py_UNICODE_ISNUMERIC(digit) ? 10000 : 0;\n"
        "    score += Py_UNICODE_ISLOWER(lower) ? 100000 : 0;\n"
        "    score += Py_UNICODE_ISUPPER(upper) ? 1000000 : 0;\n"
        "    score += Py_UNICODE_ISTITLE(upper) ? 10000000 : 0;\n"
        "    score += Py_UNICODE_ISALPHA(lower) && Py_UNICODE_ISALPHA(upper) ? 100000000 : 0;\n"
        "    score += Py_UNICODE_ISALNUM(lower) && Py_UNICODE_ISALNUM(digit) && !Py_UNICODE_ISALNUM(other) ? 1000000000 : 0;\n"
        "    Py_DECREF(text);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_unicode_kind_score(PyObject *Py_UNUSED(self), PyObject *Py_UNUSED(args)) {\n"
        "    Py_UCS1 ucs1[] = {'A', 0xe9};\n"
        "    Py_UCS2 ucs2[] = {0x03a9, 'b'};\n"
        "    Py_UCS4 ucs4[] = {'A', 0x1f600};\n"
        "    PyObject *text1 = NULL;\n"
        "    PyObject *text2 = NULL;\n"
        "    PyObject *text4 = NULL;\n"
        "    PyObject *find_text = NULL;\n"
        "    PyObject *find_needle = NULL;\n"
        "    PyObject *empty = NULL;\n"
        "    PyObject *ordinal = NULL;\n"
        "    PyObject *bad = NULL;\n"
        "    Py_UCS4 stack_buf[3] = {0, 0, 0};\n"
        "    Py_UCS4 tiny_buf[1] = {0};\n"
        "    Py_UCS4 *copy = NULL;\n"
        "    const char *raw = NULL;\n"
        "    Py_ssize_t n = 0;\n"
        "    long score = 0;\n"
        "    score += sizeof(Py_UCS1) == 1 && sizeof(Py_UCS2) == 2 && sizeof(Py_UCS4) == 4 && PyUnicode_1BYTE_KIND == 1 && PyUnicode_2BYTE_KIND == 2 && PyUnicode_4BYTE_KIND == 4 ? 1 : 0;\n"
        "    text1 = PyUnicode_FromKindAndData(PyUnicode_1BYTE_KIND, ucs1, 2);\n"
        "    if (text1 == NULL) return NULL;\n"
        "    raw = PyUnicode_AsUTF8AndSize(text1, &n);\n"
        "    score += raw != NULL && n == 3 && PyUnicode_GetLength(text1) == 2 && (unsigned char)raw[1] == 0xc3 && (unsigned char)raw[2] == 0xa9 ? 10 : 0;\n"
        "    text2 = PyUnicode_FromKindAndData(PyUnicode_2BYTE_KIND, ucs2, 2);\n"
        "    if (text2 == NULL) return NULL;\n"
        "    raw = PyUnicode_AsUTF8AndSize(text2, &n);\n"
        "    score += raw != NULL && n == 3 && PyUnicode_GetLength(text2) == 2 && (unsigned char)raw[0] == 0xce && (unsigned char)raw[1] == 0xa9 && raw[2] == 'b' ? 100 : 0;\n"
        "    text4 = PyUnicode_FromKindAndData(PyUnicode_4BYTE_KIND, ucs4, 2);\n"
        "    if (text4 == NULL) return NULL;\n"
        "    raw = PyUnicode_AsUTF8AndSize(text4, &n);\n"
        "    score += raw != NULL && n == 5 && PyUnicode_GetLength(text4) == 2 && raw[0] == 'A' && (unsigned char)raw[1] == 0xf0 && (unsigned char)raw[4] == 0x80 ? 1000 : 0;\n"
        "    score += PyUnicode_AsUCS4(text4, stack_buf, 3, 1) == stack_buf && stack_buf[0] == 'A' && stack_buf[1] == 0x1f600 && stack_buf[2] == 0 ? 10000 : 0;\n"
        "    copy = PyUnicode_AsUCS4Copy(text2);\n"
        "    if (copy == NULL) return NULL;\n"
        "    score += copy[0] == 0x03a9 && copy[1] == 'b' && copy[2] == 0 ? 100000 : 0;\n"
        "    PyMem_Free(copy);\n"
        "    copy = NULL;\n"
        "    score += PyUnicode_AsUCS4(text4, tiny_buf, 1, 1) == NULL && PyErr_ExceptionMatches(PyExc_SystemError) ? 1000000 : 0;\n"
        "    PyErr_Clear();\n"
        "    ordinal = PyUnicode_FromOrdinal(0x2665);\n"
        "    if (ordinal == NULL) return NULL;\n"
        "    score += PyUnicode_GetLength(ordinal) == 1 && PyUnicode_ReadChar(ordinal, 0) == 0x2665 ? 100000000 : 0;\n"
        "    score += PyUnicode_ReadChar(text4, 1) == 0x1f600 && PyUnicode_ReadChar(text2, 0) == 0x03a9 ? 1000000000 : 0;\n"
        '    find_text = PyUnicode_FromString("aba");\n'
        '    find_needle = PyUnicode_FromString("a");\n'
        '    empty = PyUnicode_FromString("");\n'
        "    if (find_text == NULL || find_needle == NULL || empty == NULL) return NULL;\n"
        "    score += PyUnicode_FindChar(text4, 'A', 0, 10, 1) == 0 && PyUnicode_FindChar(text4, 0x1f600, 0, 10, 1) == 1 && PyUnicode_FindChar(find_text, 'a', 0, 3, -1) == 2 && PyUnicode_FindChar(text4, 'Z', 0, 10, 1) == -1 ? 10000000000L : 0;\n"
        "    score += PyUnicode_Find(find_text, find_needle, 0, 3, 1) == 0 && PyUnicode_Find(find_text, find_needle, 0, 3, -1) == 2 && PyUnicode_Find(find_text, text2, 0, 3, 1) == -1 ? 1000000000000L : 0;\n"
        "    score += PyUnicode_Count(find_text, find_needle, 0, 3) == 2 && PyUnicode_Count(find_text, empty, 0, 3) == 4 ? 10000000000000L : 0;\n"
        "    score += PyUnicode_ReadChar(text4, 2) == (Py_UCS4)-1 && PyErr_ExceptionMatches(PyExc_IndexError) ? 100000000000L : 0;\n"
        "    PyErr_Clear();\n"
        "    bad = PyUnicode_FromKindAndData(3, ucs1, 1);\n"
        "    score += bad == NULL && PyErr_ExceptionMatches(PyExc_ValueError) ? 10000000 : 0;\n"
        "    PyErr_Clear();\n"
        "    Py_XDECREF(copy);\n"
        "    Py_XDECREF(bad);\n"
        "    Py_DECREF(ordinal);\n"
        "    Py_DECREF(empty);\n"
        "    Py_DECREF(find_needle);\n"
        "    Py_DECREF(find_text);\n"
        "    Py_DECREF(text4);\n"
        "    Py_DECREF(text2);\n"
        "    Py_DECREF(text1);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_float_bool_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    PyObject *flt = PyFloat_FromDouble(3.5);\n"
        "    PyObject *truth = PyBool_FromLong(1);\n"
        "    PyObject *falsehood = PyBool_FromLong(0);\n"
        "    if (flt == NULL || truth == NULL || falsehood == NULL) return NULL;\n"
        "    long score = (long)(PyFloat_AsDouble(flt) * 2.0);\n"
        "    score += PyObject_IsTrue(truth) * 10;\n"
        "    score += PyObject_IsTrue(falsehood);\n"
        "    Py_DECREF(flt);\n"
        "    Py_DECREF(truth);\n"
        "    Py_DECREF(falsehood);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_scalar_complex_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    PyObject *flt = PyFloat_FromDouble(2.5);\n"
        "    PyObject *truth = PyBool_FromLong(1);\n"
        "    PyObject *z = PyComplex_FromDoubles(3.0, 4.0);\n"
        "    Py_complex raw;\n"
        "    Py_complex made;\n"
        "    Py_complex bad;\n"
        "    PyObject *from_c = NULL;\n"
        "    PyObject *text = NULL;\n"
        "    long score = 0;\n"
        "    if (flt == NULL || truth == NULL || z == NULL) return NULL;\n"
        "    score += PyFloat_Check(flt) && PyFloat_CheckExact(flt) && !PyFloat_Check(truth) ? 1 : 0;\n"
        "    score += PyBool_Check(truth) && !PyBool_Check(flt) ? 10 : 0;\n"
        "    score += PyComplex_Check(z) && PyComplex_CheckExact(z) && !PyComplex_Check(flt) ? 100 : 0;\n"
        "    score += PyComplex_RealAsDouble(z) == 3.0 ? 1000 : 0;\n"
        "    score += PyComplex_ImagAsDouble(z) == 4.0 ? 10000 : 0;\n"
        "    raw = PyComplex_AsCComplex(z);\n"
        "    score += raw.real == 3.0 && raw.imag == 4.0 ? 100000 : 0;\n"
        "    score += PyComplex_RealAsDouble(flt) == 2.5 && PyComplex_ImagAsDouble(flt) == 0.0 ? 1000000 : 0;\n"
        "    made.real = 5.0;\n"
        "    made.imag = 6.0;\n"
        "    from_c = PyComplex_FromCComplex(made);\n"
        "    if (from_c == NULL) return NULL;\n"
        "    score += PyComplex_RealAsDouble(from_c) == 5.0 && PyComplex_ImagAsDouble(from_c) == 6.0 ? 10000000 : 0;\n"
        '    text = PyUnicode_FromString("x");\n'
        "    if (text == NULL) return NULL;\n"
        "    bad = PyComplex_AsCComplex(text);\n"
        "    score += bad.real == -1.0 && bad.imag == 0.0 && PyErr_ExceptionMatches(PyExc_TypeError) ? 100000000 : 0;\n"
        "    PyErr_Clear();\n"
        "    Py_DECREF(text);\n"
        "    Py_DECREF(from_c);\n"
        "    Py_DECREF(z);\n"
        "    Py_DECREF(truth);\n"
        "    Py_DECREF(flt);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_set_attr(PyObject *self, PyObject *args) {\n"
        "    PyObject *obj = NULL;\n"
        "    (void)self;\n"
        '    if (!PyArg_ParseTuple(args, "O", &obj)) return NULL;\n'
        "    PyObject *value = PyLong_FromLong(64);\n"
        "    if (value == NULL) return NULL;\n"
        '    if (PyObject_SetAttrString(obj, "answer", value) != 0) {\n'
        "        Py_DECREF(value);\n"
        "        return NULL;\n"
        "    }\n"
        "    Py_DECREF(value);\n"
        '    return PyObject_GetAttrString(obj, "answer");\n'
        "}\n"
        "\n"
        "static PyObject *objectapi_has_attr(PyObject *self, PyObject *args) {\n"
        "    PyObject *obj = NULL;\n"
        "    const char *name = NULL;\n"
        "    (void)self;\n"
        '    if (!PyArg_ParseTuple(args, "Os", &obj, &name)) return NULL;\n'
        "    return PyLong_FromLong(PyObject_HasAttrString(obj, name));\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_format_error(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        '    PyObject *name = PyUnicode_FromString("thing");\n'
        "    if (name == NULL) return NULL;\n"
        '    PyErr_Format(PyExc_ValueError, "bad %s %ld %.5R", "value", 7L, name);\n'
        "    Py_DECREF(name);\n"
        "    int ok = PyErr_Occurred() != NULL;\n"
        "    PyErr_Clear();\n"
        "    return PyLong_FromLong(ok);\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_err_format_v(PyObject *type, const char *format, ...) {\n"
        "    __builtin_va_list ap;\n"
        "    __builtin_va_start(ap, format);\n"
        "    PyObject *result = PyErr_FormatV(type, format, ap);\n"
        "    __builtin_va_end(ap);\n"
        "    return result;\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_warning_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        '    score += PyErr_WarnEx(PyExc_UserWarning, "heads up", 1) == 0 ? 100 : 0;\n'
        '    score += PyErr_WarnFormat(PyExc_RuntimeWarning, 1, "warn %s %ld", "value", 7L) == 0 ? 10000 : 0;\n'
        "    score += PyErr_Occurred() == NULL ? 10 : 0;\n"
        '    PyErr_SetString(PyExc_RuntimeError, "ignored");\n'
        "    PyErr_WriteUnraisable(Py_None);\n"
        "    score += PyErr_Occurred() == NULL ? 1 : 0;\n"
        "    score += PyExc_Warning != NULL && PyExc_RuntimeWarning != NULL && PyExc_DeprecationWarning != NULL && PyExc_FutureWarning != NULL ? 1000 : 0;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_gil_state_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    score += Py_IsInitialized() ? 1 : 0;\n"
        "    PyGILState_STATE state = PyGILState_Ensure();\n"
        "    score += PyGILState_Check() ? 10 : 0;\n"
        "    PyGILState_Release(state);\n"
        "    score += PyGILState_Check() ? 100 : 0;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_error_helper_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    PyErr_SetNone(PyExc_MemoryError);\n"
        "    score += PyErr_ExceptionMatches(PyExc_MemoryError) ? 10 : 0;\n"
        "    PyErr_Clear();\n"
        "    score += PyErr_Occurred() == NULL ? 1 : 0;\n"
        "    PyErr_BadInternalCall();\n"
        "    score += PyErr_ExceptionMatches(PyExc_SystemError) ? 100 : 0;\n"
        "    PyErr_Clear();\n"
        "    score += PyErr_Occurred() == NULL ? 1000 : 0;\n"
        "    score += PyErr_SetFromErrno(PyExc_OSError) == NULL && PyErr_ExceptionMatches(PyExc_OSError) ? 10000 : 0;\n"
        "    PyErr_Clear();\n"
        '    PyObject *filename = PyUnicode_FromString("missing.dat");\n'
        "    if (filename == NULL) return NULL;\n"
        "    score += PyErr_SetFromErrnoWithFilenameObject(PyExc_OSError, filename) == NULL && PyErr_ExceptionMatches(PyExc_OSError) ? 100000 : 0;\n"
        "    Py_DECREF(filename);\n"
        "    PyErr_Clear();\n"
        '    score += objectapi_err_format_v(PyExc_ValueError, "bad %s %ld", "value", 9L) == NULL && PyErr_ExceptionMatches(PyExc_ValueError) ? 1000000 : 0;\n'
        "    PyErr_Clear();\n"
        "    score += PyErr_CheckSignals() == 0 ? 10000000 : 0;\n"
        '    PyErr_SetString(PyExc_RuntimeError, "printed error");\n'
        "    PyErr_Print();\n"
        "    score += PyErr_Occurred() == NULL ? 100000000 : 0;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_exception_global_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    score += PyExc_BaseException != NULL && PyExc_Exception != NULL && PyExc_ArithmeticError != NULL && PyExc_LookupError != NULL && PyExc_OSError != NULL && PyExc_IOError != NULL && PyExc_AssertionError != NULL && PyExc_StopIteration != NULL && PyExc_StopAsyncIteration != NULL && PyExc_ZeroDivisionError != NULL && PyExc_ReferenceError != NULL && PyExc_BufferError != NULL && PyExc_ImportError != NULL && PyExc_ModuleNotFoundError != NULL && PyExc_ImportWarning != NULL && PyExc_FloatingPointError != NULL && PyExc_RecursionError != NULL && PyExc_UnicodeDecodeError != NULL ? 1 : 0;\n"
        "    score += PyExc_OSError == PyExc_IOError ? 2 : 0;\n"
        '    PyErr_SetString(PyExc_OSError, "os");\n'
        "    score += PyErr_ExceptionMatches(PyExc_OSError) ? 4 : 0;\n"
        "    PyErr_Clear();\n"
        '    PyErr_SetString(PyExc_IOError, "io");\n'
        "    score += PyErr_ExceptionMatches(PyExc_OSError) && PyErr_ExceptionMatches(PyExc_IOError) ? 8 : 0;\n"
        "    PyErr_Clear();\n"
        '    PyErr_SetString(PyExc_ZeroDivisionError, "zero");\n'
        "    score += PyErr_ExceptionMatches(PyExc_ArithmeticError) && PyErr_ExceptionMatches(PyExc_ZeroDivisionError) ? 16 : 0;\n"
        "    PyErr_Clear();\n"
        "    PyErr_SetNone(PyExc_StopIteration);\n"
        "    score += PyErr_ExceptionMatches(PyExc_Exception) && PyErr_ExceptionMatches(PyExc_StopIteration) ? 32 : 0;\n"
        "    PyErr_Clear();\n"
        '    PyErr_SetString(PyExc_AssertionError, "assert");\n'
        "    score += PyErr_ExceptionMatches(PyExc_AssertionError) ? 64 : 0;\n"
        "    PyErr_Clear();\n"
        '    PyErr_SetString(PyExc_ReferenceError, "ref");\n'
        "    score += PyErr_ExceptionMatches(PyExc_ReferenceError) ? 128 : 0;\n"
        "    PyErr_Clear();\n"
        '    PyErr_SetString(PyExc_ImportError, "import");\n'
        "    score += PyErr_Occurred() != NULL ? 256 : 0;\n"
        "    PyErr_Clear();\n"
        '    PyErr_SetString(PyExc_BufferError, "buffer");\n'
        "    score += PyErr_Occurred() != NULL ? 512 : 0;\n"
        "    PyErr_Clear();\n"
        '    PyErr_SetString(PyExc_RecursionError, "recursion");\n'
        "    score += PyErr_ExceptionMatches(PyExc_RuntimeError) ? 1024 : 0;\n"
        "    PyErr_Clear();\n"
        '    PyErr_SetString(PyExc_UnicodeDecodeError, "unicode");\n'
        "    score += PyErr_ExceptionMatches(PyExc_ValueError) ? 2048 : 0;\n"
        "    PyErr_Clear();\n"
        '    PyErr_SetString(PyExc_FloatingPointError, "float");\n'
        "    score += PyErr_ExceptionMatches(PyExc_ArithmeticError) ? 4096 : 0;\n"
        "    PyErr_Clear();\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_module_dict_magic(PyObject *self, PyObject *args) {\n"
        "    (void)args;\n"
        "    PyObject *dict = PyModule_GetDict(self);\n"
        "    if (dict == NULL) return NULL;\n"
        '    PyObject *magic = PyDict_GetItemString(dict, "MAGIC");\n'
        "    if (magic == NULL) return NULL;\n"
        "    return PyLong_FromLong(PyLong_AsLong(magic) + 1);\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_bump_module_global(PyObject *self, PyObject *args) {\n"
        "    (void)args;\n"
        "    PyObject *dict = PyModule_GetDict(self);\n"
        "    if (dict == NULL) return NULL;\n"
        '    PyObject *current = PyDict_GetItemString(dict, "SEEN");\n'
        "    long next = current == NULL ? 1 : PyLong_AsLong(current) + 1;\n"
        "    PyObject *value = PyLong_FromLong(next);\n"
        "    if (value == NULL) return NULL;\n"
        '    if (PyDict_SetItemString(dict, "SEEN", value) != 0) {\n'
        "        Py_DECREF(value);\n"
        "        return NULL;\n"
        "    }\n"
        "    Py_DECREF(value);\n"
        "    return PyLong_FromLong(next);\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_delete_module_global(PyObject *self, PyObject *args) {\n"
        "    (void)args;\n"
        "    PyObject *dict = PyModule_GetDict(self);\n"
        "    if (dict == NULL) return NULL;\n"
        '    if (PyDict_DelItemString(dict, "SEEN") != 0) return NULL;\n'
        '    return PyLong_FromLong(PyDict_GetItemString(dict, "SEEN") == NULL ? 1 : 0);\n'
        "}\n"
        "\n"
        "static PyObject *objectapi_str_repr_compare(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        '    PyObject *text = PyUnicode_FromString("abc");\n'
        "    PyObject *as_str = NULL;\n"
        "    PyObject *as_repr = NULL;\n"
        "    PyObject *raw = NULL;\n"
        "    PyObject *as_bytes = NULL;\n"
        "    PyObject *format_spec = NULL;\n"
        "    PyObject *formatted = NULL;\n"
        "    PyObject *rich = NULL;\n"
        "    long score = 0;\n"
        "    if (text == NULL) return NULL;\n"
        "    as_str = PyObject_Str(text);\n"
        "    as_repr = PyObject_Repr(text);\n"
        '    raw = PyBytes_FromStringAndSize("xy", 2);\n'
        '    format_spec = PyUnicode_FromString(">5");\n'
        "    if (as_str == NULL || as_repr == NULL || raw == NULL || format_spec == NULL) return NULL;\n"
        "    as_bytes = PyObject_Bytes(raw);\n"
        "    if (as_bytes == NULL) return NULL;\n"
        "    formatted = PyObject_Format(text, format_spec);\n"
        "    if (formatted == NULL) return NULL;\n"
        "    score += PyObject_RichCompareBool(text, as_str, Py_EQ) * 10;\n"
        "    rich = PyObject_RichCompare(text, as_str, Py_EQ);\n"
        "    if (rich == NULL) return NULL;\n"
        "    score += PyObject_IsTrue(rich) * 100;\n"
        "    const char *repr_raw = PyUnicode_AsUTF8(as_repr);\n"
        "    if (repr_raw != NULL && repr_raw[0] == '\\'') score += 1;\n"
        "    char *bytes_raw = PyBytes_AsString(as_bytes);\n"
        "    score += PyBytes_Size(as_bytes) == 2 ? 1000 : 0;\n"
        "    score += bytes_raw != NULL && bytes_raw[0] == 'x' && bytes_raw[1] == 'y' ? 10000 : 0;\n"
        "    const char *formatted_raw = PyUnicode_AsUTF8(formatted);\n"
        "    score += formatted_raw != NULL && formatted_raw[0] == ' ' && formatted_raw[1] == ' ' && formatted_raw[2] == 'a' ? 100000 : 0;\n"
        "    Py_DECREF(rich);\n"
        "    Py_DECREF(formatted);\n"
        "    Py_DECREF(format_spec);\n"
        "    Py_DECREF(as_bytes);\n"
        "    Py_DECREF(raw);\n"
        "    Py_DECREF(as_repr);\n"
        "    Py_DECREF(as_str);\n"
        "    Py_DECREF(text);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_print_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        '    PyObject *text = PyUnicode_FromString("abc");\n'
        "    FILE *fp = NULL;\n"
        "    char buf[16];\n"
        "    size_t n = 0;\n"
        "    long score = 0;\n"
        "    if (text == NULL) return NULL;\n"
        "    fp = tmpfile();\n"
        "    if (fp == NULL) {\n"
        "        Py_DECREF(text);\n"
        '        PyErr_SetString(PyExc_OSError, "tmpfile failed");\n'
        "        return NULL;\n"
        "    }\n"
        "    if (PyObject_Print(text, fp, Py_PRINT_RAW) != 0) {\n"
        "        fclose(fp);\n"
        "        Py_DECREF(text);\n"
        "        return NULL;\n"
        "    }\n"
        "    if (PyObject_Print(text, fp, 0) != 0) {\n"
        "        fclose(fp);\n"
        "        Py_DECREF(text);\n"
        "        return NULL;\n"
        "    }\n"
        "    fflush(fp);\n"
        "    fseek(fp, 0, SEEK_SET);\n"
        "    n = fread(buf, 1, sizeof(buf), fp);\n"
        "    fclose(fp);\n"
        "    Py_DECREF(text);\n"
        "    score += n == 8 ? 1 : 0;\n"
        "    score += n >= 3 && buf[0] == 'a' && buf[2] == 'c' ? 10 : 0;\n"
        "    score += n >= 8 && buf[3] == '\\'' && buf[7] == '\\'' ? 100 : 0;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_unsigned_sum(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    PyObject *a = PyLong_FromUnsignedLong(5UL);\n"
        "    PyObject *b = PyLong_FromUnsignedLongLong(6ULL);\n"
        "    if (a == NULL || b == NULL) return NULL;\n"
        "    long total = PyLong_AsLong(a) + PyLong_AsLong(b);\n"
        "    Py_DECREF(a);\n"
        "    Py_DECREF(b);\n"
        "    return PyLong_FromLong(total);\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_import_self_magic(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        '    PyObject *module = PyImport_ImportModule("objectapi");\n'
        "    if (module == NULL) return NULL;\n"
        '    PyObject *magic = PyObject_GetAttrString(module, "MAGIC");\n'
        "    Py_DECREF(module);\n"
        "    if (magic == NULL) return NULL;\n"
        "    long value = PyLong_AsLong(magic);\n"
        "    Py_DECREF(magic);\n"
        "    return PyLong_FromLong(value);\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_sequence_score(PyObject *self, PyObject *args) {\n"
        "    (void)args;\n"
        "    PyObject *a = PyLong_FromLong(3);\n"
        "    PyObject *b = PyLong_FromLong(4);\n"
        "    PyObject *tuple = PyTuple_Pack(2, a, b);\n"
        "    PyObject *list = NULL;\n"
        "    PyObject *tuple2 = NULL;\n"
        "    PyObject *fast = NULL;\n"
        "    PyObject *item = NULL;\n"
        "    PyObject *replacement = NULL;\n"
        "    PyObject *concat = NULL;\n"
        "    PyObject *repeat = NULL;\n"
        "    PyObject *inplace_concat = NULL;\n"
        "    PyObject *inplace_repeat = NULL;\n"
        "    PyObject *bytes = NULL;\n"
        "    PyObject *interned = NULL;\n"
        "    long score = 0;\n"
        "    Py_DECREF(a);\n"
        "    Py_DECREF(b);\n"
        "    if (tuple == NULL) return NULL;\n"
        "    list = PySequence_List(tuple);\n"
        "    tuple2 = PySequence_Tuple(list);\n"
        '    fast = PySequence_Fast(list, "expected sequence");\n'
        '    bytes = PyBytes_FromStringAndSize("x", 1);\n'
        '    interned = PyUnicode_InternFromString("name");\n'
        "    if (list == NULL || tuple2 == NULL || fast == NULL || bytes == NULL || interned == NULL) return NULL;\n"
        "    PyObject *dict = PyModule_GetDict(self);\n"
        "    if (dict == NULL) return NULL;\n"
        "    score += PyTuple_Check(tuple) + PyTuple_CheckExact(tuple);\n"
        "    score += PyList_Check(list) + PyList_CheckExact(list);\n"
        "    score += PyDict_Check(dict) + PyDict_CheckExact(dict);\n"
        "    score += PyBytes_Check(bytes) + PyBytes_CheckExact(bytes);\n"
        "    score += PyUnicode_Check(interned) + PyUnicode_CheckExact(interned);\n"
        "    score += PySequence_Check(list) + PySequence_Check(tuple2);\n"
        "    score += PySequence_Size(list);\n"
        "    score += PySequence_Length(tuple2);\n"
        "    item = PySequence_GetItem(tuple2, 1);\n"
        "    if (item == NULL) return NULL;\n"
        "    score += PyLong_AsLong(item);\n"
        "    PyObject **items = PySequence_Fast_ITEMS(fast);\n"
        "    if (items == NULL) return NULL;\n"
        "    score += PyLong_AsLong(items[0]);\n"
        "    score += PySequence_Fast_GET_SIZE(fast);\n"
        "    score += PyTuple_Check(tuple2);\n"
        "    Py_DECREF(item);\n"
        "    item = NULL;\n"
        "    replacement = PyLong_FromLong(9);\n"
        "    if (replacement == NULL) return NULL;\n"
        "    if (PySequence_SetItem(list, 0, replacement) != 0) return NULL;\n"
        "    Py_DECREF(replacement);\n"
        "    item = PySequence_GetItem(list, 0);\n"
        "    if (item == NULL) return NULL;\n"
        "    score += PyLong_AsLong(item) == 9 ? 100 : 0;\n"
        "    Py_DECREF(item);\n"
        "    concat = PySequence_Concat(tuple, tuple2);\n"
        "    if (concat == NULL) return NULL;\n"
        "    score += PyTuple_Check(concat) && PySequence_Size(concat) == 4 ? 1000 : 0;\n"
        "    Py_DECREF(concat);\n"
        "    repeat = PySequence_Repeat(tuple, 2);\n"
        "    if (repeat == NULL) return NULL;\n"
        "    score += PyTuple_Check(repeat) && PySequence_Size(repeat) == 4 ? 10000 : 0;\n"
        "    Py_DECREF(repeat);\n"
        "    inplace_concat = PySequence_InPlaceConcat(tuple, tuple2);\n"
        "    if (inplace_concat == NULL) return NULL;\n"
        "    score += PyTuple_Check(inplace_concat) && PySequence_Size(inplace_concat) == 4 ? 20000 : 0;\n"
        "    Py_DECREF(inplace_concat);\n"
        "    inplace_repeat = PySequence_InPlaceRepeat(bytes, 3);\n"
        "    if (inplace_repeat == NULL) return NULL;\n"
        "    score += PyBytes_Check(inplace_repeat) && PyBytes_Size(inplace_repeat) == 3 ? 40000 : 0;\n"
        "    Py_DECREF(inplace_repeat);\n"
        "    Py_DECREF(interned);\n"
        "    Py_DECREF(bytes);\n"
        "    Py_DECREF(fast);\n"
        "    Py_DECREF(tuple2);\n"
        "    Py_DECREF(list);\n"
        "    Py_DECREF(tuple);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_iter_contains_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    PyObject *list = PyList_New(2);\n"
        "    PyObject *dict = PyDict_New();\n"
        '    PyObject *key = PyUnicode_FromString("k");\n'
        "    PyObject *needle = PyLong_FromLong(4);\n"
        "    PyObject *missing = PyLong_FromLong(5);\n"
        "    PyObject *iter = NULL;\n"
        "    PyObject *item = NULL;\n"
        "    PyObject *self_iter = NULL;\n"
        "    int status = 0;\n"
        "    long score = 0;\n"
        "    if (list == NULL || dict == NULL || key == NULL || needle == NULL || missing == NULL) return NULL;\n"
        "    if (PyList_SetItem(list, 0, PyLong_FromLong(3)) != 0) return NULL;\n"
        "    if (PyList_SetItem(list, 1, PyLong_FromLong(4)) != 0) return NULL;\n"
        "    iter = PyObject_GetIter(list);\n"
        "    if (iter == NULL) return NULL;\n"
        "    score += PyIter_Check(iter) ? 1 : 0;\n"
        "    score += !PyIter_Check(list) ? 10 : 0;\n"
        "    item = PyIter_Next(iter);\n"
        "    if (item == NULL) return NULL;\n"
        "    score += PyLong_AsLong(item) == 3 ? 100 : 0;\n"
        "    Py_DECREF(item);\n"
        "    item = PyIter_Next(iter);\n"
        "    if (item == NULL) return NULL;\n"
        "    score += PyLong_AsLong(item) == 4 ? 1000 : 0;\n"
        "    Py_DECREF(item);\n"
        "    item = PyIter_Next(iter);\n"
        "    score += item == NULL && PyErr_Occurred() == NULL ? 10000 : 0;\n"
        "    Py_DECREF(iter);\n"
        "    iter = PyObject_GetIter(list);\n"
        "    if (iter == NULL) return NULL;\n"
        "    status = PyIter_NextItem(iter, &item);\n"
        "    if (status != 1 || item == NULL) return NULL;\n"
        "    score += PyLong_AsLong(item) == 3 ? 100000000 : 0;\n"
        "    Py_DECREF(item);\n"
        "    status = PyIter_NextItem(iter, &item);\n"
        "    if (status != 1 || item == NULL) return NULL;\n"
        "    score += PyLong_AsLong(item) == 4 ? 200000000 : 0;\n"
        "    Py_DECREF(item);\n"
        "    item = needle;\n"
        "    status = PyIter_NextItem(iter, &item);\n"
        "    score += status == 0 && item == NULL && PyErr_Occurred() == NULL ? 400000000 : 0;\n"
        "    self_iter = PyObject_SelfIter(iter);\n"
        "    if (self_iter == NULL) return NULL;\n"
        "    score += self_iter == iter ? 20000000 : 0;\n"
        "    Py_DECREF(self_iter);\n"
        "    item = needle;\n"
        "    status = PyIter_NextItem(list, &item);\n"
        "    score += status == -1 && item == NULL && PyErr_Occurred() != NULL ? 800000000 : 0;\n"
        "    PyErr_Clear();\n"
        "    score += PySequence_Contains(list, needle) == 1 ? 100000 : 0;\n"
        "    score += PySequence_Contains(list, missing) == 0 ? 1000000 : 0;\n"
        "    if (PyDict_SetItem(dict, key, needle) != 0) return NULL;\n"
        "    score += PySequence_Contains(dict, key) == 1 ? 10000000 : 0;\n"
        "    Py_DECREF(iter);\n"
        "    Py_DECREF(missing);\n"
        "    Py_DECREF(needle);\n"
        "    Py_DECREF(key);\n"
        "    Py_DECREF(dict);\n"
        "    Py_DECREF(list);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_collection_views_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    PyObject *dict = PyDict_New();\n"
        '    PyObject *key1 = PyUnicode_FromString("a");\n'
        '    PyObject *key2 = PyUnicode_FromString("b");\n'
        "    PyObject *value1 = PyLong_FromLong(11);\n"
        "    PyObject *value2 = PyLong_FromLong(22);\n"
        "    PyObject *keys = NULL;\n"
        "    PyObject *values = NULL;\n"
        "    PyObject *items = NULL;\n"
        "    PyObject *map_keys = NULL;\n"
        "    PyObject *map_values = NULL;\n"
        "    PyObject *map_items = NULL;\n"
        "    PyObject *key_tuple = NULL;\n"
        "    PyObject *item0 = NULL;\n"
        "    PyObject *item0_key = NULL;\n"
        "    PyObject *item0_value = NULL;\n"
        "    PyObject *dict_value = NULL;\n"
        "    long score = 0;\n"
        "    if (dict == NULL || key1 == NULL || key2 == NULL || value1 == NULL || value2 == NULL) return NULL;\n"
        "    if (PyDict_SetItem(dict, key1, value1) != 0) return NULL;\n"
        "    if (PyDict_SetItem(dict, key2, value2) != 0) return NULL;\n"
        "    keys = PyDict_Keys(dict);\n"
        "    values = PyDict_Values(dict);\n"
        "    items = PyDict_Items(dict);\n"
        "    map_keys = PyMapping_Keys(dict);\n"
        "    map_values = PyMapping_Values(dict);\n"
        "    map_items = PyMapping_Items(dict);\n"
        "    if (keys == NULL || values == NULL || items == NULL || map_keys == NULL || map_values == NULL || map_items == NULL) return NULL;\n"
        "    key_tuple = PyList_AsTuple(keys);\n"
        "    if (key_tuple == NULL) return NULL;\n"
        "    score += PyList_Check(keys) ? 1 : 0;\n"
        "    score += PyList_Size(keys) == 2 ? 10 : 0;\n"
        "    score += PyList_Size(values) == 2 ? 100 : 0;\n"
        "    score += PyList_Size(items) == 2 ? 1000 : 0;\n"
        "    score += PyTuple_Check(key_tuple) ? 10000 : 0;\n"
        "    score += PyTuple_Size(key_tuple) == 2 ? 100000 : 0;\n"
        "    score += PySequence_Contains(key_tuple, key1) == 1 ? 1000000 : 0;\n"
        "    score += PyList_Size(map_keys) == 2 ? 2 : 0;\n"
        "    score += PyList_Size(map_values) == 2 ? 20 : 0;\n"
        "    score += PyList_Size(map_items) == 2 ? 200 : 0;\n"
        "    score += PyObject_LengthHint(values, 99) == 2 ? 300 : 0;\n"
        "    score += PyObject_LengthHint(value1, 77) == 77 ? 400 : 0;\n"
        "    item0 = PyList_GetItem(items, 0);\n"
        "    if (item0 == NULL) return NULL;\n"
        "    score += PyTuple_Check(item0) && PyTuple_Size(item0) == 2 ? 10000000 : 0;\n"
        "    item0_key = PyTuple_GetItem(item0, 0);\n"
        "    item0_value = PyTuple_GetItem(item0, 1);\n"
        "    if (item0_key == NULL || item0_value == NULL) return NULL;\n"
        "    dict_value = PyDict_GetItem(dict, item0_key);\n"
        "    score += dict_value == item0_value ? 100000000 : 0;\n"
        "    score += PySequence_Contains(values, value2) == 1 ? 1000000000 : 0;\n"
        "    Py_DECREF(map_items);\n"
        "    Py_DECREF(map_values);\n"
        "    Py_DECREF(map_keys);\n"
        "    Py_DECREF(key_tuple);\n"
        "    Py_DECREF(items);\n"
        "    Py_DECREF(values);\n"
        "    Py_DECREF(keys);\n"
        "    Py_DECREF(value2);\n"
        "    Py_DECREF(value1);\n"
        "    Py_DECREF(key2);\n"
        "    Py_DECREF(key1);\n"
        "    Py_DECREF(dict);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_set_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    PyObject *one = PyLong_FromLong(1);\n"
        "    PyObject *two = PyLong_FromLong(2);\n"
        "    PyObject *three = PyLong_FromLong(3);\n"
        "    PyObject *missing = PyLong_FromLong(99);\n"
        "    PyObject *tuple = NULL;\n"
        "    PyObject *set = NULL;\n"
        "    long score = 0;\n"
        "    if (one == NULL || two == NULL || three == NULL || missing == NULL) return NULL;\n"
        "    tuple = PyTuple_Pack(3, one, two, two);\n"
        "    if (tuple == NULL) return NULL;\n"
        "    set = PySet_New(tuple);\n"
        "    if (set == NULL) return NULL;\n"
        "    score += PySet_Check(set) ? 1 : 0;\n"
        "    score += PySet_CheckExact(set) ? 10 : 0;\n"
        "    score += PyAnySet_Check(set) ? 100 : 0;\n"
        "    score += PyAnySet_CheckExact(set) ? 1000 : 0;\n"
        "    score += PySet_Size(set) == 2 ? 10000 : 0;\n"
        "    score += PySet_GET_SIZE(set) == 2 ? 100000 : 0;\n"
        "    score += PySet_Contains(set, two) == 1 ? 1000000 : 0;\n"
        "    score += (PySet_Add(set, three) == 0 && PySet_Contains(set, three) == 1 && PySet_Size(set) == 3) ? 10000000 : 0;\n"
        "    score += (PySet_Discard(set, two) == 1 && PySet_Contains(set, two) == 0 && PySet_Size(set) == 2) ? 100000000 : 0;\n"
        "    score += PySet_Discard(set, missing) == 0 ? 1000000000 : 0;\n"
        "    Py_DECREF(set);\n"
        "    Py_DECREF(tuple);\n"
        "    Py_DECREF(missing);\n"
        "    Py_DECREF(three);\n"
        "    Py_DECREF(two);\n"
        "    Py_DECREF(one);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_attr_hash_score(PyObject *self, PyObject *args) {\n"
        "    (void)args;\n"
        '    PyObject *magic_name = PyUnicode_FromString("MAGIC");\n'
        "    PyObject *magic = PyObject_GetAttr(self, magic_name);\n"
        '    PyObject *attr_name = PyUnicode_FromString("LABEL2");\n'
        '    PyObject *attr_value = PyUnicode_FromString("set");\n'
        '    PyObject *missing_name = PyUnicode_FromString("MISSING_ATTR");\n'
        "    PyObject *got = NULL;\n"
        "    PyObject *method = NULL;\n"
        "    PyObject *optional = NULL;\n"
        "    long score = 0;\n"
        "    if (magic_name == NULL || magic == NULL || attr_name == NULL || attr_value == NULL || missing_name == NULL) return NULL;\n"
        "    score += PyLong_AsLong(magic);\n"
        "    if (PyObject_SetAttr(self, attr_name, attr_value) != 0) return NULL;\n"
        "    got = PyObject_GetAttr(self, attr_name);\n"
        '    method = PyObject_GetAttrString(self, "tuple_sum");\n'
        "    if (got == NULL || method == NULL) return NULL;\n"
        '    score += (PyUnicode_CompareWithASCIIString(got, "set") == 0) ? 1 : 0;\n'
        "    score += (PyUnicode_Compare(got, attr_value) == 0) ? 10 : 0;\n"
        "    score += (PyObject_Hash(got) != -1) ? 100 : 0;\n"
        "    score += PyCallable_Check(method) ? 1000 : 0;\n"
        "    score += PyCallable_Check(self) ? 0 : 10000;\n"
        "    score += PyObject_HasAttr(self, magic_name) ? 100000 : 0;\n"
        "    score += PyObject_HasAttrWithError(self, attr_name) == 1 ? 200000 : 0;\n"
        '    score += PyObject_HasAttrStringWithError(self, "LABEL2") == 1 ? 300000 : 0;\n'
        "    score += PyObject_HasAttrWithError(self, missing_name) == 0 ? 400000 : 0;\n"
        '    score += PyObject_HasAttrStringWithError(self, "MISSING_ATTR") == 0 ? 500000 : 0;\n'
        '    score += PyObject_HasAttrStringWithError(NULL, "x") == -1 && PyErr_ExceptionMatches(PyExc_TypeError) ? 600000 : 0;\n'
        "    PyErr_Clear();\n"
        "    score += PyObject_HasAttr(self, missing_name) == 0 ? 700000 : 0;\n"
        "    score += PyObject_GetOptionalAttr(self, attr_name, &optional) == 1 && optional != NULL ? 800000 : 0;\n"
        "    Py_XDECREF(optional);\n"
        "    optional = NULL;\n"
        '    score += PyObject_GetOptionalAttrString(self, "LABEL2", &optional) == 1 && optional != NULL ? 900000 : 0;\n'
        "    Py_XDECREF(optional);\n"
        "    optional = NULL;\n"
        "    score += PyObject_GetOptionalAttr(self, missing_name, &optional) == 0 && optional == NULL ? 1000000 : 0;\n"
        '    score += PyObject_GetOptionalAttrString(NULL, "x", &optional) == -1 && PyErr_ExceptionMatches(PyExc_TypeError) ? 1100000 : 0;\n'
        "    PyErr_Clear();\n"
        "    Py_DECREF(method);\n"
        "    Py_DECREF(got);\n"
        "    Py_DECREF(missing_name);\n"
        "    Py_DECREF(attr_value);\n"
        "    Py_DECREF(attr_name);\n"
        "    Py_DECREF(magic);\n"
        "    Py_DECREF(magic_name);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_build_mem_kw_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    char *buf = (char *)PyMem_Malloc(3);\n"
        "    char *zero = NULL;\n"
        "    PyObject *built = NULL;\n"
        "    PyObject *kw_args = NULL;\n"
        "    PyObject *kwargs = NULL;\n"
        "    PyObject *name = NULL;\n"
        "    PyObject *value_obj = NULL;\n"
        "    PyObject *owned = NULL;\n"
        "    PyObject *again = NULL;\n"
        "    PyObject *slot = NULL;\n"
        "    PyObject *xslot = NULL;\n"
        "    const char *built_text = NULL;\n"
        '    char *kwlist[] = {"value", "name", NULL};\n'
        "    long value = 0;\n"
        '    const char *text = "unset";\n'
        "    char *raw = NULL;\n"
        "    Py_ssize_t raw_len = 0;\n"
        "    if (buf == NULL) return PyErr_NoMemory();\n"
        "    buf[0] = 'o'; buf[1] = 'k'; buf[2] = '\\0';\n"
        "    buf = (char *)PyMem_Realloc(buf, 4);\n"
        "    if (buf == NULL) return PyErr_NoMemory();\n"
        "    buf[2] = '!'; buf[3] = '\\0';\n"
        "    zero = (char *)PyMem_Calloc(2, 1);\n"
        "    if (zero == NULL) return PyErr_NoMemory();\n"
        '    built = Py_BuildValue("(is#y#)", 7, buf, (Py_ssize_t)3, "AB", (Py_ssize_t)2);\n'
        "    PyMem_Free(buf);\n"
        "    if (built == NULL) return NULL;\n"
        "    score += PyTuple_Check(built) ? 1000 : 0;\n"
        "    score += PyLong_AsLong(PyTuple_GET_ITEM(built, 0));\n"
        "    built_text = PyUnicode_AsUTF8(PyTuple_GET_ITEM(built, 1));\n"
        "    score += built_text[0] == 'o' && built_text[1] == 'k' && built_text[2] == '!' && built_text[3] == '\\0' ? 100 : 0;\n"
        "    if (PyBytes_AsStringAndSize(PyTuple_GET_ITEM(built, 2), &raw, &raw_len) != 0) return NULL;\n"
        "    score += raw_len == 2 && raw[0] == 'A' && raw[1] == 'B' ? 200 : 0;\n"
        "    score += zero[0] == '\\0' && zero[1] == '\\0' ? 1 : 0;\n"
        "    PyMem_Free(zero);\n"
        "    value_obj = PyLong_FromLong(10);\n"
        "    kw_args = PyTuple_Pack(1, value_obj);\n"
        "    Py_DECREF(value_obj);\n"
        "    kwargs = PyDict_New();\n"
        '    name = PyUnicode_FromString("kw");\n'
        "    if (kw_args == NULL || kwargs == NULL || name == NULL) return NULL;\n"
        '    if (PyDict_SetItemString(kwargs, "name", name) != 0) return NULL;\n'
        "    Py_DECREF(name);\n"
        '    if (!PyArg_ParseTupleAndKeywords(kw_args, kwargs, "l|s", kwlist, &value, &text)) return NULL;\n'
        "    score += value;\n"
        "    score += text[0] == 'k' && text[1] == 'w' && text[2] == '\\0' ? 20 : 0;\n"
        "    Py_DECREF(kwargs);\n"
        "    Py_DECREF(kw_args);\n"
        "    owned = PyLong_FromLong(5);\n"
        "    again = Py_NewRef(owned);\n"
        "    score += PyLong_AsLong(again);\n"
        "    Py_DECREF(again);\n"
        "    Py_CLEAR(owned);\n"
        "    score += owned == NULL ? 30 : 0;\n"
        "    score += Py_XNewRef(NULL) == NULL ? 40 : 0;\n"
        "    Py_XDECREF(NULL);\n"
        "    slot = PyLong_FromLong(1);\n"
        "    Py_SETREF(slot, PyLong_FromLong(2));\n"
        "    score += PyLong_AsLong(slot);\n"
        "    Py_DECREF(slot);\n"
        "    xslot = NULL;\n"
        "    Py_XSETREF(xslot, PyLong_FromLong(3));\n"
        "    score += PyLong_AsLong(xslot);\n"
        "    Py_DECREF(xslot);\n"
        "    PyErr_NoMemory();\n"
        "    score += PyErr_Occurred() != NULL ? 50 : 0;\n"
        "    PyErr_Clear();\n"
        "    Py_DECREF(built);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_refcnt_macro_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        '    PyObject *obj = PyUnicode_FromString("rc");\n'
        "    if (obj == NULL) return NULL;\n"
        "    Py_ssize_t refcnt = Py_REFCNT(obj);\n"
        "    long score = 0;\n"
        "    Py_INCREF(obj);\n"
        "    score += Py_REFCNT(obj) == refcnt + 1 ? 1 : 0;\n"
        "    Py_DECREF(obj);\n"
        "    Py_SET_REFCNT(obj, Py_REFCNT(obj));\n"
        "    score += Py_REFCNT(obj) == refcnt ? 10 : 0;\n"
        "    Py_DECREF(obj);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static int objectapi_vsnprintf_probe(char *buffer, size_t size, const char *format, ...) {\n"
        "    __builtin_va_list ap;\n"
        "    __builtin_va_start(ap, format);\n"
        "    int result = PyOS_vsnprintf(buffer, size, format, ap);\n"
        "    __builtin_va_end(ap);\n"
        "    return result;\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_memory_os_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    char formatted[32];\n"
        "    char formatted_v[32];\n"
        "    char *raw = (char *)PyMem_RawMalloc(3);\n"
        "    char *zero = NULL;\n"
        "    char *obj = NULL;\n"
        "    char *obj_zero = NULL;\n"
        "    char *macro_obj = NULL;\n"
        "    char *del_obj = NULL;\n"
        "    char *DEL_obj = NULL;\n"
        "    char *mem_macro = NULL;\n"
        '    int n = PyOS_snprintf(formatted, sizeof(formatted), "%s-%d", "np", 24);\n'
        '    int nv = objectapi_vsnprintf_probe(formatted_v, sizeof(formatted_v), "%s-%d", "os", 7);\n'
        "    score += n == 5 && formatted[0] == 'n' && formatted[4] == '4' ? 1 : 0;\n"
        "    score += nv == 4 && formatted_v[0] == 'o' && formatted_v[3] == '7' ? 10 : 0;\n"
        "    if (raw == NULL) return PyErr_NoMemory();\n"
        "    raw[0] = 'a'; raw[1] = 'b'; raw[2] = '\\0';\n"
        "    raw = (char *)PyMem_RawRealloc(raw, 4);\n"
        "    if (raw == NULL) return PyErr_NoMemory();\n"
        "    raw[2] = 'c'; raw[3] = '\\0';\n"
        "    score += raw[0] == 'a' && raw[2] == 'c' ? 100 : 0;\n"
        "    PyMem_RawFree(raw);\n"
        "    zero = (char *)PyMem_RawCalloc(2, 1);\n"
        "    if (zero == NULL) return PyErr_NoMemory();\n"
        "    score += zero[0] == '\\0' && zero[1] == '\\0' ? 1000 : 0;\n"
        "    PyMem_RawFree(zero);\n"
        "    obj = (char *)PyObject_Malloc(2);\n"
        "    if (obj == NULL) return PyErr_NoMemory();\n"
        "    obj[0] = 'o'; obj[1] = '\\0';\n"
        "    obj = (char *)PyObject_Realloc(obj, 3);\n"
        "    if (obj == NULL) return PyErr_NoMemory();\n"
        "    obj[1] = 'k'; obj[2] = '\\0';\n"
        "    score += obj[0] == 'o' && obj[1] == 'k' ? 10000 : 0;\n"
        "    PyObject_Free(obj);\n"
        "    obj_zero = (char *)PyObject_Calloc(2, 1);\n"
        "    if (obj_zero == NULL) return PyErr_NoMemory();\n"
        "    score += obj_zero[0] == '\\0' && obj_zero[1] == '\\0' ? 100000 : 0;\n"
        "    PyObject_Free(obj_zero);\n"
        "    macro_obj = (char *)PyObject_MALLOC(2);\n"
        "    if (macro_obj == NULL) return PyErr_NoMemory();\n"
        "    macro_obj[0] = 'x'; macro_obj[1] = '\\0';\n"
        "    macro_obj = (char *)PyObject_REALLOC(macro_obj, 3);\n"
        "    if (macro_obj == NULL) return PyErr_NoMemory();\n"
        "    macro_obj[1] = 'y'; macro_obj[2] = '\\0';\n"
        "    score += macro_obj[0] == 'x' && macro_obj[1] == 'y' ? 1000000 : 0;\n"
        "    PyObject_FREE(macro_obj);\n"
        "    del_obj = (char *)PyObject_Malloc(1);\n"
        "    DEL_obj = (char *)PyObject_Malloc(1);\n"
        "    mem_macro = (char *)PyMem_Malloc(1);\n"
        "    if (del_obj == NULL || DEL_obj == NULL || mem_macro == NULL) return PyErr_NoMemory();\n"
        "    PyObject_Del(del_obj);\n"
        "    PyObject_DEL(DEL_obj);\n"
        "    PyMem_FREE(mem_macro);\n"
        "    score += 10000000;\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_pythoncapi_compat_score(PyObject *self, PyObject *args) {\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        '    PyObject *abc = PyUnicode_FromString("abc");\n'
        '    PyObject *abc0def = PyUnicode_FromStringAndSize("abc\\0def", 7);\n'
        "    PyObject *list = PyList_New(2);\n"
        "    PyObject *dict = PyDict_New();\n"
        '    PyObject *key = PyUnicode_FromString("key");\n'
        '    PyObject *value = PyUnicode_FromString("default");\n'
        '    PyObject *other = PyUnicode_FromString("other");\n'
        '    PyObject *null_key = PyUnicode_FromString("null-result");\n'
        "    PyObject *result = NULL;\n"
        "    PyObject *item = NULL;\n"
        "    PyObject *added = NULL;\n"
        "    if (abc == NULL || abc0def == NULL || list == NULL || dict == NULL || key == NULL || value == NULL || other == NULL || null_key == NULL) return NULL;\n"
        '    score += PyUnicode_EqualToUTF8(abc, "abc") ? 1 : 0;\n'
        '    score += !PyUnicode_EqualToUTF8(abc, "ab") ? 10 : 0;\n'
        '    score += PyUnicode_EqualToUTF8AndSize(abc0def, "abc\\0def", 7) ? 100 : 0;\n'
        '    score += !PyUnicode_EqualToUTF8(abc0def, "abc\\0def") ? 1000 : 0;\n'
        "    if (PyList_SetItem(list, 0, PyLong_FromLong(41)) != 0) return NULL;\n"
        "    if (PyList_SetItem(list, 1, PyLong_FromLong(42)) != 0) return NULL;\n"
        "    item = PyList_GetItemRef(list, 1);\n"
        "    if (item == NULL) return NULL;\n"
        "    score += PyLong_AsLong(item) == 42 ? 10000 : 0;\n"
        "    Py_DECREF(item);\n"
        "    if (PyDict_SetDefaultRef(dict, key, value, &result) != 0 || result != value) return NULL;\n"
        "    score += 100000;\n"
        "    Py_DECREF(result);\n"
        "    result = NULL;\n"
        "    if (PyDict_SetDefaultRef(dict, key, other, &result) != 1 || result != value) return NULL;\n"
        "    score += 1000000;\n"
        "    Py_DECREF(result);\n"
        "    result = NULL;\n"
        "    if (PyDict_SetDefaultRef(dict, null_key, other, NULL) != 0) return NULL;\n"
        "    if (PyDict_SetDefaultRef(dict, key, other, NULL) != 1) return NULL;\n"
        "    score += 10000000;\n"
        '    if (PyModule_Add(self, "ADDED_BY_MODULE_ADD", PyUnicode_FromString("module-added")) != 0) return NULL;\n'
        '    added = PyObject_GetAttrString(self, "ADDED_BY_MODULE_ADD");\n'
        "    if (added == NULL) return NULL;\n"
        '    score += PyUnicode_EqualToUTF8(added, "module-added") ? 100000000 : 0;\n'
        "    Py_DECREF(added);\n"
        "    Py_DECREF(other);\n"
        "    Py_DECREF(null_key);\n"
        "    Py_DECREF(value);\n"
        "    Py_DECREF(key);\n"
        "    Py_DECREF(dict);\n"
        "    Py_DECREF(list);\n"
        "    Py_DECREF(abc0def);\n"
        "    Py_DECREF(abc);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_mapping_long_exc_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    long score = 0;\n"
        "    PyObject *dict = PyDict_New();\n"
        '    PyObject *key = PyUnicode_FromString("a");\n'
        "    PyObject *value = PyLong_FromLong(33);\n"
        "    PyObject *got = NULL;\n"
        "    PyObject *bval = NULL;\n"
        "    PyObject *missing = NULL;\n"
        "    PyObject *ss = NULL;\n"
        "    PyObject *ull = NULL;\n"
        "    PyObject *i32o = NULL;\n"
        "    PyObject *i64o = NULL;\n"
        "    PyObject *u32o = NULL;\n"
        "    PyObject *u64o = NULL;\n"
        "    PyObject *ptro = NULL;\n"
        "    PyObject *neg = NULL;\n"
        "    PyObject *truth = NULL;\n"
        "    PyObject *ptype = NULL;\n"
        "    PyObject *pvalue = NULL;\n"
        "    PyObject *ptb = NULL;\n"
        "    PyObject *next_key = NULL;\n"
        "    PyObject *next_value = NULL;\n"
        "    PyObject *ref = NULL;\n"
        "    PyObject *missing_key = NULL;\n"
        "    PyObject *ckey = NULL;\n"
        "    PyObject *cval = NULL;\n"
        "    Py_ssize_t pos = 0;\n"
        "    int32_t i32 = 0;\n"
        "    int64_t i64 = 0;\n"
        "    uint32_t u32 = 0;\n"
        "    uint64_t u64 = 0;\n"
        "    char ptr_marker = 0;\n"
        "    int overflow = 99;\n"
        "    int optional_rc = 0;\n"
        "    if (dict == NULL || key == NULL || value == NULL) return NULL;\n"
        "    if (PyObject_SetItem(dict, key, value) != 0) return NULL;\n"
        "    got = PyObject_GetItem(dict, key);\n"
        "    if (got == NULL) return NULL;\n"
        "    score += PyLong_AsLong(got);\n"
        "    Py_DECREF(got);\n"
        "    score += PyObject_Size(dict) * 10;\n"
        "    score += PyMapping_Size(dict) == 1 ? 37000 : 0;\n"
        "    bval = PyLong_FromLong(4);\n"
        '    if (PyMapping_SetItemString(dict, "b", bval) != 0) return NULL;\n'
        "    Py_DECREF(bval);\n"
        '    got = PyMapping_GetItemString(dict, "b");\n'
        "    if (got == NULL) return NULL;\n"
        "    score += PyLong_AsLong(got);\n"
        "    Py_DECREF(got);\n"
        "    score += PyMapping_Length(dict) == 2 ? 38000 : 0;\n"
        "    score += PyDict_Size(dict) == 2 ? 11000 : 0;\n"
        "    score += PyDict_Contains(dict, key) == 1 ? 12000 : 0;\n"
        '    score += PyDict_ContainsString(dict, "b") == 1 ? 13000 : 0;\n'
        '    score += PyDict_ContainsString(dict, "missing") == 0 ? 14000 : 0;\n'
        "    score += PyLong_AsLong(PyDict_GetItemWithError(dict, key)) == 33 ? 25000 : 0;\n"
        "    if (PyDict_GetItemRef(dict, key, &ref) != 1 || ref == NULL) return NULL;\n"
        "    score += PyLong_AsLong(ref) == 33 ? 26000 : 0;\n"
        "    Py_DECREF(ref);\n"
        "    ref = NULL;\n"
        '    missing_key = PyUnicode_FromString("missing");\n'
        "    if (missing_key == NULL) return NULL;\n"
        "    score += PyDict_GetItemRef(dict, missing_key, &ref) == 0 && ref == NULL ? 27000 : 0;\n"
        "    Py_DECREF(missing_key);\n"
        "    missing_key = NULL;\n"
        '    if (PyDict_GetItemStringRef(dict, "b", &ref) != 1 || ref == NULL) return NULL;\n'
        "    score += PyLong_AsLong(ref) == 4 ? 28000 : 0;\n"
        "    Py_DECREF(ref);\n"
        "    ref = NULL;\n"
        '    ckey = PyUnicode_FromString("c");\n'
        "    cval = PyLong_FromLong(6);\n"
        "    if (ckey == NULL || cval == NULL) return NULL;\n"
        "    if (PyDict_SetItem(dict, ckey, cval) != 0) return NULL;\n"
        "    Py_DECREF(cval);\n"
        "    cval = NULL;\n"
        "    if (PyDict_Pop(dict, ckey, &ref) != 1 || ref == NULL) return NULL;\n"
        "    score += PyLong_AsLong(ref) == 6 ? 29000 : 0;\n"
        "    Py_DECREF(ref);\n"
        "    ref = NULL;\n"
        "    score += PyDict_Contains(dict, ckey) == 0 ? 30000 : 0;\n"
        "    score += PyDict_Pop(dict, ckey, &ref) == 0 && ref == NULL ? 31000 : 0;\n"
        "    cval = PyLong_FromLong(8);\n"
        "    if (cval == NULL) return NULL;\n"
        "    if (PyDict_SetItem(dict, ckey, cval) != 0) return NULL;\n"
        "    Py_DECREF(cval);\n"
        "    cval = NULL;\n"
        "    score += PyDict_Pop(dict, ckey, NULL) == 1 ? 33000 : 0;\n"
        "    score += PyDict_Pop(dict, ckey, NULL) == 0 ? 34000 : 0;\n"
        "    Py_DECREF(ckey);\n"
        "    ckey = NULL;\n"
        "    cval = PyLong_FromLong(7);\n"
        "    if (cval == NULL) return NULL;\n"
        '    if (PyMapping_SetItemString(dict, "d", cval) != 0) return NULL;\n'
        "    Py_DECREF(cval);\n"
        "    cval = NULL;\n"
        '    score += PyDict_PopString(dict, "d", NULL) == 1 ? 35000 : 0;\n'
        '    score += PyDict_PopString(dict, "d", &ref) == 0 && ref == NULL ? 36000 : 0;\n'
        "    score += PyDict_GetItemWithError(NULL, key) == NULL && PyErr_ExceptionMatches(PyExc_TypeError) ? 32000 : 0;\n"
        "    PyErr_Clear();\n"
        "    score += PyMapping_Check(dict) ? 100 : 0;\n"
        '    score += PyMapping_HasKeyString(dict, "b") ? 1000 : 0;\n'
        '    optional_rc = PyMapping_GetOptionalItemString(dict, "missing", &missing);\n'
        "    score += optional_rc == 0 && missing == NULL ? 2000 : 0;\n"
        '    score += PyMapping_HasKeyStringWithError(dict, "b") == 1 ? 3000 : 0;\n'
        "    if (PyDict_DelItem(dict, key) != 0) return NULL;\n"
        "    score += PyDict_Contains(dict, key) == 0 ? 15000 : 0;\n"
        "    score += PyDict_Size(dict) == 1 ? 16000 : 0;\n"
        "    score += PyMapping_HasKey(dict, key) == 0 ? 4000 : 0;\n"
        "    if (PyDict_SetItem(dict, key, value) != 0) return NULL;\n"
        "    if (PyObject_DelItem(dict, key) != 0) return NULL;\n"
        "    score += PyMapping_HasKey(dict, key) == 0 ? 50000 : 0;\n"
        "    if (!PyDict_Next(dict, &pos, &next_key, &next_value)) return NULL;\n"
        '    score += PyUnicode_CompareWithASCIIString(next_key, "b") == 0 && PyLong_AsLong(next_value) == 4 ? 23000 : 0;\n'
        "    score += !PyDict_Next(dict, &pos, &next_key, &next_value) && pos > 0 ? 24000 : 0;\n"
        "    got = PyLong_FromSize_t(12);\n"
        "    if (got == NULL) return NULL;\n"
        "    score += (long)PyLong_AsSize_t(got);\n"
        "    score += PyLong_AsInt(got) == 12 ? 17000 : 0;\n"
        "    Py_DECREF(got);\n"
        "    i32o = PyLong_FromInt32((int32_t)-1234);\n"
        "    if (i32o == NULL || PyLong_AsInt32(i32o, &i32) != 0) return NULL;\n"
        "    score += i32 == (int32_t)-1234 ? 18000 : 0;\n"
        "    Py_DECREF(i32o);\n"
        "    u32o = PyLong_FromUInt32((uint32_t)4000000000U);\n"
        "    if (u32o == NULL || PyLong_AsUInt32(u32o, &u32) != 0) return NULL;\n"
        "    score += u32 == (uint32_t)4000000000U ? 19000 : 0;\n"
        "    Py_DECREF(u32o);\n"
        "    i64o = PyLong_FromInt64((int64_t)-1234567890123LL);\n"
        "    if (i64o == NULL || PyLong_AsInt64(i64o, &i64) != 0) return NULL;\n"
        "    score += i64 == (int64_t)-1234567890123LL ? 20000 : 0;\n"
        "    Py_DECREF(i64o);\n"
        "    u64o = PyLong_FromUInt64((uint64_t)9223372036854775813ULL);\n"
        "    if (u64o == NULL || PyLong_AsUInt64(u64o, &u64) != 0) return NULL;\n"
        "    score += u64 == (uint64_t)9223372036854775813ULL ? 21000 : 0;\n"
        "    Py_DECREF(u64o);\n"
        "    ptro = PyLong_FromVoidPtr((void *)&ptr_marker);\n"
        "    if (ptro == NULL) return NULL;\n"
        "    score += PyLong_AsVoidPtr(ptro) == (void *)&ptr_marker ? 22000 : 0;\n"
        "    Py_DECREF(ptro);\n"
        "    ss = PyLong_FromSsize_t((Py_ssize_t)-3);\n"
        "    if (ss == NULL) return NULL;\n"
        "    score += PyLong_AsLongAndOverflow(ss, &overflow) == -3 && overflow == 0 ? 5000 : 0;\n"
        "    Py_DECREF(ss);\n"
        "    ull = PyLong_FromUnsignedLongLong(42ULL);\n"
        "    if (ull == NULL) return NULL;\n"
        "    score += (long)PyLong_AsUnsignedLongLong(ull);\n"
        "    score += (long)PyLong_AsUnsignedLong(ull);\n"
        "    neg = PyLong_FromLong(-1);\n"
        "    if (neg == NULL) return NULL;\n"
        "    score += PyLong_AsUnsignedLongLongMask(neg) == (unsigned long long)-1 ? 6000 : 0;\n"
        "    score += PyLong_Check(ull) ? 7000 : 0;\n"
        "    truth = PyBool_FromLong(1);\n"
        "    if (truth == NULL) return NULL;\n"
        "    score += PyLong_Check(truth) ? 8000 : 0;\n"
        "    score += !PyLong_CheckExact(truth) ? 9000 : 0;\n"
        "    Py_DECREF(truth);\n"
        "    Py_DECREF(neg);\n"
        "    Py_DECREF(ull);\n"
        "    PyErr_SetObject(PyExc_KeyError, key);\n"
        "    score += PyErr_ExceptionMatches(PyExc_KeyError) ? 10000 : 0;\n"
        "    PyErr_Fetch(&ptype, &pvalue, &ptb);\n"
        "    score += ptype != NULL && pvalue != NULL ? 20000 : 0;\n"
        "    PyErr_Restore(ptype, pvalue, ptb);\n"
        "    score += PyErr_GivenExceptionMatches(PyErr_Occurred(), PyExc_KeyError) ? 30000 : 0;\n"
        "    PyErr_Clear();\n"
        "    Py_DECREF(value);\n"
        "    Py_DECREF(key);\n"
        "    Py_DECREF(dict);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_unicode_from_format_v(const char *format, ...) {\n"
        "    __builtin_va_list ap;\n"
        "    __builtin_va_start(ap, format);\n"
        "    PyObject *result = PyUnicode_FromFormatV(format, ap);\n"
        "    __builtin_va_end(ap);\n"
        "    return result;\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_call_type_format_score(PyObject *self, PyObject *args) {\n"
        "    (void)args;\n"
        "    PyObject *callable = NULL;\n"
        "    PyObject *called = NULL;\n"
        "    PyObject *method0 = NULL;\n"
        "    PyObject *method1 = NULL;\n"
        "    PyObject *method_noargs = NULL;\n"
        "    PyObject *method_name = NULL;\n"
        "    PyObject *formatted = NULL;\n"
        "    PyObject *formatted_v = NULL;\n"
        "    PyObject *type_obj = NULL;\n"
        "    PyObject *label = NULL;\n"
        "    long score = 0;\n"
        '    callable = PyObject_GetAttrString(self, "tuple_sum");\n'
        "    if (callable == NULL) return NULL;\n"
        "    called = PyObject_CallFunction(callable, NULL);\n"
        "    Py_DECREF(callable);\n"
        "    if (called == NULL) return NULL;\n"
        "    score += PyLong_AsLong(called);\n"
        "    Py_DECREF(called);\n"
        '    method0 = PyObject_CallMethod(self, "tuple_sum", NULL);\n'
        "    if (method0 == NULL) return NULL;\n"
        "    score += PyLong_AsLong(method0) * 10;\n"
        "    Py_DECREF(method0);\n"
        '    method1 = PyObject_CallMethod(self, "has_attr", "Os", self, "MAGIC");\n'
        "    if (method1 == NULL) return NULL;\n"
        "    score += PyLong_AsLong(method1) * 100;\n"
        "    Py_DECREF(method1);\n"
        '    method_name = PyUnicode_FromString("tuple_sum");\n'
        "    if (method_name == NULL) return NULL;\n"
        "    method_noargs = PyObject_CallMethodNoArgs(self, method_name);\n"
        "    Py_DECREF(method_name);\n"
        "    if (method_noargs == NULL) return NULL;\n"
        "    score += PyLong_AsLong(method_noargs) * 1000;\n"
        "    Py_DECREF(method_noargs);\n"
        '    label = PyUnicode_FromString("ok");\n'
        "    if (label == NULL) return NULL;\n"
        '    formatted = PyUnicode_FromFormat("v=%ld/%S", 3L, label);\n'
        "    if (formatted == NULL) return NULL;\n"
        '    score += PyUnicode_CompareWithASCIIString(formatted, "v=3/ok") == 0 ? 10000 : 0;\n'
        "    Py_DECREF(formatted);\n"
        '    formatted_v = objectapi_unicode_from_format_v("vv=%ld/%S", 4L, label);\n'
        "    if (formatted_v == NULL) return NULL;\n"
        '    score += PyUnicode_CompareWithASCIIString(formatted_v, "vv=4/ok") == 0 ? 100000 : 0;\n'
        "    Py_DECREF(formatted_v);\n"
        "    Py_DECREF(label);\n"
        "    type_obj = PyObject_Type(self);\n"
        "    if (type_obj == NULL) return NULL;\n"
        "    score += PyObject_IsInstance(self, type_obj) == 1 ? 20000 : 0;\n"
        "    Py_DECREF(type_obj);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_call_helper_score(PyObject *self, PyObject *args) {\n"
        "    PyObject *callable = NULL;\n"
        "    PyObject *method_name = NULL;\n"
        "    PyObject *result = NULL;\n"
        "    PyObject *vc_args[1];\n"
        "    PyObject *vc_method_args[2];\n"
        "    long score = 0;\n"
        "    (void)args;\n"
        '    callable = PyObject_GetAttrString(self, "tuple_sum");\n'
        "    if (callable == NULL) return NULL;\n"
        "    result = PyObject_CallNoArgs(callable);\n"
        "    Py_DECREF(callable);\n"
        "    if (result == NULL) return NULL;\n"
        "    score += PyLong_AsLong(result);\n"
        "    Py_DECREF(result);\n"
        '    callable = PyObject_GetAttrString(self, "set_attr");\n'
        "    if (callable == NULL) return NULL;\n"
        "    result = PyObject_CallOneArg(callable, self);\n"
        "    Py_DECREF(callable);\n"
        "    if (result == NULL) return NULL;\n"
        "    score += PyLong_AsLong(result) * 10;\n"
        "    Py_DECREF(result);\n"
        '    method_name = PyUnicode_FromString("set_attr");\n'
        "    if (method_name == NULL) return NULL;\n"
        "    result = PyObject_CallMethodOneArg(self, method_name, self);\n"
        "    Py_DECREF(method_name);\n"
        "    if (result == NULL) return NULL;\n"
        "    score += PyLong_AsLong(result) * 100;\n"
        "    Py_DECREF(result);\n"
        '    callable = PyObject_GetAttrString(self, "set_attr");\n'
        "    if (callable == NULL) return NULL;\n"
        "    vc_args[0] = self;\n"
        "    result = PyObject_Vectorcall(callable, vc_args, 1, NULL);\n"
        "    Py_DECREF(callable);\n"
        "    if (result == NULL) return NULL;\n"
        "    score += PyLong_AsLong(result) * 1000;\n"
        "    Py_DECREF(result);\n"
        '    method_name = PyUnicode_FromString("set_attr");\n'
        "    if (method_name == NULL) return NULL;\n"
        "    vc_method_args[0] = self;\n"
        "    vc_method_args[1] = self;\n"
        "    result = PyObject_VectorcallMethod(method_name, vc_method_args, 2, NULL);\n"
        "    Py_DECREF(method_name);\n"
        "    if (result == NULL) return NULL;\n"
        "    score += PyLong_AsLong(result) * 10000;\n"
        "    Py_DECREF(result);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_number_protocol_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    PyObject *a = PyLong_FromLong(9);\n"
        "    PyObject *b = PyLong_FromLong(4);\n"
        "    PyObject *two = PyLong_FromLong(2);\n"
        '    PyObject *text = PyUnicode_FromString("x");\n'
        "    PyObject *add = NULL;\n"
        "    PyObject *sub = NULL;\n"
        "    PyObject *mul = NULL;\n"
        "    PyObject *div = NULL;\n"
        "    PyObject *floor_div = NULL;\n"
        "    PyObject *rem = NULL;\n"
        "    PyObject *pow_obj = NULL;\n"
        "    PyObject *neg = NULL;\n"
        "    PyObject *pos = NULL;\n"
        "    PyObject *abs_obj = NULL;\n"
        "    PyObject *index_true = NULL;\n"
        "    PyObject *repeat = NULL;\n"
        "    long score = 0;\n"
        "    if (a == NULL || b == NULL || two == NULL || text == NULL) return NULL;\n"
        "    add = PyNumber_Add(a, b);\n"
        "    sub = PyNumber_Subtract(a, b);\n"
        "    mul = PyNumber_Multiply(a, b);\n"
        "    div = PyNumber_TrueDivide(a, b);\n"
        "    floor_div = PyNumber_FloorDivide(a, b);\n"
        "    rem = PyNumber_Remainder(a, b);\n"
        "    pow_obj = PyNumber_Power(b, two, Py_None);\n"
        "    neg = PyNumber_Negative(b);\n"
        "    pos = PyNumber_Positive(b);\n"
        "    abs_obj = PyNumber_Absolute(neg);\n"
        "    index_true = PyNumber_Index(Py_True);\n"
        "    repeat = PyNumber_Multiply(text, two);\n"
        "    if (add == NULL || sub == NULL || mul == NULL || div == NULL || floor_div == NULL || rem == NULL || pow_obj == NULL || neg == NULL || pos == NULL || abs_obj == NULL || index_true == NULL || repeat == NULL) return NULL;\n"
        "    score += PyLong_AsLong(add);\n"
        "    score += PyLong_AsLong(sub) * 10;\n"
        "    score += PyLong_AsLong(mul) * 100;\n"
        "    score += (long)(PyFloat_AsDouble(div) * 100.0);\n"
        "    score += PyLong_AsLong(floor_div) * 1000;\n"
        "    score += PyLong_AsLong(rem) * 10000;\n"
        "    score += PyLong_AsLong(pos) * 100000;\n"
        "    score += PyLong_AsLong(pow_obj) * 100000;\n"
        "    score += PyLong_AsLong(abs_obj) * 1000000;\n"
        "    score += (long)PyNumber_AsSsize_t(b, PyExc_OverflowError) * 10000000;\n"
        "    score += PyObject_Not(Py_False) * 100000000;\n"
        "    score += PyIndex_Check(a) * 200000000;\n"
        "    score += PyLong_AsLong(index_true) * 300000000;\n"
        '    score += PyUnicode_CompareWithASCIIString(repeat, "xx") == 0 ? 400000000 : 0;\n'
        "    Py_DECREF(repeat);\n"
        "    Py_DECREF(index_true);\n"
        "    Py_DECREF(abs_obj);\n"
        "    Py_DECREF(pos);\n"
        "    Py_DECREF(neg);\n"
        "    Py_DECREF(pow_obj);\n"
        "    Py_DECREF(rem);\n"
        "    Py_DECREF(floor_div);\n"
        "    Py_DECREF(div);\n"
        "    Py_DECREF(mul);\n"
        "    Py_DECREF(sub);\n"
        "    Py_DECREF(add);\n"
        "    Py_DECREF(text);\n"
        "    Py_DECREF(two);\n"
        "    Py_DECREF(b);\n"
        "    Py_DECREF(a);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyObject *objectapi_number_bitwise_score(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    PyObject *one = PyLong_FromLong(1);\n"
        "    PyObject *two = PyLong_FromLong(2);\n"
        "    PyObject *three = PyLong_FromLong(3);\n"
        "    PyObject *four = PyLong_FromLong(4);\n"
        "    PyObject *six = PyLong_FromLong(6);\n"
        "    PyObject *seven = PyLong_FromLong(7);\n"
        "    PyObject *eight = PyLong_FromLong(8);\n"
        "    PyObject *float_obj = PyFloat_FromDouble(6.5);\n"
        '    PyObject *text = PyUnicode_FromString("x");\n'
        "    PyObject *as_long = NULL;\n"
        "    PyObject *as_float = NULL;\n"
        "    PyObject *and_obj = NULL;\n"
        "    PyObject *or_obj = NULL;\n"
        "    PyObject *xor_obj = NULL;\n"
        "    PyObject *lshift_obj = NULL;\n"
        "    PyObject *rshift_obj = NULL;\n"
        "    PyObject *invert_obj = NULL;\n"
        "    PyObject *from_double = NULL;\n"
        "    long score = 0;\n"
        "    if (one == NULL || two == NULL || three == NULL || four == NULL || six == NULL || seven == NULL || eight == NULL || float_obj == NULL || text == NULL) return NULL;\n"
        "    as_long = PyNumber_Long(float_obj);\n"
        "    from_double = PyLong_FromDouble(-3.75);\n"
        "    as_float = PyNumber_Float(seven);\n"
        "    and_obj = PyNumber_And(six, three);\n"
        "    or_obj = PyNumber_Or(four, one);\n"
        "    xor_obj = PyNumber_Xor(seven, three);\n"
        "    lshift_obj = PyNumber_Lshift(one, three);\n"
        "    rshift_obj = PyNumber_Rshift(eight, two);\n"
        "    invert_obj = PyNumber_Invert(two);\n"
        "    if (as_long == NULL || from_double == NULL || as_float == NULL || and_obj == NULL || or_obj == NULL || xor_obj == NULL || lshift_obj == NULL || rshift_obj == NULL || invert_obj == NULL) return NULL;\n"
        "    score += PyNumber_Check(seven) ? 1 : 0;\n"
        "    score += !PyNumber_Check(text) ? 2 : 0;\n"
        "    score += PyLong_AsLong(as_long) == 6 ? 4 : 0;\n"
        "    score += PyFloat_AS_DOUBLE(as_float) == 7.0 ? 8 : 0;\n"
        "    score += PyLong_AsDouble(seven) == 7.0 ? 16 : 0;\n"
        "    score += PyFloat_AS_DOUBLE(float_obj) == 6.5 ? 32 : 0;\n"
        "    score += PyLong_AsLong(and_obj) == 2 ? 64 : 0;\n"
        "    score += PyLong_AsLong(or_obj) == 5 ? 128 : 0;\n"
        "    score += PyLong_AsLong(xor_obj) == 4 ? 256 : 0;\n"
        "    score += PyLong_AsLong(lshift_obj) == 8 ? 512 : 0;\n"
        "    score += PyLong_AsLong(rshift_obj) == 2 ? 1024 : 0;\n"
        "    score += PyLong_AsLong(invert_obj) == -3 ? 2048 : 0;\n"
        "    score += PyLong_AsLong(from_double) == -3 ? 4096 : 0;\n"
        "    Py_DECREF(invert_obj);\n"
        "    Py_DECREF(rshift_obj);\n"
        "    Py_DECREF(lshift_obj);\n"
        "    Py_DECREF(xor_obj);\n"
        "    Py_DECREF(or_obj);\n"
        "    Py_DECREF(and_obj);\n"
        "    Py_DECREF(as_float);\n"
        "    Py_DECREF(from_double);\n"
        "    Py_DECREF(as_long);\n"
        "    Py_DECREF(text);\n"
        "    Py_DECREF(float_obj);\n"
        "    Py_DECREF(eight);\n"
        "    Py_DECREF(seven);\n"
        "    Py_DECREF(six);\n"
        "    Py_DECREF(four);\n"
        "    Py_DECREF(three);\n"
        "    Py_DECREF(two);\n"
        "    Py_DECREF(one);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "\n"
        "static PyMethodDef ObjectApiMethods[] = {\n"
        '    {"tuple_sum", objectapi_tuple_sum, METH_VARARGS, "tuple C-API smoke"},\n'
        '    {"pack_sum", objectapi_pack_sum, METH_VARARGS, "PyTuple_Pack smoke"},\n'
        '    {"dict_answer", objectapi_dict_answer, METH_VARARGS, "dict C-API smoke"},\n'
        '    {"list_sum", objectapi_list_sum, METH_VARARGS, "list C-API smoke"},\n'
        '    {"bytes_sum", objectapi_bytes_sum, METH_VARARGS, "bytes C-API smoke"},\n'
        '    {"bytes_size_sum", objectapi_bytes_size_sum, METH_VARARGS, "PyBytes_AsStringAndSize smoke"},\n'
        '    {"unicode_prefix", objectapi_unicode_prefix, METH_VARARGS, "PyUnicode_FromStringAndSize smoke"},\n'
        '    {"unicode_utf8_size_score", objectapi_unicode_utf8_size_score, METH_VARARGS, "PyUnicode_AsUTF8AndSize smoke"},\n'
        '    {"unicode_decode_length_score", objectapi_unicode_decode_length_score, METH_VARARGS, "Unicode decode/length smoke"},\n'
        '    {"unicode_encode_concat_score", objectapi_unicode_encode_concat_score, METH_VARARGS, "Unicode encode/concat C-API smoke"},\n'
        '    {"unicode_macro_score", objectapi_unicode_macro_score, METH_VARARGS, "Unicode macro C-API smoke"},\n'
        '    {"unicode_kind_score", objectapi_unicode_kind_score, METH_VARARGS, "Unicode kind/data C-API smoke"},\n'
        '    {"float_bool_score", objectapi_float_bool_score, METH_VARARGS, "float/bool C-API smoke"},\n'
        '    {"scalar_complex_score", objectapi_scalar_complex_score, METH_VARARGS, "scalar/complex C-API smoke"},\n'
        '    {"set_attr", objectapi_set_attr, METH_VARARGS, "attr string smoke"},\n'
        '    {"has_attr", objectapi_has_attr, METH_VARARGS, "has attr string smoke"},\n'
        '    {"format_error", objectapi_format_error, METH_VARARGS, "PyErr_Format smoke"},\n'
        '    {"warning_score", objectapi_warning_score, METH_VARARGS, "warning C-API smoke"},\n'
        '    {"gil_state_score", objectapi_gil_state_score, METH_VARARGS, "GIL state C-API smoke"},\n'
        '    {"error_helper_score", objectapi_error_helper_score, METH_VARARGS, "error helper C-API smoke"},\n'
        '    {"exception_global_score", objectapi_exception_global_score, METH_VARARGS, "exception global C-API smoke"},\n'
        '    {"module_dict_magic", objectapi_module_dict_magic, METH_VARARGS, "PyModule_GetDict smoke"},\n'
        '    {"bump_module_global", objectapi_bump_module_global, METH_VARARGS, "mutate module globals"},\n'
        '    {"delete_module_global", objectapi_delete_module_global, METH_VARARGS, "delete module global"},\n'
        '    {"str_repr_compare", objectapi_str_repr_compare, METH_VARARGS, "str/repr/richcompare smoke"},\n'
        '    {"print_score", objectapi_print_score, METH_VARARGS, "PyObject_Print smoke"},\n'
        '    {"unsigned_sum", objectapi_unsigned_sum, METH_VARARGS, "unsigned long constructors smoke"},\n'
        '    {"import_self_magic", objectapi_import_self_magic, METH_VARARGS, "PyImport_ImportModule smoke"},\n'
        '    {"sequence_score", objectapi_sequence_score, METH_VARARGS, "sequence/type-check C-API smoke"},\n'
        '    {"iter_contains_score", objectapi_iter_contains_score, METH_VARARGS, "iteration/containment C-API smoke"},\n'
        '    {"collection_views_score", objectapi_collection_views_score, METH_VARARGS, "list/dict collection-view C-API smoke"},\n'
        '    {"set_score", objectapi_set_score, METH_VARARGS, "set C-API smoke"},\n'
        '    {"attr_hash_score", objectapi_attr_hash_score, METH_VARARGS, "object attr/hash C-API smoke"},\n'
        '    {"build_mem_kw_score", objectapi_build_mem_kw_score, METH_VARARGS, "build/memory/keyword C-API smoke"},\n'
        '    {"refcnt_macro_score", objectapi_refcnt_macro_score, METH_VARARGS, "refcount macro C-API smoke"},\n'
        '    {"memory_os_score", objectapi_memory_os_score, METH_VARARGS, "raw/object memory and PyOS smoke"},\n'
        '    {"pythoncapi_compat_score", objectapi_pythoncapi_compat_score, METH_VARARGS, "pythoncapi-compat C-API smoke"},\n'
        '    {"mapping_long_exc_score", objectapi_mapping_long_exc_score, METH_VARARGS, "mapping/long/exception C-API smoke"},\n'
        '    {"call_type_format_score", objectapi_call_type_format_score, METH_VARARGS, "call/type/format C-API smoke"},\n'
        '    {"call_helper_score", objectapi_call_helper_score, METH_VARARGS, "call helper C-API smoke"},\n'
        '    {"number_protocol_score", objectapi_number_protocol_score, METH_VARARGS, "abstract number protocol C-API smoke"},\n'
        '    {"number_bitwise_score", objectapi_number_bitwise_score, METH_VARARGS, "number conversion/bitwise C-API smoke"},\n'
        "    {NULL, NULL, 0, NULL},\n"
        "};\n"
        "\n"
        "static PyModuleDef objectapimodule = {\n"
        '    PyModuleDef_HEAD_INIT, "objectapi", NULL, -1, ObjectApiMethods,\n'
        "};\n"
        "\n"
        "PyMODINIT_FUNC PyInit_objectapi(void) {\n"
        "    PyObject *module = PyModule_Create(&objectapimodule);\n"
        "    if (module == NULL) return NULL;\n"
        '    if (PyModule_AddIntConstant(module, "MAGIC", 12) != 0) return NULL;\n'
        '    if (PyModule_AddStringConstant(module, "LABEL", "ok") != 0) return NULL;\n'
        "    PyObject *added_ref = PyLong_FromLong(77);\n"
        "    if (added_ref == NULL) return NULL;\n"
        '    if (PyModule_AddObjectRef(module, "ADDED_REF", added_ref) != 0) return NULL;\n'
        "    PyObject *dict = PyModule_GetDict(module);\n"
        '    PyObject *exc = PyErr_NewException("objectapi.CustomError", NULL, NULL);\n'
        "    if (dict == NULL || exc == NULL) return NULL;\n"
        '    if (PyDict_SetItemString(dict, "CustomError", exc) != 0) return NULL;\n'
        "    Py_DECREF(exc);\n"
        "    Py_DECREF(added_ref);\n"
        "    return module;\n"
        "}\n",
    )


def _compile_errno_extension(tmp_path: Path) -> Path:
    return _compile_extension(
        tmp_path,
        "errnoapi",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "#include <errno.h>\n"
        "extern int *__error(void);\n"
        "static PyObject *fetch_message(PyObject *filename, int code) {\n"
        "  PyObject *type = NULL, *value = NULL, *traceback = NULL;\n"
        "  *__error() = code;\n"
        "  if (filename == NULL) PyErr_SetFromErrno(PyExc_OSError);\n"
        "  else PyErr_SetFromErrnoWithFilenameObject(PyExc_OSError, filename);\n"
        "  PyErr_Fetch(&type, &value, &traceback);\n"
        "  if (value == NULL) { Py_XDECREF(type); Py_XDECREF(traceback); return NULL; }\n"
        "  PyObject *text = PyObject_Str(value);\n"
        "  Py_XDECREF(type); Py_DECREF(value); Py_XDECREF(traceback);\n"
        "  return text;\n"
        "}\n"
        "static PyObject *message(PyObject *self, PyObject *args) {\n"
        "  (void)self; (void)args; return fetch_message(NULL, ENOENT);\n"
        "}\n"
        "static PyObject *filename_message(PyObject *self, PyObject *args) {\n"
        "  (void)self; (void)args;\n"
        "  PyObject *name = PyUnicode_FromString(\"missing.dat\");\n"
        "  if (name == NULL) return NULL;\n"
        "  PyObject *result = fetch_message(name, ENOENT);\n"
        "  Py_DECREF(name); return result;\n"
        "}\n"
        "static PyObject *unknown_message(PyObject *self, PyObject *args) {\n"
        "  (void)self; (void)args; return fetch_message(NULL, 9999);\n"
        "}\n"
        "static PyMethodDef Methods[] = {\n"
        "  {\"message\", message, METH_NOARGS, NULL},\n"
        "  {\"filename_message\", filename_message, METH_NOARGS, NULL},\n"
        "  {\"unknown_message\", unknown_message, METH_NOARGS, NULL},\n"
        "  {NULL, NULL, 0, NULL}\n"
        "};\n"
        "static PyModuleDef module = { PyModuleDef_HEAD_INIT, \"errnoapi\", NULL, -1, Methods };\n"
        "PyMODINIT_FUNC PyInit_errnoapi(void) { return PyModule_Create(&module); }\n",
    )


def _compile_missing_init_extension(tmp_path: Path) -> Path:
    return _compile_extension(
        tmp_path,
        "badinit",
        "#include <Python.h>\n" "int not_the_init_symbol(void) { return 0; }\n",
    )


def _compile_null_init_extension(tmp_path: Path) -> Path:
    return _compile_extension(
        tmp_path,
        "nullinit",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "PyMODINIT_FUNC PyInit_nullinit(void) {\n"
        '    PyErr_SetString(PyExc_RuntimeError, "init failed intentionally");\n'
        "    return NULL;\n"
        "}\n",
    )


def _compile_retry_init_extension(tmp_path: Path) -> Path:
    return _compile_extension(
        tmp_path,
        "retryinit",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "#include <stdlib.h>\n"
        "\n"
        "#ifdef _WIN32\n"
        "extern int _putenv(const char *);\n"
        "#else\n"
        "extern int unsetenv(const char *);\n"
        "#endif\n"
        "\n"
        "static long successful_inits = 0;\n"
        "\n"
        "static PyObject *retryinit_count(PyObject *self, PyObject *args) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    return PyLong_FromLong(successful_inits);\n"
        "}\n"
        "\n"
        "static PyMethodDef RetryMethods[] = {\n"
        '    {"count", retryinit_count, METH_VARARGS, "return successful init count"},\n'
        "    {NULL, NULL, 0, NULL},\n"
        "};\n"
        "\n"
        "static PyModuleDef retrymodule = {\n"
        '    PyModuleDef_HEAD_INIT, "retryinit", NULL, -1, RetryMethods,\n'
        "};\n"
        "\n"
        "PyMODINIT_FUNC PyInit_retryinit(void) {\n"
        '    const char *fail_once = getenv("PCC_RETRYINIT_FAIL_ONCE");\n'
        "    if (fail_once != NULL && fail_once[0] != '\\0') {\n"
        "#ifdef _WIN32\n"
        '        _putenv("PCC_RETRYINIT_FAIL_ONCE=");\n'
        "#else\n"
        '        unsetenv("PCC_RETRYINIT_FAIL_ONCE");\n'
        "#endif\n"
        '        PyErr_SetString(PyExc_RuntimeError, "retry init failed once");\n'
        "        return NULL;\n"
        "    }\n"
        "    successful_inits += 1;\n"
        "    return PyModule_Create(&retrymodule);\n"
        "}\n",
    )


def _compile_silent_null_init_extension(tmp_path: Path) -> Path:
    return _compile_extension(
        tmp_path,
        "silentnullinit",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "PyMODINIT_FUNC PyInit_silentnullinit(void) {\n"
        "    return NULL;\n"
        "}\n",
    )


def _compile_numpy_25_capi_batch_extension(tmp_path: Path) -> Path:
    return _compile_extension(
        tmp_path,
        "capi25",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "static PyTypeObject SequenceType = {\n"
        "    PyVarObject_HEAD_INIT(NULL, 0)\n"
        '    .tp_name = "capi25.Sequence",\n'
        "    .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_SEQUENCE,\n"
        "};\n"
        "static PyObject *probe(PyObject *self, PyObject *args) {\n"
        "    long score = 0;\n"
        "    PyObject *zero = PyLong_FromLong(0);\n"
        "    PyObject *one = PyLong_FromLong(1);\n"
        '    PyObject *text = PyUnicode_FromString("ok");\n'
        "    PyObject *copy = NULL;\n"
        "    (void)self; (void)args;\n"
        "    if (PYMEM_DOMAIN_RAW == 0) score += 1;\n"
        "    if (PyLong_IsZero(zero) == 1 && PyLong_IsZero(one) == 0) score += 2;\n"
        "    copy = PyUnicode_FromObject(text);\n"
        "    if (copy == text) score += 4;\n"
        "    PyType_Modified(&SequenceType);\n"
        "    score += 8;\n"
        "    if (SequenceType.tp_flags & Py_TPFLAGS_SEQUENCE) score += 16;\n"
        "    if (PyLong_IsZero(text) == -1 && PyErr_ExceptionMatches(PyExc_TypeError)) score += 32;\n"
        "    PyErr_Clear();\n"
        "    Py_XDECREF(copy); Py_DECREF(text); Py_DECREF(one); Py_DECREF(zero);\n"
        "    return PyLong_FromLong(score);\n"
        "}\n"
        "static PyMethodDef Methods[] = {\n"
        '    {"probe", probe, METH_VARARGS, "probe NumPy 2.5 C-API batch"},\n'
        "    {NULL, NULL, 0, NULL},\n"
        "};\n"
        "static PyModuleDef module = {\n"
        '    PyModuleDef_HEAD_INIT, "capi25", NULL, -1, Methods,\n'
        "};\n"
        "PyMODINIT_FUNC PyInit_capi25(void) { return PyModule_Create(&module); }\n",
    )


def _compile_main(
    site: Path, main: Path, exe: Path, *, runtime_cc: str = "cc"
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    env["PCC_SELF_LINK"] = "cc"
    env["PCC_RUNTIME_CC"] = runtime_cc
    return subprocess.run(
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
        timeout=180,
        env=env,
    )


def _run_main(
    site: Path, exe: Path, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    env["PCC_SELF_LINK"] = "cc"
    if extra_env is not None:
        env.update(extra_env)
    return subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )


def test_pcc_native_extension_import_runs_under_self_backend_no_libpython(tmp_path):
    site = _compile_demo_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import demo\n" "print(demo.add(2, 3))\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    env["PCC_SELF_LINK"] = "cc"
    env["PCC_RUNTIME_CC"] = "cc"
    proc = subprocess.run(
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
        timeout=180,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "5"


def test_relative_extension_import_publishes_wrapper_module_binding(tmp_path):
    site = _compile_extension(
        tmp_path,
        "_native",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "static struct PyModuleDef module = {\n"
        "    PyModuleDef_HEAD_INIT, \"_native\", NULL, -1, NULL,\n"
        "};\n"
        "PyMODINIT_FUNC PyInit__native(void) { return PyModule_Create(&module); }\n",
    )
    package = site / "pkg"
    package.mkdir()
    (site / "_native.so").replace(package / "_native.so")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "wrapper.py").write_text(
        "from . import _native\n"
        + "".join(f"module_value_{index} = {index}\n" for index in range(200)),
        encoding="utf-8",
    )
    main = tmp_path / "main.py"
    main.write_text(
        "import pkg.wrapper as wrapper\n"
        "print(hasattr(wrapper, '_native'))\n"
        "print(wrapper._native.__name__)\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"

    compiled = _compile_main(site, main, exe)
    assert compiled.returncode == 0, compiled.stderr
    run = _run_main(site, exe, {"PCC_PACKAGE_SITE": ""})

    assert run.returncode == 0, run.stderr
    assert run.stdout == "True\n_native\n"


def test_pcc_native_extension_numpy_25_capi_batch_under_self_backend_no_libpython(
    tmp_path,
):
    site = _compile_numpy_25_capi_batch_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text("import capi25\nprint(capi25.probe())\n", encoding="utf-8")
    exe = tmp_path / "main_bin"

    compiled = _compile_main(site, main, exe)
    assert compiled.returncode == 0, compiled.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "63"


def test_pcc_native_extension_star_import_publishes_compiled_module_names(tmp_path):
    site = _compile_demo_extension(tmp_path)
    bridge = tmp_path / "bridge.py"
    bridge.write_text(
        "from demo import *\n" "probe = add(2, 3)\n",
        encoding="utf-8",
    )
    main = tmp_path / "main.py"
    main.write_text(
        "import bridge\n" "print(bridge.probe)\n" "print(bridge.add(4, 5))\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["5", "9"]


def test_pcc_native_extension_fastcall_method_is_published_and_callable(tmp_path):
    site = _compile_fastcall_extension(tmp_path)
    bridge = tmp_path / "bridge.py"
    bridge.write_text(
        "from fastdemo import *\n" "probe = fast_sum(2, 3)\n",
        encoding="utf-8",
    )
    main = tmp_path / "main.py"
    main.write_text(
        "import bridge\n"
        "print(bridge.probe)\n"
        "print(bridge.fast_sum(4, 5))\n"
        "print(bridge.fast_kw_score(7, value=9))\n"
        "print(bridge.method_name(bridge.fast_sum))\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["5", "9", "791", "fast_sum"]


def test_pcc_native_extension_function_allows_module_metadata_assignment(tmp_path):
    site = _compile_fastcall_extension(tmp_path)
    bridge = tmp_path / "bridge.py"
    bridge.write_text(
        "from fastdemo import *\n" "fast_sum.__module__ = 'bridge'\n",
        encoding="utf-8",
    )
    main = tmp_path / "main.py"
    main.write_text(
        "import bridge\n" "print(bridge.fast_sum.__module__)\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "bridge"


def test_pcc_native_extension_import_is_visible_inside_lambda(tmp_path):
    site = _compile_demo_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "from demo import add\n"
        "values = [3, 1, 2]\n"
        "values.sort(key=lambda value: add(value, 0))\n"
        "print(values)\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "[1, 2, 3]"


def test_pcc_native_custom_type_pytype_ready_under_self_backend_no_libpython(tmp_path):
    """A C extension that registers a custom type via PyType_Ready and
    instantiates it must import + instantiate under strict no-libpython. This
    exercises the type bridge in py_capi_shim.c (PyType_Ready + the dynamic
    type_tag registry + PyType_GenericNew/Alloc). Reduced numpy
    type-registration pattern.
"""
    site = _compile_typedemo_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import typedemo\n"
        'print("has", hasattr(typedemo, "Foo"))\n'
        'print("notnone", typedemo.Foo is not None)\n'
        'print("type-meta", typedemo.Foo.__module__, typedemo.Foo.__name__)\n'
        "x = typedemo.Foo()\n"
        'print("inst", x is not None)\n'
        "x.__module__ = 'demo'\n"
        "x.__qualname__ = 'Foo'\n"
        "print(x.__module__, x.__qualname__, x.itemsize)\n"
        "base_descriptor = typedemo.Foo.base\n"
        "setattr(base_descriptor, '__doc__', 'base doc')\n"
        "print(base_descriptor.__doc__, x.base)\n"
        "member_descriptor = typedemo.Foo.itemsize\n"
        "setattr(member_descriptor, '__doc__', 'member doc')\n"
        "print(member_descriptor.__doc__)\n"
        "ge_descriptor = typedemo.Foo.__ge__\n"
        "setattr(ge_descriptor, '__doc__', 'ge doc')\n"
        "print(ge_descriptor.__doc__, ge_descriptor(x, x))\n"
        "print(issubclass(typedemo.Foo, typedemo.Base))\n"
        "print(issubclass(typedemo.Base, typedemo.Foo))\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    env["PCC_SELF_LINK"] = "cc"
    env["PCC_RUNTIME_CC"] = "pcc"
    proc = subprocess.run(
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
        timeout=180,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    assert run.returncode == 0, run.stderr
    assert "has True" in run.stdout, run.stdout
    assert "notnone True" in run.stdout, run.stdout
    assert "type-meta typedemo Foo" in run.stdout, run.stdout
    assert "inst True" in run.stdout, run.stdout
    assert "demo Foo 0" in run.stdout, run.stdout
    assert "base doc 7" in run.stdout, run.stdout
    assert "member doc" in run.stdout, run.stdout
    assert "ge doc True" in run.stdout, run.stdout
    assert run.stdout.splitlines()[-2:] == ["True", "False"], run.stdout


def test_pcc_native_custom_type_from_spec_under_self_backend_no_libpython(tmp_path):
    site = _compile_typespec_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import specdemo\n" "print(specdemo.check())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "31"


def test_pcc_native_custom_type_getslot_under_self_backend_no_libpython(tmp_path):
    site = _compile_typespec_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import specdemo\n" "print(specdemo.slots())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "7"


def test_pcc_native_custom_type_name_under_self_backend_no_libpython(tmp_path):
    site = _compile_typespec_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import specdemo\n" "print(specdemo.Spec.__name__)\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "Spec"


def test_pcc_native_custom_type_method_descriptor_under_self_backend_no_libpython(
    tmp_path,
):
    site = _compile_typespec_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import specdemo\n"
        "obj = specdemo.Spec()\n"
        "first = specdemo.Spec.marker\n"
        "second = specdemo.Spec.marker\n"
        "print(first is second, first(obj, 41))\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "True 41"


def test_pcc_native_custom_metaclass_typecheck_under_self_backend_no_libpython(
    tmp_path,
):
    site = _compile_typespec_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import specdemo\nprint(specdemo.metatype_check())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "3"


def test_pcc_native_builtin_py_type_mapping_under_self_backend_no_libpython(tmp_path):
    """Py_TYPE on pcc builtin objects must resolve to the shim's &PyXxx_Type
    tokens (numpy compares Py_TYPE(o) against builtin type objects). Validates
    the builtin-tag mapping in py_capi_shim.c, including immediate (tagged) ints.
    `check()` returns 3 when both int and str map correctly."""
    site = _compile_typecheck_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text("import tcdemo\nprint('tc', tcdemo.check())\n", encoding="utf-8")
    exe = tmp_path / "main_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    env["PCC_SELF_LINK"] = "cc"
    env["PCC_RUNTIME_CC"] = "cc"
    proc = subprocess.run(
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
        timeout=180,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=30, env=env
    )
    assert run.returncode == 0, run.stderr
    assert "tc 3" in run.stdout, run.stdout


def test_pcc_native_subtype_check_under_self_backend_no_libpython(tmp_path):
    """Type-bridge inheritance + alloc surface under strict no-libpython.
    check() returns 63 = PyObject_TypeCheck(derived,&Base) + (derived,&Derived) +
    !(base,&Derived) + PyType_IsSubtype(&Derived,&Base) + !(&Base,&Derived) +
    PyObject_New(&Base) non-NULL — i.e. subtype walk (both PyObject_TypeCheck and
    PyType_IsSubtype) and _PyObject_New all work."""
    site = _compile_subtype_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text("import stdemo\nprint('st', stdemo.check())\n", encoding="utf-8")
    exe = tmp_path / "main_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    env["PCC_SELF_LINK"] = "cc"
    env["PCC_RUNTIME_CC"] = "cc"
    proc = subprocess.run(
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
        timeout=180,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=30, env=env
    )
    assert run.returncode == 0, run.stderr
    assert "st 63" in run.stdout, run.stdout


def test_pcc_native_type_ready_inherits_alloc_slot(tmp_path):
    site = _compile_subtype_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import stdemo\nprint(stdemo.alloc_slots())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "7"


def test_pcc_native_extension_numeric_conversion_slots(tmp_path):
    site = _compile_number_slot_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import numslotdemo\nprint(numslotdemo.check())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe, runtime_cc="pcc")
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "63"


def test_pcc_native_extension_managed_dealloc_gate_under_self_backend_no_libpython(
    tmp_path,
):
    """pcc-private managed dealloc hooks may clean extension payloads, but
    ordinary C-extension tp_dealloc slots must not be called blindly because
    most CPython slots free the object body themselves."""
    site = _compile_managed_dealloc_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text("import mdemo\nprint('managed', mdemo.check())\n", encoding="utf-8")
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert "managed 1" in run.stdout, run.stdout


def test_pcc_native_link_symbols_behave_under_self_backend_no_libpython(tmp_path):
    """Behavioral regression for the batch 7-9 link symbols added to
    py_capi_shim.c for numpy's no-libpython link surface (PyTuple_GetSlice,
    PyObject_AsFileDescriptor, Py_GenericAlias, PyUnicode_AsLatin1String,
    PyErr_NormalizeException). They were link-validated only; this exercises
    them at runtime under strict --python-libpython=off --backend self.
    check() returns 31 when all five behave correctly."""
    site = _compile_linksym_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text("import lsdemo\nprint('ls', lsdemo.check())\n", encoding="utf-8")
    exe = tmp_path / "main_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    env["PCC_SELF_LINK"] = "cc"
    env["PCC_RUNTIME_CC"] = "cc"
    proc = subprocess.run(
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
        timeout=180,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=30, env=env
    )
    assert run.returncode == 0, run.stderr
    assert "ls 31" in run.stdout, run.stdout


def test_pcc_native_contextvar_get_under_self_backend_no_libpython(tmp_path):
    """Behavioral regression for the single-context contextvar (PyContextVar_New
    + PyContextVar_Get from batch 10, PyContextVar_Set from batch 14) in
    py_capi_shim.c — a real object, not a stub, since numpy.errstate creates a
    ContextVar at import and numpy alloc.c calls PyContextVar_Set. check() returns
    7: Get returns the var's own default (5) for a NULL default arg, the explicit
    default arg (9) when unset, and the set value (7) after Set — correct CPython
    single-context semantics under strict --python-libpython=off."""
    site = _compile_contextvar_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import cvdemo\n"
        "cv = cvdemo.make()\n"
        "token = cv.set(7)\n"
        "before = cv.get()\n"
        "cv.reset(token)\n"
        "after = cv.get()\n"
        "print('cv', cvdemo.check(), before * 10 + after)\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    env["PCC_SELF_LINK"] = "cc"
    env["PCC_RUNTIME_CC"] = "cc"
    proc = subprocess.run(
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
        timeout=180,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=30, env=env
    )
    assert run.returncode == 0, run.stderr
    assert "cv 7 75" in run.stdout, run.stdout


def test_pcc_native_buildvalue_dict_under_self_backend_no_libpython(tmp_path):
    site = _compile_buildvalue_dict_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import buildvaluedict\nprint(buildvaluedict.check())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["42"]


def test_pcc_native_buildvalue_nested_list_under_self_backend_no_libpython(tmp_path):
    site = _compile_buildvalue_dict_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import buildvaluedict\nprint(buildvaluedict.nested())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["127"]


def test_pcc_native_vaparse_tuple_and_keywords_under_self_backend_no_libpython(
    tmp_path,
):
    """Behavioral regression for the PyArg_ParseTupleAndKeywords -> (new)
    PyArg_VaParseTupleAndKeywords va_list-core delegation (canonical CPython
    refactor; PyArg_VaParseTupleAndKeywords is the symbol numpy's C core
    references). check(10, 20) parses "ll" through the core and returns 1020
    under strict --python-libpython=off --backend self."""
    site = _compile_vaparse_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import vademo\nprint('va', vademo.check(10, 20))\n", encoding="utf-8"
    )
    exe = tmp_path / "main_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    env["PCC_SELF_LINK"] = "cc"
    env["PCC_RUNTIME_CC"] = "cc"
    proc = subprocess.run(
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
        timeout=180,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=30, env=env
    )
    assert run.returncode == 0, run.stderr
    assert "va 1020" in run.stdout, run.stdout


def test_pcc_native_eval_getbuiltins_under_self_backend_no_libpython(tmp_path):
    """Behavioral regression for PyEval_GetBuiltins — numpy's sole consumer
    (npy_PyFile_OpenFile) does PyDict_GetItemString(PyEval_GetBuiltins(), "open")
    and returns NULL gracefully if absent, so an empty real dict is import-safe.
    check() returns 7: GetBuiltins is a non-NULL real dict, an absent key yields
    NULL without crashing, and it is a mutable persistent singleton (borrowed-ref
    semantics) under strict --python-libpython=off --backend self."""
    site = _compile_getbuiltins_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text("import ebdemo\nprint('eb', ebdemo.check())\n", encoding="utf-8")
    exe = tmp_path / "main_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    env["PCC_SELF_LINK"] = "cc"
    env["PCC_RUNTIME_CC"] = "pcc"
    proc = subprocess.run(
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
        timeout=180,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=30, env=env
    )
    assert run.returncode == 0, run.stderr
    assert "eb 7" in run.stdout, run.stdout


def test_pcc_native_uniquely_referenced_under_self_backend_no_libpython(tmp_path):
    """Behavioral regression for PyUnstable_Object_IsUniquelyReferenced (full
    numpy _core references it for safe in-place mutation). check() returns 3 when
    a freshly-created object (refcount 1) is uniquely referenced and, after
    Py_INCREF (refcount 2), it is not — under strict --python-libpython=off."""
    site = _compile_uniqueref_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text("import urdemo\nprint('ur', urdemo.check())\n", encoding="utf-8")
    exe = tmp_path / "main_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    env["PCC_SELF_LINK"] = "cc"
    env["PCC_RUNTIME_CC"] = "cc"
    proc = subprocess.run(
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
        timeout=180,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=30, env=env
    )
    assert run.returncode == 0, run.stderr
    assert "ur 3" in run.stdout, run.stdout


def test_pcc_native_import_and_vectorcall_under_self_backend_no_libpython(tmp_path):
    """Behavioral regression for PyImport_Import (-> PyImport_ImportModule) and
    PyVectorcall_Call (-> PyObject_Call), batch 14, both referenced by the full
    numpy _core. The caller extension imports the sibling `demo` extension by name
    and invokes demo.add(2, 3) through the vectorcall entry. check() returns 7
    (import non-NULL, attr reachable, call == 5) under strict
    --python-libpython=off --backend self."""
    site = _compile_demo_extension(tmp_path)
    _compile_import_vectorcall_extension(tmp_path)  # same site as demo
    main = tmp_path / "main.py"
    main.write_text("import imdemo\nprint('im', imdemo.check())\n", encoding="utf-8")
    exe = tmp_path / "main_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    env["PCC_SELF_LINK"] = "cc"
    env["PCC_RUNTIME_CC"] = "pcc"
    proc = subprocess.run(
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
        timeout=180,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=30, env=env
    )
    assert run.returncode == 0, run.stderr
    assert "im 7" in run.stdout, run.stdout


def test_pcc_native_type_vectorcall_dispatch_under_self_backend_no_libpython(tmp_path):
    """A C-extension type using NumPy's vectorcall layout dispatches its
    instance vectorcall slot for positional and keyword calls without recursing
    through ``tp_call = PyVectorcall_Call``."""
    site = _compile_type_vectorcall_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import typevcdemo\n"
        "def target(value, scale=1):\n"
        "    return value * scale\n"
        "vector = typevcdemo.make()\n"
        "def slice_vector(value):\n"
        "    return value[:, 1:5:2, None]\n"
        "items = []\n"
        "for item in vector:\n"
        "    items.append(item)\n"
        "print('typevc', vector(2, 3), vector(2, scale=4), "
        "typevcdemo.call_keyword(target), items, slice_vector(vector), "
        "typevcdemo.check_builtin_type(int), typevcdemo.call_method_offset())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe, runtime_cc="pcc")
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert "typevc 23 241 8 [5, 6] 73 1 1024" in run.stdout, run.stdout


def test_pcc_native_type_number_slot_subtract_under_self_backend_no_libpython(
    tmp_path,
):
    """Dynamic C-extension values use ``tp_as_number->nb_subtract``."""
    site = _compile_type_number_slot_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import typenumdemo\n"
        "left = typenumdemo.make(9)\n"
        "right = typenumdemo.make(4)\n"
        "negative = typenumdemo.make(-9)\n"
        "zero = typenumdemo.make(0)\n"
        "larger = typenumdemo.make(10)\n"
        "print('typenum', left - right, abs(negative), bool(zero), bool(left), left < larger, left + right, 2 + left, left + 0.5, 0.5 + left, left * right, 2 * left, left / 2, 18 / left)\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert (
        "typenum 5 9 False True True 13 11 9.5 9.5 36 18 4.5 2.0" in run.stdout
    ), run.stdout


def test_pcc_native_sys_getobject_flags_under_self_backend_no_libpython(tmp_path):
    """Behavioral regression for PySys_GetObject (batch 15) — numpy's
    npy_static_data init reads sys.flags.optimize at IMPORT and fails on NULL.
    check() returns 7: PySys_GetObject("flags") is a non-NULL real namespace,
    flags.optimize == 0 (accurate for pcc's no-O compile, matching numpy's exact
    PyObject_GetAttrString pattern), and an unprovided sys attr returns NULL —
    under strict --python-libpython=off --backend self."""
    site = _compile_sysflags_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text("import sfdemo\nprint('sf', sfdemo.check())\n", encoding="utf-8")
    exe = tmp_path / "main_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    env["PCC_SELF_LINK"] = "cc"
    env["PCC_RUNTIME_CC"] = "cc"
    proc = subprocess.run(
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
        timeout=180,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=30, env=env
    )
    assert run.returncode == 0, run.stderr
    assert "sf 7" in run.stdout, run.stdout


def test_pcc_native_generic_getdict_under_self_backend_no_libpython(tmp_path):
    """Behavioral regression for PyObject_GenericGetDict (batch 16), which numpy
    installs as a `__dict__` getset. A module-level function gets the module as
    self; check() returns 3 when PyObject_GenericGetDict(self, NULL) is a non-NULL
    dict that contains the module's own "check" entry — under strict
    --python-libpython=off --backend self."""
    site = _compile_genericgetdict_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text("import gddemo\nprint('gd', gddemo.check())\n", encoding="utf-8")
    exe = tmp_path / "main_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    env["PCC_SELF_LINK"] = "cc"
    env["PCC_RUNTIME_CC"] = "cc"
    proc = subprocess.run(
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
        timeout=180,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=30, env=env
    )
    assert run.returncode == 0, run.stderr
    assert "gd 3" in run.stdout, run.stdout


def test_pcc_native_seqiter_under_self_backend_no_libpython(tmp_path):
    """Behavioral regression for PySeqIter_New (batch 17) — a real sequence
    iterator driven through the C-API PyIter_Next path. check() returns 1 when
    iterating the tuple (10,20,30) yields exactly those 3 values (sum 60) then
    NULL, under strict --python-libpython=off --backend self."""
    site = _compile_seqiter_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text("import sidemo\nprint('si', sidemo.check())\n", encoding="utf-8")
    exe = tmp_path / "main_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    env["PCC_SELF_LINK"] = "cc"
    env["PCC_RUNTIME_CC"] = "pcc"
    proc = subprocess.run(
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
        timeout=180,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=30, env=env
    )
    assert run.returncode == 0, run.stderr
    assert "si 1" in run.stdout, run.stdout


def test_pcc_native_pymethod_new_under_self_backend_no_libpython(tmp_path):
    """Behavioral regression for PyMethod_New (batch 17) — binds a function to a
    self via the runtime's instance-method machinery. check() returns 7 when the
    func is reachable, PyMethod_New(func, self) is non-NULL, and the result is a
    real callable, under strict --python-libpython=off --backend self."""
    site = _compile_pymethod_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text("import mndemo\nprint('mn', mndemo.check())\n", encoding="utf-8")
    exe = tmp_path / "main_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    env["PCC_SELF_LINK"] = "cc"
    env["PCC_RUNTIME_CC"] = "cc"
    proc = subprocess.run(
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
        timeout=180,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=30, env=env
    )
    assert run.returncode == 0, run.stderr
    assert "mn 7" in run.stdout, run.stdout


def test_pcc_native_batch18_host_symbols_under_self_backend_no_libpython(tmp_path):
    """Behavioral regression for batch 18 full-module host symbols (PyDict_Copy,
    PyDict_Merge, PyUnicode_Format, PyObject_GenericGetAttr/SetAttr), all routed
    to existing pcc primitives. check() returns 15 when copy/merge/format and the
    generic getattr/setattr round-trip all behave, under strict
    --python-libpython=off --backend self."""
    site = _compile_batch18_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text("import b18demo\nprint('b18', b18demo.check())\n", encoding="utf-8")
    exe = tmp_path / "main_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    env["PCC_SELF_LINK"] = "cc"
    env["PCC_RUNTIME_CC"] = "cc"
    proc = subprocess.run(
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
        timeout=180,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=30, env=env
    )
    assert run.returncode == 0, run.stderr
    assert "b18 15" in run.stdout, run.stdout


def test_pcc_native_batch19_host_symbols_under_self_backend_no_libpython(tmp_path):
    """Behavioral regression for batch 19 full-module host symbols (PySlice_New +
    PySlice_GetIndicesEx, PyArg_UnpackTuple, PyDictProxy_New, PyObject_Init).
    check() returns 31 when slice index computation (both an explicit and an
    all-None slice), tuple unpacking, the dict proxy read, and PyObject_Init
    passthrough all behave, under strict --python-libpython=off --backend self."""
    site = _compile_batch19_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text("import b19demo\nprint('b19', b19demo.check())\n", encoding="utf-8")
    exe = tmp_path / "main_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    env["PCC_SELF_LINK"] = "cc"
    env["PCC_RUNTIME_CC"] = "cc"
    proc = subprocess.run(
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
        timeout=180,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=30, env=env
    )
    assert run.returncode == 0, run.stderr
    assert "b19 31" in run.stdout, run.stdout


def test_pcc_native_batch2021_host_symbols_under_self_backend_no_libpython(tmp_path):
    """Behavioral regression for batch 20/21 host surface (PyUnicode_KIND,
    PyUnicode_READ_CHAR, PyUnicode_1BYTE_DATA, PyNumber_Divmod, _Py_HashDouble) —
    the symbols that let all 98 numpy _core files compile and link. check()
    returns 15 when KIND/READ_CHAR/1BYTE_DATA read the UTF-8 buffer correctly,
    PyNumber_Divmod(17,5)==(3,2), and _Py_HashDouble(NULL,2.0)==2, under strict
    --python-libpython=off --backend self."""
    site = _compile_batch2021_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text("import b21demo\nprint('b21', b21demo.check())\n", encoding="utf-8")
    exe = tmp_path / "main_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    env["PCC_SELF_LINK"] = "cc"
    env["PCC_RUNTIME_CC"] = "cc"
    proc = subprocess.run(
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
        timeout=180,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=30, env=env
    )
    assert run.returncode == 0, run.stderr
    assert "b21 15" in run.stdout, run.stdout


def test_pcc_native_unicode_writer_under_self_backend_no_libpython(tmp_path):
    site = _compile_unicode_writer_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import writerdemo\nprint('writer', writerdemo.check())\n", encoding="utf-8"
    )
    exe = tmp_path / "main_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    env["PCC_SELF_LINK"] = "cc"
    env["PCC_RUNTIME_CC"] = "cc"
    proc = subprocess.run(
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
        timeout=180,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=30, env=env
    )
    assert run.returncode == 0, run.stderr
    assert "writer 7" in run.stdout, run.stdout


def test_pcc_native_decode_heaptype_batch_under_self_backend_no_libpython(tmp_path):
    site = _compile_decode_heaptype_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import decodeheapdemo\nprint('decodeheap', decodeheapdemo.check())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    env["PCC_SELF_LINK"] = "cc"
    env["PCC_RUNTIME_CC"] = "cc"
    proc = subprocess.run(
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
        timeout=180,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=30, env=env
    )
    assert run.returncode == 0, run.stderr
    assert "decodeheap 15" in run.stdout, run.stdout


def test_pcc_native_simplejson_final_api_batch_under_self_backend_no_libpython(
    tmp_path,
):
    site = _compile_simplejson_final_api_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "class Target:\n"
        "    def combine(self, a, b):\n"
        "        return a * 10 + b\n"
        "import sjfinaldemo\n"
        "print('sjfinal', sjfinaldemo.check(Target()))\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    env["PCC_SELF_LINK"] = "cc"
    env["PCC_RUNTIME_CC"] = "cc"
    proc = subprocess.run(
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
        timeout=180,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=30, env=env
    )
    assert run.returncode == 0, run.stderr
    assert "sjfinal 15" in run.stdout, run.stdout


def test_capi_import_sees_compiled_python_module_under_self_backend_no_libpython(
    tmp_path,
):
    site = _compile_compiled_module_import_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import depmod\n"
        "import compiledimportdemo\n"
        "print('compiledimport', compiledimportdemo.lookup())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    env["PCC_SELF_LINK"] = "cc"
    env["PCC_RUNTIME_CC"] = "cc"
    env["PCC_RUNTIME_HIGH"] = "c"
    proc = subprocess.run(
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
        timeout=180,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=30, env=env
    )
    assert run.returncode == 0, run.stderr
    assert "compiledimport 41" in run.stdout, run.stdout


def test_capi_import_sees_native_math_module_under_self_backend_no_libpython(
    tmp_path,
):
    site = _compile_builtin_module_import_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import math\n"
        "import builtinimportdemo\n"
        "print('builtinimport', builtinimportdemo.lookup())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    env["PCC_SELF_LINK"] = "cc"
    env["PCC_RUNTIME_CC"] = "cc"
    env["PCC_RUNTIME_HIGH"] = "c"
    proc = subprocess.run(
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
        timeout=180,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=30, env=env
    )
    assert run.returncode == 0, run.stderr
    assert "builtinimport 7" in run.stdout, run.stdout


def test_capi_import_sees_explicit_pcc_python_math_port_under_self_backend(
    tmp_path,
):
    site = _compile_builtin_module_import_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import math\n"
        "import builtinimportdemo\n"
        "print('builtinimport', builtinimportdemo.lookup())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    env["PCC_SELF_LINK"] = "cc"
    env["PCC_RUNTIME_CC"] = "cc"
    env["PCC_RUNTIME_HIGH"] = "c"
    proc = subprocess.run(
        [
            "uv",
            "run",
            "python",
            str(REPO / "scripts" / "pcc_multi.py"),
            "--entry",
            "main",
            "--out",
            str(exe),
            "--backend",
            "self",
            "--python-libpython",
            "off",
            "--ir-scaffold",
            "on",
            f"{main}=main",
            f"{REPO / 'pcc' / 'py_stdlib' / 'math.py'}=math",
        ],
        text=True,
        capture_output=True,
        timeout=180,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=30, env=env
    )
    assert run.returncode == 0, run.stderr
    assert "builtinimport 7" in run.stdout, run.stdout


def test_capi_import_sees_pcc_python_builtin_module_graph_under_self_backend(
    tmp_path,
):
    site = _compile_builtin_module_graph_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import math\n"
        "import sys\n"
        "import time\n"
        "import gc\n"
        "import copy\n"
        "import builtinmodulegraphdemo\n"
        "print('builtinmodulegraph', builtinmodulegraphdemo.check())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    env["PCC_SELF_LINK"] = "cc"
    env["PCC_RUNTIME_CC"] = "cc"
    env["PCC_RUNTIME_HIGH"] = "c"
    proc = subprocess.run(
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
        timeout=180,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=30, env=env
    )
    assert run.returncode == 0, run.stderr
    assert "builtinmodulegraph 5" in run.stdout, run.stdout


def test_pcc_native_batch22_host_symbols_under_self_backend_no_libpython(tmp_path):
    """Behavioral regression for batch 22 host symbols (PyLong_FromUnicodeObject,
    PyFloat_FromString, PyLong_AsLongLongAndOverflow, PySlice_AdjustIndices) from
    numpy's C++ umath layer. check() returns 15 when str->int, str->float,
    long-long-with-overflow, and slice-index adjustment all behave, under strict
    --python-libpython=off --backend self."""
    site = _compile_batch22_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text("import b22demo\nprint('b22', b22demo.check())\n", encoding="utf-8")
    exe = tmp_path / "main_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    env["PCC_SELF_LINK"] = "cc"
    env["PCC_RUNTIME_CC"] = "cc"
    proc = subprocess.run(
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
        timeout=180,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=30, env=env
    )
    assert run.returncode == 0, run.stderr
    assert "b22 15" in run.stdout, run.stdout


def test_pcc_native_set_type_under_self_backend_no_libpython(tmp_path):
    """Behavioral regression for Py_SET_TYPE -> pcc_capi_set_type (batch 23), the
    sole symbol undefined when link-testing numpy's full _core. check() returns 3
    when a fresh Foo instance reports type Foo and, after Py_SET_TYPE(o, &BarType),
    reports type Bar, under strict --python-libpython=off --backend self."""
    site = _compile_settype_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text("import stdemo2\nprint('st2', stdemo2.check())\n", encoding="utf-8")
    exe = tmp_path / "main_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    env["PCC_SELF_LINK"] = "cc"
    env["PCC_RUNTIME_CC"] = "cc"
    proc = subprocess.run(
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
        timeout=180,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=30, env=env
    )
    assert run.returncode == 0, run.stderr
    assert "st2 3" in run.stdout, run.stdout


def test_pcc_native_multiphase_init_under_self_backend_no_libpython(tmp_path):
    """Behavioral regression for PEP 489 multi-phase C-extension init (numpy's
    _multiarray_umath uses it): PyInit returns PyModuleDef_Init(&def) with a
    Py_mod_exec slot, and the no-libpython loader must build the module + run the
    exec slot. The slot registers answer=42; this asserts mpdemo.answer == 42,
    proving the loader executes Py_mod_exec under --python-libpython=off."""
    site = _compile_multiphase_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text("import mpdemo\nprint('mp', mpdemo.answer)\n", encoding="utf-8")
    exe = tmp_path / "main_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    env["PCC_SELF_LINK"] = "cc"
    env["PCC_RUNTIME_CC"] = "cc"
    proc = subprocess.run(
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
        timeout=180,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=30, env=env
    )
    assert run.returncode == 0, run.stderr
    assert "mp 42" in run.stdout, run.stdout


def test_pcc_native_extension_capsule_roundtrip_under_self_backend_no_libpython(
    tmp_path,
):
    site = _compile_capsule_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import capsdemo\n"
        "print(capsdemo.value())\n"
        "print(capsdemo.context_score())\n"
        "print(capsdemo.destructor_score())\n"
        "print(capsdemo.pointer_score())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    env["PCC_SELF_LINK"] = "cc"
    env["PCC_RUNTIME_CC"] = "cc"
    proc = subprocess.run(
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
        timeout=180,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["37", "48", "13", "31"]


def test_pcc_native_extension_capsule_import_loads_provider_under_self_backend_no_libpython(
    tmp_path,
):
    site = _compile_capsule_import_extensions(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import capicons\n"
        "print(capicons.value())\n"
        "print(capicons.table_score())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    env["PCC_SELF_LINK"] = "cc"
    env["PCC_RUNTIME_CC"] = "cc"
    proc = subprocess.run(
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
        timeout=180,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["73", "151"]


def test_pcc_native_extension_numpy_capi_provider_minimal_array_metadata(tmp_path):
    site = _compile_numpy_capi_provider_extensions(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import pccnpcons\n"
        "print(pccnpcons.table_shape_score())\n"
        "print(pccnpcons.type_object_score())\n"
        "print(pccnpcons.type_object_from_type_score())\n"
        "print(pccnpcons.unsupported_score())\n"
        "print(pccnpcons.ufunc_registration_score())\n"
        "print(pccnpcons.inferred_dtype_score())\n"
        "print(pccnpcons.descr_score())\n"
        "print(pccnpcons.fromany_score())\n"
        "print(pccnpcons.array_metadata_score())\n"
        "print(pccnpcons.pointer_metadata_score())\n"
        "print(pccnpcons.dtype_field_score())\n"
        "print(pccnpcons.dtype_classification_score())\n"
        "print(pccnpcons.scalar_kind_score())\n"
        "print(pccnpcons.can_coerce_scalar_score())\n"
        "print(pccnpcons.can_cast_scalar_score())\n"
        "print(pccnpcons.convert_common_type_score())\n"
        "print(pccnpcons.state_byteorder_score())\n"
        "print(pccnpcons.from_macro_score())\n"
        "print(pccnpcons.fill_byteorder_score())\n"
        "print(pccnpcons.shape_compare_score())\n"
        "print(pccnpcons.empty_zeros_score())\n"
        "print(pccnpcons.equiv_types_score())\n"
        "print(pccnpcons.new_from_descr_score())\n"
        "print(pccnpcons.new_score())\n"
        "print(pccnpcons.array_check_score())\n"
        "print(pccnpcons.size_itemsize_score())\n"
        "print(pccnpcons.array_size_score())\n"
        "print(pccnpcons.accessor_score())\n"
        "print(pccnpcons.core_provider_score())\n"
        "print(pccnpcons.base_object_score())\n"
        "print(pccnpcons.return_score())\n"
        "print(pccnpcons.flags_mutation_score())\n"
        "print(pccnpcons.copy_into_score())\n"
        "print(pccnpcons.to_scalar_score())\n"
        "print(pccnpcons.copy_score())\n"
        "print(pccnpcons.new_copy_score())\n"
        "print(pccnpcons.copy_object_score())\n"
        "print(pccnpcons.resize_score())\n"
        "print(pccnpcons.new_like_score())\n"
        "print(pccnpcons.view_score())\n"
        "print(pccnpcons.squeeze_score())\n"
        "print(pccnpcons.transpose_score())\n"
        "print(pccnpcons.swap_axes_score())\n"
        "print(pccnpcons.ravel_score())\n"
        "print(pccnpcons.flatten_score())\n"
        "print(pccnpcons.take_from_score())\n"
        "print(pccnpcons.put_to_score())\n"
        "print(pccnpcons.put_mask_score())\n"
        "print(pccnpcons.repeat_score())\n"
        "print(pccnpcons.choose_score())\n"
        "print(pccnpcons.concatenate_score())\n"
        "print(pccnpcons.arange_score())\n"
        "print(pccnpcons.arange_obj_score())\n"
        "print(pccnpcons.inner_product_score())\n"
        "print(pccnpcons.matrix_product_score())\n"
        "print(pccnpcons.matrix_product2_score())\n"
        "print(pccnpcons.correlate_score())\n"
        "print(pccnpcons.correlate2_score())\n"
        "print(pccnpcons.lexsort_score())\n"
        "print(pccnpcons.sort_score())\n"
        "print(pccnpcons.argsort_score())\n"
        "print(pccnpcons.searchsorted_score())\n"
        "print(pccnpcons.nonzero_score())\n"
        "print(pccnpcons.count_nonzero_score())\n"
        "print(pccnpcons.min_scalar_type_score())\n"
        "print(pccnpcons.sorted_stride_perm_score())\n"
        "print(pccnpcons.remove_axes_in_place_score())\n"
        "print(pccnpcons.where_score())\n"
        "print(pccnpcons.compress_score())\n"
        "print(pccnpcons.diagonal_score())\n"
        "print(pccnpcons.trace_score())\n"
        "print(pccnpcons.clip_score())\n"
        "print(pccnpcons.conjugate_score())\n"
        "print(pccnpcons.std_score())\n"
        "print(pccnpcons.round_score())\n"
        "print(pccnpcons.sum_score())\n"
        "print(pccnpcons.cumsum_score())\n"
        "print(pccnpcons.prod_score())\n"
        "print(pccnpcons.cumprod_score())\n"
        "print(pccnpcons.max_score())\n"
        "print(pccnpcons.min_score())\n"
        "print(pccnpcons.ptp_score())\n"
        "print(pccnpcons.mean_score())\n"
        "print(pccnpcons.any_score())\n"
        "print(pccnpcons.all_score())\n"
        "print(pccnpcons.argmax_score())\n"
        "print(pccnpcons.argmin_score())\n"
        "print(pccnpcons.reshape_score())\n"
        "print(pccnpcons.newshape_score())\n"
        "print(pccnpcons.ensure_array_score())\n"
        "print(pccnpcons.descr_check_score())\n"
        "print(pccnpcons.descr_new_from_type_score())\n"
        "print(pccnpcons.descr_new_score())\n"
        "print(pccnpcons.descr_new_byteorder_score())\n"
        "print(pccnpcons.can_cast_safely_score())\n"
        "print(pccnpcons.can_cast_to_score())\n"
        "print(pccnpcons.can_cast_type_array_score())\n"
        "print(pccnpcons.casting_converter_score())\n"
        "print(pccnpcons.zero_one_score())\n"
        "print(pccnpcons.object_type_score())\n"
        "print(pccnpcons.descr_from_object_score())\n"
        "print(pccnpcons.descr_converter_score())\n"
        "print(pccnpcons.descr_align_converter_score())\n"
        "print(pccnpcons.descr_from_scalar_score())\n"
        "print(pccnpcons.descr_from_type_object_score())\n"
        "print(pccnpcons.scalar_score())\n"
        "print(pccnpcons.scalar_as_ctype_score())\n"
        "print(pccnpcons.from_scalar_score())\n"
        "print(pccnpcons.cast_scalar_to_ctype_score())\n"
        "print(pccnpcons.cast_scalar_direct_score())\n"
        "print(pccnpcons.pack_score())\n"
        "print(pccnpcons.from_array_score())\n"
        "print(pccnpcons.cast_to_type_score())\n"
        "print(pccnpcons.fill_with_scalar_score())\n"
        "print(pccnpcons.to_list_score())\n"
        "print(pccnpcons.to_string_score())\n"
        "print(pccnpcons.byteswap_score())\n"
        "print(pccnpcons.from_string_score())\n"
        "print(pccnpcons.from_buffer_score())\n"
        "print(pccnpcons.buffer_converter_score())\n"
        "print(pccnpcons.from_iter_score())\n"
        "print(pccnpcons.converter_score())\n"
        "print(pccnpcons.pyint_converter_score())\n"
        "print(pccnpcons.intp_from_sequence_score())\n"
        "print(pccnpcons.intp_converter_score())\n"
        "print(pccnpcons.optional_intp_converter_score())\n"
        "print(pccnpcons.promote_types_score())\n"
        "print(pccnpcons.result_type_score())\n"
        "print(pccnpcons.priority_score())\n"
        "print(pccnpcons.check_strides_score())\n"
        "print(pccnpcons.broadcast_to_shape_score())\n"
        "print(pccnpcons.broadcast_multi_score())\n"
        "print(pccnpcons.multi_iter_from_objects_score())\n"
        "print(pccnpcons.remove_smallest_score())\n"
        "print(pccnpcons.multi_iter_score())\n"
        "print(pccnpcons.iter_all_but_axis_score())\n"
        "print(pccnpcons.list_pointer_score())\n"
        "print(pccnpcons.overflow_multiply_score())\n"
        "print(pccnpcons.endianness_score())\n"
        "print(pccnpcons.feature_version_score())\n"
        "print(pccnpcons.version_score())\n"
        "print(pccnpcons.check_axis_score())\n"
        "print(pccnpcons.clipmode_converter_score())\n"
        "print(pccnpcons.clipmode_sequence_score())\n"
        "print(pccnpcons.output_converter_score())\n"
        "print(pccnpcons.searchside_converter_score())\n"
        "print(pccnpcons.byteorder_converter_score())\n"
        "print(pccnpcons.sortkind_converter_score())\n"
        "print(pccnpcons.selectkind_converter_score())\n"
        "print(pccnpcons.order_converter_score())\n"
        "print(pccnpcons.bool_converter_score())\n"
        "print(pccnpcons.optional_bool_converter_score())\n"
        "print(pccnpcons.axis_converter_score())\n"
        "print(pccnpcons.element_strides_score())\n"
        "print(pccnpcons.valid_type_score())\n"
        "print(pccnpcons.item_refcount_score())\n"
        "print(pccnpcons.array_refcount_score())\n"
        "print(pccnpcons.allocator_score())\n"
        "print(pccnpcons.datamem_score())\n"
        "print(pccnpcons.datamem_user_score())\n"
        "print(pccnpcons.array_free_score())\n"
        "print(pccnpcons.as_carray_score())\n"
        "print(pccnpcons.fail_writeable_score())\n"
        "print(pccnpcons.writeback_if_copy_score())\n"
        "print(pccnpcons.discard_writeback_if_copy_score())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == [
        "123",
        "1111111111",
        "1111111",
        "101",
        "111111111111111",
        "11111111111111111",
        "11111111111111",
        "1111111111111111111",
        "1111111111111111111",
        "11111",
        "11111",
        "111111111",
        "11111111",
        "111111",
        "111111",
        "111111",
        "1111111111",
        "1111111111",
        "11111",
        "111111",
        "111111",
        "11111111",
        "11111",
        "111111",
        "1111",
        "1111",
        "1111",
        "1111111",
        "111111",
        "11111",
        "1111",
        "1111111",
        "11111",
        "111",
        "1111",
        "11111",
        "1111",
        "1111",
        "1111",
        "11",
        "111",
        "111",
        "1111111",
        "111",
        "1111",
        "1111",
        "1111",
        "111",
        "1111",
        "1111",
        "11111111",
        "111111",
        "111111",
        "111111",
        "11111111",
        "11111111",
        "111111111",
        "111111",
        "111111",
        "11111",
        "111111",
        "111111111",
        "11111",
        "11111111",
        "11111111",
        "1111",
        "11111",
        "1111111",
        "11111111",
        "11111111",
        "11111111",
        "11111111",
        "1111111",
        "11111111111",
        "11111111",
        "111111111",
        "1111111111",
        "111111111",
        "1111111111",
        "111111111",
        "111111111",
        "111111111",
        "1111111111",
        "1111111111",
        "1111111111",
        "11111111",
        "11111111",
        "111111111",
        "111111111111",
        "1111",
        "1111",
        "11111",
        "111111",
        "11111111",
        "111111111",
        "1111",
        "1111111",
        "111111111",
        "111111",
        "111111111",
        "111111",
        "111111",
        "11111",
        "11111",
        "111111",
        "111111",
        "1111",
        "11111",
        "1111",
        "1111",
        "1111",
        "111",
        "111",
        "11",
        "11",
        "11",
        "11",
        "1111",
        "111111",
        "11111",
        "1111",
        "1111",
        "11111111111111",
        "111111",
        "11111",
        "11111",
        "11111",
        "11111",
        "11111",
        "1111111",
        "111111",
        "11111",
        "111111",
        "11111",
        "111111",
        "111111",
        "11111",
        "1111",
        "111",
        "111",
        "1111",
        "1111111",
        "1111111",
        "111111",
        "111",
        "1111",
        "11111111",
        "11111111",
        "111111",
        "11111111",
        "111111",
        "111111",
        "11111",
        "111",
        "111",
        "1111",
        "1111",
        "111",
        "1111",
        "11111",
        "111",
        "111",
        "111",
        "111111111",
        "11111111",
    ]


def test_pcc_native_extension_numpy_capi_provider_debug_print(tmp_path):
    site = _compile_numpy_capi_provider_extensions(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import pccnpcons\n"
        "print('debug-start')\n"
        "print(pccnpcons.debug_print_score())\n"
        "print('debug-end')\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    lines = run.stdout.splitlines()
    assert lines[0] == "debug-start"
    assert "111" in lines
    assert lines[-1] == "debug-end"
    assert " Dump of pcc NumPy ndarray at address" in run.stdout
    assert " ndim   : 2" in run.stdout
    assert " shape  : 2 3" in run.stdout
    assert " dtype  : NPY_INT" in run.stdout
    assert " It's NULL!" in run.stdout
    assert " not a pcc NumPy array" in run.stdout


def test_pcc_native_extension_numpy_capi_provider_einstein_sum(tmp_path):
    site = _compile_numpy_capi_provider_extensions(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import pccnpcons\n" "print(pccnpcons.einstein_sum_score())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["1111111"]


def test_pcc_native_extension_numpy_capi_provider_partition_argpartition(tmp_path):
    site = _compile_numpy_capi_provider_extensions(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import pccnpcons\n" "print(pccnpcons.partition_score())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["11111111"]


def test_pcc_native_extension_numpy_capi_provider_searchsorted_sorter(tmp_path):
    site = _compile_numpy_capi_provider_extensions(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import pccnpcons\n" "print(pccnpcons.searchsorted_score())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["111111111"]


def test_pcc_native_extension_numpy_capi_provider_cumsum_out(tmp_path):
    site = _compile_numpy_capi_provider_extensions(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import pccnpcons\n" "print(pccnpcons.cumsum_score())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["1111111111"]


def test_pcc_native_extension_numpy_capi_provider_nonzero_2d(tmp_path):
    site = _compile_numpy_capi_provider_extensions(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import pccnpcons\n" "print(pccnpcons.nonzero_score())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["11111"]


def test_pcc_native_extension_numpy_capi_provider_where_2d(tmp_path):
    site = _compile_numpy_capi_provider_extensions(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import pccnpcons\n" "print(pccnpcons.where_score())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["1111111"]


def test_pcc_native_extension_numpy_capi_provider_compress_2d(tmp_path):
    site = _compile_numpy_capi_provider_extensions(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import pccnpcons\n" "print(pccnpcons.compress_score())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["11111111"]


def test_pcc_native_extension_numpy_capi_provider_diagonal_axes(tmp_path):
    site = _compile_numpy_capi_provider_extensions(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import pccnpcons\n" "print(pccnpcons.diagonal_score())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["11111111"]


def test_pcc_native_extension_numpy_capi_provider_trace_rtype_axes(tmp_path):
    site = _compile_numpy_capi_provider_extensions(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import pccnpcons\n" "print(pccnpcons.trace_score())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["11111111"]


def test_pcc_native_extension_numpy_capi_provider_std_rtype(tmp_path):
    site = _compile_numpy_capi_provider_extensions(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import pccnpcons\n" "print(pccnpcons.std_score())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["11111111111"]


def test_pcc_native_extension_numpy_capi_provider_std_scalar_out(tmp_path):
    site = _compile_numpy_capi_provider_extensions(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import pccnpcons\n" "print(pccnpcons.std_score())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["11111111111"]


def test_pcc_native_extension_numpy_capi_provider_sum_rtype(tmp_path):
    site = _compile_numpy_capi_provider_extensions(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import pccnpcons\n" "print(pccnpcons.sum_score())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["111111111"]


def test_pcc_native_extension_numpy_capi_provider_sum_scalar_out(tmp_path):
    site = _compile_numpy_capi_provider_extensions(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import pccnpcons\n" "print(pccnpcons.sum_score())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["111111111"]


def test_pcc_native_extension_numpy_capi_provider_prod_rtype(tmp_path):
    site = _compile_numpy_capi_provider_extensions(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import pccnpcons\n" "print(pccnpcons.prod_score())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["111111111"]


def test_pcc_native_extension_numpy_capi_provider_prod_scalar_out(tmp_path):
    site = _compile_numpy_capi_provider_extensions(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import pccnpcons\n" "print(pccnpcons.prod_score())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["111111111"]


def test_pcc_native_extension_numpy_capi_provider_cumsum_rtype(tmp_path):
    site = _compile_numpy_capi_provider_extensions(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import pccnpcons\n" "print(pccnpcons.cumsum_score())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["1111111111"]


def test_pcc_native_extension_numpy_capi_provider_cumprod_rtype(tmp_path):
    site = _compile_numpy_capi_provider_extensions(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import pccnpcons\n" "print(pccnpcons.cumprod_score())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["1111111111"]


def test_pcc_native_extension_numpy_capi_provider_cumprod_out(tmp_path):
    site = _compile_numpy_capi_provider_extensions(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import pccnpcons\n" "print(pccnpcons.cumprod_score())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["1111111111"]


def test_pcc_native_extension_numpy_capi_provider_max_scalar_out(tmp_path):
    site = _compile_numpy_capi_provider_extensions(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import pccnpcons\n" "print(pccnpcons.max_score())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["111111111"]


def test_pcc_native_extension_numpy_capi_provider_min_scalar_out(tmp_path):
    site = _compile_numpy_capi_provider_extensions(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import pccnpcons\n" "print(pccnpcons.min_score())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["111111111"]


def test_pcc_native_extension_numpy_capi_provider_ptp_scalar_out(tmp_path):
    site = _compile_numpy_capi_provider_extensions(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import pccnpcons\n" "print(pccnpcons.ptp_score())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["111111111"]


def test_pcc_native_extension_numpy_capi_provider_mean_rtype(tmp_path):
    site = _compile_numpy_capi_provider_extensions(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import pccnpcons\n" "print(pccnpcons.mean_score())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["1111111111"]


def test_pcc_native_extension_numpy_capi_provider_mean_scalar_out(tmp_path):
    site = _compile_numpy_capi_provider_extensions(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import pccnpcons\n" "print(pccnpcons.mean_score())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["1111111111"]


def test_pcc_native_extension_numpy_capi_provider_check_any_scalar_exact(tmp_path):
    site = _compile_numpy_capi_provider_extensions(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import pccnpcons\n" "print(pccnpcons.check_any_scalar_exact_score())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["11111"]


def test_pcc_native_extension_cache_string_args_and_multiple_methods(tmp_path):
    site = _compile_feature_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import featuredemo\n"
        "import featuredemo as again\n"
        "print(featuredemo.count())\n"
        "print(again.count())\n"
        "print(featuredemo.echo('hello'))\n"
        "print(featuredemo.add(4, 5))\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["1", "1", "hello", "9"]


def test_pcc_native_extension_method_flags_noargs_and_o(tmp_path):
    site = _compile_methodflag_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import flagdemo\n"
        "print(flagdemo.sum(2, 3))\n"
        "print(flagdemo.noargs())\n"
        "print(flagdemo.one(7))\n"
        "try:\n"
        "    flagdemo.noargs(1)\n"
        "    print('unexpected-noargs')\n"
        "except Exception as exc:\n"
        "    print(str(exc))\n"
        "try:\n"
        "    flagdemo.one()\n"
        "    print('unexpected-one-missing')\n"
        "except Exception as exc:\n"
        "    print(str(exc))\n"
        "try:\n"
        "    flagdemo.one(1, 2)\n"
        "    print('unexpected-one-extra')\n"
        "except Exception as exc:\n"
        "    print(str(exc))\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == [
        "5",
        "41",
        "107",
        "method takes no arguments",
        "method takes exactly one argument",
        "method takes exactly one argument",
    ]


def test_pcc_native_extension_method_flags_keywords(tmp_path):
    site = _compile_keywordflag_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import kwdemo\n"
        "print(kwdemo.mix(5))\n"
        "print(kwdemo.mix(5, name='abc'))\n"
        "print(kwdemo.mix(value=7, name='zz'))\n"
        "try:\n"
        "    kwdemo.mix(name='abc')\n"
        "    print('unexpected-missing')\n"
        "except Exception as exc:\n"
        "    print(str(exc))\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == [
        "5",
        "8",
        "9",
        "argument type mismatch",
    ]


def test_pcc_native_extension_optional_truth_and_converter_parse_units(tmp_path):
    site = _compile_keywordflag_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import kwdemo\n"
        "print(kwdemo.options())\n"
        "print(kwdemo.options(coerce=False))\n"
        "print(kwdemo.options(na_object=7))\n"
        "print(kwdemo.options(coerce=False, na_object=7))\n"
        "print(kwdemo.typed_long(9))\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["100", "0", "107", "7", "9"]


def test_pcc_native_extension_singleton_return_macros(tmp_path):
    site = _compile_feature_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import featuredemo\n"
        "print(1 if featuredemo.return_none() is None else 0)\n"
        "print(1 if featuredemo.return_true() else 0)\n"
        "print(1 if not featuredemo.return_false() else 0)\n"
        "print(1 if featuredemo.return_notimplemented() is NotImplemented else 0)\n"
        "print(featuredemo.identity_score())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["1", "1", "1", "1", "11111"]


def test_pcc_native_extension_method_exception_propagates_and_module_still_works(
    tmp_path,
):
    site = _compile_feature_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import featuredemo\n"
        "try:\n"
        "    featuredemo.fail()\n"
        "    print('unexpected-success')\n"
        "except Exception as exc:\n"
        "    print(str(exc))\n"
        "print(featuredemo.add(6, 7))\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["feature method failed", "13"]


def test_pcc_native_extension_imported_callable_exception_is_catchable(tmp_path):
    site = _compile_feature_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "from featuredemo import fail\n"
        "try:\n"
        "    fail()\n"
        "except Exception:\n"
        "    pass\n"
        "print('caught')\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout == "caught\n"


def test_pcc_native_extension_buffer_protocol_for_bytes_bytearray_memoryview(tmp_path):
    site = _compile_buffer_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import bufdemo\n"
        "b = b'ABC'\n"
        "ba = bytearray(b'\\x01\\x02\\x03')\n"
        "mv = memoryview(b'\\x04\\x05\\x06')\n"
        "print(bufdemo.sum(b))\n"
        "print(bufdemo.sum(ba))\n"
        "print(bufdemo.sum(mv))\n"
        "print(bufdemo.writable(b))\n"
        "print(bufdemo.metadata_score(b))\n"
        "print(bufdemo.metadata_score(ba))\n"
        "print(bufdemo.view_sum(ba))\n"
        "print(bufdemo.writable(ba))\n"
        "print(bufdemo.from_memory_sum())\n"
        "print(bufdemo.from_memory_write_sum())\n"
        "print(bufdemo.memoryview_inspect_score(ba))\n"
        "print(bufdemo.check_score(b, ba, mv, 123))\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe, runtime_cc="pcc")
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == [
        "198",
        "6",
        "15",
        "0",
        "3111311",
        "3211311",
        "6",
        "1",
        "24",
        "15",
        "711113111",
        "1111",
    ]


def test_pcc_native_extension_memoryviews_keep_distinct_owned_buffers(tmp_path):
    site = _compile_buffer_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import bufdemo\n"
        "print(bufdemo.two_live_views_score(b'ABC', b'xyz'))\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe, runtime_cc="pcc")
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout == "127\n"


def test_pcc_native_extension_pyobject_call_class_constructor(tmp_path):
    site = _compile_call_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import calldemo\n"
        "class Box:\n"
        "    x: int\n"
        "obj = calldemo.call0(Box)\n"
        "print(isinstance(obj, Box))\n"
        "print(calldemo.bad_args(Box))\n"
        "def answer():\n"
        "    return 42\n"
        "fn = [answer][0]\n"
        "print(calldemo.call0(fn))\n"
        "print(calldemo.call_object0(fn))\n"
        "print(isinstance(calldemo.call_one(Box), Box))\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe, runtime_cc="pcc")
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["True", "1", "42", "42", "True"]


def test_pcc_native_extension_generic_vectorcall_preserves_one_argument(tmp_path):
    site = _compile_generic_vectorcall_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import genericvcdemo\n"
        "print(genericvcdemo.call_one())\n"
        "print(genericvcdemo.vectorcall_one())\n"
        "print(genericvcdemo.vectorcall_method_one())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe, runtime_cc="pcc")
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["1", "1", "1"]


def test_pcc_native_extension_pyobject_call_receives_none_return(tmp_path):
    site = _compile_call_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import calldemo\n"
        "def implicit_none():\n"
        "    value = 1\n"
        "def bare_none():\n"
        "    return\n"
        "implicit_fn = [implicit_none][0]\n"
        "bare_fn = [bare_none][0]\n"
        "print(calldemo.call_is_none(implicit_fn))\n"
        "print(calldemo.call_is_none(bare_fn))\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["1", "1"]


def test_pcc_native_extension_tuple_dict_attr_helpers(tmp_path):
    site = _compile_object_api_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import objectapi\n"
        "class Box:\n"
        "    answer: int\n"
        "box = Box()\n"
        "print(objectapi.tuple_sum())\n"
        "print(objectapi.pack_sum())\n"
        "print(objectapi.dict_answer())\n"
        "print(objectapi.list_sum())\n"
        "print(objectapi.bytes_sum())\n"
        "print(objectapi.bytes_size_sum())\n"
        "print(objectapi.unicode_prefix())\n"
        "print(objectapi.unicode_utf8_size_score())\n"
        "print(objectapi.unicode_decode_length_score())\n"
        "print(objectapi.unicode_encode_concat_score())\n"
        "print(objectapi.unicode_macro_score())\n"
        "print(objectapi.unicode_kind_score())\n"
        "print(objectapi.float_bool_score())\n"
        "print(objectapi.scalar_complex_score())\n"
        "print(objectapi.set_attr(box))\n"
        "print(objectapi.has_attr(box, 'answer'))\n"
        "print(objectapi.has_attr(box, 'missing'))\n"
        "print(objectapi.format_error())\n"
        "print(objectapi.warning_score())\n"
        "print(objectapi.gil_state_score())\n"
        "print(objectapi.error_helper_score())\n"
        "print(objectapi.exception_global_score())\n"
        "print(objectapi.module_dict_magic())\n"
        "print(objectapi.bump_module_global())\n"
        "print(objectapi.SEEN)\n"
        "print(objectapi.bump_module_global())\n"
        "print(objectapi.SEEN)\n"
        "print(objectapi.delete_module_global())\n"
        "print(objectapi.has_attr(objectapi, 'SEEN'))\n"
        "print(objectapi.str_repr_compare())\n"
        "print(objectapi.print_score())\n"
        "print(objectapi.unsigned_sum())\n"
        "print(objectapi.import_self_magic())\n"
        "print(objectapi.sequence_score())\n"
        "print(objectapi.iter_contains_score())\n"
        "print(objectapi.collection_views_score())\n"
        "print(objectapi.set_score())\n"
        "print(objectapi.attr_hash_score())\n"
        "print(objectapi.build_mem_kw_score())\n"
        "print(objectapi.refcnt_macro_score())\n"
        "print(objectapi.memory_os_score())\n"
        "print(objectapi.pythoncapi_compat_score())\n"
        "print(objectapi.mapping_long_exc_score())\n"
        "print(objectapi.call_type_format_score())\n"
        "print(objectapi.call_helper_score())\n"
        "print(objectapi.number_protocol_score())\n"
        "print(objectapi.number_bitwise_score())\n"
        "print(objectapi.has_attr(objectapi, 'CustomError'))\n"
        "print(objectapi.ADDED_REF)\n"
        "print(objectapi.MAGIC)\n"
        "print(objectapi.LABEL)\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe, runtime_cc="pcc")
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == [
        "42",
        "14",
        "42",
        "12",
        "198",
        "3210",
        "abc",
        "107",
        "1111",
        "511111111",
        "1111111111",
        "11111111111111",
        "17",
        "111111111",
        "64",
        "1",
        "0",
        "1",
        "11111",
        "111",
        "111111111",
        "8191",
        "13",
        "1",
        "1",
        "2",
        "2",
        "1",
        "0",
        "111111",
        "111",
        "11",
        "12",
        "71126",
        "1531111111",
        "1111112033",
        "1111111111",
        "6611123",
        "1468",
        "11",
        "11111111",
        "111111111",
        "841243",
        "172562",
        "711082",
        "1046015888",
        "8191",
        "1",
        "77",
        "12",
        "ok",
    ]


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Darwin host-extension errno is a named libSystem boundary",
)
def test_pcc_native_extension_errno_messages_use_saved_thread_errno(tmp_path):
    site = _compile_errno_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import errnoapi\n"
        "print(errnoapi.message())\n"
        "print(errnoapi.filename_message())\n"
        "print(errnoapi.unknown_message())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe, runtime_cc="pcc")
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode == 0, run.stderr
    lines = run.stdout.splitlines()
    assert lines[:2] == [
        "No such file or directory",
        "No such file or directory: missing.dat",
    ]
    assert len(lines) == 3
    assert "9999" in lines[2]


def test_pcc_native_extension_missing_pyinit_symbol_fails_at_runtime(tmp_path):
    site = _compile_missing_init_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text("import badinit\n", encoding="utf-8")
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode != 0
    assert "dlsym failed" in run.stderr
    assert "PyInit_badinit" in run.stderr


def test_pcc_native_extension_init_returning_null_fails_at_runtime(tmp_path):
    site = _compile_null_init_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text("import nullinit\n", encoding="utf-8")
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode != 0
    assert "init failed intentionally" in run.stderr


def test_pcc_native_extension_failed_import_is_not_cached(tmp_path):
    site = _compile_retry_init_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "try:\n"
        "    import retryinit\n"
        "    print('unexpected-success')\n"
        "except Exception as exc:\n"
        "    print(str(exc))\n"
        "import retryinit\n"
        "print(retryinit.count())\n",
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe, {"PCC_RETRYINIT_FAIL_ONCE": "1"})
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["retry init failed once", "1"]


def test_pcc_native_extension_init_returning_null_without_error_reports_import_failure(
    tmp_path,
):
    site = _compile_silent_null_init_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text("import silentnullinit\n", encoding="utf-8")
    exe = tmp_path / "main_bin"
    proc = _compile_main(site, main, exe)
    assert proc.returncode == 0, proc.stderr
    run = _run_main(site, exe)
    assert run.returncode != 0
    assert "native extension init failed" in run.stderr
    assert "silentnullinit" in run.stderr


def _compile_phasedemo_extension(tmp_path: Path) -> Path:
    # numpy-shaped multi-phase (PEP 489) extension covering the C-API shim
    # semantics the C->pcc-Python migration dropped (2026-08-08, see
    # docs/investigations/pcc1-pip-numpy-runtime-import-capi-regressions.md):
    # m_slots@64 (not m_doc@40), Py_mod_exec == 2, PyDict_SetItemString into
    # the live module dict, Py_BuildValue "{..}" pairs, sys.flags.optimize,
    # PyObject_CallFunction full formats, METH_FASTCALL|METH_KEYWORDS nargs,
    # PyArg_ParseTupleAndKeywords, PyMemberDef T_CHAR/T_PYSSIZET codes,
    # tp_richcompare both-operand protocol, and nb_* right-operand dispatch.
    return _compile_extension(
        tmp_path,
        "phasedemo",
        "#define PY_SSIZE_T_CLEAN\n"
        "#include <Python.h>\n"
        "#include <structmember.h>\n"
        "\n"
        "typedef struct { PyObject_HEAD Py_ssize_t psize; char letter; } PhObject;\n"
        "\n"
        "static PyObject *disp_richcompare(PyObject *l, PyObject *r, int op) {\n"
        "    (void)l;\n"
        "    (void)r;\n"
        "    if (op == Py_EQ) return PyBool_FromLong(1);\n"
        "    Py_RETURN_NOTIMPLEMENTED;\n"
        "}\n"
        "\n"
        "static PyMemberDef DispMembers[] = {\n"
        '    {"psize", T_PYSSIZET, __builtin_offsetof(PhObject, psize), READONLY, NULL},\n'
        '    {"letter", T_CHAR, __builtin_offsetof(PhObject, letter), READONLY, NULL},\n'
        "    {NULL, 0, 0, 0, NULL},\n"
        "};\n"
        "\n"
        "static PyTypeObject DispType = {\n"
        "    PyVarObject_HEAD_INIT(NULL, 0)\n"
        '    .tp_name = "phasedemo.Disp",\n'
        "    .tp_basicsize = sizeof(PhObject),\n"
        "    .tp_flags = Py_TPFLAGS_DEFAULT,\n"
        "    .tp_members = DispMembers,\n"
        "    .tp_richcompare = disp_richcompare,\n"
        "    .tp_new = PyType_GenericNew,\n"
        "};\n"
        "\n"
        "static PyTypeObject NoCmpType = {\n"
        "    PyVarObject_HEAD_INIT(NULL, 0)\n"
        '    .tp_name = "phasedemo.NoCmp",\n'
        "    .tp_basicsize = sizeof(PhObject),\n"
        "    .tp_flags = Py_TPFLAGS_DEFAULT,\n"
        "    .tp_new = PyType_GenericNew,\n"
        "};\n"
        "\n"
        "static PyObject *ph_mul(PyObject *a, PyObject *b) {\n"
        "    long v = PyLong_AsLong(a);\n"
        "    if (PyErr_Occurred()) {\n"
        "        PyErr_Clear();\n"
        "        v = PyLong_AsLong(b);\n"
        "    }\n"
        "    if (PyErr_Occurred()) {\n"
        "        PyErr_Clear();\n"
        "        Py_RETURN_NOTIMPLEMENTED;\n"
        "    }\n"
        "    return PyLong_FromLong(v * 7);\n"
        "}\n"
        "\n"
        "static PyNumberMethods NumAsNumber = {\n"
        "    .nb_multiply = ph_mul,\n"
        "};\n"
        "\n"
        "static PyTypeObject NumType = {\n"
        "    PyVarObject_HEAD_INIT(NULL, 0)\n"
        '    .tp_name = "phasedemo.Num",\n'
        "    .tp_basicsize = sizeof(PhObject),\n"
        "    .tp_flags = Py_TPFLAGS_DEFAULT,\n"
        "    .tp_as_number = &NumAsNumber,\n"
        "    .tp_new = PyType_GenericNew,\n"
        "};\n"
        "\n"
        "static PyObject *ph_callback(PyObject *self, PyObject *fn) {\n"
        "    (void)self;\n"
        '    return PyObject_CallFunction(fn, "Os", Py_None, "alias");\n'
        "}\n"
        "\n"
        "static PyObject *ph_fastkw(PyObject *self, PyObject *const *args,\n"
        "                           Py_ssize_t nargs, PyObject *kwnames) {\n"
        "    (void)self;\n"
        "    (void)args;\n"
        "    Py_ssize_t kwn = kwnames == NULL ? 0 : PyTuple_Size(kwnames);\n"
        '    return Py_BuildValue("nn", nargs, kwn);\n'
        "}\n"
        "\n"
        "static PyObject *ph_kwparse(PyObject *self, PyObject *args, PyObject *kwargs) {\n"
        "    (void)self;\n"
        "    long a = 0;\n"
        "    long b = 0;\n"
        '    static char *kwlist[] = {"a", "b", NULL};\n'
        '    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "l|l:kwparse", kwlist, &a, &b)) {\n'
        "        return NULL;\n"
        "    }\n"
        "    return PyLong_FromLong(a + b);\n"
        "}\n"
        "\n"
        "static PyMethodDef PhaseMethods[] = {\n"
        '    {"callback", ph_callback, METH_O, "call fn via CallFunction Os"},\n'
        '    {"fastkw", (PyCFunction)ph_fastkw, METH_FASTCALL | METH_KEYWORDS, "report nargs/kw"},\n'
        '    {"kwparse", (PyCFunction)ph_kwparse, METH_VARARGS | METH_KEYWORDS, "a+b"},\n'
        "    {NULL, NULL, 0, NULL},\n"
        "};\n"
        "\n"
        "static int phase_exec(PyObject *m) {\n"
        "    PyObject *d = PyModule_GetDict(m);\n"
        "    if (d == NULL) return -1;\n"
        "    if (PyType_Ready(&DispType) < 0) return -1;\n"
        "    if (PyType_Ready(&NoCmpType) < 0) return -1;\n"
        "    if (PyType_Ready(&NumType) < 0) return -1;\n"
        "    Py_INCREF((PyObject *)&DispType);\n"
        '    if (PyDict_SetItemString(d, "Disp", (PyObject *)&DispType) < 0) return -1;\n'
        "    Py_INCREF((PyObject *)&NoCmpType);\n"
        '    if (PyDict_SetItemString(d, "NoCmp", (PyObject *)&NoCmpType) < 0) return -1;\n'
        "    Py_INCREF((PyObject *)&NumType);\n"
        '    if (PyDict_SetItemString(d, "Num", (PyObject *)&NumType) < 0) return -1;\n'
        '    PyObject *bv = Py_BuildValue("{ss}", "k", "v");\n'
        "    if (bv == NULL) return -1;\n"
        '    if (PyDict_SetItemString(d, "bv", bv) < 0) { Py_DECREF(bv); return -1; }\n'
        "    Py_DECREF(bv);\n"
        '    PyObject *flags = PySys_GetObject("flags");\n'
        "    if (flags == NULL) return -1;\n"
        '    PyObject *opt = PyObject_GetAttrString(flags, "optimize");\n'
        "    if (opt == NULL) return -1;\n"
        '    if (PyDict_SetItemString(d, "optimize", opt) < 0) { Py_DECREF(opt); return -1; }\n'
        "    Py_DECREF(opt);\n"
        "    return 0;\n"
        "}\n"
        "\n"
        "static PyModuleDef_Slot phase_slots[] = {\n"
        "    {Py_mod_exec, phase_exec},\n"
        "    {0, NULL},\n"
        "};\n"
        "\n"
        "static struct PyModuleDef phasemodule = {\n"
        "    PyModuleDef_HEAD_INIT,\n"
        '    .m_name = "phasedemo",\n'
        '    .m_doc = "m_doc canary: reading m_doc as m_slots must not happen",\n'
        "    .m_size = 0,\n"
        "    .m_methods = PhaseMethods,\n"
        "    .m_slots = phase_slots,\n"
        "};\n"
        "\n"
        "PyMODINIT_FUNC PyInit_phasedemo(void) {\n"
        "    return PyModuleDef_Init(&phasemodule);\n"
        "}\n",
    )


def test_pcc_native_multiphase_capi_surface_default_runtime(tmp_path):
    """Multi-phase (PEP 489) numpy-shaped C-API surface under the DEFAULT
    runtime (pcc-Python ports linked; no PCC_RUNTIME_CC=cc): every assertion
    here corresponds to a hole the 93cfbca5-era C->pcc-Python migration
    dropped and numpy 2.4.6 import tripped over."""
    site = _compile_phasedemo_extension(tmp_path)
    main = tmp_path / "main.py"
    main.write_text(
        "import phasedemo\n"
        'print("disp", phasedemo.Disp is not None)\n'
        'print("bv", phasedemo.bv["k"])\n'
        'print("opt", phasedemo.optimize)\n'
        "def f(a, b):\n"
        "    return (a is None, b)\n"
        'print("cb", phasedemo.callback(f))\n'
        'print("fk", phasedemo.fastkw(1, 2, c=3))\n'
        'print("kw", phasedemo.kwparse(5, b=7))\n'
        "d = phasedemo.Disp()\n"
        'print("mem", d.psize, len(d.letter))\n'
        'print("eq", d == 42, 42 == d)\n'
        "n = phasedemo.NoCmp()\n"
        'print("ncmp", n == 42, n == n)\n'
        "x = phasedemo.Num()\n"
        'print("mul", 6 * x, x * 6)\n',
        encoding="utf-8",
    )
    exe = tmp_path / "main_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env.pop("PCC_RUNTIME_CC", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    env["PCC_SELF_LINK"] = "cc"
    proc = subprocess.run(
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
        timeout=300,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    run_env = env.copy()
    run_env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=30,
        env=run_env,
    )
    assert run.returncode == 0, run.stderr
    lines = run.stdout.splitlines()
    assert "disp True" in lines, run.stdout
    assert "bv v" in lines, run.stdout
    assert "opt 0" in lines, run.stdout
    assert "cb (True, 'alias')" in lines, run.stdout
    assert "fk (2, 1)" in lines, run.stdout
    assert "kw 12" in lines, run.stdout
    assert "mem 0 1" in lines, run.stdout
    assert "eq True True" in lines, run.stdout
    assert "ncmp False True" in lines, run.stdout
    assert "mul 42 42" in lines, run.stdout
