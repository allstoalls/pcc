"""Ordinary callable construction: exact definition effects and identity."""
events = []


def default(label, value):
    events.append('default:' + label)
    return value


def decorator(label):
    events.append('evaluate:' + label)
    def apply(function):
        events.append('apply:' + label)
        assert function.__name__ == 'decorated'
        assert function.__doc__ == 'decorated documentation'
        return function
    return apply


@decorator('outer')
@decorator('inner')
def decorated(value=default('positional', 10), *, extra=default('keyword', 20)):
    """decorated documentation"""
    return value + extra


def ordinary(value=7):
    """ordinary documentation"""
    return value


def factory(left, right):
    def inner(value=default('closure', left)):
        """inner documentation"""
        return left, right, value
    return inner


def published():
    return ordinary


def main():
    assert events == ['evaluate:outer', 'evaluate:inner', 'default:positional',
                      'default:keyword', 'apply:inner', 'apply:outer']
    assert decorated() == 30
    assert decorated(2, extra=5) == 7
    assert len(events) == 6
    assert ordinary() == 7 and ordinary(9) == 9
    assert published() is ordinary and published() is published()
    assert ordinary.__module__ == '__main__'
    assert ordinary.__qualname__ == 'ordinary'
    assert ordinary.__name__ == 'ordinary'
    assert ordinary.__doc__ == 'ordinary documentation'
    left = [11]
    right = {'answer': 22}
    first = factory(left, right)
    second = factory(left, right)
    assert first is not second
    assert first.__module__ == '__main__'
    assert first.__qualname__ == 'factory.<locals>.inner'
    assert first.__name__ == 'inner'
    assert first.__doc__ == 'inner documentation'
    values = first()
    assert values[0] is left and values[1] is right and values[2] is left
    replacement = [33]
    values = first(replacement)
    assert values[0] is left and values[1] is right and values[2] is replacement
    left.append(44)
    right['answer'] = 55
    values = second()
    assert values[0] is left and values[1] is right and values[2] is left
    assert values[0] == [11, 44] and values[1]['answer'] == 55
    assert events == ['evaluate:outer', 'evaluate:inner', 'default:positional',
                      'default:keyword', 'apply:inner', 'apply:outer',
                      'default:closure', 'default:closure']
    print('CONSTRUCTOR_SEMANTICS_OK')


main()
