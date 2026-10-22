"""Independent lifetime gate; original compound lifetime gate is unchanged."""
import gc
import weakref

started = []
completed = []
violations = []


def reenter(label):
    def inner(value=label, *, option=label):
        return value, option
    return inner()


class Witness:
    def __init__(self, label):
        self.label = label
        self.token = 73

    def __del__(self):
        started.append(self.label)
        if self.token != 73:
            violations.append('token')
        value = reenter(self.label)
        if value[0] != self.label or value[1] != self.label:
            violations.append('reentry')
        completed.append(self.label)


def factory():
    owner = Witness('capture')
    reference = weakref.ref(owner)
    def inner():
        return owner
    return inner, reference


def main():
    function, reference = factory()
    gc.collect()
    gc.collect()
    assert reference() is not None
    assert function() is reference()
    assert started == [] and completed == [] and violations == []
    del function
    gc.collect()
    gc.collect()
    assert reference() is None, 'capture owner must retire after callable deletion'
    assert started == ['capture'], 'finalizer must start exactly once'
    assert completed == ['capture'], 'finalizer must finish exactly once'
    assert violations == [], 'finalizer reentry/token invariant'
    print('CONSTRUCTOR_CAPTURE_LIFETIME_OK')


main()
