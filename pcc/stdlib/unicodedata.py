"""Bounded Unicode width data used by native build tools.

Meson's terminal formatter uses only :func:`east_asian_width`.  Shipping a
partial normalization/name/category database would be a false compatibility
claim, so this provider owns only the width classes needed to compute terminal
columns.  The wide/full-width/half-width ranges below cover modern CJK,
Hangul, full-width forms and emoji; unlisted code points use the neutral class.
"""
from __future__ import annotations


_WIDE_RANGES = (
    (0x1100, 0x115F),
    (0x231A, 0x231B),
    (0x2329, 0x232A),
    (0x23E9, 0x23EC),
    (0x23F0, 0x23F0),
    (0x23F3, 0x23F3),
    (0x25FD, 0x25FE),
    (0x2614, 0x2615),
    (0x2648, 0x2653),
    (0x267F, 0x267F),
    (0x2693, 0x2693),
    (0x26A1, 0x26A1),
    (0x26AA, 0x26AB),
    (0x26BD, 0x26BE),
    (0x26C4, 0x26C5),
    (0x26CE, 0x26CE),
    (0x26D4, 0x26D4),
    (0x26EA, 0x26EA),
    (0x26F2, 0x26F3),
    (0x26F5, 0x26F5),
    (0x26FA, 0x26FA),
    (0x26FD, 0x26FD),
    (0x2705, 0x2705),
    (0x270A, 0x270B),
    (0x2728, 0x2728),
    (0x274C, 0x274C),
    (0x274E, 0x274E),
    (0x2753, 0x2755),
    (0x2757, 0x2757),
    (0x2795, 0x2797),
    (0x27B0, 0x27B0),
    (0x27BF, 0x27BF),
    (0x2B1B, 0x2B1C),
    (0x2B50, 0x2B50),
    (0x2B55, 0x2B55),
    (0x2E80, 0x303E),
    (0x3041, 0xA4CF),
    (0xAC00, 0xD7A3),
    (0xF900, 0xFAFF),
    (0xFE10, 0xFE19),
    (0xFE30, 0xFE6F),
    (0x1F004, 0x1F004),
    (0x1F0CF, 0x1F0CF),
    (0x1F18E, 0x1F18E),
    (0x1F191, 0x1F19A),
    (0x1F200, 0x1F202),
    (0x1F210, 0x1F23B),
    (0x1F240, 0x1F248),
    (0x1F250, 0x1F251),
    (0x1F300, 0x1FAFF),
    (0x20000, 0x3FFFD),
)


def east_asian_width(character):
    if not isinstance(character, str) or len(character) != 1:
        raise TypeError("east_asian_width() argument must be a unicode character")
    codepoint = ord(character)
    if 0xFF01 <= codepoint <= 0xFF60 or 0xFFE0 <= codepoint <= 0xFFE6:
        return "F"
    if 0xFF61 <= codepoint <= 0xFFDC or 0xFFE8 <= codepoint <= 0xFFEE:
        return "H"
    for lower, upper in _WIDE_RANGES:
        if lower <= codepoint <= upper:
            return "W"
    if 0x20 <= codepoint <= 0x7E:
        return "Na"
    # Ambiguous, narrow and neutral characters are all one column in Meson's
    # mapping.  This bounded provider does not claim their complete UCD label.
    return "N"


__all__ = ["east_asian_width"]
