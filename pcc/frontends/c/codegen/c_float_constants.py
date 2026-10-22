"""Exact target-rounded C extended floating constant expressions.

Only compiler-owned integer arithmetic is used. A wide literal never passes
through the host's binary64 representation before reaching its target format.
"""
from pcc.backend.wide_float import encode_float_bits, encode_ratio, decode_float_bits


class ConstFloatValue:
    def __init__(self, bits, width):
        self.bits = bits
        self.width = width

    @classmethod
    def parse(cls, text, width):
        return cls(encode_float_bits(text, width), width)

    @classmethod
    def convert(cls, value, width):
        if isinstance(value, ConstFloatValue):
            sign, numerator, denominator, special = decode_float_bits(value.bits, value.width)
            if special:
                return cls.parse(("-" if sign else "") + special, width)
            return cls(encode_ratio(numerator, denominator, width, negative=bool(sign)), width)
        if isinstance(value, float):
            # First recover the source binary64 value, then widen that value.
            return cls.convert(cls.parse(str(value), 64), width)
        return cls(encode_ratio(int(value), 1, width), width)

    def __str__(self):
        sign, numerator, denominator, special = decode_float_bits(self.bits, self.width)
        prefix = "-" if sign else ""
        if special:
            return prefix + special
        # The decoded denominator is a power of two. This spelling is exact
        # even for the smallest subnormal and preserves the sign of zero.
        exponent = 0
        while denominator > 1:
            denominator >>= 1
            exponent -= 1
        return prefix + "0x" + format(numerator, "x") + "p" + str(exponent)

    def __bool__(self):
        _, numerator, _, special = decode_float_bits(self.bits, self.width)
        return bool(numerator) or bool(special)

    def __int__(self):
        sign, numerator, denominator, special = decode_float_bits(self.bits, self.width)
        if special:
            raise ValueError("cannot convert non-finite C constant to integer")
        magnitude = numerator // denominator
        return -magnitude if sign else magnitude

    def __float__(self):
        import struct
        bits = ConstFloatValue.convert(self, 64).bits
        return struct.unpack("<d", bits.to_bytes(8, "little"))[0]

    def __neg__(self):
        return ConstFloatValue(self.bits ^ (1 << (self.width - 1)), self.width)

    def _pair(self, other):
        width = max(self.width, other.width) if isinstance(other, ConstFloatValue) else self.width
        lhs = ConstFloatValue.convert(self, width)
        rhs = ConstFloatValue.convert(other, width)
        return width, decode_float_bits(lhs.bits, width), decode_float_bits(rhs.bits, width)

    def binary(self, op, other):
        width, lhs, rhs = self._pair(other)
        ls, ln, ld, lk = lhs
        rs, rn, rd, rk = rhs
        if op == "-":
            rs ^= 1
            op = "+"
        if lk == "nan" or rk == "nan":
            return ConstFloatValue.parse("nan", width)
        if op == "+":
            if lk == "inf" or rk == "inf":
                if lk == rk and ls != rs:
                    return ConstFloatValue.parse("nan", width)
                sign = ls if lk else rs
                return ConstFloatValue.parse(("-" if sign else "") + "inf", width)
            numerator = (-ln if ls else ln) * rd + (-rn if rs else rn) * ld
            return ConstFloatValue(encode_ratio(numerator, ld * rd, width,
                                                negative=bool(ls and rs and numerator == 0)), width)
        if op == "*":
            if lk == "inf" or rk == "inf":
                if (not lk and ln == 0) or (not rk and rn == 0):
                    return ConstFloatValue.parse("nan", width)
                return ConstFloatValue.parse(("-" if ls ^ rs else "") + "inf", width)
            return ConstFloatValue(encode_ratio(ln * rn, ld * rd, width, negative=bool(ls ^ rs)), width)
        if op == "/":
            if (lk == rk == "inf") or (not lk and not rk and ln == rn == 0):
                return ConstFloatValue.parse("nan", width)
            if lk == "inf" or (not rk and rn == 0):
                return ConstFloatValue.parse(("-" if ls ^ rs else "") + "inf", width)
            if rk == "inf":
                return ConstFloatValue(encode_ratio(0, 1, width, negative=bool(ls ^ rs)), width)
            return ConstFloatValue(encode_ratio(ln * rd, ld * rn, width, negative=bool(ls ^ rs)), width)
        raise ValueError("invalid floating constant operator: " + op)

    def compare(self, op, other):
        _, lhs, rhs = self._pair(other)
        ls, ln, ld, lk = lhs
        rs, rn, rd, rk = rhs
        if lk == "nan" or rk == "nan":
            return op == "!="
        if lk == "inf" and rk == "inf":
            cmp = 0 if ls == rs else (-1 if ls else 1)
        elif lk == "inf":
            cmp = -1 if ls else 1
        elif rk == "inf":
            cmp = 1 if rs else -1
        else:
            left = (-ln if ls else ln) * rd
            right = (-rn if rs else rn) * ld
            cmp = -1 if left < right else 1 if left > right else 0
        if op == "==": return cmp == 0
        if op == "!=": return cmp != 0
        if op == "<": return cmp < 0
        if op == "<=": return cmp <= 0
        if op == ">": return cmp > 0
        if op == ">=": return cmp >= 0
        raise ValueError("invalid floating comparison: " + op)
