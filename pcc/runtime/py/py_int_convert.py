"""Phase 4c: pcc-Python port of py_int_convert.c.

Converts tagged or heap ints to int64 for runtime callers that need a
native scalar and an overflow flag. Tagged-int decoding still uses the
existing C helper until the unsafe pointer/integer intrinsic exists.

PyIntObject layout:
    offset  0   PyObjectHeader
    offset 16   sign       (i32)
    offset 20   ndigits    (i32)
    offset 24   digits[]   (u32 little-endian)
"""

__pcc_runtime_port__ = True

from pcc.extern import extern, c_abi_export, c_ptr, c_int64, c_void
from pcc.runtime.py.py_abi_constants import (
    PYBYTESOBJECT_BYTE_LEN_OFFSET,
    PYBYTESOBJECT_DATA_OFFSET,
    PYINTOBJECT_DIGITS_OFFSET,
    PYINTOBJECT_NDIGITS_OFFSET,
    PYINTOBJECT_SIGN_OFFSET,
    PYMEMORYVIEWOBJECT_BASE_OFFSET,
    PYOBJECTHEADER_FLAGS_OFFSET,
    PYOBJECTHEADER_TYPE_TAG_OFFSET,
    PY_FLAG_GC_PINNED,
    PY_TYPE_BOOL,
    PY_TYPE_BYTEARRAY,
    PY_TYPE_BYTES,
    PY_TYPE_INT,
    PY_TYPE_MEMORYVIEW,
    PY_TYPE_STR,
)
from pcc.unsafe import (
    cstr,
    global_load_ptr,
    is_tagged_int,
    load_i8,
    load_i32,
    load_i64,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    stack_alloc,
    store_i8,
    store_i32,
    store_i64,
    store_ptr,
    untag_int,
)


py_int_value_i64     = extern("py_int_value_i64",     (c_ptr,),                  c_int64)
# Unbox a C-extension number scalar (numpy int/bool scalar from ndarray element
# access) via its nb_int/nb_index slot. C-only helper (py_capi_shim.c); no cc
# baseline mirror because cext objects only exist under the no-libpython C-API
# shim that this port archive links.
py_cext_number_to_i64 = extern("py_cext_number_to_i64", (c_ptr, c_ptr),          c_int64)
py_str_utf8 = extern("py_str_utf8", (c_ptr,), c_ptr)
py_bytes_new = extern("py_bytes_new", (c_ptr, c_int64), c_ptr)
py_bigint_alloc = extern("py_bigint_alloc", (c_int64,), c_ptr)
py_bigint_to_pyobject = extern("py_bigint_to_pyobject", (c_ptr,), c_ptr)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_index_i64_checked = extern("py_index_i64_checked", (c_ptr,), c_int64)
py_obj_truthy = extern("py_obj_truthy", (c_ptr,), c_int64)
py_err_occurred = extern("py_err_occurred", (), c_int64)
pcc_gc_store_root = extern("pcc_gc_store_root", (c_ptr, c_ptr), c_void)
pcc_gc_scheduler_root_register_handle = extern("pcc_gc_scheduler_root_register_handle", (c_ptr,), c_ptr)
pcc_gc_scheduler_root_unregister_handle = extern("pcc_gc_scheduler_root_unregister_handle", (c_ptr,), c_void)
pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
pcc_gc_unpin = extern("pcc_gc_unpin", (c_ptr,), c_void)
pcc_gc_take_pinned_slot = extern("pcc_gc_take_pinned_slot", (c_ptr, c_int64), c_ptr)
pcc_gc_foreign_lease_acquire = extern("pcc_gc_foreign_lease_acquire", (c_ptr,), c_int64)
pcc_gc_foreign_lease_release = extern("pcc_gc_foreign_lease_release", (c_ptr, c_int64), c_int64)
pcc_gc_retain_plan_prepare_locked = extern("pcc_gc_retain_plan_prepare_locked", (c_ptr, c_ptr), c_ptr)
pcc_gc_retain_plan_finish = extern("pcc_gc_retain_plan_finish", (c_ptr,), c_void)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)



def _set_overflow(slot: c_ptr, value: int) -> None:
    if not ptr_is_null(slot):
        store_i32(slot, 0, value)


def _load_u32(obj, offset: int) -> int:
    v: int = load_i32(obj, offset)
    if v < 0:
        v = v + 4294967296
    return v


def _store_u32(obj, offset: int, value: int) -> None:
    store_i32(obj, offset, value)


def _byteorder_is_big(byteorder) -> int:
    raw = py_str_utf8(byteorder)
    if ptr_is_null(raw):
        return -1
    if (
        load_i8(raw, 0) == 98
        and load_i8(raw, 1) == 105
        and load_i8(raw, 2) == 103
        and load_i8(raw, 3) == 0
    ):
        return 1
    if (
        load_i8(raw, 0) == 108
        and load_i8(raw, 1) == 105
        and load_i8(raw, 2) == 116
        and load_i8(raw, 3) == 116
        and load_i8(raw, 4) == 108
        and load_i8(raw, 5) == 101
        and load_i8(raw, 6) == 0
    ):
        return 0
    return -1


def _raise_int_bytes(kind: int, message: c_ptr) -> None:
    py_raise_owned(py_exc_new(kind, message))


def _int_bytes_like_base(value):
    current = value
    while not ptr_is_null(current) and not is_tagged_int(current):
        tag: int = load_i32(current, PYOBJECTHEADER_TYPE_TAG_OFFSET)
        if tag == PY_TYPE_BYTES or tag == PY_TYPE_BYTEARRAY:
            return current
        if tag != PY_TYPE_MEMORYVIEW:
            return null()
        current = pcc_gc_load_ptr(
            current,
            ptr_add(current, PYMEMORYVIEWOBJECT_BASE_OFFSET),
        )
    return null()


@c_abi_export("py_int_to_i64")
def py_int_to_i64(o, overflow: c_ptr) -> int:
    # Tagged small ints are the overwhelming majority of unboxes, and this
    # function was the single hottest symbol in a `pcc1 -> pcc2` build (117
    # instructions).  pcc does not inline, so the fast path must contain no
    # calls: `is_tagged_int` / `untag_int` / `ptr_is_null` / `store_i32` are
    # compiler intrinsics, while `_set_overflow` and the `py_int_value_i64`
    # extern are real calls — the old fast path made both.  A tagged value is
    # `(v << 1) | 1`, always odd and therefore never null, so testing it
    # before the null check is equivalent.  (The module docstring's claim that
    # tagged decoding needs the C helper predates the `untag_int` intrinsic.)
    if is_tagged_int(o):
        if not ptr_is_null(overflow):
            store_i32(overflow, 0, 0)
        return untag_int(o)
    _set_overflow(overflow, 0)
    if ptr_is_null(o):
        _set_overflow(overflow, 1)
        return 0
    if load_i32(o, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_BOOL:
        # bool is an int subclass: True is 1 and False 0.  Treating it as a
        # foreign number returned 0 with overflow set, so an int-typed slot
        # that received a dynamic ``True`` read 0 -- pcc1 dropped the release
        # of every owned receiver whose flag came through such a slot.
        if ptr_eq(o, global_load_ptr("py_True")) != 0:
            return 1
        return 0
    if load_i32(o, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_INT:
        # Not a pcc heap int. A C-extension number scalar (numpy int/bool)
        # unboxes through its number protocol; py_cext_number_to_i64 sets
        # overflow=1 and returns 0 for any non-cext-number object, preserving
        # the previous behaviour for genuinely non-integer objects.
        return py_cext_number_to_i64(o, overflow)

    sign: int = load_i32(o, PYINTOBJECT_SIGN_OFFSET)
    if sign == 0:
        return 0
    ndigits: int = load_i32(o, PYINTOBJECT_NDIGITS_OFFSET)
    if ndigits <= 0:
        return 0
    if ndigits > 2:
        _set_overflow(overflow, 1)
        return 0

    low: int = _load_u32(o, PYINTOBJECT_DIGITS_OFFSET)
    high: int = 0
    if ndigits == 2:
        high = _load_u32(o, PYINTOBJECT_DIGITS_OFFSET + 4)

    if sign > 0:
        if high > 2147483647:
            _set_overflow(overflow, 1)
            return 0
        return high * 4294967296 + low

    if high > 2147483648:
        _set_overflow(overflow, 1)
        return 0
    if high == 2147483648:
        if low != 0:
            _set_overflow(overflow, 1)
            return 0
        min_i64: int = -9223372036854775807
        return min_i64 - 1
    return 0 - (high * 4294967296 + low)


@c_abi_export("py_int_to_i64_lane")
def py_int_to_i64_lane(o, overflow: c_ptr) -> int:
    """``py_int_to_i64`` for a value entering a compiled i64 lane.

    The compiler keeps an ``int`` in the raw i64 lane when it believes the
    value is bounded.  An int that does not fit means that belief was wrong,
    and the silent 0 ``py_int_to_i64`` returns turned it into a wrong program
    (pcc1 folded ``10 ** 30`` to 0).  Such an int raises OverflowError here;
    anything else converts exactly as ``py_int_to_i64`` does.
    """
    flag = overflow
    if ptr_is_null(flag):
        flag = stack_alloc(8)
    value: int = py_int_to_i64(o, flag)
    if load_i32(flag, 0) != 0 and not ptr_is_null(o) and not is_tagged_int(o):
        if load_i32(o, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_INT:
            py_raise_owned(
                py_exc_new(15, cstr("Python int too large to convert to C int64"))
            )
    return value


# Raw ABI callers hold counted address leases while these local roots are
# registered. Each stored argument owns one separate reference, while result
# roots take (rather than duplicate) the newly returned owner's reference.
@c_abi_export("_pcc_int_bytes_open_root")
def _int_bytes_open_root(slot: c_ptr, value) -> c_ptr:
    store_ptr(slot, 0, null())
    handle = pcc_gc_scheduler_root_register_handle(slot)
    if not ptr_is_null(handle):
        pcc_gc_store_root(slot, value)
    return handle


@c_abi_export("_pcc_int_bytes_close_root")
def _int_bytes_close_root(slot: c_ptr, handle: c_ptr) -> None:
    if not ptr_is_null(handle):
        pcc_gc_store_root(slot, null())
        pcc_gc_scheduler_root_unregister_handle(handle)


@c_abi_export("_pcc_int_bytes_save_result")
def _int_bytes_save_result(state: c_ptr, result) -> c_ptr:
    store_ptr(state, 0, null())
    store_i64(state, 8, 0)
    if ptr_is_null(result):
        return null()
    prior: int = 0
    if not is_tagged_int(result):
        prior = load_i32(result, PYOBJECTHEADER_FLAGS_OFFSET) & PY_FLAG_GC_PINNED
    pcc_gc_pin(result)
    store_ptr(state, 0, result)
    store_i64(state, 8, prior)
    handle = pcc_gc_scheduler_root_register_handle(state)
    if ptr_is_null(handle):
        result = pcc_gc_take_pinned_slot(state, prior)
        py_decref(result)
        if py_err_occurred() == 0:
            _raise_int_bytes(7, cstr("integer byte conversion result root registration failed"))
    return handle


@c_abi_export("_pcc_int_bytes_take_result")
def _int_bytes_take_result(state: c_ptr, handle: c_ptr):
    if ptr_is_null(handle):
        return null()
    pcc_py_gc_minor_graph_lock()
    result = pcc_gc_load_ptr(null(), state)
    # A finalizer may have balanced a nested pin/unpin on an alias.
    pcc_gc_unpin(result)
    pcc_gc_pin(result)
    pcc_py_gc_minor_graph_unlock()
    pcc_gc_scheduler_root_unregister_handle(handle)
    result = pcc_gc_take_pinned_slot(state, load_i64(state, 8))
    if py_err_occurred() != 0:
        py_decref(result)
        return null()
    return result


@c_abi_export("py_int_to_bytes")
def py_int_to_bytes(v, length: int, byteorder):
    # Preserve the unsigned ABI consumed by pre-existing native objects.
    return _int_to_bytes(v, length, byteorder, 0)


@c_abi_export("py_int_to_bytes_args")
def py_int_to_bytes_args(v, length_obj, byteorder, signed_value):
    # The caller's counted leases protect incoming raw copies while root
    # registration can park. Callback results never make SSA copies current:
    # every later operand read comes from one of these updateable roots.
    slots = stack_alloc(32)
    value_handle = _int_bytes_open_root(slots, v)
    length_handle = _int_bytes_open_root(ptr_add(slots, 8), length_obj)
    order_handle = _int_bytes_open_root(ptr_add(slots, 16), byteorder)
    signed_handle = _int_bytes_open_root(ptr_add(slots, 24), signed_value)
    result = null()
    if (ptr_is_null(value_handle) or ptr_is_null(length_handle)
            or ptr_is_null(order_handle) or ptr_is_null(signed_handle)):
        _raise_int_bytes(7, cstr("integer byte conversion argument root registration failed"))
    else:
        result = _int_to_bytes_rooted_args(slots)
    result_state = stack_alloc(16)
    result_handle = _int_bytes_save_result(result_state, result)
    _int_bytes_close_root(ptr_add(slots, 24), signed_handle)
    _int_bytes_close_root(ptr_add(slots, 16), order_handle)
    _int_bytes_close_root(ptr_add(slots, 8), length_handle)
    _int_bytes_close_root(slots, value_handle)
    return _int_bytes_take_result(result_state, result_handle)


@c_abi_export("_pcc_int_to_bytes_rooted_args")
def _int_to_bytes_rooted_args(slots: c_ptr):
    # Signature conversion order differs from keyword evaluation order.
    length: int = py_index_i64_checked(pcc_gc_load_ptr(null(), ptr_add(slots, 8)))
    if py_err_occurred() != 0:
        return null()
    byteorder = pcc_gc_load_ptr(null(), ptr_add(slots, 16))
    if ptr_is_null(byteorder) or is_tagged_int(byteorder):
        _raise_int_bytes(3, cstr("to_bytes() byteorder must be str"))
        return null()
    if load_i32(byteorder, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_STR:
        _raise_int_bytes(3, cstr("to_bytes() byteorder must be str"))
        return null()
    is_signed: int = py_obj_truthy(pcc_gc_load_ptr(null(), ptr_add(slots, 24)))
    if py_err_occurred() != 0:
        return null()
    # Counted caller leases survive Boolean-pin clearing by either callback
    # and cover the encoder's allocation as well as these reloaded pointers.
    return _int_to_bytes(pcc_gc_load_ptr(null(), slots), length,
                         pcc_gc_load_ptr(null(), ptr_add(slots, 16)), is_signed)


def _int_to_bytes(v, length: int, byteorder, is_signed: int):
    if ptr_is_null(byteorder) or is_tagged_int(byteorder):
        _raise_int_bytes(3, cstr("to_bytes() byteorder must be str"))
        return null()
    if load_i32(byteorder, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_STR:
        _raise_int_bytes(3, cstr("to_bytes() byteorder must be str"))
        return null()
    big: int = _byteorder_is_big(byteorder)
    if big < 0:
        _raise_int_bytes(2, cstr("byteorder must be either 'little' or 'big'"))
        return null()
    if length < 0:
        _raise_int_bytes(2, cstr("length argument must be non-negative"))
        return null()
    if length > 9223372036854775807 - PYBYTESOBJECT_DATA_OFFSET - 1:
        _raise_int_bytes(15, cstr("byte string is too large"))
        return null()

    tagged: bool = is_tagged_int(v)
    ndigits: int = 0
    small_low: int = 0
    small_high: int = 0
    negative: int = 0
    if tagged:
        raw: int = py_int_value_i64(v)
        if raw < 0:
            negative = 1
            # Tagged values have at most 63 signed bits, so their magnitude
            # fits this raw i64 lane. Heap bigints never enter that lane.
            raw = 0 - raw
        small_low = raw & 4294967295
        small_high = raw >> 32
        if small_high != 0:
            ndigits = 2
        elif small_low != 0:
            ndigits = 1
    else:
        if ptr_is_null(v):
            _raise_int_bytes(3, cstr("to_bytes expects an int"))
            return null()
        tag: int = load_i32(v, PYOBJECTHEADER_TYPE_TAG_OFFSET)
        if tag == PY_TYPE_BOOL:
            tagged = True
            if ptr_eq(v, global_load_ptr("py_True")) != 0:
                small_low = 1
                ndigits = 1
        elif tag == PY_TYPE_INT:
            if load_i32(v, PYINTOBJECT_SIGN_OFFSET) < 0:
                negative = 1
            ndigits = load_i32(v, PYINTOBJECT_NDIGITS_OFFSET)
        else:
            _raise_int_bytes(3, cstr("to_bytes expects an int"))
            return null()
    if negative != 0 and is_signed == 0:
        _raise_int_bytes(15, cstr("can't convert negative int to unsigned"))
        return null()

    needed: int = 0
    top: int = 0
    top_bytes: int = 0
    if ndigits > 0:
        if tagged:
            top = small_low
            if ndigits == 2:
                top = small_high
        else:
            top = _load_u32(v, PYINTOBJECT_DIGITS_OFFSET + (ndigits - 1) * 4)
        top_bytes = 4
        while top_bytes > 1 and (top >> ((top_bytes - 1) * 8)) == 0:
            top_bytes = top_bytes - 1
        needed = (ndigits - 1) * 4 + top_bytes
    overflow: int = 1 if needed > length else 0
    if is_signed != 0 and needed == length and needed > 0:
        sign_limit: int = 128 << ((top_bytes - 1) * 8)
        if top >= sign_limit:
            if negative == 0 or top > sign_limit:
                overflow = 1
            else:
                # Exactly -2**(8*length-1) fits; any lower magnitude limb
                # makes it more negative than the signed range permits.
                i: int = 0
                while i < ndigits - 1:
                    limb: int = small_low
                    if not tagged:
                        limb = _load_u32(v, PYINTOBJECT_DIGITS_OFFSET + i * 4)
                    if limb != 0:
                        overflow = 1
                    i = i + 1
    if overflow != 0:
        _raise_int_bytes(15, cstr("int too big to convert"))
        return null()

    out = py_bytes_new(null(), length)
    if ptr_is_null(out):
        return null()
    carry: int = negative
    i: int = 0
    while i < length:
        byte: int = 0
        if i < needed:
            limb_index: int = i // 4
            if tagged:
                limb = small_low
                if limb_index == 1:
                    limb = small_high
            else:
                limb = _load_u32(v, PYINTOBJECT_DIGITS_OFFSET + limb_index * 4)
            byte = (limb >> ((i % 4) * 8)) & 255
        if negative != 0:
            byte = (byte ^ 255) + carry
            carry = byte >> 8
            byte = byte & 255
        output_index: int = i
        if big != 0:
            output_index = length - 1 - i
        store_i8(out, PYBYTESOBJECT_DATA_OFFSET + output_index, byte)
        i = i + 1
    return out


@c_abi_export("py_int_from_bytes")
def py_int_from_bytes(bytes_obj, byteorder):
    # Preserve the unsigned two-argument runtime ABI used by existing objects.
    return py_int_from_bytes_signed(bytes_obj, byteorder, 0)


@c_abi_export("py_int_from_bytes_signed")
def py_int_from_bytes_signed(bytes_obj, byteorder, is_signed: int):
    # py_str_utf8 is a raw layout accessor, so validate before reading it.
    if ptr_is_null(byteorder) or is_tagged_int(byteorder):
        _raise_int_bytes(3, cstr("from_bytes() byteorder must be str"))
        return null()
    if load_i32(byteorder, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_STR:
        _raise_int_bytes(3, cstr("from_bytes() byteorder must be str"))
        return null()
    big: int = _byteorder_is_big(byteorder)
    if big < 0:
        _raise_int_bytes(
            2,
            cstr("byteorder must be either 'little' or 'big'"),
        )
        return null()
    base_slot = stack_alloc(8)
    store_ptr(base_slot, 0, null())
    base_handle = pcc_gc_scheduler_root_register_handle(base_slot)
    if ptr_is_null(base_handle):
        _raise_int_bytes(7, cstr("integer byte conversion buffer root registration failed"))
        return null()
    retain_plan = stack_alloc(56)
    pcc_py_gc_minor_graph_lock()
    # A memoryview's address lease does not protect its child buffer. Resolve
    # and retain that actual owner under the graph lock into a traced slot.
    base = _int_bytes_like_base(bytes_obj)
    base = pcc_gc_retain_plan_prepare_locked(retain_plan, base)
    store_ptr(base_slot, 0, base)
    pcc_py_gc_minor_graph_unlock()
    pcc_gc_retain_plan_finish(retain_plan)
    acquired: int = pcc_gc_foreign_lease_acquire(base_slot)
    result = null()
    if acquired < 0:
        if acquired == -2:
            _raise_int_bytes(15, cstr("integer byte conversion buffer lease overflow"))
        else:
            _raise_int_bytes(7, cstr("integer byte conversion requires a stable buffer owner"))
    else:
        result = _int_from_bytes_leased_base(pcc_gc_load_ptr(null(), base_slot), big, is_signed)
    result_state = stack_alloc(16)
    result_handle = _int_bytes_save_result(result_state, result)
    if acquired >= 0:
        status: int = pcc_gc_foreign_lease_release(base_slot, acquired)
        if status < 0 and py_err_occurred() == 0:
            _raise_int_bytes(7, cstr("integer byte conversion buffer lease cleanup failed"))
    _int_bytes_close_root(base_slot, base_handle)
    return _int_bytes_take_result(result_state, result_handle)


@c_abi_export("_pcc_int_from_bytes_leased_base")
def _int_from_bytes_leased_base(base, big: int, is_signed: int):
    if ptr_is_null(base):
        _raise_int_bytes(3, cstr("from_bytes expects a bytes object"))
        return null()

    n: int = load_i64(base, PYBYTESOBJECT_BYTE_LEN_OFFSET)
    ndigits: int = (n + 3) // 4
    if ndigits < 1:
        ndigits = 1
    out = py_bigint_alloc(ndigits)
    if ptr_is_null(out):
        return null()
    negative: int = 0
    if is_signed != 0 and n > 0:
        most_significant: int = n - 1
        if big != 0:
            most_significant = 0
        if (load_i8(base, PYBYTESOBJECT_DATA_OFFSET + most_significant) & 128) != 0:
            negative = 1
    # Walk least-significant byte first for either byte order. A negative
    # two's-complement input becomes a sign/magnitude bigint by complementing
    # every byte and adding one with carry; no fixed-width integer conversion.
    carry: int = negative
    i: int = 0
    while i < n:
        source_index: int = i
        if big != 0:
            source_index = n - 1 - i
        byte: int = load_i8(base, PYBYTESOBJECT_DATA_OFFSET + source_index) & 255
        if negative != 0:
            byte = (byte ^ 255) + carry
            carry = byte >> 8
            byte = byte & 255
        offset: int = PYINTOBJECT_DIGITS_OFFSET + (i // 4) * 4
        limb: int = _load_u32(out, offset)
        _store_u32(out, offset, limb | (byte << ((i % 4) * 8)))
        i = i + 1

    used: int = ndigits
    while used > 0 and _load_u32(out, PYINTOBJECT_DIGITS_OFFSET + (used - 1) * 4) == 0:
        used = used - 1
    store_i32(out, PYINTOBJECT_NDIGITS_OFFSET, used)
    if used > 0:
        sign: int = 1
        if negative != 0:
            sign = -1
        store_i32(out, PYINTOBJECT_SIGN_OFFSET, sign)
    else:
        store_i32(out, PYINTOBJECT_SIGN_OFFSET, 0)
    return py_bigint_to_pyobject(out)
