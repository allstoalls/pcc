"""Ordinary ``os`` and ``os.path`` providers over the owned runtime ABI.

The same callables serve managed modules, saved imports and dynamic receivers.
Native calls use the existing target-aware runtime helpers. Interpreted source
use follows the host ``os`` module, as the ``time`` provider does.
"""

from __future__ import annotations

import os as _native_os
import sys as _native_sys

from pcc.unsafe import (
    load_i32, ptr_is_null, stack_alloc, store_i32, strlen, sync_file,
)

from pcc.extern import (
    c_int, c_int64, c_obj, c_ptr, c_rawptr, c_str, c_void, extern,
)

_getenv = extern("getenv", (c_str,), c_rawptr)
_setenv = extern("setenv", (c_str, c_str, c_int), c_int)
_getcwd = extern("getcwd", (c_str, c_int64), c_rawptr)
_access = extern("pcc_platform_access", (c_str, c_int64), c_int64)
_getpid = extern("pcc_platform_getpid", (), c_int64)
_fd_integer = extern("py_int_to_i64", (c_obj, c_rawptr), c_int64)
_errno_message = extern("pcc_errno_message_into", (c_int, c_rawptr, c_int64), c_int)
_new_text = extern("py_str_new", (c_rawptr, c_int64), c_obj)
_thread_safepoint = extern("pcc_thread_safepoint", (), c_void)
_error_pending = extern("py_err_occurred", (), c_int64)
_walk_scan = extern("py_os_walk_scan", (c_obj, c_obj, c_obj), c_obj)
_walk_prefix = extern("py_os_walk_prefix", (c_obj,), c_obj)
_path_islink_result = extern("py_os_path_islink_result", (c_obj,), c_obj)
_environ_get = extern("py_os_getenv", (c_obj, c_obj), c_obj)
_environ_getitem = extern("py_os_environ_getitem", (c_obj,), c_obj)
_environ_setitem = extern("py_os_environ_setitem", (c_obj, c_obj), c_obj)
_environ_unset = extern("py_os_unsetenv", (c_obj,), c_obj)
_environ_contains = extern("py_os_environ_contains", (c_obj,), c_int)
_environ_snapshot = extern("py_os_environ_snapshot", (), c_obj)
_module_attribute = extern("py_module_attr_get", (c_str, c_str), c_obj)

# c_obj results transfer an independent owner (or the immortal None). Keep
# each result in an ordinary local until the pending exception is checked.
_os_platform = extern("py_sys_platform_str", (), c_obj)
_os_getcwd = extern("py_os_getcwd_str", (), c_obj)
_os_cpu_count = extern("py_os_cpu_count", (), c_obj)
_os_uname = extern("py_os_uname", (), c_obj)
_os_listdir = extern("py_os_listdir", (c_obj,), c_obj)
_os_makedirs = extern("py_os_makedirs", (c_obj, c_int64, c_int), c_obj)
_os_kill = extern("py_os_kill", (c_obj, c_obj), c_obj)
_os_access = extern("py_os_access", (c_obj, c_int), c_int)
_os_chmod = extern("py_os_chmod", (c_obj, c_int64), c_obj)
_os_write = extern("py_os_write", (c_int, c_obj), c_int)
_os_unlink = extern("py_os_unlink", (c_obj,), c_obj)
_os_rmdir = extern("py_os_rmdir", (c_obj,), c_obj)
_os_replace = extern("py_os_replace", (c_obj, c_obj), c_obj)
_os_urandom = extern("py_os_urandom", (c_obj,), c_obj)
_os_putenv = extern("py_os_putenv", (c_obj, c_obj), c_obj)
_index_value = extern("py_obj_index", (c_obj,), c_obj)
_errno_get = extern("pcc_errno_get", (), c_int)
_http_download = extern("py_http_download_to_file", (c_obj, c_obj), c_int64)
_file_digest = extern("py_sha256_file_hex", (c_obj,), c_obj)
_file_digest_bounded = extern("py_sha256_file_hex_bounded", (c_obj, c_int64), c_obj)

_path_basename = extern("py_os_path_basename", (c_obj,), c_obj)
_path_dirname = extern("py_os_path_dirname", (c_obj,), c_obj)
_path_split = extern("py_os_path_split", (c_obj,), c_obj)
_path_isfile = extern("py_os_path_isfile", (c_obj,), c_int)
_path_isdir = extern("py_os_path_isdir", (c_obj,), c_int)
_path_getmtime = extern("py_os_path_getmtime", (c_obj,), c_obj)
_path_getsize = extern("py_os_path_getsize", (c_obj,), c_obj)
_path_abspath = extern("py_os_path_abspath", (c_obj,), c_obj)
_path_commonpath = extern("py_os_path_commonpath", (c_obj,), c_obj)
_path_commonprefix = extern("py_os_path_commonprefix", (c_obj,), c_obj)
_path_normcase = extern("py_os_path_normcase", (c_obj,), c_obj)
_path_splitdrive = extern("py_os_path_splitdrive", (c_obj,), c_obj)
_path_expanduser = extern("py_os_path_expanduser", (c_obj,), c_obj)
_path_expandvars = extern("py_os_path_expandvars", (c_obj,), c_obj)
_path_relpath = extern("py_os_path_relpath", (c_obj, c_obj), c_obj)
_path_realpath = extern("py_os_path_realpath", (c_obj,), c_obj)


def _target_platform_name():
    if _native_sys.implementation.name != "pcc":
        return _native_sys.platform
    result = _os_platform()
    if _error_pending():
        raise
    if ptr_is_null(result):
        raise MemoryError('owned target platform result allocation failed')
    return result


# Platform ABI choices describe the executing target; later user writes to
# sys.platform must not select a different kernel error convention.
_platform_name = _target_platform_name()


# POSIX file-access constants.
F_OK: int = 0
R_OK: int = 4
W_OK: int = 2
X_OK: int = 1

name: str = "nt" if _platform_name == "win32" else "posix"
sep: str = "\\" if _platform_name == "win32" else "/"
linesep: str = "\r\n" if _platform_name == "win32" else "\n"
altsep = "/" if _platform_name == "win32" else None
pathsep: str = ";" if _platform_name == "win32" else ":"
curdir: str = "."
pardir: str = ".."
extsep: str = "."
devnull: str = "nul" if _platform_name == "win32" else "/dev/null"


def getpid() -> int:
    # The portable owned platform ABI selects the target process primitive.
    # Keep this live on every call, including after a fork.
    return _getpid()


def fsync(fd):
    """Synchronize an integer descriptor or an object exposing fileno()."""
    if isinstance(fd, bool):
        import warnings
        warnings.warn(
            "bool is used as a file descriptor", RuntimeWarning, stacklevel=2,
        )
    descriptor = fd
    if not isinstance(descriptor, int):
        try:
            method = fd.fileno
        except AttributeError:
            raise TypeError("argument must be an int, or have a fileno() method.") from None
        descriptor = method()
        if not isinstance(descriptor, int):
            raise TypeError(type(fd).__name__ + ".fileno() must return an int, not "
                            + type(descriptor).__name__)
    # Convert the integer payload directly, without invoking __index__ or
    # overloaded comparisons. Validate before the platform's C-int narrowing.
    overflow = stack_alloc(4)
    store_i32(overflow, 0, 0)
    number = _fd_integer(descriptor, overflow)
    if load_i32(overflow, 0) or number < -2147483648 or number > 2147483647:
        raise OverflowError("Python int too large to convert to C int")
    if number < 0:
        raise ValueError("file descriptor cannot be a negative integer (" + str(number) + ")")
    # fd and descriptor remain ordinary rooted locals until this call exits;
    # a temporary file object must not close between fileno and the syscall.
    status = sync_file(number)
    while status == -4:
        _thread_safepoint()
        if _error_pending():
            raise
        status = sync_file(number)
    if status < 0:
        buffer = stack_alloc(256)
        result = _errno_message(-status, buffer, 256)
        if result < 0:
            raise OSError(-status, "file synchronization failed")
        message = _new_text(buffer, strlen(buffer))
        if message is None:
            raise MemoryError("file synchronization error message allocation failed")
        raise OSError(-status, message)
    return None


def getenv(key, default=None):
    # LOAD_GLOBAL must observe replacements and deletion in this provider's
    # registered namespace.  The object ABI transfers an independent owner;
    # a missing NULL is distinct from an explicit Python None binding.
    mapping = _module_attribute(__name__, "environ")
    if _error_pending():
        raise
    if ptr_is_null(mapping):
        raise NameError("name 'environ' is not defined")
    return mapping.get(key, default)


class _EnvironView:
    def __init__(self, mapping, kind):
        self._mapping = mapping
        self._kind = kind

    def __len__(self):
        return len(self._mapping)

    def __iter__(self):
        for key in self._mapping:
            if self._kind == 0:
                yield key
            elif self._kind == 1:
                yield self._mapping[key]
            else:
                yield key, self._mapping[key]

    def __contains__(self, value):
        if self._kind == 0:
            return value in self._mapping
        for candidate in self:
            if candidate == value:
                return True
        return False


class _Environ:
    """A live environment mapping backed by the owned platform helpers."""

    def __getitem__(self, key):
        value = _environ_getitem(key)
        if _error_pending():
            raise
        return value

    def __setitem__(self, key, value):
        _environ_setitem(key, value)
        if _error_pending():
            raise

    def __delitem__(self, key):
        previous = _environ_getitem(key)
        if _error_pending():
            raise
        _environ_unset(key)
        if _error_pending():
            raise

    def __contains__(self, key):
        found = _environ_contains(key)
        if _error_pending():
            raise
        return found != 0

    def __iter__(self):
        for key in self.copy():
            yield key

    def __len__(self):
        return len(self.copy())

    def get(self, key, default=None):
        value = _environ_get(key, default)
        if _error_pending():
            raise
        return value

    def copy(self):
        value = _environ_snapshot()
        if _error_pending():
            raise
        return value

    def keys(self):
        return _EnvironView(self, 0)

    def values(self):
        return _EnvironView(self, 1)

    def items(self):
        return _EnvironView(self, 2)

    def pop(self, key, *default):
        if len(default) > 1:
            raise TypeError("pop expected at most 2 arguments")
        try:
            value = self[key]
        except KeyError:
            if default:
                return default[0]
            raise
        del self[key]
        return value


environ = _Environ()


def exists(path: str) -> bool:
    """True if ``path`` exists on disk, via ``access(path, F_OK)``.

    The owned platform ABI follows symlinks and returns failure as a status,
    so a missing or inaccessible path is False rather than an exception.
    """
    return _access(path, F_OK) == 0


class PathLike:
    """Base protocol for objects that provide a filesystem path."""

    def __fspath__(self):
        raise NotImplementedError("PathLike subclasses must define __fspath__")


def fspath(path):
    """Return the filesystem representation of a path-like object."""
    if isinstance(path, (str, bytes)):
        return path
    # Special methods belong to the type, not an instance's attribute dict.
    # Catch only the missing-method lookup: AttributeError raised *inside*
    # __fspath__ is the provider's error and must reach the caller unchanged.
    try:
        path_repr = type(path).__fspath__
    except AttributeError:
        raise TypeError(
            "expected str, bytes or os.PathLike object, not " + type(path).__name__
        )
    result = path_repr(path)
    if not isinstance(result, (str, bytes)):
        raise TypeError(
            "expected " + type(path).__name__
            + ".__fspath__() to return str or bytes, not " + type(result).__name__
        )
    return result


def _native_path_text(value):
    # The legacy lexical/file helpers consume text, not repr(path). Convert
    # the protocol once, before entering helpers that may read their input
    # more than once. islink has its own complete bytes/PathLike boundary.
    result = fspath(value)
    if isinstance(result, bytes):
        raise NotImplementedError("owned OS/path text helper does not support bytes paths")
    return result


def _native_file_path(value):
    result = _native_path_text(value)
    if "\0" in result:
        raise ValueError("embedded null byte")
    return result


def _native_integer(value):
    result = _index_value(value)
    if _error_pending():
        raise
    if ptr_is_null(result):
        raise MemoryError('owned integer index result allocation failed')
    return result


def _native_c_int(value):
    integer = _native_integer(value)
    overflow = stack_alloc(4)
    store_i32(overflow, 0, 0)
    number = _fd_integer(integer, overflow)
    if _error_pending():
        raise
    if load_i32(overflow, 0) or number < -2147483648 or number > 2147483647:
        raise OverflowError("Python int too large to convert to C int")
    return number


def _os_error(number):
    buffer = stack_alloc(256)
    status = _errno_message(number, buffer, 256)
    if status < 0:
        raise OSError(number, "operating system call failed")
    message = _new_text(buffer, strlen(buffer))
    if _error_pending():
        raise
    raise OSError(number, message)


def getcwd():
    if _native_sys.implementation.name != "pcc":
        return _native_os.getcwd()
    result = _os_getcwd()
    if _error_pending():
        raise
    if ptr_is_null(result):
        raise RuntimeError("owned os.getcwd helper returned NULL without an exception")
    return result


def cpu_count():
    if _native_sys.implementation.name != "pcc":
        return _native_os.cpu_count()
    result = _os_cpu_count()
    if _error_pending():
        raise
    if ptr_is_null(result):
        raise MemoryError('owned os.cpu_count result allocation failed')
    return result if result > 0 else None


class uname_result(tuple):
    __slots__ = ()
    n_fields = 5
    n_sequence_fields = 5
    n_unnamed_fields = 0

    @property
    def sysname(self):
        return self[0]

    @property
    def nodename(self):
        return self[1]

    @property
    def release(self):
        return self[2]

    @property
    def version(self):
        return self[3]

    @property
    def machine(self):
        return self[4]


def uname():
    if _native_sys.implementation.name != "pcc":
        return _native_os.uname()
    result = _os_uname()
    if _error_pending():
        raise
    if ptr_is_null(result):
        raise MemoryError('owned os.uname result allocation failed')
    return uname_result(result)


def listdir(path="."):
    if _native_sys.implementation.name != "pcc":
        return _native_os.listdir(path)
    if path is None:
        path = "."
    if isinstance(path, int):
        raise NotImplementedError("owned os.listdir directory descriptors are not implemented")
    value = _native_file_path(path)
    result = _os_listdir(value)
    if _error_pending():
        raise
    if ptr_is_null(result):
        raise RuntimeError('owned os.listdir helper returned NULL without an exception')
    return result


def makedirs(name, mode=0o777, exist_ok=False):
    if _native_sys.implementation.name != "pcc":
        return _native_os.makedirs(name, mode, exist_ok)
    value = _native_file_path(name)
    permission = _native_c_int(mode)
    result = _os_makedirs(value, permission, bool(exist_ok))
    if _error_pending():
        raise
    return result


def kill(pid, signal, /):
    if _native_sys.implementation.name != "pcc":
        return _native_os.kill(pid, signal)
    # The owned helper implements __index__, C-width checking and the
    # ProcessLookupError/PermissionError distinctions for both live operands.
    result = _os_kill(pid, signal)
    if _error_pending():
        raise
    return result


def access(path, mode, *, dir_fd=None, effective_ids=False, follow_symlinks=True):
    if _native_sys.implementation.name != "pcc":
        return _native_os.access(path, mode, dir_fd=dir_fd,
                                 effective_ids=effective_ids,
                                 follow_symlinks=follow_symlinks)
    if dir_fd is not None or effective_ids or not follow_symlinks:
        raise NotImplementedError("owned os.access only supports the default path lookup")
    value = _native_file_path(path)
    permission = _native_c_int(mode)
    result = _os_access(value, permission)
    if _error_pending():
        raise
    return result != 0


def chmod(path, mode, *, dir_fd=None, follow_symlinks=True):
    if _native_sys.implementation.name != "pcc":
        return _native_os.chmod(path, mode, dir_fd=dir_fd,
                                follow_symlinks=follow_symlinks)
    if dir_fd is not None or not follow_symlinks or isinstance(path, int):
        raise NotImplementedError("owned os.chmod only supports the default path lookup")
    value = _native_file_path(path)
    permission = _native_c_int(mode)
    result = _os_chmod(value, permission)
    if _error_pending():
        raise
    return result


def write(fd, data, /):
    if _native_sys.implementation.name != "pcc":
        return _native_os.write(fd, data)
    descriptor = _native_c_int(fd)
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("a bytes-like object is required")
    result = _os_write(descriptor, data)
    if _error_pending():
        raise
    while result < 0:
        # The existing write substrate uses the named libc call on Darwin;
        # Linux/Windows return the portable negative-errno status directly.
        number = _errno_get() if _platform_name == "darwin" else -result
        if number != 4:
            _os_error(number)
        _thread_safepoint()
        if _error_pending():
            raise
        result = _os_write(descriptor, data)
        if _error_pending():
            raise
    return result


def unlink(path, *, dir_fd=None):
    if _native_sys.implementation.name != "pcc":
        return _native_os.unlink(path, dir_fd=dir_fd)
    if dir_fd is not None:
        raise NotImplementedError("owned os.unlink dir_fd is not implemented")
    result = _os_unlink(_native_file_path(path))
    if _error_pending():
        raise
    return result


def remove(path, *, dir_fd=None):
    if _native_sys.implementation.name != "pcc":
        return _native_os.remove(path, dir_fd=dir_fd)
    if dir_fd is not None:
        raise NotImplementedError("owned os.remove dir_fd is not implemented")
    result = _os_unlink(_native_file_path(path))
    if _error_pending():
        raise
    return result


def rmdir(path, *, dir_fd=None):
    if _native_sys.implementation.name != "pcc":
        return _native_os.rmdir(path, dir_fd=dir_fd)
    if dir_fd is not None:
        raise NotImplementedError("owned os.rmdir dir_fd is not implemented")
    result = _os_rmdir(_native_file_path(path))
    if _error_pending():
        raise
    return result


def replace(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
    if _native_sys.implementation.name != "pcc":
        return _native_os.replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
    if src_dir_fd is not None or dst_dir_fd is not None:
        raise NotImplementedError("owned os.replace directory descriptors are not implemented")
    source = _native_file_path(src)
    destination = _native_file_path(dst)
    result = _os_replace(source, destination)
    if _error_pending():
        raise
    return result


def rename(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
    if _native_sys.implementation.name != "pcc":
        return _native_os.rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
    if src_dir_fd is not None or dst_dir_fd is not None:
        raise NotImplementedError("owned os.rename directory descriptors are not implemented")
    if _platform_name == "win32":
        raise NotImplementedError("owned os.rename destination-exists semantics on Windows are not implemented")
    source = _native_file_path(src)
    destination = _native_file_path(dst)
    result = _os_replace(source, destination)
    if _error_pending():
        raise
    return result


def urandom(size, /):
    if _native_sys.implementation.name != "pcc":
        return _native_os.urandom(size)
    count = _native_integer(size)
    if count < 0:
        raise ValueError("negative argument not allowed")
    if count > 9223372036854775807:
        raise OverflowError("Python int too large to convert to C ssize_t")
    result = _os_urandom(count)
    if _error_pending():
        raise
    if ptr_is_null(result):
        raise MemoryError("could not allocate random bytes")
    return result


def putenv(key, value, /):
    if _native_sys.implementation.name != "pcc":
        return _native_os.putenv(key, value)
    result = _os_putenv(key, value)
    if _error_pending():
        raise
    return result


def unsetenv(key, /):
    if _native_sys.implementation.name != "pcc":
        return _native_os.unsetenv(key)
    result = _environ_unset(key)
    if _error_pending():
        raise
    return result


def _pcc_http_download_to_file(url, destination):
    target = _native_file_path(destination)
    result = _http_download(url, target)
    if _error_pending():
        raise
    return result


def _pcc_sha256_file_hex(path):
    result = _file_digest(_native_file_path(path))
    if _error_pending():
        raise
    if ptr_is_null(result):
        raise MemoryError('owned os._pcc_sha256_file_hex result allocation failed')
    return result


def _pcc_sha256_file_hex_bounded(path, max_bytes):
    value = _native_file_path(path)
    limit = _native_integer(max_bytes)
    if limit < -9223372036854775808 or limit > 9223372036854775807:
        raise OverflowError("maximum file size does not fit in int64")
    result = _file_digest_bounded(value, limit)
    if _error_pending():
        raise
    if ptr_is_null(result):
        raise MemoryError('owned os._pcc_sha256_file_hex_bounded result allocation failed')
    return result


def walk(top, topdown=True, onerror=None, followlinks=False):
    """Yield owned directory rows, retaining the caller's mutable dirnames."""
    # This is an ordinary generator: path conversion and directory access
    # start at first next(), and normal generator frames own all suspended
    # values. No directory stream survives the synchronous scan boundary.
    pending = [(False, fspath(top))]
    while pending:
        emit, value = pending.pop()
        if emit:
            yield value
            continue
        root = value
        try:
            scanned = _walk_scan(root, topdown, followlinks)
            if isinstance(scanned, BaseException):
                raise scanned
            directories, files, children = scanned
        except OSError as error:
            if onerror is not None:
                onerror(error)
            continue
        row = root, directories, files
        if topdown:
            yield row
            if directories:
                prefix = _walk_prefix(root)
                if isinstance(prefix, BaseException):
                    raise prefix
                # Read the identical list exposed in the row. Recheck links
                # only after the caller's pruning, reordering and replacement.
                for child in reversed(directories):
                    child_path = prefix + child
                    if followlinks:
                        pending.append((False, child_path))
                    else:
                        linked = _path_islink_result(child_path)
                        if isinstance(linked, BaseException):
                            raise linked
                        if not linked:
                            pending.append((False, child_path))
        else:
            pending.append((True, row))
            for child in reversed(children):
                pending.append((False, child))


class _path:
    """The mutable path namespace, backed by ordinary saved-callable values."""

    sep = sep
    altsep = altsep
    pathsep = pathsep
    curdir = curdir
    pardir = pardir
    extsep = extsep
    devnull = devnull

    @staticmethod
    def join(*parts: str) -> str:
        if not parts:
            return ""
        out = parts[0]
        for p in parts[1:]:
            if not p:
                continue
            if p.startswith("/"):
                out = p
                continue
            if out.endswith("/"):
                out = out + p
            else:
                out = out + "/" + p
        return out


    @staticmethod
    def basename(p):
        if _native_sys.implementation.name != "pcc":
            return _native_os.path.basename(p)
        value = _native_path_text(p)
        result = _path_basename(value)
        if _error_pending():
            raise
        if ptr_is_null(result):
            raise MemoryError('owned os.path.basename result allocation failed')
        return result


    @staticmethod
    def dirname(p):
        if _native_sys.implementation.name != "pcc":
            return _native_os.path.dirname(p)
        # Closed-world ``from os import path`` binds this ordinary namespace.
        # Path.parents therefore needs the same target-aware dirname helper
        # as direct os.path calls, including Windows backslashes and roots.
        value = _native_path_text(p)
        result = _path_dirname(value)
        if _error_pending():
            raise
        if ptr_is_null(result):
            raise MemoryError('owned os.path.dirname result allocation failed')
        return result


    @staticmethod
    def split(p):
        if _native_sys.implementation.name != "pcc":
            return _native_os.path.split(p)
        value = _native_path_text(p)
        result = _path_split(value)
        if _error_pending():
            raise
        if ptr_is_null(result):
            raise MemoryError('owned os.path.split result allocation failed')
        return result


    @staticmethod
    def splitext(p: str):
        slash = -1
        dot = -1
        i = 0
        while i < len(p):
            if p[i] == "/":
                slash = i
                dot = -1
            elif p[i] == ".":
                dot = i
            i += 1
        if dot <= slash + 1:
            return (p, "")
        return (p[:dot], p[dot:])


    @staticmethod
    def normcase(path):
        if _native_sys.implementation.name != "pcc":
            return _native_os.path.normcase(path)
        value = _native_path_text(path)
        result = _path_normcase(value)
        if _error_pending():
            raise
        if ptr_is_null(result):
            raise MemoryError('owned os.path.normcase result allocation failed')
        return result


    @staticmethod
    def normpath(p: str) -> str:
        absolute = p.startswith("/")
        parts = []
        for part in p.split("/"):
            if part == "" or part == ".":
                continue
            if part == "..":
                if parts and parts[-1] != "..":
                    parts.pop()
                elif not absolute:
                    parts.append(part)
                continue
            parts.append(part)
        out = "/".join(parts)
        if absolute:
            out = "/" + out
        if out == "":
            return "/" if absolute else "."
        return out


    @staticmethod
    def splitdrive(path):
        if _native_sys.implementation.name != "pcc":
            return _native_os.path.splitdrive(path)
        value = _native_path_text(path)
        result = _path_splitdrive(value)
        if _error_pending():
            raise
        if ptr_is_null(result):
            raise MemoryError('owned os.path.splitdrive result allocation failed')
        return result


    @staticmethod
    def expanduser(path):
        if _native_sys.implementation.name != "pcc":
            return _native_os.path.expanduser(path)
        value = _native_path_text(path)
        result = _path_expanduser(value)
        if _error_pending():
            raise
        if ptr_is_null(result):
            raise MemoryError('owned os.path.expanduser result allocation failed')
        return result


    @staticmethod
    def expandvars(path):
        if _native_sys.implementation.name != "pcc":
            return _native_os.path.expandvars(path)
        value = _native_path_text(path)
        result = _path_expandvars(value)
        if _error_pending():
            raise
        if ptr_is_null(result):
            raise MemoryError('owned os.path.expandvars result allocation failed')
        return result


    @staticmethod
    def abspath(path):
        if _native_sys.implementation.name != "pcc":
            return _native_os.path.abspath(path)
        value = _native_path_text(path)
        result = _path_abspath(value)
        if _error_pending():
            raise
        if ptr_is_null(result):
            raise RuntimeError('owned os.path.abspath failed without an OS or allocation error status')
        return result


    @staticmethod
    def exists(p: str) -> bool:
        return exists(p)


    @staticmethod
    def isfile(path):
        if _native_sys.implementation.name != "pcc":
            return _native_os.path.isfile(path)
        if isinstance(path, int):
            raise NotImplementedError("owned os.path.isfile file descriptors are not implemented")
        try:
            value = _native_file_path(path)
        except (OSError, ValueError):
            return False
        result = _path_isfile(value)
        if _error_pending():
            raise
        return result != 0


    @staticmethod
    def isdir(path):
        if _native_sys.implementation.name != "pcc":
            return _native_os.path.isdir(path)
        if isinstance(path, int):
            raise NotImplementedError("owned os.path.isdir file descriptors are not implemented")
        try:
            value = _native_file_path(path)
        except (OSError, ValueError):
            return False
        result = _path_isdir(value)
        if _error_pending():
            raise
        return result != 0


    @staticmethod
    def isabs(p: str) -> bool:
        return p.startswith("/")

    @staticmethod
    def islink(path):
        if _native_sys.implementation.name != "pcc":
            return _native_os.path.islink(path)
        # This ordinary-object ABI owns fspath, bytes paths and predicate
        # suppression; it transfers a bool or the original exception object.
        result = _path_islink_result(path)
        if ptr_is_null(result):
            raise RuntimeError("owned os.path.islink helper returned NULL without an exception")
        if isinstance(result, BaseException):
            raise result
        return result


    @staticmethod
    def getmtime(filename):
        if _native_sys.implementation.name != "pcc":
            return _native_os.path.getmtime(filename)
        value = _native_file_path(filename)
        result = _path_getmtime(value)
        if _error_pending():
            raise
        if ptr_is_null(result):
            raise MemoryError('owned os.path.getmtime result allocation failed')
        return result


    @staticmethod
    def getsize(filename):
        if _native_sys.implementation.name != "pcc":
            return _native_os.path.getsize(filename)
        value = _native_file_path(filename)
        result = _path_getsize(value)
        if _error_pending():
            raise
        if ptr_is_null(result):
            raise MemoryError('owned os.path.getsize result allocation failed')
        return result


    @staticmethod
    def commonpath(paths) -> str:
        if _platform_name == "win32":
            if _native_sys.implementation.name != "pcc":
                return _native_os.path.commonpath(paths)
            values = [_native_path_text(value) for value in paths]
            result = _path_commonpath(values)
            if _error_pending():
                raise
            if ptr_is_null(result):
                raise MemoryError('owned os.path.commonpath result allocation failed')
            return result
        if not paths:
            raise ValueError("commonpath() arg is an empty sequence")
        split_paths = [p.split("/") for p in paths]
        prefix = []
        i = 0
        while True:
            if i >= len(split_paths[0]):
                break
            part = split_paths[0][i]
            for pieces in split_paths[1:]:
                if i >= len(pieces) or pieces[i] != part:
                    return "/".join(prefix)
            prefix.append(part)
            i += 1
        return "/".join(prefix)

    @staticmethod
    def commonprefix(m):
        if _native_sys.implementation.name != "pcc":
            return _native_os.path.commonprefix(m)
        values = [_native_path_text(value) for value in m]
        result = _path_commonprefix(values)
        if _error_pending():
            raise
        if ptr_is_null(result):
            raise MemoryError('owned os.path.commonprefix result allocation failed')
        return result

    @staticmethod
    def relpath(path, start=None):
        if _native_sys.implementation.name != "pcc":
            return _native_os.path.relpath(path, start)
        value = _native_path_text(path)
        origin = "." if start is None else _native_path_text(start)
        result = _path_relpath(value, origin)
        if _error_pending():
            raise
        if ptr_is_null(result):
            raise RuntimeError('owned os.path.relpath failed without an OS or allocation error status')
        return result

    @staticmethod
    def realpath(filename, *, strict=False):
        if _native_sys.implementation.name != "pcc":
            return _native_os.path.realpath(filename, strict=strict)
        if strict:
            raise NotImplementedError("owned os.path.realpath strict lookup is not implemented")
        value = _native_file_path(filename)
        result = _path_realpath(value)
        if _error_pending():
            raise
        if ptr_is_null(result):
            raise RuntimeError('owned os.path.realpath failed without an OS or allocation error status')
        return result


path = _path()
