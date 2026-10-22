"""Native JSON semantics authored in pcc-Python.

The public ABI mirrors ``src/py_json.c``.  That C file remains a host-C oracle;
the production pcc-Python archive owns these symbols through this module.
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import PY_TYPE_BOOL, PY_TYPE_DICT, PY_TYPE_FLOAT, PY_TYPE_INT, PY_TYPE_LIST, PY_TYPE_STR

from pcc.extern import c_abi_export, c_double, c_int32, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    cstr,
    free,
    global_load_ptr,
    is_tagged_int,
    load_i8,
    load_i32,
    load_i64,
    load_ptr,
    malloc,
    memcpy,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    realloc,
    stack_alloc,
    store_i8,
    store_i64,
    store_ptr,
)


py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_str_new = extern("py_str_new", (c_ptr, c_int64), c_ptr)
py_str_utf8 = extern("py_str_utf8", (c_ptr,), c_ptr)
py_str_byte_len = extern("py_str_byte_len", (c_ptr,), c_int64)
py_list_new = extern("py_list_new", (c_int64,), c_ptr)
py_list_append = extern("py_list_append", (c_ptr, c_ptr), c_void)
py_list_len = extern("py_list_len", (c_ptr,), c_int64)
py_list_get = extern("py_list_get", (c_ptr, c_int64), c_ptr)
py_dict_new = extern("py_dict_new", (), c_ptr)
py_dict_set = extern("py_dict_set", (c_ptr, c_ptr, c_ptr), c_void)
py_dict_entries_used = extern("py_dict_entries_used", (c_ptr,), c_int64)
py_dict_entry_key_at = extern("py_dict_entry_key_at", (c_ptr, c_int64), c_ptr)
py_dict_entry_value_at = extern("py_dict_entry_value_at", (c_ptr, c_int64), c_ptr)
py_int_from_cstr = extern("py_int_from_cstr", (c_ptr, c_int64), c_ptr)
py_float_from_f64 = extern("py_float_from_f64", (c_double,), c_ptr)
py_float_to_f64 = extern("py_float_to_f64", (c_ptr,), c_double)
py_obj_repr = extern("py_obj_repr", (c_ptr,), c_ptr)
py_decref = extern("py_decref", (c_ptr,), c_void)
pcc_gc_retain = extern("pcc_gc_retain", (c_ptr,), c_ptr)
strtod_c = extern("strtod", (c_ptr, c_ptr), c_double)


def _byte_at(data, i: int) -> int:
    return load_i8(data, i) & 255


def _type_of(obj) -> int:
    if is_tagged_int(obj) != 0:
        return PY_TYPE_INT
    return load_i32(obj, 8)


def _is_ws(c: int) -> int:
    if c == 32 or c == 9 or c == 10 or c == 13:
        return 1
    return 0


def _skip_ws(data, n: int, pos) -> None:
    i: int = load_i64(pos, 0)
    while i < n and _is_ws(_byte_at(data, i)) != 0:
        i = i + 1
    store_i64(pos, 0, i)


def _buf_init(buf) -> None:
    # Raw output buffer: data pointer, used byte count, allocated capacity.
    # Keep layout offsets as literals: pcc-Python library modules have no
    # module initializer that could materialize ordinary global constants.
    store_ptr(buf, 0, null())
    store_i64(buf, 8, 0)
    store_i64(buf, 16, 0)


def _buf_dispose(buf) -> None:
    data = load_ptr(buf, 0)
    if ptr_is_null(data) == 0:
        free(data)
    store_ptr(buf, 0, null())


def _buf_reserve(buf, extra: int) -> int:
    length: int = load_i64(buf, 8)
    want: int = length + extra + 1
    cap: int = load_i64(buf, 16)
    if want <= cap:
        return 0
    if cap <= 0:
        cap = 64
    while cap < want:
        next_cap: int = cap * 2
        if next_cap <= cap:
            return -1
        cap = next_cap
    data = realloc(load_ptr(buf, 0), cap)
    if ptr_is_null(data) != 0:
        return -1
    store_ptr(buf, 0, data)
    store_i64(buf, 16, cap)
    return 0


def _buf_append_bytes(buf, data, n: int) -> int:
    if n < 0:
        return -1
    if _buf_reserve(buf, n) != 0:
        return -1
    out = load_ptr(buf, 0)
    length: int = load_i64(buf, 8)
    if n > 0:
        memcpy(ptr_add(out, length), data, n)
    length = length + n
    store_i64(buf, 8, length)
    store_i8(out, length, 0)
    return 0


def _buf_append_char(buf, c: int) -> int:
    if _buf_reserve(buf, 1) != 0:
        return -1
    out = load_ptr(buf, 0)
    length: int = load_i64(buf, 8)
    store_i8(out, length, c)
    length = length + 1
    store_i64(buf, 8, length)
    store_i8(out, length, 0)
    return 0


def _buf_append_literal(buf, data, n: int) -> int:
    return _buf_append_bytes(buf, data, n)


def _hex_digit(c: int) -> int:
    if c >= 48 and c <= 57:
        return c - 48
    if c >= 97 and c <= 102:
        return c - 97 + 10
    if c >= 65 and c <= 70:
        return c - 65 + 10
    return -1


def _parse_hex4(data, n: int, pos) -> int:
    i: int = load_i64(pos, 0)
    if i + 4 > n:
        return -1
    value: int = 0
    j: int = 0
    while j < 4:
        digit: int = _hex_digit(_byte_at(data, i + j))
        if digit < 0:
            return -1
        value = (value << 4) | digit
        j = j + 1
    store_i64(pos, 0, i + 4)
    return value


def _append_utf8(buf, codepoint: int) -> int:
    if codepoint <= 127:
        return _buf_append_char(buf, codepoint)
    if codepoint <= 2047:
        if _buf_append_char(buf, 192 | (codepoint >> 6)) != 0:
            return -1
        return _buf_append_char(buf, 128 | (codepoint & 63))
    if codepoint <= 65535:
        if codepoint >= 55296 and codepoint <= 57343:
            return -1
        if _buf_append_char(buf, 224 | (codepoint >> 12)) != 0:
            return -1
        if _buf_append_char(buf, 128 | ((codepoint >> 6) & 63)) != 0:
            return -1
        return _buf_append_char(buf, 128 | (codepoint & 63))
    if codepoint <= 1114111:
        if _buf_append_char(buf, 240 | (codepoint >> 18)) != 0:
            return -1
        if _buf_append_char(buf, 128 | ((codepoint >> 12) & 63)) != 0:
            return -1
        if _buf_append_char(buf, 128 | ((codepoint >> 6) & 63)) != 0:
            return -1
        return _buf_append_char(buf, 128 | (codepoint & 63))
    return -1


def _parse_string(data, n: int, pos):
    i: int = load_i64(pos, 0)
    if i >= n or _byte_at(data, i) != 34:
        return null()
    store_i64(pos, 0, i + 1)
    buf = stack_alloc(24)
    _buf_init(buf)
    failed: int = 0
    done: int = 0
    while done == 0 and failed == 0:
        i = load_i64(pos, 0)
        if i >= n:
            failed = 1
        else:
            c: int = _byte_at(data, i)
            if c == 34:
                store_i64(pos, 0, i + 1)
                done = 1
            elif c == 92:
                i = i + 1
                if i >= n:
                    failed = 1
                else:
                    esc: int = _byte_at(data, i)
                    store_i64(pos, 0, i + 1)
                    if esc == 34 or esc == 92 or esc == 47:
                        failed = _buf_append_char(buf, esc) != 0
                    elif esc == 98:
                        failed = _buf_append_char(buf, 8) != 0
                    elif esc == 102:
                        failed = _buf_append_char(buf, 12) != 0
                    elif esc == 110:
                        failed = _buf_append_char(buf, 10) != 0
                    elif esc == 114:
                        failed = _buf_append_char(buf, 13) != 0
                    elif esc == 116:
                        failed = _buf_append_char(buf, 9) != 0
                    elif esc == 117:
                        codepoint: int = _parse_hex4(data, n, pos)
                        if codepoint < 0:
                            failed = 1
                        elif codepoint >= 55296 and codepoint <= 56319:
                            i = load_i64(pos, 0)
                            if i + 6 > n:
                                failed = 1
                            elif _byte_at(data, i) != 92 or _byte_at(data, i + 1) != 117:
                                failed = 1
                            else:
                                store_i64(pos, 0, i + 2)
                                low: int = _parse_hex4(data, n, pos)
                                if low < 56320 or low > 57343:
                                    failed = 1
                                else:
                                    codepoint = 65536 + ((codepoint - 55296) << 10) + (low - 56320)
                        elif codepoint >= 56320 and codepoint <= 57343:
                            failed = 1
                        if failed == 0 and _append_utf8(buf, codepoint) != 0:
                            failed = 1
                    else:
                        failed = 1
            elif c < 32:
                failed = 1
            else:
                if _buf_append_char(buf, c) != 0:
                    failed = 1
                else:
                    store_i64(pos, 0, i + 1)
    if failed != 0:
        _buf_dispose(buf)
        return null()
    out_data = load_ptr(buf, 0)
    if ptr_is_null(out_data) != 0:
        out_data = cstr("")
    result = py_str_new(out_data, load_i64(buf, 8))
    _buf_dispose(buf)
    return result


def _copy_token(data, start: int, end: int):
    length: int = end - start
    token = malloc(length + 1)
    if ptr_is_null(token) != 0:
        return null()
    if length > 0:
        memcpy(token, ptr_add(data, start), length)
    store_i8(token, length, 0)
    return token


def _parse_number(data, n: int, pos):
    start: int = load_i64(pos, 0)
    i: int = start
    if i < n and _byte_at(data, i) == 45:
        i = i + 1
    if i >= n or _byte_at(data, i) < 48 or _byte_at(data, i) > 57:
        return null()
    while i < n and _byte_at(data, i) >= 48 and _byte_at(data, i) <= 57:
        i = i + 1
    is_float: int = 0
    if i < n and _byte_at(data, i) == 46:
        is_float = 1
        i = i + 1
        if i >= n or _byte_at(data, i) < 48 or _byte_at(data, i) > 57:
            return null()
        while i < n and _byte_at(data, i) >= 48 and _byte_at(data, i) <= 57:
            i = i + 1
    if i < n and (_byte_at(data, i) == 101 or _byte_at(data, i) == 69):
        is_float = 1
        i = i + 1
        if i < n and (_byte_at(data, i) == 43 or _byte_at(data, i) == 45):
            i = i + 1
        if i >= n or _byte_at(data, i) < 48 or _byte_at(data, i) > 57:
            return null()
        while i < n and _byte_at(data, i) >= 48 and _byte_at(data, i) <= 57:
            i = i + 1
    token = _copy_token(data, start, i)
    if ptr_is_null(token) != 0:
        return null()
    store_i64(pos, 0, i)
    if is_float != 0:
        value: float = strtod_c(token, null())
        free(token)
        return py_float_from_f64(value)
    result = py_int_from_cstr(token, 10)
    free(token)
    return result


def _literal_matches(data, n: int, pos: int, literal, length: int) -> int:
    if pos + length > n:
        return 0
    i: int = 0
    while i < length:
        if _byte_at(data, pos + i) != _byte_at(literal, i):
            return 0
        i = i + 1
    return 1


def _parse_array(data, n: int, pos):
    i: int = load_i64(pos, 0)
    if i >= n or _byte_at(data, i) != 91:
        return null()
    store_i64(pos, 0, i + 1)
    out = py_list_new(4)
    if ptr_is_null(out) != 0:
        return null()
    done: int = 0
    while done == 0:
        _skip_ws(data, n, pos)
        i = load_i64(pos, 0)
        if i < n and _byte_at(data, i) == 93:
            store_i64(pos, 0, i + 1)
            done = 1
        else:
            value = _parse_value(data, n, pos)
            if ptr_is_null(value) != 0:
                py_decref(out)
                return null()
            py_list_append(out, value)
            py_decref(value)
            _skip_ws(data, n, pos)
            i = load_i64(pos, 0)
            if i < n and _byte_at(data, i) == 44:
                store_i64(pos, 0, i + 1)
            elif i < n and _byte_at(data, i) == 93:
                store_i64(pos, 0, i + 1)
                done = 1
            else:
                py_decref(out)
                return null()
    return out


def _parse_object(data, n: int, pos):
    i: int = load_i64(pos, 0)
    if i >= n or _byte_at(data, i) != 123:
        return null()
    store_i64(pos, 0, i + 1)
    out = py_dict_new()
    if ptr_is_null(out) != 0:
        return null()
    done: int = 0
    while done == 0:
        _skip_ws(data, n, pos)
        i = load_i64(pos, 0)
        if i < n and _byte_at(data, i) == 125:
            store_i64(pos, 0, i + 1)
            done = 1
        else:
            key = _parse_string(data, n, pos)
            if ptr_is_null(key) != 0:
                py_decref(out)
                return null()
            _skip_ws(data, n, pos)
            i = load_i64(pos, 0)
            if i >= n or _byte_at(data, i) != 58:
                py_decref(key)
                py_decref(out)
                return null()
            store_i64(pos, 0, i + 1)
            _skip_ws(data, n, pos)
            value = _parse_value(data, n, pos)
            if ptr_is_null(value) != 0:
                py_decref(key)
                py_decref(out)
                return null()
            py_dict_set(out, key, value)
            py_decref(key)
            py_decref(value)
            _skip_ws(data, n, pos)
            i = load_i64(pos, 0)
            if i < n and _byte_at(data, i) == 44:
                store_i64(pos, 0, i + 1)
            elif i < n and _byte_at(data, i) == 125:
                store_i64(pos, 0, i + 1)
                done = 1
            else:
                py_decref(out)
                return null()
    return out


def _parse_value(data, n: int, pos):
    _skip_ws(data, n, pos)
    i: int = load_i64(pos, 0)
    if i >= n:
        return null()
    c: int = _byte_at(data, i)
    if c == 34:
        return _parse_string(data, n, pos)
    if c == 123:
        return _parse_object(data, n, pos)
    if c == 91:
        return _parse_array(data, n, pos)
    if c == 116 and _literal_matches(data, n, i, cstr("true"), 4) != 0:
        store_i64(pos, 0, i + 4)
        return pcc_gc_retain(global_load_ptr("py_True"))
    if c == 102 and _literal_matches(data, n, i, cstr("false"), 5) != 0:
        store_i64(pos, 0, i + 5)
        return pcc_gc_retain(global_load_ptr("py_False"))
    if c == 110 and _literal_matches(data, n, i, cstr("null"), 4) != 0:
        store_i64(pos, 0, i + 4)
        return pcc_gc_retain(global_load_ptr("py_None"))
    if c == 78 and _literal_matches(data, n, i, cstr("NaN"), 3) != 0:
        store_i64(pos, 0, i + 3)
        return py_float_from_f64(strtod_c(cstr("nan"), null()))
    if c == 73 and _literal_matches(data, n, i, cstr("Infinity"), 8) != 0:
        store_i64(pos, 0, i + 8)
        return py_float_from_f64(strtod_c(cstr("inf"), null()))
    if c == 45 and _literal_matches(data, n, i, cstr("-Infinity"), 9) != 0:
        store_i64(pos, 0, i + 9)
        inf: float = strtod_c(cstr("inf"), null())
        return py_float_from_f64(0.0 - inf)
    return _parse_number(data, n, pos)


@c_abi_export("py_json_loads")
def py_json_loads(text):
    # Every failure below has to leave a pending exception.  Returning NULL
    # without one made a malformed document vanish: `json.loads('{"k":')`
    # answered `<null>` in compiled code where CPython raises, and a caller
    # that checked for an exception instead saw "compiled native function
    # returned NULL without exc".  `JSONDecodeError` subclasses `ValueError`,
    # so `except ValueError` catches this; a handler naming
    # `json.JSONDecodeError` specifically does not, because this helper cannot
    # reach the provider's class.  Recorded, not silently accepted.
    if ptr_is_null(text) != 0:
        py_raise_owned(
            py_exc_new(2, cstr("json.loads requires a string document"))
        )
        return null()
    if _type_of(text) != PY_TYPE_STR:
        py_raise_owned(
            py_exc_new(2, cstr("json.loads requires a string document"))
        )
        return null()
    data = py_str_utf8(text)
    n: int = py_str_byte_len(text)
    pos = stack_alloc(8)
    store_i64(pos, 0, 0)
    result = _parse_value(data, n, pos)
    if ptr_is_null(result) != 0:
        py_raise_owned(
            py_exc_new(2, cstr("json.loads could not decode the document"))
        )
        return null()
    _skip_ws(data, n, pos)
    # Preserve the established native helper contract: trailing non-whitespace
    # is tolerated after the first complete JSON value.
    return result


def _append_u00_escape(buf, c: int) -> int:
    digits = cstr("0123456789abcdef")
    if _buf_append_literal(buf, cstr("\\u00"), 4) != 0:
        return -1
    if _buf_append_char(buf, _byte_at(digits, (c >> 4) & 15)) != 0:
        return -1
    return _buf_append_char(buf, _byte_at(digits, c & 15))


def _append_quoted_str(buf, obj) -> int:
    if ptr_is_null(obj) != 0 or _type_of(obj) != PY_TYPE_STR:
        return -1
    if _buf_append_char(buf, 34) != 0:
        return -1
    data = py_str_utf8(obj)
    n: int = py_str_byte_len(obj)
    i: int = 0
    while i < n:
        c: int = _byte_at(data, i)
        if c == 34 or c == 92:
            if _buf_append_char(buf, 92) != 0 or _buf_append_char(buf, c) != 0:
                return -1
        elif c == 10:
            if _buf_append_literal(buf, cstr("\\n"), 2) != 0:
                return -1
        elif c == 13:
            if _buf_append_literal(buf, cstr("\\r"), 2) != 0:
                return -1
        elif c == 9:
            if _buf_append_literal(buf, cstr("\\t"), 2) != 0:
                return -1
        elif c == 8:
            if _buf_append_literal(buf, cstr("\\b"), 2) != 0:
                return -1
        elif c == 12:
            if _buf_append_literal(buf, cstr("\\f"), 2) != 0:
                return -1
        elif c < 32:
            if _append_u00_escape(buf, c) != 0:
                return -1
        else:
            if _buf_append_char(buf, c) != 0:
                return -1
        i = i + 1
    return _buf_append_char(buf, 34)


def _append_repr(buf, obj) -> int:
    text = py_obj_repr(obj)
    if ptr_is_null(text) != 0:
        return -1
    result: int = _buf_append_bytes(buf, py_str_utf8(text), py_str_byte_len(text))
    py_decref(text)
    return result


def _key_cmp(a, b) -> int:
    ad = cstr("")
    bd = cstr("")
    alen: int = 0
    blen: int = 0
    if ptr_is_null(a) == 0 and _type_of(a) == PY_TYPE_STR:
        ad = py_str_utf8(a)
        alen = py_str_byte_len(a)
    if ptr_is_null(b) == 0 and _type_of(b) == PY_TYPE_STR:
        bd = py_str_utf8(b)
        blen = py_str_byte_len(b)
    n: int = alen
    if blen < n:
        n = blen
    i: int = 0
    while i < n:
        ca: int = _byte_at(ad, i)
        cb: int = _byte_at(bd, i)
        if ca < cb:
            return -1
        if ca > cb:
            return 1
        i = i + 1
    if alen < blen:
        return -1
    if alen > blen:
        return 1
    return 0


def _sort_live_dict_indices(obj, order, entries_used: int) -> int:
    count: int = 0
    i: int = 0
    while i < entries_used:
        key = py_dict_entry_key_at(obj, i)
        if ptr_is_null(key) == 0:
            store_i64(order, count * 8, i)
            count = count + 1
            py_decref(key)
        i = i + 1
    i = 1
    while i < count:
        current: int = load_i64(order, i * 8)
        current_key = py_dict_entry_key_at(obj, current)
        j: int = i - 1
        moving: int = 1
        while j >= 0 and moving != 0:
            prior: int = load_i64(order, j * 8)
            prior_key = py_dict_entry_key_at(obj, prior)
            cmp: int = _key_cmp(prior_key, current_key)
            py_decref(prior_key)
            if cmp > 0:
                store_i64(order, (j + 1) * 8, prior)
                j = j - 1
            else:
                moving = 0
        store_i64(order, (j + 1) * 8, current)
        py_decref(current_key)
        i = i + 1
    return count


def _dump_dict_entry(buf, obj, entry_index: int, sort_keys: int) -> int:
    key = py_dict_entry_key_at(obj, entry_index)
    if ptr_is_null(key) != 0:
        return 1
    value = py_dict_entry_value_at(obj, entry_index)
    result: int = _append_quoted_str(buf, key)
    if result == 0:
        result = _buf_append_literal(buf, cstr(": "), 2)
    if result == 0:
        result = _dump_value(buf, value, sort_keys)
    py_decref(key)
    if ptr_is_null(value) == 0:
        py_decref(value)
    return result


def _dump_value(buf, obj, sort_keys: int) -> int:
    if ptr_is_null(obj) != 0 or ptr_eq(obj, global_load_ptr("py_None")) != 0:
        return _buf_append_literal(buf, cstr("null"), 4)
    tag: int = _type_of(obj)
    if tag == PY_TYPE_INT:
        return _append_repr(buf, obj)
    if tag == PY_TYPE_FLOAT:
        value: float = py_float_to_f64(obj)
        if value != value:
            return _buf_append_literal(buf, cstr("NaN"), 3)
        inf: float = strtod_c(cstr("inf"), null())
        if value == inf:
            return _buf_append_literal(buf, cstr("Infinity"), 8)
        if value == 0.0 - inf:
            return _buf_append_literal(buf, cstr("-Infinity"), 9)
        return _append_repr(buf, obj)
    if tag == PY_TYPE_BOOL:
        if ptr_eq(obj, global_load_ptr("py_True")) != 0:
            return _buf_append_literal(buf, cstr("true"), 4)
        return _buf_append_literal(buf, cstr("false"), 5)
    if tag == PY_TYPE_STR:
        return _append_quoted_str(buf, obj)
    if tag == PY_TYPE_LIST:
        if _buf_append_char(buf, 91) != 0:
            return -1
        length: int = py_list_len(obj)
        i: int = 0
        while i < length:
            if i > 0 and _buf_append_literal(buf, cstr(", "), 2) != 0:
                return -1
            item = py_list_get(obj, i)
            result: int = _dump_value(buf, item, sort_keys)
            if ptr_is_null(item) == 0:
                py_decref(item)
            if result != 0:
                return -1
            i = i + 1
        return _buf_append_char(buf, 93)
    if tag == PY_TYPE_DICT:
        if _buf_append_char(buf, 123) != 0:
            return -1
        entries_used: int = py_dict_entries_used(obj)
        first: int = 1
        if sort_keys != 0 and entries_used > 0:
            order = malloc(entries_used * 8)
            if ptr_is_null(order) != 0:
                return -1
            count: int = _sort_live_dict_indices(obj, order, entries_used)
            i: int = 0
            while i < count:
                if i > 0 and _buf_append_literal(buf, cstr(", "), 2) != 0:
                    free(order)
                    return -1
                if _dump_dict_entry(buf, obj, load_i64(order, i * 8), sort_keys) != 0:
                    free(order)
                    return -1
                i = i + 1
            free(order)
        else:
            i: int = 0
            while i < entries_used:
                key = py_dict_entry_key_at(obj, i)
                if ptr_is_null(key) == 0:
                    py_decref(key)
                    if first == 0 and _buf_append_literal(buf, cstr(", "), 2) != 0:
                        return -1
                    first = 0
                    if _dump_dict_entry(buf, obj, i, sort_keys) != 0:
                        return -1
                i = i + 1
        return _buf_append_char(buf, 125)
    return _buf_append_literal(buf, cstr("null"), 4)


@c_abi_export("py_json_dumps_ex")
def py_json_dumps_ex(obj, sort_keys: int):
    buf = stack_alloc(24)
    _buf_init(buf)
    if _dump_value(buf, obj, sort_keys) != 0:
        _buf_dispose(buf)
        return null()
    data = load_ptr(buf, 0)
    if ptr_is_null(data) != 0:
        data = cstr("")
    result = py_str_new(data, load_i64(buf, 8))
    _buf_dispose(buf)
    return result


@c_abi_export("py_json_dumps")
def py_json_dumps(obj):
    return py_json_dumps_ex(obj, 0)
