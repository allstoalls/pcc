"""pcc.stdlib.random - deterministic native subset."""
from __future__ import annotations

_STATE = 88172645463393265
_MOD = 1 << 63


def seed(a=None, version: int = 2):
    global _STATE
    if a is None:
        _STATE = 88172645463393265
    elif isinstance(a, int):
        _STATE = a & (_MOD - 1)
    else:
        value = 0
        text = str(a)
        for ch in text:
            value = ((value * 131) + ord(ch)) & (_MOD - 1)
        _STATE = value


def _next_u63():
    global _STATE
    _STATE = ((_STATE * 6364136223846793005) + 1442695040888963407) & (_MOD - 1)
    return _STATE


def getrandbits(k: int):
    if k <= 0:
        raise ValueError("number of bits must be greater than zero")
    out = 0
    bits = 0
    while bits < k:
        out = (out << 63) | _next_u63()
        bits += 63
    extra = bits - k
    if extra:
        out = out >> extra
    return out


def random():
    return (_next_u63() % 9007199254740992) / 9007199254740992.0


def randrange(start, stop=None, step: int = 1):
    if stop is None:
        stop = start
        start = 0
    if step == 0:
        raise ValueError("zero step for randrange()")
    width = stop - start
    if step == 1:
        if width <= 0:
            raise ValueError("empty range for randrange()")
        return start + (_next_u63() % width)
    n = (width + step - 1) // step if step > 0 else (width + step + 1) // step
    if n <= 0:
        raise ValueError("empty range for randrange()")
    return start + step * (_next_u63() % n)


def randint(a, b):
    return randrange(a, b + 1)


def choice(seq):
    if len(seq) == 0:
        raise IndexError("cannot choose from an empty sequence")
    return seq[randrange(len(seq))]


class Random:
    def __init__(self, x=None):
        self._state = 88172645463393265
        if x is not None:
            self.seed(x)

    def seed(self, a=None, version: int = 2):
        if a is None:
            self._state = 88172645463393265
        elif isinstance(a, int):
            self._state = a & (_MOD - 1)
        else:
            value = 0
            text = str(a)
            for ch in text:
                value = ((value * 131) + ord(ch)) & (_MOD - 1)
            self._state = value

    def _next_u63(self):
        self._state = ((self._state * 6364136223846793005) + 1442695040888963407) & (_MOD - 1)
        return self._state

    def random(self):
        return (self._next_u63() % 9007199254740992) / 9007199254740992.0

    def randrange(self, start, stop=None, step: int = 1):
        if stop is None:
            stop = start
            start = 0
        if step == 0:
            raise ValueError("zero step for randrange()")
        width = stop - start
        if step == 1:
            if width <= 0:
                raise ValueError("empty range for randrange()")
            return start + (self._next_u63() % width)
        n = (width + step - 1) // step if step > 0 else (width + step + 1) // step
        if n <= 0:
            raise ValueError("empty range for randrange()")
        return start + step * (self._next_u63() % n)

    def randint(self, a, b):
        return self.randrange(a, b + 1)

    def choice(self, seq):
        if len(seq) == 0:
            raise IndexError("cannot choose from an empty sequence")
        return seq[self.randrange(len(seq))]

    def getrandbits(self, k: int):
        if k <= 0:
            raise ValueError("number of bits must be greater than zero")
        out = 0
        bits = 0
        while bits < k:
            out = (out << 63) | self._next_u63()
            bits += 63
        extra = bits - k
        if extra:
            out = out >> extra
        return out
