"""InstructionSimplify (subset) — IR-level pass.

Upstream reference:

- ``/tmp/llvm-src/llvm-20.1.8.src/lib/Analysis/InstructionSimplify.cpp``
  implements the full simplifier. The entry point
  :cpp:func:`llvm::simplifyInstruction` dispatches on opcode and
  calls per-op helpers (``SimplifyAddInst``, ``SimplifyAndInst``, ...),
  each returning a replacement ``Value *`` or nullptr. The pass
  wrapper :cpp:class:`llvm::InstSimplifyPass`
  (``.../Transforms/Scalar/InstSimplifyPass.cpp``) walks each block
  in postorder and replaces every instruction that simplifies.

The subset implemented here mirrors the pure-arithmetic identity
short-circuits upstream returns *early*, without requiring the
full recursive simplifier:

    Arithmetic: x+0, x-0, x-x, x*0, x*1, x*-1 (→ neg)
                constant/constant integer folding for the covered binops
    Bitwise:    x&x, x|x, x^x, x&-1, x&0, x|0, x|-1, x^-1 (→ ~x placeholder)
    Shifts:     x<<0, x>>0, 0<<x, 0>>x, and constant/constant shifts
                with W ≥ bit-width → poison
    Compares:   eq/ne on equal constants, eq on same SSA value, etc.
    Selects:    select true,x,y → x; select false,x,y → y;
                select c,x,x → x.

Unsupported transformations leave their input unchanged. Upstream
``opt -passes=instsimplify`` is an external reference only, never an
execution fallback for this owned kernel.
"""

from __future__ import annotations

import re

from .text_tokens import replace_local_names

from .integer_fold_contract import (
    FOLD_CONSTANT,
    FOLD_POISON,
    fold_llvm_integer_binary,
    fold_llvm_integer_compare,
    signed_value,
    unsigned_value,
)


# ---------------------------------------------------------------------------
# Instruction line patterns
# ---------------------------------------------------------------------------


_BINOP_RE = re.compile(
    r"""
    ^(?P<indent>\s*)
    %(?P<result>[\w\.]+)\s*=\s*
    (?P<op>add|sub|mul|and|or|xor|shl|lshr|ashr|udiv|sdiv|urem|srem)
    (?P<flags>(?:\s+(?:nsw|nuw|exact))*)
    \s+(?P<ty>i\d+)\s+
    (?P<lhs>[^,\s][^,]*?)\s*,\s*
    (?P<rhs>.+?)\s*$
    """,
    re.VERBOSE,
)


_ICMP_RE = re.compile(
    r"""
    ^(?P<indent>\s*)
    %(?P<result>[\w\.]+)\s*=\s*icmp\s+
    (?P<pred>eq|ne|ugt|uge|ult|ule|sgt|sge|slt|sle)
    \s+(?P<ty>i\d+)\s+
    (?P<lhs>[^,\s][^,]*?)\s*,\s*
    (?P<rhs>.+?)\s*$
    """,
    re.VERBOSE,
)


_SELECT_RE = re.compile(
    r"""
    ^(?P<indent>\s*)
    %(?P<result>[\w\.]+)\s*=\s*select\s+
    i1\s+(?P<cond>[^,]+?)\s*,\s*
    (?P<ty1>[\w\*]+)\s+(?P<tval>[^,]+?)\s*,\s*
    (?P<ty2>[\w\*]+)\s+(?P<fval>.+?)\s*$
    """,
    re.VERBOSE,
)

_IDENTITY_BITCAST_RE = re.compile(
    r"^\s*%(?P<result>[\w.$-]+)\s*=\s*bitcast\s+"
    r"(?P<source_type>ptr|i\d+)\s+(?P<value>[^\s,]+)\s+to\s+"
    r"(?P<target_type>ptr|i\d+)\s*$"
)
_BOOL_EXTENSION_RE = re.compile(
    r"^\s*%(?P<result>[\w.$-]+)\s*=\s*zext\s+i1\s+"
    r"(?P<value>[^\s,]+)\s+to\s+(?P<type>i\d+)\s*$"
)
_INVERSE_INTEGER_PREDICATES = {
    "eq": "ne", "ne": "eq", "slt": "sge", "sle": "sgt",
    "sgt": "sle", "sge": "slt", "ult": "uge", "ule": "ugt",
    "ugt": "ule", "uge": "ult",
}


def _bit_width(ty: str) -> int:
    m = re.match(r"i(\d+)", ty)
    return int(m.group(1)) if m else 0


def _is(token: str, val: int | str) -> bool:
    return token.strip() == str(val)


def _try_int(token: str) -> object:
    token = token.strip()
    try:
        return int(token)
    except ValueError:
        return None


def _normalize_unsigned(value: object, ty: str) -> object:
    width = _bit_width(ty)
    if width <= 0:
        return value
    return unsigned_value(value, width)


def _normalize_signed(value: object, ty: str) -> object:
    width = _bit_width(ty)
    if width <= 0:
        return value
    return signed_value(value, width)


def _is_neg_one(token: str, ty: str) -> bool:
    t = token.strip()
    if t == "-1":
        return True
    if t == "true" and ty == "i1":
        return True
    w = _bit_width(ty)
    if w > 0 and t.lstrip("-").isdigit():
        try:
            val = _try_int(t)
            if val == unsigned_value(-1, w) or val == -1:
                return True
        except ValueError:
            pass
    return False


# ---------------------------------------------------------------------------
# Per-opcode simplification
# ---------------------------------------------------------------------------


def _fold_constant_binop(
    op: str,
    ty: str,
    lhs: str,
    rhs: str,
    flags="",
) -> str | None:
    li = _try_int(lhs)
    ri = _try_int(rhs)
    if li is None or ri is None:
        return None

    width = _bit_width(ty)
    if width <= 0:
        return None

    status, value = fold_llvm_integer_binary(op, width, li, ri, flags)
    if status == FOLD_POISON:
        return "poison"
    if status == FOLD_CONSTANT:
        return str(signed_value(value, width))
    return None


def _simplify_binop(
    op: str,
    ty: str,
    lhs: str,
    rhs: str,
    flags="",
) -> str | None:
    l, r = lhs.strip(), rhs.strip()
    const_folded = _fold_constant_binop(op, ty, l, r, flags)
    if const_folded is not None:
        return const_folded

    if op == "add":
        if _is(r, 0): return l
        if _is(l, 0): return r
    elif op == "sub":
        if _is(r, 0): return l
        if l == r and not l.isdigit() and not l.startswith("-"):
            return "0"
    elif op == "mul":
        if _is(r, 0) or _is(l, 0): return "0"
        if _is(r, 1): return l
        if _is(l, 1): return r
    elif op == "and":
        if _is(r, 0) or _is(l, 0): return "0"
        if _is_neg_one(r, ty): return l
        if _is_neg_one(l, ty): return r
        if l == r: return l
    elif op == "or":
        if _is(r, 0): return l
        if _is(l, 0): return r
        if _is_neg_one(r, ty): return r
        if _is_neg_one(l, ty): return l
        if l == r: return l
    elif op == "xor":
        if _is(r, 0): return l
        if _is(l, 0): return r
        if l == r: return "0"
    elif op in ("shl", "lshr", "ashr"):
        if _is(l, 0): return "0"
        if _is(r, 0): return l
        if op in ("lshr", "ashr") and l == r:
            return "0"
        if op == "ashr" and _is_neg_one(l, ty):
            return "-1"
        width = _bit_width(ty)
        shift = _try_int(r)
        if shift is not None and (shift < 0 or shift >= width):
            return "poison"
    elif op == "udiv":
        if _is(r, 0): return "poison"
        if _is(l, 0): return "0"
        if _is(r, 1): return l
        if l == r: return "1"
    elif op == "sdiv":
        if _is(r, 0): return "poison"
        if _is(l, 0): return "0"
        if _is(r, 1): return l
        if l == r: return "1"
    elif op == "urem":
        if _is(r, 0): return "poison"
        if _is(l, 0): return "0"
        if _is(r, 1): return "0"
        if l == r: return "0"
    elif op == "srem":
        if _is(r, 0): return "poison"
        if _is(l, 0): return "0"
        if _is(r, 1): return "0"
        if l == r: return "0"
    return None


def _simplify_icmp(pred: str, ty: str, lhs: str, rhs: str) -> str | None:
    """Return the 1-bit simplified result, or None."""
    l, r = lhs.strip(), rhs.strip()
    # Both sides identical → pred determines result.
    if l == r:
        if pred in ("eq", "sle", "sge", "ule", "uge"):
            return "true"
        if pred in ("ne", "slt", "sgt", "ult", "ugt"):
            return "false"
    # Both constants → fold via Python.
    li = _try_int(l)
    ri = _try_int(r)
    if li is None or ri is None:
        return None
    w = _bit_width(ty) or 32
    status, value = fold_llvm_integer_compare(pred, w, li, ri)
    if status != FOLD_CONSTANT:
        return None
    return "true" if value else "false"


def _simplify_select(cond: str, tval: str, fval: str) -> str | None:
    c = cond.strip()
    if c == "true":
        return tval.strip()
    if c == "false":
        return fval.strip()
    if tval.strip() == fval.strip():
        return tval.strip()
    return None


# ---------------------------------------------------------------------------
# Pass wrapper — textual rewrite with fixed-point substitution
# ---------------------------------------------------------------------------




def simplify_module_text(ir_text: str) -> tuple[str, bool]:
    """Run the simplifier until fixed point; return (new_ir, changed)."""
    current = ir_text
    any_change = False
    for _ in range(16):
        next_text, changed = _one_pass(current)
        if not changed:
            break
        any_change = True
        current = next_text
    return current, any_change


def _rewrite_function_text(fn_text: str) -> tuple[str, bool]:
    replacements: dict[str, str] = {}
    kept: list[str] = []
    changed = False
    # Collect definitions independently of textual block order. LLVM SSA
    # dominance does not require a defining block to appear earlier in text.
    boolean_extensions: dict[str, tuple[str, str]] = {}
    integer_comparisons: dict[str, tuple[str, str, str, str]] = {}
    for line in fn_text.splitlines():
        if "zext i1 " in line:
            extension = _BOOL_EXTENSION_RE.match(line)
            if extension is not None:
                boolean_extensions["%" + extension.group("result")] = (
                    extension.group("type"), extension.group("value"),
                )
        if "icmp " in line:
            comparison = _ICMP_RE.match(line)
            if comparison is not None:
                integer_comparisons["%" + comparison.group("result")] = (
                    comparison.group("pred"), comparison.group("ty"),
                    comparison.group("lhs"), comparison.group("rhs"),
                )

    for line in fn_text.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        if "bitcast " in stripped:
            identity = _IDENTITY_BITCAST_RE.match(stripped)
            if identity is not None and identity.group("source_type") == identity.group("target_type"):
                replacements[identity.group("result")] = identity.group("value")
                changed = True
                continue
        m = _BINOP_RE.match(stripped)
        if m:
            if m.group("op") == "xor" and m.group("ty") == "i1" and not m.group("flags"):
                left, right = m.group("lhs").strip(), m.group("rhs").strip()
                compared = ""
                if left in ("true", "1"):
                    compared = right
                elif right in ("true", "1"):
                    compared = left
                operands = integer_comparisons.get(compared)
                if operands is not None:
                    kept.append(m.group("indent") + "%" + m.group("result") + " = icmp "
                                + _INVERSE_INTEGER_PREDICATES[operands[0]] + " "
                                + operands[1] + " " + operands[2] + ", " + operands[3] + "\n")
                    changed = True
                    continue
            rep = _simplify_binop(
                m.group("op"), m.group("ty"),
                m.group("lhs"), m.group("rhs"), m.group("flags"),
            )
            if rep is not None:
                replacements[m.group("result")] = rep
                changed = True
                continue
            kept.append(line)
            continue

        m = _ICMP_RE.match(stripped)
        if m:
            predicate = m.group("pred")
            lhs, rhs = m.group("lhs").strip(), m.group("rhs").strip()
            if predicate in ("eq", "ne"):
                extension = boolean_extensions.get(lhs)
                constant = rhs
                if extension is None:
                    extension = boolean_extensions.get(rhs)
                    constant = lhs
                if extension is not None and extension[0] == m.group("ty") and constant in ("0", "1"):
                    inverted = (predicate == "eq") == (constant == "0")
                    if inverted:
                        kept.append(m.group("indent") + "%" + m.group("result")
                                    + " = xor i1 " + extension[1] + ", true\n")
                    else:
                        replacements[m.group("result")] = extension[1]
                    changed = True
                    continue
            rep = _simplify_icmp(
                m.group("pred"), m.group("ty"),
                m.group("lhs"), m.group("rhs"),
            )
            if rep is not None:
                replacements[m.group("result")] = rep
                changed = True
                continue
            kept.append(line)
            continue

        m = _SELECT_RE.match(stripped)
        if m:
            rep = _simplify_select(
                m.group("cond"), m.group("tval"), m.group("fval"),
            )
            if rep is not None:
                replacements[m.group("result")] = rep
                changed = True
                continue
            kept.append(line)
            continue

        kept.append(line)

    if not changed:
        return fn_text, False

    # Resolve alias chains before one lexical replacement. A fixed number of
    # text sweeps can leave dangling names after deleting a long cast chain;
    # regex word boundaries can also corrupt %name.suffix or inline assembly.
    resolved: dict[str, str] = {}
    for name in replacements:
        if name in resolved:
            continue
        chain = [name]
        rep = replacements[name]
        while rep.startswith("%") and rep[1:] in replacements:
            next_name = rep[1:]
            if next_name in resolved:
                rep = resolved[next_name]
                break
            if len(chain) > len(replacements):
                # Invalid cyclic aliases must not make this optimizer loop or
                # erase their definitions before the verifier diagnoses them.
                return fn_text, False
            chain.append(next_name)
            rep = replacements[next_name]
        for alias in chain:
            resolved[alias] = rep
    return replace_local_names("".join(kept), resolved), True


def _one_pass(ir_text: str) -> tuple[str, bool]:
    out: list[str] = []
    changed = False
    in_function = False
    fn_lines: list[str] = []

    for line in ir_text.splitlines(keepends=True):
        stripped = line.lstrip()
        if not in_function and stripped.startswith("define "):
            in_function = True
            fn_lines = [line]
            continue
        if in_function:
            fn_lines.append(line)
            if stripped.startswith("}"):
                rewritten, fn_changed = _rewrite_function_text("".join(fn_lines))
                out.append(rewritten)
                changed = changed or fn_changed
                in_function = False
                fn_lines = []
            continue
        out.append(line)

    if in_function and fn_lines:
        rewritten, fn_changed = _rewrite_function_text("".join(fn_lines))
        out.append(rewritten)
        changed = changed or fn_changed

    if not changed:
        return ir_text, False
    return "".join(out), True
