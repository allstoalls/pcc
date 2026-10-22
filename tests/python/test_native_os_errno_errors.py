"""os.replace, os.unlink and os.rmdir raise the errno subclass, as CPython does.

The native runtime raised a bare ``OSError`` without ``errno`` for every
failure, so ``except FileNotFoundError`` around ``os.unlink`` did not catch a
missing file, and the worker RSS publication could not tell a Windows sharing
violation (EACCES) from a permanent failure.
"""

from __future__ import annotations

import os
import subprocess
import sys


_PROGRAM = '''
import os
import tempfile


def describe(error, first, second=None):
    return (type(error).__name__, error.errno, error.filename == first,
            error.filename2 == second, str(error).startswith("[Errno " + str(error.errno) + "]"))


def main():
    base = tempfile.mkdtemp()
    missing = os.path.join(base, "missing")
    target = os.path.join(base, "target")
    try:
        os.replace(missing, target)
    except FileNotFoundError as error:
        print("replace", describe(error, missing, target))
    try:
        os.unlink(missing)
    except FileNotFoundError as error:
        print("unlink", describe(error, missing))
    try:
        os.rmdir(missing)
    except FileNotFoundError as error:
        print("rmdir", describe(error, missing))
    child = os.path.join(base, "child")
    os.mkdir(child)
    keep = os.path.join(child, "keep")
    with open(keep, "w") as stream:
        stream.write("x")
    try:
        os.rmdir(child)
    except OSError as error:
        print("rmdir-nonempty", type(error).__name__, error.errno is not None, error.filename == child)
    source = os.path.join(base, "source")
    with open(source, "w") as stream:
        stream.write("moved")
    os.replace(source, target)
    with open(target) as stream:
        print("replaced", os.path.exists(source), stream.read())
    try:
        os.unlink(missing)
    except OSError as error:
        print("caught-as-oserror", isinstance(error, FileNotFoundError))
    os.unlink(target)
    os.unlink(keep)
    os.rmdir(child)
    os.rmdir(base)
    print("clean", os.path.exists(base))


main()
'''


def test_native_os_errors_match_cpython(tmp_path, pcc_runtime_archive, python_program_compiler):
    source = tmp_path / "os_errno_errors.py"
    source.write_text(_PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    output = tmp_path / "os_errno_errors"
    python_program_compiler(
        str(source), str(output), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for gc in range(5):
        ran = subprocess.run(
            [str(output)], capture_output=True, text=True, timeout=60,
            env=dict(os.environ, PCC_GC_BACKEND=str(gc)),
        )
        assert ran.returncode == 0, f"GC{gc}: {ran.stdout}{ran.stderr}"
        assert ran.stdout == expected.stdout, f"GC{gc}: {ran.stdout}"
