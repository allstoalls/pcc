"""Large-file and incremental decompression contracts for owned codecs."""
from __future__ import annotations

import bz2 as host_bz2
import gzip as host_gzip
import lzma as host_lzma
import os
import subprocess
import sys
import zlib as host_zlib

import pytest

from pcc.stdlib._compression_stream import DecompressReader


class _RecordingSource:
    def __init__(self, payload):
        self._payload = payload
        self._position = 0
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        if self._position >= len(self._payload):
            return b""
        end = self._position + size
        result = self._payload[self._position:end]
        self._position = end
        return result


class _OneChunkDecoder:
    def __init__(self):
        self.eof = False
        self.needs_input = True
        self.unused_data = b""
        self.unconsumed_tail = b""

    def decompress(self, data, max_length=-1):
        assert len(data) <= 64 * 1024
        assert max_length == 64 * 1024
        self.eof = True
        self.needs_input = False
        return data

    def close(self):
        return None


def test_shared_file_reader_requests_and_retains_only_bounded_fragments():
    source = _RecordingSource(b"x" * (3 * 64 * 1024 + 17))
    reader = DecompressReader(source, _OneChunkDecoder)
    chunks = []
    while True:
        chunk = reader.read(32 * 1024)
        if not chunk:
            break
        assert len(reader._buffer) <= 64 * 1024
        chunks.append(chunk)
    assert b"".join(chunks) == b"x" * (3 * 64 * 1024 + 17)
    assert source.read_sizes
    assert max(source.read_sizes) == 64 * 1024


def _write_large_codec_fixtures(root):
    total = 64 * 1024 * 1024 + 257
    block = (b"pcc-large-stream-" * 65536)[:1024 * 1024]
    remaining = total
    checksum = 0

    zlib_encoder = host_zlib.compressobj()
    bz2_encoder = host_bz2.BZ2Compressor()
    lzma_encoder = host_lzma.LZMACompressor()
    zlib_path = root / "payload.zlib"
    gzip_path = root / "payload.gz"
    bz2_path = root / "payload.bz2"
    lzma_path = root / "payload.xz"

    with zlib_path.open("wb") as zlib_out, host_gzip.GzipFile(
        filename=str(gzip_path), mode="wb", mtime=0
    ) as gzip_out, bz2_path.open("wb") as bz2_out, lzma_path.open(
        "wb"
    ) as lzma_out:
        while remaining:
            chunk = block if remaining >= len(block) else block[:remaining]
            remaining -= len(chunk)
            checksum = host_zlib.crc32(chunk, checksum)
            zlib_out.write(zlib_encoder.compress(chunk))
            gzip_out.write(chunk)
            bz2_out.write(bz2_encoder.compress(chunk))
            lzma_out.write(lzma_encoder.compress(chunk))
        zlib_out.write(zlib_encoder.flush())
        bz2_out.write(bz2_encoder.flush())
        lzma_out.write(lzma_encoder.flush())
    return total, checksum & 0xFFFFFFFF, zlib_path, gzip_path, bz2_path, lzma_path


@pytest.mark.integration
def test_large_codec_streams_cross_the_old_64mib_limit_under_self_no_libpython(
    tmp_path,
):
    total, checksum, zlib_path, gzip_path, bz2_path, lzma_path = (
        _write_large_codec_fixtures(tmp_path)
    )
    source = '''\
import bz2
import gzip
import lzma
import zlib

def digest_reader(stream):
    total = 0
    checksum = 0
    while True:
        chunk = stream.read(65536)
        if not chunk:
            break
        total += len(chunk)
        checksum = zlib.crc32(chunk, checksum)
    return total, checksum

def digest_zlib(path):
    decoder = zlib.decompressobj()
    total = 0
    checksum = 0
    with open(path, "rb") as stream:
        while True:
            pending = stream.read(65536)
            if not pending:
                break
            while True:
                chunk = decoder.decompress(pending, 32768)
                pending = decoder.unconsumed_tail
                total += len(chunk)
                checksum = zlib.crc32(chunk, checksum)
                if pending:
                    continue
                if len(chunk) == 32768:
                    pending = b""
                    continue
                break
    tail = decoder.flush()
    total += len(tail)
    checksum = zlib.crc32(tail, checksum)
    return total, checksum

with open(%(zlib_path)r, "rb") as stream:
    one_shot = zlib.decompress(stream.read())
print("zlib-one", len(one_shot), zlib.crc32(one_shot))
print("zlib-stream", digest_zlib(%(zlib_path)r))
with gzip.open(%(gzip_path)r, "rb") as stream:
    print("gzip-stream", digest_reader(stream))
with bz2.open(%(bz2_path)r, "rb") as stream:
    print("bz2-stream", digest_reader(stream))
with lzma.open(%(lzma_path)r, "rb") as stream:
    print("lzma-stream", digest_reader(stream))
''' % {
        "zlib_path": str(zlib_path),
        "gzip_path": str(gzip_path),
        "bz2_path": str(bz2_path),
        "lzma_path": str(lzma_path),
    }
    src = tmp_path / "large_stream_probe.py"
    src.write_text(source, encoding="utf-8")
    executable = tmp_path / "large_stream_probe"
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
    run_env = env.copy()
    run_env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    run_env["PATH"] = str(tmp_path / "no-host-python")
    actual = subprocess.run(
        [str(executable)],
        text=True,
        capture_output=True,
        timeout=180,
        env=run_env,
    )
    assert actual.returncode == 0, actual.stdout + actual.stderr
    expected_line = "(" + str(total) + ", " + str(checksum) + ")"
    assert actual.stdout.splitlines() == [
        "zlib-one " + str(total) + " " + str(checksum),
        "zlib-stream " + expected_line,
        "gzip-stream " + expected_line,
        "bz2-stream " + expected_line,
        "lzma-stream " + expected_line,
    ]
    expected = subprocess.run(
        [sys.executable, str(src)],
        text=True,
        capture_output=True,
        timeout=180,
        env=env,
    )
    assert expected.returncode == 0, expected.stdout + expected.stderr
    assert actual.stdout == expected.stdout
