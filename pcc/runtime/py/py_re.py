"""pcc-Python port of py_re.c.

This intentionally remains the same bootstrap regex subset as the C helper:
``re.match`` / ``re.search`` truthiness for literals, '.', anchors, '*', '+',
'?', the ASCII classes \d, \w, \s plus uppercase negations, and the re.I /
re.S flags.
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import PY_TYPE_INT, PY_TYPE_STR

from pcc.extern import extern, c_abi_export, c_int64, c_ptr, c_void
from pcc.unsafe import (
    cstr,
    global_load_ptr,
    is_tagged_int,
    load_i8,
    load_i32,
    null,
    ptr_is_null,
)

py_str_utf8 = extern("py_str_utf8", (c_ptr,), c_ptr)
py_str_byte_slice_i64 = extern(
    "py_str_byte_slice_i64", (c_ptr, c_int64, c_int64), c_ptr
)
py_list_new = extern("py_list_new", (c_int64,), c_ptr)
py_list_append = extern("py_list_append", (c_ptr, c_ptr), c_void)
py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_tuple_get = extern("py_tuple_get", (c_ptr, c_int64), c_ptr)
py_tuple_len = extern("py_tuple_len", (c_ptr,), c_int64)
py_tuple_set_item = extern("py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void)
py_int_from_i64 = extern("py_int_from_i64", (c_int64,), c_ptr)
py_int_to_i64 = extern("py_int_to_i64", (c_ptr, c_ptr), c_int64)
py_func_new_named = extern("py_func_new_named", (c_ptr, c_ptr, c_ptr), c_ptr)
# E1a/E4 faithful-engine bridge (owned by py_re_engine_runtime.py).
py_re_engine_truth_flags = extern(
    "py_re_engine_truth_flags", (c_ptr, c_ptr, c_int64, c_int64), c_ptr
)
py_re_engine_truth_flags_from = extern(
    "py_re_engine_truth_flags_from",
    (c_ptr, c_ptr, c_int64, c_int64, c_int64, c_int64),
    c_ptr,
)
py_re_engine_fullmatch_flags = extern(
    "py_re_engine_fullmatch_flags", (c_ptr, c_ptr, c_int64), c_ptr
)
py_re_engine_findall = extern("py_re_engine_findall", (c_ptr, c_ptr, c_int64), c_ptr)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)


def _type_of(obj) -> int:
    if is_tagged_int(obj):
        return PY_TYPE_INT
    return load_i32(obj, 8)


def _byte(p, idx: int) -> int:
    return load_i8(p, idx) & 0xFF


def _is_digit(c: int) -> int:
    if c >= 48 and c <= 57:
        return 1
    return 0


def _is_alpha(c: int) -> int:
    if c >= 65 and c <= 90:
        return 1
    if c >= 97 and c <= 122:
        return 1
    return 0


def _is_word(c: int) -> int:
    if _is_alpha(c) != 0:
        return 1
    if _is_digit(c) != 0:
        return 1
    if c == 95:
        return 1
    return 0


def _is_space(c: int) -> int:
    if c == 32:
        return 1
    if c == 9:
        return 1
    if c == 10:
        return 1
    if c == 13:
        return 1
    if c == 11:
        return 1
    if c == 12:
        return 1
    return 0


def _lower_ascii(c: int) -> int:
    if c >= 65 and c <= 90:
        return c + 32
    return c


def _literal_eq(a: int, b: int, ignore_case: int) -> int:
    if ignore_case == 0:
        if a == b:
            return 1
        return 0
    if _lower_ascii(a) == _lower_ascii(b):
        return 1
    return 0


def _atom_len(p, idx: int) -> int:
    if _byte(p, idx) == 92 and _byte(p, idx + 1) != 0:
        return 2
    return 1


def _atom_literal(p, idx: int) -> int:
    if _byte(p, idx) == 92 and _byte(p, idx + 1) != 0:
        return _byte(p, idx + 1)
    return _byte(p, idx)


def _atom_kind(p, idx: int) -> int:
    c: int = _byte(p, idx)
    if c == 46:
        return 1
    if c == 92 and _byte(p, idx + 1) != 0:
        e: int = _byte(p, idx + 1)
        if e == 100:
            return 2
        if e == 68:
            return 3
        if e == 119:
            return 4
        if e == 87:
            return 5
        if e == 115:
            return 6
        if e == 83:
            return 7
    return 0


def _atom_matches(
    p,
    atom_idx: int,
    t,
    text_idx: int,
    ignore_case: int,
    dot_all: int,
) -> int:
    c: int = _byte(t, text_idx)
    if c == 0:
        return 0
    kind: int = _atom_kind(p, atom_idx)
    if kind == 1:
        if dot_all != 0:
            return 1
        if c != 10:
            return 1
        return 0
    if kind == 2:
        return _is_digit(c)
    if kind == 3:
        if _is_digit(c) == 0:
            return 1
        return 0
    if kind == 4:
        return _is_word(c)
    if kind == 5:
        if _is_word(c) == 0:
            return 1
        return 0
    if kind == 6:
        return _is_space(c)
    if kind == 7:
        if _is_space(c) == 0:
            return 1
        return 0
    return _literal_eq(c, _atom_literal(p, atom_idx), ignore_case)


def _match_star(
    p,
    atom_idx: int,
    rest_idx: int,
    t,
    text_idx: int,
    ignore_case: int,
    dot_all: int,
) -> int:
    end: int = text_idx
    while _atom_matches(p, atom_idx, t, end, ignore_case, dot_all) != 0:
        end = end + 1
    while True:
        if _match_here(p, rest_idx, t, end, ignore_case, dot_all) != 0:
            return 1
        if end == text_idx:
            break
        end = end - 1
    return 0


def _match_plus(
    p,
    atom_idx: int,
    rest_idx: int,
    t,
    text_idx: int,
    ignore_case: int,
    dot_all: int,
) -> int:
    if _atom_matches(p, atom_idx, t, text_idx, ignore_case, dot_all) == 0:
        return 0
    return _match_star(
        p,
        atom_idx,
        rest_idx,
        t,
        text_idx + 1,
        ignore_case,
        dot_all,
    )


def _match_here(
    p,
    pattern_idx: int,
    t,
    text_idx: int,
    ignore_case: int,
    dot_all: int,
) -> int:
    if _byte(p, pattern_idx) == 0:
        return 1
    if _byte(p, pattern_idx) == 36 and _byte(p, pattern_idx + 1) == 0:
        if _byte(t, text_idx) == 0:
            return 1
        return 0

    atom_idx: int = pattern_idx
    rest_idx: int = pattern_idx + _atom_len(p, pattern_idx)
    q: int = _byte(p, rest_idx)
    if q == 42:
        return _match_star(
            p,
            atom_idx,
            rest_idx + 1,
            t,
            text_idx,
            ignore_case,
            dot_all,
        )
    if q == 43:
        return _match_plus(
            p,
            atom_idx,
            rest_idx + 1,
            t,
            text_idx,
            ignore_case,
            dot_all,
        )
    if q == 63:
        if _atom_matches(p, atom_idx, t, text_idx, ignore_case, dot_all) != 0:
            if (
                _match_here(
                    p,
                    rest_idx + 1,
                    t,
                    text_idx + 1,
                    ignore_case,
                    dot_all,
                )
                != 0
            ):
                return 1
        return _match_here(p, rest_idx + 1, t, text_idx, ignore_case, dot_all)
    if _atom_matches(p, atom_idx, t, text_idx, ignore_case, dot_all) != 0:
        return _match_here(p, rest_idx, t, text_idx + 1, ignore_case, dot_all)
    return 0


def _re_match_impl(pattern, text, flags: int, search: int):
    none = global_load_ptr("py_None")
    if ptr_is_null(pattern) or ptr_is_null(text):
        return none
    if _type_of(pattern) != PY_TYPE_STR or _type_of(text) != PY_TYPE_STR:
        return none
    if (flags & ~26) == 0:  # 26 == re.I|re.M|re.S — the engine flag mask
        # Subset patterns (incl. re.I/M/S since E4) run on the faithful
        # engine; outside-subset patterns raise NotImplementedError (NULL)
        # instead of silently mismatching like the legacy matcher would.
        return py_re_engine_truth_flags(pattern, text, flags, search)
    py_raise_owned(
        py_exc_new(
            11, cstr("pcc re: flags outside the native regex subset (no-libpython)")
        )
    )
    return null()
    p = py_str_utf8(pattern)
    t = py_str_utf8(text)
    if ptr_is_null(p) or ptr_is_null(t):
        return none
    ignore_case: int = 0
    dot_all: int = 0
    if (flags & 2) != 0:
        ignore_case = 1
    if (flags & 16) != 0:
        dot_all = 1
    start: int = 0
    if _byte(p, 0) == 94:
        start = 1
        if _match_here(p, start, t, 0, ignore_case, dot_all) != 0:
            return global_load_ptr("py_True")
        return none
    if search == 0:
        if _match_here(p, start, t, 0, ignore_case, dot_all) != 0:
            return global_load_ptr("py_True")
        return none
    text_idx: int = 0
    while True:
        if _match_here(p, start, t, text_idx, ignore_case, dot_all) != 0:
            return global_load_ptr("py_True")
        if _byte(t, text_idx) == 0:
            break
        text_idx = text_idx + 1
    return none


@c_abi_export("py_re_match")
def py_re_match(pattern, text):
    return _re_match_impl(pattern, text, 0, 0)


@c_abi_export("py_re_match_flags")
def py_re_match_flags(pattern, text, flags: int):
    return _re_match_impl(pattern, text, flags, 0)


@c_abi_export("py_re_fullmatch")
def py_re_fullmatch(pattern, text):
    return py_re_fullmatch_flags(pattern, text, 0)


@c_abi_export("py_re_fullmatch_flags")
def py_re_fullmatch_flags(pattern, text, flags: int):
    none = global_load_ptr("py_None")
    if ptr_is_null(pattern) or ptr_is_null(text):
        return none
    if _type_of(pattern) != PY_TYPE_STR or _type_of(text) != PY_TYPE_STR:
        return none
    if (flags & ~26) == 0:  # 26 == re.I|re.M|re.S — the engine flag mask
        return py_re_engine_fullmatch_flags(pattern, text, flags)
    py_raise_owned(
        py_exc_new(
            11, cstr("pcc re: flags outside the native regex subset (no-libpython)")
        )
    )
    return null()


@c_abi_export("py_re_search")
def py_re_search(pattern, text):
    return _re_match_impl(pattern, text, 0, 1)


@c_abi_export("py_re_search_flags")
def py_re_search_flags(pattern, text, flags: int):
    return _re_match_impl(pattern, text, flags, 1)


def _pattern_is_ident_words(p) -> int:
    if _byte(p, 0) != 92:
        return 0
    if _byte(p, 1) != 98:
        return 0
    if _byte(p, 2) != 91:
        return 0
    if _byte(p, 3) != 97:
        return 0
    if _byte(p, 4) != 45:
        return 0
    if _byte(p, 5) != 122:
        return 0
    if _byte(p, 6) != 93:
        return 0
    if _byte(p, 7) != 91:
        return 0
    if _byte(p, 8) != 92:
        return 0
    if _byte(p, 9) != 119:
        return 0
    if _byte(p, 10) != 36:
        return 0
    if _byte(p, 11) != 93:
        return 0
    if _byte(p, 12) != 42:
        return 0
    if _byte(p, 13) != 92:
        return 0
    if _byte(p, 14) != 98:
        return 0
    if _byte(p, 15) != 0:
        return 0
    return 1


def _pattern_is_parenthesized(p) -> int:
    if _byte(p, 0) != 92:
        return 0
    if _byte(p, 1) != 40:
        return 0
    if _byte(p, 2) != 46:
        return 0
    if _byte(p, 3) != 42:
        return 0
    if _byte(p, 4) != 63:
        return 0
    if _byte(p, 5) != 92:
        return 0
    if _byte(p, 6) != 41:
        return 0
    if _byte(p, 7) != 0:
        return 0
    return 1


def _word_body(c: int) -> int:
    if _is_word(c) != 0:
        return 1
    if c == 36:
        return 1
    return 0


def _append_slice(out, text, lo: int, hi: int) -> None:
    part = py_str_byte_slice_i64(text, lo, hi)
    if ptr_is_null(part) != 0:
        return
    py_list_append(out, part)
    py_decref(part)


def _findall_ident_words(text):
    t = py_str_utf8(text)
    out = py_list_new(0)
    if ptr_is_null(t) != 0:
        return out
    i: int = 0
    while _byte(t, i) != 0:
        c: int = _byte(t, i)
        prev_word: int = 0
        if i > 0 and _is_word(_byte(t, i - 1)) != 0:
            prev_word = 1
        if prev_word == 0 and _is_alpha(c) != 0:
            start: int = i
            i = i + 1
            while _byte(t, i) != 0 and _word_body(_byte(t, i)) != 0:
                i = i + 1
            end: int = i
            while end > start and _byte(t, end - 1) == 36:
                end = end - 1
            _append_slice(out, text, start, end)
        else:
            i = i + 1
    return out


def _findall_parenthesized(text):
    t = py_str_utf8(text)
    out = py_list_new(0)
    if ptr_is_null(t) != 0:
        return out
    i: int = 0
    while _byte(t, i) != 0:
        if _byte(t, i) == 40:
            start: int = i
            i = i + 1
            while _byte(t, i) != 0 and _byte(t, i) != 41:
                i = i + 1
            if _byte(t, i) == 41:
                _append_slice(out, text, start, i + 1)
                i = i + 1
            else:
                break
        else:
            i = i + 1
    return out


@c_abi_export("py_re_findall_flags")
def py_re_findall_flags(pattern, text, flags: int):
    if ptr_is_null(pattern) != 0 or ptr_is_null(text) != 0:
        return py_list_new(0)
    if _type_of(pattern) != PY_TYPE_STR or _type_of(text) != PY_TYPE_STR:
        return py_list_new(0)
    if (flags & ~26) == 0:
        # E3/E4 faithful-engine findall (owned by py_re_engine_runtime.py).
        return py_re_engine_findall(pattern, text, flags)
    py_raise_owned(
        py_exc_new(
            11, cstr("pcc re: flags outside the native regex subset (no-libpython)")
        )
    )
    return null()
    p = py_str_utf8(pattern)
    if _pattern_is_ident_words(p) != 0:
        return _findall_ident_words(text)
    if _pattern_is_parenthesized(p) != 0:
        return _findall_parenthesized(text)
    return py_list_new(0)


def py_re_bound_method_call(captures, args):
    none = global_load_ptr("py_None")
    if ptr_is_null(captures) != 0 or ptr_is_null(args) != 0:
        return none
    if py_tuple_len(captures) < 3 or py_tuple_len(args) < 1:
        return none
    pattern = py_tuple_get(captures, 0)
    flags_obj = py_tuple_get(captures, 1)
    method_obj = py_tuple_get(captures, 2)
    text = py_tuple_get(args, 0)
    if (
        ptr_is_null(pattern) != 0
        or ptr_is_null(flags_obj) != 0
        or ptr_is_null(method_obj) != 0
        or ptr_is_null(text) != 0
    ):
        if ptr_is_null(pattern) == 0:
            py_decref(pattern)
        if ptr_is_null(flags_obj) == 0:
            py_decref(flags_obj)
        if ptr_is_null(method_obj) == 0:
            py_decref(method_obj)
        if ptr_is_null(text) == 0:
            py_decref(text)
        return none
    flags: int = py_int_to_i64(flags_obj, null())
    method_kind: int = py_int_to_i64(method_obj, null())
    start: int = 0
    endpos: int = -1
    start_obj = null()
    endpos_obj = null()
    if py_tuple_len(args) >= 2:
        start_obj = py_tuple_get(args, 1)
        if ptr_is_null(start_obj) == 0:
            start = py_int_to_i64(start_obj, null())
    if py_tuple_len(args) >= 3:
        endpos_obj = py_tuple_get(args, 2)
        if ptr_is_null(endpos_obj) == 0:
            endpos = py_int_to_i64(endpos_obj, null())
    if method_kind == 2:
        result = py_re_findall_flags(pattern, text, flags)
    else:
        result = py_re_engine_truth_flags_from(
            pattern, text, flags, method_kind, start, endpos
        )
    py_decref(pattern)
    py_decref(flags_obj)
    py_decref(method_obj)
    py_decref(text)
    if ptr_is_null(start_obj) == 0:
        py_decref(start_obj)
    if ptr_is_null(endpos_obj) == 0:
        py_decref(endpos_obj)
    return result


@c_abi_export("py_re_compile_method")
def py_re_compile_method(pattern, flags: int, method_kind: int):
    captures = py_tuple_new(3)
    if ptr_is_null(captures) != 0:
        return null()
    flags_obj = py_int_from_i64(flags)
    method_obj = py_int_from_i64(method_kind)
    if ptr_is_null(flags_obj) != 0 or ptr_is_null(method_obj) != 0:
        if ptr_is_null(flags_obj) == 0:
            py_decref(flags_obj)
        if ptr_is_null(method_obj) == 0:
            py_decref(method_obj)
        py_decref(captures)
        return null()
    py_tuple_set_item(captures, 0, pattern)
    py_tuple_set_item(captures, 1, flags_obj)
    py_tuple_set_item(captures, 2, method_obj)
    py_decref(flags_obj)
    py_decref(method_obj)
    name = cstr("re.Pattern.match")
    if method_kind == 1:
        name = cstr("re.Pattern.search")
    if method_kind == 2:
        name = cstr("re.Pattern.findall")
    fn = py_func_new_named(py_re_bound_method_call, captures, name)
    py_decref(captures)
    return fn
