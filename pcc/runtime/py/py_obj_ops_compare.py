"""Phase 4c.15b: pcc-Python port of py_obj_ops_compare.c.

Equality / hashing / three-way compare / sorted / contains.

The earlier port had an insertion-sort bug: I used `k = 0` to "break"
the inner loop, then unconditionally wrote cur to out[k] — which
overwrote slot 0 with later elements instead of leaving them at slot
j. The fix uses a `done` flag so the post-loop k is the correct slot.

Public object type tags come from the generated ``py_abi_constants`` module.

Object layouts:
    PyStrObject:  byte_len@16 (i64), cp_len@24, hash@32 (i64, -1=unset), data@40
    PyBytesObject/PyByteArrayObject: byte_len@16 (i64), data@24
    PyMemoryViewObject: base@16 (ptr)
    PyListObject: length@16   (i64),  capacity@24, items@32 (ptr)
    PyTupleObject: len@16     (i64),  items[]@24  (flex)

FNV-1a constants (verified to work in pcc-Python signed-i64):
    offset basis: 0xcbf29ce484222325 = -3750763034362895579 (signed)
    prime:        0x100000001b3      =  1099511628211
"""

__pcc_runtime_port__ = True

from pcc.extern import extern, c_abi_export, c_ptr, c_int32, c_int64, c_void, c_double
from pcc.runtime.py.py_abi_constants import PY_TYPE_ELLIPSIS, PY_TYPE_NOTIMPLEMENTED
from pcc.runtime.py.py_abi_constants import PY_OBJ_CMP_UNORDERED, C_POINTER_SIZE, DICTENTRY_KEY_OFFSET, DICTENTRY_SIZE, DICTENTRY_VALUE_OFFSET, PYBYTEARRAYOBJECT_BYTE_LEN_OFFSET, PYBYTEARRAYOBJECT_DATA_OFFSET, PYBYTESOBJECT_BYTE_LEN_OFFSET, PYBYTESOBJECT_DATA_OFFSET, PYDICTOBJECT_ENTRIES_OFFSET, PYDICTOBJECT_ENTRIES_USED_OFFSET, PYDICTOBJECT_ITEM_COUNT_OFFSET, PYFLOATOBJECT_VALUE_OFFSET, PYINTOBJECT_DIGITS_OFFSET, PYINTOBJECT_NDIGITS_OFFSET, PYINTOBJECT_SIGN_OFFSET, PYCLASSOBJECT_FIELD_NAMES_OFFSET, PYCLASSOBJECT_NAME_OFFSET, PYCLASSOBJECT_N_FIELDS_OFFSET, PYINSTANCEOBJECT_CLS_OFFSET, PYINSTANCEOBJECT_FIELDS_OFFSET, PYLISTOBJECT_ITEMS_OFFSET, PYMEMORYVIEWOBJECT_BASE_OFFSET, PYOBJECTHEADER_TYPE_TAG_OFFSET, PYSTROBJECT_BYTE_LEN_OFFSET, PYSTROBJECT_DATA_OFFSET, PYSTROBJECT_HASH_OFFSET, PYTUPLEOBJECT_ITEMS_OFFSET, PYTUPLEOBJECT_LEN_OFFSET, PY_TYPE_BOOL, PY_TYPE_BYTEARRAY, PY_TYPE_BYTES, PY_TYPE_DICT, PY_TYPE_FLOAT, PY_TYPE_INT, PY_TYPE_LIST, PY_TYPE_MEMORYVIEW, PY_TYPE_NONE, PY_TYPE_SET, PY_TYPE_STR, PY_TYPE_TUPLE, PY_TYPE_VALUEBOX
from pcc.runtime.py.py_abi_constants import PY_TYPE_CLASS, PY_TYPE_COMPLEX, PY_TYPE_INSTANCE, PY_TYPE_USER_CLASS_START
from pcc.unsafe import (
    cstr,
    define_global_i32,
    free,
    malloc,
    memset,
    global_addr,
    global_load_ptr,
    is_tagged_int,
    load_i8,
    load_i32,
    load_i64,
    load_ptr,
    logical_shift_left_i64,
    logical_shift_right_i64,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    ptr_to_int,
    stack_alloc,
    store_i8,
    store_ptr,
    store_i64,
    strlen,
    untag_int,
)

py_int_value_i64     = extern("py_int_value_i64",     (c_ptr,),                    c_int64)
py_int_from_i64      = extern("py_int_from_i64",      (c_int64,),                  c_ptr)
py_int_cmp           = extern("py_int_cmp",           (c_ptr, c_ptr),              c_int32)
py_int_f64_cmp       = extern("py_int_f64_cmp",       (c_ptr, c_double),           c_int64)
py_int_neg           = extern("py_int_neg",           (c_ptr,),                    c_ptr)
py_float_from_f64    = extern("py_float_from_f64",    (c_double,),                 c_ptr)
py_float_to_f64      = extern("py_float_to_f64",      (c_ptr,),                    c_double)

py_set_issubset      = extern("py_set_issubset",      (c_ptr, c_ptr),              c_int64)
py_set_issuperset    = extern("py_set_issuperset",    (c_ptr, c_ptr),              c_int64)
py_set_len           = extern("py_set_len",           (c_ptr,),                    c_int64)
py_str_eq            = extern("py_str_eq",            (c_ptr, c_ptr),              c_int32)
py_str_contains      = extern("py_str_contains",      (c_ptr, c_ptr),              c_int32)
py_bytes_find        = extern("py_bytes_find",        (c_ptr, c_ptr),              c_int64)
py_str_len           = extern("py_str_len",           (c_ptr,),                    c_int64)

py_list_new          = extern("py_list_new",          (c_int64,),                  c_ptr)
py_list_append       = extern("py_list_append",       (c_ptr, c_ptr),              c_void)
py_list_get          = extern("py_list_get",          (c_ptr, c_int64),            c_ptr)
py_list_set          = extern("py_list_set",          (c_ptr, c_int64, c_ptr),     c_void)
py_list_len          = extern("py_list_len",          (c_ptr,),                    c_int64)
py_list_contains     = extern("py_list_contains",     (c_ptr, c_ptr),              c_int64)
py_dict_keys         = extern("py_dict_keys",         (c_ptr,),                    c_ptr)

py_tuple_get         = extern("py_tuple_get",         (c_ptr, c_int64),            c_ptr)
py_tuple_len         = extern("py_tuple_len",         (c_ptr,),                    c_int64)

py_dict_contains     = extern("py_dict_contains",     (c_ptr, c_ptr),              c_int64)
py_dict_get          = extern("py_dict_get",          (c_ptr, c_ptr),              c_ptr)
py_set_contains      = extern("py_set_contains",      (c_ptr, c_ptr),              c_int64)

py_obj_len           = extern("py_obj_len",           (c_ptr,),                    c_int64)
py_err_occurred      = extern("py_err_occurred",      (),                          c_int64)
py_clear_exception   = extern("py_clear_exception",   (),                          c_void)
py_obj_iter          = extern("py_obj_iter",          (c_ptr,),                    c_ptr)
py_obj_next          = extern("py_obj_next",          (c_ptr,),                    c_ptr)
py_current_exception = extern("py_current_exception", (),                          c_ptr)
py_exc_builtin_class = extern("py_exc_builtin_class", (c_int64,),                  c_ptr)
py_exc_matches       = extern("py_exc_matches",       (c_ptr, c_ptr),              c_int64)
py_obj_truthy = extern("py_obj_truthy", (c_ptr,), c_int64)
py_obj_getitem       = extern("py_obj_getitem",       (c_ptr, c_ptr),              c_ptr)

py_user_hash_dispatch = extern("py_user_hash_dispatch", (c_ptr, c_ptr),            c_int64)
py_user_contains_dispatch = extern("py_user_contains_dispatch", (c_ptr, c_ptr, c_ptr), c_int64)
py_user_eq_dispatch = extern("py_user_eq_dispatch", (c_ptr, c_ptr),               c_int64)
py_user_order_dispatch = extern("py_user_order_dispatch", (c_ptr, c_ptr, c_int64), c_int64)

pcc_gc_load_ptr      = extern("pcc_gc_load_ptr",      (c_ptr, c_ptr),              c_ptr)
pcc_gc_backend = extern("pcc_gc_backend", (), c_int64)
pcc_gc_scheduler_root_register_handle = extern(
    "pcc_gc_scheduler_root_register_handle", (c_ptr,), c_ptr
)
pcc_gc_scheduler_root_unregister_handle = extern(
    "pcc_gc_scheduler_root_unregister_handle", (c_ptr,), c_void
)
pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
pcc_gc_unpin = extern("pcc_gc_unpin", (c_ptr,), c_void)
py_exc_new           = extern("py_exc_new",           (c_int64, c_ptr),            c_ptr)
py_raise             = extern("py_raise",             (c_ptr,),                    c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_user_abs_dispatch = extern("py_user_abs_dispatch", (c_ptr,),                    c_ptr)
py_complex_abs = extern("py_complex_abs", (c_ptr,), c_ptr)
pcc_capi_is_cext_type_tag = extern("pcc_capi_is_cext_type_tag", (c_int64,),       c_int64)
pcc_capi_cext_absolute = extern("pcc_capi_cext_absolute", (c_ptr,),               c_ptr)
pcc_capi_cext_richcompare_bool = extern(
    "pcc_capi_cext_richcompare_bool", (c_ptr, c_ptr, c_int64), c_int64
)
py_err_occurred      = extern("py_err_occurred",      (),                          c_int64)
py_incref            = extern("py_incref",            (c_ptr,),                    c_void)
py_decref            = extern("py_decref",            (c_ptr,),                    c_void)
py_obj_type_name     = extern("py_obj_type_name",     (c_ptr,),                    c_ptr)
py_str_utf8          = extern("py_str_utf8",          (c_ptr,),                    c_ptr)
py_str_byte_len      = extern("py_str_byte_len",      (c_ptr,),                    c_int64)
py_str_payload       = extern("py_str_payload",       (c_ptr,),                    c_ptr)
py_tuple_payload     = extern("py_tuple_payload",     (c_ptr,),                    c_ptr)


def _type_of(o) -> int:
    if is_tagged_int(o) != 0:
        return PY_TYPE_INT
    return load_i32(o, PYOBJECTHEADER_TYPE_TAG_OFFSET)


def _is_bool(o) -> int:
    if ptr_eq(o, global_load_ptr("py_True")) != 0:
        return 1
    if ptr_eq(o, global_load_ptr("py_False")) != 0:
        return 1
    return 0


def _cstr_eq(a, b) -> int:
    # Byte-by-byte NUL-terminated compare, inlined to avoid pulling
    # the substrate strcmp helper into an active runtime module
    # (the substrate-helper boundary is enforced by
    # test_runtime_substrate_spike.py).
    if ptr_eq(a, b) != 0:
        return 1
    if ptr_is_null(a) != 0:
        return 0
    if ptr_is_null(b) != 0:
        return 0
    i: int = 0
    while True:
        ca: int = load_i8(a, i) & 255
        cb: int = load_i8(b, i) & 255
        if ca != cb:
            return 0
        if ca == 0:
            return 1
        i = i + 1


def _valuebox_classes_eq(a, b) -> int:
    if ptr_eq(a, b) != 0:
        if ptr_is_null(a) == 0:
            return 1
        return 0
    if ptr_is_null(a) != 0:
        return 0
    if ptr_is_null(b) != 0:
        return 0
    if _cstr_eq(
        load_ptr(a, PYCLASSOBJECT_NAME_OFFSET),
        load_ptr(b, PYCLASSOBJECT_NAME_OFFSET),
    ) == 0:
        return 0
    n_fields: int = load_i32(a, PYCLASSOBJECT_N_FIELDS_OFFSET)
    if n_fields != load_i32(b, PYCLASSOBJECT_N_FIELDS_OFFSET):
        return 0
    if n_fields < 0:
        return 0
    field_names_a = load_ptr(a, PYCLASSOBJECT_FIELD_NAMES_OFFSET)
    field_names_b = load_ptr(b, PYCLASSOBJECT_FIELD_NAMES_OFFSET)
    i: int = 0
    while i < n_fields:
        fa = null()
        fb = null()
        if ptr_is_null(field_names_a) == 0:
            fa = load_ptr(field_names_a, i * C_POINTER_SIZE)
        if ptr_is_null(field_names_b) == 0:
            fb = load_ptr(field_names_b, i * C_POINTER_SIZE)
        if _cstr_eq(fa, fb) == 0:
            return 0
        i = i + 1
    return 1


def _is_int_like_tag(tag: int) -> int:
    if tag == PY_TYPE_INT:
        return 1
    if tag == PY_TYPE_BOOL:
        return 1
    return 0


def _bool_as_i64(o) -> int:
    if ptr_eq(o, global_load_ptr("py_True")) != 0:
        return 1
    return 0


def _int_or_bool_as_i64(o) -> int:
    if _is_bool(o) != 0:
        return _bool_as_i64(o)
    return py_int_value_i64(o)


def _is_bytes_like_tag(tag: int) -> int:
    if tag == PY_TYPE_BYTES:
        return 1
    if tag == PY_TYPE_BYTEARRAY:
        return 1
    if tag == PY_TYPE_MEMORYVIEW:
        return 1
    return 0


def _bytes_len(o) -> int:
    if _type_of(o) == PY_TYPE_MEMORYVIEW:
        base = pcc_gc_load_ptr(o, ptr_add(o, PYMEMORYVIEWOBJECT_BASE_OFFSET))
        return _bytes_len(base)
    return load_i64(o, PYBYTESOBJECT_BYTE_LEN_OFFSET)


def _bytes_data_ptr(o):
    if _type_of(o) == PY_TYPE_MEMORYVIEW:
        base = pcc_gc_load_ptr(o, ptr_add(o, PYMEMORYVIEWOBJECT_BASE_OFFSET))
        return _bytes_data_ptr(base)
    return ptr_add(o, PYBYTESOBJECT_DATA_OFFSET)


def _dict_key(d, entries, off: int):
    k = load_ptr(entries, off + DICTENTRY_KEY_OFFSET)
    if ptr_is_null(k) != 0:
        return k
    return pcc_gc_load_ptr(d, ptr_add(entries, off + DICTENTRY_KEY_OFFSET))


def _dict_value(d, entries, off: int):
    v = load_ptr(entries, off + DICTENTRY_VALUE_OFFSET)
    if ptr_is_null(v) != 0:
        return v
    return pcc_gc_load_ptr(d, ptr_add(entries, off + DICTENTRY_VALUE_OFFSET))


def _set_key(s, entries, off: int):
    k = load_ptr(entries, off + DICTENTRY_KEY_OFFSET)
    if ptr_is_null(k) != 0:
        return k
    if ptr_eq(k, global_load_ptr("py_set_dummy")) != 0:
        return k
    return pcc_gc_load_ptr(s, ptr_add(entries, off + DICTENTRY_KEY_OFFSET))


def _bytes_cmp(a, b) -> int:
    la: int = _bytes_len(a)
    lb: int = _bytes_len(b)
    n: int = la
    if lb < n:
        n = lb
    da = _bytes_data_ptr(a)
    db = _bytes_data_ptr(b)
    i: int = 0
    while i < n:
        ba: int = load_i8(da, i) & 0xFF
        bb: int = load_i8(db, i) & 0xFF
        if ba < bb:
            return -1
        if ba > bb:
            return 1
        i = i + 1
    if la < lb:
        return -1
    if la > lb:
        return 1
    return 0


# ---- FNV-1a ---------------------------------------------------------

# CPython's numeric hash: every value is reduced modulo the Mersenne prime
# P = 2**61 - 1 so that ``x == y`` implies ``hash(x) == hash(y)`` across int,
# bool and float (``hash(1) == hash(1.0) == hash(True) == 1``).  Mirrors
# ``long_hash`` / ``_Py_HashDouble`` in Objects/longobject.c and
# Python/pyhash.c, and ``pcc_hash_i64`` / ``pcc_hash_double_bits`` in
# py_obj_ops_compare.c.  The port used to hash a float by its raw IEEE bits,
# so ``d[1.0]`` missed the entry stored under ``d[1]`` and ``{1: a, 1.0: b}``
# held two keys.


def _hash_i64(v: int) -> int:
    if v == -9223372036854775808:
        # |v| overflows i64; 2**63 mod P == 4.
        return -4
    neg: int = 0
    mag: int = v
    if v < 0:
        neg = 1
        mag = -v
    x: int = mag % 2305843009213693951
    if neg != 0:
        x = -x
    if x == -1:
        x = -2
    return x


def _hash_bigint(o) -> int:
    """hash() of a heap int: its value modulo 2**61 - 1, as CPython.

    Base-2**32 digits fold from the most significant one; multiplying by
    2**32 modulo 2**61 - 1 is a 61-bit rotation because 2**61 == 1 (mod P).
    """
    x: int = 0
    index: int = load_i32(o, PYINTOBJECT_NDIGITS_OFFSET) - 1
    while index >= 0:
        digit: int = load_i32(o, PYINTOBJECT_DIGITS_OFFSET + index * 4) & 4294967295
        x = (logical_shift_left_i64(x, 32) & 2305843009213693951) | logical_shift_right_i64(x, 29)
        x = x + digit
        if x >= 2305843009213693951:
            x = x - 2305843009213693951
        index = index - 1
    if load_i32(o, PYINTOBJECT_SIGN_OFFSET) < 0:
        x = -x
    if x == -1:
        x = -2
    return x


def _hash_f64_bits(bits: int, o) -> int:
    # value = (-1)**sign * mant * 2**e with mant < 2**53, so
    # hash = mant * 2**e mod P, and because 2**61 == 1 (mod P) the power of
    # two is a 61-bit rotation by ``e mod 61``.
    #
    # Both rotations shift with the wrapping ``logical_shift_left_i64``, as
    # CPython does in ``Py_uhash_t``.  A plain ``<<`` on an unbounded lane
    # compiles to an exact big-int shift: the NaN pointer rotation then
    # overflowed the i64 lane (``{nan}`` raised OverflowError for any object
    # address with a low nibble set), and every float hash boxed a big int.
    sign: int = 1
    if bits < 0:
        sign = -1
    exp: int = logical_shift_right_i64(bits, 52) & 2047
    frac: int = bits & 4503599627370495
    if exp == 2047:
        if frac == 0:
            return 314159 * sign
        # nan hashes by identity (CPython >= 3.10: _Py_HashPointer).
        p: int = ptr_to_int(o)
        h: int = logical_shift_right_i64(p, 4) | logical_shift_left_i64(p, 60)
        if h == -1:
            h = -2
        return h
    mant: int = frac
    e: int = 0
    if exp == 0:
        if frac == 0:
            return 0
        e = -1074
    else:
        mant = frac | 4503599627370496
        e = exp - 1075
    k: int = ((e % 61) + 61) % 61
    x: int = mant
    if k != 0:
        x = (logical_shift_left_i64(x, k) & 2305843009213693951) | logical_shift_right_i64(
            x, 61 - k
        )
    x = x * sign
    if x == -1:
        x = -2
    return x


@c_abi_export("pcc_hash_f64_bits")
def pcc_hash_f64_bits(bits: int, o) -> int:
    # The C-API ``_Py_HashDouble(o, v)`` for ``bits == f64_bits(v)``.  Sharing
    # this one implementation keeps ``_Py_HashDouble(NULL, 2.0) == hash(2)``.
    return _hash_f64_bits(bits, o)


def _fnv1a(p, n: int) -> int:
    # 0xcbf29ce484222325 as signed i64.
    h: int = -3750763034362895579
    i: int = 0
    while i < n:
        b: int = load_i8(p, i) & 0xFF
        h = h ^ b
        # Multiply with i64 wrap. Signed mul wraps the same as unsigned.
        h = h * 1099511628211
        i = i + 1
    if h == -1:
        return -2
    return h


# ---- Three-way compare (recursive over containers) ------------------

def _cmp_threeway(a, b) -> int:
    if ptr_eq(a, b) != 0:
        if ptr_is_null(a) == 0 and _type_of(a) == PY_TYPE_FLOAT:
            same_float: float = py_float_to_f64(a)
            if same_float != same_float:
                return PY_OBJ_CMP_UNORDERED
        return 0
    if ptr_is_null(a) != 0:
        if ptr_is_null(b) != 0:
            return 0
        return -1
    if ptr_is_null(b) != 0:
        return 1

    ta: int = _type_of(a)
    tb: int = _type_of(b)
    a_is_int: int = _is_int_like_tag(ta)
    b_is_int: int = _is_int_like_tag(tb)

    if a_is_int != 0:
        if b_is_int != 0:
            if ta == PY_TYPE_INT:
                if tb == PY_TYPE_INT:
                    return py_int_cmp(a, b)
            av: int = _int_or_bool_as_i64(a)
            bv: int = _int_or_bool_as_i64(b)
            if av < bv:
                return -1
            if av > bv:
                return 1
            return 0

    # Numeric with at least one float (pure int/int handled above). Without
    # this, float vs int / float vs float fell through to the final
    # ``return 0`` (treated as equal), so boxed-float comparisons via
    # py_obj_lt/gt were wrong. An int orders against a float by exact value,
    # as in CPython: converting it to a double first put 2**53 + 1 level with
    # 2**53.0.
    if ta == PY_TYPE_FLOAT:
        fa: float = py_float_to_f64(a)
        if tb == PY_TYPE_FLOAT:
            fb: float = py_float_to_f64(b)
            if fa != fa or fb != fb:
                return PY_OBJ_CMP_UNORDERED
            if fa < fb:
                return -1
            if fa > fb:
                return 1
            return 0
        if b_is_int != 0:
            # py_int_f64_cmp orders the int first; flip it back.
            flipped: int = py_int_f64_cmp(b, fa)
            if flipped == PY_OBJ_CMP_UNORDERED:
                return flipped
            return 0 - flipped
    elif tb == PY_TYPE_FLOAT:
        if a_is_int != 0:
            return py_int_f64_cmp(a, py_float_to_f64(b))

    if ta == PY_TYPE_STR:                       # STR
        if tb == PY_TYPE_STR:
            # memcmp byte semantics over min(len_a, len_b), scanned a
            # 64-bit word at a time (both data blocks start at offset
            # 40, so 8-step i64 loads stay aligned). i64 words cannot
            # be ordered directly on a little-endian host, so the first
            # differing word falls back to byte order inside it. The
            # old byte-by-byte loop dominated codegen-worker profiles
            # (sorted symbol names share long prefixes).
            la: int = load_i64(a, PYSTROBJECT_BYTE_LEN_OFFSET)
            lb: int = load_i64(b, PYSTROBJECT_BYTE_LEN_OFFSET)
            n: int = la
            if lb < n:
                n = lb
            da = ptr_add(a, PYSTROBJECT_DATA_OFFSET)
            db = ptr_add(b, PYSTROBJECT_DATA_OFFSET)
            i: int = 0
            w_end: int = n - 7
            while i < w_end:
                wa: int = load_i64(da, i)
                wb: int = load_i64(db, i)
                if wa != wb:
                    j: int = i
                    stop: int = i + 8
                    while j < stop:
                        wba: int = load_i8(da, j) & 0xFF
                        wbb: int = load_i8(db, j) & 0xFF
                        if wba < wbb:
                            return -1
                        if wba > wbb:
                            return 1
                        j = j + 1
                i = i + 8
            while i < n:
                ba: int = load_i8(da, i) & 0xFF
                bb: int = load_i8(db, i) & 0xFF
                if ba < bb:
                    return -1
                if ba > bb:
                    return 1
                i = i + 1
            if la < lb:
                return -1
            if la > lb:
                return 1
            return 0

    if _is_bytes_like_tag(ta) != 0:
        if _is_bytes_like_tag(tb) != 0:
            return _bytes_cmp(a, b)

    if ta == PY_TYPE_TUPLE:                       # TUPLE
        if tb == PY_TYPE_TUPLE:
            la: int = py_tuple_len(a)
            lb: int = py_tuple_len(b)
            n: int = la
            if lb < n:
                n = lb
            i: int = 0
            while i < n:
                ea = py_tuple_get(a, i)
                eb = py_tuple_get(b, i)
                r: int = 0
                if ptr_eq(ea, eb) == 0:
                    r = _cmp_threeway(ea, eb)
                if r != 0:
                    return r
                i = i + 1
            if la < lb:
                return -1
            if la > lb:
                return 1
            return 0

    if ta == PY_TYPE_LIST:                       # LIST
        if tb == PY_TYPE_LIST:
            la: int = py_list_len(a)
            lb: int = py_list_len(b)
            n: int = la
            if lb < n:
                n = lb
            i: int = 0
            while i < n:
                ea = py_list_get(a, i)
                eb = py_list_get(b, i)
                r: int = 0
                if ptr_eq(ea, eb) == 0:
                    r = _cmp_threeway(ea, eb)
                py_decref(ea)
                py_decref(eb)
                if r != 0:
                    return r
                i = i + 1
            if la < lb:
                return -1
            if la > lb:
                return 1
            return 0

    if ta == PY_TYPE_NONE:
        if tb == PY_TYPE_NONE:
            return 0

    return 0


def _abs_operand_error(o):
    # CPython: "bad operand type for abs(): 'str'".
    message = stack_alloc(160)
    prefix = cstr("bad operand type for abs(): '")
    length: int = _order_message_append(message, 0, prefix, strlen(prefix))
    length = _order_message_type(message, length, o)
    store_i8(message, length, 39)
    store_i8(message, length + 1, 0)
    py_raise_owned(py_exc_new(3, message))
    return null()


@c_abi_export("py_obj_abs")
def py_obj_abs(o):
    if ptr_is_null(o) != 0:
        py_raise_owned(py_exc_new(3, cstr("bad operand type for abs()")))
        return null()
    if is_tagged_int(o) != 0:
        ivalue: int = py_int_value_i64(o)
        if ivalue < 0:
            return py_int_neg(o)
        return py_int_from_i64(ivalue)
    tag: int = _type_of(o)
    if tag == PY_TYPE_BOOL:                      # BOOL
        return py_int_from_i64(_bool_as_i64(o))
    if tag == PY_TYPE_INT:                      # INT
        if load_i32(o, PYINTOBJECT_SIGN_OFFSET) < 0:
            return py_int_neg(o)
        py_incref(o)
        return o
    if tag == PY_TYPE_FLOAT:                      # FLOAT
        fvalue: float = py_float_to_f64(o)
        # Sign-bit clear: -0.0 and a negative NaN compare false against zero.
        return py_float_from_f64(abs(fvalue))
    if tag == PY_TYPE_COMPLEX:
        return py_complex_abs(o)
    if pcc_capi_is_cext_type_tag(tag) != 0:
        return pcc_capi_cext_absolute(o)
    if tag == PY_TYPE_INSTANCE or tag >= PY_TYPE_USER_CLASS_START:
        r = py_user_abs_dispatch(o)
        if ptr_is_null(r) == 0:
            return r
        if py_err_occurred() != 0:
            return null()
    return _abs_operand_error(o)


# ---- Equality -------------------------------------------------------

@c_abi_export("py_obj_eq")
def py_obj_eq(a, b) -> int:
    # Container identity-or-equality is distinct from a direct value operator.
    if ptr_eq(a, b) != 0:
        return 1
    return py_obj_eq_value(a, b)


@c_abi_export("py_obj_eq_value")
def py_obj_eq_value(a, b) -> int:
    if ptr_is_null(a) != 0:
        if ptr_is_null(b) != 0:
            return 1
        return 0
    if ptr_is_null(b) != 0:
        return 0

    ta: int = _type_of(a)
    tb: int = _type_of(b)
    if ptr_eq(a, b) != 0:
        if ta != PY_TYPE_FLOAT and ta != PY_TYPE_INSTANCE and ta < PY_TYPE_USER_CLASS_START:
            return 1

    # numpy / C-extension scalar ==: drive its tp_richcompare (Py_EQ=2), same as
    # py_obj_lt/le/gt/ge already do. Without this a[i] == 3 fell through to the
    # default not-equal and returned False even when the values matched.
    if pcc_capi_is_cext_type_tag(ta) != 0:
        if pcc_capi_cext_richcompare_bool(a, b, 2) > 0:
            return 1
        return 0
    if pcc_capi_is_cext_type_tag(tb) != 0:
        if pcc_capi_cext_richcompare_bool(a, b, 2) > 0:
            return 1
        return 0

    if ta == PY_TYPE_BOOL:                       # BOOL ↔ BOOL: distinct singletons
        if tb == PY_TYPE_BOOL:
            return 0

    if ta == PY_TYPE_STR:                       # STR
        if tb == PY_TYPE_STR:
            if py_str_eq(a, b) != 0:
                return 1
            return 0

    a_int: int = 0
    if ta == PY_TYPE_BOOL or ta == PY_TYPE_INT:
        a_int = 1
    b_int: int = 0
    if tb == PY_TYPE_BOOL or tb == PY_TYPE_INT:
        b_int = 1

    if a_int != 0:
        if b_int != 0:
            if ta == PY_TYPE_INT:
                if tb == PY_TYPE_INT:
                    if py_int_cmp(a, b) == 0:
                        return 1
                    return 0
            if _int_or_bool_as_i64(a) == _int_or_bool_as_i64(b):
                return 1
            return 0

    # Numeric with at least one float (pure int/int handled above).
    # py_obj_eq had no FLOAT (tag 3) branch, so float==float and float==int
    # fell through to the default ``return 0`` (not-equal) — e.g.
    # ``(c.v / c.w) == 2.5`` was False. Mirrors the float arm of
    # _cmp_threeway: an int equals a float only at exactly the same value.
    if ta == PY_TYPE_FLOAT:
        if tb == PY_TYPE_FLOAT:
            if py_float_to_f64(a) == py_float_to_f64(b):
                return 1
            return 0
        if b_int != 0:
            if py_int_f64_cmp(b, py_float_to_f64(a)) == 0:
                return 1
            return 0
    elif tb == PY_TYPE_FLOAT:
        if a_int != 0:
            if py_int_f64_cmp(a, py_float_to_f64(b)) == 0:
                return 1
            return 0

    if _is_bytes_like_tag(ta) != 0:
        if _is_bytes_like_tag(tb) != 0:
            if _bytes_cmp(a, b) == 0:
                return 1
            return 0

    if ta == PY_TYPE_TUPLE:                       # TUPLE
        if tb == PY_TYPE_TUPLE:
            la: int = load_i64(a, PYTUPLEOBJECT_LEN_OFFSET)
            lb: int = load_i64(b, PYTUPLEOBJECT_LEN_OFFSET)
            if la != lb:
                return 0
            i: int = 0
            while i < la:
                ea = pcc_gc_load_ptr(a, ptr_add(a, PYTUPLEOBJECT_ITEMS_OFFSET + i * 8))
                eb = pcc_gc_load_ptr(b, ptr_add(b, PYTUPLEOBJECT_ITEMS_OFFSET + i * 8))
                if ptr_eq(ea, eb) == 0:
                    if is_tagged_int(ea) != 0 and is_tagged_int(eb) != 0:
                        return 0
                    if is_tagged_int(ea) == 0 and is_tagged_int(eb) == 0:
                        if ptr_is_null(ea) != 0:
                            return 0
                        if ptr_is_null(eb) != 0:
                            return 0
                        if load_i32(ea, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_STR and load_i32(eb, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_STR:
                            if py_str_eq(ea, eb) == 0:
                                return 0
                        else:
                            if py_obj_eq(ea, eb) == 0:
                                return 0
                    else:
                        if py_obj_eq(ea, eb) == 0:
                            return 0
                i = i + 1
            return 1

    if ta == PY_TYPE_LIST:                       # LIST
        if tb == PY_TYPE_LIST:
            la: int = py_list_len(a)
            lb: int = py_list_len(b)
            if la != lb:
                return 0
            i: int = 0
            while i < la:
                ea = py_list_get(a, i)
                eb = py_list_get(b, i)
                eq: int = py_obj_eq(ea, eb)
                py_decref(ea)
                py_decref(eb)
                if eq == 0:
                    return 0
                i = i + 1
            return 1

    if ta == PY_TYPE_DICT:                       # DICT
        if tb == PY_TYPE_DICT:
            da_size: int = load_i64(a, PYDICTOBJECT_ITEM_COUNT_OFFSET)
            db_size: int = load_i64(b, PYDICTOBJECT_ITEM_COUNT_OFFSET)
            if da_size != db_size:
                return 0
            entries = load_ptr(a, PYDICTOBJECT_ENTRIES_OFFSET)
            used: int = load_i64(a, PYDICTOBJECT_ENTRIES_USED_OFFSET)
            i: int = 0
            while i < used:
                off: int = i * DICTENTRY_SIZE
                key = _dict_key(a, entries, off)
                if ptr_is_null(key) == 0:
                    val = _dict_value(a, entries, off)
                    other = py_dict_get(b, key)
                    if ptr_is_null(other) != 0:
                        return 0
                    eq: int = py_obj_eq(val, other)
                    py_decref(other)
                    if eq == 0:
                        return 0
                i = i + 1
            return 1

    if ta == PY_TYPE_SET:                       # SET
        if tb == PY_TYPE_SET:
            if load_i64(a, 16) != load_i64(b, 16):
                return 0
            entries = load_ptr(a, 40)
            capacity: int = load_i64(a, 24)
            dummy = global_load_ptr("py_set_dummy")
            i: int = 0
            while i < capacity:
                key = _set_key(a, entries, i * 16)
                if ptr_is_null(key) == 0:
                    if ptr_eq(key, dummy) == 0:
                        if py_set_contains(b, key) == 0:
                            return 0
                i = i + 1
            return 1

    if ta == PY_TYPE_VALUEBOX:                     # VALUEBOX
        if tb == PY_TYPE_VALUEBOX:
            cls_a = pcc_gc_load_ptr(a, ptr_add(a, PYINSTANCEOBJECT_CLS_OFFSET))
            cls_b = pcc_gc_load_ptr(b, ptr_add(b, PYINSTANCEOBJECT_CLS_OFFSET))
            if _valuebox_classes_eq(cls_a, cls_b) == 0:
                return 0
            n_fields: int = load_i32(cls_a, PYCLASSOBJECT_N_FIELDS_OFFSET)
            if n_fields < 0:
                return 0
            fields_a = ptr_add(a, PYINSTANCEOBJECT_FIELDS_OFFSET)
            fields_b = ptr_add(b, PYINSTANCEOBJECT_FIELDS_OFFSET)
            i: int = 0
            while i < n_fields:
                va = pcc_gc_load_ptr(
                    a, ptr_add(fields_a, i * C_POINTER_SIZE)
                )
                vb = pcc_gc_load_ptr(
                    b, ptr_add(fields_b, i * C_POINTER_SIZE)
                )
                if ptr_eq(va, vb) == 0:
                    if ptr_is_null(va) != 0:
                        return 0
                    if ptr_is_null(vb) != 0:
                        return 0
                    if py_obj_eq(va, vb) == 0:
                        return 0
                i = i + 1
            return 1

    if ta == PY_TYPE_NONE:
        return 0
    if tb == PY_TYPE_NONE:
        return 0

    # User instances: honor __eq__ for container key lookup and ``==``.
    # Tri-state: -1 = no user __eq__ (or NotImplemented) -> keep the
    if (ta == PY_TYPE_INSTANCE or ta >= PY_TYPE_USER_CLASS_START
            or tb == PY_TYPE_INSTANCE or tb >= PY_TYPE_USER_CLASS_START):
        dispatched: int = py_user_eq_dispatch(a, b)
        if dispatched >= 0:
            return dispatched
        if dispatched == -1:
            # A str subclass inherits str.__eq__, which has no method entry
            # (tuple subclasses answer through their view): compare payloads.
            payload_a = py_str_payload(a)
            payload_b = py_str_payload(b)
            if ptr_is_null(payload_a) == 0 and ptr_is_null(payload_b) == 0:
                return 1 if py_str_eq(payload_a, payload_b) != 0 else 0

    return ptr_eq(a, b)


# ---- Hash -----------------------------------------------------------

@c_abi_export("py_obj_hash")
def py_obj_hash(o) -> int:
    if ptr_is_null(o) != 0:
        return 0
    if is_tagged_int(o) != 0:
        return _hash_i64(untag_int(o))
    tag: int = load_i32(o, PYOBJECTHEADER_TYPE_TAG_OFFSET)
    if tag == PY_TYPE_ELLIPSIS or tag == PY_TYPE_NOTIMPLEMENTED:
        # The singleton is immortal and never relocates: identity hashing is
        # stable for its lifetime and needs no cached mutable object field.
        address: int = ptr_to_int(o)
        hashed: int = (logical_shift_right_i64(address, 4)
                       | logical_shift_left_i64(address, 60))
        if hashed == -1:
            return -2
        return hashed
    if tag == PY_TYPE_NONE:                      # NONE
        # CPython 3.12+: a constant, not the address.
        return 4238894112
    if tag == PY_TYPE_BOOL:                      # BOOL
        if ptr_eq(o, global_load_ptr("py_True")) != 0:
            return 1
        return 0
    if tag == PY_TYPE_VALUEBOX:                    # VALUEBOX
        cls = pcc_gc_load_ptr(o, ptr_add(o, PYINSTANCEOBJECT_CLS_OFFSET))
        if ptr_is_null(cls) != 0:
            return 0
        n_fields: int = load_i32(cls, PYCLASSOBJECT_N_FIELDS_OFFSET)
        if n_fields < 0:
            return 0
        h: int = n_fields
        fields = ptr_add(o, PYINSTANCEOBJECT_FIELDS_OFFSET)
        i: int = 0
        while i < n_fields:
            v = pcc_gc_load_ptr(o, ptr_add(fields, i * C_POINTER_SIZE))
            field_hash: int = 0
            if ptr_is_null(v) == 0:
                field_hash = py_obj_hash(v)
                if py_err_occurred() != 0:
                    return -1
            h = (h * 31 + (field_hash % 1000003)) % 1000000007
            i = i + 1
        if h == -1:
            return -2
        return h
    if tag == PY_TYPE_INT:                      # INT
        return _hash_bigint(o)
    if tag == PY_TYPE_FLOAT:                      # FLOAT
        return _hash_f64_bits(load_i64(o, PYFLOATOBJECT_VALUE_OFFSET), o)
    if tag == PY_TYPE_STR:                      # STR — FNV-1a with cache @32
        cached: int = load_i64(o, PYSTROBJECT_HASH_OFFSET)
        if cached != -1:
            return cached
        bl: int = load_i64(o, PYSTROBJECT_BYTE_LEN_OFFSET)
        data_ptr = ptr_add(o, PYSTROBJECT_DATA_OFFSET)
        h: int = _fnv1a(data_ptr, bl)
        store_i64(o, PYSTROBJECT_HASH_OFFSET, h)
        return h
    if tag == PY_TYPE_BYTES:                     # BYTES
        bl: int = load_i64(o, PYBYTESOBJECT_BYTE_LEN_OFFSET)
        data_ptr = ptr_add(o, PYBYTESOBJECT_DATA_OFFSET)
        return _fnv1a(data_ptr, bl)
    if tag == PY_TYPE_LIST:
        py_raise_owned(py_exc_new(3, cstr("unhashable type: 'list'")))
        return -1
    if tag == PY_TYPE_DICT:
        py_raise_owned(py_exc_new(3, cstr("unhashable type: 'dict'")))
        return -1
    if tag == PY_TYPE_SET:
        py_raise_owned(py_exc_new(3, cstr("unhashable type: 'set'")))
        return -1
    if tag == PY_TYPE_BYTEARRAY:
        py_raise_owned(py_exc_new(3, cstr("unhashable type: 'bytearray'")))
        return -1
    if tag == PY_TYPE_TUPLE:                      # TUPLE
        # CPython's tuple_hash (xxHash lanes): XXPRIME_5 seed, each element
        # hash * XXPRIME_2 added, rotated left 31 bits, times XXPRIME_1.
        n: int = load_i64(o, PYTUPLEOBJECT_LEN_OFFSET)
        h: int = 2870177450012600261
        read_barrier_enabled: int = load_i32(
            global_addr("pcc_gc_read_barrier_enabled"), 0
        )
        i: int = 0
        while i < n:
            slot = ptr_add(o, PYTUPLEOBJECT_ITEMS_OFFSET + i * 8)
            el = load_ptr(slot, 0)
            if read_barrier_enabled != 0:
                el = pcc_gc_load_ptr(o, slot)
            el_hash: int = 0
            handled: int = 0
            if ptr_is_null(el) != 0:
                handled = 1
            else:
                if is_tagged_int(el) != 0:
                    el_hash = _hash_i64(untag_int(el))
                    handled = 1
                else:
                    el_tag: int = load_i32(el, PYOBJECTHEADER_TYPE_TAG_OFFSET)
                    if el_tag == PY_TYPE_NONE:
                        el_hash = 4238894112
                        handled = 1
                    elif el_tag == PY_TYPE_BOOL:
                        if ptr_eq(el, global_load_ptr("py_True")) != 0:
                            el_hash = 1
                        handled = 1
                    elif el_tag == PY_TYPE_INT:
                        el_hash = _hash_bigint(el)
                        handled = 1
                    elif el_tag == PY_TYPE_STR:
                        cached: int = load_i64(el, PYSTROBJECT_HASH_OFFSET)
                        if cached != -1:
                            el_hash = cached
                        else:
                            bl: int = load_i64(el, PYSTROBJECT_BYTE_LEN_OFFSET)
                            data_ptr = ptr_add(el, PYSTROBJECT_DATA_OFFSET)
                            el_hash = _fnv1a(data_ptr, bl)
                            store_i64(el, PYSTROBJECT_HASH_OFFSET, el_hash)
                        handled = 1
            if handled == 0:
                el_hash = py_obj_hash(el)
                if py_err_occurred() != 0:
                    return -1
            h = h + el_hash * -4417276706812531889
            h = logical_shift_left_i64(h, 31) | logical_shift_right_i64(h, 33)
            h = h * -7046029288634856825
            i = i + 1
        h = h + (n ^ (2870177450012600261 ^ 3527539))
        if h == -1:
            return 1546275796
        return h
    if tag == PY_TYPE_INSTANCE or tag >= PY_TYPE_USER_CLASS_START:
        return py_user_hash_dispatch(o, null())
    return 0


# ---- Comparison ops -------------------------------------------------

# CPython's rich-comparison codes for the four orderings.
_ORDER_LT = 0
_ORDER_LE = 1
_ORDER_GT = 4
_ORDER_GE = 5
# _builtin_order: neither operand's type defines the ordering (TypeError).
_ORDER_UNSUPPORTED = -2


# A user instance on either side asks its ordering dunders first; -1 leaves
# builtin fallback available, while -2 means both selected methods declined.
def _user_order(a, b, op: int) -> int:
    ta: int = _type_of(a)
    tb: int = _type_of(b)
    if (
        ta != PY_TYPE_INSTANCE
        and ta < PY_TYPE_USER_CLASS_START
        and tb != PY_TYPE_INSTANCE
        and tb < PY_TYPE_USER_CLASS_START
    ):
        return -1
    return py_user_order_dispatch(a, b, op)


def _order_verdict(comparison: int, op: int) -> int:
    # A three-way result as the answer to ``op``; NaN answers False.
    if comparison == PY_OBJ_CMP_UNORDERED:
        return 0
    if op == _ORDER_LT:
        return 1 if comparison < 0 else 0
    if op == _ORDER_LE:
        return 1 if comparison <= 0 else 0
    if op == _ORDER_GT:
        return 1 if comparison > 0 else 0
    return 1 if comparison >= 0 else 0


def _is_number_tag(tag: int) -> int:
    if tag == PY_TYPE_INT or tag == PY_TYPE_BOOL or tag == PY_TYPE_FLOAT:
        return 1
    return 0


def _bytes_orderable(ta: int, tb: int) -> int:
    # bytes orders only against bytes; bytearray against any buffer.
    if ta == PY_TYPE_BYTES and tb == PY_TYPE_BYTES:
        return 1
    if ta == PY_TYPE_BYTEARRAY and _is_bytes_like_tag(tb) != 0:
        return 1
    if tb == PY_TYPE_BYTEARRAY and _is_bytes_like_tag(ta) != 0:
        return 1
    return 0


def _order_message_append(message, length: int, text, count: int) -> int:
    index: int = 0
    while index < count:
        store_i8(message, length, load_i8(text, index))
        length = length + 1
        index = index + 1
    return length


def _order_message_type(message, length: int, obj) -> int:
    name = py_obj_type_name(obj)
    if ptr_is_null(name) == 0:
        count: int = py_str_byte_len(name)
        if count > 100:
            count = 100
        length = _order_message_append(message, length, py_str_utf8(name), count)
        py_decref(name)
    return length


def _order_type_error(a, b, op: int) -> None:
    # CPython: "'<' not supported between instances of 'int' and 'str'".
    message = stack_alloc(320)
    store_i8(message, 0, 39)
    store_i8(message, 1, 62 if op == _ORDER_GT or op == _ORDER_GE else 60)
    length: int = 2
    if op == _ORDER_LE or op == _ORDER_GE:
        store_i8(message, length, 61)
        length = length + 1
    middle = cstr("' not supported between instances of '")
    length = _order_message_append(message, length, middle, strlen(middle))
    length = _order_message_type(message, length, a)
    separator = cstr("' and '")
    length = _order_message_append(message, length, separator, 7)
    length = _order_message_type(message, length, b)
    store_i8(message, length, 39)
    store_i8(message, length + 1, 0)
    py_raise_owned(py_exc_new(3, message))


def _sequence_order(a, b, op: int, is_list: int) -> int:
    # CPython's tuple/list ordering: the first index whose items differ
    # (identity, then ==) decides with ``op`` itself; when one sequence is a
    # prefix of the other, the lengths decide. Lists are re-measured each
    # step because an item's __eq__ may resize them.
    index: int = 0
    verdict: int = -3
    while verdict == -3:
        length_a: int = py_list_len(a) if is_list != 0 else py_tuple_len(a)
        length_b: int = py_list_len(b) if is_list != 0 else py_tuple_len(b)
        if index >= length_a or index >= length_b:
            sizes: int = 0
            if length_a < length_b:
                sizes = -1
            elif length_a > length_b:
                sizes = 1
            verdict = _order_verdict(sizes, op)
        else:
            item_a = py_list_get(a, index) if is_list != 0 else py_tuple_get(a, index)
            item_b = py_list_get(b, index) if is_list != 0 else py_tuple_get(b, index)
            if ptr_eq(item_a, item_b) == 0:
                kind_a: int = PY_TYPE_NONE if ptr_is_null(item_a) != 0 else _type_of(item_a)
                kind_b: int = PY_TYPE_NONE if ptr_is_null(item_b) != 0 else _type_of(item_b)
                if (kind_a == PY_TYPE_STR and kind_b == PY_TYPE_STR) or (
                    _is_number_tag(kind_a) != 0 and _is_number_tag(kind_b) != 0
                ):
                    # == and the ordering agree with the three-way result.
                    comparison: int = _cmp_threeway(item_a, item_b)
                    if comparison != 0:
                        verdict = _order_verdict(comparison, op)
                else:
                    same: int = py_obj_eq(item_a, item_b)
                    if py_err_occurred() != 0:
                        verdict = -1
                    elif same == 0:
                        verdict = _obj_order(item_a, item_b, op)
                        if py_err_occurred() != 0:
                            verdict = -1
            if is_list != 0:
                py_decref(item_a)
                py_decref(item_b)
            index = index + 1
    return verdict


def _order_payload(o):
    # Immutable builtin subclasses (str, tuple) order by their payload.
    payload = py_str_payload(o)
    if ptr_is_null(payload) == 0:
        return payload
    return py_tuple_payload(o)


def _builtin_order(a, b, op: int) -> int:
    """``a <op> b`` without user dunders: 1/0, -1 with an exception set, or
    _ORDER_UNSUPPORTED when neither type defines the ordering."""
    if ptr_is_null(a) != 0 or ptr_is_null(b) != 0:
        return _ORDER_UNSUPPORTED
    ta: int = _type_of(a)
    tb: int = _type_of(b)
    if ta == PY_TYPE_INSTANCE or ta >= PY_TYPE_USER_CLASS_START or (
        tb == PY_TYPE_INSTANCE or tb >= PY_TYPE_USER_CLASS_START
    ):
        payload_a = a
        payload_b = b
        if ta == PY_TYPE_INSTANCE or ta >= PY_TYPE_USER_CLASS_START:
            payload_a = _order_payload(a)
        if tb == PY_TYPE_INSTANCE or tb >= PY_TYPE_USER_CLASS_START:
            payload_b = _order_payload(b)
        if ptr_is_null(payload_a) != 0 or ptr_is_null(payload_b) != 0:
            return _ORDER_UNSUPPORTED
        return _builtin_order(payload_a, payload_b, op)
    if _is_number_tag(ta) != 0 and _is_number_tag(tb) != 0:
        return _order_verdict(_cmp_threeway(a, b), op)
    if ta == PY_TYPE_STR and tb == PY_TYPE_STR:
        return _order_verdict(_cmp_threeway(a, b), op)
    if _bytes_orderable(ta, tb) != 0:
        return _order_verdict(_bytes_cmp(a, b), op)
    if ta == PY_TYPE_TUPLE and tb == PY_TYPE_TUPLE:
        return _sequence_order(a, b, op, 0)
    if ta == PY_TYPE_LIST and tb == PY_TYPE_LIST:
        return _sequence_order(a, b, op, 1)
    if ta == PY_TYPE_SET and tb == PY_TYPE_SET:
        # Sets order by subset/superset (a partial order).
        if op == _ORDER_LT:
            return 1 if py_set_issubset(a, b) != 0 and py_set_len(a) < py_set_len(b) else 0
        if op == _ORDER_LE:
            return py_set_issubset(a, b)
        if op == _ORDER_GT:
            return 1 if py_set_issuperset(a, b) != 0 and py_set_len(a) > py_set_len(b) else 0
        return py_set_issuperset(a, b)
    return _ORDER_UNSUPPORTED


def _obj_order(a, b, op: int) -> int:
    """``a <op> b``: 1/0, or 0 with an exception set. As in CPython, the
    operands' methods answer first, then the builtin ordering; when none
    applies the comparison raises TypeError instead of answering."""
    if ptr_is_null(a) == 0 and ptr_is_null(b) == 0:
        if (
            pcc_capi_is_cext_type_tag(_type_of(a)) != 0
            or pcc_capi_is_cext_type_tag(_type_of(b)) != 0
        ):
            if pcc_capi_cext_richcompare_bool(a, b, op) > 0:
                return 1
            return 0
        user: int = _user_order(a, b, op)
        if user == _ORDER_UNSUPPORTED:
            # Do not revive a builtin-base payload after both overriding
            # descriptors declined. Existing callback/TLS errors win.
            if py_err_occurred() == 0:
                _order_type_error(a, b, op)
            return 0
        if user != -1:
            return user
    verdict: int = _builtin_order(a, b, op)
    if verdict == _ORDER_UNSUPPORTED:
        _order_type_error(a, b, op)
        return 0
    if verdict < 0:
        return 0
    return verdict


@c_abi_export("py_obj_lt")
def py_obj_lt(a, b) -> int:
    return _obj_order(a, b, _ORDER_LT)


@c_abi_export("py_obj_le")
def py_obj_le(a, b) -> int:
    return _obj_order(a, b, _ORDER_LE)


@c_abi_export("py_obj_gt")
def py_obj_gt(a, b) -> int:
    return _obj_order(a, b, _ORDER_GT)


@c_abi_export("py_obj_ge")
def py_obj_ge(a, b) -> int:
    return _obj_order(a, b, _ORDER_GE)


@c_abi_export("py_builtin_any_all")
def py_builtin_any_all(iterable, want_all: int) -> int:
    """any(iterable) (``want_all`` 0) or all(iterable) (1): 1/0, -1 on error.

    CPython's iterator protocol with its short circuit: iteration stops at
    the first truthy (any) or falsy (all) item, so generators, sets, strings
    and user iterables are consumed exactly as far as CPython consumes them,
    and a non-iterable raises iter()'s TypeError.
    """
    if ptr_is_null(iterable) != 0:
        return -1
    it = py_obj_iter(iterable)
    if ptr_is_null(it) != 0:
        return -1
    moving_backend: int = pcc_gc_backend()
    it_slot = stack_alloc(8)
    store_ptr(it_slot, 0, it)
    it_handle = null()
    if moving_backend == 3 or moving_backend == 4:
        it_handle = pcc_gc_scheduler_root_register_handle(it_slot)
        if ptr_is_null(it_handle) != 0:
            py_decref(it)
            return -1
    result: int = want_all
    done: int = 0
    while done == 0:
        if ptr_is_null(it_handle) == 0:
            it = pcc_gc_load_ptr(null(), it_slot)
        item = py_obj_next(it)
        if ptr_is_null(item) != 0:
            done = 1
            if py_err_occurred() != 0:
                if py_exc_matches(py_current_exception(), py_exc_builtin_class(8)) != 0:  # StopIteration
                    py_clear_exception()
                else:
                    result = -1
        else:
            truth: int = py_obj_truthy(item)
            py_decref(item)
            if py_err_occurred() != 0:
                result = -1
                done = 1
            elif want_all != 0 and truth == 0:
                result = 0
                done = 1
            elif want_all == 0 and truth != 0:
                result = 1
                done = 1
    if ptr_is_null(it_handle) == 0:
        it = pcc_gc_load_ptr(null(), it_slot)
        pcc_gc_scheduler_root_unregister_handle(it_handle)
    py_decref(it)
    return result


@c_abi_export("py_obj_min_max")
def py_obj_min_max(iterable, want_max: int):
    """Return the minimum or maximum owned item from an iterable."""
    if ptr_is_null(iterable) != 0:
        return null()
    it = py_obj_iter(iterable)
    if ptr_is_null(it) != 0:
        return null()
    moving_backend: int = pcc_gc_backend()
    it_slot = stack_alloc(8)
    store_ptr(it_slot, 0, it)
    it_handle = null()
    if moving_backend == 3 or moving_backend == 4:
        it_handle = pcc_gc_scheduler_root_register_handle(it_slot)
        if ptr_is_null(it_handle) != 0:
            py_decref(it)
            return null()
        it = pcc_gc_load_ptr(null(), it_slot)

    best = py_obj_next(it)
    if ptr_is_null(it_handle) == 0:
        it = pcc_gc_load_ptr(null(), it_slot)
    if ptr_is_null(best) != 0:
        if py_err_occurred() != 0:
            current = py_current_exception()
            stop = py_exc_builtin_class(8)  # PY_EXC_STOPITERATION
            if py_exc_matches(current, stop) != 0:
                py_clear_exception()
            else:
                if ptr_is_null(it_handle) == 0:
                    pcc_gc_scheduler_root_unregister_handle(it_handle)
                py_decref(it)
                return null()
        if ptr_is_null(it_handle) == 0:
            pcc_gc_scheduler_root_unregister_handle(it_handle)
        py_decref(it)
        if want_max != 0:
            py_raise_owned(py_exc_new(2, cstr("max() iterable argument is empty")))
        else:
            py_raise_owned(py_exc_new(2, cstr("min() iterable argument is empty")))
        return null()

    best_slot = stack_alloc(8)
    element_slot = stack_alloc(8)
    store_ptr(best_slot, 0, best)
    store_ptr(element_slot, 0, null())
    best_handle = null()
    if moving_backend == 3 or moving_backend == 4:
        best_handle = pcc_gc_scheduler_root_register_handle(best_slot)
        if ptr_is_null(best_handle) != 0:
            py_decref(best)
            if ptr_is_null(it_handle) == 0:
                pcc_gc_scheduler_root_unregister_handle(it_handle)
            py_decref(it)
            return null()

    done: int = 0
    failed: int = 0
    while done == 0:
        if ptr_is_null(it_handle) == 0:
            it = pcc_gc_load_ptr(null(), it_slot)
        element = py_obj_next(it)
        if ptr_is_null(it_handle) == 0:
            it = pcc_gc_load_ptr(null(), it_slot)
        if ptr_is_null(best_handle) == 0:
            best = pcc_gc_load_ptr(null(), best_slot)
        if ptr_is_null(element) != 0:
            if py_err_occurred() != 0:
                current = py_current_exception()
                stop = py_exc_builtin_class(8)  # PY_EXC_STOPITERATION
                if py_exc_matches(current, stop) != 0:
                    py_clear_exception()
                else:
                    if ptr_is_null(best_handle) == 0:
                        best = pcc_gc_load_ptr(null(), best_slot)
                        pcc_gc_scheduler_root_unregister_handle(best_handle)
                    py_decref(best)
                    if ptr_is_null(it_handle) == 0:
                        pcc_gc_scheduler_root_unregister_handle(it_handle)
                    py_decref(it)
                    return null()
            done = 1
        else:
            store_ptr(element_slot, 0, element)
            element_handle = null()
            if moving_backend == 3 or moving_backend == 4:
                element_handle = pcc_gc_scheduler_root_register_handle(element_slot)
                if ptr_is_null(element_handle) != 0:
                    py_decref(element)
                    if ptr_is_null(best_handle) == 0:
                        best = pcc_gc_load_ptr(null(), best_slot)
                        pcc_gc_scheduler_root_unregister_handle(best_handle)
                    py_decref(best)
                    if ptr_is_null(it_handle) == 0:
                        pcc_gc_scheduler_root_unregister_handle(it_handle)
                    py_decref(it)
                    return null()
            replace: int = 0
            if want_max != 0:
                replace = py_obj_gt(element, best)
            else:
                replace = py_obj_lt(element, best)
            if py_err_occurred() != 0:
                replace = 0
                failed = 1
                done = 1
            if ptr_is_null(best_handle) == 0:
                best = pcc_gc_load_ptr(null(), best_slot)
            if ptr_is_null(element_handle) == 0:
                element = pcc_gc_load_ptr(null(), element_slot)
            if replace != 0:
                old_best_slot = best_slot
                best_slot = element_slot
                element_slot = old_best_slot
                old_best_handle = best_handle
                best_handle = element_handle
                element_handle = old_best_handle
                best = element
            discard = pcc_gc_load_ptr(null(), element_slot)
            store_ptr(element_slot, 0, null())
            if ptr_is_null(element_handle) == 0:
                pcc_gc_scheduler_root_unregister_handle(element_handle)
            py_decref(discard)

    if ptr_is_null(it_handle) == 0:
        pcc_gc_scheduler_root_unregister_handle(it_handle)
    py_decref(it)
    if ptr_is_null(best_handle) == 0:
        best = pcc_gc_load_ptr(null(), best_slot)
        pcc_gc_scheduler_root_unregister_handle(best_handle)
    if failed != 0:
        py_decref(best)
        return null()
    return best


# ---- sorted (insertion sort) — fixed break logic --------------------

# Sorted uses integer index buffers; every managed value lives in this frame.
_SORT_SOURCE = 0
_SORT_KEY = 1
_SORT_COMPARE = 2
_SORT_VALUES = 3
_SORT_KEYS = 4
_SORT_RESULT = 5
_SORT_ARGS = 6
_SORT_ITEM = 7
_SORT_LEFT = 8
_SORT_RIGHT = 9
_SORT_ERROR = 10
_SORT_COUNT = 11
_SORT_BYTES = 8

define_global_i32("pcc_sorted_owned_map", 11)
pcc_gc_frame_enter = extern("pcc_gc_frame_enter", (c_ptr, c_ptr), c_void)
pcc_gc_frame_leave = extern("pcc_gc_frame_leave", (c_ptr,), c_void)
pcc_gc_store_root = extern("pcc_gc_store_root", (c_ptr, c_ptr), c_void)
pcc_gc_root_copy_lease = extern("pcc_gc_root_copy_lease", (c_ptr, c_ptr), c_int64)
pcc_gc_root_move = extern("pcc_gc_root_move", (c_ptr, c_ptr), c_int64)
pcc_gc_foreign_lease_acquire = extern("pcc_gc_foreign_lease_acquire", (c_ptr,), c_int64)
pcc_gc_foreign_lease_release = extern("pcc_gc_foreign_lease_release", (c_ptr, c_int64), c_int64)
pcc_gc_note_slot_write_barrier = extern("pcc_gc_note_slot_write_barrier", (c_ptr, c_ptr, c_ptr), c_void)
pcc_platform_abort = extern("pcc_platform_abort", (), c_void)
py_tls_exc_swap_slot = extern("py_tls_exc_swap_slot", (c_ptr,), c_void)
py_list_extend = extern("py_list_extend", (c_ptr, c_ptr), c_void)
py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_tuple_set_item = extern("py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void)
py_obj_call_slots = extern("py_obj_call_slots", (c_ptr, c_ptr, c_ptr, c_ptr), c_int64)


def _sorted_error(message) -> int:
    if py_err_occurred() == 0:
        py_raise_owned(py_exc_new(19, message))
    return -1


def _sorted_drop(slots, tokens, index: int) -> None:
    slot = ptr_add(slots, index * _SORT_BYTES)
    token: int = load_i64(tokens, index * _SORT_BYTES)
    if token >= 0:
        if pcc_gc_foreign_lease_release(slot, token) < 0:
            pcc_platform_abort()
            return
    store_i64(tokens, index * _SORT_BYTES, -1)
    pcc_gc_store_root(slot, null())


def _sorted_adopt(slots, tokens, index: int) -> int:
    slot = ptr_add(slots, index * _SORT_BYTES)
    if ptr_is_null(load_ptr(slot, 0)) != 0:
        return _sorted_error(cstr("sorted: missing owned value"))
    token: int = pcc_gc_foreign_lease_acquire(slot)
    store_i64(tokens, index * _SORT_BYTES, token)
    if token < 0:
        return _sorted_error(cstr("sorted: cannot lease owned value"))
    pcc_gc_note_slot_write_barrier(null(), slot, load_ptr(slot, 0))
    if py_err_occurred() != 0:
        return -1
    return 0


def _sorted_copy(slots, tokens, index: int, source) -> int:
    if ptr_is_null(source) != 0:
        return 0
    token: int = pcc_gc_root_copy_lease(ptr_add(slots, index * _SORT_BYTES), source)
    store_i64(tokens, index * _SORT_BYTES, token)
    if token < 0:
        return _sorted_error(cstr("sorted: cannot retain input"))
    return 0


def _sorted_arguments(slots, tokens, first, second) -> int:
    count: int = 1
    if ptr_is_null(second) == 0:
        count = 2
    arguments = ptr_add(slots, _SORT_ARGS * _SORT_BYTES)
    store_ptr(arguments, 0, py_tuple_new(count))
    if _sorted_adopt(slots, tokens, _SORT_ARGS) != 0:
        return -1
    py_tuple_set_item(load_ptr(arguments, 0), 0, load_ptr(first, 0))
    if py_err_occurred() != 0:
        return -1
    if count == 2:
        py_tuple_set_item(load_ptr(arguments, 0), 1, load_ptr(second, 0))
        if py_err_occurred() != 0:
            return -1
    return 0


def _sorted_prepare_keys(slots, tokens) -> int:
    values = ptr_add(slots, _SORT_VALUES * _SORT_BYTES)
    keys = ptr_add(slots, _SORT_KEYS * _SORT_BYTES)
    key = ptr_add(slots, _SORT_KEY * _SORT_BYTES)
    item = ptr_add(slots, _SORT_ITEM * _SORT_BYTES)
    result = ptr_add(slots, _SORT_LEFT * _SORT_BYTES)
    callable_key: int = 0
    if ptr_is_null(load_ptr(key, 0)) == 0:
        if _type_of(load_ptr(key, 0)) != PY_TYPE_NONE:
            callable_key = 1
    if callable_key == 0:
        return _sorted_copy(slots, tokens, _SORT_KEYS, values)
    n: int = py_list_len(load_ptr(values, 0))
    store_ptr(keys, 0, py_list_new(n))
    if _sorted_adopt(slots, tokens, _SORT_KEYS) != 0:
        return -1
    index: int = 0
    while index < n:
        store_ptr(item, 0, py_list_get(load_ptr(values, 0), index))
        if _sorted_adopt(slots, tokens, _SORT_ITEM) != 0:
            return -1
        if _sorted_arguments(slots, tokens, item, null()) != 0:
            return -1
        if py_obj_call_slots(key, ptr_add(slots, _SORT_ARGS * _SORT_BYTES), null(), result) != 0:
            return -1
        if _sorted_adopt(slots, tokens, _SORT_LEFT) != 0:
            return -1
        py_list_append(load_ptr(keys, 0), load_ptr(result, 0))
        if py_err_occurred() != 0:
            return -1
        _sorted_drop(slots, tokens, _SORT_LEFT)
        _sorted_drop(slots, tokens, _SORT_ARGS)
        _sorted_drop(slots, tokens, _SORT_ITEM)
        index = index + 1
    return 0


def _sorted_less(slots, tokens, first: int, second: int) -> int:
    keys = ptr_add(slots, _SORT_KEYS * _SORT_BYTES)
    left = ptr_add(slots, _SORT_LEFT * _SORT_BYTES)
    right = ptr_add(slots, _SORT_RIGHT * _SORT_BYTES)
    compare = ptr_add(slots, _SORT_COMPARE * _SORT_BYTES)
    item = ptr_add(slots, _SORT_ITEM * _SORT_BYTES)
    store_ptr(left, 0, py_list_get(load_ptr(keys, 0), first))
    if _sorted_adopt(slots, tokens, _SORT_LEFT) != 0:
        return -1
    store_ptr(right, 0, py_list_get(load_ptr(keys, 0), second))
    if _sorted_adopt(slots, tokens, _SORT_RIGHT) != 0:
        return -1
    verdict: int = 0
    if ptr_is_null(load_ptr(compare, 0)) == 0:
        if _sorted_arguments(slots, tokens, left, right) != 0:
            return -1
        if py_obj_call_slots(compare, ptr_add(slots, _SORT_ARGS * _SORT_BYTES), null(), item) != 0:
            return -1
        if _sorted_adopt(slots, tokens, _SORT_ITEM) != 0:
            return -1
        verdict = py_obj_truthy(load_ptr(item, 0))
    else:
        verdict = py_obj_lt(load_ptr(left, 0), load_ptr(right, 0))
    if py_err_occurred() != 0:
        return -1
    _sorted_drop(slots, tokens, _SORT_ITEM)
    _sorted_drop(slots, tokens, _SORT_ARGS)
    _sorted_drop(slots, tokens, _SORT_RIGHT)
    _sorted_drop(slots, tokens, _SORT_LEFT)
    return verdict


def _sorted_merge_indices(slots, tokens, order, scratch, count: int, reverse: int) -> int:
    width: int = 1
    while width < count:
        low: int = 0
        while low < count:
            middle: int = low + width
            if middle > count:
                middle = count
            high: int = middle + width
            if high > count:
                high = count
            left: int = low
            right: int = middle
            out: int = low
            while out < high:
                take_right: int = 0
                if left >= middle:
                    take_right = 1
                elif right < high:
                    # Ties always choose the left run, including reverse=True.
                    # Reversing a completed ascending list would reverse ties.
                    if reverse != 0:
                        take_right = _sorted_less(slots, tokens, load_i64(order, left * 8), load_i64(order, right * 8))
                    else:
                        take_right = _sorted_less(slots, tokens, load_i64(order, right * 8), load_i64(order, left * 8))
                    if take_right < 0:
                        return -1
                if take_right != 0:
                    store_i64(scratch, out * 8, load_i64(order, right * 8))
                    right = right + 1
                else:
                    store_i64(scratch, out * 8, load_i64(order, left * 8))
                    left = left + 1
                out = out + 1
            low = high
        index: int = 0
        while index < count:
            store_i64(order, index * 8, load_i64(scratch, index * 8))
            index = index + 1
        width = width * 2
    return 0


@c_abi_export("py_obj_sorted_slots")
def py_obj_sorted_slots(source_slot, key_slot, compare_slot, reverse: int, result_slot) -> int:
    if ptr_is_null(source_slot) != 0 or ptr_is_null(result_slot) != 0:
        return _sorted_error(cstr("sorted: missing source or result root"))
    if ptr_is_null(load_ptr(result_slot, 0)) == 0:
        return _sorted_error(cstr("sorted: result root must be empty"))
    slots = stack_alloc(_SORT_COUNT * _SORT_BYTES)
    tokens = stack_alloc(_SORT_COUNT * _SORT_BYTES)
    memset(slots, 0, _SORT_COUNT * _SORT_BYTES)
    index: int = 0
    while index < _SORT_COUNT:
        store_i64(tokens, index * _SORT_BYTES, -1)
        index = index + 1
    pcc_gc_frame_enter(global_addr("pcc_sorted_owned_map"), slots)
    status: int = _sorted_copy(slots, tokens, _SORT_SOURCE, source_slot)
    if status == 0:
        status = _sorted_copy(slots, tokens, _SORT_KEY, key_slot)
    if status == 0:
        status = _sorted_copy(slots, tokens, _SORT_COMPARE, compare_slot)
    source = ptr_add(slots, _SORT_SOURCE * _SORT_BYTES)
    values = ptr_add(slots, _SORT_VALUES * _SORT_BYTES)
    result = ptr_add(slots, _SORT_RESULT * _SORT_BYTES)
    item = ptr_add(slots, _SORT_ITEM * _SORT_BYTES)
    error = ptr_add(slots, _SORT_ERROR * _SORT_BYTES)
    if status == 0:
        store_ptr(values, 0, py_list_new(0))
        status = _sorted_adopt(slots, tokens, _SORT_VALUES)
    if status == 0:
        py_list_extend(load_ptr(values, 0), load_ptr(source, 0))
        if py_err_occurred() != 0:
            status = -1
    if status == 0:
        status = _sorted_prepare_keys(slots, tokens)
    count: int = 0
    order = null()
    scratch = null()
    if status == 0:
        count = py_list_len(load_ptr(values, 0))
        if count > 1152921504606846975:
            status = _sorted_error(cstr("sorted: index buffer is too large"))
        elif count > 0:
            order = malloc(count * 8)
            scratch = malloc(count * 8)
            if ptr_is_null(order) != 0 or ptr_is_null(scratch) != 0:
                status = _sorted_error(cstr("sorted: cannot allocate index buffer"))
    if status == 0:
        index = 0
        while index < count:
            store_i64(order, index * 8, index)
            index = index + 1
        status = _sorted_merge_indices(slots, tokens, order, scratch, count, reverse)
    if status == 0:
        store_ptr(result, 0, py_list_new(count))
        status = _sorted_adopt(slots, tokens, _SORT_RESULT)
    index = 0
    while index < count and status == 0:
        store_ptr(item, 0, py_list_get(load_ptr(values, 0), load_i64(order, index * 8)))
        status = _sorted_adopt(slots, tokens, _SORT_ITEM)
        if status == 0:
            py_list_append(load_ptr(result, 0), load_ptr(item, 0))
            if py_err_occurred() != 0:
                status = -1
        if status == 0:
            _sorted_drop(slots, tokens, _SORT_ITEM)
        index = index + 1
    if status == 0:
        status = pcc_gc_root_move(result_slot, result)
        if status == 0:
            token: int = load_i64(tokens, _SORT_RESULT * _SORT_BYTES)
            store_i64(tokens, _SORT_RESULT * _SORT_BYTES, -1)
            if pcc_gc_foreign_lease_release(result_slot, token) < 0:
                pcc_platform_abort()
                return -1
        else:
            _sorted_error(cstr("sorted: cannot transfer result owner"))
    py_tls_exc_swap_slot(error)
    free(scratch)
    free(order)
    index = _SORT_ERROR - 1
    while index >= 0:
        _sorted_drop(slots, tokens, index)
        index = index - 1
    py_clear_exception()
    py_tls_exc_swap_slot(error)
    pcc_gc_frame_leave(slots)
    return status


def _sort_before(left, right) -> int:
    """``left < right`` for py_obj_sorted: 1/0, or -1 with an exception set.
    Same-kind str and number pairs skip the protocol dispatch."""
    if ptr_is_null(left) == 0 and ptr_is_null(right) == 0:
        kind_left: int = _type_of(left)
        kind_right: int = _type_of(right)
        if (kind_left == PY_TYPE_STR and kind_right == PY_TYPE_STR) or (
            _is_number_tag(kind_left) != 0 and _is_number_tag(kind_right) != 0
        ):
            return 1 if _cmp_threeway(left, right) == -1 else 0
    verdict: int = _obj_order(left, right, _ORDER_LT)
    if py_err_occurred() != 0:
        return -1
    return verdict


def _sorted_release_list(items) -> None:
    # Balanced slot clears, then the owner itself (see the merge below).
    count: int = py_list_len(items)
    index: int = 0
    while index < count:
        py_list_set(items, index, null())
        index = index + 1
    store_i64(items, 16, 0)
    pcc_gc_unpin(items)
    py_decref(items)


@c_abi_export("py_obj_sorted")
def py_obj_sorted(x):
    if ptr_is_null(x) != 0:
        return null()
    moving_backend: int = pcc_gc_backend()
    x_slot = stack_alloc(8)
    store_ptr(x_slot, 0, x)
    x_handle = null()
    if moving_backend == 3 or moving_backend == 4:
        x_handle = pcc_gc_scheduler_root_register_handle(x_slot)
        if ptr_is_null(x_handle) != 0:
            return null()
        x = pcc_gc_load_ptr(null(), x_slot)
    n: int = py_obj_len(x)
    if ptr_is_null(x_handle) == 0:
        x = pcc_gc_load_ptr(null(), x_slot)
    # py_obj_len is a sizing hint only; a custom iterator (no __len__) raises
    # from it, and a pending exception would abort the iterator loop below
    # (yielding []). Clear it — the iterator branch handles length-less srcs.
    if py_err_occurred() != 0:
        py_clear_exception()
        n = 0
    out = py_list_new(n)
    if ptr_is_null(out) != 0:
        if ptr_is_null(x_handle) == 0:
            pcc_gc_scheduler_root_unregister_handle(x_handle)
        return null()
    pcc_gc_pin(out)
    if _type_of(x) == PY_TYPE_SET:
        dummy = global_load_ptr("py_set_dummy")
        i: int = 0
        set_done: int = 0
        while set_done == 0:
            if ptr_is_null(x_handle) == 0:
                x = pcc_gc_load_ptr(null(), x_slot)
            entries = load_ptr(x, 40)
            capacity: int = load_i64(x, 24)
            if i >= capacity:
                set_done = 1
                continue
            key = _set_key(x, entries, i * 16)
            if ptr_is_null(key) == 0:
                if ptr_eq(key, dummy) == 0:
                    py_list_append(out, key)
            i = i + 1
    elif _type_of(x) == PY_TYPE_DICT:
        # dict -> sort its keys. py_obj_getitem(dict, int) would look up the
        # int as a KEY (returns NULL -> [<null>,...]); use py_dict_keys instead.
        # (The C py_obj_sorted uses the iterator protocol for all non-indexables;
        # the port handles the common dict case here — generator/range in
        # default mode still fall to the indexable else-path, a follow-on.)
        keys = py_dict_keys(x)
        if ptr_is_null(keys) == 0:
            pcc_gc_pin(keys)
            nk: int = py_list_len(keys)
            ki: int = 0
            while ki < nk:
                el = py_list_get(keys, ki)
                py_list_append(out, el)
                py_decref(el)
                ki = ki + 1
            pcc_gc_unpin(keys)
            py_decref(keys)
    else:
        # General length-less iterable (custom __iter__/__next__ class,
        # generator, range): use the iterator protocol — matching the C
        # py_obj_sorted. The old index-based py_obj_len + py_obj_getitem
        # path yielded [] for anything without __len__/__getitem__.
        it = py_obj_iter(x)
        if ptr_is_null(it) == 0:
            it_slot = stack_alloc(8)
            store_ptr(it_slot, 0, it)
            it_handle = null()
            if moving_backend == 3 or moving_backend == 4:
                it_handle = pcc_gc_scheduler_root_register_handle(it_slot)
                if ptr_is_null(it_handle) != 0:
                    py_decref(it)
                    pcc_gc_unpin(out)
                    py_decref(out)
                    if ptr_is_null(x_handle) == 0:
                        pcc_gc_scheduler_root_unregister_handle(x_handle)
                    return null()
                it = pcc_gc_load_ptr(null(), it_slot)
            it_done: int = 0
            while it_done == 0:
                if ptr_is_null(it_handle) == 0:
                    it = pcc_gc_load_ptr(null(), it_slot)
                el = py_obj_next(it)
                if ptr_is_null(it_handle) == 0:
                    it = pcc_gc_load_ptr(null(), it_slot)
                if ptr_is_null(el) != 0:
                    if py_err_occurred() != 0:
                        cur = py_current_exception()
                        stop = py_exc_builtin_class(8)
                        if py_exc_matches(cur, stop) != 0:
                            py_clear_exception()
                    it_done = 1
                else:
                    py_list_append(out, el)
                    py_decref(el)
            if ptr_is_null(it_handle) == 0:
                it = pcc_gc_load_ptr(null(), it_slot)
                pcc_gc_scheduler_root_unregister_handle(it_handle)
            py_decref(it)
    m: int = py_list_len(out)
    if m > 1:
        # Bottom-up stable merge sort, mirroring the C runtime (was
        # insertion sort — O(n^2) comparisons dominated codegen-worker
        # profiles via sorted symbol lists). Ping-pong between ``out``
        # and a scratch py_list: elements are MOVED borrowed (raw
        # barrier loads, py_list_append stores without incref), every
        # element stays GC-visible in at least one list slot, and the
        # scratch is emptied by resetting its length field before
        # release so aliasing slots never trigger element decrefs.
        # Stability: the right run only wins when strictly smaller.
        scratch = py_list_new(m)
        if ptr_is_null(scratch) == 0:
            pcc_gc_pin(scratch)
            src_list = out
            dst_list = scratch
            width: int = 1
            failed: int = 0
            while width < m and failed == 0:
                # Reset dst through the BALANCED slot store (py_list_set
                # -> pcc_gc_store_ptr increfs new / decrefs old): a bare
                # length=0 write would leak one reference per element
                # per pass. Every element stays alive via its other
                # list's slot.
                dlen: int = py_list_len(dst_list)
                di: int = 0
                while di < dlen:
                    py_list_set(dst_list, di, null())
                    di = di + 1
                store_i64(dst_list, 16, 0)
                src_items = load_ptr(src_list, PYLISTOBJECT_ITEMS_OFFSET)
                lo: int = 0
                while lo < m and failed == 0:
                    mid: int = lo + width
                    if mid > m:
                        mid = m
                    hi: int = mid + width
                    if hi > m:
                        hi = m
                    mi: int = lo
                    mj: int = mid
                    while mi < mid and mj < hi and failed == 0:
                        ea = pcc_gc_load_ptr(
                            src_list, ptr_add(src_items, mi * 8)
                        )
                        eb = pcc_gc_load_ptr(
                            src_list, ptr_add(src_items, mj * 8)
                        )
                        before: int = _sort_before(eb, ea)
                        if before < 0:
                            failed = 1
                        elif before != 0:
                            eb = pcc_gc_load_ptr(
                                src_list, ptr_add(src_items, mj * 8)
                            )
                            py_list_append(dst_list, eb)
                            mj = mj + 1
                        else:
                            ea = pcc_gc_load_ptr(
                                src_list, ptr_add(src_items, mi * 8)
                            )
                            py_list_append(dst_list, ea)
                            mi = mi + 1
                    while mi < mid and failed == 0:
                        py_list_append(
                            dst_list,
                            pcc_gc_load_ptr(
                                src_list, ptr_add(src_items, mi * 8)
                            ),
                        )
                        mi = mi + 1
                    while mj < hi and failed == 0:
                        py_list_append(
                            dst_list,
                            pcc_gc_load_ptr(
                                src_list, ptr_add(src_items, mj * 8)
                            ),
                        )
                        mj = mj + 1
                    lo = lo + 2 * width
                tmp = src_list
                src_list = dst_list
                dst_list = tmp
                width = width * 2
            if failed != 0:
                # A comparison raised: both lists hold counted element
                # references; release them and propagate the exception.
                _sorted_release_list(scratch)
                _sorted_release_list(out)
                if ptr_is_null(x_handle) == 0:
                    pcc_gc_scheduler_root_unregister_handle(x_handle)
                return null()
            if ptr_eq(src_list, out) == 0:
                # Final ordering ended in the scratch: move it back
                # (balanced: clear out's stale aliases first — each
                # element stays held by the scratch slot).
                olen: int = py_list_len(out)
                oi: int = 0
                while oi < olen:
                    py_list_set(out, oi, null())
                    oi = oi + 1
                store_i64(out, 16, 0)
                back_items = load_ptr(src_list, PYLISTOBJECT_ITEMS_OFFSET)
                bi: int = 0
                while bi < m:
                    py_list_append(
                        out,
                        pcc_gc_load_ptr(
                            src_list, ptr_add(back_items, bi * 8)
                        ),
                    )
                    bi = bi + 1
            # Release the scratch's element references (balanced) and
            # the scratch itself; out's slots keep the elements.
            slen: int = py_list_len(scratch)
            si: int = 0
            while si < slen:
                py_list_set(scratch, si, null())
                si = si + 1
            store_i64(scratch, 16, 0)
            pcc_gc_unpin(scratch)
            py_decref(scratch)
            pcc_gc_unpin(out)
            if ptr_is_null(x_handle) == 0:
                pcc_gc_scheduler_root_unregister_handle(x_handle)
            return out
        # malloc-failure fallback: original insertion sort.
        insertion_failed: int = 0
        j: int = 1
        while j < m:
            cur = py_list_get(out, j)
            k: int = j
            # Use a `done` flag in the loop condition (NOT mutating
            # k=0) — earlier port mutated k=0 to "break" then
            # unconditionally wrote cur to out[k].
            done: int = 0
            while k > 0 and done == 0:
                prev = py_list_get(out, k - 1)
                before: int = _sort_before(cur, prev)
                if before <= 0:
                    py_decref(prev)
                    done = 1
                    if before < 0:
                        insertion_failed = 1
                else:
                    py_list_set(out, k, prev)
                    py_decref(prev)
                    k = k - 1
            py_list_set(out, k, cur)
            py_decref(cur)
            j = j + 1
            if insertion_failed != 0:
                j = m
        if insertion_failed != 0:
            pcc_gc_unpin(out)
            py_decref(out)
            if ptr_is_null(x_handle) == 0:
                pcc_gc_scheduler_root_unregister_handle(x_handle)
            return null()
    pcc_gc_unpin(out)
    if ptr_is_null(x_handle) == 0:
        pcc_gc_scheduler_root_unregister_handle(x_handle)
    return out


# ---- Membership -----------------------------------------------------

@c_abi_export("py_obj_contains")
def py_obj_contains(container, item) -> int:
    if ptr_is_null(container) != 0:
        return 0
    tag: int = _type_of(container)
    if tag == PY_TYPE_LIST:                      # LIST
        if py_list_contains(container, item) != 0:
            return 1
        return 0
    if tag == PY_TYPE_TUPLE:                      # TUPLE — linear scan via py_obj_eq
        n: int = py_tuple_len(container)
        i: int = 0
        while i < n:
            el = py_tuple_get(container, i)
            equal: int = py_obj_eq(el, item)
            py_decref(el)
            if py_err_occurred() != 0:
                return 0
            if equal != 0:
                return 1
            i = i + 1
        return 0
    if tag == PY_TYPE_DICT:                      # DICT
        if py_dict_contains(container, item) != 0:
            return 1
        return 0
    if tag == PY_TYPE_SET:                      # SET
        if py_set_contains(container, item) != 0:
            return 1
        return 0
    if tag == PY_TYPE_STR:                      # STR
        if py_str_contains(container, item) != 0:
            return 1
        return 0
    if tag == PY_TYPE_BYTES or tag == PY_TYPE_BYTEARRAY:
        return 1 if py_bytes_find(container, item) >= 0 else 0
    if tag == PY_TYPE_INSTANCE or tag >= PY_TYPE_USER_CLASS_START or tag == PY_TYPE_CLASS:
        # ``item in cls`` calls the metaclass's __contains__ (enum classes).
        return py_user_contains_dispatch(container, item, null())
    return 0
