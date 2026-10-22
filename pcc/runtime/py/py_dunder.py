"""pcc-Python port of py_dunder.c."""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import (
    C_POINTER_SIZE,
    PYINSTANCEOBJECT_CLS_OFFSET,
    PYOBJECTHEADER_FLAGS_OFFSET,
    PY_TYPE_BOOL,
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
    atomic_rmw_i32,
    call_void_ptr1,
    call_ptr1,
    cstr,
    free,
    global_addr,
    global_load_ptr,
    is_tagged_int,
    load_i8,
    load_i32,
    load_i64,
    load_ptr,
    malloc,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    ptr_to_int,
    stack_alloc,
    store_i8,
    store_i32,
    store_i64,
    store_ptr,
    untag_int,
)


py_bigint_from_any = extern("py_bigint_from_any", (c_ptr,), c_ptr)
py_bigint_to_cstr = extern("py_bigint_to_cstr", (c_ptr,), c_ptr)
py_bigint_to_base_cstr = extern("py_bigint_to_base_cstr", (c_ptr, c_int32, c_int32), c_ptr)
py_user_special_dispatch = extern(
    "py_user_special_dispatch",
    (c_ptr, c_ptr, c_ptr, c_ptr, c_int64, c_int64, c_ptr, c_ptr), c_ptr,
)
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
py_obj_index = extern("py_obj_index", (c_ptr,), c_ptr)
py_obj_format = extern("py_obj_format", (c_ptr, c_ptr), c_ptr)
py_obj_type_name = extern("py_obj_type_name", (c_ptr,), c_ptr)
py_str_utf8 = extern("py_str_utf8", (c_ptr,), c_ptr)
py_str_byte_len = extern("py_str_byte_len", (c_ptr,), c_int64)
py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_tuple_set_item = extern("py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void)
py_func_call = extern("py_func_call", (c_ptr, c_ptr), c_ptr)
py_gen_finalize = extern("py_gen_finalize", (c_ptr,), c_void)
pcc_gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_ptr)
strlen = extern("strlen", (c_ptr,), c_int64)
pcc_diagnostics_runtime_log_event_code = extern("pcc_diagnostics_runtime_log_event_code", (c_int32, c_int32, c_int64, c_int64, c_ptr), c_void)
pcc_gc_backend = extern("pcc_gc_backend", (), c_int64)
pcc_gc_object_id = extern("pcc_gc_object_id", (c_ptr,), c_int64)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
pcc_gc_unpin = extern("pcc_gc_unpin", (c_ptr,), c_void)
py_tls_exc_set = extern("py_tls_exc_set", (c_ptr,), c_void)
pcc_refcount_incref = extern("pcc_refcount_incref", (c_ptr,), c_int64)
pcc_refcount_decref = extern("pcc_refcount_decref", (c_ptr,), c_int64)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)
pcc_gc_foreign_lease_acquire = extern("pcc_gc_foreign_lease_acquire", (c_ptr,), c_int64)
pcc_gc_foreign_lease_release = extern("pcc_gc_foreign_lease_release", (c_ptr, c_int64), c_int64)
pcc_platform_abort = extern("pcc_platform_abort", (), c_void)

# One temporary owner keeps self alive if the finalizer drops every ordinary
# reference. Counted address leases are also collector roots: the production
# pcc_gc_gray_current_roots walk seeds pinned object-index entries. Keep this
# lease across the whole callback and its cleanup, independently of legacy
# pin-bit changes in nested calls. No frame retirement transfers this owner.


def _finalizer_owner_enter(slot: c_ptr) -> int:
    pcc_py_gc_minor_graph_lock()
    value = load_ptr(slot, 0)
    pcc_refcount_incref(value)
    flags: int = load_i32(value, PYOBJECTHEADER_FLAGS_OFFSET)
    # Match py_gen_finalize_from_dealloc: a real temporary owner revives the
    # terminal object for its callback. Ordinary terminal leases still fail.
    store_i32(value, PYOBJECTHEADER_FLAGS_OFFSET, flags & ~524288)
    token: int = pcc_gc_foreign_lease_acquire(slot)
    pcc_py_gc_minor_graph_unlock()
    if token < 0:
        pcc_platform_abort()
    return token


def _finalizer_owner_leave(slot: c_ptr, token: int, was_deallocating: int) -> None:
    pcc_py_gc_minor_graph_lock()
    value = load_ptr(slot, 0)
    status: int = pcc_gc_foreign_lease_release(slot, token)
    if status != 0:
        pcc_py_gc_minor_graph_unlock()
        pcc_platform_abort()
        return
    # Avoid recursive deallocation. Zero resumes the enclosing deallocator;
    # a positive count belongs to a real resurrection owner.
    remaining: int = pcc_refcount_decref(value)
    if was_deallocating != 0 and remaining == 0:
        flags: int = load_i32(value, PYOBJECTHEADER_FLAGS_OFFSET)
        store_i32(value, PYOBJECTHEADER_FLAGS_OFFSET, flags | 524288)
    store_ptr(slot, 0, null())
    pcc_py_gc_minor_graph_unlock()


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


def _int_format_spec(o, width: int, zero_pad: int, grouping: int, code: int):
    # f"{o:[0][width][grouping]<code>}" through py_obj_format, the one
    # CPython-compatible implementation: bignums print in the requested base
    # and zero padding is grouped ("0,001,234").  The i64-only bodies these
    # entry points used to have printed a bignum's hex format in DECIMAL.
    spec = stack_alloc(40)
    position: int = 0
    if zero_pad != 0:
        store_i8(spec, position, 48)
        position = position + 1
    if width > 0:
        digits = stack_alloc(24)
        count: int = 0
        value: int = width
        while value > 0:
            store_i8(digits, count, 48 + value % 10)
            value = value // 10
            count = count + 1
        while count > 0:
            count = count - 1
            store_i8(spec, position, load_i8(digits, count))
            position = position + 1
    if grouping != 0:
        store_i8(spec, position, grouping)
        position = position + 1
    store_i8(spec, position, code)
    position = position + 1
    spec_obj = py_str_new(spec, position)
    if ptr_is_null(spec_obj):
        return null()
    result = py_obj_format(o, spec_obj)
    py_decref(spec_obj)
    return result


@c_abi_export("py_int_format_hex")
def py_int_format_hex(o, width: int, zero_pad: int):
    return _int_format_spec(o, width, zero_pad, 0, 120)


@c_abi_export("py_int_format_decimal")
def py_int_format_decimal(o, width: int, zero_pad: int, comma: int):
    # ``comma`` is the grouping byte: 0, ',' (44) or '_' (95).
    return _int_format_spec(o, width, zero_pad, comma, 100)


def _raise_not_integer(o):
    # CPython: "'float' object cannot be interpreted as an integer".
    message = stack_alloc(160)
    length: int = 0
    store_i8(message, length, 39)
    length = length + 1
    name = py_obj_type_name(o)
    if not ptr_is_null(name):
        text = py_str_utf8(name)
        count: int = py_str_byte_len(name)
        if count > 100:
            count = 100
        index: int = 0
        while index < count:
            store_i8(message, length, load_i8(text, index))
            length = length + 1
            index = index + 1
        py_decref(name)
    suffix = cstr("' object cannot be interpreted as an integer")
    index2: int = 0
    while load_i8(suffix, index2) != 0:
        store_i8(message, length, load_i8(suffix, index2))
        length = length + 1
        index2 = index2 + 1
    store_i8(message, length, 0)
    py_raise_owned(py_exc_new(3, message))
    return null()


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
        if ptr_is_null(o) or _type_of(o) != PY_TYPE_INT:
            # Not an int (py_int_to_i64 flags every non-number): hex(1.5) is
            # a TypeError.  The bignum converter below would read a float's
            # payload as bigint limbs.
            return _raise_not_integer(o)
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


def _builtin_base_repr(o, base: int, prefix_ch: int):
    """bin()/hex()/oct() as CPython's PyNumber_ToBase: any other argument
    first goes through the __index__ protocol (its TypeErrors included)."""
    if ptr_is_null(o) == 0 and (is_tagged_int(o) != 0 or _type_of(o) == PY_TYPE_INT
                                or _type_of(o) == PY_TYPE_BOOL):
        return _int_based_repr(o, base, prefix_ch)
    index = py_obj_index(o)
    if ptr_is_null(index) != 0:
        return null()
    result = _int_based_repr(index, base, prefix_ch)
    py_decref(index)
    return result


@c_abi_export("py_builtin_bin")
def py_builtin_bin(o):
    return _builtin_base_repr(o, 2, 98)    # 'b'


@c_abi_export("py_builtin_hex")
def py_builtin_hex(o):
    return _builtin_base_repr(o, 16, 120)  # 'x'


@c_abi_export("py_builtin_oct")
def py_builtin_oct(o):
    return _builtin_base_repr(o, 8, 111)   # 'o'


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
    return py_user_special_dispatch(o, cstr("__str__"), null(), null(), 0, 6, null(), null())


@c_abi_export("py_user_repr_dispatch")
def py_user_repr_dispatch(o):
    return py_user_special_dispatch(o, cstr("__repr__"), null(), null(), 0, 6, null(), null())


@c_abi_export("py_obj_id")
def py_obj_id(o) -> int:
    """``id(o)``: the address, stable for the object's lifetime.

    The forwarding collectors (GC3/GC4) move objects, so an address is not an
    identity there; they report the stable object id instead (as
    ``object.__hash__`` does), scaled by 16 so it never equals a tagged int's
    odd value.
    """
    if ptr_is_null(o) or is_tagged_int(o):
        return ptr_to_int(o)
    backend: int = pcc_gc_backend()
    if backend == 3 or backend == 4:
        return pcc_gc_object_id(o) * 16
    return ptr_to_int(o)


def _identity_hash(o) -> int:
    # object.__hash__: identity.  The forwarding collectors move objects, so
    # they hash the stable object id instead of the address.
    backend: int = pcc_gc_backend()
    h: int = 0
    if backend == 3 or backend == 4:
        h = pcc_gc_object_id(o)
    else:
        h = ptr_to_int(o) >> 4
    if h == -1:
        return -2
    return h


@c_abi_export("py_obj_identity_hash")
def py_obj_identity_hash(o) -> int:
    """``object.__hash__(o)``: the identity hash a plain instance hashes to."""
    return _identity_hash(o)


@c_abi_export("py_user_hash_dispatch")
def py_user_hash_dispatch(o, handled) -> int:
    selected = stack_alloc(8)
    scalar = stack_alloc(8)
    py_user_special_dispatch(o, cstr("__hash__"), null(), null(), 0, 4, scalar, selected)
    if ptr_is_null(handled) == 0:
        store_i64(handled, 0, load_i64(selected, 0))
    if load_i64(selected, 0) != 0:
        return load_i64(scalar, 0)
    if ptr_is_null(o) != 0 or is_tagged_int(o) != 0:
        return 0
    tag: int = load_i32(o, 8)
    if tag != PY_TYPE_INSTANCE and tag < PY_TYPE_USER_CLASS_START:
        return 0
    if pcc_capi_is_cext_type_tag(tag) != 0:
        return 0
    cls = _load_instance_cls(o)
    if ptr_is_null(cls) != 0:
        return 0
    # Legacy identity fallback only observes presence; it never classifies or
    # invokes a borrowed mutable descriptor. Tuple inherits a real __hash__.
    if ptr_is_null(py_class_lookup(cls, cstr("__eq__"))) == 0:
        return 0
    if ptr_is_null(handled) == 0:
        store_i64(handled, 0, 1)
    return _identity_hash(o)


@c_abi_export("py_user_iter_dispatch")
def py_user_iter_dispatch(o):
    return py_user_special_dispatch(o, cstr("__iter__"), null(), null(), 0, 0, null(), null())


@c_abi_export("py_user_next_dispatch")
def py_user_next_dispatch(o):
    return py_user_special_dispatch(o, cstr("__next__"), null(), null(), 0, 0, null(), null())


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
        pcc_diagnostics_runtime_log_event_code(5, 4, tag, 1, o)
        return
    cls = _load_instance_cls(o)
    if ptr_is_null(cls):
        return
    func = py_class_lookup(cls, cstr("__del__"))
    if ptr_is_null(func):
        return
    store_i32(o, PYOBJECTHEADER_FLAGS_OFFSET, flags | 4)
    finalizer_owner = stack_alloc(C_POINTER_SIZE)
    store_ptr(finalizer_owner, 0, o)
    finalizer_lease: int = _finalizer_owner_enter(finalizer_owner)
    saved_exc = py_current_exception()
    saved_exc_pin: int = 0
    if ptr_is_null(saved_exc) == 0:
        py_incref(saved_exc)
        # TLS stops owning this address during the user finalizer. Its raw
        # local and the callback's enclosing deallocation frame cannot heal
        # it, so keep this one exception stationary until TLS owns it again.
        saved_exc_pin = load_i32(saved_exc, PYOBJECTHEADER_FLAGS_OFFSET) & 64
        pcc_gc_pin(saved_exc)
        py_tls_exc_set(null())
    pcc_diagnostics_runtime_log_event_code(5, 2, tag, 0, o)
    _call_user_unary_method_void(func, o)
    pcc_diagnostics_runtime_log_event_code(5, 3, tag, 0, o)
    py_clear_exception()
    if ptr_is_null(saved_exc) == 0:
        py_tls_exc_set(saved_exc)
        pcc_gc_unpin(saved_exc)
        if saved_exc_pin != 0:
            atomic_rmw_i32("or", saved_exc, PYOBJECTHEADER_FLAGS_OFFSET, 64, "relaxed")
        py_decref(saved_exc)
    _finalizer_owner_leave(finalizer_owner, finalizer_lease, flags & 524288)
