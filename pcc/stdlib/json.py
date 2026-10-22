"""pcc.stdlib.json — minimal, self-contained JSON encoder/decoder.

No dependencies beyond the pcc-native list / dict / str / int / float
/ bool / None primitives. Covers the subset pcc.py actually needs.
"""
from __future__ import annotations


_INF = float("inf")


class JSONDecodeError(ValueError):
    def __init__(self, msg: str, doc: str, pos: int) -> None:
        super().__init__(f"{msg} at pos {pos}")
        self.msg = msg
        self.doc = doc
        self.pos = pos


def _indent_text(indent) -> str:
    """CPython accepts an int (that many spaces) or a literal string."""
    if indent is None:
        return ""
    if isinstance(indent, str):
        return indent
    if indent <= 0:
        return ""
    return " " * indent


def dumps(
    obj,
    indent=None,
    sort_keys=False,
    separators=None,
    ensure_ascii=True,
    allow_nan=True,
) -> str:
    """Serialize ``obj``, matching CPython's spacing.

    The provider previously ignored ``indent`` entirely and always emitted
    the compact form, so none of ``dumps(o)``, ``dumps(o, indent=2)`` or
    ``dumps(o, sort_keys=True)`` produced CPython's bytes: the default gained
    `{"a": 1}` versus `{"a":1}`, and an `indent=2` request came back on one
    line.  Anything that writes JSON for later byte comparison -- the runtime
    archive provenance manifests, build receipts, benchmark reports -- would
    therefore differ depending on whether host pcc or pcc1 wrote it.

    ``separators`` is honoured rather than accepted and dropped; every call in
    this tree passes ``(",", ":")`` for the compact form, and CPython's own
    defaults are ``(", ", ": ")`` without ``indent`` and ``(",", ": ")`` with
    it.
    """
    if separators is not None:
        item_sep, key_sep = separators
    elif indent is None:
        item_sep = ", "
        key_sep = ": "
    else:
        item_sep = ","
        key_sep = ": "
    pad = _indent_text(indent)
    newline = indent is not None
    buf: list[str] = []
    _encode(
        obj, buf, pad, 0, sort_keys, item_sep, key_sep, newline,
        ensure_ascii, allow_nan,
    )
    return "".join(buf)


def dump(
    obj,
    fp,
    indent=None,
    sort_keys=False,
    separators=None,
    ensure_ascii=True,
    allow_nan=True,
) -> None:
    """``json.dump`` over any writable file object.

    ``pcc/`` calls this in ten places and ``load`` in thirteen; the provider
    defined neither, so binding these names to it at codegen time needed both
    before a compile could resolve them.
    """
    fp.write(
        dumps(
            obj,
            indent=indent,
            sort_keys=sort_keys,
            separators=separators,
            ensure_ascii=ensure_ascii,
            allow_nan=allow_nan,
        )
    )


def load(fp):
    """``json.load`` over any readable file object."""
    return loads(fp.read())


def loads(s: str, object_pairs_hook=None):
    dec = _Decoder(s)
    dec.object_pairs_hook = object_pairs_hook
    val = dec.parse_value()
    dec.skip_ws()
    if dec.pos != len(s):
        raise JSONDecodeError("extra data", s, dec.pos)
    return val


# ---------- encoder ----------

def _float_text(value, allow_nan) -> str:
    """CPython's json spells the three non-finite floats differently to repr."""
    if value != value:
        text = "NaN"
    elif value == _INF:
        text = "Infinity"
    elif value == -_INF:
        text = "-Infinity"
    else:
        return repr(value)
    if not allow_nan:
        raise ValueError("Out of range float values are not JSON compliant")
    return text


def _encode(
    obj, buf, pad, depth, sort_keys, item_sep, key_sep, newline,
    ensure_ascii, allow_nan,
) -> None:
    if obj is None:
        buf.append("null")
    elif obj is True:
        buf.append("true")
    elif obj is False:
        buf.append("false")
    elif isinstance(obj, int):
        buf.append(str(obj))
    elif isinstance(obj, float):
        buf.append(_float_text(obj, allow_nan))
    elif isinstance(obj, str):
        _encode_str(obj, buf, ensure_ascii)
    elif isinstance(obj, (list, tuple)):
        _encode_array(
            obj, buf, pad, depth, sort_keys, item_sep, key_sep, newline,
            ensure_ascii, allow_nan,
        )
    elif isinstance(obj, dict):
        _encode_object(
            obj, buf, pad, depth, sort_keys, item_sep, key_sep, newline,
            ensure_ascii, allow_nan,
        )
    else:
        raise TypeError(f"json.dumps: unsupported type {type(obj).__name__}")


def _encode_str(s, buf, ensure_ascii=True) -> None:
    # Characters 0x20..0x7E other than '"' and '\\' are emitted unchanged in
    # both ensure_ascii modes, so such a string is appended whole instead of
    # one character at a time.  isprintable() alone is not enough: CPython
    # also accepts printable non-ASCII characters.
    if s.isascii() and s.isprintable() and '"' not in s and "\\" not in s:
        buf.append('"')
        buf.append(s)
        buf.append('"')
        return
    buf.append('"')
    for c in s:
        if c == '"':
            buf.append('\\"')
        elif c == "\\":
            buf.append("\\\\")
        elif c == "\n":
            buf.append("\\n")
        elif c == "\r":
            buf.append("\\r")
        elif c == "\t":
            buf.append("\\t")
        elif ord(c) < 0x20:
            buf.append("\\u{:04x}".format(ord(c)))
        elif ensure_ascii and ord(c) > 0x7E:
            # CPython escapes every non-ASCII code point by default, and
            # spells anything above the BMP as a UTF-16 surrogate pair.  The
            # provider emitted them raw, i.e. it behaved as
            # ``ensure_ascii=False`` no matter what the caller asked for.
            point = ord(c)
            if point > 0xFFFF:
                point = point - 0x10000
                buf.append("\\u{:04x}".format(0xD800 + (point >> 10)))
                buf.append("\\u{:04x}".format(0xDC00 + (point & 0x3FF)))
            else:
                buf.append("\\u{:04x}".format(point))
        else:
            buf.append(c)
    buf.append('"')


def _encode_array(
    arr, buf, pad, depth, sort_keys, item_sep, key_sep, newline,
    ensure_ascii, allow_nan,
) -> None:
    if not arr:
        buf.append("[]")
        return
    buf.append("[")
    inner = "\n" + pad * (depth + 1)
    closing = "\n" + pad * depth
    for i, item in enumerate(arr):
        if i > 0:
            buf.append(item_sep)
        if newline:
            buf.append(inner)
        _encode(
            item, buf, pad, depth + 1, sort_keys, item_sep, key_sep, newline,
            ensure_ascii, allow_nan,
        )
    if newline:
        buf.append(closing)
    buf.append("]")


def _encode_object(
    obj, buf, pad, depth, sort_keys, item_sep, key_sep, newline,
    ensure_ascii, allow_nan,
) -> None:
    if not obj:
        buf.append("{}")
        return
    buf.append("{")
    inner = "\n" + pad * (depth + 1)
    closing = "\n" + pad * depth
    keys = sorted(obj.keys()) if sort_keys else list(obj.keys())
    for i, k in enumerate(keys):
        if i > 0:
            buf.append(item_sep)
        if newline:
            buf.append(inner)
        _encode_str(str(k), buf, ensure_ascii)
        buf.append(key_sep)
        _encode(
            obj[k], buf, pad, depth + 1, sort_keys, item_sep, key_sep, newline,
            ensure_ascii, allow_nan,
        )
    if newline:
        buf.append(closing)
    buf.append("}")


# ---------- decoder ----------

class _Decoder:
    def __init__(self, s: str) -> None:
        self.s = s
        self.pos = 0
        self.object_pairs_hook = None

    def skip_ws(self) -> None:
        while self.pos < len(self.s) and self.s[self.pos] in " \t\n\r":
            self.pos += 1

    def parse_value(self):
        self.skip_ws()
        if self.pos >= len(self.s):
            raise JSONDecodeError("unexpected EOF", self.s, self.pos)
        ch = self.s[self.pos]
        if ch == '"':
            return self._parse_string()
        if ch == "{":
            return self._parse_object()
        if ch == "[":
            return self._parse_array()
        if ch == "t" and self.s.startswith("true", self.pos):
            self.pos += 4
            return True
        if ch == "f" and self.s.startswith("false", self.pos):
            self.pos += 5
            return False
        if ch == "n" and self.s.startswith("null", self.pos):
            self.pos += 4
            return None
        # The encoder spells non-finite floats like CPython (allow_nan), and
        # CPython's decoder reads them back by default.
        if ch == "N" and self.s.startswith("NaN", self.pos):
            self.pos += 3
            return float("nan")
        if ch == "I" and self.s.startswith("Infinity", self.pos):
            self.pos += 8
            return _INF
        if ch == "-" and self.s.startswith("-Infinity", self.pos):
            self.pos += 9
            return -_INF
        return self._parse_number()

    def _parse_string(self) -> str:
        if self.pos >= len(self.s) or self.s[self.pos] != '"':
            raise JSONDecodeError("expected string", self.s, self.pos)
        self.pos += 1
        # Without a backslash before the closing quote the value is one slice.
        end = self.s.find('"', self.pos)
        if end >= 0 and self.s.find("\\", self.pos, end) < 0:
            value = self.s[self.pos:end]
            self.pos = end + 1
            return value
        out: list[str] = []
        while self.pos < len(self.s):
            ch = self.s[self.pos]
            if ch == '"':
                self.pos += 1
                return "".join(out)
            if ch == "\\":
                self.pos += 1
                if self.pos >= len(self.s):
                    raise JSONDecodeError(
                        "bad escape", self.s, self.pos,
                    )
                esc = self.s[self.pos]
                self.pos += 1
                if esc == '"':
                    out.append('"')
                elif esc == "\\":
                    out.append("\\")
                elif esc == "/":
                    out.append("/")
                elif esc == "n":
                    out.append("\n")
                elif esc == "r":
                    out.append("\r")
                elif esc == "t":
                    out.append("\t")
                elif esc == "u":
                    hex4 = self.s[self.pos:self.pos + 4]
                    self.pos += 4
                    point = int(hex4, 16)
                    # Like CPython, an escaped UTF-16 surrogate pair spells one
                    # code point above the BMP; a lone surrogate stays itself.
                    if (
                        0xD800 <= point <= 0xDBFF
                        and self.s[self.pos:self.pos + 2] == "\\u"
                    ):
                        low = int(self.s[self.pos + 2:self.pos + 6], 16)
                        if 0xDC00 <= low <= 0xDFFF:
                            point = 0x10000 + ((point - 0xD800) << 10) + (low - 0xDC00)
                            self.pos += 6
                    out.append(chr(point))
                else:
                    raise JSONDecodeError(
                        f"bad escape \\{esc}", self.s, self.pos,
                    )
            else:
                out.append(ch)
                self.pos += 1
        raise JSONDecodeError("unterminated string", self.s, self.pos)

    def _parse_number(self):
        start = self.pos
        if self.s[self.pos] == "-":
            self.pos += 1
        while self.pos < len(self.s) and self.s[self.pos].isdigit():
            self.pos += 1
        is_float = False
        if self.pos < len(self.s) and self.s[self.pos] == ".":
            is_float = True
            self.pos += 1
            while self.pos < len(self.s) and self.s[self.pos].isdigit():
                self.pos += 1
        if self.pos < len(self.s) and self.s[self.pos] in "eE":
            is_float = True
            self.pos += 1
            if self.pos < len(self.s) and self.s[self.pos] in "+-":
                self.pos += 1
            while self.pos < len(self.s) and self.s[self.pos].isdigit():
                self.pos += 1
        tok = self.s[start:self.pos]
        if not tok or tok == "-":
            raise JSONDecodeError("bad number", self.s, start)
        if is_float:
            return float(tok)
        return int(tok)

    def _parse_array(self) -> list:
        assert self.s[self.pos] == "["
        self.pos += 1
        out: list = []
        self.skip_ws()
        if self.pos < len(self.s) and self.s[self.pos] == "]":
            self.pos += 1
            return out
        while True:
            out.append(self.parse_value())
            self.skip_ws()
            if self.pos < len(self.s) and self.s[self.pos] == ",":
                self.pos += 1
                continue
            if self.pos < len(self.s) and self.s[self.pos] == "]":
                self.pos += 1
                return out
            raise JSONDecodeError("bad array", self.s, self.pos)

    def _finish_object(self, pairs):
        """``object_pairs_hook`` sees document order, including duplicates."""
        hook = self.object_pairs_hook
        if hook is not None:
            return hook(pairs)
        out: dict = {}
        for key, value in pairs:
            out[key] = value
        return out

    def _parse_object(self):
        assert self.s[self.pos] == "{"
        self.pos += 1
        pairs: list = []
        self.skip_ws()
        if self.pos < len(self.s) and self.s[self.pos] == "}":
            self.pos += 1
            return self._finish_object(pairs)
        while True:
            self.skip_ws()
            key = self._parse_string()
            self.skip_ws()
            if self.pos >= len(self.s) or self.s[self.pos] != ":":
                raise JSONDecodeError("missing :", self.s, self.pos)
            self.pos += 1
            pairs.append((key, self.parse_value()))
            self.skip_ws()
            if self.pos < len(self.s) and self.s[self.pos] == ",":
                self.pos += 1
                continue
            if self.pos < len(self.s) and self.s[self.pos] == "}":
                self.pos += 1
                return self._finish_object(pairs)
            raise JSONDecodeError("bad object", self.s, self.pos)
