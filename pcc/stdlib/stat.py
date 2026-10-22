"""pcc.stdlib.stat - st_mode decoding.

Scope: the file-type and permission surface the build tools use
(S_ISDIR/S_ISREG/S_ISLNK, S_IMODE, S_IFMT and the permission bits).
"""
from __future__ import annotations

S_IFMT_MASK = 0o170000

S_IFDIR = 0o040000
S_IFCHR = 0o020000
S_IFBLK = 0o060000
S_IFREG = 0o100000
S_IFIFO = 0o010000
S_IFLNK = 0o120000
S_IFSOCK = 0o140000

S_ISUID = 0o4000
S_ISGID = 0o2000
S_ISVTX = 0o1000

S_IRWXU = 0o700
S_IRUSR = 0o400
S_IWUSR = 0o200
S_IXUSR = 0o100
S_IRWXG = 0o070
S_IRGRP = 0o040
S_IWGRP = 0o020
S_IXGRP = 0o010
S_IRWXO = 0o007
S_IROTH = 0o004
S_IWOTH = 0o002
S_IXOTH = 0o001

S_IREAD = S_IRUSR
S_IWRITE = S_IWUSR
S_IEXEC = S_IXUSR


def S_IFMT(mode: int) -> int:
    return mode & S_IFMT_MASK


def S_IMODE(mode: int) -> int:
    return mode & 0o7777


def S_ISDIR(mode: int) -> bool:
    return S_IFMT(mode) == S_IFDIR


def S_ISCHR(mode: int) -> bool:
    return S_IFMT(mode) == S_IFCHR


def S_ISBLK(mode: int) -> bool:
    return S_IFMT(mode) == S_IFBLK


def S_ISREG(mode: int) -> bool:
    return S_IFMT(mode) == S_IFREG


def S_ISFIFO(mode: int) -> bool:
    return S_IFMT(mode) == S_IFIFO


def S_ISLNK(mode: int) -> bool:
    return S_IFMT(mode) == S_IFLNK


def S_ISSOCK(mode: int) -> bool:
    return S_IFMT(mode) == S_IFSOCK
