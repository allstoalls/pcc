"""pcc.py_stdlib.shlex — narrow ``shlex`` skeleton."""
from __future__ import annotations


def split(s: str, comments: bool = False, posix: bool = True) -> list[str]:
    """Split a shell-style string respecting quotes. Tight POSIX subset
    covering pcc's callsites (build command parsing)."""
    if not posix:
        raise NotImplementedError("non-POSIX shlex splitting is not implemented")
    out: list[str] = []
    cur: list[str] = []
    started = False
    quote = ""
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if quote:
            if ch == quote:
                quote = ""
                i += 1
            elif ch == "\\" and quote == '"':
                if i + 1 == n:
                    raise ValueError("No escaped character")
                following = s[i + 1]
                if following in ('"', "\\"):
                    cur.append(following)
                else:
                    cur.append("\\")
                    cur.append(following)
                i += 2
            else:
                cur.append(ch)
                i += 1
            continue
        if ch in " \t\n\r":
            if started:
                out.append("".join(cur))
                cur = []
                started = False
            i += 1
            continue
        if comments and ch == "#":
            while i < n and s[i] != "\n":
                i += 1
            continue
        if ch in ('"', "'"):
            quote = ch
            started = True
            i += 1
            continue
        if ch == "\\":
            if i + 1 == n:
                raise ValueError("No escaped character")
            started = True
            cur.append(s[i + 1])
            i += 2
            continue
        cur.append(ch)
        started = True
        i += 1
    if quote:
        raise ValueError("No closing quotation")
    if started:
        out.append("".join(cur))
    return out


def quote(s: str) -> str:
    """Return a POSIX-safe quoted form of ``s``."""
    if not s:
        return "''"
    for c in s:
        if not (c.isalnum() or c in "_-./:="):
            return "'" + s.replace("'", "'\"'\"'") + "'"
    return s


def join(parts) -> str:
    return " ".join(quote(p) for p in parts)
