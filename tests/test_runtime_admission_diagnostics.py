"""Host-only admission diagnostics preserve the original fail-closed decisions."""

import json
from pathlib import Path

import pytest

from pcc.frontends.python import owned_runtime_build as owned
from pcc.tools import runtime_archive_provenance as provenance


TARGET = "x86_64-pc-windows-msvc"
CURRENT = "a" * 64
PREVIOUS = "b" * 64
CONFIG = {"threads": False, "refcount": "atomic"}
PREFIX = "explicit runtime archive does not match the target/source configuration: "


def _original_codegen_stale(manifest, current):
    if not isinstance(manifest, dict):
        return True
    members = manifest.get("members")
    if not isinstance(members, list) or not members:
        return True
    if not provenance._is_sha256(current):
        return True
    return any(provenance.receipt_is_stale_for_current_codegen(
        record, current_checksum=current,
    ) for record in members)


@pytest.mark.parametrize("manifest,current,reason", [
    (None, CURRENT, "codegen manifest is not an object"),
    ({}, CURRENT, "codegen member inventory is missing or invalid"),
    ({"members": []}, CURRENT, "codegen member inventory is missing or invalid"),
    ({"members": {}}, CURRENT, "codegen member inventory is missing or invalid"),
    ({"members": [None]}, CURRENT, "recorded codegen checksum is missing or invalid"),
    ({"members": [{}]}, CURRENT, "recorded codegen checksum is missing or invalid"),
    ({"members": [{"codegen_checksum": "/private/credential"}]}, CURRENT,
     "recorded codegen checksum is missing or invalid"),
    ({"members": [{"codegen_checksum": CURRENT}]}, "/private/credential",
     "current codegen checksum is unavailable"),
    ({"members": [{"codegen_checksum": CURRENT}]}, "unknown",
     "current codegen checksum is unavailable"),
    ({"members": [{"codegen_checksum": CURRENT}]}, None,
     "current codegen checksum is unavailable"),
    ({"members": [{"codegen_checksum": CURRENT}]}, "a" * 63,
     "current codegen checksum is unavailable"),
    ({"members": [{"codegen_checksum": CURRENT}]}, CURRENT, ""),
    ({"members": [{"codegen_checksum": CURRENT}, {"codegen_checksum": CURRENT}]}, CURRENT, ""),
    ({"members": [{"codegen_checksum": CURRENT}, {"codegen_checksum": PREVIOUS}]}, CURRENT,
     "codegen checksum differs: recorded=" + PREVIOUS + "; current=" + CURRENT),
])
def test_codegen_reason_preserves_boolean_and_hashes_once(monkeypatch, manifest, current, reason):
    calls = []
    monkeypatch.setattr(provenance, "codegen_checksum", lambda: calls.append(True) or current)
    assert provenance.manifest_codegen_staleness_reason(manifest) == reason
    expected_calls = int(isinstance(manifest, dict)
                         and isinstance(manifest.get("members"), list)
                         and bool(manifest["members"]))
    assert len(calls) == expected_calls
    calls.clear()
    assert provenance.manifest_is_stale_for_current_codegen(manifest) is _original_codegen_stale(manifest, current)
    assert len(calls) == expected_calls
    assert "/private/credential" not in reason


def _forbidden(*args, **kwargs):
    pytest.fail("unexpected runtime build or extra admission traversal")


def _original_configuration_matches(receipt, runtime_dir, target, config):
    if not isinstance(receipt, dict) or receipt.get("target_triple") != target:
        return False
    members = receipt.get("members")
    if not isinstance(members, list) or not members:
        return False
    expected = {name + ".o" for name in owned.runtime_modules(runtime_dir, target, config["threads"])}
    if len(members) != len(expected):
        return False
    actual = set()
    for member in members:
        if not isinstance(member, dict) or member.get("runtime_build_config") != config:
            return False
        actual.add(member.get("member"))
    return actual == expected


@pytest.fixture
def admission(tmp_path, monkeypatch):
    for key in ("PCC_RUNTIME_DIR", "PCC_RUNTIME_ARCHIVE", "PCC_WITH_THREADS", "PCC_REFCOUNT_KIND"):
        monkeypatch.delenv(key, raising=False)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "Makefile").write_text("PY_MODULES = probe\n", encoding="utf-8")
    archive = runtime / "explicit.a"
    archive.write_bytes(b"host-only admission fixture; never executed")
    manifest = {"target_triple": TARGET, "members": [
        {"member": name + ".o", "runtime_build_config": dict(CONFIG), "codegen_checksum": CURRENT}
        for name in owned.runtime_modules(str(runtime), TARGET, False)
    ]}
    calls = {"verify": 0, "inventory": 0, "checksum": 0}
    modules = owned.runtime_modules

    def inventory(*args):
        calls["inventory"] += 1
        return modules(*args)

    def verify(path, *, runtime_root):
        calls["verify"] += 1
        assert path == archive and runtime_root == runtime
        return manifest

    def checksum():
        calls["checksum"] += 1
        return CURRENT

    monkeypatch.setattr(owned, "runtime_modules", inventory)
    monkeypatch.setattr(provenance, "verify_runtime_archive_manifest", verify)
    monkeypatch.setattr(provenance, "codegen_checksum", checksum)
    monkeypatch.setattr(owned, "build_runtime_archive", _forbidden)
    monkeypatch.setattr(owned, "require_native_provisioning_allowed", _forbidden)
    return runtime, archive, manifest, calls


@pytest.mark.parametrize("change,reason,inventory", [
    ("target", "target triple differs", 0),
    ("no_members", "member inventory is missing or invalid", 0),
    ("count", "member inventory count differs", 1),
    ("record", "member inventory contains an invalid record", 1),
    ("threads", "runtime build configuration differs", 1),
    ("refcount", "runtime build configuration differs", 1),
    ("missing_config", "runtime build configuration differs", 1),
    ("inventory", "member inventory differs", 1),
    ("recorded", "recorded codegen checksum is missing or invalid", 1),
    ("codegen", "codegen checksum differs: recorded=" + PREVIOUS + "; current=" + CURRENT, 1),
])
def test_explicit_diagnostics_keep_rejection_and_do_not_repeat_work(admission, change, reason, inventory):
    runtime, archive, manifest, calls = admission
    if change == "target":
        manifest["target_triple"] = "aarch64-unknown-linux-gnu"
    elif change == "no_members":
        manifest["members"] = []
    elif change == "count":
        manifest["members"].pop()
    elif change == "record":
        manifest["members"][0] = None
    elif change in ("threads", "refcount"):
        manifest["members"][0]["runtime_build_config"][change] = True if change == "threads" else "local"
    elif change == "missing_config":
        del manifest["members"][0]["runtime_build_config"]
    elif change == "inventory":
        manifest["members"][0]["member"] = "unexpected.o"
    else:
        manifest["members"][-1]["codegen_checksum"] = PREVIOUS if change == "codegen" else "/private/credential"
    original_match = _original_configuration_matches(manifest, str(runtime), TARGET, CONFIG)
    assert owned._manifest_matches_config(manifest, str(runtime), TARGET, CONFIG) is original_match
    calls["inventory"] = 0
    with pytest.raises(ValueError) as error:
        owned.ensure_target_runtime(str(runtime), TARGET, explicit_archive=str(archive))
    assert str(error.value) == PREFIX + reason
    assert calls == {"verify": 1, "inventory": inventory, "checksum": int(change in ("recorded", "codegen"))}
    assert str(runtime) not in str(error.value)
    assert "/private/credential" not in str(error.value)


def test_explicit_matching_archive_keeps_success_and_single_checks(admission):
    runtime, archive, manifest, calls = admission
    assert _original_configuration_matches(manifest, str(runtime), TARGET, CONFIG)
    assert owned._manifest_matches_config(manifest, str(runtime), TARGET, CONFIG)
    calls["inventory"] = 0
    assert owned.ensure_target_runtime(str(runtime), TARGET, explicit_archive=str(archive)) == str(archive)
    assert calls == {"verify": 1, "inventory": 1, "checksum": 1}


def test_explicit_unknown_current_identity_is_not_a_recorded_mismatch(admission, monkeypatch):
    runtime, archive, _manifest, calls = admission

    def unavailable():
        calls["checksum"] += 1
        return "unknown"

    monkeypatch.setattr(provenance, "codegen_checksum", unavailable)
    with pytest.raises(ValueError) as error:
        owned.ensure_target_runtime(str(runtime), TARGET, explicit_archive=str(archive))
    assert str(error.value) == PREFIX + "current codegen checksum is unavailable"
    assert calls == {"verify": 1, "inventory": 1, "checksum": 1}


def test_verified_wheel_still_skips_source_identity(admission):
    runtime, archive, manifest, calls = admission
    # A real wheel callback binds payload bytes; this fixture tests only its
    # existing successful-admission branch, with a deliberately old checksum.
    manifest["members"][0]["codegen_checksum"] = PREVIOUS
    Path(str(archive) + ".provenance.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert owned.ensure_target_runtime(
        str(runtime), TARGET, explicit_archive=str(archive), wheel_matches=lambda _: True,
    ) == str(archive)
    assert calls == {"verify": 0, "inventory": 1, "checksum": 0}


def test_provenance_errors_are_not_replaced_by_admission_diagnostics(admission, monkeypatch):
    runtime, archive, _manifest, calls = admission
    original = provenance.ProvenanceError("runtime source digest does not match")

    def invalid(*args, **kwargs):
        calls["verify"] += 1
        raise original

    monkeypatch.setattr(provenance, "verify_runtime_archive_manifest", invalid)
    with pytest.raises(provenance.ProvenanceError) as error:
        owned.ensure_target_runtime(str(runtime), TARGET, explicit_archive=str(archive))
    assert error.value is original
    assert calls == {"verify": 1, "inventory": 0, "checksum": 0}


@pytest.mark.parametrize("stale", [False, True])
def test_implicit_archive_keeps_reuse_or_rebuild_fence(admission, monkeypatch, stale):
    runtime, archive, manifest, calls = admission
    manifest["members"][0]["codegen_checksum"] = PREVIOUS if stale else CURRENT
    fence_calls = []

    def rebuild_fence():
        fence_calls.append(True)
        raise PermissionError("host-only test rebuild fence")

    monkeypatch.setattr(owned, "require_native_provisioning_allowed", rebuild_fence)
    if stale:
        with pytest.raises(PermissionError, match="host-only test rebuild fence"):
            owned.ensure_target_runtime(str(runtime), TARGET, packaged_archive=str(archive))
        assert fence_calls == [True]
    else:
        assert owned.ensure_target_runtime(str(runtime), TARGET, packaged_archive=str(archive)) == str(archive)
        assert fence_calls == []
    assert calls == {"verify": 1, "inventory": 1, "checksum": 1}
    assert not (runtime / "build_owned").exists()
