"""Static archive (`.a`) reading and member selection.

The remaining half of LINK-P1-MACHO-LINK-STATIC and the prerequisite for
routing pcc's own link path at the runtime archive: an archive is not a bag
of objects, it is a *pool* the linker draws from, and which members it pulls
is a semantic decision — pull a member only when it defines a symbol that is
currently undefined, then repeat, because a newly pulled member can itself
reference something no one had needed yet.

Format handled: Darwin BSD `ar` — the `!<arch>` magic, 60-byte member
headers, `#1/<n>` extended names (the name follows the header and is counted
in the member size), and the `__.SYMDEF`/`__.SYMDEF SORTED` table of
contents, which is skipped rather than trusted: pcc reads each member's own
symbol table, so a stale or absent index cannot change what gets linked.

Fail closed on a non-archive, a truncated member, or a member that is not a
Mach-O object.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import macho_spec as spec
from .macho_parallel import ParallelLinkError, ordered_parallel_map

ARMAG = b"!<arch>\n"
_HEADER_SIZE = 60
_SYMDEF_NAMES = (b"__.SYMDEF", b"__.SYMDEF SORTED", b"__.SYMDEF_64",
                 b"__.SYMDEF_64 SORTED")


class ArchiveError(Exception):
    """The archive is outside the format pcc reads."""


@dataclass(frozen=True)
class Member:
    name: str
    data: bytes
    defines: frozenset[str]
    undefined: frozenset[str]


@dataclass(frozen=True)
class _PendingMember:
    name: str
    data: bytes


def _member_symbols(data: bytes) -> tuple[frozenset[str], frozenset[str]]:
    obj = spec.parse_object(data)
    defines, undefined = set(), set()
    for sym in obj.symbols():
        kind = sym["n_type"] & spec.N_TYPE
        if not sym["n_type"] & spec.N_EXT:
            continue
        if kind == spec.N_SECT:
            defines.add(sym["name"])
        elif kind == spec.N_UNDF and not (sym["n_desc"] & spec.N_WEAK_REF):
            undefined.add(sym["name"])
    return frozenset(defines), frozenset(undefined)


def _inspect_member(member: _PendingMember) -> Member:
    try:
        defines, undefined = _member_symbols(member.data)
    except spec.MachOFormatError as exc:
        raise ArchiveError(
            f"member {member.name!r} is not a valid Mach-O object: {exc}"
        ) from exc
    return Member(member.name, member.data, defines, undefined)


def read_archive(data: bytes) -> list[Member]:
    """Parse an archive into its object members, in file order."""
    if not data.startswith(ARMAG):
        raise ArchiveError("not an ar archive (bad magic)")
    pending_members: list[_PendingMember] = []
    offset = len(ARMAG)
    while offset < len(data):
        if offset + _HEADER_SIZE > len(data):
            raise ArchiveError(f"truncated member header at {offset}")
        header = data[offset:offset + _HEADER_SIZE]
        if header[58:60] != b"`\n":
            raise ArchiveError(f"bad member header magic at {offset}")
        raw_name = header[0:16].rstrip()
        try:
            size = int(header[48:58].decode().strip())
        except ValueError as exc:
            raise ArchiveError(f"bad member size at {offset}") from exc
        if size < 0:
            # Skipped indexes must still advance past their own header.
            raise ArchiveError(f"negative member size at {offset}")
        body = offset + _HEADER_SIZE
        if body + size > len(data):
            raise ArchiveError(f"member at {offset} runs past end of archive")

        name_len = 0
        if raw_name.startswith(b"#1/"):
            try:
                name_len = int(raw_name[3:].decode())
            except ValueError as exc:
                raise ArchiveError(f"bad extended name at {offset}") from exc
            if name_len < 0 or name_len > size:
                raise ArchiveError(f"bad extended name length at {offset}")
            name = data[body:body + name_len].rstrip(b"\0").decode(
                "utf-8", "surrogateescape"
            )
        else:
            name = raw_name.rstrip(b"/").decode("utf-8", "surrogateescape")

        payload = data[body + name_len:body + size]
        offset = body + size
        if offset % 2:
            offset += 1

        if name.encode() in _SYMDEF_NAMES or name.startswith("__.SYMDEF"):
            # The table of contents is an index, not a source of truth: each
            # member's own symbol table is read instead, so a stale index
            # cannot silently change which members get pulled.
            continue
        if len(payload) < spec.MACH_HEADER_64.size:
            raise ArchiveError(f"member {name!r} is too small to be an object")
        pending_members.append(_PendingMember(name, payload))

    # Header walking fixes exact archive order and member ownership first.
    # Symbol extraction is independent per member, so workers fill indexed
    # result slots and never race on an append buffer.  The lowest malformed
    # member remains the reported error regardless of completion order.
    try:
        return ordered_parallel_map(
            pending_members,
            _inspect_member,
            total_bytes=sum(len(member.data) for member in pending_members),
        )
    except ParallelLinkError as exc:
        raise ArchiveError(f"parallel archive inspection failed: {exc}") from exc


def select_members(
    members: list[Member],
    undefined: set[str],
    *,
    already_defined: set[str] | frozenset[str] = frozenset(),
    selected_members: list[Member] | None = None,
) -> tuple[list[bytes], set[str]]:
    """Pull members that satisfy pending undefined symbols, repeatedly.

    `already_defined` names symbols supplied by explicit objects or earlier
    archives. Returns (pulled objects in archive order, symbols still
    undefined).
    The repeated scan is the whole point: a member pulled in round N can
    reference a symbol only defined by a member earlier in the archive, which
    a single forward pass would already have walked past.
    """
    pending = set(undefined)
    taken: set[int] = set()
    # A selected member may refer back to a symbol supplied by an explicit
    # object (or by an earlier archive).  Such a reference is already
    # satisfied and must not become pending again: doing so can pull an
    # otherwise-unused archive member that defines the same symbol, turning a
    # valid link into a spurious duplicate-definition failure.
    provided: set[str] = set(already_defined)

    changed = True
    while changed:
        changed = False
        for index, member in enumerate(members):
            if index in taken:
                continue
            if not (member.defines & pending):
                continue
            taken.add(index)
            provided |= member.defines
            pending -= member.defines
            pending |= member.undefined - provided
            changed = True

    ordered = [m for i, m in enumerate(members) if i in taken]
    if selected_members is not None:
        selected_members.extend(ordered)
    return [m.data for m in ordered], pending
