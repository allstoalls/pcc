"""pcc-Python port of py_os_path.c.

Narrow os.path runtime helpers: join, basename, and exists. Non-string
path objects are coerced through py_obj_str before reading UTF-8 bytes.
"""

__pcc_runtime_port__ = True

from pcc.py_runtime.py.py_abi_constants import (
    PY_TYPE_INT,
    PY_TYPE_LIST,
    PY_TYPE_STR,
    PY_TYPE_TUPLE,
)
from pcc.extern import (
    extern, c_abi_export, c_double, c_ptr, c_int32, c_int64, c_void,
)
from pcc.unsafe import (
    chmod_file,
    cstr,
    free,
    global_load_ptr,
    is_tagged_int,
    load_i8,
    load_i32,
    load_i64,
    load_ptr,
    malloc,
    memmove,
    null,
    ptr_add,
    ptr_is_null,
    realloc,
    store_i8,
    store_i64,
    rename_file,
    sync_file,
    unlinkat,
)


py_decref         = extern("py_decref",         (c_ptr,),         c_void)
py_obj_str        = extern("py_obj_str",        (c_ptr,),         c_ptr)
py_str_new        = extern("py_str_new",        (c_ptr, c_int64), c_ptr)
py_int_from_i64   = extern("py_int_from_i64",   (c_int64,),       c_ptr)
py_float_from_f64 = extern("py_float_from_f64", (c_double,),      c_ptr)
pcc_gc_load_ptr   = extern("pcc_gc_load_ptr",   (c_ptr, c_ptr),   c_ptr)
py_file_open      = extern("py_file_open",      (c_ptr, c_ptr),   c_ptr)
py_file_read_all  = extern("py_file_read_all",  (c_ptr,),         c_ptr)
py_file_close     = extern("py_file_close",     (c_ptr,),         c_void)
py_str_byte_len   = extern("py_str_byte_len",   (c_ptr,),         c_int64)
# Low-level platform-portable stat helpers (defined in C; see
# src/py_os_substrate.c). Avoids encoding struct stat layout — which
# differs between Linux and macOS — in pcc-Python.
py_path_stat_kind  = extern("py_path_stat_kind",  (c_ptr,),         c_int32)
py_path_stat_mtime = extern("py_path_stat_mtime", (c_ptr,),         c_double)
py_path_getcwd     = extern("py_path_getcwd",     (),               c_ptr)
py_path_realpath   = extern("py_path_realpath",   (c_ptr,),         c_ptr)
py_tuple_new       = extern("py_tuple_new",       (c_int64,),       c_ptr)
py_tuple_set_item  = extern("py_tuple_set_item",  (c_ptr, c_int64, c_ptr), c_void)
py_str_utf8        = extern("py_str_utf8",        (c_ptr,),         c_ptr)
py_exc_new         = extern("py_exc_new",         (c_int64, c_ptr), c_ptr)
py_raise_owned     = extern("py_raise_owned",     (c_ptr,),         c_void)
mkdir_sys          = extern("mkdir",              (c_ptr, c_int32), c_int32)
access             = extern("pcc_platform_access", (c_ptr, c_int64), c_int64)
getenv             = extern("pcc_platform_getenv", (c_ptr,), c_ptr)


def _type_of(obj) -> int:
    if is_tagged_int(obj):
        return PY_TYPE_INT       # PY_TYPE_INT
    return load_i32(obj, 8)


def _coerce_path_str(o):
    if ptr_is_null(o) != 0:
        return null(), null()
    if _type_of(o) == PY_TYPE_STR:           # PY_TYPE_STR
        return o, null()
    owned = py_obj_str(o)
    return owned, owned


def _path_seq_len(parts) -> int:
    if ptr_is_null(parts) != 0:
        return -1
    if is_tagged_int(parts):
        return -1
    tag: int = _type_of(parts)
    if tag == PY_TYPE_LIST:                  # PY_TYPE_LIST
        return load_i64(parts, 16)
    if tag == PY_TYPE_TUPLE:                  # PY_TYPE_TUPLE
        return load_i64(parts, 16)
    return -1


def _path_seq_borrow(parts, i: int):
    if ptr_is_null(parts) != 0:
        return null()
    if is_tagged_int(parts):
        return null()
    tag: int = _type_of(parts)
    if tag == PY_TYPE_LIST:                  # PY_TYPE_LIST
        items = load_ptr(parts, 32)
        return pcc_gc_load_ptr(parts, ptr_add(items, i * 8))
    if tag == PY_TYPE_TUPLE:                  # PY_TYPE_TUPLE
        return pcc_gc_load_ptr(parts, ptr_add(parts, 24 + i * 8))
    return null()


@c_abi_export("py_os_makedirs")
def py_os_makedirs(path, mode: int, exist_ok: int):
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        py_raise_owned(py_exc_new(3, cstr("path must be string-like")))
        return null()

    raw = py_str_utf8(item)
    raw_len: int = py_str_byte_len(item)
    if ptr_is_null(raw) != 0 or raw_len <= 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        py_raise_owned(py_exc_new(14, cstr("cannot create empty path")))
        return null()

    buf = malloc(raw_len + 1)
    if ptr_is_null(buf) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        py_raise_owned(py_exc_new(14, cstr("could not allocate path")))
        return null()
    memmove(buf, raw, raw_len)
    store_i8(buf, raw_len, 0)

    end: int = raw_len
    while end > 1 and load_i8(buf, end - 1) == 47:
        end = end - 1
    store_i8(buf, end, 0)

    i: int = 1
    while i <= end:
        if i == end or load_i8(buf, i) == 47:
            saved: int = load_i8(buf, i)
            store_i8(buf, i, 0)
            component_mode: int = mode if i == end else 511
            if mkdir_sys(buf, component_mode) != 0:
                kind: int = py_path_stat_kind(buf)
                if kind != 2 or (i == end and exist_ok == 0):
                    store_i8(buf, i, saved)
                    free(buf)
                    if ptr_is_null(owned) == 0:
                        py_decref(owned)
                    py_raise_owned(
                        py_exc_new(14, cstr("could not create directory"))
                    )
                    return null()
            store_i8(buf, i, saved)
        i = i + 1

    free(buf)
    if ptr_is_null(owned) == 0:
        py_decref(owned)
    return global_load_ptr("py_None")


@c_abi_export("py_os_unlink")
def py_os_unlink(path):
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        py_decref(owned)
        py_raise_owned(py_exc_new(3, cstr("path must be string-like")))
        return null()
    result: int = unlinkat(py_str_utf8(item), 0)
    py_decref(owned)
    if result != 0:
        py_raise_owned(py_exc_new(14, cstr("could not unlink path")))
        return null()
    return global_load_ptr("py_None")


@c_abi_export("py_os_rmdir")
def py_os_rmdir(path):
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        py_decref(owned)
        py_raise_owned(py_exc_new(3, cstr("path must be string-like")))
        return null()
    result: int = unlinkat(py_str_utf8(item), 1)
    py_decref(owned)
    if result != 0:
        py_raise_owned(py_exc_new(14, cstr("could not remove directory")))
        return null()
    return global_load_ptr("py_None")


@c_abi_export("py_os_replace")
def py_os_replace(source, destination):
    source_item, source_owned = _coerce_path_str(source)
    destination_item, destination_owned = _coerce_path_str(destination)
    if ptr_is_null(source_item) != 0 or ptr_is_null(destination_item) != 0:
        py_decref(source_owned)
        py_decref(destination_owned)
        py_raise_owned(py_exc_new(3, cstr("paths must be string-like")))
        return null()
    result: int = rename_file(
        py_str_utf8(source_item), py_str_utf8(destination_item)
    )
    py_decref(source_owned)
    py_decref(destination_owned)
    if result != 0:
        py_raise_owned(py_exc_new(14, cstr("could not replace path")))
        return null()
    return global_load_ptr("py_None")


@c_abi_export("py_os_chmod")
def py_os_chmod(path, mode: int):
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        py_decref(owned)
        py_raise_owned(py_exc_new(3, cstr("path must be string-like")))
        return null()
    result: int = chmod_file(py_str_utf8(item), mode)
    py_decref(owned)
    if result != 0:
        py_raise_owned(py_exc_new(14, cstr("could not change path mode")))
        return null()
    return global_load_ptr("py_None")


@c_abi_export("py_os_fsync")
def py_os_fsync(fd: int):
    if sync_file(fd) != 0:
        py_raise_owned(py_exc_new(14, cstr("could not synchronize file")))
        return null()
    return global_load_ptr("py_None")


@c_abi_export("py_os_path_join")
def py_os_path_join(parts):
    n: int = _path_seq_len(parts)
    if n < 0:
        return null()
    if n == 0:
        return py_str_new(null(), 0)

    # First pass: validate/coerce path parts, find the last absolute
    # component, and compute final byte length. Do not shuttle raw
    # char* buffers through Python tuples; tuple assignment would treat
    # them as PyObject* and corrupt memory through refcounting.
    start_idx: int = 0
    total: int = 0
    last_char: int = 0
    i: int = 0
    while i < n:
        item, owned = _coerce_path_str(_path_seq_borrow(parts, i))
        if ptr_is_null(item) != 0:
            if ptr_is_null(owned) == 0:
                py_decref(owned)
            return null()

        part = ptr_add(item, 40)
        part_len: int = load_i64(item, 16)

        if part_len > 0 and load_i8(part, 0) == 47:     # '/'
            if i > 0:
                start_idx = i
                total = 0
        if i > start_idx:
            if total > 0 and last_char != 47:
                total = total + 1
                last_char = 47
        total = total + part_len
        if part_len > 0:
            last_char = load_i8(part, part_len - 1)
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        i = i + 1

    out = py_str_new(null(), total)
    if ptr_is_null(out) != 0:
        return null()
    dst = ptr_add(out, 40)

    # Second pass: copy bytes into the final PyStrObject.
    off: int = 0
    j: int = start_idx
    while j < n:
        item2, owned2 = _coerce_path_str(_path_seq_borrow(parts, j))
        if ptr_is_null(item2) != 0:
            if ptr_is_null(owned2) == 0:
                py_decref(owned2)
            return null()
        part2 = ptr_add(item2, 40)
        part2_len: int = load_i64(item2, 16)
        if j > start_idx:
            if off > 0 and load_i8(dst, off - 1) != 47:
                store_i8(dst, off, 47)
                off = off + 1
        if part2_len > 0:
            memmove(ptr_add(dst, off), part2, part2_len)
            off = off + part2_len
        if ptr_is_null(owned2) == 0:
            py_decref(owned2)
        j = j + 1
    return out


@c_abi_export("py_os_path_dirname")
def py_os_path_dirname(path):
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return null()

    data = ptr_add(item, 40)
    n: int = load_i64(item, 16)

    last: int = -1
    i: int = 0
    while i < n:
        if load_i8(data, i) == 47:
            last = i
        i = i + 1

    head_len: int = last + 1
    if head_len == 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return py_str_new(null(), 0)

    all_slash: int = 1
    j: int = 0
    while j < head_len:
        if load_i8(data, j) != 47:
            all_slash = 0
        j = j + 1

    out_len: int = head_len
    if all_slash == 0:
        while out_len > 0 and load_i8(data, out_len - 1) == 47:
            out_len = out_len - 1

    out = py_str_new(data, out_len)
    if ptr_is_null(owned) == 0:
        py_decref(owned)
    return out


@c_abi_export("py_os_path_basename")
def py_os_path_basename(path):
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return null()

    data = ptr_add(item, 40)
    end: int = load_i64(item, 16)
    while end > 0 and load_i8(data, end - 1) == 47:
        end = end - 1
    if end == 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return py_str_new(null(), 0)

    start: int = end
    while start > 0 and load_i8(data, start - 1) != 47:
        start = start - 1
    out = py_str_new(ptr_add(data, start), end - start)
    if ptr_is_null(owned) == 0:
        py_decref(owned)
    return out


@c_abi_export("py_os_path_split")
def py_os_path_split(path):
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return null()

    data = ptr_add(item, 40)
    n: int = load_i64(item, 16)

    split_at: int = 0
    i: int = 0
    while i < n:
        if load_i8(data, i) == 47:
            split_at = i + 1
        i = i + 1

    head_len: int = split_at
    if head_len > 0:
        all_slash: int = 1
        j: int = 0
        while j < head_len:
            if load_i8(data, j) != 47:
                all_slash = 0
            j = j + 1
        if all_slash == 0:
            while head_len > 0 and load_i8(data, head_len - 1) == 47:
                head_len = head_len - 1

    head = py_str_new(data, head_len)
    tail = py_str_new(ptr_add(data, split_at), n - split_at)
    out = py_tuple_new(2)
    if ptr_is_null(out) == 0:
        py_tuple_set_item(out, 0, head)
        py_tuple_set_item(out, 1, tail)
    else:
        py_decref(head)
        py_decref(tail)

    if ptr_is_null(owned) == 0:
        py_decref(owned)
    return out


@c_abi_export("py_os_path_isfile")
def py_os_path_isfile(path) -> int:
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return 0
    raw = ptr_add(item, 40)
    kind: int = py_path_stat_kind(raw)
    if ptr_is_null(owned) == 0:
        py_decref(owned)
    if kind == 1:
        return 1
    return 0


@c_abi_export("py_os_path_isabs")
def py_os_path_isabs(path) -> int:
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return 0
    data = ptr_add(item, 40)
    n: int = load_i64(item, 16)
    ok: int = 0
    if n > 0 and load_i8(data, 0) == 47:
        ok = 1
    if ptr_is_null(owned) == 0:
        py_decref(owned)
    return ok


@c_abi_export("py_os_path_isdir")
def py_os_path_isdir(path) -> int:
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return 0
    raw = ptr_add(item, 40)
    kind: int = py_path_stat_kind(raw)
    if ptr_is_null(owned) == 0:
        py_decref(owned)
    if kind == 2:
        return 1
    return 0


@c_abi_export("py_os_path_abspath")
def py_os_path_abspath(path):
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return null()
    data = ptr_add(item, 40)
    in_len: int = load_i64(item, 16)

    # Already absolute → copy unchanged.
    if in_len > 0:
        if load_i8(data, 0) == 47:
            out = py_str_new(data, in_len)
            if ptr_is_null(owned) == 0:
                py_decref(owned)
            return out

    cwd_ptr = py_path_getcwd()
    if ptr_is_null(cwd_ptr) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return null()
    cwd_len: int = 0
    while load_i8(cwd_ptr, cwd_len) != 0:
        cwd_len = cwd_len + 1

    if in_len == 0:
        out = py_str_new(cwd_ptr, cwd_len)
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return out

    total: int = cwd_len + 1 + in_len
    out = py_str_new(null(), total)
    if ptr_is_null(out) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return null()
    dst = ptr_add(out, 40)
    memmove(dst, cwd_ptr, cwd_len)
    store_i8(dst, cwd_len, 47)
    memmove(ptr_add(dst, cwd_len + 1), data, in_len)
    if ptr_is_null(owned) == 0:
        py_decref(owned)
    return out


@c_abi_export("py_os_path_expanduser")
def py_os_path_expanduser(path):
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return null()
    data = ptr_add(item, 40)
    n: int = load_i64(item, 16)

    # Only a bare "~" or "~/..." prefix expands to $HOME. A "~user" prefix
    # (no '/' right after '~') and a path without a leading '~' (126) are
    # returned unchanged, matching CPython posixpath.expanduser.
    is_home: int = 0
    if n >= 1:
        if load_i8(data, 0) == 126:
            if n == 1:
                is_home = 1
            if n > 1:
                if load_i8(data, 1) == 47:
                    is_home = 1
    home_ptr = null()
    if is_home == 1:
        home_ptr = getenv(cstr("HOME"))
    if is_home == 0 or ptr_is_null(home_ptr) != 0:
        out = py_str_new(data, n)
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return out

    # userhome = home.rstrip('/'); result = (userhome + path[1:]) or "/".
    home_len: int = 0
    while load_i8(home_ptr, home_len) != 0:
        home_len = home_len + 1
    while home_len > 0 and load_i8(home_ptr, home_len - 1) == 47:
        home_len = home_len - 1
    rest_len: int = n - 1
    total: int = home_len + rest_len
    if total == 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return py_str_new(cstr("/"), 1)
    out = py_str_new(null(), total)
    if ptr_is_null(out) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return null()
    dst = ptr_add(out, 40)
    memmove(dst, home_ptr, home_len)
    memmove(ptr_add(dst, home_len), ptr_add(data, 1), rest_len)
    if ptr_is_null(owned) == 0:
        py_decref(owned)
    return out


@c_abi_export("py_os_path_realpath")
def py_os_path_realpath(path):
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return null()
    raw = ptr_add(item, 40)
    resolved = py_path_realpath(raw)
    if ptr_is_null(resolved) == 0:
        rlen: int = 0
        while load_i8(resolved, rlen) != 0:
            rlen = rlen + 1
        out = py_str_new(resolved, rlen)
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return out
    # realpath(3) failed (path or a component does not exist): fall back to
    # lexical normpath(abspath(path)) — absolute with "." / ".." collapsed.
    if ptr_is_null(owned) == 0:
        py_decref(owned)
    abs_path = py_os_path_abspath(path)
    if ptr_is_null(abs_path) != 0:
        return null()
    out = py_os_path_normpath(abs_path)
    py_decref(abs_path)
    return out


@c_abi_export("py_os_path_getmtime")
def py_os_path_getmtime(path):
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return null()
    raw = ptr_add(item, 40)
    t: float = py_path_stat_mtime(raw)
    if ptr_is_null(owned) == 0:
        py_decref(owned)
    return py_float_from_f64(t)


@c_abi_export("py_os_path_getsize")
def py_os_path_getsize(path):
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return null()
    mode = py_str_new(cstr("rb"), 2)
    if ptr_is_null(mode) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return py_int_from_i64(-1)
    f = py_file_open(item, mode)
    py_decref(mode)
    if ptr_is_null(f) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return py_int_from_i64(-1)
    data = py_file_read_all(f)
    py_file_close(f)
    py_decref(f)
    if ptr_is_null(data) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return py_int_from_i64(-1)
    size: int = py_str_byte_len(data)
    py_decref(data)
    if ptr_is_null(owned) == 0:
        py_decref(owned)
    return py_int_from_i64(size)


@c_abi_export("py_os_path_splitext")
def py_os_path_splitext(path):
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return null()
    data = ptr_add(item, 40)
    n: int = load_i64(item, 16)

    slash: int = -1
    dot: int = -1
    i: int = 0
    while i < n:
        b: int = load_i8(data, i)
        if b == 47:        # '/'
            slash = i
            dot = -1
        elif b == 46:      # '.'
            dot = i
        i = i + 1

    if dot <= slash + 1:
        base = py_str_new(data, n)
        ext = py_str_new(null(), 0)
    else:
        base = py_str_new(data, dot)
        ext = py_str_new(ptr_add(data, dot), n - dot)

    out = py_tuple_new(2)
    if ptr_is_null(out) == 0:
        py_tuple_set_item(out, 0, base)
        py_tuple_set_item(out, 1, ext)
    else:
        py_decref(base)
        py_decref(ext)

    if ptr_is_null(owned) == 0:
        py_decref(owned)
    return out


@c_abi_export("py_os_path_normcase")
def py_os_path_normcase(path):
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return null()
    data = ptr_add(item, 40)
    n: int = load_i64(item, 16)
    out = py_str_new(data, n)
    if ptr_is_null(owned) == 0:
        py_decref(owned)
    return out


def _path_component_is_dotdot(data, start: int, n: int) -> int:
    if n == 2:
        if load_i8(data, start) == 46 and load_i8(data, start + 1) == 46:
            return 1
    return 0


@c_abi_export("py_os_path_normpath")
def py_os_path_normpath(path):
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return null()
    data = ptr_add(item, 40)
    n: int = load_i64(item, 16)
    work_cap: int = n + 2
    if work_cap < 2:
        work_cap = 2
    work = malloc(work_cap)
    starts = malloc((n + 1) * 8)
    lens = malloc((n + 1) * 8)
    if ptr_is_null(work) != 0 or ptr_is_null(starts) != 0 or ptr_is_null(lens) != 0:
        if ptr_is_null(work) == 0:
            free(work)
        if ptr_is_null(starts) == 0:
            free(starts)
        if ptr_is_null(lens) == 0:
            free(lens)
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return null()

    initial: int = 0
    if n > 0 and load_i8(data, 0) == 47:
        initial = 1
        if n > 1 and load_i8(data, 1) == 47:
            if n == 2 or load_i8(data, 2) != 47:
                initial = 2

    out_len: int = 0
    k: int = 0
    while k < initial:
        store_i8(work, out_len, 47)
        out_len = out_len + 1
        k = k + 1
    base_len: int = initial
    comps: int = 0
    i: int = initial
    while i < n:
        while i < n and load_i8(data, i) == 47:
            i = i + 1
        start: int = i
        while i < n and load_i8(data, i) != 47:
            i = i + 1
        part_len: int = i - start
        if part_len == 0:
            continue
        if part_len == 1 and load_i8(data, start) == 46:
            continue
        is_dotdot: int = _path_component_is_dotdot(data, start, part_len)
        if is_dotdot != 0:
            if comps > 0:
                last_start: int = load_i64(starts, (comps - 1) * 8)
                last_len: int = load_i64(lens, (comps - 1) * 8)
                last_is_dotdot: int = _path_component_is_dotdot(
                    work,
                    last_start,
                    last_len,
                )
                if last_is_dotdot == 0:
                    comps = comps - 1
                    out_len = last_start
                    if out_len > base_len and load_i8(work, out_len - 1) == 47:
                        out_len = out_len - 1
                    continue
            if initial > 0:
                continue
        if out_len > base_len and load_i8(work, out_len - 1) != 47:
            store_i8(work, out_len, 47)
            out_len = out_len + 1
        store_i64(starts, comps * 8, out_len)
        store_i64(lens, comps * 8, part_len)
        memmove(ptr_add(work, out_len), ptr_add(data, start), part_len)
        out_len = out_len + part_len
        comps = comps + 1

    if out_len == 0:
        out = py_str_new(cstr("."), 1)
    else:
        out = py_str_new(work, out_len)
    free(work)
    free(starts)
    free(lens)
    if ptr_is_null(owned) == 0:
        py_decref(owned)
    return out


@c_abi_export("py_os_path_splitdrive")
def py_os_path_splitdrive(path):
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return null()
    data = ptr_add(item, 40)
    n: int = load_i64(item, 16)
    drive = py_str_new(null(), 0)
    tail = py_str_new(data, n)
    out = py_tuple_new(2)
    if ptr_is_null(out) == 0:
        py_tuple_set_item(out, 0, drive)
        py_tuple_set_item(out, 1, tail)
    else:
        py_decref(drive)
        py_decref(tail)
    if ptr_is_null(owned) == 0:
        py_decref(owned)
    return out


@c_abi_export("py_os_path_commonprefix")
def py_os_path_commonprefix(paths):
    n: int = _path_seq_len(paths)
    if n < 0:
        return null()
    if n == 0:
        return py_str_new(null(), 0)

    first, first_owned = _coerce_path_str(_path_seq_borrow(paths, 0))
    if ptr_is_null(first) != 0:
        if ptr_is_null(first_owned) == 0:
            py_decref(first_owned)
        return null()
    first_data = ptr_add(first, 40)
    common_len: int = load_i64(first, 16)

    i: int = 1
    while i < n:
        item, owned = _coerce_path_str(_path_seq_borrow(paths, i))
        if ptr_is_null(item) != 0:
            if ptr_is_null(owned) == 0:
                py_decref(owned)
            if ptr_is_null(first_owned) == 0:
                py_decref(first_owned)
            return null()
        data = ptr_add(item, 40)
        item_len: int = load_i64(item, 16)
        limit: int = common_len
        if item_len < limit:
            limit = item_len
        j: int = 0
        while j < limit and load_i8(first_data, j) == load_i8(data, j):
            j = j + 1
        common_len = j
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        if common_len == 0:
            break
        i = i + 1

    out = py_str_new(first_data, common_len)
    if ptr_is_null(first_owned) == 0:
        py_decref(first_owned)
    return out


@c_abi_export("py_os_path_commonpath")
def py_os_path_commonpath(paths):
    """Return the shared POSIX component prefix of a list or tuple."""
    n: int = _path_seq_len(paths)
    if n <= 0:
        return py_str_new(null(), 0)

    first, first_owned = _coerce_path_str(_path_seq_borrow(paths, 0))
    if ptr_is_null(first) != 0:
        if ptr_is_null(first_owned) == 0:
            py_decref(first_owned)
        return null()
    first_data = ptr_add(first, 40)
    first_len: int = load_i64(first, 16)
    prefix_len: int = first_len

    i: int = 1
    while i < n:
        item, owned = _coerce_path_str(_path_seq_borrow(paths, i))
        if ptr_is_null(item) != 0:
            if ptr_is_null(owned) == 0:
                py_decref(owned)
            if ptr_is_null(first_owned) == 0:
                py_decref(first_owned)
            return null()
        data = ptr_add(item, 40)
        item_len: int = load_i64(item, 16)
        limit: int = prefix_len
        if item_len < limit:
            limit = item_len
        j: int = 0
        while j < limit and load_i8(first_data, j) == load_i8(data, j):
            j = j + 1
        prefix_len = j
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        if prefix_len == 0:
            break
        i = i + 1

    if prefix_len > 0:
        diverged: int = 0
        k: int = 1
        while k < n:
            current, current_owned = _coerce_path_str(_path_seq_borrow(paths, k))
            if ptr_is_null(current) != 0:
                if ptr_is_null(current_owned) == 0:
                    py_decref(current_owned)
                if ptr_is_null(first_owned) == 0:
                    py_decref(first_owned)
                return null()
            current_len: int = load_i64(current, 16)
            if current_len > prefix_len:
                if load_i8(ptr_add(current, 40), prefix_len) != 47:
                    diverged = 1
            if ptr_is_null(current_owned) == 0:
                py_decref(current_owned)
            k = k + 1
        if prefix_len < first_len:
            if load_i8(first_data, prefix_len) != 47:
                diverged = 1
        if diverged != 0:
            while prefix_len > 0 and load_i8(first_data, prefix_len - 1) != 47:
                prefix_len = prefix_len - 1
        while prefix_len > 1 and load_i8(first_data, prefix_len - 1) == 47:
            prefix_len = prefix_len - 1

    out = py_str_new(first_data, prefix_len)
    if ptr_is_null(first_owned) == 0:
        py_decref(first_owned)
    return out


def _path_env_name_char(value: int) -> int:
    if value == 95:  # '_'
        return 1
    if value >= 65 and value <= 90:
        return 1
    if value >= 97 and value <= 122:
        return 1
    if value >= 48 and value <= 57:
        return 1
    return 0


def _path_cstr_len(value) -> int:
    length: int = 0
    while load_i8(value, length) != 0:
        length = length + 1
    return length


@c_abi_export("py_os_path_expandvars")
def py_os_path_expandvars(path):
    """Expand POSIX ``$name`` and ``${name}`` environment references."""
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return null()
    data = ptr_add(item, 40)
    n: int = load_i64(item, 16)

    has_dollar: int = 0
    probe: int = 0
    while probe < n:
        if load_i8(data, probe) == 36:  # '$'
            has_dollar = 1
            break
        probe = probe + 1
    if has_dollar == 0:
        out = py_str_new(data, n)
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return out

    cap: int = n + 16
    if cap < 16:
        cap = 16
    buf = malloc(cap)
    if ptr_is_null(buf) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return null()

    out_len: int = 0
    i: int = 0
    failed: int = 0
    while i < n:
        seg = ptr_add(data, i)
        seg_len: int = 1
        advance: int = 1
        current: int = load_i8(data, i)
        if current == 36 and i + 1 < n:
            next_value: int = load_i8(data, i + 1)
            if next_value == 123:  # '{'
                end: int = i + 2
                while end < n and load_i8(data, end) != 125:  # '}'
                    end = end + 1
                if end < n:
                    name_len: int = end - (i + 2)
                    name = malloc(name_len + 1)
                    if ptr_is_null(name) != 0:
                        failed = 1
                    else:
                        memmove(name, ptr_add(data, i + 2), name_len)
                        store_i8(name, name_len, 0)
                        value = getenv(name)
                        free(name)
                        if ptr_is_null(value) == 0:
                            seg = value
                            seg_len = _path_cstr_len(value)
                        else:
                            seg = ptr_add(data, i)
                            seg_len = end - i + 1
                        advance = end - i + 1
            elif _path_env_name_char(next_value) != 0:
                end2: int = i + 1
                while end2 < n and _path_env_name_char(load_i8(data, end2)) != 0:
                    end2 = end2 + 1
                name_len2: int = end2 - (i + 1)
                name2 = malloc(name_len2 + 1)
                if ptr_is_null(name2) != 0:
                    failed = 1
                else:
                    memmove(name2, ptr_add(data, i + 1), name_len2)
                    store_i8(name2, name_len2, 0)
                    value2 = getenv(name2)
                    free(name2)
                    if ptr_is_null(value2) == 0:
                        seg = value2
                        seg_len = _path_cstr_len(value2)
                    else:
                        seg = ptr_add(data, i)
                        seg_len = end2 - i
                    advance = end2 - i

        if failed != 0:
            break
        while out_len + seg_len + 1 > cap:
            new_cap: int = cap * 2
            grown = realloc(buf, new_cap)
            if ptr_is_null(grown) != 0:
                failed = 1
                break
            buf = grown
            cap = new_cap
        if failed != 0:
            break
        memmove(ptr_add(buf, out_len), seg, seg_len)
        out_len = out_len + seg_len
        i = i + advance

    if failed != 0:
        free(buf)
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return null()
    out2 = py_str_new(buf, out_len)
    free(buf)
    if ptr_is_null(owned) == 0:
        py_decref(owned)
    return out2


def _path_split_normalized_components(data, n: int, offsets, lengths) -> int:
    count: int = 0
    i: int = 0
    while i < n:
        while i < n and load_i8(data, i) == 47:
            i = i + 1
        if i >= n:
            break
        start: int = i
        while i < n and load_i8(data, i) != 47:
            i = i + 1
        part_len: int = i - start
        if part_len == 1 and load_i8(data, start) == 46:
            continue
        if part_len == 2:
            if load_i8(data, start) == 46 and load_i8(data, start + 1) == 46:
                if count > 0:
                    count = count - 1
                continue
        store_i64(offsets, count * 8, start)
        store_i64(lengths, count * 8, part_len)
        count = count + 1
    return count


def _path_bytes_equal(left, right, length: int) -> int:
    i: int = 0
    while i < length:
        if load_i8(left, i) != load_i8(right, i):
            return 0
        i = i + 1
    return 1


@c_abi_export("py_os_path_relpath")
def py_os_path_relpath(path, start):
    """Return the normalized POSIX component difference from start to path."""
    path_item, path_owned = _coerce_path_str(path)
    start_item, start_owned = _coerce_path_str(start)
    if ptr_is_null(path_item) != 0 or ptr_is_null(start_item) != 0:
        if ptr_is_null(path_owned) == 0:
            py_decref(path_owned)
        if ptr_is_null(start_owned) == 0:
            py_decref(start_owned)
        return null()

    path_data = ptr_add(path_item, 40)
    path_len: int = load_i64(path_item, 16)
    start_data = ptr_add(start_item, 40)
    start_len: int = load_i64(start_item, 16)
    path_offsets = malloc((path_len + 1) * 8)
    path_lengths = malloc((path_len + 1) * 8)
    start_offsets = malloc((start_len + 1) * 8)
    start_lengths = malloc((start_len + 1) * 8)
    if (
        ptr_is_null(path_offsets) != 0
        or ptr_is_null(path_lengths) != 0
        or ptr_is_null(start_offsets) != 0
        or ptr_is_null(start_lengths) != 0
    ):
        free(path_offsets)
        free(path_lengths)
        free(start_offsets)
        free(start_lengths)
        if ptr_is_null(path_owned) == 0:
            py_decref(path_owned)
        if ptr_is_null(start_owned) == 0:
            py_decref(start_owned)
        return null()

    path_count: int = _path_split_normalized_components(
        path_data, path_len, path_offsets, path_lengths
    )
    start_count: int = _path_split_normalized_components(
        start_data, start_len, start_offsets, start_lengths
    )
    common: int = 0
    while common < path_count and common < start_count:
        component_len: int = load_i64(path_lengths, common * 8)
        if component_len != load_i64(start_lengths, common * 8):
            break
        path_component = ptr_add(
            path_data, load_i64(path_offsets, common * 8)
        )
        start_component = ptr_add(
            start_data, load_i64(start_offsets, common * 8)
        )
        if _path_bytes_equal(path_component, start_component, component_len) == 0:
            break
        common = common + 1

    up: int = start_count - common
    tail: int = path_count - common
    total: int = up + tail
    if total == 0:
        free(path_offsets)
        free(path_lengths)
        free(start_offsets)
        free(start_lengths)
        if ptr_is_null(path_owned) == 0:
            py_decref(path_owned)
        if ptr_is_null(start_owned) == 0:
            py_decref(start_owned)
        return py_str_new(cstr("."), 1)

    output_len: int = 2 * up + total - 1
    j: int = common
    while j < path_count:
        output_len = output_len + load_i64(path_lengths, j * 8)
        j = j + 1
    out = py_str_new(null(), output_len)
    if ptr_is_null(out) != 0:
        free(path_offsets)
        free(path_lengths)
        free(start_offsets)
        free(start_lengths)
        if ptr_is_null(path_owned) == 0:
            py_decref(path_owned)
        if ptr_is_null(start_owned) == 0:
            py_decref(start_owned)
        return null()

    destination = ptr_add(out, 40)
    pos: int = 0
    written: int = 0
    k: int = 0
    while k < up:
        if written > 0:
            store_i8(destination, pos, 47)
            pos = pos + 1
        store_i8(destination, pos, 46)
        store_i8(destination, pos + 1, 46)
        pos = pos + 2
        written = written + 1
        k = k + 1
    j2: int = common
    while j2 < path_count:
        if written > 0:
            store_i8(destination, pos, 47)
            pos = pos + 1
        part_len2: int = load_i64(path_lengths, j2 * 8)
        part_offset2: int = load_i64(path_offsets, j2 * 8)
        memmove(ptr_add(destination, pos), ptr_add(path_data, part_offset2), part_len2)
        pos = pos + part_len2
        written = written + 1
        j2 = j2 + 1

    free(path_offsets)
    free(path_lengths)
    free(start_offsets)
    free(start_lengths)
    if ptr_is_null(path_owned) == 0:
        py_decref(path_owned)
    if ptr_is_null(start_owned) == 0:
        py_decref(start_owned)
    return out


@c_abi_export("py_os_path_exists")
def py_os_path_exists(path) -> int:
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return 0

    raw = ptr_add(item, 40)
    ok: int = 0
    if access(raw, 0) == 0:        # F_OK
        ok = 1
    if ptr_is_null(owned) == 0:
        py_decref(owned)
    return ok
