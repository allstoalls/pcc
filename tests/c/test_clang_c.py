import json
from pathlib import Path

import pytest

from tests.corpus_manifest import runtime_cases_except
from tests.clang_c_cases import compile_native, compile_pcc, run_native, run_pcc


REPO_ROOT = Path(__file__).absolute().parents[2]
CLANG_C_TESTS_DIR = REPO_ROOT / "projects" / "clang-c-tests"
CLANG_C_MANIFEST_PATH = REPO_ROOT / "tests" / "clang_c_manifest.json"
CLANG_C_MANIFEST = json.loads(CLANG_C_MANIFEST_PATH.read_text(encoding="utf-8"))

CLANG_C_COMPILE_ONLY_SUCCESS_CASES = CLANG_C_MANIFEST.get("compile_only_success", [])
CLANG_C_COMPILE_ONLY_BOTH_FAIL_CASES = CLANG_C_MANIFEST.get("compile_only_both_fail", [])
CLANG_C_COMPILE_ONLY_NATIVE_PASS_PCC_FAIL_CASES = CLANG_C_MANIFEST.get(
    "compile_only_native_pass_pcc_fail", []
)
CLANG_C_COMPILE_ONLY_NATIVE_FAIL_PCC_PASS_CASES = CLANG_C_MANIFEST.get(
    "compile_only_native_fail_pcc_pass", []
)
CLANG_C_RUNTIME_EXACT_MATCH_CASES = CLANG_C_MANIFEST.get("runtime_exact_match", [])
CLANG_C_RUNTIME_SUCCESS_CASES = (
    CLANG_C_RUNTIME_EXACT_MATCH_CASES
    + CLANG_C_MANIFEST.get("runtime_returncode_match_only", [])
)
CLANG_C_RUNTIME_COMPARISON_CASES = (
    CLANG_C_RUNTIME_EXACT_MATCH_CASES
    + runtime_cases_except(
        CLANG_C_MANIFEST,
        {"runtime_exact_match", "runtime_returncode_match_only", "runtime_both_fail"},
    )
)

CLANG_C_RUNTIME_BOTH_FAIL_CASES = CLANG_C_MANIFEST.get("runtime_both_fail", [])


if CLANG_C_COMPILE_ONLY_SUCCESS_CASES:

    @pytest.mark.parametrize("filename", CLANG_C_COMPILE_ONLY_SUCCESS_CASES)
    def test_clang_c_compile_only_succeeds_under_native_and_pcc(filename):
        case_path = CLANG_C_TESTS_DIR / filename
        assert case_path.is_file(), f"missing clang C test case: {case_path}"

        native_result = compile_native(case_path, REPO_ROOT)
        pcc_result = compile_pcc(case_path)

        assert native_result.returncode == 0, f"native cc failed for {filename}:\n{native_result.stderr}"
        assert pcc_result.returncode == 0, f"pcc failed to compile {filename}:\n{pcc_result.stderr}"


if CLANG_C_RUNTIME_SUCCESS_CASES:

    @pytest.mark.parametrize("filename", CLANG_C_RUNTIME_SUCCESS_CASES)
    def test_clang_c_runtime_succeeds_under_native_and_pcc(filename):
        case_path = CLANG_C_TESTS_DIR / filename
        assert case_path.is_file(), f"missing clang C test case: {case_path}"

        native_result = run_native(case_path, REPO_ROOT)
        pcc_result = run_pcc(case_path, REPO_ROOT)

        native_result.require_execution(f"host reference {filename}")
        pcc_result.require_execution(f"owned pcc {filename}")

        assert (
            pcc_result.returncode == native_result.returncode
        ), f"{filename} return code mismatch:\nnative={native_result.returncode}\npcc={pcc_result.returncode}\npcc stderr:\n{pcc_result.stderr}"


if CLANG_C_RUNTIME_COMPARISON_CASES:

    @pytest.mark.parametrize("filename", CLANG_C_RUNTIME_COMPARISON_CASES)
    def test_clang_c_runtime_matches_native_exactly(filename):
        case_path = CLANG_C_TESTS_DIR / filename
        assert case_path.is_file(), f"missing clang C test case: {case_path}"

        native_result = run_native(case_path, REPO_ROOT)
        pcc_result = run_pcc(case_path, REPO_ROOT)

        native_result.require_execution(f"host reference {filename}")
        pcc_result.require_execution(f"owned pcc {filename}")

        assert (
            pcc_result.returncode == native_result.returncode
        ), f"{filename} return code mismatch:\nnative={native_result.returncode}\npcc={pcc_result.returncode}\npcc stderr:\n{pcc_result.stderr}"
        assert (
            pcc_result.stdout == native_result.stdout
        ), f"{filename} stdout mismatch:\nnative={native_result.stdout!r}\npcc={pcc_result.stdout!r}"
        assert (
            pcc_result.stderr == native_result.stderr
        ), f"{filename} stderr mismatch:\nnative={native_result.stderr!r}\npcc={pcc_result.stderr!r}"


if CLANG_C_COMPILE_ONLY_BOTH_FAIL_CASES:

    @pytest.mark.parametrize("filename", CLANG_C_COMPILE_ONLY_BOTH_FAIL_CASES)
    def test_clang_c_compile_only_both_native_and_pcc_reject_case(filename):
        case_path = CLANG_C_TESTS_DIR / filename
        assert case_path.is_file(), f"missing clang C test case: {case_path}"

        native_result = compile_native(case_path, REPO_ROOT)
        pcc_result = compile_pcc(case_path)

        assert native_result.returncode != 0, f"native cc unexpectedly accepted {filename}"
        assert pcc_result.returncode != 0, f"pcc unexpectedly accepted {filename}"


if CLANG_C_COMPILE_ONLY_NATIVE_PASS_PCC_FAIL_CASES:

    @pytest.mark.parametrize("filename", CLANG_C_COMPILE_ONLY_NATIVE_PASS_PCC_FAIL_CASES)
    def test_clang_c_compile_only_native_accepts_but_pcc_rejects_case(filename):
        case_path = CLANG_C_TESTS_DIR / filename
        assert case_path.is_file(), f"missing clang C test case: {case_path}"

        native_result = compile_native(case_path, REPO_ROOT)
        pcc_result = compile_pcc(case_path)

        assert native_result.returncode == 0, f"native cc unexpectedly rejected {filename}:\n{native_result.stderr}"
        assert pcc_result.returncode != 0, f"pcc unexpectedly accepted {filename}"


if CLANG_C_COMPILE_ONLY_NATIVE_FAIL_PCC_PASS_CASES:

    @pytest.mark.parametrize("filename", CLANG_C_COMPILE_ONLY_NATIVE_FAIL_PCC_PASS_CASES)
    def test_clang_c_compile_only_native_rejects_but_pcc_accepts_case(filename):
        case_path = CLANG_C_TESTS_DIR / filename
        assert case_path.is_file(), f"missing clang C test case: {case_path}"

        native_result = compile_native(case_path, REPO_ROOT)
        pcc_result = compile_pcc(case_path)

        assert native_result.returncode != 0, f"native cc unexpectedly accepted {filename}"
        assert pcc_result.returncode == 0, f"pcc unexpectedly rejected {filename}:\n{pcc_result.stderr}"


if CLANG_C_RUNTIME_BOTH_FAIL_CASES:

    @pytest.mark.parametrize("filename", CLANG_C_RUNTIME_BOTH_FAIL_CASES)
    def test_clang_c_runtime_both_native_and_pcc_reject_case(filename):
        case_path = CLANG_C_TESTS_DIR / filename
        assert case_path.is_file(), f"missing clang C test case: {case_path}"

        native_result = run_native(case_path, REPO_ROOT)
        pcc_result = run_pcc(case_path, REPO_ROOT)

        native_result.require_execution("host reference")
        pcc_result.require_execution("owned pcc")

        assert native_result.returncode != 0, f"native runtime unexpectedly succeeded for {filename}"
        assert pcc_result.returncode != 0, f"pcc unexpectedly accepted {filename}"


def test_clang_c_manifest_covers_all_cases():
    manifest_files = {}
    duplicate_categories = {}
    for category, cases in CLANG_C_MANIFEST.items():
        for case in cases:
            previous_category = manifest_files.setdefault(case, category)
            if previous_category != category:
                duplicate_categories.setdefault(case, {previous_category}).add(category)

    assert not duplicate_categories, f"duplicate manifest categories: {duplicate_categories}"

    actual_files = {path.name for path in CLANG_C_TESTS_DIR.glob("*.c")}
    assert set(manifest_files) == actual_files
