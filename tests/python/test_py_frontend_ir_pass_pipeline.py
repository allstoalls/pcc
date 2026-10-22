from __future__ import annotations

import json
import os
import re
import subprocess

import pytest

from pcc.frontends.python import ir_pass_pipeline, pipeline, pipeline_pass_config


def test_default_python_ir_pass_manifest_is_versioned_and_bounded():
    assert (
        pipeline_pass_config.PYTHON_IR_PASS_DEFAULT_TIER_SCHEMA
        == "pcc.python-ir-default-tier.v1"
    )
    assert pipeline_pass_config.PYTHON_IR_PASS_DEFAULT_TIER == (
        "mem2reg",
        "sroa",
    )
    assert (
        len(pipeline_pass_config.PYTHON_IR_PASS_DEFAULT_TIER)
        <= pipeline_pass_config.PYTHON_IR_PASS_DEFAULT_TIER_MAX_PASSES
        <= 6
    )
    assert pipeline._PYTHON_IR_PASS_DEFAULT_TIER == (
        "mem2reg",
        "sroa",
    )


def test_python_frontend_jobs_defaults_to_auto(monkeypatch):
    monkeypatch.delenv("PCC_PY_FRONTEND_JOBS", raising=False)
    monkeypatch.delenv("PCC_OUTER_PARALLELISM", raising=False)
    monkeypatch.setattr(pipeline.os, "cpu_count", lambda: 12)

    assert pipeline._python_frontend_jobs(111) == 10


def test_profiled_gc_collect_records_current_process_reclamation(monkeypatch):
    profile = {}
    monkeypatch.setattr(pipeline.gc, "collect", lambda: 7)

    assert pipeline._profiled_gc_collect(profile, "current_process") == 7
    assert profile["counters"]["current_process_objects"] == 7
    assert profile["counters"]["current_process_skipped"] == 0
    assert profile["phase_totals_ms"]["current_process"] >= 0


def test_profiled_gc_collect_skips_subprocess_reclamation_boundary(monkeypatch):
    profile = {}

    def unexpected_collect():
        raise AssertionError("worker exit owns reclamation")

    monkeypatch.setattr(pipeline.gc, "collect", unexpected_collect)

    assert (
        pipeline._profiled_gc_collect(
            profile,
            "worker_boundary",
            allocations_owned_by_current_process=False,
        )
        == 0
    )
    assert profile["counters"]["worker_boundary_objects"] == 0
    assert profile["counters"]["worker_boundary_skipped"] == 1
    assert profile["phase_totals_ms"]["worker_boundary"] >= 0


def test_python_frontend_package_graph_auto_budget_caps_retained_heap(monkeypatch):
    monkeypatch.delenv("PCC_PY_FRONTEND_JOBS", raising=False)
    monkeypatch.delenv("PCC_OUTER_PARALLELISM", raising=False)
    monkeypatch.setattr(pipeline.os, "cpu_count", lambda: 12)
    monkeypatch.setattr(
        pipeline,
        "_package_site_package_root_for_src",
        lambda path: "/site/demo" if path.startswith("/site/") else None,
    )

    assert pipeline._python_frontend_jobs_for_sources(["/repo/main.py"] * 111) == 10
    assert (
        pipeline._python_frontend_jobs_for_sources(
            ["/repo/main.py"] * 110 + ["/site/demo/__init__.py"]
        )
        == 1
    )


def test_python_frontend_package_graph_explicit_jobs_remain_authoritative(monkeypatch):
    monkeypatch.setenv("PCC_PY_FRONTEND_JOBS", "5")
    monkeypatch.setattr(pipeline.os, "cpu_count", lambda: 12)
    monkeypatch.setattr(
        pipeline,
        "_package_site_package_root_for_src",
        lambda _path: "/site/demo",
    )

    assert pipeline._python_frontend_jobs_for_sources(["/site/demo/mod.py"] * 111) == 5


def test_python_frontend_package_graph_caps_outer_auto_budget(monkeypatch):
    monkeypatch.delenv("PCC_PY_FRONTEND_JOBS", raising=False)
    monkeypatch.setenv("PCC_OUTER_PARALLELISM", "6")
    monkeypatch.setattr(pipeline.os, "cpu_count", lambda: 12)
    monkeypatch.setattr(
        pipeline,
        "_package_site_package_root_for_src",
        lambda _path: "/site/demo",
    )

    assert pipeline._python_frontend_jobs_for_sources(["/site/demo/mod.py"] * 111) == 1


def test_python_frontend_jobs_env_can_force_serial(monkeypatch):
    monkeypatch.setenv("PCC_PY_FRONTEND_JOBS", "0")
    monkeypatch.setattr(pipeline.os, "cpu_count", lambda: 12)

    assert pipeline._python_frontend_jobs(111) == 1


def test_self_backend_jobs_defaults_to_bounded_pool(monkeypatch):
    monkeypatch.delenv("PCC_SELF_BACKEND_JOBS", raising=False)
    monkeypatch.delenv("PCC_OUTER_PARALLELISM", raising=False)
    monkeypatch.setattr(pipeline.os, "cpu_count", lambda: 12)

    # SELF_BACKEND_DEFAULT_JOBS is 8: the four-worker cap was DENIED with a
    # measured 52.8% wall regression on the frozen 32-item medium lane
    # (docs/goal/evidence/2026-08-21-stage2-medium-concurrency-cap-denied.md),
    # so the bounded default this asserts is min(n_modules, cpu, 8).
    assert pipeline._self_backend_jobs(111) == 8
    assert pipeline._self_backend_jobs(1) == 1


def test_self_backend_large_ir_defaults_bound_native_worker_memory(monkeypatch):
    monkeypatch.delenv("PCC_SELF_BACKEND_SPLIT_THRESHOLD_BYTES", raising=False)
    monkeypatch.delenv("PCC_SELF_BACKEND_SPLIT_SHARD_BYTES", raising=False)

    assert pipeline._self_backend_split_threshold_bytes() == 2_000_000
    assert pipeline._self_backend_split_shard_bytes() == 1_000_000


def test_self_backend_large_ir_memory_budget_remains_overridable(monkeypatch):
    monkeypatch.setenv("PCC_SELF_BACKEND_SPLIT_THRESHOLD_BYTES", "3000000")
    monkeypatch.setenv("PCC_SELF_BACKEND_SPLIT_SHARD_BYTES", "1500000")

    assert pipeline._self_backend_split_threshold_bytes() == 3_000_000
    assert pipeline._self_backend_split_shard_bytes() == 1_500_000


def test_compiled_self_backend_large_ir_bounds_concurrency(monkeypatch):
    monkeypatch.delenv("PCC_SELF_BACKEND_JOBS", raising=False)
    monkeypatch.delenv("PCC_OUTER_PARALLELISM", raising=False)
    monkeypatch.delenv("PCC_SELF_BACKEND_SPLIT_THRESHOLD_BYTES", raising=False)
    monkeypatch.setattr(pipeline.os, "cpu_count", lambda: 12)

    # Large inputs bound the lane to LARGE_INPUT_CONCURRENCY instead of
    # collapsing to one worker: the old any-large->1 rule serialized all 525
    # objects of a cold stage1 behind a single large module (35 s -> 434 s in
    # the emit phase; see the comment in pipeline_self_backend_config.jobs_for_
    # input_sizes).  Two inputs cap at min(2, LARGE_INPUT_CONCURRENCY).
    assert (
        pipeline._self_backend_jobs_for_ir_texts(
            ["x" * 2_000_000, "small"], native_worker=True
        )
        == 2
    )
    assert (
        pipeline._self_backend_jobs_for_ir_texts(
            ["x" * 1_999_999, "small"], native_worker=True
        )
        == 2
    )
    assert (
        pipeline._self_backend_jobs_for_ir_texts(
            ["x" * 2_000_000, "small"], native_worker=False
        )
        == 2
    )


def test_compiled_self_backend_explicit_jobs_override_large_ir_cap(monkeypatch):
    monkeypatch.setenv("PCC_SELF_BACKEND_JOBS", "2")

    assert (
        pipeline._self_backend_jobs_for_ir_texts(
            ["x" * 2_000_000, "small"], native_worker=True
        )
        == 2
    )


def test_nested_parallelism_shares_cpu_budget_across_outer_workers(monkeypatch):
    monkeypatch.delenv("PCC_PY_FRONTEND_JOBS", raising=False)
    monkeypatch.delenv("PCC_PYTHON_IR_PASS_JOBS", raising=False)
    monkeypatch.delenv("PCC_SELF_BACKEND_JOBS", raising=False)
    monkeypatch.setenv("PCC_OUTER_PARALLELISM", "6")
    monkeypatch.setattr(pipeline.os, "cpu_count", lambda: 12)

    assert pipeline._python_frontend_jobs(111) == 2
    assert pipeline._python_ir_pass_jobs(111) == 2
    assert pipeline._self_backend_jobs(111) == 2


def test_explicit_inner_jobs_override_outer_parallelism_budget(monkeypatch):
    monkeypatch.setenv("PCC_OUTER_PARALLELISM", "6")
    monkeypatch.setenv("PCC_PY_FRONTEND_JOBS", "5")
    monkeypatch.setenv("PCC_PYTHON_IR_PASS_JOBS", "4")
    monkeypatch.setenv("PCC_SELF_BACKEND_JOBS", "3")
    monkeypatch.setattr(pipeline.os, "cpu_count", lambda: 12)

    assert pipeline._python_frontend_jobs(111) == 5
    assert pipeline._python_ir_pass_jobs(111) == 4
    assert pipeline._self_backend_jobs(111) == 3


def test_python_frontend_worker_timing_is_opt_in(monkeypatch):
    monkeypatch.delenv("PCC_PY_FRONTEND_WORKER_TIMING", raising=False)

    assert pipeline._python_frontend_worker_timing_enabled() is False
    assert pipeline._python_frontend_worker_env_prefix() == "PCC_PY_FRONTEND_JOBS=1"

    monkeypatch.setenv("PCC_PY_FRONTEND_WORKER_TIMING", "1")

    assert pipeline._python_frontend_worker_timing_enabled() is True
    assert pipeline._python_frontend_worker_env_prefix() == (
        "PCC_PY_FRONTEND_JOBS=1 PCC_PY_FRONTEND_WORKER_TIMING=1"
    )


def test_python_frontend_native_workers_keep_one_module_per_process(tmp_path):
    native_worker = tmp_path / "pcc1"

    assert (
        pipeline._python_frontend_codegen_chunk_count(111, 10, [str(native_worker)])
        == 111
    )


def test_python_frontend_single_native_worker_still_isolates_each_module(tmp_path):
    native_worker = tmp_path / "pcc1"

    assert (
        pipeline._python_frontend_codegen_chunk_count(111, 1, [str(native_worker)])
        == 111
    )


def test_python_frontend_single_source_worker_bounds_retained_state():
    assert (
        pipeline._python_frontend_codegen_chunk_count(
            111,
            1,
            ["python", "-m", "pcc"],
        )
        == 4
    )


def test_python_frontend_source_workers_use_short_bounded_chunks():
    assert (
        pipeline._python_frontend_codegen_chunk_count(
            111,
            10,
            ["python", "-m", "pcc"],
        )
        == 40
    )


def test_python_frontend_worker_executable_skips_text_console_script(
    monkeypatch, tmp_path
                ):
    console_script = tmp_path / "pcc"
    console_script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    monkeypatch.setattr(pipeline.sys, "argv", [str(console_script)])
    monkeypatch.setattr(pipeline.sys, "executable", "")

    assert pipeline._python_frontend_worker_executable() == ""


def test_closed_world_shallow_lift_preserves_class_keywords(tmp_path):
    src = tmp_path / "mod.py"
    src.write_text(
        "class Base:\n"
        "    pass\n"
        "\n"
        "class Child(Base, total=False):\n"
        "    pass\n",
        encoding="utf-8",
    )

    parsed_modules, _native_exports, _derived = pipeline.build_closed_world_context(
        [str(src)],
        ["pkg.mod"],
        lift_indices=[],
        merge_exports=False,
    )

    child = parsed_modules[0].body[1]
    assert child.keywords[0][0] == "total"


def test_native_export_wire_preserves_expression_defaults(tmp_path):
    src = tmp_path / "mod.py"
    src.write_text(
        "import numpy as np\n"
        "\n"
        "def f(dtype=int, axis=-1, keepdims=np._NoValue):\n"
        "    pass\n",
        encoding="utf-8",
    )
    _parsed_modules, native_exports, derived = pipeline.build_closed_world_context(
        [str(src)],
        ["pkg.mod"],
        lift_indices=[],
        merge_exports=False,
    )
    path = tmp_path / "exports.json"

    pipeline._write_native_exports_wire(str(path), native_exports, derived)
    restored_exports, _restored_derived = pipeline._read_native_exports_wire(str(path))

    export = restored_exports["pkg.mod"]["f"]
    param_types = export["param_types"]
    assert isinstance(param_types, tuple)
    assert param_types[0] == ("dyn",)

    sig = export["call_sig"]
    assert isinstance(sig, tuple)
    assert sig[0]["has_default"] is True
    assert sig[0]["default"].ident == "int"
    assert sig[1]["has_default"] is True
    assert sig[1]["default"].op == "-"
    assert sig[2]["has_default"] is True
    assert sig[2]["default"].name == "_NoValue"


_DEAD_ADD_IR = """
define i32 @main() {
entry:
  %dead = add i32 1, 2
  ret i32 0
}
"""

_RUNTIME_CALL_IR = """
; ModuleID = "probe"
target triple = "unknown-unknown-unknown"

declare ptr @py_int_from_i64(i64)
declare void @py_print(ptr)

define i32 @main() {
entry:
  %v = call ptr @py_int_from_i64(i64 1)
  call void @py_print(ptr %v)
  ret i32 0
}
"""

_GLOBAL_STRING_BRANCH_IR = """
; ModuleID = "probe"
target triple = "unknown-unknown-unknown"

@.pystr.0 = internal constant [2 x i8] c"x\\00"

declare ptr @py_str_from_cstr(ptr)
declare void @py_print(ptr)

define i32 @main() {
entry:
  br i1 true, label %then, label %else
then:
  %p = getelementptr inbounds [2 x i8], ptr @.pystr.0, i32 0, i32 0
  %v = call ptr @py_str_from_cstr(ptr %p)
  call void @py_print(ptr %v)
  ret i32 0
else:
  ret i32 1
}
"""

_SIBLING_CALL_BRANCH_IR = """
; ModuleID = "probe"
target triple = "unknown-unknown-unknown"

define ptr @helper(ptr %x) {
entry:
  ret ptr %x
}

define i32 @main(ptr %arg) {
entry:
  br i1 true, label %then, label %else
then:
  %v = call ptr @helper(ptr %arg)
  ret i32 0
else:
  ret i32 1
}
"""

_INTERNAL_SIBLING_CALL_BRANCH_IR = """
; ModuleID = "probe"
target triple = "unknown-unknown-unknown"

define internal ptr @helper(ptr %x) {
entry:
  ret ptr %x
}

define i32 @main(ptr %arg) {
entry:
  br i1 true, label %then, label %else
then:
  %v = call ptr @helper(ptr %arg)
  ret i32 0
else:
  ret i32 1
}
"""

_OPT_DEFAULT_PIPELINE_IR = """
; ModuleID = "probe"
target triple = "unknown-unknown-unknown"

define i32 @main() {
entry:
  %p = alloca i32
  store i32 1, ptr %p
  %v = load i32, ptr %p
  %dead = add i32 %v, 0
  ret i32 %dead
}
"""


def test_python_ir_pass_pipeline_off_is_noop(monkeypatch):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")

    out = pipeline._apply_python_ir_pass_pipeline(
        _DEAD_ADD_IR,
        module_name="probe",
    )

    assert out == _DEAD_ADD_IR




def test_python_ir_pass_names_stay_list_for_bootstrap_joining():
    pass_names = pipeline._resolve_python_ir_pass_names("default")

    assert isinstance(pass_names, list)
    assert pipeline._join_strings(pass_names, ",") == "mem2reg,sroa"


def test_host_python_prefers_repo_venv(tmp_path, monkeypatch):
    monkeypatch.delenv("PCC_HOST_PYTHON", raising=False)
    source_root = tmp_path / "pcc-source"
    caller_root = tmp_path / "application"
    source_venv_bin = source_root / ".venv" / "bin"
    caller_venv_bin = caller_root / ".venv" / "bin"
    source_venv_bin.mkdir(parents=True)
    caller_venv_bin.mkdir(parents=True)
    host_py = source_venv_bin / "python3"
    host_py.write_text("#!/bin/sh\n", encoding="utf-8")
    (caller_venv_bin / "python3").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(
        pipeline,
        "_pcc_source_root_for_host_subprocess",
        lambda: str(source_root),
    )
    monkeypatch.chdir(caller_root)

    assert pipeline._host_python_command() == str(host_py)


def test_runtime_make_resolves_path_command_without_using_repo_relative_path(
    monkeypatch,
):
    monkeypatch.setattr(pipeline, "_host_python_command", lambda: "python3")
    monkeypatch.setattr(
        pipeline.shutil,
        "which",
        lambda command: "/usr/bin/python3" if command == "python3" else None,
    )

    assert pipeline._runtime_host_python_for_make() == "/usr/bin/python3"


def test_module_name_from_package_main(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    main = pkg / "__main__.py"
    main.write_text("print(1)\n", encoding="utf-8")

    assert pipeline._module_name_from_src(str(main)) == "pkg.__main__"


def test_python_ir_pass_pipeline_failure_is_not_empty_success(monkeypatch):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "not_a_real_pass")

    with pytest.raises(pipeline.PyPipelineError):
        pipeline._apply_python_ir_pass_pipeline(
            _DEAD_ADD_IR,
            module_name="probe",
        )




def test_python_ir_pass_pipeline_on_expands_to_fast_default(monkeypatch):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "on")

    assert pipeline._resolve_python_ir_pass_names() == [
        "mem2reg",
        "sroa",
    ]


def test_python_ir_pass_pipeline_all_stays_explicit_all(monkeypatch):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "all")

    assert pipeline._resolve_python_ir_pass_names() == ["all"]




























def test_python_ir_pass_memory_transport_rejects_bad_transport():
    with pytest.raises(ir_pass_pipeline.PythonIRPassError, match="TRANSPORT"):
        ir_pass_pipeline.resolve_python_ir_pass_transport("socket")


def test_python_ir_pass_pipeline_rejects_removed_external_loop_unroll():
    ir = """
define i32 @main(i32 %n) {
entry:
  br label %loop
loop:
  %i = phi i32 [ 0, %entry ], [ %inc, %loop ]
  %s = phi i32 [ 0, %entry ], [ %s2, %loop ]
  %s2 = add i32 %s, %i
  %inc = add i32 %i, 1
  %cmp = icmp slt i32 %inc, 3
  br i1 %cmp, label %loop, label %exit
exit:
  ret i32 %s2
}
"""

    with pytest.raises(ir_pass_pipeline.PythonIRPassError, match="unsupported owned IR pass.*loop-unroll"):
        ir_pass_pipeline.run_python_ir_pass_pipeline(ir, pass_names=("loop-unroll",), module_name="probe")


def test_python_ir_pass_pipeline_rejects_external_dse_and_promotes_owned_slot():
    ir = """
define i32 @main() {
entry:
  %p = alloca i32
  store i32 1, ptr %p
  store i32 2, ptr %p
  %v = load i32, ptr %p
  ret i32 %v
}
"""

    with pytest.raises(ir_pass_pipeline.PythonIRPassError, match="unsupported owned IR pass.*dse"):
        ir_pass_pipeline.run_python_ir_pass_pipeline(ir, pass_names=("dse",), module_name="probe")
    out = ir_pass_pipeline.run_python_ir_pass_pipeline(ir, pass_names=("mem2reg", "sroa"), module_name="probe")
    assert "alloca" not in out and "store" not in out
    assert "ret i32 2" in out


def test_python_ir_pass_pipeline_keeps_owned_dce_without_external_licm_budget(monkeypatch):
    monkeypatch.delenv("PCC_LICM_LOOP_BUDGET", raising=False)

    out = ir_pass_pipeline.run_python_ir_pass_pipeline(
        _DEAD_ADD_IR,
        pass_names=("dce",),
        module_name="probe",
    )

    assert "PCC_LICM_LOOP_BUDGET" not in os.environ
    assert "add i64" not in out




def test_owned_cfg_pass_is_not_skipped_by_retired_external_size_limit(monkeypatch):
    from pcc.frontends.python import compiled_owned_passes

    monkeypatch.setenv("PCC_PYTHON_IR_PASS_LARGE_MODULE_BYTES", "1")
    original = compiled_owned_passes.simplify_cfg_text
    calls = []

    def observed(text):
        calls.append(text)
        return original(text)

    monkeypatch.setattr(compiled_owned_passes, "simplify_cfg_text", observed)

    out = ir_pass_pipeline.run_python_ir_pass_pipeline(
        _GLOBAL_STRING_BRANCH_IR,
        pass_names=("simplifycfg",),
        module_name="probe",
    )

    assert calls == [_GLOBAL_STRING_BRANCH_IR]
    assert "define" in out




def test_python_ir_pass_telemetry_can_write_jsonl_file(
    tmp_path,
    monkeypatch,
    capsys,
):
    telemetry = tmp_path / "passes.jsonl"
    monkeypatch.setenv(
        "PCC_PYTHON_IR_PASS_TELEMETRY_PATH",
        str(telemetry),
    )

    ir_pass_pipeline.run_python_ir_pass_pipeline(
        _DEAD_ADD_IR,
        pass_names=("dce",),
        module_name="probe",
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    records = [
        json.loads(line) for line in telemetry.read_text(encoding="utf-8").splitlines()
    ]
    assert records[0]["event"] == "start"
    assert records[1]["pass"] == "dce"
    assert records[1]["status"] == "run"
    assert records[-1]["event"] == "end"






















def test_python_ir_pass_pipeline_timeout_is_bounded(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        raise subprocess.TimeoutExpired(command, timeout=0.5)

    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "dce")
    monkeypatch.setenv("PCC_PYTHON_IR_PASS_TIMEOUT", "0.5")
    monkeypatch.setattr(pipeline.subprocess, "run", fake_run)

    with pytest.raises(
        pipeline.PyPipelineError,
        match=(
            "Python IR pass pipeline timed out.*probe.*0.500s.*" "passes=dce.*ir_bytes="
        ),
    ):
        pipeline._apply_python_ir_pass_pipeline(
            _DEAD_ADD_IR,
            module_name="probe",
        )

    assert calls
    assert calls[0][1]["timeout"] == 0.5


def test_python_ir_pass_pipeline_many_timeout_is_bounded(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        raise subprocess.TimeoutExpired(command, timeout=0.25)

    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "dce")
    monkeypatch.setenv("PCC_PYTHON_IR_PASS_TIMEOUT", "0.25")
    monkeypatch.setattr(pipeline.subprocess, "run", fake_run)

    with pytest.raises(
        pipeline.PyPipelineError,
        match=(
            "Python IR pass batch pipeline timed out after 0.250s; "
            "modules=2 passes=dce total_bytes=.*largest=second:.*first:"
        ),
    ):
        pipeline._apply_python_ir_pass_pipeline_many(
            [
                ("first", _DEAD_ADD_IR),
                ("second", _DEAD_ADD_IR),
            ],
        )

    assert calls
    assert calls[0][1]["timeout"] == 0.25


def test_compile_python_emit_llvm_applies_python_ir_pass_pipeline(
    tmp_path,
    monkeypatch,
):
    src = tmp_path / "main.py"
    src.write_text("print(1)\n", encoding="utf-8")
    out = tmp_path / "main.ll"
    seen = []

    def fake_pipeline(
        ir_text,
        *,
        module_name,
        verbose=False,
        default_raw=None,
        strict_no_libpython=False,
    ):
        seen.append((module_name, verbose))
        return str(ir_text) + "\n; pass marker\n"

    monkeypatch.setattr(
        pipeline,
        "_apply_python_ir_pass_pipeline",
        fake_pipeline,
    )

    pipeline.compile_python(str(src), str(out), emit_llvm_only=True)

    assert seen == [("main", False)]
    assert "; pass marker" in out.read_text(encoding="utf-8")


def test_compile_python_link_args_reach_only_the_final_native_link(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(pipeline, "_explicit_runtime_archive", lambda archive, **_kwargs: archive)
    src = tmp_path / "main.py"
    src.write_text("print(1)\n", encoding="utf-8")
    runtime = tmp_path / "fake_runtime.a"
    runtime.write_bytes(b"")
    executable = tmp_path / "main.out"
    emitted_ir = tmp_path / "main.ll"
    link_calls = []
    requested = ("-Wl,-rpath,/tmp/pcc-link-arg", "/tmp/libhelper.a")

    def fake_link_native(
        ll_paths,
        out_path,
        runtime_archive,
        verbose,
        **kwargs,
    ):
        link_calls.append(tuple(kwargs.get("extra_link_args", ())))

    monkeypatch.setattr(pipeline, "_link_with_self_backend_ir_texts", fake_link_native)

    pipeline.compile_python(
        str(src),
        str(executable),
        runtime_archive=str(runtime),
        link_args=requested,
    )
    pipeline.compile_python(
        str(src),
        str(emitted_ir),
        emit_llvm_only=True,
        link_args=requested,
    )

    assert link_calls == [requested]
    assert emitted_ir.is_file()


def test_compile_python_package_link_args_reach_the_multi_file_link(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(pipeline, "_explicit_runtime_archive", lambda archive, **_kwargs: archive)
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "helper.py").write_text(
        "def value() -> int:\n"
        "    return 7\n",
        encoding="utf-8",
    )
    entry = package / "__main__.py"
    entry.write_text(
        "from .helper import value\n"
        "print(value())\n",
        encoding="utf-8",
    )
    runtime = tmp_path / "fake_runtime.a"
    runtime.write_bytes(b"")
    executable = tmp_path / "pkg.out"
    emitted_ir = tmp_path / "pkg.ll"
    link_calls = []
    requested = ("-Wl,-rpath,/tmp/pcc-package-link-arg", "/tmp/libpkghelper.a")

    def fake_link_native(
        ll_paths,
        out_path,
        runtime_archive,
        verbose,
        **kwargs,
    ):
        link_calls.append(tuple(kwargs.get("extra_link_args", ())))

    monkeypatch.setattr(pipeline, "_link_with_self_backend_ir_texts", fake_link_native)

    pipeline.compile_python(
        str(entry),
        str(executable),
        runtime_archive=str(runtime),
        link_args=requested,
    )
    pipeline.compile_python(
        str(entry),
        str(emitted_ir),
        emit_llvm_only=True,
        link_args=requested,
    )

    assert link_calls == [requested]
    assert emitted_ir.is_file()


def test_string_literals_emit_immortal_globals_not_py_str_new(tmp_path):
    src = tmp_path / "main.py"
    src.write_text('print("same")\nprint("same")\n', encoding="utf-8")
    out = tmp_path / "main.ll"

    pipeline.compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
    )

    text = out.read_text(encoding="utf-8")
    assert "call ptr @py_str_new" not in text
    from pcc.backend.self_backend_parse import decode_llvm_c_string

    initializers = re.findall(r'c"(?:[^"\\]|\\[0-9A-Fa-f]{2})*"', text)
    assert [decode_llvm_c_string(value) for value in initializers].count(b"same\0") == 1
    assert "i32 4, i32 1" in text


def test_compile_python_multi_batches_python_ir_pass_pipeline(
    tmp_path,
    monkeypatch,
):
    entry = tmp_path / "entry.py"
    helper = tmp_path / "helper.py"
    entry.write_text("print(1)\n", encoding="utf-8")
    helper.write_text("print(2)\n", encoding="utf-8")
    out = tmp_path / "combined.ll"
    seen = []

    def fail_per_module(*args, **kwargs):
        raise AssertionError("multi-file compile should use batch IR passes")

    def fake_pipeline_many(
        module_ir_texts,
        *,
        verbose=False,
        default_raw=None,
        strict_no_libpython=False,
    ):
        seen.append([name for name, _text in module_ir_texts])
        return [
            (name, str(text) + "\n; pass marker " + name + "\n")
            for name, text in module_ir_texts
        ]

    monkeypatch.setattr(
        pipeline,
        "_apply_python_ir_pass_pipeline",
        fail_per_module,
    )
    monkeypatch.setattr(
        pipeline,
        "_apply_python_ir_pass_pipeline_many",
        fake_pipeline_many,
        raising=False,
    )

    pipeline.compile_python_multi(
        [str(entry), str(helper)],
        str(out),
        emit_llvm_only=True,
        entry_module="entry",
        module_names=["entry", "helper"],
    )

    assert seen == [["entry", "helper"]]
    text = out.read_text(encoding="utf-8")
    assert "; pass marker entry" in text
    assert "; pass marker helper" in text


def test_compile_python_multi_strict_no_libpython_fails_after_first_fallback_module(
    tmp_path,
    monkeypatch,
):
    import pytest

    from pcc.frontends.python import type_infer
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.codegen import layer1

    monkeypatch.setenv("PCC_PY_FRONTEND_JOBS", "1")
    monkeypatch.setattr(
        pipeline, "_compile_python_multi_codegen_parallel", lambda *_args, **_kwargs: None
    )

    entry = tmp_path / "entry.py"
    helper = tmp_path / "helper.py"
    entry.write_text("print(1)\n", encoding="utf-8")
    helper.write_text("print(2)\n", encoding="utf-8")
    out = tmp_path / "combined.ll"
    generated = []

    class FakeCodeGen:
        def __init__(self, typed_mod, allow_libpython, ir_scaffold_mode):
            self._native_module_exports = {}

        def generate(self, typed_mod):
            generated.append(typed_mod)
            if len(generated) == 1:
                return (
                    "declare ptr @py_cpy_import(ptr)\n\n"
                    "define void @user_entry_main() {\n"
                    "  %m = call ptr @py_cpy_import(ptr null)\n"
                    "  ret void\n"
                    "}\n"
                )
            raise AssertionError("later modules should not be generated")

    def fake_build_closed_world_context(src_paths, module_names, profile):
        with open(src_paths[0], encoding="utf-8") as source:
            entry_ast = parse_and_lift(source.read(), src_paths[0], module_names[0])
        with open(src_paths[1], encoding="utf-8") as source:
            helper_ast = parse_and_lift(source.read(), src_paths[1], module_names[1])
        return [entry_ast, helper_ast], {}, {}

    def fake_infer_module(ast_mod, **_kwargs):
        return ast_mod

    def fail_pipeline_many(*_args, **_kwargs):
        raise AssertionError(
            "strict no-libpython fallback should fail before IR passes"
        )

    monkeypatch.setattr(
        pipeline,
        "_collect_multi_source_relative_closure",
        lambda srcs, mods, recursive_stdlib=False: (list(srcs), list(mods)),
    )
    monkeypatch.setattr(
        pipeline,
        "_filter_ir_scaffold_closure",
        lambda srcs, mods, ir_scaffold_mode=None: (list(srcs), list(mods)),
    )
    monkeypatch.setattr(
        pipeline,
        "_validate_package_site_no_libpython_abi",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(pipeline, "_order_module_inits", lambda *_args: [])
    monkeypatch.setattr(
        pipeline,
        "build_closed_world_context",
        fake_build_closed_world_context,
    )
    monkeypatch.setattr(
        pipeline,
        "_module_imports_pcc_native_extension",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        pipeline,
        "_contextual_host_params_for_module",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        pipeline,
        "_module_uses_default_native_exports",
        lambda _mod_name: False,
    )
    monkeypatch.setattr(
        pipeline,
        "_apply_python_ir_pass_pipeline_many",
        fail_pipeline_many,
    )
    monkeypatch.setattr(type_infer, "infer_module", fake_infer_module)
    monkeypatch.setattr(layer1, "L1CodeGen", FakeCodeGen)

    with pytest.raises(pipeline.PyPipelineError) as excinfo:
        pipeline.compile_python_multi(
            [str(entry), str(helper)],
            str(out),
            emit_llvm_only=True,
            entry_module="entry",
            module_names=["entry", "helper"],
            libpython_mode="off",
        )

    message = str(excinfo.value)
    assert "requires libpython fallback for multi-file compile" in message
    assert "module entry generated IR still calls py_cpy_* helpers" in message
    assert len(generated) == 1
    assert getattr(generated[0], "name", "") == "entry"


def test_compile_python_multi_reuses_export_pass_ast(tmp_path, monkeypatch):
    from pcc.frontends.python import py_lift

    entry = tmp_path / "entry.py"
    helper = tmp_path / "helper.py"
    entry.write_text("from .helper import value\nprint(value())\n", encoding="utf-8")
    helper.write_text("def value() -> int:\n    return 3\n", encoding="utf-8")
    out = tmp_path / "combined.ll"
    calls = []
    real_parse_and_lift = py_lift.parse_and_lift

    def counted_parse_and_lift(source, filename, module_name):
        calls.append(module_name)
        return real_parse_and_lift(source, filename, module_name)

    monkeypatch.setattr(py_lift, "parse_and_lift", counted_parse_and_lift)

    pipeline.compile_python_multi(
        [str(entry), str(helper)],
        str(out),
        emit_llvm_only=True,
        backend="self",
        module_names=["pkg.entry", "pkg.helper"],
        entry_module="pkg.entry",
    )

    assert calls == []


def test_parallel_frontend_codegen_uses_shared_export_context(tmp_path, monkeypatch):
    entry = tmp_path / "entry.py"
    helper = tmp_path / "helper.py"
    entry.write_text("def main() -> int:\n    return 0\n\nmain()\n", encoding="utf-8")
    helper.write_text("def value() -> int:\n    return 3\n", encoding="utf-8")
    out = tmp_path / "program.out"
    context_lift_indices = []
    seen_exports = []
    export_manifests = []
    codegen_manifests = []

    def fake_build_closed_world_context(
        src_paths,
        module_names,
        profile=None,
        lift_indices=None,
        merge_exports=True,
    ):
        context_lift_indices.append(tuple(lift_indices or ()))
        return [None for _src in src_paths], {"entry": {}, "helper": {}}, {}

    def fake_run_worker_commands(commands, max_parallel=None):
        for command in commands:
            manifest_path = command.split()[-1]
            manifest = pipeline._read_python_frontend_worker_manifest(manifest_path)
            if manifest["job_kind"] == "summary":
                assert pipeline._run_python_multi_summary_worker(manifest) == 0
                continue
            if manifest["job_kind"] == "export":
                export_manifests.append(manifest)
                # The production export worker also writes one AST wire per
                # assigned module (pipeline_frontend_worker_execution.py), and
                # the coordinator reads them back; a fake that skips this makes
                # the coordinator fail on a missing module_<index>.json.
                ast_dir = str(manifest.get("ast_dir", "") or "")
                if ast_dir:
                    from pcc.frontends.python.py_lift import parse_and_lift

                    for index in manifest["assigned_indices"]:
                        src_path = manifest["src_paths"][index]
                        with open(src_path, "r", encoding="utf-8") as f:
                            module_source = f.read()
                        ast_module = parse_and_lift(
                            module_source,
                            src_path,
                            manifest["module_names"][index],
                        )
                        pipeline._write_py_ast_wire(
                            os.path.join(ast_dir, f"module_{index}.json"),
                            ast_module,
                        )
                exports_path = os.path.join(
                    manifest["ir_dir"],
                    f"exports_{len(export_manifests)}.json",
                )
                edges_path = os.path.join(
                    manifest["ir_dir"],
                    f"edges_{len(export_manifests)}.json",
                )
                pipeline._write_native_exports_wire(
                    exports_path,
                    {"entry": {}, "helper": {}},
                    {},
                )
                pipeline._write_reexport_edges_wire(
                    edges_path,
                    (),
                    module_dependencies=tuple(
                        (manifest["module_names"][index], ())
                        for index in manifest["assigned_indices"]
                    ),
                )
                with open(manifest["result_path"], "w", encoding="utf-8") as f:
                    f.write("EXPORT\t" + exports_path + "\t" + edges_path + "\n")
                continue
            codegen_manifests.append(manifest)
            assert manifest["exports_path"]
            native_exports, derived_class_map = pipeline._read_native_exports_wire(
                manifest["exports_path"]
            )
            seen_exports.append((native_exports, derived_class_map))
            result_lines = []
            for index in manifest["assigned_indices"]:
                mod_name = manifest["module_names"][index]
                ir_path = os.path.join(manifest["ir_dir"], f"module_{index}.ll")
                with open(ir_path, "w", encoding="utf-8") as f:
                    f.write(f"; module {mod_name}\n")
                result_lines.append(
                    "OK\t"
                    + str(index)
                    + "\t"
                    + mod_name
                    + "\t0\t0\t"
                    + str(len(mod_name))
                    + "\t"
                    + ir_path
                )
            with open(manifest["result_path"], "w", encoding="utf-8") as f:
                for line in result_lines:
                    f.write(line + "\n")

    monkeypatch.setattr(pipeline, "_python_frontend_jobs", lambda _n: 2)
    monkeypatch.setattr(pipeline, "_can_spawn_python_frontend_worker", lambda: True)
    monkeypatch.setattr(
        pipeline,
        "_python_frontend_worker_command_prefix",
        lambda: ["/tmp/pcc-fake-worker"],
    )
    monkeypatch.setattr(
        pipeline,
        "build_closed_world_context",
        fake_build_closed_world_context,
    )
    monkeypatch.setattr(
        pipeline,
        "_run_python_frontend_worker_commands",
        fake_run_worker_commands,
    )
    monkeypatch.setattr(
        pipeline,
        "_apply_python_ir_pass_pipeline_many",
        lambda module_ir_texts, **_kwargs: module_ir_texts,
    )
    monkeypatch.setattr(
        pipeline,
        "_ensure_runtime",
        lambda verbose, *, needs_libpython=False, target_triple=None: "/tmp/fake_runtime.a",
    )
    monkeypatch.setattr(
        pipeline,
        "_link_with_self_backend_ir_texts",
        lambda ir_texts, out_path, runtime_archive, verbose, **_kwargs: out.write_text(
            "linked", encoding="utf-8"
        ),
    )

    pipeline.compile_python_multi(
        [str(entry), str(helper)],
        str(out),
        backend="self",
        module_names=["entry", "helper"],
        entry_module="entry",
    )

    assert context_lift_indices == []
    assert export_manifests
    assert codegen_manifests
    assert seen_exports
    assert all(exports == {"entry": {}, "helper": {}} for exports, _ in seen_exports)
    assert out.read_text(encoding="utf-8") == "linked"


def test_self_backend_native_compile_defaults_to_bounded_python_ir_pass_manifest(
    tmp_path,
    monkeypatch,
):
    entry = tmp_path / "entry.py"
    helper = tmp_path / "helper.py"
    entry.write_text("from .helper import value\nprint(value())\n", encoding="utf-8")
    helper.write_text("def value() -> int:\n    return 3\n", encoding="utf-8")
    out = tmp_path / "program.out"
    defaults = []

    def fake_pipeline_many(
        module_ir_texts,
        *,
        verbose=False,
        default_raw=None,
        strict_no_libpython=False,
    ):
        defaults.append(default_raw)
        return [(name, str(text)) for name, text in module_ir_texts]

    monkeypatch.delenv("PCC_PYTHON_IR_PASSES", raising=False)
    monkeypatch.setattr(
        pipeline,
        "_apply_python_ir_pass_pipeline_many",
        fake_pipeline_many,
        raising=False,
    )
    monkeypatch.setattr(
        pipeline,
        "_ensure_runtime",
        lambda verbose, *, needs_libpython=False, target_triple=None: "/tmp/fake_runtime.a",
    )
    monkeypatch.setattr(
        pipeline,
        "_link_native",
        lambda ll_paths, out_path, runtime_archive, verbose, *, backend, needs_libpython=False: out.write_text(
            "linked", encoding="utf-8"
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "_link_with_self_backend_ir_texts",
        lambda ir_texts, out_path, runtime_archive, verbose, **_kwargs: out.write_text(
            "linked", encoding="utf-8"
        ),
    )

    pipeline.compile_python_multi(
        [str(entry), str(helper)],
        str(out),
        backend="self",
        module_names=["pkg.entry", "pkg.helper"],
        entry_module="pkg.entry",
    )

    assert defaults == ["default"]


def test_self_backend_emit_llvm_defaults_to_bounded_python_ir_pass_manifest(
    tmp_path,
    monkeypatch,
):
    entry = tmp_path / "entry.py"
    helper = tmp_path / "helper.py"
    entry.write_text("from .helper import value\nprint(value())\n", encoding="utf-8")
    helper.write_text("def value() -> int:\n    return 3\n", encoding="utf-8")
    out = tmp_path / "combined.ll"
    defaults = []

    def fake_pipeline_many(
        module_ir_texts,
        *,
        verbose=False,
        default_raw=None,
        strict_no_libpython=False,
    ):
        defaults.append(default_raw)
        return [(name, str(text)) for name, text in module_ir_texts]

    monkeypatch.delenv("PCC_PYTHON_IR_PASSES", raising=False)
    monkeypatch.setattr(
        pipeline,
        "_apply_python_ir_pass_pipeline_many",
        fake_pipeline_many,
        raising=False,
    )

    pipeline.compile_python_multi(
        [str(entry), str(helper)],
        str(out),
        emit_llvm_only=True,
        backend="self",
        module_names=["pkg.entry", "pkg.helper"],
        entry_module="pkg.entry",
    )

    assert defaults == ["default"]
    assert out.exists()


def test_explicit_python_ir_pass_env_overrides_self_backend_default(monkeypatch):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "dce")

    assert pipeline._resolve_python_ir_pass_names(default_raw="off") == ["dce"]
