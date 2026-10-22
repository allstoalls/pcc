import gc
events = []
original = ValueError('original')

class Temporary:
    def __init__(self, label):
        self.label = label
    def __del__(self):
        events.append(self.label)

def fails(*, value):
    gc.collect()
    assert value == [1, 2, 3]
    raise original

def takes(*, value):
    gc.collect()
    return value[0]

def main():
    assert takes(value=[11]) == 11
    try:
        fails(value=[1, 2, 3])
    except ValueError as caught:
        assert caught is original
        assert str(caught) == 'original'
    else:
        raise AssertionError('missing original exception')
    try:
        takes(unexpected=Temporary('argument'))
    except TypeError:
        pass
    else:
        raise AssertionError('missing binding exception')
    gc.collect()
    assert events == ['argument']
    print('slot-status-owned-callsite-ok')

main()
