"""Exact binary floating constants shared by owned C and backend emission.

All rounding uses integer arithmetic. Host binary64 is never an intermediate
for extended/quad literals. Values returned here are bit patterns, not host
floating point objects; runtime arithmetic has its own fixed-limb helpers.
"""


def _format(width: int) -> tuple[int, int, int, int]:
    # precision, exponent bits, exponent bias, explicit integer bit
    if width == 16:
        return 11, 5, 15, 0
    if width == 32:
        return 24, 8, 127, 0
    if width == 64:
        return 53, 11, 1023, 0
    if width == 80:
        return 64, 15, 16383, 1
    if width == 128:
        return 113, 15, 16383, 0
    raise ValueError("unsupported binary floating width: " + str(width))


def _pack(sign: int, exponent: int, significand: int, width: int) -> int:
    precision, exponent_bits, _bias, explicit = _format(width)
    stored = precision if explicit else precision - 1
    fraction = significand & ((1 << stored) - 1)
    return (sign << (stored + exponent_bits)) | (exponent << stored) | fraction


def infinity_bits(width: int, negative: bool = False) -> int:
    precision, exponent_bits, _bias, explicit = _format(width)
    return _pack(int(negative), (1 << exponent_bits) - 1,
                 (1 << (precision - 1)) if explicit else 0, width)


def nan_bits(width: int, negative: bool = False) -> int:
    precision, exponent_bits, _bias, explicit = _format(width)
    significand = 1 << (precision - 2)
    if explicit:
        significand |= 1 << (precision - 1)
    return _pack(int(negative), (1 << exponent_bits) - 1, significand, width)


def _rounded_ratio(numerator: int, denominator: int, shift: int) -> int:
    if shift >= 0:
        numerator <<= shift
    else:
        denominator <<= -shift
    quotient, remainder = divmod(numerator, denominator)
    twice = remainder * 2
    if twice > denominator or (twice == denominator and quotient & 1):
        quotient += 1
    return quotient


def encode_ratio(numerator: int, denominator: int, width: int,
                 negative: bool = False) -> int:
    """Round an exact rational to nearest, ties to even, retaining signed zero."""
    precision, _exponent_bits, bias, _explicit = _format(width)
    if denominator <= 0:
        raise ValueError("floating rational denominator must be positive")
    sign = int(negative)
    if numerator < 0:
        numerator = -numerator
        sign ^= 1
    if numerator == 0:
        return _pack(sign, 0, 0, width)
    exponent = numerator.bit_length() - denominator.bit_length()
    if exponent >= 0:
        if numerator < denominator << exponent:
            exponent -= 1
    elif numerator << -exponent < denominator:
        exponent -= 1
    minimum = 1 - bias
    if exponent > bias:
        return infinity_bits(width, bool(sign))
    if exponent < minimum:
        # Keep the subnormal quantum fixed, including values that round into
        # the smallest normal number or down to a signed zero.
        significand = _rounded_ratio(numerator, denominator, precision - 1 - minimum)
        normal = significand >= 1 << (precision - 1)
        return _pack(sign, 1 if normal else 0, significand, width)
    significand = _rounded_ratio(numerator, denominator, precision - 1 - exponent)
    if significand >= 1 << precision:
        significand >>= 1
        exponent += 1
        if exponent > bias:
            return infinity_bits(width, bool(sign))
    return _pack(sign, exponent + bias, significand, width)


def decode_float_bits(bits: int, width: int) -> tuple[int, int, int, str]:
    """Return sign, nonnegative numerator, denominator, and ''/'inf'/'nan'."""
    precision, exponent_bits, bias, explicit = _format(width)
    stored = precision if explicit else precision - 1
    if bits < 0 or bits >= 1 << width:
        raise ValueError("floating bit pattern is outside its declared width")
    sign = (bits >> (stored + exponent_bits)) & 1
    exponent = (bits >> stored) & ((1 << exponent_bits) - 1)
    significand = bits & ((1 << stored) - 1)
    if exponent == (1 << exponent_bits) - 1:
        fraction = significand & ((1 << (precision - 1)) - 1)
        return sign, 0, 1, "nan" if fraction else "inf"
    if exponent and not explicit:
        significand |= 1 << (precision - 1)
    binary_exponent = (exponent if exponent else 1) - bias - (precision - 1)
    if binary_exponent >= 0:
        return sign, significand << binary_exponent, 1, ""
    return sign, significand, 1 << -binary_exponent, ""


def encode_float_bits(text: str, width: int) -> int:
    """Encode decimal/C hexadecimal literals or LLVM hexadecimal constants."""
    _format(width)
    value = str(text).strip()
    if value in ("zeroinitializer", "undef", "poison"):
        return 0
    if value.startswith(("0xK", "0xk")):
        if width != 80 or len(value) != 23:
            raise ValueError("x86_fp80 constant must contain twenty hexadecimal digits")
        return int(value[3:], 16)
    if value.startswith(("0xL", "0xl")):
        if width != 128 or len(value) != 35:
            raise ValueError("fp128 constant must contain thirty-two hexadecimal digits")
        # LLVM writes the low 64-bit word first, then the high word.
        return int(value[3:19], 16) | (int(value[19:], 16) << 64)
    sign = False
    if value.startswith(("-", "+")):
        sign = value[0] == "-"
        value = value[1:]
    lower = value.lower()
    if lower in ("inf", "infinity"):
        return infinity_bits(width, sign)
    if lower == "nan" or (lower.startswith("nan(") and lower.endswith(")")):
        return nan_bits(width, sign)
    if lower.startswith("0x") and "p" not in lower and "." not in lower:
        bits = int(lower[2:], 16)
        if width == 64:
            if bits >= 1 << 64:
                raise ValueError("LLVM binary64 constant exceeds 64 bits")
            return bits ^ (int(sign) << 63)
        if width == 32:
            source_sign, numerator, denominator, special = decode_float_bits(bits, 64)
            negative = bool(source_sign) != sign
            if special == "inf":
                return infinity_bits(width, negative)
            if special == "nan":
                # IR tokens preserve the NaN sign, quiet bit and payload.
                # Keep the binary32 significand's high 23 bits directly;
                # constructing a host NaN can change all three properties.
                fraction = (bits & ((1 << 52) - 1)) >> 29
                if fraction == 0:
                    # A payload entirely below binary32 precision must still
                    # denote NaN rather than becoming infinity.
                    fraction = 1
                return _pack(int(negative), 255, fraction, width)
            return encode_ratio(numerator, denominator, width, negative)
        raise ValueError("extended LLVM constant needs 0xK or 0xL marker")
    if lower.endswith(("f", "l")):
        lower = lower[:-1]
    if lower.startswith("0x"):
        mantissa, marker, exponent_text = lower[2:].partition("p")
        if not marker:
            raise ValueError("C hexadecimal floating literal needs a binary exponent")
        whole, dot, fraction = mantissa.partition(".")
        digits = whole + fraction
        numerator = int(digits or "0", 16)
        exponent = int(exponent_text) - 4 * len(fraction)
        order = numerator.bit_length() + exponent - 1
        if numerator and order > 17000:
            return infinity_bits(width, sign)
        if not numerator or order < -17000:
            return _pack(int(sign), 0, 0, width)
        if exponent >= 0:
            return encode_ratio(numerator << exponent, 1, width, sign)
        return encode_ratio(numerator, 1 << -exponent, width, sign)
    mantissa, marker, exponent_text = lower.partition("e")
    exponent = int(exponent_text) if marker else 0
    whole, dot, fraction = mantissa.partition(".")
    digits = (whole + fraction).lstrip("0")
    if not digits:
        return _pack(int(sign), 0, 0, width)
    exponent -= len(fraction)
    order = len(digits) + exponent - 1
    if order > 5000:
        return infinity_bits(width, sign)
    if order < -6000:
        return _pack(int(sign), 0, 0, width)
    numerator = int(digits, 10)
    if exponent >= 0:
        return encode_ratio(numerator * 10 ** exponent, 1, width, sign)
    return encode_ratio(numerator, 10 ** -exponent, width, sign)


def float_bytes(text: str, width: int) -> bytes:
    """Return little-endian ABI storage with deterministic fp80 padding."""
    size = 16 if width in (80, 128) else width // 8
    return encode_float_bits(text, width).to_bytes(size, "little")


def llvm_float_literal(bits: int, width: int) -> str:
    """Canonical LLVM spelling for the two extended types."""
    if width == 80:
        return "0xK" + format(bits, "020X")
    if width == 128:
        low = bits & ((1 << 64) - 1)
        return "0xL" + format(low, "016X") + format(bits >> 64, "016X")
    raise ValueError("extended LLVM literal requires 80 or 128 bits")
