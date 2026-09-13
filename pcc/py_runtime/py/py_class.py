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

from pcc.extern import extern, c_abi_export, c_ptr, c_int32, c_int64, c_void
from pcc.py_runtime.py.py_abi_constants import (
    C_POINTER_SIZE,
    DICTENTRY_KEY_OFFSET,
    DICTENTRY_SIZE,
    DICTENTRY_VALUE_OFFSET,
    PYCLASSMETHOD_FUNC_OFFSET,
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
    PY_TYPE_LIST,
    PY_TYPE_PROPERTY,
    PY_TYPE_STATICMETHOD,
    PY_TYPE_STR,
    PY_TYPE_TUPLE,
    PY_TYPE_USER_CLASS_START,
    PY_TYPE_VALUEBOX,
)
from pcc.unsafe import (
    atomic_load_i32,
    atomic_rmw_i32,
    cstr,
    define_global_i32,
    call_ptr1,
    call_ptr2,
    call_ptr3,
    call_ptr4,
    free,
    global_addr,
    global_load_ptr,
    global_store_ptr,
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
    ptr_to_int,
    realloc,
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
py_dict_subclass_getattr = extern("py_dict_subclass_getattr", (c_ptr, c_ptr), c_ptr)
py_func_new_bound = extern(
    "py_func_new_bound", (c_ptr, c_ptr, c_ptr, c_ptr), c_ptr
)
py_class_new_abi = extern(
    "py_class_new", (c_ptr, c_ptr, c_int32, c_ptr, c_int32), c_ptr
)
py_dict_new = extern("py_dict_new", (), c_ptr)
py_dict_set = extern("py_dict_set", (c_ptr, c_ptr, c_ptr), c_void)
py_dict_get = extern("py_dict_get", (c_ptr, c_ptr), c_ptr)
py_dict_keys = extern("py_dict_keys", (c_ptr,), c_ptr)
py_dict_update = extern("py_dict_update", (c_ptr, c_ptr), c_void)
py_dict_del = extern("py_dict_del", (c_ptr, c_ptr), c_int64)
py_list_len = extern("py_list_len", (c_ptr,), c_int64)
py_list_get = extern("py_list_get", (c_ptr, c_int64), c_ptr)
py_gc_track = extern("py_gc_track", (c_ptr,), c_void)
py_gc_untrack = extern("py_gc_untrack", (c_ptr,), c_void)
pcc_gc_note_object_freeing = extern(
    "pcc_gc_note_object_freeing", (c_ptr,), c_void
)
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
    slot = global_addr("py_next_user_tag")
    tag: int = load_i32(slot, 0)
    store_i32(slot, 0, tag + 1)
    return tag


def _object_root():
    root = global_load_ptr("py_object_root_cache")
    if ptr_is_null(root) == 0:
        return root

    mro = malloc(C_POINTER_SIZE)
    if ptr_is_null(mro) != 0:
        return null()
    # This root is cached in a raw global pointer, not a relocation-updated
    # root slot.  Give it stable storage and register exact provenance.
    r = malloc(PYCLASSOBJECT_SIZE)
    if ptr_is_null(r) != 0:
        free(mro)
        return null()
    memset(r, 0, PYCLASSOBJECT_SIZE)
    store_i64(r, PYOBJECTHEADER_REFCOUNT_OFFSET, 1)
    store_i32(r, PYOBJECTHEADER_TYPE_TAG_OFFSET, PY_TYPE_CLASS)
    store_i32(
        r,
        PYOBJECTHEADER_FLAGS_OFFSET,
        PY_FLAG_IMMORTAL | PY_FLAG_GC_MALLOC_ALLOC,
    )
    if pcc_gc_pointer_register(r) < 0:
        free(r)
        free(mro)
        return null()
    store_ptr(r, PYCLASSOBJECT_NAME_OFFSET, cstr("object"))
    store_i32(r, PYCLASSOBJECT_N_BASES_OFFSET, 0)
    store_ptr(r, PYCLASSOBJECT_BASES_OFFSET, null())
    store_i32(r, PYCLASSOBJECT_N_MRO_OFFSET, 1)

    store_ptr(mro, 0, r)
    store_ptr(r, PYCLASSOBJECT_MRO_OFFSET, mro)

    store_i32(r, PYCLASSOBJECT_N_METHODS_OFFSET, 0)
    store_ptr(r, PYCLASSOBJECT_METHODS_OFFSET, null())
    store_i32(r, PYCLASSOBJECT_N_FIELDS_OFFSET, 0)
    store_ptr(r, PYCLASSOBJECT_FIELD_NAMES_OFFSET, null())
    store_i32(
        r,
        PYCLASSOBJECT_INSTANCE_SIZE_OFFSET,
        PYINSTANCEOBJECT_SIZE + C_POINTER_SIZE,
    )
    store_i32(r, PYCLASSOBJECT_TYPE_TAG_ALLOC_OFFSET, PY_TYPE_INSTANCE)
    store_ptr(r, PYCLASSOBJECT_DEL_METHOD_OFFSET, null())
    store_ptr(r, PYCLASSOBJECT_ATTRS_OFFSET, null())

    global_store_ptr("py_object_root_cache", r)
    return r


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


def _cstr_is_dunder_name(s) -> int:
    if strlen(s) != 8:
        return 0
    if load_i8(s, 0) != 95:
        return 0
    if load_i8(s, 1) != 95:
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
    if strlen(s) != 7:
        return 0
    if load_i8(s, 0) != 95:
        return 0
    if load_i8(s, 1) != 95:
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
    if strlen(s) != 9:
        return 0
    if load_i8(s, 0) != 95:
        return 0
    if load_i8(s, 1) != 95:
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
    if strlen(s) != 8:
        return 0
    if load_i8(s, 0) != 95:
        return 0
    if load_i8(s, 1) != 95:
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


def _func_signature_valid(signature) -> bool:
    if not _ptr_can_have_header(signature):
        return False
    if load_i32(signature, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_TUPLE:
        return False
    if py_tuple_len(signature) < 5:
        return False
    magic = py_tuple_get(signature, 0)
    if ptr_is_null(magic) != 0:
        return False
    expected = py_str_new(cstr("__pcc_func_signature_v1__"), 25)
    ok: int = 0
    if ptr_is_null(expected) == 0:
        ok = py_str_eq(magic, expected)
        py_decref(expected)
    py_decref(magic)
    return ok != 0


def _func_signature(func):
    if not _ptr_can_have_header(func):
        return null()
    if load_i32(func, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_FUNC:
        return null()
    captures = pcc_gc_load_ptr(func, ptr_add(func, 64))
    if not _ptr_can_have_header(captures):
        return null()
    if load_i32(captures, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_TUPLE or py_tuple_len(captures) != 2:
        return null()
    candidate = py_tuple_get(captures, 1)
    if not _func_signature_valid(candidate):
        if ptr_is_null(candidate) == 0:
            py_decref(candidate)
        return null()
    return candidate


def _bound_signature(func):
    signature = _func_signature(func)
    if ptr_is_null(signature) != 0:
        return null()
    names = py_tuple_get(signature, 1)
    kinds = py_tuple_get(signature, 2)
    has_defaults = py_tuple_get(signature, 3)
    defaults = py_tuple_get(signature, 4)
    if (
        ptr_is_null(names) != 0
        or ptr_is_null(kinds) != 0
        or ptr_is_null(has_defaults) != 0
        or ptr_is_null(defaults) != 0
    ):
        if ptr_is_null(names) == 0:
            py_decref(names)
        if ptr_is_null(kinds) == 0:
            py_decref(kinds)
        if ptr_is_null(has_defaults) == 0:
            py_decref(has_defaults)
        if ptr_is_null(defaults) == 0:
            py_decref(defaults)
        py_decref(signature)
        return null()
    n: int = py_tuple_len(names)
    if (
        n <= 0
        or py_tuple_len(kinds) != n
        or py_tuple_len(has_defaults) != n
        or py_tuple_len(defaults) != n
    ):
        py_decref(names)
        py_decref(kinds)
        py_decref(has_defaults)
        py_decref(defaults)
        py_decref(signature)
        return null()
    out_names = py_tuple_new(n - 1)
    out_kinds = py_tuple_new(n - 1)
    out_has_defaults = py_tuple_new(n - 1)
    out_defaults = py_tuple_new(n - 1)
    out_signature = py_tuple_new(5)
    if (
        ptr_is_null(out_names) != 0
        or ptr_is_null(out_kinds) != 0
        or ptr_is_null(out_has_defaults) != 0
        or ptr_is_null(out_defaults) != 0
        or ptr_is_null(out_signature) != 0
    ):
        if ptr_is_null(out_names) == 0:
            py_decref(out_names)
        if ptr_is_null(out_kinds) == 0:
            py_decref(out_kinds)
        if ptr_is_null(out_has_defaults) == 0:
            py_decref(out_has_defaults)
        if ptr_is_null(out_defaults) == 0:
            py_decref(out_defaults)
        if ptr_is_null(out_signature) == 0:
            py_decref(out_signature)
        py_decref(names)
        py_decref(kinds)
        py_decref(has_defaults)
        py_decref(defaults)
        py_decref(signature)
        return null()
    i: int = 1
    valid: int = 1
    while i < n:
        name = py_tuple_get(names, i)
        kind = py_tuple_get(kinds, i)
        has_default = py_tuple_get(has_defaults, i)
        default_obj = py_tuple_get(defaults, i)
        if (
            ptr_is_null(name) != 0
            or ptr_is_null(kind) != 0
            or ptr_is_null(has_default) != 0
            or ptr_is_null(default_obj) != 0
        ):
            valid = 0
        if valid != 0:
            py_tuple_set_item(out_names, i - 1, name)
            py_tuple_set_item(out_kinds, i - 1, kind)
            py_tuple_set_item(out_has_defaults, i - 1, has_default)
            py_tuple_set_item(out_defaults, i - 1, default_obj)
        if ptr_is_null(name) == 0:
            py_decref(name)
        if ptr_is_null(kind) == 0:
            py_decref(kind)
        if ptr_is_null(has_default) == 0:
            py_decref(has_default)
        if ptr_is_null(default_obj) == 0:
            py_decref(default_obj)
        if valid == 0:
            i = n
        i = i + 1
    magic = null()
    if valid != 0:
        magic = py_tuple_get(signature, 0)
        if ptr_is_null(magic) != 0:
            valid = 0
    if valid != 0:
        py_tuple_set_item(out_signature, 0, magic)
        py_tuple_set_item(out_signature, 1, out_names)
        py_tuple_set_item(out_signature, 2, out_kinds)
        py_tuple_set_item(out_signature, 3, out_has_defaults)
        py_tuple_set_item(out_signature, 4, out_defaults)
    if ptr_is_null(magic) == 0:
        py_decref(magic)
    py_decref(out_names)
    py_decref(out_kinds)
    py_decref(out_has_defaults)
    py_decref(out_defaults)
    py_decref(names)
    py_decref(kinds)
    py_decref(has_defaults)
    py_decref(defaults)
    py_decref(signature)
    if valid == 0:
        py_decref(out_signature)
        return null()
    return out_signature


def _wrap_bound_captures(method, captures):
    signature = _bound_signature(method)
    if ptr_is_null(signature) != 0:
        return captures
    wrapped = py_tuple_new(2)
    if ptr_is_null(wrapped) != 0:
        py_decref(signature)
        return captures
    py_tuple_set_item(wrapped, 0, captures)
    py_tuple_set_item(wrapped, 1, signature)
    py_decref(signature)
    return wrapped


def _call_pyfunc_bound_args(func, bound_args):
    if not _ptr_can_have_header(func):
        return _class_require_result(
            null(),
            cstr("class callback"),
            cstr("class callback received an invalid function object"),
        )
    if load_i32(func, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_FUNC:
        return _class_require_result(
            null(),
            cstr("class callback"),
            cstr("class callback received an invalid function object"),
        )
    entry = load_ptr(func, 56)
    if ptr_is_null(entry) != 0:
        return _class_require_result(
            null(),
            cstr("class callback"),
            cstr("class callback function has no entry point"),
        )
    captures = pcc_gc_load_ptr(func, ptr_add(func, 64))
    actual_captures = captures
    owns_actual: int = 0
    if _ptr_can_have_header(captures):
        if load_i32(captures, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_TUPLE and py_tuple_len(captures) == 2:
            candidate = py_tuple_get(captures, 1)
            if _func_signature_valid(candidate):
                inner = py_tuple_get(captures, 0)
                if ptr_is_null(inner) == 0:
                    actual_captures = inner
                    owns_actual = 1
            if ptr_is_null(candidate) == 0:
                py_decref(candidate)
    out = call_ptr2(entry, actual_captures, bound_args)
    _class_require_result(
        out,
        cstr("class callback"),
        cstr("class callback returned NULL without setting an exception"),
    )
    if owns_actual != 0:
        py_decref(actual_captures)
    return out


def _instance_bound_method_entry(captures, args):
    func = py_tuple_get(captures, 0)
    self_obj = py_tuple_get(captures, 1)
    if ptr_is_null(func) != 0 or ptr_is_null(self_obj) != 0:
        if ptr_is_null(func) == 0:
            py_decref(func)
        if ptr_is_null(self_obj) == 0:
            py_decref(self_obj)
        return null()
    n_args: int = py_tuple_len(args)
    out = null()
    if _ptr_can_have_header(func) and load_i32(func, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_FUNC:
        full_args = py_tuple_new(n_args + 1)
        if ptr_is_null(full_args) == 0:
            py_tuple_set_item(full_args, 0, self_obj)
            i: int = 0
            valid: int = 1
            while i < n_args:
                arg = py_tuple_get(args, i)
                if ptr_is_null(arg) != 0:
                    valid = 0
                    i = n_args
                else:
                    py_tuple_set_item(full_args, i + 1, arg)
                    py_decref(arg)
                i = i + 1
            if valid != 0:
                out = _call_pyfunc_bound_args(func, full_args)
                _class_require_result(
                    out,
                    cstr("class callback"),
                    cstr("class callback returned NULL without setting an exception"),
                )
            py_decref(full_args)
        else:
            _class_require_result(
                null(),
                cstr("py_tuple_new"),
                cstr("class callback argument tuple allocation failed"),
            )
    elif n_args == 0:
        out = call_ptr1(func, self_obj)
        _class_require_result(
            out,
            cstr("class callback"),
            cstr("class callback returned NULL without setting an exception"),
        )
    elif n_args == 1:
        arg0 = py_tuple_get(args, 0)
        if ptr_is_null(arg0) == 0:
            out = call_ptr2(func, self_obj, arg0)
            _class_require_result(
                out,
                cstr("class callback"),
                cstr("class callback returned NULL without setting an exception"),
            )
            py_decref(arg0)
    elif n_args == 2:
        arg0 = py_tuple_get(args, 0)
        arg1 = py_tuple_get(args, 1)
        if ptr_is_null(arg0) == 0 and ptr_is_null(arg1) == 0:
            out = call_ptr3(func, self_obj, arg0, arg1)
            _class_require_result(
                out,
                cstr("class callback"),
                cstr("class callback returned NULL without setting an exception"),
            )
        if ptr_is_null(arg0) == 0:
            py_decref(arg0)
        if ptr_is_null(arg1) == 0:
            py_decref(arg1)
    elif n_args == 3:
        arg0 = py_tuple_get(args, 0)
        arg1 = py_tuple_get(args, 1)
        arg2 = py_tuple_get(args, 2)
        if (
            ptr_is_null(arg0) == 0
            and ptr_is_null(arg1) == 0
            and ptr_is_null(arg2) == 0
        ):
            out = call_ptr4(func, self_obj, arg0, arg1, arg2)
            _class_require_result(
                out,
                cstr("class callback"),
                cstr("class callback returned NULL without setting an exception"),
            )
        if ptr_is_null(arg0) == 0:
            py_decref(arg0)
        if ptr_is_null(arg1) == 0:
            py_decref(arg1)
        if ptr_is_null(arg2) == 0:
            py_decref(arg2)
    if ptr_is_null(out) != 0:
        _class_require_result(
            out,
            cstr("class callback"),
            cstr("class callback returned NULL without setting an exception"),
        )
    py_decref(func)
    py_decref(self_obj)
    return out


@c_abi_export("py_instance_bind_method")
def py_instance_bind_method(method, self_obj, name):
    if ptr_is_null(method) != 0 or ptr_is_null(self_obj) != 0:
        return null()
    captures = py_tuple_new(2)
    if ptr_is_null(captures) != 0:
        return null()
    py_tuple_set_item(captures, 0, method)
    py_tuple_set_item(captures, 1, self_obj)
    bound_name = name
    if _ptr_can_have_header(method) and load_i32(method, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_FUNC:
        method_name = load_ptr(method, 72)
        if ptr_is_null(method_name) == 0:
            bound_name = method_name
    bound_captures = _wrap_bound_captures(method, captures)
    bound = py_func_new_bound(
        _instance_bound_method_entry,
        bound_captures,
        bound_name,
        self_obj,
    )
    if ptr_eq(bound_captures, captures) == 0:
        py_decref(bound_captures)
    py_decref(captures)
    return bound


def _field_cache_slot(cls, name) -> int:
    return ((ptr_to_int(cls) >> 4) ^ (ptr_to_int(name) >> 4)) & 3


def _field_cache_lookup(cls, name) -> int:
    epoch: int = _class_attr_cache_epoch()
    if (
        load_i32(global_addr("py_inst_field_cache_epoch0"), 0) == epoch
        and ptr_eq(global_load_ptr("py_inst_field_cache_cls0"), cls) != 0
        and ptr_eq(global_load_ptr("py_inst_field_cache_name0"), name) != 0
    ):
        return load_i32(global_addr("py_inst_field_cache_idx0"), 0)
    if (
        load_i32(global_addr("py_inst_field_cache_epoch1"), 0) == epoch
        and ptr_eq(global_load_ptr("py_inst_field_cache_cls1"), cls) != 0
        and ptr_eq(global_load_ptr("py_inst_field_cache_name1"), name) != 0
    ):
        return load_i32(global_addr("py_inst_field_cache_idx1"), 0)
    if (
        load_i32(global_addr("py_inst_field_cache_epoch2"), 0) == epoch
        and ptr_eq(global_load_ptr("py_inst_field_cache_cls2"), cls) != 0
        and ptr_eq(global_load_ptr("py_inst_field_cache_name2"), name) != 0
    ):
        return load_i32(global_addr("py_inst_field_cache_idx2"), 0)
    if (
        load_i32(global_addr("py_inst_field_cache_epoch3"), 0) == epoch
        and ptr_eq(global_load_ptr("py_inst_field_cache_cls3"), cls) != 0
        and ptr_eq(global_load_ptr("py_inst_field_cache_name3"), name) != 0
    ):
        return load_i32(global_addr("py_inst_field_cache_idx3"), 0)
    return -1


def _field_cache_store(cls, name, idx: int) -> None:
    slot: int = _field_cache_slot(cls, name)
    epoch: int = _class_attr_cache_epoch()
    if slot == 0:
        global_store_ptr("py_inst_field_cache_cls0", cls)
        global_store_ptr("py_inst_field_cache_name0", name)
        store_i32(global_addr("py_inst_field_cache_idx0"), 0, idx)
        store_i32(global_addr("py_inst_field_cache_epoch0"), 0, epoch)
        return
    if slot == 1:
        global_store_ptr("py_inst_field_cache_cls1", cls)
        global_store_ptr("py_inst_field_cache_name1", name)
        store_i32(global_addr("py_inst_field_cache_idx1"), 0, idx)
        store_i32(global_addr("py_inst_field_cache_epoch1"), 0, epoch)
        return
    if slot == 2:
        global_store_ptr("py_inst_field_cache_cls2", cls)
        global_store_ptr("py_inst_field_cache_name2", name)
        store_i32(global_addr("py_inst_field_cache_idx2"), 0, idx)
        store_i32(global_addr("py_inst_field_cache_epoch2"), 0, epoch)
        return
    global_store_ptr("py_inst_field_cache_cls3", cls)
    global_store_ptr("py_inst_field_cache_name3", name)
    store_i32(global_addr("py_inst_field_cache_idx3"), 0, idx)
    store_i32(global_addr("py_inst_field_cache_epoch3"), 0, epoch)


def _dynamic_attr_slot(inst):
    if not _ptr_is_instance(inst):
        return null()
    cls = pcc_gc_load_ptr(inst, ptr_add(inst, PYINSTANCEOBJECT_CLS_OFFSET))
    flags: int = load_i32(cls, PYOBJECTHEADER_FLAGS_OFFSET)
    if (flags & 2) != 0:
        return null()
    n_fields: int = load_i32(cls, PYCLASSOBJECT_N_FIELDS_OFFSET)
    if n_fields < 0:
        n_fields = 0
    return ptr_add(
        inst, PYINSTANCEOBJECT_FIELDS_OFFSET + n_fields * C_POINTER_SIZE
    )


@c_abi_export("py_instance_vars")
def py_instance_vars(inst):
    if not _ptr_is_instance(inst):
        py_raise_owned(py_exc_new(3, cstr("vars() argument has no __dict__")))
        return null()
    cls = pcc_gc_load_ptr(inst, ptr_add(inst, PYINSTANCEOBJECT_CLS_OFFSET))
    if not _ptr_is_class(cls):
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
        return py_instance_vars(o)
    if _ptr_is_class(o):
        attrs = py_class_attrs_dict(o, 1)
        if ptr_is_null(attrs) == 0:
            py_incref(attrs)
            return attrs
    py_raise_owned(py_exc_new(3, cstr("vars() argument has no __dict__")))
    return null()


def _class_attr_lookup_in_mro(cls, name):
    if not _ptr_is_class(cls):
        return null()
    if ptr_is_null(name) != 0:
        return null()
    cls = pcc_gc_note_relocation_read(cls)
    key = py_str_new(name, strlen(name))
    if ptr_is_null(key) != 0:
        return null()
    n_mro: int = load_i32(cls, PYCLASSOBJECT_N_MRO_OFFSET)
    mro = load_ptr(cls, PYCLASSOBJECT_MRO_OFFSET)
    i: int = 0
    while i < n_mro:
        m = pcc_gc_load_ptr(cls, ptr_add(mro, i * 8))
        if ptr_is_null(m) == 0:
            attrs = py_class_attrs_dict(m, 0)
            if ptr_is_null(attrs) == 0:
                value = py_dict_get(attrs, key)
                if ptr_is_null(value) == 0:
                    py_decref(key)
                    return value
        i = i + 1
    py_decref(key)
    return null()


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
    value = _class_attr_lookup_in_mro(cls, name)
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
    return py_class_setattr_raw(cls, name, value)


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
    store_ptr(cls, PYCLASSOBJECT_METHODS_OFFSET, new_methods)
    store_i32(cls, PYCLASSOBJECT_N_METHODS_OFFSET, new_n)
    payload_offset: int = -1
    if ptr_is_null(methods) == 0:
        if ptr_eq(methods, new_methods) == 0:
            payload_offset = pcc_gc_backend4_zpage_retarget_owner_payload_span(
                cls,
                methods,
                new_methods,
                new_n * 16,
            )
    if payload_offset < 0:
        if ptr_is_null(methods) == 0:
            if ptr_eq(methods, new_methods) == 0:
                pcc_gc_backend4_zpage_unregister_owner_payload_span(cls, methods)
        pcc_gc_backend4_zpage_register_owner_payload_span(cls, new_methods, new_n * 16)
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
    store_ptr(cls, PYCLASSOBJECT_METACLASS_OFFSET, metaclass)
    _class_note_borrowed_metadata_slot_store(cls, ptr_add(cls, PYCLASSOBJECT_METACLASS_OFFSET), metaclass)


@c_abi_export("py_instance_new")
def py_instance_new(cls) -> c_ptr:
    if not _ptr_is_class(cls):
        return null()
    cls = pcc_gc_note_relocation_read(cls)
    n_fields_i32: int = load_i32(cls, PYCLASSOBJECT_N_FIELDS_OFFSET)
    n_slots: int = n_fields_i32 + 1
    if n_slots < 0:
        n_slots = 1
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
    if not _ptr_is_instance(inst):
        return null()
    if idx < 0:
        return null()
    cls = pcc_gc_load_ptr(inst, ptr_add(inst, PYINSTANCEOBJECT_CLS_OFFSET))
    n_fields: int = load_i32(cls, PYCLASSOBJECT_N_FIELDS_OFFSET)
    if idx >= n_fields:
        return null()
    fields_base = ptr_add(inst, PYINSTANCEOBJECT_FIELDS_OFFSET)
    v = pcc_gc_load_ptr(inst, ptr_add(fields_base, idx * C_POINTER_SIZE))
    if ptr_is_null(v) == 0:
        py_incref(v)
    return v


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
            pcc_gc_store_ptr(inst, dyn_slot, dyn)
            py_decref(dyn)
        py_incref(dyn)
        return dyn
    cached_idx: int = _field_cache_lookup(cls, name)
    if cached_idx >= 0:
        fields_base_cached = ptr_add(inst, PYINSTANCEOBJECT_FIELDS_OFFSET)
        cached = pcc_gc_load_ptr(
            inst,
            ptr_add(fields_base_cached, cached_idx * C_POINTER_SIZE),
        )
        if ptr_is_null(cached) == 0:
            py_incref(cached)
        return cached
    class_attr = _class_attr_lookup_in_mro(cls, name)
    if ptr_is_null(class_attr) == 0:
        if _descriptor_is_data(class_attr):
            got = _descriptor_call_get(class_attr, inst, cls)
            py_decref(class_attr)
            if ptr_is_null(got) == 0:
                return got
            if py_err_occurred() != 0:
                return null()
    idx: int = _lookup_field_index(cls, name)
    if idx >= 0:
        _field_cache_store(cls, name, idx)
        fields_base = ptr_add(inst, PYINSTANCEOBJECT_FIELDS_OFFSET)
        v = pcc_gc_load_ptr(inst, ptr_add(fields_base, idx * C_POINTER_SIZE))
        if ptr_is_null(v) == 0:
            py_incref(v)
        return v
    dyn_slot = _dynamic_attr_slot(inst)
    if ptr_is_null(dyn_slot) == 0:
        dyn = pcc_gc_load_ptr(inst, dyn_slot)
        if ptr_is_null(dyn) == 0:
            key = py_str_new(name, strlen(name))
            got = py_dict_get(dyn, key)
            py_decref(key)
            if ptr_is_null(got) == 0:
                return got
    if ptr_is_null(class_attr) == 0:
        if is_tagged_int(class_attr) == 0:
            if load_i32(class_attr, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_FUNC:
                bound = py_instance_bind_method(class_attr, inst, name)
                py_decref(class_attr)
                return bound
        got = _descriptor_call_get(class_attr, inst, cls)
        if ptr_is_null(got) == 0:
            py_decref(class_attr)
            return got
        if py_err_occurred() != 0:
            py_decref(class_attr)
            return null()
        return class_attr
    if ptr_is_null(cls) != 0:
        return null()
    method = _class_lookup_in_mro(cls, name)
    if ptr_is_null(method) == 0:
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
    getattr_method = _class_lookup_in_mro(cls, cstr("__getattr__"))
    if ptr_is_null(getattr_method) != 0:
        return null()
    key = py_str_new(name, strlen(name))
    if ptr_is_null(key) != 0:
        return null()
    # The method-table slot for a compiled __getattr__ holds a PY_TYPE_FUNC
    # object, not a raw code pointer; invoke it via py_obj_call in that case.
    # call_ptr2 alone treats the object as a code address and crashes. Mirrors
    # class_call_binary_method in py_class.c.
    if is_tagged_int(getattr_method) == 0:
        if load_i32(getattr_method, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_FUNC:
            gargs = py_tuple_new(2)
            if ptr_is_null(gargs) != 0:
                _class_require_result(
                    null(),
                    cstr("py_tuple_new"),
                    cstr("class callback argument tuple allocation failed"),
                )
                py_decref(key)
                return null()
            py_tuple_set_item(gargs, 0, inst)
            py_tuple_set_item(gargs, 1, key)
            got = py_obj_call(getattr_method, gargs, null())
            _class_require_result(
                got,
                cstr("__getattr__"),
                cstr("class callback returned NULL without setting an exception"),
            )
            py_decref(gargs)
            py_decref(key)
            return got
    got = call_ptr2(getattr_method, inst, key)
    _class_require_result(
        got,
        cstr("__getattr__"),
        cstr("class callback returned NULL without setting an exception"),
    )
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
    getattribute_method = _class_lookup_in_mro(cls, cstr("__getattribute__"))
    if ptr_is_null(getattribute_method) == 0:
        key = py_str_new(name, strlen(name))
        if ptr_is_null(key) != 0:
            return null()
        got = call_ptr2(getattribute_method, inst, key)
        _class_require_result(
            got,
            cstr("__getattribute__"),
            cstr("class callback returned NULL without setting an exception"),
        )
        if ptr_is_null(got) == 0:
            py_decref(key)
            return got
        if py_err_occurred() != 0:
            cur = py_current_exception()
            attr_cls = py_exc_builtin_class(6)  # PY_EXC_ATTRIBUTEERROR
            if ptr_is_null(attr_cls) == 0:
                if py_exc_matches(cur, attr_cls) != 0:
                    getattr_method = _class_lookup_in_mro(cls, cstr("__getattr__"))
                    if ptr_is_null(getattr_method) == 0:
                        py_clear_exception()
                        fallback = call_ptr2(getattr_method, inst, key)
                        _class_require_result(
                            fallback,
                            cstr("__getattr__"),
                            cstr("class callback returned NULL without setting an exception"),
                        )
                        py_decref(key)
                        return fallback
        py_decref(key)
        return null()
    return py_instance_getattr_default(inst, name)


@c_abi_export("py_instance_setattr")
def py_instance_setattr(inst, name, value) -> int:
    if not _ptr_is_instance(inst):
        return -1
    if ptr_is_null(name) != 0:
        return -1
    cls = pcc_gc_load_ptr(inst, ptr_add(inst, PYINSTANCEOBJECT_CLS_OFFSET))
    class_attr = _class_attr_lookup_in_mro(cls, name)
    if ptr_is_null(class_attr) == 0:
        set_method = _descriptor_method(class_attr, cstr("__set__"))
        if ptr_is_null(set_method) == 0:
            rc: int = _descriptor_call_set(class_attr, inst, value)
            py_decref(class_attr)
            return rc
        py_decref(class_attr)
    idx: int = _lookup_field_index(cls, name)
    if idx >= 0:
        n_fields: int = load_i32(cls, PYCLASSOBJECT_N_FIELDS_OFFSET)
        if idx >= n_fields:
            return -1
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
        delete_method = _descriptor_method(class_attr, cstr("__delete__"))
        if ptr_is_null(delete_method) == 0:
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


@c_abi_export("py_class_apply_namespace_dict")
def py_class_apply_namespace_dict(cls, ns) -> int:
    if not _ptr_is_class(cls):
        return -1
    if ptr_is_null(ns) != 0:
        py_raise_owned(py_exc_new(3, cstr("type.__new__() argument 3 must be dict")))
        return -1
    if load_i32(ns, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_DICT:
        py_raise_owned(py_exc_new(3, cstr("type.__new__() argument 3 must be dict")))
        return -1
    keys = py_dict_keys(ns)
    if ptr_is_null(keys) != 0:
        return -1
    n: int = py_list_len(keys)
    i: int = 0
    while i < n:
        key = py_list_get(keys, i)
        if ptr_is_null(key) != 0:
            py_decref(keys)
            return -1
        name = py_str_utf8(key)
        if ptr_is_null(name) != 0:
            py_decref(key)
            py_decref(keys)
            return -1
        value = py_dict_get(ns, key)
        if ptr_is_null(value) != 0:
            py_decref(key)
            py_decref(keys)
            return -1
        rc: int = py_class_setattr_raw(cls, name, value)
        py_decref(value)
        py_decref(key)
        if rc != 0:
            py_decref(keys)
            return rc
        i = i + 1
    py_decref(keys)
    return 0


@c_abi_export("py_isinstance")
def py_isinstance(obj, cls) -> int:
    obj = pcc_gc_note_relocation_read(obj)
    cls = pcc_gc_note_relocation_read(cls)
    if _ptr_can_have_header(obj):
        if load_i32(obj, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_EXC:
            # Exception instances match through their exception-class MRO.
            if py_exc_matches(obj, cls) != 0:
                return 1
            return 0
    if not _ptr_is_instance(obj):
        return 0
    if not _ptr_is_class(cls):
        return 0
    obj_cls = pcc_gc_load_ptr(obj, ptr_add(obj, PYINSTANCEOBJECT_CLS_OFFSET))
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


@c_abi_export("py_super_lookup")
def py_super_lookup(start_cls, from_cls, name):
    if not _ptr_is_class(start_cls):
        return null()
    if not _ptr_is_class(from_cls):
        return null()
    if ptr_is_null(name) != 0:
        return null()
    start_cls = pcc_gc_note_relocation_read(start_cls)
    from_cls = pcc_gc_note_relocation_read(from_cls)
    n_mro: int = load_i32(start_cls, PYCLASSOBJECT_N_MRO_OFFSET)
    mro = load_ptr(start_cls, PYCLASSOBJECT_MRO_OFFSET)
    start: int = -1
    i: int = 0
    while i < n_mro:
        m = pcc_gc_load_ptr(start_cls, ptr_add(mro, i * C_POINTER_SIZE))
        if ptr_eq(m, from_cls) != 0:
            start = i
            i = n_mro  # force-exit
        i = i + 1
    if start < 0:
        exc = py_exc_new(
            3,
            cstr("super(type, obj): obj must be an instance or subtype of type"),
        )
        py_raise_owned(exc)
        return null()

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
    exc = py_exc_new(6, cstr("super object has no attribute"))
    py_raise_owned(exc)
    return null()


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
    py_weakref_invalidate(o)
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
        dyn_slot = _dynamic_attr_slot(o)
        if ptr_is_null(dyn_slot) == 0:
            dyn = pcc_gc_load_ptr(o, dyn_slot)
            if ptr_is_null(dyn) == 0:
                store_ptr(dyn_slot, 0, null())
                py_decref(dyn)
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
        pcc_gc_free_object_memory(o)
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
    src_dyn_slot = _dynamic_attr_slot(obj)
    dst_dyn_slot = _dynamic_attr_slot(dst)
    if ptr_is_null(src_dyn_slot) == 0:
        dyn = pcc_gc_load_ptr(obj, src_dyn_slot)
        if ptr_is_null(dyn) == 0:
            py_incref(dyn)
            store_ptr(dst_dyn_slot, 0, dyn)

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


# c3_linearize + py_class_new are the heaviest part. We allocate the
# MergeSeq array on the heap (16 bytes per seq: items@0 + head@8 +
# len@12) and write everything inline in py_class_new to avoid the
# i32→i64 user-helper call problem.


@c_abi_export("py_class_new")
def py_class_new(name, bases, n_bases: int, field_names, n_fields: int):
    c = pcc_gc_alloc(PYCLASSOBJECT_SIZE, PY_TYPE_CLASS, 0)
    if ptr_is_null(c) != 0:
        return null()
    memset(
        ptr_add(c, PYCLASSOBJECT_NAME_OFFSET),
        0,
        PYCLASSOBJECT_SIZE - PYCLASSOBJECT_NAME_OFFSET,
    )
    store_i64(c, PYOBJECTHEADER_REFCOUNT_OFFSET, 1)
    store_i32(c, PYOBJECTHEADER_TYPE_TAG_OFFSET, PY_TYPE_CLASS)
    # Class/type objects are effectively process-lifetime and are referenced
    # by every instance via a *borrowed* (uncounted) class pointer at inst+16.
    # Mark them PY_FLAG_IMMORTAL so a stray over-release cannot drive a live
    # class to refcount 0 and free it out from under its instances -- which
    # zeroes the class field table (n_fields/field_names) and makes every
    # subsequent getattr on any instance fail / segfault. Mirrors py_class.c.
    store_i32(
        c,
        PYOBJECTHEADER_FLAGS_OFFSET,
        load_i32(c, PYOBJECTHEADER_FLAGS_OFFSET) | PY_FLAG_IMMORTAL,
    )
    store_ptr(c, PYCLASSOBJECT_NAME_OFFSET, name)
    store_i32(c, PYCLASSOBJECT_N_BASES_OFFSET, n_bases)
    store_i32(c, PYCLASSOBJECT_N_FIELDS_OFFSET, n_fields)
    store_ptr(c, PYCLASSOBJECT_ATTRS_OFFSET, null())
    store_ptr(c, PYCLASSOBJECT_METACLASS_OFFSET, null())

    # Copy bases array.
    if n_bases > 0:
        if ptr_is_null(bases) == 0:
            bases_copy = malloc(n_bases * C_POINTER_SIZE)
            if ptr_is_null(bases_copy) == 0:
                ii: int = 0
                while ii < n_bases:
                    bv = pcc_gc_note_relocation_read(
                        load_ptr(bases, ii * C_POINTER_SIZE)
                    )
                    store_ptr(bases_copy, ii * C_POINTER_SIZE, bv)
                    ii = ii + 1
                store_ptr(c, PYCLASSOBJECT_BASES_OFFSET, bases_copy)
                pcc_gc_backend4_zpage_register_owner_payload_span(
                    c,
                    bases_copy,
                    n_bases * C_POINTER_SIZE,
                )
        if ptr_is_null(load_ptr(c, PYCLASSOBJECT_BASES_OFFSET)) != 0:
            free(c)
            return null()

    # Copy field_names array.
    if n_fields > 0:
        if ptr_is_null(field_names) == 0:
            fn_copy = malloc(n_fields * C_POINTER_SIZE)
            if ptr_is_null(fn_copy) == 0:
                jj: int = 0
                while jj < n_fields:
                    fv = load_ptr(field_names, jj * C_POINTER_SIZE)
                    store_ptr(fn_copy, jj * C_POINTER_SIZE, fv)
                    jj = jj + 1
                store_ptr(c, PYCLASSOBJECT_FIELD_NAMES_OFFSET, fn_copy)

    user_tag: int = _alloc_user_tag()
    store_i32(c, PYCLASSOBJECT_TYPE_TAG_ALLOC_OFFSET, user_tag)

    n_slots: int = n_fields + 1
    if n_slots < 0:
        n_slots = 1
    inst_size: int = PYINSTANCEOBJECT_SIZE + n_slots * C_POINTER_SIZE
    if inst_size > 0x7FFFFFFF:
        inst_size = 0x7FFFFFFF
    store_i32(c, PYCLASSOBJECT_INSTANCE_SIZE_OFFSET, inst_size)

    # ---- C3 linearize inline ----
    # Allocate MergeSeq array (n_bases + 1 entries, 16 bytes each).
    tail = null()
    tail_len: int = 0
    if n_bases > 0:
        linear_bases = load_ptr(c, PYCLASSOBJECT_BASES_OFFSET)
        if ptr_is_null(linear_bases) != 0:
            linear_bases = bases
        nseqs: int = n_bases + 1
        seqs = malloc(nseqs * 16)
        if ptr_is_null(seqs) != 0:
            # alloc failure — bail out, leak partial state
            free(c)
            return null()
        memset(seqs, 0, nseqs * 16)

        cap_total: int = 0
        kk: int = 0
        while kk < n_bases:
            b = pcc_gc_note_relocation_read(
                load_ptr(linear_bases, kk * C_POINTER_SIZE)
            )
            b_mro = load_ptr(b, PYCLASSOBJECT_MRO_OFFSET)
            b_n_mro: int = load_i32(b, PYCLASSOBJECT_N_MRO_OFFSET)
            seq_off: int = kk * 16
            store_ptr(seqs, seq_off, b_mro)
            store_i32(seqs, seq_off + 8, 0)
            store_i32(seqs, seq_off + 12, b_n_mro)
            cap_total = cap_total + b_n_mro
            kk = kk + 1
        last_off: int = n_bases * 16
        store_ptr(seqs, last_off, linear_bases)
        store_i32(seqs, last_off + 8, 0)
        store_i32(seqs, last_off + 12, n_bases)
        cap_total = cap_total + n_bases

        if cap_total <= 0:
            cap_total = 1
        acc = malloc(cap_total * C_POINTER_SIZE)
        if ptr_is_null(acc) != 0:
            free(seqs)
            free(c)
            return null()
        acc_len: int = 0

        # Outer merge loop.
        merge_done: int = 0
        while merge_done == 0:
            any_remaining: int = 0
            i_check: int = 0
            while i_check < nseqs:
                so2: int = i_check * 16
                hd: int = load_i32(seqs, so2 + 8)
                ln: int = load_i32(seqs, so2 + 12)
                if hd < ln:
                    any_remaining = 1
                    i_check = nseqs
                i_check = i_check + 1
            if any_remaining == 0:
                merge_done = 1
            else:
                # Pick candidate.
                cand = null()
                pick_i: int = 0
                while pick_i < nseqs:
                    so3: int = pick_i * 16
                    hd2: int = load_i32(seqs, so3 + 8)
                    ln2: int = load_i32(seqs, so3 + 12)
                    if hd2 < ln2:
                        items = load_ptr(seqs, so3)
                        c_cand = pcc_gc_note_relocation_read(
                            load_ptr(items, hd2 * C_POINTER_SIZE)
                        )
                        ok: int = 1
                        check_j: int = 0
                        while check_j < nseqs:
                            if check_j != pick_i:
                                so4: int = check_j * 16
                                hd3: int = load_i32(seqs, so4 + 8)
                                ln3: int = load_i32(seqs, so4 + 12)
                                items3 = load_ptr(seqs, so4)
                                tail_i: int = hd3 + 1
                                while tail_i < ln3:
                                    tv = pcc_gc_note_relocation_read(
                                        load_ptr(items3, tail_i * C_POINTER_SIZE)
                                    )
                                    if ptr_eq(tv, c_cand) != 0:
                                        ok = 0
                                        tail_i = ln3
                                    tail_i = tail_i + 1
                                if ok == 0:
                                    check_j = nseqs
                            check_j = check_j + 1
                        if ok != 0:
                            cand = c_cand
                            pick_i = nseqs
                    pick_i = pick_i + 1

                if ptr_is_null(cand) != 0:
                    # Inconsistent MRO — bail.
                    free(acc)
                    free(seqs)
                    free(c)
                    return null()
                store_ptr(acc, acc_len * C_POINTER_SIZE, cand)
                acc_len = acc_len + 1
                # Consume head where matches cand.
                consume_i: int = 0
                while consume_i < nseqs:
                    so5: int = consume_i * 16
                    hd4: int = load_i32(seqs, so5 + 8)
                    ln4: int = load_i32(seqs, so5 + 12)
                    if hd4 < ln4:
                        items5 = load_ptr(seqs, so5)
                        cur = pcc_gc_note_relocation_read(
                            load_ptr(items5, hd4 * C_POINTER_SIZE)
                        )
                        if ptr_eq(cur, cand) != 0:
                            store_i32(seqs, so5 + 8, hd4 + 1)
                    consume_i = consume_i + 1
        free(seqs)
        tail = acc
        tail_len = acc_len

    # MRO assembly.
    root = _object_root()
    append_root: int = 0
    if n_bases == 0:
        if ptr_eq(c, root) == 0:
            append_root = 1

    mro_len: int = 1 + tail_len + append_root
    mro = malloc(mro_len * C_POINTER_SIZE)
    if ptr_is_null(mro) != 0:
        if ptr_is_null(tail) == 0:
            free(tail)
        free(c)
        return null()
    store_ptr(mro, 0, c)
    mi: int = 0
    while mi < tail_len:
        v = load_ptr(tail, mi * C_POINTER_SIZE)
        store_ptr(mro, (1 + mi) * C_POINTER_SIZE, v)
        mi = mi + 1
    if append_root != 0:
        store_ptr(mro, (mro_len - 1) * C_POINTER_SIZE, root)
    store_ptr(c, PYCLASSOBJECT_MRO_OFFSET, mro)
    store_i32(c, PYCLASSOBJECT_N_MRO_OFFSET, mro_len)
    pcc_gc_backend4_zpage_register_owner_payload_span(
        c,
        mro,
        mro_len * C_POINTER_SIZE,
    )

    if ptr_is_null(tail) == 0:
        free(tail)
    pcc_gc_publish_initialized(c)
    return c


@c_abi_export("py_class_new_from_objects")
def py_class_new_from_objects(name_obj, bases_obj, namespace):
    if not _ptr_can_have_header(name_obj) or load_i32(name_obj, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_STR:
        py_raise_owned(py_exc_new(3, cstr("type.__new__() argument 1 must be str")))
        return null()
    name = py_str_utf8(name_obj)
    if ptr_is_null(name) != 0:
        return null()
    n_bases: int = 0
    bases_kind: int = 0
    none_obj = global_load_ptr("py_None")
    if ptr_is_null(bases_obj) != 0 or ptr_eq(bases_obj, none_obj) != 0:
        n_bases = 0
    elif _ptr_can_have_header(bases_obj) and load_i32(bases_obj, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_TUPLE:
        n_bases = py_tuple_len(bases_obj)
        bases_kind = 1
    elif _ptr_can_have_header(bases_obj) and load_i32(bases_obj, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_LIST:
        n_bases = py_list_len(bases_obj)
        bases_kind = 2
    else:
        py_raise_owned(py_exc_new(3, cstr("type.__new__() argument 2 must be tuple")))
        return null()
    if n_bases < 0 or n_bases > 2147483647:
        py_raise_owned(py_exc_new(3, cstr("too many base classes")))
        return null()
    base_array = null()
    if n_bases > 0:
        base_array = malloc(n_bases * 8)
        if ptr_is_null(base_array) != 0:
            return null()
        i: int = 0
        valid: int = 1
        while i < n_bases:
            item = null()
            if bases_kind == 1:
                item = py_tuple_get(bases_obj, i)
            else:
                item = py_list_get(bases_obj, i)
            if not _ptr_is_class(item):
                if ptr_is_null(item) == 0:
                    py_decref(item)
                valid = 0
                i = n_bases
            else:
                store_ptr(base_array, i * 8, item)
            i = i + 1
        if valid == 0:
            free(base_array)
            py_raise_owned(py_exc_new(3, cstr("type.__new__() base must be class")))
            return null()
    cls = py_class_new_abi(name, base_array, n_bases, null(), 0)
    if ptr_is_null(base_array) == 0:
        free(base_array)
    if ptr_is_null(cls) != 0:
        return null()
    if ptr_is_null(namespace) == 0 and ptr_eq(namespace, none_obj) == 0:
        if py_class_apply_namespace_dict(cls, namespace) != 0:
            py_decref(cls)
            return null()
    return cls


@c_abi_export("py_class_mark_slots_only")
def py_class_mark_slots_only(cls) -> None:
    if ptr_is_null(cls) != 0:
        return
    flags: int = load_i32(cls, PYOBJECTHEADER_FLAGS_OFFSET)
    store_i32(cls, PYOBJECTHEADER_FLAGS_OFFSET, flags | 2)


@c_abi_export("py_class_mark_dict_subclass")
def py_class_mark_dict_subclass(cls) -> None:
    # Set PY_CLASS_FLAG_DICT_SUBCLASS (bit 2, value 4): this class subclasses
    # the builtin ``dict``, so dict-inherited item storage / methods are routed
    # to a backing dict in the instance's __dict__ slot (see py_protocol.c).
    if ptr_is_null(cls) != 0:
        return
    flags: int = load_i32(cls, PYOBJECTHEADER_FLAGS_OFFSET)
    store_i32(cls, PYOBJECTHEADER_FLAGS_OFFSET, flags | 4)
