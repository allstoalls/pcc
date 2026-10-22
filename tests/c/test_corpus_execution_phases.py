"""Build errors cannot masquerade as completed GCC/c-testsuite programs."""
from pathlib import Path
import subprocess

import pytest

from tests import gcc_torture_cases, c_testsuite_cases
from tests.c import test_gcc_torture_execute, test_c_testsuite
from tests.c import test_gcc_torture_self, test_c_testsuite_self
from tests.corpus_execution_phases import CStageResult, PccCompileResult
from tests.worker_process import WorkerProcessResult


HELPERS = [gcc_torture_cases, c_testsuite_cases]
SELF_LANES = [test_gcc_torture_self, test_c_testsuite_self]


def outcome(stage, returncode=1, *, completed=True):
    return PccCompileResult.from_stages((CStageResult(stage, returncode, completed=completed),))


class Connection:
    payload = None
    closed = False

    def send(self, payload):
        self.payload = payload

    def close(self):
        self.closed = True


@pytest.mark.parametrize("helper", HELPERS, ids=["gcc", "c-testsuite"])
@pytest.mark.parametrize("failed_stage", ["compile", "link", None])
def test_reference_records_compile_link_and_completed_nonzero_run(tmp_path, monkeypatch, helper, failed_stage):
    case = tmp_path / "case.c"
    case.write_text("int main(void) { return 1; }\n")
    helper.run_native.cache_clear()
    monkeypatch.setattr(helper, "_host_cc", lambda: "/reference/cc")
    calls = []

    def run(command, **options):
        stage = ["compile", "link", "run"][len(calls)]
        calls.append((stage, command))
        return subprocess.CompletedProcess(command, 1 if stage in (failed_stage, "run") else 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    result = helper.run_native(case, tmp_path)
    expected = ["compile", "link", "run"][:{"compile": 1, "link": 2, None: 3}[failed_stage]]
    assert [stage.stage for stage in result.stages] == expected
    assert result.executed is (failed_stage is None)
    assert "-c" in calls[0][1]
    if len(calls) > 1:
        assert any(argument.endswith("a.o") for argument in calls[1][1])
    if result.executed:
        assert result.returncode == 1
    else:
        with pytest.raises(AssertionError, match="did not execute"):
            result.require_execution("reference")


@pytest.mark.parametrize("helper", HELPERS, ids=["gcc", "c-testsuite"])
@pytest.mark.parametrize("failed_stage", ["compile", "link", "run", None])
def test_owned_worker_preserves_original_source_and_stage(tmp_path, monkeypatch, helper, failed_stage):
    case = tmp_path / "case.c"
    original = "int answer(void) { return 1; }\nint main(void) { return answer(); }\n"
    case.write_text(original)
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
                raise RuntimeError("owned link rejected the case")

    def run(command, **options):
        events.append("run")
        if failed_stage == "run":
            raise OSError("owned program could not launch")
        return subprocess.CompletedProcess(command, 1, "out", "err")

    monkeypatch.setattr(helper, "CEvaluator", Evaluator)
    monkeypatch.setattr(subprocess, "run", run)
    connection = Connection()
    helper._pcc_worker_entry("run", str(case), 20, connection)
    result = PccCompileResult.from_stages(CStageResult(**stage) for stage in connection.payload["stages"])
    assert connection.closed
    assert [stage.stage for stage in result.stages] == events
    assert result.executed is (failed_stage is None)
    assert result.returncode == 1
    assert all(stage.completed and stage.returncode == 0 for stage in result.stages[:-1])
    if failed_stage is not None:
        assert result.stages[-1].completed is False


@pytest.mark.parametrize("helper", HELPERS, ids=["gcc", "c-testsuite"])
@pytest.mark.parametrize("worker_result", [WorkerProcessResult(True, -15), WorkerProcessResult(False, 1)])
def test_worker_timeout_and_crash_are_not_execution(monkeypatch, helper, worker_result):
    monkeypatch.setattr(helper, "run_worker_process", lambda *args: worker_result)
    result = helper._run_pcc_worker("run", Path("case.c"), 20)
    assert not result.executed
    assert result.stages[-1].stage == "worker"
    with pytest.raises(AssertionError, match="did not execute"):
        result.require_execution("owned pcc")


@pytest.mark.parametrize("corpus,comparison", [
    (test_gcc_torture_execute, "returncode"),
    (test_gcc_torture_execute, "exact"),
    (test_c_testsuite, "exact"),
], ids=["gcc-returncode", "gcc-exact", "c-testsuite-exact"])
@pytest.mark.parametrize("failed_side", ["native", "pcc", None])
def test_original_runtime_assertions_require_actual_execution(tmp_path, monkeypatch, corpus, comparison, failed_side):
    case = tmp_path / "case.c"
    case.write_text("int main(void) { return 1; }\n")
    native = outcome("compile" if failed_side == "native" else "run")
    owned = outcome("link" if failed_side == "pcc" else "run")
    if corpus is test_gcc_torture_execute:
        monkeypatch.setattr(corpus, "GCC_TORTURE_DIR", tmp_path)
        monkeypatch.setattr(corpus, "run_native_and_pcc", lambda *args: (native, owned))
        prefix = "test_gcc_torture_runtime_"
    else:
        monkeypatch.setattr(corpus, "C_TESTSUITE_DIR", tmp_path)
        monkeypatch.setattr(corpus, "run_native", lambda *args: native)
        monkeypatch.setattr(corpus, "run_pcc", lambda *args: owned)
        prefix = "test_c_testsuite_runtime_"
    function = getattr(corpus, prefix + ("returncode_matches_native" if comparison == "returncode" else "matches_native_exactly"))
    if failed_side is None:
        function("case.c")
    else:
        with pytest.raises(AssertionError, match="did not execute"):
            function("case.c")


@pytest.mark.parametrize("lane", SELF_LANES, ids=["gcc", "c-testsuite"])
@pytest.mark.parametrize("failed_stage", ["compile", "link", None])
def test_direct_self_lane_keeps_phase_evidence(tmp_path, monkeypatch, lane, failed_stage):
    case = tmp_path / "case.c"
    case.write_text("int main(void) { return 1; }\n")
    lane._run_backend.cache_clear()
    monkeypatch.setattr(lane, "CEvaluator", lambda **kwargs: object())

    def run(evaluator, units, **options):
        for stage in ("compile", "link", "run"):
            options["on_stage"](stage)
            if stage == failed_stage:
                raise RuntimeError("owned " + stage + " failed")
        return subprocess.CompletedProcess([], 1, "", "")

    monkeypatch.setattr(lane, "run_owned_c_corpus", run)
    result = lane._run_self_backend(case)
    assert result.executed is (failed_stage is None)
    assert result.stages[-1].stage == (failed_stage or "run")
    assert result.returncode == 1


@pytest.mark.integration
@pytest.mark.parametrize("helper", HELPERS, ids=["gcc", "c-testsuite"])
def test_real_owned_exit_one_cannot_match_compile_failure(tmp_path, monkeypatch, pcc_runtime_archive, helper):
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(pcc_runtime_archive))
    valid = tmp_path / "exit_one.c"
    valid.write_text("int answer(void) { return 1; }\nint main(void) { return answer(); }\n")
    invalid = tmp_path / "compile_failure.c"
    invalid.write_text("int main(void) { return this is invalid C; }\n")
    good = helper.run_pcc(valid, tmp_path)
    bad = helper.run_pcc(invalid, tmp_path)
    good.require_execution("real owned exit-one program")
    assert [(stage.stage, stage.returncode) for stage in good.stages] == [("compile", 0), ("link", 0), ("run", 1)]
    assert all(stage.completed for stage in good.stages)
    assert good.stdout == good.stderr == ""
    assert bad.returncode == good.returncode == 1
    assert bad.stages[-1].stage == "compile"
    assert not bad.executed
    with pytest.raises(AssertionError, match="did not execute: compile"):
        bad.require_execution("real compile failure")


@pytest.mark.integration
@pytest.mark.parametrize("helper", HELPERS, ids=["gcc", "c-testsuite"])
def test_real_reference_exit_one_cannot_match_compile_failure(tmp_path, helper):
    valid = tmp_path / "exit_one.c"
    valid.write_text("int answer(void) { return 1; }\nint main(void) { return answer(); }\n")
    invalid = tmp_path / "compile_failure.c"
    invalid.write_text("int main(void) { return this is invalid C; }\n")
    good = helper.run_native(valid, tmp_path)
    bad = helper.run_native(invalid, tmp_path)
    good.require_execution("real external reference exit-one program")
    assert [(stage.stage, stage.returncode) for stage in good.stages] == [("compile", 0), ("link", 0), ("run", 1)]
    assert bad.returncode == good.returncode == 1
    assert bad.stages[-1].stage == "compile"
    assert not bad.executed
