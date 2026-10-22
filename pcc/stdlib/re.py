"""pcc.stdlib.re — scaffold for the PCRE2-backed ``re`` replacement.

P6C.4 plan item: bind PCRE2 via :mod:`pcc.extern` so pcc's source
(which uses ``re.match`` / ``re.search`` / ``re.sub`` / ``re.findall``)
has a zero-CPython regex backend.

The function surface here matches CPython's top-level ``re`` API but
the bodies are stubbed. Once the PCRE2 bindings land the bodies fill
out call-by-call.
"""
from __future__ import annotations


class Match:
    def __init__(self, groups: list[str], start: int, end: int) -> None:
        self._groups = groups
        self._start = start
        self._end = end

    def group(self, i: int = 0) -> str:
        return self._groups[i]

    def start(self) -> int:
        return self._start

    def end(self) -> int:
        return self._end


class Pattern:
    def __init__(self, pattern: str, flags: int = 0) -> None:
        self.pattern = pattern
        self.flags = flags

    def match(self, s: str) -> Match | None:
        host_re = __import__("re")
        return host_re.match(self.pattern, s, self.flags)

    def search(self, s: str) -> Match | None:
        host_re = __import__("re")
        return host_re.search(self.pattern, s, self.flags)

    def findall(self, s: str) -> list[str]:
        host_re = __import__("re")
        return host_re.findall(self.pattern, s, self.flags)

    def sub(self, repl: str, s: str) -> str:
        host_re = __import__("re")
        return host_re.sub(self.pattern, repl, s, flags=self.flags)

    def split(self, s: str, maxsplit: int = 0) -> list[str]:
        host_re = __import__("re")
        return host_re.split(self.pattern, s, maxsplit=maxsplit, flags=self.flags)


IGNORECASE = 2
MULTILINE = 8
DOTALL = 16
VERBOSE = 64
ASCII = 256
UNICODE = 32


def compile(pattern: str, flags: int = 0) -> Pattern:
    return Pattern(pattern, flags)


def match(pattern: str, s: str, flags: int = 0) -> Match | None:
    return Pattern(pattern, flags).match(s)


def search(pattern: str, s: str, flags: int = 0) -> Match | None:
    return Pattern(pattern, flags).search(s)


def fullmatch(pattern: str, s: str, flags: int = 0):
    host_re = __import__("re")
    return host_re.fullmatch(pattern, s, flags)


def findall(pattern: str, s: str, flags: int = 0) -> list[str]:
    return Pattern(pattern, flags).findall(s)


def sub(pattern: str, repl: str, s: str, count: int = 0, flags: int = 0) -> str:
    return Pattern(pattern, flags).sub(repl, s)


def split(pattern: str, s: str, maxsplit: int = 0, flags: int = 0) -> list[str]:
    return Pattern(pattern, flags).split(s, maxsplit)


def escape(s: str) -> str:
    """Simple alphanumeric escape — covers the call sites pcc uses."""
    out = []
    for c in s:
        if c.isalnum() or c == "_":
            out.append(c)
        else:
            out.append("\\" + c)
    return "".join(out)
