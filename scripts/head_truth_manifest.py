"""Commit-bound truth manifest for release and milestone claims."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile
import time
from typing import Callable, Sequence

from pcc.diagnostics.macho_normalize import normalize_macho_metadata
from pcc.tools.runtime_archive_provenance import (
    MANIFEST_SCHEMA as RUNTIME_ARCHIVE_PROVENANCE_SCHEMA,
    PRODUCTION_POLICY as RUNTIME_ARCHIVE_PRODUCTION_POLICY,
    ProvenanceError,
    capi_inventory_path_for_archive,
    manifest_path_for_archive,
    verify_runtime_archive_manifest,
)
from scripts.numpy_first_blocker import ALLOWED_KINDS, evaluate_result

SCHEMA = "pcc.head_truth.v1"
PASS = "PASS"
FAIL = "FAIL"
TIMEOUT = "TIMEOUT"
NOT_RUN = "NOT_RUN"
VALID_GATE_STATUSES = {PASS, FAIL, TIMEOUT, NOT_RUN}
REQUIRED_GATE_IDS = (
    "runtime-archive-preflight",
    "fallback-ratchet",
    "gc-production-contract",
    "self-five-gc-bootstrap",
    "numpy-core-head",
)

_PYTEST_SUMMARY_RE = re.compile(
    r"(?m)^(?:=+\s*)?"
    r"(?P<summary>"
    r"\d+\s+(?:passed|failed|skipped|xfailed|xpassed|error|errors)"
    r"(?:,\s*\d+\s+(?:passed|failed|skipped|xfailed|xpassed|error|errors))*"
    r"\s+in\s+[0-9.]+s"
    r")(?:\s+\([0-9:]+\))?(?:\s*=+)?$"
)
_PYTEST_COUNT_RE = re.compile(
    r"(?P<count>\d+)\s+(?P<label>passed|failed|skipped|xfailed|xpassed|error|errors)"
)
_FIVE_GC_TEST_NODEIDS = {
    str(gc_backend): (
        "tests/python/gc/test_pcc_bootstrap_full_gc"
        f"{gc_backend}.py::test_full_three_stage_bootstrap_self_gc{gc_backend}"
    )
    for gc_backend in range(5)
}
_RUNTIME_ARCHIVE = Path("pcc/runtime/libpy_runtime_pcc_py.a")
# The archive is produced by the owned self backend's object writer; the
# llvmlite route was removed with the dependency, and these labels had drifted
# out of agreement with the archive's own provenance.
_RUNTIME_ARCHIVE_BACKEND = "self"
_RUNTIME_ARCHIVE_PRODUCER = "pcc-python-library-ir-to-obj"
_RUNTIME_ARCHIVE_SOURCE_KIND = "pcc-python"
_RUNTIME_ARCHIVE_OBJECT_EMITTER = "pcc-self-backend-object-writer"


@dataclass(frozen=True)
class GateSpec:
    gate_id: str
    suite: str
    command: tuple[str, ...]
    timeout_seconds: int
    kind: str
    backend: str | None = None
    gc_backend: str | None = None


@dataclass
class GateResult:
    gate_id: str
    command: list[str]
    timeout_seconds: int
    kind: str
    status: str
    returncode: int | None
    duration_seconds: float | None
    log_path: str | None
    output_sha256: str | None
    pytest_summary: str | None
    backend: str | None
    gc_backend: str | None
    links_libpython: bool | None = None
    pcc2_pcc3_equal: bool | None = None
    artifact_paths: list[str] = field(default_factory=list)
    observations: list[dict[str, object]] = field(default_factory=list)
    failure: str | None = None


def gate_specs(repo_root: Path) -> tuple[GateSpec, ...]:
    del repo_root
    return (
        GateSpec(
            gate_id="runtime-archive-preflight",
            suite="heavy",
            command=(
                "make",
                "-B",
                "-C",
                "pcc/runtime",
                "libpy_runtime_pcc_py.a",
                "PCC=../../.venv/bin/pcc",
                "PYTHON=../../.venv/bin/python3",
            ),
            timeout_seconds=900,
            kind="command",
            backend=_RUNTIME_ARCHIVE_BACKEND,
        ),
        GateSpec(
            gate_id="fallback-ratchet",
            suite="light",
            command=(
                "uv",
                "run",
                "pytest",
                "-q",
                "-n0",
                "tests/python/test_fallback_baseline.py",
                "tests/python/test_ir_py_fallback_baseline.py",
            ),
            timeout_seconds=420,
            kind="pytest",
        ),
        GateSpec(
            gate_id="control-plane-ratchets",
            suite="light",
            command=(
                "uv",
                "run",
                "pytest",
                "-q",
                "-n0",
                "tests/test_goal_state.py",
                "tests/test_head_truth_manifest.py",
                "tests/test_head_truth_workflows.py",
                "tests/test_numpy_head_gate.py",
                "tests/test_numpy_first_blocker.py",
                "tests/test_gc_bootstrap_xdist_group.py",
                "tests/python/test_pcc_bootstrap_full.py::test_bootstrap_gc_parallel_slots_grouped_files_use_one_slot",
                "tests/python/test_intent_constraints.py::TestObligation1ModeLabeling",
                "tests/python/test_intent_constraints.py::TestObligation3EcosystemGeneric",
                "tests/python/test_intent_constraints.py::TestObligation5FixedPointContract",
                "tests/python/test_intent_constraints.py::TestObligation6FiveGCComparativeStatic",
                "tests/python/test_intent_constraints.py::TestObligation7ValueModelStatic",
            ),
            timeout_seconds=180,
            kind="pytest",
        ),
        GateSpec(
            gate_id="gc-production-contract",
            suite="heavy",
            command=(
                "uv",
                "run",
                "pytest",
                "-q",
                "-n0",
                "tests/python/gc_production_contract",
            ),
            timeout_seconds=420,
            kind="pytest",
            backend="self",
            gc_backend="0..4",
        ),
        GateSpec(
            gate_id="self-five-gc-bootstrap",
            suite="heavy",
            command=(
                "uv",
                "run",
                "pytest",
                "-q",
                "-m",
                "integration",
                "-rA",
                "tests/python/gc/test_pcc_bootstrap_full_gc0.py",
                "tests/python/gc/test_pcc_bootstrap_full_gc1.py",
                "tests/python/gc/test_pcc_bootstrap_full_gc2.py",
                "tests/python/gc/test_pcc_bootstrap_full_gc3.py",
                "tests/python/gc/test_pcc_bootstrap_full_gc4.py",
            ),
            timeout_seconds=1800,
            kind="pytest-bootstrap",
            backend="self",
            gc_backend="0..4",
        ),
        GateSpec(
            gate_id="numpy-core-head",
            suite="heavy",
            command=(
                "uv",
                "run",
                "python",
                "scripts/numpy_head_gate.py",
                "run",
                "--source",
                "projects/numpy-2.4.4",
                "--build-root",
                "build/head-truth/numpy-core",
                "--result",
                "build/head-truth/numpy-core/result.json",
            ),
            timeout_seconds=1200,
            kind="command",
            backend="self",
        ),
    )


def _run_git(repo_root: Path, args: Sequence[str]) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        timeout=15,
        check=True,
    )
    return result.stdout


def source_identity(repo_root: Path) -> dict[str, object]:
    commit = _run_git(repo_root, ("rev-parse", "HEAD")).decode().strip()
    status = _run_git(repo_root, ("status", "--porcelain=v1", "-z"))
    diff = _run_git(repo_root, ("diff", "--binary", "HEAD", "--"))
    untracked = _run_git(
        repo_root, ("ls-files", "--others", "--exclude-standard", "-z")
    ).split(b"\0")
    digest = hashlib.sha256()
    digest.update(b"tracked-diff\0")
    digest.update(diff)
    for raw_path in sorted(path for path in untracked if path):
        path = repo_root / os.fsdecode(raw_path)
        digest.update(b"untracked\0")
        digest.update(raw_path)
        digest.update(b"\0")
        if path.is_file():
            digest.update(path.read_bytes())
    return {
        "commit": commit,
        "worktree_dirty": bool(status),
        "worktree_fingerprint": digest.hexdigest(),
    }


def platform_identity() -> dict[str, str]:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
    }


def _pytest_summary(output: str) -> tuple[str | None, dict[str, int]]:
    matches = list(_PYTEST_SUMMARY_RE.finditer(output))
    if not matches:
        return None, {}
    summary = matches[-1].group("summary")
    counts = {
        match.group("label"): int(match.group("count"))
        for match in _PYTEST_COUNT_RE.finditer(summary)
    }
    return summary, counts


def _five_gc_pytest_successes(output: str) -> dict[str, str]:
    """Return current-run GC backends proven PASS by pytest's ``-rA`` report."""

    marker = "short test summary info"
    marker_index = output.rfind(marker)
    if marker_index < 0:
        return {}
    report = output[marker_index + len(marker) :]
    passed_lines = {line.strip() for line in report.splitlines()}
    return {
        gc_backend: nodeid
        for gc_backend, nodeid in _FIVE_GC_TEST_NODEIDS.items()
        if f"PASSED {nodeid}" in passed_lines
    }


def _relative(path: Path, repo_root: Path) -> str:
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path)


def _links_libpython(path: Path) -> bool:
    command = (
        ["otool", "-L", str(path)]
        if platform.system() == "Darwin"
        else [
            "ldd",
            str(path),
        ]
    )
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    output = (result.stdout or "") + (result.stderr or "")
    return "libpython" in output or "Python.framework" in output


def _normalized_equal(left: Path, right: Path) -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        left_copy = Path(tmp) / "left"
        right_copy = Path(tmp) / "right"
        shutil.copy2(left, left_copy)
        shutil.copy2(right, right_copy)
        if platform.system() == "Darwin" and shutil.which("codesign"):
            for path in (left_copy, right_copy):
                subprocess.run(
                    ["codesign", "--remove-signature", str(path)],
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
                normalize_macho_metadata(path)
        return left_copy.read_bytes() == right_copy.read_bytes()


def _bootstrap_observation(
    repo_root: Path, *, backend: str, gc_backend: str | None, out_dir: Path
) -> dict[str, object]:
    stages = [out_dir / f"pcc{stage}" for stage in (1, 2, 3)]
    artifact_paths = [_relative(path, repo_root) for path in stages]
    missing = [path for path in stages if not path.exists()]
    if missing:
        return {
            "backend": backend,
            "gc_backend": gc_backend,
            "links_libpython": None,
            "pcc2_pcc3_equal": None,
            "artifact_paths": artifact_paths,
            "failure": "missing artifacts: "
            + ", ".join(_relative(path, repo_root) for path in missing),
        }
    return {
        "backend": backend,
        "gc_backend": gc_backend,
        "links_libpython": any(_links_libpython(path) for path in stages),
        "pcc2_pcc3_equal": _normalized_equal(stages[1], stages[2]),
        "artifact_paths": artifact_paths,
        "failure": None,
    }


def inspect_bootstrap_artifacts(
    repo_root: Path, gate_id: str
) -> list[dict[str, object]]:
    if gate_id != "self-five-gc-bootstrap":
        return []
    observations: list[dict[str, object]] = []
    for gc_backend in ("0", "1", "2", "3", "4"):
        suffix = "" if gc_backend == "0" else f"-gc{gc_backend}"
        observations.append(
            _bootstrap_observation(
                repo_root,
                backend="self",
                gc_backend=gc_backend,
                out_dir=repo_root / "build" / f"bootstrap-pytest-self{suffix}",
            )
        )
    return observations


def _five_gc_observations_are_complete(
    observations: object,
) -> bool:
    if not isinstance(observations, list) or len(observations) != 5:
        return False
    gc_backends: set[str] = set()
    for observation in observations:
        if not isinstance(observation, dict):
            return False
        gc_backend = observation.get("gc_backend")
        if not isinstance(gc_backend, str):
            return False
        expected_nodeid = _FIVE_GC_TEST_NODEIDS.get(gc_backend)
        if expected_nodeid is None:
            return False
        artifact_paths = observation.get("artifact_paths")
        expected_root = (
            "build/bootstrap-pytest-self"
            if gc_backend == "0"
            else f"build/bootstrap-pytest-self-gc{gc_backend}"
        )
        expected_paths = [f"{expected_root}/pcc{stage}" for stage in (1, 2, 3)]
        if (
            observation.get("backend") != "self"
            or observation.get("links_libpython") is not False
            or observation.get("pcc2_pcc3_equal") is not True
            or observation.get("pytest_status") != "passed"
            or observation.get("pytest_nodeid") != expected_nodeid
            or observation.get("failure") is not None
            or not isinstance(artifact_paths, list)
            or artifact_paths != expected_paths
        ):
            return False
        gc_backends.add(gc_backend)
    return gc_backends == {"0", "1", "2", "3", "4"}


def inspect_numpy_head_artifact(repo_root: Path) -> list[dict[str, object]]:
    result_path = repo_root / "build" / "head-truth" / "numpy-core" / "result.json"
    if not result_path.is_file():
        return [
            {
                "backend": "self",
                "gc_backend": None,
                "links_libpython": None,
                "pcc2_pcc3_equal": None,
                "artifact_paths": [_relative(result_path, repo_root)],
                "failure": f"missing NumPy gate result: {_relative(result_path, repo_root)}",
            }
        ]
    try:
        value = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [
            {
                "backend": "self",
                "gc_backend": None,
                "links_libpython": None,
                "pcc2_pcc3_equal": None,
                "artifact_paths": [_relative(result_path, repo_root)],
                "failure": f"invalid NumPy gate result: {exc}",
            }
        ]
    if not isinstance(value, dict):
        value = {}
    loader = value.get("loader") if isinstance(value.get("loader"), dict) else {}
    link = value.get("link") if isinstance(value.get("link"), dict) else {}
    blocker_ratchet = evaluate_result(value, "numpy-core-head")
    recorded_ratchet = value.get("first_blocker_ratchet")
    artifacts = (
        value.get("artifacts") if isinstance(value.get("artifacts"), list) else []
    )
    artifact_paths = [str(item) for item in artifacts]
    if _relative(result_path, repo_root) not in artifact_paths:
        artifact_paths.append(_relative(result_path, repo_root))
    failure: str | None = None
    if value.get("schema") != "pcc.numpy-head-gate.v1":
        failure = f"unexpected NumPy gate schema {value.get('schema')!r}"
    elif value.get("status") != PASS:
        failure = str(value.get("failure") or "NumPy gate status is not PASS")
    elif link.get("links_libpython") is not False:
        failure = "NumPy pcc-native artifact links libpython"
    elif loader.get("entered_pyinit") is not True:
        failure = "NumPy gate did not enter PyInit"
    elif loader.get("entered_py_mod_exec") is not True:
        failure = "NumPy gate did not enter Py_mod_exec"
    elif recorded_ratchet != blocker_ratchet:
        failure = "NumPy gate did not record the current first-blocker ratchet"
    elif blocker_ratchet.get("accepted") is not True:
        failure = "NumPy first-blocker ratchet rejected the gate result"
    return [
        {
            "backend": "self",
            "gc_backend": None,
            "links_libpython": link.get("links_libpython"),
            "pcc2_pcc3_equal": None,
            "artifact_paths": artifact_paths,
            "source": value.get("source"),
            "compile": value.get("compile"),
            "link": link,
            "first_blocker": loader.get("first_blocker"),
            "first_blocker_ratchet": blocker_ratchet,
            "entered_pyinit": loader.get("entered_pyinit"),
            "entered_py_mod_exec": loader.get("entered_py_mod_exec"),
            "failure": failure,
        }
    ]


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _runtime_archive_provenance_errors(
    gate: dict[str, object], observation: dict[str, object]
) -> list[str]:
    prefix = "runtime-archive-preflight: PASS provenance "
    errors: list[str] = []
    if gate.get("backend") != _RUNTIME_ARCHIVE_BACKEND:
        errors.append(prefix + "backend must be self")
    if observation.get("backend") != _RUNTIME_ARCHIVE_BACKEND:
        errors.append(prefix + "observation backend must be self")
    if observation.get("schema") != RUNTIME_ARCHIVE_PROVENANCE_SCHEMA:
        errors.append(prefix + "schema is invalid")
    if observation.get("policy") != RUNTIME_ARCHIVE_PRODUCTION_POLICY:
        errors.append(prefix + "policy is invalid")
    target_triple = observation.get("target_triple")
    if (
        not isinstance(target_triple, str)
        or not target_triple
        or target_triple != target_triple.strip()
        or any(ord(character) < 32 for character in target_triple)
    ):
        errors.append(prefix + "target_triple is invalid")
    member_count = observation.get("member_count")
    if type(member_count) is not int or member_count < 1:
        errors.append(prefix + "member_count is invalid")
    for field_name in (
        "archive_sha256",
        "manifest_sha256",
        "members_sha256",
        "capi_inventory_sha256",
    ):
        if not _is_sha256(observation.get(field_name)):
            errors.append(prefix + f"{field_name} is invalid")
    capi_symbol_count = observation.get("capi_symbol_count")
    if type(capi_symbol_count) is not int or capi_symbol_count < 1:
        errors.append(prefix + "capi_symbol_count is invalid")
    expected_modes = {
        "producer_kind": _RUNTIME_ARCHIVE_PRODUCER,
        "source_kind": _RUNTIME_ARCHIVE_SOURCE_KIND,
        "object_emitter": _RUNTIME_ARCHIVE_OBJECT_EMITTER,
    }
    for field_name, expected in expected_modes.items():
        if observation.get(field_name) != expected:
            errors.append(prefix + f"{field_name} is invalid")
    if observation.get("uses_host_cc") is not False:
        errors.append(prefix + "uses_host_cc must be false")
    expected_paths = [
        _RUNTIME_ARCHIVE.as_posix(),
        _RUNTIME_ARCHIVE.as_posix() + ".provenance.json",
        _RUNTIME_ARCHIVE.as_posix() + ".capi_syms",
    ]
    if observation.get("artifact_paths") != expected_paths:
        errors.append(prefix + "artifact_paths are invalid")
    if gate.get("artifact_paths") != expected_paths:
        errors.append(prefix + "gate artifact_paths do not match the observation")
    return errors


def inspect_runtime_archive_artifact(repo_root: Path) -> list[dict[str, object]]:
    """Verify and summarize the production archive's adjacent provenance."""

    archive = repo_root / _RUNTIME_ARCHIVE
    provenance_path = manifest_path_for_archive(archive)
    capi_inventory_path = capi_inventory_path_for_archive(archive)
    artifact_paths = [
        _relative(archive, repo_root),
        _relative(provenance_path, repo_root),
        _relative(capi_inventory_path, repo_root),
    ]
    observation: dict[str, object] = {
        "artifact_paths": artifact_paths,
        "backend": _RUNTIME_ARCHIVE_BACKEND,
        "gc_backend": None,
        "links_libpython": None,
        "pcc2_pcc3_equal": None,
        "schema": None,
        "policy": None,
        "target_triple": None,
        "member_count": None,
        "archive_sha256": None,
        "manifest_sha256": None,
        "members_sha256": None,
        "capi_symbol_count": None,
        "capi_inventory_sha256": None,
        "producer_kind": None,
        "source_kind": None,
        "object_emitter": None,
        "uses_host_cc": None,
        "failure": None,
    }
    try:
        archive_sha256 = _sha256_file(archive)
        manifest_sha256 = _sha256_file(provenance_path)
        manifest = verify_runtime_archive_manifest(
            archive,
            runtime_root=archive.parent,
        )
        # The verifier validates manifest shape/schema before consulting the
        # adjacent inventory.  Preserve that causal diagnostic ordering here:
        # a malformed manifest must not be masked by a missing sidecar.  The
        # validated manifest digest is also the stable before-state for the
        # inventory; hashing the sidecar after verification detects a change
        # during or immediately after the verifier's read.
        capi_inventory_sha256 = _sha256_file(capi_inventory_path)
        if (
            archive_sha256 != _sha256_file(archive)
            or manifest_sha256 != _sha256_file(provenance_path)
            or capi_inventory_sha256 != manifest["capi_inventory_sha256"]
        ):
            raise ProvenanceError(
                "runtime archive bundle changed during inspection"
            )
        members = manifest["members"]
        if (
            not isinstance(members, list)
            or not members
            or any(not isinstance(member, dict) for member in members)
        ):
            raise ProvenanceError("runtime archive provenance members are invalid")
        producer_kinds = {member["producer_kind"] for member in members}
        source_kinds = {member["source_kind"] for member in members}
        object_emitters = {member["object_emitter"] for member in members}
        uses_host_cc_values = {member["uses_host_cc"] for member in members}
        if any(
            len(values) != 1
            for values in (
                producer_kinds,
                source_kinds,
                object_emitters,
                uses_host_cc_values,
            )
        ):
            raise ProvenanceError(
                "runtime archive provenance members have mixed production modes"
            )
        observation.update(
            {
                "schema": manifest["schema"],
                "policy": manifest["policy"],
                "target_triple": manifest["target_triple"],
                "member_count": manifest["member_count"],
                "archive_sha256": archive_sha256,
                "manifest_sha256": manifest_sha256,
                "members_sha256": manifest["members_sha256"],
                "capi_symbol_count": manifest["capi_symbol_count"],
                "capi_inventory_sha256": capi_inventory_sha256,
                "producer_kind": next(iter(producer_kinds)),
                "source_kind": next(iter(source_kinds)),
                "object_emitter": next(iter(object_emitters)),
                "uses_host_cc": next(iter(uses_host_cc_values)),
            }
        )
    except (OSError, ProvenanceError, KeyError, TypeError) as exc:
        observation["failure"] = f"runtime archive provenance invalid: {exc}"
    return [observation]


def not_run_result(spec: GateSpec) -> GateResult:
    return GateResult(
        gate_id=spec.gate_id,
        command=list(spec.command),
        timeout_seconds=spec.timeout_seconds,
        kind=spec.kind,
        status=NOT_RUN,
        returncode=None,
        duration_seconds=None,
        log_path=None,
        output_sha256=None,
        pytest_summary=None,
        backend=spec.backend,
        gc_backend=spec.gc_backend,
    )


def run_gate(
    repo_root: Path,
    artifacts_root: Path,
    spec: GateSpec,
    process_runner: Callable[..., subprocess.CompletedProcess[str]],
) -> GateResult:
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    if spec.gate_id == "runtime-archive-preflight":
        env["PCC_BACKEND"] = _RUNTIME_ARCHIVE_BACKEND
    started = time.monotonic()
    process = process_runner(
        spec.command,
        cwd=repo_root,
        env=env,
        timeout=spec.timeout_seconds,
    )
    duration = time.monotonic() - started
    output = (process.stdout or "") + (process.stderr or "")
    artifacts_root.mkdir(parents=True, exist_ok=True)
    log_path = artifacts_root / f"{spec.gate_id}.log"
    log_path.write_text(output, encoding="utf-8")
    summary, counts = _pytest_summary(output)

    failure: str | None = None
    if process.returncode == 124:
        status = TIMEOUT
        failure = f"command timed out after {spec.timeout_seconds}s"
    elif process.returncode != 0:
        status = FAIL
        failure = f"command exited {process.returncode}"
    elif spec.kind.startswith("pytest") and summary is None:
        status = FAIL
        failure = "pytest process exited zero without a final summary"
    elif spec.kind.startswith("pytest") and (
        counts.get("passed", 0) == 0
        or counts.get("failed", 0) > 0
        or counts.get("error", 0) > 0
        or counts.get("errors", 0) > 0
        or counts.get("skipped", 0) > 0
        or counts.get("xfailed", 0) > 0
        or counts.get("xpassed", 0) > 0
    ):
        status = FAIL
        failure = f"pytest summary is not strict green: {summary}"
    elif spec.gate_id == "self-five-gc-bootstrap" and counts.get("passed", 0) < 5:
        status = FAIL
        failure = "self-five-gc-bootstrap executed fewer than five tests"
    else:
        status = PASS

    observations = inspect_bootstrap_artifacts(repo_root, spec.gate_id)
    if spec.gate_id == "runtime-archive-preflight":
        observations = inspect_runtime_archive_artifact(repo_root)
    elif spec.gate_id == "numpy-core-head":
        observations = inspect_numpy_head_artifact(repo_root)
    elif spec.gate_id == "self-five-gc-bootstrap":
        pytest_successes = _five_gc_pytest_successes(output)
        for observation in observations:
            if not isinstance(observation, dict):
                continue
            gc_backend = observation.get("gc_backend")
            nodeid = (
                pytest_successes.get(gc_backend)
                if isinstance(gc_backend, str)
                else None
            )
            observation["pytest_nodeid"] = nodeid
            observation["pytest_status"] = "passed" if nodeid is not None else None
    artifact_paths = [
        str(path)
        for observation in observations
        for path in observation.get("artifact_paths", [])
    ]
    observation_failure: str | None = None
    if spec.gate_id == "runtime-archive-preflight":
        if len(observations) != 1:
            observation_failure = (
                "runtime-archive-preflight requires exactly one provenance observation"
            )
        else:
            observation = observations[0]
            if observation.get("failure"):
                observation_failure = str(observation["failure"])
            else:
                provenance_errors = _runtime_archive_provenance_errors(
                    {
                        "backend": spec.backend,
                        "artifact_paths": artifact_paths,
                    },
                    observation,
                )
                if provenance_errors:
                    observation_failure = provenance_errors[0]
    elif spec.gate_id == "self-five-gc-bootstrap":
        if not _five_gc_observations_are_complete(observations):
            observation_failure = (
                "self-five-gc-bootstrap requires one current pytest PASS and one "
                "valid artifact observation for each GC backend 0..4"
            )
    elif observations:
        observation_failure = next(
            (
                str(observation.get("failure"))
                for observation in observations
                if observation.get("failure")
            ),
            None,
        )
    # Artifact inspection is the second half of a successful gate.  It may
    # demote an otherwise-green command, but it must not hide the earlier,
    # causal process/pytest failure (timeout, non-zero exit, partial summary,
    # skips, or too few executed tests).
    if observation_failure is not None and status == PASS:
        status = FAIL
        failure = observation_failure or "artifact inspection failed"

    links_libpython_values = [
        observation.get("links_libpython") for observation in observations
    ]
    links_libpython = (
        any(bool(value) for value in links_libpython_values)
        if links_libpython_values
        and all(type(value) is bool for value in links_libpython_values)
        else None
    )
    pcc2_pcc3_equal = (
        all(observation["pcc2_pcc3_equal"] is True for observation in observations)
        if observations and spec.gate_id == "self-five-gc-bootstrap"
        else None
    )
    return GateResult(
        gate_id=spec.gate_id,
        command=list(spec.command),
        timeout_seconds=spec.timeout_seconds,
        kind=spec.kind,
        status=status,
        returncode=process.returncode,
        duration_seconds=round(duration, 3),
        log_path=_relative(log_path, repo_root),
        output_sha256=hashlib.sha256(output.encode("utf-8")).hexdigest(),
        pytest_summary=summary,
        backend=spec.backend,
        gc_backend=spec.gc_backend,
        links_libpython=links_libpython,
        pcc2_pcc3_equal=pcc2_pcc3_equal,
        artifact_paths=artifact_paths,
        observations=observations,
        failure=failure,
    )


def build_manifest(
    *,
    source: dict[str, object],
    platform_info: dict[str, str],
    results: Sequence[GateResult],
    generated_at: str | None = None,
) -> dict[str, object]:
    result_by_id = {result.gate_id: result for result in results}
    complete = all(
        result_by_id.get(gate_id) is not None and result_by_id[gate_id].status == PASS
        for gate_id in REQUIRED_GATE_IDS
    )
    return {
        "schema": SCHEMA,
        "generated_at": generated_at
        or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": source,
        "platform": platform_info,
        "required_gate_ids": list(REQUIRED_GATE_IDS),
        "complete": complete,
        "claimable_commit": complete and not bool(source.get("worktree_dirty")),
        "gates": [asdict(result) for result in results],
    }


def validate_manifest(
    manifest: dict[str, object], *, require_complete: bool = False
) -> list[str]:
    errors: list[str] = []
    if manifest.get("schema") != SCHEMA:
        errors.append(f"unexpected schema {manifest.get('schema')!r}")
    source = manifest.get("source")
    if not isinstance(source, dict):
        errors.append("source must be an object")
    else:
        commit = source.get("commit")
        if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
            errors.append("source.commit must be a full lowercase Git SHA")
        if not isinstance(source.get("worktree_dirty"), bool):
            errors.append("source.worktree_dirty must be boolean")
        fingerprint = source.get("worktree_fingerprint")
        if (
            not isinstance(fingerprint, str)
            or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
        ):
            errors.append("source.worktree_fingerprint must be SHA-256")
    platform_info = manifest.get("platform")
    if not isinstance(platform_info, dict) or any(
        not isinstance(platform_info.get(key), str)
        for key in ("system", "release", "machine", "python")
    ):
        errors.append("platform must record system, release, machine, and python")
    gates = manifest.get("gates")
    if not isinstance(gates, list):
        errors.append("gates must be a list")
        gates = []
    registered_specs = {spec.gate_id: spec for spec in gate_specs(Path("."))}
    gate_by_id: dict[str, dict[str, object]] = {}
    for index, gate in enumerate(gates):
        if not isinstance(gate, dict):
            errors.append(f"gate #{index + 1} must be an object")
            continue
        gate_id = gate.get("gate_id")
        if not isinstance(gate_id, str) or not gate_id:
            errors.append(f"gate #{index + 1} has no gate_id")
            continue
        if gate_id in gate_by_id:
            errors.append(f"duplicate gate {gate_id}")
        gate_by_id[gate_id] = gate
        registered = registered_specs.get(gate_id)
        if registered is not None:
            if gate.get("command") != list(registered.command):
                errors.append(f"{gate_id}: command differs from registry")
            if gate.get("timeout_seconds") != registered.timeout_seconds:
                errors.append(f"{gate_id}: timeout differs from registry")
            if gate.get("kind") != registered.kind:
                errors.append(f"{gate_id}: kind differs from registry")
            if gate.get("backend") != registered.backend:
                errors.append(f"{gate_id}: backend differs from registry")
            if gate.get("gc_backend") != registered.gc_backend:
                errors.append(f"{gate_id}: gc_backend differs from registry")
        status = gate.get("status")
        if status not in VALID_GATE_STATUSES:
            errors.append(f"{gate_id}: invalid status {status!r}")
        if status == PASS:
            if gate.get("returncode") != 0:
                errors.append(f"{gate_id}: PASS requires returncode 0")
            if not isinstance(gate.get("duration_seconds"), (int, float)):
                errors.append(f"{gate_id}: PASS requires duration_seconds")
            if str(gate.get("kind", "")).startswith("pytest"):
                pytest_summary = gate.get("pytest_summary")
                if not isinstance(pytest_summary, str) or not pytest_summary:
                    errors.append(f"{gate_id}: pytest PASS requires a final summary")
                else:
                    parsed_summary, counts = _pytest_summary(pytest_summary)
                    if parsed_summary is None or (
                        counts.get("passed", 0) == 0
                        or counts.get("failed", 0) > 0
                        or counts.get("error", 0) > 0
                        or counts.get("errors", 0) > 0
                        or counts.get("skipped", 0) > 0
                        or counts.get("xfailed", 0) > 0
                        or counts.get("xpassed", 0) > 0
                    ):
                        errors.append(
                            f"{gate_id}: pytest PASS requires a strict green summary"
                        )
                    if (
                        gate_id == "self-five-gc-bootstrap"
                        and counts.get("passed", 0) < 5
                    ):
                        errors.append(
                            "self-five-gc-bootstrap: PASS requires at least five "
                            "passed tests"
                        )
            if gate_id == "self-five-gc-bootstrap":
                if gate.get("links_libpython") is not False:
                    errors.append(f"{gate_id}: PASS requires links_libpython=false")
                if gate.get("pcc2_pcc3_equal") is not True:
                    errors.append(f"{gate_id}: PASS requires pcc2_pcc3_equal=true")
            if gate_id == "self-five-gc-bootstrap" and not (
                _five_gc_observations_are_complete(gate.get("observations"))
            ):
                errors.append(
                    "self-five-gc-bootstrap: PASS requires one current pytest PASS "
                    "and one valid artifact observation for each GC backend 0..4"
                )
            if (
                gate_id == "numpy-core-head"
                and gate.get("links_libpython") is not False
            ):
                errors.append("numpy-core-head: PASS requires links_libpython=false")
            if gate_id == "runtime-archive-preflight":
                observations = gate.get("observations")
                if not isinstance(observations, list) or len(observations) != 1:
                    errors.append(
                        "runtime-archive-preflight: PASS requires exactly one "
                        "provenance observation"
                    )
                elif (
                    not isinstance(observations[0], dict)
                    or observations[0].get("failure") is not None
                ):
                    errors.append(
                        "runtime-archive-preflight: PASS requires a valid "
                        "provenance observation"
                    )
                else:
                    errors.extend(
                        _runtime_archive_provenance_errors(gate, observations[0])
                    )
            if gate_id == "numpy-core-head":
                observations = gate.get("observations")
                if not isinstance(observations, list) or len(observations) != 1:
                    errors.append(
                        "numpy-core-head: PASS requires exactly one artifact observation"
                    )
                else:
                    observation = observations[0]
                    blocker = (
                        observation.get("first_blocker")
                        if isinstance(observation, dict)
                        else None
                    )
                    ratchet = (
                        observation.get("first_blocker_ratchet")
                        if isinstance(observation, dict)
                        else None
                    )
                    if (
                        not isinstance(observation, dict)
                        or "first_blocker" not in observation
                        or (
                            blocker is not None
                            and (
                                not isinstance(blocker, dict)
                                or blocker.get("kind") not in ALLOWED_KINDS
                            )
                        )
                    ):
                        errors.append(
                            "numpy-core-head: PASS requires a classified first blocker "
                            "or an explicit empty completion record"
                        )
                    if (
                        not isinstance(ratchet, dict)
                        or ratchet.get("accepted") is not True
                    ):
                        errors.append(
                            "numpy-core-head: PASS requires an accepted first-blocker ratchet"
                        )
    if require_complete:
        for gate_id in REQUIRED_GATE_IDS:
            gate = gate_by_id.get(gate_id)
            if gate is None or gate.get("status") != PASS:
                errors.append(f"required gate {gate_id} is not PASS")
        if manifest.get("complete") is not True:
            errors.append("complete manifest required")
    return errors


def serialize_manifest(manifest: dict[str, object]) -> str:
    return json.dumps(manifest, indent=2, sort_keys=True) + "\n"


def load_manifest(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("manifest root must be an object")
    return value


def write_manifest(path: Path, manifest: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialize_manifest(manifest), encoding="utf-8")


def not_run_results(repo_root: Path) -> list[GateResult]:
    return [not_run_result(spec) for spec in gate_specs(repo_root)]


def selected_gate_failures(
    results: Sequence[GateResult], selected_gate_ids: set[str]
) -> list[str]:
    return [
        f"selected gate {result.gate_id} is {result.status}"
        for result in results
        if result.gate_id in selected_gate_ids and result.status != PASS
    ]
