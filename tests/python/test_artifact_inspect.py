from __future__ import annotations

import json
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from pcc.artifact_inspect import ArtifactInspectionError, inspect_artifact, main


def _string_command(command, name, minimum=24):
    payload = name.encode() + b"\0"
    size = (minimum + len(payload) + 7) // 8 * 8
    header = struct.pack("<III", command, size, minimum)
    return header.ljust(minimum, b"\0") + payload.ljust(size - minimum, b"\0")


def _macho(commands=(), payload=b"", *, filetype=2):
    body = b"".join(commands)
    return (
        struct.pack(
            "<8I", 0xFEEDFACF, 0x0100000C, 0, filetype, len(commands), len(body), 0, 0
        )
        + body
        + payload
    )


def test_inspection_reads_dependencies_not_embedded_strings(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("inspection must not execute any process")

    monkeypatch.setattr(subprocess, "run", forbidden)
    artifact = tmp_path / "app"
    artifact.write_bytes(
        _macho(
            [
                _string_command(0xC, "/usr/lib/libSystem.B.dylib"),
                _string_command(0x80000018, "@rpath/liboptional.dylib"),
                _string_command(0x8000001C, "@executable_path/lib", 12),
                _string_command(0xE, "/usr/lib/dyld", 12),
                _string_command(0xD, "libself.dylib"),  # LC_ID_DYLIB isn't an import.
            ],
            b"no-libpython] libpython3.15.dylib LLVM clang",
        )
    )
    report = inspect_artifact(artifact)
    assert report["declared_dependencies"] == [
        {"name": "/usr/lib/libSystem.B.dylib", "kind": "required"},
        {"name": "@rpath/liboptional.dylib", "kind": "weak"},
    ]
    assert report["rpaths"] == ["@executable_path/lib"]
    assert report["interpreter"] == "/usr/lib/dyld"
    assert report["build_provenance"] == "unknown"
    assert report["inspected_artifact_executed"] is False


@pytest.mark.parametrize(
    "data",
    [
        b"not an artifact",
        b"\xcf\xfa\xed\xfe",
        struct.pack("<8I", 0xFEEDFACF, 0x0100000C, 0, 2, 1, 100, 0, 0),
        _macho([struct.pack("<II", 0xC, 0)]),
        _macho([struct.pack("<III", 0xC, 24, 2).ljust(24, b"\0")]),
        _macho([struct.pack("<III", 0xC, 32, 24).ljust(24, b"\0") + b"no-null!"]),
        _macho([struct.pack("<II", 0x19, 8)]),
    ],
)
def test_rejects_malformed_artifacts(tmp_path, data):
    artifact = tmp_path / "bad"
    artifact.write_bytes(data)
    with pytest.raises(ArtifactInspectionError):
        inspect_artifact(artifact)


def test_python_public_api_and_host_cli(tmp_path, capsys):
    import pcc
    from pcc.cli_core import cli_main

    artifact = tmp_path / "object.o"
    artifact.write_bytes(_macho(filetype=1))
    assert pcc.inspect_artifact(artifact)["kind"] == "object"
    assert cli_main(["inspect", str(artifact), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["schema"] == "pcc.artifact-inspection.v1"
    assert main([str(tmp_path / "absent")]) == 2
    assert "PCC-INSPECT-001" in capsys.readouterr().err


def test_reads_owned_static_elf(tmp_path):
    ident = b"\x7fELF\x02\x01\x01" + b"\0" * 9
    header = struct.pack(
        "<16sHHIQQQIHHHHHH", ident, 2, 62, 1, 0x400080, 64, 0, 0, 64, 56, 1, 64, 0, 0
    )
    segment = struct.pack("<IIQQQQQQ", 1, 5, 0, 0x400000, 0x400000, 120, 120, 4096)
    artifact = tmp_path / "elf"
    artifact.write_bytes(header + segment)
    report = inspect_artifact(artifact)
    assert report["format"] == "elf"
    assert report["architecture"] == "x86_64"
    assert report["declared_dependencies"] == []


def test_public_api_works_without_site_packages(tmp_path):
    artifact = tmp_path / "app"
    artifact.write_bytes(_macho())
    proc = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            "import pcc,sys; print(pcc.inspect_artifact(sys.argv[1])['format'])",
            str(artifact),
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "mach-o"


def test_native_dispatch_does_not_delegate_artifact_to_host(monkeypatch):
    import pcc.cli_bootstrap as cli

    calls = []
    monkeypatch.setattr(
        cli,
        "_run_compiled_python_module_from_pcc1",
        lambda module, args: calls.append((module, args)) or 7,
    )
    monkeypatch.setattr(
        cli, "_run_host_pcc_from_pcc1", lambda args: pytest.fail("host delegation")
    )
    assert cli.bootstrap_cli_main(["inspect", "app.c", "--json"]) == 7
    assert calls == [("pcc.artifact_inspect", ["app.c", "--json"])]
