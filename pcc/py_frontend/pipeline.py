"""Python frontend compilation pipeline.

Orchestrates the full pipeline for a single ``.py`` file:

    source(.py)
      -> pcc.py_frontend.parser.parse()        -> Module AST
      -> pcc.py_frontend.type_infer.infer_module() -> typed Module
      -> pcc.py_frontend.codegen.layer1.L1CodeGen().generate() -> LLVM IR text
      -> write .ll to a temp file
      -> clang .ll + pcc/py_runtime/libpy_runtime.a -> native exe

This is the Phase 1 MVP dispatcher. See
``docs/plans/python-frontend-interfaces.md`` for the frozen v0.1
interface contract and ``docs/plans/python-frontend-plan.md`` for the
Phase 1 scope.
"""

from __future__ import annotations

import gc
import os
import importlib
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Optional

from ..backend.self_backend_aarch64_darwin import (
    emit_aarch64_darwin_asm as _emit_aarch64_darwin_asm_native,
)
from ..backend.self_backend_indexed_emit import (
    emit_indexed_module_file as _emit_indexed_module_file,
)
from ..backend.self_backend_parse import (
    parse_self_backend_target_triple as _parse_self_backend_target_triple_native,
)
from ..backend.self_backend_target_match import (
    is_aarch64_darwin_triple as _is_aarch64_darwin_triple_native,
)
from .export_meta import encode_type
from .compile_cache import (
    acquire_python_frontend_ir_cache,
    load_python_frontend_ir_cache,
    plan_python_frontend_ir_cache,
    publish_python_frontend_ir_cache,
    release_python_frontend_ir_cache,
    wait_python_frontend_ir_cache,
)
from .codegen.host_contract import (
    L1_CODEGEN_HOST_ATTRS,
    L1_CODEGEN_HOST_CLASS,
    L1_CODEGEN_HOST_METHODS,
    PROBE_POLICY_CLOSED_WORLD,
    PROBE_POLICY_CONTEXTUAL_MIXIN,
    PROBE_POLICY_STANDALONE,
    contextual_host_for_module,
    contextual_per_module_modules,
    l1_codegen_lowering_host_contract,
    per_module_probe_policy,
)
from .codegen.layer1_support import (
    _default_native_module_exports,
)
from .codegen.port_abi_exports import PORT_ABI_NATIVE_EXPORTS
from . import pipeline_paths as _pipeline_paths
from . import pipeline_profile as _pipeline_profile
from . import pipeline_modes as _pipeline_modes
from . import pipeline_ir_text as _pipeline_ir_text
from . import pipeline_packages as _pipeline_packages
from . import pipeline_pass_config as _pipeline_pass_config
from . import pipeline_pass_driver as _pipeline_pass_driver
from .codegen.debug_info_lowering import debug_info_requested
from . import pipeline_runtime_archive as _pipeline_runtime_archive
from . import pipeline_native_link as _pipeline_native_link
from . import pipeline_ir_split as _pipeline_ir_split
from . import pipeline_ast_wire as _pipeline_ast_wire
from . import pipeline_frontend_workers as _pipeline_frontend_workers
from . import pipeline_frontend_worker_execution as _pipeline_frontend_worker_execution
from . import pipeline_frontend_parallel as _pipeline_frontend_parallel
from . import pipeline_self_backend_host as _pipeline_self_backend_host
from . import pipeline_self_backend_config as _pipeline_self_backend_config
from . import pipeline_self_backend_cache as _pipeline_self_backend_cache
from . import pipeline_self_backend_emit as _pipeline_self_backend_emit
from . import pipeline_self_backend_link as _pipeline_self_backend_link
from . import pipeline_self_link as _pipeline_self_link
from . import pipeline_semantic_layout as _pipeline_semantic_layout
from . import pipeline_targets as _pipeline_targets
from . import pipeline_import_scan as _pipeline_import_scan
from . import pipeline_freestanding as _pipeline_freestanding
from . import pipeline_exports as _pipeline_exports
from . import pipeline_closed_world as _pipeline_closed_world
from . import pipeline_import_policy as _pipeline_import_policy
from . import pipeline_libpython as _pipeline_libpython
from . import pipeline_dependency_closure as _pipeline_dependency_closure
from . import pipeline_context as _pipeline_context
from . import module_action_dag as _module_action_dag

_bootstrap_append_install_prefix_candidates = (
    _pipeline_paths.bootstrap_append_install_prefix_candidates
)
_bootstrap_append_pcc_dir_ancestors = (
    _pipeline_paths.bootstrap_append_pcc_dir_ancestors
)
_bootstrap_append_pcc_dir_candidate = (
    _pipeline_paths.bootstrap_append_pcc_dir_candidate
)
_bootstrap_append_unique_path = _pipeline_paths.bootstrap_append_unique_path
_pcc_dir_has_source_files = _pipeline_paths.pcc_dir_has_source_files
_resolve_pcc_dir_from_environment_impl = (
    _pipeline_paths.resolve_pcc_dir_from_environment
)
_resolve_runtime_paths = _pipeline_paths.resolve_runtime_paths
_runtime_dir_has_runtime_files = _pipeline_paths.runtime_dir_has_runtime_files
_join_strings = _pipeline_paths.join_strings
_join_dotted_parts = _pipeline_paths.join_dotted_parts
_first_string = _pipeline_paths.first_string
_module_name_from_src = _pipeline_paths.module_name_from_src
_module_root_from_src = _pipeline_paths.module_root_from_src
_package_parts_for_module = _pipeline_paths.package_parts_for_module
_resolve_module_src = _pipeline_paths.resolve_module_src
_profile_now_ms = _pipeline_profile.profile_now_ms
_profile_events = _pipeline_profile.profile_events
_profile_totals = _pipeline_profile.profile_totals
_profile_counters = _pipeline_profile.profile_counters
_profile_begin = _pipeline_profile.profile_begin
_profile_end = _pipeline_profile.profile_end
_profile_counter = _pipeline_profile.profile_counter
_profiled_gc_collect = _pipeline_profile.profiled_gc_collect
PyPipelineError = _pipeline_modes.PyPipelineError
_normalize_native_backend_name = _pipeline_modes.normalize_native_backend_name
_resolve_native_backend = _pipeline_modes.resolve_native_backend
_native_backend_kind = _pipeline_modes.native_backend_kind
_resolve_libpython_mode = _pipeline_modes.resolve_libpython_mode
_resolve_ir_scaffold_mode = _pipeline_modes.resolve_ir_scaffold_mode
_finalize_libpython_mode = _pipeline_modes.finalize_libpython_mode
_reject_mixed_extension_object_models = (
    _pipeline_modes.reject_mixed_extension_object_models
)
_normalize_gpu_backend_name = _pipeline_modes.normalize_gpu_backend_name
_resolve_gpu_backend_kind = _pipeline_modes.resolve_gpu_backend_kind
_self_backend_publish_sync_enabled = (
    _pipeline_modes.self_backend_publish_sync_enabled
)
_defined_function_name_from_line = (
    _pipeline_ir_text.defined_function_name_from_line
)
_function_declaration_from_define_line = (
    _pipeline_ir_text.function_declaration_from_define_line
)
_python_ir_pass_export_split_function_text = (
    _pipeline_ir_text.export_split_function_text
)
_python_ir_pass_export_split_function_declaration = (
    _pipeline_ir_text.export_split_function_declaration
)
_llvm_split_export_prefix = _pipeline_ir_text.split_export_prefix
_llvm_split_line_has_internal_linkage = (
    _pipeline_ir_text.line_has_internal_linkage
)
_llvm_split_private_symbol_rename_map = (
    _pipeline_ir_text.private_symbol_rename_map
)
_llvm_split_rename_symbol_name = _pipeline_ir_text.rename_symbol_name
_global_name_from_definition_line = (
    _pipeline_ir_text.global_name_from_definition_line
)
_rename_llvm_global_refs = _pipeline_ir_text.rename_llvm_global_refs
_llvm_global_name_char = _pipeline_ir_text.llvm_global_name_char
_global_declaration_from_definition_line = (
    _pipeline_ir_text.global_declaration_from_definition_line
)
_find_global_kind_pos = _pipeline_ir_text.find_global_kind_pos
_global_initializer_type_text = _pipeline_ir_text.global_initializer_type_text
_find_substring = _pipeline_ir_text.find_substring
_substring_at = _pipeline_ir_text.substring_at
_find_last_char = _pipeline_ir_text.find_last_char
_package_site_roots = _pipeline_packages.package_site_roots
_native_extension_name_uses_cpython_abi = (
    _pipeline_packages.native_extension_name_uses_cpython_abi
)
_resolve_pcc_native_extension_path = (
    _pipeline_packages.resolve_pcc_native_extension_path
)
_resolve_module_src_for_import = _pipeline_packages.resolve_module_src_for_import
_package_site_package_root_for_src = (
    _pipeline_packages.package_site_package_root_for_src
)
_package_site_package_root_for_module_name = (
    _pipeline_packages.package_site_package_root_for_module_name
)
_package_root_no_libpython_diagnostic = (
    _pipeline_packages.package_root_no_libpython_diagnostic
)
_resolve_python_ir_pass_names = (
    _pipeline_pass_config.resolve_python_ir_pass_names
)
_parallel_cpu_budget = _pipeline_pass_config.parallel_cpu_budget
_python_ir_pass_jobs = _pipeline_pass_config.python_ir_pass_jobs
_parse_seconds_text = _pipeline_pass_config.parse_seconds_text
_seconds_debug_text = _pipeline_pass_config.seconds_debug_text
_python_ir_pass_timeout_seconds = (
    _pipeline_pass_config.python_ir_pass_timeout_seconds
)
_python_ir_pass_strict_arg = _pipeline_pass_config.python_ir_pass_strict_arg
_python_ir_pass_batch_size_summary = (
    _pipeline_pass_config.python_ir_pass_batch_size_summary
)
_small_int_decimal = _pipeline_pass_config.small_int_decimal
_python_ir_pass_transport_is_memory = (
    _pipeline_pass_config.python_ir_pass_transport_is_memory
)
_default_python_ir_pass_transport = (
    _pipeline_pass_config.default_python_ir_pass_transport
)
_effective_python_ir_pass_transport_is_memory = (
    _pipeline_pass_config.effective_python_ir_pass_transport_is_memory
)
_python_ir_pass_split_large_modules_enabled = (
    _pipeline_pass_config.python_ir_pass_split_large_modules_enabled
)
_python_ir_pass_split_threshold_bytes = (
    _pipeline_pass_config.python_ir_pass_split_threshold_bytes
)
_python_ir_pass_split_shard_bytes = (
    _pipeline_pass_config.python_ir_pass_split_shard_bytes
)
_python_ir_pass_names_allow_module_sharding = (
    _pipeline_pass_config.python_ir_pass_names_allow_module_sharding
)
_python_ir_pass_skip_prefixes = (
    _pipeline_pass_config.python_ir_pass_skip_prefixes
)
_python_ir_pass_should_skip_module = (
    _pipeline_pass_config.python_ir_pass_should_skip_module
)
_python_ir_pass_skip_modules_for_batch = (
    _pipeline_pass_config.python_ir_pass_skip_modules_for_batch
)
_self_backend_split_int_env = _pipeline_pass_config.positive_int_env
_split_python_ir_module_for_pass_shards = (
    _pipeline_ir_split.split_python_ir_module_for_pass_shards
)
_split_self_backend_ir_module_for_object_shards = (
    _pipeline_ir_split.split_self_backend_ir_module_for_object_shards
)
_self_backend_ir_global_definition_line = (
    _pipeline_ir_split.ir_global_definition_line
)
_self_backend_export_split_global_line = (
    _pipeline_ir_split.export_split_global_line
)
_PY_AST_WIRE_SCHEMA = _pipeline_ast_wire._PY_AST_WIRE_SCHEMA
_PY_AST_WIRE_NODE_KEY = _pipeline_ast_wire._PY_AST_WIRE_NODE_KEY
_PY_AST_WIRE_BYTES_KEY = _pipeline_ast_wire._PY_AST_WIRE_BYTES_KEY
_PY_AST_FIELD_NAME_OVERRIDES = (
    _pipeline_ast_wire._PY_AST_FIELD_NAME_OVERRIDES
)
_PY_AST_BASE_NAME_OVERRIDES = (
    _pipeline_ast_wire._PY_AST_BASE_NAME_OVERRIDES
)
_PY_AST_FIELD_TYPE_OVERRIDES = (
    _pipeline_ast_wire._PY_AST_FIELD_TYPE_OVERRIDES
)
_py_ast_field_type_override = _pipeline_ast_wire._py_ast_field_type_override
_py_ast_bytes_to_wire = _pipeline_ast_wire._py_ast_bytes_to_wire
_py_ast_to_wire = _pipeline_ast_wire._py_ast_to_wire
_py_ast_wire_bytes = _pipeline_ast_wire._py_ast_wire_bytes
_py_ast_wire_field = _pipeline_ast_wire._py_ast_wire_field
_py_ast_wire_tuple_field = _pipeline_ast_wire._py_ast_wire_tuple_field
_py_ast_wire_bool_field = _pipeline_ast_wire._py_ast_wire_bool_field
_py_ast_from_wire = _pipeline_ast_wire._py_ast_from_wire
_py_ast_node_from_wire = _pipeline_ast_wire._py_ast_node_from_wire
_write_py_ast_wire = _pipeline_ast_wire._write_py_ast_wire
_read_py_ast_wire = _pipeline_ast_wire._read_py_ast_wire


def _load_pcc_gpu_kernel_module():
    return importlib.import_module("pcc.gpu_kernel")


def _load_pcc_gpu_metal_module():
    return importlib.import_module("pcc.gpu_metal")


def _resolve_pcc_dir_from_environment() -> str:
    return _resolve_pcc_dir_from_environment_impl(__file__)


# Resolve pcc/py_runtime/ at import time. In CPython source mode this
# file lives under ``.../pcc/py_frontend/``. In compiled bootstrap mode
# ``__file__`` is synthetic and can resolve to the user's current working
# directory, so derive the package root from stable stage-binary ancestors.
(
    _PCC_DIR,
    _PIPELINE_DIR,
    _PY_RUNTIME_DIR,
    _PY_RUNTIME_DIR_CANDIDATES,
) = _resolve_runtime_paths(__file__)
(
    _PY_RUNTIME_DIR_CANDIDATE_1,
    _PY_RUNTIME_DIR_CANDIDATE_2,
    _PY_RUNTIME_DIR_CANDIDATE_3,
    _PY_RUNTIME_DIR_CANDIDATE_4,
    _PY_RUNTIME_DIR_CANDIDATE_5,
) = _PY_RUNTIME_DIR_CANDIDATES

if os.environ.get("PCC_DEBUG_RUNTIME", "").strip():
    try:
        with open("/tmp/pcc_runtime_debug_probe.txt", "a", encoding="utf-8") as _f:
            _f.write("[probe] _PIPELINE_DIR=" + _PIPELINE_DIR + "\n")
            _f.write("[probe] _PCC_DIR=" + _PCC_DIR + "\n")
            _f.write(
                "[probe] candidates="
                + ",".join(
                    [
                        _PY_RUNTIME_DIR_CANDIDATE_1,
                        _PY_RUNTIME_DIR_CANDIDATE_2,
                        _PY_RUNTIME_DIR_CANDIDATE_3,
                        _PY_RUNTIME_DIR_CANDIDATE_4,
                    ]
                )
                + "\n"
            )
    except Exception:
        pass
_PY_RUNTIME_ARCHIVE = str(os.path.join(_PY_RUNTIME_DIR, "libpy_runtime.a"))
_PY_RUNTIME_ARCHIVE_LIBPYTHON = str(
    os.path.join(
        _PY_RUNTIME_DIR,
        "libpy_runtime_libpython.a",
    )
)
_PY_RUNTIME_ARCHIVE_PCC = str(
    os.path.join(
        _PY_RUNTIME_DIR,
        "libpy_runtime_pcc.a",
    )
)
_PY_RUNTIME_ARCHIVE_PCC_PY = str(
    os.path.join(
        _PY_RUNTIME_DIR,
        "libpy_runtime_pcc_py.a",
    )
)
_PY_RUNTIME_ARCHIVE_PCC_PY_LIBPYTHON = str(
    os.path.join(
        _PY_RUNTIME_DIR,
        "libpy_runtime_pcc_py_libpython.a",
    )
)
_PY_LIBPYTHON_MODE_ENV = _pipeline_modes.PY_LIBPYTHON_MODE_ENV
_IR_SCAFFOLD_MODE_ENV = _pipeline_modes.IR_SCAFFOLD_MODE_ENV
_PYTHON_IR_PASSES_ENV = _pipeline_pass_config.PYTHON_IR_PASSES_ENV
_PYTHON_IR_PASS_TRANSPORT_ENV = (
    _pipeline_pass_config.PYTHON_IR_PASS_TRANSPORT_ENV
)
_PYTHON_IR_PASS_JOBS_ENV = _pipeline_pass_config.PYTHON_IR_PASS_JOBS_ENV
_PYTHON_IR_PASS_TIMEOUT_ENV = _pipeline_pass_config.PYTHON_IR_PASS_TIMEOUT_ENV
_PYTHON_IR_PASS_STRICT_NO_LIBPYTHON_ENV = "PCC_PYTHON_IR_PASS_STRICT_NO_LIBPYTHON"
_PYTHON_IR_PASS_SPLIT_LARGE_MODULES_ENV = (
    _pipeline_pass_config.PYTHON_IR_PASS_SPLIT_LARGE_MODULES_ENV
)
_PYTHON_IR_PASS_SPLIT_THRESHOLD_BYTES_ENV = (
    _pipeline_pass_config.PYTHON_IR_PASS_SPLIT_THRESHOLD_BYTES_ENV
)
_PYTHON_IR_PASS_SPLIT_SHARD_BYTES_ENV = (
    _pipeline_pass_config.PYTHON_IR_PASS_SPLIT_SHARD_BYTES_ENV
)
_PYTHON_IR_PASS_SKIP_MODULE_PREFIXES_ENV = (
    _pipeline_pass_config.PYTHON_IR_PASS_SKIP_MODULE_PREFIXES_ENV
)
_PY_FRONTEND_JOBS_ENV = "PCC_PY_FRONTEND_JOBS"
_OUTER_PARALLELISM_ENV = _pipeline_pass_config.OUTER_PARALLELISM_ENV
_PY_FRONTEND_WORKER_TIMING_ENV = "PCC_PY_FRONTEND_WORKER_TIMING"
_PY_FRONTEND_WORKER_ARG = "--pcc-python-multi-codegen-worker"
_SELF_BACKEND_EMIT_WORKER_ARG = "--pcc-self-backend-emit-worker"
_SELF_BACKEND_EMIT_BATCH_WORKER_ARG = "--pcc-self-backend-emit-batch-worker"
_SELF_BACKEND_SPLIT_WORKER_ARG = "--pcc-self-backend-split-worker"
_SELF_BACKEND_EMIT_BATCH_MANIFEST_V1 = "pcc.self_backend.emit_batch.v1"
_SELF_BACKEND_DEFAULT_JOBS = _pipeline_self_backend_config.SELF_BACKEND_DEFAULT_JOBS
_SELF_BACKEND_EMIT_BATCH_MAX_ITEMS = 4
_PY_FRONTEND_WORKER_MANIFEST_V1 = "pcc.py_frontend.codegen_worker.v1"
_PY_FRONTEND_WORKER_MANIFEST_V2 = "pcc.py_frontend.codegen_worker.v2"
_PY_FRONTEND_WORKER_MANIFEST_V3 = "pcc.py_frontend.codegen_worker.v3"
_PY_FRONTEND_WORKER_MANIFEST_V4 = "pcc.py_frontend.codegen_worker.v4"
_PY_FRONTEND_AST_WIRE_ENV = "PCC_PY_FRONTEND_AST_WIRE"
_PY_RUNTIME_CC_ENV = "PCC_RUNTIME_CC"
_PY_RUNTIME_HIGH_ENV = "PCC_RUNTIME_HIGH"
_PY_RUNTIME_ARCHIVE_ENV = "PCC_RUNTIME_ARCHIVE"
_PY_RUNTIME_DIR_ENV = "PCC_RUNTIME_DIR"
_GPU_BACKEND_ENV = _pipeline_modes.GPU_BACKEND_ENV
_DEFAULT_GPU_BACKEND = _pipeline_modes.DEFAULT_GPU_BACKEND
_KNOWN_GPU_BACKENDS = _pipeline_modes.KNOWN_GPU_BACKENDS
_SELF_BACKEND_JOBS_ENV = _pipeline_self_backend_config.SELF_BACKEND_JOBS_ENV
_SELF_BACKEND_SKIP_LL_TEMP_ENV = (
    _pipeline_self_backend_config.SELF_BACKEND_SKIP_LL_TEMP_ENV
)
_SELF_BACKEND_SPLIT_LARGE_MODULES_ENV = (
    _pipeline_self_backend_config.SELF_BACKEND_SPLIT_LARGE_MODULES_ENV
)
_SELF_BACKEND_SPLIT_THRESHOLD_BYTES_ENV = (
    _pipeline_self_backend_config.SELF_BACKEND_SPLIT_THRESHOLD_BYTES_ENV
)
_SELF_BACKEND_SPLIT_SHARD_BYTES_ENV = (
    _pipeline_self_backend_config.SELF_BACKEND_SPLIT_SHARD_BYTES_ENV
)
_SELF_BACKEND_PUBLISH_SYNC_ENV = (
    _pipeline_modes.SELF_BACKEND_PUBLISH_SYNC_ENV
)
_SELF_BACKEND_OBJECT_CACHE_ENV = _pipeline_self_backend_cache.OBJECT_CACHE_ENV
_SELF_BACKEND_OBJECT_CACHE_DIR_ENV = _pipeline_self_backend_cache.OBJECT_CACHE_DIR_ENV
_SELF_BACKEND_OBJECT_CACHE_IDENTITY_ENV = (
    _pipeline_self_backend_cache.OBJECT_CACHE_IDENTITY_ENV
)
_SELF_BACKEND_OBJECT_CACHE_VERSION = _pipeline_self_backend_cache.OBJECT_CACHE_VERSION
_COMPILE_TIME_ONLY_IMPORT_FROMS = (
    _pipeline_import_policy.COMPILE_TIME_ONLY_IMPORT_FROMS
)
_COMPILE_TIME_ONLY_IMPORT_MODULES = (
    _pipeline_import_policy.COMPILE_TIME_ONLY_IMPORT_MODULES
)
_TEST_FACADE_IMPORT_MODULES = _pipeline_import_policy.TEST_FACADE_IMPORT_MODULES
_ANNOTATION_ONLY_IMPORT_MODULES = (
    _pipeline_import_policy.ANNOTATION_ONLY_IMPORT_MODULES
)
_NATIVE_BUILTIN_IMPORTS = _pipeline_import_policy.NATIVE_BUILTIN_IMPORTS
_NATIVE_BUILTIN_IMPORTS_WITH_COMPILED_PROVIDER = (
    _pipeline_import_policy.NATIVE_BUILTIN_IMPORTS_WITH_COMPILED_PROVIDER
)
_NATIVE_IMPORT_FROMS = _pipeline_import_policy.NATIVE_IMPORT_FROMS
_SCAFFOLD_IMPORT_MODULES = _pipeline_import_policy.SCAFFOLD_IMPORT_MODULES
_PYTHON_IR_PASS_FAST_PRESET = _pipeline_pass_config.PYTHON_IR_PASS_FAST_PRESET
_PYTHON_IR_PASS_DEFAULT_TIER = (
    _pipeline_pass_config.PYTHON_IR_PASS_DEFAULT_TIER
)
_PYTHON_IR_PASS_DEFAULT_TIER_SCHEMA = (
    _pipeline_pass_config.PYTHON_IR_PASS_DEFAULT_TIER_SCHEMA
)
_PYTHON_IR_PASS_UNSAFE_MODULES = (
    _pipeline_pass_config.PYTHON_IR_PASS_UNSAFE_MODULES
)
_PYTHON_IR_PASS_UNSAFE_MODULE_PREFIXES = (
    _pipeline_pass_config.PYTHON_IR_PASS_UNSAFE_MODULE_PREFIXES
)
_PYTHON_IR_PASS_PRESETS = _pipeline_pass_config.PYTHON_IR_PASS_PRESETS


_SELF_BACKEND_HOST_CODE = _pipeline_self_backend_host._SELF_BACKEND_HOST_CODE
_COMPILER_CACHE_RETENTION_HOST_CODE = (
    _pipeline_self_backend_host._COMPILER_CACHE_RETENTION_HOST_CODE
)
_SELF_BACKEND_HOST_MANY_CODE = (
    _pipeline_self_backend_host._SELF_BACKEND_HOST_MANY_CODE
)
_SELF_BACKEND_OBJECT_CACHE_PLAN_CODE = (
    _pipeline_self_backend_host._SELF_BACKEND_OBJECT_CACHE_PLAN_CODE
)
_SELF_BACKEND_OBJECT_CACHE_PUBLISH_CODE = (
    _pipeline_self_backend_host._SELF_BACKEND_OBJECT_CACHE_PUBLISH_CODE
)


def _log(verbose: bool, msg: str) -> None:
    if verbose:
        sys.stderr.write("[pcc.py] " + msg + "\n")


_NATIVE_EXTENSION_SUFFIXES = _pipeline_packages.NATIVE_EXTENSION_SUFFIXES
_validate_package_site_no_libpython_abi = _pipeline_dependency_closure._validate_package_site_no_libpython_abi
_top_level_import_targets = _pipeline_dependency_closure._top_level_import_targets

_source_module_scope_lines = _pipeline_import_scan._source_module_scope_lines
_iter_source_import_specs = _pipeline_import_scan._iter_source_import_specs
_iter_source_importlib_literal_specs = (
    _pipeline_import_scan._iter_source_importlib_literal_specs
)
_iter_source_importlib_resource_literal_specs = (
    _pipeline_import_scan._iter_source_importlib_resource_literal_specs
)
_append_source_import_from_spec = (
    _pipeline_import_scan._append_source_import_from_spec
)
_iter_source_import_from_specs = (
    _pipeline_import_scan._iter_source_import_from_specs
)
_without_attribute_error_handler_imports = (
    _pipeline_import_scan._without_attribute_error_handler_imports
)
_source_after_unescaped_delimiter = (
    _pipeline_import_scan._source_after_unescaped_delimiter
)
_source_import_discovery_line = (
    _pipeline_import_scan._source_import_discovery_line
)
_source_import_discovery_text = (
    _pipeline_import_scan._source_import_discovery_text
)
_without_type_checking_imports = (
    _pipeline_import_scan._without_type_checking_imports
)


_source_declares_freestanding_module = (
    _pipeline_freestanding.source_declares_freestanding_module
)
_source_declares_runtime_port_module = (
    _pipeline_freestanding.source_declares_runtime_port_module
)
_freestanding_allowed_external_symbols = (
    _pipeline_freestanding.freestanding_allowed_external_symbols
)
_source_call_arguments = _pipeline_freestanding.source_call_arguments
_freestanding_module_scope_extern_bindings = (
    _pipeline_freestanding.freestanding_module_scope_extern_bindings
)
_freestanding_readonly_gc_runtime_imports = (
    _pipeline_freestanding.freestanding_readonly_gc_runtime_imports
)
_freestanding_gc_cross_object_runtime_imports = (
    _pipeline_freestanding.freestanding_gc_cross_object_runtime_imports
)
_freestanding_gc_runtime_global_imports = (
    _pipeline_freestanding.freestanding_gc_runtime_global_imports
)
_validate_freestanding_ir = _pipeline_freestanding.validate_freestanding_ir




_package_import_targets = _pipeline_dependency_closure._package_import_targets
_collect_relative_module_closure = _pipeline_dependency_closure._collect_relative_module_closure
_collect_multi_source_relative_closure = _pipeline_dependency_closure._collect_multi_source_relative_closure
_filter_ir_scaffold_closure = _pipeline_dependency_closure._filter_ir_scaffold_closure
_prepare_multi_source_compile_closure = (
    _pipeline_dependency_closure._prepare_multi_source_compile_closure
)


_host_find_spec_origin = _pipeline_dependency_closure._host_find_spec_origin
_host_sysconfig_roots = _pipeline_dependency_closure._host_sysconfig_roots
_host_stdlib_roots = _pipeline_dependency_closure._host_stdlib_roots
_host_site_roots = _pipeline_dependency_closure._host_site_roots
_append_unique_path = _pipeline_dependency_closure._append_unique_path
_path_is_under = _pipeline_dependency_closure._path_is_under
_path_is_under_any = _pipeline_dependency_closure._path_is_under_any
_host_origin_is_stdlib_py = _pipeline_dependency_closure._host_origin_is_stdlib_py
_append_pcc_package_dir_candidate = _pipeline_dependency_closure._append_pcc_package_dir_candidate
_append_pcc_package_dir_ancestors = _pipeline_dependency_closure._append_pcc_package_dir_ancestors
_pcc_package_dir_has_native_stdlib = _pipeline_dependency_closure._pcc_package_dir_has_native_stdlib
_pcc_package_dir_candidates = _pipeline_dependency_closure._pcc_package_dir_candidates
_locate_native_stdlib_module_source = _pipeline_dependency_closure._locate_native_stdlib_module_source
_native_stdlib_parent_package_sources = _pipeline_dependency_closure._native_stdlib_parent_package_sources
_locate_stdlib_module_source = _pipeline_dependency_closure._locate_stdlib_module_source
_native_stdlib_root_for_path = _pipeline_dependency_closure._native_stdlib_root_for_path
_pcc_log_channel_enabled = _pipeline_dependency_closure._pcc_log_channel_enabled
_pcc_json_escape = _pipeline_dependency_closure._pcc_json_escape
_pcc_import_log_line = _pipeline_dependency_closure._pcc_import_log_line
_pcc_emit_import_log = _pipeline_dependency_closure._pcc_emit_import_log
_record_import_classification = _pipeline_dependency_closure._record_import_classification
_classify_python_import = _pipeline_dependency_closure._classify_python_import
_source_uses_native_stdlib = _pipeline_dependency_closure._source_uses_native_stdlib
_sources_use_native_stdlib = _pipeline_dependency_closure._sources_use_native_stdlib
_source_absolute_imports_for_discovery = _pipeline_dependency_closure._source_absolute_imports_for_discovery
_source_pcc_native_extension_paths = _pipeline_dependency_closure._source_pcc_native_extension_paths
_source_imports_pcc_native_extension = _pipeline_dependency_closure._source_imports_pcc_native_extension
_is_ascii_module_candidate = _pipeline_dependency_closure._is_ascii_module_candidate
_native_extension_literal_module_candidates = _pipeline_dependency_closure._native_extension_literal_module_candidates
_expand_native_extension_module_object_ports = _pipeline_dependency_closure._expand_native_extension_module_object_ports
_stdlib_absolute_imports_in = _pipeline_dependency_closure._stdlib_absolute_imports_in
_stdlib_module_compiles = _pipeline_dependency_closure._stdlib_module_compiles
_expand_recursive_stdlib = _pipeline_dependency_closure._expand_recursive_stdlib
_expand_required_native_builtin_providers = _pipeline_dependency_closure._expand_required_native_builtin_providers
_relative_import_targets = _pipeline_dependency_closure._relative_import_targets
_order_module_init_deps_for = _pipeline_dependency_closure._order_module_init_deps_for
_order_module_inits = _pipeline_dependency_closure._order_module_inits

_EXPORT_DEFAULT_WIRE_KEY = _pipeline_exports._EXPORT_DEFAULT_WIRE_KEY
_export_param_types = _pipeline_exports._export_param_types
_export_return_type = _pipeline_exports._export_return_type
_export_returns_none = _pipeline_exports._export_returns_none
_export_typed_int_unboxed_abi_mode = _pipeline_exports._export_typed_int_unboxed_abi_mode
_export_typed_int_unboxed_abi_enabled = _pipeline_exports._export_typed_int_unboxed_abi_enabled
_export_int_literal_fits_i64 = _pipeline_exports._export_int_literal_fits_i64
_export_literal_value_or_none = _pipeline_exports._export_literal_value_or_none
_closed_world_node_kind = _pipeline_exports._closed_world_node_kind
_closed_world_expected_kind = _pipeline_exports._closed_world_expected_kind
_closed_world_is_node = _pipeline_exports._closed_world_is_node
_export_default_is_native_typed_int_shape = _pipeline_exports._export_default_is_native_typed_int_shape
_export_func_uses_unboxed_typed_int_abi = _pipeline_exports._export_func_uses_unboxed_typed_int_abi
_export_static_literal_type = _pipeline_exports._export_static_literal_type
_export_signed_int_literal_or_none = (
    _pipeline_exports._export_signed_int_literal_or_none
)
_export_static_all_names = _pipeline_exports._export_static_all_names
_export_common_static_type = _pipeline_exports._export_common_static_type
_decorator_name = _pipeline_exports._decorator_name
_split_top_level_type_args = _pipeline_exports._split_top_level_type_args
_normalise_export_annotation_text = _pipeline_exports._normalise_export_annotation_text
_normalise_export_annotation = _pipeline_exports._normalise_export_annotation
_export_annotation_or_none = _pipeline_exports._export_annotation_or_none
_export_return_ty_or_none = _pipeline_exports._export_return_ty_or_none
_class_is_dataclass = _pipeline_exports._class_is_dataclass
_export_default_native_func_ref = _pipeline_exports._export_default_native_func_ref
_export_default_native_global_ref = _pipeline_exports._export_default_native_global_ref
_export_call_sig = _pipeline_exports._export_call_sig
_export_default_to_wire = _pipeline_exports._export_default_to_wire
_export_default_wire_is_safe = _pipeline_exports._export_default_wire_is_safe
_export_default_from_wire = _pipeline_exports._export_default_from_wire
_native_export_arg_to_wire = _pipeline_exports._native_export_arg_to_wire
_native_export_to_wire = _pipeline_exports._native_export_to_wire
_native_export_from_wire = _pipeline_exports._native_export_from_wire
_write_native_exports_wire = _pipeline_exports._write_native_exports_wire
_read_native_exports_wire = _pipeline_exports._read_native_exports_wire
_read_native_exports_wire_for_module = (
    _pipeline_exports._read_native_exports_wire_for_module
)
_read_native_exports_wire_raw_modules = (
    _pipeline_exports._read_native_exports_wire_raw_modules
)
_export_method_symbol = _pipeline_exports._export_method_symbol


_resolve_ast_import_from_module = _pipeline_closed_world._resolve_ast_import_from_module
_closed_world_star_export_items = _pipeline_closed_world._closed_world_star_export_items
_closed_world_module_block_assign_targets = _pipeline_closed_world._closed_world_module_block_assign_targets
_closed_world_dyn_module_global_export = _pipeline_closed_world._closed_world_dyn_module_global_export
_merge_closed_world_reexports = _pipeline_closed_world._merge_closed_world_reexports
_closed_world_reexport_edges = _pipeline_closed_world._closed_world_reexport_edges
_closed_world_module_dependencies = (
    _pipeline_closed_world._closed_world_module_dependencies
)
_merge_closed_world_reexport_edges = _pipeline_closed_world._merge_closed_world_reexport_edges
_flatten_closed_world_class_export_fields = _pipeline_closed_world._flatten_closed_world_class_export_fields
_repair_closed_world_default_global_owners = _pipeline_closed_world._repair_closed_world_default_global_owners
_mark_closed_world_function_object_exports = _pipeline_closed_world._mark_closed_world_function_object_exports
_apply_closed_world_function_object_uses = _pipeline_closed_world._apply_closed_world_function_object_uses
_closed_world_function_object_exports = _pipeline_closed_world._closed_world_function_object_exports
_write_reexport_edges_wire = _pipeline_closed_world._write_reexport_edges_wire
_read_reexport_edges_wire = _pipeline_closed_world._read_reexport_edges_wire
_closed_world_shallow_func_body = _pipeline_closed_world._closed_world_shallow_func_body
_closed_world_shallow_func = _pipeline_closed_world._closed_world_shallow_func
_closed_world_shallow_lift_module = _pipeline_closed_world._closed_world_shallow_lift_module
_closed_world_is_identity_decorator = _pipeline_closed_world._closed_world_is_identity_decorator


_build_context_owner = _pipeline_context
build_closed_world_context = _build_context_owner.build_closed_world_context
_annotate_closed_world_vthread_effects = (
    _build_context_owner.annotate_closed_world_vthread_effects
)
_annotate_closed_world_vthread_effect_summaries = (
    _build_context_owner.annotate_closed_world_vthread_effect_summaries
)
_build_closed_world_vthread_effect_summary = (
    _build_context_owner.build_closed_world_vthread_effect_summary
)
_closed_world_vthread_effect_export_surface = (
    _build_context_owner.closed_world_vthread_effect_export_surface
)
_write_closed_world_vthread_effect_summary = (
    _build_context_owner.write_closed_world_vthread_effect_summary
)
_read_closed_world_vthread_effect_summary = (
    _build_context_owner.read_closed_world_vthread_effect_summary
)
_closed_world_derived_class_map = (
    _build_context_owner._closed_world_derived_class_map
)
_merge_l1_mixin_stack_methods = (
    _build_context_owner._merge_l1_mixin_stack_methods
)
_merge_l1_codegen_methods = _build_context_owner._merge_l1_codegen_methods
_contextual_host_export_surface = (
    _build_context_owner._contextual_host_export_surface
)
_contextual_host_params_for_module = (
    _build_context_owner._contextual_host_params_for_module
)
count_py_cpy_fallback_calls = _build_context_owner.count_py_cpy_fallback_calls
_copy_native_module_exports = _build_context_owner._copy_native_module_exports
_module_uses_default_native_exports = (
    _build_context_owner._module_uses_default_native_exports
)
compile_contextual_per_module_fallback_counts = (
    _build_context_owner.compile_contextual_per_module_fallback_counts
)


def _runtime_archive_stale(archive: str) -> bool:
    return _pipeline_runtime_archive.archive_stale(
        archive,
        runtime_dir=_PY_RUNTIME_DIR,
        base_pcc_py_archive=_PY_RUNTIME_ARCHIVE_PCC_PY,
        c_bundle_valid=_runtime_archive_c_bundle_valid,
        target_matches=_runtime_archive_target_matches,
        archive_requires_provenance=_runtime_archive_requires_provenance,
        archive_provenance_valid=_runtime_archive_provenance_valid,
        archive_codegen_stale=_runtime_archive_codegen_stale,
        wheel_matches=_runtime_archive_wheel_stamp_matches,
        compiler_sources_newer=_runtime_archive_compiler_sources_newer_than,
        replaced_c_modules=_runtime_pcc_python_replaced_c_modules,
    )


def _runtime_makefile_variable_words(name: str) -> list[str]:
    return _pipeline_runtime_archive.makefile_variable_words(_PY_RUNTIME_DIR, name)


def _runtime_pcc_python_replaced_c_modules() -> set[str]:
    return _pipeline_runtime_archive.pcc_python_replaced_c_modules(_PY_RUNTIME_DIR)


def _runtime_archive_compiler_sources_newer_than(
    archive_base: str,
    archive_mtime: float,
) -> bool:
    return _pipeline_runtime_archive.compiler_sources_newer_than(
        _PCC_DIR,
        archive_base,
        archive_mtime,
    )


def _is_py_runtime_library_source(src_path: str) -> bool:
    return _pipeline_runtime_archive.is_library_source(_PY_RUNTIME_DIR, src_path)


def _runtime_archive_target_stamp(archive: str) -> str:
    return _pipeline_runtime_archive.target_stamp(archive)


def _runtime_archive_provenance_manifest(archive: str) -> str:
    return _pipeline_runtime_archive.provenance_manifest(archive)


def _runtime_archive_capi_inventory(archive: str) -> str:
    return _pipeline_runtime_archive.capi_inventory(archive)


def _runtime_archive_requires_provenance(archive: str) -> bool:
    return _pipeline_runtime_archive.requires_provenance(archive)


def _runtime_archive_requires_c_bundle_validation(archive: str) -> bool:
    return _pipeline_runtime_archive.requires_c_bundle_validation(archive)


def _runtime_archive_c_bundle_valid(archive: str) -> bool:
    return _pipeline_runtime_archive.c_bundle_valid(
        archive,
        host_python_command=_host_python_command,
    )


def _runtime_archive_provenance_root(archive: str) -> str:
    return _pipeline_runtime_archive.provenance_root(archive, _PY_RUNTIME_DIR)


def _runtime_archive_provenance_valid(
    archive: str,
    *,
    runtime_root: Optional[str] = None,
) -> bool:
    return _pipeline_runtime_archive.provenance_valid(
        archive,
        runtime_dir=_PY_RUNTIME_DIR,
        pcc_source_root=_pcc_source_root_for_host_subprocess,
        host_python_command=_host_python_command,
        runtime_root=runtime_root,
    )


def _runtime_archive_codegen_stale(archive: str) -> bool:
    return _pipeline_runtime_archive.provenance_codegen_stale(
        archive,
        pcc_source_root=_pcc_source_root_for_host_subprocess,
        host_python_command=_host_python_command,
    )


def _runtime_archive_target_matches(archive: str) -> bool:
    return _pipeline_runtime_archive.target_matches(
        archive,
        _runtime_archive_target_id(),
    )


def _runtime_archive_wheel_stamp_matches(archive: str) -> bool:
    return _pipeline_runtime_archive.wheel_stamp_matches(
        archive,
        _runtime_archive_target_id(),
    )


def _runtime_archive_target_id() -> str:
    return _pipeline_runtime_archive.target_id(
        _host_target_triple_for_self_backend()
    )


def _write_runtime_archive_target_stamp(archive: str) -> None:
    _pipeline_runtime_archive.write_target_stamp(
        archive,
        _runtime_archive_target_id(),
    )


def _run_runtime_make(make_cmd, *, verbose: bool) -> None:
    _pipeline_runtime_archive.run_runtime_make(
        _PY_RUNTIME_DIR,
        make_cmd,
        verbose=verbose,
    )


def _runtime_cc_mode() -> str:
    return _pipeline_runtime_archive.cc_mode(
        str(os.environ.get(_PY_RUNTIME_CC_ENV, "") or "")
    )


def _runtime_high_mode() -> str:
    return _pipeline_runtime_archive.high_mode(
        str(os.environ.get(_PY_RUNTIME_HIGH_ENV, "") or "")
    )


def _runtime_host_python_for_make() -> str:
    return _pipeline_runtime_archive.host_python_for_make(_host_python_command())


def _ensure_runtime(
    verbose: bool,
    *,
    needs_libpython: bool = False,
) -> str:
    try:
        return _pipeline_runtime_archive.ensure_runtime(
            verbose,
            needs_libpython=needs_libpython,
            runtime_dir_default=_PY_RUNTIME_DIR,
            archive_default=_PY_RUNTIME_ARCHIVE,
            archive_libpython=_PY_RUNTIME_ARCHIVE_LIBPYTHON,
            archive_pcc=_PY_RUNTIME_ARCHIVE_PCC,
            archive_pcc_py=_PY_RUNTIME_ARCHIVE_PCC_PY,
            archive_pcc_py_libpython=_PY_RUNTIME_ARCHIVE_PCC_PY_LIBPYTHON,
            archive_stale_check=_runtime_archive_stale,
            selected_cc_mode=_runtime_cc_mode,
            selected_high_mode=_runtime_high_mode,
            c_bundle_valid=_runtime_archive_c_bundle_valid,
            archive_requires_provenance=_runtime_archive_requires_provenance,
            archive_provenance_valid=_runtime_archive_provenance_valid,
            archive_codegen_stale=_runtime_archive_codegen_stale,
            wheel_matches=_runtime_archive_wheel_stamp_matches,
            archive_manifest=_runtime_archive_provenance_manifest,
            archive_target_matches=_runtime_archive_target_matches,
            compiler_sources_newer=_runtime_archive_compiler_sources_newer_than,
            resolve_pcc_binary=_resolve_pcc_binary,
            runtime_host_python=_runtime_host_python_for_make,
            run_make=_run_runtime_make,
            write_archive_target_stamp=_write_runtime_archive_target_stamp,
            logger=_log,
        )
    except _pipeline_runtime_archive.RuntimeArchiveError as exc:
        raise PyPipelineError(str(exc) or type(exc).__name__) from exc


def _ensure_runtime_without_direct_indexed_env(verbose: bool) -> str:
    """Build/load the runtime without leaking frontend-worker direct mode."""
    direct_names = (
        "PCC_DIRECT_INDEXED_KERNEL_CAPTURE",
        "PCC_DIRECT_INDEXED_KERNEL_EMIT",
        "PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK",
        "PCC_DIRECT_INDEXED_KERNEL_FUSE_USES",
        "PCC_DIRECT_INDEXED_KERNEL_RELEASE_FRONTEND",
        "PCC_DIRECT_INDEXED_KERNEL_VALIDATE",
        "PCC_TEXT_INDEXED_KERNEL_EMIT",
    )
    saved = {}
    for name in direct_names:
        if name in os.environ:
            saved[name] = os.environ.pop(name)
    try:
        return _ensure_runtime(verbose, needs_libpython=False)
    finally:
        for name, value in saved.items():
            os.environ[name] = value


def _resolve_pcc_binary() -> Optional[str]:
    """Locate the pcc CLI binary for PCC_RUNTIME_CC=pcc builds."""
    env_override = str(os.environ.get("PCC_BINARY", "") or "").strip()
    if env_override:
        return env_override
    # When a compiled pcc binary is building a nested runtime archive,
    # sys.executable still describes the host Python used during stage0,
    # while sys.argv[0] points at the stage binary that must self-compile
    # the runtime modules. Prefer it when it is an executable path rather
    # than a Python source entry point.
    argv0 = str(sys.argv[0] if len(sys.argv) > 0 else "" or "").strip()
    argv0_base = os.path.basename(argv0)
    if argv0 and argv0_base.startswith("pcc") and not argv0.endswith(".py"):
        argv0_path = os.path.abspath(argv0)
        if os.path.isfile(argv0_path) and os.access(argv0_path, os.X_OK):
            return argv0_path
    # Prefer the pcc installed alongside the running Python (uv/venv).
    candidate = os.path.join(
        os.path.dirname(sys.executable),
        "pcc",
    )
    if os.path.isfile(candidate):
        return candidate
    found = shutil.which("pcc")
    return found


def _emit_ll(ir_text, out_ll_path, verbose: bool) -> None:
    ir_text = str(ir_text)
    out_ll_path = str(out_ll_path)
    if verbose:
        _log(
            verbose,
            "writing LLVM IR to " + out_ll_path + " (" + str(len(ir_text)) + " bytes)",
        )
    with open(out_ll_path, "w") as f:
        f.write(ir_text)


_split_large_modules_for_python_ir_passes = (
    _pipeline_pass_driver.split_large_modules_for_passes
)


def _apply_python_ir_pass_pipeline(
    ir_text: str,
    *,
    module_name: str,
    verbose: bool = False,
    default_raw: Optional[str] = None,
    strict_no_libpython: bool = False,
) -> str:
    if debug_info_requested():
        # A debug build is an unoptimized build.  Every transport in this
        # pipeline rewrites instruction lines and drops their ``!dbg`` suffix,
        # which would leave the line table pointing at instructions that no
        # longer exist -- worse than no line table, because it looks right.
        # Carrying locations through the transforms belongs with the real pass
        # infrastructure; until then `-g` means `-O0`, as it does elsewhere.
        return ir_text
    try:
        return _pipeline_pass_driver.apply_passes(
            ir_text,
            module_name=module_name,
            host_python_command=_host_python_command,
            pcc_source_root=_pcc_source_root_for_host_subprocess,
            logger=lambda message: _log(verbose, message),
            verbose=verbose,
            default_raw=default_raw,
            strict_no_libpython=strict_no_libpython,
        )
    except _pipeline_pass_driver.PassDriverError as exc:
        raise PyPipelineError(str(exc) or type(exc).__name__) from exc


def _apply_python_ir_pass_pipeline_many(
    module_ir_texts: list[tuple[str, str]],
    *,
    verbose: bool = False,
    default_raw: Optional[str] = None,
    strict_no_libpython: bool = False,
) -> list[tuple[str, str]]:
    try:
        return _pipeline_pass_driver.apply_passes_many(
            module_ir_texts,
            apply_one=_apply_python_ir_pass_pipeline,
            host_python_command=_host_python_command,
            pcc_source_root=_pcc_source_root_for_host_subprocess,
            logger=lambda message: _log(verbose, message),
            verbose=verbose,
            default_raw=default_raw,
            strict_no_libpython=strict_no_libpython,
        )
    except _pipeline_pass_driver.PassDriverError as exc:
        raise PyPipelineError(str(exc) or type(exc).__name__) from exc


_default_python_ir_pass_raw_for_backend = (
    _pipeline_pass_driver.default_raw_for_backend
)
_default_python_ir_pass_raw_for_request = (
    _pipeline_pass_driver.default_raw_for_request
)


_native_extension_export_link_flags = (
    _pipeline_native_link.native_extension_export_link_flags
)


def _capi_export_anchor_symbols(runtime_archive: str) -> list[str]:
    try:
        return _pipeline_native_link.capi_export_anchor_symbols(
            runtime_archive,
            archive_requires_provenance=_runtime_archive_requires_provenance,
            archive_bundle_valid=lambda archive: (
                _runtime_archive_wheel_stamp_matches(archive)
                or _runtime_archive_provenance_valid(archive)
            ),
            host_python_command=_host_python_command,
        )
    except _pipeline_native_link.NativeLinkError as exc:
        raise PyPipelineError(str(exc) or type(exc).__name__) from exc


def _libpython_capi_isolation_link_flags(
    runtime_archive: Optional[str],
    needs_libpython: bool,
) -> list[str]:
    try:
        return _pipeline_native_link.libpython_capi_isolation_link_flags(
            runtime_archive,
            needs_libpython,
            anchor_symbols=_capi_export_anchor_symbols,
        )
    except _pipeline_native_link.NativeLinkError as exc:
        raise PyPipelineError(str(exc) or type(exc).__name__) from exc


def _runtime_archive_link_args_for_native_extensions(
    runtime_archive: str,
    needs_native_extension_exports: bool = False,
) -> list[str]:
    try:
        return _pipeline_native_link.runtime_archive_link_args_for_native_extensions(
            runtime_archive,
            needs_native_extension_exports,
            anchor_symbols=_capi_export_anchor_symbols,
        )
    except _pipeline_native_link.NativeLinkError as exc:
        raise PyPipelineError(str(exc) or type(exc).__name__) from exc


def _link_with_clang(
    ll_paths,
    out_path: str,
    runtime_archive: Optional[str],
    verbose: bool,
    *,
    needs_libpython: bool = False,
    needs_native_extension_exports: bool = False,
    extra_link_inputs: tuple[str, ...] = (),
    extra_link_args: tuple[str, ...] = (),
) -> None:
    try:
        _pipeline_native_link.link_with_clang(
            ll_paths,
            out_path,
            runtime_archive,
            verbose,
            needs_libpython=needs_libpython,
            needs_native_extension_exports=needs_native_extension_exports,
            extra_link_inputs=extra_link_inputs,
            extra_link_args=extra_link_args,
            input_target_triple=_link_input_target_triple,
            normalize_target_triple=_normalize_clang_target_triple,
            clang_target_triple=_clang_target_triple_for_link,
            export_link_flags=_native_extension_export_link_flags,
            runtime_link_args=_runtime_archive_link_args_for_native_extensions,
            isolation_link_flags=_libpython_capi_isolation_link_flags,
            libpython_link_flags=_libpython_link_flags,
            logger=_log,
        )
    except _pipeline_native_link.NativeLinkError as exc:
        raise PyPipelineError(str(exc) or type(exc).__name__) from exc
def _ensure_llvm_module_target(path: str, triple: str) -> str:
    return _pipeline_targets.ensure_module_target(path, triple)


def _write_utf8_text_file(path: str, text: str) -> None:
    _pipeline_targets.write_utf8_text_file(path, text)


def _normalize_clang_target_triple(triple: str) -> str:
    return _pipeline_targets.normalize_clang_target_triple(triple)


def _link_input_target_triple(ll_paths: list[str]) -> Optional[str]:
    return _pipeline_targets.link_input_target_triple(ll_paths)


def _clang_target_triple_for_link(ll_paths: list[str]) -> Optional[str]:
    triple = _pipeline_targets.link_input_target_triple(ll_paths)
    if triple not in (None, "unknown-unknown-unknown"):
        return _pipeline_targets.normalize_clang_target_triple(triple)
    return _pipeline_targets.clang_target_triple(
        [],
        host_target_triple=_host_target_triple_for_self_backend(),
    )


_host_target_triple_for_self_backend = _pipeline_targets.host_target_triple


def _self_backend_ir_text(ir_text: str) -> str:
    # A concrete module target is already authoritative.  Resolve the host
    # target only for a missing/placeholder directive: besides avoiding an
    # unnecessary compiler subprocess, this keeps the internal-assembly worker
    # genuinely independent of ``cc``.
    text = str(ir_text)
    header = text[:4096]
    placeholder = 'target triple = "unknown-unknown-unknown"'
    if placeholder not in header and 'target triple = "' in header:
        return text
    return _pipeline_targets.self_backend_ir_text(
        text,
        host_target_triple=_host_target_triple_for_self_backend(),
    )


def _ir_text_with_target_triple(ir_text: str, target_triple: Optional[str]) -> str:
    """Apply an explicit CLI/API target to frontend-emitted Python IR."""
    try:
        return _pipeline_targets.ir_text_with_target_triple(
            ir_text,
            target_triple,
        )
    except _pipeline_targets.PipelineTargetError as exc:
        raise PyPipelineError(str(exc) or type(exc).__name__) from exc


def _host_python_command() -> str:
    return _pipeline_paths.host_python_command(
        _pcc_source_root_for_host_subprocess(),
        os.getcwd(),
    )


def _pcc_source_root_for_host_subprocess() -> str:
    return _pipeline_paths.pcc_source_root_for_host_subprocess(_PCC_DIR)


_debug_dump_self_backend_ir_texts = _pipeline_self_backend_emit.debug_dump_ir_texts
def _emit_self_asm_via_host_python(
    ir_text: str,
    tmp_dir: str,
    index: int,
) -> tuple[str, str]:
    try:
        return _pipeline_self_backend_emit.emit_via_host_python(
            ir_text,
            tmp_dir,
            index,
            emit_native=_emit_self_asm_in_process,
            normalize_ir=_self_backend_ir_text,
            host_python_command=_host_python_command,
            host_code=_SELF_BACKEND_HOST_CODE,
            pcc_source_root=_pcc_source_root_for_host_subprocess,
        )
    except _pipeline_self_backend_emit.SelfBackendEmitError as exc:
        raise PyPipelineError(str(exc) or type(exc).__name__) from exc
def _emit_self_asm_in_process(
    ir_text: str,
) -> Optional[tuple[str, str]]:
    return _pipeline_self_backend_emit.emit_in_process(
        ir_text,
        parse_target_triple=_parse_self_backend_target_triple_native,
        host_target_triple=_host_target_triple_for_self_backend,
        target_supported=_is_aarch64_darwin_triple_native,
        emit_asm=_emit_aarch64_darwin_asm_native,
    )
def _maintain_self_backend_object_cache(
    protected_paths: list[str],
) -> None:
    _pipeline_self_backend_cache.maintain(
        protected_paths,
        host_python_command=_host_python_command,
        pcc_source_root=_pcc_source_root_for_host_subprocess,
        retention_host_code=_COMPILER_CACHE_RETENTION_HOST_CODE,
    )
_self_backend_object_cache_path_allowed = _pipeline_self_backend_cache.path_allowed
def _plan_self_backend_object_cache(
    worker_items: list[tuple[str, str, str]],
    target_id: str,
    cc: str,
    tmp_dir: str,
) -> list[tuple[str, str]]:
    return _pipeline_self_backend_cache.plan(
        worker_items,
        target_id,
        cc,
        tmp_dir,
        host_python_command=_host_python_command,
        plan_host_code=_SELF_BACKEND_OBJECT_CACHE_PLAN_CODE,
        small_int_decimal=_small_int_decimal,
    )
def _publish_self_backend_object_cache(
    worker_items: list[tuple[str, str, str]],
    cache_plan: list[tuple[str, str]],
    tmp_dir: str,
) -> bool:
    return _pipeline_self_backend_cache.publish(
        worker_items,
        cache_plan,
        tmp_dir,
        host_python_command=_host_python_command,
        publish_host_code=_SELF_BACKEND_OBJECT_CACHE_PUBLISH_CODE,
    )
def run_self_backend_emit_worker(
    ir_path: str,
    result_path: str,
    obj_path: str = "",
    cc: str = "",
) -> int:
    return _pipeline_self_backend_emit.run_emit_worker(
        ir_path,
        result_path,
        obj_path,
        cc,
        normalize_ir=_self_backend_ir_text,
        emit_native=_emit_self_asm_in_process,
    )


def run_self_backend_indexed_emit_worker(
    sidecar_path: str,
    output_path: str,
    artifact_kind: str,
) -> int:
    try:
        _emit_indexed_module_file(sidecar_path, output_path, artifact_kind)
        return 0
    except Exception as exc:
        sys.stderr.write(
            "self backend indexed emit worker failed: "
            + (str(exc) or type(exc).__name__)
            + "\n"
        )
        return 1


def run_self_backend_emit_batch_worker(manifest_path: str) -> int:
    return _pipeline_self_backend_emit.run_emit_batch_worker(
        manifest_path,
        manifest_version=_SELF_BACKEND_EMIT_BATCH_MANIFEST_V1,
        emit_worker=run_self_backend_emit_worker,
    )
def _run_self_backend_emit_worker_pool(
    worker_command_prefix: list[str],
    worker_items: list[tuple[str, str, str]],
    cc: str,
    tmp_dir: str,
    batch_label: str,
    max_parallel: int,
    fresh_process_per_item: bool,
    item_bytes=None,
    admission_byte_cap: int = 0,
) -> int:
    try:
        return _pipeline_self_backend_emit.run_emit_worker_pool(
            worker_command_prefix,
            worker_items,
            cc,
            tmp_dir,
            batch_label,
            max_parallel,
            item_bytes=item_bytes,
            admission_byte_cap=admission_byte_cap,
            batch_max_items=(
                1
                if fresh_process_per_item
                else _SELF_BACKEND_EMIT_BATCH_MAX_ITEMS
            ),
            manifest_version=_SELF_BACKEND_EMIT_BATCH_MANIFEST_V1,
            worker_arg=_SELF_BACKEND_EMIT_BATCH_WORKER_ARG,
            small_int_decimal=_small_int_decimal,
            shell_quote_arg=_shell_quote_arg,
            run_worker_commands=_run_python_frontend_worker_commands,
        )
    except _pipeline_self_backend_emit.SelfBackendEmitError as exc:
        raise PyPipelineError(str(exc) or type(exc).__name__) from exc


def run_self_backend_split_worker(
    ir_path: str,
    result_path: str,
    output_prefix: str,
    export_prefix: str,
    shard_bytes_text: str,
) -> int:
    return _pipeline_self_backend_emit.run_split_worker(
        ir_path,
        result_path,
        output_prefix,
        export_prefix,
        shard_bytes_text,
        split_ir_module=_split_self_backend_ir_module_for_object_shards,
        small_int_decimal=_small_int_decimal,
    )


def _emit_self_objects_many_in_process(
    ir_texts: list[str],
    tmp_dir: str,
    cc: str,
    *,
    split_large_modules: bool,
    profile: Optional[dict],
    internal_link: bool = False,
) -> Optional[list[tuple[str, str]]]:
    try:
        return _pipeline_self_backend_emit.emit_objects_many_in_process(
            ir_texts,
            tmp_dir,
            cc,
            split_large_modules=split_large_modules,
            profile=profile,
            internal_link=internal_link,
            parse_target_triple=_parse_self_backend_target_triple_native,
            host_target_triple=_host_target_triple_for_self_backend,
            target_supported=_is_aarch64_darwin_triple_native,
            native_worker_executable=_python_frontend_worker_executable,
            split_large_ir_modules=_split_self_backend_large_ir_modules,
            source_workers_worthwhile=_source_self_backend_emit_workers_worthwhile,
            worker_command_prefix_for_frontend=_python_frontend_worker_command_prefix,
            split_threshold_bytes=_self_backend_split_threshold_bytes,
            split_shard_bytes=_self_backend_split_shard_bytes,
            jobs_for_ir_texts=_self_backend_jobs_for_ir_texts,
            profile_counter=_profile_counter,
            profiled_gc_collect=_profiled_gc_collect,
            profile_begin=_profile_begin,
            profile_end=_profile_end,
            run_worker_commands=_run_python_frontend_worker_commands,
            small_int_decimal=_small_int_decimal,
            shell_quote_arg=_shell_quote_arg,
            split_worker_arg=_SELF_BACKEND_SPLIT_WORKER_ARG,
            plan_cache=_plan_self_backend_object_cache,
            jobs=_self_backend_jobs,
            jobs_for_input_sizes=_self_backend_jobs_for_input_sizes,
            jobs_env=_SELF_BACKEND_JOBS_ENV,
            run_emit_worker_pool=_run_self_backend_emit_worker_pool,
            publish_cache=_publish_self_backend_object_cache,
            maintain_cache=_maintain_self_backend_object_cache,
            emit_in_process=_emit_self_asm_in_process,
            join_strings=_join_strings,
        )
    except _pipeline_self_backend_emit.SelfBackendEmitError as exc:
        raise PyPipelineError(str(exc) or type(exc).__name__) from exc


def _source_self_backend_emit_workers_worthwhile(ir_texts: list[str]) -> bool:
    if len(ir_texts) >= 4:
        return True
    total_bytes = 0
    for ir_text in ir_texts:
        total_bytes += len(ir_text)
    return total_bytes >= 1_000_000


def _emit_self_objects_many_via_host_python(
    ir_texts: list[str],
    tmp_dir: str,
    cc: str,
    *,
    split_large_modules: bool = False,
    profile: Optional[dict] = None,
    internal_link: bool = False,
) -> list[tuple[str, str]]:
    try:
        return _pipeline_self_backend_emit.emit_objects_many_via_host_python(
            ir_texts,
            tmp_dir,
            cc,
            split_large_modules=split_large_modules,
            profile=profile,
            internal_link=internal_link,
            emit_in_process_many=_emit_self_objects_many_in_process,
            profile_begin=_profile_begin,
            profile_end=_profile_end,
            split_threshold_bytes=_self_backend_split_threshold_bytes,
            jobs_for_count=_self_backend_jobs,
            host_python_command=_host_python_command,
            host_many_code=_SELF_BACKEND_HOST_MANY_CODE,
            pcc_source_root=_pcc_source_root_for_host_subprocess,
            small_int_decimal=_small_int_decimal,
            profile_counter=_profile_counter,
        )
    except _pipeline_self_backend_emit.SelfBackendEmitError as exc:
        raise PyPipelineError(str(exc) or type(exc).__name__) from exc


_self_backend_jobs = _pipeline_self_backend_config.jobs
_self_backend_jobs_for_ir_texts = _pipeline_self_backend_config.jobs_for_ir_texts
_self_backend_jobs_for_input_sizes = (
    _pipeline_self_backend_config.jobs_for_input_sizes
)
_self_backend_skip_ll_temp = _pipeline_self_backend_config.skip_ll_temp
_self_backend_split_large_modules_enabled = (
    _pipeline_self_backend_config.split_large_modules_enabled
)
_self_backend_split_threshold_bytes = (
    _pipeline_self_backend_config.split_threshold_bytes
)
_self_backend_split_shard_bytes = _pipeline_self_backend_config.split_shard_bytes
_split_self_backend_large_ir_modules = (
    _pipeline_self_backend_config.split_large_ir_modules
)


_platform_link_flags = _pipeline_targets.platform_link_flags


def _libpython_link_flags() -> list[str]:
    try:
        return _pipeline_libpython.link_flags()
    except _pipeline_libpython.LibpythonLinkConfigError as exc:
        raise PyPipelineError(str(exc) or type(exc).__name__) from exc


def _append_libpython_link_flags(cmd: list[str]) -> None:
    cmd.extend(_libpython_link_flags())


def _link_with_self_backend(
    ll_paths,
    out_path: str,
    runtime_archive: Optional[str],
    verbose: bool,
    *,
    needs_libpython: bool = False,
    needs_native_extension_exports: bool = False,
    extra_link_inputs: tuple[str, ...] = (),
    extra_link_args: tuple[str, ...] = (),
    profile: Optional[dict] = None,
) -> None:
    _pipeline_self_backend_link.link_paths(
        ll_paths,
        out_path,
        runtime_archive,
        verbose,
        needs_libpython=needs_libpython,
        needs_native_extension_exports=needs_native_extension_exports,
        extra_link_inputs=extra_link_inputs,
        extra_link_args=extra_link_args,
        profile=profile,
        normalize_ir=_self_backend_ir_text,
        link_ir_texts=_link_with_self_backend_ir_texts,
        profile_begin=_profile_begin,
        profile_end=_profile_end,
    )


def _finish_self_backend_executable(
    tmp_out_path: str,
    out_path: str,
    profile,
    *,
    signature_owned_by_pcc: bool = False,
) -> None:
    _pipeline_self_backend_link.finish_executable(
        tmp_out_path,
        out_path,
        profile,
        signature_owned_by_pcc=signature_owned_by_pcc,
        profile_begin=_profile_begin,
        profile_end=_profile_end,
        publish_sync_enabled=_self_backend_publish_sync_enabled,
    )


def _link_self_backend_ir_texts_run(
    ir_texts: list[str],
    out_path: str,
    runtime_archive: Optional[str],
    verbose: bool,
    *,
    needs_libpython: bool,
    needs_native_extension_exports: bool = False,
    extra_link_inputs: tuple[str, ...] = (),
    extra_link_args: tuple[str, ...] = (),
    tmp: str,
    profile,
    consume_ir_texts: bool = False,
) -> None:
    try:
        _pipeline_self_backend_link.link_ir_texts_run(
            ir_texts,
            out_path,
            runtime_archive,
            verbose,
            needs_libpython=needs_libpython,
            needs_native_extension_exports=needs_native_extension_exports,
            extra_link_inputs=extra_link_inputs,
            extra_link_args=extra_link_args,
            tmp=tmp,
            profile=profile,
            consume_ir_texts=consume_ir_texts,
            resolve_self_link_mode=_resolve_self_link_mode,
            validate_pcc_self_link_surface=_validate_pcc_self_link_surface,
            profile_begin=_profile_begin,
            profile_end=_profile_end,
            debug_dump_ir_texts=_debug_dump_self_backend_ir_texts,
            split_large_modules_enabled=_self_backend_split_large_modules_enabled,
            split_threshold_bytes=_self_backend_split_threshold_bytes,
            emit_asm=_emit_self_asm_via_host_python,
            emit_objects=_emit_self_objects_many_via_host_python,
            runtime_archive_link_args=(
                _runtime_archive_link_args_for_native_extensions
            ),
            native_extension_export_flags=_native_extension_export_link_flags,
            libpython_isolation_flags=_libpython_capi_isolation_link_flags,
            platform_link_flags=_platform_link_flags,
            append_libpython_link_flags=_append_libpython_link_flags,
            log=_log,
            join_strings=_join_strings,
            run_self_link_command=_run_self_link_command,
            finish_self_backend_executable=_finish_self_backend_executable,
            semantic_layout_enabled=_macho_semantic_layout_enabled,
            write_semantic_layout_policy=_write_macho_semantic_layout_policy,
        )
    except _pipeline_self_backend_link.SelfBackendLinkError as exc:
        raise PyPipelineError(str(exc) or type(exc).__name__) from exc


def _repo_root_for_link() -> str:
    """Resolve the explicitly owned repo root before consulting ``__file__``."""
    explicit = str(os.environ.get("PCC_REPO_ROOT", "") or "").strip()
    if explicit:
        root = os.path.abspath(os.path.expanduser(explicit))
        if not os.path.isfile(os.path.join(root, "AGENTS.md")):
            raise PyPipelineError(
                "PCC_REPO_ROOT does not name a complete pcc source root"
            )
        return root
    cur = os.path.dirname(os.path.abspath(__file__))
    while True:
        if os.path.isfile(os.path.join(cur, "AGENTS.md")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            raise PyPipelineError("repository root not found above pipeline.py")
        cur = parent


def _default_self_link_mode() -> str:
    """Use pcc by default only on the accepted Darwin arm64 host."""
    machine = None
    if sys.platform == "darwin":
        try:
            machine = str(os.uname().machine or "")
        except (AttributeError, OSError) as exc:
            raise PyPipelineError(
                "cannot identify the Darwin host architecture for self-link "
                "selection; set PCC_SELF_LINK=cc or PCC_SELF_LINK=pcc explicitly"
            ) from exc
    try:
        return _pipeline_self_link.default_self_link_mode(sys.platform, machine)
    except _pipeline_self_link.SelfLinkContractError as exc:
        raise PyPipelineError(str(exc) or type(exc).__name__) from exc


def _resolve_self_link_mode() -> str:
    """Return the selected self-link implementation, rejecting typos.

    Darwin arm64 owns the accepted pcc default. Other hosts retain cc because
    this Mach-O route does not own their output format. Once the variable is
    present, only explicit ``cc`` (oracle/fallback) and ``pcc`` are accepted;
    arbitrary text must not turn a misspelled owner selection into cc output.
    """
    value = os.environ.get("PCC_SELF_LINK", "")
    default_mode = "cc"
    if not str(value or "").strip():
        default_mode = _default_self_link_mode()
    try:
        return _pipeline_self_link.normalize_self_link_mode(
            value,
            default_mode=default_mode,
        )
    except _pipeline_self_link.SelfLinkContractError as exc:
        raise PyPipelineError(str(exc) or type(exc).__name__) from exc


def _validate_pcc_self_link_surface(
    *,
    extra_link_args: tuple[str, ...] = (),
    needs_libpython: bool = False,
    needs_native_extension_exports: bool = False,
) -> None:
    """Validate the finite pcc-owned Mach-O linker surface before emission.

    The in-repo driver currently owns ordinary assembly/object inputs, extra
    object inputs, and one runtime archive.  It does not implement arbitrary
    driver/linker flags, libpython linkage, or the export-anchor/isolation
    semantics required by native extensions.  Reject those modes before even
    producing temporary assembly/objects; the subprocess seam remains the
    second fail-closed boundary for direct/internal callers.
    """
    try:
        _pipeline_self_link.validate_pcc_self_link_surface(
            _resolve_self_link_mode(),
            extra_link_args=extra_link_args,
            needs_libpython=needs_libpython,
            needs_native_extension_exports=needs_native_extension_exports,
        )
    except _pipeline_self_link.SelfLinkContractError as exc:
        raise PyPipelineError(str(exc) or type(exc).__name__) from exc


def _macho_semantic_layout_enabled() -> bool:
    try:
        return _pipeline_semantic_layout.semantic_layout_enabled(
            os.environ.get(_pipeline_semantic_layout.MODE_ENV, ""),
            platform=sys.platform,
            link_mode=_resolve_self_link_mode(),
        )
    except _pipeline_semantic_layout.FrontendSemanticLayoutError as exc:
        raise PyPipelineError(str(exc) or type(exc).__name__) from exc


def _write_macho_semantic_layout_policy(
    path: str, ir_texts: list[str]
) -> None:
    try:
        roots = _pipeline_semantic_layout.parse_root_names(
            os.environ.get(_pipeline_semantic_layout.ROOTS_ENV, "")
        )
        _pipeline_semantic_layout.write_frontend_semantic_layout_policy(
            path,
            ir_texts,
            root_names=roots,
            defined_function_name_from_line=_defined_function_name_from_line,
            global_name_from_definition_line=_global_name_from_definition_line,
            ir_global_definition_line=_self_backend_ir_global_definition_line,
            line_has_internal_linkage=_llvm_split_line_has_internal_linkage,
        )
    except _pipeline_semantic_layout.FrontendSemanticLayoutError as exc:
        raise PyPipelineError(str(exc) or type(exc).__name__) from exc


def _run_self_link_command(
    cmd,
    asm_path: Optional[str],
    tmp_out_path,
    runtime_archive,
    extra_link_inputs,
    verbose,
    *,
    extra_link_args: tuple[str, ...] = (),
    needs_libpython: bool = False,
    needs_native_extension_exports: bool = False,
    pcc_asm_inputs: tuple[str, ...] = (),
    pcc_native_object_inputs: tuple[str, ...] = (),
    pcc_internal_input_manifest: Optional[str] = None,
    semantic_layout_policy: Optional[str] = None,
    link_profile_path: Optional[str] = None,
) -> None:
    try:
        _pipeline_self_backend_link.run_link_command(
            cmd,
            asm_path,
            tmp_out_path,
            runtime_archive,
            extra_link_inputs,
            verbose,
            extra_link_args=extra_link_args,
            needs_libpython=needs_libpython,
            needs_native_extension_exports=needs_native_extension_exports,
            pcc_asm_inputs=pcc_asm_inputs,
            pcc_native_object_inputs=pcc_native_object_inputs,
            pcc_internal_input_manifest=pcc_internal_input_manifest,
            semantic_layout_policy=semantic_layout_policy,
            link_profile_path=link_profile_path,
            resolve_self_link_mode=_resolve_self_link_mode,
            validate_pcc_self_link_surface=_validate_pcc_self_link_surface,
            repo_root_for_link=_repo_root_for_link,
            host_python_command=_host_python_command,
            build_pcc_link_command=_pipeline_self_link.build_pcc_link_command,
            log=_log,
            join_strings=_join_strings,
        )
    except _pipeline_self_backend_link.SelfBackendLinkError as exc:
        raise PyPipelineError(str(exc) or type(exc).__name__) from exc


def _link_with_self_backend_ir_texts(
    ir_texts: list[str],
    out_path: str,
    runtime_archive: Optional[str],
    verbose: bool,
    *,
    needs_libpython: bool = False,
    needs_native_extension_exports: bool = False,
    extra_link_inputs: tuple[str, ...] = (),
    extra_link_args: tuple[str, ...] = (),
    tmp_dir: Optional[str] = None,
    profile: Optional[dict] = None,
    consume_ir_texts: bool = False,
) -> None:
    _pipeline_self_backend_link.link_ir_texts(
        ir_texts,
        out_path,
        runtime_archive,
        verbose,
        needs_libpython=needs_libpython,
        needs_native_extension_exports=needs_native_extension_exports,
        extra_link_inputs=extra_link_inputs,
        extra_link_args=extra_link_args,
        tmp_dir=tmp_dir,
        profile=profile,
        consume_ir_texts=consume_ir_texts,
        link_run=_link_self_backend_ir_texts_run,
    )


def _record_macho_link_profile(profile, path: str) -> None:
    if profile is None:
        return
    try:
        with open(path, "r", encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, ValueError) as exc:
        raise PyPipelineError("pcc Mach-O linker produced no valid phase profile") from exc
    if payload.get("schema") != "pcc.macho-link-profile.v1":
        raise PyPipelineError("pcc Mach-O linker phase profile schema mismatch")
    phases = payload.get("phases_ms")
    if not isinstance(phases, dict):
        raise PyPipelineError("pcc Mach-O linker phase profile has no phases")
    for name in (
        "assemble_pool",
        "decode_pco",
        "prepare_link",
        "sign",
        "write",
        "validate",
    ):
        value = phases.get(name)
        if not isinstance(value, (int, float)):
            raise PyPipelineError(
                "pcc Mach-O linker phase profile is missing " + name
            )
        _profile_counter(profile, "link_macho_" + name + "_ms", int(value))


def _prepare_direct_native_object_dir(out_path: str) -> str:
    path = os.path.abspath(str(out_path)) + ".pcc-pco." + str(os.getpid())
    subprocess.run(["/bin/rm", "-rf", path], check=True)
    subprocess.run(["mkdir", "-p", path], check=True)
    return path


def _remove_direct_native_object_dir(path: str) -> None:
    if path:
        subprocess.run(["/bin/rm", "-rf", path], check=True)


def _link_with_self_backend_assembly_texts(
    assembly_texts: list[tuple[str, str]],
    out_path: str,
    runtime_archive: Optional[str],
    verbose: bool,
    *,
    needs_libpython: bool = False,
    needs_native_extension_exports: bool = False,
    extra_link_inputs: tuple[str, ...] = (),
    extra_link_args: tuple[str, ...] = (),
    profile: Optional[dict] = None,
) -> None:
    """Link deterministically ordered assembly already emitted by workers."""
    if _macho_semantic_layout_enabled():
        raise PyPipelineError(
            "direct indexed assembly publication does not yet own semantic layout"
        )
    _validate_pcc_self_link_surface(
        extra_link_args=extra_link_args,
        needs_libpython=needs_libpython,
        needs_native_extension_exports=needs_native_extension_exports,
    )
    signature_owned_by_pcc = _resolve_self_link_mode() == "pcc"
    export_pcc_capi = needs_native_extension_exports and not needs_libpython
    with tempfile.TemporaryDirectory(prefix="pcc_py_direct_asm_") as tmp:
        asm_paths = []
        needs_subsections_via_symbols = False
        started = _profile_begin(profile)
        for index, item in enumerate(assembly_texts):
            module_name, asm_text = item
            safe_name = module_name.replace(".", "_").replace("-", "_")
            asm_path = os.path.join(
                tmp,
                str(index) + "_" + safe_name + ".s",
            )
            with open(asm_path, "w", encoding="utf-8") as stream:
                stream.write(asm_text)
            asm_paths.append(asm_path)
            if ".subsections_via_symbols" in asm_text:
                needs_subsections_via_symbols = True
        _profile_end(profile, "link_self_direct_asm_write", started)

        cc = str(os.environ.get("CC", "") or "").strip() or "cc"
        tmp_out_path = str(out_path) + ".tmp"
        link_profile_path = (
            os.path.join(tmp, "pcc-link-profile.json")
            if signature_owned_by_pcc and profile is not None
            else None
        )
        cmd = [cc] + asm_paths + list(extra_link_inputs)
        if runtime_archive is not None:
            cmd.extend(
                _runtime_archive_link_args_for_native_extensions(
                    runtime_archive,
                    export_pcc_capi,
                )
            )
        cmd.extend(["-o", tmp_out_path, "-lm"])
        cmd.extend(extra_link_args)
        cmd.extend(_native_extension_export_link_flags(export_pcc_capi))
        cmd.extend(
            _libpython_capi_isolation_link_flags(
                runtime_archive,
                needs_libpython,
            )
        )
        if sys.platform == "darwin" and needs_subsections_via_symbols:
            cmd.append("-Wl,-dead_strip")
        cmd.extend(_platform_link_flags())
        if needs_libpython:
            _append_libpython_link_flags(cmd)
        _log(verbose, "direct self link: " + _join_strings(cmd, " "))
        started = _profile_begin(profile)
        _run_self_link_command(
            cmd,
            None,
            tmp_out_path,
            runtime_archive,
            extra_link_inputs,
            verbose,
            extra_link_args=extra_link_args,
            needs_libpython=needs_libpython,
            needs_native_extension_exports=export_pcc_capi,
            pcc_asm_inputs=(
                tuple(asm_paths) if signature_owned_by_pcc else ()
            ),
            link_profile_path=link_profile_path,
        )
        _profile_end(profile, "link_self_direct_asm_driver", started)
        if link_profile_path is not None:
            _record_macho_link_profile(profile, link_profile_path)
        _finish_self_backend_executable(
            tmp_out_path,
            out_path,
            profile,
            signature_owned_by_pcc=signature_owned_by_pcc,
        )


def _write_direct_input_manifest(
    path: str,
    ordered: list[tuple[str, str]],
) -> None:
    with open(path, "w", encoding="utf-8") as stream:
        stream.write("pcc.macho-internal-inputs.v1\n")
        stream.write(str(len(ordered)) + "\n")
        for kind, artifact_path in ordered:
            stream.write(kind + "\t" + artifact_path + "\n")


def _write_deferred_self_link_plan(
    plan_path: str,
    *,
    out_path: str,
    runtime_archive: Optional[str],
    input_manifest: str,
    profile_path: str,
    cleanup_root: str,
    extra_link_inputs: tuple[str, ...],
) -> None:
    with open(plan_path, "w", encoding="utf-8") as stream:
        stream.write("pcc.deferred-self-link.v1\n")
        stream.write(os.path.abspath(str(out_path)) + "\n")
        stream.write(
            ""
            if runtime_archive is None
            else os.path.abspath(str(runtime_archive))
        )
        stream.write("\n")
        stream.write(os.path.abspath(str(input_manifest)) + "\n")
        stream.write(os.path.abspath(str(profile_path)) + "\n")
        stream.write(
            "" if not cleanup_root else os.path.abspath(str(cleanup_root))
        )
        stream.write("\n")
        stream.write(str(len(extra_link_inputs)) + "\n")
        for extra in extra_link_inputs:
            stream.write(os.path.abspath(str(extra)) + "\n")


def _link_with_self_backend_direct_artifacts(
    artifacts: list[tuple[str, str, str]],
    out_path: str,
    runtime_archive: Optional[str],
    verbose: bool,
    *,
    needs_libpython: bool = False,
    needs_native_extension_exports: bool = False,
    extra_link_inputs: tuple[str, ...] = (),
    extra_link_args: tuple[str, ...] = (),
    profile: Optional[dict] = None,
) -> None:
    """Link ordered worker-owned ASM/PCO paths without coordinator copies."""
    if _macho_semantic_layout_enabled():
        raise PyPipelineError(
            "direct indexed artifact publication does not yet own semantic layout"
        )
    _validate_pcc_self_link_surface(
        extra_link_args=extra_link_args,
        needs_libpython=needs_libpython,
        needs_native_extension_exports=needs_native_extension_exports,
    )
    if _resolve_self_link_mode() != "pcc" or sys.platform != "darwin":
        raise PyPipelineError(
            "direct indexed artifacts require the pcc-owned Darwin linker"
        )
    ordered: list[tuple[str, str]] = []
    for _module_name, raw_kind, raw_path in artifacts:
        kind = str(raw_kind)
        if kind not in ("ASM", "PCO"):
            raise PyPipelineError("unknown direct indexed artifact kind: " + kind)
        path = os.path.abspath(str(raw_path))
        if not os.path.isfile(path):
            raise PyPipelineError("direct indexed artifact not found: " + path)
        if "\t" in path or "\n" in path or "\r" in path:
            raise PyPipelineError("direct indexed artifact path is not line-safe")
        ordered.append((kind, path))
    if not ordered:
        raise PyPipelineError("direct indexed artifact set is empty")
    deferred_plan = str(
        os.environ.get("PCC_DEFER_SELF_LINK_PLAN", "") or ""
    ).strip()
    if deferred_plan:
        deferred_plan = os.path.abspath(deferred_plan)
        input_manifest = deferred_plan + ".inputs"
        profile_path = deferred_plan + ".profile.json"
        _write_direct_input_manifest(input_manifest, ordered)
        cleanup_root = os.path.dirname(ordered[0][1])
        for _kind, path in ordered:
            if os.path.dirname(path) != cleanup_root:
                cleanup_root = ""
                break
        _write_deferred_self_link_plan(
            deferred_plan,
            out_path=out_path,
            runtime_archive=runtime_archive,
            input_manifest=input_manifest,
            profile_path=profile_path,
            cleanup_root=cleanup_root,
            extra_link_inputs=extra_link_inputs,
        )
        return
    with tempfile.TemporaryDirectory(prefix="pcc_py_direct_artifacts_") as tmp:
        tmp_out_path = str(out_path) + ".tmp"
        input_manifest = os.path.join(tmp, "internal-inputs.txt")
        _write_direct_input_manifest(input_manifest, ordered)
        link_profile_path = (
            os.path.join(tmp, "pcc-link-profile.json")
            if profile is not None
            else None
        )
        cc = str(os.environ.get("CC", "") or "").strip() or "cc"
        cmd = [cc] + [path for _kind, path in ordered]
        cmd += list(extra_link_inputs) + ["-o", tmp_out_path]
        started = _profile_begin(profile)
        _run_self_link_command(
            cmd,
            None,
            tmp_out_path,
            runtime_archive,
            extra_link_inputs,
            verbose,
            extra_link_args=extra_link_args,
            needs_libpython=needs_libpython,
            needs_native_extension_exports=needs_native_extension_exports,
            pcc_internal_input_manifest=input_manifest,
            link_profile_path=link_profile_path,
        )
        all_native = all(kind == "PCO" for kind, _path in ordered)
        _profile_end(
            profile,
            (
                "link_self_direct_native_object_driver"
                if all_native
                else "link_self_direct_mixed_artifact_driver"
            ),
            started,
        )
        if link_profile_path is not None:
            _record_macho_link_profile(profile, link_profile_path)
        _finish_self_backend_executable(
            tmp_out_path,
            out_path,
            profile,
            signature_owned_by_pcc=True,
        )


def _link_with_self_backend_native_objects(
    native_objects: list[tuple[str, str]],
    out_path: str,
    runtime_archive: Optional[str],
    verbose: bool,
    *,
    needs_libpython: bool = False,
    needs_native_extension_exports: bool = False,
    extra_link_inputs: tuple[str, ...] = (),
    extra_link_args: tuple[str, ...] = (),
    profile: Optional[dict] = None,
) -> None:
    _link_with_self_backend_direct_artifacts(
        [(module_name, "PCO", path) for module_name, path in native_objects],
        out_path,
        runtime_archive,
        verbose,
        needs_libpython=needs_libpython,
        needs_native_extension_exports=needs_native_extension_exports,
        extra_link_inputs=extra_link_inputs,
        extra_link_args=extra_link_args,
        profile=profile,
    )


def _link_native(
    ll_paths,
    out_path: str,
    runtime_archive: Optional[str],
    verbose: bool,
    *,
    backend,
    needs_libpython: bool = False,
    needs_native_extension_exports: bool = False,
    extra_link_inputs: tuple[str, ...] = (),
    extra_link_args: tuple[str, ...] = (),
    profile: Optional[dict] = None,
) -> None:
    kind = _native_backend_kind(backend)
    if kind == "llvm":
        _link_with_clang(
            ll_paths,
            out_path,
            runtime_archive,
            verbose,
            needs_libpython=needs_libpython,
            needs_native_extension_exports=needs_native_extension_exports,
            extra_link_inputs=extra_link_inputs,
            extra_link_args=extra_link_args,
        )
        return
    if kind == "self":
        _link_with_self_backend(
            ll_paths,
            out_path,
            runtime_archive,
            verbose,
            needs_libpython=needs_libpython,
            needs_native_extension_exports=needs_native_extension_exports,
            extra_link_inputs=extra_link_inputs,
            extra_link_args=extra_link_args,
            profile=profile,
        )
        return
    raise PyPipelineError(f"unsupported Python native backend: {kind}")


def _clang_link_compatible_python_ir(ir_text: str) -> str:
    """Lower newer LLVM memory-effect attrs before handing .ll to clang."""

    text = str(ir_text)
    # Keep the source patterns split so this module's own string-object
    # globals do not contain the exact rewrite needles when pipeline.py is
    # self-compiled. A raw text replace across the emitted IR cannot
    # distinguish attributes from string initializers.
    text = text.replace("memory(" + "argmem: read)", "argmemonly readonly")
    text = text.replace("memory(" + "argmem: write)", "argmemonly writeonly")
    text = text.replace("memory(" + "none)", "readnone")
    text = text.replace("memory(" + "read)", "readonly")
    return text


_py_ast_field_names = _pipeline_ast_wire._py_ast_field_names


_py_ast_field_value = _pipeline_libpython.ast_field_value


def _py_ast_name_used_at_runtime(stmts, ident: str) -> bool:
    return _pipeline_libpython.ast_name_used_at_runtime(stmts, ident)


_module_import_is_scaffold = _pipeline_libpython.module_import_is_scaffold
_resolve_relative_import = _pipeline_libpython.resolve_relative_import


def _module_needs_libpython(
    ast_module,
    native_modules=None,
    ir_scaffold_mode: str = "off",
    strict_no_libpython: bool = False,
) -> bool:
    return _pipeline_libpython.module_needs_libpython(
        ast_module,
        native_modules=native_modules,
        ir_scaffold_mode=ir_scaffold_mode,
        strict_no_libpython=strict_no_libpython,
    )


def _module_imports_pcc_native_extension(
    ast_module,
    native_modules=None,
    ir_scaffold_mode: str = "off",
) -> bool:
    return _pipeline_libpython.module_imports_native_extension(
        ast_module,
        native_modules=native_modules,
        ir_scaffold_mode=ir_scaffold_mode,
    )


_resolve_python_config_command = _pipeline_libpython.resolve_python_config_command


_ir_needs_libpython = _pipeline_libpython.ir_needs_libpython
_ensure_libpython_main_thread_init = _pipeline_libpython.ensure_main_thread_init


def compile_python(
    src_path: str,
    out_path: str,
    *,
    verbose: bool = False,
    emit_llvm_only: bool = False,
    libpython_mode: Optional[str] = None,
    ir_scaffold_mode: Optional[str] = None,
    backend: Optional[str] = None,
    gpu_backend: Optional[str] = None,
    target_triple: Optional[str] = None,
    recursive_stdlib: bool = False,
    python_library: bool = False,
    runtime_archive: Optional[str] = None,
    link_args: tuple[str, ...] = (),
    profile: Optional[dict] = None,
) -> None:
    """Compile a single ``.py`` file to a native executable.

    Parameters
    ----------
    src_path:
        Path to the input ``.py`` source file.
    out_path:
        Output path. When ``emit_llvm_only`` is False this is the
        native executable; when True it is the ``.ll`` file.
    verbose:
        If True, print each pipeline step and timing info to stderr.
    emit_llvm_only:
        If True, stop after writing LLVM IR (used by ``--emit-llvm``).
    backend:
        Native emission backend for executable output. ``llvm`` keeps
        the historical clang ``.ll`` path; ``self`` lowers ``.ll``
        through the in-repo asm backend before linking.
    target_triple:
        Optional explicit target written into frontend-emitted IR.  This is
        required for cross-target self-backend emission because the backend
        refuses the frontend's ``unknown-unknown-unknown`` placeholder.
    python_library:
        Emit a library module object shape: no program ``@main`` is
        synthesized, but module init/top-init functions remain available
        for an embedding entrypoint to call. This is intended for
        pcc-Python runtime archives and must be paired with
        ``emit_llvm_only``.
    runtime_archive:
        Optional explicit native runtime archive for isolated builds/tests.
        When omitted, the configured repository runtime is located or built.
    link_args:
        Raw arguments appended only to the final native linker invocation.
        They are ignored when ``emit_llvm_only`` stops before linking.
    """
    if python_library and not emit_llvm_only:
        raise PyPipelineError("python_library mode requires emit_llvm_only=True")
    # Imports are deferred so that modules still under construction by
    # sibling agents don't break ``pcc --help`` or ``.c`` compilation.
    total_start = _profile_begin(profile)
    libpython_mode = _resolve_libpython_mode(libpython_mode)
    ir_scaffold_mode = _resolve_ir_scaffold_mode(ir_scaffold_mode)
    gpu_backend_kind = _resolve_gpu_backend_kind(gpu_backend)

    try:
        from .type_infer import infer_module as _infer_module
        from .codegen.layer1 import L1CodeGen as _L1CodeGen
    except ImportError as e:
        raise PyPipelineError(
            f"Python frontend module not available: {e}. "
            "The Python pipeline is currently Phase 1 MVP and some "
            "components may still be under construction."
        ) from e

    if not os.path.isfile(src_path):
        raise PyPipelineError(f"input file not found: {src_path}")

    module_name = _module_name_from_src(src_path)
    gpu_source = None
    gpu_source_has_kernels = False
    gpu_artifact_dir: Optional[str] = None
    gpu_metallib_path: Optional[str] = None
    if gpu_backend_kind == "metal":
        with open(src_path, "r", encoding="utf-8") as f:
            gpu_source = f.read()
        source_contains_gpu_kernel = getattr(
            _load_pcc_gpu_kernel_module(),
            "source_contains_gpu_kernel",
        )

        gpu_source_has_kernels = source_contains_gpu_kernel(gpu_source, src_path)
    should_auto_close = (not emit_llvm_only) or module_name.endswith(".__main__")
    t = _profile_begin(profile)
    auto_srcs, auto_mods = (
        _collect_relative_module_closure(
            src_path,
            include_same_package_absolute=(module_name.endswith(".__main__")),
            recurse_same_package_absolute=(libpython_mode == "off"),
        )
        if should_auto_close
        else ([str(os.path.abspath(src_path))], [module_name])
    )
    _profile_end(profile, "collect_relative_module_closure", t)
    t = _profile_begin(profile)
    auto_srcs, auto_mods = _filter_ir_scaffold_closure(
        auto_srcs,
        auto_mods,
        ir_scaffold_mode=ir_scaffold_mode,
    )
    _profile_end(profile, "filter_ir_scaffold_closure", t)
    t = _profile_begin(profile)
    auto_seen = {mod_name: src_path for src_path, mod_name in zip(auto_srcs, auto_mods)}
    _expand_native_extension_module_object_ports(
        auto_srcs,
        auto_mods,
        auto_seen,
    )
    _profile_end(profile, "expand_native_extension_module_object_ports", t)
    t = _profile_begin(profile)
    _validate_package_site_no_libpython_abi(
        auto_srcs,
        libpython_mode=libpython_mode,
    )
    _profile_end(profile, "validate_package_site_abi", t)
    _profile_counter(profile, "auto_files", len(auto_srcs))
    effective_recursive_stdlib = recursive_stdlib
    if (
        not effective_recursive_stdlib
        and libpython_mode == "off"
        and not python_library
        # A plain emit-only request is a per-module IR diagnostic.  Expanding
        # its stdlib imports changes that request into a multi-module compile,
        # multiplying parse/type/codegen work and changing when the
        # no-libpython gate runs.  Executable builds and package entrypoints
        # still close their runtime graph automatically; callers that want a
        # standalone closure dump can request recursive_stdlib explicitly.
        and should_auto_close
        and _sources_use_native_stdlib(auto_srcs)
    ):
        effective_recursive_stdlib = True
    if gpu_source_has_kernels and effective_recursive_stdlib:
        raise PyPipelineError(
            "--gpu-backend=metal currently supports @gpu.kernel only in "
            "single-file Python compiles"
        )
    direct_indexed_assembly = str(
        os.environ.get("PCC_DIRECT_INDEXED_KERNEL_EMIT", "") or ""
    ).strip().lower() in ("1", "true", "yes", "on")
    if direct_indexed_assembly and len(auto_srcs) == 1:
        if emit_llvm_only or python_library:
            raise PyPipelineError(
                "direct indexed assembly mode cannot satisfy an LLVM/library output"
            )
        if gpu_source_has_kernels:
            raise PyPipelineError(
                "direct indexed assembly mode does not yet own GPU host lowering"
            )
        entry = _first_string(auto_mods)
        compile_python_multi(
            auto_srcs,
            out_path,
            verbose=verbose,
            emit_llvm_only=False,
            entry_module=entry,
            module_names=auto_mods,
            libpython_mode=libpython_mode,
            ir_scaffold_mode=ir_scaffold_mode,
            backend=backend,
            target_triple=target_triple,
            recursive_stdlib=effective_recursive_stdlib,
            runtime_archive=runtime_archive,
            link_args=link_args,
            profile=profile,
        )
        _profile_end(profile, "compile_python_total", total_start)
        return
    # Issue 11.B.1.2: when recursive_stdlib is on, force the multi-file
    # path so _expand_recursive_stdlib has a chance to pull pure-Python
    # stdlib into the native compile. The multi-file path also
    # populates _native_module_exports which lets _emit_import skip
    # py_cpy_import for natively-compiled modules.
    if effective_recursive_stdlib and len(auto_srcs) == 1:
        entry = _first_string(auto_mods)
        compile_python_multi(
            auto_srcs,
            out_path,
            verbose=verbose,
            emit_llvm_only=emit_llvm_only,
            entry_module=entry,
            module_names=auto_mods,
            libpython_mode=libpython_mode,
            ir_scaffold_mode=ir_scaffold_mode,
            backend=backend,
            target_triple=target_triple,
            recursive_stdlib=True,
            runtime_archive=runtime_archive,
            link_args=link_args,
            profile=profile,
        )
        _profile_end(profile, "compile_python_total", total_start)
        return
    if len(auto_srcs) > 1:
        if gpu_source_has_kernels:
            raise PyPipelineError(
                "--gpu-backend=metal currently supports @gpu.kernel only in "
                "single-file Python compiles"
            )
        if python_library:
            raise PyPipelineError(
                "python_library mode only supports a single Python source"
            )
        if verbose:
            _log(
                verbose,
                "auto multi-file package compile: " + _join_strings(auto_mods, ", "),
            )
        entry = _first_string(auto_mods)
        compile_python_multi(
            auto_srcs,
            out_path,
            verbose=verbose,
            emit_llvm_only=emit_llvm_only,
            entry_module=entry,
            module_names=auto_mods,
            libpython_mode=libpython_mode,
            ir_scaffold_mode=ir_scaffold_mode,
            backend=backend,
            target_triple=target_triple,
            recursive_stdlib=effective_recursive_stdlib,
            runtime_archive=runtime_archive,
            link_args=link_args,
            profile=profile,
        )
        _profile_end(profile, "compile_python_total", total_start)
        return

    if verbose:
        _log(verbose, "reading " + src_path)
    t = _profile_begin(profile)
    if gpu_source is None:
        with open(src_path, "r", encoding="utf-8") as f:
            source = f.read()
    else:
        source = gpu_source
    _profile_end(profile, "read_source", t)
    freestanding_module = _source_declares_freestanding_module(source)
    runtime_port_module = _source_declares_runtime_port_module(source)
    freestanding_external_symbols = (
        _freestanding_allowed_external_symbols(source)
        if freestanding_module
        else set()
    )
    if freestanding_module and not python_library:
        raise PyPipelineError(
            "freestanding modules require python_library mode"
        )
    if freestanding_module and libpython_mode != "off":
        raise PyPipelineError(
            "freestanding modules require --python-libpython=off"
        )
    if gpu_source_has_kernels:
        t = _profile_begin(profile)
        prepare_gpu_kernels_for_source = getattr(
            _load_pcc_gpu_kernel_module(),
            "prepare_gpu_kernels_for_source",
        )

        artifact_dir = str(out_path) + ".gpu"
        gpu_artifact_dir = artifact_dir
        metallib_path = str(out_path) + ".metallib"
        gpu_metallib_path = metallib_path
        try:
            source, gpu_artifacts = prepare_gpu_kernels_for_source(
                source,
                src_path,
                backend=gpu_backend_kind,
                artifact_dir=artifact_dir,
                metallib_path=metallib_path,
            )
        except Exception as exc:
            raise PyPipelineError(
                "Metal GPU kernel lowering failed: " + str(exc)
            ) from exc
        _profile_counter(profile, "gpu_kernels", len(gpu_artifacts))
        _profile_end(profile, "gpu_kernel_lowering", t)

    _log(verbose, "parse")
    # pcc.parse.py_parse + pcc.parse.py_lift is the bootstrap-safe
    # parser path. The previous CPython-ast escape hatch kept a
    # libpython import edge alive in the compiled pipeline, so the
    # self-host path no longer emits it.
    from ..parse.py_lift import parse_and_lift as _parse_and_lift

    _log(verbose, "parse")
    t = _profile_begin(profile)
    ast_mod = _parse_and_lift(
        source,
        src_path,
        _module_name_from_src(src_path),
    )
    _profile_end(profile, "parse_and_lift", t)

    t = _profile_begin(profile)
    ast_needs_libpython = _module_needs_libpython(
        ast_mod,
        ir_scaffold_mode=ir_scaffold_mode,
        strict_no_libpython=(libpython_mode == "off"),
    )
    _profile_end(profile, "detect_libpython_need", t)
    t = _profile_begin(profile)
    ast_needs_native_extension_exports = _module_imports_pcc_native_extension(
        ast_mod,
        ir_scaffold_mode=ir_scaffold_mode,
    )
    _profile_end(profile, "detect_native_extension_exports", t)

    runtime_port_abi_exports = None
    if python_library and _is_py_runtime_library_source(src_path):
        runtime_port_abi_exports = _copy_native_module_exports(
            PORT_ABI_NATIVE_EXPORTS
        )

    _log(verbose, "type_infer")
    t = _profile_begin(profile)
    typed_mod = _infer_module(
        ast_mod,
        external_exports=runtime_port_abi_exports,
    )
    _profile_end(profile, "type_infer", t)

    _log(verbose, "codegen (layer1)")
    t = _profile_begin(profile)
    codegen = _L1CodeGen(
        typed_mod,
        (libpython_mode == "on" or (libpython_mode == "auto" and ast_needs_libpython)),
        ir_scaffold_mode,
    )
    codegen._strict_no_libpython = libpython_mode == "off"
    codegen._prefer_native_callable_values = libpython_mode == "off"
    codegen._module_source_path = os.path.abspath(src_path)
    codegen._target_triple = target_triple or ""
    codegen._freestanding_module = freestanding_module
    codegen._runtime_port_module = runtime_port_module
    if runtime_port_abi_exports is not None:
        codegen._native_module_exports = runtime_port_abi_exports
    if freestanding_module:
        # Freestanding functions form the runtime's dependency root.  They
        # must remain callable while the managed threading/GC substrate is
        # unavailable, so PCC_WITH_THREADS may not inject the ordinary
        # function-entry poll (pcc_thread_stop_requested /
        # pcc_thread_safepoint) into this closure.
        codegen._thread_safepoints_enabled = False
    if python_library:
        codegen._python_library = True
        codegen._skip_program_main = True
        if freestanding_module or _is_py_runtime_library_source(src_path):
            codegen._suppress_implicit_gc_roots = True
            codegen._suppress_borrowed_return_retain = True
    # Layer1 codegen returns IR text here.  Do not defensively call
    # isinstance(..., str) or str(...) on the result: pcc1/pcc2 self-host can
    # hit a builtin-type class boundary after codegen has already returned.
    ir_text = codegen.generate(typed_mod)
    ir_text = _ir_text_with_target_triple(ir_text, target_triple)
    if freestanding_module:
        _validate_freestanding_ir(ir_text, freestanding_external_symbols)
    _profile_end(profile, "codegen_layer1", t)
    _profile_counter(profile, "ir_bytes", len(ir_text))
    native_backend = None
    if not emit_llvm_only:
        native_backend = _resolve_native_backend(backend)
    t = _profile_begin(profile)
    ir_text = _apply_python_ir_pass_pipeline(
        ir_text,
        module_name=module_name,
        verbose=verbose,
        default_raw=_default_python_ir_pass_raw_for_request(
            native_backend,
            emit_llvm_only=emit_llvm_only,
            backend=backend,
        ),
        strict_no_libpython=(libpython_mode == "off"),
    )
    if freestanding_module:
        _validate_freestanding_ir(ir_text, freestanding_external_symbols)
    _profile_end(profile, "python_ir_pass_pipeline", t)

    ir_needs_libpython = _ir_needs_libpython(ir_text)
    if (
        libpython_mode != "off"
        and not python_library
        and (ast_needs_libpython or ir_needs_libpython)
    ):
        ir_text = _ensure_libpython_main_thread_init(ir_text)

    if emit_llvm_only:
        # out_path is a .ll path; just write it and return.
        t = _profile_begin(profile)
        _emit_ll(ir_text, out_path, verbose)
        _profile_end(profile, "emit_ll", t)
        _profile_end(profile, "compile_python_total", total_start)
        return

    t = _profile_begin(profile)
    needs_libpython = ast_needs_libpython
    reasons = []
    if needs_libpython:
        reasons.append("imports still lower through CPython fallback")
    # Fallback: scan the generated IR for direct call sites into the
    # libpython shim (``py_cpy_*``). Codegen emits these for DynType
    # method dispatch, ``hasattr`` fallback, ``x.__copy__()`` and
    # similar even when the source has no explicit ``import``. Using
    # ``\bcall`` rather than a plain text search avoids triggering on
    # the ``declare external`` stubs emitted unconditionally for all
    # runtime helpers.
    if ir_needs_libpython:
        needs_libpython = True
        reasons.append("generated IR still calls py_cpy_* helpers")
    needs_libpython = _finalize_libpython_mode(
        detected=needs_libpython,
        mode=libpython_mode,
        context=str(src_path),
        reasons=reasons,
    )
    _reject_mixed_extension_object_models(
        needs_libpython=needs_libpython,
        needs_native_extension_exports=ast_needs_native_extension_exports,
    )
    _profile_end(profile, "finalize_libpython_mode", t)
    if native_backend is None:
        native_backend = _resolve_native_backend(backend)
    if verbose:
        _log(verbose, "native backend: " + str(native_backend))

    t = _profile_begin(profile)
    if runtime_archive is not None:
        runtime = os.path.abspath(str(runtime_archive))
        if not os.path.isfile(runtime):
            raise PyPipelineError("explicit runtime archive not found: " + runtime)
    else:
        runtime = _ensure_runtime(
            verbose,
            needs_libpython=needs_libpython,
        )
    _profile_end(profile, "ensure_runtime", t)
    extra_link_inputs: tuple[str, ...] = ()
    extra_link_args: tuple[str, ...] = tuple(link_args)
    if gpu_source_has_kernels and gpu_backend_kind == "metal":
        if gpu_artifact_dir is None:
            gpu_artifact_dir = str(out_path) + ".gpu"
        t = _profile_begin(profile)
        compile_metal_runtime_bridge = getattr(
            _load_pcc_gpu_metal_module(),
            "compile_metal_runtime_bridge",
        )

        try:
            metal_bridge_obj = compile_metal_runtime_bridge(
                os.path.join(gpu_artifact_dir, "pcc_metal_runtime.o"),
            )
        except Exception as exc:
            raise PyPipelineError(
                "Metal GPU host bridge compile failed: " + str(exc)
            ) from exc
        extra_link_inputs = (str(metal_bridge_obj),)
        if gpu_metallib_path is None:
            gpu_metallib_path = str(out_path) + ".metallib"
        extra_link_args = extra_link_args + (
            "-Xlinker",
            "-sectcreate",
            "-Xlinker",
            "__PCCMETAL",
            "-Xlinker",
            "__metallib",
            "-Xlinker",
            str(gpu_metallib_path),
            "-framework",
            "Foundation",
            "-framework",
            "Metal",
        )
        _profile_end(profile, "gpu_metal_bridge_compile", t)
    if native_backend == "self" and _self_backend_skip_ll_temp():
        if verbose:
            _log(
                verbose,
                "self backend: linking LLVM IR text without pipeline .ll temp",
            )
        t = _profile_begin(profile)
        _link_with_self_backend_ir_texts(
            [ir_text],
            out_path,
            runtime,
            verbose,
            needs_libpython=needs_libpython,
            needs_native_extension_exports=ast_needs_native_extension_exports,
            extra_link_inputs=extra_link_inputs,
            extra_link_args=extra_link_args,
            profile=profile,
        )
        _profile_end(profile, "link_self_backend_ir_texts", t)
        if verbose:
            _log(verbose, "wrote executable: " + out_path)
        _profile_end(profile, "compile_python_total", total_start)
        return

    # Write IR to a temp .ll, link with clang + runtime, produce exe.
    with tempfile.TemporaryDirectory(prefix="pcc_py_") as tmp:
        ll_name = str(os.path.basename(out_path)) + ".ll"
        ll_path = str(os.path.join(tmp, ll_name))
        link_ir_text = ir_text
        if native_backend != "self":
            link_ir_text = _clang_link_compatible_python_ir(link_ir_text)
        t = _profile_begin(profile)
        _emit_ll(link_ir_text, ll_path, verbose)
        _profile_end(profile, "emit_ll", t)
        t = _profile_begin(profile)
        _link_native(
            [ll_path],
            out_path,
            runtime,
            verbose,
            backend=native_backend,
            needs_libpython=needs_libpython,
            needs_native_extension_exports=ast_needs_native_extension_exports,
            extra_link_inputs=extra_link_inputs,
            extra_link_args=extra_link_args,
            profile=profile,
        )
        _profile_end(profile, "link_native", t)
    if verbose:
        _log(verbose, "wrote executable: " + out_path)
    _profile_end(profile, "compile_python_total", total_start)


def _python_frontend_jobs(job_count_hint: int) -> int:
    return _pipeline_frontend_workers.frontend_jobs(
        job_count_hint,
        str(os.environ.get(_PY_FRONTEND_JOBS_ENV, "") or ""),
        _parallel_cpu_budget(),
    )


def _python_frontend_package_site_workload(src_paths) -> bool:
    for src_path in src_paths:
        if _package_site_package_root_for_src(str(src_path)) is not None:
            return True
    return False


def _python_frontend_jobs_for_sources(src_paths) -> int:
    jobs = _python_frontend_jobs(len(src_paths))
    if jobs <= 1:
        return jobs
    raw = str(os.environ.get(_PY_FRONTEND_JOBS_ENV, "") or "")
    if _pipeline_frontend_workers.numeric_jobs_override(raw):
        return jobs
    # A pcc-native package graph can mix generated package modules, native
    # extension object ports, and host-located stdlib sources. Ten isolated
    # native frontend workers are useful for the compiler bootstrap, but this
    # package shape retained enough allocator state to push one NumPy compile
    # beyond 18 GiB RSS. Even two quiet compiled-stage workers retain enough
    # allocator state to cross the 16 GiB process-group budget; verbose worker
    # timing merely masks that behavior. Keep bootstrap's auto=10 policy and
    # serialize package-graph frontend workers until the compiled allocator can
    # prove a lower retained-heap bound. An explicit numeric override remains
    # authoritative.
    if _python_frontend_package_site_workload(src_paths):
        return 1
    return jobs


def _python_frontend_worker_timing_enabled() -> bool:
    return _pipeline_frontend_workers.worker_timing_enabled(
        str(os.environ.get(_PY_FRONTEND_WORKER_TIMING_ENV, "") or "")
    )


def _python_frontend_worker_env_prefix() -> str:
    """Keep verbose worker timing separate from aggregate CLI profiling."""
    return _pipeline_frontend_workers.worker_env_prefix(
        timing_enabled=_python_frontend_worker_timing_enabled()
    )


def _python_frontend_ast_wire_enabled() -> bool:
    return _pipeline_frontend_workers.ast_wire_enabled(
        str(os.environ.get(_PY_FRONTEND_AST_WIRE_ENV, "") or "")
    )


def _is_native_worker_executable(path: str) -> bool:
    return _pipeline_frontend_workers.is_native_worker_executable(path)


def _python_frontend_worker_executable() -> str:
    sys_executable = ""
    try:
        sys_executable = str(getattr(sys, "executable", "") or "")
    except Exception:
        pass
    argv_zero = ""
    try:
        if len(sys.argv) > 0:
            argv_zero = str(sys.argv[0] or "")
    except Exception:
        pass
    candidates = _pipeline_frontend_workers.worker_executable_candidates(
        sys_executable,
        argv_zero,
    )
    return _pipeline_frontend_workers.select_native_worker_executable(
        candidates,
        native_predicate=_is_native_worker_executable,
    )


def _python_frontend_worker_command_prefix() -> list[str]:
    exe = _python_frontend_worker_executable()
    if exe:
        return [exe]
    try:
        py_exe = str(getattr(sys, "executable", "") or "")
    except Exception:
        py_exe = ""
    if py_exe and os.path.isfile(py_exe):
        # Source-mode stage1 reaches the same bootstrap entry through
        # pcc/__main__.py, so the hidden worker is available as
        # ``python -m pcc --pcc-python-multi-codegen-worker ...``. This avoids
        # forcing stage1 to stay serial while stage2/stage3 use native workers.
        return [py_exe, "-m", "pcc"]
    return []


def _can_spawn_python_frontend_worker() -> bool:
    return bool(_python_frontend_worker_command_prefix())


def _python_frontend_codegen_chunks(src_paths, jobs: int):
    return _pipeline_frontend_workers.codegen_chunks(src_paths, jobs)


def _python_frontend_codegen_chunk_count(
    src_count: int, jobs: int, worker_prefix
) -> int:
    return _pipeline_frontend_workers.codegen_chunk_count(
        src_count,
        jobs,
        worker_prefix,
        native_predicate=_is_native_worker_executable,
    )


def _write_python_frontend_worker_manifest(
    path: str,
    result_path: str,
    ir_dir: str,
    exports_path: str,
    ast_dir: str,
    src_paths,
    module_names,
    assigned_indices,
    *,
    entry_module: str,
    sibling_inits,
    libpython_mode: str,
    ir_scaffold_mode: str,
    verbose: bool,
    job_kind: str = "codegen",
) -> None:
    _pipeline_frontend_workers.write_worker_manifest(
        path,
        result_path,
        ir_dir,
        exports_path,
        ast_dir,
        src_paths,
        module_names,
        assigned_indices,
        entry_module=entry_module,
        sibling_inits=sibling_inits,
        libpython_mode=libpython_mode,
        ir_scaffold_mode=ir_scaffold_mode,
        verbose=verbose,
        job_kind=job_kind,
    )


def _read_python_frontend_worker_manifest(path: str):
    try:
        return _pipeline_frontend_workers.read_worker_manifest(path)
    except _pipeline_frontend_workers.FrontendWorkerContractError as exc:
        raise PyPipelineError(str(exc) or type(exc).__name__) from exc


def _write_python_frontend_worker_error(result_path: str, message: str) -> None:
    _pipeline_frontend_workers.write_worker_error(result_path, message)


def _read_python_frontend_worker_ir(ir_path: str, module_name: str) -> str:
    """Read one worker result and reject the silent empty-module failure."""
    try:
        return _pipeline_frontend_workers.read_worker_ir(ir_path, module_name)
    except _pipeline_frontend_workers.FrontendWorkerContractError as exc:
        raise PyPipelineError(str(exc) or type(exc).__name__) from exc


def _safe_exception_text(exc) -> str:
    return _pipeline_frontend_workers.safe_exception_text(exc)


def _shell_quote_arg(text: str) -> str:
    return _pipeline_frontend_workers.shell_quote_arg(text)


def _run_python_multi_export_worker(manifest) -> int:
    return _pipeline_frontend_worker_execution.run_export_worker(
        manifest,
        worker_timing_enabled=_python_frontend_worker_timing_enabled,
        build_closed_world_context=build_closed_world_context,
        write_ast_wire=_write_py_ast_wire,
        closed_world_reexport_edges=_closed_world_reexport_edges,
        closed_world_module_dependencies=_closed_world_module_dependencies,
        mark_function_object_exports=_mark_closed_world_function_object_exports,
        write_native_exports_wire=_write_native_exports_wire,
        write_reexport_edges_wire=_write_reexport_edges_wire,
    )


def _run_python_multi_summary_worker(manifest) -> int:
    return _pipeline_frontend_worker_execution.run_summary_worker(
        manifest,
        read_native_exports_wire=_read_native_exports_wire,
        read_ast_wire=_read_py_ast_wire,
        build_effect_summary=_build_closed_world_vthread_effect_summary,
        write_effect_summary=_write_closed_world_vthread_effect_summary,
    )


def run_python_multi_codegen_worker(manifest_path: str) -> int:
    return _pipeline_frontend_worker_execution.run_codegen_worker(
        manifest_path,
        read_manifest=_read_python_frontend_worker_manifest,
        run_export_worker_callback=_run_python_multi_export_worker,
        run_summary_worker_callback=_run_python_multi_summary_worker,
        worker_timing_enabled=_python_frontend_worker_timing_enabled,
        native_worker_executable=_python_frontend_worker_executable,
        read_native_exports_wire=_read_native_exports_wire,
        read_native_exports_wire_for_module=(
            _read_native_exports_wire_for_module
        ),
        read_ast_wire=_read_py_ast_wire,
        build_closed_world_context=build_closed_world_context,
        module_imports_native_extension=_module_imports_pcc_native_extension,
        contextual_host_params_for_module=_contextual_host_params_for_module,
        module_uses_default_native_exports=_module_uses_default_native_exports,
        copy_native_module_exports=_copy_native_module_exports,
        closed_world_function_object_exports=(
            _closed_world_function_object_exports
        ),
        log=_log,
        ir_needs_libpython=_ir_needs_libpython,
        safe_exception_text=_safe_exception_text,
        write_worker_error=_write_python_frontend_worker_error,
        pipeline_error=PyPipelineError,
    )


_run_python_frontend_worker_commands = (
    _pipeline_frontend_workers.run_worker_commands
)


def _build_unique_external_class_preload_index(native_exports):
    from .type_infer import build_unique_external_class_preload_index

    return build_unique_external_class_preload_index(native_exports)


def _build_python_frontend_shared_exports_parallel(
    tmp: str,
    src_paths,
    module_names,
    chunks,
    worker_prefix,
    *,
    entry_module: str,
    sibling_inits,
    libpython_mode: str,
    ir_scaffold_mode: str,
    verbose: bool,
    max_parallel: int,
    oversized_chunk_count: int,
    safe_parallel: int,
    ast_dir: str = "",
    profile: Optional[dict] = None,
    module_dependency_map=None,
) -> str:
    return _pipeline_frontend_parallel.build_shared_exports(
        tmp,
        src_paths,
        module_names,
        chunks,
        worker_prefix,
        entry_module=entry_module,
        sibling_inits=sibling_inits,
        libpython_mode=libpython_mode,
        ir_scaffold_mode=ir_scaffold_mode,
        verbose=verbose,
        max_parallel=max_parallel,
        oversized_chunk_count=oversized_chunk_count,
        safe_parallel=safe_parallel,
        ast_dir=ast_dir,
        profile=profile,
        module_dependency_map=module_dependency_map,
        build_unique_class_preload_index=(
            _build_unique_external_class_preload_index
        ),
        contextual_host_for_module=contextual_host_for_module,
        build_contextual_host_export_surface=(
            _contextual_host_export_surface
        ),
        write_manifest=_write_python_frontend_worker_manifest,
        shell_quote_arg=_shell_quote_arg,
        worker_arg=_PY_FRONTEND_WORKER_ARG,
        worker_env_prefix=_python_frontend_worker_env_prefix,
        join_strings=_join_strings,
        run_worker_commands=_run_python_frontend_worker_commands,
        profiled_gc_collect=_profiled_gc_collect,
        read_native_exports_wire=_read_native_exports_wire,
        read_reexport_edges_wire=_read_reexport_edges_wire,
        merge_reexport_edges=_merge_closed_world_reexport_edges,
        flatten_class_export_fields=_flatten_closed_world_class_export_fields,
        repair_default_global_owners=_repair_closed_world_default_global_owners,
        merge_mixin_stack_methods=_merge_l1_mixin_stack_methods,
        merge_codegen_methods=_merge_l1_codegen_methods,
        expand_valueclass_export_refs=(
            _pipeline_exports._expand_local_valueclass_export_refs
        ),
        apply_function_object_uses=_apply_closed_world_function_object_uses,
        read_ast_wire=_read_py_ast_wire,
        annotate_vthread_effects=_annotate_closed_world_vthread_effects,
        annotate_vthread_effect_summaries=(
            _annotate_closed_world_vthread_effect_summaries
        ),
        vthread_effect_export_surface=(
            _closed_world_vthread_effect_export_surface
        ),
        derived_class_map=_closed_world_derived_class_map,
        write_native_exports_wire=_write_native_exports_wire,
        profile_counter=_profile_counter,
        pipeline_error=PyPipelineError,
    )


def _compile_python_multi_codegen_parallel(
    src_paths,
    module_names,
    *,
    jobs: int,
    entry_module: str,
    sibling_inits,
    libpython_mode: str,
    ir_scaffold_mode: str,
    verbose: bool,
    profile: Optional[dict] = None,
    artifact_dir: str = "",
) -> Optional[
    tuple[
        list[tuple[str, str]],
        bool,
        bool,
        int,
        list[str],
        Optional[list[tuple[str, str]]],
    ]
]:
    return _pipeline_frontend_parallel.compile_parallel(
        src_paths,
        module_names,
        jobs=jobs,
        entry_module=entry_module,
        sibling_inits=sibling_inits,
        libpython_mode=libpython_mode,
        ir_scaffold_mode=ir_scaffold_mode,
        verbose=verbose,
        profile=profile,
        artifact_dir=artifact_dir,
        can_spawn_worker=_can_spawn_python_frontend_worker,
        worker_command_prefix=_python_frontend_worker_command_prefix,
        compile_uncached=_compile_python_multi_codegen_parallel_uncached,
        plan_cache=plan_python_frontend_ir_cache,
        load_cache=load_python_frontend_ir_cache,
        acquire_cache=acquire_python_frontend_ir_cache,
        wait_cache=wait_python_frontend_ir_cache,
        publish_cache=publish_python_frontend_ir_cache,
        release_cache=release_python_frontend_ir_cache,
        host_python_command=_host_python_command,
        source_root=_pcc_source_root_for_host_subprocess,
        profile_begin=_profile_begin,
        profile_end=_profile_end,
        profile_counter=_profile_counter,
    )


def _python_frontend_action_dependencies(
    src_path: str,
    module_name: str,
    known_module_names,
) -> tuple[str, ...]:
    """Return the finite in-bundle import graph used by action invalidation."""

    # pcc1's current set projection can lose string members across this
    # compiled-module boundary (the same invariant is documented by
    # _mark_closed_world_function_object_exports).  Dependency metadata is a
    # correctness input, so keep this small closed-world table list-backed.
    known: list[str] = []
    for name in known_module_names:
        clean_name = str(name)
        if clean_name not in known:
            known.append(clean_name)
    dependencies: list[str] = []
    root_dir = _module_root_from_src(src_path, module_name)
    with open(src_path, "r", encoding="utf-8") as stream:
        source = stream.read()
    targets = _top_level_import_targets(
        root_dir,
        source,
        top_level_only=False,
    )
    targets.extend(
        _package_import_targets(
            src_path,
            module_name,
            root_dir=root_dir,
            top_level_only=False,
        )
    )
    for _target_path, target_name in targets:
        clean_name = str(target_name)
        if (
            clean_name == "pcc.llvm_capi.compat"
            and clean_name not in known
            and "pcc.llvm_capi.ir" in known
        ):
            clean_name = "pcc.llvm_capi.ir"
        if clean_name in known and clean_name != module_name:
            if clean_name not in dependencies:
                dependencies.append(clean_name)
    return tuple(sorted(dependencies))


def _build_python_frontend_action_state(
    src_paths,
    module_names,
    exports_path: str,
    action_cache_plan,
    module_dependency_map=None,
):
    """Build action state from the exact merged export wire consumed by codegen."""

    if action_cache_plan is None:
        return None
    required = (
        "compiler_digest",
        "runtime_abi_digest",
        "target",
        "options_digest",
        "action_root",
    )
    for field in required:
        if not str(action_cache_plan.get(field, "")):
            return None
    native_exports = _read_native_exports_wire_raw_modules(exports_path)
    modules = []
    for src_path, module_name in zip(src_paths, module_names):
        module_wire = native_exports.get(str(module_name), {})
        canonical_export = json.dumps(
            module_wire,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        summary = _module_action_dag.PublicSummary.create(
            exports=("native-export-wire:" + canonical_export,),
        )
        modules.append(
            _module_action_dag.ModuleState.create(
                str(module_name),
                _module_action_dag.source_digest(str(src_path)),
                (
                    tuple(module_dependency_map.get(module_name, ()))
                    if module_dependency_map is not None
                    else _python_frontend_action_dependencies(
                        str(src_path),
                        str(module_name),
                        module_names,
                    )
                ),
                summary,
            )
        )
    return _module_action_dag.GraphState.create(
        compiler_digest=str(action_cache_plan["compiler_digest"]),
        runtime_abi_digest=str(action_cache_plan["runtime_abi_digest"]),
        target=str(action_cache_plan["target"]),
        options_digest=str(action_cache_plan["options_digest"]),
        modules=modules,
    )


def _compile_python_multi_codegen_parallel_uncached(
    src_paths,
    module_names,
    *,
    jobs: int,
    entry_module: str,
    sibling_inits,
    libpython_mode: str,
    ir_scaffold_mode: str,
    verbose: bool,
    profile: Optional[dict] = None,
    action_cache_plan=None,
    artifact_dir: str = "",
) -> Optional[
    tuple[
        list[tuple[str, str]],
        bool,
        bool,
        int,
        list[str],
        Optional[list[tuple[str, str]]],
    ]
]:
    auto_source_lanes = not _pipeline_frontend_workers.numeric_jobs_override(
        str(os.environ.get(_PY_FRONTEND_JOBS_ENV, "") or "")
    )
    worker_prefix = _python_frontend_worker_command_prefix()
    module_dependency_map = None
    if (
        auto_source_lanes
        and len(worker_prefix) == 1
        and _is_native_worker_executable(worker_prefix[0])
    ):
        # The export workers already own one complete lifted AST each. They
        # populate this shared map from those ASTs inside build_shared_exports;
        # avoid a second textual import scan in the pcc1 coordinator.
        module_dependency_map = {}
    return _pipeline_frontend_parallel.compile_parallel_uncached(
        src_paths,
        module_names,
        jobs=jobs,
        entry_module=entry_module,
        sibling_inits=sibling_inits,
        libpython_mode=libpython_mode,
        ir_scaffold_mode=ir_scaffold_mode,
        verbose=verbose,
        profile=profile,
        action_cache_plan=action_cache_plan,
        artifact_dir=artifact_dir,
        can_spawn_worker=_can_spawn_python_frontend_worker,
        worker_command_prefix=_python_frontend_worker_command_prefix,
        chunk_count_for_workers=_python_frontend_codegen_chunk_count,
        codegen_chunks=_python_frontend_codegen_chunks,
        ast_wire_enabled=_python_frontend_ast_wire_enabled,
        build_shared_exports_callback=(
            _build_python_frontend_shared_exports_parallel
        ),
        write_manifest=_write_python_frontend_worker_manifest,
        shell_quote_arg=_shell_quote_arg,
        worker_arg=_PY_FRONTEND_WORKER_ARG,
        worker_env_prefix=_python_frontend_worker_env_prefix,
        join_strings=_join_strings,
        run_worker_commands=_run_python_frontend_worker_commands,
        profiled_gc_collect=_profiled_gc_collect,
        read_worker_ir=_read_python_frontend_worker_ir,
        profile_begin=_profile_begin,
        profile_end=_profile_end,
        profile_counter=_profile_counter,
        build_action_state=_build_python_frontend_action_state,
        module_dependency_map=module_dependency_map,
        pipeline_error=PyPipelineError,
        auto_source_lanes=auto_source_lanes,
        run_worker_manifest_in_process=run_python_multi_codegen_worker,
    )


def compile_python_multi(
    src_paths,
    out_path: str,
    *,
    verbose: bool = False,
    emit_llvm_only: bool = False,
    entry_module: Optional[str] = None,
    module_names=None,
    libpython_mode: Optional[str] = None,
    ir_scaffold_mode: Optional[str] = None,
    backend: Optional[str] = None,
    target_triple: Optional[str] = None,
    recursive_stdlib: bool = False,
    runtime_archive: Optional[str] = None,
    link_args: tuple[str, ...] = (),
    profile: Optional[dict] = None,
) -> None:
    if verbose:
        print(
            f"ENTRY: compile_python_multi(src_paths={src_paths}, out_path={out_path}, module_names={module_names})"
        )
    """Compile multiple ``.py`` files into a single native executable.

    This is the infrastructure step for #138.5 three-stage
    bootstrap. Each source file is parsed, type-inferred, and
    lowered to LLVM IR independently; the resulting ``.ll`` files
    are handed to clang together so that cross-module symbol
    references — declared as ``external`` by each module's codegen
    — are resolved at link time.

    Parameters
    ----------
    src_paths:
        Ordered list of ``.py`` files. The first entry provides
        the native executable's ``main`` entry point (the one
        that pcc synthesises to call top-level module code).
    module_names:
        Optional parallel list of dotted module names. Defaults
        to the filename stem. The names influence the
        ``user_<module>_<fn>`` symbol mangling so two files can
        define unrelated ``main`` functions without colliding.
    entry_module:
        Dotted module name whose top-level ``main()`` is the
        executable entry. Defaults to the first file's module.
    runtime_archive:
        Optional explicit native runtime archive. This is propagated from
        single-file compilation when package closure selects this path.
    link_args:
        Raw arguments appended only to the final native linker invocation.
    target_triple:
        Optional explicit target applied consistently to every emitted module.

    The multi-compile API **does not** yet rewrite cross-module
    imports to extern references — step 2 of the spike plan
    (``docs/plans/multi-file-compile-spike.md``). Until that
    lands, imports between passed source files still route through
    ``py_cpy_import`` and the link pulls libpython. Single-file
    callers should keep using :func:`compile_python`.
    """
    if not src_paths:
        raise PyPipelineError("compile_python_multi requires at least one source file")
    total_start = _profile_begin(profile)
    _profile_counter(profile, "multi_input_files", len(src_paths))
    libpython_mode = _resolve_libpython_mode(libpython_mode)
    ir_scaffold_mode = _resolve_ir_scaffold_mode(ir_scaffold_mode)
    src_paths = list(src_paths)
    if module_names is None:
        module_names = []
        for p in src_paths:
            module_names.append(_module_name_from_src(p))
    if len(module_names) != len(src_paths):
        raise PyPipelineError("module_names length must match src_paths length")

    src_paths, module_names = _prepare_multi_source_compile_closure(
        src_paths,
        list(module_names),
        recursive_stdlib=recursive_stdlib,
        ir_scaffold_mode=ir_scaffold_mode,
        profile=profile,
    )
    t = _profile_begin(profile)
    _validate_package_site_no_libpython_abi(
        src_paths,
        libpython_mode=libpython_mode,
    )
    _profile_end(profile, "validate_package_site_abi", t)
    _profile_counter(profile, "multi_files", len(src_paths))

    t = _profile_begin(profile)
    try:
        from .type_infer import infer_module as _infer_module
        from .codegen.layer1 import L1CodeGen as _L1CodeGen
    except ImportError as e:
        raise PyPipelineError(f"Python frontend module not available: {e}") from e
    _profile_end(profile, "frontend_imports", t)

    any_needs_libpython = False
    any_needs_native_extension_exports = False
    libpython_modules = []
    module_ir_texts = []
    module_assembly_texts = None
    module_assembly_paths = None
    module_native_object_paths = None
    module_direct_artifacts = None
    native_backend = None
    if not emit_llvm_only:
        native_backend = _resolve_native_backend(backend)
    emit_only_self_backend = (
        emit_llvm_only and _normalize_native_backend_name(backend) == "self"
    )
    reuse_export_ast = native_backend == "self" or emit_only_self_backend

    # Decide which module is the entry (emits ``@main``). Default:
    # first source file in the list.
    if entry_module is None:
        entry_module = module_names[0]
    if entry_module not in module_names:
        raise PyPipelineError(
            f"entry_module {entry_module!r} not among module_names " f"{module_names!r}"
        )
    # Sibling modules whose top-level code the entry must run before
    # its own body. Use dependency order instead of caller order so a
    # child module never initializes before its imported base module.
    t = _profile_begin(profile)
    sibling_inits = _order_module_inits(src_paths, module_names, entry_module)
    _profile_end(profile, "order_module_inits", t)
    _profile_counter(profile, "before_build_closed_world_context", len(src_paths))

    total_ir_bytes_before_passes = 0
    parallel_codegen_result = None
    direct_native_object_dir = ""
    frontend_jobs = _python_frontend_jobs_for_sources(src_paths)
    _profile_counter(profile, "multi_frontend_jobs", frontend_jobs)
    _profile_counter(
        profile,
        "multi_frontend_package_site_capped",
        (
            1
            if frontend_jobs < _python_frontend_jobs(len(src_paths))
            and _python_frontend_package_site_workload(src_paths)
            else 0
        ),
    )
    if native_backend == "self" or emit_only_self_backend:
        direct_emit = str(
            os.environ.get("PCC_DIRECT_INDEXED_KERNEL_EMIT", "") or ""
        ).strip().lower() in ("1", "true", "yes", "on")
        direct_native_object = str(
            os.environ.get("PCC_DIRECT_INDEXED_NATIVE_OBJECT", "1") or "1"
        ).strip().lower() not in ("0", "false", "no", "off")
        if direct_emit:
            direct_native_object_dir = _prepare_direct_native_object_dir(
                out_path
            )
        t = _profile_begin(profile)
        try:
            parallel_codegen_result = _compile_python_multi_codegen_parallel(
                src_paths,
                module_names,
                jobs=frontend_jobs,
                entry_module=entry_module,
                sibling_inits=sibling_inits,
                libpython_mode=libpython_mode,
                ir_scaffold_mode=ir_scaffold_mode,
                verbose=verbose,
                profile=profile,
                artifact_dir=direct_native_object_dir,
            )
        except Exception:
            _remove_direct_native_object_dir(direct_native_object_dir)
            raise
        _profile_end(profile, "multi_frontend_codegen_parallel", t)

    if parallel_codegen_result is not None:
        if (
            len(parallel_codegen_result) == 1
            and parallel_codegen_result[0] == "PCC_DEFERRED_FRONTEND_CODEGEN"
        ):
            _profile_counter(profile, "multi_frontend_codegen_deferred", 1)
            _profile_end(profile, "compile_python_multi_total", total_start)
            return
        if len(parallel_codegen_result) == 8:
            (
                module_ir_texts,
                any_needs_libpython,
                any_needs_native_extension_exports,
                total_ir_bytes_before_passes,
                libpython_modules,
                module_assembly_paths,
                module_native_object_paths,
                module_direct_artifacts,
            ) = parallel_codegen_result
        elif len(parallel_codegen_result) == 7:
            (
                module_ir_texts,
                any_needs_libpython,
                any_needs_native_extension_exports,
                total_ir_bytes_before_passes,
                libpython_modules,
                module_assembly_texts,
                module_native_object_paths,
            ) = parallel_codegen_result
        elif len(parallel_codegen_result) == 6:
            (
                module_ir_texts,
                any_needs_libpython,
                any_needs_native_extension_exports,
                total_ir_bytes_before_passes,
                libpython_modules,
                module_assembly_texts,
            ) = parallel_codegen_result
        else:
            (
                module_ir_texts,
                any_needs_libpython,
                any_needs_native_extension_exports,
                total_ir_bytes_before_passes,
                libpython_modules,
            ) = parallel_codegen_result
        # The pass driver returns a new batch even when passes are disabled.
        # Once unpacked, this transport result must not retain the original
        # IR batch through emission and the memory-intensive final link.
        parallel_codegen_result = None
    else:
        # Pre-pass: build the closed-world context shared by real multi-file
        # compiles and contextual per-module probes.
        t = _profile_begin(profile)
        parsed_modules, native_exports, derived_class_map = build_closed_world_context(
            src_paths, module_names, profile
        )
        _profile_end(profile, "build_closed_world_context", t)
        if not reuse_export_ast:
            parsed_modules = [None for _ in parsed_modules]
            from ..parse.py_lift import parse_and_lift as _parse_and_lift

        # Pre-pass 2 + codegen: reuse the AST produced by the export pass on
        # the self-backend bootstrap path. Other native paths keep the older
        # reparse boundary because compiled pcc_multi still relies on it to avoid
        # lifetime issues when frontend objects cross pcc-native containers.
        for src, mod_name, ast_mod in zip(src_paths, module_names, parsed_modules):
            if ast_mod is None:
                t = _profile_begin(profile)
                with open(src, "r", encoding="utf-8") as f:
                    source = f.read()
                ast_mod = _parse_and_lift(source, src, mod_name)
                _profile_end(profile, "multi_parse_and_lift", t, mod_name)
            if _module_imports_pcc_native_extension(
                ast_mod,
                native_modules=module_names,
                ir_scaffold_mode=ir_scaffold_mode,
            ):
                any_needs_native_extension_exports = True
            external_for_this = {}
            for k, v in native_exports.items():
                if k != mod_name:
                    external_for_this[k] = v
            if verbose:
                _log(verbose, "type_infer[" + mod_name + "]")
            t = _profile_begin(profile)
            try:
                typed_mod = _infer_module(
                    ast_mod,
                    external_exports=external_for_this,
                    derived_class_map=derived_class_map,
                    contextual_host_params=_contextual_host_params_for_module(
                        ast_mod,
                        mod_name,
                    ),
                )
            except Exception as exc:
                raise PyPipelineError(
                    "type_infer[" + mod_name + "]: " + str(exc)
                ) from exc
            _profile_end(profile, "multi_type_infer", t, mod_name)
            if verbose:
                _log(verbose, "codegen " + mod_name)
            try:
                codegen = _L1CodeGen(
                    typed_mod,
                    (libpython_mode == "on"),
                    ir_scaffold_mode,
                )
                codegen._strict_no_libpython = libpython_mode == "off"
                codegen._prefer_native_callable_values = libpython_mode == "off"
                codegen._module_source_path = os.path.abspath(src)
            except Exception as exc:
                raise PyPipelineError(
                    "codegen_init["
                    + mod_name
                    + "]: "
                    + type(exc).__name__
                    + ": "
                    + str(exc)
                ) from exc
            prep_step = "entry"
            try:
                is_entry = mod_name == entry_module
                prep_step = "skip_program_main"
                codegen._skip_program_main = not is_entry
                prep_step = "sibling_module_inits"
                codegen._sibling_module_inits = sibling_inits
                # Preserve the baseline native export registry and add cross-module
                # exports from other files, excluding this module to avoid
                # sibling self-reference during multi-file inference/linking.
                if _module_uses_default_native_exports(mod_name):
                    prep_step = "read_default_exports"
                    default_exports = codegen._native_module_exports
                    prep_step = "copy_default_exports"
                    codegen_exports = _copy_native_module_exports(default_exports)
                else:
                    codegen_exports = {}
                prep_step = "merge_closed_world_exports"
                for k, v in native_exports.items():
                    if k != mod_name:
                        codegen_exports[k] = v
                prep_step = "store_exports"
                codegen._native_module_exports = codegen_exports
                codegen._native_function_object_exports = (
                    _closed_world_function_object_exports(native_exports, mod_name)
                )
            except Exception as exc:
                raise PyPipelineError(
                    "codegen_prepare["
                    + mod_name
                    + "]: "
                    + prep_step
                    + ": "
                    + type(exc).__name__
                    + ": "
                    + str(exc)
                ) from exc
            if verbose:
                _log(verbose, "codegen[" + mod_name + "]")
            t = _profile_begin(profile)
            try:
                ir_text = codegen.generate(typed_mod)
                ir_text = str(ir_text)
            except Exception as exc:
                raise PyPipelineError(
                    "codegen[" + mod_name + "]: " + type(exc).__name__ + ": " + str(exc)
                ) from exc
            _profile_end(profile, "multi_codegen_layer1", t, mod_name)
            if libpython_mode == "off" and _ir_needs_libpython(ir_text):
                any_needs_libpython = _finalize_libpython_mode(
                    detected=True,
                    mode=libpython_mode,
                    context="multi-file compile",
                    reasons=[
                        "module "
                        + mod_name
                        + " generated IR still calls py_cpy_* helpers"
                    ],
                )
                if mod_name not in libpython_modules:
                    libpython_modules.append(mod_name)
            total_ir_bytes_before_passes += len(ir_text)
            module_ir_texts.append((mod_name, ir_text))
    if module_direct_artifacts is None and module_native_object_paths is None:
        _remove_direct_native_object_dir(direct_native_object_dir)
        direct_native_object_dir = ""
    if module_direct_artifacts is not None:
        if emit_llvm_only:
            raise PyPipelineError(
                "direct indexed artifact mode cannot satisfy emit_llvm_only"
            )
        if native_backend != "self":
            raise PyPipelineError(
                "direct indexed artifact mode requires the self backend"
            )
        if libpython_mode != "off" or any_needs_libpython:
            raise PyPipelineError(
                "direct indexed artifact mode requires no-libpython"
            )
        _reject_mixed_extension_object_models(
            needs_libpython=False,
            needs_native_extension_exports=any_needs_native_extension_exports,
        )
        if runtime_archive is not None:
            runtime = os.path.abspath(str(runtime_archive))
            if not os.path.isfile(runtime):
                raise PyPipelineError(
                    "explicit runtime archive not found: " + runtime
                )
        elif str(os.environ.get(_PY_RUNTIME_ARCHIVE_ENV, "") or "").strip():
            runtime = _ensure_runtime(verbose, needs_libpython=False)
        else:
            runtime = _ensure_runtime_without_direct_indexed_env(verbose)
        assembly_count = 0
        native_count = 0
        assembly_bytes = 0
        native_bytes = 0
        for _module_name, kind, artifact_path in module_direct_artifacts:
            if kind == "ASM":
                assembly_count += 1
                assembly_bytes += os.path.getsize(artifact_path)
            elif kind == "PCO":
                native_count += 1
                native_bytes += os.path.getsize(artifact_path)
            else:
                raise PyPipelineError(
                    "unknown direct indexed artifact kind: " + str(kind)
                )
        _profile_counter(profile, "multi_direct_assembly_modules", assembly_count)
        _profile_counter(profile, "multi_direct_assembly_bytes", assembly_bytes)
        _profile_counter(profile, "multi_direct_native_object_modules", native_count)
        _profile_counter(profile, "multi_direct_native_object_bytes", native_bytes)
        _profile_counter(
            profile,
            "multi_direct_ir_text_bytes",
            sum(len(ir_text) for _module_name, ir_text in module_ir_texts),
        )
        try:
            _link_with_self_backend_direct_artifacts(
                module_direct_artifacts,
                out_path,
                runtime,
                verbose,
                needs_libpython=False,
                needs_native_extension_exports=(
                    any_needs_native_extension_exports
                ),
                extra_link_args=tuple(link_args),
                profile=profile,
            )
        finally:
            if not str(
                os.environ.get("PCC_DEFER_SELF_LINK_PLAN", "") or ""
            ).strip():
                _remove_direct_native_object_dir(direct_native_object_dir)
        _profile_end(profile, "compile_python_multi_total", total_start)
        return
    if module_native_object_paths is not None:
        if emit_llvm_only:
            raise PyPipelineError(
                "direct indexed native-object mode cannot satisfy emit_llvm_only"
            )
        if native_backend != "self":
            raise PyPipelineError(
                "direct indexed native-object mode requires the self backend"
            )
        if libpython_mode != "off" or any_needs_libpython:
            raise PyPipelineError(
                "direct indexed native-object mode requires no-libpython"
            )
        _reject_mixed_extension_object_models(
            needs_libpython=False,
            needs_native_extension_exports=any_needs_native_extension_exports,
        )
        if runtime_archive is not None:
            runtime = os.path.abspath(str(runtime_archive))
            if not os.path.isfile(runtime):
                raise PyPipelineError(
                    "explicit runtime archive not found: " + runtime
                )
        elif str(os.environ.get(_PY_RUNTIME_ARCHIVE_ENV, "") or "").strip():
            runtime = _ensure_runtime(verbose, needs_libpython=False)
        else:
            runtime = _ensure_runtime_without_direct_indexed_env(verbose)
        _profile_counter(
            profile,
            "multi_direct_native_object_modules",
            len(module_native_object_paths),
        )
        _profile_counter(
            profile,
            "multi_direct_ir_text_bytes",
            sum(len(ir_text) for _module_name, ir_text in module_ir_texts),
        )
        native_object_bytes = 0
        for _module_name, native_object_path in module_native_object_paths:
            native_object_bytes += os.path.getsize(native_object_path)
        _profile_counter(
            profile,
            "multi_direct_native_object_bytes",
            native_object_bytes,
        )
        try:
            _link_with_self_backend_native_objects(
                module_native_object_paths,
                out_path,
                runtime,
                verbose,
                needs_libpython=False,
                needs_native_extension_exports=(
                    any_needs_native_extension_exports
                ),
                extra_link_args=tuple(link_args),
                profile=profile,
            )
        finally:
            _remove_direct_native_object_dir(direct_native_object_dir)
        _profile_end(profile, "compile_python_multi_total", total_start)
        return
    if module_assembly_texts is not None:
        if emit_llvm_only:
            raise PyPipelineError(
                "direct indexed assembly mode cannot satisfy emit_llvm_only"
            )
        if native_backend != "self":
            raise PyPipelineError(
                "direct indexed assembly mode requires the self backend"
            )
        if libpython_mode != "off":
            raise PyPipelineError(
                "direct indexed assembly mode currently requires no-libpython"
            )
        if any_needs_libpython:
            raise PyPipelineError(
                "direct indexed assembly worker reported a libpython dependency"
            )
        _reject_mixed_extension_object_models(
            needs_libpython=False,
            needs_native_extension_exports=(
                any_needs_native_extension_exports
            ),
        )
        if runtime_archive is not None:
            runtime = os.path.abspath(str(runtime_archive))
            if not os.path.isfile(runtime):
                raise PyPipelineError(
                    "explicit runtime archive not found: " + runtime
                )
        elif str(
            os.environ.get(_PY_RUNTIME_ARCHIVE_ENV, "") or ""
        ).strip():
            # A receipt-bound bootstrap supplies the validated archive through
            # PCC_RUNTIME_ARCHIVE.  Preserve the ordinary bundle/provenance
            # checks without entering the auto-build path whose nested compiler
            # must have direct-worker flags stripped.
            runtime = _ensure_runtime(verbose, needs_libpython=False)
        else:
            runtime = _ensure_runtime_without_direct_indexed_env(verbose)
        _profile_counter(
            profile,
            "multi_direct_assembly_modules",
            len(module_assembly_texts),
        )
        direct_assembly_bytes = 0
        for _module_name, assembly_text in module_assembly_texts:
            direct_assembly_bytes += len(assembly_text)
        _profile_counter(
            profile,
            "multi_direct_assembly_bytes",
            direct_assembly_bytes,
        )
        _link_with_self_backend_assembly_texts(
            module_assembly_texts,
            out_path,
            runtime,
            verbose,
            needs_libpython=False,
            needs_native_extension_exports=(
                any_needs_native_extension_exports
            ),
            extra_link_args=tuple(link_args),
            profile=profile,
        )
        _profile_end(profile, "compile_python_multi_total", total_start)
        return
    if target_triple is not None:
        module_ir_texts = [
            (name, _ir_text_with_target_triple(text, target_triple))
            for name, text in module_ir_texts
        ]
    _profile_counter(
        profile,
        "multi_ir_bytes_before_passes",
        total_ir_bytes_before_passes,
    )

    t = _profile_begin(profile)
    module_ir_texts = _apply_python_ir_pass_pipeline_many(
        module_ir_texts,
        verbose=verbose,
        default_raw=_default_python_ir_pass_raw_for_request(
            native_backend,
            emit_llvm_only=emit_llvm_only,
            backend=backend,
        ),
        strict_no_libpython=(libpython_mode == "off"),
    )
    _profile_end(profile, "python_ir_pass_pipeline_many", t)
    total_ir_bytes = 0
    for _mod_name, ir_text in module_ir_texts:
        total_ir_bytes += len(str(ir_text))
    _profile_counter(profile, "multi_ir_modules", len(module_ir_texts))
    _profile_counter(profile, "multi_ir_bytes", total_ir_bytes)
    t = _profile_begin(profile)
    for mod_name, ir_text in module_ir_texts:
        if _ir_needs_libpython(ir_text):
            any_needs_libpython = True
            if mod_name not in libpython_modules:
                libpython_modules.append(mod_name)
    _profile_end(profile, "libpython_scan", t)
    if libpython_mode != "off" and any_needs_libpython:
        module_ir_texts = [
            (name, _ensure_libpython_main_thread_init(text))
            for name, text in module_ir_texts
        ]

    if emit_llvm_only:
        # Concatenate all IR texts with a separator comment so the
        # output is still valid LLVM IR (each module's header lines
        # are duplicated but ``llvm-as`` tolerates redundant
        # target-triple / datalayout directives).
        combined = str(
            "\n\n".join(
                f"; ---- module: {name} ----\n{text}" for name, text in module_ir_texts
            )
        )
        out_path = str(out_path)
        if verbose:
            _log(
                verbose,
                "writing LLVM IR to "
                + out_path
                + " ("
                + str(len(combined))
                + " bytes)",
            )
        t = _profile_begin(profile)
        _write_utf8_text_file(out_path, combined)
        _profile_end(profile, "emit_ll_many_combined", t)
        _profile_end(profile, "compile_python_multi_total", total_start)
        return

    t = _profile_begin(profile)
    any_needs_libpython = _finalize_libpython_mode(
        detected=any_needs_libpython,
        mode=libpython_mode,
        context="multi-file compile",
        reasons=(
            ["modules: " + ", ".join(libpython_modules)] if libpython_modules else []
        ),
    )
    _reject_mixed_extension_object_models(
        needs_libpython=any_needs_libpython,
        needs_native_extension_exports=any_needs_native_extension_exports,
    )
    _profile_end(profile, "finalize_libpython_mode", t)
    if verbose:
        _log(verbose, "native backend: " + str(native_backend))

    t = _profile_begin(profile)
    if runtime_archive is not None:
        runtime = os.path.abspath(str(runtime_archive))
        if not os.path.isfile(runtime):
            raise PyPipelineError("explicit runtime archive not found: " + runtime)
    else:
        runtime = _ensure_runtime(
            verbose,
            needs_libpython=any_needs_libpython,
        )
    _profile_end(profile, "ensure_runtime", t)
    if native_backend == "self" and _self_backend_skip_ll_temp():
        total_bytes = 0
        for _mod_name, text in module_ir_texts:
            total_bytes = total_bytes + len(str(text))
        if verbose:
            for mod_name, text in module_ir_texts:
                _log(
                    verbose,
                    "passing LLVM IR text to self backend for "
                    + mod_name
                    + " ("
                    + str(len(str(text)))
                    + " bytes)",
                )
        _log(
            verbose,
            "self backend: linking "
            + str(len(module_ir_texts))
            + " LLVM IR text modules without pipeline .ll temp ("
            + str(total_bytes)
            + " bytes)",
        )
        self_backend_texts = []
        for _mod_name, text in module_ir_texts:
            self_backend_texts.append(text)
        # This branch returns after linking. Transfer the IR owners to the
        # emitter so their storage can be released before the linker starts.
        module_ir_texts.clear()
        text = ""
        ir_text = ""
        t = _profile_begin(profile)
        _link_with_self_backend_ir_texts(
            self_backend_texts,
            out_path,
            runtime,
            verbose,
            needs_libpython=any_needs_libpython,
            needs_native_extension_exports=any_needs_native_extension_exports,
            extra_link_args=tuple(link_args),
            profile=profile,
            consume_ir_texts=True,
        )
        _profile_end(profile, "link_self_backend_ir_texts", t)
        if verbose:
            _log(verbose, "wrote executable: " + out_path)
        _profile_end(profile, "compile_python_multi_total", total_start)
        return

    with tempfile.TemporaryDirectory(prefix="pcc_py_multi_") as tmp:
        ll_paths = []
        t = _profile_begin(profile)
        for mod_name, text in module_ir_texts:
            safe = mod_name.replace(".", "_").replace("-", "_")
            p = str(os.path.join(tmp, safe + ".ll"))
            text = str(text)
            if native_backend != "self":
                text = _clang_link_compatible_python_ir(text)
            if verbose:
                _log(
                    verbose,
                    "writing LLVM IR to " + p + " (" + str(len(text)) + " bytes)",
                )
            _write_utf8_text_file(p, text)
            ll_paths.append(p)
        _profile_end(profile, "emit_ll_many", t)
        t = _profile_begin(profile)
        _link_native(
            ll_paths,
            out_path,
            runtime,
            verbose,
            backend=native_backend,
            needs_libpython=any_needs_libpython,
            needs_native_extension_exports=any_needs_native_extension_exports,
            extra_link_args=tuple(link_args),
            profile=profile,
        )
        _profile_end(profile, "link_native", t)
    if verbose:
        _log(verbose, "wrote executable: " + out_path)
    _profile_end(profile, "compile_python_multi_total", total_start)
