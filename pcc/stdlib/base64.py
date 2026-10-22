"""pcc.stdlib.base64 — pure Python base64 subset."""

_ALPHABET = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_URLSAFE = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def _as_bytes(data) -> bytes:
    if isinstance(data, bytes):
        return data
    if isinstance(data, bytearray):
        return bytes(data)
    if isinstance(data, str):
        # Use latin-1 because phase4 corpus inputs are ASCII.
        return data.encode("latin-1")
    return bytes(data)


def _decode_char(ch: int) -> int:
    if 65 <= ch <= 90:
        return ch - 65
    if 97 <= ch <= 122:
        return ch - 71
    if 48 <= ch <= 57:
        return ch + 4
    if ch == 43:
        return 62
    if ch == 47:
        return 63
    return -1


def _encode_index(v: int, plus: int, slash: int) -> int:
    if v == 62:
        return plus
    if v == 63:
        return slash
    return _ALPHABET[v]


def b64encode(data, altchars=None) -> bytes:
    raw = _as_bytes(data)
    plus = 43
    slash = 47
    if altchars is not None:
        alt = _as_bytes(altchars)
        if len(alt) != 2:
            raise ValueError("altchars should be length 2")
        plus = alt[0]
        slash = alt[1]
    out: list[int] = []
    i = 0
    n = len(raw)
    while i < n:
        b0 = raw[i]
        b1 = raw[i + 1] if i + 1 < n else 0
        b2 = raw[i + 2] if i + 2 < n else 0
        triple = (b0 << 16) | (b1 << 8) | b2
        out.append(_encode_index((triple >> 18) & 63, plus, slash))
        out.append(_encode_index((triple >> 12) & 63, plus, slash))
        if i + 1 < n:
            out.append(_encode_index((triple >> 6) & 63, plus, slash))
        else:
            out.append(61)
        if i + 2 < n:
            out.append(_encode_index(triple & 63, plus, slash))
        else:
            out.append(61)
        i += 3
    return bytes(out)


def b64decode(data, altchars=None, validate=False) -> bytes:
    raw = _as_bytes(data)
    plus = 43
    slash = 47
    if altchars is not None:
        alt = _as_bytes(altchars)
        if len(alt) != 2:
            raise ValueError("altchars should be length 2")
        plus = alt[0]
        slash = alt[1]
    clean: list[int] = []
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == 9 or ch == 10 or ch == 13 or ch == 32:
            if validate:
                raise ValueError("invalid whitespace in base64")
        else:
            clean.append(ch)
        i += 1
    text = clean
    while len(text) % 4:
        text.append(61)
    out: list[int] = []
    i = 0
    while i < len(text):
        c0 = text[i]
        c1 = text[i + 1]
        c2 = text[i + 2]
        c3 = text[i + 3]
        pad = (c2 == 61) + (c3 == 61)
        if c2 == 61 and c3 != 61:
            raise ValueError("invalid base64 padding")

        idx0 = _decode_char(c0)
        idx1 = _decode_char(c1)
        if idx0 < 0 or idx1 < 0:
            if c0 in (plus, slash):
                idx0 = 62 if c0 == plus else 63
            if c1 in (plus, slash):
                idx1 = 62 if c1 == plus else 63
            if idx0 < 0 or idx1 < 0:
                raise ValueError("invalid base64 character")
        if c2 == 61:
            idx2 = 0
        else:
            idx2 = _decode_char(c2)
            if idx2 < 0:
                if c2 == plus:
                    idx2 = 62
                elif c2 == slash:
                    idx2 = 63
                else:
                    raise ValueError("invalid base64 character")
        if c3 == 61:
            idx3 = 0
        else:
            idx3 = _decode_char(c3)
            if idx3 < 0:
                if c3 == plus:
                    idx3 = 62
                elif c3 == slash:
                    idx3 = 63
                else:
                    raise ValueError("invalid base64 character")

        triple = (idx0 << 18) | (idx1 << 12) | (idx2 << 6) | idx3
        out.append((triple >> 16) & 255)
        if pad < 2:
            out.append((triple >> 8) & 255)
        if pad < 1:
            out.append(triple & 255)
        i += 4
    return bytes(out)


def urlsafe_b64encode(data):
    return b64encode(data, b"-_")


def urlsafe_b64decode(data):
    return b64decode(data, b"-_")


def standard_b64encode(data):
    return b64encode(data)


def standard_b64decode(data):
    return b64decode(data)
