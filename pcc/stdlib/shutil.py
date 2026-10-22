"""pcc.stdlib.shutil — narrow ``shutil`` skeleton."""
from __future__ import annotations

from pcc.extern import extern, c_int, c_str


_access: "extern" = extern("access", (c_str, c_int), c_int)


def which(cmd: str) -> str:
    """Find ``cmd`` on ``PATH`` and return its absolute path or the
    empty string. Skeleton — real implementation needs iterating
    PATH entries."""
    host_shutil = __import__("shutil")
    found = host_shutil.which(cmd)
    return found or ""


def copyfile(src: str, dst: str) -> str:
    host_shutil = __import__("shutil")
    return host_shutil.copyfile(src, dst)


def rmtree(path: str, ignore_errors: bool = False) -> None:
    host_shutil = __import__("shutil")
    host_shutil.rmtree(path, ignore_errors=ignore_errors)


def copy(src: str, dst: str) -> str:
    return copyfile(src, dst)


def move(src: str, dst: str) -> str:
    host_shutil = __import__("shutil")
    return host_shutil.move(src, dst)
