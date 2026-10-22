"""Installed runtime bundles remain usable without a host Python process."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_installed_bundle(root: Path, target_id: str) -> Path:
    archive = root / "libpy_runtime_pcc_py.a"
    root.mkdir(parents=True)
    archive.write_bytes(b"!<arch>\nowned-runtime-bytes")
    manifest = Path(str(archive) + ".provenance.json")
    manifest.write_text('{"schema":"pcc.runtime-archive-provenance.v2"}\n')
    inventory = Path(str(archive) + ".capi_syms")
    inventory.write_text("PyRuntime_InstalledAnchor\n", encoding="ascii")
    Path(str(archive) + ".target").write_text(target_id + "\n", encoding="utf-8")
    Path(str(archive) + ".wheel").write_text(
        "pcc.runtime-wheel-artifact.v2\n"
        + "target="
        + target_id
        + "\narchive-sha256="
        + _sha256(archive)
        + "\nmanifest-sha256="
        + _sha256(manifest)
        + "\ncapi-inventory-sha256="
        + _sha256(inventory)
        + "\n",
        encoding="utf-8",
    )
    return archive


def _select_installed_bundle(
    archive: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from pcc.frontends.python import pipeline

    monkeypatch.setenv("PCC_RUNTIME_CC", "pcc")
    monkeypatch.delenv("PCC_RUNTIME_ARCHIVE", raising=False)
    monkeypatch.setattr(pipeline, "_PY_RUNTIME_DIR", str(archive.parent))
    monkeypatch.setattr(pipeline, "_PY_RUNTIME_ARCHIVE_PCC_PY", str(archive))
    return pipeline


def test_installed_runtime_bundle_is_selected_without_host_provenance_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pcc.frontends.python import pipeline

    archive = _write_installed_bundle(
        tmp_path / "site-packages" / "pcc" / "runtime",
        pipeline._runtime_archive_target_id(),
    )
    pipeline = _select_installed_bundle(archive, monkeypatch)

    def forbidden_host_verifier(*_args, **_kwargs):
        raise AssertionError("installed wheel receipt must not launch host Python")

    monkeypatch.setattr(
        pipeline,
        "_runtime_archive_provenance_valid",
        forbidden_host_verifier,
    )
    monkeypatch.setattr(
        pipeline,
        "_runtime_archive_codegen_stale",
        forbidden_host_verifier,
    )

    assert pipeline._ensure_runtime(False, needs_libpython=False) == str(archive)


def test_installed_runtime_receipt_binds_archive_manifest_and_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pcc.frontends.python import pipeline

    for damaged_suffix in ("", ".provenance.json", ".capi_syms"):
        case = tmp_path / ("case-" + (damaged_suffix.lstrip(".") or "archive"))
        archive = _write_installed_bundle(
            case,
            pipeline._runtime_archive_target_id(),
        )
        damaged = Path(str(archive) + damaged_suffix)
        damaged.write_bytes(damaged.read_bytes() + b"tampered")
        selected = _select_installed_bundle(archive, monkeypatch)
        monkeypatch.setattr(
            selected,
            "_runtime_archive_provenance_valid",
            lambda *_args, **_kwargs: False,
        )

        with pytest.raises(
            selected.PyPipelineError,
            match="invalid provenance",
        ):
            selected._ensure_runtime(False, needs_libpython=False)


def test_installed_runtime_inventory_link_read_uses_same_hostless_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pcc.frontends.python import pipeline

    archive = _write_installed_bundle(
        tmp_path / "site-packages" / "pcc" / "runtime",
        pipeline._runtime_archive_target_id(),
    )

    def forbidden_host_verifier(*_args, **_kwargs):
        raise AssertionError("link inventory must reuse the installed receipt")

    monkeypatch.setattr(
        pipeline,
        "_runtime_archive_provenance_valid",
        forbidden_host_verifier,
    )

    assert pipeline._capi_export_anchor_symbols(str(archive)) == [
        "PyRuntime_InstalledAnchor"
    ]


def test_legacy_unbound_wheel_marker_is_not_a_hostless_completion_receipt(
    tmp_path: Path,
) -> None:
    from pcc.frontends.python import pipeline

    archive = tmp_path / "libpy_runtime_pcc_py.a"
    archive.write_bytes(b"archive")
    Path(str(archive) + ".wheel").write_text(
        "pcc.runtime-wheel-artifact.v1\n"
        + pipeline._runtime_archive_target_id()
        + "\nsha256:"
        + ("a" * 64)
        + "\n",
        encoding="utf-8",
    )

    assert not pipeline._runtime_archive_wheel_stamp_matches(str(archive))
