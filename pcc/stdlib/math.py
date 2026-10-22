"""pcc.stdlib.math — libm-backed replacement for ``math``.

Transcendental functions wrap platform libm entry points declared via
:mod:`pcc.extern`; arithmetic and protocol helpers have owned Python bodies.
"""

from __future__ import annotations

from pcc.extern import extern, c_double, c_int, c_int64

# Transcendentals
sqrt_c: "extern" = extern("sqrt", (c_double,), c_double)
pow_c: "extern" = extern("pow", (c_double, c_double), c_double)
exp_c: "extern" = extern("exp", (c_double,), c_double)
log_c: "extern" = extern("log", (c_double,), c_double)
log2_c: "extern" = extern("log2", (c_double,), c_double)
log10_c: "extern" = extern("log10", (c_double,), c_double)
sin_c: "extern" = extern("sin", (c_double,), c_double)
cos_c: "extern" = extern("cos", (c_double,), c_double)
tan_c: "extern" = extern("tan", (c_double,), c_double)
floor_c: "extern" = extern("floor", (c_double,), c_double)
ceil_c: "extern" = extern("ceil", (c_double,), c_double)
fabs_c: "extern" = extern("fabs", (c_double,), c_double)
fmod_c: "extern" = extern("fmod", (c_double, c_double), c_double)


# Constants (IEEE 754 double). The values are the same libm constants
# math.pi / math.e produce; keeping them inline means the module needs
# no runtime init.
pi: float = 3.141592653589793
e: float = 2.718281828459045
tau: float = 6.283185307179586
inf: float = float("inf")
nan: float = float("nan")


def sqrt(x: float) -> float:
    return float(sqrt_c(x))


def pow(x: float, y: float) -> float:
    return float(pow_c(x, y))


def exp(x: float) -> float:
    return float(exp_c(x))


def log(x: float) -> float:
    return float(log_c(x))


def log2(x: float) -> float:
    return float(log2_c(x))


def log10(x: float) -> float:
    return float(log10_c(x))


def sin(x: float) -> float:
    return float(sin_c(x))


def cos(x: float) -> float:
    return float(cos_c(x))


def tan(x: float) -> float:
    return float(tan_c(x))


def floor(x: float) -> int:
    return int(floor_c(x))


def ceil(x: float) -> int:
    return int(ceil_c(x))


def fabs(x: float) -> float:
    return float(fabs_c(x))


def fmod(x: float, y: float) -> float:
    return float(fmod_c(x, y))


def isnan(x: float) -> bool:
    return x != x


def isinf(x: float) -> bool:
    # Keep the wrapper independent of module-global loads: the low-IR scalar
    # path does not yet admit those loads, while ``1e309`` is parsed as the
    # same IEEE-754 infinity value used by the exported ``inf`` constant.
    return x == 1e309 or x == -1e309


def isfinite(x: float) -> bool:
    return not isnan(x) and not isinf(x)


def trunc(x: float) -> int:
    return int(x)


def modf(x, /) -> tuple[float, float]:
    """Return signed fractional and integral parts of a real number."""
    # float() additionally accepts numeric text; math's real-number protocol
    # accepts __float__ or __index__, but does not parse text or bytes.
    if not isinstance(x, (int, float)):
        if not hasattr(type(x), "__float__") and not hasattr(type(x), "__index__"):
            raise TypeError("must be real number, not " + type(x).__name__)
    value = float(x)
    if value != value:
        return value, value
    if value == 1e309:
        return 0.0, value
    if value == -1e309:
        return -0.0, value
    integral = float(int(value))
    fractional = value - integral
    if integral == 0.0:
        integral = value * 0.0
    if fractional == 0.0:
        fractional = value * 0.0
    return fractional, integral


def copysign(x: float, y: float) -> float:
    ax = x if x >= 0.0 else -x
    if y < 0.0 or (y == 0.0 and str(y).startswith("-")):
        return -ax
    return ax


def prod(values, start=1):
    out = start
    for value in values:
        out *= value
    return out


def factorial(n: int) -> int:
    if n < 0:
        raise ValueError("factorial() not defined for negative values")
    out = 1
    i = 2
    while i <= n:
        out *= i
        i += 1
    return out


def gcd(a: int, b: int = 0) -> int:
    if a < 0:
        a = -a
    if b < 0:
        b = -b
    while b != 0:
        a, b = b, a % b
    return a


def radians(x: float) -> float:
    return x * 3.141592653589793 / 180.0
