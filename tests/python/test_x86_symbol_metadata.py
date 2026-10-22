"""Private x86 metadata defaults preserve ELF/COFF object semantics."""

import pytest

from pcc.backend import coff_x86_64 as coff
from pcc.backend import elf_x86_64 as elf
from pcc.backend import x86_64_asm_driver as assembler


UNDECLARED = """.intel_syntax noprefix
.text
entry:
  call external
  call .Llocal
  ret
.Llocal:
  ret
unreferenced:
  ret
"""

DECLARED = """.intel_syntax noprefix
.text
.globl entry
.globl entry
.type entry, @function
.type entry, @function
entry:
  nop
  ret
.size entry, 2
.size entry, .-entry
.size entry, .-entry
.data
.type value, @object
.type value, @object
value:
  .quad entry
.size value, 8
.size value, 8
"""

BRANCHES = """.intel_syntax noprefix
.text
.globl entry
.type entry, @function
entry:
  call helper
  call .Llocal
  ret
.Llocal:
  ret
.globl helper
.type helper, @function
helper:
  ret
"""

TLS = """.intel_syntax noprefix
.section .tdata
.globl tls_value
.type tls_value, @tls_object
tls_value:
  .quad 7
.section .tbss
.type local_tls, @tls_object
local_tls:
  .zero 8
.data
.globl pointer
pointer:
  .quad tls_value
  .quad local_tls
"""

EXTERNALS = """.intel_syntax noprefix
.globl untyped
.type external_object, @object
.type external_tls, @tls_object
.text
entry:
  call unknown
  ret
"""

UNWIND = """.intel_syntax noprefix
.text
.globl entry
.type entry, @function
.seh_proc entry
entry:
  push rbp
.seh_pushreg rbp
  mov rbp, rsp
.seh_setframe rbp, 0
  sub rsp, 32
.seh_stackalloc 32
.seh_endprologue
  xor eax, eax
  add rsp, 32
  pop rbp
  ret
.seh_endproc
"""

SUCCESS_CASES = (
    ("undeclared", UNDECLARED, ("entry",)),
    ("declared", DECLARED, ()),
    ("branches", BRANCHES, ()),
    ("tls", TLS, ()),
    ("externals", EXTERNALS, ()),
    ("zero-size", ".intel_syntax noprefix\n.text\nmarker:\n  ret\n.size marker, 0\n", ()),
    ("implicit-objects", ".intel_syntax noprefix\n.section .tdata\nimplicit_tls:\n  .quad 9\n"
     ".data\npointer:\n  .quad implicit_tls\n  .quad pointer\n", ()),
)

ERROR_CASES = (
    (".type value, @object\n.type value, @function\n",
     "conflicting .type directives for 'value'"),
    (".text\n.size value, -1\n", "negative symbol size in '.size value, -1'"),
    (".text\n.size value, 1\n.size value, 2\n",
     "conflicting .size directives for 'value'"),
    (".text\n.size value, 1\n",
     "assembly metadata names symbols without definitions: ['value']"),
    (".text\nvalue:\nvalue:\n", "duplicate assembly label 'value'"),
    (".text\nvalue:\n  nop\n.size value, 0\n.size value, .-value\nvalue:\n",
     "conflicting .size directives for 'value'"),
    (".text\n.size missing, .-missing\n", ".size references non-local symbol 'missing'"),
)


def _count_metadata(monkeypatch):
    original = assembler._SymbolMeta
    records = []

    def make_record():
        value = original()
        records.append(value)
        return value

    monkeypatch.setattr(assembler, "_SymbolMeta", make_record)
    return records


def test_undeclared_labels_and_references_need_no_metadata_records(monkeypatch):
    records = _count_metadata(monkeypatch)
    obj = assembler.assemble_file_keeping_labels(UNDECLARED, ("entry",))
    assert records == []
    symbols = {symbol.name: symbol for symbol in obj.symbols if symbol.name}
    assert set(symbols) == {"entry", "external"}
    assert symbols["entry"].binding == elf.STB_LOCAL
    assert symbols["entry"].type == elf.STT_FUNC
    assert symbols["entry"].size == 0
    assert symbols["external"].section_index == 0
    assert symbols["external"].type == elf.STT_NOTYPE
    text = next(section for section in obj.sections if section.name == ".text")
    assert len(text.relocations) == 1
    assert obj.symbols[text.relocations[0].symbol_index].name == "external"
    assert elf.parse_relocatable(elf.emit_relocatable(obj)) == obj


def test_declarations_keep_one_independent_record_per_symbol(monkeypatch):
    records = _count_metadata(monkeypatch)
    _sections, _order, metadata, _labels = assembler._encode_file(
        DECLARED, assembler._OperandParseCache(),
    )
    # Computed sizes update the declarations' records; none is added.
    assert len(records) == 2
    assert list(metadata) == ["entry", "value"]
    assert metadata["entry"] is records[0]
    assert metadata["value"] is records[1]
    assert metadata["entry"] is not metadata["value"]
    assert metadata["entry"].global_ is True
    assert metadata["entry"].type == elf.STT_FUNC
    assert metadata["entry"].size == 2
    assert metadata["value"].global_ is False
    assert metadata["value"].type == elf.STT_OBJECT
    assert metadata["value"].size == 8
    metadata["entry"].size = 99
    assert metadata["value"].size == 8


def test_existing_metadata_reads_allocate_no_defaults(monkeypatch):
    records = _count_metadata(monkeypatch)
    obj = assembler.assemble_file(DECLARED)
    assert len(records) == 2
    symbols = {symbol.name: symbol for symbol in obj.symbols if symbol.name}
    assert [(symbols[name].binding, symbols[name].type, symbols[name].size)
            for name in ("entry", "value")] == [
                (elf.STB_GLOBAL, elf.STT_FUNC, 2),
                (elf.STB_LOCAL, elf.STT_OBJECT, 8),
            ]


@pytest.mark.parametrize("name,source,keep_labels", SUCCESS_CASES,
                         ids=[row[0] for row in SUCCESS_CASES])
@pytest.mark.parametrize("format", ("elf", "coff"))
def test_emitted_objects_round_trip_metadata(name, source, keep_labels, format):
    if format == "elf":
        obj = assembler.assemble_file_keeping_labels(source, keep_labels)
        data = elf.emit_relocatable(obj)
        assert data.startswith(b"\x7fELF")
        assert elf.parse_relocatable(data) == obj
    else:
        obj = coff.assemble(source)
        data = coff.emit_object(obj)
        assert data.startswith(b"\x64\x86")
        assert coff.parse_object(data) == obj
    if name == "branches":
        text = next(section for section in obj.sections if section.name == ".text")
        assert len(text.relocations) == 1
        row = text.relocations[0]
        index = row.symbol_index if format == "elf" else row.symbol
        assert obj.symbols[index].name == "helper"
    elif name == "externals":
        symbols = {symbol.name: symbol for symbol in obj.symbols if symbol.name}
        assert set(symbols) == {"untyped", "external_object", "external_tls", "unknown"}
        if format == "elf":
            assert [symbols[name].type for name in
                    ("untyped", "external_object", "external_tls", "unknown")] == [0, 1, 6, 0]
    elif name == "zero-size":
        symbol = next(symbol for symbol in obj.symbols if symbol.name == "marker")
        if format == "elf":
            assert symbol.size == 0 and symbol.type == elf.STT_FUNC
        else:
            assert symbol.function
    elif name == "implicit-objects" and format == "elf":
        symbols = {symbol.name: symbol for symbol in obj.symbols if symbol.name}
        assert symbols["implicit_tls"].type == elf.STT_TLS
        assert symbols["pointer"].type == elf.STT_OBJECT


def test_coff_unwind_keeps_undeclared_marker_offsets():
    obj = coff.assemble(UNWIND)
    data = coff.emit_object(obj)
    assert coff.parse_object(data) == obj
    sections = {section.name: section for section in obj.sections}
    assert len(sections[".pdata"].data) == 12
    assert len(sections[".pdata"].relocations) == 3
    assert sections[".xdata"].data
    names = {symbol.name for symbol in obj.symbols}
    assert any(name.startswith(".Lpcc_seh_") for name in names)


@pytest.mark.parametrize("source,message", ERROR_CASES)
def test_metadata_errors_keep_first_failure(source, message):
    with pytest.raises(assembler.X86EncodeError) as caught:
        assembler.assemble_file(".intel_syntax noprefix\n" + source)
    assert str(caught.value) == message
