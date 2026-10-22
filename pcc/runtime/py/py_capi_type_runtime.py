"""pcc-Python owners for the no-libpython C-API type-token surface.

This module replaces the C py_capi_shim.c type-bridge block: the 24 builtin
Py*_Type recognition tokens (linker-visible data globals that C extensions
compare against and read tp_name/tp_flags from), the C-extension dynamic type
registry, pcc_capi_type / pcc_capi_type_addr / pcc_capi_typecheck, the builtin
tag mapping, the cext tag management helpers, Py_SIZE-style size access, and
the tp_base subtype walk.

Owned surface (stable C ABI names):

  data:  24 Py*_Type tokens, pcc_capi_cext_types[1024],
         pcc_capi_cext_type_count, pcc_capi_cext_type_modules[1024],
         plus the pcc_capi_type_addr static cache slots
  funcs: pcc_capi_builtin_type_token, pcc_capi_is_type_object,
         pcc_capi_is_type_object_value, pcc_capi_is_cext_type_tag,
         pcc_capi_cext_tag_for, pcc_capi_cext_type_for_object,
         pcc_capi_type, pcc_capi_type_addr, pcc_capi_typecheck,
         pcc_capi_size, pcc_capi_set_size, PyType_IsSubtype,
         pcc_capi_type_object_issubclass

The token struct mirrors fake_libc_include/Python.h ``_typeobject`` through
tp_vectorcall (52 x 8-byte words = 416 bytes).  Only ob_base {refcount=1,
type_tag=0, flags=0}, tp_name, and tp_flags=READY are set; everything else is
zero, exactly like the C ``PCC_CAPI_TYPEOBJ`` macro.

Registry contract (shared with the retained C helpers that stay in
py_capi_shim.c for later slices): pcc_capi_cext_types holds the registered
PyTypeObject* pointers, pcc_capi_cext_type_count the next free slot, and
pcc_capi_cext_type_modules the owning module per dynamic tag.  The C side
declares these extern and keeps writing through them.

PyType_Ready / PyType_Modified / PyType_GenericAlloc / PyType_FromSpec /
PyType_GetSlot / PyType_GetFlags and the cext object helpers stay C-side for
now; they call the exported pcc-Python helpers below via extern.
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import (
    PY_TYPE_BOOL,
    PY_TYPE_BYTEARRAY,
    PY_TYPE_BYTES,
    PY_TYPE_CEXT_TAG_BASE,
    PY_TYPE_CLASS,
    PY_TYPE_COMPLEX,
    PY_TYPE_DICT,
    PY_TYPE_FLOAT,
    PY_TYPE_FUNC,
    PY_TYPE_INT,
    PY_TYPE_LIST,
    PY_TYPE_MEMORYVIEW,
    PY_TYPE_NONE,
    PY_TYPE_SET,
    PY_TYPE_STR,
    PY_TYPE_TUPLE,
)

from pcc.extern import c_abi_typed_export, c_double, c_int32, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    cstr,
    define_global_cstr,
    define_global_i32,
    define_global_null_ptr_array,
    define_global_ptr_null,
    define_global_ptr_to_global,
    define_global_struct_words,
    f64_pair_first,
    f64_pair_make,
    f64_pair_second,
    function_addr,
    global_addr,
    is_tagged_int,
    load_f64,
    load_i32,
    load_i64,
    load_ptr,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    store_i32,
    store_i64,
    store_ptr,
)

py_obj_is_slice = extern("py_obj_is_slice", (c_ptr,), c_int64)
py_builtin_type_class_tag = extern("py_builtin_type_class_tag", (c_ptr,), c_int64)
py_list_len = extern("py_list_len", (c_ptr,), c_int64)
py_tuple_len = extern("py_tuple_len", (c_ptr,), c_int64)
py_str_len = extern("py_str_len", (c_ptr,), c_int64)
py_bytes_len = extern("py_bytes_len", (c_ptr,), c_int64)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)

# --- C-extension dynamic type registry -------------------------------
# Same layout and contract as the C shim: 1024 slots above the builtin
# tag range, count of registered types, and the owning module per tag.  Keep
# the ABI values as literals below: library-mode modules do not run module-top
# initializers before their exported functions are called.

define_global_null_ptr_array("pcc_capi_cext_types", 1024)
define_global_null_ptr_array("pcc_capi_cext_type_modules", 1024)
define_global_i32("pcc_capi_cext_type_count", 0)


def _cext_tag_for_type(type_ptr) -> int:
    """Assign (or fetch the cached) dynamic pcc type_tag for a C-ext type.

    The tag is stashed in tp_version_tag (u32 at word 49); the registry maps
    it back.  Mirrors pcc_capi_cext_tag_for.
    """
    if ptr_is_null(type_ptr):
        return PY_TYPE_NONE  # PY_TYPE_NONE
    existing: int = load_i32(type_ptr, 392)  # tp_version_tag
    if existing != 0:
        return existing
    count: int = load_i32(global_addr("pcc_capi_cext_type_count"), 0)
    if count >= (1024):
        return 0
    tag: int = (PY_TYPE_CEXT_TAG_BASE) + count
    store_ptr(
        global_addr("pcc_capi_cext_types"), count * 8, type_ptr
    )
    store_i32(global_addr("pcc_capi_cext_type_count"), 0, count + 1)
    store_i32(type_ptr, 392, tag)
    return tag


@c_abi_typed_export("pcc_capi_cext_tag_for", "i32", ("ptr",))
def pcc_capi_cext_tag_for(type_ptr) -> int:
    return _cext_tag_for_type(type_ptr)


@c_abi_typed_export("pcc_capi_is_cext_type_tag", "i64", ("i64",))
def pcc_capi_is_cext_type_tag(type_tag: int) -> int:
    count: int = load_i32(global_addr("pcc_capi_cext_type_count"), 0)
    offset: int = type_tag - (PY_TYPE_CEXT_TAG_BASE)
    if offset >= 0 and offset < count:
        return 1
    return 0


@c_abi_typed_export("pcc_capi_cext_type_for_object", "ptr", ("ptr",))
def pcc_capi_cext_type_for_object(o) -> c_ptr:
    if ptr_is_null(o) or is_tagged_int(o):
        return null()
    type_tag: int = load_i32(o, 8)
    count: int = load_i32(global_addr("pcc_capi_cext_type_count"), 0)
    offset: int = type_tag - (PY_TYPE_CEXT_TAG_BASE)
    if offset < 0 or offset >= count:
        return null()
    return load_ptr(global_addr("pcc_capi_cext_types"), offset * 8)


define_global_cstr("pcc_capi_type_name_type", "type")
define_global_cstr("pcc_capi_type_name_object", "object")
define_global_cstr("pcc_capi_type_name_tuple", "tuple")
define_global_cstr("pcc_capi_type_name_list", "list")
define_global_cstr("pcc_capi_type_name_dict", "dict")
define_global_cstr("pcc_capi_type_name_str", "str")
define_global_cstr("pcc_capi_type_name_long", "int")
define_global_cstr("pcc_capi_type_name_float", "float")
define_global_cstr("pcc_capi_type_name_bool", "bool")
define_global_cstr("pcc_capi_type_name_bytes", "bytes")
define_global_cstr("pcc_capi_type_name_bytearray", "bytearray")
define_global_cstr("pcc_capi_type_name_set", "set")
define_global_cstr("pcc_capi_type_name_frozenset", "frozenset")
define_global_cstr("pcc_capi_type_name_slice", "slice")
define_global_cstr("pcc_capi_type_name_complex", "complex")
define_global_cstr("pcc_capi_type_name_module", "module")
define_global_cstr("pcc_capi_type_name_function", "function")
define_global_cstr("pcc_capi_type_name_cfunction", "builtin_function_or_method")
define_global_cstr("pcc_capi_type_name_member_descr", "member_descriptor")
define_global_cstr("pcc_capi_type_name_getset_descr", "getset_descriptor")
define_global_cstr("pcc_capi_type_name_method_descr", "method_descriptor")
define_global_cstr("pcc_capi_type_name_dictproxy", "mappingproxy")
define_global_cstr("pcc_capi_type_name_memoryview", "memoryview")

# --- Builtin type tokens ---------------------------------------------
# Each token is the fake_libc _typeobject layout (52 x 8-byte words):
# ob_base {refcount=1, type_tag=0, flags=0}, tp_name at word 4,
# tp_flags=READY (0x1000) at word 22, everything else zero.

define_global_struct_words(
    "PyType_Type",
    1, 0, 0, 0, "pcc_capi_type_name_type",
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0x1000,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
)
define_global_struct_words(
    "PyBaseObject_Type",
    1, 0, 0, 0, "pcc_capi_type_name_object",
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0x1000,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
)
define_global_struct_words(
    "PyTuple_Type",
    1, 0, 0, 0, "pcc_capi_type_name_tuple",
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0x1000,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
)
define_global_struct_words(
    "PyList_Type",
    1, 0, 0, 0, "pcc_capi_type_name_list",
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0x1000,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
)
define_global_struct_words(
    "PyDict_Type",
    1, 0, 0, 0, "pcc_capi_type_name_dict",
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0x1000,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
)
define_global_struct_words(
    "PyUnicode_Type",
    1, 0, 0, 0, "pcc_capi_type_name_str",
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0x1000,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
)
define_global_struct_words(
    "PyLong_Type",
    1, 0, 0, 0, "pcc_capi_type_name_long",
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0x1000,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
)
define_global_struct_words(
    "PyFloat_Type",
    1, 0, 0, 0, "pcc_capi_type_name_float",
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0x1000,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
)
define_global_struct_words(
    "PyBool_Type",
    1, 0, 0, 0, "pcc_capi_type_name_bool",
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0x1000,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
)
define_global_struct_words(
    "PyBytes_Type",
    1, 0, 0, 0, "pcc_capi_type_name_bytes",
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0x1000,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
)
define_global_struct_words(
    "PyByteArray_Type",
    1, 0, 0, 0, "pcc_capi_type_name_bytearray",
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0x1000,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
)
define_global_struct_words(
    "PySet_Type",
    1, 0, 0, 0, "pcc_capi_type_name_set",
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0x1000,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
)
define_global_struct_words(
    "PyFrozenSet_Type",
    1, 0, 0, 0, "pcc_capi_type_name_frozenset",
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0x1000,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
)
define_global_struct_words(
    "PySlice_Type",
    1, 0, 0, 0, "pcc_capi_type_name_slice",
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0x1000,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
)
define_global_struct_words(
    "PyComplex_Type",
    1, 0, 0, 0, "pcc_capi_type_name_complex",
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0x1000,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
)
define_global_struct_words(
    "PyModule_Type",
    1, 0, 0, 0, "pcc_capi_type_name_module",
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0x1000,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
)
define_global_struct_words(
    "PyFunction_Type",
    1, 0, 0, 0, "pcc_capi_type_name_function",
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0x1000,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
)
define_global_struct_words(
    "PyCFunction_Type",
    1, 0, 0, 0, "pcc_capi_type_name_cfunction",
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0x1000,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
)
define_global_struct_words(
    "PyMemberDescr_Type",
    1, 0, 0, 0, "pcc_capi_type_name_member_descr",
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0x1000,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
)
define_global_struct_words(
    "PyGetSetDescr_Type",
    1, 0, 0, 0, "pcc_capi_type_name_getset_descr",
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0x1000,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
)
define_global_struct_words(
    "PyMethodDescr_Type",
    1, 0, 0, 0, "pcc_capi_type_name_method_descr",
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0x1000,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
)
define_global_struct_words(
    "PyDictProxy_Type",
    1, 0, 0, 0, "pcc_capi_type_name_dictproxy",
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0x1000,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
)
define_global_struct_words(
    "PyMemoryView_Type",
    1, 0, 0, 0, "pcc_capi_type_name_memoryview",
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0x1000,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
)

# --- Builtin tag -> token mapping ------------------------------------

# pcc_capi_type_addr's static cache slots (the C shim keeps these as
# function-local statics; here they are linker-visible pcc-Python globals
# the Py_TYPE macro family resolves through).
define_global_ptr_to_global("pcc_capi_tagged_int_type", "PyLong_Type")
define_global_ptr_null("pcc_capi_unknown_type")
define_global_null_ptr_array("pcc_capi_builtin_types", 256)
define_global_ptr_null("pcc_capi_func_type")


def _is_type_object(o) -> int:
    if ptr_is_null(o) or is_tagged_int(o):
        return 0
    if ptr_eq(o, global_addr("PyType_Type")):
        return 1
    if ptr_eq(o, global_addr("PyBaseObject_Type")):
        return 1
    if ptr_eq(o, global_addr("PyTuple_Type")):
        return 1
    if ptr_eq(o, global_addr("PyList_Type")):
        return 1
    if ptr_eq(o, global_addr("PyDict_Type")):
        return 1
    if ptr_eq(o, global_addr("PyUnicode_Type")):
        return 1
    if ptr_eq(o, global_addr("PyLong_Type")):
        return 1
    if ptr_eq(o, global_addr("PyFloat_Type")):
        return 1
    if ptr_eq(o, global_addr("PyBool_Type")):
        return 1
    if ptr_eq(o, global_addr("PyBytes_Type")):
        return 1
    if ptr_eq(o, global_addr("PyByteArray_Type")):
        return 1
    if ptr_eq(o, global_addr("PySet_Type")):
        return 1
    if ptr_eq(o, global_addr("PyFrozenSet_Type")):
        return 1
    if ptr_eq(o, global_addr("PySlice_Type")):
        return 1
    if ptr_eq(o, global_addr("PyComplex_Type")):
        return 1
    if ptr_eq(o, global_addr("PyModule_Type")):
        return 1
    if ptr_eq(o, global_addr("PyFunction_Type")):
        return 1
    if ptr_eq(o, global_addr("PyCFunction_Type")):
        return 1
    if ptr_eq(o, global_addr("PyMemberDescr_Type")):
        return 1
    if ptr_eq(o, global_addr("PyGetSetDescr_Type")):
        return 1
    if ptr_eq(o, global_addr("PyMethodDescr_Type")):
        return 1
    if ptr_eq(o, global_addr("PyDictProxy_Type")):
        return 1
    if ptr_eq(o, global_addr("PyMemoryView_Type")):
        return 1
    count: int = load_i32(global_addr("pcc_capi_cext_type_count"), 0)
    i: int = 0
    while i < count:
        if ptr_eq(o, load_ptr(global_addr("pcc_capi_cext_types"), i * 8)):
            return 1
        i += 1
    return 0


@c_abi_typed_export("pcc_capi_is_type_object", "i32", ("ptr",))
def pcc_capi_is_type_object(o) -> int:
    return _is_type_object(o)


@c_abi_typed_export("pcc_capi_is_type_object_value", "i64", ("ptr",))
def pcc_capi_is_type_object_value(value) -> int:
    return _is_type_object(value)


@c_abi_typed_export("pcc_capi_builtin_type_token", "ptr", ("ptr",))
def pcc_capi_builtin_type_token(value) -> c_ptr:
    tag: int = py_builtin_type_class_tag(value)
    if tag == PY_TYPE_BOOL:  # PY_TYPE_BOOL
        return global_addr("PyBool_Type")
    if tag == PY_TYPE_INT:  # PY_TYPE_INT
        return global_addr("PyLong_Type")
    if tag == PY_TYPE_FLOAT:  # PY_TYPE_FLOAT
        return global_addr("PyFloat_Type")
    if tag == PY_TYPE_STR:  # PY_TYPE_STR
        return global_addr("PyUnicode_Type")
    if tag == PY_TYPE_LIST:  # PY_TYPE_LIST
        return global_addr("PyList_Type")
    if tag == PY_TYPE_DICT:  # PY_TYPE_DICT
        return global_addr("PyDict_Type")
    if tag == PY_TYPE_TUPLE:  # PY_TYPE_TUPLE
        return global_addr("PyTuple_Type")
    if tag == PY_TYPE_SET:  # PY_TYPE_SET
        return global_addr("PySet_Type")
    if tag == PY_TYPE_CLASS:  # PY_TYPE_CLASS
        return global_addr("PyType_Type")
    if tag == PY_TYPE_COMPLEX:  # PY_TYPE_COMPLEX
        return global_addr("PyComplex_Type")
    if tag == PY_TYPE_BYTES:  # PY_TYPE_BYTES
        return global_addr("PyBytes_Type")
    if tag == PY_TYPE_BYTEARRAY:  # PY_TYPE_BYTEARRAY
        return global_addr("PyByteArray_Type")
    if tag == PY_TYPE_MEMORYVIEW:  # PY_TYPE_MEMORYVIEW
        return global_addr("PyMemoryView_Type")
    if tag == -1:  # object class
        return global_addr("PyBaseObject_Type")
    return value


@c_abi_typed_export("pcc_capi_type", "ptr", ("ptr",))
def pcc_capi_type(o) -> c_ptr:
    if ptr_is_null(o):
        return null()
    if is_tagged_int(o):
        return global_addr("PyLong_Type")
    if _is_type_object(o) != 0:
        # Registered type objects may carry a custom metaclass in the
        # compatibility ob_type slot; preserve it instead of flattening to
        # the builtin `type`.
        ob_type: c_ptr = load_ptr(o, 16)
        if not ptr_is_null(ob_type):
            return ob_type
        return global_addr("PyType_Type")
    if py_obj_is_slice(o) != 0:
        return global_addr("PySlice_Type")
    tag: int = load_i32(o, 8)
    count: int = load_i32(global_addr("pcc_capi_cext_type_count"), 0)
    if tag >= (PY_TYPE_CEXT_TAG_BASE) and tag < (PY_TYPE_CEXT_TAG_BASE) + count:
        index: int = tag - PY_TYPE_CEXT_TAG_BASE
        return load_ptr(global_addr("pcc_capi_cext_types"), index * 8)
    if tag == PY_TYPE_BOOL:  # PY_TYPE_BOOL
        return global_addr("PyBool_Type")
    if tag == PY_TYPE_INT:  # PY_TYPE_INT
        return global_addr("PyLong_Type")
    if tag == PY_TYPE_FLOAT:  # PY_TYPE_FLOAT
        return global_addr("PyFloat_Type")
    if tag == PY_TYPE_STR:  # PY_TYPE_STR
        return global_addr("PyUnicode_Type")
    if tag == PY_TYPE_LIST:  # PY_TYPE_LIST
        return global_addr("PyList_Type")
    if tag == PY_TYPE_DICT:  # PY_TYPE_DICT
        return global_addr("PyDict_Type")
    if tag == PY_TYPE_TUPLE:  # PY_TYPE_TUPLE
        return global_addr("PyTuple_Type")
    if tag == PY_TYPE_SET:  # PY_TYPE_SET
        return global_addr("PySet_Type")
    if tag == PY_TYPE_COMPLEX:  # PY_TYPE_COMPLEX
        return global_addr("PyComplex_Type")
    if tag == PY_TYPE_BYTES:  # PY_TYPE_BYTES
        return global_addr("PyBytes_Type")
    if tag == PY_TYPE_BYTEARRAY:  # PY_TYPE_BYTEARRAY
        return global_addr("PyByteArray_Type")
    if tag == PY_TYPE_FUNC:  # PY_TYPE_FUNC
        capi_method: c_ptr = load_ptr(o, 16)
        if not ptr_is_null(capi_method):
            return global_addr("PyCFunction_Type")
        return global_addr("PyFunction_Type")
    return null()


@c_abi_typed_export("pcc_capi_type_addr", "ptr", ("ptr",))
def pcc_capi_type_addr(o) -> c_ptr:
    if ptr_is_null(o):
        return global_addr("pcc_capi_unknown_type")
    if is_tagged_int(o):
        return global_addr("pcc_capi_tagged_int_type")
    if _is_type_object(o) != 0:
        # Lazy-populate the compatibility ob_type slot so Py_TYPE reads it.
        ob_type: c_ptr = load_ptr(o, 16)
        if ptr_is_null(ob_type):
            store_ptr(o, 16, global_addr("PyType_Type"))
        return ptr_add(o, 16)
    tag: int = load_i32(o, 8)
    if tag == PY_TYPE_FUNC:  # PY_TYPE_FUNC
        store_ptr(
            global_addr("pcc_capi_func_type"), 0, pcc_capi_type(o)
        )
        return global_addr("pcc_capi_func_type")
    count: int = load_i32(global_addr("pcc_capi_cext_type_count"), 0)
    if tag >= (PY_TYPE_CEXT_TAG_BASE) and tag < (PY_TYPE_CEXT_TAG_BASE) + count:
        return ptr_add(o, 16)
    if tag >= PY_TYPE_NONE and tag < 256:
        slot: c_ptr = load_ptr(
            global_addr("pcc_capi_builtin_types"), tag * 8
        )
        if ptr_is_null(slot):
            store_ptr(
                global_addr("pcc_capi_builtin_types"),
                tag * 8,
                pcc_capi_type(o),
            )
        return ptr_add(global_addr("pcc_capi_builtin_types"), tag * 8)
    store_ptr(global_addr("pcc_capi_unknown_type"), 0, pcc_capi_type(o))
    return global_addr("pcc_capi_unknown_type")


@c_abi_typed_export("pcc_capi_size", "i64", ("ptr",))
def pcc_capi_size(o) -> int:
    if ptr_is_null(o) or is_tagged_int(o):
        return 0
    tag: int = load_i32(o, 8)
    if tag == PY_TYPE_LIST:  # PY_TYPE_LIST
        return py_list_len(o)
    if tag == PY_TYPE_TUPLE:  # PY_TYPE_TUPLE
        return py_tuple_len(o)
    if tag == PY_TYPE_STR:  # PY_TYPE_STR
        return py_str_len(o)
    if tag == PY_TYPE_BYTES or tag == PY_TYPE_BYTEARRAY or tag == PY_TYPE_MEMORYVIEW:  # BYTES / BYTEARRAY / MEMORYVIEW
        return py_bytes_len(o)
    if pcc_capi_is_cext_type_tag(tag) != 0:
        # cext var objects store ob_size at header + ob_type (offset 24).
        return load_i64(o, 24)
    return 0


@c_abi_typed_export("pcc_capi_set_size", "void", ("ptr", "i64"))
def pcc_capi_set_size(o, size: int) -> None:
    if ptr_is_null(o) or is_tagged_int(o):
        return
    tag: int = load_i32(o, 8)
    if pcc_capi_is_cext_type_tag(tag) != 0:
        store_i64(o, 24, size)


@c_abi_typed_export("pcc_capi_typecheck", "i32", ("ptr", "ptr"))
def pcc_capi_typecheck(o, t) -> int:
    if ptr_is_null(o) or ptr_is_null(t):
        return 0
    ot: c_ptr = pcc_capi_type(o)
    guard: int = 0
    while not ptr_is_null(ot) and guard < 64:
        if ptr_eq(ot, t):
            return 1
        ot = load_ptr(ot, 264)  # tp_base
        guard += 1
    return 0


@c_abi_typed_export("PyType_IsSubtype", "i32", ("ptr", "ptr"))
def PyType_IsSubtype(a, b) -> int:
    guard: int = 0
    while not ptr_is_null(a) and guard < 64:
        if ptr_eq(a, b):
            return 1
        a = load_ptr(a, 264)  # tp_base
        guard += 1
    return 0


@c_abi_typed_export("pcc_capi_type_object_issubclass", "i64", ("ptr", "ptr"))
def pcc_capi_type_object_issubclass(derived, cls) -> int:
    if _is_type_object(derived) == 0 or _is_type_object(cls) == 0:
        return 0
    return PyType_IsSubtype(derived, cls)


def _type_error(message) -> None:
    py_raise_owned(py_exc_new(3, message))  # PY_EXC_TYPEERROR


# --- PyComplex_* numeric bridge ---------------------------------------
# Owns the 7 PyComplex_* C-API symbols.  PyComplexObject layout: 16-byte
# header, real at offset 16, imag at offset 24 (py_internal.h).  Complex
# values are built through the pcc-Python py_complex_new owner.

py_complex_new = extern("py_complex_new", (c_double, c_double), c_ptr)
PyFloat_AsDouble = extern("PyFloat_AsDouble", (c_ptr,), c_double)


def _is_complex(obj) -> int:
    if ptr_is_null(obj) or is_tagged_int(obj):
        return 0
    if load_i32(obj, 8) == PY_TYPE_COMPLEX:  # PY_TYPE_COMPLEX
        return 1
    return 0


@c_abi_typed_export("PyComplex_FromDoubles", "ptr", ("f64", "f64"))
def PyComplex_FromDoubles(real: float, imag: float) -> c_ptr:
    return py_complex_new(real, imag)


@c_abi_typed_export("PyComplex_RealAsDouble", "f64", ("ptr",))
def PyComplex_RealAsDouble(obj) -> float:
    if _is_complex(obj) != 0:
        return load_f64(obj, 16)
    return PyFloat_AsDouble(obj)


@c_abi_typed_export("PyComplex_ImagAsDouble", "f64", ("ptr",))
def PyComplex_ImagAsDouble(obj) -> float:
    if _is_complex(obj) != 0:
        return load_f64(obj, 24)
    if not ptr_is_null(obj) and (
        is_tagged_int(obj)
        or load_i32(obj, 8) == PY_TYPE_INT  # PY_TYPE_INT
        or load_i32(obj, 8) == PY_TYPE_BOOL  # PY_TYPE_BOOL
        or load_i32(obj, 8) == PY_TYPE_FLOAT  # PY_TYPE_FLOAT
    ):
        return 0.0
    _type_error(cstr("expected complex-compatible object"))
    return -1.0


@c_abi_typed_export("PyComplex_Check", "i32", ("ptr",))
def PyComplex_Check(obj) -> int:
    return _is_complex(obj)


@c_abi_typed_export("PyComplex_CheckExact", "i32", ("ptr",))
def PyComplex_CheckExact(obj) -> int:
    return _is_complex(obj)


@c_abi_typed_export("PyComplex_FromCComplex", "ptr", ("{f64,f64}",))
def PyComplex_FromCComplex(value: complex) -> c_ptr:
    return PyComplex_FromDoubles(
        f64_pair_first(value),
        f64_pair_second(value),
    )


@c_abi_typed_export("PyComplex_AsCComplex", "{f64,f64}", ("ptr",))
def PyComplex_AsCComplex(obj) -> complex:
    if _is_complex(obj) != 0:
        return f64_pair_make(load_f64(obj, 16), load_f64(obj, 24))
    if not ptr_is_null(obj) and (
        is_tagged_int(obj)
        or load_i32(obj, 8) == PY_TYPE_INT  # PY_TYPE_INT
        or load_i32(obj, 8) == PY_TYPE_BOOL  # PY_TYPE_BOOL
        or load_i32(obj, 8) == PY_TYPE_FLOAT  # PY_TYPE_FLOAT
    ):
        return f64_pair_make(PyFloat_AsDouble(obj), 0.0)
    _type_error(cstr("expected complex-compatible object"))
    return f64_pair_make(-1.0, 0.0)


# --- PyMutex_* / PyGILState_* / PyOS_* trivial bridge ----------------
# Free-threading mutex and GIL detach/reattach: the no-libpython shim is
# single-interpreter, so lock/unlock and GIL save/restore are no-ops
# (mirrors the C shim).  The integer and floating scanners are owned by the
# pcc-Python freestanding libc layer, so Linux acquires no host scanner.

pcc_strtod = extern("strtod", (c_ptr, c_ptr), c_double)
strtol_c = extern("strtol", (c_ptr, c_ptr, c_int32), c_int64)
strtoul_c = extern("strtoul", (c_ptr, c_ptr, c_int32), c_int64)


@c_abi_typed_export("PyMutex_Lock", "void", ("ptr",))
def PyMutex_Lock(m) -> None:
    return


@c_abi_typed_export("PyMutex_Unlock", "void", ("ptr",))
def PyMutex_Unlock(m) -> None:
    return


@c_abi_typed_export("PyGILState_Ensure", "i64", ())
def PyGILState_Ensure() -> int:
    return 0


@c_abi_typed_export("PyGILState_Release", "void", ("i64",))
def PyGILState_Release(state: int) -> None:
    return


@c_abi_typed_export("PyGILState_Check", "i32", ())
def PyGILState_Check() -> int:
    return 1


@c_abi_typed_export("PyOS_strtol", "i64", ("ptr", "ptr", "i32"))
def PyOS_strtol(text, endptr, base: int) -> int:
    return strtol_c(text, endptr, base)


@c_abi_typed_export("PyOS_strtoul", "i64", ("ptr", "ptr", "i32"))
def PyOS_strtoul(text, endptr, base: int) -> int:
    return strtoul_c(text, endptr, base)


@c_abi_typed_export("PyOS_string_to_double", "f64", ("ptr", "ptr", "ptr"))
def PyOS_string_to_double(text, endptr, overflow_exc) -> float:
    return pcc_strtod(text, endptr)


# --- PyType core (Ready/GenericAlloc/GenericNew/FromSpec/GetSlot/GetFlags/
# Modified) ----------------------------------------------------------
# Owns the heap-type creation and allocation surface.  PyTypeObject slot
# offsets are pinned from the fake-libc _typeobject mirror (tp_basicsize@40,
# tp_itemsize@48, tp_flags@176, tp_base@264, tp_alloc@312, tp_new@320,
# tp_name@32); PyType_Spec has name@0, basicsize@8, itemsize@12, flags@16,
# slots@24; PyType_Slot has slot@0, pfunc@8.

pcc_gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_ptr)
calloc_c = extern("calloc", (c_int64, c_int64), c_ptr)
free_c = extern("free", (c_ptr,), c_void)
PyErr_NoMemory = extern("PyErr_NoMemory", (), c_ptr)


@c_abi_typed_export("PyType_GenericAlloc", "ptr", ("ptr", "i64"))
def PyType_GenericAlloc(type_obj, nitems: int) -> c_ptr:
    if ptr_is_null(type_obj):
        return null()
    tag: int = _cext_tag_for_type(type_obj)
    minsz: int = 16 + 8  # PyObjectHeader + ob_type slot
    basic: int = load_i64(type_obj, 40)  # tp_basicsize
    if basic < minsz:
        basic = minsz
    itemsize: int = load_i64(type_obj, 48)  # tp_itemsize
    size: int = basic + nitems * itemsize
    obj = pcc_gc_alloc(size, tag, 0)
    if not ptr_is_null(obj):
        store_ptr(obj, 16, type_obj)  # ob_type slot
    return obj


@c_abi_typed_export("PyType_GenericNew", "ptr", ("ptr", "ptr", "ptr"))
def PyType_GenericNew(type_obj, args, kwds) -> c_ptr:
    return PyType_GenericAlloc(type_obj, 0)


@c_abi_typed_export("PyType_Ready", "i32", ("ptr",))
def PyType_Ready(type_obj) -> int:
    if ptr_is_null(type_obj):
        return -1
    flags: int = load_i64(type_obj, 176)  # tp_flags
    if (flags & (0x1000)) != 0:  # Py_TPFLAGS_READY
        return 0
    tp_alloc: c_ptr = load_ptr(type_obj, 312)
    if ptr_is_null(tp_alloc):
        base = load_ptr(type_obj, 264)  # tp_base
        if not ptr_is_null(base):
            base_alloc = load_ptr(base, 312)
            if not ptr_is_null(base_alloc):
                store_ptr(type_obj, 312, base_alloc)
            else:
                store_ptr(type_obj, 312, function_addr("PyType_GenericAlloc"))
        else:
            store_ptr(type_obj, 312, function_addr("PyType_GenericAlloc"))
    _cext_tag_for_type(type_obj)
    store_i64(type_obj, 176, flags | (0x1000))  # Py_TPFLAGS_READY
    return 0


@c_abi_typed_export("PyType_Modified", "void", ("ptr",))
def PyType_Modified(type_obj) -> None:
    return


@c_abi_typed_export("PyType_GetFlags", "i64", ("ptr",))
def PyType_GetFlags(type_obj) -> int:
    if ptr_is_null(type_obj):
        return 0
    return load_i64(type_obj, 176)  # tp_flags


@c_abi_typed_export("PyType_GetSlot", "ptr", ("ptr", "i32"))
def PyType_GetSlot(type_obj, slot: int) -> c_ptr:
    if ptr_is_null(type_obj):
        return null()
    if slot == 48:  # Py_tp_base
        return load_ptr(type_obj, 264)
    if slot == 50:  # Py_tp_call
        return load_ptr(type_obj, 136)
    if slot == 52:  # Py_tp_dealloc
        return load_ptr(type_obj, 56)
    if slot == 56:  # Py_tp_doc
        return load_ptr(type_obj, 184)
    if slot == 59:  # Py_tp_hash
        return load_ptr(type_obj, 128)
    if slot == 60:  # Py_tp_init
        return load_ptr(type_obj, 304)
    if slot == 62:  # Py_tp_iter
        return load_ptr(type_obj, 224)
    if slot == 63:  # Py_tp_iternext
        return load_ptr(type_obj, 232)
    if slot == 64:  # Py_tp_methods
        return load_ptr(type_obj, 240)
    if slot == 65:  # Py_tp_new
        return load_ptr(type_obj, 320)
    if slot == 66:  # Py_tp_repr
        return load_ptr(type_obj, 96)
    if slot == 67:  # Py_tp_richcompare
        return load_ptr(type_obj, 208)
    if slot == 70:  # Py_tp_str
        return load_ptr(type_obj, 144)
    if slot == 72:  # Py_tp_members
        return load_ptr(type_obj, 248)
    if slot == 73:  # Py_tp_getset
        return load_ptr(type_obj, 256)
    return null()


@c_abi_typed_export("PyType_FromSpec", "ptr", ("ptr",))
def PyType_FromSpec(spec) -> c_ptr:
    if ptr_is_null(spec) or ptr_is_null(load_ptr(spec, 0)):
        _type_error(cstr("invalid PyType_Spec"))
        return null()
    type_obj = calloc_c(1, 424)  # sizeof(PyTypeObject)
    if ptr_is_null(type_obj):
        PyErr_NoMemory()
        return null()
    store_i64(type_obj, 0, 1)  # refcount
    store_i32(type_obj, 8, PY_TYPE_NONE)
    store_ptr(type_obj, 16, global_addr("PyType_Type"))  # ob_type
    store_ptr(type_obj, 32, load_ptr(spec, 0))  # tp_name
    store_i64(type_obj, 40, load_i32(spec, 8))  # tp_basicsize
    store_i64(type_obj, 48, load_i32(spec, 12))  # tp_itemsize
    store_i64(type_obj, 176, load_i32(spec, 16))  # tp_flags
    slots = load_ptr(spec, 24)  # slots
    while not ptr_is_null(slots) and load_i32(slots, 0) != 0:
        slot_id: int = load_i32(slots, 0)
        pfunc = load_ptr(slots, 8)
        if slot_id == 48:
            store_ptr(type_obj, 264, pfunc)  # tp_base
        elif slot_id == 50:
            store_ptr(type_obj, 136, pfunc)  # tp_call
        elif slot_id == 51:
            store_ptr(type_obj, 200, pfunc)  # tp_clear
        elif slot_id == 52:
            store_ptr(type_obj, 56, pfunc)  # tp_dealloc
        elif slot_id == 56:
            store_ptr(type_obj, 184, pfunc)  # tp_doc
        elif slot_id == 59:
            store_ptr(type_obj, 128, pfunc)  # tp_hash
        elif slot_id == 60:
            store_ptr(type_obj, 304, pfunc)  # tp_init
        elif slot_id == 62:
            store_ptr(type_obj, 224, pfunc)  # tp_iter
        elif slot_id == 63:
            store_ptr(type_obj, 232, pfunc)  # tp_iternext
        elif slot_id == 64:
            store_ptr(type_obj, 240, pfunc)  # tp_methods
        elif slot_id == 65:
            store_ptr(type_obj, 320, pfunc)  # tp_new
        elif slot_id == 66:
            store_ptr(type_obj, 96, pfunc)  # tp_repr
        elif slot_id == 67:
            store_ptr(type_obj, 208, pfunc)  # tp_richcompare
        elif slot_id == 70:
            store_ptr(type_obj, 144, pfunc)  # tp_str
        elif slot_id == 71:
            store_ptr(type_obj, 192, pfunc)  # tp_traverse
        elif slot_id == 72:
            store_ptr(type_obj, 248, pfunc)  # tp_members
        elif slot_id == 73:
            store_ptr(type_obj, 256, pfunc)  # tp_getset
        slots = ptr_add(slots, 16)
    if ptr_is_null(load_ptr(type_obj, 320)):  # tp_new
        store_ptr(type_obj, 320, function_addr("PyType_GenericNew"))
    if ptr_is_null(load_ptr(type_obj, 312)):  # tp_alloc
        store_ptr(type_obj, 312, function_addr("PyType_GenericAlloc"))
    if PyType_Ready(type_obj) < 0:
        free_c(type_obj)
        return null()
    return type_obj
