"""pcc-Python port of py_dunder.c."""

__pcc_runtime_port__ = True

from pcc.py_runtime.py.py_abi_constants import (
    PYINSTANCEOBJECT_CLS_OFFSET,
    PYOBJECTHEADER_FLAGS_OFFSET,
    PY_TYPE_CLASS,
    PY_TYPE_FUNC,
    PY_TYPE_GEN,
    PY_TYPE_INSTANCE,
    PY_TYPE_INT,
    PY_TYPE_STATICMETHOD,
    PY_TYPE_STR,
    PY_TYPE_USER_CLASS_START,
    PY_TYPE_WEAKREF,
)

from pcc.extern import extern, c_abi_export, c_int32, c_int64, c_ptr, c_void
from pcc.unsafe import (
    call_void_ptr1,
    call_ptr1,
    cstr,
    free,
    global_addr,
    global_load_ptr,
    is_tagged_int,
    load_i8,
    load_i32,
    load_ptr,
    malloc,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    store_i8,
    store_i32,
    store_i64,
    untag_int,
)


py_bigint_from_any = extern("py_bigint_from_any", (c_ptr,), c_ptr)
py_bigint_to_cstr = extern("py_bigint_to_cstr", (c_ptr,), c_ptr)
py_bigint_to_base_cstr = extern("py_bigint_to_base_cstr", (c_ptr, c_int32, c_int32), c_ptr)
py_class_lookup = extern("py_class_lookup", (c_ptr, c_ptr), c_ptr)
pcc_capi_is_cext_type_tag = extern("pcc_capi_is_cext_type_tag", (c_int64,), c_int64)
py_bool_from_bit = extern("py_bool_from_bit", (c_int32,), c_ptr)
py_clear_exception = extern("py_clear_exception", (), c_void)
py_current_exception = extern("py_current_exception", (), c_ptr)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_runtime_error_if_unset = extern(
    "py_runtime_error_if_unset", (c_ptr, c_ptr), c_ptr
)
py_int_to_i64 = extern("py_int_to_i64", (c_ptr, c_ptr), c_int64)
py_incref = extern("py_incref", (c_ptr,), c_void)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_str_new = extern("py_str_new", (c_ptr, c_int64), c_ptr)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_tuple_set_item = extern("py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void)
py_func_call = extern("py_func_call", (c_ptr, c_ptr), c_ptr)
py_gen_finalize = extern("py_gen_finalize", (c_ptr,), c_void)
pcc_gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_ptr)
strlen = extern("strlen", (c_ptr,), c_int64)
pcc_runtime_log_event_code = extern("pcc_runtime_log_event_code", (c_int32, c_int32, c_int64, c_int64, c_ptr), c_void)
pcc_gc_backend = extern("pcc_gc_backend", (), c_int64)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
py_tls_exc_set = extern("py_tls_exc_set", (c_ptr,), c_void)


def _type_of(obj) -> int:
    if is_tagged_int(obj):
        return PY_TYPE_INT
    return load_i32(obj, 8)


def _load_instance_cls(o):
    backend: int = pcc_gc_backend()
    if backend == 3 or backend == 4:
        return pcc_gc_load_ptr(o, ptr_add(o, PYINSTANCEOBJECT_CLS_OFFSET))
    return load_ptr(o, PYINSTANCEOBJECT_CLS_OFFSET)


def _dunder_require_result(result, helper_name, message):
    if ptr_is_null(result):
        py_runtime_error_if_unset(helper_name, message)
    return result


def _call_user_unary_method(func, self_obj):
    # A NULL lookup is a deliberate "dunder not defined" sentinel.  Any NULL
    # after selecting a method is an error result.
    if ptr_is_null(func):
        return null()
    if is_tagged_int(func):
        return call_ptr1(func, self_obj)
    if load_i32(func, 8) == PY_TYPE_FUNC:  # PY_TYPE_FUNC
        args = py_tuple_new(1)
        if ptr_is_null(args):
            return _dunder_require_result(
                null(),
                cstr("py_tuple_new"),
                cstr("user dunder argument tuple allocation failed"),
            )
        py_tuple_set_item(args, 0, self_obj)
        out = py_func_call(func, args)
        _dunder_require_result(
            out,
            cstr("user dunder call"),
            cstr("user dunder callback returned NULL without an exception"),
        )
        py_decref(args)
        return out
    return _dunder_require_result(
        call_ptr1(func, self_obj),
        cstr("user dunder call"),
        cstr("user dunder callback returned NULL without an exception"),
    )


def _call_user_unary_method_void(func, self_obj) -> None:
    if ptr_is_null(func):
        return
    if is_tagged_int(func):
        call_void_ptr1(func, self_obj)
        return
    if load_i32(func, 8) == PY_TYPE_FUNC:  # PY_TYPE_FUNC
        result = _call_user_unary_method(func, self_obj)
        if ptr_is_null(result) == 0:
            py_decref(result)
        return
    call_void_ptr1(func, self_obj)


def _tagged_int_to_str_obj(o):
    v: int = untag_int(o)
    mag: int = v
    neg: int = 0
    if v < 0:
        neg = 1
        mag = 0 - v

    digits: int = 1
    if mag < 10:
        digits = 1
    elif mag < 100:
        digits = 2
    elif mag < 1000:
        digits = 3
    else:
        tmp: int = mag
        while tmp >= 10:
            tmp = tmp // 10
            digits = digits + 1

    byte_len: int = digits + neg
    out = pcc_gc_alloc(40 + byte_len + 1, PY_TYPE_STR, 0)
    if ptr_is_null(out):
        return null()
    store_i64(out, 0, 1)             # refcount
    store_i32(out, 8, PY_TYPE_STR)             # PY_TYPE_STR
    store_i64(out, 16, byte_len)     # byte_len
    store_i64(out, 24, -1)           # cp_len
    store_i64(out, 32, -1)           # hash
    store_i8(out, 40 + byte_len, 0)  # NUL terminator

    pos: int = byte_len
    while True:
        pos = pos - 1
        store_i8(out, 40 + pos, 48 + (mag % 10))
        mag = mag // 10
        if mag == 0:
            break
    if neg != 0:
        pos = pos - 1
        store_i8(out, 40 + pos, 45)
    return out


@c_abi_export("py_int_to_str_obj")
def py_int_to_str_obj(o):
    if ptr_is_null(o):
        return null()
    if is_tagged_int(o):
        return _tagged_int_to_str_obj(o)
    if _type_of(o) != PY_TYPE_INT:
        return null()
    b = py_bigint_from_any(o)
    if ptr_is_null(b):
        return null()
    raw = py_bigint_to_cstr(b)
    free(b)
    if ptr_is_null(raw):
        return null()
    out = py_str_new(raw, strlen(raw))
    free(raw)
    return out


def _store_rev_hex_digits(rev, mag: int) -> int:
    ndigits: int = 0
    while True:
        digit: int = mag & 15
        ch: int = 0
        if digit < 10:
            ch = 48 + digit
        else:
            ch = 97 + digit - 10
        store_i8(rev, ndigits, ch)
        ndigits = ndigits + 1
        mag = mag >> 4
        if mag == 0 or ndigits >= 32:
            break
    return ndigits


def _store_min_i64_hex_digits(rev) -> int:
    i: int = 0
    while i < 15:
        store_i8(rev, i, 48)
        i = i + 1
    store_i8(rev, 15, 56)
    return 16


@c_abi_export("py_int_format_hex")
def py_int_format_hex(o, width: int, zero_pad: int):
    overflow = malloc(4)
    if ptr_is_null(overflow):
        return null()
    store_i32(overflow, 0, 0)
    v: int = py_int_to_i64(o, overflow)
    overflowed: int = load_i32(overflow, 0)
    free(overflow)
    if overflowed != 0:
        return py_int_to_str_obj(o)

    neg: int = 0
    mag: int = v
    min_i64: int = -9223372036854775807
    min_i64 = min_i64 - 1
    if v < 0:
        neg = 1
        if v != min_i64:
            mag = 0 - v

    rev = malloc(32)
    if ptr_is_null(rev):
        return null()
    ndigits: int = 0
    if v == min_i64:
        ndigits = _store_min_i64_hex_digits(rev)
    else:
        ndigits = _store_rev_hex_digits(rev, mag)

    if width < 0:
        width = 0
    if width > 120:
        width = 120
    min_len: int = ndigits + neg
    pad: int = width - min_len
    if pad < 0:
        pad = 0

    buf = malloc(128)
    if ptr_is_null(buf):
        free(rev)
        return null()
    pos: int = 0
    if neg != 0 and zero_pad != 0:
        store_i8(buf, pos, 45)
        pos = pos + 1
    pad_ch: int = 32
    if zero_pad != 0:
        pad_ch = 48
    i: int = 0
    while i < pad and pos < 128:
        store_i8(buf, pos, pad_ch)
        pos = pos + 1
        i = i + 1
    if neg != 0 and zero_pad == 0 and pos < 128:
        store_i8(buf, pos, 45)
        pos = pos + 1
    i = ndigits - 1
    while i >= 0 and pos < 128:
        store_i8(buf, pos, load_i8(rev, i) & 0xFF)
        pos = pos + 1
        i = i - 1
    out = py_str_new(buf, pos)
    free(buf)
    free(rev)
    return out


@c_abi_export("py_int_format_decimal")
def py_int_format_decimal(o, width: int, zero_pad: int, comma: int):
    overflow = malloc(4)
    if ptr_is_null(overflow):
        return null()
    store_i32(overflow, 0, 0)
    v: int = py_int_to_i64(o, overflow)
    overflowed: int = load_i32(overflow, 0)
    free(overflow)
    if overflowed != 0:
        return py_int_to_str_obj(o)

    neg: int = 0
    mag: int = v
    min_i64: int = -9223372036854775807
    min_i64 = min_i64 - 1
    if v < 0:
        neg = 1
        if v == min_i64:
            return py_int_to_str_obj(o)
        mag = 0 - v

    rev = malloc(32)
    if ptr_is_null(rev):
        return null()
    ndigits: int = 0
    while True:
        digit: int = mag % 10
        store_i8(rev, ndigits, 48 + digit)
        ndigits = ndigits + 1
        mag = mag // 10
        if mag == 0 or ndigits >= 32:
            break

    comma_count: int = 0
    if comma != 0 and ndigits > 3:
        comma_count = (ndigits - 1) // 3
    if width < 0:
        width = 0
    if width > 120:
        width = 120
    min_len: int = ndigits + comma_count + neg
    pad: int = width - min_len
    if pad < 0:
        pad = 0

    buf = malloc(160)
    if ptr_is_null(buf):
        free(rev)
        return null()
    pos: int = 0
    if neg != 0 and zero_pad != 0:
        store_i8(buf, pos, 45)
        pos = pos + 1
    pad_ch: int = 32
    if zero_pad != 0:
        pad_ch = 48
    i: int = 0
    while i < pad and pos < 160:
        store_i8(buf, pos, pad_ch)
        pos = pos + 1
        i = i + 1
    if neg != 0 and zero_pad == 0 and pos < 160:
        store_i8(buf, pos, 45)
        pos = pos + 1
    i = ndigits - 1
    while i >= 0 and pos < 160:
        store_i8(buf, pos, load_i8(rev, i) & 0xFF)
        pos = pos + 1
        if comma != 0 and i > 0 and (i % 3) == 0 and pos < 160:
            store_i8(buf, pos, comma & 0xFF)
            pos = pos + 1
        i = i - 1
    out = py_str_new(buf, pos)
    free(buf)
    free(rev)
    return out


def _int_based_repr(o, base: int, prefix_ch: int):
    # bin()/hex()/oct() shared body, mirrors py_dunder.c::py_int_based_repr.
    # Negatives use half = -(v+1) (always fits i64, even min_i64) then a +1
    # carry on the digit string, so min_i64 is handled without overflow.
    overflow = malloc(4)
    if ptr_is_null(overflow):
        return null()
    store_i32(overflow, 0, 0)
    v: int = py_int_to_i64(o, overflow)
    overflowed: int = load_i32(overflow, 0)
    free(overflow)
    if overflowed != 0:
        # Bignum exceeding i64: full base-N conversion (was: wrongly returned
        # the DECIMAL value -> the C<->port drift; C raised). Mirrors
        # py_dunder.c py_int_based_repr.
        cbuf = py_bigint_to_base_cstr(o, base, prefix_ch)
        if ptr_is_null(cbuf):
            return null()
        slen: int = strlen(cbuf)
        out2 = py_str_new(cbuf, slen)
        free(cbuf)
        return out2
    neg: int = 0
    half: int = v
    add_one: int = 0
    if v < 0:
        neg = 1
        half = 0 - (v + 1)
        add_one = 1
    rev = malloc(72)   # digit VALUES (not chars), so the carry is easy
    if ptr_is_null(rev):
        return null()
    nd: int = 0
    done: int = 0
    while done == 0:
        d: int = half % base
        store_i8(rev, nd, d)
        nd = nd + 1
        half = half // base
        if half == 0:
            done = 1
        if nd >= 72:
            done = 1
    if add_one != 0:
        carry: int = 1
        ci: int = 0
        while ci < nd and carry != 0:
            dv: int = (load_i8(rev, ci) & 0xFF) + 1
            if dv >= base:
                store_i8(rev, ci, 0)
            else:
                store_i8(rev, ci, dv)
                carry = 0
            ci = ci + 1
        if carry != 0 and nd < 72:
            store_i8(rev, nd, 1)
            nd = nd + 1
    buf = malloc(96)
    if ptr_is_null(buf):
        free(rev)
        return null()
    pos: int = 0
    if neg != 0:
        store_i8(buf, pos, 45)   # '-'
        pos = pos + 1
    store_i8(buf, pos, 48)       # '0'
    pos = pos + 1
    store_i8(buf, pos, prefix_ch)
    pos = pos + 1
    i: int = nd - 1
    while i >= 0:
        dv2: int = load_i8(rev, i) & 0xFF
        ch: int = 48 + dv2       # '0' + d
        if dv2 >= 10:
            ch = 97 + dv2 - 10   # 'a' + (d - 10)
        store_i8(buf, pos, ch)
        pos = pos + 1
        i = i - 1
    out = py_str_new(buf, pos)
    free(buf)
    free(rev)
    return out


@c_abi_export("py_builtin_bin")
def py_builtin_bin(o):
    return _int_based_repr(o, 2, 98)    # 'b'


@c_abi_export("py_builtin_hex")
def py_builtin_hex(o):
    return _int_based_repr(o, 16, 120)  # 'x'


@c_abi_export("py_builtin_oct")
def py_builtin_oct(o):
    return _int_based_repr(o, 8, 111)   # 'o'


@c_abi_export("py_builtin_callable")
def py_builtin_callable(o):
    # callable(x): mirror py_obj_call's dispatch classification. Functions
    # (tag 9), classes (tag 10) and weakrefs (tag 21) are callable; an
    # instance is callable iff its class defines __call__. Tagged ints, None
    # and any other type tag are not callable.
    if ptr_is_null(o):
        return py_bool_from_bit(0)
    if is_tagged_int(o):
        return py_bool_from_bit(0)
    tag: int = load_i32(o, 8)
    if tag == PY_TYPE_FUNC or tag == PY_TYPE_CLASS or tag == PY_TYPE_WEAKREF or tag == PY_TYPE_STATICMETHOD:
        return py_bool_from_bit(1)
    if tag == PY_TYPE_INSTANCE or tag >= PY_TYPE_USER_CLASS_START:
        cls = _load_instance_cls(o)
        method = py_class_lookup(cls, cstr("__call__"))
        if ptr_is_null(method):
            return py_bool_from_bit(0)
        return py_bool_from_bit(1)
    return py_bool_from_bit(0)


@c_abi_export("py_user_str_dispatch")
def py_user_str_dispatch(o):
    if ptr_is_null(o):
        return null()
    if is_tagged_int(o):
        return null()
    tag: int = load_i32(o, 8)
    if tag != PY_TYPE_INSTANCE and tag < PY_TYPE_USER_CLASS_START:
        return null()
    cls = _load_instance_cls(o)
    if ptr_is_null(cls):
        return null()
    func = py_class_lookup(cls, cstr("__str__"))
    if ptr_is_null(func):
        return null()
    return _call_user_unary_method(func, o)


@c_abi_export("py_user_repr_dispatch")
def py_user_repr_dispatch(o):
    if ptr_is_null(o):
        return null()
    if is_tagged_int(o):
        return null()
    tag: int = load_i32(o, 8)
    if tag != PY_TYPE_INSTANCE and tag < PY_TYPE_USER_CLASS_START:
        return null()
    cls = _load_instance_cls(o)
    if ptr_is_null(cls):
        return null()
    func = py_class_lookup(cls, cstr("__repr__"))
    if ptr_is_null(func):
        return null()
    return _call_user_unary_method(func, o)


@c_abi_export("py_user_hash_dispatch")
def py_user_hash_dispatch(o, handled) -> int:
    if ptr_is_null(handled) == 0:
        store_i64(handled, 0, 0)
    if ptr_is_null(o):
        return 0
    if is_tagged_int(o):
        return 0
    tag: int = load_i32(o, 8)
    if tag != PY_TYPE_INSTANCE and tag < PY_TYPE_USER_CLASS_START:
        return 0
    cls = _load_instance_cls(o)
    if ptr_is_null(cls):
        return 0
    func = py_class_lookup(cls, cstr("__hash__"))
    if ptr_is_null(func):
        return 0
    if ptr_eq(func, global_load_ptr("py_None")) != 0:
        if ptr_is_null(handled) == 0:
            store_i64(handled, 0, 1)
        py_raise_owned(py_exc_new(3, cstr("unhashable type")))
        return 0
    result = _call_user_unary_method(func, o)
    if ptr_is_null(handled) == 0:
        store_i64(handled, 0, 1)
    if ptr_is_null(result):
        return 0
    overflow = malloc(4)
    if ptr_is_null(overflow):
        py_decref(result)
        return 0
    store_i32(overflow, 0, 0)
    value: int = py_int_to_i64(result, overflow)
    overflowed: int = load_i32(overflow, 0)
    free(overflow)
    py_decref(result)
    if overflowed != 0:
        return 0
    if value == -1:
        return -2
    return value


@c_abi_export("py_user_iter_dispatch")
def py_user_iter_dispatch(o):
    if ptr_is_null(o):
        return null()
    if is_tagged_int(o):
        return null()
    tag: int = load_i32(o, 8)
    if tag != PY_TYPE_INSTANCE and tag < PY_TYPE_USER_CLASS_START:
        return null()
    cls = _load_instance_cls(o)
    if ptr_is_null(cls):
        return null()
    func = py_class_lookup(cls, cstr("__iter__"))
    if ptr_is_null(func):
        return null()
    return _call_user_unary_method(func, o)


@c_abi_export("py_user_next_dispatch")
def py_user_next_dispatch(o):
    if ptr_is_null(o):
        return null()
    if is_tagged_int(o):
        return null()
    tag: int = load_i32(o, 8)
    if tag != PY_TYPE_INSTANCE and tag < PY_TYPE_USER_CLASS_START:
        return null()
    cls = _load_instance_cls(o)
    if ptr_is_null(cls):
        return null()
    func = py_class_lookup(cls, cstr("__next__"))
    if ptr_is_null(func):
        return null()
    return _call_user_unary_method(func, o)


@c_abi_export("py_user_del_dispatch")
def py_user_del_dispatch(o) -> None:
    if ptr_is_null(o):
        return
    if is_tagged_int(o):
        return
    tag: int = load_i32(o, 8)
    if tag == PY_TYPE_GEN:
        py_gen_finalize(o)
        return
    if tag != PY_TYPE_INSTANCE and tag < PY_TYPE_USER_CLASS_START:
        return
    if pcc_capi_is_cext_type_tag(tag) != 0:
        return
    # No class in this process ever defined ``__del__``: the MRO lookup below
    # (string hash + per-class dict probes) cannot find one.  This was the
    # largest single cost of freeing a plain instance.
    if load_i32(global_addr("pcc_class_del_defined_count"), 0) == 0:
        return
    flags: int = load_i32(o, PYOBJECTHEADER_FLAGS_OFFSET)
    if (flags & 4) != 0:
        pcc_runtime_log_event_code(5, 4, tag, 1, o)
        return
    cls = _load_instance_cls(o)
    if ptr_is_null(cls):
        return
    func = py_class_lookup(cls, cstr("__del__"))
    if ptr_is_null(func):
        return
    store_i32(o, PYOBJECTHEADER_FLAGS_OFFSET, flags | 4)
    saved_exc = py_current_exception()
    if ptr_is_null(saved_exc) == 0:
        py_incref(saved_exc)
        py_tls_exc_set(null())
    pcc_runtime_log_event_code(5, 2, tag, 0, o)
    _call_user_unary_method_void(func, o)
    pcc_runtime_log_event_code(5, 3, tag, 0, o)
    py_clear_exception()
    if ptr_is_null(saved_exc) == 0:
        py_tls_exc_set(saved_exc)
        py_decref(saved_exc)
