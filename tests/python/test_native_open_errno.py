"""Missing-file errors traverse owned fopen, payload and handler dispatch.

Host ABI models check the actual provider bodies. Emitted execution is a
separate gate and requires an explicitly selected source-matched runtime.
"""
import errno
import os

import pytest

from tests.python.test_owned_fdopen_provider import FileRuntime, Obj
from tests.python.owned_regression_support import (
    assert_owned_program,
    assert_reference_program,
    explicit_owned_runtime,
)


@pytest.mark.parametrize("number,tag", [(2, 34), (17, 35), (13, 36), (20, 38), (84, 14)])
@pytest.mark.parametrize("bytes_path", [False, True])
def test_open_errno_keeps_original_filename_and_args(number, tag, bytes_path):
    runtime = FileRuntime()
    filename = Obj("bytes", b"/missing/owned") if bytes_path else runtime.string("/missing/owned")
    runtime.errno = errno.EBADF
    runtime.ns["open_file"] = lambda *_: -number
    result = runtime.ns["py_file_open"](filename, runtime.string("r"))
    error = runtime.error
    assert result is None and error.value[0] == tag
    assert error.attrs["errno"] == number
    assert error.attrs["strerror"].value == os.strerror(number)
    assert error.attrs["filename"] is filename
    args = error.attrs["args"].value
    assert args[0] == number and args[1].value == os.strerror(number)
    assert not runtime.frames and not runtime.leases


def test_open_errno_metadata_failure_keeps_allocation_exception():
    runtime = FileRuntime()
    original = Obj("exception", (19, "metadata allocation"))
    runtime.ns["open_file"] = lambda *_: -errno.ENOENT

    def fail_metadata(*_):
        runtime.error = original
        return -1

    runtime.ns["py_obj_setattr"] = fail_metadata
    assert runtime.ns["py_file_open"](runtime.string("missing"), runtime.string("r")) is None
    assert runtime.error is original
    assert not runtime.frames and not runtime.leases


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_fopen_restores_seek_errno_after_close(platform):
    runtime = FileRuntime()
    closed = []
    runtime.ns["open_file"] = lambda *_: 123
    runtime.ns["target_sys_platform"] = lambda: runtime.cstr(platform)

    def seek(*_):
        runtime.errno = errno.ESPIPE
        return -1 if platform == "darwin" else -errno.ESPIPE

    runtime.ns["seek_file"] = seek

    def close(fd):
        closed.append(fd)
        runtime.errno = errno.EBADF
        return -errno.EBADF

    runtime.ns["close"] = close
    assert runtime.ns["fopen"](runtime.cstr("path"), runtime.cstr("a")) is None
    assert closed == [123] and runtime.errno == errno.ESPIPE


def test_fopen_allocation_failure_publishes_enomem_after_close():
    runtime = FileRuntime()
    closed = []
    runtime.ns["open_file"] = lambda *_: 123
    runtime.ns["_stream_new"] = lambda *_: None

    def close(fd):
        closed.append(fd)
        runtime.errno = errno.EBADF
        return -errno.EBADF

    runtime.ns["close"] = close
    assert runtime.ns["fopen"](runtime.cstr("path"), runtime.cstr("r")) is None
    assert closed == [123] and runtime.errno == errno.ENOMEM


PROGRAM = r'''
import gc
import tempfile

MISSING_MESSAGE = MISSING_MESSAGE_VALUE
EXISTS_MESSAGE = EXISTS_MESSAGE_VALUE

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
'''
PROGRAM = PROGRAM.replace("MISSING_MESSAGE_VALUE", repr(os.strerror(2))).replace(
    "EXISTS_MESSAGE_VALUE", repr(os.strerror(17)))


def test_open_errno_reference(tmp_path):
    assert_reference_program(PROGRAM, "OPEN_ERRNO_OK\n", tmp_path)


@pytest.mark.integration
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_open_errno_native_five_gc(python_program_compiler, request,
                                  explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params["python_program_compiler"]
    assert_owned_program(PROGRAM, "OPEN_ERRNO_OK\n", tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime,
                         capfd, provenance_probe="2")
