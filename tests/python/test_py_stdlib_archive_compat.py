"""Real-archive compatibility corpus for the owned tar/ZIP readers.

Supported shapes are compared with CPython.  Deliberately unsupported archive
features are represented by real archive records and must fail with a precise
owned-boundary diagnostic instead of being silently misread.
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import tarfile as host_tarfile
import zipfile as host_zipfile

import pytest

from pcc.stdlib import tarfile as port_tarfile
from pcc.stdlib import zipfile as port_zipfile


def _tar_add_bytes(archive, name, payload):
    info = host_tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = 0o640
    info.mtime = 1_700_000_000
    archive.addfile(info, io.BytesIO(payload))


@pytest.fixture
def gnu_tar_corpus(tmp_path):
    path = tmp_path / "gnu-shapes.tar"
    long_name = "tree/" + "component/" * 18 + "payload.txt"
    long_target = "target/" + "segment/" * 18 + "destination.txt"
    with host_tarfile.open(path, "w", format=host_tarfile.GNU_FORMAT) as archive:
        _tar_add_bytes(archive, "tree/target.txt", b"hard-link payload")
        _tar_add_bytes(archive, long_name, b"GNU long-name payload")

        symbolic = host_tarfile.TarInfo("tree/symbolic")
        symbolic.type = host_tarfile.SYMTYPE
        symbolic.linkname = long_target
        archive.addfile(symbolic)

        hard = host_tarfile.TarInfo("tree/hard")
        hard.type = host_tarfile.LNKTYPE
        hard.linkname = "tree/target.txt"
        archive.addfile(hard)

        # GNU's multi-volume continuation type is readable as metadata, but
        # materialising it is intentionally outside the content-only port.
        continuation = host_tarfile.TarInfo("tree/continued")
        continuation.type = b"M"
        archive.addfile(continuation)
    return path, long_name, long_target


def test_gnu_long_names_and_link_metadata_match_cpython(gnu_tar_corpus):
    path, long_name, long_target = gnu_tar_corpus
    with host_tarfile.open(path) as expected, port_tarfile.open(path) as actual:
        assert actual.getnames() == expected.getnames()
        for name in actual.getnames():
            actual_info = actual.getmember(name)
            expected_info = expected.getmember(name)
            assert (
                actual_info.type,
                actual_info.linkname,
                actual_info.size,
                actual_info.mode,
            ) == (
                expected_info.type,
                expected_info.linkname,
                expected_info.size,
                expected_info.mode,
            )
        assert actual.extractfile(long_name).read() == (
            expected.extractfile(long_name).read()
        )
        assert actual.getmember("tree/symbolic").linkname == long_target


def test_tar_links_and_multivolume_materialisation_fail_closed(gnu_tar_corpus):
    path, _long_name, _long_target = gnu_tar_corpus
    with port_tarfile.open(path) as archive:
        for name in ("tree/symbolic", "tree/hard", "tree/continued"):
            with pytest.raises(
                NotImplementedError,
                match="link and special-file reads are not runtime-owned",
            ):
                archive.extractfile(name)


def test_gnu_sparse_pax_metadata_is_explicitly_rejected(tmp_path):
    path = tmp_path / "sparse-pax.tar"
    with host_tarfile.open(path, "w", format=host_tarfile.PAX_FORMAT) as archive:
        member = host_tarfile.TarInfo("sparse.bin")
        member.size = 1
        member.pax_headers = {
            "GNU.sparse.map": "0,1",
            "GNU.sparse.realsize": "1048577",
        }
        archive.addfile(member, io.BytesIO(b"x"))
    with pytest.raises(port_tarfile.ReadError, match="GNU sparse PAX"):
        port_tarfile.open(path)


@pytest.fixture
def zip_feature_corpus(tmp_path):
    regular = tmp_path / "features.zip"
    with host_zipfile.ZipFile(regular, "w") as archive:
        archive.writestr("stored.txt", b"stored", host_zipfile.ZIP_STORED)
        archive.writestr(
            "deflated.txt", b"deflated" * 32, host_zipfile.ZIP_DEFLATED
        )
        archive.writestr("directory/", b"")
        archive.writestr("directory/child.txt", b"child")
        archive.writestr("lzma.txt", b"lzma member", host_zipfile.ZIP_LZMA)
        archive.writestr("../traversal.txt", b"must not escape")
        archive.comment = b"archive compatibility corpus"

    zip64 = tmp_path / "zip64.zip"
    original_limit = host_zipfile.ZIP64_LIMIT
    try:
        host_zipfile.ZIP64_LIMIT = 1
        with host_zipfile.ZipFile(zip64, "w", allowZip64=True) as archive:
            archive.writestr("zip64.txt", b"forced zip64 member")
    finally:
        host_zipfile.ZIP64_LIMIT = original_limit
    return regular, zip64


def test_owned_zip_shapes_match_cpython_and_unsupported_codecs_are_named(
    zip_feature_corpus,
):
    regular, _zip64 = zip_feature_corpus
    with host_zipfile.ZipFile(regular) as expected, port_zipfile.ZipFile(
        regular
    ) as actual:
        assert actual.namelist() == expected.namelist()
        assert actual.comment == expected.comment
        for name in ("stored.txt", "deflated.txt", "directory/child.txt"):
            assert actual.read(name) == expected.read(name)
        assert actual.getinfo("directory/").is_dir()
        with pytest.raises(NotImplementedError, match="compression method"):
            actual.read("lzma.txt")
        with pytest.raises(port_zipfile.BadZipFile, match="parent traversal"):
            actual.extract("../traversal.txt")


def test_real_zip64_member_is_explicitly_rejected(zip_feature_corpus):
    _regular, zip64 = zip_feature_corpus
    with host_zipfile.ZipFile(zip64) as expected:
        assert expected.read("zip64.txt") == b"forced zip64 member"
    with pytest.raises(port_zipfile.LargeZipFile, match="ZIP64"):
        port_zipfile.ZipFile(zip64)


def test_multidisk_zip_header_is_not_silently_treated_as_single_disk(
    zip_feature_corpus, tmp_path
):
    regular, _zip64 = zip_feature_corpus
    payload = bytearray(regular.read_bytes())
    eocd = payload.rfind(b"PK\x05\x06")
    assert eocd >= 0
    payload[eocd + 4:eocd + 6] = (1).to_bytes(2, "little")
    payload[eocd + 6:eocd + 8] = (1).to_bytes(2, "little")
    path = tmp_path / "multidisk.zip"
    path.write_bytes(payload)
    with pytest.raises(port_zipfile.BadZipFile, match="multi-disk"):
        port_zipfile.ZipFile(path)


_ZIP_TOOL = shutil.which("zip")


@pytest.mark.pcc_gate(
    unavailable="system zip tool is required for a real ZipCrypto fixture"
    if _ZIP_TOOL is None
    else None
)
def test_real_encrypted_zip_is_an_explicit_owned_boundary(tmp_path):
    source = tmp_path / "secret.txt"
    source.write_bytes(b"encrypted payload")
    archive_path = tmp_path / "encrypted.zip"
    subprocess.run(
        [_ZIP_TOOL, "-q", "-j", "-P", "pcc-secret", archive_path, source],
        check=True,
        timeout=30,
    )
    with host_zipfile.ZipFile(archive_path) as expected:
        assert expected.read("secret.txt", pwd=b"pcc-secret") == b"encrypted payload"
    with port_zipfile.ZipFile(archive_path) as actual:
        with pytest.raises(NotImplementedError, match="password handling"):
            actual.read("secret.txt", pwd=b"pcc-secret")

