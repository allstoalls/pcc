"""pcc-Python owners for the C-extension object dispatch surface.

Replaces the pcc_capi_cext_object_* / pcc_capi_cext_* slot-dispatch block of
py_capi_shim.c.  These drive the C-extension PyTypeObject slots (tp_iter,
tp_repr, tp_iternext, tp_getattro, tp_setattro, mp_subscript, sq_item,
tp_call, nb_bool, tp_richcompare, nb_absolute, binary nb_* slots) on behalf
of the migrated C-API object/number/attr surfaces.

_typeobject layout (52 x 8-byte words = 416 bytes, fake-libc _typeobject):
  tp_repr@96, tp_as_number@104, tp_as_sequence@112, tp_as_mapping@120,
  tp_hash@128, tp_call@136, tp_getattro@152, tp_setattro@160, tp_flags@176,
  tp_richcompare@208, tp_iter@224, tp_iternext@232, tp_base@264,
  tp_dictoffset@296, tp_init@304, tp_alloc@312, tp_new@320,
  tp_version_tag@392,
  tp_vectorcall@408
PccCapiNumberMethods (mirror of CPython PyNumberMethods, offsets in words):
  nb_add@0, nb_subtract@8, nb_multiply@16, nb_remainder@24, nb_divmod@32,
  nb_power@40, nb_negative@48, nb_positive@56, nb_absolute@64, nb_bool@72,
  nb_invert@80, nb_lshift@88, nb_rshift@96, nb_and@104, nb_xor@112, nb_or@120,
  nb_int@128, nb_reserved@136, nb_float@144, nb_inplace_add@152,
  nb_inplace_subtract@160, nb_inplace_multiply@168, nb_inplace_remainder@176,
  nb_inplace_power@184, nb_inplace_lshift@192, nb_inplace_rshift@200,
  nb_inplace_and@208, nb_inplace_xor@216, nb_inplace_or@224,
  nb_floor_divide@232, nb_true_divide@240, nb_inplace_floor_divide@248,
  nb_inplace_true_divide@256, nb_index@264, nb_matrix_multiply@272,
  nb_inplace_matrix_multiply@280
PccCapiMappingMethods: mp_length@0, mp_subscript@8, mp_ass_subscript@16
PccCapiSequenceMethods: sq_length@0, sq_concat@8, sq_repeat@16, sq_item@24,
  sq_ass_item@40, sq_contains@56, sq_inplace_concat@64, sq_inplace_repeat@72
The registry uses the generated PY_TYPE_CEXT_TAG_BASE boundary.

Owned surface (stable C ABI names):

  pcc_capi_cext_object_iter, pcc_capi_cext_object_repr,
  pcc_capi_cext_object_next, pcc_capi_cext_object_is_iterator,
  pcc_capi_cext_object_getitem, pcc_capi_cext_object_setitem,
  pcc_capi_cext_object_getattr,
  pcc_capi_cext_object_setattr, pcc_capi_cext_object_is_callable,
  pcc_capi_call_cext_object, pcc_capi_cext_truthy,
  pcc_capi_cext_richcompare_bool, pcc_capi_cext_absolute,
  pcc_capi_cext_binary_number, pcc_capi_cext_inplace_number,
  pcc_capi_cext_subtract,
  pcc_capi_type_object_is_callable
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import (
    PY_TYPE_CEXT_TAG_BASE,
    PY_TYPE_CLASS,
    PY_TYPE_FUNC,
    PY_TYPE_GEN,
    PY_TYPE_STR,
)

from pcc.extern import c_abi_typed_export, c_double, c_int32, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    call_i64_ptr1,
    call_i64_ptr3,
    call_i64_ptr_i64_ptr,
    call_ptr1,
    call_ptr2,
    call_ptr3,
    call_ptr_ptr_i64,
    call_ptr_ptr_ptr_i32,
    call_void_ptr1,
    cstr,
    f64_bits,
    global_addr,
    global_load_ptr,
    i64_to_float,
    is_tagged_int,
    load_f64,
    load_i32,
    load_i64,
    load_i8,
    load_ptr,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    stack_alloc,
    store_i32,
    store_ptr,
    strlen,
)


py_type_of = extern("pcc_py_type_of", (c_ptr,), c_int64)
py_str_new = extern("py_str_new", (c_ptr, c_int64), c_ptr)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_runtime_error_if_unset = extern(
    "py_runtime_error_if_unset", (c_ptr, c_ptr), c_ptr
)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
PyErr_SetNone = extern("PyErr_SetNone", (c_ptr,), c_void)
PyLong_AsLong = extern("PyLong_AsLong", (c_ptr,), c_int64)
pcc_capi_cext_type_for_object = extern("pcc_capi_cext_type_for_object", (c_ptr,), c_ptr)
pcc_capi_cext_tag_for = extern("pcc_capi_cext_tag_for", (c_ptr,), c_int32)
pcc_capi_is_type_object = extern("pcc_capi_is_type_object", (c_ptr,), c_int64)
pcc_capi_is_seqiter = extern("pcc_capi_is_seqiter", (c_ptr,), c_int64)
pcc_capi_seqiter_next = extern("pcc_capi_seqiter_next", (c_ptr,), c_ptr)
PyObject_GenericGetAttr = extern("PyObject_GenericGetAttr", (c_ptr, c_ptr), c_ptr)
PyObject_GenericSetAttr = extern("PyObject_GenericSetAttr", (c_ptr, c_ptr, c_ptr), c_int64)
py_obj_truthy = extern("py_obj_truthy", (c_ptr,), c_int64)
PyType_IsSubtype = extern("PyType_IsSubtype", (c_ptr, c_ptr), c_int32)
py_incref = extern("py_incref", (c_ptr,), c_void)
py_str_utf8 = extern("py_str_utf8", (c_ptr,), c_ptr)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_gc_store_ptr = extern("pcc_gc_store_ptr", (c_ptr, c_ptr, c_ptr), c_void)
PyBool_FromLong = extern("PyBool_FromLong", (c_int64,), c_ptr)
PyFloat_FromDouble = extern("PyFloat_FromDouble", (c_double,), c_ptr)
PyLong_FromLong = extern("PyLong_FromLong", (c_int64,), c_ptr)
PyLong_FromUnsignedLong = extern("PyLong_FromUnsignedLong", (c_int64,), c_ptr)
PyUnicode_FromString = extern("PyUnicode_FromString", (c_ptr,), c_ptr)
PyUnicode_FromStringAndSize = extern("PyUnicode_FromStringAndSize", (c_ptr, c_int64), c_ptr)

# ABI slot offsets and tag values stay literal at their use sites. Library
# modules do not execute module initialization, so top-level numeric constants
# would otherwise become zero-initialized ``.modvar.`` globals.


def _type_error(message) -> None:
    py_raise_owned(py_exc_new(3, message))  # PY_EXC_TYPEERROR


def _runtime_error(message) -> None:
    py_raise_owned(py_exc_new(7, message))  # PY_EXC_RUNTIMEERROR


def _value_error(message) -> None:
    py_raise_owned(py_exc_new(2, message))  # PY_EXC_VALUEERROR


def _signed_i32_result(value: int) -> int:
    # The available indirect-call primitive returns i64, while CPython's
    # objobjargproc/ssizeobjargproc slots return C int.  AArch64 zero-extends
    # w0, so normalize the low word before testing the conventional -1 error.
    low: int = value & 4294967295
    if low >= 2147483648:
        return low - 4294967296
    return low


def _cext_offset(o) -> int:
    if ptr_is_null(o) or is_tagged_int(o):
        return -1
    tag: int = load_i32(o, 8)
    return tag - (PY_TYPE_CEXT_TAG_BASE)


def _cext_type_for_tag(tag: int) -> c_ptr:
    offset = tag - (PY_TYPE_CEXT_TAG_BASE)
    if offset < 0 or offset >= 1024:
        return null()
    count: int = load_i32(global_addr("pcc_capi_cext_type_count"), 0)
    if offset >= count:
        return null()
    table = global_addr("pcc_capi_cext_types")
    return load_ptr(ptr_add(table, offset * 8), 0)


def _cext_require_result(result, helper, message) -> c_ptr:
    """Preserve a slot exception or synthesize one for an invalid NULL."""
    if ptr_is_null(result):
        py_runtime_error_if_unset(helper, message)
    return result


@c_abi_typed_export("pcc_capi_cext_object_iter", "ptr", ("ptr",))
def pcc_capi_cext_object_iter(o) -> c_ptr:
    type_obj = pcc_capi_cext_type_for_object(o)
    if ptr_is_null(type_obj):
        return null()
    iter_slot = load_ptr(type_obj, (224))
    if ptr_is_null(iter_slot):
        return null()
    result = call_ptr1(iter_slot, o)
    return _cext_require_result(
        result,
        cstr("C extension tp_iter"),
        cstr("tp_iter returned NULL without setting an exception"),
    )


@c_abi_typed_export("pcc_capi_cext_object_repr", "ptr", ("ptr",))
def pcc_capi_cext_object_repr(o) -> c_ptr:
    type_obj = pcc_capi_cext_type_for_object(o)
    if ptr_is_null(type_obj):
        return null()
    repr_slot = load_ptr(type_obj, (96))
    if ptr_is_null(repr_slot):
        return null()
    result = call_ptr1(repr_slot, o)
    return _cext_require_result(
        result,
        cstr("C extension tp_repr"),
        cstr("tp_repr returned NULL without setting an exception"),
    )


@c_abi_typed_export("pcc_capi_cext_object_next", "ptr", ("ptr",))
def pcc_capi_cext_object_next(o) -> c_ptr:
    if pcc_capi_is_seqiter(o) != 0:
        result = pcc_capi_seqiter_next(o)
    else:
        type_obj = pcc_capi_cext_type_for_object(o)
        if ptr_is_null(type_obj):
            return null()
        next_slot = load_ptr(type_obj, (232))
        if ptr_is_null(next_slot):
            return null()
        result = call_ptr1(next_slot, o)
    if ptr_is_null(result) and py_err_occurred() == 0:
        PyErr_SetNone(global_load_ptr("PyExc_StopIteration"))
    return result


@c_abi_typed_export("pcc_capi_cext_object_is_iterator", "i64", ("ptr",))
def pcc_capi_cext_object_is_iterator(o) -> int:
    if pcc_capi_is_seqiter(o) != 0:
        return 1
    type_obj = pcc_capi_cext_type_for_object(o)
    if ptr_is_null(type_obj):
        return 0
    if ptr_is_null(load_ptr(type_obj, (232))):
        return 0
    return 1


@c_abi_typed_export("pcc_capi_cext_object_getitem", "ptr", ("ptr", "ptr"))
def pcc_capi_cext_object_getitem(o, key) -> c_ptr:
    type_obj = pcc_capi_cext_type_for_object(o)
    if ptr_is_null(type_obj):
        return null()
    mapping = load_ptr(type_obj, (120))
    if not ptr_is_null(mapping):
        subscript = load_ptr(mapping, (8))
        if not ptr_is_null(subscript):
            result = call_ptr2(subscript, o, key)
            return _cext_require_result(
                result,
                cstr("C extension mp_subscript"),
                cstr("mp_subscript returned NULL without setting an exception"),
            )
    sequence = load_ptr(type_obj, (112))
    if not ptr_is_null(sequence):
        item = load_ptr(sequence, (24))
        if not ptr_is_null(item):
            index = PyLong_AsLong(key)
            if py_err_occurred() != 0:
                return null()
            result = call_ptr_ptr_i64(item, o, index)
            return _cext_require_result(
                result,
                cstr("C extension sq_item"),
                cstr("sq_item returned NULL without setting an exception"),
            )
    return null()


@c_abi_typed_export(
    "pcc_capi_cext_object_setitem", "i64", ("ptr", "ptr", "ptr")
)
def pcc_capi_cext_object_setitem(o, key, value) -> int:
    type_obj = pcc_capi_cext_type_for_object(o)
    if ptr_is_null(type_obj):
        _type_error(cstr("object does not support item assignment"))
        return -1
    mapping = load_ptr(type_obj, 120)  # tp_as_mapping
    if not ptr_is_null(mapping):
        assign = load_ptr(mapping, 16)  # mp_ass_subscript
        if not ptr_is_null(assign):
            result: int = _signed_i32_result(
                call_i64_ptr3(assign, o, key, value)
            )
            if result < 0 and py_err_occurred() == 0:
                _runtime_error(
                    cstr(
                        "mp_ass_subscript returned failure without setting an exception"
                    )
                )
            return result
    sequence = load_ptr(type_obj, 112)  # tp_as_sequence
    if not ptr_is_null(sequence):
        assign = load_ptr(sequence, 40)  # sq_ass_item
        if not ptr_is_null(assign):
            index: int = PyLong_AsLong(key)
            if py_err_occurred() != 0:
                return -1
            result = _signed_i32_result(
                call_i64_ptr_i64_ptr(assign, o, index, value)
            )
            if result < 0 and py_err_occurred() == 0:
                _runtime_error(
                    cstr(
                        "sq_ass_item returned failure without setting an exception"
                    )
                )
            return result
    _type_error(cstr("object does not support item assignment"))
    return -1


@c_abi_typed_export("pcc_capi_cext_object_length", "i64", ("ptr",))
def pcc_capi_cext_object_length(o) -> int:
    # len() for a cext object: mp_length@0 of tp_as_mapping@120, else
    # sq_length@0 of tp_as_sequence@112. Returns -1 when neither slot
    # exists so py_obj_len can fall through instead of reporting 0.
    type_obj = pcc_capi_cext_type_for_object(o)
    if ptr_is_null(type_obj):
        return -1
    mapping = load_ptr(type_obj, (120))
    if not ptr_is_null(mapping):
        mlen = load_ptr(mapping, (0))
        if not ptr_is_null(mlen):
            result: int = call_i64_ptr1(mlen, o)
            if result < 0:
                py_runtime_error_if_unset(
                    cstr("C extension mp_length"),
                    cstr(
                        "mp_length returned a negative result without setting an exception"
                    ),
                )
            return result
    sequence = load_ptr(type_obj, (112))
    if not ptr_is_null(sequence):
        slen = load_ptr(sequence, (0))
        if not ptr_is_null(slen):
            result = call_i64_ptr1(slen, o)
            if result < 0:
                py_runtime_error_if_unset(
                    cstr("C extension sq_length"),
                    cstr(
                        "sq_length returned a negative result without setting an exception"
                    ),
                )
            return result
    return -1


@c_abi_typed_export("pcc_capi_cext_object_getattr", "ptr", ("ptr", "ptr"))
def pcc_capi_cext_object_getattr(o, name) -> c_ptr:
    type_obj = pcc_capi_cext_type_for_object(o)
    if ptr_is_null(type_obj) or ptr_is_null(name):
        return null()
    name_obj = py_str_new(name, strlen(name))
    if ptr_is_null(name_obj):
        return null()
    getattro = load_ptr(type_obj, (152))
    if not ptr_is_null(getattro):
        result = call_ptr2(getattro, o, name_obj)
    else:
        result = PyObject_GenericGetAttr(o, name_obj)
    result = _cext_require_result(
        result,
        cstr("C extension tp_getattro"),
        cstr("tp_getattro returned NULL without setting an exception"),
    )
    py_decref(name_obj)
    return result


@c_abi_typed_export("pcc_capi_cext_object_setattr", "i64", ("ptr", "ptr", "ptr"))
def pcc_capi_cext_object_setattr(o, name, value) -> int:
    type_obj = pcc_capi_cext_type_for_object(o)
    if ptr_is_null(type_obj) or ptr_is_null(name):
        return -1
    name_obj = py_str_new(name, strlen(name))
    if ptr_is_null(name_obj):
        return -1
    setattro = load_ptr(type_obj, (160))
    if not ptr_is_null(setattro):
        result = _signed_i32_result(call_ptr3(setattro, o, name_obj, value))
    else:
        result = PyObject_GenericSetAttr(o, name_obj, value)
    if result != 0:
        py_runtime_error_if_unset(
            cstr("C extension tp_setattro"),
            cstr("tp_setattro returned failure without setting an exception"),
        )
    py_decref(name_obj)
    return result


@c_abi_typed_export("pcc_capi_cext_object_is_callable", "i64", ("ptr",))
def pcc_capi_cext_object_is_callable(callable) -> int:
    offset = _cext_offset(callable)
    if offset < 0:
        return 0
    count: int = load_i32(global_addr("pcc_capi_cext_type_count"), 0)
    if offset >= count:
        return 0
    table = global_addr("pcc_capi_cext_types")
    type_obj = load_ptr(ptr_add(table, offset * 8), 0)
    if ptr_is_null(type_obj):
        return 0
    if ptr_is_null(load_ptr(type_obj, (136))):
        return 0
    return 1


@c_abi_typed_export("pcc_capi_call_cext_object", "ptr", ("ptr", "ptr", "ptr"))
def pcc_capi_call_cext_object(callable, args, kwargs) -> c_ptr:
    if pcc_capi_cext_object_is_callable(callable) == 0:
        _type_error(cstr("C extension object is not callable"))
        return null()
    offset = _cext_offset(callable)
    table = global_addr("pcc_capi_cext_types")
    type_obj = load_ptr(ptr_add(table, offset * 8), 0)
    tp_call = load_ptr(type_obj, (136))
    # tp_call's kwargs contract is NULL-or-dict; the runtime passes py_None
    # for "no kwargs", normalize it (mirrors the C shim).
    call_kwargs = kwargs
    if not ptr_is_null(kwargs) and ptr_eq(kwargs, global_load_ptr("py_None")):
        call_kwargs = null()
    result = call_ptr3(tp_call, callable, args, call_kwargs)
    if ptr_is_null(result) and py_err_occurred() == 0:
        py_runtime_error_if_unset(
            cstr("C extension tp_call"),
            cstr("C extension tp_call returned NULL without setting an exception"),
        )
    return result


@c_abi_typed_export("pcc_capi_cext_truthy", "i64", ("ptr",))
def pcc_capi_cext_truthy(value) -> int:
    type_obj = pcc_capi_cext_type_for_object(value)
    if ptr_is_null(type_obj):
        return 1
    methods = load_ptr(type_obj, (104))
    if ptr_is_null(methods):
        return 1
    slot = load_ptr(methods, (72))
    if ptr_is_null(slot):
        return 1
    result = call_ptr1(slot, value)
    if result < 0:
        if py_err_occurred() == 0:
            _runtime_error(
                cstr("nb_bool returned a negative result without an exception")
            )
        return -1
    if result != 0:
        return 1
    return 0


def _swapped_richcompare_op(op: int) -> int:
    # op codes: 0=LT 1=LE 2=EQ 3=NE 4=GT 5=GE; swapped = GT GE EQ NE LT LE.
    # if-chain, not a module-level tuple: library builds zero module consts.
    if op == 0:
        return 4
    if op == 1:
        return 5
    if op == 2:
        return 2
    if op == 3:
        return 3
    if op == 4:
        return 0
    if op == 5:
        return 1
    return -1


def _call_richcompare_slot(slot, left, right, op: int) -> int:
    result = call_ptr_ptr_ptr_i32(slot, left, right, op)
    if ptr_is_null(result):
        if py_err_occurred() == 0:
            _runtime_error(
                cstr("tp_richcompare returned NULL without setting an exception")
            )
        return -1
    if ptr_eq(result, global_load_ptr("py_NotImplemented")):
        py_decref(result)
        return -2
    truth = py_obj_truthy(result)
    py_decref(result)
    if py_err_occurred() != 0:
        return -1
    if truth != 0:
        return 1
    return 0


@c_abi_typed_export("pcc_capi_cext_richcompare_bool", "i64", ("ptr", "ptr", "i32"))
def pcc_capi_cext_richcompare_bool(left, right, op: int) -> int:
    swapped_op = _swapped_richcompare_op(op)
    if swapped_op < 0:
        _value_error(cstr("invalid rich-compare operation"))
        return -1
    left_type = pcc_capi_cext_type_for_object(left)
    right_type = pcc_capi_cext_type_for_object(right)
    left_slot = null()
    right_slot = null()
    if not ptr_is_null(left_type):
        left_slot = load_ptr(left_type, (208))
    if not ptr_is_null(right_type):
        right_slot = load_ptr(right_type, (208))
    # Raw pointer compares must be ptr_eq: `==` on object pointers lowers to
    # py_obj_eq, which dispatches back here and recurses forever.
    if ptr_eq(left_type, right_type) or ptr_eq(right_slot, left_slot):
        right_slot = null()
    if not ptr_is_null(right_slot) and not ptr_is_null(left_type):
        if PyType_IsSubtype(right_type, left_type) != 0:
            result = _call_richcompare_slot(right_slot, right, left, swapped_op)
            if result != -2:
                return result
            right_slot = null()
    if not ptr_is_null(left_slot):
        result = _call_richcompare_slot(left_slot, left, right, op)
        if result != -2:
            return result
    if not ptr_is_null(right_slot):
        result = _call_richcompare_slot(right_slot, right, left, swapped_op)
        if result != -2:
            return result
    if op == 2:  # Py_EQ
        if ptr_eq(left, right):
            return 1
        return 0
    if op == 3:  # Py_NE
        if ptr_eq(left, right):
            return 0
        return 1
    _type_error(cstr("unsupported rich comparison"))
    return -1


@c_abi_typed_export("pcc_capi_cext_absolute", "ptr", ("ptr",))
def pcc_capi_cext_absolute(o) -> c_ptr:
    type_obj = pcc_capi_cext_type_for_object(o)
    if ptr_is_null(type_obj):
        return null()
    methods = load_ptr(type_obj, (104))
    if ptr_is_null(methods):
        return null()
    slot = load_ptr(methods, (64))
    if ptr_is_null(slot):
        return null()
    result = call_ptr1(slot, o)
    return _cext_require_result(
        result,
        cstr("C extension nb_absolute"),
        cstr("nb_absolute returned NULL without setting an exception"),
    )


@c_abi_typed_export("pcc_capi_cext_subtract", "ptr", ("ptr", "ptr"))
def pcc_capi_cext_subtract(left, right) -> c_ptr:
    return _binary_slot(left, right, (8), "nb_subtract")


@c_abi_typed_export("pcc_capi_cext_binary_number", "ptr", ("ptr", "ptr", "i64"))
def pcc_capi_cext_binary_number(left, right, op: int) -> c_ptr:
    # op codes: 0=add 1=subtract 2=multiply 3=remainder 4=divmod 5=power
    # 6=lshift 7=rshift 8=and 9=xor 10=or 11=floor_divide 12=true_divide
    # 13=matrix_multiply
    if op == 0:
        return _binary_slot(left, right, (0), "nb_add")
    if op == 1:
        return _binary_slot(left, right, (8), "nb_subtract")
    if op == 2:
        return _binary_slot(left, right, (16), "nb_multiply")
    if op == 3:
        return _binary_slot(left, right, (24), "nb_remainder")
    if op == 4:
        return _binary_slot(left, right, (32), "nb_divmod")
    if op == 5:
        return _binary_slot(left, right, (40), "nb_power")
    if op == 6:
        return _binary_slot(left, right, (88), "nb_lshift")
    if op == 7:
        return _binary_slot(left, right, (96), "nb_rshift")
    if op == 8:
        return _binary_slot(left, right, (104), "nb_and")
    if op == 9:
        return _binary_slot(left, right, (112), "nb_xor")
    if op == 10:
        return _binary_slot(left, right, (120), "nb_or")
    if op == 11:
        return _binary_slot(left, right, (232), "nb_floor_divide")
    if op == 12:
        return _binary_slot(left, right, (240), "nb_true_divide")
    if op == 13:
        return _binary_slot(left, right, (272), "nb_matrix_multiply")
    return null()


@c_abi_typed_export(
    "pcc_capi_cext_inplace_number", "ptr", ("ptr", "ptr", "i64")
)
def pcc_capi_cext_inplace_number(left, right, op: int) -> c_ptr:
    # The op table intentionally matches py_obj_inplace_op rather than the
    # wider ordinary-binary table: 0=add 1=subtract 2=multiply 3=true_divide
    # 4=floor_divide 5=remainder.
    slot_offset: int = -1
    if op == 0:
        slot_offset = 152  # nb_inplace_add
    elif op == 1:
        slot_offset = 160  # nb_inplace_subtract
    elif op == 2:
        slot_offset = 168  # nb_inplace_multiply
    elif op == 3:
        slot_offset = 256  # nb_inplace_true_divide
    elif op == 4:
        slot_offset = 248  # nb_inplace_floor_divide
    elif op == 5:
        slot_offset = 176  # nb_inplace_remainder
    else:
        _value_error(cstr("invalid in-place number operation"))
        return null()

    not_implemented = global_load_ptr("py_NotImplemented")
    type_obj = pcc_capi_cext_type_for_object(left)
    if ptr_is_null(type_obj):
        py_incref(not_implemented)
        return not_implemented
    methods = load_ptr(type_obj, 104)  # tp_as_number
    if ptr_is_null(methods):
        py_incref(not_implemented)
        return not_implemented
    slot = load_ptr(methods, slot_offset)
    if ptr_is_null(slot):
        py_incref(not_implemented)
        return not_implemented

    result = call_ptr2(slot, left, right)
    if ptr_is_null(result) and py_err_occurred() == 0:
        _runtime_error(cstr("in-place number slot returned NULL without setting an exception"))
    return result


def _binary_number_slot_of(type_obj, slot_offset: int) -> c_ptr:
    if ptr_is_null(type_obj):
        return null()
    methods = load_ptr(type_obj, (104))  # tp_as_number
    if ptr_is_null(methods):
        return null()
    return load_ptr(methods, slot_offset)


def _call_binary_number_slot(slot, left, right, slot_name) -> c_ptr:
    result = call_ptr2(slot, left, right)
    if ptr_is_null(result) and py_err_occurred() == 0:
        _runtime_error(cstr("binary number slot returned NULL without setting an exception"))
    return result


def _binary_slot(left, right, slot_offset: int, slot_name) -> c_ptr:
    # Full CPython binary-op protocol (mirrors the C shim): try left's slot,
    # then right's (subclass-priority first), treat NotImplemented as
    # fall-through, and raise TypeError if nothing handled it. Only-left-slot
    # short-circuiting silently returned NULL for int * numpy-scalar.
    left_type = pcc_capi_cext_type_for_object(left)
    right_type = pcc_capi_cext_type_for_object(right)
    left_slot = _binary_number_slot_of(left_type, slot_offset)
    right_slot = _binary_number_slot_of(right_type, slot_offset)
    if ptr_eq(left_type, right_type) or ptr_eq(right_slot, left_slot):
        right_slot = null()
    if not ptr_is_null(right_slot) and not ptr_is_null(left_type):
        if PyType_IsSubtype(right_type, left_type) != 0:
            result = _call_binary_number_slot(right_slot, left, right, slot_name)
            if ptr_is_null(result):
                return null()
            if not ptr_eq(result, global_load_ptr("py_NotImplemented")):
                return result
            py_decref(result)
            right_slot = null()
    if not ptr_is_null(left_slot):
        result = _call_binary_number_slot(left_slot, left, right, slot_name)
        if ptr_is_null(result):
            return null()
        if not ptr_eq(result, global_load_ptr("py_NotImplemented")):
            return result
        py_decref(result)
    if not ptr_is_null(right_slot):
        result = _call_binary_number_slot(right_slot, left, right, slot_name)
        if ptr_is_null(result):
            return null()
        if not ptr_eq(result, global_load_ptr("py_NotImplemented")):
            return result
        py_decref(result)
    if py_err_occurred() == 0:
        _type_error(cstr("unsupported operand type(s) for C-extension binary op"))
    return null()


@c_abi_typed_export("pcc_capi_type_object_is_callable", "i64", ("ptr",))
def pcc_capi_type_object_is_callable(callable) -> int:
    if pcc_capi_is_type_object(callable) == 0:
        return 0
    type_obj = callable
    version_tag: int = load_i32(type_obj, (392))
    if version_tag < (PY_TYPE_CEXT_TAG_BASE):
        return 0
    if ptr_is_null(load_ptr(type_obj, (320))):
        return 0
    return 1


@c_abi_typed_export("pcc_capi_call_type_object", "ptr", ("ptr", "ptr", "ptr"))
def pcc_capi_call_type_object(callable, args, kwargs) -> c_ptr:
    if pcc_capi_type_object_is_callable(callable) == 0:
        _type_error(cstr("C extension type is not callable"))
        return null()
    call_kwargs = kwargs
    if not ptr_is_null(kwargs) and ptr_eq(kwargs, global_load_ptr("py_None")):
        call_kwargs = null()
    # Runtime-library module globals are zero-initialized and module-top
    # assignments do not execute, so ABI offsets must remain literal here.
    tp_new = load_ptr(callable, 320)
    result = call_ptr3(tp_new, callable, args, call_kwargs)
    if ptr_is_null(result):
        py_runtime_error_if_unset(
            cstr("C extension tp_new"),
            cstr("tp_new returned NULL without setting an exception"),
        )
        return null()
    tp_init = load_ptr(callable, 304)
    if not ptr_is_null(tp_init):
        init_result = _signed_i32_result(
            call_i64_ptr3(tp_init, result, args, call_kwargs)
        )
        if init_result != 0:
            py_runtime_error_if_unset(
                cstr("C extension tp_init"),
                cstr("tp_init returned failure without setting an exception"),
            )
            py_decref(result)
            return null()
    return result


# --- PyCallable_Check -------------------------------------------------


@c_abi_typed_export("PyCallable_Check", "i32", ("ptr",))
def PyCallable_Check(obj) -> int:
    if ptr_is_null(obj) or is_tagged_int(obj):
        return 0
    tag: int = load_i32(obj, 8)
    if tag == (PY_TYPE_CLASS) or tag == (PY_TYPE_FUNC) or tag == (PY_TYPE_GEN):
        return 1
    if pcc_capi_cext_object_is_callable(obj) != 0:
        return 1
    if pcc_capi_type_object_is_callable(obj) != 0:
        return 1
    return 0


# --- pcc_capi_dealloc_cext_object / set_type -------------------------

pcc_gc_free_object_memory = extern("pcc_gc_free_object_memory", (c_ptr,), c_void)


@c_abi_typed_export("pcc_capi_dealloc_cext_object", "i64", ("ptr", "i64"))
def pcc_capi_dealloc_cext_object(o, type_tag: int) -> int:
    offset = type_tag - (PY_TYPE_CEXT_TAG_BASE)
    count: int = load_i32(global_addr("pcc_capi_cext_type_count"), 0)
    if offset < 0 or offset >= count:
        return 0
    table = global_addr("pcc_capi_cext_types")
    type_obj = load_ptr(ptr_add(table, offset * 8), 0)
    if not ptr_is_null(type_obj):
        flags: int = load_i64(type_obj, 176)  # tp_flags
        dealloc = load_ptr(type_obj, 56)  # tp_dealloc
        # Public fake-Python.h/C-oracle ABI: PCC_TPFLAGS_MANAGED_DEALLOC is
        # 1UL<<62. Keep the literal in the owner so library mode never depends
        # on module initialization.
        if (flags & 4611686018427387904) != 0 and not ptr_is_null(dealloc):
            call_void_ptr1(dealloc, o)
    pcc_gc_free_object_memory(o)
    return 1


@c_abi_typed_export("pcc_capi_set_type", "void", ("ptr", "ptr"))
def pcc_capi_set_type(o, t) -> None:
    if ptr_is_null(o):
        return
    store_i32(o, 8, pcc_capi_cext_tag_for(t))
    store_ptr(o, 16, t)


# --- py_cext_number_to_i64 -------------------------------------------

PyNumber_Long = extern("PyNumber_Long", (c_ptr,), c_ptr)
py_int_to_i64 = extern("py_int_to_i64", (c_ptr, c_ptr), c_int64)
pcc_capi_is_cext_type_tag = extern("pcc_capi_is_cext_type_tag", (c_int64,), c_int64)
py_clear_exception = extern("py_clear_exception", (), c_void)


@c_abi_typed_export("py_cext_number_to_i64", "i64", ("ptr", "ptr"))
def py_cext_number_to_i64(o, overflow_ptr) -> int:
    if not ptr_is_null(overflow_ptr):
        store_i32(overflow_ptr, 0, 1)
    if ptr_is_null(o) or is_tagged_int(o):
        return 0
    tag: int = load_i32(o, 8)
    if pcc_capi_is_cext_type_tag(tag) == 0:
        return 0
    boxed = PyNumber_Long(o)
    if ptr_is_null(boxed):
        if py_err_occurred() != 0:
            py_clear_exception()
        return 0
    ov_slot = stack_alloc(4)
    store_i32(ov_slot, 0, 0)
    value = py_int_to_i64(boxed, ov_slot)
    py_decref(boxed)
    if not ptr_is_null(overflow_ptr):
        store_i32(overflow_ptr, 0, load_i32(ov_slot, 0))
    return value


# --- PyObject_GenericGetAttr / SetAttr / GetDict ---------------------

py_dict_get = extern("py_dict_get", (c_ptr, c_ptr), c_ptr)
py_dict_new = extern("py_dict_new", (), c_ptr)
py_dict_set = extern("py_dict_set", (c_ptr, c_ptr, c_ptr), c_void)
pcc_capi_member_get = extern("pcc_capi_member_get", (c_ptr, c_ptr), c_ptr)
pcc_capi_object_dict_slot = extern("pcc_capi_object_dict_slot", (c_ptr, c_ptr), c_ptr)
pcc_capi_method_func_new = extern("pcc_capi_method_func_new", (c_ptr, c_ptr), c_ptr)
PyObject_GetAttr = extern("PyObject_GetAttr", (c_ptr, c_ptr), c_ptr)
PyObject_SetAttr = extern("PyObject_SetAttr", (c_ptr, c_ptr, c_ptr), c_int64)
py_obj_getattr = extern("py_obj_getattr", (c_ptr, c_ptr), c_ptr)

# PccCapiGetSetDef: name@0, get@8, set@16, doc@24, closure@32
# PccCapiMemberDef: name@0, type@8, offset@16, flags@24, doc@32


def _cstr_eq(a, b) -> int:
    i: int = 0
    while True:
        ca: int = load_i8(a, i)
        cb: int = load_i8(b, i)
        if ca != cb:
            return 0
        if ca == 0:
            return 1
        i += 1


def _find_getset(type_obj, attr) -> c_ptr:
    # Arrays are terminated by a {NULL, ...} entry: stop on NULL name, the
    # array pointer itself never becomes NULL.
    getset = load_ptr(type_obj, (256))
    while not ptr_is_null(getset):
        gs_name = load_ptr(getset, 0)
        if ptr_is_null(gs_name):
            return null()
        if _cstr_eq(gs_name, attr) != 0:
            return getset
        getset = ptr_add(getset, 40)
    return null()


def _find_member(type_obj, attr) -> c_ptr:
    member = load_ptr(type_obj, (248))
    while not ptr_is_null(member):
        m_name = load_ptr(member, 0)
        if ptr_is_null(m_name):
            return null()
        if _cstr_eq(m_name, attr) != 0:
            return member
        member = ptr_add(member, 40)
    return null()


def _find_method(type_obj, attr) -> c_ptr:
    method = load_ptr(type_obj, (240))
    while not ptr_is_null(method):
        m_name = load_ptr(method, 0)
        if ptr_is_null(m_name):
            return null()
        if _cstr_eq(m_name, attr) != 0:
            return method
        method = ptr_add(method, 32)
    return null()


@c_abi_typed_export("PyObject_GenericGetAttr", "ptr", ("ptr", "ptr"))
def PyObject_GenericGetAttr(o, name) -> c_ptr:
    type_obj = pcc_capi_cext_type_for_object(o)
    if ptr_is_null(name) or is_tagged_int(name):
        _type_error(cstr("attribute name must be a string"))
        return null()
    if load_i32(name, 8) != PY_TYPE_STR:  # PY_TYPE_STR
        _type_error(cstr("attribute name must be a string"))
        return null()
    if ptr_is_null(type_obj):
        return PyObject_GetAttr(o, name)
    attr = py_str_utf8(name)
    if ptr_is_null(attr):
        return null()
    current = type_obj
    while not ptr_is_null(current):
        getset = _find_getset(current, attr)
        if not ptr_is_null(getset):
            get_slot = load_ptr(getset, 8)
            if not ptr_is_null(get_slot):
                result = call_ptr2(get_slot, o, load_ptr(getset, 32))
                return _cext_require_result(
                    result,
                    cstr("C extension getset getter"),
                    cstr("getset getter returned NULL without setting an exception"),
                )
        member = _find_member(current, attr)
        if not ptr_is_null(member):
            return pcc_capi_member_get(o, member)
        current = load_ptr(current, (264))
    dict_slot = pcc_capi_object_dict_slot(o, type_obj)
    if not ptr_is_null(dict_slot):
        dict_obj = pcc_gc_load_ptr(o, dict_slot)
        if not ptr_is_null(dict_obj):
            value = py_dict_get(dict_obj, name)
            if not ptr_is_null(value):
                return value
    current = type_obj
    while not ptr_is_null(current):
        method = _find_method(current, attr)
        if not ptr_is_null(method):
            return pcc_capi_method_func_new(o, method)
        current = load_ptr(current, (264))
    py_raise_owned(py_exc_new(6, attr))  # PY_EXC_ATTRIBUTEERROR
    return null()


@c_abi_typed_export("PyObject_GenericSetAttr", "i32", ("ptr", "ptr", "ptr"))
def PyObject_GenericSetAttr(o, name, value) -> int:
    type_obj = pcc_capi_cext_type_for_object(o)
    if ptr_is_null(name) or is_tagged_int(name):
        _type_error(cstr("attribute name must be a string"))
        return -1
    if load_i32(name, 8) != PY_TYPE_STR:
        _type_error(cstr("attribute name must be a string"))
        return -1
    if ptr_is_null(type_obj):
        return PyObject_SetAttr(o, name, value)
    attr = py_str_utf8(name)
    if ptr_is_null(attr):
        return -1
    current = type_obj
    while not ptr_is_null(current):
        getset = _find_getset(current, attr)
        if not ptr_is_null(getset):
            set_slot = load_ptr(getset, 16)
            if ptr_is_null(set_slot):
                py_raise_owned(py_exc_new(6, attr))  # PY_EXC_ATTRIBUTEERROR
                return -1
            result = _signed_i32_result(
                call_i64_ptr3(set_slot, o, value, load_ptr(getset, 32))
            )
            if result != 0:
                py_runtime_error_if_unset(
                    cstr("C extension getset setter"),
                    cstr(
                        "getset setter returned failure without setting an exception"
                    ),
                )
            return result
        member = _find_member(current, attr)
        if not ptr_is_null(member):
            _type_error(cstr("read-only attribute"))
            return -1
        current = load_ptr(current, (264))
    dict_slot = pcc_capi_object_dict_slot(o, type_obj)
    if not ptr_is_null(dict_slot):
        dict_obj = pcc_gc_load_ptr(o, dict_slot)
        if ptr_is_null(dict_obj):
            dict_obj = py_dict_new()
            if ptr_is_null(dict_obj):
                return -1
            pcc_gc_store_ptr(o, dict_slot, dict_obj)
        py_dict_set(dict_obj, name, value)
        return 0
    py_raise_owned(py_exc_new(6, attr))  # PY_EXC_ATTRIBUTEERROR
    return -1


@c_abi_typed_export("PyObject_GenericGetDict", "ptr", ("ptr", "ptr"))
def PyObject_GenericGetDict(o, context) -> c_ptr:
    if ptr_is_null(o):
        _type_error(cstr("NULL object has no __dict__"))
        return null()
    type_obj = pcc_capi_cext_type_for_object(o)
    if ptr_is_null(type_obj):
        return py_obj_getattr(o, cstr("__dict__"))
    dict_slot = pcc_capi_object_dict_slot(o, type_obj)
    if ptr_is_null(dict_slot):
        py_raise_owned(py_exc_new(6, cstr("object has no __dict__")))  # PY_EXC_ATTRIBUTEERROR
        return null()
    dict_obj = pcc_gc_load_ptr(o, dict_slot)
    if not ptr_is_null(dict_obj):
        py_incref(dict_obj)
        return dict_obj
    dict_obj = py_dict_new()
    if ptr_is_null(dict_obj):
        return null()
    pcc_gc_store_ptr(o, dict_slot, dict_obj)
    return dict_obj


# --- pcc_capi_object_dict_slot / pcc_capi_member_get ----------------


@c_abi_typed_export("pcc_capi_object_dict_slot", "ptr", ("ptr", "ptr"))
def pcc_capi_object_dict_slot(o, type_obj) -> c_ptr:
    if ptr_is_null(o) or ptr_is_null(type_obj):
        return null()
    dictoffset: int = load_i64(type_obj, (296))
    if dictoffset <= 0:
        return null()
    if dictoffset + 8 > load_i64(type_obj, (40)):  # tp_basicsize
        return null()
    return ptr_add(o, dictoffset)


# PccCapiMemberDef: name@0, type@8(i32), offset@16, flags@24, doc@32
# T_* codes are CPython structmember.h values (see fake structmember.h):
# SHORT=0 INT=1 LONG=2 FLOAT=3 DOUBLE=4 STRING=5 OBJECT=6 CHAR=7 BYTE=8
# UBYTE=9 USHORT=10 UINT=11 ULONG=12 STRING_INPLACE=13 BOOL=14 OBJECT_EX=16
# LONGLONG=17 ULONGLONG=18 PYSSIZET=19 NONE=20


@c_abi_typed_export("pcc_capi_member_get", "ptr", ("ptr", "ptr"))
def pcc_capi_member_get(o, member) -> c_ptr:
    member_type: int = load_i32(member, 8)
    offset: int = load_i64(member, 16)
    address = ptr_add(o, offset)
    if member_type == 0:  # T_SHORT
        return PyLong_FromLong(_load_i16(address))
    if member_type == 1:  # T_INT
        return PyLong_FromLong(load_i32(address, 0))
    if member_type == 2:  # T_LONG
        return PyLong_FromLong(load_i64(address, 0))
    if member_type == 3:  # T_FLOAT
        return PyFloat_FromDouble(_f32_bits_to_f64(load_i32(address, 0)))
    if member_type == 4:  # T_DOUBLE
        return PyFloat_FromDouble(load_f64(address, 0))
    if member_type == 5:  # T_STRING
        value = load_ptr(address, 0)
        if ptr_is_null(value):
            py_raise_owned(py_exc_new(6, load_ptr(member, 0)))  # PY_EXC_ATTRIBUTEERROR
            return null()
        return PyUnicode_FromString(value)
    if member_type == 6 or member_type == 16:  # T_OBJECT / T_OBJECT_EX
        value = pcc_gc_load_ptr(o, address)
        if ptr_is_null(value) and member_type == 16:
            py_raise_owned(py_exc_new(6, load_ptr(member, 0)))  # PY_EXC_ATTRIBUTEERROR
            return null()
        if ptr_is_null(value):
            value = global_load_ptr("py_None")
        py_incref(value)
        return value
    if member_type == 7:  # T_CHAR
        return PyUnicode_FromStringAndSize(address, 1)
    if member_type == 8:  # T_BYTE
        return PyLong_FromLong(_load_i8_signed(address))
    if member_type == 9:  # T_UBYTE
        return PyLong_FromLong(load_i8(address, 0))
    if member_type == 10:  # T_USHORT
        return PyLong_FromLong(_load_u16(address))
    if member_type == 11:  # T_UINT (u32 zero-extends into i64)
        return PyLong_FromUnsignedLong(load_i32(address, 0) & 0xFFFFFFFF)
    if member_type == 12:  # T_ULONG
        return PyLong_FromUnsignedLong(load_i64(address, 0))
    if member_type == 13:  # T_STRING_INPLACE
        return PyUnicode_FromString(address)
    if member_type == 14:  # T_BOOL (char-sized)
        v: int = load_i8(address, 0)
        return PyBool_FromLong(1 if v != 0 else 0)
    if member_type == 17:  # T_LONGLONG
        return PyLong_FromLong(load_i64(address, 0))
    if member_type == 18:  # T_ULONGLONG
        return PyLong_FromUnsignedLong(load_i64(address, 0))
    if member_type == 19:  # T_PYSSIZET
        return PyLong_FromLong(load_i64(address, 0))
    if member_type == 20:  # T_NONE
        none = global_load_ptr("py_None")
        py_incref(none)
        return none
    _type_error(cstr("unknown member type"))
    return null()


def _f32_bits_to_f64(bits: int) -> float:
    # Reinterpret an IEEE-754 single (in an i32 lane) as f64 by bit manipulation.
    sign: int = (bits >> 31) & 1
    exp: int = (bits >> 23) & 0xFF
    mant: int = bits & 0x7FFFFF
    result: float = 0.0
    if exp == 0:
        if mant == 0:
            result = 0.0
        else:
            result = i64_to_float(mant) / 8388608.0 * 2.0**-126
    elif exp == 255:
        result = 0.0  # inf/nan not expected here
    else:
        result = (1.0 + i64_to_float(mant) / 8388608.0) * 2.0**(exp - 127)
    if sign != 0:
        result = 0.0 - result
    return result


def _load_i16(p) -> int:
    lo: int = load_i8(p, 0) & 0xFF
    hi: int = load_i8(p, 1) & 0xFF
    v: int = lo | (hi << 8)
    if v >= 32768:
        v = v - 65536
    return v


def _load_u16(p) -> int:
    lo: int = load_i8(p, 0) & 0xFF
    hi: int = load_i8(p, 1) & 0xFF
    return lo | (hi << 8)


def _load_i8_signed(p) -> int:
    v: int = load_i8(p, 0)
    if v >= 128:
        v = v - 256
    return v


# --- _Py_HashDouble --------------------------------------------------

pcc_hash_f64_bits = extern("pcc_hash_f64_bits", (c_int64, c_ptr), c_int64)


@c_abi_typed_export("_Py_HashDouble", "i64", ("ptr", "f64"))
def _Py_HashDouble(inst, v: float) -> int:
    # Python/pyhash.c: the value modulo 2**61 - 1, +-314159 for infinities and
    # the identity hash of ``inst`` for NaN.  The numeric hash lives beside
    # ``py_obj_hash``; the old local loop hashed ``|v|`` instead of frexp's
    # mantissa, so ``_Py_HashDouble(NULL, 2.0)`` was 8 rather than hash(2).
    return pcc_hash_f64_bits(f64_bits(v), inst)
