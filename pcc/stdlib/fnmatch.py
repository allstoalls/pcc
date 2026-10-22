"""pcc.stdlib.fnmatch - shell-style filename matching.

Mirrors CPython's semantics: ``*`` matches everything, ``?`` any single
character, ``[seq]`` any character in seq, ``[!seq]`` any character not in
seq.

Matching is done by a direct glob matcher (``_glob_match``), NOT by compiling
``translate()``'s output: that output uses ``(?s:...)\\Z``, which is outside
pcc's native regex subset, so every compiled call raised NotImplementedError
under no-libpython. ``translate()`` is kept because it is public API and
CPython callers use it; it is no longer on the matching path.
"""
from __future__ import annotations

import os
import re

def _bracket(pat: str, i: int, n: int):
    """Translate the bracket expression starting at ``i`` (just past ``[``).

    Returns ``[next_index, regex_text]``. ``next_index`` is the index just
    past the closing ``]``; when the expression is unterminated it is ``i``
    and the text is the escaped literal ``[``.
    """
    j = i
    if j < n and pat[j] == "!":
        j = j + 1
    if j < n and pat[j] == "]":
        j = j + 1
    while j < n and pat[j] != "]":
        j = j + 1
    if j >= n:
        return [i, "\\["]
    stuff = pat[i:j]
    if "-" not in stuff:
        stuff = stuff.replace("\\", "\\\\")
    else:
        # Keep ranges intact while escaping literal '-' and backslashes.
        chunks = []
        start = i
        if pat[start] == "!":
            start = start + 1
        if start < j and pat[start] == "]":
            start = start + 1
        chunk_start = i
        idx = start
        while True:
            found = pat.find("-", idx, j)
            if found < 0:
                break
            chunks.append(pat[chunk_start:found])
            chunk_start = found + 1
            idx = found + 1
        chunks.append(pat[chunk_start:j])
        parts = []
        for chunk in chunks:
            parts.append(chunk.replace("\\", "\\\\").replace("-", "\\-"))
        stuff = "-".join(parts)
    stuff = re.sub("([&~|])", "\\\\\\1", stuff)
    if len(stuff) > 0 and stuff[0] == "!":
        stuff = "^" + stuff[1:]
    elif len(stuff) > 0 and (stuff[0] == "^" or stuff[0] == "["):
        stuff = "\\" + stuff
    return [j + 1, "[" + stuff + "]"]


def translate(pat: str) -> str:
    """Translate a shell pattern to a regular expression."""
    res = ""
    i = 0
    n = len(pat)
    while i < n:
        c = pat[i]
        i = i + 1
        if c == "*":
            while i < n and pat[i] == "*":
                i = i + 1
            res = res + ".*"
        elif c == "?":
            res = res + "."
        elif c == "[":
            out = _bracket(pat, i, n)
            res = res + out[1]
            if out[0] != i:
                i = out[0]
        else:
            res = res + re.escape(c)
    return "(?s:" + res + ")\\Z"


def _bracket_match(pat: str, j: int, ch: str):
    """Match ``ch`` against the bracket expression starting at ``pat[j] == '['``.

    Returns ``[matched, next_j]``. An unterminated ``[`` is a literal, matching
    CPython (whose translation emits an escaped ``[`` in that case).
    """
    n = len(pat)
    k = j + 1
    negate = False
    if k < n and pat[k] == "!":
        negate = True
        k = k + 1
    first = k
    # A ']' immediately after '[' or '[!' is a literal member.
    if k < n and pat[k] == "]":
        k = k + 1
    while k < n and pat[k] != "]":
        k = k + 1
    if k >= n:
        return [ch == "[", j + 1]
    matched = False
    idx = first
    while idx < k:
        # A '-' that is not first and not last denotes a range.
        if idx + 2 < k and pat[idx + 1] == "-":
            if pat[idx] <= ch and ch <= pat[idx + 2]:
                matched = True
            idx = idx + 3
        else:
            if pat[idx] == ch:
                matched = True
            idx = idx + 1
    if negate:
        matched = not matched
    return [matched, k + 1]


def _glob_match(name: str, pat: str) -> bool:
    """Iterative glob match with backtracking on ``*``.

    Implemented directly rather than through ``re`` because the translated
    pattern uses ``(?s:...)\\Z``, which is outside pcc's native regex subset;
    routing through the regex engine made every compiled fnmatch call raise
    NotImplementedError under no-libpython.
    """
    i = 0
    j = 0
    star = -1
    mark = 0
    n = len(name)
    m = len(pat)
    while i < n:
        if j < m and pat[j] == "*":
            star = j
            mark = i
            j = j + 1
        elif j < m and pat[j] == "?":
            i = i + 1
            j = j + 1
        elif j < m and pat[j] == "[":
            out = _bracket_match(pat, j, name[i])
            if out[0]:
                j = out[1]
                i = i + 1
            elif star >= 0:
                mark = mark + 1
                i = mark
                j = star + 1
            else:
                return False
        elif j < m and pat[j] == name[i]:
            i = i + 1
            j = j + 1
        elif star >= 0:
            mark = mark + 1
            i = mark
            j = star + 1
        else:
            return False
    while j < m and pat[j] == "*":
        j = j + 1
    return j == m


def fnmatchcase(name: str, pat: str) -> bool:
    """Case-sensitive match with no path normalization."""
    return _glob_match(name, pat)


def _norm(value: str) -> str:
    return os.path.normcase(value)


def fnmatch(name: str, pat: str) -> bool:
    """Match using the platform's case rules (posix: case-sensitive)."""
    return fnmatchcase(_norm(name), _norm(pat))


def filter(names, pat: str):
    """Return the sublist of ``names`` matching ``pat``."""
    out = []
    normalized_pat = _norm(pat)
    for name in names:
        if _glob_match(_norm(name), normalized_pat):
            out.append(name)
    return out
