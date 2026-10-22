"""bytes/bytearray payload copies run through the runtime's memcpy.

The pcc-Python runtime built bytes and bytearray payloads one byte per loop
iteration: constructors, bytearray.extend/append/insert (each builds a fresh
object and copies the whole payload), bytes.join and bytes repetition. The
owned memcpy copies eight bytes per iteration, so the copies now go through
it. Lengths around the word size exercise both its word loop and its tail,
and repetition doubles a filled prefix, so totals that are not a power of
two exercise its last partial copy.

The native program's output is compared with CPython running the same
source. Runs under ``--backend self --python-libpython=off`` in DEFAULT
runtime mode.
"""
from __future__ import annotations

import os
import subprocess
import sys

_SOURCE = """
def checksum(data):
    total = 0
    i = 0
    for byte in data:
        total = (total * 31 + byte + i) % 1000000007
        i += 1
    return total


def pattern(n, seed):
    out = bytearray()
    i = 0
    while i < n:
        out.append((i * 7 + seed) % 256)
        i += 1
    return bytes(out)


def main():
    sizes = [0, 1, 7, 8, 9, 15, 16, 17, 100, 1000, 4099]
    for n in sizes:
        data = pattern(n, 3)
        print('ctor', n, len(bytes(data)), checksum(bytearray(data)), checksum(data[1:]))
        ba = bytearray(pattern(n % 13, 5))
        ba.extend(data)
        ba.extend(bytearray(pattern(n % 5, 9)))
        print('extend', n, len(ba), checksum(ba))
        ba.append(201)
        print('append', n, len(ba), checksum(ba), ba[-1])
        for at in (0, n // 2, len(ba), -1, -len(ba) - 3):
            grown = bytearray(ba)
            grown.insert(at, 77)
            print('insert', n, at, len(grown), checksum(grown))
        pieces = [pattern(k, k) for k in (n % 9, n, 0, n % 4)]
        print('join', n, checksum(b''.join(pieces)), checksum(b'--'.join(pieces)))
        print('joinba', n, checksum(bytearray(b'|').join(pieces)))
        for count in (0, 1, 2, 3, 5, 7):
            print('repeat', n, count, len(data * count), checksum(data * count))
    print('repeat-byte', len(b'z' * 1023), checksum(b'z' * 1023))
    print(bytearray(b'ab') * 3, b'xy' * 2, bytearray(b'abc') + b'd')


main()
"""


def _run(tmp_path, source):
    src = tmp_path / "p.py"
    src.write_text(source, encoding="utf-8")
    exe = tmp_path / "p_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    built = subprocess.run(
        ["uv", "run", "pcc", "--backend", "self", "--python-libpython=off",
         "--ir-scaffold=on", str(src), "-o", str(exe)],
        text=True, capture_output=True, timeout=420, env=env,
    )
    assert built.returncode == 0, built.stderr
    native = subprocess.run([str(exe)], text=True, capture_output=True, timeout=60, env=env)
    assert native.returncode == 0, native.stderr
    expected = subprocess.run([sys.executable, str(src)], text=True, capture_output=True,
                              timeout=60, env=env)
    assert expected.returncode == 0, expected.stderr
    return native.stdout, expected.stdout


def test_bytes_payload_copies_match_cpython(tmp_path):
    native, expected = _run(tmp_path, _SOURCE)
    assert native.splitlines() == expected.splitlines()
