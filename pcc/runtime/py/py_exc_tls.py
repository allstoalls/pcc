"""Phase 4c.3: pcc-Python port of py_exc_tls.c.

Reimplements the four TLS-facing exception entries:
    py_raise, py_err_occurred, py_current_exception, py_clear_exception

The raw-slot accessors py_tls_exc_get / py_tls_exc_set are owned by the
pcc-Python substrate.  That module defines the native-TLS slot and its
per-thread GC-root handle; this semantic layer calls the accessors via extern
so exception ownership stays separate from the freestanding storage ABI.

PyExceptionObject layout (from py_internal.h):
    offset  0   PyObjectHeader  (i64 refcount, i32 tag, i32 flags — 16 bytes total)
    offset  16  exc_class  (ptr)
    offset  24  message    (ptr)
    offset  32  cause      (ptr)
    offset  40  context    (ptr)
    ...

Tagged-int encoding: low bit of pointer == 1 means tagged int. Non-
tagged pointers come from malloc (8-byte aligned) so low bit is 0.

Public object type tags come from the generated ``py_abi_constants`` module.
The runtime-error exception-table code remains owned by the exception ABI.
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import (
    PYOBJECTHEADER_FLAGS_OFFSET,
    PY_FLAG_GC_PINNED,
    PY_TYPE_CLASS,
    PY_TYPE_EXC,
    PY_TYPE_INSTANCE,
    PY_TYPE_INT,
    PY_TYPE_USER_CLASS_START,
)
from pcc.extern import extern, c_abi_export, c_int32, c_ptr, c_int64, c_void
from pcc.unsafe import (
    cstr,
    define_global_i32,
    global_addr,
    stack_alloc,
    is_tagged_int,
    load_i32,
    load_ptr,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    store_ptr,
)


py_handled_exception_slot = extern("py_handled_exception_slot", (), c_ptr)
py_tls_exc_swap_slot = extern("py_tls_exc_swap_slot", (c_ptr,), c_void)
py_exc_set_implicit_context_slots = extern("py_exc_set_implicit_context_slots", (c_ptr, c_ptr), c_void)

py_tls_exc_get         = extern("py_tls_exc_get",         (),        c_ptr)
py_tls_exc_set         = extern("py_tls_exc_set",         (c_ptr,),  c_void)
py_incref              = extern("py_incref",              (c_ptr,),  c_void)
py_decref              = extern("py_decref",              (c_ptr,),  c_void)
py_exc_new             = extern("py_exc_new",             (c_int64, c_ptr), c_ptr)
py_exc_new_with_class  = extern("py_exc_new_with_class",  (c_ptr, c_ptr), c_ptr)
py_exc_builtin_class   = extern("py_exc_builtin_class",   (c_int64,), c_ptr)
py_isinstance          = extern("py_isinstance",          (c_ptr, c_ptr), c_int64)
py_instance_getattr    = extern("py_instance_getattr",    (c_ptr, c_ptr), c_ptr)
py_obj_str             = extern("py_obj_str",             (c_ptr,), c_ptr)
py_obj_repr            = extern("py_obj_repr",            (c_ptr,), c_ptr)
py_type_builtin        = extern("py_type_builtin",        (c_ptr,), c_ptr)
py_obj_call            = extern("py_obj_call",            (c_ptr, c_ptr, c_ptr), c_ptr)
py_exc_matches         = extern("py_exc_matches",         (c_ptr, c_ptr), c_int64)
py_exc_new_with_value  = extern("py_exc_new_with_value",  (c_int64, c_ptr), c_ptr)
py_str_new             = extern("py_str_new",             (c_ptr, c_int64), c_ptr)
py_str_concat          = extern("py_str_concat",          (c_ptr, c_ptr), c_ptr)
py_tuple_new           = extern("py_tuple_new",           (c_int64,), c_ptr)
py_str_utf8            = extern("py_str_utf8",            (c_ptr,), c_ptr)
pcc_gc_load_ptr        = extern("pcc_gc_load_ptr",        (c_ptr, c_ptr), c_ptr)
pcc_gc_store_ptr       = extern("pcc_gc_store_ptr",       (c_ptr, c_ptr, c_ptr), c_void)
pcc_gc_note_relocation_read = extern(
    "pcc_gc_note_relocation_read", (c_ptr,), c_ptr,
)
pcc_diagnostics_runtime_log_event_code = extern(
    "pcc_diagnostics_runtime_log_event_code", (c_int32, c_int32, c_int64, c_int64, c_ptr), c_void,
)


def _type_of(obj) -> int:
    # Offsets / tag literals inlined to avoid module-level globals
    # (which would require a main() for init). See py_obj_stubs.py.
    if is_tagged_int(obj):
        return PY_TYPE_INT       # PY_TYPE_INT
    obj = pcc_gc_note_relocation_read(obj)
    return load_i32(obj, 8)   # PyObjectHeader.type_tag


def _instance_like(obj) -> int:
    if ptr_is_null(obj):
        return 0
    if is_tagged_int(obj):
        return 0
    tag: int = _type_of(obj)
    if tag == PY_TYPE_INSTANCE:             # PY_TYPE_INSTANCE
        return 1
    if tag >= PY_TYPE_USER_CLASS_START:
        return 1
    return 0


def _take_pending_exception():
    """The pending exception, its TLS owner moved to the caller."""
    current = _resolve_current_exception()
    if ptr_is_null(current):
        return py_exc_new(7, cstr("exception class call failed without an exception"))
    py_tls_exc_set(null())
    return current


def _message_part(message, part):
    """``message + part`` as a new str, consuming both owners."""
    if ptr_is_null(message) or ptr_is_null(part):
        if not ptr_is_null(message):
            py_decref(message)
        if not ptr_is_null(part):
            py_decref(part)
        return null()
    pcc_gc_pin(message)
    pcc_gc_pin(part)
    joined = py_str_concat(message, part)
    pcc_gc_unpin(part)
    pcc_gc_unpin(message)
    py_decref(part)
    py_decref(message)
    return joined


def _raised_class_instance(slot, base):
    """``raise C`` for an exception class C raises ``C()`` (CPython do_raise).

    The exception the call raises, if any, is raised instead, and a call
    that returns anything but an exception instance raises TypeError.
    """
    args = py_tuple_new(0)
    if ptr_is_null(args):
        return _take_pending_exception()
    pcc_gc_pin(args)
    value = py_obj_call(pcc_gc_load_ptr(null(), slot), args, null())
    pcc_gc_unpin(args)
    py_decref(args)
    if ptr_is_null(value):
        return _take_pending_exception()
    if _type_of(value) == PY_TYPE_EXC:
        return value
    if py_isinstance(value, base) != 0 and _instance_like(value) != 0:
        return value
    pcc_gc_pin(value)
    message = _message_part(
        py_str_new(cstr("calling "), 8), py_obj_repr(pcc_gc_load_ptr(null(), slot)),
    )
    message = _message_part(
        message, py_str_new(cstr(" should have returned an instance of BaseException, not "), 56),
    )
    value_type = py_type_builtin(value)
    pcc_gc_unpin(value)
    py_decref(value)
    if ptr_is_null(value_type):
        message = _message_part(message, null())
    else:
        pcc_gc_pin(value_type)
        message = _message_part(message, py_obj_repr(value_type))
        pcc_gc_unpin(value_type)
        py_decref(value_type)
    if ptr_is_null(message):
        return _take_pending_exception()
    pcc_gc_pin(message)
    error = py_exc_new_with_value(3, message)   # PY_EXC_TYPEERROR
    pcc_gc_unpin(message)
    py_decref(message)
    return error


def _normalize_raised(slot):
    # Returns a reference owned by the exception slot. Existing
    # PyExceptionObject values are incref'd; freshly normalized errors
    # already carry refcount=1 from py_exc_new*.
    exc = pcc_gc_load_ptr(null(), slot)
    if ptr_is_null(exc):
        return py_exc_new(
            7,  # PY_EXC_RUNTIMEERROR
            cstr("no active exception to reraise"),
        )
    if _type_of(exc) == PY_TYPE_EXC:              # PY_TYPE_EXC
        exc = pcc_gc_load_ptr(null(), slot)
        py_incref(exc)
        return pcc_gc_load_ptr(null(), slot)

    base = py_exc_builtin_class(0)       # PY_EXC_BASE
    if not ptr_is_null(base):
        raised = pcc_gc_load_ptr(null(), slot)
        if _type_of(raised) == PY_TYPE_CLASS and py_exc_matches(raised, base) != 0:
            return _raised_class_instance(slot, base)
        if py_isinstance(pcc_gc_load_ptr(null(), slot), base) != 0:
            if _instance_like(pcc_gc_load_ptr(null(), slot)) != 0:
                # A raised user exception subclass *instance*: keep it AS-IS so
                # the instance attributes set by __init__ (e.g. self.code)
                # survive. exc_to_class now projects an instance to its class
                # for except-matching. Previously this wrapped the instance in
                # a fresh PY_TYPE_EXC carrying only a message string, discarding
                # every user attribute. Incref to match the slot-owned contract.
                exc = pcc_gc_load_ptr(null(), slot)
                py_incref(exc)
                return pcc_gc_load_ptr(null(), slot)

            # Non-instance-like BaseException: wrap with a message only.
            msg_c = null()
            msg_str = py_obj_str(exc)
            if not ptr_is_null(msg_str):
                msg_c = py_str_utf8(msg_str)
            normalized = py_exc_new_with_class(null(), msg_c)
            if not ptr_is_null(msg_str):
                py_decref(msg_str)
            if not ptr_is_null(normalized):
                return normalized

    return py_exc_new(
        3,  # PY_EXC_TYPEERROR
        cstr("exceptions must derive from BaseException"),
    )


@c_abi_export("py_err_occurred")
def py_err_occurred() -> int:
    cur = py_tls_exc_get()
    if ptr_is_null(cur):
        return 0
    return 1


def _resolve_current_exception():
    cur = py_tls_exc_get()
    if ptr_is_null(cur):
        return cur
    resolved = pcc_gc_note_relocation_read(cur)
    if ptr_eq(resolved, cur) == 0:
        py_incref(resolved)
        py_tls_exc_set(resolved)
        py_decref(cur)
        cur = resolved
    return cur


@c_abi_export("py_current_exception")
def py_current_exception():
    # Borrowed reference — TLS still owns it.
    return _resolve_current_exception()


@c_abi_export("py_clear_exception")
def py_clear_exception() -> None:
    cur = _resolve_current_exception()
    if not ptr_is_null(cur):
        pcc_diagnostics_runtime_log_event_code(6, 4, _type_of(cur), 0, cur)
        py_decref(cur)
        py_tls_exc_set(null())


def _handled_exception():
    slot = py_handled_exception_slot()
    if ptr_is_null(slot):
        return null()
    return pcc_gc_load_ptr(null(), slot)


@c_abi_export("py_raise_rooted")
def py_raise_rooted(borrowed, owned) -> None:
    if ptr_is_null(pcc_gc_load_ptr(null(), borrowed)):
        store_ptr(borrowed, 0, _handled_exception())
    store_ptr(owned, 0, _normalize_raised(borrowed))
    exc = pcc_gc_load_ptr(null(), owned)
    if ptr_is_null(exc):
        pcc_diagnostics_runtime_log_event_code(6, 3, -1, 0, exc)
    else:
        pcc_diagnostics_runtime_log_event_code(6, 3, _type_of(exc), 0, exc)
    current = _resolve_current_exception()
    if ptr_is_null(current):
        current = _handled_exception()
    store_ptr(borrowed, 8, current)
    py_exc_set_implicit_context_slots(owned, ptr_add(borrowed, 8))
    # Transfer normalized ownership to pending TLS; its displaced old owner
    # returns in the same registered slot and is released by the raw wrapper.
    py_tls_exc_swap_slot(owned)

pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
pcc_gc_unpin = extern("pcc_gc_unpin", (c_ptr,), c_void)
pcc_gc_take_pinned_slot = extern("pcc_gc_take_pinned_slot", (c_ptr, c_int64), c_ptr)
pcc_gc_frame_enter = extern("pcc_gc_frame_enter", (c_ptr, c_ptr), c_void)
pcc_gc_frame_leave = extern("pcc_gc_frame_leave", (c_ptr,), c_void)
pcc_gc_store_root = extern("pcc_gc_store_root", (c_ptr, c_ptr), c_void)
define_global_i32("pcc_raise_borrowed_frame_map", -2)
define_global_i32("pcc_raise_owned_frame_map", 1)


@c_abi_export("py_raise")
def py_raise(exc: c_ptr) -> None:
    exc = pcc_gc_note_relocation_read(exc)
    borrowed = stack_alloc(16)
    owned = stack_alloc(8)
    store_ptr(borrowed, 0, exc)
    store_ptr(borrowed, 8, null())
    store_ptr(owned, 0, null())
    prior: int = 0
    if ptr_is_null(exc) == 0 and is_tagged_int(exc) == 0:
        prior = load_i32(exc, PYOBJECTHEADER_FLAGS_OFFSET) & PY_FLAG_GC_PINNED
    pcc_gc_pin(exc)
    pcc_gc_frame_enter(global_addr("pcc_raise_borrowed_frame_map"), borrowed)
    pcc_gc_frame_enter(global_addr("pcc_raise_owned_frame_map"), owned)
    store_ptr(borrowed, 0, pcc_gc_take_pinned_slot(borrowed, prior))
    py_raise_rooted(borrowed, owned)
    # The displaced pending owner may be the last reference to a context.
    # Retire all borrowed aliases before releasing it or unregistering roots.
    store_ptr(borrowed, 0, null())
    store_ptr(borrowed, 8, null())
    pcc_gc_store_root(owned, null())
    pcc_gc_frame_leave(owned)
    pcc_gc_frame_leave(borrowed)


@c_abi_export("py_raise_owned")
def py_raise_owned(exc: c_ptr) -> None:
    exc = pcc_gc_note_relocation_read(exc)
    owned = stack_alloc(8)
    store_ptr(owned, 0, exc)
    prior: int = 0
    if ptr_is_null(exc) == 0 and is_tagged_int(exc) == 0:
        prior = load_i32(exc, PYOBJECTHEADER_FLAGS_OFFSET) & PY_FLAG_GC_PINNED
    pcc_gc_pin(exc)
    pcc_gc_frame_enter(global_addr("pcc_raise_owned_frame_map"), owned)
    store_ptr(owned, 0, pcc_gc_take_pinned_slot(owned, prior))
    py_raise(load_ptr(owned, 0))
    pcc_gc_store_root(owned, null())
    pcc_gc_frame_leave(owned)
