"""Exact bytes construction shares immutable storage with a new owner."""

import os
import subprocess


def test_bytes_constructor_shares_bytes_but_copies_mutable_buffers(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    archive = pcc_runtime_archive
    source = tmp_path / "bytes_identity.py"
    source.write_text('''import gc
def main():
    original = b"hello" * 1024
    alias = bytes(original)
    print(alias is original)
    original = None
    gc.collect()
    assert len(alias) == 5120
    assert alias[:5] == b"hello"
    for index in range(100):
        assert bytes(alias) is alias
    mutable = bytearray(b"a")
    frozen = bytes(mutable)
    mutable[0] = 98
    assert frozen == b"a"
    empty = b""
    assert bytes(empty) is empty
    print("ok")
main()
''')
    binary = tmp_path / "bytes_identity"
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off",
                            runtime_archive=str(archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=15,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout == "True\nok\n", (backend, result.stdout)
