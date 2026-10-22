"""Validated, rereadable ELF inputs with one resident immutable ELF payload.

Symbols, section descriptors and private validated file ranges are retained for
resolution/layout. Object and archive payloads are fully parsed once, released,
then reread with exact length/SHA checks before projecting the validated ranges.
Assembly retains its existing reassembly path. This is bounded payload
retention, not constant-space linking: symbols and the final image remain resident.
"""

from dataclasses import dataclass
import hashlib
import os
import struct

from . import elf_x86_64 as elf

_HASH_CHUNK_BYTES = 1024 * 1024


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(_HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class _ClassificationProof:
    # Kept with one source's original digest and immutable validated ranges.
    sha256: str
    size: int
    section_ranges: tuple
    section_bits: tuple


@dataclass(frozen=True)
class _InputSource:
    path: str
    offset: int
    size: int
    sha256: str
    label: str
    kind: str
    whole_file: bool
    section_ranges: tuple | None = None
    classification_proof: _ClassificationProof | None = None


@dataclass(frozen=True)
class _InputSection:
    store: object
    source_index: int
    section_index: int
    name: str
    type: int
    flags: int
    align: int
    file_size: int
    mem_size: int
    relocation_count: int

    @property
    def size(self) -> int:
        return self.mem_size if self.type == elf.SHT_NOBITS else self.file_size

    @property
    def data(self):
        if not self.file_size:
            return b""
        payload = self.store.load(self.source_index)
        ranges = self.store.sources[self.source_index].section_ranges
        if ranges is None:  # Assembly keeps its validated reassembly path.
            return payload.sections[self.section_index].data
        offset, size, _rela_offset, _rela_size = ranges[self.section_index]
        return payload[offset:offset + size]

    @property
    def relocations(self):
        if not self.relocation_count:
            return ()
        payload = self.store.load(self.source_index)
        ranges = self.store.sources[self.source_index].section_ranges
        if ranges is None:
            return payload.sections[self.section_index].relocations
        _offset, _size, rela_offset, rela_size = ranges[self.section_index]
        return elf._PackedElfRelocations(payload, rela_offset, rela_size)


@dataclass(frozen=True)
class _InputObject:
    # Phase metadata from an already validated ElfObject, never an unchecked
    # alternative public ElfObject constructor. ELF reloads verify exact bytes
    # before reusing that validation; assembly reloads still use the same parser.
    sections: tuple
    symbols: tuple
    machine: int


def _archive_ranges(path: str):
    """Index owned GNU/BSD/COFF ar names without retaining member payloads."""
    size = os.path.getsize(path)
    rows = []
    gnu_names = b""
    with open(path, "rb") as stream:
        if stream.read(8) != elf._AR_MAGIC:
            raise elf.ElfError("not an ar archive")
        offset = 8
        while offset < size:
            if offset + elf._AR_HEADER_SIZE > size:
                raise elf.ElfError(f"truncated ar header at {offset}")
            stream.seek(offset)
            header = stream.read(elf._AR_HEADER_SIZE)
            if len(header) != elf._AR_HEADER_SIZE:
                raise elf.ElfError(f"truncated ar header at {offset}")
            if header[58:60] != b"`\n":
                raise elf.ElfError(f"bad ar member magic at {offset}")
            try:
                length = int(header[48:58].decode("ascii").strip())
            except (UnicodeDecodeError, ValueError) as exc:
                raise elf.ElfError(f"bad ar member size at {offset}") from exc
            body = offset + elf._AR_HEADER_SIZE
            end = body + length
            if length < 0 or end > size:
                raise elf.ElfError(f"ar member at {offset} runs past end of archive")
            raw_name = header[:16].rstrip()
            offset = end + (end & 1)
            if raw_name == b"//":
                gnu_names = stream.read(length)
                if len(gnu_names) != length:
                    raise elf.ElfError("staged ELF archive string table became truncated")
                continue
            if raw_name in (b"/", b"/SYM64/") or raw_name.startswith(b"__.SYMDEF"):
                continue
            if raw_name.startswith(b"#1/"):
                try:
                    name_length = int(raw_name[3:])
                except ValueError as exc:
                    raise elf.ElfError("bad BSD ar extended name") from exc
                if name_length < 0 or name_length > length:
                    raise elf.ElfError("BSD ar extended name exceeds member")
                name_bytes = stream.read(name_length)
                if len(name_bytes) != name_length:
                    raise elf.ElfError("staged ELF archive member name became truncated")
                name = name_bytes.rstrip(b"\0").decode("utf-8", "surrogateescape")
                body += name_length
                length -= name_length
            elif raw_name.startswith(b"/") and raw_name[1:].isdigit():
                if not gnu_names:
                    raise elf.ElfError("GNU ar member uses a missing string table")
                name_offset = int(raw_name[1:])
                if name_offset >= len(gnu_names):
                    raise elf.ElfError("GNU ar member name offset is out of range")
                name_end = gnu_names.find(b"/\n", name_offset)
                if name_end < 0:
                    name_end = gnu_names.find(b"\0", name_offset)
                if name_end < 0:
                    raise elf.ElfError("ar long member name is unterminated")
                name = gnu_names[name_offset:name_end].decode("utf-8", "surrogateescape")
            else:
                name = raw_name.rstrip(b"/").decode("utf-8", "surrogateescape")
            if not name.startswith("__.SYMDEF"):
                rows.append((name, body, length))
    return rows


class ElfInputStore:
    """One link scope; at most one reload cache, plus validated metadata."""

    def __init__(self, target: str, archives=()) -> None:
        self.target = target
        self.archives = list(archives)
        self.sources = []
        self.archive_hashes = []
        self.member_hashes = []
        self.duplicate_member_names = []
        self.current_index = -1
        self.current = None
        self.current_classification_proof = None
        self.current_classification_payload = None

    def clear(self) -> None:
        self.current = None
        self.current_index = -1
        self.current_classification_proof = None
        self.current_classification_payload = None

    def _read(self, source):
        with open(source.path, "rb") as stream:
            stream.seek(source.offset)
            data = stream.read(source.size)
            if len(data) != source.size:
                raise elf.ElfError("staged ELF input became truncated: " + source.label)
            # For a direct object/ASM, an append also changes the input. Archive
            # members have bounded ranges; the complete archive is hashed too.
            if source.whole_file and stream.read(1):
                raise elf.ElfError("staged ELF input size changed: " + source.label)
        digest = hashlib.sha256(data).hexdigest()
        if source.sha256 and digest != source.sha256:
            raise elf.ElfError("staged ELF input identity changed: " + source.label)
        return data, digest

    def _parse(self, data, kind):
        if kind == "ASM":
            from .owned_elf_link import assemble
            # Match open(..., encoding='utf-8') universal-newline semantics.
            text = data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
            return assemble(text, self.target)
        return elf.parse_relocatable(data, compact_relocations=True)

    def stage(self, path: str, *, kind: str = "PCO", offset: int = 0,
              size=None, label: str = ""):
        self.clear()
        whole_file = size is None
        if whole_file:
            size = os.path.getsize(path)
        source = _InputSource(path, offset, size, "", label or path, kind, whole_file)
        data, digest = self._read(source)
        ranges = None
        proof = None
        if kind == "ASM":
            obj = self._parse(data, kind)
        else:
            summaries = None
            if type(kind) is str and kind == "PCO":
                try:
                    summaries = []
                except MemoryError:
                    pass
            obj, ranges = elf._parse_relocatable_indexed(
                data, _classification_summaries=summaries,
            )
            try:
                if (type(obj) is elf.ElfObject and type(obj.machine) is int
                        and obj.machine == elf.EM_AARCH64 and type(summaries) is list
                        and len(summaries) == len(ranges)):
                    proof = _ClassificationProof(digest, size, ranges, tuple(summaries))
            except MemoryError:
                proof = None
        del data
        index = len(self.sources)
        self.sources.append(_InputSource(
            path, offset, size, digest, source.label, kind, whole_file, ranges, proof,
        ))
        sections = tuple(
            _InputSection(self, index, si, section.name, section.type, section.flags,
                          section.align, len(section.data), section.mem_size,
                          len(section.relocations))
            for si, section in enumerate(obj.sections)
        )
        return _InputObject(sections, obj.symbols, obj.machine)

    def load(self, index: int):
        if self.current_index == index:
            return self.current
        self.clear()
        source = self.sources[index]
        data, _digest = self._read(source)
        proof = None
        try:
            if (type(data) is bytes and type(_digest) is str
                    and type(source) is _InputSource and type(source.kind) is str and source.kind == "PCO"
                    and type(source.classification_proof) is _ClassificationProof):
                candidate = source.classification_proof
                if (type(candidate.sha256) is str and candidate.sha256 == _digest
                        and type(candidate.size) is int and candidate.size == len(data)
                        and candidate.section_ranges is source.section_ranges
                        and type(candidate.section_ranges) is tuple
                        and type(candidate.section_bits) is tuple):
                    proof = candidate
        except MemoryError:
            proof = None
        # No indexed byte can be consumed until the complete source has passed
        # the same exact-length and SHA check. Only this one immutable payload
        # is cached; the index retains neither payloads nor relocation objects.
        obj = self._parse(data, source.kind) if source.kind == "ASM" else data
        del data
        self.current = obj
        self.current_index = index
        self.current_classification_proof = proof
        self.current_classification_payload = obj if proof is not None else None
        return obj

    def read_archive(self, archive_index: int):
        path = self.archives[archive_index]
        digest = file_sha256(path)
        members = []
        hashes = {}
        duplicate_names = False
        for name, offset, size in _archive_ranges(path):
            try:
                obj = self.stage(path, offset=offset, size=size, label=path + "(" + name + ")")
            except elf.ElfError as exc:
                raise elf.ElfError(f"archive member {name!r} is not a proven ELF object: {exc}") from exc
            if name in hashes:
                duplicate_names = True
            hashes[name] = self.sources[-1].sha256
            defined, undefined = elf._object_symbol_sets(obj)
            members.append(elf.ElfArchiveMember(name, obj, defined, undefined))
        if file_sha256(path) != digest:
            raise elf.ElfError("staged ELF archive identity changed during index: " + path)
        self.archive_hashes.append(digest)
        self.member_hashes.append(hashes)
        self.duplicate_member_names.append(duplicate_names)
        return tuple(members)

    def verify(self) -> None:
        # The current cache is unnecessary during final validation/publication.
        self.clear()
        for source in self.sources:
            if not source.whole_file:
                continue  # Complete archive identity also covers unused members.
            digest = hashlib.sha256()
            with open(source.path, "rb") as stream:
                remaining = source.size
                while remaining:
                    chunk = stream.read(min(_HASH_CHUNK_BYTES, remaining))
                    if not chunk:
                        raise elf.ElfError("staged ELF input became truncated: " + source.label)
                    digest.update(chunk)
                    remaining -= len(chunk)
                if stream.read(1):
                    raise elf.ElfError("staged ELF input size changed: " + source.label)
            if digest.hexdigest() != source.sha256:
                raise elf.ElfError("staged ELF input identity changed: " + source.label)
        for path, digest in zip(self.archives, self.archive_hashes):
            if file_sha256(path) != digest:
                raise elf.ElfError("staged ELF archive identity changed: " + path)


def _classification_is_empty(store, owner, section_index, section, relocations) -> bool:
    """Consume only a completed staged proof, after the original checked load."""
    if (type(store) is not ElfInputStore or type(owner) is not _InputObject
            or type(owner.machine) is not int or owner.machine != elf.EM_AARCH64
            or type(owner.sections) is not tuple or type(section_index) is not int
            or not 1 <= section_index <= len(owner.sections)
            or owner.sections[section_index - 1] is not section
            or type(section) is not _InputSection
            or section.store is not store or type(store.sources) is not list
            or type(section.source_index) is not int
            or not 0 <= section.source_index < len(store.sources)
            or type(section.section_index) is not int):
        return False
    source = store.sources[section.source_index]
    if type(source) is not _InputSource or type(source.kind) is not str or source.kind != "PCO":
        return False
    proof = source.classification_proof
    if (type(proof) is not _ClassificationProof
            or getattr(store, "current_classification_proof", None) is not proof
            or type(source.sha256) is not str or type(proof.sha256) is not str
            or source.sha256 != proof.sha256
            or type(source.size) is not int or type(proof.size) is not int
            or source.size != proof.size
            or source.section_ranges is not proof.section_ranges
            or type(proof.section_ranges) is not tuple
            or type(proof.section_bits) is not tuple
            or len(proof.section_ranges) != len(proof.section_bits)
            or not 0 <= section.section_index < len(proof.section_bits)
            or proof.section_bits[section.section_index] is not True):
        return False
    row = proof.section_ranges[section.section_index]
    if (type(row) is not tuple or len(row) != 4
            or type(row[2]) is not int or type(row[3]) is not int
            or type(relocations) is not elf._PackedElfRelocations
            or type(relocations.payload) is not bytes
            or type(relocations.offset) is not int or type(relocations.size) is not int
            or type(elf._RELA) is not struct.Struct or elf._RELA.format != "<QQq"
            or type(store.current_index) is not int
            or store.current_index != section.source_index
            or store.current is not relocations.payload
            or store.current_classification_payload is not relocations.payload):
        return False
    return relocations.offset == row[2] and relocations.size == row[3]
