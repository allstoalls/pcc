
import gc

EVENTS = []

def operand(name, value):
    EVENTS.append(name)
    gc.collect()
    return value

class Unformatted:
    def __str__(self):
        raise AssertionError('constructor formatted an argument')

def direct():
    return OSError(84, 'msg')

def verify(error, cls, args, number, message, filename=None, filename2=None):
    gc.collect()
    assert type(error) is cls
    assert isinstance(error, OSError) and isinstance(error, Exception)
    assert error.args == args
    assert error.errno == number and error.strerror == message
    assert error.filename == filename and error.filename2 == filename2

def main():
    verify(direct(), OSError, (84, 'msg'), 84, 'msg')
    verify(OSError(2, 'missing', 'path'), FileNotFoundError,
           (2, 'missing'), 2, 'missing', 'path')
    verify(FileNotFoundError(13, 'denied'), FileNotFoundError,
           (13, 'denied'), 13, 'denied')
    evaluated = OSError(operand('errno', 84), operand('message', 'msg'),
                        operand('filename', 'path'))
    assert EVENTS == ['errno', 'message', 'filename']
    verify(evaluated, OSError, (84, 'msg'), 84, 'msg', 'path')
    marker = Unformatted()
    untouched = OSError(marker, marker)
    assert untouched.errno is marker and untouched.strerror is marker
    assert untouched.args[0] is marker and untouched.args[1] is marker
    constructor = OSError
    verify(constructor(84, 'msg'), OSError, (84, 'msg'), 84, 'msg')
    for cls in (OSError, IOError, EnvironmentError):
        verify(cls(*(2, 'missing', b'path')), FileNotFoundError,
               (2, 'missing'), 2, 'missing', b'path')
    for args in ((), ('',), (None,), (84,)):
        verify(constructor(*args), OSError, args, None, None)
    verify(constructor(2, 'missing', None), FileNotFoundError,
           (2, 'missing', None), 2, 'missing')
    verify(constructor(2, 'missing', 'first', None, 'second'), FileNotFoundError,
           (2, 'missing'), 2, 'missing', 'first', 'second')
    verify(constructor(2, 'missing', None, None, 'ignored'), FileNotFoundError,
           (2, 'missing', None, None, 'ignored'), 2, 'missing')
    verify(constructor(2 ** 100, 'large'), OSError,
           (2 ** 100, 'large'), 2 ** 100, 'large')
    verify(constructor('2', 'text'), OSError, ('2', 'text'), '2', 'text')
    args = (2, 'missing', 'first', None, 'second', 'extra')
    verify(constructor(*args), OSError, args, None, None)
    try:
        raise OSError(2, 'missing', 'path')
    except PermissionError:
        raise AssertionError('sibling handler matched')
    except FileNotFoundError as error:
        assert error.args == (2, 'missing') and error.filename == 'path'
        assert str(error) == "[Errno 2] missing: 'path'"
        assert repr(error) == "FileNotFoundError(2, 'missing')"
    except OSError:
        raise AssertionError('errno subclass was lost')
    for cls in (OSError, FileNotFoundError):
        try:
            cls(message='invalid')
        except TypeError:
            pass
        else:
            raise AssertionError('constructor accepted keywords')
    try:
        OSError(84, strerror='invalid')
    except TypeError:
        pass
    else:
        raise AssertionError('direct constructor accepted keywords')
    value = constructor(84, 'original', 'path')
    value.args = ('changed',)
    gc.collect()
    assert value.errno == 84 and value.strerror == 'original'
    assert value.filename == 'path' and value.args == ('changed',)
    print('OS_ERROR_CONSTRUCTORS_OK')

main()
