"""Native gate for traversal actually used by metadata, with five-GC pressure."""
import gc
import os
from pathlib import Path
import sys


def files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name != 'skip']
        base = Path(dirpath)
        for name in filenames:
            yield base / name


def main(root):
    assert sorted(str(path.relative_to(Path(root))) for path in files(Path(root))) == ['a/a.txt', 'a/deep/deep.txt', 'b/b.txt', 'root.txt']
    for count in range(3):
        walker = os.walk(root)
        first = next(walker)
        directories = first[1]
        del first
        gc.collect()
        directories[:] = ['b', 'a']
        rows = [path for path, dirs, names in walker]
        assert rows == [root + '/b', root + '/a', root + '/a/deep']
    bottom = [path for path, dirs, names in os.walk(root, topdown=False)]
    assert bottom[-1] == root
    assert bottom.index(root + '/a/deep') < bottom.index(root + '/a')
    assert root + '/link' not in bottom
    followed = [path for path, dirs, names in os.walk(root, followlinks=True)]
    assert root + '/link' in followed and root + '/link/deep' in followed
    errors = []
    def onerror(error):
        gc.collect()
        errors.append((error.errno, error.filename))
    assert list(os.walk(root + '/missing', onerror=onerror)) == []
    assert errors == [(2, root + '/missing')]
    iterator = os.walk(root)
    next(iterator)[1][:] = []
    gc.collect()
    assert list(iterator) == []
    print('OWNED_WALK_TRAVERSAL_LIFETIME_OK')


main(sys.argv[1])
