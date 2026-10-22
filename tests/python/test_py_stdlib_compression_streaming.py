"""Focused source for bounded compression and decompression streams.

The dynamic test is deliberately a pcc1 consumer: a host-pcc compile is not
self-host evidence.  Fixtures expand beyond the former 64 MiB ceiling while
remaining small on disk, and the compiled program consumes them in bounded
chunks instead of materializing the complete decoded file.
"""
from __future__ import annotations

import bz2
import gzip
import lzma
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import zlib

import pytest

from pcc.stdlib._compression_stream import CompressionWriter
from tests.python.pcc1_gate import find_current_pcc1


REPO = Path(__file__).resolve().parents[2]
PCC_STDLIB = REPO / "pcc" / "ir" / "support"


def test_compression_streaming_source_has_a_fixed_chunk_policy():
    shared = (PCC_STDLIB / "_compression_stream.py").read_text(
        encoding="utf-8"
    )
    assert "_COMPRESSED_CHUNK = 64 * 1024" in shared
    assert "_DECODED_CHUNK = 64 * 1024" in shared
    assert "self._source.read(_COMPRESSED_CHUNK)" in shared
    assert "backwards seeks replay" in shared
    assert "attempts <" not in shared
    assert "class CompressionWriter:" in shared
    assert "self._write_encoded(self._compressor.compress(data))" in shared
    assert "self._write_encoded(compressor.flush())" in shared

    contracts = {
        "zlib.py": (
            "class Compress:",
            "class Decompress:",
            "_STREAM_OUTPUT_CHUNK = 64 * 1024",
        ),
        "bz2.py": (
            "class BZ2Compressor:",
            "class BZ2Decompressor:",
            "_STREAM_OUTPUT_CHUNK = 64 * 1024",
        ),
        "lzma.py": (
            "class LZMACompressor:",
            "class LZMADecompressor:",
            "_STREAM_OUTPUT_CHUNK = 64 * 1024",
        ),
    }
    for filename, required in contracts.items():
        source = (PCC_STDLIB / filename).read_text(encoding="utf-8")
        for marker in required:
            assert marker in source
        assert "decompressed data exceeds the 64 MiB limit" not in source

    for filename in ("gzip.py", "bz2.py", "lzma.py"):
        source = (PCC_STDLIB / filename).read_text(encoding="utf-8")
        assert "DecompressReader" in source
        assert "read(_MAX_INPUT" not in source
        assert "BytesIO(decompress(" not in source


class _RecordingSink:
    def __init__(self, fail=False):
        self.payload = bytearray()
        self.flushes = 0
        self.closes = 0
        self.fail = fail
        self.name = "recording"

    def write(self, data):
        if self.fail:
            raise OSError("sink failure")
        self.payload.extend(data)
        return len(data)

    def flush(self):
        self.flushes += 1

    def close(self):
        self.closes += 1


class _RecordingCompressor:
    def __init__(self):
        self.flushes = []
        self.closes = 0

    def compress(self, data):
        return b"[" + bytes(data) + b"]"

    def flush(self, mode=None):
        self.flushes.append(mode)
        return b"<final>" if mode is None else b"<sync>"

    def close(self):
        self.closes += 1


def test_shared_compression_writer_releases_once_and_rejects_post_close():
    sink = _RecordingSink()
    compressor = _RecordingCompressor()
    writer = CompressionWriter(sink, compressor)
    assert writer.write(b"") == 0
    assert writer.write(b"abc") == 3
    writer._sync_flush(2)
    assert writer.tell() == 3
    writer.close()
    writer.close()
    assert bytes(sink.payload) == b"[][abc]<sync><final>"
    assert sink.flushes == 2
    assert sink.closes == 0
    assert compressor.flushes == [2, None]
    assert compressor.closes == 1
    assert writer.closed
    with pytest.raises(ValueError, match="closed compressed file"):
        writer.write(b"late")


def test_shared_compression_writer_aborts_codec_on_sink_error():
    sink = _RecordingSink(fail=True)
    compressor = _RecordingCompressor()
    writer = CompressionWriter(sink, compressor)
    with pytest.raises(OSError, match="sink failure"):
        writer.write(b"payload")
    assert writer.closed
    assert compressor.closes == 1
    writer.close()
    assert compressor.closes == 1


def _write_fixtures(tmp_path: Path) -> dict[str, Path]:
    size = 64 * 1024 * 1024 + 256 * 1024 + 37
    pattern = bytes(range(251))
    payload = (pattern * (size // len(pattern) + 1))[:size]
    paths = {
        "zlib": tmp_path / "large.zlib",
        "gzip": tmp_path / "large.gz",
        "bz2": tmp_path / "large.bz2",
        "lzma": tmp_path / "large.xz",
    }
    paths["zlib"].write_bytes(zlib.compress(payload, 1))
    paths["gzip"].write_bytes(gzip.compress(payload, compresslevel=1, mtime=0))
    paths["bz2"].write_bytes(bz2.compress(payload, compresslevel=1))
    paths["lzma"].write_bytes(lzma.compress(payload, preset=0))

    small = (b"incremental decoder payload|" * 173) + bytes(range(97))
    small_path = tmp_path / "small.raw"
    small_path.write_bytes(small)
    paths["small"] = small_path
    paths["small_zlib"] = tmp_path / "small.zlib"
    paths["small_bz2"] = tmp_path / "small.bz2"
    paths["small_lzma"] = tmp_path / "small.xz"
    paths["small_zlib"].write_bytes(zlib.compress(small))
    paths["small_bz2"].write_bytes(bz2.compress(small))
    paths["small_lzma"].write_bytes(lzma.compress(small))
    return paths


def _probe_source(paths: dict[str, Path]) -> str:
    values = {name: str(path) for name, path in paths.items()}
    return textwrap.dedent(
        '''\
        import bz2
        import gzip
        import lzma
        import zlib

        def add_chunk(state, chunk):
            total, checksum = state
            return total + len(chunk), zlib.crc32(chunk, checksum)

        def drain_stream(stream):
            state = (0, 0)
            largest = 0
            while True:
                chunk = stream.read(65537)
                if not chunk:
                    break
                if len(chunk) > largest:
                    largest = len(chunk)
                state = add_chunk(state, chunk)
            return state[0], state[1] & 0xFFFFFFFF, largest

        def drain_zlib_file(path):
            decoder = zlib.decompressobj()
            state = (0, 0)
            largest = 0
            with open(path, "rb") as source:
                while True:
                    compressed = source.read(4093)
                    if not compressed:
                        break
                    pending = compressed
                    while pending:
                        decoded = decoder.decompress(pending, 65537)
                        if len(decoded) > largest:
                            largest = len(decoded)
                        state = add_chunk(state, decoded)
                        pending = decoder.unconsumed_tail
            final = decoder.flush()
            if len(final) > largest:
                largest = len(final)
            state = add_chunk(state, final)
            return state[0], state[1] & 0xFFFFFFFF, largest, decoder.eof

        def drain_buffered_decoder(decoder, encoded):
            chunks = [decoder.decompress(encoded + b"TAIL", 17)]
            turns = 0
            while not decoder.eof:
                if decoder.needs_input:
                    raise RuntimeError("decoder requested input after receiving a complete stream")
                chunks.append(decoder.decompress(b"", 17))
                turns += 1
                if turns > 10000:
                    raise RuntimeError("decoder failed to make bounded progress")
            result = b"".join(chunks)
            return len(result), zlib.crc32(result) & 0xFFFFFFFF, decoder.unused_data

        def drain_zlib_decoder(encoded):
            decoder = zlib.decompressobj()
            chunks = []
            pending = encoded + b"TAIL"
            turns = 0
            while pending:
                chunks.append(decoder.decompress(pending, 17))
                pending = decoder.unconsumed_tail
                turns += 1
                if turns > 10000:
                    raise RuntimeError("zlib decoder failed to consume input")
            while not decoder.eof:
                decoded = decoder.decompress(b"", 17)
                if not decoded:
                    break
                chunks.append(decoded)
            chunks.append(decoder.flush())
            result = b"".join(chunks)
            return len(result), zlib.crc32(result) & 0xFFFFFFFF, decoder.unused_data

        print("zlib-large", drain_zlib_file(%(zlib)r))
        with gzip.open(%(gzip)r, "rb") as stream:
            print("gzip-large", drain_stream(stream))
        with bz2.open(%(bz2)r, "rb") as stream:
            print("bz2-large", drain_stream(stream))
        with lzma.open(%(lzma)r, "rb") as stream:
            print("lzma-large", drain_stream(stream))

        with gzip.open(%(gzip)r, "rb") as stream:
            head = stream.read(23)
            original = stream.tell()
            stream.seek(3)
            replay = stream.read(11)
            stream.seek(original)
            resumed = stream.read(7)
            stream.seek(0, 2)
            print("seek", head[:4], replay[:4], resumed[:4], stream.tell())

        with open(%(small)r, "rb") as source:
            small = source.read()
        with open(%(small_zlib)r, "rb") as source:
            zlib_encoded = source.read()
        with open(%(small_bz2)r, "rb") as source:
            bz2_encoded = source.read()
        with open(%(small_lzma)r, "rb") as source:
            lzma_encoded = source.read()
        expected = (len(small), zlib.crc32(small) & 0xFFFFFFFF, b"TAIL")
        print("zlib-incremental", drain_zlib_decoder(zlib_encoded), expected)
        print(
            "bz2-incremental",
            drain_buffered_decoder(bz2.BZ2Decompressor(), bz2_encoded),
            expected,
        )
        print(
            "bz2-members",
            bz2.decompress(bz2_encoded + bz2_encoded + b"TAIL") == small + small,
        )
        lzma_decoder = lzma.LZMADecompressor()
        print(
            "lzma-incremental",
            drain_buffered_decoder(lzma_decoder, lzma_encoded),
            expected,
            lzma_decoder.check,
        )
        print(
            "lzma-members",
            lzma.decompress(lzma_encoded + lzma_encoded + b"TAIL") == small + small,
        )
        '''
        % values
    )


def _assert_no_libpython(executable: Path) -> None:
    if sys.platform == "darwin":
        command = ["otool", "-L", str(executable)]
    else:
        command = ["readelf", "-d", str(executable)]
    linkage = subprocess.run(
        command,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert linkage.returncode == 0, linkage.stdout + linkage.stderr
    lowered = linkage.stdout.lower()
    assert "libpython" not in lowered
    assert "python3" not in lowered


@pytest.mark.integration
def test_large_streaming_decompression_matches_cpython_under_pcc1_no_libpython(
    tmp_path, native_pcc1_compiler,
):
    paths = _write_fixtures(tmp_path)
    source = tmp_path / "compression_streaming_probe.py"
    source.write_text(_probe_source(paths), encoding="utf-8")
    executable = tmp_path / "compression_streaming_probe"
    # The shared selection (PCC_CURRENT_PCC1 first) every pcc1 test honors.
    pcc1 = native_pcc1_compiler

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            str(pcc1),
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(source),
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
    _assert_no_libpython(executable)

    no_host_env = env.copy()
    no_host_env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    no_host_env["PATH"] = str(tmp_path / "no-host-python")
    actual = subprocess.run(
        [str(executable)],
        text=True,
        capture_output=True,
        timeout=300,
        env=no_host_env,
    )
    assert actual.returncode == 0, actual.stdout + actual.stderr
    expected = subprocess.run(
        [sys.executable, str(source)],
        text=True,
        capture_output=True,
        timeout=300,
        env=env,
    )
    assert expected.returncode == 0, expected.stdout + expected.stderr
    assert actual.stdout == expected.stdout
    decoded_size = 64 * 1024 * 1024 + 256 * 1024 + 37
    for label in ("zlib-large", "gzip-large", "bz2-large", "lzma-large"):
        assert f"{label} ({decoded_size}," in actual.stdout
    assert actual.stdout.count(", 65537") >= 4
    assert "True" in actual.stdout


def _write_host_stream_artifacts(root: Path, payload: bytes) -> dict[str, Path]:
    artifacts = {
        "zlib": root / "host.zlib",
        "gzip": root / "host.gz",
        "bz2": root / "host.bz2",
        "lzma": root / "host.xz",
    }
    encoder = zlib.compressobj(6)
    with artifacts["zlib"].open("wb") as stream:
        stream.write(encoder.compress(payload[:12345]))
        stream.write(encoder.flush(zlib.Z_SYNC_FLUSH))
        stream.write(encoder.compress(payload[12345:]))
        stream.write(encoder.flush())
    with gzip.GzipFile(
        filename=str(artifacts["gzip"]), mode="wb", compresslevel=6, mtime=0
    ) as stream:
        stream.write(payload)
    with bz2.open(artifacts["bz2"], "wb", compresslevel=5) as stream:
        stream.write(payload)
    with lzma.open(artifacts["lzma"], "wb", preset=3) as stream:
        stream.write(payload)
    return artifacts


def _writer_probe_source(
    payload_path: Path,
    output_root: Path,
    host_artifacts: dict[str, Path],
) -> str:
    return textwrap.dedent(
        '''\
        import bz2
        import gzip
        import lzma
        import zlib

        with open(%(payload_path)r, "rb") as source:
            PAYLOAD = source.read()

        def feed(stream, payload):
            offset = 0
            while offset < len(payload):
                end = offset + 8191
                stream.write(payload[offset:end])
                offset = end

        encoder = zlib.compressobj(6)
        with open(%(zlib_output)r, "wb") as output:
            output.write(encoder.compress(PAYLOAD[:12345]))
            output.write(encoder.flush(zlib.Z_SYNC_FLUSH))
            output.write(encoder.compress(PAYLOAD[12345:]))
            output.write(encoder.flush())
        zlib_rejected = False
        try:
            encoder.compress(b"late")
        except Exception:
            zlib_rejected = True

        with gzip.open(%(gzip_output)r, "wb", compresslevel=6) as stream:
            stream.write(b"")
            feed(stream, PAYLOAD)
            stream.flush()
        gzip_closed = stream.closed
        gzip_rejected = False
        try:
            stream.write(b"late")
        except Exception:
            gzip_rejected = True

        with bz2.open(%(bz2_output)r, "wb", compresslevel=5) as stream:
            stream.write(b"")
            feed(stream, PAYLOAD)
            stream.flush()
        bz2_closed = stream.closed

        with lzma.open(%(lzma_output)r, "wb", preset=3) as stream:
            stream.write(b"")
            feed(stream, PAYLOAD)
            stream.flush()
        lzma_closed = stream.closed

        with open(%(host_zlib)r, "rb") as source:
            reverse_zlib = zlib.decompress(source.read()) == PAYLOAD
        with gzip.open(%(host_gzip)r, "rb") as source:
            reverse_gzip = source.read() == PAYLOAD
        with bz2.open(%(host_bz2)r, "rb") as source:
            reverse_bz2 = source.read() == PAYLOAD
        with lzma.open(%(host_lzma)r, "rb") as source:
            reverse_lzma = source.read() == PAYLOAD

        print(
            "stream-writers",
            zlib_rejected,
            gzip_closed,
            gzip_rejected,
            bz2_closed,
            lzma_closed,
            reverse_zlib,
            reverse_gzip,
            reverse_bz2,
            reverse_lzma,
        )
        '''
        % {
            "payload_path": str(payload_path),
            "zlib_output": str(output_root / "pcc.zlib"),
            "gzip_output": str(output_root / "pcc.gz"),
            "bz2_output": str(output_root / "pcc.bz2"),
            "lzma_output": str(output_root / "pcc.xz"),
            "host_zlib": str(host_artifacts["zlib"]),
            "host_gzip": str(host_artifacts["gzip"]),
            "host_bz2": str(host_artifacts["bz2"]),
            "host_lzma": str(host_artifacts["lzma"]),
        }
    )


@pytest.mark.integration
def test_stream_writers_match_cpython_under_current_pcc1(
    tmp_path: Path,
    pcc_runtime_archive: Path,
):
    payload = (bytes(range(251)) * 4097) + b"pcc-stream-tail"
    payload_path = tmp_path / "payload.bin"
    payload_path.write_bytes(payload)
    host_artifacts = _write_host_stream_artifacts(tmp_path, payload)
    output_root = tmp_path / "output"
    output_root.mkdir()
    source = tmp_path / "stream_writer_probe.py"
    source.write_text(
        _writer_probe_source(payload_path, output_root, host_artifacts),
        encoding="utf-8",
    )
    executable = tmp_path / "stream_writer_probe"
    pcc1 = find_current_pcc1(REPO)
    assert pcc1 is not None, "receipt-current pcc1 is required"

    environment = os.environ.copy()
    for name in ("LC_ALL", "PYTHONPATH", "PCC_PACKAGE_SITE"):
        environment.pop(name, None)
    environment["PCC_RUNTIME_ARCHIVE"] = str(pcc_runtime_archive)
    environment["PCC_HOST_PYTHON"] = "/usr/bin/false"
    environment["PCC_HOST_PCC"] = "/usr/bin/false"
    build = subprocess.run(
        [
            str(pcc1),
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(source),
            "-o",
            str(executable),
        ],
        text=True,
        capture_output=True,
        timeout=900,
        env=environment,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    _assert_no_libpython(executable)

    deny_path = tmp_path / "no-host-python"
    deny_path.mkdir()
    run_environment = environment.copy()
    run_environment["PATH"] = str(deny_path)
    expected_stdout = (
        "stream-writers True True True True True True True True True\n"
    )
    for backend in range(5):
        run_environment["PCC_GC_BACKEND"] = str(backend)
        result = subprocess.run(
            [str(executable)],
            text=True,
            capture_output=True,
            timeout=180,
            env=run_environment,
        )
        assert result.returncode == 0, (
            f"PCC_GC_BACKEND={backend}\n" + result.stdout + result.stderr
        )
        assert result.stdout == expected_stdout
        assert zlib.decompress((output_root / "pcc.zlib").read_bytes()) == payload
        assert gzip.decompress((output_root / "pcc.gz").read_bytes()) == payload
        assert bz2.decompress((output_root / "pcc.bz2").read_bytes()) == payload
        assert lzma.decompress((output_root / "pcc.xz").read_bytes()) == payload
