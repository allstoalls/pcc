"""Scalar semantics shared by array models and the native array CLI.

Floating values use Python float operations and round-trip repr text. Integer
values remain integer objects, including while applying fixed-width dtype wrap.
The token adapter never substitutes decimal fixed point for a floating value.
"""

import math


def _require_integer(value: object) -> None:
    if not isinstance(value, int):
        raise TypeError("PCC-ARRAY-INTEGER-CARRIER-REQUIRED")


def wrap_integer(value: object, bits: int, signed: bool) -> object:
    """Wrap an already parsed integer; conversion belongs to the caller."""
    _require_integer(value)
    if bits not in (8, 16, 32, 64):
        raise ValueError("PCC-ARRAY-INTEGER-WIDTH-UNSUPPORTED")
    raw: object = value
    modulo: object = 1
    modulo = modulo << bits
    raw = raw % modulo
    sign: object = modulo >> 1
    if signed and raw >= sign:
        raw = raw - modulo
    return raw


def coerce_float(value: object) -> float:
    return float(value)


def float_binary(op: str, left: float, right: float) -> float:
    if op == "add":
        return left + right
    if op == "sub":
        return left - right
    if op == "mul":
        return left * right
    if op == "div":
        return left / right
    raise ValueError("PCC-ARRAY-UFUNC-UNSUPPORTED")


def float_unary(op: str, value: float) -> float:
    if op == "neg":
        return -value
    if op == "abs":
        return abs(value)
    raise ValueError("PCC-ARRAY-UNARY-UNSUPPORTED")


def float_compare(op: str, left: float, right: float) -> bool:
    if op == "eq":
        return left == right
    if op == "ne":
        return left != right
    if op == "lt":
        return left < right
    if op == "le":
        return left <= right
    if op == "gt":
        return left > right
    if op == "ge":
        return left >= right
    raise ValueError("PCC-ARRAY-COMPARE-UNSUPPORTED")


def float_sum(values: list[float]) -> float:
    """Compensated float accumulation, shared by both array front doors.

    Keep the rounding residual separately, adding it only when it is finite.
    This retains infinities/NaNs and the zero sign of Python's initial 0 sum.
    """
    total: float = 0.0
    correction: float = 0.0
    index = 0
    while index < len(values):
        value: float = values[index]
        updated: float = total + value
        if abs(total) >= abs(value):
            correction = correction + ((total - updated) + value)
        else:
            correction = correction + ((value - updated) + total)
        total = updated
        index += 1
    if correction != 0.0 and math.isfinite(correction):
        return total + correction
    return total


def token_is_float(token: str) -> bool:
    if token == "True" or token == "False":
        return False
    return (
        "." in token
        or "e" in token
        or "E" in token
        or token.lower() in ("nan", "+nan", "-nan", "inf", "+inf", "-inf")
    )


def token_float(token: str) -> float:
    if token == "True":
        return 1.0
    if token == "False":
        return 0.0
    return float(token)


def float_token(value: float) -> str:
    if math.isnan(value):
        return "nan"
    if math.isinf(value):
        return "inf" if value > 0.0 else "-inf"
    return repr(value)


def token_integer(token: str) -> object:
    if token == "True":
        return 1
    if token == "False":
        return 0
    # The scalar carrier is explicitly boxed. A raw-i64 temporary cannot
    # represent the unsigned64 boundary or an intermediate before dtype wrap.
    value: object = 0
    if token_is_float(token):
        value = int(token_float(token))
    else:
        value = int(token)
    return value


def token_truth(token: str) -> bool:
    if token_is_float(token):
        return token_float(token) != 0.0
    return token_integer(token) != 0


def cast_token(token: str, dtype: str) -> str:
    if dtype == "object":
        return token
    if dtype == "float32" or dtype == "float64":
        return float_token(token_float(token))
    if dtype == "bool":
        return "True" if token_truth(token) else "False"
    signed = dtype.startswith("int")
    width = dtype[3:] if signed else dtype[4:]
    if width not in ("8", "16", "32", "64"):
        raise ValueError("PCC-ARRAY-DTYPE-UNSUPPORTED")
    return str(wrap_integer(token_integer(token), int(width), signed))


def binary_token(left: str, right: str, op: str, dtype: str) -> str:
    if token_is_float(left) or token_is_float(right):
        result = float_binary(op, token_float(left), token_float(right))
        return cast_token(float_token(result), dtype)
    left_int: object = token_integer(left)
    right_int: object = token_integer(right)
    integer: object = 0
    if op == "add":
        integer = left_int + right_int
    elif op == "sub":
        integer = left_int - right_int
    elif op == "mul":
        integer = left_int * right_int
    elif op == "div":
        quotient: object = left_int / right_int
        return cast_token(float_token(float(quotient)), dtype)
    else:
        raise ValueError("PCC-ARRAY-UFUNC-UNSUPPORTED")
    return cast_token(str(integer), dtype)


def unary_token(token: str, op: str, dtype: str) -> str:
    if op == "logical_not":
        return "False" if token_truth(token) else "True"
    if token_is_float(token):
        return cast_token(float_token(float_unary(op, token_float(token))), dtype)
    value: object = token_integer(token)
    if op == "neg":
        value = -value
    elif op == "abs":
        value = abs(value)
    else:
        raise ValueError("PCC-ARRAY-UNARY-UNSUPPORTED")
    return cast_token(str(value), dtype)


def number_compare(op: str, left: object, right: object) -> bool:
    if op == "eq":
        return left == right
    if op == "ne":
        return left != right
    if op == "lt":
        return left < right
    if op == "le":
        return left <= right
    if op == "gt":
        return left > right
    if op == "ge":
        return left >= right
    raise ValueError("PCC-ARRAY-COMPARE-UNSUPPORTED")


def compare_tokens(left: str, right: str, op: str) -> bool:
    left_float = token_is_float(left)
    right_float = token_is_float(right)
    if left_float and right_float:
        return float_compare(op, token_float(left), token_float(right))
    first: object = 0
    second: object = 0
    if left_float:
        first = token_float(left)
    else:
        first = token_integer(left)
    if right_float:
        second = token_float(right)
    else:
        second = token_integer(right)
    return number_compare(op, first, second)


def reduce_tokens(values: list[str], kind: str, dtype: str) -> str:
    if not values:
        raise ValueError("PCC-ARRAY-REDUCE-EMPTY")
    if kind == "any" or kind == "all":
        truth = kind == "all"
        for value in values:
            if token_truth(value):
                if kind == "any":
                    truth = True
            elif kind == "all":
                truth = False
        return "True" if truth else "False"
    if kind == "min" or kind == "max":
        result = values[0]
        for value in values[1:]:
            if compare_tokens(value, result, "lt" if kind == "min" else "gt"):
                result = value
        return cast_token(result, dtype)
    if kind not in ("sum", "prod", "mean"):
        raise ValueError("PCC-ARRAY-REDUCE-UNSUPPORTED")
    floating = False
    for value in values:
        if token_is_float(value):
            floating = True
    if floating:
        numbers: list[float] = []
        for value in values:
            numbers.append(token_float(value))
        total: float = 1.0 if kind == "prod" else 0.0
        if kind == "prod":
            for number in numbers:
                total *= number
        else:
            total = float_sum(numbers)
        if kind == "mean":
            total /= len(values)
        return cast_token(float_token(total), dtype)
    integer: object = 1 if kind == "prod" else 0
    for value in values:
        current: object = token_integer(value)
        if kind == "prod":
            integer = integer * current
        else:
            integer = integer + current
    if kind == "mean":
        mean: object = integer / len(values)
        return cast_token(float_token(float(mean)), dtype)
    return cast_token(str(integer), dtype)
