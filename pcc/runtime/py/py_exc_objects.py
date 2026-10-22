"""Phase 4c.5: pcc-Python port of py_exc_objects.c.

Exception-object construction, accessor, and deallocation. Handles:
    py_exc_alloc, py_exc_new, py_exc_new_with_class,
    py_exc_set_cause, py_exc_set_context, py_exc_get_message,
    py_dealloc_exc

PyExceptionObject layout (from py_internal.h):
    offset  0   PyObjectHeader    (i64 refcount + i32 tag + i32 flags = 16 bytes)
    offset 16   exc_class         (ptr)
    offset 24   message           (ptr)
    offset 32   cause             (ptr)
    offset 40   context           (ptr)
    offset 48   traceback         (ptr)
    offset 56   n_frames          (i32)
    offset 60   cap_frames        (i32)
    total size: 64 bytes

Public object type tags come from the generated ``py_abi_constants`` module.
The private exception-table code used here is ``PY_EXC_EXCEPTION``.
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import (
    PY_TYPE_NONE,
    PY_FLAG_EXC_SUPPRESS_CONTEXT,
    PY_FLAG_EXC_UNICODE_PAYLOAD,
    PY_FLAG_EXC_OS_PAYLOAD,
    PYTUPLEOBJECT_ITEMS_OFFSET,
    PY_TYPE_BOOL,
    PY_TYPE_BYTEARRAY,
    PY_TYPE_BYTES,
    PY_TYPE_CLASS,
    PY_TYPE_COMPLEX,
    PY_TYPE_EXC,
    PY_TYPE_FLOAT,
    PY_TYPE_INT,
    PY_TYPE_MEMORYVIEW,
    PY_TYPE_STR,
    PY_TYPE_TUPLE,
)
from pcc.extern import extern, c_abi_export, c_int32, c_ptr, c_int64, c_void
from pcc.unsafe import (
    atomic_rmw_i32,
    cstr,
    define_global_i32,
    free,
    global_addr,
    global_load_ptr,
    is_tagged_int,
    load_i32,
    load_i8,
    load_ptr,
    malloc,
    memset,
    null,
    ptr_add,
    ptr_is_null,
    ptr_eq,
    stack_alloc,
    store_i32,
    store_i8,
    store_i64,
    store_ptr,
    strlen,
)

py_incref            = extern("py_incref",            (c_ptr,),                    c_void)
py_decref            = extern("py_decref",            (c_ptr,),                    c_void)
py_str_new           = extern("py_str_new",           (c_ptr, c_int64),            c_ptr)
py_exc_builtin_class = extern("py_exc_builtin_class", (c_int64,),                  c_ptr)
pcc_gc_alloc         = extern("pcc_gc_alloc",         (c_int64, c_int32, c_int32), c_ptr)
pcc_gc_load_ptr      = extern("pcc_gc_load_ptr",      (c_ptr, c_ptr), c_ptr)
pcc_gc_note_relocation_read = extern("pcc_gc_note_relocation_read", (c_ptr,), c_ptr)
pcc_gc_publish_initialized = extern(
    "pcc_gc_publish_initialized", (c_ptr,), c_void
)
pcc_gc_store_ptr     = extern("pcc_gc_store_ptr",     (c_ptr, c_ptr, c_ptr), c_void)
pcc_gc_free_object_memory = extern("pcc_gc_free_object_memory", (c_ptr,), c_void)
pcc_gc_frame_enter = extern("pcc_gc_frame_enter", (c_ptr, c_ptr), c_void)
pcc_gc_frame_leave = extern("pcc_gc_frame_leave", (c_ptr,), c_void)
pcc_gc_store_root = extern("pcc_gc_store_root", (c_ptr, c_ptr), c_void)
pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
pcc_gc_take_pinned_slot = extern("pcc_gc_take_pinned_slot", (c_ptr, c_int64), c_ptr)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)
define_global_i32("pcc_exc_construct_borrowed_frame_map", -2)
define_global_i32("pcc_exc_construct_owned_frame_map", 2)
pcc_gc_backend = extern("pcc_gc_backend", (), c_int64)
pcc_gc_store_ptr_plan_init = extern("pcc_gc_store_ptr_plan_init", (c_ptr, c_ptr, c_int64), c_void)
pcc_gc_store_ptr_plan_commit_locked = extern("pcc_gc_store_ptr_plan_commit_locked", (c_ptr, c_ptr, c_ptr, c_ptr), c_int64)
pcc_gc_store_ptr_plan_finish = extern("pcc_gc_store_ptr_plan_finish", (c_ptr,), c_void)
pcc_diagnostics_runtime_log_event_code = extern(
    "pcc_diagnostics_runtime_log_event_code", (c_int32, c_int32, c_int64, c_int64, c_ptr), c_void,
)


def _type_of(obj) -> int:
    if is_tagged_int(obj):
        return PY_TYPE_INT       # PY_TYPE_INT
    return load_i32(obj, 8)


def _exc_store_constructed_slot(owned, values, value_offset: int, field_offset: int) -> None:
    # Fresh construction slots contain NULL/None, so this store has no user
    # destructor tail. Reload both operands under the normal graph lease;
    # a callee's implicit poll cannot invalidate the raw field address.
    pcc_py_gc_minor_graph_lock()
    e = pcc_gc_load_ptr(null(), owned)
    value = pcc_gc_load_ptr(null(), ptr_add(values, value_offset))
    pcc_gc_store_ptr(e, ptr_add(e, field_offset), value)
    pcc_py_gc_minor_graph_unlock()


def _exc_construct_body(borrowed, owned, msg) -> int:
    if ptr_is_null(pcc_gc_load_ptr(null(), borrowed)):
        cls = py_exc_builtin_class(1)
        store_ptr(borrowed, 0, cls)
        if ptr_is_null(cls):
            return 0
    e = pcc_gc_alloc(64, PY_TYPE_EXC, 0)   # sizeof(PyExceptionObject)
    store_ptr(owned, 0, e)
    if ptr_is_null(e):
        return 0
    # Header is initialized by pcc_gc_alloc; clear the payload tail.
    memset(ptr_add(e, 16), 0, 48)
    store_i64(e, 0, 1)
    store_i32(e, 8, PY_TYPE_EXC)
    _exc_store_constructed_slot(owned, borrowed, 0, 16)
    if not ptr_is_null(msg):
        n: int = strlen(msg)
        s = py_str_new(msg, n)
        store_ptr(owned, 8, s)
        if ptr_is_null(s):
            return 0
    else:
        # NULL means no positional argument. py_None and the empty string
        # are real supplied arguments and must remain distinguishable.
        store_ptr(owned, 8, null())
    _exc_store_constructed_slot(owned, owned, 8, 24)
    return 1


def _exc_construct_finish(borrowed, owned, complete: int, log_code: int, log_tag: int):
    prior_pin: int = 0
    if complete != 0:
        pcc_diagnostics_runtime_log_event_code(6, 1, 12, 0, pcc_gc_load_ptr(null(), owned))
    if log_code == 2:
        pcc_diagnostics_runtime_log_event_code(6, 2, log_tag, 0, pcc_gc_load_ptr(null(), owned))
    elif log_code == 8:
        pcc_diagnostics_runtime_log_event_code(6, 8, log_tag, 0, pcc_gc_load_ptr(null(), owned))
    elif log_code == 9:
        pcc_diagnostics_runtime_log_event_code(6, 9, log_tag, 0, pcc_gc_load_ptr(null(), owned))
    if complete != 0:
        pcc_gc_publish_initialized(pcc_gc_load_ptr(null(), owned))
        # Construction remained movable. Only the final raw NEW handoff
        # spans root unregistration, whose graph unlock can itself park.
        result = pcc_gc_load_ptr(null(), owned)
        prior_pin = load_i32(result, 12) & 64
        pcc_gc_pin(result)
    pcc_gc_store_root(ptr_add(owned, 8), null())
    if complete == 0:
        pcc_gc_store_root(owned, null())
    pcc_gc_frame_leave(owned)
    pcc_gc_frame_leave(borrowed)
    if complete == 0:
        return null()
    return pcc_gc_take_pinned_slot(owned, prior_pin)


@c_abi_export("py_exc_alloc")
def py_exc_alloc(cls, msg):
    borrowed = stack_alloc(16)
    store_ptr(borrowed, 0, cls)
    store_ptr(borrowed, 8, null())
    pcc_gc_frame_enter(global_addr("pcc_exc_construct_borrowed_frame_map"), borrowed)
    owned = stack_alloc(16)
    memset(owned, 0, 16)
    pcc_gc_frame_enter(global_addr("pcc_exc_construct_owned_frame_map"), owned)
    complete: int = _exc_construct_body(borrowed, owned, msg)
    return _exc_construct_finish(borrowed, owned, complete, 0, 0)


def _default_exc_alloc(msg):
    # Direct PY_EXC_EXCEPTION default-class lookup. (Previously we
    # avoided calling py_exc_new here because of an int32/int64
    # signature mismatch; now resolved.)
    return py_exc_alloc(null(), msg)


@c_abi_export("py_exc_new")
def py_exc_new(type_tag: int, msg):
    borrowed = stack_alloc(16)
    memset(borrowed, 0, 16)
    pcc_gc_frame_enter(global_addr("pcc_exc_construct_borrowed_frame_map"), borrowed)
    owned = stack_alloc(16)
    memset(owned, 0, 16)
    pcc_gc_frame_enter(global_addr("pcc_exc_construct_owned_frame_map"), owned)
    cls = py_exc_builtin_class(type_tag)
    store_ptr(borrowed, 0, cls)
    complete: int = 0
    if ptr_is_null(cls) == 0:
        complete = _exc_construct_body(borrowed, owned, msg)
    return _exc_construct_finish(borrowed, owned, complete, 2, type_tag)


@c_abi_export("py_exc_new_with_value")
def py_exc_new_with_value(type_tag: int, value):
    borrowed = stack_alloc(16)
    store_ptr(borrowed, 0, null())
    store_ptr(borrowed, 8, value)
    pcc_gc_frame_enter(global_addr("pcc_exc_construct_borrowed_frame_map"), borrowed)
    owned = stack_alloc(16)
    memset(owned, 0, 16)
    pcc_gc_frame_enter(global_addr("pcc_exc_construct_owned_frame_map"), owned)
    cls = py_exc_builtin_class(type_tag)
    store_ptr(borrowed, 0, cls)
    complete: int = 0
    if ptr_is_null(cls) == 0:
        complete = _exc_construct_body(borrowed, owned, null())
    if complete != 0:
        _exc_store_constructed_slot(owned, borrowed, 8, 24)
    return _exc_construct_finish(borrowed, owned, complete, 8, type_tag)


@c_abi_export("py_exc_new_with_class")
def py_exc_new_with_class(cls, msg):
    borrowed = stack_alloc(16)
    store_ptr(borrowed, 0, cls)
    store_ptr(borrowed, 8, null())
    pcc_gc_frame_enter(global_addr("pcc_exc_construct_borrowed_frame_map"), borrowed)
    owned = stack_alloc(16)
    memset(owned, 0, 16)
    pcc_gc_frame_enter(global_addr("pcc_exc_construct_owned_frame_map"), owned)
    current = pcc_gc_load_ptr(null(), borrowed)
    tag: int = 0
    if ptr_is_null(current) == 0 and is_tagged_int(current) == 0:
        tag = load_i32(current, 8)
    log_code: int = 9
    if tag != PY_TYPE_CLASS:
        store_ptr(borrowed, 0, null())
        log_code = 0
    complete: int = _exc_construct_body(borrowed, owned, msg)
    return _exc_construct_finish(borrowed, owned, complete, log_code, tag)


@c_abi_export("py_exc_set_cause")
def py_exc_set_cause(exc, cause) -> None:
    if ptr_is_null(exc):
        return
    if _type_of(exc) != PY_TYPE_EXC:
        return
    exc = pcc_gc_note_relocation_read(exc)
    # Explicit cause assignment suppresses implicit context even for None.
    # Atomic bit operations leave concurrent collector flags unchanged.
    atomic_rmw_i32("or", exc, 12, PY_FLAG_EXC_SUPPRESS_CONTEXT, "relaxed")
    pcc_gc_store_ptr(exc, ptr_add(exc, 32), cause)      # ->cause
    pcc_diagnostics_runtime_log_event_code(6, 5, 0 if ptr_is_null(cause) else 1, 0, exc)


@c_abi_export("py_exc_set_context")
def py_exc_set_context(exc, context) -> None:
    if ptr_is_null(exc):
        return
    if _type_of(exc) != PY_TYPE_EXC:
        return
    pcc_gc_store_ptr(exc, ptr_add(exc, 40), context)      # ->context
    pcc_diagnostics_runtime_log_event_code(6, 6, 0 if ptr_is_null(context) else 1, 0, exc)


@c_abi_export("py_exc_set_implicit_context_slots")
def py_exc_set_implicit_context_slots(exception_slot, context_slot) -> None:
    """Overwrite implicit context, breaking only links that would make a cycle.

    The caller keeps both slots in the shared root/frame contract. Traverse
    under the graph lease, then finish displaced-owner decrefs outside it so
    finalizers remain free to allocate, collect and resurrect. The slow cursor
    terminates even a pre-existing user-created context cycle (CPython's rule).
    Explicit cause and suppression fields are never changed here.
    """
    if ptr_is_null(exception_slot) or ptr_is_null(context_slot):
        return
    sever = stack_alloc(128)
    replace = stack_alloc(128)
    backend: int = pcc_gc_backend()
    pcc_gc_store_ptr_plan_init(sever, null(), backend)
    pcc_gc_store_ptr_plan_init(replace, null(), backend)
    pcc_py_gc_minor_graph_lock()
    exc = pcc_gc_load_ptr(null(), exception_slot)
    context = pcc_gc_load_ptr(null(), context_slot)
    if ptr_is_null(exc) == 0 and ptr_is_null(context) == 0 and ptr_eq(exc, context) == 0:
        if is_tagged_int(exc) == 0 and load_i32(exc, 8) == PY_TYPE_EXC:
            current = context
            slow = context
            advance_slow: int = 0
            while ptr_is_null(current) == 0 and is_tagged_int(current) == 0:
                if load_i32(current, 8) != PY_TYPE_EXC:
                    break
                following = pcc_gc_load_ptr(current, ptr_add(current, 40))
                if ptr_is_null(following):
                    break
                if ptr_eq(following, exc):
                    pcc_gc_store_ptr_plan_commit_locked(sever, current, ptr_add(current, 40), null())
                    break
                current = following
                if ptr_eq(current, slow):
                    break
                if advance_slow != 0:
                    if ptr_is_null(slow) == 0 and is_tagged_int(slow) == 0:
                        if load_i32(slow, 8) == PY_TYPE_EXC:
                            slow = pcc_gc_load_ptr(slow, ptr_add(slow, 40))
                advance_slow = 0 if advance_slow != 0 else 1
            pcc_gc_store_ptr_plan_commit_locked(replace, exc, ptr_add(exc, 40), context)
    pcc_py_gc_minor_graph_unlock()
    pcc_gc_store_ptr_plan_finish(sever)
    pcc_gc_store_ptr_plan_finish(replace)


@c_abi_export("py_exc_get_message")
def py_exc_get_message(exc):
    if ptr_is_null(exc):
        return null()
    if _type_of(exc) != PY_TYPE_EXC:
        return null()
    # The discriminator and its mutable slot must be read under one lease.
    # This remains a BORROWED API: callers still must keep its argument alive
    # and synchronize concurrent argument mutation while using the result.
    return _exc_primary_argument(exc)


@c_abi_export("py_exc_get_cause")
def py_exc_get_cause(exc):
    if ptr_is_null(exc):
        return null()
    if _type_of(exc) != PY_TYPE_EXC:
        return null()
    cause = pcc_gc_load_ptr(exc, ptr_add(exc, 32))
    if ptr_is_null(cause):
        cause = global_load_ptr("py_None")
    py_incref(cause)
    return cause


@c_abi_export("py_exc_get_context")
def py_exc_get_context(exc):
    if ptr_is_null(exc):
        return null()
    if _type_of(exc) != PY_TYPE_EXC:
        return null()
    context = pcc_gc_load_ptr(exc, ptr_add(exc, 40))
    if ptr_is_null(context):
        context = global_load_ptr("py_None")
    py_incref(context)
    return context


@c_abi_export("py_exc_traceback_len")
def py_exc_traceback_len(exc) -> int:
    if ptr_is_null(exc):
        return 0
    if _type_of(exc) != PY_TYPE_EXC:
        return 0
    return load_i32(exc, 56)


@c_abi_export("py_dealloc_exc")
def py_dealloc_exc(o) -> None:
    pcc_diagnostics_runtime_log_event_code(6, 7, 12, 0, o)
    # ->exc_class
    cls = pcc_gc_load_ptr(o, ptr_add(o, 16))
    if not ptr_is_null(cls):
        py_decref(cls)
    # ->message
    msg = pcc_gc_load_ptr(o, ptr_add(o, 24))
    if not ptr_is_null(msg):
        py_decref(msg)
    # ->cause
    cause = pcc_gc_load_ptr(o, ptr_add(o, 32))
    if not ptr_is_null(cause):
        py_decref(cause)
    # ->context
    ctx = pcc_gc_load_ptr(o, ptr_add(o, 40))
    if not ptr_is_null(ctx):
        py_decref(ctx)
    # ->traceback (malloc'd array, free not decref)
    tb = load_ptr(o, 48)
    if not ptr_is_null(tb):
        free(tb)
    pcc_gc_free_object_memory(o)


# UnicodeEncodeError and UnicodeDecodeError use the owned/traced message slot.
# Record[0] is .args, initially the constructor tuple; [1:6] are independently
# mutable attributes; [6] is the immutable builtin tag. The explicit flag
# distinguishes this record from a legacy message or user-supplied tuple.
py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_tuple_get = extern("py_tuple_get", (c_ptr, c_int64), c_ptr)
py_tuple_from_splat = extern("py_tuple_from_splat", (c_ptr,), c_ptr)
py_bytes_new = extern("py_bytes_new", (c_ptr, c_int64), c_ptr)
py_bytes_from_obj = extern("py_bytes_from_obj", (c_ptr,), c_ptr)
py_tuple_len = extern("py_tuple_len", (c_ptr,), c_int64)
py_tuple_set_item = extern("py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void)
py_int_from_i64 = extern("py_int_from_i64", (c_int64,), c_ptr)
py_int_to_i64 = extern("py_int_to_i64", (c_ptr, c_ptr), c_int64)
py_exc_matches = extern("py_exc_matches", (c_ptr, c_ptr), c_int64)
py_obj_index = extern("py_obj_index", (c_ptr,), c_ptr)
py_str_utf8 = extern("py_str_utf8", (c_ptr,), c_ptr)
py_str_byte_len = extern("py_str_byte_len", (c_ptr,), c_int64)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_gc_track = extern("py_gc_track", (c_ptr,), c_void)
py_obj_type_name = extern("py_obj_type_name", (c_ptr,), c_ptr)
define_global_i32("pcc_unicode_borrowed_map", -2)
define_global_i32("pcc_unicode_owned_map", 8)


def _unicode_pin_slot(slot) -> int:
    pcc_py_gc_minor_graph_lock()
    value = pcc_gc_load_ptr(null(), slot)
    prior: int = 0
    if ptr_is_null(value) == 0 and is_tagged_int(value) == 0:
        prior = load_i32(value, 12) & 64
        pcc_gc_pin(value)
    pcc_py_gc_minor_graph_unlock()
    return prior


def _unicode_tuple_put(slots, tuple_offset: int, index: int, value_offset: int) -> None:
    # Setters may park. These are NEW tuple slots; the pin covers only their
    # raw-argument ABI handoff and does not pin the completed exception graph.
    target_slot = ptr_add(slots, tuple_offset)
    value_slot = ptr_add(slots, value_offset)
    target_pin: int = _unicode_pin_slot(target_slot)
    value_pin: int = _unicode_pin_slot(value_slot)
    py_tuple_set_item(load_ptr(target_slot, 0), index, load_ptr(value_slot, 0))
    store_ptr(value_slot, 0, pcc_gc_take_pinned_slot(value_slot, value_pin))
    store_ptr(target_slot, 0, pcc_gc_take_pinned_slot(target_slot, target_pin))


def _unicode_finish(borrowed, owned, success: int):
    pin: int = 0
    if success != 0:
        pin = _unicode_pin_slot(owned)
    i: int = 1
    while i < 8:
        pcc_gc_store_root(ptr_add(owned, i * 8), null())
        i = i + 1
    if success == 0:
        pcc_gc_store_root(owned, null())
    pcc_gc_frame_leave(owned)
    pcc_gc_frame_leave(borrowed)
    if success == 0:
        return null()
    return pcc_gc_take_pinned_slot(owned, pin)


def _unicode_copy_cstr(out, offset: int, text) -> int:
    i: int = 0
    while load_i8(text, i) != 0:
        store_i8(out, offset, load_i8(text, i))
        offset = offset + 1
        i = i + 1
    return offset


def _unicode_arity_error(count: int) -> None:
    out = stack_alloc(96)
    offset: int = _unicode_copy_cstr(out, 0, cstr("function takes exactly 5 arguments ("))
    digits = stack_alloc(24)
    size: int = 0
    while count > 9:
        store_i8(digits, size, 48 + count % 10)
        size = size + 1
        count = count // 10
    store_i8(digits, size, 48 + count)
    size = size + 1
    while size > 0:
        size = size - 1
        store_i8(out, offset, load_i8(digits, size))
        offset = offset + 1
    offset = _unicode_copy_cstr(out, offset, cstr(" given)"))
    store_i8(out, offset, 0)
    py_raise_owned(py_exc_new(3, out))


def _unicode_type_error(slots, offset: int, argument: int) -> None:
    slot = ptr_add(slots, offset)
    name_slot = ptr_add(slots, 16)
    prior: int = _unicode_pin_slot(slot)
    # py_obj_type_name returns a NEW managed str, not a borrowed char pointer.
    store_ptr(name_slot, 0, py_obj_type_name(load_ptr(slot, 0)))
    store_ptr(slot, 0, pcc_gc_take_pinned_slot(slot, prior))
    if ptr_is_null(load_ptr(name_slot, 0)):
        return
    name_pin: int = _unicode_pin_slot(name_slot)
    name = py_str_utf8(load_ptr(name_slot, 0))
    message = malloc(py_str_byte_len(load_ptr(name_slot, 0)) + 48)
    if ptr_is_null(message) == 0:
        end: int = 0
        if argument == 0:
            end = _unicode_copy_cstr(message, 0, cstr("a bytes-like object is required, not '"))
        else:
            end = _unicode_copy_cstr(message, 0, cstr("argument "))
            store_i8(message, end, 48 + argument)
            end = _unicode_copy_cstr(message, end + 1, cstr(" must be str, not "))
        end = _unicode_copy_cstr(message, end, name)
        if argument == 0:
            store_i8(message, end, 39)
            end = end + 1
        store_i8(message, end, 0)
    store_ptr(name_slot, 0, pcc_gc_take_pinned_slot(name_slot, name_pin))
    pcc_gc_store_root(name_slot, null())
    if ptr_is_null(message) == 0:
        py_raise_owned(py_exc_new(3, message))
        free(message)


def _unicode_constructor_index(owned, offset: int) -> int:
    source_slot = ptr_add(owned, offset)
    result_slot = ptr_add(owned, 16)
    prior: int = _unicode_pin_slot(source_slot)
    # Invoke __index__ exactly once, retaining the full arbitrary-precision
    # result until the platform-sized conversion has been checked.
    store_ptr(result_slot, 0, py_obj_index(load_ptr(source_slot, 0)))
    store_ptr(source_slot, 0, pcc_gc_take_pinned_slot(source_slot, prior))
    if ptr_is_null(load_ptr(result_slot, 0)):
        return 0
    overflow = stack_alloc(4)
    store_i32(overflow, 0, 0)
    result_pin: int = _unicode_pin_slot(result_slot)
    number: int = py_int_to_i64(load_ptr(result_slot, 0), overflow)
    if load_i32(overflow, 0) == 0:
        pcc_gc_store_root(source_slot, load_ptr(result_slot, 0))
    store_ptr(result_slot, 0, pcc_gc_take_pinned_slot(result_slot, result_pin))
    pcc_gc_store_root(result_slot, null())
    if load_i32(overflow, 0) != 0:
        py_raise_owned(py_exc_new(15, cstr("Python int too large to convert to C ssize_t")))
        return 0
    return 1


def _unicode_constructor_body(borrowed, owned, type_tag: int) -> int:
    args = pcc_gc_load_ptr(null(), borrowed)
    count: int = py_tuple_len(args)
    if count != 5:
        _unicode_arity_error(count)
        return 0
    # Snapshot constructor arguments before invoking any __index__ method.
    i: int = 0
    while i < 5:
        source_pin: int = _unicode_pin_slot(borrowed)
        store_ptr(owned, 24 + i * 8, py_tuple_get(load_ptr(borrowed, 0), i))
        store_ptr(borrowed, 0, pcc_gc_take_pinned_slot(borrowed, source_pin))
        i = i + 1
    if _type_of(pcc_gc_load_ptr(null(), ptr_add(owned, 24))) != PY_TYPE_STR:
        _unicode_type_error(owned, 24, 1)
        return 0
    source_tag: int = _type_of(pcc_gc_load_ptr(null(), ptr_add(owned, 32)))
    if type_tag == 58:
        if source_tag != PY_TYPE_BYTES:
            if source_tag != PY_TYPE_BYTEARRAY and source_tag != PY_TYPE_MEMORYVIEW:
                _unicode_type_error(owned, 32, 0)
                return 0
            # Buffer conversion owns and leases the actual payload leaf, so
            # a memoryview's base may move independently of the outer view.
            source_slot = ptr_add(owned, 32)
            source_pin: int = _unicode_pin_slot(source_slot)
            store_ptr(owned, 16, py_bytes_from_obj(load_ptr(source_slot, 0)))
            store_ptr(source_slot, 0, pcc_gc_take_pinned_slot(source_slot, source_pin))
            if ptr_is_null(load_ptr(owned, 16)):
                return 0
            pcc_gc_store_root(source_slot, load_ptr(owned, 16))
            pcc_gc_store_root(ptr_add(owned, 16), null())
    elif source_tag != PY_TYPE_STR:
        _unicode_type_error(owned, 32, 2)
        return 0
    if _unicode_constructor_index(owned, 40) == 0:
        return 0
    if _unicode_constructor_index(owned, 48) == 0:
        return 0
    if _type_of(pcc_gc_load_ptr(null(), ptr_add(owned, 56))) != PY_TYPE_STR:
        _unicode_type_error(owned, 56, 5)
        return 0
    store_ptr(owned, 8, py_tuple_new(7))
    if ptr_is_null(load_ptr(owned, 8)):
        return 0
    args = pcc_gc_load_ptr(null(), borrowed)
    py_incref(args)
    store_ptr(owned, 16, args)
    _unicode_tuple_put(owned, 8, 0, 16)
    i = 0
    while i < 5:
        _unicode_tuple_put(owned, 8, i + 1, 24 + i * 8)
        i = i + 1
    pcc_gc_store_root(ptr_add(owned, 16), null())
    store_ptr(owned, 16, py_int_from_i64(type_tag))
    _unicode_tuple_put(owned, 8, 6, 16)
    store_ptr(owned, 0, py_exc_new(type_tag, null()))
    if ptr_is_null(load_ptr(owned, 0)):
        return 0
    _exc_store_constructed_slot(owned, owned, 8, 24)
    pcc_py_gc_minor_graph_lock()
    error = pcc_gc_load_ptr(null(), owned)
    atomic_rmw_i32("or", error, 12, PY_FLAG_EXC_UNICODE_PAYLOAD, "relaxed")
    pcc_py_gc_minor_graph_unlock()
    return 1


def _unicode_error_new(args, type_tag: int):
    borrowed = stack_alloc(16)
    store_ptr(borrowed, 0, args)
    store_ptr(borrowed, 8, null())
    pcc_gc_frame_enter(global_addr("pcc_unicode_borrowed_map"), borrowed)
    owned = stack_alloc(64)
    memset(owned, 0, 64)
    pcc_gc_frame_enter(global_addr("pcc_unicode_owned_map"), owned)
    success: int = _unicode_constructor_body(borrowed, owned, type_tag)
    return _unicode_finish(borrowed, owned, success)


@c_abi_export("py_unicode_encode_error_new")
def py_unicode_encode_error_new(args):
    return _unicode_error_new(args, 59)


@c_abi_export("py_unicode_decode_error_new")
def py_unicode_decode_error_new(args):
    return _unicode_error_new(args, 58)


def _unicode_error_normalize(value, type_tag: int):
    """Normalize a PyErr value: existing instance, argument tuple, or one arg."""
    borrowed = stack_alloc(16)
    store_ptr(borrowed, 0, value)
    store_ptr(borrowed, 8, null())
    pcc_gc_frame_enter(global_addr("pcc_unicode_borrowed_map"), borrowed)
    owned = stack_alloc(64)
    memset(owned, 0, 64)
    pcc_gc_frame_enter(global_addr("pcc_unicode_owned_map"), owned)
    current = pcc_gc_load_ptr(null(), borrowed)
    tag: int = -1
    if ptr_is_null(current) == 0:
        tag = _type_of(current)
    matched: int = 0
    if tag == PY_TYPE_EXC:
        class_slot = ptr_add(borrowed, 8)
        store_ptr(class_slot, 0, py_exc_builtin_class(type_tag))
        class_pin: int = _unicode_pin_slot(class_slot)
        prior: int = _unicode_pin_slot(borrowed)
        matched = py_exc_matches(load_ptr(borrowed, 0), load_ptr(class_slot, 0))
        store_ptr(borrowed, 0, pcc_gc_take_pinned_slot(borrowed, prior))
        store_ptr(class_slot, 0, pcc_gc_take_pinned_slot(class_slot, class_pin))
    if matched != 0:
        pcc_py_gc_minor_graph_lock()
        pcc_gc_store_root(owned, pcc_gc_load_ptr(null(), borrowed))
        pcc_py_gc_minor_graph_unlock()
        return _unicode_finish(borrowed, owned, 1)
    if tag == PY_TYPE_TUPLE:
        pcc_py_gc_minor_graph_lock()
        pcc_gc_store_root(ptr_add(owned, 8), pcc_gc_load_ptr(null(), borrowed))
        pcc_py_gc_minor_graph_unlock()
    else:
        count: int = 0 if tag < PY_TYPE_NONE else 1
        store_ptr(owned, 8, py_tuple_new(count))
        if count != 0 and ptr_is_null(load_ptr(owned, 8)) == 0:
            pcc_py_gc_minor_graph_lock()
            pcc_gc_store_root(ptr_add(owned, 16), pcc_gc_load_ptr(null(), borrowed))
            pcc_py_gc_minor_graph_unlock()
            _unicode_tuple_put(owned, 8, 0, 16)
    if ptr_is_null(load_ptr(owned, 8)) == 0:
        store_ptr(owned, 0, _unicode_error_new(pcc_gc_load_ptr(null(), ptr_add(owned, 8)), type_tag))
    return _unicode_finish(borrowed, owned, 0 if ptr_is_null(load_ptr(owned, 0)) else 1)


@c_abi_export("py_unicode_encode_error_normalize")
def py_unicode_encode_error_normalize(value):
    return _unicode_error_normalize(value, 59)


@c_abi_export("py_unicode_decode_error_normalize")
def py_unicode_decode_error_normalize(value):
    return _unicode_error_normalize(value, 58)


@c_abi_export("py_unicode_decode_error_from_buffer")
def py_unicode_decode_error_from_buffer(data, count: int, encoding, start: int, end: int, reason) -> None:
    # data/encoding/reason are caller-stable native buffers, never borrowed
    # interiors of movable Python objects. py_bytes_new copies all count bytes,
    # including embedded NULs, before the caller may release its buffer.
    borrowed = stack_alloc(16)
    memset(borrowed, 0, 16)
    pcc_gc_frame_enter(global_addr("pcc_unicode_borrowed_map"), borrowed)
    owned = stack_alloc(64)
    memset(owned, 0, 64)
    pcc_gc_frame_enter(global_addr("pcc_unicode_owned_map"), owned)
    store_ptr(owned, 24, py_bytes_new(data, count))
    if ptr_is_null(load_ptr(owned, 24)) == 0:
        store_ptr(owned, 8, py_tuple_new(5))
        store_ptr(owned, 16, py_str_new(encoding, strlen(encoding)))
        store_ptr(owned, 32, py_int_from_i64(start))
        store_ptr(owned, 40, py_int_from_i64(end))
        store_ptr(owned, 48, py_str_new(reason, strlen(reason)))
        complete: int = 1
        i: int = 1
        while i < 7:
            if ptr_is_null(pcc_gc_load_ptr(null(), ptr_add(owned, i * 8))):
                complete = 0
            i = i + 1
        if complete != 0:
            i = 0
            while i < 5:
                _unicode_tuple_put(owned, 8, i, 16 + i * 8)
                i = i + 1
            store_ptr(owned, 0, py_unicode_decode_error_new(pcc_gc_load_ptr(null(), ptr_add(owned, 8))))
    # Publish while the full graph still has an owning frame. Keep the owned
    # slot until py_raise_owned has installed the TLS reference, then release
    # the construction owner through the normal shared cleanup traversal.
    if ptr_is_null(load_ptr(owned, 0)) == 0:
        result_pin: int = _unicode_pin_slot(owned)
        result = load_ptr(owned, 0)
        py_incref(result)
        py_raise_owned(result)
        store_ptr(owned, 0, pcc_gc_take_pinned_slot(owned, result_pin))
    _unicode_finish(borrowed, owned, 0)


@c_abi_export("py_unicode_encode_error")
def py_unicode_encode_error(obj, encoding, start: int, end: int, reason) -> None:
    borrowed = stack_alloc(16)
    store_ptr(borrowed, 0, obj)
    store_ptr(borrowed, 8, null())
    pcc_gc_frame_enter(global_addr("pcc_unicode_borrowed_map"), borrowed)
    owned = stack_alloc(64)
    memset(owned, 0, 64)
    pcc_gc_frame_enter(global_addr("pcc_unicode_owned_map"), owned)
    store_ptr(owned, 8, py_tuple_new(5))
    store_ptr(owned, 16, py_str_new(encoding, strlen(encoding)))
    source = pcc_gc_load_ptr(null(), borrowed)
    py_incref(source)
    store_ptr(owned, 24, source)
    store_ptr(owned, 32, py_int_from_i64(start))
    store_ptr(owned, 40, py_int_from_i64(end))
    store_ptr(owned, 48, py_str_new(reason, strlen(reason)))
    complete: int = 1
    i: int = 1
    while i < 7:
        if ptr_is_null(pcc_gc_load_ptr(null(), ptr_add(owned, i * 8))):
            complete = 0
        i = i + 1
    if complete != 0:
        i = 0
        while i < 5:
            _unicode_tuple_put(owned, 8, i, 16 + i * 8)
            i = i + 1
        store_ptr(owned, 0, py_unicode_encode_error_new(pcc_gc_load_ptr(null(), ptr_add(owned, 8))))
        complete = 0 if ptr_is_null(load_ptr(owned, 0)) else 1
    result = _unicode_finish(borrowed, owned, complete)
    if ptr_is_null(result) == 0:
        py_raise_owned(result)


def _exc_primary_argument(error):
    # Keep the legacy BORROWED args[0] contract without leaking the private
    # record, and protect its raw handoff across frame unregistration.
    borrowed = stack_alloc(16)
    store_ptr(borrowed, 0, error)
    store_ptr(borrowed, 8, null())
    pcc_gc_frame_enter(global_addr("pcc_unicode_borrowed_map"), borrowed)
    pcc_py_gc_minor_graph_lock()
    error = pcc_gc_load_ptr(null(), borrowed)
    flags: int = load_i32(error, 12)
    value = pcc_gc_load_ptr(error, ptr_add(error, 24))
    if (flags & (PY_FLAG_EXC_UNICODE_PAYLOAD | PY_FLAG_EXC_OS_PAYLOAD)) != 0:
        args = pcc_gc_load_ptr(value, ptr_add(value, PYTUPLEOBJECT_ITEMS_OFFSET))
        value = null()
        if py_tuple_len(args) > 0:
            value = pcc_gc_load_ptr(args, ptr_add(args, PYTUPLEOBJECT_ITEMS_OFFSET))
    store_ptr(borrowed, 8, value)
    prior: int = 0
    if ptr_is_null(value) == 0 and is_tagged_int(value) == 0:
        prior = load_i32(value, 12) & 64
        pcc_gc_pin(value)
    pcc_py_gc_minor_graph_unlock()
    pcc_gc_frame_leave(borrowed)
    return pcc_gc_take_pinned_slot(ptr_add(borrowed, 8), prior)


@c_abi_export("py_unicode_error_get_field")
def py_unicode_error_get_field(error, index: int):
    # The flag must be checked by dispatch before reaching this accessor.
    borrowed = stack_alloc(16)
    store_ptr(borrowed, 0, error)
    store_ptr(borrowed, 8, null())
    pcc_gc_frame_enter(global_addr("pcc_unicode_borrowed_map"), borrowed)
    pcc_py_gc_minor_graph_lock()
    error = pcc_gc_load_ptr(null(), borrowed)
    payload = pcc_gc_load_ptr(error, ptr_add(error, 24))
    value = pcc_gc_load_ptr(payload, ptr_add(payload, PYTUPLEOBJECT_ITEMS_OFFSET + index * 8))
    if ptr_is_null(value):
        value = global_load_ptr("py_None")
    py_incref(value)
    store_ptr(borrowed, 8, value)
    prior: int = 0
    if is_tagged_int(value) == 0:
        prior = load_i32(value, 12) & 64
        pcc_gc_pin(value)
    pcc_py_gc_minor_graph_unlock()
    pcc_gc_frame_leave(borrowed)
    return pcc_gc_take_pinned_slot(ptr_add(borrowed, 8), prior)


@c_abi_export("py_unicode_error_set_field")
def py_unicode_error_set_field(error, index: int, value) -> int:
    borrowed = stack_alloc(16)
    store_ptr(borrowed, 0, error)
    store_ptr(borrowed, 8, value)
    pcc_gc_frame_enter(global_addr("pcc_unicode_borrowed_map"), borrowed)
    owned = stack_alloc(64)
    memset(owned, 0, 64)
    pcc_gc_frame_enter(global_addr("pcc_unicode_owned_map"), owned)
    ok: int = 1
    if index == 0:
        # CPython changes args independently of the five private fields and
        # consumes an iterable. Preserve exact tuple identity when supplied.
        source_slot = ptr_add(borrowed, 8)
        if _type_of(pcc_gc_load_ptr(null(), source_slot)) == PY_TYPE_TUPLE:
            pcc_py_gc_minor_graph_lock()
            pcc_gc_store_root(ptr_add(owned, 8), pcc_gc_load_ptr(null(), source_slot))
            pcc_py_gc_minor_graph_unlock()
        else:
            prior: int = _unicode_pin_slot(source_slot)
            store_ptr(owned, 8, py_tuple_from_splat(load_ptr(source_slot, 0)))
            store_ptr(source_slot, 0, pcc_gc_take_pinned_slot(source_slot, prior))
            if ptr_is_null(load_ptr(owned, 8)):
                ok = 0
    elif index == 3 or index == 4:
        tag: int = _type_of(pcc_gc_load_ptr(null(), ptr_add(borrowed, 8)))
        if tag != PY_TYPE_INT and tag != PY_TYPE_BOOL:
            py_raise_owned(py_exc_new(3, cstr("an integer is required")))
            ok = 0
        else:
            overflow = stack_alloc(8)
            store_i32(overflow, 0, 0)
            number: int = py_int_to_i64(pcc_gc_load_ptr(null(), ptr_add(borrowed, 8)), overflow)
            if load_i32(overflow, 0) != 0:
                py_raise_owned(py_exc_new(15, cstr("Python int too large to convert to C ssize_t")))
                ok = 0
            else:
                store_ptr(owned, 8, py_int_from_i64(number))
    if ok != 0:
        plan = stack_alloc(128)
        pcc_gc_store_ptr_plan_init(plan, null(), pcc_gc_backend())
        # Payload tuples are internal mutable records. Track unconditionally
        # before publishing a new edge (including a possible cycle).
        error = pcc_gc_load_ptr(null(), borrowed)
        py_gc_track(pcc_gc_load_ptr(error, ptr_add(error, 24)))
        pcc_py_gc_minor_graph_lock()
        error = pcc_gc_load_ptr(null(), borrowed)
        payload = pcc_gc_load_ptr(error, ptr_add(error, 24))
        replacement = pcc_gc_load_ptr(null(), ptr_add(borrowed, 8))
        if index == 0 or index == 3 or index == 4:
            replacement = pcc_gc_load_ptr(null(), ptr_add(owned, 8))
        pcc_gc_store_ptr_plan_commit_locked(plan, payload, ptr_add(payload, PYTUPLEOBJECT_ITEMS_OFFSET + index * 8), replacement)
        pcc_py_gc_minor_graph_unlock()
        # Release the displaced object outside the graph lease. It may run
        # arbitrary finalizers, reenter this setter, or resurrect itself.
        pcc_gc_store_ptr_plan_finish(plan)
    _unicode_finish(borrowed, owned, 0)
    return 0 if ok != 0 else -1


# OSError fields are independent of .args, exactly as in the Unicode record.
# Legacy message-only runtime helpers still convert lazily in the setter. The
# positional constructor below builds the same record in the already traced
# message slot; no exception-layout or collector-specific extension is needed.
pcc_errno_exception_kind = extern("pcc_errno_exception_kind", (c_int32,), c_int64)

# Reuse the existing two-borrowed/eight-owned construction frame. These are
# byte offsets, matching _unicode_tuple_put and _unicode_finish.
_OS_NEW_ERROR = 0
_OS_NEW_PAYLOAD = 8
_OS_NEW_ARGS = 16
_OS_NEW_ERRNO = 24
_OS_NEW_STRERROR = 32
_OS_NEW_FILENAME = 40
_OS_NEW_FILENAME2 = 48
_OS_NEW_TEMP = 56


def _os_error_argument(borrowed, owned, index: int, destination: int) -> int:
    prior: int = _unicode_pin_slot(borrowed)
    store_ptr(owned, destination, py_tuple_get(load_ptr(borrowed, 0), index))
    store_ptr(borrowed, 0, pcc_gc_take_pinned_slot(borrowed, prior))
    return 0 if ptr_is_null(load_ptr(owned, destination)) else 1


def _os_error_constructor_body(borrowed, owned, exception_tag: int) -> int:
    count: int = 0
    prior: int = 0
    if ptr_is_null(pcc_gc_load_ptr(null(), borrowed)):
        store_ptr(owned, _OS_NEW_ARGS, py_tuple_new(0))
        if ptr_is_null(load_ptr(owned, _OS_NEW_ARGS)):
            return 0
    else:
        prior = _unicode_pin_slot(borrowed)
        count = py_tuple_len(load_ptr(borrowed, 0))
        store_ptr(borrowed, 0, pcc_gc_take_pinned_slot(borrowed, prior))
        pcc_py_gc_minor_graph_lock()
        args = pcc_gc_load_ptr(null(), borrowed)
        py_incref(args)
        store_ptr(owned, _OS_NEW_ARGS, args)
        pcc_py_gc_minor_graph_unlock()
    kind: int = exception_tag
    if count >= 2 and count <= 5:
        if _os_error_argument(borrowed, owned, 0, _OS_NEW_ERRNO) == 0:
            return 0
        if _os_error_argument(borrowed, owned, 1, _OS_NEW_STRERROR) == 0:
            return 0
        number_slot = ptr_add(owned, _OS_NEW_ERRNO)
        number_tag: int = _type_of(pcc_gc_load_ptr(null(), number_slot))
        if exception_tag == 14 and (number_tag == PY_TYPE_INT or number_tag == PY_TYPE_BOOL):
            # OSError accepts arbitrary objects, including arbitrary-precision
            # ints. Mapping must neither coerce user objects nor truncate ints.
            overflow = stack_alloc(4)
            store_i32(overflow, 0, 0)
            prior = _unicode_pin_slot(number_slot)
            number: int = py_int_to_i64(load_ptr(number_slot, 0), overflow)
            store_ptr(number_slot, 0, pcc_gc_take_pinned_slot(number_slot, prior))
            if py_err_occurred():
                return 0
            if load_i32(overflow, 0) == 0 and number >= -2147483648 and number <= 2147483647:
                kind = pcc_errno_exception_kind(number)
        if count >= 3:
            if _os_error_argument(borrowed, owned, 2, _OS_NEW_FILENAME) == 0:
                return 0
            filename_slot = ptr_add(owned, _OS_NEW_FILENAME)
            filename = pcc_gc_load_ptr(null(), filename_slot)
            filename_tag: int = _type_of(filename)
            numeric_written: int = kind == 43 and (
                filename_tag == PY_TYPE_INT or filename_tag == PY_TYPE_BOOL
                or filename_tag == PY_TYPE_FLOAT or filename_tag == PY_TYPE_COMPLEX)
            if ptr_eq(filename, global_load_ptr("py_None")) or numeric_written:
                # BlockingIOError's numeric third argument is not a filename.
                # characters_written remains outside this metadata-only slice;
                # keep the full args and the existing absent-filename behavior.
                pcc_gc_store_root(filename_slot, null())
            else:
                if count == 5:
                    if _os_error_argument(borrowed, owned, 4, _OS_NEW_FILENAME2) == 0:
                        return 0
                    second_slot = ptr_add(owned, _OS_NEW_FILENAME2)
                    if ptr_eq(pcc_gc_load_ptr(null(), second_slot), global_load_ptr("py_None")):
                        pcc_gc_store_root(second_slot, null())
                store_ptr(owned, _OS_NEW_TEMP, py_tuple_new(2))
                if ptr_is_null(load_ptr(owned, _OS_NEW_TEMP)):
                    return 0
                _unicode_tuple_put(owned, _OS_NEW_TEMP, 0, _OS_NEW_ERRNO)
                _unicode_tuple_put(owned, _OS_NEW_TEMP, 1, _OS_NEW_STRERROR)
                pcc_py_gc_minor_graph_lock()
                pcc_gc_store_root(ptr_add(owned, _OS_NEW_ARGS),
                    pcc_gc_load_ptr(null(), ptr_add(owned, _OS_NEW_TEMP)))
                pcc_py_gc_minor_graph_unlock()
                pcc_gc_store_root(ptr_add(owned, _OS_NEW_TEMP), null())
    store_ptr(owned, _OS_NEW_PAYLOAD, py_tuple_new(5))
    if ptr_is_null(load_ptr(owned, _OS_NEW_PAYLOAD)):
        return 0
    _unicode_tuple_put(owned, _OS_NEW_PAYLOAD, 0, _OS_NEW_ARGS)
    index: int = 1
    while index < 5:
        field: int = _OS_NEW_ERRNO + (index - 1) * 8
        if not ptr_is_null(pcc_gc_load_ptr(null(), ptr_add(owned, field))):
            _unicode_tuple_put(owned, _OS_NEW_PAYLOAD, index, field)
        index = index + 1
    pcc_gc_publish_initialized(pcc_gc_load_ptr(null(), ptr_add(owned, _OS_NEW_PAYLOAD)))
    store_ptr(owned, _OS_NEW_ERROR, py_exc_new(kind, null()))
    if ptr_is_null(load_ptr(owned, _OS_NEW_ERROR)):
        return 0
    _exc_store_constructed_slot(owned, owned, _OS_NEW_PAYLOAD, 24)
    pcc_py_gc_minor_graph_lock()
    error = pcc_gc_load_ptr(null(), owned)
    atomic_rmw_i32("or", error, 12, PY_FLAG_EXC_OS_PAYLOAD, "relaxed")
    pcc_py_gc_minor_graph_unlock()
    return 1


@c_abi_export("py_os_error_new")
def py_os_error_new(exception_tag: int, args):
    borrowed = stack_alloc(16)
    store_ptr(borrowed, 0, args)
    store_ptr(borrowed, 8, null())
    pcc_gc_frame_enter(global_addr("pcc_unicode_borrowed_map"), borrowed)
    owned = stack_alloc(64)
    memset(owned, 0, 64)
    pcc_gc_frame_enter(global_addr("pcc_unicode_owned_map"), owned)
    success: int = _os_error_constructor_body(borrowed, owned, exception_tag)
    return _unicode_finish(borrowed, owned, success)


def _os_error_prepare_payload(borrowed, owned) -> int:
    error = pcc_gc_load_ptr(null(), borrowed)
    if (load_i32(error, 12) & PY_FLAG_EXC_OS_PAYLOAD) != 0:
        return 1
    pcc_py_gc_minor_graph_lock()
    error = pcc_gc_load_ptr(null(), borrowed)
    pcc_gc_store_root(ptr_add(owned, 24), pcc_gc_load_ptr(error, ptr_add(error, 24)))
    pcc_py_gc_minor_graph_unlock()
    count: int = 0 if ptr_is_null(pcc_gc_load_ptr(null(), ptr_add(owned, 24))) else 1
    store_ptr(owned, 16, py_tuple_new(count))
    if ptr_is_null(load_ptr(owned, 16)):
        return 0
    if count != 0:
        _unicode_tuple_put(owned, 16, 0, 24)
    store_ptr(owned, 8, py_tuple_new(5))
    if ptr_is_null(load_ptr(owned, 8)):
        return 0
    _unicode_tuple_put(owned, 8, 0, 16)
    # NULL optional fields are initialized absence, distinct from assigning
    # the Python None singleton (which OSError.__str__ must render).
    pcc_gc_publish_initialized(pcc_gc_load_ptr(null(), ptr_add(owned, 8)))
    plan = stack_alloc(128)
    pcc_gc_store_ptr_plan_init(plan, null(), pcc_gc_backend())
    py_gc_track(pcc_gc_load_ptr(null(), ptr_add(owned, 8)))
    pcc_py_gc_minor_graph_lock()
    error = pcc_gc_load_ptr(null(), borrowed)
    # A competing setter can have installed the record while we allocated.
    if (load_i32(error, 12) & PY_FLAG_EXC_OS_PAYLOAD) == 0:
        payload = pcc_gc_load_ptr(null(), ptr_add(owned, 8))
        pcc_gc_store_ptr_plan_commit_locked(plan, error, ptr_add(error, 24), payload)
        atomic_rmw_i32("or", error, 12, PY_FLAG_EXC_OS_PAYLOAD, "relaxed")
    pcc_py_gc_minor_graph_unlock()
    pcc_gc_store_ptr_plan_finish(plan)
    pcc_gc_store_root(ptr_add(owned, 8), null())
    pcc_gc_store_root(ptr_add(owned, 16), null())
    pcc_gc_store_root(ptr_add(owned, 24), null())
    return 1


@c_abi_export("py_os_error_get_field")
def py_os_error_get_field(error, index: int):
    # Caller dispatches only an OSError. Uninitialized optional fields are None.
    if (load_i32(error, 12) & PY_FLAG_EXC_OS_PAYLOAD) == 0:
        value = global_load_ptr("py_None")
        py_incref(value)
        return value
    return py_unicode_error_get_field(error, index)


@c_abi_export("py_os_error_set_field")
def py_os_error_set_field(error, index: int, value) -> int:
    borrowed = stack_alloc(16)
    store_ptr(borrowed, 0, error)
    store_ptr(borrowed, 8, value)
    pcc_gc_frame_enter(global_addr("pcc_unicode_borrowed_map"), borrowed)
    owned = stack_alloc(64)
    memset(owned, 0, 64)
    pcc_gc_frame_enter(global_addr("pcc_unicode_owned_map"), owned)
    ok: int = _os_error_prepare_payload(borrowed, owned)
    if ok != 0 and index == 0:
        source = ptr_add(borrowed, 8)
        if _type_of(pcc_gc_load_ptr(null(), source)) == PY_TYPE_TUPLE:
            pcc_py_gc_minor_graph_lock()
            pcc_gc_store_root(ptr_add(owned, 8), pcc_gc_load_ptr(null(), source))
            pcc_py_gc_minor_graph_unlock()
        else:
            prior: int = _unicode_pin_slot(source)
            store_ptr(owned, 8, py_tuple_from_splat(load_ptr(source, 0)))
            store_ptr(source, 0, pcc_gc_take_pinned_slot(source, prior))
            if ptr_is_null(load_ptr(owned, 8)):
                ok = 0
    if ok != 0:
        plan = stack_alloc(128)
        pcc_gc_store_ptr_plan_init(plan, null(), pcc_gc_backend())
        error = pcc_gc_load_ptr(null(), borrowed)
        py_gc_track(pcc_gc_load_ptr(error, ptr_add(error, 24)))
        pcc_py_gc_minor_graph_lock()
        error = pcc_gc_load_ptr(null(), borrowed)
        payload = pcc_gc_load_ptr(error, ptr_add(error, 24))
        replacement = pcc_gc_load_ptr(null(), ptr_add(borrowed, 8))
        if index == 0:
            replacement = pcc_gc_load_ptr(null(), ptr_add(owned, 8))
        pcc_gc_store_ptr_plan_commit_locked(plan, payload, ptr_add(payload, PYTUPLEOBJECT_ITEMS_OFFSET + index * 8), replacement)
        pcc_py_gc_minor_graph_unlock()
        pcc_gc_store_ptr_plan_finish(plan)
    _unicode_finish(borrowed, owned, 0)
    return 0 if ok != 0 else -1


@c_abi_export("py_os_error_fields_present")
def py_os_error_fields_present(error) -> int:
    borrowed = stack_alloc(16)
    store_ptr(borrowed, 0, error)
    store_ptr(borrowed, 8, null())
    pcc_gc_frame_enter(global_addr("pcc_unicode_borrowed_map"), borrowed)
    pcc_py_gc_minor_graph_lock()
    error = pcc_gc_load_ptr(null(), borrowed)
    payload = pcc_gc_load_ptr(error, ptr_add(error, 24))
    present: int = 0
    index: int = 1
    while index < 5:
        if ptr_is_null(pcc_gc_load_ptr(payload, ptr_add(payload, PYTUPLEOBJECT_ITEMS_OFFSET + index * 8))) == 0:
            present = present | (1 << index)
        index = index + 1
    pcc_py_gc_minor_graph_unlock()
    pcc_gc_frame_leave(borrowed)
    return present


# Projection views reuse the named two-borrowed/eight-owned payload frames.
# Their output slot is a NEW owner; the legacy argument must stay independently
# owned across allocation, publication by another setter, and tuple assembly.
_EXC_PROJECT_RESULT = 0
_EXC_PROJECT_ARGUMENT = 8
_EXC_PROJECT_ARGS = 1
_EXC_PROJECT_VALUE = 2


def _exc_project_new(error, projection: int):
    borrowed = stack_alloc(16)
    store_ptr(borrowed, 0, error)
    store_ptr(borrowed, 8, null())
    pcc_gc_frame_enter(global_addr("pcc_unicode_borrowed_map"), borrowed)
    owned = stack_alloc(64)
    memset(owned, 0, 64)
    pcc_gc_frame_enter(global_addr("pcc_unicode_owned_map"), owned)
    legacy_args: int = 0
    pcc_py_gc_minor_graph_lock()
    error = pcc_gc_load_ptr(null(), borrowed)
    flags: int = load_i32(error, 12)
    message = pcc_gc_load_ptr(error, ptr_add(error, 24))
    structured: int = flags & (PY_FLAG_EXC_UNICODE_PAYLOAD | PY_FLAG_EXC_OS_PAYLOAD)
    if projection == _EXC_PROJECT_ARGS:
        if structured != 0:
            args = pcc_gc_load_ptr(message, ptr_add(message, PYTUPLEOBJECT_ITEMS_OFFSET))
            py_incref(args)
            store_ptr(owned, _EXC_PROJECT_RESULT, args)
        else:
            # Retain this exact logical argument before releasing the lease.
            # Reloading error.message after allocation could read a new record.
            py_incref(message)
            store_ptr(owned, _EXC_PROJECT_ARGUMENT, message)
            legacy_args = 1
    elif structured == 0:
        if ptr_is_null(message):
            message = global_load_ptr("py_None")
        py_incref(message)
        store_ptr(owned, _EXC_PROJECT_RESULT, message)
    pcc_py_gc_minor_graph_unlock()
    if legacy_args != 0:
        count: int = 0 if ptr_is_null(pcc_gc_load_ptr(null(), ptr_add(owned, _EXC_PROJECT_ARGUMENT))) else 1
        store_ptr(owned, _EXC_PROJECT_RESULT, py_tuple_new(count))
        if count != 0 and ptr_is_null(load_ptr(owned, _EXC_PROJECT_RESULT)) == 0:
            _unicode_tuple_put(owned, _EXC_PROJECT_RESULT, 0, _EXC_PROJECT_ARGUMENT)
    success: int = 0 if ptr_is_null(load_ptr(owned, _EXC_PROJECT_RESULT)) else 1
    return _unicode_finish(borrowed, owned, success)


@c_abi_export("py_exc_get_args")
def py_exc_get_args(error):
    return _exc_project_new(error, _EXC_PROJECT_ARGS)


@c_abi_export("py_exc_get_legacy_value")
def py_exc_get_legacy_value(error):
    # NULL denotes an attribute miss for structured payloads. An unstructured
    # no-argument error returns a NEW None, preserving the legacy value API.
    return _exc_project_new(error, _EXC_PROJECT_VALUE)
