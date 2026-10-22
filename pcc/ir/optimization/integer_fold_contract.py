"""Bit-precise contract shared by pcc's integer constant folders.

The helpers return ``("constant", raw_bits)``, ``("poison", 0)``, or
``("unsupported", 0)``.  ``raw_bits`` is always the unsigned bit pattern for
the requested width.  Consumers choose their preferred textual spelling.

This is intentionally not a general optimizer.  It is the finite semantic
kernel used by SCCP, instsimplify, instcombine, reassociate and loop-unroll
when both operands are integer constants.
"""

from __future__ import annotations


FOLD_CONSTANT = "constant"
FOLD_POISON = "poison"
FOLD_UNSUPPORTED = "unsupported"

LLVM_INTEGER_BINARY_OPS = (
    "add",
    "sub",
    "mul",
    "and",
    "or",
    "xor",
    "shl",
    "lshr",
    "ashr",
    "udiv",
    "sdiv",
    "urem",
    "srem",
)
LLVM_INTEGER_COMPARE_PREDS = (
    "eq",
    "ne",
    "ult",
    "ule",
    "ugt",
    "uge",
    "slt",
    "sle",
    "sgt",
    "sge",
)


def power_of_two(exponent: int) -> object:
    # Integer payloads need the object projection: masks and intermediates can
    # exceed the target width. The legacy pcc.* scaffold uses machine ints for
    # annotated int parameters/locals; a mask such as (1 << 64) must not wrap.
    one: object = 1
    return one << exponent


def unsigned_value(value: object, width: int) -> object:
    if width <= 0:
        raise ValueError("integer fold width must be positive")
    return int(value) & (power_of_two(width) - 1)


def signed_value(value: object, width: int) -> object:
    raw = unsigned_value(value, width)
    sign_bit = power_of_two(width - 1)
    return raw - power_of_two(width) if raw & sign_bit else raw


def fold_llvm_integer_binary(
    op: str,
    width: int,
    lhs: object,
    rhs: object,
    flags=(),
) -> tuple[str, object]:
    """Evaluate one LLVM integer binop, including poison-producing flags."""

    if width <= 0:
        return FOLD_UNSUPPORTED, 0
    normalized_flags = _normalized_flags(flags)
    if not _flags_valid_for_op(op, normalized_flags):
        return FOLD_UNSUPPORTED, 0
    mask = power_of_two(width) - 1
    lhs_u = int(lhs) & mask
    rhs_u = int(rhs) & mask
    lhs_s = signed_value(lhs_u, width)
    rhs_s = signed_value(rhs_u, width)
    signed_min = -power_of_two(width - 1)
    signed_max = power_of_two(width - 1) - 1

    if op in ("add", "sub", "mul"):
        if op == "add":
            unsigned_math = lhs_u + rhs_u
            signed_math = lhs_s + rhs_s
        elif op == "sub":
            unsigned_math = lhs_u - rhs_u
            signed_math = lhs_s - rhs_s
        else:
            unsigned_math = lhs_u * rhs_u
            signed_math = lhs_s * rhs_s
        if "nuw" in normalized_flags and not (0 <= unsigned_math <= mask):
            return FOLD_POISON, 0
        if "nsw" in normalized_flags and not (
            signed_min <= signed_math <= signed_max
        ):
            return FOLD_POISON, 0
        return FOLD_CONSTANT, unsigned_math & mask

    if op == "and":
        return FOLD_CONSTANT, lhs_u & rhs_u
    if op == "or":
        return FOLD_CONSTANT, lhs_u | rhs_u
    if op == "xor":
        return FOLD_CONSTANT, lhs_u ^ rhs_u

    if op in ("shl", "lshr", "ashr"):
        shift = rhs_u
        if shift >= width:
            return FOLD_POISON, 0
        if op == "shl":
            unsigned_math = lhs_u << shift
            signed_math = lhs_s * power_of_two(shift)
            if "nuw" in normalized_flags and unsigned_math > mask:
                return FOLD_POISON, 0
            if "nsw" in normalized_flags and not (
                signed_min <= signed_math <= signed_max
            ):
                return FOLD_POISON, 0
            return FOLD_CONSTANT, unsigned_math & mask
        discarded_mask = power_of_two(shift) - 1 if shift > 0 else 0
        if "exact" in normalized_flags and lhs_u & discarded_mask:
            return FOLD_POISON, 0
        if op == "lshr":
            return FOLD_CONSTANT, lhs_u >> shift
        return FOLD_CONSTANT, unsigned_value(lhs_s >> shift, width)

    if op in ("udiv", "urem"):
        if rhs_u == 0:
            return FOLD_POISON, 0
        if op == "udiv":
            if "exact" in normalized_flags and lhs_u % rhs_u != 0:
                return FOLD_POISON, 0
            return FOLD_CONSTANT, lhs_u // rhs_u
        return FOLD_CONSTANT, lhs_u % rhs_u

    if op in ("sdiv", "srem"):
        if rhs_s == 0:
            return FOLD_POISON, 0
        if op == "sdiv" and lhs_s == signed_min and rhs_s == -1:
            return FOLD_POISON, 0
        quotient = abs(lhs_s) // abs(rhs_s)
        if (lhs_s < 0) != (rhs_s < 0):
            quotient = -quotient
        remainder = lhs_s - quotient * rhs_s
        if op == "sdiv":
            if "exact" in normalized_flags and remainder != 0:
                return FOLD_POISON, 0
            return FOLD_CONSTANT, unsigned_value(quotient, width)
        # Unlike sdiv, INT_MIN srem -1 has the representable result zero.
        return FOLD_CONSTANT, unsigned_value(remainder, width)

    return FOLD_UNSUPPORTED, 0


def fold_llvm_integer_compare(
    pred: str,
    width: int,
    lhs: object,
    rhs: object,
) -> tuple[str, int]:
    if width <= 0 or pred not in LLVM_INTEGER_COMPARE_PREDS:
        return FOLD_UNSUPPORTED, 0
    lhs_u = unsigned_value(lhs, width)
    rhs_u = unsigned_value(rhs, width)
    lhs_s = signed_value(lhs_u, width)
    rhs_s = signed_value(rhs_u, width)
    if pred == "eq":
        result = lhs_u == rhs_u
    elif pred == "ne":
        result = lhs_u != rhs_u
    elif pred == "ult":
        result = lhs_u < rhs_u
    elif pred == "ule":
        result = lhs_u <= rhs_u
    elif pred == "ugt":
        result = lhs_u > rhs_u
    elif pred == "uge":
        result = lhs_u >= rhs_u
    elif pred == "slt":
        result = lhs_s < rhs_s
    elif pred == "sle":
        result = lhs_s <= rhs_s
    elif pred == "sgt":
        result = lhs_s > rhs_s
    else:
        result = lhs_s >= rhs_s
    return FOLD_CONSTANT, 1 if result else 0


def _normalized_flags(flags) -> frozenset[str]:
    if isinstance(flags, str):
        values = flags.split()
    else:
        values = flags
    out: set[str] = set()
    for flag in values:
        text = str(flag).strip().lower()
        if text:
            out.add(text)
    return frozenset(out)


def _flags_valid_for_op(op: str, flags: frozenset[str]) -> bool:
    if op in ("add", "sub", "mul", "shl"):
        return flags <= frozenset({"nsw", "nuw"})
    if op in ("lshr", "ashr", "udiv", "sdiv"):
        return flags <= frozenset({"exact"})
    return not flags
