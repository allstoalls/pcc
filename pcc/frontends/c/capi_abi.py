from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CApiSymbol:
    name: str
    category: str
    signature: str
    required_for_extension_import: bool = False


CORE_CAPI_SYMBOLS: tuple[CApiSymbol, ...] = (
    CApiSymbol("Py_INCREF", "refcount", "void(PyObject*)", True),
    CApiSymbol("Py_DECREF", "refcount", "void(PyObject*)", True),
    CApiSymbol("PyLong_FromLong", "int", "PyObject*(long)", True),
    CApiSymbol("PyUnicode_FromString", "unicode", "PyObject*(const char*)", True),
    CApiSymbol("PyErr_SetString", "exceptions", "void(PyObject*, const char*)", True),
    CApiSymbol("PyCapsule_New", "capsule", "PyObject*(void*, const char*, void*)", True),
    CApiSymbol("PyModule_Create2", "module", "PyObject*(PyModuleDef*, int)", True),
)


def symbols_by_category(category: str) -> tuple[CApiSymbol, ...]:
    return tuple(s for s in CORE_CAPI_SYMBOLS if s.category == category)


def extension_import_blockers(implemented: set[str]) -> tuple[CApiSymbol, ...]:
    return tuple(
        s for s in CORE_CAPI_SYMBOLS
        if s.required_for_extension_import and s.name not in implemented
    )
