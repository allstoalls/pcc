"""pcc.stdlib.posixpath - POSIX path manipulation.

The string operations are implemented here rather than delegated to
``os.path``: delegating would make the differential tests tautological when
this module is imported under CPython (``os.path`` *is* CPython's posixpath
there), and the compiled program has no ``pcc.stdlib.os`` name to import.
Only the filesystem probes go through ``os``.
"""
from __future__ import annotations

import os

sep = "/"
extsep = "."
altsep = None
pathsep = ":"
curdir = "."
pardir = ".."
defpath = "/bin:/usr/bin"


def normcase(s: str) -> str:
    """POSIX leaves case alone."""
    return s


def isabs(s: str) -> bool:
    return s.startswith("/")


def split(p: str):
    """Split into ``(head, tail)`` at the last slash."""
    i = p.rfind("/") + 1
    head = p[:i]
    tail = p[i:]
    if head != "" and head != "/" * len(head):
        head = head.rstrip("/")
    return (head, tail)


def basename(p: str) -> str:
    return p[p.rfind("/") + 1 :]


def dirname(p: str) -> str:
    return split(p)[0]


def splitext(p: str):
    """Split into ``(root, ext)``; leading dots are not extensions."""
    slash = p.rfind("/")
    dot = p.rfind(".")
    if dot <= slash + 1:
        return (p, "")
    # A name of only dots ("..", "...") has no extension either.
    i = dot
    while i > slash + 1 and p[i - 1] == ".":
        i = i - 1
    if i == slash + 1:
        return (p, "")
    return (p[:dot], p[dot:])


def join(a: str, *parts: str) -> str:
    """Join path components; an absolute component restarts the path."""
    path = a
    for part in parts:
        if part.startswith("/"):
            path = part
        elif path == "" or path.endswith("/"):
            path = path + part
        else:
            path = path + "/" + part
    return path


def normpath(p: str) -> str:
    """Collapse ``.``/``..`` and duplicate slashes lexically."""
    if p == "":
        return "."
    initial_slashes = 0
    if p.startswith("/"):
        initial_slashes = 1
        # POSIX keeps exactly two leading slashes as-is.
        if p.startswith("//") and not p.startswith("///"):
            initial_slashes = 2
    comps = []
    for comp in p.split("/"):
        if comp == "" or comp == ".":
            continue
        if comp != ".." or (initial_slashes == 0 and len(comps) == 0) or (
            len(comps) > 0 and comps[-1] == ".."
        ):
            comps.append(comp)
        elif len(comps) > 0:
            comps.pop()
    out = "/".join(comps)
    if initial_slashes > 0:
        out = "/" * initial_slashes + out
    return out if out != "" else "."


def exists(p: str) -> bool:
    return os.path.exists(p)


def isdir(p: str) -> bool:
    try:
        os.listdir(p)
        return True
    except Exception:
        return False


def commonpath(paths):
    """Longest common leading path of ``paths`` (all absolute or all relative)."""
    if len(paths) == 0:
        raise ValueError("commonpath() arg is an empty sequence")
    split_paths = []
    for p in paths:
        parts = []
        for comp in p.split("/"):
            if comp != "" and comp != ".":
                parts.append(comp)
        split_paths.append(parts)
    absolute = paths[0].startswith("/")
    common = split_paths[0]
    for parts in split_paths[1:]:
        i = 0
        limit = len(common) if len(common) < len(parts) else len(parts)
        while i < limit and common[i] == parts[i]:
            i = i + 1
        common = common[:i]
    prefix = "/" if absolute else ""
    return prefix + "/".join(common)
