"""Host-only controls for the post-Stage1 native object gate; no native build."""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
import struct
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import ci_native_object_regressions as gate
from scripts import ci_worker_size_prior_gate as prior
from tests.python import owned_regression_support as support


ROOT = Path(__file__).resolve().parents[1]


def _receipts(directory, runtime):
    paths = []
    for label, (program, output) in gate.expected_programs().items():
        work = directory / label
        work.mkdir(parents=True)
        source = work / "program.py"
        source.write_text(program, encoding="utf-8")
        binary = work / "program.out"
        binary.write_bytes(b"host-only receipt control; never executed")
        oracle = {"command": [sys.executable, str(source)],
                  "returncode": 0, "stdout": output, "stderr": ""}
        record = {
            "status": "PASS", "compiler_fixture_parameter": "pcc0", "compiler_mode": "pcc0",
            "backend": "self", "libpython": "off", "source_sha256": gate.sha(source),
            "runtime_archive": runtime["path"], "runtime_sha256": runtime["hashes"][""],
            "binary_sha256": gate.sha(binary), "reference": oracle,
            "executions": [{**oracle, "command": [str(binary)], "gc_backend": gc}
                           for gc in range(5)],
        }
        path = work / "ownership-regression.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        paths.append(path)
    return paths


@pytest.fixture
def admitted(tmp_path, monkeypatch):
    archive = tmp_path / "libpy_runtime_pcc_py.a"
    archive.write_bytes(b"host-only runtime placeholder")
    source = {"commit": "test-commit", "clean_commit": True, "source_sha256": "test-source",
              "python": {"executable": "host-python", "sha256": "python-hash", "version": "3.13"},
              "test_versions": {"pytest": "9.0.3", "xdist": "3.8.0"}}
    runtime = {"path": str(archive), "config": {"threads": False, "refcount": "atomic"},
               "target": "host-control", "hashes": {"": gate.sha(archive)}}
    (tmp_path / "stage1.json").write_text(json.dumps({
        "phase": "stage1", "status": "FAIL", "source": source, "runtime": runtime,
    }), encoding="utf-8")
    (tmp_path / "input.py").write_text("host-only source input", encoding="utf-8")
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "TEST_INPUTS", ("input.py",))
    monkeypatch.setenv("GITHUB_SHA", source["commit"])
    monkeypatch.setattr(gate, "source_identity", lambda: copy.deepcopy(source))
    monkeypatch.setattr(gate, "runtime_identity", lambda path: copy.deepcopy(runtime))
    monkeypatch.setattr(gate, "environment", lambda: {"PCC_RUNTIME_ARCHIVE": str(archive)})
    monkeypatch.setattr(gate, "verify_native_image", lambda path: {"format": "host-only control"})
    # Common-program controls remain platform-independent; the Windows
    # extension has its own selection and receipt controls below.
    monkeypatch.setattr(gate, "selected_nodes", lambda: gate.NODES)
    return source, runtime


@pytest.mark.parametrize("stage_status", ("PASS", "FAIL"))
def test_gate_reuses_terminal_runtime_and_records_twenty_five_executions(tmp_path, monkeypatch, admitted, stage_status):
    source, runtime = admitted
    path = tmp_path / "stage1.json"
    stage = json.loads(path.read_text())
    stage["status"] = stage_status
    path.write_text(json.dumps(stage))
    calls = []

    def run(out, name, nodes, env, **kwargs):
        calls.append((out, name, nodes, env, kwargs))
        _receipts(out / name, runtime)

    monkeypatch.setattr(gate, "run_pytest", run)
    result = gate.run(tmp_path)
    assert len(calls) == 1
    out, name, nodes, env, options = calls[0]
    assert (out, name, nodes) == (tmp_path, gate.NAME, gate.NODES)
    assert options == {"integration": True, "timeout": 300}
    assert env["PCC_RUNTIME_ARCHIVE"] == runtime["path"]
    assert env["PCC_TEST_NO_NATIVE_PROVISIONING"] == "1"
    assert env["UV_PYTHON_DOWNLOADS"] == "never"
    assert result["status"] == "PASS" and result["qualifies_stage1"] is False
    assert result["observed_collector_witness"] is False
    assert result["collector_evidence"] == (
        "requested_gc_backend records are PCC_GC_BACKEND requests; "
        "collector selection is not independently observed"
    )
    assert result["source"] == result["source_after"] == source
    assert result["runtime"] == result["runtime_after"] == runtime
    assert result["stage1_status"] == stage_status
    assert set(result["programs"]) == {
        "object-identity", "super-result", "dict-copy-result", "dict-pop-result", "two-root-cleanup",
    }
    assert sum(len(row["executions"]) for row in result["programs"].values()) == 25
    for row in result["programs"].values():
        assert [item["requested_gc_backend"] for item in row["executions"]] == list(range(5))
        assert all("gc_backend" not in item for item in row["executions"])
    assert json.loads((tmp_path / (gate.NAME + ".json")).read_text()) == result


def test_gate_isolates_stage1_timing_without_filtering_compiler_errors(
    tmp_path, monkeypatch, admitted,
):
    _source, runtime = admitted
    inherited = {"PCC_RUNTIME_ARCHIVE": runtime["path"],
                 "PCC_PY_FRONTEND_WORKER_TIMING": "1",
                 "PCC_DEBUG_WORKER_RERAISE": "1"}
    monkeypatch.setattr(gate, "environment", lambda: inherited)

    def run(out, name, nodes, env, **kwargs):
        assert "PCC_PY_FRONTEND_WORKER_TIMING" not in env
        assert env["PCC_DEBUG_WORKER_RERAISE"] == "1"
        raise RuntimeError("real compiler failure")

    monkeypatch.setattr(gate, "run_pytest", run)
    with pytest.raises(RuntimeError, match="real compiler failure"):
        gate.run(tmp_path)
    assert inherited["PCC_PY_FRONTEND_WORKER_TIMING"] == "1"
    result = json.loads((tmp_path / (gate.NAME + ".json")).read_text())
    assert result["status"] == "FAIL"
    assert "real compiler failure" in result["error"]


@pytest.mark.parametrize("change", (
    "no-admission", "missing-archive", "missing-stage", "running-stage", "wrong-stage",
    "wrong-commit", "dirty-source", "stale-source", "wrong-host-python", "wrong-test-tools",
    "wrong-runtime",
))
def test_missing_or_stale_admission_fails_before_any_test(tmp_path, monkeypatch, admitted, change):
    source, runtime = admitted
    stage_path = tmp_path / "stage1.json"
    stage = json.loads(stage_path.read_text())
    if change == "no-admission":
        monkeypatch.setattr(gate, "environment", lambda: {})
    elif change == "missing-archive":
        Path(runtime["path"]).unlink()
    elif change == "missing-stage":
        stage_path.unlink()
    elif change == "running-stage":
        stage["status"] = "RUNNING"
    elif change == "wrong-stage":
        stage["phase"] = "pcc1"
    elif change == "wrong-commit":
        monkeypatch.setenv("GITHUB_SHA", "another-commit")
    elif change == "dirty-source":
        source["clean_commit"] = False
    elif change == "stale-source":
        stage["source"]["source_sha256"] = "stale"
    elif change == "wrong-host-python":
        stage["source"]["python"]["sha256"] = "another-python"
    elif change == "wrong-test-tools":
        stage["source"]["test_versions"]["pytest"] = "another-pytest"
    else:
        stage["runtime"]["hashes"][""] = "stale"
    if stage_path.exists():
        stage_path.write_text(json.dumps(stage))
    monkeypatch.setattr(gate, "run_pytest", lambda *args, **kwargs: pytest.fail("unadmitted native test"))
    with pytest.raises((AssertionError, FileNotFoundError)):
        gate.run(tmp_path)
    result = json.loads((tmp_path / (gate.NAME + ".json")).read_text())
    assert result["status"] == "FAIL" and result["qualifies_stage1"] is False


@pytest.mark.parametrize("change", ("source", "runtime", "test-input", "pytest-failure"))
def test_run_failure_or_identity_drift_is_never_a_pass(tmp_path, monkeypatch, admitted, change):
    source, runtime = admitted

    def run(out, name, *args, **kwargs):
        _receipts(out / name, runtime)
        if change == "source":
            source["source_sha256"] = "changed"
        elif change == "runtime":
            runtime["hashes"][""] = "changed"
        elif change == "test-input":
            (tmp_path / "input.py").write_text("changed")
        else:
            raise RuntimeError("native execution failed or timed out")

    monkeypatch.setattr(gate, "run_pytest", run)
    with pytest.raises((AssertionError, RuntimeError)):
        gate.run(tmp_path)
    result = json.loads((tmp_path / (gate.NAME + ".json")).read_text())
    assert result["status"] == "FAIL"
    assert "source_after" in result and "runtime_after" in result


@pytest.mark.parametrize("change", (
    "missing-receipt", "failed-receipt", "pcc1", "routed-pcc1", "llvm", "libpython",
    "source-digest", "runtime-path", "runtime-digest", "binary-digest", "oracle",
    "missing-gc", "duplicate-gc", "failed-gc", "wrong-output", "stderr", "wrong-command",
    "foreign-program", "duplicate-program",
))
@pytest.mark.parametrize("label", (
    "object-identity", "super-result", "dict-copy-result", "dict-pop-result", "two-root-cleanup",
))
def test_native_evidence_requires_exact_source_and_all_five_successes(
    tmp_path, monkeypatch, admitted, change, label,
):
    _source, runtime = admitted
    directory = tmp_path / "receipts"
    paths = _receipts(directory, runtime)
    path = next(path for path in paths if path.parent.name == label)
    record = json.loads(path.read_text())
    fields = {"failed-receipt": ("status", "FAIL"), "pcc1": ("compiler_fixture_parameter", "pcc1"),
              "routed-pcc1": ("compiler_mode", "pcc1-routed"), "llvm": ("backend", "llvm"),
              "libpython": ("libpython", "on"), "source-digest": ("source_sha256", "wrong"),
              "runtime-path": ("runtime_archive", "another-runtime"),
              "runtime-digest": ("runtime_sha256", "wrong"), "binary-digest": ("binary_sha256", "wrong")}
    if change in fields:
        key, value = fields[change]
        record[key] = value
    elif change == "oracle":
        record["reference"]["returncode"] = 1
    elif change == "missing-gc":
        record["executions"].pop()
    elif change == "duplicate-gc":
        record["executions"][-1]["gc_backend"] = 0
    elif change == "failed-gc":
        record["executions"][2]["returncode"] = 124
    elif change == "wrong-output":
        record["executions"][2]["stdout"] = "wrong"
    elif change == "stderr":
        record["executions"][2]["stderr"] = "unexpected diagnostic"
    elif change == "wrong-command":
        record["executions"][2]["command"] = [sys.executable]
    elif change == "foreign-program":
        (path.parent / "program.py").write_text("print('unrelated')")
    elif change == "duplicate-program":
        other = next(other for other in paths if other != path).parent / "program.py"
        other.write_text((path.parent / "program.py").read_text())
    path.write_text(json.dumps(record))
    if change == "missing-receipt":
        path.unlink()
    with pytest.raises(AssertionError):
        gate.verify_program_receipts(directory, runtime)


@pytest.mark.parametrize("missing", (
    ("dict-copy-result",), ("dict-pop-result",),
    ("dict-copy-result", "dict-pop-result"), ("two-root-cleanup",),
), ids=("copy", "pop", "both", "two-root"))
def test_missing_native_evidence_names_the_absent_program_and_fails_gate(
    tmp_path, monkeypatch, admitted, missing,
):
    _source, runtime = admitted

    def run(out, name, *args, **kwargs):
        for path in _receipts(out / name, runtime):
            if path.parent.name in missing:
                path.unlink()

    monkeypatch.setattr(gate, "run_pytest", run)
    message = "missing native execution receipts: " + ", ".join(missing)
    with pytest.raises(AssertionError, match=message):
        gate.run(tmp_path)
    receipt = json.loads((tmp_path / (gate.NAME + ".json")).read_text())
    assert receipt["status"] == "FAIL"
    assert receipt["error"] == "AssertionError: " + message


def test_gate_selects_exact_dict_pcc0_nodes_and_program_sources():
    from tests.python.test_dict_method_result_ownership import _program

    assert gate.NODES[2:4] == (
        "tests/python/test_dict_method_result_ownership.py::"
        "test_dict_method_result_owners_native_five_gc[pcc0-copy]",
        "tests/python/test_dict_method_result_ownership.py::"
        "test_dict_method_result_owners_native_five_gc[pcc0-pop]",
    )
    programs = gate.expected_programs()
    assert programs["dict-copy-result"] == (_program("copy"), "DICT_METHOD_OWNERS_OK\n")
    assert programs["dict-pop-result"] == (_program("pop"), "DICT_METHOD_OWNERS_OK\n")
    assert programs["dict-copy-result"][0] != programs["dict-pop-result"][0]
    assert "tests/python/test_dict_method_result_ownership.py" in gate.TEST_INPUTS


def test_gate_selects_exact_two_root_pcc0_node_and_program_source():
    from tests.python.test_slot_call_exception_cleanup import TWO_ROOT_PROGRAM

    assert gate.NODES[4:] == (
        "tests/python/test_slot_call_exception_cleanup.py::"
        "test_two_root_cleanup_error_path_native_five_gc[pcc0]",
    )
    assert gate.expected_programs()["two-root-cleanup"] == (
        TWO_ROOT_PROGRAM, "TWO_ROOT_CLEANUP_EXCEPTION_OK\n",
    )
    assert set(prior.TWO_ROOT_CLEANUP_INPUTS) <= set(gate.TEST_INPUTS)
    assert len(gate.NODES) == len(set(gate.NODES)) == 5


@pytest.mark.parametrize("machine", (62, 183))
@pytest.mark.parametrize("program_header", (1, 2, 3))
def test_elf_admission_checks_program_headers_without_external_tools(tmp_path, machine, program_header):
    from pcc.backend.elf_x86_64 import ElfError

    payload = bytearray(128)
    ident = b"\x7fELF\x02\x01\x01" + bytes(9)
    struct.pack_into("<16sHHIQQQIHHHHHH", payload, 0,
                     ident, 2, machine, 1, 0x400000, 64, 0, 0, 64, 56, 1, 0, 0, 0)
    struct.pack_into("<IIQQQQQQ", payload, 64,
                     program_header, 5, 0, 0x400000, 0x400000, 128, 128, 4096)
    binary = tmp_path / "program.out"
    binary.write_bytes(payload)
    if program_header == 1:
        assert gate.verify_native_image(binary) == {"format": "static-elf", "libraries": []}
    else:
        with pytest.raises(ElfError, match="unexpected program header"):
            gate.verify_native_image(binary)


@pytest.mark.parametrize("libraries", (
    ["kernel32.dll"], ["kernel32.dll", "ws2_32.dll"], [],
    ["python313.dll"], ["libLLVM.dll"], ["custom.dll"],
))
def test_windows_admission_allows_only_named_system_libraries(tmp_path, monkeypatch, libraries):
    from scripts import verify_nolibpython

    binary = tmp_path / "program.out"
    binary.write_bytes(b"MZ host-only image-parser control")
    monkeypatch.setattr(verify_nolibpython, "linked_libraries", lambda payload: libraries)
    if libraries and set(libraries) <= gate.WINDOWS_SYSTEM_LIBRARIES:
        assert gate.verify_native_image(binary) == {"format": "pe", "libraries": libraries}
    else:
        with pytest.raises(AssertionError):
            gate.verify_native_image(binary)


@pytest.mark.parametrize("fail_gc", (None, 2))
def test_shared_helper_runs_absolute_native_paths_with_no_host_fallback(tmp_path, monkeypatch, fail_gc):
    calls = []
    archive = tmp_path / "runtime.a"
    archive.write_bytes(b"host-only archive")
    monkeypatch.setenv("LC_ALL", "C")

    def execute(command, *, env, timeout):
        calls.append((command, env, timeout))
        rc = 124 if env.get("PCC_GC_BACKEND") == str(fail_gc) else 0
        return subprocess.CompletedProcess(command, rc, stdout="ok\n", stderr="")

    def compile_program(source, binary, **options):
        assert options == {"backend": "self", "libpython_mode": "off",
                           "ir_scaffold_mode": "on", "runtime_archive": str(archive)}
        Path(binary).write_bytes(b"host-only executable placeholder")

    monkeypatch.setattr(support, "run_process_group_timeout", execute)
    capfd = SimpleNamespace(readouterr=lambda: SimpleNamespace(out="", err=""))
    args = ("print('ok')\n", "ok\n", tmp_path, compile_program, "pcc0", archive, capfd)
    if fail_gc is None:
        support.assert_owned_program(*args)
    else:
        with pytest.raises(AssertionError):
            support.assert_owned_program(*args)
    assert calls[0][0] == [sys.executable, str(tmp_path / "program.py")]
    for gc, (command, env, timeout) in enumerate(calls[1:]):
        assert command == [str(tmp_path / "program.out")]
        assert Path(command[0]).is_absolute()
        assert env["PCC_GC_BACKEND"] == str(gc) and env["PATH"] == ""
        assert env["PCC_HOST_PYTHON"] == "/nonexistent/host-python"
        assert env["PCC_HOST_PCC"] == "/nonexistent/host-pcc"
        assert env["PCC_TEST_NO_NATIVE_PROVISIONING"] == env["PCC_NO_AUTO_PCC1"] == "1"
        assert "LC_ALL" not in env and timeout == 10
    record = json.loads((tmp_path / "ownership-regression.json").read_text())
    assert record["status"] == ("PASS" if fail_gc is None else "NATIVE_EXECUTION_FAILED")
    assert len(record["executions"]) == (5 if fail_gc is None else 3)


def test_super_regression_uses_explicit_owned_helper_without_pcc1(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python
    from tests.python import test_super_call_result_roots as probe

    calls = []
    monkeypatch.setattr(probe, "assert_owned_program", lambda *args: calls.append(args))
    archive, capfd = object(), object()
    probe.test_super_result_native_matches_cpython_under_five_collectors(tmp_path, archive, capfd)
    assert calls == [(probe.NATIVE_SOURCE, "5\nTrue\ncaught\n", tmp_path,
                      compile_python, "pcc0", archive, capfd)]
    tree = ast.parse((ROOT / "tests/python/test_super_call_result_roots.py").read_text())
    native = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                  and node.name == gate.NODES[1].split("::")[1])
    assert [arg.arg for arg in native.args.args] == ["tmp_path", "explicit_owned_runtime", "capfd"]
    assert [ast.unparse(mark) for mark in native.decorator_list] == ["pytest.mark.integration"]


@pytest.mark.parametrize("method", ("copy", "pop"))
def test_dict_regressions_reuse_the_explicit_owned_helper(tmp_path, monkeypatch, method):
    from tests.python import test_dict_method_result_ownership as probe

    calls = []
    monkeypatch.setattr(probe, "assert_owned_program", lambda *args, **kwargs: calls.append((args, kwargs)))
    compiler, archive, capfd = object(), object(), object()
    request = SimpleNamespace(node=SimpleNamespace(
        callspec=SimpleNamespace(params={"python_program_compiler": "pcc0"}),
    ))
    probe.test_dict_method_result_owners_native_five_gc(
        compiler, request, archive, tmp_path, capfd, method,
    )
    assert calls == [((probe._program(method), "DICT_METHOD_OWNERS_OK\n", tmp_path,
                      compiler, "pcc0", archive, capfd), {"provenance_probe": "2"})]


def test_two_root_regression_reuses_the_explicit_owned_helper(tmp_path, monkeypatch):
    from tests.python import test_slot_call_exception_cleanup as probe

    calls = []
    monkeypatch.setattr(probe, "assert_owned_program", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(probe, "_assert_two_root_program_error_edge", lambda: "host-only IR control\n")
    compiler, archive, capfd = object(), object(), object()
    request = SimpleNamespace(node=SimpleNamespace(
        callspec=SimpleNamespace(params={"python_program_compiler": "pcc0"}),
    ))
    probe.test_two_root_cleanup_error_path_native_five_gc(
        compiler, request, archive, tmp_path, capfd,
    )
    assert calls == [((probe.TWO_ROOT_PROGRAM, "TWO_ROOT_CLEANUP_EXCEPTION_OK\n", tmp_path,
                      compiler, "pcc0", archive, capfd), {"provenance_probe": "2"})]
    assert (tmp_path / "two-root-cleanup.ll").read_text() == "host-only IR control\n"
    tree = ast.parse((ROOT / gate.NODES[4].split("::")[0]).read_text())
    native = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                  and node.name == "test_two_root_cleanup_error_path_native_five_gc")
    assert [arg.arg for arg in native.args.args] == [
        "python_program_compiler", "request", "explicit_owned_runtime", "tmp_path", "capfd",
    ]
    assert ast.unparse(native.decorator_list[0]) == "pytest.mark.integration"
    fixture_mark = native.decorator_list[1]
    assert ast.unparse(fixture_mark.func) == "pytest.mark.parametrize"
    assert [ast.literal_eval(arg) for arg in fixture_mark.args] == [
        "python_program_compiler", ("pcc0", "pcc1"),
    ]
    assert [(keyword.arg, ast.literal_eval(keyword.value)) for keyword in fixture_mark.keywords] == [
        ("indirect", True),
    ]


def test_explicit_fixture_retains_single_thread_config_and_never_requests_pcc1(tmp_path, monkeypatch):
    from pcc.tools import runtime_archive_provenance

    archive = tmp_path / "libpy_runtime_pcc_py.a"
    archive.write_bytes(b"host-only archive placeholder")
    Path(str(archive) + ".provenance.json").write_text("{}")
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(archive))
    monkeypatch.setenv("PCC_WITH_THREADS", "0")
    monkeypatch.setenv("PCC_REFCOUNT_KIND", "atomic")
    monkeypatch.setattr(runtime_archive_provenance, "manifest_is_stale_for_current_codegen", lambda _: False)
    fixtures = []

    def getfixturevalue(name):
        fixtures.append(name)
        assert name == "pcc_runtime_archive"
        return archive

    request = SimpleNamespace(getfixturevalue=getfixturevalue)
    assert support.explicit_owned_runtime.__wrapped__(request, monkeypatch) == archive
    assert fixtures == ["pcc_runtime_archive"]
    import os

    assert os.environ["PCC_WITH_THREADS"] == "0"
    assert os.environ["PCC_REFCOUNT_KIND"] == "atomic"
    assert os.environ["PCC_TEST_NO_NATIVE_PROVISIONING"] == os.environ["PCC_NO_AUTO_PCC1"] == "1"
    assert os.environ["PATH"] == ""


def test_workflow_runs_post_stage1_without_building_or_widening_the_job(tmp_path):
    assert gate.source_identity is prior.source_identity
    text = (ROOT / ".github/workflows/pcc1-package-parity.yml").read_text()
    other = text.split("  other-platform-native-wheel:\n", 1)[1].split("  pcc1-package-parity:\n", 1)[0]
    name = "Verify native object ownership and cleanup with the existing runtime"
    step = other.split("      - name: " + name + "\n", 1)[1].split("      - name:", 1)[0]
    assert other.index("id: platform_qualification") < other.index("id: platform_runtime") < other.index(name)
    assert other.index(name) < other.index("--phase pcc1")
    assert "!cancelled()" in step
    assert "steps.platform_qualification.outcome == 'success'" in step
    assert "steps.platform_qualification.outcome == 'failure'" in step
    assert "steps.platform_runtime.outputs.ready" not in step
    assert "PCC_RUNTIME_ARCHIVE: ${{ steps.platform_runtime.outputs.archive }}" in step
    assert "timeout-minutes: 6" in step and "timeout-minutes: 360" in other
    for key, value in {"PCC_TEST_NO_NATIVE_PROVISIONING": "1", "PCC_NO_AUTO_PCC1": "1",
                       "PCC_WITH_THREADS": "1", "PCC_REFCOUNT_KIND": "atomic",
                       "PCC_PYTEST_AUTO_CLEAN": "0", "UV_PYTHON_DOWNLOADS": "never"}.items():
        assert f'{key}: "{value}"' in step
    command = "uv run --frozen --no-sync python scripts/ci_native_object_regressions.py --out-dir build/worker-prior-gate"
    assert "run: " + command in step
    assert "download-artifact" not in step and "continue-on-error" not in step
    host_name = "Verify host Stage2 export-retention, AArch64 call-emission, and ELF assembly contracts"
    host_header = "      - name: " + host_name + "\n"
    assert other.count(host_header) == 1
    host_step = other.split(host_header, 1)[1].split("      - name:", 1)[0]
    host_commands = [line.removeprefix("        run: ") for line in host_step.splitlines()
                     if line.startswith("        run: ")]
    assert len(host_commands) == 1
    host_command, linux_condition = host_commands[0].split(" ${{ ", 1)
    assert host_command.split() == [
        "uv", "run", "--frozen", "--no-sync", "python", "-m", "pytest", "-x", "-q",
        "tests/python/test_stage2_export_capture.py",
        "tests/python/test_aarch64_call_target_reuse.py",
        "tests/python/test_x86_jump_threading.py",
        "tests/python/test_owned_elf_assembler_dispatch.py",
        "tests/python/test_elf_arm_branch_slot_defaults.py",
        "tests/python/test_gc_phase_attribution.py",
        "tests/python/test_gc_phase_counter_widths.py",
        "tests/test_ci_native_object_regressions.py",
        "tests/test_ci_native_ordering_gate.py",
        "tests/python/test_user_ordering_exhaustion.py",
    ]
    linux_only = (
        "tests/test_ci_stage1_artifact_export.py",
        "tests/python/test_gc4_copy_page_probe_native.py",
        "tests/python/test_gc_relocation_membership_lifecycle.py",
        "tests/python/test_gc_remembered_membership_lifecycle.py",
        "tests/python/test_gc_remembered_membership_native.py",
        "tests/python/test_owned_runtime_pass_prefix.py",
        "tests/python/test_pipeline_ir_split.py",
        "tests/python/test_pipeline_ir_frame_maps.py",
        "tests/python/test_x86_64_branch_syntax_cache.py",
        "tests/python/test_x86_64_instruction_encoding_cache.py",
        "tests/python/test_x86_64_instruction_encoding_cache_lowering.py",
        "tests/python/test_x86_64_operand_cache.py",
    )
    assert linux_condition == (
        "matrix.platform == 'linux-x86_64' && '" + " ".join(linux_only) + "' || '' }}")
    for path in (*[node.split("::")[0] for node in (*gate.NODES, *gate.WINDOWS_NODES)],
                 "scripts/ci_native_object_regressions.py", "tests/test_ci_native_object_regressions.py",
                 "tests/python/test_elf_arm_branch_slot_defaults.py",
                 "tests/python/test_gc_phase_attribution.py",
                 "tests/python/test_gc_phase_counter_widths.py",
                 "tests/elf_arm_branch_slot_reference.py",
                 "tests/elf_x86_packed_classification_reference.py",
                 *linux_only, "tests/python/test_freestanding_gc_relocation_copy.py",
                 "tests/python/fixtures/gc4_copy_page_probe_native.c"):
        assert text.count('      - "' + path + '"') == 2
    for suffix in ("json", "jsonl", "stdout", "stderr", "tsv", "xml"):
        assert "build/worker-prior-gate/**/*." + suffix in other
    assert gate.TIMEOUT_S == 300 and len(gate.NODES) == 5
    assert "[pcc0]" in gate.NODES[0] and all("pcc1" not in node for node in gate.NODES)
    command = prior.pytest_command(tmp_path, gate.NODES, integration=True)
    assert command[-7:] == ["-m", "integration", *gate.NODES]
    assert "-x" in command
    assert not any(option in command for option in ("-n", "-n0", "--dist", "-o", "--override-ini"))
    assert '-n 6 --dist=loadgroup' in (ROOT / "pyproject.toml").read_text()


def _win64_receipt(directory, runtime, run_id):
    import hashlib
    from pcc.backend.target_objects import encode_assembly_object
    from tests.python.test_windows_direct_scalar_call_args import (
        HOME_CALLBACK_ASM,
        MANAGED_CALLBACK_SOURCE,
        TARGET,
    )

    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "win64_home_callback.py"
    source.write_text(MANAGED_CALLBACK_SOURCE)
    helper = directory / "win64_home_callback.obj"
    helper.write_bytes(encode_assembly_object(HOME_CALLBACK_ASM, TARGET))
    binary = directory / "win64_home_callback.exe"
    payload = bytearray(70)
    payload[:2] = b"MZ"
    payload[60:64] = (64).to_bytes(4, "little")
    payload[64:] = b"PE\0\0\x64\x86"
    binary.write_bytes(payload)
    record = {
        "schema": "pcc.win64-home-callback.v1", "status": "PASS", "run_id": run_id,
        "compiler_mode": "host-pcc0", "backend": "self", "libpython": "off", "target": TARGET,
        "source_sha256": gate.sha(source),
        "helper_assembly_sha256": hashlib.sha256(HOME_CALLBACK_ASM.encode()).hexdigest(),
        "helper_sha256": gate.sha(helper), "runtime_archive": runtime["path"],
        "runtime_sha256": runtime["hashes"][""],
        "binary_path": str(binary), "binary_sha256": gate.sha(binary),
        "executions": [{"requested_gc_backend": gc, "command": [str(binary)],
                        "returncode": 0, "stdout": "WIN64_HOME_ROOT_CALLBACK_OK\n", "stderr": ""}
                       for gc in range(5)],
    }
    path = directory / "win64-home-callback.json"
    path.write_text(json.dumps(record))
    return path


def test_win64_native_extension_selects_only_windows_and_binds_its_inputs():
    expected = (prior.WIN64_DIRECT_SCALAR_TEST +
                "::test_native_win64_home_space_and_rooted_callback_five_collectors",)
    assert gate.WINDOWS_NODES == expected
    assert gate.selected_nodes("win32") == (*gate.NODES, *expected)
    assert gate.selected_nodes("linux") == gate.NODES
    assert gate.selected_nodes("darwin") == gate.NODES
    assert set(prior.WIN64_DIRECT_SCALAR_INPUTS) <= set(gate.TEST_INPUTS)
    command = prior.pytest_command(Path("host-only"), gate.selected_nodes("win32"), integration=True)
    assert command[-8:] == ["-m", "integration", *gate.NODES, *expected]
    assert "-x" in command
    assert not any(option in command for option in ("-n", "-n0", "--dist", "-o", "--override-ini"))


@pytest.mark.parametrize("stage_status", ("PASS", "FAIL"))
def test_win64_extension_reuses_admission_in_one_existing_gate_call(
    tmp_path, monkeypatch, admitted, stage_status,
):
    _source, runtime = admitted
    from tests.python.test_windows_direct_scalar_call_args import TARGET

    runtime["target"] = TARGET
    stage_path = tmp_path / "stage1.json"
    stage = json.loads(stage_path.read_text())
    stage.update(status=stage_status, runtime=runtime)
    stage_path.write_text(json.dumps(stage))
    nodes = (*gate.NODES, *gate.WINDOWS_NODES)
    monkeypatch.setattr(gate, "selected_nodes", lambda: nodes)
    monkeypatch.setattr(gate, "verify_native_image", lambda path: {"format": "pe", "libraries": ["kernel32.dll"]})
    calls = []

    def run(out, name, selected, env, **options):
        calls.append((selected, env, options))
        _receipts(out / name, runtime)
        _win64_receipt(out / name / "win64", runtime, env["PCC_NATIVE_OBJECT_GATE_RUN_ID"])

    monkeypatch.setattr(gate, "run_pytest", run)
    result = gate.run(tmp_path)
    assert len(calls) == 1
    selected, env, options = calls[0]
    assert selected == nodes and options == {"integration": True, "timeout": 300}
    assert env["PCC_RUNTIME_ARCHIVE"] == runtime["path"]
    assert env["PCC_NATIVE_OBJECT_GATE_RUN_ID"] == result["run_id"]
    assert env["PCC_TEST_NO_NATIVE_PROVISIONING"] == "1" and env["UV_PYTHON_DOWNLOADS"] == "never"
    assert result["status"] == "PASS" and result["nodes"] == list(nodes)
    assert result["tree_rss_limit_bytes"] == 17179869184
    assert result["observed_collector_witness"] is False
    assert len(result["programs"]) == 6
    assert sum(len(row["executions"]) for row in result["programs"].values()) == 30
    extra = result["programs"]["win64-home-callback"]
    assert extra["compiler_mode"] == "host-pcc0" and extra["observed_collector_witness"] is True
    assert [row["requested_gc_backend"] for row in extra["executions"]] == list(range(5))
    assert result["source"] == result["source_after"]
    assert result["runtime"] == result["runtime_after"] == runtime


@pytest.mark.parametrize("change", (
    "missing", "duplicate", "stale", "running", "schema", "pcc1", "llvm", "libpython",
    "target", "runtime-target", "runtime-path", "runtime-digest", "source", "source-digest",
    "assembly-digest", "helper", "helper-digest", "binary-path", "binary-digest", "machine",
    "image-format", "missing-gc", "duplicate-gc", "boolean-gc", "failed-gc", "wrong-output",
    "stderr", "wrong-command",
))
def test_win64_receipt_rejects_missing_stale_or_mismatched_evidence(
    tmp_path, monkeypatch, admitted, change,
):
    _source, runtime = admitted
    from tests.python.test_windows_direct_scalar_call_args import TARGET

    runtime["target"] = TARGET
    monkeypatch.setattr(gate, "verify_native_image", lambda path: {"format": "pe", "libraries": ["kernel32.dll"]})
    directory = tmp_path / "receipts"
    path = _win64_receipt(directory / "win64", runtime, "current-run")
    record = json.loads(path.read_text())
    fields = {
        "stale": ("run_id", "old-run"), "running": ("status", "RUNNING"),
        "schema": ("schema", "other"), "pcc1": ("compiler_mode", "pcc1"),
        "llvm": ("backend", "llvm"), "libpython": ("libpython", "on"),
        "target": ("target", "aarch64-pc-windows-msvc"),
        "runtime-path": ("runtime_archive", "other.a"), "runtime-digest": ("runtime_sha256", "stale"),
        "source-digest": ("source_sha256", "stale"),
        "assembly-digest": ("helper_assembly_sha256", "stale"),
        "helper-digest": ("helper_sha256", "stale"),
        "binary-path": ("binary_path", "other.exe"), "binary-digest": ("binary_sha256", "stale"),
    }
    if change in fields:
        key, value = fields[change]
        record[key] = value
    elif change == "runtime-target":
        runtime["target"] = "aarch64-pc-windows-msvc"
    elif change in ("source", "helper"):
        target = path.parent / ("win64_home_callback.py" if change == "source" else "win64_home_callback.obj")
        target.write_bytes(b"unexpected source or helper")
        record[change + "_sha256"] = gate.sha(target)
    elif change == "machine":
        binary = path.parent / "win64_home_callback.exe"
        raw = bytearray(binary.read_bytes())
        raw[68:70] = b"\x64\xaa"
        binary.write_bytes(raw)
        record["binary_sha256"] = gate.sha(binary)
    elif change == "image-format":
        monkeypatch.setattr(gate, "verify_native_image", lambda path: {"format": "static-elf"})
    elif change == "missing-gc":
        record["executions"].pop()
    elif change == "duplicate-gc":
        record["executions"][-1]["requested_gc_backend"] = 0
    elif change == "boolean-gc":
        record["executions"][1]["requested_gc_backend"] = True
    elif change == "failed-gc":
        record["executions"][2]["returncode"] = 124
    elif change == "wrong-output":
        record["executions"][2]["stdout"] = "616\n"
    elif change == "stderr":
        record["executions"][2]["stderr"] = "diagnostic"
    elif change == "wrong-command":
        record["executions"][2]["command"] = [sys.executable]
    path.write_text(json.dumps(record))
    if change == "missing":
        path.unlink()
    elif change == "duplicate":
        _win64_receipt(directory / "duplicate", runtime, "current-run")
    with pytest.raises(AssertionError):
        gate.verify_win64_callback_receipt(directory, runtime, "current-run")


@pytest.mark.parametrize("failure", (None, "compile", "collector", "runtime-drift"))
def test_win64_native_probe_records_one_compile_and_fails_closed(
    tmp_path, monkeypatch, admitted, failure,
):
    _source, runtime = admitted
    from pcc.frontends.python import pipeline
    from tests.python import test_windows_direct_scalar_call_args as probe

    runtime["target"] = probe.TARGET
    monkeypatch.setenv("PCC_NATIVE_OBJECT_GATE_RUN_ID", "host-control-run")
    calls, executions = [], []
    expected = tmp_path / "expected"
    expected_path = _win64_receipt(expected, runtime, "host-control-run")
    expected_binary = (expected_path.parent / "win64_home_callback.exe").read_bytes()
    work = tmp_path / "probe"
    work.mkdir()

    def compile_program(source, binary, **options):
        calls.append((source, binary, options))
        if failure == "compile":
            raise RuntimeError("deliberate compile failure")
        Path(binary).write_bytes(expected_binary)

    def execute(command, *, env, timeout):
        backend = int(env["PCC_GC_BACKEND"])
        executions.append((command, env, timeout))
        assert command == [str(work / "win64_home_callback.exe")] and timeout == 10
        if failure == "runtime-drift" and backend == 4:
            Path(runtime["path"]).write_bytes(b"changed runtime")
        return subprocess.CompletedProcess(command, 124 if failure == "collector" and backend == 2 else 0,
                                           stdout="WIN64_HOME_ROOT_CALLBACK_OK\n", stderr="")

    monkeypatch.setattr(pipeline, "compile_python", compile_program)
    monkeypatch.setattr(probe, "run_process_group_timeout", execute)
    monkeypatch.setattr(gate, "verify_native_image", lambda path: {"format": "pe", "libraries": ["kernel32.dll"]})
    capfd = SimpleNamespace(readouterr=lambda: SimpleNamespace(out="", err=""))
    if failure is None:
        probe.test_native_win64_home_space_and_rooted_callback_five_collectors(work, Path(runtime["path"]), capfd)
        verified = gate.verify_win64_callback_receipt(work, runtime, "host-control-run")
        assert verified["status"] == "PASS" and verified["compiler_mode"] == "host-pcc0"
    else:
        with pytest.raises((AssertionError, RuntimeError)):
            probe.test_native_win64_home_space_and_rooted_callback_five_collectors(work, Path(runtime["path"]), capfd)
    assert len(calls) == 1
    source, binary, options = calls[0]
    assert options == {"backend": "self", "libpython_mode": "off", "ir_scaffold_mode": "on",
                       "runtime_archive": runtime["path"], "object_inputs": (str(work / "win64_home_callback.obj"),),
                       "target_triple": probe.TARGET}
    record = json.loads((work / "win64-home-callback.json").read_text())
    assert record["status"] == ("PASS" if failure is None else "FAIL")
    assert len(executions) == (0 if failure == "compile" else 3 if failure == "collector" else 5)
    assert "assert result == 616" in probe.MANAGED_CALLBACK_SOURCE
    assert 'assert caller_only[0]["answer"] == 73' in probe.MANAGED_CALLBACK_SOURCE
    assert 'assert collector() == int(os.environ["PCC_GC_BACKEND"])' in probe.MANAGED_CALLBACK_SOURCE
