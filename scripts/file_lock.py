"""Portable advisory file locking for build/qualification drivers.

The repository's build tools and pytest provisioning share lock files across
xdist workers and concurrent agents.  ``fcntl`` does not exist on Windows, so
every one of those call sites needs the same POSIX/Windows split; this module
owns it so a caller does not have to import the platform module itself.

The lock is advisory and process-scoped: the OS releases it when the process
exits, so a crashed holder never blocks the next run.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import IO, Iterator


def _acquire(stream: IO[bytes], blocking: bool) -> None:
    if os.name == "nt":  # pragma: no cover - Windows host
        import msvcrt

        stream.seek(0)
        if stream.read(1) == b"":
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        # LK_LOCK retries for about ten seconds before raising; LK_NBLCK
        # fails immediately with OSError.  Both are advisory byte locks.
        msvcrt.locking(
            stream.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1
        )
        return
    import fcntl

    flags = fcntl.LOCK_EX
    if not blocking:
        flags |= fcntl.LOCK_NB
    fcntl.flock(stream.fileno(), flags)


def _release(stream: IO[bytes]) -> None:
    if os.name == "nt":  # pragma: no cover - Windows host
        import msvcrt

        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl

    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def exclusive_file_lock(
    path: str | os.PathLike[str], *, blocking: bool = True
) -> Iterator[IO[bytes]]:
    """Hold an exclusive advisory lock on ``path`` for the ``with`` block."""

    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as stream:
        _acquire(stream, blocking)
        try:
            yield stream
        finally:
            _release(stream)


@contextlib.contextmanager
def try_exclusive_file_lock(
    path: str | os.PathLike[str],
) -> Iterator[bool]:
    """Yield True when the lock was taken, False when another owner holds it."""

    try:
        with exclusive_file_lock(path, blocking=False) as stream:
            yield True
            del stream
    except OSError:
        yield False
