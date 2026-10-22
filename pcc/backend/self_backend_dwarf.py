"""Owned DWARF4 source/function records over finalized native text labels.

The line state machine and DIE forms follow https://dwarfstd.org/doc/DWARF4.pdf
sections 3, 6.2 and 7. Address operands carry ordinary Mach-O relocations; no
source coordinate is used to estimate a machine-code address.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import struct

from . import BackendUnavailable
from . import macho_spec as spec
from .macho_obj import Relocation, Section
from .self_backend_debug import (
    DebugLocation, DebugMetadata, DebugSource, FunctionDebugInfo, _reference, _string,
)


@dataclass
class DebugFunctionPlan:
    metadata: DebugMetadata
    function: FunctionDebugInfo
    begin: str
    end: str
    rows: list[tuple[str, DebugLocation | None]] = field(default_factory=list)

    def mark(self, block: int, instruction: int) -> str:
        source = self.function.blocks[block]
        location = (source.terminator if instruction < 0
                    else source.instructions[instruction])
        return self.mark_location(location)

    def mark_location(self, location: DebugLocation | None) -> str:
        # The exact function symbol is already valid for the owned assembler.
        label = "L_pcc_dbg_" + self.begin + "_" + str(len(self.rows))
        self.rows.append((label, location))
        return label + ":"


def function_debug(function):
    if function.type_context is None or function.type_context.debug_info is None:
        return None
    return function.type_context.debug_info.functions.get(function.name)


def has_source_debug(functions) -> bool:
    return any(function_debug(function) is not None for function in functions)


def _uleb(value: int) -> bytes:
    if value < 0:
        raise BackendUnavailable("negative DWARF unsigned value")
    data = []
    while True:
        byte = value & 127
        value >>= 7
        data.append(byte | (128 if value else 0))
        if not value:
            return bytes(data)


def _sleb(value: int) -> bytes:
    data = []
    while True:
        byte = value & 127
        value >>= 7
        done = (value == 0 and byte & 64 == 0) or (value == -1 and byte & 64 != 0)
        data.append(byte | (0 if done else 128))
        if done:
            return bytes(data)


def _cstring(value: str) -> bytes:
    if "\0" in value:
        raise BackendUnavailable("DWARF source text contains NUL")
    return value.encode("utf-8") + b"\0"


def _abbreviation(code: int, tag: int, children: bool, attributes) -> bytes:
    result = _uleb(code) + _uleb(tag) + bytes([int(children)])
    return result + b"".join(_uleb(name) + _uleb(form) for name, form in attributes) + b"\0\0"


# DW_TAG_compile_unit / DW_TAG_subprogram. Inline strings need no debug_str
# offset rewriting; stmt_list and abbrev offsets remain section-relative.
_ABBREVIATIONS = (
    _abbreviation(1, 0x11, True, [
        (0x25, 0x08), (0x13, 0x05), (0x03, 0x08), (0x1B, 0x08), (0x10, 0x17),
    ])
    + _abbreviation(2, 0x2E, False, [
        (0x03, 0x08), (0x6E, 0x08), (0x3A, 0x0F), (0x3B, 0x0F),
        (0x11, 0x01), (0x12, 0x07),
    ])
    + b"\0"
)
_LANGUAGES = {"DW_LANG_C": 2, "DW_LANG_C89": 1, "DW_LANG_C99": 12,
              "DW_LANG_C11": 29, "DW_LANG_Python": 20}


def _final_rows(plan: DebugFunctionPlan, offsets: dict[str, int]):
    if plan.begin not in offsets or plan.end not in offsets:
        raise BackendUnavailable("source debug function has no final text bounds")
    begin, end = offsets[plan.begin], offsets[plan.end]
    if not 0 <= begin < end:
        raise BackendUnavailable("source debug function has invalid final text bounds")
    rows: list[tuple[int, DebugLocation | None]] = [(begin, None)]
    for label, location in plan.rows:
        if label not in offsets:
            raise BackendUnavailable("source debug label is absent from final text")
        address = offsets[label]
        if not rows[-1][0] <= address <= end:
            raise BackendUnavailable("source debug row is outside its final function")
        if address == rows[-1][0]:
            # An instruction that emits no bytes has no independently
            # breakable address. The next actual instruction owns this row.
            rows[-1] = (address, location)
        else:
            rows.append((address, location))
    return [(address, location) for address, location in rows if address < end], end


def _line_unit(plans, source: DebugSource, offsets: dict[str, int]):
    files = [source]
    directories: list[str] = []
    for plan in plans:
        if plan.function.source not in files:
            files.append(plan.function.source)
        for _label, location in plan.rows:
            if location is not None and location.source not in files:
                files.append(location.source)
    for file in files:
        if file.directory and file.directory != source.directory and file.directory not in directories:
            directories.append(file.directory)
    # min instruction length=1, max ops=1, default is_stmt=1, line_base=-5,
    # line_range=14, opcode_base=13 and the twelve standard operand counts.
    header = bytes([1, 1, 1, 251, 14, 13, 0, 1, 1, 1, 1, 0, 0, 0, 1, 0, 0, 1])
    header += b"".join(_cstring(directory) for directory in directories) + b"\0"
    for file in files:
        directory = (directories.index(file.directory) + 1
                     if file.directory in directories else 0)
        header += _cstring(file.filename) + _uleb(directory) + b"\0\0"
    header += b"\0"
    prefix = struct.pack("<HI", 4, len(header)) + header
    program = bytearray()
    relocations = []
    for plan in plans:
        rows, end = _final_rows(plan, offsets)
        begin = offsets[plan.begin]
        program.extend(b"\0\x09\x02")  # DW_LNE_set_address, 8-byte address
        relocations.append(Relocation(
            4 + len(prefix) + len(program), plan.begin,
            type=spec.ARM64_RELOC_UNSIGNED, length=3, pcrel=False,
        ))
        program.extend(b"\0" * 8)
        last_address, last_line, last_file, last_column, is_stmt = begin, 1, 1, 0, True
        for address, location in rows:
            line = location.line if location is not None else 0
            column = location.column if location is not None else 0
            file = files.index(location.source) + 1 if location is not None else 1
            stmt = line > 0
            if address != last_address:
                program.extend(b"\x02" + _uleb(address - last_address))
            if file != last_file:
                program.extend(b"\x04" + _uleb(file))
            if line != last_line:
                program.extend(b"\x03" + _sleb(line - last_line))
            if column != last_column:
                program.extend(b"\x05" + _uleb(column))
            if stmt != is_stmt:
                program.extend(b"\x06")
            if location is not None and location.discriminator:
                discriminator = _uleb(location.discriminator)
                program.extend(b"\0" + _uleb(1 + len(discriminator)) + b"\x04" + discriminator)
            program.extend(b"\x01")  # DW_LNS_copy resets discriminator
            last_address, last_line, last_file, last_column, is_stmt = address, line, file, column, stmt
        program.extend(b"\x02" + _uleb(end - last_address) + b"\0\x01\x01")
    body = prefix + bytes(program)
    return struct.pack("<I", len(body)) + body, relocations, files


def build_dwarf_sections(plans: list[DebugFunctionPlan], offsets: dict[str, int]) -> list[Section]:
    if not plans:
        return []
    groups: dict[int, list[DebugFunctionPlan]] = {}
    for plan in plans:
        groups.setdefault(plan.function.unit, []).append(plan)
    line_chunks, info_chunks = [], []
    line_relocations, info_relocations = [], []
    line_size, info_size = 0, 0
    for unit, functions in groups.items():
        debug = functions[0].metadata
        node = debug.node(unit, "DICompileUnit")
        source = debug.source(_reference(node.fields.get("file", ""), "compile unit file"))
        language = _LANGUAGES.get(node.fields.get("language", ""))
        if language is None:
            raise BackendUnavailable("owned DWARF compile unit language is unsupported")
        producer = _string(node.fields.get("producer", '"pcc"'), "producer")
        line_data, relocations, files = _line_unit(functions, source, offsets)
        for relocation in relocations:
            line_relocations.append(Relocation(
                line_size + relocation.offset, relocation.symbol,
                type=spec.ARM64_RELOC_UNSIGNED, length=3, pcrel=False,
            ))
        info = bytearray(struct.pack("<HIB", 4, 0, 8))
        info.extend(b"\x01" + _cstring(producer) + struct.pack("<H", language))
        info.extend(_cstring(source.filename) + _cstring(source.directory) + struct.pack("<I", line_size))
        for plan in functions:
            info.extend(b"\x02" + _cstring(plan.function.name) + _cstring(plan.begin))
            info.extend(_uleb(files.index(plan.function.source) + 1) + _uleb(plan.function.line))
            info_relocations.append(Relocation(
                info_size + 4 + len(info), plan.begin,
                type=spec.ARM64_RELOC_UNSIGNED, length=3, pcrel=False,
            ))
            info.extend(b"\0" * 8 + struct.pack("<Q", offsets[plan.end] - offsets[plan.begin]))
        info.extend(b"\0")
        line_chunks.append(line_data)
        info_chunks.append(struct.pack("<I", len(info)) + bytes(info))
        line_size += len(line_data)
        info_size += 4 + len(info)
    return [
        Section("__debug_abbrev", "__DWARF", _ABBREVIATIONS, align_log2=0, flags=spec.S_ATTR_DEBUG),
        Section("__debug_info", "__DWARF", b"".join(info_chunks), align_log2=0,
                flags=spec.S_ATTR_DEBUG, relocations=tuple(info_relocations)),
        Section("__debug_line", "__DWARF", b"".join(line_chunks), align_log2=0,
                flags=spec.S_ATTR_DEBUG, relocations=tuple(line_relocations)),
    ]


def render_dwarf_sections(sections: list[Section]) -> list[str]:
    lines = []
    for section in sections:
        lines.append(".section __DWARF," + section.sectname + ",regular,debug")
        relocations = {relocation.offset: relocation for relocation in section.relocations}
        offset = 0
        while offset < len(section.data):
            relocation = relocations.get(offset)
            if relocation is not None:
                lines.append(".quad " + relocation.symbol)
                offset += 8
            else:
                end = min(offset + 16, len(section.data))
                while any(position in relocations for position in range(offset + 1, end)):
                    end -= 1
                lines.append(".byte " + ", ".join(str(byte) for byte in section.data[offset:end]))
                offset = end
    return lines
