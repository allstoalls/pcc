"""Additional default-error cleanup gate, separate from blocked decorator gates."""
import gc
import weakref

events = []
watched = []
completed = []
violations = []


class Witness:
    def __init__(self):
        self.token = 97

    def __del__(self):
        if self.token != 97:
            violations.append('token')
        completed.append('default')


def evaluated():
    events.append('default')
    value = Witness()
    watched.append(weakref.ref(value))
    return value


def fail(original):
    events.append('fail')
    raise original


def exercise(original):
    previous = 37
    observed = False
    try:
        def previous(first=evaluated(), *, option=fail(original)):
            return first, option
    except ValueError as caught:
        assert caught is original
        assert str(caught) == 'definition marker'
        observed = True
    assert observed and previous == 37


def main():
    original = ValueError('definition marker')
    exercise(original)
    del original
    gc.collect()
    gc.collect()
    assert events == ['default', 'fail']
    assert len(watched) == 1
    assert watched[0]() is None, 'failed definition default must retire'
    assert completed == ['default']
    assert violations == []
    print('CONSTRUCTOR_DEFAULT_EXCEPTION_OK')


main()
