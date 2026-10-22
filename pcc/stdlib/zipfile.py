"""Bounded, read-only ZIP support for pcc-native build tools.

The parser owns classic single-disk ZIP archives with stored, DEFLATE or bzip2
members, UTF-8 or ASCII names, CRC verification, member inspection and bounded
content extraction.  Extraction rejects lexical traversal and resolvable
pre-existing symlink escapes; it is not a race-free filesystem sandbox.  ZIP64,
encryption, prepended executable stubs, non-ASCII CP437 names and archive
creation are deliberately fail-closed instead of being mistaken for full
compatibility.
"""
from __future__ import annotations

import builtins
import bz2
import io
import os
import zlib


ZIP_STORED = 0
ZIP_DEFLATED = 8
ZIP_BZIP2 = 12
ZIP_LZMA = 14

_MAX_ARCHIVE = 128 * 1024 * 1024
_MAX_MEMBER = 64 * 1024 * 1024
_MAX_ENTRIES = 100000


class BadZipFile(Exception):
    pass


BadZipfile = BadZipFile


class LargeZipFile(Exception):
    pass


def _u16(data, offset):
    return int.from_bytes(data[offset:offset + 2], "little")


def _u32(data, offset):
    return int.from_bytes(data[offset:offset + 4], "little")


def _decode_ascii(raw):
    text = ""
    for value in raw:
        if value > 127:
            raise NotImplementedError(
                "non-ASCII CP437 ZIP member names are not runtime-owned"
            )
        text = text + chr(value)
    return text


def _decode_name(raw, flag_bits):
    if flag_bits & 0x800:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raise BadZipFile("invalid UTF-8 ZIP member name")
    return _decode_ascii(raw)


def _read_source(file):
    if hasattr(file, "read"):
        payload = file.read(_MAX_ARCHIVE + 1)
    else:
        with builtins.open(str(file), "rb") as source:
            payload = source.read(_MAX_ARCHIVE + 1)
    if len(payload) > _MAX_ARCHIVE:
        raise LargeZipFile("ZIP archive exceeds the 128 MiB read limit")
    return bytes(payload)


def _find_eocd(data):
    minimum = len(data) - 65557
    if minimum < 0:
        minimum = 0
    offset = len(data) - 22
    while offset >= minimum:
        if data[offset:offset + 4] == b"PK\x05\x06":
            comment_size = _u16(data, offset + 20)
            if offset + 22 + comment_size == len(data):
                return offset
        offset -= 1
    raise BadZipFile("File is not a zip file")


def _safe_target(root, member_name):
    if "\x00" in member_name:
        raise BadZipFile("ZIP member contains a NUL byte")
    normalized = member_name.replace("\\", "/")
    if normalized.startswith("/"):
        raise BadZipFile("absolute ZIP member path is not safe to extract")
    pieces = []
    for piece in normalized.split("/"):
        if piece == "" or piece == ".":
            continue
        if piece == "..":
            raise BadZipFile("parent traversal in ZIP member path")
        pieces.append(piece)
    target = str(root)
    for piece in pieces:
        target = os.path.join(target, piece)
    resolved_root = os.path.realpath(str(root))
    root_prefix = resolved_root
    if not root_prefix.endswith("/"):
        root_prefix = root_prefix + "/"
    # The pcc runtime's realpath helper deliberately falls back to lexical
    # normalization when the final path doesn't exist.  Resolve each existing
    # prefix separately so a symlinked parent can't disappear in that fallback.
    lexical_prefix = str(root)
    resolved_prefix = resolved_root
    for piece in pieces:
        lexical_prefix = os.path.join(lexical_prefix, piece)
        if os.path.exists(lexical_prefix):
            resolved_prefix = os.path.realpath(lexical_prefix)
        else:
            resolved_prefix = os.path.join(resolved_prefix, piece)
        if (
            resolved_prefix != resolved_root
            and not resolved_prefix.startswith(root_prefix)
        ):
            raise BadZipFile("ZIP member resolves outside the extraction root")
    return target


class ZipInfo:
    def __init__(self, filename="NoName", date_time=(1980, 1, 1, 0, 0, 0)):
        self.orig_filename = filename
        self.filename = filename
        self.date_time = date_time
        self.compress_type = ZIP_STORED
        self.comment = b""
        self.extra = b""
        self.create_system = 3
        self.create_version = 20
        self.extract_version = 20
        self.reserved = 0
        self.flag_bits = 0
        self.volume = 0
        self.internal_attr = 0
        self.external_attr = 0
        self.header_offset = 0
        self.CRC = 0
        self.compress_size = 0
        self.file_size = 0
        self._raw_filename = b""

    def is_dir(self):
        return self.filename.endswith("/")

    def __repr__(self):
        return "<ZipInfo filename=" + repr(self.filename) + ">"


class ZipFile:
    def __init__(
        self,
        file,
        mode="r",
        compression=ZIP_STORED,
        allowZip64=True,
        compresslevel=None,
        strict_timestamps=True,
        metadata_encoding=None,
    ):
        if mode != "r":
            raise NotImplementedError("ZIP creation and append are not runtime-owned")
        if metadata_encoding is not None:
            raise NotImplementedError(
                "custom ZIP metadata encodings are not runtime-owned"
            )
        self.mode = mode
        self.filename = None if hasattr(file, "read") else str(file)
        self._data = _read_source(file)
        self._infos = []
        self._by_name = {}
        self.comment = b""
        self._central_offset = 0
        self._parse()

    def _parse(self):
        data = self._data
        eocd = _find_eocd(data)
        disk_number = _u16(data, eocd + 4)
        central_disk = _u16(data, eocd + 6)
        disk_entries = _u16(data, eocd + 8)
        total_entries = _u16(data, eocd + 10)
        central_size = _u32(data, eocd + 12)
        central_offset = _u32(data, eocd + 16)
        self.comment = data[eocd + 22:]
        self._central_offset = central_offset
        if disk_number != 0 or central_disk != 0 or disk_entries != total_entries:
            raise BadZipFile("multi-disk ZIP archives are not supported")
        if (
            total_entries == 0xFFFF
            or central_size == 0xFFFFFFFF
            or central_offset == 0xFFFFFFFF
        ):
            raise LargeZipFile("ZIP64 archives are not runtime-owned")
        if total_entries > _MAX_ENTRIES:
            raise BadZipFile("ZIP archive has too many members")
        if central_offset + central_size > eocd:
            raise BadZipFile("central directory extends beyond its boundary")

        offset = central_offset
        count = 0
        while count < total_entries:
            if offset + 46 > len(data) or data[offset:offset + 4] != b"PK\x01\x02":
                raise BadZipFile("Bad magic number for central directory")
            made_by = _u16(data, offset + 4)
            needed = _u16(data, offset + 6)
            flags = _u16(data, offset + 8)
            method = _u16(data, offset + 10)
            dos_time = _u16(data, offset + 12)
            dos_date = _u16(data, offset + 14)
            crc = _u32(data, offset + 16)
            compressed_size = _u32(data, offset + 20)
            file_size = _u32(data, offset + 24)
            name_size = _u16(data, offset + 28)
            extra_size = _u16(data, offset + 30)
            comment_size = _u16(data, offset + 32)
            disk_start = _u16(data, offset + 34)
            internal_attr = _u16(data, offset + 36)
            external_attr = _u32(data, offset + 38)
            local_offset = _u32(data, offset + 42)
            end = offset + 46 + name_size + extra_size + comment_size
            if end > len(data):
                raise BadZipFile("truncated central directory entry")
            if disk_start != 0:
                raise BadZipFile("multi-disk ZIP member is not supported")
            if (
                compressed_size == 0xFFFFFFFF
                or file_size == 0xFFFFFFFF
                or local_offset == 0xFFFFFFFF
            ):
                raise LargeZipFile("ZIP64 member is not runtime-owned")
            raw_name = data[offset + 46:offset + 46 + name_size]
            name = _decode_name(raw_name, flags)
            info = ZipInfo(name)
            info._raw_filename = raw_name
            info.create_system = made_by >> 8
            info.create_version = made_by & 0xFF
            info.extract_version = needed & 0xFF
            info.flag_bits = flags
            info.compress_type = method
            info.CRC = crc
            info.compress_size = compressed_size
            info.file_size = file_size
            info.volume = disk_start
            info.internal_attr = internal_attr
            info.external_attr = external_attr
            info.header_offset = local_offset
            info.extra = data[
                offset + 46 + name_size:offset + 46 + name_size + extra_size
            ]
            info.comment = data[
                offset + 46 + name_size + extra_size:end
            ]
            info.date_time = (
                ((dos_date >> 9) & 127) + 1980,
                (dos_date >> 5) & 15,
                dos_date & 31,
                (dos_time >> 11) & 31,
                (dos_time >> 5) & 63,
                (dos_time & 31) * 2,
            )
            self._infos.append(info)
            self._by_name[name] = info
            offset = end
            count += 1
        if offset != central_offset + central_size:
            raise BadZipFile("central directory size does not match its entries")

    def namelist(self):
        return [info.filename for info in self._infos]

    def infolist(self):
        return list(self._infos)

    def getinfo(self, name):
        if name not in self._by_name:
            raise KeyError("There is no item named " + repr(name) + " in the archive")
        return self._by_name[name]

    def _member_data(self, info):
        if info.flag_bits & 1:
            raise NotImplementedError("encrypted ZIP members are not runtime-owned")
        if info.file_size > _MAX_MEMBER:
            raise BadZipFile("ZIP member exceeds the 64 MiB output limit")
        offset = info.header_offset
        if (
            offset < 0
            or offset + 30 > len(self._data)
            or self._data[offset:offset + 4] != b"PK\x03\x04"
        ):
            raise BadZipFile("Bad magic number for file header")
        local_flags = _u16(self._data, offset + 6)
        local_method = _u16(self._data, offset + 8)
        name_size = _u16(self._data, offset + 26)
        extra_size = _u16(self._data, offset + 28)
        if local_method != info.compress_type or local_flags != info.flag_bits:
            raise BadZipFile("central and local ZIP headers disagree")
        start = offset + 30 + name_size + extra_size
        end = start + info.compress_size
        if start > self._central_offset or end > self._central_offset:
            raise BadZipFile("ZIP member payload overlaps the central directory")
        local_name = self._data[offset + 30:offset + 30 + name_size]
        if local_name != info._raw_filename:
            raise BadZipFile("central and local ZIP member names disagree")
        if end > len(self._data):
            raise BadZipFile("truncated ZIP member payload")
        compressed = self._data[start:end]
        try:
            if info.compress_type == ZIP_STORED:
                result = compressed
            elif info.compress_type == ZIP_DEFLATED:
                result = zlib.decompress(compressed, -zlib.MAX_WBITS)
            elif info.compress_type == ZIP_BZIP2:
                result = bz2.decompress(compressed)
            else:
                raise NotImplementedError(
                    "ZIP compression method is not runtime-owned: "
                    + str(info.compress_type)
                )
        except (OSError, zlib.error) as exc:
            raise BadZipFile(
                "invalid compressed data for file "
                + repr(info.filename)
                + ": "
                + str(exc)
            )
        if len(result) != info.file_size:
            raise BadZipFile("Bad uncompressed size for file " + repr(info.filename))
        if zlib.crc32(result) != info.CRC:
            raise BadZipFile("Bad CRC-32 for file " + repr(info.filename))
        return result

    def read(self, name, pwd=None):
        if pwd is not None:
            raise NotImplementedError("ZIP password handling is not runtime-owned")
        info = name if isinstance(name, ZipInfo) else self.getinfo(name)
        return self._member_data(info)

    def open(self, name, mode="r", pwd=None, *, force_zip64=False):
        if mode not in ("r", "rb"):
            raise NotImplementedError("ZIP member writes are not runtime-owned")
        if force_zip64:
            raise LargeZipFile("ZIP64 member streams are not runtime-owned")
        return io.BytesIO(self.read(name, pwd=pwd))

    def extract(self, member, path=None, pwd=None):
        info = member if isinstance(member, ZipInfo) else self.getinfo(member)
        root = "." if path is None else path
        target = _safe_target(root, info.filename)
        if info.is_dir():
            os.makedirs(target, exist_ok=True)
            return target
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with builtins.open(target, "wb") as destination:
            destination.write(self.read(info, pwd=pwd))
        return target

    def extractall(self, path=None, members=None, pwd=None):
        selected = self._infos if members is None else members
        for member in selected:
            self.extract(member, path=path, pwd=pwd)

    def testzip(self):
        for info in self._infos:
            if info.is_dir():
                continue
            try:
                self._member_data(info)
            except BadZipFile:
                return info.filename
        return None

    def write(self, filename, arcname=None, compress_type=None, compresslevel=None):
        raise NotImplementedError("ZIP creation is not runtime-owned")

    def writestr(self, zinfo_or_arcname, data, compress_type=None,
                 compresslevel=None):
        raise NotImplementedError("ZIP creation is not runtime-owned")

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


def is_zipfile(filename):
    try:
        ZipFile(filename).close()
        return True
    except (OSError, BadZipFile, LargeZipFile, NotImplementedError):
        return False


__all__ = [
    "BadZipFile",
    "BadZipfile",
    "LargeZipFile",
    "ZipInfo",
    "ZipFile",
    "is_zipfile",
    "ZIP_STORED",
    "ZIP_DEFLATED",
    "ZIP_BZIP2",
    "ZIP_LZMA",
]
