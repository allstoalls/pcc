"""pcc-Python owners for the no-libpython C-API capsule surface.

Replaces the PyCapsule_* block of py_capi_shim.c.  A capsule is a pcc
instance of a lazily-created ``capsule`` class carrying four attributes:
__pcc_capsule_pointer__, __pcc_capsule_name__, __pcc_capsule_context__,
__pcc_capsule_destructor__.  The destructor is a raw function address stored
as a void-pointer int and invoked through a fixed-signature indirect call.

Owned surface (stable C ABI names):

  PyCapsule_New, PyCapsule_CheckExact, PyCapsule_IsValid, PyCapsule_GetName,
  PyCapsule_GetContext, PyCapsule_GetDestructor, PyCapsule_GetPointer,
  PyCapsule_SetPointer, PyCapsule_SetContext, PyCapsule_SetDestructor,
  PyCapsule_SetName, PyCapsule_Import

Public object type tags come from the generated ``py_abi_constants`` module.
Private exception codes remain owned by the capsule contract:
  PY_EXC_TYPEERROR = 3, PY_EXC_VALUEERROR = 2, PY_EXC_RUNTIMEERROR = 5
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import (
    C_POINTER_SIZE,
    PYINSTANCEOBJECT_CLS_OFFSET,
    PY_TYPE_INSTANCE,
    PY_TYPE_STR,
    PY_TYPE_USER_CLASS_START,
)

from pcc.extern import c_abi_typed_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    call_void_ptr1,
    cstr,
    define_global_cstr,
    define_global_i32,
    define_global_ptr_null,
    function_addr,
    global_addr,
    global_load_ptr,
    global_store_ptr,
    is_tagged_int,
    load_i32,
    load_i8,
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
    store_i64,
    store_ptr,
    strlen,
)

py_class_new = extern("py_class_new", (c_ptr, c_ptr, c_int64, c_ptr, c_int64), c_ptr)
py_class_add_method = extern("py_class_add_method", (c_ptr, c_ptr, c_ptr), c_void)
py_instance_new = extern("py_instance_new", (c_ptr,), c_ptr)
py_instance_setattr = extern("py_instance_setattr", (c_ptr, c_ptr, c_ptr), c_int64)
py_instance_getattr = extern("py_instance_getattr", (c_ptr, c_ptr), c_ptr)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
py_incref = extern("py_incref", (c_ptr,), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_str_new = extern("py_str_new", (c_ptr, c_int64), c_ptr)
py_str_utf8 = extern("py_str_utf8", (c_ptr,), c_ptr)
py_obj_getattr = extern("py_obj_getattr", (c_ptr, c_ptr), c_ptr)
memcmp_c = extern("memcmp", (c_ptr, c_ptr, c_int64), c_int64)
memcpy_c = extern("memcpy", (c_ptr, c_ptr, c_int64), c_ptr)
free_c = extern("free", (c_ptr,), c_void)
py_native_extension_import_by_name = extern(
    "py_native_extension_import_by_name", (c_ptr,), c_ptr
)
py_err_occurred = extern("py_err_occurred", (), c_int64)
pcc_py_long_from_void_ptr = extern("pcc_py_long_from_void_ptr", (c_ptr,), c_ptr)
pcc_py_long_as_void_ptr = extern("pcc_py_long_as_void_ptr", (c_ptr,), c_ptr)

define_global_ptr_null("pcc_capi_capsule_class_cache")

# Persistent field-name strings: py_class_new copies the POINTERS from the
# caller's array into the class, so the strings themselves must outlive the
# array.  define_global_cstr pins them as module-lifetime globals (the C
# shim used static const char *fields[] for the same reason).
define_global_cstr("pcc_capi_capsule_field_pointer", "__pcc_capsule_pointer__")
define_global_cstr("pcc_capi_capsule_field_name", "__pcc_capsule_name__")
define_global_cstr("pcc_capi_capsule_field_context", "__pcc_capsule_context__")
define_global_cstr("pcc_capi_capsule_field_destructor", "__pcc_capsule_destructor__")


def _py_none() -> c_ptr:
    return global_load_ptr("py_None")


@c_abi_typed_export("pcc_capi_capsule_del_runtime", "void", ("ptr",))
def pcc_capi_capsule_del_runtime(capsule) -> None:
    if _is_capsule_object(capsule) == 0:
        return
    destructor_obj = py_instance_getattr(
        capsule, cstr("__pcc_capsule_destructor__")
    )
    if ptr_is_null(destructor_obj) or destructor_obj == _py_none():
        if not ptr_is_null(destructor_obj):
            py_decref(destructor_obj)
        return
    destructor_ptr = pcc_py_long_as_void_ptr(destructor_obj)
    py_decref(destructor_obj)
    if ptr_is_null(destructor_ptr) or py_err_occurred() != 0:
        return
    call_void_ptr1(destructor_ptr, capsule)


def _capsule_class() -> c_ptr:
    cls = global_load_ptr("pcc_capi_capsule_class_cache")
    if not ptr_is_null(cls):
        return cls
    # py_class_new expects an array of field-name pointers; the capsule class
    # uses the same layout the C shim's static const char *fields[] describes.
    names = malloc(4 * C_POINTER_SIZE)
    if ptr_is_null(names):
        return null()
    store_ptr(names, 0, global_addr("pcc_capi_capsule_field_pointer"))
    store_ptr(
        names,
        C_POINTER_SIZE,
        global_addr("pcc_capi_capsule_field_name"),
    )
    store_ptr(
        names,
        2 * C_POINTER_SIZE,
        global_addr("pcc_capi_capsule_field_context"),
    )
    store_ptr(
        names,
        3 * C_POINTER_SIZE,
        global_addr("pcc_capi_capsule_field_destructor"),
    )
    cls = py_class_new(cstr("capsule"), null(), 0, names, 4)
    if not ptr_is_null(cls):
        py_class_add_method(
            cls, cstr("__del__"), function_addr("pcc_capi_capsule_del_runtime")
        )
        pcc_gc_pin(cls)
        global_store_ptr("pcc_capi_capsule_class_cache", cls)
    return cls


def _is_capsule_object(obj) -> int:
    if ptr_is_null(obj) or is_tagged_int(obj):
        return 0
    tag: int = load_i32(obj, 8)
    if tag != PY_TYPE_INSTANCE and tag < PY_TYPE_USER_CLASS_START:
        return 0
    cls = pcc_gc_load_ptr(obj, ptr_add(obj, PYINSTANCEOBJECT_CLS_OFFSET))
    if ptr_eq(cls, _capsule_class()) != 0:
        return 1
    return 0


def _capsule_name_matches(name_obj, name) -> int:
    if ptr_is_null(name):
        if ptr_is_null(name_obj) or name_obj == _py_none():
            return 1
        return 0
    if (
        ptr_is_null(name_obj)
        or name_obj == _py_none()
        or is_tagged_int(name_obj)
    ):
        return 0
    if load_i32(name_obj, 8) != PY_TYPE_STR:  # PY_TYPE_STR
        return 0
    stored = py_str_utf8(name_obj)
    if ptr_is_null(stored):
        return 0
    if strlen(stored) != strlen(name):
        return 0
    return memcmp_c(stored, name, strlen(name)) == 0


def _strrchr(text, needle: int) -> int:
    n = strlen(text)
    i = n - 1
    while i >= 0:
        if load_i8(text, i) == needle:
            return i
        i -= 1
    return -1


def _type_error(message) -> None:
    py_raise_owned(py_exc_new(3, message))  # PY_EXC_TYPEERROR


def _value_error(message) -> None:
    py_raise_owned(py_exc_new(2, message))  # PY_EXC_VALUEERROR


def _runtime_error(message) -> None:
    py_raise_owned(py_exc_new(7, message))  # PY_EXC_RUNTIMEERROR


@c_abi_typed_export("pcc_py_capsule_new", "ptr", ("ptr", "ptr", "ptr"))
def pcc_py_capsule_new(pointer, name, destructor) -> c_ptr:
    if ptr_is_null(pointer):
        _value_error(cstr("PyCapsule_New called with NULL pointer"))
        return null()
    cls = _capsule_class()
    if ptr_is_null(cls):
        return null()
    capsule = py_instance_new(cls)
    if ptr_is_null(capsule):
        return null()
    ptr_obj = pcc_py_long_from_void_ptr(pointer)
    if ptr_is_null(name):
        name_obj = _py_none()
    else:
        name_obj = py_str_new(name, strlen(name))
    if ptr_is_null(destructor):
        destructor_obj = _py_none()
    else:
        destructor_obj = pcc_py_long_from_void_ptr(destructor)
    if (
        ptr_is_null(ptr_obj)
        or ptr_is_null(name_obj)
        or ptr_is_null(destructor_obj)
    ):
        if not ptr_is_null(ptr_obj):
            py_decref(ptr_obj)
        if not ptr_is_null(name_obj) and name_obj != _py_none():
            py_decref(name_obj)
        if not ptr_is_null(destructor_obj) and destructor_obj != _py_none():
            py_decref(destructor_obj)
        py_decref(capsule)
        return null()
    stored_pointer = py_instance_setattr(
        capsule, cstr("__pcc_capsule_pointer__"), ptr_obj
    )
    stored_name = py_instance_setattr(capsule, cstr("__pcc_capsule_name__"), name_obj)
    stored_destructor = py_instance_setattr(
        capsule, cstr("__pcc_capsule_destructor__"), destructor_obj
    )
    py_decref(ptr_obj)
    if name_obj != _py_none():
        py_decref(name_obj)
    if destructor_obj != _py_none():
        py_decref(destructor_obj)
    if stored_pointer != 0 or stored_name != 0 or stored_destructor != 0:
        py_decref(capsule)
        _runtime_error(cstr("failed to initialize capsule"))
        return null()
    return capsule


@c_abi_typed_export("PyCapsule_CheckExact", "i32", ("ptr",))
def PyCapsule_CheckExact(capsule) -> int:
    return _is_capsule_object(capsule)


@c_abi_typed_export("pcc_py_capsule_is_valid", "i32", ("ptr", "ptr"))
def pcc_py_capsule_is_valid(capsule, name) -> int:
    if _is_capsule_object(capsule) == 0:
        return 0
    name_obj = py_instance_getattr(capsule, cstr("__pcc_capsule_name__"))
    valid = _capsule_name_matches(name_obj, name)
    if not ptr_is_null(name_obj):
        py_decref(name_obj)
    return valid


@c_abi_typed_export("pcc_py_capsule_get_name", "ptr", ("ptr",))
def pcc_py_capsule_get_name(capsule) -> c_ptr:
    if _is_capsule_object(capsule) == 0:
        _type_error(cstr("expected capsule"))
        return null()
    name_obj = py_instance_getattr(capsule, cstr("__pcc_capsule_name__"))
    out = null()
    if (
        not ptr_is_null(name_obj)
        and name_obj != _py_none()
        and not is_tagged_int(name_obj)
        and load_i32(name_obj, 8) == PY_TYPE_STR  # PY_TYPE_STR
    ):
        out = py_str_utf8(name_obj)
    if not ptr_is_null(name_obj):
        py_decref(name_obj)
    return out


@c_abi_typed_export("PyCapsule_GetContext", "ptr", ("ptr",))
def PyCapsule_GetContext(capsule) -> c_ptr:
    if _is_capsule_object(capsule) == 0:
        _type_error(cstr("expected capsule"))
        return null()
    context_obj = py_instance_getattr(capsule, cstr("__pcc_capsule_context__"))
    if ptr_is_null(context_obj) or context_obj == _py_none():
        if not ptr_is_null(context_obj):
            py_decref(context_obj)
        return null()
    context = pcc_py_long_as_void_ptr(context_obj)
    py_decref(context_obj)
    return context


@c_abi_typed_export("PyCapsule_GetDestructor", "ptr", ("ptr",))
def PyCapsule_GetDestructor(capsule) -> c_ptr:
    if _is_capsule_object(capsule) == 0:
        _type_error(cstr("expected capsule"))
        return null()
    destructor_obj = py_instance_getattr(
        capsule, cstr("__pcc_capsule_destructor__")
    )
    if ptr_is_null(destructor_obj) or destructor_obj == _py_none():
        if not ptr_is_null(destructor_obj):
            py_decref(destructor_obj)
        return null()
    destructor_ptr = pcc_py_long_as_void_ptr(destructor_obj)
    py_decref(destructor_obj)
    if ptr_is_null(destructor_ptr) or py_err_occurred() != 0:
        return null()
    return destructor_ptr


@c_abi_typed_export("pcc_py_capsule_get_pointer", "ptr", ("ptr", "ptr"))
def pcc_py_capsule_get_pointer(capsule, name) -> c_ptr:
    if pcc_py_capsule_is_valid(capsule, name) == 0:
        _value_error(cstr("invalid capsule or capsule name"))
        return null()
    ptr_obj = py_instance_getattr(capsule, cstr("__pcc_capsule_pointer__"))
    pointer = pcc_py_long_as_void_ptr(ptr_obj)
    if not ptr_is_null(ptr_obj):
        py_decref(ptr_obj)
    return pointer


@c_abi_typed_export("PyCapsule_SetPointer", "i32", ("ptr", "ptr"))
def PyCapsule_SetPointer(capsule, pointer) -> int:
    if _is_capsule_object(capsule) == 0:
        _type_error(cstr("expected capsule"))
        return -1
    if ptr_is_null(pointer):
        _value_error(cstr("PyCapsule_SetPointer called with NULL pointer"))
        return -1
    ptr_obj = pcc_py_long_from_void_ptr(pointer)
    if ptr_is_null(ptr_obj):
        return -1
    stored = py_instance_setattr(capsule, cstr("__pcc_capsule_pointer__"), ptr_obj)
    py_decref(ptr_obj)
    if stored != 0:
        _runtime_error(cstr("failed to set capsule pointer"))
        return -1
    return 0


@c_abi_typed_export("PyCapsule_SetContext", "i32", ("ptr", "ptr"))
def PyCapsule_SetContext(capsule, context) -> int:
    if _is_capsule_object(capsule) == 0:
        _type_error(cstr("expected capsule"))
        return -1
    context_obj = null()
    if not ptr_is_null(context):
        context_obj = pcc_py_long_from_void_ptr(context)
        if ptr_is_null(context_obj):
            return -1
    stored = py_instance_setattr(
        capsule, cstr("__pcc_capsule_context__"), context_obj
    )
    if not ptr_is_null(context_obj):
        py_decref(context_obj)
    if stored != 0:
        _runtime_error(cstr("failed to set capsule context"))
        return -1
    return 0


@c_abi_typed_export("PyCapsule_SetDestructor", "i32", ("ptr", "ptr"))
def PyCapsule_SetDestructor(capsule, destructor) -> int:
    if _is_capsule_object(capsule) == 0:
        _type_error(cstr("expected capsule"))
        return -1
    destructor_obj = null()
    if not ptr_is_null(destructor):
        destructor_obj = pcc_py_long_from_void_ptr(destructor)
        if ptr_is_null(destructor_obj):
            return -1
    stored = py_instance_setattr(
        capsule, cstr("__pcc_capsule_destructor__"), destructor_obj
    )
    if not ptr_is_null(destructor_obj):
        py_decref(destructor_obj)
    if stored != 0:
        _runtime_error(cstr("failed to set capsule destructor"))
        return -1
    return 0


@c_abi_typed_export("pcc_py_capsule_set_name", "i32", ("ptr", "ptr"))
def pcc_py_capsule_set_name(capsule, name) -> int:
    if _is_capsule_object(capsule) == 0:
        _type_error(cstr("expected capsule"))
        return -1
    if ptr_is_null(name):
        name_obj = _py_none()
    else:
        name_obj = py_str_new(name, strlen(name))
        if ptr_is_null(name_obj):
            return -1
    stored = py_instance_setattr(capsule, cstr("__pcc_capsule_name__"), name_obj)
    if name_obj != _py_none():
        py_decref(name_obj)
    if stored != 0:
        _runtime_error(cstr("failed to set capsule name"))
        return -1
    return 0


_CAPSULE_IMPORT_MODULE = 0
_CAPSULE_IMPORT_VALUE = 1
_CAPSULE_IMPORT_ERROR = 2
_CAPSULE_IMPORT_COUNT = 3

define_global_i32("pcc_capsule_import_owned_map", _CAPSULE_IMPORT_COUNT)
pcc_gc_frame_enter = extern("pcc_gc_frame_enter", (c_ptr, c_ptr), c_void)
pcc_gc_frame_leave = extern("pcc_gc_frame_leave", (c_ptr,), c_void)
pcc_gc_foreign_lease_acquire = extern("pcc_gc_foreign_lease_acquire", (c_ptr,), c_int64)
pcc_gc_foreign_lease_release = extern("pcc_gc_foreign_lease_release", (c_ptr, c_int64), c_int64)
pcc_gc_note_slot_write_barrier = extern("pcc_gc_note_slot_write_barrier", (c_ptr, c_ptr, c_ptr), c_void)
pcc_gc_store_root = extern("pcc_gc_store_root", (c_ptr, c_ptr), c_void)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)
py_tls_exc_swap_slot = extern("py_tls_exc_swap_slot", (c_ptr,), c_void)
py_clear_exception = extern("py_clear_exception", (), c_void)
py_runtime_error_if_unset = extern("py_runtime_error_if_unset", (c_ptr, c_ptr), c_ptr)
pcc_platform_abort = extern("pcc_platform_abort", (), c_void)


def _capsule_import_adopt(slots, tokens, index: int) -> int:
    # The caller publishes NEW in its registered slot before this may park.
    offset: int = index * C_POINTER_SIZE
    slot = ptr_add(slots, offset)
    token: int = pcc_gc_foreign_lease_acquire(slot)
    if token < 0:
        py_runtime_error_if_unset(cstr("capsule import"), cstr("result owner lease failed"))
        return -1
    store_i64(tokens, offset, token)
    pcc_py_gc_minor_graph_lock()
    pcc_gc_note_slot_write_barrier(null(), slot, load_ptr(slot, 0))
    pcc_py_gc_minor_graph_unlock()
    return 0



def _capsule_import_drop(slots, tokens, index: int) -> None:
    offset: int = index * C_POINTER_SIZE
    slot = ptr_add(slots, offset)
    if pcc_gc_foreign_lease_release(slot, load_i64(tokens, offset)) != 0:
        pcc_platform_abort()
        return
    store_i64(tokens, offset, 0)
    pcc_gc_store_root(slot, null())

def _capsule_import_body(name, slots, tokens) -> c_ptr:
    if ptr_is_null(name) or load_i8(name, 0) == 0:
        _value_error(cstr("empty capsule import name"))
        return null()
    dot = _strrchr(name, 46)  # '.'
    if dot == -1 or dot == 0 or dot + 1 == strlen(name):
        _value_error(cstr("capsule import name must be module.attr"))
        return null()
    module_len = dot
    module_name = malloc(module_len + 1)
    if ptr_is_null(module_name):
        _runtime_error(cstr("out of memory importing capsule"))
        return null()
    memcpy_c(module_name, name, module_len)
    store_i8(module_name, module_len, 0)
    store_ptr(slots, _CAPSULE_IMPORT_MODULE * C_POINTER_SIZE,
              py_native_extension_import_by_name(module_name))
    status: int = _capsule_import_adopt(slots, tokens, _CAPSULE_IMPORT_MODULE)
    free_c(module_name)
    if status != 0:
        return null()
    if ptr_is_null(load_ptr(slots, _CAPSULE_IMPORT_MODULE * C_POINTER_SIZE)):
        if py_err_occurred() == 0:
            _runtime_error(cstr("capsule import module not found"))
        return null()
    store_ptr(slots, _CAPSULE_IMPORT_VALUE * C_POINTER_SIZE, py_obj_getattr(
        load_ptr(slots, _CAPSULE_IMPORT_MODULE * C_POINTER_SIZE), ptr_add(name, dot + 1),
    ))
    if _capsule_import_adopt(slots, tokens, _CAPSULE_IMPORT_VALUE) != 0:
        return null()
    py_tls_exc_swap_slot(ptr_add(slots, _CAPSULE_IMPORT_ERROR * C_POINTER_SIZE))
    _capsule_import_drop(slots, tokens, _CAPSULE_IMPORT_MODULE)
    py_clear_exception()
    py_tls_exc_swap_slot(ptr_add(slots, _CAPSULE_IMPORT_ERROR * C_POINTER_SIZE))
    if ptr_is_null(load_ptr(slots, _CAPSULE_IMPORT_VALUE * C_POINTER_SIZE)):
        return null()
    return pcc_py_capsule_get_pointer(
        load_ptr(slots, _CAPSULE_IMPORT_VALUE * C_POINTER_SIZE), name,
    )


@c_abi_typed_export("PyCapsule_Import", "ptr", ("ptr", "i32"))
def PyCapsule_Import(name, no_block: int) -> c_ptr:
    slots = stack_alloc(_CAPSULE_IMPORT_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_CAPSULE_IMPORT_COUNT * C_POINTER_SIZE)
    memset(slots, 0, _CAPSULE_IMPORT_COUNT * C_POINTER_SIZE)
    memset(tokens, 0, _CAPSULE_IMPORT_COUNT * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_capsule_import_owned_map"), slots)
    pointer = _capsule_import_body(name, slots, tokens)
    # The returned pointer is foreign data, not a managed object. Keep the
    # capsule owner until all accesses finish, and preserve the exact error
    # while either imported object can run a finalizer during disposal.
    py_tls_exc_swap_slot(ptr_add(slots, _CAPSULE_IMPORT_ERROR * C_POINTER_SIZE))
    index: int = 0
    while index < _CAPSULE_IMPORT_ERROR:
        _capsule_import_drop(slots, tokens, index)
        index = index + 1
    py_clear_exception()
    py_tls_exc_swap_slot(ptr_add(slots, _CAPSULE_IMPORT_ERROR * C_POINTER_SIZE))
    pcc_gc_frame_leave(slots)
    return pointer


# Runtime-internal callers use an explicitly pcc-namespaced ABI.  This keeps
# their object-layout contract distinct from the public CPython-compatible
# symbol names at a mixed libpython final link.
@c_abi_typed_export("PyCapsule_New", "ptr", ("ptr", "ptr", "ptr"))
def PyCapsule_New(pointer, name, destructor) -> c_ptr:
    return pcc_py_capsule_new(pointer, name, destructor)


@c_abi_typed_export("PyCapsule_GetPointer", "ptr", ("ptr", "ptr"))
def PyCapsule_GetPointer(capsule, name) -> c_ptr:
    return pcc_py_capsule_get_pointer(capsule, name)


@c_abi_typed_export("PyCapsule_GetName", "ptr", ("ptr",))
def PyCapsule_GetName(capsule) -> c_ptr:
    return pcc_py_capsule_get_name(capsule)


@c_abi_typed_export("PyCapsule_IsValid", "i32", ("ptr", "ptr"))
def PyCapsule_IsValid(capsule, name) -> int:
    return pcc_py_capsule_is_valid(capsule, name)


@c_abi_typed_export("PyCapsule_SetName", "i32", ("ptr", "ptr"))
def PyCapsule_SetName(capsule, name) -> int:
    return pcc_py_capsule_set_name(capsule, name)
