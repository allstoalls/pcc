import gc
import weakref

references = []
finalized = []

class Token:
    def __del__(self):
        finalized.append('token')

def make():
    token = Token()
    references.append(weakref.ref(token))
    return token

def fail():
    gc.collect()
    raise ValueError('later default')

def construct():
    class Broken:
        fields = make()
        def target(self, value=fields, later=fail()):
            return value
    return Broken

def main():
    try:
        construct()
    except ValueError as error:
        assert str(error) == 'later default'
    else:
        raise AssertionError('missing default error')
    gc.collect()
    assert len(references) == 1
    assert references[0]() is None
    assert finalized == ['token']
    print('CLASS_NAMESPACE_FAILURE_OK')

main()
