"""The owned pathlib provider answers what CPython's Path answers.

pcc's own package tooling (``pcc1 -m pcc.package build-plan/build-exec``)
runs on this provider and died on ``'Path' object has no attribute
'expanduser'``; it also sorts ``iterdir()`` results, compares and hashes
paths, creates directories, writes bytes and reads ``stat().st_mtime``.
Native output under every collector must match CPython.
"""
from __future__ import annotations

import os
import subprocess
import sys

_SOURCE = """
import os
import sys
from pathlib import Path, PurePath


def main():
    base = Path(sys.argv[1])
    print(Path("a/b") == Path("a//b"), Path("a/./b") == Path("a/b"), Path("a") == Path("b"))
    print(Path("a/b") != Path("a/c"), Path("a") == "a")
    print(len({Path("x/y"), Path("x//y"), Path("x/z")}))
    print([str(p) for p in sorted([Path("b"), Path("a/c"), Path("a"), Path("a-b")])])
    print(Path("a-b") < Path("a/b"), Path("a") <= Path("a"), Path("b") > Path("a/z"))
    print(Path("/x").parts, Path("x/y/z").parts, PurePath("").parts, Path("x/./y/").parts)
    print(Path("/x/y").is_absolute(), Path("x/y").is_absolute())
    print(Path("x//y/./z").as_posix(), Path("x").joinpath("y", "z").as_posix())
    print(Path("/a/b").is_relative_to("/a"), Path("/a/b").is_relative_to("/c"))
    print(Path("~").expanduser() == Path.home(), Path("plain").expanduser().as_posix())
    print(Path.cwd() == Path(os.getcwd()))

    nested = base / "one" / "two"
    try:
        nested.mkdir()
    except FileNotFoundError:
        print("missing parent")
    nested.mkdir(parents=True)
    nested.mkdir(exist_ok=True)
    try:
        nested.mkdir()
    except FileExistsError:
        print("exists")
    data = nested / "data.bin"
    print(data.write_bytes(b"\\x00\\x01payload"))
    with data.open("rb") as handle:
        print(handle.read())
    with (nested / "text.txt").open("w", encoding="utf-8") as handle:
        handle.write("line\\n")
    print((nested / "text.txt").read_text())
    print(sorted([child.name for child in nested.iterdir()]))
    print([child.name for child in sorted(nested.iterdir())])
    info = data.stat()
    print(info.st_size, info.st_mode & 0o170000 == 0o100000,
          nested.stat().st_mode & 0o170000 == 0o040000, info.st_mtime > 0)
    data.chmod(0o600)
    moved = data.rename(nested / "moved.bin")
    print(moved.name, data.exists(), moved.exists())
    swapped = moved.replace(nested / "swapped.bin")
    print(swapped.read_bytes())
    swapped.unlink()
    swapped.unlink(missing_ok=True)
    try:
        swapped.unlink()
    except FileNotFoundError:
        print("unlinked")
    (nested / "text.txt").unlink()
    nested.rmdir()
    print(nested.exists(), nested.parent.exists(), nested.is_symlink())


main()
"""


def test_pathlib_provider_methods_match_cpython(tmp_path):
    source = tmp_path / "paths.py"
    source.write_text(_SOURCE, encoding="utf-8")
    exe = tmp_path / "paths_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    built = subprocess.run(
        ["uv", "run", "pcc", "--backend", "self", "--python-libpython=off",
         "--ir-scaffold=on", str(source), "-o", str(exe)],
        text=True, capture_output=True, timeout=600, env=env,
    )
    assert built.returncode == 0, built.stderr
    work = tmp_path / "cpython"
    work.mkdir()
    expected = subprocess.run([sys.executable, str(source), str(work)], text=True,
                              capture_output=True, timeout=60, env=env, cwd=tmp_path)
    assert expected.returncode == 0, expected.stderr
    for backend in range(5):
        work = tmp_path / ("gc" + str(backend))
        work.mkdir()
        native = subprocess.run([str(exe), str(work)], text=True, capture_output=True,
                                timeout=60, cwd=tmp_path,
                                env=dict(env, PCC_GC_BACKEND=str(backend)))
        assert native.returncode == 0, (backend, native.stderr)
        assert native.stdout == expected.stdout, backend
