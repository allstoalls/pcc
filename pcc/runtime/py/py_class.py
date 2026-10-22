"""Phase 4c.14: pcc-Python port of py_class.c.

Public ``PyClassObject``, ``PyClassMethod``, ``PyInstanceObject``, descriptor,
header, flag, and type-tag ABI values come from the generated
``py_abi_constants`` module. Numeric copies do not belong in this prose: the
C headers and generator are the layout authority.

Notes on int width: py_class_new / py_instance_get_field / py_instance_
set_field / py_instance_setattr / py_isinstance use int32 in their C
ABI. pcc-Python's int → i64 default applies inside the function body
for arithmetic / comparison (auto-sext) but NOT for direct user-helper
calls. So we keep i32 args (via `: int` which the ABI forces to i32),
inline most logic, and only call extern (C) helpers where the C side
handles its own int width.
"""

__pcc_runtime_port__ = True

from pcc.extern import (
    extern,
    c_abi_export,
    c_ptr,
    c_int32,
    c_int64,
    c_void,
)
from pcc.runtime.py.py_abi_constants import (
    PY_FLAG_CLASS_ATTRS_EXPOSED,
    PY_FLAG_FUNC_BUILTIN,
    PY_TYPE_NONE,
    PYSTROBJECT_BYTE_LEN_OFFSET,
    PYSTROBJECT_DATA_OFFSET,
    PYTUPLEOBJECT_ITEMS_OFFSET,
)
from pcc.runtime.py.py_abi_constants import (
    PY_FLAG_FUNC_TRANSPARENT_CALL,
    C_POINTER_SIZE,
    DICTENTRY_KEY_OFFSET,
    DICTENTRY_SIZE,
    DICTENTRY_VALUE_OFFSET,
    PYCLASSMETHOD_FUNC_OFFSET,
    PYCLASSMETHOD_NAME_HASH_OFFSET,
    PYCLASSMETHOD_NAME_LENGTH_OFFSET,
    PYCLASSMETHOD_NAME_OFFSET,
    PYCLASSMETHOD_SIZE,
    PYCLASSMETHODOBJECT_FUNC_OFFSET,
    PYCLASSMETHODOBJECT_SIZE,
    PYCLASSOBJECT_ATTRS_OFFSET,
    PYCLASSOBJECT_BASES_OFFSET,
    PYCLASSOBJECT_DEL_METHOD_OFFSET,
    PYCLASSOBJECT_FIELD_NAMES_OFFSET,
    PYCLASSOBJECT_INSTANCE_SIZE_OFFSET,
    PYCLASSOBJECT_METACLASS_OFFSET,
    PYCLASSOBJECT_METHODS_OFFSET,
    PYCLASSOBJECT_MRO_OFFSET,
    PYCLASSOBJECT_NAME_OFFSET,
    PYCLASSOBJECT_N_BASES_OFFSET,
    PYCLASSOBJECT_N_FIELDS_OFFSET,
    PYCLASSOBJECT_N_METHODS_OFFSET,
    PYCLASSOBJECT_N_MRO_OFFSET,
    PYCLASSOBJECT_SIZE,
    PYCLASSOBJECT_TYPE_TAG_ALLOC_OFFSET,
    PYDICTOBJECT_ENTRIES_OFFSET,
    PYDICTOBJECT_ENTRIES_USED_OFFSET,
    PYINSTANCEOBJECT_CLS_OFFSET,
    PYINSTANCEOBJECT_FIELDS_OFFSET,
    PYINSTANCEOBJECT_SIZE,
    PYOBJECTHEADER_FLAGS_OFFSET,
    PYOBJECTHEADER_REFCOUNT_OFFSET,
    PYOBJECTHEADER_TYPE_TAG_OFFSET,
    PY_FLAG_GC_MALLOC_ALLOC,
    PY_FLAG_GC_PINNED,
    PY_FLAG_IMMORTAL,
    PYPROPERTYOBJECT_FDEL_OFFSET,
    PYPROPERTYOBJECT_FGET_OFFSET,
    PYPROPERTYOBJECT_FSET_OFFSET,
    PYPROPERTYOBJECT_SIZE,
    PYSTATICMETHODOBJECT_FUNC_OFFSET,
    PYSTATICMETHODOBJECT_SIZE,
    PY_TYPE_CLASS,
    PY_TYPE_CLASSMETHOD,
    PY_TYPE_DICT,
    PY_TYPE_EXC,
    PY_TYPE_FUNC,
    PY_TYPE_INSTANCE,
    PY_TYPE_INT,
    PY_TYPE_LIST,
    PY_TYPE_PROPERTY,
    PY_TYPE_STATICMETHOD,
    PY_TYPE_STR,
    PY_TYPE_TUPLE,
    PY_TYPE_USER_CLASS_START,
    PY_TYPE_VALUEBOX,
)
from pcc.unsafe import (
    atomic_cas_i64,
    define_global_ptr_null,
    unsigned_div_i64,
    atomic_load_i64,
    atomic_rmw_i32,
    atomic_load_i32,
    cstr,
    define_global_i32,
    define_global_i64,
    define_thread_local_i32,
    define_thread_local_ptr_null,
    call_ptr1,
    call_ptr2,
    call_ptr3,
    call_ptr4,
    free,
    function_addr,
    global_addr,
    global_load_ptr,
    global_store_ptr,
    is_tagged_int,
    untag_int,
    int_to_ptr,
    load_i8,
    load_i32,
    load_i64,
    load_ptr,
    malloc,
    memcpy,
    memmove,
    memset,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    ptr_to_int,
    realloc,
    stack_alloc,
    store_i8,
    store_i32,
    store_i64,
    store_ptr,
    strlen,
    untag_int,
)

# Number of classes that ever had ``__del__`` installed (class body or a later
# ``cls.__del__ = f``).  Never decremented: a stale nonzero count only costs
# the lookup.  ``py_user_del_dispatch`` skips its per-dealloc MRO walk while
# this is zero -- the common program, and every pcc1 compile.  Mirrors
# ``pcc_class_del_defined_count`` in py_substrate.c / py_class.c.
define_global_i32("pcc_class_del_defined_count", 0)
define_global_i64("pcc_object_new_cache_mutex_bits", 0)
# object's members (CPython vars(object)): built once, scheduler-rooted.
define_global_ptr_null("pcc_object_namespace")
define_global_i32("pcc_object_namespace_registered", 0)
define_global_i64("pcc_object_namespace_mutex_bits", 0)
# The builtin bases' native namespaces (BaseException, KeyError, dict).
define_global_ptr_null("pcc_exception_namespace")
define_global_ptr_null("pcc_keyerror_namespace")
define_global_ptr_null("pcc_dict_namespace")
define_global_i32("pcc_builtin_namespaces_registered", 0)
define_global_i32("pcc_class_construct_owned_frame_map", 5)
define_global_i32("pcc_class_construct_borrowed_frame_map", -2)


py_incref = extern("py_incref", (c_ptr,), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_str_utf8 = extern("py_str_utf8", (c_ptr,), c_ptr)
py_str_new = extern("py_str_new", (c_ptr, c_int64), c_ptr)
py_str_eq = extern("py_str_eq", (c_ptr, c_ptr), c_int64)
py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_tuple_get = extern("py_tuple_get", (c_ptr, c_int64), c_ptr)
py_tuple_len = extern("py_tuple_len", (c_ptr,), c_int64)
py_tuple_set_item = extern("py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void)
py_obj_call = extern("py_obj_call", (c_ptr, c_ptr, c_ptr), c_ptr)
py_obj_call_default = extern("py_obj_call_default", (c_ptr, c_ptr, c_ptr), c_ptr)
py_obj_call_context_is_deferred = extern("py_obj_call_context_is_deferred", (), c_int64)
py_obj_call_slots_sync = extern("py_obj_call_slots_sync", (c_ptr, c_ptr, c_ptr, c_ptr), c_int64)
py_tls_exc_swap_slot = extern("py_tls_exc_swap_slot", (c_ptr,), c_void)
pcc_gc_root_copy_lease = extern("pcc_gc_root_copy_lease", (c_ptr, c_ptr), c_int64)
pcc_gc_root_copy_borrowed_lease = extern("pcc_gc_root_copy_borrowed_lease", (c_ptr, c_ptr), c_int64)
pcc_gc_root_move = extern("pcc_gc_root_move", (c_ptr, c_ptr), c_int64)
pcc_gc_foreign_lease_acquire = extern("pcc_gc_foreign_lease_acquire", (c_ptr,), c_int64)
pcc_gc_foreign_lease_release = extern("pcc_gc_foreign_lease_release", (c_ptr, c_int64), c_int64)
pcc_gc_root_copy_lease_prepare_locked = extern("pcc_gc_root_copy_lease_prepare_locked", (c_ptr, c_ptr, c_int64, c_ptr), c_int64)
pcc_gc_root_copy_lease_finish = extern("pcc_gc_root_copy_lease_finish", (c_ptr,), c_void)
pcc_gc_resolve_root_slot_unlocked = extern("pcc_gc_resolve_root_slot_unlocked", (c_ptr, c_int64), c_ptr)
py_obj_getattr = extern("py_obj_getattr", (c_ptr, c_ptr), c_ptr)
py_func_call_kwargs = extern("py_func_call_kwargs", (c_ptr, c_ptr, c_ptr), c_ptr)
py_call_validate_kwargs = extern("py_call_validate_kwargs", (c_ptr,), c_int64)
py_dict_subclass_getattr = extern("py_dict_subclass_getattr", (c_ptr, c_ptr), c_ptr)
py_func_call_bound_forward = extern("py_func_call_bound_forward", (c_ptr, c_ptr), c_ptr)
py_func_new_bound = extern(
    "py_func_new_bound", (c_ptr, c_ptr, c_ptr, c_ptr), c_ptr
)
py_class_new_abi = extern(
    "py_class_new", (c_ptr, c_ptr, c_int32, c_ptr, c_int32), c_ptr
)
py_dict_new = extern("py_dict_new", (), c_ptr)
py_dict_set = extern("py_dict_set", (c_ptr, c_ptr, c_ptr), c_void)
py_dict_get = extern("py_dict_get", (c_ptr, c_ptr), c_ptr)
py_dict_len = extern("py_dict_len", (c_ptr,), c_int64)
py_dict_keys = extern("py_dict_keys", (c_ptr,), c_ptr)
py_dict_items = extern("py_dict_items", (c_ptr,), c_ptr)
py_dict_storage_slots = extern("py_dict_storage_slots", (c_ptr, c_ptr), c_int64)
py_dict_get_default_slots = extern("py_dict_get_default_slots", (c_ptr, c_ptr, c_ptr, c_ptr), c_int64)
py_dict_update = extern("py_dict_update", (c_ptr, c_ptr), c_void)
py_dict_del = extern("py_dict_del", (c_ptr, c_ptr), c_int64)
py_builtin_type_class_tag = extern("py_builtin_type_class_tag", (c_ptr,), c_int32)
py_subs_alloc_user_tag = extern("py_subs_alloc_user_tag", (), c_int32)
py_subs_object_root = extern("py_subs_object_root", (), c_ptr)
pcc_capi_is_cext_type_tag = extern("pcc_capi_is_cext_type_tag", (c_int64,), c_int64)
py_int_from_i64 = extern("py_int_from_i64", (c_int64,), c_ptr)
py_bool_from_bit = extern("py_bool_from_bit", (c_int32,), c_ptr)
py_slice_index_i64 = extern("py_slice_index_i64", (c_ptr, c_int64), c_int64)
py_str_tailmatch_range = extern(
    "py_str_tailmatch_range", (c_ptr, c_ptr, c_int64, c_int64, c_int64), c_int64
)
py_str_count_range = extern("py_str_count_range", (c_ptr, c_ptr, c_ptr, c_ptr), c_int64)
pcc_mutex_new = extern("pcc_mutex_new", (), c_ptr)
pcc_mutex_free = extern("pcc_mutex_free", (c_ptr,), c_void)
pcc_mutex_lock = extern("pcc_mutex_lock", (c_ptr,), c_int64)
pcc_mutex_unlock = extern("pcc_mutex_unlock", (c_ptr,), c_int64)
pcc_gc_scheduler_root_register_handle = extern(
    "pcc_gc_scheduler_root_register_handle", (c_ptr,), c_ptr
)
pcc_gc_scheduler_root_unregister_handle = extern(
    "pcc_gc_scheduler_root_unregister_handle", (c_ptr,), c_void
)
py_list_len = extern("py_list_len", (c_ptr,), c_int64)
py_list_get = extern("py_list_get", (c_ptr, c_int64), c_ptr)
py_gc_track = extern("py_gc_track", (c_ptr,), c_void)
py_gc_untrack = extern("py_gc_untrack", (c_ptr,), c_void)
pcc_gc_note_object_freeing = extern(
    "pcc_gc_note_object_freeing", (c_ptr,), c_void
)
pcc_gc_free_watch_begin = extern(
    "pcc_gc_free_watch_begin", (c_ptr, c_ptr), c_void
)
pcc_gc_free_watch_end = extern("pcc_gc_free_watch_end", (c_ptr,), c_int64)
# pcc_gc_free_watch_begin's record (layout owned by py_gc_backend.py).
GC_FREE_WATCH_BYTES = 24
pcc_gc_object_index_find = extern(
    "pcc_gc_object_index_find", (c_ptr,), c_ptr
)
py_user_del_dispatch = extern("py_user_del_dispatch", (c_ptr,), c_void)
py_weakref_invalidate = extern("py_weakref_invalidate", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_runtime_error_if_unset = extern(
    "py_runtime_error_if_unset", (c_ptr, c_ptr), c_ptr
)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_current_exception = extern("py_current_exception", (), c_ptr)
py_clear_exception = extern("py_clear_exception", (), c_void)
py_exc_builtin_class = extern("py_exc_builtin_class", (c_int64,), c_ptr)
py_exc_matches = extern("py_exc_matches", (c_ptr, c_ptr), c_int64)
pcc_gc_store_ptr = extern("pcc_gc_store_ptr", (c_ptr, c_ptr, c_ptr), c_void)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_gc_note_relocation_read = extern("pcc_gc_note_relocation_read", (c_ptr,), c_ptr)
pcc_gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_ptr)
pcc_gc_publish_initialized = extern(
    "pcc_gc_publish_initialized", (c_ptr,), c_void
)
pcc_gc_pointer_register = extern(
    "pcc_gc_pointer_register", (c_ptr,), c_int64
)
pcc_gc_free_object_memory = extern("pcc_gc_free_object_memory", (c_ptr,), c_void)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)
pcc_gc_backend = extern("pcc_gc_backend", (), c_int64)
pcc_gc_note_store = extern("pcc_gc_note_store", (), c_void)
pcc_gc_note_write_barrier = extern(
    "pcc_gc_note_write_barrier",
    (c_ptr, c_ptr),
    c_void,
)
pcc_gc_note_slot_write_barrier = extern(
    "pcc_gc_note_slot_write_barrier",
    (c_ptr, c_ptr, c_ptr),
    c_void,
)
pcc_gc_backend4_zpage_register_owner_payload_span = extern(
    "pcc_gc_backend4_zpage_register_owner_payload_span",
    (c_ptr, c_ptr, c_int64),
    c_int64,
)
pcc_gc_backend4_zpage_unregister_owner_payload_span = extern(
    "pcc_gc_backend4_zpage_unregister_owner_payload_span",
    (c_ptr, c_ptr),
    c_int64,
)
pcc_gc_backend4_zpage_retarget_owner_payload_span = extern(
    "pcc_gc_backend4_zpage_retarget_owner_payload_span",
    (c_ptr, c_ptr, c_ptr, c_int64),
    c_int64,
)


# Helpers below take only ptrs / cstrs (no int args). They can be
# called from i32-param functions because pcc-Python doesn't need to
# sext anything at the call boundary.


def _class_require_result(result, helper_name, message):
    if ptr_is_null(result) != 0:
        py_runtime_error_if_unset(helper_name, message)
    return result


def _alloc_user_tag() -> int:
    # The substrate owns the shared counter, reserved tags and exhaustion.
    return py_subs_alloc_user_tag()


def _object_root():
    # The substrate owns the canonical root's identity, flags and lifetime.
    return py_subs_object_root()


pcc_gc_pointer_is_managed = extern(
    "pcc_gc_pointer_is_managed", (c_ptr,), c_int64
)


def _ptr_can_have_header(o) -> bool:
    return pcc_gc_pointer_is_managed(o) != 0


def _ptr_is_class_of_validated_instance(cls) -> bool:
    """`_ptr_is_class` without the provenance probe, for a class pointer that
    came out of an already-validated instance's `cls` slot.

    The caller has just proved `o` is a managed object whose type tag says
    instance.  `py_instance_new` only ever stores a validated class there, the
    slot is documented as a borrowed uncounted class pointer, and classes carry
    PY_FLAG_IMMORTAL -- so reading this header is safe on the strength of the
    instance being valid.  Probing again cost a second lock plus hash probe on
    the hottest path in the runtime: every attribute access and method dispatch
    ran `_ptr_is_instance`, which paid the probe once for the instance and once
    more for its class.

    Note what this does NOT protect against either way: a stray over-release
    that frees the class and lets the address be reused answers "managed" from
    the index too (see the self-host over-release notes in AGENTS.md).  The
    probe was not buying safety against that; it only excluded a genuinely
    foreign pointer, which this slot cannot hold.  The relocation read is kept.
    """
    cls = pcc_gc_note_relocation_read(cls)
    if ptr_is_null(cls) != 0:
        return False
    return load_i32(cls, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_CLASS


def _ptr_is_class(o) -> bool:
    o = pcc_gc_note_relocation_read(o)
    if not _ptr_can_have_header(o):
        return False
    return load_i32(o, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_CLASS


def _note_class_defines_del() -> None:
    n: int = load_i32(global_addr("pcc_class_del_defined_count"), 0)
    store_i32(global_addr("pcc_class_del_defined_count"), 0, n + 1)


def _gc_backend_selected_fast() -> int:
    if load_i32(global_addr("pcc_gc_config_initialized"), 0) == 0:
        return pcc_gc_backend()
    return load_i32(global_addr("pcc_gc_backend_selected"), 0)


def _dealloc_ptr_is_instance(o) -> bool:
    # ``_ptr_is_instance`` for an object whose header this thread is about to
    # free: provenance is already established (we hold the last reference),
    # so the radix-walk ``_ptr_can_have_header`` probe is skipped.
    o = pcc_gc_note_relocation_read(o)
    tag: int = load_i32(o, PYOBJECTHEADER_TYPE_TAG_OFFSET)
    if tag != PY_TYPE_INSTANCE:
        if tag < PY_TYPE_USER_CLASS_START:
            return False
    cls = pcc_gc_load_ptr(o, ptr_add(o, PYINSTANCEOBJECT_CLS_OFFSET))
    if ptr_is_null(cls) != 0:
        return False
    return _ptr_is_class_of_validated_instance(cls)


def _ptr_is_instance(o) -> bool:
    o = pcc_gc_note_relocation_read(o)
    if not _ptr_can_have_header(o):
        return False
    tag: int = load_i32(o, PYOBJECTHEADER_TYPE_TAG_OFFSET)
    if tag != PY_TYPE_INSTANCE:
        if tag < PY_TYPE_USER_CLASS_START:
            return False
    cls = pcc_gc_load_ptr(o, ptr_add(o, PYINSTANCEOBJECT_CLS_OFFSET))
    if ptr_is_null(cls) != 0:
        return False
    return _ptr_is_class_of_validated_instance(cls)


def _class_note_borrowed_metadata_store(cls, value) -> None:
    _class_note_borrowed_metadata_slot_store(cls, null(), value)


def _class_note_borrowed_metadata_slot_store(cls, slot, value) -> None:
    if not _ptr_is_class(cls):
        return
    backend: int = pcc_gc_backend()
    if backend == 1 or backend == 2 or backend == 3 or backend == 4:
        pcc_gc_note_store()
    pcc_gc_note_slot_write_barrier(cls, slot, value)


def _strs_eq(a, b) -> int:
    if ptr_eq(a, b) != 0:
        return 1
    if ptr_is_null(a) != 0:
        return 0
    if ptr_is_null(b) != 0:
        return 0
    a0: int = load_i8(a, 0) & 0xFF
    b0: int = load_i8(b, 0) & 0xFF
    if a0 != b0:
        return 0
    if a0 == 0:
        return 1
    a1: int = load_i8(a, 1) & 0xFF
    b1: int = load_i8(b, 1) & 0xFF
    if a1 != b1:
        return 0
    if a1 == 0:
        return 1
    # One pass, comparing terminators, instead of strlen(a) + strlen(b) + a
    # bounded byte loop.  Those three walked the name up to three times for
    # every method and every field on every attribute access; the C mirror in
    # py_class.c always did this in one pass because it calls strcmp.  Exactly
    # equivalent: unequal lengths are caught when one side reaches its NUL
    # while the other has not, and equal-length strings are decided at the
    # first differing byte or at the shared terminator.  Bytes 0 and 1 are
    # already known equal and nonzero, so the scan resumes at index 2.
    result: int = -1
    i: int = 2
    while result < 0:
        ca: int = load_i8(a, i) & 0xFF
        cb: int = load_i8(b, i) & 0xFF
        if ca != cb:
            result = 0
        elif ca == 0:
            result = 1
        else:
            i = i + 1
    return result


def _method_name_signature(name, out) -> None:
    """Write the hash and length without allocating a Python result tuple."""
    value: int = 2166136261
    length: int = 0
    while True:
        char: int = load_i8(name, length) & 0xFF
        if char == 0:
            store_i32(out, 0, value)
            store_i32(out, 4, length & 0xFFFFFFFF)
            return
        value = ((value ^ char) * 16777619) & 0xFFFFFFFF
        length = length + 1


def _cstr_is_dunder_name(s) -> int:
    if load_i8(s, 0) != 95:
        return 0
    if load_i8(s, 1) != 95:
        return 0
    if strlen(s) != 8:
        return 0
    if load_i8(s, 2) != 110:
        return 0
    if load_i8(s, 3) != 97:
        return 0
    if load_i8(s, 4) != 109:
        return 0
    if load_i8(s, 5) != 101:
        return 0
    if load_i8(s, 6) != 95:
        return 0
    if load_i8(s, 7) != 95:
        return 0
    return 1


def _cstr_is_dunder_mro(s) -> int:
    if load_i8(s, 0) != 95:
        return 0
    if load_i8(s, 1) != 95:
        return 0
    if strlen(s) != 7:
        return 0
    if load_i8(s, 2) != 109:
        return 0
    if load_i8(s, 3) != 114:
        return 0
    if load_i8(s, 4) != 111:
        return 0
    if load_i8(s, 5) != 95:
        return 0
    if load_i8(s, 6) != 95:
        return 0
    return 1


def _cstr_is_dunder_class(s) -> int:
    if load_i8(s, 0) != 95:
        return 0
    if load_i8(s, 1) != 95:
        return 0
    if strlen(s) != 9:
        return 0
    if load_i8(s, 2) != 99:
        return 0
    if load_i8(s, 3) != 108:
        return 0
    if load_i8(s, 4) != 97:
        return 0
    if load_i8(s, 5) != 115:
        return 0
    if load_i8(s, 6) != 115:
        return 0
    if load_i8(s, 7) != 95:
        return 0
    if load_i8(s, 8) != 95:
        return 0
    return 1


def _cstr_is_dunder_dict(s) -> int:
    if load_i8(s, 0) != 95:
        return 0
    if load_i8(s, 1) != 95:
        return 0
    if strlen(s) != 8:
        return 0
    if load_i8(s, 2) != 100:
        return 0
    if load_i8(s, 3) != 105:
        return 0
    if load_i8(s, 4) != 99:
        return 0
    if load_i8(s, 5) != 116:
        return 0
    if load_i8(s, 6) != 95:
        return 0
    if load_i8(s, 7) != 95:
        return 0
    return 1


def _class_lookup_in_mro(cls, name):
    signature = stack_alloc(8)
    _method_name_signature(name, signature)
    name_hash: int = load_i32(signature, 0) & 0xFFFFFFFF
    name_length: int = load_i32(signature, 4)
    n_mro_i32: int = load_i32(cls, PYCLASSOBJECT_N_MRO_OFFSET)
    mro = load_ptr(cls, PYCLASSOBJECT_MRO_OFFSET)
    i: int = 0
    while i < n_mro_i32:
        m = pcc_gc_load_ptr(cls, ptr_add(mro, i * 8))
        if ptr_is_null(m) == 0:
            n_methods_i32: int = load_i32(m, PYCLASSOBJECT_N_METHODS_OFFSET)
            methods = load_ptr(m, PYCLASSOBJECT_METHODS_OFFSET)
            j: int = 0
            while j < n_methods_i32:
                m_off: int = j * PYCLASSMETHOD_SIZE
                if load_i32(methods, m_off + PYCLASSMETHOD_NAME_LENGTH_OFFSET) == name_length:
                    stored_hash: int = load_i32(
                        methods, m_off + PYCLASSMETHOD_NAME_HASH_OFFSET
                    ) & 0xFFFFFFFF
                    if stored_hash == name_hash:
                        m_name = load_ptr(methods, m_off + PYCLASSMETHOD_NAME_OFFSET)
                        if _strs_eq(m_name, name) != 0:
                            method_slot = ptr_add(
                                methods, m_off + PYCLASSMETHOD_FUNC_OFFSET
                            )
                            func = pcc_gc_note_relocation_read(load_ptr(method_slot, 0))
                            store_ptr(method_slot, 0, func)
                            return func
                j = j + 1
        i = i + 1
    return null()


def _lookup_field_index(cls, name):
    if not _ptr_is_class(cls):
        return -1
    if ptr_is_null(name) != 0:
        return -1
    n_fields_i32: int = load_i32(cls, PYCLASSOBJECT_N_FIELDS_OFFSET)
    field_names = load_ptr(cls, PYCLASSOBJECT_FIELD_NAMES_OFFSET)
    if ptr_is_null(field_names) != 0:
        return -1
    i: int = 0
    while i < n_fields_i32:
        fn = load_ptr(field_names, i * C_POINTER_SIZE)
        # print("  field[" + str(i) + "]=" + str(fn))
        if _strs_eq(fn, name) != 0:
            return i
        i = i + 1
    return -1


def _class_attr_cache_epoch() -> int:
    return atomic_load_i32(
        global_addr("py_class_attr_cache_epoch"), 0, "acquire"
    )


def _bump_class_attr_cache_epoch() -> None:
    atomic_rmw_i32(
        "add", global_addr("py_class_attr_cache_epoch"), 0, 1, "release"
    )


@c_abi_export("py_class_attrs_dict")
def py_class_attrs_dict(cls, create: int):
    if not _ptr_is_class(cls):
        return null()
    cls = pcc_gc_note_relocation_read(cls)
    attrs = pcc_gc_load_ptr(cls, ptr_add(cls, PYCLASSOBJECT_ATTRS_OFFSET))
    if ptr_is_null(attrs) != 0 and create != 0:
        created = py_dict_new()
        if ptr_is_null(created) != 0:
            return null()
        pcc_gc_store_ptr(cls, ptr_add(cls, PYCLASSOBJECT_ATTRS_OFFSET), created)
        py_decref(created)
        attrs = pcc_gc_load_ptr(cls, ptr_add(cls, PYCLASSOBJECT_ATTRS_OFFSET))
    if ptr_is_null(attrs) == 0 and create != 0:
        # Exposing this mutable dict can later install a descriptor without
        # going through py_class_setattr_raw. Retire field-lookup proofs the
        # first time it is handed out, before a caller can mutate it. Class
        # creation already writes __doc__, so this is not tied to creating
        # the dict.
        previous: int = atomic_rmw_i32(
            "or", cls, PYOBJECTHEADER_FLAGS_OFFSET, PY_FLAG_CLASS_ATTRS_EXPOSED, "acq_rel"
        )
        if (previous & PY_FLAG_CLASS_ATTRS_EXPOSED) == 0:
            _bump_class_attr_cache_epoch()
    return attrs


@c_abi_export("py_classmethod_new")
def py_classmethod_new(func):
    if ptr_is_null(func) != 0:
        return null()
    descriptor = pcc_gc_alloc(PYCLASSMETHODOBJECT_SIZE, PY_TYPE_CLASSMETHOD, 0)
    if ptr_is_null(descriptor) != 0:
        return null()
    store_ptr(descriptor, PYCLASSMETHODOBJECT_FUNC_OFFSET, null())
    pcc_gc_store_ptr(
        descriptor,
        ptr_add(descriptor, PYCLASSMETHODOBJECT_FUNC_OFFSET),
        func,
    )
    py_gc_track(descriptor)
    pcc_gc_publish_initialized(descriptor)
    return descriptor


@c_abi_export("py_staticmethod_new")
def py_staticmethod_new(func):
    if ptr_is_null(func) != 0:
        return null()
    descriptor = pcc_gc_alloc(PYSTATICMETHODOBJECT_SIZE, PY_TYPE_STATICMETHOD, 0)
    if ptr_is_null(descriptor) != 0:
        return null()
    store_ptr(descriptor, PYSTATICMETHODOBJECT_FUNC_OFFSET, null())
    pcc_gc_store_ptr(
        descriptor, ptr_add(descriptor, PYSTATICMETHODOBJECT_FUNC_OFFSET), func
    )
    py_gc_track(descriptor)
    pcc_gc_publish_initialized(descriptor)
    return descriptor


@c_abi_export("py_property_new")
def py_property_new(fget, fset, fdel):
    descriptor = pcc_gc_alloc(PYPROPERTYOBJECT_SIZE, PY_TYPE_PROPERTY, 0)
    if ptr_is_null(descriptor) != 0:
        return null()
    store_ptr(descriptor, PYPROPERTYOBJECT_FGET_OFFSET, null())
    store_ptr(descriptor, PYPROPERTYOBJECT_FSET_OFFSET, null())
    store_ptr(descriptor, PYPROPERTYOBJECT_FDEL_OFFSET, null())
    none_obj = global_load_ptr("py_None")
    if ptr_is_null(fget) == 0 and ptr_eq(fget, none_obj) == 0:
        pcc_gc_store_ptr(
            descriptor, ptr_add(descriptor, PYPROPERTYOBJECT_FGET_OFFSET), fget
        )
    if ptr_is_null(fset) == 0 and ptr_eq(fset, none_obj) == 0:
        pcc_gc_store_ptr(
            descriptor, ptr_add(descriptor, PYPROPERTYOBJECT_FSET_OFFSET), fset
        )
    if ptr_is_null(fdel) == 0 and ptr_eq(fdel, none_obj) == 0:
        pcc_gc_store_ptr(
            descriptor, ptr_add(descriptor, PYPROPERTYOBJECT_FDEL_OFFSET), fdel
        )
    py_gc_track(descriptor)
    pcc_gc_publish_initialized(descriptor)
    return descriptor


# Each signature component keeps its own counted address lease while the
# copied bound signature is allocated. A lease on the method does not pin its
# captures, signature, vectors, or magic string recursively.
_BOUND_SIGNATURE_METHOD = 1
_BOUND_SIGNATURE_CAPTURES = 2
_BOUND_SIGNATURE_FUNCTION_CAPTURES = 3
_BOUND_SIGNATURE_SOURCE = 4
_BOUND_SIGNATURE_VECTOR = 5
_BOUND_SIGNATURE_OUTPUT_VECTOR = 6
_BOUND_SIGNATURE_ITEM = 7
_BOUND_SIGNATURE_OUTPUT = 8
_BOUND_SIGNATURE_RESULT = 12


def _bound_signature_original_captures(slots, tokens) -> int:
    return _special_copy(slots, tokens, _BOUND_SIGNATURE_RESULT,
        ptr_add(slots, _BOUND_SIGNATURE_CAPTURES * C_POINTER_SIZE), 0)


def _bound_signature_wrap_body(slots, tokens) -> int:
    method = load_ptr(slots, _BOUND_SIGNATURE_METHOD * C_POINTER_SIZE)
    if not _ptr_can_have_header(method):
        return _bound_signature_original_captures(slots, tokens)
    if load_i32(method, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_FUNC:
        return _bound_signature_original_captures(slots, tokens)
    if _special_copy(slots, tokens, _BOUND_SIGNATURE_FUNCTION_CAPTURES,
            ptr_add(method, 64), 0) != 0:
        return -1
    captures = load_ptr(slots, _BOUND_SIGNATURE_FUNCTION_CAPTURES * C_POINTER_SIZE)
    if not _ptr_can_have_header(captures):
        return _bound_signature_original_captures(slots, tokens)
    if load_i32(captures, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_TUPLE or py_tuple_len(captures) != 2:
        return _bound_signature_original_captures(slots, tokens)
    if _special_tuple_item(slots, tokens, _BOUND_SIGNATURE_SOURCE,
            _BOUND_SIGNATURE_FUNCTION_CAPTURES, 1) != 0:
        return -1
    signature = load_ptr(slots, _BOUND_SIGNATURE_SOURCE * C_POINTER_SIZE)
    if not _ptr_can_have_header(signature):
        return _bound_signature_original_captures(slots, tokens)
    if load_i32(signature, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_TUPLE or py_tuple_len(signature) < 5:
        return _bound_signature_original_captures(slots, tokens)
    if _special_tuple_item(slots, tokens, _BOUND_SIGNATURE_ITEM,
            _BOUND_SIGNATURE_SOURCE, 0) != 0:
        return -1
    valid: int = _special_name_equal(
        load_ptr(slots, _BOUND_SIGNATURE_ITEM * C_POINTER_SIZE),
        cstr("__pcc_func_signature_v1__"), 25)
    _special_drop(slots, tokens, _BOUND_SIGNATURE_ITEM)
    if valid == 0:
        return _bound_signature_original_captures(slots, tokens)

    # Validate all four vectors before copying them. Signatureless builtin
    # adapters retain their original two-capture convention; allocation or
    # ownership errors must never silently discard a compiled signature.
    count: int = -1
    part: int = 1
    while part <= 4:
        if _special_tuple_item(slots, tokens, _BOUND_SIGNATURE_VECTOR,
                _BOUND_SIGNATURE_SOURCE, part) != 0:
            return -1
        vector = load_ptr(slots, _BOUND_SIGNATURE_VECTOR * C_POINTER_SIZE)
        if not _ptr_can_have_header(vector):
            return _bound_signature_original_captures(slots, tokens)
        if load_i32(vector, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_TUPLE:
            return _bound_signature_original_captures(slots, tokens)
        length: int = py_tuple_len(vector)
        if part == 1:
            count = length
        if length != count or count <= 0:
            return _bound_signature_original_captures(slots, tokens)
        _special_drop(slots, tokens, _BOUND_SIGNATURE_VECTOR)
        part = part + 1

    if _special_tuple_new(slots, tokens, _BOUND_SIGNATURE_OUTPUT, 5) != 0:
        return -1
    if _special_tuple_item(slots, tokens, _BOUND_SIGNATURE_ITEM,
            _BOUND_SIGNATURE_SOURCE, 0) != 0:
        return -1
    py_tuple_set_item(load_ptr(slots, _BOUND_SIGNATURE_OUTPUT * C_POINTER_SIZE),
        0, load_ptr(slots, _BOUND_SIGNATURE_ITEM * C_POINTER_SIZE))
    _special_drop(slots, tokens, _BOUND_SIGNATURE_ITEM)
    part = 1
    while part <= 4:
        if _special_tuple_item(slots, tokens, _BOUND_SIGNATURE_VECTOR,
                _BOUND_SIGNATURE_SOURCE, part) != 0:
            return -1
        if _special_tuple_new(slots, tokens, _BOUND_SIGNATURE_OUTPUT_VECTOR, count - 1) != 0:
            return -1
        index: int = 1
        while index < count:
            if _special_tuple_item(slots, tokens, _BOUND_SIGNATURE_ITEM,
                    _BOUND_SIGNATURE_VECTOR, index) != 0:
                return -1
            py_tuple_set_item(load_ptr(slots, _BOUND_SIGNATURE_OUTPUT_VECTOR * C_POINTER_SIZE),
                index - 1, load_ptr(slots, _BOUND_SIGNATURE_ITEM * C_POINTER_SIZE))
            _special_drop(slots, tokens, _BOUND_SIGNATURE_ITEM)
            if py_err_occurred() != 0:
                return -1
            index = index + 1
        py_tuple_set_item(load_ptr(slots, _BOUND_SIGNATURE_OUTPUT * C_POINTER_SIZE),
            part, load_ptr(slots, _BOUND_SIGNATURE_OUTPUT_VECTOR * C_POINTER_SIZE))
        _special_drop(slots, tokens, _BOUND_SIGNATURE_OUTPUT_VECTOR)
        _special_drop(slots, tokens, _BOUND_SIGNATURE_VECTOR)
        if py_err_occurred() != 0:
            return -1
        part = part + 1
    if _special_tuple_new(slots, tokens, _BOUND_SIGNATURE_RESULT, 2) != 0:
        return -1
    py_tuple_set_item(load_ptr(slots, _BOUND_SIGNATURE_RESULT * C_POINTER_SIZE),
        0, load_ptr(slots, _BOUND_SIGNATURE_CAPTURES * C_POINTER_SIZE))
    py_tuple_set_item(load_ptr(slots, _BOUND_SIGNATURE_RESULT * C_POINTER_SIZE),
        1, load_ptr(slots, _BOUND_SIGNATURE_OUTPUT * C_POINTER_SIZE))
    return 0


def _wrap_bound_captures(method_slot, captures_slot, result_slot) -> int:
    slots = stack_alloc(14 * C_POINTER_SIZE)
    tokens = stack_alloc(14 * C_POINTER_SIZE)
    handles = stack_alloc(14 * C_POINTER_SIZE)
    count: int = _special_open(slots, tokens, handles)
    status: int = -1
    suspended: int = 0
    if count == 14:
        py_tls_exc_swap_slot(slots)
        suspended = 1
        status = _special_copy(slots, tokens, _BOUND_SIGNATURE_METHOD, method_slot, 0)
        if status == 0:
            status = _special_copy(slots, tokens, _BOUND_SIGNATURE_CAPTURES, captures_slot, 0)
        if status == 0:
            status = _bound_signature_wrap_body(slots, tokens)
        if status == 0:
            status = _special_publish(slots, tokens, result_slot)
    if status != 0:
        _special_error(cstr("bound signature copy failed without an exception"))
    _special_close(slots, tokens, handles, count, suspended)
    return status


def _call_pyfunc_bound_args(func, bound_args):
    # The outer bound PyFunc already normalized varargs/keyword-only/defaults.
    # Forward its context while consuming those slots without rebinding them.
    return py_func_call_bound_forward(func, bound_args)


def _bound_clear_root(slots, pins, offset: int) -> None:
    value = load_ptr(slots, offset)
    if ptr_is_null(value) == 0 and is_tagged_int(value) == 0:
        # Only the function slot can contain an unmanaged native entry. Its
        # ownership remains in the tuple; it carries no pin lease (pin=-1).
        prior: int = load_i64(pins, offset)
        if prior >= 0:
            pcc_gc_unpin(value)
            if prior != 0:
                atomic_rmw_i32("or", value, 12, 64, "relaxed")
    pcc_gc_store_root(ptr_add(slots, offset), null())


def _bound_entry_build(slots, pins) -> None:
    # output0, captures8, args16, function24, self32, full48,
    # current arg56, arg1 64, arg2 72, saved error80.
    func = py_tuple_get(load_ptr(slots, 8), 0)
    store_ptr(slots, 24, func)
    store_i64(pins, 24, -1)
    if ptr_is_null(func):
        return
    # The slot is already registered. Query can park, so reload after it.
    managed: int = _ptr_can_have_header(func)
    func = load_ptr(slots, 24)
    if managed and is_tagged_int(func) == 0:
        store_i64(pins, 24, load_i32(func, 12) & 64)
        pcc_gc_pin(func)
        pcc_gc_note_write_barrier(null(), func)
    self_obj = py_tuple_get(load_ptr(slots, 8), 1)
    store_ptr(slots, 32, self_obj)
    if ptr_is_null(self_obj) == 0 and is_tagged_int(self_obj) == 0:
        store_i64(pins, 32, load_i32(self_obj, 12) & 64)
        pcc_gc_pin(self_obj)
        pcc_gc_note_write_barrier(null(), self_obj)
    if ptr_is_null(self_obj):
        return
    n_args: int = py_tuple_len(load_ptr(slots, 16))
    func = load_ptr(slots, 24)
    if managed and load_i32(func, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_FUNC:
        full = py_tuple_new(n_args + 1)
        store_ptr(slots, 48, full)
        if ptr_is_null(full) == 0 and is_tagged_int(full) == 0:
            store_i64(pins, 48, load_i32(full, 12) & 64)
            pcc_gc_pin(full)
            pcc_gc_note_write_barrier(null(), full)
        if ptr_is_null(full):
            return
        py_tuple_set_item(load_ptr(slots, 48), 0, load_ptr(slots, 32))
        i: int = 0
        while i < n_args:
            arg = py_tuple_get(load_ptr(slots, 16), i)
            store_ptr(slots, 56, arg)
            if ptr_is_null(arg) == 0 and is_tagged_int(arg) == 0:
                store_i64(pins, 56, load_i32(arg, 12) & 64)
                pcc_gc_pin(arg)
                pcc_gc_note_write_barrier(null(), arg)
            if ptr_is_null(arg):
                return
            py_tuple_set_item(load_ptr(slots, 48), i + 1, load_ptr(slots, 56))
            _bound_clear_root(slots, pins, 56)
            i = i + 1
        result = _call_pyfunc_bound_args(load_ptr(slots, 24), load_ptr(slots, 48))
        store_ptr(slots, 0, result)
        if ptr_is_null(result) == 0 and is_tagged_int(result) == 0:
            store_i64(pins, 0, load_i32(result, 12) & 64)
            pcc_gc_pin(result)
            pcc_gc_note_write_barrier(null(), result)
    elif n_args >= 0 and n_args <= 3:
        i: int = 0
        while i < n_args:
            arg = py_tuple_get(load_ptr(slots, 16), i)
            store_ptr(slots, 56 + i * 8, arg)
            if ptr_is_null(arg) == 0 and is_tagged_int(arg) == 0:
                store_i64(pins, 56 + i * 8, load_i32(arg, 12) & 64)
                pcc_gc_pin(arg)
                pcc_gc_note_write_barrier(null(), arg)
            if ptr_is_null(arg):
                return
            i = i + 1
        if n_args == 0:
            result = call_ptr1(load_ptr(slots, 24), load_ptr(slots, 32))
        elif n_args == 1:
            result = call_ptr2(load_ptr(slots, 24), load_ptr(slots, 32), load_ptr(slots, 56))
        elif n_args == 2:
            result = call_ptr3(load_ptr(slots, 24), load_ptr(slots, 32), load_ptr(slots, 56), load_ptr(slots, 64))
        else:
            result = call_ptr4(load_ptr(slots, 24), load_ptr(slots, 32), load_ptr(slots, 56), load_ptr(slots, 64), load_ptr(slots, 72))
        store_ptr(slots, 0, result)
        if ptr_is_null(result) == 0 and is_tagged_int(result) == 0:
            store_i64(pins, 0, load_i32(result, 12) & 64)
            pcc_gc_pin(result)
            pcc_gc_note_write_barrier(null(), result)


@c_abi_export("pcc_instance_bound_method_entry")
def _instance_bound_method_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    slots = stack_alloc(88)
    pins = stack_alloc(88)
    memset(slots, 0, 88)
    memset(pins, 0, 88)
    pcc_gc_frame_enter(global_addr("pcc_bound_callback_frame_map"), slots)
    if ptr_is_null(captures) == 0 and is_tagged_int(captures) == 0:
        store_i64(pins, 8, load_i32(captures, 12) & 64)
        pcc_gc_pin(captures)
    pcc_gc_store_root(ptr_add(slots, 8), captures)
    if ptr_is_null(args) == 0 and is_tagged_int(args) == 0:
        store_i64(pins, 16, load_i32(args, 12) & 64)
        pcc_gc_pin(args)
    pcc_gc_store_root(ptr_add(slots, 16), args)
    _bound_entry_build(slots, pins)
    if ptr_is_null(load_ptr(slots, 0)):
        _class_require_result(null(), cstr("class callback"), cstr("class callback returned NULL without setting an exception"))
        error = py_current_exception()
        if ptr_is_null(error) == 0:
            store_i64(pins, 80, load_i32(error, 12) & 64)
            pcc_gc_pin(error)
            py_incref(error)
            store_ptr(slots, 80, error)
            pcc_gc_note_write_barrier(null(), error)
    prior_result_pin: int = load_i64(pins, 0)
    index: int = 9
    while index > 0:
        if ptr_eq(load_ptr(slots, 0), load_ptr(slots, index * 8)):
            prior_result_pin = load_i64(pins, index * 8)
        index = index - 1
    index = 9
    while index > 0:
        _bound_clear_root(slots, pins, index * 8)
        index = index - 1
    error = load_ptr(slots, 80)
    if ptr_is_null(error) == 0:
        py_raise(error)
        _bound_clear_root(slots, pins, 80)
    result = load_ptr(slots, 0)
    if ptr_is_null(result) == 0 and is_tagged_int(result) == 0:
        atomic_rmw_i32("or", result, 12, 64, "relaxed")
    pcc_gc_frame_leave(slots)
    return pcc_gc_take_pinned_slot(slots, prior_result_pin)


_BOUND_BIND_METHOD = 1
_BOUND_BIND_RECEIVER = 2
_BOUND_BIND_CAPTURES = 3
_BOUND_BIND_WRAPPED = 4
_BOUND_BIND_RESULT = 12

define_global_i32("pcc_bound_bind_borrowed_map", -2)
define_global_i32("pcc_bound_bind_result_map", 1)


def _instance_bind_method_body(slots, tokens, name) -> int:
    if _special_tuple_new(slots, tokens, _BOUND_BIND_CAPTURES, 2) != 0:
        return -1
    py_tuple_set_item(load_ptr(slots, _BOUND_BIND_CAPTURES * C_POINTER_SIZE), 0,
        load_ptr(slots, _BOUND_BIND_METHOD * C_POINTER_SIZE))
    py_tuple_set_item(load_ptr(slots, _BOUND_BIND_CAPTURES * C_POINTER_SIZE), 1,
        load_ptr(slots, _BOUND_BIND_RECEIVER * C_POINTER_SIZE))
    method = load_ptr(slots, _BOUND_BIND_METHOD * C_POINTER_SIZE)
    bound_name = name
    if _ptr_can_have_header(method) and load_i32(method, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_FUNC:
        method_name = load_ptr(method, 72)
        if ptr_is_null(method_name) == 0:
            bound_name = method_name
    if _wrap_bound_captures(
            ptr_add(slots, _BOUND_BIND_METHOD * C_POINTER_SIZE),
            ptr_add(slots, _BOUND_BIND_CAPTURES * C_POINTER_SIZE),
            ptr_add(slots, _BOUND_BIND_WRAPPED * C_POINTER_SIZE)) != 0:
        return -1
    if _special_adopt(slots, tokens, _BOUND_BIND_WRAPPED) != 0:
        return -1
    if py_err_occurred() != 0:
        return -1
    store_ptr(slots, _BOUND_BIND_RESULT * C_POINTER_SIZE,
        py_func_new_bound(function_addr("pcc_instance_bound_method_entry"),
            load_ptr(slots, _BOUND_BIND_WRAPPED * C_POINTER_SIZE), bound_name,
            load_ptr(slots, _BOUND_BIND_RECEIVER * C_POINTER_SIZE)))
    if _special_adopt(slots, tokens, _BOUND_BIND_RESULT) != 0:
        return -1
    bound = load_ptr(slots, _BOUND_BIND_RESULT * C_POINTER_SIZE)
    if ptr_is_null(bound) != 0:
        return _special_error(cstr("bound method allocation failed"))
    atomic_rmw_i32("or", bound, PYOBJECTHEADER_FLAGS_OFFSET, PY_FLAG_FUNC_TRANSPARENT_CALL, "relaxed")
    return 0


@c_abi_export("py_instance_bind_method")
def py_instance_bind_method(method, self_obj, name):
    if ptr_is_null(method) != 0 or ptr_is_null(self_obj) != 0:
        return null()
    borrowed = stack_alloc(2 * C_POINTER_SIZE)
    store_ptr(borrowed, 0, method)
    store_ptr(borrowed, C_POINTER_SIZE, self_obj)
    pcc_gc_frame_enter(global_addr("pcc_bound_bind_borrowed_map"), borrowed)
    output = stack_alloc(C_POINTER_SIZE)
    store_ptr(output, 0, null())
    pcc_gc_frame_enter(global_addr("pcc_bound_bind_result_map"), output)
    slots = stack_alloc(14 * C_POINTER_SIZE)
    tokens = stack_alloc(14 * C_POINTER_SIZE)
    handles = stack_alloc(14 * C_POINTER_SIZE)
    count: int = _special_open(slots, tokens, handles)
    status: int = -1
    suspended: int = 0
    if count == 14:
        py_tls_exc_swap_slot(slots)
        suspended = 1
        status = _special_copy(slots, tokens, _BOUND_BIND_METHOD, borrowed, 1)
        if status == 0:
            status = _special_copy(slots, tokens, _BOUND_BIND_RECEIVER, ptr_add(borrowed, C_POINTER_SIZE), 1)
        if status == 0:
            status = _instance_bind_method_body(slots, tokens, name)
        if status == 0:
            status = _special_publish(slots, tokens, output)
    if status != 0:
        _special_error(cstr("method binding failed without an exception"))
    _special_close(slots, tokens, handles, count, suspended)
    value = load_ptr(output, 0)
    prior: int = 0
    if ptr_is_null(value) == 0 and is_tagged_int(value) == 0:
        prior = load_i32(value, PYOBJECTHEADER_FLAGS_OFFSET) & 64
        pcc_gc_pin(value)
    pcc_gc_frame_leave(output)
    pcc_gc_frame_leave(borrowed)
    return pcc_gc_take_pinned_slot(output, prior)


def _bound_method_function_slot_locked(bound):
    # The caller holds the graph transaction and an address lease on bound.
    # Only this exact wrapper owns the (method, self) capture convention.
    if not _ptr_can_have_header(bound):
        return null()
    if load_i32(bound, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_FUNC:
        return null()
    if ptr_eq(load_ptr(bound, 56), function_addr("pcc_instance_bound_method_entry")) == 0:
        return null()
    captures = pcc_gc_resolve_root_slot_unlocked(ptr_add(bound, 64), 0)
    if not _ptr_can_have_header(captures):
        return null()
    if load_i32(captures, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_TUPLE:
        return null()
    if py_tuple_len(captures) != 2:
        return null()
    source = ptr_add(captures, PYTUPLEOBJECT_ITEMS_OFFSET)
    first = pcc_gc_resolve_root_slot_unlocked(source, 0)
    if not _ptr_can_have_header(first):
        # A native method's first capture may be an untraced code address.
        return null()
    if load_i32(first, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_TUPLE:
        # _wrap_bound_captures stores (original captures, bound signature).
        if py_tuple_len(first) != 2:
            return null()
        source = ptr_add(first, PYTUPLEOBJECT_ITEMS_OFFSET)
        first = pcc_gc_resolve_root_slot_unlocked(source, 0)
        if not _ptr_can_have_header(first):
            return null()
    if load_i32(first, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_FUNC:
        return null()
    return source


@c_abi_export("py_func_kind")
def py_func_kind(fn) -> int:
    """type(fn) for a function object: 0 function, 1 builtin function or
    method, 2 bound method; -1 for anything else.

    A user function bound to an instance runs through the instance-method
    entry. A builtin carries PY_FLAG_FUNC_BUILTIN; a runtime method bound to
    its receiver, and a C-extension function, are builtin methods too.
    """
    if ptr_is_null(fn) != 0 or is_tagged_int(fn) != 0:
        return -1
    fn = pcc_gc_note_relocation_read(fn)
    if load_i32(fn, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_FUNC:
        return -1
    if ptr_eq(load_ptr(fn, 56), function_addr("pcc_instance_bound_method_entry")) != 0:
        return 2
    if (load_i32(fn, PYOBJECTHEADER_FLAGS_OFFSET) & PY_FLAG_FUNC_BUILTIN) != 0:
        return 1
    if ptr_is_null(load_ptr(fn, 16)) == 0 or ptr_is_null(load_ptr(fn, 80)) == 0:
        return 1
    return 0


@c_abi_export("py_bound_method_function")
def py_bound_method_function(bound):
    """NEW live original function, or NULL for non-wrapper/native entries.

    Diagnostic callers already hold the bound object stable. Inspect existing
    owning capture slots under the graph lock; only after managed FUNC proof
    acquire the result reference and pin. No raw code pointer becomes a root.
    """
    result_slot = stack_alloc(C_POINTER_SIZE)
    store_ptr(result_slot, 0, null())
    prior_pin: int = 0
    pcc_py_gc_minor_graph_lock()
    source = _bound_method_function_slot_locked(bound)
    if ptr_is_null(source) == 0:
        original = load_ptr(source, 0)
        prior_pin = load_i32(original, PYOBJECTHEADER_FLAGS_OFFSET) & 64
        py_incref(original)
        pcc_gc_pin(original)
        store_ptr(result_slot, 0, original)
    pcc_py_gc_minor_graph_unlock()
    return pcc_gc_take_pinned_slot(result_slot, prior_pin)


# Instance attribute resolution cache, one table per thread.  Resolving
# `inst.name` walked the MRO for every access: every method table for
# `__getattribute__`, then every class-attribute dict with a freshly allocated
# str key, before a field or method was found -- 56% of a native Stage2
# frontend worker.  An entry records one proven outcome for (class, name).
# The shared class epoch retires every entry on class mutation, relocation or
# deallocation, and the name is copied into the entry so a hit never reads a
# caller buffer or a freed class-owned spelling.
#   +0 class  +8 epoch  +12 kind  +16 payload  +24 name bytes (NUL-terminated)
define_thread_local_ptr_null("py_attr_resolution_table")
_ATTR_RES_ENTRIES = 16384
_ATTR_RES_ENTRY_SIZE = 64
_ATTR_RES_NAME_MAX = 39
_ATTR_RES_FIELD = 1
_ATTR_RES_METHOD = 2
# The class resolves `name` to nothing: no class attribute, field or method in
# the MRO, no `__getattr__`, and not a dict subclass.  Only the instance's own
# dict can still answer.  Generic AST walks probe every node for dozens of
# field names it lacks -- 28% of the lookups in a native Stage2 frontend
# worker -- and each miss walked the MRO twice with a fresh str key.
_ATTR_RES_ABSENT = 4
# A data descriptor (a property, or an object with `__set__`/`__delete__`)
# answers for (class, name); payload is the borrowed descriptor, held by the
# class dict, so it is cached only where methods are (non-moving collectors).
_ATTR_RES_DATA_DESCRIPTOR = 5


def _attr_res_slot(table, cls, name):
    slot: int = (
        (ptr_to_int(cls) >> 4) ^ (ptr_to_int(name) >> 3)
    ) & (_ATTR_RES_ENTRIES - 1)
    return ptr_add(table, slot * _ATTR_RES_ENTRY_SIZE)


def _attr_res_find(cls, name, kind: int):
    entry = _attr_res_find_any(cls, name)
    if ptr_is_null(entry) != 0:
        return null()
    if load_i32(entry, 12) != kind:
        return null()
    return entry


def _attr_res_find_any(cls, name):
    """The live entry for (cls, name) whatever its kind: every kind of one
    (class, name) lands in the same slot, so a caller that accepts several
    kinds compares the name once."""
    table = global_load_ptr("py_attr_resolution_table")
    if ptr_is_null(table) != 0:
        return null()
    entry = _attr_res_slot(table, cls, name)
    if ptr_eq(load_ptr(entry, 0), cls) == 0:
        return null()
    if load_i32(entry, 12) == 0:
        return null()
    if load_i32(entry, 8) != _class_attr_cache_epoch():
        return null()
    i: int = 0
    while i <= _ATTR_RES_NAME_MAX:
        a: int = load_i8(entry, 24 + i) & 0xFF
        b: int = load_i8(name, i) & 0xFF
        if a != b:
            return null()
        if a == 0:
            return entry
        i = i + 1
    return null()


# `__getattribute__` absence, keyed by the class alone.  Every instance
# attribute read asks this first; answering it from the name-keyed table cost
# a 17-byte name compare per access.  Entry: +0 class  +8 epoch.
define_thread_local_ptr_null("py_attr_no_getattribute_table")
_NO_GETATTRIBUTE_ENTRIES = 1024


def _no_getattribute_known(cls) -> int:
    table = global_load_ptr("py_attr_no_getattribute_table")
    if ptr_is_null(table) != 0:
        return 0
    entry = ptr_add(
        table, ((ptr_to_int(cls) >> 4) & (_NO_GETATTRIBUTE_ENTRIES - 1)) * 16
    )
    if ptr_eq(load_ptr(entry, 0), cls) == 0:
        return 0
    if load_i32(entry, 8) != _class_attr_cache_epoch():
        return 0
    return 1


def _no_getattribute_store(cls, epoch: int) -> None:
    table = global_load_ptr("py_attr_no_getattribute_table")
    if ptr_is_null(table) != 0:
        table = malloc(_NO_GETATTRIBUTE_ENTRIES * 16)
        if ptr_is_null(table) != 0:
            return
        memset(table, 0, _NO_GETATTRIBUTE_ENTRIES * 16)
        global_store_ptr("py_attr_no_getattribute_table", table)
    entry = ptr_add(
        table, ((ptr_to_int(cls) >> 4) & (_NO_GETATTRIBUTE_ENTRIES - 1)) * 16
    )
    store_ptr(entry, 0, null())
    store_i32(entry, 8, epoch)
    store_ptr(entry, 0, cls)


def _attr_res_store(cls, name, kind: int, payload: int, epoch: int) -> None:
    # `epoch` is sampled before the lookup that proved the outcome, so a
    # concurrent class mutation during that lookup leaves the entry stale.
    length: int = strlen(name)
    if length > _ATTR_RES_NAME_MAX:
        return
    table = global_load_ptr("py_attr_resolution_table")
    if ptr_is_null(table) != 0:
        table = malloc(_ATTR_RES_ENTRIES * _ATTR_RES_ENTRY_SIZE)
        if ptr_is_null(table) != 0:
            return
        memset(table, 0, _ATTR_RES_ENTRIES * _ATTR_RES_ENTRY_SIZE)
        global_store_ptr("py_attr_resolution_table", table)
    entry = _attr_res_slot(table, cls, name)
    store_i32(entry, 12, 0)
    store_ptr(entry, 0, cls)
    store_i32(entry, 8, epoch)
    store_i64(entry, 16, payload)
    i: int = 0
    while i <= length:
        store_i8(entry, 24 + i, load_i8(name, i))
        i = i + 1
    store_i32(entry, 12, kind)


def _attr_res_store_dict_only(cls, name, epoch: int) -> None:
    """Record ABSENT for a name that only an instance dict answered.

    Module objects and other dict-backed instances otherwise repeated the
    whole class lookup on every read of such a name. The outcome must hold
    for any instance of ``cls``: an instance without the key must still get
    nothing from the class, so no method, ``__getattr__`` or dict-subclass
    fallback may exist. The class epoch retires the entry when that changes.
    """
    if (load_i32(cls, PYOBJECTHEADER_FLAGS_OFFSET) & 4) != 0:
        return
    if ptr_is_null(_class_lookup_in_mro(cls, name)) == 0:
        return
    if ptr_is_null(_class_lookup_in_mro(cls, cstr("__getattr__"))) == 0:
        return
    if py_err_occurred() != 0:
        return
    # object's namespace answers an instance whose dict lacks the name.
    if _object_member_name(name) != 0:
        return
    _attr_res_store(cls, name, _ATTR_RES_ABSENT, 0, epoch)


def _attr_res_method_cacheable() -> int:
    # Methods are cached by address; the forwarding collectors can move them.
    backend: int = pcc_gc_backend()
    if backend == 3 or backend == 4:
        return 0
    return 1


# Class-only semantic flag. This bit has a distinct meaning on function
# objects; it is outside all collector bits and is read only after a class check.
_STRUCTSEQ_CLASS_FLAG = 8388608


def _class_is_structseq(cls) -> bool:
    return _ptr_is_class(cls) and (load_i32(cls, PYOBJECTHEADER_FLAGS_OFFSET) & _STRUCTSEQ_CLASS_FLAG) != 0


def _instance_reserved_owner_slot(inst, cls):
    # Every native instance physically reserves this owning slot, including
    # slots-only dict/exception subclasses. Public __dict__ visibility is a
    # separate policy; GC, teardown and physical copies must not use it.
    n_fields: int = load_i32(cls, PYCLASSOBJECT_N_FIELDS_OFFSET)
    if n_fields < 0:
        n_fields = 0
    return ptr_add(
        inst, PYINSTANCEOBJECT_FIELDS_OFFSET + n_fields * C_POINTER_SIZE
    )


def _instance_storage_slot_count(cls) -> int:
    # Declared fields, the established dynamic/builtin backing owner, then
    # any appended builtin payload owners. Existing field offsets never move.
    minimum: int = load_i32(cls, PYCLASSOBJECT_N_FIELDS_OFFSET) + 1
    if minimum < 1:
        minimum = 1
    count: int = (load_i32(cls, PYCLASSOBJECT_INSTANCE_SIZE_OFFSET) - PYINSTANCEOBJECT_FIELDS_OFFSET) // C_POINTER_SIZE
    if count < minimum:
        count = minimum
    return count


@c_abi_export("py_class_is_str_subclass")
def py_class_is_str_subclass(cls) -> int:
    if not _ptr_is_class(cls):
        return 0
    n_mro: int = load_i32(cls, PYCLASSOBJECT_N_MRO_OFFSET)
    mro = load_ptr(cls, PYCLASSOBJECT_MRO_OFFSET)
    index: int = 0
    while index < n_mro:
        owner = pcc_gc_load_ptr(cls, ptr_add(mro, index * C_POINTER_SIZE))
        if py_builtin_type_class_tag(owner) == PY_TYPE_STR:
            return 1
        index = index + 1
    return 0


@c_abi_export("py_class_is_tuple_subclass")
def py_class_is_tuple_subclass(cls) -> int:
    if not _ptr_is_class(cls):
        return 0
    n_mro: int = load_i32(cls, PYCLASSOBJECT_N_MRO_OFFSET)
    mro = load_ptr(cls, PYCLASSOBJECT_MRO_OFFSET)
    index: int = 0
    while index < n_mro:
        owner = pcc_gc_load_ptr(cls, ptr_add(mro, index * C_POINTER_SIZE))
        if py_builtin_type_class_tag(owner) == PY_TYPE_TUPLE:
            return 1
        index = index + 1
    return 0


def _instance_builtin_payload_slot(inst, cls):
    # The immutable builtin payload follows the pre-existing reserved owner.
    count: int = load_i32(cls, PYCLASSOBJECT_N_FIELDS_OFFSET) + 1
    if count < 1:
        count = 1
    if _instance_storage_slot_count(cls) <= count:
        return null()
    return ptr_add(inst, PYINSTANCEOBJECT_FIELDS_OFFSET + count * C_POINTER_SIZE)


@c_abi_export("py_str_payload")
def py_str_payload(value):
    # Borrowed native string storage. The receiver keeps its own class,
    # identity, fields, dictionary and finalizer throughout this view.
    if ptr_is_null(value) != 0 or is_tagged_int(value) != 0:
        return null()
    value = pcc_gc_note_relocation_read(value)
    if load_i32(value, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_STR:
        return value
    if not _ptr_is_instance(value):
        return null()
    cls = pcc_gc_load_ptr(value, ptr_add(value, PYINSTANCEOBJECT_CLS_OFFSET))
    # Immutable builtin payloads share an appended owner slot. Read its
    # scalar layout immediately; do not hold a borrowed class pointer over
    # another allocating lookup. Return is an immediate borrowed check only.
    count: int = load_i32(cls, PYCLASSOBJECT_N_FIELDS_OFFSET) + 1
    if count < 1:
        count = 1
    offset: int = PYINSTANCEOBJECT_FIELDS_OFFSET + count * C_POINTER_SIZE
    if load_i32(cls, PYCLASSOBJECT_INSTANCE_SIZE_OFFSET) <= offset:
        return null()
    payload = pcc_gc_load_ptr(value, ptr_add(value, offset))
    if ptr_is_null(payload) != 0 or is_tagged_int(payload) != 0:
        return null()
    if load_i32(payload, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_STR:
        return null()
    return payload


@c_abi_export("py_tuple_payload")
def py_tuple_payload(value):
    # Immediate borrowed view only. A consumer that can park must acquire
    # its own payload-slot owner/lease rather than retaining this raw view.
    if ptr_is_null(value) != 0 or is_tagged_int(value) != 0:
        return null()
    value = pcc_gc_note_relocation_read(value)
    if load_i32(value, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_TUPLE:
        return value
    if not _ptr_is_instance(value):
        return null()
    cls = pcc_gc_load_ptr(value, ptr_add(value, PYINSTANCEOBJECT_CLS_OFFSET))
    slot = _instance_builtin_payload_slot(value, cls)
    if ptr_is_null(slot) != 0:
        return null()
    payload = pcc_gc_load_ptr(value, slot)
    if ptr_is_null(payload) != 0 or is_tagged_int(payload) != 0:
        return null()
    if load_i32(payload, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_TUPLE:
        return null()
    return payload


@c_abi_export("py_tuple_check")
def py_tuple_check(value) -> int:
    if ptr_is_null(value) != 0 or is_tagged_int(value) != 0:
        return 0
    value = pcc_gc_note_relocation_read(value)
    if load_i32(value, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_TUPLE:
        return 1
    if not _ptr_is_instance(value):
        return 0
    return py_class_is_tuple_subclass(
        pcc_gc_load_ptr(value, ptr_add(value, PYINSTANCEOBJECT_CLS_OFFSET))
    )


@c_abi_export("py_instance_copy_builtin_payload")
def py_instance_copy_builtin_payload(source, destination) -> None:
    if not _ptr_is_instance(source) or not _ptr_is_instance(destination):
        return
    pcc_py_gc_minor_graph_lock()
    source = pcc_gc_note_relocation_read(source)
    destination = pcc_gc_note_relocation_read(destination)
    cls = pcc_gc_load_ptr(source, ptr_add(source, PYINSTANCEOBJECT_CLS_OFFSET))
    source_slot = _instance_builtin_payload_slot(source, cls)
    if ptr_is_null(source_slot) == 0:
        value = pcc_gc_load_ptr(source, source_slot)
        pcc_gc_store_ptr(destination, _instance_builtin_payload_slot(destination, cls), value)
    pcc_py_gc_minor_graph_unlock()


def _dynamic_attr_slot(inst):
    if not _ptr_is_instance(inst):
        return null()
    cls = pcc_gc_load_ptr(inst, ptr_add(inst, PYINSTANCEOBJECT_CLS_OFFSET))
    flags: int = load_i32(cls, PYOBJECTHEADER_FLAGS_OFFSET)
    if (flags & 2) != 0:
        return null()
    return _instance_reserved_owner_slot(inst, cls)


def _copy_instance_reserved_owner(obj, dst, cls) -> None:
    # The surrounding legacy replace entry still owns its raw object-entry
    # contract. Within this physical copy, heal the owning source and publish
    # the additional destination owner in one graph transaction.
    pcc_py_gc_minor_graph_lock()
    source = _instance_reserved_owner_slot(obj, cls)
    destination = _instance_reserved_owner_slot(dst, cls)
    value = pcc_gc_resolve_root_slot_unlocked(source, 0)
    if ptr_is_null(value) == 0:
        py_incref(value)
        store_ptr(destination, 0, value)
        pcc_gc_note_slot_write_barrier(dst, destination, value)
    pcc_py_gc_minor_graph_unlock()


def _instance_dict_of(inst, cls):
    """The instance dict of a validated instance of `cls` (borrowed), or NULL:
    `_dynamic_attr_slot` without validating `inst` a second time."""
    if (load_i32(cls, PYOBJECTHEADER_FLAGS_OFFSET) & 2) != 0:
        return null()
    n_fields: int = load_i32(cls, PYCLASSOBJECT_N_FIELDS_OFFSET)
    if n_fields < 0:
        n_fields = 0
    return pcc_gc_load_ptr(
        inst,
        ptr_add(inst, PYINSTANCEOBJECT_FIELDS_OFFSET + n_fields * C_POINTER_SIZE),
    )


def _instance_dict_attr(inst, cls, name):
    """`name` from the instance's own dict (a new reference), or NULL, for an
    ABSENT outcome: the class already answered nothing."""
    dyn = _instance_dict_of(inst, cls)
    if ptr_is_null(dyn) != 0:
        return null()
    cached = pcc_cached_name_key(name)
    if ptr_is_null(cached) == 0:
        return py_dict_get(dyn, cached)
    key = py_str_new(name, strlen(name))
    if ptr_is_null(key) != 0:
        return null()
    got = py_dict_get(dyn, key)
    py_decref(key)
    return got


_INSTANCE_LOOKUP_VARS = 3


@c_abi_export("py_instance_vars")
def py_instance_vars(inst):
    if not _ptr_is_instance(inst):
        py_raise_owned(py_exc_new(3, cstr("vars() argument has no __dict__")))
        return null()
    cls = pcc_gc_load_ptr(inst, ptr_add(inst, PYINSTANCEOBJECT_CLS_OFFSET))
    if not _ptr_is_class(cls):
        py_raise_owned(py_exc_new(3, cstr("vars() argument has no __dict__")))
        return null()
    if _class_is_structseq(cls):
        py_raise_owned(py_exc_new(3, cstr("vars() argument has no __dict__")))
        return null()
    out = py_dict_new()
    if ptr_is_null(out) != 0:
        return null()
    n_fields: int = load_i32(cls, PYCLASSOBJECT_N_FIELDS_OFFSET)
    if n_fields < 0:
        n_fields = 0
    field_names = load_ptr(cls, PYCLASSOBJECT_FIELD_NAMES_OFFSET)
    fields = ptr_add(inst, PYINSTANCEOBJECT_FIELDS_OFFSET)
    i: int = 0
    while i < n_fields:
        field_name = null()
        if ptr_is_null(field_names) == 0:
            field_name = load_ptr(field_names, i * C_POINTER_SIZE)
        if ptr_is_null(field_name) == 0:
            value = pcc_gc_load_ptr(
                inst, ptr_add(fields, i * C_POINTER_SIZE)
            )
            if ptr_is_null(value) == 0:
                key = py_str_new(field_name, strlen(field_name))
                if ptr_is_null(key) != 0:
                    py_decref(out)
                    return null()
                py_dict_set(out, key, value)
                py_decref(key)
        i = i + 1
    dyn_slot = _dynamic_attr_slot(inst)
    if ptr_is_null(dyn_slot) == 0:
        dyn = pcc_gc_load_ptr(inst, dyn_slot)
        if ptr_is_null(dyn) == 0:
            py_dict_update(out, dyn)
    return out


@c_abi_export("py_obj_vars")
def py_obj_vars(o):
    if ptr_is_null(o) != 0:
        py_raise_owned(py_exc_new(3, cstr("vars() argument has no __dict__")))
        return null()
    if is_tagged_int(o) != 0:
        py_raise_owned(py_exc_new(3, cstr("vars() argument has no __dict__")))
        return null()
    if _ptr_is_instance(o):
        return _instance_lookup_rooted(
            o, -1, cstr("__dict__"), _INSTANCE_LOOKUP_VARS,
        )
    if _ptr_is_class(o):
        attrs = py_class_attrs_dict(o, 1)
        if ptr_is_null(attrs) == 0:
            py_incref(attrs)
            return attrs
    py_raise_owned(py_exc_new(3, cstr("vars() argument has no __dict__")))
    return null()


pcc_cached_name_key = extern("pcc_cached_name_key", (c_ptr,), c_ptr)
py_obj_type_name = extern("py_obj_type_name", (c_ptr,), c_ptr)
py_obj_default_repr = extern("py_obj_default_repr", (c_ptr,), c_ptr)
py_exc_base_str = extern("py_exc_base_str", (c_ptr, c_int64), c_ptr)
py_exc_base_repr = extern("py_exc_base_repr", (c_ptr,), c_ptr)
py_dict_subclass_storage = extern("py_dict_subclass_storage", (c_ptr, c_int64), c_ptr)
py_dict_subclass_getitem = extern("py_dict_subclass_getitem", (c_ptr, c_ptr), c_ptr)
py_dict_getitem = extern("py_dict_getitem", (c_ptr, c_ptr), c_ptr)
py_dict_get_default = extern("py_dict_get_default", (c_ptr, c_ptr, c_ptr), c_ptr)
py_dict_contains = extern("py_dict_contains", (c_ptr, c_ptr), c_int64)
py_dict_clear = extern("py_dict_clear", (c_ptr,), c_void)
py_dict_popitem = extern("py_dict_popitem", (c_ptr,), c_ptr)
py_dict_values = extern("py_dict_values", (c_ptr,), c_ptr)
py_exc_new_with_value = extern("py_exc_new_with_value", (c_int64, c_ptr), c_ptr)
py_obj_len = extern("py_obj_len", (c_ptr,), c_int64)
py_obj_getitem = extern("py_obj_getitem", (c_ptr, c_ptr), c_ptr)
py_obj_identity_hash = extern("py_obj_identity_hash", (c_ptr,), c_int64)
py_obj_getattr_default = extern("py_obj_getattr_default", (c_ptr, c_ptr), c_ptr)


def _class_attr_lookup_in_mro(cls, name):
    if not _ptr_is_class(cls):
        return null()
    if ptr_is_null(name) != 0:
        return null()
    cls = pcc_gc_note_relocation_read(cls)
    # The cache owns its pinned key; an uncached key is this lookup's own.
    owned: int = 0
    key = pcc_cached_name_key(name)
    if ptr_is_null(key) != 0:
        key = py_str_new(name, strlen(name))
        if ptr_is_null(key) != 0:
            return null()
        owned = 1
    n_mro: int = load_i32(cls, PYCLASSOBJECT_N_MRO_OFFSET)
    mro = load_ptr(cls, PYCLASSOBJECT_MRO_OFFSET)
    found = null()
    i: int = 0
    while ptr_is_null(found) != 0 and i < n_mro:
        m = pcc_gc_load_ptr(cls, ptr_add(mro, i * 8))
        if ptr_is_null(m) == 0:
            attrs = py_class_attrs_dict(m, 0)
            if ptr_is_null(attrs) == 0:
                found = py_dict_get(attrs, key)
        i = i + 1
    if owned != 0:
        py_decref(key)
    return found


def _descriptor_method(descriptor, name):
    if ptr_is_null(descriptor) != 0:
        return null()
    if is_tagged_int(descriptor) != 0:
        return null()
    if not _ptr_is_instance(descriptor):
        return null()
    desc_cls = pcc_gc_load_ptr(
        descriptor,
        ptr_add(descriptor, PYINSTANCEOBJECT_CLS_OFFSET),
    )
    return _class_lookup_in_mro(desc_cls, name)


def _class_attr_is_property(descriptor) -> bool:
    if ptr_is_null(descriptor) == 0:
        if is_tagged_int(descriptor) == 0:
            if load_i32(descriptor, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_PROPERTY:
                return True
    return False


def _descriptor_is_data(descriptor) -> bool:
    if ptr_is_null(descriptor) == 0:
        if is_tagged_int(descriptor) == 0:
            if load_i32(descriptor, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_PROPERTY:
                return True
    if ptr_is_null(_descriptor_method(descriptor, cstr("__set__"))) == 0:
        return True
    if ptr_is_null(_descriptor_method(descriptor, cstr("__delete__"))) == 0:
        return True
    return False


def _descriptor_call_get(descriptor, obj, owner):
    if ptr_is_null(descriptor) == 0:
        if is_tagged_int(descriptor) == 0:
            if load_i32(descriptor, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_CLASSMETHOD:
                return _classmethod_bind(descriptor, owner)
            if load_i32(descriptor, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_STATICMETHOD:
                func = pcc_gc_load_ptr(
                    descriptor, ptr_add(descriptor, PYSTATICMETHODOBJECT_FUNC_OFFSET)
                )
                py_incref(func)
                return func
            if load_i32(descriptor, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_PROPERTY:
                fget = pcc_gc_load_ptr(
                    descriptor,
                    ptr_add(descriptor, PYPROPERTYOBJECT_FGET_OFFSET),
                )
                if ptr_is_null(fget) != 0:
                    py_raise_owned(py_exc_new(6, cstr("unreadable attribute")))
                    return null()
                if ptr_eq(obj, global_load_ptr("py_None")) != 0:
                    py_incref(descriptor)
                    return descriptor
                args = py_tuple_new(1)
                if ptr_is_null(args) != 0:
                    return _class_require_result(
                        null(),
                        cstr("py_tuple_new"),
                        cstr("class callback argument tuple allocation failed"),
                    )
                py_tuple_set_item(args, 0, obj)
                out = py_obj_call(fget, args, global_load_ptr("py_None"))
                _class_require_result(
                    out,
                    cstr("property __get__"),
                    cstr("class callback returned NULL without setting an exception"),
                )
                py_decref(args)
                return out
    method = _descriptor_method(descriptor, cstr("__get__"))
    if ptr_is_null(method) != 0:
        return null()
    args = py_tuple_new(3)
    if ptr_is_null(args) != 0:
        return _class_require_result(
            null(),
            cstr("py_tuple_new"),
            cstr("class callback argument tuple allocation failed"),
        )
    py_tuple_set_item(args, 0, descriptor)
    py_tuple_set_item(args, 1, obj)
    py_tuple_set_item(args, 2, owner)
    out = py_obj_call(method, args, global_load_ptr("py_None"))
    _class_require_result(
        out,
        cstr("descriptor __get__"),
        cstr("class callback returned NULL without setting an exception"),
    )
    py_decref(args)
    return out


def _descriptor_call_set(descriptor, obj, value) -> int:
    if ptr_is_null(descriptor) == 0:
        if is_tagged_int(descriptor) == 0:
            if load_i32(descriptor, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_PROPERTY:
                fset = pcc_gc_load_ptr(
                    descriptor,
                    ptr_add(descriptor, PYPROPERTYOBJECT_FSET_OFFSET),
                )
                if ptr_is_null(fset) != 0:
                    py_raise_owned(py_exc_new(6, cstr("can't set attribute")))
                    return -1
                args = py_tuple_new(2)
                if ptr_is_null(args) != 0:
                    _class_require_result(
                        null(),
                        cstr("py_tuple_new"),
                        cstr("class callback argument tuple allocation failed"),
                    )
                    return -1
                py_tuple_set_item(args, 0, obj)
                py_tuple_set_item(args, 1, value)
                out = py_obj_call(fset, args, global_load_ptr("py_None"))
                _class_require_result(
                    out,
                    cstr("property __set__"),
                    cstr("class callback returned NULL without setting an exception"),
                )
                py_decref(args)
                if ptr_is_null(out) != 0:
                    return -1
                py_decref(out)
                return 0
    method = _descriptor_method(descriptor, cstr("__set__"))
    if ptr_is_null(method) != 0:
        return -1
    args = py_tuple_new(3)
    if ptr_is_null(args) != 0:
        _class_require_result(
            null(),
            cstr("py_tuple_new"),
            cstr("class callback argument tuple allocation failed"),
        )
        return -1
    py_tuple_set_item(args, 0, descriptor)
    py_tuple_set_item(args, 1, obj)
    py_tuple_set_item(args, 2, value)
    out = py_obj_call(method, args, global_load_ptr("py_None"))
    _class_require_result(
        out,
        cstr("descriptor __set__"),
        cstr("class callback returned NULL without setting an exception"),
    )
    py_decref(args)
    if ptr_is_null(out) != 0:
        return -1
    py_decref(out)
    return 0


def _descriptor_call_delete(descriptor, obj) -> int:
    if ptr_is_null(descriptor) == 0:
        if is_tagged_int(descriptor) == 0:
            if load_i32(descriptor, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_PROPERTY:
                fdel = pcc_gc_load_ptr(
                    descriptor,
                    ptr_add(descriptor, PYPROPERTYOBJECT_FDEL_OFFSET),
                )
                if ptr_is_null(fdel) != 0:
                    py_raise_owned(py_exc_new(6, cstr("can't delete attribute")))
                    return -1
                args = py_tuple_new(1)
                if ptr_is_null(args) != 0:
                    _class_require_result(
                        null(),
                        cstr("py_tuple_new"),
                        cstr("class callback argument tuple allocation failed"),
                    )
                    return -1
                py_tuple_set_item(args, 0, obj)
                out = py_obj_call(fdel, args, global_load_ptr("py_None"))
                _class_require_result(
                    out,
                    cstr("property __delete__"),
                    cstr("class callback returned NULL without setting an exception"),
                )
                py_decref(args)
                if ptr_is_null(out) != 0:
                    return -1
                py_decref(out)
                return 0
    method = _descriptor_method(descriptor, cstr("__delete__"))
    if ptr_is_null(method) != 0:
        return -1
    args = py_tuple_new(2)
    if ptr_is_null(args) != 0:
        _class_require_result(
            null(),
            cstr("py_tuple_new"),
            cstr("class callback argument tuple allocation failed"),
        )
        return -1
    py_tuple_set_item(args, 0, descriptor)
    py_tuple_set_item(args, 1, obj)
    out = py_obj_call(method, args, global_load_ptr("py_None"))
    _class_require_result(
        out,
        cstr("descriptor __delete__"),
        cstr("class callback returned NULL without setting an exception"),
    )
    py_decref(args)
    if ptr_is_null(out) != 0:
        return -1
    py_decref(out)
    return 0


def _classmethod_bind(descriptor, cls):
    if ptr_is_null(descriptor) != 0 or ptr_is_null(cls) != 0:
        return null()
    if (
        is_tagged_int(descriptor) != 0
        or load_i32(descriptor, PYOBJECTHEADER_TYPE_TAG_OFFSET)
        != PY_TYPE_CLASSMETHOD
    ):
        return descriptor
    func = pcc_gc_load_ptr(
        descriptor,
        ptr_add(descriptor, PYCLASSMETHODOBJECT_FUNC_OFFSET),
    )
    if ptr_is_null(func) != 0:
        return null()
    name = null()
    if _ptr_can_have_header(func) and load_i32(func, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_FUNC:
        name = load_ptr(func, 72)
    return py_instance_bind_method(func, cls, name)


def _metaclass(cls):
    if not _ptr_is_class(cls):
        return null()
    metaclass = pcc_gc_load_ptr(cls, ptr_add(cls, PYCLASSOBJECT_METACLASS_OFFSET))
    if not _ptr_is_class(metaclass):
        return null()
    return pcc_gc_note_relocation_read(metaclass)


def _object_new_cache_mutex() -> c_ptr:
    slot = global_addr("pcc_object_new_cache_mutex_bits")
    bits: int = atomic_load_i64(slot, 0, "acquire")
    if bits != 0:
        return int_to_ptr(bits)
    candidate = pcc_mutex_new()
    if ptr_is_null(candidate) != 0:
        return null()
    installed: int = atomic_cas_i64(
        slot, 0, 0, ptr_to_int(candidate), "acq_rel", "acquire"
    )
    if installed != 0:
        pcc_mutex_free(candidate)
        return int_to_ptr(installed)
    return candidate


def _object_allocator_supports_class(cls) -> bool:
    """An ordinary class layout, excluding other builtin allocation owners."""
    if not _ptr_is_class(cls):
        return False
    cls = pcc_gc_note_relocation_read(cls)
    n_mro: int = load_i32(cls, PYCLASSOBJECT_N_MRO_OFFSET)
    mro = load_ptr(cls, PYCLASSOBJECT_MRO_OFFSET)
    root = global_load_ptr("py_object_root_cache")
    found_root: int = 0
    index: int = 0
    while index < n_mro:
        owner = pcc_gc_load_ptr(cls, ptr_add(mro, index * C_POINTER_SIZE))
        if ptr_is_null(owner) == 0:
            builtin_tag: int = py_builtin_type_class_tag(owner)
            # -1 is object, -2 is an ordinary user class. Other builtin
            # classes need their allocator; object layout is not a fallback.
            if builtin_tag >= PY_TYPE_NONE or builtin_tag == -3:
                return False
            exc_index: int = 0
            while exc_index < 65:
                exception_class = load_ptr(
                    global_addr("py_exc_classes"), exc_index * C_POINTER_SIZE
                )
                if ptr_is_null(exception_class) == 0:
                    if ptr_eq(owner, exception_class) != 0:
                        return False
                exc_index = exc_index + 1
            if ptr_eq(owner, root) != 0:
                found_root = 1
        index = index + 1
    return found_root != 0


def _object_new_signature_formal(names, kinds, has_defaults, defaults, index: int, name, kind: int) -> bool:
    name_obj = py_str_new(name, strlen(name))
    if ptr_is_null(name_obj) != 0:
        return False
    pcc_gc_pin(name_obj)
    py_tuple_set_item(names, index, name_obj)
    py_tuple_set_item(kinds, index, py_int_from_i64(kind))
    py_tuple_set_item(has_defaults, index, py_bool_from_bit(0))
    py_tuple_set_item(defaults, index, global_load_ptr("py_None"))
    pcc_gc_unpin(name_obj)
    py_decref(name_obj)
    return py_err_occurred() == 0


def _object_new_captures() -> c_ptr:
    return _object_member_captures(cstr("cls"))


def _object_member_captures(first) -> c_ptr:
    # The ordinary native function binder owns positional-only ``first``,
    # *args and **kwargs. Use its existing signature format, without a
    # builtin-only keyword or argument transport.
    names = py_tuple_new(3)
    pcc_gc_pin(names)
    kinds = py_tuple_new(3)
    pcc_gc_pin(kinds)
    has_defaults = py_tuple_new(3)
    pcc_gc_pin(has_defaults)
    defaults = py_tuple_new(3)
    pcc_gc_pin(defaults)
    signature = null()
    wrapper = null()
    valid: int = 0
    if ptr_is_null(names) == 0 and ptr_is_null(kinds) == 0 and ptr_is_null(has_defaults) == 0 and ptr_is_null(defaults) == 0:
        if _object_new_signature_formal(names, kinds, has_defaults, defaults, 0, first, 1):
            if _object_new_signature_formal(names, kinds, has_defaults, defaults, 1, cstr("args"), 3):
                if _object_new_signature_formal(names, kinds, has_defaults, defaults, 2, cstr("kwargs"), 4):
                    valid = 1
    if valid != 0:
        signature = py_tuple_new(5)
        pcc_gc_pin(signature)
        if ptr_is_null(signature) == 0:
            magic = py_str_new(cstr("__pcc_func_signature_v1__"), 25)
            if ptr_is_null(magic) == 0:
                pcc_gc_pin(magic)
                py_tuple_set_item(signature, 0, magic)
                py_tuple_set_item(signature, 1, names)
                py_tuple_set_item(signature, 2, kinds)
                py_tuple_set_item(signature, 3, has_defaults)
                py_tuple_set_item(signature, 4, defaults)
                pcc_gc_unpin(magic)
                py_decref(magic)
                wrapper = py_tuple_new(2)
                pcc_gc_pin(wrapper)
                if ptr_is_null(wrapper) == 0:
                    empty = py_tuple_new(0)
                    if ptr_is_null(empty) == 0:
                        pcc_gc_pin(empty)
                        py_tuple_set_item(wrapper, 0, empty)
                        py_tuple_set_item(wrapper, 1, signature)
                        pcc_gc_unpin(empty)
                        py_decref(empty)
                    else:
                        pcc_gc_unpin(wrapper)
                        py_decref(wrapper)
                        wrapper = null()
    pcc_gc_unpin(signature)
    py_decref(signature)
    pcc_gc_unpin(defaults)
    py_decref(defaults)
    pcc_gc_unpin(has_defaults)
    py_decref(has_defaults)
    pcc_gc_unpin(kinds)
    py_decref(kinds)
    pcc_gc_unpin(names)
    py_decref(names)
    pcc_gc_unpin(wrapper)
    return wrapper


def _object_new_accepts_excess(cls) -> bool:
    current = _class_new_lookup(cls)
    current_pin: int = 0
    if _ptr_can_have_header(current):
        current_pin = load_i32(current, PYOBJECTHEADER_FLAGS_OFFSET) & 64
        pcc_gc_pin(current)
    inherited = _object_new_cached(global_load_ptr("py_object_root_cache"))
    inherited_pin: int = 0
    if _ptr_can_have_header(inherited):
        inherited_pin = load_i32(inherited, PYOBJECTHEADER_FLAGS_OFFSET) & 64
        pcc_gc_pin(inherited)
    same: int = ptr_eq(current, inherited)
    if same == 0 and _ptr_can_have_header(current) and ptr_is_null(inherited) == 0:
        inherited_func = pcc_gc_load_ptr(inherited, ptr_add(inherited, PYSTATICMETHODOBJECT_FUNC_OFFSET))
        current_func = current
        if load_i32(current, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_STATICMETHOD:
            current_func = pcc_gc_load_ptr(current, ptr_add(current, PYSTATICMETHODOBJECT_FUNC_OFFSET))
        same = ptr_eq(current_func, inherited_func)
    if _ptr_can_have_header(inherited):
        pcc_gc_unpin(inherited)
        if inherited_pin != 0:
            atomic_rmw_i32("or", inherited, PYOBJECTHEADER_FLAGS_OFFSET, 64, "relaxed")
    py_decref(inherited)
    if _ptr_can_have_header(current):
        pcc_gc_unpin(current)
        if current_pin != 0:
            atomic_rmw_i32("or", current, PYOBJECTHEADER_FLAGS_OFFSET, 64, "relaxed")
    py_decref(current)
    if same == 0:
        return False
    init = _class_attr_lookup_in_mro(cls, cstr("__init__"))
    if ptr_is_null(init) == 0:
        py_decref(init)
        return True
    return ptr_is_null(_class_lookup_in_mro(cls, cstr("__init__"))) == 0


def _object_new_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    if py_tuple_len(args) != 3:
        py_raise_owned(py_exc_new(3, cstr("object.__new__() requires a class argument")))
        return null()
    cls = py_tuple_get(args, 0)
    extra_args = py_tuple_get(args, 1)
    kwargs = py_tuple_get(args, 2)
    has_excess: int = 0
    if py_tuple_len(extra_args) != 0 or py_dict_len(kwargs) != 0:
        has_excess = 1
    py_decref(extra_args)
    py_decref(kwargs)
    if not _ptr_is_class(cls):
        py_decref(cls)
        py_raise_owned(py_exc_new(3, cstr("object.__new__() argument must be a type")))
        return null()
    if not _object_allocator_supports_class(cls):
        py_decref(cls)
        py_raise_owned(py_exc_new(11, cstr("owned object allocator does not implement this builtin class layout")))
        return null()
    if has_excess != 0:
        if not _object_new_accepts_excess(cls):
            py_decref(cls)
            if py_err_occurred() == 0:
                py_raise_owned(py_exc_new(3, cstr("object.__new__() takes exactly one argument for this class")))
            return null()
    result = py_instance_new(cls)
    py_decref(cls)
    return _class_require_result(
        result, cstr("object.__new__"), cstr("object allocation failed without setting an exception")
    )


def _object_new_cache_fill(root, result_slot) -> None:
    """Called with the owned cache mutex, without the GC graph lock."""
    attrs = py_class_attrs_dict(root, 1)
    if ptr_is_null(attrs) != 0:
        _class_require_result(null(), cstr("object.__new__"), cstr("object allocator namespace allocation failed"))
        return
    prior_attrs_pin: int = load_i32(attrs, PYOBJECTHEADER_FLAGS_OFFSET) & 64
    pcc_gc_pin(attrs)
    key = py_str_new(cstr("__new__"), 7)
    if ptr_is_null(key) == 0:
        pcc_gc_pin(key)
        descriptor = py_dict_get(attrs, key)
        if ptr_is_null(descriptor) != 0:
            captures = _object_new_captures()
            pcc_gc_pin(captures)
            function = null()
            if ptr_is_null(captures) == 0 and py_err_occurred() == 0:
                if py_builtin_type_class_tag(root) == PY_TYPE_STR:
                    function = py_func_new_bound(_str_new_entry, captures, cstr("__new__"), null())
                else:
                    function = py_func_new_bound(_object_new_entry, captures, cstr("__new__"), null())
                pcc_gc_pin(function)
            pcc_gc_unpin(captures)
            py_decref(captures)
            if ptr_is_null(function) == 0:
                if py_err_occurred() == 0:
                    descriptor = py_staticmethod_new(function)
                    if ptr_is_null(descriptor) == 0:
                        pcc_gc_pin(descriptor)
                        if py_err_occurred() == 0:
                            py_dict_set(attrs, key, descriptor)
                            if py_err_occurred() == 0:
                                pcc_gc_store_root(result_slot, descriptor)
                        pcc_gc_unpin(descriptor)
                        py_decref(descriptor)
                pcc_gc_unpin(function)
                py_decref(function)
        else:
            prior_pin: int = load_i32(descriptor, PYOBJECTHEADER_FLAGS_OFFSET) & 64
            pcc_gc_pin(descriptor)
            pcc_gc_store_root(result_slot, descriptor)
            pcc_gc_unpin(descriptor)
            if prior_pin != 0:
                atomic_rmw_i32("or", descriptor, PYOBJECTHEADER_FLAGS_OFFSET, 64, "relaxed")
            py_decref(descriptor)
        pcc_gc_unpin(key)
        py_decref(key)
    pcc_gc_unpin(attrs)
    if prior_attrs_pin != 0:
        atomic_rmw_i32("or", attrs, PYOBJECTHEADER_FLAGS_OFFSET, 64, "relaxed")
    if ptr_is_null(load_ptr(result_slot, 0)) != 0:
        _class_require_result(null(), cstr("object.__new__"), cstr("object allocator callable allocation failed"))


def _object_new_cached(root) -> c_ptr:
    slot = stack_alloc(C_POINTER_SIZE)
    store_ptr(slot, 0, null())
    handle = pcc_gc_scheduler_root_register_handle(slot)
    if ptr_is_null(handle) != 0:
        return _class_require_result(null(), cstr("object.__new__"), cstr("object allocator result root registration failed"))
    mutex = _object_new_cache_mutex()
    if ptr_is_null(mutex) != 0 or pcc_mutex_lock(mutex) != 0:
        pcc_gc_scheduler_root_unregister_handle(handle)
        return _class_require_result(null(), cstr("object.__new__"), cstr("object allocator cache lock failed"))
    _object_new_cache_fill(root, slot)
    pcc_mutex_unlock(mutex)
    result = load_ptr(slot, 0)
    prior_pin: int = 0
    if ptr_is_null(result) == 0:
        prior_pin = load_i32(result, PYOBJECTHEADER_FLAGS_OFFSET) & 64
        pcc_gc_pin(result)
    pcc_gc_scheduler_root_unregister_handle(handle)
    return pcc_gc_take_pinned_slot(slot, prior_pin)


def _class_new_return_value(value, key, prior_pin: int) -> c_ptr:
    # The caller pins the owned lookup result before this helper can park.
    # Key cleanup must finish before the raw return reference is unpinned.
    slot = stack_alloc(C_POINTER_SIZE)
    store_ptr(slot, 0, value)
    pcc_gc_unpin(key)
    py_decref(key)
    return pcc_gc_take_pinned_slot(slot, prior_pin)


def _class_new_lookup(cls) -> c_ptr:
    """Resolve namespace and native method entries in the same C3 order."""
    root = global_load_ptr("py_object_root_cache")
    key = py_str_new(cstr("__new__"), 7)
    if ptr_is_null(key) != 0:
        return null()
    pcc_gc_pin(key)
    n_mro: int = load_i32(cls, PYCLASSOBJECT_N_MRO_OFFSET)
    mro = load_ptr(cls, PYCLASSOBJECT_MRO_OFFSET)
    i: int = 0
    while i < n_mro:
        owner = pcc_gc_load_ptr(cls, ptr_add(mro, i * C_POINTER_SIZE))
        if ptr_is_null(owner) == 0:
            if ptr_eq(owner, root) != 0:
                pcc_gc_unpin(key)
                py_decref(key)
                if not _object_allocator_supports_class(cls):
                    py_raise_owned(py_exc_new(11, cstr("owned __new__ lookup does not implement this builtin allocator")))
                    return null()
                return _object_new_cached(root)
            attrs = py_class_attrs_dict(owner, 0)
            if ptr_is_null(attrs) == 0:
                value = py_dict_get(attrs, key)
                if ptr_is_null(value) == 0:
                    prior_pin: int = 0
                    if _ptr_can_have_header(value):
                        prior_pin = load_i32(value, PYOBJECTHEADER_FLAGS_OFFSET) & 64
                    pcc_gc_pin(value)
                    return _class_new_return_value(value, key, prior_pin)
            n_methods: int = load_i32(owner, PYCLASSOBJECT_N_METHODS_OFFSET)
            methods = load_ptr(owner, PYCLASSOBJECT_METHODS_OFFSET)
            j: int = 0
            while j < n_methods:
                offset: int = j * PYCLASSMETHOD_SIZE
                method_name = load_ptr(methods, offset + PYCLASSMETHOD_NAME_OFFSET)
                if _strs_eq(method_name, cstr("__new__")) != 0:
                    value = pcc_gc_note_relocation_read(load_ptr(methods, offset + PYCLASSMETHOD_FUNC_OFFSET))
                    prior_pin: int = 0
                    if _ptr_can_have_header(value):
                        prior_pin = load_i32(value, PYOBJECTHEADER_FLAGS_OFFSET) & 64
                    pcc_gc_pin(value)
                    py_incref(value)
                    return _class_new_return_value(value, key, prior_pin)
                j = j + 1
            if py_builtin_type_class_tag(owner) == PY_TYPE_STR:
                pcc_gc_unpin(key)
                py_decref(key)
                return _object_new_cached(owner)
        i = i + 1
    pcc_gc_unpin(key)
    py_decref(key)
    return null()


def _metaclass_call_body(slots, pins):
    # The namespace owns descriptors; the native method table can also
    # contain a raw entry point. Preserve that distinction while binding.
    meta = load_ptr(slots, 4 * C_POINTER_SIZE)
    descriptor = _class_attr_lookup_in_mro(meta, cstr("__call__"))
    borrowed: int = 0
    if ptr_is_null(descriptor) != 0:
        if py_err_occurred() != 0:
            return null()
        descriptor = py_class_lookup(load_ptr(slots, 4 * C_POINTER_SIZE), cstr("__call__"))
        borrowed = 1
    if ptr_is_null(descriptor) != 0:
        # No override, including inherited default type.__call__. The caller
        # performs ordinary type construction instead of calling itself.
        return null()
    _instance_lookup_hold(slots, pins, 5, descriptor, borrowed)
    descriptor = load_ptr(slots, 5 * C_POINTER_SIZE)
    cls = load_ptr(slots, C_POINTER_SIZE)
    meta = load_ptr(slots, 4 * C_POINTER_SIZE)
    if not _ptr_can_have_header(descriptor):
        # py_instance_bind_method's owned wrapper already supports native
        # entries. Never read a PyObject header from an unmanaged code address.
        bound = py_instance_bind_method(descriptor, cls, cstr("__call__"))
    else:
        tag: int = load_i32(descriptor, PYOBJECTHEADER_TYPE_TAG_OFFSET)
        if tag == PY_TYPE_FUNC:
            bound = py_instance_bind_method(descriptor, cls, cstr("__call__"))
        elif tag == PY_TYPE_CLASSMETHOD:
            # A classmethod descriptor belongs to the metaclass, so bind its
            # owner once; do not also prepend the class being constructed.
            bound = _classmethod_bind(descriptor, meta)
        else:
            bound = _descriptor_call_get(descriptor, cls, meta)
            if ptr_is_null(bound) != 0:
                if py_err_occurred() != 0:
                    return null()
                bound = load_ptr(slots, 5 * C_POINTER_SIZE)
                py_incref(bound)
    if ptr_is_null(bound) != 0:
        return _class_require_result(null(), cstr("metaclass __call__ binding"),
                                     cstr("metaclass __call__ binding returned NULL without an exception"))
    _instance_lookup_hold(slots, pins, 6, bound, 0)
    result = py_obj_call(load_ptr(slots, 6 * C_POINTER_SIZE),
                         load_ptr(slots, 2 * C_POINTER_SIZE),
                         load_ptr(slots, 3 * C_POINTER_SIZE))
    return _class_require_result(result, cstr("metaclass __call__"),
                                 cstr("metaclass __call__ returned NULL without an exception"))


@c_abi_export("py_class_metaclass_call")
def py_class_metaclass_call(cls, args, kwargs):
    """One owned override result; NULL/no-error means ordinary construction.

    Special-method lookup uses the actual class's metaclass, never the class
    or instance namespace. Descriptor failures and failed callbacks preserve
    their exception and must not select the default construction path.
    """
    meta = _metaclass(cls)
    if ptr_is_null(meta) != 0:
        return null()
    if py_builtin_type_class_tag(meta) == PY_TYPE_CLASS:
        return null()
    # Reuse the eight-slot lookup lease protocol, including raw-method
    # classification, original pin states, aliasing and exception retirement.
    # result, cls, args, kwargs, metaclass, descriptor, bound callable, error.
    slots = stack_alloc(8 * C_POINTER_SIZE)
    pins = stack_alloc(8 * C_POINTER_SIZE)
    handles = stack_alloc(8 * C_POINTER_SIZE)
    memset(slots, 0, 8 * C_POINTER_SIZE)
    memset(pins, 0, 8 * C_POINTER_SIZE)
    memset(handles, 0, 8 * C_POINTER_SIZE)
    _instance_lookup_hold(slots, pins, 1, cls, 1)
    _instance_lookup_hold(slots, pins, 2, args, 1)
    _instance_lookup_hold(slots, pins, 3, kwargs, 1)
    _instance_lookup_hold(slots, pins, 4, meta, 1)
    count: int = 0
    while count < 8:
        handle = pcc_gc_scheduler_root_register_handle(ptr_add(slots, count * C_POINTER_SIZE))
        if ptr_is_null(handle) != 0:
            break
        store_ptr(handles, count * C_POINTER_SIZE, handle)
        count = count + 1
    if count == 8:
        result = _metaclass_call_body(slots, pins)
        _instance_lookup_hold(slots, pins, 0, result, 0)
    else:
        _class_require_result(null(), cstr("metaclass __call__"),
                              cstr("metaclass temporary root registration failed"))
    prior_result_pin: int = load_i64(pins, 0)
    saved_error_index: int = 7
    saved_error_offset: int = saved_error_index * C_POINTER_SIZE
    if py_err_occurred() != 0:
        _instance_lookup_hold(slots, pins, saved_error_index, py_current_exception(), 1)
    index: int = 6
    while index > 0:
        _instance_lookup_release(slots, pins, index)
        index = index - 1
    if ptr_is_null(load_ptr(slots, saved_error_offset)) == 0:
        py_raise(load_ptr(slots, saved_error_offset))
        _instance_lookup_release(slots, pins, saved_error_index)
    index = 0
    while index < count:
        pcc_gc_scheduler_root_unregister_handle(load_ptr(handles, index * C_POINTER_SIZE))
        index = index + 1
    return pcc_gc_take_pinned_slot(slots, prior_result_pin)


@c_abi_export("py_class_getattr")
def py_class_getattr(cls, name):
    if not _ptr_is_class(cls) or ptr_is_null(name) != 0:
        return null()
    cls = pcc_gc_note_relocation_read(cls)
    if _cstr_is_dunder_dict(name) != 0:
        attrs = py_class_attrs_dict(cls, 1)
        if ptr_is_null(attrs) == 0:
            py_incref(attrs)
        return attrs
    metaclass = _metaclass(cls)
    meta_attr = null()
    if ptr_is_null(metaclass) == 0:
        meta_attr = _class_attr_lookup_in_mro(metaclass, name)
        if ptr_is_null(meta_attr) == 0:
            if _descriptor_is_data(meta_attr):
                out = _descriptor_call_get(meta_attr, cls, metaclass)
                py_decref(meta_attr)
                if ptr_is_null(out) == 0 or py_err_occurred() != 0:
                    return out
            else:
                py_decref(meta_attr)
    if _strs_eq(name, cstr("__new__")) != 0:
        value = _class_new_lookup(cls)
    else:
        value = _class_attr_lookup_in_mro(cls, name)
    if py_err_occurred() != 0:
        return null()
    if ptr_is_null(value) == 0:
        bound = _classmethod_bind(value, cls)
        if ptr_eq(bound, value) == 0:
            py_decref(value)
            return bound
        descriptor_value = _descriptor_call_get(value, global_load_ptr("py_None"), cls)
        if ptr_is_null(descriptor_value) == 0 or py_err_occurred() != 0:
            py_decref(value)
            return descriptor_value
        return bound
    if ptr_is_null(metaclass) == 0:
        meta_attr = _class_attr_lookup_in_mro(metaclass, name)
        if ptr_is_null(meta_attr) == 0:
            out = _descriptor_call_get(meta_attr, cls, metaclass)
            if ptr_is_null(out) == 0 or py_err_occurred() != 0:
                py_decref(meta_attr)
                return out
            return meta_attr
    method = py_class_lookup(cls, name)
    if ptr_is_null(method) == 0:
        py_incref(method)
        return method
    if py_err_occurred() == 0:
        builtin_member = _builtin_mro_member(cls, name, 1)
        if ptr_is_null(builtin_member) == 0:
            return _object_member_value(builtin_member, null(), cls, name)
    if py_err_occurred() == 0 and _object_member_name(name) != 0 and _mro_builtin_defines(cls, name) == 0:
        member = _object_namespace_get(name)
        if ptr_is_null(member) == 0:
            return _object_member_value(member, null(), cls, name)
    return null()


@c_abi_export("py_class_setattr_raw")
def py_class_setattr_raw(cls, name, value) -> int:
    if not _ptr_is_class(cls):
        return -1
    if ptr_is_null(name) != 0 or ptr_is_null(value) != 0:
        return -1
    cls = pcc_gc_note_relocation_read(cls)
    attrs = py_class_attrs_dict(cls, 1)
    if ptr_is_null(attrs) != 0:
        return -1
    key = py_str_new(name, strlen(name))
    if ptr_is_null(key) != 0:
        return -1
    if _strs_eq(name, cstr("__del__")) != 0:
        _note_class_defines_del()
    py_dict_set(attrs, key, value)
    py_decref(key)
    _bump_class_attr_cache_epoch()
    return 0


def _class_method_alias_index(cls, name) -> int:
    """Index of ``name`` in ``cls``'s own method table, or -1."""
    signature = stack_alloc(8)
    _method_name_signature(name, signature)
    name_hash: int = load_i32(signature, 0) & 0xFFFFFFFF
    name_length: int = load_i32(signature, 4)
    count: int = load_i32(cls, PYCLASSOBJECT_N_METHODS_OFFSET)
    methods = load_ptr(cls, PYCLASSOBJECT_METHODS_OFFSET)
    found: int = -1
    index: int = 0
    while found < 0 and index < count:
        offset: int = index * PYCLASSMETHOD_SIZE
        if (load_i32(methods, offset + PYCLASSMETHOD_NAME_LENGTH_OFFSET) == name_length
                and (load_i32(methods, offset + PYCLASSMETHOD_NAME_HASH_OFFSET) & 0xFFFFFFFF) == name_hash
                and _strs_eq(load_ptr(methods, offset + PYCLASSMETHOD_NAME_OFFSET), name) != 0):
            found = index
        index = index + 1
    return found


def _class_method_alias_sync(cls, name, value) -> None:
    """Make ``cls``'s method-table alias of ``name`` follow its namespace entry.

    CPython's type lookup reads only the class ``__dict__``; pcc's method
    tables are borrowed aliases its MRO walks read first (construction,
    dunder dispatch, method calls). After ``Cls.name = value`` (``value`` a
    function) the alias is that function; after deletion or a non-function
    value there is none. A newly assigned function gains an alias under a
    copied name, since table names must outlive the class.
    """
    function: int = 0
    if (ptr_is_null(value) == 0 and is_tagged_int(value) == 0 and _ptr_can_have_header(value)
            and load_i32(value, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_FUNC):
        function = 1
    index: int = _class_method_alias_index(cls, name)
    if index >= 0:
        methods = load_ptr(cls, PYCLASSOBJECT_METHODS_OFFSET)
        offset: int = index * PYCLASSMETHOD_SIZE
        if function != 0:
            store_ptr(methods, offset + PYCLASSMETHOD_FUNC_OFFSET, value)
            _class_note_borrowed_metadata_slot_store(cls, ptr_add(methods, offset + PYCLASSMETHOD_FUNC_OFFSET), value)
        else:
            count: int = load_i32(cls, PYCLASSOBJECT_N_METHODS_OFFSET)
            tail: int = (count - index - 1) * PYCLASSMETHOD_SIZE
            if tail > 0:
                memmove(ptr_add(methods, offset), ptr_add(methods, offset + PYCLASSMETHOD_SIZE), tail)
            # The vacated last entry stays inside the registered payload span.
            memset(ptr_add(methods, (count - 1) * PYCLASSMETHOD_SIZE), 0, PYCLASSMETHOD_SIZE)
            store_i32(cls, PYCLASSOBJECT_N_METHODS_OFFSET, count - 1)
        if _strs_eq(name, cstr("__del__")) != 0:
            del_method = null()
            if function != 0:
                del_method = value
            store_ptr(cls, PYCLASSOBJECT_DEL_METHOD_OFFSET, del_method)
            _class_note_borrowed_metadata_slot_store(cls, ptr_add(cls, PYCLASSOBJECT_DEL_METHOD_OFFSET), del_method)
        _bump_class_attr_cache_epoch()
    elif function != 0:
        size: int = strlen(name) + 1
        stable = malloc(size)
        if ptr_is_null(stable) == 0:
            memcpy(stable, name, size)
            py_class_add_method(cls, stable, value)


@c_abi_export("py_class_setattr")
def py_class_setattr(cls, name, value) -> int:
    if not _ptr_is_class(cls):
        return -1
    if ptr_is_null(name) != 0 or ptr_is_null(value) != 0:
        return -1
    cls = pcc_gc_note_relocation_read(cls)
    metaclass = _metaclass(cls)
    if ptr_is_null(metaclass) == 0:
        meta_attr = _class_attr_lookup_in_mro(metaclass, name)
        if ptr_is_null(meta_attr) == 0:
            if _descriptor_is_data(meta_attr):
                rc: int = _descriptor_call_set(meta_attr, cls, value)
                py_decref(meta_attr)
                return rc
            py_decref(meta_attr)
    stored: int = py_class_setattr_raw(cls, name, value)
    if stored == 0:
        _class_method_alias_sync(pcc_gc_note_relocation_read(cls), name, value)
    return stored


@c_abi_export("py_class_delattr")
def py_class_delattr(cls, name) -> int:
    if not _ptr_is_class(cls) or ptr_is_null(name) != 0:
        return -1
    cls = pcc_gc_note_relocation_read(cls)
    metaclass = _metaclass(cls)
    if ptr_is_null(metaclass) == 0:
        meta_attr = _class_attr_lookup_in_mro(metaclass, name)
        if ptr_is_null(meta_attr) == 0:
            if _descriptor_is_data(meta_attr):
                rc: int = _descriptor_call_delete(meta_attr, cls)
                py_decref(meta_attr)
                return rc
            py_decref(meta_attr)
    attrs = py_class_attrs_dict(cls, 0)
    if ptr_is_null(attrs) != 0:
        return -1
    key = py_str_new(name, strlen(name))
    if ptr_is_null(key) != 0:
        return -1
    rc: int = py_dict_del(attrs, key)
    py_decref(key)
    if rc == 0:
        _class_method_alias_sync(pcc_gc_note_relocation_read(cls), name, null())
        _bump_class_attr_cache_epoch()
    return rc


@c_abi_export("py_class_attrs_dispose")
def py_class_attrs_dispose(cls) -> None:
    if ptr_is_null(cls) != 0:
        return
    attrs = load_ptr(cls, PYCLASSOBJECT_ATTRS_OFFSET)
    if ptr_is_null(attrs) == 0:
        store_ptr(cls, PYCLASSOBJECT_ATTRS_OFFSET, null())
        py_decref(attrs)


@c_abi_export("py_class_attrs_retarget")
def py_class_attrs_retarget(source, destination) -> int:
    if not _ptr_is_class(source) or not _ptr_is_class(destination):
        return -1
    attrs = pcc_gc_load_ptr(source, ptr_add(source, PYCLASSOBJECT_ATTRS_OFFSET))
    if ptr_is_null(attrs) != 0:
        return 0
    existing = pcc_gc_load_ptr(destination, ptr_add(destination, PYCLASSOBJECT_ATTRS_OFFSET))
    if ptr_is_null(existing) == 0:
        return -1
    pcc_gc_store_ptr(destination, ptr_add(destination, PYCLASSOBJECT_ATTRS_OFFSET), attrs)
    return 0


@c_abi_export("py_class_lookup")
def py_class_lookup(cls, name):
    if not _ptr_is_class(cls):
        return null()
    if ptr_is_null(name) != 0:
        return null()
    cls = pcc_gc_note_relocation_read(cls)
    if _cstr_is_dunder_name(name) != 0:
        cls_name = load_ptr(cls, PYCLASSOBJECT_NAME_OFFSET)
        if ptr_is_null(cls_name) != 0:
            return py_str_new(name, 0)
        return py_str_new(cls_name, strlen(cls_name))
    if _cstr_is_dunder_mro(name) != 0:
        n_mro: int = load_i32(cls, PYCLASSOBJECT_N_MRO_OFFSET)
        mro = load_ptr(cls, PYCLASSOBJECT_MRO_OFFSET)
        t = py_tuple_new(n_mro)
        i: int = 0
        while i < n_mro:
            item = pcc_gc_load_ptr(cls, ptr_add(mro, i * 8))
            py_tuple_set_item(t, i, item)
            i = i + 1
        return t
    if _strs_eq(name, cstr("__base__")) != 0:
        n_bases: int = load_i32(cls, PYCLASSOBJECT_N_BASES_OFFSET)
        bases = load_ptr(cls, PYCLASSOBJECT_BASES_OFFSET)
        if n_bases <= 0 or ptr_is_null(bases) != 0:
            return global_load_ptr("py_None")
        return pcc_gc_load_ptr(cls, bases)
    return _class_lookup_in_mro(cls, name)


@c_abi_export("py_class_add_method")
def py_class_add_method(cls, name, func) -> None:
    # ABI contract mirrors py_internal.h: name is immutable/lifetime-borrowed,
    # and callers serialize mutation of this realloc-backed method table.
    if not _ptr_is_class(cls):
        return
    if ptr_is_null(name) != 0:
        return
    signature = stack_alloc(8)
    _method_name_signature(name, signature)
    name_hash: int = load_i32(signature, 0) & 0xFFFFFFFF
    name_length: int = load_i32(signature, 4)
    cls = pcc_gc_note_relocation_read(cls)
    n_methods_i32: int = load_i32(cls, PYCLASSOBJECT_N_METHODS_OFFSET)
    new_n: int = n_methods_i32 + 1
    methods = load_ptr(cls, PYCLASSOBJECT_METHODS_OFFSET)
    new_methods = realloc(methods, new_n * PYCLASSMETHOD_SIZE)
    if ptr_is_null(new_methods) != 0:
        return
    method_off: int = n_methods_i32 * PYCLASSMETHOD_SIZE
    store_ptr(new_methods, method_off + PYCLASSMETHOD_NAME_OFFSET, name)
    store_ptr(new_methods, method_off + PYCLASSMETHOD_FUNC_OFFSET, func)
    store_i32(new_methods, method_off + PYCLASSMETHOD_NAME_HASH_OFFSET, name_hash)
    store_i32(new_methods, method_off + PYCLASSMETHOD_NAME_LENGTH_OFFSET, name_length)
    store_ptr(cls, PYCLASSOBJECT_METHODS_OFFSET, new_methods)
    store_i32(cls, PYCLASSOBJECT_N_METHODS_OFFSET, new_n)
    payload_offset: int = -1
    if ptr_is_null(methods) == 0:
        if ptr_eq(methods, new_methods) == 0:
            payload_offset = pcc_gc_backend4_zpage_retarget_owner_payload_span(
                cls,
                methods,
                new_methods,
                new_n * PYCLASSMETHOD_SIZE,
            )
    if payload_offset < 0:
        if ptr_is_null(methods) == 0:
            if ptr_eq(methods, new_methods) == 0:
                pcc_gc_backend4_zpage_unregister_owner_payload_span(cls, methods)
        pcc_gc_backend4_zpage_register_owner_payload_span(
            cls, new_methods, new_n * PYCLASSMETHOD_SIZE
        )
    _class_note_borrowed_metadata_slot_store(
        cls,
        ptr_add(new_methods, method_off + 8),
        func,
    )
    if _strs_eq(name, cstr("__del__")) != 0:
        # Borrowed update-only alias for GC forwarding.  py_user_del_dispatch
        # deliberately resolves through py_class_lookup rather than treating
        # this slot as a separate semantic cache.
        _note_class_defines_del()
        store_ptr(cls, PYCLASSOBJECT_DEL_METHOD_OFFSET, func)
        _class_note_borrowed_metadata_slot_store(cls, ptr_add(cls, PYCLASSOBJECT_DEL_METHOD_OFFSET), func)
    # A newly installed data descriptor can shadow an already-cached instance
    # field. Release-publish only after the whole method-table/GC metadata
    # transaction is complete, matching the C oracle.
    _bump_class_attr_cache_epoch()


@c_abi_export("py_class_set_metaclass")
def py_class_set_metaclass(cls, metaclass) -> None:
    if not _ptr_is_class(cls):
        return
    cls = pcc_gc_note_relocation_read(cls)
    if ptr_is_null(metaclass) == 0:
        if not _ptr_is_class(metaclass):
            return
        metaclass = pcc_gc_note_relocation_read(metaclass)
    # A metaclass is a real class owner, even while user classes remain
    # immortal. The retaining store also retires the previous relation.
    pcc_gc_store_ptr(
        cls, ptr_add(cls, PYCLASSOBJECT_METACLASS_OFFSET), metaclass,
    )


@c_abi_export("py_instance_new")
def py_instance_new(cls) -> c_ptr:
    if not _ptr_is_class(cls):
        return null()
    cls = pcc_gc_note_relocation_read(cls)
    n_fields_i32: int = load_i32(cls, PYCLASSOBJECT_N_FIELDS_OFFSET)
    if n_fields_i32 < 0:
        n_fields_i32 = 0
    n_slots: int = _instance_storage_slot_count(cls)
    size: int = PYINSTANCEOBJECT_SIZE + n_slots * C_POINTER_SIZE
    inst = pcc_gc_alloc(
        size,
        load_i32(cls, PYCLASSOBJECT_TYPE_TAG_ALLOC_OFFSET),
        0,
    )
    if ptr_is_null(inst) != 0:
        return null()
    memset(
        ptr_add(inst, PYINSTANCEOBJECT_CLS_OFFSET),
        0,
        size - PYINSTANCEOBJECT_CLS_OFFSET,
    )
    store_i64(inst, PYOBJECTHEADER_REFCOUNT_OFFSET, 1)
    type_tag_alloc: int = load_i32(cls, PYCLASSOBJECT_TYPE_TAG_ALLOC_OFFSET)
    store_i32(inst, PYOBJECTHEADER_TYPE_TAG_OFFSET, type_tag_alloc)
    store_ptr(inst, PYINSTANCEOBJECT_CLS_OFFSET, cls)
    py_gc_track(inst)
    # The freshly allocated instance is already a new reference; keep this as
    # a raw pointer expression so return lowering does not retain it again.
    return ptr_add(inst, 0)


@c_abi_export("py_instance_get_field")
def py_instance_get_field(inst, idx: int):
    # Every static `self.field` read lands here.  With the read barrier off
    # and a non-forwarding collector, pcc_gc_load_ptr is a plain load and
    # pcc_gc_note_relocation_read the identity, so this path makes the same
    # checks -- provenance probe included -- without calling them.
    if (
        load_i32(global_addr("pcc_gc_read_barrier_enabled"), 0) == 0
        and load_i32(global_addr("pcc_gc_backend_selected"), 0) != 3
        and load_i32(global_addr("pcc_gc_backend_selected"), 0) != 4
    ):
        if ptr_is_null(inst) != 0 or is_tagged_int(inst) != 0:
            return null()
        if pcc_gc_pointer_is_managed(inst) == 0:
            return null()
        tag: int = load_i32(inst, PYOBJECTHEADER_TYPE_TAG_OFFSET)
        if tag != PY_TYPE_INSTANCE and tag < PY_TYPE_USER_CLASS_START:
            return null()
        cls = load_ptr(inst, PYINSTANCEOBJECT_CLS_OFFSET)
        if ptr_is_null(cls) != 0:
            return null()
        if load_i32(cls, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_CLASS:
            return null()
        if idx < 0 or idx >= load_i32(cls, PYCLASSOBJECT_N_FIELDS_OFFSET):
            return null()
        field = load_ptr(inst, PYINSTANCEOBJECT_FIELDS_OFFSET + idx * C_POINTER_SIZE)
        if ptr_is_null(field) == 0:
            py_incref(field)
        else:
            return _instance_missing_field_lookup(inst, idx)
        return field
    if not _ptr_is_instance(inst):
        return null()
    if idx < 0:
        return null()
    # Validation heals its own local copy. Derive every subsequent slot from
    # the current receiver, rather than the caller's old relocation address.
    inst = pcc_gc_note_relocation_read(inst)
    cls = pcc_gc_load_ptr(inst, ptr_add(inst, PYINSTANCEOBJECT_CLS_OFFSET))
    n_fields: int = load_i32(cls, PYCLASSOBJECT_N_FIELDS_OFFSET)
    if idx >= n_fields:
        return null()
    fields_base = ptr_add(inst, PYINSTANCEOBJECT_FIELDS_OFFSET)
    v = pcc_gc_load_ptr(inst, ptr_add(fields_base, idx * C_POINTER_SIZE))
    if ptr_is_null(v) == 0:
        py_incref(v)
    else:
        return _instance_missing_field_lookup(inst, idx)
    return v


def _instance_missing_field_lookup(inst, idx: int, default_only: int = 0):
    """A valid but unbound physical slot still obeys ordinary lookup.

    NULL is absence, not Python None. Resolve the stable field name and let
    semantic lookup handle class defaults, descriptors, __getattr__, and the
    missing-attribute exception. Invalid indices never enter this helper.
    """
    return _instance_lookup_rooted(inst, idx, null(), default_only)


def _instance_lookup_rooted(inst, idx: int, name, lookup_kind: int):
    """Keep lookup inputs current while the rooted callback path runs.

    A nonnegative index resolves an unbound physical field's stable name;
    otherwise the caller supplies the attribute name. Lookup kinds are normal
    (0), default (1), custom __getattribute__ (2), and vars (3).  The vars
    route selects a canonical module namespace after input admission; other
    instances retain the existing fixed-field snapshot path.
    """
    roots = stack_alloc(3 * C_POINTER_SIZE)
    handles = stack_alloc(3 * C_POINTER_SIZE)
    pins = stack_alloc(2 * C_POINTER_SIZE)
    memset(roots, 0, 3 * C_POINTER_SIZE)
    memset(handles, 0, 3 * C_POINTER_SIZE)
    memset(pins, 0, 2 * C_POINTER_SIZE)
    pcc_py_gc_minor_graph_lock()
    inst = pcc_gc_note_relocation_read(inst)
    cls = pcc_gc_load_ptr(inst, ptr_add(inst, PYINSTANCEOBJECT_CLS_OFFSET))
    if lookup_kind == _INSTANCE_LOOKUP_VARS and not _ptr_is_class(cls):
        pcc_py_gc_minor_graph_unlock()
        py_raise_owned(py_exc_new(3, cstr("vars() argument has no __dict__")))
        return null()
    store_ptr(roots, 0, inst)
    store_ptr(roots, C_POINTER_SIZE, cls)
    index: int = 0
    while index < 2:
        value = load_ptr(roots, index * C_POINTER_SIZE)
        store_i64(pins, index * C_POINTER_SIZE, load_i32(value, PYOBJECTHEADER_FLAGS_OFFSET) & 64)
        pcc_gc_pin(value)
        index = index + 1
    index = 0
    while index < 3:
        handle = pcc_gc_scheduler_root_register_handle(ptr_add(roots, index * C_POINTER_SIZE))
        if ptr_is_null(handle) != 0:
            break
        store_ptr(handles, index * C_POINTER_SIZE, handle)
        index = index + 1
    pcc_py_gc_minor_graph_unlock()
    if index == 3:
        cls = pcc_gc_load_ptr(null(), ptr_add(roots, C_POINTER_SIZE))
        if idx >= 0:
            names = load_ptr(cls, PYCLASSOBJECT_FIELD_NAMES_OFFSET)
            name = load_ptr(names, idx * C_POINTER_SIZE)
        if lookup_kind == _INSTANCE_LOOKUP_VARS:
            # The admitted receiver and class stay pinned while read barriers
            # may park. Canonical runtime module objects own their live dict;
            # class names and matching field layouts do not prove that type.
            module_cls = pcc_gc_load_ptr(
                null(), global_addr("pcc_runtime_module_class_cache"),
            )
            if ptr_is_null(module_cls) == 0 and ptr_eq(cls, module_cls) != 0:
                result = py_obj_getattr(pcc_gc_load_ptr(null(), roots), name)
            else:
                result = py_instance_vars(pcc_gc_load_ptr(null(), roots))
        elif lookup_kind != 0:
            result = _instance_getattr_default_rooted(
                pcc_gc_load_ptr(null(), roots), cls, name, lookup_kind - 1,
            )
        else:
            result = py_obj_getattr(pcc_gc_load_ptr(null(), roots), name)
        store_ptr(roots, 2 * C_POINTER_SIZE, result)
    prior_result_pin: int = 0
    pcc_py_gc_minor_graph_lock()
    result = pcc_gc_load_ptr(null(), ptr_add(roots, 2 * C_POINTER_SIZE))
    if ptr_is_null(result) == 0 and is_tagged_int(result) == 0:
        prior_result_pin = load_i32(result, PYOBJECTHEADER_FLAGS_OFFSET) & 64
        if ptr_eq(result, load_ptr(roots, 0)):
            prior_result_pin = load_i64(pins, 0)
        if ptr_eq(result, load_ptr(roots, C_POINTER_SIZE)):
            prior_result_pin = load_i64(pins, C_POINTER_SIZE)
        pcc_gc_pin(result)
    pcc_py_gc_minor_graph_unlock()
    cleanup_index: int = 0
    pcc_py_gc_minor_graph_lock()
    while cleanup_index < 2:
        value = pcc_gc_load_ptr(null(), ptr_add(roots, cleanup_index * C_POINTER_SIZE))
        pcc_gc_unpin(value)
        if ptr_eq(value, result) != 0 or load_i64(pins, cleanup_index * C_POINTER_SIZE) != 0:
            # Balance this input's pin lease, but preserve the original bit
            # or the independent result lease. No moving window under lock.
            atomic_rmw_i32("or", value, PYOBJECTHEADER_FLAGS_OFFSET, 64, "relaxed")
        cleanup_index = cleanup_index + 1
    pcc_py_gc_minor_graph_unlock()
    cleanup_index = 0
    while cleanup_index < 3:
        handle = load_ptr(handles, cleanup_index * C_POINTER_SIZE)
        if ptr_is_null(handle) == 0:
            pcc_gc_scheduler_root_unregister_handle(handle)
        cleanup_index = cleanup_index + 1
    if index != 3:
        return _class_require_result(null(), cstr("instance field lookup"), cstr("instance field lookup root registration failed"))
    return pcc_gc_take_pinned_slot(ptr_add(roots, 2 * C_POINTER_SIZE), prior_result_pin)


@c_abi_export("py_instance_set_field")
def py_instance_set_field(inst, idx: int, value) -> None:
    if not _ptr_is_instance(inst):
        return
    if idx < 0:
        return
    cls = pcc_gc_load_ptr(inst, ptr_add(inst, PYINSTANCEOBJECT_CLS_OFFSET))
    n_fields: int = load_i32(cls, PYCLASSOBJECT_N_FIELDS_OFFSET)
    if idx >= n_fields:
        return
    fields_base = ptr_add(inst, PYINSTANCEOBJECT_FIELDS_OFFSET)
    pcc_gc_store_ptr(inst, ptr_add(fields_base, idx * C_POINTER_SIZE), value)


@c_abi_export("py_valuebox_new")
def py_valuebox_new(cls) -> c_ptr:
    box = py_instance_new(cls)
    if ptr_is_null(box) != 0:
        return null()
    store_i32(box, PYOBJECTHEADER_TYPE_TAG_OFFSET, PY_TYPE_VALUEBOX)
    # py_instance_new already returned a new reference.
    return ptr_add(box, 0)


@c_abi_export("py_valuebox_get_field")
def py_valuebox_get_field(box, idx: int):
    return py_instance_get_field(box, idx)


@c_abi_export("py_valuebox_set_field")
def py_valuebox_set_field(box, idx: int, value) -> None:
    py_instance_set_field(box, idx, value)


@c_abi_export("py_instance_getattr_default")
def py_instance_getattr_default(inst, name):
    if not _ptr_is_instance(inst):
        return null()
    if ptr_is_null(name) != 0:
        return null()
    cls = pcc_gc_load_ptr(inst, ptr_add(inst, PYINSTANCEOBJECT_CLS_OFFSET))
    return _instance_getattr_default(inst, cls, name)


def _instance_getattr_default(inst, cls, name):
    return _instance_getattr_default_body(inst, cls, name, null(), null())


def _instance_lookup_hold(slots, pins, index: int, value, borrowed: int):
    """Keep one slow-lookup temporary current and owned across callbacks.

    Each slot is assigned once. Inputs are already protected by the missing
    field wrapper. Native method entries are not managed objects or owners.
    """
    if ptr_is_null(slots) != 0:
        return value
    slot = ptr_add(slots, index * C_POINTER_SIZE)
    store_ptr(slot, 0, value)
    store_i64(pins, index * C_POINTER_SIZE, -1)
    pcc_py_gc_minor_graph_lock()
    value = pcc_gc_load_ptr(null(), slot)
    store_ptr(slot, 0, value)
    if ptr_is_null(value) == 0 and is_tagged_int(value) == 0:
        if _ptr_can_have_header(value):
            prior: int = load_i32(value, PYOBJECTHEADER_FLAGS_OFFSET) & 64
            other: int = 0
            while other < 8:
                if other != index and ptr_eq(value, load_ptr(slots, other * C_POINTER_SIZE)) != 0:
                    other_pin: int = load_i64(pins, other * C_POINTER_SIZE)
                    if other_pin >= 0:
                        prior = prior & other_pin
                other = other + 1
            store_i64(pins, index * C_POINTER_SIZE, prior)
            pcc_gc_pin(value)
            if borrowed != 0:
                py_incref(value)
    pcc_py_gc_minor_graph_unlock()
    return value


def _instance_lookup_release(slots, pins, index: int) -> None:
    slot = ptr_add(slots, index * C_POINTER_SIZE)
    value = load_ptr(slot, 0)
    if ptr_is_null(value) != 0:
        return
    prior_pin: int = load_i64(pins, index * C_POINTER_SIZE)
    if ptr_is_null(value) == 0 and is_tagged_int(value) == 0:
        if prior_pin < 0:
            # A raw native method pointer has no refcount or pin lease.
            store_ptr(slot, 0, null())
            return
        other: int = 0
        while other < 8:
            if other != index and ptr_eq(value, load_ptr(slots, other * C_POINTER_SIZE)) != 0:
                if load_i64(pins, other * C_POINTER_SIZE) >= 0:
                    prior_pin = prior_pin | 64
            other = other + 1
    # This strict primitive clears the root and balances exactly one lease
    # without a park before decref takes over ownership of the raw argument.
    py_decref(pcc_gc_take_pinned_slot(slot, prior_pin))


def _instance_getattr_default_rooted(inst, cls, name, custom_lookup: int):
    # result, class attribute, dynamic dict, dict key, method, callback key,
    # callback arguments, saved error. Input leases belong to the caller.
    slots = stack_alloc(8 * C_POINTER_SIZE)
    pins = stack_alloc(8 * C_POINTER_SIZE)
    handles = stack_alloc(8 * C_POINTER_SIZE)
    memset(slots, 0, 8 * C_POINTER_SIZE)
    memset(pins, 0, 8 * C_POINTER_SIZE)
    memset(handles, 0, 8 * C_POINTER_SIZE)
    count: int = 0
    while count < 8:
        handle = pcc_gc_scheduler_root_register_handle(ptr_add(slots, count * C_POINTER_SIZE))
        if ptr_is_null(handle) != 0:
            break
        store_ptr(handles, count * C_POINTER_SIZE, handle)
        count = count + 1
    if count == 8:
        if custom_lookup != 0:
            result = _instance_getattr_custom_body(inst, cls, name, slots, pins)
        else:
            result = _instance_getattr_default_body(inst, cls, name, slots, pins)
        _instance_lookup_hold(slots, pins, 0, result, 0)
    prior_result_pin: int = load_i64(pins, 0)
    index: int = 7
    while index > 0:
        if ptr_eq(load_ptr(slots, 0), load_ptr(slots, index * C_POINTER_SIZE)) != 0:
            prior_result_pin = load_i64(pins, index * C_POINTER_SIZE)
        index = index - 1
    saved_error_index: int = 7
    saved_error_offset: int = saved_error_index * C_POINTER_SIZE
    if py_err_occurred() != 0:
        _instance_lookup_hold(slots, pins, saved_error_index, py_current_exception(), 1)
    # Release every owner once, keeping any remaining aliased lease pinned.
    # The result and original exception survive arbitrary finalizer callbacks.
    index = 6
    while index > 0:
        _instance_lookup_release(slots, pins, index)
        index = index - 1
    if ptr_is_null(load_ptr(slots, saved_error_offset)) == 0:
        py_raise(load_ptr(slots, saved_error_offset))
        _instance_lookup_release(slots, pins, saved_error_index)
    index = 0
    while index < count:
        pcc_gc_scheduler_root_unregister_handle(load_ptr(handles, index * C_POINTER_SIZE))
        index = index + 1
    if count != 8:
        return _class_require_result(null(), cstr("instance field lookup"),
                                     cstr("instance field temporary root registration failed"))
    return pcc_gc_take_pinned_slot(slots, prior_result_pin)


def _instance_lookup_descriptor(descriptor, inst, cls, slots, pins):
    if ptr_is_null(slots) != 0:
        return _descriptor_call_get(descriptor, inst, cls)
    if ptr_is_null(descriptor) != 0 or is_tagged_int(descriptor) != 0:
        return null()
    tag: int = load_i32(descriptor, PYOBJECTHEADER_TYPE_TAG_OFFSET)
    if tag == PY_TYPE_CLASSMETHOD:
        # Non-data lookup reaches here after the instance namespace. Bind
        # the actual receiver class, including inherited subclass access.
        return _classmethod_bind(descriptor, cls)
    if tag == PY_TYPE_STATICMETHOD:
        value = pcc_gc_load_ptr(descriptor, ptr_add(descriptor, PYSTATICMETHODOBJECT_FUNC_OFFSET))
        py_incref(value)
        return value
    count: int = 3
    method = null()
    if tag == PY_TYPE_PROPERTY:
        method = pcc_gc_load_ptr(descriptor, ptr_add(descriptor, PYPROPERTYOBJECT_FGET_OFFSET))
        if ptr_is_null(method) != 0:
            py_raise_owned(py_exc_new(6, cstr("unreadable attribute")))
            return null()
        if ptr_eq(inst, global_load_ptr("py_None")) != 0:
            py_incref(descriptor)
            return descriptor
        count = 1
    else:
        method = _descriptor_method(descriptor, cstr("__get__"))
        if ptr_is_null(method) != 0:
            return null()
    method = _instance_lookup_hold(slots, pins, 4, method, 1)
    args = _instance_lookup_hold(slots, pins, 6, py_tuple_new(count), 0)
    if ptr_is_null(args) != 0:
        return _class_require_result(null(), cstr("py_tuple_new"),
                                     cstr("class callback argument tuple allocation failed"))
    if count == 1:
        py_tuple_set_item(args, 0, inst)
    else:
        py_tuple_set_item(args, 0, descriptor)
        if py_err_occurred() != 0:
            return null()
        py_tuple_set_item(args, 1, inst)
        if py_err_occurred() != 0:
            return null()
        py_tuple_set_item(args, 2, cls)
    if py_err_occurred() != 0:
        return null()
    return _class_require_result(
        py_obj_call(method, args, global_load_ptr("py_None")),
        cstr("descriptor __get__"),
        cstr("class callback returned NULL without setting an exception"),
    )


def _instance_lookup_call(method, inst, key, slots, pins, args_index: int):
    if _ptr_can_have_header(method):
        if load_i32(method, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_FUNC:
            args = _instance_lookup_hold(slots, pins, args_index, py_tuple_new(2), 0)
            if ptr_is_null(args) != 0:
                return _class_require_result(null(), cstr("py_tuple_new"),
                                             cstr("class callback argument tuple allocation failed"))
            py_tuple_set_item(args, 0, inst)
            if py_err_occurred() != 0:
                return null()
            py_tuple_set_item(args, 1, key)
            if py_err_occurred() != 0:
                return null()
            return py_obj_call(method, args, null())
    return call_ptr2(method, inst, key)


def _instance_getattr_custom_body(inst, cls, name, slots, pins):
    # Empty-field custom lookup uses the same temporary ownership frame as
    # default lookup. Function objects and native method pointers are distinct.
    method = _instance_lookup_hold(
        slots, pins, 4, _class_lookup_in_mro(cls, cstr("__getattribute__")), 1,
    )
    if py_err_occurred() != 0:
        return null()
    if ptr_is_null(method) != 0:
        return _instance_getattr_default_body(inst, cls, name, slots, pins)
    key = _instance_lookup_hold(slots, pins, 5, py_str_new(name, strlen(name)), 0)
    if ptr_is_null(key) != 0:
        return _class_require_result(null(), cstr("py_str_new"),
                                     cstr("instance attribute key allocation failed"))
    got = _instance_lookup_call(method, inst, key, slots, pins, 6)
    _class_require_result(got, cstr("__getattribute__"),
                          cstr("class callback returned NULL without setting an exception"))
    if ptr_is_null(got) == 0:
        return got
    if py_err_occurred() != 0:
        current = _instance_lookup_hold(slots, pins, 1, py_current_exception(), 1)
        attr_cls = py_exc_builtin_class(6)
        if ptr_is_null(attr_cls) == 0:
            if py_exc_matches(current, attr_cls) != 0:
                fallback = _instance_lookup_hold(
                    slots, pins, 2, _class_lookup_in_mro(cls, cstr("__getattr__")), 1,
                )
                if ptr_is_null(fallback) == 0:
                    py_clear_exception()
                    got = _instance_lookup_call(fallback, inst, key, slots, pins, 3)
                    return _class_require_result(
                        got, cstr("__getattr__"),
                        cstr("class callback returned NULL without setting an exception"),
                    )
    return null()


def _instance_getattr_default_body(inst, cls, name, slots, pins):
    """`py_instance_getattr_default` for an instance the caller validated,
    with its class `cls` and a non-NULL `name`."""
    # `__class__` and `__dict__` return below before any outcome is recorded,
    # so a cached entry can never answer for them.
    field_fallback_rooted: int = 0
    if ptr_is_null(slots) == 0:
        field_fallback_rooted = 1
    entry = _attr_res_find_any(cls, name)
    if field_fallback_rooted != 0:
        # Resolve mutable descriptors afresh on the uncommon empty-slot path.
        entry = null()
    # A generic lookup can reach an empty field without the indexed getter.
    # Protect its newly exposed allocating/callback path, while keeping the
    # populated field-cache hit constant-time. The rooted helper enters this
    # body with the guard disabled rather than recursively looking up itself.
    if field_fallback_rooted == 0:
        if ptr_is_null(entry) != 0 or load_i32(entry, 12) == _ATTR_RES_DATA_DESCRIPTOR:
            empty_index: int = _lookup_field_index(cls, name)
            if empty_index >= 0:
                inst = pcc_gc_note_relocation_read(inst)
                empty_value = pcc_gc_load_ptr(inst, ptr_add(inst, PYINSTANCEOBJECT_FIELDS_OFFSET + empty_index * C_POINTER_SIZE))
                if ptr_is_null(empty_value) != 0:
                    return _instance_missing_field_lookup(inst, empty_index, 1)
    if ptr_is_null(entry) == 0:
        kind: int = load_i32(entry, 12)
        if kind == _ATTR_RES_FIELD:
            fields_base_hit = ptr_add(inst, PYINSTANCEOBJECT_FIELDS_OFFSET)
            field_hit = pcc_gc_load_ptr(
                inst, ptr_add(fields_base_hit, load_i64(entry, 16) * C_POINTER_SIZE)
            )
            if ptr_is_null(field_hit) == 0:
                py_incref(field_hit)
                return field_hit
            if field_fallback_rooted == 0:
                return _instance_missing_field_lookup(inst, load_i64(entry, 16), 1)
        if kind == _ATTR_RES_METHOD:
            if ptr_is_null(_instance_dict_of(inst, cls)) != 0:
                return py_instance_bind_method(load_ptr(entry, 16), inst, name)
        if kind == _ATTR_RES_ABSENT:
            return _instance_dict_attr(inst, cls, name)
        if kind == _ATTR_RES_DATA_DESCRIPTOR:
            got_hit = _descriptor_call_get(load_ptr(entry, 16), inst, cls)
            if ptr_is_null(got_hit) == 0:
                return got_hit
            if py_err_occurred() != 0:
                return null()
    outcome_epoch: int = _class_attr_cache_epoch()
    if _cstr_is_dunder_class(name) != 0:
        if ptr_is_null(cls) == 0:
            py_incref(cls)
        return cls
    if _cstr_is_dunder_dict(name) != 0:
        dyn_slot = _dynamic_attr_slot(inst)
        if ptr_is_null(dyn_slot) != 0:
            return null()
        dyn = pcc_gc_load_ptr(inst, dyn_slot)
        if ptr_is_null(dyn) != 0:
            dyn = py_dict_new()
            if ptr_is_null(dyn) != 0:
                return null()
            dyn = _instance_lookup_hold(slots, pins, 2, dyn, 0)
            pcc_gc_store_ptr(inst, dyn_slot, dyn)
            if field_fallback_rooted == 0:
                py_decref(dyn)
        else:
            dyn = _instance_lookup_hold(slots, pins, 2, dyn, 1)
        py_incref(dyn)
        return dyn
    class_attr = _instance_lookup_hold(
        slots, pins, 1, _class_attr_lookup_in_mro(cls, name), 0,
    )
    if py_err_occurred() != 0:
        return null()
    if ptr_is_null(class_attr) == 0:
        if _descriptor_is_data(class_attr):
            if _attr_res_method_cacheable() != 0:
                _attr_res_store(
                    cls, name, _ATTR_RES_DATA_DESCRIPTOR,
                    ptr_to_int(class_attr), outcome_epoch,
                )
            got = _instance_lookup_descriptor(class_attr, inst, cls, slots, pins)
            if ptr_is_null(got) == 0:
                if field_fallback_rooted == 0:
                    py_decref(class_attr)
                return got
            if py_err_occurred() != 0:
                if field_fallback_rooted == 0:
                    py_decref(class_attr)
                return null()
    idx: int = _lookup_field_index(cls, name)
    if idx >= 0:
        # Reached only when no data descriptor shadows the field.
        _attr_res_store(cls, name, _ATTR_RES_FIELD, idx, outcome_epoch)
        fields_base = ptr_add(inst, PYINSTANCEOBJECT_FIELDS_OFFSET)
        v = pcc_gc_load_ptr(inst, ptr_add(fields_base, idx * C_POINTER_SIZE))
        if ptr_is_null(v) == 0:
            py_incref(v)
            if field_fallback_rooted == 0:
                py_decref(class_attr)
            return v
    # A method outcome holds only while the instance has no dynamic dict.
    method_cacheable: int = _attr_res_method_cacheable()
    if idx >= 0:
        # A later direct slot store must immediately shadow a callable class
        # default; a cached method outcome cannot observe that store.
        method_cacheable = 0
    dyn_slot = _dynamic_attr_slot(inst)
    if ptr_is_null(dyn_slot) == 0:
        dyn = pcc_gc_load_ptr(inst, dyn_slot)
        if ptr_is_null(dyn) == 0:
            method_cacheable = 0
            dyn = _instance_lookup_hold(slots, pins, 2, dyn, 1)
            key = _instance_lookup_hold(slots, pins, 3, py_str_new(name, strlen(name)), 0)
            if ptr_is_null(key) != 0:
                return _class_require_result(null(), cstr("py_str_new"),
                                             cstr("instance attribute key allocation failed"))
            got = py_dict_get(dyn, key)
            if field_fallback_rooted == 0:
                py_decref(key)
            if ptr_is_null(got) == 0:
                if ptr_is_null(class_attr) != 0 and idx < 0:
                    _attr_res_store_dict_only(cls, name, outcome_epoch)
                if field_fallback_rooted == 0:
                    py_decref(class_attr)
                return got
            if py_err_occurred() != 0:
                if field_fallback_rooted == 0:
                    py_decref(class_attr)
                return null()
    if ptr_is_null(class_attr) == 0:
        if is_tagged_int(class_attr) == 0:
            if load_i32(class_attr, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_FUNC:
                if method_cacheable != 0:
                    _attr_res_store(
                        cls, name, _ATTR_RES_METHOD, ptr_to_int(class_attr),
                        outcome_epoch,
                    )
                bound = py_instance_bind_method(class_attr, inst, name)
                if field_fallback_rooted == 0:
                    py_decref(class_attr)
                return bound
        got = _instance_lookup_descriptor(class_attr, inst, cls, slots, pins)
        if ptr_is_null(got) == 0:
            if field_fallback_rooted == 0:
                py_decref(class_attr)
            return got
        if py_err_occurred() != 0:
            if field_fallback_rooted == 0:
                py_decref(class_attr)
            return null()
        if field_fallback_rooted != 0:
            # The temporary frame owns the lookup reference until cleanup.
            py_incref(class_attr)
        return class_attr
    if ptr_is_null(cls) != 0:
        return null()
    method = _instance_lookup_hold(slots, pins, 4, _class_lookup_in_mro(cls, name), 1)
    if py_err_occurred() != 0:
        return null()
    if ptr_is_null(method) == 0:
        # Method tables may hold raw entry points; only function objects are
        # recorded, so a cached method is always safe to call directly.
        if method_cacheable != 0 and _ptr_can_have_header(method) and load_i32(
            method, PYOBJECTHEADER_TYPE_TAG_OFFSET
        ) == PY_TYPE_FUNC:
            _attr_res_store(
                cls, name, _ATTR_RES_METHOD, ptr_to_int(method), outcome_epoch
            )
        return py_instance_bind_method(method, inst, name)
    # dict-subclass inherited method fallback (get / keys / values / items /
    # pop / setdefault / clear). User methods/attrs above win; this only fires
    # for names the user class did not define. PY_CLASS_FLAG_DICT_SUBCLASS is
    # bit 2 (value 4) in the class header flags at offset 12. Returns a bound
    # native callable, or NULL -> fall through to __getattr__ / AttributeError.
    ds_flags: int = load_i32(cls, PYOBJECTHEADER_FLAGS_OFFSET)
    if (ds_flags & 4) != 0:
        dm = py_dict_subclass_getattr(inst, name)
        if ptr_is_null(dm) == 0:
            return dm
        if py_err_occurred() != 0:
            return null()
    if (ds_flags & 16777216) != 0:
        # The builtin exception bases' members, in MRO order, before object's.
        exc_member = _builtin_mro_member(cls, name, 0)
        if ptr_is_null(exc_member) == 0:
            return _object_member_value(exc_member, inst, cls, name)
        if py_err_occurred() != 0:
            return null()
    if _object_member_name(name) != 0 and _mro_builtin_defines(cls, name) == 0:
        # object's members come before __getattr__, as CPython's normal
        # lookup finds them in ``object.__dict__``.
        member = _object_namespace_get(name)
        if ptr_is_null(member) == 0:
            return _object_member_value(member, inst, cls, name)
        if py_err_occurred() != 0:
            return null()
    getattr_method = _instance_lookup_hold(
        slots, pins, 4, _class_lookup_in_mro(cls, cstr("__getattr__")), 1,
    )
    if py_err_occurred() != 0:
        return null()
    if ptr_is_null(getattr_method) != 0:
        if (ds_flags & 4) == 0 and idx < 0:
            # No class attribute, field or method answered and the instance
            # dict (checked above) did not either.
            _attr_res_store(cls, name, _ATTR_RES_ABSENT, 0, outcome_epoch)
        return null()
    key = _instance_lookup_hold(slots, pins, 5, py_str_new(name, strlen(name)), 0)
    if ptr_is_null(key) != 0:
        return null()
    # The method-table slot for a compiled __getattr__ holds a PY_TYPE_FUNC
    # object, not a raw code pointer; invoke it via py_obj_call in that case.
    # call_ptr2 alone treats the object as a code address and crashes. Mirrors
    # class_call_binary_method in py_class.c.
    if _ptr_can_have_header(getattr_method):
        if load_i32(getattr_method, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_FUNC:
            gargs = _instance_lookup_hold(slots, pins, 6, py_tuple_new(2), 0)
            if ptr_is_null(gargs) != 0:
                _class_require_result(
                    null(),
                    cstr("py_tuple_new"),
                    cstr("class callback argument tuple allocation failed"),
                )
                if field_fallback_rooted == 0:
                    py_decref(key)
                return null()
            py_tuple_set_item(gargs, 0, inst)
            if py_err_occurred() != 0:
                if field_fallback_rooted == 0:
                    py_decref(gargs)
                    py_decref(key)
                return null()
            py_tuple_set_item(gargs, 1, key)
            if py_err_occurred() != 0:
                if field_fallback_rooted == 0:
                    py_decref(gargs)
                    py_decref(key)
                return null()
            got = py_obj_call(getattr_method, gargs, null())
            _class_require_result(
                got,
                cstr("__getattr__"),
                cstr("class callback returned NULL without setting an exception"),
            )
            if field_fallback_rooted == 0:
                py_decref(gargs)
                py_decref(key)
            return got
    got = call_ptr2(getattr_method, inst, key)
    _class_require_result(
        got,
        cstr("__getattr__"),
        cstr("class callback returned NULL without setting an exception"),
    )
    if field_fallback_rooted == 0:
        py_decref(key)
    return got


@c_abi_export("py_instance_getattr")
def py_instance_getattr(inst, name):
    if not _ptr_is_instance(inst):
        return null()
    if ptr_is_null(name) != 0:
        return null()
    cls = pcc_gc_load_ptr(inst, ptr_add(inst, PYINSTANCEOBJECT_CLS_OFFSET))
    if ptr_is_null(cls) != 0:
        return null()
    if _no_getattribute_known(cls) != 0:
        return _instance_getattr_default(inst, cls, name)
    getattribute_name = cstr("__getattribute__")
    probe_epoch: int = _class_attr_cache_epoch()
    getattribute_method = _class_lookup_in_mro(cls, getattribute_name)
    if ptr_is_null(getattribute_method) != 0:
        _no_getattribute_store(cls, probe_epoch)
    if ptr_is_null(getattribute_method) == 0:
        # Method-table entries can be native code pointers or PY_TYPE_FUNC
        # objects. The shared callback path distinguishes their ABIs and
        # protects the receiver, callback, key, result and exception owners.
        return _instance_lookup_rooted(inst, -1, name, 2)
    return _instance_getattr_default(inst, cls, name)


@c_abi_export("py_instance_setattr")
def py_instance_setattr(inst, name, value) -> int:
    if not _ptr_is_instance(inst):
        return -1
    if ptr_is_null(name) != 0:
        return -1
    cls = pcc_gc_load_ptr(inst, ptr_add(inst, PYINSTANCEOBJECT_CLS_OFFSET))
    # A FIELD outcome proves no data descriptor shadows the field, so the
    # store goes straight to its slot, as the full walk below would decide.
    entry = _attr_res_find_any(cls, name)
    if ptr_is_null(entry) == 0 and load_i32(entry, 12) == _ATTR_RES_FIELD:
        pcc_gc_store_ptr(
            inst,
            ptr_add(
                inst,
                PYINSTANCEOBJECT_FIELDS_OFFSET + load_i64(entry, 16) * C_POINTER_SIZE,
            ),
            value,
        )
        return 0
    outcome_epoch: int = _class_attr_cache_epoch()
    class_attr = _class_attr_lookup_in_mro(cls, name)
    if ptr_is_null(class_attr) == 0:
        # A property is a data descriptor whether or not it has a setter;
        # _descriptor_method only sees instance descriptors, so a property
        # store used to land in the instance dict, unseen by its getter.
        if _class_attr_is_property(class_attr) or ptr_is_null(
            _descriptor_method(class_attr, cstr("__set__"))
        ) == 0:
            rc: int = _descriptor_call_set(class_attr, inst, value)
            py_decref(class_attr)
            return rc
        py_decref(class_attr)
    idx: int = _lookup_field_index(cls, name)
    if idx >= 0:
        n_fields: int = load_i32(cls, PYCLASSOBJECT_N_FIELDS_OFFSET)
        if idx >= n_fields:
            return -1
        if ptr_is_null(class_attr) != 0:
            # No class attribute at all, so no data descriptor: the same
            # outcome the read path records for this field.
            _attr_res_store(cls, name, _ATTR_RES_FIELD, idx, outcome_epoch)
        fields_base = ptr_add(inst, PYINSTANCEOBJECT_FIELDS_OFFSET)
        pcc_gc_store_ptr(
            inst,
            ptr_add(fields_base, idx * C_POINTER_SIZE),
            value,
        )
        return 0
    if ptr_is_null(value) != 0:
        return -1
    dyn_slot = _dynamic_attr_slot(inst)
    if ptr_is_null(dyn_slot) != 0:
        return -1
    dyn = pcc_gc_load_ptr(inst, dyn_slot)
    if ptr_is_null(dyn) != 0:
        dyn = py_dict_new()
        if ptr_is_null(dyn) != 0:
            return -1
        pcc_gc_store_ptr(inst, dyn_slot, dyn)
        py_decref(dyn)
    key = py_str_new(name, strlen(name))
    py_dict_set(dyn, key, value)
    py_decref(key)
    return 0


@c_abi_export("py_instance_delattr")
def py_instance_delattr(inst, name) -> int:
    if not _ptr_is_instance(inst):
        return -1
    if ptr_is_null(name) != 0:
        return -1
    cls = pcc_gc_load_ptr(inst, ptr_add(inst, PYINSTANCEOBJECT_CLS_OFFSET))
    class_attr = _class_attr_lookup_in_mro(cls, name)
    if ptr_is_null(class_attr) == 0:
        if _class_attr_is_property(class_attr) or ptr_is_null(
            _descriptor_method(class_attr, cstr("__delete__"))
        ) == 0:
            rc: int = _descriptor_call_delete(class_attr, inst)
            py_decref(class_attr)
            return rc
        py_decref(class_attr)
    idx: int = _lookup_field_index(cls, name)
    if idx >= 0:
        n_fields: int = load_i32(cls, PYCLASSOBJECT_N_FIELDS_OFFSET)
        if idx >= n_fields:
            return -1
        fields_base = ptr_add(inst, PYINSTANCEOBJECT_FIELDS_OFFSET)
        old = pcc_gc_load_ptr(
            inst,
            ptr_add(fields_base, idx * C_POINTER_SIZE),
        )
        if ptr_is_null(old) != 0:
            return -1
        store_ptr(fields_base, idx * C_POINTER_SIZE, null())
        py_decref(old)
        return 0
    dyn_slot = _dynamic_attr_slot(inst)
    if ptr_is_null(dyn_slot) != 0:
        return -1
    dyn = pcc_gc_load_ptr(inst, dyn_slot)
    if ptr_is_null(dyn) != 0:
        return -1
    key = py_str_new(name, strlen(name))
    rc: int = py_dict_del(dyn, key)
    py_decref(key)
    return rc


# Namespace application reuses the special-call frame and exception protocol.
# The source storage and its eager item snapshot are independent owners.
_CLASS_APPLY_CLASS = 1
_CLASS_APPLY_NAMESPACE = 2
_CLASS_APPLY_STORAGE = 3
_CLASS_APPLY_ITEMS = 4
_CLASS_APPLY_PAIR = 5
_CLASS_APPLY_KEY = 6
_CLASS_APPLY_VALUE = 7
_CLASS_APPLY_SLOT_COUNT = 14

define_global_i32("pcc_class_apply_borrowed_map", -2)


def _class_apply_adopt(slots: c_ptr, tokens: c_ptr, index: int) -> int:
    if _special_adopt(slots, tokens, index) != 0:
        return -1
    if ptr_is_null(load_ptr(slots, index * C_POINTER_SIZE)) != 0 or py_err_occurred() != 0:
        return _special_error(cstr("class namespace snapshot item acquisition failed"))
    return 0


def _class_apply_namespace_body(slots: c_ptr, tokens: c_ptr) -> int:
    if not _ptr_is_class(load_ptr(slots, _CLASS_APPLY_CLASS * C_POINTER_SIZE)):
        return _special_error(cstr("namespace destination must be a class"))
    if py_dict_storage_slots(ptr_add(slots, _CLASS_APPLY_NAMESPACE * C_POINTER_SIZE),
            ptr_add(slots, _CLASS_APPLY_STORAGE * C_POINTER_SIZE)) != 0:
        return -1
    if _class_apply_adopt(slots, tokens, _CLASS_APPLY_STORAGE) != 0:
        return -1
    store_ptr(slots, _CLASS_APPLY_ITEMS * C_POINTER_SIZE,
              py_dict_items(load_ptr(slots, _CLASS_APPLY_STORAGE * C_POINTER_SIZE)))
    if _class_apply_adopt(slots, tokens, _CLASS_APPLY_ITEMS) != 0:
        return -1
    count: int = py_list_len(load_ptr(slots, _CLASS_APPLY_ITEMS * C_POINTER_SIZE))
    index: int = 0
    while index < count:
        store_ptr(slots, _CLASS_APPLY_PAIR * C_POINTER_SIZE,
                  py_list_get(load_ptr(slots, _CLASS_APPLY_ITEMS * C_POINTER_SIZE), index))
        if _class_apply_adopt(slots, tokens, _CLASS_APPLY_PAIR) != 0:
            return -1
        store_ptr(slots, _CLASS_APPLY_KEY * C_POINTER_SIZE,
                  py_tuple_get(load_ptr(slots, _CLASS_APPLY_PAIR * C_POINTER_SIZE), 0))
        if _class_apply_adopt(slots, tokens, _CLASS_APPLY_KEY) != 0:
            return -1
        store_ptr(slots, _CLASS_APPLY_VALUE * C_POINTER_SIZE,
                  py_tuple_get(load_ptr(slots, _CLASS_APPLY_PAIR * C_POINTER_SIZE), 1))
        if _class_apply_adopt(slots, tokens, _CLASS_APPLY_VALUE) != 0:
            return -1
        # Retain the actual key, including non-string dictionary keys. Its
        # separate lease protects equality/hash callbacks and name bytes.
        if py_class_write_namespace_key_slots(ptr_add(slots, _CLASS_APPLY_CLASS * C_POINTER_SIZE),
                ptr_add(slots, _CLASS_APPLY_KEY * C_POINTER_SIZE),
                ptr_add(slots, _CLASS_APPLY_VALUE * C_POINTER_SIZE)) != 0:
            return -1
        _special_drop(slots, tokens, _CLASS_APPLY_VALUE)
        _special_drop(slots, tokens, _CLASS_APPLY_KEY)
        _special_drop(slots, tokens, _CLASS_APPLY_PAIR)
        index = index + 1
    return 0


def _class_apply_namespace_bound(class_slot: c_ptr, namespace_slot: c_ptr, borrowed: int) -> int:
    slots = stack_alloc(_CLASS_APPLY_SLOT_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_CLASS_APPLY_SLOT_COUNT * C_POINTER_SIZE)
    handles = stack_alloc(_CLASS_APPLY_SLOT_COUNT * C_POINTER_SIZE)
    count: int = _special_open(slots, tokens, handles)
    status: int = -1
    suspended: int = 0
    if count == _CLASS_APPLY_SLOT_COUNT:
        py_tls_exc_swap_slot(slots)
        suspended = 1
        status = _special_copy(slots, tokens, _CLASS_APPLY_CLASS, class_slot, borrowed)
        if status == 0:
            status = _special_copy(slots, tokens, _CLASS_APPLY_NAMESPACE, namespace_slot, borrowed)
        if status == 0:
            status = _class_apply_namespace_body(slots, tokens)
    if status != 0:
        _special_error(cstr("class namespace application failed without an exception"))
    _special_close(slots, tokens, handles, count, suspended)
    return status


@c_abi_export("py_class_apply_namespace_dict")
def py_class_apply_namespace_dict(cls, ns) -> int:
    # Compatibility raw ABI: callers keep their borrowed arguments stable
    # through entry. The frame becomes authoritative before scratch opens.
    borrowed = stack_alloc(2 * C_POINTER_SIZE)
    store_ptr(borrowed, 0, cls)
    store_ptr(borrowed, C_POINTER_SIZE, ns)
    pcc_gc_frame_enter(global_addr("pcc_class_apply_borrowed_map"), borrowed)
    status: int = _class_apply_namespace_bound(borrowed, ptr_add(borrowed, C_POINTER_SIZE), 1)
    pcc_gc_frame_leave(borrowed)
    return status


@c_abi_export("py_isinstance")
def py_isinstance(obj, cls) -> int:
    obj = pcc_gc_note_relocation_read(obj)
    cls = pcc_gc_note_relocation_read(cls)
    # One provenance probe, for `obj`.  `cls` is only compared by address with
    # the validated class of `obj` and its MRO entries, all real classes, so
    # an argument that is not a class never matches -- the answer the former
    # `_ptr_is_class(cls)` probe gave up front.  `_ptr_is_instance` probed
    # `obj` a second time.
    if not _ptr_can_have_header(obj):
        return 0
    tag: int = load_i32(obj, PYOBJECTHEADER_TYPE_TAG_OFFSET)
    if tag == PY_TYPE_EXC:
        # Exception instances match through their exception-class MRO.
        if py_exc_matches(obj, cls) != 0:
            return 1
        return 0
    if tag != PY_TYPE_INSTANCE and tag < PY_TYPE_USER_CLASS_START:
        return 0
    obj_cls = pcc_gc_load_ptr(obj, ptr_add(obj, PYINSTANCEOBJECT_CLS_OFFSET))
    if ptr_is_null(obj_cls) != 0:
        return 0
    if not _ptr_is_class_of_validated_instance(obj_cls):
        return 0
    if ptr_eq(obj_cls, cls) != 0:
        return 1
    n_mro: int = load_i32(obj_cls, PYCLASSOBJECT_N_MRO_OFFSET)
    mro = load_ptr(obj_cls, PYCLASSOBJECT_MRO_OFFSET)
    i: int = 0
    while i < n_mro:
        m = pcc_gc_load_ptr(obj_cls, ptr_add(mro, i * C_POINTER_SIZE))
        if ptr_eq(m, cls) != 0:
            return 1
        i = i + 1
    return 0


def _super_append(buf, at: int, text) -> int:
    """Copy the NUL-terminated ``text`` into ``buf`` at ``at``; return the end."""
    pos: int = at
    index: int = 0
    ch: int = load_i8(text, 0) & 0xFF
    while ch != 0:
        store_i8(buf, pos, ch)
        pos = pos + 1
        index = index + 1
        ch = load_i8(text, index) & 0xFF
    return pos


def _super_raise_message(tag: int, parts, count: int):
    """Raise exception ``tag`` with the ``count`` cstr ``parts`` joined."""
    total: int = 1
    index: int = 0
    while index < count:
        total = total + strlen(load_ptr(parts, index * C_POINTER_SIZE))
        index = index + 1
    buf = malloc(total)
    if ptr_is_null(buf) != 0:
        py_raise_owned(py_exc_new(tag, load_ptr(parts, 0)))
        return null()
    pos: int = 0
    index = 0
    while index < count:
        pos = _super_append(buf, pos, load_ptr(parts, index * C_POINTER_SIZE))
        index = index + 1
    store_i8(buf, pos, 0)
    py_raise_owned(py_exc_new(tag, buf))
    free(buf)
    return null()


def _super_class_name(cls):
    name = load_ptr(cls, PYCLASSOBJECT_NAME_OFFSET)
    if ptr_is_null(name) != 0:
        return cstr("?")
    return name


def _super_raise_not_type(from_cls):
    """CPython: ``super() argument 1 must be a type, not X``."""
    parts = stack_alloc(2 * C_POINTER_SIZE)
    store_ptr(parts, 0, cstr("super() argument 1 must be a type, not "))
    store_ptr(parts, C_POINTER_SIZE, py_obj_type_name(from_cls))
    return _super_raise_message(3, parts, 2)


def _super_raise_not_subtype(receiver, receiver_cls, from_cls):
    """CPython: ``super(type, obj): obj (instance of X) is not an instance or
    subtype of type (Y).``, with ``type X`` when ``obj`` is a class."""
    parts = stack_alloc(5 * C_POINTER_SIZE)
    store_ptr(parts, 0, cstr("super(type, obj): obj ("))
    if ptr_is_null(receiver) == 0 and is_tagged_int(receiver) == 0 and _ptr_is_class(receiver):
        store_ptr(parts, C_POINTER_SIZE, cstr("type "))
        store_ptr(parts, 2 * C_POINTER_SIZE, _super_class_name(receiver))
    elif ptr_is_null(receiver_cls) == 0 and _ptr_is_class(receiver_cls):
        store_ptr(parts, C_POINTER_SIZE, cstr("instance of "))
        store_ptr(parts, 2 * C_POINTER_SIZE, _super_class_name(receiver_cls))
    else:
        store_ptr(parts, C_POINTER_SIZE, cstr("instance of "))
        store_ptr(parts, 2 * C_POINTER_SIZE, py_obj_type_name(receiver))
    store_ptr(parts, 3 * C_POINTER_SIZE, cstr(") is not an instance or subtype of type ("))
    store_ptr(parts, 4 * C_POINTER_SIZE, _super_class_name(from_cls))
    return _super_raise_message(3, parts, 5)


def _super_raise_missing(name):
    """CPython: ``'super' object has no attribute 'name'``."""
    parts = stack_alloc(3 * C_POINTER_SIZE)
    store_ptr(parts, 0, cstr("'super' object has no attribute '"))
    store_ptr(parts, C_POINTER_SIZE, name)
    store_ptr(parts, 2 * C_POINTER_SIZE, cstr("'"))
    return _super_raise_message(6, parts, 3)


# ---- object's namespace ----------------------------------------------------
# CPython's lookups reach ``object.__dict__`` at the end of every MRO. The
# root class keeps only the cached ``__new__`` in its attrs dict: internal
# dunder detection walks attrs dicts and method tables and must not find
# object's members. This namespace is read only where CPython's public lookup
# reaches object: the super() walk at the root, the instance getattr miss
# before ``__getattr__``, and the class getattr miss.


def _object_namespace_mutex() -> c_ptr:
    slot = global_addr("pcc_object_namespace_mutex_bits")
    bits: int = atomic_load_i64(slot, 0, "acquire")
    if bits != 0:
        return int_to_ptr(bits)
    candidate = pcc_mutex_new()
    if ptr_is_null(candidate) != 0:
        return null()
    installed: int = atomic_cas_i64(
        slot, 0, 0, ptr_to_int(candidate), "acq_rel", "acquire"
    )
    if installed != 0:
        pcc_mutex_free(candidate)
        return int_to_ptr(installed)
    return candidate


def _object_decimal_append(buf, pos: int, value: int) -> int:
    digits = stack_alloc(24)
    count: int = 0
    while count == 0 or value > 0:
        quotient: int = unsigned_div_i64(value, 10)
        store_i8(digits, 23 - count, 48 + value - quotient * 10)
        value = quotient
        count = count + 1
    while count > 0:
        store_i8(buf, pos, load_i8(digits, 24 - count))
        pos = pos + 1
        count = count - 1
    return pos


def _object_type_name(receiver):
    """``type(receiver).__name__`` for an instance or class receiver."""
    if _ptr_is_class(receiver):
        return cstr("type")
    if _ptr_is_instance(receiver):
        cls = pcc_gc_load_ptr(receiver, ptr_add(receiver, PYINSTANCEOBJECT_CLS_OFFSET))
        if ptr_is_null(cls) == 0:
            name = load_ptr(cls, PYCLASSOBJECT_NAME_OFFSET)
            if ptr_is_null(name) == 0:
                return name
    return cstr("object")


def _object_owner_name(receiver, kind: int):
    return _member_owner_name(cstr("object"), receiver, kind)


def _object_member_arity(receiver, name, kind: int, expected: int, given: int,
                         keywords: int) -> int:
    return _member_arity(cstr("object"), receiver, name, kind, expected, given, keywords)


def _object_call_args(args, slots) -> None:
    """slots[0..2] = self, the extra positional tuple, the keyword dict (owned)."""
    store_ptr(slots, 0, py_tuple_get(args, 0))
    store_ptr(slots, C_POINTER_SIZE, py_tuple_get(args, 1))
    store_ptr(slots, 2 * C_POINTER_SIZE, py_tuple_get(args, 2))


def _object_call_release(slots) -> None:
    index: int = 0
    while index < 3:
        value = load_ptr(slots, index * C_POINTER_SIZE)
        if ptr_is_null(value) == 0:
            py_decref(value)
        index = index + 1


def _object_call_check(slots, name, kind: int, expected: int) -> int:
    return _member_call_check(slots, cstr("object"), name, kind, expected)


def _object_call_arg(slots, index: int):
    """The ``index``-th extra positional argument, a NEW reference."""
    return py_tuple_get(load_ptr(slots, C_POINTER_SIZE), index)


def _object_new_ref(value):
    py_incref(value)
    return value


def _object_receiver_class(receiver):
    if _ptr_is_instance(receiver):
        return pcc_gc_load_ptr(receiver, ptr_add(receiver, PYINSTANCEOBJECT_CLS_OFFSET))
    return null()


def _object_overrides(cls, name) -> int:
    """Whether a class before the root defines ``name`` (user or builtin)."""
    if ptr_is_null(cls) != 0:
        return 0
    found = _class_attr_lookup_in_mro(cls, name)
    if ptr_is_null(found) == 0:
        root = global_load_ptr("py_object_root_cache")
        inherited = null()
        if _strs_eq(name, cstr("__new__")) != 0 and ptr_is_null(root) == 0:
            inherited = _object_new_cached(root)
        same: int = ptr_eq(found, inherited)
        py_decref(found)
        if ptr_is_null(inherited) == 0:
            py_decref(inherited)
        return 1 if same == 0 else 0
    return 1 if ptr_is_null(_class_lookup_in_mro(cls, name)) == 0 else 0


def _object_init_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    # CPython object_init: excess arguments are an error unless the type
    # overrides __new__ without overriding __init__.
    slots = stack_alloc(3 * C_POINTER_SIZE)
    _object_call_args(args, slots)
    extra = load_ptr(slots, C_POINTER_SIZE)
    excess: int = 0
    if ptr_is_null(extra) == 0 and py_tuple_len(extra) != 0:
        excess = 1
    kwargs = load_ptr(slots, 2 * C_POINTER_SIZE)
    if ptr_is_null(kwargs) == 0 and ptr_eq(kwargs, global_load_ptr("py_None")) == 0:
        if py_dict_len(kwargs) != 0:
            excess = 1
    result = null()
    if excess == 0:
        result = _object_new_ref(global_load_ptr("py_None"))
    else:
        cls = _object_receiver_class(load_ptr(slots, 0))
        if _object_overrides(cls, cstr("__init__")) != 0:
            py_raise_owned(py_exc_new(3, cstr(
                "object.__init__() takes exactly one argument (the instance to initialize)")))
        elif _object_overrides(cls, cstr("__new__")) == 0:
            parts = stack_alloc(2 * C_POINTER_SIZE)
            store_ptr(parts, 0, _object_type_name(load_ptr(slots, 0)))
            store_ptr(parts, C_POINTER_SIZE, cstr(
                ".__init__() takes exactly one argument (the instance to initialize)"))
            _super_raise_message(3, parts, 2)
        else:
            result = _object_new_ref(global_load_ptr("py_None"))
    _object_call_release(slots)
    return result


def _object_rich_entry(args, name):
    # object_richcompare: __eq__ is identity or NotImplemented, the orderings
    # always NotImplemented.
    slots = stack_alloc(3 * C_POINTER_SIZE)
    _object_call_args(args, slots)
    result = null()
    if _object_call_check(slots, name, 0, 1) == 0:
        result = _object_new_ref(global_load_ptr("py_NotImplemented"))
        if _strs_eq(name, cstr("__eq__")) != 0:
            other = _object_call_arg(slots, 0)
            if ptr_eq(other, load_ptr(slots, 0)) != 0:
                py_decref(result)
                result = py_bool_from_bit(1)
            py_decref(other)
    _object_call_release(slots)
    return result


def _object_eq_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    return _object_rich_entry(args, cstr("__eq__"))


def _object_lt_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    return _object_rich_entry(args, cstr("__lt__"))


def _object_le_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    return _object_rich_entry(args, cstr("__le__"))


def _object_gt_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    return _object_rich_entry(args, cstr("__gt__"))


def _object_ge_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    return _object_rich_entry(args, cstr("__ge__"))


def _object_ne_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    # object_richcompare(Py_NE): ask type(self).__eq__ and invert its answer
    # unless it is NotImplemented.
    slots = stack_alloc(3 * C_POINTER_SIZE)
    _object_call_args(args, slots)
    result = null()
    if _object_call_check(slots, cstr("__ne__"), 0, 1) == 0:
        receiver = load_ptr(slots, 0)
        other = _object_call_arg(slots, 0)
        pcc_gc_pin(other)
        equal = null()
        cls = _object_receiver_class(receiver)
        if ptr_is_null(cls) == 0:
            method = py_class_getattr(cls, cstr("__eq__"))
            if ptr_is_null(method) == 0:
                pcc_gc_pin(method)
                pair = py_tuple_new(2)
                if ptr_is_null(pair) == 0:
                    py_tuple_set_item(pair, 0, receiver)
                    py_tuple_set_item(pair, 1, other)
                    equal = py_obj_call(method, pair, null())
                    py_decref(pair)
                pcc_gc_unpin(method)
                py_decref(method)
        elif ptr_eq(receiver, other) != 0:
            equal = py_bool_from_bit(1)
        else:
            equal = _object_new_ref(global_load_ptr("py_NotImplemented"))
        if ptr_is_null(equal) == 0:
            if ptr_eq(equal, global_load_ptr("py_NotImplemented")) != 0:
                result = equal
            else:
                truth: int = py_obj_truthy(equal)
                py_decref(equal)
                if py_err_occurred() == 0:
                    result = py_bool_from_bit(1 if truth == 0 else 0)
        pcc_gc_unpin(other)
        py_decref(other)
    _object_call_release(slots)
    return result


def _object_repr_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    slots = stack_alloc(3 * C_POINTER_SIZE)
    _object_call_args(args, slots)
    result = null()
    if _object_call_check(slots, cstr("__repr__"), 0, 0) == 0:
        result = py_obj_default_repr(load_ptr(slots, 0))
    _object_call_release(slots)
    return result


def _object_str_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    # object_str: repr(self), so a user __repr__ answers.
    slots = stack_alloc(3 * C_POINTER_SIZE)
    _object_call_args(args, slots)
    result = null()
    if _object_call_check(slots, cstr("__str__"), 0, 0) == 0:
        result = py_obj_repr(load_ptr(slots, 0))
    _object_call_release(slots)
    return result


def _object_hash_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    slots = stack_alloc(3 * C_POINTER_SIZE)
    _object_call_args(args, slots)
    result = null()
    if _object_call_check(slots, cstr("__hash__"), 0, 0) == 0:
        result = py_int_from_i64(py_obj_identity_hash(load_ptr(slots, 0)))
    _object_call_release(slots)
    return result


def _object_type_error(head, middle, tail):
    parts = stack_alloc(3 * C_POINTER_SIZE)
    store_ptr(parts, 0, head)
    store_ptr(parts, C_POINTER_SIZE, middle)
    store_ptr(parts, 2 * C_POINTER_SIZE, tail)
    return _super_raise_message(3, parts, 3)


def _object_is_str(value) -> int:
    if ptr_is_null(value) != 0 or is_tagged_int(value) != 0:
        return 0
    if _ptr_can_have_header(value) and load_i32(value, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_STR:
        return 1
    return 0


def _object_typed_error(head, value, tail):
    """TypeError head + type(value).__name__ + tail."""
    shown = py_obj_type_name(value)
    if ptr_is_null(shown) != 0:
        return null()
    pcc_gc_pin(shown)
    _object_type_error(head, py_str_utf8(shown), tail)
    pcc_gc_unpin(shown)
    py_decref(shown)
    return null()


def _object_name_text(slots, value):
    """The C text of a str attribute name, or NULL after CPython's TypeError."""
    if _object_is_str(value) != 0:
        return py_str_utf8(value)
    _object_typed_error(cstr("attribute name must be string, not '"), value, cstr("'"))
    return null()


def _object_format_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    # object___format__: a str spec; non-empty is a TypeError, else str(self).
    slots = stack_alloc(3 * C_POINTER_SIZE)
    _object_call_args(args, slots)
    result = null()
    if _object_call_check(slots, cstr("__format__"), 1, 1) == 0:
        spec = _object_call_arg(slots, 0)
        if _object_is_str(spec) == 0:
            _object_typed_error(cstr("__format__() argument must be str, not "), spec, cstr(""))
        elif py_str_byte_len(spec) != 0:
            _object_type_error(cstr("unsupported format string passed to "),
                               _object_type_name(load_ptr(slots, 0)), cstr(".__format__"))
        else:
            result = py_obj_str(load_ptr(slots, 0))
        if ptr_is_null(spec) == 0:
            py_decref(spec)
    _object_call_release(slots)
    return result


def _object_getattribute_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    slots = stack_alloc(3 * C_POINTER_SIZE)
    _object_call_args(args, slots)
    result = null()
    if _object_call_check(slots, cstr("__getattribute__"), 0, 1) == 0:
        name_obj = _object_call_arg(slots, 0)
        text = _object_name_text(slots, name_obj)
        if ptr_is_null(text) == 0:
            result = py_obj_getattr_default(load_ptr(slots, 0), text)
        if ptr_is_null(name_obj) == 0:
            py_decref(name_obj)
    _object_call_release(slots)
    return result


def _object_missing_attribute(receiver, text, tail):
    parts = stack_alloc(5 * C_POINTER_SIZE)
    store_ptr(parts, 0, cstr("'"))
    store_ptr(parts, C_POINTER_SIZE, _object_type_name(receiver))
    store_ptr(parts, 2 * C_POINTER_SIZE, cstr("' object has no attribute '"))
    store_ptr(parts, 3 * C_POINTER_SIZE, text)
    store_ptr(parts, 4 * C_POINTER_SIZE, tail)
    return _super_raise_message(6, parts, 5)


def _object_setattr_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    # PyObject_GenericSetAttr: no user __setattr__ is consulted.
    slots = stack_alloc(3 * C_POINTER_SIZE)
    _object_call_args(args, slots)
    result = null()
    if _object_call_check(slots, cstr("__setattr__"), 0, 2) == 0:
        name_obj = _object_call_arg(slots, 0)
        value = _object_call_arg(slots, 1)
        text = _object_name_text(slots, name_obj)
        if ptr_is_null(text) == 0:
            receiver = load_ptr(slots, 0)
            if _ptr_is_instance(receiver) and py_instance_setattr(receiver, text, value) == 0:
                result = _object_new_ref(global_load_ptr("py_None"))
            elif py_err_occurred() == 0:
                _object_missing_attribute(receiver, text,
                                          cstr("' and no __dict__ for setting new attributes"))
        if ptr_is_null(value) == 0:
            py_decref(value)
        if ptr_is_null(name_obj) == 0:
            py_decref(name_obj)
    _object_call_release(slots)
    return result


def _object_delattr_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    slots = stack_alloc(3 * C_POINTER_SIZE)
    _object_call_args(args, slots)
    result = null()
    if _object_call_check(slots, cstr("__delattr__"), 0, 1) == 0:
        name_obj = _object_call_arg(slots, 0)
        text = _object_name_text(slots, name_obj)
        if ptr_is_null(text) == 0:
            receiver = load_ptr(slots, 0)
            if _ptr_is_instance(receiver) and py_instance_delattr(receiver, text) == 0:
                result = _object_new_ref(global_load_ptr("py_None"))
            elif py_err_occurred() == 0:
                _object_missing_attribute(receiver, text, cstr("'"))
        if ptr_is_null(name_obj) == 0:
            py_decref(name_obj)
    _object_call_release(slots)
    return result


def _object_sizeof_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    # pcc's own instance layout size, as each implementation reports its own.
    slots = stack_alloc(3 * C_POINTER_SIZE)
    _object_call_args(args, slots)
    result = null()
    if _object_call_check(slots, cstr("__sizeof__"), 1, 0) == 0:
        size: int = PYINSTANCEOBJECT_SIZE
        cls = _object_receiver_class(load_ptr(slots, 0))
        if ptr_is_null(cls) == 0:
            size = load_i32(cls, PYCLASSOBJECT_INSTANCE_SIZE_OFFSET)
        result = py_int_from_i64(size)
    _object_call_release(slots)
    return result


def _object_getstate_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    # object_getstate_default: None for an empty instance dict, else the dict.
    slots = stack_alloc(3 * C_POINTER_SIZE)
    _object_call_args(args, slots)
    result = null()
    if _object_call_check(slots, cstr("__getstate__"), 1, 0) == 0:
        receiver = load_ptr(slots, 0)
        cls = _object_receiver_class(receiver)
        if ptr_is_null(cls) == 0 and (load_i32(cls, PYOBJECTHEADER_FLAGS_OFFSET) & 2) != 0:
            py_raise_owned(py_exc_new(11, cstr(
                "object.__getstate__() of a __slots__ instance is not implemented natively")))
        else:
            state = py_obj_vars(receiver)
            if ptr_is_null(state) == 0 and py_dict_len(state) == 0:
                py_decref(state)
                result = _object_new_ref(global_load_ptr("py_None"))
            else:
                result = state
    _object_call_release(slots)
    return result


def _object_gap_entry(args, name, why):
    # A runtime capability gap, reported explicitly as NotImplementedError.
    parts = stack_alloc(3 * C_POINTER_SIZE)
    store_ptr(parts, 0, name)
    store_ptr(parts, C_POINTER_SIZE, cstr(" is not implemented natively: "))
    store_ptr(parts, 2 * C_POINTER_SIZE, why)
    return _super_raise_message(11, parts, 3)


def _object_reduce_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    return _object_gap_entry(args, cstr("object.__reduce__()"), cstr("it needs copyreg"))


def _object_reduce_ex_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    return _object_gap_entry(args, cstr("object.__reduce_ex__()"), cstr("it needs copyreg"))


def _object_dir_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    return _object_gap_entry(args, cstr("object.__dir__()"),
                             cstr("default dir needs complete class and builtin namespaces"))


def _object_init_subclass_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    # A classmethod taking no arguments.
    slots = stack_alloc(3 * C_POINTER_SIZE)
    _object_call_args(args, slots)
    result = null()
    if _object_call_check(slots, cstr("__init_subclass__"), 2, 0) == 0:
        result = _object_new_ref(global_load_ptr("py_None"))
    _object_call_release(slots)
    return result


def _object_subclasshook_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    slots = stack_alloc(3 * C_POINTER_SIZE)
    _object_call_args(args, slots)
    result = null()
    if _object_call_check(slots, cstr("__subclasshook__"), 2, 1) == 0:
        result = _object_new_ref(global_load_ptr("py_NotImplemented"))
    _object_call_release(slots)
    return result


def _object_namespace_add(namespace, name, value, wrap: int) -> int:
    """Store NEW ``value`` (consumed) under ``name``; ``wrap`` 1 makes it a classmethod."""
    if ptr_is_null(value) != 0:
        return -1
    pcc_gc_pin(value)
    stored = value
    if wrap != 0:
        stored = py_classmethod_new(value)
        pcc_gc_unpin(value)
        py_decref(value)
        if ptr_is_null(stored) != 0:
            return -1
        pcc_gc_pin(stored)
    status: int = -1
    key = py_str_new(name, strlen(name))
    if ptr_is_null(key) == 0:
        pcc_gc_pin(key)
        py_dict_set(namespace, key, stored)
        if py_err_occurred() == 0:
            status = 0
        pcc_gc_unpin(key)
        py_decref(key)
    pcc_gc_unpin(stored)
    py_decref(stored)
    return status


def _object_namespace_build():
    """A NEW dict of object's members (CPython 3.15 ``vars(object)``)."""
    namespace = py_dict_new()
    if ptr_is_null(namespace) != 0:
        return null()
    pcc_gc_pin(namespace)
    # Each entry appears directly in py_func_new_bound so the port compiler
    # passes its code address (a function value through a helper would be
    # boxed as an object).
    self_captures = _object_member_captures(cstr("self"))
    cls_captures = _object_member_captures(cstr("cls"))
    status: int = 0
    if ptr_is_null(self_captures) != 0 or ptr_is_null(cls_captures) != 0:
        status = -1
    else:
        pcc_gc_pin(self_captures)
        pcc_gc_pin(cls_captures)
        status = status | _object_namespace_add(namespace, cstr("__init__"), py_func_new_bound(_object_init_entry, self_captures, cstr("__init__"), null()), 0)
        status = status | _object_namespace_add(namespace, cstr("__repr__"), py_func_new_bound(_object_repr_entry, self_captures, cstr("__repr__"), null()), 0)
        status = status | _object_namespace_add(namespace, cstr("__str__"), py_func_new_bound(_object_str_entry, self_captures, cstr("__str__"), null()), 0)
        status = status | _object_namespace_add(namespace, cstr("__hash__"), py_func_new_bound(_object_hash_entry, self_captures, cstr("__hash__"), null()), 0)
        status = status | _object_namespace_add(namespace, cstr("__eq__"), py_func_new_bound(_object_eq_entry, self_captures, cstr("__eq__"), null()), 0)
        status = status | _object_namespace_add(namespace, cstr("__ne__"), py_func_new_bound(_object_ne_entry, self_captures, cstr("__ne__"), null()), 0)
        status = status | _object_namespace_add(namespace, cstr("__lt__"), py_func_new_bound(_object_lt_entry, self_captures, cstr("__lt__"), null()), 0)
        status = status | _object_namespace_add(namespace, cstr("__le__"), py_func_new_bound(_object_le_entry, self_captures, cstr("__le__"), null()), 0)
        status = status | _object_namespace_add(namespace, cstr("__gt__"), py_func_new_bound(_object_gt_entry, self_captures, cstr("__gt__"), null()), 0)
        status = status | _object_namespace_add(namespace, cstr("__ge__"), py_func_new_bound(_object_ge_entry, self_captures, cstr("__ge__"), null()), 0)
        status = status | _object_namespace_add(namespace, cstr("__format__"), py_func_new_bound(_object_format_entry, self_captures, cstr("__format__"), null()), 0)
        status = status | _object_namespace_add(namespace, cstr("__getattribute__"), py_func_new_bound(_object_getattribute_entry, self_captures, cstr("__getattribute__"), null()), 0)
        status = status | _object_namespace_add(namespace, cstr("__setattr__"), py_func_new_bound(_object_setattr_entry, self_captures, cstr("__setattr__"), null()), 0)
        status = status | _object_namespace_add(namespace, cstr("__delattr__"), py_func_new_bound(_object_delattr_entry, self_captures, cstr("__delattr__"), null()), 0)
        status = status | _object_namespace_add(namespace, cstr("__sizeof__"), py_func_new_bound(_object_sizeof_entry, self_captures, cstr("__sizeof__"), null()), 0)
        status = status | _object_namespace_add(namespace, cstr("__getstate__"), py_func_new_bound(_object_getstate_entry, self_captures, cstr("__getstate__"), null()), 0)
        status = status | _object_namespace_add(namespace, cstr("__reduce__"), py_func_new_bound(_object_reduce_entry, self_captures, cstr("__reduce__"), null()), 0)
        status = status | _object_namespace_add(namespace, cstr("__reduce_ex__"), py_func_new_bound(_object_reduce_ex_entry, self_captures, cstr("__reduce_ex__"), null()), 0)
        status = status | _object_namespace_add(namespace, cstr("__dir__"), py_func_new_bound(_object_dir_entry, self_captures, cstr("__dir__"), null()), 0)
        status = status | _object_namespace_add(namespace, cstr("__init_subclass__"), py_func_new_bound(_object_init_subclass_entry, cls_captures, cstr("__init_subclass__"), null()), 1)
        status = status | _object_namespace_add(namespace, cstr("__subclasshook__"), py_func_new_bound(_object_subclasshook_entry, cls_captures, cstr("__subclasshook__"), null()), 1)
        pcc_gc_unpin(cls_captures)
        pcc_gc_unpin(self_captures)
    if ptr_is_null(self_captures) == 0:
        py_decref(self_captures)
    if ptr_is_null(cls_captures) == 0:
        py_decref(cls_captures)
    root_class = _object_root()
    if ptr_is_null(root_class) == 0:
        status = status | _object_namespace_add(namespace, cstr("__new__"), _object_new_cached(root_class), 0)
    doc = cstr("The base class of the class hierarchy.\n\nWhen called, it accepts no arguments and returns a new featureless\ninstance that has no instance attributes and cannot be given any.\n")
    status = status | _object_namespace_add(namespace, cstr("__doc__"), py_str_new(doc, strlen(doc)), 0)
    pcc_gc_unpin(namespace)
    if status != 0:
        py_decref(namespace)
        return _class_require_result(null(), cstr("object namespace"),
                                     cstr("object namespace construction failed"))
    return namespace


def _object_namespace():
    """object's namespace, borrowed from its scheduler root, or NULL with an exception."""
    published = pcc_gc_load_ptr(null(), global_addr("pcc_object_namespace"))
    if ptr_is_null(published) == 0:
        return published
    mutex = _object_namespace_mutex()
    if ptr_is_null(mutex) != 0 or pcc_mutex_lock(mutex) != 0:
        return _class_require_result(null(), cstr("object namespace"),
                                     cstr("object namespace lock failed"))
    published = pcc_gc_load_ptr(null(), global_addr("pcc_object_namespace"))
    if ptr_is_null(published) == 0:
        pcc_mutex_unlock(mutex)
        return published
    if load_i32(global_addr("pcc_object_namespace_registered"), 0) == 0:
        handle = pcc_gc_scheduler_root_register_handle(global_addr("pcc_object_namespace"))
        if ptr_is_null(handle) != 0:
            pcc_mutex_unlock(mutex)
            return _class_require_result(null(), cstr("object namespace"),
                                         cstr("object namespace root registration failed"))
        store_i32(global_addr("pcc_object_namespace_registered"), 0, 1)
    namespace = _object_namespace_build()
    if ptr_is_null(namespace) != 0:
        pcc_mutex_unlock(mutex)
        return null()
    pcc_gc_pin(namespace)
    pcc_py_gc_minor_graph_lock()
    pcc_gc_store_root(global_addr("pcc_object_namespace"), namespace)
    pcc_py_gc_minor_graph_unlock()
    pcc_gc_unpin(namespace)
    py_decref(namespace)
    pcc_mutex_unlock(mutex)
    return pcc_gc_load_ptr(null(), global_addr("pcc_object_namespace"))


def _object_member_name(name) -> int:
    """Whether ``name`` is a key of object's namespace, without allocating.

    Every member is a dunder, so other names cost two byte loads; the
    attribute-miss paths ask this before any namespace or MRO work.
    """
    if load_i8(name, 0) != 95 or load_i8(name, 1) != 95:
        return 0
    return _names_contain(cstr(' __delattr__ __dir__ __doc__ __eq__ __format__ __ge__ __getattribute__ __getstate__ __gt__ __hash__ __init__ __init_subclass__ __le__ __lt__ __ne__ __new__ __reduce__ __reduce_ex__ __repr__ __setattr__ __sizeof__ __str__ __subclasshook__ '), name)


def _object_namespace_get(name):
    """object's member ``name`` as a NEW reference, or NULL (absent or error)."""
    if _object_member_name(name) == 0:
        return null()
    namespace = _object_namespace()
    if ptr_is_null(namespace) != 0:
        return null()
    owned_key: int = 0
    key = pcc_cached_name_key(name)
    if ptr_is_null(key) != 0:
        key = py_str_new(name, strlen(name))
        if ptr_is_null(key) != 0:
            return null()
        owned_key = 1
    value = py_dict_get(namespace, key)
    if owned_key != 0:
        py_decref(key)
    return value


def _names_contain(table, name) -> int:
    """Whether the space-delimited ``table`` lists ``name``."""
    length: int = strlen(name)
    found: int = 0
    pos: int = 0
    while found == 0 and load_i8(table, pos) != 0:
        if load_i8(table, pos) == 32:
            index: int = 0
            matches: int = 1
            while matches != 0 and index < length:
                if load_i8(table, pos + 1 + index) != load_i8(name, index):
                    matches = 0
                index = index + 1
            if matches != 0 and load_i8(table, pos + 1 + length) == 32:
                found = 1
        pos = pos + 1
    return found


def _builtin_type_names(tag: int):
    """CPython 3.15 vars(T) for the builtin types pcc keeps in MROs."""
    if tag == PY_TYPE_STR:
        return cstr(' __add__ __contains__ __doc__ __eq__ __format__ __ge__ __getitem__ __getnewargs__ __gt__ __hash__ __iter__ __le__ __len__ __lt__ __mod__ __mul__ __ne__ __new__ __repr__ __rmod__ __rmul__ __sizeof__ __str__ capitalize casefold center count encode endswith expandtabs find format format_map index isalnum isalpha isascii isdecimal isdigit isidentifier islower isnumeric isprintable isspace istitle isupper join ljust lower lstrip maketrans partition removeprefix removesuffix replace rfind rindex rjust rpartition rsplit rstrip split splitlines startswith strip swapcase title translate upper zfill ')
    if tag == PY_TYPE_TUPLE:
        return cstr(' __add__ __class_getitem__ __contains__ __doc__ __eq__ __ge__ __getitem__ __getnewargs__ __gt__ __hash__ __iter__ __le__ __len__ __lt__ __mul__ __ne__ __new__ __repr__ __rmul__ count index ')
    if tag == PY_TYPE_DICT:
        return cstr(' __class_getitem__ __contains__ __delitem__ __doc__ __eq__ __ge__ __getitem__ __gt__ __hash__ __init__ __ior__ __iter__ __le__ __len__ __lt__ __ne__ __new__ __or__ __repr__ __reversed__ __ror__ __setitem__ __sizeof__ clear copy fromkeys get items keys pop popitem setdefault update values ')
    return null()


def _builtin_exception_names(class_name):
    """CPython 3.15 vars(E) for a builtin exception class, by name."""
    if _names_contain(cstr(' ArithmeticError AssertionError BufferError BytesWarning DeprecationWarning EOFError EncodingWarning Exception FloatingPointError FutureWarning GeneratorExit ImportWarning IndexError KeyboardInterrupt LookupError MemoryError NotImplementedError OverflowError PendingDeprecationWarning PythonFinalizationError RecursionError ReferenceError ResourceWarning RuntimeError RuntimeWarning StopAsyncIteration SyntaxWarning SystemError TypeError UnicodeError UnicodeWarning UserWarning ValueError Warning ZeroDivisionError '), class_name) != 0:
        return cstr(' __doc__ __new__ ')
    if _names_contain(cstr(' BlockingIOError BrokenPipeError ChildProcessError ConnectionAbortedError ConnectionError ConnectionRefusedError ConnectionResetError FileExistsError FileNotFoundError ImportCycleError IndentationError InterruptedError IsADirectoryError ModuleNotFoundError NotADirectoryError PermissionError ProcessLookupError TabError TimeoutError UnboundLocalError _IncompleteInputError '), class_name) != 0:
        return cstr(' __doc__ ')
    if _names_contain(cstr(' UnicodeDecodeError UnicodeEncodeError UnicodeTranslateError '), class_name) != 0:
        return cstr(' __doc__ __init__ __new__ __str__ encoding end object reason start ')
    if _names_contain(cstr(' AttributeError '), class_name) != 0:
        return cstr(' __doc__ __getstate__ __init__ __reduce__ name obj ')
    if _names_contain(cstr(' BaseException '), class_name) != 0:
        return cstr(' __cause__ __context__ __dict__ __doc__ __init__ __new__ __reduce__ __repr__ __setstate__ __str__ __suppress_context__ __traceback__ add_note args with_traceback ')
    if _names_contain(cstr(' BaseExceptionGroup '), class_name) != 0:
        return cstr(' __class_getitem__ __doc__ __init__ __new__ __repr__ __str__ derive exceptions message split subgroup ')
    if _names_contain(cstr(' ExceptionGroup '), class_name) != 0:
        return cstr(' __doc__ __module__ __weakref__ ')
    if _names_contain(cstr(' ImportError '), class_name) != 0:
        return cstr(' __doc__ __init__ __reduce__ __repr__ __str__ msg name name_from path ')
    if _names_contain(cstr(' KeyError '), class_name) != 0:
        return cstr(' __doc__ __str__ ')
    if _names_contain(cstr(' NameError '), class_name) != 0:
        return cstr(' __doc__ __init__ name ')
    if _names_contain(cstr(' OSError '), class_name) != 0:
        return cstr(' __doc__ __init__ __new__ __reduce__ __str__ characters_written errno filename filename2 strerror ')
    if _names_contain(cstr(' StopIteration '), class_name) != 0:
        return cstr(' __doc__ __init__ value ')
    if _names_contain(cstr(' SyntaxError '), class_name) != 0:
        return cstr(' __doc__ __init__ __str__ _metadata end_lineno end_offset filename lineno msg offset print_file_and_line text ')
    if _names_contain(cstr(' SystemExit '), class_name) != 0:
        return cstr(' __doc__ __init__ code ')
    return null()



def _builtin_base_names(m):
    """CPython's ``vars(T)`` names when MRO entry ``m`` is a builtin type, else NULL."""
    tag: int = py_builtin_type_class_tag(m)
    if tag >= PY_TYPE_NONE:
        return _builtin_type_names(tag)
    exc_index: int = 0
    names = null()
    while ptr_is_null(names) != 0 and exc_index < 65:
        exception_class = load_ptr(global_addr("py_exc_classes"), exc_index * C_POINTER_SIZE)
        if ptr_is_null(exception_class) == 0 and ptr_eq(m, exception_class) != 0:
            class_name = load_ptr(m, PYCLASSOBJECT_NAME_OFFSET)
            if ptr_is_null(class_name) == 0:
                names = _builtin_exception_names(class_name)
            exc_index = 65
        exc_index = exc_index + 1
    return names


def _mro_builtin_defines(cls, name) -> int:
    """Whether a builtin base of ``cls`` (dict included) defines ``name`` in CPython."""
    if not _ptr_is_class(cls):
        return 0
    if (load_i32(cls, PYOBJECTHEADER_FLAGS_OFFSET) & 4) != 0:
        if _names_contain(_builtin_type_names(PY_TYPE_DICT), name) != 0:
            return 1
    n_mro: int = load_i32(cls, PYCLASSOBJECT_N_MRO_OFFSET)
    mro = load_ptr(cls, PYCLASSOBJECT_MRO_OFFSET)
    defined: int = 0
    i: int = 0
    while defined == 0 and i < n_mro:
        m = pcc_gc_load_ptr(cls, ptr_add(mro, i * C_POINTER_SIZE))
        if ptr_is_null(m) == 0:
            table = _builtin_base_names(m)
            if ptr_is_null(table) == 0 and _names_contain(table, name) != 0:
                defined = 1
        i = i + 1
    return defined


def _super_raise_builtin_gap(base_name, name):
    """super() reached a builtin base whose CPython namespace defines ``name``."""
    parts = stack_alloc(5 * C_POINTER_SIZE)
    store_ptr(parts, 0, cstr("super(): attribute '"))
    store_ptr(parts, C_POINTER_SIZE, name)
    store_ptr(parts, 2 * C_POINTER_SIZE, cstr("' of builtin base '"))
    store_ptr(parts, 3 * C_POINTER_SIZE, base_name)
    store_ptr(parts, 4 * C_POINTER_SIZE, cstr("' is not implemented natively"))
    return _super_raise_message(11, parts, 5)


def _object_member_value(member, receiver, cls, name):
    """Bind object's ``member`` (NEW, consumed) as attribute access does."""
    if ptr_is_null(member) != 0 or is_tagged_int(member) != 0:
        return member
    tag: int = load_i32(member, PYOBJECTHEADER_TYPE_TAG_OFFSET)
    if tag == PY_TYPE_FUNC:
        if ptr_is_null(receiver) != 0:
            return member
        bound = py_instance_bind_method(member, receiver, name)
        py_decref(member)
        return bound
    if tag == PY_TYPE_CLASSMETHOD or tag == PY_TYPE_STATICMETHOD:
        got = _descriptor_call_get(member, receiver, cls)
        py_decref(member)
        return got
    return member


# ---- builtin bases' namespaces ----------------------------------------------
# A builtin base answers attribute lookup and super() from its own CPython
# namespace (vars(T)) at its MRO position. Like object's, these namespaces
# live beside the class objects: internal dispatch (py_obj_str's exception
# branch, the dict-subclass storage hooks) must not take their members for
# user overrides. Index 0 is BaseException, 1 KeyError, 2 dict. A name the
# CPython namespace defines and these lack stays an explicit gap.


def _builtin_namespace_names(index: int):
    """The member names of native namespace ``index`` (space-delimited)."""
    if index == 0:
        return cstr(' __init__ __str__ __repr__ add_note ')
    if index == 1:
        return cstr(' __str__ ')
    return cstr(' __init__ __getitem__ __setitem__ __delitem__ __contains__ __len__ __iter__ __repr__ __eq__ __ne__ __lt__ __le__ __gt__ __ge__ __or__ __ror__ __ior__ get keys values items pop popitem setdefault update clear copy ')


def _builtin_namespace_slot(index: int):
    if index == 0:
        return global_addr("pcc_exception_namespace")
    if index == 1:
        return global_addr("pcc_keyerror_namespace")
    return global_addr("pcc_dict_namespace")


def _builtin_namespace_index(m) -> int:
    """Native namespace of builtin MRO entry ``m``, or -1."""
    if ptr_eq(m, py_exc_builtin_class(0)) != 0:  # PY_EXC_BASE
        return 0
    if ptr_eq(m, py_exc_builtin_class(4)) != 0:  # PY_EXC_KEYERROR
        return 1
    if ptr_eq(m, global_load_ptr("pcc_type_cls_dict")) != 0:
        return 2
    return -1


def _member_owner_name(owner, receiver, kind: int):
    # kind 2 is a classmethod: CPython names it by its class. A method
    # descriptor names itself ``<owner>.<name>`` when reached through the
    # instance or called unbound (through super() CPython names type(self)).
    if kind == 2 and _ptr_is_class(receiver):
        name = load_ptr(receiver, PYCLASSOBJECT_NAME_OFFSET)
        if ptr_is_null(name) == 0:
            return name
    if kind == 1:
        return owner
    return _object_type_name(receiver)


def _member_decimal(buf, value: int):
    """``buf`` (24 bytes) holding ``value`` in decimal, NUL-terminated."""
    end: int = _object_decimal_append(buf, 0, value)
    store_i8(buf, end, 0)
    return buf


def _member_arity(owner, receiver, name, kind: int, expected: int, given: int,
                  keywords: int) -> int:
    """0 when a member call fits; else raise CPython's TypeError and return -1.

    ``kind`` 0 is a slot wrapper ("expected 1 argument, got 0", "__setattr__
    expected 2 arguments, got 1", "wrapper __eq__() takes no keyword
    arguments"); 1 a bound method descriptor and 2 a classmethod ("T.__format__()
    takes exactly one argument (0 given)", "C.__init_subclass__() takes no
    arguments (1 given)", "T.__format__() takes no keyword arguments").
    """
    if keywords == 0 and given == expected:
        return 0
    parts = stack_alloc(8 * C_POINTER_SIZE)
    expected_text = stack_alloc(24)
    given_text = stack_alloc(24)
    _member_decimal(expected_text, expected)
    _member_decimal(given_text, given)
    count: int = 0
    if keywords != 0:
        if kind == 0:
            store_ptr(parts, 0, cstr("wrapper "))
            store_ptr(parts, C_POINTER_SIZE, name)
            store_ptr(parts, 2 * C_POINTER_SIZE, cstr("() takes no keyword arguments"))
            count = 3
        else:
            store_ptr(parts, 0, _member_owner_name(owner, receiver, kind))
            store_ptr(parts, C_POINTER_SIZE, cstr("."))
            store_ptr(parts, 2 * C_POINTER_SIZE, name)
            store_ptr(parts, 3 * C_POINTER_SIZE, cstr("() takes no keyword arguments"))
            count = 4
    elif kind == 0:
        if _strs_eq(name, cstr("__setattr__")) != 0 or _strs_eq(name, cstr("__setitem__")) != 0:
            store_ptr(parts, 0, name)
            store_ptr(parts, C_POINTER_SIZE, cstr(" "))
            count = 2
        store_ptr(parts, count * C_POINTER_SIZE, cstr("expected "))
        store_ptr(parts, (count + 1) * C_POINTER_SIZE, expected_text)
        if expected == 1:
            store_ptr(parts, (count + 2) * C_POINTER_SIZE, cstr(" argument, got "))
        else:
            store_ptr(parts, (count + 2) * C_POINTER_SIZE, cstr(" arguments, got "))
        store_ptr(parts, (count + 3) * C_POINTER_SIZE, given_text)
        count = count + 4
    else:
        store_ptr(parts, 0, _member_owner_name(owner, receiver, kind))
        store_ptr(parts, C_POINTER_SIZE, cstr("."))
        store_ptr(parts, 2 * C_POINTER_SIZE, name)
        if expected == 0:
            store_ptr(parts, 3 * C_POINTER_SIZE, cstr("() takes no arguments ("))
        else:
            store_ptr(parts, 3 * C_POINTER_SIZE, cstr("() takes exactly one argument ("))
        store_ptr(parts, 4 * C_POINTER_SIZE, given_text)
        store_ptr(parts, 5 * C_POINTER_SIZE, cstr(" given)"))
        count = 6
    _super_raise_message(3, parts, count)
    return -1


def _member_range_arity(owner, name, minimum: int, maximum: int, given: int,
                        keywords: int) -> int:
    """CPython's positional range check ("get expected at least 1 argument, got 0")."""
    if keywords == 0 and given >= minimum and given <= maximum:
        return 0
    parts = stack_alloc(8 * C_POINTER_SIZE)
    bound_text = stack_alloc(24)
    given_text = stack_alloc(24)
    _member_decimal(given_text, given)
    count: int = 0
    if keywords != 0:
        store_ptr(parts, 0, owner)
        store_ptr(parts, C_POINTER_SIZE, cstr("."))
        store_ptr(parts, 2 * C_POINTER_SIZE, name)
        store_ptr(parts, 3 * C_POINTER_SIZE, cstr("() takes no keyword arguments"))
        count = 4
    else:
        bound: int = minimum
        qualifier = cstr(" expected at least ")
        if given > maximum:
            bound = maximum
            qualifier = cstr(" expected at most ")
        if minimum == maximum:
            qualifier = cstr(" expected ")
        _member_decimal(bound_text, bound)
        store_ptr(parts, 0, name)
        store_ptr(parts, C_POINTER_SIZE, qualifier)
        store_ptr(parts, 2 * C_POINTER_SIZE, bound_text)
        if bound == 1:
            store_ptr(parts, 3 * C_POINTER_SIZE, cstr(" argument, got "))
        else:
            store_ptr(parts, 3 * C_POINTER_SIZE, cstr(" arguments, got "))
        store_ptr(parts, 4 * C_POINTER_SIZE, given_text)
        count = 5
    _super_raise_message(3, parts, count)
    return -1


def _member_call_given(slots) -> int:
    extra = load_ptr(slots, C_POINTER_SIZE)
    if ptr_is_null(extra) != 0:
        return 0
    return py_tuple_len(extra)


def _member_call_keywords(slots) -> int:
    kwargs = load_ptr(slots, 2 * C_POINTER_SIZE)
    if ptr_is_null(kwargs) == 0 and ptr_eq(kwargs, global_load_ptr("py_None")) == 0:
        if py_dict_len(kwargs) != 0:
            return 1
    return 0


def _member_call_check(slots, owner, name, kind: int, expected: int) -> int:
    return _member_arity(owner, load_ptr(slots, 0), name, kind, expected,
                         _member_call_given(slots), _member_call_keywords(slots))


def _member_receiver_error(kind: int, owner, name, receiver):
    """CPython's descriptor TypeError for a receiver of the wrong type."""
    shown = py_obj_type_name(receiver)
    if ptr_is_null(shown) != 0:
        return null()
    pcc_gc_pin(shown)
    parts = stack_alloc(7 * C_POINTER_SIZE)
    store_ptr(parts, 0, cstr("descriptor '"))
    store_ptr(parts, C_POINTER_SIZE, name)
    if kind == 0:
        store_ptr(parts, 2 * C_POINTER_SIZE, cstr("' requires a '"))
        store_ptr(parts, 3 * C_POINTER_SIZE, owner)
        store_ptr(parts, 4 * C_POINTER_SIZE, cstr("' object but received a '"))
        store_ptr(parts, 5 * C_POINTER_SIZE, py_str_utf8(shown))
        store_ptr(parts, 6 * C_POINTER_SIZE, cstr("'"))
    else:
        store_ptr(parts, 2 * C_POINTER_SIZE, cstr("' for '"))
        store_ptr(parts, 3 * C_POINTER_SIZE, owner)
        store_ptr(parts, 4 * C_POINTER_SIZE, cstr("' objects doesn't apply to a '"))
        store_ptr(parts, 5 * C_POINTER_SIZE, py_str_utf8(shown))
        store_ptr(parts, 6 * C_POINTER_SIZE, cstr("' object"))
    _super_raise_message(3, parts, 7)
    pcc_gc_unpin(shown)
    py_decref(shown)
    return null()


def _member_tag(value) -> int:
    if ptr_is_null(value) != 0:
        return -1
    if is_tagged_int(value) != 0:
        return PY_TYPE_INT
    return load_i32(value, PYOBJECTHEADER_TYPE_TAG_OFFSET)


# ---- BaseException and KeyError ----------------------------------------------


def _exception_member_receiver(slots, kind: int, name) -> int:
    """1 when slots[0] is an exception; else CPython's descriptor TypeError."""
    receiver = load_ptr(slots, 0)
    if _member_tag(receiver) == PY_TYPE_EXC:
        return 1
    exc_base = py_exc_builtin_class(0)  # PY_EXC_BASE
    if ptr_is_null(exc_base) == 0 and _ptr_is_instance(receiver):
        if py_isinstance(receiver, exc_base) != 0:
            return 1
    if py_err_occurred() == 0:
        _member_receiver_error(kind, cstr("BaseException"), name, receiver)
    return 0


def _exception_init_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    # BaseException_init: keywords are a TypeError naming type(self); the
    # positional arguments replace ``args``.
    slots = stack_alloc(3 * C_POINTER_SIZE)
    _object_call_args(args, slots)
    result = null()
    if _exception_member_receiver(slots, 0, cstr("__init__")) != 0:
        receiver = load_ptr(slots, 0)
        if _member_call_keywords(slots) != 0:
            _object_type_error(_object_type_name(receiver), cstr("() takes no keyword arguments"), cstr(""))
        elif _ptr_is_instance(receiver):
            if py_instance_setattr(receiver, cstr("args"), load_ptr(slots, C_POINTER_SIZE)) == 0:
                result = _object_new_ref(global_load_ptr("py_None"))
        else:
            _object_gap_entry(args, cstr("BaseException.__init__() on a builtin exception object"),
                              cstr("its args are fixed when it is created"))
    _object_call_release(slots)
    return result


def _exception_str_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    slots = stack_alloc(3 * C_POINTER_SIZE)
    _object_call_args(args, slots)
    result = null()
    if _exception_member_receiver(slots, 0, cstr("__str__")) != 0:
        if _member_call_check(slots, cstr("BaseException"), cstr("__str__"), 0, 0) == 0:
            result = py_exc_base_str(load_ptr(slots, 0), 0)
    _object_call_release(slots)
    return result


def _keyerror_str_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    # KeyError.__str__: one argument shows as its repr.
    slots = stack_alloc(3 * C_POINTER_SIZE)
    _object_call_args(args, slots)
    result = null()
    receiver = load_ptr(slots, 0)
    keyerror = py_exc_builtin_class(4)  # PY_EXC_KEYERROR
    if ptr_is_null(keyerror) == 0 and py_isinstance(receiver, keyerror) == 0:
        if py_err_occurred() == 0:
            _member_receiver_error(0, cstr("KeyError"), cstr("__str__"), receiver)
    elif _member_call_check(slots, cstr("KeyError"), cstr("__str__"), 0, 0) == 0:
        result = py_exc_base_str(receiver, 1)
    _object_call_release(slots)
    return result


def _exception_repr_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    slots = stack_alloc(3 * C_POINTER_SIZE)
    _object_call_args(args, slots)
    result = null()
    if _exception_member_receiver(slots, 0, cstr("__repr__")) != 0:
        if _member_call_check(slots, cstr("BaseException"), cstr("__repr__"), 0, 0) == 0:
            result = py_exc_base_repr(load_ptr(slots, 0))
    _object_call_release(slots)
    return result


def _exception_add_note_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    # BaseException.add_note: a str note appended to __notes__, which it
    # creates as a list when the lookup raises AttributeError.
    slots = stack_alloc(3 * C_POINTER_SIZE)
    _object_call_args(args, slots)
    result = null()
    if (_exception_member_receiver(slots, 1, cstr("add_note")) != 0
            and _member_call_check(slots, cstr("BaseException"), cstr("add_note"), 1, 1) == 0):
        note = _object_call_arg(slots, 0)
        pcc_gc_pin(note)
        receiver = load_ptr(slots, 0)
        if _object_is_str(note) == 0:
            _object_typed_error(cstr("add_note() argument must be str, not "), note, cstr(""))
        elif _ptr_is_instance(receiver) == 0:
            _object_gap_entry(args, cstr("BaseException.add_note() on a builtin exception object"),
                              cstr("it has no attribute storage for __notes__"))
        else:
            notes = py_instance_getattr(receiver, cstr("__notes__"))
            if ptr_is_null(notes) != 0:
                # A miss is NULL without an exception, or an AttributeError
                # (from __getattr__); any other error propagates.
                missing: int = 1
                if py_err_occurred() != 0:
                    missing = 0
                    current = py_current_exception()
                    attribute_error = py_exc_builtin_class(6)  # PY_EXC_ATTRIBUTEERROR
                    if (ptr_is_null(current) == 0 and ptr_is_null(attribute_error) == 0
                            and py_exc_matches(current, attribute_error) != 0):
                        py_clear_exception()
                        missing = 1
                if missing != 0:
                    created = py_list_new(0)
                    if ptr_is_null(created) == 0:
                        pcc_gc_pin(created)
                        py_list_append(created, note)
                        if py_err_occurred() == 0 and py_instance_setattr(receiver, cstr("__notes__"), created) == 0:
                            result = _object_new_ref(global_load_ptr("py_None"))
                        pcc_gc_unpin(created)
                        py_decref(created)
            else:
                pcc_gc_pin(notes)
                if _member_tag(notes) == PY_TYPE_LIST:
                    py_list_append(notes, note)
                    if py_err_occurred() == 0:
                        result = _object_new_ref(global_load_ptr("py_None"))
                else:
                    py_raise_owned(py_exc_new(3, cstr("Cannot add note: __notes__ is not a list")))
                pcc_gc_unpin(notes)
                py_decref(notes)
        pcc_gc_unpin(note)
        py_decref(note)
    _object_call_release(slots)
    return result


# ---- dict ---------------------------------------------------------------------


def _dict_member_storage(receiver, kind: int, name):
    """The dict a dict member acts on (borrowed): an exact dict itself or a
    dict subclass instance's item storage; NULL after CPython's TypeError."""
    tag: int = _member_tag(receiver)
    if tag == PY_TYPE_DICT:
        return receiver
    if _ptr_is_instance(receiver):
        cls = _object_receiver_class(receiver)
        if ptr_is_null(cls) == 0 and (load_i32(cls, PYOBJECTHEADER_FLAGS_OFFSET) & 4) != 0:
            return py_dict_subclass_storage(receiver, 1)
    if py_err_occurred() == 0:
        _member_receiver_error(kind, cstr("dict"), name, receiver)
    return null()


def _dict_like_storage(value):
    """The item storage of an exact dict or dict subclass instance, else NULL."""
    tag: int = _member_tag(value)
    if tag == PY_TYPE_DICT:
        return value
    if _ptr_is_instance(value):
        cls = _object_receiver_class(value)
        if ptr_is_null(cls) == 0 and (load_i32(cls, PYOBJECTHEADER_FLAGS_OFFSET) & 4) != 0:
            return py_dict_subclass_storage(value, 1)
    return null()


def _dict_update_from(storage, source, name) -> int:
    """dict_update_arg: a mapping's items or an iterable of key/value pairs."""
    other = _dict_like_storage(source)
    if ptr_is_null(other) == 0:
        py_dict_update(storage, other)
        return 0 if py_err_occurred() == 0 else -1
    if py_err_occurred() != 0:
        return -1
    iterator = py_obj_iter(source)
    if ptr_is_null(iterator) != 0:
        return -1
    pcc_gc_pin(iterator)
    status: int = 0
    done: int = 0
    index: int = 0
    while done == 0:
        item = py_obj_next(iterator)
        if ptr_is_null(item) != 0:
            if py_err_occurred() != 0:
                stop = py_exc_builtin_class(8)  # PY_EXC_STOPITERATION
                if py_exc_matches(py_current_exception(), stop) != 0:
                    py_clear_exception()
                else:
                    status = -1
            done = 1
        else:
            pcc_gc_pin(item)
            if _member_tag(item) != PY_TYPE_TUPLE and _member_tag(item) != PY_TYPE_LIST:
                text = stack_alloc(24)
                parts = stack_alloc(3 * C_POINTER_SIZE)
                store_ptr(parts, 0, cstr("cannot convert dictionary update sequence element #"))
                store_ptr(parts, C_POINTER_SIZE, _member_decimal(text, index))
                store_ptr(parts, 2 * C_POINTER_SIZE, cstr(" to a sequence"))
                _super_raise_message(3, parts, 3)
                status = -1
                done = 1
            else:
                length: int = py_obj_len(item)
                if length != 2:
                    if py_err_occurred() == 0:
                        number = stack_alloc(24)
                        size = stack_alloc(24)
                        parts2 = stack_alloc(5 * C_POINTER_SIZE)
                        store_ptr(parts2, 0, cstr("dictionary update sequence element #"))
                        store_ptr(parts2, C_POINTER_SIZE, _member_decimal(number, index))
                        store_ptr(parts2, 2 * C_POINTER_SIZE, cstr(" has length "))
                        store_ptr(parts2, 3 * C_POINTER_SIZE, _member_decimal(size, length))
                        store_ptr(parts2, 4 * C_POINTER_SIZE, cstr("; 2 is required"))
                        _super_raise_message(10, parts2, 5)
                    status = -1
                    done = 1
                else:
                    key = py_obj_getitem(item, py_int_from_i64(0))
                    value = null()
                    if ptr_is_null(key) == 0:
                        pcc_gc_pin(key)
                        value = py_obj_getitem(item, py_int_from_i64(1))
                    if ptr_is_null(key) != 0 or ptr_is_null(value) != 0:
                        status = -1
                        done = 1
                    else:
                        py_dict_set(storage, key, value)
                        if py_err_occurred() != 0:
                            status = -1
                            done = 1
                        py_decref(value)
                    if ptr_is_null(key) == 0:
                        pcc_gc_unpin(key)
                        py_decref(key)
            pcc_gc_unpin(item)
            py_decref(item)
            index = index + 1
    pcc_gc_unpin(iterator)
    py_decref(iterator)
    return status


def _dict_update_call(slots, name, maximum_error_name) -> int:
    """dict_update_common: at most one positional source, then the keywords."""
    given: int = _member_call_given(slots)
    if given > 1:
        return _member_range_arity(cstr("dict"), maximum_error_name, 0, 1, given, 0)
    storage = _dict_member_storage(load_ptr(slots, 0), 1, name)
    if ptr_is_null(storage) != 0:
        return -1
    pcc_gc_pin(storage)
    status: int = 0
    if given == 1:
        source = _object_call_arg(slots, 0)
        pcc_gc_pin(source)
        status = _dict_update_from(storage, source, name)
        pcc_gc_unpin(source)
        py_decref(source)
    if status == 0 and _member_call_keywords(slots) != 0:
        py_dict_update(storage, load_ptr(slots, 2 * C_POINTER_SIZE))
        if py_err_occurred() != 0:
            status = -1
    pcc_gc_unpin(storage)
    return status


def _dict_init_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    slots = stack_alloc(3 * C_POINTER_SIZE)
    _object_call_args(args, slots)
    result = null()
    if _dict_update_call(slots, cstr("__init__"), cstr("dict")) == 0:
        result = _object_new_ref(global_load_ptr("py_None"))
    _object_call_release(slots)
    return result


def _dict_update_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    slots = stack_alloc(3 * C_POINTER_SIZE)
    _object_call_args(args, slots)
    result = null()
    if _dict_update_call(slots, cstr("update"), cstr("update")) == 0:
        result = _object_new_ref(global_load_ptr("py_None"))
    _object_call_release(slots)
    return result


def _dict_key_error(key):
    py_raise_owned(py_exc_new_with_value(4, key))  # KeyError(key)
    return null()


def _dict_getitem_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    # dict_subscript: a subclass's __missing__ answers a missing key.
    slots = stack_alloc(3 * C_POINTER_SIZE)
    _object_call_args(args, slots)
    result = null()
    storage = _dict_member_storage(load_ptr(slots, 0), 1, cstr("__getitem__"))
    if ptr_is_null(storage) == 0 and _member_call_check(slots, cstr("dict"), cstr("__getitem__"), 1, 1) == 0:
        key = _object_call_arg(slots, 0)
        receiver = load_ptr(slots, 0)
        if ptr_eq(storage, receiver) != 0:
            result = py_dict_getitem(storage, key)
        else:
            result = py_dict_subclass_getitem(receiver, key)
        py_decref(key)
    _object_call_release(slots)
    return result


def _dict_setitem_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    slots = stack_alloc(3 * C_POINTER_SIZE)
    _object_call_args(args, slots)
    result = null()
    storage = _dict_member_storage(load_ptr(slots, 0), 0, cstr("__setitem__"))
    if ptr_is_null(storage) == 0 and _member_call_check(slots, cstr("dict"), cstr("__setitem__"), 0, 2) == 0:
        key = _object_call_arg(slots, 0)
        value = _object_call_arg(slots, 1)
        py_dict_set(storage, key, value)
        if py_err_occurred() == 0:
            result = _object_new_ref(global_load_ptr("py_None"))
        py_decref(value)
        py_decref(key)
    _object_call_release(slots)
    return result


def _dict_delitem_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    slots = stack_alloc(3 * C_POINTER_SIZE)
    _object_call_args(args, slots)
    result = null()
    storage = _dict_member_storage(load_ptr(slots, 0), 0, cstr("__delitem__"))
    if ptr_is_null(storage) == 0 and _member_call_check(slots, cstr("dict"), cstr("__delitem__"), 0, 1) == 0:
        key = _object_call_arg(slots, 0)
        pcc_gc_pin(key)
        if py_dict_del(storage, key) < 0:
            if py_err_occurred() == 0:
                _dict_key_error(key)
        elif py_err_occurred() == 0:
            result = _object_new_ref(global_load_ptr("py_None"))
        pcc_gc_unpin(key)
        py_decref(key)
    _object_call_release(slots)
    return result


def _dict_contains_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    slots = stack_alloc(3 * C_POINTER_SIZE)
    _object_call_args(args, slots)
    result = null()
    storage = _dict_member_storage(load_ptr(slots, 0), 1, cstr("__contains__"))
    if ptr_is_null(storage) == 0 and _member_call_check(slots, cstr("dict"), cstr("__contains__"), 1, 1) == 0:
        key = _object_call_arg(slots, 0)
        found: int = py_dict_contains(storage, key)
        if py_err_occurred() == 0:
            result = py_bool_from_bit(1 if found != 0 else 0)
        py_decref(key)
    _object_call_release(slots)
    return result


def _dict_len_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    slots = stack_alloc(3 * C_POINTER_SIZE)
    _object_call_args(args, slots)
    result = null()
    storage = _dict_member_storage(load_ptr(slots, 0), 0, cstr("__len__"))
    if ptr_is_null(storage) == 0 and _member_call_check(slots, cstr("dict"), cstr("__len__"), 0, 0) == 0:
        result = py_int_from_i64(py_dict_len(storage))
    _object_call_release(slots)
    return result


def _dict_iter_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    slots = stack_alloc(3 * C_POINTER_SIZE)
    _object_call_args(args, slots)
    result = null()
    storage = _dict_member_storage(load_ptr(slots, 0), 0, cstr("__iter__"))
    if ptr_is_null(storage) == 0 and _member_call_check(slots, cstr("dict"), cstr("__iter__"), 0, 0) == 0:
        result = py_obj_iter(storage)
    _object_call_release(slots)
    return result


def _dict_repr_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    slots = stack_alloc(3 * C_POINTER_SIZE)
    _object_call_args(args, slots)
    result = null()
    storage = _dict_member_storage(load_ptr(slots, 0), 0, cstr("__repr__"))
    if ptr_is_null(storage) == 0 and _member_call_check(slots, cstr("dict"), cstr("__repr__"), 0, 0) == 0:
        result = py_obj_repr(storage)
    _object_call_release(slots)
    return result


def _dict_compare(args, name, negate: int):
    # dict_richcompare: equality with another dict, NotImplemented otherwise.
    slots = stack_alloc(3 * C_POINTER_SIZE)
    _object_call_args(args, slots)
    result = null()
    storage = _dict_member_storage(load_ptr(slots, 0), 0, name)
    if ptr_is_null(storage) == 0 and _member_call_check(slots, cstr("dict"), name, 0, 1) == 0:
        other_value = _object_call_arg(slots, 0)
        pcc_gc_pin(other_value)
        pcc_gc_pin(storage)
        other = _dict_like_storage(other_value)
        if ptr_is_null(other) != 0:
            if py_err_occurred() == 0:
                result = _object_new_ref(global_load_ptr("py_NotImplemented"))
        else:
            equal: int = py_obj_eq(storage, other)
            if equal >= 0 and py_err_occurred() == 0:
                if negate != 0:
                    equal = 1 if equal == 0 else 0
                result = py_bool_from_bit(1 if equal != 0 else 0)
        pcc_gc_unpin(storage)
        pcc_gc_unpin(other_value)
        py_decref(other_value)
    _object_call_release(slots)
    return result


def _dict_eq_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    return _dict_compare(args, cstr("__eq__"), 0)


def _dict_ne_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    return _dict_compare(args, cstr("__ne__"), 1)


def _dict_order(args, name):
    # dict defines the orderings only to answer NotImplemented.
    slots = stack_alloc(3 * C_POINTER_SIZE)
    _object_call_args(args, slots)
    result = null()
    storage = _dict_member_storage(load_ptr(slots, 0), 0, name)
    if ptr_is_null(storage) == 0 and _member_call_check(slots, cstr("dict"), name, 0, 1) == 0:
        result = _object_new_ref(global_load_ptr("py_NotImplemented"))
    _object_call_release(slots)
    return result


def _dict_lt_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    return _dict_order(args, cstr("__lt__"))


def _dict_le_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    return _dict_order(args, cstr("__le__"))


def _dict_gt_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    return _dict_order(args, cstr("__gt__"))


def _dict_ge_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    return _dict_order(args, cstr("__ge__"))


def _dict_copy_of(storage):
    """A NEW exact dict with ``storage``'s items."""
    pcc_gc_pin(storage)
    fresh = py_dict_new()
    if ptr_is_null(fresh) == 0:
        pcc_gc_pin(fresh)
        py_dict_update(fresh, storage)
        pcc_gc_unpin(fresh)
        if py_err_occurred() != 0:
            py_decref(fresh)
            fresh = null()
    pcc_gc_unpin(storage)
    return fresh


def _dict_union(args, name, reflected: int):
    # dict_or / dict_ror: a new exact dict, the left operand's items first.
    slots = stack_alloc(3 * C_POINTER_SIZE)
    _object_call_args(args, slots)
    result = null()
    storage = _dict_member_storage(load_ptr(slots, 0), 0, name)
    if ptr_is_null(storage) == 0 and _member_call_check(slots, cstr("dict"), name, 0, 1) == 0:
        other_value = _object_call_arg(slots, 0)
        pcc_gc_pin(other_value)
        pcc_gc_pin(storage)
        other = _dict_like_storage(other_value)
        if ptr_is_null(other) != 0:
            if py_err_occurred() == 0:
                result = _object_new_ref(global_load_ptr("py_NotImplemented"))
        else:
            pcc_gc_pin(other)
            first = storage
            second = other
            if reflected != 0:
                first = other
                second = storage
            result = _dict_copy_of(first)
            if ptr_is_null(result) == 0:
                pcc_gc_pin(result)
                py_dict_update(result, second)
                pcc_gc_unpin(result)
                if py_err_occurred() != 0:
                    py_decref(result)
                    result = null()
            pcc_gc_unpin(other)
        pcc_gc_unpin(storage)
        pcc_gc_unpin(other_value)
        py_decref(other_value)
    _object_call_release(slots)
    return result


def _dict_or_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    return _dict_union(args, cstr("__or__"), 0)


def _dict_ror_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    return _dict_union(args, cstr("__ror__"), 1)


def _dict_ior_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    # dict_ior: update in place from a mapping or pairs, return self.
    slots = stack_alloc(3 * C_POINTER_SIZE)
    _object_call_args(args, slots)
    result = null()
    storage = _dict_member_storage(load_ptr(slots, 0), 0, cstr("__ior__"))
    if ptr_is_null(storage) == 0 and _member_call_check(slots, cstr("dict"), cstr("__ior__"), 0, 1) == 0:
        source = _object_call_arg(slots, 0)
        pcc_gc_pin(source)
        pcc_gc_pin(storage)
        if _dict_update_from(storage, source, cstr("__ior__")) == 0:
            result = _object_new_ref(load_ptr(slots, 0))
        pcc_gc_unpin(storage)
        pcc_gc_unpin(source)
        py_decref(source)
    _object_call_release(slots)
    return result


def _dict_get_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    slots = stack_alloc(3 * C_POINTER_SIZE)
    _object_call_args(args, slots)
    result = null()
    storage = _dict_member_storage(load_ptr(slots, 0), 1, cstr("get"))
    if ptr_is_null(storage) == 0 and _member_range_arity(cstr("dict"), cstr("get"), 1, 2,
                                                         _member_call_given(slots),
                                                         _member_call_keywords(slots)) == 0:
        key = _object_call_arg(slots, 0)
        default = global_load_ptr("py_None")
        owned_default: int = 0
        if _member_call_given(slots) == 2:
            default = _object_call_arg(slots, 1)
            owned_default = 1
        result = py_dict_get_default(storage, key, default)
        if owned_default != 0:
            py_decref(default)
        py_decref(key)
    _object_call_release(slots)
    return result


def _dict_setdefault_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    slots = stack_alloc(3 * C_POINTER_SIZE)
    _object_call_args(args, slots)
    result = null()
    storage = _dict_member_storage(load_ptr(slots, 0), 1, cstr("setdefault"))
    if ptr_is_null(storage) == 0 and _member_range_arity(cstr("dict"), cstr("setdefault"), 1, 2,
                                                         _member_call_given(slots),
                                                         _member_call_keywords(slots)) == 0:
        key = _object_call_arg(slots, 0)
        pcc_gc_pin(key)
        result = py_dict_get(storage, key)
        if ptr_is_null(result) != 0 and py_err_occurred() == 0:
            if _member_call_given(slots) == 2:
                result = _object_call_arg(slots, 1)
            else:
                result = _object_new_ref(global_load_ptr("py_None"))
            py_dict_set(storage, key, result)
            if py_err_occurred() != 0:
                py_decref(result)
                result = null()
        pcc_gc_unpin(key)
        py_decref(key)
    _object_call_release(slots)
    return result


def _dict_pop_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    slots = stack_alloc(3 * C_POINTER_SIZE)
    _object_call_args(args, slots)
    result = null()
    storage = _dict_member_storage(load_ptr(slots, 0), 1, cstr("pop"))
    if ptr_is_null(storage) == 0 and _member_range_arity(cstr("dict"), cstr("pop"), 1, 2,
                                                         _member_call_given(slots),
                                                         _member_call_keywords(slots)) == 0:
        key = _object_call_arg(slots, 0)
        pcc_gc_pin(key)
        result = py_dict_get(storage, key)
        if ptr_is_null(result) == 0:
            py_dict_del(storage, key)
            if py_err_occurred() != 0:
                py_decref(result)
                result = null()
        elif py_err_occurred() == 0:
            if _member_call_given(slots) == 2:
                result = _object_call_arg(slots, 1)
            else:
                _dict_key_error(key)
        pcc_gc_unpin(key)
        py_decref(key)
    _object_call_release(slots)
    return result


def _dict_popitem_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    slots = stack_alloc(3 * C_POINTER_SIZE)
    _object_call_args(args, slots)
    result = null()
    storage = _dict_member_storage(load_ptr(slots, 0), 1, cstr("popitem"))
    if ptr_is_null(storage) == 0 and _member_call_check(slots, cstr("dict"), cstr("popitem"), 1, 0) == 0:
        if py_dict_len(storage) == 0:
            py_raise_owned(py_exc_new_with_value(4, py_str_new(cstr("popitem(): dictionary is empty"), 29)))
        else:
            result = py_dict_popitem(storage)
    _object_call_release(slots)
    return result


def _dict_view(args, name, which: int):
    slots = stack_alloc(3 * C_POINTER_SIZE)
    _object_call_args(args, slots)
    result = null()
    storage = _dict_member_storage(load_ptr(slots, 0), 1, name)
    if ptr_is_null(storage) == 0 and _member_call_check(slots, cstr("dict"), name, 1, 0) == 0:
        if which == 0:
            result = py_dict_keys(storage)
        elif which == 1:
            result = py_dict_values(storage)
        else:
            result = py_dict_items(storage)
    _object_call_release(slots)
    return result


def _dict_keys_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    return _dict_view(args, cstr("keys"), 0)


def _dict_values_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    return _dict_view(args, cstr("values"), 1)


def _dict_items_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    return _dict_view(args, cstr("items"), 2)


def _dict_clear_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    slots = stack_alloc(3 * C_POINTER_SIZE)
    _object_call_args(args, slots)
    result = null()
    storage = _dict_member_storage(load_ptr(slots, 0), 1, cstr("clear"))
    if ptr_is_null(storage) == 0 and _member_call_check(slots, cstr("dict"), cstr("clear"), 1, 0) == 0:
        py_dict_clear(storage)
        if py_err_occurred() == 0:
            result = _object_new_ref(global_load_ptr("py_None"))
    _object_call_release(slots)
    return result


def _dict_copy_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    # dict.copy on a subclass instance returns an exact dict.
    slots = stack_alloc(3 * C_POINTER_SIZE)
    _object_call_args(args, slots)
    result = null()
    storage = _dict_member_storage(load_ptr(slots, 0), 1, cstr("copy"))
    if ptr_is_null(storage) == 0 and _member_call_check(slots, cstr("dict"), cstr("copy"), 1, 0) == 0:
        result = _dict_copy_of(storage)
    _object_call_release(slots)
    return result


# ---- construction and lookup ----------------------------------------------------


def _builtin_namespace_build(index: int):
    """A NEW dict of native namespace ``index``'s members."""
    namespace = py_dict_new()
    if ptr_is_null(namespace) != 0:
        return null()
    pcc_gc_pin(namespace)
    # Each entry appears directly in py_func_new_bound so the port compiler
    # passes its code address (a function value through a helper would be
    # boxed as an object).
    captures = _object_member_captures(cstr("self"))
    status: int = 0
    if ptr_is_null(captures) != 0:
        status = -1
    else:
        pcc_gc_pin(captures)
        if index == 0:
            status = status | _object_namespace_add(namespace, cstr("__init__"), py_func_new_bound(_exception_init_entry, captures, cstr("__init__"), null()), 0)
            status = status | _object_namespace_add(namespace, cstr("__str__"), py_func_new_bound(_exception_str_entry, captures, cstr("__str__"), null()), 0)
            status = status | _object_namespace_add(namespace, cstr("__repr__"), py_func_new_bound(_exception_repr_entry, captures, cstr("__repr__"), null()), 0)
            status = status | _object_namespace_add(namespace, cstr("add_note"), py_func_new_bound(_exception_add_note_entry, captures, cstr("add_note"), null()), 0)
        elif index == 1:
            status = status | _object_namespace_add(namespace, cstr("__str__"), py_func_new_bound(_keyerror_str_entry, captures, cstr("__str__"), null()), 0)
        else:
            status = status | _object_namespace_add(namespace, cstr("__init__"), py_func_new_bound(_dict_init_entry, captures, cstr("__init__"), null()), 0)
            status = status | _object_namespace_add(namespace, cstr("__getitem__"), py_func_new_bound(_dict_getitem_entry, captures, cstr("__getitem__"), null()), 0)
            status = status | _object_namespace_add(namespace, cstr("__setitem__"), py_func_new_bound(_dict_setitem_entry, captures, cstr("__setitem__"), null()), 0)
            status = status | _object_namespace_add(namespace, cstr("__delitem__"), py_func_new_bound(_dict_delitem_entry, captures, cstr("__delitem__"), null()), 0)
            status = status | _object_namespace_add(namespace, cstr("__contains__"), py_func_new_bound(_dict_contains_entry, captures, cstr("__contains__"), null()), 0)
            status = status | _object_namespace_add(namespace, cstr("__len__"), py_func_new_bound(_dict_len_entry, captures, cstr("__len__"), null()), 0)
            status = status | _object_namespace_add(namespace, cstr("__iter__"), py_func_new_bound(_dict_iter_entry, captures, cstr("__iter__"), null()), 0)
            status = status | _object_namespace_add(namespace, cstr("__repr__"), py_func_new_bound(_dict_repr_entry, captures, cstr("__repr__"), null()), 0)
            status = status | _object_namespace_add(namespace, cstr("__eq__"), py_func_new_bound(_dict_eq_entry, captures, cstr("__eq__"), null()), 0)
            status = status | _object_namespace_add(namespace, cstr("__ne__"), py_func_new_bound(_dict_ne_entry, captures, cstr("__ne__"), null()), 0)
            status = status | _object_namespace_add(namespace, cstr("__lt__"), py_func_new_bound(_dict_lt_entry, captures, cstr("__lt__"), null()), 0)
            status = status | _object_namespace_add(namespace, cstr("__le__"), py_func_new_bound(_dict_le_entry, captures, cstr("__le__"), null()), 0)
            status = status | _object_namespace_add(namespace, cstr("__gt__"), py_func_new_bound(_dict_gt_entry, captures, cstr("__gt__"), null()), 0)
            status = status | _object_namespace_add(namespace, cstr("__ge__"), py_func_new_bound(_dict_ge_entry, captures, cstr("__ge__"), null()), 0)
            status = status | _object_namespace_add(namespace, cstr("__or__"), py_func_new_bound(_dict_or_entry, captures, cstr("__or__"), null()), 0)
            status = status | _object_namespace_add(namespace, cstr("__ror__"), py_func_new_bound(_dict_ror_entry, captures, cstr("__ror__"), null()), 0)
            status = status | _object_namespace_add(namespace, cstr("__ior__"), py_func_new_bound(_dict_ior_entry, captures, cstr("__ior__"), null()), 0)
            status = status | _object_namespace_add(namespace, cstr("get"), py_func_new_bound(_dict_get_entry, captures, cstr("get"), null()), 0)
            status = status | _object_namespace_add(namespace, cstr("keys"), py_func_new_bound(_dict_keys_entry, captures, cstr("keys"), null()), 0)
            status = status | _object_namespace_add(namespace, cstr("values"), py_func_new_bound(_dict_values_entry, captures, cstr("values"), null()), 0)
            status = status | _object_namespace_add(namespace, cstr("items"), py_func_new_bound(_dict_items_entry, captures, cstr("items"), null()), 0)
            status = status | _object_namespace_add(namespace, cstr("pop"), py_func_new_bound(_dict_pop_entry, captures, cstr("pop"), null()), 0)
            status = status | _object_namespace_add(namespace, cstr("popitem"), py_func_new_bound(_dict_popitem_entry, captures, cstr("popitem"), null()), 0)
            status = status | _object_namespace_add(namespace, cstr("setdefault"), py_func_new_bound(_dict_setdefault_entry, captures, cstr("setdefault"), null()), 0)
            status = status | _object_namespace_add(namespace, cstr("update"), py_func_new_bound(_dict_update_entry, captures, cstr("update"), null()), 0)
            status = status | _object_namespace_add(namespace, cstr("clear"), py_func_new_bound(_dict_clear_entry, captures, cstr("clear"), null()), 0)
            status = status | _object_namespace_add(namespace, cstr("copy"), py_func_new_bound(_dict_copy_entry, captures, cstr("copy"), null()), 0)
        pcc_gc_unpin(captures)
        py_decref(captures)
    pcc_gc_unpin(namespace)
    if status != 0:
        py_decref(namespace)
        return _class_require_result(null(), cstr("builtin namespace"),
                                     cstr("builtin namespace construction failed"))
    return namespace


def _builtin_namespace(index: int):
    """Native namespace ``index``, borrowed from its scheduler root, or NULL."""
    slot = _builtin_namespace_slot(index)
    published = pcc_gc_load_ptr(null(), slot)
    if ptr_is_null(published) == 0:
        return published
    mutex = _object_namespace_mutex()
    if ptr_is_null(mutex) != 0 or pcc_mutex_lock(mutex) != 0:
        return _class_require_result(null(), cstr("builtin namespace"),
                                     cstr("builtin namespace lock failed"))
    published = pcc_gc_load_ptr(null(), slot)
    if ptr_is_null(published) == 0:
        pcc_mutex_unlock(mutex)
        return published
    bit: int = 1
    if index == 1:
        bit = 2
    elif index == 2:
        bit = 4
    registered: int = load_i32(global_addr("pcc_builtin_namespaces_registered"), 0)
    if (registered & bit) == 0:
        handle = pcc_gc_scheduler_root_register_handle(slot)
        if ptr_is_null(handle) != 0:
            pcc_mutex_unlock(mutex)
            return _class_require_result(null(), cstr("builtin namespace"),
                                         cstr("builtin namespace root registration failed"))
        store_i32(global_addr("pcc_builtin_namespaces_registered"), 0, registered | bit)
    namespace = _builtin_namespace_build(index)
    if ptr_is_null(namespace) != 0:
        pcc_mutex_unlock(mutex)
        return null()
    pcc_gc_pin(namespace)
    pcc_py_gc_minor_graph_lock()
    pcc_gc_store_root(slot, namespace)
    pcc_py_gc_minor_graph_unlock()
    pcc_gc_unpin(namespace)
    py_decref(namespace)
    pcc_mutex_unlock(mutex)
    return pcc_gc_load_ptr(null(), slot)


def _builtin_namespace_get(index: int, name):
    """Member ``name`` of native namespace ``index`` as a NEW reference, or NULL."""
    if _names_contain(_builtin_namespace_names(index), name) == 0:
        return null()
    namespace = _builtin_namespace(index)
    if ptr_is_null(namespace) != 0:
        return null()
    owned_key: int = 0
    key = pcc_cached_name_key(name)
    if ptr_is_null(key) != 0:
        key = py_str_new(name, strlen(name))
        if ptr_is_null(key) != 0:
            return null()
        owned_key = 1
    value = py_dict_get(namespace, key)
    if owned_key != 0:
        py_decref(key)
    return value


def _builtin_mro_member(cls, name, include_type: int):
    """A NEW member of the first builtin base along ``cls``'s MRO whose native
    namespace has ``name``, or NULL. dict, which is not an MRO entry, answers
    just before the root. ``include_type`` also looks at ``cls`` itself being
    a builtin type (class attribute access such as ``dict.get``)."""
    if not _ptr_is_class(cls):
        return null()
    if include_type != 0 and ptr_eq(cls, global_load_ptr("pcc_type_cls_dict")) != 0:
        return _builtin_namespace_get(2, name)
    flags: int = load_i32(cls, PYOBJECTHEADER_FLAGS_OFFSET)
    if (flags & 4) != 0:
        return _builtin_namespace_get(2, name)
    if ((flags & 16777216) == 0 and include_type == 0) or _names_contain(_builtin_namespace_names(0), name) == 0:
        return null()
    n_mro: int = load_i32(cls, PYCLASSOBJECT_N_MRO_OFFSET)
    mro = load_ptr(cls, PYCLASSOBJECT_MRO_OFFSET)
    found = null()
    index: int = 0
    while ptr_is_null(found) != 0 and py_err_occurred() == 0 and index < n_mro:
        m = pcc_gc_load_ptr(cls, ptr_add(mro, index * C_POINTER_SIZE))
        if ptr_is_null(m) == 0:
            namespace_index: int = _builtin_namespace_index(m)
            if namespace_index == 0 or namespace_index == 1:
                found = _builtin_namespace_get(namespace_index, name)
        index = index + 1
    return found


@c_abi_export("py_dict_namespace_member")
def py_dict_namespace_member(name):
    """dict's native member ``name`` as a NEW unbound function, or NULL."""
    if ptr_is_null(name) != 0:
        return null()
    return _builtin_namespace_get(2, name)


@c_abi_export("py_builtin_exception_member")
def py_builtin_exception_member(exc, name):
    """``exc.name`` from the builtin exception namespaces along the class of
    a builtin exception object, bound, or NULL with no exception set."""
    if ptr_is_null(exc) != 0 or ptr_is_null(name) != 0 or _member_tag(exc) != PY_TYPE_EXC:
        return null()
    if _names_contain(_builtin_namespace_names(0), name) == 0:
        return null()
    cls = pcc_gc_load_ptr(exc, ptr_add(exc, 16))
    if ptr_is_null(cls) != 0:
        return null()
    member = _builtin_mro_member(cls, name, 1)
    if ptr_is_null(member) != 0:
        return null()
    bound = py_instance_bind_method(member, exc, name)
    py_decref(member)
    return bound


def _super_mro_index(start_cls, from_cls) -> int:
    """Position of ``from_cls`` in ``start_cls``'s MRO, or -1."""
    n_mro: int = load_i32(start_cls, PYCLASSOBJECT_N_MRO_OFFSET)
    mro = load_ptr(start_cls, PYCLASSOBJECT_MRO_OFFSET)
    found: int = -1
    i: int = 0
    while found < 0 and i < n_mro:
        m = pcc_gc_load_ptr(start_cls, ptr_add(mro, i * C_POINTER_SIZE))
        if ptr_eq(m, from_cls) != 0:
            found = i
        i = i + 1
    return found


def _super_start_type(receiver, from_cls):
    """CPython's super_check: the type the attribute search starts from.

    A class receiver that has ``from_cls`` in its MRO is itself the start type
    (a classmethod's ``cls``); otherwise the receiver's class is. NULL when
    neither holds; the caller raises CPython's TypeError.
    """
    if ptr_is_null(receiver) != 0 or is_tagged_int(receiver) != 0:
        return null()
    if _ptr_is_class(receiver):
        if _super_mro_index(pcc_gc_note_relocation_read(receiver), from_cls) >= 0:
            return receiver
        return null()
    if _ptr_is_instance(receiver):
        return pcc_gc_load_ptr(receiver, ptr_add(receiver, PYINSTANCEOBJECT_CLS_OFFSET))
    return null()


@c_abi_export("py_super_lookup")
def py_super_lookup(start_cls, from_cls, name):
    """The method-table entry ``super(from_cls, obj).name`` resolves to.

    ``start_cls`` is ``type(obj)`` (or ``obj`` for a class receiver). A NULL
    result always carries CPython's exception.
    """
    if not _ptr_is_class(from_cls):
        return _super_raise_not_type(from_cls)
    if ptr_is_null(name) != 0:
        py_raise_owned(py_exc_new(3, cstr("super(): attribute name is NULL")))
        return null()
    if not _ptr_is_class(start_cls):
        return _super_raise_not_subtype(null(), start_cls, from_cls)
    start_cls = pcc_gc_note_relocation_read(start_cls)
    from_cls = pcc_gc_note_relocation_read(from_cls)
    start: int = _super_mro_index(start_cls, from_cls)
    if start < 0:
        return _super_raise_not_subtype(null(), start_cls, from_cls)
    n_mro: int = load_i32(start_cls, PYCLASSOBJECT_N_MRO_OFFSET)
    mro = load_ptr(start_cls, PYCLASSOBJECT_MRO_OFFSET)
    j: int = start + 1
    while j < n_mro:
        m = pcc_gc_load_ptr(start_cls, ptr_add(mro, j * C_POINTER_SIZE))
        if ptr_is_null(m) == 0:
            n_methods: int = load_i32(m, PYCLASSOBJECT_N_METHODS_OFFSET)
            methods = load_ptr(m, PYCLASSOBJECT_METHODS_OFFSET)
            k: int = 0
            while k < n_methods:
                m_off: int = k * PYCLASSMETHOD_SIZE
                m_name = load_ptr(methods, m_off + PYCLASSMETHOD_NAME_OFFSET)
                if _strs_eq(m_name, name) != 0:
                    method_slot = ptr_add(
                        methods,
                        m_off + PYCLASSMETHOD_FUNC_OFFSET,
                    )
                    func = pcc_gc_note_relocation_read(load_ptr(method_slot, 0))
                    store_ptr(method_slot, 0, func)
                    return func
                k = k + 1
        j = j + 1
    return _super_raise_missing(name)


@c_abi_export("py_super_getattr")
def py_super_getattr(start_cls, from_cls, receiver, name):
    """``super(from_cls, receiver).name`` with CPython's descriptor binding.

    The search starts after ``from_cls`` in ``start_cls``'s MRO and reads each
    class's own namespace, so it needs no compile-time knowledge of the bases.
    A function binds to an instance receiver; for a receiver that is the start
    type itself (a classmethod's ``cls``) it stays a plain function; other
    descriptors (classmethod, staticmethod, property, ``__get__``) bind with
    the start type as owner. Returns a NEW reference.
    """
    if not _ptr_is_class(from_cls):
        return _super_raise_not_type(from_cls)
    if ptr_is_null(name) != 0:
        py_raise_owned(py_exc_new(3, cstr("super(): attribute name is NULL")))
        return null()
    if ptr_is_null(start_cls) != 0:
        start_cls = _super_start_type(receiver, from_cls)
    start: int = -1
    if ptr_is_null(start_cls) == 0 and _ptr_is_class(start_cls):
        start_cls = pcc_gc_note_relocation_read(start_cls)
        start = _super_mro_index(start_cls, pcc_gc_note_relocation_read(from_cls))
    if start < 0:
        return _super_raise_not_subtype(receiver, start_cls, from_cls)
    owned_key: int = 0
    key = pcc_cached_name_key(name)
    if ptr_is_null(key) != 0:
        key = py_str_new(name, strlen(name))
        if ptr_is_null(key) != 0:
            return null()
        owned_key = 1
    n_mro: int = load_i32(start_cls, PYCLASSOBJECT_N_MRO_OFFSET)
    mro = load_ptr(start_cls, PYCLASSOBJECT_MRO_OFFSET)
    found = null()
    blocked = null()
    blocked_name = null()
    root = global_load_ptr("py_object_root_cache")
    dict_base: int = load_i32(start_cls, PYOBJECTHEADER_FLAGS_OFFSET) & 4
    j: int = start + 1
    while ptr_is_null(found) != 0 and ptr_is_null(blocked) != 0 and j < n_mro:
        m = pcc_gc_load_ptr(start_cls, ptr_add(mro, j * C_POINTER_SIZE))
        if ptr_is_null(m) == 0:
            attrs = py_class_attrs_dict(m, 0)
            if ptr_is_null(attrs) == 0:
                found = py_dict_get(attrs, key)
            if ptr_is_null(found) != 0 and py_err_occurred() == 0:
                # Builtin bases answer from CPython's namespace; dict is not
                # an MRO entry but sits just before the root.
                table = _builtin_base_names(m)
                if ptr_is_null(table) == 0 and _names_contain(table, name) != 0:
                    namespace_index: int = _builtin_namespace_index(m)
                    if namespace_index >= 0:
                        found = _builtin_namespace_get(namespace_index, name)
                    if ptr_is_null(found) != 0 and py_err_occurred() == 0:
                        blocked = m
                        blocked_name = _super_class_name(m)
                elif ptr_eq(m, root) != 0:
                    if dict_base != 0 and _names_contain(_builtin_type_names(PY_TYPE_DICT), name) != 0:
                        found = _builtin_namespace_get(2, name)
                        if ptr_is_null(found) != 0 and py_err_occurred() == 0:
                            blocked = m
                            blocked_name = cstr("dict")
                    else:
                        found = _object_namespace_get(name)
        j = j + 1
    if owned_key != 0:
        py_decref(key)
    if ptr_is_null(found) != 0:
        if py_err_occurred() != 0:
            return null()
        if ptr_is_null(blocked) == 0:
            return _super_raise_builtin_gap(blocked_name, name)
        return _super_raise_missing(name)
    instance = receiver
    if ptr_eq(receiver, start_cls) != 0:
        instance = null()
    if is_tagged_int(found) == 0 and load_i32(found, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_FUNC:
        if ptr_is_null(instance) != 0:
            return found
        bound = py_instance_bind_method(found, instance, name)
        py_decref(found)
        return bound
    obj = instance
    if ptr_is_null(obj) != 0:
        obj = global_load_ptr("py_None")
    bound = _descriptor_call_get(found, obj, start_cls)
    if ptr_is_null(bound) == 0:
        py_decref(found)
        return bound
    if py_err_occurred() != 0:
        py_decref(found)
        return null()
    return found


pcc_class_retire_metaclass = extern("pcc_class_retire_metaclass", (c_ptr,), c_void)
pcc_class_abort_definition_slots = extern("pcc_class_abort_definition_slots", (c_ptr,), c_void)
define_global_i32("pcc_class_definition_abort_error_map", 1)


@c_abi_export("py_class_abort_definition_slots")
def py_class_abort_definition_slots(class_slot: c_ptr) -> None:
    # Keep exception ownership in the semantic layer. The freestanding entry
    # owns only graph detach and deferred disposal; class_slot is the caller's
    # still-registered root, never a borrowed managed address.
    error = stack_alloc(C_POINTER_SIZE)
    store_ptr(error, 0, null())
    pcc_gc_frame_enter(global_addr("pcc_class_definition_abort_error_map"), error)
    py_tls_exc_swap_slot(error)
    pcc_class_abort_definition_slots(class_slot)
    py_clear_exception()
    py_tls_exc_swap_slot(error)
    pcc_gc_frame_leave(error)


@c_abi_export("py_class_dealloc")
def py_class_dealloc(o) -> None:
    if ptr_is_null(o) != 0:
        return
    attrs = pcc_gc_load_ptr(o, ptr_add(o, PYCLASSOBJECT_ATTRS_OFFSET))
    if ptr_is_null(attrs) == 0:
        store_ptr(o, PYCLASSOBJECT_ATTRS_OFFSET, null())
        py_decref(attrs)
    bases = load_ptr(o, PYCLASSOBJECT_BASES_OFFSET)
    if ptr_is_null(bases) == 0:
        free(bases)
    mro = load_ptr(o, PYCLASSOBJECT_MRO_OFFSET)
    if ptr_is_null(mro) == 0:
        free(mro)
    methods = load_ptr(o, PYCLASSOBJECT_METHODS_OFFSET)
    if ptr_is_null(methods) == 0:
        free(methods)
    field_names = load_ptr(o, PYCLASSOBJECT_FIELD_NAMES_OFFSET)
    if ptr_is_null(field_names) == 0:
        free(field_names)
    # py_class_new copies the name; the static-named builtin object class is
    # immortal and never reaches here.
    name = load_ptr(o, PYCLASSOBJECT_NAME_OFFSET)
    if ptr_is_null(name) == 0:
        free(name)
    # Attribute outcomes are keyed by this address; retire them before a new
    # class can be allocated there or relation cleanup can reenter lookup.
    # The counted relation stays in its authoritative slot across this call.
    _bump_class_attr_cache_epoch()
    # Retire the relation through the existing owning-slot transaction. Its
    # commit detaches before deferred cleanup, without an unrooted raw value
    # spanning a safepoint in the epoch or decref helpers.
    pcc_class_retire_metaclass(o)
    pcc_gc_free_object_memory(o)


def _descriptor_release_slot(owner, offset: int) -> None:
    slot = ptr_add(owner, offset)
    value = pcc_gc_load_ptr(owner, slot)
    store_ptr(owner, offset, null())
    if ptr_is_null(value) == 0:
        py_decref(value)


@c_abi_export("py_descriptor_dealloc")
def py_descriptor_dealloc(o) -> None:
    if ptr_is_null(o) != 0:
        return
    tag: int = load_i32(o, PYOBJECTHEADER_TYPE_TAG_OFFSET)
    if tag == PY_TYPE_PROPERTY:
        _descriptor_release_slot(o, PYPROPERTYOBJECT_FGET_OFFSET)
        _descriptor_release_slot(o, PYPROPERTYOBJECT_FSET_OFFSET)
        _descriptor_release_slot(o, PYPROPERTYOBJECT_FDEL_OFFSET)
    elif tag == PY_TYPE_CLASSMETHOD:
        _descriptor_release_slot(o, PYCLASSMETHODOBJECT_FUNC_OFFSET)
    elif tag == PY_TYPE_STATICMETHOD:
        _descriptor_release_slot(o, PYSTATICMETHODOBJECT_FUNC_OFFSET)
    pcc_gc_free_object_memory(o)


@c_abi_export("py_instance_dealloc")
def py_instance_dealloc(o) -> None:
    if ptr_is_null(o) != 0:
        return
    py_user_del_dispatch(o)
    if load_i64(o, PYOBJECTHEADER_REFCOUNT_OFFSET) > 0:
        py_gc_track(o)
        backend: int = _gc_backend_selected_fast()
        metadata_valid: int = 1
        if backend == 3 or backend == 4:
            if pcc_gc_pointer_is_managed(o) == 0:
                metadata_valid = 0
            elif ptr_is_null(pcc_gc_object_index_find(o)) != 0:
                metadata_valid = 0
        if metadata_valid == 0:
            return
        flags: int = load_i32(o, PYOBJECTHEADER_FLAGS_OFFSET)
        store_i32(o, PYOBJECTHEADER_FLAGS_OFFSET, flags & ~524288)
        return
    # A resurrected instance keeps its live weakrefs. Only the terminal
    # path clears them, after __del__ and before dropping owned fields.
    py_weakref_invalidate(o)
    if _dealloc_ptr_is_instance(o):
        cls = pcc_gc_load_ptr(o, ptr_add(o, PYINSTANCEOBJECT_CLS_OFFSET))
        n_fields: int = load_i32(cls, PYCLASSOBJECT_N_FIELDS_OFFSET)
        fields_base = ptr_add(o, PYINSTANCEOBJECT_FIELDS_OFFSET)
        i: int = 0
        while i < n_fields:
            v = pcc_gc_load_ptr(
                o,
                ptr_add(fields_base, i * C_POINTER_SIZE),
            )
            if ptr_is_null(v) == 0:
                # Null the slot before decref so a finalizer that re-enters
                # py_instance_dealloc (its __del__ arg-tuple holds self and
                # drops self back to 0 on free) reads NULL and does not
                # double-release this field. Mirrors py_class_dealloc.
                store_ptr(fields_base, i * C_POINTER_SIZE, null())
                py_decref(v)
            i = i + 1
        # Detach before deferred decref/finalizers, including slots-only
        # protocol backing stores. The store owner resolves forwarding and
        # preserves the NULL-before-reentrant-cleanup invariant.
        pcc_gc_store_ptr(o, _instance_reserved_owner_slot(o, cls), null())
        payload_slot = _instance_builtin_payload_slot(o, cls)
        if ptr_is_null(payload_slot) == 0:
            pcc_gc_store_ptr(o, payload_slot, null())
    delayed_zpage_note: int = 0
    if _gc_backend_selected_fast() == 4:
        if (load_i32(o, PYOBJECTHEADER_FLAGS_OFFSET) & 65536) != 0:
            delayed_zpage_note = 1
    if delayed_zpage_note == 0:
        pcc_gc_note_object_freeing(o)
        py_gc_untrack(o)
        pcc_gc_free_object_memory(o)
    else:
        py_gc_untrack(o)
        # Probe the address only while this free has not noted ``o``: once
        # it has, its zpage bytes may hold another thread's new object.
        watch = stack_alloc(GC_FREE_WATCH_BYTES)
        pcc_gc_free_watch_begin(watch, o)
        pcc_gc_free_object_memory(o)
        if pcc_gc_free_watch_end(watch) == 0:
            if pcc_gc_pointer_is_managed(o) != 0:
                pcc_gc_note_object_freeing(o)


@c_abi_export("py_dataclass_replace")
def py_dataclass_replace(obj, n_overrides: int, names, values):
    if not _ptr_is_instance(obj):
        return null()
    cls = pcc_gc_load_ptr(obj, ptr_add(obj, PYINSTANCEOBJECT_CLS_OFFSET))
    dst = py_instance_new(cls)
    if ptr_is_null(dst) != 0:
        return null()

    n_fields: int = load_i32(cls, PYCLASSOBJECT_N_FIELDS_OFFSET)
    src_fields = ptr_add(obj, PYINSTANCEOBJECT_FIELDS_OFFSET)
    dst_fields = ptr_add(dst, PYINSTANCEOBJECT_FIELDS_OFFSET)
    i: int = 0
    while i < n_fields:
        v = pcc_gc_load_ptr(obj, ptr_add(src_fields, i * C_POINTER_SIZE))
        if ptr_is_null(v) == 0:
            py_incref(v)
            store_ptr(dst_fields, i * C_POINTER_SIZE, v)
        i = i + 1
    _copy_instance_reserved_owner(obj, dst, cls)
    py_instance_copy_builtin_payload(obj, dst)

    j: int = 0
    while j < n_overrides:
        name_ptr = null()
        if ptr_is_null(names) == 0:
            name_ptr = load_ptr(names, j * C_POINTER_SIZE)
        val_ptr = null()
        if ptr_is_null(values) == 0:
            val_ptr = load_ptr(values, j * C_POINTER_SIZE)
        idx: int = _lookup_field_index(cls, name_ptr)
        if idx < 0:
            py_decref(dst)
            return null()
        # Inline py_instance_set_field — avoid passing idx (which is
        # i64 here, but py_instance_set_field is i32 per ABI).
        if idx < n_fields:
            f_off: int = idx * C_POINTER_SIZE
            old = load_ptr(dst_fields, f_off)
            if ptr_is_null(val_ptr) == 0:
                py_incref(val_ptr)
            store_ptr(dst_fields, f_off, val_ptr)
            if ptr_is_null(old) == 0:
                py_decref(old)
        j = j + 1
    return dst


@c_abi_export("py_dataclass_replace_from_dict")
def py_dataclass_replace_from_dict(obj, overrides):
    if not _ptr_is_instance(obj):
        return null()
    if not _ptr_can_have_header(overrides):
        return null()
    if load_i32(overrides, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_DICT:
        return null()

    cls = pcc_gc_load_ptr(obj, ptr_add(obj, PYINSTANCEOBJECT_CLS_OFFSET))
    dst = py_instance_new(cls)
    if ptr_is_null(dst) != 0:
        return null()

    n_fields: int = load_i32(cls, PYCLASSOBJECT_N_FIELDS_OFFSET)
    src_fields = ptr_add(obj, PYINSTANCEOBJECT_FIELDS_OFFSET)
    dst_fields = ptr_add(dst, PYINSTANCEOBJECT_FIELDS_OFFSET)
    i: int = 0
    while i < n_fields:
        v = pcc_gc_load_ptr(obj, ptr_add(src_fields, i * C_POINTER_SIZE))
        if ptr_is_null(v) == 0:
            py_incref(v)
            store_ptr(dst_fields, i * C_POINTER_SIZE, v)
        i = i + 1

    _copy_instance_reserved_owner(obj, dst, cls)
    py_instance_copy_builtin_payload(obj, dst)

    entries = load_ptr(overrides, PYDICTOBJECT_ENTRIES_OFFSET)
    entries_used: int = load_i64(overrides, PYDICTOBJECT_ENTRIES_USED_OFFSET)
    j: int = 0
    while j < entries_used:
        ent_off: int = j * DICTENTRY_SIZE
        key = load_ptr(entries, ent_off + DICTENTRY_KEY_OFFSET)
        if ptr_is_null(key) == 0:
            val_ptr = load_ptr(entries, ent_off + DICTENTRY_VALUE_OFFSET)
            name_ptr = py_str_utf8(key)
            idx: int = _lookup_field_index(cls, name_ptr)
            if idx < 0:
                py_decref(dst)
                return null()
            if idx < n_fields:
                f_off: int = idx * C_POINTER_SIZE
                old = load_ptr(dst_fields, f_off)
                if ptr_is_null(val_ptr) == 0:
                    py_incref(val_ptr)
                store_ptr(dst_fields, f_off, val_ptr)
                if ptr_is_null(old) == 0:
                    py_decref(old)
        j = j + 1
    return dst


# C3 keeps the existing 16-byte MergeSeq head@8/len@12 state. Sequence
# values are owned managed tuple snapshots instead of raw MRO addresses;
# the reserved pointer word is never carried across a safepoint.


def _class_construct_input_roots(bases, count: int):
    if count <= 0:
        return null()
    if ptr_is_null(bases):
        return null()
    handles = malloc(count * C_POINTER_SIZE)
    if ptr_is_null(handles):
        return null()
    memset(handles, 0, count * C_POINTER_SIZE)
    pcc_py_gc_minor_graph_lock()
    index: int = 0
    while index < count:
        value = pcc_gc_note_relocation_read(load_ptr(bases, index * C_POINTER_SIZE))
        store_ptr(bases, index * C_POINTER_SIZE, value)
        handle = pcc_gc_scheduler_root_register_handle(ptr_add(bases, index * C_POINTER_SIZE))
        if ptr_is_null(handle):
            break
        store_ptr(handles, index * C_POINTER_SIZE, handle)
        index = index + 1
    pcc_py_gc_minor_graph_unlock()
    if index != count:
        _class_construct_input_leave(handles, count)
        return null()
    return handles


def _class_construct_input_leave(handles, count: int) -> None:
    if ptr_is_null(handles):
        return
    index: int = 0
    while index < count:
        handle = load_ptr(handles, index * C_POINTER_SIZE)
        if ptr_is_null(handle) == 0:
            pcc_gc_scheduler_root_unregister_handle(handle)
        index = index + 1
    free(handles)


def _class_construct_tuple_set(roots, target_offset: int, values, value_offset: int, index: int) -> None:
    pcc_py_gc_minor_graph_lock()
    target = pcc_gc_load_ptr(null(), ptr_add(roots, target_offset))
    value = pcc_gc_load_ptr(null(), ptr_add(values, value_offset))
    py_tuple_set_item(target, index, value)
    pcc_py_gc_minor_graph_unlock()


def _class_construct_base(roots, borrowed, index: int) -> None:
    pcc_py_gc_minor_graph_lock()
    bases = pcc_gc_load_ptr(null(), ptr_add(roots, 8))
    value = pcc_gc_load_ptr(bases, ptr_add(bases, 24 + index * C_POINTER_SIZE))
    store_ptr(borrowed, 0, value)
    pcc_py_gc_minor_graph_unlock()
    value = pcc_gc_note_relocation_read(pcc_gc_load_ptr(null(), borrowed))
    store_ptr(borrowed, 0, value)


def _class_construct_snapshot(roots, borrowed, count: int) -> None:
    # The owner supplies the current payload only inside the existing graph
    # lease. The resulting ordinary tuple owns each class entry independently
    # of a parent's movable raw MRO allocation.
    pcc_py_gc_minor_graph_lock()
    base = pcc_gc_load_ptr(null(), borrowed)
    payload = load_ptr(base, PYCLASSOBJECT_MRO_OFFSET)
    snapshot = pcc_gc_load_ptr(null(), ptr_add(roots, 32))
    index: int = 0
    while index < count:
        value = pcc_gc_load_ptr(base, ptr_add(payload, index * C_POINTER_SIZE))
        py_tuple_set_item(snapshot, index, value)
        index = index + 1
    pcc_py_gc_minor_graph_unlock()


def _class_construct_pick(roots, borrowed, sequence: int, index: int) -> None:
    pcc_py_gc_minor_graph_lock()
    owners = pcc_gc_load_ptr(null(), ptr_add(roots, 16))
    items = pcc_gc_load_ptr(owners, ptr_add(owners, 24 + sequence * C_POINTER_SIZE))
    value = pcc_gc_load_ptr(items, ptr_add(items, 24 + index * C_POINTER_SIZE))
    store_ptr(borrowed, 0, value)
    pcc_py_gc_minor_graph_unlock()
    value = pcc_gc_note_relocation_read(pcc_gc_load_ptr(null(), borrowed))
    store_ptr(borrowed, 0, value)


def _class_construct_equal(roots, borrowed, sequence: int, index: int) -> int:
    pcc_py_gc_minor_graph_lock()
    owners = pcc_gc_load_ptr(null(), ptr_add(roots, 16))
    items = pcc_gc_load_ptr(owners, ptr_add(owners, 24 + sequence * C_POINTER_SIZE))
    value = pcc_gc_load_ptr(items, ptr_add(items, 24 + index * C_POINTER_SIZE))
    candidate = pcc_gc_load_ptr(null(), borrowed)
    result: int = ptr_eq(value, candidate)
    pcc_py_gc_minor_graph_unlock()
    return result


def _class_construct_finish(roots, borrowed, success: int):
    prior_pin: int = 0
    if success != 0:
        pcc_gc_publish_initialized(pcc_gc_load_ptr(null(), roots))
        result = pcc_gc_load_ptr(null(), roots)
        prior_pin = load_i32(result, PYOBJECTHEADER_FLAGS_OFFSET) & 64
        pcc_gc_pin(result)
    else:
        value = pcc_gc_load_ptr(null(), roots)
        if ptr_is_null(value) == 0:
            atomic_rmw_i32("and", value, PYOBJECTHEADER_FLAGS_OFFSET, ~PY_FLAG_IMMORTAL, "relaxed")
    store_ptr(borrowed, 0, null())
    store_ptr(borrowed, 8, null())
    index: int = 4
    while index > 0:
        pcc_gc_store_root(ptr_add(roots, index * C_POINTER_SIZE), null())
        index = index - 1
    if success == 0:
        pcc_gc_store_root(roots, null())
    pcc_gc_frame_leave(borrowed)
    pcc_gc_frame_leave(roots)
    if success == 0:
        return null()
    return pcc_gc_take_pinned_slot(roots, prior_pin)


@c_abi_export("py_class_new_runtime_layout")
def py_class_new_runtime_layout(name, bases, n_bases: int):
    """Create a class whose bases were values when its statement ran.

    Its methods were compiled without the bases' field layout, so its own
    attributes live in the instance dictionary. The instance keeps the one
    field layout the bases share, so the bases' compiled methods find their
    fields at the same slots. Bases whose field layouts disagree raise
    CPython's lay-out conflict TypeError.
    """
    layout = null()
    layout_n: int = 0
    index: int = 0
    while index < n_bases:
        base = pcc_gc_load_ptr(null(), ptr_add(bases, index * C_POINTER_SIZE))
        if not _ptr_is_class(base):
            py_raise_owned(py_exc_new(3, cstr("bases must be types")))
            return null()
        count: int = load_i32(base, PYCLASSOBJECT_N_FIELDS_OFFSET)
        if count > 0:
            names = load_ptr(base, PYCLASSOBJECT_FIELD_NAMES_OFFSET)
            shared: int = count
            if layout_n < shared:
                shared = layout_n
            position: int = 0
            while position < shared:
                if _strs_eq(
                    load_ptr(names, position * C_POINTER_SIZE),
                    load_ptr(layout, position * C_POINTER_SIZE),
                ) == 0:
                    py_raise_owned(py_exc_new(3, cstr("multiple bases have instance lay-out conflict")))
                    return null()
                position = position + 1
            if count > layout_n:
                layout = names
                layout_n = count
        index = index + 1
    return py_class_new(name, bases, n_bases, layout, layout_n)


@c_abi_export("py_class_new")
def py_class_new(name, bases, n_bases: int, field_names, n_fields: int):
    # Fixed frames reuse ordinary tuple/slot contracts; class and MergeSeq
    # public layouts are unchanged. Inputs remain borrowed until copied.
    if n_fields < 0:
        n_fields = 0
    handles = _class_construct_input_roots(bases, n_bases)
    roots = stack_alloc(40)
    memset(roots, 0, 40)
    pcc_gc_frame_enter(global_addr("pcc_class_construct_owned_frame_map"), roots)
    borrowed = stack_alloc(16)
    memset(borrowed, 0, 16)
    pcc_gc_frame_enter(global_addr("pcc_class_construct_borrowed_frame_map"), borrowed)
    if n_bases > 0 and ptr_is_null(handles):
        return _class_construct_finish(roots, borrowed, 0)
    base_index: int = 0
    while base_index < n_bases:
        if _class_is_structseq(pcc_gc_load_ptr(null(), ptr_add(bases, base_index * C_POINTER_SIZE))):
            _class_construct_input_leave(handles, n_bases)
            py_raise_owned(py_exc_new(3, cstr("struct sequence type is not an acceptable base type")))
            return _class_construct_finish(roots, borrowed, 0)
        base_index = base_index + 1
    # The class keeps its name for its immortal life, like the bases and
    # field names copied below. type(name, bases, namespace) passes a str's
    # bytes, which die or move with the str.
    own_name = null()
    if ptr_is_null(name) == 0:
        name_size: int = strlen(name) + 1
        own_name = malloc(name_size)
        if ptr_is_null(own_name):
            _class_construct_input_leave(handles, n_bases)
            return _class_construct_finish(roots, borrowed, 0)
        memcpy(own_name, name, name_size)

    c = pcc_gc_alloc(PYCLASSOBJECT_SIZE, PY_TYPE_CLASS, 0)
    store_ptr(roots, 0, c)
    if ptr_is_null(c):
        # No class took ownership of the name copy.
        if ptr_is_null(own_name) == 0:
            free(own_name)
        _class_construct_input_leave(handles, n_bases)
        return _class_construct_finish(roots, borrowed, 0)
    memset(ptr_add(c, PYCLASSOBJECT_NAME_OFFSET), 0, PYCLASSOBJECT_SIZE - PYCLASSOBJECT_NAME_OFFSET)
    store_i64(c, PYOBJECTHEADER_REFCOUNT_OFFSET, 1)
    store_i32(c, PYOBJECTHEADER_TYPE_TAG_OFFSET, PY_TYPE_CLASS)
    store_i32(c, PYOBJECTHEADER_FLAGS_OFFSET, load_i32(c, PYOBJECTHEADER_FLAGS_OFFSET) | PY_FLAG_IMMORTAL)
    store_ptr(c, PYCLASSOBJECT_NAME_OFFSET, own_name)
    store_i32(c, PYCLASSOBJECT_N_BASES_OFFSET, n_bases)
    store_i32(c, PYCLASSOBJECT_N_FIELDS_OFFSET, n_fields)

    owners = py_tuple_new(n_bases)
    store_ptr(roots, 8, owners)
    if ptr_is_null(owners):
        _class_construct_input_leave(handles, n_bases)
        return _class_construct_finish(roots, borrowed, 0)
    index: int = 0
    while index < n_bases:
        store_ptr(borrowed, 0, pcc_gc_load_ptr(null(), ptr_add(bases, index * C_POINTER_SIZE)))
        _class_construct_tuple_set(roots, 8, borrowed, 0, index)
        index = index + 1
    _class_construct_input_leave(handles, n_bases)

    if n_bases > 0:
        copied = malloc(n_bases * C_POINTER_SIZE)
        if ptr_is_null(copied):
            return _class_construct_finish(roots, borrowed, 0)
        pcc_py_gc_minor_graph_lock()
        c = pcc_gc_load_ptr(null(), roots)
        owners = pcc_gc_load_ptr(null(), ptr_add(roots, 8))
        index = 0
        while index < n_bases:
            store_ptr(copied, index * C_POINTER_SIZE, pcc_gc_load_ptr(owners, ptr_add(owners, 24 + index * C_POINTER_SIZE)))
            index = index + 1
        store_ptr(c, PYCLASSOBJECT_BASES_OFFSET, copied)
        pcc_gc_backend4_zpage_register_owner_payload_span(c, copied, n_bases * C_POINTER_SIZE)
        pcc_py_gc_minor_graph_unlock()
    if n_fields > 0 and ptr_is_null(field_names) == 0:
        copied_fields = malloc(n_fields * C_POINTER_SIZE)
        if ptr_is_null(copied_fields):
            return _class_construct_finish(roots, borrowed, 0)
        index = 0
        while index < n_fields:
            store_ptr(copied_fields, index * C_POINTER_SIZE, load_ptr(field_names, index * C_POINTER_SIZE))
            index = index + 1
        c = pcc_gc_load_ptr(null(), roots)
        store_ptr(c, PYCLASSOBJECT_FIELD_NAMES_OFFSET, copied_fields)
    user_tag: int = _alloc_user_tag()
    if user_tag < PY_TYPE_NONE:
        _class_require_result(
            null(), cstr("class type tag allocation"),
            cstr("user class type tag space exhausted"),
        )
        return _class_construct_finish(roots, borrowed, 0)
    c = pcc_gc_load_ptr(null(), roots)
    store_i32(c, PYCLASSOBJECT_TYPE_TAG_ALLOC_OFFSET, user_tag)
    n_slots: int = n_fields + 1
    inst_size: int = PYINSTANCEOBJECT_SIZE + n_slots * C_POINTER_SIZE
    if inst_size > 0x7FFFFFFF:
        inst_size = 0x7FFFFFFF
    store_i32(c, PYCLASSOBJECT_INSTANCE_SIZE_OFFSET, inst_size)

    tail_len: int = 0
    if n_bases > 0:
        nseqs: int = n_bases + 1
        seqs = malloc(nseqs * 16)
        if ptr_is_null(seqs):
            return _class_construct_finish(roots, borrowed, 0)
        memset(seqs, 0, nseqs * 16)
        sequences = py_tuple_new(nseqs)
        store_ptr(roots, 16, sequences)
        if ptr_is_null(sequences):
            free(seqs)
            return _class_construct_finish(roots, borrowed, 0)
        cap_total: int = n_bases
        index = 0
        while index < n_bases:
            _class_construct_base(roots, borrowed, index)
            base = pcc_gc_load_ptr(null(), borrowed)
            count: int = load_i32(base, PYCLASSOBJECT_N_MRO_OFFSET)
            snapshot = py_tuple_new(count)
            store_ptr(roots, 32, snapshot)
            if ptr_is_null(snapshot):
                free(seqs)
                return _class_construct_finish(roots, borrowed, 0)
            _class_construct_snapshot(roots, borrowed, count)
            _class_construct_tuple_set(roots, 16, roots, 32, index)
            pcc_gc_store_root(ptr_add(roots, 32), null())
            store_i32(seqs, index * 16 + 12, count)
            cap_total = cap_total + count
            index = index + 1
        _class_construct_tuple_set(roots, 16, roots, 8, n_bases)
        store_i32(seqs, n_bases * 16 + 12, n_bases)
        if cap_total <= 0:
            cap_total = 1
        accumulator = py_tuple_new(cap_total)
        store_ptr(roots, 24, accumulator)
        if ptr_is_null(accumulator):
            free(seqs)
            return _class_construct_finish(roots, borrowed, 0)
        merge_done: int = 0
        while merge_done == 0:
            any_remaining: int = 0
            index = 0
            while index < nseqs:
                if load_i32(seqs, index * 16 + 8) < load_i32(seqs, index * 16 + 12):
                    any_remaining = 1
                index = index + 1
            if any_remaining == 0:
                merge_done = 1
            else:
                found: int = 0
                pick: int = 0
                while pick < nseqs and found == 0:
                    head: int = load_i32(seqs, pick * 16 + 8)
                    if head < load_i32(seqs, pick * 16 + 12):
                        _class_construct_pick(roots, borrowed, pick, head)
                        acceptable: int = 1
                        check: int = 0
                        while check < nseqs:
                            if check != pick:
                                tail: int = load_i32(seqs, check * 16 + 8) + 1
                                limit: int = load_i32(seqs, check * 16 + 12)
                                while tail < limit:
                                    if _class_construct_equal(roots, borrowed, check, tail) != 0:
                                        acceptable = 0
                                        tail = limit
                                    tail = tail + 1
                            check = check + 1
                        if acceptable != 0:
                            found = 1
                    pick = pick + 1
                if found == 0:
                    free(seqs)
                    return _class_construct_finish(roots, borrowed, 0)
                _class_construct_tuple_set(roots, 24, borrowed, 0, tail_len)
                tail_len = tail_len + 1
                index = 0
                while index < nseqs:
                    head = load_i32(seqs, index * 16 + 8)
                    if head < load_i32(seqs, index * 16 + 12):
                        if _class_construct_equal(roots, borrowed, index, head) != 0:
                            store_i32(seqs, index * 16 + 8, head + 1)
                    index = index + 1
        free(seqs)

    root = _object_root()
    store_ptr(borrowed, 8, root)
    append_root: int = 0
    if n_bases == 0:
        if ptr_eq(pcc_gc_load_ptr(null(), roots), pcc_gc_load_ptr(null(), ptr_add(borrowed, 8))) == 0:
            append_root = 1
    mro_len: int = 1 + tail_len + append_root
    mro = malloc(mro_len * C_POINTER_SIZE)
    if ptr_is_null(mro):
        return _class_construct_finish(roots, borrowed, 0)
    pcc_py_gc_minor_graph_lock()
    c = pcc_gc_load_ptr(null(), roots)
    store_ptr(mro, 0, c)
    accumulator = pcc_gc_load_ptr(null(), ptr_add(roots, 24))
    index = 0
    while index < tail_len:
        value = pcc_gc_load_ptr(accumulator, ptr_add(accumulator, 24 + index * C_POINTER_SIZE))
        store_ptr(mro, (1 + index) * C_POINTER_SIZE, value)
        index = index + 1
    if append_root != 0:
        store_ptr(mro, (mro_len - 1) * C_POINTER_SIZE, pcc_gc_load_ptr(null(), ptr_add(borrowed, 8)))
    store_ptr(c, PYCLASSOBJECT_MRO_OFFSET, mro)
    store_i32(c, PYCLASSOBJECT_N_MRO_OFFSET, mro_len)
    pcc_gc_backend4_zpage_register_owner_payload_span(c, mro, mro_len * C_POINTER_SIZE)
    pcc_py_gc_minor_graph_unlock()
    c = pcc_gc_load_ptr(null(), roots)
    str_payload: int = py_class_is_str_subclass(c)
    tuple_payload: int = py_class_is_tuple_subclass(c)
    if str_payload != 0 and tuple_payload != 0:
        py_raise_owned(py_exc_new(3, cstr("multiple bases have instance lay-out conflict")))
        return _class_construct_finish(roots, borrowed, 0)
    if str_payload != 0 or tuple_payload != 0:
        # One extra traced owner; __dict__ stays at the existing offset.
        store_i32(c, PYCLASSOBJECT_INSTANCE_SIZE_OFFSET, inst_size + C_POINTER_SIZE)
    # Every class owns its default documentation entry. Otherwise ordinary
    # MRO lookup incorrectly inherits a base's docstring (or reports absence).
    # The completed C3 scratch owner is no longer needed; reuse its rooted slot.
    doc_slot = ptr_add(roots, 4 * C_POINTER_SIZE)
    pcc_gc_store_root(doc_slot, global_load_ptr("py_None"))
    if py_class_write_namespace_slots(roots, cstr("__doc__"), doc_slot, 0) != 0:
        return _class_construct_finish(roots, borrowed, 0)
    return _class_construct_finish(roots, borrowed, 1)


# type(name, bases, namespace) protects every operand before allocations.
_CLASS_OBJECTS_NAME = 1
_CLASS_OBJECTS_BASES = 2
_CLASS_OBJECTS_NAMESPACE = 3
_CLASS_OBJECTS_BASE_SNAPSHOT = 4
_CLASS_OBJECTS_BASE_ITEM = 5
_CLASS_OBJECTS_RESULT = 12

define_global_i32("pcc_class_objects_borrowed_map", -3)
define_global_i32("pcc_class_objects_result_map", 1)


def _class_new_from_objects_body(slots: c_ptr, tokens: c_ptr) -> int:
    name_obj = load_ptr(slots, _CLASS_OBJECTS_NAME * C_POINTER_SIZE)
    if not _ptr_can_have_header(name_obj) or load_i32(name_obj, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_STR:
        py_raise_owned(py_exc_new(3, cstr("type.__new__() argument 1 must be str")))
        return -1
    bases_obj = load_ptr(slots, _CLASS_OBJECTS_BASES * C_POINTER_SIZE)
    n_bases: int = 0
    bases_kind: int = 0
    if ptr_is_null(bases_obj) != 0 or ptr_eq(bases_obj, global_load_ptr("py_None")) != 0:
        n_bases = 0
    elif _ptr_can_have_header(bases_obj) and load_i32(bases_obj, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_TUPLE:
        n_bases = py_tuple_len(bases_obj)
        bases_kind = 1
    elif _ptr_can_have_header(bases_obj) and load_i32(bases_obj, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_LIST:
        n_bases = py_list_len(bases_obj)
        bases_kind = 2
    else:
        py_raise_owned(py_exc_new(3, cstr("type.__new__() argument 2 must be tuple")))
        return -1
    if n_bases < 0 or n_bases > 2147483647:
        py_raise_owned(py_exc_new(3, cstr("too many base classes")))
        return -1
    store_ptr(slots, _CLASS_OBJECTS_BASE_SNAPSHOT * C_POINTER_SIZE, py_tuple_new(n_bases))
    if _special_adopt(slots, tokens, _CLASS_OBJECTS_BASE_SNAPSHOT) != 0:
        return -1
    if ptr_is_null(load_ptr(slots, _CLASS_OBJECTS_BASE_SNAPSHOT * C_POINTER_SIZE)) != 0:
        return _special_error(cstr("class bases snapshot allocation failed"))
    index: int = 0
    while index < n_bases:
        if bases_kind == 1:
            store_ptr(slots, _CLASS_OBJECTS_BASE_ITEM * C_POINTER_SIZE,
                py_tuple_get(load_ptr(slots, _CLASS_OBJECTS_BASES * C_POINTER_SIZE), index))
        else:
            store_ptr(slots, _CLASS_OBJECTS_BASE_ITEM * C_POINTER_SIZE,
                py_list_get(load_ptr(slots, _CLASS_OBJECTS_BASES * C_POINTER_SIZE), index))
        if _special_adopt(slots, tokens, _CLASS_OBJECTS_BASE_ITEM) != 0:
            return -1
        if not _ptr_is_class(load_ptr(slots, _CLASS_OBJECTS_BASE_ITEM * C_POINTER_SIZE)):
            py_raise_owned(py_exc_new(3, cstr("type.__new__() base must be class")))
            return -1
        py_tuple_set_item(load_ptr(slots, _CLASS_OBJECTS_BASE_SNAPSHOT * C_POINTER_SIZE), index,
                         load_ptr(slots, _CLASS_OBJECTS_BASE_ITEM * C_POINTER_SIZE))
        if py_err_occurred() != 0:
            return -1
        _special_drop(slots, tokens, _CLASS_OBJECTS_BASE_ITEM)
        index = index + 1
    # The class keeps its name as a C string, so a NUL would truncate it;
    # CPython rejects that name here, after the argument checks above.
    if strlen(py_str_utf8(load_ptr(slots, _CLASS_OBJECTS_NAME * C_POINTER_SIZE))) != load_i64(
            load_ptr(slots, _CLASS_OBJECTS_NAME * C_POINTER_SIZE), PYSTROBJECT_BYTE_LEN_OFFSET):
        py_raise_owned(py_exc_new(2, cstr("type name must not contain null characters")))
        return -1
    # Both name bytes and the tuple's inline item array have independent
    # address leases. Its traced elements remain authoritative to C3.
    store_ptr(slots, _CLASS_OBJECTS_RESULT * C_POINTER_SIZE,
        py_class_new_abi(py_str_utf8(load_ptr(slots, _CLASS_OBJECTS_NAME * C_POINTER_SIZE)),
            ptr_add(load_ptr(slots, _CLASS_OBJECTS_BASE_SNAPSHOT * C_POINTER_SIZE), PYTUPLEOBJECT_ITEMS_OFFSET),
            n_bases, null(), 0))
    if _special_adopt(slots, tokens, _CLASS_OBJECTS_RESULT) != 0:
        return -1
    if ptr_is_null(load_ptr(slots, _CLASS_OBJECTS_RESULT * C_POINTER_SIZE)) != 0:
        return _special_error(cstr("class construction failed"))
    status: int = _class_apply_namespace_bound(ptr_add(slots, _CLASS_OBJECTS_RESULT * C_POINTER_SIZE),
                                              ptr_add(slots, _CLASS_OBJECTS_NAMESPACE * C_POINTER_SIZE), 0)
    if status != 0:
        # This fresh class has not escaped. Classes remain immortal, so a
        # decref alone would leave the partially installed namespace alive.
        py_class_abort_definition_slots(ptr_add(slots, _CLASS_OBJECTS_RESULT * C_POINTER_SIZE))
    return status


@c_abi_export("py_class_new_from_objects")
def py_class_new_from_objects(name_obj, bases_obj, namespace):
    borrowed = stack_alloc(3 * C_POINTER_SIZE)
    store_ptr(borrowed, 0, name_obj)
    store_ptr(borrowed, C_POINTER_SIZE, bases_obj)
    store_ptr(borrowed, 2 * C_POINTER_SIZE, namespace)
    pcc_gc_frame_enter(global_addr("pcc_class_objects_borrowed_map"), borrowed)
    output = stack_alloc(C_POINTER_SIZE)
    store_ptr(output, 0, null())
    pcc_gc_frame_enter(global_addr("pcc_class_objects_result_map"), output)
    slots = stack_alloc(14 * C_POINTER_SIZE)
    tokens = stack_alloc(14 * C_POINTER_SIZE)
    handles = stack_alloc(14 * C_POINTER_SIZE)
    count: int = _special_open(slots, tokens, handles)
    status: int = -1
    suspended: int = 0
    if count == 14:
        py_tls_exc_swap_slot(slots)
        suspended = 1
        status = _special_copy(slots, tokens, _CLASS_OBJECTS_NAME, borrowed, 1)
        if status == 0:
            status = _special_copy(slots, tokens, _CLASS_OBJECTS_BASES, ptr_add(borrowed, C_POINTER_SIZE), 1)
        if status == 0:
            status = _special_copy(slots, tokens, _CLASS_OBJECTS_NAMESPACE, ptr_add(borrowed, 2 * C_POINTER_SIZE), 1)
        if status == 0:
            status = _class_new_from_objects_body(slots, tokens)
        if status == 0:
            status = _special_publish(slots, tokens, output)
    if status != 0:
        _special_error(cstr("class construction failed without an exception"))
    _special_close(slots, tokens, handles, count, suspended)
    prior: int = 0
    value = load_ptr(output, 0)
    if ptr_is_null(value) == 0:
        prior = load_i32(value, PYOBJECTHEADER_FLAGS_OFFSET) & 64
        pcc_gc_pin(value)
    pcc_gc_frame_leave(output)
    pcc_gc_frame_leave(borrowed)
    return pcc_gc_take_pinned_slot(output, prior)


@c_abi_export("py_class_mark_slots_only")
def py_class_mark_slots_only(cls) -> None:
    if ptr_is_null(cls) != 0:
        return
    flags: int = load_i32(cls, PYOBJECTHEADER_FLAGS_OFFSET)
    store_i32(cls, PYOBJECTHEADER_FLAGS_OFFSET, flags | 2)


# Set once at class creation on a class whose ancestors include a builtin
# exception class; the class call then keeps its constructor arguments as
# ``args`` (BaseException.__new__) by testing this bit. Bit 24 is a function
# role elsewhere and free on classes. Mirrored in py_obj_ops_dispatch.py.
_EXCEPTION_SUBCLASS_CLASS_FLAG = 16777216


@c_abi_export("py_class_mark_exception_subclass")
def py_class_mark_exception_subclass(cls) -> None:
    if ptr_is_null(cls) != 0:
        return
    atomic_rmw_i32("or", cls, PYOBJECTHEADER_FLAGS_OFFSET, _EXCEPTION_SUBCLASS_CLASS_FLAG, "relaxed")


@c_abi_export("py_class_mark_dict_subclass")
def py_class_mark_dict_subclass(cls) -> None:
    # Set PY_CLASS_FLAG_DICT_SUBCLASS (bit 2, value 4): this class subclasses
    # the builtin ``dict``, so dict-inherited item storage / methods are routed
    # to a backing dict in the instance's __dict__ slot (see py_protocol.c).
    if ptr_is_null(cls) != 0:
        return
    flags: int = load_i32(cls, PYOBJECTHEADER_FLAGS_OFFSET)
    store_i32(cls, PYOBJECTHEADER_FLAGS_OFFSET, flags | 4)


@c_abi_export("py_instance_method_call_direct")
def py_instance_method_call_direct(inst, name, full_args):
    """Call ``inst``'s ``name`` method with ``full_args``, which already holds
    ``inst`` at index 0.

    The general protocol for ``self.foo(a, b)`` on a method some subclass
    overrides costs one MRO walk for ``__getattribute__``, one for
    ``__getattr__``, one field-index scan and one for ``foo`` itself, then
    allocates a bound-method object with a two-element captures tuple; the
    caller allocates an argument tuple; ``_instance_bound_method_entry``
    allocates a *second* tuple to prepend ``self``; and all four objects are
    then refcounted down again.  On pcc1 compiling a module that protocol is
    about 40% of self time, with another 13% in the refcounting and
    deallocation it creates.

    Only use the direct path when the *runtime* receiver still has ordinary
    method lookup. Instance/class mutation can invalidate a compile-time
    class-graph proof, so every uncertain shape uses the general protocol.
    """
    if not _ptr_is_instance(inst):
        return _instance_method_call_fallback(inst, name, full_args)
    cls = pcc_gc_load_ptr(inst, ptr_add(inst, PYINSTANCEOBJECT_CLS_OFFSET))
    if ptr_is_null(cls) != 0:
        return _instance_method_call_fallback(inst, name, full_args)
    if ptr_is_null(_class_lookup_in_mro(cls, cstr("__getattribute__"))) == 0:
        return _instance_method_call_fallback(inst, name, full_args)
    class_attr = _class_attr_lookup_in_mro(cls, name)
    if ptr_is_null(class_attr) == 0:
        py_decref(class_attr)
        return _instance_method_call_fallback(inst, name, full_args)
    if _lookup_field_index(cls, name) >= 0:
        return _instance_method_call_fallback(inst, name, full_args)
    dyn_slot = _dynamic_attr_slot(inst)
    if ptr_is_null(dyn_slot) == 0:
        dyn = pcc_gc_load_ptr(inst, dyn_slot)
        if ptr_is_null(dyn) == 0:
            return _instance_method_call_fallback(inst, name, full_args)
    func = _class_lookup_in_mro(cls, name)
    if ptr_is_null(func) != 0:
        return _instance_method_call_fallback(inst, name, full_args)
    if load_i32(func, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_FUNC:
        return _instance_method_call_fallback(inst, name, full_args)
    return py_func_call_kwargs(func, full_args, null())


@c_abi_export("py_obj_load_method")
def py_obj_load_method(obj, name, out_self):
    """`obj.name` for an immediate positional call, without a bound object.

    When the cached resolution proves `name` is a plain class function that
    nothing on the instance shadows, return that function and store `obj` in
    `*out_self`; `py_obj_call_method` then calls it with `obj` first, which is
    what the bound method would do.  Otherwise `*out_self` stays NULL and the
    ordinary attribute is returned.  Resolution happens here, before the
    caller evaluates arguments, exactly like the attribute load it replaces.
    """
    store_ptr(out_self, 0, null())
    if _ptr_is_instance(obj):
        cls = pcc_gc_load_ptr(obj, ptr_add(obj, PYINSTANCEOBJECT_CLS_OFFSET))
        if ptr_is_null(cls) == 0:
            # A method entry is recorded only past the `__getattribute__`
            # check, and any class mutation retires it.
            entry = _attr_res_find(cls, name, _ATTR_RES_METHOD)
            if ptr_is_null(entry) == 0:
                # Method entries hold only verified function objects.
                func = load_ptr(entry, 16)
                dyn_slot = _dynamic_attr_slot(obj)
                if ptr_is_null(dyn_slot) != 0 or ptr_is_null(
                    pcc_gc_load_ptr(obj, dyn_slot)
                ) != 0:
                    py_incref(func)
                    store_ptr(out_self, 0, obj)
                    return func
    return py_obj_getattr(obj, name)


@c_abi_export("py_obj_call_method")
def py_obj_call_method(method, self_obj, args):
    """Call what `py_obj_load_method` returned, with positional `args`."""
    if ptr_is_null(self_obj) != 0:
        return py_obj_call(method, args, null())
    n: int = 0
    if ptr_is_null(args) == 0:
        n = py_tuple_len(args)
    full_args = py_tuple_new(n + 1)
    if ptr_is_null(full_args) != 0:
        return _class_require_result(
            null(),
            cstr("py_tuple_new"),
            cstr("method call argument tuple allocation failed"),
        )
    py_tuple_set_item(full_args, 0, self_obj)
    i: int = 0
    while i < n:
        item = py_tuple_get(args, i)
        py_tuple_set_item(full_args, i + 1, item)
        if ptr_is_null(item) == 0:
            py_decref(item)
        i = i + 1
    out = py_func_call_kwargs(method, full_args, null())
    py_decref(full_args)
    return out


@c_abi_export("py_obj_call_method_kwargs")
def py_obj_call_method_kwargs(method, self_obj, args, kwargs):
    """`py_obj_call_method` with a keywords dict (or NULL).

    `obj.name(a, key=b)` used to bind a method object -- a fresh captures
    tuple and a copied signature per call -- only to call it once.
    """
    if ptr_is_null(self_obj) != 0:
        return py_obj_call(method, args, kwargs)
    n: int = 0
    if ptr_is_null(args) == 0:
        n = py_tuple_len(args)
    full_args = py_tuple_new(n + 1)
    if ptr_is_null(full_args) != 0:
        return _class_require_result(
            null(),
            cstr("py_tuple_new"),
            cstr("method call argument tuple allocation failed"),
        )
    py_tuple_set_item(full_args, 0, self_obj)
    i: int = 0
    while i < n:
        item = py_tuple_get(args, i)
        py_tuple_set_item(full_args, i + 1, item)
        if ptr_is_null(item) == 0:
            py_decref(item)
        i = i + 1
    out = py_func_call_kwargs(method, full_args, kwargs)
    py_decref(full_args)
    return out


def _instance_method_call_fallback(inst, name, full_args):
    """The general attribute protocol, rebuilding the argument tuple.

    ``full_args`` carries the receiver at index 0 because the fast path wants
    it there; the bound method the general path produces supplies its own, so
    the tail has to be copied out.  This runs only for receivers the fast path
    could not resolve, so the extra tuple is not on any hot path.
    """
    n: int = py_tuple_len(full_args)
    tail = py_tuple_new(n - 1)
    if ptr_is_null(tail) != 0:
        return null()
    i: int = 1
    while i < n:
        item = py_tuple_get(full_args, i)
        py_tuple_set_item(tail, i - 1, item)
        if ptr_is_null(item) == 0:
            py_decref(item)
        i = i + 1
    method = py_obj_getattr(inst, name)
    if ptr_is_null(method) != 0:
        py_decref(tail)
        return null()
    out = py_obj_call(method, tail, null())
    py_decref(method)
    py_decref(tail)
    return out

pcc_gc_frame_enter = extern("pcc_gc_frame_enter", (c_ptr, c_ptr), c_void)

pcc_gc_frame_leave = extern("pcc_gc_frame_leave", (c_ptr,), c_void)

pcc_gc_take_pinned_slot = extern("pcc_gc_take_pinned_slot", (c_ptr, c_int64), c_ptr)

pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)

pcc_gc_unpin = extern("pcc_gc_unpin", (c_ptr,), c_void)

define_global_i32("pcc_bound_callback_frame_map", 11)

pcc_gc_store_root = extern("pcc_gc_store_root", (c_ptr, c_ptr), c_void)


# Slot-based call boundary. Every managed scratch slot is registered EMPTY
# before reading any incoming operand. Native code pointers live exclusively
# in the untraced lookup record, never in this managed root array.
pcc_platform_abort = extern("pcc_platform_abort", (), c_void)


def _special_open(slots, tokens, handles) -> int:
    memset(slots, 0, 14 * C_POINTER_SIZE)
    memset(tokens, 0, 14 * C_POINTER_SIZE)
    memset(handles, 0, 14 * C_POINTER_SIZE)
    count: int = 0
    while count < 14:
        handle = pcc_gc_scheduler_root_register_handle(ptr_add(slots, count * C_POINTER_SIZE))
        if ptr_is_null(handle) != 0:
            return count
        store_ptr(handles, count * C_POINTER_SIZE, handle)
        count = count + 1
    return count


def _special_error(message) -> int:
    py_runtime_error_if_unset(cstr("slot-based special call"), message)
    return -1


def _special_copy(slots, tokens, index: int, source, borrowed: int) -> int:
    destination = ptr_add(slots, index * C_POINTER_SIZE)
    token: int = 0
    if ptr_is_null(source) == 0:
        if borrowed != 0:
            token = pcc_gc_root_copy_borrowed_lease(destination, source)
        else:
            token = pcc_gc_root_copy_lease(destination, source)
    if token < 0:
        # Lookup can hold an outer graph transaction. Allocate the diagnostic
        # only in the public caller after that transaction has been released.
        return -1
    store_i64(tokens, index * C_POINTER_SIZE, token)
    return 0


def _special_adopt(slots, tokens, index: int) -> int:
    # The producer must have stored its owned result directly into this
    # already registered empty slot, with no intervening call or poll.
    slot = ptr_add(slots, index * C_POINTER_SIZE)
    token: int = pcc_gc_foreign_lease_acquire(slot)
    if token < 0:
        return _special_error(cstr("special-call result lease acquisition failed"))
    store_i64(tokens, index * C_POINTER_SIZE, token)
    pcc_py_gc_minor_graph_lock()
    pcc_gc_note_slot_write_barrier(null(), slot, load_ptr(slot, 0))
    pcc_py_gc_minor_graph_unlock()
    return 0


def _special_drop(slots, tokens, index: int) -> None:
    slot = ptr_add(slots, index * C_POINTER_SIZE)
    token: int = load_i64(tokens, index * C_POINTER_SIZE)
    if pcc_gc_foreign_lease_release(slot, token) != 0:
        # Returning would leave a counted lease referring to dead stack
        # storage. This is an internal token invariant, not a Python error.
        pcc_platform_abort()
        return
    store_i64(tokens, index * C_POINTER_SIZE, 0)
    pcc_gc_store_root(slot, null())


def _special_close(slots, tokens, handles, count: int, suspended: int) -> None:
    if suspended != 0:
        # Keep callback failure independent of the exception that was pending
        # on entry, and protect it from operand finalizers during cleanup.
        py_tls_exc_swap_slot(ptr_add(slots, 13 * C_POINTER_SIZE))
    index: int = 12
    while index > 0:
        _special_drop(slots, tokens, index)
        index = index - 1
    if suspended != 0:
        py_clear_exception()
        if ptr_is_null(load_ptr(slots, 13 * C_POINTER_SIZE)) == 0:
            pcc_gc_store_root(slots, null())
            py_clear_exception()
            py_tls_exc_swap_slot(ptr_add(slots, 13 * C_POINTER_SIZE))
        else:
            py_tls_exc_swap_slot(slots)
    index = 0
    while index < count:
        pcc_gc_scheduler_root_unregister_handle(load_ptr(handles, index * C_POINTER_SIZE))
        index = index + 1


def _special_name_equal(key, name, length: int) -> int:
    if ptr_is_null(key) != 0 or is_tagged_int(key) != 0:
        return 0
    if load_i32(key, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_STR:
        return 0
    if load_i64(key, PYSTROBJECT_BYTE_LEN_OFFSET) != length:
        return 0
    index: int = 0
    while index < length:
        if load_i8(key, PYSTROBJECT_DATA_OFFSET + index) != load_i8(name, index):
            return 0
        index = index + 1
    return 1


def _special_native_instance(value) -> bool:
    if ptr_is_null(value) != 0 or is_tagged_int(value) != 0:
        return False
    # Extension tags occupy the same high range, but extension instances do
    # not have PyInstanceObject.cls. Their call protocol belongs to the C-API
    # kernel, not this native class/descriptor lookup.
    tag: int = load_i32(value, PYOBJECTHEADER_TYPE_TAG_OFFSET)
    if pcc_capi_is_cext_type_tag(tag) != 0:
        return False
    return _ptr_is_instance(value)


def _special_lookup_locked(cls, name, record, start_index: int = 0):
    """One C3 pass: namespace then native table at each individual owner.

    record.kind: 0 absent, 1 owning namespace slot, 2 borrowed managed native
    metadata, 3 raw native address. Only record.raw may contain a code pointer.
    No key allocation, descriptor callback or foreign invocation occurs here.
    """
    store_i64(record, 0, 0)
    store_ptr(record, C_POINTER_SIZE, null())
    length: int = strlen(name)
    count: int = load_i32(cls, PYCLASSOBJECT_N_MRO_OFFSET)
    mro = load_ptr(cls, PYCLASSOBJECT_MRO_OFFSET)
    index: int = start_index
    while index < count:
        owner = pcc_gc_load_ptr(cls, ptr_add(mro, index * C_POINTER_SIZE))
        if ptr_is_null(owner) == 0:
            attrs = pcc_gc_resolve_root_slot_unlocked(ptr_add(owner, PYCLASSOBJECT_ATTRS_OFFSET), 0)
            if ptr_is_null(attrs) == 0:
                entries = load_ptr(attrs, PYDICTOBJECT_ENTRIES_OFFSET)
                used: int = load_i64(attrs, PYDICTOBJECT_ENTRIES_USED_OFFSET)
                entry: int = 0
                while entry < used:
                    offset: int = entry * DICTENTRY_SIZE
                    # Dictionary entries own their keys. GC3 forwarding must
                    # transfer that slot's reference, not just heal a borrowed
                    # raw copy or overwrite it with the uncounted read barrier.
                    key = pcc_gc_resolve_root_slot_unlocked(ptr_add(entries, offset + DICTENTRY_KEY_OFFSET), 0)
                    if _special_name_equal(key, name, length) != 0:
                        slot = ptr_add(entries, offset + DICTENTRY_VALUE_OFFSET)
                        if ptr_is_null(load_ptr(slot, 0)) == 0:
                            store_i64(record, 0, 1)
                            return slot
                    entry = entry + 1
            methods = load_ptr(owner, PYCLASSOBJECT_METHODS_OFFSET)
            method_count: int = load_i32(owner, PYCLASSOBJECT_N_METHODS_OFFSET)
            entry = 0
            while entry < method_count:
                offset = entry * PYCLASSMETHOD_SIZE
                method_name = load_ptr(methods, offset + PYCLASSMETHOD_NAME_OFFSET)
                if _strs_eq(method_name, name) != 0:
                    slot = ptr_add(methods, offset + PYCLASSMETHOD_FUNC_OFFSET)
                    value = load_ptr(slot, 0)
                    if ptr_is_null(value) == 0:
                        if is_tagged_int(value) != 0 or _ptr_can_have_header(value):
                            store_i64(record, 0, 2)
                            return slot
                        store_i64(record, 0, 3)
                        store_ptr(record, C_POINTER_SIZE, value)
                        return null()
                entry = entry + 1
        index = index + 1
    return null()


def _special_tuple_item(slots, tokens, index: int, tuple_index: int, item: int) -> int:
    # The tuple has a counted address lease; its inline slot address remains
    # valid while copy_lease acquires its own graph transaction.
    owner = load_ptr(slots, tuple_index * C_POINTER_SIZE)
    source = ptr_add(owner, PYTUPLEOBJECT_ITEMS_OFFSET + item * C_POINTER_SIZE)
    return _special_copy(slots, tokens, index, source, 0)


def _special_tuple_new(slots, tokens, index: int, length: int) -> int:
    store_ptr(slots, index * C_POINTER_SIZE, py_tuple_new(length))
    if _special_adopt(slots, tokens, index) != 0:
        return -1
    if ptr_is_null(load_ptr(slots, index * C_POINTER_SIZE)) != 0:
        return _special_error(cstr("special-call argument tuple allocation failed"))
    return 0


def _special_prepend(slots, tokens) -> int:
    # slots6 binding receiver, 2 original args, 7 full args, 8 current item.
    count: int = 0
    if ptr_is_null(load_ptr(slots, 2 * C_POINTER_SIZE)) == 0:
        count = py_tuple_len(load_ptr(slots, 2 * C_POINTER_SIZE))
    if _special_tuple_new(slots, tokens, 7, count + 1) != 0:
        return -1
    py_tuple_set_item(load_ptr(slots, 7 * C_POINTER_SIZE), 0, load_ptr(slots, 6 * C_POINTER_SIZE))
    index: int = 0
    while index < count:
        if _special_tuple_item(slots, tokens, 8, 2, index) != 0:
            return -1
        py_tuple_set_item(load_ptr(slots, 7 * C_POINTER_SIZE), index + 1, load_ptr(slots, 8 * C_POINTER_SIZE))
        _special_drop(slots, tokens, 8)
        if py_err_occurred() != 0:
            return -1
        index = index + 1
    return 0


def _special_validate_arguments(slots) -> int:
    args = load_ptr(slots, 2 * C_POINTER_SIZE)
    if ptr_is_null(args) == 0:
        if is_tagged_int(args) != 0 or load_i32(args, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_TUPLE:
            py_raise_owned(py_exc_new(3, cstr("call positional arguments must be a tuple")))
            return -1
    kwargs = load_ptr(slots, 3 * C_POINTER_SIZE)
    if ptr_is_null(kwargs) == 0 and ptr_eq(kwargs, global_load_ptr("py_None")) == 0:
        if is_tagged_int(kwargs) != 0 or load_i32(kwargs, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_DICT:
            py_raise_owned(py_exc_new(3, cstr("call keyword arguments must be a dict")))
            return -1
    return py_call_validate_kwargs(kwargs)


def _special_call_native(slots, tokens, native) -> int:
    count: int = 0
    if ptr_is_null(load_ptr(slots, 2 * C_POINTER_SIZE)) == 0:
        count = py_tuple_len(load_ptr(slots, 2 * C_POINTER_SIZE))
    kwargs = load_ptr(slots, 3 * C_POINTER_SIZE)
    if ptr_is_null(kwargs) == 0 and ptr_eq(kwargs, global_load_ptr("py_None")) == 0:
        if py_dict_len(kwargs) != 0:
            py_raise_owned(py_exc_new(3, cstr("native special method does not accept keyword arguments")))
            return -1
    if count > 3:
        py_raise_owned(py_exc_new(3, cstr("native special method accepts at most three arguments")))
        return -1
    index: int = 0
    while index < count:
        if _special_tuple_item(slots, tokens, 6 + index, 2, index) != 0:
            return -1
        index = index + 1
    if count == 0:
        store_ptr(slots, 12 * C_POINTER_SIZE, call_ptr1(native, load_ptr(slots, C_POINTER_SIZE)))
    elif count == 1:
        store_ptr(slots, 12 * C_POINTER_SIZE, call_ptr2(native, load_ptr(slots, C_POINTER_SIZE), load_ptr(slots, 6 * C_POINTER_SIZE)))
    elif count == 2:
        store_ptr(slots, 12 * C_POINTER_SIZE, call_ptr3(native, load_ptr(slots, C_POINTER_SIZE), load_ptr(slots, 6 * C_POINTER_SIZE), load_ptr(slots, 7 * C_POINTER_SIZE)))
    else:
        store_ptr(slots, 12 * C_POINTER_SIZE, call_ptr4(native, load_ptr(slots, C_POINTER_SIZE), load_ptr(slots, 6 * C_POINTER_SIZE), load_ptr(slots, 7 * C_POINTER_SIZE), load_ptr(slots, 8 * C_POINTER_SIZE)))
    return _special_adopt(slots, tokens, 12)


def _special_bind_and_call(slots, tokens, record) -> int:
    if load_i64(record, 0) == 3:
        return _special_call_native(slots, tokens, load_ptr(record, C_POINTER_SIZE))
    descriptor = load_ptr(slots, 5 * C_POINTER_SIZE)
    tag: int = -1
    if is_tagged_int(descriptor) == 0:
        tag = load_i32(descriptor, PYOBJECTHEADER_TYPE_TAG_OFFSET)
    prepend: int = 0
    if tag == PY_TYPE_FUNC:
        if _special_copy(slots, tokens, 9, ptr_add(slots, 5 * C_POINTER_SIZE), 0) != 0:
            return -1
        if _special_copy(slots, tokens, 6, ptr_add(slots, C_POINTER_SIZE), 0) != 0:
            return -1
        prepend = 1
    elif tag == PY_TYPE_STATICMETHOD or tag == PY_TYPE_CLASSMETHOD:
        offset: int = PYSTATICMETHODOBJECT_FUNC_OFFSET
        if tag == PY_TYPE_CLASSMETHOD:
            offset = PYCLASSMETHODOBJECT_FUNC_OFFSET
        source = ptr_add(load_ptr(slots, 5 * C_POINTER_SIZE), offset)
        result: int = _special_copy(slots, tokens, 9, source, 0)
        if result != 0:
            return -1
        if tag == PY_TYPE_CLASSMETHOD:
            if _special_copy(slots, tokens, 6, ptr_add(slots, 4 * C_POINTER_SIZE), 0) != 0:
                return -1
            prepend = 1
    elif tag == PY_TYPE_PROPERTY:
        source = ptr_add(load_ptr(slots, 5 * C_POINTER_SIZE), PYPROPERTYOBJECT_FGET_OFFSET)
        result = _special_copy(slots, tokens, 11, source, 0)
        if result != 0:
            return -1
        if ptr_is_null(load_ptr(slots, 11 * C_POINTER_SIZE)) != 0:
            py_raise_owned(py_exc_new(6, cstr("unreadable attribute")))
            return -1
        if _special_tuple_new(slots, tokens, 10, 1) != 0:
            return -1
        py_tuple_set_item(load_ptr(slots, 10 * C_POINTER_SIZE), 0, load_ptr(slots, C_POINTER_SIZE))
        if py_obj_call_slots(ptr_add(slots, 11 * C_POINTER_SIZE), ptr_add(slots, 10 * C_POINTER_SIZE), null(), ptr_add(slots, 9 * C_POINTER_SIZE)) != 0:
            return -1
        if _special_adopt(slots, tokens, 9) != 0:
            return -1
    else:
        if _special_tuple_new(slots, tokens, 10, 2) != 0:
            return -1
        py_tuple_set_item(load_ptr(slots, 10 * C_POINTER_SIZE), 0, load_ptr(slots, C_POINTER_SIZE))
        py_tuple_set_item(load_ptr(slots, 10 * C_POINTER_SIZE), 1, load_ptr(slots, 4 * C_POINTER_SIZE))
        get_handled = stack_alloc(8)
        store_i64(get_handled, 0, 0)
        if py_obj_special_call_slots(ptr_add(slots, 5 * C_POINTER_SIZE), cstr("__get__"), ptr_add(slots, 10 * C_POINTER_SIZE), null(), ptr_add(slots, 9 * C_POINTER_SIZE), get_handled) != 0:
            return -1
        if load_i64(get_handled, 0) == 0:
            if _special_copy(slots, tokens, 9, ptr_add(slots, 5 * C_POINTER_SIZE), 0) != 0:
                return -1
        elif _special_adopt(slots, tokens, 9) != 0:
            return -1
    args_slot = ptr_add(slots, 2 * C_POINTER_SIZE)
    if prepend != 0:
        if _special_prepend(slots, tokens) != 0:
            return -1
        args_slot = ptr_add(slots, 7 * C_POINTER_SIZE)
    if py_obj_call_slots(ptr_add(slots, 9 * C_POINTER_SIZE), args_slot, ptr_add(slots, 3 * C_POINTER_SIZE), ptr_add(slots, 12 * C_POINTER_SIZE)) != 0:
        return -1
    return _special_adopt(slots, tokens, 12)


def _special_publish(slots, tokens, result_slot) -> int:
    source = ptr_add(slots, 12 * C_POINTER_SIZE)
    if ptr_is_null(load_ptr(source, 0)) != 0:
        return _special_error(cstr("special-call callback returned NULL without an exception"))
    if py_err_occurred() != 0:
        return -1
    if pcc_gc_root_move(result_slot, source) != 0:
        return _special_error(cstr("special-call result destination must be empty"))
    token: int = load_i64(tokens, 12 * C_POINTER_SIZE)
    store_i64(tokens, 12 * C_POINTER_SIZE, 0)
    if pcc_gc_foreign_lease_release(result_slot, token) != 0:
        pcc_platform_abort()
        return -1
    return 0


@c_abi_export("py_obj_special_call_slots")
def py_obj_special_call_slots(receiver_slot, name, args_slot, kwargs_slot, result_slot, handled) -> int:
    """Call a named type-level special method from authoritative owning roots.

    Status 0 means absent or success; handled distinguishes them. It is zero
    only on absence, one on selection or any failure. result_slot receives one
    owned result on success. NULL args/kwargs slot pointers denote emptiness.
    """
    if ptr_is_null(handled) == 0:
        store_i64(handled, 0, 1)
    if ptr_is_null(receiver_slot) != 0 or ptr_is_null(result_slot) != 0 or ptr_is_null(name) != 0:
        return _special_error(cstr("special call requires source and output root slots"))
    slots = stack_alloc(14 * C_POINTER_SIZE)
    tokens = stack_alloc(14 * C_POINTER_SIZE)
    handles = stack_alloc(14 * C_POINTER_SIZE)
    count: int = _special_open(slots, tokens, handles)
    status: int = -1
    suspended: int = 0
    if count == 14:
        py_tls_exc_swap_slot(slots)
        suspended = 1
        status = _special_copy(slots, tokens, 1, receiver_slot, 0)
        if status == 0:
            status = _special_copy(slots, tokens, 2, args_slot, 0)
        if status == 0:
            status = _special_copy(slots, tokens, 3, kwargs_slot, 0)
        if status == 0:
            status = _special_validate_arguments(slots)
        if status == 0:
            record = stack_alloc(2 * C_POINTER_SIZE)
            memset(record, 0, 2 * C_POINTER_SIZE)
            status = _special_resolve_type(slots, tokens, 1, 4)
            if status == 0:
                status = _special_select_owned(slots, tokens, 4, name, 5, record)
            if status == 0:
                if load_i64(record, 0) == 0:
                    if ptr_is_null(handled) == 0:
                        store_i64(handled, 0, 0)
                else:
                    status = _special_bind_and_call(slots, tokens, record)
                    if status == 0:
                        status = _special_publish(slots, tokens, result_slot)
    else:
        _special_error(cstr("special-call root registration failed"))
    if status < 0:
        _special_error(cstr("special-call binding failed without an exception"))
    _special_close(slots, tokens, handles, count, suspended)
    return status


@c_abi_export("py_obj_call_slots")
def py_obj_call_slots(callable_slot, args_slot, kwargs_slot, result_slot) -> int:
    """Slot entry for generic calls; raw py_obj_call remains compatibility ABI."""
    if ptr_is_null(callable_slot) != 0 or ptr_is_null(result_slot) != 0:
        return _special_error(cstr("call requires source and output root slots"))
    slots = stack_alloc(14 * C_POINTER_SIZE)
    tokens = stack_alloc(14 * C_POINTER_SIZE)
    handles = stack_alloc(14 * C_POINTER_SIZE)
    count: int = _special_open(slots, tokens, handles)
    status: int = -1
    suspended: int = 0
    if count == 14:
        py_tls_exc_swap_slot(slots)
        suspended = 1
        status = _special_copy(slots, tokens, 1, callable_slot, 0)
        if status == 0:
            status = _special_copy(slots, tokens, 2, args_slot, 0)
        if status == 0:
            status = _special_copy(slots, tokens, 3, kwargs_slot, 0)
        if status == 0:
            status = _special_validate_arguments(slots)
        if status == 0:
            selected = stack_alloc(8)
            store_i64(selected, 0, 0)
            callable_obj = load_ptr(slots, C_POINTER_SIZE)
            tag: int = -1
            if ptr_is_null(callable_obj) == 0 and is_tagged_int(callable_obj) == 0:
                tag = load_i32(callable_obj, PYOBJECTHEADER_TYPE_TAG_OFFSET)
            if tag == PY_TYPE_STATICMETHOD:
                source = ptr_add(callable_obj, PYSTATICMETHODOBJECT_FUNC_OFFSET)
                status = _special_copy(slots, tokens, 9, source, 0)
                if status == 0:
                    status = py_obj_call_slots(ptr_add(slots, 9 * C_POINTER_SIZE), ptr_add(slots, 2 * C_POINTER_SIZE), ptr_add(slots, 3 * C_POINTER_SIZE), ptr_add(slots, 12 * C_POINTER_SIZE))
                store_i64(selected, 0, 1)
            elif _ptr_is_class(callable_obj) and py_obj_call_context_is_deferred() != 0:
                status = py_obj_call_slots_sync(ptr_add(slots, C_POINTER_SIZE), ptr_add(slots, 2 * C_POINTER_SIZE), ptr_add(slots, 3 * C_POINTER_SIZE), ptr_add(slots, 12 * C_POINTER_SIZE))
                store_i64(selected, 0, 1)
            elif _ptr_is_class(callable_obj) or _special_native_instance(callable_obj):
                status = py_obj_special_call_slots(ptr_add(slots, C_POINTER_SIZE), cstr("__call__"), ptr_add(slots, 2 * C_POINTER_SIZE), ptr_add(slots, 3 * C_POINTER_SIZE), ptr_add(slots, 12 * C_POINTER_SIZE), selected)
            if status == 0 and load_i64(selected, 0) == 0:
                store_ptr(slots, 12 * C_POINTER_SIZE, py_obj_call_default(load_ptr(slots, C_POINTER_SIZE), load_ptr(slots, 2 * C_POINTER_SIZE), load_ptr(slots, 3 * C_POINTER_SIZE)))
            if status == 0:
                status = _special_adopt(slots, tokens, 12)
            if status == 0:
                status = _special_publish(slots, tokens, result_slot)
    else:
        _special_error(cstr("call root registration failed"))
    if status < 0:
        _special_error(cstr("call failed without an exception"))
    _special_close(slots, tokens, handles, count, suspended)
    return status


@c_abi_export("py_obj_special_present")
def py_obj_special_present(value, name) -> int:
    """Type-level presence only, for an independently address-leased value.

    The caller retains an owning source slot and its counted address lease.
    This graph transaction reads namespace/method slots without binding a
    descriptor or invoking user code. A present None descriptor still counts
    as present; raw native method addresses are never traced as objects.
    """
    if ptr_is_null(value) != 0 or is_tagged_int(value) != 0 or ptr_is_null(name) != 0:
        return 0
    found: int = 0
    record = stack_alloc(2 * C_POINTER_SIZE)
    pcc_py_gc_minor_graph_lock()
    owner_source = null()
    if _ptr_is_class(value):
        owner_source = ptr_add(value, PYCLASSOBJECT_METACLASS_OFFSET)
    elif _special_native_instance(value):
        owner_source = ptr_add(value, PYINSTANCEOBJECT_CLS_OFFSET)
    if ptr_is_null(owner_source) == 0:
        owner = pcc_gc_resolve_root_slot_unlocked(owner_source, 0)
        if ptr_is_null(owner) == 0:
            _special_lookup_locked(owner, name, record)
            if load_i64(record, 0) != 0:
                found = 1
    pcc_py_gc_minor_graph_unlock()
    return found


pcc_class_namespace_acquire_slots = extern(
    "pcc_class_namespace_acquire_slots", (c_ptr, c_ptr, c_ptr, c_ptr, c_ptr), c_int64
)
pcc_class_namespace_install_slots = extern(
    "pcc_class_namespace_install_slots", (c_ptr, c_ptr, c_ptr, c_ptr, c_ptr, c_ptr, c_ptr), c_int64
)
pcc_gc_store_ptr_plan_init = extern(
    "pcc_gc_store_ptr_plan_init", (c_ptr, c_ptr, c_int64), c_void
)
pcc_gc_store_ptr_plan_finish = extern("pcc_gc_store_ptr_plan_finish", (c_ptr,), c_void)
py_dict_namespace_set_slots = extern(
    "py_dict_namespace_set_slots", (c_ptr, c_ptr, c_ptr, c_ptr), c_int64
)
py_dict_namespace_del_slots = extern(
    "py_dict_namespace_del_slots", (c_ptr, c_ptr, c_ptr), c_int64
)

# Owning namespace writer. No graph lock spans a
# semantic function call or local integer assignment in this module.
_CLASS_WRITE_CLASS = 1
_CLASS_WRITE_VALUE = 2
_CLASS_WRITE_DICT = 3
_CLASS_WRITE_KEY = 4
_CLASS_WRITE_CREATED = 5
_CLASS_WRITE_SLOT_COUNT = 14
_CLASS_WRITE_CONTEXT_OWNER_SLOT = 0
_CLASS_WRITE_CONTEXT_NAME = 1
_CLASS_WRITE_CONTEXT_COUNT = 2


def _class_write_acquire_namespace(slots: c_ptr, tokens: c_ptr, create: int) -> int:
    output = ptr_add(slots, _CLASS_WRITE_DICT * C_POINTER_SIZE)
    token_slot = ptr_add(tokens, _CLASS_WRITE_DICT * C_POINTER_SIZE)
    class_slot = ptr_add(slots, _CLASS_WRITE_CLASS * C_POINTER_SIZE)
    plan = stack_alloc(256)
    prepared = stack_alloc(C_POINTER_SIZE)
    status: int = pcc_class_namespace_acquire_slots(class_slot, output, token_slot, plan, prepared)
    if load_i64(prepared, 0) != 0:
        pcc_gc_root_copy_lease_finish(plan)
    if status != 0:
        return status
    if create == 0:
        return 0
    created_slot = ptr_add(slots, _CLASS_WRITE_CREATED * C_POINTER_SIZE)
    store_ptr(created_slot, 0, py_dict_new())
    if _special_adopt(slots, tokens, _CLASS_WRITE_CREATED) != 0:
        return -1
    if ptr_is_null(load_ptr(created_slot, 0)) != 0:
        return _special_error(cstr("class namespace allocation failed"))
    write_plan = stack_alloc(128)
    pcc_gc_store_ptr_plan_init(write_plan, load_ptr(class_slot, 0), pcc_gc_backend())
    status = pcc_class_namespace_install_slots(class_slot, created_slot, output, token_slot, write_plan, plan, prepared)
    pcc_gc_store_ptr_plan_finish(write_plan)
    if load_i64(prepared, 0) != 0:
        pcc_gc_root_copy_lease_finish(plan)
    _special_drop(slots, tokens, _CLASS_WRITE_CREATED)
    return status


def _class_write_namespace_body(slots: c_ptr, tokens: c_ptr, name: c_ptr, value_slot: c_ptr, remove: int) -> int:
    class_slot = ptr_add(slots, _CLASS_WRITE_CLASS * C_POINTER_SIZE)
    if remove == 0:
        if _special_copy(slots, tokens, _CLASS_WRITE_VALUE, value_slot, 0) != 0:
            return -1
    key_slot = ptr_add(slots, _CLASS_WRITE_KEY * C_POINTER_SIZE)
    store_ptr(key_slot, 0, py_str_new(name, strlen(name)))
    if _special_adopt(slots, tokens, _CLASS_WRITE_KEY) != 0:
        return -1
    if ptr_is_null(load_ptr(key_slot, 0)) != 0:
        return _special_error(cstr("class namespace key allocation failed"))
    return _class_write_namespace_commit(slots, tokens, name, remove)


def _class_write_namespace_commit(slots: c_ptr, tokens: c_ptr, name: c_ptr, remove: int) -> int:
    class_slot = ptr_add(slots, _CLASS_WRITE_CLASS * C_POINTER_SIZE)
    key_slot = ptr_add(slots, _CLASS_WRITE_KEY * C_POINTER_SIZE)
    # Discovery is conservative: a failed installation may leave this hint
    # positive, but no successful __del__ installation may leave it zero.
    if ptr_is_null(name) == 0 and _strs_eq(name, cstr("__del__")) != 0 and remove == 0:
        _note_class_defines_del()
    context = stack_alloc(_CLASS_WRITE_CONTEXT_COUNT * C_POINTER_SIZE)
    store_ptr(context, _CLASS_WRITE_CONTEXT_OWNER_SLOT * C_POINTER_SIZE, class_slot)
    store_ptr(context, _CLASS_WRITE_CONTEXT_NAME * C_POINTER_SIZE, name)
    while True:
        acquired: int = _class_write_acquire_namespace(slots, tokens, 0 if remove != 0 else 1)
        if acquired < 0:
            return _special_error(cstr("class namespace owner acquisition failed"))
        if acquired == 0:
            return 1
        if remove != 0:
            status: int = py_dict_namespace_del_slots(
                ptr_add(slots, _CLASS_WRITE_DICT * C_POINTER_SIZE), key_slot, context,
            )
        else:
            status = py_dict_namespace_set_slots(
                ptr_add(slots, _CLASS_WRITE_DICT * C_POINTER_SIZE), key_slot,
                ptr_add(slots, _CLASS_WRITE_VALUE * C_POINTER_SIZE), context,
            )
        if status != -2:
            return status
        # A validated receiver's namespace identity changed before commit.
        # Reacquire its current owner; neither a stale dictionary nor an
        # invalid receiver is silently treated as a successful mutation.
        _special_drop(slots, tokens, _CLASS_WRITE_DICT)


@c_abi_export("py_class_write_namespace_slots")
def py_class_write_namespace_slots(class_slot: c_ptr, name: c_ptr, value_slot: c_ptr, remove: int) -> int:
    """Internal owning-slot writer; 0 success, 1 absent delete, -1 error.

    Inputs are registered owning roots. name is an immutable C string whose
    caller-owned storage remains valid throughout the call. Raw callbacks are
    never supplied as values; their publication has a separate native ABI.
    """
    if ptr_is_null(class_slot) != 0 or ptr_is_null(name) != 0:
        return _special_error(cstr("class namespace write requires owner and name"))
    if remove == 0 and ptr_is_null(value_slot) != 0:
        return _special_error(cstr("class namespace write requires a value owner"))
    slots = stack_alloc(_CLASS_WRITE_SLOT_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_CLASS_WRITE_SLOT_COUNT * C_POINTER_SIZE)
    handles = stack_alloc(_CLASS_WRITE_SLOT_COUNT * C_POINTER_SIZE)
    count: int = _special_open(slots, tokens, handles)
    status: int = -1
    suspended: int = 0
    if count == _CLASS_WRITE_SLOT_COUNT:
        py_tls_exc_swap_slot(slots)
        suspended = 1
        status = _special_copy(slots, tokens, _CLASS_WRITE_CLASS, class_slot, 0)
        if status == 0:
            status = _class_write_namespace_body(slots, tokens, name, value_slot, remove)
    if status < 0:
        _special_error(cstr("class namespace write failed without an exception"))
    _special_close(slots, tokens, handles, count, suspended)
    return status


@c_abi_export("py_class_write_namespace_key_slots")
def py_class_write_namespace_key_slots(class_slot: c_ptr, key_slot: c_ptr, value_slot: c_ptr) -> int:
    """Install an actual dictionary key while retaining each owning source.

    Class construction copies the dictionary, including arbitrary non-string
    keys and embedded NULs. Such keys have no native C-string method alias.
    """
    if ptr_is_null(class_slot) != 0 or ptr_is_null(key_slot) != 0 or ptr_is_null(value_slot) != 0:
        return _special_error(cstr("class namespace item requires owner, key and value slots"))
    slots = stack_alloc(_CLASS_WRITE_SLOT_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_CLASS_WRITE_SLOT_COUNT * C_POINTER_SIZE)
    handles = stack_alloc(_CLASS_WRITE_SLOT_COUNT * C_POINTER_SIZE)
    count: int = _special_open(slots, tokens, handles)
    status: int = -1
    suspended: int = 0
    if count == _CLASS_WRITE_SLOT_COUNT:
        py_tls_exc_swap_slot(slots)
        suspended = 1
        status = _special_copy(slots, tokens, _CLASS_WRITE_CLASS, class_slot, 0)
        if status == 0:
            status = _special_copy(slots, tokens, _CLASS_WRITE_KEY, key_slot, 0)
        if status == 0:
            status = _special_copy(slots, tokens, _CLASS_WRITE_VALUE, value_slot, 0)
        if status == 0:
            name = null()
            key = load_ptr(slots, _CLASS_WRITE_KEY * C_POINTER_SIZE)
            if _ptr_can_have_header(key) and load_i32(key, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_STR:
                candidate = py_str_utf8(key)
                if strlen(candidate) == load_i64(key, PYSTROBJECT_BYTE_LEN_OFFSET):
                    name = candidate
            status = _class_write_namespace_commit(slots, tokens, name, 0)
    if status < 0:
        _special_error(cstr("class namespace item write failed without an exception"))
    _special_close(slots, tokens, handles, count, suspended)
    return status


# Read-only metadata selection shares the namespace writer's class/dict/key
# slots. The original class is separate from the current immutable MRO entry.
_CLASS_READ_CURRENT = 1
_CLASS_READ_ORIGINAL = 2
_CLASS_READ_DICT = 3
_CLASS_READ_KEY = 4
_CLASS_READ_ABSENT = 5
_CLASS_READ_RESULT = 12


def _class_read_namespace_body(slots: c_ptr, tokens: c_ptr, name: c_ptr, result_slot: c_ptr) -> int:
    original = load_ptr(slots, _CLASS_READ_ORIGINAL * C_POINTER_SIZE)
    if not _ptr_is_class(original):
        return _special_error(cstr("class namespace read requires a class"))
    store_ptr(slots, _CLASS_READ_KEY * C_POINTER_SIZE, py_str_new(name, strlen(name)))
    if _class_apply_adopt(slots, tokens, _CLASS_READ_KEY) != 0:
        return -1
    count: int = load_i32(load_ptr(slots, _CLASS_READ_ORIGINAL * C_POINTER_SIZE), PYCLASSOBJECT_N_MRO_OFFSET)
    index: int = 0
    while index < count:
        # The leased class owns immutable MRO storage. Acquire the current
        # class independently before selecting its owning dictionary slot.
        mro = load_ptr(load_ptr(slots, _CLASS_READ_ORIGINAL * C_POINTER_SIZE), PYCLASSOBJECT_MRO_OFFSET)
        if _special_copy(slots, tokens, _CLASS_READ_CURRENT, ptr_add(mro, index * C_POINTER_SIZE), 1) != 0:
            return -1
        acquired: int = _class_write_acquire_namespace(slots, tokens, 0)
        if acquired < 0:
            return -1
        if acquired != 0:
            if py_dict_get_default_slots(ptr_add(slots, _CLASS_READ_DICT * C_POINTER_SIZE),
                    ptr_add(slots, _CLASS_READ_KEY * C_POINTER_SIZE),
                    ptr_add(slots, _CLASS_READ_ABSENT * C_POINTER_SIZE),
                    ptr_add(slots, _CLASS_READ_RESULT * C_POINTER_SIZE)) != 0:
                return -1
            if ptr_is_null(load_ptr(slots, _CLASS_READ_RESULT * C_POINTER_SIZE)) == 0:
                if _special_adopt(slots, tokens, _CLASS_READ_RESULT) != 0:
                    return -1
                return _special_publish(slots, tokens, result_slot)
        _special_drop(slots, tokens, _CLASS_READ_DICT)
        _special_drop(slots, tokens, _CLASS_READ_CURRENT)
        index = index + 1
    return 0


@c_abi_export("py_class_read_namespace_slots")
def py_class_read_namespace_slots(class_slot: c_ptr, name: c_ptr, result_slot: c_ptr) -> int:
    """Own raw class metadata selected through the MRO, without descriptors.

    The caller owns class_slot and the empty registered result_slot. An absent
    entry returns success with NULL; a Python None value remains a real owner.
    This is an internal namespace API, not Python attribute lookup.
    """
    if ptr_is_null(class_slot) != 0 or ptr_is_null(name) != 0 or ptr_is_null(result_slot) != 0:
        return _special_error(cstr("class namespace read requires owner, name and result slots"))
    slots = stack_alloc(_CLASS_WRITE_SLOT_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_CLASS_WRITE_SLOT_COUNT * C_POINTER_SIZE)
    handles = stack_alloc(_CLASS_WRITE_SLOT_COUNT * C_POINTER_SIZE)
    count: int = _special_open(slots, tokens, handles)
    status: int = -1
    suspended: int = 0
    if count == _CLASS_WRITE_SLOT_COUNT:
        py_tls_exc_swap_slot(slots)
        suspended = 1
        status = _special_copy(slots, tokens, _CLASS_READ_ORIGINAL, class_slot, 0)
        if status == 0:
            status = _class_read_namespace_body(slots, tokens, name, result_slot)
    if status < 0:
        _special_error(cstr("class namespace read failed without an exception"))
    _special_close(slots, tokens, handles, count, suspended)
    return status


# The constructor reuses the fourteen-slot special-call frame. Slot zero and
# thirteen retain its existing exception protocol; names make semantic owners
# explicit while preserving the shared _special_publish result position.
_STR_NEW_CLASS = 1
_STR_NEW_ARGS = 2
_STR_NEW_KWARGS = 3
_STR_NEW_OBJECT = 4
_STR_NEW_ENCODING = 5
_STR_NEW_ERRORS = 6
_STR_NEW_TEXT = 7
_STR_NEW_KEY = 8
_STR_NEW_KEY_VALUE = 9
_STR_NEW_PAYLOAD_CLASS = 10
_STR_NEW_RESULT = 12

define_global_i32("pcc_str_new_borrowed_map", -3)
define_global_i32("pcc_str_new_result_map", 1)
py_obj_str = extern("py_obj_str", (c_ptr,), c_ptr)
py_str_byte_len = extern("py_str_byte_len", (c_ptr,), c_int64)
py_bytes_decode_with_encoding = extern("py_bytes_decode_with_encoding", (c_ptr, c_ptr, c_ptr), c_ptr)


def _str_new_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    # The ordinary native binder resolved cls, *args and **kwargs. args is
    # leased by that binder throughout the raw constructor call.
    return py_str_subclass_new(
        load_ptr(args, PYTUPLEOBJECT_ITEMS_OFFSET),
        load_ptr(args, PYTUPLEOBJECT_ITEMS_OFFSET + C_POINTER_SIZE),
        load_ptr(args, PYTUPLEOBJECT_ITEMS_OFFSET + 2 * C_POINTER_SIZE),
    )


def _str_new_exact_argument(slots, tokens, index: int) -> int:
    value = load_ptr(slots, index * C_POINTER_SIZE)
    if ptr_is_null(value) != 0:
        return 0
    if load_i32(value, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_STR:
        return 0
    # Codec helpers use the exact-string raw view ABI. Snapshot a subtype
    # under its backing-owner lease instead of passing the wrapper through.
    store_ptr(slots, _STR_NEW_KEY_VALUE * C_POINTER_SIZE, py_str_exact_copy(value))
    if _special_adopt(slots, tokens, _STR_NEW_KEY_VALUE) != 0:
        return -1
    if ptr_is_null(load_ptr(slots, _STR_NEW_KEY_VALUE * C_POINTER_SIZE)) != 0:
        return -1
    _special_drop(slots, tokens, index)
    status: int = _special_copy(slots, tokens, index, ptr_add(slots, _STR_NEW_KEY_VALUE * C_POINTER_SIZE), 0)
    _special_drop(slots, tokens, _STR_NEW_KEY_VALUE)
    return status


def _str_new_body(slots, tokens) -> int:
    cls = load_ptr(slots, _STR_NEW_CLASS * C_POINTER_SIZE)
    if py_class_is_str_subclass(cls) == 0:
        py_raise_owned(py_exc_new(3, cstr("str.__new__ requires a str subtype")))
        return -1
    if _special_validate_arguments(slots) != 0:
        return -1
    nargs: int = 0
    args = load_ptr(slots, _STR_NEW_ARGS * C_POINTER_SIZE)
    if ptr_is_null(args) == 0:
        nargs = py_tuple_len(args)
    if nargs > 3:
        py_raise_owned(py_exc_new(3, cstr("str() takes at most 3 arguments")))
        return -1
    index: int = 0
    while index < nargs:
        if _special_tuple_item(slots, tokens, _STR_NEW_OBJECT + index, _STR_NEW_ARGS, index) != 0:
            return -1
        index = index + 1
    kwargs = load_ptr(slots, _STR_NEW_KWARGS * C_POINTER_SIZE)
    if ptr_is_null(kwargs) == 0 and ptr_eq(kwargs, global_load_ptr("py_None")) == 0:
        matched: int = 0
        index = 0
        while index < 3:
            name = cstr("object")
            if index == 1:
                name = cstr("encoding")
            elif index == 2:
                name = cstr("errors")
            store_ptr(slots, _STR_NEW_KEY * C_POINTER_SIZE, py_str_new(name, strlen(name)))
            if _special_adopt(slots, tokens, _STR_NEW_KEY) != 0:
                return -1
            store_ptr(slots, _STR_NEW_KEY_VALUE * C_POINTER_SIZE, py_dict_get(kwargs, load_ptr(slots, _STR_NEW_KEY * C_POINTER_SIZE)))
            if _special_adopt(slots, tokens, _STR_NEW_KEY_VALUE) != 0:
                return -1
            if ptr_is_null(load_ptr(slots, _STR_NEW_KEY_VALUE * C_POINTER_SIZE)) == 0:
                matched = matched + 1
                if index < nargs:
                    py_raise_owned(py_exc_new(3, cstr("str() argument supplied by name and position")))
                    return -1
                if _special_copy(slots, tokens, _STR_NEW_OBJECT + index, ptr_add(slots, _STR_NEW_KEY_VALUE * C_POINTER_SIZE), 0) != 0:
                    return -1
            _special_drop(slots, tokens, _STR_NEW_KEY_VALUE)
            _special_drop(slots, tokens, _STR_NEW_KEY)
            index = index + 1
        if matched != py_dict_len(kwargs):
            py_raise_owned(py_exc_new(3, cstr("invalid keyword argument for str()")))
            return -1
    value = load_ptr(slots, _STR_NEW_OBJECT * C_POINTER_SIZE)
    encoding = load_ptr(slots, _STR_NEW_ENCODING * C_POINTER_SIZE)
    errors = load_ptr(slots, _STR_NEW_ERRORS * C_POINTER_SIZE)
    if ptr_is_null(encoding) == 0 or ptr_is_null(errors) == 0:
        if ptr_is_null(encoding) == 0:
            if ptr_is_null(py_str_payload(encoding)) != 0:
                py_raise_owned(py_exc_new(3, cstr("str() encoding must be str")))
                return -1
        if ptr_is_null(errors) == 0:
            if ptr_is_null(py_str_payload(errors)) != 0:
                py_raise_owned(py_exc_new(3, cstr("str() errors must be str")))
                return -1
        if _str_new_exact_argument(slots, tokens, _STR_NEW_ENCODING) != 0:
            return -1
        if _str_new_exact_argument(slots, tokens, _STR_NEW_ERRORS) != 0:
            return -1
        encoding = load_ptr(slots, _STR_NEW_ENCODING * C_POINTER_SIZE)
        errors = load_ptr(slots, _STR_NEW_ERRORS * C_POINTER_SIZE)
        if ptr_is_null(value) != 0:
            store_ptr(slots, _STR_NEW_TEXT * C_POINTER_SIZE, py_str_new(cstr(""), 0))
        else:
            store_ptr(slots, _STR_NEW_TEXT * C_POINTER_SIZE, py_bytes_decode_with_encoding(value, encoding, errors))
    elif ptr_is_null(value) != 0:
        store_ptr(slots, _STR_NEW_TEXT * C_POINTER_SIZE, py_str_new(cstr(""), 0))
    else:
        store_ptr(slots, _STR_NEW_TEXT * C_POINTER_SIZE, py_obj_str(value))
    if _special_adopt(slots, tokens, _STR_NEW_TEXT) != 0:
        return -1
    if py_err_occurred() != 0:
        return -1
    text = load_ptr(slots, _STR_NEW_TEXT * C_POINTER_SIZE)
    payload = py_str_payload(text)
    if ptr_is_null(payload) != 0:
        py_raise_owned(py_exc_new(3, cstr("__str__ returned non-string")))
        return -1
    # A __str__ override can itself return a subclass. The immutable storage
    # owner must be an exact native string, not a hidden second user object.
    if ptr_eq(payload, text) == 0:
        store_ptr(slots, _STR_NEW_KEY_VALUE * C_POINTER_SIZE, py_str_exact_copy(text))
        if _special_adopt(slots, tokens, _STR_NEW_KEY_VALUE) != 0:
            return -1
        _special_drop(slots, tokens, _STR_NEW_TEXT)
        if _special_copy(slots, tokens, _STR_NEW_TEXT, ptr_add(slots, _STR_NEW_KEY_VALUE * C_POINTER_SIZE), 0) != 0:
            return -1
        _special_drop(slots, tokens, _STR_NEW_KEY_VALUE)
    if py_builtin_type_class_tag(cls) == PY_TYPE_STR:
        return _special_copy(slots, tokens, _STR_NEW_RESULT, ptr_add(slots, _STR_NEW_TEXT * C_POINTER_SIZE), 0)
    store_ptr(slots, _STR_NEW_RESULT * C_POINTER_SIZE, py_instance_new(cls))
    if _special_adopt(slots, tokens, _STR_NEW_RESULT) != 0:
        return -1
    result = load_ptr(slots, _STR_NEW_RESULT * C_POINTER_SIZE)
    if ptr_is_null(result) != 0:
        return _special_error(cstr("str subtype allocation failed"))
    pcc_gc_store_ptr(result, _instance_builtin_payload_slot(result, cls), load_ptr(slots, _STR_NEW_TEXT * C_POINTER_SIZE))
    return -1 if py_err_occurred() != 0 else 0


def _str_exact_copy_body(slots, tokens) -> int:
    value = load_ptr(slots, _STR_NEW_CLASS * C_POINTER_SIZE)
    if ptr_is_null(value) != 0 or is_tagged_int(value) != 0:
        py_raise_owned(py_exc_new(3, cstr("string copy requires str")))
        return -1
    if load_i32(value, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_STR:
        source = ptr_add(slots, _STR_NEW_CLASS * C_POINTER_SIZE)
    else:
        if not _ptr_is_instance(value):
            py_raise_owned(py_exc_new(3, cstr("string copy requires str")))
            return -1
        if _special_copy(slots, tokens, _STR_NEW_PAYLOAD_CLASS,
                ptr_add(value, PYINSTANCEOBJECT_CLS_OFFSET), 1) != 0:
            return -1
        cls = load_ptr(slots, _STR_NEW_PAYLOAD_CLASS * C_POINTER_SIZE)
        if py_class_is_str_subclass(cls) == 0:
            py_raise_owned(py_exc_new(3, cstr("string copy requires str")))
            return -1
        source = _instance_builtin_payload_slot(value, cls)
    # The receiver is leased, so its payload slot remains addressable while
    # the child acquires its own counted address lease. This child lease, not
    # a receiver root/pin, protects UTF-8 bytes across allocation and polls.
    if _special_copy(slots, tokens, _STR_NEW_TEXT, source, 0) != 0:
        return -1
    payload = load_ptr(slots, _STR_NEW_TEXT * C_POINTER_SIZE)
    if ptr_is_null(payload) != 0:
        return _special_error(cstr("str subtype has no initialized payload"))
    store_ptr(slots, _STR_NEW_RESULT * C_POINTER_SIZE,
              py_str_new(py_str_utf8(payload), py_str_byte_len(payload)))
    return _special_adopt(slots, tokens, _STR_NEW_RESULT)


def _str_rooted_entry(cls, args, kwargs, copy_only: int):
    borrowed = stack_alloc(3 * C_POINTER_SIZE)
    store_ptr(borrowed, 0, cls)
    store_ptr(borrowed, C_POINTER_SIZE, args)
    store_ptr(borrowed, 2 * C_POINTER_SIZE, kwargs)
    pcc_gc_frame_enter(global_addr("pcc_str_new_borrowed_map"), borrowed)
    output = stack_alloc(C_POINTER_SIZE)
    store_ptr(output, 0, null())
    pcc_gc_frame_enter(global_addr("pcc_str_new_result_map"), output)
    slots = stack_alloc(14 * C_POINTER_SIZE)
    tokens = stack_alloc(14 * C_POINTER_SIZE)
    handles = stack_alloc(14 * C_POINTER_SIZE)
    count: int = _special_open(slots, tokens, handles)
    status: int = -1
    suspended: int = 0
    if count == 14:
        py_tls_exc_swap_slot(slots)
        suspended = 1
        status = _special_copy(slots, tokens, _STR_NEW_CLASS, borrowed, 1)
        if status == 0:
            status = _special_copy(slots, tokens, _STR_NEW_ARGS, ptr_add(borrowed, C_POINTER_SIZE), 1)
        if status == 0:
            status = _special_copy(slots, tokens, _STR_NEW_KWARGS, ptr_add(borrowed, 2 * C_POINTER_SIZE), 1)
        if status == 0:
            if copy_only != 0:
                status = _str_exact_copy_body(slots, tokens)
            else:
                status = _str_new_body(slots, tokens)
        if status == 0:
            status = _special_publish(slots, tokens, output)
    if status != 0:
        _special_error(cstr("str subtype construction failed without an exception"))
    _special_close(slots, tokens, handles, count, suspended)
    prior: int = 0
    value = load_ptr(output, 0)
    if ptr_is_null(value) == 0:
        prior = load_i32(value, PYOBJECTHEADER_FLAGS_OFFSET) & 64
        pcc_gc_pin(value)
    pcc_gc_frame_leave(output)
    pcc_gc_frame_leave(borrowed)
    return pcc_gc_take_pinned_slot(output, prior)


@c_abi_export("py_str_subclass_new")
def py_str_subclass_new(cls, args, kwargs):
    return _str_rooted_entry(cls, args, kwargs, 0)


@c_abi_export("py_str_exact_copy")
def py_str_exact_copy(value):
    return _str_rooted_entry(value, null(), null(), 1)


# Reflection uses the existing special-call root frame and result transfer.
# Slots 0 and 13 remain the entry/cleanup exception owners; 12 is the output.
_DIR_SOURCE = 1
_DIR_NAMESPACE = 2
_DIR_CALLABLE = 3
_DIR_NAMES = 4
_DIR_KEY = 5
_DIR_RESULT = 12
_DIR_FRAME_COUNT = 14

py_obj_sorted_slots = extern(
    "py_obj_sorted_slots", (c_ptr, c_ptr, c_ptr, c_int64, c_ptr), c_int64
)


def _dir_adopt(slots, tokens, index: int) -> int:
    status: int = _special_adopt(slots, tokens, index)
    if status == 0 and py_err_occurred() != 0:
        return -1
    if status == 0 and ptr_is_null(load_ptr(slots, index * C_POINTER_SIZE)) != 0:
        return _special_error(cstr("dir producer returned NULL without an exception"))
    return status


def _dir_is_module(receiver) -> int:
    # A runtime module is identified by its canonical class, never by its
    # name, package, or attribute spelling. Module subclasses share this path.
    if not _special_native_instance(receiver):
        return 0
    # Lock acquisition can park, so load the canonical class afterward and
    # through its authoritative global root. The receiver is already leased.
    pcc_py_gc_minor_graph_lock()
    module_cls = pcc_gc_load_ptr(
        null(), global_addr("pcc_runtime_module_class_cache")
    )
    if ptr_is_null(module_cls) != 0:
        pcc_py_gc_minor_graph_unlock()
        return 0
    cls = pcc_gc_load_ptr(receiver, ptr_add(receiver, PYINSTANCEOBJECT_CLS_OFFSET))
    count: int = load_i32(cls, PYCLASSOBJECT_N_MRO_OFFSET)
    mro = load_ptr(cls, PYCLASSOBJECT_MRO_OFFSET)
    found: int = 0
    index: int = 0
    while index < count:
        base = pcc_gc_load_ptr(cls, ptr_add(mro, index * C_POINTER_SIZE))
        if ptr_eq(base, module_cls) != 0:
            found = 1
            break
        index = index + 1
    pcc_py_gc_minor_graph_unlock()
    return found


def _dir_module_names(slots, tokens) -> int:
    source = ptr_add(slots, _DIR_SOURCE * C_POINTER_SIZE)
    namespace = ptr_add(slots, _DIR_NAMESPACE * C_POINTER_SIZE)
    method = ptr_add(slots, _DIR_CALLABLE * C_POINTER_SIZE)
    names = ptr_add(slots, _DIR_NAMES * C_POINTER_SIZE)
    key = ptr_add(slots, _DIR_KEY * C_POINTER_SIZE)
    store_ptr(namespace, 0, py_obj_getattr(load_ptr(source, 0), cstr("__dict__")))
    if _dir_adopt(slots, tokens, _DIR_NAMESPACE) != 0:
        return -1
    dictionary = load_ptr(namespace, 0)
    if is_tagged_int(dictionary) != 0 or load_i32(dictionary, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_DICT:
        py_raise_owned(py_exc_new(3, cstr("module.__dict__ is not a dictionary")))
        return -1
    store_ptr(key, 0, py_str_new(cstr("__dir__"), 7))
    if _dir_adopt(slots, tokens, _DIR_KEY) != 0:
        return -1
    store_ptr(method, 0, py_dict_get(load_ptr(namespace, 0), load_ptr(key, 0)))
    if _special_adopt(slots, tokens, _DIR_CALLABLE) != 0:
        return -1
    if py_err_occurred() != 0:
        return -1
    if ptr_is_null(load_ptr(method, 0)) == 0:
        # A present None is a non-callable error, not absence. The module
        # dictionary callback takes no implicit receiver, matching PEP 562.
        if py_obj_call_slots(method, null(), null(), names) != 0:
            return -1
    else:
        store_ptr(names, 0, py_dict_keys(load_ptr(namespace, 0)))
    return _dir_adopt(slots, tokens, _DIR_NAMES)


@c_abi_export("py_obj_dir_slots")
def py_obj_dir_slots(source_slot, result_slot) -> int:
    """Publish sorted native module/custom-__dir__ names into an empty root.

    Default class/instance reflection requires complete canonical builtin
    namespaces. Those are not yet represented by this runtime; fail explicitly
    rather than silently omit inherited names. No-argument frame reflection
    is a separate compiler capability and does not enter this ABI.
    """
    if ptr_is_null(source_slot) != 0 or ptr_is_null(result_slot) != 0:
        return _special_error(cstr("dir requires source and output root slots"))
    if ptr_is_null(load_ptr(result_slot, 0)) == 0:
        return _special_error(cstr("dir output root must be empty"))
    slots = stack_alloc(_DIR_FRAME_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_DIR_FRAME_COUNT * C_POINTER_SIZE)
    handles = stack_alloc(_DIR_FRAME_COUNT * C_POINTER_SIZE)
    count: int = _special_open(slots, tokens, handles)
    status: int = -1
    suspended: int = 0
    if count == _DIR_FRAME_COUNT:
        py_tls_exc_swap_slot(slots)
        suspended = 1
        status = _special_copy(slots, tokens, _DIR_SOURCE, source_slot, 0)
        source = ptr_add(slots, _DIR_SOURCE * C_POINTER_SIZE)
        names = ptr_add(slots, _DIR_NAMES * C_POINTER_SIZE)
        result = ptr_add(slots, _DIR_RESULT * C_POINTER_SIZE)
        handled = stack_alloc(C_POINTER_SIZE)
        store_i64(handled, 0, 0)
        if status == 0:
            status = py_obj_special_call_slots(
                source, cstr("__dir__"), null(), null(), names, handled
            )
        if status == 0:
            if load_i64(handled, 0) != 0:
                status = _dir_adopt(slots, tokens, _DIR_NAMES)
            elif _dir_is_module(load_ptr(source, 0)) != 0:
                status = _dir_module_names(slots, tokens)
            else:
                status = _special_error(cstr(
                    "native default dir requires complete class and builtin namespaces"
                ))
        if status == 0:
            status = py_obj_sorted_slots(names, null(), null(), 0, result)
        if status == 0:
            status = _dir_adopt(slots, tokens, _DIR_RESULT)
        if status == 0:
            status = _special_publish(slots, tokens, result_slot)
    else:
        _special_error(cstr("dir root registration failed"))
    if status < 0:
        _special_error(cstr("dir failed without an exception"))
    _special_close(slots, tokens, handles, count, suspended)
    return status

# Tuple construction owns its class, arguments and every iterable temporary
# through the same counted-lease frame used by other class constructors.
_TUPLE_NEW_CLASS = 1
_TUPLE_NEW_ARGS = 2
_TUPLE_NEW_KWARGS = 3
_TUPLE_NEW_PAYLOAD = 4
_TUPLE_NEW_SOURCE = 5
_TUPLE_NEW_ITERATOR = 6
_TUPLE_NEW_ITEMS = 7
_TUPLE_NEW_ITEM = 8
_TUPLE_NEW_ERROR = 9
_TUPLE_NEW_INITIALIZER = 10
_TUPLE_NEW_RESULT = 12

define_global_i32("pcc_tuple_new_borrowed_map", -3)
define_global_i32("pcc_tuple_new_result_map", 1)
py_obj_iter = extern("py_obj_iter", (c_ptr,), c_ptr)
py_obj_next = extern("py_obj_next", (c_ptr,), c_ptr)
py_list_new = extern("py_list_new", (c_int64,), c_ptr)
py_list_append = extern("py_list_append", (c_ptr, c_ptr), c_void)
py_tuple_from_list = extern("py_tuple_from_list", (c_ptr,), c_ptr)


def _tuple_new_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    # The standard binder supplies positional-only cls, *args and **kwargs.
    return py_tuple_subclass_new(
        load_ptr(args, PYTUPLEOBJECT_ITEMS_OFFSET),
        load_ptr(args, PYTUPLEOBJECT_ITEMS_OFFSET + C_POINTER_SIZE),
        load_ptr(args, PYTUPLEOBJECT_ITEMS_OFFSET + 2 * C_POINTER_SIZE),
    )


def _tuple_new_collect(slots, tokens) -> int:
    source = load_ptr(slots, _TUPLE_NEW_SOURCE * C_POINTER_SIZE)
    if is_tagged_int(source) == 0:
        if load_i32(source, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_TUPLE:
            # Exact tuple input has no iterable override and is already an
            # immutable owner. Subclass inputs take the ordinary iterator path.
            return _special_copy(slots, tokens, _TUPLE_NEW_PAYLOAD,
                ptr_add(slots, _TUPLE_NEW_SOURCE * C_POINTER_SIZE), 0)
    store_ptr(slots, _TUPLE_NEW_ITERATOR * C_POINTER_SIZE, py_obj_iter(source))
    if _special_adopt(slots, tokens, _TUPLE_NEW_ITERATOR) != 0:
        return -1
    if ptr_is_null(load_ptr(slots, _TUPLE_NEW_ITERATOR * C_POINTER_SIZE)) != 0:
        return _special_error(cstr("tuple input did not produce an iterator"))
    store_ptr(slots, _TUPLE_NEW_ITEMS * C_POINTER_SIZE, py_list_new(0))
    if _special_adopt(slots, tokens, _TUPLE_NEW_ITEMS) != 0:
        return -1
    if ptr_is_null(load_ptr(slots, _TUPLE_NEW_ITEMS * C_POINTER_SIZE)) != 0:
        return _special_error(cstr("tuple accumulation allocation failed"))
    while True:
        store_ptr(slots, _TUPLE_NEW_ITEM * C_POINTER_SIZE,
            py_obj_next(load_ptr(slots, _TUPLE_NEW_ITERATOR * C_POINTER_SIZE)))
        if _special_adopt(slots, tokens, _TUPLE_NEW_ITEM) != 0:
            return -1
        if ptr_is_null(load_ptr(slots, _TUPLE_NEW_ITEM * C_POINTER_SIZE)) != 0:
            if py_err_occurred() != 0:
                # Move TLS ownership before builtin-class lookup can park.
                # A raw py_current_exception() argument would otherwise span
                # that lookup without owning the movable exception.
                py_tls_exc_swap_slot(ptr_add(slots, _TUPLE_NEW_ERROR * C_POINTER_SIZE))
                if _special_adopt(slots, tokens, _TUPLE_NEW_ERROR) != 0:
                    return -1
                if py_exc_matches(load_ptr(slots, _TUPLE_NEW_ERROR * C_POINTER_SIZE), py_exc_builtin_class(8)) == 0:
                    py_raise(load_ptr(slots, _TUPLE_NEW_ERROR * C_POINTER_SIZE))
                    return -1
                _special_drop(slots, tokens, _TUPLE_NEW_ERROR)
            break
        py_list_append(load_ptr(slots, _TUPLE_NEW_ITEMS * C_POINTER_SIZE),
            load_ptr(slots, _TUPLE_NEW_ITEM * C_POINTER_SIZE))
        if py_err_occurred() != 0:
            return -1
        _special_drop(slots, tokens, _TUPLE_NEW_ITEM)
    store_ptr(slots, _TUPLE_NEW_PAYLOAD * C_POINTER_SIZE,
        py_tuple_from_list(load_ptr(slots, _TUPLE_NEW_ITEMS * C_POINTER_SIZE)))
    if _special_adopt(slots, tokens, _TUPLE_NEW_PAYLOAD) != 0:
        return -1
    if ptr_is_null(load_ptr(slots, _TUPLE_NEW_PAYLOAD * C_POINTER_SIZE)) != 0:
        return _special_error(cstr("tuple payload allocation failed"))
    return 0


def _tuple_new_body(slots, tokens) -> int:
    cls = load_ptr(slots, _TUPLE_NEW_CLASS * C_POINTER_SIZE)
    if py_class_is_tuple_subclass(cls) == 0:
        py_raise_owned(py_exc_new(3, cstr("tuple.__new__ requires a tuple subtype")))
        return -1
    if _class_is_structseq(cls):
        py_raise_owned(py_exc_new(3, cstr("tuple.__new__ is not safe for a struct sequence type")))
        return -1
    if _special_validate_arguments(slots) != 0:
        return -1
    args = load_ptr(slots, _TUPLE_NEW_ARGS * C_POINTER_SIZE)
    nargs: int = 0 if ptr_is_null(args) != 0 else py_tuple_len(args)
    kwargs = load_ptr(slots, _TUPLE_NEW_KWARGS * C_POINTER_SIZE)
    if ptr_is_null(kwargs) == 0 and ptr_eq(kwargs, global_load_ptr("py_None")) == 0:
        if py_dict_len(kwargs) != 0:
            # tuple's default initializer rejects keywords. A subtype with
            # its own initializer owns those keyword arguments instead.
            record = stack_alloc(2 * C_POINTER_SIZE)
            if _special_select_owned(slots, tokens, _TUPLE_NEW_CLASS, cstr("__init__"),
                    _TUPLE_NEW_INITIALIZER, record) != 0:
                return -1
            if load_i64(record, 0) == 0:
                py_raise_owned(py_exc_new(3, cstr("tuple() takes no keyword arguments")))
                return -1
            _special_drop(slots, tokens, _TUPLE_NEW_INITIALIZER)
    if nargs > 1:
        py_raise_owned(py_exc_new(3, cstr("tuple expected at most 1 argument")))
        return -1
    if nargs == 0:
        store_ptr(slots, _TUPLE_NEW_PAYLOAD * C_POINTER_SIZE, py_tuple_new(0))
        if _special_adopt(slots, tokens, _TUPLE_NEW_PAYLOAD) != 0:
            return -1
    else:
        if _special_tuple_item(slots, tokens, _TUPLE_NEW_SOURCE, _TUPLE_NEW_ARGS, 0) != 0:
            return -1
        if _tuple_new_collect(slots, tokens) != 0:
            return -1
    if py_err_occurred() != 0:
        return -1
    if ptr_is_null(load_ptr(slots, _TUPLE_NEW_PAYLOAD * C_POINTER_SIZE)) != 0:
        return _special_error(cstr("tuple payload construction failed"))
    cls = load_ptr(slots, _TUPLE_NEW_CLASS * C_POINTER_SIZE)
    if py_builtin_type_class_tag(cls) == PY_TYPE_TUPLE:
        return _special_copy(slots, tokens, _TUPLE_NEW_RESULT,
            ptr_add(slots, _TUPLE_NEW_PAYLOAD * C_POINTER_SIZE), 0)
    store_ptr(slots, _TUPLE_NEW_RESULT * C_POINTER_SIZE, py_instance_new(cls))
    if _special_adopt(slots, tokens, _TUPLE_NEW_RESULT) != 0:
        return -1
    result = load_ptr(slots, _TUPLE_NEW_RESULT * C_POINTER_SIZE)
    if ptr_is_null(result) != 0:
        return _special_error(cstr("tuple subtype allocation failed"))
    slot = _instance_builtin_payload_slot(result,
        load_ptr(slots, _TUPLE_NEW_CLASS * C_POINTER_SIZE))
    if ptr_is_null(slot) != 0:
        return _special_error(cstr("tuple subtype has no payload owner slot"))
    pcc_gc_store_ptr(result, slot, load_ptr(slots, _TUPLE_NEW_PAYLOAD * C_POINTER_SIZE))
    return -1 if py_err_occurred() != 0 else 0


@c_abi_export("py_tuple_subclass_new")
def py_tuple_subclass_new(cls, args, kwargs):
    borrowed = stack_alloc(3 * C_POINTER_SIZE)
    store_ptr(borrowed, 0, cls)
    store_ptr(borrowed, C_POINTER_SIZE, args)
    store_ptr(borrowed, 2 * C_POINTER_SIZE, kwargs)
    pcc_gc_frame_enter(global_addr("pcc_tuple_new_borrowed_map"), borrowed)
    output = stack_alloc(C_POINTER_SIZE)
    store_ptr(output, 0, null())
    pcc_gc_frame_enter(global_addr("pcc_tuple_new_result_map"), output)
    slots = stack_alloc(14 * C_POINTER_SIZE)
    tokens = stack_alloc(14 * C_POINTER_SIZE)
    handles = stack_alloc(14 * C_POINTER_SIZE)
    count: int = _special_open(slots, tokens, handles)
    status: int = -1
    suspended: int = 0
    if count == 14:
        py_tls_exc_swap_slot(slots)
        suspended = 1
        status = _special_copy(slots, tokens, _TUPLE_NEW_CLASS, borrowed, 1)
        if status == 0:
            status = _special_copy(slots, tokens, _TUPLE_NEW_ARGS, ptr_add(borrowed, C_POINTER_SIZE), 1)
        if status == 0:
            status = _special_copy(slots, tokens, _TUPLE_NEW_KWARGS, ptr_add(borrowed, 2 * C_POINTER_SIZE), 1)
        if status == 0:
            status = _tuple_new_body(slots, tokens)
        if status == 0:
            status = _special_publish(slots, tokens, output)
    if status != 0:
        _special_error(cstr("tuple subtype construction failed without an exception"))
    _special_close(slots, tokens, handles, count, suspended)
    prior: int = 0
    value = load_ptr(output, 0)
    if ptr_is_null(value) == 0:
        prior = load_i32(value, PYOBJECTHEADER_FLAGS_OFFSET) & 64
        pcc_gc_pin(value)
    pcc_gc_frame_leave(output)
    pcc_gc_frame_leave(borrowed)
    return pcc_gc_take_pinned_slot(output, prior)


py_builtin_type_for_tag = extern("py_builtin_type_for_tag", (c_int64,), c_ptr)
py_obj_issubclass = extern("py_obj_issubclass", (c_ptr, c_ptr), c_int64)


def _special_resolve_type(slots, tokens, receiver_index: int, class_index: int) -> int:
    receiver = load_ptr(slots, receiver_index * C_POINTER_SIZE)
    owner_source = null()
    if _ptr_is_class(receiver):
        owner_source = ptr_add(receiver, PYCLASSOBJECT_METACLASS_OFFSET)
    elif _special_native_instance(receiver):
        owner_source = ptr_add(receiver, PYINSTANCEOBJECT_CLS_OFFSET)
    elif ptr_is_null(receiver) == 0 and is_tagged_int(receiver) == 0:
        if load_i32(receiver, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_TUPLE:
            # Canonical builtin initialization can allocate. The receiver has
            # its own counted lease before this class owner is requested.
            store_ptr(slots, class_index * C_POINTER_SIZE,
                py_builtin_type_for_tag(PY_TYPE_TUPLE))
            if _special_adopt(slots, tokens, class_index) != 0:
                return -1
            if ptr_is_null(load_ptr(slots, class_index * C_POINTER_SIZE)) != 0:
                return _special_error(cstr("canonical tuple type is unavailable"))
            return 0
    if ptr_is_null(owner_source) == 0:
        return _special_copy(slots, tokens, class_index, owner_source, 1)
    return 0


def _special_select_owned(slots, tokens, class_index: int, name,
                          descriptor_index: int, record, start_index: int = 0) -> int:
    memset(record, 0, 2 * C_POINTER_SIZE)
    if ptr_is_null(load_ptr(slots, class_index * C_POINTER_SIZE)) != 0:
        return 0
    plan = stack_alloc(256)
    prepared: int = 0
    status: int = 0
    pcc_py_gc_minor_graph_lock()
    source = _special_lookup_locked(load_ptr(slots, class_index * C_POINTER_SIZE),
        name, record, start_index)
    kind: int = load_i64(record, 0)
    if kind == 1 or kind == 2:
        token: int = pcc_gc_root_copy_lease_prepare_locked(
            ptr_add(slots, descriptor_index * C_POINTER_SIZE), source,
            1 if kind == 2 else 0, plan)
        prepared = 1
        if token < 0:
            status = -1
        else:
            store_i64(tokens, descriptor_index * C_POINTER_SIZE, token)
    pcc_py_gc_minor_graph_unlock()
    if prepared != 0:
        pcc_gc_root_copy_lease_finish(plan)
    return status


def _special_invoke_selected(receiver_slot, class_slot, descriptor_slot,
                             record, args_slot, result_slot, kwargs_slot) -> int:
    # Selection owns descriptor/class independently of a mutable namespace.
    # Code addresses remain solely in the separate, untraced record.
    slots = stack_alloc(14 * C_POINTER_SIZE)
    tokens = stack_alloc(14 * C_POINTER_SIZE)
    handles = stack_alloc(14 * C_POINTER_SIZE)
    count: int = _special_open(slots, tokens, handles)
    status: int = -1
    suspended: int = 0
    if count == 14:
        py_tls_exc_swap_slot(slots)
        suspended = 1
        status = _special_copy(slots, tokens, 1, receiver_slot, 0)
        if status == 0:
            status = _special_copy(slots, tokens, 2, args_slot, 0)
        if status == 0:
            status = _special_copy(slots, tokens, 3, kwargs_slot, 0)
        if status == 0:
            status = _special_copy(slots, tokens, 4, class_slot, 0)
        if status == 0 and load_i64(record, 0) != 3:
            status = _special_copy(slots, tokens, 5, descriptor_slot, 0)
        if status == 0:
            status = _special_bind_and_call(slots, tokens, record)
        if status == 0:
            status = _special_publish(slots, tokens, result_slot)
    if status < 0:
        _special_error(cstr("selected special call failed without an exception"))
    _special_close(slots, tokens, handles, count, suspended)
    return status


_BINARY_LEFT = 1
_BINARY_RIGHT = 2
_BINARY_LEFT_CLASS = 3
_BINARY_RIGHT_CLASS = 4
_BINARY_LEFT_METHOD = 5
_BINARY_RIGHT_METHOD = 6
_BINARY_LEFT_REFLECTED = 7
_BINARY_ARGS = 8
_BINARY_LEFT_BOUND = 9
_BINARY_RIGHT_BOUND = 11
_BINARY_CANDIDATE = 10
_BINARY_RESULT = 12


def _binary_selected_attempt(slots, tokens, receiver_index: int, class_index: int,
                             method_index: int, record, argument_index: int, extra_slot) -> int:
    # extra_slot: the caller's authoritative slot of a trailing argument
    # (pow()'s modulus), or NULL for an ordinary binary operation.
    if load_i64(record, 0) == 0:
        return 0
    count: int = 1
    if ptr_is_null(extra_slot) == 0:
        count = 2
    if _special_tuple_new(slots, tokens, _BINARY_ARGS, count) != 0:
        return -1
    py_tuple_set_item(load_ptr(slots, _BINARY_ARGS * C_POINTER_SIZE), 0,
        load_ptr(slots, argument_index * C_POINTER_SIZE))
    if count == 2 and py_err_occurred() == 0:
        py_tuple_set_item(load_ptr(slots, _BINARY_ARGS * C_POINTER_SIZE), 1,
            load_ptr(extra_slot, 0))
    if py_err_occurred() != 0:
        return -1
    status: int = _special_invoke_selected(
        ptr_add(slots, receiver_index * C_POINTER_SIZE),
        ptr_add(slots, class_index * C_POINTER_SIZE),
        ptr_add(slots, method_index * C_POINTER_SIZE), record,
        ptr_add(slots, _BINARY_ARGS * C_POINTER_SIZE),
        ptr_add(slots, _BINARY_CANDIDATE * C_POINTER_SIZE), null())
    if status != 0:
        return -1
    if _special_adopt(slots, tokens, _BINARY_CANDIDATE) != 0:
        return -1
    _special_drop(slots, tokens, _BINARY_ARGS)
    value = load_ptr(slots, _BINARY_CANDIDATE * C_POINTER_SIZE)
    if ptr_eq(value, global_load_ptr("py_NotImplemented")) != 0:
        _special_drop(slots, tokens, _BINARY_CANDIDATE)
        return 0
    if _special_copy(slots, tokens, _BINARY_RESULT,
            ptr_add(slots, _BINARY_CANDIDATE * C_POINTER_SIZE), 0) != 0:
        return -1
    return 1


def _binary_selected_same(slots, tokens, left_record, right_record) -> int:
    left_kind: int = load_i64(left_record, 0)
    right_kind: int = load_i64(right_record, 0)
    if left_kind == 0 or right_kind == 0:
        return 1 if left_kind == right_kind else 0
    if left_kind == 3 or right_kind == 3:
        if left_kind != right_kind:
            return 0
        return ptr_eq(load_ptr(left_record, C_POINTER_SIZE), load_ptr(right_record, C_POINTER_SIZE))
    # Compare the attributes as obtained from each semantic class. Distinct
    # staticmethod wrappers can expose one function, while an inherited
    # classmethod binds two different classes. Custom descriptors may execute.
    if _binary_priority_value(slots, tokens, _BINARY_LEFT_REFLECTED,
            _BINARY_LEFT_CLASS, _BINARY_LEFT_BOUND) != 0:
        return -1
    if _binary_priority_value(slots, tokens, _BINARY_RIGHT_METHOD,
            _BINARY_RIGHT_CLASS, _BINARY_RIGHT_BOUND) != 0:
        return -1
    compared = stack_alloc(C_POINTER_SIZE)
    store_i64(compared, 0, 0)
    if py_obj_binary_special_call_slots(ptr_add(slots, _BINARY_LEFT_BOUND * C_POINTER_SIZE),
            ptr_add(slots, _BINARY_RIGHT_BOUND * C_POINTER_SIZE), cstr("__ne__"), cstr("__ne__"),
            1, ptr_add(slots, _BINARY_CANDIDATE * C_POINTER_SIZE), compared) != 0:
        return -1
    same: int = 0
    if load_i64(compared, 0) != 0:
        if _special_adopt(slots, tokens, _BINARY_CANDIDATE) != 0:
            return -1
        different: int = py_obj_truthy(load_ptr(slots, _BINARY_CANDIDATE * C_POINTER_SIZE))
        if py_err_occurred() != 0:
            return -1
        same = 1 if different == 0 else 0
        _special_drop(slots, tokens, _BINARY_CANDIDATE)
    else:
        same = ptr_eq(load_ptr(slots, _BINARY_LEFT_BOUND * C_POINTER_SIZE),
            load_ptr(slots, _BINARY_RIGHT_BOUND * C_POINTER_SIZE))
    _special_drop(slots, tokens, _BINARY_RIGHT_BOUND)
    _special_drop(slots, tokens, _BINARY_LEFT_BOUND)
    return same


def _binary_special_body(slots, tokens, name, rname, mode: int, handled, extra_slot) -> int:
    if _special_resolve_type(slots, tokens, _BINARY_LEFT, _BINARY_LEFT_CLASS) != 0:
        return -1
    if _special_resolve_type(slots, tokens, _BINARY_RIGHT, _BINARY_RIGHT_CLASS) != 0:
        return -1
    left_record = stack_alloc(2 * C_POINTER_SIZE)
    right_record = stack_alloc(2 * C_POINTER_SIZE)
    inherited_record = stack_alloc(2 * C_POINTER_SIZE)
    if _special_select_owned(slots, tokens, _BINARY_LEFT_CLASS, name, _BINARY_LEFT_METHOD, left_record) != 0:
        return -1
    if _special_select_owned(slots, tokens, _BINARY_RIGHT_CLASS, rname, _BINARY_RIGHT_METHOD, right_record) != 0:
        return -1
    left_cls = load_ptr(slots, _BINARY_LEFT_CLASS * C_POINTER_SIZE)
    right_cls = load_ptr(slots, _BINARY_RIGHT_CLASS * C_POINTER_SIZE)
    same_type: int = ptr_eq(left_cls, right_cls)
    right_first: int = 0
    if ptr_is_null(left_cls) == 0 and ptr_is_null(right_cls) == 0 and same_type == 0:
        subtype: int = py_obj_issubclass(right_cls, left_cls)
        if subtype < 0 or py_err_occurred() != 0:
            return -1
        if subtype != 0 and load_i64(right_record, 0) != 0:
            if mode != 0:
                right_first = 1
            else:
                if _special_select_owned(slots, tokens, _BINARY_LEFT_CLASS, rname,
                        _BINARY_LEFT_REFLECTED, inherited_record) != 0:
                    return -1
                same: int = _binary_selected_same(slots, tokens, inherited_record, right_record)
                if same < 0:
                    return -1
                right_first = 1 if same == 0 else 0
    # Builtin tuple addition/repetition are sequence fallback operations.
    # Give an unrelated/sibling type's numeric reflected method its chance
    # before invoking the sequence descriptor (whose direct call raises on
    # incompatible operands). Do not invent tuple.__radd__ or defer a user
    # override of the left operation.
    if mode == 0 and same_type == 0 and right_first == 0 and load_i64(right_record, 0) != 0:
        operation: int = _tuple_sequence_default_operation(slots, left_record)
        if operation == 13 or operation == 14:
            right_first = 1
    result: int = 0
    if right_first != 0:
        result = _binary_selected_attempt(slots, tokens, _BINARY_RIGHT, _BINARY_RIGHT_CLASS,
            _BINARY_RIGHT_METHOD, right_record, _BINARY_LEFT, extra_slot)
        if result != 0:
            return -1 if result < 0 else 0
    result = _binary_selected_attempt(slots, tokens, _BINARY_LEFT, _BINARY_LEFT_CLASS,
        _BINARY_LEFT_METHOD, left_record, _BINARY_RIGHT, extra_slot)
    if result != 0:
        return -1 if result < 0 else 0
    if right_first == 0 and (mode != 0 or same_type == 0):
        result = _binary_selected_attempt(slots, tokens, _BINARY_RIGHT, _BINARY_RIGHT_CLASS,
            _BINARY_RIGHT_METHOD, right_record, _BINARY_LEFT, extra_slot)
        if result != 0:
            return -1 if result < 0 else 0
    if ptr_is_null(handled) == 0:
        # Ordering mode distinguishes two selected methods that declined from
        # a missing builtin descriptor. Use the original owned selection, not
        # a new namespace lookup after either callback may have mutated it.
        exhausted: int = 0
        if mode == 2 and load_i64(left_record, 0) != 0 and load_i64(right_record, 0) != 0:
            exhausted = 2
        store_i64(handled, 0, exhausted)
    return 0


@c_abi_export("py_obj_binary_special_call_slots")
def py_obj_binary_special_call_slots(left_slot, right_slot, name, rname,
                                     mode: int, result_slot, handled) -> int:
    if ptr_is_null(handled) == 0:
        store_i64(handled, 0, 1)
    if (ptr_is_null(left_slot) != 0 or ptr_is_null(right_slot) != 0
            or ptr_is_null(result_slot) != 0 or ptr_is_null(name) != 0 or ptr_is_null(rname) != 0):
        return _special_error(cstr("binary special call requires authoritative slots and names"))
    slots = stack_alloc(14 * C_POINTER_SIZE)
    tokens = stack_alloc(14 * C_POINTER_SIZE)
    handles = stack_alloc(14 * C_POINTER_SIZE)
    count: int = _special_open(slots, tokens, handles)
    status: int = -1
    suspended: int = 0
    if count == 14:
        py_tls_exc_swap_slot(slots)
        suspended = 1
        status = _special_copy(slots, tokens, _BINARY_LEFT, left_slot, 0)
        if status == 0:
            status = _special_copy(slots, tokens, _BINARY_RIGHT, right_slot, 0)
        if status == 0:
            status = _binary_special_body(slots, tokens, name, rname, mode, handled, null())
        if status == 0 and ptr_is_null(load_ptr(slots, _BINARY_RESULT * C_POINTER_SIZE)) == 0:
            status = _special_publish(slots, tokens, result_slot)
    if status < 0:
        _special_error(cstr("binary special call failed without an exception"))
    _special_close(slots, tokens, handles, count, suspended)
    return status


@c_abi_export("py_obj_ternary_special_call_slots")
def py_obj_ternary_special_call_slots(left_slot, right_slot, extra_slot, name, rname,
                                      result_slot, handled) -> int:
    """CPython's slot_nb_power with a modulus: the binary protocol whose
    calls pass one more argument, ``left.name(right, extra)`` and the
    reflected ``right.rname(left, extra)``. The caller keeps extra_slot
    authoritative for the whole call."""
    if ptr_is_null(handled) == 0:
        store_i64(handled, 0, 1)
    if (ptr_is_null(left_slot) != 0 or ptr_is_null(right_slot) != 0
            or ptr_is_null(extra_slot) != 0 or ptr_is_null(result_slot) != 0
            or ptr_is_null(name) != 0 or ptr_is_null(rname) != 0):
        return _special_error(cstr("ternary special call requires authoritative slots and names"))
    slots = stack_alloc(14 * C_POINTER_SIZE)
    tokens = stack_alloc(14 * C_POINTER_SIZE)
    handles = stack_alloc(14 * C_POINTER_SIZE)
    count: int = _special_open(slots, tokens, handles)
    status: int = -1
    suspended: int = 0
    if count == 14:
        py_tls_exc_swap_slot(slots)
        suspended = 1
        status = _special_copy(slots, tokens, _BINARY_LEFT, left_slot, 0)
        if status == 0:
            status = _special_copy(slots, tokens, _BINARY_RIGHT, right_slot, 0)
        if status == 0:
            status = _binary_special_body(slots, tokens, name, rname, 0, handled, extra_slot)
        if status == 0 and ptr_is_null(load_ptr(slots, _BINARY_RESULT * C_POINTER_SIZE)) == 0:
            status = _special_publish(slots, tokens, result_slot)
    if status < 0:
        _special_error(cstr("ternary special call failed without an exception"))
    _special_close(slots, tokens, handles, count, suspended)
    return status


_TUPLE_VIEW_RECEIVER = 1
_TUPLE_VIEW_ARGS = 2
_TUPLE_VIEW_PAYLOAD = 4
_TUPLE_VIEW_ARGUMENT = 5
_TUPLE_VIEW_OTHER_PAYLOAD = 6
_TUPLE_VIEW_START = 7
_TUPLE_VIEW_STOP = 8
_TUPLE_VIEW_STEP = 9
_TUPLE_VIEW_CLASS = 11
_TUPLE_VIEW_RESULT = 12
_TUPLE_VIEW_ITER_NEXT = 99

define_global_i32("pcc_tuple_view_borrowed_map", -2)
define_global_i32("pcc_tuple_view_result_map", 1)
py_tuple_iter_new = extern("py_tuple_iter_new", (c_ptr,), c_ptr)
py_obj_index_i64 = extern("py_obj_index_i64", (c_ptr,), c_int64)
py_obj_is_slice = extern("py_obj_is_slice", (c_ptr,), c_int64)
py_obj_repr = extern("py_obj_repr", (c_ptr,), c_ptr)
py_obj_hash = extern("py_obj_hash", (c_ptr,), c_int64)
py_obj_contains = extern("py_obj_contains", (c_ptr, c_ptr), c_int64)
py_obj_eq = extern("py_obj_eq", (c_ptr, c_ptr), c_int64)
py_obj_lt = extern("py_obj_lt", (c_ptr, c_ptr), c_int64)
py_obj_le = extern("py_obj_le", (c_ptr, c_ptr), c_int64)
py_obj_gt = extern("py_obj_gt", (c_ptr, c_ptr), c_int64)
py_obj_ge = extern("py_obj_ge", (c_ptr, c_ptr), c_int64)
py_int_value_i64 = extern("py_int_value_i64", (c_ptr,), c_int64)
py_tuple_getitem = extern("py_tuple_getitem", (c_ptr, c_int64), c_ptr)
py_tuple_slice = extern("py_tuple_slice", (c_ptr, c_ptr, c_ptr, c_ptr), c_ptr)
py_tuple_count = extern("py_tuple_count", (c_ptr, c_ptr), c_int64)
py_tuple_index_range = extern("py_tuple_index_range", (c_ptr, c_ptr, c_ptr, c_ptr), c_int64)
py_tuple_concat = extern("py_tuple_concat", (c_ptr, c_ptr), c_ptr)
py_tuple_repeat = extern("py_tuple_repeat", (c_ptr, c_int64), c_ptr)


def _tuple_copy_payload(slots, tokens, owner_index: int, output_index: int) -> int:
    owner = load_ptr(slots, owner_index * C_POINTER_SIZE)
    if ptr_is_null(owner) != 0 or is_tagged_int(owner) != 0:
        return 1
    if load_i32(owner, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_TUPLE:
        return _special_copy(slots, tokens, output_index,
            ptr_add(slots, owner_index * C_POINTER_SIZE), 0)
    if not _special_native_instance(owner):
        return 1
    if _special_copy(slots, tokens, _TUPLE_VIEW_CLASS,
            ptr_add(owner, PYINSTANCEOBJECT_CLS_OFFSET), 1) != 0:
        return -1
    cls = load_ptr(slots, _TUPLE_VIEW_CLASS * C_POINTER_SIZE)
    if py_class_is_tuple_subclass(cls) == 0:
        _special_drop(slots, tokens, _TUPLE_VIEW_CLASS)
        return 1
    source = _instance_builtin_payload_slot(
        load_ptr(slots, owner_index * C_POINTER_SIZE), cls)
    if ptr_is_null(source) != 0:
        return _special_error(cstr("tuple subtype has no payload owner"))
    status: int = _special_copy(slots, tokens, output_index, source, 0)
    _special_drop(slots, tokens, _TUPLE_VIEW_CLASS)
    if status != 0:
        return -1
    payload = load_ptr(slots, output_index * C_POINTER_SIZE)
    if ptr_is_null(payload) != 0 or is_tagged_int(payload) != 0:
        return _special_error(cstr("tuple subtype has no initialized payload"))
    if load_i32(payload, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_TUPLE:
        return _special_error(cstr("tuple subtype payload has invalid storage"))
    return 0


def _tuple_view_not_implemented(slots, tokens) -> int:
    return _special_copy(slots, tokens, _TUPLE_VIEW_RESULT,
        global_addr("py_NotImplemented"), 0)


def _tuple_view_body(slots, tokens, operation: int, iteration_index: int) -> int:
    nargs: int = 0
    if operation != _TUPLE_VIEW_ITER_NEXT:
        args = load_ptr(slots, _TUPLE_VIEW_ARGS * C_POINTER_SIZE)
        if ptr_is_null(args) == 0:
            nargs = py_tuple_len(args)
        expected: int = 2
        if operation == 1 or operation == 2 or operation == 5 or operation == 6 or operation == 18:
            expected = 1
        if (operation != 17 and nargs != expected) or (operation == 17 and (nargs < 2 or nargs > 4)):
            py_raise_owned(py_exc_new(3, cstr("tuple method received invalid arguments")))
            return -1
        if _special_tuple_item(slots, tokens, _TUPLE_VIEW_RECEIVER, _TUPLE_VIEW_ARGS, 0) != 0:
            return -1
        if nargs >= 2:
            if _special_tuple_item(slots, tokens, _TUPLE_VIEW_ARGUMENT, _TUPLE_VIEW_ARGS, 1) != 0:
                return -1
    compatible: int = _tuple_copy_payload(slots, tokens, _TUPLE_VIEW_RECEIVER, _TUPLE_VIEW_PAYLOAD)
    if compatible < 0:
        return -1
    if compatible != 0:
        py_raise_owned(py_exc_new(3, cstr("tuple descriptor requires a tuple receiver")))
        return -1
    scalar: int = 0
    is_scalar: int = 0
    is_boolean: int = 0
    if operation == 1:
        scalar = py_tuple_len(load_ptr(slots, _TUPLE_VIEW_PAYLOAD * C_POINTER_SIZE))
        is_scalar = 1
    elif operation == 2:
        store_ptr(slots, _TUPLE_VIEW_RESULT * C_POINTER_SIZE,
            py_tuple_iter_new(load_ptr(slots, _TUPLE_VIEW_RECEIVER * C_POINTER_SIZE)))
    elif operation == _TUPLE_VIEW_ITER_NEXT:
        length: int = py_tuple_len(load_ptr(slots, _TUPLE_VIEW_PAYLOAD * C_POINTER_SIZE))
        if iteration_index < 0 or iteration_index >= length:
            py_raise_owned(py_exc_new(8, cstr("")))
            return -1
        store_ptr(slots, _TUPLE_VIEW_RESULT * C_POINTER_SIZE,
            py_tuple_getitem(load_ptr(slots, _TUPLE_VIEW_PAYLOAD * C_POINTER_SIZE), iteration_index))
    elif operation == 3:
        key = load_ptr(slots, _TUPLE_VIEW_ARGUMENT * C_POINTER_SIZE)
        if py_obj_is_slice(key) != 0:
            store_ptr(slots, _TUPLE_VIEW_START * C_POINTER_SIZE, py_obj_getattr(key, cstr("start")))
            if _special_adopt(slots, tokens, _TUPLE_VIEW_START) != 0:
                return -1
            store_ptr(slots, _TUPLE_VIEW_STOP * C_POINTER_SIZE,
                py_obj_getattr(load_ptr(slots, _TUPLE_VIEW_ARGUMENT * C_POINTER_SIZE), cstr("stop")))
            if _special_adopt(slots, tokens, _TUPLE_VIEW_STOP) != 0:
                return -1
            store_ptr(slots, _TUPLE_VIEW_STEP * C_POINTER_SIZE,
                py_obj_getattr(load_ptr(slots, _TUPLE_VIEW_ARGUMENT * C_POINTER_SIZE), cstr("step")))
            if _special_adopt(slots, tokens, _TUPLE_VIEW_STEP) != 0:
                return -1
            if py_err_occurred() != 0:
                return -1
            store_ptr(slots, _TUPLE_VIEW_RESULT * C_POINTER_SIZE,
                py_tuple_slice(load_ptr(slots, _TUPLE_VIEW_PAYLOAD * C_POINTER_SIZE),
                    load_ptr(slots, _TUPLE_VIEW_START * C_POINTER_SIZE),
                    load_ptr(slots, _TUPLE_VIEW_STOP * C_POINTER_SIZE),
                    load_ptr(slots, _TUPLE_VIEW_STEP * C_POINTER_SIZE)))
        else:
            scalar = py_obj_index_i64(key)
            if py_err_occurred() != 0:
                return -1
            store_ptr(slots, _TUPLE_VIEW_RESULT * C_POINTER_SIZE,
                py_tuple_getitem(load_ptr(slots, _TUPLE_VIEW_PAYLOAD * C_POINTER_SIZE), scalar))
    elif operation == 4:
        scalar = py_obj_contains(load_ptr(slots, _TUPLE_VIEW_PAYLOAD * C_POINTER_SIZE),
            load_ptr(slots, _TUPLE_VIEW_ARGUMENT * C_POINTER_SIZE))
        is_boolean = 1
    elif operation == 5:
        store_ptr(slots, _TUPLE_VIEW_RESULT * C_POINTER_SIZE,
            py_obj_repr(load_ptr(slots, _TUPLE_VIEW_PAYLOAD * C_POINTER_SIZE)))
    elif operation == 6:
        scalar = py_obj_hash(load_ptr(slots, _TUPLE_VIEW_PAYLOAD * C_POINTER_SIZE))
        is_scalar = 1
    elif operation >= 7 and operation <= 13:
        compatible = _tuple_copy_payload(slots, tokens, _TUPLE_VIEW_ARGUMENT, _TUPLE_VIEW_OTHER_PAYLOAD)
        if compatible < 0:
            return -1
        if compatible != 0:
            if operation == 13:
                py_raise_owned(py_exc_new(3, cstr("can only concatenate tuple to tuple")))
                return -1
            return _tuple_view_not_implemented(slots, tokens)
        left = load_ptr(slots, _TUPLE_VIEW_PAYLOAD * C_POINTER_SIZE)
        right = load_ptr(slots, _TUPLE_VIEW_OTHER_PAYLOAD * C_POINTER_SIZE)
        if operation == 13:
            store_ptr(slots, _TUPLE_VIEW_RESULT * C_POINTER_SIZE, py_tuple_concat(left, right))
        else:
            is_boolean = 1
            if operation == 7 or operation == 8:
                scalar = py_obj_eq(left, right)
                if operation == 8:
                    scalar = 1 if scalar == 0 else 0
            elif operation == 9:
                scalar = py_obj_lt(left, right)
            elif operation == 10:
                scalar = py_obj_le(left, right)
            elif operation == 11:
                scalar = py_obj_gt(left, right)
            else:
                scalar = py_obj_ge(left, right)
    elif operation == 14 or operation == 15:
        scalar = py_obj_index_i64(load_ptr(slots, _TUPLE_VIEW_ARGUMENT * C_POINTER_SIZE))
        if py_err_occurred() != 0:
            return -1
        store_ptr(slots, _TUPLE_VIEW_RESULT * C_POINTER_SIZE,
            py_tuple_repeat(load_ptr(slots, _TUPLE_VIEW_PAYLOAD * C_POINTER_SIZE), scalar))
    elif operation == 16:
        scalar = py_tuple_count(load_ptr(slots, _TUPLE_VIEW_PAYLOAD * C_POINTER_SIZE),
            load_ptr(slots, _TUPLE_VIEW_ARGUMENT * C_POINTER_SIZE))
        is_scalar = 1
    elif operation == 17:
        if nargs >= 3:
            if _special_tuple_item(slots, tokens, _TUPLE_VIEW_START, _TUPLE_VIEW_ARGS, 2) != 0:
                return -1
        if nargs == 4:
            if _special_tuple_item(slots, tokens, _TUPLE_VIEW_STOP, _TUPLE_VIEW_ARGS, 3) != 0:
                return -1
        scalar = py_tuple_index_range(load_ptr(slots, _TUPLE_VIEW_PAYLOAD * C_POINTER_SIZE),
            load_ptr(slots, _TUPLE_VIEW_ARGUMENT * C_POINTER_SIZE),
            load_ptr(slots, _TUPLE_VIEW_START * C_POINTER_SIZE),
            load_ptr(slots, _TUPLE_VIEW_STOP * C_POINTER_SIZE))
        is_scalar = 1
    elif operation == 18:
        if _special_tuple_new(slots, tokens, _TUPLE_VIEW_RESULT, 1) != 0:
            return -1
        py_tuple_set_item(load_ptr(slots, _TUPLE_VIEW_RESULT * C_POINTER_SIZE), 0,
            load_ptr(slots, _TUPLE_VIEW_PAYLOAD * C_POINTER_SIZE))
        return -1 if py_err_occurred() != 0 else 0
    else:
        return _special_error(cstr("unknown tuple descriptor operation"))
    if py_err_occurred() != 0:
        return -1
    if is_boolean != 0:
        store_ptr(slots, _TUPLE_VIEW_RESULT * C_POINTER_SIZE, py_bool_from_bit(scalar))
    elif is_scalar != 0:
        store_ptr(slots, _TUPLE_VIEW_RESULT * C_POINTER_SIZE, py_int_from_i64(scalar))
    return _special_adopt(slots, tokens, _TUPLE_VIEW_RESULT)


def _tuple_view_rooted(receiver, args, operation: int, iteration_index: int):
    borrowed = stack_alloc(2 * C_POINTER_SIZE)
    store_ptr(borrowed, 0, receiver)
    store_ptr(borrowed, C_POINTER_SIZE, args)
    pcc_gc_frame_enter(global_addr("pcc_tuple_view_borrowed_map"), borrowed)
    output = stack_alloc(C_POINTER_SIZE)
    store_ptr(output, 0, null())
    pcc_gc_frame_enter(global_addr("pcc_tuple_view_result_map"), output)
    slots = stack_alloc(14 * C_POINTER_SIZE)
    tokens = stack_alloc(14 * C_POINTER_SIZE)
    handles = stack_alloc(14 * C_POINTER_SIZE)
    count: int = _special_open(slots, tokens, handles)
    status: int = -1
    suspended: int = 0
    if count == 14:
        py_tls_exc_swap_slot(slots)
        suspended = 1
        status = _special_copy(slots, tokens, _TUPLE_VIEW_RECEIVER, borrowed, 1)
        if status == 0:
            status = _special_copy(slots, tokens, _TUPLE_VIEW_ARGS, ptr_add(borrowed, C_POINTER_SIZE), 1)
        if status == 0:
            status = _tuple_view_body(slots, tokens, operation, iteration_index)
        if status == 0:
            status = _special_publish(slots, tokens, output)
    if status != 0:
        _special_error(cstr("tuple descriptor failed without an exception"))
    _special_close(slots, tokens, handles, count, suspended)
    value = load_ptr(output, 0)
    prior: int = 0
    if ptr_is_null(value) == 0 and is_tagged_int(value) == 0:
        prior = load_i32(value, PYOBJECTHEADER_FLAGS_OFFSET) & 64
        pcc_gc_pin(value)
    pcc_gc_frame_leave(output)
    pcc_gc_frame_leave(borrowed)
    return pcc_gc_take_pinned_slot(output, prior)


def _tuple_method_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    operation: int = py_int_value_i64(load_ptr(captures, PYTUPLEOBJECT_ITEMS_OFFSET))
    return _tuple_view_rooted(null(), args, operation, 0)


@c_abi_export("py_tuple_iter_next")
def py_tuple_iter_next(receiver, index: int):
    return _tuple_view_rooted(receiver, null(), _TUPLE_VIEW_ITER_NEXT, index)


def _tuple_method_name(operation: int) -> c_ptr:
    if operation == 1:
        return cstr("__len__")
    if operation == 2:
        return cstr("__iter__")
    if operation == 3:
        return cstr("__getitem__")
    if operation == 4:
        return cstr("__contains__")
    if operation == 5:
        return cstr("__repr__")
    if operation == 6:
        return cstr("__hash__")
    if operation == 7:
        return cstr("__eq__")
    if operation == 8:
        return cstr("__ne__")
    if operation == 9:
        return cstr("__lt__")
    if operation == 10:
        return cstr("__le__")
    if operation == 11:
        return cstr("__gt__")
    if operation == 12:
        return cstr("__ge__")
    if operation == 13:
        return cstr("__add__")
    if operation == 14:
        return cstr("__mul__")
    if operation == 15:
        return cstr("__rmul__")
    if operation == 16:
        return cstr("count")
    if operation == 17:
        return cstr("index")
    return cstr("__getnewargs__")


_TUPLE_TYPE_CLASS = 1
_TUPLE_TYPE_CAPTURES = 2
_TUPLE_TYPE_FUNCTION = 3
_TUPLE_TYPE_DESCRIPTOR = 4
_TUPLE_TYPE_RESULT = 12


def _tuple_type_install_method(slots, tokens, operation: int) -> int:
    name = cstr("__new__") if operation == 0 else _tuple_method_name(operation)
    if operation == 0:
        store_ptr(slots, _TUPLE_TYPE_CAPTURES * C_POINTER_SIZE, _object_new_captures())
        if _special_adopt(slots, tokens, _TUPLE_TYPE_CAPTURES) != 0:
            return -1
        if ptr_is_null(load_ptr(slots, _TUPLE_TYPE_CAPTURES * C_POINTER_SIZE)) != 0:
            return _special_error(cstr("tuple allocator signature allocation failed"))
        store_ptr(slots, _TUPLE_TYPE_FUNCTION * C_POINTER_SIZE,
            py_func_new_bound(_tuple_new_entry, load_ptr(slots, _TUPLE_TYPE_CAPTURES * C_POINTER_SIZE), name, null()))
    else:
        if _special_tuple_new(slots, tokens, _TUPLE_TYPE_CAPTURES, 1) != 0:
            return -1
        py_tuple_set_item(load_ptr(slots, _TUPLE_TYPE_CAPTURES * C_POINTER_SIZE), 0, py_int_from_i64(operation))
        store_ptr(slots, _TUPLE_TYPE_FUNCTION * C_POINTER_SIZE,
            py_func_new_bound(_tuple_method_entry, load_ptr(slots, _TUPLE_TYPE_CAPTURES * C_POINTER_SIZE), name, null()))
    if _special_adopt(slots, tokens, _TUPLE_TYPE_FUNCTION) != 0:
        return -1
    if ptr_is_null(load_ptr(slots, _TUPLE_TYPE_FUNCTION * C_POINTER_SIZE)) != 0:
        return _special_error(cstr("tuple descriptor allocation failed"))
    value_slot = ptr_add(slots, _TUPLE_TYPE_FUNCTION * C_POINTER_SIZE)
    if operation == 0:
        store_ptr(slots, _TUPLE_TYPE_DESCRIPTOR * C_POINTER_SIZE,
            py_staticmethod_new(load_ptr(value_slot, 0)))
        if _special_adopt(slots, tokens, _TUPLE_TYPE_DESCRIPTOR) != 0:
            return -1
        if ptr_is_null(load_ptr(slots, _TUPLE_TYPE_DESCRIPTOR * C_POINTER_SIZE)) != 0:
            return _special_error(cstr("tuple static allocator descriptor allocation failed"))
        value_slot = ptr_add(slots, _TUPLE_TYPE_DESCRIPTOR * C_POINTER_SIZE)
    if py_class_write_namespace_slots(ptr_add(slots, _TUPLE_TYPE_CLASS * C_POINTER_SIZE), name, value_slot, 0) != 0:
        return -1
    cls = load_ptr(slots, _TUPLE_TYPE_CLASS * C_POINTER_SIZE)
    before: int = load_i32(cls, PYCLASSOBJECT_N_METHODS_OFFSET)
    # The namespace now owns the function (through a staticmethod for __new__).
    # The table is only a borrowed compatibility alias, never another owner.
    py_class_add_method(cls, name, load_ptr(slots, _TUPLE_TYPE_FUNCTION * C_POINTER_SIZE))
    cls = load_ptr(slots, _TUPLE_TYPE_CLASS * C_POINTER_SIZE)
    if load_i32(cls, PYCLASSOBJECT_N_METHODS_OFFSET) != before + 1:
        return _special_error(cstr("tuple descriptor table publication failed"))
    _special_drop(slots, tokens, _TUPLE_TYPE_DESCRIPTOR)
    _special_drop(slots, tokens, _TUPLE_TYPE_FUNCTION)
    _special_drop(slots, tokens, _TUPLE_TYPE_CAPTURES)
    return -1 if py_err_occurred() != 0 else 0


def _tuple_type_fill(slots, tokens) -> int:
    # Caller holds the owned cache mutex. Read the canonical source only now;
    # lock acquisition can park and relocate a previously loaded class pointer.
    if ptr_is_null(global_load_ptr("pcc_type_cls_tuple")) == 0:
        return _special_copy(slots, tokens, _TUPLE_TYPE_RESULT, global_addr("pcc_type_cls_tuple"), 0)
    store_ptr(slots, _TUPLE_TYPE_CLASS * C_POINTER_SIZE,
        py_class_new(cstr("tuple"), null(), 0, null(), 0))
    if _special_adopt(slots, tokens, _TUPLE_TYPE_CLASS) != 0:
        return -1
    if ptr_is_null(load_ptr(slots, _TUPLE_TYPE_CLASS * C_POINTER_SIZE)) != 0:
        return _special_error(cstr("tuple type allocation failed"))
    operation: int = 0
    while operation <= 18:
        if _tuple_type_install_method(slots, tokens, operation) != 0:
            return -1
        operation = operation + 1
    # Publish only the completed class. Failed construction remains private
    # and the ordinary class/namespace owners retire its partial descriptors.
    pcc_gc_store_root(global_addr("pcc_type_cls_tuple"),
        load_ptr(slots, _TUPLE_TYPE_CLASS * C_POINTER_SIZE))
    return _special_copy(slots, tokens, _TUPLE_TYPE_RESULT,
        ptr_add(slots, _TUPLE_TYPE_CLASS * C_POINTER_SIZE), 0)


@c_abi_export("py_tuple_type_new")
def py_tuple_type_new():
    output = stack_alloc(C_POINTER_SIZE)
    store_ptr(output, 0, null())
    pcc_gc_frame_enter(global_addr("pcc_tuple_view_result_map"), output)
    slots = stack_alloc(14 * C_POINTER_SIZE)
    tokens = stack_alloc(14 * C_POINTER_SIZE)
    handles = stack_alloc(14 * C_POINTER_SIZE)
    count: int = _special_open(slots, tokens, handles)
    status: int = -1
    suspended: int = 0
    if count == 14:
        py_tls_exc_swap_slot(slots)
        suspended = 1
        mutex = _object_new_cache_mutex()
        if ptr_is_null(mutex) == 0 and pcc_mutex_lock(mutex) == 0:
            status = _tuple_type_fill(slots, tokens)
            pcc_mutex_unlock(mutex)
        if status == 0:
            status = _special_publish(slots, tokens, output)
    if status != 0:
        _special_error(cstr("tuple type initialization failed without an exception"))
    _special_close(slots, tokens, handles, count, suspended)
    value = load_ptr(output, 0)
    prior: int = 0
    if ptr_is_null(value) == 0:
        prior = load_i32(value, PYOBJECTHEADER_FLAGS_OFFSET) & 64
        pcc_gc_pin(value)
    pcc_gc_frame_leave(output)
    return pcc_gc_take_pinned_slot(output, prior)


def _tuple_class_call_body(slots, tokens) -> int:
    # 1 class, 2 original args, 3 kwargs, 4 constructed result,
    # 5 selected descriptor, 6 binding receiver, 7 full args, 9 actual type.
    if py_class_is_tuple_subclass(load_ptr(slots, C_POINTER_SIZE)) == 0:
        py_raise_owned(py_exc_new(3, cstr("tuple class call requires a tuple subtype")))
        return -1
    if _special_validate_arguments(slots) != 0:
        return -1
    record = stack_alloc(2 * C_POINTER_SIZE)
    if _special_select_owned(slots, tokens, 1, cstr("__new__"), 5, record) != 0:
        return -1
    if load_i64(record, 0) == 0 or load_i64(record, 0) == 3:
        return _special_error(cstr("tuple allocator requires a managed callable descriptor"))
    if _special_copy(slots, tokens, 6, ptr_add(slots, C_POINTER_SIZE), 0) != 0:
        return -1
    if _special_prepend(slots, tokens) != 0:
        return -1
    if py_obj_call_slots(ptr_add(slots, 5 * C_POINTER_SIZE),
            ptr_add(slots, 7 * C_POINTER_SIZE), ptr_add(slots, 3 * C_POINTER_SIZE),
            ptr_add(slots, 12 * C_POINTER_SIZE)) != 0:
        return -1
    if _special_adopt(slots, tokens, 12) != 0:
        return -1
    if _special_copy(slots, tokens, 4, ptr_add(slots, 12 * C_POINTER_SIZE), 0) != 0:
        return -1
    _special_drop(slots, tokens, 12)
    _special_drop(slots, tokens, 7)
    _special_drop(slots, tokens, 6)
    _special_drop(slots, tokens, 5)
    if _special_resolve_type(slots, tokens, 4, 9) != 0:
        return -1
    actual = load_ptr(slots, 9 * C_POINTER_SIZE)
    matches: int = 0
    if ptr_is_null(actual) == 0:
        matches = py_obj_issubclass(actual, load_ptr(slots, C_POINTER_SIZE))
    if matches < 0 or py_err_occurred() != 0:
        return -1
    if matches != 0:
        if _special_select_owned(slots, tokens, 9, cstr("__init__"), 5, record) != 0:
            return -1
        if load_i64(record, 0) != 0:
            if _special_invoke_selected(ptr_add(slots, 4 * C_POINTER_SIZE),
                    ptr_add(slots, 9 * C_POINTER_SIZE), ptr_add(slots, 5 * C_POINTER_SIZE),
                    record, ptr_add(slots, 2 * C_POINTER_SIZE),
                    ptr_add(slots, 12 * C_POINTER_SIZE), ptr_add(slots, 3 * C_POINTER_SIZE)) != 0:
                return -1
            if _special_adopt(slots, tokens, 12) != 0:
                return -1
            if ptr_eq(load_ptr(slots, 12 * C_POINTER_SIZE), global_load_ptr("py_None")) == 0:
                py_raise_owned(py_exc_new(3, cstr("__init__() should return None")))
                return -1
            _special_drop(slots, tokens, 12)
    if py_err_occurred() != 0:
        return -1
    return _special_copy(slots, tokens, 12, ptr_add(slots, 4 * C_POINTER_SIZE), 0)


@c_abi_export("py_tuple_class_call")
def py_tuple_class_call(cls, args, kwargs):
    borrowed = stack_alloc(3 * C_POINTER_SIZE)
    store_ptr(borrowed, 0, cls)
    store_ptr(borrowed, C_POINTER_SIZE, args)
    store_ptr(borrowed, 2 * C_POINTER_SIZE, kwargs)
    pcc_gc_frame_enter(global_addr("pcc_tuple_new_borrowed_map"), borrowed)
    output = stack_alloc(C_POINTER_SIZE)
    store_ptr(output, 0, null())
    pcc_gc_frame_enter(global_addr("pcc_tuple_new_result_map"), output)
    slots = stack_alloc(14 * C_POINTER_SIZE)
    tokens = stack_alloc(14 * C_POINTER_SIZE)
    handles = stack_alloc(14 * C_POINTER_SIZE)
    count: int = _special_open(slots, tokens, handles)
    status: int = -1
    suspended: int = 0
    if count == 14:
        py_tls_exc_swap_slot(slots)
        suspended = 1
        status = _special_copy(slots, tokens, 1, borrowed, 1)
        if status == 0:
            status = _special_copy(slots, tokens, 2, ptr_add(borrowed, C_POINTER_SIZE), 1)
        if status == 0:
            status = _special_copy(slots, tokens, 3, ptr_add(borrowed, 2 * C_POINTER_SIZE), 1)
        if status == 0:
            status = _tuple_class_call_body(slots, tokens)
        if status == 0:
            status = _special_publish(slots, tokens, output)
    if status != 0:
        _special_error(cstr("tuple class call failed without an exception"))
    _special_close(slots, tokens, handles, count, suspended)
    value = load_ptr(output, 0)
    prior: int = 0
    if ptr_is_null(value) == 0 and is_tagged_int(value) == 0:
        prior = load_i32(value, PYOBJECTHEADER_FLAGS_OFFSET) & 64
        pcc_gc_pin(value)
    pcc_gc_frame_leave(output)
    pcc_gc_frame_leave(borrowed)
    return pcc_gc_take_pinned_slot(output, prior)


def _super_new_select(slots, tokens, record) -> int:
    # 1 bound receiver, 4 actual MRO class, 6 starting class, 5 descriptor.
    receiver = load_ptr(slots, C_POINTER_SIZE)
    origin = load_ptr(slots, 6 * C_POINTER_SIZE)
    if not _ptr_is_class(origin):
        py_raise_owned(py_exc_new(3, cstr("super() argument 1 must be a type")))
        return -1
    if _ptr_is_class(receiver):
        if _special_copy(slots, tokens, 4, ptr_add(slots, C_POINTER_SIZE), 0) != 0:
            return -1
    elif _special_native_instance(receiver):
        if _special_copy(slots, tokens, 4, ptr_add(receiver, PYINSTANCEOBJECT_CLS_OFFSET), 1) != 0:
            return -1
    else:
        py_raise_owned(py_exc_new(3, cstr("super(type, obj): obj must be an instance or subtype of type")))
        return -1
    plan = stack_alloc(256)
    prepared: int = 0
    status: int = 0
    found: int = 0
    memset(record, 0, 2 * C_POINTER_SIZE)
    pcc_py_gc_minor_graph_lock()
    actual = load_ptr(slots, 4 * C_POINTER_SIZE)
    origin = load_ptr(slots, 6 * C_POINTER_SIZE)
    mro = load_ptr(actual, PYCLASSOBJECT_MRO_OFFSET)
    count: int = load_i32(actual, PYCLASSOBJECT_N_MRO_OFFSET)
    index: int = 0
    start: int = 0
    while index < count:
        owner = pcc_gc_load_ptr(actual, ptr_add(mro, index * C_POINTER_SIZE))
        if ptr_eq(owner, origin) != 0:
            found = 1
            start = index + 1
            break
        index = index + 1
    if found != 0:
        source = _special_lookup_locked(actual, cstr("__new__"), record, start)
        kind: int = load_i64(record, 0)
        if kind == 1 or kind == 2:
            token: int = pcc_gc_root_copy_lease_prepare_locked(ptr_add(slots, 5 * C_POINTER_SIZE),
                source, 1 if kind == 2 else 0, plan)
            prepared = 1
            if token < 0:
                status = -1
            else:
                store_i64(tokens, 5 * C_POINTER_SIZE, token)
    pcc_py_gc_minor_graph_unlock()
    if prepared != 0:
        pcc_gc_root_copy_lease_finish(plan)
    if found == 0:
        py_raise_owned(py_exc_new(3, cstr("super(type, obj): obj must be an instance or subtype of type")))
        return -1
    if status != 0:
        return -1
    if load_i64(record, 0) == 0:
        py_raise_owned(py_exc_new(6, cstr("super object has no attribute '__new__'")))
        return -1
    if load_i64(record, 0) == 3:
        return _special_error(cstr("super allocator requires a managed callable descriptor"))
    return 0


def _special_bind_attribute(slots, tokens, name, implicitly_static: int) -> int:
    descriptor = load_ptr(slots, 5 * C_POINTER_SIZE)
    tag: int = -1
    if ptr_is_null(descriptor) == 0 and is_tagged_int(descriptor) == 0:
        tag = load_i32(descriptor, PYOBJECTHEADER_TYPE_TAG_OFFSET)
    if tag == PY_TYPE_STATICMETHOD:
        return _special_copy(slots, tokens, 12,
            ptr_add(descriptor, PYSTATICMETHODOBJECT_FUNC_OFFSET), 0)
    if tag == PY_TYPE_FUNC:
        if implicitly_static != 0:
            return _special_copy(slots, tokens, 12, ptr_add(slots, 5 * C_POINTER_SIZE), 0)
        store_ptr(slots, 12 * C_POINTER_SIZE,
            py_instance_bind_method(descriptor, load_ptr(slots, C_POINTER_SIZE), name))
        return _special_adopt(slots, tokens, 12)
    if tag == PY_TYPE_CLASSMETHOD:
        if _special_copy(slots, tokens, 9,
                ptr_add(descriptor, PYCLASSMETHODOBJECT_FUNC_OFFSET), 0) != 0:
            return -1
        store_ptr(slots, 12 * C_POINTER_SIZE,
            py_instance_bind_method(load_ptr(slots, 9 * C_POINTER_SIZE),
                load_ptr(slots, 4 * C_POINTER_SIZE), name))
        return _special_adopt(slots, tokens, 12)
    if tag == PY_TYPE_PROPERTY:
        if _special_copy(slots, tokens, 9,
                ptr_add(descriptor, PYPROPERTYOBJECT_FGET_OFFSET), 0) != 0:
            return -1
        if _special_tuple_new(slots, tokens, 7, 1) != 0:
            return -1
        py_tuple_set_item(load_ptr(slots, 7 * C_POINTER_SIZE), 0, load_ptr(slots, C_POINTER_SIZE))
        if py_obj_call_slots(ptr_add(slots, 9 * C_POINTER_SIZE),
                ptr_add(slots, 7 * C_POINTER_SIZE), null(), ptr_add(slots, 12 * C_POINTER_SIZE)) != 0:
            return -1
        return _special_adopt(slots, tokens, 12)
    if _special_tuple_new(slots, tokens, 7, 2) != 0:
        return -1
    py_tuple_set_item(load_ptr(slots, 7 * C_POINTER_SIZE), 0, load_ptr(slots, C_POINTER_SIZE))
    py_tuple_set_item(load_ptr(slots, 7 * C_POINTER_SIZE), 1, load_ptr(slots, 4 * C_POINTER_SIZE))
    handled = stack_alloc(C_POINTER_SIZE)
    store_i64(handled, 0, 0)
    if py_obj_special_call_slots(ptr_add(slots, 5 * C_POINTER_SIZE), cstr("__get__"),
            ptr_add(slots, 7 * C_POINTER_SIZE), null(), ptr_add(slots, 12 * C_POINTER_SIZE), handled) != 0:
        return -1
    if load_i64(handled, 0) != 0:
        return _special_adopt(slots, tokens, 12)
    return _special_copy(slots, tokens, 12, ptr_add(slots, 5 * C_POINTER_SIZE), 0)


def _super_new_bind(slots, tokens) -> int:
    # __new__ is implicitly static: explicit cls remains in call args.
    return _special_bind_attribute(slots, tokens, cstr("__new__"), 1)


@c_abi_export("py_super_new_lookup_slots")
def py_super_new_lookup_slots(receiver_slot, from_class_slot, result_slot) -> int:
    # Bind super and fetch its descriptor BEFORE the caller evaluates call
    # operands. The returned callable is one owned result, not a borrowed MRO
    # pointer; later class mutation cannot free the selected value.
    if ptr_is_null(receiver_slot) != 0 or ptr_is_null(from_class_slot) != 0 or ptr_is_null(result_slot) != 0:
        return _special_error(cstr("super allocator lookup requires authoritative root slots"))
    slots = stack_alloc(14 * C_POINTER_SIZE)
    tokens = stack_alloc(14 * C_POINTER_SIZE)
    handles = stack_alloc(14 * C_POINTER_SIZE)
    count: int = _special_open(slots, tokens, handles)
    status: int = -1
    suspended: int = 0
    if count == 14:
        py_tls_exc_swap_slot(slots)
        suspended = 1
        status = _special_copy(slots, tokens, 1, receiver_slot, 0)
        if status == 0:
            status = _special_copy(slots, tokens, 6, from_class_slot, 0)
        record = stack_alloc(2 * C_POINTER_SIZE)
        if status == 0:
            status = _super_new_select(slots, tokens, record)
        if status == 0:
            status = _super_new_bind(slots, tokens)
        if status == 0:
            status = _special_publish(slots, tokens, result_slot)
    if status < 0:
        _special_error(cstr("super allocator lookup failed without an exception"))
    _special_close(slots, tokens, handles, count, suspended)
    return status



# PyFunc's entry/captures fields, shared with py_func_new_bound.
_TUPLE_METHOD_ENTRY_OFFSET = 56
_TUPLE_METHOD_CAPTURES_OFFSET = 64


def _tuple_sequence_default_operation(slots, record) -> int:
    if load_i64(record, 0) != 1 and load_i64(record, 0) != 2:
        return 0
    operation: int = 0
    pcc_py_gc_minor_graph_lock()
    function = load_ptr(slots, _BINARY_LEFT_METHOD * C_POINTER_SIZE)
    if ptr_is_null(function) == 0 and is_tagged_int(function) == 0:
        if load_i32(function, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_FUNC:
            if ptr_eq(load_ptr(function, _TUPLE_METHOD_ENTRY_OFFSET), _tuple_method_entry) != 0:
                captures = pcc_gc_load_ptr(function, ptr_add(function, _TUPLE_METHOD_CAPTURES_OFFSET))
                if ptr_is_null(captures) == 0:
                    code = pcc_gc_load_ptr(captures, ptr_add(captures, PYTUPLEOBJECT_ITEMS_OFFSET))
                    if is_tagged_int(code) != 0:
                        operation = untag_int(code)
    pcc_py_gc_minor_graph_unlock()
    return operation



py_obj_truthy = extern("py_obj_truthy", (c_ptr,), c_int64)


def _binary_priority_value(slots, tokens, descriptor_index: int,
                           class_index: int, output_index: int) -> int:
    descriptor = load_ptr(slots, descriptor_index * C_POINTER_SIZE)
    tag: int = -1
    if ptr_is_null(descriptor) == 0 and is_tagged_int(descriptor) == 0:
        tag = load_i32(descriptor, PYOBJECTHEADER_TYPE_TAG_OFFSET)
    if tag == PY_TYPE_FUNC or tag == PY_TYPE_PROPERTY:
        return _special_copy(slots, tokens, output_index,
            ptr_add(slots, descriptor_index * C_POINTER_SIZE), 0)
    if tag == PY_TYPE_STATICMETHOD:
        return _special_copy(slots, tokens, output_index,
            ptr_add(descriptor, PYSTATICMETHODOBJECT_FUNC_OFFSET), 0)
    if tag == PY_TYPE_CLASSMETHOD:
        if _special_copy(slots, tokens, _BINARY_CANDIDATE,
                ptr_add(descriptor, PYCLASSMETHODOBJECT_FUNC_OFFSET), 0) != 0:
            return -1
        store_ptr(slots, output_index * C_POINTER_SIZE,
            py_instance_bind_method(load_ptr(slots, _BINARY_CANDIDATE * C_POINTER_SIZE),
                load_ptr(slots, class_index * C_POINTER_SIZE), cstr("reflected method")))
        if _special_adopt(slots, tokens, output_index) != 0:
            return -1
        if ptr_is_null(load_ptr(slots, output_index * C_POINTER_SIZE)) != 0:
            return _special_error(cstr("reflected classmethod binding failed"))
        _special_drop(slots, tokens, _BINARY_CANDIDATE)
        return 0
    if _special_tuple_new(slots, tokens, _BINARY_ARGS, 2) != 0:
        return -1
    py_tuple_set_item(load_ptr(slots, _BINARY_ARGS * C_POINTER_SIZE), 0, global_load_ptr("py_None"))
    py_tuple_set_item(load_ptr(slots, _BINARY_ARGS * C_POINTER_SIZE), 1,
        load_ptr(slots, class_index * C_POINTER_SIZE))
    handled = stack_alloc(C_POINTER_SIZE)
    store_i64(handled, 0, 0)
    status: int = py_obj_special_call_slots(ptr_add(slots, descriptor_index * C_POINTER_SIZE),
        cstr("__get__"), ptr_add(slots, _BINARY_ARGS * C_POINTER_SIZE), null(),
        ptr_add(slots, output_index * C_POINTER_SIZE), handled)
    if status != 0:
        return -1
    if load_i64(handled, 0) != 0:
        if _special_adopt(slots, tokens, output_index) != 0:
            return -1
    else:
        if _special_copy(slots, tokens, output_index,
                ptr_add(slots, descriptor_index * C_POINTER_SIZE), 0) != 0:
            return -1
    _special_drop(slots, tokens, _BINARY_ARGS)
    return 0



define_global_i32("pcc_tuple_attribute_borrowed_map", -1)


@c_abi_export("py_tuple_getattr")
def py_tuple_getattr(receiver, name):
    # Used for exact tuples after the ordinary public attribute preflight.
    # Tuple subtype instances keep their existing __getattribute__ path.
    borrowed = stack_alloc(C_POINTER_SIZE)
    store_ptr(borrowed, 0, receiver)
    pcc_gc_frame_enter(global_addr("pcc_tuple_attribute_borrowed_map"), borrowed)
    output = stack_alloc(C_POINTER_SIZE)
    store_ptr(output, 0, null())
    pcc_gc_frame_enter(global_addr("pcc_tuple_view_result_map"), output)
    slots = stack_alloc(14 * C_POINTER_SIZE)
    tokens = stack_alloc(14 * C_POINTER_SIZE)
    handles = stack_alloc(14 * C_POINTER_SIZE)
    count: int = _special_open(slots, tokens, handles)
    status: int = -1
    suspended: int = 0
    if count == 14:
        py_tls_exc_swap_slot(slots)
        suspended = 1
        status = _special_copy(slots, tokens, 1, borrowed, 1)
        if status == 0:
            status = _special_resolve_type(slots, tokens, 1, 4)
        record = stack_alloc(2 * C_POINTER_SIZE)
        if status == 0:
            status = _special_select_owned(slots, tokens, 4, name, 5, record)
        if status == 0:
            if load_i64(record, 0) == 0:
                status = 1
            elif load_i64(record, 0) == 3:
                status = _special_error(cstr("tuple attribute requires a managed descriptor"))
            else:
                status = _special_bind_attribute(slots, tokens, name, 0)
                if status == 0:
                    status = _special_publish(slots, tokens, output)
    if status < 0:
        _special_error(cstr("tuple attribute lookup failed without an exception"))
    _special_close(slots, tokens, handles, count, suspended)
    value = load_ptr(output, 0)
    prior: int = 0
    if ptr_is_null(value) == 0 and is_tagged_int(value) == 0:
        prior = load_i32(value, PYOBJECTHEADER_FLAGS_OFFSET) & 64
        pcc_gc_pin(value)
    pcc_gc_frame_leave(output)
    pcc_gc_frame_leave(borrowed)
    return pcc_gc_take_pinned_slot(output, prior)




# First-class string search descriptors. These are the supported family, not
# a claim that the complete str namespace has been implemented.
_STR_METHOD_RECEIVER = 1
_STR_METHOD_ARGS = 2
_STR_METHOD_ARGUMENT = 3
_STR_METHOD_TEXT = 4
_STR_METHOD_NEEDLE = 5
_STR_METHOD_START = 6
_STR_METHOD_END = 7
_STR_METHOD_ITEM = 8
_STR_METHOD_CLASS = 10
_STR_METHOD_RESULT = 12
_STR_METHOD_COUNT = 0
_STR_METHOD_STARTSWITH = 1
_STR_METHOD_ENDSWITH = 2

define_global_i32("pcc_str_method_borrowed_map", -1)
define_global_i32("pcc_str_method_result_map", 1)


def _str_method_payload(slots, tokens, source_index: int, output_index: int) -> int:
    value = load_ptr(slots, source_index * C_POINTER_SIZE)
    if ptr_is_null(value) != 0 or is_tagged_int(value) != 0:
        py_raise_owned(py_exc_new(3, cstr("string method requires a str argument")))
        return -1
    if load_i32(value, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_STR:
        return _special_copy(slots, tokens, output_index,
            ptr_add(slots, source_index * C_POINTER_SIZE), 0)
    if not _ptr_is_instance(value):
        py_raise_owned(py_exc_new(3, cstr("string method requires a str argument")))
        return -1
    if _special_copy(slots, tokens, _STR_METHOD_CLASS,
            ptr_add(value, PYINSTANCEOBJECT_CLS_OFFSET), 1) != 0:
        return -1
    cls = load_ptr(slots, _STR_METHOD_CLASS * C_POINTER_SIZE)
    if py_class_is_str_subclass(cls) == 0:
        py_raise_owned(py_exc_new(3, cstr("string method requires a str argument")))
        return -1
    value = load_ptr(slots, source_index * C_POINTER_SIZE)
    source = _instance_builtin_payload_slot(value, cls)
    if _special_copy(slots, tokens, output_index, source, 0) != 0:
        return -1
    _special_drop(slots, tokens, _STR_METHOD_CLASS)
    if ptr_is_null(load_ptr(slots, output_index * C_POINTER_SIZE)) != 0:
        return _special_error(cstr("str subtype has no initialized payload"))
    return 0


def _str_method_match_one(slots, tokens, source_index: int, start: int, end: int, suffix: int) -> int:
    if _str_method_payload(slots, tokens, source_index, _STR_METHOD_NEEDLE) != 0:
        return -1
    result: int = py_str_tailmatch_range(
        load_ptr(slots, _STR_METHOD_TEXT * C_POINTER_SIZE),
        load_ptr(slots, _STR_METHOD_NEEDLE * C_POINTER_SIZE), start, end, suffix)
    _special_drop(slots, tokens, _STR_METHOD_NEEDLE)
    return result


def _str_method_body(slots, tokens, operation: int) -> int:
    args = load_ptr(slots, _STR_METHOD_ARGS * C_POINTER_SIZE)
    nargs: int = 0
    if ptr_is_null(args) == 0 and is_tagged_int(args) == 0:
        if load_i32(args, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_TUPLE:
            nargs = py_tuple_len(args)
    # The namespace function is unbound; ordinary descriptor binding prepends
    # exactly one receiver. Its signature is entirely positional-only.
    if nargs < 2 or nargs > 4:
        py_raise_owned(py_exc_new(3, cstr("string search method requires 1 to 3 arguments")))
        return -1
    if _special_tuple_item(slots, tokens, _STR_METHOD_RECEIVER, _STR_METHOD_ARGS, 0) != 0:
        return -1
    if _str_method_payload(slots, tokens, _STR_METHOD_RECEIVER, _STR_METHOD_TEXT) != 0:
        return -1
    if _special_tuple_item(slots, tokens, _STR_METHOD_ARGUMENT, _STR_METHOD_ARGS, 1) != 0:
        return -1
    # count validates its string argument before invoking either __index__;
    # startswith/endswith validate their prefix only after both bounds.
    if operation == _STR_METHOD_COUNT:
        if _str_method_payload(slots, tokens, _STR_METHOD_ARGUMENT, _STR_METHOD_NEEDLE) != 0:
            return -1
    start: int = 0
    end: int = 9223372036854775807
    if nargs >= 3:
        if _special_tuple_item(slots, tokens, _STR_METHOD_START, _STR_METHOD_ARGS, 2) != 0:
            return -1
        start = py_slice_index_i64(load_ptr(slots, _STR_METHOD_START * C_POINTER_SIZE), 0)
        if py_err_occurred() != 0:
            return -1
    if nargs == 4:
        if _special_tuple_item(slots, tokens, _STR_METHOD_END, _STR_METHOD_ARGS, 3) != 0:
            return -1
        end = py_slice_index_i64(load_ptr(slots, _STR_METHOD_END * C_POINTER_SIZE), end)
        if py_err_occurred() != 0:
            return -1
    result: int = 0
    if operation == _STR_METHOD_COUNT:
        # The older counting primitive takes boxed machine bounds. Normalize
        # through the shared slice protocol first, while every source is leased.
        _special_drop(slots, tokens, _STR_METHOD_START)
        _special_drop(slots, tokens, _STR_METHOD_END)
        store_ptr(slots, _STR_METHOD_START * C_POINTER_SIZE, py_int_from_i64(start))
        if _special_adopt(slots, tokens, _STR_METHOD_START) != 0:
            return -1
        if ptr_is_null(load_ptr(slots, _STR_METHOD_START * C_POINTER_SIZE)) != 0:
            return _special_error(cstr("string count start allocation failed"))
        store_ptr(slots, _STR_METHOD_END * C_POINTER_SIZE, py_int_from_i64(end))
        if _special_adopt(slots, tokens, _STR_METHOD_END) != 0:
            return -1
        if ptr_is_null(load_ptr(slots, _STR_METHOD_END * C_POINTER_SIZE)) != 0:
            return _special_error(cstr("string count end allocation failed"))
        result = py_str_count_range(load_ptr(slots, _STR_METHOD_TEXT * C_POINTER_SIZE),
            load_ptr(slots, _STR_METHOD_NEEDLE * C_POINTER_SIZE),
            load_ptr(slots, _STR_METHOD_START * C_POINTER_SIZE),
            load_ptr(slots, _STR_METHOD_END * C_POINTER_SIZE))
        store_ptr(slots, _STR_METHOD_RESULT * C_POINTER_SIZE, py_int_from_i64(result))
    else:
        suffix: int = 1 if operation == _STR_METHOD_ENDSWITH else 0
        argument = load_ptr(slots, _STR_METHOD_ARGUMENT * C_POINTER_SIZE)
        is_tuple: int = 0
        if ptr_is_null(argument) == 0 and is_tagged_int(argument) == 0:
            is_tuple = 1 if load_i32(argument, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_TUPLE else 0
        if is_tuple != 0:
            length: int = py_tuple_len(argument)
            index: int = 0
            while index < length:
                if _special_tuple_item(slots, tokens, _STR_METHOD_ITEM, _STR_METHOD_ARGUMENT, index) != 0:
                    return -1
                result = _str_method_match_one(slots, tokens, _STR_METHOD_ITEM, start, end, suffix)
                _special_drop(slots, tokens, _STR_METHOD_ITEM)
                if result != 0:
                    break
                index = index + 1
        else:
            result = _str_method_match_one(slots, tokens, _STR_METHOD_ARGUMENT, start, end, suffix)
        if result < 0 or py_err_occurred() != 0:
            return -1
        store_ptr(slots, _STR_METHOD_RESULT * C_POINTER_SIZE, py_bool_from_bit(result))
    return _special_adopt(slots, tokens, _STR_METHOD_RESULT)


def _str_method_entry(captures: c_ptr, args: c_ptr) -> c_ptr:
    # Operation is an immediate tagged integer; read it before any safepoint.
    operation: int = py_int_value_i64(load_ptr(captures, PYTUPLEOBJECT_ITEMS_OFFSET))
    borrowed = stack_alloc(C_POINTER_SIZE)
    store_ptr(borrowed, 0, args)
    pcc_gc_frame_enter(global_addr("pcc_str_method_borrowed_map"), borrowed)
    output = stack_alloc(C_POINTER_SIZE)
    store_ptr(output, 0, null())
    pcc_gc_frame_enter(global_addr("pcc_str_method_result_map"), output)
    slots = stack_alloc(14 * C_POINTER_SIZE)
    tokens = stack_alloc(14 * C_POINTER_SIZE)
    handles = stack_alloc(14 * C_POINTER_SIZE)
    count: int = _special_open(slots, tokens, handles)
    status: int = -1
    suspended: int = 0
    if count == 14:
        py_tls_exc_swap_slot(slots)
        suspended = 1
        status = _special_copy(slots, tokens, _STR_METHOD_ARGS, borrowed, 1)
        if status == 0:
            status = _str_method_body(slots, tokens, operation)
        if status == 0:
            status = _special_publish(slots, tokens, output)
    if status != 0:
        _special_error(cstr("string descriptor failed without an exception"))
    _special_close(slots, tokens, handles, count, suspended)
    value = load_ptr(output, 0)
    prior: int = 0
    if ptr_is_null(value) == 0 and is_tagged_int(value) == 0:
        prior = load_i32(value, PYOBJECTHEADER_FLAGS_OFFSET) & 64
        pcc_gc_pin(value)
    pcc_gc_frame_leave(output)
    pcc_gc_frame_leave(borrowed)
    return pcc_gc_take_pinned_slot(output, prior)


_STR_TYPE_CLASS = 1
_STR_TYPE_CAPTURES = 2
_STR_TYPE_FUNCTION = 3
_STR_TYPE_RESULT = 12


def _str_type_install_method(slots, tokens, operation: int) -> int:
    name = cstr("count")
    if operation == _STR_METHOD_STARTSWITH:
        name = cstr("startswith")
    elif operation == _STR_METHOD_ENDSWITH:
        name = cstr("endswith")
    if _special_tuple_new(slots, tokens, _STR_TYPE_CAPTURES, 1) != 0:
        return -1
    py_tuple_set_item(load_ptr(slots, _STR_TYPE_CAPTURES * C_POINTER_SIZE), 0, py_int_from_i64(operation))
    store_ptr(slots, _STR_TYPE_FUNCTION * C_POINTER_SIZE,
        py_func_new_bound(_str_method_entry, load_ptr(slots, _STR_TYPE_CAPTURES * C_POINTER_SIZE), name, null()))
    if _special_adopt(slots, tokens, _STR_TYPE_FUNCTION) != 0:
        return -1
    if ptr_is_null(load_ptr(slots, _STR_TYPE_FUNCTION * C_POINTER_SIZE)) != 0:
        return _special_error(cstr("string descriptor allocation failed"))
    if py_class_write_namespace_slots(ptr_add(slots, _STR_TYPE_CLASS * C_POINTER_SIZE), name,
            ptr_add(slots, _STR_TYPE_FUNCTION * C_POINTER_SIZE), 0) != 0:
        return -1
    # The owning namespace is canonical. The native table is only a borrowed
    # compatibility alias, matching the existing instance/class lookup paths.
    cls = load_ptr(slots, _STR_TYPE_CLASS * C_POINTER_SIZE)
    before: int = load_i32(cls, PYCLASSOBJECT_N_METHODS_OFFSET)
    py_class_add_method(cls, name, load_ptr(slots, _STR_TYPE_FUNCTION * C_POINTER_SIZE))
    cls = load_ptr(slots, _STR_TYPE_CLASS * C_POINTER_SIZE)
    if load_i32(cls, PYCLASSOBJECT_N_METHODS_OFFSET) != before + 1:
        return _special_error(cstr("string descriptor table publication failed"))
    _special_drop(slots, tokens, _STR_TYPE_FUNCTION)
    _special_drop(slots, tokens, _STR_TYPE_CAPTURES)
    return -1 if py_err_occurred() != 0 else 0


def _str_type_fill(slots, tokens) -> int:
    if ptr_is_null(global_load_ptr("pcc_type_cls_str")) == 0:
        return _special_copy(slots, tokens, _STR_TYPE_RESULT, global_addr("pcc_type_cls_str"), 0)
    store_ptr(slots, _STR_TYPE_CLASS * C_POINTER_SIZE,
        py_class_new(cstr("str"), null(), 0, null(), 0))
    if _special_adopt(slots, tokens, _STR_TYPE_CLASS) != 0:
        return -1
    if ptr_is_null(load_ptr(slots, _STR_TYPE_CLASS * C_POINTER_SIZE)) != 0:
        return _special_error(cstr("str type allocation failed"))
    operation: int = _STR_METHOD_COUNT
    while operation <= _STR_METHOD_ENDSWITH:
        if _str_type_install_method(slots, tokens, operation) != 0:
            return -1
        operation = operation + 1
    # Failed construction never publishes a half-populated type.
    pcc_gc_store_root(global_addr("pcc_type_cls_str"), load_ptr(slots, _STR_TYPE_CLASS * C_POINTER_SIZE))
    return _special_copy(slots, tokens, _STR_TYPE_RESULT,
        ptr_add(slots, _STR_TYPE_CLASS * C_POINTER_SIZE), 0)


@c_abi_export("py_str_type_new")
def py_str_type_new() -> c_ptr:
    output = stack_alloc(C_POINTER_SIZE)
    store_ptr(output, 0, null())
    pcc_gc_frame_enter(global_addr("pcc_str_method_result_map"), output)
    slots = stack_alloc(14 * C_POINTER_SIZE)
    tokens = stack_alloc(14 * C_POINTER_SIZE)
    handles = stack_alloc(14 * C_POINTER_SIZE)
    count: int = _special_open(slots, tokens, handles)
    status: int = -1
    suspended: int = 0
    if count == 14:
        py_tls_exc_swap_slot(slots)
        suspended = 1
        mutex = _object_new_cache_mutex()
        if ptr_is_null(mutex) == 0 and pcc_mutex_lock(mutex) == 0:
            status = _str_type_fill(slots, tokens)
            pcc_mutex_unlock(mutex)
        if status == 0:
            status = _special_publish(slots, tokens, output)
    if status != 0:
        _special_error(cstr("str type initialization failed without an exception"))
    _special_close(slots, tokens, handles, count, suspended)
    value = load_ptr(output, 0)
    prior: int = 0
    if ptr_is_null(value) == 0:
        prior = load_i32(value, PYOBJECTHEADER_FLAGS_OFFSET) & 64
        pcc_gc_pin(value)
    pcc_gc_frame_leave(output)
    return pcc_gc_take_pinned_slot(output, prior)


# list.sort reached as a method object (getattr, or a call with unpacked
# arguments): CPython's list_sort_impl over this counted-lease frame.
_LIST_SORT_RECEIVER = 1
_LIST_SORT_ARGS = 2
_LIST_SORT_KWARGS = 3
_LIST_SORT_KEY = 4
_LIST_SORT_WORKING = 5
_LIST_SORT_SORTED = 6
_LIST_SORT_NAMES = 7
_LIST_SORT_NAME = 8
_LIST_SORT_VALUE = 9
_LIST_SORT_RESULT = 12

define_global_i32("pcc_list_sort_borrowed_map", -3)
py_list_copy = extern("py_list_copy", (c_ptr,), c_ptr)
py_list_clear = extern("py_list_clear", (c_ptr,), c_void)
py_list_extend = extern("py_list_extend", (c_ptr, c_ptr), c_void)


def _list_sort_keywords(slots, tokens) -> int:
    """Bind ``key=`` and ``reverse=``, the only, keyword-only, parameters.

    The reverse flag (0/1), or -1 with CPython's TypeError raised.
    """
    reverse: int = 0
    truth: int = 0
    at: int = 0
    offset: int = 0
    message = stack_alloc(160)
    kwargs = load_ptr(slots, _LIST_SORT_KWARGS * C_POINTER_SIZE)
    if ptr_is_null(kwargs) != 0 or is_tagged_int(kwargs) != 0:
        return 0
    if load_i32(kwargs, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_DICT or py_dict_len(kwargs) == 0:
        return 0
    store_ptr(slots, _LIST_SORT_NAMES * C_POINTER_SIZE, py_dict_keys(kwargs))
    if _special_adopt(slots, tokens, _LIST_SORT_NAMES) != 0:
        return -1
    if ptr_is_null(load_ptr(slots, _LIST_SORT_NAMES * C_POINTER_SIZE)) != 0:
        return _special_error(cstr("list.sort keyword names allocation failed"))
    count: int = py_list_len(load_ptr(slots, _LIST_SORT_NAMES * C_POINTER_SIZE))
    index: int = 0
    while index < count and reverse >= 0:
        store_ptr(slots, _LIST_SORT_NAME * C_POINTER_SIZE,
            py_list_get(load_ptr(slots, _LIST_SORT_NAMES * C_POINTER_SIZE), index))
        if _special_adopt(slots, tokens, _LIST_SORT_NAME) != 0:
            return -1
        name = load_ptr(slots, _LIST_SORT_NAME * C_POINTER_SIZE)
        if ptr_is_null(name) != 0 or is_tagged_int(name) != 0 or load_i32(name, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_STR:
            py_raise_owned(py_exc_new(3, cstr("keywords must be strings")))
            return -1
        store_ptr(slots, _LIST_SORT_VALUE * C_POINTER_SIZE,
            py_dict_get(load_ptr(slots, _LIST_SORT_KWARGS * C_POINTER_SIZE), name))
        if _special_adopt(slots, tokens, _LIST_SORT_VALUE) != 0:
            return -1
        text = py_str_utf8(load_ptr(slots, _LIST_SORT_NAME * C_POINTER_SIZE))
        if _strs_eq(text, cstr("key")) != 0:
            value = load_ptr(slots, _LIST_SORT_VALUE * C_POINTER_SIZE)
            if ptr_is_null(value) == 0 and ptr_eq(value, global_load_ptr("py_None")) == 0:
                if _special_copy(slots, tokens, _LIST_SORT_KEY,
                        ptr_add(slots, _LIST_SORT_VALUE * C_POINTER_SIZE), 0) != 0:
                    return -1
        elif _strs_eq(text, cstr("reverse")) != 0:
            truth = py_obj_truthy(load_ptr(slots, _LIST_SORT_VALUE * C_POINTER_SIZE))
            if py_err_occurred() != 0:
                return -1
            reverse = 1 if truth != 0 else 0
        else:
            at = 0
            prefix = cstr("sort() got an unexpected keyword argument '")
            while load_i8(prefix, at) != 0:
                store_i8(message, at, load_i8(prefix, at))
                at = at + 1
            offset = 0
            while offset < 100 and load_i8(text, offset) != 0:
                store_i8(message, at, load_i8(text, offset))
                at = at + 1
                offset = offset + 1
            store_i8(message, at, 39)
            store_i8(message, at + 1, 0)
            py_raise_owned(py_exc_new(3, message))
            return -1
        _special_drop(slots, tokens, _LIST_SORT_VALUE)
        _special_drop(slots, tokens, _LIST_SORT_NAME)
        index = index + 1
    return reverse


def _list_sort_body(slots, tokens) -> int:
    args = load_ptr(slots, _LIST_SORT_ARGS * C_POINTER_SIZE)
    if ptr_is_null(args) == 0 and is_tagged_int(args) == 0:
        if load_i32(args, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_TUPLE and py_tuple_len(args) != 0:
            py_raise_owned(py_exc_new(3, cstr("sort() takes no positional arguments")))
            return -1
    reverse: int = _list_sort_keywords(slots, tokens)
    if reverse < 0:
        return -1
    if _list_sort_apply(slots, tokens, reverse) != 0:
        return -1
    none = global_load_ptr("py_None")
    py_incref(none)
    store_ptr(slots, _LIST_SORT_RESULT * C_POINTER_SIZE, none)
    return _special_adopt(slots, tokens, _LIST_SORT_RESULT)


def _list_sort_apply(slots, tokens, reverse: int) -> int:
    """Sort the receiver slot in place by the optional key slot: 0, or -1
    with the exception set. Every operand stays in this frame's leased slots
    while keys and comparisons run arbitrary code."""
    # CPython exposes an empty receiver while keys and comparisons run; the
    # working copy owns every original item meanwhile.
    store_ptr(slots, _LIST_SORT_WORKING * C_POINTER_SIZE,
        py_list_copy(load_ptr(slots, _LIST_SORT_RECEIVER * C_POINTER_SIZE)))
    if _special_adopt(slots, tokens, _LIST_SORT_WORKING) != 0:
        return -1
    if ptr_is_null(load_ptr(slots, _LIST_SORT_WORKING * C_POINTER_SIZE)) != 0:
        return _special_error(cstr("list.sort working copy allocation failed"))
    py_list_clear(load_ptr(slots, _LIST_SORT_RECEIVER * C_POINTER_SIZE))
    key_slot = null()
    if ptr_is_null(load_ptr(slots, _LIST_SORT_KEY * C_POINTER_SIZE)) == 0:
        key_slot = ptr_add(slots, _LIST_SORT_KEY * C_POINTER_SIZE)
    # The merge sort keeps ties stable for reverse=True as well.
    status: int = py_obj_sorted_slots(ptr_add(slots, _LIST_SORT_WORKING * C_POINTER_SIZE),
        key_slot, null(), reverse, ptr_add(slots, _LIST_SORT_SORTED * C_POINTER_SIZE))
    if status == 0:
        status = _special_adopt(slots, tokens, _LIST_SORT_SORTED)
    receiver = load_ptr(slots, _LIST_SORT_RECEIVER * C_POINTER_SIZE)
    modified: int = 1 if py_list_len(receiver) != 0 else 0
    py_list_clear(receiver)
    if status != 0:
        # A key or comparison failed: the receiver keeps every original item.
        py_list_extend(load_ptr(slots, _LIST_SORT_RECEIVER * C_POINTER_SIZE),
            load_ptr(slots, _LIST_SORT_WORKING * C_POINTER_SIZE))
        return -1
    py_list_extend(load_ptr(slots, _LIST_SORT_RECEIVER * C_POINTER_SIZE),
        load_ptr(slots, _LIST_SORT_SORTED * C_POINTER_SIZE))
    if py_err_occurred() != 0:
        return -1
    if modified != 0:
        py_raise_owned(py_exc_new(2, cstr("list modified during sort")))
        return -1
    return 0


@c_abi_export("py_list_sort_keyed")
def py_list_sort_keyed(receiver, key, reverse: int) -> int:
    """``lst.sort(key=key, reverse=reverse)`` for compiled code, over this
    frame's leased slots like sorted(): 0, or -1 with the exception set. A
    NULL key sorts by the items themselves."""
    borrowed = stack_alloc(3 * C_POINTER_SIZE)
    store_ptr(borrowed, 0, receiver)
    store_ptr(borrowed, C_POINTER_SIZE, key)
    store_ptr(borrowed, 2 * C_POINTER_SIZE, null())
    pcc_gc_frame_enter(global_addr("pcc_list_sort_borrowed_map"), borrowed)
    slots = stack_alloc(14 * C_POINTER_SIZE)
    tokens = stack_alloc(14 * C_POINTER_SIZE)
    handles = stack_alloc(14 * C_POINTER_SIZE)
    count: int = _special_open(slots, tokens, handles)
    status: int = -1
    suspended: int = 0
    if count == 14:
        py_tls_exc_swap_slot(slots)
        suspended = 1
        status = _special_copy(slots, tokens, _LIST_SORT_RECEIVER, borrowed, 1)
        if status == 0 and ptr_is_null(key) == 0:
            status = _special_copy(slots, tokens, _LIST_SORT_KEY, ptr_add(borrowed, C_POINTER_SIZE), 1)
        if status == 0:
            status = _list_sort_apply(slots, tokens, 1 if reverse != 0 else 0)
    if status != 0:
        _special_error(cstr("list.sort failed without an exception"))
    _special_close(slots, tokens, handles, count, suspended)
    pcc_gc_frame_leave(borrowed)
    return status


@c_abi_export("py_list_sort_call")
def py_list_sort_call(receiver, args, kwargs) -> c_ptr:
    """``list.sort(*, key=None, reverse=False)`` called through a method
    object: None, or NULL with the exception set."""
    borrowed = stack_alloc(3 * C_POINTER_SIZE)
    store_ptr(borrowed, 0, receiver)
    store_ptr(borrowed, C_POINTER_SIZE, args)
    store_ptr(borrowed, 2 * C_POINTER_SIZE, kwargs)
    pcc_gc_frame_enter(global_addr("pcc_list_sort_borrowed_map"), borrowed)
    output = stack_alloc(C_POINTER_SIZE)
    store_ptr(output, 0, null())
    pcc_gc_frame_enter(global_addr("pcc_str_method_result_map"), output)
    slots = stack_alloc(14 * C_POINTER_SIZE)
    tokens = stack_alloc(14 * C_POINTER_SIZE)
    handles = stack_alloc(14 * C_POINTER_SIZE)
    count: int = _special_open(slots, tokens, handles)
    status: int = -1
    suspended: int = 0
    if count == 14:
        py_tls_exc_swap_slot(slots)
        suspended = 1
        status = _special_copy(slots, tokens, _LIST_SORT_RECEIVER, borrowed, 1)
        if status == 0:
            status = _special_copy(slots, tokens, _LIST_SORT_ARGS, ptr_add(borrowed, C_POINTER_SIZE), 1)
        if status == 0:
            status = _special_copy(slots, tokens, _LIST_SORT_KWARGS, ptr_add(borrowed, 2 * C_POINTER_SIZE), 1)
        if status == 0:
            status = _list_sort_body(slots, tokens)
        if status == 0:
            status = _special_publish(slots, tokens, output)
    if status != 0:
        _special_error(cstr("list.sort failed without an exception"))
    _special_close(slots, tokens, handles, count, suspended)
    value = load_ptr(output, 0)
    prior: int = 0
    if ptr_is_null(value) == 0 and is_tagged_int(value) == 0:
        prior = load_i32(value, PYOBJECTHEADER_FLAGS_OFFSET) & 64
        pcc_gc_pin(value)
    pcc_gc_frame_leave(output)
    pcc_gc_frame_leave(borrowed)
    return pcc_gc_take_pinned_slot(output, prior)


@c_abi_export("py_str_getattr")
def py_str_getattr(receiver: c_ptr, name: c_ptr) -> c_ptr:
    # Do not expose a base-object fallback as an unimplemented str descriptor.
    if (_strs_eq(name, cstr("count")) == 0
            and _strs_eq(name, cstr("startswith")) == 0
            and _strs_eq(name, cstr("endswith")) == 0):
        return null()
    # Exact str only. Subclasses retain ordinary hooks, instance attributes,
    # overrides, and C3 lookup through their namespace-owned base descriptors.
    borrowed = stack_alloc(C_POINTER_SIZE)
    store_ptr(borrowed, 0, receiver)
    pcc_gc_frame_enter(global_addr("pcc_str_method_borrowed_map"), borrowed)
    output = stack_alloc(C_POINTER_SIZE)
    store_ptr(output, 0, null())
    pcc_gc_frame_enter(global_addr("pcc_str_method_result_map"), output)
    slots = stack_alloc(14 * C_POINTER_SIZE)
    tokens = stack_alloc(14 * C_POINTER_SIZE)
    handles = stack_alloc(14 * C_POINTER_SIZE)
    count: int = _special_open(slots, tokens, handles)
    status: int = -1
    suspended: int = 0
    if count == 14:
        py_tls_exc_swap_slot(slots)
        suspended = 1
        status = _special_copy(slots, tokens, 1, borrowed, 1)
        if status == 0:
            store_ptr(slots, 4 * C_POINTER_SIZE, py_str_type_new())
            status = _special_adopt(slots, tokens, 4)
        record = stack_alloc(2 * C_POINTER_SIZE)
        memset(record, 0, 2 * C_POINTER_SIZE)
        if status == 0 and ptr_is_null(load_ptr(slots, 4 * C_POINTER_SIZE)) == 0:
            plan = stack_alloc(256)
            prepared: int = 0
            pcc_py_gc_minor_graph_lock()
            source = _special_lookup_locked(load_ptr(slots, 4 * C_POINTER_SIZE), name, record)
            kind: int = load_i64(record, 0)
            if kind == 1 or kind == 2:
                token: int = pcc_gc_root_copy_lease_prepare_locked(ptr_add(slots, 5 * C_POINTER_SIZE),
                    source, 1 if kind == 2 else 0, plan)
                prepared = 1
                if token < 0:
                    status = -1
                else:
                    store_i64(tokens, 5 * C_POINTER_SIZE, token)
            pcc_py_gc_minor_graph_unlock()
            if prepared != 0:
                pcc_gc_root_copy_lease_finish(plan)
            if status == 0:
                if kind == 0:
                    status = 1
                elif kind == 3:
                    status = _special_error(cstr("str attribute requires a managed descriptor"))
                else:
                    status = _special_bind_attribute(slots, tokens, name, 0)
                    if status == 0:
                        status = _special_publish(slots, tokens, output)
        elif status == 0:
            status = -1
    if status < 0:
        _special_error(cstr("str attribute lookup failed without an exception"))
    _special_close(slots, tokens, handles, count, suspended)
    value = load_ptr(output, 0)
    prior: int = 0
    if ptr_is_null(value) == 0 and is_tagged_int(value) == 0:
        prior = load_i32(value, PYOBJECTHEADER_FLAGS_OFFSET) & 64
        pcc_gc_pin(value)
    pcc_gc_frame_leave(output)
    pcc_gc_frame_leave(borrowed)
    return pcc_gc_take_pinned_slot(output, prior)


# Struct sequences retain an ordinary immutable tuple payload and keep their
# non-sequence fields in the existing, separately traced reserved owner. They
# are slots-only classes: no dynamic dictionary is exposed or synthesized.
_STRUCTSEQ_RECEIVER = 1
_STRUCTSEQ_HIDDEN = 2
_STRUCTSEQ_VISIBLE = 3
_STRUCTSEQ_RESULT = 12
_STRUCTSEQ_SCRATCH_COUNT = 14
_STRUCTSEQ_RETURN_COUNT = 2
_STRUCTSEQ_RETURN_ENTRY_EXCEPTION = 1

define_global_i32("pcc_structseq_borrowed_map", -3)
define_global_i32("pcc_structseq_result_map", _STRUCTSEQ_RETURN_COUNT)


def _structseq_exact_tuple(value) -> bool:
    return (ptr_is_null(value) == 0 and is_tagged_int(value) == 0
            and load_i32(value, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_TUPLE)


def _structseq_body(slots, tokens, create: int) -> int:
    value = load_ptr(slots, _STRUCTSEQ_RECEIVER * C_POINTER_SIZE)
    if create == 2:
        status: int = py_dict_storage_slots(
            ptr_add(slots, _STRUCTSEQ_RECEIVER * C_POINTER_SIZE),
            ptr_add(slots, _STRUCTSEQ_RESULT * C_POINTER_SIZE))
        if status != 0:
            return -1
        if ptr_is_null(load_ptr(slots, _STRUCTSEQ_RESULT * C_POINTER_SIZE)):
            py_raise_owned(py_exc_new(3, cstr("struct sequence metadata requires dictionary storage")))
            return -1
        return _special_adopt(slots, tokens, _STRUCTSEQ_RESULT)
    if create == 1:
        if not _class_is_structseq(value):
            py_raise_owned(py_exc_new(3, cstr("struct sequence type required")))
            return -1
        if not _structseq_exact_tuple(load_ptr(slots, _STRUCTSEQ_VISIBLE * C_POINTER_SIZE)) or not _structseq_exact_tuple(load_ptr(slots, _STRUCTSEQ_HIDDEN * C_POINTER_SIZE)):
            py_raise_owned(py_exc_new(3, cstr("struct sequence fields require exact tuple owners")))
            return -1
        store_ptr(slots, _STRUCTSEQ_RESULT * C_POINTER_SIZE, py_instance_new(value))
        if _special_adopt(slots, tokens, _STRUCTSEQ_RESULT) != 0:
            return -1
        instance = load_ptr(slots, _STRUCTSEQ_RESULT * C_POINTER_SIZE)
        if ptr_is_null(instance) != 0:
            return _special_error(cstr("struct sequence allocation failed"))
        cls = load_ptr(slots, _STRUCTSEQ_RECEIVER * C_POINTER_SIZE)
        payload_slot = _instance_builtin_payload_slot(instance, cls)
        hidden_slot = _instance_reserved_owner_slot(instance, cls)
        if ptr_is_null(payload_slot) != 0:
            return _special_error(cstr("struct sequence tuple payload owner is missing"))
        # All three source owners and the fresh instance have counted leases.
        # Publication cannot expose an incomplete object: it has not escaped.
        pcc_gc_store_ptr(instance, payload_slot,
            load_ptr(slots, _STRUCTSEQ_VISIBLE * C_POINTER_SIZE))
        pcc_gc_store_ptr(instance, hidden_slot,
            load_ptr(slots, _STRUCTSEQ_HIDDEN * C_POINTER_SIZE))
        return -1 if py_err_occurred() else 0
    if not _special_native_instance(value):
        py_raise_owned(py_exc_new(3, cstr("struct sequence receiver required")))
        return -1
    cls = pcc_gc_load_ptr(value, ptr_add(value, PYINSTANCEOBJECT_CLS_OFFSET))
    if not _class_is_structseq(cls):
        py_raise_owned(py_exc_new(3, cstr("struct sequence receiver required")))
        return -1
    owner_slot = _instance_reserved_owner_slot(value, cls)
    if ptr_is_null(load_ptr(owner_slot, 0)) != 0:
        return _special_error(cstr("struct sequence hidden fields are not initialized"))
    return _special_copy(slots, tokens, _STRUCTSEQ_RESULT, owner_slot, 1)


def _structseq_c_object(receiver, visible, hidden, create: int):
    borrowed = stack_alloc(3 * C_POINTER_SIZE)
    store_ptr(borrowed, 0, receiver)
    store_ptr(borrowed, C_POINTER_SIZE, hidden)
    store_ptr(borrowed, 2 * C_POINTER_SIZE, visible)
    pcc_gc_frame_enter(global_addr("pcc_structseq_borrowed_map"), borrowed)
    output = stack_alloc(_STRUCTSEQ_RETURN_COUNT * C_POINTER_SIZE)
    memset(output, 0, _STRUCTSEQ_RETURN_COUNT * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_structseq_result_map"), output)
    entry_exception = ptr_add(output, _STRUCTSEQ_RETURN_ENTRY_EXCEPTION * C_POINTER_SIZE)
    # Snapshot the incoming exception before any fallible handle registration.
    # It has a distinct owner even if the first scratch root cannot register.
    py_tls_exc_swap_slot(entry_exception)
    slots = stack_alloc(_STRUCTSEQ_SCRATCH_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_STRUCTSEQ_SCRATCH_COUNT * C_POINTER_SIZE)
    handles = stack_alloc(_STRUCTSEQ_SCRATCH_COUNT * C_POINTER_SIZE)
    count: int = _special_open(slots, tokens, handles)
    status: int = -1
    suspended: int = 0
    if count == _STRUCTSEQ_SCRATCH_COUNT:
        status = pcc_gc_root_move(slots, entry_exception)
        if status == 0:
            suspended = 1
            status = _special_copy(slots, tokens, _STRUCTSEQ_RECEIVER, borrowed, 1)
        if status == 0 and create == 1:
            status = _special_copy(slots, tokens, _STRUCTSEQ_HIDDEN, ptr_add(borrowed, C_POINTER_SIZE), 1)
        if status == 0 and create == 1:
            status = _special_copy(slots, tokens, _STRUCTSEQ_VISIBLE, ptr_add(borrowed, 2 * C_POINTER_SIZE), 1)
        if status == 0:
            status = _structseq_body(slots, tokens, create)
        if status == 0:
            status = _special_publish(slots, tokens, output)
    if status != 0:
        # Application c_obj externs return one owned exception object on error;
        # the semantic wrapper raises it under its ordinary Python handlers.
        _special_error(cstr("struct sequence operation failed without an exception"))
        py_tls_exc_swap_slot(output)
    _special_close(slots, tokens, handles, count, suspended)
    if suspended == 0:
        py_clear_exception()
        py_tls_exc_swap_slot(entry_exception)
    pcc_py_gc_minor_graph_lock()
    value = load_ptr(output, 0)
    prior: int = 0
    if ptr_is_null(value) == 0 and is_tagged_int(value) == 0:
        prior = load_i32(value, PYOBJECTHEADER_FLAGS_OFFSET) & PY_FLAG_GC_PINNED
        pcc_gc_pin(value)
    pcc_py_gc_minor_graph_unlock()
    pcc_gc_frame_leave(output)
    pcc_gc_frame_leave(borrowed)
    return pcc_gc_take_pinned_slot(output, prior)


@c_abi_export("py_structseq_new")
def py_structseq_new(cls, visible, hidden):
    return _structseq_c_object(cls, visible, hidden, 1)


@c_abi_export("py_structseq_hidden")
def py_structseq_hidden(receiver):
    return _structseq_c_object(receiver, null(), null(), 0)


@c_abi_export("py_class_mark_structseq")
def py_class_mark_structseq(cls) -> None:
    # Class publication still has an authoritative root. Validation performs
    # no allocation on success and no managed pointer survives its error.
    if not _ptr_is_class(cls) or py_class_is_tuple_subclass(cls) == 0:
        py_raise_owned(py_exc_new(3, cstr("struct sequence requires a tuple subtype")))
        return
    if load_i32(cls, PYCLASSOBJECT_N_FIELDS_OFFSET) != 0 or (load_i32(cls, PYOBJECTHEADER_FLAGS_OFFSET) & 2) == 0:
        py_raise_owned(py_exc_new(3, cstr("struct sequence requires an empty slots-only layout")))
        return
    atomic_rmw_i32("or", cls, PYOBJECTHEADER_FLAGS_OFFSET, _STRUCTSEQ_CLASS_FLAG, "relaxed")


@c_abi_export("py_structseq_dict_storage")
def py_structseq_dict_storage(mapping):
    return _structseq_c_object(mapping, null(), null(), 2)
