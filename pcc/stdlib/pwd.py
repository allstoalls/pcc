"""POSIX password-database lookups for pcc-native build tools.

The lookup surface is backed by the re-entrant libc/NSS entry points and
copies every returned string before releasing the caller-owned scratch
buffer.  This keeps records independent across calls and native threads.

``getpwall`` is intentionally not implemented with ``getpwent``: that API has
a process-global enumeration cursor and would make concurrent pcc callers
race.  It remains fail-closed until the runtime owns a serialized/re-entrant
enumeration contract.
"""
from __future__ import annotations

import sys

from pcc.extern import c_int, c_int64, c_size_t, c_uint32, extern, c_obj, c_rawptr
from pcc.unsafe import free, load_i32, load_ptr, malloc, null, ptr_is_null, store_ptr


# The lookup state, buffers and returned strings are raw addresses, never
# Python objects. A c_rawptr parameter also unboxes one that came back
# through the lookup-state tuple; c_ptr passed that int object itself, so
# getpwuid_r wrote through a tagged integer (SIGBUS).
_getpwuid_r: "extern" = extern(
    "getpwuid_r",
    (c_uint32, c_rawptr, c_rawptr, c_size_t, c_rawptr),
    c_int,
)
_getpwnam_r: "extern" = extern(
    "getpwnam_r",
    (c_rawptr, c_rawptr, c_rawptr, c_size_t, c_rawptr),
    c_int,
)
_py_str_utf8: "extern" = extern("py_str_utf8", (c_obj,), c_rawptr)
_py_str_new: "extern" = extern("py_str_new", (c_rawptr, c_int64), c_obj)
_strlen: "extern" = extern("strlen", (c_rawptr,), c_size_t)


_INITIAL_BUFFER_SIZE = 16384
_MAX_BUFFER_SIZE = 1048576
_ERANGE = 34
_UID_MAX = 4294967295


class struct_passwd:
    """Seven-field sequence view matching CPython's ``pwd.struct_passwd``."""

    n_fields = 7
    n_sequence_fields = 7
    n_unnamed_fields = 0

    def __init__(self, values):
        if len(values) != 7:
            raise TypeError("pwd.struct_passwd requires a seven-item sequence")
        self._pw_name = values[0]
        self._pw_passwd = values[1]
        self._pw_uid = values[2]
        self._pw_gid = values[3]
        self._pw_gecos = values[4]
        self._pw_dir = values[5]
        self._pw_shell = values[6]

    @property
    def pw_name(self):
        return self._pw_name

    @property
    def pw_passwd(self):
        return self._pw_passwd

    @property
    def pw_uid(self):
        return self._pw_uid

    @property
    def pw_gid(self):
        return self._pw_gid

    @property
    def pw_gecos(self):
        return self._pw_gecos

    @property
    def pw_dir(self):
        return self._pw_dir

    @property
    def pw_shell(self):
        return self._pw_shell

    def _as_tuple(self):
        return (
            self.pw_name,
            self.pw_passwd,
            self.pw_uid,
            self.pw_gid,
            self.pw_gecos,
            self.pw_dir,
            self.pw_shell,
        )

    def __len__(self):
        return 7

    def __getitem__(self, index):
        return self._as_tuple()[index]

    def __iter__(self):
        return iter(self._as_tuple())

    def __hash__(self):
        return hash(self._as_tuple())

    def __eq__(self, other):
        if isinstance(other, struct_passwd):
            return self._as_tuple() == other._as_tuple()
        return self._as_tuple() == other

    def __ne__(self, other):
        return not self.__eq__(other)

    def count(self, value):
        return self._as_tuple().count(value)

    def index(self, value, start=0, stop=7):
        return self._as_tuple().index(value, start, stop)

    def __repr__(self):
        return (
            "pwd.struct_passwd(pw_name="
            + repr(self.pw_name)
            + ", pw_passwd="
            + repr(self.pw_passwd)
            + ", pw_uid="
            + repr(self.pw_uid)
            + ", pw_gid="
            + repr(self.pw_gid)
            + ", pw_gecos="
            + repr(self.pw_gecos)
            + ", pw_dir="
            + repr(self.pw_dir)
            + ", pw_shell="
            + repr(self.pw_shell)
            + ")"
        )


def _copy_text(raw):
    if ptr_is_null(raw):
        return ""
    return _py_str_new(raw, _strlen(raw))


def _unsigned_i32(value):
    if value < 0:
        return value + 4294967296
    return value


def _record_from_native(entry):
    if sys.platform.startswith("darwin"):
        gecos_offset = 40
        directory_offset = 48
        shell_offset = 56
    elif sys.platform.startswith("linux"):
        gecos_offset = 24
        directory_offset = 32
        shell_offset = 40
    else:
        raise NotImplementedError(
            "pwd native struct layout is owned only for Darwin and Linux"
        )
    return struct_passwd(
        (
            _copy_text(load_ptr(entry, 0)),
            _copy_text(load_ptr(entry, 8)),
            _unsigned_i32(load_i32(entry, 16)),
            _unsigned_i32(load_i32(entry, 20)),
            _copy_text(load_ptr(entry, gecos_offset)),
            _copy_text(load_ptr(entry, directory_offset)),
            _copy_text(load_ptr(entry, shell_offset)),
        )
    )


def _allocate_lookup_state():
    # Darwin's struct passwd is 80 bytes; Linux's is 48.  Reserving the larger
    # layout is safe for both ABIs and keeps the allocation target-independent.
    entry = malloc(80)
    result_slot = malloc(8)
    if ptr_is_null(entry) or ptr_is_null(result_slot):
        free(entry)
        free(result_slot)
        raise MemoryError("unable to allocate pwd lookup state")
    return entry, result_slot


def getpwuid(uid):
    if not isinstance(uid, int):
        raise TypeError("getpwuid() argument must be an integer")
    value = uid
    if value < 0 or value > _UID_MAX:
        raise KeyError("getpwuid(): uid not found: " + str(uid))
    entry, result_slot = _allocate_lookup_state()
    buffer_size = _INITIAL_BUFFER_SIZE
    while buffer_size <= _MAX_BUFFER_SIZE:
        buffer = malloc(buffer_size)
        if ptr_is_null(buffer):
            free(entry)
            free(result_slot)
            raise MemoryError("unable to allocate pwd lookup buffer")
        store_ptr(result_slot, 0, null())
        status = _getpwuid_r(
            value,
            entry,
            buffer,
            buffer_size,
            result_slot,
        )
        if status == _ERANGE:
            free(buffer)
            buffer_size = buffer_size * 2
            continue
        result = load_ptr(result_slot, 0)
        if status != 0:
            free(buffer)
            free(entry)
            free(result_slot)
            raise OSError("getpwuid_r failed with errno " + str(status))
        if ptr_is_null(result):
            free(buffer)
            free(entry)
            free(result_slot)
            raise KeyError("getpwuid(): uid not found: " + str(uid))
        try:
            return _record_from_native(result)
        finally:
            free(buffer)
            free(entry)
            free(result_slot)
    free(entry)
    free(result_slot)
    raise OSError("getpwuid_r exceeded the owned NSS buffer limit")


def getpwnam(name):
    if not isinstance(name, str):
        raise TypeError("getpwnam() argument must be str")
    if "\x00" in name:
        raise ValueError("embedded null character")
    entry, result_slot = _allocate_lookup_state()
    encoded_name = _py_str_utf8(name)
    if ptr_is_null(encoded_name):
        free(entry)
        free(result_slot)
        raise ValueError("getpwnam() name cannot be encoded as UTF-8")
    buffer_size = _INITIAL_BUFFER_SIZE
    while buffer_size <= _MAX_BUFFER_SIZE:
        buffer = malloc(buffer_size)
        if ptr_is_null(buffer):
            free(entry)
            free(result_slot)
            raise MemoryError("unable to allocate pwd lookup buffer")
        store_ptr(result_slot, 0, null())
        status = _getpwnam_r(
            encoded_name,
            entry,
            buffer,
            buffer_size,
            result_slot,
        )
        if status == _ERANGE:
            free(buffer)
            buffer_size = buffer_size * 2
            continue
        result = load_ptr(result_slot, 0)
        if status != 0:
            free(buffer)
            free(entry)
            free(result_slot)
            raise OSError("getpwnam_r failed with errno " + str(status))
        if ptr_is_null(result):
            free(buffer)
            free(entry)
            free(result_slot)
            raise KeyError("getpwnam(): name not found: " + name)
        try:
            return _record_from_native(result)
        finally:
            free(buffer)
            free(entry)
            free(result_slot)
    free(entry)
    free(result_slot)
    raise OSError("getpwnam_r exceeded the owned NSS buffer limit")


def getpwall():
    raise NotImplementedError(
        "pwd.getpwall requires a runtime-owned serialized NSS enumeration cursor"
    )


__all__ = ["struct_passwd", "getpwuid", "getpwnam", "getpwall"]
