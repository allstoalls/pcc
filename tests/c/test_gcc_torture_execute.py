import json
from pathlib import Path

import pytest

from tests.corpus_manifest import runtime_cases_except
from tests.gcc_torture_cases import run_native_and_pcc

REPO_ROOT = Path(__file__).absolute().parents[2]
GCC_TORTURE_DIR = REPO_ROOT / "projects" / "gcc-torture-execute"
GCC_TORTURE_MANIFEST_PATH = REPO_ROOT / "tests" / "gcc_torture_manifest.json"
GCC_TORTURE_MANIFEST = json.loads(GCC_TORTURE_MANIFEST_PATH.read_text(encoding="utf-8"))

GCC_TORTURE_RUNTIME_EXACT_MATCH_CASES = GCC_TORTURE_MANIFEST.get(
    "runtime_exact_match", []
)
GCC_TORTURE_RUNTIME_RETURNCODE_CASES = GCC_TORTURE_MANIFEST.get(
    "runtime_returncode_match_only", []
)
GCC_TORTURE_RUNTIME_COMPARISON_CASES = (
    GCC_TORTURE_RUNTIME_EXACT_MATCH_CASES
    + runtime_cases_except(
        GCC_TORTURE_MANIFEST,
        {
            "runtime_exact_match",
            "runtime_returncode_match_only",
            "runtime_both_fail",
            "runtime_native_fail_pcc_pass",
            "runtime_native_pass_pcc_fail",
        },
    )
)
GCC_TORTURE_RUNTIME_BOTH_FAIL_CASES = GCC_TORTURE_MANIFEST.get("runtime_both_fail", [])
GCC_TORTURE_RUNTIME_NATIVE_FAIL_PCC_PASS_CASES = GCC_TORTURE_MANIFEST.get(
    "runtime_native_fail_pcc_pass", []
)
GCC_TORTURE_RUNTIME_NATIVE_PASS_PCC_FAIL_CASES = GCC_TORTURE_MANIFEST.get(
    "runtime_native_pass_pcc_fail", []
)
GCC_TORTURE_RUNTIME_TIMEOUT_CASES = GCC_TORTURE_MANIFEST.get("runtime_timeout", [])

pytestmark = pytest.mark.integration


def _case_params(cases):
    return [
        pytest.param(
            relative_path,
            marks=pytest.mark.xdist_group(name=f"gcc_torture:{relative_path}"),
        )
        for relative_path in cases
    ]


def _case_path(relative_path: str) -> Path:
    return GCC_TORTURE_DIR / relative_path


if GCC_TORTURE_RUNTIME_RETURNCODE_CASES:

    @pytest.mark.parametrize(
        "relative_path", _case_params(GCC_TORTURE_RUNTIME_RETURNCODE_CASES)
    )
    def test_gcc_torture_runtime_returncode_matches_native(relative_path):
        case_path = _case_path(relative_path)
        assert case_path.is_file(), f"missing gcc torture case: {case_path}"

        native_result, pcc_result = run_native_and_pcc(case_path, REPO_ROOT)

        native_result.require_execution("host reference")
        pcc_result.require_execution("owned pcc")

        assert (
            pcc_result.returncode == native_result.returncode
        ), f"{relative_path} return code mismatch:\nnative={native_result.returncode}\npcc={pcc_result.returncode}\npcc stderr:\n{pcc_result.stderr}"


if GCC_TORTURE_RUNTIME_COMPARISON_CASES:

    @pytest.mark.parametrize(
        "relative_path", _case_params(GCC_TORTURE_RUNTIME_COMPARISON_CASES)
    )
    def test_gcc_torture_runtime_matches_native_exactly(relative_path):
        case_path = _case_path(relative_path)
        assert case_path.is_file(), f"missing gcc torture case: {case_path}"

        native_result, pcc_result = run_native_and_pcc(case_path, REPO_ROOT)

        native_result.require_execution("host reference")
        pcc_result.require_execution("owned pcc")

        assert (
            pcc_result.returncode == native_result.returncode
        ), f"{relative_path} return code mismatch:\nnative={native_result.returncode}\npcc={pcc_result.returncode}\npcc stderr:\n{pcc_result.stderr}"
        assert (
            pcc_result.stdout == native_result.stdout
        ), f"{relative_path} stdout mismatch:\nnative={native_result.stdout!r}\npcc={pcc_result.stdout!r}"
        assert (
            pcc_result.stderr == native_result.stderr
        ), f"{relative_path} stderr mismatch:\nnative={native_result.stderr!r}\npcc={pcc_result.stderr!r}"


if GCC_TORTURE_RUNTIME_BOTH_FAIL_CASES:

    @pytest.mark.parametrize(
        "relative_path", _case_params(GCC_TORTURE_RUNTIME_BOTH_FAIL_CASES)
    )
    def test_gcc_torture_runtime_both_native_and_pcc_reject_case(relative_path):
        case_path = _case_path(relative_path)
        assert case_path.is_file(), f"missing gcc torture case: {case_path}"

        native_result, pcc_result = run_native_and_pcc(case_path, REPO_ROOT)

        native_result.require_execution("host reference")
        pcc_result.require_execution("owned pcc")

        assert (
            native_result.returncode != 0
        ), f"native runtime unexpectedly succeeded for {relative_path}"
        assert pcc_result.returncode != 0, f"pcc unexpectedly accepted {relative_path}"


if GCC_TORTURE_RUNTIME_NATIVE_FAIL_PCC_PASS_CASES:

    @pytest.mark.parametrize(
        "relative_path", _case_params(GCC_TORTURE_RUNTIME_NATIVE_FAIL_PCC_PASS_CASES)
    )
    def test_gcc_torture_runtime_native_rejects_but_pcc_accepts_case(relative_path):
        case_path = _case_path(relative_path)
        assert case_path.is_file(), f"missing gcc torture case: {case_path}"

        native_result, pcc_result = run_native_and_pcc(case_path, REPO_ROOT)

        native_result.require_execution("host reference")
        pcc_result.require_execution("owned pcc")

        assert (
            native_result.returncode != 0
        ), f"native runtime unexpectedly succeeded for {relative_path}"
        assert (
            pcc_result.returncode == 0
        ), f"pcc unexpectedly rejected {relative_path}:\n{pcc_result.stderr}"


if GCC_TORTURE_RUNTIME_NATIVE_PASS_PCC_FAIL_CASES:

    @pytest.mark.parametrize(
        "relative_path", _case_params(GCC_TORTURE_RUNTIME_NATIVE_PASS_PCC_FAIL_CASES)
    )
    def test_gcc_torture_runtime_native_accepts_but_pcc_rejects_case(relative_path):
        case_path = _case_path(relative_path)
        assert case_path.is_file(), f"missing gcc torture case: {case_path}"

        native_result, pcc_result = run_native_and_pcc(case_path, REPO_ROOT)

        native_result.require_execution("host reference")
        pcc_result.require_execution("owned pcc")

        assert (
            native_result.returncode == 0
        ), f"native runtime unexpectedly rejected {relative_path}:\n{native_result.stderr}"
        assert pcc_result.returncode != 0, f"pcc unexpectedly accepted {relative_path}"


def test_gcc_torture_runtime_timeout_cases_are_explicitly_tracked():
    for relative_path in GCC_TORTURE_RUNTIME_TIMEOUT_CASES:
        assert _case_path(
            relative_path
        ).is_file(), f"missing gcc torture timeout case: {relative_path}"


def test_gcc_torture_manifest_covers_all_cases():
    manifest_files = {}
    duplicate_categories = {}
    for category, cases in GCC_TORTURE_MANIFEST.items():
        for case in cases:
            previous_category = manifest_files.setdefault(case, category)
            if previous_category != category:
                duplicate_categories.setdefault(case, {previous_category}).add(category)

    assert (
        not duplicate_categories
    ), f"duplicate manifest categories: {duplicate_categories}"

    actual_files = {
        path.relative_to(GCC_TORTURE_DIR).as_posix()
        for path in GCC_TORTURE_DIR.rglob("*.c")
    }
    assert set(manifest_files) == actual_files
