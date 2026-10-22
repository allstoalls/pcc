"""CPython C-API compatibility surface catalogue.

This is not an implementation of every C-API symbol.  It is the executable
priority map used by extension-loader work so gaps are explicit and tested.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path


class CApiPriority(IntEnum):
    IMPORT_BLOCKER = 0
    RUNTIME_CORE = 1
    ARRAY_CORE = 2
    NUMPY_CAPI = 3
    DOWNSTREAM_EXTENSION = 4
    ACCELERATION = 5


@dataclass(frozen=True)
class CApiSymbol:
    name: str
    header: str
    priority: CApiPriority
    implemented: bool = False
    notes: str = ""


_DEFAULT_SYMBOLS = (
    CApiSymbol("Py_Initialize", "Python.h", CApiPriority.IMPORT_BLOCKER, True),
    CApiSymbol("Py_INCREF", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("Py_DECREF", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("Py_XINCREF", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("Py_XDECREF", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("Py_REFCNT", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("Py_SET_REFCNT", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("Py_NewRef", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("Py_XNewRef", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("Py_CLEAR", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("Py_SETREF", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("Py_XSETREF", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("Py_None", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("Py_True", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("Py_False", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("Py_NotImplemented", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("Py_Is", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("Py_IsNone", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("Py_IsTrue", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("Py_IsFalse", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("Py_RETURN_NONE", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("Py_RETURN_TRUE", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("Py_RETURN_FALSE", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("Py_RETURN_NOTIMPLEMENTED", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyMem_Malloc", "pymem.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyMem_Calloc", "pymem.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyMem_Realloc", "pymem.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyMem_Free", "pymem.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyMem_RawMalloc", "pymem.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyMem_RawCalloc", "pymem.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyMem_RawRealloc", "pymem.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyMem_RawFree", "pymem.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyMem_FREE", "pymem.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyObject_Malloc", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyObject_Calloc", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyObject_Realloc", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyObject_Free", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyObject_MALLOC", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyObject_REALLOC", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyObject_FREE", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyObject_Del", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyObject_DEL", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyOS_snprintf", "Python.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyOS_vsnprintf", "Python.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyModule_Create", "moduleobject.h", CApiPriority.IMPORT_BLOCKER, True),
    CApiSymbol("PyModule_Create2", "moduleobject.h", CApiPriority.IMPORT_BLOCKER, True),
    CApiSymbol(
        "PyModule_AddObject", "moduleobject.h", CApiPriority.IMPORT_BLOCKER, True
    ),
    CApiSymbol(
        "PyModule_AddObjectRef",
        "moduleobject.h",
        CApiPriority.RUNTIME_CORE,
        True,
    ),
    CApiSymbol("PyModule_Add", "moduleobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyArg_ParseTuple", "modsupport.h", CApiPriority.IMPORT_BLOCKER, True),
    CApiSymbol(
        "PyArg_ParseTupleAndKeywords",
        "modsupport.h",
        CApiPriority.RUNTIME_CORE,
        True,
        "Supports pcc-native tuple/dict parsing for l/i/O/s/y and optional arguments.",
    ),
    CApiSymbol(
        "Py_BuildValue",
        "modsupport.h",
        CApiPriority.RUNTIME_CORE,
        True,
        "Builds pcc-native scalars, strings/bytes, objects, and tuples for common formats.",
    ),
    CApiSymbol("PyLong_FromLong", "longobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol(
        "PyLong_FromUnsignedLong", "longobject.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol("PyLong_AsLong", "longobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyLong_FromLongLong", "longobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol(
        "PyLong_FromUnsignedLongLong",
        "longobject.h",
        CApiPriority.RUNTIME_CORE,
        True,
    ),
    CApiSymbol("PyLong_FromInt32", "longobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyLong_FromInt64", "longobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyLong_FromUInt32", "longobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyLong_FromUInt64", "longobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyLong_FromVoidPtr", "longobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyLong_FromSsize_t", "longobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyLong_FromSize_t", "longobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyLong_FromDouble", "longobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyLong_AsLongLong", "longobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyLong_AsDouble", "longobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyLong_AsInt", "longobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyLong_AsInt32", "longobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyLong_AsInt64", "longobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyLong_AsUInt32", "longobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyLong_AsUInt64", "longobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyLong_AsVoidPtr", "longobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol(
        "PyLong_AsLongAndOverflow", "longobject.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol(
        "PyLong_AsUnsignedLong", "longobject.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol(
        "PyLong_AsUnsignedLongLong",
        "longobject.h",
        CApiPriority.RUNTIME_CORE,
        True,
    ),
    CApiSymbol(
        "PyLong_AsUnsignedLongLongMask",
        "longobject.h",
        CApiPriority.RUNTIME_CORE,
        True,
    ),
    CApiSymbol("PyLong_AsSsize_t", "longobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyLong_AsSize_t", "longobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyLong_Check", "longobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyLong_CheckExact", "longobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyBool_FromLong", "boolobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyBool_Check", "boolobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyFloat_FromDouble", "floatobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyFloat_AsDouble", "floatobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyFloat_AS_DOUBLE", "floatobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyFloat_Check", "floatobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyFloat_CheckExact", "floatobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("Py_complex", "complexobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol(
        "PyComplex_FromDoubles", "complexobject.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol(
        "PyComplex_FromCComplex", "complexobject.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol(
        "PyComplex_AsCComplex", "complexobject.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol(
        "PyComplex_RealAsDouble",
        "complexobject.h",
        CApiPriority.RUNTIME_CORE,
        True,
    ),
    CApiSymbol(
        "PyComplex_ImagAsDouble",
        "complexobject.h",
        CApiPriority.RUNTIME_CORE,
        True,
    ),
    CApiSymbol("PyComplex_Check", "complexobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol(
        "PyComplex_CheckExact", "complexobject.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol("Py_UNUSED", "Python.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("Py_UCS1", "unicodeobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("Py_UCS2", "unicodeobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("Py_UCS4", "unicodeobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol(
        "PyUnicode_1BYTE_KIND", "unicodeobject.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol(
        "PyUnicode_2BYTE_KIND", "unicodeobject.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol(
        "PyUnicode_4BYTE_KIND", "unicodeobject.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol(
        "PyUnicode_FromString", "unicodeobject.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol(
        "PyUnicode_FromStringAndSize",
        "unicodeobject.h",
        CApiPriority.RUNTIME_CORE,
        True,
    ),
    CApiSymbol(
        "PyUnicode_FromFormat", "unicodeobject.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol(
        "PyUnicode_FromFormatV", "unicodeobject.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol(
        "PyUnicode_InternFromString",
        "unicodeobject.h",
        CApiPriority.RUNTIME_CORE,
        True,
    ),
    CApiSymbol(
        "PyUnicode_FromKindAndData",
        "unicodeobject.h",
        CApiPriority.RUNTIME_CORE,
        True,
        "Converts UCS1/UCS2/UCS4 buffers into pcc-native UTF-8 strings.",
    ),
    CApiSymbol(
        "PyUnicode_FromOrdinal",
        "unicodeobject.h",
        CApiPriority.RUNTIME_CORE,
        True,
        "Creates a one-codepoint pcc-native string from a Unicode ordinal.",
    ),
    CApiSymbol(
        "PyUnicode_AsUCS4",
        "unicodeobject.h",
        CApiPriority.RUNTIME_CORE,
        True,
        "Copies pcc-native strings into caller-provided UCS4 buffers.",
    ),
    CApiSymbol(
        "PyUnicode_AsUCS4Copy",
        "unicodeobject.h",
        CApiPriority.RUNTIME_CORE,
        True,
        "Allocates a UCS4 copy of pcc-native strings with PyMem_Malloc.",
    ),
    CApiSymbol(
        "PyUnicode_FromEncodedObject",
        "unicodeobject.h",
        CApiPriority.RUNTIME_CORE,
        True,
        "Narrow pcc-native decode path for str and bytes with UTF-8/ASCII/Latin-1 labels.",
    ),
    CApiSymbol(
        "PyUnicode_AsEncodedString",
        "unicodeobject.h",
        CApiPriority.RUNTIME_CORE,
        True,
        "Narrow pcc-native encode path for str to bytes with UTF-8/ASCII/Latin-1 labels.",
    ),
    CApiSymbol("PyUnicode_AsUTF8", "unicodeobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol(
        "PyUnicode_AsUTF8AndSize",
        "unicodeobject.h",
        CApiPriority.RUNTIME_CORE,
        True,
    ),
    CApiSymbol(
        "PyUnicode_AsUTF8String", "unicodeobject.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol(
        "PyUnicode_AsASCIIString", "unicodeobject.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol("PyUnicode_Check", "unicodeobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol(
        "PyUnicode_CheckExact", "unicodeobject.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol(
        "PyUnicode_GetLength", "unicodeobject.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol(
        "PyUnicode_GET_LENGTH", "unicodeobject.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol("PyUnicode_Compare", "unicodeobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol(
        "PyUnicode_CompareWithASCIIString",
        "unicodeobject.h",
        CApiPriority.RUNTIME_CORE,
        True,
    ),
    CApiSymbol(
        "PyUnicode_Tailmatch", "unicodeobject.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol("PyUnicode_Find", "unicodeobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol(
        "PyUnicode_ReadChar", "unicodeobject.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol(
        "PyUnicode_FindChar", "unicodeobject.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol("PyUnicode_Count", "unicodeobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyUnicode_Replace", "unicodeobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol(
        "PyUnicode_Substring", "unicodeobject.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol(
        "PyUnicode_Contains", "unicodeobject.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol("PyUnicode_Concat", "unicodeobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol(
        "PyUnicode_EqualToUTF8",
        "unicodeobject.h",
        CApiPriority.RUNTIME_CORE,
        True,
    ),
    CApiSymbol(
        "PyUnicode_EqualToUTF8AndSize",
        "unicodeobject.h",
        CApiPriority.RUNTIME_CORE,
        True,
    ),
    CApiSymbol(
        "Py_UNICODE_ISSPACE", "unicodeobject.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol(
        "Py_UNICODE_ISDIGIT", "unicodeobject.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol(
        "Py_UNICODE_ISDECIMAL", "unicodeobject.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol(
        "Py_UNICODE_ISNUMERIC", "unicodeobject.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol(
        "Py_UNICODE_ISLOWER", "unicodeobject.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol(
        "Py_UNICODE_ISUPPER", "unicodeobject.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol(
        "Py_UNICODE_ISTITLE", "unicodeobject.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol(
        "Py_UNICODE_ISALPHA", "unicodeobject.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol(
        "Py_UNICODE_ISALNUM", "unicodeobject.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol("PyErr_SetString", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyErr_SetNone", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyErr_SetObject", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol(
        "PyErr_Format",
        "pyerrors.h",
        CApiPriority.RUNTIME_CORE,
        True,
        "Formats common C printf and CPython %R/%S/%U exception messages.",
    ),
    CApiSymbol(
        "PyErr_FormatV",
        "pyerrors.h",
        CApiPriority.RUNTIME_CORE,
        True,
        "va_list form of the same narrow formatter used by PyErr_Format.",
    ),
    CApiSymbol("PyErr_NoMemory", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyErr_SetFromErrno", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol(
        "PyErr_SetFromErrnoWithFilenameObject",
        "pyerrors.h",
        CApiPriority.RUNTIME_CORE,
        True,
    ),
    CApiSymbol("PyErr_NewException", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyErr_BadInternalCall", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyErr_WarnEx", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyErr_WarnFormat", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyErr_WriteUnraisable", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyErr_Print", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyErr_CheckSignals", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyErr_Occurred", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyErr_Clear", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol(
        "PyErr_GivenExceptionMatches", "pyerrors.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol("PyErr_ExceptionMatches", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyErr_Fetch", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyErr_Restore", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol(
        "PyObject_Call",
        "abstract.h",
        CApiPriority.RUNTIME_CORE,
        True,
        "Dispatches pcc-native callables through py_obj_call.",
    ),
    CApiSymbol("PyObject_CallObject", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyObject_CallNoArgs", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyObject_CallOneArg", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyObject_Vectorcall", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol(
        "PyObject_VectorcallMethod",
        "abstract.h",
        CApiPriority.RUNTIME_CORE,
        True,
    ),
    CApiSymbol("PyObject_CallFunction", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyObject_CallMethod", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol(
        "PyObject_CallMethodNoArgs",
        "abstract.h",
        CApiPriority.RUNTIME_CORE,
        True,
    ),
    CApiSymbol(
        "PyObject_CallMethodOneArg",
        "abstract.h",
        CApiPriority.RUNTIME_CORE,
        True,
    ),
    CApiSymbol(
        "PyObject_CallFunctionObjArgs",
        "abstract.h",
        CApiPriority.RUNTIME_CORE,
        True,
    ),
    CApiSymbol("Py_IsInitialized", "pylifecycle.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyGILState_Ensure", "pystate.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyGILState_Release", "pystate.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyGILState_Check", "pystate.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyObject_GetAttrString", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyObject_GetAttr", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyObject_GetOptionalAttr", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol(
        "PyObject_GetOptionalAttrString",
        "object.h",
        CApiPriority.RUNTIME_CORE,
        True,
    ),
    CApiSymbol("PyObject_SetAttrString", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyObject_SetAttr", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyObject_HasAttr", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyObject_HasAttrString", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol(
        "PyObject_HasAttrWithError", "object.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol(
        "PyObject_HasAttrStringWithError",
        "object.h",
        CApiPriority.RUNTIME_CORE,
        True,
    ),
    CApiSymbol("PyObject_IsTrue", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyObject_Not", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyObject_Hash", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyCallable_Check", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyObject_Str", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyObject_Repr", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyObject_Bytes", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyObject_Format", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("Py_PRINT_RAW", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyObject_Print", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyObject_Type", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyObject_IsInstance", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyObject_RichCompare", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyObject_RichCompareBool", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyObject_GetItem", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyObject_SetItem", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyObject_DelItem", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyObject_Size", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyObject_Length", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyObject_LengthHint", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyObject_SelfIter", "object.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyObject_GetIter", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyIter_Next", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyIter_NextItem", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyIter_Check", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyNumber_Add", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyNumber_Subtract", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyNumber_Multiply", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyNumber_TrueDivide", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyNumber_FloorDivide", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyNumber_Remainder", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyNumber_Power", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyNumber_Negative", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyNumber_Positive", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyNumber_Absolute", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyNumber_Check", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyNumber_Long", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyNumber_Float", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyNumber_And", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyNumber_Or", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyNumber_Xor", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyNumber_Invert", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyNumber_Lshift", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyNumber_Rshift", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyNumber_Index", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyNumber_AsSsize_t", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyIndex_Check", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyTuple_New", "tupleobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyTuple_SetItem", "tupleobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyTuple_GetItem", "tupleobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyTuple_Size", "tupleobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyTuple_GET_ITEM", "tupleobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyTuple_GET_SIZE", "tupleobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyTuple_SET_ITEM", "tupleobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyTuple_Pack", "tupleobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyTuple_Check", "tupleobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyTuple_CheckExact", "tupleobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyList_New", "listobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyList_SetItem", "listobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyList_GetItem", "listobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyList_GetItemRef", "listobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyList_Size", "listobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyList_GET_ITEM", "listobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyList_GET_SIZE", "listobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyList_SET_ITEM", "listobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyList_Append", "listobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyList_AsTuple", "listobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyList_Check", "listobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyList_CheckExact", "listobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyDict_New", "dictobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyDict_SetItem", "dictobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyDict_SetItemString", "dictobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyDict_GetItem", "dictobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyDict_GetItemString", "dictobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol(
        "PyDict_GetItemWithError", "dictobject.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol("PyDict_GetItemRef", "dictobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol(
        "PyDict_GetItemStringRef", "dictobject.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol("PyDict_SetDefaultRef", "dictobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyDict_Pop", "dictobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyDict_PopString", "dictobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyDict_DelItem", "dictobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyDict_DelItemString", "dictobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyDict_Size", "dictobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyDict_Contains", "dictobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol(
        "PyDict_ContainsString", "dictobject.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol("PyDict_Next", "dictobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyDict_Keys", "dictobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyDict_Values", "dictobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyDict_Items", "dictobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyDict_Check", "dictobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyDict_CheckExact", "dictobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PySet_New", "setobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PySet_Add", "setobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PySet_Contains", "setobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PySet_Discard", "setobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PySet_Size", "setobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PySet_GET_SIZE", "setobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PySet_Check", "setobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PySet_CheckExact", "setobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyAnySet_Check", "setobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyAnySet_CheckExact", "setobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyBytes_FromString", "bytesobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol(
        "PyBytes_FromStringAndSize", "bytesobject.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol("PyBytes_AsString", "bytesobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol(
        "PyBytes_AsStringAndSize", "bytesobject.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol("PyBytes_AS_STRING", "bytesobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyBytes_Size", "bytesobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyBytes_GET_SIZE", "bytesobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyBytes_Check", "bytesobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyBytes_CheckExact", "bytesobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyExc_BaseException", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyExc_Exception", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyExc_ValueError", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyExc_TypeError", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyExc_RuntimeError", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyExc_KeyError", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyExc_IndexError", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyExc_AttributeError", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyExc_MemoryError", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyExc_OverflowError", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyExc_SystemError", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyExc_NameError", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol(
        "PyExc_NotImplementedError", "pyerrors.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol("PyExc_ArithmeticError", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyExc_LookupError", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyExc_OSError", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyExc_IOError", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyExc_AssertionError", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyExc_StopIteration", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol(
        "PyExc_StopAsyncIteration", "pyerrors.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol(
        "PyExc_ZeroDivisionError", "pyerrors.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol("PyExc_ReferenceError", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyExc_BufferError", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyExc_ImportError", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol(
        "PyExc_ModuleNotFoundError", "pyerrors.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol("PyExc_ImportWarning", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol(
        "PyExc_FloatingPointError", "pyerrors.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol("PyExc_RecursionError", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol(
        "PyExc_UnicodeDecodeError", "pyerrors.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol("PyExc_Warning", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyExc_UserWarning", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyExc_RuntimeWarning", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol(
        "PyExc_DeprecationWarning", "pyerrors.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol("PyExc_FutureWarning", "pyerrors.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol(
        "PyModule_AddIntConstant", "moduleobject.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol(
        "PyModule_AddStringConstant", "moduleobject.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol("PyModule_GetDict", "moduleobject.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyImport_ImportModule", "import.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PySequence_Check", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyMapping_Check", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyMapping_Size", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyMapping_Length", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol(
        "PyMapping_GetItemString", "abstract.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol(
        "PyMapping_SetItemString", "abstract.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol("PyMapping_HasKey", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyMapping_HasKeyString", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol(
        "PyMapping_GetOptionalItem", "abstract.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol(
        "PyMapping_GetOptionalItemString",
        "abstract.h",
        CApiPriority.RUNTIME_CORE,
        True,
    ),
    CApiSymbol(
        "PyMapping_HasKeyWithError", "abstract.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol(
        "PyMapping_HasKeyStringWithError",
        "abstract.h",
        CApiPriority.RUNTIME_CORE,
        True,
    ),
    CApiSymbol("PyMapping_Keys", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyMapping_Values", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyMapping_Items", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PySequence_Size", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PySequence_Length", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PySequence_GetItem", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PySequence_SetItem", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PySequence_Contains", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PySequence_Concat", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PySequence_Repeat", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol(
        "PySequence_InPlaceConcat", "abstract.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol(
        "PySequence_InPlaceRepeat", "abstract.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol("PySequence_Fast", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol(
        "PySequence_Fast_GET_SIZE", "abstract.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol("PySequence_Fast_ITEMS", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol(
        "PySequence_Fast_GET_ITEM", "abstract.h", CApiPriority.RUNTIME_CORE, True
    ),
    CApiSymbol("PySequence_List", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PySequence_Tuple", "abstract.h", CApiPriority.RUNTIME_CORE, True),
    CApiSymbol("PyCapsule_New", "pycapsule.h", CApiPriority.ARRAY_CORE, True),
    CApiSymbol("PyCapsule_GetPointer", "pycapsule.h", CApiPriority.ARRAY_CORE, True),
    CApiSymbol("PyCapsule_GetName", "pycapsule.h", CApiPriority.ARRAY_CORE, True),
    CApiSymbol("PyCapsule_GetContext", "pycapsule.h", CApiPriority.ARRAY_CORE, True),
    CApiSymbol("PyCapsule_IsValid", "pycapsule.h", CApiPriority.ARRAY_CORE, True),
    CApiSymbol("PyCapsule_CheckExact", "pycapsule.h", CApiPriority.ARRAY_CORE, True),
    CApiSymbol("PyCapsule_SetContext", "pycapsule.h", CApiPriority.ARRAY_CORE, True),
    CApiSymbol("PyCapsule_SetName", "pycapsule.h", CApiPriority.ARRAY_CORE, True),
    CApiSymbol("PyCapsule_SetPointer", "pycapsule.h", CApiPriority.ARRAY_CORE, True),
    CApiSymbol(
        "PyCapsule_GetDestructor", "pycapsule.h", CApiPriority.ARRAY_CORE, True
    ),
    CApiSymbol(
        "PyCapsule_SetDestructor", "pycapsule.h", CApiPriority.ARRAY_CORE, True
    ),
    CApiSymbol("PyCapsule_Import", "pycapsule.h", CApiPriority.ARRAY_CORE, True),
    CApiSymbol(
        "PyMemoryView_FromObject",
        "memoryobject.h",
        CApiPriority.ARRAY_CORE,
        True,
        "Creates pcc memoryview objects for exporters handled by PyObject_GetBuffer.",
    ),
    CApiSymbol(
        "PyMemoryView_FromMemory",
        "memoryobject.h",
        CApiPriority.ARRAY_CORE,
        True,
        "Creates copy-backed pcc memoryview objects from raw extension memory.",
    ),
    CApiSymbol(
        "PyMemoryView_Check",
        "memoryobject.h",
        CApiPriority.ARRAY_CORE,
        True,
        "Checks pcc memoryview objects without exposing CPython object layout.",
    ),
    CApiSymbol(
        "PyMemoryView_GET_BUFFER",
        "memoryobject.h",
        CApiPriority.ARRAY_CORE,
        True,
        "Macro over a pcc helper returning buffer metadata for pcc memoryviews.",
    ),
    CApiSymbol(
        "PyMemoryView_GET_BASE",
        "memoryobject.h",
        CApiPriority.ARRAY_CORE,
        True,
        "Macro over a pcc helper returning the borrowed base object.",
    ),
    CApiSymbol(
        "PyObject_GetBuffer",
        "abstract.h",
        CApiPriority.ARRAY_CORE,
        True,
        "SIMPLE/1D contiguous buffers for pcc bytes, bytearray, and memoryview.",
    ),
    CApiSymbol(
        "PyObject_CheckBuffer",
        "abstract.h",
        CApiPriority.ARRAY_CORE,
        True,
        "Checks whether pcc-native bytes, bytearray, or memoryview export a buffer.",
    ),
    CApiSymbol(
        "PyBuffer_Release",
        "abstract.h",
        CApiPriority.ARRAY_CORE,
        True,
        "Releases pcc-native buffer views acquired by PyObject_GetBuffer.",
    ),
    CApiSymbol(
        "PyArray_API",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "pcc fake NumPy capsule/API table import; slots still describe the supported subset.",
    ),
    CApiSymbol("PyArray_malloc", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_free", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_realloc", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyDimMem_NEW", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyDimMem_FREE", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyDimMem_RENEW", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol(
        "PyArray_Type",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "pcc fake NumPy ndarray type-object token; provider-created arrays use the reduced in-repo dynamic type representation.",
    ),
    CApiSymbol(
        "PyArrayDescr_Type",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "pcc fake NumPy dtype type-object token; descriptor instances are the reduced in-repo descriptor structs.",
    ),
    CApiSymbol(
        "PyArray_DescrCheck",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_DescrFromType",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_TypeObjectFromType",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced scalar type-object lookup for the pcc fake NumPy built-in dtype subset.",
    ),
    CApiSymbol(
        "PyArray_DescrNewFromType",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_DescrNew",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_DescrNewByteorder",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_CanCastSafely",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_CanCastTo",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_CanCastTypeTo",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_CanCastArrayTo",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_CastingConverter",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced casting-policy string/bytes to NPY_CASTING converter for pcc fake NumPy metadata.",
    ),
    CApiSymbol(
        "PyArray_Zero",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_One",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_ObjectType",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_DescrFromObject",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_Size",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_DescrFromScalar",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_DescrFromTypeObject",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_Scalar",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced raw C item pointer plus descriptor to Python scalar helper for pcc fake NumPy metadata.",
    ),
    CApiSymbol(
        "PyArray_ScalarAsCtype",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_FromScalar",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced scalar-to-0d-array helper for the pcc fake NumPy built-in dtype subset.",
    ),
    CApiSymbol(
        "PyArray_CastScalarToCtype",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced scalar-to-typed-C-buffer helper for the pcc fake NumPy built-in dtype subset.",
    ),
    CApiSymbol(
        "PyArray_CastScalarDirect",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced direct scalar-to-output-type buffer helper for the pcc fake NumPy built-in dtype subset.",
    ),
    CApiSymbol(
        "PyArray_Pack",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced scalar/0d-array-to-item-buffer helper for the pcc fake NumPy built-in dtype subset.",
    ),
    CApiSymbol(
        "PyArray_CastToType",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced always-copy array-to-target-descriptor cast helper for the pcc fake NumPy built-in dtype subset.",
    ),
    CApiSymbol(
        "PyArray_Cast",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "header macro over the reduced pcc fake NumPy PyArray_CastToType helper.",
    ),
    CApiSymbol(
        "PyArray_FillWithScalar",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced element-wise scalar fill helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_ToList",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced ndarray-to-nested-Python-list helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_ToString",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced C-order ndarray-to-bytes helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_Byteswap",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced C-order item byteswap helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_FromString",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced binary raw-string-to-1d-array helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_FromBuffer",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced buffer-view-to-1d-array helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_FromIter",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced iterator-to-1d-array helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_Converter",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced generic object-to-array converter for NumPy C-API users.",
    ),
    CApiSymbol(
        "PyArray_IterNew",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced C-order iterator helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_BroadcastToShape",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced broadcast-shape iterator helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_Broadcast",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced broadcast recompute helper for pcc fake multi-iterators.",
    ),
    CApiSymbol(
        "PyArray_Concatenate",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced C-order same-dtype array concatenation helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_Arange",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced one-dimensional range array constructor for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_ArangeObj",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced object-scalar range array constructor for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_LexSort",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced 1D real-numeric lexicographic argsort helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_InnerProduct",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced 1D real-numeric inner-product helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_MatrixProduct",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced 1D/2D real-numeric matrix-product helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_MatrixProduct2",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced 1D/2D real-numeric matrix-product helper with out rejection for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_CountNonzero",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced C-order real-numeric nonzero-count helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_MinScalarType",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced descriptor/minimum-scalar-type helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_CreateSortedStridePerm",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced stride-sort permutation helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_RemoveAxesInPlace",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced in-place shape/stride axis-removal helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_DebugPrint",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced debug printer for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_EinsteinSum",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced Einstein-sum helper for pcc fake NumPy dot and matrix-product forms.",
    ),
    CApiSymbol(
        "PyArray_Partition",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced 1D real-numeric partition helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_ArgPartition",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced 1D real-numeric argpartition helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_CheckAnyScalarExact",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced exact NumPy scalar checker boundary for pcc fake NumPy metadata.",
    ),
    CApiSymbol(
        "PyArray_Correlate",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced 1D real-numeric correlate helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_Correlate2",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced 1D real-numeric correlate2 helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_RemoveSmallest",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced stride-sum axis removal helper for pcc fake multi-iterators.",
    ),
    CApiSymbol(
        "PyArray_IterAllButAxis",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced all-but-one-axis iterator helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_PyIntAsInt",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced Python integer to C int converter for NumPy C-API users.",
    ),
    CApiSymbol(
        "PyArray_PyIntAsIntp",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced Python integer to npy_intp converter for NumPy C-API users.",
    ),
    CApiSymbol(
        "PyArray_PythonPyIntFromInt",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced Python integer argument-parser converter for NumPy C-API users.",
    ),
    CApiSymbol(
        "PyArray_IntpFromSequence",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced scalar-or-sequence integer to npy_intp array converter for NumPy C-API users.",
    ),
    CApiSymbol(
        "PyArray_IntpConverter",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced shape object to PyArray_Dims converter for NumPy C-API users.",
    ),
    CApiSymbol(
        "PyArray_BufferConverter",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced object-to-buffer-chunk converter over Python buffer objects.",
    ),
    CApiSymbol(
        "PyArray_OptionalIntpConverter",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced optional shape object to PyArray_Dims converter for NumPy C-API users.",
    ),
    CApiSymbol(
        "PyArray_Free",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced AsCArray cleanup/free helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_AsCArray",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced 1D-3D C pointer adapter for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_FailUnlessWriteable",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced writeable-flag guard for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_CheckStrides",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced stride bounds checker for NumPy C-API users.",
    ),
    CApiSymbol(
        "PyArray_GetPriority",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced array-priority lookup helper for NumPy C-API users.",
    ),
    CApiSymbol(
        "PyArray_ITER_RESET",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "header macro for reduced pcc fake NumPy legacy iterators.",
    ),
    CApiSymbol(
        "PyArray_ITER_NEXT",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "header macro for reduced pcc fake NumPy legacy iterators.",
    ),
    CApiSymbol(
        "PyArray_ITER_DATA",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "header macro for reduced pcc fake NumPy legacy iterators.",
    ),
    CApiSymbol(
        "PyArray_ITER_NOTDONE",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "header macro for reduced pcc fake NumPy legacy iterators.",
    ),
    CApiSymbol(
        "PyArray_CopyObject",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced object-to-existing-array assignment helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_Resize",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced in-place C-order resize helper for owned pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_NewLikeArray",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced C-order empty-like allocation helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_View",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced same-dtype C-order view helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_Squeeze",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced C-order squeeze view helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_Transpose",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced C-order transpose view helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_Ravel",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced C-order flattening view helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_Flatten",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced C-order flattening copy helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_TakeFrom",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced 1D signed-index take helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_PutTo",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced 1D signed-index put helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_PutMask",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced 1D boolean-mask put helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_Repeat",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced 1D repeat helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_Choose",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced 1D choose helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_Sort",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced 1D in-place numeric sort helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_ArgSort",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced 1D numeric argsort helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_SearchSorted",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced 1D numeric searchsorted helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_Nonzero",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced 1D numeric nonzero helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_Where",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced 1D numeric where helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_Compress",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced 1D numeric compress helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_Diagonal",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced 2D numeric diagonal-copy helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_Trace",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced 2D numeric diagonal-sum helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_Clip",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced 1D numeric scalar-bound clip helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_Conjugate",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced real-numeric conjugate helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_Std",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced 1D real-numeric standard-deviation helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_Round",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced 1D real-numeric round helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_EquivTypenums",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced built-in dtype-number equivalence helper for pcc fake NumPy descriptors.",
    ),
    CApiSymbol(
        "PyArray_ScalarKind",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced built-in dtype scalar-kind classifier for pcc fake NumPy metadata.",
    ),
    CApiSymbol(
        "PyArray_CanCoerceScalar",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced built-in scalar-kind coercion query for pcc fake NumPy metadata.",
    ),
    CApiSymbol(
        "PyArray_CanCastScalar",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced Python scalar type-object safe-cast query for pcc fake NumPy metadata.",
    ),
    CApiSymbol(
        "PyArray_PromoteTypes",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced built-in dtype promotion helper for pcc fake NumPy descriptors.",
    ),
    CApiSymbol(
        "PyArray_ResultType",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced result-type helper over pcc fake arrays and built-in descriptors.",
    ),
    CApiSymbol(
        "PyArray_ConvertToCommonType",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced sequence-to-common-dtype converter returning a PyDataMem-owned PyArrayObject** for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_IntTupleFromIntp",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced dimension-list to Python tuple helper for pcc fake NumPy metadata.",
    ),
    CApiSymbol(
        "PyArray_ClipmodeConverter",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced clipmode object-to-enum converter for pcc fake NumPy metadata.",
    ),
    CApiSymbol(
        "PyArray_ConvertClipmodeSequence",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced scalar-or-sequence clipmode converter for pcc fake NumPy metadata.",
    ),
    CApiSymbol(
        "PyArray_OutputConverter",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced output argument converter for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_SearchsideConverter",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced searchsorted side object-to-enum converter for pcc fake NumPy metadata.",
    ),
    CApiSymbol(
        "PyArray_OrderConverter",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced array-order object-to-enum converter for pcc fake NumPy metadata.",
    ),
    CApiSymbol(
        "PyArray_BoolConverter",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced truth-value object-to-npy_bool converter for pcc fake NumPy metadata.",
    ),
    CApiSymbol(
        "PyArray_OptionalBoolConverter",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced optional truth-value object-to-int converter for pcc fake NumPy metadata.",
    ),
    CApiSymbol(
        "PyArray_AxisConverter",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced axis object-to-int converter for pcc fake NumPy metadata.",
    ),
    CApiSymbol(
        "PyArray_GetNDArrayCVersion",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced ABI-version query for pcc fake NumPy provider users.",
    ),
    CApiSymbol(
        "PyArray_ByteorderConverter",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced byteorder object-to-char converter for pcc fake NumPy metadata.",
    ),
    CApiSymbol(
        "PyArray_SortkindConverter",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced sort-kind object-to-enum converter for pcc fake NumPy metadata.",
    ),
    CApiSymbol(
        "PyArray_SelectkindConverter",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced select-kind object-to-enum converter for pcc fake NumPy metadata.",
    ),
    CApiSymbol(
        "PyArray_OverflowMultiplyList",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced dimension-list multiplication helper with overflow sentinel for pcc fake NumPy metadata.",
    ),
    CApiSymbol(
        "PyArray_GetEndianness",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced runtime CPU-endianness query helper for pcc fake NumPy metadata.",
    ),
    CApiSymbol(
        "PyArray_GetNDArrayCFeatureVersion",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced fake-provider NumPy C-API feature-version bookkeeping helper.",
    ),
    CApiSymbol(
        "PyArray_CheckAxis",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced axis-normalization helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_DescrAlignConverter",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced aligned descriptor converter for pcc fake NumPy descriptors.",
    ),
    CApiSymbol(
        "PyArray_DescrAlignConverter2",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced aligned descriptor converter with None-to-NULL handling for pcc fake NumPy descriptors.",
    ),
    CApiSymbol(
        "PyArray_DescrConverter",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced descriptor converter for pcc fake NumPy descriptors.",
    ),
    CApiSymbol(
        "PyArray_DescrConverter2",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced descriptor converter with None-to-NULL handling for pcc fake NumPy descriptors.",
    ),
    CApiSymbol(
        "PyArray_Sum",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced 1D real-numeric sum helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_CumSum",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced 1D real-numeric cumulative-sum helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_Prod",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced 1D real-numeric product helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_CumProd",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced 1D real-numeric cumulative-product helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_Max",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced 1D real-numeric maximum helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_Min",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced 1D real-numeric minimum helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_Ptp",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced 1D real-numeric peak-to-peak helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_Mean",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced 1D real-numeric mean helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_Any",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced 1D real-numeric truth-any helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_All",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced 1D real-numeric truth-all helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_ArgMax",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced 1D real-numeric argmax helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_ArgMin",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced 1D real-numeric argmin helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_Reshape",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced C-order same-size reshape view helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_Newshape",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced C-order same-size newshape view helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_SwapAxes",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced C-order swap-axes view helper for pcc fake NumPy arrays.",
    ),
    CApiSymbol(
        "PyArray_CheckFromAny",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_FromArray",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_MultiplyList",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_MultiplyIntList",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_GetPtr",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_ElementStrides",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_ValidType",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_Item_INCREF",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_Item_XDECREF",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_NewCopy",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_INCREF",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_XDECREF",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol("PyArray_FromAny", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol(
        "PyArray_SimpleNew", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True
    ),
    CApiSymbol(
        "PyArray_SimpleNewFromData",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol("PyArray_NDIM", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_DIMS", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol(
        "PyArray_STRIDES", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True
    ),
    CApiSymbol("PyArray_DATA", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_DESCR", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_DTYPE", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_TYPE", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyDataType_TYPE", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyDataType_KIND", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyDataType_ELSIZE", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol(
        "PyDataType_ALIGNMENT",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol("PyTypeNum_ISBOOL", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyTypeNum_ISUNSIGNED", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyTypeNum_ISSIGNED", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyTypeNum_ISINTEGER", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyTypeNum_ISFLOAT", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyTypeNum_ISNUMBER", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyTypeNum_ISSTRING", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyTypeNum_ISCOMPLEX", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyTypeNum_ISFLEXIBLE", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyTypeNum_ISOBJECT", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyDataType_ISBOOL", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyDataType_ISUNSIGNED", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyDataType_ISSIGNED", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyDataType_ISINTEGER", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyDataType_ISFLOAT", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyDataType_ISNUMBER", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyDataType_ISSTRING", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyDataType_ISCOMPLEX", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyDataType_ISFLEXIBLE", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyDataType_ISOBJECT", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_GETITEM", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_SETITEM", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_SIZE", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol(
        "PyArray_ITEMSIZE",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol("PyArray_NBYTES", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_FILLWBYTE", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol(
        "PyArray_EquivByteorders",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol("PyArray_SHAPE", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_FLAGS", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol(
        "PyArray_CompareLists",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol("PyArray_Empty", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_Zeros", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_EMPTY", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_ZEROS", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol(
        "PyArray_EquivTypes",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_EquivArrTypes",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_NewFromDescr",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_New",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced generic C-order fake ndarray constructor over built-in dtype numbers.",
    ),
    CApiSymbol(
        "PyArray_MultiIterNew",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced broadcast multi-iterator over fake ndarrays and sequence coercions.",
    ),
    CApiSymbol(
        "PyArray_MultiIterFromObjects",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "reduced pointer-vector plus varargs broadcast multi-iterator over fake ndarrays and sequence coercions.",
    ),
    CApiSymbol(
        "PyArray_SimpleNewFromDescr",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol("PyArray_BASE", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol(
        "PyArray_SetBaseObject",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_SetUpdateIfCopyBase",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_SetWritebackIfCopyBase",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_ResolveWritebackIfCopy",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_DiscardWritebackIfCopy",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol("PyDataMem_NEW", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyDataMem_FREE", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyDataMem_RENEW", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol(
        "PyDataMem_NEW_ZEROED",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyDataMem_GetHandler",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol("PyDataMem_UserNEW", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyDataMem_UserFREE", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol(
        "PyDataMem_UserRENEW",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyDataMem_UserNEW_ZEROED",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol("PyArray_Return", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol(
        "PyArray_ENABLEFLAGS",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_CLEARFLAGS",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_UpdateFlags",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_CopyInto",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_CopyAnyInto",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_ToScalar",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol("PyArray_Copy", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol(
        "PyArray_EnsureArray",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_EnsureAnyArray",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_SAMESHAPE",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol("PyArray_CHKFLAGS", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_FROM_O", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_FROM_OF", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_FROM_OT", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_FROM_OTF", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_FROMANY", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol(
        "PyArray_ContiguousFromAny",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_FromObject",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_ContiguousFromObject",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_CopyFromObject",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_ISCONTIGUOUS",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_IS_C_CONTIGUOUS",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol("PyArray_ISALIGNED", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_ISWRITEABLE", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_ISCARRAY", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol(
        "PyArray_IS_F_CONTIGUOUS",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_ISONESEGMENT",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol("PyArray_ISFORTRAN", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_FORTRAN_IF", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_ISNBO", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol(
        "PyArray_IsNativeByteOrder",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_ISNOTSWAPPED",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyArray_ISBYTESWAPPED",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol("PyArray_FLAGSWAP", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_ISCARRAY_RO", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_ISFARRAY", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_ISFARRAY_RO", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_ISBEHAVED", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_ISBEHAVED_RO", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol(
        "PyDataType_ISNOTSWAPPED",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol(
        "PyDataType_ISBYTESWAPPED",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol("PyArray_ISVARIABLE", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol(
        "PyArray_SAFEALIGNEDCOPY",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol("PyArray_ISBOOL", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_ISUNSIGNED", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_ISSIGNED", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_ISINTEGER", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_ISFLOAT", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_ISNUMBER", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_ISSTRING", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_ISCOMPLEX", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_ISFLEXIBLE", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_ISOBJECT", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_Check", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol(
        "PyArray_CheckExact",
        "numpy/arrayobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
    CApiSymbol("PyArray_DIM", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_BYTES", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_STRIDE", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_GETPTR1", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_GETPTR2", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_GETPTR3", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol("PyArray_GETPTR4", "numpy/arrayobject.h", CApiPriority.NUMPY_CAPI, True),
    CApiSymbol(
        "PyUFunc_API",
        "numpy/ufuncobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
        "pcc fake NumPy ufunc capsule/API table import; slots still describe the supported subset.",
    ),
    CApiSymbol(
        "PyUFunc_FromFuncAndData",
        "numpy/ufuncobject.h",
        CApiPriority.NUMPY_CAPI,
        True,
    ),
)


_NUMPY_CAPI_REQUIREMENT = (
    "PyArray_API",
    "PyArray_malloc",
    "PyArray_free",
    "PyArray_realloc",
    "PyDimMem_NEW",
    "PyDimMem_FREE",
    "PyDimMem_RENEW",
    "PyArray_Type",
    "PyArrayDescr_Type",
    "PyArray_DescrCheck",
    "PyArray_DescrFromType",
    "PyArray_TypeObjectFromType",
    "PyArray_DescrNewFromType",
    "PyArray_DescrNew",
    "PyArray_DescrNewByteorder",
    "PyArray_CanCastSafely",
    "PyArray_CanCastTo",
    "PyArray_CanCastTypeTo",
    "PyArray_CanCastArrayTo",
    "PyArray_CastingConverter",
    "PyArray_Zero",
    "PyArray_One",
    "PyArray_ObjectType",
    "PyArray_DescrFromObject",
    "PyArray_Size",
    "PyArray_DescrFromScalar",
    "PyArray_DescrFromTypeObject",
    "PyArray_Scalar",
    "PyArray_ScalarAsCtype",
    "PyArray_FromScalar",
    "PyArray_CastScalarToCtype",
    "PyArray_CastScalarDirect",
    "PyArray_Pack",
    "PyArray_CastToType",
    "PyArray_Cast",
    "PyArray_FillWithScalar",
    "PyArray_ToList",
    "PyArray_ToString",
    "PyArray_Byteswap",
    "PyArray_FromString",
    "PyArray_FromBuffer",
    "PyArray_FromIter",
    "PyArray_Converter",
    "PyArray_IterNew",
    "PyArray_BroadcastToShape",
    "PyArray_Broadcast",
    "PyArray_Concatenate",
    "PyArray_Arange",
    "PyArray_ArangeObj",
    "PyArray_LexSort",
    "PyArray_InnerProduct",
    "PyArray_MatrixProduct",
    "PyArray_MatrixProduct2",
    "PyArray_CountNonzero",
    "PyArray_MinScalarType",
    "PyArray_CreateSortedStridePerm",
    "PyArray_RemoveAxesInPlace",
    "PyArray_DebugPrint",
    "PyArray_EinsteinSum",
    "PyArray_Partition",
    "PyArray_ArgPartition",
    "PyArray_CheckAnyScalarExact",
    "PyArray_Correlate",
    "PyArray_Correlate2",
    "PyArray_RemoveSmallest",
    "PyArray_IterAllButAxis",
    "PyArray_PyIntAsInt",
    "PyArray_PyIntAsIntp",
    "PyArray_PythonPyIntFromInt",
    "PyArray_IntpFromSequence",
    "PyArray_IntpConverter",
    "PyArray_BufferConverter",
    "PyArray_OptionalIntpConverter",
    "PyArray_Free",
    "PyArray_AsCArray",
    "PyArray_FailUnlessWriteable",
    "PyArray_CheckStrides",
    "PyArray_GetPriority",
    "PyArray_ITER_RESET",
    "PyArray_ITER_NEXT",
    "PyArray_ITER_DATA",
    "PyArray_ITER_NOTDONE",
    "PyArray_CopyObject",
    "PyArray_Resize",
    "PyArray_NewLikeArray",
    "PyArray_View",
    "PyArray_Squeeze",
    "PyArray_Transpose",
    "PyArray_Ravel",
    "PyArray_Flatten",
    "PyArray_TakeFrom",
    "PyArray_PutTo",
    "PyArray_PutMask",
    "PyArray_Repeat",
    "PyArray_Choose",
    "PyArray_Sort",
    "PyArray_ArgSort",
    "PyArray_SearchSorted",
    "PyArray_Nonzero",
    "PyArray_Where",
    "PyArray_Compress",
    "PyArray_Diagonal",
    "PyArray_Trace",
    "PyArray_Clip",
    "PyArray_Conjugate",
    "PyArray_Std",
    "PyArray_Round",
    "PyArray_EquivTypenums",
    "PyArray_ScalarKind",
    "PyArray_CanCoerceScalar",
    "PyArray_CanCastScalar",
    "PyArray_PromoteTypes",
    "PyArray_ResultType",
    "PyArray_ConvertToCommonType",
    "PyArray_IntTupleFromIntp",
    "PyArray_ClipmodeConverter",
    "PyArray_ConvertClipmodeSequence",
    "PyArray_OutputConverter",
    "PyArray_SearchsideConverter",
    "PyArray_OrderConverter",
    "PyArray_BoolConverter",
    "PyArray_OptionalBoolConverter",
    "PyArray_AxisConverter",
    "PyArray_GetNDArrayCVersion",
    "PyArray_ByteorderConverter",
    "PyArray_SortkindConverter",
    "PyArray_SelectkindConverter",
    "PyArray_OverflowMultiplyList",
    "PyArray_GetEndianness",
    "PyArray_GetNDArrayCFeatureVersion",
    "PyArray_CheckAxis",
    "PyArray_DescrAlignConverter",
    "PyArray_DescrAlignConverter2",
    "PyArray_DescrConverter",
    "PyArray_DescrConverter2",
    "PyArray_Sum",
    "PyArray_CumSum",
    "PyArray_Prod",
    "PyArray_CumProd",
    "PyArray_Max",
    "PyArray_Min",
    "PyArray_Ptp",
    "PyArray_Mean",
    "PyArray_Any",
    "PyArray_All",
    "PyArray_ArgMax",
    "PyArray_ArgMin",
    "PyArray_Reshape",
    "PyArray_Newshape",
    "PyArray_SwapAxes",
    "PyArray_CheckFromAny",
    "PyArray_FromArray",
    "PyArray_MultiplyList",
    "PyArray_MultiplyIntList",
    "PyArray_GetPtr",
    "PyArray_ElementStrides",
    "PyArray_ValidType",
    "PyArray_Item_INCREF",
    "PyArray_Item_XDECREF",
    "PyArray_NewCopy",
    "PyArray_INCREF",
    "PyArray_XDECREF",
    "PyArray_FromAny",
    "PyArray_SimpleNew",
    "PyArray_SimpleNewFromData",
    "PyArray_NDIM",
    "PyArray_DIMS",
    "PyArray_STRIDES",
    "PyArray_DATA",
    "PyArray_DESCR",
    "PyArray_DTYPE",
    "PyArray_TYPE",
    "PyDataType_TYPE",
    "PyDataType_KIND",
    "PyDataType_ELSIZE",
    "PyDataType_ALIGNMENT",
    "PyTypeNum_ISBOOL",
    "PyTypeNum_ISUNSIGNED",
    "PyTypeNum_ISSIGNED",
    "PyTypeNum_ISINTEGER",
    "PyTypeNum_ISFLOAT",
    "PyTypeNum_ISNUMBER",
    "PyTypeNum_ISSTRING",
    "PyTypeNum_ISCOMPLEX",
    "PyTypeNum_ISFLEXIBLE",
    "PyTypeNum_ISOBJECT",
    "PyDataType_ISBOOL",
    "PyDataType_ISUNSIGNED",
    "PyDataType_ISSIGNED",
    "PyDataType_ISINTEGER",
    "PyDataType_ISFLOAT",
    "PyDataType_ISNUMBER",
    "PyDataType_ISSTRING",
    "PyDataType_ISCOMPLEX",
    "PyDataType_ISFLEXIBLE",
    "PyDataType_ISOBJECT",
    "PyArray_GETITEM",
    "PyArray_SETITEM",
    "PyArray_SIZE",
    "PyArray_ITEMSIZE",
    "PyArray_NBYTES",
    "PyArray_FILLWBYTE",
    "PyArray_EquivByteorders",
    "PyArray_SHAPE",
    "PyArray_FLAGS",
    "PyArray_CompareLists",
    "PyArray_Empty",
    "PyArray_Zeros",
    "PyArray_EMPTY",
    "PyArray_ZEROS",
    "PyArray_EquivTypes",
    "PyArray_EquivArrTypes",
    "PyArray_NewFromDescr",
    "PyArray_New",
    "PyArray_MultiIterNew",
    "PyArray_MultiIterFromObjects",
    "PyArray_SimpleNewFromDescr",
    "PyArray_BASE",
    "PyArray_SetBaseObject",
    "PyArray_SetUpdateIfCopyBase",
    "PyArray_SetWritebackIfCopyBase",
    "PyArray_ResolveWritebackIfCopy",
    "PyArray_DiscardWritebackIfCopy",
    "PyDataMem_NEW",
    "PyDataMem_FREE",
    "PyDataMem_RENEW",
    "PyDataMem_NEW_ZEROED",
    "PyDataMem_GetHandler",
    "PyDataMem_UserNEW",
    "PyDataMem_UserFREE",
    "PyDataMem_UserRENEW",
    "PyDataMem_UserNEW_ZEROED",
    "PyArray_Return",
    "PyArray_ENABLEFLAGS",
    "PyArray_CLEARFLAGS",
    "PyArray_UpdateFlags",
    "PyArray_CopyInto",
    "PyArray_CopyAnyInto",
    "PyArray_ToScalar",
    "PyArray_Copy",
    "PyArray_EnsureArray",
    "PyArray_EnsureAnyArray",
    "PyArray_SAMESHAPE",
    "PyArray_CHKFLAGS",
    "PyArray_FROM_O",
    "PyArray_FROM_OF",
    "PyArray_FROM_OT",
    "PyArray_FROM_OTF",
    "PyArray_FROMANY",
    "PyArray_ContiguousFromAny",
    "PyArray_FromObject",
    "PyArray_ContiguousFromObject",
    "PyArray_CopyFromObject",
    "PyArray_ISCONTIGUOUS",
    "PyArray_IS_C_CONTIGUOUS",
    "PyArray_ISALIGNED",
    "PyArray_ISWRITEABLE",
    "PyArray_ISCARRAY",
    "PyArray_IS_F_CONTIGUOUS",
    "PyArray_ISONESEGMENT",
    "PyArray_ISFORTRAN",
    "PyArray_FORTRAN_IF",
    "PyArray_ISNBO",
    "PyArray_IsNativeByteOrder",
    "PyArray_ISNOTSWAPPED",
    "PyArray_ISBYTESWAPPED",
    "PyArray_FLAGSWAP",
    "PyArray_ISCARRAY_RO",
    "PyArray_ISFARRAY",
    "PyArray_ISFARRAY_RO",
    "PyArray_ISBEHAVED",
    "PyArray_ISBEHAVED_RO",
    "PyDataType_ISNOTSWAPPED",
    "PyDataType_ISBYTESWAPPED",
    "PyArray_ISVARIABLE",
    "PyArray_SAFEALIGNEDCOPY",
    "PyArray_ISBOOL",
    "PyArray_ISUNSIGNED",
    "PyArray_ISSIGNED",
    "PyArray_ISINTEGER",
    "PyArray_ISFLOAT",
    "PyArray_ISNUMBER",
    "PyArray_ISSTRING",
    "PyArray_ISCOMPLEX",
    "PyArray_ISFLEXIBLE",
    "PyArray_ISOBJECT",
    "PyArray_Check",
    "PyArray_CheckExact",
    "PyArray_DIM",
    "PyArray_BYTES",
    "PyArray_STRIDE",
    "PyArray_GETPTR1",
    "PyArray_GETPTR2",
    "PyArray_GETPTR3",
    "PyArray_GETPTR4",
    "PyUFunc_API",
    "PyUFunc_FromFuncAndData",
)

_NUMPY_CAPI_TABLE_SLOTS: dict[str, dict[str, object]] = {
    "PyArray_API": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": None,
        "failure_mode": "implemented_provider_table",
    },
    "PyArray_Type": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 0,
        "failure_mode": "implemented_provider_type_object",
    },
    "PyArrayDescr_Type": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 1,
        "failure_mode": "implemented_provider_type_object",
    },
    "PyArray_DescrFromType": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 2,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_FromAny": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 3,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_SimpleNew": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 4,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_SimpleNewFromData": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 5,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_NDIM": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 6,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_DIMS": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 7,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_STRIDES": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 8,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_DATA": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 9,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_DESCR": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 10,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_DTYPE": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": None,
        "failure_mode": "implemented_header_macro",
    },
    "PyArray_TYPE": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": None,
        "failure_mode": "implemented_header_macro",
    },
    "PyDataType_TYPE": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": None,
        "failure_mode": "implemented_header_macro",
    },
    "PyDataType_KIND": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": None,
        "failure_mode": "implemented_header_macro",
    },
    "PyDataType_ELSIZE": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": None,
        "failure_mode": "implemented_header_macro",
    },
    "PyDataType_ALIGNMENT": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": None,
        "failure_mode": "implemented_header_macro",
    },
    "PyArray_GETITEM": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 11,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_SETITEM": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 12,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_SIZE": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 13,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_ITEMSIZE": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 14,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_NBYTES": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": None,
        "failure_mode": "implemented_header_macro",
    },
    "PyArray_FLAGS": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 17,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_CompareLists": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 18,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Empty": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 19,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Zeros": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 20,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_EquivTypes": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 21,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_NewFromDescr": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 22,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_New": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 172,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_MultiIterNew": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 173,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_MultiIterFromObjects": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 177,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_BASE": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 23,
        "failure_mode": "implemented_header_macro",
    },
    "PyArray_SetBaseObject": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 24,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_SetUpdateIfCopyBase": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 152,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_SetWritebackIfCopyBase": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 153,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_ResolveWritebackIfCopy": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 154,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_DiscardWritebackIfCopy": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 155,
        "failure_mode": "implemented_provider_slot",
    },
    "PyDataMem_NEW": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 156,
        "failure_mode": "implemented_provider_slot",
    },
    "PyDataMem_FREE": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 157,
        "failure_mode": "implemented_provider_slot",
    },
    "PyDataMem_RENEW": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 158,
        "failure_mode": "implemented_provider_slot",
    },
    "PyDataMem_NEW_ZEROED": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 159,
        "failure_mode": "implemented_provider_slot",
    },
    "PyDataMem_GetHandler": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 160,
        "failure_mode": "implemented_provider_slot",
    },
    "PyDataMem_UserNEW": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 161,
        "failure_mode": "implemented_provider_slot",
    },
    "PyDataMem_UserFREE": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 162,
        "failure_mode": "implemented_provider_slot",
    },
    "PyDataMem_UserRENEW": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 163,
        "failure_mode": "implemented_provider_slot",
    },
    "PyDataMem_UserNEW_ZEROED": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 164,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Return": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 25,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_ENABLEFLAGS": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 26,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_CLEARFLAGS": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 27,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_UpdateFlags": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 28,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_CopyInto": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 29,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_CopyAnyInto": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 30,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_ToScalar": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 31,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Copy": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 32,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_EnsureArray": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 33,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_EnsureAnyArray": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 34,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_DescrCheck": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": None,
        "failure_mode": "implemented_header_macro",
    },
    "PyArray_DescrNewFromType": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 35,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_DescrNew": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 36,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_DescrNewByteorder": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 37,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_CanCastSafely": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 38,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_ObjectType": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 39,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_CheckFromAny": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 40,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_FromArray": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 41,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_MultiplyList": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 42,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_MultiplyIntList": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 43,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_GetPtr": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 44,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_ElementStrides": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 45,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_ValidType": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 46,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Item_INCREF": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 47,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Item_XDECREF": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 48,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_NewCopy": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 49,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_INCREF": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 50,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_XDECREF": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 51,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_CanCastTo": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 52,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_CanCastTypeTo": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 165,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_CanCastArrayTo": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 166,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_CastingConverter": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 168,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Zero": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 53,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_One": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 54,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_TypeObjectFromType": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 55,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_DescrFromObject": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 56,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Size": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 57,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_DescrFromScalar": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 58,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_DescrFromTypeObject": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 59,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Scalar": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 169,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_ScalarAsCtype": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 60,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_FromScalar": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 61,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_CastScalarToCtype": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 62,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Pack": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 63,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_CastScalarDirect": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 64,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_CastToType": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 65,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Cast": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": None,
        "failure_mode": "implemented_header_macro",
    },
    "PyArray_FillWithScalar": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 66,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_ToList": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 67,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_ToString": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 68,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Byteswap": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 69,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_FromString": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 70,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_FromBuffer": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 71,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_FromIter": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 72,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Converter": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 144,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_IterNew": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 127,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_BroadcastToShape": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 128,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Broadcast": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 176,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Concatenate": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 180,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Arange": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 181,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_ArangeObj": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 182,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_LexSort": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 183,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_InnerProduct": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 184,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_MatrixProduct": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 185,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_MatrixProduct2": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 188,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_CountNonzero": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 189,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_MinScalarType": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 190,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_CreateSortedStridePerm": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 191,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_RemoveAxesInPlace": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 192,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_DebugPrint": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 193,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_EinsteinSum": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 194,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Partition": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 195,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_ArgPartition": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 196,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_CheckAnyScalarExact": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 197,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Correlate": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 186,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Correlate2": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 187,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_RemoveSmallest": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 178,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_IterAllButAxis": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 129,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_PyIntAsInt": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 130,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_PyIntAsIntp": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 131,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_PythonPyIntFromInt": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 167,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_IntpFromSequence": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 143,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_IntpConverter": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 145,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_BufferConverter": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 179,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_OptionalIntpConverter": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 146,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Free": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 147,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_AsCArray": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 148,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_FailUnlessWriteable": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 149,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_CheckStrides": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 132,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_GetPriority": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 133,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_ITER_RESET": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": None,
        "failure_mode": "implemented_header_macro",
    },
    "PyArray_ITER_NEXT": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": None,
        "failure_mode": "implemented_header_macro",
    },
    "PyArray_ITER_DATA": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": None,
        "failure_mode": "implemented_header_macro",
    },
    "PyArray_ITER_NOTDONE": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": None,
        "failure_mode": "implemented_header_macro",
    },
    "PyArray_CopyObject": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 73,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Resize": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 74,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_NewLikeArray": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 75,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_View": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 76,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Squeeze": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 77,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Transpose": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 78,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Ravel": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 79,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Flatten": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 80,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_TakeFrom": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 81,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_PutTo": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 82,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_PutMask": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 83,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Repeat": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 84,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Choose": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 85,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Sort": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 86,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_ArgSort": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 87,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_SearchSorted": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 88,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Nonzero": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 89,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Where": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 90,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Compress": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 91,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Diagonal": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 92,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Trace": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 93,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Clip": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 94,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Conjugate": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 95,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Sum": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 96,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_CumSum": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 109,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Prod": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 97,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_CumProd": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 110,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Std": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 111,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Round": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 112,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_EquivTypenums": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 113,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_ScalarKind": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 170,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_CanCoerceScalar": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 114,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_CanCastScalar": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 116,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_PromoteTypes": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 174,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_ResultType": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 175,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_ConvertToCommonType": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 171,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_IntTupleFromIntp": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 117,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_ClipmodeConverter": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 118,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_ConvertClipmodeSequence": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 141,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_OutputConverter": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 119,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_SearchsideConverter": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 120,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_OrderConverter": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 134,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_BoolConverter": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 135,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_OptionalBoolConverter": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 142,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_AxisConverter": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 136,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_GetNDArrayCVersion": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 137,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_ByteorderConverter": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 138,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_SortkindConverter": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 139,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_SelectkindConverter": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 140,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_OverflowMultiplyList": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 121,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_GetEndianness": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 122,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_GetNDArrayCFeatureVersion": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 123,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_CheckAxis": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 124,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_DescrAlignConverter": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 125,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_DescrAlignConverter2": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 126,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_DescrConverter": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 150,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_DescrConverter2": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 151,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Max": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 98,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Min": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 99,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Ptp": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 105,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Mean": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 106,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Any": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 107,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_All": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 108,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_ArgMax": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 100,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_ArgMin": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 101,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Reshape": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 102,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_Newshape": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 103,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_SwapAxes": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 104,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_CHKFLAGS": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": None,
        "failure_mode": "implemented_header_macro",
    },
    "PyArray_ISCONTIGUOUS": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": None,
        "failure_mode": "implemented_header_macro",
    },
    "PyArray_IS_C_CONTIGUOUS": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": None,
        "failure_mode": "implemented_header_macro",
    },
    "PyArray_ISALIGNED": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": None,
        "failure_mode": "implemented_header_macro",
    },
    "PyArray_ISWRITEABLE": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": None,
        "failure_mode": "implemented_header_macro",
    },
    "PyArray_ISCARRAY": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": None,
        "failure_mode": "implemented_header_macro",
    },
    "PyArray_Check": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 15,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_CheckExact": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 16,
        "failure_mode": "implemented_provider_slot",
    },
    "PyArray_DIM": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 7,
        "failure_mode": "implemented_header_macro",
    },
    "PyArray_BYTES": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": 9,
        "failure_mode": "implemented_header_macro",
    },
    "PyArray_STRIDE": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": None,
        "failure_mode": "implemented_header_macro",
    },
    "PyArray_GETPTR1": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": None,
        "failure_mode": "implemented_header_macro",
    },
    "PyArray_GETPTR2": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": None,
        "failure_mode": "implemented_header_macro",
    },
    "PyArray_GETPTR3": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": None,
        "failure_mode": "implemented_header_macro",
    },
    "PyArray_GETPTR4": {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": None,
        "failure_mode": "implemented_header_macro",
    },
    "PyUFunc_API": {
        "provider_shape": "ufunc_api",
        "table": "_UFUNC_API",
        "slot": None,
        "failure_mode": "implemented_provider_table",
    },
    "PyUFunc_FromFuncAndData": {
        "provider_shape": "ufunc_api",
        "table": "_UFUNC_API",
        "slot": 0,
        "failure_mode": "implemented_provider_slot",
    },
}

_NUMPY_CAPI_CLASSIFICATION_MACROS = (
    "PyTypeNum_ISBOOL",
    "PyTypeNum_ISUNSIGNED",
    "PyTypeNum_ISSIGNED",
    "PyTypeNum_ISINTEGER",
    "PyTypeNum_ISFLOAT",
    "PyTypeNum_ISNUMBER",
    "PyTypeNum_ISSTRING",
    "PyTypeNum_ISCOMPLEX",
    "PyTypeNum_ISFLEXIBLE",
    "PyTypeNum_ISOBJECT",
    "PyDataType_ISBOOL",
    "PyDataType_ISUNSIGNED",
    "PyDataType_ISSIGNED",
    "PyDataType_ISINTEGER",
    "PyDataType_ISFLOAT",
    "PyDataType_ISNUMBER",
    "PyDataType_ISSTRING",
    "PyDataType_ISCOMPLEX",
    "PyDataType_ISFLEXIBLE",
    "PyDataType_ISOBJECT",
    "PyArray_ISBOOL",
    "PyArray_ISUNSIGNED",
    "PyArray_ISSIGNED",
    "PyArray_ISINTEGER",
    "PyArray_ISFLOAT",
    "PyArray_ISNUMBER",
    "PyArray_ISSTRING",
    "PyArray_ISCOMPLEX",
    "PyArray_ISFLEXIBLE",
    "PyArray_ISOBJECT",
)

_NUMPY_CAPI_ALLOCATOR_MACROS = (
    "PyArray_malloc",
    "PyArray_free",
    "PyArray_realloc",
    "PyDimMem_NEW",
    "PyDimMem_FREE",
    "PyDimMem_RENEW",
)

for _allocator_macro in _NUMPY_CAPI_ALLOCATOR_MACROS:
    _NUMPY_CAPI_TABLE_SLOTS[_allocator_macro] = {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": None,
        "failure_mode": "implemented_header_macro",
    }

for _classification_macro in _NUMPY_CAPI_CLASSIFICATION_MACROS:
    _NUMPY_CAPI_TABLE_SLOTS[_classification_macro] = {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": None,
        "failure_mode": "implemented_header_macro",
    }

_NUMPY_CAPI_STATE_BYTEORDER_MACROS = (
    "PyArray_IS_F_CONTIGUOUS",
    "PyArray_ISONESEGMENT",
    "PyArray_ISFORTRAN",
    "PyArray_FORTRAN_IF",
    "PyArray_ISNBO",
    "PyArray_IsNativeByteOrder",
    "PyArray_ISNOTSWAPPED",
    "PyArray_ISBYTESWAPPED",
    "PyArray_FLAGSWAP",
    "PyArray_ISCARRAY_RO",
    "PyArray_ISFARRAY",
    "PyArray_ISFARRAY_RO",
    "PyArray_ISBEHAVED",
    "PyArray_ISBEHAVED_RO",
    "PyDataType_ISNOTSWAPPED",
    "PyDataType_ISBYTESWAPPED",
    "PyArray_ISVARIABLE",
    "PyArray_SAFEALIGNEDCOPY",
)

for _state_byteorder_macro in _NUMPY_CAPI_STATE_BYTEORDER_MACROS:
    _NUMPY_CAPI_TABLE_SLOTS[_state_byteorder_macro] = {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": None,
        "failure_mode": "implemented_header_macro",
    }

_NUMPY_CAPI_FROMANY_MACROS = (
    "PyArray_FROM_O",
    "PyArray_FROM_OF",
    "PyArray_FROM_OT",
    "PyArray_FROM_OTF",
    "PyArray_FROMANY",
    "PyArray_ContiguousFromAny",
    "PyArray_FromObject",
    "PyArray_ContiguousFromObject",
    "PyArray_CopyFromObject",
)

for _fromany_macro in _NUMPY_CAPI_FROMANY_MACROS:
    _NUMPY_CAPI_TABLE_SLOTS[_fromany_macro] = {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": None,
        "failure_mode": "implemented_header_macro",
    }

_NUMPY_CAPI_FILL_BYTEORDER_MACROS = (
    "PyArray_FILLWBYTE",
    "PyArray_EquivByteorders",
)

for _fill_byteorder_macro in _NUMPY_CAPI_FILL_BYTEORDER_MACROS:
    _NUMPY_CAPI_TABLE_SLOTS[_fill_byteorder_macro] = {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": None,
        "failure_mode": "implemented_header_macro",
    }

_NUMPY_CAPI_SHAPE_COMPARE_MACROS = (
    "PyArray_SHAPE",
    "PyArray_SAMESHAPE",
)

for _shape_compare_macro in _NUMPY_CAPI_SHAPE_COMPARE_MACROS:
    _NUMPY_CAPI_TABLE_SLOTS[_shape_compare_macro] = {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": None,
        "failure_mode": "implemented_header_macro",
    }

_NUMPY_CAPI_ALLOC_MACROS = (
    "PyArray_EMPTY",
    "PyArray_ZEROS",
)

for _alloc_macro in _NUMPY_CAPI_ALLOC_MACROS:
    _NUMPY_CAPI_TABLE_SLOTS[_alloc_macro] = {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": None,
        "failure_mode": "implemented_header_macro",
    }

_NUMPY_CAPI_EQUIV_TYPE_MACROS = (
    "PyArray_EquivArrTypes",
)

for _equiv_type_macro in _NUMPY_CAPI_EQUIV_TYPE_MACROS:
    _NUMPY_CAPI_TABLE_SLOTS[_equiv_type_macro] = {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": None,
        "failure_mode": "implemented_header_macro",
    }

_NUMPY_CAPI_DESCR_ALLOC_MACROS = (
    "PyArray_SimpleNewFromDescr",
)

for _descr_alloc_macro in _NUMPY_CAPI_DESCR_ALLOC_MACROS:
    _NUMPY_CAPI_TABLE_SLOTS[_descr_alloc_macro] = {
        "provider_shape": "array_api",
        "table": "_ARRAY_API",
        "slot": None,
        "failure_mode": "implemented_header_macro",
    }


def _numpy_capi_family(name: str) -> str | None:
    if name == "PyUFunc_API" or name.startswith("PyUFunc_"):
        return "ufunc_api"
    if (
        name == "PyArray_API"
        or name == "PyArray_Type"
        or name == "PyArrayDescr_Type"
        or name.startswith("PyArray_")
        or name.startswith("PyDimMem_")
        or name.startswith("PyDataMem_")
        or name.startswith("PyDataType_")
        or name.startswith("PyTypeNum_")
    ):
        return "array_api"
    return None


def _numpy_capi_symbol_status(name: str) -> dict[str, object] | None:
    meta = _NUMPY_CAPI_TABLE_SLOTS.get(name)
    if meta is None:
        family = _numpy_capi_family(name)
        if family is None:
            return None
        meta = {
            "provider_shape": family,
            "table": "_UFUNC_API" if family == "ufunc_api" else "_ARRAY_API",
            "slot": None,
            "failure_mode": "unknown_numpy_capi_symbol",
        }
    catalogue = symbols_by_name()
    sym = catalogue.get(name)
    return {
        "symbol": name,
        "capability": "numpy_capi",
        "provider_shape": meta["provider_shape"],
        "table": meta["table"],
        "slot": meta["slot"],
        "implemented": bool(sym.implemented) if sym is not None else False,
        "failure_mode": meta["failure_mode"],
    }


def default_capi_symbols() -> tuple[CApiSymbol, ...]:
    return _DEFAULT_SYMBOLS


def missing_symbols(
    priority_at_most: CApiPriority = CApiPriority.ARRAY_CORE,
) -> list[CApiSymbol]:
    return [
        sym
        for sym in _DEFAULT_SYMBOLS
        if not sym.implemented and sym.priority <= priority_at_most
    ]


def symbols_by_name() -> dict[str, CApiSymbol]:
    return {sym.name: sym for sym in _DEFAULT_SYMBOLS}


def capi_header_manifest(
    required_symbols: tuple[str, ...] | list[str] | None = None,
    *,
    include_dir: str | None = None,
) -> dict[str, object]:
    catalogue = symbols_by_name()
    requested = (
        list(required_symbols)
        if required_symbols is not None
        else [sym.name for sym in _DEFAULT_SYMBOLS]
    )
    headers: list[str] = []
    provided_headers: list[str] = []
    missing_headers: list[str] = []
    symbol_rows: list[dict[str, object]] = []
    unknown: list[str] = []
    include_root = Path(include_dir).expanduser().resolve() if include_dir else None
    for name in requested:
        sym = catalogue.get(name)
        if sym is None:
            unknown.append(name)
            continue
        if sym.header not in headers:
            headers.append(sym.header)
            if include_root is not None:
                if (include_root / sym.header).exists():
                    provided_headers.append(sym.header)
                else:
                    missing_headers.append(sym.header)
        header_path = (
            str(include_root / sym.header) if include_root is not None else None
        )
        provided = bool(
            include_root is not None
            and header_path is not None
            and Path(header_path).exists()
        )
        symbol_rows.append(
            {
                "name": sym.name,
                "header": sym.header,
                "header_path": header_path,
                "provided_by_package": provided,
                "implemented": sym.implemented,
                "priority": int(sym.priority),
            }
        )
    return {
        "include_dir": include_dir,
        "headers": headers,
        "provided_headers": provided_headers,
        "missing_headers": missing_headers,
        "symbols": symbol_rows,
        "unknown_symbols": unknown,
    }


def extension_abi_plan(
    required_symbols: tuple[str, ...] | list[str] | None = None,
    *,
    provider: str = "extension",
    expected_abi: int | None = None,
    actual_abi: int | None = None,
    abi_mode: str = "pcc-native",
    include_dir: str | None = None,
    require_capsule: bool = False,
    require_buffer: bool = False,
    require_memoryview: bool = False,
    require_numpy_capi: bool = False,
) -> dict[str, object]:
    requested = list(required_symbols or [])
    if require_capsule:
        requested.extend(
            [
                "PyCapsule_New",
                "PyCapsule_GetPointer",
                "PyCapsule_GetName",
                "PyCapsule_GetContext",
                "PyCapsule_IsValid",
                "PyCapsule_CheckExact",
                "PyCapsule_SetContext",
                "PyCapsule_SetName",
                "PyCapsule_SetPointer",
                "PyCapsule_GetDestructor",
                "PyCapsule_SetDestructor",
                "PyCapsule_Import",
            ]
        )
    if require_buffer:
        requested.extend(
            ["PyObject_GetBuffer", "PyObject_CheckBuffer", "PyBuffer_Release"]
        )
    if require_memoryview:
        requested.extend(
            [
                "PyMemoryView_FromObject",
                "PyMemoryView_FromMemory",
                "PyMemoryView_Check",
                "PyMemoryView_GET_BUFFER",
                "PyMemoryView_GET_BASE",
            ]
        )
    if require_numpy_capi:
        requested.extend(_NUMPY_CAPI_REQUIREMENT)
    requested = list(dict.fromkeys(requested))

    manifest = capi_header_manifest(requested, include_dir=include_dir)
    catalogue = symbols_by_name()
    numpy_capi_status = [
        status
        for name in requested
        if (status := _numpy_capi_symbol_status(name)) is not None
    ]
    missing = [
        name
        for name in requested
        if name in catalogue and not catalogue[name].implemented
    ]
    diagnostics: list[dict[str, object]] = []
    for name in missing:
        sym = catalogue[name]
        numpy_family = (
            _numpy_capi_family(name)
            if sym.priority == CApiPriority.NUMPY_CAPI
            else None
        )
        if numpy_family is not None:
            status = _numpy_capi_symbol_status(name) or {}
            diagnostics.append(
                {
                    "code": "PCC-EXT-MISSING-NUMPY-CAPI-SYMBOL",
                    "symbol": name,
                    "capability": "numpy_capi",
                    "provider_shape": numpy_family,
                    "table": status.get("table"),
                    "slot": status.get("slot"),
                    "failure_mode": status.get("failure_mode"),
                    "message": (
                        f"{name} requires a pcc-native NumPy {numpy_family} "
                        f"provider; it is not implemented for {abi_mode}"
                    ),
                }
            )
            continue
        diagnostics.append(
            {
                "code": "PCC-EXT-MISSING-CAPI-SYMBOL",
                "symbol": name,
                "message": f"{name} is not implemented for {abi_mode}",
            }
        )
    for name in manifest["unknown_symbols"]:  # type: ignore[index]
        diagnostics.append(
            {
                "code": "PCC-EXT-UNKNOWN-CAPI-SYMBOL",
                "symbol": name,
                "message": f"{name} is not in the pcc C-API catalogue",
            }
        )
    if include_dir is not None:
        for header in manifest["missing_headers"]:  # type: ignore[index]
            diagnostics.append(
                {
                    "code": "PCC-EXT-MISSING-CAPI-HEADER",
                    "header": header,
                    "include_dir": include_dir,
                    "message": f"{header} was not provided by {include_dir}",
                }
            )
    abi_diag: dict[str, object] | None = None
    if expected_abi is not None and actual_abi is not None:
        abi_diag = abi_version_diagnostic(
            provider=provider,
            expected=expected_abi,
            actual=actual_abi,
            abi_mode=abi_mode,
        )
        if not abi_diag["ok"]:
            diagnostics.append(abi_diag)

    return {
        "ok": not diagnostics,
        "provider": provider,
        "abi_mode": abi_mode,
        "required_symbols": requested,
        "header_manifest": manifest,
        "missing_symbols": missing,
        "unknown_symbols": manifest["unknown_symbols"],
        "numpy_capi_status": numpy_capi_status,
        "abi_version": abi_diag,
        "diagnostics": diagnostics,
    }


def symbol_report() -> dict:
    missing_by_priority: dict[str, list[str]] = {}
    implemented_by_priority: dict[str, list[str]] = {}
    for priority in CApiPriority:
        missing_by_priority[priority.name.lower()] = [
            s.name
            for s in _DEFAULT_SYMBOLS
            if s.priority == priority and not s.implemented
        ]
        implemented_by_priority[priority.name.lower()] = [
            s.name for s in _DEFAULT_SYMBOLS if s.priority == priority and s.implemented
        ]
    return {
        "symbols": [
            {
                "name": s.name,
                "header": s.header,
                "priority": int(s.priority),
                "implemented": s.implemented,
                "notes": s.notes,
            }
            for s in _DEFAULT_SYMBOLS
        ],
        "missing_import_blockers": [
            s.name for s in missing_symbols(CApiPriority.IMPORT_BLOCKER)
        ],
        "missing_runtime_core": [
            s.name
            for s in _DEFAULT_SYMBOLS
            if s.priority == CApiPriority.RUNTIME_CORE and not s.implemented
        ],
        "missing_array_core": [
            s.name for s in missing_symbols(CApiPriority.ARRAY_CORE)
        ],
        "missing_numpy_capi": [
            s.name
            for s in _DEFAULT_SYMBOLS
            if s.priority == CApiPriority.NUMPY_CAPI and not s.implemented
        ],
        "numpy_capi_status": [
            status
            for s in _DEFAULT_SYMBOLS
            if s.priority == CApiPriority.NUMPY_CAPI
            if (status := _numpy_capi_symbol_status(s.name)) is not None
        ],
        "missing_downstream_extension": [
            s.name
            for s in _DEFAULT_SYMBOLS
            if s.priority == CApiPriority.DOWNSTREAM_EXTENSION and not s.implemented
        ],
        "missing_by_priority": missing_by_priority,
        "implemented_by_priority": implemented_by_priority,
    }


def abi_version_diagnostic(
    *,
    provider: str,
    expected: int,
    actual: int,
    abi_mode: str = "pcc-native",
) -> dict[str, object]:
    ok = expected == actual
    return {
        "ok": ok,
        "code": None if ok else "PCC-EXT-ABI-VERSION-MISMATCH",
        "provider": provider,
        "expected": expected,
        "actual": actual,
        "abi_mode": abi_mode,
        "message": (
            "ABI versions match"
            if ok
            else (
                f"{provider} ABI version mismatch: expected {expected}, "
                f"got {actual} under {abi_mode}"
            )
        ),
    }
