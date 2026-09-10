"""pcc.py_stdlib.heapq - pure Python binary heap, no libpython fallback.

CPython's own `heapq` opens with `from _heapq import *`, which is a star
import of a C accelerator: under `--python-libpython=off` that resolves
through `py_cpy_getattr` and forces the whole multi-file compile onto
libpython. `asyncio` imports `heappush`/`heappop` from here for its timer
queue, so that single star import was enough to keep native asyncio from
compiling at all.

The invariant and the sift algorithms are CPython's: for every index i,
`heap[i] <= heap[2*i+1]` and `heap[i] <= heap[2*i+2]`, with `heap[0]` the
smallest item. Comparisons use `<` only, as CPython's does, so any type with
`__lt__` works and no `__le__` or `__gt__` is required.
"""
from __future__ import annotations


def _siftdown(heap, startpos: int, pos: int) -> None:
    """Move heap[pos] up toward startpos until its parent is not larger."""
    newitem = heap[pos]
    while pos > startpos:
        parentpos = (pos - 1) >> 1
        parent = heap[parentpos]
        if newitem < parent:
            heap[pos] = parent
            pos = parentpos
            continue
        break
    heap[pos] = newitem


def _siftup(heap, pos: int) -> None:
    """Move heap[pos] down, then sift it back up from where it lands."""
    endpos = len(heap)
    startpos = pos
    newitem = heap[pos]
    childpos = 2 * pos + 1
    while childpos < endpos:
        rightpos = childpos + 1
        if rightpos < endpos and not heap[childpos] < heap[rightpos]:
            childpos = rightpos
        heap[pos] = heap[childpos]
        pos = childpos
        childpos = 2 * pos + 1
    heap[pos] = newitem
    _siftdown(heap, startpos, pos)


def heappush(heap, item) -> None:
    """Push item onto heap, keeping the heap invariant."""
    heap.append(item)
    _siftdown(heap, 0, len(heap) - 1)


def heappop(heap):
    """Pop and return the smallest item, keeping the heap invariant."""
    lastelt = heap.pop()
    if heap:
        returnitem = heap[0]
        heap[0] = lastelt
        _siftup(heap, 0)
        return returnitem
    return lastelt


def heapreplace(heap, item):
    """Pop and return the smallest item, then push item.

    More efficient than a heappop followed by a heappush, and the size of the
    heap never changes, which matters for a fixed-size queue.
    """
    returnitem = heap[0]
    heap[0] = item
    _siftup(heap, 0)
    return returnitem


def heappushpop(heap, item):
    """Push item, then pop and return the smallest item."""
    if heap and heap[0] < item:
        item, heap[0] = heap[0], item
        _siftup(heap, 0)
    return item


def heapify(x) -> None:
    """Rearrange x into a heap in linear time."""
    n = len(x)
    i = n // 2 - 1
    while i >= 0:
        _siftup(x, i)
        i -= 1
