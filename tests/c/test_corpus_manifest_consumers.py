"""A refresh category must retain an executable, phase-aware consumer route."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from tests import self_backend_c_testsuite_common as common
from tests.corpus_execution_phases import CStageResult, PccCompileResult
from utils.internal import (
    refresh_c_testsuite_manifest,
    refresh_clang_c_manifest,
    refresh_gcc_torture_manifest,
)


CONSUMERS = (
    ("clang", "test_clang_c.py", refresh_clang_c_manifest),
    ("gcc", "test_gcc_torture_execute.py", refresh_gcc_torture_manifest),
    ("gcc-self", "test_gcc_torture_self.py", refresh_gcc_torture_manifest),
    ("c-testsuite", "test_c_testsuite.py", refresh_c_testsuite_manifest),
    ("c-testsuite-self", "test_c_testsuite_self.py", refresh_c_testsuite_manifest),
)


def outcome(stage="run", returncode=0, *, completed=True, stdout="", stderr=""):
    return PccCompileResult.from_stages((
        CStageResult(stage, returncode, stdout, stderr, completed),
    ))


def classify_runtime(refresh, path):
    options = {} if refresh is refresh_clang_c_manifest else {"timeout": 10}
    classified = refresh._classify_runtime(path, **options)
    return classified if isinstance(classified, str) else classified[0]


def classified_manifest(refresh, root, monkeypatch):
    """Use the real classifiers, including both failure/timeout directions."""
    manifest = {}
    outcomes = (
        (outcome(), outcome()),
        (outcome(), outcome(stdout="different")),
        (outcome(returncode=1), outcome(returncode=2)),
        (outcome(returncode=1), outcome()),
        (outcome(), outcome(returncode=1)),
        (outcome("link", 1), outcome()),
        (outcome(), outcome("compile", 1)),
        (outcome("worker", 124, completed=False), outcome()),
        (outcome(), outcome("worker", 124, completed=False)),
    )
    for index, (native, pcc) in enumerate(outcomes):
        path = root / f"case-{index}.c"
        path.write_text("int main(void) { return 0; }\n")
        monkeypatch.setattr(refresh, "run_native", lambda *args, **kwargs: native)
        monkeypatch.setattr(refresh, "run_pcc", lambda *args, **kwargs: pcc)
        category = classify_runtime(refresh, path)
        manifest.setdefault(category, []).append(path.name)
    # A new runtime category must be run without adding another allowlist.
    manifest["runtime_future_category"] = ["future.c"]
    (root / "future.c").write_text("int main(void) { return 0; }\n")
    if refresh is refresh_clang_c_manifest:
        for index, (native_code, pcc_code) in enumerate(((0, 0), (1, 1), (0, 1), (1, 0))):
            path = root / f"compile-{index}.c"
            path.write_text("int value;\n")
            monkeypatch.setattr(refresh, "compile_native", lambda *args: outcome("compile", native_code))
            monkeypatch.setattr(refresh, "compile_pcc", lambda *args: outcome("compile", pcc_code))
            category = refresh._classify_compile_only(path)
            manifest.setdefault(category, []).append(path.name)
    return manifest


def load_consumer(filename, manifest, root, monkeypatch):
    read_text = Path.read_text

    def read_manifest(path, *args, **kwargs):
        if path.name in {"clang_c_manifest.json", "gcc_torture_manifest.json", "c_testsuite_manifest.json"}:
            return json.dumps(manifest)
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_manifest)
    monkeypatch.setattr(common, "C_TESTSUITE_MANIFEST", manifest)
    monkeypatch.setattr(common, "C_TESTSUITE_RUNTIME_EXACT_MATCH_CASES", tuple(manifest.get("runtime_exact_match", ())))
    monkeypatch.setattr(common, "C_TESTSUITE_DIR", root)
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location("manifest_consumer_subject", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("CLANG_C_TESTS_DIR", "GCC_TORTURE_DIR", "C_TESTSUITE_DIR"):
        if hasattr(module, name):
            monkeypatch.setattr(module, name, root)
    return module


def selected_routes(module):
    routes = {}
    for name, test in vars(module).items():
        if not name.startswith("test_") or not callable(test):
            continue
        marks = getattr(test, "pytestmark", ())
        assert not any(mark.name in {"skip", "skipif", "xfail"} for mark in marks)
        for mark in marks:
            if mark.name != "parametrize":
                continue
            assert mark.args[0] in {"filename", "relative_path"}
            for parameter in mark.args[1]:
                if isinstance(parameter, str):
                    case = parameter
                else:
                    assert not any(mark.name in {"skip", "skipif", "xfail"} for mark in parameter.marks)
                    case, = parameter.values
                routes.setdefault(case, []).append(test)
    return routes


@pytest.fixture(params=CONSUMERS, ids=[item[0] for item in CONSUMERS])
def consumer(request, tmp_path, monkeypatch):
    name, filename, refresh = request.param
    for attribute in ("CLANG_C_TESTS_DIR", "GCC_TORTURE_DIR", "C_TESTSUITE_DIR"):
        if hasattr(refresh, attribute):
            monkeypatch.setattr(refresh, attribute, tmp_path)
    manifest = classified_manifest(refresh, tmp_path, monkeypatch)
    module = load_consumer(filename, manifest, tmp_path, monkeypatch)
    return name, module, refresh, manifest, selected_routes(module), tmp_path


def set_results(module, monkeypatch, native, pcc, explicit=None):
    if hasattr(module, "run_native_and_pcc"):
        monkeypatch.setattr(module, "run_native_and_pcc", lambda *args: (native, pcc))
    else:
        monkeypatch.setattr(module, "run_native", lambda *args: native)
    if hasattr(module, "run_pcc"):
        monkeypatch.setattr(module, "run_pcc", lambda *args: pcc)
    if hasattr(module, "_run_llvm_backend"):
        monkeypatch.setattr(module, "_run_llvm_backend", lambda *args: pcc)
        monkeypatch.setattr(module, "_run_self_backend", lambda *args: pcc if explicit is None else explicit)


def test_every_classified_case_has_a_live_consumer(consumer):
    _, _, _, manifest, routes, _ = consumer
    assert set(routes) == {case for cases in manifest.values() for case in cases}


@pytest.mark.parametrize("stage,code,completed", [
    ("compile", 1, True),
    ("link", 1, True),
    ("run", 1, False),
    ("worker", 124, False),
])
@pytest.mark.parametrize("failed_side", ["native", "pcc"])
def test_refreshed_failures_cannot_pass_as_equal_program_statuses(
    consumer, monkeypatch, stage, code, completed, failed_side,
):
    _, module, _, manifest, routes, _ = consumer
    failed = outcome(stage, code, completed=completed)
    results = {"native": outcome(returncode=code), "pcc": outcome(returncode=code)}
    results[failed_side] = failed
    set_results(module, monkeypatch, **results)
    for category in ("runtime_build_or_execution_failure", "runtime_timeout", "runtime_future_category"):
        for case in manifest[category]:
            assert case in routes, f"{category} lost {case} during collection"
            for test in routes[case]:
                with pytest.raises(AssertionError, match="did not execute"):
                    test(case)


@pytest.mark.parametrize(
    "consumer", [item for item in CONSUMERS if item[0].endswith("-self")],
    ids=[item[0] for item in CONSUMERS if item[0].endswith("-self")], indirect=True,
)
def test_self_consumer_checks_explicit_backend_execution(consumer, monkeypatch):
    _, module, _, manifest, routes, _ = consumer
    set_results(module, monkeypatch, outcome(returncode=1), outcome(returncode=1), outcome("link", 1))
    for case in manifest["runtime_build_or_execution_failure"]:
        assert case in routes
        for test in routes[case]:
            with pytest.raises(AssertionError, match="explicit owned self did not execute"):
                test(case)


@pytest.mark.parametrize("code", [0, 1, 124])
def test_previously_failed_cases_can_recover_to_real_program_exits(consumer, monkeypatch, code):
    _, module, _, manifest, routes, _ = consumer
    set_results(module, monkeypatch, outcome(returncode=code), outcome(returncode=code))
    for category in ("runtime_build_or_execution_failure", "runtime_timeout", "runtime_future_category"):
        for case in manifest[category]:
            assert case in routes
            for test in routes[case]:
                test(case)


def test_previously_failed_cases_still_check_program_status(consumer, monkeypatch):
    _, module, _, manifest, routes, _ = consumer
    set_results(module, monkeypatch, outcome(), outcome(returncode=1))
    for case in manifest["runtime_build_or_execution_failure"]:
        assert case in routes
        for test in routes[case]:
            with pytest.raises(AssertionError, match="return code mismatch"):
                test(case)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize(
    "consumer,different_side",
    [
        pytest.param(item, side, id=f"{item[0]}-{side}")
        for item in CONSUMERS
        for side in (("default", "explicit") if item[0].endswith("-self") else ("default",))
    ],
    indirect=["consumer"],
)
def test_previously_unhandled_cases_keep_full_output_comparison(
    consumer, monkeypatch, stream, different_side,
):
    _, module, _, manifest, routes, _ = consumer
    different = outcome(returncode=1, **{stream: "different output"})
    if different_side == "default":
        set_results(module, monkeypatch, outcome(returncode=1), different, outcome(returncode=1))
    else:
        set_results(module, monkeypatch, outcome(returncode=1), outcome(returncode=1), different)
    for category in ("runtime_build_or_execution_failure", "runtime_timeout", "runtime_future_category"):
        for case in manifest[category]:
            assert case in routes
            for test in routes[case]:
                with pytest.raises(AssertionError, match=f"{stream} mismatch"):
                    test(case)


def test_historical_returncode_only_category_allows_output_differences(consumer, monkeypatch):
    _, module, _, manifest, routes, _ = consumer
    set_results(module, monkeypatch, outcome(returncode=1), outcome(returncode=1, stdout="different", stderr="different"))
    for case in manifest["runtime_returncode_match_only"]:
        assert case in routes
        for test in routes[case]:
            test(case)


@pytest.mark.parametrize(
    "consumer", [item for item in CONSUMERS if item[0].startswith("c-testsuite")],
    ids=[item[0] for item in CONSUMERS if item[0].startswith("c-testsuite")], indirect=True,
)
def test_recovered_c_testsuite_cases_still_check_expected_output(consumer, monkeypatch):
    _, module, _, manifest, routes, root = consumer
    set_results(module, monkeypatch, outcome(stdout="actual"), outcome(stdout="actual"))
    for case in manifest["runtime_build_or_execution_failure"]:
        (root / (case + ".expected")).write_text("required output")
        assert case in routes
        for test in routes[case]:
            with pytest.raises(AssertionError, match="output vs .expected mismatch"):
                test(case)


@pytest.mark.parametrize("failed_side", ["native", "pcc"])
def test_historical_runtime_rejection_routes_require_execution(
    consumer, monkeypatch, failed_side,
):
    _, module, _, manifest, routes, _ = consumer
    for category, native_code, pcc_code in (
        ("runtime_both_fail", 1, 2),
        ("runtime_native_fail_pcc_pass", 1, 0),
        ("runtime_native_pass_pcc_fail", 0, 1),
        ("runtime_mismatch", 0, 1),
    ):
        for case in manifest.get(category, ()):
            assert case in routes
            results = {"native": outcome(returncode=native_code), "pcc": outcome(returncode=pcc_code)}
            code = native_code if failed_side == "native" else pcc_code
            results[failed_side] = outcome("link", code)
            set_results(module, monkeypatch, **results)
            for test in routes[case]:
                with pytest.raises(AssertionError, match="did not execute"):
                    test(case)


@pytest.mark.integration
@pytest.mark.parametrize("failed_stage", ["compile", "link"])
def test_real_reference_build_failure_survives_refresh_and_reaches_consumer(
    consumer, monkeypatch, failed_stage,
):
    """Real external-cc error probe; no owned compiler/runtime qualification."""
    _, module, refresh, _, _, root = consumer
    # The fixture models classification only; use its original reference runner
    # here so both classification and the consumer observe a real failed build.
    if refresh is refresh_clang_c_manifest:
        from tests.clang_c_cases import run_native
    elif refresh is refresh_gcc_torture_manifest:
        from tests.gcc_torture_cases import run_native
    else:
        from tests.c_testsuite_cases import run_native
    case = root / "real-build-failure.c"
    source = "int main(void) { invalid C syntax; }\n" if failed_stage == "compile" else (
        "extern int missing_symbol(void);\nint main(void) { return missing_symbol(); }\n"
    )
    case.write_text(source)
    monkeypatch.setattr(refresh, "run_native", run_native)
    monkeypatch.setattr(refresh, "run_pcc", lambda *args, **kwargs: outcome(returncode=1))
    category = classify_runtime(refresh, case)
    assert category == "runtime_build_or_execution_failure"
    manifest = {category: [case.name]}
    module = load_consumer(Path(module.__file__).name, manifest, root, monkeypatch)
    native = run_native(case, refresh.REPO_ROOT)
    assert native.stages[-1].stage == failed_stage
    assert not native.executed
    assert native.returncode != 0
    set_results(module, monkeypatch, native, outcome(returncode=native.returncode))
    routes = selected_routes(module)
    assert case.name in routes
    for test in routes[case.name]:
        with pytest.raises(AssertionError, match=f"did not execute: {failed_stage}"):
            test(case.name)
    assert case.read_text() == source
