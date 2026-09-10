"""pcc-Python port of py_os_substrate.c.

The stat helpers delegate ABI-sensitive ``struct stat`` reads to
pcc.unsafe intrinsics. The runtime source stays portable and does not encode
Darwin/Linux field offsets directly.
"""

__pcc_runtime_port__ = True

from pcc.py_runtime.py.py_abi_constants import (
    PY_TYPE_BYTEARRAY,
    PY_TYPE_BYTES,
    PY_TYPE_INT,
    PY_TYPE_MEMORYVIEW,
    PY_TYPE_STR,
)

from pcc.extern import (
    extern,
    c_abi_export,
    c_double,
    c_int32,
    c_int64,
    c_ptr,
    c_size_t,
    c_void,
)
from pcc.unsafe import (
    cstr,
    define_global_ptr_null,
    free,
    global_load_ptr,
    global_store_ptr,
    is_tagged_int,
    load_i8,
    load_i32,
    load_i64,
    malloc,
    null,
    ptr_add,
    ptr_is_null,
    store_i64,
    strlen,
    target_platform_machine,
    target_sys_platform,
)

localtime_r = extern("localtime_r", (c_ptr, c_ptr), c_ptr)
strftime = extern("strftime", (c_ptr, c_size_t, c_ptr, c_ptr), c_size_t)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_obj_str = extern("py_obj_str", (c_ptr,), c_ptr)
py_str_new = extern("py_str_new", (c_ptr, c_int64), c_ptr)
py_str_utf8 = extern("py_str_utf8", (c_ptr,), c_ptr)
py_list_new = extern("py_list_new", (c_int64,), c_ptr)
py_list_append = extern("py_list_append", (c_ptr, c_ptr), c_void)
py_float_from_f64 = extern("py_float_from_f64", (c_double,), c_ptr)
pcc_runtime_now_us = extern("pcc_platform_wall_time_us", (), c_int64)
pcc_runtime_monotonic_us = extern("pcc_platform_monotonic_us", (), c_int64)
pcc_platform_sleep_ns = extern("pcc_platform_sleep_ns", (c_int64,), c_int64)
py_float_to_f64 = extern("py_float_to_f64", (c_ptr,), c_double)
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_str_byte_len = extern("py_str_byte_len", (c_ptr,), c_int64)
py_bytes_new = extern("py_bytes_new", (c_ptr, c_int64), c_ptr)
py_int_value_i64 = extern("py_int_value_i64", (c_ptr,), c_int64)
arc4random_buf = extern("arc4random_buf", (c_ptr, c_size_t), c_void)
read_sys = extern("pcc_platform_read", (c_int64, c_ptr, c_int64), c_int64)
write_sys = extern("pcc_platform_write", (c_int64, c_ptr, c_int64), c_int64)
pcc_platform_access = extern(
    "pcc_platform_access", (c_ptr, c_int64), c_int64
)
pcc_platform_getcwd = extern(
    "pcc_platform_getcwd", (c_ptr, c_int64), c_ptr
)
pcc_platform_stat_kind = extern(
    "pcc_platform_stat_kind", (c_ptr,), c_int64
)
pcc_platform_stat_mtime = extern(
    "pcc_platform_stat_mtime", (c_ptr,), c_double
)
pcc_platform_realpath = extern(
    "pcc_platform_realpath", (c_ptr, c_ptr, c_int64), c_ptr
)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
py_program_argv = extern("py_program_argv", (c_int64,), c_ptr)
py_program_mode = extern("py_program_mode", (), c_int32)


def _type_of(obj) -> int:
    if is_tagged_int(obj):
        return PY_TYPE_INT
    return load_i32(obj, 8)


define_global_ptr_null("py_path_getcwd_buf")
define_global_ptr_null("py_path_realpath_buf")


@c_abi_export("py_path_stat_kind")
def py_path_stat_kind(p) -> int:
    return pcc_platform_stat_kind(p)


@c_abi_export("py_path_stat_mtime")
def py_path_stat_mtime(p) -> float:
    return pcc_platform_stat_mtime(p)


@c_abi_export("py_time_monotonic")
def py_time_monotonic():
    return py_float_from_f64(pcc_runtime_monotonic_us() * 0.000001)


@c_abi_export("py_time_perf_counter")
def py_time_perf_counter():
    return py_float_from_f64(pcc_runtime_monotonic_us() * 0.000001)


@c_abi_export("py_time_time")
def py_time_time():
    return py_float_from_f64(pcc_runtime_now_us() * 0.000001)


@c_abi_export("py_time_sleep")
def py_time_sleep(delay):
    """``time.sleep(seconds)``.

    There was no native lowering for it, so any module calling ``time.sleep``
    resolved the call through CPython -- which fail-closes the whole enclosing
    function under ``--python-libpython=off``. asyncio's event loop uses it for
    its idle wait, so a loop that ever went idle died with "no-libpython
    function unavailable: asyncio._run_once".

    Accepts an int or a float the way CPython does. A zero delay is a no-op and
    a negative one is a ValueError, both matching CPython -- an earlier version
    silently accepted a negative delay, which a differential run against
    CPython caught.
    """
    seconds: float = py_float_to_f64(delay)
    if seconds < 0.0:
        py_raise_owned(
            py_exc_new(2, cstr("sleep length must be non-negative"))
        )  # PY_EXC_VALUEERROR
        return null()
    if seconds > 0.0:
        pcc_platform_sleep_ns(int(seconds * 1000000000.0))
    return global_load_ptr("py_None")


@c_abi_export("py_time_strftime")
def py_time_strftime(fmt):
    fmt_str = py_obj_str(fmt)
    if ptr_is_null(fmt_str):
        return null()
    raw_fmt = py_str_utf8(fmt_str)
    if ptr_is_null(raw_fmt):
        py_decref(fmt_str)
        return py_str_new(null(), 0)
    now_buf = malloc(8)
    tm_buf = malloc(128)
    out_buf = malloc(256)
    if ptr_is_null(now_buf) or ptr_is_null(tm_buf) or ptr_is_null(out_buf):
        free(now_buf)
        free(tm_buf)
        free(out_buf)
        py_decref(fmt_str)
        return null()
    store_i64(now_buf, 0, pcc_runtime_now_us() // 1000000)
    if ptr_is_null(localtime_r(now_buf, tm_buf)):
        free(now_buf)
        free(tm_buf)
        free(out_buf)
        py_decref(fmt_str)
        return py_str_new(null(), 0)
    n: int = strftime(out_buf, 256, raw_fmt, tm_buf)
    out = py_str_new(out_buf, n)
    free(now_buf)
    free(tm_buf)
    free(out_buf)
    py_decref(fmt_str)
    return out


@c_abi_export("py_sys_stdin_readline")
def py_sys_stdin_readline():
    buf = malloc(4096)
    if ptr_is_null(buf):
        return null()
    pos: int = 0
    while pos < 4095:
        got: int = read_sys(0, ptr_add(buf, pos), 1)
        if got <= 0:
            break
        if load_i8(buf, pos) == 10:
            pos = pos + 1
            break
        pos = pos + 1
    out = py_str_new(buf, pos)
    free(buf)
    return out


@c_abi_export("py_os_urandom")
def py_os_urandom(n_obj):
    n: int = py_int_value_i64(n_obj)
    if n < 0:
        return null()
    out = py_bytes_new(null(), n)
    if ptr_is_null(out):
        return null()
    if n > 0:
        arc4random_buf(ptr_add(out, 24), n)
    return out


@c_abi_export("py_path_getcwd")
def py_path_getcwd():
    buf = global_load_ptr("py_path_getcwd_buf")
    if ptr_is_null(buf):
        buf = malloc(8192)
        if ptr_is_null(buf):
            return null()
        global_store_ptr("py_path_getcwd_buf", buf)
    result = pcc_platform_getcwd(buf, 8192)
    if ptr_is_null(result):
        return null()
    return buf


@c_abi_export("py_path_realpath")
def py_path_realpath(p):
    if ptr_is_null(p):
        return null()
    buf = global_load_ptr("py_path_realpath_buf")
    if ptr_is_null(buf):
        buf = malloc(8192)
        if ptr_is_null(buf):
            return null()
        global_store_ptr("py_path_realpath_buf", buf)
    result = pcc_platform_realpath(p, buf, 8192)
    if ptr_is_null(result):
        return null()
    return buf


def _str_from_cstr(p):
    if ptr_is_null(p):
        return null()
    return py_str_new(p, strlen(p))


@c_abi_export("py_sys_platform_str")
def py_sys_platform_str():
    return _str_from_cstr(target_sys_platform())


@c_abi_export("py_platform_machine_str")
def py_platform_machine_str():
    return _str_from_cstr(target_platform_machine())


@c_abi_export("py_platform_release_str")
def py_platform_release_str():
    return py_str_new(cstr("0"), 1)


@c_abi_export("py_os_getcwd_str")
def py_os_getcwd_str():
    return _str_from_cstr(py_path_getcwd())


@c_abi_export("py_sys_path_list")
def py_sys_path_list():
    path0 = null()
    mode: int = py_program_mode()
    if mode == 3 or mode == 4:
        path0 = py_str_new(null(), 0)
    elif mode == 1:
        raw = py_program_argv(0)
        if not ptr_is_null(raw):
            resolved = py_path_realpath(raw)
            if not ptr_is_null(resolved):
                i: int = 0
                last_slash: int = -1
                while load_i8(resolved, i) != 0:
                    if load_i8(resolved, i) == 47:
                        last_slash = i
                    i += 1
                if last_slash >= 0:
                    length: int = last_slash
                    if last_slash == 0:
                        length = 1
                    path0 = py_str_new(resolved, length)
    if ptr_is_null(path0):
        path0 = py_os_getcwd_str()
    if ptr_is_null(path0):
        path0 = py_str_new(null(), 0)
    lst = py_list_new(0)
    py_list_append(lst, path0)
    py_decref(path0)
    return lst


@c_abi_export("py_os_access")
def py_os_access(path, mode: int) -> int:
    if ptr_is_null(path):
        return 0
    owned = py_obj_str(path)
    if ptr_is_null(owned):
        return 0
    raw = py_str_utf8(owned)
    ok: int = 0
    if not ptr_is_null(raw):
        if pcc_platform_access(raw, mode) == 0:
            ok = 1
    py_decref(owned)
    return ok


@c_abi_export("py_os_write")
def py_os_write(fd: int, data) -> int:
    if ptr_is_null(data):
        return -1
    ptr = null()
    length = 0
    curr = data
    while not ptr_is_null(curr):
        tag = _type_of(curr)
        if tag == PY_TYPE_STR:  # PY_TYPE_STR
            ptr = py_str_utf8(curr)
            length = py_str_byte_len(curr)
            break
        elif tag == PY_TYPE_BYTES:  # PY_TYPE_BYTES
            ptr = ptr_add(curr, 24)
            length = load_i64(curr, 16)
            break
        elif tag == PY_TYPE_BYTEARRAY:  # PY_TYPE_BYTEARRAY
            ptr = ptr_add(curr, 24)
            length = load_i64(curr, 16)
            break
        elif tag == PY_TYPE_MEMORYVIEW:  # PY_TYPE_MEMORYVIEW
            # base pointer is at offset 16
            curr = pcc_gc_load_ptr(curr, ptr_add(curr, 16))
        else:
            return -1

    if ptr_is_null(ptr) or length < 0:
        return -1
    return write_sys(fd, ptr, length)
