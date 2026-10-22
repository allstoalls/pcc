from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from pcc1_gate import repo_root

import pytest

from pcc1_gate import find_current_pcc1, skip_or_fail_no_current_pcc1

from pcc.frontends.c.capi_surface import capi_header_manifest, extension_abi_plan

REPO = repo_root()

GENERIC_REFCNT_MACRO_SYMBOLS = [
    "Py_REFCNT",
    "Py_SET_REFCNT",
]

GENERIC_MEMORY_OS_SYMBOLS = [
    "PyMem_RawMalloc",
    "PyMem_RawCalloc",
    "PyMem_RawRealloc",
    "PyMem_RawFree",
    "PyMem_FREE",
    "PyObject_Malloc",
    "PyObject_Calloc",
    "PyObject_Realloc",
    "PyObject_Free",
    "PyObject_MALLOC",
    "PyObject_REALLOC",
    "PyObject_FREE",
    "PyObject_Del",
    "PyObject_DEL",
    "PyOS_snprintf",
    "PyOS_vsnprintf",
]

GENERIC_MAPPING_LONG_EXCEPTION_SYMBOLS = [
    "PyLong_FromSsize_t",
    "PyLong_FromSize_t",
    "PyLong_FromInt32",
    "PyLong_FromInt64",
    "PyLong_FromUInt32",
    "PyLong_FromUInt64",
    "PyLong_FromVoidPtr",
    "PyLong_AsInt",
    "PyLong_FromDouble",
    "PyLong_AsInt32",
    "PyLong_AsInt64",
    "PyLong_AsUInt32",
    "PyLong_AsUInt64",
    "PyLong_AsVoidPtr",
    "PyLong_AsLongAndOverflow",
    "PyLong_AsUnsignedLong",
    "PyLong_AsUnsignedLongLong",
    "PyLong_AsUnsignedLongLongMask",
    "PyLong_AsSsize_t",
    "PyLong_AsSize_t",
    "PyLong_AsDouble",
    "PyLong_Check",
    "PyLong_CheckExact",
    "PyObject_GetItem",
    "PyObject_SetItem",
    "PyObject_DelItem",
    "PyDict_GetItemWithError",
    "PyDict_GetItemRef",
    "PyDict_GetItemStringRef",
    "PyDict_SetDefaultRef",
    "PyDict_Pop",
    "PyDict_PopString",
    "PyDict_Size",
    "PyDict_Contains",
    "PyDict_ContainsString",
    "PyDict_Next",
    "PyDict_Keys",
    "PyDict_Values",
    "PyDict_Items",
    "PySet_New",
    "PySet_Add",
    "PySet_Contains",
    "PySet_Discard",
    "PySet_Size",
    "PySet_GET_SIZE",
    "PySet_Check",
    "PySet_CheckExact",
    "PyAnySet_Check",
    "PyAnySet_CheckExact",
    "PyObject_LengthHint",
    "PyObject_Size",
    "PyObject_Length",
    "PyMapping_Check",
    "PyMapping_Size",
    "PyMapping_Length",
    "PyMapping_GetItemString",
    "PyMapping_SetItemString",
    "PyMapping_HasKey",
    "PyMapping_HasKeyString",
    "PyMapping_GetOptionalItem",
    "PyMapping_GetOptionalItemString",
    "PyMapping_HasKeyWithError",
    "PyMapping_HasKeyStringWithError",
    "PyMapping_Keys",
    "PyMapping_Values",
    "PyMapping_Items",
    "PyErr_SetNone",
    "PyErr_SetObject",
    "PyErr_FormatV",
    "PyErr_BadInternalCall",
    "PyErr_SetFromErrno",
    "PyErr_SetFromErrnoWithFilenameObject",
    "PyErr_GivenExceptionMatches",
    "PyErr_ExceptionMatches",
    "PyErr_Fetch",
    "PyErr_Restore",
    "PyExc_BaseException",
    "PyExc_Exception",
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
    "PyErr_WarnEx",
    "PyErr_WarnFormat",
    "PyErr_WriteUnraisable",
    "PyErr_Print",
    "PyErr_CheckSignals",
    "PyExc_Warning",
    "PyExc_UserWarning",
    "PyExc_RuntimeWarning",
    "PyExc_DeprecationWarning",
    "PyExc_FutureWarning",
]

GENERIC_CALL_TYPE_FORMAT_SYMBOLS = [
    "PyUnicode_FromFormat",
    "PyUnicode_FromFormatV",
    "PyUnicode_FromKindAndData",
    "PyUnicode_FromOrdinal",
    "PyUnicode_AsUCS4",
    "PyUnicode_AsUCS4Copy",
    "PyUnicode_FromEncodedObject",
    "PyUnicode_GetLength",
    "PyUnicode_AsUTF8AndSize",
    "PyUnicode_AsEncodedString",
    "PyUnicode_AsUTF8String",
    "PyUnicode_AsASCIIString",
    "PyUnicode_Tailmatch",
    "PyUnicode_Find",
    "PyUnicode_ReadChar",
    "PyUnicode_FindChar",
    "PyUnicode_Count",
    "PyUnicode_Replace",
    "PyUnicode_Substring",
    "PyUnicode_Contains",
    "PyUnicode_Concat",
    "PyUnicode_EqualToUTF8",
    "PyUnicode_EqualToUTF8AndSize",
    "PyBytes_AS_STRING",
    "PyBytes_GET_SIZE",
    "PyObject_CallNoArgs",
    "PyObject_CallOneArg",
    "PyObject_Vectorcall",
    "PyObject_VectorcallMethod",
    "PyObject_CallFunction",
    "PyObject_CallMethod",
    "PyObject_CallMethodNoArgs",
    "PyObject_CallMethodOneArg",
    "PyObject_Bytes",
    "PyObject_Format",
    "Py_PRINT_RAW",
    "PyObject_Print",
    "PyObject_Type",
    "PyObject_IsInstance",
    "PyModule_Add",
]

GENERIC_UNICODE_MACRO_SYMBOLS = [
    "Py_UNUSED",
    "Py_UCS1",
    "Py_UCS2",
    "Py_UCS4",
    "PyUnicode_1BYTE_KIND",
    "PyUnicode_2BYTE_KIND",
    "PyUnicode_4BYTE_KIND",
    "PyUnicode_GET_LENGTH",
    "Py_UNICODE_ISSPACE",
    "Py_UNICODE_ISDIGIT",
    "Py_UNICODE_ISDECIMAL",
    "Py_UNICODE_ISNUMERIC",
    "Py_UNICODE_ISLOWER",
    "Py_UNICODE_ISUPPER",
    "Py_UNICODE_ISTITLE",
    "Py_UNICODE_ISALPHA",
    "Py_UNICODE_ISALNUM",
]

GENERIC_SCALAR_COMPLEX_SYMBOLS = [
    "PyBool_Check",
    "PyFloat_Check",
    "PyFloat_CheckExact",
    "PyFloat_AS_DOUBLE",
    "Py_complex",
    "PyComplex_FromDoubles",
    "PyComplex_FromCComplex",
    "PyComplex_AsCComplex",
    "PyComplex_RealAsDouble",
    "PyComplex_ImagAsDouble",
    "PyComplex_Check",
    "PyComplex_CheckExact",
]

GENERIC_GIL_STATE_SYMBOLS = [
    "Py_IsInitialized",
    "PyGILState_Ensure",
    "PyGILState_Release",
    "PyGILState_Check",
]

GENERIC_NUMBER_PROTOCOL_SYMBOLS = [
    "PyObject_Not",
    "PyNumber_Add",
    "PyNumber_Subtract",
    "PyNumber_Multiply",
    "PyNumber_TrueDivide",
    "PyNumber_FloorDivide",
    "PyNumber_Remainder",
    "PyNumber_Power",
    "PyNumber_Negative",
    "PyNumber_Positive",
    "PyNumber_Absolute",
    "PyNumber_Check",
    "PyNumber_Long",
    "PyNumber_Float",
    "PyNumber_And",
    "PyNumber_Or",
    "PyNumber_Xor",
    "PyNumber_Invert",
    "PyNumber_Lshift",
    "PyNumber_Rshift",
    "PyNumber_Index",
    "PyNumber_AsSsize_t",
    "PyIndex_Check",
]

GENERIC_ABSTRACT_PROTOCOL_SYMBOLS = [
    "PyObject_SelfIter",
    "PyObject_GetIter",
    "PyIter_Next",
    "PyIter_NextItem",
    "PyIter_Check",
    "PySequence_Contains",
    "PySequence_SetItem",
    "PySequence_Concat",
    "PySequence_Repeat",
    "PySequence_InPlaceConcat",
    "PySequence_InPlaceRepeat",
]

GENERIC_CONTAINER_MACRO_SYMBOLS = [
    "PyTuple_GET_ITEM",
    "PyTuple_GET_SIZE",
    "PyTuple_SET_ITEM",
    "PyList_GET_ITEM",
    "PyList_GET_SIZE",
    "PyList_SET_ITEM",
    "PyList_GetItemRef",
    "PyList_AsTuple",
    "PySequence_Fast_GET_ITEM",
]

GENERIC_CAPSULE_SYMBOLS = [
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

GENERIC_MEMORYVIEW_SYMBOLS = [
    "PyMemoryView_FromObject",
    "PyMemoryView_FromMemory",
    "PyMemoryView_Check",
    "PyMemoryView_GET_BUFFER",
    "PyMemoryView_GET_BASE",
]

GENERIC_SINGLETON_RETURN_SYMBOLS = [
    "Py_None",
    "Py_True",
    "Py_False",
    "Py_NotImplemented",
    "Py_Is",
    "Py_IsNone",
    "Py_IsTrue",
    "Py_IsFalse",
    "Py_RETURN_NONE",
    "Py_RETURN_TRUE",
    "Py_RETURN_FALSE",
    "Py_RETURN_NOTIMPLEMENTED",
]


def _find_current_pcc1() -> Path | None:
    return find_current_pcc1(REPO)


def test_capi_header_manifest_tracks_headers_and_unknown_symbols():
    manifest = capi_header_manifest(
        [
            "Py_Initialize",
            "Py_INCREF",
            "Py_DECREF",
            "Py_XINCREF",
            "Py_XDECREF",
            "Py_NewRef",
            "Py_XNewRef",
            "Py_CLEAR",
            "Py_SETREF",
            "Py_XSETREF",
            *GENERIC_REFCNT_MACRO_SYMBOLS,
            "PyMem_Malloc",
            "PyMem_Calloc",
            "PyMem_Realloc",
            "PyMem_Free",
            *GENERIC_MEMORY_OS_SYMBOLS,
            *GENERIC_SINGLETON_RETURN_SYMBOLS,
            *GENERIC_MAPPING_LONG_EXCEPTION_SYMBOLS,
            *GENERIC_CALL_TYPE_FORMAT_SYMBOLS,
            *GENERIC_UNICODE_MACRO_SYMBOLS,
            *GENERIC_SCALAR_COMPLEX_SYMBOLS,
            *GENERIC_GIL_STATE_SYMBOLS,
            *GENERIC_NUMBER_PROTOCOL_SYMBOLS,
            *GENERIC_ABSTRACT_PROTOCOL_SYMBOLS,
            "PyObject_Call",
            "PyObject_CallObject",
            "PyObject_CallFunctionObjArgs",
            "Py_BuildValue",
            "PyArg_ParseTupleAndKeywords",
            "PyObject_CheckBuffer",
            "PyTuple_New",
            *GENERIC_CONTAINER_MACRO_SYMBOLS,
            "PyDict_SetItemString",
            "PyObject_GetAttrString",
            *GENERIC_CAPSULE_SYMBOLS,
            "PyObject_GetBuffer",
            "Missing_Symbol",
        ],
        include_dir="/pcc/include",
    )
    assert manifest["include_dir"] == "/pcc/include"
    assert manifest["headers"] == [
        "Python.h",
        "object.h",
        "pymem.h",
        "longobject.h",
        "dictobject.h",
        "setobject.h",
        "abstract.h",
        "pyerrors.h",
        "unicodeobject.h",
        "bytesobject.h",
        "moduleobject.h",
        "boolobject.h",
        "floatobject.h",
        "complexobject.h",
        "pylifecycle.h",
        "pystate.h",
        "modsupport.h",
        "tupleobject.h",
        "listobject.h",
        "pycapsule.h",
    ]
    assert manifest["unknown_symbols"] == ["Missing_Symbol"]


def test_extension_abi_plan_require_capsule_includes_full_generic_surface():
    plan = extension_abi_plan(provider="capsule", require_capsule=True)
    assert plan["ok"] is True
    for symbol in GENERIC_CAPSULE_SYMBOLS:
        assert symbol in plan["required_symbols"]
        assert symbol not in plan["missing_symbols"]
    assert plan["unknown_symbols"] == []
    assert plan["diagnostics"] == []


def test_extension_abi_plan_reports_capsule_buffer_memoryview_and_version_gaps():
    plan = extension_abi_plan(
        [
            "Py_Initialize",
            "Py_INCREF",
            "Py_DECREF",
            "Py_XINCREF",
            "Py_XDECREF",
            "Py_NewRef",
            "Py_XNewRef",
            "Py_CLEAR",
            "Py_SETREF",
            "Py_XSETREF",
            *GENERIC_REFCNT_MACRO_SYMBOLS,
            "PyMem_Malloc",
            "PyMem_Calloc",
            "PyMem_Realloc",
            "PyMem_Free",
            *GENERIC_MEMORY_OS_SYMBOLS,
            *GENERIC_MAPPING_LONG_EXCEPTION_SYMBOLS,
            *GENERIC_CALL_TYPE_FORMAT_SYMBOLS,
            *GENERIC_UNICODE_MACRO_SYMBOLS,
            *GENERIC_SCALAR_COMPLEX_SYMBOLS,
            *GENERIC_NUMBER_PROTOCOL_SYMBOLS,
            *GENERIC_ABSTRACT_PROTOCOL_SYMBOLS,
            "PyObject_Call",
            "PyObject_CallObject",
            "PyObject_CallFunctionObjArgs",
            "Py_BuildValue",
            "PyArg_ParseTupleAndKeywords",
            "PyTuple_New",
            *GENERIC_CONTAINER_MACRO_SYMBOLS,
            "PyTuple_SetItem",
            "PyTuple_Pack",
            "PyDict_New",
            "PyDict_SetItemString",
            "PyDict_DelItem",
            "PyDict_DelItemString",
            "PyDict_Size",
            "PyDict_Contains",
            "PyDict_ContainsString",
            "PyObject_GetAttrString",
            "PyObject_GetAttr",
            "PyObject_GetOptionalAttr",
            "PyObject_GetOptionalAttrString",
            "PyObject_SetAttrString",
            "PyObject_SetAttr",
            "PyObject_HasAttr",
            "PyObject_HasAttrString",
            "PyObject_HasAttrWithError",
            "PyObject_HasAttrStringWithError",
            "PyObject_Hash",
            "PyCallable_Check",
            "PyObject_Str",
            "PyObject_Repr",
            "PyObject_RichCompare",
            "PyObject_RichCompareBool",
            "PyModule_AddObjectRef",
            "PyModule_AddIntConstant",
            "PyModule_GetDict",
            "PyList_New",
            "PyList_Append",
            "PyList_Check",
            "PyTuple_Check",
            "PyDict_Check",
            "PyBytes_Check",
            "PyLong_FromUnsignedLong",
            "PyLong_FromUnsignedLongLong",
            "PyUnicode_Check",
            "PyUnicode_InternFromString",
            "PyUnicode_CompareWithASCIIString",
            "PySequence_Fast",
            "PySequence_Fast_ITEMS",
            "PySequence_Size",
            "PySequence_GetItem",
            "PyUnicode_FromStringAndSize",
            "PyBytes_FromString",
            "PyBytes_FromStringAndSize",
            "PyBytes_AsStringAndSize",
            "PyFloat_FromDouble",
            "PyBool_FromLong",
            "PyObject_IsTrue",
            "PyErr_Format",
            "PyErr_NoMemory",
            "PyErr_NewException",
            "PyImport_ImportModule",
            "Missing_Symbol",
        ],
        provider="array-api",
        expected_abi=3,
        actual_abi=2,
        require_capsule=True,
        require_buffer=True,
        require_memoryview=True,
    )
    assert plan["ok"] is False
    assert "PyCapsule_New" not in plan["missing_symbols"]
    assert "Py_INCREF" not in plan["missing_symbols"]
    assert "Py_DECREF" not in plan["missing_symbols"]
    assert "Py_XINCREF" not in plan["missing_symbols"]
    assert "Py_XDECREF" not in plan["missing_symbols"]
    assert "Py_NewRef" not in plan["missing_symbols"]
    assert "Py_XNewRef" not in plan["missing_symbols"]
    assert "Py_CLEAR" not in plan["missing_symbols"]
    assert "Py_SETREF" not in plan["missing_symbols"]
    assert "Py_XSETREF" not in plan["missing_symbols"]
    for symbol in GENERIC_REFCNT_MACRO_SYMBOLS:
        assert symbol not in plan["missing_symbols"]
    assert "PyMem_Malloc" not in plan["missing_symbols"]
    assert "PyMem_Calloc" not in plan["missing_symbols"]
    assert "PyMem_Realloc" not in plan["missing_symbols"]
    assert "PyMem_Free" not in plan["missing_symbols"]
    for symbol in GENERIC_MEMORY_OS_SYMBOLS:
        assert symbol not in plan["missing_symbols"]
    for symbol in GENERIC_SINGLETON_RETURN_SYMBOLS:
        assert symbol not in plan["missing_symbols"]
    for symbol in GENERIC_MAPPING_LONG_EXCEPTION_SYMBOLS:
        assert symbol not in plan["missing_symbols"]
    for symbol in GENERIC_CALL_TYPE_FORMAT_SYMBOLS:
        assert symbol not in plan["missing_symbols"]
    for symbol in GENERIC_UNICODE_MACRO_SYMBOLS:
        assert symbol not in plan["missing_symbols"]
    for symbol in GENERIC_SCALAR_COMPLEX_SYMBOLS:
        assert symbol not in plan["missing_symbols"]
    for symbol in GENERIC_GIL_STATE_SYMBOLS:
        assert symbol not in plan["missing_symbols"]
    for symbol in GENERIC_NUMBER_PROTOCOL_SYMBOLS:
        assert symbol not in plan["missing_symbols"]
    for symbol in GENERIC_ABSTRACT_PROTOCOL_SYMBOLS:
        assert symbol not in plan["missing_symbols"]
    for symbol in GENERIC_CONTAINER_MACRO_SYMBOLS:
        assert symbol not in plan["missing_symbols"]
    for symbol in GENERIC_CAPSULE_SYMBOLS:
        assert symbol not in plan["missing_symbols"]
    assert "PyObject_Call" not in plan["missing_symbols"]
    assert "PyObject_CallObject" not in plan["missing_symbols"]
    assert "PyObject_CallFunctionObjArgs" not in plan["missing_symbols"]
    assert "Py_BuildValue" not in plan["missing_symbols"]
    assert "PyArg_ParseTupleAndKeywords" not in plan["missing_symbols"]
    assert "PyObject_CheckBuffer" not in plan["missing_symbols"]
    assert "PyTuple_New" not in plan["missing_symbols"]
    assert "PyTuple_SetItem" not in plan["missing_symbols"]
    assert "PyTuple_Pack" not in plan["missing_symbols"]
    assert "PyDict_New" not in plan["missing_symbols"]
    assert "PyDict_SetItemString" not in plan["missing_symbols"]
    assert "PyDict_DelItem" not in plan["missing_symbols"]
    assert "PyDict_DelItemString" not in plan["missing_symbols"]
    assert "PyDict_Size" not in plan["missing_symbols"]
    assert "PyDict_Contains" not in plan["missing_symbols"]
    assert "PyDict_ContainsString" not in plan["missing_symbols"]
    assert "PyObject_GetAttrString" not in plan["missing_symbols"]
    assert "PyObject_GetAttr" not in plan["missing_symbols"]
    assert "PyObject_GetOptionalAttr" not in plan["missing_symbols"]
    assert "PyObject_GetOptionalAttrString" not in plan["missing_symbols"]
    assert "PyObject_SetAttrString" not in plan["missing_symbols"]
    assert "PyObject_SetAttr" not in plan["missing_symbols"]
    assert "PyObject_HasAttr" not in plan["missing_symbols"]
    assert "PyObject_HasAttrString" not in plan["missing_symbols"]
    assert "PyObject_HasAttrWithError" not in plan["missing_symbols"]
    assert "PyObject_HasAttrStringWithError" not in plan["missing_symbols"]
    assert "PyObject_Hash" not in plan["missing_symbols"]
    assert "PyCallable_Check" not in plan["missing_symbols"]
    assert "PyObject_Str" not in plan["missing_symbols"]
    assert "PyObject_Repr" not in plan["missing_symbols"]
    assert "PyObject_RichCompare" not in plan["missing_symbols"]
    assert "PyObject_RichCompareBool" not in plan["missing_symbols"]
    assert "PyModule_AddObjectRef" not in plan["missing_symbols"]
    assert "PyModule_AddIntConstant" not in plan["missing_symbols"]
    assert "PyModule_GetDict" not in plan["missing_symbols"]
    assert "PyList_New" not in plan["missing_symbols"]
    assert "PyList_Append" not in plan["missing_symbols"]
    assert "PyList_Check" not in plan["missing_symbols"]
    assert "PyTuple_Check" not in plan["missing_symbols"]
    assert "PyDict_Check" not in plan["missing_symbols"]
    assert "PyBytes_Check" not in plan["missing_symbols"]
    assert "PyLong_FromUnsignedLong" not in plan["missing_symbols"]
    assert "PyLong_FromUnsignedLongLong" not in plan["missing_symbols"]
    assert "PyUnicode_Check" not in plan["missing_symbols"]
    assert "PyUnicode_InternFromString" not in plan["missing_symbols"]
    assert "PyUnicode_CompareWithASCIIString" not in plan["missing_symbols"]
    assert "PySequence_Fast" not in plan["missing_symbols"]
    assert "PySequence_Fast_ITEMS" not in plan["missing_symbols"]
    assert "PySequence_Size" not in plan["missing_symbols"]
    assert "PySequence_GetItem" not in plan["missing_symbols"]
    assert "PyUnicode_FromStringAndSize" not in plan["missing_symbols"]
    assert "PyBytes_FromString" not in plan["missing_symbols"]
    assert "PyBytes_FromStringAndSize" not in plan["missing_symbols"]
    assert "PyBytes_AsStringAndSize" not in plan["missing_symbols"]
    assert "PyFloat_FromDouble" not in plan["missing_symbols"]
    assert "PyBool_FromLong" not in plan["missing_symbols"]
    assert "PyObject_IsTrue" not in plan["missing_symbols"]
    assert "PyErr_Format" not in plan["missing_symbols"]
    assert "PyErr_NoMemory" not in plan["missing_symbols"]
    assert "PyErr_NewException" not in plan["missing_symbols"]
    assert "PyImport_ImportModule" not in plan["missing_symbols"]
    assert "PyObject_GetBuffer" not in plan["missing_symbols"]
    assert "PyObject_CheckBuffer" not in plan["missing_symbols"]
    assert "PyBuffer_Release" not in plan["missing_symbols"]
    for symbol in GENERIC_MEMORYVIEW_SYMBOLS:
        assert symbol not in plan["missing_symbols"]
    assert plan["unknown_symbols"] == ["Missing_Symbol"]
    assert {diag["code"] for diag in plan["diagnostics"]} == {
        "PCC-EXT-UNKNOWN-CAPI-SYMBOL",
        "PCC-EXT-ABI-VERSION-MISMATCH",
    }


def test_extension_abi_plan_tracks_numpy_capi_provider_subset():
    plan = extension_abi_plan(
        ["Py_Initialize"],
        provider="numpy-capi",
        require_numpy_capi=True,
    )
    assert plan["ok"] is True
    assert "PyArray_API" in plan["required_symbols"]
    for symbol in [
        "PyArray_malloc",
        "PyArray_free",
        "PyArray_realloc",
        "PyDimMem_NEW",
        "PyDimMem_FREE",
        "PyDimMem_RENEW",
    ]:
        assert symbol in plan["required_symbols"]
    assert "PyArray_DescrCheck" in plan["required_symbols"]
    assert "PyArray_DescrNewFromType" in plan["required_symbols"]
    assert "PyArray_DescrNew" in plan["required_symbols"]
    assert "PyArray_DescrNewByteorder" in plan["required_symbols"]
    assert "PyArray_CanCastSafely" in plan["required_symbols"]
    assert "PyArray_CanCastTo" in plan["required_symbols"]
    assert "PyArray_Zero" in plan["required_symbols"]
    assert "PyArray_One" in plan["required_symbols"]
    assert "PyArray_TypeObjectFromType" in plan["required_symbols"]
    assert "PyArray_ObjectType" in plan["required_symbols"]
    assert "PyArray_DescrFromObject" in plan["required_symbols"]
    assert "PyArray_Size" in plan["required_symbols"]
    assert "PyArray_DescrFromScalar" in plan["required_symbols"]
    assert "PyArray_DescrFromTypeObject" in plan["required_symbols"]
    assert "PyArray_ScalarAsCtype" in plan["required_symbols"]
    assert "PyArray_FromScalar" in plan["required_symbols"]
    assert "PyArray_CastScalarToCtype" in plan["required_symbols"]
    assert "PyArray_CastScalarDirect" in plan["required_symbols"]
    assert "PyArray_Pack" in plan["required_symbols"]
    assert "PyArray_CastToType" in plan["required_symbols"]
    assert "PyArray_Cast" in plan["required_symbols"]
    assert "PyArray_FillWithScalar" in plan["required_symbols"]
    assert "PyArray_ToList" in plan["required_symbols"]
    assert "PyArray_ToString" in plan["required_symbols"]
    assert "PyArray_Byteswap" in plan["required_symbols"]
    assert "PyArray_FromString" in plan["required_symbols"]
    assert "PyArray_FromBuffer" in plan["required_symbols"]
    assert "PyArray_CheckFromAny" in plan["required_symbols"]
    assert "PyArray_FromArray" in plan["required_symbols"]
    assert "PyArray_MultiplyList" in plan["required_symbols"]
    assert "PyArray_MultiplyIntList" in plan["required_symbols"]
    assert "PyArray_GetPtr" in plan["required_symbols"]
    assert "PyArray_ElementStrides" in plan["required_symbols"]
    assert "PyArray_ValidType" in plan["required_symbols"]
    assert "PyArray_Item_INCREF" in plan["required_symbols"]
    assert "PyArray_Item_XDECREF" in plan["required_symbols"]
    assert "PyArray_NewCopy" in plan["required_symbols"]
    assert "PyArray_INCREF" in plan["required_symbols"]
    assert "PyArray_XDECREF" in plan["required_symbols"]
    assert "PyArray_DTYPE" in plan["required_symbols"]
    assert "PyDataType_TYPE" in plan["required_symbols"]
    assert "PyTypeNum_ISFLOAT" in plan["required_symbols"]
    assert "PyDataType_ISNUMBER" in plan["required_symbols"]
    assert "PyArray_ISOBJECT" in plan["required_symbols"]
    assert "PyArray_ISONESEGMENT" in plan["required_symbols"]
    assert "PyArray_ISNBO" in plan["required_symbols"]
    assert "PyDataType_ISBYTESWAPPED" in plan["required_symbols"]
    assert "PyArray_SAFEALIGNEDCOPY" in plan["required_symbols"]
    assert "PyArray_FROMANY" in plan["required_symbols"]
    assert "PyArray_FROM_OF" in plan["required_symbols"]
    assert "PyArray_FromObject" in plan["required_symbols"]
    assert "PyArray_CopyFromObject" in plan["required_symbols"]
    assert "PyArray_TYPE" in plan["required_symbols"]
    assert "PyArray_NBYTES" in plan["required_symbols"]
    assert "PyArray_FILLWBYTE" in plan["required_symbols"]
    assert "PyArray_EquivByteorders" in plan["required_symbols"]
    assert "PyArray_SHAPE" in plan["required_symbols"]
    assert "PyArray_FLAGS" in plan["required_symbols"]
    assert "PyArray_CompareLists" in plan["required_symbols"]
    assert "PyArray_Empty" in plan["required_symbols"]
    assert "PyArray_Zeros" in plan["required_symbols"]
    assert "PyArray_EMPTY" in plan["required_symbols"]
    assert "PyArray_ZEROS" in plan["required_symbols"]
    assert "PyArray_EquivTypes" in plan["required_symbols"]
    assert "PyArray_EquivArrTypes" in plan["required_symbols"]
    assert "PyArray_NewFromDescr" in plan["required_symbols"]
    assert "PyArray_SimpleNewFromDescr" in plan["required_symbols"]
    assert "PyArray_BASE" in plan["required_symbols"]
    assert "PyArray_SetBaseObject" in plan["required_symbols"]
    assert "PyArray_Return" in plan["required_symbols"]
    assert "PyArray_ENABLEFLAGS" in plan["required_symbols"]
    assert "PyArray_CLEARFLAGS" in plan["required_symbols"]
    assert "PyArray_UpdateFlags" in plan["required_symbols"]
    assert "PyArray_CopyInto" in plan["required_symbols"]
    assert "PyArray_CopyAnyInto" in plan["required_symbols"]
    assert "PyArray_ToScalar" in plan["required_symbols"]
    assert "PyArray_Copy" in plan["required_symbols"]
    assert "PyArray_EnsureArray" in plan["required_symbols"]
    assert "PyArray_EnsureAnyArray" in plan["required_symbols"]
    assert "PyArray_SAMESHAPE" in plan["required_symbols"]
    assert "PyArray_ISCONTIGUOUS" in plan["required_symbols"]
    assert "PyArray_STRIDE" in plan["required_symbols"]
    assert "PyArray_GETPTR2" in plan["required_symbols"]
    assert "PyArray_API" not in plan["missing_symbols"]
    for symbol in [
        "PyArray_malloc",
        "PyArray_free",
        "PyArray_realloc",
        "PyDimMem_NEW",
        "PyDimMem_FREE",
        "PyDimMem_RENEW",
    ]:
        assert symbol not in plan["missing_symbols"]
    assert "PyArray_Type" not in plan["missing_symbols"]
    assert "PyArrayDescr_Type" not in plan["missing_symbols"]
    assert "PyArray_DescrCheck" not in plan["missing_symbols"]
    assert "PyArray_DescrFromType" not in plan["missing_symbols"]
    assert "PyArray_DescrNewFromType" not in plan["missing_symbols"]
    assert "PyArray_DescrNew" not in plan["missing_symbols"]
    assert "PyArray_DescrNewByteorder" not in plan["missing_symbols"]
    assert "PyArray_CanCastSafely" not in plan["missing_symbols"]
    assert "PyArray_CanCastTo" not in plan["missing_symbols"]
    assert "PyArray_Zero" not in plan["missing_symbols"]
    assert "PyArray_One" not in plan["missing_symbols"]
    assert "PyArray_TypeObjectFromType" not in plan["missing_symbols"]
    assert "PyArray_ObjectType" not in plan["missing_symbols"]
    assert "PyArray_DescrFromObject" not in plan["missing_symbols"]
    assert "PyArray_Size" not in plan["missing_symbols"]
    assert "PyArray_DescrFromScalar" not in plan["missing_symbols"]
    assert "PyArray_DescrFromTypeObject" not in plan["missing_symbols"]
    assert "PyArray_ScalarAsCtype" not in plan["missing_symbols"]
    assert "PyArray_FromScalar" not in plan["missing_symbols"]
    assert "PyArray_CastScalarToCtype" not in plan["missing_symbols"]
    assert "PyArray_CastScalarDirect" not in plan["missing_symbols"]
    assert "PyArray_Pack" not in plan["missing_symbols"]
    assert "PyArray_CastToType" not in plan["missing_symbols"]
    assert "PyArray_Cast" not in plan["missing_symbols"]
    assert "PyArray_FillWithScalar" not in plan["missing_symbols"]
    assert "PyArray_ToList" not in plan["missing_symbols"]
    assert "PyArray_ToString" not in plan["missing_symbols"]
    assert "PyArray_Byteswap" not in plan["missing_symbols"]
    assert "PyArray_FromString" not in plan["missing_symbols"]
    assert "PyArray_FromBuffer" not in plan["missing_symbols"]
    assert "PyArray_CheckFromAny" not in plan["missing_symbols"]
    assert "PyArray_FromArray" not in plan["missing_symbols"]
    assert "PyArray_MultiplyList" not in plan["missing_symbols"]
    assert "PyArray_MultiplyIntList" not in plan["missing_symbols"]
    assert "PyArray_GetPtr" not in plan["missing_symbols"]
    assert "PyArray_ElementStrides" not in plan["missing_symbols"]
    assert "PyArray_ValidType" not in plan["missing_symbols"]
    assert "PyArray_Item_INCREF" not in plan["missing_symbols"]
    assert "PyArray_Item_XDECREF" not in plan["missing_symbols"]
    assert "PyArray_NewCopy" not in plan["missing_symbols"]
    assert "PyArray_INCREF" not in plan["missing_symbols"]
    assert "PyArray_XDECREF" not in plan["missing_symbols"]
    assert "PyArray_FromAny" not in plan["missing_symbols"]
    assert "PyArray_SimpleNew" not in plan["missing_symbols"]
    assert "PyArray_SimpleNewFromData" not in plan["missing_symbols"]
    assert "PyArray_GETITEM" not in plan["missing_symbols"]
    assert "PyArray_SETITEM" not in plan["missing_symbols"]
    assert "PyArray_NDIM" not in plan["missing_symbols"]
    assert "PyArray_DIMS" not in plan["missing_symbols"]
    assert "PyArray_STRIDES" not in plan["missing_symbols"]
    assert "PyArray_DATA" not in plan["missing_symbols"]
    assert "PyArray_DESCR" not in plan["missing_symbols"]
    assert "PyArray_SIZE" not in plan["missing_symbols"]
    assert "PyArray_ITEMSIZE" not in plan["missing_symbols"]
    assert "PyArray_DIM" not in plan["missing_symbols"]
    assert "PyArray_BYTES" not in plan["missing_symbols"]
    assert "PyArray_Check" not in plan["missing_symbols"]
    assert "PyArray_CheckExact" not in plan["missing_symbols"]
    assert "PyArray_DTYPE" not in plan["missing_symbols"]
    assert "PyDataType_TYPE" not in plan["missing_symbols"]
    assert "PyTypeNum_ISFLOAT" not in plan["missing_symbols"]
    assert "PyDataType_ISNUMBER" not in plan["missing_symbols"]
    assert "PyArray_ISOBJECT" not in plan["missing_symbols"]
    assert "PyArray_ISONESEGMENT" not in plan["missing_symbols"]
    assert "PyArray_ISNBO" not in plan["missing_symbols"]
    assert "PyDataType_ISBYTESWAPPED" not in plan["missing_symbols"]
    assert "PyArray_SAFEALIGNEDCOPY" not in plan["missing_symbols"]
    assert "PyArray_FROMANY" not in plan["missing_symbols"]
    assert "PyArray_FROM_OF" not in plan["missing_symbols"]
    assert "PyArray_FromObject" not in plan["missing_symbols"]
    assert "PyArray_CopyFromObject" not in plan["missing_symbols"]
    assert "PyArray_TYPE" not in plan["missing_symbols"]
    assert "PyArray_NBYTES" not in plan["missing_symbols"]
    assert "PyArray_FILLWBYTE" not in plan["missing_symbols"]
    assert "PyArray_EquivByteorders" not in plan["missing_symbols"]
    assert "PyArray_SHAPE" not in plan["missing_symbols"]
    assert "PyArray_FLAGS" not in plan["missing_symbols"]
    assert "PyArray_CompareLists" not in plan["missing_symbols"]
    assert "PyArray_Empty" not in plan["missing_symbols"]
    assert "PyArray_Zeros" not in plan["missing_symbols"]
    assert "PyArray_EMPTY" not in plan["missing_symbols"]
    assert "PyArray_ZEROS" not in plan["missing_symbols"]
    assert "PyArray_EquivTypes" not in plan["missing_symbols"]
    assert "PyArray_EquivArrTypes" not in plan["missing_symbols"]
    assert "PyArray_NewFromDescr" not in plan["missing_symbols"]
    assert "PyArray_SimpleNewFromDescr" not in plan["missing_symbols"]
    assert "PyArray_BASE" not in plan["missing_symbols"]
    assert "PyArray_SetBaseObject" not in plan["missing_symbols"]
    assert "PyArray_Return" not in plan["missing_symbols"]
    assert "PyArray_ENABLEFLAGS" not in plan["missing_symbols"]
    assert "PyArray_CLEARFLAGS" not in plan["missing_symbols"]
    assert "PyArray_UpdateFlags" not in plan["missing_symbols"]
    assert "PyArray_CopyInto" not in plan["missing_symbols"]
    assert "PyArray_CopyAnyInto" not in plan["missing_symbols"]
    assert "PyArray_ToScalar" not in plan["missing_symbols"]
    assert "PyArray_Copy" not in plan["missing_symbols"]
    assert "PyArray_EnsureArray" not in plan["missing_symbols"]
    assert "PyArray_EnsureAnyArray" not in plan["missing_symbols"]
    assert "PyArray_SAMESHAPE" not in plan["missing_symbols"]
    assert "PyArray_ISCONTIGUOUS" not in plan["missing_symbols"]
    assert "PyArray_STRIDE" not in plan["missing_symbols"]
    assert "PyArray_GETPTR2" not in plan["missing_symbols"]
    assert "PyUFunc_API" not in plan["missing_symbols"]
    assert "PyUFunc_FromFuncAndData" not in plan["missing_symbols"]
    assert plan["unknown_symbols"] == []
    assert "numpy/arrayobject.h" in plan["header_manifest"]["headers"]
    assert "numpy/ufuncobject.h" in plan["header_manifest"]["headers"]
    assert plan["diagnostics"] == []
    status = {row["symbol"]: row for row in plan["numpy_capi_status"]}
    assert status["PyArray_API"]["implemented"] is True
    assert status["PyArray_API"]["provider_shape"] == "array_api"
    assert status["PyArray_API"]["table"] == "_ARRAY_API"
    assert status["PyArray_API"]["slot"] is None
    assert status["PyArray_API"]["failure_mode"] == "implemented_provider_table"
    assert status["PyUFunc_API"]["implemented"] is True
    assert status["PyUFunc_API"]["provider_shape"] == "ufunc_api"
    assert status["PyUFunc_API"]["table"] == "_UFUNC_API"
    assert status["PyUFunc_API"]["slot"] is None
    assert status["PyUFunc_API"]["failure_mode"] == "implemented_provider_table"
    assert status["PyArray_Type"]["implemented"] is True
    assert status["PyArray_Type"]["slot"] == 0
    assert status["PyArray_Type"]["failure_mode"] == "implemented_provider_type_object"
    assert status["PyArrayDescr_Type"]["implemented"] is True
    assert status["PyArrayDescr_Type"]["slot"] == 1
    assert (
        status["PyArrayDescr_Type"]["failure_mode"]
        == "implemented_provider_type_object"
    )
    assert status["PyArray_DescrCheck"]["implemented"] is True
    assert status["PyArray_DescrCheck"]["slot"] is None
    assert status["PyArray_DescrCheck"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_DescrFromType"]["implemented"] is True
    assert status["PyArray_DescrFromType"]["slot"] == 2
    assert (
        status["PyArray_DescrFromType"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_DescrNewFromType"]["implemented"] is True
    assert status["PyArray_DescrNewFromType"]["slot"] == 35
    assert (
        status["PyArray_DescrNewFromType"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_DescrNew"]["implemented"] is True
    assert status["PyArray_DescrNew"]["slot"] == 36
    assert (
        status["PyArray_DescrNew"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_DescrNewByteorder"]["implemented"] is True
    assert status["PyArray_DescrNewByteorder"]["slot"] == 37
    assert (
        status["PyArray_DescrNewByteorder"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_CanCastSafely"]["implemented"] is True
    assert status["PyArray_CanCastSafely"]["slot"] == 38
    assert (
        status["PyArray_CanCastSafely"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_CanCastTo"]["implemented"] is True
    assert status["PyArray_CanCastTo"]["slot"] == 52
    assert (
        status["PyArray_CanCastTo"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_Zero"]["implemented"] is True
    assert status["PyArray_Zero"]["slot"] == 53
    assert (
        status["PyArray_Zero"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_One"]["implemented"] is True
    assert status["PyArray_One"]["slot"] == 54
    assert (
        status["PyArray_One"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_TypeObjectFromType"]["implemented"] is True
    assert status["PyArray_TypeObjectFromType"]["slot"] == 55
    assert (
        status["PyArray_TypeObjectFromType"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_ObjectType"]["implemented"] is True
    assert status["PyArray_ObjectType"]["slot"] == 39
    assert (
        status["PyArray_ObjectType"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_DescrFromObject"]["implemented"] is True
    assert status["PyArray_DescrFromObject"]["slot"] == 56
    assert (
        status["PyArray_DescrFromObject"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_Size"]["implemented"] is True
    assert status["PyArray_Size"]["slot"] == 57
    assert (
        status["PyArray_Size"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_DescrFromScalar"]["implemented"] is True
    assert status["PyArray_DescrFromScalar"]["slot"] == 58
    assert (
        status["PyArray_DescrFromScalar"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_DescrFromTypeObject"]["implemented"] is True
    assert status["PyArray_DescrFromTypeObject"]["slot"] == 59
    assert (
        status["PyArray_DescrFromTypeObject"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_ScalarAsCtype"]["implemented"] is True
    assert status["PyArray_ScalarAsCtype"]["slot"] == 60
    assert (
        status["PyArray_ScalarAsCtype"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_FromScalar"]["implemented"] is True
    assert status["PyArray_FromScalar"]["slot"] == 61
    assert (
        status["PyArray_FromScalar"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_CastScalarToCtype"]["implemented"] is True
    assert status["PyArray_CastScalarToCtype"]["slot"] == 62
    assert (
        status["PyArray_CastScalarToCtype"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_CastScalarDirect"]["implemented"] is True
    assert status["PyArray_CastScalarDirect"]["slot"] == 64
    assert (
        status["PyArray_CastScalarDirect"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_Pack"]["implemented"] is True
    assert status["PyArray_Pack"]["slot"] == 63
    assert (
        status["PyArray_Pack"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_CastToType"]["implemented"] is True
    assert status["PyArray_CastToType"]["slot"] == 65
    assert (
        status["PyArray_CastToType"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_Cast"]["implemented"] is True
    assert status["PyArray_Cast"]["slot"] is None
    assert status["PyArray_Cast"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_FillWithScalar"]["implemented"] is True
    assert status["PyArray_FillWithScalar"]["slot"] == 66
    assert (
        status["PyArray_FillWithScalar"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_ToList"]["implemented"] is True
    assert status["PyArray_ToList"]["slot"] == 67
    assert (
        status["PyArray_ToList"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_ToString"]["implemented"] is True
    assert status["PyArray_ToString"]["slot"] == 68
    assert (
        status["PyArray_ToString"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_Byteswap"]["implemented"] is True
    assert status["PyArray_Byteswap"]["slot"] == 69
    assert (
        status["PyArray_Byteswap"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_FromString"]["implemented"] is True
    assert status["PyArray_FromString"]["slot"] == 70
    assert (
        status["PyArray_FromString"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_FromBuffer"]["implemented"] is True
    assert status["PyArray_FromBuffer"]["slot"] == 71
    assert (
        status["PyArray_FromBuffer"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_FromAny"]["implemented"] is True
    assert status["PyArray_FromAny"]["slot"] == 3
    assert status["PyArray_FromAny"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_CheckFromAny"]["implemented"] is True
    assert status["PyArray_CheckFromAny"]["slot"] == 40
    assert (
        status["PyArray_CheckFromAny"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_FromArray"]["implemented"] is True
    assert status["PyArray_FromArray"]["slot"] == 41
    assert (
        status["PyArray_FromArray"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_MultiplyList"]["implemented"] is True
    assert status["PyArray_MultiplyList"]["slot"] == 42
    assert (
        status["PyArray_MultiplyList"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_MultiplyIntList"]["implemented"] is True
    assert status["PyArray_MultiplyIntList"]["slot"] == 43
    assert (
        status["PyArray_MultiplyIntList"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_GetPtr"]["implemented"] is True
    assert status["PyArray_GetPtr"]["slot"] == 44
    assert status["PyArray_GetPtr"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_ElementStrides"]["implemented"] is True
    assert status["PyArray_ElementStrides"]["slot"] == 45
    assert (
        status["PyArray_ElementStrides"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_ValidType"]["implemented"] is True
    assert status["PyArray_ValidType"]["slot"] == 46
    assert (
        status["PyArray_ValidType"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_Item_INCREF"]["implemented"] is True
    assert status["PyArray_Item_INCREF"]["slot"] == 47
    assert (
        status["PyArray_Item_INCREF"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_Item_XDECREF"]["implemented"] is True
    assert status["PyArray_Item_XDECREF"]["slot"] == 48
    assert (
        status["PyArray_Item_XDECREF"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_NewCopy"]["implemented"] is True
    assert status["PyArray_NewCopy"]["slot"] == 49
    assert (
        status["PyArray_NewCopy"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_INCREF"]["implemented"] is True
    assert status["PyArray_INCREF"]["slot"] == 50
    assert (
        status["PyArray_INCREF"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_XDECREF"]["implemented"] is True
    assert status["PyArray_XDECREF"]["slot"] == 51
    assert (
        status["PyArray_XDECREF"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_SimpleNew"]["implemented"] is True
    assert status["PyArray_SimpleNew"]["slot"] == 4
    assert status["PyArray_SimpleNew"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_SimpleNewFromData"]["implemented"] is True
    assert status["PyArray_SimpleNewFromData"]["slot"] == 5
    assert (
        status["PyArray_SimpleNewFromData"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_NDIM"]["implemented"] is True
    assert status["PyArray_NDIM"]["slot"] == 6
    assert status["PyArray_NDIM"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_DIMS"]["implemented"] is True
    assert status["PyArray_DIMS"]["slot"] == 7
    assert status["PyArray_DIMS"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_STRIDES"]["implemented"] is True
    assert status["PyArray_STRIDES"]["slot"] == 8
    assert status["PyArray_STRIDES"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_DATA"]["implemented"] is True
    assert status["PyArray_DATA"]["slot"] == 9
    assert status["PyArray_DATA"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_DESCR"]["implemented"] is True
    assert status["PyArray_DESCR"]["slot"] == 10
    assert status["PyArray_DESCR"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_DIM"]["implemented"] is True
    assert status["PyArray_DIM"]["slot"] == 7
    assert status["PyArray_DIM"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_BYTES"]["implemented"] is True
    assert status["PyArray_BYTES"]["slot"] == 9
    assert status["PyArray_BYTES"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_SIZE"]["implemented"] is True
    assert status["PyArray_SIZE"]["slot"] == 13
    assert status["PyArray_SIZE"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_ITEMSIZE"]["implemented"] is True
    assert status["PyArray_ITEMSIZE"]["slot"] == 14
    assert status["PyArray_ITEMSIZE"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_GETITEM"]["implemented"] is True
    assert status["PyArray_GETITEM"]["slot"] == 11
    assert status["PyArray_GETITEM"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_SETITEM"]["implemented"] is True
    assert status["PyArray_SETITEM"]["slot"] == 12
    assert status["PyArray_SETITEM"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_Check"]["implemented"] is True
    assert status["PyArray_Check"]["slot"] == 15
    assert status["PyArray_Check"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_CheckExact"]["implemented"] is True
    assert status["PyArray_CheckExact"]["slot"] == 16
    assert status["PyArray_CheckExact"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_DTYPE"]["implemented"] is True
    assert status["PyArray_DTYPE"]["slot"] is None
    assert status["PyArray_DTYPE"]["failure_mode"] == "implemented_header_macro"
    assert status["PyDataType_TYPE"]["implemented"] is True
    assert status["PyDataType_TYPE"]["slot"] is None
    assert status["PyDataType_TYPE"]["failure_mode"] == "implemented_header_macro"
    assert status["PyTypeNum_ISFLOAT"]["implemented"] is True
    assert status["PyTypeNum_ISFLOAT"]["slot"] is None
    assert status["PyTypeNum_ISFLOAT"]["failure_mode"] == "implemented_header_macro"
    assert status["PyDataType_ISNUMBER"]["implemented"] is True
    assert status["PyDataType_ISNUMBER"]["slot"] is None
    assert status["PyDataType_ISNUMBER"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_ISOBJECT"]["implemented"] is True
    assert status["PyArray_ISOBJECT"]["slot"] is None
    assert status["PyArray_ISOBJECT"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_ISONESEGMENT"]["implemented"] is True
    assert status["PyArray_ISONESEGMENT"]["slot"] is None
    assert status["PyArray_ISONESEGMENT"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_ISNBO"]["implemented"] is True
    assert status["PyArray_ISNBO"]["slot"] is None
    assert status["PyArray_ISNBO"]["failure_mode"] == "implemented_header_macro"
    assert status["PyDataType_ISBYTESWAPPED"]["implemented"] is True
    assert status["PyDataType_ISBYTESWAPPED"]["slot"] is None
    assert (
        status["PyDataType_ISBYTESWAPPED"]["failure_mode"]
        == "implemented_header_macro"
    )
    assert status["PyArray_SAFEALIGNEDCOPY"]["implemented"] is True
    assert status["PyArray_SAFEALIGNEDCOPY"]["slot"] is None
    assert status["PyArray_SAFEALIGNEDCOPY"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_FROMANY"]["implemented"] is True
    assert status["PyArray_FROMANY"]["slot"] is None
    assert status["PyArray_FROMANY"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_FROM_OF"]["implemented"] is True
    assert status["PyArray_FROM_OF"]["slot"] is None
    assert status["PyArray_FROM_OF"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_FromObject"]["implemented"] is True
    assert status["PyArray_FromObject"]["slot"] is None
    assert status["PyArray_FromObject"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_CopyFromObject"]["implemented"] is True
    assert status["PyArray_CopyFromObject"]["slot"] is None
    assert status["PyArray_CopyFromObject"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_TYPE"]["implemented"] is True
    assert status["PyArray_NBYTES"]["implemented"] is True
    assert status["PyArray_FILLWBYTE"]["implemented"] is True
    assert status["PyArray_FILLWBYTE"]["slot"] is None
    assert status["PyArray_FILLWBYTE"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_EquivByteorders"]["implemented"] is True
    assert status["PyArray_EquivByteorders"]["slot"] is None
    assert (
        status["PyArray_EquivByteorders"]["failure_mode"]
        == "implemented_header_macro"
    )
    assert status["PyArray_SHAPE"]["implemented"] is True
    assert status["PyArray_SHAPE"]["slot"] is None
    assert status["PyArray_SHAPE"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_FLAGS"]["implemented"] is True
    assert status["PyArray_FLAGS"]["slot"] == 17
    assert status["PyArray_FLAGS"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_Check"]["implemented"] is True
    assert status["PyArray_Check"]["slot"] == 15
    assert status["PyArray_Check"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_CheckExact"]["implemented"] is True
    assert status["PyArray_CheckExact"]["slot"] == 16
    assert status["PyArray_CheckExact"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_CompareLists"]["implemented"] is True
    assert status["PyArray_CompareLists"]["slot"] == 18
    assert status["PyArray_CompareLists"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_Empty"]["implemented"] is True
    assert status["PyArray_Empty"]["slot"] == 19
    assert status["PyArray_Empty"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_Zeros"]["implemented"] is True
    assert status["PyArray_Zeros"]["slot"] == 20
    assert status["PyArray_Zeros"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_EMPTY"]["implemented"] is True
    assert status["PyArray_EMPTY"]["slot"] is None
    assert status["PyArray_EMPTY"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_ZEROS"]["implemented"] is True
    assert status["PyArray_ZEROS"]["slot"] is None
    assert status["PyArray_ZEROS"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_EquivTypes"]["implemented"] is True
    assert status["PyArray_EquivTypes"]["slot"] == 21
    assert status["PyArray_EquivTypes"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_EquivArrTypes"]["implemented"] is True
    assert status["PyArray_EquivArrTypes"]["slot"] is None
    assert status["PyArray_EquivArrTypes"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_NewFromDescr"]["implemented"] is True
    assert status["PyArray_NewFromDescr"]["slot"] == 22
    assert status["PyArray_NewFromDescr"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_SimpleNewFromDescr"]["implemented"] is True
    assert status["PyArray_SimpleNewFromDescr"]["slot"] is None
    assert (
        status["PyArray_SimpleNewFromDescr"]["failure_mode"]
        == "implemented_header_macro"
    )
    assert status["PyArray_BASE"]["implemented"] is True
    assert status["PyArray_BASE"]["slot"] == 23
    assert status["PyArray_BASE"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_SetBaseObject"]["implemented"] is True
    assert status["PyArray_SetBaseObject"]["slot"] == 24
    assert status["PyArray_SetBaseObject"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_Return"]["implemented"] is True
    assert status["PyArray_Return"]["slot"] == 25
    assert status["PyArray_Return"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_ENABLEFLAGS"]["implemented"] is True
    assert status["PyArray_ENABLEFLAGS"]["slot"] == 26
    assert status["PyArray_ENABLEFLAGS"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_CLEARFLAGS"]["implemented"] is True
    assert status["PyArray_CLEARFLAGS"]["slot"] == 27
    assert status["PyArray_CLEARFLAGS"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_UpdateFlags"]["implemented"] is True
    assert status["PyArray_UpdateFlags"]["slot"] == 28
    assert status["PyArray_UpdateFlags"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_CopyInto"]["implemented"] is True
    assert status["PyArray_CopyInto"]["slot"] == 29
    assert status["PyArray_CopyInto"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_CopyAnyInto"]["implemented"] is True
    assert status["PyArray_CopyAnyInto"]["slot"] == 30
    assert status["PyArray_CopyAnyInto"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_ToScalar"]["implemented"] is True
    assert status["PyArray_ToScalar"]["slot"] == 31
    assert status["PyArray_ToScalar"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_Copy"]["implemented"] is True
    assert status["PyArray_Copy"]["slot"] == 32
    assert status["PyArray_Copy"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_EnsureArray"]["implemented"] is True
    assert status["PyArray_EnsureArray"]["slot"] == 33
    assert status["PyArray_EnsureArray"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_EnsureAnyArray"]["implemented"] is True
    assert status["PyArray_EnsureAnyArray"]["slot"] == 34
    assert (
        status["PyArray_EnsureAnyArray"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_SAMESHAPE"]["implemented"] is True
    assert status["PyArray_SAMESHAPE"]["slot"] is None
    assert status["PyArray_SAMESHAPE"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_ISCONTIGUOUS"]["implemented"] is True
    assert status["PyArray_ISCONTIGUOUS"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_STRIDE"]["implemented"] is True
    assert status["PyArray_STRIDE"]["slot"] is None
    assert status["PyArray_STRIDE"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_GETPTR2"]["implemented"] is True
    assert status["PyArray_GETPTR2"]["slot"] is None
    assert status["PyArray_GETPTR2"]["failure_mode"] == "implemented_header_macro"


def test_extension_abi_plan_sees_fake_numpy_headers_and_provider_subset():
    fake_include = Path("utils/fake_libc_include").resolve()
    assert (fake_include / "numpy/arrayobject.h").exists()
    assert (fake_include / "numpy/ufuncobject.h").exists()
    plan = extension_abi_plan(
        [],
        provider="numpy-capi",
        include_dir=str(fake_include),
        require_numpy_capi=True,
    )
    assert plan["ok"] is True
    assert "PyArray_API" not in plan["missing_symbols"]
    assert "PyUFunc_API" not in plan["missing_symbols"]
    assert "numpy/arrayobject.h" in plan["header_manifest"]["provided_headers"]
    assert "numpy/ufuncobject.h" in plan["header_manifest"]["provided_headers"]
    assert "numpy/arrayobject.h" not in plan["header_manifest"]["missing_headers"]
    assert "numpy/ufuncobject.h" not in plan["header_manifest"]["missing_headers"]


def test_extension_abi_plan_scans_package_supplied_headers(tmp_path):
    include = tmp_path / "include"
    include.mkdir()
    (include / "Python.h").write_text("int Py_Initialize(void);\n", encoding="utf-8")
    (include / "abstract.h").write_text(
        "int PyObject_GetBuffer(void);\n", encoding="utf-8"
    )

    ok_plan = extension_abi_plan(["Py_Initialize"], include_dir=str(include))
    assert ok_plan["ok"] is True
    assert ok_plan["header_manifest"]["provided_headers"] == ["Python.h"]
    assert ok_plan["header_manifest"]["missing_headers"] == []
    assert ok_plan["header_manifest"]["symbols"][0]["provided_by_package"] is True

    missing_header = extension_abi_plan(
        ["Py_Initialize"],
        include_dir=str(tmp_path / "missing"),
    )
    assert missing_header["ok"] is False
    assert missing_header["header_manifest"]["missing_headers"] == ["Python.h"]
    assert missing_header["diagnostics"][0]["code"] == "PCC-EXT-MISSING-CAPI-HEADER"


def test_pcc_package_ext_abi_cli_is_json_ready():
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    proc = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "ext-abi",
            "--symbol",
            "Py_Initialize",
            "--require-buffer",
            "--provider",
            "array-api",
            "--expected-abi",
            "1",
            "--actual-abi",
            "2",
            "--json",
        ],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert proc.returncode == 2
    plan = json.loads(proc.stdout)
    assert plan["provider"] == "array-api"
    assert plan["abi_version"]["code"] == "PCC-EXT-ABI-VERSION-MISMATCH"
    assert "abstract.h" in plan["header_manifest"]["headers"]
    assert "PyObject_GetBuffer" not in plan["missing_symbols"]
    assert "PyObject_CheckBuffer" not in plan["missing_symbols"]
    assert "PyBuffer_Release" not in plan["missing_symbols"]


def test_pcc_package_ext_abi_cli_reports_numpy_capi_bucket():
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    proc = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "ext-abi",
            "--require-numpy-capi",
            "--provider",
            "numpy-capi",
            "--json",
        ],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert proc.returncode == 0
    plan = json.loads(proc.stdout)
    assert plan["provider"] == "numpy-capi"
    assert "PyArray_API" not in plan["missing_symbols"]
    assert "PyArray_Type" not in plan["missing_symbols"]
    assert "PyArrayDescr_Type" not in plan["missing_symbols"]
    assert "PyArray_DescrCheck" not in plan["missing_symbols"]
    assert "PyArray_DescrFromType" not in plan["missing_symbols"]
    assert "PyArray_DescrNewFromType" not in plan["missing_symbols"]
    assert "PyArray_DescrNew" not in plan["missing_symbols"]
    assert "PyArray_DescrNewByteorder" not in plan["missing_symbols"]
    assert "PyArray_CanCastSafely" not in plan["missing_symbols"]
    assert "PyArray_CanCastTo" not in plan["missing_symbols"]
    assert "PyArray_Zero" not in plan["missing_symbols"]
    assert "PyArray_One" not in plan["missing_symbols"]
    assert "PyArray_TypeObjectFromType" not in plan["missing_symbols"]
    assert "PyArray_ObjectType" not in plan["missing_symbols"]
    assert "PyArray_DescrFromObject" not in plan["missing_symbols"]
    assert "PyArray_Size" not in plan["missing_symbols"]
    assert "PyArray_DescrFromScalar" not in plan["missing_symbols"]
    assert "PyArray_DescrFromTypeObject" not in plan["missing_symbols"]
    assert "PyArray_ScalarAsCtype" not in plan["missing_symbols"]
    assert "PyArray_FromScalar" not in plan["missing_symbols"]
    assert "PyArray_CastScalarToCtype" not in plan["missing_symbols"]
    assert "PyArray_CastScalarDirect" not in plan["missing_symbols"]
    assert "PyArray_Pack" not in plan["missing_symbols"]
    assert "PyArray_CastToType" not in plan["missing_symbols"]
    assert "PyArray_Cast" not in plan["missing_symbols"]
    assert "PyArray_FillWithScalar" not in plan["missing_symbols"]
    assert "PyArray_ToList" not in plan["missing_symbols"]
    assert "PyArray_ToString" not in plan["missing_symbols"]
    assert "PyArray_Byteswap" not in plan["missing_symbols"]
    assert "PyArray_CheckFromAny" not in plan["missing_symbols"]
    assert "PyArray_FromArray" not in plan["missing_symbols"]
    assert "PyArray_MultiplyList" not in plan["missing_symbols"]
    assert "PyArray_MultiplyIntList" not in plan["missing_symbols"]
    assert "PyArray_GetPtr" not in plan["missing_symbols"]
    assert "PyArray_ElementStrides" not in plan["missing_symbols"]
    assert "PyArray_ValidType" not in plan["missing_symbols"]
    assert "PyArray_Item_INCREF" not in plan["missing_symbols"]
    assert "PyArray_Item_XDECREF" not in plan["missing_symbols"]
    assert "PyArray_NewCopy" not in plan["missing_symbols"]
    assert "PyArray_INCREF" not in plan["missing_symbols"]
    assert "PyArray_XDECREF" not in plan["missing_symbols"]
    assert "PyArray_FromAny" not in plan["missing_symbols"]
    assert "PyArray_SimpleNew" not in plan["missing_symbols"]
    assert "PyArray_SimpleNewFromData" not in plan["missing_symbols"]
    assert "PyArray_GETITEM" not in plan["missing_symbols"]
    assert "PyArray_SETITEM" not in plan["missing_symbols"]
    assert "PyArray_NDIM" not in plan["missing_symbols"]
    assert "PyArray_DIMS" not in plan["missing_symbols"]
    assert "PyArray_STRIDES" not in plan["missing_symbols"]
    assert "PyArray_DATA" not in plan["missing_symbols"]
    assert "PyArray_DESCR" not in plan["missing_symbols"]
    assert "PyArray_SIZE" not in plan["missing_symbols"]
    assert "PyArray_ITEMSIZE" not in plan["missing_symbols"]
    assert "PyArray_DIM" not in plan["missing_symbols"]
    assert "PyArray_BYTES" not in plan["missing_symbols"]
    assert "PyUFunc_API" not in plan["missing_symbols"]
    assert "PyUFunc_FromFuncAndData" not in plan["missing_symbols"]
    assert plan["diagnostics"] == []
    assert "PyArray_DTYPE" in plan["required_symbols"]
    assert "PyDataType_TYPE" in plan["required_symbols"]
    assert "PyTypeNum_ISFLOAT" in plan["required_symbols"]
    assert "PyDataType_ISNUMBER" in plan["required_symbols"]
    assert "PyArray_ISOBJECT" in plan["required_symbols"]
    assert "PyArray_ISONESEGMENT" in plan["required_symbols"]
    assert "PyArray_ISNBO" in plan["required_symbols"]
    assert "PyDataType_ISBYTESWAPPED" in plan["required_symbols"]
    assert "PyArray_SAFEALIGNEDCOPY" in plan["required_symbols"]
    assert "PyArray_FROMANY" in plan["required_symbols"]
    assert "PyArray_FROM_OF" in plan["required_symbols"]
    assert "PyArray_FromObject" in plan["required_symbols"]
    assert "PyArray_CopyFromObject" in plan["required_symbols"]
    assert "PyArray_TYPE" in plan["required_symbols"]
    assert "PyArray_NBYTES" in plan["required_symbols"]
    assert "PyArray_FILLWBYTE" in plan["required_symbols"]
    assert "PyArray_EquivByteorders" in plan["required_symbols"]
    assert "PyArray_SHAPE" in plan["required_symbols"]
    assert "PyArray_FLAGS" in plan["required_symbols"]
    assert "PyArray_CompareLists" in plan["required_symbols"]
    assert "PyArray_Empty" in plan["required_symbols"]
    assert "PyArray_Zeros" in plan["required_symbols"]
    assert "PyArray_EMPTY" in plan["required_symbols"]
    assert "PyArray_ZEROS" in plan["required_symbols"]
    assert "PyArray_EquivTypes" in plan["required_symbols"]
    assert "PyArray_EquivArrTypes" in plan["required_symbols"]
    assert "PyArray_NewFromDescr" in plan["required_symbols"]
    assert "PyArray_SimpleNewFromDescr" in plan["required_symbols"]
    assert "PyArray_BASE" in plan["required_symbols"]
    assert "PyArray_SetBaseObject" in plan["required_symbols"]
    assert "PyArray_Return" in plan["required_symbols"]
    assert "PyArray_ENABLEFLAGS" in plan["required_symbols"]
    assert "PyArray_CLEARFLAGS" in plan["required_symbols"]
    assert "PyArray_UpdateFlags" in plan["required_symbols"]
    assert "PyArray_CopyInto" in plan["required_symbols"]
    assert "PyArray_CopyAnyInto" in plan["required_symbols"]
    assert "PyArray_ToScalar" in plan["required_symbols"]
    assert "PyArray_Copy" in plan["required_symbols"]
    assert "PyArray_EnsureArray" in plan["required_symbols"]
    assert "PyArray_EnsureAnyArray" in plan["required_symbols"]
    assert "PyArray_SAMESHAPE" in plan["required_symbols"]
    assert "PyArray_ISCONTIGUOUS" in plan["required_symbols"]
    assert "PyArray_STRIDE" in plan["required_symbols"]
    assert "PyArray_GETPTR2" in plan["required_symbols"]
    assert "PyArray_TYPE" not in plan["missing_symbols"]
    assert "PyArray_NBYTES" not in plan["missing_symbols"]
    assert "PyArray_FILLWBYTE" not in plan["missing_symbols"]
    assert "PyArray_EquivByteorders" not in plan["missing_symbols"]
    assert "PyArray_SHAPE" not in plan["missing_symbols"]
    assert "PyArray_FLAGS" not in plan["missing_symbols"]
    assert "PyArray_CompareLists" not in plan["missing_symbols"]
    assert "PyArray_Empty" not in plan["missing_symbols"]
    assert "PyArray_Zeros" not in plan["missing_symbols"]
    assert "PyArray_EMPTY" not in plan["missing_symbols"]
    assert "PyArray_ZEROS" not in plan["missing_symbols"]
    assert "PyArray_EquivTypes" not in plan["missing_symbols"]
    assert "PyArray_EquivArrTypes" not in plan["missing_symbols"]
    assert "PyArray_NewFromDescr" not in plan["missing_symbols"]
    assert "PyArray_SimpleNewFromDescr" not in plan["missing_symbols"]
    assert "PyArray_BASE" not in plan["missing_symbols"]
    assert "PyArray_SetBaseObject" not in plan["missing_symbols"]
    assert "PyArray_Return" not in plan["missing_symbols"]
    assert "PyArray_ENABLEFLAGS" not in plan["missing_symbols"]
    assert "PyArray_CLEARFLAGS" not in plan["missing_symbols"]
    assert "PyArray_UpdateFlags" not in plan["missing_symbols"]
    assert "PyArray_CopyInto" not in plan["missing_symbols"]
    assert "PyArray_CopyAnyInto" not in plan["missing_symbols"]
    assert "PyArray_ToScalar" not in plan["missing_symbols"]
    assert "PyArray_Copy" not in plan["missing_symbols"]
    assert "PyArray_EnsureArray" not in plan["missing_symbols"]
    assert "PyArray_EnsureAnyArray" not in plan["missing_symbols"]
    assert "PyArray_SAMESHAPE" not in plan["missing_symbols"]
    assert "PyArray_ISCONTIGUOUS" not in plan["missing_symbols"]
    assert "PyArray_ISONESEGMENT" not in plan["missing_symbols"]
    assert "PyArray_ISNBO" not in plan["missing_symbols"]
    assert "PyDataType_ISBYTESWAPPED" not in plan["missing_symbols"]
    assert "PyArray_SAFEALIGNEDCOPY" not in plan["missing_symbols"]
    assert "PyArray_FROMANY" not in plan["missing_symbols"]
    assert "PyArray_FROM_OF" not in plan["missing_symbols"]
    assert "PyArray_FromObject" not in plan["missing_symbols"]
    assert "PyArray_CopyFromObject" not in plan["missing_symbols"]
    assert "PyArray_Check" not in plan["missing_symbols"]
    assert "PyArray_CheckExact" not in plan["missing_symbols"]
    assert "PyArray_DescrFromObject" not in plan["missing_symbols"]
    assert "PyArray_Size" not in plan["missing_symbols"]
    assert "PyArray_DescrFromScalar" not in plan["missing_symbols"]
    assert "PyArray_DescrFromTypeObject" not in plan["missing_symbols"]
    assert "PyArray_ScalarAsCtype" not in plan["missing_symbols"]
    assert "PyArray_FromScalar" not in plan["missing_symbols"]
    assert "PyArray_CastScalarToCtype" not in plan["missing_symbols"]
    assert "PyArray_CastScalarDirect" not in plan["missing_symbols"]
    assert "PyArray_Pack" not in plan["missing_symbols"]
    assert "PyArray_CastToType" not in plan["missing_symbols"]
    assert "PyArray_Cast" not in plan["missing_symbols"]
    assert "PyArray_FillWithScalar" not in plan["missing_symbols"]
    assert "PyArray_ToList" not in plan["missing_symbols"]
    assert "PyArray_ToString" not in plan["missing_symbols"]
    assert "PyArray_Byteswap" not in plan["missing_symbols"]
    assert "PyArray_CheckFromAny" not in plan["missing_symbols"]
    assert "PyArray_FromArray" not in plan["missing_symbols"]
    assert "PyArray_MultiplyList" not in plan["missing_symbols"]
    assert "PyArray_MultiplyIntList" not in plan["missing_symbols"]
    assert "PyArray_GetPtr" not in plan["missing_symbols"]
    assert "PyArray_ElementStrides" not in plan["missing_symbols"]
    assert "PyArray_ValidType" not in plan["missing_symbols"]
    assert "PyArray_Item_INCREF" not in plan["missing_symbols"]
    assert "PyArray_Item_XDECREF" not in plan["missing_symbols"]
    assert "PyArray_NewCopy" not in plan["missing_symbols"]
    assert "PyArray_INCREF" not in plan["missing_symbols"]
    assert "PyArray_XDECREF" not in plan["missing_symbols"]
    assert "PyArray_DescrFromType" not in plan["missing_symbols"]
    assert "PyArray_FromAny" not in plan["missing_symbols"]
    assert "PyArray_SimpleNew" not in plan["missing_symbols"]
    assert "PyArray_SimpleNewFromData" not in plan["missing_symbols"]
    assert "PyArray_GETITEM" not in plan["missing_symbols"]
    assert "PyArray_SETITEM" not in plan["missing_symbols"]
    assert "PyArray_NDIM" not in plan["missing_symbols"]
    assert "PyArray_DIMS" not in plan["missing_symbols"]
    assert "PyArray_STRIDES" not in plan["missing_symbols"]
    assert "PyArray_DATA" not in plan["missing_symbols"]
    assert "PyArray_DESCR" not in plan["missing_symbols"]
    assert "PyArray_SIZE" not in plan["missing_symbols"]
    assert "PyArray_ITEMSIZE" not in plan["missing_symbols"]
    assert "PyArray_DIM" not in plan["missing_symbols"]
    assert "PyArray_BYTES" not in plan["missing_symbols"]
    assert "PyArray_STRIDE" not in plan["missing_symbols"]
    assert "PyArray_GETPTR2" not in plan["missing_symbols"]
    assert plan["unknown_symbols"] == []
    assert plan["diagnostics"] == []
    assert plan["numpy_capi_status"]
    status = {row["symbol"]: row for row in plan["numpy_capi_status"]}
    assert status["PyArray_DATA"]["table"] == "_ARRAY_API"
    assert status["PyArray_DescrCheck"]["implemented"] is True
    assert status["PyArray_DescrCheck"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_DescrNewFromType"]["implemented"] is True
    assert (
        status["PyArray_DescrNewFromType"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_DescrNew"]["implemented"] is True
    assert (
        status["PyArray_DescrNew"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_DescrNewByteorder"]["implemented"] is True
    assert (
        status["PyArray_DescrNewByteorder"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_CanCastSafely"]["implemented"] is True
    assert (
        status["PyArray_CanCastSafely"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_CanCastTo"]["implemented"] is True
    assert status["PyArray_CanCastTo"]["slot"] == 52
    assert (
        status["PyArray_CanCastTo"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_Zero"]["implemented"] is True
    assert status["PyArray_Zero"]["slot"] == 53
    assert (
        status["PyArray_Zero"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_One"]["implemented"] is True
    assert status["PyArray_One"]["slot"] == 54
    assert (
        status["PyArray_One"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_TypeObjectFromType"]["implemented"] is True
    assert status["PyArray_TypeObjectFromType"]["slot"] == 55
    assert (
        status["PyArray_TypeObjectFromType"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_ObjectType"]["implemented"] is True
    assert (
        status["PyArray_ObjectType"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_DescrFromObject"]["implemented"] is True
    assert status["PyArray_DescrFromObject"]["slot"] == 56
    assert (
        status["PyArray_DescrFromObject"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_Size"]["implemented"] is True
    assert status["PyArray_Size"]["slot"] == 57
    assert (
        status["PyArray_Size"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_DescrFromScalar"]["implemented"] is True
    assert status["PyArray_DescrFromScalar"]["slot"] == 58
    assert (
        status["PyArray_DescrFromScalar"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_DescrFromTypeObject"]["implemented"] is True
    assert status["PyArray_DescrFromTypeObject"]["slot"] == 59
    assert (
        status["PyArray_DescrFromTypeObject"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_ScalarAsCtype"]["implemented"] is True
    assert status["PyArray_ScalarAsCtype"]["slot"] == 60
    assert (
        status["PyArray_ScalarAsCtype"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_FromScalar"]["implemented"] is True
    assert status["PyArray_FromScalar"]["slot"] == 61
    assert (
        status["PyArray_FromScalar"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_CastScalarToCtype"]["implemented"] is True
    assert status["PyArray_CastScalarToCtype"]["slot"] == 62
    assert (
        status["PyArray_CastScalarToCtype"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_CastScalarDirect"]["implemented"] is True
    assert status["PyArray_CastScalarDirect"]["slot"] == 64
    assert (
        status["PyArray_CastScalarDirect"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_Pack"]["implemented"] is True
    assert status["PyArray_Pack"]["slot"] == 63
    assert (
        status["PyArray_Pack"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_CastToType"]["implemented"] is True
    assert status["PyArray_CastToType"]["slot"] == 65
    assert (
        status["PyArray_CastToType"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_Cast"]["implemented"] is True
    assert status["PyArray_Cast"]["slot"] is None
    assert status["PyArray_Cast"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_FillWithScalar"]["implemented"] is True
    assert status["PyArray_FillWithScalar"]["slot"] == 66
    assert (
        status["PyArray_FillWithScalar"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_ToList"]["implemented"] is True
    assert status["PyArray_ToList"]["slot"] == 67
    assert (
        status["PyArray_ToList"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_ToString"]["implemented"] is True
    assert status["PyArray_ToString"]["slot"] == 68
    assert (
        status["PyArray_ToString"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_Byteswap"]["implemented"] is True
    assert status["PyArray_Byteswap"]["slot"] == 69
    assert (
        status["PyArray_Byteswap"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_DescrFromType"]["implemented"] is True
    assert status["PyArray_DescrFromType"]["slot"] == 2
    assert (
        status["PyArray_DescrFromType"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_FromAny"]["implemented"] is True
    assert status["PyArray_FromAny"]["slot"] == 3
    assert status["PyArray_FromAny"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_CheckFromAny"]["implemented"] is True
    assert status["PyArray_CheckFromAny"]["slot"] == 40
    assert (
        status["PyArray_CheckFromAny"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_FromArray"]["implemented"] is True
    assert status["PyArray_FromArray"]["slot"] == 41
    assert (
        status["PyArray_FromArray"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_MultiplyList"]["implemented"] is True
    assert status["PyArray_MultiplyList"]["slot"] == 42
    assert (
        status["PyArray_MultiplyList"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_MultiplyIntList"]["implemented"] is True
    assert status["PyArray_MultiplyIntList"]["slot"] == 43
    assert (
        status["PyArray_MultiplyIntList"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_GetPtr"]["implemented"] is True
    assert status["PyArray_GetPtr"]["slot"] == 44
    assert status["PyArray_GetPtr"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_ElementStrides"]["implemented"] is True
    assert status["PyArray_ElementStrides"]["slot"] == 45
    assert (
        status["PyArray_ElementStrides"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_ValidType"]["implemented"] is True
    assert status["PyArray_ValidType"]["slot"] == 46
    assert (
        status["PyArray_ValidType"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_Item_INCREF"]["implemented"] is True
    assert status["PyArray_Item_INCREF"]["slot"] == 47
    assert (
        status["PyArray_Item_INCREF"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_Item_XDECREF"]["implemented"] is True
    assert status["PyArray_Item_XDECREF"]["slot"] == 48
    assert (
        status["PyArray_Item_XDECREF"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_NewCopy"]["implemented"] is True
    assert status["PyArray_NewCopy"]["slot"] == 49
    assert (
        status["PyArray_NewCopy"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_INCREF"]["implemented"] is True
    assert status["PyArray_INCREF"]["slot"] == 50
    assert (
        status["PyArray_INCREF"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_XDECREF"]["implemented"] is True
    assert status["PyArray_XDECREF"]["slot"] == 51
    assert (
        status["PyArray_XDECREF"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_SimpleNew"]["implemented"] is True
    assert status["PyArray_SimpleNew"]["slot"] == 4
    assert status["PyArray_SimpleNew"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_SimpleNewFromData"]["implemented"] is True
    assert status["PyArray_SimpleNewFromData"]["slot"] == 5
    assert (
        status["PyArray_SimpleNewFromData"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_NDIM"]["implemented"] is True
    assert status["PyArray_NDIM"]["slot"] == 6
    assert status["PyArray_NDIM"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_DIMS"]["implemented"] is True
    assert status["PyArray_DIMS"]["slot"] == 7
    assert status["PyArray_DIMS"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_STRIDES"]["implemented"] is True
    assert status["PyArray_STRIDES"]["slot"] == 8
    assert status["PyArray_STRIDES"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_DATA"]["implemented"] is True
    assert status["PyArray_DATA"]["slot"] == 9
    assert status["PyArray_DATA"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_DESCR"]["implemented"] is True
    assert status["PyArray_DESCR"]["slot"] == 10
    assert status["PyArray_DESCR"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_DIM"]["implemented"] is True
    assert status["PyArray_DIM"]["slot"] == 7
    assert status["PyArray_DIM"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_BYTES"]["implemented"] is True
    assert status["PyArray_BYTES"]["slot"] == 9
    assert status["PyArray_BYTES"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_TYPE"]["implemented"] is True
    assert status["PyArray_TYPE"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_NBYTES"]["implemented"] is True
    assert status["PyArray_NBYTES"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_FILLWBYTE"]["implemented"] is True
    assert status["PyArray_FILLWBYTE"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_EquivByteorders"]["implemented"] is True
    assert status["PyArray_EquivByteorders"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_SHAPE"]["implemented"] is True
    assert status["PyArray_SHAPE"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_FLAGS"]["implemented"] is True
    assert status["PyArray_FLAGS"]["slot"] == 17
    assert status["PyArray_FLAGS"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_GETITEM"]["implemented"] is True
    assert status["PyArray_GETITEM"]["slot"] == 11
    assert status["PyArray_GETITEM"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_SETITEM"]["implemented"] is True
    assert status["PyArray_SETITEM"]["slot"] == 12
    assert status["PyArray_SETITEM"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_SIZE"]["implemented"] is True
    assert status["PyArray_SIZE"]["slot"] == 13
    assert status["PyArray_SIZE"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_ITEMSIZE"]["implemented"] is True
    assert status["PyArray_ITEMSIZE"]["slot"] == 14
    assert status["PyArray_ITEMSIZE"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_CompareLists"]["implemented"] is True
    assert status["PyArray_CompareLists"]["slot"] == 18
    assert status["PyArray_CompareLists"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_Empty"]["implemented"] is True
    assert status["PyArray_Empty"]["slot"] == 19
    assert status["PyArray_Empty"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_Zeros"]["implemented"] is True
    assert status["PyArray_Zeros"]["slot"] == 20
    assert status["PyArray_Zeros"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_EMPTY"]["implemented"] is True
    assert status["PyArray_EMPTY"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_ZEROS"]["implemented"] is True
    assert status["PyArray_ZEROS"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_EquivTypes"]["implemented"] is True
    assert status["PyArray_EquivTypes"]["slot"] == 21
    assert status["PyArray_EquivTypes"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_EquivArrTypes"]["implemented"] is True
    assert status["PyArray_EquivArrTypes"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_NewFromDescr"]["implemented"] is True
    assert status["PyArray_NewFromDescr"]["slot"] == 22
    assert status["PyArray_NewFromDescr"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_SimpleNewFromDescr"]["implemented"] is True
    assert (
        status["PyArray_SimpleNewFromDescr"]["failure_mode"]
        == "implemented_header_macro"
    )
    assert status["PyArray_BASE"]["implemented"] is True
    assert status["PyArray_BASE"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_SetBaseObject"]["implemented"] is True
    assert status["PyArray_SetBaseObject"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_Return"]["implemented"] is True
    assert status["PyArray_Return"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_ENABLEFLAGS"]["implemented"] is True
    assert status["PyArray_ENABLEFLAGS"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_CLEARFLAGS"]["implemented"] is True
    assert status["PyArray_CLEARFLAGS"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_UpdateFlags"]["implemented"] is True
    assert status["PyArray_UpdateFlags"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_CopyInto"]["implemented"] is True
    assert status["PyArray_CopyInto"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_CopyAnyInto"]["implemented"] is True
    assert status["PyArray_CopyAnyInto"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_ToScalar"]["implemented"] is True
    assert status["PyArray_ToScalar"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_Copy"]["implemented"] is True
    assert status["PyArray_Copy"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_EnsureArray"]["implemented"] is True
    assert status["PyArray_EnsureArray"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_EnsureAnyArray"]["implemented"] is True
    assert (
        status["PyArray_EnsureAnyArray"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_SAMESHAPE"]["implemented"] is True
    assert status["PyArray_SAMESHAPE"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_ISCONTIGUOUS"]["implemented"] is True
    assert status["PyArray_ISCONTIGUOUS"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_ISONESEGMENT"]["implemented"] is True
    assert status["PyArray_ISONESEGMENT"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_ISNBO"]["implemented"] is True
    assert status["PyArray_ISNBO"]["failure_mode"] == "implemented_header_macro"
    assert status["PyDataType_ISBYTESWAPPED"]["implemented"] is True
    assert status["PyDataType_ISBYTESWAPPED"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_SAFEALIGNEDCOPY"]["implemented"] is True
    assert status["PyArray_SAFEALIGNEDCOPY"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_FROMANY"]["implemented"] is True
    assert status["PyArray_FROMANY"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_FROM_OF"]["implemented"] is True
    assert status["PyArray_FROM_OF"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_FromObject"]["implemented"] is True
    assert status["PyArray_FromObject"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_CopyFromObject"]["implemented"] is True
    assert status["PyArray_CopyFromObject"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_STRIDE"]["implemented"] is True
    assert status["PyArray_STRIDE"]["slot"] is None
    assert status["PyArray_STRIDE"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_GETPTR2"]["implemented"] is True
    assert status["PyArray_GETPTR2"]["slot"] is None
    assert status["PyArray_GETPTR2"]["failure_mode"] == "implemented_header_macro"
    assert status["PyUFunc_FromFuncAndData"]["implemented"] is True
    assert status["PyUFunc_FromFuncAndData"]["slot"] == 0
    assert (
        status["PyUFunc_FromFuncAndData"]["failure_mode"]
        == "implemented_provider_slot"
    )


def test_pcc1_ext_abi_no_host_matches_host_capability_flag_plan():
    pcc1 = _find_current_pcc1()
    if pcc1 is None:
        skip_or_fail_no_current_pcc1("no current pcc1 binary with native ext-abi shim")
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    proc = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "ext-abi",
            "--provider",
            "combined",
            "--require-capsule",
            "--require-buffer",
            "--require-memoryview",
            "--require-numpy-capi",
            "--json",
        ],
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    assert proc.returncode == 0
    pcc1_plan = json.loads(proc.stdout)
    host_plan = extension_abi_plan(
        provider="combined",
        require_capsule=True,
        require_buffer=True,
        require_memoryview=True,
        require_numpy_capi=True,
    )
    assert pcc1_plan["provider"] == host_plan["provider"]
    assert pcc1_plan["required_symbols"] == host_plan["required_symbols"]
    assert pcc1_plan["missing_symbols"] == host_plan["missing_symbols"]
    assert pcc1_plan["unknown_symbols"] == host_plan["unknown_symbols"]
    assert pcc1_plan["numpy_capi_status"] == host_plan["numpy_capi_status"]


def test_pcc_package_ext_abi_cli_reports_supplied_headers(tmp_path):
    include = tmp_path / "include"
    include.mkdir()
    (include / "Python.h").write_text("int Py_Initialize(void);\n", encoding="utf-8")
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    proc = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "ext-abi",
            "--symbol",
            "Py_Initialize",
            "--include-dir",
            str(include),
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    plan = json.loads(proc.stdout)
    assert plan["ok"] is True
    assert plan["header_manifest"]["provided_headers"] == ["Python.h"]


def test_pcc1_ext_abi_does_not_need_host_python():
    pcc1 = _find_current_pcc1()
    if pcc1 is None:
        skip_or_fail_no_current_pcc1("no current pcc1 binary with native ext-abi shim")
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    proc = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "ext-abi",
            "--symbol",
            "Py_Initialize",
            "--symbol",
            "Py_INCREF",
            "--symbol",
            "Py_DECREF",
            "--symbol",
            "Py_XINCREF",
            "--symbol",
            "Py_XDECREF",
            "--symbol",
            "Py_NewRef",
            "--symbol",
            "Py_XNewRef",
            "--symbol",
            "Py_CLEAR",
            "--symbol",
            "Py_SETREF",
            "--symbol",
            "Py_XSETREF",
            *[
                part
                for symbol in GENERIC_REFCNT_MACRO_SYMBOLS
                for part in ("--symbol", symbol)
            ],
            "--symbol",
            "PyMem_Malloc",
            "--symbol",
            "PyMem_Calloc",
            "--symbol",
            "PyMem_Realloc",
            "--symbol",
            "PyMem_Free",
            *[
                part
                for symbol in GENERIC_MEMORY_OS_SYMBOLS
                for part in ("--symbol", symbol)
            ],
            *[
                part
                for symbol in GENERIC_SINGLETON_RETURN_SYMBOLS
                for part in ("--symbol", symbol)
            ],
            *[
                part
                for symbol in GENERIC_MAPPING_LONG_EXCEPTION_SYMBOLS
                for part in ("--symbol", symbol)
            ],
            *[
                part
                for symbol in GENERIC_CALL_TYPE_FORMAT_SYMBOLS
                for part in ("--symbol", symbol)
            ],
            *[
                part
                for symbol in GENERIC_UNICODE_MACRO_SYMBOLS
                for part in ("--symbol", symbol)
            ],
            *[
                part
                for symbol in GENERIC_SCALAR_COMPLEX_SYMBOLS
                for part in ("--symbol", symbol)
            ],
            *[
                part
                for symbol in GENERIC_GIL_STATE_SYMBOLS
                for part in ("--symbol", symbol)
            ],
            *[
                part
                for symbol in GENERIC_NUMBER_PROTOCOL_SYMBOLS
                for part in ("--symbol", symbol)
            ],
            *[
                part
                for symbol in GENERIC_ABSTRACT_PROTOCOL_SYMBOLS
                for part in ("--symbol", symbol)
            ],
            *[
                part
                for symbol in GENERIC_CONTAINER_MACRO_SYMBOLS
                for part in ("--symbol", symbol)
            ],
            *[
                part
                for symbol in GENERIC_CAPSULE_SYMBOLS
                for part in ("--symbol", symbol)
            ],
            "--symbol",
            "PyObject_Call",
            "--symbol",
            "PyObject_CallObject",
            "--symbol",
            "PyObject_CallFunctionObjArgs",
            "--require-capsule",
            "--symbol",
            "Py_BuildValue",
            "--symbol",
            "PyArg_ParseTupleAndKeywords",
            "--symbol",
            "PyTuple_New",
            "--symbol",
            "PyTuple_SetItem",
            "--symbol",
            "PyTuple_Pack",
            "--symbol",
            "PyDict_SetItemString",
            "--symbol",
            "PyDict_DelItem",
            "--symbol",
            "PyDict_DelItemString",
            "--symbol",
            "PyDict_Size",
            "--symbol",
            "PyDict_Contains",
            "--symbol",
            "PyDict_ContainsString",
            "--symbol",
            "PyObject_GetAttrString",
            "--symbol",
            "PyObject_GetAttr",
            "--symbol",
            "PyObject_GetOptionalAttr",
            "--symbol",
            "PyObject_GetOptionalAttrString",
            "--symbol",
            "PyObject_SetAttrString",
            "--symbol",
            "PyObject_SetAttr",
            "--symbol",
            "PyObject_HasAttr",
            "--symbol",
            "PyObject_HasAttrString",
            "--symbol",
            "PyObject_HasAttrWithError",
            "--symbol",
            "PyObject_HasAttrStringWithError",
            "--symbol",
            "PyObject_Hash",
            "--symbol",
            "PyCallable_Check",
            "--symbol",
            "PyObject_Str",
            "--symbol",
            "PyObject_Repr",
            "--symbol",
            "PyObject_RichCompare",
            "--symbol",
            "PyObject_RichCompareBool",
            "--symbol",
            "PyModule_AddObjectRef",
            "--symbol",
            "PyModule_AddIntConstant",
            "--symbol",
            "PyModule_GetDict",
            "--symbol",
            "PyList_New",
            "--symbol",
            "PyList_Append",
            "--symbol",
            "PyList_Check",
            "--symbol",
            "PyTuple_Check",
            "--symbol",
            "PyDict_Check",
            "--symbol",
            "PyBytes_Check",
            "--symbol",
            "PyLong_FromUnsignedLong",
            "--symbol",
            "PyLong_FromUnsignedLongLong",
            "--symbol",
            "PyUnicode_Check",
            "--symbol",
            "PyUnicode_InternFromString",
            "--symbol",
            "PyUnicode_CompareWithASCIIString",
            "--symbol",
            "PySequence_Fast",
            "--symbol",
            "PySequence_Fast_ITEMS",
            "--symbol",
            "PySequence_Size",
            "--symbol",
            "PySequence_GetItem",
            "--symbol",
            "PyUnicode_FromStringAndSize",
            "--symbol",
            "PyBytes_FromString",
            "--symbol",
            "PyBytes_FromStringAndSize",
            "--symbol",
            "PyBytes_AsStringAndSize",
            "--symbol",
            "PyFloat_FromDouble",
            "--symbol",
            "PyBool_FromLong",
            "--symbol",
            "PyObject_IsTrue",
            "--symbol",
            "PyErr_Format",
            "--symbol",
            "PyErr_NoMemory",
            "--symbol",
            "PyErr_NewException",
            "--symbol",
            "PyImport_ImportModule",
            "--require-buffer",
            "--require-memoryview",
            "--require-numpy-capi",
            "--provider",
            "array-api",
            "--expected-abi",
            "3",
            "--actual-abi",
            "2",
            "--json",
        ],
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    assert proc.returncode == 2
    plan = json.loads(proc.stdout)
    assert plan["provider"] == "array-api"
    assert "Py_INCREF" not in plan["missing_symbols"]
    assert "Py_DECREF" not in plan["missing_symbols"]
    assert "Py_XINCREF" not in plan["missing_symbols"]
    assert "Py_XDECREF" not in plan["missing_symbols"]
    assert "Py_NewRef" not in plan["missing_symbols"]
    assert "Py_XNewRef" not in plan["missing_symbols"]
    assert "Py_CLEAR" not in plan["missing_symbols"]
    assert "Py_SETREF" not in plan["missing_symbols"]
    assert "Py_XSETREF" not in plan["missing_symbols"]
    for symbol in GENERIC_REFCNT_MACRO_SYMBOLS:
        assert symbol not in plan["missing_symbols"]
    assert "PyMem_Malloc" not in plan["missing_symbols"]
    assert "PyMem_Calloc" not in plan["missing_symbols"]
    assert "PyMem_Realloc" not in plan["missing_symbols"]
    assert "PyMem_Free" not in plan["missing_symbols"]
    for symbol in GENERIC_MEMORY_OS_SYMBOLS:
        assert symbol not in plan["missing_symbols"]
    for symbol in GENERIC_SINGLETON_RETURN_SYMBOLS:
        assert symbol not in plan["missing_symbols"]
    for symbol in GENERIC_MAPPING_LONG_EXCEPTION_SYMBOLS:
        assert symbol not in plan["missing_symbols"]
    for symbol in GENERIC_CALL_TYPE_FORMAT_SYMBOLS:
        assert symbol not in plan["missing_symbols"]
    for symbol in GENERIC_UNICODE_MACRO_SYMBOLS:
        assert symbol not in plan["missing_symbols"]
    for symbol in GENERIC_SCALAR_COMPLEX_SYMBOLS:
        assert symbol not in plan["missing_symbols"]
    for symbol in GENERIC_GIL_STATE_SYMBOLS:
        assert symbol not in plan["missing_symbols"]
    for symbol in GENERIC_NUMBER_PROTOCOL_SYMBOLS:
        assert symbol not in plan["missing_symbols"]
    for symbol in GENERIC_ABSTRACT_PROTOCOL_SYMBOLS:
        assert symbol not in plan["missing_symbols"]
    for symbol in GENERIC_CONTAINER_MACRO_SYMBOLS:
        assert symbol not in plan["missing_symbols"]
    for symbol in GENERIC_CAPSULE_SYMBOLS:
        assert symbol not in plan["missing_symbols"]
    assert "PyObject_Call" not in plan["missing_symbols"]
    assert "PyObject_CallObject" not in plan["missing_symbols"]
    assert "PyObject_CallFunctionObjArgs" not in plan["missing_symbols"]
    assert "Py_BuildValue" not in plan["missing_symbols"]
    assert "PyArg_ParseTupleAndKeywords" not in plan["missing_symbols"]
    assert "PyTuple_New" not in plan["missing_symbols"]
    assert "PyTuple_SetItem" not in plan["missing_symbols"]
    assert "PyTuple_Pack" not in plan["missing_symbols"]
    assert "PyDict_SetItemString" not in plan["missing_symbols"]
    assert "PyDict_DelItem" not in plan["missing_symbols"]
    assert "PyDict_DelItemString" not in plan["missing_symbols"]
    assert "PyDict_Size" not in plan["missing_symbols"]
    assert "PyDict_Contains" not in plan["missing_symbols"]
    assert "PyDict_ContainsString" not in plan["missing_symbols"]
    assert "PyObject_GetAttrString" not in plan["missing_symbols"]
    assert "PyObject_GetAttr" not in plan["missing_symbols"]
    assert "PyObject_GetOptionalAttr" not in plan["missing_symbols"]
    assert "PyObject_GetOptionalAttrString" not in plan["missing_symbols"]
    assert "PyObject_SetAttrString" not in plan["missing_symbols"]
    assert "PyObject_SetAttr" not in plan["missing_symbols"]
    assert "PyObject_HasAttr" not in plan["missing_symbols"]
    assert "PyObject_HasAttrString" not in plan["missing_symbols"]
    assert "PyObject_HasAttrWithError" not in plan["missing_symbols"]
    assert "PyObject_HasAttrStringWithError" not in plan["missing_symbols"]
    assert "PyObject_Hash" not in plan["missing_symbols"]
    assert "PyCallable_Check" not in plan["missing_symbols"]
    assert "PyObject_Str" not in plan["missing_symbols"]
    assert "PyObject_Repr" not in plan["missing_symbols"]
    assert "PyObject_RichCompare" not in plan["missing_symbols"]
    assert "PyObject_RichCompareBool" not in plan["missing_symbols"]
    assert "PyModule_AddObjectRef" not in plan["missing_symbols"]
    assert "PyModule_AddIntConstant" not in plan["missing_symbols"]
    assert "PyModule_GetDict" not in plan["missing_symbols"]
    assert "PyList_New" not in plan["missing_symbols"]
    assert "PyList_Append" not in plan["missing_symbols"]
    assert "PyList_Check" not in plan["missing_symbols"]
    assert "PyTuple_Check" not in plan["missing_symbols"]
    assert "PyDict_Check" not in plan["missing_symbols"]
    assert "PyBytes_Check" not in plan["missing_symbols"]
    assert "PyLong_FromUnsignedLong" not in plan["missing_symbols"]
    assert "PyLong_FromUnsignedLongLong" not in plan["missing_symbols"]
    assert "PyUnicode_Check" not in plan["missing_symbols"]
    assert "PyUnicode_InternFromString" not in plan["missing_symbols"]
    assert "PyUnicode_CompareWithASCIIString" not in plan["missing_symbols"]
    assert "PySequence_Fast" not in plan["missing_symbols"]
    assert "PySequence_Fast_ITEMS" not in plan["missing_symbols"]
    assert "PySequence_Size" not in plan["missing_symbols"]
    assert "PySequence_GetItem" not in plan["missing_symbols"]
    assert "PyUnicode_FromStringAndSize" not in plan["missing_symbols"]
    assert "PyBytes_FromString" not in plan["missing_symbols"]
    assert "PyBytes_FromStringAndSize" not in plan["missing_symbols"]
    assert "PyBytes_AsStringAndSize" not in plan["missing_symbols"]
    assert "PyFloat_FromDouble" not in plan["missing_symbols"]
    assert "PyBool_FromLong" not in plan["missing_symbols"]
    assert "PyObject_IsTrue" not in plan["missing_symbols"]
    assert "PyErr_Format" not in plan["missing_symbols"]
    assert "PyErr_NoMemory" not in plan["missing_symbols"]
    assert "PyErr_NewException" not in plan["missing_symbols"]
    assert "PyImport_ImportModule" not in plan["missing_symbols"]
    assert "PyObject_GetBuffer" not in plan["missing_symbols"]
    assert "PyObject_CheckBuffer" not in plan["missing_symbols"]
    assert "PyBuffer_Release" not in plan["missing_symbols"]
    for symbol in GENERIC_MEMORYVIEW_SYMBOLS:
        assert symbol not in plan["missing_symbols"]
    assert "PyArray_API" not in plan["missing_symbols"]
    assert "PyArray_Type" not in plan["missing_symbols"]
    assert "PyArrayDescr_Type" not in plan["missing_symbols"]
    assert "PyArray_DescrCheck" not in plan["missing_symbols"]
    assert "PyArray_DescrFromType" not in plan["missing_symbols"]
    assert "PyArray_DescrNewFromType" not in plan["missing_symbols"]
    assert "PyArray_DescrNew" not in plan["missing_symbols"]
    assert "PyArray_DescrNewByteorder" not in plan["missing_symbols"]
    assert "PyArray_CanCastSafely" not in plan["missing_symbols"]
    assert "PyArray_CanCastTo" not in plan["missing_symbols"]
    assert "PyArray_Zero" not in plan["missing_symbols"]
    assert "PyArray_One" not in plan["missing_symbols"]
    assert "PyArray_TypeObjectFromType" not in plan["missing_symbols"]
    assert "PyArray_ObjectType" not in plan["missing_symbols"]
    assert "PyArray_DescrFromObject" not in plan["missing_symbols"]
    assert "PyArray_Size" not in plan["missing_symbols"]
    assert "PyArray_DescrFromScalar" not in plan["missing_symbols"]
    assert "PyArray_DescrFromTypeObject" not in plan["missing_symbols"]
    assert "PyArray_ScalarAsCtype" not in plan["missing_symbols"]
    assert "PyArray_FromScalar" not in plan["missing_symbols"]
    assert "PyArray_CastScalarToCtype" not in plan["missing_symbols"]
    assert "PyArray_CastScalarDirect" not in plan["missing_symbols"]
    assert "PyArray_Pack" not in plan["missing_symbols"]
    assert "PyArray_CastToType" not in plan["missing_symbols"]
    assert "PyArray_Cast" not in plan["missing_symbols"]
    assert "PyArray_FillWithScalar" not in plan["missing_symbols"]
    assert "PyArray_ToList" not in plan["missing_symbols"]
    assert "PyArray_ToString" not in plan["missing_symbols"]
    assert "PyArray_Byteswap" not in plan["missing_symbols"]
    assert "PyArray_FromAny" not in plan["missing_symbols"]
    assert "PyArray_SimpleNew" not in plan["missing_symbols"]
    assert "PyArray_SimpleNewFromData" not in plan["missing_symbols"]
    assert "PyArray_GETITEM" not in plan["missing_symbols"]
    assert "PyArray_SETITEM" not in plan["missing_symbols"]
    assert "PyArray_NDIM" not in plan["missing_symbols"]
    assert "PyArray_DIMS" not in plan["missing_symbols"]
    assert "PyArray_STRIDES" not in plan["missing_symbols"]
    assert "PyArray_DATA" not in plan["missing_symbols"]
    assert "PyArray_DESCR" not in plan["missing_symbols"]
    assert "PyArray_SIZE" not in plan["missing_symbols"]
    assert "PyArray_ITEMSIZE" not in plan["missing_symbols"]
    assert "PyArray_DIM" not in plan["missing_symbols"]
    assert "PyArray_BYTES" not in plan["missing_symbols"]
    assert "PyArray_Check" not in plan["missing_symbols"]
    assert "PyArray_CheckExact" not in plan["missing_symbols"]
    assert "PyArray_CheckFromAny" not in plan["missing_symbols"]
    assert "PyArray_FromArray" not in plan["missing_symbols"]
    assert "PyArray_MultiplyList" not in plan["missing_symbols"]
    assert "PyArray_MultiplyIntList" not in plan["missing_symbols"]
    assert "PyArray_GetPtr" not in plan["missing_symbols"]
    assert "PyArray_ElementStrides" not in plan["missing_symbols"]
    assert "PyArray_ValidType" not in plan["missing_symbols"]
    assert "PyArray_Item_INCREF" not in plan["missing_symbols"]
    assert "PyArray_Item_XDECREF" not in plan["missing_symbols"]
    assert "PyArray_NewCopy" not in plan["missing_symbols"]
    assert "PyArray_INCREF" not in plan["missing_symbols"]
    assert "PyArray_XDECREF" not in plan["missing_symbols"]
    assert "PyUFunc_API" not in plan["missing_symbols"]
    assert "PyUFunc_FromFuncAndData" not in plan["missing_symbols"]
    assert plan["unknown_symbols"] == []
    assert "PCC-EXT-MISSING-NUMPY-CAPI-SYMBOL" not in {
        diag["code"] for diag in plan["diagnostics"]
    }
    status = {row["symbol"]: row for row in plan["numpy_capi_status"]}
    assert status["PyArray_DescrCheck"]["implemented"] is True
    assert status["PyArray_DescrCheck"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_DescrNewFromType"]["implemented"] is True
    assert (
        status["PyArray_DescrNewFromType"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_DescrNew"]["implemented"] is True
    assert (
        status["PyArray_DescrNew"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_DescrNewByteorder"]["implemented"] is True
    assert (
        status["PyArray_DescrNewByteorder"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_CanCastSafely"]["implemented"] is True
    assert (
        status["PyArray_CanCastSafely"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_CanCastTo"]["implemented"] is True
    assert status["PyArray_CanCastTo"]["slot"] == 52
    assert (
        status["PyArray_CanCastTo"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_Zero"]["implemented"] is True
    assert status["PyArray_Zero"]["slot"] == 53
    assert (
        status["PyArray_Zero"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_One"]["implemented"] is True
    assert status["PyArray_One"]["slot"] == 54
    assert (
        status["PyArray_One"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_TypeObjectFromType"]["implemented"] is True
    assert status["PyArray_TypeObjectFromType"]["slot"] == 55
    assert (
        status["PyArray_TypeObjectFromType"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_ObjectType"]["implemented"] is True
    assert (
        status["PyArray_ObjectType"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_DescrFromObject"]["implemented"] is True
    assert status["PyArray_DescrFromObject"]["slot"] == 56
    assert (
        status["PyArray_DescrFromObject"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_Size"]["implemented"] is True
    assert status["PyArray_Size"]["slot"] == 57
    assert (
        status["PyArray_Size"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_DescrFromScalar"]["implemented"] is True
    assert status["PyArray_DescrFromScalar"]["slot"] == 58
    assert (
        status["PyArray_DescrFromScalar"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_DescrFromTypeObject"]["implemented"] is True
    assert status["PyArray_DescrFromTypeObject"]["slot"] == 59
    assert (
        status["PyArray_DescrFromTypeObject"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_ScalarAsCtype"]["implemented"] is True
    assert status["PyArray_ScalarAsCtype"]["slot"] == 60
    assert (
        status["PyArray_ScalarAsCtype"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_FromScalar"]["implemented"] is True
    assert status["PyArray_FromScalar"]["slot"] == 61
    assert (
        status["PyArray_FromScalar"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_CastScalarToCtype"]["implemented"] is True
    assert status["PyArray_CastScalarToCtype"]["slot"] == 62
    assert (
        status["PyArray_CastScalarToCtype"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_CastScalarDirect"]["implemented"] is True
    assert status["PyArray_CastScalarDirect"]["slot"] == 64
    assert (
        status["PyArray_CastScalarDirect"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_Pack"]["implemented"] is True
    assert status["PyArray_Pack"]["slot"] == 63
    assert (
        status["PyArray_Pack"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_CastToType"]["implemented"] is True
    assert status["PyArray_CastToType"]["slot"] == 65
    assert (
        status["PyArray_CastToType"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_Cast"]["implemented"] is True
    assert status["PyArray_Cast"]["slot"] is None
    assert status["PyArray_Cast"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_FillWithScalar"]["implemented"] is True
    assert status["PyArray_FillWithScalar"]["slot"] == 66
    assert (
        status["PyArray_FillWithScalar"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_ToList"]["implemented"] is True
    assert status["PyArray_ToList"]["slot"] == 67
    assert (
        status["PyArray_ToList"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_ToString"]["implemented"] is True
    assert status["PyArray_ToString"]["slot"] == 68
    assert (
        status["PyArray_ToString"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_Byteswap"]["implemented"] is True
    assert status["PyArray_Byteswap"]["slot"] == 69
    assert (
        status["PyArray_Byteswap"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_DescrFromType"]["implemented"] is True
    assert status["PyArray_DescrFromType"]["slot"] == 2
    assert (
        status["PyArray_DescrFromType"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_FromAny"]["implemented"] is True
    assert status["PyArray_FromAny"]["slot"] == 3
    assert status["PyArray_FromAny"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_CheckFromAny"]["implemented"] is True
    assert status["PyArray_CheckFromAny"]["slot"] == 40
    assert (
        status["PyArray_CheckFromAny"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_FromArray"]["implemented"] is True
    assert status["PyArray_FromArray"]["slot"] == 41
    assert (
        status["PyArray_FromArray"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_MultiplyList"]["implemented"] is True
    assert status["PyArray_MultiplyList"]["slot"] == 42
    assert (
        status["PyArray_MultiplyList"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_MultiplyIntList"]["implemented"] is True
    assert status["PyArray_MultiplyIntList"]["slot"] == 43
    assert (
        status["PyArray_MultiplyIntList"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_GetPtr"]["implemented"] is True
    assert status["PyArray_GetPtr"]["slot"] == 44
    assert status["PyArray_GetPtr"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_ElementStrides"]["implemented"] is True
    assert status["PyArray_ElementStrides"]["slot"] == 45
    assert (
        status["PyArray_ElementStrides"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_ValidType"]["implemented"] is True
    assert status["PyArray_ValidType"]["slot"] == 46
    assert (
        status["PyArray_ValidType"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_Item_INCREF"]["implemented"] is True
    assert status["PyArray_Item_INCREF"]["slot"] == 47
    assert (
        status["PyArray_Item_INCREF"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_Item_XDECREF"]["implemented"] is True
    assert status["PyArray_Item_XDECREF"]["slot"] == 48
    assert (
        status["PyArray_Item_XDECREF"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_NewCopy"]["implemented"] is True
    assert status["PyArray_NewCopy"]["slot"] == 49
    assert (
        status["PyArray_NewCopy"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_INCREF"]["implemented"] is True
    assert status["PyArray_INCREF"]["slot"] == 50
    assert (
        status["PyArray_INCREF"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_XDECREF"]["implemented"] is True
    assert status["PyArray_XDECREF"]["slot"] == 51
    assert (
        status["PyArray_XDECREF"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_SimpleNew"]["implemented"] is True
    assert status["PyArray_SimpleNew"]["slot"] == 4
    assert status["PyArray_SimpleNew"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_SimpleNewFromData"]["implemented"] is True
    assert status["PyArray_SimpleNewFromData"]["slot"] == 5
    assert (
        status["PyArray_SimpleNewFromData"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_SETITEM"]["slot"] == 12
    assert status["PyArray_GETITEM"]["implemented"] is True
    assert status["PyArray_GETITEM"]["slot"] == 11
    assert status["PyArray_GETITEM"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_SETITEM"]["implemented"] is True
    assert status["PyArray_SETITEM"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_NDIM"]["implemented"] is True
    assert status["PyArray_NDIM"]["slot"] == 6
    assert status["PyArray_NDIM"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_DIMS"]["implemented"] is True
    assert status["PyArray_DIMS"]["slot"] == 7
    assert status["PyArray_DIMS"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_STRIDES"]["implemented"] is True
    assert status["PyArray_STRIDES"]["slot"] == 8
    assert status["PyArray_STRIDES"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_DATA"]["implemented"] is True
    assert status["PyArray_DATA"]["slot"] == 9
    assert status["PyArray_DATA"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_DESCR"]["implemented"] is True
    assert status["PyArray_DESCR"]["slot"] == 10
    assert status["PyArray_DESCR"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_DIM"]["implemented"] is True
    assert status["PyArray_DIM"]["slot"] == 7
    assert status["PyArray_DIM"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_BYTES"]["implemented"] is True
    assert status["PyArray_BYTES"]["slot"] == 9
    assert status["PyArray_BYTES"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_SIZE"]["implemented"] is True
    assert status["PyArray_SIZE"]["slot"] == 13
    assert status["PyArray_SIZE"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_ITEMSIZE"]["implemented"] is True
    assert status["PyArray_ITEMSIZE"]["slot"] == 14
    assert status["PyArray_ITEMSIZE"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_Check"]["implemented"] is True
    assert status["PyArray_Check"]["slot"] == 15
    assert status["PyArray_Check"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_CheckExact"]["implemented"] is True
    assert status["PyArray_CheckExact"]["slot"] == 16
    assert status["PyArray_CheckExact"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyUFunc_FromFuncAndData"]["implemented"] is True
    assert status["PyUFunc_FromFuncAndData"]["failure_mode"] == "implemented_provider_slot"
    assert plan["abi_version"]["code"] == "PCC-EXT-ABI-VERSION-MISMATCH"


def test_pcc1_ext_abi_scans_include_dir_without_host_python(tmp_path):
    pcc1 = _find_current_pcc1()
    if pcc1 is None:
        skip_or_fail_no_current_pcc1("no current pcc1 binary with native ext-abi shim")
    include = tmp_path / "include"
    include.mkdir()
    (include / "Python.h").write_text("int Py_Initialize(void);\n", encoding="utf-8")
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    proc = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "ext-abi",
            "--symbol",
            "Py_Initialize",
            "--include-dir",
            str(include),
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    plan = json.loads(proc.stdout)
    assert plan["ok"] is True
    assert plan["header_manifest"]["provided_headers"] == ["Python.h"]


def test_pcc1_ext_abi_no_host_reports_generic_plan_ok():
    pcc1 = _find_current_pcc1()
    if pcc1 is None:
        skip_or_fail_no_current_pcc1("no current pcc1 binary with native ext-abi shim")
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    proc = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "ext-abi",
            "--symbol",
            "Py_Initialize",
            "--symbol",
            "PyObject_GetAttrString",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    plan = json.loads(proc.stdout)
    assert plan["ok"] is True
    assert plan["missing_symbols"] == []
    assert plan["unknown_symbols"] == []
    assert plan["provider"] == "extension"


def test_pcc1_ext_abi_no_host_require_capsule_includes_full_surface():
    pcc1 = _find_current_pcc1()
    if pcc1 is None:
        skip_or_fail_no_current_pcc1("no current pcc1 binary with native ext-abi shim")
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    proc = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "ext-abi",
            "--require-capsule",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    plan = json.loads(proc.stdout)
    assert plan["ok"] is True
    for symbol in GENERIC_CAPSULE_SYMBOLS:
        assert symbol in plan["required_symbols"]
        assert symbol not in plan["missing_symbols"]
    assert plan["unknown_symbols"] == []


def test_pcc1_ext_abi_no_host_reports_numpy_capi_provider_subset_ok():
    pcc1 = _find_current_pcc1()
    if pcc1 is None:
        skip_or_fail_no_current_pcc1("no current pcc1 binary with native ext-abi shim")
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    proc = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "ext-abi",
            "--require-numpy-capi",
            "--provider",
            "numpy-capi",
            "--json",
        ],
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    assert proc.returncode == 0
    plan = json.loads(proc.stdout)
    assert plan["provider"] == "numpy-capi"
    assert "PyArray_API" not in plan["missing_symbols"]
    assert "PyArray_Type" not in plan["missing_symbols"]
    assert "PyArrayDescr_Type" not in plan["missing_symbols"]
    assert "PyArray_DescrCheck" not in plan["missing_symbols"]
    assert "PyUFunc_API" not in plan["missing_symbols"]
    assert "PyUFunc_FromFuncAndData" not in plan["missing_symbols"]
    assert "PyArray_DescrCheck" in plan["required_symbols"]
    assert "PyArray_DescrNewFromType" in plan["required_symbols"]
    assert "PyArray_DescrNew" in plan["required_symbols"]
    assert "PyArray_DescrNewByteorder" in plan["required_symbols"]
    assert "PyArray_CanCastSafely" in plan["required_symbols"]
    assert "PyArray_CanCastTo" in plan["required_symbols"]
    assert "PyArray_Zero" in plan["required_symbols"]
    assert "PyArray_One" in plan["required_symbols"]
    assert "PyArray_TypeObjectFromType" in plan["required_symbols"]
    assert "PyArray_ObjectType" in plan["required_symbols"]
    assert "PyArray_DescrFromObject" in plan["required_symbols"]
    assert "PyArray_Size" in plan["required_symbols"]
    assert "PyArray_DescrFromScalar" in plan["required_symbols"]
    assert "PyArray_DescrFromTypeObject" in plan["required_symbols"]
    assert "PyArray_ScalarAsCtype" in plan["required_symbols"]
    assert "PyArray_FromScalar" in plan["required_symbols"]
    assert "PyArray_CastScalarToCtype" in plan["required_symbols"]
    assert "PyArray_CastScalarDirect" in plan["required_symbols"]
    assert "PyArray_Pack" in plan["required_symbols"]
    assert "PyArray_CastToType" in plan["required_symbols"]
    assert "PyArray_Cast" in plan["required_symbols"]
    assert "PyArray_FillWithScalar" in plan["required_symbols"]
    assert "PyArray_ToList" in plan["required_symbols"]
    assert "PyArray_ToString" in plan["required_symbols"]
    assert "PyArray_Byteswap" in plan["required_symbols"]
    assert "PyArray_FromString" in plan["required_symbols"]
    assert "PyArray_FromBuffer" in plan["required_symbols"]
    assert "PyArray_CheckFromAny" in plan["required_symbols"]
    assert "PyArray_FromArray" in plan["required_symbols"]
    assert "PyArray_MultiplyList" in plan["required_symbols"]
    assert "PyArray_MultiplyIntList" in plan["required_symbols"]
    assert "PyArray_GetPtr" in plan["required_symbols"]
    assert "PyArray_ElementStrides" in plan["required_symbols"]
    assert "PyArray_ValidType" in plan["required_symbols"]
    assert "PyArray_Item_INCREF" in plan["required_symbols"]
    assert "PyArray_Item_XDECREF" in plan["required_symbols"]
    assert "PyArray_NewCopy" in plan["required_symbols"]
    assert "PyArray_INCREF" in plan["required_symbols"]
    assert "PyArray_XDECREF" in plan["required_symbols"]
    assert "PyArray_DTYPE" in plan["required_symbols"]
    assert "PyDataType_TYPE" in plan["required_symbols"]
    assert "PyArray_TYPE" in plan["required_symbols"]
    assert "PyArray_NBYTES" in plan["required_symbols"]
    assert "PyArray_FILLWBYTE" in plan["required_symbols"]
    assert "PyArray_EquivByteorders" in plan["required_symbols"]
    assert "PyArray_SHAPE" in plan["required_symbols"]
    assert "PyArray_FLAGS" in plan["required_symbols"]
    assert "PyArray_CompareLists" in plan["required_symbols"]
    assert "PyArray_Empty" in plan["required_symbols"]
    assert "PyArray_Zeros" in plan["required_symbols"]
    assert "PyArray_EMPTY" in plan["required_symbols"]
    assert "PyArray_ZEROS" in plan["required_symbols"]
    assert "PyArray_EquivTypes" in plan["required_symbols"]
    assert "PyArray_EquivArrTypes" in plan["required_symbols"]
    assert "PyArray_NewFromDescr" in plan["required_symbols"]
    assert "PyArray_SimpleNewFromDescr" in plan["required_symbols"]
    assert "PyArray_BASE" in plan["required_symbols"]
    assert "PyArray_SetBaseObject" in plan["required_symbols"]
    assert "PyArray_Return" in plan["required_symbols"]
    assert "PyArray_ENABLEFLAGS" in plan["required_symbols"]
    assert "PyArray_CLEARFLAGS" in plan["required_symbols"]
    assert "PyArray_UpdateFlags" in plan["required_symbols"]
    assert "PyArray_CopyInto" in plan["required_symbols"]
    assert "PyArray_CopyAnyInto" in plan["required_symbols"]
    assert "PyArray_ToScalar" in plan["required_symbols"]
    assert "PyArray_Copy" in plan["required_symbols"]
    assert "PyArray_EnsureArray" in plan["required_symbols"]
    assert "PyArray_EnsureAnyArray" in plan["required_symbols"]
    assert "PyArray_SAMESHAPE" in plan["required_symbols"]
    assert "PyArray_ISCONTIGUOUS" in plan["required_symbols"]
    assert "PyArray_ISONESEGMENT" in plan["required_symbols"]
    assert "PyArray_ISNBO" in plan["required_symbols"]
    assert "PyDataType_ISBYTESWAPPED" in plan["required_symbols"]
    assert "PyArray_SAFEALIGNEDCOPY" in plan["required_symbols"]
    assert "PyArray_FROMANY" in plan["required_symbols"]
    assert "PyArray_FROM_OF" in plan["required_symbols"]
    assert "PyArray_FromObject" in plan["required_symbols"]
    assert "PyArray_CopyFromObject" in plan["required_symbols"]
    assert "PyArray_DTYPE" not in plan["missing_symbols"]
    assert "PyDataType_TYPE" not in plan["missing_symbols"]
    assert "PyTypeNum_ISFLOAT" not in plan["missing_symbols"]
    assert "PyDataType_ISNUMBER" not in plan["missing_symbols"]
    assert "PyArray_ISOBJECT" not in plan["missing_symbols"]
    assert "PyArray_TYPE" not in plan["missing_symbols"]
    assert "PyArray_NBYTES" not in plan["missing_symbols"]
    assert "PyArray_FILLWBYTE" not in plan["missing_symbols"]
    assert "PyArray_EquivByteorders" not in plan["missing_symbols"]
    assert "PyArray_SHAPE" not in plan["missing_symbols"]
    assert "PyArray_FLAGS" not in plan["missing_symbols"]
    assert "PyArray_CompareLists" not in plan["missing_symbols"]
    assert "PyArray_Empty" not in plan["missing_symbols"]
    assert "PyArray_Zeros" not in plan["missing_symbols"]
    assert "PyArray_EMPTY" not in plan["missing_symbols"]
    assert "PyArray_ZEROS" not in plan["missing_symbols"]
    assert "PyArray_EquivTypes" not in plan["missing_symbols"]
    assert "PyArray_EquivArrTypes" not in plan["missing_symbols"]
    assert "PyArray_NewFromDescr" not in plan["missing_symbols"]
    assert "PyArray_SimpleNewFromDescr" not in plan["missing_symbols"]
    assert "PyArray_BASE" not in plan["missing_symbols"]
    assert "PyArray_SetBaseObject" not in plan["missing_symbols"]
    assert "PyArray_Return" not in plan["missing_symbols"]
    assert "PyArray_ENABLEFLAGS" not in plan["missing_symbols"]
    assert "PyArray_CLEARFLAGS" not in plan["missing_symbols"]
    assert "PyArray_UpdateFlags" not in plan["missing_symbols"]
    assert "PyArray_CopyInto" not in plan["missing_symbols"]
    assert "PyArray_CopyAnyInto" not in plan["missing_symbols"]
    assert "PyArray_ToScalar" not in plan["missing_symbols"]
    assert "PyArray_Copy" not in plan["missing_symbols"]
    assert "PyArray_EnsureArray" not in plan["missing_symbols"]
    assert "PyArray_EnsureAnyArray" not in plan["missing_symbols"]
    assert "PyArray_SAMESHAPE" not in plan["missing_symbols"]
    assert "PyArray_ISCONTIGUOUS" not in plan["missing_symbols"]
    assert "PyArray_ISONESEGMENT" not in plan["missing_symbols"]
    assert "PyArray_ISNBO" not in plan["missing_symbols"]
    assert "PyDataType_ISBYTESWAPPED" not in plan["missing_symbols"]
    assert "PyArray_SAFEALIGNEDCOPY" not in plan["missing_symbols"]
    assert "PyArray_FROMANY" not in plan["missing_symbols"]
    assert "PyArray_FromObject" not in plan["missing_symbols"]
    assert "PyArray_CopyFromObject" not in plan["missing_symbols"]
    assert "PyArray_FROM_OF" not in plan["missing_symbols"]
    assert "PyArray_Check" not in plan["missing_symbols"]
    assert "PyArray_CheckExact" not in plan["missing_symbols"]
    assert "PyArray_DescrFromObject" not in plan["missing_symbols"]
    assert "PyArray_Size" not in plan["missing_symbols"]
    assert "PyArray_DescrFromScalar" not in plan["missing_symbols"]
    assert "PyArray_DescrFromTypeObject" not in plan["missing_symbols"]
    assert "PyArray_ScalarAsCtype" not in plan["missing_symbols"]
    assert "PyArray_FromScalar" not in plan["missing_symbols"]
    assert "PyArray_CastScalarToCtype" not in plan["missing_symbols"]
    assert "PyArray_CastScalarDirect" not in plan["missing_symbols"]
    assert "PyArray_Pack" not in plan["missing_symbols"]
    assert "PyArray_CastToType" not in plan["missing_symbols"]
    assert "PyArray_Cast" not in plan["missing_symbols"]
    assert "PyArray_FillWithScalar" not in plan["missing_symbols"]
    assert "PyArray_ToList" not in plan["missing_symbols"]
    assert "PyArray_ToString" not in plan["missing_symbols"]
    assert "PyArray_Byteswap" not in plan["missing_symbols"]
    assert "PyArray_FromString" not in plan["missing_symbols"]
    assert "PyArray_FromBuffer" not in plan["missing_symbols"]
    assert "PyArray_CheckFromAny" not in plan["missing_symbols"]
    assert "PyArray_FromArray" not in plan["missing_symbols"]
    assert "PyArray_MultiplyList" not in plan["missing_symbols"]
    assert "PyArray_MultiplyIntList" not in plan["missing_symbols"]
    assert "PyArray_GetPtr" not in plan["missing_symbols"]
    assert "PyArray_ElementStrides" not in plan["missing_symbols"]
    assert "PyArray_ValidType" not in plan["missing_symbols"]
    assert "PyArray_Item_INCREF" not in plan["missing_symbols"]
    assert "PyArray_Item_XDECREF" not in plan["missing_symbols"]
    assert "PyArray_NewCopy" not in plan["missing_symbols"]
    assert "PyArray_INCREF" not in plan["missing_symbols"]
    assert "PyArray_XDECREF" not in plan["missing_symbols"]
    assert plan["diagnostics"] == []
    status = {row["symbol"]: row for row in plan["numpy_capi_status"]}
    assert status["PyArray_DTYPE"]["implemented"] is True
    assert status["PyArray_DTYPE"]["slot"] is None
    assert status["PyArray_DTYPE"]["failure_mode"] == "implemented_header_macro"
    assert status["PyDataType_TYPE"]["implemented"] is True
    assert status["PyDataType_TYPE"]["slot"] is None
    assert status["PyDataType_TYPE"]["failure_mode"] == "implemented_header_macro"
    assert status["PyTypeNum_ISFLOAT"]["implemented"] is True
    assert status["PyTypeNum_ISFLOAT"]["slot"] is None
    assert status["PyTypeNum_ISFLOAT"]["failure_mode"] == "implemented_header_macro"
    assert status["PyDataType_ISNUMBER"]["implemented"] is True
    assert status["PyDataType_ISNUMBER"]["slot"] is None
    assert status["PyDataType_ISNUMBER"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_ISOBJECT"]["implemented"] is True
    assert status["PyArray_ISOBJECT"]["slot"] is None
    assert status["PyArray_ISOBJECT"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_TYPE"]["implemented"] is True
    assert status["PyArray_TYPE"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_NBYTES"]["implemented"] is True
    assert status["PyArray_NBYTES"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_FILLWBYTE"]["implemented"] is True
    assert status["PyArray_FILLWBYTE"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_EquivByteorders"]["implemented"] is True
    assert status["PyArray_EquivByteorders"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_SHAPE"]["implemented"] is True
    assert status["PyArray_SHAPE"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_FLAGS"]["implemented"] is True
    assert status["PyArray_FLAGS"]["slot"] == 17
    assert status["PyArray_FLAGS"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_Check"]["implemented"] is True
    assert status["PyArray_Check"]["slot"] == 15
    assert status["PyArray_Check"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_CheckFromAny"]["implemented"] is True
    assert status["PyArray_CheckFromAny"]["slot"] == 40
    assert (
        status["PyArray_CheckFromAny"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_FromArray"]["implemented"] is True
    assert status["PyArray_FromArray"]["slot"] == 41
    assert (
        status["PyArray_FromArray"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_MultiplyList"]["implemented"] is True
    assert status["PyArray_MultiplyList"]["slot"] == 42
    assert (
        status["PyArray_MultiplyList"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_MultiplyIntList"]["implemented"] is True
    assert status["PyArray_MultiplyIntList"]["slot"] == 43
    assert (
        status["PyArray_MultiplyIntList"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_GetPtr"]["implemented"] is True
    assert status["PyArray_GetPtr"]["slot"] == 44
    assert status["PyArray_GetPtr"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_ElementStrides"]["implemented"] is True
    assert status["PyArray_ElementStrides"]["slot"] == 45
    assert (
        status["PyArray_ElementStrides"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_ValidType"]["implemented"] is True
    assert status["PyArray_ValidType"]["slot"] == 46
    assert (
        status["PyArray_ValidType"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_Item_INCREF"]["implemented"] is True
    assert status["PyArray_Item_INCREF"]["slot"] == 47
    assert (
        status["PyArray_Item_INCREF"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_Item_XDECREF"]["implemented"] is True
    assert status["PyArray_Item_XDECREF"]["slot"] == 48
    assert (
        status["PyArray_Item_XDECREF"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_NewCopy"]["implemented"] is True
    assert status["PyArray_NewCopy"]["slot"] == 49
    assert (
        status["PyArray_NewCopy"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_INCREF"]["implemented"] is True
    assert status["PyArray_INCREF"]["slot"] == 50
    assert (
        status["PyArray_INCREF"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_XDECREF"]["implemented"] is True
    assert status["PyArray_XDECREF"]["slot"] == 51
    assert (
        status["PyArray_XDECREF"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_CheckExact"]["implemented"] is True
    assert status["PyArray_CheckExact"]["slot"] == 16
    assert status["PyArray_CheckExact"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_CompareLists"]["implemented"] is True
    assert status["PyArray_CompareLists"]["slot"] == 18
    assert status["PyArray_CompareLists"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_Empty"]["implemented"] is True
    assert status["PyArray_Empty"]["slot"] == 19
    assert status["PyArray_Empty"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_Zeros"]["implemented"] is True
    assert status["PyArray_Zeros"]["slot"] == 20
    assert status["PyArray_Zeros"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_EMPTY"]["implemented"] is True
    assert status["PyArray_EMPTY"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_ZEROS"]["implemented"] is True
    assert status["PyArray_ZEROS"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_EquivTypes"]["implemented"] is True
    assert status["PyArray_EquivTypes"]["slot"] == 21
    assert status["PyArray_EquivTypes"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_EquivArrTypes"]["implemented"] is True
    assert status["PyArray_EquivArrTypes"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_NewFromDescr"]["implemented"] is True
    assert status["PyArray_NewFromDescr"]["slot"] == 22
    assert status["PyArray_NewFromDescr"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_SimpleNewFromDescr"]["implemented"] is True
    assert (
        status["PyArray_SimpleNewFromDescr"]["failure_mode"]
        == "implemented_header_macro"
    )
    assert status["PyArray_BASE"]["implemented"] is True
    assert status["PyArray_BASE"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_SetBaseObject"]["implemented"] is True
    assert status["PyArray_SetBaseObject"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_Return"]["implemented"] is True
    assert status["PyArray_Return"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_ENABLEFLAGS"]["implemented"] is True
    assert status["PyArray_ENABLEFLAGS"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_CLEARFLAGS"]["implemented"] is True
    assert status["PyArray_CLEARFLAGS"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_UpdateFlags"]["implemented"] is True
    assert status["PyArray_UpdateFlags"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_CopyInto"]["implemented"] is True
    assert status["PyArray_CopyInto"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_CopyAnyInto"]["implemented"] is True
    assert status["PyArray_CopyAnyInto"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_ToScalar"]["implemented"] is True
    assert status["PyArray_ToScalar"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_Copy"]["implemented"] is True
    assert status["PyArray_Copy"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_EnsureArray"]["implemented"] is True
    assert status["PyArray_EnsureArray"]["failure_mode"] == "implemented_provider_slot"
    assert status["PyArray_EnsureAnyArray"]["implemented"] is True
    assert (
        status["PyArray_EnsureAnyArray"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_DescrCheck"]["implemented"] is True
    assert status["PyArray_DescrCheck"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_DescrNewFromType"]["implemented"] is True
    assert (
        status["PyArray_DescrNewFromType"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_DescrNew"]["implemented"] is True
    assert (
        status["PyArray_DescrNew"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_DescrNewByteorder"]["implemented"] is True
    assert (
        status["PyArray_DescrNewByteorder"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_CanCastSafely"]["implemented"] is True
    assert (
        status["PyArray_CanCastSafely"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_CanCastTo"]["implemented"] is True
    assert status["PyArray_CanCastTo"]["slot"] == 52
    assert (
        status["PyArray_CanCastTo"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_Zero"]["implemented"] is True
    assert status["PyArray_Zero"]["slot"] == 53
    assert (
        status["PyArray_Zero"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_One"]["implemented"] is True
    assert status["PyArray_One"]["slot"] == 54
    assert (
        status["PyArray_One"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_TypeObjectFromType"]["implemented"] is True
    assert status["PyArray_TypeObjectFromType"]["slot"] == 55
    assert (
        status["PyArray_TypeObjectFromType"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_ObjectType"]["implemented"] is True
    assert (
        status["PyArray_ObjectType"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_DescrFromObject"]["implemented"] is True
    assert status["PyArray_DescrFromObject"]["slot"] == 56
    assert (
        status["PyArray_DescrFromObject"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_Size"]["implemented"] is True
    assert status["PyArray_Size"]["slot"] == 57
    assert (
        status["PyArray_Size"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_DescrFromScalar"]["implemented"] is True
    assert status["PyArray_DescrFromScalar"]["slot"] == 58
    assert (
        status["PyArray_DescrFromScalar"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_DescrFromTypeObject"]["implemented"] is True
    assert status["PyArray_DescrFromTypeObject"]["slot"] == 59
    assert (
        status["PyArray_DescrFromTypeObject"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_ScalarAsCtype"]["implemented"] is True
    assert status["PyArray_ScalarAsCtype"]["slot"] == 60
    assert (
        status["PyArray_ScalarAsCtype"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_FromScalar"]["implemented"] is True
    assert status["PyArray_FromScalar"]["slot"] == 61
    assert (
        status["PyArray_FromScalar"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_CastScalarToCtype"]["implemented"] is True
    assert status["PyArray_CastScalarToCtype"]["slot"] == 62
    assert (
        status["PyArray_CastScalarToCtype"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_CastScalarDirect"]["implemented"] is True
    assert status["PyArray_CastScalarDirect"]["slot"] == 64
    assert (
        status["PyArray_CastScalarDirect"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_Pack"]["implemented"] is True
    assert status["PyArray_Pack"]["slot"] == 63
    assert (
        status["PyArray_Pack"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_CastToType"]["implemented"] is True
    assert status["PyArray_CastToType"]["slot"] == 65
    assert (
        status["PyArray_CastToType"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_Cast"]["implemented"] is True
    assert status["PyArray_Cast"]["slot"] is None
    assert status["PyArray_Cast"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_FillWithScalar"]["implemented"] is True
    assert status["PyArray_FillWithScalar"]["slot"] == 66
    assert (
        status["PyArray_FillWithScalar"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_ToList"]["implemented"] is True
    assert status["PyArray_ToList"]["slot"] == 67
    assert (
        status["PyArray_ToList"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_ToString"]["implemented"] is True
    assert status["PyArray_ToString"]["slot"] == 68
    assert (
        status["PyArray_ToString"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_Byteswap"]["implemented"] is True
    assert status["PyArray_Byteswap"]["slot"] == 69
    assert (
        status["PyArray_Byteswap"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert status["PyArray_SAMESHAPE"]["implemented"] is True
    assert status["PyArray_SAMESHAPE"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_ISCONTIGUOUS"]["implemented"] is True
    assert status["PyArray_ISCONTIGUOUS"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_ISONESEGMENT"]["implemented"] is True
    assert status["PyArray_ISONESEGMENT"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_ISNBO"]["implemented"] is True
    assert status["PyArray_ISNBO"]["failure_mode"] == "implemented_header_macro"
    assert status["PyDataType_ISBYTESWAPPED"]["implemented"] is True
    assert status["PyDataType_ISBYTESWAPPED"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_SAFEALIGNEDCOPY"]["implemented"] is True
    assert status["PyArray_SAFEALIGNEDCOPY"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_FROMANY"]["implemented"] is True
    assert status["PyArray_FROMANY"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_FROM_OF"]["implemented"] is True
    assert status["PyArray_FROM_OF"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_FromObject"]["implemented"] is True
    assert status["PyArray_FromObject"]["failure_mode"] == "implemented_header_macro"
    assert status["PyArray_CopyFromObject"]["implemented"] is True
    assert status["PyArray_CopyFromObject"]["failure_mode"] == "implemented_header_macro"


def test_cached_built_source_tree_counts_as_installable_meson_payload(tmp_path):
    """A cache hit hands the installer an already-built package tree: the
    package ``__init__.py`` plus built native artifacts, and no
    ``build/pcc-package/meson-build`` directory (that only exists in a tree the
    fresh-acquire path just configured). While only the build directory was
    inspected, a cache hit re-entered the meson configure path on a tree that
    carries no build system, so installing the same spec twice reported
    ``install_success`` true and then false on the identical machine.
    """
    from pcc.driver.cli_bootstrap import _native_has_installable_meson_payload

    source = tmp_path / "numpy"
    (source / "_core").mkdir(parents=True)
    (source / "__init__.py").write_text("", encoding="utf-8")
    (source / "meson.build").write_text("", encoding="utf-8")

    # An unbuilt tree must still be built, not silently skipped.
    assert not _native_has_installable_meson_payload("numpy", str(source))

    artifact = source / "_core" / "_multiarray_umath.pcc3-pcc_native-macosx_14_0_arm64.so"
    artifact.write_bytes(b"\x00")
    assert _native_has_installable_meson_payload("numpy", str(source))
