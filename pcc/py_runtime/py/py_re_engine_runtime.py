"""pcc-Python owner of the regex engine's managed-object bridge.

The byte-regex core remains a separate oracle-sized low-level slice for now.
This module owns all construction of Match/Pattern objects and the public
findall/sub/split/truth adapters, so the production archive no longer needs
``py_re_engine_obj.o``.
"""

__pcc_runtime_port__ = True

from pcc.py_runtime.py.py_abi_constants import (
    PY_TYPE_INT,
    PY_TYPE_STR,
)

from pcc.extern import c_abi_export, c_int32, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    cstr,
    define_global_ptr_null,
    global_load_ptr,
    global_store_ptr,
    is_tagged_int,
    load_i8,
    load_i32,
    load_i64,
    load_ptr,
    memset,
    null,
    ptr_add,
    ptr_is_null,
    stack_alloc,
    store_i32,
    store_i64,
    store_ptr,
)


py_str_utf8 = extern("py_str_utf8", (c_ptr,), c_ptr)
py_str_byte_slice_i64 = extern(
    "py_str_byte_slice_i64", (c_ptr, c_int64, c_int64), c_ptr
)
py_str_new = extern("py_str_new", (c_ptr, c_int64), c_ptr)
py_str_join = extern("py_str_join", (c_ptr, c_ptr), c_ptr)
py_list_new = extern("py_list_new", (c_int64,), c_ptr)
py_list_append = extern("py_list_append", (c_ptr, c_ptr), c_void)
py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_tuple_get = extern("py_tuple_get", (c_ptr, c_int64), c_ptr)
py_tuple_len = extern("py_tuple_len", (c_ptr,), c_int64)
py_tuple_set_item = extern("py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void)
py_dict_new = extern("py_dict_new", (), c_ptr)
py_dict_set = extern("py_dict_set", (c_ptr, c_ptr, c_ptr), c_void)
py_int_from_i64 = extern("py_int_from_i64", (c_int64,), c_ptr)
py_int_to_i64 = extern("py_int_to_i64", (c_ptr, c_ptr), c_int64)
py_func_new_named = extern("py_func_new_named", (c_ptr, c_ptr, c_ptr), c_ptr)
py_class_new = extern(
    "py_class_new", (c_ptr, c_ptr, c_int32, c_ptr, c_int32), c_ptr
)
py_instance_new = extern("py_instance_new", (c_ptr,), c_ptr)
py_obj_setattr = extern("py_obj_setattr", (c_ptr, c_ptr, c_ptr), c_int64)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_obj_call = extern("py_obj_call", (c_ptr, c_ptr, c_ptr), c_ptr)
py_builtin_callable = extern("py_builtin_callable", (c_ptr,), c_ptr)
py_obj_truthy = extern("py_obj_truthy", (c_ptr,), c_int64)
py_iter_callable_new = extern("py_iter_callable_new", (c_ptr, c_ptr), c_ptr)
py_index_i64_checked = extern("py_index_i64_checked", (c_ptr,), c_int64)
py_err_occurred = extern("py_err_occurred", (), c_int64)
pcc_gc_backend = extern("pcc_gc_backend", (), c_int64)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_gc_scheduler_root_register_handle = extern("pcc_gc_scheduler_root_register_handle", (c_ptr,), c_ptr)
pcc_gc_scheduler_root_unregister_handle = extern("pcc_gc_scheduler_root_unregister_handle", (c_ptr,), c_void)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)

pcc_re_engine_supported = extern("pcc_re_engine_supported", (c_ptr,), c_int32)
pcc_re_engine_supported_flags = extern(
    "pcc_re_engine_supported_flags", (c_ptr, c_int64), c_int32
)
pcc_re_engine_run_flags = extern(
    "pcc_re_engine_run_flags",
    (c_ptr, c_int64, c_ptr, c_int64, c_int64, c_int32, c_ptr, c_int32, c_ptr),
    c_int32,
)
pcc_re_engine_group_names_flags = extern(
    "pcc_re_engine_group_names_flags", (c_ptr, c_int64, c_ptr, c_int32), c_int32
)


define_global_ptr_null("pcc_re_match_class")
define_global_ptr_null("pcc_re_pattern_class")


def _none():
    return global_load_ptr("py_None")


def _type_of(value) -> int:
    if is_tagged_int(value) != 0:
        return PY_TYPE_INT
    return load_i32(value, 8)


def _cstrlen(text) -> int:
    length: int = 0
    if ptr_is_null(text) != 0:
        return 0
    while load_i8(text, length) != 0:
        length = length + 1
    return length


def _cstr_equal(left, right) -> int:
    if ptr_is_null(left) != 0 or ptr_is_null(right) != 0:
        return 0
    index: int = 0
    while load_i8(left, index) == load_i8(right, index):
        if load_i8(left, index) == 0:
            return 1
        index = index + 1
    return 0


def _raise_engine_status(status: int) -> None:
    if status == -1:
        py_raise_owned(
            py_exc_new(
                11,
                cstr("pcc re: pattern outside the native regex subset (no-libpython)"),
            )
        )
    elif status == -4:
        py_raise_owned(
            py_exc_new(
                11, cstr("pcc re: non-ASCII text outside the native regex subset")
            )
        )
    else:
        py_raise_owned(
            py_exc_new(7, cstr("pcc re: native regex engine limit reached"))
        )


def _match_class():
    cls = global_load_ptr("pcc_re_match_class")
    if ptr_is_null(cls) != 0:
        cls = py_class_new(cstr("re.Match"), null(), 0, null(), 0)
        if ptr_is_null(cls) == 0:
            store_i32(cls, 12, load_i32(cls, 12) | 1)
            global_store_ptr("pcc_re_match_class", cls)
    return cls


def _pattern_class():
    cls = global_load_ptr("pcc_re_pattern_class")
    if ptr_is_null(cls) != 0:
        cls = py_class_new(cstr("re.Pattern"), null(), 0, null(), 0)
        if ptr_is_null(cls) == 0:
            store_i32(cls, 12, load_i32(cls, 12) | 1)
            global_store_ptr("pcc_re_pattern_class", cls)
    return cls


def _span_at(spans, index: int) -> int:
    item = py_tuple_get(spans, index)
    if ptr_is_null(item) != 0:
        return -1
    overflow = stack_alloc(4)
    store_i32(overflow, 0, 0)
    value: int = py_int_to_i64(item, overflow)
    py_decref(item)
    if load_i32(overflow, 0) != 0:
        return -1
    return value


def _match_method_call(captures, args):
    none = _none()
    if ptr_is_null(captures) != 0 or py_tuple_len(captures) < 4:
        return none
    text = py_tuple_get(captures, 0)
    spans = py_tuple_get(captures, 1)
    kind_obj = py_tuple_get(captures, 2)
    names = py_tuple_get(captures, 3)
    if (
        ptr_is_null(text) != 0
        or ptr_is_null(spans) != 0
        or ptr_is_null(kind_obj) != 0
        or ptr_is_null(names) != 0
    ):
        if ptr_is_null(text) == 0:
            py_decref(text)
        if ptr_is_null(spans) == 0:
            py_decref(spans)
        if ptr_is_null(kind_obj) == 0:
            py_decref(kind_obj)
        if ptr_is_null(names) == 0:
            py_decref(names)
        return none
    overflow = stack_alloc(4)
    store_i32(overflow, 0, 0)
    kind: int = py_int_to_i64(kind_obj, overflow)
    py_decref(kind_obj)
    if load_i32(overflow, 0) != 0:
        kind = 0
    ngroups: int = (py_tuple_len(spans) >> 1) - 1
    nargs: int = 0
    if ptr_is_null(args) == 0:
        nargs = py_tuple_len(args)

    if kind == 4:
        out = py_tuple_new(ngroups)
        group: int = 1
        while group <= ngroups and ptr_is_null(out) == 0:
            lo: int = _span_at(spans, group * 2)
            hi: int = _span_at(spans, group * 2 + 1)
            item = none
            if lo >= 0 and hi >= 0:
                item = py_str_byte_slice_i64(text, lo, hi)
            py_tuple_set_item(out, group - 1, item)
            if ptr_is_null(item) == 0 and item != none:
                py_decref(item)
            group = group + 1
        py_decref(text)
        py_decref(spans)
        py_decref(names)
        return out

    if kind == 5:
        out = py_dict_new()
        group = 1
        while group <= ngroups and ptr_is_null(out) == 0:
            name = py_tuple_get(names, group - 1)
            if ptr_is_null(name) == 0 and name != none:
                lo = _span_at(spans, group * 2)
                hi = _span_at(spans, group * 2 + 1)
                value = none
                if lo >= 0 and hi >= 0:
                    value = py_str_byte_slice_i64(text, lo, hi)
                if ptr_is_null(value) == 0:
                    py_dict_set(out, name, value)
                    if value != none:
                        py_decref(value)
            if ptr_is_null(name) == 0:
                py_decref(name)
            group = group + 1
        py_decref(text)
        py_decref(spans)
        py_decref(names)
        return out

    if nargs >= 2:
        py_decref(text)
        py_decref(spans)
        py_decref(names)
        py_raise_owned(
            py_exc_new(
                11, cstr("pcc re: multi-group Match method arguments are not supported")
            )
        )
        return null()

    selected: int = 0
    if nargs == 1:
        requested = py_tuple_get(args, 0)
        if ptr_is_null(requested) == 0:
            if _type_of(requested) == PY_TYPE_STR:
                want = py_str_utf8(requested)
                selected = -1
                group = 1
                while group <= ngroups and ptr_is_null(want) == 0:
                    name = py_tuple_get(names, group - 1)
                    if ptr_is_null(name) == 0 and name != none:
                        have = py_str_utf8(name)
                        if _cstr_equal(have, want) != 0:
                            selected = group
                            py_decref(name)
                            break
                    if ptr_is_null(name) == 0:
                        py_decref(name)
                    group = group + 1
            else:
                store_i32(overflow, 0, 0)
                selected = py_int_to_i64(requested, overflow)
                if load_i32(overflow, 0) != 0:
                    selected = -1
            py_decref(requested)
    if selected < 0 or selected > ngroups:
        py_decref(text)
        py_decref(spans)
        py_decref(names)
        py_raise_owned(py_exc_new(5, cstr("no such group")))
        return null()

    lo = _span_at(spans, selected * 2)
    hi = _span_at(spans, selected * 2 + 1)
    result = none
    if kind == 0:
        if lo >= 0 and hi >= 0:
            result = py_str_byte_slice_i64(text, lo, hi)
    elif kind == 1:
        result = py_int_from_i64(lo)
    elif kind == 2:
        result = py_int_from_i64(hi)
    elif kind == 3:
        result = py_tuple_new(2)
        lo_obj = py_int_from_i64(lo)
        hi_obj = py_int_from_i64(hi)
        if (
            ptr_is_null(result) == 0
            and ptr_is_null(lo_obj) == 0
            and ptr_is_null(hi_obj) == 0
        ):
            py_tuple_set_item(result, 0, lo_obj)
            py_tuple_set_item(result, 1, hi_obj)
        if ptr_is_null(lo_obj) == 0:
            py_decref(lo_obj)
        if ptr_is_null(hi_obj) == 0:
            py_decref(hi_obj)
    py_decref(text)
    py_decref(spans)
    py_decref(names)
    return result


def _add_match_method(instance, name, text, spans, names, kind: int) -> None:
    captures = py_tuple_new(4)
    if ptr_is_null(captures) != 0:
        return
    kind_obj = py_int_from_i64(kind)
    if ptr_is_null(kind_obj) != 0:
        py_decref(captures)
        return
    py_tuple_set_item(captures, 0, text)
    py_tuple_set_item(captures, 1, spans)
    py_tuple_set_item(captures, 2, kind_obj)
    py_tuple_set_item(captures, 3, names)
    py_decref(kind_obj)
    fn = py_func_new_named(_match_method_call, captures, name)
    py_decref(captures)
    if ptr_is_null(fn) != 0:
        return
    py_obj_setattr(instance, name, fn)
    py_decref(fn)


def _new_match(pattern, text, caps, ngroups: int, flags: int):
    none = _none()
    cls = _match_class()
    if ptr_is_null(cls) != 0:
        return none
    instance = py_instance_new(cls)
    if ptr_is_null(instance) != 0:
        return none
    spans = py_tuple_new((ngroups + 1) * 2)
    if ptr_is_null(spans) != 0:
        return instance
    index: int = 0
    while index < (ngroups + 1) * 2:
        value = py_int_from_i64(load_i64(caps, index * 8))
        if ptr_is_null(value) != 0:
            break
        py_tuple_set_item(spans, index, value)
        py_decref(value)
        index = index + 1
    names = py_tuple_new(ngroups)
    if ptr_is_null(names) != 0:
        py_decref(spans)
        return instance
    name_buffer = stack_alloc(1024)
    names_status: int = -1
    if ptr_is_null(pattern) == 0 and _type_of(pattern) == PY_TYPE_STR:
        pattern_text = py_str_utf8(pattern)
        if ptr_is_null(pattern_text) == 0:
            names_status = pcc_re_engine_group_names_flags(
                pattern_text, flags, name_buffer, 1024
            )
    cursor = name_buffer
    group = 1
    while group <= ngroups:
        name_obj = none
        if names_status >= 0:
            length: int = _cstrlen(cursor)
            if length > 0:
                name_obj = py_str_new(cursor, length)
            cursor = ptr_add(cursor, length + 1)
        if ptr_is_null(name_obj) != 0:
            name_obj = none
        py_tuple_set_item(names, group - 1, name_obj)
        if name_obj != none:
            py_decref(name_obj)
        group = group + 1
    _add_match_method(instance, cstr("group"), text, spans, names, 0)
    _add_match_method(instance, cstr("start"), text, spans, names, 1)
    _add_match_method(instance, cstr("end"), text, spans, names, 2)
    _add_match_method(instance, cstr("span"), text, spans, names, 3)
    _add_match_method(instance, cstr("groups"), text, spans, names, 4)
    _add_match_method(instance, cstr("groupdict"), text, spans, names, 5)
    py_decref(spans)
    py_decref(names)
    return instance


def _run(pattern, text, flags: int, search: int, start: int, endpos: int, caps, ngroups) -> int:
    pattern_text = py_str_utf8(pattern)
    text_bytes = py_str_utf8(text)
    if ptr_is_null(pattern_text) != 0 or ptr_is_null(text_bytes) != 0:
        return 0
    text_length: int = _cstrlen(text_bytes)
    if start < 0:
        start = 0
    if start > text_length:
        start = text_length
    if endpos < 0 or endpos > text_length:
        endpos = text_length
    if endpos < start:
        endpos = start
    return pcc_re_engine_run_flags(
        pattern_text, flags, text_bytes, endpos, start, search, caps, 64, ngroups
    )


@c_abi_export("py_re_engine_truth_flags_from")
def py_re_engine_truth_flags_from(
    pattern, text, flags: int, search: int, start: int, endpos: int
):
    none = _none()
    if ptr_is_null(pattern) != 0 or ptr_is_null(text) != 0:
        return none
    if _type_of(pattern) != PY_TYPE_STR or _type_of(text) != PY_TYPE_STR:
        return none
    caps = stack_alloc(512)
    ngroups = stack_alloc(8)
    store_i64(ngroups, 0, 0)
    status: int = _run(pattern, text, flags, search, start, endpos, caps, ngroups)
    if status == 1:
        return _new_match(pattern, text, caps, load_i64(ngroups, 0), flags)
    if status == 0:
        return none
    _raise_engine_status(status)
    return null()


@c_abi_export("py_re_engine_truth_flags")
def py_re_engine_truth_flags(pattern, text, flags: int, search: int):
    return py_re_engine_truth_flags_from(pattern, text, flags, search, 0, -1)


@c_abi_export("py_re_engine_truth")
def py_re_engine_truth(pattern, text, search: int):
    return py_re_engine_truth_flags_from(pattern, text, 0, search, 0, -1)


@c_abi_export("py_re_engine_fullmatch_flags")
def py_re_engine_fullmatch_flags(pattern, text, flags: int):
    none = _none()
    if ptr_is_null(pattern) != 0 or ptr_is_null(text) != 0:
        return none
    if _type_of(pattern) != PY_TYPE_STR or _type_of(text) != PY_TYPE_STR:
        return none
    if (flags & ~26) != 0:
        py_raise_owned(
            py_exc_new(11, cstr("pcc re: flags outside the native regex subset (no-libpython)"))
        )
        return null()
    caps = stack_alloc(512)
    ngroups = stack_alloc(8)
    store_i64(ngroups, 0, 0)
    text_bytes = py_str_utf8(text)
    text_length: int = _cstrlen(text_bytes)
    status: int = _run(pattern, text, flags, 0, 0, text_length, caps, ngroups)
    if status == 1:
        if load_i64(caps, 8) == text_length:
            return _new_match(pattern, text, caps, load_i64(ngroups, 0), flags)
        return none
    if status == 0:
        return none
    _raise_engine_status(status)
    return null()


def _findall_group(text, caps, group: int):
    lo: int = load_i64(caps, group * 16)
    hi: int = load_i64(caps, group * 16 + 8)
    if lo < 0 or hi < 0:
        return py_str_byte_slice_i64(text, 0, 0)
    return py_str_byte_slice_i64(text, lo, hi)


@c_abi_export("py_re_engine_findall")
def py_re_engine_findall(pattern, text, flags: int):
    if ptr_is_null(pattern) != 0 or ptr_is_null(text) != 0:
        return py_list_new(0)
    if _type_of(pattern) != PY_TYPE_STR or _type_of(text) != PY_TYPE_STR:
        return py_list_new(0)
    text_bytes = py_str_utf8(text)
    if ptr_is_null(text_bytes) != 0:
        return py_list_new(0)
    text_length: int = _cstrlen(text_bytes)
    out = py_list_new(0)
    if ptr_is_null(out) != 0:
        return null()
    caps = stack_alloc(512)
    ngroups = stack_alloc(8)
    position: int = 0
    while position <= text_length:
        store_i64(ngroups, 0, 0)
        status: int = _run(pattern, text, flags, 1, position, text_length, caps, ngroups)
        if status == 0:
            break
        if status != 1:
            py_decref(out)
            _raise_engine_status(status)
            return null()
        group_count: int = load_i64(ngroups, 0)
        lo: int = load_i64(caps, 0)
        hi: int = load_i64(caps, 8)
        if group_count == 0:
            item = py_str_byte_slice_i64(text, lo, hi)
        elif group_count == 1:
            item = _findall_group(text, caps, 1)
        else:
            item = py_tuple_new(group_count)
            group: int = 1
            while group <= group_count and ptr_is_null(item) == 0:
                value = _findall_group(text, caps, group)
                if ptr_is_null(value) != 0:
                    break
                py_tuple_set_item(item, group - 1, value)
                py_decref(value)
                group = group + 1
        if ptr_is_null(item) != 0:
            py_decref(out)
            return null()
        py_list_append(out, item)
        py_decref(item)
        if hi == lo:
            position = hi + 1
        else:
            position = hi
    return out


def _str_has_backslash(value) -> int:
    text = py_str_utf8(value)
    if ptr_is_null(text) != 0:
        return 0
    index: int = 0
    while load_i8(text, index) != 0:
        if load_i8(text, index) == 92:
            return 1
        index = index + 1
    return 0


def _sub_hold(slots, handles, index: int, value, backend: int) -> int:
    slot = ptr_add(slots, index * 8)
    store_ptr(slot, 0, value)
    if (backend == 3 or backend == 4) and ptr_is_null(value) == 0:
        handle = pcc_gc_scheduler_root_register_handle(slot)
        store_ptr(handles, index * 8, handle)
        if ptr_is_null(handle) != 0:
            py_raise_owned(py_exc_new(19, cstr("re.sub: cannot root temporary")))
            return 0
    return 1


def _sub_load(slots, handles, index: int):
    slot = ptr_add(slots, index * 8)
    if ptr_is_null(load_ptr(handles, index * 8)) == 0:
        return pcc_gc_load_ptr(null(), slot)
    return load_ptr(slot, 0)


def _sub_drop(slots, handles, index: int) -> None:
    value = _sub_load(slots, handles, index)
    handle = load_ptr(handles, index * 8)
    if ptr_is_null(handle) == 0:
        pcc_gc_scheduler_root_unregister_handle(handle)
    store_ptr(handles, index * 8, null())
    store_ptr(slots, index * 8, null())
    if index >= 3:
        py_decref(value)


def _sub_append(slots, handles, piece, backend: int) -> int:
    ok: int = _sub_hold(slots, handles, 6, piece, backend)
    if ptr_is_null(piece) != 0:
        ok = 0
    if ok != 0:
        py_list_append(_sub_load(slots, handles, 3), _sub_load(slots, handles, 6))
        if py_err_occurred() != 0:
            ok = 0
    _sub_drop(slots, handles, 6)
    return ok


def _sub_callable(pattern, replacement, text, count: int, flags: int):
    # Borrowed pattern/replacement/text; owned parts/match/args/piece. Each
    # pointer survives replacement callbacks, including explicit collection.
    slots = stack_alloc(56)
    handles = stack_alloc(56)
    memset(slots, 0, 56)
    memset(handles, 0, 56)
    backend: int = pcc_gc_backend()
    ok: int = _sub_hold(slots, handles, 0, pattern, backend)
    if ok != 0:
        ok = _sub_hold(slots, handles, 1, replacement, backend)
    if ok != 0:
        ok = _sub_hold(slots, handles, 2, text, backend)
    if ok != 0:
        parts = py_list_new(0)
        ok = _sub_hold(slots, handles, 3, parts, backend)
        if ptr_is_null(parts) != 0:
            ok = 0
    text_length: int = 0
    if ok != 0:
        text_length = _cstrlen(py_str_utf8(_sub_load(slots, handles, 2)))
    position: int = 0
    last: int = 0
    done: int = 0
    caps = stack_alloc(512)
    ngroups = stack_alloc(8)
    while ok != 0 and position <= text_length and (count == 0 or done < count):
        store_i64(ngroups, 0, 0)
        status: int = _run(_sub_load(slots, handles, 0), _sub_load(slots, handles, 2),
                           flags, 1, position, text_length, caps, ngroups)
        if status == 0:
            break
        if status != 1:
            _raise_engine_status(status)
            ok = 0
            break
        lo: int = load_i64(caps, 0)
        hi: int = load_i64(caps, 8)
        if lo == hi:
            py_raise_owned(py_exc_new(11, cstr("pcc re: callable replacement of empty matches is not supported")))
            ok = 0
            break
        before = py_str_byte_slice_i64(_sub_load(slots, handles, 2), last, lo)
        ok = _sub_append(slots, handles, before, backend)
        if ok != 0:
            match = _new_match(_sub_load(slots, handles, 0), _sub_load(slots, handles, 2),
                               caps, load_i64(ngroups, 0), flags)
            ok = _sub_hold(slots, handles, 4, match, backend)
            if ptr_is_null(match) != 0 or match == _none():
                ok = 0
        if ok != 0:
            args = py_tuple_new(1)
            ok = _sub_hold(slots, handles, 5, args, backend)
            if ptr_is_null(args) != 0:
                ok = 0
        if ok != 0:
            py_tuple_set_item(_sub_load(slots, handles, 5), 0, _sub_load(slots, handles, 4))
            piece = py_obj_call(_sub_load(slots, handles, 1), _sub_load(slots, handles, 5), null())
            if ptr_is_null(piece) != 0:
                ok = 0
            elif _type_of(piece) != PY_TYPE_STR:
                py_decref(piece)
                py_raise_owned(py_exc_new(3, cstr("re.sub replacement callback must return str")))
                ok = 0
            else:
                ok = _sub_append(slots, handles, piece, backend)
        _sub_drop(slots, handles, 5)
        _sub_drop(slots, handles, 4)
        done = done + 1
        last = hi
        position = hi
    result = null()
    if ok != 0:
        tail = py_str_byte_slice_i64(_sub_load(slots, handles, 2), last, text_length)
        ok = _sub_append(slots, handles, tail, backend)
    if ok != 0:
        empty = py_str_byte_slice_i64(_sub_load(slots, handles, 2), 0, 0)
        ok = _sub_hold(slots, handles, 6, empty, backend)
        if ptr_is_null(empty) == 0 and ok != 0:
            result = py_str_join(_sub_load(slots, handles, 6), _sub_load(slots, handles, 3))
    index: int = 6
    while index >= 0:
        _sub_drop(slots, handles, index)
        index = index - 1
    return result


@c_abi_export("py_re_engine_sub")
def py_re_engine_sub(pattern, replacement, text, count: int, flags: int):
    none = _none()
    if (
        ptr_is_null(pattern) != 0
        or ptr_is_null(replacement) != 0
        or ptr_is_null(text) != 0
    ):
        return none
    if _type_of(pattern) != PY_TYPE_STR or _type_of(text) != PY_TYPE_STR:
        py_raise_owned(
            py_exc_new(3, cstr("pcc re: sub expects string pattern, replacement, and text"))
        )
        return null()
    callable_value = py_builtin_callable(replacement)
    is_callable: int = py_obj_truthy(callable_value)
    py_decref(callable_value)
    if is_callable != 0:
        return _sub_callable(pattern, replacement, text, count, flags)
    if _type_of(replacement) != PY_TYPE_STR:
        py_raise_owned(py_exc_new(3, cstr("re.sub replacement must be str or callable")))
        return null()
    if _str_has_backslash(replacement) != 0:
        py_raise_owned(
            py_exc_new(11, cstr("pcc re: backslash replacement templates are not supported"))
        )
        return null()
    text_bytes = py_str_utf8(text)
    text_length: int = _cstrlen(text_bytes)
    parts = py_list_new(0)
    if ptr_is_null(parts) != 0:
        return null()
    position: int = 0
    last: int = 0
    done: int = 0
    caps = stack_alloc(512)
    ngroups = stack_alloc(8)
    while position <= text_length and (count <= 0 or done < count):
        store_i64(ngroups, 0, 0)
        status: int = _run(pattern, text, flags, 1, position, text_length, caps, ngroups)
        if status == 0:
            break
        if status != 1:
            py_decref(parts)
            _raise_engine_status(status)
            return null()
        lo: int = load_i64(caps, 0)
        hi: int = load_i64(caps, 8)
        before = py_str_byte_slice_i64(text, last, lo)
        if ptr_is_null(before) == 0:
            py_list_append(parts, before)
            py_decref(before)
        py_list_append(parts, replacement)
        done = done + 1
        if lo == hi:
            if hi < text_length:
                one = py_str_byte_slice_i64(text, hi, hi + 1)
                if ptr_is_null(one) == 0:
                    py_list_append(parts, one)
                    py_decref(one)
            last = hi + 1
            position = hi + 1
        else:
            last = hi
            position = hi
    if last <= text_length:
        tail = py_str_byte_slice_i64(text, last, text_length)
        if ptr_is_null(tail) == 0:
            py_list_append(parts, tail)
            py_decref(tail)
    empty = py_str_byte_slice_i64(text, 0, 0)
    if ptr_is_null(empty) != 0:
        py_decref(parts)
        return null()
    result = py_str_join(empty, parts)
    py_decref(empty)
    py_decref(parts)
    return result


def _finditer_position(state, position: int) -> int:
    value = py_int_from_i64(position)
    if ptr_is_null(value) != 0:
        return 0
    py_tuple_set_item(state, 3, value)
    py_decref(value)
    return 1


def _finditer_next(state, args):
    slots = stack_alloc(56)
    handles = stack_alloc(56)
    memset(slots, 0, 56)
    memset(handles, 0, 56)
    backend: int = pcc_gc_backend()
    ok: int = _sub_hold(slots, handles, 0, state, backend)
    position: int = _arg_int(state, 3, 0)
    end: int = _arg_int(state, 4, 0)
    flags: int = _arg_int(state, 2, 0)
    result = null()
    if ok != 0 and position <= end:
        pattern = py_tuple_get(_sub_load(slots, handles, 0), 0)
        ok = _sub_hold(slots, handles, 3, pattern, backend)
        if ptr_is_null(pattern) != 0:
            ok = 0
        if ok != 0:
            text = py_tuple_get(_sub_load(slots, handles, 0), 1)
            ok = _sub_hold(slots, handles, 4, text, backend)
            if ptr_is_null(text) != 0:
                ok = 0
        if ok != 0:
            caps = stack_alloc(512)
            ngroups = stack_alloc(8)
            store_i64(ngroups, 0, 0)
            status: int = pcc_re_engine_run_flags(
                py_str_utf8(_sub_load(slots, handles, 3)), flags,
                py_str_utf8(_sub_load(slots, handles, 4)), end, position, 1, caps, 64, ngroups)
            if status == 0:
                ok = _finditer_position(_sub_load(slots, handles, 0), end + 1)
                if ok != 0:
                    result = _none()
            elif status != 1:
                _raise_engine_status(status)
            elif load_i64(caps, 0) == load_i64(caps, 8):
                py_raise_owned(py_exc_new(11, cstr("pcc re: finditer of empty matches is not supported")))
            else:
                ok = _finditer_position(_sub_load(slots, handles, 0), load_i64(caps, 8))
                if ok != 0:
                    result = _new_match(_sub_load(slots, handles, 3), _sub_load(slots, handles, 4),
                                        caps, load_i64(ngroups, 0), flags)
    elif ok != 0:
        result = _none()
    index: int = 6
    while index >= 0:
        _sub_drop(slots, handles, index)
        index = index - 1
    return result


def _new_finditer(pattern, text, flags: int, start: int, end: int):
    if ptr_is_null(text) != 0 or _type_of(text) != PY_TYPE_STR:
        py_raise_owned(py_exc_new(3, cstr("pcc re: finditer expects a string")))
        return null()
    length: int = load_i64(text, 16)
    if start < 0:
        start = 0
    elif start > length:
        start = length
    if end < 0:
        end = 0
    elif end > length:
        end = length
    slots = stack_alloc(56)
    handles = stack_alloc(56)
    memset(slots, 0, 56)
    memset(handles, 0, 56)
    backend: int = pcc_gc_backend()
    ok: int = _sub_hold(slots, handles, 0, pattern, backend)
    if ok != 0:
        ok = _sub_hold(slots, handles, 1, text, backend)
    if ok != 0:
        captures = py_tuple_new(5)
        ok = _sub_hold(slots, handles, 3, captures, backend)
        if ptr_is_null(captures) != 0:
            ok = 0
    if ok != 0:
        py_tuple_set_item(_sub_load(slots, handles, 3), 0, _sub_load(slots, handles, 0))
        py_tuple_set_item(_sub_load(slots, handles, 3), 1, _sub_load(slots, handles, 1))
        index: int = 2
        while index < 5 and ok != 0:
            number: int = flags
            if index == 3:
                number = start
            elif index == 4:
                number = end
            value = py_int_from_i64(number)
            if ptr_is_null(value) != 0:
                ok = 0
            else:
                py_tuple_set_item(_sub_load(slots, handles, 3), index, value)
                py_decref(value)
            index = index + 1
    result = null()
    if ok != 0:
        callback = py_func_new_named(_finditer_next, _sub_load(slots, handles, 3), cstr("re.finditer.next"))
        ok = _sub_hold(slots, handles, 4, callback, backend)
        if ptr_is_null(callback) == 0 and ok != 0:
            result = py_iter_callable_new(_sub_load(slots, handles, 4), _none())
    index: int = 6
    while index >= 0:
        _sub_drop(slots, handles, index)
        index = index - 1
    return result


@c_abi_export("py_re_finditer_flags")
def py_re_finditer_flags(pattern, text, flags: int):
    if ptr_is_null(pattern) != 0 or _type_of(pattern) != PY_TYPE_STR:
        py_raise_owned(py_exc_new(3, cstr("pcc re: finditer pattern must be a string")))
        return null()
    if (flags & ~26) != 0 or pcc_re_engine_supported_flags(py_str_utf8(pattern), flags) == 0:
        py_raise_owned(py_exc_new(11, cstr("pcc re: finditer pattern or flags outside the native subset")))
        return null()
    return _new_finditer(pattern, text, flags, 0, 9223372036854775807)


@c_abi_export("py_re_engine_split")
def py_re_engine_split(pattern, text, maxsplit: int, flags: int):
    none = _none()
    if ptr_is_null(pattern) != 0 or ptr_is_null(text) != 0:
        return none
    if _type_of(pattern) != PY_TYPE_STR or _type_of(text) != PY_TYPE_STR:
        py_raise_owned(py_exc_new(3, cstr("pcc re: split expects string pattern and text")))
        return null()
    text_bytes = py_str_utf8(text)
    text_length: int = _cstrlen(text_bytes)
    out = py_list_new(0)
    if ptr_is_null(out) != 0:
        return null()
    position: int = 0
    last: int = 0
    done: int = 0
    caps = stack_alloc(512)
    ngroups = stack_alloc(8)
    while position <= text_length and (maxsplit <= 0 or done < maxsplit):
        store_i64(ngroups, 0, 0)
        status: int = _run(pattern, text, flags, 1, position, text_length, caps, ngroups)
        if status == 0:
            break
        if status != 1:
            py_decref(out)
            _raise_engine_status(status)
            return null()
        lo: int = load_i64(caps, 0)
        hi: int = load_i64(caps, 8)
        piece = py_str_byte_slice_i64(text, last, lo)
        if ptr_is_null(piece) == 0:
            py_list_append(out, piece)
            py_decref(piece)
        group_count: int = load_i64(ngroups, 0)
        group: int = 1
        while group <= group_count:
            group_lo: int = load_i64(caps, group * 16)
            group_hi: int = load_i64(caps, group * 16 + 8)
            if group_lo < 0 or group_hi < 0:
                py_list_append(out, none)
            else:
                group_value = py_str_byte_slice_i64(text, group_lo, group_hi)
                if ptr_is_null(group_value) == 0:
                    py_list_append(out, group_value)
                    py_decref(group_value)
            group = group + 1
        done = done + 1
        last = hi
        if hi == lo:
            position = hi + 1
        else:
            position = hi
    tail = py_str_byte_slice_i64(text, last, text_length)
    if ptr_is_null(tail) == 0:
        py_list_append(out, tail)
        py_decref(tail)
    return out


def _arg_int(args, index: int, fallback: int) -> int:
    if ptr_is_null(args) != 0 or py_tuple_len(args) <= index:
        return fallback
    value = py_tuple_get(args, index)
    if ptr_is_null(value) != 0:
        return fallback
    overflow = stack_alloc(4)
    store_i32(overflow, 0, 0)
    result: int = py_int_to_i64(value, overflow)
    py_decref(value)
    if load_i32(overflow, 0) != 0:
        return fallback
    return result


def _finditer_from_args(pattern, args, flags: int):
    count: int = 0
    if ptr_is_null(args) == 0:
        count = py_tuple_len(args)
    if count < 1 or count > 3:
        py_raise_owned(py_exc_new(3, cstr("Pattern.finditer expects one to three arguments")))
        return null()
    slots = stack_alloc(56)
    handles = stack_alloc(56)
    memset(slots, 0, 56)
    memset(handles, 0, 56)
    backend: int = pcc_gc_backend()
    ok: int = _sub_hold(slots, handles, 0, pattern, backend)
    if ok != 0:
        ok = _sub_hold(slots, handles, 1, args, backend)
    if ok != 0:
        text = py_tuple_get(_sub_load(slots, handles, 1), 0)
        ok = _sub_hold(slots, handles, 3, text, backend)
        if ptr_is_null(text) != 0:
            ok = 0
    start: int = 0
    end: int = 9223372036854775807
    index: int = 1
    while index < count and ok != 0:
        value = py_tuple_get(_sub_load(slots, handles, 1), index)
        ok = _sub_hold(slots, handles, 4, value, backend)
        if ptr_is_null(value) != 0:
            ok = 0
        elif value == _none():
            py_raise_owned(py_exc_new(3, cstr("finditer bounds must be integers")))
            ok = 0
        elif ok != 0:
            bound: int = py_index_i64_checked(_sub_load(slots, handles, 4))
            if py_err_occurred() != 0:
                ok = 0
            elif index == 1:
                start = bound
            else:
                end = bound
        _sub_drop(slots, handles, 4)
        index = index + 1
    result = null()
    if ok != 0:
        result = _new_finditer(_sub_load(slots, handles, 0), _sub_load(slots, handles, 3),
                               flags, start, end)
    index: int = 6
    while index >= 0:
        _sub_drop(slots, handles, index)
        index = index - 1
    return result


def _pattern_method_call(captures, args):
    none = _none()
    if ptr_is_null(captures) != 0 or py_tuple_len(captures) < 3:
        return none
    pattern = py_tuple_get(captures, 0)
    kind_obj = py_tuple_get(captures, 1)
    flags_obj = py_tuple_get(captures, 2)
    if ptr_is_null(pattern) != 0 or ptr_is_null(kind_obj) != 0 or ptr_is_null(flags_obj) != 0:
        if ptr_is_null(pattern) == 0:
            py_decref(pattern)
        if ptr_is_null(kind_obj) == 0:
            py_decref(kind_obj)
        if ptr_is_null(flags_obj) == 0:
            py_decref(flags_obj)
        return none
    kind: int = _arg_int(captures, 1, 0)
    flags: int = _arg_int(captures, 2, 0)
    py_decref(kind_obj)
    py_decref(flags_obj)
    nargs: int = 0
    if ptr_is_null(args) == 0:
        nargs = py_tuple_len(args)
    if kind == 5:
        result = _finditer_from_args(pattern, args, flags)
        py_decref(pattern)
        return result
    if kind == 3:
        if nargs < 2:
            py_decref(pattern)
            py_raise_owned(py_exc_new(3, cstr("pcc re: Pattern.sub expects replacement and string")))
            return null()
        replacement = py_tuple_get(args, 0)
        text = py_tuple_get(args, 1)
        result = null()
        if ptr_is_null(replacement) == 0 and ptr_is_null(text) == 0:
            result = py_re_engine_sub(pattern, replacement, text, _arg_int(args, 2, 0), flags)
        if ptr_is_null(replacement) == 0:
            py_decref(replacement)
        if ptr_is_null(text) == 0:
            py_decref(text)
        py_decref(pattern)
        return result
    if kind == 4:
        if nargs < 1:
            py_decref(pattern)
            py_raise_owned(py_exc_new(3, cstr("pcc re: Pattern.split expects a string")))
            return null()
        text = py_tuple_get(args, 0)
        result = null()
        if ptr_is_null(text) == 0:
            result = py_re_engine_split(pattern, text, _arg_int(args, 1, 0), flags)
            py_decref(text)
        py_decref(pattern)
        return result
    if nargs < 1:
        py_decref(pattern)
        py_raise_owned(py_exc_new(3, cstr("pcc re: Pattern method expects one string argument")))
        return null()
    text = py_tuple_get(args, 0)
    result = null()
    if ptr_is_null(text) == 0:
        if kind == 2:
            result = py_re_engine_findall(pattern, text, flags)
        else:
            search: int = 0
            if kind == 1:
                search = 1
            result = py_re_engine_truth_flags_from(
                pattern,
                text,
                flags,
                search,
                _arg_int(args, 1, 0),
                _arg_int(args, 2, -1),
            )
        py_decref(text)
    py_decref(pattern)
    return result


def _add_pattern_method(instance, name, pattern, kind: int, flags: int) -> None:
    captures = py_tuple_new(3)
    if ptr_is_null(captures) != 0:
        return
    kind_obj = py_int_from_i64(kind)
    flags_obj = py_int_from_i64(flags)
    if ptr_is_null(kind_obj) != 0 or ptr_is_null(flags_obj) != 0:
        if ptr_is_null(kind_obj) == 0:
            py_decref(kind_obj)
        if ptr_is_null(flags_obj) == 0:
            py_decref(flags_obj)
        py_decref(captures)
        return
    py_tuple_set_item(captures, 0, pattern)
    py_tuple_set_item(captures, 1, kind_obj)
    py_tuple_set_item(captures, 2, flags_obj)
    py_decref(kind_obj)
    py_decref(flags_obj)
    fn = py_func_new_named(_pattern_method_call, captures, name)
    py_decref(captures)
    if ptr_is_null(fn) != 0:
        return
    py_obj_setattr(instance, name, fn)
    py_decref(fn)


@c_abi_export("py_re_compile_obj")
def py_re_compile_obj(pattern, flags: int):
    if ptr_is_null(pattern) != 0 or _type_of(pattern) != PY_TYPE_STR:
        py_raise_owned(py_exc_new(3, cstr("pcc re: re.compile pattern must be a string")))
        return null()
    if (flags & ~26) != 0:
        py_raise_owned(
            py_exc_new(11, cstr("pcc re: re.compile flags are outside the native regex subset"))
        )
        return null()
    pattern_text = py_str_utf8(pattern)
    if ptr_is_null(pattern_text) != 0 or pcc_re_engine_supported_flags(pattern_text, flags) == 0:
        py_raise_owned(
            py_exc_new(
                11, cstr("pcc re: pattern outside the native regex subset (no-libpython)")
            )
        )
        return null()
    cls = _pattern_class()
    if ptr_is_null(cls) != 0:
        return _none()
    instance = py_instance_new(cls)
    if ptr_is_null(instance) != 0:
        return _none()
    py_obj_setattr(instance, cstr("pattern"), pattern)
    _add_pattern_method(instance, cstr("match"), pattern, 0, flags)
    _add_pattern_method(instance, cstr("search"), pattern, 1, flags)
    _add_pattern_method(instance, cstr("findall"), pattern, 2, flags)
    _add_pattern_method(instance, cstr("sub"), pattern, 3, flags)
    _add_pattern_method(instance, cstr("split"), pattern, 4, flags)
    _add_pattern_method(instance, cstr("finditer"), pattern, 5, flags)
    return instance


@c_abi_export("py_re_engine_pattern_supported")
def py_re_engine_pattern_supported(pattern) -> int:
    if ptr_is_null(pattern) != 0 or _type_of(pattern) != PY_TYPE_STR:
        return 0
    pattern_text = py_str_utf8(pattern)
    if ptr_is_null(pattern_text) != 0:
        return 0
    if pcc_re_engine_supported(pattern_text) != 0:
        return 1
    return 0
