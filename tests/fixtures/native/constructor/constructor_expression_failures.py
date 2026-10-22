"""Ordinary definition-expression errors, never synthetic allocator hooks."""
import gc
import weakref

events = []
completed = []
violations = []
watched = []


class Witness:
    def __init__(self, label):
        self.label = label
        self.token = 91

    def __del__(self):
        if self.token != 91:
            violations.append(self.label)
        completed.append(self.label)


def make(label):
    events.append('make:' + label)
    value = Witness(label)
    watched.append(weakref.ref(value))
    return value


def fail_default(error):
    events.append('fail:default')
    raise error


def default_failure(error):
    def never(first=make('default'), second=fail_default(error)):
        return first, second
    events.append('unreachable:default')
    return never


def apply_failure(error):
    def reject(function):
        events.append('apply:reject')
        assert function.__name__ == 'never'
        raise error
    @reject
    def never(value=make('decorator')):
        return value
    events.append('unreachable:decorator')
    return never


def run_default_failure():
    original = ValueError('default marker')
    observed = False
    try:
        default_failure(original)
    except ValueError as caught:
        assert caught is original
        assert str(caught) == 'default marker'
        observed = True
    assert observed


def run_decorator_failure():
    original = ValueError('decorator marker')
    observed = False
    try:
        apply_failure(original)
    except ValueError as caught:
        assert caught is original
        assert str(caught) == 'decorator marker'
        observed = True
    assert observed


def main():
    run_default_failure()
    run_decorator_failure()
    # Exception tracebacks may retain failed definition frames until cyclic
    # collection; do not confuse that valid ownership with a construction leak.
    gc.collect()
    gc.collect()
    assert events == ['make:default', 'fail:default', 'make:decorator', 'apply:reject']
    assert len(watched) == 2
    assert watched[0]() is None and watched[1]() is None
    assert completed.count('default') == completed.count('decorator') == 1
    assert len(completed) == 2
    assert violations == []
    print('CONSTRUCTOR_EXPRESSION_FAILURES_OK')


main()
