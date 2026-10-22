"""Single-pass assembler controls against two frozen drivers.

The x86 assembler places and encodes every line once, at its final offset.
Two frozen sources keep that replacement honest without a system assembler:

* ``x86_64_asm_driver_two_pass.py.txt`` is the complete parse/measure/emit
  driver that the single pass replaced.  Each case must produce the same ELF
  and COFF bytes, and invalid input the same diagnostic.
* ``x86_64_parse_buffer_before_81776.py.txt`` pins the line and directive
  helpers both drivers share; they are loaded without any backend import.
"""

import ast
import hashlib
import importlib.util
from pathlib import Path
import re
import struct
import sys

import pytest

from pcc.backend import coff_x86_64 as coff
from pcc.backend import elf_x86_64 as elf
from pcc.backend import x86_64_asm_driver as assembler


DATA = Path(__file__).with_name("data")
SOURCE = Path(assembler.__file__)
BEFORE = DATA / "x86_64_parse_buffer_before_81776.py.txt"
BEFORE_SHA256 = "e68388f456dcf9036ad0f198b75fe08e09b72e520806b04d457fe5ac57b3ad3f"
TWO_PASS = DATA / "x86_64_asm_driver_two_pass.py.txt"
TWO_PASS_SHA256 = "73f78c4baccec6bfb660df9fa597a64e7aaf80f9e19e63147ab232ed0f36047d"
PREFIX = ".intel_syntax noprefix\n"

_HELPER_DEFINITIONS = frozenset({
    "_parse_int", "_data_items", "_integer_payload", "_symbol_plus_addend",
    "_section_from_directive", "_iter_assembly_lines",
})
_HELPER_CONSTANTS = frozenset({
    "_SECTION_SPECS", "_ASSEMBLY_LINE_CHUNK_CHARS", "_ASSEMBLY_LINE_BREAKS",
})


class _ParseError(ValueError):
    """No instruction encoder is imported by the helper-only boundary."""


def _helper_namespace(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    selected = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            if node.name in _HELPER_DEFINITIONS:
                selected.append(node)
        elif isinstance(node, ast.Assign):
            if (len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id in _HELPER_CONSTANTS):
                selected.append(node)
    # Deliberately omit every source import, including future annotations.
    namespace = {
        "__name__": __name__, "re": re, "struct": struct, "X86EncodeError": _ParseError,
        "SHF_ALLOC": 2, "SHF_EXECINSTR": 4, "SHF_TLS": 1024, "SHF_WRITE": 1,
        "SHT_NOBITS": 8, "SHT_PROGBITS": 1,
    }
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), namespace)
    assert _HELPER_DEFINITIONS | _HELPER_CONSTANTS <= set(namespace)
    assert "encode_instruction" not in namespace
    return namespace


def _assert_frozen_identity(raw, digest):
    # Git may check these text fixtures out as CRLF on Windows. Only that exact
    # newline substitution is ignored; body and all other whitespace stay pinned.
    normalized = raw.replace(b"\r\n", b"\n")
    assert hashlib.sha256(normalized).hexdigest() == digest


def _two_pass():
    raw = TWO_PASS.read_bytes()
    _assert_frozen_identity(raw, TWO_PASS_SHA256)
    spec = importlib.util.spec_from_loader("pcc.backend._x86_64_asm_driver_two_pass", loader=None)
    module = importlib.util.module_from_spec(spec)
    # Its relative imports bind the current encoder and ELF records.
    module.__package__ = "pcc.backend"
    # Python 3.13 dataclasses read the defining module from sys.modules.
    sys.modules[spec.name] = module
    try:
        exec(compile(raw.decode("utf-8"), str(TWO_PASS), "exec"), module.__dict__)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def _outcome(build):
    try:
        return ("ok", build())
    except Exception as error:
        return (type(error).__name__, str(error))


def _elf(driver, text):
    return _outcome(lambda: elf.emit_relocatable(driver.assemble_file(text)))


def _coff(monkeypatch, driver, text):
    with monkeypatch.context() as context:
        context.setattr(assembler, "assemble_file_keeping_labels",
                        driver.assemble_file_keeping_labels)
        return _outcome(lambda: coff.assemble_object(text))


@pytest.mark.parametrize("path, digest", [(BEFORE, BEFORE_SHA256), (TWO_PASS, TWO_PASS_SHA256)],
                         ids=["helpers", "two-pass"])
@pytest.mark.parametrize("line_ending", [b"\n", b"\r\n"])
def test_frozen_oracle_identity_accepts_lf_and_crlf(path, digest, line_ending):
    canonical = path.read_bytes().replace(b"\r\n", b"\n")
    _assert_frozen_identity(canonical.replace(b"\n", line_ending), digest)


@pytest.mark.parametrize("line_ending", [b"\n", b"\r\n"])
@pytest.mark.parametrize("mutation", ["body", "trailing-space", "bare-cr"])
def test_frozen_oracle_identity_rejects_body_and_other_whitespace_changes(line_ending, mutation):
    canonical = BEFORE.read_bytes().replace(b"\r\n", b"\n")
    if mutation == "body":
        changed = canonical.replace(b"pending_data = bytearray()", b"pending_data = bytes()", 1)
    elif mutation == "trailing-space":
        changed = canonical + b" "
    else:
        changed = canonical + b"\r"
    assert changed != canonical
    with pytest.raises(AssertionError):
        _assert_frozen_identity(changed.replace(b"\n", line_ending), BEFORE_SHA256)


_CASES = {
    "empty": PREFIX,
    "empty-section": PREFIX + ".text\n",
    "single-data": PREFIX + ".data\n.byte 7\n",
    "numeric-multichunk": PREFIX + ".data\n" + ".byte 7\n" * 8193,
    "instruction-multichunk": PREFIX + ".text\nprobe:\n" + "  nop\n" * 2053,
    "mixed-records": PREFIX + """.text
probe:
  nop
  nop
  nop
  nop
  nop
.globl probe
.type probe, @function
.size external, 0

  nop
  .byte 1, 2, 3
  .short -1
  .quad external+8
  .long finish-probe
  .float -0.0
  .double 1.25
  .p2align 3
  .zero 2
finish:
  ret
.size probe, .-probe
.data
payload:
  .long 16909060
.section .note.GNU-stack,"",@progbits
""",
    "section-return-without-text-record": PREFIX + """.text
  nop
  nop
  nop
.data
.byte 9
.text
.globl probe
.type probe, @function
.size probe, 6
  nop
  nop
  nop
""",
    "blank-lines-within-batch": PREFIX + ".data\n.byte 1\n\n\r\n.short 2\n",
    "metadata-data-barrier": PREFIX + ".data\n.byte 1\n.globl p\n.byte 2\n",
    "oversized-instruction": PREFIX + ".text\n" + "  nop\n" * 5
        + "mov eax, " + "1" * 65536 + "\n  ret\n",
    # Forward and backward local branches are patched; a global target, a
    # label in another section and an undefined name stay relocations.
    "local-and-global-branches": PREFIX + """.text
.globl entry
.type entry, @function
entry:
  jmp .Lforward
.Lback:
  cmp eax, 1
  jne .Lforward
  call helper
  call entry
  call external
  jmp .Ldata_label
.Lforward:
  je .Lback
  call .Lback
  lea rax, .Lback[rip]
  ret
.size entry, .-entry
helper:
  ret
.data
.Ldata_label:
  .quad entry
""",
    # Differences may name labels that are only defined later.
    "forward-differences": PREFIX + """.data
table:
  .long .Lthree-.Lone
  .short .Ltwo-.Lone
  .byte .Lone-.Lone
  .quad .Lthree-.Ltwo
.text
.Lone:
  nop
.Ltwo:
  mov eax, 1
.Lthree:
  ret
""",
    # A computed size may precede the matching explicit declaration.
    "sizes-and-tls": PREFIX + """.text
.globl f
.type f, @function
f:
  mov eax, DWORD PTR value[rip]
  mov rcx, QWORD PTR slot@gottpoff[rip]
  mov eax, OFFSET FLAT:tls@TPOFF
  ret
.size f, .-f
.size f, 19
.section .tdata,"awT",@progbits
.type tls_data, @tls_object
tls_data:
  .long 123
.section .tbss,"awT",@nobits
.p2align 4
.type tls_zero, @tls_object
tls_zero:
  .zero 257
.section .note.GNU-stack,"",@progbits
""",
    "seh-procedures": PREFIX + """.text
.globl probe
.seh_proc probe
probe:
  push rbp
.seh_pushreg rbp
  mov rbp, rsp
.seh_setframe rbp, 0
  sub rsp, 32
.seh_stackalloc 32
.seh_endprologue
  call external
  add rsp, 32
  pop rbp
  ret
.seh_endproc
""",
}
for _count in (1, 2, 3, 4, 5, 6, 1024, 1025, 1026):
    _CASES["run-" + str(_count)] = PREFIX + ".text\n" + "  nop\n" * _count


@pytest.mark.parametrize("name", tuple(_CASES))
@pytest.mark.parametrize("target", ("elf", "coff"))
def test_objects_and_diagnostics_match_frozen_two_pass_driver(monkeypatch, name, target):
    oracle = _two_pass()
    text = _CASES[name]
    if target == "elf":
        expected, actual = _elf(oracle, text), _elf(assembler, text)
    else:
        expected = _coff(monkeypatch, oracle, text)
        actual = _coff(monkeypatch, assembler, text)
    assert actual == expected


def test_cases_cover_patched_and_relocated_label_references():
    obj = assembler.assemble_file(_CASES["local-and-global-branches"])
    text = next(section for section in obj.sections if section.name == ".text")
    symbols = [symbol.name for symbol in obj.symbols]
    relocated = sorted(symbols[relocation.symbol_index] for relocation in text.relocations)
    # Branches and calls to .Lforward, .Lback and helper were patched; the
    # RIP-relative .Lback reference stays a relocation, as GAS-style output.
    assert relocated == [".Lback", ".Ldata_label", "entry", "external"]
    assert ".Lforward" not in symbols and "helper" not in symbols
    # jmp .Lforward: 5 + cmp 3 + jne 6 + three calls 15 + jmp 5 = 29 bytes.
    assert text.data.startswith(bytes.fromhex("e9 1d 00 00 00"))
    data = assembler.assemble_file(_CASES["forward-differences"]).sections
    table = next(section for section in data if section.name == ".data")
    assert table.data == struct.pack("<IHBQ", 6, 1, 0, 5)


@pytest.mark.parametrize("chunk_bytes", (1, 7, 16, 8192))
def test_payload_chunk_bounds_keep_two_pass_objects(monkeypatch, chunk_bytes):
    text = _CASES["local-and-global-branches"] + _CASES["sizes-and-tls"][len(PREFIX):]
    expected = _elf(_two_pass(), text)
    assert expected[0] == "ok"
    monkeypatch.setattr(assembler, "_SECTION_PAYLOAD_CHUNK_BYTES", chunk_bytes)
    assert _elf(assembler, text) == expected


def test_each_instruction_is_encoded_once_at_its_final_offset(monkeypatch):
    text = _CASES["local-and-global-branches"]
    instructions = [line.strip() for line in text.splitlines()
                    if line.startswith("  ") and not line.strip().startswith(".")]
    observed_calls = []
    for driver in (assembler, _two_pass()):
        calls = []
        real = driver.encode_instruction

        def observed(line, **kwargs):
            calls.append((line, kwargs["pc"]))
            return real(line, **kwargs)

        monkeypatch.setattr(driver, "encode_instruction", observed)
        driver.assemble_file(text)
        observed_calls.append(calls)
    single, two_pass = observed_calls
    assert [line for line, _pc in single] == instructions
    offsets = [pc for _line, pc in single]
    assert offsets == sorted(offsets) and len(set(offsets)) == len(offsets)
    # The replaced driver measured (with a per-text size memo) and then
    # encoded every line again at its final offset.
    assert [line for line, _pc in two_pass[-len(instructions):]] == instructions
    assert len(two_pass) > len(instructions)


_ERRORS = (
    "", ".text\n", PREFIX + ".data\n.byte 256,\n",
    PREFIX + ".data\n.short external\n.byte 256\n",
    PREFIX + ".data\n.byte 1\n.float wrong\n",
    PREFIX + ".data\n.byte 1\n.section .unsupported\n",
    PREFIX + ".text\n" + "  nop\n" * 5 + ".p2align 17\n",
    PREFIX + ".text\n" + "  nop\n" * 5 + ".zero -1\n",
    PREFIX + ".text\nprobe:\nunsupported eax\n",
    PREFIX + ".data\n.long missing-other\n",
    PREFIX + ".section .tbss,\"awT\",@nobits\n.byte 1\n",
    PREFIX + ".data\n.quad x\n.section .tbss,\"awT\",@nobits\n.quad y\n",
    PREFIX + ".text\ncall missing\n.size absent, 1\n",
    PREFIX + ".text\nx:\nx:\n",
    PREFIX + ".text\n.size f, .-f\n",
    PREFIX + ".text\nf:\nnop\n.size f, .-f\n.size f, 7\n",
    PREFIX + ".text\nf:\nnop\n.size f, 7\n.size f, .-f\n",
    PREFIX + ".text\nnop\n.data\nmov eax, 1\n",
)


@pytest.mark.parametrize("text", _ERRORS)
def test_first_error_matches_frozen_two_pass_driver(text):
    expected = _elf(_two_pass(), text)
    assert expected[0] != "ok"
    assert _elf(assembler, text) == expected


def test_first_error_is_the_first_in_source_order():
    # The two-pass driver raised every directive error before any layout
    # error; one pass reports whichever line fails first.
    text = PREFIX + '.section .tbss,"awT",@nobits\n.byte 1\n.short external\n'
    assert _elf(_two_pass(), text) == (
        "X86EncodeError", "symbol-valued .short is not proven: '.short external'")
    assert _elf(assembler, text) == ("X86EncodeError", "NOBITS section '.tbss' has file data")


@pytest.mark.parametrize("name", tuple(_CASES))
def test_line_iterator_matches_frozen_helper(name):
    before, after = _helper_namespace(BEFORE), _helper_namespace(SOURCE)
    text = _CASES[name]
    assert list(after["_iter_assembly_lines"](text)) == list(before["_iter_assembly_lines"](text))


_HELPER_CALLS = (
    ("_parse_int", (" 0x10 ",)), ("_parse_int", ("seven",)),
    ("_data_items", ("1, 2 ,3",)), ("_data_items", ("1,,2",)),
    ("_integer_payload", (-1, 2), {"owner": ".short"}),
    ("_integer_payload", (256, 1), {"owner": ".byte"}),
    ("_symbol_plus_addend", ("external+8",)), ("_symbol_plus_addend", ("a - 3",)),
    ("_symbol_plus_addend", ("bad name",)),
    ("_section_from_directive", ('.section .tbss,"awT",@nobits',)),
    ("_section_from_directive", ('.section .init_array.65535,"aw",@progbits',)),
    ("_section_from_directive", ('.section .tbss,"aw",@nobits',)),
    ("_section_from_directive", (".section .unsupported",)),
)


@pytest.mark.parametrize("call", _HELPER_CALLS, ids=lambda call: call[0])
def test_directive_helpers_match_frozen_helpers(call):
    name, args = call[0], call[1]
    kwargs = call[2] if len(call) > 2 else {}
    outcomes = []
    for namespace in (_helper_namespace(BEFORE), _helper_namespace(SOURCE)):
        outcomes.append(_outcome(lambda: namespace[name](*args, **kwargs)))
    assert outcomes[0] == outcomes[1]
