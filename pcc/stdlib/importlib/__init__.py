"""Finite, native :mod:`importlib` surface for linked pcc modules.

pcc owns an ahead-of-time compiled-module registry rather than a runtime
Python source/bytecode evaluator.  ``import_module`` therefore imports only
modules already linked into the executable (or a pinned pcc-native extension)
through the ordinary ``__import__`` runtime entry.  Unknown names fail closed;
there is no host-interpreter or filesystem source-execution fallback.
"""
from __future__ import annotations


__all__ = ["import_module", "invalidate_caches", "reload"]


def _resolve_name(name, package):
    if not isinstance(name, str):
        raise TypeError("module name must be a string")
    if name == "":
        raise ValueError("Empty module name")
    if "\x00" in name:
        raise ValueError("module name may not contain a null character")
    if not name.startswith("."):
        return name
    if not isinstance(package, str) or package == "":
        raise TypeError(
            "the 'package' argument is required to perform a relative import for "
            + repr(name)
        )
    if "\x00" in package:
        raise ValueError("package name may not contain a null character")

    level = 0
    while level < len(name) and name[level] == ".":
        level += 1
    base = package
    remaining = level - 1
    while remaining > 0:
        dot = base.rfind(".")
        if dot < 0:
            raise ImportError("attempted relative import beyond top-level package")
        base = base[:dot]
        remaining -= 1
    tail = name[level:]
    if tail:
        return base + "." + tail
    return base


def import_module(name, package=None):
    """Import and return one linked module.

    The non-empty ``fromlist`` is important: builtin ``__import__`` otherwise
    returns the top-level package for a dotted name, whereas ``import_module``
    returns the named leaf module.
    """
    absolute_name = _resolve_name(name, package)
    return __import__(absolute_name, None, None, ("*",), 0)


def invalidate_caches():
    """Invalidate import caches.

    The linked registry is immutable after executable startup and keeps no
    filesystem finder cache, so there is no state to invalidate.
    """
    return None


def reload(module):
    """Fail closed until compiled modules own repeat initialization."""
    raise NotImplementedError(
        "importlib.reload requires compiler-owned repeat module execution; "
        "the linked-module registry deliberately initializes each module once"
    )
