"""pcc.stdlib.itertools — narrow ``itertools`` replacement.

Scope matches pcc's own usage: chain, repeat, islice, product,
groupby, count.
"""
from __future__ import annotations


def _chain_impl(*iterables):
    for it in iterables:
        for item in it:
            yield item

def _chain_from_iterable(iterables):
    for it in iterables:
        for item in it:
            yield item


class _Chain:
    def __call__(self, *iterables):
        return _chain_impl(*iterables)

    def from_iterable(self, iterables):
        return _chain_from_iterable(iterables)


chain = _Chain()


def repeat(value, times=None):
    if times is None:
        while True:
            yield value
    else:
        for _ in range(times):
            yield value


def islice(iterable, *args):
    """``islice(iterable, stop)`` or ``islice(iterable, start, stop[, step])``."""
    if len(args) == 1:
        start, stop, step = 0, args[0], 1
    elif len(args) == 2:
        start, stop, step = args[0], args[1], 1
    elif len(args) == 3:
        start, stop, step = args
    else:
        raise TypeError("islice expects 1..3 positional args after iterable")
    if stop is None:
        # islice(iterable, start, None) is "from start to end, step".
        stop = 1 << 62
    i = 0
    nxt = start
    for item in iterable:
        if i >= stop:
            break
        if i == nxt:
            yield item
            nxt += step
        i += 1


def count(start=0, step=1):
    i = start
    while True:
        yield i
        i += step


def product(*iterables, repeat: int = 1):
    """Cartesian product."""
    pools = [list(it) for it in iterables] * repeat
    result: list[tuple] = [()]
    for pool in pools:
        new_result: list[tuple] = []
        for prefix in result:
            for item in pool:
                new_result.append(prefix + (item,))
        result = new_result
    for r in result:
        yield r


def groupby(iterable, key=None):
    if key is None:
        key = lambda x: x
    last_key = object()
    group: list = []
    for item in iterable:
        k = key(item)
        if group and k != last_key:
            yield (last_key, iter(group))
            group = []
        group.append(item)
        last_key = k
    if group:
        yield (last_key, iter(group))


def accumulate(iterable, func=None, *, initial=None):
    it = iter(iterable)
    if func is None:
        func = lambda a, b: a + b
    if initial is None:
        try:
            total = next(it)
        except StopIteration:
            return
    else:
        total = initial
    yield total
    for item in it:
        total = func(total, item)
        yield total


def takewhile(predicate, iterable):
    for item in iterable:
        if not predicate(item):
            break
        yield item


def dropwhile(predicate, iterable):
    it = iter(iterable)
    for item in it:
        if not predicate(item):
            yield item
            break
    for item in it:
        yield item


def starmap(function, iterable):
    for args in iterable:
        yield function(*args)


def compress(data, selectors):
    for item, keep in zip(data, selectors):
        if keep:
            yield item


def zip_longest(*iterables, fillvalue=None):
    iterators = [iter(it) for it in iterables]
    active = len(iterators)
    while active:
        row = []
        active = 0
        for it in iterators:
            try:
                value = next(it)
                row.append(value)
                active += 1
            except StopIteration:
                row.append(fillvalue)
        if active:
            yield tuple(row)


def tee(iterable, n=2):
    data = list(iterable)
    return tuple(iter(data) for _ in range(n))


def permutations(iterable, r=None):
    pool = tuple(iterable)
    n = len(pool)
    if r is None:
        r = n
    if r > n:
        return
    indices = list(range(n))
    cycles = list(range(n, n-r, -1))
    yield tuple(pool[i] for i in indices[:r])
    while n:
        for i in reversed(range(r)):
            cycles[i] -= 1
            if cycles[i] == 0:
                indices[i:] = indices[i+1:] + indices[i:i+1]
                cycles[i] = n - i
            else:
                j = cycles[i]
                indices[i], indices[-j] = indices[-j], indices[i]
                yield tuple(pool[i] for i in indices[:r])
                break
        else:
            return
