"""Native regression for all rerouted path predicates and a void error call."""
import gc
import os
import sys


def main(root):
    for index in range(3):
        gc.collect()
        assert os.path.exists(root + '/root.txt') is True
        assert os.path.exists(root + '/missing') is False
        assert os.path.isabs(root + '/a') is True
        assert os.path.isabs('relative/path') is False
        assert os.path.isfile(root + '/root.txt') is True
        assert os.path.isfile(root + '/a') is False
        assert os.path.isdir(root + '/a') is True
        assert os.path.isdir(root + '/root.txt') is False
        assert os.path.islink(root + '/link') is True
        assert os.path.islink(root + '/root.txt') is False
    original = ValueError('iterator failed during extend')
    def values():
        yield 1
        gc.collect()
        raise original
    target = []
    try:
        target.extend(values())
    except ValueError as error:
        assert error is original
    else:
        raise AssertionError('void call swallowed iterator failure')
    assert target == [1]
    print('OWNED_PATH_PREDICATES_VOID_ERROR_OK')


main(sys.argv[1])
