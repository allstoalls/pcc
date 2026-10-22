"""Rooted multiplication and builtin sequence repetition.

The public entry consumes owning slot addresses, not borrowed raw operands.
Legacy numeric/user dispatch keeps its existing semantics. Builtin sequences
use a checked index and a rooted snapshot before allocating their output.
"""

__pcc_runtime_port__ = True

from pcc.extern import (
    c_abi_export,
    c_int64,
    c_ptr,
    c_void,
    extern,
)
from pcc.runtime.py.py_abi_constants import (
    C_POINTER_SIZE,
    PYBYTESOBJECT_BYTE_LEN_OFFSET,
    PYBYTESOBJECT_DATA_OFFSET,
    PYLISTOBJECT_LENGTH_OFFSET,
    PYOBJECTHEADER_TYPE_TAG_OFFSET,
    PYSTROBJECT_BYTE_LEN_OFFSET,
    PYSTROBJECT_CP_LEN_OFFSET,
    PYSTROBJECT_DATA_OFFSET,
    PYTUPLEOBJECT_ITEMS_OFFSET,
    PYTUPLEOBJECT_LEN_OFFSET,
    PYTUPLEOBJECT_SIZE,
    PY_TYPE_BOOL,
    PY_TYPE_BYTEARRAY,
    PY_TYPE_BYTES,
    PY_TYPE_INT,
    PY_TYPE_LIST,
    PY_TYPE_STR,
    PY_TYPE_TUPLE,
)
from pcc.unsafe import (
    cstr,
    define_global_i32,
    free,
    global_addr,
    global_load_ptr,
    is_tagged_int,
    load_i8,
    load_i32,
    load_i64,
    load_ptr,
    malloc,
    memset,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    stack_alloc,
    store_i8,
    store_i32,
    store_i64,
    store_ptr,
)


pcc_gc_frame_enter = extern("pcc_gc_frame_enter", (c_ptr, c_ptr), c_void)
pcc_gc_frame_leave = extern("pcc_gc_frame_leave", (c_ptr,), c_void)
pcc_gc_root_copy_lease = extern("pcc_gc_root_copy_lease", (c_ptr, c_ptr), c_int64)
pcc_gc_root_move = extern("pcc_gc_root_move", (c_ptr, c_ptr), c_int64)
pcc_gc_foreign_lease_acquire = extern("pcc_gc_foreign_lease_acquire", (c_ptr,), c_int64)
pcc_gc_foreign_lease_release = extern("pcc_gc_foreign_lease_release", (c_ptr, c_int64), c_int64)
pcc_gc_store_root = extern("pcc_gc_store_root", (c_ptr, c_ptr), c_void)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)
pcc_gc_note_slot_write_barrier = extern("pcc_gc_note_slot_write_barrier", (c_ptr, c_ptr, c_ptr), c_void)
pcc_gc_backend = extern("pcc_gc_backend", (), c_int64)
pcc_gc_root_copy_lease_finish = extern("pcc_gc_root_copy_lease_finish", (c_ptr,), c_void)
pcc_gc_publish_initialized = extern("pcc_gc_publish_initialized", (c_ptr,), c_void)
pcc_list_snapshot_commit_slots = extern(
    "pcc_list_snapshot_commit_slots", (c_ptr, c_ptr, c_int64, c_ptr, c_ptr), c_int64
)
pcc_platform_abort = extern("pcc_platform_abort", (), c_void)
py_tls_exc_swap_slot = extern("py_tls_exc_swap_slot", (c_ptr,), c_void)
py_clear_exception = extern("py_clear_exception", (), c_void)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_raise_type_named_error = extern("py_raise_type_named_error", (c_ptr, c_ptr, c_ptr), c_ptr)
py_obj_has_index = extern("py_obj_has_index", (c_ptr,), c_int64)
py_obj_mul = extern("py_obj_mul", (c_ptr, c_ptr), c_ptr)
py_obj_special_call_slots = extern(
    "py_obj_special_call_slots", (c_ptr, c_ptr, c_ptr, c_ptr, c_ptr, c_ptr), c_int64
)
py_index_i64_checked_slots = extern("py_index_i64_checked_slots", (c_ptr,), c_int64)
pcc_capi_is_cext_type_tag = extern("pcc_capi_is_cext_type_tag", (c_int64,), c_int64)
py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_tuple_set_item = extern("py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void)
py_list_new = extern("py_list_new", (c_int64,), c_ptr)
py_list_append = extern("py_list_append", (c_ptr, c_ptr), c_void)
py_str_new = extern("py_str_new", (c_ptr, c_int64), c_ptr)
py_bytes_new = extern("py_bytes_new", (c_ptr, c_int64), c_ptr)
py_bytes_from_obj = extern("py_bytes_from_obj", (c_ptr,), c_ptr)
py_bytearray_from_obj = extern("py_bytearray_from_obj", (c_ptr,), c_ptr)


_MUL_LEFT = 0
_MUL_RIGHT = 1
_MUL_SNAPSHOT = 2
_MUL_ARGS = 3
_MUL_ITEM = 4
_MUL_RESULT = 5
_MUL_TEMP = 6
_MUL_ERROR = 7
_MUL_SLOT_COUNT = 8
_MUL_ROOT_COPY_PLAN_BYTES = 256
_MUL_MAX_INDEX = 9223372036854775807

define_global_i32("pcc_binary_mul_owned_map", _MUL_SLOT_COUNT)


def _mul_type(value) -> int:
    if is_tagged_int(value) != 0:
        return PY_TYPE_INT
    return load_i32(value, PYOBJECTHEADER_TYPE_TAG_OFFSET)


def _mul_error(kind: int, message: c_ptr) -> int:
    if py_err_occurred() == 0:
        py_raise_owned(py_exc_new(kind, message))
    return -1


def _mul_copy(slots: c_ptr, tokens: c_ptr, index: int, source: c_ptr) -> int:
    offset: int = index * C_POINTER_SIZE
    token: int = pcc_gc_root_copy_lease(ptr_add(slots, offset), source)
    if token < 0:
        return _mul_error(7, cstr("multiplication owner copy failed"))
    store_i64(tokens, offset, token)
    return 0


def _mul_adopt(slots: c_ptr, tokens: c_ptr, index: int) -> int:
    slot = ptr_add(slots, index * C_POINTER_SIZE)
    token: int = pcc_gc_foreign_lease_acquire(slot)
    if token < 0:
        return _mul_error(7, cstr("multiplication result lease failed"))
    store_i64(tokens, index * C_POINTER_SIZE, token)
    pcc_py_gc_minor_graph_lock()
    pcc_gc_note_slot_write_barrier(null(), slot, load_ptr(slot, 0))
    pcc_py_gc_minor_graph_unlock()
    if ptr_is_null(load_ptr(slot, 0)) != 0:
        return _mul_error(19, cstr("multiplication allocation failed"))
    return -1 if py_err_occurred() != 0 else 0


def _mul_clear(slots: c_ptr, tokens: c_ptr, index: int) -> None:
    slot = ptr_add(slots, index * C_POINTER_SIZE)
    token: int = load_i64(tokens, index * C_POINTER_SIZE)
    if token >= 0:
        if pcc_gc_foreign_lease_release(slot, token) != 0:
            pcc_platform_abort()
            return
    store_i64(tokens, index * C_POINTER_SIZE, -1)
    pcc_gc_store_root(slot, null())


def _mul_drop(slots: c_ptr, tokens: c_ptr, index: int) -> None:
    error = ptr_add(slots, _MUL_ERROR * C_POINTER_SIZE)
    py_tls_exc_swap_slot(error)
    _mul_clear(slots, tokens, index)
    py_clear_exception()
    py_tls_exc_swap_slot(error)


def _mul_snapshot_list(slots: c_ptr, tokens: c_ptr, source_index: int) -> int:
    """Copy one list version into registered roots before heap publication.

    Scratch storage and its owning/borrowed frames exist before locking.
    Each split copy retains into an empty root and transfers a child lease.
    Heap tuple stores and every plan finish occur after outermost unlock.
    """
    source = ptr_add(slots, source_index * C_POINTER_SIZE)
    snapshot = ptr_add(slots, _MUL_SNAPSHOT * C_POINTER_SIZE)
    error = ptr_add(slots, _MUL_ERROR * C_POINTER_SIZE)
    frame_map = stack_alloc(8)
    borrowed_map = ptr_add(frame_map, 4)
    store_i32(borrowed_map, 0, -1)
    while True:
        length: int = load_i64(load_ptr(source, 0), PYLISTOBJECT_LENGTH_OFFSET)
        # Keep the tuple constructor's admitted length and a signed i32 map.
        if length < 0 or length > 134217728:
            return _mul_error(19, cstr("repeated list is too large"))
        plans = null()
        children = malloc((length + 1) * C_POINTER_SIZE)
        child_tokens = null()
        if length > 0:
            plans = malloc(length * _MUL_ROOT_COPY_PLAN_BYTES)
            child_tokens = malloc(length * C_POINTER_SIZE)
            if ptr_is_null(plans) != 0 or ptr_is_null(children) != 0 or ptr_is_null(child_tokens) != 0:
                free(child_tokens)
                free(children)
                free(plans)
                return _mul_error(19, cstr("list snapshot allocation failed"))
            memset(plans, 0, length * _MUL_ROOT_COPY_PLAN_BYTES)
            memset(child_tokens, 0, length * C_POINTER_SIZE)
        elif ptr_is_null(children) != 0:
            return _mul_error(19, cstr("list snapshot allocation failed"))
        memset(children, 0, (length + 1) * C_POINTER_SIZE)
        borrowed = ptr_add(children, length * C_POINTER_SIZE)
        if length > 0:
            store_i32(frame_map, 0, length)
            pcc_gc_frame_enter(frame_map, children)
        pcc_gc_frame_enter(borrowed_map, borrowed)
        # Configuration is ready before any locked split-copy entry.
        pcc_gc_backend()
        status: int = pcc_list_snapshot_commit_slots(source, children, length, plans, child_tokens)
        py_tls_exc_swap_slot(error)
        index: int = 0
        while index < length:
            pcc_gc_root_copy_lease_finish(ptr_add(plans, index * _MUL_ROOT_COPY_PLAN_BYTES))
            index += 1
        py_clear_exception()
        py_tls_exc_swap_slot(error)
        if status == 0:
            store_ptr(snapshot, 0, py_tuple_new(length))
            if _mul_adopt(slots, tokens, _MUL_SNAPSHOT) != 0:
                status = -1
            index = 0
            while index < length and status == 0:
                py_tuple_set_item(load_ptr(snapshot, 0), index, load_ptr(children, index * C_POINTER_SIZE))
                if py_err_occurred() != 0:
                    status = -1
                index += 1
            if status == 0:
                pcc_gc_publish_initialized(load_ptr(snapshot, 0))
        # Independent roots remain registered through tuple allocation/stores,
        # every partial failure and all potentially reentrant retirement.
        py_tls_exc_swap_slot(error)
        index = length
        while index > 0:
            index -= 1
            child = ptr_add(children, index * C_POINTER_SIZE)
            if pcc_gc_foreign_lease_release(child, load_i64(child_tokens, index * C_POINTER_SIZE)) != 0:
                pcc_platform_abort()
                return -1
            pcc_gc_store_root(child, null())
        pcc_gc_frame_leave(borrowed)
        if length > 0:
            pcc_gc_frame_leave(children)
        free(child_tokens)
        free(children)
        free(plans)
        py_clear_exception()
        py_tls_exc_swap_slot(error)
        if status == 0:
            return 0
        if status != -2:
            return _mul_error(7, cstr("list snapshot ownership failed"))
    return -1


def _mul_sequence_items(slots: c_ptr, tokens: c_ptr, source_index: int, tag: int, count: int) -> int:
    source = ptr_add(slots, source_index * C_POINTER_SIZE)
    snapshot = ptr_add(slots, _MUL_SNAPSHOT * C_POINTER_SIZE)
    result = ptr_add(slots, _MUL_RESULT * C_POINTER_SIZE)
    item = ptr_add(slots, _MUL_ITEM * C_POINTER_SIZE)
    length: int = 0
    if count > 0:
        if tag == PY_TYPE_LIST:
            if _mul_snapshot_list(slots, tokens, source_index) != 0:
                return -1
        elif _mul_copy(slots, tokens, _MUL_SNAPSHOT, source) != 0:
            return -1
        length = load_i64(load_ptr(snapshot, 0), PYTUPLEOBJECT_LEN_OFFSET)
    if length < 0 or (length > 0 and count > (_MUL_MAX_INDEX - PYTUPLEOBJECT_SIZE) // C_POINTER_SIZE // length):
        return _mul_error(19, cstr("repeated sequence is too large"))
    total: int = length * count if count > 0 else 0
    if tag == PY_TYPE_TUPLE and (count == 1 or load_i64(load_ptr(source, 0), PYTUPLEOBJECT_LEN_OFFSET) == 0):
        return _mul_copy(slots, tokens, _MUL_RESULT, source)
    if tag == PY_TYPE_LIST:
        store_ptr(result, 0, py_list_new(total))
    else:
        store_ptr(result, 0, py_tuple_new(total))
    if _mul_adopt(slots, tokens, _MUL_RESULT) != 0:
        return -1
    position: int = 0
    while position < total:
        source_field = ptr_add(load_ptr(snapshot, 0), PYTUPLEOBJECT_ITEMS_OFFSET + (position % length) * C_POINTER_SIZE)
        if _mul_copy(slots, tokens, _MUL_ITEM, source_field) != 0:
            return -1
        if tag == PY_TYPE_LIST:
            py_list_append(load_ptr(result, 0), load_ptr(item, 0))
        else:
            py_tuple_set_item(load_ptr(result, 0), position, load_ptr(item, 0))
        if py_err_occurred() != 0:
            return -1
        _mul_drop(slots, tokens, _MUL_ITEM)
        position = position + 1
    return 0


def _mul_sequence_bytes(slots: c_ptr, tokens: c_ptr, source_index: int, tag: int, count: int) -> int:
    source = ptr_add(slots, source_index * C_POINTER_SIZE)
    result = ptr_add(slots, _MUL_RESULT * C_POINTER_SIZE)
    snapshot = ptr_add(slots, _MUL_SNAPSHOT * C_POINTER_SIZE)
    # Immutable sources need only an independent lease. Mutable bytearray
    # becomes immutable bytes before its payload is traversed repeatedly.
    if tag == PY_TYPE_BYTEARRAY:
        store_ptr(snapshot, 0, py_bytes_from_obj(load_ptr(source, 0)))
        if _mul_adopt(slots, tokens, _MUL_SNAPSHOT) != 0:
            return -1
    elif _mul_copy(slots, tokens, _MUL_SNAPSHOT, source) != 0:
        return -1
    data_offset: int = PYBYTESOBJECT_DATA_OFFSET
    length_offset: int = PYBYTESOBJECT_BYTE_LEN_OFFSET
    if tag == PY_TYPE_STR:
        data_offset = PYSTROBJECT_DATA_OFFSET
        length_offset = PYSTROBJECT_BYTE_LEN_OFFSET
    length: int = load_i64(load_ptr(snapshot, 0), length_offset)
    if length < 0:
        return _mul_error(7, cstr("invalid sequence length"))
    if count < 0:
        count = 0
    if length > 0 and count > _MUL_MAX_INDEX // length:
        return _mul_error(19 if tag == PY_TYPE_BYTEARRAY else 15, cstr("repeated sequence is too large"))
    total: int = length * count
    if total > _MUL_MAX_INDEX - data_offset - 1:
        return _mul_error(19, cstr("repeated sequence is too large"))
    if tag != PY_TYPE_BYTEARRAY and (count == 1 or length == 0):
        return _mul_copy(slots, tokens, _MUL_RESULT, source)
    if tag == PY_TYPE_STR:
        store_ptr(result, 0, py_str_new(null(), total))
    else:
        store_ptr(result, 0, py_bytes_new(null(), total))
    if _mul_adopt(slots, tokens, _MUL_RESULT) != 0:
        return -1
    position: int = 0
    while position < total:
        store_i8(load_ptr(result, 0), data_offset + position,
                 load_i8(load_ptr(snapshot, 0), data_offset + position % length))
        position = position + 1
    if tag == PY_TYPE_STR:
        codepoints: int = load_i64(load_ptr(snapshot, 0), PYSTROBJECT_CP_LEN_OFFSET)
        if codepoints >= 0:
            store_i64(load_ptr(result, 0), PYSTROBJECT_CP_LEN_OFFSET, codepoints * count)
    elif tag == PY_TYPE_BYTEARRAY:
        temporary = ptr_add(slots, _MUL_TEMP * C_POINTER_SIZE)
        store_ptr(temporary, 0, py_bytearray_from_obj(load_ptr(result, 0)))
        if _mul_adopt(slots, tokens, _MUL_TEMP) != 0:
            return -1
        _mul_drop(slots, tokens, _MUL_RESULT)
        if pcc_gc_root_move(result, temporary) != 0:
            return _mul_error(7, cstr("bytearray result transfer failed"))
        store_i64(tokens, _MUL_RESULT * C_POINTER_SIZE, load_i64(tokens, _MUL_TEMP * C_POINTER_SIZE))
        store_i64(tokens, _MUL_TEMP * C_POINTER_SIZE, -1)
    return 0


def _mul_is_sequence(tag: int) -> int:
    return 1 if tag == PY_TYPE_STR or tag == PY_TYPE_BYTES or tag == PY_TYPE_BYTEARRAY or tag == PY_TYPE_LIST or tag == PY_TYPE_TUPLE else 0


def _mul_body(slots: c_ptr, tokens: c_ptr) -> int:
    left = ptr_add(slots, _MUL_LEFT * C_POINTER_SIZE)
    right = ptr_add(slots, _MUL_RIGHT * C_POINTER_SIZE)
    result = ptr_add(slots, _MUL_RESULT * C_POINTER_SIZE)
    if ptr_is_null(load_ptr(left, 0)) != 0 or ptr_is_null(load_ptr(right, 0)) != 0:
        return _mul_error(3, cstr("unsupported operand type(s) for *"))
    left_tag: int = _mul_type(load_ptr(left, 0))
    right_tag: int = _mul_type(load_ptr(right, 0))
    if pcc_capi_is_cext_type_tag(left_tag) != 0 or pcc_capi_is_cext_type_tag(right_tag) != 0:
        store_ptr(result, 0, py_obj_mul(load_ptr(left, 0), load_ptr(right, 0)))
        return _mul_adopt(slots, tokens, _MUL_RESULT)
    source_index: int = _MUL_LEFT
    count_index: int = _MUL_RIGHT
    tag: int = left_tag
    count_tag: int = right_tag
    method_name = cstr("__rmul__")
    if _mul_is_sequence(left_tag) == 0:
        if _mul_is_sequence(right_tag) == 0:
            store_ptr(result, 0, py_obj_mul(load_ptr(left, 0), load_ptr(right, 0)))
            return _mul_adopt(slots, tokens, _MUL_RESULT)
        source_index = _MUL_RIGHT
        count_index = _MUL_LEFT
        tag = right_tag
        count_tag = left_tag
        method_name = cstr("__mul__")
    source = ptr_add(slots, source_index * C_POINTER_SIZE)
    count_slot = ptr_add(slots, count_index * C_POINTER_SIZE)
    if count_tag != PY_TYPE_INT and count_tag != PY_TYPE_BOOL:
        arguments = ptr_add(slots, _MUL_ARGS * C_POINTER_SIZE)
        store_ptr(arguments, 0, py_tuple_new(1))
        if _mul_adopt(slots, tokens, _MUL_ARGS) != 0:
            return -1
        py_tuple_set_item(load_ptr(arguments, 0), 0, load_ptr(source, 0))
        if py_err_occurred() != 0:
            return -1
        handled = stack_alloc(C_POINTER_SIZE)
        store_i64(handled, 0, 0)
        if py_obj_special_call_slots(count_slot, method_name, arguments, null(), result, handled) != 0:
            return -1
        if load_i64(handled, 0) != 0:
            if _mul_adopt(slots, tokens, _MUL_RESULT) != 0:
                return -1
            if ptr_eq(load_ptr(result, 0), global_load_ptr("py_NotImplemented")) == 0:
                return 0
            _mul_drop(slots, tokens, _MUL_RESULT)
        _mul_drop(slots, tokens, _MUL_ARGS)
    if py_obj_has_index(load_ptr(count_slot, 0)) == 0:
        # CPython's sequence_repeat checks the count's index protocol first.
        py_raise_type_named_error(cstr("can't multiply sequence by non-int of type '"),
                                  load_ptr(count_slot, 0), cstr("'"))
        return -1
    count: int = py_index_i64_checked_slots(count_slot)
    if py_err_occurred() != 0:
        return -1
    if tag == PY_TYPE_LIST or tag == PY_TYPE_TUPLE:
        return _mul_sequence_items(slots, tokens, source_index, tag, count)
    return _mul_sequence_bytes(slots, tokens, source_index, tag, count)


@c_abi_export("py_obj_mul_slots")
def py_obj_mul_slots(left_owner: c_ptr, right_owner: c_ptr, output_owner: c_ptr) -> int:
    """Status 0/-1; publish one owner to a distinct empty caller root.

    Both inputs are authoritative owning caller roots. The output remains
    registered through input/snapshot retirement and every pending exception.
    No caller may manufacture these contracts by wrapping a raw pointer.
    """
    if ptr_is_null(left_owner) != 0 or ptr_is_null(right_owner) != 0 or ptr_is_null(output_owner) != 0:
        return _mul_error(7, cstr("multiplication requires owning slots"))
    if ptr_eq(output_owner, left_owner) != 0 or ptr_eq(output_owner, right_owner) != 0 or ptr_is_null(load_ptr(output_owner, 0)) == 0:
        return _mul_error(7, cstr("multiplication output must be distinct and empty"))
    slots = stack_alloc(_MUL_SLOT_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_MUL_SLOT_COUNT * C_POINTER_SIZE)
    memset(slots, 0, _MUL_SLOT_COUNT * C_POINTER_SIZE)
    index: int = 0
    while index < _MUL_SLOT_COUNT:
        store_i64(tokens, index * C_POINTER_SIZE, -1)
        index = index + 1
    pcc_gc_frame_enter(global_addr("pcc_binary_mul_owned_map"), slots)
    status: int = _mul_copy(slots, tokens, _MUL_LEFT, left_owner)
    if status == 0:
        status = _mul_copy(slots, tokens, _MUL_RIGHT, right_owner)
    if status == 0:
        status = _mul_body(slots, tokens)
    if status == 0:
        status = pcc_gc_root_move(output_owner, ptr_add(slots, _MUL_RESULT * C_POINTER_SIZE))
        if status == 0:
            token: int = load_i64(tokens, _MUL_RESULT * C_POINTER_SIZE)
            store_i64(tokens, _MUL_RESULT * C_POINTER_SIZE, -1)
            if pcc_gc_foreign_lease_release(output_owner, token) != 0:
                pcc_platform_abort()
                return -1
    if status != 0:
        _mul_error(7, cstr("multiplication failed without an exception"))
    error = ptr_add(slots, _MUL_ERROR * C_POINTER_SIZE)
    py_tls_exc_swap_slot(error)
    index = _MUL_ERROR - 1
    while index >= 0:
        _mul_clear(slots, tokens, index)
        index = index - 1
    py_clear_exception()
    py_tls_exc_swap_slot(error)
    pcc_gc_frame_leave(slots)
    return status
