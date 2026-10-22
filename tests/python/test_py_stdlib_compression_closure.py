"""Focused source for STDLIB-P1-ZLIB-COMPRESSION-CLOSURE.

Valid compressor calls cross pcc's owned dynamic-library boundary and cannot
run under the host interpreter.  The integration case therefore executes one
source file under CPython and under a self-backed no-libpython binary, comparing
observable level/window/header/check behavior and round trips.  Representative
bytes emitted by the native binary are also decoded by CPython's extension
modules so port-to-port bugs cannot satisfy the test by agreeing with each
other.
"""
from __future__ import annotations

import bz2 as host_bz2
import gzip as host_gzip
import lzma as host_lzma
import os
import random
import subprocess
import sys
import zlib as host_zlib

import pytest

from pcc.stdlib import bz2 as port_bz2
from pcc.stdlib import gzip as port_gzip
from pcc.stdlib import lzma as port_lzma
from pcc.stdlib import zlib as port_zlib


def test_compression_parameter_boundaries_fail_before_native_dispatch():
    for level in (-2, 10):
        with pytest.raises(port_zlib.error, match="Bad compression level"):
            port_zlib.compress(b"payload", level)
    for wbits in (-16, -8, 0, 8, 16, 24, 32):
        with pytest.raises(port_zlib.error, match="Bad compression level"):
            port_zlib.compress(b"payload", 6, wbits)
    with pytest.raises(host_zlib.error, match="Bad compression level"):
        port_gzip.compress(b"payload", 10)
    with pytest.raises(NotImplementedError, match="mtime=None"):
        port_gzip.compress(b"payload", mtime=None)
    for level in (0, 10):
        with pytest.raises(ValueError, match="between 1 and 9"):
            port_bz2.compress(b"payload", level)
    with pytest.raises(NotImplementedError, match="FORMAT_XZ"):
        port_lzma.compress(b"payload", format=port_lzma.FORMAT_RAW)
    with pytest.raises(NotImplementedError, match="filters"):
        port_lzma.compress(b"payload", filters=[{"id": 1}])
    with pytest.raises(port_lzma.LZMAError, match="unsupported options"):
        port_lzma.compress(b"payload", preset=10)
    with pytest.raises(port_lzma.LZMAError, match="integrity check"):
        port_lzma.compress(b"payload", check=port_lzma.CHECK_UNKNOWN)


def test_unsupported_streaming_options_fail_before_native_dispatch():
    with pytest.raises(NotImplementedError, match="preset dictionaries"):
        port_zlib.compressobj(zdict=b"dictionary")
    with pytest.raises(ValueError, match="between 1 and 9"):
        port_bz2.BZ2Compressor(0)
    with pytest.raises(NotImplementedError, match="FORMAT_XZ"):
        port_lzma.LZMACompressor(format=port_lzma.FORMAT_RAW)
    with pytest.raises(NotImplementedError, match="text mode"):
        port_gzip.open("unused.gz", "wt")
    with pytest.raises(NotImplementedError, match="text mode"):
        port_bz2.open("unused.bz2", "wt")
    with pytest.raises(NotImplementedError, match="text mode"):
        port_lzma.open("unused.xz", "wt")


@pytest.mark.integration
def test_compression_family_matches_cpython_self_no_libpython(tmp_path):
    rng = random.Random(8675309)
    payload = rng.randbytes(40000)
    payload_path = tmp_path / "compression-payload.bin"
    payload_path.write_bytes(payload)
    artifact_dir = tmp_path / "compiled-compression-artifacts"
    artifact_dir.mkdir()
    source = '''\
import bz2
import gzip
import lzma
import zlib

with open(%(payload_path)r, "rb") as payload_stream:
    PAYLOAD = payload_stream.read()

def write_artifact(name, data):
    with open(%(artifact_dir)r + "/" + name, "wb") as stream:
        stream.write(data)

for level in [-1, 0, 1, 6, 9]:
    encoded = zlib.compress(PAYLOAD, level, 15)
    print("zlib-level", level, zlib.decompress(encoded, 15) == PAYLOAD)

for wbits in [9, 15, -9, -15, 25, 31]:
    encoded = zlib.compress(PAYLOAD, 6, wbits)
    print("zlib-window", wbits, zlib.decompress(encoded, wbits) == PAYLOAD)

mutable = bytearray(PAYLOAD[:257])
print("zlib-bytearray", zlib.decompress(zlib.compress(mutable)) == bytes(mutable))
print(
    "gzip-bytearray",
    gzip.decompress(gzip.compress(mutable, mtime=0)) == bytes(mutable),
)
print("bz2-bytearray", bz2.decompress(bz2.compress(mutable)) == bytes(mutable))
print(
    "lzma-bytearray",
    lzma.decompress(lzma.compress(mutable)) == bytes(mutable),
)

for level in [0, 1, 6, 9]:
    for mtime in [0, 123456789]:
        encoded = gzip.compress(PAYLOAD, level, mtime=mtime)
        stored_mtime = int.from_bytes(encoded[4:8], "little")
        print(
            "gzip",
            level,
            mtime,
            encoded[:4] == b"\\x1f\\x8b\\x08\\x00",
            stored_mtime,
            encoded[8],
            encoded[9],
            gzip.decompress(encoded) == PAYLOAD,
        )

for level in [1, 5, 9]:
    encoded = bz2.compress(PAYLOAD, level)
    print(
        "bz2",
        level,
        encoded[:3] == b"BZh",
        bz2.decompress(encoded) == PAYLOAD,
    )

for preset in [0, 3, 6, 3 | lzma.PRESET_EXTREME]:
    for check in [lzma.CHECK_NONE, lzma.CHECK_CRC32, lzma.CHECK_CRC64]:
        encoded = lzma.compress(PAYLOAD, check=check, preset=preset)
        print(
            "lzma",
            preset,
            check,
            encoded[:6] == b"\\xfd7zXZ\\x00",
            lzma.decompress(encoded) == PAYLOAD,
        )

print("empty-zlib", zlib.decompress(zlib.compress(b"")) == b"")
print("empty-gzip", gzip.decompress(gzip.compress(b"", mtime=0)) == b"")
print("empty-bz2", bz2.decompress(bz2.compress(b"")) == b"")
print("empty-lzma", lzma.decompress(lzma.compress(b"")) == b"")

# Preserve representative bytes from the native run.  The outer test decodes
# these with CPython's extension modules, so a mutually-compatible bug in a
# port compressor/decompressor pair cannot masquerade as a round-trip proof.
write_artifact("zlib.bin", zlib.compress(PAYLOAD, 6, 15))
write_artifact("deflate.bin", zlib.compress(PAYLOAD, 6, -15))
write_artifact("zlib-gzip.bin", zlib.compress(PAYLOAD, 6, 31))
write_artifact("gzip.bin", gzip.compress(PAYLOAD, 6, mtime=0))
write_artifact("bz2.bin", bz2.compress(PAYLOAD, 5))
write_artifact("lzma.bin", lzma.compress(PAYLOAD, preset=3))
''' % {
        "payload_path": str(payload_path),
        "artifact_dir": str(artifact_dir),
    }
    src = tmp_path / "compression_probe.py"
    src.write_text(source, encoding="utf-8")
    executable = tmp_path / "compression_probe"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env.pop("PCC_RUNTIME_CC", None)

    build = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(src),
            "-o",
            str(executable),
        ],
        text=True,
        capture_output=True,
        timeout=900,
        env=env,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    assert "PCC-PY-COMPILE-001" not in build.stdout + build.stderr

    no_host_env = env.copy()
    no_host_env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    no_host_env["PATH"] = str(tmp_path / "no-host-python")
    actual = subprocess.run(
        [str(executable)],
        text=True,
        capture_output=True,
        timeout=120,
        env=no_host_env,
    )
    assert actual.returncode == 0, actual.stdout + actual.stderr
    compiled_artifacts = {
        path.name: path.read_bytes() for path in artifact_dir.iterdir()
    }
    assert set(compiled_artifacts) == {
        "zlib.bin",
        "deflate.bin",
        "zlib-gzip.bin",
        "gzip.bin",
        "bz2.bin",
        "lzma.bin",
    }
    assert host_zlib.decompress(compiled_artifacts["zlib.bin"], 15) == payload
    assert host_zlib.decompress(compiled_artifacts["deflate.bin"], -15) == payload
    assert host_zlib.decompress(compiled_artifacts["zlib-gzip.bin"], 31) == payload
    assert host_gzip.decompress(compiled_artifacts["gzip.bin"]) == payload
    assert host_bz2.decompress(compiled_artifacts["bz2.bin"]) == payload
    assert host_lzma.decompress(compiled_artifacts["lzma.bin"]) == payload
    expected = subprocess.run(
        [sys.executable, str(src)],
        text=True,
        capture_output=True,
        timeout=120,
        env=env,
    )
    assert expected.returncode == 0, expected.stdout + expected.stderr
    assert actual.stdout == expected.stdout
