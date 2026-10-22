from __future__ import annotations
import sys

import hashlib
from pathlib import Path

import pytest

from pcc.frontends.python import pipeline_self_backend_cache as cache
from pcc.backend import self_backend_cache_identity as identity_module

SETTINGS = (
    "PCC_SELF_TARGET_PASSES", "PCC_SELF_TARGET_PASS_TRANSPORT",
    "PCC_SELF_CALL_RESULT_REGISTERS", "PCC_SELF_CALLEE_SAVED_REGISTERS",
    "PCC_SELF_FUNCTION_LIVE_INTERVALS", "PCC_SELF_BRANCH_PROTECTION",
    "PCC_CODE_PROFILE",
)


@pytest.fixture
def cache_case(monkeypatch, tmp_path):
    for name in SETTINGS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(cache.OBJECT_CACHE_ENV, "1")
    monkeypatch.setenv(cache.OBJECT_CACHE_IDENTITY_ENV, "caller-compiler-abi")
    monkeypatch.setenv(cache.OBJECT_CACHE_DIR_ENV, str(tmp_path / "cache"))
    monkeypatch.setattr(identity_module, "self_backend_emitter_source_identity", lambda: "backend-source")
    ir = tmp_path / "input.ll"
    ir.write_bytes(b'define i64 @answer() {\nentry:\n ret i64 42\n}\n')
    items = [(str(tmp_path / "result"), str(tmp_path / "object.pco"), str(ir))]
    def plan(target="self-aarch64-darwin-v0", mode="pcc-native-pco-v1"):
        return cache.plan(items, target, mode, str(tmp_path), host_python_command=None,
                          plan_host_code="", small_int_decimal=None)
    return items, plan


@pytest.mark.parametrize("name,value", [
    ("PCC_SELF_TARGET_PASSES", "strip-trailing-whitespace"),
    ("PCC_SELF_TARGET_PASS_TRANSPORT", "memory"),
    ("PCC_SELF_CALL_RESULT_REGISTERS", "1"),
    ("PCC_SELF_CALLEE_SAVED_REGISTERS", "0"),
    ("PCC_SELF_FUNCTION_LIVE_INTERVALS", "1"),
    ("PCC_SELF_BRANCH_PROTECTION", "0"),
])
def test_each_effective_setting_separates_exact_ir(cache_case, monkeypatch, name, value):
    _, plan = cache_case
    baseline = plan()
    monkeypatch.setenv(name, value)
    assert plan()[0][0] != baseline[0][0]


@pytest.mark.parametrize("name,value", [
    ("PCC_SELF_TARGET_PASSES", "default"),
    ("PCC_SELF_TARGET_PASSES", "OFF"),
    ("PCC_SELF_TARGET_PASS_TRANSPORT", " TEXT "),
    ("PCC_SELF_CALL_RESULT_REGISTERS", "off"),
    ("PCC_SELF_CALLEE_SAVED_REGISTERS", "yes"),
    ("PCC_SELF_FUNCTION_LIVE_INTERVALS", "0"),
    ("PCC_SELF_BRANCH_PROTECTION", "on"),
])
def test_equivalent_default_settings_share_key(cache_case, monkeypatch, name, value):
    _, plan = cache_case
    baseline = plan()
    monkeypatch.setenv(name, value)
    assert plan() == baseline


def test_pass_order_and_all_expansion(cache_case, monkeypatch):
    _, plan = cache_case
    monkeypatch.setenv("PCC_SELF_TARGET_PASSES", "all")
    expanded = plan()
    monkeypatch.setenv("PCC_SELF_TARGET_PASSES", "strip-trailing-whitespace,aarch64-fold-frame-address,aarch64-fold-immediate")
    assert plan() == expanded
    monkeypatch.setenv("PCC_SELF_TARGET_PASSES", "aarch64-fold-immediate,aarch64-fold-frame-address,strip-trailing-whitespace")
    assert plan() != expanded


def test_old_schema_and_existing_key_fields_cannot_alias(cache_case, monkeypatch):
    items, plan = cache_case
    baseline = plan()[0][0]
    assert cache.OBJECT_CACHE_VERSION == "pcc.self-backend-object-cache.v4"
    old = hashlib.sha256()
    for field in ("pcc.self-backend-object-cache.v3", "caller-compiler-abi", "backend-source", "self-aarch64-darwin-v0", "pcc-native-pco-v1"):
        old.update(field.encode() + b"\0")
    old.update(Path(items[0][2]).read_bytes())
    assert Path(baseline).stem != old.hexdigest()
    assert plan(target="self-x86_64-linux-v0")[0][0] != baseline
    assert plan(mode="other-artifact-mode")[0][0] != baseline
    monkeypatch.setenv(cache.OBJECT_CACHE_IDENTITY_ENV, "other-compiler-abi")
    assert plan()[0][0] != baseline
    monkeypatch.setenv(cache.OBJECT_CACHE_IDENTITY_ENV, "caller-compiler-abi")
    monkeypatch.setattr(identity_module, "self_backend_emitter_source_identity", lambda: "other-source")
    assert plan()[0][0] != baseline
    monkeypatch.setattr(identity_module, "self_backend_emitter_source_identity", lambda: "backend-source")
    Path(items[0][2]).write_bytes(Path(items[0][2]).read_bytes() + b"\n")
    assert plan()[0][0] != baseline


def test_profile_bypasses_without_reading_external_input(cache_case, monkeypatch):
    items, plan = cache_case
    monkeypatch.setenv("PCC_CODE_PROFILE", "/does/not/exist/profile.json")
    Path(items[0][2]).unlink()
    assert plan() == [("", "off")]
    assert cache.publish(items, [("", "off")], "", host_python_command=None, publish_host_code="")


def test_unrelated_secret_does_not_change_key(cache_case, monkeypatch):
    _, plan = cache_case
    baseline = plan()
    monkeypatch.setenv("UNRELATED_API_TOKEN", "synthetic-test-only")
    assert plan() == baseline


def test_hit_checksum_validation_and_configuration_miss(cache_case, monkeypatch):
    items, plan = cache_case
    first = plan()
    obj = Path(items[0][1])
    obj.write_bytes(b"owned-object-test-bytes")
    assert cache.publish(items, first, "", host_python_command=None, publish_host_code="")
    obj.unlink()
    assert plan()[0][1] == "hit"
    assert obj.read_bytes() == b"owned-object-test-bytes"
    monkeypatch.setenv("PCC_SELF_TARGET_PASSES", "all")
    assert plan()[0][1] == "miss"
    monkeypatch.delenv("PCC_SELF_TARGET_PASSES")
    Path(first[0][0]).write_bytes(b"corrupt")
    assert plan()[0][1] == "miss"
    assert not obj.exists()


def test_tiny_owned_object_bytes_change_with_branch_protection(cache_case, monkeypatch):
    from pcc.backend.owned_object_emit import emit_owned_object
    items, plan = cache_case
    target = "aarch64-apple-darwin"
    ir = 'target triple = "' + target + '"\ndefine i64 @answer() {\nentry:\n ret i64 42\n}\n'
    Path(items[0][2]).write_text(ir)
    protected_key = plan()[0][0]
    protected = emit_owned_object(ir, target)
    monkeypatch.setenv("PCC_SELF_BRANCH_PROTECTION", "0")
    unprotected_key = plan()[0][0]
    unprotected = emit_owned_object(ir, target)
    assert protected and unprotected
    assert protected != unprotected
    assert protected_key != unprotected_key


def test_invalid_pass_setting_cannot_hit_existing_entry(cache_case, monkeypatch):
    from pcc.backend import BackendUnavailable
    items, plan = cache_case
    first = plan()
    Path(items[0][1]).write_bytes(b"owned-object-test-bytes")
    assert cache.publish(items, first, "", host_python_command=None, publish_host_code="")
    monkeypatch.setenv("PCC_SELF_TARGET_PASSES", "not-a-real-pass")
    with pytest.raises(BackendUnavailable, match="unknown self target pass"):
        plan()


@pytest.mark.parametrize("setting,value", [
    ("PCC_SELF_BACKEND_OBJECT_CACHE", "0"),
    ("PCC_SELF_BACKEND_OBJECT_CACHE_IDENTITY", ""),
])
def test_disabled_path_does_not_resolve_configuration(cache_case, monkeypatch, setting, value):
    items, plan = cache_case
    monkeypatch.setenv(setting, value)
    monkeypatch.setenv("PCC_SELF_TARGET_PASSES", "not-a-real-pass")
    Path(items[0][2]).unlink()
    assert plan() == [("", "off")]


# bounded host worker receipt uses Linux RLIMIT_AS
@pytest.mark.pcc_gate(probe=lambda: sys.platform == "linux")
def test_real_owned_worker_subprocess_cache_round_trip(monkeypatch, tmp_path):
    """Exercise the exact hidden worker route, without a pcc1 build or link."""
    import os
    import subprocess
    import sys

    import pcc

    for name in SETTINGS:
        monkeypatch.delenv(name, raising=False)
    root = Path(pcc.__file__).resolve().parents[1]
    cache_root = tmp_path / "private-worker-cache"
    cache_root.mkdir(mode=0o700)
    monkeypatch.setenv(cache.OBJECT_CACHE_ENV, "1")
    # Deliberately isolated test namespace; not a production provenance claim.
    monkeypatch.setenv(cache.OBJECT_CACHE_IDENTITY_ENV, "test-real-owned-worker-v1")
    monkeypatch.setenv(cache.OBJECT_CACHE_DIR_ENV, str(cache_root))
    monkeypatch.setenv("PCC_NO_AUTO_PCC1", "1")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setenv("PYTHONPATH", str(root))
    ir = tmp_path / "worker.ll"
    ir.write_text('target triple = "aarch64-apple-darwin"\n'
                  'define i64 @answer() {\nentry:\n ret i64 42\n}\n')
    ir_hash = hashlib.sha256(ir.read_bytes()).hexdigest()
    result = tmp_path / "worker.result"
    obj = tmp_path / "worker.pco"
    items = [(str(result), str(obj), str(ir))]
    target = "self-aarch64-darwin-v0"
    invocations = []

    # Set the bound in a normal child interpreter, then exec the exact worker.
    # A preexec_fn can deadlock before subprocess.run's timeout starts.
    bounded_launcher = (
        "import os, resource, sys; "
        "resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 ** 2,) * 2); "
        "os.execv(sys.argv[1], sys.argv[1:])"
    )

    def compile_or_hit():
        obj.unlink(missing_ok=True)
        result.unlink(missing_ok=True)
        planned = cache.plan(items, target, "pcc-native-pco-v1", str(tmp_path),
                             host_python_command=None, plan_host_code="",
                             small_int_decimal=None)
        path, status = planned[0]
        if status == "miss":
            command = [sys.executable, "-B", "-m", "pcc",
                       "--pcc-self-backend-emit-worker", str(ir), str(result),
                       str(obj), ""]
            run = subprocess.run(
                [sys.executable, "-B", "-c", bounded_launcher, *command],
                cwd=root, capture_output=True, text=True, timeout=8,
            )
            assert run.returncode == 0, run.stderr
            invocations.append(command)
            assert result.read_text().splitlines()[:2] == [target, str(obj)]
            assert obj.stat().st_size > 0
            assert cache.publish(items, planned, str(tmp_path),
                                 host_python_command=None, publish_host_code="")
        else:
            assert status == "hit"
            assert result.read_text().splitlines() == [target, str(obj), "hit"]
        artifact_hash = hashlib.sha256(obj.read_bytes()).hexdigest()
        assert Path(path + ".sha256").read_text().strip() == artifact_hash
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == artifact_hash
        assert hashlib.sha256(ir.read_bytes()).hexdigest() == ir_hash
        return path, status, artifact_hash

    cold = compile_or_hit()
    warm = compile_or_hit()
    assert cold[1] == "miss" and warm[1] == "hit"
    assert cold[0] == warm[0] and cold[2] == warm[2]
    assert len(invocations) == 1
    monkeypatch.setenv("PCC_SELF_TARGET_PASSES", "all")
    changed_passes = compile_or_hit()
    assert changed_passes[0] != cold[0] and changed_passes[1] == "miss"
    # The worker may intentionally ignore some text passes for this target;
    # key separation must not depend on this tiny program exposing a change.
    assert compile_or_hit()[1] == "hit"
    monkeypatch.delenv("PCC_SELF_TARGET_PASSES")
    monkeypatch.setenv("PCC_SELF_BRANCH_PROTECTION", "0")
    changed_protection = compile_or_hit()
    assert changed_protection[0] != cold[0] and changed_protection[1] == "miss"
    assert changed_protection[2] != cold[2]
    assert compile_or_hit()[1] == "hit"
    assert len(invocations) == 3
    assert not list(cache_root.rglob("*.pcc-lease.*"))
    import json
    receipt = {
        "schema": "pcc.test-owned-worker-cache-round-trip.v1",
        "ir_sha256": ir_hash,
        "backend_source_identity": identity_module.self_backend_emitter_source_identity(),
        "caller_identity": "test-real-owned-worker-v1",
        "target": target,
        "mode": "pcc-native-pco-v1",
        "worker_invocations": len(invocations),
        "memory_limit_bytes": 512 * 1024 ** 2,
        "cold": {"key": Path(cold[0]).stem, "status": cold[1], "object_sha256": cold[2]},
        "warm": {"key": Path(warm[0]).stem, "status": warm[1], "object_sha256": warm[2]},
        "all_passes": {"key": Path(changed_passes[0]).stem, "status": changed_passes[1], "object_sha256": changed_passes[2]},
        "branch_protection_off": {"key": Path(changed_protection[0]).stem, "status": changed_protection[1], "object_sha256": changed_protection[2]},
    }
    print("OWNED_WORKER_CACHE_RECEIPT " + json.dumps(receipt, sort_keys=True))
