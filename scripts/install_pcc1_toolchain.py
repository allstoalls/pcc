#!/usr/bin/env python3
"""Prepare pcc1 toolchains and manage qualified promotion or verified rollback.

Initial installation never replaces a command. Promotion requires independently
recorded, source-bound gate evidence and does not qualify a package release.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import shutil
import signal
import sys
import tempfile
import tomllib
import uuid

REPO = Path(__file__).resolve().parents[1]
QUALIFICATION_GATES = frozenset(
    {"default", "integration", "bootstrap", "installed_packages"}
)
QUALIFICATION_PROTOCOL = """\
Qualification protocol (toolchain selection only; no package release claim):
  Prepare with --stage-only. Run existing pytest + process-tree tools from a
  frozen checkout matching CANDIDATE/evidence/source.json. For both collection
  and execution, export PCC_VALIDATION_SOURCE_MANIFEST to that absolute file,
  PCC_VALIDATION_INSTALLATION_SHA256 to the SHA256 of installation.json,
  PCC1_BINARY to CANDIDATE/bin/pcc1 or CANDIDATE/libexec/pcc1,
  PCC_SOURCE_ROOT and PCC_REPO_ROOT to CANDIDATE/source, and
  PCC_RUNTIME_ARCHIVE to CANDIDATE/runtime/libpy_runtime_pcc_py.a.
  Use -x and -p scripts.pytest_live_report --pcc-live-report ABSOLUTE_PATH.
  The reporter must record effective selection, including cache/stepwise and
  project-configuration fields, plus actual pytest-side validation_environment.
  Older reports missing these fields cannot qualify an installation.
  Collect with --collect-only -n0. default uses -m 'not integration'; the other
  gates use -m integration. default/integration collections cover the full
  tests root using its frozen pyproject.toml, without ini overrides, cache-based
  selection, pyargs or conftest suppression. Executions may be sharded;
  all collected nodes need final reports.

  --record-gate REQUEST.json --output GATE.json hashes existing artifacts.
  REQUEST has: candidate (absolute directory), gate (one of default,
  integration, bootstrap, installed_packages), collection ({process: absolute
  process-tree result JSON, pytest: absolute pytest JSONL}), runs (a nonempty
  list of the same pairs), must_pass (nonempty exact release-canary nodeids),
  and allowed_skips (optional map: nodeid -> {reason: exact longrepr,
  kind: 'skip' or 'xfail'}). Required canaries cannot be skipped. Exceptions
  must be explicitly reviewed; skipped surfaces are not execution proof.

  --qualify CANDIDATE --gate default=GATE.json --gate integration=GATE.json
    --gate bootstrap=GATE.json --gate installed_packages=GATE.json
    --output QUALIFICATION.json verifies all source/installation/report hashes.
  --promote CANDIDATE --qualification QUALIFICATION.json atomically replaces
  only a verified managed symlink under --bin-dir, using versions under --root.
  --rollback TRANSACTION restores that promotion's unchanged predecessor.
  .pcc1-activation/*.prepare.json records intent before replacement;
  *.commit.json records completion. Rollback also handles a missing commit
  after a crash by checking the actual symlink and both recorded identities.
"""


def bootstrap_tools():
    # The existing standalone tools import one another by their script names.
    # Load that exact directory, including when this module is imported by pytest.
    sys.path.insert(0, str(REPO / "scripts"))
    try:
        import run_pcc_compile_ab
        import run_process_tree_sample
    finally:
        sys.path.pop(0)
    return run_pcc_compile_ab, run_process_tree_sample


def run_logged(
    evidence: Path,
    name: str,
    command: list[str],
    timeout: float = 60,
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    performance_lock: bool = False,
    max_tree_rss_bytes: int = 0,
):
    """One watchdog owns the child tree and writes output before waiting."""
    _, sampler = bootstrap_tools()
    arguments = sampler._parser().parse_args(
        [
            "--result",
            str(evidence / (name + ".result.json")),
            "--samples",
            str(evidence / (name + ".rss.tsv")),
            "--stdout",
            str(evidence / (name + ".stdout")),
            "--stderr",
            str(evidence / (name + ".stderr")),
            "--cwd",
            str(cwd or evidence),
            "--timeout",
            str(timeout),
            "--max-tree-rss-bytes",
            str(max_tree_rss_bytes),
            "--performance-lock" if performance_lock else "--no-performance-lock",
            "--",
            *command,
        ]
    )
    print(f"[{name}] timeout={timeout}s artifacts={evidence}", flush=True)
    original_environment = os.environ.copy()
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    sampler._INTERRUPT_REQUESTED = False
    signal.signal(signal.SIGTERM, sampler._request_interrupt)
    try:
        if env is not None:
            os.environ.clear()
            os.environ.update(env)
        # In-process use removes the outer subprocess timeout that could kill
        # the sampler while its compiler lived in another process group.
        result = sampler.run(arguments)
    finally:
        os.environ.clear()
        os.environ.update(original_environment)
        signal.signal(signal.SIGTERM, previous_sigterm)
    if result.get("status") != "COMPLETE" or result.get("returncode") != 0:
        error = (evidence / (name + ".stderr")).read_text()[-5000:]
        raise RuntimeError(
            f"{name} failed ({result.get('status')}, {result.get('returncode')}); "
            f"artifacts: {evidence}\n{error}"
        )
    return (evidence / (name + ".stdout")).read_text()


def prepare_runtime_source(frozen: Path, work: Path) -> Path:
    from tests.runtime_build_cache import _make_runtime_staging_writable

    # The frontend recognizes isolated runtime ports under py_runtime/py.
    # Keeping this directory contract preserves its static ABI exports and
    # freestanding ownership policy when compiling a copied runtime.
    runtime = work / "py_runtime"
    shutil.copytree(frozen / "pcc/runtime", runtime)
    _make_runtime_staging_writable(runtime)
    return runtime


def build_from_source(source_root: Path):
    """Use the existing frozen bootstrap runners for a clean first install."""
    sys.path.insert(0, str(REPO))
    from scripts import run_pcc_stage1_build as stage1

    source_root = source_root.resolve(strict=True)
    metadata = tomllib.loads((source_root / "pyproject.toml").read_text())
    cache = Path.home() / ".cache/pcc/installations"
    cache.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="source-", dir=cache))
    ab = stage1._load_ab_tool()
    manifest = stage1.source_manifest(source_root, ab)
    frozen = work / "source"
    stage1._snapshot_sources(source_root, manifest, frozen, ab)
    (work / "source-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    runtime = prepare_runtime_source(frozen, work)
    # Match the measured host Stage1 envelope (7 workers at 16 GiB on M2).
    # An 8-GiB admission budget resolves to 3 workers and measured 324s of
    # frontend work plus 52s of linking, beyond the old 360s watchdog.
    host_budget = min(16 * 1024**3, stage1._host_memory_budget_bytes(0) or 8 * 1024**3)
    compiler = work / "host-pcc"
    compiler.write_text(
        "#!/bin/sh\nexport PYTHONPATH="
        + shlex.quote(str(frozen))
        + "\nexec "
        + shlex.quote(sys.executable)
        + ' -m pcc "$@"\n'
    )
    compiler.chmod(0o755)

    def run(name, command, timeout, *, performance_lock):
        env = dict(os.environ, PYTHONPATH=str(frozen))
        env.pop("LC_ALL", None)
        run_logged(
            work,
            name,
            command,
            timeout,
            cwd=frozen,
            env=env,
            performance_lock=performance_lock,
            max_tree_rss_bytes=8589934592 if name == "stage1" else 0,
        )

    run(
        "runtime",
        [
            "make",
            "-j6",
            "-C",
            str(runtime),
            "PCC=" + str(compiler),
            "PYTHON=" + sys.executable,
            "PCC_REPO_ROOT=" + str(frozen),
            "libpy_runtime_pcc_py.a",
        ],
        600,
        performance_lock=True,
    )
    first = work / "stage1"
    run(
        "stage1",
        [
            sys.executable,
            str(REPO / "scripts/run_pcc_stage1_build.py"),
            "--arm",
            "candidate",
            "--source-root",
            str(frozen),
            "--runtime-archive",
            str(runtime / "libpy_runtime_pcc_py.a"),
            "--output-dir",
            str(first),
            "--timeout",
            "480",
            "--smoke-timeout",
            "60",
            "--memory-budget-bytes",
            str(host_budget),
            "--self-backend-jobs",
            "2",
            "--direct-indexed-emit",
        ],
        600,
        performance_lock=False,
    )
    second = work / "stage2"
    run(
        "stage2",
        [
            sys.executable,
            str(REPO / "scripts/run_pcc_stage2_from_receipt.py"),
            "--stage1-dir",
            str(first),
            "--output-dir",
            str(second),
            "--stage2-timeout",
            "600",
            "--smoke-timeout",
            "60",
            "--self-backend-jobs",
            "2",
        ],
        720,
        performance_lock=False,
    )
    return first, second, "source-" + metadata["project"]["version"]


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text())


def receipt_child(root: Path, name: str) -> Path:
    relative = PurePosixPath(name)
    if (
        not relative.parts
        or name != relative.as_posix()
        or relative.is_absolute()
        or ".." in relative.parts
    ):
        raise ValueError(f"unsafe receipt path: {name}")
    path = root
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError(f"receipt path is a symlink: {path}")
    return path


def verify_bootstrap(first: Path, second: Path):
    """Validate the retained stage evidence before copying or exposing files."""
    ab, _ = bootstrap_tools()
    build_path = first / "build-receipt.json"
    raw_build = read_json(build_path)
    compiler = first / "pcc1"
    runtime = first / "runtime-bundle/libpy_runtime_pcc_py.a"
    validated = ab._load_build_receipt(
        build_path,
        arm=raw_build.get("arm"),
        compiler_sha256=digest(compiler),
        compiler_size_bytes=compiler.stat().st_size,
        runtime_sha256=digest(runtime),
    )
    build = validated["receipt"]
    source = Path(validated["source_snapshot_root"])
    runtime_files = build["runtime_bundle"]["files"]
    runtime_root = runtime.parent
    actual_files = {
        path.relative_to(runtime_root).as_posix()
        for path in runtime_root.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    if actual_files != set(runtime_files):
        raise ValueError("runtime bundle file set differs from build receipt")
    for name, item in runtime_files.items():
        path = receipt_child(runtime_root, name)
        ab.verify_portable_file_receipt(
            {**item, "path": str(path)}, "installed runtime input " + name
        )

    manifest_path = second / "manifest.json"
    stage2 = read_json(manifest_path)
    if (
        stage2.get("schema") != "pcc.stage2-from-stage1-receipt.v1"
        or stage2.get("status") != "COMPLETE"
    ):
        raise ValueError("Stage2 did not succeed")
    if Path(stage2["stage1_dir"]).resolve() != first:
        raise ValueError("Stage2 does not belong to this Stage1")
    record_path = receipt_child(second, stage2["stage2_record"])
    record = read_json(record_path)
    output = second / "stage2/pcc2"
    compiler_hash = digest(output)
    if (
        digest(second / "stage2/pcc1") != build["compiler_sha256"]
        or compiler_hash != record.get("compiler_sha256")
        or compiler_hash != stage2.get("compiler_sha256")
        or record.get("compiler") != str(output)
    ):
        raise ValueError("Stage2 compiler identity mismatch")
    result_path = second / "stage2/profile/stage2.result.json"
    result = read_json(result_path)
    if (
        record.get("result_path") != str(result_path)
        or record.get("result") != result
        or result.get("schema") != "pcc.bootstrap_stage_result.v1"
        or result.get("stage") != 2
        or result.get("backend") != "self"
        or result.get("returncode") != 0
        or result.get("publish_barrier_returncode") != 0
        or result.get("output") != str(output)
    ):
        raise ValueError("Stage2 result does not bind its successful output")
    process_path = second / "stage2-process.result.json"
    process = read_json(process_path)
    expected_command = [
        sys.executable,
        str(source / "scripts/bootstrap.py"),
        "--out-dir",
        str(second / "stage2"),
        "--backend",
        "self",
        "--from-stage",
        "2",
        "--stage",
        "2",
        "--reuse-stage1",
    ]
    if (
        record.get("process") != process
        or process.get("status") != "COMPLETE"
        or process.get("returncode") != 0
        or process.get("cwd") != str(source)
        or process.get("command") != expected_command
    ):
        raise ValueError("Stage2 process does not bind the verified source snapshot")
    environment = process.get("environment", {})
    for key in (
        "PCC_SOURCE_ROOT",
        "PCC_REPO_ROOT",
        "PCC_RUNTIME_ARCHIVE",
        "PCC_RUNTIME_HIGH",
        "PCC_GC_BACKEND",
        "PCC_SELF_LINK",
    ):
        if environment.get(key) != build["environment"].get(key):
            raise ValueError("Stage2 changed the receipt-bound " + key)
    if Path(environment["PCC_RUNTIME_ARCHIVE"]).resolve() != runtime:
        raise ValueError("Stage2 runtime is outside the verified bundle")
    linkage = record.get("linkage", {})
    if (
        linkage.get("checked") is not True
        or linkage.get("links_libpython") is not False
        or linkage.get("links_llvm") is not False
    ):
        raise ValueError("bootstrap linkage gate is not strict self/no-libpython")
    evidence = {
        "stage1": (build_path, validated["sha256"]),
        "stage1-result": (
            Path(validated["stage_result_path"]),
            build["stage_result_sha256"],
        ),
        "source": (
            Path(validated["source_manifest_path"]),
            build["source_manifest_sha256"],
        ),
    }
    for label, path in (
        ("stage2", manifest_path),
        ("stage2-record", record_path),
        ("stage2-result", result_path),
        ("stage2-process", process_path),
    ):
        evidence[label] = (path, digest(path))
    return validated, evidence


def checked_copy(source: Path, target: Path, expected: str):
    if source.is_symlink() or digest(source) != expected:
        raise ValueError(f"source identity mismatch: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as src, target.open("xb") as dst:
        shutil.copyfileobj(src, dst)
    if digest(target) != expected:
        raise ValueError(f"copy identity mismatch: {target}")
    target.chmod(0o444)


def verify_installation(root: Path):
    receipt = read_json(root / "installation.json")
    if receipt.get("status") != "VERIFIED_BASELINE":
        raise ValueError("candidate has not passed installed execution checks")
    for name, expected in receipt["files"].items():
        path = root / name
        if path.is_symlink() or digest(path) != expected:
            raise ValueError(f"installed identity mismatch: {path}")
    return receipt


def installation_identity(root: Path):
    receipt = verify_installation(root)
    return {
        "root": str(root),
        "receipt_sha256": digest(root / "installation.json"),
        "compiler_sha256": receipt.get("compiler_sha256"),
        "source_manifest_sha256": receipt["files"].get("evidence/source.json"),
    }


def managed_identity(root: Path, store: Path):
    root = root.expanduser().absolute()
    if root.is_symlink() or root.resolve().parent != store.resolve():
        raise ValueError("toolchain is outside the managed version store")
    root = root.resolve(strict=True)
    receipt = verify_installation(root)
    required = {
        "bin/pcc1",
        "libexec/pcc1",
        "runtime/libpy_runtime_pcc_py.a",
        "evidence/source.json",
    }
    if (
        receipt.get("schema") != "pcc.installed-baseline.v1"
        or not required.issubset(receipt["files"])
        or receipt.get("compiler_sha256") != receipt["files"]["libexec/pcc1"]
        or not os.access(root / "bin/pcc1", os.X_OK)
        or not os.access(root / "libexec/pcc1", os.X_OK)
    ):
        raise ValueError("toolchain has no managed installation receipt")
    for name in receipt["files"]:
        receipt_child(root, name)
    return installation_identity(root)


def checked_reference(reference: dict):
    if set(reference) != {"path", "sha256"}:
        raise ValueError("evidence reference needs exactly path and sha256")
    path = Path(reference["path"])
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not path.is_file()
        or digest(path) != reference["sha256"]
    ):
        raise ValueError("qualification evidence identity mismatch: " + str(path))
    return path


def verify_qualification(
    root: Path, qualification_path: Path | None, identity: dict, *, payload=None
):
    """Check required collections against complete, hashed execution reports."""
    qualification_hash = digest(qualification_path) if qualification_path else None
    qualification = read_json(qualification_path) if qualification_path else payload
    if (
        qualification.get("schema") != "pcc.toolchain-qualification.v1"
        or qualification.get("installation_sha256") != identity["receipt_sha256"]
        or qualification.get("source_manifest_sha256")
        != identity["source_manifest_sha256"]
        or set(qualification.get("gates", {})) != QUALIFICATION_GATES
    ):
        raise ValueError(
            "qualification does not bind the installation and all required gates"
        )
    ab, _ = bootstrap_tools()
    source_manifest = ab._load_source_manifest(root / "evidence/source.json")
    checked_sources = set()
    for name, reference in qualification["gates"].items():
        seen_runs = set()
        gate = read_json(checked_reference(reference))
        must_pass = gate.get("must_pass", [])
        allowed_skips = gate.get("allowed_skips", {})
        if (
            gate.get("schema") != "pcc.toolchain-gate.v1"
            or gate.get("gate") != name
            or gate.get("installation_sha256") != identity["receipt_sha256"]
            or gate.get("source_manifest_sha256") != identity["source_manifest_sha256"]
            or not isinstance(must_pass, list)
            or not must_pass
            or any(not isinstance(node, str) or "::" not in node for node in must_pass)
            or len(set(must_pass)) != len(must_pass)
            or not isinstance(allowed_skips, dict)
            or not isinstance(gate.get("collection"), dict)
            or not isinstance(gate.get("runs"), list)
            or not gate["runs"]
        ):
            raise ValueError("invalid or mismatched qualification gate: " + name)
        passed = set()
        skipped = {}
        required = set()
        for run_index, run in enumerate([gate["collection"], *gate["runs"]]):
            process_path = checked_reference(run["process"])
            report_path = checked_reference(run["pytest"])
            if str(process_path) in seen_runs:
                raise ValueError("qualification reuses a process receipt")
            seen_runs.add(str(process_path))
            process = read_json(process_path)
            rows = [
                json.loads(line)
                for line in report_path.read_text().splitlines()
                if line
            ]
            if (
                process.get("schema") != "pcc.process_tree_sample.v1"
                or process.get("status") != "COMPLETE"
                or type(process.get("returncode")) is not int
                or process["returncode"] != 0
                or len(rows) < 3
                or rows[0].get("event") != "start"
                or rows[-1].get("event") != "finish"
                or sum(row.get("event") == "start" for row in rows) != 1
                or sum(row.get("event") == "finish" for row in rows) != 1
                or rows[-1].get("exitstatus") != 0
                or rows[-1].get("testsfailed") != 0
                or type(rows[-1].get("testscollected")) is not int
                or rows[-1]["testscollected"] <= 0
                or any(row.get("outcome") == "failed" for row in rows)
            ):
                raise ValueError(
                    "qualification has incomplete or failed execution: " + name
                )
            start = rows[0]
            environment = process.get("environment", {})
            manifest_path = Path(start.get("source_manifest", ""))
            if (
                not manifest_path.is_absolute()
                or not manifest_path.is_file()
                or digest(manifest_path) != identity["source_manifest_sha256"]
                or environment.get("PCC_VALIDATION_SOURCE_MANIFEST")
                != str(manifest_path)
                or environment.get("PCC_VALIDATION_INSTALLATION_SHA256")
                != identity["receipt_sha256"]
                or environment.get("PCC_SOURCE_ROOT") != str(root / "source")
                or environment.get("PCC_REPO_ROOT") != str(root / "source")
                or environment.get("PCC_RUNTIME_ARCHIVE")
                != str(root / "runtime/libpy_runtime_pcc_py.a")
                or start.get("root") != process.get("cwd")
            ):
                raise ValueError(
                    "qualification execution used a different source or installation"
                )
            actual_environment = start.get("validation_environment")
            binding_keys = (
                "PCC_VALIDATION_SOURCE_MANIFEST",
                "PCC_VALIDATION_INSTALLATION_SHA256",
                "PCC1_BINARY",
                "PCC_SOURCE_ROOT",
                "PCC_REPO_ROOT",
                "PCC_RUNTIME_ARCHIVE",
            )
            if (
                not isinstance(actual_environment, dict)
                or not set(binding_keys).issubset(actual_environment)
                or actual_environment.get("PCC1_BINARY")
                not in {
                    str(root / "bin/pcc1"),
                    str(root / "libexec/pcc1"),
                }
                or any(
                    actual_environment[key] != environment.get(key)
                    for key in binding_keys
                    if key != "PCC1_BINARY"
                )
                # The sampler's PCC_* filter does not normally include PCC1_.
                # The actual pytest-side compiler selection is authoritative.
                or (
                    "PCC1_BINARY" in environment
                    and actual_environment["PCC1_BINARY"] != environment["PCC1_BINARY"]
                )
            ):
                raise ValueError(
                    "actual pytest environment differs from the qualified invocation"
                )
            selection_keys = {
                "keyword",
                "ignore",
                "ignore_glob",
                "deselect",
                "collect_only",
                "lf",
                "stepwise",
                "stepwise_skip",
                "inifile",
                "override_ini",
                "pyargs",
                "noconftest",
                "confcutdir",
            }
            if (
                not selection_keys.issubset(start)
                or any(
                    not isinstance(start[key], str)
                    for key in ("keyword", "inifile", "confcutdir")
                )
                or any(
                    type(start[key]) is not bool
                    for key in (
                        "collect_only",
                        "lf",
                        "stepwise",
                        "stepwise_skip",
                        "pyargs",
                        "noconftest",
                    )
                )
                or start["collect_only"] != (run_index == 0)
                or any(
                    start[key] is not None
                    and (
                        not isinstance(start[key], list)
                        or any(not isinstance(value, str) for value in start[key])
                    )
                    for key in ("ignore", "ignore_glob", "deselect", "override_ini")
                )
            ):
                raise ValueError(
                    "qualification is missing valid effective pytest selection fields"
                )
            source_root = Path(process["cwd"])
            if str(source_root) not in checked_sources:
                # A qualification checkout also contains tests and reports.
                # Verify the exact compiler build closure, not the bootstrap
                # snapshot's stricter "no other directories" storage layout.
                actual_sources = {
                    path.relative_to(source_root).as_posix(): ab.sha256_path(path)
                    for path in ab.build_source_files(source_root)
                }
                if actual_sources != source_manifest["files"]:
                    raise ValueError(
                        "qualification source differs from the installed build closure"
                    )
                checked_sources.add(str(source_root))
            command = process.get("command", [])
            pytest_positions = [
                index
                for index, part in enumerate(command)
                if isinstance(part, str) and Path(part).name == "pytest"
            ]
            arguments = start.get("argv", [])
            if (
                len(pytest_positions) != 1
                or command[pytest_positions[0] + 1 :] != arguments
                or not any(flag in arguments for flag in ("-x", "--maxfail=1"))
            ):
                raise ValueError("qualification process/report commands disagree")
            expected_mark = "not integration" if name == "default" else "integration"
            if start.get("markexpr") != expected_mark:
                raise ValueError(
                    "qualification gate selected a different test mode: " + name
                )
            if run_index == 0:
                collected = [row for row in rows if row.get("event") == "collected"]
                if (
                    len(collected) != 1
                    or not isinstance(collected[0].get("nodeids"), list)
                    or len(set(collected[0]["nodeids"])) != rows[-1]["testscollected"]
                    or any(
                        not isinstance(node, str) or "::" not in node
                        for node in collected[0]["nodeids"]
                    )
                ):
                    raise ValueError(
                        "qualification needs a complete collect-only receipt"
                    )
                required = set(collected[0]["nodeids"])
                if name in {"default", "integration"} and (
                    "tests" not in arguments
                    or any(
                        start[key]
                        for key in (
                            "keyword",
                            "ignore",
                            "ignore_glob",
                            "deselect",
                            "lf",
                            "stepwise",
                            "stepwise_skip",
                            "override_ini",
                            "pyargs",
                            "noconftest",
                            "confcutdir",
                        )
                    )
                    or start["inifile"] != str(source_root / "pyproject.toml")
                    or any(arg.startswith("tests/") or "::" in arg for arg in arguments)
                    or any(
                        arg == "-k"
                        or arg.startswith(
                            ("--ignore", "--deselect", "--lf", "--last-failed")
                        )
                        for arg in arguments
                    )
                ):
                    raise ValueError(
                        "suite collection must cover the complete tests root"
                    )
                if not set(must_pass).issubset(required) or not set(
                    allowed_skips
                ).issubset(required):
                    raise ValueError(
                        "qualification exceptions/canaries are outside the collection"
                    )
                if set(must_pass) & set(allowed_skips):
                    raise ValueError("required release canaries cannot be skipped")
                continue
            completed = {
                row["nodeid"]
                for row in rows
                if row.get("event") == "report"
                and (
                    row.get("when") == "call"
                    or (row.get("when") == "setup" and row.get("outcome") == "skipped")
                )
            }
            if len(completed) != rows[-1]["testscollected"]:
                raise ValueError("qualification is missing collected node reports")
            passed.update(
                row["nodeid"]
                for row in rows
                if row.get("event") == "report"
                and row.get("when") == "call"
                and row.get("outcome") == "passed"
                and "wasxfail" not in row
            )
            for row in rows:
                if row.get("event") == "report" and row.get("outcome") == "skipped":
                    actual = {
                        "reason": row.get("longrepr"),
                        "kind": "xfail" if "wasxfail" in row else "skip",
                    }
                    if allowed_skips.get(row["nodeid"]) != actual:
                        raise ValueError(
                            "unreviewed skip or xfail in qualification: "
                            + row["nodeid"]
                        )
                    skipped[row["nodeid"]] = actual
        if not required.issubset(passed | set(skipped)) or not set(must_pass).issubset(
            passed
        ):
            raise ValueError("qualification did not pass every required node: " + name)
    if qualification_path and digest(qualification_path) != qualification_hash:
        raise ValueError("qualification changed during verification")
    return {"path": str(qualification_path), "sha256": qualification_hash}


def file_reference(path: Path):
    path = path.expanduser().resolve(strict=True)
    return {"path": str(path), "sha256": digest(path)}


def record_gate(request: dict, output: Path, store: Path):
    """Hash existing collection/execution artifacts; never run a second executor."""
    identity = managed_identity(Path(request["candidate"]), store)
    if request["gate"] not in QUALIFICATION_GATES:
        raise ValueError("unknown qualification gate")

    def run_references(run):
        return {name: file_reference(Path(run[name])) for name in ("process", "pytest")}

    gate = {
        "schema": "pcc.toolchain-gate.v1",
        "gate": request["gate"],
        "installation_sha256": identity["receipt_sha256"],
        "source_manifest_sha256": identity["source_manifest_sha256"],
        "collection": run_references(request["collection"]),
        "runs": [run_references(run) for run in request["runs"]],
        "must_pass": request["must_pass"],
        "allowed_skips": request.get("allowed_skips", {}),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    write_history(output, gate)
    return {"gate": gate["gate"], "receipt": str(output)}


def qualify(root: Path, gates: dict[str, Path], output: Path, store: Path):
    identity = managed_identity(root, store)
    payload = {
        "schema": "pcc.toolchain-qualification.v1",
        "installation_sha256": identity["receipt_sha256"],
        "source_manifest_sha256": identity["source_manifest_sha256"],
        "gates": {name: file_reference(path) for name, path in gates.items()},
    }
    verify_qualification(Path(identity["root"]), None, identity, payload=payload)
    output.parent.mkdir(parents=True, exist_ok=True)
    write_history(output, payload)
    return {"qualification": str(output), "installation": identity["root"]}


def sync_directory(path: Path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_history(path: Path, payload: dict):
    with path.open("x") as stream:
        stream.write(json.dumps(payload, sort_keys=True, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(path.parent)


@contextmanager
def activation_lock(bin_dir: Path):
    bin_dir.mkdir(parents=True, exist_ok=True)
    state = bin_dir / ".pcc1-activation"
    if state.is_symlink():
        raise ValueError("activation state must not be a symlink")
    state.mkdir(exist_ok=True)
    fd = os.open(state / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("another toolchain activation is in progress") from exc
        yield state
    finally:
        os.close(fd)


def current_managed_entry(bin_dir: Path, store: Path):
    entry = bin_dir / "pcc1"
    if not entry.is_symlink() or not entry.exists():
        raise ValueError("stable command is not an intact managed symlink")
    target = entry.resolve(strict=True)
    if not entry.readlink().is_absolute() or entry.readlink() != target:
        raise ValueError("stable command is not a direct managed symlink")
    if target.name != "pcc1" or target.parent.name != "bin":
        raise ValueError("stable command does not name a managed launcher")
    identity = managed_identity(target.parent.parent, store)
    if target != Path(identity["root"]) / "bin/pcc1":
        raise ValueError("stable command does not name its managed launcher")
    return identity


def replace_managed_entry(
    bin_dir, state, before, after, *, operation, qualification=None, reverses=None
):
    transaction = uuid.uuid4().hex
    prepared = {
        "schema": "pcc.toolchain-activation.v1",
        "transaction": transaction,
        "operation": operation,
        "before": before,
        "after": after,
        "qualification": qualification,
        "reverses": reverses,
    }
    write_history(state / (transaction + ".prepare.json"), prepared)
    temporary = state / (transaction + ".next")
    temporary.symlink_to(Path(after["root"]) / "bin/pcc1")
    entry = bin_dir / "pcc1"
    if not entry.is_symlink() or entry.resolve() != Path(before["root"]) / "bin/pcc1":
        raise ValueError("stable command changed before atomic activation")
    os.replace(temporary, entry)
    sync_directory(bin_dir)
    write_history(
        state / (transaction + ".commit.json"),
        {
            "schema": "pcc.toolchain-activation-commit.v1",
            "transaction": transaction,
            "prepared_sha256": digest(state / (transaction + ".prepare.json")),
        },
    )
    return {
        "transaction": transaction,
        "operation": operation,
        "entry": str(entry),
        "installation": after["root"],
    }


def promote(root: Path, qualification: Path, bin_dir: Path, store: Path):
    identity = managed_identity(root, store)
    root = Path(identity["root"])
    qualified = verify_qualification(root, qualification.resolve(strict=True), identity)
    with activation_lock(bin_dir) as state:
        before = current_managed_entry(bin_dir, store)
        if before["root"] == identity["root"]:
            raise ValueError("candidate is already the stable installation")
        if (
            managed_identity(root, store) != identity
            or digest(qualification) != qualified["sha256"]
        ):
            raise ValueError("candidate or qualification changed before activation")
        return replace_managed_entry(
            bin_dir,
            state,
            before,
            identity,
            operation="promote",
            qualification=qualified,
        )


def rollback(transaction: str, bin_dir: Path, store: Path):
    if len(transaction) != 32 or any(
        char not in "0123456789abcdef" for char in transaction
    ):
        raise ValueError("rollback needs a recorded transaction id")
    with activation_lock(bin_dir) as state:
        history = read_json(state / (transaction + ".prepare.json"))
        if (
            history.get("schema") != "pcc.toolchain-activation.v1"
            or history.get("transaction") != transaction
            or history.get("operation") != "promote"
        ):
            raise ValueError("rollback transaction is not a managed promotion")
        commit = state / (transaction + ".commit.json")
        if commit.exists() and read_json(commit).get("prepared_sha256") != digest(
            state / (transaction + ".prepare.json")
        ):
            raise ValueError("activation history changed after commit")
        previous = managed_identity(Path(history["before"]["root"]), store)
        if previous != history["before"]:
            raise ValueError("previous installation changed since promotion")
        entry = bin_dir / "pcc1"
        if not entry.is_symlink():
            raise ValueError("stable command is not this promotion's managed symlink")
        target = entry.readlink()
        if target == Path(previous["root"]) / "bin/pcc1":
            return {
                "operation": "rollback",
                "status": "ALREADY_PREVIOUS",
                "transaction": transaction,
            }
        recorded_after = history["after"]
        after_root = Path(recorded_after["root"])
        if (
            not after_root.is_absolute()
            or after_root.parent != store.resolve()
            or target != after_root / "bin/pcc1"
        ):
            raise ValueError("stable command is no longer this promotion's candidate")
        # Recovery must work when the new compiler is the broken payload. The
        # journal and exact active symlink identify that version; only the
        # predecessor we are about to restore must pass full integrity checks.
        return replace_managed_entry(
            bin_dir,
            state,
            recorded_after,
            previous,
            operation="rollback",
            reverses=transaction,
        )


def activate_initial(root: Path, bin_dir: Path):
    """Atomically create the first entry; never overwrite an existing entry."""
    verify_installation(root)
    with activation_lock(bin_dir):
        target = bin_dir / "pcc1"
        # symlink creation is atomic and fails with EEXIST even for a dangling link.
        target.symlink_to(root / "bin/pcc1")
    return target


def install(args):
    first = args.stage1_dir.resolve(strict=True)
    second = args.stage2_dir.resolve(strict=True)
    validated, stage_evidence = verify_bootstrap(first, second)
    build = validated["receipt"]
    compiler_hash = build["compiler_sha256"]

    root = args.root.expanduser().resolve() / (args.name + "-" + compiler_hash[:12])
    root.mkdir(parents=True, exist_ok=False)
    evidence = root / "evidence"
    evidence.mkdir()
    files = {}

    def copy(source, relative, sha):
        checked_copy(source, root / relative, sha)
        files[relative] = sha

    copy(first / "pcc1", "libexec/pcc1", compiler_hash)
    (root / "libexec/pcc1").chmod(0o555)
    source = Path(validated["source_snapshot_root"])
    source_manifest = validated["source_manifest"]
    for name, sha in source_manifest["files"].items():
        copy(source / name, "source/" + name, sha)
    for name, item in build["runtime_bundle"]["files"].items():
        copy(first / "runtime-bundle" / name, "runtime/" + name, item["sha256"])
    for label, (path, sha) in stage_evidence.items():
        copy(path, "evidence/" + label + ".json", sha)

    def run(name, command, timeout=60):
        return run_logged(evidence, name, command, timeout)

    host = root / "host"
    run("venv", ["uv", "venv", "--python", sys.executable, str(host)])
    # These are helper dependencies, isolated from application/core environments.
    run(
        "dependencies",
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(host / "bin/python"),
            "black==26.3.1",
        ],
        timeout=120,
    )
    launcher = root / "bin/pcc1"
    launcher.parent.mkdir()
    launcher.write_text(launcher_text(root, host))
    launcher.chmod(0o555)
    files["bin/pcc1"] = digest(launcher)
    smoke = evidence / "installed_canary.py"
    smoke.write_text(
        "def add(a: int, b: int) -> int:\n    return a + b\nprint(add(20, 22))\n"
    )
    run("help", [str(launcher), "--help"])
    output = evidence / "installed_canary"
    run("compile", [str(launcher), str(smoke), "-o", str(output)], timeout=90)
    if run("execute", [str(output)]).strip() != "42":
        raise ValueError("installed native canary returned the wrong value")
    linkage = run("linkage", ["otool", "-L", str(output)])
    if "libpython" in linkage.lower() or "libllvm" in linkage.lower():
        raise ValueError("installed canary links a forbidden runtime")
    receipt = {
        "schema": "pcc.installed-baseline.v1",
        "status": "VERIFIED_BASELINE",
        "claim": "Stage1/Stage2 historical baseline plus installed native execution; not release qualification",
        "compiler_sha256": compiler_hash,
        "files": files,
        "stage1": str(first),
        "stage2": str(second),
        "canary_stdout": "42\n",
    }
    (root / "installation.json").write_text(json.dumps(receipt, indent=2) + "\n")
    verify_installation(root)
    entry = (
        None if args.stage_only else activate_initial(root, args.bin_dir.expanduser())
    )
    print(
        json.dumps(
            {
                "installation": str(root),
                "entry": str(entry) if entry else None,
                "status": receipt["status"],
            }
        )
    )


def launcher_text(root: Path, host: Path) -> str:
    q = shlex.quote
    return (
        "#!/bin/sh\nset -eu\n"
        + "export PCC_SOURCE_ROOT="
        + q(str(root / "source"))
        + "\n"
        # Python's -c/-m/script directory otherwise precedes PYTHONPATH. Keep
        # helper imports pinned while leaving native app cwd/package selection.
        + "export PYTHONSAFEPATH=1\n"
        + 'export PCC_REPO_ROOT="$PCC_SOURCE_ROOT"\n'
        + "export PYTHONPATH="
        + q(str(root / "source"))
        + "\n"
        + "export PCC_RUNTIME_ARCHIVE="
        + q(str(root / "runtime/libpy_runtime_pcc_py.a"))
        + "\n"
        + 'export PCC_HOST_PYTHON="${PCC_HOST_PYTHON:-'
        + str(host / "bin/python")
        + '}"\n'
        + "export PCC_RUNTIME_CC=/usr/bin/false\nexport PCC_RUNTIME_HIGH=py\n"
        + "exec "
        + q(str(root / "libexec/pcc1"))
        + ' "$@"\n'
    )


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=QUALIFICATION_PROTOCOL,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--from-source",
        type=Path,
        help="Build and validate an isolated first installation from a checkout",
    )
    parser.add_argument("--stage1-dir", type=Path)
    parser.add_argument("--stage2-dir", type=Path)
    parser.add_argument("--name")
    parser.add_argument(
        "--root", type=Path, default=Path.home() / ".local/share/pcc/toolchains"
    )
    parser.add_argument("--bin-dir", type=Path, default=Path.home() / ".local/bin")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--stage-only",
        action="store_true",
        help="Prepare a candidate without creating or replacing the stable command",
    )
    actions.add_argument(
        "--promote",
        type=Path,
        help="Atomically select a managed candidate with --qualification",
    )
    actions.add_argument(
        "--rollback",
        help="Restore the intact predecessor recorded by this promotion transaction",
    )
    actions.add_argument(
        "--record-gate",
        type=Path,
        help="Hash existing collection/run evidence described by a request JSON",
    )
    actions.add_argument(
        "--qualify",
        type=Path,
        help="Validate all --gate NAME=PATH inputs for a managed candidate",
    )
    parser.add_argument("--qualification", type=Path)
    parser.add_argument("--gate", action="append", default=[])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.promote or args.rollback or args.record_gate or args.qualify:
        if args.from_source or args.stage1_dir or args.stage2_dir or args.name:
            parser.error(
                "activation/evidence commands cannot be mixed with build inputs"
            )
        if args.promote:
            if not args.qualification or args.output or args.gate:
                parser.error(
                    "--promote requires only --qualification plus installation paths"
                )
            result = promote(
                args.promote,
                args.qualification,
                args.bin_dir.expanduser(),
                args.root.expanduser(),
            )
        elif args.rollback:
            if args.qualification or args.output or args.gate:
                parser.error("--rollback takes a transaction id and installation paths")
            result = rollback(
                args.rollback, args.bin_dir.expanduser(), args.root.expanduser()
            )
        elif args.record_gate:
            if not args.output or args.qualification or args.gate:
                parser.error("--record-gate requires --output")
            result = record_gate(
                read_json(args.record_gate), args.output, args.root.expanduser()
            )
        else:
            if not args.output or args.qualification:
                parser.error(
                    "--qualify requires --output and all four --gate NAME=PATH inputs"
                )
            gates = {}
            for value in args.gate:
                name, separator, path = value.partition("=")
                if not separator or name in gates:
                    parser.error("--gate needs a unique NAME=PATH")
                gates[name] = Path(path)
            result = qualify(args.qualify, gates, args.output, args.root.expanduser())
        print(json.dumps(result))
        return
    if args.qualification or args.gate or args.output:
        parser.error("qualification inputs require an activation/evidence command")
    if not args.stage_only and os.path.lexists(args.bin_dir.expanduser() / "pcc1"):
        parser.error("pcc1 already exists; initial installation never replaces it")
    if args.from_source:
        if args.stage1_dir or args.stage2_dir:
            parser.error("--from-source cannot be mixed with stage receipt directories")
        args.stage1_dir, args.stage2_dir, default_name = build_from_source(
            args.from_source
        )
        args.name = args.name or default_name
    elif not (args.stage1_dir and args.stage2_dir and args.name):
        parser.error(
            "use --from-source PATH, or provide --stage1-dir, --stage2-dir and --name"
        )
    if not args.name or any(
        char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
        for char in args.name
    ):
        parser.error(
            "name must contain only letters, digits, dot, underscore or hyphen"
        )
    install(args)


if __name__ == "__main__":
    main()
