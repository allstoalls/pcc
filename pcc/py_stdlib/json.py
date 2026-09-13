"""pcc.py_stdlib.json — minimal, self-contained JSON encoder/decoder.

No dependencies beyond the pcc-native list / dict / str / int / float
/ bool / None primitives. Covers the subset pcc.py actually needs.
"""
from __future__ import annotations


class JSONDecodeError(ValueError):
    def __init__(self, msg: str, doc: str, pos: int) -> None:
        super().__init__(f"{msg} at pos {pos}")
        self.msg = msg
        self.doc = doc
        self.pos = pos


def dumps(obj, indent=None, sort_keys=False) -> str:
    buf: list[str] = []
    _encode(obj, buf, indent, 0, sort_keys)
    return "".join(buf)


def loads(s: str):
    dec = _Decoder(s)
    val = dec.parse_value()
    dec.skip_ws()
    if dec.pos != len(s):
        raise JSONDecodeError("extra data", s, dec.pos)
    return val


# ---------- encoder ----------

def _encode(obj, buf, indent, depth, sort_keys) -> None:
    if obj is None:
        buf.append("null")
    elif obj is True:
        buf.append("true")
    elif obj is False:
        buf.append("false")
    elif isinstance(obj, int):
        buf.append(str(obj))
    elif isinstance(obj, float):
        buf.append(repr(obj))
    elif isinstance(obj, str):
        _encode_str(obj, buf)
    elif isinstance(obj, (list, tuple)):
        _encode_array(obj, buf, indent, depth, sort_keys)
    elif isinstance(obj, dict):
        _encode_object(obj, buf, indent, depth, sort_keys)
    else:
        raise TypeError(f"json.dumps: unsupported type {type(obj).__name__}")


def _encode_str(s, buf) -> None:
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
        else:
            buf.append(c)
    buf.append('"')


def _encode_array(arr, buf, indent, depth, sort_keys) -> None:
    if not arr:
        buf.append("[]")
        return
    buf.append("[")
    for i, item in enumerate(arr):
        if i > 0:
            buf.append(",")
        _encode(item, buf, indent, depth + 1, sort_keys)
    buf.append("]")


def _encode_object(obj, buf, indent, depth, sort_keys) -> None:
    if not obj:
        buf.append("{}")
        return
    buf.append("{")
    keys = sorted(obj.keys()) if sort_keys else list(obj.keys())
    for i, k in enumerate(keys):
        if i > 0:
            buf.append(",")
        _encode_str(str(k), buf)
        buf.append(":")
        _encode(obj[k], buf, indent, depth + 1, sort_keys)
    buf.append("}")


# ---------- decoder ----------

class _Decoder:
    def __init__(self, s: str) -> None:
        self.s = s
        self.pos = 0

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
        return self._parse_number()

    def _parse_string(self) -> str:
        if self.pos >= len(self.s) or self.s[self.pos] != '"':
            raise JSONDecodeError("expected string", self.s, self.pos)
        self.pos += 1
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
                    out.append(chr(int(hex4, 16)))
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

    def _parse_object(self) -> dict:
        assert self.s[self.pos] == "{"
        self.pos += 1
        out: dict = {}
        self.skip_ws()
        if self.pos < len(self.s) and self.s[self.pos] == "}":
            self.pos += 1
            return out
        while True:
            self.skip_ws()
            key = self._parse_string()
            self.skip_ws()
            if self.pos >= len(self.s) or self.s[self.pos] != ":":
                raise JSONDecodeError("missing :", self.s, self.pos)
            self.pos += 1
            out[key] = self.parse_value()
            self.skip_ws()
            if self.pos < len(self.s) and self.s[self.pos] == ",":
                self.pos += 1
                continue
            if self.pos < len(self.s) and self.s[self.pos] == "}":
                self.pos += 1
                return out
            raise JSONDecodeError("bad object", self.s, self.pos)
