"""Bounded, read-only tar support for pcc-native build tools.

This port parses ustar, GNU long-name and POSIX PAX records from plain, gzip,
bzip2 or XZ/LZMA archives.  It owns member inspection, bounded regular-file
reads and content extraction with lexical and resolvable-symlink containment
checks.  Extraction is not a race-free filesystem sandbox.  Archive creation
and special-file/link materialisation remain explicit fail-closed boundaries.
"""
from __future__ import annotations

import builtins
import bz2
import gzip
import io
import lzma
import os

from pcc.extern import c_ptr, extern, c_rawptr
from pcc.unsafe import (
    chmod_file, free, malloc, ptr_is_null, store_i64, utime_file,
)


_py_str_utf8: "extern" = extern("py_str_utf8", (c_ptr,), c_rawptr)

NUL = b"\0"
BLOCKSIZE = 512
RECORDSIZE = BLOCKSIZE * 20

REGTYPE = b"0"
AREGTYPE = b"\0"
LNKTYPE = b"1"
SYMTYPE = b"2"
CHRTYPE = b"3"
BLKTYPE = b"4"
DIRTYPE = b"5"
FIFOTYPE = b"6"
CONTTYPE = b"7"
GNUTYPE_LONGNAME = b"L"
GNUTYPE_LONGLINK = b"K"
XHDTYPE = b"x"
XGLTYPE = b"g"

SUPPORTED_TYPES = (REGTYPE, AREGTYPE, DIRTYPE)

_MAX_ARCHIVE = 128 * 1024 * 1024
_MAX_MEMBER = 64 * 1024 * 1024
_MAX_ENTRIES = 100000


class TarError(Exception):
    pass


class ReadError(TarError):
    pass


class CompressionError(TarError):
    pass


class StreamError(TarError):
    pass


class ExtractError(TarError):
    pass


class HeaderError(TarError):
    pass


class EmptyHeaderError(HeaderError):
    pass


class TruncatedHeaderError(HeaderError):
    pass


class EOFHeaderError(HeaderError):
    pass


class InvalidHeaderError(HeaderError):
    pass


class SubsequentHeaderError(HeaderError):
    pass


def _field_bytes(field):
    end = 0
    while end < len(field) and field[end] != 0:
        end += 1
    # Text fields may legally start or end with spaces.  Numeric callers strip
    # their own padding; doing it here silently renamed archive members.
    return field[:end]


def _ascii_text(raw):
    text = ""
    for value in raw:
        if value > 127:
            raise InvalidHeaderError("non-ASCII tar numeric field")
        text = text + chr(value)
    return text


def _decode_field(field):
    raw = _field_bytes(field)
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ReadError("non-UTF-8 tar header text is not runtime-owned")


def _number(field):
    if not field:
        return 0
    if field[0] & 0x80:
        if field[0] & 0x40:
            raise InvalidHeaderError("negative base-256 tar number unsupported")
        bits = len(field) * 8 - 1
        return int.from_bytes(field, "big") & ((1 << bits) - 1)
    raw = _field_bytes(field).strip()
    if not raw:
        return 0
    try:
        return int(_ascii_text(raw), 8)
    except ValueError:
        raise InvalidHeaderError("invalid tar numeric field")


def _checksum(block):
    return sum(block[:148]) + 8 * 32 + sum(block[156:])


def _parse_pax(payload):
    headers = {}
    offset = 0
    while offset < len(payload):
        if payload[offset] == 0:
            break
        space = offset
        while space < len(payload) and payload[space] != 32:
            space += 1
        if space >= len(payload):
            space = -1
        if space < 0:
            raise InvalidHeaderError("malformed PAX record length")
        try:
            record_size = int(_ascii_text(payload[offset:space]))
        except ValueError:
            raise InvalidHeaderError("malformed PAX record length")
        end = offset + record_size
        if record_size <= 0 or end > len(payload) or payload[end - 1] != 10:
            raise InvalidHeaderError("truncated PAX record")
        try:
            record = payload[space + 1:end - 1].decode("utf-8")
        except UnicodeDecodeError:
            raise InvalidHeaderError("non-UTF-8 PAX record")
        equals = record.find("=")
        if equals <= 0:
            raise InvalidHeaderError("malformed PAX key/value record")
        headers[record[:equals]] = record[equals + 1:]
        offset = end
    return headers


def _long_text(payload):
    end = 0
    while end < len(payload) and payload[end] not in (0, 10):
        end += 1
    try:
        return payload[:end].decode("utf-8")
    except UnicodeDecodeError:
        raise ReadError("non-UTF-8 GNU long name is not runtime-owned")


def _read_source(name, fileobj):
    if fileobj is not None:
        payload = fileobj.read(_MAX_ARCHIVE + 1)
    else:
        if name is None:
            raise ValueError("nothing to open")
        with builtins.open(str(name), "rb") as source:
            payload = source.read(_MAX_ARCHIVE + 1)
    if len(payload) > _MAX_ARCHIVE:
        raise ReadError("tar archive exceeds the 128 MiB read limit")
    return bytes(payload)


def _bounded_archive(decoded):
    if len(decoded) > _MAX_ARCHIVE:
        raise ReadError("decoded tar archive exceeds the 128 MiB read limit")
    return decoded


def _decode_archive(payload, kind):
    detected = kind
    if detected == "*":
        if payload[:2] == b"\x1f\x8b":
            detected = "gz"
        elif payload[:3] == b"BZh":
            detected = "bz2"
        elif payload[:6] == b"\xfd7zXZ\x00":
            detected = "xz"
        else:
            detected = "tar"
    try:
        if detected == "gz":
            return _bounded_archive(gzip.decompress(payload))
        if detected == "bz2":
            return _bounded_archive(bz2.decompress(payload))
        if detected == "xz":
            return _bounded_archive(lzma.decompress(payload))
        if detected == "tar":
            return _bounded_archive(payload)
        raise CompressionError("unknown tar compression: " + str(kind))
    except (OSError, gzip.BadGzipFile, lzma.LZMAError) as exc:
        raise ReadError("not a readable compressed tar archive: " + str(exc))


def _mode_kind(mode):
    if mode == "r" or mode == "r:*":
        return "*"
    if mode == "r:" or mode == "r:tar":
        return "tar"
    if mode == "r:gz":
        return "gz"
    if mode == "r:bz2":
        return "bz2"
    if mode == "r:xz":
        return "xz"
    if mode.startswith("r|"):
        raise StreamError("streaming tar modes are not runtime-owned")
    raise NotImplementedError("tar archive creation is not runtime-owned")


def _safe_target(root, name):
    if "\x00" in name:
        raise ExtractError("tar member contains a NUL byte")
    normalized = name.replace("\\", "/")
    if normalized.startswith("/"):
        raise ExtractError("absolute tar member path is not safe to extract")
    pieces = []
    target = str(root)
    for piece in normalized.split("/"):
        if piece == "" or piece == ".":
            continue
        if piece == "..":
            raise ExtractError("parent traversal in tar member path")
        pieces.append(piece)
        target = os.path.join(target, piece)
    # Lexical checks alone still follow a pre-existing symlink in the output
    # tree.  Resolve both sides before opening the destination and reject a
    # target outside the resolved extraction root.  The port never creates
    # symlink members, so later members cannot introduce a new escape path.
    resolved_root = os.path.realpath(str(root))
    root_prefix = resolved_root
    if not root_prefix.endswith("/"):
        root_prefix = root_prefix + "/"
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
            raise ExtractError("tar member resolves outside the extraction root")
    return target


def fully_trusted_filter(member, dest_path):
    return member


def tar_filter(member, dest_path):
    _safe_target(dest_path, member.name)
    return member


def data_filter(member, dest_path):
    _safe_target(dest_path, member.name)
    if not member.isfile() and not member.isdir():
        raise ExtractError(
            "tar data filter rejects links and special-file members"
        )
    return member


class TarInfo:
    def __init__(self, name=""):
        self.name = name
        self.mode = 0o644
        self.uid = 0
        self.gid = 0
        self.size = 0
        self.mtime = 0
        self.chksum = 0
        self.type = REGTYPE
        self.linkname = ""
        self.uname = ""
        self.gname = ""
        self.devmajor = 0
        self.devminor = 0
        self.pax_headers = {}
        self.offset = 0
        self.offset_data = 0

    @property
    def path(self):
        return self.name

    def isreg(self):
        return self.type in (REGTYPE, AREGTYPE)

    def isfile(self):
        return self.isreg()

    def isdir(self):
        return self.type == DIRTYPE

    def issym(self):
        return self.type == SYMTYPE

    def islnk(self):
        return self.type == LNKTYPE

    def ischr(self):
        return self.type == CHRTYPE

    def isblk(self):
        return self.type == BLKTYPE

    def isfifo(self):
        return self.type == FIFOTYPE

    def isdev(self):
        return self.ischr() or self.isblk() or self.isfifo()

    def __repr__(self):
        return "<TarInfo " + repr(self.name) + " at " + str(id(self)) + ">"


class TarFile:
    extraction_filter = None

    def __init__(
        self,
        name=None,
        mode="r",
        fileobj=None,
        format=None,
        tarinfo=TarInfo,
        dereference=None,
        ignore_zeros=False,
        encoding="utf-8",
        errors="strict",
        pax_headers=None,
        debug=0,
        errorlevel=1,
        copybufsize=None,
    ):
        self.name = None if name is None else str(name)
        self.mode = mode
        self.closed = False
        self.errorlevel = errorlevel
        self.debug = debug
        self.dereference = dereference
        if tarinfo is not TarInfo:
            raise NotImplementedError("custom TarInfo classes are not runtime-owned")
        if encoding is not None and encoding not in ("utf-8", "UTF-8"):
            raise NotImplementedError("custom tar header encodings are not runtime-owned")
        if errors is not None and errors != "strict":
            raise NotImplementedError("custom tar decode error modes are not runtime-owned")
        archive_kind = _mode_kind(mode)
        self._data = _decode_archive(
            _read_source(name, fileobj), archive_kind
        )
        self.members = []
        self._parse(ignore_zeros)

    @classmethod
    def open(
        cls,
        name=None,
        mode="r",
        fileobj=None,
        bufsize=RECORDSIZE,
        format=None,
        tarinfo=TarInfo,
        dereference=None,
        ignore_zeros=False,
        encoding="utf-8",
        errors="strict",
        pax_headers=None,
        debug=0,
        errorlevel=1,
        copybufsize=None,
    ):
        return cls(
            name=name,
            mode=mode,
            fileobj=fileobj,
            format=format,
            tarinfo=tarinfo,
            dereference=dereference,
            ignore_zeros=ignore_zeros,
            encoding=encoding,
            errors=errors,
            pax_headers=pax_headers,
            debug=debug,
            errorlevel=errorlevel,
            copybufsize=copybufsize,
        )

    def _parse(self, ignore_zeros):
        data = self._data
        offset = 0
        pending_name = None
        pending_link = None
        pending_pax = {}
        global_pax = {}
        while offset < len(data):
            if len(self.members) >= _MAX_ENTRIES:
                raise ReadError("tar archive has too many members")
            if offset + BLOCKSIZE > len(data):
                raise TruncatedHeaderError("truncated tar header")
            block = data[offset:offset + BLOCKSIZE]
            if block == bytes(BLOCKSIZE):
                offset += BLOCKSIZE
                if not ignore_zeros:
                    break
                continue
            stored_checksum = _number(block[148:156])
            if stored_checksum != _checksum(block):
                raise InvalidHeaderError("bad checksum")

            header_size = _number(block[124:136])
            data_offset = offset + BLOCKSIZE
            data_end = data_offset + header_size
            if data_end > len(data):
                raise ReadError("unexpected end of data")
            type_value = block[156:157]
            if type_value == b"":
                type_value = AREGTYPE
            payload = data[data_offset:data_end]
            next_offset = data_offset + ((header_size + 511) // 512) * 512
            if next_offset > len(data):
                raise ReadError("truncated tar member padding")

            if type_value == GNUTYPE_LONGNAME:
                pending_name = _long_text(payload)
                offset = next_offset
                continue
            if type_value == GNUTYPE_LONGLINK:
                pending_link = _long_text(payload)
                offset = next_offset
                continue
            if type_value == XHDTYPE or type_value == XGLTYPE:
                parsed = _parse_pax(payload)
                if type_value == XGLTYPE:
                    global_pax.update(parsed)
                else:
                    pending_pax.update(parsed)
                offset = next_offset
                continue

            prefix = _decode_field(block[345:500])
            short_name = _decode_field(block[0:100])
            name = short_name if not prefix else prefix + "/" + short_name
            if pending_name is not None:
                name = pending_name
            linkname = _decode_field(block[157:257])
            if pending_link is not None:
                linkname = pending_link
            pax = dict(global_pax)
            pax.update(pending_pax)
            for key in pax:
                if key.startswith("GNU.sparse."):
                    raise ReadError(
                        "GNU sparse PAX members are not runtime-owned"
                    )
            if "path" in pax:
                name = pax["path"]
            if "linkpath" in pax:
                linkname = pax["linkpath"]
            member_size = header_size
            if "size" in pax:
                try:
                    member_size = int(pax["size"])
                except ValueError:
                    raise InvalidHeaderError("invalid PAX member size")
                data_end = data_offset + member_size
                next_offset = data_offset + ((member_size + 511) // 512) * 512
                if member_size < 0 or data_end > len(data):
                    raise ReadError("invalid PAX member size")
            if next_offset > len(data):
                raise ReadError("truncated tar member padding")

            # CPython removes only redundant trailing separators from
            # directory members.  Do this after extended-header path
            # overrides are applied; ordinary member text remains byte-for-
            # byte significant and must not be stripped.
            if type_value == DIRTYPE:
                name = name.rstrip("/")

            info = TarInfo(name)
            info.mode = _number(block[100:108])
            info.uid = _number(block[108:116])
            info.gid = _number(block[116:124])
            info.size = member_size
            info.mtime = _number(block[136:148])
            info.chksum = stored_checksum
            info.type = type_value
            info.linkname = linkname
            info.uname = _decode_field(block[265:297])
            info.gname = _decode_field(block[297:329])
            info.devmajor = _number(block[329:337])
            info.devminor = _number(block[337:345])
            info.pax_headers = pax
            info.offset = offset
            info.offset_data = data_offset
            if "mtime" in pax:
                try:
                    info.mtime = float(pax["mtime"])
                except ValueError:
                    raise InvalidHeaderError("invalid PAX mtime")
            if "uid" in pax:
                try:
                    info.uid = int(pax["uid"])
                except ValueError:
                    raise InvalidHeaderError("invalid PAX uid")
            if "gid" in pax:
                try:
                    info.gid = int(pax["gid"])
                except ValueError:
                    raise InvalidHeaderError("invalid PAX gid")
            if "uname" in pax:
                info.uname = pax["uname"]
            if "gname" in pax:
                info.gname = pax["gname"]
            self.members.append(info)
            pending_name = None
            pending_link = None
            pending_pax = {}
            offset = next_offset
        if pending_name is not None or pending_link is not None or pending_pax:
            raise ReadError("tar archive ends with unapplied extended metadata")

    def getmembers(self):
        return list(self.members)

    def getnames(self):
        return [member.name for member in self.members]

    def getmember(self, name):
        index = len(self.members) - 1
        while index >= 0:
            member = self.members[index]
            if member.name == name:
                return member
            index -= 1
        raise KeyError("filename " + repr(name) + " not found")

    def next(self):
        if not hasattr(self, "_next_index"):
            self._next_index = 0
        if self._next_index >= len(self.members):
            return None
        member = self.members[self._next_index]
        self._next_index += 1
        return member

    def __iter__(self):
        return iter(self.members)

    def extractfile(self, member):
        info = member if isinstance(member, TarInfo) else self.getmember(member)
        if not info.isfile():
            if info.isdir():
                return None
            raise NotImplementedError(
                "tar link and special-file reads are not runtime-owned"
            )
        if info.size > _MAX_MEMBER:
            raise ReadError("tar member exceeds the 64 MiB read limit")
        start = info.offset_data
        return io.BytesIO(self._data[start:start + info.size])

    def _filter(self, member, root, selected):
        if selected is None:
            selected = self.extraction_filter
            if selected is None:
                selected = data_filter
        trusted = False
        if isinstance(selected, str):
            if selected == "data":
                selected = data_filter
            elif selected == "tar":
                selected = tar_filter
            elif selected == "fully_trusted":
                selected = fully_trusted_filter
                trusted = True
            else:
                raise ValueError("filter " + repr(selected) + " not found")
        elif selected is fully_trusted_filter:
            trusted = True
        filtered = selected(member, str(root))
        return filtered, trusted

    def _set_attrs(self, member, target):
        # Ownership changes and special permission bits are outside this
        # content-only extractor.  Never materialize setuid/setgid/sticky bits
        # from an untrusted archive, even when they are present in the header.
        if "\0" in target:
            raise ExtractError("tar member target contains NUL")
        if chmod_file(_py_str_utf8(target), int(member.mode) & 0o777) != 0:
            raise ExtractError("could not apply tar member mode")
        times = malloc(16)
        if ptr_is_null(times):
            raise MemoryError("unable to allocate tar timestamp state")
        try:
            timestamp = int(member.mtime)
            # Check the Python integer before narrowing into the existing
            # LP64 utimbuf. Windows applies its narrower FILETIME range in
            # the owned leaf; neither boundary may wrap a large timestamp.
            if timestamp < -9223372036854775808 or timestamp > 9223372036854775807:
                raise ExtractError("could not apply tar member timestamp")
            store_i64(times, 0, timestamp)
            store_i64(times, 8, timestamp)
            status = utime_file(_py_str_utf8(target), times)
            if status == 1:
                raise ExtractError(
                    "tar member timestamp was applied but its handle could not be closed"
                )
            if status != 0:
                raise ExtractError("could not apply tar member timestamp")
        finally:
            free(times)

    def extract(self, member, path="", set_attrs=True, numeric_owner=False,
                filter=None):
        if numeric_owner:
            raise NotImplementedError("numeric tar ownership is not runtime-owned")
        info = member if isinstance(member, TarInfo) else self.getmember(member)
        root = path or "."
        info, trusted = self._filter(info, root, filter)
        if info is None:
            return None
        if trusted:
            target = os.path.join(str(root), info.name)
        else:
            target = _safe_target(root, info.name)
        if info.isdir():
            os.makedirs(target, exist_ok=True)
        elif info.isfile():
            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)
            source = self.extractfile(info)
            with builtins.open(target, "wb") as destination:
                destination.write(source.read())
        else:
            raise NotImplementedError(
                "tar link and special-file extraction is not runtime-owned"
            )
        if set_attrs:
            self._set_attrs(info, target)
        return target

    def extractall(self, path=".", members=None, *, numeric_owner=False,
                   filter=None):
        if numeric_owner:
            raise NotImplementedError("numeric tar ownership is not runtime-owned")
        selected = self.members if members is None else members
        directories = []
        for member in selected:
            info = member if isinstance(member, TarInfo) else self.getmember(member)
            result = self.extract(
                info,
                path=path,
                set_attrs=not info.isdir(),
                numeric_owner=False,
                filter=filter,
            )
            if result is not None and info.isdir():
                directories.append((info, result))
        index = len(directories) - 1
        while index >= 0:
            info, target = directories[index]
            self._set_attrs(info, target)
            index -= 1

    def add(self, name, arcname=None, recursive=True, *, filter=None):
        raise NotImplementedError("tar archive creation is not runtime-owned")

    def addfile(self, tarinfo, fileobj=None):
        raise NotImplementedError("tar archive creation is not runtime-owned")

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


def open(
    name=None,
    mode="r",
    fileobj=None,
    bufsize=RECORDSIZE,
    format=None,
    tarinfo=TarInfo,
    dereference=None,
    ignore_zeros=False,
    encoding="utf-8",
    errors="strict",
    pax_headers=None,
    debug=0,
    errorlevel=1,
    copybufsize=None,
):
    return TarFile.open(
        name=name,
        mode=mode,
        fileobj=fileobj,
        bufsize=bufsize,
        format=format,
        tarinfo=tarinfo,
        dereference=dereference,
        ignore_zeros=ignore_zeros,
        encoding=encoding,
        errors=errors,
        pax_headers=pax_headers,
        debug=debug,
        errorlevel=errorlevel,
        copybufsize=copybufsize,
    )


def is_tarfile(name):
    try:
        archive = open(name)
        archive.close()
        return True
    except (OSError, TarError, NotImplementedError):
        return False


__all__ = [
    "TarError",
    "ReadError",
    "CompressionError",
    "StreamError",
    "ExtractError",
    "HeaderError",
    "TarInfo",
    "TarFile",
    "open",
    "is_tarfile",
    "fully_trusted_filter",
    "tar_filter",
    "data_filter",
    "REGTYPE",
    "AREGTYPE",
    "LNKTYPE",
    "SYMTYPE",
    "DIRTYPE",
]
