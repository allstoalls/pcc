"""A codec name without a native codec fails as CPython's codec lookup does.

The runtime implements utf-8, ascii and latin-1 and answered every other
name with "LookupError: unknown or unsupported native encoding". CPython
says "unknown encoding: X" for a name that is no codec and "'X' is not a
text encoding; use codecs.decode() to handle arbitrary codecs" (encode()
for an encode, nothing more for open()) for a binary transform such as
base64; a CPython text codec the runtime lacks (cp1252) now raises
NotImplementedError naming it. The names come from CPython's encodings
package through scripts/gen_codec_names.py, whose --check keeps the table
current.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]


PROGRAM = r'''
def decode(data, encoding):
    try:
        return data.decode(encoding)
    except LookupError as caught:
        return type(caught).__name__ + ": " + str(caught)


def construct(data, encoding):
    try:
        return str(data, encoding)
    except LookupError as caught:
        return type(caught).__name__ + ": " + str(caught)


def encode(text, encoding):
    try:
        return text.encode(encoding)
    except LookupError as caught:
        return type(caught).__name__ + ": " + str(caught)


def missing(text, encoding):
    try:
        return text.encode(encoding)
    except NotImplementedError as caught:
        return "missing " + encoding
    except LookupError as caught:
        return "lookup " + encoding


def main():
    print(decode(b"x", "no-such-codec"), decode(b"x", "base64"), decode(b"x", "Hex"))
    print(construct(b"x", "rot13"), construct(b"x", "UTF 8"), construct(b"x", " Latin-1 "))
    print(encode("x", "no such"), encode("x", "zlib"), encode("x", "US-ASCII"))
    print(missing("x", "cp1252") in ("missing cp1252", b"x"), missing("x", "utf_16") in ("missing utf_16", b"\xff\xfex\x00"))


main()
'''


def test_codec_name_table_is_current():
    checked = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "gen_codec_names.py"), "--check"],
        capture_output=True, text=True, timeout=120, cwd=REPO,
    )
    assert checked.returncode == 0, checked.stderr


@pytest.mark.integration
def test_codec_lookup_errors_match_cpython_on_five_collectors(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "codec_lookup.py"
    source.write_text(PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "codec_lookup"
    python_program_compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        environment = dict(os.environ, PCC_GC_BACKEND=str(backend))
        environment.pop("LC_ALL", None)
        ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60, env=environment)
        assert ran.returncode == 0, f"GC{backend}: {ran.stdout}{ran.stderr}"
        assert ran.stdout == expected.stdout, f"GC{backend}: {ran.stdout}"
