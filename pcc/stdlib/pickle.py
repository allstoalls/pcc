"""pcc.stdlib.pickle — deterministic bootstrap pickle subset."""
from __future__ import annotations


HIGHEST_PROTOCOL = 5
DEFAULT_PROTOCOL = 5


class PickleError(Exception):
    pass


class PicklingError(PickleError):
    pass


class UnpicklingError(PickleError):
    pass


def _host_pickle():
    try:
        mod = __import__("pickle")
    except Exception:
        return None
    if getattr(mod, "__name__", "") == __name__:
        return None
    return mod


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("|", "\\p").replace(";", "\\s").replace(":", "\\c")


def _unescape(s: str) -> str:
    out = []
    i = 0
    while i < len(s):
        if s[i] != "\\":
            out.append(s[i])
            i += 1
            continue
        i += 1
        if i >= len(s):
            out.append("\\")
            break
        ch = s[i]
        out.append({"p": "|", "s": ";", "c": ":"}.get(ch, ch))
        i += 1
    return "".join(out)


def _encode(obj) -> str:
    if obj is None:
        return "N"
    if obj is True:
        return "T"
    if obj is False:
        return "F"
    if isinstance(obj, int):
        return "I:" + str(obj)
    if isinstance(obj, float):
        return "R:" + repr(obj)
    if isinstance(obj, str):
        return "S:" + _escape(obj)
    if isinstance(obj, bytes):
        return "B:" + obj.hex()
    if isinstance(obj, bytearray):
        return "Y:" + bytes(obj).hex()
    if isinstance(obj, list):
        return "L:" + ";".join(_encode(x) for x in obj)
    if isinstance(obj, tuple):
        return "U:" + ";".join(_encode(x) for x in obj)
    if isinstance(obj, set):
        return "E:" + ";".join(_encode(x) for x in obj)
    if isinstance(obj, dict):
        return "D:" + ";".join(_encode(k) + "|" + _encode(v) for k, v in obj.items())
    if hasattr(obj, "__getstate__"):
        return "O:" + _encode(obj.__getstate__())
    if hasattr(obj, "__dict__"):
        return "O:" + _encode(obj.__dict__)
    raise PicklingError("unsupported pickle object: " + type(obj).__name__)


def _split_top(text: str, sep: str) -> list[str]:
    out = []
    cur = []
    esc = False
    for ch in text:
        if esc:
            cur.append("\\" + ch)
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == sep:
            out.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    out.append("".join(cur))
    return [p for p in out if p != ""]


def _decode(text: str):
    if text == "N":
        return None
    if text == "T":
        return True
    if text == "F":
        return False
    if len(text) < 2 or text[1] != ":":
        raise UnpicklingError("invalid pickle payload")
    tag = text[0]
    body = text[2:]
    if tag == "I":
        return int(body)
    if tag == "R":
        return float(body)
    if tag == "S":
        return _unescape(body)
    if tag == "B":
        return bytes.fromhex(body)
    if tag == "Y":
        return bytearray(bytes.fromhex(body))
    if tag == "L":
        return [_decode(p) for p in _split_top(body, ";")]
    if tag == "U":
        return tuple(_decode(p) for p in _split_top(body, ";"))
    if tag == "E":
        return set(_decode(p) for p in _split_top(body, ";"))
    if tag == "D":
        out = {}
        for item in _split_top(body, ";"):
            k, v = item.split("|", 1)
            out[_decode(k)] = _decode(v)
        return out
    if tag == "O":
        return _decode(body)
    raise UnpicklingError("unknown pickle tag: " + tag)


def dumps(obj, protocol=None) -> bytes:
    host = _host_pickle()
    if host is not None:
        if protocol is None:
            protocol = DEFAULT_PROTOCOL
        return host.dumps(obj, protocol=protocol)
    return ("PCCPICKLE1|" + _encode(obj)).encode("utf-8")


def loads(data):
    prefix = "PCCPICKLE1|"
    if isinstance(data, bytes):
        prefix_bytes = prefix.encode("utf-8")
        if not data.startswith(prefix_bytes):
            host = _host_pickle()
            if host is not None:
                return host.loads(data)
            raise UnpicklingError("unsupported pickle protocol")
        text = data.decode("utf-8")
    else:
        text = str(data)
    if not text.startswith(prefix):
        host = _host_pickle()
        if host is not None:
            return host.loads(data)
        raise UnpicklingError("unsupported pickle protocol")
    return _decode(text[len(prefix):])


def dump(obj, file, protocol=None) -> None:
    file.write(dumps(obj, protocol=protocol))


def load(file):
    return loads(file.read())
