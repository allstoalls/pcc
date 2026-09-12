"""Focused facade contracts for runtime archive policy extraction."""

from __future__ import annotations

from pathlib import Path
import json

import pytest

from pcc.py_frontend import pipeline
from pcc.py_frontend import pipeline_runtime_archive as runtime_archive


def test_runtime_archive_path_and_mode_helpers_have_one_owner(monkeypatch):
    archive = "/tmp/libpy_runtime_pcc_py.a"
    assert pipeline._runtime_archive_target_stamp(archive) == runtime_archive.target_stamp(
        archive
    )
    assert (
        pipeline._runtime_archive_provenance_manifest(archive)
        == runtime_archive.provenance_manifest(archive)
    )
    assert pipeline._runtime_archive_capi_inventory(archive) == runtime_archive.capi_inventory(
        archive
    )
    monkeypatch.setenv("PCC_RUNTIME_CC", "host")
    monkeypatch.setenv("PCC_RUNTIME_HIGH", "c")
    assert pipeline._runtime_cc_mode() == "cc"
    assert pipeline._runtime_high_mode() == "c"


def test_non_production_archive_bundle_policy_is_basename_scoped():
    assert runtime_archive.requires_provenance("/tmp/libpy_runtime_pcc_py.a")
    assert not runtime_archive.requires_provenance("/tmp/foreign.a")
    assert runtime_archive.requires_c_bundle_validation("/tmp/libpy_runtime.a")
    assert not runtime_archive.requires_c_bundle_validation("/tmp/foreign.a")


def test_codegen_freshness_checker_runs_the_owned_verifier(
    tmp_path: Path,
    monkeypatch,
):
    archive = tmp_path / "libpy_runtime_pcc_py.a"
    manifest = Path(str(archive) + ".provenance.json")
    manifest.write_text(json.dumps({"source": "test"}), encoding="utf-8")
    calls = []
    from pcc.tools import runtime_archive_provenance as provenance

    def check(records):
        calls.append(records)
        return False

    monkeypatch.setattr(provenance, "manifest_is_stale_for_current_codegen", check)
    monkeypatch.setattr(runtime_archive.subprocess, "run", lambda *a, **k: pytest.fail("host delegation"))

    assert not runtime_archive.provenance_codegen_stale(
        str(archive),
        pcc_source_root=lambda: "/source/root",
        host_python_command=lambda: "/host/python",
    )
    assert calls == [{"source": "test"}]

    monkeypatch.setattr(
        provenance,
        "manifest_is_stale_for_current_codegen",
        lambda *_args, **_kwargs: True,
    )
    assert runtime_archive.provenance_codegen_stale(
        str(archive),
        pcc_source_root=lambda: "/source/root",
        host_python_command=lambda: "/host/python",
    )


def test_c_bundle_inventory_is_checked_without_ar_nm_or_python(tmp_path, monkeypatch):
    from pcc.backend.ar_writer import write_archive
    from pcc.backend.arm64_asm_driver import assemble_file
    from pcc.backend.macho_obj import emit_object

    sections, undefined = assemble_file(
        ".section __TEXT,__text,regular,pure_instructions\n.globl _PyOwned\n_PyOwned:\n ret\n"
    )
    archive = tmp_path / "libpy_runtime.a"
    archive.write_bytes(write_archive([("owned.o", emit_object(sections, undefined=undefined))]))
    inventory = Path(str(archive) + ".capi_syms")
    inventory.write_text("_PyOwned\n")
    def forbidden(*args, **kwargs):
        pytest.fail("archive validation delegated to an external tool")
    monkeypatch.setattr(runtime_archive.subprocess, "run", forbidden)
    assert runtime_archive.c_bundle_valid(str(archive), host_python_command=forbidden)
    inventory.write_text("_PyMissing\n")
    assert not runtime_archive.c_bundle_valid(str(archive), host_python_command=forbidden)
    archive.write_bytes(archive.read_bytes()[:-1])
    assert not runtime_archive.c_bundle_valid(str(archive), host_python_command=forbidden)
