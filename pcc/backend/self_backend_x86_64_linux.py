from __future__ import annotations

"""Asm-first self backend bootstrap for x86_64 Linux.

This is the first truthful translated slice for the Linux x86_64 target. It is
intentionally narrow and explicit:

- scalar integer / pointer args and returns
- local `alloca`, scalar `load`, scalar `store`
- integer scalar `binop`
- direct calls
- finite ELF TLS definitions/accesses via the initial-exec model
- the `pcc.unsafe.syscall6` inline-asm shape (musl x86_64 syscall ABI)
- i8/i16/i32/i64 atomics (`load atomic`, `store atomic`, `atomicrmw`
  add/sub/and/or/xchg, `cmpxchg`, `fence`) via x86-TSO `mov`/`xchg`/
  `lock xadd`/`lock cmpxchg`/`mfence`
- `ret` / `ret void`

Anything outside this slice still raises ``BackendUnavailable`` instead of
guessing.
"""

from . import BackendUnavailable
from .code_profile import apply_function_order_profile
from .precise_stackmap import precise_stackmap_tables_enabled
from .self_backend_emit import emit_function_blocks
from .self_backend_float_bits import (
    float32_to_bits,
    float64_to_bits,
)
from .wide_float import encode_float_bits
from .self_backend_instruction_dispatch import (
    emit_instruction_dispatch,
    emit_instruction_dispatch_parts,
)
from .self_backend_kernel import IndexedFunctionKernel, get_indexed_function_kernel
from .self_backend_aarch64_darwin_symbols import sanitize_label
from .self_backend_ir import (
    GlobalDef,
    PARSED_INSTRUCTION_KINDS,
    ParsedBlock,
    ParsedFunction,
    ParsedInstr,
    TypeDesc,
    _align_to,
    _interned_operands,
    aggregate_member_info,
    clear_text_key_mapping,
    reset_operand_intern,
)
from .self_backend_module_symbols import PreparedModuleSymbols
from .self_backend_parse import (
    aggregate_literal_to_bytes,
    const_int_from_value,
    is_aggregate_literal_value,
    parse_constant_gep,
)
from .self_backend_prepare import prepare_module_for_target
from .self_backend_precise_stackmaps import (
    FunctionStackMapPlan,
    build_function_stack_map_plan,
    render_x86_64_stack_map_section,
)
from .self_backend_target_passes import run_self_target_memory_pass_pipeline
from .self_backend_target_match import is_x86_64_linux_triple
from .self_backend_terminator_dispatch import emit_terminator_dispatch
from .self_backend_value_arena import CompilerInt2
from .self_backend_x86_64_linux_data import (
    emit_globals,
    validate_x86_tls_global,
    validate_x86_tls_globals,
)
from .x86_64_frame_registers import (
    CALLEE_SAVED_AREA_SIZE,
    keep_frame_slots_in_registers,
)
from .x86_64_register_copies import forward_register_copies

_WINDOWS_ABI = False
_WINDOWS_SCRATCH_BASE = 0
# Instruction-selection tests read the selector's own text with this off;
# emission always runs the register, jump and copy passes over it.
_POST_SELECTION_PASSES = True
_X86_EMISSION_ACTIVE = False

_MODULE_SYMBOLS = PreparedModuleSymbols(
    internal_prefix="",
    defined_symbols=frozenset(),
    internal_symbols=frozenset(),
    thread_local_symbols=frozenset(),
)
_VARARG_FUNCTIONS: frozenset[str] = frozenset()
_TLS_GLOBALS: dict[str, GlobalDef] = {}

def retire_x86_module_state() -> None:
    """Drop prior-module roots only after assembly AND stack-map encoding."""
    global _MODULE_SYMBOLS, _TLS_GLOBALS, _VARARG_FUNCTIONS
    if _X86_EMISSION_ACTIVE:
        raise BackendUnavailable("cannot retire active x86 emission")
    _MODULE_SYMBOLS = PreparedModuleSymbols(
        internal_prefix="", defined_symbols=frozenset(),
        internal_symbols=frozenset(), thread_local_symbols=frozenset(),
    )
    # Do not clear the former TypeParseContext or aliased dictionaries in place.
    _TLS_GLOBALS = {}
    _VARARG_FUNCTIONS = frozenset()


_ARG_REGS = ("rdi", "rsi", "rdx", "rcx", "r8", "r9")
_ARG_REGS_32 = ("edi", "esi", "edx", "ecx", "r8d", "r9d")
_ARG_REGS_16 = ("di", "si", "dx", "cx", "r8w", "r9w")
_ARG_REGS_8 = ("dil", "sil", "dl", "cl", "r8b", "r9b")
_FP_ARG_REGS = ("xmm0", "xmm1", "xmm2", "xmm3", "xmm4", "xmm5", "xmm6", "xmm7")
_RESERVED_ASM_SYMBOLS = frozenset(
    {
        "al",
        "ah",
        "ax",
        "eax",
        "rax",
        "bl",
        "bh",
        "bx",
        "ebx",
        "rbx",
        "cl",
        "ch",
        "cx",
        "ecx",
        "rcx",
        "dl",
        "dh",
        "dx",
        "edx",
        "rdx",
        "si",
        "esi",
        "rsi",
        "di",
        "edi",
        "rdi",
        "bp",
        "ebp",
        "rbp",
        "sp",
        "esp",
        "rsp",
        "fs",
        "gs",
        "eq",
        "ne",
        "lt",
        "le",
        "gt",
        "ge",
        "and",
        "or",
        "not",
        "mod",
        "shl",
        "shr",
    }
    | {f"r{i}" for i in range(8, 16)}
    | {f"r{i}d" for i in range(8, 16)}
    | {f"r{i}w" for i in range(8, 16)}
    | {f"r{i}b" for i in range(8, 16)}
    | {f"xmm{i}" for i in range(32)}
)
_GP_REG_ALIASES = {
    "rax": {1: "al", 2: "ax", 4: "eax", 8: "rax"},
    "rdx": {1: "dl", 2: "dx", 4: "edx", 8: "rdx"},
    "rdi": {1: "dil", 2: "di", 4: "edi", 8: "rdi"},
    "rsi": {1: "sil", 2: "si", 4: "esi", 8: "rsi"},
    "rcx": {1: "cl", 2: "cx", 4: "ecx", 8: "rcx"},
    "r8": {1: "r8b", 2: "r8w", 4: "r8d", 8: "r8"},
    "r9": {1: "r9b", 2: "r9w", 4: "r9d", 8: "r9"},
    "r10": {1: "r10b", 2: "r10w", 4: "r10d", 8: "r10"},
    "r11": {1: "r11b", 2: "r11w", 4: "r11d", 8: "r11"},
}


_FRAME_TRANSFER_WIDTHS = {"QWORD": 8, "DWORD": 4, "WORD": 2, "BYTE": 1}
_FRAME_TRANSFER_REG_SIZES = {
    alias: size for aliases in _GP_REG_ALIASES.values() for size, alias in aliases.items()
}


def _parse_frame_transfer(line: str):
    """``(is_store, width, offset, register)`` of a GP ``mov`` to or from
    ``[rbp - N]``, or None."""
    text = line.strip()
    if not text.startswith("mov "):
        return None
    operands = text[4:]
    comma = operands.find(", ")
    if comma < 0:
        return None
    first = operands[:comma]
    second = operands[comma + 2:]
    for is_store, memory, register in ((True, first, second), (False, second, first)):
        marker = memory.find(" PTR [rbp - ")
        if marker <= 0 or not memory.endswith("]"):
            continue
        width = _FRAME_TRANSFER_WIDTHS.get(memory[:marker], 0)
        offset = memory[marker + 12:-1]
        if width and offset.isdecimal() and _FRAME_TRANSFER_REG_SIZES.get(register, 0) == width:
            return is_store, width, int(offset), register
    return None


def _forward_adjacent_frame_store_load(lines: list[str]) -> list[str]:
    """Reuse a value just stored to its frame slot instead of reloading it.

    Every SSA value lives in a stack slot, so a definition is stored and its
    next use usually reloads the same slot on the following line. The store
    stays (the slot remains the value's home for stack maps and later uses);
    the reload becomes a register move, or disappears when the register is
    the same. Only adjacent lines qualify, so no label, call or other write
    sits between them.
    """
    out: list[str] = []
    index = 0
    count = len(lines)
    while index < count:
        line = lines[index]
        out.append(line)
        if index + 1 < count:
            store = _parse_frame_transfer(line)
            if store is not None and store[0]:
                load = _parse_frame_transfer(lines[index + 1])
                if load is not None and not load[0] and load[1] == store[1] and load[2] == store[2]:
                    if load[3] != store[3]:
                        out.append("  mov " + load[3] + ", " + store[3])
                    index += 2
                    continue
        index += 1
    return out


_SETCC_BRANCHES = {
    "sete": "je", "setne": "jne", "setl": "jl", "setle": "jle", "setg": "jg",
    "setge": "jge", "setb": "jb", "setbe": "jbe", "seta": "ja", "setae": "jae",
}
_BRANCH_INVERSES = {
    "je": "jne", "jne": "je", "jl": "jge", "jge": "jl", "jle": "jg", "jg": "jle",
    "jb": "jae", "jae": "jb", "jbe": "ja", "ja": "jbe",
}
def _flags_consumer(text: str) -> bool:
    mnemonic = text.split(" ", 1)[0]
    return mnemonic in _SETCC_BRANCHES or mnemonic in _BRANCH_INVERSES


def _immediate_compare(move: str, compare: str, consumer: str) -> str:
    """``cmp r10, IMM`` for ``mov r11, IMM; cmp r10, r11`` before a flags-only
    consumer, else ``""``. ``xor r11, r11`` is the zero immediate."""
    if not _flags_consumer(consumer):
        return ""
    for width_reg, compared in (("r11", "cmp r10, r11"), ("r11d", "cmp r10d, r11d")):
        if compare != compared:
            continue
        immediate = ""
        if move == "xor " + width_reg + ", " + width_reg:
            immediate = "0"
        elif move.startswith("mov " + width_reg + ", "):
            immediate = move[len(width_reg) + 6:]
        digits = immediate[1:] if immediate.startswith("-") else immediate
        if digits.isdecimal() and -2147483648 <= int(immediate) < 2147483648:
            return compared.split(",", 1)[0] + ", " + immediate
    return ""


def _fuse_x86_compare_branches(lines: list[str]) -> list[str]:
    """Compare against small immediates directly and branch on compare flags.

    The scalar compare loads its constant operand into r11, and an i1 that
    feeds an error edge is materialized with setcc, stored to its slot and
    tested again. ``cmp r10, IMM`` replaces the scratch move when the next
    instruction only reads flags (so r11 is not consumed, unlike cmov), and
    ``setcc al; mov [slot], al; test al, al; jne/je`` branches with the
    setcc's own condition: setcc and mov leave the flags of the compare, and
    the store stays because the slot remains the value's home.
    """
    out: list[str] = []
    index = 0
    count = len(lines)
    while index < count:
        line = lines[index]
        text = line.strip()
        if index + 2 < count and (text.startswith("mov r11") or text.startswith("xor r11")):
            fused_compare = _immediate_compare(
                text, lines[index + 1].strip(), lines[index + 2].strip()
            )
            if fused_compare:
                out.append("  " + fused_compare)
                index += 2
                continue
        if (
            index + 3 < count
            and text.startswith("set")
            and text.endswith(" al")
            and text[:-3] in _SETCC_BRANCHES
        ):
            store = lines[index + 1].strip()
            branch = lines[index + 3].strip()
            branch_mnemonic = branch.split(" ", 1)[0]
            if (
                store.startswith("mov BYTE PTR [rbp - ")
                and store.endswith("], al")
                and lines[index + 2].strip() == "test al, al"
                and branch_mnemonic in ("jne", "je")
            ):
                fused = _SETCC_BRANCHES[text[:-3]]
                if branch_mnemonic == "je":
                    fused = _BRANCH_INVERSES[fused]
                out.append(line)
                out.append(lines[index + 1])
                out.append("  " + fused + branch[len(branch_mnemonic):])
                index += 4
                continue
        out.append(line)
        index += 1
    return out


def _has_label_before_next_instruction(lines: list[str], index: int, target: str) -> bool:
    """Whether ``target`` labels the next instruction, without collecting labels."""
    label = target + ":"
    index += 1
    while index < len(lines):
        line = lines[index]
        if line:
            if line[0] == " " or not line.endswith(":"):
                return False
            if line == label:
                return True
        index += 1
    return False


def _local_jump(text: str) -> tuple[str, str]:
    """``(mnemonic, label)`` of ``jmp``/``jcc`` to a ``.L`` label, else ``("", "")``."""
    space = text.find(" ")
    if space < 0 or text[0] != "j" or not text.startswith(" .L", space):
        return "", ""
    mnemonic = text[:space]
    if mnemonic != "jmp" and mnemonic not in _BRANCH_INVERSES and mnemonic not in (
        "js", "jns", "jo", "jno", "jp", "jnp",
    ):
        return "", ""
    return mnemonic, text[space + 1:]


def _thread_x86_jumps(lines: list[str]) -> list[str]:
    """Branch past jump-only blocks and drop jumps to the next label.

    Phi edges and empty blocks become labels whose only instruction is a
    ``jmp``; every branch to one is redirected to its final target, and
    ``jcc A; jmp B; A:`` becomes ``jncc B; A:``. A ``jmp`` to a label that
    immediately follows it is dropped. Labels stay (stack maps may name a
    block), so a block no branch reaches anymore remains unreachable text.
    """
    count = len(lines)
    forward: dict[str, str] = {}
    pending: list[str] = []
    for line in lines:
        if not line:
            continue
        if line[0] != " ":
            if line.endswith(":"):
                pending.append(line[:len(line) - 1])
            else:
                pending = []
            continue
        if pending and line.startswith("  jmp "):
            mnemonic, target = _local_jump(line.strip())
            if mnemonic == "jmp":
                for label in pending:
                    if label != target:
                        forward[label] = target
        pending = []
    out: list[str] = []
    index = 0
    while index < count:
        line = lines[index]
        mnemonic, target = _local_jump(line.strip()) if line.startswith("  j") else ("", "")
        if mnemonic and mnemonic != "jmp" and mnemonic in _BRANCH_INVERSES and index + 1 < count:
            follow_mnemonic, follow_target = _local_jump(lines[index + 1].strip())
            if follow_mnemonic == "jmp" and _has_label_before_next_instruction(lines, index + 1, target):
                mnemonic = _BRANCH_INVERSES[mnemonic]
                target = follow_target
                index += 1
        if mnemonic:
            hops = 0
            while target in forward and hops < 16:
                target = forward[target]
                hops += 1
            if mnemonic == "jmp" and _has_label_before_next_instruction(lines, index, target):
                index += 1
                continue
            line = "  " + mnemonic + " " + target
        out.append(line)
        index += 1
    return out


def _asm_symbol(name: str) -> str:
    if name in _MODULE_SYMBOLS.internal_symbols:
        return f"{_MODULE_SYMBOLS.internal_prefix}{name}"
    if name in _MODULE_SYMBOLS.defined_symbols and name.lower() in _RESERVED_ASM_SYMBOLS:
        return f"__pcc_sym_{name}"
    return name


def _mem_size(type_desc: TypeDesc) -> str:
    if type_desc.is_fp:
        return "QWORD PTR" if type_desc.width > 32 else "DWORD PTR"
    if type_desc.is_ptr or type_desc.width > 32:
        return "QWORD PTR"
    if type_desc.width <= 8:
        return "BYTE PTR"
    if type_desc.width <= 16:
        return "WORD PTR"
    return "DWORD PTR"


def _slot_addr(offset: int) -> str:
    return f"[rbp - {offset}]"


def _stack_arg_addr(offset: int) -> str:
    return f"[rbp + {offset}]"


def _reg_name(type_desc: TypeDesc, index: int) -> str:
    if index not in (0, 1, 10, 11):
        raise BackendUnavailable(f"x86_64 self backend scratch register index {index} not supported")
    if type_desc.is_fp:
        return {0: "xmm0", 1: "xmm1", 10: "xmm10", 11: "xmm11"}[index]
    if type_desc.is_ptr:
        return {0: "rax", 1: "rbx", 10: "r10", 11: "r11"}[index]
    if type_desc.width <= 8:
        return {0: "al", 1: "bl", 10: "r10b", 11: "r11b"}[index]
    if type_desc.width <= 16:
        return {0: "ax", 1: "bx", 10: "r10w", 11: "r11w"}[index]
    if type_desc.width <= 32:
        return {0: "eax", 1: "ebx", 10: "r10d", 11: "r11d"}[index]
    return {0: "rax", 1: "rbx", 10: "r10", 11: "r11"}[index]


def _stack_arg_reg(type_desc: TypeDesc, index: int) -> str:
    if index >= len(_ARG_REGS):
        raise BackendUnavailable("x86_64 self backend stack args beyond six regs not translated yet")
    if type_desc.is_fp:
        raise BackendUnavailable("x86_64 self backend fp args not translated yet")
    if type_desc.is_ptr:
        return _ARG_REGS[index]
    if type_desc.width <= 8:
        return _ARG_REGS_8[index]
    if type_desc.width <= 16:
        return _ARG_REGS_16[index]
    if type_desc.width <= 32:
        return _ARG_REGS_32[index]
    return _ARG_REGS[index]


def _is_memory_aggregate_arg(type_desc: TypeDesc) -> bool:
    return (type_desc.is_array or type_desc.is_struct) and type_desc.slot_size > 16


def _aggregate_returned_indirect(ty: TypeDesc) -> bool:
    if _WINDOWS_ABI:
        from .self_backend_win64_abi import indirect
        return indirect(ty)
    if ty.is_array or ty.is_struct:
        from .self_backend_sysv_aggregates import classes
        return not bool(classes(ty))
    return False


def _aggregate_reg_chunks(type_desc: TypeDesc) -> tuple[int, ...]:
    if not (type_desc.is_array or type_desc.is_struct):
        raise BackendUnavailable(
            f"x86_64 self backend aggregate chunks requested for scalar {type_desc.describe()}"
        )
    size = type_desc.slot_size
    if size <= 0 or size > 16:
        raise BackendUnavailable(
            f"x86_64 self backend aggregate register chunks not available for {type_desc.describe()}"
        )
    chunks: list[int] = []
    offset = 0
    while offset < size:
        chunk_size = min(8, size - offset)
        chunks.append(chunk_size)
        offset += chunk_size
    return tuple(chunks)


def _append_fp_aggregate_members(
    type_desc: TypeDesc,
    base_offset: int,
    members: list[tuple[TypeDesc, int]],
) -> bool:
    if type_desc.is_fp:
        members.append((type_desc, base_offset))
        return True
    if type_desc.is_array:
        if type_desc.elem is None or type_desc.count <= 0:
            return False
        stride = _align_to(type_desc.elem.slot_size, type_desc.elem.align)
        index = 0
        while index < type_desc.count:
            if not _append_fp_aggregate_members(
                type_desc.elem,
                base_offset + index * stride,
                members,
            ):
                return False
            index += 1
        return True
    if type_desc.is_struct:
        index = 0
        while index < len(type_desc.fields):
            if not _append_fp_aggregate_members(
                type_desc.fields[index],
                base_offset + type_desc.field_offset(index),
                members,
            ):
                return False
            index += 1
        return True
    return False


def _aggregate_sse_members(type_desc: TypeDesc) -> tuple[tuple[TypeDesc, int], ...]:
    """Classify simple FP aggregates into SysV SSE eightbyte classes.

    This finite slice accepts natural-layout aggregates up to two eightbytes
    when each occupied eightbyte contains one scalar f32/f64 member.  It is
    deliberately structural (arrays/nested structs recurse) and fail-closed
    for packed or mixed-class shapes that need the full SysV merge algorithm.
    """
    if _WINDOWS_ABI:
        return ()
    if not (type_desc.is_array or type_desc.is_struct):
        return ()
    if type_desc.slot_size <= 0 or type_desc.slot_size > 16:
        return ()
    members: list[tuple[TypeDesc, int]] = []
    if not _append_fp_aggregate_members(type_desc, 0, members):
        return ()
    if not members or len(members) > 2:
        return ()
    seen_eightbytes: set[int] = set()
    for member_type, member_offset in members:
        if member_offset % member_type.align != 0:
            return ()
        eightbyte = member_offset // 8
        if eightbyte in seen_eightbytes:
            return ()
        if member_offset + member_type.slot_size > (eightbyte + 1) * 8:
            return ()
        seen_eightbytes.add(eightbyte)
    return tuple(members)


def _reg_alias(reg64: str, size: int) -> str:
    aliases = _GP_REG_ALIASES.get(reg64)
    if aliases is None or size not in aliases:
        raise BackendUnavailable(f"x86_64 self backend register alias not translated: {reg64}/{size}")
    return aliases[size]


def _reg32_alias(reg64: str) -> str:
    return _reg_alias(reg64, 4)


def _int_type_for_size(size: int) -> TypeDesc:
    return TypeDesc("int", size * 8)


def _stack_arg_storage_size(type_desc: TypeDesc) -> int:
    if _is_memory_aggregate_arg(type_desc):
        return _align_to(type_desc.slot_size, 8)
    return 8


def _vararg_stack_storage_size(type_desc: TypeDesc) -> int:
    if type_desc.is_array or type_desc.is_struct:
        return _align_to(type_desc.slot_size, 8)
    return 8


def _iter_arg_locations(
    arg_types: list[TypeDesc],
    *,
    gp_index: int = 0,
    fp_index: int = 0,
) -> tuple[list[tuple[str, object]], int, int]:
    locations: list[tuple[str, object]] = []
    stack_offset = 0
    for arg_type in arg_types:
        if arg_type.is_array or arg_type.is_struct:
            from .self_backend_sysv_aggregates import assign
            assigned, next_gp, next_fp = assign(arg_type, _ARG_REGS, _FP_ARG_REGS, gp_index, fp_index)
            if assigned:
                locations.append(("aggregate_abi_regs", assigned))
                gp_index, fp_index = next_gp, next_fp
            else:
                stack_offset = _align_to(stack_offset, max(8, min(16, arg_type.align)))
                locations.append(("stack_byval", stack_offset))
                stack_offset += _stack_arg_storage_size(arg_type)
            continue
        if arg_type.is_fp:
            if fp_index < len(_FP_ARG_REGS):
                locations.append(("reg", _FP_ARG_REGS[fp_index]))
                fp_index += 1
                continue
            locations.append(("stack_scalar", stack_offset))
            stack_offset += _stack_arg_storage_size(arg_type)
            continue
        if gp_index < len(_ARG_REGS):
            locations.append(("reg", _stack_arg_reg(arg_type, gp_index)))
            gp_index += 1
            continue
        locations.append(("stack_scalar", stack_offset))
        stack_offset += _stack_arg_storage_size(arg_type)
    return locations, stack_offset, fp_index


def _store_reg_to_slot(reg: str, offset: int, type_desc: TypeDesc) -> list[str]:
    if type_desc.is_fp:
        op = "movsd" if type_desc.width > 32 else "movss"
        return [f"  {op} {_mem_size(type_desc)} {_slot_addr(offset)}, {reg}"]
    return [f"  mov {_mem_size(type_desc)} {_slot_addr(offset)}, {reg}"]


def _load_slot_to_reg(offset: int, reg: str, type_desc: TypeDesc) -> list[str]:
    if type_desc.is_fp:
        op = "movsd" if type_desc.width > 32 else "movss"
        return [f"  {op} {reg}, {_mem_size(type_desc)} {_slot_addr(offset)}"]
    return [f"  mov {reg}, {_mem_size(type_desc)} {_slot_addr(offset)}"]


def _global_addr(name: str) -> str:
    return f"{_asm_symbol(name[1:])}[rip]"


def _global_symbol_addr(name: str) -> str:
    return f"{_asm_symbol(name)}[rip]"


def _tls_global(name: str) -> GlobalDef | None:
    symbol = name[1:] if name.startswith("@") else name
    return _TLS_GLOBALS.get(symbol)


def _materialize_global_symbol_address(name: str, reg: str) -> list[str]:
    if _WINDOWS_ABI and _tls_global(name) is not None:
        from .self_backend_win64_abi import tls_address
        return tls_address(_asm_symbol(name), reg, _WINDOWS_SCRATCH_BASE)
    global_ = _tls_global(name)
    if global_ is None:
        if _WINDOWS_ABI:
            return [f"  movabs {reg}, OFFSET FLAT:{_asm_symbol(name)}"]
        return [f"  lea {reg}, {_global_symbol_addr(name)}"]
    validate_x86_tls_global(global_)
    symbol = _asm_symbol(global_.name)
    # The finite x86 ELF contract is initial-exec.  The GOT entry contains
    # the signed variant-II offset from the current thread pointer; adding
    # %fs:0 yields this thread's instance.  This deliberately emits a
    # R_X86_64_GOTTPOFF relocation instead of a process-global PC-relative
    # address.
    return [
        f"  mov {reg}, QWORD PTR {symbol}@gottpoff[rip]",
        f"  add {reg}, QWORD PTR fs:0",
    ]


def _materialize_global_address(name: str, reg: str) -> list[str]:
    symbol = name[1:] if name.startswith("@") else name
    return _materialize_global_symbol_address(symbol, reg)


def _block_label(func_name: str, block_name: str) -> str:
    return f".L{func_name}_{block_name}"


def _edge_label(func_name: str, source_block: str, target_block: str) -> str:
    return f".L{func_name}_{source_block}_to_{target_block}"


def _value_label(func_name: str, value_name: str, suffix: str) -> str:
    # The parser preserves a leading '%' on anonymous llvmlite values such as
    # ``%.83``.  GNU as interprets a resulting label like ``.Lfn_%_83`` as an
    # invalid pseudo-op, so route all SSA-derived label text through the same
    # assembler-safe sanitizer used by the rest of the self backend.
    safe = sanitize_label(value_name.lstrip("%"))
    return f".L{func_name}_{safe}_{suffix}"


def _load_global_to_reg(name: str, reg: str, type_desc: TypeDesc) -> list[str]:
    if _WINDOWS_ABI and _tls_global(name) is None:
        from .self_backend_win64_abi import global_memory_access
        return global_memory_access(name, reg, type_desc, _WINDOWS_SCRATCH_BASE, False)
    if _tls_global(name) is not None:
        address_reg = "r11"
        lines = _materialize_global_address(name, address_reg)
        lines.extend(_load_from_address(address_reg, reg, type_desc))
        return lines
    if type_desc.is_fp:
        op = "movsd" if type_desc.width > 32 else "movss"
        return [f"  {op} {reg}, {_mem_size(type_desc)} {_global_addr(name)}"]
    return [f"  mov {reg}, {_mem_size(type_desc)} {_global_addr(name)}"]


def _store_reg_to_global(name: str, reg: str, type_desc: TypeDesc) -> list[str]:
    if _WINDOWS_ABI and _tls_global(name) is None:
        from .self_backend_win64_abi import global_memory_access
        return global_memory_access(name, reg, type_desc, _WINDOWS_SCRATCH_BASE, True)
    if _tls_global(name) is not None:
        address_reg = (
            "r10" if reg in {"r11", "r11d", "r11w", "r11b"} else "r11"
        )
        lines = _materialize_global_address(name, address_reg)
        lines.extend(_store_to_address(address_reg, reg, type_desc))
        return lines
    if type_desc.is_fp:
        op = "movsd" if type_desc.width > 32 else "movss"
        return [f"  {op} {_mem_size(type_desc)} {_global_addr(name)}, {reg}"]
    return [f"  mov {_mem_size(type_desc)} {_global_addr(name)}, {reg}"]


def _load_from_address(addr_reg: str, reg: str, type_desc: TypeDesc) -> list[str]:
    if type_desc.is_fp:
        op = "movsd" if type_desc.width > 32 else "movss"
        return [f"  {op} {reg}, {_mem_size(type_desc)} [{addr_reg}]"]
    return [f"  mov {reg}, {_mem_size(type_desc)} [{addr_reg}]"]


def _store_to_address(addr_reg: str, reg: str, type_desc: TypeDesc) -> list[str]:
    if type_desc.is_fp:
        op = "movsd" if type_desc.width > 32 else "movss"
        return [f"  {op} {_mem_size(type_desc)} [{addr_reg}], {reg}"]
    return [f"  mov {_mem_size(type_desc)} [{addr_reg}], {reg}"]


def _addr_with_offset(addr_reg: str, offset: int) -> str:
    if offset == 0:
        return f"[{addr_reg}]"
    return f"[{addr_reg} + {offset}]"


def _store_reg_to_address_offset(
    addr_reg: str,
    offset: int,
    reg: str,
    type_desc: TypeDesc,
) -> list[str]:
    if type_desc.is_fp:
        op = "movsd" if type_desc.width > 32 else "movss"
        return [f"  {op} {_mem_size(type_desc)} {_addr_with_offset(addr_reg, offset)}, {reg}"]
    return [f"  mov {_mem_size(type_desc)} {_addr_with_offset(addr_reg, offset)}, {reg}"]


def _load_address_offset_to_reg(
    addr_reg: str,
    offset: int,
    reg: str,
    type_desc: TypeDesc,
) -> list[str]:
    if type_desc.is_fp:
        op = "movsd" if type_desc.width > 32 else "movss"
        return [f"  {op} {reg}, {_mem_size(type_desc)} {_addr_with_offset(addr_reg, offset)}"]
    return [f"  mov {reg}, {_mem_size(type_desc)} {_addr_with_offset(addr_reg, offset)}"]


def _store_reg_to_temp(reg: str, offset: int, type_desc: TypeDesc) -> list[str]:
    if type_desc.is_fp:
        op = "movsd" if type_desc.width > 32 else "movss"
        return [f"  {op} {_mem_size(type_desc)} [rsp + {offset}], {reg}"]
    return [f"  mov {_mem_size(type_desc)} [rsp + {offset}], {reg}"]


def _load_temp_to_reg(offset: int, reg: str, type_desc: TypeDesc) -> list[str]:
    if type_desc.is_fp:
        op = "movsd" if type_desc.width > 32 else "movss"
        return [f"  {op} {reg}, {_mem_size(type_desc)} [rsp + {offset}]"]
    return [f"  mov {reg}, {_mem_size(type_desc)} [rsp + {offset}]"]


def _zero_address(addr_reg: str, size: int) -> list[str]:
    if size <= 0:
        return []
    lines = ["  xor eax, eax"]
    offset = 0
    while size - offset >= 8:
        lines.append(f"  mov QWORD PTR [{addr_reg} + {offset}], rax")
        offset += 8
    if size - offset >= 4:
        lines.append(f"  mov DWORD PTR [{addr_reg} + {offset}], eax")
        offset += 4
    if size - offset >= 2:
        lines.append(f"  mov WORD PTR [{addr_reg} + {offset}], ax")
        offset += 2
    if size - offset >= 1:
        lines.append(f"  mov BYTE PTR [{addr_reg} + {offset}], al")
        offset += 1
    return lines


def _copy_address_to_address(src_reg: str, dst_reg: str, size: int) -> list[str]:
    if size <= 0:
        return []
    lines: list[str] = []
    offset = 0
    while size - offset >= 8:
        lines.append(f"  mov rax, QWORD PTR [{src_reg} + {offset}]")
        lines.append(f"  mov QWORD PTR [{dst_reg} + {offset}], rax")
        offset += 8
    if size - offset >= 4:
        lines.append(f"  mov eax, DWORD PTR [{src_reg} + {offset}]")
        lines.append(f"  mov DWORD PTR [{dst_reg} + {offset}], eax")
        offset += 4
    if size - offset >= 2:
        lines.append(f"  mov ax, WORD PTR [{src_reg} + {offset}]")
        lines.append(f"  mov WORD PTR [{dst_reg} + {offset}], ax")
        offset += 2
    if size - offset >= 1:
        lines.append(f"  mov al, BYTE PTR [{src_reg} + {offset}]")
        lines.append(f"  mov BYTE PTR [{dst_reg} + {offset}], al")
    return lines


def _store_gp_chunk_to_address(reg64: str, addr_reg: str, offset: int, size: int) -> list[str]:
    if size in (1, 2, 4, 8):
        return _store_reg_to_address_offset(addr_reg, offset, _reg_alias(reg64, size), _int_type_for_size(size))
    if not 1 < size < 8:
        raise BackendUnavailable(f"x86_64 self backend cannot store aggregate register chunk of size {size}")
    lines = [f"  mov rax, {reg64}"]
    sub_offset = 0
    remaining = size
    if remaining >= 4:
        lines.append(f"  mov DWORD PTR {_addr_with_offset(addr_reg, offset)}, eax")
        lines.append("  shr rax, 32")
        sub_offset += 4
        remaining -= 4
    if remaining >= 2:
        lines.append(f"  mov WORD PTR {_addr_with_offset(addr_reg, offset + sub_offset)}, ax")
        lines.append("  shr rax, 16")
        sub_offset += 2
        remaining -= 2
    if remaining:
        lines.append(f"  mov BYTE PTR {_addr_with_offset(addr_reg, offset + sub_offset)}, al")
    return lines


def _load_address_chunk_to_gp_reg(addr_reg: str, offset: int, size: int, reg64: str) -> list[str]:
    if size == 8:
        return [f"  mov {reg64}, QWORD PTR {_addr_with_offset(addr_reg, offset)}"]
    if size == 4:
        return [f"  mov {_reg32_alias(reg64)}, DWORD PTR {_addr_with_offset(addr_reg, offset)}"]
    if size == 2:
        return [f"  movzx {_reg32_alias(reg64)}, WORD PTR {_addr_with_offset(addr_reg, offset)}"]
    if size == 1:
        return [f"  movzx {_reg32_alias(reg64)}, BYTE PTR {_addr_with_offset(addr_reg, offset)}"]
    if not 1 < size < 8:
        raise BackendUnavailable(f"x86_64 self backend cannot load aggregate register chunk of size {size}")
    lines = [f"  xor {_reg32_alias(reg64)}, {_reg32_alias(reg64)}"]
    sub_offset = 0
    remaining = size
    if remaining >= 4:
        lines.append(f"  mov {_reg32_alias(reg64)}, DWORD PTR {_addr_with_offset(addr_reg, offset)}")
        sub_offset += 4
        remaining -= 4
    elif remaining >= 2:
        lines.append(f"  movzx {_reg32_alias(reg64)}, WORD PTR {_addr_with_offset(addr_reg, offset)}")
        sub_offset += 2
        remaining -= 2
    if remaining >= 2:
        lines.append(f"  movzx r11d, WORD PTR {_addr_with_offset(addr_reg, offset + sub_offset)}")
        lines.append(f"  shl r11, {sub_offset * 8}")
        lines.append(f"  or {reg64}, r11")
        sub_offset += 2
        remaining -= 2
    if remaining:
        shift = sub_offset * 8
        lines.append(f"  movzx r11d, BYTE PTR {_addr_with_offset(addr_reg, offset + sub_offset)}")
        lines.append(f"  shl r11, {shift}")
        lines.append(f"  or {reg64}, r11")
    return lines


def _store_aggregate_regs_to_slot(
    type_desc: TypeDesc,
    slot_offset: int,
    regs_and_chunks: tuple[tuple[str, int], ...],
) -> list[str]:
    lines = [f"  lea r10, {_slot_addr(slot_offset)}"]
    offset = 0
    for reg64, chunk_size in regs_and_chunks:
        lines.extend(_store_gp_chunk_to_address(reg64, "r10", offset, chunk_size))
        offset += chunk_size
    if offset < type_desc.slot_size:
        lines.extend(_zero_address("r10", type_desc.slot_size - offset))
    return lines


def _store_aggregate_sse_regs_to_slot(
    slot_offset: int,
    regs_and_members: tuple[tuple[str, TypeDesc, int], ...],
) -> list[str]:
    lines: list[str] = []
    for reg, member_type, member_offset in regs_and_members:
        op = "movss" if member_type.width <= 32 else "movsd"
        member_slot_offset = slot_offset - member_offset
        lines.append(
            f"  {op} {_mem_size(member_type)} {_slot_addr(member_slot_offset)}, {reg}"
        )
    return lines


def _materialize_aggregate_to_sse_regs(
    func: ParsedFunction,
    value: str,
    type_desc: TypeDesc,
    regs_and_members: tuple[tuple[str, TypeDesc, int], ...],
) -> list[str]:
    if value in {"zeroinitializer", "poison", "undef"}:
        return [
            f"  {'xorps' if member_type.width <= 32 else 'xorpd'} {reg}, {reg}"
            for reg, member_type, _member_offset in regs_and_members
        ]
    if is_aggregate_literal_value(value):
        literal_bytes = aggregate_literal_to_bytes(type_desc, value, type_context=func.type_context)
        lines: list[str] = []
        for reg, member_type, member_offset in regs_and_members:
            member_size = member_type.slot_size
            bits = int.from_bytes(
                literal_bytes[member_offset : member_offset + member_size],
                "little",
                signed=False,
            )
            if member_size <= 4:
                lines.append(f"  mov r10d, {bits}")
                lines.append(f"  movd {reg}, r10d")
            else:
                lines.append(f"  mov r10, {bits}")
                lines.append(f"  movq {reg}, r10")
        return lines
    lines = _materialize_aggregate_value_address(func, value, type_desc, "r10")
    for reg, member_type, member_offset in regs_and_members:
        op = "movss" if member_type.width <= 32 else "movsd"
        lines.append(
            f"  {op} {reg}, {_mem_size(member_type)} {_addr_with_offset('r10', member_offset)}"
        )
    return lines


def _materialize_aggregate_to_gp_regs(
    func: ParsedFunction,
    value: str,
    type_desc: TypeDesc,
    regs_and_chunks: tuple[tuple[str, int], ...],
) -> list[str]:
    if value in {"zeroinitializer", "poison", "undef"}:
        return [f"  xor {_reg32_alias(reg64)}, {_reg32_alias(reg64)}" for reg64, _chunk in regs_and_chunks]
    if is_aggregate_literal_value(value):
        literal_bytes = aggregate_literal_to_bytes(type_desc, value, type_context=func.type_context)
        lines: list[str] = []
        offset = 0
        for reg64, chunk_size in regs_and_chunks:
            chunk_value = int.from_bytes(
                literal_bytes[offset : offset + chunk_size],
                byteorder="little",
                signed=False,
            )
            lines.append(f"  mov {reg64}, {chunk_value}")
            offset += chunk_size
        return lines
    lines = _materialize_aggregate_value_address(func, value, type_desc, "r10")
    offset = 0
    for reg64, chunk_size in regs_and_chunks:
        lines.extend(_load_address_chunk_to_gp_reg("r10", offset, chunk_size, reg64))
        offset += chunk_size
    return lines


def _store_literal_bytes_to_address(data: bytes, dst_reg: str) -> list[str]:
    lines: list[str] = []
    offset = 0
    size = len(data)
    while size - offset >= 8:
        value = int.from_bytes(data[offset : offset + 8], "little", signed=False)
        lines.append(f"  mov r10, {value}")
        lines.append(f"  mov QWORD PTR [{dst_reg} + {offset}], r10")
        offset += 8
    if size - offset >= 4:
        value = int.from_bytes(data[offset : offset + 4], "little", signed=False)
        lines.append(f"  mov r10d, {value}")
        lines.append(f"  mov DWORD PTR [{dst_reg} + {offset}], r10d")
        offset += 4
    if size - offset >= 2:
        value = int.from_bytes(data[offset : offset + 2], "little", signed=False)
        lines.append(f"  mov r10w, {value}")
        lines.append(f"  mov WORD PTR [{dst_reg} + {offset}], r10w")
        offset += 2
    if size - offset >= 1:
        value = data[offset]
        lines.append(f"  mov r10b, {value}")
        lines.append(f"  mov BYTE PTR [{dst_reg} + {offset}], r10b")
    return lines


def _fill_address_with_byte(dst_reg: str, size: int, byte_value: int) -> list[str]:
    byte_value &= 0xFF
    lines: list[str] = []
    offset = 0
    if byte_value == 0:
        lines.append("  xor eax, eax")
        while size - offset >= 8:
            lines.append(f"  mov QWORD PTR [{dst_reg} + {offset}], rax")
            offset += 8
        if size - offset >= 4:
            lines.append(f"  mov DWORD PTR [{dst_reg} + {offset}], eax")
            offset += 4
        if size - offset >= 2:
            lines.append(f"  mov WORD PTR [{dst_reg} + {offset}], ax")
            offset += 2
        if size - offset >= 1:
            lines.append(f"  mov BYTE PTR [{dst_reg} + {offset}], al")
        return lines

    word = byte_value
    word |= word << 8
    word |= word << 16
    qword = word | (word << 32)
    while size - offset >= 8:
        lines.append(f"  mov r10, {qword}")
        lines.append(f"  mov QWORD PTR [{dst_reg} + {offset}], r10")
        offset += 8
    if size - offset >= 4:
        lines.append(f"  mov r10d, {word}")
        lines.append(f"  mov DWORD PTR [{dst_reg} + {offset}], r10d")
        offset += 4
    if size - offset >= 2:
        lines.append(f"  mov r10w, {word & 0xFFFF}")
        lines.append(f"  mov WORD PTR [{dst_reg} + {offset}], r10w")
        offset += 2
    if size - offset >= 1:
        lines.append(f"  mov r10b, {byte_value}")
        lines.append(f"  mov BYTE PTR [{dst_reg} + {offset}], r10b")
    return lines


def _materialize_fp_bits(bits: int, type_desc: TypeDesc, reg: str) -> list[str]:
    """Move already decoded natural-width bits without interpreting IR text."""
    if type_desc.width <= 32:
        bits = bits & 0xFFFFFFFF
        gp_reg = "r10d" if reg == "xmm10" else "r11d"
        return [f"  mov {gp_reg}, 0x{bits:08x}", f"  movd {reg}, {gp_reg}"]
    bits = bits & ((0xFFFFFFFF << 32) | 0xFFFFFFFF)
    gp_reg = "r10" if reg == "xmm10" else "r11"
    return [f"  mov {gp_reg}, 0x{bits:016x}", f"  movq {reg}, {gp_reg}"]


def _materialize_fp_constant(value: str, type_desc: TypeDesc, reg: str) -> list[str]:
    if type_desc.width <= 32:
        # LLVM float hex tokens carry the widened binary64 representation.
        # Raw vector-lane bytes use _materialize_fp_bits directly instead.
        if value.startswith("0x"):
            bits = encode_float_bits(value, 32)
        else:
            bits = float32_to_bits(float(value))
    elif value.startswith("0x"):
        bits = int(value, 16)
    else:
        bits = float64_to_bits(float(value))
    return _materialize_fp_bits(bits, type_desc, reg)


def _materialize_value(func: ParsedFunction, value: str, type_desc: TypeDesc, reg: str) -> list[str]:
    if type_desc.is_void:
        return []
    if value == "null":
        return [f"  xor {reg}, {reg}"]
    if value in {"poison", "undef"}:
        if type_desc.is_fp:
            return _materialize_fp_constant("0.0", type_desc, reg)
        return [f"  xor {reg}, {reg}"]
    if value in func.alloca_slots and type_desc.is_ptr:
        return [f"  lea {reg}, {_slot_addr(func.alloca_slots[value].offset)}"]
    if value.startswith("inttoptrconst:"):
        if not type_desc.is_ptr:
            raise BackendUnavailable(
                f"x86_64 self backend cannot materialize inttoptr constant as non-pointer in {func.name!r}: {value}"
            )
        const_value = const_int_from_value(value.split(":", 1)[1])
        if const_value is None:
            raise BackendUnavailable(
                f"x86_64 self backend expected integer constant inside inttoptr expression in {func.name!r}: {value}"
            )
        return [f"  mov {reg}, {const_value}"]
    if _is_constant_gep_value(value):
        if not type_desc.is_ptr:
            raise BackendUnavailable(
                f"x86_64 self backend cannot use GEP constant {value!r} as non-pointer in {func.name!r}"
            )
        return _materialize_constant_gep(value, reg)
    if value.startswith("@"):
        if type_desc.is_ptr:
            return _materialize_global_address(value, reg)
        return _load_global_to_reg(value, reg, type_desc)
    if value in func.value_slots:
        slot = func.value_slots[value]
        return _load_slot_to_reg(slot.offset, reg, type_desc)
    if type_desc.is_fp:
        return _materialize_fp_constant(value, type_desc, reg)
    const_value = const_int_from_value(value)
    if const_value is None:
        raise BackendUnavailable(
            f"x86_64 self backend cannot materialize value {value!r} as {type_desc.describe()} in {func.name!r}"
        )
    return [f"  mov {reg}, {const_value}"]


def _materialize_aggregate_value_address(
    func: ParsedFunction,
    value: str,
    type_desc: TypeDesc,
    reg: str,
) -> list[str]:
    if value in func.value_slots:
        return [f"  lea {reg}, {_slot_addr(func.value_slots[value].offset)}"]
    if value.startswith("@"):
        return _materialize_global_address(value, reg)
    raise BackendUnavailable(
        f"x86_64 self backend aggregate value source not translated yet in {func.name!r}: {value}"
    )


def _materialize_pointer_storage_address(func: ParsedFunction, value: str, reg: str) -> list[str]:
    if value in func.alloca_slots:
        return [f"  lea {reg}, {_slot_addr(func.alloca_slots[value].offset)}"]
    ptr_type = func.value_types.get(value, TypeDesc("ptr", pointee=TypeDesc("void")))
    if (
        value in func.value_slots
        or value.startswith("@")
        or value.startswith("inttoptrconst:")
        or _is_constant_gep_value(value)
        or value in {"null", "poison", "undef"}
    ):
        return _materialize_value(func, value, ptr_type, reg)
    raise BackendUnavailable(
        f"x86_64 self backend pointer storage address not translated yet in {func.name!r}: {value}"
    )


def _vector_elem_type(type_desc: TypeDesc) -> TypeDesc:
    if not type_desc.is_array or type_desc.elem is None:
        raise BackendUnavailable(
            f"x86_64 self backend expected vector-as-array type, got {type_desc.describe()}"
        )
    if not (type_desc.elem.is_int or type_desc.elem.is_ptr or type_desc.elem.is_fp):
        raise BackendUnavailable(
            f"x86_64 self backend vector lane type not translated yet: {type_desc.elem.describe()}"
        )
    return type_desc.elem


def _materialize_vector_lane_to_reg(
    func: ParsedFunction,
    value: str,
    vector_type: TypeDesc,
    lane: int,
    reg: str,
) -> list[str]:
    elem_type = _vector_elem_type(vector_type)
    lane_offset = lane * elem_type.slot_size
    if value in func.value_slots:
        return _load_slot_to_reg(func.value_slots[value].offset - lane_offset, reg, elem_type)
    if value == "zeroinitializer" or value == "poison":
        return [f"  xor {reg}, {reg}"] if not elem_type.is_fp else _materialize_fp_constant("0.0", elem_type, reg)
    if is_aggregate_literal_value(value):
        data = aggregate_literal_to_bytes(vector_type, value, type_context=func.type_context)
        lane_bytes = data[lane_offset : lane_offset + elem_type.slot_size]
        if elem_type.is_fp:
            int_value = int.from_bytes(lane_bytes, "little", signed=False)
            return _materialize_fp_bits(int_value, elem_type, reg)
        int_value = int.from_bytes(lane_bytes, "little", signed=False)
        if elem_type.is_int and elem_type.width > 0 and int_value >= (1 << (elem_type.width - 1)):
            int_value -= 1 << elem_type.width
        return [f"  mov {reg}, {int_value}"]
    return _materialize_value(func, value, elem_type, reg)


def _materialize_vector_value_to_address(
    func: ParsedFunction,
    value: str,
    vector_type: TypeDesc,
    addr_reg: str,
) -> list[str]:
    elem_type = _vector_elem_type(vector_type)
    if value in func.value_slots:
        lines = [f"  lea r10, {_slot_addr(func.value_slots[value].offset)}"]
        lines.extend(_copy_address_to_address("r10", addr_reg, vector_type.slot_size))
        return lines
    if value == "zeroinitializer" or value == "poison":
        return _zero_address(addr_reg, vector_type.slot_size)
    if is_aggregate_literal_value(value):
        return _store_literal_bytes_to_address(aggregate_literal_to_bytes(vector_type, value, type_context=func.type_context), addr_reg)
    lines: list[str] = []
    lane_reg = _reg_name(elem_type, 10)
    lines.extend(_materialize_value(func, value, elem_type, lane_reg))
    for lane in range(vector_type.count):
        lines.extend(
            _store_reg_to_address_offset(
                addr_reg,
                lane * elem_type.slot_size,
                lane_reg,
                elem_type,
            )
        )
    return lines


def _emit_vararg_start(func: ParsedFunction, ap_ptr: str) -> list[str]:
    if not func.is_vararg:
        raise BackendUnavailable(f"x86_64 va_start in non-variadic function {func.name!r}")
    if _WINDOWS_ABI:
        from .self_backend_win64_abi import vararg_start
        return vararg_start(func, ap_ptr)
    from .self_backend_sysv_varargs import start
    return start(func, ap_ptr)


def _emit_va_arg(
    func: ParsedFunction,
    dest: str,
    ap_type: TypeDesc,
    ap: str,
    value_type: TypeDesc,
) -> list[str]:
    if not _WINDOWS_ABI:
        from .self_backend_sysv_varargs import argument
        return argument(func, dest, ap, value_type)
    if not ap_type.is_ptr:
        raise BackendUnavailable(
            f"x86_64 self backend va_arg expects pointer va_list storage, got {ap_type.describe()}"
        )
    lines = _materialize_pointer_storage_address(func, ap, "r11")
    lines.append("  mov r10, QWORD PTR [r11]")
    if dest in func.value_slots:
        if value_type.is_array or value_type.is_struct:
            lines.append(f"  lea r8, {_slot_addr(func.value_slots[dest].offset)}")
            if _WINDOWS_ABI and _aggregate_returned_indirect(value_type):
                lines.append("  mov rdx, QWORD PTR [r10]")
                lines.extend(_copy_address_to_address("rdx", "r8", value_type.slot_size))
            else:
                lines.extend(_copy_address_to_address("r10", "r8", value_type.slot_size))
        elif value_type.is_int or value_type.is_ptr or value_type.is_fp:
            value_reg = _reg_name(value_type, 0)
            lines.extend(_load_from_address("r10", value_reg, value_type))
            lines.extend(_store_reg_to_slot(value_reg, func.value_slots[dest].offset, value_type))
        else:
            raise BackendUnavailable(
                f"x86_64 self backend va_arg type not translated yet in {func.name!r}: {value_type.describe()}"
            )
    lines.extend(_emit_add_immediate_to_reg("r10", 8 if _WINDOWS_ABI else _vararg_stack_storage_size(value_type)))
    lines.append("  mov QWORD PTR [r11], r10")
    return lines


def _store_vararg_stack_value(func: ParsedFunction, arg_type: TypeDesc, value: str, stack_offset: int) -> list[str]:
    if arg_type.is_array or arg_type.is_struct:
        lines = [f"  lea r11, [rsp + {stack_offset}]"]
        if value in {"zeroinitializer", "poison", "undef"}:
            lines.extend(_zero_address("r11", arg_type.slot_size))
            return lines
        if is_aggregate_literal_value(value):
            lines.extend(_store_literal_bytes_to_address(aggregate_literal_to_bytes(arg_type, value, type_context=func.type_context), "r11"))
            return lines
        lines.extend(_materialize_aggregate_value_address(func, value, arg_type, "r10"))
        lines.extend(_copy_address_to_address("r10", "r11", arg_type.slot_size))
        return lines
    if not (arg_type.is_int or arg_type.is_ptr or arg_type.is_fp):
        raise BackendUnavailable(
            f"x86_64 self backend vararg stack type not translated yet in {func.name!r}: {arg_type.describe()}"
        )
    lines = _materialize_value(func, value, arg_type, _reg_name(arg_type, 10))
    lines.extend(_store_reg_to_temp(_reg_name(arg_type, 10), stack_offset, arg_type))
    return lines


def _emit_internal_vararg_call(
    func: ParsedFunction,
    dest: str | None,
    ret_type: TypeDesc,
    callee: str,
    args: tuple[tuple[TypeDesc, str], ...],
    fixed_arg_count: int,
) -> list[str]:
    fixed_args = args[:fixed_arg_count]
    vararg_args = args[fixed_arg_count:]
    hidden_sret = _aggregate_returned_indirect(ret_type)
    if hidden_sret and (dest is None or dest not in func.value_slots):
        raise BackendUnavailable(
            f"x86_64 self backend aggregate return needs a destination slot in {func.name!r}: {callee}"
        )
    fixed_locations, fixed_stack_bytes, _fp_reg_count = _iter_arg_locations(
        [arg_type for arg_type, _value in fixed_args],
        gp_index=1 if hidden_sret else 0,
    )
    vararg_offsets: list[int] = []
    stack_bytes = fixed_stack_bytes
    for arg_type, _value in vararg_args:
        stack_bytes = _align_to(stack_bytes, 8)
        vararg_offsets.append(stack_bytes)
        stack_bytes += _vararg_stack_storage_size(arg_type)
    call_stack_size = _align_to(stack_bytes, 16)
    lines: list[str] = []
    if call_stack_size:
        lines.append(f"  sub rsp, {call_stack_size}")
    if hidden_sret:
        lines.append(f"  lea rdi, {_slot_addr(func.value_slots[dest].offset)}")
    for (arg_type, value), (kind, payload) in zip(fixed_args, fixed_locations, strict=False):
        if kind == "reg":
            lines.extend(_materialize_value(func, value, arg_type, str(payload)))
            continue
        if kind == "aggregate_regs":
            lines.extend(_materialize_aggregate_to_gp_regs(func, value, arg_type, payload))
            continue
        if kind == "aggregate_sse_regs":
            lines.extend(_materialize_aggregate_to_sse_regs(func, value, arg_type, payload))
            continue
        if kind == "stack_scalar":
            stack_offset = int(payload)
            lines.extend(_materialize_value(func, value, arg_type, _reg_name(arg_type, 10)))
            lines.extend(_store_reg_to_temp(_reg_name(arg_type, 10), stack_offset, arg_type))
            continue
        if kind == "stack_byval":
            stack_offset = int(payload)
            lines.append(f"  lea r11, [rsp + {stack_offset}]")
            lines.extend(_materialize_aggregate_value_address(func, value, arg_type, "r10"))
            lines.extend(_copy_address_to_address("r10", "r11", arg_type.slot_size))
            continue
        raise BackendUnavailable(
            f"x86_64 self backend internal vararg fixed arg kind not translated yet in {func.name!r}: {kind}"
        )
    for (arg_type, value), stack_offset in zip(vararg_args, vararg_offsets, strict=False):
        lines.extend(_store_vararg_stack_value(func, arg_type, value, stack_offset))
    lines.append(f"  call {_asm_symbol(callee)}")
    if call_stack_size:
        lines.append(f"  add rsp, {call_stack_size}")
    if dest is None or ret_type.is_void or dest not in func.value_slots:
        return lines
    if ret_type.is_array or ret_type.is_struct:
        if hidden_sret:
            return lines
        sse_members = _aggregate_sse_members(ret_type)
        if sse_members:
            lines.extend(
                _store_aggregate_sse_regs_to_slot(
                    func.value_slots[dest].offset,
                    tuple(
                        (f"xmm{index}", member_type, member_offset)
                        for index, (member_type, member_offset) in enumerate(sse_members)
                    ),
                )
            )
            return lines
        lines.extend(
            _store_aggregate_regs_to_slot(
                ret_type,
                func.value_slots[dest].offset,
                tuple(zip(("rax", "rdx"), _aggregate_reg_chunks(ret_type), strict=False)),
            )
        )
        return lines
    lines.extend(_store_reg_to_slot(_reg_name(ret_type, 0), func.value_slots[dest].offset, ret_type))
    return lines


def _emit_vector_binop(
    func: ParsedFunction,
    op: str,
    dest: str,
    vector_type: TypeDesc,
    lhs: str,
    rhs: str,
) -> list[str]:
    if dest not in func.value_slots:
        return []
    elem_type = _vector_elem_type(vector_type)
    if not elem_type.is_int:
        raise BackendUnavailable(
            f"x86_64 self backend vector binop lane type not translated yet in {func.name!r}: "
            f"{elem_type.describe()}"
        )
    lines = [f"  lea rax, {_slot_addr(func.value_slots[dest].offset)}"]
    for lane in range(vector_type.count):
        lhs_reg = _reg_name(elem_type, 10)
        rhs_reg = _reg_name(elem_type, 11)
        lines.extend(_materialize_vector_lane_to_reg(func, lhs, vector_type, lane, lhs_reg))
        lines.extend(_materialize_vector_lane_to_reg(func, rhs, vector_type, lane, rhs_reg))
        if op == "add":
            lines.append(f"  add {lhs_reg}, {rhs_reg}")
        elif op == "sub":
            lines.append(f"  sub {lhs_reg}, {rhs_reg}")
        elif op == "mul":
            lines.append(f"  imul {lhs_reg}, {rhs_reg}")
        elif op == "and":
            lines.append(f"  and {lhs_reg}, {rhs_reg}")
        elif op == "or":
            lines.append(f"  or {lhs_reg}, {rhs_reg}")
        elif op == "xor":
            lines.append(f"  xor {lhs_reg}, {rhs_reg}")
        else:
            raise BackendUnavailable(
                f"x86_64 self backend vector binop {op!r} not translated yet in {func.name!r}"
            )
        lines.extend(
            _store_reg_to_address_offset(
                "rax",
                lane * elem_type.slot_size,
                lhs_reg,
                elem_type,
            )
        )
    return lines


def _emit_memcpy_intrinsic_call(
    func: ParsedFunction,
    args: tuple[tuple[TypeDesc, str], ...],
) -> list[str]:
    if len(args) < 4:
        raise BackendUnavailable(
            f"x86_64 self backend memcpy intrinsic expects at least 4 args in {func.name!r}"
        )
    dst_type, dst_value = args[0]
    src_type, src_value = args[1]
    size_type, size_value = args[2]
    _isvolatile_type, _isvolatile_value = args[3]
    if not (dst_type.is_ptr and src_type.is_ptr and size_type.is_int):
        raise BackendUnavailable(
            f"x86_64 self backend memcpy intrinsic arg types not translated yet in {func.name!r}"
        )
    size = const_int_from_value(size_value)
    if size is None:
        raise BackendUnavailable(
            f"x86_64 self backend memcpy intrinsic only supports constant sizes right now in {func.name!r}"
        )
    lines = _materialize_value(func, src_value, src_type, "r10")
    lines.extend(_materialize_value(func, dst_value, dst_type, "r11"))
    lines.extend(_copy_address_to_address("r10", "r11", size))
    return lines


def _emit_memset_intrinsic_call(
    func: ParsedFunction,
    args: tuple[tuple[TypeDesc, str], ...],
) -> list[str]:
    if len(args) < 4:
        raise BackendUnavailable(
            f"x86_64 self backend memset intrinsic expects at least 4 args in {func.name!r}"
        )
    dst_type, dst_value = args[0]
    value_type, value = args[1]
    size_type, size_value = args[2]
    _isvolatile_type, _isvolatile_value = args[3]
    if not (dst_type.is_ptr and value_type.is_int and size_type.is_int):
        raise BackendUnavailable(
            f"x86_64 self backend memset intrinsic arg types not translated yet in {func.name!r}"
        )
    size = const_int_from_value(size_value)
    byte_value = const_int_from_value(value)
    if size is None or byte_value is None:
        raise BackendUnavailable(
            f"x86_64 self backend memset intrinsic only supports constant size/value right now in {func.name!r}"
        )
    lines = _materialize_value(func, dst_value, dst_type, "r11")
    lines.extend(_fill_address_with_byte("r11", size, byte_value))
    return lines


def _emit_floor_intrinsic_call(
    func: ParsedFunction,
    dest: str | None,
    ret_type: TypeDesc,
    callee: str,
    args: tuple[tuple[TypeDesc, str], ...],
) -> list[str]:
    if dest is None or dest not in func.value_slots:
        return []
    if len(args) != 1:
        raise BackendUnavailable(
            f"x86_64 self backend {callee} expects 1 arg in {func.name!r}"
        )
    value_type, value = args[0]
    if (
        value_type.describe() != ret_type.describe()
        or not value_type.is_fp
        or value_type.width not in (32, 64)
    ):
        raise BackendUnavailable(
            f"x86_64 self backend {callee} currently only supports "
            f"float/double in {func.name!r}"
        )
    lines = _materialize_value(func, value, value_type, "xmm0")
    lines.append("  call floorf" if value_type.width <= 32 else "  call floor")
    lines.extend(_store_reg_to_slot("xmm0", func.value_slots[dest].offset, ret_type))
    return lines


def _emit_sqrt_intrinsic_call(
    func: ParsedFunction,
    dest: str | None,
    ret_type: TypeDesc,
    callee: str,
    args: tuple[tuple[TypeDesc, str], ...],
) -> list[str]:
    if dest is None or dest not in func.value_slots:
        return []
    if len(args) != 1:
        raise BackendUnavailable(
            f"x86_64 self backend {callee} expects 1 arg in {func.name!r}"
        )
    value_type, value = args[0]
    if (
        value_type.describe() != ret_type.describe()
        or not value_type.is_fp
        or value_type.width not in (32, 64)
    ):
        raise BackendUnavailable(
            f"x86_64 self backend {callee} currently only supports "
            f"float/double in {func.name!r}"
        )
    lines = _materialize_value(func, value, value_type, "xmm10")
    op = "sqrtss" if value_type.width <= 32 else "sqrtsd"
    lines.append(f"  {op} xmm10, xmm10")
    lines.extend(_store_reg_to_slot("xmm10", func.value_slots[dest].offset, ret_type))
    return lines


def _emit_smul_overflow_intrinsic_call(
    func: ParsedFunction,
    dest: str | None,
    ret_type: TypeDesc,
    callee: str,
    args: tuple[tuple[TypeDesc, str], ...],
) -> list[str]:
    if dest is None or dest not in func.value_slots:
        return []
    if len(args) != 2:
        raise BackendUnavailable(
            f"x86_64 self backend {callee} intrinsic expects 2 args in {func.name!r}"
        )
    lhs_type, lhs = args[0]
    rhs_type, rhs = args[1]
    if (
        lhs_type.describe() != rhs_type.describe()
        or not lhs_type.is_int
        or lhs_type.width not in (32, 64)
    ):
        raise BackendUnavailable(
            f"x86_64 self backend {callee} intrinsic currently expects i32/i64 "
            f"same-width integer args in {func.name!r}"
        )
    if not ret_type.is_struct or len(ret_type.fields) != 2:
        raise BackendUnavailable(
            f"x86_64 self backend {callee} intrinsic expected "
            f"{{ value, overflow }} return in {func.name!r}"
        )
    value_type, overflow_type = ret_type.fields
    if (
        value_type.describe() != lhs_type.describe()
        or not overflow_type.is_int
        or overflow_type.width != 1
    ):
        raise BackendUnavailable(
            f"x86_64 self backend {callee} intrinsic return shape mismatch "
            f"in {func.name!r}: {ret_type.describe()}"
        )
    slot_offset = func.value_slots[dest].offset
    lhs_reg = _reg_name(lhs_type, 10)
    rhs_reg = _reg_name(rhs_type, 11)
    lines = _materialize_value(func, lhs, lhs_type, lhs_reg)
    lines.extend(_materialize_value(func, rhs, rhs_type, rhs_reg))
    # Match the instruction width to the intrinsic: imul sets OF when the
    # mathematical product cannot be represented in that signed width.
    lines.append(f"  imul {lhs_reg}, {rhs_reg}")
    lines.append("  seto r11b")
    lines.extend(_store_reg_to_slot(lhs_reg, slot_offset, value_type))
    lines.extend(_store_reg_to_slot(
        "r11b", slot_offset - ret_type.field_offset(1), overflow_type,
    ))
    return lines


def _validate_integer_register_type(type_desc: TypeDesc) -> None:
    if not type_desc.is_int or not 1 <= type_desc.width <= 64:
        raise BackendUnavailable(
            "x86_64 self backend integer register cast requires i1..i64, "
            f"got {type_desc.describe()}"
        )


def _truncate_reg_to_r10(dst_type: TypeDesc) -> list[str]:
    """Keep the declared IR bits, not the rounded x86 storage lane."""
    _validate_integer_register_type(dst_type)
    width = dst_type.width
    if width in (8, 16, 32, 64):
        return []
    if width < 32:
        return [f"  and {_reg_name(dst_type, 10)}, {(1 << width) - 1}"]
    shift = 64 - width
    # x86 AND r64, imm32 sign-extends its immediate. A shift pair handles
    # every 33..63-bit mask without a second scratch register or large imm.
    return [f"  shl r10, {shift}", f"  shr r10, {shift}"]


def _zero_extend_reg_to_r10(src_type: TypeDesc) -> list[str]:
    lines = _truncate_reg_to_r10(src_type)
    if src_type.width <= 8:
        lines.append("  movzx r10d, r10b")
    elif src_type.width <= 16:
        lines.append("  movzx r10d, r10w")
    elif src_type.width == 32:
        lines.append("  mov r10d, r10d")
    return lines


def _sign_extend_reg_to_r10(src_type: TypeDesc) -> list[str]:
    _validate_integer_register_type(src_type)
    if src_type.width == 8:
        return ["  movsx r10, r10b"]
    if src_type.width == 16:
        return ["  movsx r10, r10w"]
    if src_type.width == 32:
        return ["  movsxd r10, r10d"]
    if src_type.width < 64:
        shift = 64 - src_type.width
        return [f"  shl r10, {shift}", f"  sar r10, {shift}"]
    return []


def _materialize_index_to_r10(func: ParsedFunction, index_value: str) -> list[str]:
    const_index = const_int_from_value(index_value)
    if const_index is not None:
        return [f"  mov r10, {const_index}"]
    if index_value.startswith("@"):
        raise BackendUnavailable("x86_64 self backend does not support symbol-valued getelementptr indices")
    index_type = func.value_types.get(index_value)
    if index_type is None:
        raise BackendUnavailable(f"x86_64 self backend does not know getelementptr index value {index_value!r}")
    if not index_type.is_int:
        raise BackendUnavailable(
            f"x86_64 self backend getelementptr index type not translated yet in {func.name!r}: {index_type.describe()}"
        )
    lines = _materialize_value(func, index_value, index_type, _reg_name(index_type, 10))
    if index_type.width < 64:
        lines.extend(_sign_extend_reg_to_r10(index_type))
    return lines


def _emit_add_immediate_to_r11(offset: int) -> list[str]:
    return _emit_add_immediate_to_reg("r11", offset)


def _emit_add_immediate_to_reg(reg: str, offset: int) -> list[str]:
    if offset == 0:
        return []
    if offset > 0:
        return [f"  lea {reg}, [{reg} + {offset}]"]
    return [f"  lea {reg}, [{reg} - {-offset}]"]


def _materialize_constant_gep(value: str, reg: str) -> list[str]:
    if value.startswith("gep0:"):
        return _materialize_global_symbol_address(value.split(":", 1)[1], reg)
    if value.startswith("gepconst:"):
        _tag, base, offset_text = value.split(":", 2)
        lines = _materialize_global_symbol_address(base, reg)
        lines.extend(_emit_add_immediate_to_reg(reg, int(offset_text)))
        return lines
    if value.startswith("getelementptr"):
        base, offset = parse_constant_gep(value, type_context=_MODULE_SYMBOLS.type_context)
        lines = _materialize_global_symbol_address(base, reg)
        lines.extend(_emit_add_immediate_to_reg(reg, offset))
        return lines
    raise BackendUnavailable(f"x86_64 self backend expected constant GEP value, got {value!r}")


def _is_constant_gep_value(value: str) -> bool:
    return value.startswith(("gep0:", "gepconst:", "getelementptr"))


def _emit_indexed_pointer_add(func: ParsedFunction, index_value: str, elem_size: int) -> list[str]:
    if elem_size == 0:
        const_index = const_int_from_value(index_value)
        if const_index == 0:
            return []
        raise BackendUnavailable("x86_64 self backend cannot index into zero-sized element type")
    const_index = const_int_from_value(index_value)
    if const_index is not None:
        return _emit_add_immediate_to_r11(const_index * elem_size)

    lines = _materialize_index_to_r10(func, index_value)
    if elem_size == 1:
        lines.append("  add r11, r10")
        return lines
    if elem_size in (2, 4, 8):
        lines.append(f"  lea r11, [r11 + r10*{elem_size}]")
        return lines
    lines.extend(
        [
            f"  imul r10, r10, {elem_size}",
            "  add r11, r10",
        ]
    )
    return lines


def _emit_gep_instruction(
    func: ParsedFunction,
    dest: str,
    base_type: TypeDesc,
    ptr_name: str,
    indices: tuple[tuple[TypeDesc, str], ...],
) -> list[str]:
    if dest not in func.value_slots:
        return []
    if not indices:
        raise BackendUnavailable("x86_64 self backend getelementptr requires at least one index")
    ptr_type = func.value_types.get(ptr_name, TypeDesc("ptr", pointee=base_type))
    lines = _materialize_value(func, ptr_name, ptr_type, "r11")
    current = base_type
    lines.extend(_emit_indexed_pointer_add(func, indices[0][1], current.slot_size))
    for _index_type, index_value in indices[1:]:
        if current.is_array:
            assert current.elem is not None
            current = current.elem
            lines.extend(_emit_indexed_pointer_add(func, index_value, current.slot_size))
            continue
        if current.is_struct:
            struct_type = current
            field_index = const_int_from_value(index_value)
            if field_index is None:
                raise BackendUnavailable(
                    "x86_64 self backend struct getelementptr currently requires constant field indices"
                )
            current = struct_type.field_type(field_index)
            lines.extend(_emit_add_immediate_to_r11(struct_type.field_offset(field_index)))
            continue
        else:
            raise BackendUnavailable(
                f"x86_64 self backend cannot index into scalar pointee {current.describe()} with more getelementptr indices"
            )
    dest_type = func.value_types.get(dest, TypeDesc("ptr", pointee=current))
    lines.extend(_store_reg_to_slot("r11", func.value_slots[dest].offset, dest_type))
    return lines


def _emit_prologue(func: ParsedFunction) -> list[str]:
    if _WINDOWS_ABI:
        from .self_backend_win64_abi import prologue
        return prologue(func)
    is_process_entry = _is_linux_process_entry(func)
    symbol = _asm_symbol(func.name)
    lines = ["", ".text", ".p2align 4, 0x90"]
    if func.is_global:
        lines.append(f".globl {symbol}")
    lines.append(f".type {symbol}, @function")
    lines.append(f"{symbol}:")
    if is_process_entry:
        # Linux enters _start without a return address or SysV argument
        # registers.  Preserve the kernel-provided initial stack pointer as
        # the sole pcc-Python argument, then synthesize the stack parity that
        # an ordinary call would have provided before using the normal frame.
        lines.append("  mov r11, rsp")
        lines.append("  and rsp, -16")
        lines.append("  sub rsp, 8")
    lines.append("  push rbp")
    lines.append("  mov rbp, rsp")
    total_frame = func.frame_size + func.platform_frame_extra
    if total_frame:
        lines.append(f"  sub rsp, {total_frame}")
    saved_rbx = 176 if func.is_vararg else 0
    lines.append(f"  mov QWORD PTR [rsp + {saved_rbx}], rbx")
    if func.is_vararg:
        for index, register in enumerate(_ARG_REGS):
            lines.append(f"  mov QWORD PTR [rsp + {index * 8}], {register}")
        for index in range(8):
            lines.append(f"  movdqu XMMWORD PTR [rsp + {48 + index * 16}], xmm{index}")
    gp_start = 0
    if func.hidden_sret_slot is not None:
        lines.extend(_store_reg_to_slot("rdi", func.hidden_sret_slot.offset, func.hidden_sret_slot.type))
        gp_start = 1
    arg_locations, _total_stack, _fp_reg_count = _iter_arg_locations(
        [arg.type for arg in func.args],
        gp_index=gp_start,
    )
    for arg, (kind, payload) in zip(func.args, arg_locations, strict=False):
        if arg.name not in func.value_slots:
            continue
        if is_process_entry:
            lines.extend(
                _store_reg_to_slot(
                    "r11", func.value_slots[arg.name].offset, arg.type
                )
            )
            continue
        if kind == "reg":
            lines.extend(_store_reg_to_slot(str(payload), func.value_slots[arg.name].offset, arg.type))
            continue
        if kind == "aggregate_abi_regs":
            from .self_backend_sysv_aggregates import store
            lines.extend(store(func.value_slots[arg.name].offset, payload))
            continue
        if kind == "aggregate_regs":
            lines.extend(
                _store_aggregate_regs_to_slot(
                    arg.type,
                    func.value_slots[arg.name].offset,
                    payload,
                )
            )
            continue
        if kind == "aggregate_sse_regs":
            lines.extend(
                _store_aggregate_sse_regs_to_slot(
                    func.value_slots[arg.name].offset,
                    payload,
                )
            )
            continue
        if kind == "stack_scalar":
            stack_offset = 16 + int(payload)
            scratch = _reg_name(arg.type, 10)
            lines.extend(_load_address_offset_to_reg("rbp", stack_offset, scratch, arg.type))
            lines.extend(_store_reg_to_slot(scratch, func.value_slots[arg.name].offset, arg.type))
            continue
        if kind == "stack_byval":
            stack_offset = 16 + int(payload)
            lines.append(f"  lea r10, {_stack_arg_addr(stack_offset)}")
            lines.append(f"  lea r11, {_slot_addr(func.value_slots[arg.name].offset)}")
            lines.extend(_copy_address_to_address("r10", "r11", arg.type.slot_size))
            continue
        raise BackendUnavailable(
            f"x86_64 self backend arg location kind not translated yet in {func.name!r}: {kind}"
        )
    return lines


def _is_linux_process_entry(func: ParsedFunction) -> bool:
    if func.name != "_start":
        return False
    valid = (
        func.is_global
        and func.ret_type.is_void
        and not func.is_vararg
        and len(func.args) == 1
        and func.args[0].type.is_ptr
    )
    if not valid:
        raise BackendUnavailable(
            "Linux process entry '_start' must be global void "
            "(ptr initial_stack)"
        )
    return True


def _atomic_width_check(
    func: ParsedFunction,
    kind: str,
    value_type: TypeDesc,
    widths: tuple = (8, 16, 32, 64),
    *,
    allow_pointer: bool = False,
) -> None:
    if allow_pointer and value_type.is_ptr:
        return
    if value_type.is_int and value_type.width in widths:
        return
    names = "/".join(f"i{w}" for w in widths)
    raise BackendUnavailable(
        f"x86_64 self backend {kind} only supports {names} operands in "
        f"{func.name!r}: {value_type.describe()}"
    )


def _emit_memory_instruction(func: ParsedFunction, kind: str, data: tuple) -> list[str] | None:
    if kind == "alloca":
        return []

    if kind == "store":
        value_type, value, _ptr_type, ptr_name = data
        if value_type.is_array or value_type.is_struct:
            if ptr_name in func.alloca_slots:
                lines = [f"  lea r11, {_slot_addr(func.alloca_slots[ptr_name].offset)}"]
            elif ptr_name.startswith("@"):
                lines = _materialize_global_address(ptr_name, "r11")
            elif _is_constant_gep_value(ptr_name):
                lines = _materialize_value(func, ptr_name, _ptr_type, "r11")
            elif ptr_name in func.value_slots:
                ptr_type = func.value_types.get(ptr_name)
                if ptr_type is None or not ptr_type.is_ptr:
                    raise BackendUnavailable(
                        f"x86_64 self backend aggregate store pointer source is not a pointer in {func.name!r}: {ptr_name}"
                    )
                lines = _materialize_value(func, ptr_name, ptr_type, "r11")
            else:
                raise BackendUnavailable(
                    f"x86_64 self backend aggregate store destination not translated yet in {func.name!r}: {ptr_name}"
                )
            if value == "zeroinitializer":
                lines.extend(_zero_address("r11", value_type.slot_size))
                return lines
            lines.extend(_materialize_aggregate_value_address(func, value, value_type, "r10"))
            lines.extend(_copy_address_to_address("r10", "r11", value_type.slot_size))
            return lines
        if not (value_type.is_int or value_type.is_ptr or value_type.is_fp):
            raise BackendUnavailable(
                f"x86_64 self backend scalar store type not translated yet in {func.name!r}: {value_type.describe()}"
            )
        if ptr_name in func.alloca_slots:
            lines = _materialize_value(func, value, value_type, _reg_name(value_type, 10))
            lines.extend(_store_reg_to_slot(_reg_name(value_type, 10), func.alloca_slots[ptr_name].offset, value_type))
            return lines
        if ptr_name.startswith("@"):
            lines = _materialize_value(func, value, value_type, _reg_name(value_type, 10))
            lines.extend(_store_reg_to_global(ptr_name, _reg_name(value_type, 10), value_type))
            return lines
        if _is_constant_gep_value(ptr_name):
            lines = _materialize_value(func, value, value_type, _reg_name(value_type, 10))
            lines.extend(_materialize_value(func, ptr_name, _ptr_type, "r11"))
            lines.extend(_store_to_address("r11", _reg_name(value_type, 10), value_type))
            return lines
        if ptr_name in func.value_slots:
            ptr_type = func.value_types.get(ptr_name)
            if ptr_type is None or not ptr_type.is_ptr:
                raise BackendUnavailable(
                    f"x86_64 self backend store pointer source is not a pointer in {func.name!r}: {ptr_name}"
                )
            lines = _materialize_value(func, value, value_type, _reg_name(value_type, 10))
            lines.extend(_materialize_value(func, ptr_name, ptr_type, "r11"))
            lines.extend(_store_to_address("r11", _reg_name(value_type, 10), value_type))
            return lines
        raise BackendUnavailable(
            f"x86_64 self backend only supports scalar store to local alloca slots, globals, or pointer-valued SSA right now in {func.name!r}: {ptr_name}"
        )

    if kind == "load":
        dest, value_type, _ptr_type, ptr_name = data
        if dest not in func.value_slots:
            return []
        if value_type.is_array or value_type.is_struct:
            if ptr_name in func.alloca_slots:
                lines = [f"  lea r10, {_slot_addr(func.alloca_slots[ptr_name].offset)}"]
            elif ptr_name.startswith("@"):
                lines = _materialize_global_address(ptr_name, "r10")
            elif _is_constant_gep_value(ptr_name):
                lines = _materialize_value(func, ptr_name, _ptr_type, "r10")
            elif ptr_name in func.value_slots:
                ptr_type = func.value_types.get(ptr_name)
                if ptr_type is None or not ptr_type.is_ptr:
                    raise BackendUnavailable(
                        f"x86_64 self backend aggregate load pointer source is not a pointer in {func.name!r}: {ptr_name}"
                    )
                lines = _materialize_value(func, ptr_name, ptr_type, "r10")
            else:
                raise BackendUnavailable(
                    f"x86_64 self backend aggregate load source not translated yet in {func.name!r}: {ptr_name}"
                )
            lines.append(f"  lea r11, {_slot_addr(func.value_slots[dest].offset)}")
            lines.extend(_copy_address_to_address("r10", "r11", value_type.slot_size))
            return lines
        if not (value_type.is_int or value_type.is_ptr or value_type.is_fp):
            raise BackendUnavailable(
                f"x86_64 self backend scalar load type not translated yet in {func.name!r}: {value_type.describe()}"
            )
        if ptr_name in func.alloca_slots:
            lines = _load_slot_to_reg(func.alloca_slots[ptr_name].offset, _reg_name(value_type, 10), value_type)
        elif ptr_name.startswith("@"):
            lines = _load_global_to_reg(ptr_name, _reg_name(value_type, 10), value_type)
        elif _is_constant_gep_value(ptr_name):
            lines = _materialize_value(func, ptr_name, _ptr_type, "r11")
            lines.extend(_load_from_address("r11", _reg_name(value_type, 10), value_type))
        elif ptr_name in func.value_slots:
            ptr_type = func.value_types.get(ptr_name)
            if ptr_type is None or not ptr_type.is_ptr:
                raise BackendUnavailable(
                    f"x86_64 self backend load pointer source is not a pointer in {func.name!r}: {ptr_name}"
                )
            lines = _materialize_value(func, ptr_name, ptr_type, "r11")
            lines.extend(_load_from_address("r11", _reg_name(value_type, 10), value_type))
        else:
            raise BackendUnavailable(
                f"x86_64 self backend only supports scalar load from local alloca slots, globals, or pointer-valued SSA right now in {func.name!r}: {ptr_name}"
            )
        lines.extend(_store_reg_to_slot(_reg_name(value_type, 10), func.value_slots[dest].offset, value_type))
        return lines

    if kind == "load_atomic":
        dest, value_type, ptr_type, ptr_name, _ordering = data[:5]
        _atomic_width_check(func, kind, value_type)
        if dest not in func.value_slots:
            return []
        # x86-TSO: every aligned mov load is already an acquire load; a
        # seq_cst load needs no fence because seq_cst stores lower to
        # the implicitly locked xchg below.
        lines = _materialize_value(func, ptr_name, ptr_type, "r11")
        lines.extend(_load_from_address("r11", _reg_name(value_type, 10), value_type))
        lines.extend(
            _store_reg_to_slot(
                _reg_name(value_type, 10), func.value_slots[dest].offset, value_type
            )
        )
        return lines

    if kind == "store_atomic":
        value_type, value, ptr_type, ptr_name, ordering = data[:5]
        _atomic_width_check(func, kind, value_type)
        val_reg = _reg_name(value_type, 10)
        lines = _materialize_value(func, value, value_type, val_reg)
        lines.extend(_materialize_value(func, ptr_name, ptr_type, "r11"))
        if ordering == "seq_cst":
            # xchg is implicitly locked: a store plus a full barrier,
            # which is clang's own seq_cst store lowering on x86.
            lines.append(f"  xchg {_mem_size(value_type)} [r11], {val_reg}")
        else:
            lines.extend(_store_to_address("r11", val_reg, value_type))
        return lines

    if kind == "atomicrmw":
        dest, op, ptr_type, ptr_name, value_type, value, _ordering = data
        _atomic_width_check(func, kind, value_type, allow_pointer=op == "xchg")
        val_reg = _reg_name(value_type, 10)
        acc_reg = _reg_name(value_type, 0)
        mem = f"{_mem_size(value_type)} [r11]"
        lines = _materialize_value(func, ptr_name, ptr_type, "r11")
        if op in ("add", "sub"):
            lines.extend(_materialize_value(func, value, value_type, val_reg))
            if op == "sub":
                lines.append(f"  neg {val_reg}")
            lines.append(f"  lock xadd {mem}, {val_reg}")
            old_reg = val_reg
        elif op == "xchg":
            lines.extend(_materialize_value(func, value, value_type, val_reg))
            lines.append(f"  xchg {mem}, {val_reg}")
            old_reg = val_reg
        elif op in ("and", "or"):
            label = ".Lat_" + sanitize_label(func.name) + "_" + sanitize_label(dest)
            lines.append(f"  mov {acc_reg}, {mem}")
            lines.append(f"{label}:")
            # and/or are commutative, so `val OP old` computes the new
            # value with only rax/r10/r11 live; on a cmpxchg miss rax is
            # reloaded with the current memory value by the hardware.
            lines.extend(_materialize_value(func, value, value_type, val_reg))
            lines.append(f"  {op} {val_reg}, {acc_reg}")
            lines.append(f"  lock cmpxchg {mem}, {val_reg}")
            lines.append(f"  jne {label}")
            old_reg = acc_reg
        else:
            raise BackendUnavailable(
                f"x86_64 self backend atomicrmw op not translated yet in {func.name!r}: {op}"
            )
        if dest in func.value_slots:
            lines.extend(
                _store_reg_to_slot(old_reg, func.value_slots[dest].offset, value_type)
            )
        return lines

    if kind == "cmpxchg":
        (
            dest,
            pair_type,
            ptr_type,
            ptr_name,
            value_type,
            expected,
            desired,
            _success,
            _failure,
        ) = data
        _atomic_width_check(func, kind, value_type)
        acc_reg = _reg_name(value_type, 0)
        des_reg = _reg_name(value_type, 10)
        lines = _materialize_value(func, ptr_name, ptr_type, "r11")
        lines.extend(_materialize_value(func, expected, value_type, acc_reg))
        lines.extend(_materialize_value(func, desired, value_type, des_reg))
        lines.append(f"  lock cmpxchg {_mem_size(value_type)} [r11], {des_reg}")
        lines.append("  sete r10b")
        if dest in func.value_slots:
            slot = func.value_slots[dest]
            lines.extend(_store_reg_to_slot(acc_reg, slot.offset, value_type))
            _flag_type, flag_offset = aggregate_member_info(pair_type, (1,))
            lines.append(
                f"  mov BYTE PTR {_slot_addr(slot.offset - flag_offset)}, r10b"
            )
        return lines

    if kind == "fence":
        (ordering,) = data
        if ordering == "seq_cst":
            return ["  mfence"]
        # x86-TSO already provides acquire/release/acq_rel fence
        # semantics, and this stack-slot backend performs no
        # compile-time reordering either.
        return []

    if kind == "syscall6":
        if _WINDOWS_ABI:
            raise BackendUnavailable("Windows uses named system DLL ABIs, not raw Linux syscalls")
        # musl arch/x86_64/syscall_arch.h ABI: rax=nr, args in
        # rdi/rsi/rdx/r10/r8/r9; the kernel clobbers rcx/r11 and rax
        # carries the raw return. Every value lives in a stack slot, so
        # the clobbers cannot corrupt live state.
        dest, arg_values = data
        i64_type = TypeDesc("int", 64)
        lines = []
        for value, reg in zip(
            arg_values, ("rax", "rdi", "rsi", "rdx", "r10", "r8", "r9")
        ):
            lines.extend(_materialize_value(func, value, i64_type, reg))
        lines.append("  syscall")
        if dest in func.value_slots:
            lines.extend(
                _store_reg_to_slot("rax", func.value_slots[dest].offset, i64_type)
            )
        return lines

    return None


def _emit_compute_instruction(func: ParsedFunction, kind: str, data: tuple) -> list[str] | None:
    if kind == "binop":
        op, dest, value_type, lhs, rhs = data
        if dest not in func.value_slots:
            return []
        if value_type.is_array:
            return _emit_vector_binop(func, op, dest, value_type, lhs, rhs)
        if not value_type.is_int:
            raise BackendUnavailable(
                f"x86_64 self backend binop type not translated yet in {func.name!r}: {value_type.describe()}"
            )
        lhs_reg = _reg_name(value_type, 10)
        rhs_reg = _reg_name(value_type, 11)
        lines = _materialize_value(func, lhs, value_type, lhs_reg)
        lines.extend(_materialize_value(func, rhs, value_type, rhs_reg))
        if op == "add":
            lines.append(f"  add {lhs_reg}, {rhs_reg}")
        elif op == "sub":
            lines.append(f"  sub {lhs_reg}, {rhs_reg}")
        elif op == "mul":
            lines.append(f"  imul {lhs_reg}, {rhs_reg}")
        elif op == "and":
            lines.append(f"  and {lhs_reg}, {rhs_reg}")
        elif op == "or":
            lines.append(f"  or {lhs_reg}, {rhs_reg}")
        elif op == "xor":
            lines.append(f"  xor {lhs_reg}, {rhs_reg}")
        elif op in {"shl", "lshr", "ashr"}:
            instr = {"shl": "shl", "lshr": "shr", "ashr": "sar"}[op]
            count = const_int_from_value(rhs)
            if count is not None:
                lines.append(f"  {instr} {lhs_reg}, {count & 0xFF}")
            else:
                lines.append("  mov cl, r11b")
                lines.append(f"  {instr} {lhs_reg}, cl")
        elif op in {"sdiv", "srem", "udiv", "urem"}:
            dividend_reg = _reg_name(value_type, 0)
            divisor_reg = _reg_name(value_type, 10)
            lines = _materialize_value(func, lhs, value_type, dividend_reg)
            lines.extend(_materialize_value(func, rhs, value_type, divisor_reg))
            if value_type.width <= 32:
                if op in {"sdiv", "srem"}:
                    lines.append("  cdq")
                    lines.append(f"  idiv {divisor_reg}")
                    result_reg = "eax" if op == "sdiv" else "edx"
                else:
                    lines.append("  xor edx, edx")
                    lines.append(f"  div {divisor_reg}")
                    result_reg = "eax" if op == "udiv" else "edx"
            else:
                if op in {"sdiv", "srem"}:
                    # x86 ``idiv`` traps for INT64_MIN / -1.  The
                    # compiler-rt ``__divdi3`` / ``__moddi3`` shapes instead
                    # compute unsigned magnitudes and consequently return the
                    # two's-complement pair (INT64_MIN, 0).  Keep the self
                    # backend deterministic at that machine boundary and in
                    # parity with AArch64 ``sdiv`` + ``msub`` rather than
                    # exposing an x86-only SIGFPE.
                    overflow_label = _value_label(
                        func.name, dest, "signed_div_overflow"
                    )
                    divide_label = _value_label(func.name, dest, "signed_div")
                    done_label = _value_label(func.name, dest, "signed_div_done")
                    lines.extend(
                        [
                            "  mov r11, -9223372036854775808",
                            "  cmp rax, r11",
                            f"  jne {divide_label}",
                            "  cmp r10, -1",
                            f"  je {overflow_label}",
                            f"{divide_label}:",
                            "  cqo",
                            f"  idiv {divisor_reg}",
                            f"  jmp {done_label}",
                            f"{overflow_label}:",
                            "  xor edx, edx",
                            f"{done_label}:",
                        ]
                    )
                    result_reg = "rax" if op == "sdiv" else "rdx"
                else:
                    lines.append("  xor rdx, rdx")
                    lines.append(f"  div {divisor_reg}")
                    result_reg = "rax" if op == "udiv" else "rdx"
            lines.extend(_store_reg_to_slot(result_reg, func.value_slots[dest].offset, value_type))
            return lines
        else:
            raise BackendUnavailable(
                f"x86_64 self backend binop {op!r} not translated yet in {func.name!r}"
            )
        lines.extend(_store_reg_to_slot(lhs_reg, func.value_slots[dest].offset, value_type))
        return lines

    if kind == "fbinop":
        op, dest, value_type, lhs, rhs = data
        if dest not in func.value_slots:
            return []
        if not value_type.is_fp:
            raise BackendUnavailable(
                f"x86_64 self backend fbinop type not translated yet in {func.name!r}: {value_type.describe()}"
            )
        lhs_reg = _reg_name(value_type, 10)
        rhs_reg = _reg_name(value_type, 11)
        lines = _materialize_value(func, lhs, value_type, lhs_reg)
        lines.extend(_materialize_value(func, rhs, value_type, rhs_reg))
        if op == "fadd":
            instr = "addsd" if value_type.width > 32 else "addss"
        elif op == "fsub":
            instr = "subsd" if value_type.width > 32 else "subss"
        elif op == "fmul":
            instr = "mulsd" if value_type.width > 32 else "mulss"
        elif op == "fdiv":
            instr = "divsd" if value_type.width > 32 else "divss"
        else:
            raise BackendUnavailable(
                f"x86_64 self backend fbinop {op!r} not translated yet in {func.name!r}"
            )
        lines.append(f"  {instr} {lhs_reg}, {rhs_reg}")
        lines.extend(_store_reg_to_slot(lhs_reg, func.value_slots[dest].offset, value_type))
        return lines

    if kind == "fneg":
        dest, value_type, value = data
        if dest not in func.value_slots:
            return []
        if not value_type.is_fp:
            raise BackendUnavailable(
                f"x86_64 self backend fneg type not translated yet in {func.name!r}: {value_type.describe()}"
            )
        reg = _reg_name(value_type, 10)
        lines = _materialize_value(func, value, value_type, reg)
        if value_type.width <= 32:
            lines.extend(
                [
                    "  mov r11d, 0x80000000",
                    "  movd xmm11, r11d",
                    "  xorps xmm10, xmm11",
                ]
            )
        else:
            lines.extend(
                [
                    "  mov r11, 0x8000000000000000",
                    "  movq xmm11, r11",
                    "  xorpd xmm10, xmm11",
                ]
            )
        lines.extend(_store_reg_to_slot(reg, func.value_slots[dest].offset, value_type))
        return lines

    if kind == "icmp":
        cond, dest, value_type, lhs, rhs = data
        if dest not in func.value_slots:
            return []
        if not (value_type.is_int or value_type.is_ptr):
            raise BackendUnavailable(
                f"x86_64 self backend icmp type not translated yet in {func.name!r}: {value_type.describe()}"
            )
        lhs_reg = _reg_name(value_type, 10)
        rhs_reg = _reg_name(value_type, 11)
        lines = _materialize_value(func, lhs, value_type, lhs_reg)
        lines.extend(_materialize_value(func, rhs, value_type, rhs_reg))
        lines.append(f"  cmp {lhs_reg}, {rhs_reg}")
        if cond == "eq":
            lines.append("  sete al")
        elif cond == "ne":
            lines.append("  setne al")
        elif cond == "slt":
            lines.append("  setl al")
        elif cond == "sle":
            lines.append("  setle al")
        elif cond == "sgt":
            lines.append("  setg al")
        elif cond == "sge":
            lines.append("  setge al")
        elif cond == "ult":
            lines.append("  setb al")
        elif cond == "ule":
            lines.append("  setbe al")
        elif cond == "ugt":
            lines.append("  seta al")
        elif cond == "uge":
            lines.append("  setae al")
        else:
            raise BackendUnavailable(
                f"x86_64 self backend icmp predicate {cond!r} not translated yet in {func.name!r}"
            )
        lines.extend(_store_reg_to_slot("al", func.value_slots[dest].offset, TypeDesc("int", 1)))
        return lines

    if kind == "fcmp":
        cond, dest, value_type, lhs, rhs = data
        if dest not in func.value_slots:
            return []
        if not value_type.is_fp:
            raise BackendUnavailable(
                f"x86_64 self backend fcmp type not translated yet in {func.name!r}: {value_type.describe()}"
            )
        lhs_reg = _reg_name(value_type, 10)
        rhs_reg = _reg_name(value_type, 11)
        lines = _materialize_value(func, lhs, value_type, lhs_reg)
        lines.extend(_materialize_value(func, rhs, value_type, rhs_reg))
        cmp_op = "ucomisd" if value_type.width > 32 else "ucomiss"
        lines.append(f"  {cmp_op} {lhs_reg}, {rhs_reg}")
        if cond == "olt":
            lines.extend(["  setb al", "  setnp bl", "  and al, bl"])
        elif cond == "ole":
            lines.extend(["  setbe al", "  setnp bl", "  and al, bl"])
        elif cond == "ogt":
            lines.extend(["  seta al", "  setnp bl", "  and al, bl"])
        elif cond == "oge":
            lines.extend(["  setae al", "  setnp bl", "  and al, bl"])
        elif cond == "oeq":
            lines.extend(["  sete al", "  setnp bl", "  and al, bl"])
        elif cond == "one":
            lines.extend(["  setne al", "  setnp bl", "  and al, bl"])
        elif cond == "ult":
            lines.extend(["  setb al", "  setp bl", "  or al, bl"])
        elif cond == "ule":
            lines.extend(["  setbe al", "  setp bl", "  or al, bl"])
        elif cond == "ugt":
            lines.extend(["  seta al", "  setp bl", "  or al, bl"])
        elif cond == "uge":
            lines.extend(["  setae al", "  setp bl", "  or al, bl"])
        elif cond == "ueq":
            lines.extend(["  sete al", "  setp bl", "  or al, bl"])
        elif cond == "une":
            lines.extend(["  setne al", "  setp bl", "  or al, bl"])
        elif cond == "uno":
            lines.append("  setp al")
        elif cond == "ord":
            # Ordered: true when NEITHER operand is NaN. ucomiss/ucomisd set
            # PF on an unordered compare (a NaN present), so setnp yields 1
            # exactly when both operands are ordered.
            lines.append("  setnp al")
        else:
            raise BackendUnavailable(
                f"x86_64 self backend fcmp predicate {cond!r} not translated yet in {func.name!r}"
            )
        lines.extend(_store_reg_to_slot("al", func.value_slots[dest].offset, TypeDesc("int", 1)))
        return lines

    if kind == "cast":
        op, dest, src_type, value, dst_type = data
        if dest not in func.value_slots:
            return []
        if op in {"trunc", "zext", "sext"} and src_type.is_int and dst_type.is_int:
            _validate_integer_register_type(src_type)
            _validate_integer_register_type(dst_type)
        if op == "zext" and src_type.is_int and dst_type.is_int and src_type.width <= dst_type.width:
            if value in func.value_slots and src_type.width == 8 and dst_type.width > 8:
                lines = [f"  movzx {_reg_name(dst_type, 10)}, BYTE PTR {_slot_addr(func.value_slots[value].offset)}"]
            elif value in func.value_slots and src_type.width == 16 and dst_type.width > 16:
                lines = [f"  movzx {_reg_name(dst_type, 10)}, WORD PTR {_slot_addr(func.value_slots[value].offset)}"]
            else:
                lines = _materialize_value(func, value, src_type, _reg_name(src_type, 10))
                # Materializing i32 writes r10d, which already clears the
                # upper half; retain this common width's no-op fastpath.
                if src_type.width != 32:
                    lines.extend(_zero_extend_reg_to_r10(src_type))
            lines.extend(_store_reg_to_slot(_reg_name(dst_type, 10), func.value_slots[dest].offset, dst_type))
            return lines
        if op == "sext" and src_type.is_int and dst_type.is_int and src_type.width <= dst_type.width:
            lines = _materialize_value(func, value, src_type, _reg_name(src_type, 10))
            if src_type.width < dst_type.width:
                lines.extend(_sign_extend_reg_to_r10(src_type))
            lines.extend(_truncate_reg_to_r10(dst_type))
            lines.extend(_store_reg_to_slot(_reg_name(dst_type, 10), func.value_slots[dest].offset, dst_type))
            return lines
        if op == "trunc" and src_type.is_int and dst_type.is_int and src_type.width >= dst_type.width:
            lines = _materialize_value(func, value, src_type, _reg_name(src_type, 10))
            lines.extend(_truncate_reg_to_r10(dst_type))
            lines.extend(_store_reg_to_slot(_reg_name(dst_type, 10), func.value_slots[dest].offset, dst_type))
            return lines
        if op == "ptrtoint" and src_type.is_ptr and dst_type.is_int:
            _validate_integer_register_type(dst_type)
            lines = _materialize_value(func, value, src_type, "r10")
            dest_reg = _reg_name(dst_type, 10)
            lines.extend(_truncate_reg_to_r10(dst_type))
            lines.extend(_store_reg_to_slot(dest_reg, func.value_slots[dest].offset, dst_type))
            return lines
        if op == "inttoptr" and src_type.is_int and dst_type.is_ptr:
            lines = _materialize_value(func, value, src_type, _reg_name(src_type, 10))
            lines.extend(_zero_extend_reg_to_r10(src_type))
            lines.extend(_store_reg_to_slot("r10", func.value_slots[dest].offset, dst_type))
            return lines
        if op == "bitcast":
            if src_type.is_ptr and dst_type.is_ptr:
                lines = _materialize_value(func, value, src_type, "r10")
                lines.extend(_store_reg_to_slot("r10", func.value_slots[dest].offset, dst_type))
                return lines
            if src_type.is_int and dst_type.is_int and src_type.width == dst_type.width:
                lines = _materialize_value(func, value, src_type, _reg_name(src_type, 10))
                lines.extend(_store_reg_to_slot(_reg_name(dst_type, 10), func.value_slots[dest].offset, dst_type))
                return lines
            if (
                src_type.is_fp
                and dst_type.is_int
                and src_type.width == dst_type.width
                and src_type.width in {32, 64}
            ):
                lines = _materialize_value(func, value, src_type, "xmm10")
                move = "movd" if src_type.width == 32 else "movq"
                dst_reg = _reg_name(dst_type, 10)
                lines.append(f"  {move} {dst_reg}, xmm10")
                lines.extend(
                    _store_reg_to_slot(dst_reg, func.value_slots[dest].offset, dst_type)
                )
                return lines
            if (
                src_type.is_int
                and dst_type.is_fp
                and src_type.width == dst_type.width
                and src_type.width in {32, 64}
            ):
                src_reg = _reg_name(src_type, 10)
                lines = _materialize_value(func, value, src_type, src_reg)
                move = "movd" if src_type.width == 32 else "movq"
                lines.append(f"  {move} xmm10, {src_reg}")
                lines.extend(
                    _store_reg_to_slot("xmm10", func.value_slots[dest].offset, dst_type)
                )
                return lines
        if op == "sitofp" and src_type.is_int and dst_type.is_fp:
            src_reg = _reg_name(src_type, 10)
            dst_reg = _reg_name(dst_type, 10)
            lines = _materialize_value(func, value, src_type, src_reg)
            cvt_op = "cvtsi2ss" if dst_type.width <= 32 else "cvtsi2sd"
            lines.append(f"  {cvt_op} {dst_reg}, {src_reg}")
            lines.extend(_store_reg_to_slot(dst_reg, func.value_slots[dest].offset, dst_type))
            return lines
        if op == "uitofp" and src_type.is_int and dst_type.is_fp:
            dst_reg = _reg_name(dst_type, 10)
            cvt_op = "cvtsi2ss" if dst_type.width <= 32 else "cvtsi2sd"
            lines = _materialize_value(func, value, src_type, _reg_name(src_type, 10))
            if src_type.width < 64:
                # Zero-extend the source into the full 64-bit r10 so its
                # unsigned value is a NON-negative signed 64-bit integer; then
                # a plain signed cvtsi2s{s,d} is exact (matches clang's
                # `mov eax, edi` / `cvtsi2sd xmm, rax` lowering).
                if src_type.width <= 8:
                    lines.append("  movzx r10, r10b")
                elif src_type.width <= 16:
                    lines.append("  movzx r10, r10w")
                else:
                    # Writing r10d already zeroes the upper 32 bits of r10.
                    lines.append("  mov r10d, r10d")
                lines.append(f"  {cvt_op} {dst_reg}, r10")
            else:
                # 64-bit unsigned: values with the high bit set are negative as
                # signed, so cvtsi2s{s,d} would be wrong. Branch on sign and, in
                # the high-bit case, halve (odd-rounded) then double after the
                # conversion. Mirrors clang's u64->fp fixup and is valid for
                # both float and double results.
                fixup_label = _value_label(func.name, dest, "uitofp_fixup")
                done_label = _value_label(func.name, dest, "uitofp_done")
                add_op = "addss" if dst_type.width <= 32 else "addsd"
                lines.extend(
                    [
                        "  test r10, r10",
                        f"  js {fixup_label}",
                        f"  {cvt_op} {dst_reg}, r10",
                        f"  jmp {done_label}",
                        f"{fixup_label}:",
                        "  mov r11, r10",
                        "  shr r11, 1",
                        "  and r10, 1",
                        "  or r11, r10",
                        f"  {cvt_op} {dst_reg}, r11",
                        f"  {add_op} {dst_reg}, {dst_reg}",
                        f"{done_label}:",
                    ]
                )
            lines.extend(_store_reg_to_slot(dst_reg, func.value_slots[dest].offset, dst_type))
            return lines
        if op == "fptosi" and src_type.is_fp and dst_type.is_int:
            src_reg = _reg_name(src_type, 10)
            cvt_dst_reg = "r10" if dst_type.width > 32 else "r10d"
            lines = _materialize_value(func, value, src_type, src_reg)
            cvt_op = "cvttss2si" if src_type.width <= 32 else "cvttsd2si"
            lines.append(f"  {cvt_op} {cvt_dst_reg}, {src_reg}")
            lines.extend(_store_reg_to_slot(_reg_name(dst_type, 10), func.value_slots[dest].offset, dst_type))
            return lines
        if op == "fptoui" and src_type.is_fp and dst_type.is_int:
            src_reg = _reg_name(src_type, 10)
            cvt_op = "cvttss2si" if src_type.width <= 32 else "cvttsd2si"
            lines = _materialize_value(func, value, src_type, src_reg)
            if dst_type.width <= 32:
                # Any in-range u32 value fits in a positive signed 64-bit int,
                # so a 64-bit signed truncating convert is exact; the low
                # width bits are the unsigned result (matches clang emitting a
                # 64-bit cvttsd2si then killing the high dword).
                lines.append(f"  {cvt_op} r10, {src_reg}")
            else:
                # 64-bit unsigned: cvttsd2si saturates to INT64_MIN for values
                # >= 2^63. Subtract 2^63, convert the remainder, and OR the
                # saturated high bit back in only when the value overflowed the
                # signed range. Mirrors clang's u64 fptoui fixup.
                two63 = "9223372036854775808.0"
                lines.append(f"  {cvt_op} rax, {src_reg}")
                lines.append("  mov rbx, rax")
                lines.append("  sar rbx, 63")
                lines.extend(_materialize_fp_constant(two63, src_type, "xmm11"))
                sub_op = "subss" if src_type.width <= 32 else "subsd"
                lines.append(f"  {sub_op} {src_reg}, xmm11")
                lines.append(f"  {cvt_op} r10, {src_reg}")
                lines.append("  and r10, rbx")
                lines.append("  or r10, rax")
            lines.extend(_store_reg_to_slot(_reg_name(dst_type, 10), func.value_slots[dest].offset, dst_type))
            return lines
        if op == "fptrunc" and src_type.is_fp and dst_type.is_fp and src_type.width > dst_type.width:
            lines = _materialize_value(func, value, src_type, _reg_name(src_type, 10))
            lines.append("  cvtsd2ss xmm10, xmm10")
            lines.extend(_store_reg_to_slot(_reg_name(dst_type, 10), func.value_slots[dest].offset, dst_type))
            return lines
        if op == "fpext" and src_type.is_fp and dst_type.is_fp and src_type.width < dst_type.width:
            lines = _materialize_value(func, value, src_type, _reg_name(src_type, 10))
            lines.append("  cvtss2sd xmm10, xmm10")
            lines.extend(_store_reg_to_slot(_reg_name(dst_type, 10), func.value_slots[dest].offset, dst_type))
            return lines
        raise BackendUnavailable(
            f"x86_64 self backend cast {op!r} not translated yet in {func.name!r}: {src_type.describe()} -> {dst_type.describe()}"
        )

    if kind == "extractvalue":
        dest, aggregate_type, value, _indices, result_type, offset = data
        if dest not in func.value_slots:
            return []
        if is_aggregate_literal_value(value):
            literal_bytes = aggregate_literal_to_bytes(aggregate_type, value, type_context=func.type_context)
            field_bytes = literal_bytes[offset : offset + result_type.slot_size]
            field_value = str(int.from_bytes(field_bytes, byteorder="little", signed=False))
            lines = _materialize_value(func, field_value, result_type, _reg_name(result_type, 10))
            lines.extend(_store_reg_to_slot(_reg_name(result_type, 10), func.value_slots[dest].offset, result_type))
            return lines
        if value not in func.value_slots:
            raise BackendUnavailable(
                f"x86_64 self backend extractvalue source not translated yet in {func.name!r}: {value}"
            )
        lines = [f"  lea r11, {_slot_addr(func.value_slots[value].offset)}"]
        if offset:
            lines.extend(_emit_add_immediate_to_r11(offset))
        if result_type.is_array or result_type.is_struct:
            lines.append(f"  lea r10, {_slot_addr(func.value_slots[dest].offset)}")
            lines.extend(_copy_address_to_address("r11", "r10", result_type.slot_size))
            return lines
        lines.extend(_load_from_address("r11", _reg_name(result_type, 10), result_type))
        lines.extend(_store_reg_to_slot(_reg_name(result_type, 10), func.value_slots[dest].offset, result_type))
        return lines

    if kind == "insertvalue":
        dest, aggregate_type, aggregate_value, elem_type, elem_value, _indices, offset = data
        if dest not in func.value_slots:
            return []
        lines = [f"  lea r11, {_slot_addr(func.value_slots[dest].offset)}"]
        if aggregate_value in {"zeroinitializer", "poison", "undef"}:
            lines.extend(_zero_address("r11", aggregate_type.slot_size))
        elif is_aggregate_literal_value(aggregate_value):
            lines.extend(
                _store_literal_bytes_to_address(
                    aggregate_literal_to_bytes(aggregate_type, aggregate_value, type_context=func.type_context),
                    "r11",
                )
            )
        else:
            lines.extend(_materialize_aggregate_value_address(func, aggregate_value, aggregate_type, "r10"))
            lines.extend(_copy_address_to_address("r10", "r11", aggregate_type.slot_size))
        if offset:
            lines.extend(_emit_add_immediate_to_reg("r11", offset))
        if elem_type.is_array or elem_type.is_struct:
            if elem_value in {"zeroinitializer", "poison", "undef"}:
                lines.extend(_zero_address("r11", elem_type.slot_size))
                return lines
            if is_aggregate_literal_value(elem_value):
                lines.extend(
                    _store_literal_bytes_to_address(
                        aggregate_literal_to_bytes(elem_type, elem_value, type_context=func.type_context),
                        "r11",
                    )
                )
                return lines
            lines.extend(_materialize_aggregate_value_address(func, elem_value, elem_type, "r10"))
            lines.extend(_copy_address_to_address("r10", "r11", elem_type.slot_size))
            return lines
        lines.extend(_materialize_value(func, elem_value, elem_type, _reg_name(elem_type, 10)))
        lines.extend(_store_to_address("r11", _reg_name(elem_type, 10), elem_type))
        return lines

    if kind == "freeze":
        dest, value_type, value = data
        if dest not in func.value_slots:
            return []
        if value_type.is_array or value_type.is_struct:
            raise BackendUnavailable(
                f"x86_64 self backend freeze aggregate result not translated yet in {func.name!r}: "
                f"{value_type.describe()}"
            )
        lines = _materialize_value(func, value, value_type, _reg_name(value_type, 10))
        lines.extend(_store_reg_to_slot(_reg_name(value_type, 10), func.value_slots[dest].offset, value_type))
        return lines

    if kind == "select":
        dest, value_type, cond, true_value, false_value = data
        if dest not in func.value_slots:
            return []
        if value_type.is_array or value_type.is_struct:
            raise BackendUnavailable(
                f"x86_64 self backend select aggregate result not translated yet in {func.name!r}: "
                f"{value_type.describe()}"
            )
        cond_type = func.value_types.get(cond, TypeDesc("int", 1))
        false_label = _value_label(func.name, dest, "select_false")
        done_label = _value_label(func.name, dest, "select_done")
        lines = _materialize_value(func, cond, cond_type, _reg_name(cond_type, 0))
        lines.extend(
            [
                "  test al, al",
                f"  je {false_label}",
            ]
        )
        lines.extend(_materialize_value(func, true_value, value_type, _reg_name(value_type, 10)))
        lines.append(f"  jmp {done_label}")
        lines.append(f"{false_label}:")
        lines.extend(_materialize_value(func, false_value, value_type, _reg_name(value_type, 10)))
        lines.append(f"{done_label}:")
        lines.extend(_store_reg_to_slot(_reg_name(value_type, 10), func.value_slots[dest].offset, value_type))
        return lines

    if kind == "insertelement":
        dest, vector_type, vector_value, elem_type, elem_value, index_value = data
        if dest not in func.value_slots:
            return []
        lane = const_int_from_value(index_value)
        if lane is None:
            raise BackendUnavailable(
                f"x86_64 self backend insertelement requires constant lane index in {func.name!r}: {index_value}"
            )
        expected_elem = _vector_elem_type(vector_type)
        if expected_elem.describe() != elem_type.describe():
            raise BackendUnavailable(
                f"x86_64 self backend insertelement lane type mismatch in {func.name!r}: "
                f"{elem_type.describe()} -> {expected_elem.describe()}"
            )
        lines = [f"  lea r8, {_slot_addr(func.value_slots[dest].offset)}"]
        lines.extend(_materialize_vector_value_to_address(func, vector_value, vector_type, "r8"))
        elem_reg = _reg_name(elem_type, 10)
        lines.extend(_materialize_value(func, elem_value, elem_type, elem_reg))
        lines.extend(_store_reg_to_address_offset("r8", lane * elem_type.slot_size, elem_reg, elem_type))
        return lines

    if kind == "shufflevector":
        dest, vector_type, lhs, rhs, _mask_type, mask_value = data
        if dest not in func.value_slots:
            return []
        elem_type = _vector_elem_type(vector_type)
        lines = [f"  lea r8, {_slot_addr(func.value_slots[dest].offset)}"]
        if mask_value == "zeroinitializer":
            for lane in range(vector_type.count):
                reg = _reg_name(elem_type, 10)
                lines.extend(_materialize_vector_lane_to_reg(func, lhs, vector_type, 0, reg))
                lines.extend(_store_reg_to_address_offset("r8", lane * elem_type.slot_size, reg, elem_type))
            return lines
        if not is_aggregate_literal_value(mask_value):
            raise BackendUnavailable(
                f"x86_64 self backend shufflevector mask not translated yet in {func.name!r}: {mask_value}"
            )
        mask_bytes = aggregate_literal_to_bytes(vector_type, mask_value, type_context=func.type_context)
        for lane in range(vector_type.count):
            mask_offset = lane * elem_type.slot_size
            source_lane = int.from_bytes(
                mask_bytes[mask_offset : mask_offset + elem_type.slot_size],
                "little",
                signed=True,
            )
            if source_lane < 0:
                lines.append("  xor r10d, r10d")
                lines.extend(_store_reg_to_address_offset("r8", lane * elem_type.slot_size, "r10d", elem_type))
                continue
            source_value = lhs
            if source_lane >= vector_type.count:
                source_value = rhs
                source_lane -= vector_type.count
            reg = _reg_name(elem_type, 10)
            lines.extend(_materialize_vector_lane_to_reg(func, source_value, vector_type, source_lane, reg))
            lines.extend(_store_reg_to_address_offset("r8", lane * elem_type.slot_size, reg, elem_type))
        return lines

    if kind == "va_arg":
        dest, ap_type, ap, value_type = data
        return _emit_va_arg(func, dest, ap_type, ap, value_type)

    if kind == "gep":
        dest, base_type, _ptr_type, ptr_name, indices = data
        return _emit_gep_instruction(func, dest, base_type, ptr_name, indices)

    if kind == "call":
        (
            dest,
            ret_type,
            callee,
            is_indirect,
            args,
            _fixed_arg_count,
            _is_vararg_call,
            _arg_alignments,
        ) = data
        if callee is None or callee == "None":
            raise BackendUnavailable(
                f"x86_64 self backend unresolved direct callee not translated yet in {func.name!r}"
            )
        # Constant-size copies/fills unroll into wide moves; the shared
        # byte loop below is only the fallback for run-time sizes.
        if (
            not is_indirect
            and callee.startswith("llvm.memcpy.")
            and len(args) >= 4
            and const_int_from_value(args[2][1]) is not None
        ):
            return _emit_memcpy_intrinsic_call(func, args)
        if (
            not is_indirect
            and callee.startswith("llvm.memset.")
            and len(args) >= 4
            and const_int_from_value(args[2][1]) is not None
            and const_int_from_value(args[1][1]) is not None
        ):
            return _emit_memset_intrinsic_call(func, args)
        if not is_indirect and callee.startswith("llvm."):
            from .self_backend_x86_intrinsics import emit as emit_extra_intrinsic
            intrinsic_lines = emit_extra_intrinsic(func, dest, ret_type, callee, args, windows=_WINDOWS_ABI)
            if intrinsic_lines is not None:
                return intrinsic_lines
        if not is_indirect and callee.startswith("llvm.floor."):
            return _emit_floor_intrinsic_call(func, dest, ret_type, callee, args)
        if not is_indirect and callee.startswith("llvm.sqrt."):
            return _emit_sqrt_intrinsic_call(func, dest, ret_type, callee, args)
        if not is_indirect and callee.startswith("llvm.smul.with.overflow."):
            return _emit_smul_overflow_intrinsic_call(
                func, dest, ret_type, callee, args
            )
        if not is_indirect and callee.startswith("llvm.trap"):
            if args:
                raise BackendUnavailable(
                    f"x86_64 self backend llvm.trap expects 0 args in {func.name!r}"
                )
            return ["  ud2"]
        if not is_indirect and callee.startswith("llvm.va_start"):
            if len(args) != 1:
                raise BackendUnavailable(f"x86_64 self backend llvm.va_start expects 1 arg in {func.name!r}")
            _arg_type, ap_ptr = args[0]
            return _emit_vararg_start(func, ap_ptr)
        if not is_indirect and callee.startswith(("llvm.va_end", "llvm.lifetime.start", "llvm.lifetime.end")):
            return []
        if not is_indirect and callee == "llvm.vector.reduce.mul.v4i32":
            if dest is None or dest not in func.value_slots:
                return []
            if len(args) != 1:
                raise BackendUnavailable(
                    f"x86_64 self backend {callee} expects 1 arg in {func.name!r}"
                )
            vector_type, vector_value = args[0]
            elem_type = _vector_elem_type(vector_type)
            if not elem_type.is_int or elem_type.width != 32 or vector_type.count != 4:
                raise BackendUnavailable(
                    f"x86_64 self backend {callee} type not translated yet in {func.name!r}: "
                    f"{vector_type.describe()}"
                )
            lines = ["  mov r10d, 1"]
            for lane in range(vector_type.count):
                lines.extend(_materialize_vector_lane_to_reg(func, vector_value, vector_type, lane, "r11d"))
                lines.append("  imul r10d, r11d")
            lines.extend(_store_reg_to_slot("r10d", func.value_slots[dest].offset, ret_type))
            return lines
        if not is_indirect and callee.startswith(("llvm.smax.", "llvm.smin.", "llvm.umax.", "llvm.umin.")):
            if dest is None or dest not in func.value_slots:
                return []
            if len(args) != 2:
                raise BackendUnavailable(
                    f"x86_64 self backend {callee} intrinsic expects 2 args in {func.name!r}"
                )
            lhs_type, lhs = args[0]
            rhs_type, rhs = args[1]
            if lhs_type.describe() != rhs_type.describe() or lhs_type.describe() != ret_type.describe():
                raise BackendUnavailable(
                    f"x86_64 self backend {callee} intrinsic type mismatch in {func.name!r}"
                )
            if not lhs_type.is_int:
                raise BackendUnavailable(
                    f"x86_64 self backend {callee} intrinsic only supports integer results right now in {func.name!r}"
                )
            dst_reg = _reg_name(ret_type, 0)
            rhs_reg = _reg_name(ret_type, 10)
            lines = _materialize_value(func, lhs, lhs_type, dst_reg)
            lines.extend(_materialize_value(func, rhs, rhs_type, rhs_reg))
            lines.append(f"  cmp {dst_reg}, {rhs_reg}")
            cmov = {
                "llvm.smax.": "cmovl",
                "llvm.smin.": "cmovg",
                "llvm.umax.": "cmovb",
                "llvm.umin.": "cmova",
            }
            for prefix, op in cmov.items():
                if callee.startswith(prefix):
                    lines.append(f"  {op} {dst_reg}, {rhs_reg}")
                    lines.extend(_store_reg_to_slot(dst_reg, func.value_slots[dest].offset, ret_type))
                    return lines
            raise BackendUnavailable(
                f"x86_64 self backend intrinsic not translated yet in {func.name!r}: {callee}"
            )
        if not is_indirect and callee == "llvm.pcc.dynamic.alloca":
            # Frame slots are addressed from RSP here, so moving RSP at run
            # time would shift every slot; refuse instead of miscompiling.
            raise BackendUnavailable(
                "x86_64 self backend does not support run-time sized stack "
                f"allocation (VLA or alloca) in {func.name!r}"
            )
        if not is_indirect and callee.startswith("llvm."):
            # Fail fast: an un-lowered LLVM intrinsic must never fall
            # through to a plain `call llvm.foo.i64` — the assembler
            # accepts the dotted symbol and the error only surfaces at
            # link time (or worse). Seen with llvm.smul.with.overflow
            # from the typed-int fast path (INT-P0-PROJ).
            raise BackendUnavailable(
                f"x86_64 self backend has no native lowering for "
                f"intrinsic {callee!r} in {func.name!r}"
            )
        if _WINDOWS_ABI:
            from .self_backend_win64_abi import call
            return call(func, dest, ret_type, callee, is_indirect, args, _is_vararg_call)
        lines: list[str] = []
        hidden_sret = _aggregate_returned_indirect(ret_type)
        if hidden_sret and (dest is None or dest not in func.value_slots):
            raise BackendUnavailable(
                f"x86_64 self backend aggregate return needs a destination slot in {func.name!r}: {callee}"
            )
        arg_locations, stack_bytes, fp_reg_count = _iter_arg_locations(
            [arg_type for arg_type, _value in args],
            gp_index=1 if hidden_sret else 0,
        )
        call_stack_size = _align_to(stack_bytes, 16)
        if call_stack_size:
            lines.append(f"  sub rsp, {call_stack_size}")
        if hidden_sret:
            lines.append(f"  lea rdi, {_slot_addr(func.value_slots[dest].offset)}")
        for (arg_type, value), (kind, payload) in zip(args, arg_locations, strict=False):
            if kind == "reg":
                lines.extend(_materialize_value(func, value, arg_type, str(payload)))
                continue
            if kind == "aggregate_abi_regs":
                from .self_backend_sysv_aggregates import load
                lines.extend(load(func, value, arg_type, payload))
                continue
            if kind == "aggregate_regs":
                lines.extend(_materialize_aggregate_to_gp_regs(func, value, arg_type, payload))
                continue
            if kind == "aggregate_sse_regs":
                lines.extend(_materialize_aggregate_to_sse_regs(func, value, arg_type, payload))
                continue
            if kind == "stack_scalar":
                stack_offset = int(payload)
                lines.extend(_materialize_value(func, value, arg_type, _reg_name(arg_type, 10)))
                lines.extend(_store_reg_to_temp(_reg_name(arg_type, 10), stack_offset, arg_type))
                continue
            if kind == "stack_byval":
                stack_offset = int(payload)
                lines.append(f"  lea r11, [rsp + {stack_offset}]")
                if value == "zeroinitializer":
                    lines.extend(_zero_address("r11", arg_type.slot_size))
                    continue
                lines.extend(_materialize_aggregate_value_address(func, value, arg_type, "r10"))
                lines.extend(_copy_address_to_address("r10", "r11", arg_type.slot_size))
                continue
            raise BackendUnavailable(
                f"x86_64 self backend call arg location kind not translated yet in {func.name!r}: {kind}"
            )
        # SysV uses %al to pass the number of vector registers consumed by a
        # variadic call; a non-variadic callee ignores %rax. The call site's
        # own function type decides (calling a variadic function through a
        # non-variadic type is undefined), so the output stays deterministic
        # across stages. Setting %al on every call was one instruction in
        # seven of an x86-64 module.
        if _is_vararg_call:
            if fp_reg_count == 0:
                lines.append("  xor eax, eax")
            else:
                lines.append(f"  mov al, {fp_reg_count}")
        if is_indirect:
            callee_type = func.value_types.get(callee, TypeDesc("ptr", pointee=ret_type))
            if not callee_type.is_ptr:
                raise BackendUnavailable(
                    f"x86_64 self backend indirect callee is not pointer-typed in {func.name!r}: {callee}"
                )
            lines.extend(_materialize_value(func, callee, callee_type, "r11"))
            lines.append("  call r11")
        else:
            lines.append(f"  call {_asm_symbol(callee) if callee in _MODULE_SYMBOLS.defined_symbols else callee}")
        if call_stack_size:
            lines.append(f"  add rsp, {call_stack_size}")
        if dest is None or ret_type.is_void or dest not in func.value_slots:
            return lines
        if ret_type.is_array or ret_type.is_struct:
            if hidden_sret:
                return lines
            from .self_backend_sysv_aggregates import store, returned
            lines.extend(store(func.value_slots[dest].offset, returned(ret_type)))
            return lines
        if not (ret_type.is_int or ret_type.is_ptr or ret_type.is_fp):
            raise BackendUnavailable(
                f"x86_64 self backend call return type not translated yet in {func.name!r}: {ret_type.describe()}"
            )
        lines.extend(_store_reg_to_slot(_reg_name(ret_type, 0), func.value_slots[dest].offset, ret_type))
        return lines

    return None


def _emit_return_terminator(func: ParsedFunction, ret_type: TypeDesc, value: str) -> list[str]:
    if ret_type.is_void:
        return _emit_epilogue(func)
    if ret_type.is_array or ret_type.is_struct:
        if _aggregate_returned_indirect(ret_type):
            if func.hidden_sret_slot is None:
                raise BackendUnavailable(
                    f"x86_64 self backend missing hidden sret slot for aggregate return in {func.name!r}"
                )
            lines = _load_slot_to_reg(func.hidden_sret_slot.offset, "r11", func.hidden_sret_slot.type)
            if value == "zeroinitializer":
                lines.extend(_zero_address("r11", ret_type.slot_size))
            elif is_aggregate_literal_value(value):
                literal = aggregate_literal_to_bytes(ret_type, value, type_context=func.type_context)
                lines.extend(_store_literal_bytes_to_address(literal, "r11"))
            else:
                lines.extend(_materialize_aggregate_value_address(func, value, ret_type, "r10"))
                lines.extend(_copy_address_to_address("r10", "r11", ret_type.slot_size))
            lines.extend(_load_slot_to_reg(func.hidden_sret_slot.offset, "rax", func.hidden_sret_slot.type))
            lines.extend(_emit_epilogue(func))
            return lines
        if not _WINDOWS_ABI:
            from .self_backend_sysv_aggregates import load, returned
            lines = load(func, value, ret_type, returned(ret_type))
            lines.extend(_emit_epilogue(func))
            return lines
        sse_members = _aggregate_sse_members(ret_type)
        if sse_members:
            lines = _materialize_aggregate_to_sse_regs(
                func,
                value,
                ret_type,
                tuple(
                    (f"xmm{index}", member_type, member_offset)
                    for index, (member_type, member_offset) in enumerate(sse_members)
                ),
            )
            lines.extend(_emit_epilogue(func))
            return lines
        lines = _materialize_aggregate_to_gp_regs(
            func,
            value,
            ret_type,
            tuple(zip(("rax", "rdx"), _aggregate_reg_chunks(ret_type), strict=False)),
        )
        lines.extend(_emit_epilogue(func))
        return lines
    if not (ret_type.is_int or ret_type.is_ptr or ret_type.is_fp):
        raise BackendUnavailable(
            f"x86_64 self backend return type not translated yet in {func.name!r}: {ret_type.describe()}"
        )
    lines = _materialize_value(func, value, ret_type, _reg_name(ret_type, 0))
    lines.extend(_emit_epilogue(func))
    return lines


def _emit_epilogue(func: ParsedFunction) -> list[str]:
    if _WINDOWS_ABI:
        from .self_backend_win64_abi import epilogue
        return epilogue(func)
    if _is_linux_process_entry(func):
        # There is no caller and therefore no return address.  A correctly
        # authored process entry terminates through pcc.unsafe.process_exit;
        # trap if that machine-boundary syscall unexpectedly returns.
        return ["  ud2"]
    lines: list[str] = []
    saved_rbx = 176 if func.is_vararg else 0
    lines.append(f"  mov rbx, QWORD PTR [rsp + {saved_rbx}]")
    total_frame = func.frame_size + func.platform_frame_extra
    if total_frame:
        lines.append(f"  add rsp, {total_frame}")
    lines.append("  pop rbp")
    lines.append("  ret")
    return lines


def _emit_phi_assignments(func: ParsedFunction, *, source_block: str, target_block: str) -> list[str]:
    target = func.block_map.get(target_block)
    if target is None:
        raise BackendUnavailable(
            f"x86_64 self backend branch targets unknown block {target_block!r} in {func.name!r}"
        )

    assignments = []
    temp_offset = 0
    for phi in target.phis:
        if phi.dest not in func.value_slots:
            continue
        match = next((incoming for incoming in phi.incoming if incoming.label == source_block), None)
        if match is None:
            raise BackendUnavailable(
                f"x86_64 self backend could not resolve phi incoming for {phi.dest!r} from {source_block!r}"
            )
        if not (phi.type.is_int or phi.type.is_ptr or phi.type.is_fp or phi.type.is_array or phi.type.is_struct):
            raise BackendUnavailable(
                f"x86_64 self backend phi type not translated yet in {func.name!r}: {phi.type.describe()}"
            )
        temp_align = max(1, min(phi.type.align, 8))
        temp_offset = _align_to(temp_offset, temp_align)
        assignments.append((phi, match, temp_offset))
        temp_offset += phi.type.slot_size

    if not assignments:
        return []

    direct = _phi_copies_without_temps(func, assignments)
    if direct is not None:
        return direct
    total_temp = _align_to(temp_offset, 16)
    lines: list[str] = []
    if total_temp and not _WINDOWS_ABI:
        lines.append(f"  sub rsp, {total_temp}")

    for phi, match, offset in assignments:
        if phi.type.is_array or phi.type.is_struct:
            lines.append(f"  lea r11, [rsp + {offset}]")
            if phi.type.is_array and phi.type.elem is not None:
                lines.extend(_materialize_vector_value_to_address(func, match.value, phi.type, "r11"))
            elif match.value == "zeroinitializer":
                lines.extend(_zero_address("r11", phi.type.slot_size))
            elif is_aggregate_literal_value(match.value):
                lines.extend(_store_literal_bytes_to_address(aggregate_literal_to_bytes(phi.type, match.value, type_context=func.type_context), "r11"))
            else:
                lines.extend(_materialize_aggregate_value_address(func, match.value, phi.type, "r10"))
                lines.extend(_copy_address_to_address("r10", "r11", phi.type.slot_size))
            continue
        reg = _reg_name(phi.type, 10)
        lines.extend(_materialize_value(func, match.value, phi.type, reg))
        lines.extend(_store_reg_to_temp(reg, offset, phi.type))

    for phi, _match, offset in assignments:
        if phi.type.is_array or phi.type.is_struct:
            lines.append(f"  lea r10, [rsp + {offset}]")
            lines.append(f"  lea r11, {_slot_addr(func.value_slots[phi.dest].offset)}")
            lines.extend(_copy_address_to_address("r10", "r11", phi.type.slot_size))
            continue
        reg = _reg_name(phi.type, 10)
        lines.extend(_load_temp_to_reg(offset, reg, phi.type))
        lines.extend(_store_reg_to_slot(reg, func.value_slots[phi.dest].offset, phi.type))

    if total_temp and not _WINDOWS_ABI:
        lines.append(f"  add rsp, {total_temp}")
    return lines


def _phi_copies_without_temps(func: ParsedFunction, assignments) -> list[str] | None:
    """Scalar phi copies made one after another, or None when they need the
    parallel-copy temps.

    The temps exist for edges where a copy would overwrite a slot a later
    copy still reads (a swap, or two values sharing a slot). One copy reads
    its source before it writes; several are safe when no destination slot
    shares a byte with any source slot.
    """
    destinations: list[tuple[int, int]] = []
    sources: list[tuple[int, int]] = []
    for phi, match, _offset in assignments:
        if phi.type.is_array or phi.type.is_struct:
            return None
        slot = func.value_slots[phi.dest]
        destinations.append((slot.offset, phi.type.slot_size))
        source = func.value_slots.get(match.value)
        if source is not None:
            sources.append((source.offset, source.type.slot_size))
        elif match.value in func.alloca_slots or match.value.startswith("%"):
            # An address or an operand without a slot: keep the temps.
            if match.value not in func.alloca_slots:
                return None
    if len(assignments) > 1:
        for destination_offset, destination_size in destinations:
            for source_offset, source_size in sources:
                # [rbp - offset, rbp - offset + size)
                if (destination_offset - destination_size < source_offset
                        and source_offset - source_size < destination_offset):
                    return None
    lines: list[str] = []
    for phi, match, _offset in assignments:
        reg = _reg_name(phi.type, 10)
        lines.extend(_materialize_value(func, match.value, phi.type, reg))
        lines.extend(_store_reg_to_slot(reg, func.value_slots[phi.dest].offset, phi.type))
    return lines


def _emit_branch_terminator(func: ParsedFunction, source_block: str, target: str) -> list[str]:
    lines = _emit_phi_assignments(func, source_block=source_block, target_block=target)
    lines.append(f"  jmp {_block_label(func.name, target)}")
    return lines


def _emit_cond_branch_terminator(
    func: ParsedFunction,
    source_block: str,
    cond_name: str,
    true_target: str,
    false_target: str,
) -> list[str]:
    true_edge = _edge_label(func.name, source_block, true_target)
    lines = _load_slot_to_reg(func.value_slots[cond_name].offset, "al", TypeDesc("int", 1))
    lines.extend(
        [
            "  test al, al",
            f"  jne {true_edge}",
        ]
    )
    lines.extend(_emit_phi_assignments(func, source_block=source_block, target_block=false_target))
    lines.append(f"  jmp {_block_label(func.name, false_target)}")
    lines.append("")
    lines.append(f"{true_edge}:")
    lines.extend(_emit_phi_assignments(func, source_block=source_block, target_block=true_target))
    lines.append(f"  jmp {_block_label(func.name, true_target)}")
    return lines


def _emit_indexed_error_edge(
    func: ParsedFunction,
    kernel: IndexedFunctionKernel,
    edge_id: int,
    cold_lines: list[str],
) -> list[str]:
    """Branch to the error target when the edge's i1 condition is true.

    Normal execution falls through. An edge into a shared frame landing goes
    through a cold stub after the function's blocks that first stores the
    raise site's traceback table index into the landing's slot. Error targets
    carry no PHIs (the verifier enforces it), so no edge moves are needed.
    """
    target_id = kernel.inline_error_edge_target(edge_id)
    target_name = kernel.block_names[target_id]
    lines = _materialize_value(
        func,
        kernel.value_name(kernel.inline_error_edge_condition(edge_id)),
        TypeDesc("int", 1),
        "al",
    )
    lines.append("  test al, al")
    payload = kernel.inline_error_edge_payload(edge_id)
    landing_slot = kernel.inline_error_landing_slot(target_id)
    if payload < 0 or landing_slot < 0:
        lines.append(f"  jne {_block_label(func.name, target_name)}")
        return lines
    slot_name = kernel.value_name(landing_slot)
    if slot_name not in func.alloca_slots:
        raise BackendUnavailable(
            f"x86_64 self backend has no frame slot for inline error landing {slot_name!r} in {func.name!r}"
        )
    stub = _block_label(func.name, target_name + ".edge." + str(edge_id))
    lines.append(f"  jne {stub}")
    cold_lines.append("")
    cold_lines.append(stub + ":")
    cold_lines.append(
        f"  mov DWORD PTR {_slot_addr(func.alloca_slots[slot_name].offset)}, {payload}"
    )
    cold_lines.append(f"  jmp {_block_label(func.name, target_name)}")
    return lines


def _emit_switch_terminator(
    func: ParsedFunction,
    source_block: str,
    value_type: TypeDesc,
    value: str,
    default_target: str,
    cases: tuple[tuple[int, str], ...],
) -> list[str]:
    if not value_type.is_int:
        raise BackendUnavailable(
            f"x86_64 self backend switch value type not translated yet in {func.name!r}: {value_type.describe()}"
        )
    value_reg = _reg_name(value_type, 10)
    case_reg = _reg_name(value_type, 11)
    lines = _materialize_value(func, value, value_type, value_reg)
    edge_targets: list[str] = []
    for case_value, case_target in cases:
        lines.append(f"  mov {case_reg}, {case_value}")
        lines.append(f"  cmp {value_reg}, {case_reg}")
        lines.append(f"  je {_edge_label(func.name, source_block, case_target)}")
        if case_target not in edge_targets:
            edge_targets.append(case_target)
    lines.append(f"  jmp {_edge_label(func.name, source_block, default_target)}")
    if default_target not in edge_targets:
        edge_targets.append(default_target)
    for target in edge_targets:
        lines.append("")
        lines.append(f"{_edge_label(func.name, source_block, target)}:")
        lines.extend(_emit_phi_assignments(func, source_block=source_block, target_block=target))
        lines.append(f"  jmp {_block_label(func.name, target)}")
    return lines


def _emit_instruction(func: ParsedFunction, block, instr: ParsedInstr) -> list[str]:
    return emit_instruction_dispatch(
        func,
        block,
        instr,
        emit_memory=_emit_memory_instruction,
        emit_compute=_emit_compute_instruction,
    )


def _emit_terminator(func: ParsedFunction, block, term: ParsedInstr) -> list[str]:
    return emit_terminator_dispatch(
        func,
        block,
        term,
        emit_ret_void=_emit_epilogue,
        emit_ret=_emit_return_terminator,
        emit_br=_emit_branch_terminator,
        emit_br_cond=_emit_cond_branch_terminator,
        emit_switch=_emit_switch_terminator,
        emit_unreachable=_emit_unreachable_terminator,
    )


def _emit_unreachable_terminator() -> list[str]:
    return ["  ud2"]


def _materialize_indexed_control_flow(
    func: ParsedFunction, kernel: IndexedFunctionKernel,
) -> None:
    # Branch/PHI selection still needs every target block and its PHIs. The
    # ordinary instruction selector only reads one kind/data pair at a time;
    # do not rebuild a second instruction arena to immediately project it again.
    blocks = []
    block_id = 0
    while block_id < len(kernel.block_names):
        blocks.append(kernel.diagnostic_block(block_id, include_instructions=False))
        block_id += 1
    func.blocks = blocks
    func.block_map = {block.name: block for block in blocks}


def _emit_indexed_instruction_parts(
    func: ParsedFunction,
    block: ParsedBlock,
    kernel: IndexedFunctionKernel,
    block_id: int,
    instruction_index: int,
    instruction_id: int,
    kind_id: int,
    data: int | tuple,
    is_volatile: bool,
) -> list[str]:
    # The shared loop supplies packed IDs for its fixed-payload vocabulary and
    # already-decoded tuples for other kinds, including atomic/cold records.
    # Let the authoritative decoder distinguish packed and negative cold IDs.
    if isinstance(data, int):
        data = kernel.instruction_data_by_id(
            instruction_id, block_id, instruction_index,
        )
    # Preserve the legacy projection's metadata check and operand interning.
    # x86 selection itself consumes only kind/data; the kernel retains flags.
    kernel.instruction_arithmetic_flags(block_id, instruction_index)
    return emit_instruction_dispatch_parts(
        func,
        block,
        PARSED_INSTRUCTION_KINDS[kind_id],
        _interned_operands(data),
        emit_memory=_emit_memory_instruction,
        emit_compute=_emit_compute_instruction,
    )


def _emit_function(
    func: ParsedFunction,
    stack_map_plan: FunctionStackMapPlan,
    *,
    phase_timing=None,
) -> list[str]:
    global _WINDOWS_SCRATCH_BASE
    if phase_timing is not None:
        phase_start = phase_timing.start()
    if _WINDOWS_ABI:
        from .self_backend_win64_abi import outgoing_size
        _WINDOWS_SCRATCH_BASE = outgoing_size(func)
    symbol = _asm_symbol(func.name)
    lines = _emit_prologue(func)
    kernel = get_indexed_function_kernel(func)
    if phase_timing is not None:
        phase_timing.add(12, phase_start)
    # Indexed input owns no legacy block graph. Keep the compatibility view
    # alive only while this function emits; retaining it on every function
    # duplicates the whole module's instructions alongside the indexed arenas.
    original_blocks = func.blocks
    original_block_map = func.block_map
    try:
        indexed_instruction_emitter = None
        if (
            type(func) is ParsedFunction
            and type(kernel) is IndexedFunctionKernel
            and type(original_blocks) is list
            and not original_blocks
            and type(original_block_map) is dict
        ):
            _materialize_indexed_control_flow(func, kernel)
            indexed_instruction_emitter = _emit_indexed_instruction_parts
        else:
            kernel.materialize_legacy_blocks(func)
            # Supplied compatibility blocks may have released scalar PHIs.
            # Fresh indexed shells already project every PHI in diagnostic_block;
            # do not revisit all of their empty-PHI blocks to prove that again.
            for block_id, block in enumerate(func.blocks):
                if block.phis:
                    continue
                phi_fact: CompilerInt2 = kernel.block_phi_fact(block_id)
                if phi_fact.second == 0:
                    continue
                projected = []
                phi_index = 0
                while phi_index < phi_fact.second:
                    projected.append(kernel.diagnostic_phi(block_id, phi_index))
                    phi_index += 1
                block.phis = tuple(projected)
        lines.extend(
            emit_function_blocks(
                func,
                block_label=_block_label,
                emit_instruction=_emit_instruction,
                emit_terminator=_emit_terminator,
                stack_map_plan=stack_map_plan,
                indexed_kernel=kernel,
                emit_indexed_instruction=indexed_instruction_emitter,
                emit_indexed_error_edge=_emit_indexed_error_edge,
            )
        )
    finally:
        if not original_blocks:
            func.blocks = original_blocks
            func.block_map = original_block_map
    lines = _fuse_x86_compare_branches(_forward_adjacent_frame_store_load(lines))
    if _POST_SELECTION_PASSES:
        lines = keep_frame_slots_in_registers(lines, func, stack_map_plan, windows=_WINDOWS_ABI)
        # A second round drops the jumps the first one left falling into
        # their own target.
        lines = _thread_x86_jumps(_thread_x86_jumps(lines))
        lines = forward_register_copies(lines)
    lines.append(stack_map_plan.end_label + ":")
    lines.append(f".size {symbol}, .-{symbol}")
    if _WINDOWS_ABI:
        lines.append(".seh_endproc")
    return lines


def _emit_x86_64_module(
    ir_text: str, *, windows: bool = False, module=None,
    stack_map_plans_out=None,
    phase_timing=None,
    builder=None,
) -> str:
    global _WINDOWS_ABI, _X86_EMISSION_ACTIVE
    if _X86_EMISSION_ACTIVE:
        raise BackendUnavailable("x86 emission is already active")
    _X86_EMISSION_ACTIVE = True
    _WINDOWS_ABI = windows
    try:
        if module is None:
            prepared = prepare_module_for_target(
                ir_text,
                aggregate_returned_indirect=_aggregate_returned_indirect,
                phase_timing=phase_timing,
            )
        else:
            from .self_backend_prepare import prepare_parsed_module_for_target
            prepared = prepare_parsed_module_for_target(
                module,
                aggregate_returned_indirect=_aggregate_returned_indirect,
                phase_timing=phase_timing,
            )
        from .self_backend_target_match import is_x86_64_windows_triple
        valid = is_x86_64_windows_triple(prepared.triple) if windows else is_x86_64_linux_triple(prepared.triple)
        if not valid:
            raise BackendUnavailable("x86 emitter target mismatch: " + prepared.triple)
        prepared = run_self_target_memory_pass_pipeline(prepared, "self-x86_64-linux-v0")
        return _emit_prepared_x86_64_module(
            prepared, ir_text, stack_map_plans_out=stack_map_plans_out,
            phase_timing=phase_timing,
            _share_globals_index=True,
            builder=builder,
        )
    finally:
        _WINDOWS_ABI = False
        _X86_EMISSION_ACTIVE = False


def emit_x86_64_linux_asm(ir_text: str) -> str:
    return _emit_x86_64_module(ir_text)


def _append_assembly_chunk(
    chunks: list[str], label_lines: list[str] | None, lines: list[str],
) -> None:
    # Keep finished text compact instead of retaining one Python object per
    # assembly line until the module joins. Symbolic stack-map rendering only
    # needs labels in their original relative order, not instruction lines.
    chunks.append("\n".join(lines))
    if label_lines is not None:
        for line in lines:
            if line.strip().endswith(":"):
                label_lines.append(line)


def _split_assembly_entries(entries: list[str]) -> list[str]:
    # A global initializer is one entry joined with newlines; the text route
    # splits it again when it reads the module text back.
    lines: list[str] = []
    for entry in entries:
        if "\n" in entry or "\r" in entry:
            lines.extend(entry.splitlines())
        else:
            lines.append(entry)
    return lines


def _emit_prepared_x86_64_module(
    prepared, ir_text: str, *, stack_map_plans_out=None, phase_timing=None,
    _share_globals_index: bool = False, builder=None,
) -> str:
    """Emit the module's assembly text, or stream it into ``builder``.

    A streaming caller passes an ``X86ModuleBuilder`` and a list that receives
    the stack-map plans; each function's lines are placed and encoded as soon
    as the function is finished, and no module text is joined.
    """
    global _MODULE_SYMBOLS, _VARARG_FUNCTIONS, _TLS_GLOBALS
    triple = prepared.triple
    validate_x86_tls_globals(prepared.globals_)
    _MODULE_SYMBOLS = prepared.module_symbols
    functions, _profile_decision = apply_function_order_profile(
        prepared.functions,
        ir_text=ir_text,
        target=triple,
    )
    if _WINDOWS_ABI:
        from .self_backend_win64_abi import prepare_frame
        for func in functions:
            prepare_frame(func)
    else:
        for func in functions:
            # Saved rbx and the nearbyint MXCSR pair (after the vararg
            # register save area), then the r12-r15 save area.
            func.platform_frame_extra = (
                (192 if func.is_vararg else 16) + CALLEE_SAVED_AREA_SIZE
            )
    _VARARG_FUNCTIONS = frozenset(func.name for func in functions if func.is_vararg)
    tls_globals: dict[str, GlobalDef] = {}
    for global_ in prepared.globals_:
        if global_.tls_model:
            tls_globals[global_.name] = global_
    _TLS_GLOBALS = tls_globals
    # A plan depends only on its function and the immutable module globals.
    # Construct it at that function's emission boundary so completed kernels
    # retire before the next plan adds root-state and liveness storage.
    # Retain the final plans for target-final stack-map offsets below.
    stack_map_plans = {}
    if builder is not None and stack_map_plans_out is None:
        raise BackendUnavailable("streamed x86 emission packs its stack maps")
    chunks = [".intel_syntax noprefix"]
    if builder is not None:
        builder.append_lines(chunks)
    label_lines = [] if stack_map_plans_out is None else None
    global_lines = emit_globals(prepared.globals_, _MODULE_SYMBOLS)
    if global_lines:
        if builder is None:
            _append_assembly_chunk(chunks, label_lines, global_lines)
        else:
            builder.append_lines(_split_assembly_entries(global_lines))
    del global_lines
    # The public entry owns prepared.globals_: preparation copied the input,
    # and ordinary emission never mutates that list. Direct prepared callers
    # and timing callbacks may expose/mutate it, so retain their original
    # per-function indexing. Even the owned timer's methods and clock can be
    # replaced by callers. The planner additionally rejects custom records.
    globals_index = (
        [] if _share_globals_index and phase_timing is None else None
    )
    try:
        for func in functions:
            if phase_timing is not None:
                phase_start = phase_timing.start()
            plan = build_function_stack_map_plan(
                func,
                prepared.globals_,
                target="x86_64-linux",
                identity_name=_asm_symbol(func.name),
                _globals_index=globals_index,
            )
            if phase_timing is not None:
                phase_timing.add(5, phase_start)
            stack_map_plans[func.name] = plan
            if phase_timing is not None:
                phase_start = phase_timing.start()
            if builder is None:
                _append_assembly_chunk(
                    chunks, label_lines,
                    _emit_function(func, plan, phase_timing=phase_timing),
                )
            else:
                builder.append_lines(_emit_function(func, plan, phase_timing=phase_timing))
            # The stack-map plan retains scalar/root metadata, never the IR.
            # Release completed functions before the next body adds assembly.
            get_indexed_function_kernel(func).close_native_tables()
            if not func.blocks:
                # Direct indexed functions are consumed above. Their compatibility
                # slots are no longer needed by selection; stack-map plans retain
                # scalar offsets and types, never these lookup dictionaries.
                # Retire both the direct and equality-bucket projections before
                # another function or the final assembly/object buffers allocate.
                clear_text_key_mapping(func.value_slots)
                func.value_slot_buckets.clear()
                clear_text_key_mapping(func.alloca_slots)
                func.alloca_slot_buckets.clear()
                clear_text_key_mapping(func.value_types)
            if phase_timing is not None:
                phase_timing.add(6, phase_start)
    finally:
        # A saved exception must not retain the module index through its frames.
        # A later emission or public replanning builds from its current globals.
        if globals_index is not None:
            if globals_index:
                globals_index[0].clear()
            globals_index.clear()
    if phase_timing is not None:
        phase_start = phase_timing.start()
    if functions and precise_stackmap_tables_enabled("x86_64-unknown-linux-gnu"):
        plans = tuple(stack_map_plans[func.name] for func in functions)
        if stack_map_plans_out is None:
            _append_assembly_chunk(chunks, None, render_x86_64_stack_map_section(
                label_lines, plans, function_symbol=_asm_symbol, block_label=_block_label,
            ))
        else:
            # Preserve section order/alignment while deferring metadata bytes
            # to the owned assembler's final variable-length label offsets.
            # Plans retain scalar/root metadata only, never the function IR.
            stack_map_plans_out.extend(plans)
            if builder is None:
                chunks.extend(('.section .pcc_stackmaps,"a",@progbits', '.p2align 3'))
            else:
                builder.append_lines(('.section .pcc_stackmaps,"a",@progbits', '.p2align 3'))
    if builder is not None:
        builder.append_lines(('.section .note.GNU-stack,"",@progbits',))
        reset_operand_intern()
        if phase_timing is not None:
            phase_timing.add(7, phase_start)
        return ""
    chunks.append('.section .note.GNU-stack,"",@progbits')
    # The empty final chunk supplies the newline without copying the full text.
    chunks.append("")
    reset_operand_intern()
    result = "\n".join(chunks)
    if phase_timing is not None:
        phase_timing.add(7, phase_start)
    return result
