"""Bounded premerge phases using the existing platform watchdogs and pytest."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Stage1 watchdog for every platform. Measured and projected completions
# (docs/status.md, 2026-10-14): Linux aarch64 ~2360 s; Linux x86_64
# ~2600 s on fast runners and ~4900 s on slow ones (1.8-1.9x per module);
# Windows x86_64 ~3850-4500 s; macOS arm64 ~4500-5000 s at the 1.3-1.7
# workers its memory admission allows. Stage1 now has an explicit two-hour
# allowance; this does not extend native canaries or later-stage compiles.
STAGE1_TIMEOUT_S = 7200
PLATFORM_COMPILE_TIMEOUT_S = 6000
# Prior-worker native gate: one compile of the fixture plus its ten
# collector/case executions, under one watchdog. 400 s by the maintainer's
# decision (2026-10-20, was 300 s): pass first, then keep optimizing.
NATIVE_GATE_TIMEOUT_S = 400
# The 45-minute macOS job target. Recorded in the Stage1 receipt; not met
# while memory admission keeps the 3-core runner near one worker.
DARWIN_TARGET_WALL_S = 1800
DARWIN_TARGET_AVERAGE_WORKERS = 2
PROCESS_TEST = "tests/python/test_worker_size_prior_processes.py"
CLOSURE_TEST = "tests/integration/test_worker_size_prior_closed_world.py"
EXIT_TEST = "tests/python/test_owned_windows_exit.py"
WIN64_DIRECT_SCALAR_TEST = "tests/python/test_windows_direct_scalar_call_args.py"
WIN64_DIRECT_SCALAR_INPUTS = (
    WIN64_DIRECT_SCALAR_TEST, "pcc/backend/self_backend_win64_abi.py",
)
CONSUMED_UNSAFE_TEST = "tests/python/test_consumed_unsafe_dependency.py"
CONSUMED_UNSAFE_INPUTS = (
    CONSUMED_UNSAFE_TEST,
    "pcc/frontends/python/pipeline_dependency_closure.py",
    "pcc/frontends/python/pipeline_unsafe_imports.py",
    "pcc/frontends/python/unsafe_intrinsic_policy.py",
    "pcc/frontends/python/codegen/unsafe_lowering.py",
    "pcc/frontends/python/type_infer.py",
    "pcc/frontends/python/py_parse.py",
    "pcc/frontends/python/pipeline_import_scan.py",
    "pcc/unsafe/__init__.py",
    "scripts/bootstrap_platform.py",
    "tests/owned_ir_validation.py",
)
TAR_ATTRIBUTES_TEST = "tests/python/test_windows_tar_attributes.py"
TAR_STRICT_ORACLE_TEST = "tests/test_tar_attribute_strict_oracle.py"
IR_BUILDER_CALL9_TEST = "tests/test_ir_builder_call9.py"
TAR_ATTRIBUTES_INPUTS = (
    TAR_ATTRIBUTES_TEST, TAR_STRICT_ORACLE_TEST, "tests/tar_attribute_strict_oracle.py",
    "pcc/backend/pe_x86_64.py",
    "pcc/frontends/python/codegen/platform_machine_abis.py",
    "pcc/frontends/python/codegen/unsafe_lowering.py",
    "pcc/frontends/python/codegen/windows_platform.py",
    "pcc/frontends/python/pipeline_freestanding.py",
    "pcc/frontends/python/type_infer.py",
    "pcc/runtime/py/freestanding_windows.py", "pcc/stdlib/tarfile.py",
    "pcc/unsafe/__init__.py", "tests/python/owned_regression_support.py",
    "tests/python/process_timeout.py",
)
EXIT_INPUTS = (EXIT_TEST, "tests/c/test_owned_linux_c_exports.py",
               "tests/c/fixtures/owned_linux_c_exports/exit_lifecycle.c",
               "tests/c/fixtures/owned_linux_c_exports/immediate_bypass.c")
ARM_TRANSPORT_TEST = "tests/python/test_linux_aarch64_worker_transport.py"
ARM_TRANSPORT_NATIVE_TEST = "tests/python/test_linux_aarch64_transport_native.py"
ARM_TRANSPORT_INPUTS = (ARM_TRANSPORT_TEST, ARM_TRANSPORT_NATIVE_TEST)
ELF_FORMAT_TEST = "tests/python/test_elf_x86_64.py"
ELF_STAGING_TEST = "tests/python/test_owned_elf_staging.py"
ELF_OWNER_TEST = "tests/python/test_pipeline_self_backend_link_owner.py"
ELF_RELOCATION_CONTEXT_TEST = "tests/python/test_aarch64_relocation_error_context.py"
ARM_CALL_VENEERS_TEST = "tests/python/test_aarch64_elf_call_veneers.py"
ELF_PACKED_TEST = "tests/python/test_elf_packed_link_classification.py"
ELF_PACKED_SOURCE_TEST = "tests/test_elf_packed_classification_source_controls.py"
ELF_NEGATIVE_TEST = "tests/python/test_elf_negative_classification_summary.py"
ELF_NEGATIVE_SOURCE_TEST = "tests/test_elf_negative_classification_source_controls.py"
ELF_PROGRESS_TEST = "tests/python/test_elf_relocation_progress.py"
ELF_PROGRESS_SOURCE_TEST = "tests/test_elf_relocation_progress_source_controls.py"
ELF_APPLICATION_TEST = "tests/python/test_elf_packed_application.py"
ELF_APPLICATION_SOURCE_TEST = "tests/test_elf_packed_application_source_controls.py"
ELF_X86_CLASSIFICATION_TEST = "tests/python/test_elf_x86_packed_classification.py"
ELF_X86_APPLICATION_TEST = "tests/python/test_elf_x86_packed_application.py"
ELF_OWNER_NODE = ELF_OWNER_TEST + "::test_linux_pcc_link_route_uses_owned_elf_driver_and_internal_assembly"
ELF_INPUTS = (ELF_FORMAT_TEST, ELF_STAGING_TEST, ELF_OWNER_TEST,
              "pcc/backend/owned_elf_inputs.py", "pcc/backend/elf_x86_64.py",
              "pcc/backend/elf_aarch64_relocations.py", "tests/elf_validate_object_preimage.py",
              ELF_RELOCATION_CONTEXT_TEST, ARM_CALL_VENEERS_TEST,
              ELF_PACKED_TEST, ELF_PACKED_SOURCE_TEST, "tests/elf_link_image_preimage.py",
              "pcc/backend/owned_elf_link.py", "pcc/backend/indexed_phase_timing.py",
              ELF_NEGATIVE_TEST, ELF_NEGATIVE_SOURCE_TEST,
              "tests/elf_negative_classification_preimage.py",
              "scripts/pcc_record_inventory.py", ELF_PROGRESS_TEST, ELF_PROGRESS_SOURCE_TEST,
              "tests/elf_relocation_progress_preimage.py",
              ELF_APPLICATION_TEST, ELF_APPLICATION_SOURCE_TEST,
              "tests/elf_packed_application_preimage.py",
              ELF_X86_CLASSIFICATION_TEST, ELF_X86_APPLICATION_TEST,
              "tests/elf_x86_packed_classification_reference.py",
              "tests/elf_arm_branch_slot_reference.py",
              "tests/elf_x86_packed_application_reference.py")
REGALLOC_TEST = "tests/c/test_self_backend_aarch64_regalloc.py"
REGALLOC_FACTS_TEST = "tests/python/test_aarch64_regalloc_single_seed.py"
FACTS_STRUCTURE_TEST = "tests/python/test_aarch64_facts_structure.py"
FACTS_NATIVE_BOUNDARY_TEST = "tests/python/test_aarch64_facts_native_boundary.py"
HOME_SUMMARY_CI_TEST = "tests/python/test_aarch64_home_summary_ci.py"
SCALAR_PROJECTION_TEST = "tests/python/test_scalar_arena_projections.py"
MANAGED_SCRATCH_TEST = "tests/python/test_managed_scratch_retirement.py"
MANAGED_MEMORY_TEST = "tests/python/test_managed_scratch_host_memory.py"
MANAGED_PHI_TEST = "tests/python/test_managed_phi_liveness.py"
MANAGED_DIRTY_ROWS_TEST = "tests/python/test_managed_liveness_dirty_rows.py"
CALLEE_SAVED_TEST = "tests/c/test_self_backend_aarch64_callee_saved.py"
BRANCH_PARITY_TEST = "tests/python/test_aarch64_branch_layout_parity.py"
REGALLOC_INPUTS = (REGALLOC_TEST, CALLEE_SAVED_TEST, BRANCH_PARITY_TEST,
                  "tests/aarch64_regalloc_scan_reference.py",
                  "tests/aarch64_regalloc_facts_reference.py", REGALLOC_FACTS_TEST,
                  FACTS_STRUCTURE_TEST, FACTS_NATIVE_BOUNDARY_TEST, HOME_SUMMARY_CI_TEST,
                  SCALAR_PROJECTION_TEST, "tests/scalar_arena_projection_reference.json",
                  "tests/scalar_arena_projection_ci_reference.py",
                  "tests/python/test_precise_stackmap_abi.py",
                  "tests/python/test_unsafe_syscall6.py",
                  MANAGED_SCRATCH_TEST, MANAGED_MEMORY_TEST, MANAGED_PHI_TEST,
                  "tests/managed_scratch_retirement_reference.json",
                  MANAGED_DIRTY_ROWS_TEST,
                  "tests/managed_liveness_dirty_rows_reference.json",
                  "tests/managed_liveness_dirty_rows_reference.py",
                  "tests/managed_liveness_dirty_rows_ci_reference.py")
REGALLOC_MODES = ("default", "function", "local")
REGALLOC_SHAPES = ("safe", "call", "loop", "empty", "syscall", "inline-error",
                  "madd", "reload", "vararg", "invalid-low", "invalid-high")
REGALLOC_HOST_NODES = (
    *(REGALLOC_TEST + "::test_aarch64_regalloc_reuses_function_facts_without_changing_state["
      + calls + "-" + mode + "-" + shape + "]"
      for calls in ("0", "1") for mode in REGALLOC_MODES for shape in REGALLOC_SHAPES),
    *(REGALLOC_TEST + "::test_aarch64_regalloc_scan_reuse_preserves_syscall_failure[" + mode + "]"
      for mode in REGALLOC_MODES),
    *(CALLEE_SAVED_TEST + "::test_scan_reuse_preserves_callee_saved_assembly_and_frames[" + mode + "]"
      for mode in REGALLOC_MODES),
    *(BRANCH_PARITY_TEST + "::test_regalloc_scan_reuse_preserves_assembly_native_sections_and_stackmaps["
      + mode + "-" + shape + "]" for mode in REGALLOC_MODES for shape in ("branches", "root-reloads")),
    CALLEE_SAVED_TEST + "::test_callee_saved_registers_are_saved_and_restored_on_every_exit",
    CALLEE_SAVED_TEST + "::test_mode_off_emits_no_callee_saved_registers",
    *(MANAGED_PHI_TEST + "::test_phi_liveness_matches_set_oracle_without_repeated_fixpoint_scans["
      + selection + "-" + loop + "-" + fanin + "]"
      for selection in ("all", "alternating", "none")
      for loop in ("False", "True") for fanin in ("2", "9", "65")),
    BRANCH_PARITY_TEST + "::test_no_suffix_parts_are_not_sliced_or_extended_twice",
)
REGALLOC_NATIVE_NODES = tuple(
    REGALLOC_TEST + "::test_aarch64_call_result_executes_through_indexed_emission[" + mode + "]"
    for mode in REGALLOC_MODES
)
X87_PREFIX_TEST = "tests/python/test_x86_64_x87_table_prefix.py"
X87_ENCODING_TEST = "tests/python/test_x86_64_x87_table_encoding.py"
X87_NATIVE_TEST = "tests/python/test_x86_64_x87_table_native.py"
X87_INPUTS = (X87_PREFIX_TEST, X87_ENCODING_TEST, X87_NATIVE_TEST,
              "tests/x86_64_x87_reference.py", "tests/x86_64_x87_source.py",
              "tests/x86_64_operand_whitespace.py",
              "pcc/backend/x86_64_encode.py", "pcc/backend/x86_64_asm_driver.py",
              "tests/python/owned_regression_support.py")
ASSEMBLY_PROFILE_TEST = "tests/python/test_host_assembly_profile.py"
ASSEMBLY_PROFILE_INPUTS = (ASSEMBLY_PROFILE_TEST, "scripts/host_assembly_profile.py")
RSS_READER_TEST = "tests/python/test_worker_resource_reader.py"
RSS_PUBLICATION_TEST = "tests/python/test_worker_resource_publication.py"
RSS_PUBLISHER_TEST = "tests/python/test_worker_resource_publisher.py"
RSS_NATIVE_TEST = "tests/python/test_worker_resource_native_windows.py"
RSS_FILESYSTEM_TEST = "tests/python/test_worker_resource_windows_filesystem.py"
RSS_READER_INPUTS = (RSS_READER_TEST, RSS_PUBLICATION_TEST, RSS_PUBLISHER_TEST,
                     RSS_NATIVE_TEST, RSS_FILESYSTEM_TEST,
                     "pcc/frontends/python/worker_resource_plan.py",
                     "pcc/frontends/python/worker_resource_host_windows.py",
                     "pcc/runtime/py/freestanding_windows_rss.py",
                     "pcc/runtime/py/freestanding_windows_directory.py",
                     "pcc/frontends/python/pipeline_import_scan.py")
STACKMAP_BUDGET_TEST = 'tests/python/test_compact_stackmap_processing_budget.py'
STACKMAP_HOST_BUDGET_TEST = 'tests/python/test_stackmap_host_budget.py'
PE_IMAGE_STACKMAP_PARTITION_TEST = 'tests/python/test_pe_image_stackmap_partition.py'
PE_IMAGE_STACKMAP_PARTITION_INPUTS = (
    PE_IMAGE_STACKMAP_PARTITION_TEST,
    'pcc/backend/pe_x86_64.py',
    'pcc/backend/pe_host_budget.py',
    'pcc/backend/coff_x86_64.py',
    'pcc/backend/precise_stackmap.py',
)
MACHO_HOST_RELOCATIONS_TEST = 'tests/python/test_macho_host_relocations.py'
MACHO_ARCHIVE_SYMBOL_STATE_TEST = 'tests/python/test_macho_archive_symbol_state.py'
MACHO_SECTION_PLACEMENT_TEST = 'tests/python/test_macho_section_placement.py'
MACHO_ARCHIVE_FRAMING_TEST = 'tests/python/test_macho_archive_framing.py'
MACHO_WINDOWED_PREPARE_TEST = 'tests/python/test_macho_windowed_prepare.py'
MACHO_WINDOWED_PARITY_TEST = 'tests/python/test_macho_windowed_parity.py'
MACHO_SECTION_TARGET_INDEX_TEST = 'tests/python/test_macho_section_target_index.py'
MACHO_ARCHIVE_RETIREMENT_TEST = 'tests/python/test_macho_archive_retirement.py'
STACKMAP_BUDGET_INPUTS = (
    'tests/python/test_compact_stackmap_processing_budget.py',
    'tests/python/test_stackmap_host_budget.py',
    'tests/python/test_macho_host_relocations.py',
    'tests/python/test_macho_archive_symbol_state.py',
    'tests/python/test_macho_section_placement.py',
    'tests/python/test_macho_archive_framing.py',
    'tests/python/test_macho_windowed_prepare.py',
    'tests/python/test_macho_windowed_parity.py',
    'tests/python/test_macho_section_target_index.py',
    'tests/python/test_macho_archive_retirement.py',
    'pcc/backend/precise_stackmap.py',
    'pcc/backend/macho_link.py',
    'pcc/backend/macho_exec.py',
    'pcc/backend/native_object.py',
    'pcc/backend/macho_obj.py',
    'pcc/backend/macho_archive.py',
    'pcc/frontends/python/pipeline_self_backend_link.py',
    'pcc/backend/stackmap_host_budget.py',
    'pcc/backend/macho_host_relocations.py',
    'pcc/backend/macho_host_prepare.py',
    'pcc/backend/macho_spec.py',
    'pcc/backend/macho_parallel.py',
    'pcc/frontends/python/pipeline_dependency_closure.py',
)
PARSE_BUFFER_TEST = 'tests/python/test_x86_64_parse_buffer_publication.py'
PARSE_BUFFER_OBJECT_TEST = 'tests/python/test_x86_64_parse_buffer_objects.py'
PARSE_BUFFER_INPUTS = (
    'tests/python/test_x86_64_parse_buffer_publication.py',
    'tests/python/test_x86_64_parse_buffer_objects.py',
    'tests/python/test_x86_64_assembler_lifetime.py',
    'tests/python/test_x86_64_numeric_data_batching.py',
    'tests/python/data/x86_64_parse_buffer_before_81776.py.txt',
    'tests/python/data/x86_64_asm_driver_two_pass.py.txt',
    'pcc/backend/x86_64_asm_driver.py',
)
TREE_RECOVERY_TEST = 'tests/python/test_worker_current_rss.py'
TREE_RECOVERY_GUARD_TEST = 'tests/python/test_worker_tree_recovery_guard.py'
TREE_RECOVERY_INPUTS = (
    'tests/python/test_worker_current_rss.py',
    'tests/python/test_worker_tree_recovery_guard.py',
    'tests/python/test_process_tree_sample_tool.py',
    'tests/python/test_worker_guard_cadence.py',
    'pcc/frontends/python/worker_resource_plan.py',
    'pcc/frontends/python/worker_process_pool.py',
    'pcc/frontends/python/worker_resource_host_recovery.py',
    'scripts/run_process_tree_sample.py',
    'scripts/run_pcc_stage1_build.py',
)
TWO_ROOT_CLEANUP_TESTS = (
    "tests/python/test_owned_cleanup_helpers.py",
    "tests/python/test_owned_cleanup_helper_ir.py",
    "tests/python/test_slot_call_cleanup_reuse.py",
    "tests/python/test_slot_call_exception_cleanup.py",
)
TWO_ROOT_CLEANUP_INPUTS = (
    *TWO_ROOT_CLEANUP_TESTS,
    "pcc/frontends/python/codegen/call_object_lowering.py",
    "pcc/frontends/python/codegen/runtime_abi.py",
    "pcc/runtime/py/py_cleanup_runtime.py",
    "pcc/runtime/include/py_runtime.h",
    "tests/python/test_foreign_address_leases.py",
    "tests/python/test_slot_call_lexical_roots.py",
    "tests/python/test_shared_call_binding.py",
    "tests/owned_ir_validation.py",
    "tests/python/owned_regression_support.py",
    "tests/python/process_timeout.py",
)
RUNTIME_ABI_CHUNK_TEST = "tests/python/test_runtime_abi_chunking.py"
GC_RELOCATION_PAGE_CURSOR_MODEL_TEST = "tests/python/test_gc_relocation_page_cursor_model.py"
GC_RELOCATION_PAGE_CURSOR_INPUTS = (
    GC_RELOCATION_PAGE_CURSOR_MODEL_TEST,
    "pcc/runtime/py/freestanding_gc_relocation_selector.py",
    "pcc/runtime/py/freestanding_gc_zpage_lifecycle.py",
)
GC_MARK_COLOR_ATOMIC_TEST = "tests/python/test_gc_mark_color_atomic.py"
HOST_FILES = ("tests/python/test_worker_size_priors.py", PROCESS_TEST,
              "tests/test_ci_worker_size_prior_gate.py", CLOSURE_TEST,
              "tests/python/test_worker_guard_cadence.py",
              "tests/c/test_self_backend_function_body_lines.py",
              "tests/python/test_compiled_default_pass_tier.py",
              "tests/python/test_owned_mem2reg_frontiers.py",
              "tests/c/test_self_backend_text_index.py",
              "tests/c/test_self_backend_type_parser.py",
              "tests/python/test_host_indexed_process_split.py",
              "tests/python/test_dynamic_handoff_slots.py",
              "tests/python/test_indexed_phase_timing.py",
              "tests/python/test_x86_64_windows_packed_stackmaps.py",
              X87_PREFIX_TEST, X87_ENCODING_TEST, ASSEMBLY_PROFILE_TEST, REGALLOC_FACTS_TEST,
              RSS_READER_TEST, RSS_PUBLICATION_TEST, SCALAR_PROJECTION_TEST, RSS_PUBLISHER_TEST,
              ELF_RELOCATION_CONTEXT_TEST, STACKMAP_BUDGET_TEST, STACKMAP_HOST_BUDGET_TEST,
              PARSE_BUFFER_TEST, PARSE_BUFFER_OBJECT_TEST, ARM_CALL_VENEERS_TEST,
              TREE_RECOVERY_TEST, TREE_RECOVERY_GUARD_TEST,
              TAR_ATTRIBUTES_TEST, TAR_STRICT_ORACLE_TEST,
              MANAGED_SCRATCH_TEST, MANAGED_MEMORY_TEST,
              ELF_PACKED_TEST, ELF_PACKED_SOURCE_TEST,
              ELF_NEGATIVE_TEST, ELF_NEGATIVE_SOURCE_TEST,
              ELF_PROGRESS_TEST, ELF_PROGRESS_SOURCE_TEST,
              ELF_APPLICATION_TEST, ELF_APPLICATION_SOURCE_TEST,
              MANAGED_DIRTY_ROWS_TEST, FACTS_STRUCTURE_TEST, FACTS_NATIVE_BOUNDARY_TEST,
              HOME_SUMMARY_CI_TEST, MACHO_HOST_RELOCATIONS_TEST,
              MACHO_ARCHIVE_SYMBOL_STATE_TEST, MACHO_SECTION_PLACEMENT_TEST,
              MACHO_ARCHIVE_FRAMING_TEST, MACHO_WINDOWED_PREPARE_TEST,
              MACHO_WINDOWED_PARITY_TEST, PE_IMAGE_STACKMAP_PARTITION_TEST,
              *TWO_ROOT_CLEANUP_TESTS, RUNTIME_ABI_CHUNK_TEST, GC_RELOCATION_PAGE_CURSOR_MODEL_TEST,
              MACHO_SECTION_TARGET_INDEX_TEST, WIN64_DIRECT_SCALAR_TEST, CONSUMED_UNSAFE_TEST,
              IR_BUILDER_CALL9_TEST, MACHO_ARCHIVE_RETIREMENT_TEST, GC_MARK_COLOR_ATOMIC_TEST)
# Existing behavioral coverage remains. Thirty-five finite facts controls
# and one CI-only helper A/B share the same default-xdist invocation.
HOST_COUNTS = (106, 3, 291, 0, 6, 29, 25, 7, 11, 49, 20, 21, 413, 17, 4, 5, 60, 67, 24, 13, 89, 55, 16, 136, 19, 117, 23, 55, 84, 12, 51, 109, 34, 1, 17, 1, 8, 1, 9, 1, 8, 1, 95, 43, 10, 1, 38, 43, 41, 46, 42, 31, 25, 43, 21, 6, 43, 4, 51, 11, 111, 177, 9, 12, 26)
PHASES = ("preflight", "stage1", "pcc1")


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


_RELEASE_LEVELS = {"a": "alpha", "b": "beta", "rc": "candidate"}


def python_version_pin(text):
    """sys.version_info named by .python-version ("3.15.0", "3.15.0rc1")."""
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?", text.strip())
    if match is None:
        raise ValueError(".python-version must name one CPython release: " + text.strip())
    major, minor, micro, level, serial = match.groups()
    if level is None:
        return (int(major), int(minor), int(micro), "final", 0)
    return (int(major), int(minor), int(micro), _RELEASE_LEVELS[level], int(serial))


def source_identity():
    from scripts.bootstrap_platform import source_identity as identity
    value = identity()
    # Every platform runs the one interpreter .python-version names.
    expected_python = python_version_pin((ROOT / ".python-version").read_text())
    assert tuple(sys.version_info) == expected_python, "use the .python-version interpreter"
    import pytest
    import xdist
    assert (pytest.__version__, xdist.__version__) == ("9.0.3", "3.8.0"), "use the frozen development lock"
    value["test_versions"] = {"pytest": pytest.__version__, "xdist": xdist.__version__}
    value["test_inputs"] = {name: sha(ROOT / name) for name in (*HOST_FILES, *EXIT_INPUTS, *ARM_TRANSPORT_INPUTS, *REGALLOC_INPUTS, *ELF_INPUTS, *X87_INPUTS, *ASSEMBLY_PROFILE_INPUTS, *RSS_READER_INPUTS, *STACKMAP_BUDGET_INPUTS, *PARSE_BUFFER_INPUTS, *TREE_RECOVERY_INPUTS, *TAR_ATTRIBUTES_INPUTS,
                           *PE_IMAGE_STACKMAP_PARTITION_INPUTS, *TWO_ROOT_CLEANUP_INPUTS,
                           *GC_RELOCATION_PAGE_CURSOR_INPUTS, *WIN64_DIRECT_SCALAR_INPUTS,
                           *CONSUMED_UNSAFE_INPUTS,
                           "tests/strict_exception_oracle.py", "tests/fixtures/strict_stackmap_catch.ll",
                           "tests/owned_ir_validation.py",
                           "tests/fixtures/native/worker_size_priors.py", "conftest.py")}
    value["python"] = {"executable": sys.executable, "sha256": sha(sys.executable),
                       "version": sys.version}
    return value


def environment():
    assert not os.environ.get("PYTEST_ADDOPTS"), "default pytest configuration must be unmodified"
    assert os.environ.get("PCC_PYTEST_AUTO_CLEAN", "0").lower() in ("", "0", "false", "off"), "qualification cannot delete source outputs automatically"
    assert os.environ.get("PCC_WITH_THREADS", "1") in ("", "1"), "every runtime is threaded"
    assert os.environ.get("PCC_REFCOUNT_KIND", "atomic") in ("", "atomic")
    assert not os.environ.get("PCC_TEST_COMPILER"), "use the explicit pcc0/pcc1 fixture parameter"
    assert not os.environ.get("PCC_BOOTSTRAP_EXTERNAL_MEMORY_GUARD"), "bootstrap owns its Stage1 guard"
    assert not os.environ.get("PCC_BOOTSTRAP_MAX_TREE_RSS_BYTES"), "explicit cap overrides the required automatic Mac budget"
    value = dict(os.environ)
    if sys.platform == "darwin":
        assert not value.get("UV_PROJECT_ENVIRONMENT"), "use the ordinary project environment"
        assert sha(ROOT / ".venv/bin/python") == sha(sys.executable)
        value.update(UV_PYTHON=sys.executable, UV_NO_SYNC="1")
    value.update(PCC_NO_AUTO_PCC1="1", PCC_TEST_COMPILER_STRICT="1",
                 PCC_PYTEST_AUTO_CLEAN="0", PCC_WITH_THREADS="1", PCC_REFCOUNT_KIND="atomic",
                 PCC_PY_FRONTEND_IR_CACHE="0", PCC_SELF_BACKEND_OBJECT_CACHE="0",
                 PCC_RUNTIME_CC="pcc", PCC_RUNTIME_HIGH="py", PCC_RUNTIME_BUILD="owned",
                 PCC_SELF_LINK="pcc", PCC_SELF_OBJ="pcc", PCC_IR_TO_OBJ_EMITTER="pcc",
                 PCC_PYTHON_IR_PASSES="off", PCC_GC_BACKEND="0")
    return value


def guarded(command, directory, timeout, env):
    """One existing guard owns the tree and performance lock for this command."""
    directory.mkdir(parents=True, exist_ok=False)
    save(directory / "command.json", {"argv": command, "timeout": timeout})
    if sys.platform == "win32":
        from scripts.file_lock import exclusive_file_lock
        from scripts.platform_process_watchdog import run
        child_env = dict(env, PCC_WORKER_TREE_BUDGET_BYTES="17179869184")
        with exclusive_file_lock(ROOT / "build/.pcc-performance.lock", blocking=False):
            result = run(command, cwd=ROOT, env=child_env, log_path=directory / "target.stdout",
                         timeout=timeout, rss_limit=17179869184)
        assert result["command"] == list(command) and 0 <= result["peak_tree_rss"] <= 17179869184
        # The existing watchdog returns only after exit 0 and finally wait();
        # it exposes no independent post-cleanup PID inventory. Keep that limit.
        save(directory / "result.json", {"guard": "platform_process_watchdog.run",
             "return_contract": "normal return after child exit 0 and finally wait",
             "requested_rss_limit": 17179869184, "requested_timeout": timeout, **result})
    else:
        options = (["--auto-tree-rss-ceiling-bytes", "4294967296", "--min-tree-rss-bytes", "2147483648",
                    "--darwin-preflight-reserve-bytes", "536870912"] if sys.platform == "darwin"
                   else ["--max-tree-rss-bytes", "17179869184"])
        argv = [sys.executable, str(ROOT / "scripts/run_process_tree_sample.py"),
                "--result", str(directory / "result.json"), "--samples", str(directory / "samples.tsv"),
                "--stdout", str(directory / "target.stdout"), "--stderr", str(directory / "target.stderr"),
                "--cwd", str(ROOT), "--timeout", str(timeout), *options, "--", *command]
        subprocess.run(argv, cwd=ROOT, env=env, check=True)
        verify_posix_guard(json.loads((directory / "result.json").read_text()), timeout)


def verify_posix_guard(result, timeout):
    assert result["status"] == "COMPLETE" and result["returncode"] == 0
    assert result["timeout_s"] == timeout
    cap = result["max_tree_rss_bytes"]
    if sys.platform == "darwin":
        assert 2147483648 <= cap <= 4294967296
        assert result["darwin_preflight_reserve_bytes"] == 536870912
    else:
        assert cap == 17179869184
    assert 0 <= result["peak_tree_rss_bytes"] <= cap
    assert not result.get("post_exit_cleanup_pids"), "worker exited before its descendants"
    # terminal_processes is the last live sample, not a post-cleanup inventory.
    return {"max_tree_rss_bytes": cap, "peak_tree_rss_bytes": result["peak_tree_rss_bytes"],
            "timeout_s": timeout, "status": result["status"], "returncode": result["returncode"]}


def pytest_command(directory, nodes, integration=False):
    # In particular, never clear addopts or override -n/--dist.
    command = [sys.executable, "-m", "pytest", "-x", "-vv", "--tb=short",
               "-p", "scripts.pytest_live_report", "--pcc-live-report", str(directory / "live.jsonl"),
               "--junitxml", str(directory / "junit.xml"), "--basetemp", str(directory / "tests")]
    return command + (["-m", "integration"] if integration else []) + list(nodes)


def verify_pytest(directory, expected, file_counts=None):
    rows = [json.loads(line) for line in (directory / "live.jsonl").read_text().splitlines()]
    starts = [r for r in rows if r["event"] == "start"]
    assert len(starts) == 1 and not starts[0]["collect_only"] and not starts[0]["override_ini"]
    collections = [r["nodeids"] for r in rows if r["event"] == "collected"]
    assert len(collections) == 1 and len(set(collections[0])) == len(collections[0])
    nodes = collections[0]
    if file_counts is None:
        assert nodes == list(expected), (nodes, expected)
    else:
        assert len(nodes) == sum(file_counts)
        assert all(sum(node.startswith(path + "::") for node in nodes) == count
                   for path, count in zip(expected, file_counts))
        assert all(any(node.startswith(path + "::") for path in expected) for node in nodes)
    reports = [r for r in rows if r["event"] == "report"]
    assert len(reports) == 3 * len(nodes)
    for node in nodes:
        actual = [r for r in reports if r["nodeid"] == node]
        assert [r["when"] for r in actual] == ["setup", "call", "teardown"]
        assert all(r["outcome"] == "passed" and "wasxfail" not in r for r in actual)
    finish = [r for r in rows if r["event"] == "finish"]
    assert len(finish) == 1 and finish[0]["exitstatus"] == 0
    assert finish[0]["testsfailed"] == 0 and finish[0]["testscollected"] == len(nodes)
    lines = (directory / "target.stdout").read_text().splitlines()
    assert "created: 6/6 workers" in lines
    assert "scheduling tests via LoadGroupScheduling" in lines
    noun = "item" if len(nodes) == 1 else "items"
    assert "6 workers [" + str(len(nodes)) + " " + noun + "]" in lines
    return nodes


def run_pytest(out, name, nodes, env, *, integration=False, counts=None, timeout=300):
    directory = out / name
    guarded(pytest_command(directory, nodes, integration), directory, timeout, env)
    return verify_pytest(directory, nodes, counts)


def runtime_identity(archive):
    from tests.runtime_fixture_provenance import _verified_test_runtime_archive
    checked, manifest = _verified_test_runtime_archive(archive)
    assert checked == archive.resolve()
    return {"path": str(checked), "target": manifest["target_triple"],
            "config": dict(manifest["members"][0]["runtime_build_config"]),
            "hashes": {suffix: sha(str(checked) + suffix)
                       for suffix in ("", ".provenance.json", ".capi_syms")}}


def native_gate(out, owner, env):
    node = PROCESS_TEST + "::test_size_prior_native_processes_five_collectors[" + owner + "]"
    name = "native-" + owner
    # The small changed-shape program shares the existing pcc0 invocation and
    # watchdog. Give it selection priority; the unchanged xdist scheduler may
    # still run both nodes concurrently and -x may stop in-flight work.
    # Native-pcc1 scope stays the original worker control.
    nodes = ([X87_NATIVE_TEST + "::test_x87_tables_native_five_gc[pcc0]", node]
             if owner == "pcc0" else [node])
    run_pytest(out, name, nodes, env, integration=True, timeout=NATIVE_GATE_TIMEOUT_S)
    files = list((out / name).rglob("native-worker-size-priors.json"))
    assert len(files) == 1
    receipt = json.loads(files[0].read_text())
    assert receipt["status"] == "PASS" and receipt["compiler_parameter"] == owner
    assert receipt["source_sha256"] == sha(ROOT / "tests/fixtures/native/worker_size_priors.py")
    assert receipt["runtime_archive_sha256"] == sha(env["PCC_RUNTIME_ARCHIVE"])
    assert len(receipt["executions"]) == 10
    assert {(row["case"], row["requested_collector"]) for row in receipt["executions"]} == {
        (case, gc) for case in ("width", "growth") for gc in range(5)}
    from scripts.verify_nolibpython import linked_libraries
    binary = files[0].parent / ("prior-worker.exe" if sys.platform == "win32" else "prior-worker.out")
    assert sha(binary) == receipt["binary_sha256"]
    assert not any("python" in name.lower() or "llvm" in name.lower()
                   for name in linked_libraries(binary.read_bytes()))
    return receipt["binary_sha256"]


def admission_metrics(text, compile_wall):
    """Reduce original coordinator events; worker time is never added to wall."""
    assert compile_wall > 0
    active, cancelled, exports, units, completed = {}, set(), {}, {}, set()
    area, exclusive_intervals, seen, events = 0.0, [], set(), []
    counts = {"start": 0, "calibrate": 0, "cancel": 0, "retire": 0, "exclusive": 0}
    for line in text.splitlines():
        if not line.startswith("pcc frontend admission ") or line in seen:
            continue
        seen.add(line)
        row = {}
        for key, value in re.findall(r'(\w+)=(.*?)(?= \w+=|$)', line):
            try:
                row[key] = ast.literal_eval(value)
            except (ValueError, SyntaxError):
                row[key] = value
        offset = row.get("mapping_offset", 0)
        if offset:
            assert events, "orphan mapping continuation"
            first = events[-1]
            repeated = set(row) - {"indices", "modules", "mapping_offset", "monotonic_s"}
            assert all(row[key] == first[key] for key in repeated), "mismatched mapping continuation"
            assert offset == max(len(first["indices"]), len(first.get("modules", []))), "missing or duplicated mapping page"
            first["indices"].extend(row["indices"])
            if "modules" in row:
                first["modules"].extend(row["modules"])
        else:
            events.append(row)
    for row in events:
        if "module_count" in row:
            assert len(row["modules"]) == row["module_count"]
            if row["phase"] == "preload-delta":
                assert row["indices"] == []  # Existing module-only full-graph diagnostics.
            else:
                assert len(row["indices"]) == row["module_count"]
        event, stamp = row["event"], row["monotonic_s"]
        key, task = (row["phase"], row["task"], row["pid"]), (row["phase"], row["task"])
        indices = tuple(row["indices"])
        if event in counts:
            counts[event] += 1
        if event in ("start", "calibrate"):
            assert key not in active
            active[key] = (stamp, stamp if event == "calibrate" or task in cancelled else None)
            if row["phase"] == "export":
                assert len(row["inputs"]) == 6
                exports[indices] = row["inputs"][0]
            if row["phase"] in ("codegen", "indexed-frontend"):
                assert len(row["inputs"]) == 6
                units[indices] = row["inputs"][0]
        elif event == "exclusive":
            start, exclusive = active[key]
            assert exclusive is None
            active[key] = (start, stamp)
        elif event in ("retire", "cancel", "failed", "unverified-retire"):
            start, exclusive = active.pop(key)
            assert stamp >= start
            area += stamp - start
            if exclusive is not None:
                exclusive_intervals.append((exclusive, stamp))
            if event == "cancel":
                cancelled.add(task)
            elif event == "retire" and row["phase"] in ("codegen", "indexed-backend"):
                completed.update(indices)
        else:
            raise AssertionError("unknown admission event: " + str(event))
    assert exports and units, "missing actual source/admission evidence"
    total_bytes = sum(exports.values())
    completed_bytes = sum(size for indices, size in units.items() if set(indices) <= completed)
    exclusive_wall, end = 0.0, -1.0
    for start, stop in sorted(exclusive_intervals):
        exclusive_wall += max(0.0, stop - max(start, end))
        end = max(end, stop)
    return {"compile_wall_s": compile_wall, "worker_seconds": area,
            "average_workers": area / compile_wall,
            "exclusive_wall_s": exclusive_wall, "events": counts,
            "source_bytes_total": total_bytes, "source_bytes_complete": completed_bytes,
            "source_byte_fraction": completed_bytes / total_bytes,
            "completed_modules": len(completed),
            "planned_modules": len({index for indices in exports for index in indices}),
            "open_attempts": len(active),
            "scope": "original event durations; incomplete attempts excluded from area until terminal; no CPU inference"}


def prepare_runtime(out, archive, env):
    """Build once per independent job, then verify the complete provenance."""
    build_env = dict(env)
    build_env.pop("PCC_TEST_NO_NATIVE_PROVISIONING", None)
    build_env.pop("PCC_RUNTIME_ARCHIVE", None)
    guarded([sys.executable, "-m", "pcc.frontends.python.owned_runtime_build", "--output", str(archive)],
            out / "runtime-build", 1200, build_env)
    return runtime_identity(archive)


def run(out, phase, *, strict_closure_timeout=300, full_chain=False):
    assert phase in PHASES
    assert strict_closure_timeout > 0
    assert ((sys.platform == "darwin" and platform.machine().lower() == "arm64")
            or (sys.platform.startswith("linux") and platform.machine().lower() in ("x86_64", "aarch64", "arm64"))
            or (sys.platform == "win32" and platform.machine().lower() in ("amd64", "x86_64")))
    out = out.resolve()
    if phase in ("preflight", "stage1"):
        assert not out.exists(), phase + " requires a fresh output root"
    receipt_path = out / (phase + ".json")
    assert not receipt_path.exists(), "refuse stale phase receipt"
    source = source_identity()
    env = environment()
    archive = out / "runtime/libpy_runtime_pcc_py.a"
    receipt = {"status": "RUNNING", "phase": phase, "source": source}
    save(receipt_path, receipt)
    try:
        if phase == "preflight":
            run_pytest(out, "default-xdist", HOST_FILES, env, counts=HOST_COUNTS)
            receipt["runtime"] = prepare_runtime(out, archive, env)
            env.update(PCC_RUNTIME_ARCHIVE=str(archive), PCC_TEST_NO_NATIVE_PROVISIONING="1")
            if sys.platform == "darwin":
                # Each exact node retains the six-worker default and the 300s
                # tree watchdog. Owned executable probes also have 10s bounds.
                run_pytest(out, "aarch64-regalloc-host", REGALLOC_HOST_NODES, env)
                run_pytest(out, "aarch64-regalloc-native", REGALLOC_NATIVE_NODES, env)
            if sys.platform == "win32":
                run_pytest(out, "windows-exit-host", [EXIT_TEST], env, counts=(9,))
                run_pytest(out, "windows-exit-native", [EXIT_TEST, TAR_ATTRIBUTES_TEST],
                           env, integration=True, counts=(6, 2))
            elif sys.platform.startswith("linux") and platform.machine().lower() in ("aarch64", "arm64"):
                run_pytest(out, "linux-arm-transport-host", [ARM_TRANSPORT_TEST], env, counts=(7,))
                run_pytest(out, "linux-arm-transport-routes", [ARM_TRANSPORT_TEST], env, integration=True, counts=(8,))
                run_pytest(out, "linux-arm-transport-native", [ARM_TRANSPORT_NATIVE_TEST
                           + "::test_linux_aarch64_transport_executes_tls_varargs_and_managed_reload"],
                           env, integration=True)
            if sys.platform.startswith("linux"):
                run_pytest(out, "linux-elf-format", [ELF_FORMAT_TEST], env, counts=(67,))
                # Match the staging test's actual pcc_gate predicate exactly.
                # ARM deselects two x86 executable probes at collection; skips
                # in any selected case still fail verify_pytest.
                staging_count = 69 if platform.machine() in ("x86_64", "amd64") else 67
                run_pytest(out, "linux-elf-staging", [ELF_STAGING_TEST], env,
                           counts=(staging_count,))
                # The whole owner file also contains unmarked Darwin native
                # execution. Select only the affected Linux dispatch contract.
                run_pytest(out, "linux-elf-owner", [ELF_OWNER_NODE], env)
                run_pytest(out, "linux-elf-x86-packed",
                           [ELF_X86_CLASSIFICATION_TEST, ELF_X86_APPLICATION_TEST],
                           env, counts=(26, 35))
            native_gate(out, "pcc0", env)
            if sys.platform == "darwin":
                # The single-process strict closure peaks at ~8.6 GB on Linux;
                # the 7 GB macOS runner admits ~3.1 GB and stops it with
                # MEMORY_LIMIT. Record that instead of a failure it cannot avoid.
                receipt["strict-closure"] = {
                    "status": "NOT_RUN",
                    "reason": "macOS hosted runner memory is below the strict closure's peak RSS",
                }
            else:
                run_pytest(out, "strict-closure", [CLOSURE_TEST + "::test_worker_size_prior_modules_strict_target_emission"],
                           env, integration=True, timeout=strict_closure_timeout)
        else:
            if phase == "stage1":
                # Runtime preparation is a build prerequisite, not a validation
                # gate. Never wait for the independent preflight job/artifact.
                receipt["runtime"] = prepare_runtime(out, archive, env)
            else:
                stage = json.loads((out / "stage1.json").read_text())
                assert stage["status"] == "PASS" and stage["source"] == source
                assert runtime_identity(archive) == stage["runtime"]
                receipt["runtime"] = stage["runtime"]
            env.update(PCC_RUNTIME_ARCHIVE=str(archive), PCC_TEST_NO_NATIVE_PROVISIONING="1",
                       PCC_PY_FRONTEND_WORKER_TIMING="1")
            if phase == "stage1":
                destination = ROOT / "build" / ("bootstrap" if sys.platform == "darwin" else "platform-qualification")
                assert not destination.exists()
                if sys.platform == "darwin":
                    env.update(PCC_HOST_INDEXED_PROCESS_SPLIT="1", PCC_BOOTSTRAP_STAGE_TIMEOUT=str(STAGE1_TIMEOUT_S),
                               PCC_BOOTSTRAP_AUTO_TREE_RSS_CEILING_BYTES="4294967296",
                               PCC_BOOTSTRAP_MIN_TREE_RSS_BYTES="2147483648",
                               PCC_BOOTSTRAP_HOST_MEMORY_RESERVE_BYTES="536870912",
                               PCC_BOOTSTRAP_PROFILE_DIR=str(out / "profile"), PCC_BOOTSTRAP_KEEP_PROCESS_SAMPLES="1")
                    command = [sys.executable, "scripts/bootstrap.py", "--backend", "self", "--stage", "1", "--out-dir", str(destination)]
                    binary = destination / "pcc1"
                else:
                    env["PCC_HOST_INDEXED_PROCESS_SPLIT"] = "0"
                    # One shared Stage1, then every collector's chain. A push
                    # qualifies Stage1 plus the native canary under all five
                    # collectors; the Stage2/Stage3 fixed point (--full-chain,
                    # manual dispatch) needs ~66k-116k pcc1 task-s of Stage2
                    # codegen today, beyond a 6 h hosted job.
                    stage_limit = "3" if full_chain else "1"
                    command = [sys.executable, "scripts/bootstrap_platform.py", "--gc", "all", "--stage", stage_limit,
                               "--out-dir", str(destination), "--runtime-archive", str(archive),
                               "--timeout", str(PLATFORM_COMPILE_TIMEOUT_S),
                               "--stage1-timeout", str(STAGE1_TIMEOUT_S),
                               "--rss-limit", "17179869184", "--cpu-budget", "4"]
                    binary = destination / "shared" / ("pcc1.exe" if sys.platform == "win32" else "pcc1")
                # The original bootstrap owns its own lock and tree watchdog.
                # Do not hold an outer copy of that same lock around this call.
                started = time.monotonic()
                with (out / "stage1.log").open("wb") as log:
                    result = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
                entry_wall = time.monotonic() - started
                receipt.update(command=command, returncode=result.returncode, entry_wall_s=entry_wall)
                if result.returncode:
                    originals = (list(destination.glob("stage1.process.*/target.stderr"))
                                 if sys.platform == "darwin" else [destination / "shared/stage1.log"])
                    try:
                        if len(originals) == 1 and originals[0].is_file():
                            receipt["partial_metrics"] = admission_metrics(originals[0].read_text(), entry_wall)
                            receipt["partial_metrics"]["scope"] = "closed-attempt lower bounds divided by bootstrap entry wall; includes validation overhead"
                    except (AssertionError, KeyError, ValueError, OSError) as error:
                        receipt["metrics_error"] = str(error)
                    raise subprocess.CalledProcessError(result.returncode, command)
                receipt["compiler"] = {"path": str(binary), "sha256": sha(binary)}
                if sys.platform == "darwin":
                    result = json.loads((out / "profile/stage1.result.json").read_text())
                    wall = result["compile_wall_ms"] / 1000
                    originals = list(destination.glob("stage1.process.*/target.stderr"))
                    assert len(originals) == 1, "use one original stderr, not replayed console tails"
                    text = originals[0].read_text()
                    guard = json.loads((originals[0].parent / "result.json").read_text())
                    receipt["stage_guard"] = verify_posix_guard(guard, STAGE1_TIMEOUT_S)
                    assert result["returncode"] == result["publish_barrier_returncode"] == 0
                else:
                    result = json.loads((destination / "shared/receipt.json").read_text())
                    wall = result["stages"][0]["seconds"]
                    text = (destination / "shared/stage1.log").read_text()
                receipt["metrics"] = admission_metrics(text, wall)
                save(receipt_path, receipt)
                assert receipt["metrics"]["open_attempts"] == 0
                assert receipt["metrics"]["source_bytes_complete"] == receipt["metrics"]["source_bytes_total"]
                assert receipt["metrics"]["completed_modules"] == receipt["metrics"]["planned_modules"]
                if sys.platform == "darwin":
                    workers = receipt["metrics"]["average_workers"]
                    receipt["darwin_target"] = {
                        "wall_s": wall, "wall_s_max": DARWIN_TARGET_WALL_S,
                        "average_workers": workers, "average_workers_min": DARWIN_TARGET_AVERAGE_WORKERS,
                        "met": wall < DARWIN_TARGET_WALL_S and workers >= DARWIN_TARGET_AVERAGE_WORKERS,
                    }
            else:
                assert sha(stage["compiler"]["path"]) == stage["compiler"]["sha256"]
                env["PCC_CURRENT_PCC1"] = stage["compiler"]["path"]
                receipt["compiler"] = stage["compiler"]
                native_gate(out, "pcc1", env)
                assert sha(stage["compiler"]["path"]) == stage["compiler"]["sha256"], "pcc1 changed during use"
        assert source_identity() == source
        assert runtime_identity(archive) == receipt["runtime"]
        receipt["status"] = "PASS"
    except BaseException as error:
        receipt.update(status="FAIL", error=type(error).__name__ + ": " + str(error))
        raise
    finally:
        save(receipt_path, receipt)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--phase", choices=PHASES, required=True)
    parser.add_argument("--strict-closure-timeout", type=int, default=300,
                        help="strict closure watchdog seconds (local default: 300; CI sets its own budget)")
    parser.add_argument("--full-chain", action="store_true",
                        help="stage1 phase on Linux/Windows: run every collector's Stage2/Stage3 "
                             "fixed point after Stage1 (default: Stage1 and the five-collector canary)")
    args = parser.parse_args(argv)
    if args.strict_closure_timeout <= 0:
        parser.error("--strict-closure-timeout must be positive")
    run(args.out_dir, args.phase, strict_closure_timeout=args.strict_closure_timeout,
        full_chain=args.full_chain)


if __name__ == "__main__":
    main()
