import os
import fcntl
import gc
from os import pwrite as put, ftruncate as resize

class Index:
    def __init__(self, value):
        self.value = value
    def __index__(self):
        gc.collect()
        return self.value

def main(path):
    assert put is os.pwrite
    assert resize is os.ftruncate
    with open(path, 'w+b') as stream:
        stream.write(b'abcdefgh')
        stream.flush()
        fd = stream.fileno()
        assert fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDWR
        assert fcntl.fcntl(stream, fcntl.F_GETFL) & os.O_APPEND == 0
        assert put(Index(fd), b'XY', Index(2)) == 2
        for buffer in (bytearray(b'Z'), memoryview(b'Q')):
            try:
                put(fd, buffer, 4)
            except NotImplementedError:
                pass
            else:
                raise AssertionError('unimplemented buffer export silently accepted')
        try:
            put(fd=fd, buffer=b'X', offset=0)
        except TypeError:
            pass
        else:
            raise AssertionError('positional-only API accepted keywords')
        assert stream.tell() == 8
        assert resize(Index(fd), Index(12)) is None
        assert stream.tell() == 8
        stream.seek(0)
        assert stream.read() == b'abXYefgh\0\0\0\0'
        assert resize(fd, 4) is None
        for invalid in ('', 1.5, 2 ** 100):
            try:
                resize(fd, invalid)
            except (TypeError, OverflowError):
                pass
            else:
                raise AssertionError('invalid resize accepted')
        try:
            put(fd, b'x', -1)
        except OSError as error:
            assert error.errno == 22
        else:
            raise AssertionError('negative pwrite offset accepted')
        stream.seek(0)
        assert stream.read() == b'abXY'
    os.unlink(path)
    print('OWNED_POSITIONAL_PROVIDERS_OK')

import sys
main(sys.argv[1])
