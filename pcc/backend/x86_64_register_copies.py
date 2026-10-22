from __future__ import annotations

"""Forward x86-64 register copies and drop register moves nothing reads.

The instruction selector moves every operand through a scratch register:
``mov r10, rbx`` / ``cmp r10, 0`` or ``mov r10, rbx`` / ``mov rdi, r10``,
and x86_64_frame_registers turns slot loads and stores into more of these
moves.  Within a block, a read of a register that holds a copy of another
register reads the original instead; afterwards a ``mov``, ``movzx``,
``movsx``, ``movsxd``, ``movabs`` or ``lea`` into a general register that no
instruction reads before it is written again is dropped (none of them
touches flags or memory).

Every instruction outside the modeled set below is a barrier: it may read
and write every general register, so nothing is forwarded into it or across
it and every register is live before it.  A label ends forwarding (another
path may enter there).  rsp and rbp are never forwarded or dropped.
"""

_GP_BASES = (
    "rax", "rcx", "rdx", "rbx", "rsp", "rbp", "rsi", "rdi",
    "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15",
)
_GP_NAMES = (
    ("al", "ax", "eax", "rax"), ("cl", "cx", "ecx", "rcx"),
    ("dl", "dx", "edx", "rdx"), ("bl", "bx", "ebx", "rbx"),
    ("spl", "sp", "esp", "rsp"), ("bpl", "bp", "ebp", "rbp"),
    ("sil", "si", "esi", "rsi"), ("dil", "di", "edi", "rdi"),
    ("r8b", "r8w", "r8d", "r8"), ("r9b", "r9w", "r9d", "r9"),
    ("r10b", "r10w", "r10d", "r10"), ("r11b", "r11w", "r11d", "r11"),
    ("r12b", "r12w", "r12d", "r12"), ("r13b", "r13w", "r13d", "r13"),
    ("r14b", "r14w", "r14d", "r14"), ("r15b", "r15w", "r15d", "r15"),
)
_WIDTH_SLOTS = {1: 0, 2: 1, 4: 2, 8: 3}
_SLOT_WIDTHS = (1, 2, 4, 8)
_REG_BASE: dict[str, int] = {}
_REG_WIDTH: dict[str, int] = {}
for _base in range(16):
    for _slot in range(4):
        _REG_BASE[_GP_NAMES[_base][_slot]] = _base
        _REG_WIDTH[_GP_NAMES[_base][_slot]] = _SLOT_WIDTHS[_slot]

_RAX = 1 << 0
_RCX = 1 << 1
_RDX = 1 << 2
_RSP = 1 << 4
_RBP = 1 << 5
_RSI = 1 << 6
_RDI = 1 << 7
_R8 = 1 << 8
_R9 = 1 << 9
_R10 = 1 << 10
_R11 = 1 << 11
_ALL = (1 << 16) - 1
# The status flags, tracked like a seventeenth register.
_FLAGS = 1 << 16
_EVERYTHING = _ALL | _FLAGS
_FRAME = _RSP | _RBP
# SysV and Win64 argument and caller-saved registers, together. A call
# reads rax only as SysV's variadic vector count, which the emitter sets with
# xor eax, eax or mov al, N after the arguments; _droppable_source keeps
# both, and no other value in rax reaches a callee.
_CALL_READS = _RDI | _RSI | _RDX | _RCX | _R8 | _R9
_CALL_WRITES = _RAX | _RCX | _RDX | _RSI | _RDI | _R8 | _R9 | _R10 | _R11
_SYSCALL_READS = _RAX | _RDI | _RSI | _RDX | _R10 | _R8 | _R9
# The return value (rax, rdx) and every register a callee preserves under
# SysV or Win64; rcx and r8-r11 are dead at a return under both.
_RET_READS = _ALL & ~(_RCX | _R8 | _R9 | _R10 | _R11)
_SYSCALL_WRITES = _RAX | _RCX | _R11

_CONDITIONAL_JUMPS = frozenset({
    "ja", "jae", "jb", "jbe", "jc", "je", "jg", "jge", "jl", "jle", "jna",
    "jnae", "jnb", "jnbe", "jnc", "jne", "jng", "jnge", "jnl", "jnle", "jno",
    "jnp", "jns", "jnz", "jo", "jp", "jpe", "jpo", "js", "jz",
})
# Destination read and written, source read.
_READ_WRITE = frozenset({"add", "sub", "and", "or", "xor", "adc", "sbb"})
# Destination written, source read; a byte or word destination keeps the
# rest of its register.
_MOVES = frozenset({"mov", "movzx", "movsx", "movsxd"})
_COMPARES = frozenset({"cmp", "test"})
_SHIFTS = frozenset({"shl", "shr", "sar", "sal", "rol", "ror"})
_UNARY = frozenset({"neg", "not", "inc", "dec"})
# Only xmm registers and memory: no general register besides addresses.
_VECTOR = frozenset({
    "movsd", "movss", "movaps", "movapd", "movups", "movupd", "movdqu", "movdqa",
    "addsd", "subsd", "mulsd", "divsd", "sqrtsd", "minsd", "maxsd",
    "addss", "subss", "mulss", "divss", "sqrtss", "minss", "maxss",
    "ucomisd", "ucomiss", "comisd", "comiss", "xorpd", "xorps", "andpd",
    "andps", "andnpd", "andnps", "orpd", "orps", "pxor", "cvtsd2ss",
    "cvtss2sd", "roundsd", "roundss", "stmxcsr", "ldmxcsr",
})
# A general register destination is written, general register sources read.
_TRANSFERS = frozenset({
    "movq", "movd", "cvtsi2sd", "cvtsi2ss", "cvttsd2si", "cvttss2si",
    "cvtsd2si", "cvtss2si",
})
_DROPPABLE = frozenset({"mov", "movzx", "movsx", "movsxd", "movabs", "lea"})
_VECTOR_COMPARES = frozenset({"ucomisd", "ucomiss", "comisd", "comiss"})
# Register-destination forms the owned encoder takes with an immediate source
# (a store takes one too; test, adc and sbb do not).
_IMMEDIATE_ALU = frozenset({"add", "sub", "and", "or", "xor", "cmp"})
# The immediate range each operand width takes in an ALU, compare or store
# operand; a 64-bit operand takes a sign-extended imm32.
_IMMEDIATE_RANGES = {1: (-128, 255), 2: (-32768, 65535), 4: (-2147483648, 4294967295),
                     8: (-2147483648, 2147483647)}


def _memory_registers(operand: str) -> int:
    """General registers an address operand reads."""
    open_ = operand.find("[")
    if open_ < 0:
        return 0
    if operand.startswith("rbp - ", open_ + 1):
        return _RBP
    if operand.startswith("rsp + ", open_ + 1):
        return _RSP
    close = operand.find("]", open_)
    mask = 0
    start = open_ + 1
    while start < close:
        space = operand.find(" ", start)
        stop = close
        if 0 <= space < stop:
            stop = space
        star = operand.find("*", start)
        if 0 <= star < stop:
            stop = star
        base = _REG_BASE.get(operand[start:stop], -1)
        if base >= 0:
            mask |= 1 << base
        if space < 0 or space >= close:
            start = close
        else:
            start = space + 1
    return mask


def _forward_memory(operand: str, sources: list[int]) -> str:
    """``operand`` with each address register that holds a full copy of
    another register replaced by that register (never r12 as an index)."""
    mask = _memory_registers(operand)
    held = False
    base = 0
    while base < 16:
        if mask & (1 << base) and sources[base] >= 0:
            held = True
        base += 1
    if not held:
        return operand
    open_ = operand.find("[")
    close = operand.find("]", open_)
    tokens = operand[open_ + 1:close].split(" ")
    changed = False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        star = token.find("*")
        name = token if star < 0 else token[:star]
        base = _REG_BASE.get(name, -1)
        if base >= 0 and _REG_WIDTH[name] == 8 and sources[base] >= 0:
            source = sources[base]
            # r12 as a scaled or second register would be an SIB index.
            is_index = star >= 0 or index > 0
            if not (is_index and source == 12):
                tokens[index] = _GP_BASES[source] + ("" if star < 0 else token[star:])
                changed = True
        index += 1
    if not changed:
        return operand
    return operand[:open_ + 1] + " ".join(tokens) + operand[close:]


# _effects packs reads, writes and the forwardable operands into one int:
# bits 0-16 reads, 17-33 writes, bit 34 operand 0, bit 35 operand 1.
_WRITES_SHIFT = 17
_FIRST_FORWARDABLE = 1 << 34
_SECOND_FORWARDABLE = 1 << 35
_MASK_17 = (1 << 17) - 1


def _effects(mnemonic: str, first_text: str, second_text: str, count: int) -> int:
    """Packed ``reads | writes << 17 | forwardable operands``, or -1 for a
    barrier. Address registers are read in every position."""
    addresses = 0
    if count and "[" in first_text:
        addresses = _memory_registers(first_text)
    if count > 1 and "[" in second_text:
        addresses |= _memory_registers(second_text)
    first = _REG_BASE.get(first_text, -1) if count else -1
    first_mask = (1 << first) if first >= 0 else 0
    second = _REG_BASE.get(second_text, -1) if count > 1 else -1
    second_mask = (1 << second) if second >= 0 else 0
    if mnemonic in _MOVES and count == 2:
        reads = addresses | second_mask
        if first >= 0 and _REG_WIDTH[first_text] <= 2:
            reads |= first_mask
        return reads | (first_mask << _WRITES_SHIFT) | _SECOND_FORWARDABLE
    if mnemonic == "lea" and count == 2:
        return addresses | (first_mask << _WRITES_SHIFT)
    if mnemonic == "movabs" and count == 2:
        return addresses | (first_mask << _WRITES_SHIFT)
    if mnemonic in _READ_WRITE and count == 2:
        if mnemonic == "xor" and first_text == second_text and first >= 0:
            return (first_mask | _FLAGS) << _WRITES_SHIFT
        carry = _FLAGS if mnemonic == "adc" or mnemonic == "sbb" else 0
        return (addresses | first_mask | second_mask | carry
                | ((first_mask | _FLAGS) << _WRITES_SHIFT) | _SECOND_FORWARDABLE)
    if mnemonic in _COMPARES and count == 2:
        return (addresses | first_mask | second_mask | (_FLAGS << _WRITES_SHIFT)
                | _FIRST_FORWARDABLE | _SECOND_FORWARDABLE)
    if mnemonic in _SHIFTS and count == 2:
        # A zero count leaves the flags as they were.
        return (addresses | first_mask | second_mask | _FLAGS
                | ((first_mask | _FLAGS) << _WRITES_SHIFT))
    if mnemonic in _UNARY and count == 1:
        if mnemonic == "not":
            return addresses | first_mask | (first_mask << _WRITES_SHIFT)
        # inc and dec keep the carry flag.
        carry = _FLAGS if mnemonic != "neg" else 0
        return addresses | first_mask | carry | ((first_mask | _FLAGS) << _WRITES_SHIFT)
    if count == 1 and mnemonic.startswith("set"):
        return addresses | first_mask | _FLAGS | (first_mask << _WRITES_SHIFT)
    if count == 2 and mnemonic.startswith("cmov"):
        return (addresses | first_mask | second_mask | _FLAGS
                | (first_mask << _WRITES_SHIFT) | _SECOND_FORWARDABLE)
    if mnemonic == "imul" and count == 2:
        return (addresses | first_mask | second_mask
                | ((first_mask | _FLAGS) << _WRITES_SHIFT) | _SECOND_FORWARDABLE)
    if mnemonic == "imul" and count == 3:
        return addresses | second_mask | ((first_mask | _FLAGS) << _WRITES_SHIFT) | _SECOND_FORWARDABLE
    if (mnemonic == "idiv" or mnemonic == "div") and count == 1:
        return (addresses | first_mask | _RAX | _RDX
                | ((_RAX | _RDX | _FLAGS) << _WRITES_SHIFT) | _FIRST_FORWARDABLE)
    if (mnemonic == "mul" or mnemonic == "imul") and count == 1:
        return (addresses | first_mask | _RAX
                | ((_RAX | _RDX | _FLAGS) << _WRITES_SHIFT) | _FIRST_FORWARDABLE)
    if (mnemonic == "cqo" or mnemonic == "cdq") and count == 0:
        return _RAX | (_RDX << _WRITES_SHIFT)
    if mnemonic == "cdqe" and count == 0:
        return _RAX | (_RAX << _WRITES_SHIFT)
    if mnemonic == "call" and count == 1:
        if first_text == "__pcc_chkstk":
            # The Win64 stack probe takes the frame size in rax and keeps
            # it; it uses r10 and r11 (self_backend_x86_64_windows).
            return _RAX | _RSP | ((_R10 | _R11 | _FLAGS) << _WRITES_SHIFT)
        return addresses | first_mask | _CALL_READS | ((_CALL_WRITES | _FLAGS) << _WRITES_SHIFT)
    if mnemonic == "syscall" and count == 0:
        return _SYSCALL_READS | ((_SYSCALL_WRITES | _FLAGS) << _WRITES_SHIFT)
    if mnemonic == "push" and count == 1:
        return addresses | first_mask | _RSP | (_RSP << _WRITES_SHIFT) | _FIRST_FORWARDABLE
    if mnemonic == "pop" and count == 1:
        return addresses | _RSP | ((first_mask | _RSP) << _WRITES_SHIFT)
    if mnemonic == "ret":
        return _RET_READS
    if mnemonic in _CONDITIONAL_JUMPS:
        return addresses | _FLAGS
    if mnemonic == "jmp" or mnemonic == "ud2" or mnemonic == "nop":
        return addresses
    if mnemonic in _VECTOR and count:
        if first >= 0 or second >= 0 or count > 2:
            return -1
        return addresses | ((_FLAGS if mnemonic in _VECTOR_COMPARES else 0) << _WRITES_SHIFT)
    if mnemonic in _TRANSFERS and count == 2:
        return addresses | second_mask | (first_mask << _WRITES_SHIFT) | _SECOND_FORWARDABLE
    return -1


def _droppable(mnemonic: str, first_text: str, second_text: str, count: int) -> bool:
    """An instruction whose only effect is a general register (and, for the
    xor zero idiom, the flags) and that reads no memory: lea computes an
    address, setcc reads the flags, the others read a register or an
    immediate. A variadic call's vector count (xor eax, eax or mov al, N)
    always stays: the model has calls read no rax."""
    if not count or first_text not in _REG_BASE:
        return False
    if mnemonic.startswith("set"):
        return count == 1
    if mnemonic == "xor":
        return count == 2 and first_text == second_text and first_text != "eax" and first_text != "rax"
    if count != 2 or mnemonic not in _DROPPABLE:
        return False
    if first_text == "al" and second_text not in _REG_BASE:
        return False
    if mnemonic == "lea":
        return True
    return "[" not in second_text and " PTR " not in second_text and ":" not in second_text


def forward_register_copies(lines: list[str]) -> list[str]:
    """Forward register copies inside blocks, then drop dead register moves.

    Each line is parsed once: its mnemonic and first two operands, the
    registers it reads and writes, and whether it is a droppable move; the
    dead-move pass reuses that.
    """
    count = len(lines)
    out = lines
    copied = False
    reads_of = [0] * count
    writes_of = [0] * count
    # 1: a droppable move; 2: dropped already (a self-move).
    droppable = [0] * count
    labels: dict[str, int] = {}
    ends: list[int] = []
    end_targets: list[str] = []
    end_falls: list[int] = []
    followable = True
    sources = [-1] * 16
    widths = [0] * 16
    # Number of registers that currently hold a copy.
    copies = 0
    # Registers known to hold an immediate, at the width it was written.
    values = [0] * 16
    value_widths = [0] * 16
    constants = 0
    index = 0
    while index < count:
        line = lines[index]
        if not line.startswith("  "):
            if line.endswith(":"):
                labels[line[:len(line) - 1]] = index
                if copies or constants:
                    base = 0
                    while base < 16:
                        sources[base] = -1
                        value_widths[base] = 0
                        base += 1
                    copies = 0
                    constants = 0
            index += 1
            continue
        if line.startswith("  ."):
            index += 1
            continue
        space = line.find(" ", 2)
        tail = ""
        if space < 0:
            mnemonic = line[2:]
            first_text = ""
            second_text = ""
            operands = 0
        else:
            mnemonic = line[2:space]
            comma = line.find(", ", space + 1)
            if comma < 0:
                first_text = line[space + 1:]
                second_text = ""
                operands = 1
            else:
                first_text = line[space + 1:comma]
                after = line.find(", ", comma + 2)
                if after < 0:
                    second_text = line[comma + 2:]
                    operands = 2
                else:
                    second_text = line[comma + 2:after]
                    tail = line[after:]
                    operands = 3
        packed = _effects(mnemonic, first_text, second_text, operands)
        if packed < 0:
            if copies or constants:
                base = 0
                while base < 16:
                    sources[base] = -1
                    value_widths[base] = 0
                    base += 1
                copies = 0
                constants = 0
            reads_of[index] = _EVERYTHING
            writes_of[index] = _EVERYTHING
            index += 1
            continue
        changed = False
        if copies:
            if packed & _FIRST_FORWARDABLE:
                base = _REG_BASE.get(first_text, -1)
                if base >= 0 and sources[base] >= 0 and _REG_WIDTH[first_text] <= widths[base]:
                    first_text = _GP_NAMES[sources[base]][_WIDTH_SLOTS[_REG_WIDTH[first_text]]]
                    changed = True
            if packed & _SECOND_FORWARDABLE:
                base = _REG_BASE.get(second_text, -1)
                if base >= 0 and sources[base] >= 0 and _REG_WIDTH[second_text] <= widths[base]:
                    second_text = _GP_NAMES[sources[base]][_WIDTH_SLOTS[_REG_WIDTH[second_text]]]
                    changed = True
            if operands and "[" in first_text:
                forwarded = _forward_memory(first_text, sources)
                if forwarded is not first_text:
                    first_text = forwarded
                    changed = True
            if operands > 1 and "[" in second_text:
                forwarded = _forward_memory(second_text, sources)
                if forwarded is not second_text:
                    second_text = forwarded
                    changed = True
            if changed:
                packed = _effects(mnemonic, first_text, second_text, operands)
            writes = (packed >> _WRITES_SHIFT) & _MASK_17
            if writes:
                base = 0
                while base < 16:
                    if sources[base] >= 0 and (
                        writes & (1 << base) or writes & (1 << sources[base])
                    ):
                        sources[base] = -1
                        copies -= 1
                    base += 1
        if constants and operands == 2 and (
            mnemonic == "mov" or (
                mnemonic in _IMMEDIATE_ALU and first_text in _REG_BASE
                and first_text != second_text
            )
        ):
            base = _REG_BASE.get(second_text, -1)
            width = _REG_WIDTH[second_text] if base >= 0 else 0
            if base >= 0 and value_widths[base] == width:
                value = values[base]
                low = _IMMEDIATE_RANGES[width][0]
                high = _IMMEDIATE_RANGES[width][1]
                if (mnemonic == "mov" and first_text in _REG_BASE) or low <= value <= high:
                    second_text = str(value)
                    changed = True
                    packed = _effects(mnemonic, first_text, second_text, operands)
        writes = (packed >> _WRITES_SHIFT) & _MASK_17
        if constants and writes:
            base = 0
            while base < 16:
                if value_widths[base] and writes & (1 << base):
                    value_widths[base] = 0
                    constants -= 1
                base += 1
        if (
            mnemonic == "mov" and operands == 2 and first_text == second_text
            and first_text in _REG_BASE and _REG_WIDTH[first_text] != 4
        ):
            # A self-move changes nothing; only the 32-bit form zero-extends.
            droppable[index] = 2
            index += 1
            continue
        if operands == 2 and (mnemonic == "mov" or mnemonic == "movabs" or (
            mnemonic == "xor" and first_text == second_text
        )):
            target = _REG_BASE.get(first_text, -1)
            if target >= 0 and not (1 << target) & _FRAME:
                immediate = "0" if mnemonic == "xor" else second_text
                digits = immediate[1:] if immediate.startswith("-") else immediate
                if digits.isdecimal():
                    if not value_widths[target]:
                        constants += 1
                    values[target] = int(immediate)
                    value_widths[target] = _REG_WIDTH[first_text]
        if mnemonic == "mov" and operands == 2:
            target = _REG_BASE.get(first_text, -1)
            source = _REG_BASE.get(second_text, -1)
            if (
                target >= 0 and source >= 0 and target != source
                and not (1 << source) & _FRAME and not (1 << target) & _FRAME
                and _REG_WIDTH[first_text] == _REG_WIDTH[second_text]
            ):
                if sources[target] < 0:
                    copies += 1
                sources[target] = source
                widths[target] = _REG_WIDTH[first_text]
        if changed:
            if not copied:
                out = list(lines)
                copied = True
            if operands == 1:
                out[index] = "  " + mnemonic + " " + first_text
            else:
                out[index] = "  " + mnemonic + " " + first_text + ", " + second_text + tail
        reads_of[index] = packed & _MASK_17
        writes_of[index] = writes
        if writes and not writes & _FRAME and _droppable(mnemonic, first_text, second_text, operands):
            droppable[index] = 1
        if mnemonic == "jmp" or mnemonic in _CONDITIONAL_JUMPS:
            if operands == 1 and first_text.startswith(".L"):
                ends.append(index)
                end_targets.append(first_text)
                end_falls.append(0 if mnemonic == "jmp" else 1)
            else:
                followable = False
        elif mnemonic == "ret" or mnemonic == "ud2":
            ends.append(index)
            end_targets.append("")
            end_falls.append(0)
        index += 1
    if followable:
        dead = _dead_moves(count, reads_of, writes_of, droppable, labels, ends, end_targets, end_falls)
    else:
        dead = None
    if dead is None:
        # Still drop the self-moves.
        dead = droppable
        dropped = 0
        index = 0
        while index < count:
            if droppable[index] == 2:
                dropped += 1
            index += 1
        if not dropped:
            return out
        kept: list[str] = []
        index = 0
        while index < count:
            if droppable[index] != 2:
                kept.append(out[index])
            index += 1
        return kept
    kept = []
    index = 0
    while index < count:
        if not dead[index]:
            kept.append(out[index])
        index += 1
    if len(kept) == count and not copied:
        return lines
    return kept


def _dead_moves(
    count: int,
    reads_of: list[int],
    writes_of: list[int],
    droppable: list[int],
    labels: dict[str, int],
    ends: list[int],
    end_targets: list[str],
    end_falls: list[int],
):
    """Per-line 1 for each move nothing reads (self-moves included), or None
    when a jump target is not a label of this text."""
    starts: list[int] = [0]
    for label_line in labels.values():
        starts.append(label_line)
    for end in ends:
        if end + 1 < count:
            starts.append(end + 1)
    starts.sort()
    block_first: list[int] = []
    for start in starts:
        if start < count and (not block_first or block_first[len(block_first) - 1] != start):
            block_first.append(start)
    block_count = len(block_first)
    if not block_count:
        return None
    block_last: list[int] = []
    block_id = 0
    while block_id < block_count:
        block_last.append(block_first[block_id + 1] - 1 if block_id + 1 < block_count else count - 1)
        block_id += 1
    line_block = [0] * count
    block_id = 0
    while block_id < block_count:
        line_index = block_first[block_id]
        while line_index <= block_last[block_id]:
            line_block[line_index] = block_id
            line_index += 1
        block_id += 1
    successors: list[list[int]] = []
    falls = [1] * block_count
    block_id = 0
    while block_id < block_count:
        successors.append([])
        block_id += 1
    end_index = 0
    while end_index < len(ends):
        owner = line_block[ends[end_index]]
        target = end_targets[end_index]
        if target:
            target_line = labels.get(target, -1)
            if target_line < 0:
                return None
            successors[owner].append(line_block[target_line])
        falls[owner] = end_falls[end_index]
        end_index += 1
    block_id = 0
    while block_id + 1 < block_count:
        if falls[block_id]:
            successors[block_id].append(block_id + 1)
        block_id += 1

    # Strong liveness: a droppable move contributes its reads only while what
    # it writes is live, so moves that only feed each other (a byte write
    # merging into a register nothing else reads) stay dead.  Iterating from
    # empty sets reaches the least fixed point.
    live_in = [0] * block_count
    live_out = [0] * block_count
    changed = True
    while changed:
        changed = False
        block_id = block_count - 1
        while block_id >= 0:
            out = 0
            block_successors = successors[block_id]
            successor_index = 0
            while successor_index < len(block_successors):
                out |= live_in[block_successors[successor_index]]
                successor_index += 1
            live = out
            line_index = block_last[block_id]
            first = block_first[block_id]
            while line_index >= first:
                kind = droppable[line_index]
                if kind != 2:
                    writes = writes_of[line_index]
                    if not (kind == 1 and not writes & live):
                        live = (live & ~writes) | reads_of[line_index]
                line_index -= 1
            if out != live_out[block_id] or live != live_in[block_id]:
                live_out[block_id] = out
                live_in[block_id] = live
                changed = True
            block_id -= 1

    dead = [0] * count
    block_id = 0
    while block_id < block_count:
        live = live_out[block_id]
        line_index = block_last[block_id]
        first = block_first[block_id]
        while line_index >= first:
            kind = droppable[line_index]
            if kind == 2:
                dead[line_index] = 1
            else:
                writes = writes_of[line_index]
                if kind == 1 and not writes & live:
                    dead[line_index] = 1
                else:
                    live = (live & ~writes) | reads_of[line_index]
            line_index -= 1
        block_id += 1
    return dead
