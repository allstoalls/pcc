import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

import pytest

from scripts.install_pcc1_toolchain import activate_initial, checked_copy, digest
from scripts import install_pcc1_toolchain as installer


def candidate(tmp_path):
    root = tmp_path / "candidate"
    launcher = root / "bin/pcc1"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\nexit 0\n")
    (root / "installation.json").write_text(
        json.dumps(
            {
                "status": "VERIFIED_BASELINE",
                "files": {"bin/pcc1": digest(launcher)},
            }
        )
    )
    return root


def test_initial_activation_keeps_existing_compiler_even_if_link_is_dangling(tmp_path):
    root = candidate(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    entry = bin_dir / "pcc1"
    entry.symlink_to("old-toolchain/pcc1")
    with pytest.raises(FileExistsError):
        activate_initial(root, bin_dir)
    assert str(entry.readlink()) == "old-toolchain/pcc1"


def test_changed_candidate_cannot_become_the_stable_compiler(tmp_path):
    root = candidate(tmp_path)
    (root / "bin/pcc1").write_text("changed after qualification")
    with pytest.raises(ValueError, match="identity mismatch"):
        activate_initial(root, tmp_path / "bin")
    assert not (tmp_path / "bin/pcc1").exists()


def test_unverified_candidate_cannot_become_the_stable_compiler(tmp_path):
    root = candidate(tmp_path)
    p = root / "installation.json"
    data = json.loads(p.read_text())
    data["status"] = "FAILED"
    p.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="has not passed"):
        activate_initial(root, tmp_path / "bin")


def test_initial_entry_resolves_to_verified_candidate(tmp_path):
    root = candidate(tmp_path)
    entry = activate_initial(root, tmp_path / "bin")
    assert entry.resolve() == root / "bin/pcc1"


def test_copy_rejects_mismatched_receipt_before_writing(tmp_path):
    source = tmp_path / "source"
    source.write_text("changed")
    target = tmp_path / "target"
    with pytest.raises(ValueError, match="identity mismatch"):
        checked_copy(source, target, "0" * 64)
    assert not target.exists()


def test_first_install_command_never_builds_over_an_existing_entry(
    tmp_path, monkeypatch
):
    entry = tmp_path / "pcc1"
    entry.write_text("existing compiler")
    monkeypatch.setattr(
        sys, "argv", ["install", "--from-source", ".", "--bin-dir", str(tmp_path)]
    )

    def unexpected_build(_source):
        pytest.fail("must refuse before starting any bootstrap")

    monkeypatch.setattr(installer, "build_from_source", unexpected_build)
    with pytest.raises(SystemExit) as stopped:
        installer.main()
    assert stopped.value.code == 2
    assert entry.read_text() == "existing compiler"


def test_copied_runtime_preserves_static_abi_exports(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    frozen = tmp_path / "frozen"
    ports = frozen / "pcc/runtime/py"
    ports.mkdir(parents=True)
    (ports / "probe.py").write_text(
        "__pcc_runtime_port__ = True\n"
        "from pcc.runtime.py.py_abi_constants import PY_FLAG_IMMORTAL\n"
        "from pcc.unsafe import define_global_header\n"
        'define_global_header("installed_runtime_header", 1, 0, PY_FLAG_IMMORTAL)\n'
    )
    runtime = installer.prepare_runtime_source(frozen, tmp_path / "work")
    output = tmp_path / "runtime.ll"
    compile_python(
        str(runtime / "py/probe.py"),
        str(output),
        python_library=True,
        emit_llvm_only=True,
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    assert "@installed_runtime_header = " in output.read_text()


def test_installed_helper_imports_ignore_caller_checkout_and_keep_app_environment(
    tmp_path,
):
    root = tmp_path / "installed"
    source = root / "source"
    source.mkdir(parents=True)
    (source / "helper_identity.py").write_text("IDENTITY = 'installed'\n")
    application = tmp_path / "application"
    application.mkdir()
    (application / "helper_identity.py").write_text("IDENTITY = 'mutable'\n")
    compiler = root / "libexec/pcc1"
    compiler.parent.mkdir()
    compiler.write_text('#!/bin/sh\nexec "$PCC_HOST_PYTHON" "$@"\n')
    compiler.chmod(0o755)
    launcher = root / "bin/pcc1"
    launcher.parent.mkdir()
    launcher.write_text(installer.launcher_text(root, root / "host"))
    launcher.chmod(0o755)
    environment = dict(
        os.environ,
        PCC_HOST_PYTHON=sys.executable,
        VIRTUAL_ENV=str(application / ".venv"),
        PCC_PACKAGE_SITE=str(application / "packages"),
    )
    result = subprocess.run(
        [
            str(launcher),
            "-c",
            "import helper_identity,json,os; print(json.dumps([helper_identity.IDENTITY, "
            "os.getcwd(),os.environ['VIRTUAL_ENV'],os.environ['PCC_PACKAGE_SITE']]))",
        ],
        cwd=application,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    assert json.loads(result.stdout) == [
        "installed",
        str(application),
        environment["VIRTUAL_ENV"],
        environment["PCC_PACKAGE_SITE"],
    ]


def test_logged_timeout_keeps_output_and_terminates_detached_descendant(tmp_path):
    evidence = tmp_path / "evidence"
    child_pid_file = tmp_path / "child.pid"
    command = [
        sys.executable,
        "-c",
        (
            "import subprocess,sys,time; from pathlib import Path; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],"
            "start_new_session=True); "
            f"Path({str(child_pid_file)!r}).write_text(str(child.pid)); "
            "print('partial stdout',flush=True); print('partial stderr',file=sys.stderr,flush=True); "
            "time.sleep(30)"
        ),
    ]
    try:
        with pytest.raises(RuntimeError, match=r"TIMEOUT.*artifacts:"):
            installer.run_logged(evidence, "timeout", command, timeout=0.75)
        assert "partial stdout" in (evidence / "timeout.stdout").read_text()
        assert "partial stderr" in (evidence / "timeout.stderr").read_text()
        receipt = json.loads((evidence / "timeout.result.json").read_text())
        assert receipt["status"] == "TIMEOUT"
        child_pid = int(child_pid_file.read_text())
        state = subprocess.run(
            ["ps", "-p", str(child_pid), "-o", "stat="],
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
        assert not state or state.startswith("Z")
    finally:
        if child_pid_file.exists():
            try:
                os.kill(int(child_pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_logged_command_restores_environment_and_persists_success(tmp_path):
    before = os.environ.copy()
    result = installer.run_logged(
        tmp_path,
        "success",
        [sys.executable, "-c", "print('42')"],
        timeout=5,
        env={**before, "PCC_INSTALL_TEST": "private"},
    )
    assert result == "42\n"
    assert os.environ == before
    assert (
        json.loads((tmp_path / "success.result.json").read_text())["status"]
        == "COMPLETE"
    )


def test_terminating_installer_watcher_cleans_its_detached_child(tmp_path):
    evidence = tmp_path / "evidence"
    child_pid_file = tmp_path / "child.pid"
    child_code = (
        "import subprocess,sys,time; from pathlib import Path; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],"
        "start_new_session=True); "
        f"Path({str(child_pid_file)!r}).write_text(str(child.pid)); "
        "print('started',flush=True); time.sleep(30)"
    )
    watcher_code = (
        "from pathlib import Path; from scripts.install_pcc1_toolchain import run_logged; "
        f"run_logged(Path({str(evidence)!r}),'interrupted',"
        f"{[sys.executable, '-c', child_code]!r},timeout=30)"
    )
    with (tmp_path / "watcher.log").open("w") as log:
        watcher = subprocess.Popen(
            [sys.executable, "-c", watcher_code],
            cwd=installer.REPO,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 5
            while not child_pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert child_pid_file.exists()
            # Allow the repository sampler to observe the detached descendant.
            time.sleep(0.35)
            watcher.terminate()
            assert watcher.wait(timeout=5) != 0
            receipt = json.loads((evidence / "interrupted.result.json").read_text())
            assert receipt["status"] == "INTERRUPTED"
            assert "started" in (evidence / "interrupted.stdout").read_text()
            state = subprocess.run(
                ["ps", "-p", child_pid_file.read_text(), "-o", "stat="],
                capture_output=True,
                text=True,
                timeout=2,
            ).stdout.strip()
            assert not state or state.startswith("Z")
        finally:
            if watcher.poll() is None:
                os.killpg(watcher.pid, signal.SIGKILL)
                watcher.wait(timeout=2)
            if child_pid_file.exists():
                try:
                    os.kill(int(child_pid_file.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass


def bootstrap_evidence(tmp_path):
    from tests.python.test_pcc_compile_ab_tool import _write_build_evidence
    from scripts import run_pcc_stage1_build

    ab, _ = installer.bootstrap_tools()
    compilers = [tmp_path / "candidate", tmp_path / "baseline"]
    for index, compiler in enumerate(compilers):
        compiler.write_bytes(b"compiler" + bytes([index]))
    runtime = tmp_path / "libpy_runtime_pcc_py.a"
    runtime.write_bytes(b"runtime")
    receipts, _ = _write_build_evidence(
        ab, tmp_path, compilers, runtime, shared_logical_source_root=False
    )
    first = receipts[0].parent
    shutil.copyfile(compilers[0], first / "pcc1")
    bundled_runtime = first / "runtime-bundle" / runtime.name
    bundled_runtime.parent.mkdir()
    shutil.copyfile(runtime, bundled_runtime)
    build = json.loads(receipts[0].read_text())
    stage1_result_path = first / build["stage_result"]
    stage1_result = json.loads(stage1_result_path.read_text())
    stage1_result["metric_scopes"] = run_pcc_stage1_build.STAGE1_METRIC_SCOPES
    stage1_result["comparison_contract"] = (
        run_pcc_stage1_build.STAGE1_COMPARISON_CONTRACT
    )
    stage1_result_path.write_text(json.dumps(stage1_result))
    build["stage_result_sha256"] = digest(stage1_result_path)
    build["environment"]["PCC_RUNTIME_ARCHIVE"] = str(bundled_runtime)
    build["environment_sha256"] = ab._canonical_sha256(build["environment"])
    receipts[0].write_text(json.dumps(build))
    second = tmp_path / "stage2-run"
    output_root = second / "stage2"
    output_root.mkdir(parents=True)
    shutil.copyfile(compilers[0], output_root / "pcc1")
    output = output_root / "pcc2"
    output.write_bytes(b"second compiler")
    result_path = output_root / "profile/stage2.result.json"
    result_path.parent.mkdir()
    result = {
        "schema": "pcc.bootstrap_stage_result.v1",
        "stage": 2,
        "backend": "self",
        "returncode": 0,
        "publish_barrier_returncode": 0,
        "output": str(output),
    }
    result_path.write_text(json.dumps(result))
    source = first / "source-snapshot"
    process = {
        "status": "COMPLETE",
        "returncode": 0,
        "cwd": str(source),
        "command": [
            sys.executable,
            str(source / "scripts/bootstrap.py"),
            "--out-dir",
            str(output_root),
            "--backend",
            "self",
            "--from-stage",
            "2",
            "--stage",
            "2",
            "--reuse-stage1",
        ],
        "environment": build["environment"],
    }
    (second / "stage2-process.result.json").write_text(json.dumps(process))
    record = {
        "compiler": str(output),
        "compiler_sha256": digest(output),
        "result": result,
        "result_path": str(result_path),
        "process": process,
        "linkage": {"checked": True, "links_libpython": False, "links_llvm": False},
    }
    (second / "stage2-record.json").write_text(json.dumps(record))
    (second / "manifest.json").write_text(
        json.dumps(
            {
                "schema": "pcc.stage2-from-stage1-receipt.v1",
                "status": "COMPLETE",
                "stage1_dir": str(first),
                "stage2_record": "stage2-record.json",
                "compiler_sha256": digest(output),
            }
        )
    )
    return first, second


def test_bootstrap_evidence_binds_the_complete_source_runtime_and_stage_results(
    tmp_path,
):
    first, second = bootstrap_evidence(tmp_path)
    validated, evidence = installer.verify_bootstrap(first, second)
    assert validated["receipt"]["compiler_sha256"] == digest(first / "pcc1")
    assert set(evidence) == {
        "stage1",
        "stage1-result",
        "source",
        "stage2",
        "stage2-record",
        "stage2-result",
        "stage2-process",
    }


@pytest.mark.parametrize(
    "changed", ["source", "stage1-result", "runtime", "stage2-process", "stage2-result"]
)
def test_bootstrap_rejects_changed_bound_evidence(tmp_path, changed):
    first, second = bootstrap_evidence(tmp_path)
    ab, _ = installer.bootstrap_tools()
    if changed == "source":
        manifest_path = first / "source-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        source = first / "source-snapshot/pcc/driver/cli_core.py"
        source.chmod(0o644)
        source.write_text("changed = True\n")
        source.chmod(0o444)
        manifest["files"]["pcc/driver/cli_core.py"] = digest(source)
        manifest["bootstrap_source_sha256"] = ab._source_manifest_identity(
            manifest["files"]
        )
        manifest_path.write_text(json.dumps(manifest))
        expected = "source manifest digest differs"
    elif changed == "stage1-result":
        path = first / "stage1-result.json"
        path.write_text(path.read_text() + "\n")
        expected = "stage result digest differs"
    elif changed == "runtime":
        (first / "runtime-bundle/libpy_runtime_pcc_py.a").write_bytes(
            b"different runtime"
        )
        expected = "names a different runtime"
    else:
        path = (
            second / "stage2-process.result.json"
            if changed == "stage2-process"
            else second / "stage2/profile/stage2.result.json"
        )
        data = json.loads(path.read_text())
        data["returncode"] = 1
        path.write_text(json.dumps(data))
        expected = "Stage2 (process|result) does not bind"
    with pytest.raises((ValueError, ab.CompileABError), match=expected):
        installer.verify_bootstrap(first, second)


def test_bootstrap_rejects_consistently_recorded_different_runtime_owner(tmp_path):
    first, second = bootstrap_evidence(tmp_path)
    process_path = second / "stage2-process.result.json"
    process = json.loads(process_path.read_text())
    process["environment"]["PCC_RUNTIME_ARCHIVE"] = str(tmp_path / "mutable.a")
    process_path.write_text(json.dumps(process))
    record_path = second / "stage2-record.json"
    record = json.loads(record_path.read_text())
    record["process"] = process
    record_path.write_text(json.dumps(record))
    with pytest.raises(
        ValueError, match="Stage2 changed the receipt-bound PCC_RUNTIME_ARCHIVE"
    ):
        installer.verify_bootstrap(first, second)


def managed_candidate(tmp_path, name):
    inputs = tmp_path / (name + "-inputs")
    inputs.mkdir()
    first, _ = bootstrap_evidence(inputs)
    store = tmp_path / "toolchains"
    root = store / name
    root.mkdir(parents=True)
    shutil.copytree(first / "source-snapshot", root / "source")
    (root / "evidence").mkdir()
    shutil.copyfile(first / "source-manifest.json", root / "evidence/source.json")
    for relative, content in (
        ("bin/pcc1", "#!/bin/sh\nexit 0\n"),
        ("libexec/pcc1", name + " compiler"),
        ("runtime/libpy_runtime_pcc_py.a", "runtime"),
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        if relative in {"bin/pcc1", "libexec/pcc1"}:
            path.chmod(0o555)
    files = {
        path.relative_to(root).as_posix(): digest(path)
        for path in root.rglob("*")
        if path.is_file()
    }
    (root / "installation.json").write_text(
        json.dumps(
            {
                "schema": "pcc.installed-baseline.v1",
                "status": "VERIFIED_BASELINE",
                "compiler_sha256": files["libexec/pcc1"],
                "files": files,
            }
        )
    )
    return root


def qualification_requests(tmp_path, root):
    identity = installer.installation_identity(root)
    requests = {}
    for name in sorted(installer.QUALIFICATION_GATES):
        directory = tmp_path / name
        directory.mkdir(parents=True)
        nodes = [
            "tests/test_" + name + ".py::test_release",
            "tests/test_" + name + ".py::test_other",
        ]
        mark = "not integration" if name == "default" else "integration"
        artifacts = []
        for collect in (True, False):
            prefix = "collect" if collect else "run"
            report = directory / (prefix + ".pytest.jsonl")
            arguments = [
                "-x",
                "-m",
                mark,
                "tests",
                "-p",
                "scripts.pytest_live_report",
                "--pcc-live-report",
                str(report),
            ]
            if collect:
                arguments += ["--collect-only"]
            rows = [
                {
                    "event": "start",
                    "argv": arguments,
                    "root": str(root / "source"),
                    "markexpr": mark,
                    "keyword": "",
                    "ignore": [],
                    "ignore_glob": [],
                    "deselect": [],
                    "collect_only": collect,
                    "lf": False,
                    "stepwise": False,
                    "stepwise_skip": False,
                    "inifile": str(root / "source/pyproject.toml"),
                    "override_ini": [],
                    "pyargs": False,
                    "noconftest": False,
                    "confcutdir": "",
                    "validation_environment": {
                        "PCC_VALIDATION_INSTALLATION_SHA256": identity[
                            "receipt_sha256"
                        ],
                        "PCC_VALIDATION_SOURCE_MANIFEST": str(
                            root / "evidence/source.json"
                        ),
                        "PCC1_BINARY": str(root / "bin/pcc1"),
                        "PCC_SOURCE_ROOT": str(root / "source"),
                        "PCC_REPO_ROOT": str(root / "source"),
                        "PCC_RUNTIME_ARCHIVE": str(
                            root / "runtime/libpy_runtime_pcc_py.a"
                        ),
                    },
                    "source_manifest": str(root / "evidence/source.json"),
                },
                {"event": "collected", "nodeids": nodes},
            ]
            if not collect:
                rows.extend(
                    {
                        "event": "report",
                        "when": "call",
                        "outcome": "passed",
                        "nodeid": node,
                    }
                    for node in nodes
                )
            rows.append(
                {
                    "event": "finish",
                    "exitstatus": 0,
                    "testsfailed": 0,
                    "testscollected": len(nodes),
                }
            )
            report.write_text("".join(json.dumps(row) + "\n" for row in rows))
            process = directory / (prefix + ".result.json")
            process.write_text(
                json.dumps(
                    {
                        "schema": "pcc.process_tree_sample.v1",
                        "status": "COMPLETE",
                        "returncode": 0,
                        "command": [sys.executable, "-m", "pytest", *arguments],
                        "cwd": str(root / "source"),
                        "environment": {
                            "PCC_VALIDATION_INSTALLATION_SHA256": identity[
                                "receipt_sha256"
                            ],
                            "PCC_VALIDATION_SOURCE_MANIFEST": str(
                                root / "evidence/source.json"
                            ),
                            "PCC1_BINARY": str(root / "bin/pcc1"),
                            "PCC_SOURCE_ROOT": str(root / "source"),
                            "PCC_REPO_ROOT": str(root / "source"),
                            "PCC_RUNTIME_ARCHIVE": str(
                                root / "runtime/libpy_runtime_pcc_py.a"
                            ),
                        },
                    }
                )
            )
            artifacts.append({"process": str(process), "pytest": str(report)})
        requests[name] = {
            "candidate": str(root),
            "gate": name,
            "collection": artifacts[0],
            "runs": [artifacts[1]],
            "must_pass": [nodes[0]],
            "allowed_skips": {},
        }
    return requests


def qualify_requests(tmp_path, root, requests):
    gates = {}
    for name, request in requests.items():
        gates[name] = tmp_path / (name + ".gate.json")
        installer.record_gate(request, gates[name], root.parent)
    output = tmp_path / "qualification.json"
    installer.qualify(root, gates, output, root.parent)
    return output


def test_qualified_promotion_and_verified_rollback_preserve_both_versions(tmp_path):
    old = managed_candidate(tmp_path, "old")
    new = managed_candidate(tmp_path, "new")
    before = installer.installation_identity(old)
    bin_dir = tmp_path / "bin"
    installer.activate_initial(old, bin_dir)
    qualification = qualify_requests(
        tmp_path, new, qualification_requests(tmp_path / "gates", new)
    )
    result = installer.promote(new, qualification, bin_dir, old.parent)
    assert (bin_dir / "pcc1").resolve() == new / "bin/pcc1"
    assert installer.installation_identity(old) == before
    state = bin_dir / ".pcc1-activation"
    assert (state / (result["transaction"] + ".prepare.json")).exists()
    assert (state / (result["transaction"] + ".commit.json")).exists()
    installer.rollback(result["transaction"], bin_dir, old.parent)
    assert (bin_dir / "pcc1").resolve() == old / "bin/pcc1"
    assert installer.verify_installation(new)


@pytest.mark.parametrize("kind", ["regular", "dangling", "unmanaged"])
def test_promotion_never_replaces_an_unmanaged_command(tmp_path, kind):
    new = managed_candidate(tmp_path, "new")
    qualification = qualify_requests(
        tmp_path, new, qualification_requests(tmp_path / "gates", new)
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    entry = bin_dir / "pcc1"
    if kind == "regular":
        entry.write_text("user command")
    elif kind == "dangling":
        entry.symlink_to(tmp_path / "missing")
    else:
        external = tmp_path / "external/bin/pcc1"
        external.parent.mkdir(parents=True)
        external.write_text("external command")
        entry.symlink_to(external)
    previous = entry.readlink() if entry.is_symlink() else entry.read_bytes()
    with pytest.raises(ValueError, match="managed"):
        installer.promote(new, qualification, bin_dir, new.parent)
    assert (entry.readlink() if entry.is_symlink() else entry.read_bytes()) == previous


@pytest.mark.parametrize(
    "defect", ["missing-finish", "failed", "missing-node", "source", "compiler", "skip"]
)
def test_qualification_rejects_incomplete_failed_or_mismatched_gates(tmp_path, defect):
    new = managed_candidate(tmp_path, "new")
    requests = qualification_requests(tmp_path / "gates", new)
    run = requests["default"]["runs"][0]
    report = Path(run["pytest"])
    rows = [json.loads(line) for line in report.read_text().splitlines()]
    if defect == "missing-finish":
        rows.pop()
    elif defect == "failed":
        rows[-1]["testsfailed"] = 1
    elif defect == "missing-node":
        rows.pop(-2)
        rows[-1]["testscollected"] = 1
    elif defect == "skip":
        rows[-2].update(outcome="skipped", longrepr="Skipped: unsupported platform")
    else:
        process_path = Path(run["process"])
        process = json.loads(process_path.read_text())
        key = (
            "PCC_VALIDATION_INSTALLATION_SHA256"
            if defect == "source"
            else "PCC1_BINARY"
        )
        process["environment"][key] = "different"
        process_path.write_text(json.dumps(process))
    report.write_text("".join(json.dumps(row) + "\n" for row in rows))
    with pytest.raises(ValueError):
        qualify_requests(tmp_path, new, requests)
    assert not (tmp_path / "qualification.json").exists()


def test_reviewed_platform_skip_is_explicit_and_release_canary_cannot_skip(tmp_path):
    new = managed_candidate(tmp_path, "new")
    requests = qualification_requests(tmp_path / "gates", new)
    request = requests["integration"]
    report = Path(request["runs"][0]["pytest"])
    rows = [json.loads(line) for line in report.read_text().splitlines()]
    skipped = rows[-2]
    skipped.update(outcome="skipped", longrepr="Skipped: requires Linux")
    request["allowed_skips"][skipped["nodeid"]] = {
        "reason": skipped["longrepr"],
        "kind": "skip",
    }
    report.write_text("".join(json.dumps(row) + "\n" for row in rows))
    qualification = qualify_requests(tmp_path, new, requests)
    assert qualification.exists()
    request["must_pass"].append(skipped["nodeid"])
    second = tmp_path / "invalid"
    second.mkdir()
    with pytest.raises(ValueError, match="canaries cannot be skipped"):
        qualify_requests(second, new, requests)


def test_promotion_rechecks_qualification_hashes_and_previous_integrity(tmp_path):
    old = managed_candidate(tmp_path, "old")
    new = managed_candidate(tmp_path, "new")
    installer.activate_initial(old, tmp_path / "bin")
    qualification = qualify_requests(
        tmp_path, new, qualification_requests(tmp_path / "gates", new)
    )
    (tmp_path / "default.gate.json").write_text("{}")
    with pytest.raises(ValueError, match="identity mismatch"):
        installer.promote(new, qualification, tmp_path / "bin", old.parent)
    assert (tmp_path / "bin/pcc1").resolve() == old / "bin/pcc1"


def test_rollback_recovers_switch_with_missing_commit_record(tmp_path, monkeypatch):
    old = managed_candidate(tmp_path, "old")
    new = managed_candidate(tmp_path, "new")
    bin_dir = tmp_path / "bin"
    installer.activate_initial(old, bin_dir)
    qualification = qualify_requests(
        tmp_path, new, qualification_requests(tmp_path / "gates", new)
    )
    original_write = installer.write_history

    def fail_commit(path, payload):
        if path.name.endswith(".commit.json"):
            raise OSError("simulated crash after symlink replacement")
        return original_write(path, payload)

    monkeypatch.setattr(installer, "write_history", fail_commit)
    with pytest.raises(OSError, match="simulated crash"):
        installer.promote(new, qualification, bin_dir, old.parent)
    assert (bin_dir / "pcc1").resolve() == new / "bin/pcc1"
    prepared = next((bin_dir / ".pcc1-activation").glob("*.prepare.json"))
    transaction = json.loads(prepared.read_text())["transaction"]
    monkeypatch.setattr(installer, "write_history", original_write)
    installer.rollback(transaction, bin_dir, old.parent)
    assert (bin_dir / "pcc1").resolve() == old / "bin/pcc1"


def test_staging_candidate_never_touches_existing_stable_command(tmp_path, monkeypatch):
    import argparse

    inputs = tmp_path / "inputs"
    inputs.mkdir()
    first, second = bootstrap_evidence(inputs)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "pcc1").write_text("user-owned stable command")
    monkeypatch.setattr(
        installer,
        "run_logged",
        lambda _evidence, name, *_args: "42\n" if name == "execute" else "",
    )

    def unexpected_activation(*_args):
        pytest.fail("stage-only must not attempt activation")

    monkeypatch.setattr(installer, "activate_initial", unexpected_activation)
    installer.install(
        argparse.Namespace(
            stage1_dir=first,
            stage2_dir=second,
            root=tmp_path / "toolchains",
            name="candidate",
            bin_dir=bin_dir,
            stage_only=True,
        )
    )
    assert (bin_dir / "pcc1").read_text() == "user-owned stable command"
    assert not (bin_dir / ".pcc1-activation").exists()


def test_qualification_requires_all_gates_and_never_accepts_a_pass_boolean(tmp_path):
    old = managed_candidate(tmp_path, "old")
    new = managed_candidate(tmp_path, "new")
    bin_dir = tmp_path / "bin"
    installer.activate_initial(old, bin_dir)
    requests = qualification_requests(tmp_path / "gates", new)
    requests.pop("bootstrap")
    with pytest.raises(ValueError, match="all required gates"):
        qualify_requests(tmp_path, new, requests)
    qualification = tmp_path / "naked-pass.json"
    qualification.write_text('{"passed": true}')
    with pytest.raises(ValueError, match="all required gates"):
        installer.promote(new, qualification, bin_dir, new.parent)
    assert (bin_dir / "pcc1").resolve() == old / "bin/pcc1"


def test_rollback_refuses_a_changed_previous_payload(tmp_path):
    old = managed_candidate(tmp_path, "old")
    new = managed_candidate(tmp_path, "new")
    bin_dir = tmp_path / "bin"
    installer.activate_initial(old, bin_dir)
    qualification = qualify_requests(
        tmp_path, new, qualification_requests(tmp_path / "gates", new)
    )
    result = installer.promote(new, qualification, bin_dir, old.parent)
    compiler = old / "libexec/pcc1"
    compiler.chmod(0o755)
    compiler.write_text("changed predecessor")
    with pytest.raises(ValueError, match="identity mismatch"):
        installer.rollback(result["transaction"], bin_dir, old.parent)
    assert (bin_dir / "pcc1").resolve() == new / "bin/pcc1"


def test_promotion_refuses_an_overlapping_activation_lock(tmp_path):
    old = managed_candidate(tmp_path, "old")
    new = managed_candidate(tmp_path, "new")
    bin_dir = tmp_path / "bin"
    installer.activate_initial(old, bin_dir)
    qualification = qualify_requests(
        tmp_path, new, qualification_requests(tmp_path / "gates", new)
    )
    with installer.activation_lock(bin_dir):
        with pytest.raises(ValueError, match="activation is in progress"):
            installer.promote(new, qualification, bin_dir, old.parent)
    assert (bin_dir / "pcc1").resolve() == old / "bin/pcc1"


def test_failed_atomic_switch_keeps_previous_entry_and_recoverable_history(
    tmp_path, monkeypatch
):
    old = managed_candidate(tmp_path, "old")
    new = managed_candidate(tmp_path, "new")
    bin_dir = tmp_path / "bin"
    installer.activate_initial(old, bin_dir)
    qualification = qualify_requests(
        tmp_path, new, qualification_requests(tmp_path / "gates", new)
    )

    def fail_replace(_source, _target):
        raise OSError("simulated failure before replacement")

    monkeypatch.setattr(installer.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated failure"):
        installer.promote(new, qualification, bin_dir, old.parent)
    assert (bin_dir / "pcc1").resolve() == old / "bin/pcc1"
    prepared = next((bin_dir / ".pcc1-activation").glob("*.prepare.json"))
    result = installer.rollback(
        json.loads(prepared.read_text())["transaction"], bin_dir, old.parent
    )
    assert result["status"] == "ALREADY_PREVIOUS"


@pytest.mark.parametrize(
    "filter_source",
    [
        "joined-k",
        "joined-k-equals",
        "ini-addopts",
        "override-addopts",
        "ignore",
        "ignore_glob",
        "deselect",
    ],
)
def test_full_collection_rejects_effective_selection_filters(tmp_path, filter_source):
    new = managed_candidate(tmp_path, "new")
    requests = qualification_requests(tmp_path / "gates", new)
    collection = requests["default"]["collection"]
    report_path = Path(collection["pytest"])
    rows = [json.loads(line) for line in report_path.read_text().splitlines()]
    start = rows[0]
    if filter_source in {"ignore", "ignore_glob", "deselect"}:
        start[filter_source] = ["tests/test_excluded.py"]
    else:
        start["keyword"] = "release"
        if filter_source == "joined-k":
            start["argv"].append("-krelease")
        elif filter_source == "joined-k-equals":
            start["argv"].append("-k=release")
        elif filter_source == "override-addopts":
            start["argv"].extend(["-o", "addopts=-krelease"])
        # ini/PYTEST_ADDOPTS selection does not appear in invocation argv.
    report_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    process_path = Path(collection["process"])
    process = json.loads(process_path.read_text())
    process["command"] = [sys.executable, "-m", "pytest", *start["argv"]]
    process_path.write_text(json.dumps(process))
    with pytest.raises(ValueError, match="complete tests root"):
        qualify_requests(tmp_path, new, requests)


@pytest.mark.parametrize(
    "field",
    [
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
    ],
)
def test_qualification_requires_effective_selection_fields(tmp_path, field):
    new = managed_candidate(tmp_path, "new")
    requests = qualification_requests(tmp_path / "gates", new)
    report_path = Path(requests["default"]["collection"]["pytest"])
    rows = [json.loads(line) for line in report_path.read_text().splitlines()]
    del rows[0][field]
    report_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    with pytest.raises(ValueError, match="effective pytest selection"):
        qualify_requests(tmp_path, new, requests)


def test_qualification_rejects_a_different_actual_pytest_compiler_environment(tmp_path):
    new = managed_candidate(tmp_path, "new")
    requests = qualification_requests(tmp_path / "gates", new)
    parent_environment = json.loads(
        Path(requests["default"]["runs"][0]["process"]).read_text()
    )["environment"]
    source = new / "source"
    source.chmod(0o755)
    (source / "tests").mkdir()
    (source / "tests/test_default.py").write_text(
        "def test_release():\n    assert True\n\ndef test_other():\n    assert True\n"
    )
    evidence = tmp_path / "actual-child"
    report_path = evidence / "run.pytest.jsonl"
    # Exercise the actual sampler -> env override -> pytest -> reporter path.
    # These tiny tests never invoke a compiler.
    installer.run_logged(
        evidence,
        "run",
        [
            "env",
            "PCC1_BINARY=/other/compiler",
            sys.executable,
            "-m",
            "pytest",
            "-x",
            "-n0",
            "-m",
            "not integration",
            "--rootdir",
            str(source),
            "tests",
            "-p",
            "scripts.pytest_live_report",
            "--pcc-live-report",
            str(report_path),
        ],
        timeout=15,
        cwd=source,
        env={**os.environ, **parent_environment, "PYTHONPATH": str(installer.REPO)},
    )
    rows = [json.loads(line) for line in report_path.read_text().splitlines()]
    assert rows[0]["validation_environment"]["PCC1_BINARY"] == "/other/compiler"
    assert rows[-1]["exitstatus"] == 0
    requests["default"]["runs"] = [
        {
            "process": str(evidence / "run.result.json"),
            "pytest": str(report_path),
        }
    ]
    with pytest.raises(ValueError, match="actual pytest environment"):
        qualify_requests(tmp_path, new, requests)


@pytest.mark.parametrize("damage", ["corrupt-binary", "missing-binary"])
def test_rollback_recovers_a_broken_candidate_when_predecessor_is_intact(
    tmp_path, damage
):
    old = managed_candidate(tmp_path, "old")
    new = managed_candidate(tmp_path, "new")
    bin_dir = tmp_path / "bin"
    installer.activate_initial(old, bin_dir)
    qualification = qualify_requests(
        tmp_path, new, qualification_requests(tmp_path / "gates", new)
    )
    result = installer.promote(new, qualification, bin_dir, old.parent)
    compiler = new / "libexec/pcc1"
    if damage == "corrupt-binary":
        compiler.chmod(0o755)
        compiler.write_text("broken new compiler")
    else:
        # Preserve the test artifact while making its installed name absent.
        compiler.rename(new / "libexec/pcc1.saved")
    installer.rollback(result["transaction"], bin_dir, old.parent)
    assert (bin_dir / "pcc1").resolve() == old / "bin/pcc1"


def test_real_pytest_receipts_flow_through_record_gate_and_qualify_cli(tmp_path):
    root = managed_candidate(tmp_path, "candidate")
    source = root / "source"
    source.chmod(0o755)
    tests = source / "tests"
    tests.mkdir()
    (tests / "test_protocol.py").write_text(
        "import pytest\n\ndef test_default():\n    assert True\n\n"
        "@pytest.mark.integration\ndef test_integration():\n    assert True\n"
    )
    identity = installer.installation_identity(root)
    environment = {
        **os.environ,
        "PYTHONPATH": str(installer.REPO),
        "PCC_VALIDATION_SOURCE_MANIFEST": str(root / "evidence/source.json"),
        "PCC_VALIDATION_INSTALLATION_SHA256": identity["receipt_sha256"],
        "PCC1_BINARY": str(root / "bin/pcc1"),
        "PCC_SOURCE_ROOT": str(source),
        "PCC_REPO_ROOT": str(source),
        "PCC_RUNTIME_ARCHIVE": str(root / "runtime/libpy_runtime_pcc_py.a"),
    }
    gate_runs = {}
    for name, mark in (("default", "not integration"), ("integration", "integration")):
        pairs = []
        for collect in (True, False):
            label = name + ("-collection" if collect else "-execution")
            evidence = tmp_path / "receipts" / label
            report = evidence / "pytest.jsonl"
            command = [
                sys.executable,
                "-m",
                "pytest",
                "-x",
                "-n0",
                "-m",
                mark,
                "--rootdir",
                str(source),
                "tests",
                "-p",
                "scripts.pytest_live_report",
                "--pcc-live-report",
                str(report),
            ]
            if collect:
                command.append("--collect-only")
            installer.run_logged(
                evidence, "process", command, timeout=15, cwd=source, env=environment
            )
            pairs.append(
                {
                    "process": str(evidence / "process.result.json"),
                    "pytest": str(report),
                }
            )
        gate_runs[name] = pairs
    gate_flags = []
    for name in sorted(installer.QUALIFICATION_GATES):
        mode = "default" if name == "default" else "integration"
        collection, execution = gate_runs[mode]
        request = tmp_path / (name + "-request.json")
        request.write_text(
            json.dumps(
                {
                    "candidate": str(root),
                    "gate": name,
                    "collection": collection,
                    "runs": [execution],
                    "must_pass": ["tests/test_protocol.py::test_" + mode],
                }
            )
        )
        output = tmp_path / (name + "-gate.json")
        subprocess.run(
            [
                sys.executable,
                str(installer.REPO / "scripts/install_pcc1_toolchain.py"),
                "--record-gate",
                str(request),
                "--output",
                str(output),
                "--root",
                str(root.parent),
            ],
            cwd=installer.REPO,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        gate_flags.extend(["--gate", name + "=" + str(output)])
    qualification = tmp_path / "qualified.json"
    subprocess.run(
        [
            sys.executable,
            str(installer.REPO / "scripts/install_pcc1_toolchain.py"),
            "--qualify",
            str(root),
            *gate_flags,
            "--output",
            str(qualification),
            "--root",
            str(root.parent),
        ],
        cwd=installer.REPO,
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert (
        json.loads(qualification.read_text())["installation_sha256"]
        == identity["receipt_sha256"]
    )


def test_rollback_does_not_replace_an_external_link_selected_after_promotion(tmp_path):
    old = managed_candidate(tmp_path, "old")
    new = managed_candidate(tmp_path, "new")
    bin_dir = tmp_path / "bin"
    installer.activate_initial(old, bin_dir)
    qualification = qualify_requests(
        tmp_path, new, qualification_requests(tmp_path / "gates", new)
    )
    result = installer.promote(new, qualification, bin_dir, old.parent)
    entry = bin_dir / "pcc1"
    entry.rename(bin_dir / "promoted-link.saved")
    external = tmp_path / "external-command"
    external.write_text("user selected something else")
    entry.symlink_to(external)
    with pytest.raises(ValueError, match="no longer this promotion's candidate"):
        installer.rollback(result["transaction"], bin_dir, old.parent)
    assert entry.readlink() == external


@pytest.mark.parametrize("field", ["lf", "stepwise", "stepwise_skip"])
@pytest.mark.parametrize("value", [True, 0, "false"])
def test_full_collection_rejects_cache_selection_and_nonboolean_fields(
    tmp_path, field, value
):
    root = managed_candidate(tmp_path, "candidate")
    requests = qualification_requests(tmp_path / "gates", root)
    path = Path(requests["default"]["collection"]["pytest"])
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0][field] = value
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    with pytest.raises(
        ValueError, match="complete tests root|effective pytest selection"
    ):
        qualify_requests(tmp_path, root, requests)


@pytest.mark.parametrize(
    "source_kind, selector, field",
    [
        ("environment", "--lf", "lf"),
        ("override-addopts", "--lf", "lf"),
        ("environment", "--sw", "stepwise"),
        ("environment", "--sw-skip", "stepwise_skip"),
    ],
)
def test_real_addopts_cache_selection_cannot_qualify_full_suite(
    tmp_path, source_kind, selector, field
):
    root = managed_candidate(tmp_path, "candidate")
    requests = qualification_requests(tmp_path / "gates", root)
    environment = json.loads(
        Path(requests["default"]["collection"]["process"]).read_text()
    )["environment"]
    source = root / "source"
    source.chmod(0o755)
    (source / "tests").mkdir()
    (source / "tests/test_default.py").write_text(
        "def test_release():\n    assert True\n\ndef test_other():\n    assert True\n"
    )
    cache = source / ".pytest_cache/v/cache"
    cache.mkdir(parents=True)
    selected = "tests/test_default.py::test_other"
    (cache / "lastfailed").write_text(json.dumps({selected: True}))
    evidence = tmp_path / "actual-collection"
    report = evidence / "pytest.jsonl"
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-x",
        "-n0",
        "-m",
        "not integration",
        "--rootdir",
        str(source),
        "tests",
        "--collect-only",
        "-p",
        "scripts.pytest_live_report",
        "--pcc-live-report",
        str(report),
    ]
    environment = {**os.environ, **environment, "PYTHONPATH": str(installer.REPO)}
    if source_kind == "environment":
        environment["PYTEST_ADDOPTS"] = selector
    else:
        environment.pop("PYTEST_ADDOPTS", None)
        command.extend(["-o", "addopts=" + selector])
    installer.run_logged(
        evidence, "process", command, timeout=15, cwd=source, env=environment
    )
    rows = [json.loads(line) for line in report.read_text().splitlines()]
    assert rows[0][field] is True
    assert selector not in rows[0]["argv"]
    if field == "lf":
        assert next(row for row in rows if row["event"] == "collected")["nodeids"] == [
            selected
        ]
    requests["default"]["collection"] = {
        "process": str(evidence / "process.result.json"),
        "pytest": str(report),
    }
    requests["default"]["must_pass"] = [selected]
    with pytest.raises(ValueError, match="complete tests root"):
        qualify_requests(tmp_path, root, requests)


@pytest.mark.parametrize(
    "field, value",
    [
        ("inifile", "/different/pytest.ini"),
        ("override_ini", ["python_files=test_one.py"]),
        ("override_ini", ["norecursedirs=important_tests"]),
        ("pyargs", True),
        ("noconftest", True),
        ("confcutdir", "/different"),
    ],
)
def test_full_collection_uses_frozen_project_discovery_configuration(
    tmp_path, field, value
):
    root = managed_candidate(tmp_path, "candidate")
    requests = qualification_requests(tmp_path / "gates", root)
    path = Path(requests["integration"]["collection"]["pytest"])
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0][field] = value
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    with pytest.raises(ValueError, match="complete tests root"):
        qualify_requests(tmp_path, root, requests)
