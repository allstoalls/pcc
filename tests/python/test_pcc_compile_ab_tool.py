from __future__ import annotations

import importlib.util
import difflib
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "run_pcc_compile_ab.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("run_pcc_compile_ab", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(wall, cpu, instructions, cycles, rss, footprint):
    return {
        "metrics": {
            "wall_s": wall,
            "cpu_s": cpu,
            "instructions": instructions,
            "cycles": cycles,
            "max_rss_bytes": rss,
            "peak_footprint_bytes": footprint,
        }
    }


def _write_build_evidence(
    tool, tmp_path, compilers, runtime, *, shared_logical_source_root=True
):
    canonical_source = tmp_path / "canonical-source"
    canonical_build_root = tmp_path / "canonical-stage-build"

    def populate_source(root, primary_text):
        (root / "pcc" / "ir").mkdir(parents=True)
        (root / "scripts").mkdir()
        (root / "utils" / "fake_libc_include").mkdir(parents=True)
        (root / tool.PRIMARY_SOURCE).write_text(primary_text, encoding="utf-8")
        (root / "pcc" / "driver" / "cli_core.py").write_text("common = 1\n", encoding="utf-8")
        (root / "pcc" / "__main__.py").write_text("main = 1\n", encoding="utf-8")
        (root / "AGENTS.md").write_text("# frozen\n", encoding="utf-8")
        (root / "pyproject.toml").write_text("[project]\nname='pcc'\n", encoding="utf-8")
        (root / "scripts" / "bootstrap.py").write_text(
            "# bootstrap\n", encoding="utf-8"
        )
        (root / "scripts" / "run_pcc_native_deferred.py").write_text(
            "# native deferred wrapper\n", encoding="utf-8"
        )
        (root / "scripts" / "run_pcc_deferred_link.py").write_text(
            "# deferred link\n", encoding="utf-8"
        )
        (root / "scripts" / "pcc_link_macho.py").write_text("# macho\n", encoding="utf-8")
        (root / "scripts" / "pcc_link_elf.py").write_text("# elf\n", encoding="utf-8")
        (root / "utils" / "fake_libc_include" / "Python.h").write_text(
            "/* fake */\n", encoding="utf-8"
        )

    populate_source(canonical_source, "exact = True\n")
    receipts = []
    snapshot_roots = {}
    runtime_portable = tool._portable_file_receipt(tool._path_receipt(runtime), "test")
    runtime_bundle = {
        "files": {
            runtime.name: {
                "sha256": runtime_portable["sha256"],
                "size_bytes": runtime_portable["size_bytes"],
            }
        },
        "provider": runtime_portable,
        "ar": runtime_portable,
        "manifest_target": "test",
        "manifest_member_count": 1,
        "wheel_target": "test",
        "object_emitter": "test-object-emitter",
        "codegen_checksum": "f" * 64,
        "producer_claim": "binary-integrity-only; producer source closure not proven",
    }
    external_tools = [runtime_portable]
    host_python_runtime = {
        "schema": "pcc.host-python-runtime.v1",
        "test_identity": "same-host-runtime",
    }
    for arm, compiler, primary_text in zip(
        ("candidate", "baseline"),
        compilers,
        ("exact = True\n", "exact = False\n"),
        strict=True,
    ):
        build_root = tmp_path / f"{arm}-build"
        build_root.mkdir()
        logical_build_root = (
            canonical_build_root if shared_logical_source_root else build_root
        )
        built_compiler = logical_build_root / "pcc1"
        logical_source_root = logical_build_root / "source-snapshot"
        source_root = build_root / "source-snapshot"
        populate_source(source_root, primary_text)
        snapshot_roots[arm] = source_root
        files = {
            path.relative_to(source_root).as_posix(): tool.sha256_path(path)
            for path in tool.build_source_files(source_root)
        }
        source_manifest = build_root / "source-manifest.json"
        source_manifest.write_text(
            json.dumps(
                {
                    "schema": tool.SOURCE_MANIFEST_SCHEMA,
                    "bootstrap_source_sha256": tool._source_manifest_identity(files),
                    "files": files,
                }
            ),
            encoding="utf-8",
        )
        time_output = build_root / "stage1.time"
        time_output.write_text(
            "real 1.0\nuser 0.8\nsys 0.1\n"
            "100 maximum resident set size\n100 instructions retired\n"
            "100 cycles elapsed\n100 peak memory footprint\n",
            encoding="utf-8",
        )
        (build_root / "stage1.profile.json").write_text("{}\n", encoding="utf-8")
        (build_root / "stage1.stdout").write_text("", encoding="utf-8")
        (build_root / "stage1.stderr").write_text("", encoding="utf-8")
        producer_dir = build_root / "producer-tools"
        producer_dir.mkdir()
        producer_tools = {}
        for name in ("run_pcc_stage1_build.py", "run_pcc_compile_ab.py"):
            producer = producer_dir / name
            producer.write_text("# same producer\n", encoding="utf-8")
            producer_tools[name] = {
                "path": "producer-tools/" + name,
                "sha256": tool.sha256_path(producer),
                "size_bytes": producer.stat().st_size,
            }
        stage_result = build_root / "stage1-result.json"
        artifacts = {}
        for name, relative in (
            ("time", "stage1.time"),
            ("profile", "stage1.profile.json"),
            ("stdout", "stage1.stdout"),
            ("stderr", "stage1.stderr"),
        ):
            artifact = build_root / relative
            artifacts[name] = {
                "path": relative,
                "sha256": tool.sha256_path(artifact),
                "size_bytes": artifact.stat().st_size,
            }
        stage_result.write_text(
            json.dumps(
                {
                    "schema": "pcc.stage1-build-result.v1",
                    "returncode": 0,
                    "compiler": str(built_compiler),
                    "compiler_sha256": tool.sha256_path(compiler),
                    "compiler_size_bytes": compiler.stat().st_size,
                    "metrics": tool.parse_time_output(time_output.read_text(encoding="utf-8")),
                    "profile_sha256": tool.sha256_path(
                        build_root / "stage1.profile.json"
                    ),
                    "stdout_sha256": tool.sha256_path(build_root / "stage1.stdout"),
                    "stderr_sha256": tool.sha256_path(build_root / "stage1.stderr"),
                    "linkage": {
                        "checked": True,
                        "links_libpython": False,
                        "links_llvm": False,
                        "stdout": "strict",
                    },
                    "artifacts": artifacts,
                }
            ),
            encoding="utf-8",
        )
        command = [
            str(tool.TIME_BINARY),
            "-lp",
            "-o",
            str(logical_build_root / "stage1.time"),
            sys.executable,
            "-m",
            "pcc",
            "--profile-json",
            str(logical_build_root / "stage1.profile.json"),
            "--ir-scaffold=on",
            "--backend",
            "self",
            "--python-libpython",
            "off",
            str(logical_source_root / "pcc" / "__main__.py"),
            "-o",
            str(built_compiler),
        ]
        environment = {
            "PCC_RUNTIME_ARCHIVE": str(runtime),
            "PCC_SOURCE_ROOT": str(logical_source_root),
            "PCC_REPO_ROOT": str(logical_source_root),
            "PCC_HOST_PYTHON": sys.executable,
            "PCC_GC_BACKEND": "0",
            "PCC_RUNTIME_HIGH": "py",
            "PCC_SELF_LINK": "pcc",
            "PCC_SELF_BACKEND_PUBLISH_SYNC": "1",
            "PCC_PYTHON_IR_PASSES": "off",
            "PCC_PYTHON_IR_PASS_JOBS": "1",
            "PCC_PY_FRONTEND_IR_CACHE": "0",
            "PCC_SELF_BACKEND_OBJECT_CACHE": "0",
            "PCC_DEBUG_IR_CALL": "0",
            "PCC_DEBUG_IR_RENDER": "0",
            "PYTHONHASHSEED": "0",
            "PCC_PY_FRONTEND_JOBS": "10",
            "PCC_BOOTSTRAP_STAGE1_PY_FRONTEND_JOBS": "10",
            "PCC_SELF_BACKEND_JOBS": "8",
            "PCC_MACHO_LINK_JOBS": "8",
            "HOME": str(logical_build_root / "private-state" / "home"),
        }
        receipt = build_root / "build-receipt.json"
        receipt.write_text(
            json.dumps(
                {
                    "schema": tool.BUILD_RECEIPT_SCHEMA,
                    "arm": arm,
                    "status": "SUCCEEDED",
                    "compiler_sha256": tool.sha256_path(compiler),
                    "compiler_size_bytes": compiler.stat().st_size,
                    "runtime_archive_sha256": tool.sha256_path(runtime),
                    "bootstrap_source_sha256": tool._source_manifest_identity(files),
                    "primary_source_sha256": files[tool.PRIMARY_SOURCE],
                    "origin_source_root": str(canonical_source),
                    "logical_source_root": str(logical_source_root),
                    "source_manifest": source_manifest.name,
                    "source_manifest_sha256": tool.sha256_path(source_manifest),
                    "source_snapshot": source_root.name,
                    "command": command,
                    "command_sha256": tool._canonical_sha256(command),
                    "environment": environment,
                    "environment_sha256": tool._canonical_sha256(environment),
                    "cwd": str(logical_source_root),
                    "stage_result": stage_result.name,
                    "stage_result_sha256": tool.sha256_path(stage_result),
                    "runtime_bundle": runtime_bundle,
                    "runtime_bundle_sha256": tool._canonical_sha256(runtime_bundle),
                    "external_tools": external_tools,
                    "external_tools_sha256": tool._canonical_sha256(external_tools),
                    "producer_tools": producer_tools,
                    "producer_tools_sha256": tool._canonical_sha256(producer_tools),
                    "host_python_runtime": host_python_runtime,
                    "host_python_runtime_sha256": tool._canonical_sha256(
                        host_python_runtime
                    ),
                }
            ),
            encoding="utf-8",
        )
        tool._seal_source_snapshot(source_root, arm + " fixture source snapshot")
        receipts.append(receipt)
    source_diff = tmp_path / "candidate-vs-baseline.diff"
    baseline_text = (snapshot_roots["baseline"] / tool.PRIMARY_SOURCE).read_text(
        encoding="utf-8"
    )
    candidate_text = (snapshot_roots["candidate"] / tool.PRIMARY_SOURCE).read_text(
        encoding="utf-8"
    )
    source_diff.write_text(
        "".join(
            difflib.unified_diff(
                baseline_text.splitlines(keepends=True),
                candidate_text.splitlines(keepends=True),
                fromfile="baseline/" + tool.PRIMARY_SOURCE,
                tofile="candidate/" + tool.PRIMARY_SOURCE,
                lineterm="\n",
            )
        ),
        encoding="utf-8",
    )
    return receipts, source_diff


def _rewrite_stage_result(tool, receipt_path, mutate):
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    stage_result_path = receipt_path.parent / receipt["stage_result"]
    stage_result = json.loads(stage_result_path.read_text(encoding="utf-8"))
    mutate(stage_result)
    stage_result_path.write_text(json.dumps(stage_result), encoding="utf-8")
    receipt["stage_result_sha256"] = tool.sha256_path(stage_result_path)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")


def test_time_parser_uses_labels_not_line_positions():
    tool = _load_tool()
    metrics = tool.parse_time_output(
        "warning before metrics\n"
        "real 41.69\n"
        "user 39.02\n"
        "sys 1.82\n"
        "          3568123904  maximum resident set size\n"
        "         39468301366  instructions retired\n"
        "         10749673296  cycles elapsed\n"
        "           281166520  peak memory footprint\n"
    )
    assert metrics == {
        "wall_s": 41.69,
        "user_s": 39.02,
        "system_s": 1.82,
        "max_rss_bytes": 3_568_123_904,
        "instructions": 39_468_301_366,
        "cycles": 10_749_673_296,
        "peak_footprint_bytes": 281_166_520,
        "cpu_s": pytest.approx(40.84),
    }
    with pytest.raises(tool.CompileABError, match="expected one wall_s, found 2"):
        tool.parse_time_output(
            "real 1.0\nreal 2.0\nuser 1.0\nsys 1.0\n"
            "1 maximum resident set size\n1 instructions retired\n"
            "1 cycles elapsed\n1 peak memory footprint\n"
        )


def test_measurement_environment_drops_ambient_pcc_state(monkeypatch, tmp_path):
    tool = _load_tool()
    monkeypatch.setenv("PCC_FORCE_GENERIC_IR_CALLS", "1")
    monkeypatch.setenv("PCC_BOOTSTRAP_PROFILE_DIR", "/should/not/leak")
    monkeypatch.setenv("PCC_GC_BACKEND", "4")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", "/should/not/leak/pycache")
    host_root = tmp_path / "host-source"
    host_root.mkdir()
    env = tool._measurement_env(
        tmp_path / "runtime.a",
        0,
        host_source_root=host_root,
        host_python=Path(sys.executable),
        private_root=tmp_path / "private",
        frontend_jobs=10,
        self_backend_jobs=8,
    )
    assert env["PCC_GC_BACKEND"] == "0"
    assert env["PCC_PY_FRONTEND_IR_CACHE"] == "0"
    assert env["PCC_SELF_BACKEND_OBJECT_CACHE"] == "0"
    assert "PCC_FORCE_GENERIC_IR_CALLS" not in env
    assert "PCC_BOOTSTRAP_PROFILE_DIR" not in env
    assert env["PCC_SOURCE_ROOT"] == str(host_root)
    assert env["PCC_HOST_PYTHON"] == str(Path(sys.executable))
    assert env["HOME"].startswith(str(tmp_path))
    assert env["PYTHONHASHSEED"] == "0"
    assert "PYTHONDONTWRITEBYTECODE" not in env
    assert Path(env["PYTHONPYCACHEPREFIX"]) == tmp_path / "private" / "pycache"
    assert Path(env["PYTHONPYCACHEPREFIX"]).is_dir()
    assert Path(env["PCC_RUNTIME_CC"]).is_absolute()
    assert env["PCC_RUNTIME_CC"] == "/usr/bin/false"
    assert env["PCC_SELF_LINK"] == "pcc"
    assert env["PCC_SELF_BACKEND_PUBLISH_SYNC"] == "1"
    assert env["PCC_PY_FRONTEND_JOBS"] == "10"
    assert env["PCC_SELF_BACKEND_JOBS"] == "8"
    assert env["PCC_MACHO_LINK_JOBS"] == "8"


def test_measurement_environment_rejects_relative_path(monkeypatch, tmp_path):
    tool = _load_tool()
    monkeypatch.setenv("PATH", ".:/usr/bin")
    with pytest.raises(tool.CompileABError, match="absolute directories"):
        tool._measurement_env(
            tmp_path / "runtime.a",
            0,
            host_source_root=tmp_path / "host-source",
            host_python=Path(sys.executable),
            private_root=tmp_path / "private",
            frontend_jobs=10,
            self_backend_jobs=8,
        )


def test_host_source_closure_contains_link_and_fake_libc_owners():
    tool = _load_tool()
    names = {
        path.relative_to(tool.REPO_ROOT).as_posix()
        for path in tool.build_source_files(tool.REPO_ROOT)
    }
    assert {
        "AGENTS.md",
        "scripts/run_pcc_deferred_link.py",
        "scripts/run_pcc_native_deferred.py",
        "scripts/pcc_link_macho.py",
        "scripts/pcc_link_elf.py",
        "utils/fake_libc_include/Python.h",
    } <= names


def test_host_python_runtime_tree_identity_changes_with_module_bytes(tmp_path):
    tool = _load_tool()
    runtime_root = tmp_path / "stdlib"
    runtime_root.mkdir()
    module = runtime_root / "worker.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    before = tool._runtime_tree_evidence(runtime_root)
    module.write_text("VALUE = 2\n", encoding="utf-8")
    after = tool._runtime_tree_evidence(runtime_root)
    assert before["file_count"] == after["file_count"] == 1
    assert before["content_sha256"] != after["content_sha256"]


@pytest.mark.parametrize(
    ("dependency", "owner"),
    (
        ("/usr/local/lib/libpython3.13.dylib", "libpython"),
        ("/Library/Frameworks/Python.framework/Versions/3.13/Python", "libpython"),
        ("/usr/local/lib/libLLVM.dylib", "LLVM"),
    ),
)
def test_linkage_rejects_library_and_framework_python(
    dependency, owner, tmp_path, monkeypatch
):
    tool = _load_tool()
    otool = tmp_path / "otool"
    otool.write_text("", encoding="utf-8")
    monkeypatch.setattr(tool, "OTOOL_BINARY", otool)
    monkeypatch.setattr(
        tool,
        "_run_process",
        lambda command, *, timeout, env, cwd: tool.subprocess.CompletedProcess(
            command, 0, "probe:\n\t" + dependency + " (compatibility 1.0.0)\n", ""
        ),
    )
    with pytest.raises(tool.CompileABError, match="forbidden " + owner):
        tool._linkage(
            tmp_path / "probe", timeout=1, env={}, cwd=tmp_path
        )


def test_claim_platform_rejects_non_arm64(monkeypatch):
    tool = _load_tool()
    monkeypatch.setattr(tool.sys, "platform", "darwin")
    monkeypatch.setattr(tool.platform, "machine", lambda: "x86_64")
    with pytest.raises(tool.CompileABError, match="Darwin arm64"):
        tool.require_claim_platform()


def test_pair_order_is_predeclared_and_alternating():
    tool = _load_tool()
    assert [tool.pair_order(index) for index in range(1, 5)] == [
        ("candidate", "baseline"),
        ("baseline", "candidate"),
        ("candidate", "baseline"),
        ("baseline", "candidate"),
    ]
    assert tool._verdict_exit_code({"verdict": "ACCEPT"}) == 0
    assert tool._verdict_exit_code({"verdict": "DENY"}) == 2


def test_summary_uses_paired_wall_ratio_and_rejects_resource_regression():
    tool = _load_tool()
    pairs = [
        {
            "candidate": _row(90.0, 80.0, 90, 90, 100, 100),
            "baseline": _row(100.0, 81.0, 100, 100, 100, 100),
        },
        {
            "candidate": _row(100.0, 80.0, 90, 90, 100, 100),
            "baseline": _row(108.0, 81.0, 100, 100, 100, 100),
        },
        {
            "candidate": _row(110.0, 80.0, 90, 90, 100, 100),
            "baseline": _row(121.0, 81.0, 100, 100, 100, 100),
        },
    ]
    summary = tool.summarize_pairs(
        pairs,
        min_speedup_ratio=1.08,
        max_compute_regression_ratio=1.0,
        max_memory_regression_ratio=1.02,
    )
    assert summary["paired_median_wall_speedup"] == pytest.approx(1.1)
    assert summary["median_wall_speedup"] == pytest.approx(1.08)
    assert summary["verdict"] == "ACCEPT"

    pairs[0]["candidate"]["metrics"]["cycles"] = 200
    pairs[1]["candidate"]["metrics"]["cycles"] = 200
    summary = tool.summarize_pairs(
        pairs,
        min_speedup_ratio=1.08,
        max_compute_regression_ratio=1.0,
        max_memory_regression_ratio=1.02,
    )
    assert summary["verdict"] == "DENY"
    assert summary["resource_regressions"] == [
        "cycles=median:2.000000x/paired:2.000000x/max:2.000000x"
    ]


def test_summary_denies_when_one_counterbalanced_pair_is_slower():
    tool = _load_tool()
    pairs = [
        {"candidate": _row(90, 80, 90, 90, 100, 100), "baseline": _row(100, 80, 90, 90, 100, 100)},
        {"candidate": _row(90, 80, 90, 90, 100, 100), "baseline": _row(100, 80, 90, 90, 100, 100)},
        {"candidate": _row(90, 80, 90, 90, 100, 100), "baseline": _row(100, 80, 90, 90, 100, 100)},
        {"candidate": _row(101, 80, 90, 90, 100, 100), "baseline": _row(100, 80, 90, 90, 100, 100)},
    ]
    summary = tool.summarize_pairs(
        pairs,
        min_speedup_ratio=1.08,
        max_compute_regression_ratio=1.0,
        max_memory_regression_ratio=1.02,
    )
    assert summary["paired_median_wall_speedup"] > 1.08
    assert summary["paired_wall_speedup_range"][0] < 1.0
    assert summary["verdict"] == "DENY"


def test_summary_denies_when_one_pair_exceeds_resource_ceiling():
    tool = _load_tool()
    pairs = [
        {
            "candidate": _row(90, cpu, 90, 90, 100, 100),
            "baseline": _row(100, 100, 100, 100, 100, 100),
        }
        for cpu in (90, 90, 90, 101)
    ]
    summary = tool.summarize_pairs(
        pairs,
        min_speedup_ratio=1.08,
        max_compute_regression_ratio=1.0,
        max_memory_regression_ratio=1.02,
    )
    assert summary["paired_median_resource_ratios"]["cpu_s"] < 1.0
    assert summary["paired_max_resource_ratios"]["cpu_s"] == pytest.approx(1.01)
    assert summary["verdict"] == "DENY"


def test_summary_requires_both_arm_medians_and_paired_median_to_pass():
    tool = _load_tool()
    pairs = [
        {
            "candidate": _row(10.0, 10, 10, 10, 10, 10),
            "baseline": _row(9.0, 10, 10, 10, 10, 10),
        },
        {
            "candidate": _row(100.0, 10, 10, 10, 10, 10),
            "baseline": _row(108.0, 10, 10, 10, 10, 10),
        },
        {
            "candidate": _row(1000.0, 10, 10, 10, 10, 10),
            "baseline": _row(1070.0, 10, 10, 10, 10, 10),
        },
    ]
    summary = tool.summarize_pairs(
        pairs,
        min_speedup_ratio=1.08,
        max_compute_regression_ratio=1.0,
        max_memory_regression_ratio=1.02,
    )
    assert summary["median_wall_speedup"] == pytest.approx(1.08)
    assert summary["paired_median_wall_speedup"] == pytest.approx(1.07)
    assert summary["verdict"] == "DENY"

    reverse_pairs = [
        {
            "candidate": _row(10.0, 10, 10, 10, 10, 10),
            "baseline": _row(11.0, 10, 10, 10, 10, 10),
        },
        {
            "candidate": _row(100.0, 10, 10, 10, 10, 10),
            "baseline": _row(107.0, 10, 10, 10, 10, 10),
        },
        {
            "candidate": _row(1000.0, 10, 10, 10, 10, 10),
            "baseline": _row(1080.0, 10, 10, 10, 10, 10),
        },
    ]
    summary = tool.summarize_pairs(
        reverse_pairs,
        min_speedup_ratio=1.08,
        max_compute_regression_ratio=1.0,
        max_memory_regression_ratio=1.02,
    )
    assert summary["median_wall_speedup"] == pytest.approx(1.07)
    assert summary["paired_median_wall_speedup"] == pytest.approx(1.08)
    assert summary["verdict"] == "DENY"


def test_resource_guards_require_arm_and_paired_medians_independently():
    tool = _load_tool()
    arm_only = [
        {
            "candidate": _row(1, candidate, 1, 1, 1, 1),
            "baseline": _row(2, baseline, 1, 1, 1, 1),
        }
        for candidate, baseline in ((10, 11), (100, 101), (1000, 5))
    ]
    summary = tool.summarize_pairs(
        arm_only,
        min_speedup_ratio=1.01,
        max_compute_regression_ratio=1.0,
        max_memory_regression_ratio=1.02,
    )
    assert summary["paired_median_resource_ratios"]["cpu_s"] < 1.0
    assert summary["candidate_medians"]["cpu_s"] > summary["baseline_medians"]["cpu_s"]
    assert any(item.startswith("cpu_s=") for item in summary["resource_regressions"])

    paired_only = [
        {
            "candidate": _row(1, candidate, 1, 1, 1, 1),
            "baseline": _row(2, baseline, 1, 1, 1, 1),
        }
        for candidate, baseline in ((1000, 500), (10, 5), (100, 101))
    ]
    summary = tool.summarize_pairs(
        paired_only,
        min_speedup_ratio=1.01,
        max_compute_regression_ratio=1.0,
        max_memory_regression_ratio=1.02,
    )
    assert summary["paired_median_resource_ratios"]["cpu_s"] > 1.0
    assert summary["candidate_medians"]["cpu_s"] < summary["baseline_medians"]["cpu_s"]
    assert any(item.startswith("cpu_s=") for item in summary["resource_regressions"])


def test_thresholds_reject_nan():
    tool = _load_tool()
    with pytest.raises(tool.CompileABError, match="must be finite"):
        tool.summarize_pairs(
            [{"candidate": _row(1, 1, 1, 1, 1, 1), "baseline": _row(2, 1, 1, 1, 1, 1)}],
            min_speedup_ratio=1.01,
            max_compute_regression_ratio=float("nan"),
            max_memory_regression_ratio=1.02,
        )


def test_timeout_kills_child_that_ignores_term_and_holds_pipe(tmp_path):
    tool = _load_tool()
    child_pid = tmp_path / "child.pid"
    child_code = (
        "import os, signal, sys, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "open(sys.argv[1], 'w').write(str(os.getpid())); "
        "print('holding inherited pipe', flush=True); time.sleep(60)"
    )
    parent_code = (
        "import subprocess, sys; "
        "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]]); "
        "sys.exit(0)"
    )
    started = time.monotonic()
    with pytest.raises(tool.CompileABError, match="timed out"):
        tool._run_process(
            [sys.executable, "-c", parent_code, child_code, str(child_pid)],
            timeout=1,
            env=os.environ.copy(),
        )
    assert time.monotonic() - started < 6
    pid = int(child_pid.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        pytest.fail(f"timed-out child {pid} survived process-group kill")


def test_successful_parent_cannot_leave_a_residual_child(tmp_path):
    tool = _load_tool()
    child_pid = tmp_path / "residual-child.pid"
    child_code = "import time; time.sleep(60)"
    parent_code = (
        "import subprocess, sys; "
        "child=subprocess.Popen([sys.executable, '-c', sys.argv[1]], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        "open(sys.argv[2], 'w').write(str(child.pid))"
    )
    with pytest.raises(tool.CompileABError, match="returned while a child process remained"):
        tool._run_process(
            [sys.executable, "-c", parent_code, child_code, str(child_pid)],
            timeout=5,
            env=os.environ.copy(),
        )
    pid = int(child_pid.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        pytest.fail(f"normal-return residual child {pid} survived cleanup")


def test_direct_run_interrupt_cleans_process_group(monkeypatch):
    tool = _load_tool()
    events = []

    class FakeProcess:
        pid = 123
        returncode = None

        def communicate(self, timeout):
            raise KeyboardInterrupt

    monkeypatch.setattr(tool.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(
        tool, "_terminate_process_group", lambda process: events.append(process.pid)
    )
    with pytest.raises(KeyboardInterrupt):
        tool._run_process(["probe"], timeout=1, env={}, cwd=tool.REPO_ROOT)
    assert events == [123]


def test_runtime_snapshot_lock_is_archive_scoped_and_released(tmp_path):
    tool = _load_tool()
    first = tmp_path / "first.a"
    second = tmp_path / "second.a"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    first_lock = Path(str(first) + ".build.lock")
    with tool._runtime_snapshot_lock(first):
        assert first_lock.is_dir()
        with pytest.raises(tool.CompileABError, match="build lock is held"):
            with tool._runtime_snapshot_lock(first):
                pass
        with tool._runtime_snapshot_lock(second):
            assert Path(str(second) + ".build.lock").is_dir()
    assert not first_lock.exists()
    assert not Path(str(second) + ".build.lock").exists()


def test_runner_requires_distinct_inputs_and_exact_oracles(tmp_path):
    tool = _load_tool()
    with pytest.raises(tool.CompileABError, match="distinct content hashes"):
        tool._require_distinct_input_hashes(["same", "same", "different"])
    parser = tool._parser()
    base = [
        "--candidate", str(tmp_path / "a"),
        "--baseline", str(tmp_path / "b"),
        "--runtime-archive", str(tmp_path / "runtime.a"),
        "--warmup", str(tmp_path / "warmup.py"),
        "--output-dir", str(tmp_path / "out"),
        "--input", str(tmp_path / "one.py"),
        "--input", str(tmp_path / "two.py"),
        "--input", str(tmp_path / "three.py"),
    ]
    with pytest.raises(SystemExit):
        parser.parse_args(base)
    with pytest.raises(tool.CompileABError, match="child of build"):
        tool._validate_output_dir(str(tool.REPO_ROOT / "tests" / "accidental-output"))
    with pytest.raises(tool.CompileABError, match="child of build"):
        tool._validate_output_dir(str(tool.REPO_ROOT / "projects" / "output"))
    assert tool._validate_output_dir(
        str(tool.REPO_ROOT / "build" / "claim-grade-output")
    ) == tool.REPO_ROOT / "build" / "claim-grade-output"


def test_failure_status_only_updates_the_claimed_output(tmp_path):
    tool = _load_tool()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / tool.RUN_OWNER_FILE).write_text(
        json.dumps({"run_token": "somebody-else"}), encoding="utf-8"
    )
    manifest = foreign / "manifest.json"
    manifest.write_text('{"status":"KEEP"}\n', encoding="utf-8")
    before = manifest.read_bytes()
    tool._mark_owned_run_failure(
        foreign, run_token="ours", status="ERROR", error="must not write"
    )
    assert manifest.read_bytes() == before

    owned = tmp_path / "owned"
    tool._claim_output_directory(owned, harness="test", run_token="ours")
    tool._mark_owned_run_failure(
        owned, run_token="ours", status="ERROR", error="expected"
    )
    recorded = json.loads((owned / "manifest.json").read_text(encoding="utf-8"))
    assert recorded["status"] == "ERROR"
    assert recorded["run_token"] == "ours"


def test_build_receipts_bind_compilers_runtime_manifests_and_single_diff(tmp_path):
    tool = _load_tool()
    compilers = []
    for arm in ("candidate", "baseline"):
        compiler = tmp_path / arm
        compiler.write_text(arm, encoding="utf-8")
        compilers.append(compiler)
    runtime = tmp_path / "runtime.a"
    runtime.write_bytes(b"runtime")
    runtime_sha = tool.sha256_path(runtime)
    receipts, source_diff = _write_build_evidence(tool, tmp_path, compilers, runtime)
    loaded = [
        tool._load_build_receipt(
            receipt,
            arm=arm,
            compiler_sha256=tool.sha256_path(compiler),
            runtime_sha256=runtime_sha,
            compiler_size_bytes=compiler.stat().st_size,
        )
        for receipt, arm, compiler in zip(
            receipts, ("candidate", "baseline"), compilers, strict=True
        )
    ]
    single = tool._validate_single_variable(
        loaded[0], loaded[1], [tool.PRIMARY_SOURCE], source_diff
    )
    assert single["changed_sources"] == [tool.PRIMARY_SOURCE]
    assert single["logical_source_root"].endswith(
        "/canonical-stage-build/source-snapshot"
    )
    with pytest.raises(tool.CompileABError, match="different compiler bytes"):
        tool._load_build_receipt(
            receipts[0],
            arm="candidate",
            compiler_sha256="0" * 64,
            runtime_sha256=runtime_sha,
            compiler_size_bytes=compilers[0].stat().st_size,
        )
    with pytest.raises(tool.CompileABError, match="different compiler size"):
        tool._load_build_receipt(
            receipts[0],
            arm="candidate",
            compiler_sha256=tool.sha256_path(compilers[0]),
            runtime_sha256=runtime_sha,
            compiler_size_bytes=compilers[0].stat().st_size + 1,
        )

    mismatched_producer = copy.deepcopy(loaded[1])
    mismatched_producer["producer_tools"]["run_pcc_stage1_build.py"][
        "sha256"
    ] = "f" * 64
    with pytest.raises(tool.CompileABError, match="different build producer"):
        tool._validate_single_variable(
            loaded[0], mismatched_producer, [tool.PRIMARY_SOURCE], source_diff
        )

    mismatched_env = copy.deepcopy(loaded[1])
    mismatched_env["receipt"]["environment"]["PCC_PY_FRONTEND_JOBS"] = "9"
    with pytest.raises(tool.CompileABError, match="build environments differ"):
        tool._validate_single_variable(
            loaded[0], mismatched_env, [tool.PRIMARY_SOURCE], source_diff
        )


def test_single_variable_rejects_distinct_absolute_consumed_source_roots(tmp_path):
    tool = _load_tool()
    compilers = []
    for arm in ("candidate", "baseline"):
        compiler = tmp_path / arm
        compiler.write_text(arm, encoding="utf-8")
        compilers.append(compiler)
    runtime = tmp_path / "runtime.a"
    runtime.write_bytes(b"runtime")
    receipts, source_diff = _write_build_evidence(
        tool,
        tmp_path / "per-arm-roots",
        compilers,
        runtime,
        shared_logical_source_root=False,
    )
    loaded = [
        tool._load_build_receipt(
            receipt,
            arm=arm,
            compiler_sha256=tool.sha256_path(compiler),
            runtime_sha256=tool.sha256_path(runtime),
            compiler_size_bytes=compiler.stat().st_size,
        )
        for receipt, arm, compiler in zip(
            receipts, ("candidate", "baseline"), compilers, strict=True
        )
    ]
    with pytest.raises(tool.CompileABError, match="different absolute source roots"):
        tool._validate_single_variable(
            loaded[0], loaded[1], [tool.PRIMARY_SOURCE], source_diff
        )


def test_build_receipt_v3_is_self_contained_after_original_build_is_removed(tmp_path):
    tool = _load_tool()
    compilers = []
    for arm in ("candidate", "baseline"):
        compiler = tmp_path / arm
        compiler.write_text(arm, encoding="utf-8")
        compilers.append(compiler)
    runtime = tmp_path / "runtime.a"
    runtime.write_bytes(b"runtime")
    receipts, _source_diff = _write_build_evidence(
        tool, tmp_path / "builds", compilers, runtime
    )
    bundle = tool._snapshot_build_evidence(
        receipts[0], tmp_path / "localized-evidence", "candidate"
    )
    copied_receipt = Path(bundle["receipt"])
    old_receipt = json.loads(copied_receipt.read_text(encoding="utf-8"))
    receipts[0].parent.rename(tmp_path / "retired-candidate-build")
    (tmp_path / "builds" / "canonical-source").rename(
        tmp_path / "retired-canonical-source"
    )
    loaded = tool._load_build_receipt(
        copied_receipt,
        arm="candidate",
        compiler_sha256=tool.sha256_path(compilers[0]),
        runtime_sha256=tool.sha256_path(runtime),
        compiler_size_bytes=compilers[0].stat().st_size,
    )
    assert loaded["receipt"] == old_receipt
    assert not Path(loaded["source_root"]).exists()
    tool._verify_build_evidence(bundle)


def test_build_receipt_v2_fails_closed(tmp_path):
    tool = _load_tool()
    compilers = []
    for arm in ("candidate", "baseline"):
        compiler = tmp_path / arm
        compiler.write_text(arm, encoding="utf-8")
        compilers.append(compiler)
    runtime = tmp_path / "runtime.a"
    runtime.write_bytes(b"runtime")
    receipts, _source_diff = _write_build_evidence(
        tool, tmp_path / "builds", compilers, runtime
    )
    receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    receipt["schema"] = "pcc.stage1-build-receipt.v2"
    receipt.pop("origin_source_root")
    receipt.pop("logical_source_root")
    receipts[0].write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(tool.CompileABError, match="fields mismatch"):
        tool._load_build_receipt(
            receipts[0],
            arm="candidate",
            compiler_sha256=tool.sha256_path(compilers[0]),
            runtime_sha256=tool.sha256_path(runtime),
            compiler_size_bytes=compilers[0].stat().st_size,
        )


def test_build_receipt_rejects_origin_consumption_and_output_overlap(tmp_path):
    tool = _load_tool()
    compilers = []
    for arm in ("candidate", "baseline"):
        compiler = tmp_path / arm
        compiler.write_text(arm, encoding="utf-8")
        compilers.append(compiler)
    runtime = tmp_path / "runtime.a"
    runtime.write_bytes(b"runtime")
    receipts, _source_diff = _write_build_evidence(
        tool, tmp_path / "origin-consumed", compilers, runtime
    )
    receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    origin = receipt["origin_source_root"]
    receipt["cwd"] = origin
    receipt["logical_source_root"] = origin
    receipt["environment"]["PCC_SOURCE_ROOT"] = origin
    receipt["environment"]["PCC_REPO_ROOT"] = origin
    receipt["environment_sha256"] = tool._canonical_sha256(receipt["environment"])
    receipt["command"][14] = str(Path(origin) / "pcc" / "__main__.py")
    receipt["command_sha256"] = tool._canonical_sha256(receipt["command"])
    receipts[0].write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(tool.CompileABError, match="owned build snapshot"):
        tool._load_build_receipt(
            receipts[0],
            arm="candidate",
            compiler_sha256=tool.sha256_path(compilers[0]),
            runtime_sha256=tool.sha256_path(runtime),
            compiler_size_bytes=compilers[0].stat().st_size,
        )

    receipts, _source_diff = _write_build_evidence(
        tool, tmp_path / "overlap", compilers, runtime
    )
    receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    receipt["origin_source_root"] = str(
        Path(receipt["logical_source_root"]).parent
    )
    receipts[0].write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(tool.CompileABError, match="overlaps the build output"):
        tool._load_build_receipt(
            receipts[0],
            arm="candidate",
            compiler_sha256=tool.sha256_path(compilers[0]),
            runtime_sha256=tool.sha256_path(runtime),
            compiler_size_bytes=compilers[0].stat().st_size,
        )


@pytest.mark.parametrize(
    ("position", "replacement_name"),
    ((3, "other.time"), (8, "other.profile.json"), (16, "other-pcc1")),
)
def test_build_receipt_binds_exact_command_artifact_paths(
    tmp_path, position, replacement_name
):
    tool = _load_tool()
    compilers = []
    for arm in ("candidate", "baseline"):
        compiler = tmp_path / arm
        compiler.write_text(arm, encoding="utf-8")
        compilers.append(compiler)
    runtime = tmp_path / "runtime.a"
    runtime.write_bytes(b"runtime")
    receipts, _source_diff = _write_build_evidence(
        tool, tmp_path / "exact-command", compilers, runtime
    )
    receipt_path = receipts[0]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    original_build_root = Path(receipt["logical_source_root"]).parent
    receipt["command"][position] = str(original_build_root / replacement_name)
    receipt["command_sha256"] = tool._canonical_sha256(receipt["command"])
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(tool.CompileABError, match="strict stage1 protocol"):
        tool._load_build_receipt(
            receipt_path,
            arm="candidate",
            compiler_sha256=tool.sha256_path(compilers[0]),
            runtime_sha256=tool.sha256_path(runtime),
            compiler_size_bytes=compilers[0].stat().st_size,
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("extra-field", "stage result fields mismatch"),
        ("profile-hash", "artifact hashes are inconsistent"),
        ("compiler-size", "not a strict successful build"),
        ("compiler-size-type", "not a strict successful build"),
    ),
)
def test_build_receipt_cross_binds_exact_stage_result(
    tmp_path, mutation, message
):
    tool = _load_tool()
    compilers = []
    for arm in ("candidate", "baseline"):
        compiler = tmp_path / arm
        compiler.write_text(arm, encoding="utf-8")
        compilers.append(compiler)
    runtime = tmp_path / "runtime.a"
    runtime.write_bytes(b"runtime")
    receipts, _source_diff = _write_build_evidence(
        tool, tmp_path / "stage-result-binding", compilers, runtime
    )

    def mutate(stage_result):
        if mutation == "extra-field":
            stage_result["unexpected"] = True
        elif mutation == "profile-hash":
            stage_result["profile_sha256"] = "0" * 64
        elif mutation == "compiler-size":
            stage_result["compiler_size_bytes"] += 1
        else:
            stage_result["compiler_size_bytes"] = float(
                stage_result["compiler_size_bytes"]
            )

    _rewrite_stage_result(tool, receipts[0], mutate)
    with pytest.raises(tool.CompileABError, match=message):
        tool._load_build_receipt(
            receipts[0],
            arm="candidate",
            compiler_sha256=tool.sha256_path(compilers[0]),
            runtime_sha256=tool.sha256_path(runtime),
            compiler_size_bytes=compilers[0].stat().st_size,
        )


@pytest.mark.parametrize(
    "shape",
    ["producer", "legacy", "missing-contract", "wrong-scope", "wrong-verdict-type"],
)
def test_build_receipt_validates_current_producer_metric_contract(tmp_path, shape):
    from scripts import run_pcc_stage1_build as producer

    tool = _load_tool()
    compilers = [tmp_path / "candidate", tmp_path / "baseline"]
    for compiler in compilers:
        compiler.write_bytes(compiler.name.encode())
    runtime = tmp_path / "runtime.a"
    runtime.write_bytes(b"runtime")
    receipts, _ = _write_build_evidence(
        tool, tmp_path / "metric-contract", compilers, runtime
    )

    def mutate(result):
        if shape == "legacy":
            return
        result["metric_scopes"] = dict(producer.STAGE1_METRIC_SCOPES)
        result["comparison_contract"] = dict(producer.STAGE1_COMPARISON_CONTRACT)
        if shape == "missing-contract":
            result.pop("comparison_contract")
        elif shape == "wrong-scope":
            result["metric_scopes"]["instructions"] = "whole_process_tree"
        elif shape == "wrong-verdict-type":
            result["comparison_contract"]["single_wall_verdict_allowed"] = 0

    _rewrite_stage_result(tool, receipts[0], mutate)

    def load():
        return tool._load_build_receipt(
            receipts[0],
            arm="candidate",
            compiler_sha256=tool.sha256_path(compilers[0]),
            compiler_size_bytes=compilers[0].stat().st_size,
            runtime_sha256=tool.sha256_path(runtime),
        )

    if shape in {"producer", "legacy"}:
        assert load()["receipt"]["status"] == "SUCCEEDED"
    else:
        with pytest.raises(
            tool.CompileABError, match="stage result (fields|metric contract)"
        ):
            load()


def test_build_receipt_rejects_protocol_artifact_and_incomplete_source(tmp_path):
    tool = _load_tool()
    compilers = []
    for arm in ("candidate", "baseline"):
        compiler = tmp_path / arm
        compiler.write_text(arm, encoding="utf-8")
        compilers.append(compiler)
    runtime = tmp_path / "runtime.a"
    runtime.write_bytes(b"runtime")
    receipts, _source_diff = _write_build_evidence(
        tool, tmp_path, compilers, runtime
    )
    receipt_path = receipts[0]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["command"][receipt["command"].index("self")] = "llvm"
    receipt["command_sha256"] = tool._canonical_sha256(receipt["command"])
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(tool.CompileABError, match="strict stage1 protocol"):
        tool._load_build_receipt(
            receipt_path,
            arm="candidate",
            compiler_sha256=tool.sha256_path(compilers[0]),
            runtime_sha256=tool.sha256_path(runtime),
            compiler_size_bytes=compilers[0].stat().st_size,
        )

    receipts, _source_diff = _write_build_evidence(
        tool, tmp_path / "artifact-case", compilers, runtime
    )
    (receipts[0].parent / "stage1.time").write_text("changed\n", encoding="utf-8")
    with pytest.raises(tool.CompileABError, match="stage artifact time changed"):
        tool._load_build_receipt(
            receipts[0],
            arm="candidate",
            compiler_sha256=tool.sha256_path(compilers[0]),
            runtime_sha256=tool.sha256_path(runtime),
            compiler_size_bytes=compilers[0].stat().st_size,
        )

    receipts, _source_diff = _write_build_evidence(
        tool, tmp_path / "closure-case", compilers, runtime
    )
    extra = receipts[0].parent / "source-snapshot" / "pcc" / "unlisted.py"
    extra.parent.chmod(0o755)
    extra.write_text("extra = 1\n", encoding="utf-8")
    with pytest.raises(tool.CompileABError, match="complete build closure"):
        tool._load_build_receipt(
            receipts[0],
            arm="candidate",
            compiler_sha256=tool.sha256_path(compilers[0]),
            runtime_sha256=tool.sha256_path(runtime),
            compiler_size_bytes=compilers[0].stat().st_size,
        )
    with pytest.raises(tool.CompileABError, match="original source snapshot"):
        tool._snapshot_build_evidence(
            receipts[0], tmp_path / "laundering-output", "candidate"
        )


@pytest.mark.parametrize("has_stamp", [False, True])
def test_runtime_bundle_copies_provenance_sources_and_detects_changes(
    tmp_path, monkeypatch, has_stamp,
):
    tool = _load_tool()
    monkeypatch.setattr(tool, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        tool,
        "_seal_runtime_bundle",
        lambda _bundle_dir, _archive, _copied: {"verified": True},
    )
    runtime_source = tmp_path / "pcc" / "runtime" / "py" / "sample.py"
    runtime_source.parent.mkdir(parents=True)
    runtime_source.write_text("VALUE = 1\n", encoding="utf-8")
    source_sha = hashlib.sha256(runtime_source.read_bytes()).hexdigest()
    runtime = tmp_path / "renamed-runtime.a"
    runtime.write_bytes(b"frozen runtime")
    Path(str(runtime) + ".capi_syms").write_text("_PyAnchor\n", encoding="utf-8")
    if has_stamp:
        Path(str(runtime) + ".target").write_text("darwin:arm64:test\n", encoding="utf-8")
    Path(str(runtime) + ".provenance.json").write_text(
        json.dumps(
            {
                "members": [
                    {
                        "source": "pcc/runtime/py/sample.py",
                        "source_sha256": source_sha,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "out"
    output.mkdir()
    bundle = tool._prepare_runtime_bundle(runtime, output)
    copied_source = output / "runtime-bundle" / "py" / "sample.py"
    assert copied_source.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert Path(bundle["archive"]).read_bytes() == b"frozen runtime"
    assert Path(bundle["archive"] + ".target").exists() == has_stamp
    tool._verify_runtime_bundle(bundle)
    copied_source.chmod(0o644)
    copied_source.write_text("VALUE = 2\n", encoding="utf-8")
    with pytest.raises(tool.CompileABError, match="changed during A/B"):
        tool._verify_runtime_bundle(bundle)


@pytest.mark.parametrize("checksums", [["a" * 64], ["a" * 64, "b" * 64]])
@pytest.mark.parametrize("has_stamp", [False, True])
def test_runtime_bundle_preserves_every_verified_codegen_identity(
    tmp_path, monkeypatch, checksums, has_stamp,
):
    from types import SimpleNamespace

    tool = _load_tool()
    archive = tmp_path / "runtime.a"
    archive.write_bytes(b"archive whose members were verified")
    for suffix, content in [
        (".target", "darwin:arm64:arm64-apple-darwin\n"),
        (".provenance.json", "{}\n"), (".capi_syms", "PyAnchor\n"),
    ]:
        if suffix != ".target" or has_stamp:
            Path(str(archive) + suffix).write_text(content)
    manifest = {
        "target_triple": "arm64-apple-darwin", "member_count": len(checksums),
        "members": [{"object_emitter": "pcc", "codegen_checksum": checksum}
                    for checksum in checksums],
    }
    calls = []

    def verify(path, **kwargs):
        calls.append((path, kwargs["runtime_root"]))
        return manifest

    monkeypatch.setattr(tool, "_load_provenance_provider", lambda: (
        SimpleNamespace(verify_runtime_archive_manifest=verify), tool._path_receipt(SCRIPT),
    ))
    copied = {}
    verification = tool._seal_runtime_bundle(tmp_path, archive, copied)
    assert calls == [(archive, tmp_path)]
    assert verification["target_identity_source"] == (
        "verified-manifest+legacy-stamp" if has_stamp else "verified-manifest"
    )
    assert Path(str(archive) + ".target").exists() == has_stamp
    # One compiler gets the singular key; several get the plural one and the
    # singular key is absent.  It is never published as null: a reader that
    # only knows "codegen_checksum" must fail to find it rather than read a
    # null that looks like "not recorded" beside real checksums.
    if len(checksums) == 1:
        assert verification["codegen_checksum"] == checksums[0]
        assert "codegen_checksums" not in verification
    else:
        assert "codegen_checksum" not in verification
        assert verification["codegen_checksums"] == checksums
    evidence = tool.runtime_bundle_evidence({"files": copied, "verification": verification})
    assert evidence.get("codegen_checksum") == verification.get("codegen_checksum")
    assert evidence.get("codegen_checksums") == verification.get("codegen_checksums")
    assert ("codegen_checksum" in evidence) == ("codegen_checksum" in verification)
    assert evidence["producer_claim"] == "binary-integrity-only; producer source closure not proven"
    assert evidence["target_identity_source"] == verification["target_identity_source"]
    if has_stamp:
        for invalid_stamp in ("", "darwin:x86_64:arm64-apple-darwin", "darwin:arm64:arm64-apple-darwin\nextra"):
            Path(str(archive) + ".target").write_text(invalid_stamp)
            with pytest.raises(tool.CompileABError, match="runtime target"):
                tool._seal_runtime_bundle(tmp_path, archive, {})
        Path(str(archive) + ".target").write_text("darwin:arm64:arm64-apple-darwin\n")
    for invalid_target in ("x86_64-unknown-linux-gnu", "arm64-apple-darwin\ninjected"):
        manifest["target_triple"] = invalid_target
        with pytest.raises(tool.CompileABError, match="claim platform"):
            tool._seal_runtime_bundle(tmp_path, archive, {})
    manifest["target_triple"] = "arm64-apple-darwin"

    for invalid in (None, "unknown", "f" * 63):
        manifest["members"][-1]["codegen_checksum"] = invalid
        with pytest.raises(tool.CompileABError, match="unknown codegen checksum"):
            tool._seal_runtime_bundle(tmp_path, archive, {})


def test_runtime_bundle_requires_provenance_even_after_archive_rename(
    tmp_path, monkeypatch
):
    tool = _load_tool()
    monkeypatch.setattr(tool, "REPO_ROOT", tmp_path)
    runtime = tmp_path / "renamed.a"
    runtime.write_bytes(b"archive")
    output = tmp_path / "out"
    output.mkdir()
    with pytest.raises(tool.CompileABError, match="runtime sidecar"):
        tool._prepare_runtime_bundle(runtime, output)


def test_runtime_bundle_can_verify_against_an_isolated_source_root(
    tmp_path, monkeypatch
):
    tool = _load_tool()
    live_root = tmp_path / "live"
    frozen_root = tmp_path / "frozen"
    monkeypatch.setattr(tool, "REPO_ROOT", live_root)
    monkeypatch.setattr(
        tool,
        "_seal_runtime_bundle",
        lambda _bundle_dir, _archive, _copied: {"verified": True},
    )
    runtime_source = frozen_root / "pcc" / "runtime" / "py" / "sample.py"
    runtime_source.parent.mkdir(parents=True)
    runtime_source.write_text("VALUE = 1\n", encoding="utf-8")
    source_sha = hashlib.sha256(runtime_source.read_bytes()).hexdigest()
    live_source = live_root / "pcc" / "runtime" / "py" / "sample.py"
    live_source.parent.mkdir(parents=True)
    live_source.write_text("VALUE = 2\n", encoding="utf-8")

    runtime = tmp_path / "runtime.a"
    runtime.write_bytes(b"frozen runtime")
    Path(str(runtime) + ".capi_syms").write_text("_PyAnchor\n", encoding="utf-8")
    Path(str(runtime) + ".target").write_text("darwin:arm64:test\n", encoding="utf-8")
    Path(str(runtime) + ".provenance.json").write_text(
        json.dumps(
            {
                "members": [
                    {
                        "source": "pcc/runtime/py/sample.py",
                        "source_sha256": source_sha,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "out"
    output.mkdir()

    bundle = tool._prepare_runtime_bundle(
        runtime,
        output,
        runtime_source_root=frozen_root,
    )

    copied = output / "runtime-bundle" / "py" / "sample.py"
    assert copied.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert Path(bundle["archive"]).read_bytes() == b"frozen runtime"


def test_runner_writes_complete_manifest_for_four_matched_pairs(tmp_path, monkeypatch):
    tool = _load_tool()
    monkeypatch.setattr(tool, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        tool,
        "_linkage",
        lambda _binary, *, timeout, env, cwd: {
            "checked": True,
            "links_libpython": False,
            "links_llvm": False,
            "stdout": "fake strict linkage",
        },
    )
    monkeypatch.setattr(tool, "_toolchain_receipts", lambda: [])

    def fake_runtime_bundle(runtime, output):
        root = output / "runtime-bundle"
        root.mkdir()
        copied = root / runtime.name
        tool._snapshot_file(runtime, copied, label="runtime")
        return {
            "source_archive": tool._path_receipt(runtime),
            "archive": str(copied),
            "files": {copied.name: tool._path_receipt(copied)},
            "verification": {
                "provider": tool._path_receipt(runtime),
                "ar": tool._path_receipt(runtime),
                "manifest_target": "test",
                "manifest_member_count": 1,
                "wheel_target": "test",
                "object_emitter": "test-object-emitter",
                "codegen_checksum": "f" * 64,
                "producer_claim": "binary-integrity-only; producer source closure not proven",
            },
        }

    monkeypatch.setattr(tool, "_prepare_runtime_bundle", fake_runtime_bundle)
    fake_source = (
        "#!/usr/bin/env python3\n"
        "import shlex\n"
        "import sys\n"
        "from pathlib import Path\n"
        "output_index = sys.argv.index('-o')\n"
        "source = Path(sys.argv[output_index - 1])\n"
        "output = Path(sys.argv[output_index + 1])\n"
        "value = source.read_text(encoding='utf-8').strip()\n"
        "output.write_text('#!/bin/sh\\nprintf \"%s\\\\n\" ' + shlex.quote(value) + "
        "'\\n', encoding='utf-8')\n"
        "output.chmod(0o755)\n"
    )
    compilers = []
    for index, name in enumerate(("candidate-pcc1", "baseline-pcc1")):
        compiler = tmp_path / name
        compiler.write_text(fake_source + f"# arm {index}\n", encoding="utf-8")
        compiler.chmod(0o755)
        compilers.append(compiler)
    runtime = tmp_path / "runtime.a"
    runtime.write_bytes(b"frozen runtime")
    monkeypatch.setattr(
        tool,
        "external_tool_evidence",
        lambda _host_python: [
            tool._portable_file_receipt(tool._path_receipt(runtime), "test")
        ],
    )
    receipts, source_diff = _write_build_evidence(
        tool,
        tmp_path,
        compilers,
        runtime,
    )
    recorded_producer_tools = json.loads(
        receipts[0].read_text(encoding="utf-8")
    )["producer_tools"]
    recorded_host_runtime = json.loads(
        receipts[0].read_text(encoding="utf-8")
    )["host_python_runtime"]
    monkeypatch.setattr(
        tool,
        "current_producer_tool_evidence",
        lambda: (recorded_producer_tools, []),
    )
    monkeypatch.setattr(
        tool,
        "host_python_runtime_evidence",
        lambda _host_python: recorded_host_runtime,
    )
    warmup = tmp_path / "warmup.py"
    warmup.write_text("warmup\n", encoding="utf-8")
    inputs = []
    for index, value in enumerate(("74856", "84856", "94856", "104856"), 1):
        source = tmp_path / f"pair{index}.py"
        source.write_text(value + "\n", encoding="utf-8")
        inputs.append(source)
    output_dir = tmp_path / "build" / "ab-output"
    argv = [
        "--candidate",
        str(compilers[0]),
        "--baseline",
        str(compilers[1]),
        "--runtime-archive",
        str(runtime),
        "--candidate-build-receipt",
        str(receipts[0]),
        "--baseline-build-receipt",
        str(receipts[1]),
        "--source-diff",
        str(source_diff),
        "--allowed-changed-source",
        tool.PRIMARY_SOURCE,
        "--warmup",
        str(warmup),
        "--output-dir",
        str(output_dir),
        "--min-speedup-ratio",
        "100",
    ]
    for source, expected in zip(
        inputs, ("74856", "84856", "94856", "104856"), strict=True
    ):
        argv.extend(("--input", str(source), "--expected-output", expected))
    manifest = tool.run(tool._parser().parse_args(argv))
    persisted = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == persisted["status"] == "DENIED"
    assert manifest["claim_level"] == "optimization-slice"
    assert manifest["summary"]["verdict"] == "DENY"
    assert (
        manifest["environment_by_arm"]["candidate"]["PCC_SOURCE_ROOT"]
        == manifest["environment_by_arm"]["baseline"]["PCC_SOURCE_ROOT"]
        == manifest["host_source_roots"]["baseline"]
    )
    assert "build-evidence/arm-b/source-snapshot" in manifest[
        "host_source_roots"
    ]["baseline"]
    assert manifest["host_helper_policy"] == "common-frozen-baseline-build-evidence"
    assert manifest["runtime_bundle"]["archive"].endswith("runtime-bundle/runtime.a")
    assert manifest["compiler_linkage"]["candidate"]["checked"] is True
    assert len(manifest["pairs"]) == 4
    assert manifest["warmup"]["schedule"] == [
        "candidate",
        "baseline",
        "baseline",
        "candidate",
    ]
    assert [pair["order"] for pair in manifest["pairs"]] == [
        ["candidate", "baseline"],
        ["baseline", "candidate"],
        ["candidate", "baseline"],
        ["baseline", "candidate"],
    ]
    assert all(
        pair["candidate"]["binary_sha256"] == pair["baseline"]["binary_sha256"]
        for pair in manifest["pairs"]
    )
    assert all(pair["candidate"]["run"]["returncode"] == 0 for pair in manifest["pairs"])
