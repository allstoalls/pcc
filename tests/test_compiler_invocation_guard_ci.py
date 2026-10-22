"""Keep invocation containment covered without starting native bootstrap CI."""

from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path
import re
import shlex
import textwrap
import tomllib
from xml.etree import ElementTree

import pytest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ".github/workflows/compiler-invocation-guard.yml"
WORKFLOW = (ROOT / WORKFLOW_PATH).read_text(encoding="utf-8")
GUARD_TEST = "tests/python/test_compiler_invocation_guard.py"
SAMPLER_TESTS = (
    "tests/python/test_process_tree_sample_tool.py",
    "tests/python/test_process_tree_manifest_evidence.py",
)
PACKAGE_PROCESS_TEST = "tests/python/test_package_process_timeout.py"
PACKAGE_PROCESS_HELPER = "tests/python/process_timeout.py"
PACKAGE_PROCESS_NAMES = [
    "test_package_process_capture_preserves_success_and_nonzero",
    "test_package_process_timeout_cleans_separate_session_and_retains_output",
    "test_package_process_check_preserves_called_process_error",
    "test_package_deadline_expiring_during_telemetry_is_timeout",
] + [
    f"test_package_process_output_limit_is_bounded_and_fails[{stream}]"
    for stream in ("stdout", "stderr")
] + [
    f"test_package_process_cleanup_error_does_not_mask_original[error{error}]"
    for error in range(2)
] + [
    f"test_package_teardown_errors_preserve_original_and_attempt_both_streams[error{error}-{kind}]"
    for error in range(3)
    for kind in ("pipe", "log")
]
PACKAGE_PROCESS_NAMES = [name + "@package-process-timeout" for name in PACKAGE_PROCESS_NAMES]
CONTROL_TEST = "tests/test_compiler_invocation_guard_ci.py"
RUNTIME_CACHE_TESTS = ('tests/test_runtime_build_source_override.py', 'tests/test_runtime_archive_consumers.py')
RUNTIME_CACHE_SELECTORS = (
    "tests/test_runtime_build_source_override.py::test_default_runtime_forwards_effective_thread_configuration",
    "tests/test_runtime_build_source_override.py::test_single_thread_runtime_request_fails_explicitly",
    "tests/test_runtime_build_source_override.py::test_threaded_runtime_helper_uses_the_one_build",
    "tests/test_runtime_archive_consumers.py::test_pcc_runtime_cache_reuses_only_a_bound_verified_entry",
    "tests/test_runtime_archive_consumers.py::test_pcc_runtime_cache_mock_admission_preserves_unmocked_provisioning_guard",
)
RUNTIME_CACHE_INPUTS = (
    "pcc/frontends/python/owned_runtime_build.py",
    "pcc/frontends/python/thread_runtime_config.py",
    "pcc/tools/runtime_archive_provenance.py",
    "pcc/frontends/python/pipeline_runtime_archive.py",
)
RUNTIME_CACHE_NAMES = {
    "test_runtime_build_source_override": [
        f"test_default_runtime_forwards_effective_thread_configuration[{value}]"
        for value in ("None", "", "1", "true", "yes", "on", " YES ")
    ] + [
        f"test_single_thread_runtime_request_fails_explicitly[{value}]"
        for value in ("0", "false", "no", "off")
    ] + ["test_threaded_runtime_helper_uses_the_one_build"],
    "test_runtime_archive_consumers": [
        "test_pcc_runtime_cache_reuses_only_a_bound_verified_entry",
        "test_pcc_runtime_cache_mock_admission_preserves_unmocked_provisioning_guard",
    ],
}
PYTEST_HARNESS_TEST = "tests/python/test_cli_bootstrap_pytest_extract.py"
PYTEST_HARNESS_INPUTS = (
    "pcc/driver/cli_bootstrap.py",
    "pcc/driver/cli_bootstrap_pytest.py",
    "pcc/frontends/python/pipeline_dependency_closure.py",
    "pcc/frontends/python/codegen/layer1_support.py",
)
PYTEST_HARNESS_NAMES = (
    "test_cli_keeps_both_native_pytest_request_spellings",
    "test_cli_pytest_facade_forwards_current_stage_and_timeout",
    "test_extracted_harness_launcher_uses_supplied_stage_and_bounded_runs",
    "test_pytest_harness_source_is_owned_by_sibling_and_facade_stays_thin",
    "test_repo_main_closure_and_static_exports_include_pytest_harness",
)

RELATIVE_IMPORT_TEST = "tests/python/test_py_parse_relative_imports.py"
RELATIVE_IMPORT_INPUTS = (
    "pcc/frontends/python/py_parse.py",
    "pcc/frontends/python/py_lex.py",
    "pcc/frontends/python/py_lift.py",
    "pcc/frontends/python/py_ast.py",
)
MESON_FIXTURE_DIR = "projects/numpy-2.4.4/vendored-meson/meson/mesonbuild/compilers/mixins"
MESON_FIXTURE = MESON_FIXTURE_DIR + "/gnu.py"
RELATIVE_IMPORT_COUNTS = {
    "test_relative_import_depth_matches_python": 18,
    "test_from_import_token_and_name_forms_match_python": 7,
    "test_ellipsis_token_and_expression_remain_unchanged": 1,
    "test_malformed_imports_are_still_rejected": 4,
    "test_vendored_meson_gnu_mixin_parses_and_lifts": 1,
}
FINAL_LAYOUT_METADATA_TEST = "tests/python/test_aarch64_final_layout_metadata.py"
FINAL_LAYOUT_METADATA_INPUTS = (
    "pcc/backend/arm64_encode.py",
    "pcc/backend/arm64_asm_driver.py",
    "pcc/backend/arm64_elf_driver.py",
    "pcc/backend/self_backend_aarch64_darwin.py",
    "pcc/backend/self_backend_precise_stackmaps.py",
)
FINAL_LAYOUT_METADATA_NAMES = [
    f"test_emitted_metadata_uses_final_relaxed_layout[{padding}-{route}-{triple}]"
    for padding in ("near", "far")
    for route in ("native", "optimized-structured", "optimized-rendered")
    for triple in ("arm64-apple-darwin", "aarch64-unknown-linux-gnu", "arm64-apple-darwin-debug")
] + [
    f"test_finalized_text_layout_is_idempotent_and_immutable[{structured}-{padding}]"
    for structured in ("text", "encoded")
    for padding in ("near", "far")
] + ["test_module_layout_freeze_still_allows_nontext_metadata"]
RAW_BRANCH_FIXUPS_TEST = "tests/python/test_arm64_raw_branch_fixups.py"
RAW_BRANCH_FIXUPS_NAMES = [
    f"test_raw_branch19_rejects_invalid_source_entries[{entry}-{padding}]"
    for entry in ("nop", "patched-condition", "patched-cbz", "negative-word",
                  "wide-word", "unknown-kind", "text-kind")
    for padding in ("near", "far")
] + [
    f"test_raw_branch19_preserves_relocation_validation[{shape}-{padding}]"
    for shape in ("duplicate", "unordered", "trailing", "negative-entry", "data-entry",
                  "negative-symbol", "missing-symbol", "unknown-fixup")
    for padding in ("near", "far")
] + [
    f"test_valid_raw_branch19_still_matches_text_relaxation[{branch}-{direction}-{padding}]"
    for branch in ("condition", "cbz", "cbnz")
    for direction in ("forward", "backward")
    for padding in ("near", "far")
] + [
    f"test_raw_none_relocation_keeps_existing_adapter_contract[{padding}]"
    for padding in ("near", "far")
] + [
    f"test_raw_unconditional_condition_codes_keep_original_range_contract[{condition}-{padding}]"
    for condition in ("al", "nv")
    for padding in ("near", "far")
] + [
    f"test_raw_entries_reject_invalid_data_indices[{index}-{padding}]"
    for index in ("negative", "missing")
    for padding in ("near", "far")
] + [
    f"test_raw_entries_reject_unaligned_instructions[{padding}]"
    for padding in ("near", "far")
] + [
    f"test_conditional_cross_atom_target_alignment[{branch}-{residue}-{direction}-{padding}-{route}]"
    for branch in ("condition", "cbz", "cbnz")
    for residue in ("mod1", "mod2", "mod3")
    for direction in ("forward", "backward")
    for padding in ("near", "far")
    for route in ("raw", "text")
] + [
    f"test_aligned_cross_atom_conditional_relocations[{branch}-{direction}-{padding}]"
    for branch in ("condition", "cbz", "cbnz")
    for direction in ("forward", "backward")
    for padding in ("near", "far")
] + [
    f"test_misaligned_layout_rejection_preserves_builder_state[{representation}]"
    for representation in ("text", "encoded")
]
BACKEND_REGRESSION_NAMES = {
    "test_x86_64_phi_shell_completeness": [
        f"test_fresh_indexed_shells_are_phi_complete_without_repair[{packed}-{source}]"
        for packed in ("symbolic", "packed")
        for source in ("zero", "mixed")
    ] + [
        f"test_compatibility_owners_still_repair_released_scalar_phis[{owner}]"
        for owner in ("legacy", "function", "kernel", "blocks", "block-map")
    ],
    "test_aarch64_suffix_scratch": [
        f"test_suffix_scratch_is_per_invocation_and_preserves_route_order[{shape}-{sink}]"
        for shape in ("ordinary", "nested", "label-exception", "reload-exception")
        for sink in ("buffered", "native-sink")
    ] + [
        f"test_owned_empty_reload_fragments_preserve_output_and_reset[nonempty{index}]"
        for index in range(3)
    ] + [
        f"test_empty_reload_guard_retains_failure_fallback[{failure}]"
        for failure in ("subclass-hook", "incomplete", "wrong-target", "closed")
    ] + [
        "test_empty_reload_owner_guards_are_outside_instruction_loops",
    ],
    "test_x86_64_indexed_instruction_ids": [
        f"test_adapter_uses_absolute_id_without_reprojecting_block[{packed}-{source}]"
        for packed in ("symbolic", "packed")
        for source in ("call", "fixed-gep-cold", "later-blocks")
    ] + [
        f"test_adapter_preserves_authoritative_payload_and_tuple_fallback[{replacement}]"
        for replacement in ("negative", "tuple")
    ] + [
        "test_absolute_decoder_preserves_missing_arithmetic_flag_failure",
        "test_inconsistent_kind_metadata_is_still_rejected_before_emission",
    ],
}
BACKEND_REGRESSION_TESTS = tuple(
    f"tests/python/{module}.py" for module in BACKEND_REGRESSION_NAMES
)
BACKEND_REGRESSION_INPUTS = (
    "pcc/backend/self_backend_x86_64_linux.py",
    "pcc/backend/self_backend_kernel.py",
)


def _step(name):
    return WORKFLOW.split(f"      - name: {name}\n", 1)[1].split("\n      - ", 1)[0]


def _event_paths(workflow, event):
    block = workflow.split(f"  {event}:\n", 1)[1].split("\n  pull_request:", 1)[0]
    block = block.split("\npermissions:", 1)[0]
    return re.findall(r'^      - "([^"]+)"$', block, re.MULTILINE)


@pytest.mark.parametrize("event", ("push", "pull_request"))
def test_host_workflow_tracks_guard_helpers_and_test_configuration(event):
    paths = _event_paths(WORKFLOW, event)
    assert len(paths) == len(set(paths))
    assert set(paths) == {
        "scripts/compiler_invocation_guard.py",
        "scripts/run_process_tree_sample.py",
        "scripts/run_pcc_compile_ab.py",
        GUARD_TEST,
        *SAMPLER_TESTS,
        PACKAGE_PROCESS_TEST,
        PACKAGE_PROCESS_HELPER,
        CONTROL_TEST,
        PYTEST_HARNESS_TEST,
        *PYTEST_HARNESS_INPUTS,
        RELATIVE_IMPORT_TEST,
        *RELATIVE_IMPORT_INPUTS,
        MESON_FIXTURE,
        FINAL_LAYOUT_METADATA_TEST,
        *FINAL_LAYOUT_METADATA_INPUTS,
        RAW_BRANCH_FIXUPS_TEST,
        *BACKEND_REGRESSION_TESTS,
        *BACKEND_REGRESSION_INPUTS,
        "conftest.py",
        "tests/conftest.py",
        "tests/xdist_cpu_budget.py",
        "scripts/pytest_live_report.py",
        "tests/native_provisioning.py",
        "tests/runtime_build_cache.py",
        *RUNTIME_CACHE_TESTS,
        *RUNTIME_CACHE_INPUTS,
        "tests/runtime_fixture_provenance.py",
        "run.py",
        "pyproject.toml",
        "uv.lock",
        ".python-version",
        WORKFLOW_PATH,
    }
    assert "    branches: [master]\n" in WORKFLOW.split("  pull_request:", 1)[0]


def test_host_workflow_uses_normal_pytest_scheduling_and_resource_controls():
    options = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]
    assert shlex.split(options["addopts"]) == ["-n", "6", "--dist=loadgroup", "-m", "not integration"]
    step = _step("Run guard regressions with the normal six-worker pytest default")
    command = shlex.split(step.split("        run: |\n", 1)[1].replace("\\\n", ""))
    assert command == [
        "env", "-u", "LC_ALL", "uv", "run", "--frozen", "--no-sync",
        "python", "-m", "pytest", "-x", "-vv", "--tb=short",
        "--junitxml=build/compiler-invocation-guard.xml", GUARD_TEST, *SAMPLER_TESTS,
        PACKAGE_PROCESS_TEST, CONTROL_TEST,
        PYTEST_HARNESS_TEST, RELATIVE_IMPORT_TEST, FINAL_LAYOUT_METADATA_TEST, RAW_BRANCH_FIXUPS_TEST,
        *BACKEND_REGRESSION_TESTS,
        *RUNTIME_CACHE_SELECTORS,
    ]
    assert "        timeout-minutes: 2\n" in step
    assert "--noconftest" not in WORKFLOW
    assert "--confcutdir" not in WORKFLOW
    assert 'PCC_TEST_NO_NATIVE_PROVISIONING: "1"' in WORKFLOW
    assert 'PCC_NO_AUTO_PCC1: "1"' in WORKFLOW
    assert 'PCC_PYTEST_AUTO_CLEAN: "0"' in WORKFLOW


def test_host_workflow_has_no_native_build_or_duplicate_stage1_trigger():
    jobs = WORKFLOW.split("\njobs:\n", 1)[1]
    assert re.findall(r"^  ([a-zA-Z0-9_-]+):$", jobs, re.MULTILINE) == ["linux-host"]
    assert "    runs-on: ubuntu-24.04\n" in jobs
    assert "    timeout-minutes: 10\n" in jobs
    install = _step("Install host test dependencies without the native build hook")
    assert install.strip() == "run: env -u LC_ALL uv sync --frozen --dev --no-install-project"
    checkout = jobs.split("      - uses: actions/checkout@v4\n", 1)[1].split("\n      - ", 1)[0]
    assert checkout.split("          sparse-checkout: |\n", 1)[1].split() == [
        ".github/workflows", "pcc", "utils", "scripts", "tests", MESON_FIXTURE_DIR,
    ]
    assert (ROOT / MESON_FIXTURE).is_file()
    parity = (ROOT / ".github/workflows/pcc1-package-parity.yml").read_text()
    for event in ("push", "pull_request"):
        for path in (
            WORKFLOW_PATH, GUARD_TEST, CONTROL_TEST, PYTEST_HARNESS_TEST,
            RELATIVE_IMPORT_TEST, FINAL_LAYOUT_METADATA_TEST, RAW_BRANCH_FIXUPS_TEST,
            *BACKEND_REGRESSION_TESTS,
            *RUNTIME_CACHE_TESTS,
            "scripts/compiler_invocation_guard.py",
        ):
            assert not any(fnmatchcase(path, pattern) for pattern in _event_paths(parity, event))
    for command in ("scripts/bootstrap.py", "ci_worker_size_prior_gate.py", "python -m build"):
        assert command not in jobs


def test_host_workflow_requires_real_pidfd_support():
    step = _step("Require working Linux process handles")
    assert "shell: env -u LC_ALL uv run --frozen --no-sync python {0}" in step
    probe = textwrap.dedent(step.split("        run: |\n", 1)[1])
    assert "os.pidfd_open(os.getpid(), 0)" in probe
    assert "signal.pidfd_send_signal(descriptor, 0)" in probe
    assert "finally:\n    os.close(descriptor)" in probe


@pytest.mark.parametrize("result", (
    "pass", "skipped", "deselected", "empty", "missing_sampler", "missing_manifests",
    "missing_harness", "partial_harness", "duplicate_harness",
    "missing_relative_imports", "partial_relative_imports", "duplicate_relative_imports",
    "replaced_relative_import", "wrong_relative_import_function", "skipped_relative_import",
    "missing_final_layout", "partial_final_layout", "duplicate_final_layout", "replaced_final_layout",
    "wrong_final_layout_function", "wrong_final_layout_parameters", "skipped_final_layout",
    "missing_final_layout_debug", "wrong_final_layout_debug_parameters",
    "skipped_final_layout_debug", "extra_final_layout",
    "missing_raw_branch", "partial_raw_branch", "duplicate_raw_branch", "replaced_raw_branch",
    "wrong_raw_branch_function", "wrong_raw_branch_parameters", "skipped_raw_branch",
    "missing_package_process", "partial_package_process", "duplicate_package_process",
    "replaced_package_process", "wrong_package_process_function", "wrong_package_process_parameters",
    "wrong_package_process_group", "skipped_package_process", "failed_package_process", "error_package_process",
) + tuple(
    f"{mutation}_{module}"
    for module in BACKEND_REGRESSION_NAMES
    for mutation in ("missing", "partial", "duplicate", "replaced", "wrong_function", "wrong_parameters", "skipped")
) + tuple(
    f"{mutation}_{module}"
    for module in RUNTIME_CACHE_NAMES
    for mutation in ("missing", "partial", "duplicate", "replaced", "wrong_function",
                     "wrong_parameters", "extra", "skipped", "failed", "error")
))
def test_host_workflow_receipt_check_rejects_missing_or_skipped_guard_tests(monkeypatch, result):
    backend_module = next(
        (module for module in BACKEND_REGRESSION_NAMES if result.endswith("_" + module)), None,
    )
    cache_module = next(
        (module for module in RUNTIME_CACHE_NAMES if result.endswith("_" + module)), None,
    )
    suite = ElementTree.Element("testsuite")
    if result != "empty":
        ElementTree.SubElement(suite, "testcase", classname="tests.test_compiler_invocation_guard_ci")
    if (
        result in ("pass", "skipped", "missing_sampler", "missing_manifests",
                   "missing_harness", "partial_harness", "duplicate_harness")
        or "relative_import" in result
        or "final_layout" in result
        or "raw_branch" in result
        or "package_process" in result
        or backend_module is not None
        or cache_module is not None
    ):
        case = ElementTree.SubElement(suite, "testcase", classname="tests.python.test_compiler_invocation_guard")
        if result == "skipped":
            ElementTree.SubElement(case, "skipped")
    for index, path in enumerate(SAMPLER_TESTS):
        if (
            result in ("pass", "skipped", "missing_harness", "partial_harness", "duplicate_harness")
            or "relative_import" in result
            or "final_layout" in result
            or "raw_branch" in result
            or "package_process" in result
            or backend_module is not None
            or cache_module is not None
            or (result == "missing_sampler" and index == 1)
            or (result == "missing_manifests" and index == 0)
        ):
            ElementTree.SubElement(suite, "testcase", classname=path[:-3].replace("/", "."))
    if result not in ("empty", "missing_harness"):
        names = PYTEST_HARNESS_NAMES
        if result == "partial_harness":
            names = names[:-1]
        elif result == "duplicate_harness":
            names = names + (names[0],)
        for name in names:
            ElementTree.SubElement(suite, "testcase",
                                   classname="tests.python.test_cli_bootstrap_pytest_extract",
                                   name=name)
    if result not in ("empty", "missing_relative_imports"):
        names = [
            f"{name}[{index}]" if count > 1 else name
            for name, count in RELATIVE_IMPORT_COUNTS.items()
            for index in range(count)
        ]
        if result == "partial_relative_imports":
            names = names[:-1]
        elif result == "duplicate_relative_imports":
            names.append(names[0])
        elif result == "replaced_relative_import":
            names[1] = names[0]
        elif result == "wrong_relative_import_function":
            names[-1] = "test_unexpected_relative_import_case"
        for name in names:
            case = ElementTree.SubElement(
                suite, "testcase", classname="tests.python.test_py_parse_relative_imports", name=name,
            )
            if result == "skipped_relative_import":
                ElementTree.SubElement(case, "skipped")
    if result not in ("empty", "missing_final_layout"):
        names = list(FINAL_LAYOUT_METADATA_NAMES)
        if result == "partial_final_layout":
            names = names[1:]
        elif result == "duplicate_final_layout":
            names.append(names[0])
        elif result == "replaced_final_layout":
            names[1] = names[0]
        elif result == "wrong_final_layout_function":
            names[0] = "test_unexpected_final_layout_case"
        elif result == "wrong_final_layout_parameters":
            names[0] = names[0].replace("[near-", "[unexpected-", 1)
        elif result == "missing_final_layout_debug":
            names = [name for name in names if not name.endswith("-debug]")]
        elif result == "wrong_final_layout_debug_parameters":
            index = next(index for index, name in enumerate(names) if name.endswith("-debug]"))
            names[index] = names[index].replace("-debug]", "-unexpected-debug]")
        elif result == "extra_final_layout":
            names.append("test_unexpected_final_layout_case")
        for index, name in enumerate(names):
            case = ElementTree.SubElement(
                suite, "testcase", classname="tests.python.test_aarch64_final_layout_metadata", name=name,
            )
            if (
                result == "skipped_final_layout" and index == 0
                or result == "skipped_final_layout_debug" and name.endswith("-debug]")
            ):
                ElementTree.SubElement(case, "skipped")
    if result not in ("empty", "missing_raw_branch"):
        names = list(RAW_BRANCH_FIXUPS_NAMES)
        if result == "partial_raw_branch":
            names = names[1:]
        elif result == "duplicate_raw_branch":
            names.append(names[0])
        elif result == "replaced_raw_branch":
            names[1] = names[0]
        elif result == "wrong_raw_branch_function":
            names[0] = "test_unexpected_raw_branch_case"
        elif result == "wrong_raw_branch_parameters":
            names[0] = names[0].replace("-near]", "-unexpected]", 1)
        for index, name in enumerate(names):
            case = ElementTree.SubElement(
                suite, "testcase", classname="tests.python.test_arm64_raw_branch_fixups", name=name,
            )
            if result == "skipped_raw_branch" and index == 0:
                ElementTree.SubElement(case, "skipped")
    for module, expected_names in BACKEND_REGRESSION_NAMES.items():
        if result in ("empty", "missing_" + module):
            continue
        names = list(expected_names)
        if result == "partial_" + module:
            names = names[1:]
        elif result == "duplicate_" + module:
            names.append(names[0])
        elif result == "replaced_" + module:
            names[1] = names[0]
        elif result == "wrong_function_" + module:
            names[0] = names[0].replace("test_", "test_unexpected_", 1)
        elif result == "wrong_parameters_" + module:
            names[0] = names[0].replace("[", "[unexpected-", 1)
        for index, name in enumerate(names):
            case = ElementTree.SubElement(
                suite, "testcase", classname="tests.python." + module, name=name,
            )
            if result == "skipped_" + module and index == 0:
                ElementTree.SubElement(case, "skipped")
    if result not in ("empty", "missing_package_process"):
        names = list(PACKAGE_PROCESS_NAMES)
        if result == "partial_package_process":
            names = names[1:]
        elif result == "duplicate_package_process":
            names.append(names[0])
        elif result == "replaced_package_process":
            names[1] = names[0]
        elif result == "wrong_package_process_function":
            names[0] = "test_unexpected_package_process_case@package-process-timeout"
        elif result == "wrong_package_process_parameters":
            names[-1] = names[-1].replace("error2-log", "error3-log", 1)
        elif result == "wrong_package_process_group":
            names[0] = names[0].replace("@package-process-timeout", "@unexpected-group", 1)
        for index, name in enumerate(names):
            case = ElementTree.SubElement(
                suite, "testcase", classname="tests.python.test_package_process_timeout", name=name,
            )
            if index == 0:
                if result == "skipped_package_process":
                    ElementTree.SubElement(case, "skipped")
                elif result == "failed_package_process":
                    ElementTree.SubElement(case, "failure")
                elif result == "error_package_process":
                    ElementTree.SubElement(case, "error")
    for module, expected_names in RUNTIME_CACHE_NAMES.items():
        if result in ("empty", "missing_" + module):
            continue
        names = list(expected_names)
        if result == "partial_" + module:
            names = names[1:]
        elif result == "duplicate_" + module:
            names.append(names[0])
        elif result == "replaced_" + module:
            names[1] = names[0]
        elif result == "wrong_function_" + module:
            names[0] = "test_unexpected_runtime_cache_case"
        elif result == "wrong_parameters_" + module:
            names[0] += "[unexpected]"
        elif result == "extra_" + module:
            names.append("test_unexpected_runtime_cache_case")
        for index, name in enumerate(names):
            case = ElementTree.SubElement(
                suite, "testcase", classname="tests." + module, name=name,
            )
            for mutation, tag in (("skipped", "skipped"), ("failed", "failure"), ("error", "error")):
                if result == mutation + "_" + module and index == 0:
                    ElementTree.SubElement(case, tag)
    monkeypatch.setattr(ElementTree, "parse", lambda path: ElementTree.ElementTree(suite))
    step = _step("Require executed guard tests without skips")
    check = textwrap.dedent(step.split("        run: |\n", 1)[1])
    if result == "pass":
        exec(check, {})
    else:
        if cache_module is not None:
            if result.startswith("skipped_"):
                message = "must not skip tests"
            elif result.startswith(("failed_", "error_")):
                message = cache_module + " runtime cache tests must pass"
            else:
                message = cache_module + " runtime cache tests (were not fully executed|contain duplicate cases)"
        elif backend_module is not None:
            message = (
                "must not skip tests" if result.startswith("skipped_")
                else backend_module + " tests (were not fully executed|contain duplicate cases)"
            )
        elif result in (
            "skipped_relative_import", "skipped_final_layout", "skipped_final_layout_debug",
            "skipped_raw_branch", "skipped_package_process",
        ):
            message = "must not skip tests"
        elif "relative_import" in result:
            message = "relative import tests (were not fully executed|contain duplicate cases)"
        elif "final_layout" in result:
            message = "final layout metadata tests (were not fully executed|contain duplicate cases)"
        elif "raw_branch" in result:
            message = "raw branch fixup tests (were not fully executed|contain duplicate cases)"
        elif result in ("failed_package_process", "error_package_process"):
            message = "package process timeout tests must pass"
        elif "package_process" in result:
            message = "package process timeout tests (were not fully executed|contain duplicate cases)"
        else:
            message = "guard tests were not executed|must not skip tests|pytest harness tests were not fully executed"
        with pytest.raises(AssertionError, match=message):
            exec(check, {})
