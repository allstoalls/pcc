"""pcc-Python owners for the module-state registry and heap-type surface.

Replaces the PccCapiModuleStateNode registry + PyModule_Create2/GetState +
PyType_FromSpec/FromModuleAndSpec/GetModule/GetModuleByDef block of
py_capi_shim.c.  The registry is a singly-linked list of 24-byte nodes
(module@0, def@8, state@16, next@24) rooted at a global pointer.

PyModuleDef layout (pcc fake_libc Python.h shape, NOT CPython's):
PyModuleDef_Base is 32 bytes {ob_base@0, m_init@8, m_index@16, m_copy@24},
then m_name@32, m_doc@40, m_size@48, m_methods@56, m_slots@64,
m_traverse@72, m_clear@80, m_free@88.
PyType_Spec: name@0, basicsize@8, itemsize@12, flags@16, slots@24.
PyType_Slot: slot@0, pfunc@8.

Py_tp_* slot constants (fake-libc Python.h values):
  Py_tp_base=0, Py_tp_dealloc=1, Py_tp_repr=2, Py_tp_hash=4, Py_tp_call=5,
  Py_tp_str=6, Py_tp_getattro=7, Py_tp_setattro=8, Py_tp_iter=17,
  Py_tp_iternext=18, Py_tp_descr_get=19, Py_tp_init=26, Py_tp_new=27,
  Py_tp_doc=32, Py_tp_clear=16, Py_tp_richcompare=14, Py_tp_traverse=15,
  Py_tp_members=33, Py_tp_getset=34

Owned surface (stable C ABI names):

  pcc_capi_register_module_state, pcc_capi_find_module_state,
  PyModule_Create2, PyModule_GetState, PyType_FromSpec,
  PyType_FromModuleAndSpec, PyType_GetModule, PyType_GetModuleByDef
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import (
    PY_TYPE_CEXT_TAG_BASE,
)
from pcc.extern import c_abi_typed_export, c_int32, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    call_i64_ptr1,
    call_i64_ptr3,
    call_void_ptr2,
    cstr,
    define_global_ptr_null,
    function_addr,
    global_addr,
    global_load_ptr,
    is_tagged_int,
    load_i32,
    load_i64,
    load_i8,
    load_ptr,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    stack_alloc,
    store_i32,
    store_i64,
    store_ptr,
    strlen,
)

py_instance_new = extern("py_instance_new", (c_ptr,), c_ptr)
py_instance_setattr = extern("py_instance_setattr", (c_ptr, c_ptr, c_ptr), c_void)
py_str_new = extern("py_str_new", (c_ptr, c_int64), c_ptr)
py_incref = extern("py_incref", (c_ptr,), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_err_occurred = extern("py_err_occurred", (), c_int64)
pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
pcc_gc_object_is_known_no_lock = extern("pcc_gc_object_is_known_no_lock", (c_ptr,), c_int64)
pcc_runtime_module_class = extern("pcc_runtime_module_class", (), c_ptr)
PyMem_Calloc = extern("PyMem_Calloc", (c_int64, c_int64), c_ptr)
PyMem_Free = extern("PyMem_Free", (c_ptr,), c_void)
PyErr_NoMemory = extern("PyErr_NoMemory", (), c_ptr)
PyType_FromSpec = extern("PyType_FromSpec", (c_ptr,), c_ptr)
PyType_Ready = extern("PyType_Ready", (c_ptr,), c_int32)
PyType_GenericNew = extern("PyType_GenericNew", (c_ptr, c_ptr), c_ptr)
PyType_GenericAlloc = extern("PyType_GenericAlloc", (c_ptr, c_int64), c_ptr)
pcc_capi_cext_tag_for = extern("pcc_capi_cext_tag_for", (c_ptr,), c_int32)
pcc_capi_method_func_new = extern("pcc_capi_method_func_new", (c_ptr, c_ptr), c_ptr)

define_global_ptr_null("pcc_capi_module_states")

# ABI slot offsets and tag values stay literal at their use sites. Library
# modules do not execute module initialization, so top-level numeric constants
# would otherwise become zero-initialized ``.modvar.`` globals.


def _type_error(message) -> None:
    py_raise_owned(py_exc_new(3, message))  # PY_EXC_TYPEERROR


def _runtime_error(message) -> None:
    py_raise_owned(py_exc_new(7, message))  # PY_EXC_RUNTIMEERROR (6 is AttributeError)


def _system_error(message) -> None:
    py_raise_owned(py_exc_new(7, message))  # PY_EXC_SYSTEMERROR


@c_abi_typed_export("pcc_capi_find_module_state", "ptr", ("ptr",))
def pcc_capi_find_module_state(module) -> c_ptr:
    head = global_addr("pcc_capi_module_states")
    node = load_ptr(head, 0)
    while not ptr_is_null(node):
        if ptr_eq(load_ptr(node, 0), module):
            return node
        node = load_ptr(node, 24)
    return null()


@c_abi_typed_export("pcc_capi_register_module_state", "i32", ("ptr", "ptr", "ptr"))
def pcc_capi_register_module_state(module, def_obj, state) -> int:
    node = PyMem_Calloc(1, 24)
    if ptr_is_null(node):
        return -1
    store_ptr(node, 0, module)
    store_ptr(node, 8, def_obj)
    store_ptr(node, 16, state)
    head = global_addr("pcc_capi_module_states")
    store_ptr(node, 24, load_ptr(head, 0))
    store_ptr(head, 0, node)
    if not ptr_is_null(module) and not is_tagged_int(module):
        if pcc_gc_object_is_known_no_lock(module) != 0:
            pcc_gc_pin(module)
    return 0


@c_abi_typed_export("PyModule_GetState", "ptr", ("ptr",))
def PyModule_GetState(module) -> c_ptr:
    node = pcc_capi_find_module_state(module)
    if ptr_is_null(node):
        return null()
    return load_ptr(node, 16)


@c_abi_typed_export("PyModule_Create2", "ptr", ("ptr", "i32"))
def PyModule_Create2(def_obj, api_version: int) -> c_ptr:
    # PyModuleDef layout is pcc's fake_libc Python.h shape, NOT CPython's:
    # m_base = {ob_base@0, m_init@8, m_index@16, m_copy@24} (32 bytes), then
    # m_name@32, m_doc@40, m_size@48, m_methods@56, m_slots@64,
    # m_traverse@72, m_clear@80, m_free@88. Reading CPython offsets here
    # rejected numpy's _multiarray_umath def as "invalid module definition"
    # (offset 8 is m_init, NULL in static defs).
    if ptr_is_null(def_obj) or ptr_is_null(load_ptr(def_obj, 32)):  # m_name
        _runtime_error(cstr("invalid module definition"))
        return null()
    cls = pcc_runtime_module_class()
    if ptr_is_null(cls):
        return null()
    module = py_instance_new(cls)
    if ptr_is_null(module):
        return null()
    m_size: int = load_i64(def_obj, 48)
    if m_size > 0:
        state = PyMem_Calloc(1, m_size)
        if ptr_is_null(state):
            py_decref(module)
            PyErr_NoMemory()
            return null()
        if pcc_capi_register_module_state(module, def_obj, state) != 0:
            PyMem_Free(state)
            py_decref(module)
            PyErr_NoMemory()
            return null()
    m_name = load_ptr(def_obj, 32)
    name = py_str_new(m_name, strlen(m_name))
    if not ptr_is_null(name):
        py_instance_setattr(module, cstr("__name__"), name)
        py_decref(name)
    methods = load_ptr(def_obj, 56)
    method = methods
    while not ptr_is_null(method) and not ptr_is_null(load_ptr(method, 0)):
        fn = pcc_capi_method_func_new(module, method)
        if not ptr_is_null(fn):
            py_instance_setattr(module, load_ptr(method, 0), fn)
            py_decref(fn)
        method = ptr_add(method, 32)  # sizeof(PyMethodDef)
    return module


@c_abi_typed_export("pcc_capi_generic_new_proxy", "ptr", ("ptr", "ptr", "ptr"))
def pcc_capi_generic_new_proxy(type_obj, args, kwds) -> c_ptr:
    return PyType_GenericAlloc(type_obj, 0)


@c_abi_typed_export("PyType_FromModuleAndSpec", "ptr", ("ptr", "ptr", "ptr"))
def PyType_FromModuleAndSpec(module, spec, bases) -> c_ptr:
    if ptr_is_null(module):
        _type_error(cstr("NULL module for heap type"))
        return null()
    type_obj = PyType_FromSpec(spec)
    if ptr_is_null(type_obj):
        return null()
    tag: int = load_i32(type_obj, 8)  # type_tag set by PyType_Ready via cext tag
    offset = tag - (PY_TYPE_CEXT_TAG_BASE)
    if offset < 0 or offset >= (1024):
        _runtime_error(cstr("heap type registry exhausted"))
        return null()
    py_incref(module)
    if not is_tagged_int(module):
        if pcc_gc_object_is_known_no_lock(module) != 0:
            pcc_gc_pin(module)
    table = global_addr("pcc_capi_cext_type_modules")
    store_ptr(ptr_add(table, offset * 8), 0, module)
    return type_obj


@c_abi_typed_export("PyType_GetModule", "ptr", ("ptr",))
def PyType_GetModule(type_obj) -> c_ptr:
    if ptr_is_null(type_obj):
        return null()
    version_tag: int = load_i32(type_obj, (392))
    if version_tag < (PY_TYPE_CEXT_TAG_BASE):
        _type_error(cstr("type has no associated module"))
        return null()
    offset = version_tag - (PY_TYPE_CEXT_TAG_BASE)
    if offset < 0 or offset >= (1024):
        _type_error(cstr("type has no associated module"))
        return null()
    count: int = load_i32(global_addr("pcc_capi_cext_type_count"), 0)
    if offset >= count:
        _type_error(cstr("type has no associated module"))
        return null()
    table = global_addr("pcc_capi_cext_type_modules")
    module = load_ptr(ptr_add(table, offset * 8), 0)
    if ptr_is_null(module):
        _type_error(cstr("type has no associated module"))
        return null()
    return module


@c_abi_typed_export("PyType_GetModuleByDef", "ptr", ("ptr", "ptr"))
def PyType_GetModuleByDef(type_obj, def_obj) -> c_ptr:
    if ptr_is_null(type_obj) or ptr_is_null(def_obj):
        _type_error(cstr("invalid type or module definition"))
        return null()
    guard: int = 0
    while not ptr_is_null(type_obj) and guard < 64:
        module = null()
        version_tag: int = load_i32(type_obj, (392))
        if version_tag >= (PY_TYPE_CEXT_TAG_BASE):
            offset = version_tag - (PY_TYPE_CEXT_TAG_BASE)
            count: int = load_i32(global_addr("pcc_capi_cext_type_count"), 0)
            if offset >= 0 and offset < count:
                table = global_addr("pcc_capi_cext_type_modules")
                module = load_ptr(ptr_add(table, offset * 8), 0)
        node = pcc_capi_find_module_state(module)
        if not ptr_is_null(node) and ptr_eq(load_ptr(node, 8), def_obj):
            return module
        type_obj = load_ptr(type_obj, (264))
        guard += 1
    _type_error(cstr("module definition not found in type MRO"))
    return null()


# --- module loader exec slots -----------------------------------------


@c_abi_typed_export("pcc_capi_module_from_def", "ptr", ("ptr",))
def pcc_capi_module_from_def(def_as_obj) -> c_ptr:
    if ptr_is_null(def_as_obj):
        return null()
    return PyModule_Create2(def_as_obj, 0)


@c_abi_typed_export("pcc_capi_module_run_exec_slots", "i32", ("ptr", "ptr"))
def pcc_capi_module_run_exec_slots(def_as_obj, module) -> int:
    if ptr_is_null(def_as_obj) or ptr_is_null(module):
        return -1
    slots = load_ptr(def_as_obj, 64)  # m_slots (m_doc is @40; see layout above)
    slot = slots
    while not ptr_is_null(slot) and load_i32(slot, 0) != 0:
        slot_id: int = load_i32(slot, 0)
        value = load_ptr(slot, 8)
        if slot_id == (2) and not ptr_is_null(value):  # Py_mod_exec
            result = call_i64_ptr1(value, module)
            if result != 0:
                if py_err_occurred() == 0:
                    _system_error(cstr("module exec slot failed"))
                return -1
        slot = ptr_add(slot, 16)  # sizeof(PyModuleDef_Slot)
    return 0


@c_abi_typed_export("pcc_capi_module_exec", "ptr", ("ptr",))
def pcc_capi_module_exec(def_as_obj) -> c_ptr:
    module = pcc_capi_module_from_def(def_as_obj)
    if ptr_is_null(module):
        return null()
    if pcc_capi_module_run_exec_slots(def_as_obj, module) != 0:
        py_decref(module)
        return null()
    return module


@c_abi_typed_export("pcc_capi_visit_module_state_ref", "i32", ("ptr", "ptr"))
def pcc_capi_visit_module_state_ref(obj, arg) -> int:
    if ptr_is_null(obj):
        return 0
    if not ptr_is_null(arg):
        visit = load_ptr(arg, 0)
        ctx = load_ptr(arg, 8)
        if not ptr_is_null(visit):
            call_void_ptr2(visit, obj, ctx)
    return 0


# --- pcc_capi_visit_extension_module_state_roots ---------------------

pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)


@c_abi_typed_export("pcc_capi_visit_extension_module_state_roots", "void", ("ptr", "ptr"))
def pcc_capi_visit_extension_module_state_roots(visit, ctx) -> None:
    if ptr_is_null(visit):
        return
    head = global_addr("pcc_capi_module_states")
    node = load_ptr(head, 0)
    while not ptr_is_null(node):
        module = load_ptr(node, 0)
        if not ptr_is_null(module):
            call_void_ptr2(visit, module, ctx)
        def_obj = load_ptr(node, 8)
        if not ptr_is_null(def_obj):
            m_traverse = load_ptr(def_obj, 72)  # PyModuleDef.m_traverse
            if not ptr_is_null(m_traverse):
                # traverse(module, pcc_capi_visit_module_state_ref, &visit_ctx)
                visit_ctx = stack_alloc(16)
                store_ptr(visit_ctx, 0, visit)
                store_ptr(visit_ctx, 8, ctx)
                call_i64_ptr3(
                    m_traverse,
                    module,
                    function_addr("pcc_capi_visit_module_state_ref"),
                    visit_ctx,
                )
        node = load_ptr(node, 24)
