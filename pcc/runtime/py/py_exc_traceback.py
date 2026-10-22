"""pcc-Python port of py_exc_traceback.c.

Traceback frame growth, fail-closed runtime-contract errors, and cold
unhandled-exception formatting. Output matches the C runtime's stderr text,
but uses pcc.unsafe.write instead of variadic fprintf.
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import (
    C_POINTER_SIZE,
    PY_FLAG_IMMORTAL,
    PYCLASSOBJECT_NAME_OFFSET,
    PY_FLAG_EXC_SUPPRESS_CONTEXT,
    PY_TYPE_EXC,
    PY_TYPE_INSTANCE,
    PY_TYPE_INT,
    PY_TYPE_STR,
    PY_TYPE_USER_CLASS_START,
)
from pcc.extern import c_abi_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    atomic_cas_i64,
    atomic_load_i64,
    atomic_load_i32,
    atomic_rmw_i32,
    cstr,
    define_global_i32,
    define_global_i64,
    define_global_ptr_null,
    free,
    global_addr,
    global_load_ptr,
    global_store_ptr,
    int_to_ptr,
    ptr_to_int,
    is_tagged_int,
    load_i32,
    load_i64,
    load_ptr,
    malloc,
    memcpy,
    memset,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    realloc,
    stack_alloc,
    store_i8,
    store_i32,
    store_i64,
    store_ptr,
    strlen,
    write,
)

pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
pcc_gc_unpin = extern("pcc_gc_unpin", (c_ptr,), c_void)
py_exc_builtin_class = extern("py_exc_builtin_class", (c_int64,), c_ptr)
py_isinstance = extern("py_isinstance", (c_ptr, c_ptr), c_int64)
py_obj_str = extern("py_obj_str", (c_ptr,), c_ptr)
py_str_new = extern("py_str_new", (c_ptr, c_int64), c_ptr)
py_str_utf8 = extern("py_str_utf8", (c_ptr,), c_ptr)
py_tls_exc_get = extern("py_tls_exc_get", (), c_ptr)
py_tls_exc_set = extern("py_tls_exc_set", (c_ptr,), c_void)
py_current_exception = extern("py_current_exception", (), c_ptr)
py_incref = extern("py_incref", (c_ptr,), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_clear_exception = extern("py_clear_exception", (), c_void)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_class_new = extern("py_class_new", (c_ptr, c_ptr, c_int64, c_ptr, c_int64), c_ptr)
py_instance_new = extern("py_instance_new", (c_ptr,), c_ptr)
py_instance_setattr = extern("py_instance_setattr", (c_ptr, c_ptr, c_ptr), c_int64)
py_int_from_i64 = extern("py_int_from_i64", (c_int64,), c_ptr)
py_dict_new = extern("py_dict_new", (), c_ptr)
py_exc_matches = extern("py_exc_matches", (c_ptr, c_ptr), c_int64)
py_int_value_i64 = extern("py_int_value_i64", (c_ptr,), c_int64)
py_instance_getattr = extern("py_instance_getattr", (c_ptr, c_ptr), c_ptr)

define_global_ptr_null("py_traceback_class_cache")
define_global_ptr_null("py_frame_class_cache")
define_global_ptr_null("py_code_class_cache")


# Cache slots are process-lifetime owners. The mutex protects registration and
# publication, never managed allocation or destruction. Candidates remain in
# an ordinary construction frame until publication (including reentrant calls).
define_global_i64("pcc_traceback_cache_mutex_bits", 0)
define_global_i32("pcc_traceback_cache_registered", 0)
define_global_i32("pcc_frame_cache_registered", 0)
define_global_i32("pcc_code_cache_registered", 0)
define_global_i32("pcc_traceback_borrowed_map", -1)
define_global_i32("pcc_traceback_owned_map", 9)

_TB_EXCEPTION = 0
_TB_CLASS = 1
_TB_FRAME_CLASS = 2
_TB_CODE_CLASS = 3
_TB_CHAIN = 4
_TB_CODE = 5
_TB_FRAME = 6
_TB_ENTRY = 7
_TB_VALUE = 8
_TB_SLOT_COUNT = 9

pcc_mutex_new = extern("pcc_mutex_new", (), c_ptr)
pcc_mutex_free = extern("pcc_mutex_free", (c_ptr,), c_void)
pcc_mutex_lock = extern("pcc_mutex_lock", (c_ptr,), c_int64)
pcc_mutex_unlock = extern("pcc_mutex_unlock", (c_ptr,), c_int64)
pcc_gc_scheduler_root_register_handle = extern("pcc_gc_scheduler_root_register_handle", (c_ptr,), c_ptr)
pcc_gc_frame_enter = extern("pcc_gc_frame_enter", (c_ptr, c_ptr), c_void)
pcc_gc_frame_leave = extern("pcc_gc_frame_leave", (c_ptr,), c_void)
pcc_gc_store_root = extern("pcc_gc_store_root", (c_ptr, c_ptr), c_void)
pcc_gc_root_move = extern("pcc_gc_root_move", (c_ptr, c_ptr), c_int64)
pcc_gc_root_copy_lease = extern("pcc_gc_root_copy_lease", (c_ptr, c_ptr), c_int64)
py_handled_exception_slot = extern("py_handled_exception_slot", (), c_ptr)
py_exc_traceback_format_exc_abi = extern("py_exc_traceback_format_exc", (c_ptr,), c_ptr)
py_exc_traceback_print_exc_abi = extern("py_exc_traceback_print_exc", (c_ptr,), c_void)
define_global_i32("pcc_traceback_current_map", 2)

py_runtime_error_if_unset_abi = extern("py_runtime_error_if_unset", (c_ptr, c_ptr), c_ptr)
pcc_gc_root_copy_borrowed_lease = extern("pcc_gc_root_copy_borrowed_lease", (c_ptr, c_ptr), c_int64)
pcc_gc_foreign_lease_acquire = extern("pcc_gc_foreign_lease_acquire", (c_ptr,), c_int64)
pcc_gc_take_pinned_slot = extern("pcc_gc_take_pinned_slot", (c_ptr, c_int64), c_ptr)
pcc_gc_note_slot_write_barrier = extern("pcc_gc_note_slot_write_barrier", (c_ptr, c_ptr, c_ptr), c_void)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)
py_cleanup_one_root_preserving_exception = extern("py_cleanup_one_root_preserving_exception", (c_ptr,), c_void)
py_cleanup_one_lease_preserving_exception = extern("py_cleanup_one_lease_preserving_exception", (c_ptr, c_int64), c_void)


def _type_of(obj) -> int:
    if is_tagged_int(obj):
        return PY_TYPE_INT       # PY_TYPE_INT
    return load_i32(obj, 8)


def _is_exception(obj) -> int:
    if ptr_is_null(obj) != 0:
        return 0
    if is_tagged_int(obj):
        return 0
    if _type_of(obj) != PY_TYPE_EXC:        # PY_TYPE_EXC
        return 0
    return 1


def _instance_like(obj) -> int:
    if ptr_is_null(obj) != 0:
        return 0
    if is_tagged_int(obj):
        return 0
    tag: int = _type_of(obj)
    if tag == PY_TYPE_INSTANCE:             # PY_TYPE_INSTANCE
        return 1
    if tag >= PY_TYPE_USER_CLASS_START:
        return 1
    return 0


def _is_user_exception(obj) -> int:
    if _instance_like(obj) == 0:
        return 0
    base = py_exc_builtin_class(0)       # PY_EXC_BASE
    if ptr_is_null(base) != 0:
        return 0
    return py_isinstance(obj, base)


def _write_raw(p) -> None:
    if ptr_is_null(p) != 0:
        return
    n: int = strlen(p)
    if n > 0:
        write(2, p, n)


def _write_raw_or(p, fallback) -> None:
    if ptr_is_null(p) != 0:
        _write_raw(fallback)
        return
    _write_raw(p)


def _write_i64(v: int) -> None:
    if v == 0:
        write(2, cstr("0"), 1)
        return
    if v < 0:
        write(2, cstr("-"), 1)
        v = 0 - v

    buf = malloc(32)
    if ptr_is_null(buf) != 0:
        return
    n: int = 0
    while v > 0:
        digit: int = v % 10
        store_i8(buf, n, 48 + digit)
        n = n + 1
        v = v // 10
    i: int = n - 1
    while i >= 0:
        write(2, ptr_add(buf, i), 1)
        i = i - 1
    free(buf)


def _write_heading(e) -> None:
    cls_name = cstr("Exception")
    cls = pcc_gc_load_ptr(e, ptr_add(e, 16))
    if ptr_is_null(cls) == 0:
        name = load_ptr(cls, PYCLASSOBJECT_NAME_OFFSET)
        if ptr_is_null(name) == 0:
            cls_name = name

    text = _exc_display_text(e)
    if ptr_is_null(text) == 0:
        _write_raw(cls_name)
        write(2, cstr(": "), 2)
        _write_raw(py_str_utf8(text))
        write(2, cstr("\n"), 1)
        py_decref(text)
        return
    _write_raw(cls_name)
    write(2, cstr("\n"), 1)


def _exc_display_text(e):
    """NEW non-empty ``str(e)`` of a builtin exception object, or NULL.

    Non-str arguments (``ValueError(3)``, ``KeyError(('a', 1))``) render as
    CPython's ``str(exc)`` does instead of being dropped from the heading.
    """
    msg = pcc_gc_load_ptr(e, ptr_add(e, 24))
    if ptr_is_null(msg) != 0:
        return null()
    text = py_obj_str(e)
    if ptr_is_null(text) != 0:
        if py_err_occurred() != 0:
            py_clear_exception()
        return null()
    if _type_of(text) != PY_TYPE_STR or strlen(py_str_utf8(text)) == 0:
        py_decref(text)
        return null()
    return text


def _write_user_exception_heading(exc) -> None:
    cls_name = cstr("Exception")
    cls = pcc_gc_load_ptr(exc, ptr_add(exc, 16))
    if ptr_is_null(cls) == 0:
        name = load_ptr(cls, PYCLASSOBJECT_NAME_OFFSET)
        if ptr_is_null(name) == 0:
            cls_name = name

    saved_exc = py_current_exception()
    saved_exc_pin: int = 0
    if ptr_is_null(saved_exc) == 0:
        py_incref(saved_exc)
        saved_exc_pin = load_i32(saved_exc, 12) & 64
        pcc_gc_pin(saved_exc)
        py_tls_exc_set(null())
    msg = py_obj_str(exc)
    if ptr_is_null(py_tls_exc_get()) == 0:
        py_clear_exception()
    if ptr_is_null(saved_exc) == 0:
        py_tls_exc_set(saved_exc)
        pcc_gc_unpin(saved_exc)
        if saved_exc_pin != 0:
            atomic_rmw_i32("or", saved_exc, 12, 64, "relaxed")
        py_decref(saved_exc)
    if ptr_is_null(msg) == 0:
        if _type_of(msg) == PY_TYPE_STR:          # PY_TYPE_STR
            raw = py_str_utf8(msg)
            if ptr_is_null(raw) == 0:
                if strlen(raw) > 0:
                    _write_raw(cls_name)
                    write(2, cstr(": "), 2)
                    _write_raw(raw)
                    write(2, cstr("\n"), 1)
                    py_decref(msg)
                    return
        py_decref(msg)
    _write_raw(cls_name)
    write(2, cstr("\n"), 1)


@c_abi_export("py_exc_append_frame")
def py_exc_append_frame(exc, func_name, filename, line: int) -> None:
    py_exc_append_frame_source(exc, func_name, filename, null(), line)


@c_abi_export("py_exc_append_frame_source")
def py_exc_append_frame_source(
    exc, func_name, filename, source_line, line: int
) -> None:
    if _is_exception(exc) == 0:
        return

    n_frames: int = load_i32(exc, 56)
    cap_frames: int = load_i32(exc, 60)
    if n_frames == cap_frames:
        new_cap: int = 8
        if cap_frames != 0:
            new_cap = cap_frames * 2
        newbuf = realloc(load_ptr(exc, 48), new_cap * 32)
        if ptr_is_null(newbuf) != 0:
            return
        store_ptr(exc, 48, newbuf)
        store_i32(exc, 60, new_cap)

    traceback = load_ptr(exc, 48)
    fr = ptr_add(traceback, n_frames * 32)
    store_ptr(fr, 0, func_name)
    store_ptr(fr, 8, filename)
    store_ptr(fr, 16, source_line)
    store_i32(fr, 24, line)
    store_i32(fr, 28, 0)
    store_i32(exc, 56, n_frames + 1)


@c_abi_export("py_exc_append_frame_indexed")
def py_exc_append_frame_indexed(
    exc, func_name, filename, lines, sources, index: int
) -> None:
    # Mirror of py_exc_append_frame_indexed in py_exc_traceback.c: one shared
    # landing per function/target reads the raise site's (line, source) pair
    # from the module tables by index.
    py_exc_append_frame_source(
        exc,
        func_name,
        filename,
        load_ptr(sources, index * 8),
        load_i32(lines, index * 4),
    )


@c_abi_export("py_runtime_error_if_unset")
def py_runtime_error_if_unset(helper_name: c_ptr, message: c_ptr) -> c_ptr:
    if py_err_occurred() != 0:
        return null()
    if ptr_is_null(helper_name) != 0:
        helper_name = cstr("<pcc runtime>")
    if ptr_is_null(message) != 0:
        message = cstr(
            "runtime helper returned NULL without setting an exception"
        )
    exc = py_exc_new(7, message)  # PY_EXC_RUNTIMEERROR
    if ptr_is_null(exc) == 0:
        py_exc_append_frame_source(
            exc,
            helper_name,
            cstr("<pcc runtime>"),
            cstr("runtime contract: NULL result without an exception"),
            0,
        )
        py_raise_owned(exc)
    return null()


@c_abi_export("py_exc_print_unhandled")
def py_exc_print_unhandled(exc) -> None:
    if _is_exception(exc) == 0:
        if _is_user_exception(exc) != 0:
            _write_user_exception_heading(exc)
            return
        write(2, cstr("Unhandled non-exception object"), 30)
        if ptr_is_null(exc) != 0:
            write(2, cstr(" (null)\n"), 8)
            return
        if is_tagged_int(exc):
            write(2, cstr(" (tagged int)\n"), 14)
            return
        tag: int = _type_of(exc)
        write(2, cstr(" (tag="), 6)
        _write_i64(tag)
        write(2, cstr(")"), 1)
        if tag == PY_TYPE_STR:             # PY_TYPE_STR
            write(2, cstr(": "), 2)
            _write_raw(ptr_add(exc, 40))
        write(2, cstr("\n"), 1)
        return

    cause = pcc_gc_load_ptr(exc, ptr_add(exc, 32))
    context = pcc_gc_load_ptr(exc, ptr_add(exc, 40))
    suppressed: int = atomic_load_i32(exc, 12, "relaxed") & PY_FLAG_EXC_SUPPRESS_CONTEXT
    if _is_exception(cause) != 0:
        py_exc_print_unhandled(cause)
        write(
            2,
            cstr(
                "\nThe above exception was the direct cause of the "
                "following exception:\n\n"
            ),
            71,
        )
    elif suppressed == 0 and _is_exception(context) != 0:
        py_exc_print_unhandled(context)
        write(
            2,
            cstr(
                "\nDuring handling of the above exception, another "
                "exception occurred:\n\n"
            ),
            70,
        )

    write(2, cstr("Traceback (most recent call last):\n"), 35)
    traceback = load_ptr(exc, 48)
    n_frames: int = load_i32(exc, 56)
    i: int = n_frames - 1
    while i >= 0:
        fr = ptr_add(traceback, i * 32)
        func_name = load_ptr(fr, 0)
        filename = load_ptr(fr, 8)
        source_line = load_ptr(fr, 16)
        line: int = load_i32(fr, 24)
        write(2, cstr("  File \""), 8)
        _write_raw_or(filename, cstr("<unknown>"))
        write(2, cstr("\", line "), 8)
        _write_i64(line)
        write(2, cstr(", in "), 5)
        _write_raw_or(func_name, cstr("<module>"))
        write(2, cstr("\n"), 1)
        if ptr_is_null(source_line) == 0:
            if strlen(source_line) > 0:
                write(2, cstr("    "), 4)
                _write_raw(source_line)
                write(2, cstr("\n"), 1)
        i = i - 1
    _write_heading(exc)


# ---- traceback.format_exc() / traceback.print_exc() -----------------
#
# Mirrors the PccTbBuf helpers in py_exc_traceback.c: a growable heap
# buffer {buf ptr @0, len i64 @8, cap i64 @16} that collects the
# CPython-style traceback text. Frames are emitted in reverse trail
# order (pcc appends the raise site first; CPython prints the outermost
# frame first under "most recent call last").


def _tb_buf_new():
    b = malloc(24)
    if ptr_is_null(b) != 0:
        return null()
    store_ptr(b, 0, null())
    store_i64(b, 8, 0)
    store_i64(b, 16, 0)
    return b


def _tb_reserve(b, extra: int) -> int:
    length: int = load_i64(b, 8)
    cap: int = load_i64(b, 16)
    if length + extra + 1 <= cap:
        return 1
    new_cap: int = cap
    if new_cap == 0:
        new_cap = 256
    while new_cap < length + extra + 1:
        new_cap = new_cap * 2
    nb = realloc(load_ptr(b, 0), new_cap)
    if ptr_is_null(nb) != 0:
        return 0
    store_ptr(b, 0, nb)
    store_i64(b, 16, new_cap)
    return 1


def _tb_append_n(b, s, n: int) -> None:
    if ptr_is_null(s) != 0:
        return
    if n <= 0:
        return
    if _tb_reserve(b, n) == 0:
        return
    buf = load_ptr(b, 0)
    length: int = load_i64(b, 8)
    memcpy(ptr_add(buf, length), s, n)
    store_i64(b, 8, length + n)
    store_i8(buf, length + n, 0)


def _tb_append(b, s) -> None:
    if ptr_is_null(s) != 0:
        return
    _tb_append_n(b, s, strlen(s))


def _tb_append_i64(b, v: int) -> None:
    if v == 0:
        _tb_append_n(b, cstr("0"), 1)
        return
    if v < 0:
        _tb_append_n(b, cstr("-"), 1)
        v = 0 - v
    tmp = malloc(32)
    if ptr_is_null(tmp) != 0:
        return
    n: int = 0
    while v > 0:
        digit: int = v % 10
        store_i8(tmp, n, 48 + digit)
        n = n + 1
        v = v // 10
    i: int = n - 1
    while i >= 0:
        _tb_append_n(b, ptr_add(tmp, i), 1)
        i = i - 1
    free(tmp)


def _tb_append_exc_heading(b, e) -> None:
    cls_name = cstr("Exception")
    cls = pcc_gc_load_ptr(e, ptr_add(e, 16))
    if ptr_is_null(cls) == 0:
        name = load_ptr(cls, PYCLASSOBJECT_NAME_OFFSET)
        if ptr_is_null(name) == 0:
            cls_name = name

    text = _exc_display_text(e)
    if ptr_is_null(text) == 0:
        _tb_append(b, cls_name)
        _tb_append_n(b, cstr(": "), 2)
        _tb_append(b, py_str_utf8(text))
        _tb_append_n(b, cstr("\n"), 1)
        py_decref(text)
        return
    _tb_append(b, cls_name)
    _tb_append_n(b, cstr("\n"), 1)


def _tb_append_user_exc_heading(b, exc) -> None:
    cls_name = cstr("Exception")
    cls = pcc_gc_load_ptr(exc, ptr_add(exc, 16))
    if ptr_is_null(cls) == 0:
        name = load_ptr(cls, PYCLASSOBJECT_NAME_OFFSET)
        if ptr_is_null(name) == 0:
            cls_name = name

    saved_exc = py_current_exception()
    saved_exc_pin: int = 0
    if ptr_is_null(saved_exc) == 0:
        py_incref(saved_exc)
        saved_exc_pin = load_i32(saved_exc, 12) & 64
        pcc_gc_pin(saved_exc)
        py_tls_exc_set(null())
    msg = py_obj_str(exc)
    if ptr_is_null(py_tls_exc_get()) == 0:
        py_clear_exception()
    if ptr_is_null(saved_exc) == 0:
        py_tls_exc_set(saved_exc)
        pcc_gc_unpin(saved_exc)
        if saved_exc_pin != 0:
            atomic_rmw_i32("or", saved_exc, 12, 64, "relaxed")
        py_decref(saved_exc)
    if ptr_is_null(msg) == 0:
        if _type_of(msg) == PY_TYPE_STR:          # PY_TYPE_STR
            raw = py_str_utf8(msg)
            if ptr_is_null(raw) == 0:
                if strlen(raw) > 0:
                    _tb_append(b, cls_name)
                    _tb_append_n(b, cstr(": "), 2)
                    _tb_append(b, raw)
                    _tb_append_n(b, cstr("\n"), 1)
                    py_decref(msg)
                    return
        py_decref(msg)
    _tb_append(b, cls_name)
    _tb_append_n(b, cstr("\n"), 1)


def _tb_format_into(b, exc, depth: int) -> None:
    if ptr_is_null(exc) != 0:
        _tb_append(b, cstr("NoneType: None\n"))
        return
    if is_tagged_int(exc):
        _tb_append(b, cstr("NoneType: None\n"))
        return
    if _is_exception(exc) == 0:
        if _is_user_exception(exc) != 0:
            # User exception subclass instances raised as-is carry no
            # PyFrameRecord trail; emit the heading under the CPython
            # banner so callers still see the exception identity.
            _tb_append(b, cstr("Traceback (most recent call last):\n"))
            _tb_append_user_exc_heading(b, exc)
            return
        _tb_append(b, cstr("NoneType: None\n"))
        return

    # Chained causes oldest-first, CPython-style. Depth-capped so a
    # pathological __context__ cycle cannot recurse forever.
    if depth < 8:
        cause = pcc_gc_load_ptr(exc, ptr_add(exc, 32))
        context = pcc_gc_load_ptr(exc, ptr_add(exc, 40))
        suppressed: int = atomic_load_i32(exc, 12, "relaxed") & PY_FLAG_EXC_SUPPRESS_CONTEXT
        if _is_exception(cause) != 0:
            _tb_format_into(b, cause, depth + 1)
            _tb_append(
                b,
                cstr(
                    "\nThe above exception was the direct cause of the "
                    "following exception:\n\n"
                ),
            )
        elif suppressed == 0 and _is_exception(context) != 0:
            _tb_format_into(b, context, depth + 1)
            _tb_append(
                b,
                cstr(
                    "\nDuring handling of the above exception, another "
                    "exception occurred:\n\n"
                ),
            )

    _tb_append(b, cstr("Traceback (most recent call last):\n"))
    traceback = load_ptr(exc, 48)
    n_frames: int = load_i32(exc, 56)
    i: int = n_frames - 1
    while i >= 0:
        fr = ptr_add(traceback, i * 32)
        func_name = load_ptr(fr, 0)
        filename = load_ptr(fr, 8)
        source_line = load_ptr(fr, 16)
        line: int = load_i32(fr, 24)
        _tb_append_n(b, cstr("  File \""), 8)
        if ptr_is_null(filename) != 0:
            _tb_append_n(b, cstr("<unknown>"), 9)
        else:
            _tb_append(b, filename)
        _tb_append_n(b, cstr("\", line "), 8)
        _tb_append_i64(b, line)
        _tb_append_n(b, cstr(", in "), 5)
        if ptr_is_null(func_name) != 0:
            _tb_append_n(b, cstr("<module>"), 8)
        else:
            _tb_append(b, func_name)
        _tb_append_n(b, cstr("\n"), 1)
        if ptr_is_null(source_line) == 0:
            if strlen(source_line) > 0:
                _tb_append_n(b, cstr("    "), 4)
                _tb_append(b, source_line)
                _tb_append_n(b, cstr("\n"), 1)
        i = i - 1
    _tb_append_exc_heading(b, exc)


@c_abi_export("py_exc_traceback_format_exc")
def py_exc_traceback_format_exc(exc):
    b = _tb_buf_new()
    if ptr_is_null(b) != 0:
        return py_str_new(cstr(""), 0)
    _tb_format_into(b, exc, 0)
    buf = load_ptr(b, 0)
    length: int = load_i64(b, 8)
    if ptr_is_null(buf) != 0:
        free(b)
        return py_str_new(cstr(""), 0)
    result = py_str_new(buf, length)
    free(buf)
    free(b)
    return result


@c_abi_export("py_exc_traceback_print_exc")
def py_exc_traceback_print_exc(exc) -> None:
    b = _tb_buf_new()
    if ptr_is_null(b) != 0:
        return
    _tb_format_into(b, exc, 0)
    buf = load_ptr(b, 0)
    length: int = load_i64(b, 8)
    if ptr_is_null(buf) == 0:
        if length > 0:
            write(2, buf, length)
        free(buf)
    free(b)


def _tb_current_finish(slots, lease: int):
    # The formatter result is published before either operand cleanup or
    # frame retirement. Keep its NEW owner through the final raw ABI handoff.
    if lease >= 0:
        py_cleanup_one_lease_preserving_exception(slots, lease)
    py_cleanup_one_root_preserving_exception(slots)
    result = ptr_add(slots, C_POINTER_SIZE)
    pcc_py_gc_minor_graph_lock()
    value = pcc_gc_load_ptr(null(), result)
    prior: int = 0
    if ptr_is_null(value) == 0 and is_tagged_int(value) == 0:
        prior = load_i32(value, 12) & 64
        pcc_gc_pin(value)
    pcc_py_gc_minor_graph_unlock()
    pcc_gc_frame_leave(slots)
    return pcc_gc_take_pinned_slot(result, prior)


def _tb_current_call(print_only: int):
    # This module is built without implicit polls. All managed state is in
    # registered slots before any allocating call. The dynamic handled slot
    # belongs to the caller's still-live handler, including across ordinary
    # provider/helper function calls; pending TLS is not the handled state.
    slots = stack_alloc(2 * C_POINTER_SIZE)
    memset(slots, 0, 2 * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_traceback_current_map"), slots)
    source = py_handled_exception_slot()
    lease: int = 0
    if ptr_is_null(source) == 0:
        lease = pcc_gc_root_copy_lease(slots, source)
        if lease < 0:
            _tb_error(cstr("handled exception owner acquisition failed"))
            return _tb_current_finish(slots, lease)
    if print_only != 0:
        py_exc_traceback_print_exc_abi(load_ptr(slots, 0))
        pcc_gc_store_root(ptr_add(slots, C_POINTER_SIZE), global_load_ptr("py_None"))
    else:
        store_ptr(slots, C_POINTER_SIZE, py_exc_traceback_format_exc_abi(load_ptr(slots, 0)))
        _tb_adopt(ptr_add(slots, C_POINTER_SIZE))
    return _tb_current_finish(slots, lease)


@c_abi_export("py_exc_traceback_format_current")
def py_exc_traceback_format_current():
    """NEW text for the dynamically handled exception, or NoneType: None."""
    return _tb_current_call(0)


@c_abi_export("py_exc_traceback_print_current")
def py_exc_traceback_print_current() -> None:
    # The shared result is the immortal None singleton on this branch.
    _tb_current_call(1)


def _tb_cache_mutex():
    slot = global_addr("pcc_traceback_cache_mutex_bits")
    bits: int = atomic_load_i64(slot, 0, "acquire")
    if bits != 0:
        return int_to_ptr(bits)
    candidate = pcc_mutex_new()
    if ptr_is_null(candidate):
        return null()
    installed: int = atomic_cas_i64(slot, 0, 0, ptr_to_int(candidate), "acq_rel", "acquire")
    if installed != 0:
        pcc_mutex_free(candidate)
        return int_to_ptr(installed)
    return candidate


def _tb_error(message) -> int:
    py_runtime_error_if_unset_abi(cstr("traceback construction"), message)
    return -1


def _tb_adopt(slot) -> None:
    # NEW results are stored directly into an empty registered slot before
    # any call. The barrier reloads under the relocation graph lock.
    pcc_py_gc_minor_graph_lock()
    pcc_gc_note_slot_write_barrier(null(), slot, load_ptr(slot, 0))
    pcc_py_gc_minor_graph_unlock()


def _tb_cache_class(cache, registered, name, destination, candidate) -> int:
    mutex = _tb_cache_mutex()
    if ptr_is_null(mutex) or pcc_mutex_lock(mutex) != 0:
        return _tb_error(cstr("class cache lock failed"))
    if load_i32(registered, 0) == 0:
        handle = pcc_gc_scheduler_root_register_handle(cache)
        if ptr_is_null(handle):
            pcc_mutex_unlock(mutex)
            return _tb_error(cstr("class cache root registration failed"))
        store_i32(registered, 0, 1)
    pcc_py_gc_minor_graph_lock()
    pcc_gc_store_root(destination, pcc_gc_load_ptr(null(), cache))
    pcc_py_gc_minor_graph_unlock()
    pcc_mutex_unlock(mutex)
    if ptr_is_null(load_ptr(destination, 0)) == 0:
        return 0

    # A finalizer may reenter here and publish first. No cache mutex or graph
    # lock spans construction, and the losing candidate never escapes.
    store_ptr(candidate, 0, py_class_new(name, null(), 0, null(), 0))
    _tb_adopt(candidate)
    if ptr_is_null(load_ptr(candidate, 0)):
        return _tb_error(cstr("class cache allocation failed"))
    if pcc_mutex_lock(mutex) != 0:
        _tb_discard_class(candidate)
        return _tb_error(cstr("class cache publication lock failed"))
    pcc_py_gc_minor_graph_lock()
    published = pcc_gc_load_ptr(null(), cache)
    if ptr_is_null(published):
        pcc_gc_store_root(cache, pcc_gc_load_ptr(null(), candidate))
    pcc_gc_store_root(destination, pcc_gc_load_ptr(null(), cache))
    loser: int = 1 - ptr_eq(pcc_gc_load_ptr(null(), candidate), pcc_gc_load_ptr(null(), cache))
    pcc_py_gc_minor_graph_unlock()
    pcc_mutex_unlock(mutex)
    if loser != 0:
        _tb_discard_class(candidate)
    else:
        py_cleanup_one_root_preserving_exception(candidate)
    return 0


def _tb_discard_class(candidate) -> None:
    # Only our unexposed, empty, zero-base synthetic class reaches this path.
    # Its self-MRO entries are borrowed, and its initial reference is the sole
    # owner. Demote this unpublished shell so ordinary class disposal can
    # retire its allocation and MRO even on refcount-only GC0.
    pcc_py_gc_minor_graph_lock()
    value = pcc_gc_load_ptr(null(), candidate)
    atomic_rmw_i32("and", value, 12, ~PY_FLAG_IMMORTAL, "relaxed")
    pcc_py_gc_minor_graph_unlock()
    py_cleanup_one_root_preserving_exception(candidate)


def _tb_new_instance(slots, class_index: int, result_index: int) -> int:
    source = ptr_add(slots, class_index * C_POINTER_SIZE)
    destination = ptr_add(slots, result_index * C_POINTER_SIZE)
    lease: int = pcc_gc_foreign_lease_acquire(source)
    if lease < 0:
        return _tb_error(cstr("class address lease failed"))
    store_ptr(destination, 0, py_instance_new(load_ptr(source, 0)))
    _tb_adopt(destination)
    py_cleanup_one_lease_preserving_exception(source, lease)
    if ptr_is_null(load_ptr(destination, 0)):
        return _tb_error(cstr("instance allocation failed"))
    return 0


def _tb_set_slot(slots, object_index: int, name, value_index: int) -> int:
    destination = ptr_add(slots, object_index * C_POINTER_SIZE)
    value = ptr_add(slots, value_index * C_POINTER_SIZE)
    if ptr_is_null(load_ptr(value, 0)):
        return -1
    object_lease: int = pcc_gc_foreign_lease_acquire(destination)
    if object_lease < 0:
        return _tb_error(cstr("instance address lease failed"))
    value_lease: int = pcc_gc_foreign_lease_acquire(value)
    if value_lease < 0:
        py_cleanup_one_lease_preserving_exception(destination, object_lease)
        return _tb_error(cstr("attribute address lease failed"))
    rc: int = py_instance_setattr(load_ptr(destination, 0), name, load_ptr(value, 0))
    py_cleanup_one_lease_preserving_exception(value, value_lease)
    py_cleanup_one_lease_preserving_exception(destination, object_lease)
    return rc


def _tb_set_value(slots, object_index: int, name) -> int:
    value = ptr_add(slots, _TB_VALUE * C_POINTER_SIZE)
    _tb_adopt(value)
    rc: int = _tb_set_slot(slots, object_index, name, _TB_VALUE)
    py_cleanup_one_root_preserving_exception(value)
    return rc


def _tb_cstr_or(p, fallback):
    if ptr_is_null(p) != 0:
        return py_str_new(fallback, strlen(fallback))
    return py_str_new(p, strlen(p))


def _tb_new_entry(slots, index: int) -> int:
    """Build through authoritative slots; lease only individual raw ABI calls."""
    pcc_py_gc_minor_graph_lock()
    exc = pcc_gc_load_ptr(null(), ptr_add(slots, _TB_EXCEPTION * C_POINTER_SIZE))
    fr = ptr_add(load_ptr(exc, 48), index * 32)
    # Frame metadata points to immutable native strings, not managed objects.
    func_name = load_ptr(fr, 0)
    filename = load_ptr(fr, 8)
    line: int = load_i32(fr, 24)
    pcc_py_gc_minor_graph_unlock()
    value = ptr_add(slots, _TB_VALUE * C_POINTER_SIZE)
    if _tb_new_instance(slots, _TB_CODE_CLASS, _TB_CODE) != 0:
        return -1
    store_ptr(value, 0, _tb_cstr_or(filename, cstr("<unknown>")))
    if _tb_set_value(slots, _TB_CODE, cstr("co_filename")) != 0:
        return -1
    store_ptr(value, 0, _tb_cstr_or(func_name, cstr("<module>")))
    if _tb_set_value(slots, _TB_CODE, cstr("co_name")) != 0:
        return -1
    store_ptr(value, 0, _tb_cstr_or(func_name, cstr("<module>")))
    if _tb_set_value(slots, _TB_CODE, cstr("co_qualname")) != 0:
        return -1
    store_ptr(value, 0, py_int_from_i64(line))
    if _tb_set_value(slots, _TB_CODE, cstr("co_firstlineno")) != 0:
        return -1
    if _tb_new_instance(slots, _TB_FRAME_CLASS, _TB_FRAME) != 0:
        return -1
    if _tb_set_slot(slots, _TB_FRAME, cstr("f_code"), _TB_CODE) != 0:
        return -1
    py_cleanup_one_root_preserving_exception(ptr_add(slots, _TB_CODE * C_POINTER_SIZE))
    store_ptr(value, 0, py_int_from_i64(line))
    if _tb_set_value(slots, _TB_FRAME, cstr("f_lineno")) != 0:
        return -1
    store_ptr(value, 0, py_int_from_i64(-1))
    if _tb_set_value(slots, _TB_FRAME, cstr("f_lasti")) != 0:
        return -1
    store_ptr(value, 0, py_dict_new())
    if _tb_set_value(slots, _TB_FRAME, cstr("f_globals")) != 0:
        return -1
    store_ptr(value, 0, py_dict_new())
    if _tb_set_value(slots, _TB_FRAME, cstr("f_locals")) != 0:
        return -1
    pcc_gc_store_root(value, global_load_ptr("py_None"))
    if _tb_set_value(slots, _TB_FRAME, cstr("f_back")) != 0:
        return -1
    if _tb_new_instance(slots, _TB_CLASS, _TB_ENTRY) != 0:
        return -1
    if _tb_set_slot(slots, _TB_ENTRY, cstr("tb_frame"), _TB_FRAME) != 0:
        return -1
    py_cleanup_one_root_preserving_exception(ptr_add(slots, _TB_FRAME * C_POINTER_SIZE))
    store_ptr(value, 0, py_int_from_i64(line))
    if _tb_set_value(slots, _TB_ENTRY, cstr("tb_lineno")) != 0:
        return -1
    store_ptr(value, 0, py_int_from_i64(-1))
    if _tb_set_value(slots, _TB_ENTRY, cstr("tb_lasti")) != 0:
        return -1
    return _tb_set_slot(slots, _TB_ENTRY, cstr("tb_next"), _TB_CHAIN)


def _tb_finish(slots, borrowed, success: int):
    index: int = 0
    while index < _TB_SLOT_COUNT:
        if index != _TB_CHAIN or success == 0:
            py_cleanup_one_root_preserving_exception(ptr_add(slots, index * C_POINTER_SIZE))
        index = index + 1
    result = ptr_add(slots, _TB_CHAIN * C_POINTER_SIZE)
    pcc_py_gc_minor_graph_lock()
    value = pcc_gc_load_ptr(null(), result)
    prior: int = 0
    if ptr_is_null(value) == 0 and is_tagged_int(value) == 0:
        prior = load_i32(value, 12) & 64
        pcc_gc_pin(value)
    pcc_py_gc_minor_graph_unlock()
    pcc_gc_frame_leave(slots)
    pcc_gc_frame_leave(borrowed)
    return pcc_gc_take_pinned_slot(result, prior)


@c_abi_export("py_exc_traceback_object")
def py_exc_traceback_object(exc):
    """NEW traceback chain, starting at the outermost recorded frame.

    Register the incoming borrowed argument before the first call; all managed
    construction state thereafter lives in collector-rewritten owning slots.
    """
    borrowed = stack_alloc(C_POINTER_SIZE)
    store_ptr(borrowed, 0, exc)
    pcc_gc_frame_enter(global_addr("pcc_traceback_borrowed_map"), borrowed)
    slots = stack_alloc(_TB_SLOT_COUNT * C_POINTER_SIZE)
    memset(slots, 0, _TB_SLOT_COUNT * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_traceback_owned_map"), slots)
    lease: int = pcc_gc_root_copy_borrowed_lease(slots, borrowed)
    if lease < 0:
        _tb_error(cstr("exception owner acquisition failed"))
        return _tb_finish(slots, borrowed, 0)
    py_cleanup_one_lease_preserving_exception(slots, lease)
    pcc_py_gc_minor_graph_lock()
    exc = pcc_gc_load_ptr(null(), slots)
    n_frames: int = 0
    if _is_exception(exc) != 0:
        if ptr_is_null(load_ptr(exc, 48)) == 0:
            n_frames = load_i32(exc, 56)
    pcc_py_gc_minor_graph_unlock()
    pcc_gc_store_root(ptr_add(slots, _TB_CHAIN * C_POINTER_SIZE), global_load_ptr("py_None"))
    if n_frames <= 0:
        return _tb_finish(slots, borrowed, 1)
    candidate = ptr_add(slots, _TB_VALUE * C_POINTER_SIZE)
    if _tb_cache_class(global_addr("py_traceback_class_cache"), global_addr("pcc_traceback_cache_registered"), cstr("traceback"), ptr_add(slots, _TB_CLASS * C_POINTER_SIZE), candidate) != 0:
        return _tb_finish(slots, borrowed, 0)
    if _tb_cache_class(global_addr("py_frame_class_cache"), global_addr("pcc_frame_cache_registered"), cstr("frame"), ptr_add(slots, _TB_FRAME_CLASS * C_POINTER_SIZE), candidate) != 0:
        return _tb_finish(slots, borrowed, 0)
    if _tb_cache_class(global_addr("py_code_class_cache"), global_addr("pcc_code_cache_registered"), cstr("code"), ptr_add(slots, _TB_CODE_CLASS * C_POINTER_SIZE), candidate) != 0:
        return _tb_finish(slots, borrowed, 0)
    index: int = 0
    while index < n_frames:
        if _tb_new_entry(slots, index) != 0:
            return _tb_finish(slots, borrowed, 0)
        py_cleanup_one_root_preserving_exception(ptr_add(slots, _TB_CHAIN * C_POINTER_SIZE))
        if pcc_gc_root_move(ptr_add(slots, _TB_CHAIN * C_POINTER_SIZE), ptr_add(slots, _TB_ENTRY * C_POINTER_SIZE)) != 0:
            _tb_error(cstr("traceback chain owner transfer failed"))
            return _tb_finish(slots, borrowed, 0)
        index = index + 1
    return _tb_finish(slots, borrowed, 1)


@c_abi_export("py_exc_handle_uncaught")
def py_exc_handle_uncaught(exc) -> int:
    """Exit status for an exception that reached the top of the program.

    As in CPython: ``SystemExit(code)`` exits silently with ``code`` (None is
    0, an int is itself, anything else is printed to stderr and exits 1);
    ``KeyboardInterrupt`` prints its traceback and exits 130; every other
    exception prints its traceback and exits 1.
    """
    system_exit = py_exc_builtin_class(53)  # PY_EXC_SYSTEMEXIT
    if ptr_is_null(exc) == 0 and ptr_is_null(system_exit) == 0:
        if py_exc_matches(exc, system_exit) != 0:
            code = null()
            if _is_exception(exc) != 0:
                code = pcc_gc_load_ptr(exc, ptr_add(exc, 24))
            else:
                args = py_instance_getattr(exc, cstr("code"))
                if ptr_is_null(args) != 0:
                    py_clear_exception()
                else:
                    code = args
                    py_decref(args)
            none = global_load_ptr("py_None")
            if ptr_is_null(code) != 0 or ptr_eq(code, none) != 0:
                return 0
            if is_tagged_int(code) or _type_of(code) == PY_TYPE_INT:
                return py_int_value_i64(code)
            text = py_obj_str(code)
            if ptr_is_null(text) == 0:
                raw = py_str_utf8(text)
                if ptr_is_null(raw) == 0:
                    _write_raw(raw)
                write(2, cstr("\n"), 1)
                py_decref(text)
            return 1
    keyboard_interrupt = py_exc_builtin_class(54)  # PY_EXC_KEYBOARDINTERRUPT
    py_exc_print_unhandled(exc)
    if ptr_is_null(exc) == 0 and ptr_is_null(keyboard_interrupt) == 0:
        if py_exc_matches(exc, keyboard_interrupt) != 0:
            return 130
    return 1
