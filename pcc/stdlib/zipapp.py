"""Finite, deterministic zipapp creation for pcc-native build tools.

This port owns directory-to-archive creation with optional shebangs, generated
ASCII ``module:function`` entry points, stored or raw-DEFLATE members, UTF-8
names and classic 32-bit ZIP records.  It normalizes member timestamps to
1980-01-01 and permissions to files 0644/directories 0755, and does not chmod
path targets.  File-like targets must be tellable and positioned at offset
zero; ZIP64, source-archive copying, filter callbacks, special files and trees
that escape through symlinks fail closed.
"""
from __future__ import annotations

import builtins
import os
import zlib


_MAX_MEMBER = 64 * 1024 * 1024
_MAX_ARCHIVE = 128 * 1024 * 1024
_MAX_ENTRIES = 65535
_MAX_DEPTH = 128
_UTF8_FLAG = 0x800
_ZIP_STORED = 0
_ZIP_DEFLATED = 8
_DOS_TIME = 0
_DOS_DATE = 33
_IDENTIFIER_START = "_abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
_IDENTIFIER_CONTINUE = _IDENTIFIER_START + "0123456789"


class ZipAppError(ValueError):
    pass


def _u16(value):
    number = int(value)
    if number < 0 or number > 0xFFFF:
        raise ZipAppError("ZIP field exceeds the unsigned 16-bit boundary")
    return number.to_bytes(2, "little")


def _u32(value):
    number = int(value)
    if number < 0 or number > 0xFFFFFFFF:
        raise ZipAppError("ZIP field exceeds the unsigned 32-bit boundary")
    return number.to_bytes(4, "little")


def _within(root, candidate):
    if candidate == root:
        return True
    prefix = root
    if not prefix.endswith("/"):
        prefix += "/"
    return candidate.startswith(prefix)


def _collect_directory(root, current, prefix, entries, seen, depth):
    if depth > _MAX_DEPTH:
        raise ZipAppError("zipapp directory depth exceeds 128")
    resolved = os.path.realpath(current)
    if not _within(root, resolved):
        raise ZipAppError("zipapp source tree escapes through a symlink")
    if resolved in seen:
        raise ZipAppError("zipapp source tree contains a directory cycle")
    seen.append(resolved)
    for name in sorted(os.listdir(current)):
        path = os.path.join(current, name)
        archive_name = name if prefix == "" else prefix + "/" + name
        if os.path.isdir(path):
            resolved_child = os.path.realpath(path)
            expected_child = os.path.join(resolved, name)
            if not _within(root, resolved_child):
                raise ZipAppError(
                    "zipapp source directory escapes through a symlink"
                )
            # ``zipfile.ZipFile.write`` preserves directory members.  Keep
            # them explicit rather than relying on readers to synthesize a
            # directory tree from file names.
            entries.append((archive_name + "/", None, b""))
            if len(entries) > _MAX_ENTRIES:
                raise ZipAppError("ZIP64 entry counts are not runtime-owned")
            # pathlib.rglob records a directory symlink but does not descend
            # through it.  Avoid duplicate trees and cycles in the same way.
            if resolved_child != expected_child:
                continue
            _collect_directory(
                root,
                path,
                archive_name,
                entries,
                seen,
                depth + 1,
            )
        elif os.path.isfile(path):
            resolved_file = os.path.realpath(path)
            if not _within(root, resolved_file):
                raise ZipAppError(
                    "zipapp source file escapes through a symlink"
                )
            entries.append((archive_name, path, None))
            if len(entries) > _MAX_ENTRIES:
                raise ZipAppError("ZIP64 entry counts are not runtime-owned")
        else:
            raise NotImplementedError(
                "zipapp special filesystem entries are not runtime-owned"
            )
    seen.pop()


def _identifier(value):
    if value == "":
        return False
    if value[0] not in _IDENTIFIER_START:
        return False
    for char in value[1:]:
        if char not in _IDENTIFIER_CONTINUE:
            return False
    return True


def _main_source(main):
    parts = str(main).split(":")
    if len(parts) != 2 or parts[0] == "" or parts[1] == "":
        raise ZipAppError("Invalid entry point: " + repr(main))
    module = parts[0]
    function = parts[1]
    for component in module.split(".") + function.split("."):
        if not _identifier(component):
            raise ZipAppError("Invalid entry point: " + repr(main))
    return (
        "# -*- coding: utf-8 -*-\n"
        + "import "
        + module
        + "\n"
        + module
        + "."
        + function
        + "()\n"
    ).encode("utf-8")


def _read_member(path):
    with builtins.open(path, "rb") as source:
        data = source.read(_MAX_MEMBER + 1)
    if len(data) > _MAX_MEMBER:
        raise ZipAppError("zipapp member exceeds the 64 MiB limit: " + path)
    return bytes(data)


def _local_header(name, flags, method, checksum, compressed_size, size):
    return (
        b"PK\x03\x04"
        + _u16(20)
        + _u16(flags)
        + _u16(method)
        + _u16(_DOS_TIME)
        + _u16(_DOS_DATE)
        + _u32(checksum)
        + _u32(compressed_size)
        + _u32(size)
        + _u16(len(name))
        + _u16(0)
        + name
    )


def _central_header(
    name,
    flags,
    method,
    checksum,
    compressed_size,
    size,
    external_attr,
    local_offset,
):
    return (
        b"PK\x01\x02"
        + _u16((3 << 8) | 20)
        + _u16(20)
        + _u16(flags)
        + _u16(method)
        + _u16(_DOS_TIME)
        + _u16(_DOS_DATE)
        + _u32(checksum)
        + _u32(compressed_size)
        + _u32(size)
        + _u16(len(name))
        + _u16(0)
        + _u16(0)
        + _u16(0)
        + _u16(0)
        + _u32(external_attr)
        + _u32(local_offset)
        + name
    )


def _end_record(entry_count, central_size, central_offset):
    return (
        b"PK\x05\x06"
        + _u16(0)
        + _u16(0)
        + _u16(entry_count)
        + _u16(entry_count)
        + _u32(central_size)
        + _u32(central_offset)
        + _u16(0)
    )


def _default_target(source_path):
    # Match pathlib.Path.with_suffix(".pyz") without making pathlib part of
    # the bootstrap closure.  A leading dot alone does not form a suffix.
    base = source_path.rstrip("/")
    separator = base.rfind("/")
    dot = base.rfind(".")
    if dot > separator + 1 and dot < len(base) - 1:
        base = base[:dot]
    return base + ".pyz"


def create_archive(
    source,
    target=None,
    interpreter=None,
    main=None,
    filter=None,
    compressed=False,
):
    if filter is not None:
        raise NotImplementedError("zipapp filter callbacks are not runtime-owned")
    if hasattr(source, "read"):
        raise NotImplementedError(
            "zipapp source-archive copying is not runtime-owned"
        )
    source_path = str(source)
    if not os.path.exists(source_path):
        raise ZipAppError("Source does not exist")
    if not os.path.isdir(source_path):
        raise NotImplementedError(
            "zipapp source-archive copying is not runtime-owned"
        )
    root = os.path.realpath(source_path)
    target_object = target
    if target_object is None:
        target_object = _default_target(source_path)
    target_is_stream = hasattr(target_object, "write")
    if target_is_stream:
        if not hasattr(target_object, "tell"):
            raise NotImplementedError(
                "zipapp untellable file-like targets are not runtime-owned"
            )
        if int(target_object.tell()) != 0:
            raise ZipAppError(
                "zipapp file-like target must be positioned at offset zero"
            )
    else:
        target_path = os.path.realpath(str(target_object))
        if _within(root, target_path):
            raise ZipAppError("zipapp target must be outside the source tree")
    shebang_prefix = b""
    if interpreter:
        interpreter_text = str(interpreter)
        if "\n" in interpreter_text or "\r" in interpreter_text:
            raise ZipAppError("The interpreter must not contain a newline")
        shebang_tail = (interpreter_text + "\n").encode("utf-8")
        if len(shebang_tail) > 4096:
            raise ZipAppError("zipapp shebang exceeds 4096 bytes")
        shebang_prefix = b"#!" + shebang_tail

    entries = []
    _collect_directory(root, root, "", entries, [], 0)
    has_main = False
    for archive_name, _path, _virtual in entries:
        if archive_name == "__main__.py":
            has_main = True
            break
    if main:
        if has_main:
            raise ZipAppError(
                "Cannot specify an entry point if __main__.py already exists"
            )
        entries.append(("__main__.py", None, _main_source(main)))
        has_main = True
    if not has_main:
        raise ZipAppError("Archive has no entry point: __main__.py is missing")
    if len(entries) > 0xFFFF:
        raise ZipAppError("ZIP64 entry counts are not runtime-owned")

    output = bytearray()
    output.extend(shebang_prefix)
    central_records = []
    file_method = _ZIP_DEFLATED if bool(compressed) else _ZIP_STORED
    for archive_name, path, virtual_data in entries:
        raw_name = archive_name.encode("utf-8")
        flags = 0 if archive_name.isascii() else _UTF8_FLAG
        if len(raw_name) > 0xFFFF:
            raise ZipAppError("zipapp member name exceeds 65535 bytes")
        data = virtual_data if path is None else _read_member(path)
        is_directory = archive_name.endswith("/")
        method = _ZIP_STORED if is_directory else file_method
        packed = data
        if method == _ZIP_DEFLATED:
            packed = zlib.compress(data, -1, -zlib.MAX_WBITS)
        checksum = zlib.crc32(data) & 0xFFFFFFFF
        local_offset = len(output)
        output.extend(
            _local_header(
                raw_name, flags, method, checksum, len(packed), len(data)
            )
        )
        output.extend(packed)
        central_records.append(
            _central_header(
                raw_name,
                flags,
                method,
                checksum,
                len(packed),
                len(data),
                ((0o40755 << 16) | 0x10)
                if is_directory
                else (0o100644 << 16),
                local_offset,
            )
        )
        if len(output) > _MAX_ARCHIVE:
            raise ZipAppError("zipapp archive exceeds the 128 MiB limit")

    central_offset = len(output)
    for record in central_records:
        output.extend(record)
        if len(output) > _MAX_ARCHIVE:
            raise ZipAppError("zipapp archive exceeds the 128 MiB limit")
    central_size = len(output) - central_offset
    output.extend(_end_record(len(entries), central_size, central_offset))
    if len(output) > _MAX_ARCHIVE:
        raise ZipAppError("zipapp archive exceeds the 128 MiB limit")

    payload = bytes(output)
    if target_is_stream:
        target_object.write(payload)
    else:
        with builtins.open(str(target_object), "wb") as destination:
            destination.write(payload)
    return None


def get_interpreter(archive):
    if hasattr(archive, "read"):
        stream = archive
        should_close = False
    else:
        stream = builtins.open(str(archive), "rb")
        should_close = True
    try:
        prefix = stream.read(2)
        if prefix != b"#!":
            return None
        shebang = stream.readline(4097)
    finally:
        if should_close:
            stream.close()
    if len(shebang) > 4096:
        raise ZipAppError("zipapp shebang exceeds 4096 bytes")
    try:
        start = 0
        end = len(shebang)
        while start < end and shebang[start] in (9, 10, 11, 12, 13, 32):
            start += 1
        while end > start and shebang[end - 1] in (9, 10, 11, 12, 13, 32):
            end -= 1
        return shebang[start:end].decode("utf-8")
    except UnicodeDecodeError:
        raise ZipAppError("zipapp shebang is not valid UTF-8")


def main(args=None):
    raise NotImplementedError("zipapp command-line parsing is not runtime-owned")


__all__ = [
    "ZipAppError",
    "create_archive",
    "get_interpreter",
]
