"""Phase 4c.1: pcc-Python replacement for py_runtime/src/py_tuple.c.

Each public entry point is decorated with ``@c_abi_export(<name>)`` so
pcc emits the unmangled C-ABI symbol name directly. This lets the
.o produced from this module drop-in replace py_tuple.o at link time.

Layout mirrors py_tuple.c exactly:

    typedef struct {
        PyObjectHeader h;          // 16 bytes (i64 refcount, i32 tag, i32 flags)
        int64_t        len;        // 8 bytes
        PyObject      *items[];    // flexible array of owned refs
    } PyTupleObject;

Offsets: 0 refcount, 8 type_tag, 12 flags, 16 len, 24 items[0...]

Layout constants are imported from the generated ABI module.  Closed-world
native constant exports keep these as compile-time values in library objects;
there is no module-init dependency.
"""

__pcc_runtime_port__ = True

from pcc.extern import extern, c_abi_export, c_ptr, c_int32, c_int64, c_void
from pcc.py_runtime.py.py_abi_constants import (
    PYLISTOBJECT_ITEMS_OFFSET,
    PYOBJECTHEADER_FLAGS_OFFSET,
    PYOBJECTHEADER_TYPE_TAG_OFFSET,
    PYTUPLEOBJECT_ITEMS_OFFSET,
    PYTUPLEOBJECT_LEN_OFFSET,
    PYTUPLEOBJECT_SIZE,
    PY_FLAG_GC_TRACKED,
    PY_TYPE_CLASS,
    PY_TYPE_COROUTINE,
    PY_TYPE_DICT,
    PY_TYPE_EXC,
    PY_TYPE_FUNC,
    PY_TYPE_GEN,
    PY_TYPE_INSTANCE,
    PY_TYPE_ITER,
    PY_TYPE_LIST,
    PY_TYPE_MEMORYVIEW,
    PY_TYPE_SET,
    PY_TYPE_TUPLE,
    PY_TYPE_USER,
    PY_TYPE_WEAKREF,
)
from pcc.unsafe import (
    cstr,
    global_addr,
    is_tagged_int,
    load_i32,
    load_i64,
    load_ptr,
    malloc,
    memset,
    null,
    ptr_add,
    ptr_is_null,
    ptr_to_int,
    store_i32,
    store_i64,
    store_ptr,
    stack_alloc,
    untag_int,
)


py_incref         = extern("py_incref",        (c_ptr,),                             c_void)
py_decref         = extern("py_decref",        (c_ptr,),                             c_void)
py_list_len       = extern("py_list_len",      (c_ptr,),                             c_int64)
py_list_get       = extern("py_list_get",      (c_ptr, c_int64),                     c_ptr)
py_obj_len        = extern("py_obj_len",       (c_ptr,),                             c_int64)
py_obj_getitem_i64 = extern("py_obj_getitem_i64", (c_ptr, c_int64),                  c_ptr)
py_obj_eq         = extern("py_obj_eq",        (c_ptr, c_ptr),                       c_int64)
py_int_value_i64  = extern("py_int_value_i64", (c_ptr,),                             c_int64)
py_int_to_i64     = extern("py_int_to_i64",    (c_ptr, c_ptr),                      c_int64)
py_err_occurred   = extern("py_err_occurred",  (),                                   c_int64)
py_gc_track       = extern("py_gc_track",       (c_ptr,),                             c_void)
py_exc_new        = extern("py_exc_new",        (c_int64, c_ptr),                     c_ptr)
py_raise          = extern("py_raise",          (c_ptr,),                             c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
pcc_gc_store_ptr  = extern("pcc_gc_store_ptr",  (c_ptr, c_ptr, c_ptr),                c_void)
pcc_gc_load_ptr   = extern("pcc_gc_load_ptr",   (c_ptr, c_ptr),                       c_ptr)
pcc_gc_backend = extern("pcc_gc_backend", (), c_int64)
pcc_gc_scheduler_root_register_handle = extern(
    "pcc_gc_scheduler_root_register_handle", (c_ptr,), c_ptr
)
pcc_gc_scheduler_root_unregister_handle = extern(
    "pcc_gc_scheduler_root_unregister_handle", (c_ptr,), c_void
)
pcc_gc_alloc      = extern("pcc_gc_alloc",      (c_int64, c_int32, c_int32),          c_ptr)
pcc_gc_publish_initialized = extern(
    "pcc_gc_publish_initialized", (c_ptr,), c_void
)
_pcc_debug_bad_incref = extern("pcc_debug_bad_incref", (c_ptr, c_int32), c_void)
getenv = extern("pcc_platform_getenv", (c_ptr,), c_ptr)


py_list_new = extern("py_list_new", (c_int64,), c_ptr)
py_list_append = extern("py_list_append", (c_ptr, c_ptr), c_void)
py_obj_iter = extern("py_obj_iter", (c_ptr,), c_ptr)
py_obj_next = extern("py_obj_next", (c_ptr,), c_ptr)
py_current_exception = extern("py_current_exception", (), c_ptr)
py_exc_builtin_class = extern("py_exc_builtin_class", (c_int64,), c_ptr)
py_exc_matches = extern("py_exc_matches", (c_ptr, c_ptr), c_int64)
py_clear_exception = extern("py_clear_exception", (), c_void)
pcc_gc_store_root = extern("pcc_gc_store_root", (c_ptr, c_ptr), c_void)
pcc_gc_store_root_take = extern("pcc_gc_store_root_take", (c_ptr, c_ptr), c_void)


def _debug_bad_container(o, code: int) -> None:
    if ptr_is_null(getenv(cstr("PCC_DEBUG_RUNTIME"))) == 0:
        _pcc_debug_bad_incref(o, code)


pcc_gc_pointer_is_managed = extern(
    "pcc_gc_pointer_is_managed", (c_ptr,), c_int64
)


def _ptr_can_have_header(o) -> bool:
    return pcc_gc_pointer_is_managed(o) != 0


def _ptr_is_tuple(o) -> bool:
    if not _ptr_can_have_header(o):
        return False
    return load_i32(o, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_TUPLE


def _tuple_is_sane(tuple_ptr, code: int) -> bool:
    if ptr_is_null(tuple_ptr) != 0:
        return False
    if not _ptr_is_tuple(tuple_ptr):
        _debug_bad_container(tuple_ptr, code)
        return False
    length: int = load_i64(tuple_ptr, PYTUPLEOBJECT_LEN_OFFSET)
    if length < 0:
        _debug_bad_container(tuple_ptr, code)
        return False
    if length > 134217728:
        _debug_bad_container(tuple_ptr, code)
        return False
    return True


def _tuple_item_can_participate_in_cycle(item, depth: int = 0) -> int:
    if ptr_is_null(item) != 0:
        return 0
    if is_tagged_int(item) != 0:
        return 0
    if not _ptr_can_have_header(item):
        return 0
    if depth < 0:
        depth = 0
    if depth > 64:
        return 0

    tag: int = load_i32(item, PYOBJECTHEADER_TYPE_TAG_OFFSET)
    if tag >= PY_TYPE_USER:
        return 1
    if tag == PY_TYPE_LIST or tag == PY_TYPE_DICT or tag == PY_TYPE_SET or tag == PY_TYPE_FUNC:
        return 1
    if tag == PY_TYPE_CLASS or tag == PY_TYPE_INSTANCE or tag == PY_TYPE_EXC or tag == PY_TYPE_ITER or tag == PY_TYPE_GEN:
        return 1
    if tag == PY_TYPE_COROUTINE or tag == PY_TYPE_MEMORYVIEW or tag == PY_TYPE_WEAKREF:
        return 1
    if tag == PY_TYPE_TUPLE:
        length: int = load_i64(item, PYTUPLEOBJECT_LEN_OFFSET)
        i: int = 0
        while i < length:
            child = pcc_gc_load_ptr(item, ptr_add(item, PYTUPLEOBJECT_ITEMS_OFFSET + i * 8))
            if _tuple_item_can_participate_in_cycle(child, depth + 1) != 0:
                return 1
            i = i + 1
        return 0
    return 0


@c_abi_export("py_tuple_new")
def py_tuple_new(n: int):
    if n < 0:
        n = 0
    if n > 134217728:
        _debug_bad_container(null(), -130)
        return null()
    bytes_total: int = PYTUPLEOBJECT_SIZE + n * 8          # OFFSET_ITEMS + n * SIZEOF_PTR
    t = pcc_gc_alloc(bytes_total, PY_TYPE_TUPLE, 0)
    if ptr_is_null(t):
        return null()
    store_i64(t, PYTUPLEOBJECT_LEN_OFFSET, n)             # len = n
    if n > 0:
        items_ptr = ptr_add(t, PYTUPLEOBJECT_ITEMS_OFFSET)
        memset(items_ptr, 0, n * 8)
    else:
        pcc_gc_publish_initialized(t)
    return t


@c_abi_export("py_tuple_from_list")
def py_tuple_from_list(lst):
    # New tuple from a pcc list's elements. Mirrors py_tuple_from_list in
    # py_tuple.c; used by the dynamic-call lowering to normalize mixed
    # ``f(a, *rest)`` argument lists to the tuple the callable ABI requires.
    if ptr_is_null(lst):
        return null()
    n: int = py_list_len(lst)
    out = py_tuple_new(n)
    if ptr_is_null(out):
        return null()
    i: int = 0
    while i < n:
        v = py_list_get(lst, i)        # new ref
        py_tuple_set_item(out, i, v)   # increfs
        py_decref(v)
        i = i + 1
    return out


def _tuple_collect_iterator(slots) -> int:
    # Slots own source, iterator, accumulated list, current item and output.
    pcc_gc_store_root_take(ptr_add(slots, 8), py_obj_iter(pcc_gc_load_ptr(null(), slots)))
    if ptr_is_null(load_ptr(slots, 8)):
        return 0
    pcc_gc_store_root_take(ptr_add(slots, 16), py_list_new(0))
    if ptr_is_null(load_ptr(slots, 16)):
        py_raise_owned(py_exc_new(19, cstr("tuple: out of memory")))
        return 0
    while True:
        item = py_obj_next(pcc_gc_load_ptr(null(), ptr_add(slots, 8)))
        if ptr_is_null(item):
            if py_err_occurred() != 0:
                if py_exc_matches(py_current_exception(), py_exc_builtin_class(8)) == 0:
                    return 0
                py_clear_exception()
            break
        pcc_gc_store_root_take(ptr_add(slots, 24), item)
        py_list_append(pcc_gc_load_ptr(null(), ptr_add(slots, 16)),
                       pcc_gc_load_ptr(null(), ptr_add(slots, 24)))
        if py_err_occurred() != 0:
            return 0
        pcc_gc_store_root(ptr_add(slots, 24), null())
    n: int = py_list_len(pcc_gc_load_ptr(null(), ptr_add(slots, 16)))
    pcc_gc_store_root_take(ptr_add(slots, 32), py_tuple_new(n))
    if ptr_is_null(load_ptr(slots, 32)):
        py_raise_owned(py_exc_new(19, cstr("tuple: out of memory")))
        return 0
    i: int = 0
    while i < n:
        pcc_gc_store_root_take(ptr_add(slots, 24),
                               py_list_get(pcc_gc_load_ptr(null(), ptr_add(slots, 16)), i))
        py_tuple_set_item(pcc_gc_load_ptr(null(), ptr_add(slots, 32)), i,
                          pcc_gc_load_ptr(null(), ptr_add(slots, 24)))
        if py_err_occurred() != 0:
            return 0
        pcc_gc_store_root(ptr_add(slots, 24), null())
        i = i + 1
    return 1


def _tuple_from_iterable(seq):
    # Explicit roots survive collections inside __iter__/__next__, including
    # moving collectors. A sixth root preserves errors during owner cleanup.
    slots = stack_alloc(48)
    handles = stack_alloc(48)
    memset(slots, 0, 48)
    memset(handles, 0, 48)
    count: int = 0
    while count < 6:
        handle = pcc_gc_scheduler_root_register_handle(ptr_add(slots, count * 8))
        if ptr_is_null(handle):
            while count > 0:
                count = count - 1
                pcc_gc_scheduler_root_unregister_handle(load_ptr(handles, count * 8))
            py_raise_owned(py_exc_new(19, cstr("tuple: out of memory")))
            return null()
        store_ptr(handles, count * 8, handle)
        count = count + 1
    pcc_gc_store_root(slots, seq)
    success: int = _tuple_collect_iterator(slots)
    if success == 0:
        pcc_gc_store_root(ptr_add(slots, 40), py_current_exception())
        py_clear_exception()
    # Release other owners while the result/error is still rooted.
    i: int = 0
    while i < 4:
        pcc_gc_store_root(ptr_add(slots, i * 8), null())
        i = i + 1
    result = null()
    if success != 0:
        result = pcc_gc_load_ptr(null(), ptr_add(slots, 32))
        py_incref(result)
    else:
        py_raise(pcc_gc_load_ptr(null(), ptr_add(slots, 40)))
    pcc_gc_store_root(ptr_add(slots, 32), null())
    pcc_gc_store_root(ptr_add(slots, 40), null())
    i = 0
    while i < 6:
        pcc_gc_scheduler_root_unregister_handle(load_ptr(handles, i * 8))
        i = i + 1
    return result


@c_abi_export("py_tuple_from_splat")
def py_tuple_from_splat(seq):
    if ptr_is_null(seq):
        return null()
    if not _ptr_can_have_header(seq):
        return null()

    tag: int = load_i32(seq, PYOBJECTHEADER_TYPE_TAG_OFFSET)
    n: int = -1
    if tag == PY_TYPE_TUPLE:
        n = load_i64(seq, PYTUPLEOBJECT_LEN_OFFSET)
    elif tag == PY_TYPE_LIST:
        n = py_list_len(seq)
    else:
        return _tuple_from_iterable(seq)
    if n < 0:
        py_raise_owned(py_exc_new(6, cstr("tuple() argument is not iterable")))  # PY_EXC_TYPEERROR
        return null()

    out = py_tuple_new(n)
    if ptr_is_null(out):
        return null()

    i: int = 0
    if tag == PY_TYPE_TUPLE:
        while i < n:
            v = pcc_gc_load_ptr(seq, ptr_add(seq, PYTUPLEOBJECT_ITEMS_OFFSET + i * 8))
            py_tuple_set_item(out, i, v)
            i = i + 1
        return out
    if tag == PY_TYPE_LIST:
        items = load_ptr(seq, PYLISTOBJECT_ITEMS_OFFSET)
        while i < n:
            v = pcc_gc_load_ptr(seq, ptr_add(items, i * 8))
            py_tuple_set_item(out, i, v)
            i = i + 1
        return out

    return out


@c_abi_export("py_tuple_set_item")
def py_tuple_set_item(tuple_ptr, i: int, item) -> None:
    if ptr_is_null(tuple_ptr) != 0:
        return
    tuple_len: int = load_i64(tuple_ptr, PYTUPLEOBJECT_LEN_OFFSET)
    if i < 0 or i >= tuple_len:
        return
    slot_offset: int = PYTUPLEOBJECT_ITEMS_OFFSET + i * 8
    slot = ptr_add(tuple_ptr, slot_offset)
    if (
        load_i32(global_addr("pcc_gc_config_initialized"), 0) != 0
        and load_i32(global_addr("pcc_gc_backend_selected"), 0) == 0
    ):
        py_incref(item)
        store_ptr(slot, 0, item)
    else:
        pcc_gc_store_ptr(tuple_ptr, slot, item)
    if _tuple_item_can_participate_in_cycle(item) != 0:
        flags = load_i32(tuple_ptr, PYOBJECTHEADER_FLAGS_OFFSET)
        if (flags & PY_FLAG_GC_TRACKED) == 0:
            py_gc_track(tuple_ptr)
    # Only colored relocation waits for complete payload publication.
    # Keep the store/ownership/tracking work above for every collector.
    if pcc_gc_backend() != 4:
        return
    complete: int = 1
    slot_index: int = 0
    while slot_index < tuple_len:
        if ptr_is_null(pcc_gc_load_ptr(
            tuple_ptr,
            ptr_add(tuple_ptr, PYTUPLEOBJECT_ITEMS_OFFSET + slot_index * 8),
        )) != 0:
            complete = 0
            slot_index = tuple_len
        else:
            slot_index = slot_index + 1
    if complete != 0:
        pcc_gc_publish_initialized(tuple_ptr)


@c_abi_export("py_tuple_get")
def py_tuple_get(tuple_ptr, i: int):
    if ptr_is_null(tuple_ptr) != 0:
        return null()
    tuple_len: int = load_i64(tuple_ptr, PYTUPLEOBJECT_LEN_OFFSET)
    if i < 0:
        i = i + tuple_len
    if i < 0 or i >= tuple_len:
        # Non-raising: internal callers rely on the silent-NULL contract.
        # User-level t[i] subscripts route to py_tuple_getitem, which raises.
        return null()
    slot_offset: int = PYTUPLEOBJECT_ITEMS_OFFSET + i * 8
    v = pcc_gc_load_ptr(tuple_ptr, ptr_add(tuple_ptr, slot_offset))
    py_incref(v)
    return v


@c_abi_export("py_tuple_get_known")
def py_tuple_get_known(tuple_ptr, i: int):
    # Adapter-only helper: generated function-call ABI args are known tuples.
    # Keep py_tuple_get's owned-ref result but skip its defensive shape checks.
    if ptr_is_null(tuple_ptr) != 0:
        return null()
    tuple_len: int = load_i64(tuple_ptr, PYTUPLEOBJECT_LEN_OFFSET)
    if i < 0:
        i = i + tuple_len
    if i < 0 or i >= tuple_len:
        return null()
    slot_offset: int = PYTUPLEOBJECT_ITEMS_OFFSET + i * 8
    v = pcc_gc_load_ptr(tuple_ptr, ptr_add(tuple_ptr, slot_offset))
    py_incref(v)
    return v


@c_abi_export("py_tuple_getitem")
def py_tuple_getitem(tuple_ptr, i: int):
    # t[i] subscript: like py_tuple_get but raises IndexError on out-of-range so
    # try/except can catch it. Mirrors py_tuple_getitem in py_tuple.c; py_tuple_get
    # stays non-raising for other callers. Negative indices normalize.
    if ptr_is_null(tuple_ptr) != 0:
        return null()
    tuple_len: int = load_i64(tuple_ptr, PYTUPLEOBJECT_LEN_OFFSET)
    if i < 0:
        i = i + tuple_len
    if i < 0 or i >= tuple_len:
        py_raise_owned(py_exc_new(5, cstr("tuple index out of range")))  # PY_EXC_INDEXERROR
        return null()
    slot_offset: int = PYTUPLEOBJECT_ITEMS_OFFSET + i * 8
    v = pcc_gc_load_ptr(tuple_ptr, ptr_add(tuple_ptr, slot_offset))
    py_incref(v)
    return v


@c_abi_export("py_tuple_len")
def py_tuple_len(tuple_ptr) -> int:
    if ptr_is_null(tuple_ptr) != 0:
        return 0
    return load_i64(tuple_ptr, PYTUPLEOBJECT_LEN_OFFSET)


def _tuple_method_prepare_root(slot, value, backend: int):
    store_ptr(slot, 0, value)
    if (
        (backend == 3 or backend == 4)
        and ptr_is_null(value) == 0
        and is_tagged_int(value) == 0
    ):
        handle = pcc_gc_scheduler_root_register_handle(slot)
        if ptr_is_null(handle) == 0:
            store_ptr(slot, 0, pcc_gc_load_ptr(null(), slot))
        return handle
    return null()


def _tuple_method_root_failed(value, backend: int, handle) -> int:
    if backend != 3 and backend != 4:
        return 0
    if ptr_is_null(value) != 0 or is_tagged_int(value) != 0:
        return 0
    return ptr_is_null(handle)


def _tuple_method_reload_root(slot, handle):
    value = load_ptr(slot, 0)
    if ptr_is_null(handle) == 0:
        value = pcc_gc_load_ptr(null(), slot)
        store_ptr(slot, 0, value)
    return value


def _tuple_method_finish_root(handle) -> None:
    if ptr_is_null(handle) == 0:
        pcc_gc_scheduler_root_unregister_handle(handle)


def _tuple_method_scan(tuple_ptr, item, start, stop, want_first: int) -> int:
    if ptr_is_null(tuple_ptr) != 0:
        return -1 if want_first != 0 else 0
    backend: int = pcc_gc_backend()
    tuple_slot = stack_alloc(8)
    query_slot = stack_alloc(8)
    element_slot = stack_alloc(8)
    tuple_handle = _tuple_method_prepare_root(tuple_slot, tuple_ptr, backend)
    if _tuple_method_root_failed(tuple_ptr, backend, tuple_handle) != 0:
        return -1 if want_first != 0 else 0
    query_handle = _tuple_method_prepare_root(query_slot, item, backend)
    if _tuple_method_root_failed(item, backend, query_handle) != 0:
        _tuple_method_finish_root(tuple_handle)
        return -1 if want_first != 0 else 0

    tuple_ptr = _tuple_method_reload_root(tuple_slot, tuple_handle)
    length: int = py_tuple_len(tuple_ptr)
    lo: int = 0
    hi: int = length
    if ptr_is_null(start) == 0 or ptr_is_null(stop) == 0:
        overflow = stack_alloc(4)
        if ptr_is_null(start) == 0:
            store_i32(overflow, 0, 0)
            lo = py_int_to_i64(start, overflow)
            if load_i32(overflow, 0) != 0:
                lo = 0
        if ptr_is_null(stop) == 0:
            store_i32(overflow, 0, 0)
            hi = py_int_to_i64(stop, overflow)
            if load_i32(overflow, 0) != 0:
                hi = length
        if lo < 0:
            lo = lo + length
            if lo < 0:
                lo = 0
        if hi < 0:
            hi = hi + length
            if hi < 0:
                hi = 0
        if hi > length:
            hi = length

    count: int = 0
    i: int = lo
    while i < hi:
        tuple_ptr = _tuple_method_reload_root(tuple_slot, tuple_handle)
        item = _tuple_method_reload_root(query_slot, query_handle)
        element = py_tuple_get(tuple_ptr, i)
        store_ptr(element_slot, 0, element)
        element_handle = _tuple_method_prepare_root(
            element_slot, element, backend
        )
        if _tuple_method_root_failed(element, backend, element_handle) != 0:
            py_decref(element)
            break
        equal: int = py_obj_eq(element, item)
        tuple_ptr = _tuple_method_reload_root(tuple_slot, tuple_handle)
        item = _tuple_method_reload_root(query_slot, query_handle)
        element = _tuple_method_reload_root(element_slot, element_handle)
        _tuple_method_finish_root(element_handle)
        py_decref(element)
        if py_err_occurred() != 0:
            _tuple_method_finish_root(query_handle)
            _tuple_method_finish_root(tuple_handle)
            return -1 if want_first != 0 else 0
        if equal != 0:
            if want_first != 0:
                _tuple_method_finish_root(query_handle)
                _tuple_method_finish_root(tuple_handle)
                return i
            count = count + 1
        i = i + 1
    _tuple_method_finish_root(query_handle)
    _tuple_method_finish_root(tuple_handle)
    if want_first != 0:
        return -1
    return count


@c_abi_export("py_tuple_count")
def py_tuple_count(tuple_ptr, item) -> int:
    return _tuple_method_scan(tuple_ptr, item, null(), null(), 0)


@c_abi_export("py_tuple_index")
def py_tuple_index(tuple_ptr, item) -> int:
    result: int = _tuple_method_scan(tuple_ptr, item, null(), null(), 1)
    if result >= 0:
        return result
    if py_err_occurred() != 0:
        return -1
    py_raise_owned(py_exc_new(2, cstr("tuple.index(x): x not in tuple")))
    return -1


@c_abi_export("py_tuple_index_range")
def py_tuple_index_range(tuple_ptr, item, start, stop) -> int:
    result: int = _tuple_method_scan(tuple_ptr, item, start, stop, 1)
    if result >= 0:
        return result
    if py_err_occurred() != 0:
        return -1
    py_raise_owned(py_exc_new(2, cstr("tuple.index(x): x not in tuple")))
    return -1


@c_abi_export("py_tuple_concat")
def py_tuple_concat(a, b):
    if not _tuple_is_sane(a, -135):
        return null()
    if not _tuple_is_sane(b, -136):
        return null()
    la: int = load_i64(a, PYTUPLEOBJECT_LEN_OFFSET)
    lb: int = load_i64(b, PYTUPLEOBJECT_LEN_OFFSET)
    out = py_tuple_new(la + lb)
    i: int = 0
    while i < la:
        v = pcc_gc_load_ptr(a, ptr_add(a, PYTUPLEOBJECT_ITEMS_OFFSET + i * 8))
        py_tuple_set_item(out, i, v)
        i = i + 1
    j: int = 0
    while j < lb:
        v = pcc_gc_load_ptr(b, ptr_add(b, PYTUPLEOBJECT_ITEMS_OFFSET + j * 8))
        py_tuple_set_item(out, la + j, v)
        j = j + 1
    return out


@c_abi_export("py_tuple_repeat")
def py_tuple_repeat(tuple_ptr, count: int):
    if not _tuple_is_sane(tuple_ptr, -137):
        return null()
    length: int = load_i64(tuple_ptr, PYTUPLEOBJECT_LEN_OFFSET)
    repeats: int = count
    if repeats < 0:
        repeats = 0
    out = py_tuple_new(length * repeats)
    if ptr_is_null(out):
        return null()
    dst: int = 0
    k: int = 0
    while k < repeats:
        i: int = 0
        while i < length:
            v = pcc_gc_load_ptr(tuple_ptr, ptr_add(tuple_ptr, PYTUPLEOBJECT_ITEMS_OFFSET + i * 8))
            py_tuple_set_item(out, dst, v)
            dst = dst + 1
            i = i + 1
        k = k + 1
    return out
