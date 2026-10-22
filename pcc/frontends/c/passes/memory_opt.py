"""Passes 19-24: Memory Optimization (IR-level).

  19. Read Elimination (Load Forwarding)  — store v→p; load p → use v
  20. Redundant Store Elimination         — store a; store b to same addr → drop first
  21. Dead Store Elimination              — local within-block dead store cleanup
  22. Load-Load Elimination               — consecutive loads from same addr → reuse
  23. Store-Load Forwarding               — store v; load same addr → v
  24. Partial Redundancy Elimination      — partially redundant loads/computations

This remains intentionally narrow. It performs only single-basic-block rewrites
on simple textual LLVM IR patterns, and clears memory facts across labels,
branches, returns, invokes, unreachable, and calls.
"""

from __future__ import annotations

import re

from pcc.frontends.c.passes.base import IRPass
from pcc.frontends.c.passes.context import PassContext


class _IRLine:
    """Parsed LLVM IR instruction for analysis."""
    __slots__ = ("raw", "is_store", "is_load", "is_call", "var", "addr", "val", "ty", "callee")

    def __init__(self, raw: str):
        self.raw = raw
        self.is_store = False
        self.is_load = False
        self.is_call = False
        self.var = None   # %result for loads
        self.addr = None  # %ptr being accessed
        self.val = None   # stored value (for stores) or loaded type
        self.ty = None    # type string
        self.callee = None


# Regex for store: store TYPE VAL, TYPE* PTR
_STORE_RE = re.compile(
    r"^\s+store\s+(\S+)\s+(\S+),\s+\S+\s+(%\S+)"
)
# Regex for load: %VAR = load TYPE, TYPE* PTR
_LOAD_RE = re.compile(
    r"^\s+(%\S+)\s+=\s+load\s+(\S+),\s+\S+\s+(%\S+)"
)
_CALL_RE = re.compile(
    r'''
    ^\s+
    (?:%\S+\s+=\s+)?
    (?:(?:tail|musttail|notail)\s+)?
    call\s+
    \S+(?:\s+\w+)*
    \s+
    (?:\([^@]*\)\s+)?
    (@(?:"[^"]+"|[A-Za-z0-9_$.]+))
    \(
    ''',
    re.VERBOSE,
)
_BITCAST_ALIAS_RE = re.compile(
    r'^\s+(%("([^"\\]|\\.)+"|[-A-Za-z$._0-9]+))\s+=\s+bitcast\s+\S+\s+(%("([^"\\]|\\.)+"|[-A-Za-z$._0-9]+))\s+to\s+\S+'
)
_INT_CAST_CONST_RE = re.compile(
    r'^\s+(%("([^"\\]|\\.)+"|[-A-Za-z$._0-9]+))\s+=\s+(?:sext|zext|trunc)\s+\S+\s+(-?\d+)\s+to\s+\S+\s*$'
)
_DEF_RE = re.compile(r'^(\s*%("([^"\\]|\\.)+"|[-A-Za-z$._0-9]+)\s*=\s*)(.*)$')
_LOCAL_ID_RE = re.compile(r'%("([^"\\]|\\.)+"|[-A-Za-z$._0-9]+)')


def _normalize_callee_name(raw: str | None) -> str:
    value = str(raw or "").strip()
    if value.startswith("@"):
        value = value[1:]
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    return value


def _parse_ir_line(line: str) -> _IRLine:
    ir = _IRLine(line)
    m = _STORE_RE.match(line)
    if m:
        ir.is_store = True
        ir.ty = m.group(1)
        ir.val = m.group(2)
        ir.addr = m.group(3)
        return ir
    m = _LOAD_RE.match(line)
    if m:
        ir.is_load = True
        ir.var = m.group(1)
        ir.ty = m.group(2)
        ir.addr = m.group(3)
        return ir
    m = _CALL_RE.match(line)
    if m:
        ir.is_call = True
        ir.callee = _normalize_callee_name(m.group(1))
        return ir
    return ir


def _resolve_alias(value: str, aliases: dict[str, str]) -> str:
    current = value
    seen = set()
    while current.startswith("%") and current in aliases and current not in seen:
        seen.add(current)
        current = aliases[current]
    return current


def _rewrite_uses(text: str, aliases: dict[str, str]) -> str:
    if not aliases:
        return text

    def _replace(match):
        token = match.group(0)
        return _resolve_alias(token, aliases)

    return _LOCAL_ID_RE.sub(_replace, text)


def _rewrite_operands(line: str, aliases: dict[str, str]) -> str:
    if not aliases:
        return line
    match = _DEF_RE.match(line)
    if match:
        return match.group(1) + _rewrite_uses(match.group(4), aliases)
    return _rewrite_uses(line, aliases)


def _resolve_exact_alias(addr: str, aliases: dict[str, str]) -> str:
    current = addr
    seen = set()
    while current in aliases and current not in seen:
        seen.add(current)
        current = aliases[current]
    return current


def _resolve_int_constant_alias(token: str, aliases: dict[str, str]) -> str:
    current = token
    seen = set()
    while current.startswith("%") and current in aliases and current not in seen:
        seen.add(current)
        current = aliases[current]
    return current


def _split_top_level_commas(text: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    in_quote = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == '"':
            in_quote = not in_quote
        elif not in_quote:
            if ch in "([{<":
                depth += 1
            elif ch in ")]}>":
                depth = max(0, depth - 1)
            elif ch == "," and depth == 0:
                parts.append(text[start:i].strip())
                start = i + 1
        i += 1
    parts.append(text[start:].strip())
    return parts


def _parse_identity_gep_alias(
    line: str,
    int_constant_aliases: dict[str, str],
) -> tuple[str, str, str] | None:
    if "=" not in line:
        return None
    lhs, rhs = line.split("=", 1)
    lhs_match = _LOCAL_ID_RE.search(lhs)
    if lhs_match is None:
        return None
    result_name = lhs_match.group(0)

    rhs = rhs.strip()
    if rhs.startswith("getelementptr inbounds "):
        rhs = rhs[len("getelementptr inbounds "):]
    elif rhs.startswith("getelementptr "):
        rhs = rhs[len("getelementptr "):]
    else:
        return None

    operands = _split_top_level_commas(rhs)
    if len(operands) < 3:
        return None

    source_type = operands[0]
    base_match = _LOCAL_ID_RE.search(operands[1])
    if base_match is None:
        return None

    for operand in operands[2:]:
        value = _resolve_int_constant_alias(
            operand.strip().split()[-1],
            int_constant_aliases,
        )
        if value != "0":
            return None

    return result_name, base_match.group(0), source_type


def _parse_alloca_slot(line: str) -> tuple[str, str] | None:
    if "=" not in line:
        return None
    lhs, rhs = line.split("=", 1)
    lhs_match = _LOCAL_ID_RE.search(lhs)
    if lhs_match is None:
        return None
    result_name = lhs_match.group(0)

    rhs = rhs.strip()
    if not rhs.startswith("alloca "):
        return None

    operands = _split_top_level_commas(rhs[len("alloca "):])
    if not operands:
        return None
    return result_name, operands[0]


def _clear_memory_facts(
    block_stores: dict[str, str],
    block_loads: dict[str, str],
    removable_store_positions: dict[str, int],
) -> None:
    block_stores.clear()
    block_loads.clear()
    removable_store_positions.clear()


def _drop_potential_alias_facts(
    addr: str,
    exact_slots: set[str],
    block_stores: dict[str, str],
    block_loads: dict[str, str],
    removable_store_positions: dict[str, int],
) -> None:
    if addr not in exact_slots:
        _clear_memory_facts(block_stores, block_loads, removable_store_positions)
        return

    for mapping in (block_stores, block_loads, removable_store_positions):
        for key in list(mapping):
            if key == addr or key not in exact_slots:
                del mapping[key]


def _drop_observed_dead_store_candidates(
    addr: str,
    exact_slots: set[str],
    removable_store_positions: dict[str, int],
) -> None:
    if addr not in exact_slots:
        removable_store_positions.clear()
        return

    for key in list(removable_store_positions):
        if key not in exact_slots:
            del removable_store_positions[key]


_LABEL_RE = re.compile(r"^([A-Za-z0-9_.]+):\s*$")
_BR_UNCOND_RE = re.compile(r"^\s+br\s+label\s+%([A-Za-z0-9_.]+)\s*$")
_BR_COND_RE = re.compile(
    r"^\s+br\s+i1\s+\S+,\s+label\s+%([A-Za-z0-9_.]+),\s+label\s+%([A-Za-z0-9_.]+)\s*$"
)
_SWITCH_HEADER_RE = re.compile(
    r"^\s+switch\s+\S+\s+\S+,\s+label\s+%([A-Za-z0-9_.]+)\s*\[\s*$"
)
_SWITCH_CASE_RE = re.compile(
    r"^\s+\S+\s+\S+,\s+label\s+%([A-Za-z0-9_.]+)\s*$"
)
_DEFINE_RE = re.compile(r"^\s*define\b")


def _scan_block_predecessors(lines: list[str]) -> dict[str, set[str]]:
    """Walk the IR once and compute predecessor sets per basic block.

    Only names blocks by their label. Entry blocks (those reached only
    by function entry, no explicit `br` to them) get an implicit "." entry.
    Returns a map from block label → set of predecessor block labels.
    Multi-function functions are handled: predecessors are scoped within
    the enclosing `define ...` → `}` region.
    """
    preds: dict[str, set[str]] = {}
    current_block: str | None = None
    in_switch = False
    in_define = False
    for line in lines:
        stripped = line.rstrip()
        if _DEFINE_RE.match(stripped):
            in_define = True
            current_block = "<entry>"
            continue
        if stripped == "}":
            in_define = False
            current_block = None
            in_switch = False
            continue
        if not in_define:
            continue
        label_match = _LABEL_RE.match(stripped)
        if label_match:
            label = label_match.group(1)
            preds.setdefault(label, set())
            current_block = label
            in_switch = False
            continue
        if in_switch:
            # Inside a switch's case list `[...]`.
            if stripped.endswith("]"):
                in_switch = False
            case_match = _SWITCH_CASE_RE.match(stripped)
            if case_match and current_block is not None:
                preds.setdefault(case_match.group(1), set()).add(current_block)
            continue
        br_uncond = _BR_UNCOND_RE.match(stripped)
        if br_uncond and current_block is not None:
            preds.setdefault(br_uncond.group(1), set()).add(current_block)
            continue
        br_cond = _BR_COND_RE.match(stripped)
        if br_cond and current_block is not None:
            preds.setdefault(br_cond.group(1), set()).add(current_block)
            preds.setdefault(br_cond.group(2), set()).add(current_block)
            continue
        switch_hdr = _SWITCH_HEADER_RE.match(stripped)
        if switch_hdr and current_block is not None:
            preds.setdefault(switch_hdr.group(1), set()).add(current_block)
            in_switch = True
            continue
    return preds


class MemoryOptIRPass(IRPass):
    """Passes 19-24: Memory optimization on LLVM IR text.

    Performs within basic blocks (between labels/branches):
    - Store-load forwarding (19, 23)
    - Redundant store elimination (20)
    - Load-load elimination (22)

    Phase 4 extension: when a block has exactly one predecessor AND the
    current terminator is an unconditional branch to that block, facts
    propagate across the edge (straight-line CFG). This is the minimum
    cross-block memory forwarding needed to reduce dependence on LLVM
    mem2reg for simple fall-through patterns.
    """
    name = "memory-opt-ir"

    def run(self, ir_text: str, ctx: PassContext) -> str:
        lines = ir_text.split("\n")
        # Phase 4 cross-block: pre-scan CFG to decide which block
        # entries are safe to inherit facts from the just-executed
        # predecessor (exactly one predecessor).
        block_preds = _scan_block_predecessors(lines)

        new_lines: list[str | None] = []
        block_stores: dict[str, tuple[str, str]] = {}   # addr → (type, most recent stored value)
        block_loads: dict[str, tuple[str, str]] = {}    # addr → (type, most recent loaded value)
        removable_store_positions: dict[str, int] = {}  # addr → new_lines index
        value_aliases: dict[str, str] = {}
        exact_slots: set[str] = set()
        exact_slot_types: dict[str, str] = {}
        exact_aliases: dict[str, str] = {}
        int_constant_aliases: dict[str, str] = {}
        # Phase 4 cross-block tracking state.
        current_block: str | None = None
        pending_uncond_target: str | None = None  # target of just-emitted uncond `br`

        forwarded = 0
        eliminated = 0
        dead_stores = 0
        memcpy_like_calls = 0

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("define ") or stripped.startswith("declare ") or stripped == "}":
                _clear_memory_facts(block_stores, block_loads, removable_store_positions)
                value_aliases.clear()
                exact_slots.clear()
                exact_slot_types.clear()
                exact_aliases.clear()
                int_constant_aliases.clear()
                current_block = "<entry>" if stripped.startswith("define ") else None
                pending_uncond_target = None
                new_lines.append(line)
                continue

            # Basic-block label line: decide whether to inherit facts.
            label_match = _LABEL_RE.match(stripped)
            if label_match:
                label = label_match.group(1)
                preds_here = block_preds.get(label, set())
                # Keep facts only when:
                #   - previous terminator was an unconditional br to this block, AND
                #   - this block has exactly one predecessor (current_block).
                safe_fallthrough = (
                    pending_uncond_target == label
                    and len(preds_here) == 1
                    and current_block in preds_here
                )
                if not safe_fallthrough:
                    _clear_memory_facts(block_stores, block_loads, removable_store_positions)
                current_block = label
                pending_uncond_target = None
                new_lines.append(line)
                continue

            # `va_arg` mutates the underlying va_list state through memory.
            # Treat it as an unsupported memory barrier so later loads from the
            # same slot are never forwarded back to the original incoming ptr.
            if "= va_arg " in stripped or stripped.startswith("va_arg "):
                _clear_memory_facts(block_stores, block_loads, removable_store_positions)
                # Loads already eliminated earlier in this block still have
                # SSA aliases. Rewrite the cursor use while invalidating the
                # mutable memory facts, or va_arg refers to a removed load.
                new_lines.append(_rewrite_operands(line, value_aliases))
                continue

            alloca_slot = _parse_alloca_slot(line)
            if alloca_slot is not None:
                slot_name, slot_type = alloca_slot
                exact_slots.add(slot_name)
                exact_slot_types[slot_name] = slot_type
                new_lines.append(line)
                continue

            line = _rewrite_operands(line, value_aliases)

            const_match = _INT_CAST_CONST_RE.match(line)
            if const_match:
                int_constant_aliases[const_match.group(1)] = const_match.group(4)
                new_lines.append(line)
                continue

            match = _BITCAST_ALIAS_RE.match(line)
            if match:
                alias_name = match.group(1)
                source_name = _resolve_exact_alias(match.group(4), exact_aliases)
                if source_name in exact_slots:
                    exact_aliases[alias_name] = source_name
                new_lines.append(line)
                continue

            gep_alias = _parse_identity_gep_alias(line, int_constant_aliases)
            if gep_alias is not None:
                alias_name, base_name, source_type = gep_alias
                source_name = _resolve_exact_alias(base_name, exact_aliases)
                if (
                    source_name in exact_slots
                    and exact_slot_types.get(source_name) == source_type
                ):
                    exact_aliases[alias_name] = source_name
                new_lines.append(line)
                continue

            ir = _parse_ir_line(line)
            if ir.addr:
                ir.addr = _resolve_exact_alias(ir.addr, exact_aliases)

            if ir.is_call:
                if ir.callee and (
                    ir.callee in {"memcpy", "memmove", "memset"}
                    or ir.callee.startswith("llvm.memcpy")
                    or ir.callee.startswith("llvm.memmove")
                    or ir.callee.startswith("llvm.memset")
                ):
                    ctx.record(
                        self.name,
                        "memcpy_like_call",
                        ir.callee,
                    )
                    memcpy_like_calls += 1
            # Block boundaries reset tracking
            stripped = line.strip()
            if (
                stripped.endswith(":")
                or stripped.startswith("br ")
                or stripped.startswith("ret ")
                or stripped.startswith("switch ")
                or stripped.startswith("invoke ")
                or stripped.startswith("unreachable")
                or "call " in stripped  # calls may alias
            ):
                # Phase 4 cross-block: if this is an UNCONDITIONAL branch
                # and the target block has exactly one predecessor (us),
                # defer the clear to the label handler above — which keeps
                # facts live across the edge.
                defer_clear = False
                uncond = _BR_UNCOND_RE.match(stripped)
                if uncond is not None and current_block is not None:
                    target = uncond.group(1)
                    preds_here = block_preds.get(target, set())
                    if len(preds_here) == 1 and current_block in preds_here:
                        defer_clear = True
                        pending_uncond_target = target
                if not defer_clear:
                    _clear_memory_facts(block_stores, block_loads, removable_store_positions)
                    pending_uncond_target = None
                new_lines.append(line)
                continue

            if ir.is_store:
                previous_index = removable_store_positions.get(ir.addr)
                previous_store = block_stores.get(ir.addr)
                if (
                    previous_index is not None
                    and previous_store is not None
                    and previous_store[0] == ir.ty
                    and new_lines[previous_index] is not None
                ):
                    new_lines[previous_index] = None
                    ctx.record(
                        self.name,
                        "redundant_store_elim",
                        ir.addr,
                        f"drop previous store before {ir.val}",
                    )
                    dead_stores += 1
                _drop_potential_alias_facts(
                    ir.addr,
                    exact_slots,
                    block_stores,
                    block_loads,
                    removable_store_positions,
                )
                block_stores[ir.addr] = (ir.ty, ir.val)
                removable_store_positions[ir.addr] = len(new_lines)
                new_lines.append(line)
                continue

            if ir.is_load:
                _drop_observed_dead_store_candidates(
                    ir.addr, exact_slots, removable_store_positions
                )
                stored_fact = block_stores.get(ir.addr)
                if stored_fact is not None and stored_fact[0] == ir.ty:
                    replacement = _resolve_alias(stored_fact[1], value_aliases)
                    value_aliases[ir.var] = replacement
                    ctx.record(
                        self.name, "store_load_forward",
                        f"{ir.var} ← {replacement}",
                    )
                    forwarded += 1
                    block_loads[ir.addr] = (ir.ty, replacement)
                    continue

                loaded_fact = block_loads.get(ir.addr)
                if loaded_fact is not None and loaded_fact[0] == ir.ty:
                    replacement = _resolve_alias(loaded_fact[1], value_aliases)
                    value_aliases[ir.var] = replacement
                    ctx.record(
                        self.name, "load_load_elim",
                        f"{ir.var} = {replacement}",
                    )
                    eliminated += 1
                    continue

                block_loads[ir.addr] = (ir.ty, ir.var)
                new_lines.append(line)
                continue

            new_lines.append(line)

        if forwarded:
            ctx.bump("memory_opt.store_load_forward", forwarded)
        if eliminated:
            ctx.bump("memory_opt.load_load_elim", eliminated)
        if dead_stores:
            ctx.bump("memory_opt.redundant_store_elim", dead_stores)
        if memcpy_like_calls:
            ctx.bump("memory_opt.memcpy_like_calls", memcpy_like_calls)

        return "\n".join(line for line in new_lines if line is not None)
