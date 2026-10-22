"""Small native-compilable subset of :mod:`ast`.

Only ``literal_eval`` of a string is provided. It parses the literal
grammar CPython's ``literal_eval`` accepts: numbers (underscores, 0x/0o/0b
prefixes, floats, imaginary numbers, ``real +/- imag`` complex, one sign on
a number), str and bytes literals (prefixes, escapes, implicit
concatenation), True/False/None, tuples, lists, sets (and ``set()``) and
dicts. Anything else raises ``ValueError`` or ``SyntaxError``; nothing is
evaluated approximately. ``\\N{...}`` escapes are rejected (no Unicode name
database).
"""
from __future__ import annotations


def _malformed_literal():
    raise ValueError("malformed node or string")


_SIMPLE_ESCAPES = {
    "\\": "\\", "'": "'", '"': '"', "a": "\x07", "b": "\x08",
    "f": "\x0c", "n": "\n", "r": "\r", "t": "\t", "v": "\x0b",
}
_HEX_DIGITS = "0123456789abcdefABCDEF"


class _Reader:
    def __init__(self, text):
        self.text = text
        self.pos = 0
        # Inside brackets a newline is whitespace, as for the tokenizer.
        self.depth = 0

    def skip_space(self):
        text = self.text
        n = len(text)
        while self.pos < n:
            ch = text[self.pos]
            if ch == " " or ch == "\t" or ch == "\x0c":
                self.pos += 1
            elif ch == "\\" and self.pos + 1 < n and text[self.pos + 1] == "\n":
                self.pos += 2
            elif (ch == "\n" or ch == "\r") and self.depth > 0:
                self.pos += 1
            elif ch == "#":
                while self.pos < n and text[self.pos] != "\n":
                    self.pos += 1
            else:
                return

    def peek(self):
        self.skip_space()
        if self.pos < len(self.text):
            return self.text[self.pos]
        return ""

    def take(self, expected):
        if self.peek() != expected:
            raise SyntaxError("invalid syntax")
        self.pos += 1


def _is_digit(ch):
    return "0" <= ch <= "9"


def _is_name_char(ch):
    return ch == "_" or ("a" <= ch <= "z") or ("A" <= ch <= "Z") or _is_digit(ch)


def _is_hex(ch):
    return ch in _HEX_DIGITS


def _is_octal(ch):
    return "0" <= ch <= "7"


def _is_binary(ch):
    return ch == "0" or ch == "1"


def _digit_part(text, pos, digit_ok):
    """End of ``digit (["_"] digit)*`` starting at ``pos`` (``pos`` if none)."""
    n = len(text)
    if pos >= n or not digit_ok(text[pos]):
        return pos
    pos += 1
    while pos < n:
        if digit_ok(text[pos]):
            pos += 1
        elif text[pos] == "_" and pos + 1 < n and digit_ok(text[pos + 1]):
            pos += 2
        else:
            return pos
    return pos


def _read_number(reader):
    text = reader.text
    n = len(text)
    start = reader.pos
    if start + 1 < n and text[start] == "0" and text[start + 1] in "xXoObB":
        kind = text[start + 1].lower()
        pos = start + 2
        if pos < n and text[pos] == "_":
            pos += 1
        if kind == "x":
            end = _digit_part(text, pos, _is_hex)
        elif kind == "o":
            end = _digit_part(text, pos, _is_octal)
        else:
            end = _digit_part(text, pos, _is_binary)
        if end == pos or (end < n and _is_name_char(text[end])):
            raise SyntaxError("invalid " + kind + " literal")
        reader.pos = end
        return int(text[start:end], 0)
    pos = _digit_part(text, start, _is_digit)
    integer_end = pos
    is_float = False
    if pos < n and text[pos] == ".":
        is_float = True
        pos = _digit_part(text, pos + 1, _is_digit)
        if integer_end == start and pos == integer_end + 1:
            raise SyntaxError("invalid syntax")
    elif pos == start:
        raise SyntaxError("invalid syntax")
    if pos < n and (text[pos] == "e" or text[pos] == "E"):
        exp = pos + 1
        if exp < n and (text[exp] == "+" or text[exp] == "-"):
            exp += 1
        end = _digit_part(text, exp, _is_digit)
        if end == exp:
            raise SyntaxError("invalid float literal")
        pos = end
        is_float = True
    imaginary = pos < n and (text[pos] == "j" or text[pos] == "J")
    token = text[start:pos]
    if imaginary:
        pos += 1
    if pos < n and _is_name_char(text[pos]):
        raise SyntaxError("invalid decimal literal")
    reader.pos = pos
    if imaginary:
        return complex(0.0, float(token))
    if is_float:
        return float(token)
    digits = token.replace("_", "")
    if len(digits) > 1 and digits[0] == "0" and digits.strip("0") != "":
        raise SyntaxError("leading zeros in decimal integer literals are not permitted")
    return int(digits)


def _is_real(value):
    return (isinstance(value, int) or isinstance(value, float)) and not isinstance(value, bool)


def _parse_signed_or_number(reader):
    """A number with at most one sign, then an optional ``+/- imag`` term."""
    sign = ""
    ch = reader.peek()
    if ch == "+" or ch == "-":
        sign = ch
        reader.pos += 1
        ch = reader.peek()
    if not (_is_digit(ch) or ch == "."):
        _malformed_literal()
    value = _read_number(reader)
    if sign == "-":
        value = -value
    nxt = reader.peek()
    if nxt == "+" or nxt == "-":
        if not _is_real(value):
            _malformed_literal()
        reader.pos += 1
        ch = reader.peek()
        if not (_is_digit(ch) or ch == "."):
            _malformed_literal()
        imag = _read_number(reader)
        if not isinstance(imag, complex):
            _malformed_literal()
        if nxt == "+":
            return value + imag
        return value - imag
    return value


def _string_prefix(reader):
    """Length of a str/bytes literal prefix at the cursor, or -1."""
    text = reader.text
    pos = reader.pos
    n = len(text)
    end = pos
    while end < n and end - pos < 2 and text[end] in "rRbBuU":
        end += 1
    if end < n and (text[end] == "'" or text[end] == '"'):
        prefix = text[pos:end].lower()
        if prefix in ("", "r", "u", "b", "br", "rb"):
            return end - pos
    return -1


def _hex_run(text, start, width):
    digits = text[start:start + width]
    if len(digits) != width:
        return ""
    index = 0
    while index < width:
        if not _is_hex(digits[index]):
            return ""
        index += 1
    return digits


def _unescape(body, is_bytes):
    out: list = []
    n = len(body)
    i = 0
    while i < n:
        ch = body[i]
        if ch != "\\" or i + 1 >= n:
            if is_bytes and ord(ch) > 127:
                raise SyntaxError("bytes can only contain ASCII literal characters")
            out.append(ch)
            i += 1
            continue
        nxt = body[i + 1]
        if nxt == "\n":
            i += 2
        elif nxt in _SIMPLE_ESCAPES:
            out.append(_SIMPLE_ESCAPES[nxt])
            i += 2
        elif _is_octal(nxt):
            end = i + 1
            while end < n and end < i + 4 and _is_octal(body[end]):
                end += 1
            code = int(body[i + 1:end], 8)
            if is_bytes and code > 255:
                raise ValueError("octal escape value outside the byte range")
            out.append(chr(code))
            i = end
        elif nxt == "x":
            digits = _hex_run(body, i + 2, 2)
            if digits == "":
                raise SyntaxError("truncated \\xXX escape")
            out.append(chr(int(digits, 16)))
            i += 4
        elif (nxt == "u" or nxt == "U") and not is_bytes:
            width = 4 if nxt == "u" else 8
            digits = _hex_run(body, i + 2, width)
            if digits == "":
                raise SyntaxError("truncated \\" + nxt + " escape")
            code = int(digits, 16)
            if code > 0x10FFFF:
                raise SyntaxError("illegal Unicode character")
            out.append(chr(code))
            i += 2 + width
        elif nxt == "N" and not is_bytes:
            raise ValueError("pcc ast does not support \\N{...} escapes")
        else:
            # CPython keeps an unrecognized escape as written.
            out.append("\\")
            out.append(nxt)
            i += 2
    return "".join(out)


def _read_string(reader, prefix_length):
    text = reader.text
    n = len(text)
    prefix = text[reader.pos:reader.pos + prefix_length].lower()
    pos = reader.pos + prefix_length
    quote = text[pos]
    triple = text[pos:pos + 3] == quote * 3
    delimiter = quote * 3 if triple else quote
    pos += len(delimiter)
    start = pos
    found = False
    while pos < n and not found:
        ch = text[pos]
        if ch == "\\":
            pos += 2
        elif text[pos:pos + len(delimiter)] == delimiter:
            found = True
        elif (ch == "\n" or ch == "\r") and not triple:
            raise SyntaxError("unterminated string literal")
        else:
            pos += 1
    if not found:
        raise SyntaxError("unterminated string literal")
    body = text[start:pos]
    reader.pos = pos + len(delimiter)
    is_bytes = "b" in prefix
    if "r" in prefix:
        if is_bytes:
            index = 0
            while index < len(body):
                if ord(body[index]) > 127:
                    raise SyntaxError("bytes can only contain ASCII literal characters")
                index += 1
        return is_bytes, body
    return is_bytes, _unescape(body, is_bytes)


def _parse_strings(reader):
    """One literal or several implicitly concatenated ones."""
    parts: list = []
    kind = -1
    prefix_length = _string_prefix(reader)
    while prefix_length >= 0:
        is_bytes, value = _read_string(reader, prefix_length)
        this_kind = 1 if is_bytes else 0
        if kind != -1 and kind != this_kind:
            raise SyntaxError("cannot mix bytes and nonbytes literals")
        kind = this_kind
        parts.append(value)
        reader.skip_space()
        prefix_length = _string_prefix(reader)
    joined = "".join(parts)
    if kind == 1:
        codes: list = []
        for ch in joined:
            codes.append(ord(ch))
        return bytes(codes)
    return joined


def _parse_sequence(reader, closing):
    """Comma-separated values up to ``closing``; returns (items, saw_comma)."""
    items: list = []
    saw_comma = False
    reader.depth += 1
    while reader.peek() != closing:
        items.append(_parse_value(reader))
        if reader.peek() == ",":
            reader.pos += 1
            saw_comma = True
        elif reader.peek() != closing:
            raise SyntaxError("invalid syntax")
    reader.depth -= 1
    reader.pos += 1
    return items, saw_comma


def _parse_braces(reader):
    reader.take("{")
    reader.depth += 1
    if reader.peek() == "}":
        reader.depth -= 1
        reader.pos += 1
        return {}
    first = _parse_value(reader)
    if reader.peek() == ":":
        reader.pos += 1
        result = {first: _parse_value(reader)}
        while reader.peek() == ",":
            reader.pos += 1
            if reader.peek() != "}":
                key = _parse_value(reader)
                reader.take(":")
                result[key] = _parse_value(reader)
        reader.depth -= 1
        reader.take("}")
        return result
    items: list = [first]
    while reader.peek() == ",":
        reader.pos += 1
        if reader.peek() != "}":
            items.append(_parse_value(reader))
    reader.depth -= 1
    reader.take("}")
    return set(items)


def _parse_value(reader):
    ch = reader.peek()
    if ch == "":
        raise SyntaxError("unexpected EOF while parsing")
    if ch == "(":
        reader.pos += 1
        items, saw_comma = _parse_sequence(reader, ")")
        if len(items) == 1 and not saw_comma:
            return items[0]
        return tuple(items)
    if ch == "[":
        reader.pos += 1
        items, _saw_comma = _parse_sequence(reader, "]")
        return items
    if ch == "{":
        return _parse_braces(reader)
    if ch == "+" or ch == "-" or _is_digit(ch) or ch == ".":
        return _parse_signed_or_number(reader)
    prefix_length = _string_prefix(reader)
    if prefix_length >= 0:
        return _parse_strings(reader)
    if _is_name_char(ch):
        start = reader.pos
        while reader.pos < len(reader.text) and _is_name_char(reader.text[reader.pos]):
            reader.pos += 1
        name = reader.text[start:reader.pos]
        if name == "True":
            return True
        if name == "False":
            return False
        if name == "None":
            return None
        if name == "set" and reader.peek() == "(":
            reader.pos += 1
            reader.depth += 1
            if reader.peek() != ")":
                _malformed_literal()
            reader.depth -= 1
            reader.pos += 1
            return set()
        _malformed_literal()
    raise SyntaxError("invalid syntax")


def literal_eval(node_or_string):
    """Evaluate a Python literal held in a string, as CPython does."""
    if not isinstance(node_or_string, str):
        _malformed_literal()
    reader = _Reader(node_or_string.lstrip(" \t").rstrip())
    if reader.peek() == "":
        raise SyntaxError("unexpected EOF while parsing")
    value = _parse_value(reader)
    if reader.peek() == ",":
        items: list = [value]
        while reader.peek() == ",":
            reader.pos += 1
            if reader.peek() != "":
                items.append(_parse_value(reader))
        value = tuple(items)
    if reader.peek() != "":
        raise SyntaxError("invalid syntax")
    return value
