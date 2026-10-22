"""Native open() accepts os.PathLike paths, as CPython's io.open does.

io.open resolves a path that is not str, bytes or an int descriptor through
os.fspath (a type-level ``__fspath__`` lookup) and names the converted path
in OSError.filename. The runtime rejected every such path with "native open
path must be str or bytes", so pcc1's package build-exec failed on its first
``open(pathlib.Path)``. The TypeError texts are CPython's.
"""
from __future__ import annotations

import os
import subprocess


PROGRAM = r'''
import os
import sys
from pathlib import Path


class TextPath:
    def __init__(self, path):
        self.path = path

    def __fspath__(self):
        return self.path


class BytesPath:
    def __init__(self, path):
        self.path = path

    def __fspath__(self):
        return self.path.encode()


class BadPath:
    def __fspath__(self):
        return 3


def main():
    directory = sys.argv[1]
    target = Path(directory) / "data.txt"
    with open(target, "w", encoding="utf-8") as stream:
        stream.write("from-path\n")
    with open(target, encoding="utf-8") as stream:
        print("path", stream.read().strip())
    with open(TextPath(str(target))) as stream:
        print("text-pathlike", stream.read().strip())
    with open(BytesPath(str(target)), "rb") as stream:
        print("bytes-pathlike", stream.read())
    missing = Path(directory) / "missing.txt"
    try:
        open(missing)
    except FileNotFoundError as error:
        print("missing", error.errno, error.filename == str(missing))
    try:
        open(BytesPath(str(missing)))
    except FileNotFoundError as error:
        print("missing-bytes", error.filename == str(missing).encode())
    for value in (3.5, None, BadPath()):
        try:
            open(value)
        except TypeError as error:
            print("open-error", error)
    for value in (3.5, BadPath()):
        try:
            os.fspath(value)
        except TypeError as error:
            print("fspath-error", error)


main()
'''

EXPECTED = """\
path from-path
text-pathlike from-path
bytes-pathlike b'from-path\\n'
missing 2 True
missing-bytes True
open-error expected str, bytes or os.PathLike object, not float
open-error expected str, bytes or os.PathLike object, not NoneType
open-error expected BadPath.__fspath__() to return str or bytes, not int
fspath-error expected str, bytes or os.PathLike object, not float
fspath-error expected BadPath.__fspath__() to return str or bytes, not int
"""


def test_native_open_accepts_pathlike_like_cpython(tmp_path, pcc_runtime_archive):
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / "open_pathlike.py"
    output = tmp_path / "open_pathlike"
    source.write_text(PROGRAM)
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", recursive_stdlib=True,
                   runtime_archive=str(pcc_runtime_archive))
    for gc in range(5):
        work = tmp_path / ("work" + str(gc))
        work.mkdir()
        result = subprocess.run(
            [str(output), str(work)], capture_output=True, text=True, timeout=60,
            env=dict(os.environ, PCC_GC_BACKEND=str(gc), PATH="",
                     PCC_HOST_PYTHON="/nonexistent/host-python"),
        )
        assert result.returncode == 0, f"GC{gc}: " + result.stdout + result.stderr
        assert result.stderr == "", f"GC{gc}: " + result.stderr
        assert result.stdout == EXPECTED, f"GC{gc}: " + result.stdout
