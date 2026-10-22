"""pcc-Python helpers for ``*args``, ``**kwargs``, and ``zip(*rows)``."""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import DICTENTRY_KEY_OFFSET, DICTENTRY_SIZE, PYDICTOBJECT_ENTRIES_OFFSET, PYDICTOBJECT_ENTRIES_USED_OFFSET, PY_TYPE_DICT, PY_TYPE_INT, PY_TYPE_LIST, PY_TYPE_STR, PY_TYPE_TUPLE

from pcc.extern import c_abi_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import cstr, global_load_ptr, is_tagged_int, load_i32, load_i64, load_ptr, memset, null, ptr_add, ptr_eq, ptr_is_null, stack_alloc, store_i64, store_ptr, strlen


py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_tuple_len = extern("py_tuple_len", (c_ptr,), c_int64)
py_tuple_get = extern("py_tuple_get", (c_ptr, c_int64), c_ptr)
py_tuple_set_item = extern("py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void)
py_list_new = extern("py_list_new", (c_int64,), c_ptr)
py_list_len = extern("py_list_len", (c_ptr,), c_int64)
py_list_get = extern("py_list_get", (c_ptr, c_int64), c_ptr)
py_list_append = extern("py_list_append", (c_ptr, c_ptr), c_void)
py_dict_new = extern("py_dict_new", (), c_ptr)
py_dict_update = extern("py_dict_update", (c_ptr, c_ptr), c_void)
py_dict_keys = extern("py_dict_keys", (c_ptr,), c_ptr)
py_dict_contains = extern("py_dict_contains", (c_ptr, c_ptr), c_int64)
py_dict_set = extern("py_dict_set", (c_ptr, c_ptr, c_ptr), c_void)
py_obj_getattr = extern("py_obj_getattr", (c_ptr, c_ptr), c_ptr)
py_obj_iter = extern("py_obj_iter", (c_ptr,), c_ptr)
py_obj_next = extern("py_obj_next", (c_ptr,), c_ptr)
py_obj_type_name = extern("py_obj_type_name", (c_ptr,), c_ptr)
py_str_new = extern("py_str_new", (c_ptr, c_int64), c_ptr)
py_str_concat = extern("py_str_concat", (c_ptr, c_ptr), c_ptr)
py_str_utf8 = extern("py_str_utf8", (c_ptr,), c_ptr)
py_str_eq = extern("py_str_eq", (c_ptr, c_ptr), c_int64)
py_obj_str = extern("py_obj_str", (c_ptr,), c_ptr)
py_obj_repr = extern("py_obj_repr", (c_ptr,), c_ptr)
py_bound_method_function = extern("py_bound_method_function", (c_ptr,), c_ptr)
pcc_gc_scheduler_root_register_handle = extern("pcc_gc_scheduler_root_register_handle", (c_ptr,), c_ptr)
pcc_gc_scheduler_root_unregister_handle = extern("pcc_gc_scheduler_root_unregister_handle", (c_ptr,), c_void)
pcc_gc_foreign_lease_acquire = extern("pcc_gc_foreign_lease_acquire", (c_ptr,), c_int64)
pcc_gc_foreign_lease_release = extern("pcc_gc_foreign_lease_release", (c_ptr, c_int64), c_int64)
pcc_gc_root_move = extern("pcc_gc_root_move", (c_ptr, c_ptr), c_int64)
pcc_gc_resolve_root_slot_unlocked = extern("pcc_gc_resolve_root_slot_unlocked", (c_ptr, c_int64), c_ptr)
pcc_gc_store_root = extern("pcc_gc_store_root", (c_ptr, c_ptr), c_void)
pcc_gc_note_slot_write_barrier = extern("pcc_gc_note_slot_write_barrier", (c_ptr, c_ptr, c_ptr), c_void)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)
py_tls_exc_swap_slot = extern("py_tls_exc_swap_slot", (c_ptr,), c_void)
pcc_platform_abort = extern("pcc_platform_abort", (), c_void)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_current_exception = extern("py_current_exception", (), c_ptr)
py_clear_exception = extern("py_clear_exception", (), c_void)
py_exc_matches = extern("py_exc_matches", (c_ptr, c_ptr), c_int64)
py_exc_builtin_class = extern("py_exc_builtin_class", (c_int64,), c_ptr)
py_obj_len = extern("py_obj_len", (c_ptr,), c_int64)
py_obj_getitem = extern("py_obj_getitem", (c_ptr, c_ptr), c_ptr)
py_obj_call = extern("py_obj_call", (c_ptr, c_ptr, c_ptr), c_ptr)
py_int_from_i64 = extern("py_int_from_i64", (c_int64,), c_ptr)
py_incref = extern("py_incref", (c_ptr,), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_runtime_error_if_unset = extern(
    "py_runtime_error_if_unset", (c_ptr, c_ptr), c_ptr
)


def _require_result(result, helper_name, message):
    if ptr_is_null(result):
        py_runtime_error_if_unset(helper_name, message)
    return result


def _is_none(o) -> bool:
    return ptr_is_null(o) or ptr_eq(o, global_load_ptr("py_None"))


def _type_of(o) -> int:
    if ptr_is_null(o):
        return -1
    if is_tagged_int(o):
        return PY_TYPE_INT
    return load_i32(o, 8)


def _sequence_len(o) -> int:
    tag: int = _type_of(o)
    if tag == PY_TYPE_TUPLE:
        return py_tuple_len(o)
    if tag == PY_TYPE_LIST:
        return py_list_len(o)
    return -1


def _sequence_get(o, index: int):
    if _type_of(o) == PY_TYPE_TUPLE:
        return py_tuple_get(o, index)
    if _type_of(o) == PY_TYPE_LIST:
        return py_list_get(o, index)
    return null()


@c_abi_export("py_call_merge_posargs")
def py_call_merge_posargs(base_tuple, star_args):
    if _is_none(base_tuple):
        base_tuple = py_tuple_new(0)
        if ptr_is_null(base_tuple):
            return _require_result(
                null(),
                cstr("py_tuple_new"),
                cstr("call splat could not allocate the base argument tuple"),
            )
    elif _type_of(base_tuple) != PY_TYPE_TUPLE:
        py_raise_owned(py_exc_new(3, cstr("call args base must be tuple")))
        return null()
    else:
        py_incref(base_tuple)

    base_len: int = py_tuple_len(base_tuple)
    if _is_none(star_args):
        return base_tuple
    star_len: int = _sequence_len(star_args)
    if star_len < 0:
        py_decref(base_tuple)
        py_raise_owned(py_exc_new(3, cstr("*args must be tuple or list")))
        return null()

    out = py_tuple_new(base_len + star_len)
    if ptr_is_null(out):
        _require_result(
            null(),
            cstr("py_tuple_new"),
            cstr("call splat could not allocate the merged argument tuple"),
        )
        py_decref(base_tuple)
        return null()
    i: int = 0
    while i < base_len:
        item = py_tuple_get(base_tuple, i)
        if ptr_is_null(item):
            _require_result(
                null(),
                cstr("py_tuple_get"),
                cstr("call splat could not read a base positional argument"),
            )
            py_decref(out)
            py_decref(base_tuple)
            return null()
        py_tuple_set_item(out, i, item)
        py_decref(item)
        i = i + 1
    i = 0
    while i < star_len:
        item = _sequence_get(star_args, i)
        if ptr_is_null(item):
            _require_result(
                null(),
                cstr("pcc_sequence_get_for_splat"),
                cstr("call splat could not read a starred positional argument"),
            )
            py_decref(out)
            py_decref(base_tuple)
            return null()
        py_tuple_set_item(out, base_len + i, item)
        py_decref(item)
        i = i + 1
    py_decref(base_tuple)
    return out


@c_abi_export("py_zip_star")
def py_zip_star(rows):
    if _is_none(rows):
        return _require_result(
            py_list_new(0),
            cstr("py_list_new"),
            cstr("zip splat could not allocate its result list"),
        )
    nrows: int = _sequence_len(rows)
    if nrows < 0:
        py_raise_owned(py_exc_new(3, cstr("zip(*x): x must be a tuple or list")))
        return null()
    if nrows == 0:
        return _require_result(
            py_list_new(0),
            cstr("py_list_new"),
            cstr("zip splat could not allocate its empty result list"),
        )

    min_len: int = -1
    row_index: int = 0
    while row_index < nrows:
        row = _sequence_get(rows, row_index)
        if ptr_is_null(row):
            return _require_result(
                null(),
                cstr("pcc_sequence_get_for_splat"),
                cstr("zip splat could not read an input row"),
            )
        row_len: int = py_obj_len(row)
        if row_len < 0:
            _require_result(
                null(),
                cstr("py_obj_len"),
                cstr("zip splat row length failed without setting an exception"),
            )
            py_decref(row)
            return null()
        py_decref(row)
        if min_len < 0 or row_len < min_len:
            min_len = row_len
        row_index = row_index + 1
    if min_len < 0:
        min_len = 0

    out = py_list_new(0)
    if ptr_is_null(out):
        return _require_result(
            null(),
            cstr("py_list_new"),
            cstr("zip splat could not allocate its result list"),
        )
    column: int = 0
    while column < min_len:
        column_obj = py_int_from_i64(column)
        if ptr_is_null(column_obj):
            _require_result(
                null(),
                cstr("py_int_from_i64"),
                cstr("zip splat could not allocate a column index"),
            )
            py_decref(out)
            return null()
        tuple_obj = py_tuple_new(nrows)
        if ptr_is_null(tuple_obj):
            _require_result(
                null(),
                cstr("py_tuple_new"),
                cstr("zip splat could not allocate a result row"),
            )
            py_decref(column_obj)
            py_decref(out)
            return null()
        row_index = 0
        while row_index < nrows:
            row = _sequence_get(rows, row_index)
            if ptr_is_null(row):
                _require_result(
                    null(),
                    cstr("pcc_sequence_get_for_splat"),
                    cstr("zip splat could not reload an input row"),
                )
                py_decref(tuple_obj)
                py_decref(column_obj)
                py_decref(out)
                return null()
            element = py_obj_getitem(row, column_obj)
            if ptr_is_null(element):
                _require_result(
                    null(),
                    cstr("py_obj_getitem"),
                    cstr("zip splat element lookup failed without setting an exception"),
                )
                py_decref(row)
                py_decref(tuple_obj)
                py_decref(column_obj)
                py_decref(out)
                return null()
            py_decref(row)
            py_tuple_set_item(tuple_obj, row_index, element)
            py_decref(element)
            row_index = row_index + 1
        py_decref(column_obj)
        py_list_append(out, tuple_obj)
        py_decref(tuple_obj)
        column = column + 1
    return out


def _dict_clone(source):
    out = py_dict_new()
    if ptr_is_null(out):
        return _require_result(
            null(),
            cstr("py_dict_new"),
            cstr("call splat could not allocate the merged keyword dictionary"),
        )
    if not _is_none(source):
        if _type_of(source) != PY_TYPE_DICT:
            py_decref(out)
            py_raise_owned(py_exc_new(3, cstr("kwargs base must be dict")))
            return null()
        py_dict_update(out, source)
    return out


@c_abi_export("py_call_merge_kwargs")
def py_call_merge_kwargs(base_kwargs, star_kwargs):
    out = _dict_clone(base_kwargs)
    if ptr_is_null(out):
        return _require_result(
            null(),
            cstr("pcc_dict_clone"),
            cstr("call splat could not clone its keyword dictionary"),
        )
    if _is_none(star_kwargs):
        return out
    if _type_of(star_kwargs) != PY_TYPE_DICT:
        py_decref(out)
        py_raise_owned(py_exc_new(3, cstr("**kwargs must be dict")))
        return null()
    py_dict_update(out, star_kwargs)
    return out


def _cat_owned(acc, piece):
    if ptr_is_null(acc) or ptr_is_null(piece):
        if not ptr_is_null(acc):
            py_decref(acc)
        if not ptr_is_null(piece):
            py_decref(piece)
        return null()
    out = py_str_concat(acc, piece)
    py_decref(acc)
    py_decref(piece)
    return out


def _raise_type_error_text(text) -> None:
    if not ptr_is_null(text):
        py_raise_owned(py_exc_new(3, py_str_utf8(text)))  # 3 == PY_EXC_TYPEERROR
        py_decref(text)


def _call_lit(text):
    return py_str_new(text, strlen(text))


def _call_optional_attr(callable_obj, name):
    value = py_obj_getattr(callable_obj, name)
    if ptr_is_null(value) and py_err_occurred() != 0:
        if py_exc_matches(py_current_exception(), py_exc_builtin_class(6)) != 0:
            py_clear_exception()
    return value


def _call_diagnostic_open(slots, tokens, handles) -> int:
    memset(slots, 0, 56)
    memset(tokens, 0, 56)
    memset(handles, 0, 56)
    count: int = 0
    while count < 7:
        handle = pcc_gc_scheduler_root_register_handle(ptr_add(slots, count * 8))
        if ptr_is_null(handle):
            return count
        store_ptr(handles, count * 8, handle)
        count = count + 1
    return count


def _call_diagnostic_adopt(slots, tokens, index: int) -> int:
    slot = ptr_add(slots, index * 8)
    if ptr_is_null(load_ptr(slot, 0)):
        _require_result(null(), cstr("call diagnostic"), cstr("diagnostic value allocation failed"))
        return -1
    token: int = pcc_gc_foreign_lease_acquire(slot)
    if token < 0:
        _require_result(null(), cstr("call diagnostic"), cstr("diagnostic value lease failed"))
        return -1
    store_i64(tokens, index * 8, token)
    pcc_py_gc_minor_graph_lock()
    pcc_gc_note_slot_write_barrier(null(), slot, load_ptr(slot, 0))
    pcc_py_gc_minor_graph_unlock()
    return 0


def _call_diagnostic_drop(slots, tokens, index: int) -> None:
    slot = ptr_add(slots, index * 8)
    if pcc_gc_foreign_lease_release(slot, load_i64(tokens, index * 8)) != 0:
        pcc_platform_abort()
        return
    store_i64(tokens, index * 8, 0)
    pcc_gc_store_root(slot, null())


def _call_diagnostic_append(slots, tokens) -> int:
    # acc3 + piece4 -> temporary5; all sources keep independent counted leases.
    store_ptr(slots, 40, py_str_concat(load_ptr(slots, 24), load_ptr(slots, 32)))
    if _call_diagnostic_adopt(slots, tokens, 5) != 0:
        return -1
    _call_diagnostic_drop(slots, tokens, 3)
    _call_diagnostic_drop(slots, tokens, 4)
    token: int = load_i64(tokens, 40)
    if pcc_gc_root_move(ptr_add(slots, 24), ptr_add(slots, 40)) != 0:
        _require_result(null(), cstr("call diagnostic"), cstr("diagnostic result publication failed"))
        return -1
    store_i64(tokens, 24, token)
    store_i64(tokens, 40, 0)
    return 0


def _call_diagnostic_append_literal(slots, tokens, text) -> int:
    store_ptr(slots, 32, _call_lit(text))
    if _call_diagnostic_adopt(slots, tokens, 4) != 0:
        return -1
    return _call_diagnostic_append(slots, tokens)


def _call_diagnostic_prefix(slots, tokens, callable_obj) -> int:
    # The incoming callable has its caller's address lease. The optional
    # original and every allocating metadata/text result publish immediately.
    store_ptr(slots, 0, py_bound_method_function(callable_obj))
    if not ptr_is_null(load_ptr(slots, 0)):
        if _call_diagnostic_adopt(slots, tokens, 0) != 0:
            return -1
        callable_obj = load_ptr(slots, 0)
    store_ptr(slots, 8, _call_optional_attr(callable_obj, cstr("__qualname__")))
    if ptr_is_null(load_ptr(slots, 8)):
        if py_err_occurred() != 0:
            return -1
        store_ptr(slots, 24, py_obj_repr(callable_obj))
        if _call_diagnostic_adopt(slots, tokens, 3) != 0:
            return -1
        return _call_diagnostic_append_literal(slots, tokens, cstr(" "))
    if _call_diagnostic_adopt(slots, tokens, 1) != 0:
        return -1
    store_ptr(slots, 16, _call_optional_attr(callable_obj, cstr("__module__")))
    if ptr_is_null(load_ptr(slots, 16)):
        if py_err_occurred() != 0:
            return -1
    elif _call_diagnostic_adopt(slots, tokens, 2) != 0:
        return -1
    include_module: int = 0
    if not _is_none(load_ptr(slots, 16)):
        store_ptr(slots, 32, _call_lit(cstr("builtins")))
        if _call_diagnostic_adopt(slots, tokens, 4) != 0:
            return -1
        include_module = 1
        if _type_of(load_ptr(slots, 16)) == PY_TYPE_STR:
            if py_str_eq(load_ptr(slots, 16), load_ptr(slots, 32)) != 0:
                include_module = 0
        _call_diagnostic_drop(slots, tokens, 4)
    if include_module != 0:
        store_ptr(slots, 24, py_obj_str(load_ptr(slots, 16)))
        if _call_diagnostic_adopt(slots, tokens, 3) != 0:
            return -1
        if _call_diagnostic_append_literal(slots, tokens, cstr(".")) != 0:
            return -1
        store_ptr(slots, 32, py_obj_str(load_ptr(slots, 8)))
        if _call_diagnostic_adopt(slots, tokens, 4) != 0:
            return -1
        if _call_diagnostic_append(slots, tokens) != 0:
            return -1
    else:
        store_ptr(slots, 24, py_obj_str(load_ptr(slots, 8)))
        if _call_diagnostic_adopt(slots, tokens, 3) != 0:
            return -1
    return _call_diagnostic_append_literal(slots, tokens, cstr("() "))


def _call_diagnostic_close(slots, tokens, handles, count: int) -> None:
    if count == 7:
        py_tls_exc_swap_slot(ptr_add(slots, 48))
        index: int = 5
        while index >= 0:
            _call_diagnostic_drop(slots, tokens, index)
            index = index - 1
        py_clear_exception()
        py_tls_exc_swap_slot(ptr_add(slots, 48))
    index = 0
    while index < count:
        pcc_gc_scheduler_root_unregister_handle(load_ptr(handles, index * 8))
        index = index + 1


def _raise_duplicate_keyword(callable_obj, key) -> None:
    """Build and raise the entire duplicate error inside its rooted frame.

    The defining callable supplies its identity, never the importing caller.
    Objects without a qualname use their repr, like CPython's function display.
    """
    slots = stack_alloc(56)
    tokens = stack_alloc(56)
    handles = stack_alloc(56)
    count: int = _call_diagnostic_open(slots, tokens, handles)
    status: int = -1
    if count == 7:
        if ptr_is_null(callable_obj):
            store_ptr(slots, 24, _call_lit(cstr("")))
            status = _call_diagnostic_adopt(slots, tokens, 3)
        else:
            status = _call_diagnostic_prefix(slots, tokens, callable_obj)
        if status == 0:
            status = _call_diagnostic_append_literal(slots, tokens, cstr("got multiple values for keyword argument '"))
        if status == 0:
            store_ptr(slots, 32, py_obj_str(key))
            status = _call_diagnostic_adopt(slots, tokens, 4)
        if status == 0:
            status = _call_diagnostic_append(slots, tokens)
        if status == 0:
            status = _call_diagnostic_append_literal(slots, tokens, cstr("'"))
        if status == 0:
            py_raise_owned(py_exc_new(3, py_str_utf8(load_ptr(slots, 24))))
    else:
        _require_result(null(), cstr("call diagnostic"), cstr("diagnostic root registration failed"))
    _call_diagnostic_close(slots, tokens, handles, count)


def _check_keyword_unique(out, key, callable_obj) -> int:
    """Check before fetching a mapping value; non-string keys bind later."""
    if py_dict_contains(out, key) != 0:
        _raise_duplicate_keyword(callable_obj, key)
        return -1
    if py_err_occurred() != 0:
        return -1
    return 0


@c_abi_export("py_call_validate_kwargs")
def py_call_validate_kwargs(kwargs) -> int:
    """Validate once operands finish, before any callable's binding errors."""
    if _is_none(kwargs):
        return 0
    if _type_of(kwargs) != PY_TYPE_DICT:
        py_raise_owned(py_exc_new(3, cstr("call keyword arguments must be a dict")))
        return -1
    # The caller keeps the dictionary address leased. Inspect its owning key
    # slots only while the graph transaction prevents moves and table changes;
    # no allocating key snapshot, raw child lifetime, or user callback escapes.
    valid: int = 1
    pcc_py_gc_minor_graph_lock()
    entries = load_ptr(kwargs, PYDICTOBJECT_ENTRIES_OFFSET)
    count: int = load_i64(kwargs, PYDICTOBJECT_ENTRIES_USED_OFFSET)
    index: int = 0
    while index < count and valid != 0:
        key = pcc_gc_resolve_root_slot_unlocked(ptr_add(entries, index * DICTENTRY_SIZE + DICTENTRY_KEY_OFFSET), 0)
        if not ptr_is_null(key):
            if _type_of(key) != PY_TYPE_STR:
                valid = 0
        index = index + 1
    pcc_py_gc_minor_graph_unlock()
    if valid == 0:
        py_raise_owned(py_exc_new(3, cstr("keywords must be strings")))
        return -1
    return 0


@c_abi_export("py_call_merge_kwargs_unique")
def py_call_merge_kwargs_unique(base_kwargs, star_kwargs):
    """Compatibility merge without callable-qualified error context."""
    return py_call_merge_kwargs_for_call(base_kwargs, star_kwargs, null())


@c_abi_export("py_call_merge_kwargs_for_call")
def py_call_merge_kwargs_for_call(base_kwargs, star_kwargs, callable_obj):
    """``f(**a, **b)``: merge ``b`` into ``a`` with the identity of ``f``.

    A key already present is a TypeError, as in CPython (``py_call_merge_kwargs``
    updates silently). Non-string keys survive merging until call binding so
    later operands still execute. A non-mapping operand stops evaluation with
    CPython 3.15's unqualified diagnostic. NEW reference, NULL on failure.
    """
    out = _dict_clone(base_kwargs)
    if ptr_is_null(out):
        return null()
    if _is_none(star_kwargs):
        text = _call_lit(cstr("Value after ** must be a mapping, not NoneType"))
        _raise_type_error_text(text)
        py_decref(out)
        return null()
    keys = null()
    if _type_of(star_kwargs) == PY_TYPE_DICT:
        keys = py_dict_keys(star_kwargs)
    else:
        keys_method = py_obj_getattr(star_kwargs, cstr("keys"))
        if ptr_is_null(keys_method):
            if py_err_occurred() != 0:
                if py_exc_matches(py_current_exception(), py_exc_builtin_class(6)) == 0:
                    py_decref(out)
                    return null()
            py_clear_exception()
            text = _call_lit(cstr("Value after ** must be a mapping, not "))
            text = _cat_owned(text, py_obj_type_name(star_kwargs))
            _raise_type_error_text(text)
            py_decref(out)
            return null()
        no_args = py_tuple_new(0)
        if ptr_is_null(no_args):
            py_decref(keys_method)
            py_decref(out)
            return null()
        keys = py_obj_call(keys_method, no_args, null())
        py_decref(no_args)
        py_decref(keys_method)
    if ptr_is_null(keys):
        py_decref(out)
        return null()
    it = py_obj_iter(keys)
    py_decref(keys)
    if ptr_is_null(it):
        py_decref(out)
        return null()
    while True:
        key = py_obj_next(it)
        if ptr_is_null(key):
            py_decref(it)
            if py_err_occurred() != 0:
                if py_exc_matches(py_current_exception(), py_exc_builtin_class(8)) == 0:
                    py_decref(out)
                    return null()
                py_clear_exception()
            return out
        if _check_keyword_unique(out, key, callable_obj) != 0:
            py_decref(key)
            py_decref(it)
            py_decref(out)
            return null()
        value = py_obj_getitem(star_kwargs, key)
        if ptr_is_null(value):
            py_decref(key)
            py_decref(it)
            py_decref(out)
            return null()
        py_dict_set(out, key, value)
        py_decref(key)
        py_decref(value)
        if py_err_occurred() != 0:
            py_decref(it)
            py_decref(out)
            return null()


@c_abi_export("py_obj_call_splat")
def py_obj_call_splat(callable_obj, base_args, star_args, base_kwargs, star_kwargs):
    args = py_call_merge_posargs(base_args, star_args)
    if ptr_is_null(args):
        return _require_result(
            null(),
            cstr("py_call_merge_posargs"),
            cstr("call splat could not merge positional arguments"),
        )
    kwargs = py_call_merge_kwargs(base_kwargs, star_kwargs)
    if ptr_is_null(kwargs):
        _require_result(
            null(),
            cstr("py_call_merge_kwargs"),
            cstr("call splat could not merge keyword arguments"),
        )
        py_decref(args)
        return null()
    out = py_obj_call(callable_obj, args, kwargs)
    if ptr_is_null(out):
        _require_result(
            null(),
            cstr("py_obj_call"),
            cstr("call splat callee returned NULL without setting an exception"),
        )
    py_decref(args)
    py_decref(kwargs)
    return out


# Syntax-level iterable diagnostics use capability, never exception-text
# rewriting. Inputs remain address-leased by the slot-producing caller.
from pcc.runtime.py.py_abi_constants import PY_TYPE_BYTEARRAY, PY_TYPE_BYTES, PY_TYPE_FILE, PY_TYPE_GEN, PY_TYPE_ITER, PY_TYPE_MEMORYVIEW, PY_TYPE_SET

py_obj_special_present = extern("py_obj_special_present", (c_ptr, c_ptr), c_int64)
_call_star_cext_type_tag = extern("pcc_capi_is_cext_type_tag", (c_int64,), c_int64)


@c_abi_export("py_call_require_star_iterable")
def py_call_require_star_iterable(value) -> int:
    """Check a * operand's capability while its caller's counted lease lives.

    Presence includes None and descriptors: their ordinary iterator dispatch
    owns any eventual error. Opaque extension iteration remains in its C-API
    owner. Only a proven absent protocol gets Python's syntax-level error.
    """
    tag: int = _type_of(value)
    if (tag == PY_TYPE_LIST or tag == PY_TYPE_TUPLE or tag == PY_TYPE_STR
            or tag == PY_TYPE_DICT or tag == PY_TYPE_SET or tag == PY_TYPE_BYTES
            or tag == PY_TYPE_BYTEARRAY or tag == PY_TYPE_MEMORYVIEW
            or tag == PY_TYPE_ITER or tag == PY_TYPE_GEN or tag == PY_TYPE_FILE):
        return 0
    if _call_star_cext_type_tag(tag) != 0:
        return 0
    if py_obj_special_present(value, cstr("__iter__")) != 0:
        return 0
    if py_obj_special_present(value, cstr("__getitem__")) != 0:
        return 0
    slots = stack_alloc(56)
    tokens = stack_alloc(56)
    handles = stack_alloc(56)
    count: int = _call_diagnostic_open(slots, tokens, handles)
    status: int = -1
    if count == 7:
        store_ptr(slots, 24, _call_lit(cstr("Value after * must be an iterable, not ")))
        status = _call_diagnostic_adopt(slots, tokens, 3)
        if status == 0:
            store_ptr(slots, 32, py_obj_type_name(value))
            status = _call_diagnostic_adopt(slots, tokens, 4)
        if status == 0:
            status = _call_diagnostic_append(slots, tokens)
        if status == 0:
            py_raise_owned(py_exc_new(3, py_str_utf8(load_ptr(slots, 24))))
    else:
        _require_result(null(), cstr("call star diagnostic"), cstr("star diagnostic root registration failed"))
    _call_diagnostic_close(slots, tokens, handles, count)
    return -1
