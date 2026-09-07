"""SimplifyCFG (subset) — IR-level control-flow cleanup.

Upstream reference:

- ``/private/tmp/llvm-src/llvm-project-20.1.8.src/llvm/lib/Transforms/Utils/SimplifyCFG.cpp``
- ``/private/tmp/llvm-src/llvm-project-20.1.8.src/llvm/lib/Transforms/Scalar/SimplifyCFGPass.cpp``

Upstream implements many transforms. The subset here now covers a
useful family of local, single-entry CFG cleanups that are tractable
with textual rewriting:

- fold ``br i1 true/false`` to the chosen arm,
- collapse conditional branches whose two arms both return,
- rewrite "branch to two returns" into ``select + ret``,
- thread through empty unconditional forwarders,
- collapse two-arm forwarders into a ``phi + ret`` merge block into
  ``select + ret``,
- remove dead control-flow arms by rebuilding the whole function body,
- run local DCE on the rebuilt function text to drop dead branch
  conditions that become unused.

This is still a subset. It does not attempt the wider upstream
surface such as switch lowering, speculative hoisting, sink/common-tail
synthesis across arbitrary blocks, or large PHI surgery.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


from .dce import dce_module_text, _collect_function_attrs


_DEFINE_HEADER_RE = re.compile(
    r"^\s*define\b(?P<body>.*?@(?P<name>[\w\.\$]+)\([^)]*\)[^{]*)\{",
    re.DOTALL,
)
_BLOCK_LABEL_RE = re.compile(r"^\s*(?P<label>[\w\.\-]+):")
_COND_BR_RE = re.compile(
    r"^br\s+i1\s+(?P<cond>[^,]+)\s*,\s*label\s+%(?P<true>[\w\.]+)\s*,\s*label\s+%(?P<false>[\w\.]+)\s*$"
)
_BR_RE = re.compile(r"^br\s+label\s+%(?P<label>[\w\.]+)\s*$")
_RET_RE = re.compile(r"^ret\s+(?P<ty>[^ ]+)\s+(?P<value>.+?)\s*$")
_PHI_HEAD_RE = re.compile(
    r"^%(?P<name>[\w\.]+)\s*=\s*phi\s+(?P<ty>[^ ]+)\s+(?P<rest>.+?)\s*$"
)
_PHI_INCOMING_RE = re.compile(
    r"\[\s*(?P<val>[^,\]]+?)\s*,\s*%(?P<label>[\w\.]+)\s*\]"
)
_SSA_NAME_RE = re.compile(r"%([\w\.]+)\b")
_ASSIGN_RE = re.compile(r"^%(?P<name>[\w\.]+)\s*=\s*(?P<body>.+?)\s*$")
_PURE_OP_HEAD_RE = re.compile(
    r"^(?P<op>"
    r"add|sub|mul|udiv|sdiv|urem|srem|"
    r"and|or|xor|shl|lshr|ashr|"
    r"icmp|select|zext|sext|trunc|bitcast|ptrtoint|inttoptr"
    r")\b"
)


@dataclass
class _Block:
    label: str
    lines: list[str]

    def inst_lines(self) -> list[str]:
        out: list[str] = []
        for line in self.lines:
            code = line.split(";", 1)[0].strip()
            if code:
                out.append(code)
        return out


def _split_functions(ir_text: str) -> list[tuple[bool, str]]:
    """Split IR into chunks tagged as function / non-function."""
    chunks: list[tuple[bool, str]] = []
    current: list[str] = []
    in_function = False
    brace_depth = 0
    for line in ir_text.splitlines(keepends=True):
        stripped = line.lstrip()
        if not in_function and stripped.startswith("define "):
            if current:
                chunks.append((False, "".join(current)))
                current = []
            in_function = True
            brace_depth = line.count("{") - line.count("}")
            current.append(line)
            continue
        if in_function:
            current.append(line)
            brace_depth += line.count("{") - line.count("}")
            if brace_depth <= 0:
                chunks.append((True, "".join(current)))
                current = []
                in_function = False
            continue
        current.append(line)
    if current:
        chunks.append((in_function, "".join(current)))
    return chunks


def _module_context_without_functions(ir_text: str) -> str:
    """Return module-level IR needed to parse one function chunk.

    Several loop-local passes operate on one function body at a time, but
    LLVM still needs the module declarations, target header, attribute
    groups, and metadata used by that function.  Python frontend IR calls
    many runtime helpers, so parsing a bare ``define`` chunk reports
    undefined ``@py_*`` symbols even though the complete module is valid.
    """
    return "".join(
        chunk for is_function, chunk in _split_functions(ir_text)
        if not is_function
    )


def _function_chunk_module(context: str, fn_text: str) -> str:
    """Wrap one function chunk in its module-level parse context."""
    if not context:
        return fn_text
    if context.endswith("\n") or fn_text.startswith("\n"):
        return context + fn_text
    return context + "\n" + fn_text


def _function_name_from_chunk(fn_text: str) -> str | None:
    match = _DEFINE_HEADER_RE.search(fn_text)
    if match is None:
        return None
    return match.group("name")


def _function_declaration_from_chunk(fn_text: str) -> str | None:
    match = _DEFINE_HEADER_RE.search(fn_text)
    if match is None:
        return None
    body = match.group("body").rstrip()
    # Sibling definitions are reintroduced as declarations when a
    # function-local cleanup reparses one function in isolation. LLVM
    # allows ``define internal ...`` but not ``declare internal ...``:
    # declarations have external linkage by default. Drop definition-only
    # linkage tokens while preserving calling conventions / return attrs.
    linkage = re.match(
        r"^(\s*)"
        r"(private|internal|linkonce(?:_odr)?|weak(?:_odr)?|"
        r"available_externally|appending|common|extern_weak|external)"
        r"\b\s*",
        body,
    )
    if linkage is not None:
        body = linkage.group(1) + body[linkage.end():]
    return "declare" + body + "\n"


def _module_context_for_function(ir_text: str, fn_text: str) -> str:
    """Return parse context plus declarations for sibling definitions."""
    skip_name = _function_name_from_chunk(fn_text)
    context = [_module_context_without_functions(ir_text)]
    declarations: list[str] = []
    for is_function, chunk in _split_functions(ir_text):
        if not is_function:
            continue
        name = _function_name_from_chunk(chunk)
        if name is None or name == skip_name:
            continue
        declaration = _function_declaration_from_chunk(chunk)
        if declaration is not None:
            declarations.append(declaration)
    if declarations:
        context.append("".join(declarations))
    return "".join(context)


def _parse_blocks(fn_text: str) -> tuple[str, list[_Block], str]:
    lines = fn_text.splitlines(keepends=True)
    if len(lines) < 2:
        return fn_text, [], ""
    header = lines[0]
    footer = lines[-1]
    body = lines[1:-1]
    blocks: list[_Block] = []
    current: _Block | None = None
    for line in body:
        match = _BLOCK_LABEL_RE.match(line)
        if match is not None:
            if current is not None:
                blocks.append(current)
            current = _Block(label=match.group("label"), lines=[])
            continue
        if current is not None:
            current.lines.append(line)
    if current is not None:
        blocks.append(current)
    return header, blocks, footer


def _join_function(header: str, blocks: list[_Block], footer: str) -> str:
    out = [header]
    for block in blocks:
        out.append(f"{block.label}:\n")
        out.extend(block.lines)
    out.append(footer if footer.endswith("\n") else footer + "\n")
    return "".join(out)


def _resolve_forwarder(label: str, block_map: dict[str, _Block]) -> str:
    seen: set[str] = set()
    current = label
    while current not in seen:
        seen.add(current)
        block = block_map.get(current)
        if block is None:
            break
        insts = block.inst_lines()
        if len(insts) != 1:
            break
        match = _BR_RE.match(insts[0])
        if match is None:
            break
        current = match.group("label")
    return current


def _terminator_targets(block: _Block) -> list[str]:
    insts = block.inst_lines()
    if not insts:
        return []
    if m := _COND_BR_RE.match(insts[-1]):
        return [m.group("true"), m.group("false")]
    if m := _BR_RE.match(insts[-1]):
        return [m.group("label")]
    return []


def _predecessor_counts(blocks: list[_Block]) -> dict[str, int]:
    counts = {block.label: 0 for block in blocks}
    for block in blocks:
        for target in _terminator_targets(block):
            if target in counts:
                counts[target] += 1
    return counts


def _canonicalize_hoisted_chain(
    lines: list[str], ret_value: str
) -> tuple[list[str], str]:
    mapping: dict[str, str] = {}
    next_id = 0
    canonical_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        assign = _ASSIGN_RE.match(stripped)
        if assign is not None:
            mapping[f"%{assign.group('name')}"] = f"%canon{next_id}"
            next_id += 1
        normalized = stripped
        for old, new in mapping.items():
            normalized = re.sub(r"%" + re.escape(old[1:]) + r"\b", new, normalized)
        canonical_lines.append(normalized)
    canonical_ret = ret_value
    for old, new in mapping.items():
        canonical_ret = re.sub(r"%" + re.escape(old[1:]) + r"\b", new, canonical_ret)
    return canonical_lines, canonical_ret


def _rewrite_label_refs(blocks: list[_Block], old: str, new: str) -> None:
    branch_pattern = re.compile(r"label\s+%" + re.escape(old) + r"\b")
    phi_pattern = re.compile(
        r"(\[\s*[^,\]]+,\s*)%" + re.escape(old) + r"(\s*\])"
    )
    for block in blocks:
        new_lines: list[str] = []
        for line in block.lines:
            updated = branch_pattern.sub(f"label %{new}", line)
            # Rebuild only matched PHI predecessor spans. Native re.sub
            # currently owns literal replacements, not backreference
            # templates; the IR transform already knows each capture's role.
            pieces: list[str] = []
            cursor = 0
            while cursor < len(updated):
                suffix = updated[cursor:]
                match = phi_pattern.search(suffix)
                if match is None:
                    break
                pieces.append(suffix[:match.start()])
                pieces.append(match.group(1) + "%" + new + match.group(2))
                cursor += match.end()
            if pieces:
                pieces.append(updated[cursor:])
                updated = "".join(pieces)
            new_lines.append(updated)
        block.lines = new_lines


def _drop_unreachable_blocks(blocks: list[_Block]) -> tuple[list[_Block], bool]:
    if not blocks:
        return blocks, False
    block_map = {block.label: block for block in blocks}
    reachable: set[str] = set()
    worklist = [blocks[0].label]
    while worklist:
        label = worklist.pop()
        if label in reachable or label not in block_map:
            continue
        reachable.add(label)
        worklist.extend(_terminator_targets(block_map[label]))
    if len(reachable) == len(blocks):
        return blocks, False
    return [block for block in blocks if block.label in reachable], True


def _prune_invalid_phi_incomings(blocks: list[_Block]) -> tuple[list[_Block], bool]:
    preds_map: dict[str, list[str]] = {block.label: [] for block in blocks}
    for block in blocks:
        for target in _terminator_targets(block):
            if target in preds_map:
                preds_map[target].append(block.label)

    changed = False
    replacements: dict[str, str] = {}
    for block in blocks:
        valid_preds = set(preds_map.get(block.label, ()))
        new_lines: list[str] = []
        for line in block.lines:
            inst = line.split(";", 1)[0].strip()
            phi = _parse_phi(inst)
            if phi is None:
                new_lines.append(line)
                continue
            name, ty, incoming = phi
            filtered = [
                (pred, val) for pred, val in incoming.items() if pred in valid_preds
            ]
            if len(filtered) == len(incoming):
                new_lines.append(line)
                continue
            changed = True
            if len(filtered) == 1:
                replacements[name] = filtered[0][1]
                continue
            if not filtered:
                continue
            incoming_text = ", ".join(
                f"[ {val}, %{pred} ]" for pred, val in filtered
            )
            new_lines.append(f"  %{name} = phi {ty} {incoming_text}\n")
        block.lines = new_lines
    if not replacements:
        return blocks, changed
    for block in blocks:
        rewritten: list[str] = []
        for line in block.lines:
            updated = line
            for old, new in replacements.items():
                updated = re.sub(r"%" + re.escape(old) + r"\b", new, updated)
            rewritten.append(updated)
        block.lines = rewritten
    return blocks, changed


def _merge_linear_successors(blocks: list[_Block]) -> tuple[list[_Block], bool]:
    changed = False
    while True:
        preds = _predecessor_counts(blocks)
        block_map = {block.label: block for block in blocks}
        merged = False
        for idx, block in enumerate(blocks):
            insts = block.inst_lines()
            if not insts:
                continue
            branch = _BR_RE.match(insts[-1])
            if branch is None:
                continue
            succ = block_map.get(branch.group("label"))
            if succ is None or succ.label == blocks[0].label:
                continue
            if preds.get(succ.label, 0) != 1:
                continue
            if any(" = phi " in line.split(";", 1)[0] for line in succ.lines):
                continue
            succ_idx = next((i for i, b in enumerate(blocks) if b.label == succ.label), None)
            if succ_idx is None:
                continue
            block.lines = _drop_raw_terminator_line(block.lines) + succ.lines
            blocks.pop(succ_idx)
            _rewrite_label_refs(blocks, succ.label, block.label)
            changed = True
            merged = True
            break
        if not merged:
            break
    return blocks, changed


def _drop_raw_terminator_line(lines: list[str]) -> list[str]:
    """Return ``lines`` without the last real instruction line.

    ``_Block.lines`` may end with one or more blank/comment-only lines, so
    slicing with ``[:-1]`` is not enough to remove the block terminator
    reliably. Drop the last line that contains actual IR code instead.
    """
    kept = list(lines)
    for idx in range(len(kept) - 1, -1, -1):
        code = kept[idx].split(";", 1)[0].strip()
        if code:
            del kept[idx]
            break
    return kept


def _direct_ret(block: _Block | None) -> tuple[str, str] | None:
    if block is None:
        return None
    insts = block.inst_lines()
    if len(insts) != 1:
        return None
    match = _RET_RE.match(insts[0])
    if match is None:
        return None
    return match.group("ty"), match.group("value")


def _simple_ret(block: _Block | None) -> tuple[list[str], str, str] | None:
    if block is None:
        return None
    insts = block.inst_lines()
    if len(insts) == 1:
        ret = _RET_RE.match(insts[0])
        if ret is None:
            return None
        return [], ret.group("ty"), ret.group("value")
    if len(insts) != 2:
        return None
    assign = _ASSIGN_RE.match(insts[0])
    if assign is None or _PURE_OP_HEAD_RE.match(assign.group("body")) is None:
        return None
    ret = _RET_RE.match(insts[1])
    if ret is None or ret.group("value") != f"%{assign.group('name')}":
        return None
    return [f"  {insts[0]}\n"], ret.group("ty"), ret.group("value")


def _pure_chain_ret(block: _Block | None) -> tuple[list[str], str, str] | None:
    if block is None:
        return None
    insts = block.inst_lines()
    if not insts:
        return None
    ret = _RET_RE.match(insts[-1])
    if ret is None:
        return None
    op_lines = insts[:-1]
    if not op_lines:
        return [], ret.group("ty"), ret.group("value")
    for line in op_lines:
        assign = _ASSIGN_RE.match(line)
        if assign is None or _PURE_OP_HEAD_RE.match(assign.group("body")) is None:
            return None
    last_assign = _ASSIGN_RE.match(op_lines[-1])
    if last_assign is None or ret.group("value") != f"%{last_assign.group('name')}":
        return None
    return [f"  {line}\n" for line in op_lines], ret.group("ty"), ret.group("value")


def _phi_ret(block: _Block | None) -> tuple[str, str, dict[str, str]] | None:
    if block is None:
        return None
    insts = block.inst_lines()
    if len(insts) != 2:
        return None
    phi = _parse_phi(insts[0])
    if phi is None:
        return None
    ret = _RET_RE.match(insts[1])
    if ret is None or ret.group("value") != f"%{phi[0]}":
        return None
    return phi


def _entry_available_phi_value(
    pred_label: str,
    merge_label: str,
    value: str,
    block_map: dict[str, _Block],
) -> tuple[list[str], str] | None:
    """Return hoisted lines needed to use ``value`` from entry.

    If ``value`` is produced locally inside ``pred_label`` by a single pure op
    or a short pure-op chain followed by ``br label %merge_label``, hoist that
    chain. If ``value`` is
    already available before the branch, no hoist is needed. If it depends on a
    more complex predecessor shape, return ``None`` and let the caller skip the
    transform.
    """
    if not value.startswith("%"):
        return [], value
    pred = block_map.get(pred_label)
    if pred is None:
        return [], value
    insts = pred.inst_lines()
    defined_here = False
    last_defined: str | None = None
    for inst in insts[:-1]:
        assign = _ASSIGN_RE.match(inst)
        if assign is not None:
            last_defined = f"%{assign.group('name')}"
            if last_defined == value:
                defined_here = True
    if defined_here:
        br = _BR_RE.match(insts[-1]) if insts else None
        if br is None or br.group("label") != merge_label:
            return None
        op_lines = insts[:-1]
        if not op_lines or value != last_defined:
            return None
        seen_defs: set[str] = set()
        for line in op_lines:
            assign = _ASSIGN_RE.match(line)
            if assign is None or _PURE_OP_HEAD_RE.match(assign.group("body")) is None:
                return None
            for ref in _SSA_NAME_RE.findall(assign.group("body")):
                if ref in seen_defs:
                    continue
            seen_defs.add(assign.group("name"))
        return [f"  {inst}\n" for inst in op_lines], value
    return [], value


def _phi_single_use_chain_ret(
    block: _Block | None,
) -> tuple[str, str, dict[str, str], list[str], str] | None:
    if block is None:
        return None
    insts = block.inst_lines()
    if len(insts) < 3:
        return None
    phi = _parse_phi(insts[0])
    if phi is None:
        return None
    op_lines = insts[1:-1]
    op_results: list[str] = []
    for line in op_lines:
        assign = _ASSIGN_RE.match(line)
        if assign is None or _PURE_OP_HEAD_RE.match(assign.group("body")) is None:
            return None
        op_results.append(assign.group("name"))
    if f"%{phi[0]}" not in op_lines[0]:
        return None
    for prev_name, line in zip(op_results, op_lines[1:]):
        if f"%{prev_name}" not in line:
            return None
    tracked = {phi[0], *op_results}
    use_counts = {name: 0 for name in tracked}
    for line in op_lines:
        assign = _ASSIGN_RE.match(line)
        if assign is None:
            return None
        for name in _SSA_NAME_RE.findall(assign.group("body")):
            if name in use_counts:
                use_counts[name] += 1
    for name in _SSA_NAME_RE.findall(insts[-1]):
        if name in use_counts:
            use_counts[name] += 1
    if use_counts[phi[0]] != 1:
        return None
    if any(use_counts[name] != 1 for name in op_results):
        return None
    ret = _RET_RE.match(insts[-1])
    if ret is None or ret.group("value") != f"%{op_results[-1]}":
        return None
    return phi[0], phi[1], phi[2], op_lines, insts[-1]


def _phi_chain_ret(
    block: _Block | None,
) -> tuple[str, str, dict[str, str], list[str], str] | None:
    if block is None:
        return None
    insts = block.inst_lines()
    if len(insts) < 3:
        return None
    phi = _parse_phi(insts[0])
    if phi is None:
        return None
    op_lines = insts[1:-1]
    if not op_lines:
        return None
    for line in op_lines:
        assign = _ASSIGN_RE.match(line)
        if assign is None or _PURE_OP_HEAD_RE.match(assign.group("body")) is None:
            return None
    last_assign = _ASSIGN_RE.match(op_lines[-1])
    if last_assign is None:
        return None
    ret = _RET_RE.match(insts[-1])
    if ret is None or ret.group("value") != f"%{last_assign.group('name')}":
        return None
    return phi[0], phi[1], phi[2], op_lines, insts[-1]


def _rewrite_phi_chain_operand(
    op_lines: list[str],
    phi_name: str,
    replacement: str,
) -> list[str]:
    return [
        re.sub(r"%" + re.escape(phi_name) + r"\b", replacement, line)
        for line in op_lines
    ]


def _ssa_use_count(lines: list[str], name: str) -> int:
    count = 0
    for line in lines:
        assign = _ASSIGN_RE.match(line)
        body = assign.group("body") if assign is not None else line
        count += sum(1 for used in _SSA_NAME_RE.findall(body) if used == name)
    return count


def _parse_phi(line: str) -> tuple[str, str, dict[str, str]] | None:
    phi = _PHI_HEAD_RE.match(line)
    if phi is None:
        return None
    incoming: dict[str, str] = {}
    for value, label in _PHI_INCOMING_RE.findall(phi.group("rest")):
        incoming[label] = value.strip()
    if not incoming:
        return None
    return phi.group("name"), phi.group("ty"), incoming


def _unique_temp_name(prefix: str, fn_text: str) -> str:
    name = prefix
    counter = 0
    existing = set(_SSA_NAME_RE.findall(fn_text))
    while name in existing:
        counter += 1
        name = f"{prefix}.{counter}"
    return name


def _unique_block_label(prefix: str, blocks: list[_Block]) -> str:
    name = prefix
    counter = 0
    existing = {block.label for block in blocks}
    while name in existing:
        counter += 1
        name = f"{prefix}.{counter}"
    return name


def _cleanup_function_locally(
    fn_text: str, module_context: str = "",
    function_attrs: dict[str, set[str]] | None = None,
) -> str:
    cleaned, _ = dce_module_text(
        _function_chunk_module(module_context, fn_text), function_attrs,
    )
    for is_function, chunk in _split_functions(cleaned):
        if is_function:
            return chunk
    return fn_text


def _is_trivial_unreachable(block: _Block | None) -> bool:
    if block is None:
        return False
    insts = block.inst_lines()
    return len(insts) == 1 and insts[0] == "unreachable"


def _prefix_inst_lines(block: _Block) -> list[str]:
    """Return all non-terminator instructions from ``block``."""
    insts = block.inst_lines()
    return [f"  {inst}\n" for inst in insts[:-1]]


def _rewrite_simple_conditional_blocks(
    blocks: list[_Block],
    fn_text: str,
) -> tuple[list[_Block], bool]:
    if not blocks:
        return blocks, False
    block_map = {block.label: block for block in blocks}
    for block in blocks:
        insts = block.inst_lines()
        if not insts:
            continue

        term = _COND_BR_RE.match(insts[-1])
        if term is None:
            continue

        cond = term.group("cond").strip()
        true_label = term.group("true")
        false_label = term.group("false")
        true_block = block_map.get(true_label)
        false_block = block_map.get(false_label)
        true_resolved = _resolve_forwarder(true_label, block_map)
        false_resolved = _resolve_forwarder(false_label, block_map)
        new_body_lines = _prefix_inst_lines(block)

        if cond == "true":
            chosen_ret = _direct_ret(block_map.get(true_resolved))
            if chosen_ret is None:
                phi = _phi_ret(block_map.get(true_resolved))
                if phi is not None and true_label in phi[2]:
                    chosen_ret = (phi[1], phi[2][true_label])
            if chosen_ret is not None:
                new_body_lines.append(f"  ret {chosen_ret[0]} {chosen_ret[1]}\n")
            else:
                new_body_lines.append(f"  br label %{true_label}\n")
            block.lines = new_body_lines
            return blocks, True

        if cond == "false":
            chosen_ret = _direct_ret(block_map.get(false_resolved))
            if chosen_ret is None:
                phi = _phi_ret(block_map.get(false_resolved))
                if phi is not None and false_label in phi[2]:
                    chosen_ret = (phi[1], phi[2][false_label])
            if chosen_ret is not None:
                new_body_lines.append(f"  ret {chosen_ret[0]} {chosen_ret[1]}\n")
            else:
                new_body_lines.append(f"  br label %{false_label}\n")
            block.lines = new_body_lines
            return blocks, True

        true_ret = _simple_ret(block_map.get(true_resolved))
        false_ret = _simple_ret(block_map.get(false_resolved))
        true_chain = _pure_chain_ret(block_map.get(true_resolved))
        false_chain = _pure_chain_ret(block_map.get(false_resolved))
        true_unreachable = _is_trivial_unreachable(block_map.get(true_resolved))
        false_unreachable = _is_trivial_unreachable(block_map.get(false_resolved))
        if true_unreachable ^ false_unreachable:
            kept_label = false_label if true_unreachable else true_label
            kept_resolved = false_resolved if true_unreachable else true_resolved
            kept_ret = false_ret if true_unreachable else true_ret
            kept_chain = false_chain if true_unreachable else true_chain
            kept = kept_ret if kept_ret is not None else kept_chain
            assume_lines: list[str] = []
            assume_cond = cond
            if true_unreachable:
                not_temp = _unique_temp_name("assume.not", fn_text)
                assume_lines.append(f"  %{not_temp} = xor i1 {cond}, true\n")
                assume_cond = f"%{not_temp}"
            assume_lines.append(f"  call void @llvm.assume(i1 {assume_cond})\n")
            kept_phi = _phi_ret(block_map.get(kept_resolved))
            if kept is None and kept_phi is not None and block.label in kept_phi[2]:
                phi_name, kept_ty, incoming = kept_phi
                if len(incoming) == 1:
                    ready = _entry_available_phi_value(
                        block.label, kept_resolved, incoming[block.label], block_map
                    )
                    if ready is not None:
                        kept_hoisted, kept_val = ready
                        new_body_lines.extend(assume_lines)
                        new_body_lines.extend(kept_hoisted)
                        new_body_lines.append(f"  ret {kept_ty} {kept_val}\n")
                        block.lines = new_body_lines
                        return blocks, True
            kept_phi_use = _phi_single_use_chain_ret(block_map.get(kept_resolved))
            if kept is None and kept_phi_use is not None and block.label in kept_phi_use[2]:
                phi_name, kept_ty, incoming, op_lines, ret_line = kept_phi_use
                if len(incoming) == 1:
                    ready = _entry_available_phi_value(
                        block.label, kept_resolved, incoming[block.label], block_map
                    )
                    if ready is not None:
                        kept_hoisted, kept_val = ready
                        new_body_lines.extend(assume_lines)
                        new_body_lines.extend(kept_hoisted)
                        rewritten_ops = _rewrite_phi_chain_operand(op_lines, phi_name, kept_val)
                        new_body_lines.extend(f"  {line}\n" for line in rewritten_ops)
                        new_body_lines.append(f"  {ret_line}\n")
                        block.lines = new_body_lines
                        return blocks, True
            kept_block = block_map.get(kept_label)
            kept_insts = kept_block.inst_lines() if kept_block is not None else []
            kept_branch = _BR_RE.match(kept_insts[-1]) if kept_insts else None
            if kept is None and kept_branch is not None:
                merge_label = kept_branch.group("label")
                kept_phi = _phi_ret(block_map.get(merge_label))
                if kept_phi is not None and kept_label in kept_phi[2]:
                    phi_name, kept_ty, incoming = kept_phi
                    if len(incoming) == 1:
                        ready = _entry_available_phi_value(
                            kept_label, merge_label, incoming[kept_label], block_map
                        )
                        if ready is not None:
                            kept_hoisted, kept_val = ready
                            new_body_lines.extend(assume_lines)
                            new_body_lines.extend(kept_hoisted)
                            new_body_lines.append(f"  ret {kept_ty} {kept_val}\n")
                            block.lines = new_body_lines
                            return blocks, True
                kept_phi_use = _phi_single_use_chain_ret(block_map.get(merge_label))
                if kept_phi_use is not None and kept_label in kept_phi_use[2]:
                    phi_name, kept_ty, incoming, op_lines, ret_line = kept_phi_use
                    if len(incoming) == 1:
                        ready = _entry_available_phi_value(
                            kept_label, merge_label, incoming[kept_label], block_map
                        )
                        if ready is not None:
                            kept_hoisted, kept_val = ready
                            new_body_lines.extend(assume_lines)
                            new_body_lines.extend(kept_hoisted)
                            rewritten_ops = _rewrite_phi_chain_operand(op_lines, phi_name, kept_val)
                            new_body_lines.extend(f"  {line}\n" for line in rewritten_ops)
                            new_body_lines.append(f"  {ret_line}\n")
                            block.lines = new_body_lines
                            return blocks, True
            if kept is not None:
                kept_hoisted, kept_ty, kept_val = kept
                new_body_lines.extend(kept_hoisted)
                new_body_lines.extend(assume_lines)
                new_body_lines.append(f"  ret {kept_ty} {kept_val}\n")
                block.lines = new_body_lines
                return blocks, True
        if true_ret is not None and false_ret is not None:
            true_hoisted, true_ty, true_val = true_ret
            false_hoisted, false_ty, false_val = false_ret
            if true_ty != false_ty:
                continue
            true_canon = _canonicalize_hoisted_chain(true_hoisted, true_val)
            false_canon = _canonicalize_hoisted_chain(false_hoisted, false_val)
            if true_canon == false_canon:
                new_body_lines.extend(true_hoisted)
                new_body_lines.append(f"  ret {true_ty} {true_val}\n")
                block.lines = new_body_lines
                return blocks, True
            new_body_lines.extend(true_hoisted)
            new_body_lines.extend(false_hoisted)
            if (true_ty, true_val) == (false_ty, false_val):
                new_body_lines.append(f"  ret {true_ty} {true_val}\n")
            else:
                temp = _unique_temp_name("common.ret.op", fn_text)
                new_body_lines.append(
                    f"  %{temp} = select i1 {cond}, {true_ty} {true_val}, "
                    f"{false_ty} {false_val}\n"
                )
                new_body_lines.append(f"  ret {true_ty} %{temp}\n")
            block.lines = new_body_lines
            return blocks, True

        if (
            true_chain is not None
            and false_chain is not None
            and (len(true_chain[0]) > 1 or len(false_chain[0]) > 1)
        ):
            true_hoisted, true_ty, true_val = true_chain
            false_hoisted, false_ty, false_val = false_chain
            if true_ty != false_ty:
                continue
            if len(true_hoisted) <= 2 and len(false_hoisted) <= 2:
                new_body_lines.extend(true_hoisted)
                new_body_lines.extend(false_hoisted)
                if true_val == false_val:
                    new_body_lines.append(f"  ret {true_ty} {true_val}\n")
                else:
                    temp = _unique_temp_name("common.ret.op", fn_text)
                    new_body_lines.append(
                        f"  %{temp} = select i1 {cond}, {true_ty} {true_val}, "
                        f"{false_ty} {false_val}\n"
                    )
                    new_body_lines.append(f"  ret {true_ty} %{temp}\n")
                block.lines = new_body_lines
                return blocks, True
            true_cfg_block = block_map.get(true_resolved)
            false_cfg_block = block_map.get(false_resolved)
            if true_cfg_block is None or false_cfg_block is None:
                continue
            common_ret_label = _unique_block_label("common.ret", blocks)
            common_ret_value = _unique_temp_name("common.ret.op", fn_text)
            block.lines = new_body_lines + [
                f"  br i1 {cond}, label %{true_resolved}, label %{false_resolved}\n"
            ]
            true_cfg_block.lines = [*true_hoisted, f"  br label %{common_ret_label}\n"]
            false_cfg_block.lines = [*false_hoisted, f"  br label %{common_ret_label}\n"]
            block_idx = next(
                idx for idx, candidate in enumerate(blocks) if candidate.label == block.label
            )
            blocks.insert(
                block_idx + 1,
                _Block(
                    label=common_ret_label,
                    lines=[
                        f"  %{common_ret_value} = phi {true_ty} "
                        f"[ {true_val}, %{true_resolved} ], "
                        f"[ {false_val}, %{false_resolved} ]\n",
                        f"  ret {true_ty} %{common_ret_value}\n",
                    ],
                ),
            )
            return blocks, True

        for pure_label, merge_branch_label, pure_ret in (
            (true_label, false_label, true_chain if true_chain is not None else true_ret),
            (false_label, true_label, false_chain if false_chain is not None else false_ret),
        ):
            merge_branch_block = block_map.get(merge_branch_label)
            if merge_branch_block is None:
                continue
            merge_branch_insts = merge_branch_block.inst_lines()
            if not merge_branch_insts:
                continue
            merge_direct = _BR_RE.match(merge_branch_insts[-1])
            if merge_direct is None:
                phi_use = _phi_single_use_chain_ret(merge_branch_block)
                if pure_ret is not None and phi_use is not None and block.label in phi_use[2]:
                    pure_hoisted, pure_ty, pure_val = pure_ret
                    phi_name, phi_ty, incoming, op_lines, ret_line = phi_use
                    merge_ret = _RET_RE.match(ret_line)
                    if (
                        merge_ret is None
                        or merge_ret.group("ty") != pure_ty
                        or phi_ty != pure_ty
                        or len(incoming) != 1
                    ):
                        continue
                    merge_incoming_val = incoming[block.label]
                    common_ret_label = _unique_block_label("common.ret", blocks)
                    common_ret_value = _unique_temp_name("common.ret.op", fn_text)
                    pure_incoming_label = pure_label
                    pure_dest = pure_label
                    if not pure_hoisted:
                        pure_incoming_label = block.label
                        pure_dest = common_ret_label
                    block.lines = new_body_lines + [
                        "  br i1 "
                        f"{cond}, label %{(pure_dest if pure_label == true_label else merge_branch_label)}, "
                        f"label %{(pure_dest if pure_label == false_label else merge_branch_label)}\n"
                    ]
                    pure_block = block_map.get(pure_label)
                    if pure_block is None:
                        continue
                    if pure_hoisted:
                        pure_block.lines = [*pure_hoisted, f"  br label %{common_ret_label}\n"]
                    merge_branch_block.lines = [
                        f"  %{phi_name} = phi {phi_ty} [ {merge_incoming_val}, %{block.label} ]\n",
                        *[f"  {line}\n" for line in op_lines],
                        f"  br label %{common_ret_label}\n",
                    ]
                    block_idx = next(
                        idx for idx, candidate in enumerate(blocks) if candidate.label == block.label
                    )
                    if pure_label == true_label:
                        if pure_incoming_label == block.label:
                            phi_lines = [
                                f"  %{common_ret_value} = phi {pure_ty} "
                                f"[ {merge_ret.group('value')}, %{merge_branch_label} ], "
                                f"[ {pure_val}, %{pure_incoming_label} ]\n",
                                f"  ret {pure_ty} %{common_ret_value}\n",
                            ]
                        else:
                            phi_lines = [
                                f"  %{common_ret_value} = phi {pure_ty} "
                                f"[ {pure_val}, %{pure_incoming_label} ], "
                                f"[ {merge_ret.group('value')}, %{merge_branch_label} ]\n",
                                f"  ret {pure_ty} %{common_ret_value}\n",
                            ]
                    else:
                        phi_lines = [
                            f"  %{common_ret_value} = phi {pure_ty} "
                            f"[ {merge_ret.group('value')}, %{merge_branch_label} ], "
                            f"[ {pure_val}, %{pure_incoming_label} ]\n",
                            f"  ret {pure_ty} %{common_ret_value}\n",
                        ]
                    blocks.insert(
                        block_idx + 1,
                        _Block(label=common_ret_label, lines=phi_lines),
                    )
                    return blocks, True
                phi_chain = _phi_chain_ret(merge_branch_block)
                if pure_ret is not None and phi_chain is not None and block.label in phi_chain[2]:
                    pure_hoisted, pure_ty, pure_val = pure_ret
                    phi_name, phi_ty, incoming, op_lines, ret_line = phi_chain
                    merge_ret = _RET_RE.match(ret_line)
                    if (
                        merge_ret is None
                        or merge_ret.group("ty") != pure_ty
                        or phi_ty != pure_ty
                        or len(incoming) != 1
                    ):
                        continue
                    merge_incoming_val = incoming[block.label]
                    common_ret_label = _unique_block_label("common.ret", blocks)
                    common_ret_value = _unique_temp_name("common.ret.op", fn_text)
                    pure_incoming_label = pure_label
                    pure_dest = pure_label
                    if not pure_hoisted:
                        pure_incoming_label = block.label
                        pure_dest = common_ret_label
                    block.lines = new_body_lines + [
                        "  br i1 "
                        f"{cond}, label %{(pure_dest if pure_label == true_label else merge_branch_label)}, "
                        f"label %{(pure_dest if pure_label == false_label else merge_branch_label)}\n"
                    ]
                    pure_block = block_map.get(pure_label)
                    if pure_block is None:
                        continue
                    if pure_hoisted:
                        pure_block.lines = [*pure_hoisted, f"  br label %{common_ret_label}\n"]
                    merge_branch_block.lines = [
                        f"  %{phi_name} = phi {phi_ty} [ {merge_incoming_val}, %{block.label} ]\n",
                        *[f"  {line}\n" for line in op_lines],
                        f"  br label %{common_ret_label}\n",
                    ]
                    block_idx = next(
                        idx for idx, candidate in enumerate(blocks) if candidate.label == block.label
                    )
                    if pure_incoming_label == block.label:
                        phi_lines = [
                            f"  %{common_ret_value} = phi {pure_ty} "
                            f"[ {merge_ret.group('value')}, %{merge_branch_label} ], "
                            f"[ {pure_val}, %{pure_incoming_label} ]\n",
                            f"  ret {pure_ty} %{common_ret_value}\n",
                        ]
                    else:
                        phi_lines = [
                            f"  %{common_ret_value} = phi {pure_ty} "
                            f"[ {pure_val}, %{pure_incoming_label} ], "
                            f"[ {merge_ret.group('value')}, %{merge_branch_label} ]\n",
                            f"  ret {pure_ty} %{common_ret_value}\n",
                        ]
                    blocks.insert(
                        block_idx + 1,
                        _Block(label=common_ret_label, lines=phi_lines),
                    )
                    return blocks, True
                continue
            merge_label = merge_direct.group("label")
            phi = _phi_ret(block_map.get(merge_label))
            if pure_ret is not None and phi is not None and merge_branch_label in phi[2]:
                pure_hoisted, pure_ty, pure_val = pure_ret
                phi_name, ty, incoming = phi
                if ty != pure_ty:
                    continue
                merge_ready = _entry_available_phi_value(
                    merge_branch_label, merge_label, incoming[merge_branch_label], block_map
                )
                if len(incoming) == 1 and not pure_hoisted and merge_ready == ([], incoming[merge_branch_label]):
                    merge_block = block_map.get(merge_label)
                    if merge_block is None:
                        continue
                    merge_val = incoming[merge_branch_label]
                    common_ret_label = _unique_block_label("common.ret", blocks)
                    common_ret_value = _unique_temp_name("common.ret.op", fn_text)
                    true_dest = common_ret_label if pure_label == true_label else merge_label
                    false_dest = common_ret_label if pure_label == false_label else merge_label
                    block.lines = new_body_lines + [
                        f"  br i1 {cond}, label %{true_dest}, label %{false_dest}\n"
                    ]
                    merge_block.lines = [
                        f"  %{phi_name} = phi {ty} [ {merge_val}, %{block.label} ]\n",
                        f"  br label %{common_ret_label}\n",
                    ]
                    block_idx = next(
                        idx for idx, candidate in enumerate(blocks) if candidate.label == block.label
                    )
                    incoming_true = pure_val if pure_label == true_label else f"%{phi_name}"
                    incoming_false = pure_val if pure_label == false_label else f"%{phi_name}"
                    blocks.insert(
                        block_idx + 1,
                        _Block(
                            label=common_ret_label,
                            lines=[
                                f"  %{common_ret_value} = phi {ty} "
                                f"[ %{phi_name}, %{merge_label} ], "
                                f"[ {pure_val}, %{block.label} ]\n",
                                f"  ret {ty} %{common_ret_value}\n",
                            ],
                        ),
                    )
                    return blocks, True
                if merge_ready is None:
                    continue
                hoisted_merge, merge_val = merge_ready
                true_val = pure_val if pure_label == true_label else merge_val
                false_val = pure_val if pure_label == false_label else merge_val
                new_body_lines.extend(pure_hoisted)
                new_body_lines.extend(hoisted_merge)
                if true_val == false_val:
                    new_body_lines.append(f"  ret {ty} {true_val}\n")
                else:
                    temp = _unique_temp_name("common.ret.op", fn_text)
                    new_body_lines.append(
                        f"  %{temp} = select i1 {cond}, {ty} {true_val}, {ty} {false_val}\n"
                    )
                    new_body_lines.append(f"  ret {ty} %{temp}\n")
                block.lines = new_body_lines
                return blocks, True
            phi_use = _phi_single_use_chain_ret(block_map.get(merge_label))
            if pure_ret is not None and phi_use is not None and block.label in phi_use[2]:
                pure_hoisted, pure_ty, pure_val = pure_ret
                phi_name, phi_ty, incoming, op_lines, ret_line = phi_use
                merge_ret = _RET_RE.match(ret_line)
                if (
                    merge_ret is None
                    or merge_ret.group("ty") != pure_ty
                    or phi_ty != pure_ty
                    or len(incoming) != 1
                ):
                    continue
                merge_incoming_val = incoming[block.label]
                merge_ready = _entry_available_phi_value(
                    block.label, merge_label, merge_incoming_val, block_map
                )
                if merge_ready is None:
                    continue
                if merge_ready != ([], merge_incoming_val):
                    continue
                merge_block = block_map.get(merge_label)
                if merge_block is None:
                    continue
                common_ret_label = _unique_block_label("common.ret", blocks)
                common_ret_value = _unique_temp_name("common.ret.op", fn_text)
                pure_incoming_label = pure_label
                pure_dest = pure_label
                if not pure_hoisted:
                    pure_incoming_label = block.label
                    pure_dest = common_ret_label
                block.lines = new_body_lines + [
                    "  br i1 "
                    f"{cond}, label %{(pure_dest if pure_label == true_label else merge_label)}, "
                    f"label %{(pure_dest if pure_label == false_label else merge_label)}\n"
                ]
                if pure_hoisted:
                    pure_block = block_map.get(pure_label)
                    if pure_block is None:
                        continue
                    pure_block.lines = [*pure_hoisted, f"  br label %{common_ret_label}\n"]
                merge_block.lines = [
                    f"  %{phi_name} = phi {phi_ty} [ {merge_incoming_val}, %{block.label} ]\n",
                    *[f"  {line}\n" for line in op_lines],
                    f"  br label %{common_ret_label}\n",
                ]
                block_idx = next(
                    idx for idx, candidate in enumerate(blocks) if candidate.label == block.label
                )
                if pure_incoming_label == block.label:
                    phi_lines = [
                        f"  %{common_ret_value} = phi {pure_ty} "
                        f"[ {merge_ret.group('value')}, %{merge_label} ], "
                        f"[ {pure_val}, %{pure_incoming_label} ]\n",
                        f"  ret {pure_ty} %{common_ret_value}\n",
                    ]
                else:
                    phi_lines = [
                        f"  %{common_ret_value} = phi {pure_ty} "
                        f"[ {pure_val}, %{pure_incoming_label} ], "
                        f"[ {merge_ret.group('value')}, %{merge_label} ]\n",
                        f"  ret {pure_ty} %{common_ret_value}\n",
                    ]
                blocks.insert(
                    block_idx + 1,
                    _Block(label=common_ret_label, lines=phi_lines),
                )
                return blocks, True
            phi_chain = _phi_chain_ret(block_map.get(merge_label))
            if (
                pure_ret is not None
                and phi_use is None
                and phi_chain is not None
                and merge_branch_label in phi_chain[2]
            ):
                pure_hoisted, pure_ty, pure_val = pure_ret
                phi_name, phi_ty, incoming, op_lines, ret_line = phi_chain
                merge_ret = _RET_RE.match(ret_line)
                if (
                    merge_ret is None
                    or merge_ret.group("ty") != pure_ty
                    or phi_ty != pure_ty
                    or len(incoming) != 1
                ):
                    continue
                merge_incoming_val = incoming[merge_branch_label]
                merge_ready = _entry_available_phi_value(
                    merge_branch_label, merge_label, merge_incoming_val, block_map
                )
                if merge_ready is None:
                    continue
                hoisted_merge, merge_val = merge_ready
                rewritten_ops = _rewrite_phi_chain_operand(op_lines, phi_name, merge_val)
                merge_result_val = merge_ret.group("value")
                if merge_ready == ([], merge_incoming_val) and _ssa_use_count(op_lines, phi_name) > 1:
                    common_ret_label = _unique_block_label("common.ret", blocks)
                    common_ret_value = _unique_temp_name("common.ret.op", fn_text)
                    pure_incoming_label = pure_label
                    pure_dest = pure_label
                    if not pure_hoisted:
                        pure_incoming_label = block.label
                        pure_dest = common_ret_label
                    block.lines = new_body_lines + [
                        "  br i1 "
                        f"{cond}, label %{(pure_dest if pure_label == true_label else merge_label)}, "
                        f"label %{(pure_dest if pure_label == false_label else merge_label)}\n"
                    ]
                    if pure_hoisted:
                        pure_block = block_map.get(pure_label)
                        if pure_block is None:
                            continue
                        pure_block.lines = [*pure_hoisted, f"  br label %{common_ret_label}\n"]
                    merge_block = block_map.get(merge_label)
                    if merge_block is None:
                        continue
                    merge_block.lines = [
                        f"  %{phi_name} = phi {phi_ty} [ {merge_incoming_val}, %{block.label} ]\n",
                        *[f"  {line}\n" for line in op_lines],
                        f"  br label %{common_ret_label}\n",
                    ]
                    block_idx = next(
                        idx for idx, candidate in enumerate(blocks) if candidate.label == block.label
                    )
                    phi_lines = (
                        [
                            f"  %{common_ret_value} = phi {pure_ty} "
                            f"[ {merge_ret.group('value')}, %{merge_label} ], "
                            f"[ {pure_val}, %{pure_incoming_label} ]\n",
                            f"  ret {pure_ty} %{common_ret_value}\n",
                        ]
                        if pure_incoming_label == block.label
                        else [
                            f"  %{common_ret_value} = phi {pure_ty} "
                            f"[ {pure_val}, %{pure_incoming_label} ], "
                            f"[ {merge_ret.group('value')}, %{merge_label} ]\n",
                            f"  ret {pure_ty} %{common_ret_value}\n",
                        ]
                    )
                    blocks.insert(block_idx + 1, _Block(label=common_ret_label, lines=phi_lines))
                    return blocks, True
                if (
                    merge_ready != ([], merge_incoming_val)
                    and ((not pure_hoisted) or (len(hoisted_merge) + len(op_lines) <= 3))
                ):
                    new_body_lines.extend(pure_hoisted)
                    new_body_lines.extend(hoisted_merge)
                    new_body_lines.extend(f"  {line}\n" for line in rewritten_ops)
                    if pure_label == true_label:
                        true_val, false_val = pure_val, merge_result_val
                    else:
                        true_val, false_val = merge_result_val, pure_val
                    if true_val != false_val:
                        temp = _unique_temp_name("common.ret.op", fn_text)
                        new_body_lines.append(
                            f"  %{temp} = select i1 {cond}, {pure_ty} {true_val}, {pure_ty} {false_val}\n"
                        )
                        new_body_lines.append(f"  ret {pure_ty} %{temp}\n")
                    else:
                        new_body_lines.append(f"  ret {pure_ty} {true_val}\n")
                    block.lines = new_body_lines
                    return blocks, True

                merge_branch_block = block_map.get(merge_branch_label)
                pure_block = block_map.get(pure_label)
                if merge_branch_block is None or pure_block is None:
                    continue
                common_ret_label = _unique_block_label("common.ret", blocks)
                common_ret_value = _unique_temp_name("common.ret.op", fn_text)
                block.lines = new_body_lines + [
                    "  br i1 "
                    f"{cond}, label %{true_label}, label %{false_label}\n"
                ]
                pure_block.lines = [*pure_hoisted, f"  br label %{common_ret_label}\n"]
                merge_branch_block.lines = [
                    *hoisted_merge,
                    *[f"  {line}\n" for line in rewritten_ops],
                    f"  br label %{common_ret_label}\n",
                ]
                block_idx = next(
                    idx for idx, candidate in enumerate(blocks) if candidate.label == block.label
                )
                if pure_label == true_label:
                    phi_lines = [
                        f"  %{common_ret_value} = phi {pure_ty} "
                        f"[ {pure_val}, %{pure_label} ], "
                        f"[ {merge_result_val}, %{merge_branch_label} ]\n",
                        f"  ret {pure_ty} %{common_ret_value}\n",
                    ]
                else:
                    phi_lines = [
                        f"  %{common_ret_value} = phi {pure_ty} "
                        f"[ {merge_result_val}, %{merge_branch_label} ], "
                        f"[ {pure_val}, %{pure_label} ]\n",
                        f"  ret {pure_ty} %{common_ret_value}\n",
                    ]
                blocks.insert(block_idx + 1, _Block(label=common_ret_label, lines=phi_lines))
                return blocks, True
            phi_use = _phi_single_use_chain_ret(block_map.get(merge_label))
            pure_block = block_map.get(pure_label)
            merge_block = block_map.get(merge_label)
            pure_simple = _pure_chain_ret(pure_block)
            if (
                phi_use is not None
                and pure_block is not None
                and merge_block is not None
                and pure_simple is not None
                and len(phi_use[2]) == 1
                and merge_branch_label in phi_use[2]
            ):
                pure_hoisted, pure_ty, pure_val = pure_simple
                phi_name, phi_ty, incoming, op_lines, ret_line = phi_use
                merge_ret = _RET_RE.match(ret_line)
                if merge_ret is None or merge_ret.group("ty") != pure_ty or phi_ty != pure_ty:
                    continue
                merge_incoming_val = incoming[merge_branch_label]
                merge_ready = _entry_available_phi_value(
                    merge_branch_label, merge_label, merge_incoming_val, block_map
                )
                if merge_ready is None:
                    continue
                if merge_ready != ([], merge_incoming_val):
                    hoisted_merge, merge_val = merge_ready
                    rewritten_ops = _rewrite_phi_chain_operand(op_lines, phi_name, merge_val)
                    if len(pure_hoisted) <= 3 and len(hoisted_merge) <= 3:
                        new_body_lines.extend(pure_hoisted)
                        new_body_lines.extend(hoisted_merge)
                        new_body_lines.extend(f"  {line}\n" for line in rewritten_ops)
                        merge_result_val = merge_ret.group("value")
                        if pure_val == merge_result_val:
                            new_body_lines.append(f"  ret {pure_ty} {pure_val}\n")
                        else:
                            temp = _unique_temp_name("common.ret.op", fn_text)
                            new_body_lines.append(
                                f"  %{temp} = select i1 {cond}, {pure_ty} {pure_val}, {pure_ty} {merge_result_val}\n"
                            )
                            new_body_lines.append(f"  ret {pure_ty} %{temp}\n")
                        block.lines = new_body_lines
                        return blocks, True
                    common_ret_label = _unique_block_label("common.ret", blocks)
                    common_ret_value = _unique_temp_name("common.ret.op", fn_text)
                    pure_incoming_label = pure_label
                    pure_dest = pure_label
                    if not pure_hoisted:
                        pure_incoming_label = block.label
                        pure_dest = common_ret_label
                    block.lines = new_body_lines + [
                        "  br i1 "
                        f"{cond}, label %{(pure_dest if pure_label == true_label else merge_branch_label)}, "
                        f"label %{(pure_dest if pure_label == false_label else merge_branch_label)}\n"
                    ]
                    if pure_hoisted:
                        pure_block.lines = [*pure_hoisted, f"  br label %{common_ret_label}\n"]
                    merge_branch_block.lines = [
                        *hoisted_merge,
                        *[f"  {line}\n" for line in rewritten_ops],
                        f"  br label %{common_ret_label}\n",
                    ]
                    block_idx = next(
                        idx
                        for idx, candidate in enumerate(blocks)
                        if candidate.label == block.label
                    )
                    blocks.insert(
                        block_idx + 1,
                        _Block(
                            label=common_ret_label,
                            lines=[
                                (
                                    f"  %{common_ret_value} = phi {pure_ty} "
                                    f"[ {merge_ret.group('value')}, %{merge_branch_label} ], "
                                    f"[ {pure_val}, %{pure_incoming_label} ]\n"
                                    if pure_incoming_label == block.label
                                    else
                                    f"  %{common_ret_value} = phi {pure_ty} "
                                    f"[ {pure_val}, %{pure_incoming_label} ], "
                                    f"[ {merge_ret.group('value')}, %{merge_branch_label} ]\n"
                                ),
                                f"  ret {pure_ty} %{common_ret_value}\n",
                            ],
                        ),
                    )
                    return blocks, True
                common_ret_label = _unique_block_label("common.ret", blocks)
                common_ret_value = _unique_temp_name("common.ret.op", fn_text)
                pure_incoming_label = pure_label
                pure_dest = pure_label
                if not pure_hoisted:
                    pure_incoming_label = block.label
                    pure_dest = common_ret_label
                block.lines = new_body_lines + [
                    "  br i1 "
                    f"{cond}, label %{(pure_dest if pure_label == true_label else merge_label)}, "
                    f"label %{(pure_dest if pure_label == false_label else merge_label)}\n"
                ]
                if pure_hoisted:
                    pure_block.lines = [*pure_hoisted, f"  br label %{common_ret_label}\n"]
                merge_block.lines = [
                    f"  %{phi_name} = phi {phi_ty} [ {merge_incoming_val}, %{block.label} ]\n",
                    *[f"  {line}\n" for line in op_lines],
                    f"  br label %{common_ret_label}\n",
                ]
                block_idx = next(
                    idx for idx, candidate in enumerate(blocks) if candidate.label == block.label
                )
                blocks.insert(
                    block_idx + 1,
                    _Block(
                        label=common_ret_label,
                        lines=[
                            (
                                f"  %{common_ret_value} = phi {pure_ty} "
                                f"[ {merge_ret.group('value')}, %{merge_label} ], "
                                f"[ {pure_val}, %{pure_incoming_label} ]\n"
                                if pure_incoming_label == block.label
                                else
                                f"  %{common_ret_value} = phi {pure_ty} "
                                f"[ {pure_val}, %{pure_incoming_label} ], "
                                f"[ {merge_ret.group('value')}, %{merge_label} ]\n"
                            ),
                            f"  ret {pure_ty} %{common_ret_value}\n",
                        ],
                    ),
                )
                return blocks, True

        for pure_label, merge_label in ((true_label, false_label), (false_label, true_label)):
            pure_block = block_map.get(pure_label)
            merge_block = block_map.get(merge_label)
            if pure_block is None or merge_block is None:
                continue
            pure_insts = pure_block.inst_lines()
            if not pure_insts:
                continue
            pure_term = _BR_RE.match(pure_insts[-1])
            if pure_term is None or pure_term.group("label") != merge_label:
                continue
            phi = _phi_ret(merge_block)
            phi_use = _phi_single_use_chain_ret(merge_block)
            if phi is not None:
                phi_name, ty, incoming = phi
                if pure_label not in incoming or block.label not in incoming:
                    continue
                pure_ready = _entry_available_phi_value(
                    pure_label, merge_label, incoming[pure_label], block_map
                )
                if pure_ready is None:
                    continue
                hoisted_pure, pure_val = pure_ready
                entry_val = incoming[block.label]
                true_val = pure_val if pure_label == true_label else entry_val
                false_val = pure_val if pure_label == false_label else entry_val
                new_body_lines.extend(hoisted_pure)
                if true_val != false_val:
                    new_body_lines.append(
                        f"  %{phi_name} = select i1 {cond}, {ty} {true_val}, {ty} {false_val}\n"
                    )
                    new_body_lines.append(f"  ret {ty} %{phi_name}\n")
                else:
                    new_body_lines.append(f"  ret {ty} {true_val}\n")
                block.lines = new_body_lines
                return blocks, True
            if phi_use is not None:
                phi_name, use_ty, incoming, op_lines, ret_line = phi_use
                if pure_label not in incoming or block.label not in incoming:
                    continue
                pure_ready = _entry_available_phi_value(
                    pure_label, merge_label, incoming[pure_label], block_map
                )
                if pure_ready is None:
                    continue
                hoisted_pure, pure_val = pure_ready
                entry_val = incoming[block.label]
                true_val = pure_val if pure_label == true_label else entry_val
                false_val = pure_val if pure_label == false_label else entry_val
                new_body_lines.extend(hoisted_pure)
                if true_val != false_val:
                    new_body_lines.append(
                        f"  %{phi_name} = select i1 {cond}, {use_ty} {true_val}, {use_ty} {false_val}\n"
                    )
                    rewritten_ops = op_lines
                else:
                    rewritten_ops = _rewrite_phi_chain_operand(op_lines, phi_name, true_val)
                new_body_lines.extend(f"  {line}\n" for line in rewritten_ops)
                new_body_lines.append(f"  {ret_line}\n")
                block.lines = new_body_lines
                return blocks, True

        true_direct = _BR_RE.match(true_block.inst_lines()[-1]) if true_block.inst_lines() else None
        false_direct = _BR_RE.match(false_block.inst_lines()[-1]) if false_block.inst_lines() else None
        if (
            true_direct is not None
            and false_direct is not None
            and true_direct.group("label") == false_direct.group("label")
        ):
            merge_label = true_direct.group("label")
            phi = _phi_ret(block_map.get(merge_label))
            if phi is not None and true_label in phi[2] and false_label in phi[2]:
                phi_name, ty, incoming = phi
                true_ready = _entry_available_phi_value(
                    true_label, merge_label, incoming[true_label], block_map
                )
                false_ready = _entry_available_phi_value(
                    false_label, merge_label, incoming[false_label], block_map
                )
                if true_ready is not None and false_ready is not None:
                    hoisted_true, true_val = true_ready
                    hoisted_false, false_val = false_ready
                    if len(hoisted_true) > 2 or len(hoisted_false) > 2:
                        continue
                    new_body_lines.extend(hoisted_true)
                    new_body_lines.extend(hoisted_false)
                    if true_val == false_val:
                        new_body_lines.append(f"  ret {ty} {true_val}\n")
                    else:
                        new_body_lines.append(
                            f"  %{phi_name} = select i1 {cond}, {ty} {true_val}, {ty} {false_val}\n"
                        )
                        new_body_lines.append(f"  ret {ty} %{phi_name}\n")
                    block.lines = new_body_lines
                    return blocks, True
            phi_use = _phi_single_use_chain_ret(block_map.get(merge_label))
            if phi_use is not None and true_label in phi_use[2] and false_label in phi_use[2]:
                phi_name, ty, incoming, op_lines, ret_line = phi_use
                true_ready = _entry_available_phi_value(
                    true_label, merge_label, incoming[true_label], block_map
                )
                false_ready = _entry_available_phi_value(
                    false_label, merge_label, incoming[false_label], block_map
                )
                if true_ready is not None and false_ready is not None:
                    hoisted_true, true_val = true_ready
                    hoisted_false, false_val = false_ready
                    if len(hoisted_true) > 2 or len(hoisted_false) > 2:
                        continue
                    new_body_lines.extend(hoisted_true)
                    new_body_lines.extend(hoisted_false)
                    if true_val != false_val:
                        new_body_lines.append(
                            f"  %{phi_name} = select i1 {cond}, {ty} {true_val}, {ty} {false_val}\n"
                        )
                        rewritten_ops = op_lines
                    else:
                        rewritten_ops = _rewrite_phi_chain_operand(op_lines, phi_name, true_val)
                    new_body_lines.extend(f"  {line}\n" for line in rewritten_ops)
                    new_body_lines.append(f"  {ret_line}\n")
                    block.lines = new_body_lines
                    return blocks, True

        if true_resolved == false_resolved:
            phi = _phi_ret(block_map.get(true_resolved))
            if phi is not None and true_label in phi[2] and false_label in phi[2]:
                phi_name, ty, incoming = phi
                true_ready = _entry_available_phi_value(
                    true_label, true_resolved, incoming[true_label], block_map
                )
                false_ready = _entry_available_phi_value(
                    false_label, false_resolved, incoming[false_label], block_map
                )
                if true_ready is None or false_ready is None:
                    continue
                hoisted_true, true_val = true_ready
                hoisted_false, false_val = false_ready
                new_body_lines.extend(hoisted_true)
                new_body_lines.extend(hoisted_false)
                if true_val == false_val:
                    new_body_lines.append(f"  ret {ty} {true_val}\n")
                else:
                    new_body_lines.append(
                        f"  %{phi_name} = select i1 {cond}, {ty} {true_val}, {ty} {false_val}\n"
                    )
                    new_body_lines.append(f"  ret {ty} %{phi_name}\n")
            else:
                phi_use = _phi_single_use_chain_ret(block_map.get(true_resolved))
                if phi_use is not None and true_label in phi_use[2] and false_label in phi_use[2]:
                    phi_name, ty, incoming, op_lines, ret_line = phi_use
                    true_val = incoming[true_label]
                    false_val = incoming[false_label]
                    if true_val != false_val:
                        new_body_lines.append(
                            f"  %{phi_name} = select i1 {cond}, {ty} {true_val}, {ty} {false_val}\n"
                        )
                        rewritten_ops = op_lines
                    else:
                        rewritten_ops = _rewrite_phi_chain_operand(op_lines, phi_name, true_val)
                    new_body_lines.extend(f"  {line}\n" for line in rewritten_ops)
                    new_body_lines.append(f"  {ret_line}\n")
                else:
                    resolved_block = block_map.get(true_resolved)
                    has_phi = (
                        resolved_block is not None
                        and any(" = phi " in inst for inst in resolved_block.inst_lines())
                    )
                    if has_phi:
                        continue
                    new_body_lines.append(f"  br label %{true_resolved}\n")
            block.lines = new_body_lines
            return blocks, True

    return blocks, False


def simplify_cfg_text(ir_text: str) -> tuple[str, bool]:
    """Apply a focused local SimplifyCFG subset across all functions."""
    out: list[str] = []
    changed = False
    function_attrs = _collect_function_attrs(ir_text)
    for is_function, chunk in _split_functions(ir_text):
        if not is_function:
            out.append(chunk)
            continue
        header, blocks, footer = _parse_blocks(chunk)
        if not blocks:
            out.append(chunk)
            continue
        fn_changed = False
        for _ in range(8):
            local_changed = False
            fn_text = _join_function(header, blocks, footer)
            blocks, c1 = _rewrite_simple_conditional_blocks(blocks, fn_text)
            local_changed = local_changed or c1
            blocks, c2 = _drop_unreachable_blocks(blocks)
            local_changed = local_changed or c2
            blocks, c3 = _prune_invalid_phi_incomings(blocks)
            local_changed = local_changed or c3
            blocks, c4 = _merge_linear_successors(blocks)
            local_changed = local_changed or c4
            if not local_changed:
                break
            fn_changed = True
        current = _join_function(header, blocks, footer)
        if fn_changed:
            current = _cleanup_function_locally(current, function_attrs=function_attrs)
        out.append(current)
        changed = changed or fn_changed
    if not changed:
        return ir_text, False
    new_text = "".join(out)
    assume_decl = "declare void @llvm.assume(i1)\n"
    if "@llvm.assume(" in new_text and assume_decl not in new_text:
        chunks = _split_functions(new_text)
        rebuilt: list[str] = []
        inserted = False
        for is_function, chunk in chunks:
            if is_function and not inserted:
                rebuilt.append(assume_decl)
                inserted = True
            rebuilt.append(chunk)
        if not inserted:
            rebuilt.append(assume_decl)
        new_text = "".join(rebuilt)
    return new_text, True
