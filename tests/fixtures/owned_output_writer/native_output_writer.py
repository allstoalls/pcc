import os
from pcc.backend.macho_parallel import OutputRegion, ParallelLinkError, write_mmap_output

def main(path):
    for jobs in (1, 4):
        with open(path, 'w+b') as stream:
            stream.write(b'abcdefghijklmnop')
            write_mmap_output(stream, 8, [OutputRegion(2, b'XY')], jobs=jobs)
            assert stream.tell() == 16 and not stream.closed
            stream.seek(0)
            assert stream.read() == b'abXYefgh'
            write_mmap_output(stream, 14, [OutputRegion(11, b'end')], jobs=jobs)
            stream.seek(0)
            assert stream.read() == b'abXYefgh\0\0\0end'
            try:
                write_mmap_output(stream, 4, [OutputRegion(0, b'abc'), OutputRegion(2, b'xy')], jobs=jobs)
            except ParallelLinkError:
                pass
            else:
                raise AssertionError('overlap accepted')
            stream.seek(0)
            assert stream.read() == b'abXYefgh\0\0\0end'
            data = b'A' * 1048576 + b'B' * 1048576 + b'C' * 1048576 + b'D' * 1048576
            write_mmap_output(stream, len(data), [OutputRegion(0, data)], jobs=jobs)
            stream.seek(0)
            assert stream.read() == data
            write_mmap_output(stream, 0, [], jobs=jobs)
            stream.seek(0)
            assert stream.read() == b''
        os.unlink(path)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        with os.fdopen(descriptor, 'r+b', closefd=False) as stream:
            try:
                write_mmap_output(stream, 8, [OutputRegion(0, b'X')], jobs=4)
            except ParallelLinkError:
                pass
            else:
                raise AssertionError('append accepted')
            stream.seek(0)
            assert stream.read() == b''
    finally:
        os.close(descriptor)
    os.unlink(path)
    print('OWNED_OUTPUT_WRITER_OK')

import sys
main(sys.argv[1])
