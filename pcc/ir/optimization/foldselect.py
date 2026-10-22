"""Fold a speculatable two-entry PHI diamond into ``select``.

Upstream reference:

- ``llvm/lib/Transforms/Utils/SimplifyCFG.cpp``, ``FoldTwoEntryPHINode``.

``SimplifyCFG`` in this tree covers diamonds whose merge block ends in
``ret``.  The shape that dominates the pcc-Python runtime is the other one:
a short-circuit ``or``/``and`` guard whose merge block keeps going.

    entry:        br i1 %c, label %end, label %right
    right:        %b = and i64 %bits, 1
                  %t = icmp eq i64 %b, 1
                  br label %end
    end:          %p = phi i1 [%c, %entry], [%t, %right]
                  br i1 %p, ...

Nothing in ``right`` can fault or be observed, so it may run on both paths.
Hoisting it into ``entry`` turns the PHI into a ``select`` and removes the
branch, the merge edge and the PHI.  That last part is what pays here: this
backend gives every PHI a stack slot, so the lowered diamond costs a ``cset``,
a ``sturb``, a reload and a second branch -- 24 AArch64 instructions for
``ptr_is_null(o) != 0 or is_tagged_int(o) != 0``, which is the first thing
``py_incref``, ``py_decref`` and ``pcc_gc_pointer_is_managed`` each do.

Measured on the C=100 gateway handler, and the result is why this is not in
any default pass list: it removes 22% of the module's PHI nodes and 1.7% of
its IR lines, and changes the emitted instruction count by +1 in
``pcc_gc_pointer_is_managed`` (54 -> 55).  This backend gives every SSA value
a stack slot whether or not it is a PHI, so deleting the PHI deletes the
branch but not the ``cset``/``sturb``/reload that carries the boolean.  The
transform is correct and will pay once values stay in registers; today it
does not.

Two restrictions keep this away from the collector.  Pointer-typed PHIs are
left alone, and no hoisted instruction may produce a pointer: speculating a
managed pointer onto a path that did not have it is exactly the ambiguous root
provenance the precise stack map rejects.  Trapping arithmetic (the division
family) and anything touching memory are excluded for the ordinary reason --
speculation must not introduce a fault or a side effect.
"""

from __future__ import annotations

import re

from .simplifycfg import (
    _BLOCK_LABEL_RE,
    _BR_RE,
    _COND_BR_RE,
    _Block,
    _join_function,
    _parse_blocks,
    _predecessor_counts,
    _split_functions,
)


# Speculatable and non-trapping. ``udiv``/``sdiv``/``urem``/``srem`` are
# deliberately absent: a zero divisor traps, so they may not be hoisted.
_SPECULATABLE_OP_RE = re.compile(
    r"^(?P<op>"
    r"add|sub|mul|and|or|xor|shl|lshr|ashr|"
    r"icmp|select|zext|sext|trunc|bitcast|ptrtoint|freeze"
    r")\b"
)

_ASSIGN_RE = re.compile(r"^%(?P<name>[\w.$-]+)\s*=\s*(?P<rest>.+)$")
_PHI_RE = re.compile(
    r"^%(?P<name>[\w.$-]+)\s*=\s*phi\s+(?P<ty>\S+)\s+(?P<rest>.+?)\s*$"
)
_PHI_INCOMING_RE = re.compile(r"\[\s*(?P<value>[^,\]]+?)\s*,\s*%(?P<label>[\w.$-]+)\s*\]")

# A hoisted body this long stops being a win: the branch it removes was
# cheaper than running the arm unconditionally.  LLVM budgets by cost; this
# subset budgets by count.
_MAX_HOISTED_INSTRUCTIONS = 8


def _is_pointer_type(ty: str) -> bool:
    return ty.strip().rstrip("*").strip() == "ptr" or ty.strip().endswith("*")


def _defined_name(code: str) -> str | None:
    match = _ASSIGN_RE.match(code)
    return match.group("name") if match is not None else None


_CAST_TO_RE = re.compile(r"\bto\s+(?P<ty>\S+)\s*$")


def _result_type(rest: str) -> str | None:
    """The SSA result type of a speculatable instruction, or None if unclear."""

    opcode = rest.split(None, 1)[0]
    if opcode in ("icmp", "fcmp"):
        return "i1"
    if opcode in ("zext", "sext", "trunc", "bitcast", "ptrtoint", "inttoptr"):
        cast = _CAST_TO_RE.search(rest)
        return cast.group("ty") if cast is not None else None
    if opcode == "select":
        # select i1 <cond>, <ty> <a>, <ty> <b>
        parts = rest.split(",")
        if len(parts) < 3:
            return None
        return parts[1].strip().split(None, 1)[0]
    if opcode == "freeze":
        parts = rest.split(None, 2)
        return parts[1] if len(parts) > 1 else None
    # Binary integer arithmetic: ``<op> [flags] <ty> <a>, <b>``.
    tokens = rest.split(",", 1)[0].split()
    for token in tokens[1:]:
        if token in ("nsw", "nuw", "exact", "disjoint"):
            continue
        return token
    return None


def _speculatable_body(block: _Block) -> list[str] | None:
    """Return ``block``'s instructions when every one may run unconditionally."""

    body: list[str] = []
    for code in block.inst_lines():
        if _BR_RE.match(code) is not None:
            continue
        match = _ASSIGN_RE.match(code)
        if match is None:
            return None
        rest = match.group("rest")
        if _SPECULATABLE_OP_RE.match(rest) is None:
            return None
        # ``ptrtoint`` consumes a pointer and yields an integer, which is safe.
        # Producing one is not: speculating a managed pointer onto a path that
        # did not compute it is the ambiguous root provenance the precise stack
        # map rejects.
        result_type = _result_type(rest)
        if result_type is None or _is_pointer_type(result_type):
            return None
        body.append(code)
    if len(body) > _MAX_HOISTED_INSTRUCTIONS:
        return None
    return body


def _parse_phi(code: str) -> tuple[str, str, dict[str, str]] | None:
    match = _PHI_RE.match(code)
    if match is None:
        return None
    incoming = {
        item.group("label"): item.group("value").strip()
        for item in _PHI_INCOMING_RE.finditer(match.group("rest"))
    }
    return match.group("name"), match.group("ty"), incoming


def _split_phi_prefix(block: _Block) -> tuple[list[str], list[str]] | None:
    """Split ``block``'s lines into its leading PHIs and the rest."""

    phis: list[str] = []
    rest: list[str] = []
    for line in block.lines:
        code = line.split(";", 1)[0].strip()
        if not code:
            # A blank or comment-only line before the first PHI belongs with
            # whatever follows it, never with the PHI list itself.
            rest.append(line) if rest else None
            if not rest:
                continue
            continue
        if not rest and " = phi " in code:
            phis.append(line)
            continue
        rest.append(line)
    return phis, rest


def _fold_function(fn_text: str) -> tuple[str, bool]:
    header, blocks, footer = _parse_blocks(fn_text)
    if len(blocks) < 3:
        return fn_text, False

    changed = False
    index = 0
    while index < len(blocks):
        entry = blocks[index]
        code_lines = entry.inst_lines()
        branch = _COND_BR_RE.match(code_lines[-1]) if code_lines else None
        if branch is None:
            index += 1
            continue

        by_label = {block.label: block for block in blocks}
        counts = _predecessor_counts(blocks)
        condition = branch.group("cond").strip()

        for merge_label, right_label in (
            (branch.group("true"), branch.group("false")),
            (branch.group("false"), branch.group("true")),
        ):
            merge = by_label.get(merge_label)
            right = by_label.get(right_label)
            if merge is None or right is None or merge is right or right is entry:
                continue
            if counts.get(merge_label, 0) != 2 or counts.get(right_label, 0) != 1:
                continue
            right_codes = right.inst_lines()
            if not right_codes:
                continue
            tail = _BR_RE.match(right_codes[-1])
            if tail is None or tail.group("label") != merge_label:
                continue
            body = _speculatable_body(right)
            if body is None:
                continue

            split = _split_phi_prefix(merge)
            if split is None:
                continue
            phi_lines, merge_rest = split
            if not phi_lines:
                continue

            rewritten: list[str] = []
            usable = True
            for line in phi_lines:
                parsed = _parse_phi(line.split(";", 1)[0].strip())
                if parsed is None:
                    usable = False
                    break
                name, ty, incoming = parsed
                if set(incoming) != {entry.label, right_label}:
                    usable = False
                    break
                if _is_pointer_type(ty):
                    usable = False
                    break
                # The merge is reached from ``entry`` exactly when the branch
                # picked it, so the condition selects the entry value there.
                on_true = (
                    incoming[entry.label]
                    if merge_label == branch.group("true")
                    else incoming[right_label]
                )
                on_false = (
                    incoming[right_label]
                    if merge_label == branch.group("true")
                    else incoming[entry.label]
                )
                rewritten.append(
                    f"  %{name} = select i1 {condition}, {ty} {on_true}, {ty} {on_false}\n"
                )
            if not usable:
                continue

            # ``right``'s definitions move ahead of the branch; every use was
            # dominated by ``entry`` already, so no rename is needed.
            hoisted = [f"  {code}\n" for code in body]
            terminator_index = None
            for position in range(len(entry.lines) - 1, -1, -1):
                if entry.lines[position].split(";", 1)[0].strip():
                    terminator_index = position
                    break
            if terminator_index is None:
                continue
            entry.lines = (
                entry.lines[:terminator_index]
                + hoisted
                + [f"  br label %{merge_label}\n"]
                + entry.lines[terminator_index + 1:]
            )
            merge.lines = rewritten + merge_rest
            blocks = [block for block in blocks if block.label != right_label]
            changed = True
            break
        index += 1

    if not changed:
        return fn_text, False
    return _join_function(header, blocks, footer), True


def fold_two_entry_phi_text(ir_text: str) -> tuple[str, bool]:
    out: list[str] = []
    changed = False
    for is_function, chunk in _split_functions(ir_text):
        if not is_function:
            out.append(chunk)
            continue
        rewritten, hit = _fold_function(chunk)
        changed = changed or hit
        out.append(rewritten)
    return "".join(out), changed
