"""Link a pcc-produced executable: dyld surface, stubs, GOT, entry point.

LINK-P1-MACHO-LINK-DYLD. `macho_link` merges objects and resolves symbols;
this module turns the merged object into an `MH_EXECUTE` that dyld loads:
addresses assigned, relocations *applied* (not just recorded), libSystem
imports bound through `__stubs` + `__got` with chained fixups, and the whole
thing ad-hoc signed by `macho_codesign` — no ld, no as, no codesign.

Binding mechanism, chosen and pinned: `LC_DYLD_CHAINED_FIXUPS` with
`DYLD_CHAINED_PTR_64_OFFSET`, which needs macOS 12+. That minimum is recorded
in `LC_BUILD_VERSION` (the row asks for the choice and its OS floor to be
explicit, since chained fixups do not exist on older loaders); classic
`LC_DYLD_INFO_ONLY` is deliberately not implemented rather than half-built.

Scope is pcc's own link job: one `__TEXT` (text/stubs/cstring/const), one
`__DATA` for mutable data, `__DATA_CONST` for the GOT. Anything else — a
section the layout does not know, a relocation type outside the proven set,
a missing entry symbol — raises `LinkError` instead of producing a binary
that fails inside dyld.

LINK-P3-PARALLEL keeps every semantic/layout pass above ordered.  Only after
all file offsets are frozen does it materialise disjoint output regions through
bounded workers; signing still consumes the one complete deterministic prefix.
"""

from __future__ import annotations

import os
import struct
import sys
from dataclasses import dataclass

from . import macho_spec as spec
from .macho_codesign import _CD_HEADER_SIZE as _CD_FIXED
from .macho_codesign import _align8, build_signature
from .macho_archive import ArchiveError, read_archive, select_members
from .macho_link import (
    LinkInput,
    LinkError,
    _coerce_link_object,
    _coerce_link_objects,
    link_relocatable_native,
)
from .macho_parallel import (
    OutputRegion,
    ParallelLinkError,
    materialize_output,
)
from .native_object import NativeObject, NativeObjectView, PackedNativeObject
from .precise_stackmap import (
    ARCH_AARCH64,
    PreciseStackMapError,
    validate_stack_map_payload,
)

PAGE = 0x4000
TEXT_BASE = 0x100000000
PAGEZERO_SIZE = 0x100000000
_DROPPED_UNWIND_SECTIONS = {
    ("__LD", "__compact_unwind"),
    ("__TEXT", "__eh_frame"),
}

DYLD_CHAINED_PTR_64_OFFSET = 6
# dyld enforces this on __DATA_CONST: the segment is made read-only
# after fixups are applied, and a missing flag is a hard load failure.
SG_READ_ONLY = 0x10
CHAINED_IMPORT = 1

LIBSYSTEM = b"/usr/lib/libSystem.B.dylib"
DYLINKER = b"/usr/lib/dyld"

# adrp x16, <page> ; ldr x16, [x16, #<off>] ; br x16
_STUB_SIZE = 12
_MIN_CHAINED_FIXUPS_VERSION = (12, 0)
_PROVEN_INPUT_LOAD_COMMANDS = frozenset({
    spec.LC_SEGMENT_64,
    spec.LC_BUILD_VERSION,
    spec.LC_DATA_IN_CODE,
    spec.LC_LINKER_OPTIMIZATION_HINT,
    spec.LC_SYMTAB,
    spec.LC_DYSYMTAB,
})


def _align(value: int, alignment: int) -> int:
    return (value + alignment - 1) & ~(alignment - 1)


def _adrp_imm(word: int, delta_page: int) -> int:
    lo = delta_page & 3
    hi = (delta_page >> 2) & 0x7FFFF
    return (word & ~((3 << 29) | (0x7FFFF << 5))) | (lo << 29) | (hi << 5)


def _addimm12(word: int, value: int) -> int:
    if not 0 <= value <= 0xFFF:
        raise LinkError(f"page offset {value} does not fit imm12")
    return (word & ~(0xFFF << 10)) | (value << 10)


def _pageoff12(word: int, byte_off: int) -> int:
    """Apply an ARM64_RELOC_PAGEOFF12 byte offset to the right instruction.

    For `add (immediate)` the imm12 field is the byte offset directly. For a
    load/store with unsigned offset (`ldr`/`str`), the imm12 field is the
    byte offset divided by the access size — so the same PAGEOFF12 relocation
    encodes differently depending on the instruction. Writing the raw byte
    offset into a scaled load (the old behavior) put the global at 8x its
    address, which is why the runtime read its GC-index base from the wrong
    global and got zero.
    """
    # Load/store register, unsigned immediate: bits 29:27 == 0b111 and
    # bits 25:24 == 0b01. Bit 26 is the integer/FP-vector V bit and is
    # intentionally excluded, so the same mask recognizes both families.
    is_ldst = (word & 0x3B000000) == 0x39000000
    if is_ldst:
        size = word >> 30  # 0=byte,1=half,2=word,3=doubleword
        # SIMD/FP Q-register transfers encode 16-byte width as size=0 with
        # opc[1]=1, rather than putting 4 in the two-bit size field.  Treating
        # `ldr q` as a byte load writes the raw byte offset into imm12 and the
        # CPU scales it by 16, redirecting literal-pool loads far beyond their
        # target section.
        is_simd_fp = (word & (1 << 26)) != 0
        opc = (word >> 22) & 0x3
        if is_simd_fp and (opc & 0x2):
            size = 4
        scale = 1 << size
        if byte_off % scale != 0:
            raise LinkError(
                f"PAGEOFF12 byte offset {byte_off} not aligned for a "
                f"{scale}-byte load/store"
            )
        imm = byte_off // scale
        if not 0 <= imm <= 0xFFF:
            raise LinkError(f"scaled PAGEOFF12 imm {imm} out of range")
        return (word & ~(0xFFF << 10)) | (imm << 10)
    return _addimm12(word, byte_off)


def _ldr_uimm12(word: int, value: int, scale: int) -> int:
    if value % scale:
        raise LinkError(f"offset {value} not aligned for the load size")
    imm = value // scale
    if not 0 <= imm <= 0xFFF:
        raise LinkError(f"offset {value} does not fit a scaled imm12")
    return (word & ~(0xFFF << 10)) | (imm << 10)


def _branch26(word: int, delta: int) -> int:
    if delta % 4:
        raise LinkError("misaligned branch target")
    words = delta >> 2
    if not -(1 << 25) <= words < (1 << 25):
        raise LinkError("branch target out of range")
    return (word & ~0x03FFFFFF) | (words & 0x03FFFFFF)


def _validate_minos(minos) -> tuple[int, int]:
    if (
        not isinstance(minos, tuple)
        or len(minos) != 2
        or any(not isinstance(item, int) or isinstance(item, bool) for item in minos)
    ):
        raise LinkError("minos must be a (major, minor) integer tuple")
    major, minor = minos
    if not (0 <= major <= 0xFFFF and 0 <= minor <= 0xFF):
        raise LinkError(f"minos {minos!r} does not fit LC_BUILD_VERSION")
    if minos < _MIN_CHAINED_FIXUPS_VERSION:
        raise LinkError(
            "LC_DYLD_CHAINED_FIXUPS with DYLD_CHAINED_PTR_64_OFFSET "
            "requires minos >= (12, 0)"
        )
    return major, minor


def _validate_input_load_commands(objects: list[LinkInput]) -> None:
    for index, data in enumerate(objects):
        obj = _coerce_link_object(data, index)
        if isinstance(obj, (NativeObject, NativeObjectView, PackedNativeObject)):
            # NativeObject validation is the internal equivalent of the
            # finite input-command allowlist. It has no Mach-O commands.
            continue
        unsupported = sorted(
            {command.cmd for command in obj.commands}
            - _PROVEN_INPUT_LOAD_COMMANDS
        )
        if unsupported:
            rendered = ", ".join(hex(command) for command in unsupported)
            raise LinkError(
                f"input {index} has unsupported load command(s): {rendered}"
            )
        try:
            # LLVM emits this optional link-quality payload in ordinary
            # production objects.  It has no semantic effect, so the owned
            # linker deliberately ignores its contents after proving the
            # linkedit range is structurally valid.
            obj.linker_optimization_hints()
        except spec.MachOFormatError as exc:
            raise LinkError(f"input {index} has invalid linker hints: {exc}") from exc


@dataclass
class _Out:
    segname: str
    sectname: str
    flags: int
    align_log2: int
    data: bytearray
    addr: int = 0
    fileoff: int = 0
    reserved1: int = 0
    reserved2: int = 0
    zerofill_size: int = 0

    @property
    def vm_size(self) -> int:
        return self.zerofill_size if self.zerofill_size else len(self.data)


def _external_symbol_state(
    objects: list[LinkInput],
) -> tuple[set[str], set[str]]:
    defined, undefined = set(), set()
    for index, data in enumerate(objects):
        obj = _coerce_link_object(data, index)
        if isinstance(obj, PackedNativeObject):
            for symbol in obj.symbols:
                if symbol.section_index and symbol.external:
                    defined.add(symbol.name)
                elif not symbol.section_index and symbol.external:
                    undefined.add(symbol.name)
            continue
        if isinstance(obj, NativeObject):
            for symbol in obj.symbols:
                if symbol.section_index and symbol.external:
                    defined.add(symbol.name)
                elif not symbol.section_index and symbol.external:
                    undefined.add(symbol.name)
            continue
        for sym in obj.symbols():
            kind = sym["n_type"] & spec.N_TYPE
            if kind == spec.N_SECT and (sym["n_type"] & spec.N_EXT):
                defined.add(sym["name"])
            elif kind == spec.N_UNDF and (sym["n_type"] & spec.N_EXT):
                undefined.add(sym["name"])
    return defined, undefined - defined


def prepare_executable_object(
    objects: list[LinkInput],
    *,
    archives: list[bytes] = (),
    semantic_manifest=None,
) -> NativeObject:
    """Resolve a final link job into its reusable relocatable state.

    Archive selection belongs here: an incremental state must represent the
    exact set of members selected by the unresolved-symbol closure, not merely
    the command-line objects.  The returned object is still pre-layout and
    pre-relocation, so compatible input payloads can patch it before final
    executable layout without re-reading and re-merging every input module.
    """

    # Archives are pools, not inputs: pull only the members that satisfy
    # something still undefined, repeatedly (see macho_archive).
    objects = _coerce_link_objects(list(objects))
    # Validate before archive selection reads the main inputs' symbol tables;
    # otherwise a malformed/foreign input can escape this final-link boundary
    # as a low-level parser exception instead of a deterministic LinkError.
    _validate_input_load_commands(objects)
    if archives:
        already_defined, pending = _external_symbol_state(objects)
        for archive_index, archive in enumerate(archives):
            try:
                members = read_archive(archive)
            except (ArchiveError, spec.MachOFormatError) as exc:
                raise LinkError(
                    f"archive {archive_index} is outside the proven subset: {exc}"
                ) from exc
            pulled, pending = select_members(
                members,
                pending,
                already_defined=already_defined,
            )
            objects.extend(_coerce_link_objects(
                list(pulled),
                start_index=len(objects),
            ))
            already_defined, pending = _external_symbol_state(objects)
        # Archive members are inputs too.  Validate the selected set before the
        # relocatable core consumes it; unselected members were already parsed
        # by read_archive and a malformed member failed above.
        _validate_input_load_commands(objects)
    # Even one input goes through the relocatable core: that is where CPU,
    # symbol-provenance, companion-relocation, and section-target contracts
    # are normalized and checked. A one-object fast path must not bypass them.
    merged = link_relocatable_native(objects)
    if semantic_manifest is not None:
        # Imported lazily so the ordinary linker path and compiled-stage
        # closure do not acquire semantic-layout policy or JSON machinery.
        from .macho_semantic_layout import (
            SemanticLayoutError,
            apply_semantic_layout,
        )
        try:
            merged = apply_semantic_layout(merged, semantic_manifest).native_object
        except SemanticLayoutError as exc:
            raise LinkError(f"semantic layout rejected: {exc}") from exc
    return merged


def link_executable(
    objects: list[LinkInput],
    *,
    archives: list[bytes] = (),
    entry: str = "_main",
    minos: tuple[int, int] = (12, 0),
    identifier: bytes = b"pcc-linked",
    semantic_manifest=None,
) -> bytes:
    # Preserve fail-closed option validation before parsing potentially large
    # inputs.  ``link_prepared_executable`` validates again for direct users.
    minos = _validate_minos(minos)
    return link_prepared_executable(
        prepare_executable_object(
            objects,
            archives=archives,
            semantic_manifest=semantic_manifest,
        ),
        entry=entry,
        minos=minos,
        identifier=identifier,
    )


def link_prepared_executable(
    merged: NativeObject,
    *,
    entry: str = "_main",
    minos: tuple[int, int] = (12, 0),
    identifier: bytes = b"pcc-linked",
    phase_callback=None,
) -> bytes:
    """Finalize one validated, already-merged object as an executable.

    This is the narrow incremental-link seam.  It deliberately accepts only
    ``NativeObject``: external Mach-O and archive validation must happen in
    :func:`prepare_executable_object`, never be bypassed by a cache hit.
    """

    minos = _validate_minos(minos)
    if not isinstance(merged, NativeObject):
        raise LinkError("prepared executable input must be a NativeObject")
    obj = merged.link_view()
    del merged

    sections = obj.sections()
    symbols = obj.symbols()
    defined = {
        s["name"]: s for s in symbols
        if (s["n_type"] & spec.N_TYPE) == spec.N_SECT
    }
    imports = sorted(
        s["name"] for s in symbols
        if (s["n_type"] & spec.N_TYPE) == spec.N_UNDF
    )
    if entry not in defined:
        raise LinkError(f"entry symbol {entry!r} is not defined by the inputs")
    entry_symbol = defined[entry]
    entry_section_index = entry_symbol["n_sect"]
    if not 1 <= entry_section_index <= len(sections):
        raise LinkError(
            f"entry symbol {entry!r} names section {entry_section_index}"
        )
    entry_section = sections[entry_section_index - 1]
    if (
        entry_section["segname_str"] != "__TEXT"
        or not (entry_section["flags"] & spec.S_ATTR_PURE_INSTRUCTIONS)
    ):
        raise LinkError(
            f"entry symbol {entry!r} must be in an executable __TEXT section"
        )

    for symbol in symbols:
        name = symbol["name"]
        if not name or not name.isascii() or "\0" in name:
            raise LinkError(f"symbol name {name!r} is not a valid dyld name")

    # Classify imports by how they are referenced. An import reached by a
    # BRANCH26 (a call) needs a stub + GOT slot; an import reached only by a
    # data pointer (UNSIGNED) — e.g. a TLV descriptor's __tlv_bootstrap thunk
    # — needs neither, just an ordinal and a chained-fixup bind at the data
    # location. GOT_LOAD of an import also needs a GOT slot.
    _S_THREAD_LOCAL_REGULAR = 0x11
    _S_THREAD_LOCAL_ZEROFILL = 0x12
    _S_THREAD_LOCAL_VARIABLES = 0x13
    code_imports_set: set[str] = set()
    relocated_imports: set[str] = set()
    names_all = [s["name"] for s in symbols]
    imports_set = set(imports)
    for sec in sections:
        for r in obj.iter_relocations(sec):
            if not r["r_extern"]:
                continue
            nm = names_all[r["r_symbolnum"]]
            if nm in imports_set:
                relocated_imports.add(nm)
            if r["r_type"] in (spec.ARM64_RELOC_BRANCH26,
                               spec.ARM64_RELOC_GOT_LOAD_PAGE21,
                               spec.ARM64_RELOC_GOT_LOAD_PAGEOFF12):
                code_imports_set.add(nm)
    unreferenced_imports = sorted(set(imports) - relocated_imports)
    if unreferenced_imports:
        raise LinkError(
            "undefined symbol(s) have no relocation naming an import: "
            + ", ".join(repr(name) for name in unreferenced_imports)
        )
    code_imports = [n for n in imports if n in code_imports_set]

    # Thread block: __thread_data then __thread_bss, concatenated with each
    # section's alignment. A symbol defined in those sections gets its offset
    # within that block, which is what a TLV descriptor's third field holds.
    thread_block_offset: dict[str, int] = {}
    _tblk = 0
    for sec in sections:
        stype = sec["flags"] & spec.SECTION_TYPE
        if stype in (_S_THREAD_LOCAL_REGULAR, _S_THREAD_LOCAL_ZEROFILL):
            _tblk = _align(_tblk, 1 << sec["align"])
            base = _tblk
            for sym in symbols:
                if (sym["n_type"] & spec.N_TYPE) == spec.N_SECT and \
                        sections[sym["n_sect"] - 1] is sec:
                    thread_block_offset[sym["name"]] = base + (
                        sym["n_value"] - sec["addr"])
            _tblk = base + sec["size"]

    # --- section layout ----------------------------------------------------
    text_out: list[_Out] = []
    data_out: list[_Out] = []
    dropped_unwind = []
    for sec in sections:
        segname = sec["segname_str"]
        sectname = sec["sectname_str"]
        # Unwind metadata is dropped at executable link, exactly as ld does:
        # ld consumes __LD,__compact_unwind (and __TEXT,__eh_frame) into a
        # synthesized __TEXT,__unwind_info. pcc does not synthesize
        # __unwind_info, so it drops the inputs instead. The only cost is
        # unwinding THROUGH these frames (C++ exceptions, backtraces), which
        # LINK-P1-MACHO-LINK-DYLD already scopes out; ordinary control flow
        # and the runtime's own py_err_occurred error model are unaffected.
        # Dropped, not silently ignored: recorded and reported.
        if (segname, sectname) in _DROPPED_UNWIND_SECTIONS:
            dropped_unwind.append(f"{segname},{sectname}")
            continue
        stype = sec["flags"] & spec.SECTION_TYPE
        is_zf = stype in (0x1, 0x12)  # S_ZEROFILL / S_THREAD_LOCAL_ZEROFILL
        # A zerofill section has no file content (sec["offset"] is 0); reading
        # obj.data[0:size] for it would splice the mach header + __text into
        # __bss and the section's globals would read instruction bytes. It
        # occupies vm space only; dyld zeroes it.
        out = _Out(
            segname, sectname, sec["flags"], sec["align"],
            bytearray() if is_zf
            else bytearray(obj.data[sec["offset"]:sec["offset"] + sec["size"]]),
        )
        out.zerofill_size = sec["size"] if is_zf else 0
        if segname == "__TEXT":
            text_out.append(out)
        elif segname == "__DATA":
            data_out.append(out)
        else:
            raise LinkError(
                f"segment {segname} is outside the proven layout"
            )

    # Mach-O TLS is one compact template: file-backed __thread_data followed
    # immediately by virtual-only __thread_bss.  Input objects commonly put
    # ordinary __bss/__common before __thread_bss; preserving that order in
    # the final image makes dyld's TLS template offsets disagree with the
    # compact offsets stored in __thread_vars descriptors.  Keep all ordinary
    # file-backed DATA first, then descriptors and TLS data, then TLS
    # zerofill, and only then ordinary zerofill.  Python's sort is stable, so
    # relative order within each semantic class (including multiple TLS
    # sections) remains the merged-object order used by thread_block_offset.
    def _data_layout_rank(out):
        stype = out.flags & spec.SECTION_TYPE
        if stype == _S_THREAD_LOCAL_VARIABLES:
            return 1
        if stype == _S_THREAD_LOCAL_REGULAR:
            return 2
        if stype == _S_THREAD_LOCAL_ZEROFILL:
            return 3
        if stype == spec.S_ZEROFILL:
            return 4
        return 0

    data_out.sort(key=_data_layout_rank)
    if dropped_unwind:
        # Visible on stderr so a caller knows the binary has no unwind tables.
        sys.stderr.write(
            "pcc link: dropped unwind metadata (no __unwind_info synthesis): "
            + ", ".join(sorted(set(dropped_unwind))) + "\n"
        )

    # S_SYMBOL_STUBS / S_NON_LAZY_SYMBOL_POINTERS both index the indirect
    # symbol table through reserved1, and dyld kills a binary whose stub or
    # pointer section claims those types without one. Chained fixups bind the
    # GOT by walking the chain, not through the indirect table, so pcc's
    # stubs and GOT are plain sections and no indirect table is emitted.
    # (Measured: with the special types and no indirect table the process is
    # SIGKILLed with no diagnostic; as plain sections the same binary runs.)
    stubs = _Out("__TEXT", "__stubs", spec.S_REGULAR
                 | spec.S_ATTR_PURE_INSTRUCTIONS | spec.S_ATTR_SOME_INSTRUCTIONS,
                 2, bytearray(b"\0" * (_STUB_SIZE * len(code_imports))))
    got = _Out("__DATA_CONST", "__got", spec.S_REGULAR, 3,
               bytearray(b"\0" * (8 * len(code_imports))))

    # Load-command size is needed before addresses; compute it by counting.
    seg_cmd_size = lambda n: spec.SEGMENT_COMMAND_64.size + n * spec.SECTION_64.size
    sizeofcmds = (
        seg_cmd_size(0)                                   # __PAGEZERO
        + seg_cmd_size(len(text_out) + (1 if code_imports else 0))
        + seg_cmd_size(1 if code_imports else 0)          # __DATA_CONST
        + (seg_cmd_size(len(data_out)) if data_out else 0)
        + seg_cmd_size(0)                                 # __LINKEDIT
        + 16   # LC_DYLD_CHAINED_FIXUPS
        + spec.SYMTAB_COMMAND.size
        + spec.DYSYMTAB_COMMAND.size
        + _align(12 + len(DYLINKER) + 1, 8)               # LC_LOAD_DYLINKER
        + 24                                              # LC_UUID
        + spec.BUILD_VERSION_COMMAND.size                 # LC_BUILD_VERSION (ntools 0)
        + 24                                              # LC_MAIN
        + _align(24 + len(LIBSYSTEM) + 1, 8)              # LC_LOAD_DYLIB
        + 16                                              # LC_CODE_SIGNATURE
    )
    ncmds = (
        1 + 1 + 1 + (1 if data_out else 0) + 1  # segments
        + 1 + 1 + 1 + 1 + 1 + 1 + 1 + 1 + 1     # the nine others
    )

    cursor = spec.MACH_HEADER_64.size + sizeofcmds
    text_sections = list(text_out) + ([stubs] if code_imports else [])
    for out in text_sections:
        cursor = _align(cursor, 1 << out.align_log2)
        out.fileoff = cursor
        out.addr = TEXT_BASE + cursor
        cursor += len(out.data)
    text_filesize = _align(cursor, PAGE)

    data_const_off = text_filesize
    if code_imports:
        got.fileoff = data_const_off
        got.addr = TEXT_BASE + data_const_off
    data_const_end = _align(data_const_off + len(got.data), PAGE)

    data_off = data_const_end
    file_cursor = data_off
    vm_cursor = data_off
    data_content_end = data_off
    for out in data_out:
        if out.zerofill_size:
            vm_cursor = _align(vm_cursor, 1 << out.align_log2)
            out.addr = TEXT_BASE + vm_cursor
            out.fileoff = 0
            vm_cursor += out.zerofill_size
        else:
            file_cursor = _align(file_cursor, 1 << out.align_log2)
            vm_cursor = file_cursor
            out.fileoff = file_cursor
            out.addr = TEXT_BASE + file_cursor
            file_cursor += len(out.data)
            vm_cursor = file_cursor
            data_content_end = file_cursor
    data_file_end = _align(data_content_end, PAGE) if data_out else data_const_end
    data_end = _align(vm_cursor, PAGE) if data_out else data_const_end

    # Zerofill extends __DATA only in VM space.  __LINKEDIT follows the
    # __DATA VM end (below) but reuses the compact file offset immediately
    # after __DATA's file-backed bytes.  Darwin requires the file and VM
    # offsets to be page-congruent, not equal; both ends are PAGE-aligned.
    # Binding the file offset to data_end would materialize the entire BSS as
    # zero padding and make executable size grow with virtual-only storage.
    linkedit_off = data_file_end

    # --- symbol addresses --------------------------------------------------
    section_addr = {}
    for index, sec in enumerate(sections, start=1):
        for out in text_out + data_out:
            if (out.segname, out.sectname) == (sec["segname_str"], sec["sectname_str"]):
                section_addr[index] = (out, sec["addr"])
    sym_addr: dict[str, int] = {}
    dropped_symbols: set[str] = set()
    for name, sym in defined.items():
        mapping = section_addr.get(sym["n_sect"])
        if mapping is None:
            # This symbol lived in a dropped unwind section. Keep it out of
            # the address map; it must also not be a relocation target (an
            # unwind label reached from live code would be a real error, not
            # dead metadata) — checked below.
            dropped_symbols.add(name)
            continue
        out, sec_addr = mapping
        sym_addr[name] = out.addr + (sym["n_value"] - sec_addr)
    stub_addr = {
        name: stubs.addr + i * _STUB_SIZE for i, name in enumerate(code_imports)
    }
    got_addr = {name: got.addr + i * 8 for i, name in enumerate(code_imports)}

    # --- stub bodies -------------------------------------------------------
    for i, name in enumerate(code_imports):
        at = stubs.addr + i * _STUB_SIZE
        target = got_addr[name]
        adrp = _adrp_imm(0x90000010, (target >> 12) - (at >> 12))
        ldr = _ldr_uimm12(0xF9400210, target & 0xFFF, 8)
        struct.pack_into("<3I", stubs.data, i * _STUB_SIZE, adrp, ldr, 0xD61F0200)

    # --- apply relocations -------------------------------------------------
    import_ordinal = {name: i for i, name in enumerate(imports)}
    # Chained-fixup BIND sites outside the GOT: a TLV descriptor's thunk
    # field is an import pointer in __DATA that dyld binds. Collected here,
    # emitted into the fixups chain below.
    tlv_bind_sites: list[tuple] = []  # (_Out, offset_in_out, ordinal)
    # In-image data pointers (UNSIGNED to a defined symbol) hold absolute
    # addresses that must follow the ASLR slide, so each needs a REBASE
    # chained fixup. (_Out, offset_in_out, target_offset_from_image_base)
    rebase_sites: list[tuple] = []
    _S_TLV_VARS = 0x13
    for sec_index, sec in enumerate(sections, start=1):
        mapping = section_addr.get(sec_index)
        if mapping is None:
            continue  # a dropped unwind section carries no live relocations
        out, sec_addr = mapping
        pending_addend = 0
        for entry_r in obj.iter_relocations(sec):
            if entry_r["r_type"] == spec.ARM64_RELOC_ADDEND:
                pending_addend = entry_r["r_symbolnum"]
                continue
            if not entry_r["r_extern"]:
                target_index = entry_r["r_symbolnum"]
                if 1 <= target_index <= len(sections):
                    target_sec = sections[target_index - 1]
                    target_key = (
                        target_sec["segname_str"],
                        target_sec["sectname_str"],
                    )
                    if target_key in _DROPPED_UNWIND_SECTIONS:
                        raise LinkError(
                            "live section targets dropped unwind metadata "
                            f"{target_key[0]},{target_key[1]} — not safe "
                            "to drop"
                        )
                raise LinkError(
                    "section-target relocations must be normalized to a "
                    "defined symbol before executable linking"
                )
            symbol_index = entry_r["r_symbolnum"]
            if not 0 <= symbol_index < len(names_all):
                raise LinkError(
                    f"relocation names symbol index {symbol_index}, but the "
                    f"merged object has {len(names_all)} symbols"
                )
            name = names_all[symbol_index]
            addend, pending_addend = pending_addend, 0
            at_off = entry_r["r_address"]
            at = out.addr + at_off
            rtype = entry_r["r_type"]
            if name in sym_addr:
                target = sym_addr[name] + addend
            elif name in dropped_symbols:
                raise LinkError(
                    f"live section references {name!r}, which lives in "
                    "dropped unwind metadata — not safe to drop"
                )
            elif name in imports:
                target = None  # resolved through stub/GOT below
            else:
                raise LinkError(f"unresolved symbol {name!r}")

            if rtype == spec.ARM64_RELOC_BRANCH26:
                dest = stub_addr[name] if target is None else target
                word, = struct.unpack_from("<I", out.data, at_off)
                struct.pack_into("<I", out.data, at_off,
                                 _branch26(word, dest - at))
            elif rtype in (spec.ARM64_RELOC_PAGE21,
                           spec.ARM64_RELOC_GOT_LOAD_PAGE21):
                # GOT relaxation: a GOT load of a DEFINED symbol needs no GOT
                # slot — the target is in-image, so adrp points at the
                # symbol's own page (and the paired PAGEOFF12 rewrites the
                # ldr into an add below). Only true imports keep a GOT slot.
                # This is exactly what ld does, and it avoids a multi-page
                # GOT with rebase chained-fixups whose stride/page-start math
                # would be a silent-SIGKILL hazard.
                if rtype == spec.ARM64_RELOC_GOT_LOAD_PAGE21 and target is None:
                    dest = got_addr[name]
                else:
                    dest = target
                if dest is None:
                    raise LinkError(f"{name!r} needs a GOT slot for PAGE21")
                word, = struct.unpack_from("<I", out.data, at_off)
                struct.pack_into("<I", out.data, at_off,
                                 _adrp_imm(word, (dest >> 12) - (at >> 12)))
            elif rtype == spec.ARM64_RELOC_PAGEOFF12:
                if target is None:
                    raise LinkError(f"{name!r} needs a GOT slot for PAGEOFF12")
                word, = struct.unpack_from("<I", out.data, at_off)
                struct.pack_into(
                    "<I", out.data, at_off,
                    _pageoff12(word, target & 0xFFF),
                )
            elif rtype == spec.ARM64_RELOC_GOT_LOAD_PAGEOFF12:
                if target is not None:
                    # Relax: ldr xt,[xn,#imm]  ->  add xt,xn,#imm, so the
                    # value is the symbol's address directly, not loaded
                    # from a GOT slot. `ldr (unsigned) x` is 0xF94xxxxx;
                    # `add (imm) x` is 0x91000000 with the same Rt/Rn.
                    word, = struct.unpack_from("<I", out.data, at_off)
                    rt = word & 0x1F
                    rn = (word >> 5) & 0x1F
                    add = 0x91000000 | ((target & 0xFFF) << 10) | (rn << 5) | rt
                    struct.pack_into("<I", out.data, at_off, add)
                else:
                    word, = struct.unpack_from("<I", out.data, at_off)
                    struct.pack_into("<I", out.data, at_off,
                                     _ldr_uimm12(word, got_addr[name] & 0xFFF, 8))
            elif rtype == spec.ARM64_RELOC_UNSIGNED:
                in_tlv_desc = (sec["flags"] & spec.SECTION_TYPE) == _S_TLV_VARS
                if target is None:
                    if in_tlv_desc and name in import_ordinal:
                        # Descriptor thunk: a chained-fixup bind to the import
                        # (e.g. __tlv_bootstrap). Field is zeroed; dyld fills
                        # it via the chain.
                        struct.pack_into("<Q", out.data, at_off, 0)
                        tlv_bind_sites.append(
                            (out, at_off, import_ordinal[name]))
                    else:
                        raise LinkError(f"import {name!r} in a data pointer")
                elif in_tlv_desc and name in thread_block_offset:
                    # Descriptor offset field: the variable's offset within
                    # the thread block, not its vmaddr.
                    struct.pack_into("<Q", out.data, at_off,
                                     thread_block_offset[name])
                else:
                    inline, = struct.unpack_from("<q", out.data, at_off)
                    absolute = target + inline
                    # Store the offset from the image base; the rebase fixup
                    # below adds the runtime base back (surviving ASLR). Only
                    # data sections carry rebases — a pointer in __TEXT would
                    # be in a read-only, non-fixup segment.
                    if out.segname in ("__DATA", "__DATA_CONST"):
                        rebase_sites.append(
                            (out, at_off, absolute - TEXT_BASE))
                        struct.pack_into("<Q", out.data, at_off,
                                         absolute - TEXT_BASE)
                    else:
                        raise LinkError(
                            f"absolute pointer relocation in "
                            f"{out.segname},{out.sectname} requires a "
                            "chained rebase in a writable data segment"
                        )
            elif rtype == spec.ARM64_RELOC_TLVP_LOAD_PAGE21:
                # The TLV descriptor is a defined symbol in __thread_vars, so
                # this adrp points at its own page exactly like a relaxed GOT
                # load. (`target` is the descriptor address.)
                if target is None:
                    raise LinkError(f"TLV descriptor {name!r} is not defined")
                word, = struct.unpack_from("<I", out.data, at_off)
                struct.pack_into("<I", out.data, at_off,
                                 _adrp_imm(word, (target >> 12) - (at >> 12)))
            elif rtype == spec.ARM64_RELOC_TLVP_LOAD_PAGEOFF12:
                # Relax `ldr xt,[xn,#off]` to `add xt,xn,#off`: the value is
                # the descriptor's address directly (ld does the same).
                if target is None:
                    raise LinkError(f"TLV descriptor {name!r} is not defined")
                word, = struct.unpack_from("<I", out.data, at_off)
                rt = word & 0x1F
                rn = (word >> 5) & 0x1F
                struct.pack_into("<I", out.data, at_off,
                                 0x91000000 | ((target & 0xFFF) << 10)
                                 | (rn << 5) | rt)
            else:
                raise LinkError(f"relocation type {rtype} not applied")

    # All addresses now live in the output sections and fixup tables. Release
    # the indexed input records and its contiguous view before allocating
    # stack-map validation scratch, chained fixups and the final image.
    del obj

    # The relocatable linker rebuilds stack-map tables semantically.  Check
    # the resolved table once more after native address relocations, before
    # those pointer words are encoded as dyld chained-rebase records.  This
    # ensures a corrupt/truncated table cannot reach executable publication.
    for out in data_out:
        if (out.segname, out.sectname) != ("__DATA", "__pcc_stackmaps"):
            continue
        try:
            validate_stack_map_payload(
                bytes(out.data),
                expected_arch=ARCH_AARCH64,
                final_image=True,
            )
        except PreciseStackMapError as exc:
            raise LinkError(f"final stack-map table is invalid: {exc}") from exc

    # --- chained fixups: binds across the segments that carry them ---------
    # A bind pointer is an import reference dyld resolves by walking a chain.
    # Two kinds here: GOT slots in __DATA_CONST (code imports) and TLV
    # descriptor thunks in __DATA. Each segment with binds gets its own
    # dyld_chained_starts_in_segment with per-page starts; within a page the
    # `next` field is the 4-byte stride to the following bind (0 = end).
    DYLD_CHAINED_PTR_START_NONE = 0xFFFF
    dc_index = 2  # __PAGEZERO, __TEXT, __DATA_CONST
    data_index = 3  # __DATA (only present when data_out)
    seg_count = 4 + (1 if data_out else 0)

    # Collect fixup sites per segment index. Each site carries its
    # `base_word` — the 64-bit chained pointer WITHOUT the `next` field, which
    # _build_seg_info fills in. A bind sets bit 63 and an import ordinal; a
    # rebase clears bit 63 and encodes the target's offset from the image
    # base (36 bits) plus high8 (8 bits at bit 36).
    def _bind_word(ordinal):
        if not 0 <= ordinal < (1 << 24):
            raise LinkError(f"chained-import ordinal {ordinal} exceeds 24 bits")
        return (1 << 63) | ordinal

    def _rebase_word(image_off):
        if image_off < 0 or image_off >= (1 << 36):
            raise LinkError(f"rebase target offset {image_off} exceeds 36 bits")
        high8 = 0  # our addresses fit in 36 bits from the image base
        return (high8 << 36) | (image_off & ((1 << 36) - 1))

    seg_fixups: dict[int, list] = {}
    if code_imports:
        seg_fixups[dc_index] = [
            (got.addr + i * 8, got, i * 8, _bind_word(import_ordinal[name]))
            for i, name in enumerate(code_imports)
        ]
    if tlv_bind_sites:
        seg_fixups.setdefault(data_index, []).extend(
            (out.addr + off, out, off, _bind_word(ordinal))
            for out, off, ordinal in tlv_bind_sites
        )
    for out, off, image_off in rebase_sites:
        seg_index = dc_index if out.segname == "__DATA_CONST" else data_index
        seg_fixups.setdefault(seg_index, []).append(
            (out.addr + off, out, off, _rebase_word(image_off)))

    seg_vm = {
        dc_index: (TEXT_BASE + data_const_off, data_const_end - data_const_off,
                   data_const_off),
        # dyld_chained_starts_in_segment describes pages present in the file,
        # not the segment's virtual-only zerofill tail.  Using the full
        # __DATA vmsize here makes the starts table claim BSS pages whose
        # numeric offsets overlap compact __LINKEDIT bytes.  dyld can then
        # expose those bytes through __common/__bss instead of zero-fill.
        # Apple's linker likewise emits page_count from __DATA.filesize when
        # a segment contains both rebased pointers and a larger BSS tail.
        data_index: (
            TEXT_BASE + data_off,
            data_file_end - data_off,
            data_off,
        ),
    }

    def _build_seg_info(fixups, seg_vmaddr, seg_size, seg_file_off):
        page_count = max(1, (seg_size + PAGE - 1) // PAGE)
        page_starts = [DYLD_CHAINED_PTR_START_NONE] * page_count
        by_page: dict[int, list] = {}
        seen_sites: set[int] = set()
        for site in sorted(fixups, key=lambda item: item[0]):
            vmaddr, out, off, _base_word = site
            if vmaddr in seen_sites:
                raise LinkError(f"duplicate chained-fixup site at {vmaddr:#x}")
            seen_sites.add(vmaddr)
            if not seg_vmaddr <= vmaddr <= seg_vmaddr + seg_size - 8:
                raise LinkError(
                    f"chained-fixup site {vmaddr:#x} is outside its segment"
                )
            if vmaddr % 4 or off < 0 or off + 8 > len(out.data):
                raise LinkError(
                    f"chained-fixup site {vmaddr:#x} is not a complete, "
                    "four-byte-aligned pointer"
                )
            segment_offset = vmaddr - seg_vmaddr
            page_offset = segment_offset % PAGE
            if page_offset > PAGE - 8:
                raise LinkError(
                    f"chained-fixup pointer at {vmaddr:#x} crosses a "
                    f"{PAGE:#x}-byte page boundary"
                )
            page = segment_offset // PAGE
            by_page.setdefault(page, []).append(site)
        for page, plist in by_page.items():
            page_starts[page] = (plist[0][0] - seg_vmaddr) % PAGE
            for i, (vmaddr, out, off, base_word) in enumerate(plist):
                # `next` is the 4-byte stride to the next fixup in this page;
                # a chained pointer can only reach 4095*4 bytes, so a page
                # denser than that would need splitting (not hit here).
                if i + 1 < len(plist):
                    delta = plist[i + 1][0] - vmaddr
                    if delta <= 0 or delta % 4:
                        raise LinkError(
                            "chained-fixup sites must increase in four-byte units"
                        )
                    stride = delta // 4
                    if stride > 0xFFF:
                        raise LinkError("fixup chain stride exceeds 12 bits")
                    nxt = stride
                else:
                    nxt = 0
                struct.pack_into("<Q", out.data, off, base_word | (nxt << 51))
        info = struct.pack("<IHHQIH", 22 + page_count * 2, PAGE,
                           DYLD_CHAINED_PTR_64_OFFSET, seg_file_off, 0,
                           page_count)
        info += b"".join(struct.pack("<H", ps) for ps in page_starts)
        return info

    seg_infos = {}
    for seg_index, fixups in seg_fixups.items():
        seg_vmaddr, seg_size, seg_file_off = seg_vm[seg_index]
        seg_infos[seg_index] = _build_seg_info(
            fixups, seg_vmaddr, seg_size, seg_file_off)

    sym_blob = bytearray(b"\0")
    import_entries = []
    for name in imports:
        if len(sym_blob) >= (1 << 23):
            raise LinkError("chained-import symbol string offset exceeds 23 bits")
        import_entries.append((1 << 0) | (len(sym_blob) << 9))
        sym_blob += name.encode() + b"\0"
    while len(sym_blob) % 8:
        sym_blob += b"\0"

    starts_off = 32
    # seg_count word + per-segment offset table, then the seg_info blobs.
    offsets = [0] * seg_count
    cursor = 4 + seg_count * 4
    for seg_index in sorted(seg_infos):
        offsets[seg_index] = cursor
        cursor += len(seg_infos[seg_index])
    starts = bytearray(struct.pack("<I", seg_count))
    for i in range(seg_count):
        starts += struct.pack("<I", offsets[i])
    for seg_index in sorted(seg_infos):
        starts += seg_infos[seg_index]
    while len(starts) % 8:
        starts += b"\0"
    imports_off = starts_off + len(starts)
    imports_blob = struct.pack("<%dI" % len(import_entries), *import_entries)
    while len(imports_blob) % 8:
        imports_blob += b"\0"
    syms_off = imports_off + len(imports_blob)
    fixups = struct.pack(
        "<7I", 0, starts_off, imports_off, syms_off,
        len(import_entries), CHAINED_IMPORT, 0,
    ) + b"\0" * (starts_off - 28) + bytes(starts) + imports_blob + bytes(sym_blob)

    fixups_off = linkedit_off
    symtab_off = _align(fixups_off + len(fixups), 8)

    # --- symbol table ------------------------------------------------------
    strtab = bytearray(b"\0")
    nlists = bytearray()
    # Symbols in dropped unwind sections have no address; they are not in
    # sym_addr and must not appear in the symbol table either.
    defined_items = [
        (name, sym) for name, sym in defined.items() if name in sym_addr
    ]
    order_key = lambda kv: (sym_addr[kv[0]], kv[0])
    local_defs = sorted(
        (
            item for item in defined_items
            if not (item[1]["n_type"] & spec.N_EXT)
        ),
        key=order_key,
    )
    external_defs = sorted(
        (item for item in defined_items if item[1]["n_type"] & spec.N_EXT),
        key=order_key,
    )
    # LC_DYSYMTAB describes three contiguous partitions.  Keeping local and
    # external definitions interleaved while claiming nlocalsym == 0 makes
    # the command disagree with the actual nlist entries and confuses tools
    # (and future dyld consumers) that trust the partition indices.
    ordered = local_defs + external_defs
    for name, sym in ordered:
        strx = len(strtab)
        strtab += name.encode() + b"\0"
        out, sec_addr = section_addr[sym["n_sect"]]
        n_sect = 1 + (text_out + ([stubs] if code_imports else [])
                      + ([got] if code_imports else []) + data_out).index(out)
        nlists += spec.NLIST_64.pack({
            "n_strx": strx,
            "n_type": (
                spec.N_SECT
                | (spec.N_EXT if sym["n_type"] & spec.N_EXT else 0)
                | (spec.N_PEXT if sym["n_type"] & spec.N_PEXT else 0)
            ),
            "n_sect": n_sect, "n_desc": 0, "n_value": sym_addr[name],
        })
    n_local = len(local_defs)
    n_extdef = len(external_defs)
    n_defined = n_local + n_extdef
    for name in imports:
        strx = len(strtab)
        strtab += name.encode() + b"\0"
        nlists += spec.NLIST_64.pack({
            "n_strx": strx, "n_type": spec.N_UNDF | spec.N_EXT,
            "n_sect": spec.NO_SECT, "n_desc": 1 << 8, "n_value": 0,
        })
    while len(strtab) % 8:
        strtab += b"\0"
    strtab_off = symtab_off + len(nlists)
    sig_off = _align(strtab_off + len(strtab), 16)
    # __LINKEDIT must COVER the code signature: dyld's strict validation
    # rejects a binary whose signature sits outside the segment (observed as
    # "main executable failed strict validation"). The blob size is knowable
    # in advance — one SHA-256 per 4096-byte page up to sig_off — so predict
    # it, write it into both LC_CODE_SIGNATURE and __LINKEDIT, and sign once.
    sig_size = _align8(
        20 + _CD_FIXED + len(identifier) + 1 + ((sig_off + 4095) // 4096) * 32
    )
    linkedit_size = sig_off + sig_size - linkedit_off

    # --- load commands -----------------------------------------------------
    def segment(name, vmaddr, vmsize, fileoff, filesize, maxp, initp, secs,
                flags=0):
        body = spec.SEGMENT_COMMAND_64.pack({
            "cmd": spec.LC_SEGMENT_64,
            "cmdsize": seg_cmd_size(len(secs)),
            "segname": name.encode().ljust(16, b"\0"),
            "vmaddr": vmaddr, "vmsize": vmsize,
            "fileoff": fileoff, "filesize": filesize,
            "maxprot": maxp, "initprot": initp,
            "nsects": len(secs), "flags": flags,
        })
        for out in secs:
            body += spec.SECTION_64.pack({
                "sectname": out.sectname.encode().ljust(16, b"\0"),
                "segname": out.segname.encode().ljust(16, b"\0"),
                "addr": out.addr, "size": out.vm_size,
                "offset": 0 if out.zerofill_size else out.fileoff,
                "align": out.align_log2,
                "reloff": 0, "nreloc": 0, "flags": out.flags,
                "reserved1": out.reserved1, "reserved2": out.reserved2,
                "reserved3": 0,
            })
        return body

    RX = spec.VM_PROT_READ | spec.VM_PROT_EXECUTE
    RW = spec.VM_PROT_READ | spec.VM_PROT_WRITE
    cmds = bytearray()
    cmds += segment("__PAGEZERO", 0, PAGEZERO_SIZE, 0, 0, 0, 0, [])
    cmds += segment("__TEXT", TEXT_BASE, text_filesize, 0, text_filesize,
                    RX, RX, text_sections)
    cmds += segment("__DATA_CONST", TEXT_BASE + data_const_off,
                    data_const_end - data_const_off, data_const_off,
                    data_const_end - data_const_off, RW, RW,
                    [got] if code_imports else [], flags=SG_READ_ONLY)
    if data_out:
        # The file payload ends at the last file-backed section (data_content_end),
        # NOT the page-aligned data_file_end: the alignment padding between them
        # overlaps the __bss/__common globals, and backing those addresses with
        # file bytes instead of zero-fill made them read garbage at startup
        # (SIGBUS in run_compiled_module_init on the self-hosted pcc1).  The
        # dyld zero-fills everything beyond the file payload inside vmsize.
        cmds += segment("__DATA", TEXT_BASE + data_off, data_end - data_off,
                        data_off, data_content_end - data_off, RW, RW, data_out)
    cmds += segment("__LINKEDIT", TEXT_BASE + data_end,
                    _align(linkedit_size, PAGE), linkedit_off, linkedit_size,
                    spec.VM_PROT_READ, spec.VM_PROT_READ, [])
    cmds += struct.pack("<IIII", spec.LC_DYLD_CHAINED_FIXUPS, 16,
                        fixups_off, len(fixups))
    cmds += spec.SYMTAB_COMMAND.pack({
        "cmd": spec.LC_SYMTAB, "cmdsize": spec.SYMTAB_COMMAND.size,
        "symoff": symtab_off, "nsyms": n_defined + len(imports),
        "stroff": strtab_off, "strsize": len(strtab),
    })
    cmds += spec.DYSYMTAB_COMMAND.pack({
        "cmd": spec.LC_DYSYMTAB, "cmdsize": spec.DYSYMTAB_COMMAND.size,
        "ilocalsym": 0, "nlocalsym": n_local,
        "iextdefsym": n_local, "nextdefsym": n_extdef,
        "iundefsym": n_defined, "nundefsym": len(imports),
        "tocoff": 0, "ntoc": 0, "modtaboff": 0, "nmodtab": 0,
        "extrefsymoff": 0, "nextrefsyms": 0,
        "indirectsymoff": 0, "nindirectsyms": 0,
        "extreloff": 0, "nextrel": 0, "locreloff": 0, "nlocrel": 0,
    })
    dl_size = _align(12 + len(DYLINKER) + 1, 8)
    cmds += struct.pack("<III", spec.LC_LOAD_DYLINKER, dl_size, 12)
    cmds += DYLINKER.ljust(dl_size - 12, b"\0")
    cmds += struct.pack("<II", spec.LC_UUID, 24) + b"\x11" * 16
    cmds += spec.BUILD_VERSION_COMMAND.pack({
        "cmd": spec.LC_BUILD_VERSION,
        "cmdsize": spec.BUILD_VERSION_COMMAND.size,
        "platform": spec.PLATFORM_MACOS,
        "minos": (minos[0] << 16) | (minos[1] << 8),
        "sdk": 0, "ntools": 0,
    })
    cmds += struct.pack("<IIQQ", spec.LC_MAIN, 24,
                        sym_addr[entry] - TEXT_BASE, 0)
    dylib_size = _align(24 + len(LIBSYSTEM) + 1, 8)
    cmds += struct.pack("<IIIIII", spec.LC_LOAD_DYLIB, dylib_size, 24,
                        0, 0x10000, 0x10000)
    cmds += LIBSYSTEM.ljust(dylib_size - 24, b"\0")
    cmds += struct.pack("<IIII", spec.LC_CODE_SIGNATURE, 16, sig_off, sig_size)

    if len(cmds) != sizeofcmds:
        raise LinkError(
            f"load-command size mismatch: predicted {sizeofcmds}, built {len(cmds)}"
        )

    # MH_HAS_TLV_DESCRIPTORS (0x800000): dyld only processes thread-local
    # descriptors — replacing each thunk with tlv_get_addr and allocating the
    # pthread key — when this flag is set. Without it a __thread_vars section
    # is inert and the first TLV access calls the raw __tlv_bootstrap thunk,
    # which aborts.
    mh_flags = 0x00200085
    if tlv_bind_sites:
        mh_flags |= 0x00800000
    header = spec.MACH_HEADER_64.pack({
        "magic": spec.MH_MAGIC_64, "cputype": spec.CPU_TYPE_ARM64,
        "cpusubtype": spec.CPU_SUBTYPE_ARM64_ALL, "filetype": spec.MH_EXECUTE,
        "ncmds": ncmds, "sizeofcmds": sizeofcmds,
        "flags": mh_flags, "reserved": 0,
    })

    regions = [OutputRegion(0, header + bytes(cmds), "header/load commands")]
    for out in text_sections + ([got] if code_imports else []) + data_out:
        if out.zerofill_size:
            continue  # zerofill occupies vm only; nothing in the file
        regions.append(OutputRegion(
            out.fileoff,
            bytes(out.data),
            f"{out.segname},{out.sectname}",
        ))
    regions.extend((
        OutputRegion(fixups_off, bytes(fixups), "chained fixups"),
        OutputRegion(symtab_off, bytes(nlists), "symbol table"),
        OutputRegion(strtab_off, bytes(strtab), "string table"),
    ))
    try:
        image = materialize_output(sig_off, regions)
    except ParallelLinkError as exc:
        raise LinkError(f"parallel Mach-O output failed: {exc}") from exc

    if phase_callback is not None:
        phase_callback("sign_begin")
    try:
        blob = build_signature(
            image, identifier=identifier,
            exec_seg_base=0, exec_seg_limit=text_filesize, exec_seg_flags=1,
        )
    finally:
        if phase_callback is not None:
            phase_callback("sign_end")
    if len(blob) != sig_size:
        raise LinkError(
            f"signature size mismatch: predicted {sig_size}, built {len(blob)}"
        )
    return image + blob
