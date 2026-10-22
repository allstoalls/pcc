"""Owned Darwin arm64 shared-library publication.

Reuse the owned linker's relocation, import, TLS and section layout, then
publish that layout as MH_DYLIB with a dyld export trie and a fresh signature.
Only libSystem imports are supported; other library inputs fail explicitly.
"""

from __future__ import annotations

import ctypes
import hashlib
import platform
import struct
import sys

from . import BackendUnavailable, macho_spec as spec
from .macho_codesign import _signature_size, build_signature, parse_signature
from .macho_exec import LIBSYSTEM, PAGE, TEXT_BASE, link_executable, prepare_executable_object
from .macho_obj import Section, TextSymbol, TEXT_SECTION_FLAGS
from .native_object import NativeObject
from .self_backend_target_match import is_aarch64_darwin_triple


_LC_ID_DYLIB = 0xD
_LIBSYSTEM_NAMES = frozenset({"System", "c", "m", "pthread", "dl"})


def validate_target(target):
    if (not is_aarch64_darwin_triple(target) or platform.system() != "Darwin"
            or platform.machine() != "arm64" or sys.implementation.name != "cpython"):
        raise BackendUnavailable("owned shared libraries currently require a CPython Darwin arm64 host")


def validate_link_args(link_args):
    """Darwin's C/math/thread/dl libraries are constituents of libSystem."""
    for argument in link_args:
        if argument.startswith("-l") and argument[2:] in _LIBSYSTEM_NAMES:
            continue
        raise BackendUnavailable("owned Darwin linking does not support link argument " + repr(argument))


def _uleb(value):
    if value < 0:
        raise ValueError("negative Mach-O export value")
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def _exports_trie(exports):
    # Radix edges keep arbitrarily long symbol names out of the recursion
    # stack. Node offsets use a small monotone fixed-point calculation.
    nodes = [{"terminal": None, "children": []}]
    pending = [(0, sorted(exports))]
    while pending:
        index, rows = pending.pop()
        groups = {}
        for name, address, flags in rows:
            if not name:
                nodes[index]["terminal"] = _uleb(flags) + _uleb(address)
            else:
                groups.setdefault(name[0], []).append((name, address, flags))
        for key in sorted(groups):
            group = groups[key]
            first, last = group[0][0], group[-1][0]
            length = 0
            while length < min(len(first), len(last)) and first[length] == last[length]:
                length += 1
            child = len(nodes)
            nodes.append({"terminal": None, "children": []})
            nodes[index]["children"].append((first[:length], child))
            pending.append((child, [(name[length:], address, flags) for name, address, flags in group]))
    offsets = [0] * len(nodes)
    while True:
        encoded = []
        for node in nodes:
            terminal = node["terminal"] or b""
            children = node["children"]
            if len(children) > 255:
                raise BackendUnavailable("Mach-O export node exceeds the dyld child limit")
            record = _uleb(len(terminal)) + terminal + bytes([len(children)])
            for edge, child in children:
                record += edge + b"\0" + _uleb(offsets[child])
            encoded.append(record)
        current = 0
        updated = []
        for record in encoded:
            updated.append(current)
            current += len(record)
        if updated == offsets:
            return b"".join(encoded)
        offsets = updated


def _check_imports(merged):
    library = ctypes.CDLL(LIBSYSTEM.decode("ascii"))
    dlsym = ctypes.CDLL(None).dlsym
    dlsym.argtypes = (ctypes.c_void_p, ctypes.c_char_p)
    dlsym.restype = ctypes.c_void_p
    for symbol in merged.symbols:
        if symbol.section_index or symbol.weak_reference:
            continue
        name = symbol.name[1:] if symbol.name.startswith("_") else symbol.name
        if not dlsym(library._handle, name.encode("utf-8")):
            raise BackendUnavailable("owned shared library has an unsupported libSystem import: " + symbol.name)


def _with_text_entry(merged):
    entries = [symbol for symbol in merged.symbols
               if symbol.section_index
               and merged.sections[symbol.section_index - 1].segname == "__TEXT"
               and merged.sections[symbol.section_index - 1].flags & spec.S_ATTR_PURE_INSTRUCTIONS]
    if not entries:
        # The common executable layout requires a code entry. A data-only
        # dylib has none, so add a private, owned RET solely for that layout;
        # publication removes LC_MAIN and never exports this linker symbol.
        names = {symbol.name for symbol in merged.symbols}
        name = "__pcc_shared_link_entry"
        while name in names:
            name += "_"
        anchor = NativeObject.from_sections([Section(
            sectname="__text", segname="__TEXT", flags=TEXT_SECTION_FLAGS,
            align_log2=2, data=struct.pack("<I", 0xD65F03C0),
            symbols=(TextSymbol(name, 0, external=False),),
        )])
        merged = prepare_executable_object([merged, anchor])
        entries = [symbol for symbol in merged.symbols
                   if symbol.section_index
                   and merged.sections[symbol.section_index - 1].segname == "__TEXT"
                   and merged.sections[symbol.section_index - 1].flags & spec.S_ATTR_PURE_INSTRUCTIONS]
    entries.sort(key=lambda symbol: not symbol.external)
    return merged, entries[0].name


def link_shared_library(objects, *, target, identity, link_args=()):
    """Return a signed, loadable Mach-O dylib, without an external linker."""
    validate_target(target)
    validate_link_args(link_args)
    merged = prepare_executable_object(objects)
    definitions = [symbol for symbol in merged.symbols if symbol.section_index]
    if not definitions:
        raise BackendUnavailable("owned shared library requires at least one concrete definition")
    merged, entry = _with_text_entry(merged)
    _check_imports(merged)
    token = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    identifier = ("pcc-shared-" + token).encode("ascii")
    install_name = ("@rpath/libpcc_" + token + ".dylib").encode("ascii")
    executable = link_executable([merged], entry=entry, identifier=identifier)
    obj = spec.parse_object(executable)
    signature = parse_signature(executable)
    image = bytearray(executable[:signature.dataoff])

    exports = []
    sections = obj.sections()
    symtab = next(command for command in obj.commands if command.cmd == spec.LC_SYMTAB)
    sym_fields = spec.SYMTAB_COMMAND.unpack(symtab.raw)
    for index, symbol in enumerate(obj.symbols()):
        if symbol["n_type"] & spec.N_TYPE != spec.N_SECT:
            continue
        address = symbol["n_value"] - TEXT_BASE
        section = sections[symbol["n_sect"] - 1]
        flags = 1 if section["flags"] & spec.SECTION_TYPE == 0x13 else 0
        if symbol["n_type"] & spec.N_EXT and not symbol["n_type"] & spec.N_PEXT:
            exports.append((symbol["name"].encode("utf-8"), address, flags))
        # nlist section indices count sections, not segments, and survive
        # removal of the section-free __PAGEZERO segment unchanged.
        struct.pack_into("<Q", image, sym_fields["symoff"] + index * spec.NLIST_64.size + 8, address)
    trie = _exports_trie(exports)
    trie_offset = len(image)
    image += trie
    image += b"\0" * (-len(image) % 16)
    signature_offset = len(image)
    signature_size = _signature_size(signature_offset, identifier)
    final_size = signature_offset + signature_size

    commands = []
    for command in obj.commands:
        if command.cmd in (spec.LC_MAIN, spec.LC_LOAD_DYLINKER):
            continue
        if command.cmd == spec.LC_SEGMENT_64:
            fields = spec.SEGMENT_COMMAND_64.unpack(command.raw)
            name = fields["segname"].rstrip(b"\0")
            if name == b"__PAGEZERO":
                continue
            fields["vmaddr"] -= TEXT_BASE
            if name == b"__LINKEDIT":
                fields["filesize"] = final_size - fields["fileoff"]
                fields["vmsize"] = (fields["filesize"] + PAGE - 1) & -PAGE
            raw = spec.SEGMENT_COMMAND_64.pack(fields)
            for index in range(fields["nsects"]):
                start = spec.SEGMENT_COMMAND_64.size + index * spec.SECTION_64.size
                section = spec.SECTION_64.unpack(command.raw[start:start + spec.SECTION_64.size])
                section["addr"] -= TEXT_BASE
                raw += spec.SECTION_64.pack(section)
            commands.append(raw)
        elif command.cmd == spec.LC_DYLD_CHAINED_FIXUPS:
            data_offset = struct.unpack_from("<I", command.raw, 8)[0]
            starts = data_offset + struct.unpack_from("<I", image, data_offset + 4)[0]
            count = struct.unpack_from("<I", image, starts)[0]
            offsets = struct.unpack_from("<" + str(count) + "I", image, starts + 4)
            if not count or offsets[0] != 0:
                raise BackendUnavailable("owned shared library has an invalid PAGEZERO fixup table")
            # Keep unused padding so all starts-in-segment offsets stay valid.
            struct.pack_into("<I", image, starts, count - 1)
            struct.pack_into("<" + str(count - 1) + "I", image, starts + 4, *offsets[1:])
            commands.append(command.raw)
        elif command.cmd == spec.LC_CODE_SIGNATURE:
            commands.append(struct.pack("<IIII", spec.LC_CODE_SIGNATURE, 16, signature_offset, signature_size))
        else:
            commands.append(command.raw)
    size = (24 + len(install_name) + 1 + 7) & -8
    commands.append(struct.pack("<6I", _LC_ID_DYLIB, size, 24, 0, 0x10000, 0x10000)
                    + (install_name + b"\0").ljust(size - 24, b"\0"))
    commands.append(struct.pack("<4I", spec.LC_DYLD_EXPORTS_TRIE, 16, trie_offset, len(trie)))
    raw_commands = b"".join(commands)
    first_payload = min(section["offset"] for section in sections if section["offset"])
    if spec.MACH_HEADER_64.size + len(raw_commands) > first_payload:
        raise BackendUnavailable("owned shared-library commands exceed the reserved header space")
    header = dict(obj.header)
    header["filetype"] = spec.MH_DYLIB
    header["flags"] = (header["flags"] & ~0x200000) | 0x100000
    header["ncmds"] = len(commands)
    header["sizeofcmds"] = len(raw_commands)
    image[:first_payload] = (spec.MACH_HEADER_64.pack(header) + raw_commands).ljust(first_payload, b"\0")
    image += build_signature(memoryview(image), identifier=identifier,
                             exec_seg_base=signature.exec_seg_base,
                             exec_seg_limit=signature.exec_seg_limit, exec_seg_flags=0)
    if len(image) != final_size:
        raise ValueError("owned shared-library signature size mismatch")
    return bytes(image)
