"""Production runtime construction rejects unavailable compiler identities early."""
from __future__ import annotations

import pytest


@pytest.mark.parametrize("identity", ["unknown", "", "a" * 63, "g" * 64, None])
def test_runtime_build_rejects_unavailable_identity_before_output(tmp_path, monkeypatch, identity):
    from pcc.frontends.python import owned_runtime_build as owned
    from pcc.frontends.python import pipeline_runtime_archive as locks
    from pcc.tools import runtime_archive_provenance as provenance

    def forbidden(*args, **kwargs):
        pytest.fail("runtime construction began before compiler identity validation")

    monkeypatch.setattr(provenance, "codegen_checksum", lambda: identity)
    monkeypatch.setattr(owned, "runtime_ir_passes", forbidden)
    monkeypatch.setattr(owned, "_compile_runtime_module", forbidden)
    monkeypatch.setattr(locks, "_acquire_runtime_build_lock", forbidden)
    runtime = tmp_path / "runtime"
    archive = tmp_path / "output" / "libpy_runtime_pcc_py.a"
    with pytest.raises(ValueError, match="compiler identity is unavailable"):
        owned.build_runtime_archive(str(runtime), str(archive), "x86_64-unknown-linux-gnu")
    assert not runtime.exists()
    assert not archive.parent.exists()
