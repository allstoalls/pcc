"""Archive/compression closure for STDLIB-P1-BUILD-TOOL-CLOSURE.

The host-side cases compare the pure archive parsers with CPython.  Native
codec calls are exercised by the compiled no-libpython differential because
their production implementation intentionally crosses pcc.unsafe rather than
calling a host extension module while interpreted.
"""
from __future__ import annotations

import bz2 as host_bz2
import binascii as host_binascii
import gzip as host_gzip
import io
import lzma as host_lzma
import os
import subprocess
import sys
import tarfile as host_tarfile
import zipfile as host_zipfile

import pytest

from pcc.stdlib import binascii as port_binascii
from pcc.stdlib import tarfile as port_tarfile
from pcc.stdlib import zipfile as port_zipfile
from pcc.stdlib import zlib as port_zlib


@pytest.fixture
def archive_samples(tmp_path):
    zip_path = tmp_path / "sample.zip"
    with host_zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("root/stored.txt", b"stored payload")
        archive.writestr(
            "root/deflated.txt",
            b"deflated payload\n" * 8,
            compress_type=host_zipfile.ZIP_DEFLATED,
        )
        archive.writestr(
            "root/bzip2.txt",
            b"bzip2 member payload",
            compress_type=host_zipfile.ZIP_BZIP2,
        )
        archive.writestr("root/empty/", b"")
        archive.comment = b"pcc archive fixture"

    raw_tar = io.BytesIO()
    long_name = "root/" + "nested/" * 15 + "metadata.txt"
    with host_tarfile.open(
        fileobj=raw_tar, mode="w", format=host_tarfile.PAX_FORMAT
    ) as archive:
        directory = host_tarfile.TarInfo("root/")
        directory.type = host_tarfile.DIRTYPE
        directory.mode = 0o755
        directory.mtime = 123456789
        archive.addfile(directory)
        for name, payload in (
            ("root/plain.txt", b"plain tar payload"),
            ("root/trailing-space ", b"name whitespace payload"),
            (long_name, b"long pax payload"),
        ):
            member = host_tarfile.TarInfo(name)
            member.size = len(payload)
            member.mode = 0o644
            member.mtime = 123456789
            archive.addfile(member, io.BytesIO(payload))
    raw_payload = raw_tar.getvalue()
    tar_paths = {}
    for suffix, payload in (
        ("tar", raw_payload),
        ("tar.gz", host_gzip.compress(raw_payload, mtime=0)),
        ("tar.bz2", host_bz2.compress(raw_payload)),
        ("tar.xz", host_lzma.compress(raw_payload)),
    ):
        path = tmp_path / ("sample." + suffix)
        path.write_bytes(payload)
        tar_paths[suffix] = path

    compression_paths = {}
    payload = b"archive codec payload\n" * 32
    midpoint = len(payload) // 2
    for suffix, compressed in (
        (
            "gz",
            host_gzip.compress(payload[:midpoint], mtime=0)
            + host_gzip.compress(payload[midpoint:], mtime=0),
        ),
        (
            "bz2",
            host_bz2.compress(payload[:midpoint])
            + host_bz2.compress(payload[midpoint:]),
        ),
        (
            "xz",
            host_lzma.compress(payload[:midpoint])
            + host_lzma.compress(payload[midpoint:]),
        ),
    ):
        path = tmp_path / ("payload." + suffix)
        path.write_bytes(compressed)
        compression_paths[suffix] = path
    return zip_path, tar_paths, compression_paths, payload, long_name


def test_zipfile_read_surface_matches_cpython(archive_samples):
    zip_path, _tar_paths, _compression_paths, _payload, _long_name = archive_samples
    with port_zipfile.ZipFile(zip_path) as port_archive:
        with host_zipfile.ZipFile(zip_path) as host_archive:
            assert port_archive.namelist() == host_archive.namelist()
            port_infos = port_archive.infolist()
            host_infos = host_archive.infolist()
            assert [info.filename for info in port_infos] == [
                info.filename for info in host_infos
            ]
            assert [info.compress_type for info in port_infos] == [
                info.compress_type for info in host_infos
            ]
            assert [info.file_size for info in port_infos] == [
                info.file_size for info in host_infos
            ]
            assert port_archive.comment == host_archive.comment
            for name in host_archive.namelist():
                assert port_archive.read(name) == host_archive.read(name)
            assert port_archive.testzip() == host_archive.testzip()


@pytest.mark.parametrize("suffix", ["tar", "tar.gz", "tar.bz2", "tar.xz"])
def test_tarfile_read_surface_matches_cpython(archive_samples, suffix):
    _zip_path, tar_paths, _compression_paths, _payload, long_name = archive_samples
    path = tar_paths[suffix]
    with port_tarfile.open(path) as port_archive:
        with host_tarfile.open(path) as host_archive:
            assert port_archive.getnames() == host_archive.getnames()
            assert [member.isfile() for member in port_archive] == [
                member.isfile() for member in host_archive
            ]
            for name in ("root/plain.txt", long_name):
                port_member = port_archive.getmember(name)
                host_member = host_archive.getmember(name)
                assert port_member.size == host_member.size
                assert port_member.mode == host_member.mode
                assert port_archive.extractfile(port_member).read() == (
                    host_archive.extractfile(host_member).read()
                )


def test_archive_safe_content_extraction(archive_samples, tmp_path):
    zip_path, tar_paths, _compression_paths, _payload, _long_name = archive_samples
    zip_target = tmp_path / "zip-out"
    with port_zipfile.ZipFile(zip_path) as archive:
        archive.extractall(zip_target)
    assert (zip_target / "root" / "stored.txt").read_bytes() == b"stored payload"

    tar_target = tmp_path / "tar-out"
    with port_tarfile.open(tar_paths["tar"]) as archive:
        for member in archive.getmembers():
            archive.extract(member, tar_target, set_attrs=False, filter="data")
    assert (tar_target / "root" / "plain.txt").read_bytes() == b"plain tar payload"

    outside = tmp_path / "outside"
    outside.mkdir()
    zip_guard = tmp_path / "zip-guard"
    zip_guard.mkdir()
    os.symlink(outside, zip_guard / "redirect")
    with pytest.raises(port_zipfile.BadZipFile, match="resolves outside"):
        port_zipfile._safe_target(zip_guard, "redirect/escape.txt")

    tar_guard = tmp_path / "tar-guard"
    tar_guard.mkdir()
    os.symlink(outside, tar_guard / "redirect")
    with pytest.raises(port_tarfile.ExtractError, match="resolves outside"):
        port_tarfile._safe_target(tar_guard, "redirect/escape.txt")


def test_zipfile_rejects_local_central_name_disagreement(archive_samples, tmp_path):
    zip_path, _tar_paths, _compression_paths, _payload, _long_name = archive_samples
    damaged = bytearray(zip_path.read_bytes())
    assert damaged[:4] == b"PK\x03\x04"
    damaged[30] = ord("X")
    damaged_path = tmp_path / "name-mismatch.zip"
    damaged_path.write_bytes(damaged)

    with port_zipfile.ZipFile(damaged_path) as archive:
        with pytest.raises(port_zipfile.BadZipFile, match="names disagree"):
            archive.read("root/stored.txt")
    with host_zipfile.ZipFile(damaged_path) as archive:
        with pytest.raises(host_zipfile.BadZipFile):
            archive.read("root/stored.txt")


@pytest.mark.parametrize("seed", [0, -1, 1 << 32, (1 << 80) + 17])
def test_archive_crc32_seed_matches_cpython(seed):
    payload = b"archive crc seed"
    assert port_binascii.crc32(payload, seed) == host_binascii.crc32(
        payload, seed
    )


def test_archive_unsafe_and_unowned_boundaries_fail_closed(tmp_path):
    with pytest.raises(port_zipfile.BadZipFile, match="parent traversal"):
        port_zipfile._safe_target(tmp_path, "../escape")
    unsafe_member = port_tarfile.TarInfo("../escape")
    with pytest.raises(port_tarfile.ExtractError, match="parent traversal"):
        port_tarfile.data_filter(unsafe_member, tmp_path)

    with pytest.raises(NotImplementedError, match="creation"):
        port_zipfile.ZipFile(tmp_path / "new.zip", "w")
    with pytest.raises(NotImplementedError, match="creation"):
        port_tarfile.open(tmp_path / "new.tar", "w")
    with pytest.raises(NotImplementedError, match="decode error modes"):
        port_tarfile.open(
            tmp_path / "missing.tar", errors="surrogateescape"
        )
    with pytest.raises(ValueError, match="bufsize must be positive"):
        port_zlib.decompress(b"", bufsize=0)


@pytest.mark.parametrize(
    "module_name", ["tarfile", "zipfile", "gzip", "bz2", "lzma", "zlib"]
)
def test_archive_ports_are_selected_by_recursive_stdlib_registry(module_name):
    from pcc.frontends.python import pipeline

    source = pipeline._locate_native_stdlib_module_source(module_name)
    assert source is not None
    assert source.endswith("/pcc/stdlib/" + module_name + ".py")
    assert module_name not in pipeline._NATIVE_BUILTIN_IMPORTS


@pytest.mark.integration
def test_archive_family_compiles_and_matches_cpython_no_libpython(
    archive_samples, tmp_path
):
    zip_path, tar_paths, compression_paths, _payload, long_name = archive_samples
    source = '''\
import bz2
import gzip
import lzma
import tarfile
import zipfile
import zlib

def read_bytes(path):
    with open(path, "rb") as stream:
        return stream.read()

print("gzip", gzip.decompress(read_bytes(%(gzip_path)r)).decode())
print("bz2", bz2.decompress(read_bytes(%(bz2_path)r)).decode())
print("lzma", lzma.decompress(read_bytes(%(xz_path)r)).decode())
print("crc32", zlib.crc32(b"archive"), zlib.crc32(b"archive", 1 << 32))
with gzip.open(%(gzip_path)r) as stream:
    print("gzip-readline", stream.readline(7), stream.readline())
with bz2.open(%(bz2_path)r) as stream:
    print("bz2-readline", stream.readline(7), stream.readline())
with lzma.open(%(xz_path)r) as stream:
    print("lzma-readline", stream.readline(7), stream.readline())
with zipfile.ZipFile(%(zip_path)r) as archive:
    print("zip-names", archive.namelist())
    print("zip-comment", archive.comment.decode())
    print("zip-stored", archive.read("root/stored.txt").decode())
    print("zip-deflated", archive.read("root/deflated.txt").decode())
    print("zip-bzip2", archive.read("root/bzip2.txt").decode())
    print("zip-info", [
        (item.filename, item.compress_type, item.file_size, item.is_dir())
        for item in archive.infolist()
    ])
for kind, path in [
    ("tar", %(tar_path)r),
    ("gzip", %(tar_gzip_path)r),
    ("bzip2", %(tar_bz2_path)r),
    ("xz", %(tar_xz_path)r),
]:
    with tarfile.open(path) as archive:
        print("tar-names", kind, archive.getnames())
        member = archive.getmember(%(long_name)r)
        print("tar-member", kind, member.size, member.mode, member.isfile())
        print("tar-payload", kind, archive.extractfile(member).read().decode())
''' % {
        "gzip_path": str(compression_paths["gz"]),
        "bz2_path": str(compression_paths["bz2"]),
        "xz_path": str(compression_paths["xz"]),
        "zip_path": str(zip_path),
        "tar_path": str(tar_paths["tar"]),
        "tar_gzip_path": str(tar_paths["tar.gz"]),
        "tar_bz2_path": str(tar_paths["tar.bz2"]),
        "tar_xz_path": str(tar_paths["tar.xz"]),
        "long_name": long_name,
    }
    src = tmp_path / "archive_probe.py"
    src.write_text(source, encoding="utf-8")
    executable = tmp_path / "archive_probe"
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

    run_env = env.copy()
    run_env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    run_env["PATH"] = str(tmp_path / "no-host-python")
    actual = subprocess.run(
        [str(executable)],
        text=True,
        capture_output=True,
        timeout=120,
        env=run_env,
    )
    assert actual.returncode == 0, actual.stdout + actual.stderr
    expected = subprocess.run(
        [sys.executable, str(src)],
        text=True,
        capture_output=True,
        timeout=120,
        env=env,
    )
    assert expected.returncode == 0, expected.stdout + expected.stderr
    assert actual.stdout == expected.stdout
