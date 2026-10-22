"""Native ordinary-islink ownership and immediate error propagation gate."""
import gc
import os
import sys


def main(root):
    seen = []
    original = KeyError('original fspath failure')
    class RaisingPath:
        def __fspath__(self):
            gc.collect()
            seen.append('fspath')
            raise original
    for index in range(3):
        try:
            result = os.path.islink(RaisingPath())
        except KeyError as error:
            assert error is original
        else:
            raise AssertionError('ordinary islink swallowed fspath failure')
    assert seen == ['fspath', 'fspath', 'fspath']
    class LinkPath:
        def __fspath__(self):
            gc.collect()
            return root + '/link'
    assert os.path.islink(LinkPath()) is True
    assert os.path.islink((root + '/link').encode()) is True
    assert os.path.islink(root + '/a') is False
    print('OWNED_ISLINK_PATHLIKE_ERROR_OK')


main(sys.argv[1])
