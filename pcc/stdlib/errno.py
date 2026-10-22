"""pcc.stdlib.errno - POSIX error numbers.

Scope: the values pcc and the build tools actually test against.

Codes 1..34 agree between Linux and Darwin **except** EAGAIN/EDEADLK, which
are swapped (Linux EAGAIN=11 EDEADLK=35, Darwin EAGAIN=35 EDEADLK=11). Those
two are selected by platform; assuming a single table silently produced the
wrong EAGAIN on Darwin.
"""
from __future__ import annotations

import sys

_DARWIN = sys.platform == "darwin"

EAGAIN = 35 if _DARWIN else 11
EDEADLK = 11 if _DARWIN else 35
EWOULDBLOCK = EAGAIN

EPERM = 1
ENOENT = 2
ESRCH = 3
EINTR = 4
EIO = 5
ENXIO = 6
E2BIG = 7
ENOEXEC = 8
EBADF = 9
ECHILD = 10
ENOMEM = 12
EACCES = 13
EFAULT = 14
EBUSY = 16
EEXIST = 17
EXDEV = 18
ENODEV = 19
ENOTDIR = 20
EISDIR = 21
EINVAL = 22
ENFILE = 23
EMFILE = 24
ENOTTY = 25
ETXTBSY = 26
EFBIG = 27
ENOSPC = 28
ESPIPE = 29
EROFS = 30
EMLINK = 31
EPIPE = 32
EDOM = 33
ERANGE = 34

errorcode = {
    EPERM: "EPERM",
    ENOENT: "ENOENT",
    ESRCH: "ESRCH",
    EINTR: "EINTR",
    EIO: "EIO",
    ENXIO: "ENXIO",
    E2BIG: "E2BIG",
    ENOEXEC: "ENOEXEC",
    EBADF: "EBADF",
    ECHILD: "ECHILD",
    EAGAIN: "EAGAIN",
    EDEADLK: "EDEADLK",
    ENOMEM: "ENOMEM",
    EACCES: "EACCES",
    EFAULT: "EFAULT",
    EBUSY: "EBUSY",
    EEXIST: "EEXIST",
    EXDEV: "EXDEV",
    ENODEV: "ENODEV",
    ENOTDIR: "ENOTDIR",
    EISDIR: "EISDIR",
    EINVAL: "EINVAL",
    ENFILE: "ENFILE",
    EMFILE: "EMFILE",
    ENOTTY: "ENOTTY",
    ETXTBSY: "ETXTBSY",
    EFBIG: "EFBIG",
    ENOSPC: "ENOSPC",
    ESPIPE: "ESPIPE",
    EROFS: "EROFS",
    EMLINK: "EMLINK",
    EPIPE: "EPIPE",
    EDOM: "EDOM",
    ERANGE: "ERANGE",
}
