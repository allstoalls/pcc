from __future__ import annotations

"""Give ``alloca T, iN count`` its element count before the target parser.

The self-backend parser models every ``alloca`` as one fixed frame slot of
type ``T`` and used to drop the count operand, so a C variable-length array
(``double v[n]``) or ``__builtin_alloca(n)`` got room for a single element
and its stores ran over the saved frame record.  This rewrite runs on IR text
first:

* a constant count becomes an array allocation: ``alloca [N x T]``;
* a runtime count becomes ``call ptr @llvm.pcc.dynamic.alloca(i64 bytes,
  i64 align)``, with ``bytes`` computed as ``n * sizeof(T)`` through the
  ``getelementptr T, ptr null, iN n`` idiom so named types need no layout
  table here.  Targets lower the call by moving the stack pointer and restore
  it from the frame pointer in every epilogue, which also releases the
  storage at function exit like LLVM's dynamic ``alloca``.
"""

from .self_backend_wide_int import (
    _is_int_literal,
    _split_leading_type,
    _split_top_level,
    _symbol_end,
)


DYNAMIC_ALLOCA_INTRINSIC = "llvm.pcc.dynamic.alloca"
_DYNAMIC_ALLOCA_DECLARATION = "declare ptr @" + DYNAMIC_ALLOCA_INTRINSIC + "(i64, i64)"


def legalize_dynamic_allocas(ir_text: str) -> str:
    """Return ``ir_text`` with every counted ``alloca`` made explicit."""
    if not _has_counted_alloca(ir_text):
        return ir_text
    marker = ".pccdyn"
    serial = 0
    while ir_text.find(marker) >= 0:
        serial += 1
        marker = ".pccdyn" + str(serial)
    lines = ir_text.split("\n")
    out: list[str] = []
    changed = False
    dynamic = False
    for line in lines:
        rewritten = _rewrite_alloca_line(line, marker)
        if not rewritten:
            out.append(line)
            continue
        changed = True
        if len(rewritten) > 1:
            dynamic = True
        out.extend(rewritten)
    if not changed:
        return ir_text
    if dynamic and ir_text.find("@" + DYNAMIC_ALLOCA_INTRINSIC + "(") < 0:
        out.append(_DYNAMIC_ALLOCA_DECLARATION)
    return "\n".join(out)


def _has_counted_alloca(ir_text: str) -> bool:
    """Whether any alloca carries a count, checking only alloca lines."""
    index = ir_text.find("= alloca ")
    while index >= 0:
        line_start = ir_text.rfind("\n", 0, index) + 1
        line_end = ir_text.find("\n", index)
        if line_end < 0:
            line_end = len(ir_text)
        if _rewrite_alloca_line(ir_text[line_start:line_end], ".pccdyn"):
            return True
        index = ir_text.find("= alloca ", line_end)
    return False


def _rewrite_alloca_line(line: str, marker: str) -> list[str]:
    """Replacement lines for a counted alloca, or ``[]`` to keep ``line``."""
    stripped = line.strip()
    if not stripped.startswith("%"):
        return []
    name_end = _symbol_end(stripped, 0)
    if not stripped.startswith(" = alloca ", name_end):
        return []
    result = stripped[:name_end]
    rest = stripped[name_end + len(" = alloca "):].lstrip()
    if rest.startswith("inalloca "):
        return []
    element_type, rest = _split_leading_type(rest)
    if not rest.startswith(","):
        return []
    items = _split_top_level(rest[1:])
    if not items or items[0].startswith("align ") or items[0].startswith("addrspace("):
        return []
    count_type, count = _split_leading_type(items[0])
    if not count_type.startswith("i") or not count:
        return []
    tail = ""
    align = 16
    for item in items[1:]:
        tail = tail + ", " + item
        if item.startswith("align ") and _is_int_literal(item[len("align "):].strip()):
            align = int(item[len("align "):].strip())
    indent = line[:len(line) - len(line.lstrip())]
    if _is_int_literal(count):
        elements = int(count)
        if elements < 1:
            elements = 1
        return [indent + result + " = alloca [" + str(elements) + " x " + element_type + "]" + tail]
    end = _derived(result, marker + "end")
    size = _derived(result, marker + "bytes")
    if align < 16:
        align = 16
    return [
        indent + end + " = getelementptr " + element_type + ", ptr null, " + count_type + " " + count,
        indent + size + " = ptrtoint ptr " + end + " to i64",
        indent + result + " = call ptr @" + DYNAMIC_ALLOCA_INTRINSIC
        + "(i64 " + size + ", i64 " + str(align) + ")",
    ]


def _derived(name: str, suffix: str) -> str:
    if name.startswith('%"'):
        return name[:-1] + suffix + '"'
    if _is_int_literal(name[1:]):
        return '%"' + name[1:] + suffix + '"'
    return name + suffix
