"""Owned C11 character constant representation, shared by all C consumers.

The source/execution encoding is UTF-8. Ordinary literals have type int:
UTF-8 bytes and numeric byte escapes are packed most-significant byte first,
retaining the low 32 bits for multicharacter constants. A single byte is
sign-extended according to the target plain-char ABI before promotion to int.
L/u/U literals contain exactly one representable code unit and have the
underlying wchar_t/char16_t/char32_t integer type, respectively. Numeric escapes
must fit the code-unit width; unrepresentable Unicode and malformed source
encoding are diagnostics, never silently masked or replaced.
"""

from pcc.frontends.c.c_abi_layout import (
    builtin_integer_is_unsigned,
    builtin_scalar_layout,
    c_target_triple,
)


def parse_c_character_constant(src, start=0, target_triple=None):
    """Return (value, C integer type name, end offset) for one literal."""
    target = c_target_triple(target_triple)
    pos = start
    prefix = ""
    if src[pos:pos + 2] in ("L'", "u'", "U'"):
        prefix = src[pos]
        pos += 1
    if src[pos:pos + 1] != "'":
        raise ValueError(f"invalid character constant at pos {start}")
    pos += 1
    type_name = "int"
    width = 8
    if prefix == "u":
        type_name, width = "unsigned short", 16
    elif prefix == "U":
        type_name, width = "unsigned int", 32
    elif prefix == "L":
        width = builtin_scalar_layout(["wchar_t"], target).size * 8
        unsigned = builtin_integer_is_unsigned(["wchar_t"], target)
        base_type = "short" if width == 16 else "int"
        type_name = ("unsigned " if unsigned else "") + base_type
    values = []
    escapes = {
        "a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12,
        "r": 13, "'": 39, '"': 34, "?": 63, "\\": 92,
    }
    while pos < len(src) and src[pos] != "'":
        char = src[pos]
        pos += 1
        unicode_value = True
        if char == "\\":
            if pos >= len(src):
                raise ValueError(f"unterminated character constant at pos {start}")
            char = src[pos]
            pos += 1
            unicode_value = False
            if char in escapes:
                value = escapes[char]
            elif char in "01234567":
                digits = char
                while len(digits) < 3 and pos < len(src) and src[pos] in "01234567":
                    digits += src[pos]
                    pos += 1
                value = int(digits, 8)
            elif char in "xuU":
                digits_start = pos
                count = 4 if char == "u" else 8 if char == "U" else len(src)
                while (pos < len(src) and pos - digits_start < count
                       and src[pos] in "0123456789abcdefABCDEF"):
                    pos += 1
                digits = src[digits_start:pos]
                if not digits or (char != "x" and len(digits) != count):
                    raise ValueError(f"invalid character escape at pos {digits_start}")
                value = int(digits, 16)
                unicode_value = char != "x"
                if unicode_value and (value > 0x10ffff or 0xd800 <= value <= 0xdfff
                        or (value < 0xa0 and value not in (0x24, 0x40, 0x60))):
                    raise ValueError(f"invalid universal character at pos {digits_start}")
            else:
                raise ValueError(f"invalid character escape at pos {pos - 1}")
        else:
            if char in "\r\n":
                raise ValueError(f"unterminated character constant at pos {start}")
            value = ord(char)
            if 0xd800 <= value <= 0xdfff:
                raise ValueError(f"illegal character encoding in character literal at pos {pos - 1}")
        if unicode_value and not prefix:
            values.extend(chr(value).encode("utf-8"))
        else:
            if value >= (1 << width):
                raise ValueError(f"character value is not representable in {width} bits at pos {start}")
            values.append(value)
    if not values or pos >= len(src):
        raise ValueError(f"invalid character constant at pos {start}")
    pos += 1
    if prefix:
        if len(values) != 1:
            raise ValueError(f"multiple characters in prefixed character constant at pos {start}")
        value = values[0]
        if not type_name.startswith("unsigned") and value >= (1 << (width - 1)):
            value -= 1 << width
    else:
        value = 0
        for byte in values:
            value = ((value << 8) | byte) & 0xffffffff
        if len(values) == 1:
            if value >= 128 and not builtin_integer_is_unsigned(["char"], target):
                value -= 256
        elif value >= 0x80000000:
            value -= 0x100000000
    return value, type_name, pos


def decode_c_character_constant(raw, target_triple=None):
    """Return (value, C integer type name), diagnosing trailing input."""
    value, type_name, end = parse_c_character_constant(raw, 0, target_triple)
    if end != len(raw):
        raise ValueError(f"trailing input after character constant at pos {end}")
    return value, type_name
