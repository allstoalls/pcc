
import gc
import tempfile

MISSING_MESSAGE = 'No such file or directory'
EXISTS_MESSAGE = 'File exists'

def check_missing(path):
    try:
        with open(path, 'r'):
            raise AssertionError('missing path opened')
    except PermissionError:
        raise AssertionError('sibling handler caught missing file')
    except FileNotFoundError as error:
        assert type(error) is FileNotFoundError
        assert isinstance(error, OSError) and isinstance(error, Exception)
        assert error.errno == 2
        assert error.strerror == MISSING_MESSAGE
        assert error.args == (2, MISSING_MESSAGE)
        assert error.filename == path and type(error.filename) is type(path)
        assert error.filename2 is None
        assert str(error) == '[Errno 2] ' + MISSING_MESSAGE + ': ' + repr(path)
        saved = error
    except OSError:
        raise AssertionError('missing file lost its subclass')
    gc.collect()
    assert saved.filename == path and saved.args[0] == 2

def main():
    with tempfile.TemporaryDirectory() as root:
        missing = root + '/missing'
        check_missing(missing)
        check_missing(missing.encode())
        occupied = root + '/occupied'
        with open(occupied, 'w') as stream:
            stream.write('kept')
        try:
            with open(occupied, 'x'):
                raise AssertionError('exclusive create replaced a file')
        except FileNotFoundError:
            raise AssertionError('sibling FileNotFoundError caught EEXIST')
        except FileExistsError as error:
            assert error.errno == 17 and error.filename == occupied
            assert error.args == (17, EXISTS_MESSAGE)
        with open(occupied, 'r') as stream:
            assert stream.read() == 'kept'
    print('OPEN_ERRNO_OK')

main()
