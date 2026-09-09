"""Function Inlining — IR-level (subset).

Upstream reference:

- ``/tmp/llvm-src/llvm-20.1.8.src/lib/Transforms/IPO/Inliner.cpp``
  implements :cpp:class:`llvm::InlinerPass`. It works on the CGSCC
  pass manager, uses a cost model
  (``/tmp/llvm-src/llvm-20.1.8.src/lib/Analysis/InlineCost.cpp``),
  and clones callee bodies into callers with SSA remapping.

Subset implemented here (labelled ``subset``):

- Inline only callees that are (a) internal linkage, (b) single
  basic block, (c) end in a ``ret`` of a single-value expression,
  or ``ret void``.
- Also inline one small multi-block shape: a tiny single-exit callee
  into a caller block that is exactly ``call`` followed by ``ret``.
  This covers the common "diamond + phi + ret" case without needing a
  full general-purpose CFG splicer.
- Replace the call site with the cloned body, rewriting operands so
  ``%x`` → the actual argument value, and the final ``ret %val``
  becomes an assignment of ``%val`` to the call's result name.

With include_definitions enabled, bounded alloca-free definitions also
support ordinary call-site splitting, multiple returns and successor-PHI
repair. Expansion uses immutable callee templates and does not recursively
inline cloned call sites. Larger bodies and unsupported ABI shapes stay calls.
"""

from __future__ import annotations

import re


from .ir_mutator import BasicBlock, MutableModule
from .instsimplify import simplify_module_text
from .text_tokens import replace_local_names






# ---------------------------------------------------------------------------
# Inliner kernel
# ---------------------------------------------------------------------------


def inline_module(
    ir_text: str,
    *,
    require_alwaysinline: bool = False,
    include_definitions: bool = False,
) -> tuple[str, bool]:
    module = MutableModule.parse(ir_text)

    attr_groups = _parse_attr_groups(ir_text)
    semantic_interposition = "SemanticInterposition" in ir_text
    addressed = set(re.findall(r"\bblockaddress\s*\(\s*@([\w.$-]+)\s*,", ir_text))
    # Find candidate internal single-block callees.
    candidates: dict[str, dict] = {}
    for fn in module.functions:
        header = _first_define_line(fn)
        if not header:
            continue
        header_words = header.split()
        is_internal = "internal" in header_words or "private" in header_words
        attributes = fn.trailing
        group = re.search(r"#(?P<id>\d+)\b", header)
        if group is not None:
            attributes += " " + attr_groups.get(group.group("id"), "")
        if "noinline" in attributes or "optnone" in attributes or "loader-replaceable" in attributes:
            continue
        if re.search(r"\b(?:\w+cc|cc\s+\d+)\b", header.split("@", 1)[0]):
            continue
        is_alwaysinline = _header_has_alwaysinline(header, attr_groups)
        if require_alwaysinline:
            if not is_alwaysinline:
                continue
        elif not is_internal:
            if not include_definitions:
                continue
            # Follow the IR's replacement boundary, not symbol visibility.
            # Strong external definitions may be inlined but must remain
            # exported. Interposable/ODR variants stay outside this subset.
            if any(word in header_words for word in (
                "weak", "weak_odr", "linkonce", "linkonce_odr", "extern_weak",
                "available_externally", "common", "appending", "dllimport",
            )):
                continue
            if semantic_interposition and "dso_local" not in header_words:
                continue
        blocks = list(fn.blocks)
        body_text = "".join(block.serialize() for block in blocks)
        if fn.name in addressed or any(word in body_text for word in (
            "indirectbr ", "callbr ", "invoke ", "landingpad ",
            "catchswitch ", "catchpad ", "cleanuppad ", "blockaddress(",
        )):
            continue
        if include_definitions:
            instruction_count = sum(len(block.instructions) for block in blocks)
            if instruction_count > 32:
                continue
            if any(word in body_text for word in (
                "alloca ", "musttail ", "invoke ", "callbr ", "blockaddress(",
                "@llvm.returnaddress", "@llvm.frameaddress", "@llvm.va_start",
            )) or ("@" + fn.name + "(") in body_text:
                continue

        arg_names = []
        arg_types = []
        ok_args = True
        for arg in fn.args:
            am = re.match(r"(\S+)\s+%([\w\.]+)", arg.serialize().strip())
            if not am:
                ok_args = False
                break
            arg_names.append(am.group(2))
            arg_types.append(am.group(1))
        if not ok_args:
            continue

        body_lines: list[str] = []
        ret_void = False
        ret_val = None
        total_non_terms = 0
        exit_blocks = [b for b in blocks if list(b.instructions) and list(b.instructions)[-1].opcode == "ret"]
        if len(blocks) == 1:
            body_insts = list(blocks[0].instructions)
            if not body_insts:
                continue
            term = body_insts[-1]
            if term.opcode != "ret":
                continue
            term_text = term.text.strip()
            ret_void = term_text == "ret void"
            if not ret_void:
                ret_m = re.match(r"\s*ret\s+[^%@\s]+\s*(.+?)\s*$", term_text)
                if not ret_m:
                    continue
                ret_val = ret_m.group(1).strip()
            for inst in body_insts[:-1]:
                body_lines.append(inst.text.strip())
        else:
            total_non_terms = sum(max(len(list(b.instructions)) - 1, 0) for b in blocks)
            multi_ret_only = len(exit_blocks) == 2 and total_non_terms == 0 and len(blocks) == 3
            if include_definitions:
                if not exit_blocks or any(
                    not re.fullmatch(r"ret (?:void|\S+ \S+)", b.terminator.text.strip())
                    for b in exit_blocks
                ):
                    continue
            else:
                if len(exit_blocks) != 1 and not multi_ret_only:
                    continue
                if total_non_terms > 6:
                    continue
            if len(exit_blocks) == 1:
                exit_term = list(exit_blocks[0].instructions)[-1]
                term_text = exit_term.text.strip()
                ret_void = term_text == "ret void"
                if not ret_void:
                    ret_m = re.match(r"\s*ret\s+[^%@\s]+\s*(.+?)\s*$", term_text)
                    if not ret_m:
                        continue
                    ret_val = ret_m.group(1).strip()
            elif len(exit_blocks) == 2:
                exit_terms = [list(b.instructions)[-1].text.strip() for b in exit_blocks]
                if all(text == "ret void" for text in exit_terms):
                    ret_void = True
                elif not all(re.match(r"ret\s+[^%@\s]+\s+.+$", text) for text in exit_terms):
                    continue
        candidates[fn.name] = {
            "args": arg_names,
            "arg_types": arg_types,
            "return_type": header.split("@", 1)[0].split()[-1],
            "body": body_lines,
            "ret_val": ret_val,
            "ret_void": ret_void,
            "internal": is_internal,
            "single_block": len(blocks) == 1,
        }

    if not candidates:
        return ir_text, False

    # Iterate call sites and inline where the callee is a candidate.
    new_text = _inline_calls(ir_text, candidates)
    if include_definitions:
        new_text = _inline_multiblock_call_sites(new_text, candidates)
    else:
        new_text = _inline_multiblock_return_callers(new_text, candidates)
    changed = new_text != ir_text
    if not changed:
        return ir_text, False
    new_text, _ = simplify_module_text(new_text)
    new_text = _drop_dead_internal_callees(new_text, candidates)
    return new_text, True


def _first_define_line(fn) -> str | None:
    s = fn.header_line
    for line in s.splitlines():
        if line.lstrip().startswith("define"):
            return line
    return None


def _parse_attr_groups(ir_text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in ir_text.splitlines():
        m = re.match(r"^attributes\s+#(?P<id>\d+)\s*=\s*\{(?P<body>[^}]*)\}", line.strip())
        if m:
            out[m.group("id")] = m.group("body")
    return out


def _header_has_alwaysinline(header: str, attr_groups: dict[str, str]) -> bool:
    if " alwaysinline" in f" {header} ":
        return True
    m = re.search(r"#(?P<id>\d+)\b", header)
    if not m:
        return False
    return "alwaysinline" in attr_groups.get(m.group("id"), "")


_PASSTHROUGH_RET_TYPES = {
    "ptr",
    "half",
    "bfloat",
    "float",
    "double",
    "fp128",
    "x86_fp80",
    "ppc_fp128",
}


_CALL_RE_TEMPLATE = (
    r"^(?P<indent>\s*)%(?P<res>[\w\.]+)\s*=\s*"
    r"(?:tail\s+|musttail\s+|notail\s+)?call\s+"
    r"[^@]*@(?P<callee>{callee})\s*\((?P<args>[^)]*)\)\s*$"
)
_VOID_CALL_RE_TEMPLATE = (
    r"^(?P<indent>\s*)"
    r"(?:(?:tail|musttail|notail)\s+)?call\s+void\s+"
    r"@(?P<callee>{callee})\s*\((?P<args>[^)]*)\)\s*$"
)
_NOOP_GEP_RE = re.compile(
    r"^\s*%(?P<res>[\w\.]+)\s*=\s*getelementptr\s+[^,]+,\s+ptr\s+(?P<base>%[\w\.]+)\s*,\s+i\d+\s+0\s*$"
)
_BARE_CALL_RE_TEMPLATE = (
    r"^(?P<indent>\s*)"
    r"(?:(?:tail|musttail|notail)\s+)?call\s+"
    r"(?P<ret>[^@%\s]+)\s+@(?P<callee>{callee})\s*\((?P<args>[^)]*)\)\s*$"
)

_DIRECT_CALLEE_RE = re.compile(r"\bcall\b[^@]*@(?P<callee>[\w.$]+)\s*\(")
_VALUE_CALL_RE = re.compile(_CALL_RE_TEMPLATE.format(callee=r"[\w.$]+"))
_VOID_CALL_RE = re.compile(_VOID_CALL_RE_TEMPLATE.format(callee=r"[\w.$]+"))
_BARE_CALL_RE = re.compile(_BARE_CALL_RE_TEMPLATE.format(callee=r"[\w.$]+"))


def _call_signature_matches(text: str, actuals_raw: list[str], info: dict) -> bool:
    # Opaque-pointer IR can call a definition through a different signature.
    # Such a call is an ABI boundary, not a valid SSA substitution. This
    # subset also leaves calling-convention/return-attribute forms untouched.
    if "musttail" in text or "notail" in text:
        return False
    signature = re.search(r"\bcall\s+(\S+)\s+@", text)
    if signature is None or signature.group(1) != info["return_type"]:
        return False
    actual_types = []
    for raw in actuals_raw:
        parts = raw.split()
        if len(parts) != 2:
            return False
        actual_types.append(parts[0])
    return actual_types == info["arg_types"]


def _inline_calls(ir_text: str, candidates: dict[str, dict]) -> str:
    chunks = _split_functions(ir_text)
    out: list[str] = []
    counter = 0

    for is_function, chunk in chunks:
        if not is_function:
            out.append(chunk)
            continue
        rewritten, counter = _inline_calls_in_function(chunk, candidates, counter)
        out.append(rewritten)

    return "".join(out)


def _inline_calls_in_function(
    fn_text: str,
    candidates: dict[str, dict],
    counter: int,
) -> tuple[str, int]:
    lines = fn_text.splitlines(keepends=True)
    out: list[str] = []
    value_replacements: dict[str, str] = {}

    for line in lines:
        stripped = _apply_value_replacements(line.rstrip("\n"), value_replacements)
        if "call" not in stripped:
            out.append(stripped + "\n")
            continue
        direct_call = _DIRECT_CALLEE_RE.search(stripped)
        if direct_call is None:
            out.append(stripped + "\n")
            continue
        selected_name = direct_call.group("callee")
        if selected_name not in candidates:
            out.append(stripped + "\n")
            continue
        inlined_any = False
        # A direct call has exactly one candidate. Do not compile three
        # patterns for every (instruction, function) pair in the module.
        for callee_name in (selected_name,):
            info = candidates[callee_name]
            if not info.get("single_block", False):
                continue
            m = _VALUE_CALL_RE.match(stripped)
            is_void_call = False
            is_bare_call = False
            if not m and info["ret_void"]:
                m = _VOID_CALL_RE.match(stripped)
                is_void_call = m is not None
            if not m and not info["ret_void"]:
                m = _BARE_CALL_RE.match(stripped)
                is_bare_call = m is not None
            if not m:
                continue
            actuals_raw = [a.strip() for a in m.group("args").split(",") if a.strip()]
            if not _call_signature_matches(stripped, actuals_raw, info):
                continue
            actuals = []
            for raw in actuals_raw:
                parts = raw.split()
                actuals.append(parts[-1] if len(parts) >= 2 else raw)
            if len(actuals) != len(info["args"]):
                continue
            counter += 1
            prefix = f"inl{counter}"
            while "%" + prefix + "." in fn_text:
                counter += 1
                prefix = f"inl{counter}"
            indent = m.group("indent")
            remap: dict[str, str] = {
                arg_name: val for arg_name, val in zip(info["args"], actuals)
            }
            for body_line in info["body"]:
                defn = re.match(r"^%([\w\.]+)\s*=", body_line)
                if defn:
                    remap[defn.group(1)] = f"%{prefix}.{defn.group(1)}"

            emitted: list[str] = []
            for body_line in info["body"]:
                gep = _NOOP_GEP_RE.match(body_line)
                if gep is not None:
                    remap[gep.group("res")] = _apply_remap_token(gep.group("base"), remap)
                    continue
                new_line = _apply_remap(body_line, remap)
                emitted.append(f"{indent}{new_line}\n")

            if not is_void_call and not is_bare_call:
                ret_val = info["ret_val"].strip()
                ret_rewritten = _apply_remap_token(ret_val, remap)
                type_m = re.search(r"call\s+([^@%\s]+)\s+@", stripped)
                if not type_m:
                    continue
                ret_ty = type_m.group(1).strip()
                if ret_ty in _PASSTHROUGH_RET_TYPES:
                    value_replacements[m.group("res")] = ret_rewritten
                else:
                    emitted.append(
                        f"{indent}%{m.group('res')} = "
                        f"add {ret_ty} 0, {ret_rewritten}\n"
                    )
            out.extend(emitted)
            inlined_any = True
            break

        if not inlined_any:
            out.append(stripped + "\n")

    # A backedge PHI can occur textually before the call whose pointer result
    # it consumes. Rewrite the complete function once the replacement map is
    # complete, rather than leaving that earlier use without a definition.
    return _apply_value_replacements("".join(out), value_replacements), counter


def _inline_multiblock_call_sites(ir_text: str, candidates: dict[str, dict]) -> str:
    """Splice bounded, alloca-free callees at ordinary direct call sites.

    Keep immutable callee bodies so a traversal cannot recursively expand
    already-inlined code. Return edges join in a new continuation; successor
    PHIs must name that continuation after the caller block is split.
    """
    templates = MutableModule.parse(ir_text)
    templates_by_name = {fn.name: fn for fn in templates.functions}
    module = MutableModule.parse(ir_text)
    changed = False
    counter = 0
    addressed = set(re.findall(r"\bblockaddress\s*\(\s*@([\w.$-]+)\s*,", ir_text))
    for fn in module.functions:
        if fn.name in addressed:
            continue
        if any(inst.opcode in ("invoke", "callbr", "indirectbr", "catchswitch", "catchpad", "cleanuppad") or "blockaddress(" in inst.text
               for block in fn.blocks for inst in block.instructions):
            continue
        # Build namespace and predecessor-use indices once per function.
        # Serializing/scanning the growing function at every call site made
        # native optimization quadratic in its expanded instruction count.
        occupied = fn.defined_names()
        phi_users = {}
        for block in fn.blocks:
            occupied.add(block.name)
            for inst in block.instructions:
                if inst.opcode != "phi":
                    continue
                for predecessor in re.findall(r",\s*%([\w.$-]+)\s*\]", inst.text):
                    users = phi_users.get(predecessor)
                    if users is None:
                        users = []
                        phi_users[predecessor] = users
                    users.append(inst)
        reserved_prefixes = set()
        for name in occupied:
            if name.startswith("inl.cfg."):
                parts = name.split(".", 3)
                if len(parts) == 4 and parts[2].isdigit():
                    reserved_prefixes.add(parts[2])
        block_index = 0
        while block_index < len(fn.blocks):
            block = fn.blocks[block_index]
            selected = None
            for call_index, call in enumerate(block.instructions):
                if call.opcode not in ("call", "tail"):
                    continue
                direct = _DIRECT_CALLEE_RE.search(call.text)
                if direct is None:
                    continue
                name = direct.group("callee")
                info = candidates.get(name)
                if info is None or info["single_block"] or name == fn.name:
                    continue
                call_info = _match_call_instruction(call.text.strip(), name, info["ret_void"])
                if call_info is None or not _call_signature_matches(call.text, call_info["actuals_raw"], info):
                    continue
                callee = templates_by_name.get(name)
                if callee is None:
                    continue
                selected = (call_index, call, info, call_info, callee)
                break
            if selected is None:
                block_index += 1
                continue
            call_index, call, info, call_info, callee = selected
            counter += 1
            prefix_number = str(counter)
            while prefix_number in reserved_prefixes:
                counter += 1
                prefix_number = str(counter)
            prefix = "inl.cfg." + prefix_number
            cloned = module.clone_blocks(fn, callee.blocks, prefix)
            continuation_name = prefix + ".continuation"
            for clone in cloned:
                occupied.add(clone.name)
                for inst in clone.instructions:
                    if inst.result_name:
                        occupied.add(inst.result_name)
            continuation_name = _fresh_inline_name(continuation_name, prefix, occupied)
            remap = {arg: actual for arg, actual in zip(info["args"], call_info["actuals"], strict=True)}
            incoming = []
            for clone in cloned:
                clone.instructions = [llvm_line(_apply_remap(inst.text, remap)) for inst in clone.instructions]
                term = clone.terminator
                if term.opcode == "ret":
                    if info["return_type"] != "void":
                        value = term.text.strip().split(None, 2)[2]
                        incoming.append("[ " + value + ", %" + clone.name + " ]")
                    clone.instructions[-1] = llvm_line("  br label %" + continuation_name + "\n")
            tail = list(block.instructions[call_index + 1:])
            if call_info["kind"] == "used":
                tail.insert(0, llvm_line("  %" + call.result_name + " = phi "
                    + info["return_type"] + " " + ", ".join(incoming) + "\n"))
            continuation = BasicBlock(continuation_name, continuation_name + ":\n", tail)
            # Every original outgoing edge now leaves the continuation. Only
            # PHI predecessor labels change; values defined before the call
            # continue to dominate their uses.
            affected_phis = phi_users.pop(block.name, [])
            for inst in affected_phis:
                inst.text = replace_local_names(
                    inst.text, {block.name: "%" + continuation_name}
                )
            if affected_phis:
                phi_users[continuation_name] = affected_phis
            block.instructions = list(block.instructions[:call_index]) + [
                llvm_line("  br label %" + cloned[0].name + "\n")
            ]
            fn.blocks[block_index + 1:block_index + 1] = cloned + [continuation]
            # Skip cloned calls this round, but process subsequent original
            # calls that moved into the continuation.
            block_index += len(cloned) + 1
            changed = True
    return module.serialize() if changed else ir_text


def _inline_multiblock_return_callers(ir_text: str, candidates: dict[str, dict]) -> str:
    mut = MutableModule.parse(ir_text)
    changed = False
    counter = 0

    for fn in list(mut.functions):
        idx = 0
        while idx < len(fn.blocks):
            block = fn.blocks[idx]
            if len(block.instructions) != 2:
                idx += 1
                continue
            call_inst, ret_inst = block.instructions
            if ret_inst.opcode != "ret":
                idx += 1
                continue

            matched = False
            for callee_name, info in candidates.items():
                if info.get("single_block", True):
                    continue
                call_info = _match_call_instruction(call_inst.text.strip(), callee_name, info["ret_void"])
                if call_info is None:
                    continue
                if not _call_signature_matches(call_inst.text, call_info["actuals_raw"], info):
                    continue
                if len(call_info["actuals"]) != len(info["args"]):
                    continue
                if call_info["kind"] == "used" and ret_inst.text.strip().split()[-1] != "%" + call_inst.result_name:
                    # LLVM gives bare nonvoid calls a numeric result name on
                    # serialization. Preserve an unrelated caller return when
                    # that result is unused; otherwise this shape is ineligible.
                    result_used = False
                    for existing_block in fn.blocks:
                        for instruction in existing_block.instructions:
                            if instruction is not call_inst and call_inst.result_name in instruction.operand_names():
                                result_used = True
                    if result_used:
                        continue
                    call_info["kind"] = "unused"
                callee_fn = mut.function(callee_name)
                if callee_fn is None or callee_fn is fn:
                    continue
                exit_blocks = [b for b in callee_fn.blocks if b.terminator and b.terminator.opcode == "ret"]
                callee_entry_name = callee_fn.blocks[0].name
                if len(exit_blocks) == 1:
                    callee_exit_name = exit_blocks[0].name
                elif len(exit_blocks) == 2:
                    if call_info["kind"] not in {"used", "void"}:
                        continue
                    total_non_terms = sum(
                        max(len(list(b.instructions)) - 1, 0) for b in callee_fn.blocks
                    )
                    if total_non_terms != 0 or len(callee_fn.blocks) != 3:
                        continue
                    callee_exit_name = ""
                else:
                    continue

                counter += 1
                prefix = f"inl.{callee_name}.i{counter}"
                caller_text = fn.serialize()
                while prefix + "." in caller_text:
                    counter += 1
                    prefix = f"inl.{callee_name}.i{counter}"
                cloned = mut.clone_blocks(fn, callee_fn.blocks, prefix)
                if not cloned:
                    continue
                occupied = fn.defined_names()
                for existing in fn.blocks:
                    occupied.add(existing.name)
                for cloned_block in cloned:
                    for instruction in cloned_block.instructions:
                        if instruction.result_name:
                            occupied.add(instruction.result_name)
                arg_remap = {arg: actual for arg, actual in zip(info["args"], call_info["actuals"], strict=True)}
                for cloned_block in cloned:
                    for inst in cloned_block.instructions:
                        inst.text = _apply_remap(inst.text.rstrip("\n"), arg_remap) + "\n"
                        reparsed = llvm_line(inst.text)
                        inst.result_name = reparsed.result_name
                        inst.opcode = reparsed.opcode

                if len(exit_blocks) == 1:
                    _rename_cloned_blocks_for_inline(
                        cloned,
                        block.name,
                        callee_name,
                        callee_entry_name,
                        callee_exit_name,
                        prefix,
                        occupied,
                    )

                    exit_clone = next(b for b in cloned if b.terminator and b.terminator.opcode == "ret")
                    if call_info["kind"] == "used":
                        pass
                    else:
                        exit_clone.instructions[-1] = llvm_line(ret_inst.text)
                else:
                    cloned = _rewrite_two_exit_inline_shape(
                        cloned,
                        block.name,
                        callee_name,
                        callee_entry_name,
                        prefix,
                        occupied,
                        ret_inst.text,
                    )

                fn.blocks = fn.blocks[:idx] + cloned + fn.blocks[idx + 1:]
                changed = True
                matched = True
                idx += len(cloned)
                break
            if not matched:
                idx += 1

    return mut.serialize() if changed else ir_text


def _fresh_inline_name(preferred: str, prefix: str, occupied: set[str]) -> str:
    name = preferred
    if name[0] in "0123456789":
        name = "." + name
    if name in occupied:
        name = prefix + "." + name
    while name in occupied:
        name += ".next"
    occupied.add(name)
    return name


def _rename_cloned_blocks_for_inline(
    cloned: list,
    caller_block_name: str,
    callee_name: str,
    callee_entry_name: str,
    callee_exit_name: str,
    prefix: str,
    occupied: set[str],
) -> None:
    block_renames: dict[str, str] = {}
    for index, block in enumerate(cloned):
        old_name = block.name
        if index == 0:
            block_renames[old_name] = caller_block_name
        else:
            # Preserve the entire original name, not only its last dotted
            # component. Keep upstream's short names when they are available.
            original_name = old_name[len(prefix) + 1:]
            preferred = callee_name + ".exit" if original_name == callee_exit_name else original_name + ".i"
            block_renames[old_name] = _fresh_inline_name(preferred, prefix, occupied)

    for block in cloned:
        new_name = block_renames[block.name]
        block.name = new_name
        block.label_line = f"{new_name}:\n"

    for block in cloned:
        for inst in block.instructions:
            inst.text = replace_local_names(
                inst.text, {old: "%" + new for old, new in block_renames.items()}
            )
            reparsed = llvm_line(inst.text)
            inst.result_name = reparsed.result_name
            inst.opcode = reparsed.opcode


def _rewrite_two_exit_inline_shape(
    cloned: list,
    caller_block_name: str,
    callee_name: str,
    callee_entry_name: str,
    prefix: str,
    occupied: set[str],
    caller_return: str,
) -> list:
    from .ir_mutator import BasicBlock, Instruction

    block_renames: dict[str, str] = {}
    ret_blocks: list = []
    for index, block in enumerate(cloned):
        old_name = block.name
        if index == 0:
            block_renames[old_name] = caller_block_name
        else:
            original_name = old_name[len(prefix) + 1:]
            block_renames[old_name] = _fresh_inline_name(original_name + ".i", prefix, occupied)
        if block.terminator and block.terminator.opcode == "ret":
            ret_blocks.append(block)

    for block in cloned:
        new_name = block_renames[block.name]
        block.name = new_name
        block.label_line = f"{new_name}:\n"

    for block in cloned:
        for inst in block.instructions:
            inst.text = replace_local_names(
                inst.text, {old: "%" + new for old, new in block_renames.items()}
            )
            reparsed = llvm_line(inst.text)
            inst.result_name = reparsed.result_name
            inst.opcode = reparsed.opcode

    exit_label = _fresh_inline_name(callee_name + ".exit", prefix, occupied)
    if not ret_blocks:
        return cloned

    if all(block.terminator and block.terminator.text.strip() == "ret void" for block in ret_blocks):
        for block in ret_blocks:
            term = block.terminator
            if term is None:
                continue
            term.text = f"  br label %{exit_label}\n"
            term.opcode = "br"
            term.result_name = None
        exit_block = BasicBlock(
            name=exit_label,
            label_line=f"{exit_label}:\n",
            instructions=[Instruction.from_text(caller_return)],
        )
        return [*cloned, exit_block]

    incoming: list[tuple[str, str]] = []
    ret_ty = ""
    for block in ret_blocks:
        term = block.terminator
        if term is None:
            continue
        text = term.text.strip()
        m = re.match(r"ret\s+(?P<ty>[^%@\s]+)\s+(?P<val>.+?)\s*$", text)
        if m is None:
            continue
        ret_ty = m.group("ty")
        incoming.append((m.group("val").strip(), block.name))
        term.text = f"  br label %{exit_label}\n"
        term.opcode = "br"
        term.result_name = None

    phi_name = _fresh_inline_name("r1", prefix, occupied)
    exit_lines = [
        Instruction.from_text(
            "  %"
            + phi_name
            + " = phi "
            + ret_ty
            + " "
            + ", ".join(f"[ {val}, %{pred} ]" for val, pred in incoming)
            + "\n"
        ),
        Instruction.from_text(f"  ret {ret_ty} %{phi_name}\n"),
    ]
    exit_block = BasicBlock(name=exit_label, label_line=f"{exit_label}:\n", instructions=exit_lines)
    return [*cloned, exit_block]


def _match_call_instruction(text: str, callee_name: str, ret_void: bool) -> dict[str, object] | None:
    pattern = re.compile(_CALL_RE_TEMPLATE.format(callee=re.escape(callee_name)))
    void_pattern = re.compile(_VOID_CALL_RE_TEMPLATE.format(callee=re.escape(callee_name)))
    bare_pattern = re.compile(_BARE_CALL_RE_TEMPLATE.format(callee=re.escape(callee_name)))
    m = pattern.match(text)
    kind = "used"
    if not m and ret_void:
        m = void_pattern.match(text)
        kind = "void"
    if not m and not ret_void:
        m = bare_pattern.match(text)
        kind = "unused"
    if not m:
        return None
    actuals_raw = [a.strip() for a in m.group("args").split(",") if a.strip()]
    actuals = []
    for raw in actuals_raw:
        parts = raw.split()
        actuals.append(parts[-1] if len(parts) >= 2 else raw)
    return {"kind": kind, "actuals": actuals, "actuals_raw": actuals_raw}


def _apply_remap(body_line: str, remap: dict[str, str]) -> str:
    return _replace_percent_names(body_line, remap)


def llvm_line(text: str):
    from .ir_mutator import Instruction
    return Instruction.from_text(text if text.endswith("\n") else text + "\n")


def _apply_remap_token(tok: str, remap: dict[str, str]) -> str:
    if tok.startswith("%"):
        name = tok[1:]
        if name in remap:
            return remap[name]
    return tok


def _apply_value_replacements(text: str, replacements: dict[str, str]) -> str:
    if not replacements:
        return text
    return _replace_percent_names(text, replacements)


def _replace_percent_names(text: str, replacements: dict[str, str]) -> str:
    return replace_local_names(text, replacements)


def _split_functions(ir_text: str) -> list[tuple[bool, str]]:
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


def _drop_dead_internal_callees(ir_text: str, candidates: dict[str, dict]) -> str:
    out: list[str] = []
    for is_function, chunk in _split_functions(ir_text):
        if not is_function:
            out.append(chunk)
            continue
        m = re.search(r"define\s+[^@]*@([\w\.]+)", chunk)
        fn_name = m.group(1) if m else None
        if (
            fn_name in candidates
            and candidates[fn_name]["internal"]
            and not re.search(r"call\s+[^@]*@" + re.escape(fn_name) + r"\b", ir_text)
        ):
            continue
        out.append(chunk)
    return "".join(out)
