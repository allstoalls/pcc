"""pcc.stdlib.bisect - small pure Python bisection helpers."""
from __future__ import annotations


def bisect_left(a, x, lo: int = 0, hi=None):
    if hi is None:
        hi = len(a)
    while lo < hi:
        mid = (lo + hi) // 2
        if a[mid] < x:
            lo = mid + 1
        else:
            hi = mid
    return lo


def bisect_right(a, x, lo: int = 0, hi=None):
    if hi is None:
        hi = len(a)
    while lo < hi:
        mid = (lo + hi) // 2
        if x < a[mid]:
            hi = mid
        else:
            lo = mid + 1
    return lo


def insort_left(a, x, lo: int = 0, hi=None):
    a.insert(bisect_left(a, x, lo, hi), x)


def insort_right(a, x, lo: int = 0, hi=None):
    a.insert(bisect_right(a, x, lo, hi), x)


bisect = bisect_right
insort = insort_right
