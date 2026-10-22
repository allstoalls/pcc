"""Native hashlib.sha256 equals CPython for every block-boundary shape.

The owned update compresses whole blocks straight from the input and stages
only partial blocks. Every length 0..139 crosses the staged/direct and
padding boundaries; chunked updates of 1..67 bytes start blocks at every
offset; larger inputs run many direct blocks.
"""
import os
from pathlib import Path
import subprocess
import sys

PROGRAM = '''import hashlib


def data(n):
    out = bytearray(n)
    i = 0
    while i < n:
        out[i] = (i * 131 + 7) & 255
        i += 1
    return bytes(out)


def main():
    lines = []
    for n in list(range(0, 140)) + [1000, 4096, 65536, 100003]:
        d = data(n)
        lines.append(str(n) + " " + hashlib.sha256(d).hexdigest())
        h = hashlib.sha256()
        step = 1 + n % 67
        i = 0
        while i < n:
            h.update(d[i:i + step])
            i += step
        lines.append(str(n) + "/" + str(step) + " " + h.hexdigest())
    print("\\n".join(lines))


main()
'''


def test_native_sha256_matches_cpython_on_five_collectors(tmp_path: Path, pcc_runtime_archive):
    from pcc.frontends.python.pipeline import compile_python
    source = tmp_path / "sha.py"
    source.write_text(PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=120)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "sha"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
                             capture_output=True, text=True, timeout=120)
        assert ran.returncode == 0, f"GC{backend}: " + ran.stdout + ran.stderr[-2000:]
        assert ran.stdout == expected.stdout, f"GC{backend}"
