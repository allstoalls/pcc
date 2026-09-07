"""Independent byte differentials for the A64 encoder.

One corpus, three encoders: every operand shape the self backend emits is
assembled by as(1), by the LLVM MC/AsmPrinter embedded in the repository-pinned
llvmlite wheel, and by `pcc.backend.arm64_encode`.  The instruction words must
match exactly. Extern-referencing instructions (bl, adrp/@PAGEOFF, GOT loads)
are additionally checked for relocation equality against as(1) — the fixup
fields must be zero-filled the same way as(1) leaves them.

The corpus below IS the proven subset: a shape the self backend starts
emitting that is not covered here will fail in the encoder (EncodeError),
not silently mis-encode.
"""

from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
from importlib.metadata import version
from pathlib import Path

import pytest
from llvmlite import binding as llvm

from pcc.backend import macho_spec as spec
from pcc.backend.aarch64_fp_immediates import DIRECT_FP_IMMEDIATE_ENCODINGS
from pcc.backend.arm64_encode import (
    EncodeError,
    assemble_text,
    assemble_text_lines,
)
from pcc.backend.self_backend_aarch64_darwin_regs import emit_fp_constant
from pcc.backend.self_backend_float_bits import float32_to_bits, float64_to_bits
from pcc.backend.self_backend_ir import TypeDesc

_CC = shutil.which(os.environ.get("CC", "cc"))
_OTOOL = shutil.which("otool")
_IS_ARM64_DARWIN = os.uname().sysname == "Darwin" and os.uname().machine == "arm64"
_LLVMLITE_VERSION = version("llvmlite")
_PINNED_LLVMLITE_VERSION = "0.46.0"
_LLVM_MC_PROVENANCE = (
    f"llvmlite=={_LLVMLITE_VERSION}; "
    f"LLVM {'.'.join(str(v) for v in llvm.llvm_version_info)}; "
    "binding.TargetMachine.emit_object"
)
if not _IS_ARM64_DARWIN:
    _GATE = "needs Darwin arm64"
elif _CC is None or _OTOOL is None:
    _GATE = "needs cc and otool"
elif _LLVMLITE_VERSION != _PINNED_LLVMLITE_VERSION:
    _GATE = (
        "LLVM MC oracle provenance changed: expected llvmlite=="
        + _PINNED_LLVMLITE_VERSION
        + ", got "
        + _LLVMLITE_VERSION
    )
else:
    _GATE = None

pytestmark = pytest.mark.pcc_gate(unavailable=_GATE)


def test_line_input_api_matches_string_projection() -> None:
    text = """\
_entry:
  movz w0, #42
  cbz w0, L_done
L_done:
  ret
"""
    assert assemble_text_lines(text.splitlines()) == assemble_text(text)


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120, **kw)


def _llvm_ir_string(text: str) -> str:
    """Quote one assembler line as an LLVM IR string without locale escapes."""
    out = []
    for byte in text.encode("utf-8"):
        if 0x20 <= byte <= 0x7E and byte not in (ord('"'), ord("\\")):
            out.append(chr(byte))
        else:
            out.append("\\" + format(byte, "02X"))
    return "".join(out)


def _llvm_mc_object(asm_text: str, *, symbol: str) -> bytes:
    """Assemble through llvmlite's pinned LLVM MC/AsmPrinter oracle."""
    llvm.initialize_all_targets()
    llvm.initialize_all_asmprinters()
    llvm.initialize_native_asmparser()
    triple = llvm.get_default_triple()
    asm_lines = [
        ".section __TEXT,__text,regular,pure_instructions",
        ".globl " + symbol,
        ".p2align 2",
        *asm_text.splitlines(),
        ".subsections_via_symbols",
    ]
    ir_text = "target triple = \"" + triple + "\"\n" + "".join(
        'module asm "' + _llvm_ir_string(line) + '"\n'
        for line in asm_lines
    )
    module = llvm.parse_assembly(ir_text)
    module.verify()
    target_machine = llvm.Target.from_triple(triple).create_target_machine()
    module.data_layout = str(target_machine.target_data)
    return target_machine.emit_object(module)


def _text_bytes(object_bytes: bytes) -> bytes:
    obj = spec.parse_object(object_bytes)
    section = next(
        section
        for section in obj.sections()
        if section["segname_str"] == "__TEXT"
        and section["sectname_str"] == "__text"
    )
    return obj.data[section["offset"]:section["offset"] + section["size"]]


def _disassembled_instructions(path: Path) -> list[str]:
    result = _run([_OTOOL, "-tvV", str(path)])
    assert result.returncode == 0, result.stderr
    instructions = []
    for raw_line in result.stdout.splitlines():
        match = re.match(r"^\s*[0-9a-fA-F]+\s+(.+?)\s*$", raw_line)
        if match is not None:
            instructions.append(" ".join(match.group(1).split()))
    assert instructions, "otool emitted no __text instructions for " + str(path)
    return instructions


# Every operand shape measured from real self-backend output
# (docs/goal/evidence/2026-08-01-obj-switch-encoder-sized.md), plus local
# branch flavors in both directions.
CORPUS = """\
_f:
Lback:
	sub	sp, sp, #48
	stp	x29, x30, [sp, #-16]!
	mov	x29, sp
	paciasp
	sub	x9, x29, #32
	sub	x10, x9, x11
	add	x0, x1, x2
	sub	sp, sp, x15
	add	sp, sp, x15
	add	x12, sp, x15
	cmp	sp, x9
	add	sp, sp, #48
	movz	x9, #4660
	movz	x9, #4660, lsl #16
	movz	w8, #7
	movz	w8, #2, lsl #16
	movk	x9, #43981, lsl #32
	mov	x3, x4
	mov	w5, w6
	mov	xzr, x0
	mov	wzr, w0
	stur	x9, [x29, #-24]
	stur	w8, [x29, #-28]
	stur	xzr, [x29, #-16]
	stur	x9, [x10]
	ldur	x9, [x29, #-24]
	ldur	w8, [x29, #-28]
	ldur	x9, [x10]
	sturb	w8, [x29, #-1]
	sturb	wzr, [x29, #-2]
	sturb	w8, [x10]
	ldurb	w8, [x29, #-1]
	ldurb	w8, [x10]
	ldr	x9, [x10]
	ldr	w8, [x10]
	str	x9, [x10]
	str	w8, [x10]
	stur	d9, [x29, #-24]
	ldur	d9, [x29, #-24]
	stur	s8, [x29, #-20]
	ldur	s8, [x29, #-20]
	str	d10, [x9, #8]
	ldr	d10, [x9, #8]
	str	s11, [x9, #4]
	ldr	s11, [x9, #4]
	ldar	x9, [x10]
	ldar	w8, [x10]
	ldaxr	x9, [x10]
	ldaxr	w8, [x10]
	stlr	x9, [x10]
	stlr	w8, [x10]
	stlxr	w12, x9, [x10]
	stlxr	w12, w8, [x10]
	ldaxrb	w8, [x10]
	stlrb	w8, [x10]
	stlxrb	w12, w8, [x10]
	clrex
	dmb	ish
	cmp	x9, x10
	cmp	x9, #0
	cmp	w8, w9
	cmp	w8, #31
	and	x9, x9, x10
	and	w8, w8, w9
	and	w8, w8, #0xff
	orr	x9, x9, x10
	eor	x9, x9, x10
	asrv	x9, x9, x10
	lslv	x9, x9, x10
	cset	w8, eq
	cset	w8, ne
	cset	w8, lt
	cset	w8, ge
	cset	w8, gt
	cset	w8, le
	csel	w8, w9, w10, ne
	csel	x9, x10, x11, eq
	cbz	w8, Lfwd
	cbz	x9, Lfwd
	cbnz	w8, Lfwd
	b.eq	Lfwd
	b.ne	Lfwd
	b.ge	Lback
	b.lt	Lback
	b	Lfwd
Lfwd:
	bl	_extern_fn
	adrp	x0, _extern_data@PAGE
	add	x0, x0, _extern_data@PAGEOFF
	adrp	x1, _extern_got@GOTPAGE
	ldr	x1, [x1, _extern_got@GOTPAGEOFF]
	mul	x9, x10, x11
	mul	w8, w9, w10
	sdiv	x9, x10, x11
	sdiv	w8, w9, w10
	udiv	x9, x10, x11
	smulh	x9, x10, x11
	umulh	x9, x10, x11
	smull	x9, w10, w11
	umull	x9, w10, w11
	madd	x11, x9, x10, x12
	msub	x9, x10, x11, x12
	neg	x9, x10
	neg	w8, w9
	asr	x9, x10, #3
	asr	w8, w9, #5
	lsr	x9, x10, #7
	lsl	x9, x10, #4
	lsl	w8, w9, #1
	tst	x9, x10
	tst	w8, w9
	sxtb	w8, w9
	sxth	w8, w9
	sxtw	x9, w8
	clz	x9, x10
	clz	w8, w9
	rbit	x9, x10
	rev	x9, x10
	rev	w8, w9
	rev16	w8, w9
	cneg	x9, x10, lt
	csinv	x9, x10, x11, ge
	blr	x9
	brk	#1
	fadd	d0, d1, d2
	fsub	d0, d1, d2
	fmul	d0, d1, d2
	fdiv	d0, d1, d2
	fneg	d0, d1
	fabs	d0, d1
	fsqrt	d0, d1
	frintm	d11, d9
	frintn	d0, d1
	frintp	d31, d0
	frintz	d5, d17
	fmov	d0, d1
	fmov	d0, x9
	fmov	x9, d0
	fmov	d9, #1.0
	fmov	d10, #2.0
	fmov	s8, #1.0
	fmov	s11, #2.0
	fcvt	d0, s1
	fcvt	s1, d0
	scvtf	d0, x9
	scvtf	d0, w8
	fcvtzs	x9, d0
	fcvtzs	w8, d0
	fcmp	d0, d1
	fcmp	d0, #0.0
	fcsel	d0, d1, d2, ne
	adds	x11, x9, x10
	adds	w11, w9, w10
	subs	x11, x9, x10
	subs	w11, w9, w10
	uxtw	x11, w11
	ucvtf	d0, x9
	ucvtf	d0, w8
	fcvtzu	x9, d0
	fcvtzu	w8, d0
	lsrv	x9, x10, x11
	lsrv	w8, w9, w10
	ldrb	w8, [x9]
	strb	w8, [x9]
	ldrh	w30, [sp, #2]
	strh	w0, [x1, #8190]
	strh	wzr, [x9, #4]
	ldurh	w2, [x3, #-256]
	sturh	wzr, [x9, #255]
	cnt	v10.8b, v10.8b
	addv	b10, v10.8b
	umov	w11, v10.b[0]
	ldp	x29, x30, [sp], #16
	ldp	x19, x20, [sp, #16]
	stp	x19, x20, [sp, #32]
	autiasp
	ret
"""


def _as_reference(tmp_path: Path) -> tuple[bytes, list]:
    asm = (
        "\t.section\t__TEXT,__text,regular,pure_instructions\n"
        "\t.globl\t_f\n\t.p2align\t2\n"
        + CORPUS
        + "\t.subsections_via_symbols\n"
    )
    src = tmp_path / "corpus.s"
    src.write_text(asm, encoding="utf-8")
    obj_path = tmp_path / "corpus.o"
    build = _run(["xcrun", "as", "-o", str(obj_path), str(src)])
    assert build.returncode == 0, build.stderr
    obj = spec.parse_object(obj_path.read_bytes())
    sec = obj.sections()[0]
    code = obj.data[sec["offset"]:sec["offset"] + sec["size"]]
    names = [s["name"] for s in obj.symbols()]
    relocs = [
        (r["r_address"], names[r["r_symbolnum"]], r["r_type"],
         r["r_pcrel"], r["r_length"])
        for r in obj.relocations(sec)
    ]
    return code, sorted(relocs)


def test_every_instruction_word_matches_as(tmp_path):
    ref_code, _ = _as_reference(tmp_path)
    ours = assemble_text(CORPUS)
    assert len(ours.code) == len(ref_code), (
        f"instruction count differs: pcc {len(ours.code)//4}, "
        f"as {len(ref_code)//4}"
    )
    mismatches = []
    lines = [
        l.strip() for l in CORPUS.splitlines()
        if l.strip() and not l.strip().endswith(":")
    ]
    for i in range(0, len(ref_code), 4):
        ref_word = struct.unpack_from("<I", ref_code, i)[0]
        our_word = struct.unpack_from("<I", ours.code, i)[0]
        if ref_word != our_word:
            mismatches.append(
                f"  +{i:#06x} {lines[i // 4]!r}: "
                f"as {ref_word:#010x}, pcc {our_word:#010x}"
            )
    assert not mismatches, (
        "encodings diverge from as(1):\n" + "\n".join(mismatches)
    )


def test_every_instruction_word_matches_pinned_llvm_mc():
    ref_code = _text_bytes(_llvm_mc_object(CORPUS, symbol="_f"))
    ours = assemble_text(CORPUS)
    assert len(ours.code) == len(ref_code), (
        f"instruction count differs: pcc {len(ours.code)//4}, "
        f"LLVM MC {len(ref_code)//4}; oracle={_LLVM_MC_PROVENANCE}"
    )
    mismatches = []
    lines = [
        line.strip() for line in CORPUS.splitlines()
        if line.strip() and not line.strip().endswith(":")
    ]
    for offset in range(0, len(ref_code), 4):
        ref_word = struct.unpack_from("<I", ref_code, offset)[0]
        our_word = struct.unpack_from("<I", ours.code, offset)[0]
        if ref_word != our_word:
            mismatches.append(
                f"  +{offset:#06x} {lines[offset // 4]!r}: "
                f"LLVM MC {ref_word:#010x}, pcc {our_word:#010x}"
            )
    assert not mismatches, (
        "encodings diverge from the pinned LLVM MC oracle "
        f"({_LLVM_MC_PROVENANCE}):\n" + "\n".join(mismatches)
    )


def test_llvm_mc_and_pcc_objects_disassemble_to_the_same_instructions(tmp_path):
    """Second oracle: both byte streams survive Mach-O disassembly equally."""
    from pcc.backend import macho_obj
    from pcc.backend.macho_obj import Section, TextSymbol, TEXT_SECTION_FLAGS

    corpus = """\
_roundtrip:
	sub	sp, sp, #32
	stp	x29, x30, [sp, #-16]!
	mov	x29, sp
	movz	x9, #4660
	add	x0, x1, x2
	cmp	x0, #0
	cset	w8, ne
	fadd	d0, d1, d2
	ldp	x29, x30, [sp], #16
	add	sp, sp, #32
	ret
"""
    llvm_path = tmp_path / "llvm-mc.o"
    llvm_path.write_bytes(_llvm_mc_object(corpus, symbol="_roundtrip"))

    ours = assemble_text(corpus)
    pcc_path = tmp_path / "pcc-encoder.o"
    pcc_path.write_bytes(macho_obj.emit_object(
        [Section(
            sectname="__text",
            segname="__TEXT",
            data=ours.code,
            align_log2=2,
            flags=TEXT_SECTION_FLAGS,
            symbols=(TextSymbol("_roundtrip", 0),),
            relocations=tuple(ours.relocations),
        )],
        undefined=ours.undefined,
    ))

    assert _disassembled_instructions(pcc_path) == _disassembled_instructions(
        llvm_path
    ), "pcc and LLVM MC objects do not round-trip through otool identically"


def test_relocations_match_as(tmp_path):
    _, ref_relocs = _as_reference(tmp_path)
    ours = assemble_text(CORPUS)
    our_relocs = sorted(
        (r.offset, r.symbol, r.type, 1 if r.pcrel else 0, r.length)
        for r in ours.relocations
    )
    assert our_relocs == ref_relocs, (
        f"\n  pcc: {our_relocs}\n  as:  {ref_relocs}"
    )
    assert ours.undefined == ["_extern_data", "_extern_fn", "_extern_got"]


def test_assembled_output_plugs_into_the_object_writer(tmp_path):
    """End-to-end: encoder -> macho_obj -> system ld -> runs."""
    from pcc.backend import macho_obj
    from pcc.backend.macho_obj import Section, TextSymbol, TEXT_SECTION_FLAGS

    ours = assemble_text(
        "_answer:\n"
        "\tpaciasp\n"
        "\tstp\tx29, x30, [sp, #-16]!\n"
        "\tmov\tx29, sp\n"
        "\tbl\t_bump\n"
        "\tadd\tw0, w0, #2\n"
        "\tldp\tx29, x30, [sp], #16\n"
        "\tautiasp\n"
        "\tret\n"
    )
    obj = tmp_path / "answer.o"
    obj.write_bytes(macho_obj.emit_object(
        [Section(
            sectname="__text", segname="__TEXT", data=ours.code, align_log2=2,
            flags=TEXT_SECTION_FLAGS,
            symbols=(TextSymbol("_answer", 0),),
            relocations=tuple(ours.relocations),
        )],
        undefined=ours.undefined,
    ))
    main_c = tmp_path / "main.c"
    main_c.write_text(
        "extern int answer(void);\n"
        "int bump(void) { return 40; }\n"
        "int main(void) { return answer() == 42 ? 0 : 1; }\n",
        encoding="utf-8",
    )
    binary = tmp_path / "prog"
    link = _run([_CC, str(main_c), str(obj), "-o", str(binary)])
    assert link.returncode == 0, link.stderr
    assert _run([str(binary)]).returncode == 0


def test_madd_has_the_proven_four_gpr_encoding():
    assembled = assemble_text("\tmadd\tx0, x1, x2, x3\n")

    assert assembled.code == struct.pack("<I", 0x9B020C20)
    assert assembled.relocations == []


def test_mov_register_31_uses_zero_register_width_not_sp_alias(tmp_path):
    asm = "_mov_zero:\n\tmov\txzr, x0\n\tmov\twzr, w0\n"
    assembled = assemble_text(asm)

    assert assembled.code == struct.pack("<2I", 0xAA0003FF, 0x2A0003FF)
    assert assembled.code == _text_bytes(
        _llvm_mc_object(asm, symbol="_mov_zero")
    )

    source = tmp_path / "mov-zero.s"
    source.write_text(
        ".section __TEXT,__text,regular,pure_instructions\n"
        ".globl _mov_zero\n"
        + asm
        + ".subsections_via_symbols\n",
        encoding="utf-8",
    )
    oracle = tmp_path / "mov-zero.o"
    run = _run(["xcrun", "as", "-o", str(oracle), str(source)])
    assert run.returncode == 0, run.stderr
    assert assembled.code == _text_bytes(oracle.read_bytes())


def test_fmov_direct_immediate_inventory_is_shared_with_emitter():
    f64 = TypeDesc("fp", 64)
    for literal, encoded in DIRECT_FP_IMMEDIATE_ENCODINGS:
        assert emit_fp_constant(f64, "d0", literal) == [
            f"  fmov d0, #{literal}"
        ]
        word, = struct.unpack("<I", assemble_text(
            f"\tfmov\td0, #{literal}\n"
        ).code)
        assert (word >> 13) & 0xFF == encoded

    assert all(
        "#0.5" not in line
        for line in emit_fp_constant(f64, "d0", "0.5")
    )


def test_float32_subnormal_rounds_to_nearest_even():
    assert float32_to_bits(2.0 ** -149) == 0x00000001
    assert float32_to_bits(-(2.0 ** -149)) == 0x80000001
    assert float32_to_bits(2.0 ** -150) == 0x00000000
    assert float32_to_bits((2.0 ** -150) + (2.0 ** -151)) == 0x00000001
    assert float32_to_bits((2.0 ** -126) - (2.0 ** -149)) == 0x007FFFFF
    assert float32_to_bits(2.0 ** -126) == 0x00800000


def test_float64_subnormal_bits_are_not_flushed_to_zero():
    assert float64_to_bits(2.0 ** -1074) == 0x0000000000000001
    assert float64_to_bits(-(2.0 ** -1074)) == 0x8000000000000001
    assert float64_to_bits(float.fromhex("0x0.fffffffffffffp-1022")) == (
        0x000FFFFFFFFFFFFF
    )


def test_fails_closed_outside_the_proven_subset():
    with pytest.raises(EncodeError):
        assemble_text("\tmadd\tw0, w1, w2, w3\n")  # i64 slice only
    with pytest.raises(EncodeError):
        assemble_text("\tmadd\tx0, w1, x2, x3\n")  # mixed GPR widths
    with pytest.raises(EncodeError):
        assemble_text("\tadd\tx0, x1, #4096\n")  # imm12 overflow
    with pytest.raises(EncodeError):
        assemble_text("\tmovz\tx0, #65536\n")  # imm16 overflow
    with pytest.raises(EncodeError):
        assemble_text("\tldur\tx0, [x1, #256]\n")  # 9-bit signed overflow
    with pytest.raises(EncodeError):
        assemble_text("\tb\tL_missing\n")  # unknown assembler-local label
    with pytest.raises(EncodeError):
        assemble_text("\tand\tx0, x1, #0\n")  # invalid bitmask immediate
    with pytest.raises(EncodeError):
        assemble_text("\t.quad 1\n")  # directives are the caller's job
    with pytest.raises(EncodeError):
        assemble_text("l:\nl:\n\tret\n")  # duplicate label
    with pytest.raises(EncodeError):
        assemble_text("\tb.eq\tnowhere\n")  # unknown label
