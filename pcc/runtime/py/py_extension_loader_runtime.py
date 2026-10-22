"""pcc-native extension loading authored in pcc-Python.

The loaded artifact still implements pcc's generated C-API shim ABI.  This
module owns discovery, load-once registration, parent ordering, PEP 489 exec
ordering, and failure cleanup without a hand-written C loader object.
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import C_POINTER_SIZE

from pcc.extern import c_abi_export, c_int32, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    access,
    call_ptr0,
    calloc,
    cstr,
    define_global_ptr_null,
    define_global_i32,
    global_addr,
    memset,
    dynamic_library_close,
    dynamic_library_open_global,
    dynamic_library_symbol,
    free,
    global_load_ptr,
    global_store_ptr,
    load_i8,
    load_ptr,
    malloc,
    memcpy,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    stack_alloc,
    store_i8,
    store_ptr,
    strlen,
)


py_compiled_module_ensure_parent_packages = extern(
    "py_compiled_module_ensure_parent_packages", (c_ptr,), c_int32
)
py_sys_modules_import_cached = extern("py_sys_modules_import_cached", (c_ptr,), c_ptr)
py_sys_modules_publish = extern("py_sys_modules_publish", (c_ptr, c_ptr), c_int64)
py_sys_modules_finish_import = extern("py_sys_modules_finish_import", (c_ptr, c_ptr), c_ptr)
py_sys_modules_abort_import = extern("py_sys_modules_abort_import", (c_ptr, c_ptr), c_void)
py_sys_modules_rollback = extern("py_sys_modules_rollback", (c_ptr,), c_void)
py_sys_modules_owner_adopt = extern("py_sys_modules_owner_adopt", (c_ptr, c_ptr, c_int64), c_void)
py_sys_modules_owner_finish = extern("py_sys_modules_owner_finish", (c_ptr, c_ptr), c_ptr)
pcc_gc_frame_enter = extern("pcc_gc_frame_enter", (c_ptr, c_ptr), c_void)
pcc_capi_module_from_def = extern("pcc_capi_module_from_def", (c_ptr,), c_ptr)
pcc_capi_module_run_exec_slots = extern(
    "pcc_capi_module_run_exec_slots", (c_ptr, c_ptr), c_int32
)
pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
pcc_gc_unpin = extern("pcc_gc_unpin", (c_ptr,), c_void)
py_incref = extern("py_incref", (c_ptr,), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
pcc_platform_getenv = extern("pcc_platform_getenv", (c_ptr,), c_ptr)
pcc_platform_write = extern("pcc_platform_write", (c_int64, c_ptr, c_int64), c_int64)
dlerror = extern("dlerror", (), c_ptr)


# Shared with py_sys_modules_owner_*: each published pointer owns one ref.
_MI_TEMP = 0
_MI_CREATOR = 1
_MI_AUX = 2
_MI_ERROR = 3
_MI_OUTPUT = 4
_MI_CACHE = 5
_MI_COUNT = 6
define_global_i32("pcc_extension_module_frame_map", _MI_COUNT)
define_global_ptr_null("pcc_extension_modules")
define_global_ptr_null("pcc_extension_code_owners")


def _cstr_equal(left, right) -> int:
    if ptr_is_null(left) != 0 or ptr_is_null(right) != 0:
        if ptr_is_null(left) != 0 and ptr_is_null(right) != 0:
            return 1
        return 0
    index: int = 0
    while True:
        a: int = load_i8(left, index) & 255
        b: int = load_i8(right, index) & 255
        if a != b:
            return 0
        if a == 0:
            return 1
        index = index + 1
    return 0


def _duplicate_cstr(value):
    if ptr_is_null(value) != 0:
        value = cstr("")
    size: int = strlen(value) + 1
    copy = malloc(size)
    if ptr_is_null(copy) != 0:
        return null()
    memcpy(copy, value, size)
    return copy


def _debug_enabled() -> int:
    value = pcc_platform_getenv(cstr("PCC_DEBUG_EXT_IMPORT"))
    if ptr_is_null(value) != 0 or load_i8(value, 0) == 0:
        return 0
    return 1


def _write_stderr(value) -> None:
    if ptr_is_null(value) == 0:
        pcc_platform_write(2, value, strlen(value))


def _debug_event(event, module_name, path) -> None:
    if _debug_enabled() == 0:
        return
    _write_stderr(cstr("[ext-import] "))
    _write_stderr(event)
    _write_stderr(cstr(" name="))
    _write_stderr(module_name)
    if ptr_is_null(path) == 0:
        _write_stderr(cstr(" path="))
        _write_stderr(path)
    _write_stderr(cstr("\n"))


def _find_module(module_name):
    node = global_load_ptr("pcc_extension_modules")
    while ptr_is_null(node) == 0:
        if _cstr_equal(load_ptr(node, 0), module_name) != 0:
            return node
        node = load_ptr(node, 32)
    return null()


def _find_code_owner(module_name):
    owner = global_load_ptr("pcc_extension_code_owners")
    while not ptr_is_null(owner):
        if _cstr_equal(load_ptr(owner, 0), module_name) != 0:
            return owner
        owner = load_ptr(owner, 16)
    return null()


def _retain_extension_code(module_name, handle) -> int:
    # PyInit may publish methods, exception classes, or partial modules before
    # failing. Their machine code/data outlives cache removal. Retain the DSO
    # for process lifetime, independently of any strong module reference.
    owner = malloc(24)
    if ptr_is_null(owner):
        return -1
    name = _duplicate_cstr(module_name)
    if ptr_is_null(name):
        free(owner)
        return -1
    store_ptr(owner, 0, name)
    store_ptr(owner, 8, handle)
    store_ptr(owner, 16, global_load_ptr("pcc_extension_code_owners"))
    global_store_ptr("pcc_extension_code_owners", owner)
    return 0


def _leaf(module_name):
    leaf = module_name
    cursor = module_name
    while load_i8(cursor, 0) != 0:
        if load_i8(cursor, 0) == 46:
            leaf = ptr_add(cursor, 1)
        cursor = ptr_add(cursor, 1)
    return leaf


def _init_symbol(module_name):
    leaf = _leaf(module_name)
    leaf_len: int = strlen(leaf)
    symbol = malloc(leaf_len + 8)
    if ptr_is_null(symbol) != 0:
        return null()
    memcpy(symbol, cstr("PyInit_"), 7)
    memcpy(ptr_add(symbol, 7), leaf, leaf_len + 1)
    return symbol


def _register(module_name, path, handle):
    node = calloc(1, 40)
    if ptr_is_null(node) != 0:
        return null()
    name_copy = _duplicate_cstr(module_name)
    path_copy = _duplicate_cstr(path)
    if ptr_is_null(name_copy) != 0 or ptr_is_null(path_copy) != 0:
        if ptr_is_null(name_copy) == 0:
            free(name_copy)
        if ptr_is_null(path_copy) == 0:
            free(path_copy)
        free(node)
        return null()
    store_ptr(node, 0, name_copy)
    store_ptr(node, 8, path_copy)
    store_ptr(node, 16, handle)
    store_ptr(node, 24, null())
    store_ptr(node, 32, global_load_ptr("pcc_extension_modules"))
    global_store_ptr("pcc_extension_modules", node)
    _debug_event(cstr("registered"), module_name, null())
    return node


def _unregister(node) -> None:
    previous = null()
    current = global_load_ptr("pcc_extension_modules")
    while ptr_is_null(current) == 0 and ptr_eq(current, node) == 0:
        previous = current
        current = load_ptr(current, 32)
    if ptr_eq(current, node) != 0:
        next_node = load_ptr(node, 32)
        if ptr_is_null(previous) != 0:
            global_store_ptr("pcc_extension_modules", next_node)
        else:
            store_ptr(previous, 32, next_node)
    py_sys_modules_rollback(load_ptr(node, 0))
    free(load_ptr(node, 0))
    free(load_ptr(node, 8))
    free(node)


def _runtime_error(prefix, detail):
    if ptr_is_null(prefix) != 0:
        prefix = cstr("native extension import failed")
    if ptr_is_null(detail) != 0:
        detail = cstr("")
    prefix_len: int = strlen(prefix)
    detail_len: int = strlen(detail)
    if prefix_len > 350:
        prefix_len = 350
    if detail_len > 350:
        detail_len = 350
    message = stack_alloc(704)
    memcpy(message, prefix, prefix_len)
    store_i8(message, prefix_len, 58)
    store_i8(message, prefix_len + 1, 32)
    memcpy(ptr_add(message, prefix_len + 2), detail, detail_len)
    store_i8(message, prefix_len + 2 + detail_len, 0)
    py_raise_owned(py_exc_new(7, message))
    return null()


def _native_extension_import_into(module_name, path, slots, tokens) -> None:
    if (
        ptr_is_null(module_name) != 0
        or ptr_is_null(path) != 0
        or load_i8(module_name, 0) == 0
        or load_i8(path, 0) == 0
    ):
        _runtime_error(
            cstr("native extension import failed"),
            cstr("missing module name or path"),
        )
        return

    store_ptr(slots, _MI_OUTPUT * C_POINTER_SIZE, py_sys_modules_import_cached(module_name))
    py_sys_modules_owner_adopt(slots, tokens, _MI_OUTPUT)
    if not ptr_is_null(load_ptr(slots, _MI_OUTPUT * C_POINTER_SIZE)) or py_err_occurred() != 0:
        return
    if not ptr_is_null(_find_code_owner(module_name)):
        _runtime_error(
            cstr("native extension reinitialization unavailable after sys.modules deletion"), module_name
        )
        return

    if py_compiled_module_ensure_parent_packages(module_name) != 0:
        return
    store_ptr(slots, _MI_OUTPUT * C_POINTER_SIZE, py_sys_modules_import_cached(module_name))
    py_sys_modules_owner_adopt(slots, tokens, _MI_OUTPUT)
    if not ptr_is_null(load_ptr(slots, _MI_OUTPUT * C_POINTER_SIZE)) or py_err_occurred() != 0:
        return
    if not ptr_is_null(_find_code_owner(module_name)):
        _runtime_error(cstr("native extension reinitialization unavailable after prior initialization attempt"), module_name)
        return

    handle = dynamic_library_open_global(path)
    if ptr_is_null(handle) != 0:
        _runtime_error(cstr("dlopen failed"), dlerror())
        return

    symbol = _init_symbol(module_name)
    if ptr_is_null(symbol) != 0:
        dynamic_library_close(handle)
        _runtime_error(
            cstr("native extension import failed"), cstr("out of memory")
        )
        return
    dlerror()
    init = dynamic_library_symbol(handle, symbol)
    symbol_error = dlerror()
    if ptr_is_null(symbol_error) != 0 and ptr_is_null(init) == 0:
        free(symbol)
    else:
        if ptr_is_null(symbol_error) != 0:
            symbol_error = symbol
        # Materialize the diagnostic while both the fallback symbol and
        # loader-owned error text still have valid storage.
        _runtime_error(cstr("dlsym failed"), symbol_error)
        free(symbol)
        dynamic_library_close(handle)
        return

    if _retain_extension_code(module_name, handle) != 0:
        dynamic_library_close(handle)
        _runtime_error(cstr("native extension import failed"), cstr("code ownership allocation failed"))
        return
    module = call_ptr0(init)
    if ptr_is_null(module) != 0:
        if py_err_occurred() == 0:
            _runtime_error(cstr("native extension init failed"), module_name)
            return
        return

    # The PyInit result is a union of managed module and raw PyModuleDef.
    # Classify by the ABI marker with inline loads before any polling call;
    # raw definitions must never enter a managed root or lease.
    definition = null()
    if ptr_eq(load_ptr(module, 0), global_addr("pcc_capi_moduledef_marker")):
        definition = module
        store_ptr(slots, _MI_CREATOR * C_POINTER_SIZE, pcc_capi_module_from_def(definition))
    else:
        store_ptr(slots, _MI_CREATOR * C_POINTER_SIZE, module)
    py_sys_modules_owner_adopt(slots, tokens, _MI_CREATOR)
    if ptr_is_null(load_ptr(slots, _MI_CREATOR * C_POINTER_SIZE)) or py_err_occurred():
        if py_err_occurred() == 0:
            _runtime_error(cstr("native extension exec failed"), module_name)
            return
        return
    node = _register(module_name, path, handle)
    if ptr_is_null(node):
        _runtime_error(cstr("native extension import failed"), cstr("out of memory"))
        return
    if py_sys_modules_publish(module_name, load_ptr(slots, _MI_CREATOR * C_POINTER_SIZE)) != 0:
        _unregister(node)
        return
    if not ptr_is_null(definition):
        _debug_event(cstr("exec-begin"), module_name, null())
        if pcc_capi_module_run_exec_slots(definition, load_ptr(slots, _MI_CREATOR * C_POINTER_SIZE)) != 0:
            _unregister(node)
            if py_err_occurred() == 0:
                _runtime_error(cstr("native extension exec failed"), module_name)
                return
            return
    # Completion takes a separate reference; the active creator remains
    # leased even when a callback deletes/replaces its public cache entry.
    py_incref(load_ptr(slots, _MI_CREATOR * C_POINTER_SIZE))
    store_ptr(slots, _MI_OUTPUT * C_POINTER_SIZE, py_sys_modules_finish_import(module_name, load_ptr(slots, _MI_CREATOR * C_POINTER_SIZE)))
    py_sys_modules_owner_adopt(slots, tokens, _MI_OUTPUT)


@c_abi_export("py_native_extension_import")
def py_native_extension_import(module_name, path):
    slots = stack_alloc(_MI_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_MI_COUNT * C_POINTER_SIZE)
    memset(slots, _MI_TEMP * C_POINTER_SIZE, 48)
    memset(tokens, 255, _MI_COUNT * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_extension_module_frame_map"), slots)
    _native_extension_import_into(module_name, path, slots, tokens)
    return py_sys_modules_owner_finish(slots, tokens)


def _module_relpath(module_name):
    if ptr_is_null(module_name) != 0 or load_i8(module_name, 0) == 0:
        return null()
    size: int = strlen(module_name)
    relative = malloc(size + 1)
    if ptr_is_null(relative) != 0:
        return null()
    index: int = 0
    while index < size:
        value: int = load_i8(module_name, index)
        if value == 46:
            value = 47
        store_i8(relative, index, value)
        index = index + 1
    store_i8(relative, size, 0)
    return relative


def _candidate_path(site, site_len: int, relative, extension):
    if (
        ptr_is_null(site) != 0
        or site_len <= 0
        or ptr_is_null(relative) != 0
        or ptr_is_null(extension) != 0
    ):
        return null()
    relative_len: int = strlen(relative)
    extension_len: int = strlen(extension)
    needs_slash: int = 1
    if load_i8(site, site_len - 1) == 47:
        needs_slash = 0
    total: int = site_len + needs_slash + relative_len + extension_len
    path = malloc(total + 1)
    if ptr_is_null(path) != 0:
        return null()
    memcpy(path, site, site_len)
    position: int = site_len
    if needs_slash != 0:
        store_i8(path, position, 47)
        position = position + 1
    memcpy(ptr_add(path, position), relative, relative_len)
    position = position + relative_len
    memcpy(ptr_add(path, position), extension, extension_len)
    position = position + extension_len
    store_i8(path, position, 0)
    return path


def _try_candidate(module_name, site, site_len: int, relative, extension, slots, tokens) -> None:
    path = _candidate_path(site, site_len, relative, extension)
    if ptr_is_null(path):
        return
    if access(path, 0) == 0:
        store_ptr(slots, _MI_OUTPUT * C_POINTER_SIZE, py_native_extension_import(module_name, path))
        py_sys_modules_owner_adopt(slots, tokens, _MI_OUTPUT)
    free(path)


def _native_extension_import_by_name_into(module_name, slots, tokens) -> None:
    if ptr_is_null(module_name) or load_i8(module_name, 0) == 0:
        return
    store_ptr(slots, _MI_OUTPUT * C_POINTER_SIZE, py_sys_modules_import_cached(module_name))
    py_sys_modules_owner_adopt(slots, tokens, _MI_OUTPUT)
    if not ptr_is_null(load_ptr(slots, _MI_OUTPUT * C_POINTER_SIZE)) or py_err_occurred():
        return
    if not ptr_is_null(_find_code_owner(module_name)):
        _runtime_error(cstr("native extension reinitialization unavailable after sys.modules deletion"), module_name)
        return
    if py_compiled_module_ensure_parent_packages(module_name) != 0:
        return
    store_ptr(slots, _MI_OUTPUT * C_POINTER_SIZE, py_sys_modules_import_cached(module_name))
    py_sys_modules_owner_adopt(slots, tokens, _MI_OUTPUT)
    if not ptr_is_null(load_ptr(slots, _MI_OUTPUT * C_POINTER_SIZE)) or py_err_occurred():
        return
    if not ptr_is_null(_find_code_owner(module_name)):
        _runtime_error(cstr("native extension reinitialization unavailable after prior initialization attempt"), module_name)
        return
    sites = pcc_platform_getenv(cstr("PCC_PACKAGE_SITE"))
    if ptr_is_null(sites) or load_i8(sites, 0) == 0:
        return
    relative = _module_relpath(module_name)
    if ptr_is_null(relative):
        return
    start = sites
    while load_i8(start, 0) != 0:
        end = start
        while load_i8(end, 0) != 0 and load_i8(end, 0) != 58:
            end = ptr_add(end, 1)
        site_len: int = 0
        cursor = start
        while not ptr_eq(cursor, end):
            site_len = site_len + 1
            cursor = ptr_add(cursor, 1)
        if site_len > 0:
            _try_candidate(module_name, start, site_len, relative, cstr(".so"), slots, tokens)
            if ptr_is_null(load_ptr(slots, _MI_OUTPUT * C_POINTER_SIZE)) and not py_err_occurred():
                _try_candidate(module_name, start, site_len, relative, cstr(".dylib"), slots, tokens)
            if ptr_is_null(load_ptr(slots, _MI_OUTPUT * C_POINTER_SIZE)) and not py_err_occurred():
                _try_candidate(module_name, start, site_len, relative, cstr(".pyd"), slots, tokens)
            if ptr_is_null(load_ptr(slots, _MI_OUTPUT * C_POINTER_SIZE)) and not py_err_occurred():
                _try_candidate(module_name, start, site_len, relative, cstr(".dll"), slots, tokens)
            if not ptr_is_null(load_ptr(slots, _MI_OUTPUT * C_POINTER_SIZE)) or py_err_occurred():
                free(relative)
                return
        if load_i8(end, 0) == 0:
            start = end
        else:
            start = ptr_add(end, 1)
    free(relative)


@c_abi_export("py_native_extension_import_by_name")
def py_native_extension_import_by_name(module_name):
    slots = stack_alloc(_MI_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_MI_COUNT * C_POINTER_SIZE)
    memset(slots, _MI_TEMP * C_POINTER_SIZE, 48)
    memset(tokens, 255, _MI_COUNT * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_extension_module_frame_map"), slots)
    _native_extension_import_by_name_into(module_name, slots, tokens)
    return py_sys_modules_owner_finish(slots, tokens)
