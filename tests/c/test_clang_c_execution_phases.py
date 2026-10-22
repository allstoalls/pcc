"""A build or worker failure must never count as a corpus program exit."""
from pathlib import Path
import subprocess

import pytest

from tests import clang_c_cases as cases
from tests.c import test_clang_c as corpus
from tests.worker_process import WorkerProcessResult
from utils.internal import refresh_clang_c_manifest as refresh


class Connection:
    payload = None
    closed = False

    def send(self, payload):
        self.payload = payload

    def close(self):
        self.closed = True


def outcome(stage, returncode=1, *, stderr="", completed=True):
    return cases.CCaseResult((cases.CStageResult(stage, returncode, stderr=stderr, completed=completed),))


@pytest.mark.parametrize("test_name", [
    "test_clang_c_runtime_succeeds_under_native_and_pcc",
    "test_clang_c_runtime_matches_native_exactly",
])
@pytest.mark.parametrize("failed_side", ["native", "pcc"])
def test_runtime_comparisons_reject_equal_build_and_program_statuses(tmp_path, monkeypatch, test_name, failed_side):
    (tmp_path / "case.c").write_text("int main(void) { return 1; }\n")
    monkeypatch.setattr(corpus, "CLANG_C_TESTS_DIR", tmp_path)
    # Matching exit code and both output streams used to pass even exact-match.
    monkeypatch.setattr(corpus, "run_native", lambda *args: outcome("compile" if failed_side == "native" else "run"))
    monkeypatch.setattr(corpus, "run_pcc", lambda *args: outcome("link" if failed_side == "pcc" else "run"))
    with pytest.raises(AssertionError, match="did not execute"):
        getattr(corpus, test_name)("case.c")


@pytest.mark.parametrize("test_name", [
    "test_clang_c_runtime_succeeds_under_native_and_pcc",
    "test_clang_c_runtime_matches_native_exactly",
])
def test_runtime_comparisons_allow_real_nonzero_exit(tmp_path, monkeypatch, test_name):
    (tmp_path / "case.c").write_text("int main(void) { return 1; }\n")
    monkeypatch.setattr(corpus, "CLANG_C_TESTS_DIR", tmp_path)
    monkeypatch.setattr(corpus, "run_native", lambda *args: outcome("run"))
    monkeypatch.setattr(corpus, "run_pcc", lambda *args: outcome("run"))
    getattr(corpus, test_name)("case.c")


@pytest.mark.parametrize("failed_stage", ["compile", "link", None])
def test_reference_retains_build_stages_and_runs_only_after_link(tmp_path, monkeypatch, failed_stage):
    path = tmp_path / "case.c"
    path.write_text("// RUN: %clang -std=gnu99 -DVALUE=1 %s\nint main(void) { return 1; }\n")
    monkeypatch.setattr(cases.shutil, "which", lambda name: "/reference/cc")
    calls = []

    def run(argv, **options):
        stage = ["compile", "link", "run"][len(calls)]
        calls.append((argv, options))
        return subprocess.CompletedProcess(argv, 1 if stage in {failed_stage, "run"} else 0, "", stage)

    monkeypatch.setattr(cases.subprocess, "run", run)
    result = cases.run_native(path, tmp_path)
    expected = ["compile", "link", "run"][:{"compile": 1, "link": 2, None: 3}[failed_stage]]
    assert [stage.stage for stage in result.stages] == expected
    assert len(calls) == len(expected)
    assert result.executed is (failed_stage is None)
    assert calls[0][0][:3] == ["/reference/cc", "-std=gnu99", "-c"]
    if len(calls) > 1:
        assert calls[1][0][:2] == ["/reference/cc", "-std=gnu99"]
        assert calls[1][0][2].endswith("a.o")
    if result.executed:
        assert result.returncode == 1
        assert len(calls[2][0]) == 1


@pytest.mark.parametrize("failed_stage", ["compile", "link", "run"])
@pytest.mark.parametrize("error", [OSError("not executable"), subprocess.TimeoutExpired("case", 20)], ids=["launch-error", "timeout"])
def test_reference_exception_never_becomes_a_completed_execution(tmp_path, monkeypatch, failed_stage, error):
    path = tmp_path / "case.c"
    path.write_text("int main(void) { return 1; }\n")
    monkeypatch.setattr(cases.shutil, "which", lambda name: "/reference/cc")
    calls = []

    def run(argv, **options):
        stage = ["compile", "link", "run"][len(calls)]
        calls.append(stage)
        if stage == failed_stage:
            raise error
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(cases.subprocess, "run", run)
    result = cases.run_native(path, tmp_path)
    assert not result.executed
    assert result.stages[-1].stage == failed_stage
    assert not result.stages[-1].completed
    assert result.returncode == (124 if isinstance(error, subprocess.TimeoutExpired) else 1)


@pytest.mark.parametrize("failed_stage", ["compile", "link", "run", None])
def test_owned_worker_keeps_failed_stage_and_completed_execution_distinct(tmp_path, monkeypatch, failed_stage):
    path = tmp_path / "case.c"
    original = "int f(void) { return 1; }\nint main(void) { return f(); }\n"
    path.write_text(original)
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", "/model/archive.a")
    events = []

    class Evaluator:
        backend = "self"
        is_cross = False

        def _normalize_opt_level(self, value):
            return 2

        def compile_translation_units(self, units, **options):
            assert units[0].source == original
            assert options["use_system_cpp"] is False
            events.append("compile")
            if failed_stage == "compile":
                raise RuntimeError("owned compile rejected the case")
            return units

        def emit_executable(self, compiled, output, **options):
            events.append("link")
            if failed_stage == "link":
                raise RuntimeError("owned linker rejected the case")

    def execute(argv, **options):
        events.append("run")
        if failed_stage == "run":
            raise OSError("owned executable could not be launched")
        return subprocess.CompletedProcess(argv, 1, "out", "err")

    monkeypatch.setattr(cases, "CEvaluator", Evaluator)
    monkeypatch.setattr(subprocess, "run", execute)
    connection = Connection()
    cases._pcc_worker_entry("run", str(path), 19, connection)
    assert connection.closed
    result = cases.CCaseResult(tuple(cases.CStageResult(**stage) for stage in connection.payload["stages"]))
    assert [stage.stage for stage in result.stages] == events
    assert all(stage.returncode == 0 and stage.completed for stage in result.stages[:-1])
    assert result.executed is (failed_stage is None)
    assert result.returncode == 1
    if result.executed:
        assert (result.stdout, result.stderr) == ("out", "err")
    else:
        assert not result.stages[-1].completed


@pytest.mark.parametrize("worker_result", [WorkerProcessResult(True, -15), WorkerProcessResult(False, 1)])
def test_worker_timeout_or_crash_does_not_count_as_program_exit(monkeypatch, worker_result):
    monkeypatch.setattr(cases, "run_worker_process", lambda *args: worker_result)
    result = cases.run_pcc(Path("case.c"), Path("."))
    assert not result.executed
    assert result.stages[-1].stage == "worker"
    with pytest.raises(AssertionError, match="did not execute"):
        result.require_execution("owned pcc")


@pytest.mark.parametrize("stage", ["compile", "link", "worker"])
def test_manifest_refresh_does_not_classify_build_failure_as_runtime_match(monkeypatch, stage):
    monkeypatch.setattr(refresh, "run_native", lambda *args: outcome("run"))
    monkeypatch.setattr(refresh, "run_pcc", lambda *args: outcome(stage))
    assert refresh._classify_runtime(Path("case.c")) == "runtime_build_or_execution_failure"


def test_manifest_refresh_keeps_executed_nonzero_exact_match(monkeypatch):
    monkeypatch.setattr(refresh, "run_native", lambda *args: outcome("run"))
    monkeypatch.setattr(refresh, "run_pcc", lambda *args: outcome("run"))
    assert refresh._classify_runtime(Path("case.c")) == "runtime_exact_match"


@pytest.mark.parametrize("native,pcc,expected", [
    pytest.param(outcome("compile", completed=False), outcome("run", 124),
                 "runtime_build_or_execution_failure", id="native-compile-failure"),
    pytest.param(outcome("run", 124), outcome("link", completed=False),
                 "runtime_build_or_execution_failure", id="pcc-link-failure"),
    pytest.param(outcome("run", 124), outcome("worker", completed=False),
                 "runtime_build_or_execution_failure", id="pcc-worker-failure"),
    pytest.param(outcome("compile", 124), outcome("run", 124),
                 "runtime_build_or_execution_failure", id="completed-compiler-exit-124"),
    pytest.param(outcome("run", 124), outcome("worker", 124, completed=False),
                 "runtime_timeout", id="incomplete-worker-timeout"),
    pytest.param(outcome("run", 124), outcome("run", 124),
                 "runtime_exact_match", id="completed-program-exit-124"),
])
def test_manifest_refresh_distinguishes_exit_124_from_timeout(
    monkeypatch, native, pcc, expected,
):
    monkeypatch.setattr(refresh, "run_native", lambda *args: native)
    monkeypatch.setattr(refresh, "run_pcc", lambda *args: pcc)
    assert refresh._classify_runtime(Path("case.c")) == expected


@pytest.mark.parametrize("runner,error,expected", [
    pytest.param("run_native", OSError("cannot launch reference"),
                 "runtime_build_or_execution_failure", id="native-launch-error"),
    pytest.param("run_pcc", RuntimeError("worker failed"),
                 "runtime_build_or_execution_failure", id="pcc-runner-error"),
    pytest.param("run_native", subprocess.TimeoutExpired("reference", 20),
                 "runtime_timeout", id="native-timeout"),
    pytest.param("run_pcc", subprocess.TimeoutExpired("worker", 20),
                 "runtime_timeout", id="pcc-timeout"),
])
def test_manifest_refresh_classifies_runner_exceptions(monkeypatch, runner, error, expected):
    monkeypatch.setattr(refresh, "run_native", lambda *args: outcome("run", 124))
    monkeypatch.setattr(refresh, "run_pcc", lambda *args: outcome("run", 124))

    def fail(*args):
        raise error

    monkeypatch.setattr(refresh, runner, fail)
    assert refresh._classify_runtime(Path("case.c")) == expected


def test_compile_rejection_category_keeps_original_intent(tmp_path, monkeypatch):
    (tmp_path / "case.c").write_text("invalid fixture\n")
    monkeypatch.setattr(corpus, "CLANG_C_TESTS_DIR", tmp_path)
    monkeypatch.setattr(corpus, "compile_native", lambda *args: outcome("compile"))
    monkeypatch.setattr(corpus, "compile_pcc", lambda *args: outcome("compile"))
    corpus.test_clang_c_compile_only_both_native_and_pcc_reject_case("case.c")


@pytest.mark.integration
def test_owned_native_nonzero_exit_is_completed_execution(
    tmp_path, monkeypatch, pcc_runtime_archive,
):
    """A real owned program may legitimately exit 1 after a successful build."""
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(pcc_runtime_archive))
    case_path = tmp_path / "owned_exit_one.c"
    case_path.write_text(
        "int answer(void) { return 1; }\n"
        "int main(void) { return answer(); }\n"
    )

    result = cases.run_pcc(case_path, tmp_path)

    result.require_execution("owned native nonzero-exit program")
    assert [(stage.stage, stage.returncode) for stage in result.stages] == [
        ("compile", 0),
        ("link", 0),
        ("run", 1),
    ]
    assert all(stage.completed for stage in result.stages)
    assert result.stdout == result.stderr == ""
