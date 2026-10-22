"""Declarative libc/POSIX signature registry.

This is the first extraction point for the C roadmap item that moves ad-hoc
libc signatures out of monolithic codegen.  The API is intentionally small so
CCodeGenerator can adopt it incrementally without a flag day.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Tuple


@dataclass(frozen=True)
class LibcSignature:
    name: str
    return_type: str
    arg_types: tuple[str, ...]
    header: str
    platforms: tuple[str, ...] = ("linux", "darwin")
    confidence: str = "manual"

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "return_type": self.return_type,
            "arg_types": list(self.arg_types),
            "header": self.header,
            "platforms": list(self.platforms),
            "confidence": self.confidence,
        }


_SIGNATURES: Dict[str, LibcSignature] = {
    "malloc": LibcSignature("malloc", "void*", ("size_t",), "stdlib.h"),
    "free": LibcSignature("free", "void", ("void*",), "stdlib.h"),
    "memcpy": LibcSignature("memcpy", "void*", ("void*", "const void*", "size_t"), "string.h"),
    "memset": LibcSignature("memset", "void*", ("void*", "int", "size_t"), "string.h"),
    "strlen": LibcSignature("strlen", "size_t", ("const char*",), "string.h"),
    "printf": LibcSignature("printf", "int", ("const char*", "..."), "stdio.h"),
    "fprintf": LibcSignature("fprintf", "int", ("FILE*", "const char*", "..."), "stdio.h"),
    "open": LibcSignature("open", "int", ("const char*", "int", "..."), "fcntl.h"),
    "read": LibcSignature("read", "ssize_t", ("int", "void*", "size_t"), "unistd.h"),
    "write": LibcSignature("write", "ssize_t", ("int", "const void*", "size_t"), "unistd.h"),
    "close": LibcSignature("close", "int", ("int",), "unistd.h"),
    "__errno_location": LibcSignature("__errno_location", "int*", (), "errno.h", ("linux",)),
    "__error": LibcSignature("__error", "int*", (), "errno.h", ("darwin",)),
}


def lookup_signature(name: str, platform: str | None = None) -> LibcSignature | None:
    sig = _SIGNATURES.get(name)
    if sig is None:
        return None
    if platform is not None and platform not in sig.platforms:
        return None
    return sig


def iter_signatures(platform: str | None = None) -> tuple[LibcSignature, ...]:
    out = []
    for name in sorted(_SIGNATURES):
        sig = _SIGNATURES[name]
        if platform is None or platform in sig.platforms:
            out.append(sig)
    return tuple(out)


def register_signature(sig: LibcSignature, *, replace: bool = False) -> None:
    if not replace and sig.name in _SIGNATURES:
        raise ValueError(f"duplicate libc signature: {sig.name}")
    _SIGNATURES[sig.name] = sig


def validate_registry() -> None:
    seen = set()
    for sig in _SIGNATURES.values():
        if sig.name in seen:
            raise AssertionError(f"duplicate signature {sig.name}")
        seen.add(sig.name)
        if not sig.header:
            raise AssertionError(f"missing header for {sig.name}")
        if not sig.return_type:
            raise AssertionError(f"missing return type for {sig.name}")
