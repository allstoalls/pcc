from __future__ import annotations

from . import BackendUnavailable
from .self_backend_aarch64_darwin_abi import (
    abi_value_reg_names,
    aggregate_fits_reg_abi,
    reg_name,
    reg_name_indexed,
)
from .self_backend_aarch64_darwin_mem import (
    emitted_addsub_register_line,
    emitted_cset_line,
    emitted_move_register_line,
)
from .self_backend_ir import TypeDesc
from .self_backend_kernel import (
    TYPE_KIND_FP,
    TYPE_KIND_INT,
    TYPE_KIND_PTR,
    IndexedFunctionKernel,
)
from .self_backend_value_arena import CompilerInt4


def emit_binop(op: str, value_type: TypeDesc) -> list[str]:
    if not value_type.is_int:
        raise BackendUnavailable(
            f"self backend only supports integer binops, got {value_type.describe()}"
        )
    r9 = reg_name(value_type, 9)
    r10 = reg_name(value_type, 10)
    r11 = reg_name(value_type, 11)
    if op == "add" or op == "sub":
        return [emitted_addsub_register_line(op, r11, r9, r10)]
    mapping = {
        "mul": f"  mul {r11}, {r9}, {r10}",
        "sdiv": f"  sdiv {r11}, {r9}, {r10}",
        "udiv": f"  udiv {r11}, {r9}, {r10}",
        "and": f"  and {r11}, {r9}, {r10}",
        "or": f"  orr {r11}, {r9}, {r10}",
        "xor": f"  eor {r11}, {r9}, {r10}",
        "shl": f"  lslv {r11}, {r9}, {r10}",
        "lshr": f"  lsrv {r11}, {r9}, {r10}",
        "ashr": f"  asrv {r11}, {r9}, {r10}",
    }
    if op == "srem":
        return [
            f"  sdiv {r11}, {r9}, {r10}",
            f"  msub {r11}, {r11}, {r10}, {r9}",
        ]
    if op == "urem":
        return [
            f"  udiv {r11}, {r9}, {r10}",
            f"  msub {r11}, {r11}, {r10}, {r9}",
        ]
    if op not in mapping:
        raise BackendUnavailable(f"self backend does not support binop {op!r}")
    return [mapping[op]]


def emit_binop_indexed(
    kernel: IndexedFunctionKernel, op: str, type_id: int,
    lhs_register: int = 9, rhs_register: int = 10, result_register: int = 11,
) -> list[str]:
    header: CompilerInt4 = kernel.type_header(type_id)
    if header.first != TYPE_KIND_INT:
        raise BackendUnavailable(
            f"self backend only supports integer binops, got type_id={type_id}"
        )
    r9 = reg_name_indexed(kernel, type_id, lhs_register)
    r10 = reg_name_indexed(kernel, type_id, rhs_register)
    r11 = reg_name_indexed(kernel, type_id, result_register)
    if op == "add" or op == "sub":
        return [emitted_addsub_register_line(op, r11, r9, r10)]
    mapping = {
        "mul": f"  mul {r11}, {r9}, {r10}",
        "sdiv": f"  sdiv {r11}, {r9}, {r10}",
        "udiv": f"  udiv {r11}, {r9}, {r10}",
        "and": f"  and {r11}, {r9}, {r10}",
        "or": f"  orr {r11}, {r9}, {r10}",
        "xor": f"  eor {r11}, {r9}, {r10}",
        "shl": f"  lslv {r11}, {r9}, {r10}",
        "lshr": f"  lsrv {r11}, {r9}, {r10}",
        "ashr": f"  asrv {r11}, {r9}, {r10}",
    }
    # The quotient cannot overwrite either input before MSUB consumes it.
    quotient = reg_name_indexed(kernel, type_id, 11)
    if op == "srem":
        return [
            f"  sdiv {quotient}, {r9}, {r10}",
            f"  msub {r11}, {quotient}, {r10}, {r9}",
        ]
    if op == "urem":
        return [
            f"  udiv {quotient}, {r9}, {r10}",
            f"  msub {r11}, {quotient}, {r10}, {r9}",
        ]
    if op not in mapping:
        raise BackendUnavailable(f"self backend does not support binop {op!r}")
    return [mapping[op]]


def emit_aggregate_bitwise_binop(
    op: str,
    value_type: TypeDesc,
    *,
    lhs_start: int,
    rhs_start: int,
    dest_start: int,
) -> list[str]:
    if not (value_type.is_array or value_type.is_struct):
        raise BackendUnavailable(
            f"self backend aggregate bitwise binop expected aggregate type, got {value_type.describe()}"
        )
    if not aggregate_fits_reg_abi(value_type):
        raise BackendUnavailable(
            "self backend aggregate bitwise binop currently requires register-fit aggregates, got "
            f"{value_type.describe()}"
        )
    mnemonic = {"and": "and", "or": "orr", "xor": "eor"}.get(op)
    if mnemonic is None:
        raise BackendUnavailable(
            f"self backend aggregate binop currently only supports and/or/xor, got {op!r}"
        )
    lines: list[str] = []
    for lhs_reg, rhs_reg, dest_reg in zip(
        abi_value_reg_names(value_type, lhs_start),
        abi_value_reg_names(value_type, rhs_start),
        abi_value_reg_names(value_type, dest_start),
    ):
        lines.append(f"  {mnemonic} {dest_reg}, {lhs_reg}, {rhs_reg}")
    return lines


def emit_fbinop(op: str, value_type: TypeDesc) -> list[str]:
    if not value_type.is_fp:
        raise BackendUnavailable(
            f"self backend only supports floating binops on fp values, got {value_type.describe()}"
        )
    r9 = reg_name(value_type, 9)
    r10 = reg_name(value_type, 10)
    r11 = reg_name(value_type, 11)
    mapping = {
        "fadd": f"  fadd {r11}, {r9}, {r10}",
        "fsub": f"  fsub {r11}, {r9}, {r10}",
        "fmul": f"  fmul {r11}, {r9}, {r10}",
        "fdiv": f"  fdiv {r11}, {r9}, {r10}",
    }
    if op not in mapping:
        raise BackendUnavailable(f"self backend does not support floating binop {op!r}")
    return [mapping[op]]


def emit_cast(op: str, src_type: TypeDesc, dst_type: TypeDesc) -> list[str]:
    src9 = reg_name(src_type, 9)
    dst10 = reg_name(dst_type, 10)

    if op == "bitcast":
        if src_type.bits != dst_type.bits:
            raise BackendUnavailable(
                f"self backend bitcast requires same-size scalars, got {src_type.describe()} -> {dst_type.describe()}"
            )
        if src_type.is_fp and dst_type.is_int:
            return [f"  fmov {dst10}, {src9}"]
        if src_type.is_int and dst_type.is_fp:
            return [f"  fmov {dst10}, {src9}"]
        return [emitted_move_register_line(dst10, src9)]

    if op == "fpext":
        if not src_type.is_fp or not dst_type.is_fp:
            raise BackendUnavailable(
                f"self backend fpext mismatch: {src_type.describe()} -> {dst_type.describe()}"
            )
        return [f"  fcvt {dst10}, {src9}"]

    if op == "fptrunc":
        if not src_type.is_fp or not dst_type.is_fp:
            raise BackendUnavailable(
                f"self backend fptrunc mismatch: {src_type.describe()} -> {dst_type.describe()}"
            )
        return [f"  fcvt {dst10}, {src9}"]

    if op == "ptrtoint":
        if not src_type.is_ptr or not dst_type.is_int:
            raise BackendUnavailable(
                f"self backend ptrtoint mismatch: {src_type.describe()} -> {dst_type.describe()}"
            )
        if dst_type.width > 32:
            return [emitted_move_register_line(dst10, "x9")]
        return [emitted_move_register_line(dst10, "w9")]

    if op == "inttoptr":
        if not src_type.is_int or not dst_type.is_ptr:
            raise BackendUnavailable(
                f"self backend inttoptr mismatch: {src_type.describe()} -> {dst_type.describe()}"
            )
        if src_type.width > 32:
            return [emitted_move_register_line("x10", "x9")]
        return [emitted_move_register_line("w10", "w9")]

    if op == "trunc":
        if not src_type.is_int or not dst_type.is_int:
            raise BackendUnavailable(
                f"self backend trunc mismatch: {src_type.describe()} -> {dst_type.describe()}"
            )
        if src_type.width <= dst_type.width:
            raise BackendUnavailable(
                f"self backend trunc expects narrowing cast, got {src_type.describe()} -> {dst_type.describe()}"
            )
        if dst_type.width <= 32:
            return [emitted_move_register_line(dst10, "w9")]
        return [emitted_move_register_line(dst10, "x9")]

    if op == "zext":
        if not src_type.is_int or not dst_type.is_int:
            raise BackendUnavailable(
                f"self backend zext mismatch: {src_type.describe()} -> {dst_type.describe()}"
            )
        if src_type.width == 1:
            return ["  and w10, w9, #1"]
        if src_type.width <= 32 and dst_type.width > 32:
            return [emitted_move_register_line("w10", "w9")]
        return [emitted_move_register_line(dst10, src9)]

    if op == "sext":
        if not src_type.is_int or not dst_type.is_int:
            raise BackendUnavailable(
                f"self backend sext mismatch: {src_type.describe()} -> {dst_type.describe()}"
            )
        if src_type.width == 8:
            return ["  sxtb x10, w9" if dst_type.width > 32 else "  sxtb w10, w9"]
        if src_type.width == 16:
            return ["  sxth x10, w9" if dst_type.width > 32 else "  sxth w10, w9"]
        if src_type.width == 32 and dst_type.width > 32:
            return ["  sxtw x10, w9"]
        if src_type.width == 1:
            return (
                [
                    "  and w10, w9, #1",
                    "  neg w10, w10",
                ]
                if dst_type.width <= 32
                else [
                    "  and w10, w9, #1",
                    "  neg x10, x10",
                ]
            )
        return [emitted_move_register_line(dst10, src9)]

    if op == "sitofp":
        if not src_type.is_int or not dst_type.is_fp:
            raise BackendUnavailable(
                f"self backend sitofp mismatch: {src_type.describe()} -> {dst_type.describe()}"
            )
        if src_type.width > 32:
            return [f"  scvtf {dst10}, x9"]
        return [f"  scvtf {dst10}, w9"]

    if op == "uitofp":
        if not src_type.is_int or not dst_type.is_fp:
            raise BackendUnavailable(
                f"self backend uitofp mismatch: {src_type.describe()} -> {dst_type.describe()}"
            )
        if src_type.width > 32:
            return [f"  ucvtf {dst10}, x9"]
        return [f"  ucvtf {dst10}, w9"]

    if op == "fptosi":
        if not src_type.is_fp or not dst_type.is_int:
            raise BackendUnavailable(
                f"self backend fptosi mismatch: {src_type.describe()} -> {dst_type.describe()}"
            )
        return [f"  fcvtzs {dst10}, {src9}"]

    if op == "fptoui":
        if not src_type.is_fp or not dst_type.is_int:
            raise BackendUnavailable(
                f"self backend fptoui mismatch: {src_type.describe()} -> {dst_type.describe()}"
            )
        return [f"  fcvtzu {dst10}, {src9}"]

    raise BackendUnavailable(f"self backend does not support cast op {op!r}")


def emit_cast_indexed(
    kernel: IndexedFunctionKernel,
    op: str,
    src_type_id: int,
    dst_type_id: int,
) -> list[str]:
    src: CompilerInt4 = kernel.type_header(src_type_id)
    dst: CompilerInt4 = kernel.type_header(dst_type_id)
    src9 = reg_name_indexed(kernel, src_type_id, 9)
    dst10 = reg_name_indexed(kernel, dst_type_id, 10)
    src_layout: CompilerInt4 = kernel.type_layout(src_type_id)
    dst_layout: CompilerInt4 = kernel.type_layout(dst_type_id)
    src_bits = src_layout.third
    dst_bits = dst_layout.third

    if op == "bitcast":
        if src_bits != dst_bits:
            raise BackendUnavailable(
                f"self backend bitcast requires same-size scalar type IDs: {src_type_id} -> {dst_type_id}"
            )
        if (
            (src.first == TYPE_KIND_FP and dst.first == TYPE_KIND_INT)
            or (src.first == TYPE_KIND_INT and dst.first == TYPE_KIND_FP)
        ):
            return [f"  fmov {dst10}, {src9}"]
        return [emitted_move_register_line(dst10, src9)]
    if op == "fpext" or op == "fptrunc":
        if src.first != TYPE_KIND_FP or dst.first != TYPE_KIND_FP:
            raise BackendUnavailable(f"self backend {op} type mismatch")
        return [f"  fcvt {dst10}, {src9}"]
    if op == "ptrtoint":
        if src.first != TYPE_KIND_PTR or dst.first != TYPE_KIND_INT:
            raise BackendUnavailable("self backend ptrtoint type mismatch")
        return [
            emitted_move_register_line(
                dst10,
                "x9" if dst.second > 32 else "w9",
            )
        ]
    if op == "inttoptr":
        if src.first != TYPE_KIND_INT or dst.first != TYPE_KIND_PTR:
            raise BackendUnavailable("self backend inttoptr type mismatch")
        return [
            emitted_move_register_line(
                "x10" if src.second > 32 else "w10",
                "x9" if src.second > 32 else "w9",
            )
        ]
    if op == "trunc":
        if src.first != TYPE_KIND_INT or dst.first != TYPE_KIND_INT:
            raise BackendUnavailable("self backend trunc type mismatch")
        if src.second <= dst.second:
            raise BackendUnavailable("self backend trunc expects a narrowing cast")
        return [
            emitted_move_register_line(
                dst10,
                "w9" if dst.second <= 32 else "x9",
            )
        ]
    if op == "zext":
        if src.first != TYPE_KIND_INT or dst.first != TYPE_KIND_INT:
            raise BackendUnavailable("self backend zext type mismatch")
        if src.second == 1:
            return ["  and w10, w9, #1"]
        if src.second <= 32 and dst.second > 32:
            return [emitted_move_register_line("w10", "w9")]
        return [emitted_move_register_line(dst10, src9)]
    if op == "sext":
        if src.first != TYPE_KIND_INT or dst.first != TYPE_KIND_INT:
            raise BackendUnavailable("self backend sext type mismatch")
        if src.second == 8:
            return ["  sxtb x10, w9" if dst.second > 32 else "  sxtb w10, w9"]
        if src.second == 16:
            return ["  sxth x10, w9" if dst.second > 32 else "  sxth w10, w9"]
        if src.second == 32 and dst.second > 32:
            return ["  sxtw x10, w9"]
        if src.second == 1:
            return (
                ["  and w10, w9, #1", "  neg w10, w10"]
                if dst.second <= 32
                else ["  and w10, w9, #1", "  neg x10, x10"]
            )
        return [emitted_move_register_line(dst10, src9)]
    if op == "sitofp" or op == "uitofp":
        if src.first != TYPE_KIND_INT or dst.first != TYPE_KIND_FP:
            raise BackendUnavailable(f"self backend {op} type mismatch")
        mnemonic = "scvtf" if op == "sitofp" else "ucvtf"
        return [f"  {mnemonic} {dst10}, {'x9' if src.second > 32 else 'w9'}"]
    if op == "fptosi" or op == "fptoui":
        if src.first != TYPE_KIND_FP or dst.first != TYPE_KIND_INT:
            raise BackendUnavailable(f"self backend {op} type mismatch")
        mnemonic = "fcvtzs" if op == "fptosi" else "fcvtzu"
        return [f"  {mnemonic} {dst10}, {src9}"]
    raise BackendUnavailable(f"self backend does not support cast op {op!r}")


def sign_extend_int_reg(value_type: TypeDesc, reg: str) -> list[str]:
    if not value_type.is_int:
        raise BackendUnavailable(
            f"self backend sign-extension helper expects int type, got {value_type.describe()}"
        )
    width = value_type.width
    if width >= 32:
        return []
    if width == 1:
        return [
            f"  and {reg}, {reg}, #1",
            f"  neg {reg}, {reg}",
        ]
    if width == 8:
        return [f"  sxtb {reg}, {reg}"]
    if width == 16:
        return [f"  sxth {reg}, {reg}"]
    shift = 32 - width
    return [
        f"  lsl {reg}, {reg}, #{shift}",
        f"  asr {reg}, {reg}, #{shift}",
    ]


def sign_extend_int_reg_indexed(
    kernel: IndexedFunctionKernel,
    type_id: int,
    reg: str,
) -> list[str]:
    header: CompilerInt4 = kernel.type_header(type_id)
    if header.first != TYPE_KIND_INT:
        raise BackendUnavailable(
            f"self backend sign-extension helper expects int type_id, got {type_id}"
        )
    width = header.second
    if width >= 32:
        return []
    if width == 1:
        return [f"  and {reg}, {reg}, #1", f"  neg {reg}, {reg}"]
    if width == 8:
        return [f"  sxtb {reg}, {reg}"]
    if width == 16:
        return [f"  sxth {reg}, {reg}"]
    shift = 32 - width
    return [
        f"  lsl {reg}, {reg}, #{shift}",
        f"  asr {reg}, {reg}, #{shift}",
    ]


def aarch64_cc(cond: str) -> str:
    mapping = {
        "eq": "eq",
        "ne": "ne",
        "slt": "lt",
        "sle": "le",
        "sgt": "gt",
        "sge": "ge",
        "ult": "lo",
        "ule": "ls",
        "ugt": "hi",
        "uge": "hs",
    }
    if cond not in mapping:
        raise BackendUnavailable(f"self backend does not support icmp {cond!r}")
    return mapping[cond]


def emit_fcmp_result(cond: str) -> list[str]:
    direct = {
        "oeq": "eq",
        "ogt": "gt",
        "oge": "ge",
        "olt": "mi",
        "ole": "ls",
        "ord": "vc",
        "une": "ne",
        "ugt": "hi",
        "uge": "pl",
        "ult": "lt",
        "ule": "le",
        "uno": "vs",
    }
    if cond in direct:
        return [emitted_cset_line("w11", direct[cond])]
    if cond == "ueq":
        return [
            emitted_cset_line("w11", "eq"),
            emitted_cset_line("w12", "vs"),
            "  orr w11, w11, w12",
        ]
    if cond == "one":
        return [
            emitted_cset_line("w11", "mi"),
            emitted_cset_line("w12", "gt"),
            "  orr w11, w11, w12",
        ]
    raise BackendUnavailable(f"self backend does not support fcmp {cond!r}")
