"""Read regular Unix archives without an external archiver or extraction."""

from __future__ import annotations


class ArchiveFormatError(ValueError):
    pass


_MAGIC = b"!<arch>\n"
_METADATA_NAMES = frozenset({b"/", b"//", b"/SYM64/"})


def read_members(data: bytes) -> list[tuple[str, bytes]]:
    """Return object payloads in archive order, supporting BSD and GNU names.

    Symbol indexes and the GNU filename table are metadata. Member names are
    returned as data, never interpreted as filesystem paths. Thin archives
    are rejected because their payloads live outside the supplied bytes.
    """
    if not data.startswith(_MAGIC):
        raise ArchiveFormatError("not a regular ar archive (bad magic)")
    headers: list[tuple[bytes, int, int]] = []
    name_table = b""
    position = len(_MAGIC)
    while position < len(data):
        if position + 60 > len(data):
            raise ArchiveFormatError("truncated archive member header")
        header = data[position:position + 60]
        if header[58:60] != b"`\n":
            raise ArchiveFormatError("bad archive member header magic")
        try:
            size = int(header[48:58].decode("ascii").strip())
        except ValueError as exc:
            raise ArchiveFormatError("invalid archive member size") from exc
        if size < 0:
            raise ArchiveFormatError("negative archive member size")
        start = position + 60
        end = start + size
        if end > len(data):
            raise ArchiveFormatError("archive member runs past end of input")
        raw_name = header[:16].rstrip()
        if raw_name == b"//":
            name_table = data[start:end]
        headers.append((raw_name, start, end))
        position = end + size % 2
        if position > len(data):
            raise ArchiveFormatError("truncated archive member padding")

    members: list[tuple[str, bytes]] = []
    for raw_name, start, end in headers:
        if raw_name in _METADATA_NAMES:
            continue
        if raw_name.startswith(b"#1/"):
            try:
                name_length = int(raw_name[3:].decode("ascii"))
            except ValueError as exc:
                raise ArchiveFormatError("invalid BSD archive filename length") from exc
            if name_length < 1 or name_length > end - start:
                raise ArchiveFormatError("BSD archive filename exceeds member")
            name_bytes = data[start:start + name_length].rstrip(b"\0")
            start += name_length
        elif raw_name.startswith(b"/"):
            try:
                offset = int(raw_name[1:].decode("ascii"))
            except ValueError as exc:
                raise ArchiveFormatError("invalid GNU archive filename offset") from exc
            if offset < 0 or offset >= len(name_table):
                raise ArchiveFormatError("GNU archive filename offset outside table")
            name_end = name_table.find(b"\n", offset)
            if name_end < 0:
                raise ArchiveFormatError("unterminated GNU archive filename")
            name_bytes = name_table[offset:name_end].rstrip(b"/")
        else:
            name_bytes = raw_name.rstrip(b"/")
        name = name_bytes.decode("utf-8", "surrogateescape")
        if not name:
            raise ArchiveFormatError("empty archive member name")
        if name.startswith("__.SYMDEF"):
            continue
        members.append((name, data[start:end]))
    return members
