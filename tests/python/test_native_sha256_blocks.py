"""Native SHA-256 rotation preserves block, padding, and high-bit inputs."""

import os
import subprocess
import sys

from pcc.py_frontend.pipeline import compile_python


def test_sha256_block_boundaries_and_incremental_copy(tmp_path, pcc_py_runtime_archive):
    source = tmp_path / "sha_blocks.py"
    source.write_text('''
import hashlib
import gc
def main():
    for data in [b"", b"a", b"x" * 55, b"x" * 56, b"x" * 63, b"x" * 64, b"x" * 65, b"\\x00\\x7f\\x80\\xff" * 257]:
        print(hashlib.sha256(data).hexdigest())
    first = hashlib.sha256(b"prefix")
    copied = first.copy()
    first.update(b"left")
    copied.update(b"right")
    print(first.hexdigest())
    print(copied.hexdigest())
    for size in [1, 7, 55, 56, 63, 64, 65, 257]:
        data = b"\\x00\\x7f\\x80\\xff" * 257
        h = hashlib.sha256()
        for index in range(0, len(data), size):
            h.update(data[index:index + size])
            gc.collect()
        print(h.hexdigest())
        before = h.digest()
        print(before == h.digest())
        h.update(b"")
        print(before == h.digest())
        saved = h.copy()
        h.update(b"tail")
        print(h.hexdigest())
        print(saved.digest() == before)
    for factory in [hashlib.sha224, hashlib.sha1]:
        h = factory(b"prefix")
        h.update(b"suffix")
        print(h.hexdigest())
main()
''', encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True,
                              text=True, timeout=10)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "sha_blocks"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, f"GC{backend}: {result.stderr}"
        assert result.stdout == expected.stdout, f"GC{backend}: {result.stdout}"


def test_md5_is_md5_including_incremental_updates(tmp_path, pcc_py_runtime_archive):
    source = tmp_path / "md5_vectors.py"
    source.write_text('''
import hashlib
def main():
    for data in [b"", b"a", b"abc", b"x" * 55, b"x" * 56, b"x" * 64, b"\\x80\\xff" * 257]:
        h = hashlib.md5(data)
        print(h.hexdigest())
        h.update(b"suffix")
        print(h.hexdigest())
        saved = h.copy()
        h.update(b"tail")
        print(saved.hexdigest())
        print(h.hexdigest())
main()
''', encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True,
                              text=True, timeout=10)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "md5_vectors"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, f"GC{backend}: {result.stderr}"
        assert result.stdout == expected.stdout, f"GC{backend}: {result.stdout}"
