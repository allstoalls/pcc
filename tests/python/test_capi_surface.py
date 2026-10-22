from __future__ import annotations

from pcc.frontends.c.capi_surface import (
    CApiPriority,
    abi_version_diagnostic,
    default_capi_symbols,
    missing_symbols,
    symbol_report,
)


def test_capi_surface_prioritizes_extension_import_blockers():
    symbols = default_capi_symbols()
    assert any(sym.name == "PyModule_Create" for sym in symbols)
    missing = missing_symbols(CApiPriority.IMPORT_BLOCKER)
    assert [sym.name for sym in missing] == []


def test_capi_report_is_json_ready():
    report = symbol_report()
    assert "symbols" in report
    assert report["missing_import_blockers"] == []
    assert "PyObject_Call" not in report["missing_array_core"]
    assert "PyObject_GetBuffer" not in report["missing_array_core"]
    assert "PyObject_CheckBuffer" not in report["missing_array_core"]
    assert "PyBuffer_Release" not in report["missing_array_core"]
    assert "PyCapsule_New" not in report["missing_array_core"]
    assert "PyCapsule_GetPointer" not in report["missing_array_core"]
    assert "PyCapsule_GetName" not in report["missing_array_core"]
    assert "PyCapsule_GetContext" not in report["missing_array_core"]
    assert "PyCapsule_IsValid" not in report["missing_array_core"]
    assert "PyCapsule_CheckExact" not in report["missing_array_core"]
    assert "PyCapsule_SetContext" not in report["missing_array_core"]
    assert "PyCapsule_SetName" not in report["missing_array_core"]
    assert "PyCapsule_SetPointer" not in report["missing_array_core"]
    assert "PyCapsule_GetDestructor" not in report["missing_array_core"]
    assert "PyCapsule_SetDestructor" not in report["missing_array_core"]
    assert "PyCapsule_Import" not in report["missing_array_core"]
    assert "PyMemoryView_FromObject" not in report["missing_array_core"]
    assert "PyMemoryView_FromMemory" not in report["missing_array_core"]
    assert "PyMemoryView_Check" not in report["missing_array_core"]
    assert "PyMemoryView_GET_BUFFER" not in report["missing_array_core"]
    assert "PyMemoryView_GET_BASE" not in report["missing_array_core"]
    assert report["missing_by_priority"]["array_core"] == []
    assert "PyArray_API" not in report["missing_numpy_capi"]
    assert "PyArray_DescrFromType" not in report["missing_numpy_capi"]
    assert "PyArray_FromAny" not in report["missing_numpy_capi"]
    assert "PyArray_SimpleNew" not in report["missing_numpy_capi"]
    assert "PyArray_SimpleNewFromData" not in report["missing_numpy_capi"]
    assert "PyArray_NDIM" not in report["missing_numpy_capi"]
    assert "PyArray_DIMS" not in report["missing_numpy_capi"]
    assert "PyArray_STRIDES" not in report["missing_numpy_capi"]
    assert "PyArray_DATA" not in report["missing_numpy_capi"]
    assert "PyArray_DESCR" not in report["missing_numpy_capi"]
    assert "PyArray_DIM" not in report["missing_numpy_capi"]
    assert "PyArray_BYTES" not in report["missing_numpy_capi"]
    assert "PyArray_GETITEM" not in report["missing_numpy_capi"]
    assert "PyArray_SETITEM" not in report["missing_numpy_capi"]
    assert "PyArray_SIZE" not in report["missing_numpy_capi"]
    assert "PyArray_ITEMSIZE" not in report["missing_numpy_capi"]
    assert "PyArray_Check" not in report["missing_numpy_capi"]
    assert "PyArray_CheckExact" not in report["missing_numpy_capi"]
    assert "PyArray_CheckFromAny" not in report["missing_numpy_capi"]
    assert "PyArray_FromArray" not in report["missing_numpy_capi"]
    assert "PyArray_MultiplyList" not in report["missing_numpy_capi"]
    assert "PyArray_MultiplyIntList" not in report["missing_numpy_capi"]
    assert "PyArray_GetPtr" not in report["missing_numpy_capi"]
    assert "PyArray_ElementStrides" not in report["missing_numpy_capi"]
    assert "PyArray_ValidType" not in report["missing_numpy_capi"]
    assert "PyArray_Item_INCREF" not in report["missing_numpy_capi"]
    assert "PyArray_Item_XDECREF" not in report["missing_numpy_capi"]
    assert "PyArray_NewCopy" not in report["missing_numpy_capi"]
    assert "PyArray_INCREF" not in report["missing_numpy_capi"]
    assert "PyArray_XDECREF" not in report["missing_numpy_capi"]
    assert "PyArray_CanCastTo" not in report["missing_numpy_capi"]
    assert "PyArray_Zero" not in report["missing_numpy_capi"]
    assert "PyArray_One" not in report["missing_numpy_capi"]
    assert "PyArray_TypeObjectFromType" not in report["missing_numpy_capi"]
    assert "PyArray_DescrFromObject" not in report["missing_numpy_capi"]
    assert "PyArray_Size" not in report["missing_numpy_capi"]
    assert "PyArray_DescrFromScalar" not in report["missing_numpy_capi"]
    assert "PyArray_DescrFromTypeObject" not in report["missing_numpy_capi"]
    assert "PyArray_ScalarAsCtype" not in report["missing_numpy_capi"]
    assert "PyArray_FromScalar" not in report["missing_numpy_capi"]
    assert "PyArray_CastScalarToCtype" not in report["missing_numpy_capi"]
    assert "PyArray_CastScalarDirect" not in report["missing_numpy_capi"]
    assert "PyArray_Pack" not in report["missing_numpy_capi"]
    assert "PyArray_CastToType" not in report["missing_numpy_capi"]
    assert "PyArray_Cast" not in report["missing_numpy_capi"]
    assert "PyArray_FillWithScalar" not in report["missing_numpy_capi"]
    assert "PyArray_ToList" not in report["missing_numpy_capi"]
    assert "PyArray_ToString" not in report["missing_numpy_capi"]
    assert "PyArray_Byteswap" not in report["missing_numpy_capi"]
    assert "PyArray_FromString" not in report["missing_numpy_capi"]
    assert "PyArray_FromBuffer" not in report["missing_numpy_capi"]
    assert "PyArray_TYPE" not in report["missing_numpy_capi"]
    assert "PyArray_NBYTES" not in report["missing_numpy_capi"]
    assert "PyArray_FLAGS" not in report["missing_numpy_capi"]
    assert "PyArray_ISCONTIGUOUS" not in report["missing_numpy_capi"]
    assert "PyUFunc_API" not in report["missing_numpy_capi"]
    assert "PyArray_API" not in report["missing_by_priority"]["numpy_capi"]
    assert "PyArray_Type" not in report["missing_by_priority"]["numpy_capi"]
    assert "PyArrayDescr_Type" not in report["missing_by_priority"]["numpy_capi"]
    assert "PyArray_API" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_Type" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArrayDescr_Type" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyUFunc_API" in report["implemented_by_priority"]["numpy_capi"]
    numpy_status = {row["symbol"]: row for row in report["numpy_capi_status"]}
    assert numpy_status["PyArray_API"]["implemented"] is True
    assert numpy_status["PyArray_API"]["table"] == "_ARRAY_API"
    assert numpy_status["PyArray_API"]["slot"] is None
    assert numpy_status["PyArray_API"]["failure_mode"] == "implemented_provider_table"
    for symbol in [
        "PyArray_malloc",
        "PyArray_free",
        "PyArray_realloc",
        "PyDimMem_NEW",
        "PyDimMem_FREE",
        "PyDimMem_RENEW",
    ]:
        assert numpy_status[symbol]["implemented"] is True
        assert numpy_status[symbol]["slot"] is None
        assert numpy_status[symbol]["failure_mode"] == "implemented_header_macro"
    assert numpy_status["PyArray_Type"]["implemented"] is True
    assert numpy_status["PyArray_Type"]["slot"] == 0
    assert (
        numpy_status["PyArray_Type"]["failure_mode"]
        == "implemented_provider_type_object"
    )
    assert numpy_status["PyArrayDescr_Type"]["implemented"] is True
    assert numpy_status["PyArrayDescr_Type"]["slot"] == 1
    assert (
        numpy_status["PyArrayDescr_Type"]["failure_mode"]
        == "implemented_provider_type_object"
    )
    assert numpy_status["PyArray_DescrCheck"]["implemented"] is True
    assert numpy_status["PyArray_DescrCheck"]["slot"] is None
    assert (
        numpy_status["PyArray_DescrCheck"]["failure_mode"]
        == "implemented_header_macro"
    )
    assert numpy_status["PyArray_DescrFromType"]["implemented"] is True
    assert numpy_status["PyArray_DescrFromType"]["slot"] == 2
    assert (
        numpy_status["PyArray_DescrFromType"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_DescrNewFromType"]["implemented"] is True
    assert numpy_status["PyArray_DescrNewFromType"]["slot"] == 35
    assert (
        numpy_status["PyArray_DescrNewFromType"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_DescrNew"]["implemented"] is True
    assert numpy_status["PyArray_DescrNew"]["slot"] == 36
    assert (
        numpy_status["PyArray_DescrNew"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_DescrNewByteorder"]["implemented"] is True
    assert numpy_status["PyArray_DescrNewByteorder"]["slot"] == 37
    assert (
        numpy_status["PyArray_DescrNewByteorder"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_CanCastSafely"]["implemented"] is True
    assert numpy_status["PyArray_CanCastSafely"]["slot"] == 38
    assert (
        numpy_status["PyArray_CanCastSafely"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_CanCastTo"]["implemented"] is True
    assert numpy_status["PyArray_CanCastTo"]["slot"] == 52
    assert (
        numpy_status["PyArray_CanCastTo"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_Zero"]["implemented"] is True
    assert numpy_status["PyArray_Zero"]["slot"] == 53
    assert (
        numpy_status["PyArray_Zero"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_One"]["implemented"] is True
    assert numpy_status["PyArray_One"]["slot"] == 54
    assert (
        numpy_status["PyArray_One"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_TypeObjectFromType"]["implemented"] is True
    assert numpy_status["PyArray_TypeObjectFromType"]["slot"] == 55
    assert (
        numpy_status["PyArray_TypeObjectFromType"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_DescrFromObject"]["implemented"] is True
    assert numpy_status["PyArray_DescrFromObject"]["slot"] == 56
    assert (
        numpy_status["PyArray_DescrFromObject"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_Size"]["implemented"] is True
    assert numpy_status["PyArray_Size"]["slot"] == 57
    assert (
        numpy_status["PyArray_Size"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_DescrFromScalar"]["implemented"] is True
    assert numpy_status["PyArray_DescrFromScalar"]["slot"] == 58
    assert (
        numpy_status["PyArray_DescrFromScalar"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_DescrFromTypeObject"]["implemented"] is True
    assert numpy_status["PyArray_DescrFromTypeObject"]["slot"] == 59
    assert (
        numpy_status["PyArray_DescrFromTypeObject"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_ScalarAsCtype"]["implemented"] is True
    assert numpy_status["PyArray_ScalarAsCtype"]["slot"] == 60
    assert (
        numpy_status["PyArray_ScalarAsCtype"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_FromScalar"]["implemented"] is True
    assert numpy_status["PyArray_FromScalar"]["slot"] == 61
    assert (
        numpy_status["PyArray_FromScalar"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_CastScalarToCtype"]["implemented"] is True
    assert numpy_status["PyArray_CastScalarToCtype"]["slot"] == 62
    assert (
        numpy_status["PyArray_CastScalarToCtype"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_CastScalarDirect"]["implemented"] is True
    assert numpy_status["PyArray_CastScalarDirect"]["slot"] == 64
    assert (
        numpy_status["PyArray_CastScalarDirect"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_Pack"]["implemented"] is True
    assert numpy_status["PyArray_Pack"]["slot"] == 63
    assert (
        numpy_status["PyArray_Pack"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_CastToType"]["implemented"] is True
    assert numpy_status["PyArray_CastToType"]["slot"] == 65
    assert (
        numpy_status["PyArray_CastToType"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_Cast"]["implemented"] is True
    assert numpy_status["PyArray_Cast"]["slot"] is None
    assert (
        numpy_status["PyArray_Cast"]["failure_mode"]
        == "implemented_header_macro"
    )
    assert numpy_status["PyArray_FillWithScalar"]["implemented"] is True
    assert numpy_status["PyArray_FillWithScalar"]["slot"] == 66
    assert (
        numpy_status["PyArray_FillWithScalar"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_ToList"]["implemented"] is True
    assert numpy_status["PyArray_ToList"]["slot"] == 67
    assert (
        numpy_status["PyArray_ToList"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_ToString"]["implemented"] is True
    assert numpy_status["PyArray_ToString"]["slot"] == 68
    assert (
        numpy_status["PyArray_ToString"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_Byteswap"]["implemented"] is True
    assert numpy_status["PyArray_Byteswap"]["slot"] == 69
    assert (
        numpy_status["PyArray_Byteswap"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_FromString"]["implemented"] is True
    assert numpy_status["PyArray_FromString"]["slot"] == 70
    assert (
        numpy_status["PyArray_FromString"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_FromBuffer"]["implemented"] is True
    assert numpy_status["PyArray_FromBuffer"]["slot"] == 71
    assert (
        numpy_status["PyArray_FromBuffer"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_ObjectType"]["implemented"] is True
    assert numpy_status["PyArray_ObjectType"]["slot"] == 39
    assert (
        numpy_status["PyArray_ObjectType"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_CheckFromAny"]["implemented"] is True
    assert numpy_status["PyArray_CheckFromAny"]["slot"] == 40
    assert (
        numpy_status["PyArray_CheckFromAny"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_FromArray"]["implemented"] is True
    assert numpy_status["PyArray_FromArray"]["slot"] == 41
    assert (
        numpy_status["PyArray_FromArray"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_MultiplyList"]["implemented"] is True
    assert numpy_status["PyArray_MultiplyList"]["slot"] == 42
    assert (
        numpy_status["PyArray_MultiplyList"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_MultiplyIntList"]["implemented"] is True
    assert numpy_status["PyArray_MultiplyIntList"]["slot"] == 43
    assert (
        numpy_status["PyArray_MultiplyIntList"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_GetPtr"]["implemented"] is True
    assert numpy_status["PyArray_GetPtr"]["slot"] == 44
    assert (
        numpy_status["PyArray_GetPtr"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_ElementStrides"]["implemented"] is True
    assert numpy_status["PyArray_ElementStrides"]["slot"] == 45
    assert (
        numpy_status["PyArray_ElementStrides"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_ValidType"]["implemented"] is True
    assert numpy_status["PyArray_ValidType"]["slot"] == 46
    assert (
        numpy_status["PyArray_ValidType"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_Item_INCREF"]["implemented"] is True
    assert numpy_status["PyArray_Item_INCREF"]["slot"] == 47
    assert (
        numpy_status["PyArray_Item_INCREF"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_Item_XDECREF"]["implemented"] is True
    assert numpy_status["PyArray_Item_XDECREF"]["slot"] == 48
    assert (
        numpy_status["PyArray_Item_XDECREF"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_NewCopy"]["implemented"] is True
    assert numpy_status["PyArray_NewCopy"]["slot"] == 49
    assert (
        numpy_status["PyArray_NewCopy"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_INCREF"]["implemented"] is True
    assert numpy_status["PyArray_INCREF"]["slot"] == 50
    assert (
        numpy_status["PyArray_INCREF"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_XDECREF"]["implemented"] is True
    assert numpy_status["PyArray_XDECREF"]["slot"] == 51
    assert (
        numpy_status["PyArray_XDECREF"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_FromAny"]["implemented"] is True
    assert numpy_status["PyArray_FromAny"]["slot"] == 3
    assert numpy_status["PyArray_FromAny"]["failure_mode"] == "implemented_provider_slot"
    assert numpy_status["PyArray_SimpleNew"]["implemented"] is True
    assert numpy_status["PyArray_SimpleNew"]["slot"] == 4
    assert (
        numpy_status["PyArray_SimpleNew"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_SimpleNewFromData"]["implemented"] is True
    assert numpy_status["PyArray_SimpleNewFromData"]["slot"] == 5
    assert (
        numpy_status["PyArray_SimpleNewFromData"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_NDIM"]["implemented"] is True
    assert numpy_status["PyArray_NDIM"]["slot"] == 6
    assert numpy_status["PyArray_NDIM"]["failure_mode"] == "implemented_provider_slot"
    assert numpy_status["PyArray_DIMS"]["implemented"] is True
    assert numpy_status["PyArray_DIMS"]["slot"] == 7
    assert numpy_status["PyArray_DIMS"]["failure_mode"] == "implemented_provider_slot"
    assert numpy_status["PyArray_STRIDES"]["implemented"] is True
    assert numpy_status["PyArray_STRIDES"]["slot"] == 8
    assert numpy_status["PyArray_STRIDES"]["failure_mode"] == "implemented_provider_slot"
    assert numpy_status["PyArray_DATA"]["implemented"] is True
    assert numpy_status["PyArray_DATA"]["slot"] == 9
    assert numpy_status["PyArray_DATA"]["failure_mode"] == "implemented_provider_slot"
    assert numpy_status["PyArray_DESCR"]["implemented"] is True
    assert numpy_status["PyArray_DESCR"]["slot"] == 10
    assert numpy_status["PyArray_DESCR"]["failure_mode"] == "implemented_provider_slot"
    assert numpy_status["PyArray_SIZE"]["implemented"] is True
    assert numpy_status["PyArray_SIZE"]["slot"] == 13
    assert numpy_status["PyArray_SIZE"]["failure_mode"] == "implemented_provider_slot"
    assert numpy_status["PyArray_ITEMSIZE"]["implemented"] is True
    assert numpy_status["PyArray_ITEMSIZE"]["slot"] == 14
    assert (
        numpy_status["PyArray_ITEMSIZE"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_Check"]["implemented"] is True
    assert numpy_status["PyArray_Check"]["slot"] == 15
    assert numpy_status["PyArray_Check"]["failure_mode"] == "implemented_provider_slot"
    assert numpy_status["PyArray_CheckExact"]["implemented"] is True
    assert numpy_status["PyArray_CheckExact"]["slot"] == 16
    assert (
        numpy_status["PyArray_CheckExact"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_DIM"]["slot"] == 7
    assert numpy_status["PyArray_DIM"]["implemented"] is True
    assert numpy_status["PyArray_DIM"]["failure_mode"] == "implemented_header_macro"
    assert numpy_status["PyArray_BYTES"]["slot"] == 9
    assert numpy_status["PyArray_BYTES"]["implemented"] is True
    assert numpy_status["PyArray_BYTES"]["failure_mode"] == "implemented_header_macro"
    assert numpy_status["PyArray_TYPE"]["implemented"] is True
    assert numpy_status["PyArray_DTYPE"]["implemented"] is True
    assert numpy_status["PyArray_DTYPE"]["slot"] is None
    assert numpy_status["PyArray_DTYPE"]["failure_mode"] == "implemented_header_macro"
    assert numpy_status["PyDataType_TYPE"]["implemented"] is True
    assert numpy_status["PyDataType_TYPE"]["slot"] is None
    assert numpy_status["PyDataType_TYPE"]["failure_mode"] == "implemented_header_macro"
    assert numpy_status["PyTypeNum_ISFLOAT"]["implemented"] is True
    assert numpy_status["PyTypeNum_ISFLOAT"]["slot"] is None
    assert numpy_status["PyTypeNum_ISFLOAT"]["failure_mode"] == "implemented_header_macro"
    assert numpy_status["PyDataType_ISNUMBER"]["implemented"] is True
    assert numpy_status["PyDataType_ISNUMBER"]["slot"] is None
    assert numpy_status["PyDataType_ISNUMBER"]["failure_mode"] == "implemented_header_macro"
    assert numpy_status["PyArray_ISOBJECT"]["implemented"] is True
    assert numpy_status["PyArray_ISOBJECT"]["slot"] is None
    assert numpy_status["PyArray_ISOBJECT"]["failure_mode"] == "implemented_header_macro"
    assert numpy_status["PyArray_ISONESEGMENT"]["implemented"] is True
    assert numpy_status["PyArray_ISONESEGMENT"]["slot"] is None
    assert numpy_status["PyArray_ISONESEGMENT"]["failure_mode"] == "implemented_header_macro"
    assert numpy_status["PyArray_ISNBO"]["implemented"] is True
    assert numpy_status["PyArray_ISNBO"]["slot"] is None
    assert numpy_status["PyArray_ISNBO"]["failure_mode"] == "implemented_header_macro"
    assert numpy_status["PyDataType_ISBYTESWAPPED"]["implemented"] is True
    assert numpy_status["PyDataType_ISBYTESWAPPED"]["slot"] is None
    assert (
        numpy_status["PyDataType_ISBYTESWAPPED"]["failure_mode"]
        == "implemented_header_macro"
    )
    assert numpy_status["PyArray_SAFEALIGNEDCOPY"]["implemented"] is True
    assert numpy_status["PyArray_SAFEALIGNEDCOPY"]["slot"] is None
    assert (
        numpy_status["PyArray_SAFEALIGNEDCOPY"]["failure_mode"]
        == "implemented_header_macro"
    )
    assert numpy_status["PyArray_FROMANY"]["implemented"] is True
    assert numpy_status["PyArray_FROMANY"]["slot"] is None
    assert numpy_status["PyArray_FROMANY"]["failure_mode"] == "implemented_header_macro"
    assert numpy_status["PyArray_FROM_OF"]["implemented"] is True
    assert numpy_status["PyArray_FROM_OF"]["slot"] is None
    assert numpy_status["PyArray_FROM_OF"]["failure_mode"] == "implemented_header_macro"
    assert numpy_status["PyArray_FromObject"]["implemented"] is True
    assert numpy_status["PyArray_FromObject"]["slot"] is None
    assert numpy_status["PyArray_FromObject"]["failure_mode"] == "implemented_header_macro"
    assert numpy_status["PyArray_CopyFromObject"]["implemented"] is True
    assert numpy_status["PyArray_CopyFromObject"]["slot"] is None
    assert (
        numpy_status["PyArray_CopyFromObject"]["failure_mode"]
        == "implemented_header_macro"
    )
    assert numpy_status["PyArray_NBYTES"]["implemented"] is True
    assert numpy_status["PyArray_FILLWBYTE"]["implemented"] is True
    assert numpy_status["PyArray_FILLWBYTE"]["slot"] is None
    assert numpy_status["PyArray_FILLWBYTE"]["failure_mode"] == "implemented_header_macro"
    assert numpy_status["PyArray_EquivByteorders"]["implemented"] is True
    assert numpy_status["PyArray_EquivByteorders"]["slot"] is None
    assert (
        numpy_status["PyArray_EquivByteorders"]["failure_mode"]
        == "implemented_header_macro"
    )
    assert numpy_status["PyArray_SHAPE"]["implemented"] is True
    assert numpy_status["PyArray_SHAPE"]["slot"] is None
    assert numpy_status["PyArray_SHAPE"]["failure_mode"] == "implemented_header_macro"
    assert numpy_status["PyArray_FLAGS"]["implemented"] is True
    assert numpy_status["PyArray_FLAGS"]["slot"] == 17
    assert numpy_status["PyArray_FLAGS"]["failure_mode"] == "implemented_provider_slot"
    assert numpy_status["PyArray_CompareLists"]["implemented"] is True
    assert numpy_status["PyArray_CompareLists"]["slot"] == 18
    assert (
        numpy_status["PyArray_CompareLists"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_Empty"]["implemented"] is True
    assert numpy_status["PyArray_Empty"]["slot"] == 19
    assert numpy_status["PyArray_Empty"]["failure_mode"] == "implemented_provider_slot"
    assert numpy_status["PyArray_Zeros"]["implemented"] is True
    assert numpy_status["PyArray_Zeros"]["slot"] == 20
    assert numpy_status["PyArray_Zeros"]["failure_mode"] == "implemented_provider_slot"
    assert numpy_status["PyArray_EMPTY"]["implemented"] is True
    assert numpy_status["PyArray_EMPTY"]["slot"] is None
    assert numpy_status["PyArray_EMPTY"]["failure_mode"] == "implemented_header_macro"
    assert numpy_status["PyArray_ZEROS"]["implemented"] is True
    assert numpy_status["PyArray_ZEROS"]["slot"] is None
    assert numpy_status["PyArray_ZEROS"]["failure_mode"] == "implemented_header_macro"
    assert numpy_status["PyArray_EquivTypes"]["implemented"] is True
    assert numpy_status["PyArray_EquivTypes"]["slot"] == 21
    assert (
        numpy_status["PyArray_EquivTypes"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_EquivArrTypes"]["implemented"] is True
    assert numpy_status["PyArray_EquivArrTypes"]["slot"] is None
    assert (
        numpy_status["PyArray_EquivArrTypes"]["failure_mode"]
        == "implemented_header_macro"
    )
    assert numpy_status["PyArray_NewFromDescr"]["implemented"] is True
    assert numpy_status["PyArray_NewFromDescr"]["slot"] == 22
    assert (
        numpy_status["PyArray_NewFromDescr"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_SimpleNewFromDescr"]["implemented"] is True
    assert numpy_status["PyArray_SimpleNewFromDescr"]["slot"] is None
    assert (
        numpy_status["PyArray_SimpleNewFromDescr"]["failure_mode"]
        == "implemented_header_macro"
    )
    assert numpy_status["PyArray_BASE"]["implemented"] is True
    assert numpy_status["PyArray_BASE"]["slot"] == 23
    assert numpy_status["PyArray_BASE"]["failure_mode"] == "implemented_header_macro"
    assert numpy_status["PyArray_SetBaseObject"]["implemented"] is True
    assert numpy_status["PyArray_SetBaseObject"]["slot"] == 24
    assert (
        numpy_status["PyArray_SetBaseObject"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_Return"]["implemented"] is True
    assert numpy_status["PyArray_Return"]["slot"] == 25
    assert numpy_status["PyArray_Return"]["failure_mode"] == "implemented_provider_slot"
    assert numpy_status["PyArray_ENABLEFLAGS"]["implemented"] is True
    assert numpy_status["PyArray_ENABLEFLAGS"]["slot"] == 26
    assert (
        numpy_status["PyArray_ENABLEFLAGS"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_CLEARFLAGS"]["implemented"] is True
    assert numpy_status["PyArray_CLEARFLAGS"]["slot"] == 27
    assert (
        numpy_status["PyArray_CLEARFLAGS"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_UpdateFlags"]["implemented"] is True
    assert numpy_status["PyArray_UpdateFlags"]["slot"] == 28
    assert (
        numpy_status["PyArray_UpdateFlags"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_CopyInto"]["implemented"] is True
    assert numpy_status["PyArray_CopyInto"]["slot"] == 29
    assert (
        numpy_status["PyArray_CopyInto"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_CopyAnyInto"]["implemented"] is True
    assert numpy_status["PyArray_CopyAnyInto"]["slot"] == 30
    assert (
        numpy_status["PyArray_CopyAnyInto"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_ToScalar"]["implemented"] is True
    assert numpy_status["PyArray_ToScalar"]["slot"] == 31
    assert (
        numpy_status["PyArray_ToScalar"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_Copy"]["implemented"] is True
    assert numpy_status["PyArray_Copy"]["slot"] == 32
    assert numpy_status["PyArray_Copy"]["failure_mode"] == "implemented_provider_slot"
    assert numpy_status["PyArray_EnsureArray"]["implemented"] is True
    assert numpy_status["PyArray_EnsureArray"]["slot"] == 33
    assert (
        numpy_status["PyArray_EnsureArray"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_EnsureAnyArray"]["implemented"] is True
    assert numpy_status["PyArray_EnsureAnyArray"]["slot"] == 34
    assert (
        numpy_status["PyArray_EnsureAnyArray"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert numpy_status["PyArray_SAMESHAPE"]["implemented"] is True
    assert numpy_status["PyArray_SAMESHAPE"]["slot"] is None
    assert (
        numpy_status["PyArray_SAMESHAPE"]["failure_mode"]
        == "implemented_header_macro"
    )
    assert numpy_status["PyArray_ISCONTIGUOUS"]["implemented"] is True
    assert numpy_status["PyArray_ISCONTIGUOUS"]["failure_mode"] == "implemented_header_macro"
    assert numpy_status["PyArray_STRIDE"]["implemented"] is True
    assert numpy_status["PyArray_STRIDE"]["slot"] is None
    assert numpy_status["PyArray_STRIDE"]["failure_mode"] == "implemented_header_macro"
    assert numpy_status["PyArray_GETPTR2"]["implemented"] is True
    assert numpy_status["PyArray_GETPTR2"]["slot"] is None
    assert numpy_status["PyArray_GETPTR2"]["failure_mode"] == "implemented_header_macro"
    assert numpy_status["PyArray_GETITEM"]["implemented"] is True
    assert numpy_status["PyArray_GETITEM"]["slot"] == 11
    assert numpy_status["PyArray_GETITEM"]["failure_mode"] == "implemented_provider_slot"
    assert numpy_status["PyArray_SETITEM"]["implemented"] is True
    assert numpy_status["PyArray_SETITEM"]["slot"] == 12
    assert numpy_status["PyArray_SETITEM"]["failure_mode"] == "implemented_provider_slot"
    assert numpy_status["PyUFunc_API"]["implemented"] is True
    assert numpy_status["PyUFunc_API"]["table"] == "_UFUNC_API"
    assert numpy_status["PyUFunc_API"]["slot"] is None
    assert numpy_status["PyUFunc_API"]["failure_mode"] == "implemented_provider_table"
    assert numpy_status["PyUFunc_FromFuncAndData"]["implemented"] is True
    assert numpy_status["PyUFunc_FromFuncAndData"]["slot"] == 0
    assert (
        numpy_status["PyUFunc_FromFuncAndData"]["failure_mode"]
        == "implemented_provider_slot"
    )
    assert "Py_INCREF" in report["implemented_by_priority"]["runtime_core"]
    assert "Py_XDECREF" in report["implemented_by_priority"]["runtime_core"]
    assert "Py_NewRef" in report["implemented_by_priority"]["runtime_core"]
    assert "Py_CLEAR" in report["implemented_by_priority"]["runtime_core"]
    assert "Py_REFCNT" in report["implemented_by_priority"]["runtime_core"]
    assert "Py_SET_REFCNT" in report["implemented_by_priority"]["runtime_core"]
    assert "PyCapsule_SetPointer" in report["implemented_by_priority"]["array_core"]
    assert "PyCapsule_GetDestructor" in report["implemented_by_priority"]["array_core"]
    assert "PyCapsule_SetDestructor" in report["implemented_by_priority"]["array_core"]
    assert "PyArray_DescrFromType" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_FromAny" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_SimpleNew" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_SimpleNewFromData" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_TYPE" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_DTYPE" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyDataType_TYPE" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyTypeNum_ISFLOAT" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyDataType_ISNUMBER" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_ISOBJECT" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_ISONESEGMENT" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_ISNBO" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyDataType_ISBYTESWAPPED" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_SAFEALIGNEDCOPY" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_FROMANY" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_FROM_OF" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_FromObject" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_CopyFromObject" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_CheckFromAny" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_FromArray" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_MultiplyList" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_MultiplyIntList" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_GetPtr" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_ElementStrides" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_ValidType" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_Item_INCREF" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_Item_XDECREF" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_NewCopy" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_INCREF" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_XDECREF" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_NBYTES" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_FILLWBYTE" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_EquivByteorders" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_SHAPE" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_FLAGS" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_NDIM" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_DIMS" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_STRIDES" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_DATA" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_DESCR" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_DIM" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_BYTES" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_GETITEM" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_SETITEM" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_SIZE" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_ITEMSIZE" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_Check" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_CheckExact" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_CheckFromAny" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_FromArray" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_MultiplyList" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_MultiplyIntList" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_GetPtr" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_ElementStrides" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_ValidType" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_Item_INCREF" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_Item_XDECREF" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_NewCopy" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_INCREF" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_XDECREF" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_CompareLists" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_Empty" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_Zeros" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_EMPTY" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_ZEROS" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_EquivTypes" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_EquivArrTypes" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_NewFromDescr" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_SimpleNewFromDescr" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_BASE" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_SetBaseObject" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_Return" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_ENABLEFLAGS" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_CLEARFLAGS" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_UpdateFlags" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_CopyInto" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_CopyAnyInto" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_ToScalar" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_Copy" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_EnsureArray" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_EnsureAnyArray" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_DescrCheck" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_DescrNewFromType" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_DescrNew" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_DescrNewByteorder" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_CanCastSafely" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_CanCastTo" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_Zero" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_One" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_TypeObjectFromType" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_DescrFromObject" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_Size" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_DescrFromScalar" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_DescrFromTypeObject" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_ScalarAsCtype" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_FromScalar" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_CastScalarToCtype" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_CastScalarDirect" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_Pack" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_CastToType" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_Cast" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_FillWithScalar" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_ToList" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_ToString" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_Byteswap" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_FromString" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_FromBuffer" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_ObjectType" in report["implemented_by_priority"]["numpy_capi"]
    for symbol in [
        "PyArray_malloc",
        "PyArray_free",
        "PyArray_realloc",
        "PyDimMem_NEW",
        "PyDimMem_FREE",
        "PyDimMem_RENEW",
    ]:
        assert symbol in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_SAMESHAPE" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_ISCONTIGUOUS" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_STRIDE" in report["implemented_by_priority"]["numpy_capi"]
    assert "PyArray_GETPTR2" in report["implemented_by_priority"]["numpy_capi"]
    for symbol in [
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
    ]:
        assert symbol in report["implemented_by_priority"]["runtime_core"]
    assert "PyMem_Malloc" in report["implemented_by_priority"]["runtime_core"]
    assert "PyMem_Realloc" in report["implemented_by_priority"]["runtime_core"]
    for symbol in [
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
        "Py_UNUSED",
        "PyLong_FromSsize_t",
        "PyLong_FromSize_t",
        "PyLong_FromInt32",
        "PyLong_FromInt64",
        "PyLong_FromUInt32",
        "PyLong_FromUInt64",
        "PyLong_FromVoidPtr",
        "PyLong_FromDouble",
        "PyLong_AsLongAndOverflow",
        "PyLong_AsUnsignedLong",
        "PyLong_AsUnsignedLongLong",
        "PyLong_AsUnsignedLongLongMask",
        "PyLong_AsSsize_t",
        "PyLong_AsSize_t",
        "PyLong_AsInt",
        "PyLong_AsInt32",
        "PyLong_AsInt64",
        "PyLong_AsUInt32",
        "PyLong_AsUInt64",
        "PyLong_AsVoidPtr",
        "PyLong_AsDouble",
        "PyLong_Check",
        "PyLong_CheckExact",
        "PyObject_GetItem",
        "PyObject_SetItem",
        "PyObject_DelItem",
        "PyDict_GetItemWithError",
        "PyDict_GetItemRef",
        "PyDict_GetItemStringRef",
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
        "PyErr_BadInternalCall",
        "PyErr_SetFromErrno",
        "PyErr_SetFromErrnoWithFilenameObject",
        "PyErr_GivenExceptionMatches",
        "PyErr_ExceptionMatches",
        "PyErr_Print",
        "PyErr_CheckSignals",
        "PyErr_Fetch",
        "PyErr_Restore",
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
        "PyErr_WarnEx",
        "PyErr_WarnFormat",
        "PyErr_WriteUnraisable",
        "PyExc_Warning",
        "PyExc_UserWarning",
        "PyExc_RuntimeWarning",
        "PyExc_DeprecationWarning",
        "PyExc_FutureWarning",
        "PyUnicode_FromFormat",
        "PyUnicode_FromFormatV",
        "PyUnicode_FromKindAndData",
        "PyUnicode_FromOrdinal",
        "PyUnicode_AsUCS4",
        "PyUnicode_AsUCS4Copy",
        "PyUnicode_FromEncodedObject",
        "PyUnicode_GetLength",
        "PyUnicode_GET_LENGTH",
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
        "Py_UCS1",
        "Py_UCS2",
        "Py_UCS4",
        "PyUnicode_1BYTE_KIND",
        "PyUnicode_2BYTE_KIND",
        "PyUnicode_4BYTE_KIND",
        "Py_UNICODE_ISSPACE",
        "Py_UNICODE_ISDIGIT",
        "Py_UNICODE_ISDECIMAL",
        "Py_UNICODE_ISNUMERIC",
        "Py_UNICODE_ISLOWER",
        "Py_UNICODE_ISUPPER",
        "Py_UNICODE_ISTITLE",
        "Py_UNICODE_ISALPHA",
        "Py_UNICODE_ISALNUM",
        "PyUnicode_EqualToUTF8",
        "PyUnicode_EqualToUTF8AndSize",
        "PyErr_FormatV",
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
        "PyModule_Add",
        "PyList_GetItemRef",
        "PyList_AsTuple",
        "PyDict_SetDefaultRef",
        "PyObject_CallNoArgs",
        "PyObject_CallOneArg",
        "PyObject_Vectorcall",
        "PyObject_VectorcallMethod",
        "PyObject_CallFunction",
        "PyObject_CallMethod",
        "PyObject_CallMethodNoArgs",
        "PyObject_CallMethodOneArg",
        "PyObject_Type",
        "PyObject_IsInstance",
        "PyUnicode_AsUTF8AndSize",
        "PyBytes_AS_STRING",
        "PyBytes_GET_SIZE",
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
    ]:
        assert symbol in report["implemented_by_priority"]["runtime_core"]
    assert "PyObject_Call" in report["implemented_by_priority"]["runtime_core"]
    assert "PyObject_CallObject" in report["implemented_by_priority"]["runtime_core"]
    assert (
        "PyObject_CallFunctionObjArgs"
        in report["implemented_by_priority"]["runtime_core"]
    )
    assert "Py_BuildValue" in report["implemented_by_priority"]["runtime_core"]
    assert (
        "PyArg_ParseTupleAndKeywords"
        in report["implemented_by_priority"]["runtime_core"]
    )
    assert "PyTuple_New" in report["implemented_by_priority"]["runtime_core"]
    assert "PyTuple_SetItem" in report["implemented_by_priority"]["runtime_core"]
    assert "PyDict_SetItemString" in report["implemented_by_priority"]["runtime_core"]
    assert "PyObject_GetAttrString" in report["implemented_by_priority"]["runtime_core"]
    assert "PyObject_GetAttr" in report["implemented_by_priority"]["runtime_core"]
    assert (
        "PyObject_GetOptionalAttr" in report["implemented_by_priority"]["runtime_core"]
    )
    assert (
        "PyObject_GetOptionalAttrString"
        in report["implemented_by_priority"]["runtime_core"]
    )
    assert "PyObject_SetAttr" in report["implemented_by_priority"]["runtime_core"]
    assert "PyObject_HasAttr" in report["implemented_by_priority"]["runtime_core"]
    assert "PyObject_HasAttrString" in report["implemented_by_priority"]["runtime_core"]
    assert (
        "PyObject_HasAttrWithError" in report["implemented_by_priority"]["runtime_core"]
    )
    assert (
        "PyObject_HasAttrStringWithError"
        in report["implemented_by_priority"]["runtime_core"]
    )
    assert "PyObject_Hash" in report["implemented_by_priority"]["runtime_core"]
    assert "PyCallable_Check" in report["implemented_by_priority"]["runtime_core"]
    assert "PyObject_Str" in report["implemented_by_priority"]["runtime_core"]
    assert "PyObject_Repr" in report["implemented_by_priority"]["runtime_core"]
    assert "PyObject_Bytes" in report["implemented_by_priority"]["runtime_core"]
    assert "PyObject_Format" in report["implemented_by_priority"]["runtime_core"]
    assert "Py_PRINT_RAW" in report["implemented_by_priority"]["runtime_core"]
    assert "PyObject_Print" in report["implemented_by_priority"]["runtime_core"]
    assert "PyObject_RichCompare" in report["implemented_by_priority"]["runtime_core"]
    assert (
        "PyObject_RichCompareBool" in report["implemented_by_priority"]["runtime_core"]
    )
    assert (
        "PyModule_AddIntConstant" in report["implemented_by_priority"]["runtime_core"]
    )
    assert "PyModule_AddObjectRef" in report["implemented_by_priority"]["runtime_core"]
    assert "PyModule_GetDict" in report["implemented_by_priority"]["runtime_core"]
    assert "PyList_New" in report["implemented_by_priority"]["runtime_core"]
    assert "PyList_Append" in report["implemented_by_priority"]["runtime_core"]
    assert "PyList_Check" in report["implemented_by_priority"]["runtime_core"]
    assert "PyTuple_Check" in report["implemented_by_priority"]["runtime_core"]
    assert "PyDict_Check" in report["implemented_by_priority"]["runtime_core"]
    assert "PyBytes_Check" in report["implemented_by_priority"]["runtime_core"]
    assert (
        "PyLong_FromUnsignedLong" in report["implemented_by_priority"]["runtime_core"]
    )
    assert (
        "PyLong_FromUnsignedLongLong"
        in report["implemented_by_priority"]["runtime_core"]
    )
    assert "PyUnicode_Check" in report["implemented_by_priority"]["runtime_core"]
    assert (
        "PyUnicode_InternFromString"
        in report["implemented_by_priority"]["runtime_core"]
    )
    assert (
        "PyUnicode_CompareWithASCIIString"
        in report["implemented_by_priority"]["runtime_core"]
    )
    assert "PySequence_Fast" in report["implemented_by_priority"]["runtime_core"]
    assert "PySequence_Fast_ITEMS" in report["implemented_by_priority"]["runtime_core"]
    assert (
        "PySequence_Fast_GET_ITEM" in report["implemented_by_priority"]["runtime_core"]
    )
    assert "PyTuple_GET_ITEM" in report["implemented_by_priority"]["runtime_core"]
    assert "PyTuple_GET_SIZE" in report["implemented_by_priority"]["runtime_core"]
    assert "PyTuple_SET_ITEM" in report["implemented_by_priority"]["runtime_core"]
    assert "PyList_GET_ITEM" in report["implemented_by_priority"]["runtime_core"]
    assert "PyList_GET_SIZE" in report["implemented_by_priority"]["runtime_core"]
    assert "PyList_SET_ITEM" in report["implemented_by_priority"]["runtime_core"]
    assert "PySequence_Size" in report["implemented_by_priority"]["runtime_core"]
    assert "PySequence_GetItem" in report["implemented_by_priority"]["runtime_core"]
    assert (
        "PyUnicode_FromStringAndSize"
        in report["implemented_by_priority"]["runtime_core"]
    )
    assert (
        "PyBytes_FromStringAndSize" in report["implemented_by_priority"]["runtime_core"]
    )
    assert "PyBytes_FromString" in report["implemented_by_priority"]["runtime_core"]
    assert (
        "PyBytes_AsStringAndSize" in report["implemented_by_priority"]["runtime_core"]
    )
    assert "PyFloat_FromDouble" in report["implemented_by_priority"]["runtime_core"]
    assert "PyBool_FromLong" in report["implemented_by_priority"]["runtime_core"]
    assert "PyObject_IsTrue" in report["implemented_by_priority"]["runtime_core"]
    assert "PyErr_Format" in report["implemented_by_priority"]["runtime_core"]
    assert "PyErr_NoMemory" in report["implemented_by_priority"]["runtime_core"]
    assert "PyErr_NewException" in report["implemented_by_priority"]["runtime_core"]
    assert "Py_IsInitialized" in report["implemented_by_priority"]["runtime_core"]
    assert "PyGILState_Ensure" in report["implemented_by_priority"]["runtime_core"]
    assert "PyGILState_Release" in report["implemented_by_priority"]["runtime_core"]
    assert "PyGILState_Check" in report["implemented_by_priority"]["runtime_core"]
    for symbol in [
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
    ]:
        assert symbol in report["implemented_by_priority"]["runtime_core"]
    assert "PyImport_ImportModule" in report["implemented_by_priority"]["runtime_core"]


def test_numpy_capi_is_tracked_but_not_folded_into_array_core():
    assert missing_symbols(CApiPriority.ARRAY_CORE) == []
    missing_numpy = [sym.name for sym in missing_symbols(CApiPriority.NUMPY_CAPI)]
    assert "PyArray_API" not in missing_numpy
    assert "PyArray_Type" not in missing_numpy
    assert "PyArrayDescr_Type" not in missing_numpy
    assert "PyArray_DescrFromType" not in missing_numpy
    assert "PyArray_FromAny" not in missing_numpy
    assert "PyArray_SimpleNew" not in missing_numpy
    assert "PyArray_SimpleNewFromData" not in missing_numpy
    assert "PyArray_NDIM" not in missing_numpy
    assert "PyArray_DIMS" not in missing_numpy
    assert "PyArray_STRIDES" not in missing_numpy
    assert "PyArray_DATA" not in missing_numpy
    assert "PyArray_DESCR" not in missing_numpy
    assert "PyArray_DIM" not in missing_numpy
    assert "PyArray_BYTES" not in missing_numpy
    assert "PyArray_GETITEM" not in missing_numpy
    assert "PyArray_SETITEM" not in missing_numpy
    assert "PyArray_TYPE" not in missing_numpy
    assert "PyArray_NBYTES" not in missing_numpy
    assert "PyArray_SIZE" not in missing_numpy
    assert "PyArray_ITEMSIZE" not in missing_numpy
    assert "PyArray_FLAGS" not in missing_numpy
    assert "PyArray_Check" not in missing_numpy
    assert "PyArray_CheckExact" not in missing_numpy
    assert "PyArray_CheckFromAny" not in missing_numpy
    assert "PyArray_FromArray" not in missing_numpy
    assert "PyArray_MultiplyList" not in missing_numpy
    assert "PyArray_MultiplyIntList" not in missing_numpy
    assert "PyArray_GetPtr" not in missing_numpy
    assert "PyArray_ElementStrides" not in missing_numpy
    assert "PyArray_ValidType" not in missing_numpy
    assert "PyArray_Item_INCREF" not in missing_numpy
    assert "PyArray_Item_XDECREF" not in missing_numpy
    assert "PyArray_NewCopy" not in missing_numpy
    assert "PyArray_INCREF" not in missing_numpy
    assert "PyArray_XDECREF" not in missing_numpy
    assert "PyArray_ISCONTIGUOUS" not in missing_numpy
    assert "PyUFunc_API" not in missing_numpy
    assert "PyUFunc_FromFuncAndData" not in missing_numpy


def test_abi_version_diagnostic_is_actionable():
    ok = abi_version_diagnostic(provider="array-api", expected=1, actual=1)
    assert ok["ok"] is True
    bad = abi_version_diagnostic(provider="array-api", expected=3, actual=2)
    assert bad["ok"] is False
    assert bad["code"] == "PCC-EXT-ABI-VERSION-MISMATCH"
    assert "expected 3" in bad["message"]
