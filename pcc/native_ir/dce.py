"""Dead code elimination (DCE).

Upstream reference:

- ``/tmp/llvm-src/llvm-20.1.8.src/lib/Transforms/Scalar/DCE.cpp``
  implements :cpp:class:`llvm::DCEPass`. The algorithm is straight:
  iterate over every instruction, and remove any that has no uses
  and has no side effects. Iterate to a fixed point.

This pass mirrors that directly. An instruction is "safe to remove"
when:

- it defines a value (i.e. has a ``%name = ...`` form),
- it is not side-effecting (stores, terminators, most calls),
- or it is a direct call whose callee is explicitly marked
  ``readnone``/``memory(none)`` plus ``willreturn`` and ``nounwind``,
- it has zero uses (no other instruction names it as an operand).

Instruction enumeration and def-use collection operate on pcc-owned text.
Input is already-lowered IR; external reference verification belongs to the
legacy adapter, not this transformation kernel.
"""

from __future__ import annotations

import re




_SIDE_EFFECTING_OPCODES = {
    "store", "call", "invoke", "ret", "br", "switch", "indirectbr",
    "fence", "atomicrmw", "cmpxchg", "resume", "catchswitch",
    "catchret", "cleanupret", "unreachable",
}

_ATTR_GROUP_RE = re.compile(
    r"^\s*attributes\s+(?P<group>#\d+)\s*=\s*\{(?P<body>[^}]*)\}\s*$"
)
_FUNC_ATTR_RE = re.compile(
    r"""
    ^\s*
    (?:declare|define)\s+
    .*?@(?P<name>[\w\.\$]+)\([^)]*\)
    (?P<tail>.*?)
    (?:\s*\{)?\s*$
    """,
    re.VERBOSE,
)
_ASSIGN_RE = re.compile(r"^\s*%([\w\.]+)\s*=\s*(\w+)")
_CALL_ASSIGN_RE = re.compile(
    r"""
    ^\s*
    %(?P<result>[\w\.]+)\s*=\s*
    (?:(?:tail|musttail|notail)\s+)?
    call\b.*?@(?P<callee>[\w\.\$]+)\(
    """,
    re.VERBOSE,
)




def dce_module_text(
    ir_text: str, function_attrs: dict[str, set[str]] | None = None,
) -> tuple[str, bool]:
    """Remove dead instructions from the IR; return (new_ir, changed)."""
    current = ir_text
    any_change = False
    if function_attrs is None:
        function_attrs = _collect_function_attrs(ir_text)
    for _ in range(16):
        next_text, changed = _one_dce_pass(current, function_attrs)
        if not changed:
            break
        any_change = True
        current = next_text
    return current, any_change


def _collect_function_attrs(ir_text: str) -> dict[str, set[str]]:
    """Return direct-function attribute keywords keyed by function name."""
    attr_groups: dict[str, set[str]] = {}
    for line in ir_text.splitlines():
        match = _ATTR_GROUP_RE.match(line)
        if match is None:
            continue
        body = match.group("body").replace("{", " ").replace("}", " ")
        attr_groups[match.group("group")] = {
            tok
            for tok in body.split()
            if tok and tok != ","
        }

    attrs_by_func: dict[str, set[str]] = {}
    for line in ir_text.splitlines():
        match = _FUNC_ATTR_RE.match(line)
        if match is None:
            continue
        tail = match.group("tail")
        attrs: set[str] = set()
        for tok in tail.replace(",", " ").split():
            if not tok or tok == "{":
                continue
            if tok.startswith("#") and tok in attr_groups:
                attrs.update(attr_groups[tok])
                continue
            attrs.add(tok)
        attrs_by_func[match.group("name")] = attrs
    return attrs_by_func


def _line_by_result(ir_text: str) -> dict[str, str]:
    by_result: dict[str, str] = {}
    for line in ir_text.splitlines():
        match = _ASSIGN_RE.match(line)
        if match is not None:
            by_result[match.group(1)] = line
    return by_result


def _is_trivially_dead_call(
    name: str,
    line_by_result: dict[str, str],
    attrs_by_func: dict[str, set[str]],
) -> bool:
    line = line_by_result.get(name)
    if line is None:
        return False
    match = _CALL_ASSIGN_RE.match(line)
    if match is None:
        return False
    attrs = attrs_by_func.get(match.group("callee"), set())
    return (
        (
            "readnone" in attrs
            or "readonly" in attrs
            or "memory(none)" in attrs
            or "memory(read)" in attrs
        )
        and "willreturn" in attrs
        and "nounwind" in attrs
    )


def _one_dce_pass(
    ir_text: str, function_attrs: dict[str, set[str]] | None = None,
) -> tuple[str, bool]:
    functions = _defined_function_lines(ir_text)
    attrs_by_func = function_attrs
    if attrs_by_func is None:
        attrs_by_func = _collect_function_attrs(ir_text)

    # Scan per-function so SSA-name collisions across functions
    # (``%dead`` in fn A vs fn B are different values) don't merge.
    # For each defined-name in fn F, record:
    #   - rec.opcode,
    #   - the text line,
    #   - whether any user exists within F.
    fn_dead_names: dict[str, set[str]] = {}  # fn_name → dead ssa names
    for fn_name, fn_lines in functions:
        defs: dict[str, tuple[str, str]] = {}  # ssa name → (opcode, text)
        users: dict[str, set[str]] = {}        # ssa name → set of users
        for block_lines in (fn_lines,):
            for inst in block_lines:
                text = str(inst).strip()
                am = _ASSIGN_RE.match(text)
                opcode = am.group(2) if am else ""
                if opcode in ("tail", "musttail", "notail"):
                    opcode = "call"
                if am:
                    defs[am.group(1)] = (opcode, text)
                # Every other %name in this instruction is a use.
                for used in re.findall(r"%([\w\.]+)", text):
                    if am and used == am.group(1):
                        continue
                    users.setdefault(used, set()).add(
                        am.group(1) if am else f"__inst_{id(inst)}__"
                    )
        dead: set[str] = set()
        for name, (opcode, line_text) in defs.items():
            if opcode == "call":
                # Prepend two-space indent to match _CALL_ASSIGN_RE
                # expectations (it permits arbitrary leading ws).
                if not _is_trivially_dead_call(
                    name, {name: f"  {line_text}"}, attrs_by_func,
                ):
                    # Call with side effects — preserved.
                    continue
            elif opcode in _SIDE_EFFECTING_OPCODES or (opcode == "load" and re.search(r"\b(?:volatile|atomic)\b", line_text)):
                continue
            if not users.get(name):
                dead.add(name)
        fn_dead_names[fn_name] = dead

    if not any(fn_dead_names.values()):
        return ir_text, False

    # Walk text; drop lines whose defined-name is in the current
    # function's dead set.
    new_lines: list[str] = []
    current_fn: str | None = None
    _DEFINE_NAME_RE = re.compile(r"^\s*define\s+[^@]*@(?P<name>[\w\.]+)")
    for line in ir_text.splitlines(keepends=True):
        dm = _DEFINE_NAME_RE.match(line)
        if dm:
            current_fn = dm.group("name")
        if line.strip() == "}":
            new_lines.append(line)
            current_fn = None
            continue
        if current_fn is not None:
            m = _ASSIGN_RE.match(line)
            if m and m.group(1) in fn_dead_names.get(current_fn, set()):
                continue
        new_lines.append(line)
    return "".join(new_lines), True


def _defined_function_lines(ir_text: str) -> list[tuple[str, list[str]]]:
    """Enumerate already-lowered function bodies without an LLVM module."""
    functions: list[tuple[str, list[str]]] = []
    name = ""
    body: list[str] = []
    for line in ir_text.splitlines(keepends=True):
        if not name:
            match = re.match(r"^\s*define\s+[^@]*@(?P<name>[\w\.]+)", line)
            if match:
                name = match.group("name")
                body = []
        elif line.strip() == "}":
            functions.append((name, body))
            name = ""
            body = []
        else:
            body.append(line)
    return functions
