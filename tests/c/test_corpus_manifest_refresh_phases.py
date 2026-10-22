"""Host models keep manifest matches limited to completed program executions."""
from pathlib import Path
import subprocess

import pytest

from tests.corpus_execution_phases import (
    CStageResult,
    ExecutionPhases,
    PccCompileResult,
)
from utils.internal import (
    refresh_c_testsuite_manifest,
    refresh_gcc_torture_manifest,
)


@pytest.fixture(params=["gcc", "c-testsuite"])
def refresh_case(request, tmp_path, monkeypatch):
    if request.param == "gcc":
        refresh = refresh_gcc_torture_manifest
        monkeypatch.setattr(refresh, "GCC_TORTURE_DIR", tmp_path)
        relative = "nested/case.c"
    else:
        refresh = refresh_c_testsuite_manifest
        monkeypatch.setattr(refresh, "C_TESTSUITE_DIR", tmp_path)
        monkeypatch.setattr(refresh, "read_expected_output", lambda path: "")
        relative = "case.c"
    case = tmp_path / relative
    case.parent.mkdir(parents=True, exist_ok=True)
    case.write_text("int main(void) { return 1; }\n")
    return refresh, case, relative


def outcome(stage="run", returncode=1, *, completed=True, stdout="", stderr=""):
    return PccCompileResult.from_stages((
        CStageResult(stage, returncode, stdout, stderr, completed),
    ))


def classify(refresh_case, monkeypatch, native, pcc):
    refresh, case, relative = refresh_case
    monkeypatch.setattr(refresh, "run_native", lambda *args, **kwargs: native)
    monkeypatch.setattr(refresh, "run_pcc", lambda *args, **kwargs: pcc)
    category, observed_relative = refresh._classify_runtime(case, timeout=7)
    assert observed_relative == relative
    assert case.read_text() == "int main(void) { return 1; }\n"
    return category


@pytest.mark.parametrize("failed_side", ["native", "pcc"])
@pytest.mark.parametrize("stage", ["compile", "link", "run", "worker"])
def test_equal_statuses_do_not_promote_incomplete_execution(
    refresh_case, monkeypatch, failed_side, stage,
):
    failed = outcome(stage, completed=False)
    results = {"native": outcome(), "pcc": outcome()}
    results[failed_side] = failed
    assert classify(refresh_case, monkeypatch, **results) == "runtime_build_or_execution_failure"


@pytest.mark.parametrize("stage", ["compile", "link", "worker"])
@pytest.mark.parametrize("returncode", [0, 1, 124])
def test_completed_nonrun_stages_are_failures_even_with_equal_statuses(
    refresh_case, monkeypatch, stage, returncode,
):
    failed = outcome(stage, returncode)
    assert classify(refresh_case, monkeypatch, failed, failed) == "runtime_build_or_execution_failure"


@pytest.mark.parametrize("returncode", [0, 1, 124])
def test_unverified_result_cannot_become_match_or_timeout(
    refresh_case, monkeypatch, returncode,
):
    unverified = PccCompileResult(returncode, "", "")
    assert classify(refresh_case, monkeypatch, unverified, outcome(returncode=returncode)) == "runtime_build_or_execution_failure"


@pytest.mark.parametrize("returncode", [0, 1, 124])
@pytest.mark.parametrize("different_stream", [None, "stdout", "stderr"])
def test_completed_program_exits_retain_normal_match_classification(
    refresh_case, monkeypatch, returncode, different_stream,
):
    streams = {} if different_stream is None else {different_stream: "different"}
    expected = "runtime_exact_match" if different_stream is None else "runtime_returncode_match_only"
    assert classify(
        refresh_case, monkeypatch,
        outcome(returncode=returncode), outcome(returncode=returncode, **streams),
    ) == expected


@pytest.mark.parametrize("native_code,pcc_code,expected", [
    (1, 2, "runtime_both_fail"),
    (124, 1, "runtime_both_fail"),
    (0, 1, "runtime_native_pass_pcc_fail"),
    (0, 124, "runtime_native_pass_pcc_fail"),
    (1, 0, "runtime_native_fail_pcc_pass"),
    (124, 0, "runtime_native_fail_pcc_pass"),
])
def test_completed_mismatches_retain_normal_classification(
    refresh_case, monkeypatch, native_code, pcc_code, expected,
):
    assert classify(
        refresh_case, monkeypatch,
        outcome(returncode=native_code), outcome(returncode=pcc_code),
    ) == expected


@pytest.mark.parametrize("failed_side", ["native", "pcc"])
@pytest.mark.parametrize("stage", ["compile", "link", "run", "worker"])
def test_recorded_timeout_is_distinct_from_completed_program_exit_124(
    refresh_case, monkeypatch, failed_side, stage,
):
    phases = ExecutionPhases()
    phases.begin(stage)
    timeout = phases.fail(subprocess.TimeoutExpired("model-stage", 7))
    assert timeout.returncode == 124
    assert not timeout.stages[-1].completed
    results = {"native": outcome(returncode=124), "pcc": outcome(returncode=124)}
    results[failed_side] = timeout
    assert classify(refresh_case, monkeypatch, **results) == "runtime_timeout"


@pytest.mark.parametrize("failed_side", ["native", "pcc"])
def test_completed_exit_124_does_not_turn_other_side_failure_into_timeout(
    refresh_case, monkeypatch, failed_side,
):
    results = {"native": outcome(returncode=124), "pcc": outcome(returncode=124)}
    results[failed_side] = outcome("link", completed=False)
    assert classify(refresh_case, monkeypatch, **results) == "runtime_build_or_execution_failure"


@pytest.mark.parametrize("failed_side", ["native", "pcc"])
@pytest.mark.parametrize("error,expected", [
    (subprocess.TimeoutExpired("model-run", 7), "runtime_timeout"),
    (OSError("cannot launch"), "runtime_build_or_execution_failure"),
    (RuntimeError("worker crashed"), "runtime_build_or_execution_failure"),
])
def test_raised_timeout_and_other_errors_remain_distinct(
    refresh_case, monkeypatch, failed_side, error, expected,
):
    refresh, case, relative = refresh_case
    monkeypatch.setattr(refresh, "run_native", lambda *args, **kwargs: outcome())
    monkeypatch.setattr(refresh, "run_pcc", lambda *args, **kwargs: outcome())

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(refresh, "run_" + failed_side, fail)
    assert refresh._classify_runtime(case, timeout=7) == (expected, relative)


@pytest.mark.parametrize("expected_output", ["", "actual", "different"])
def test_c_testsuite_expected_stdout_contract_is_preserved(
    tmp_path, monkeypatch, expected_output,
):
    refresh_case = (refresh_c_testsuite_manifest, tmp_path / "case.c", "case.c")
    refresh_case[1].write_text("int main(void) { return 1; }\n")
    monkeypatch.setattr(refresh_c_testsuite_manifest, "read_expected_output", lambda path: expected_output)
    result = outcome(returncode=0, stdout="actual")
    expected = "runtime_returncode_match_only" if expected_output == "different" else "runtime_exact_match"
    assert classify(refresh_case, monkeypatch, result, result) == expected


def test_only_recorded_timeouts_are_retried_and_all_cases_remain_visible(
    refresh_case, monkeypatch,
):
    refresh, first_case, relative = refresh_case
    second_case = first_case.with_name("retry.c")
    third_case = first_case.with_name("failure.c")
    root = refresh.GCC_TORTURE_DIR if refresh is refresh_gcc_torture_manifest else refresh.C_TESTSUITE_DIR
    names = [path.relative_to(root).as_posix() for path in (first_case, second_case, third_case)]
    calls = {}

    def run_native(path, *args, **kwargs):
        return outcome(returncode=124)

    def run_pcc(path, *args, **kwargs):
        calls[path] = calls.get(path, 0) + 1
        if path == second_case and calls[path] == 1:
            return outcome("worker", 124, completed=False)
        if path == third_case:
            return outcome("compile", 1, completed=False)
        return outcome(returncode=124)

    monkeypatch.setattr(refresh, "run_native", run_native)
    monkeypatch.setattr(refresh, "run_pcc", run_pcc)
    categories = refresh._classify_case_paths(
        [first_case, second_case, third_case], jobs=1, retry_jobs=1, timeout=7,
    )
    assert categories == {
        "runtime_build_or_execution_failure": [names[2]],
        "runtime_exact_match": sorted(names[:2]),
    }
    assert calls == {first_case: 1, second_case: 2, third_case: 1}
