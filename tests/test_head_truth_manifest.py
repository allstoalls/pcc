from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

import scripts.head_truth_manifest as head_truth
import scripts.head_truth_gate as head_truth_gate

from scripts.head_truth_manifest import (
    FAIL,
    PASS,
    TIMEOUT,
    GateSpec,
    GateResult,
    _pytest_summary,
    build_manifest,
    selected_gate_failures,
    serialize_manifest,
    validate_manifest,
)


def _runtime_provenance_observation() -> dict[str, object]:
    return {
        "artifact_paths": [
            "pcc/runtime/libpy_runtime_pcc_py.a",
            "pcc/runtime/libpy_runtime_pcc_py.a.provenance.json",
            "pcc/runtime/libpy_runtime_pcc_py.a.capi_syms",
        ],
        "backend": "self",
        "gc_backend": None,
        "links_libpython": None,
        "pcc2_pcc3_equal": None,
        "schema": head_truth.RUNTIME_ARCHIVE_PROVENANCE_SCHEMA,
        "policy": "pcc-production-no-handwritten-c.v1",
        "target_triple": "arm64-apple-darwin",
        "member_count": 2,
        "archive_sha256": "4" * 64,
        "manifest_sha256": "5" * 64,
        "members_sha256": "6" * 64,
        "capi_symbol_count": 2,
        "capi_inventory_sha256": "7" * 64,
        "producer_kind": "pcc-python-library-ir-to-obj",
        "source_kind": "pcc-python",
        "object_emitter": "pcc-self-backend-object-writer",
        "uses_host_cc": False,
        "failure": None,
    }


def _five_gc_observations() -> list[dict[str, object]]:
    return [
        {
            "backend": "self",
            "gc_backend": str(gc_backend),
            "links_libpython": False,
            "pcc2_pcc3_equal": True,
            "pytest_nodeid": (
                "tests/python/gc/test_pcc_bootstrap_full_gc"
                f"{gc_backend}.py::test_full_three_stage_bootstrap_self_gc{gc_backend}"
            ),
            "pytest_status": "passed",
            "artifact_paths": [
                (
                    "build/bootstrap-pytest-self"
                    if gc_backend == 0
                    else f"build/bootstrap-pytest-self-gc{gc_backend}"
                )
                + f"/pcc{stage}"
                for stage in (1, 2, 3)
            ],
            "failure": None,
        }
        for gc_backend in range(5)
    ]


def _result(
    gate_id: str,
    *,
    kind: str = "pytest",
    status: str = PASS,
) -> GateResult:
    registered = {spec.gate_id: spec for spec in head_truth.gate_specs(Path("."))}
    spec = registered.get(gate_id)
    bootstrap = gate_id == "self-five-gc-bootstrap"
    runtime_archive = gate_id == "runtime-archive-preflight"
    native_artifact = bootstrap or gate_id == "numpy-core-head"
    return GateResult(
        gate_id=gate_id,
        command=list(spec.command) if spec is not None else ["uv", "run", "pytest"],
        timeout_seconds=spec.timeout_seconds if spec is not None else 10,
        kind=spec.kind if spec is not None else kind,
        status=status,
        returncode=0 if status == PASS else 1,
        duration_seconds=1.5,
        log_path=f"build/{gate_id}.log",
        output_sha256="a" * 64,
        pytest_summary=(
            "5 passed in 0.10s"
            if gate_id == "self-five-gc-bootstrap"
            else "1 passed in 0.10s" if kind.startswith("pytest") else None
        ),
        backend=(
            spec.backend
            if spec is not None
            else "self" if (runtime_archive or native_artifact) else None
        ),
        gc_backend=(
            spec.gc_backend
            if spec is not None
            else "0..4" if gate_id == "self-five-gc-bootstrap" else None
        ),
        links_libpython=False if native_artifact else None,
        pcc2_pcc3_equal=True if bootstrap else None,
        artifact_paths=(
            ["build/pcc1", "build/pcc2", "build/pcc3"]
            if bootstrap
            else (
                ["build/head-truth/numpy-core/result.json"]
                if gate_id == "numpy-core-head"
                else (
                    _runtime_provenance_observation()["artifact_paths"]
                    if runtime_archive
                    else []
                )
            )
        ),
        observations=(
            [_runtime_provenance_observation()]
            if runtime_archive
            else (
                _five_gc_observations()
                if gate_id == "self-five-gc-bootstrap"
                else (
                    [
                        {
                            "first_blocker": {
                                "kind": "first_missing_module",
                                "phase": "Py_mod_exec",
                                "value": "math",
                            },
                            "first_blocker_ratchet": {
                                "accepted": True,
                                "status": "STABLE",
                            },
                        }
                    ]
                    if gate_id == "numpy-core-head"
                    else []
                )
            )
        ),
    )


def _manifest() -> dict[str, object]:
    return build_manifest(
        source={
            "commit": "1" * 40,
            "worktree_dirty": False,
            "worktree_fingerprint": "2" * 64,
        },
        platform_info={
            "system": "Darwin",
            "release": "test",
            "machine": "arm64",
            "python": "3.13.0",
        },
        results=[
            _result("runtime-archive-preflight", kind="command"),
            _result("fallback-ratchet"),
            _result("gc-production-contract"),
            _result("self-five-gc-bootstrap", kind="pytest-bootstrap"),
            _result("numpy-core-head", kind="command"),
        ],
        generated_at="2026-07-10T00:00:00Z",
    )


def _gate(manifest: dict[str, object], gate_id: str) -> dict[str, object]:
    return next(gate for gate in manifest["gates"] if gate["gate_id"] == gate_id)


def test_runtime_archive_inspector_records_verified_provenance(
    tmp_path: Path, monkeypatch
) -> None:
    runtime_root = tmp_path / "pcc" / "runtime"
    runtime_root.mkdir(parents=True)
    archive = runtime_root / "libpy_runtime_pcc_py.a"
    archive.write_bytes(b"verified archive")
    provenance_path = Path(str(archive) + ".provenance.json")
    provenance_path.write_text('{"receipt":"verified"}\n', encoding="utf-8")
    capi_inventory_path = Path(str(archive) + ".capi_syms")
    capi_inventory_path.write_text(
        "PyRuntime_First\nPyRuntime_Second\n",
        encoding="ascii",
    )
    verified = {
        "schema": head_truth.RUNTIME_ARCHIVE_PROVENANCE_SCHEMA,
        "archive": archive.name,
        "policy": "pcc-production-no-handwritten-c.v1",
        "target_triple": "arm64-apple-darwin",
        "member_count": 2,
        "members_sha256": "3" * 64,
        "capi_symbol_count": 2,
        "capi_inventory_sha256": hashlib.sha256(
            capi_inventory_path.read_bytes()
        ).hexdigest(),
        "capi_symbols": ["PyRuntime_First", "PyRuntime_Second"],
        "members": [
            {
                "producer_kind": "pcc-python-library-ir-to-obj",
                "source_kind": "pcc-python",
                "object_emitter": "pcc-self-backend-object-writer",
                "uses_host_cc": False,
            },
            {
                "producer_kind": "pcc-python-library-ir-to-obj",
                "source_kind": "pcc-python",
                "object_emitter": "pcc-self-backend-object-writer",
                "uses_host_cc": False,
            },
        ],
    }
    monkeypatch.setattr(
        head_truth,
        "verify_runtime_archive_manifest",
        lambda inspected, *, runtime_root: (
            verified
            if inspected == archive and runtime_root == archive.parent
            else None
        ),
    )

    observations = head_truth.inspect_runtime_archive_artifact(tmp_path)

    assert observations == [
        {
            "artifact_paths": [
                "pcc/runtime/libpy_runtime_pcc_py.a",
                "pcc/runtime/libpy_runtime_pcc_py.a.provenance.json",
                "pcc/runtime/libpy_runtime_pcc_py.a.capi_syms",
            ],
            "backend": "self",
            "gc_backend": None,
            "links_libpython": None,
            "pcc2_pcc3_equal": None,
            "schema": head_truth.RUNTIME_ARCHIVE_PROVENANCE_SCHEMA,
            "policy": "pcc-production-no-handwritten-c.v1",
            "target_triple": "arm64-apple-darwin",
            "member_count": 2,
            "archive_sha256": hashlib.sha256(b"verified archive").hexdigest(),
            "manifest_sha256": hashlib.sha256(b'{"receipt":"verified"}\n').hexdigest(),
            "members_sha256": "3" * 64,
            "capi_symbol_count": 2,
            "capi_inventory_sha256": hashlib.sha256(
                b"PyRuntime_First\nPyRuntime_Second\n"
            ).hexdigest(),
            "producer_kind": "pcc-python-library-ir-to-obj",
            "source_kind": "pcc-python",
            "object_emitter": "pcc-self-backend-object-writer",
            "uses_host_cc": False,
            "failure": None,
        }
    ]


def test_runtime_archive_preflight_uses_self_mode_and_verified_observation(
    tmp_path: Path, monkeypatch
) -> None:
    spec = next(
        spec
        for spec in head_truth.gate_specs(tmp_path)
        if spec.gate_id == "runtime-archive-preflight"
    )
    process = subprocess.CompletedProcess(
        args=list(spec.command), returncode=0, stdout="archive built\n", stderr=""
    )
    captured_env: dict[str, str] = {}

    def runner(*_args, **kwargs):
        captured_env.update(kwargs["env"])
        return process

    monkeypatch.setattr(
        head_truth,
        "inspect_runtime_archive_artifact",
        lambda _repo_root: [_runtime_provenance_observation()],
    )

    result = head_truth.run_gate(
        tmp_path,
        tmp_path / "logs",
        spec,
        runner,
    )

    assert spec.backend == "self"
    assert captured_env["PCC_BACKEND"] == "self"
    assert result.status == PASS
    assert result.backend == "self"
    assert result.links_libpython is None
    assert result.observations == [_runtime_provenance_observation()]


def test_runtime_archive_preflight_fails_when_provenance_manifest_is_missing(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "pcc" / "runtime"
    runtime_root.mkdir(parents=True)
    (runtime_root / "libpy_runtime_pcc_py.a").write_bytes(b"archive")
    spec = next(
        spec
        for spec in head_truth.gate_specs(tmp_path)
        if spec.gate_id == "runtime-archive-preflight"
    )
    process = subprocess.CompletedProcess(
        args=list(spec.command), returncode=0, stdout="archive built\n", stderr=""
    )

    result = head_truth.run_gate(
        tmp_path,
        tmp_path / "logs",
        spec,
        lambda *_args, **_kwargs: process,
    )

    assert result.status == FAIL
    assert result.failure is not None
    assert result.failure.startswith("runtime archive provenance invalid:")
    assert len(result.observations) == 1
    assert result.observations[0]["failure"] == result.failure


def test_runtime_archive_preflight_fails_when_provenance_manifest_is_tampered(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "pcc" / "runtime"
    runtime_root.mkdir(parents=True)
    archive = runtime_root / "libpy_runtime_pcc_py.a"
    archive.write_bytes(b"archive")
    Path(str(archive) + ".provenance.json").write_text(
        json.dumps(
            {
                "schema": "tampered",
                "archive": archive.name,
                "policy": "pcc-production-no-handwritten-c.v1",
                "target_triple": "arm64-apple-darwin",
                    "member_count": 1,
                    "members_sha256": "7" * 64,
                    "members": [{}],
                    "capi_symbol_count": 1,
                    "capi_inventory_sha256": "7" * 64,
                    "capi_symbols": ["PyRuntime_Tampered"],
                }
        ),
        encoding="utf-8",
    )
    spec = next(
        spec
        for spec in head_truth.gate_specs(tmp_path)
        if spec.gate_id == "runtime-archive-preflight"
    )
    process = subprocess.CompletedProcess(
        args=list(spec.command), returncode=0, stdout="archive built\n", stderr=""
    )

    result = head_truth.run_gate(
        tmp_path,
        tmp_path / "logs",
        spec,
        lambda *_args, **_kwargs: process,
    )

    assert result.status == FAIL
    assert result.failure is not None
    assert "invalid runtime archive manifest schema" in result.failure
    assert result.observations[0]["failure"] == result.failure


@pytest.mark.parametrize(
    "observations",
    [[], [_runtime_provenance_observation(), _runtime_provenance_observation()]],
)
def test_runtime_archive_preflight_zero_exit_rejects_nonunique_observations(
    tmp_path: Path, monkeypatch, observations: list[dict[str, object]]
) -> None:
    spec = next(
        spec
        for spec in head_truth.gate_specs(tmp_path)
        if spec.gate_id == "runtime-archive-preflight"
    )
    process = subprocess.CompletedProcess(
        args=list(spec.command), returncode=0, stdout="archive built\n", stderr=""
    )
    monkeypatch.setattr(
        head_truth,
        "inspect_runtime_archive_artifact",
        lambda _repo_root: observations,
    )

    result = head_truth.run_gate(
        tmp_path,
        tmp_path / "logs",
        spec,
        lambda *_args, **_kwargs: process,
    )

    assert result.status == FAIL
    assert result.failure == (
        "runtime-archive-preflight requires exactly one provenance observation"
    )


def test_complete_manifest_is_commit_bound_and_deterministic() -> None:
    manifest = _manifest()

    assert validate_manifest(manifest, require_complete=True) == []
    assert manifest["complete"] is True
    assert manifest["claimable_commit"] is True
    assert serialize_manifest(manifest) == serialize_manifest(deepcopy(manifest))
    assert json.loads(serialize_manifest(manifest)) == manifest


def test_incomplete_or_dirty_manifest_is_not_claimable() -> None:
    manifest = _manifest()
    manifest["source"]["worktree_dirty"] = True
    manifest["claimable_commit"] = False
    fallback = _gate(manifest, "fallback-ratchet")
    fallback["status"] = FAIL
    fallback["returncode"] = 1
    manifest["complete"] = False

    errors = validate_manifest(manifest, require_complete=True)

    assert "required gate fallback-ratchet is not PASS" in errors
    assert "complete manifest required" in errors


def test_pass_rejects_missing_summary_timeout_and_bad_bootstrap_claims() -> None:
    manifest = _manifest()
    _gate(manifest, "fallback-ratchet")["pytest_summary"] = None
    _gate(manifest, "gc-production-contract")["returncode"] = 124
    five_gc = _gate(manifest, "self-five-gc-bootstrap")
    five_gc["links_libpython"] = True
    five_gc["pcc2_pcc3_equal"] = False

    errors = validate_manifest(manifest)

    assert "fallback-ratchet: pytest PASS requires a final summary" in errors
    assert "gc-production-contract: PASS requires returncode 0" in errors
    assert "self-five-gc-bootstrap: PASS requires links_libpython=false" in errors
    assert "self-five-gc-bootstrap: PASS requires pcc2_pcc3_equal=true" in errors


def test_runtime_archive_pass_requires_exactly_one_provenance_observation() -> None:
    manifest = _manifest()
    _gate(manifest, "runtime-archive-preflight")["observations"] = []

    errors = validate_manifest(manifest)

    assert (
        "runtime-archive-preflight: PASS requires exactly one provenance observation"
        in errors
    )


def test_runtime_archive_pass_rejects_failed_provenance_observation() -> None:
    manifest = _manifest()
    observation = _gate(manifest, "runtime-archive-preflight")["observations"][0]
    observation["failure"] = "runtime archive provenance invalid: stale digest"

    errors = validate_manifest(manifest)

    assert (
        "runtime-archive-preflight: PASS requires a valid provenance observation"
        in errors
    )


@pytest.mark.parametrize(
    ("scope", "field", "invalid", "error_fragment"),
    [
        ("gate", "backend", "llvm", "backend must be self"),
        ("observation", "backend", "llvm", "observation backend must be self"),
        ("observation", "schema", "tampered", "schema is invalid"),
        ("observation", "policy", "tampered", "policy is invalid"),
        ("observation", "target_triple", "", "target_triple is invalid"),
        ("observation", "member_count", 0, "member_count is invalid"),
        ("observation", "archive_sha256", "bad", "archive_sha256 is invalid"),
        ("observation", "manifest_sha256", "bad", "manifest_sha256 is invalid"),
        ("observation", "members_sha256", "bad", "members_sha256 is invalid"),
        (
            "observation",
            "capi_inventory_sha256",
            "bad",
            "capi_inventory_sha256 is invalid",
        ),
        (
            "observation",
            "capi_symbol_count",
            0,
            "capi_symbol_count is invalid",
        ),
        ("observation", "producer_kind", "host-cc", "producer_kind is invalid"),
        ("observation", "source_kind", "c", "source_kind is invalid"),
        (
            "observation",
            "object_emitter",
            "host-cc",
            "object_emitter is invalid",
        ),
        ("observation", "uses_host_cc", True, "uses_host_cc must be false"),
    ],
)
def test_runtime_archive_pass_rejects_invalid_provenance_metadata(
    scope: str,
    field: str,
    invalid: object,
    error_fragment: str,
) -> None:
    manifest = _manifest()
    gate = _gate(manifest, "runtime-archive-preflight")
    target = gate if scope == "gate" else gate["observations"][0]
    target[field] = invalid

    errors = validate_manifest(manifest)

    assert any(error_fragment in error for error in errors)


def test_numpy_pass_requires_classified_or_empty_accepted_blocker_record() -> None:
    manifest = _manifest()
    numpy_gate = _gate(manifest, "numpy-core-head")
    numpy_gate["observations"][0]["first_blocker"] = {
        "kind": "provider_slot_count",
        "phase": "Py_mod_exec",
        "value": "200",
    }
    numpy_gate["observations"][0]["first_blocker_ratchet"] = {
        "accepted": False,
        "status": "UNREVIEWED_CHANGE",
    }

    errors = validate_manifest(manifest)

    assert any("explicit empty completion record" in error for error in errors)
    assert "numpy-core-head: PASS requires an accepted first-blocker ratchet" in errors


def test_numpy_pass_accepts_explicit_empty_completion_record() -> None:
    manifest = _manifest()
    observation = _gate(manifest, "numpy-core-head")["observations"][0]
    observation["first_blocker"] = None
    observation["first_blocker_ratchet"] = {
        "accepted": True,
        "status": "STABLE",
    }

    assert validate_manifest(manifest) == []


def test_truth_manifest_tooling_stays_out_of_pcc_compiler_closure() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]

    assert not (root / "pcc" / "head_truth_manifest.py").exists()
    assert (root / "scripts" / "head_truth_manifest.py").is_file()


def test_pytest_summary_accepts_elapsed_clock_suffix() -> None:
    summary, counts = _pytest_summary("20 passed in 166.16s (0:02:46)\n")

    assert summary == "20 passed in 166.16s"
    assert counts == {"passed": 20}


def test_self_five_gc_gate_rejects_a_partial_pytest_summary(
    tmp_path: Path, monkeypatch
) -> None:
    spec = GateSpec(
        gate_id="self-five-gc-bootstrap",
        suite="heavy",
        command=("uv", "run", "pytest", "-q", "-m", "integration"),
        timeout_seconds=1800,
        kind="pytest-bootstrap",
        backend="self",
        gc_backend="0..4",
    )
    process = subprocess.CompletedProcess(
        args=list(spec.command),
        returncode=0,
        stdout="1 passed in 0.10s\n",
        stderr="",
    )
    monkeypatch.setattr(
        head_truth,
        "inspect_bootstrap_artifacts",
        lambda *_args, **_kwargs: _five_gc_observations(),
    )

    result = head_truth.run_gate(
        tmp_path,
        tmp_path / "logs",
        spec,
        lambda *_args, **_kwargs: process,
    )

    assert result.status == FAIL
    assert result.failure == "self-five-gc-bootstrap executed fewer than five tests"


def test_self_five_gc_gate_records_current_success_for_every_backend(
    tmp_path: Path, monkeypatch
) -> None:
    spec = next(
        spec
        for spec in head_truth.gate_specs(tmp_path)
        if spec.gate_id == "self-five-gc-bootstrap"
    )
    passed = "\n".join(
        "PASSED tests/python/gc/test_pcc_bootstrap_full_gc"
        f"{gc_backend}.py::test_full_three_stage_bootstrap_self_gc{gc_backend}"
        for gc_backend in range(5)
    )
    process = subprocess.CompletedProcess(
        args=list(spec.command),
        returncode=0,
        stdout=(
            "================ short test summary info ================\n"
            + passed
            + "\n5 passed in 0.10s\n"
        ),
        stderr="",
    )
    monkeypatch.setattr(
        head_truth,
        "inspect_bootstrap_artifacts",
        lambda *_args, **_kwargs: _five_gc_observations(),
    )

    result = head_truth.run_gate(
        tmp_path,
        tmp_path / "logs",
        spec,
        lambda *_args, **_kwargs: process,
    )

    assert result.status == PASS
    assert [observation["pytest_status"] for observation in result.observations] == [
        "passed"
    ] * 5
    assert [observation["gc_backend"] for observation in result.observations] == [
        "0",
        "1",
        "2",
        "3",
        "4",
    ]


def test_self_five_gc_gate_rejects_a_missing_current_backend_success(
    tmp_path: Path, monkeypatch
) -> None:
    spec = next(
        spec
        for spec in head_truth.gate_specs(tmp_path)
        if spec.gate_id == "self-five-gc-bootstrap"
    )
    passed = "\n".join(
        "PASSED tests/python/gc/test_pcc_bootstrap_full_gc"
        f"{gc_backend}.py::test_full_three_stage_bootstrap_self_gc{gc_backend}"
        for gc_backend in range(4)
    )
    process = subprocess.CompletedProcess(
        args=list(spec.command),
        returncode=0,
        stdout=(
            "================ short test summary info ================\n"
            + passed
            + "\n5 passed in 0.10s\n"
        ),
        stderr="",
    )
    monkeypatch.setattr(
        head_truth,
        "inspect_bootstrap_artifacts",
        lambda *_args, **_kwargs: _five_gc_observations(),
    )

    result = head_truth.run_gate(
        tmp_path,
        tmp_path / "logs",
        spec,
        lambda *_args, **_kwargs: process,
    )

    assert result.status == FAIL
    assert result.failure == (
        "self-five-gc-bootstrap requires one current pytest PASS and one valid "
        "artifact observation for each GC backend 0..4"
    )
    assert result.observations[4]["pytest_status"] is None


@pytest.mark.parametrize(
    ("returncode", "output", "expected_failure"),
    [
        (
            0,
            "5 passed, 1 xpassed in 0.10s\n",
            "pytest summary is not strict green: 5 passed, 1 xpassed in 0.10s",
        ),
        (
            0,
            "0 passed in 0.10s\n",
            "pytest summary is not strict green: 0 passed in 0.10s",
        ),
        (5, "5 deselected in 0.10s\n", "command exited 5"),
    ],
)
def test_pytest_gate_rejects_xpass_and_all_deselected_runs(
    tmp_path: Path,
    returncode: int,
    output: str,
    expected_failure: str,
) -> None:
    spec = GateSpec(
        gate_id="test-strict-pytest",
        suite="light",
        command=("uv", "run", "pytest"),
        timeout_seconds=30,
        kind="pytest",
    )
    process = subprocess.CompletedProcess(
        args=list(spec.command), returncode=returncode, stdout=output, stderr=""
    )

    result = head_truth.run_gate(
        tmp_path,
        tmp_path / "logs",
        spec,
        lambda *_args, **_kwargs: process,
    )

    assert result.status == FAIL
    assert result.failure == expected_failure


def test_self_five_gc_manifest_requires_all_unique_backend_observations() -> None:
    manifest = _manifest()
    gate = _gate(manifest, "self-five-gc-bootstrap")
    gate["observations"] = gate["observations"][:-1]

    errors = validate_manifest(manifest)

    assert (
        "self-five-gc-bootstrap: PASS requires one current pytest PASS and one "
        "valid artifact observation for each GC backend 0..4"
    ) in errors


def test_self_five_gc_manifest_rejects_partial_summary_and_reused_paths() -> None:
    manifest = _manifest()
    gate = _gate(manifest, "self-five-gc-bootstrap")
    gate["pytest_summary"] = "1 passed in 0.10s"
    first_paths = gate["observations"][0]["artifact_paths"]
    for observation in gate["observations"][1:]:
        observation["artifact_paths"] = list(first_paths)

    errors = validate_manifest(manifest)

    assert "self-five-gc-bootstrap: PASS requires at least five passed tests" in errors
    assert (
        "self-five-gc-bootstrap: PASS requires one current pytest PASS and one "
        "valid artifact observation for each GC backend 0..4"
    ) in errors


def test_self_five_gc_manifest_rejects_an_unproven_backend_success() -> None:
    manifest = _manifest()
    gate = _gate(manifest, "self-five-gc-bootstrap")
    gate["observations"][2]["pytest_status"] = None

    errors = validate_manifest(manifest)

    assert (
        "self-five-gc-bootstrap: PASS requires one current pytest PASS and one "
        "valid artifact observation for each GC backend 0..4"
    ) in errors


def test_manifest_rejects_a_gate_command_that_differs_from_registry() -> None:
    manifest = _manifest()
    gate = _gate(manifest, "self-five-gc-bootstrap")
    gate["command"] = [
        item for item in gate["command"] if item not in {"-m", "integration"}
    ]

    errors = validate_manifest(manifest)

    assert "self-five-gc-bootstrap: command differs from registry" in errors


def test_selected_gate_failures_reject_keep_going_failures() -> None:
    results = [
        _result("fallback-ratchet"),
        _result("control-plane-ratchets", status=FAIL),
        _result("gc-production-contract"),
    ]

    assert selected_gate_failures(
        results,
        {"fallback-ratchet", "control-plane-ratchets"},
    ) == ["selected gate control-plane-ratchets is FAIL"]


def test_truth_runner_rejects_unknown_single_gate_without_running(capsys) -> None:
    assert head_truth_gate.main(["run", "--gate", "does-not-exist"]) == 2
    assert "unknown gate(s): does-not-exist" in capsys.readouterr().err


def test_numpy_gate_keeps_fixed_point_field_not_applicable(
    tmp_path: Path, monkeypatch
) -> None:
    spec = GateSpec(
        gate_id="numpy-core-head",
        suite="heavy",
        command=("uv", "run", "python", "scripts/numpy_head_gate.py"),
        timeout_seconds=1200,
        kind="command",
        backend="self",
    )
    process = subprocess.CompletedProcess(
        args=list(spec.command), returncode=0, stdout="PASS\n", stderr=""
    )
    monkeypatch.setattr(
        head_truth,
        "inspect_numpy_head_artifact",
        lambda *_args, **_kwargs: [
            {
                "artifact_paths": ["build/head-truth/numpy-core/result.json"],
                "backend": "self",
                "gc_backend": None,
                "links_libpython": False,
                "pcc2_pcc3_equal": None,
                "failure": None,
            }
        ],
    )

    result = head_truth.run_gate(
        tmp_path,
        tmp_path / "logs",
        spec,
        lambda *_args, **_kwargs: process,
    )

    assert result.status == PASS
    assert result.links_libpython is False
    assert result.pcc2_pcc3_equal is None


def test_timeout_status_survives_bootstrap_artifact_inspection(
    tmp_path: Path, monkeypatch
) -> None:
    spec = GateSpec(
        gate_id="self-five-gc-bootstrap",
        suite="heavy",
        command=("uv", "run", "pytest"),
        timeout_seconds=900,
        kind="pytest-bootstrap",
        backend="self",
        gc_backend="0..4",
    )
    process = subprocess.CompletedProcess(
        args=list(spec.command),
        returncode=124,
        stdout="[TIMEOUT] killed process group after 900.0s\n",
        stderr="",
    )
    monkeypatch.setattr(
        head_truth,
        "inspect_bootstrap_artifacts",
        lambda *_args, **_kwargs: [
            {
                "artifact_paths": ["build/pcc1", "build/pcc2", "build/pcc3"],
                "backend": "self",
                "gc_backend": "4",
                "links_libpython": None,
                "pcc2_pcc3_equal": None,
                "failure": "missing artifacts: build/pcc3",
            }
        ],
    )

    result = head_truth.run_gate(
        tmp_path,
        tmp_path / "logs",
        spec,
        lambda *_args, **_kwargs: process,
    )

    assert result.status == TIMEOUT
    assert result.failure == "command timed out after 900s"
    assert result.observations[0]["failure"] == "missing artifacts: build/pcc3"
