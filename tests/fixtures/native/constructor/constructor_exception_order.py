"""Exact definition effect order, original exception identity and prior binding."""
events = []


def evaluated(label):
    events.append(label)
    return [label]


def fail(error):
    events.append('fail')
    raise error


def default_failure(original):
    previous = 37
    caught_it = False
    try:
        def previous(first=evaluated('positional'), *, option=fail(original)):
            return first, option
    except ValueError as caught:
        assert caught is original
        assert str(caught) == 'definition marker'
        caught_it = True
    assert caught_it
    assert previous == 37


def decorator_failure(original):
    previous = 41
    def reject(function):
        events.append('reject')
        assert function.__name__ == 'previous'
        assert function.__doc__ == 'before decorator failure'
        raise original
    caught_it = False
    try:
        @reject
        def previous(value=evaluated('decorator-default')):
            """before decorator failure"""
            return value
    except ValueError as caught:
        assert caught is original
        assert str(caught) == 'decoration marker'
        caught_it = True
    assert caught_it
    assert previous == 41


def main():
    default_failure(ValueError('definition marker'))
    decorator_failure(ValueError('decoration marker'))
    assert events == ['positional', 'fail', 'decorator-default', 'reject']
    print('CONSTRUCTOR_EXCEPTION_ORDER_OK')


main()
