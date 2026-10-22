"""pcc-Python owners for the no-libpython PyArg_* surface.

Replaces the PyArg_ParseTuple / ParseTupleAndKeywords / UnpackTuple /
VaParseTupleAndKeywords + pcc_capi_parse_one / format_counts / is_parse_code /
skip_parse_dest block of py_capi_shim.c.  Variadic forms consume a va_list
via the pcc.unsafe va_* intrinsics; dest pointers are read with va_arg_ptr and
written with the matching store intrinsic.

Owned surface (stable C ABI names):

  PyArg_ParseTuple, PyArg_ParseTupleAndKeywords, PyArg_UnpackTuple,
  PyArg_VaParseTupleAndKeywords

Public object type tags come from the generated ``py_abi_constants`` module.
Private exception codes remain owned by the C-API argument contract:
  PY_EXC_TYPEERROR = 3, PY_EXC_VALUEERROR = 2
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import PY_TYPE_BYTES, PY_TYPE_DICT, PY_TYPE_STR, PY_TYPE_TUPLE

from pcc.extern import (
    c_abi_typed_export,
    c_abi_variadic_export,
    c_int64,
    c_ptr,
    c_void,
    extern,
)
from pcc.unsafe import (
    call_i64_ptr2,
    cstr,
    global_load_ptr,
    is_tagged_int,
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
    store_i64,
    store_ptr,
    va_arg_ptr,
    va_cursor,
    va_end,
    va_start,
)

py_type_of = extern("pcc_py_type_of", (c_ptr,), c_int64)
py_tuple_len = extern("py_tuple_len", (c_ptr,), c_int64)
py_tuple_get = extern("py_tuple_get", (c_ptr, c_int64), c_ptr)
py_int_to_i64 = extern("py_int_to_i64", (c_ptr, c_ptr), c_int64)
py_str_utf8 = extern("py_str_utf8", (c_ptr,), c_ptr)
py_str_byte_len = extern("py_str_byte_len", (c_ptr,), c_int64)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
PyObject_IsTrue = extern("PyObject_IsTrue", (c_ptr,), c_int64)
PyDict_GetItemString = extern("PyDict_GetItemString", (c_ptr, c_ptr), c_ptr)
pcc_capi_typecheck = extern("pcc_capi_typecheck", (c_ptr, c_ptr), c_int64)


def _type_error(message) -> None:
    py_raise_owned(py_exc_new(3, message))  # PY_EXC_TYPEERROR


def _value_error(message) -> None:
    py_raise_owned(py_exc_new(2, message))  # PY_EXC_VALUEERROR


# NOTE: never wrap stack_alloc in a helper that returns it -- the allocation
# lives in the helper's own frame and is dangling after return (this
# corrupted PyArg_ParseTupleAndKeywords' caller frame under numpy).




def _va_cursor_pass(cursor) -> c_ptr:
    return cursor


def _is_parse_code(c: int) -> int:
    if (
        c == 108 or c == 105 or c == 110 or c == 112  # l i n p
        or c == 79 or c == 115 or c == 121  # O s y
    ):
        return 1
    return 0


def _parse_one(item, code: int, object_modifier: int, cursor) -> int:
    if code == 108:  # 'l'
        out = va_arg_ptr(cursor)
        # py_int_to_i64's overflow out-param is a C `int*`: allocate and read
        # exactly 4 bytes. Reading 8 picked up uninitialized stack garbage and
        # reported a bogus overflow for every 'l'/'i'/'n' argument.
        ov_slot = stack_alloc(4)
        store_i32(ov_slot, 0, 0)
        value = py_int_to_i64(item, ov_slot)
        if load_i32(ov_slot, 0) != 0:
            return 0
        store_i64(out, 0, value)
        return 1
    if code == 105:  # 'i'
        out = va_arg_ptr(cursor)
        # py_int_to_i64's overflow out-param is a C `int*`: allocate and read
        # exactly 4 bytes. Reading 8 picked up uninitialized stack garbage and
        # reported a bogus overflow for every 'l'/'i'/'n' argument.
        ov_slot = stack_alloc(4)
        store_i32(ov_slot, 0, 0)
        value = py_int_to_i64(item, ov_slot)
        if load_i32(ov_slot, 0) != 0:
            return 0
        if value < -2147483648 or value > 2147483647:
            return 0
        store_i32(out, 0, value)
        return 1
    if code == 110:  # 'n'
        out = va_arg_ptr(cursor)
        # py_int_to_i64's overflow out-param is a C `int*`: allocate and read
        # exactly 4 bytes. Reading 8 picked up uninitialized stack garbage and
        # reported a bogus overflow for every 'l'/'i'/'n' argument.
        ov_slot = stack_alloc(4)
        store_i32(ov_slot, 0, 0)
        value = py_int_to_i64(item, ov_slot)
        if load_i32(ov_slot, 0) != 0:
            return 0
        store_i64(out, 0, value)
        return 1
    if code == 112:  # 'p'
        out = va_arg_ptr(cursor)
        truth = PyObject_IsTrue(item)
        if truth < 0:
            return 0
        store_i32(out, 0, 1 if truth != 0 else 0)
        return 1
    if code == 79:  # 'O'
        if object_modifier == 33:  # '!'
            expected = va_arg_ptr(cursor)
            out = va_arg_ptr(cursor)
            if pcc_capi_typecheck(item, expected) == 0:
                return 0
            store_ptr(out, 0, item)
            return 1
        if object_modifier == 38:  # '&'
            converter = va_arg_ptr(cursor)
            out = va_arg_ptr(cursor)
            if ptr_is_null(converter):
                return 0
            result = call_i64_ptr2(converter, item, out)
            if result == 0:
                return 0
            return 1
        out = va_arg_ptr(cursor)
        store_ptr(out, 0, item)
        return 1
    if code == 115:  # 's'
        out = va_arg_ptr(cursor)
        if ptr_is_null(item) or is_tagged_int(item):
            return 0
        if load_i32(item, 8) != PY_TYPE_STR:  # PY_TYPE_STR
            return 0
        store_ptr(out, 0, py_str_utf8(item))
        return 1
    if code == 121:  # 'y'
        out = va_arg_ptr(cursor)
        if ptr_is_null(item) or is_tagged_int(item):
            return 0
        if load_i32(item, 8) != PY_TYPE_BYTES:  # PY_TYPE_BYTES
            return 0
        store_ptr(out, 0, ptr_add(item, 24))  # PyBytesObject data
        return 1
    return 0


def _parse_one_hash(item, code: int, cursor) -> int:
    # 's#' / 'y#' — str/bytes + length
    out = va_arg_ptr(cursor)
    len_out = va_arg_ptr(cursor)
    if code == 115:  # 's'
        if ptr_is_null(item) or is_tagged_int(item):
            return 0
        if load_i32(item, 8) != PY_TYPE_STR:
            return 0
        store_ptr(out, 0, py_str_utf8(item))
        store_i64(len_out, 0, py_str_byte_len(item))
        return 1
    if code == 121:  # 'y'
        if ptr_is_null(item) or is_tagged_int(item):
            return 0
        if load_i32(item, 8) != PY_TYPE_BYTES:
            return 0
        store_ptr(out, 0, ptr_add(item, 24))
        store_i64(len_out, 0, load_i64(item, 16))  # byte_len
        return 1
    return 0


def _skip_parse_dest(code: int, object_modifier: int, has_hash: int, cursor) -> None:
    if has_hash != 0 and (code == 115 or code == 121):
        va_arg_ptr(cursor)
        va_arg_ptr(cursor)
        return
    if code == 108 or code == 110:
        va_arg_ptr(cursor)
    elif code == 105 or code == 112:
        va_arg_ptr(cursor)
    elif code == 79 and object_modifier == 33:
        va_arg_ptr(cursor)
        va_arg_ptr(cursor)
    elif code == 79 and object_modifier == 38:
        va_arg_ptr(cursor)
        va_arg_ptr(cursor)
    elif code == 79:
        va_arg_ptr(cursor)
    elif code == 115 or code == 121:
        va_arg_ptr(cursor)


def _format_counts(format, required_ptr, total_ptr) -> None:
    req: int = 0
    all_count: int = 0
    optional: int = 0
    i: int = 0
    while True:
        c: int = load_i8(format, i)
        if c == 0:
            break
        if c == 58 or c == 59:  # ':' ';'
            break
        if c == 124:  # '|'
            optional = 1
            i += 1
            continue
        if _is_parse_code(c) != 0:
            all_count += 1
            if optional == 0:
                req += 1
            nxt: int = load_i8(format, i + 1)
            if (c == 115 or c == 121) and nxt == 35:  # s# y#
                i += 1
            elif c == 79 and (nxt == 33 or nxt == 38):  # O! O&
                i += 1
        i += 1
    if not ptr_is_null(required_ptr):
        store_i32(required_ptr, 0, req)
    if not ptr_is_null(total_ptr):
        store_i32(total_ptr, 0, all_count)


@c_abi_variadic_export("PyArg_ParseTuple")
def PyArg_ParseTuple(args, format) -> int:
    if ptr_is_null(args) or is_tagged_int(args) or py_type_of(args) != PY_TYPE_TUPLE:
        _type_error(cstr("expected argument tuple"))
        return 0
    req_slot = stack_alloc(4)
    tot_slot = stack_alloc(4)
    _format_counts(format, req_slot, tot_slot)
    nargs = py_tuple_len(args)
    if nargs < load_i32(req_slot, 0) or nargs > load_i32(tot_slot, 0):
        _type_error(cstr("argument count mismatch"))
        return 0
    cursor = va_start()
    index: int = 0
    ok: int = 1
    i: int = 0
    while True:
        c: int = load_i8(format, i)
        if c == 0:
            break
        if c == 58 or c == 59:  # ':' ';'
            break
        if c == 124:  # '|'
            i += 1
            continue
        if _is_parse_code(c) == 0:
            i += 1
            continue
        object_modifier: int = 0
        nxt: int = load_i8(format, i + 1)
        if c == 79 and (nxt == 33 or nxt == 38):
            object_modifier = nxt
        has_hash: int = 0
        if (c == 115 or c == 121) and nxt == 35:
            has_hash = 1
        if index < nargs:
            item = py_tuple_get(args, index)
            if has_hash != 0:
                parsed = _parse_one_hash(item, c, cursor)
            else:
                parsed = _parse_one(item, c, object_modifier, cursor)
            py_decref(item)
            if parsed == 0:
                ok = 0
                break
        else:
            _skip_parse_dest(c, object_modifier, has_hash, cursor)
        index += 1
        if has_hash != 0 or object_modifier != 0:
            i += 1
        i += 1
    va_end(cursor)
    if ok == 0:
        _type_error(cstr("argument type mismatch"))
        return 0
    return 1


@c_abi_typed_export("PyArg_VaParseTupleAndKeywords", "i32", ("ptr", "ptr", "ptr", "ptr", "ptr"))
def PyArg_VaParseTupleAndKeywords(args, kwargs, format, kwlist, ap) -> int:
    if ptr_is_null(args) or is_tagged_int(args) or py_type_of(args) != PY_TYPE_TUPLE:
        _type_error(cstr("expected argument tuple"))
        return 0
    if not ptr_is_null(kwargs) and not ptr_eq(kwargs, global_load_ptr("py_None")):
        if is_tagged_int(kwargs) or py_type_of(kwargs) != PY_TYPE_DICT:
            _type_error(cstr("expected keyword dict"))
            return 0
    req_slot = stack_alloc(4)
    tot_slot = stack_alloc(4)
    _format_counts(format, req_slot, tot_slot)
    nargs = py_tuple_len(args)
    if nargs > load_i32(tot_slot, 0):
        _type_error(cstr("too many positional arguments"))
        return 0
    cursor = va_cursor(ap)
    index: int = 0
    ok: int = 1
    i: int = 0
    while True:
        c: int = load_i8(format, i)
        if c == 0:
            break
        if c == 58 or c == 59:
            break
        if c == 124:
            i += 1
            continue
        if _is_parse_code(c) == 0:
            i += 1
            continue
        object_modifier: int = 0
        nxt: int = load_i8(format, i + 1)
        if c == 79 and (nxt == 33 or nxt == 38):
            object_modifier = nxt
        has_hash: int = 0
        if (c == 115 or c == 121) and nxt == 35:
            has_hash = 1
        owned_item = null()
        item = null()
        if index < nargs:
            owned_item = py_tuple_get(args, index)
            item = owned_item
        elif (
            not ptr_is_null(kwargs)
            and not ptr_eq(kwargs, global_load_ptr("py_None"))
            and not ptr_is_null(kwlist)
            and not ptr_is_null(load_ptr(kwlist, index * 8))
        ):
            item = PyDict_GetItemString(kwargs, load_ptr(kwlist, index * 8))
        if ptr_is_null(item):
            if index < load_i32(req_slot, 0):
                ok = 0
                if not ptr_is_null(owned_item):
                    py_decref(owned_item)
                break
            _skip_parse_dest(c, object_modifier, has_hash, cursor)
        else:
            if has_hash != 0:
                parsed = _parse_one_hash(item, c, cursor)
            else:
                parsed = _parse_one(item, c, object_modifier, cursor)
            if parsed == 0:
                ok = 0
        if not ptr_is_null(owned_item):
            py_decref(owned_item)
        if ok == 0:
            break
        index += 1
        if has_hash != 0 or object_modifier != 0:
            i += 1
        i += 1
    va_end(cursor)
    if ok == 0:
        _type_error(cstr("argument type mismatch"))
        return 0
    return 1


@c_abi_variadic_export("PyArg_ParseTupleAndKeywords")
def PyArg_ParseTupleAndKeywords(args, kwargs, format, kwlist) -> int:
    if ptr_is_null(args) or is_tagged_int(args) or py_type_of(args) != PY_TYPE_TUPLE:
        _type_error(cstr("expected argument tuple"))
        return 0
    if not ptr_is_null(kwargs) and not ptr_eq(kwargs, global_load_ptr("py_None")):
        if is_tagged_int(kwargs) or py_type_of(kwargs) != PY_TYPE_DICT:
            _type_error(cstr("expected keyword dict"))
            return 0
    req_slot = stack_alloc(4)
    tot_slot = stack_alloc(4)
    _format_counts(format, req_slot, tot_slot)
    nargs = py_tuple_len(args)
    if nargs > load_i32(tot_slot, 0):
        _type_error(cstr("too many positional arguments"))
        return 0
    cursor = va_start()
    index: int = 0
    ok: int = 1
    i: int = 0
    while True:
        c: int = load_i8(format, i)
        if c == 0:
            break
        if c == 58 or c == 59:
            break
        if c == 124:
            i += 1
            continue
        if _is_parse_code(c) == 0:
            i += 1
            continue
        object_modifier: int = 0
        nxt: int = load_i8(format, i + 1)
        if c == 79 and (nxt == 33 or nxt == 38):
            object_modifier = nxt
        has_hash: int = 0
        if (c == 115 or c == 121) and nxt == 35:
            has_hash = 1
        owned_item = null()
        item = null()
        if index < nargs:
            owned_item = py_tuple_get(args, index)
            item = owned_item
        elif (
            not ptr_is_null(kwargs)
            and not ptr_eq(kwargs, global_load_ptr("py_None"))
            and not ptr_is_null(kwlist)
            and not ptr_is_null(load_ptr(kwlist, index * 8))
        ):
            item = PyDict_GetItemString(kwargs, load_ptr(kwlist, index * 8))
        if ptr_is_null(item):
            if index < load_i32(req_slot, 0):
                ok = 0
                if not ptr_is_null(owned_item):
                    py_decref(owned_item)
                break
            _skip_parse_dest(c, object_modifier, has_hash, cursor)
        else:
            if has_hash != 0:
                parsed = _parse_one_hash(item, c, cursor)
            else:
                parsed = _parse_one(item, c, object_modifier, cursor)
            if parsed == 0:
                ok = 0
        if not ptr_is_null(owned_item):
            py_decref(owned_item)
        if ok == 0:
            break
        index += 1
        if has_hash != 0 or object_modifier != 0:
            i += 1
        i += 1
    va_end(cursor)
    if ok == 0:
        _type_error(cstr("argument type mismatch"))
        return 0
    return 1


@c_abi_variadic_export("PyArg_UnpackTuple")
def PyArg_UnpackTuple(args, name, min_count: int, max_count: int) -> int:
    if ptr_is_null(args) or is_tagged_int(args) or py_type_of(args) != PY_TYPE_TUPLE:
        _type_error(cstr("PyArg_UnpackTuple requires a tuple"))
        return 0
    n = py_tuple_len(args)
    if n < min_count or n > max_count:
        _type_error(cstr("PyArg_UnpackTuple: wrong number of arguments"))
        return 0
    cursor = va_start()
    index: int = 0
    while index < n:
        out = va_arg_ptr(cursor)
        item = py_tuple_get(args, index)
        store_ptr(out, 0, item)
        index += 1
    va_end(cursor)
    return 1
