"""pcc.py_stdlib.pathlib — narrow ``PurePath`` / ``Path``.

Uses ``os.path`` under the hood for the join/split logic. The full
Pathlib has ~60 methods; this scaffold covers the 15 or so pcc's
source and build scripts touch.
"""
from __future__ import annotations

import os
from os import path as _op


class PurePath:
    def __init__(self, path="", *extra) -> None:
        raw = str(path)
        for part in extra:
            raw = _op.join(raw, str(part))
        self._raw = raw

    def __str__(self) -> str:

        return self._raw

    def __repr__(self) -> str:
        return f"PurePath({self._raw!r})"

    def __truediv__(self, other) -> "PurePath":
        return PurePath(_op.join(self._raw, str(other)))

    @property
    def name(self) -> str:
        return _op.basename(self._raw)

    @property
    def parent(self) -> "PurePath":
        return PurePath(_op.dirname(self._raw))

    def _parent_raw_paths(self) -> list:
        """Ancestor path strings, closest first, as CPython orders them.

        ``/a/b/c`` -> ``["/a/b", "/a", "/"]``; ``a/b/c`` -> ``["a/b", "a",
        "."]``; ``c`` -> ``["."]``; ``.``, ``/`` and ``""`` -> ``[]``.
        """
        out: list = []
        current = self._raw
        if current == "" or current == ".":
            return out
        while True:
            parent = _op.dirname(current)
            if parent == current:
                break
            if parent == "":
                out.append(".")
                break
            out.append(parent)
            current = parent
        return out

    @property
    def parents(self) -> list:
        """The ancestors, closest first.

        CPython returns a lazy sequence; a list is returned here because
        indexing and ``len`` are the whole surface callers use --
        ``pcc/package/inspect.py`` opens with
        ``Path(__file__).resolve().parents[2]``, which was a module-level
        CPython fallback while this was missing, and module-level code is
        outside the strict no-libpython stub projection.
        """
        out: list = []
        for raw in self._parent_raw_paths():
            out.append(PurePath(raw))
        return out

    @property
    def suffix(self) -> str:
        n = self.name
        i = n.rfind(".")
        if i <= 0:
            return ""
        return n[i:]

    @property
    def stem(self) -> str:
        n = self.name
        i = n.rfind(".")
        if i <= 0:
            return n
        return n[:i]

    def with_suffix(self, suffix: str) -> "PurePath":
        base, _old = _op.splitext(self._raw)
        return PurePath(base + suffix)

    def with_name(self, name: str) -> "PurePath":
        parent = _op.dirname(self._raw)
        if parent:
            return PurePath(_op.join(parent, name))
        return PurePath(name)

    def match(self, pattern: str) -> bool:
        if pattern.startswith("*."):
            return self.name.endswith(pattern[1:])
        return self.name == pattern or self._raw == pattern


class Path(PurePath):
    @property
    def parents(self) -> list:
        """Same ancestors as ``PurePath.parents``, as ``Path`` objects."""
        out: list = []
        for raw in self._parent_raw_paths():
            out.append(Path(raw))
        return out

    def absolute(self) -> "Path":
        if _op.isabs(self._raw):
            return Path(self._raw)
        return Path(_op.join(os.getcwd(), self._raw))

    def resolve(self, strict: bool = False) -> "Path":
        """Absolute, normalized path.

        Symlinks are NOT followed: there is no native ``realpath`` yet, so
        this is ``absolute()`` plus ``normpath``.  Same kind of documented
        narrowing as ``is_file``/``is_dir`` above, and it is what the callers
        in this tree need -- ``Path(__file__).resolve().parents[2]`` wants an
        absolute repo root, not link identity.
        """
        return Path(_op.normpath(self.absolute()._raw))

    def exists(self) -> bool:
        return _op.exists(self._raw)

    def is_file(self) -> bool:
        # Real os.path.isfile would stat; defer until extern stat lands.
        return _op.exists(self._raw)

    def is_dir(self) -> bool:
        # Same caveat as is_file.
        return _op.exists(self._raw)

    def read_bytes(self) -> bytes:
        with open(self._raw, "rb") as f:
            return f.read()

    def read_text(
        self,
        encoding: str = "utf-8",
        errors: str = "strict",
        newline: str = "",
    ) -> str:
        # ``errors`` and ``newline`` are part of CPython's signature and are
        # forwarded to ``open``, which accepts them as compatibility kwargs
        # (see ``codegen/native_files``).  Accepting them here is what lets
        # ordinary code such as ``path.read_text(encoding="utf-8",
        # errors="ignore")`` compile at all -- ``pcc/package/metadata.py``
        # spells it that way twice.
        with open(
            self._raw, "r", encoding=encoding, errors=errors, newline=newline
        ) as f:
            return f.read()

    def write_text(
        self,
        s: str,
        encoding: str = "utf-8",
        errors: str = "strict",
        newline: str = "",
    ) -> int:
        with open(
            self._raw, "w", encoding=encoding, errors=errors, newline=newline
        ) as f:
            return f.write(s)
