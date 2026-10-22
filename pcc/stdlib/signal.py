"""POSIX signal constants and reporting for pcc-native build tools.

Meson imports :mod:`signal` both for process-result formatting and for optional
interactive/test-runner handlers.  Numeric signal metadata, ``Signals`` name
lookup, ``valid_signals`` and ``strsignal`` are owned here without libpython or
a host interpreter.  Native default/ignore dispositions are safely queried
and installed with ``sigaction``.  Installing a Python callable is a different
runtime contract: pcc does not yet have an async-signal-safe Python callback
trampoline, so callable handlers fail closed instead of pretending they were
registered.
"""
from __future__ import annotations

import sys

from pcc.extern import c_int, c_int64, c_ptr, c_uint32, extern, c_obj, c_rawptr
from pcc.unsafe import (
    free,
    int_to_ptr,
    load_ptr,
    malloc,
    memset,
    null,
    ptr_add,
    ptr_is_null,
    ptr_to_int,
    store_ptr,
)


_strsignal: "extern" = extern("strsignal", (c_int,), c_rawptr)
_strlen: "extern" = extern("strlen", (c_ptr,), c_int64)
_py_str_new: "extern" = extern("py_str_new", (c_ptr, c_int64), c_obj)
_raise_signal: "extern" = extern("raise", (c_int,), c_int)
_alarm: "extern" = extern("alarm", (c_uint32,), c_uint32)
_sigaction: "extern" = extern("sigaction", (c_int, c_ptr, c_ptr), c_int)
_sigemptyset: "extern" = extern("sigemptyset", (c_ptr,), c_int)


SIG_DFL = 0
SIG_IGN = 1

SIGHUP = 1
SIGINT = 2
SIGQUIT = 3
SIGILL = 4
SIGTRAP = 5
SIGABRT = 6
SIGIOT = SIGABRT
SIGFPE = 8
SIGKILL = 9
SIGSEGV = 11
SIGPIPE = 13
SIGALRM = 14
SIGTERM = 15
SIGTTIN = 21
SIGTTOU = 22
SIGXCPU = 24
SIGXFSZ = 25
SIGVTALRM = 26
SIGPROF = 27
SIGWINCH = 28

if sys.platform.startswith("darwin"):
    SIGEMT = 7
    SIGBUS = 10
    SIGSYS = 12
    SIGURG = 16
    SIGSTOP = 17
    SIGTSTP = 18
    SIGCONT = 19
    SIGCHLD = 20
    SIGCLD = SIGCHLD
    SIGIO = 23
    SIGPOLL = SIGIO
    SIGINFO = 29
    SIGUSR1 = 30
    SIGUSR2 = 31
    NSIG = 32
elif sys.platform.startswith("linux"):
    SIGBUS = 7
    SIGUSR1 = 10
    SIGUSR2 = 12
    SIGSTKFLT = 16
    SIGCHLD = 17
    SIGCLD = SIGCHLD
    SIGCONT = 18
    SIGSTOP = 19
    SIGTSTP = 20
    SIGURG = 23
    SIGIO = 29
    SIGPOLL = SIGIO
    SIGPWR = 30
    SIGSYS = 31
    SIGRTMIN = 34
    SIGRTMAX = 64
    NSIG = 65
else:
    raise NotImplementedError(
        "pcc.stdlib.signal currently owns only Darwin and Linux ABIs"
    )


def _signal_name(number):
    common = {
        1: "SIGHUP",
        2: "SIGINT",
        3: "SIGQUIT",
        4: "SIGILL",
        5: "SIGTRAP",
        6: "SIGABRT",
        8: "SIGFPE",
        9: "SIGKILL",
        11: "SIGSEGV",
        13: "SIGPIPE",
        14: "SIGALRM",
        15: "SIGTERM",
        21: "SIGTTIN",
        22: "SIGTTOU",
        24: "SIGXCPU",
        25: "SIGXFSZ",
        26: "SIGVTALRM",
        27: "SIGPROF",
        28: "SIGWINCH",
    }
    name = common.get(number)
    if name is not None:
        return name
    if sys.platform.startswith("darwin"):
        return {
            7: "SIGEMT",
            10: "SIGBUS",
            12: "SIGSYS",
            16: "SIGURG",
            17: "SIGSTOP",
            18: "SIGTSTP",
            19: "SIGCONT",
            20: "SIGCHLD",
            23: "SIGIO",
            29: "SIGINFO",
            30: "SIGUSR1",
            31: "SIGUSR2",
        }.get(number)
    return {
        7: "SIGBUS",
        10: "SIGUSR1",
        12: "SIGUSR2",
        16: "SIGSTKFLT",
        17: "SIGCHLD",
        18: "SIGCONT",
        19: "SIGSTOP",
        20: "SIGTSTP",
        23: "SIGURG",
        29: "SIGIO",
        30: "SIGPWR",
        31: "SIGSYS",
        34: "SIGRTMIN",
        64: "SIGRTMAX",
    }.get(number)


class Signals:
    """Small IntEnum-compatible view used by Meson's status formatter."""

    def __init__(self, value):
        number = int(value)
        name = _signal_name(number)
        if name is None:
            raise ValueError(str(value) + " is not a valid Signals")
        self._value = number
        self._name = name

    @property
    def name(self):
        return self._name

    @property
    def value(self):
        return self._value

    def __int__(self):
        return self._value

    def __index__(self):
        return self._value

    def __eq__(self, other):
        try:
            return self._value == int(other)
        except (TypeError, ValueError):
            return False

    def __hash__(self):
        return hash(self._value)

    def __repr__(self):
        return "<Signals." + self._name + ": " + str(self._value) + ">"

    def __str__(self):
        return str(self._value)


def valid_signals():
    values = set()
    if sys.platform.startswith("darwin"):
        number = 1
        while number < NSIG:
            values.add(number)
            number += 1
        return values
    number = 1
    while number < 32:
        values.add(number)
        number += 1
    number = 34
    while number < NSIG:
        values.add(number)
        number += 1
    return values


def strsignal(signalnum):
    number = int(signalnum)
    if number <= 0 or number >= NSIG:
        raise ValueError("signal number out of range")
    raw = _strsignal(number)
    if ptr_is_null(raw):
        raise ValueError("signal number out of range")
    return _py_str_new(raw, _strlen(raw))


def raise_signal(signalnum):
    if _raise_signal(int(signalnum)) != 0:
        raise OSError("failed to raise signal")


def alarm(seconds):
    value = int(seconds)
    if value < 0 or value > 4294967295:
        raise OverflowError("alarm timeout does not fit unsigned int")
    return _alarm(value)


def default_int_handler(signalnum, frame):
    raise KeyboardInterrupt()


def _native_disposition(signalnum):
    action = malloc(160)
    if ptr_is_null(action):
        raise MemoryError("unable to allocate sigaction state")
    status = _sigaction(int(signalnum), null(), action)
    if status != 0:
        free(action)
        raise OSError("sigaction query failed")
    disposition = ptr_to_int(load_ptr(action, 0))
    free(action)
    if disposition == SIG_DFL or disposition == SIG_IGN:
        return disposition
    raise NotImplementedError(
        "native signal handler is not a pcc-owned Python callback"
    )


def signal(signalnum, handler):
    if handler != SIG_DFL and handler != SIG_IGN:
        raise NotImplementedError(
            "Python signal callbacks require an async-signal-safe "
            "pcc runtime trampoline"
        )
    previous = _native_disposition(signalnum)
    action = malloc(160)
    if ptr_is_null(action):
        raise MemoryError("unable to allocate sigaction state")
    memset(action, 0, 160)
    store_ptr(action, 0, int_to_ptr(int(handler)))
    if _sigemptyset(ptr_add(action, 8)) != 0:
        free(action)
        raise OSError("sigemptyset failed")
    status = _sigaction(int(signalnum), action, null())
    free(action)
    if status != 0:
        raise OSError("sigaction update failed")
    return previous


def getsignal(signalnum):
    return _native_disposition(signalnum)


def pause():
    raise NotImplementedError(
        "signal.pause requires installed Python signal callback dispatch"
    )


def pthread_kill(thread_id, signalnum):
    raise NotImplementedError("pthread signal delivery is not yet runtime-owned")


def pthread_sigmask(how, mask):
    raise NotImplementedError("pthread signal masks are not yet runtime-owned")


def sigpending():
    raise NotImplementedError("pending signal sets are not yet runtime-owned")


def sigwait(sigset):
    raise NotImplementedError("synchronous signal waits are not yet runtime-owned")


def set_wakeup_fd(fd, warn_on_full_buffer=True):
    raise NotImplementedError(
        "signal wakeup file descriptors are not yet runtime-owned"
    )


__all__ = [
    "Signals",
    "SIG_DFL",
    "SIG_IGN",
    "NSIG",
    "SIGHUP",
    "SIGINT",
    "SIGQUIT",
    "SIGILL",
    "SIGTRAP",
    "SIGABRT",
    "SIGIOT",
    "SIGFPE",
    "SIGKILL",
    "SIGSEGV",
    "SIGPIPE",
    "SIGALRM",
    "SIGTERM",
    "SIGTTIN",
    "SIGTTOU",
    "SIGXCPU",
    "SIGXFSZ",
    "SIGVTALRM",
    "SIGPROF",
    "SIGWINCH",
    "signal",
    "getsignal",
    "valid_signals",
    "strsignal",
    "raise_signal",
    "alarm",
    "pause",
    "default_int_handler",
    "pthread_kill",
    "pthread_sigmask",
    "sigpending",
    "sigwait",
    "set_wakeup_fd",
]

if sys.platform.startswith("darwin"):
    __all__ += [
        "SIGEMT",
        "SIGBUS",
        "SIGSYS",
        "SIGURG",
        "SIGSTOP",
        "SIGTSTP",
        "SIGCONT",
        "SIGCHLD",
        "SIGCLD",
        "SIGIO",
        "SIGPOLL",
        "SIGINFO",
        "SIGUSR1",
        "SIGUSR2",
    ]
else:
    __all__ += [
        "SIGBUS",
        "SIGUSR1",
        "SIGUSR2",
        "SIGSTKFLT",
        "SIGCHLD",
        "SIGCLD",
        "SIGCONT",
        "SIGSTOP",
        "SIGTSTP",
        "SIGURG",
        "SIGIO",
        "SIGPOLL",
        "SIGPWR",
        "SIGSYS",
        "SIGRTMIN",
        "SIGRTMAX",
    ]
