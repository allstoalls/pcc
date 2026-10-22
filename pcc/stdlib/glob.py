"""pcc.stdlib.glob - shell-style pathname expansion.

Scope: the surface build tools use — ``glob``, ``iglob``, ``escape`` and
``has_magic``. Recursive ``**`` is supported for the common
``dir/**/pattern`` shape. Hidden files stay hidden unless the pattern's
matching component starts with a dot, as in CPython.
"""
from __future__ import annotations

import os

try:
    from pcc.stdlib import fnmatch as _fnmatch
except ImportError:
    # pcc publishes this provider as the top-level stdlib module ``fnmatch``.
    import fnmatch as _fnmatch

magic_check = "*?["


def has_magic(s: str) -> bool:
    for ch in magic_check:
        if ch in s:
            return True
    return False


def escape(pathname: str) -> str:
    """Wrap the magic characters in ``[]`` so they match literally."""
    out = ""
    for ch in pathname:
        if ch in magic_check:
            out = out + "[" + ch + "]"
        else:
            out = out + ch
    return out


def _listdir(dirname: str):
    try:
        return sorted(os.listdir(dirname if dirname != "" else "."))
    except Exception:
        return []


def _is_dir(path: str) -> bool:
    try:
        os.listdir(path)
        return True
    except Exception:
        return False


def _match_names(dirname: str, pattern: str):
    names = _listdir(dirname)
    hidden_ok = pattern.startswith(".")
    out = []
    for name in names:
        if not hidden_ok and name.startswith("."):
            continue
        if _fnmatch.fnmatch(name, pattern):
            out.append(name)
    return out


def _walk_all(root: str):
    """Yield ``root`` plus every directory beneath it, breadth-first."""
    out = [root]
    stack = [root]
    while len(stack) > 0:
        current = stack.pop()
        for name in _listdir(current):
            if name.startswith("."):
                continue
            child = os.path.join(current, name) if current != "" else name
            if _is_dir(child):
                out.append(child)
                stack.append(child)
    return out


def iglob(pathname: str, **kwargs):
    """Return an iterator over the paths matching ``pathname``."""
    return iter(glob(pathname, **kwargs))


def glob(pathname: str, **kwargs):
    """Return a sorted list of paths matching ``pathname``."""
    recursive = kwargs.get("recursive", False)
    if pathname == "":
        return []
    dirname, _, basename = pathname.rpartition("/")
    if basename == "":
        # Trailing slash: the pattern names directories only.
        results = []
        for entry in glob(dirname, recursive=recursive):
            if _is_dir(entry):
                results.append(entry + "/")
        return results
    if not has_magic(pathname):
        if dirname == "":
            return [pathname] if _exists(pathname) else []
        return [pathname] if _exists(pathname) else []
    if dirname == "":
        return sorted(_match_names("", basename))
    if recursive and dirname.endswith("**"):
        base = dirname[:-2].rstrip("/")
        results = []
        for directory in _walk_all(base):
            for name in _match_names(directory, basename):
                # base "" means the pattern was rooted at the cwd; CPython
                # yields "a.py" there, not "./a.py".
                results.append(os.path.join(directory, name) if directory != "" else name)
        return sorted(results)
    results = []
    for parent in glob(dirname, recursive=recursive) if has_magic(dirname) else [dirname]:
        for name in _match_names(parent, basename):
            results.append(os.path.join(parent, name))
    return sorted(results)


def _exists(path: str) -> bool:
    return os.path.exists(path)
