"""pcc.stdlib.pathlib — narrow ``PurePath`` / ``Path``.

Uses ``os.path`` under the hood for the join/split logic. The full
Pathlib has ~60 methods; this provider covers the ones pcc's source,
build scripts and package tooling (``pcc -m pcc.package``) touch.
"""
from __future__ import annotations

# ``Path`` defines ``open``; inside a provider module a bare ``open`` would
# bind to it, so the builtin is spelled ``builtins.open`` (as zipfile does).
import builtins
import os
from os import path as _op


class PurePath(os.PathLike):
    def __init__(self, path="", *extra) -> None:
        raw = str(path)
        for part in extra:
            raw = _op.join(raw, str(part))
        self._raw = raw

    def __str__(self) -> str:

        return self._raw

    def __fspath__(self) -> str:
        return self._raw

    def __repr__(self) -> str:
        return f"PurePath({self._raw!r})"

    def _normalized(self) -> str:
        """CPython's string form: no empty or ``.`` parts, no trailing sep.

        Construction keeps the raw join (``str`` and ``__fspath__`` still
        return it); comparison, hashing, ``parts`` and ``as_posix`` use this,
        as CPython's do.
        """
        sep = os.sep
        raw = self._raw
        drive = ""
        tail = raw
        if sep == "\\":
            raw = raw.replace("/", "\\")
            drive, tail = _op.splitdrive(raw)
        root = ""
        if tail.startswith(sep):
            root = sep
            # POSIX keeps exactly two leading slashes, as CPython does.
            if sep == "/" and tail.startswith("//") and not tail.startswith("///"):
                root = "//"
        parts: list = []
        for part in tail.split(sep):
            if part and part != ".":
                parts.append(part)
        text = drive + root + sep.join(parts)
        if not text:
            return "."
        return text

    # The comparison dunders carry no ``-> bool``: they may answer
    # NotImplemented, which a bool return type would turn into True.
    def _compare_text(self) -> str:
        text = self._normalized()
        if os.sep == "\\":
            return text.lower()
        return text

    def __eq__(self, other):
        if not isinstance(other, PurePath):
            return NotImplemented
        return self._compare_text() == other._compare_text()

    def __hash__(self) -> int:
        return hash(self._compare_text())

    def __lt__(self, other):
        if not isinstance(other, PurePath):
            return NotImplemented
        return self._compare_text().split(os.sep) < other._compare_text().split(os.sep)

    def __le__(self, other):
        if not isinstance(other, PurePath):
            return NotImplemented
        return self._compare_text().split(os.sep) <= other._compare_text().split(os.sep)

    def __gt__(self, other):
        if not isinstance(other, PurePath):
            return NotImplemented
        return self._compare_text().split(os.sep) > other._compare_text().split(os.sep)

    def __ge__(self, other):
        if not isinstance(other, PurePath):
            return NotImplemented
        return self._compare_text().split(os.sep) >= other._compare_text().split(os.sep)

    @property
    def parts(self) -> tuple:
        text = self._normalized()
        if text == ".":
            return ()
        sep = os.sep
        drive = ""
        tail = text
        if sep == "\\":
            drive, tail = _op.splitdrive(text)
        out: list = []
        if tail.startswith(sep):
            anchor = drive + sep
            if sep == "/" and tail.startswith("//"):
                anchor = "//"
            out.append(anchor)
            tail = tail[len(anchor) - len(drive):]
        elif drive:
            out.append(drive)
        for part in tail.split(sep):
            if part:
                out.append(part)
        return tuple(out)

    def is_absolute(self) -> bool:
        if os.sep == "\\":
            drive, tail = _op.splitdrive(self._raw.replace("/", "\\"))
            return bool(drive) and tail.startswith("\\")
        return self._raw.startswith("/")

    def as_posix(self) -> str:
        return self._normalized().replace("\\", "/")

    def joinpath(self, *others) -> "PurePath":
        return self.__class__(self._raw, *others)

    def is_relative_to(self, other) -> bool:
        try:
            self.relative_to(other)
        except ValueError:
            return False
        return True

    def __truediv__(self, other) -> "PurePath":
        # ``self.__class__`` and not ``PurePath``: CPython preserves the subclass
        # through every combining operation, so ``Path(root) / "pcc"`` stays a
        # ``Path``.  Hard-coding the base class here dropped every filesystem
        # method from the result -- ``bootstrap_source_sha256`` builds its
        # paths this way and died on ``'PurePath' object has no attribute
        # 'is_file'``.  Compiled, that AttributeError was swallowed by
        # ``codegen_checksum``'s ``except Exception``, which answered
        # "unknown", which the fail-closed staleness rule read as a stale
        # archive -- so pcc1 rejected every runtime archive it was given.
        return self.__class__(_op.join(self._raw, str(other)))

    @property
    def name(self) -> str:
        return _op.basename(self._raw)

    @property
    def parent(self) -> "PurePath":
        return self.__class__(_op.dirname(self._raw))

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
        return self.__class__(base + suffix)

    def with_name(self, name: str) -> "PurePath":
        parent = _op.dirname(self._raw)
        if parent:
            return self.__class__(_op.join(parent, name))
        return self.__class__(name)

    def relative_to(self, other) -> "PurePath":
        """``self`` expressed relative to ``other``, or ``ValueError``.

        ``pcc/driver/bootstrap_cache_identity.py`` builds its source-set keys from
        ``path.relative_to(base)``; without it the compiled compiler could not
        compute its own identity and fell back to "unknown", which the
        fail-closed archive staleness rule then read as a stale runtime.
        """
        base = str(other)
        mine = self._raw
        if os.sep == "\\":
            base = base.replace("/", "\\")
            mine = mine.replace("/", "\\")
            base_drive, base_tail = _op.splitdrive(base)
            mine_drive, mine_tail = _op.splitdrive(mine)
            if _op.normcase(base_drive) != _op.normcase(mine_drive) or base_tail.startswith("\\") != mine_tail.startswith("\\"):
                raise ValueError(repr(mine) + " is not in the subpath of " + repr(base))
            base_parts: list = []
            mine_parts: list = []
            for part in base_tail.split("\\"):
                if part and part != ".":
                    base_parts.append(part)
            for part in mine_tail.split("\\"):
                if part and part != ".":
                    mine_parts.append(part)
            if len(base_parts) > len(mine_parts):
                raise ValueError(repr(mine) + " is not in the subpath of " + repr(base))
            index = 0
            while index < len(base_parts):
                if base_parts[index].lower() != mine_parts[index].lower():
                    raise ValueError(repr(mine) + " is not in the subpath of " + repr(base))
                index = index + 1
            relative = "\\".join(mine_parts[len(base_parts):])
            return self.__class__(relative if relative else ".")
        if base == mine:
            return self.__class__(".")
        prefix = base if base.endswith(os.sep) else base + os.sep
        if not mine.startswith(prefix):
            raise ValueError(
                repr(mine) + " is not in the subpath of " + repr(base)
            )
        return self.__class__(mine[len(prefix):])

    def match(self, pattern: str) -> bool:
        if pattern.startswith("*."):
            return self.name.endswith(pattern[1:])
        return self.name == pattern or self._raw == pattern


class _StatResult:
    """The ``os.stat_result`` fields ``Path.stat`` provides."""

    def __init__(self, mode: int, size: int, mtime: float) -> None:
        self.st_mode = mode
        self.st_size = size
        self.st_mtime = mtime


class Path(PurePath):
    @property
    def parents(self) -> list:
        """Same ancestors as ``PurePath.parents``, as ``Path`` objects."""
        out: list = []
        for raw in self._parent_raw_paths():
            out.append(self.__class__(raw))
        return out

    @classmethod
    def cwd(cls) -> "Path":
        return cls(os.getcwd())

    @classmethod
    def home(cls) -> "Path":
        return cls("~").expanduser()

    def expanduser(self) -> "Path":
        if not self._raw.startswith("~"):
            return self
        expanded = _op.expanduser(self._raw)
        if expanded.startswith("~"):
            raise RuntimeError("Could not determine home directory.")
        return self.__class__(expanded)

    def absolute(self) -> "Path":
        if os.sep == "\\":
            return Path(_op.abspath(self._raw))
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
        if os.sep == "\\":
            if strict and not _op.exists(self._raw):
                raise FileNotFoundError(self._raw)
            return Path(_op.realpath(self._raw))
        return Path(_op.normpath(self.absolute()._raw))

    @staticmethod
    def _name_matches(name: str, pattern: str) -> bool:
        """``*`` and ``?`` matching, which is what this tree's globs use.

        Deliberately not a full ``fnmatch``: importing the provider's
        ``fnmatch`` from here fails to resolve inside the compiled closure,
        and character classes appear in no call site.  A pattern containing
        ``[`` is rejected rather than silently mismatched.
        """
        if "[" in pattern:
            raise ValueError(
                "pcc pathlib glob supports * and ? only, got " + repr(pattern)
            )
        name_len = len(name)
        pattern_len = len(pattern)
        name_index = 0
        pattern_index = 0
        star_pattern = -1
        star_name = 0
        while name_index < name_len:
            if pattern_index < pattern_len and (
                pattern[pattern_index] == "?"
                or pattern[pattern_index] == name[name_index]
            ):
                name_index = name_index + 1
                pattern_index = pattern_index + 1
            elif pattern_index < pattern_len and pattern[pattern_index] == "*":
                star_pattern = pattern_index
                star_name = name_index
                pattern_index = pattern_index + 1
            elif star_pattern >= 0:
                pattern_index = star_pattern + 1
                star_name = star_name + 1
                name_index = star_name
            else:
                return False
        while pattern_index < pattern_len and pattern[pattern_index] == "*":
            pattern_index = pattern_index + 1
        return pattern_index == pattern_len

    def _walk_entries(self, recursive: bool) -> list:
        out: list = []
        stack: list = [self._raw]
        while stack:
            current = stack.pop()
            try:
                names = sorted(os.listdir(current))
            except OSError:
                continue
            for entry in names:
                full = _op.join(current, entry)
                out.append(full)
                if recursive and _op.isdir(full):
                    stack.append(full)
        return out

    def glob(self, pattern: str) -> list:
        """Non-recursive match in this directory, sorted like a sorted walk.

        CPython returns a generator; a list is the same thing to every caller
        in this tree and keeps the provider free of generator lowering.
        """
        out: list = []
        for full in self._walk_entries(False):
            if self._name_matches(_op.basename(full), pattern):
                out.append(self.__class__(full))
        out.sort(key=lambda item: str(item))
        return out

    def rglob(self, pattern: str) -> list:
        """Recursive ``glob``; ``"*"`` therefore means every entry below."""
        out: list = []
        for full in self._walk_entries(True):
            if self._name_matches(_op.basename(full), pattern):
                out.append(self.__class__(full))
        out.sort(key=lambda item: str(item))
        return out

    def exists(self) -> bool:
        return _op.exists(self._raw)

    def is_file(self) -> bool:
        # Both of these answered ``exists()``, so a directory was a file and a
        # file was a directory.  ``bootstrap_cache_identity`` branches on
        # ``entry.is_file()`` to decide between "hash this one file" and "walk
        # this tree": compiled, it took the file branch for the ``pcc``
        # package and hashed 2 paths where the host hashed 606, so pcc1's
        # codegen identity never matched the one recorded in a runtime
        # archive and every archive read as stale.  ``os.path.isfile`` and
        # ``isdir`` are both native and agree with CPython.
        return _op.isfile(self._raw)

    def is_dir(self) -> bool:
        return _op.isdir(self._raw)

    def is_symlink(self) -> bool:
        return _op.islink(self._raw)

    def iterdir(self) -> list:
        """Entries in ``os.listdir`` order; a list, as ``glob`` returns."""
        out: list = []
        for name in os.listdir(self._raw):
            out.append(self.__class__(_op.join(self._raw, name)))
        return out

    def mkdir(self, mode: int = 0o777, parents: bool = False,
              exist_ok: bool = False) -> None:
        try:
            os.mkdir(self._raw, mode)
        except FileNotFoundError:
            if not parents or self.parent == self:
                raise
            self.parent.mkdir(parents=True, exist_ok=True)
            self.mkdir(mode, parents=False, exist_ok=exist_ok)
        except OSError:
            if not exist_ok or not self.is_dir():
                raise

    def unlink(self, missing_ok: bool = False) -> None:
        try:
            os.unlink(self._raw)
        except FileNotFoundError:
            if not missing_ok:
                raise

    def rmdir(self) -> None:
        os.rmdir(self._raw)

    def rename(self, target) -> "Path":
        os.rename(self._raw, str(target))
        return self.__class__(target)

    def replace(self, target) -> "Path":
        os.replace(self._raw, str(target))
        return self.__class__(target)

    def chmod(self, mode: int) -> None:
        os.chmod(self._raw, mode)

    def stat(self) -> "_StatResult":
        """``st_mode`` (file type bits only), ``st_size`` and ``st_mtime``.

        A narrowing like ``resolve``'s: the owned ``os`` has no ``stat``
        yet, and these three are what this tree reads.
        """
        mtime = _op.getmtime(self._raw)
        size = _op.getsize(self._raw)
        mode = 0
        if _op.isdir(self._raw):
            mode = 0o040000
        elif _op.isfile(self._raw):
            mode = 0o100000
        return _StatResult(mode, size, mtime)

    def open(self, mode: str = "r", buffering: int = -1, encoding=None,
             errors=None, newline=None):
        if buffering != -1:
            raise NotImplementedError(
                "pcc pathlib open() supports the default buffering only"
            )
        if "b" in mode:
            return builtins.open(self._raw, mode)
        return builtins.open(self._raw, mode, encoding=encoding, errors=errors,
                             newline=newline)

    def write_bytes(self, data) -> int:
        with builtins.open(self._raw, "wb") as f:
            return f.write(data)

    def read_bytes(self) -> bytes:
        with builtins.open(self._raw, "rb") as f:
            return f.read()

    def read_text(
        self,
        encoding: str = "utf-8",
        errors: str = "strict",
        newline: str | None = None,
    ) -> str:
        # Match Path.read_text's universal-newline default. An explicit empty
        # string has different read semantics and must reach open unchanged.
        with builtins.open(
            self._raw, "r", encoding=encoding, errors=errors, newline=newline
        ) as f:
            return f.read()

    def write_text(
        self,
        s: str,
        encoding: str = "utf-8",
        errors: str = "strict",
        newline: str | None = None,
    ) -> int:
        with builtins.open(
            self._raw, "w", encoding=encoding, errors=errors, newline=newline
        ) as f:
            return f.write(s)
