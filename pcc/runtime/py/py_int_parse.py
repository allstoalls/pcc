"""Phase 4c: pcc-Python port of py_int_parse.c.

String-to-int parsing for int(str) / int(str, base). The arithmetic core
stays in py_int.c for now; this module only parses an int64 payload and
delegates canonical tagged-vs-heap construction to py_int_from_i64.
"""

__pcc_runtime_port__ = True

from pcc.extern import extern, c_abi_export, c_ptr, c_int64, c_void, c_double
from pcc.runtime.py.py_abi_constants import (
    PYBYTESOBJECT_BYTE_LEN_OFFSET,
    PYBYTESOBJECT_DATA_OFFSET,
    PYMEMORYVIEWOBJECT_BASE_OFFSET,
    PY_TYPE_BOOL,
    PY_TYPE_BYTEARRAY,
    PY_TYPE_BYTES,
    PY_TYPE_FLOAT,
    PY_TYPE_INSTANCE,
    PY_TYPE_INT,
    PY_TYPE_MEMORYVIEW,
    PY_TYPE_STR,
    PY_TYPE_USER_CLASS_START,
)
from pcc.unsafe import (
    cstr,
    f64_bits,
    f64_signbit,
    free,
    global_load_ptr,
    load_i8,
    load_i32,
    load_i64,
    malloc,
    null,
    ptr_add,
    ptr_is_null,
    stack_alloc,
    store_i8,
    store_i32,
    store_i64,
    store_ptr,
    strlen,
)


py_int_from_i64 = extern("py_int_from_i64", (c_int64,),       c_ptr)
py_int_mul      = extern("py_int_mul",      (c_ptr, c_ptr),   c_ptr)
py_int_add      = extern("py_int_add",      (c_ptr, c_ptr),   c_ptr)
py_decref       = extern("py_decref",       (c_ptr,),         c_void)
py_obj_type_tag  = extern("py_obj_type_tag",  (c_ptr,),         c_int64)
py_str_utf8      = extern("py_str_utf8",      (c_ptr,),         c_ptr)
py_float_to_f64  = extern("py_float_to_f64",  (c_ptr,),         c_double)
py_obj_truthy    = extern("py_obj_truthy",    (c_ptr,),         c_int64)
py_incref        = extern("py_incref",        (c_ptr,),         c_void)
py_raise        = extern("py_raise",        (c_ptr,),         c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_exc_new      = extern("py_exc_new",      (c_int64, c_ptr), c_ptr)
py_obj_getattr = extern("py_obj_getattr", (c_ptr, c_ptr), c_ptr)
py_obj_call = extern("py_obj_call", (c_ptr, c_ptr, c_ptr), c_ptr)
py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_clear_exception = extern("py_clear_exception", (), c_void)
py_obj_type_name = extern("py_obj_type_name", (c_ptr,), c_ptr)
py_str_byte_len = extern("py_str_byte_len", (c_ptr,), c_int64)
py_obj_index = extern("py_obj_index", (c_ptr,), c_ptr)
py_int_to_i64 = extern("py_int_to_i64", (c_ptr, c_ptr), c_int64)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)
_int_open_root = extern("_pcc_int_bytes_open_root", (c_ptr, c_ptr), c_ptr)
_int_close_root = extern("_pcc_int_bytes_close_root", (c_ptr, c_ptr), c_void)
_int_save_result = extern("_pcc_int_bytes_save_result", (c_ptr, c_ptr), c_ptr)
_int_take_result = extern("_pcc_int_bytes_take_result", (c_ptr, c_ptr), c_ptr)
pcc_stdio_float_exact_digits = extern(
    "pcc_stdio_float_exact_digits", (c_int64, c_ptr, c_ptr), c_int64
)


def _byte_at(s: c_ptr, i: int) -> int:
    return load_i8(s, i) & 0xFF


def _is_space(c: int) -> int:
    if c == 32:
        return 1
    if c == 9:
        return 1
    if c == 10:
        return 1
    if c == 13:
        return 1
    return 0


def _digit_value(c: int) -> int:
    if c >= 48:
        if c <= 57:
            return c - 48
    if c >= 97:
        if c <= 122:
            return c - 97 + 10
    if c >= 65:
        if c <= 90:
            return c - 65 + 10
    return -1


def _has_prefix(s: c_ptr, i: int, lo: int, hi: int) -> int:
    if _byte_at(s, i) != 48:
        return 0
    c: int = _byte_at(s, i + 1)
    if c == lo:
        return 1
    if c == hi:
        return 1
    return 0


def _parse_bigint(s: c_ptr, start: int, base: int, negative: int):
    # Bigint fallback for decimals/values that exceed int64. Accumulates with
    # the general int ops (py_int_mul / py_int_add), which handle bignum.
    # ``start`` is the first digit position (after sign/prefix).
    base_obj = py_int_from_i64(base)
    acc = py_int_from_i64(0)
    i: int = start
    done: int = 0
    while done == 0:
        ch: int = _byte_at(s, i)
        if ch == 0:
            done = 1
        else:
            d: int = _digit_value(ch)
            if d < 0:
                done = 1
            elif d >= base:
                done = 1
            else:
                prod = py_int_mul(acc, base_obj)
                py_decref(acc)
                dobj = py_int_from_i64(d)
                acc = py_int_add(prod, dobj)
                py_decref(prod)
                py_decref(dobj)
                i = i + 1
    py_decref(base_obj)
    # Trailing: optional whitespace then NUL, matching the int64 path.
    while _is_space(_byte_at(s, i)) != 0:
        i = i + 1
    if _byte_at(s, i) != 0:
        py_decref(acc)
        return null()
    if negative != 0:
        neg_one = py_int_from_i64(-1)
        res = py_int_mul(acc, neg_one)
        py_decref(acc)
        py_decref(neg_one)
        return res
    return acc


@c_abi_export("py_int_from_cstr")
def py_int_from_cstr(s: c_ptr, base: int):
    # NULL input matches the C helper's parse-error contract.
    if ptr_is_null(s):
        return null()

    i: int = 0
    while _is_space(_byte_at(s, i)) != 0:
        i = i + 1

    negative: int = 0
    c: int = _byte_at(s, i)
    if c == 43 or c == 45:
        if c == 45:
            negative = 1
        i = i + 1

    if base == 0:
        if _has_prefix(s, i, 120, 88) != 0:
            base = 16
            i = i + 2
        elif _has_prefix(s, i, 98, 66) != 0:
            base = 2
            i = i + 2
        elif _has_prefix(s, i, 111, 79) != 0:
            base = 8
            i = i + 2
        elif _byte_at(s, i) == 48:
            base = 8
        else:
            base = 10
    elif base == 16:
        if _has_prefix(s, i, 120, 88) != 0:
            i = i + 2
    elif base == 2:
        if _has_prefix(s, i, 98, 66) != 0:
            i = i + 2
    elif base == 8:
        if _has_prefix(s, i, 111, 79) != 0:
            i = i + 2

    if base < 2:
        return null()
    if base > 36:
        return null()

    limit: int = 9223372036854775807
    max_div: int = limit // base
    max_rem: int = limit - max_div * base
    neg_div: int = max_div
    neg_rem: int = max_rem
    if negative != 0:
        if neg_rem == base - 1:
            neg_div = neg_div + 1
            neg_rem = 0
        else:
            neg_rem = neg_rem + 1

    digits_start: int = i
    value: int = 0
    saw_digit: int = 0
    min_exact: int = 0
    done: int = 0
    while done == 0:
        ch: int = _byte_at(s, i)
        if ch == 0:
            done = 1
        else:
            d: int = _digit_value(ch)
            if d < 0:
                done = 1
            elif d >= base:
                done = 1
            else:
                div_limit: int = max_div
                rem_limit: int = max_rem
                if negative != 0:
                    div_limit = neg_div
                    rem_limit = neg_rem
                if value > div_limit:
                    return _parse_bigint(s, digits_start, base, negative)
                if value == div_limit:
                    if d > rem_limit:
                        return _parse_bigint(s, digits_start, base, negative)
                    if negative != 0:
                        if value > max_div:
                            min_exact = 1
                        elif value == max_div:
                            if d > max_rem:
                                min_exact = 1
                saw_digit = 1
                i = i + 1
                if min_exact != 0:
                    done = 1
                else:
                    value = value * base + d

    if saw_digit == 0:
        return null()

    while _is_space(_byte_at(s, i)) != 0:
        i = i + 1
    if _byte_at(s, i) != 0:
        return null()

    if negative != 0:
        if min_exact != 0:
            min_i64: int = -9223372036854775807
            min_i64 = min_i64 - 1
            return py_int_from_i64(min_i64)
        return py_int_from_i64(0 - value)
    return py_int_from_i64(value)


def _repr_append_byte(buf: c_ptr, n: int, c: int, quote: int) -> int:
    # Append the CPython-style repr of source byte ``c`` into ``buf`` at
    # position ``n``, using ``quote`` (a byte value) as the active quote so only
    # the active quote is backslash-escaped. Mirrors repr_append_byte in
    # py_int_parse.c. ``buf`` must have room for the worst case (4 bytes).
    if c == 92 or c == quote:  # '\\' or the active quote
        store_i8(buf, n, 92)
        store_i8(buf, n + 1, c)
        return n + 2
    if c == 10:  # '\n'
        store_i8(buf, n, 92)
        store_i8(buf, n + 1, 110)  # 'n'
        return n + 2
    if c == 13:  # '\r'
        store_i8(buf, n, 92)
        store_i8(buf, n + 1, 114)  # 'r'
        return n + 2
    if c == 9:  # '\t'
        store_i8(buf, n, 92)
        store_i8(buf, n + 1, 116)  # 't'
        return n + 2
    if c < 32 or c == 127:
        store_i8(buf, n, 92)       # '\\'
        store_i8(buf, n + 1, 120)  # 'x'
        hi: int = (c >> 4) & 15
        lo: int = c & 15
        if hi < 10:
            store_i8(buf, n + 2, 48 + hi)
        else:
            store_i8(buf, n + 2, 87 + hi)  # 'a'(97) - 10
        if lo < 10:
            store_i8(buf, n + 3, 48 + lo)
        else:
            store_i8(buf, n + 3, 87 + lo)
        return n + 4
    store_i8(buf, n, c)
    return n + 1


def _build_bad_literal_message(s: c_ptr, base: int) -> c_ptr:
    return _build_bad_literal_message_length(s, strlen(s), base, 0)


def _build_bad_literal_message_length(s: c_ptr, slen: int, base: int, kind: int) -> c_ptr:
    # Build "invalid literal for int() with base <base>: <repr(s)>" into a
    # freshly malloc'd NUL-terminated buffer; caller frees. ``base`` is the
    # ORIGINAL base argument (0 renders as "base 0"). Mirrors
    # build_bad_literal_message in py_int_parse.c. Returns null() on OOM.
    # CPython quote selection: single quote unless the string has a single
    # quote but no double quote.
    quote: int = 39  # '\''
    has_single: int = 0
    has_double: int = 0
    i: int = 0
    while i < slen:
        b: int = _byte_at(s, i)
        if b == 39:
            has_single = 1
        elif b == 34:
            has_double = 1
        i = i + 1
    if has_single != 0:
        if has_double == 0:
            quote = 34  # '"'

    # Prefix: "invalid literal for int() with base " (36 bytes).
    prefix_len: int = 36
    cap: int = prefix_len + 24 + 16 + slen * 4 + 1
    buf = malloc(cap)
    if ptr_is_null(buf):
        return null()
    prefix = cstr("invalid literal for int() with base ")
    n: int = 0
    while n < prefix_len:
        store_i8(buf, n, _byte_at(prefix, n))
        n = n + 1

    # Render base as signed decimal (handles 0 and defensively negatives).
    neg: int = 0
    ub: int = base
    if base < 0:
        neg = 1
        ub = 0 - base
    # Collect decimal digits (reverse), then append forward.
    digit_buf = malloc(24)
    d: int = 0
    if ub == 0:
        store_i8(digit_buf, 0, 48)  # '0'
        d = 1
    else:
        while ub != 0:
            store_i8(digit_buf, d, 48 + (ub % 10))
            ub = ub // 10
            d = d + 1
    if neg != 0:
        store_i8(buf, n, 45)  # '-'
        n = n + 1
    while d > 0:
        d = d - 1
        store_i8(buf, n, _byte_at(digit_buf, d))
        n = n + 1
    free(digit_buf)

    store_i8(buf, n, 58)      # ':'
    n = n + 1
    store_i8(buf, n, 32)      # ' '
    n = n + 1
    if kind != 0:
        store_i8(buf, n, 98)  # b
        n = n + 1
    store_i8(buf, n, quote)
    n = n + 1
    i = 0
    while i < slen:
        byte: int = _byte_at(s, i)
        if kind != 0 and byte >= 128:
            store_i8(buf, n, 92)
            store_i8(buf, n + 1, 120)
            digits = cstr("0123456789abcdef")
            store_i8(buf, n + 2, _byte_at(digits, byte >> 4))
            store_i8(buf, n + 3, _byte_at(digits, byte & 15))
            n = n + 4
        else:
            n = _repr_append_byte(buf, n, byte, quote)
        i = i + 1
    store_i8(buf, n, quote)
    n = n + 1
    store_i8(buf, n, 0)       # NUL
    return buf


def _ascii_int_space(c: int) -> int:
    return 1 if c == 32 or (c >= 9 and c <= 13) else 0


def _parse_counted_ascii_int(source: c_ptr, length: int, base: int):
    """Validate the complete byte span, then reuse arbitrary-precision arithmetic.

    The temporary is unmanaged storage. No derived pointer into the source
    object survives integer allocation, and embedded NUL is an invalid byte.
    """
    start: int = 0
    end: int = length
    while start < end and _ascii_int_space(_byte_at(source, start)):
        start = start + 1
    while end > start and _ascii_int_space(_byte_at(source, end - 1)):
        end = end - 1
    negative: int = 0
    if start < end:
        sign: int = _byte_at(source, start)
        if sign == 43 or sign == 45:
            negative = 1 if sign == 45 else 0
            start = start + 1
    chosen: int = base
    prefix: int = 0
    if end - start >= 2 and _byte_at(source, start) == 48:
        marker: int = _byte_at(source, start + 1)
        if marker == 120 or marker == 88:
            prefix = 16
        elif marker == 111 or marker == 79:
            prefix = 8
        elif marker == 98 or marker == 66:
            prefix = 2
    zero_only: int = 0
    if prefix != 0 and (chosen == 0 or chosen == prefix):
        chosen = prefix
        start = start + 2
        if start < end and _byte_at(source, start) == 95:
            start = start + 1
    elif chosen == 0:
        chosen = 10
        if start < end and _byte_at(source, start) == 48:
            zero_only = 1
    if chosen < 2 or chosen > 36 or start == end:
        return null()
    normalized = malloc(length + 2)
    if ptr_is_null(normalized):
        py_raise_owned(py_exc_new(7, cstr("integer text conversion allocation failed")))
        return null()
    count: int = 0
    if negative:
        store_i8(normalized, count, 45)
        count = count + 1
    previous_digit: int = 0
    saw_digit: int = 0
    valid: int = 1
    i: int = start
    while i < end and valid:
        ch: int = _byte_at(source, i)
        if ch == 95:
            if not previous_digit:
                valid = 0
            previous_digit = 0
        else:
            digit: int = _digit_value(ch)
            if digit < 0 or digit >= chosen or (zero_only and digit != 0):
                valid = 0
            else:
                store_i8(normalized, count, ch)
                count = count + 1
                previous_digit = 1
                saw_digit = 1
        i = i + 1
    result = null()
    if valid and previous_digit and saw_digit:
        store_i8(normalized, count, 0)
        # The legacy negative i64 scanner stops at an exact INT64_MIN prefix.
        # Additional digits must still produce a larger ordinary Python int.
        if negative:
            result = _parse_bigint(normalized, 1, chosen, 1)
        else:
            result = py_int_from_cstr(normalized, chosen)
    free(normalized)
    return result


def _copy_int_bytes_text(obj, metadata: c_ptr) -> c_ptr:
    """Copy the supported byte buffer layout while its graph is stable.

    The caller owns a counted incoming address lease. A memoryview's backing
    object may still move, so resolve it only after acquiring the graph lock.
    Keep the lock through the raw malloc/copy, then release all managed views.
    """
    store_i64(metadata, 0, -1)
    store_i64(metadata, 8, 1)
    snapshot = null()
    pcc_py_gc_minor_graph_lock()
    current = obj
    tag: int = py_obj_type_tag(current)
    while tag == PY_TYPE_MEMORYVIEW:
        current = pcc_gc_load_ptr(current, ptr_add(current, PYMEMORYVIEWOBJECT_BASE_OFFSET))
        tag = py_obj_type_tag(current)
    if tag == PY_TYPE_BYTES or tag == PY_TYPE_BYTEARRAY:
        length: int = load_i64(current, PYBYTESOBJECT_BYTE_LEN_OFFSET)
        store_i64(metadata, 0, length)
        snapshot = malloc(length + 1)
        if not ptr_is_null(snapshot):
            i: int = 0
            while i < length:
                store_i8(snapshot, i, load_i8(current, PYBYTESOBJECT_DATA_OFFSET + i))
                i = i + 1
            store_i8(snapshot, length, 0)
    pcc_py_gc_minor_graph_unlock()
    return snapshot


def _int_from_bytes_text(obj, base: int):
    if base != 0 and (base < 2 or base > 36):
        py_raise_owned(py_exc_new(2, cstr("int() base must be >= 2 and <= 36, or 0")))
        return null()
    metadata = stack_alloc(16)
    snapshot = _copy_int_bytes_text(obj, metadata)
    length: int = load_i64(metadata, 0)
    if length < 0:
        py_raise_owned(py_exc_new(3, cstr("int() requires a supported contiguous bytes buffer")))
        return null()
    if ptr_is_null(snapshot):
        py_raise_owned(py_exc_new(7, cstr("integer text conversion allocation failed")))
        return null()
    result = _parse_counted_ascii_int(snapshot, length, base)
    if ptr_is_null(result) and py_err_occurred() == 0:
        message = _build_bad_literal_message_length(snapshot, length, base, load_i64(metadata, 8))
        if ptr_is_null(message):
            py_raise_owned(py_exc_new(7, cstr("integer text conversion allocation failed")))
        else:
            py_raise_owned(py_exc_new(2, message))
            free(message)
    free(snapshot)
    return result


def _cstr_length(s: c_ptr) -> int:
    length: int = 0
    while _byte_at(s, length) != 0:
        length = length + 1
    return length


def _needs_counted_parse(s: c_ptr, base: int) -> int:
    """Text the counted parser must judge: PEP 515 underscores, \\v or \\f
    padding, or base 0's "no leading zeros" rule (``int("010", 0)`` is a
    ValueError, not 8). Plain digits keep the allocation-free scan."""
    i: int = 0
    while _is_space(_byte_at(s, i)) != 0:
        i = i + 1
    sign: int = _byte_at(s, i)
    if sign == 43 or sign == 45:
        i = i + 1
    if base == 0 and _byte_at(s, i) == 48:
        marker: int = _byte_at(s, i + 1)
        if (marker != 0 and _is_space(marker) == 0
                and marker != 120 and marker != 88 and marker != 111
                and marker != 79 and marker != 98 and marker != 66):
            return 1
    ch: int = _byte_at(s, i)
    while ch != 0:
        if ch == 95 or ch == 11 or ch == 12:
            return 1
        i = i + 1
        ch = _byte_at(s, i)
    return 0


@c_abi_export("py_int_from_cstr_or_raise")
def py_int_from_cstr_or_raise(s: c_ptr, base: int):
    # int(str) builtin: parse like py_int_from_cstr but raise ValueError on
    # invalid input instead of returning NULL (which the frontend would unbox to
    # 0 -> int('xyz') silently became 0). Mirrors py_int_from_cstr_or_raise in
    # py_int_parse.c; py_int_from_cstr stays NULL-returning for other callers.
    #
    # CPython raises two distinct ValueError messages, reproduced here:
    #   - bad base (not 0 and outside 2..36):
    #       "int() base must be >= 2 and <= 36, or 0"
    #   - unparseable literal (valid base):
    #       "invalid literal for int() with base <base>: <repr(s)>"
    # ``base`` is the ORIGINAL argument (0 stays "base 0"); the repr is a
    # CPython-accurate repr of the whole original string.
    if base != 0:
        if base < 2 or base > 36:
            py_raise_owned(py_exc_new(2, cstr("int() base must be >= 2 and <= 36, or 0")))
            return null()
    v = null()
    if ptr_is_null(s) == 0 and _needs_counted_parse(s, base) != 0:
        v = _parse_counted_ascii_int(s, _cstr_length(s), base)
        if ptr_is_null(v) == 0 or py_err_occurred() != 0:
            return v
    else:
        v = py_int_from_cstr(s, base)
    if ptr_is_null(v) == 0:
        return v
    src = s
    if ptr_is_null(s):
        src = cstr("")
    msg = _build_bad_literal_message(src, base)
    if ptr_is_null(msg) == 0:
        py_raise_owned(py_exc_new(2, msg))  # PY_EXC_VALUEERROR
        free(msg)
    else:
        py_raise_owned(py_exc_new(2, cstr("invalid literal for int()")))
    return null()


@c_abi_export("py_obj_as_int_object")
def py_obj_as_int_object(obj, base: int):
    # Mirror of py_obj_as_int_object in py_int_parse.c.  `int(o[, base])` with an
    # OBJECT result: the frontend's i64-returning `int(<dyn>)` lowering phis four
    # unboxed branches and truncates a bignum to 0, so callers that want the
    # object projection emit ONE call here instead.  Returns a NEW reference.
    if ptr_is_null(obj):
        return null()
    tag: int = py_obj_type_tag(obj)
    if tag == PY_TYPE_STR:
        return py_int_from_cstr_or_raise(py_str_utf8(obj), base)
    if tag == PY_TYPE_BYTES or tag == PY_TYPE_BYTEARRAY or tag == PY_TYPE_MEMORYVIEW:
        return _int_from_bytes_text(obj, base)
    if tag == PY_TYPE_FLOAT:
        # Exact: int(1e20) is 100000000000000000000 (was a saturated i64).
        return py_int_from_f64_exact(py_float_to_f64(obj))
    if tag == PY_TYPE_BOOL:
        return py_int_from_i64(py_obj_truthy(obj))
    if tag == PY_TYPE_INT:
        py_incref(obj)
        return obj
    if tag == PY_TYPE_INSTANCE or tag >= PY_TYPE_USER_CLASS_START:  # __int__, then __index__
        method = py_obj_getattr(obj, cstr("__int__"))
        if ptr_is_null(method):
            if py_err_occurred() != 0:
                py_clear_exception()
            method = py_obj_getattr(obj, cstr("__index__"))
        if not ptr_is_null(method):
            empty = py_tuple_new(0)
            result = py_obj_call(method, empty, global_load_ptr("py_None"))
            py_decref(empty)
            py_decref(method)
            return result
        if py_err_occurred() != 0:
            py_clear_exception()
    # int([1]) is a TypeError; this used to hand the list back as the "int".
    message = stack_alloc(200)
    prefix = cstr("int() argument must be a string, a bytes-like object or a real number, not '")
    length: int = 0
    while load_i8(prefix, length) != 0:
        store_i8(message, length, load_i8(prefix, length))
        length = length + 1
    name = py_obj_type_name(obj)
    if not ptr_is_null(name):
        text = py_str_utf8(name)
        count: int = py_str_byte_len(name)
        if count > 80:
            count = 80
        index: int = 0
        while index < count:
            store_i8(message, length, load_i8(text, index))
            length = length + 1
            index = index + 1
        py_decref(name)
    store_i8(message, length, 39)
    store_i8(message, length + 1, 0)
    py_raise_owned(py_exc_new(3, message))
    return null()


@c_abi_export("py_obj_as_int_object_args")
def py_obj_as_int_object_args(obj, base_obj):
    """Boxed int arguments preserve omitted base and callback-safe receivers."""
    slots = stack_alloc(16)
    obj_handle = _int_open_root(slots, obj)
    base_handle = _int_open_root(ptr_add(slots, 8), base_obj)
    result = null()
    if ptr_is_null(obj_handle) or ptr_is_null(base_handle):
        py_raise_owned(py_exc_new(7, cstr("integer text argument root registration failed")))
    else:
        result = _int_object_rooted_args(slots)
    result_state = stack_alloc(16)
    result_handle = _int_save_result(result_state, result)
    _int_close_root(ptr_add(slots, 8), base_handle)
    _int_close_root(slots, obj_handle)
    return _int_take_result(result_state, result_handle)


def _int_object_rooted_args(slots: c_ptr):
    base: int = 10
    supplied = pcc_gc_load_ptr(null(), ptr_add(slots, 8))
    if not ptr_is_null(supplied):
        indexed = py_obj_index(supplied)
        if ptr_is_null(indexed):
            return null()
        overflow = stack_alloc(4)
        store_i32(overflow, 0, 0)
        base = py_int_to_i64(indexed, overflow)
        bad: int = load_i32(overflow, 0)
        py_decref(indexed)
        if bad or (base != 0 and (base < 2 or base > 36)):
            py_raise_owned(py_exc_new(2, cstr("int() base must be >= 2 and <= 36, or 0")))
            return null()
        obj = pcc_gc_load_ptr(null(), slots)
        tag: int = py_obj_type_tag(obj)
        if tag != PY_TYPE_STR and tag != PY_TYPE_BYTES and tag != PY_TYPE_BYTEARRAY:
            py_raise_owned(py_exc_new(3, cstr("int() can't convert non-string with explicit base")))
            return null()
    return py_obj_as_int_object(pcc_gc_load_ptr(null(), slots), base)


@c_abi_export("py_int_from_f64_exact")
def py_int_from_f64_exact(value: float):
    # int(value) for a double: truncation toward zero, exact, as a NEW int.
    if value != value:
        py_raise_owned(py_exc_new(2, cstr("cannot convert float NaN to integer")))
        return null()
    if value != 0.0 and value == value * 2.0:
        py_raise_owned(py_exc_new(15, cstr("cannot convert float infinity to integer")))
        return null()
    if value > -9.0e18 and value < 9.0e18:
        truncated: int = int(value)
        return py_int_from_i64(truncated)
    # Beyond 2**53 every double is an integer: parse its exact digits.
    negative: int = f64_signbit(value)
    absolute: float = value
    if negative != 0:
        absolute = 0.0 - value
    digits = stack_alloc(800)
    point_box = stack_alloc(8)
    total: int = pcc_stdio_float_exact_digits(f64_bits(absolute), digits, point_box)
    point: int = load_i64(point_box, 0)
    text = stack_alloc(340)
    position: int = 0
    if negative != 0:
        store_i8(text, position, 45)
        position = position + 1
    index: int = 0
    while index < point:
        digit: int = 0
        if index < total:
            digit = load_i8(digits, index)
        store_i8(text, position, 48 + digit)
        position = position + 1
        index = index + 1
    store_i8(text, position, 0)
    return py_int_from_cstr_or_raise(text, 10)


@c_abi_export("py_float_to_i64_checked")
def py_float_to_i64_checked(value: float) -> int:
    # The i64-lane int(float) slow path: the frontend inlines the in-range
    # fptosi and calls this only for NaN, infinities and |value| >= 2**63,
    # which a lane cannot hold.  Raises CPython's error for each.
    if value != value:
        py_raise_owned(py_exc_new(2, cstr("cannot convert float NaN to integer")))
        return 0
    if value != 0.0 and value == value * 2.0:
        py_raise_owned(py_exc_new(15, cstr("cannot convert float infinity to integer")))
        return 0
    if value >= -9223372036854775808.0 and value < 9223372036854775808.0:
        truncated: int = int(value)
        return truncated
    py_raise_owned(py_exc_new(15, cstr("Python int too large to convert to C int64")))
    return 0
