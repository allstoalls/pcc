"""Completion is checked outside finalizers; ignored errors cannot pass."""
import gc
import weakref

started = []
completed = []
violations = []
reentered = []


def reenter(label):
    reentered.append(label)
    def local(value=label):
        return value
    return local()


class Witness:
    def __init__(self, label):
        self.label = label
        self.token = 73

    def __del__(self):
        started.append(self.label)
        if self.token != 73:
            violations.append('token:' + self.label)
        if reenter(self.label) != self.label:
            violations.append('reentry:' + self.label)
        completed.append(self.label)


def collect():
    gc.collect()
    gc.collect()


def suspended(capture):
    yield 'ready'
    def inner(value=capture):
        return capture, value
    yield inner


def main():
    capture = Witness('generator')
    capture_ref = weakref.ref(capture)
    generator = suspended(capture)
    assert next(generator) == 'ready'
    del capture
    collect()
    assert capture_ref() is not None
    function = next(generator)
    values = function()
    assert values[0] is capture_ref() and values[1] is capture_ref()
    del values
    assert generator.close() is None
    del generator
    collect()
    assert capture_ref() is not None
    assert completed.count('generator') == 0
    del function
    collect()
    assert capture_ref() is None
    assert started.count('generator') == completed.count('generator') == 1
    assert reentered.count('generator') == 1
    assert len(started) == len(completed) == len(reentered) == 1
    assert violations == []
    print('CONSTRUCTOR_GENERATOR_LIFETIME_OK')


main()
