"""Repository-layout globals and real suspended owning-operand regressions.

Run one case per process. The 32 explicitly written call sites are intentional:
repeating one call in a loop would not exercise newly allocated root groups.
This fixture checks Python semantics; collector and IR evidence belongs to its
pytest wrapper. Alias survival is not proof that a particular object moved.
"""
import gc
import sys
import weakref

started = []
completed = []
violations = []
references = {}
weak_events = []
resurrected = []
__all__ = ["Witness", "grouped", "small"]


def collect():
    gc.collect()
    gc.collect()


def weak_gone(reference):
    weak_events.append(reference)


class Witness:
    def __init__(self, label, resurrect_at):
        self.label = label
        self.token = 73
        self.resurrect = label == resurrect_at
        references[label] = weakref.ref(self, weak_gone)

    def __del__(self):
        # Assertions outside this method check completion. An ignored exception
        # during finalization must not make the case appear successful.
        started.append(self.label)
        if self.token != 73:
            violations.append(self.label)
        if self.resurrect:
            resurrected.append(self)
        completed.append(self.label)


def consume(value, acknowledgement, fail_at):
    assert acknowledgement is None
    collect()
    assert references[value.label]() is value
    assert value.token == 73
    assert value.label not in completed
    if value.label == fail_at:
        raise ValueError("operand failure")
    return value.label


def grouped(stop, resurrect_at, fail_at):
    if stop == 0:
        return
    # Each first argument remains owned while its later argument suspends.
    yield consume(Witness(1, resurrect_at), (yield 1), fail_at)
    if stop == 1:
        return
    yield consume(Witness(2, resurrect_at), (yield 2), fail_at)
    if stop == 2:
        return
    yield consume(Witness(3, resurrect_at), (yield 3), fail_at)
    if stop == 3:
        return
    yield consume(Witness(4, resurrect_at), (yield 4), fail_at)
    if stop == 4:
        return
    yield consume(Witness(5, resurrect_at), (yield 5), fail_at)
    if stop == 5:
        return
    yield consume(Witness(6, resurrect_at), (yield 6), fail_at)
    if stop == 6:
        return
    yield consume(Witness(7, resurrect_at), (yield 7), fail_at)
    if stop == 7:
        return
    yield consume(Witness(8, resurrect_at), (yield 8), fail_at)
    if stop == 8:
        return
    yield consume(Witness(9, resurrect_at), (yield 9), fail_at)
    if stop == 9:
        return
    yield consume(Witness(10, resurrect_at), (yield 10), fail_at)
    if stop == 10:
        return
    yield consume(Witness(11, resurrect_at), (yield 11), fail_at)
    if stop == 11:
        return
    yield consume(Witness(12, resurrect_at), (yield 12), fail_at)
    if stop == 12:
        return
    yield consume(Witness(13, resurrect_at), (yield 13), fail_at)
    if stop == 13:
        return
    yield consume(Witness(14, resurrect_at), (yield 14), fail_at)
    if stop == 14:
        return
    yield consume(Witness(15, resurrect_at), (yield 15), fail_at)
    if stop == 15:
        return
    yield consume(Witness(16, resurrect_at), (yield 16), fail_at)
    if stop == 16:
        return
    yield consume(Witness(17, resurrect_at), (yield 17), fail_at)
    if stop == 17:
        return
    yield consume(Witness(18, resurrect_at), (yield 18), fail_at)
    if stop == 18:
        return
    yield consume(Witness(19, resurrect_at), (yield 19), fail_at)
    if stop == 19:
        return
    yield consume(Witness(20, resurrect_at), (yield 20), fail_at)
    if stop == 20:
        return
    yield consume(Witness(21, resurrect_at), (yield 21), fail_at)
    if stop == 21:
        return
    yield consume(Witness(22, resurrect_at), (yield 22), fail_at)
    if stop == 22:
        return
    yield consume(Witness(23, resurrect_at), (yield 23), fail_at)
    if stop == 23:
        return
    yield consume(Witness(24, resurrect_at), (yield 24), fail_at)
    if stop == 24:
        return
    yield consume(Witness(25, resurrect_at), (yield 25), fail_at)
    if stop == 25:
        return
    yield consume(Witness(26, resurrect_at), (yield 26), fail_at)
    if stop == 26:
        return
    yield consume(Witness(27, resurrect_at), (yield 27), fail_at)
    if stop == 27:
        return
    yield consume(Witness(28, resurrect_at), (yield 28), fail_at)
    if stop == 28:
        return
    yield consume(Witness(29, resurrect_at), (yield 29), fail_at)
    if stop == 29:
        return
    yield consume(Witness(30, resurrect_at), (yield 30), fail_at)
    if stop == 30:
        return
    yield consume(Witness(31, resurrect_at), (yield 31), fail_at)
    if stop == 31:
        return
    yield consume(Witness(32, resurrect_at), (yield 32), fail_at)


def small():
    # Single-site control for later before/after scan-cost measurements.
    yield consume(Witness(1, 0), (yield 1), 0)


def reset():
    assert not resurrected
    started.clear()
    completed.clear()
    violations.clear()
    references.clear()
    weak_events.clear()


def finalized(count):
    collect()
    assert sorted(started) == list(range(1, count + 1))
    assert sorted(completed) == list(range(1, count + 1))
    assert violations == []
    assert len(weak_events) == count
    for label in range(1, count + 1):
        assert references[label]() is None
        assert weak_events.count(references[label]) == 1


def exhausted(generator):
    try:
        next(generator)
    except StopIteration:
        pass
    else:
        raise AssertionError("generator did not finish")


def pending(generator, position):
    for label in range(1, position):
        assert next(generator) == label
        assert generator.send(None) == label
    assert next(generator) == position
    collect()
    assert references[position]() is not None
    assert position not in completed


def run_normal():
    generator = grouped(32, 0, 0)
    for label in range(1, 33):
        assert next(generator) == label
        collect()
        assert references[label]() is not None
        assert label not in completed
        assert generator.send(None) == label
        collect()
        assert references[label]() is None
    exhausted(generator)
    del generator
    finalized(32)


def run_early():
    for stop in (0, 16, 17, 31):
        reset()
        generator = grouped(stop, 0, 0)
        for label in range(1, stop + 1):
            assert next(generator) == label
            assert generator.send(None) == label
        exhausted(generator)
        del generator
        finalized(stop)


def run_close():
    for position in (16, 17, 31, 32):
        reset()
        generator = grouped(32, 0, 0)
        pending(generator, position)
        assert generator.close() is None
        exhausted(generator)
        del generator
        finalized(position)


def run_exception():
    for position in (16, 17, 31, 32):
        reset()
        generator = grouped(32, 0, position)
        pending(generator, position)
        try:
            generator.send(None)
        except ValueError as error:
            assert str(error) == "operand failure"
        else:
            raise AssertionError("operand exception lost")
        exhausted(generator)
        del generator
        finalized(position)


def run_weakref():
    generator = grouped(32, 0, 0)
    pending(generator, 32)
    watch = references[32]
    assert watch() is not None
    assert weak_events.count(watch) == 0
    generator.close()
    del generator
    collect()
    assert watch() is None
    assert weak_events.count(watch) == 1
    finalized(32)


def run_resurrection():
    generator = grouped(32, 32, 0)
    pending(generator, 32)
    watch = references[32]
    generator.close()
    del generator
    collect()
    assert started.count(32) == completed.count(32) == 1
    assert len(resurrected) == 1
    assert watch() is resurrected[0]
    assert watch().token == 73
    assert weak_events.count(watch) == 0
    resurrected.clear()
    collect()
    assert watch() is None
    # A resurrected object must not run its finalizer a second time.
    finalized(32)


def run_alias():
    generator = grouped(32, 0, 0)
    pending(generator, 32)
    alias = references[32]()
    collect()
    assert references[32]() is alias
    generator.close()
    del generator
    collect()
    assert references[32]() is alias
    assert alias.token == 73
    assert 32 not in completed
    del alias
    finalized(32)


def take(first, second):
    return first


def fail_later():
    collect()
    raise ValueError("later globals consumer")


def namespace_keys():
    return sorted(list(globals().keys()) + __all__)


def run_globals():
    namespace = globals()
    assert namespace is globals()
    view = namespace.keys()
    del namespace
    collect()
    assert "Witness" in view
    assert "Witness" in namespace_keys()
    globals()
    collect()
    try:
        take(globals(), fail_later())
    except ValueError as error:
        assert str(error) == "later globals consumer"
    else:
        raise AssertionError("globals argument error lost")
    namespace = globals()
    namespace["ownership_canary"] = Witness(1, 0)
    collect()
    assert references[1]() is namespace["ownership_canary"]
    assert "ownership_canary" in view
    del namespace["ownership_canary"]
    del namespace
    finalized(1)
    assert "ownership_canary" not in view


def run_small():
    for iteration in range(32):
        reset()
        generator = small()
        pending(generator, 1)
        assert generator.send(None) == 1
        exhausted(generator)
        del generator
        finalized(1)


def main():
    case = sys.argv[1]
    if case == "globals":
        run_globals()
    elif case == "normal":
        run_normal()
    elif case == "early":
        run_early()
    elif case == "close":
        run_close()
    elif case == "exception":
        run_exception()
    elif case == "weakref":
        run_weakref()
    elif case == "resurrection":
        run_resurrection()
    elif case == "alias":
        run_alias()
    elif case == "small":
        run_small()
    else:
        raise AssertionError("unknown case")
    print("GENERATOR_ROOT_GROUPS_OK:" + case)


main()
