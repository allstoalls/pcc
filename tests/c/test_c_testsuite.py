import json
from pathlib import Path

import pytest

from tests.corpus_manifest import runtime_cases_except
from tests.c_testsuite_cases import read_expected_output, run_native, run_pcc

REPO_ROOT = Path(__file__).absolute().parents[2]
C_TESTSUITE_DIR = REPO_ROOT / "projects" / "c-testsuite"
C_TESTSUITE_MANIFEST_PATH = REPO_ROOT / "tests" / "c_testsuite_manifest.json"
C_TESTSUITE_MANIFEST = json.loads(C_TESTSUITE_MANIFEST_PATH.read_text(encoding="utf-8"))

C_TESTSUITE_RUNTIME_EXACT_MATCH_CASES = C_TESTSUITE_MANIFEST.get(
    "runtime_exact_match", []
)
C_TESTSUITE_RUNTIME_RETURNCODE_CASES = C_TESTSUITE_MANIFEST.get(
    "runtime_returncode_match_only", []
)
C_TESTSUITE_RUNTIME_COMPARISON_CASES = (
    C_TESTSUITE_RUNTIME_EXACT_MATCH_CASES
    + runtime_cases_except(
        C_TESTSUITE_MANIFEST,
        {"runtime_exact_match", "runtime_returncode_match_only", "runtime_native_pass_pcc_fail"},
    )
)
C_TESTSUITE_RUNTIME_NATIVE_PASS_PCC_FAIL_CASES = C_TESTSUITE_MANIFEST.get(
    "runtime_native_pass_pcc_fail", []
)
C_TESTSUITE_RUNTIME_TIMEOUT_CASES = C_TESTSUITE_MANIFEST.get("runtime_timeout", [])

pytestmark = pytest.mark.integration


def _case_params(cases):
    return [
        pytest.param(
            filename,
            marks=pytest.mark.xdist_group(name=f"c_testsuite:{filename}"),
        )
        for filename in cases
    ]


def _case_path(filename: str) -> Path:
    return C_TESTSUITE_DIR / filename


if C_TESTSUITE_RUNTIME_RETURNCODE_CASES:

    @pytest.mark.parametrize(
        "filename", _case_params(C_TESTSUITE_RUNTIME_RETURNCODE_CASES)
    )
    def test_c_testsuite_runtime_returncode_matches_native(filename):
        case_path = _case_path(filename)
        assert case_path.is_file(), f"missing c-testsuite case: {case_path}"

        native_result = run_native(case_path, REPO_ROOT)
        pcc_result = run_pcc(case_path, REPO_ROOT)

        native_result.require_execution("host reference")
        pcc_result.require_execution("owned pcc")

        assert (
            pcc_result.returncode == native_result.returncode
        ), f"{filename} return code mismatch:\nnative={native_result.returncode}\npcc={pcc_result.returncode}\npcc stderr:\n{pcc_result.stderr}"


@pytest.mark.parametrize(
    "filename", _case_params(C_TESTSUITE_RUNTIME_COMPARISON_CASES)
)
def test_c_testsuite_runtime_matches_native_exactly(filename):
    case_path = _case_path(filename)
    assert case_path.is_file(), f"missing c-testsuite case: {case_path}"

    native_result = run_native(case_path, REPO_ROOT)
    pcc_result = run_pcc(case_path, REPO_ROOT)

    native_result.require_execution("host reference")
    pcc_result.require_execution("owned pcc")

    assert (
        pcc_result.returncode == native_result.returncode
    ), f"{filename} return code mismatch:\nnative={native_result.returncode}\npcc={pcc_result.returncode}\npcc stderr:\n{pcc_result.stderr}"
    assert (
        pcc_result.stdout == native_result.stdout
    ), f"{filename} stdout mismatch:\nnative={native_result.stdout!r}\npcc={pcc_result.stdout!r}"
    assert (
        pcc_result.stderr == native_result.stderr
    ), f"{filename} stderr mismatch:\nnative={native_result.stderr!r}\npcc={pcc_result.stderr!r}"
    expected_output = read_expected_output(case_path)
    if expected_output.strip():
        assert pcc_result.stdout == expected_output, (
            f"{filename} output vs .expected mismatch:\n"
            f"expected={expected_output!r}\npcc={pcc_result.stdout!r}"
        )


if C_TESTSUITE_RUNTIME_NATIVE_PASS_PCC_FAIL_CASES:

    @pytest.mark.parametrize(
        "filename", _case_params(C_TESTSUITE_RUNTIME_NATIVE_PASS_PCC_FAIL_CASES)
    )
    def test_c_testsuite_runtime_native_accepts_but_pcc_rejects_case(filename):
        case_path = _case_path(filename)
        assert case_path.is_file(), f"missing c-testsuite case: {case_path}"

        native_result = run_native(case_path, REPO_ROOT)
        pcc_result = run_pcc(case_path, REPO_ROOT)

        native_result.require_execution("host reference")
        pcc_result.require_execution("owned pcc")

        assert (
            native_result.returncode == 0
        ), f"native runtime unexpectedly rejected {filename}:\n{native_result.stderr}"
        assert pcc_result.returncode != 0, f"pcc unexpectedly accepted {filename}"


def test_c_testsuite_runtime_timeout_cases_are_explicitly_tracked():
    for filename in C_TESTSUITE_RUNTIME_TIMEOUT_CASES:
        assert _case_path(
            filename
        ).is_file(), f"missing c-testsuite timeout case: {filename}"


def test_c_testsuite_manifest_covers_all_cases():
    manifest_files = {}
    duplicate_categories = {}
    for category, cases in C_TESTSUITE_MANIFEST.items():
        for case in cases:
            previous_category = manifest_files.setdefault(case, category)
            if previous_category != category:
                duplicate_categories.setdefault(case, {previous_category}).add(category)

    assert (
        not duplicate_categories
    ), f"duplicate manifest categories: {duplicate_categories}"

    actual_files = {path.name for path in C_TESTSUITE_DIR.glob("*.c")}
    assert set(manifest_files) == actual_files
