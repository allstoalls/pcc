from __future__ import annotations

"""Keep x86-64 scalar frame slots in callee-saved registers.

The x86-64 instruction selector gives every SSA value a frame slot: each
definition is stored to ``[rbp - N]`` and each use reloads it into a scratch
register.  ``rbx`` is the only callee-saved register a frame saves (plus
``rsi``/``rdi`` under Win64), and r12-r15 are never used.  On the CI gate the
x86-64 pcc1 ran every frontend phase 3.7-4.8x slower than the AArch64 pcc1,
whose allocator keeps values in x19-x28.

This pass rewrites one function's finished text.  A slot is a candidate when
it belongs to a scalar SSA value, every access to it is a plain ``mov`` of
one width whose other operand is a general register or an immediate, no
``lea`` takes an address inside it, and no stack map lists it: the collector
reads and relocates root slots in memory.  Liveness over the text's own
control flow gives the interference (a candidate interferes with every
candidate live after one of its stores), and candidates live into the entry
block stay in memory.  Candidates are colored heaviest first (accesses
weighted by loop depth) over the callee-saved registers the function text
does not reference, so no call or instruction clobbers them, and each access
becomes a register move.  ``mov`` leaves the flags alone in either form.

The prologue saves and every epilogue restores the r12-r15 a function uses in
``CALLEE_SAVED_AREA_SIZE`` bytes at the top of the platform frame area, which
both x86-64 targets reserve in every frame.
"""

CALLEE_SAVED_AREA_SIZE = 32
SAVED_AREA_REGISTERS = ("r12", "r13", "r14", "r15")
_SYSV_POOL = ("rbx", "r12", "r13", "r14", "r15")
_WIN64_POOL = ("rbx", "rsi", "rdi", "r12", "r13", "r14", "r15")
# Bounds the interference bitsets of very large functions; the heaviest
# candidates are kept.
MAX_CANDIDATES = 4096

_SLOT_WIDTHS = {"QWORD": 8, "DWORD": 4, "WORD": 2, "BYTE": 1}
_ACCESS_WIDTHS = {"XMMWORD": 16, "TBYTE": 10, "QWORD": 8, "DWORD": 4, "WORD": 2, "BYTE": 1}
_POOL_ALIASES = {
    "rbx": {8: "rbx", 4: "ebx", 2: "bx", 1: "bl"},
    "rsi": {8: "rsi", 4: "esi", 2: "si", 1: "sil"},
    "rdi": {8: "rdi", 4: "edi", 2: "di", 1: "dil"},
    "r12": {8: "r12", 4: "r12d", 2: "r12w", 1: "r12b"},
    "r13": {8: "r13", 4: "r13d", 2: "r13w", 1: "r13b"},
    "r14": {8: "r14", 4: "r14d", 2: "r14w", 1: "r14b"},
    "r15": {8: "r15", 4: "r15d", 2: "r15w", 1: "r15b"},
}
_POOL_BASES = {
    alias: base for base, aliases in _POOL_ALIASES.items() for alias in aliases.values()
}
_GP_WIDTHS = {
    "rax": 8, "eax": 4, "ax": 2, "al": 1,
    "rcx": 8, "ecx": 4, "cx": 2, "cl": 1,
    "rdx": 8, "edx": 4, "dx": 2, "dl": 1,
    "r8": 8, "r8d": 4, "r8w": 2, "r8b": 1,
    "r9": 8, "r9d": 4, "r9w": 2, "r9b": 1,
    "r10": 8, "r10d": 4, "r10w": 2, "r10b": 1,
    "r11": 8, "r11d": 4, "r11w": 2, "r11b": 1,
}
for _aliases in _POOL_ALIASES.values():
    for _width, _alias in _aliases.items():
        _GP_WIDTHS[_alias] = _width
_CONDITIONAL_JUMPS = frozenset({
    "ja", "jae", "jb", "jbe", "jc", "je", "jg", "jge", "jl", "jle", "jna",
    "jnae", "jnb", "jnbe", "jnc", "jne", "jng", "jnge", "jnl", "jnle", "jno",
    "jnp", "jns", "jnz", "jo", "jp", "jpe", "jpo", "js", "jz",
})
# Instructions that read or write rbx, rsi or rdi without naming them.
_IMPLICIT_RBX = frozenset({"cpuid", "cmpxchg8b", "cmpxchg16b", "xlat", "xlatb"})
_STRING_PREFIXES = ("rep", "movs", "stos", "lods", "cmps", "scas", "ins", "outs")
# Substrings of every spelling that names or implicitly uses a pool register.
_SYSV_HINTS = ("bx", "bl", "r12", "r13", "r14", "r15", "cpuid", "cmpxchg", "xlat")
_WIN64_HINTS = _SYSV_HINTS + ("si", "di", "rep", "movs", "stos", "lods", "cmps", "scas", "ins", "outs")


def saved_area_offset(platform_frame_extra: int) -> int:
    """rsp-relative start of the r12-r15 save area of a frame."""
    return platform_frame_extra - CALLEE_SAVED_AREA_SIZE


def _scalar_value_slot_widths(func) -> dict[int, int]:
    widths: dict[int, int] = {}
    for slot in func.value_slots.values():
        slot_type = slot.type
        if slot_type.is_ptr:
            width = 8
        elif slot_type.is_int and 1 <= slot_type.width <= 64:
            width = (slot_type.width + 7) // 8
        else:
            continue
        if width in (1, 2, 4, 8):
            widths[slot.offset] = width
    return widths


def _stack_map_slot_offsets(plan) -> dict[int, bool]:
    """Frame offsets the collector reads or a reload copies from."""
    offsets: dict[int, bool] = {}
    if plan is None:
        return offsets
    packed = plan.packed_records
    # Frame offsets are negative: [rbp - N] is offset -N.
    if packed is not None:
        locations = packed.location_scalars
        index = 0
        count = len(locations)
        while index < count:
            offsets[-locations.get_unchecked(index)] = True
            index += 2
        reloads = packed.reload_scalars
        index = 0
        count = len(reloads)
        while index < count:
            offsets[-reloads.get_unchecked(index)] = True
            index += 3
        return offsets
    for record in plan.records:
        for location in record.locations:
            offsets[-location.offset] = True
        for reload in record.reloads:
            offsets[-reload.source_offset] = True
    return offsets


def _referenced_pool_registers(lines: list[str], frame_lines: dict[str, bool], windows: bool) -> dict[str, bool]:
    """Pool registers some instruction other than the frame's own saves and
    restores names or implicitly uses; whole-text searches find the few
    candidate lines."""
    referenced: dict[str, bool] = {}
    text = "\n".join(lines)
    for hint in (_WIN64_HINTS if windows else _SYSV_HINTS):
        position = text.find(hint)
        while position >= 0:
            start = text.rfind("\n", 0, position) + 1
            end = text.find("\n", position)
            if end < 0:
                end = len(text)
            line = text[start:end]
            if line.startswith("  ") and line not in frame_lines:
                stripped = line.strip()
                if stripped and stripped[0] != ".":
                    _note_register_references(stripped, referenced)
            position = text.find(hint, end)
    return referenced


def _frame_accesses(text: str) -> list[int]:
    """``offset, width`` pairs of every ``[rbp - N]`` in an instruction
    (width 1 for an address, as from ``lea``); offset -1 for any other form."""
    accesses: list[int] = []
    start = text.find("[rbp - ")
    while start >= 0:
        close = text.find("]", start)
        digits = text[start + 7:close] if close > 0 else ""
        width = 1
        if text[:start].endswith(" PTR "):
            prefix = text[:start - 5]
            space = prefix.rfind(" ")
            width = _ACCESS_WIDTHS.get(prefix[space + 1:], 16)
        accesses.append(int(digits) if digits.isdecimal() else -1)
        accesses.append(width)
        start = text.find("[rbp - ", start + 7)
    return accesses


def _note_register_references(text: str, referenced: dict[str, bool]) -> None:
    space = text.find(" ")
    mnemonic = text if space < 0 else text[:space]
    if mnemonic in _IMPLICIT_RBX:
        referenced["rbx"] = True
    if space < 0 or mnemonic.startswith("rep"):
        for prefix in _STRING_PREFIXES:
            if mnemonic.startswith(prefix):
                referenced["rsi"] = True
                referenced["rdi"] = True
    if space < 0:
        return
    for operand in text[space + 1:].split(", "):
        bracket = operand.find("[")
        if bracket < 0:
            base = _POOL_BASES.get(operand)
            if base is not None:
                referenced[base] = True
            continue
        inner = operand[bracket + 1:operand.find("]", bracket)]
        for token in inner.replace("*", " ").replace("+", " ").replace("-", " ").split(" "):
            base = _POOL_BASES.get(token)
            if base is not None:
                referenced[base] = True


def _parse_slot_move(text: str):
    """``(is_store, width, offset)`` for ``mov`` between one frame slot and a
    general register or immediate of the slot's width, else None."""
    if not text.startswith("mov "):
        return None
    operands = text[4:]
    comma = operands.find(", ")
    if comma < 0 or operands.find(", ", comma + 2) >= 0:
        return None
    first = operands[:comma]
    second = operands[comma + 2:]
    if first.endswith("]") and " PTR [rbp - " in first:
        memory = first
        other = second
        is_store = True
    elif second.endswith("]") and " PTR [rbp - " in second:
        memory = second
        other = first
        is_store = False
    else:
        return None
    marker = memory.find(" PTR [rbp - ")
    width = _SLOT_WIDTHS.get(memory[:marker], 0)
    digits = memory[marker + 12:len(memory) - 1]
    if not width or not digits.isdecimal() or "[" in other:
        return None
    if _GP_WIDTHS.get(other, 0) != width:
        immediate = other[1:] if other.startswith("-") else other
        if not is_store or not immediate.isdecimal():
            return None
    return is_store, width, int(digits)


def _rewrite_slot_move(text: str, register: str) -> str:
    operands = text[4:]
    comma = operands.find(", ")
    first = operands[:comma]
    second = operands[comma + 2:]
    if " PTR [rbp - " in first:
        return "  mov " + register + ", " + second
    return "  mov " + first + ", " + register


def _block_of(block_first: list[int], line_index: int) -> int:
    low = 0
    high = len(block_first) - 1
    while low < high:
        middle = (low + high + 1) // 2
        if block_first[middle] <= line_index:
            low = middle
        else:
            high = middle - 1
    return low


def keep_frame_slots_in_registers(
    lines: list[str], func, plan, *, windows: bool,
) -> list[str]:
    """Rewrite one emitted function so candidate slots live in registers."""
    slot_widths = _scalar_value_slot_widths(func)
    if not slot_widths:
        return lines
    roots = _stack_map_slot_offsets(plan)
    frame_lines: dict[str, bool] = {}
    if windows:
        from .self_backend_win64_abi import outgoing_size
        saved_base = outgoing_size(func)
        for register, displacement in (("rbx", 0), ("rsi", 8), ("rdi", 16)):
            frame_lines[f"  mov QWORD PTR [rsp + {saved_base + displacement}], {register}"] = True
            frame_lines[f"  mov {register}, QWORD PTR [rsp + {saved_base + displacement}]"] = True
        save_anchor = "  .seh_endprologue"
        restore_anchor = f"  mov rdi, QWORD PTR [rsp + {saved_base + 16}]"
    else:
        saved_rbx = 176 if func.is_vararg else 0
        save_anchor = f"  mov QWORD PTR [rsp + {saved_rbx}], rbx"
        restore_anchor = f"  mov rbx, QWORD PTR [rsp + {saved_rbx}]"
        frame_lines[save_anchor] = True
        frame_lines[restore_anchor] = True
    count = len(lines)
    labels: dict[str, int] = {}
    block_starts: list[int] = []
    # Per instruction that ends a block: its line and jump target ("" for
    # ret/ud2, which have no successor).
    ends: list[int] = []
    end_targets: list[str] = []
    end_falls: list[bool] = []
    access_lines: list[int] = []
    access_offsets: list[int] = []
    access_stores: list[bool] = []
    seen_widths: dict[int, int] = {}
    # Offset -> widest access a non-candidate form makes there.
    rejected: dict[int, int] = {}
    returns = 0
    first_instruction = -1
    index = 0
    while index < count:
        line = lines[index]
        if not line:
            index += 1
            continue
        if line[0] != " ":
            if line[len(line) - 1] == ":":
                labels[line[:len(line) - 1]] = index
                block_starts.append(index)
            index += 1
            continue
        text = line.strip()
        if not text or text[0] == ".":
            index += 1
            continue
        if first_instruction < 0:
            first_instruction = index
        if "[rbp - " in text:
            move = _parse_slot_move(text)
            if move is None:
                accesses = _frame_accesses(text)
                access = 0
                while access < len(accesses):
                    if accesses[access] < 0:
                        return lines
                    if accesses[access + 1] > rejected.get(accesses[access], 0):
                        rejected[accesses[access]] = accesses[access + 1]
                    access += 2
            else:
                is_store, width, offset = move
                previous = seen_widths.get(offset, 0)
                if previous and previous != width:
                    rejected[offset] = 8
                seen_widths[offset] = width
                access_lines.append(index)
                access_offsets.append(offset)
                access_stores.append(is_store)
        space = text.find(" ")
        mnemonic = text if space < 0 else text[:space]
        if mnemonic == "jmp" or mnemonic in _CONDITIONAL_JUMPS:
            target = text[space + 1:] if space >= 0 else ""
            if not target.startswith(".L"):
                # Indirect or out-of-function jumps leave the text's flow.
                return lines
            ends.append(index)
            end_targets.append(target)
            end_falls.append(mnemonic != "jmp")
        elif mnemonic == "ret" or mnemonic == "ud2":
            if mnemonic == "ret":
                returns += 1
            ends.append(index)
            end_targets.append("")
            end_falls.append(False)
        index += 1
    if first_instruction < 0 or not access_lines:
        return lines

    # A slot whose bytes another width or offset touches is not one value;
    # a pointer into a slot may reach any of its bytes.
    for offset in seen_widths:
        width = seen_widths[offset]
        probe = offset - width + 1
        while probe < offset:
            if probe in seen_widths:
                rejected[offset] = max(rejected.get(offset, 0), width)
                rejected[probe] = max(rejected.get(probe, 0), seen_widths[probe])
            probe += 1
    overlapped: list[int] = []
    for offset in rejected:
        probe = offset - rejected[offset] + 1
        while probe < offset + 8:
            if probe in slot_widths and probe - slot_widths[probe] < offset:
                overlapped.append(probe)
            probe += 1
    for offset in overlapped:
        rejected[offset] = max(rejected.get(offset, 0), 1)

    # Blocks: a label starts one, and a jump, ret or ud2 ends one.
    starts: list[int] = [first_instruction]
    for label_line in block_starts:
        if label_line > first_instruction:
            starts.append(label_line)
    for end in ends:
        if end + 1 < count:
            starts.append(end + 1)
    starts.sort()
    block_first: list[int] = []
    for start in starts:
        if not block_first or block_first[len(block_first) - 1] != start:
            block_first.append(start)
    block_count = len(block_first)

    successors: list[list[int]] = []
    falls: list[bool] = []
    block_id = 0
    while block_id < block_count:
        successors.append([])
        falls.append(True)
        block_id += 1
    end_index = 0
    while end_index < len(ends):
        owner = _block_of(block_first, ends[end_index])
        target = end_targets[end_index]
        if target:
            target_line = labels.get(target, -1)
            if target_line < 0:
                return lines
            successors[owner].append(_block_of(block_first, target_line))
        falls[owner] = end_falls[end_index]
        end_index += 1
    block_id = 0
    while block_id + 1 < block_count:
        if falls[block_id]:
            successors[block_id].append(block_id + 1)
        block_id += 1

    # Loop depth from backward jumps weights each access.
    depth_delta = [0] * (count + 1)
    end_index = 0
    while end_index < len(ends):
        target = end_targets[end_index]
        if target:
            target_line = labels[target]
            if target_line <= ends[end_index]:
                depth_delta[target_line] += 1
                depth_delta[ends[end_index] + 1] -= 1
        end_index += 1
    weights: dict[int, int] = {}
    depth = 0
    position = 0
    access_index = 0
    access_total = len(access_lines)
    while access_index < access_total:
        line_index = access_lines[access_index]
        while position <= line_index:
            depth += depth_delta[position]
            position += 1
        offset = access_offsets[access_index]
        if offset in slot_widths and offset not in rejected and offset not in roots:
            weights[offset] = weights.get(offset, 0) + (1 << (3 * min(depth, 3)))
        access_index += 1
    if not weights:
        return lines
    ranked = sorted(weights, key=lambda offset: -weights[offset])
    if len(ranked) > MAX_CANDIDATES:
        ranked = ranked[:MAX_CANDIDATES]
    bit_of: dict[int, int] = {}
    candidate = 0
    while candidate < len(ranked):
        bit_of[ranked[candidate]] = candidate
        candidate += 1

    # Per-block upward-exposed uses and stores; events are access indices.
    block_events: list[list[int]] = []
    block_id = 0
    while block_id < block_count:
        block_events.append([])
        block_id += 1
    uses = [0] * block_count
    kills = [0] * block_count
    access_index = 0
    while access_index < access_total:
        bit = bit_of.get(access_offsets[access_index], -1)
        if bit >= 0:
            owner = _block_of(block_first, access_lines[access_index])
            block_events[owner].append(access_index)
            mask = 1 << bit
            if access_stores[access_index]:
                kills[owner] |= mask
            elif not kills[owner] & mask:
                uses[owner] |= mask
        access_index += 1
    live_in = [0] * block_count
    live_out = [0] * block_count
    changed = True
    while changed:
        changed = False
        block_id = block_count - 1
        while block_id >= 0:
            out = 0
            for successor in successors[block_id]:
                out |= live_in[successor]
            entering = uses[block_id] | (out & ~kills[block_id])
            if out != live_out[block_id] or entering != live_in[block_id]:
                live_out[block_id] = out
                live_in[block_id] = entering
                changed = True
            block_id -= 1

    # A candidate interferes with every candidate live after its stores; a
    # store nothing reads before the next store is dead.
    interference = [0] * len(ranked)
    dead_stores: dict[int, bool] = {}
    block_id = 0
    while block_id < block_count:
        live = live_out[block_id]
        events = block_events[block_id]
        event_index = len(events) - 1
        while event_index >= 0:
            event = events[event_index]
            bit = bit_of[access_offsets[event]]
            mask = 1 << bit
            if access_stores[event]:
                if live & mask:
                    live ^= mask
                else:
                    dead_stores[event] = True
                interference[bit] |= live
            else:
                live |= mask
            event_index -= 1
        block_id += 1

    referenced = _referenced_pool_registers(lines, frame_lines, windows)
    pool: list[str] = []
    for register in (_WIN64_POOL if windows else _SYSV_POOL):
        if register not in referenced:
            pool.append(register)
    members = [0] * len(pool)
    forbidden = [0] * len(pool)
    assigned: dict[int, str] = {}
    entry_live = live_in[0]
    candidate = 0
    while candidate < len(ranked):
        mask = 1 << candidate
        if not entry_live & mask:
            register_index = 0
            while register_index < len(pool):
                if (
                    not interference[candidate] & members[register_index]
                    and not forbidden[register_index] & mask
                ):
                    members[register_index] |= mask
                    forbidden[register_index] |= interference[candidate]
                    assigned[ranked[candidate]] = pool[register_index]
                    register_index = len(pool)
                register_index += 1
        candidate += 1
    if not assigned and not dead_stores:
        return lines

    used_saved: list[str] = []
    for register in SAVED_AREA_REGISTERS:
        for offset in assigned:
            if assigned[offset] == register:
                used_saved.append(register)
                break
    saves: list[str] = []
    restores: list[str] = []
    area = saved_area_offset(func.platform_frame_extra)
    slot = 0
    for register in used_saved:
        displacement = area + slot * 8
        saves.append(f"  mov QWORD PTR [rsp + {displacement}], {register}")
        if windows:
            saves.append(f"  .seh_savereg {register}, {displacement}")
        restores.append(f"  mov {register}, QWORD PTR [rsp + {displacement}]")
        slot += 1

    rewritten: list[str] = []
    saved = False
    restored = 0
    access_index = 0
    index = 0
    while index < count:
        line = lines[index]
        if access_index < access_total and access_lines[access_index] == index:
            register = assigned.get(access_offsets[access_index], "")
            access_index += 1
            if access_index - 1 in dead_stores:
                index += 1
                continue
            if register:
                width = seen_widths[access_offsets[access_index - 1]]
                rewritten.append(
                    _rewrite_slot_move(line.strip(), _POOL_ALIASES[register][width])
                )
                index += 1
                continue
        if saves and line == save_anchor and not saved:
            if windows:
                rewritten.extend(saves)
                rewritten.append(line)
            else:
                rewritten.append(line)
                rewritten.extend(saves)
            saved = True
            index += 1
            continue
        rewritten.append(line)
        if restores and line == restore_anchor:
            rewritten.extend(restores)
            restored += 1
        index += 1
    if saves and (not saved or restored != returns):
        return lines
    return rewritten
