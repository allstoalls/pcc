"""Promote memory to register (mem2reg) over pcc's own IR model.

Upstream reference:

- ``/tmp/llvm-src/llvm-20.1.8.src/lib/Transforms/Utils/Mem2Reg.cpp`` selects
  the candidates: it scans **only the entry block** for ``alloca``
  instructions, because an ``alloca`` reached more than once yields a fresh
  slot per execution and a single SSA name cannot stand for it.
- ``/tmp/llvm-src/llvm-20.1.8.src/lib/Transforms/Utils/PromoteMemoryToRegister.cpp``
  implements ``llvm::PromoteMemToReg`` itself.
- Cytron, Ferrante, Rosen, Wegman and Zadeck, "Efficiently Computing Static
  Single Assignment Form and the Control Dependence Graph", TOPLAS 13(4),
  1991: place phi nodes at the iterated dominance frontier of the blocks
  that define the variable, then rename uses on a dominator-tree walk.
- Cooper, Harvey and Kennedy, "A Simple, Fast Dominance Algorithm", 2001:
  the iterative immediate-dominator loop and the dominance-frontier loop
  used below.

Why a third implementation exists, and why this is the one that survives.
pcc had two already and neither can be the production path:

- ``pcc/ir_passes/mem2reg.py`` is the faithful port, but it reads the
  function through ``llvmlite.binding``.  A self-hosted ``pcc1`` has no
  llvmlite, so that pass can never run in the compiler pcc ships, and it
  cannot be on the path to a runtime with no llvmlite anywhere.
- ``pcc/py_frontend/compiled_default_passes.py`` is llvmlite-free but only
  implements a textual subset: single-block slots, plus one entry-block
  store that precedes every load.  It needs no dominance information and so
  cannot promote anything that requires a phi.

This module is the full algorithm over ``native_ir.ir_mutator``'s
standard-library-only IR model, with no llvmlite and no ``pcc.ir_passes``
import.  Measured on the 186 runtime-archive modules, which hold 17870
allocas, 74991 loads and 36493 stores:

    owned textual subset       24.5% of the memory operations removed
    ported llvmlite pass       68.9%
    LLVM's own mem2reg + sroa  75.1%

Entry-block allocas are 17483 of those 17870 (97.8%), so following upstream
and skipping the rest costs 2.2% of the candidates and keeps the transform
correct by construction rather than by a loop analysis.

Everything here fails closed.  A terminator this module cannot read, an
alloca whose address reaches anything but a matching load or store, a
non-scalar slot, a volatile or atomic access, or a stored value that is not
a simple operand all leave the function exactly as it was.
"""

from __future__ import annotations

from .ir_mutator import Instruction, MutableModule
from .text_tokens import replace_local_names


_NAME_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.$-"

_SCALAR_TYPES = frozenset(
    {"half", "bfloat", "float", "double", "fp128", "x86_fp80", "ptr"}
)

# ``ret``/``unreachable``/``resume`` end a path; the exception-handling
# terminators are deliberately absent, so a function containing one is left
# alone rather than analysed with an incomplete CFG.
_TERMINATORS_WITHOUT_SUCCESSORS = frozenset({"ret", "unreachable", "resume"})
_TERMINATORS_WITH_LABELS = frozenset(
    {"br", "switch", "indirectbr", "invoke", "callbr"}
)

# Instruction decoding is hand-rolled rather than regex-based, matching the
# textual tier in ``py_frontend/compiled_default_passes``.  `re` is not
# natively lowered in the no-libpython closure, so importing it here costs
# CPython fallback calls in a module pcc1 runs on every compile: measured at
# 20 for this module alone.  These three parsers are the same finite subsets
# the textual tier proves, and they fail closed by returning None.


def _split_assignment(line: str):
    """``%name = rhs`` -> ``(name, rhs)``, or None."""
    stripped = line.strip()
    if not stripped.startswith("%"):
        return None
    marker = stripped.find(" = ")
    if marker < 0:
        return None
    name = stripped[1:marker]
    if not name or not _is_simple_name(name):
        return None
    return name, stripped[marker + 3 :].strip()


def _split_top_level(text: str, delimiter: str) -> list[str]:
    """Split on *delimiter* outside brackets; [] on unbalanced input."""
    out: list[str] = []
    round_depth = 0
    square_depth = 0
    brace_depth = 0
    angle_depth = 0
    start = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char == "(":
            round_depth = round_depth + 1
        elif char == ")":
            round_depth = round_depth - 1
        elif char == "[":
            square_depth = square_depth + 1
        elif char == "]":
            square_depth = square_depth - 1
        elif char == "{":
            brace_depth = brace_depth + 1
        elif char == "}":
            brace_depth = brace_depth - 1
        elif char == "<":
            angle_depth = angle_depth + 1
        elif char == ">":
            angle_depth = angle_depth - 1
        elif char == delimiter and (
            round_depth == 0
            and square_depth == 0
            and brace_depth == 0
            and angle_depth == 0
        ):
            out.append(text[start:index])
            start = index + 1
        if round_depth < 0 or square_depth < 0 or brace_depth < 0 or angle_depth < 0:
            return []
        index = index + 1
    if round_depth != 0 or square_depth != 0 or brace_depth != 0 or angle_depth != 0:
        return []
    out.append(text[start:])
    return out


def _pointer_operand_name(text: str):
    """``ptr %name`` -> ``name``, or None."""
    stripped = str(text).strip()
    if not stripped.startswith("ptr "):
        return None
    operand = stripped[len("ptr ") :].strip()
    if not operand.startswith("%"):
        return None
    pieces = operand[1:].split()
    if not pieces:
        return None
    name = pieces[0]
    if not _is_simple_name(name):
        return None
    return name


def _parse_alloca(text: str):
    """``%name = alloca ty[, ...]`` -> ``(name, ty)``, or None."""
    assignment = _split_assignment(text)
    if assignment is None:
        return None
    name, rhs = assignment
    if not rhs.startswith("alloca "):
        return None
    pieces = _split_top_level(rhs[len("alloca ") :], ",")
    if not pieces:
        return None
    slot_type = pieces[0].strip()
    if not slot_type:
        return None
    return name, slot_type


def _parse_store(text: str):
    """``store ty value, ptr %p[, ...]`` -> ``(ty, value, p)``, or None.

    ``store atomic`` and ``store volatile`` fall out on their own: the type
    token becomes ``atomic``/``volatile``, which is not a scalar type.
    """
    stripped = text.strip()
    if not stripped.startswith("store "):
        return None
    pieces = _split_top_level(stripped[len("store ") :], ",")
    if len(pieces) < 2:
        return None
    first = pieces[0].strip()
    split_at = first.find(" ")
    if split_at <= 0:
        return None
    store_type = first[:split_at].strip()
    value = first[split_at + 1 :].strip()
    pointer = _pointer_operand_name(pieces[1])
    if pointer is None:
        return None
    return store_type, value, pointer


def _parse_load(text: str):
    """``%r = load ty, ptr %p[, ...]`` -> ``(r, ty, p)``, or None."""
    assignment = _split_assignment(text)
    if assignment is None:
        return None
    result, rhs = assignment
    if not rhs.startswith("load "):
        return None
    pieces = _split_top_level(rhs[len("load ") :], ",")
    if len(pieces) < 2:
        return None
    load_type = pieces[0].strip()
    pointer = _pointer_operand_name(pieces[1])
    if pointer is None:
        return None
    return result, load_type, pointer


def _is_simple_name(name: str) -> bool:
    if not name:
        return False
    for char in name:
        if char not in _NAME_CHARS:
            return False
    return True


# A dominance-frontier runner walks up idom links, which is bounded by the
# depth of the dominator tree.  The bound exists so that malformed dominance
# information degrades into "leave this function alone" instead of hanging a
# build.
_IDOM_WALK_LIMIT = 1 << 20


def mem2reg_text(ir_text: str) -> tuple[str, bool]:
    """Promote what this module can prove, returning ``(text, changed)``."""

    text = str(ir_text)
    if " = alloca " not in text:
        return text, False
    module = MutableModule.parse(text)
    changed = False
    for function in module.functions:
        if _promote_function(function):
            changed = True
    if not changed:
        return text, False
    return module.serialize(), True


def _promote_function(function) -> bool:
    blocks = function.blocks
    if len(blocks) == 0:
        return False
    candidates = _entry_block_candidates(function)
    if not candidates:
        return False
    cfg = _build_cfg(function)
    if cfg is None:
        return False
    successors, predecessors = cfg
    entry = blocks[0].name
    order = _reverse_postorder(entry, successors)
    reachable = set(order)
    idom = _immediate_dominators(order, predecessors, reachable)
    if idom is None:
        return False
    frontiers = _dominance_frontiers(order, predecessors, idom, reachable)
    if frontiers is None:
        return False
    uses = _collect_uses(function, candidates, reachable)
    if not uses:
        return False
    children = _dominator_children(order, idom, entry)
    plan = _rename(
        function,
        uses,
        successors,
        predecessors,
        idom,
        frontiers,
        children,
        entry,
        reachable,
    )
    if plan is None:
        return False
    return _apply(function, plan)


def _entry_block_candidates(function) -> dict[str, str]:
    """Scalar allocas in the entry block, mapped to their slot type.

    Upstream restricts promotion to the entry block; a slot allocated
    somewhere a loop can reach again is a different object on each
    execution.
    """
    out: dict[str, str] = {}
    for instruction in function.blocks[0].instructions:
        parsed = _parse_alloca(instruction.text)
        if parsed is None:
            continue
        alloca_name, slot_type = parsed
        if not _is_scalar_type(slot_type):
            continue
        out[alloca_name] = slot_type
    return out


def _collect_uses(function, candidates: dict[str, str], reachable: set):
    """Classify every mention of a candidate, dropping the ones that escape.

    Returns ``{alloca: {"type", "loads", "stores"}}`` where ``loads`` and
    ``stores`` are ``(block, index, payload)``.  An alloca survives only
    when every instruction naming it is its own definition, a matching store
    into it, or a matching load out of it.
    """
    surviving = dict(candidates)
    loads: dict[str, list] = {}
    stores: dict[str, list] = {}
    for name in surviving:
        loads[name] = []
        stores[name] = []
    for block in function.blocks:
        block_reachable = block.name in reachable
        for index, instruction in enumerate(block.instructions):
            text = instruction.text
            if "%" not in text:
                continue
            names = _local_names(text)
            if not names:
                continue
            mentioned = []
            for name in names:
                if name in surviving:
                    mentioned.append(name)
            if not mentioned:
                continue
            alloca_parsed = _parse_alloca(text)
            store_parsed = _parse_store(text)
            load_parsed = _parse_load(text)
            for name in mentioned:
                if name not in surviving:
                    continue
                if alloca_parsed is not None and alloca_parsed[0] == name:
                    continue
                # A slot whose load or store sits in a block the entry cannot
                # reach has no place in the dominator-tree walk below, and
                # deleting the instruction would leave that block referring to
                # a value that no longer exists.
                if not block_reachable:
                    _drop(surviving, loads, stores, name)
                    continue
                if (
                    store_parsed is not None
                    and store_parsed[2] == name
                    and store_parsed[0] == surviving[name]
                ):
                    value = store_parsed[1]
                    if not _is_simple_operand(value) or value == "%" + name:
                        _drop(surviving, loads, stores, name)
                        continue
                    stores[name].append((block.name, index, value))
                    continue
                if (
                    load_parsed is not None
                    and load_parsed[2] == name
                    and load_parsed[1] == surviving[name]
                ):
                    loads[name].append((block.name, index, load_parsed[0]))
                    continue
                _drop(surviving, loads, stores, name)
    out: dict[str, dict] = {}
    for name in surviving:
        if not loads[name] and not stores[name]:
            continue
        out[name] = {
            "type": surviving[name],
            "loads": loads[name],
            "stores": stores[name],
        }
    return out


def _drop(surviving: dict, loads: dict, stores: dict, name: str) -> None:
    surviving.pop(name, None)
    loads.pop(name, None)
    stores.pop(name, None)


def _build_cfg(function):
    """Successors and predecessors per block, or ``None`` when unreadable.

    Predecessor lists keep duplicate edges: ``br i1 %c, label %x, label %x``
    contributes two entries and a phi in ``%x`` must carry two.
    """
    names = set()
    for block in function.blocks:
        if block.name in names:
            return None
        names.add(block.name)
    successors: dict[str, list] = {}
    predecessors: dict[str, list] = {}
    for block in function.blocks:
        successors[block.name] = []
        predecessors[block.name] = []
    for block in function.blocks:
        terminator = _terminator_text(block)
        if terminator is None:
            return None
        opcode, terminator_text = terminator
        if opcode in _TERMINATORS_WITHOUT_SUCCESSORS:
            continue
        targets = _label_operands(terminator_text)
        if not targets:
            return None
        for target in targets:
            if target not in successors:
                return None
            successors[block.name].append(target)
            predecessors[target].append(block.name)
    return successors, predecessors


def _terminator_text(block):
    """The block's terminator opcode plus its full text, or ``None``.

    A terminator may span lines, and the owned IR model stores one line per
    ``Instruction``:

        switch i64 %k, label %other [
          i64 1, label %one
        ]

    leaves ``]`` as the block's last instruction, so reading only that line
    reports no recognizable terminator and the whole function is skipped.
    Scan back to the last recognized terminator opcode instead and take
    everything from there to the end of the block as its text.
    """
    instructions = block.instructions
    index = len(instructions) - 1
    while index >= 0:
        opcode = instructions[index].opcode
        if opcode in _TERMINATORS_WITHOUT_SUCCESSORS or opcode in _TERMINATORS_WITH_LABELS:
            pieces = []
            cursor = index
            while cursor < len(instructions):
                pieces.append(instructions[cursor].text)
                cursor = cursor + 1
            return opcode, "".join(pieces)
        index = index - 1
    return None


def _label_operands(text: str) -> list[str]:
    """Every ``label %name`` operand, in order.

    The token must be a maximal ``label``; a value named ``%label`` is not a
    branch target and must not be read as one.
    """
    out: list[str] = []
    index = 0
    limit = len(text)
    while index < limit:
        found = text.find("label", index)
        if found < 0:
            index = limit
            continue
        after = found + 5
        preceded_by_name = found > 0 and text[found - 1] in _NAME_CHARS
        index = after
        if preceded_by_name or after >= limit:
            continue
        cursor = after
        while cursor < limit and (text[cursor] == " " or text[cursor] == "\t"):
            cursor = cursor + 1
        if cursor >= limit or text[cursor] != "%":
            continue
        end = cursor + 1
        while end < limit and text[end] in _NAME_CHARS:
            end = end + 1
        name = text[cursor + 1 : end]
        index = end
        if name:
            out.append(name)
    return out


def _reverse_postorder(entry: str, successors: dict) -> list[str]:
    """Blocks reachable from *entry*, in reverse postorder.

    The traversal is an explicit stack rather than recursion: real emitted
    modules reach tens of thousands of blocks in one function.
    """
    order: list[str] = []
    visited = set()
    visited.add(entry)
    stack = [[entry, 0]]
    while stack:
        frame = stack[-1]
        node = frame[0]
        children = successors.get(node, [])
        if frame[1] < len(children):
            child = children[frame[1]]
            frame[1] = frame[1] + 1
            if child not in visited:
                visited.add(child)
                stack.append([child, 0])
            continue
        stack.pop()
        order.append(node)
    order.reverse()
    return order


def _immediate_dominators(order: list, predecessors: dict, reachable: set):
    """Cooper/Harvey/Kennedy iterative immediate dominators."""
    if not order:
        return None
    number: dict[str, int] = {}
    for index, block in enumerate(order):
        number[block] = index
    entry = order[0]
    idom: dict[str, str] = {entry: entry}
    changed = True
    rounds = 0
    while changed:
        changed = False
        rounds = rounds + 1
        if rounds > len(order) + 2:
            return None
        for block in order[1:]:
            new_idom = ""
            for predecessor in predecessors.get(block, []):
                if predecessor not in reachable or predecessor not in idom:
                    continue
                if new_idom == "":
                    new_idom = predecessor
                    continue
                joined = _intersect(predecessor, new_idom, idom, number)
                if joined is None:
                    return None
                new_idom = joined
            if new_idom != "" and idom.get(block) != new_idom:
                idom[block] = new_idom
                changed = True
    for block in order:
        if block not in idom:
            return None
    return idom


def _intersect(left: str, right: str, idom: dict, number: dict):
    finger1 = left
    finger2 = right
    steps = 0
    while finger1 != finger2:
        steps = steps + 1
        if steps > _IDOM_WALK_LIMIT:
            return None
        while number[finger1] > number[finger2]:
            finger1 = idom.get(finger1, "")
            if finger1 == "":
                return None
        while number[finger2] > number[finger1]:
            finger2 = idom.get(finger2, "")
            if finger2 == "":
                return None
    return finger1


def _dominance_frontiers(order: list, predecessors: dict, idom: dict, reachable: set):
    """Cooper/Harvey/Kennedy dominance frontiers, section 5."""
    frontiers: dict[str, list] = {}
    members: dict[str, set] = {}
    for block in order:
        frontiers[block] = []
        members[block] = set()
    for block in order:
        joining = []
        for predecessor in predecessors.get(block, []):
            if predecessor in reachable:
                joining.append(predecessor)
        if len(joining) < 2:
            continue
        stop = idom[block]
        for predecessor in joining:
            runner = predecessor
            steps = 0
            while runner != stop:
                steps = steps + 1
                if steps > _IDOM_WALK_LIMIT:
                    return None
                if block not in members[runner]:
                    members[runner].add(block)
                    frontiers[runner].append(block)
                next_runner = idom.get(runner, "")
                if next_runner == "" or next_runner == runner:
                    runner = stop
                    continue
                runner = next_runner
    return frontiers


def _dominator_children(order: list, idom: dict, entry: str) -> dict[str, list]:
    children: dict[str, list] = {}
    for block in order:
        children[block] = []
    for block in order:
        if block == entry:
            continue
        parent = idom[block]
        if parent != block:
            children[parent].append(block)
    return children


def _phi_blocks(store_blocks: list, frontiers: dict, reachable: set) -> list:
    """Iterated dominance frontier of the defining blocks (Cytron)."""
    defining = set()
    work = []
    for block in store_blocks:
        if block in reachable and block not in defining:
            defining.add(block)
            work.append(block)
    placed: list[str] = []
    placed_set = set()
    queued = set(work)
    while work:
        block = work.pop()
        queued.discard(block)
        for target in frontiers.get(block, []):
            if target in placed_set:
                continue
            placed_set.add(target)
            placed.append(target)
            if target not in defining and target not in queued:
                queued.add(target)
                work.append(target)
    return placed


def _rename(
    function,
    uses: dict,
    successors: dict,
    predecessors: dict,
    idom: dict,
    frontiers: dict,
    children: dict,
    entry: str,
    reachable: set,
):
    """Place phis, then rename on a dominator-tree walk.

    Returns the plan the caller applies: which instructions to delete, which
    phi lines to insert, and what each removed load result becomes.
    """
    taken = function.defined_names()
    block_order: dict[str, int] = {}
    for index, block in enumerate(function.blocks):
        block_order[block.name] = index

    # phi_for[block][alloca] = phi ssa name
    phi_for: dict[str, dict] = {}
    phi_sequence: list = []
    names = sorted(uses.keys())
    for name in names:
        store_blocks = []
        for block_name, _index, _value in uses[name]["stores"]:
            store_blocks.append(block_name)
        for block_name in _phi_blocks(store_blocks, frontiers, reachable):
            slot = phi_for.get(block_name)
            if slot is None:
                slot = {}
                phi_for[block_name] = slot
            if name in slot:
                continue
            phi_name = _fresh_name(name + ".phi", taken)
            slot[name] = phi_name
            phi_sequence.append((block_name, name, phi_name))

    # Per-block, per-alloca load and store events, addressed by instruction
    # index so the walk can read them in program order.
    events: dict[str, dict] = {}
    removals: dict[str, set] = {}
    for name in names:
        for block_name, index, result in uses[name]["loads"]:
            _record_event(events, removals, block_name, index, "load", name, result)
        for block_name, index, value in uses[name]["stores"]:
            _record_event(events, removals, block_name, index, "store", name, value)

    replacements: dict[str, str] = {}
    incoming: dict[str, list] = {}
    for block_name, name, phi_name in phi_sequence:
        incoming[phi_name] = []

    current: dict[str, str] = {}
    for name in names:
        current[name] = "undef"

    # Explicit stack: [block, child cursor, saved values to restore]
    stack = [[entry, 0, None]]
    steps = 0
    limit = len(reachable) + 2
    while stack:
        frame = stack[-1]
        block_name = frame[0]
        if frame[2] is None:
            steps = steps + 1
            if steps > limit:
                return None
            saved = {}
            for name in names:
                saved[name] = current[name]
            frame[2] = saved
            slot = phi_for.get(block_name)
            if slot is not None:
                for name in sorted(slot.keys()):
                    current[name] = "%" + slot[name]
            block_events = events.get(block_name)
            if block_events is not None:
                for index in sorted(block_events.keys()):
                    kind, name, payload = block_events[index]
                    if kind == "store":
                        current[name] = payload
                    else:
                        replacements[payload] = current[name]
            for successor in successors.get(block_name, []):
                successor_slot = phi_for.get(successor)
                if successor_slot is None:
                    continue
                for name in sorted(successor_slot.keys()):
                    incoming[successor_slot[name]].append(
                        (block_name, current[name])
                    )
        child_list = children.get(block_name, [])
        if frame[1] < len(child_list):
            child = child_list[frame[1]]
            frame[1] = frame[1] + 1
            stack.append([child, 0, None])
            continue
        for name in names:
            current[name] = frame[2][name]
        stack.pop()

    # Every phi must name each predecessor edge exactly once.  An edge from a
    # block the entry cannot reach never executes, so ``undef`` is the honest
    # incoming value; leaving the edge out would be invalid IR.
    phi_lines: dict[str, list] = {}
    for block_name, name, phi_name in phi_sequence:
        seen: dict[str, str] = {}
        for pred, value in incoming[phi_name]:
            seen[pred] = value
        pieces = []
        for pred in predecessors.get(block_name, []):
            value = seen.get(pred, "undef")
            pieces.append("[ " + value + ", %" + pred + " ]")
        if not pieces:
            return None
        line = (
            "  %"
            + phi_name
            + " = phi "
            + uses[name]["type"]
            + " "
            + ", ".join(pieces)
            + "\n"
        )
        lines = phi_lines.get(block_name)
        if lines is None:
            lines = []
            phi_lines[block_name] = lines
        lines.append(line)

    return {
        "removals": removals,
        "phi_lines": phi_lines,
        "replacements": _resolve(replacements),
        "allocas": names,
    }


def _record_event(
    events: dict,
    removals: dict,
    block_name: str,
    index: int,
    kind: str,
    name: str,
    payload: str,
) -> None:
    block_events = events.get(block_name)
    if block_events is None:
        block_events = {}
        events[block_name] = block_events
    block_events[index] = (kind, name, payload)
    block_removals = removals.get(block_name)
    if block_removals is None:
        block_removals = set()
        removals[block_name] = block_removals
    block_removals.add(index)


def _apply(function, plan: dict) -> bool:
    removals = plan["removals"]
    phi_lines = plan["phi_lines"]
    replacements = plan["replacements"]
    allocas = set(plan["allocas"])

    kept_blocks = []
    for block in function.blocks:
        drop = removals.get(block.name, set())
        instructions = []
        for index, instruction in enumerate(block.instructions):
            if index in drop:
                continue
            if block is function.blocks[0]:
                parsed = _parse_alloca(instruction.text)
                if parsed is not None and parsed[0] in allocas:
                    continue
            instructions.append(instruction)
        inserted = phi_lines.get(block.name)
        if inserted:
            # Phis must precede every non-phi instruction, and must follow the
            # phis the frontend already emitted.
            at = 0
            while at < len(instructions) and instructions[at].opcode == "phi":
                at = at + 1
            new_instructions = list(instructions[:at])
            for line in inserted:
                new_instructions.append(Instruction.from_text(line))
            new_instructions.extend(instructions[at:])
            instructions = new_instructions
        block.instructions = instructions
        kept_blocks.append(block)
    function.blocks = kept_blocks

    if replacements:
        spellings: dict[str, str] = {}
        for name, value in replacements.items():
            spellings[name] = value
        for block in function.blocks:
            for instruction in block.instructions:
                text = replace_local_names(instruction.text, spellings)
                if text != instruction.text:
                    instruction.text = text
    if _references_removed(function, replacements, allocas):
        return False
    return True


def _references_removed(function, replacements: dict, allocas: set) -> bool:
    """Fail-closed check: no deleted definition may still be named."""
    removed = set(allocas)
    for name in replacements:
        removed.add(name)
    if not removed:
        return False
    for block in function.blocks:
        for instruction in block.instructions:
            if "%" not in instruction.text:
                continue
            for name in _local_names(instruction.text):
                if name in removed:
                    return True
    return False


def _resolve(replacements: dict) -> dict:
    """Collapse chains: a load replaced by another removed load's result."""
    out = dict(replacements)
    limit = len(out) + 1
    rounds = 0
    changed = True
    while changed and rounds < limit:
        changed = False
        rounds = rounds + 1
        for name in list(out.keys()):
            value = out[name]
            if not value.startswith("%"):
                continue
            target = out.get(value[1:])
            if target is not None and target != value:
                out[name] = target
                changed = True
    return out


def _local_names(text: str) -> list[str]:
    """Every maximal ``%name`` token, in order, without duplicates."""
    names: list[str] = []
    seen = set()
    index = 0
    limit = len(text)
    while index < limit:
        if text[index] != "%":
            index = index + 1
            continue
        end = index + 1
        while end < limit and text[end] in _NAME_CHARS:
            end = end + 1
        name = text[index + 1 : end]
        if name != "" and name not in seen:
            seen.add(name)
            names.append(name)
        index = end
    return names


def _is_scalar_type(slot_type: str) -> bool:
    text = str(slot_type).strip()
    if text in _SCALAR_TYPES:
        return True
    if text.startswith("i") and text[1:].isdigit():
        return int(text[1:]) > 0
    return False


def _is_simple_operand(value: str) -> bool:
    text = str(value).strip()
    if not text or " " in text or "," in text or "(" in text:
        return False
    return True


def _fresh_name(base: str, taken: set) -> str:
    name = base
    suffix = 0
    while name in taken:
        suffix = suffix + 1
        name = base + "." + str(suffix)
    taken.add(name)
    return name
