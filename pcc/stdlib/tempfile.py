"""pcc.stdlib.tempfile — narrow ``tempfile`` skeleton.

Plan: bind libc ``mkstemp`` / ``mkdtemp`` via :mod:`pcc.extern`. The
Python surface (``NamedTemporaryFile``, ``TemporaryDirectory``) is a
thin class-level wrapper over those calls.
"""
from __future__ import annotations


class NamedTemporaryFile:
    def __init__(self, mode: str = "w", delete: bool = True,
                 suffix: str = "", prefix: str = "tmp", dir=None) -> None:
        self.mode = mode
        self.delete = delete
        host_tempfile = __import__("tempfile")
        self._file = host_tempfile.NamedTemporaryFile(
            mode=mode,
            delete=delete,
            suffix=suffix,
            prefix=prefix,
            dir=dir,
        )
        self.name: str = self._file.name

    def write(self, text: str) -> int:
        return self._file.write(text)

    def flush(self) -> None:
        self._file.flush()

    def close(self) -> None:
        self._file.close()


class TemporaryDirectory:
    def __init__(self, suffix: str = "", prefix: str = "tmp", dir=None) -> None:
        host_tempfile = __import__("tempfile")
        self._tempdir = host_tempfile.TemporaryDirectory(
            suffix=suffix,
            prefix=prefix,
            dir=dir,
        )
        self.name: str = self._tempdir.name

    def __enter__(self):
        return self.name

    def __exit__(self, *exc) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        self._tempdir.cleanup()


def mkstemp(suffix: str = "", prefix: str = "tmp", dir=None, text: bool = False):
    host_tempfile = __import__("tempfile")
    return host_tempfile.mkstemp(
        suffix=suffix,
        prefix=prefix,
        dir=dir,
        text=text,
    )


def mkdtemp(suffix: str = "", prefix: str = "tmp", dir=None) -> str:
    host_tempfile = __import__("tempfile")
    return host_tempfile.mkdtemp(suffix=suffix, prefix=prefix, dir=dir)


def gettempdir() -> str:
    return "/tmp"
