"""Self-host-safe implementation of the bounded default IR-pass tier.

This module deliberately owns only the versioned ``mem2reg,sroa`` manifest
selected by the self backend.  It has no llvmlite dependency and never loads
the wider translated pass library.  Both transforms are finite, textual
subsets: a shape that cannot be proved safe is returned unchanged.

The host pass runner remains the owner for explicit higher tiers and pass
discovery.  Keeping that boundary here is important: making pcc1 import the
whole pass package would trade one subprocess for a much larger libpython
closure.
"""

from __future__ import annotations

from .pipeline_pass_config import (
    PYTHON_IR_PASS_DEFAULT_TIER,
    PYTHON_IR_PASS_DEFAULT_TIER_SCHEMA,
)


COMPILED_DEFAULT_TIER_SCHEMA = PYTHON_IR_PASS_DEFAULT_TIER_SCHEMA
COMPILED_DEFAULT_TIER = PYTHON_IR_PASS_DEFAULT_TIER
_SSA_NAME_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.$-"
_SCALAR_TYPES = frozenset(
    {
        "half",
        "bfloat",
        "float",
        "double",
        "fp128",
        "x86_fp80",
        "ptr",
    }
)


def is_compiled_default_tier(pass_names: list[str]) -> bool:
    """Return whether *pass_names* is the exact versioned owned manifest."""

    normalized: list[str] = []
    for pass_name in pass_names:
        normalized.append(str(pass_name).strip().lower())
    return tuple(normalized) == COMPILED_DEFAULT_TIER


def run_compiled_default_tier(
    ir_text: str,
    pass_names: list[str],
    *,
    strict_no_libpython: bool,
) -> str:
    """Run the exact self-hosted tier, leaving unsupported IR unchanged."""

    text = str(ir_text)
    if not is_compiled_default_tier(pass_names):
        raise ValueError(
            "compiled Python IR tier only owns schema "
            + COMPILED_DEFAULT_TIER_SCHEMA
            + " with passes "
            + ",".join(COMPILED_DEFAULT_TIER)
        )
    if strict_no_libpython and _has_py_cpy_call(text):
        # Match the host runner's strict-mode rule.  Declarations alone do not
        # block optimization; an actual fallback call does.
        return text
    return _rewrite_functions(text)


def _has_py_cpy_call(ir_text: str) -> bool:
    for line in str(ir_text).splitlines():
        if "@py_cpy_" not in line:
            continue
        stripped = line.lstrip()
        if stripped.startswith("call ") or stripped.startswith("tail call "):
            return True
        if " = call " in line or " = tail call " in line:
            return True
    return False


def _rewrite_functions(ir_text: str) -> str:
    """Apply the owned per-function transforms in one traversal of *ir_text*.

    Both transforms are strictly per-function — each is handed one ``define``
    block's lines and returns that block's lines — so running
    ``sroa(mem2reg(body))`` per function is equivalent to running mem2reg over
    the whole module and then sroa over the result, and it does the expensive
    part once instead of twice.  That part is not the transforms: splitting a
    multi-megabyte IR text yields hundreds of thousands of string objects
    (each an allocation the GC must track), the per-line loop appends every
    one of them to a list, and ``join`` rebuilds the text.  Under a
    self-hosted pcc1 this traversal was ~65% of the entire compile while the
    transforms inside it were ~5%.

    The transforms are also called directly rather than passed in as function
    values: a function value crossing a call boundary lowers to the fully
    dynamic path (build an argument tuple, ``py_obj_call`` to resolve the
    callable, enter its native adapter, unpack and marshal again) once per
    ``define`` in the module.
    """
    lines = str(ir_text).splitlines(keepends=True)
    out: list[str] = []
    function_lines: list[str] = []
    in_function = False
    for line in lines:
        stripped = line.lstrip()
        if not in_function and stripped.startswith("define "):
            in_function = True
            function_lines = [line]
            continue
        if in_function:
            function_lines.append(line)
            if line.strip() == "}":
                out.extend(_sroa_function(_mem2reg_function(function_lines)))
                function_lines = []
                in_function = False
            continue
        out.append(line)
    if function_lines:
        # Malformed/incomplete input is not this optimizer's diagnostic
        # boundary.  Preserve it for the downstream verifier to report.
        out.extend(function_lines)
    return "".join(out)


def _promote_entry_single_store(lines: list[str]) -> list[str]:
    """Promote a non-escaping scalar slot written exactly once in the entry block.

    The owned single-block promotion below rejects any slot whose loads leave
    the alloca's own block, so on real runtime functions it promotes nothing:
    every ``%x.addr`` parameter spill is stored in the entry block and read in
    later blocks.  Measured on ``py_obj.ll``, 148 non-escaping scalar slots
    carry 843 memory operations and the owned tier removed zero of them, while
    the llvmlite-backed ``ir_passes/mem2reg`` removed 49% of the object's frame
    load/store traffic -- the whole of LLVM O2's win on that module.

    This promotion needs no dominance analysis and is correct by construction:
    the entry block dominates every block, the slot's address never escapes, so
    nothing but these loads and this store can touch it, and requiring the
    store to precede any entry-block load means every load observes exactly
    that value.  Slots with a second store, an escape, an atomic/volatile
    access or a type mismatch are left alone, and the result still passes the
    fail-closed dangling-reference check.
    """
    block_ids = _block_ids(lines)
    if not block_ids:
        return lines
    entry_block = -1
    for index, line in enumerate(lines):
        parsed = _parse_alloca(line)
        if parsed is not None:
            entry_block = block_ids[index]
            break
    if entry_block < 0:
        return lines

    candidates: dict[str, dict[str, object]] = {}
    for index, line in enumerate(lines):
        parsed = _parse_alloca(line)
        if parsed is None:
            continue
        name, ty, _indent = parsed
        if not _is_scalar_type(ty):
            continue
        if block_ids[index] != entry_block:
            continue
        candidates[name] = {
            "line": index,
            "type": ty,
            "loads": [],
            "store_line": -1,
            "store_value": "",
            "safe": True,
        }
    if not candidates:
        return lines

    for index, line in enumerate(lines):
        names = _ssa_names_in(line)
        if not names:
            continue
        store = _parse_store(line)
        load = _parse_load(line)
        for name in names:
            if name not in candidates:
                continue
            candidate = candidates[name]
            if not candidate["safe"]:
                continue
            if index == candidate["line"]:
                continue
            if store is not None:
                store_ty, value, pointer = store
                if pointer == name and store_ty == candidate["type"]:
                    if _contains_ssa_name(value, name):
                        candidate["safe"] = False
                        continue
                    if int(candidate["store_line"]) >= 0:
                        candidate["safe"] = False
                        continue
                    candidate["store_line"] = index
                    candidate["store_value"] = value
                    continue
            if load is not None:
                result, load_ty, pointer = load
                if pointer == name and load_ty == candidate["type"]:
                    load_lines = candidate["loads"]
                    load_lines.append((index, result))
                    continue
            candidate["safe"] = False

    removed: set[int] = set()
    removed_definitions: list[str] = []
    replacements: dict[str, str] = {}
    for name, candidate in candidates.items():
        if not candidate["safe"]:
            continue
        store_line = int(candidate["store_line"])
        if store_line < 0:
            continue
        if block_ids[store_line] != entry_block:
            continue
        load_lines = candidate["loads"]
        usable = True
        position = 0
        while position < len(load_lines):
            load_index = int(load_lines[position][0])
            if block_ids[load_index] == entry_block and load_index < store_line:
                usable = False
            position += 1
        if not usable:
            continue
        removed.add(int(candidate["line"]))
        removed.add(store_line)
        removed_definitions.append(str(name))
        position = 0
        while position < len(load_lines):
            load_index, result = load_lines[position]
            removed.add(int(load_index))
            replacements[str(result)] = str(candidate["store_value"])
            removed_definitions.append(str(result))
            position += 1

    if not removed:
        return lines
    replacements = _resolved_replacements(replacements)
    out: list[str] = []
    for index, line in enumerate(lines):
        if index in removed:
            continue
        out.append(_replace_ssa_names(line, replacements))
    if _references_removed_definitions(out, removed_definitions):
        return lines
    return out


def _mem2reg_function(lines: list[str]) -> list[str]:
    """Owned mem2reg: entry-block single-store promotion, then the
    single-block scan.  The two are independent and both fail closed."""
    return _mem2reg_single_block_function(_promote_entry_single_store(lines))


def _mem2reg_single_block_function(lines: list[str]) -> list[str]:
    block_ids = _block_ids(lines)
    blocked = _control_flow_blocks(lines, block_ids)
    candidates: dict[str, dict[str, object]] = {}
    for index, line in enumerate(lines):
        parsed = _parse_alloca(line)
        if parsed is None:
            continue
        name, ty, _indent = parsed
        if not _is_scalar_type(ty):
            continue
        candidates[name] = {
            "line": index,
            "type": ty,
            "block": block_ids[index],
            "events": [],
            "safe": block_ids[index] not in blocked,
        }
    if not candidates:
        return lines

    for index, line in enumerate(lines):
        # Only the candidates this line actually mentions can change state, and
        # `_ssa_names_in` returns exactly the names `_contains_ssa_name` would
        # accept.  Candidates never interact, so visiting them in the line's
        # token order rather than in insertion order cannot change the outcome.
        names = _ssa_names_in(line)
        if not names:
            continue
        store = _parse_store(line)
        load = _parse_load(line)
        for name in names:
            if name not in candidates:
                continue
            candidate = candidates[name]
            if not candidate["safe"]:
                continue
            if index == candidate["line"]:
                continue
            if block_ids[index] != candidate["block"]:
                candidate["safe"] = False
                continue
            if store is not None:
                store_ty, value, pointer = store
                if pointer == name and store_ty == candidate["type"]:
                    # Storing the alloca address itself is an escape, not a
                    # scalar value update.  Removing that slot would leave a
                    # dangling SSA reference.
                    if _contains_ssa_name(value, name):
                        candidate["safe"] = False
                        continue
                    candidate["events"].append((index, "store", value))
                    continue
            if load is not None:
                result, load_ty, pointer = load
                if pointer == name and load_ty == candidate["type"]:
                    candidate["events"].append((index, "load", result))
                    continue
            candidate["safe"] = False

    removed: set[int] = set()
    removed_definitions: list[str] = []
    replacements: dict[str, str] = {}
    for _name, candidate in candidates.items():
        if not candidate["safe"]:
            continue
        removed.add(int(candidate["line"]))
        removed_definitions.append(str(_name))
        current_value = "undef"
        events = list(candidate["events"])
        events.sort(key=lambda item: int(item[0]))
        for index, kind, payload in events:
            removed.add(int(index))
            if kind == "store":
                current_value = str(payload)
            else:
                replacements[str(payload)] = current_value
                removed_definitions.append(str(payload))

    if not removed:
        return lines
    replacements = _resolved_replacements(replacements)
    out: list[str] = []
    for index, line in enumerate(lines):
        if index in removed:
            continue
        out.append(_replace_ssa_names(line, replacements))
    if _references_removed_definitions(out, removed_definitions):
        # The compiled bootstrap executes this pass using PCC's own string and
        # mapping runtime.  Promotion is optional, so an incomplete native
        # replacement must fail closed to the original valid function instead
        # of publishing dangling SSA references to removed loads or allocas.
        return lines
    return out


def _sroa_function(lines: list[str]) -> list[str]:
    block_ids = _block_ids(lines)
    blocked = _control_flow_blocks(lines, block_ids)
    taken_names: set[str] = set()
    names_scanned = False
    candidates: dict[str, dict[str, object]] = {}
    for index, line in enumerate(lines):
        parsed = _parse_alloca(line)
        if parsed is None:
            continue
        name, ty, indent = parsed
        fields = _literal_struct_fields(ty)
        if fields is None or len(fields) < 2 or len(fields) > 4:
            continue
        if not names_scanned:
            taken_names = _all_defined_ssa_names(lines)
            names_scanned = True
        slot_names: list[str] = []
        field_index = 0
        while field_index < len(fields):
            slot_names.append(
                _fresh_name(name + ".pcc.sroa." + str(field_index), taken_names)
            )
            field_index += 1
        candidates[name] = {
            "line": index,
            "type": ty,
            "fields": fields,
            "slots": slot_names,
            "indent": indent,
            "block": block_ids[index],
            "safe": block_ids[index] not in blocked,
            "geps": {},
            "gep_lines": set(),
        }
    if not candidates:
        return lines

    for index, line in enumerate(lines):
        gep = _parse_struct_gep(line)
        if gep is not None:
            result, aggregate_ty, pointer, field_index = gep
            candidate = candidates.get(pointer)
            if candidate is not None and candidate["safe"]:
                fields = candidate["fields"]
                if (
                    aggregate_ty == candidate["type"]
                    and 0 <= field_index < len(fields)
                    and block_ids[index] == candidate["block"]
                ):
                    candidate["geps"][result] = field_index
                    candidate["gep_lines"].add(index)
                else:
                    candidate["safe"] = False

    for index, line in enumerate(lines):
        store = _parse_store(line)
        load = _parse_load(line)
        for name, candidate in candidates.items():
            if not candidate["safe"]:
                continue
            if index == candidate["line"] or index in candidate["gep_lines"]:
                continue
            if _contains_ssa_name(line, name):
                candidate["safe"] = False
                continue
            for gep_name, field_index in candidate["geps"].items():
                if not _contains_ssa_name(line, gep_name):
                    continue
                field_ty = candidate["fields"][field_index]
                if store is not None:
                    store_ty, _value, pointer = store
                    if pointer == gep_name and store_ty == field_ty:
                        continue
                if load is not None:
                    _result, load_ty, pointer = load
                    if pointer == gep_name and load_ty == field_ty:
                        continue
                candidate["safe"] = False
                break

    removed: set[int] = set()
    removed_definitions: list[str] = []
    replacements: dict[str, str] = {}
    allocation_lines: dict[int, list[str]] = {}
    for _name, candidate in candidates.items():
        if not candidate["safe"] or not candidate["geps"]:
            continue
        alloca_index = int(candidate["line"])
        removed.add(alloca_index)
        removed_definitions.append(str(_name))
        new_lines: list[str] = []
        field_position = 0
        while field_position < len(candidate["fields"]):
            slot_name = candidate["slots"][field_position]
            field_ty = candidate["fields"][field_position]
            new_lines.append(
                str(candidate["indent"])
                + "%"
                + slot_name
                + " = alloca "
                + field_ty
                + "\n"
            )
            field_position += 1
        allocation_lines[alloca_index] = new_lines
        for gep_name, field_index in candidate["geps"].items():
            replacements[gep_name] = "%" + candidate["slots"][field_index]
            removed_definitions.append(str(gep_name))
        for gep_line in candidate["gep_lines"]:
            removed.add(int(gep_line))

    if not removed:
        return lines
    rewritten: list[str] = []
    for index, line in enumerate(lines):
        if index in allocation_lines:
            rewritten.extend(allocation_lines[index])
        if index in removed:
            continue
        rewritten.append(_replace_ssa_names(line, replacements))
    if _references_removed_definitions(rewritten, removed_definitions):
        return lines
    # SROA exposes scalar slots.  The same bounded local promotion is part of
    # the default SROA contract, matching the host subset's final cleanup.
    return _mem2reg_function(rewritten)


def _parse_alloca(line: str):
    # Reject unrelated opcodes before allocating assignment pieces. Every
    # accepted spelling below contains this token; false positives still pass
    # through the original parser and its fail-closed checks.
    if "alloca " not in line:
        return None
    assignment = _split_assignment(line)
    if assignment is None:
        return None
    name, rhs, indent = assignment
    if not rhs.startswith("alloca "):
        return None
    pieces = _split_top_level(rhs[len("alloca ") :], ",")
    if not pieces:
        return None
    ty = pieces[0].strip()
    if not ty:
        return None
    return name, ty, indent


def _parse_store(line: str):
    if "store " not in line:
        return None
    stripped = line.strip()
    if not stripped.startswith("store "):
        return None
    if stripped.startswith("store atomic ") or stripped.startswith("store volatile "):
        return None
    pieces = _split_top_level(stripped[len("store ") :], ",")
    if len(pieces) < 2:
        return None
    first = pieces[0].strip()
    split_at = first.find(" ")
    if split_at <= 0:
        return None
    ty = first[:split_at].strip()
    value = first[split_at + 1 :].strip()
    if not _is_scalar_type(ty) or not _is_safe_value_atom(value):
        return None
    pointer = _pointer_operand_name(pieces[1])
    if pointer is None:
        return None
    return ty, value, pointer


def _parse_load(line: str):
    if "load " not in line:
        return None
    assignment = _split_assignment(line)
    if assignment is None:
        return None
    result, rhs, _indent = assignment
    if not rhs.startswith("load "):
        return None
    if rhs.startswith("load atomic ") or rhs.startswith("load volatile "):
        return None
    pieces = _split_top_level(rhs[len("load ") :], ",")
    if len(pieces) < 2:
        return None
    ty = pieces[0].strip()
    if not _is_scalar_type(ty):
        return None
    pointer = _pointer_operand_name(pieces[1])
    if pointer is None:
        return None
    return result, ty, pointer


def _parse_struct_gep(line: str):
    if "getelementptr " not in line:
        return None
    assignment = _split_assignment(line)
    if assignment is None:
        return None
    result, rhs, _indent = assignment
    if not rhs.startswith("getelementptr "):
        return None
    body = rhs[len("getelementptr ") :]
    if body.startswith("inbounds "):
        body = body[len("inbounds ") :]
    pieces = _split_top_level(body, ",")
    if len(pieces) != 4:
        return None
    aggregate_ty = pieces[0].strip()
    pointer = _pointer_operand_name(pieces[1])
    zero_index = _constant_index(pieces[2])
    field_index = _constant_index(pieces[3])
    if pointer is None or zero_index != 0 or field_index is None:
        return None
    return result, aggregate_ty, pointer, field_index


def _split_assignment(line: str):
    stripped = line.strip()
    if not stripped.startswith("%") or " = " not in stripped:
        return None
    lhs, rhs = stripped.split(" = ", 1)
    name = lhs[1:]
    if not name or not _simple_ssa_name(name):
        return None
    indent = line[: len(line) - len(line.lstrip())]
    return name, rhs.strip(), indent


def _pointer_operand_name(text: str):
    stripped = str(text).strip()
    if not stripped.startswith("ptr "):
        return None
    operand = stripped[len("ptr ") :].strip()
    if not operand.startswith("%"):
        return None
    name = operand[1:].split()[0]
    if not _simple_ssa_name(name):
        return None
    return name


def _constant_index(text: str):
    pieces = str(text).strip().split()
    if len(pieces) != 2 or not pieces[0].startswith("i"):
        return None
    value = pieces[1]
    if value.startswith("-"):
        digits = value[1:]
    else:
        digits = value
    if not digits.isdigit():
        return None
    return int(value)


def _literal_struct_fields(ty: str):
    stripped = str(ty).strip()
    if not stripped.startswith("{") or not stripped.endswith("}"):
        return None
    fields = _split_top_level(stripped[1:-1], ",")
    out: list[str] = []
    for field in fields:
        field_ty = field.strip()
        if not _is_scalar_type(field_ty):
            return None
        out.append(field_ty)
    return out if out else None


def _split_top_level(text: str, delimiter: str) -> list[str]:
    out: list[str] = []
    start = 0
    round_depth = 0
    square_depth = 0
    brace_depth = 0
    angle_depth = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char == "(":
            round_depth += 1
        elif char == ")":
            round_depth -= 1
        elif char == "[":
            square_depth += 1
        elif char == "]":
            square_depth -= 1
        elif char == "{":
            brace_depth += 1
        elif char == "}":
            brace_depth -= 1
        elif char == "<":
            angle_depth += 1
        elif char == ">":
            angle_depth -= 1
        elif (
            char == delimiter
            and round_depth == 0
            and square_depth == 0
            and brace_depth == 0
            and angle_depth == 0
        ):
            out.append(text[start:index])
            start = index + 1
        index += 1
    if round_depth != 0 or square_depth != 0 or brace_depth != 0 or angle_depth != 0:
        return []
    out.append(text[start:])
    return out


def _block_ids(lines: list[str]) -> list[int]:
    out: list[int] = []
    current = 0
    saw_body = False
    for line in lines:
        if _is_block_label(line):
            if saw_body:
                current += 1
            saw_body = True
        out.append(current)
    return out


def _control_flow_blocks(lines: list[str], block_ids: list[int]) -> set[int]:
    out: set[int] = set()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if (
            stripped.startswith("br ")
            or stripped.startswith("switch ")
            or stripped.startswith("indirectbr ")
            or stripped.startswith("invoke ")
            or stripped.startswith("callbr ")
            or stripped.startswith("catchswitch ")
            or " = phi " in stripped
        ):
            out.add(block_ids[index])
    return out


def _is_block_label(line: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith(";"):
        return False
    if stripped.endswith(":"):
        return True
    colon = stripped.find(":")
    return colon > 0 and stripped[colon + 1 :].lstrip().startswith(";")


def _is_scalar_type(ty: str) -> bool:
    text = str(ty).strip()
    if text in _SCALAR_TYPES:
        return True
    if text.startswith("i") and text[1:].isdigit():
        return int(text[1:]) > 0
    return False


def _is_safe_value_atom(value: str) -> bool:
    text = str(value).strip()
    if not text or " " in text or "," in text:
        return False
    if text.startswith("%"):
        return _simple_ssa_name(text[1:])
    if text.startswith("@"):
        return _simple_ssa_name(text[1:])
    return True


def _simple_ssa_name(name: str) -> bool:
    if not name:
        return False
    for char in name:
        if char not in _SSA_NAME_CHARS:
            return False
    return True


def _contains_ssa_name(text: str, name: str) -> bool:
    index = 0
    needle = "%" + name
    while True:
        found = text.find(needle, index)
        if found < 0:
            return False
        end = found + len(needle)
        if end >= len(text) or text[end] not in _SSA_NAME_CHARS:
            return True
        index = end


def _ssa_names_in(text: str) -> list[str]:
    """Every maximal ``%name`` token in *text*, in order, without duplicates.

    This is exactly the set of names for which ``_contains_ssa_name(text,
    name)`` is True: that helper accepts ``%name`` only when the following
    character is outside ``_SSA_NAME_CHARS``, which is the same thing as the
    token being maximal.  ``%s1`` therefore does not answer for ``s``, and the
    prefix families real IR is full of (``%s1`` / ``%s10`` / ``%s100``) stay
    distinct -- the exact case a plain substring scan gets wrong.

    `_mem2reg_function` uses this to look up only the candidates a line
    actually mentions.  It used to walk every candidate for every line: on one
    real emitted module that was 38.5M (line, candidate) pairs against 252k
    lines, a 152x amplification, and under a self-hosted pcc1 each pair is a
    dict lookup plus a provenance-checked object touch.
    """
    names: list[str] = []
    seen: dict[str, int] = {}
    index = 0
    limit = len(text)
    while index < limit:
        if text[index] != "%":
            index = index + 1
            continue
        end = index + 1
        while end < limit and text[end] in _SSA_NAME_CHARS:
            end = end + 1
        name = text[index + 1 : end]
        if name != "" and name not in seen:
            seen[name] = 1
            names.append(name)
        index = end
    return names


def _replace_ssa_names(text: str, replacements: dict[str, str]) -> str:
    if not replacements or "%" not in text:
        return text
    out: list[str] = []
    index = 0
    while index < len(text):
        if text[index] != "%":
            out.append(text[index])
            index += 1
            continue
        end = index + 1
        while end < len(text) and text[end] in _SSA_NAME_CHARS:
            end += 1
        if end == index + 1:
            out.append(text[index])
            index += 1
            continue
        name = text[index + 1 : end]
        replacement = replacements.get(name)
        if replacement is None:
            out.append(text[index:end])
        else:
            out.append(replacement)
        index = end
    return "".join(out)


def _resolved_replacements(replacements: dict[str, str]) -> dict[str, str]:
    out = dict(replacements)
    limit = len(out) + 1
    for _round in range(limit):
        changed = False
        for name, value in list(out.items()):
            if not value.startswith("%"):
                continue
            replacement = out.get(value[1:])
            if replacement is not None and replacement != value:
                out[name] = replacement
                changed = True
        if not changed:
            break
    return out


def _all_defined_ssa_names(lines: list[str]) -> set[str]:
    out: set[str] = set()
    for line in lines:
        assignment = _split_assignment(line)
        if assignment is not None:
            out.add(assignment[0])
    return out


def _references_removed_definitions(lines, removed_names) -> bool:
    """Return whether transformed IR still uses an SSA definition it removed."""
    for line in lines:
        for name in removed_names:
            if _contains_ssa_name(line, str(name)):
                return True
    return False


def _fresh_name(base: str, taken: set[str]) -> str:
    name = base
    suffix = 0
    while name in taken:
        suffix += 1
        name = base + "." + str(suffix)
    taken.add(name)
    return name
