"""Additional gate. The original decorator-factory gate is retained separately."""
events = []


def evaluated(label, value):
    events.append(label)
    return value


def keep(function):
    events.append('decorate')
    assert function.__name__ == 'target'
    assert function.__doc__ == 'target documentation'
    return function


@keep
def target(value=evaluated('positional', [7]), *, option=evaluated('keyword', [9])):
    """target documentation"""
    return value, option


def factory(capture):
    def inner(value=capture, *, option=capture):
        """inner documentation"""
        return capture, value, option
    return inner


def main():
    assert events == ['positional', 'keyword', 'decorate']
    first = target()
    second = target()
    assert first[0] is second[0] and first[1] is second[1]
    first[0].append(11)
    assert target()[0] == [7, 11]
    replacement = [13]
    assert target(replacement, option=replacement)[0] is replacement
    assert target(replacement, option=replacement)[1] is replacement
    assert events == ['positional', 'keyword', 'decorate']
    assert target.__name__ == 'target'
    assert target.__module__ == '__main__'
    assert target.__qualname__ == 'target'
    capture = [17]
    left = factory(capture)
    right = factory(capture)
    assert left is not right
    assert left.__name__ == 'inner'
    assert left.__qualname__ == 'factory.<locals>.inner'
    assert left.__doc__ == 'inner documentation'
    triple = left()
    assert triple[0] is capture and triple[1] is capture and triple[2] is capture
    triple = right(replacement, option=replacement)
    assert triple[0] is capture and triple[1] is replacement and triple[2] is replacement
    capture.append(19)
    assert left()[0] == [17, 19]
    raised = False
    try:
        target(replacement, replacement)
    except TypeError:
        raised = True
    assert raised
    print('CONSTRUCTOR_CORE_OK')


main()
