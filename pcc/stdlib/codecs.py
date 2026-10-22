"""pcc-owned subset of :mod:`codecs` used by Python build tools.

Meson's parser needs the UTF BOM constants plus ``unicode_escape``
encode/decode.  Keeping that surface here prevents the recursive stdlib walker
from selecting CPython's large registry-based ``codecs.py``.  Unknown codecs
fail explicitly; this module does not pretend to provide CPython's full codec
registry.
"""

from __future__ import annotations


BOM_UTF8 = b"\xef\xbb\xbf"
BOM_UTF16_LE = b"\xff\xfe"
BOM_UTF16_BE = b"\xfe\xff"
BOM_UTF32_LE = b"\xff\xfe\x00\x00"
BOM_UTF32_BE = b"\x00\x00\xfe\xff"

# pcc's current native targets are little-endian.  These aliases match
# CPython on every target pcc currently supports.
BOM_LE = BOM_UTF16_LE
BOM_BE = BOM_UTF16_BE
BOM_UTF16 = BOM_UTF16_LE
BOM_UTF32 = BOM_UTF32_LE
BOM = BOM_UTF16


def _encoding_key(encoding: str) -> str:
    return encoding.lower().replace("_", "-").replace(" ", "")


def _bytes_text(data: bytes) -> str:
    """One-codepoint-per-byte text without relying on a latin-1 decoder."""
    out = ""
    for value in data:
        out = out + chr(value)
    return out


def _hex_value(text: str) -> int:
    value = 0
    for ch in text:
        value = value * 16
        if ch >= "0" and ch <= "9":
            value = value + ord(ch) - ord("0")
        elif ch >= "a" and ch <= "f":
            value = value + ord(ch) - ord("a") + 10
        elif ch >= "A" and ch <= "F":
            value = value + ord(ch) - ord("A") + 10
        else:
            raise ValueError("invalid hexadecimal escape")
    return value


def _octal_value(text: str) -> int:
    value = 0
    for ch in text:
        if ch < "0" or ch > "7":
            raise ValueError("invalid octal escape")
        value = value * 8 + ord(ch) - ord("0")
    return value


def _named_character(name: str) -> str:
    # The build-tool grammar accepts named escapes, but its own sources only
    # require a very small stable set.  Common algorithmic ASCII names keep
    # the useful surface generic; uncommon Unicode names remain an explicit
    # unsupported boundary rather than silently producing the wrong text.
    if name == "SPACE":
        return " "
    if name == "TAB":
        return "\t"
    if name == "LINE FEED" or name == "NEW LINE":
        return "\n"
    if name == "SNOWMAN":
        return chr(0x2603)
    if name == "EURO SIGN":
        return chr(0x20AC)
    if name.startswith("LATIN CAPITAL LETTER "):
        tail = name[len("LATIN CAPITAL LETTER ") :]
        if len(tail) == 1 and tail >= "A" and tail <= "Z":
            return tail
    if name.startswith("LATIN SMALL LETTER "):
        tail = name[len("LATIN SMALL LETTER ") :]
        if len(tail) == 1 and tail >= "A" and tail <= "Z":
            return tail.lower()
    raise NotImplementedError(
        "unicode character name is outside pcc codecs subset"
    )


def _decode_unicode_escape(data: bytes, errors: str) -> str:
    text = _bytes_text(data)
    out = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if ch != "\\":
            out = out + ch
            i += 1
            continue
        if i + 1 >= len(text):
            if errors == "ignore":
                return out
            if errors == "replace":
                return out + chr(0xFFFD)
            raise ValueError("trailing backslash in unicode_escape input")
        esc = text[i + 1]
        i += 2
        if esc == "\\" or esc == "'" or esc == '"':
            out = out + esc
        elif esc == "a":
            out = out + chr(7)
        elif esc == "b":
            out = out + chr(8)
        elif esc == "f":
            out = out + chr(12)
        elif esc == "n":
            out = out + "\n"
        elif esc == "r":
            out = out + "\r"
        elif esc == "t":
            out = out + "\t"
        elif esc == "v":
            out = out + chr(11)
        elif esc == "x":
            if i + 2 > len(text):
                raise ValueError("truncated hexadecimal escape")
            out = out + chr(_hex_value(text[i : i + 2]))
            i += 2
        elif esc == "u":
            if i + 4 > len(text):
                raise ValueError("truncated Unicode escape")
            out = out + chr(_hex_value(text[i : i + 4]))
            i += 4
        elif esc == "U":
            if i + 8 > len(text):
                raise ValueError("truncated long Unicode escape")
            out = out + chr(_hex_value(text[i : i + 8]))
            i += 8
        elif esc == "N" and i < len(text) and text[i] == "{":
            end = text.find("}", i + 1)
            if end < 0:
                raise ValueError("malformed named Unicode escape")
            out = out + _named_character(text[i + 1 : end])
            i = end + 1
        elif esc >= "0" and esc <= "7":
            digits = esc
            count = 1
            while (
                count < 3
                and i < len(text)
                and text[i] >= "0"
                and text[i] <= "7"
            ):
                digits = digits + text[i]
                i += 1
                count += 1
            out = out + chr(_octal_value(digits))
        else:
            # CPython preserves unknown escapes and warns.  Warnings are not
            # part of Meson's parser contract, but preserving the text is.
            out = out + "\\" + esc
    return out


_HEX_DIGITS = "0123456789abcdef"


def _hex_escape(value: int, digits: int) -> str:
    out = ""
    remaining = digits
    while remaining > 0:
        out = _HEX_DIGITS[value & 15] + out
        value = value >> 4
        remaining -= 1
    return out


def _encode_unicode_escape(text: str) -> bytes:
    out = ""
    for ch in text:
        value = ord(ch)
        if ch == "\\":
            out = out + "\\\\"
        elif ch == "\t":
            out = out + "\\t"
        elif ch == "\n":
            out = out + "\\n"
        elif ch == "\r":
            out = out + "\\r"
        elif value == 7:
            out = out + "\\a"
        elif value == 8:
            out = out + "\\b"
        elif value == 11:
            out = out + "\\v"
        elif value == 12:
            out = out + "\\f"
        elif value >= 32 and value < 127:
            out = out + ch
        elif value <= 0xFF:
            out = out + "\\x" + _hex_escape(value, 2)
        elif value <= 0xFFFF:
            out = out + "\\u" + _hex_escape(value, 4)
        else:
            out = out + "\\U" + _hex_escape(value, 8)
    return out.encode("utf-8")


def decode(data: bytes, encoding: str = "utf-8", errors: str = "strict") -> str:
    key = _encoding_key(encoding)
    if key == "unicode-escape":
        return _decode_unicode_escape(data, errors)
    if key == "utf-8" or key == "utf8":
        if errors == "ignore":
            return data.decode("utf-8", "ignore")
        if errors == "strict":
            return data.decode("utf-8")
        raise NotImplementedError("pcc codecs utf-8 decode error mode unsupported")
    if key == "ascii":
        text = _bytes_text(data)
        for ch in text:
            if ord(ch) > 127:
                raise ValueError("ordinal not in range(128)")
        return text
    if key == "latin-1" or key == "latin1" or key == "iso-8859-1":
        return _bytes_text(data)
    raise LookupError("unknown encoding: " + encoding)


def encode(text: str, encoding: str = "utf-8", errors: str = "strict") -> bytes:
    key = _encoding_key(encoding)
    if key == "unicode-escape":
        return _encode_unicode_escape(text)
    if errors != "strict":
        raise NotImplementedError("pcc codecs encode error mode unsupported")
    if key == "utf-8" or key == "utf8":
        return text.encode("utf-8")
    if key == "latin-1" or key == "latin1" or key == "iso-8859-1":
        return text.encode("latin-1")
    if key == "ascii":
        for ch in text:
            if ord(ch) > 127:
                raise ValueError("ordinal not in range(128)")
        return text.encode("utf-8")
    raise LookupError("unknown encoding: " + encoding)
