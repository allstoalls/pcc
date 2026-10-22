import gc
import os
import sys
import warnings
from os import fsync as saved_sync


class Integer(int):
    def __int__(self):
        raise AssertionError('__int__ must not run')

    def __index__(self):
        raise AssertionError('__index__ must not run')

    def __lt__(self, other):
        raise AssertionError('__lt__ must not run')

    def __gt__(self, other):
        raise AssertionError('__gt__ must not run')


class Descriptor:
    def __init__(self, value):
        self.value = value

    def fileno(self):
        gc.collect()
        return self.value


class IndexOnly:
    def __index__(self):
        raise AssertionError('__index__ must not run')


class Raising:
    def __init__(self, error):
        self.error = error

    def fileno(self):
        gc.collect()
        raise self.error


class LookupRaising:
    def __init__(self, error):
        self.error = error

    @property
    def fileno(self):
        gc.collect()
        raise self.error


class Temporary:
    def __init__(self, path):
        self.stream = open(path, 'r+b')

    def fileno(self):
        gc.collect()
        return self.stream.fileno()

    def __del__(self):
        self.stream.close()


def main(path):
    assert saved_sync is os.fsync
    with open(path, 'w+b') as stream:
        stream.write(b'durable')
        stream.flush()
        fd = stream.fileno()
        assert saved_sync(fd) is None
        assert saved_sync(Integer(fd)) is None
        assert os.fsync(fd=fd) is None
        assert saved_sync(stream) is None
        assert saved_sync(Descriptor(fd)) is None
        assert saved_sync(Temporary(path)) is None
        assert stream.tell() == 7
        for value in (-1, -2147483648, Descriptor(-1)):
            try:
                saved_sync(value)
            except ValueError:
                pass
            else:
                raise AssertionError('negative descriptor accepted')
        for value in (2147483648, -2147483649, 2 ** 100, Descriptor(2 ** 100)):
            try:
                saved_sync(value)
            except OverflowError:
                pass
            else:
                raise AssertionError('descriptor overflow accepted')
        for value in (None, '1', 1.0, IndexOnly(), Descriptor(None), Descriptor(IndexOnly())):
            try:
                saved_sync(value)
            except TypeError:
                pass
            else:
                raise AssertionError('invalid descriptor accepted')
        for error in (ValueError('callback'), AttributeError('callback')):
            try:
                saved_sync(Raising(error))
            except Exception as actual:
                assert actual is error
            else:
                raise AssertionError('fileno exception lost')
        error = ValueError('lookup')
        try:
            saved_sync(LookupRaising(error))
        except ValueError as actual:
            assert actual is error
        else:
            raise AssertionError('lookup exception lost')
        try:
            saved_sync(LookupRaising(AttributeError('missing')))
        except TypeError:
            pass
        else:
            raise AssertionError('missing fileno accepted')
        try:
            saved_sync(fd, fd=fd)
        except TypeError:
            pass
        else:
            raise AssertionError('duplicate fd accepted')
        for args in ((), (fd, fd)):
            try:
                saved_sync(*args)
            except TypeError:
                pass
            else:
                raise AssertionError('wrong argument count accepted')
    try:
        saved_sync(fd)
    except OSError as error:
        assert error.errno == 9
        assert error.strerror == 'Bad file descriptor'
        assert error.filename is None
        assert error.args == (9, 'Bad file descriptor')
    else:
        raise AssertionError('closed descriptor accepted')
    with open('/dev/null', 'rb') as stream:
        try:
            saved_sync(stream)
        except OSError as error:
            assert error.errno == 22
        else:
            raise AssertionError('non-synchronizable descriptor accepted')
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter('always')
        try:
            saved_sync(False)
        except OSError:
            pass
    assert len(captured) == 1
    assert captured[0].category is RuntimeWarning
    assert str(captured[0].message) == 'bool is used as a file descriptor'
    with warnings.catch_warnings():
        warnings.simplefilter('error', RuntimeWarning)
        try:
            saved_sync(False)
        except RuntimeWarning:
            pass
        else:
            raise AssertionError('warning filter did not prevent syscall')
    os.unlink(path)
    print('OWNED_FSYNC_PROVIDER_OK')


if __name__ == '__main__':
    main(sys.argv[1])
