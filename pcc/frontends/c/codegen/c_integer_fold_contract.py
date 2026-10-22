"""Bit-precise integer rules for C compile-time expression folding.

The caller has already applied integer promotion/usual arithmetic conversion.
Undefined C operations are reported as ``poison`` so a frontend can reject the
constant expression instead of accidentally inheriting Python's arithmetic.
"""

from __future__ import annotations


FOLD_CONSTANT = "constant"
FOLD_POISON = "poison"
FOLD_UNSUPPORTED = "unsupported"

C_INTEGER_BINARY_OPS = (
    "+",
    "-",
    "*",
    "/",
    "%",
    "<<",
    ">>",
    "&",
    "|",
    "^",
    "==",
    "!=",
    "<",
    "<=",
    ">",
    ">=",
)


def _unsigned(value: int, width: int) -> int:
    return int(value) & ((1 << width) - 1)


def _signed(value: int, width: int) -> int:
    raw = _unsigned(value, width)
    sign = 1 << (width - 1)
    return raw - (1 << width) if raw & sign else raw


def _result(raw: int, width: int, is_unsigned: bool) -> int:
    raw = _unsigned(raw, width)
    return raw if is_unsigned else _signed(raw, width)


def fold_c_integer_unary(
    op: str,
    width: int,
    is_unsigned: bool,
    operand: int,
) -> tuple[str, int]:
    if width <= 0:
        return FOLD_UNSUPPORTED, 0
    value = _unsigned(operand, width) if is_unsigned else _signed(operand, width)
    if op == "+":
        return FOLD_CONSTANT, value
    if op == "-":
        if not is_unsigned and value == -(1 << (width - 1)):
            return FOLD_POISON, 0
        return FOLD_CONSTANT, _result(-value, width, is_unsigned)
    if op == "~":
        return FOLD_CONSTANT, _result(~_unsigned(value, width), width, is_unsigned)
    if op == "!":
        return FOLD_CONSTANT, 0 if value else 1
    return FOLD_UNSUPPORTED, 0


def fold_c_integer_binary(
    op: str,
    width: int,
    is_unsigned: bool,
    lhs: int,
    rhs: int,
) -> tuple[str, int]:
    """Fold already-promoted operands under pcc's documented C ABI."""

    if width <= 0:
        return FOLD_UNSUPPORTED, 0
    lhs_u = _unsigned(lhs, width)
    rhs_u = _unsigned(rhs, width)
    lhs_n = lhs_u if is_unsigned else _signed(lhs_u, width)
    rhs_n = rhs_u if is_unsigned else _signed(rhs_u, width)
    signed_min = -(1 << (width - 1))
    signed_max = (1 << (width - 1)) - 1

    if op in ("+", "-", "*"):
        if op == "+":
            result = lhs_n + rhs_n
        elif op == "-":
            result = lhs_n - rhs_n
        else:
            result = lhs_n * rhs_n
        if not is_unsigned and not (signed_min <= result <= signed_max):
            return FOLD_POISON, 0
        return FOLD_CONSTANT, _result(result, width, is_unsigned)

    if op in ("&", "|", "^"):
        if op == "&":
            result = lhs_u & rhs_u
        elif op == "|":
            result = lhs_u | rhs_u
        else:
            result = lhs_u ^ rhs_u
        return FOLD_CONSTANT, _result(result, width, is_unsigned)

    if op in ("/", "%"):
        if rhs_n == 0:
            return FOLD_POISON, 0
        if not is_unsigned and lhs_n == signed_min and rhs_n == -1:
            return FOLD_POISON, 0
        if is_unsigned:
            quotient = lhs_u // rhs_u
            remainder = lhs_u % rhs_u
        else:
            quotient = abs(lhs_n) // abs(rhs_n)
            if (lhs_n < 0) != (rhs_n < 0):
                quotient = -quotient
            remainder = lhs_n - quotient * rhs_n
        return FOLD_CONSTANT, quotient if op == "/" else remainder

    if op in ("<<", ">>"):
        # The shift count is the promoted RHS value, not converted to the LHS
        # type.  Callers therefore pass its signed/unsigned numeric value here.
        shift = int(rhs)
        if shift < 0 or shift >= width:
            return FOLD_POISON, 0
        if op == "<<":
            if not is_unsigned and lhs_n < 0:
                return FOLD_POISON, 0
            result = lhs_u << shift if is_unsigned else lhs_n << shift
            if not is_unsigned and not (signed_min <= result <= signed_max):
                return FOLD_POISON, 0
            return FOLD_CONSTANT, _result(result, width, is_unsigned)
        if is_unsigned:
            return FOLD_CONSTANT, lhs_u >> shift
        # pcc's C runtime lowering selects arithmetic shift for signed ints.
        return FOLD_CONSTANT, lhs_n >> shift

    if op in ("==", "!=", "<", "<=", ">", ">="):
        if op == "==":
            result = lhs_n == rhs_n
        elif op == "!=":
            result = lhs_n != rhs_n
        elif op == "<":
            result = lhs_n < rhs_n
        elif op == "<=":
            result = lhs_n <= rhs_n
        elif op == ">":
            result = lhs_n > rhs_n
        else:
            result = lhs_n >= rhs_n
        return FOLD_CONSTANT, 1 if result else 0

    return FOLD_UNSUPPORTED, 0
