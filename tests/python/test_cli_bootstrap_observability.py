from __future__ import annotations

import json
import os
import subprocess
import stat
from pathlib import Path


def test_bootstrap_subprocess_wrapper_enforces_configured_timeout(monkeypatch):
    import pcc.cli_bootstrap as cli

    calls = []
    def fake_run(args, *, check, timeout):
        calls.append((args, check, timeout))
        return object()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setenv("PCC_BOOTSTRAP_SUBPROCESS_TIMEOUT_SECONDS", "7")

    assert cli._bootstrap_subprocess_run(["tool", "arg"], check=True) is None
    assert calls == [(["tool", "arg"], True, 7)]


def test_bootstrap_cli_parses_observability_flags():
    from pcc.cli_bootstrap import parse_bootstrap_cli_args

    parsed, status, err = parse_bootstrap_cli_args([
        "--diagnostic-format", "json",
        "--profile-json", "profile.json",
        "--explain-fallback",
        "--backend", "self",
        "-o", "out",
        "prog.py",
    ])

    assert status == 0, err
    assert parsed is not None
    assert parsed[-3:] == ("json", "profile.json", True)


def test_bootstrap_cli_writes_profile_json_on_success(monkeypatch, tmp_path):
    import pcc.cli_bootstrap as cli

    src = tmp_path / "prog.py"
    src.write_text("print('ok')\n", encoding="utf-8")
    out = tmp_path / "prog.out"
    profile = tmp_path / "profile.json"
    calls = []

    def fake_compile(*args, **kwargs):
        calls.append((args, kwargs))
        Path(args[1]).write_text("fake", encoding="utf-8")

    monkeypatch.setattr(cli, "_compile_python", fake_compile)
    status = cli.bootstrap_cli_main([
        "--profile-json", str(profile),
        "-o", str(out),
        str(src),
    ])

    assert status == 0
    assert calls
    data = json.loads(profile.read_text(encoding="utf-8"))
    assert data["schema"] == "pcc.profile.v1"
    assert data["metadata"]["entry"] == "cli_bootstrap"
    assert data["metadata"]["time_unit"] == "seconds"
    assert "phase_totals_s" in data


def test_bootstrap_cli_without_output_compiles_to_temp_and_runs(monkeypatch, tmp_path):
    import pcc.cli_bootstrap as cli

    src = tmp_path / "prog.py"
    src.write_text("print('ok')\n", encoding="utf-8")
    calls = []

    def fake_compile(_src, out, **_kwargs):
        calls.append((_src, out))
        out_path = Path(out)
        out_path.write_text("#!/bin/sh\nprintf 'ran-temp\\n'\n", encoding="utf-8")
        out_path.chmod(out_path.stat().st_mode | stat.S_IXUSR)

    monkeypatch.setattr(cli, "_compile_python", fake_compile)
    monkeypatch.setenv("PCC_PY_RUN_CACHE_DIR", str(tmp_path / "cache"))
    status = cli.bootstrap_cli_main([str(src)])

    assert status == 0
    assert calls


def test_bootstrap_cli_without_output_reuses_run_cache(monkeypatch, tmp_path):
    import pcc.cli_bootstrap as cli

    src = tmp_path / "prog.py"
    src.write_text("print('ok')\n", encoding="utf-8")
    calls = []

    def fake_compile(_src, out, **_kwargs):
        calls.append((_src, out))
        out_path = Path(out)
        out_path.write_text("#!/bin/sh\nprintf 'cached\\n'\n", encoding="utf-8")
        out_path.chmod(out_path.stat().st_mode | stat.S_IXUSR)

    monkeypatch.setattr(cli, "_compile_python", fake_compile)
    monkeypatch.setenv("PCC_PY_RUN_CACHE_DIR", str(tmp_path / "cache"))

    assert cli.bootstrap_cli_main([str(src)]) == 0
    assert cli.bootstrap_cli_main([str(src)]) == 0
    assert len(calls) == 1


def test_cli_core_without_output_cache_separates_link_args(monkeypatch, tmp_path):
    import pcc.cli_core as cli
    import pcc.py_frontend.pipeline as pipeline

    src = tmp_path / "prog.py"
    src.write_text("print('ok')\n", encoding="utf-8")
    calls = []

    def fake_compile(_src, out, **kwargs):
        calls.append((_src, out, tuple(kwargs.get("link_args", ()))))
        out_path = Path(out)
        out_path.write_text("#!/bin/sh\nprintf 'cached\\n'\n", encoding="utf-8")
        out_path.chmod(out_path.stat().st_mode | stat.S_IXUSR)

    monkeypatch.setattr(pipeline, "compile_python", fake_compile)
    monkeypatch.setenv("PCC_PY_RUN_CACHE_DIR", str(tmp_path / "cache"))

    assert cli.cli_main([str(src)]) == 0
    assert cli.cli_main([str(src)]) == 0
    assert len(calls) == 1
    assert cli.cli_main(["--link-arg=-Wl,-dead_strip", str(src)]) == 0
    assert len(calls) == 2
    assert calls[-1][2] == ("-Wl,-dead_strip",)


def test_cli_core_forwards_python_link_args_in_executable_and_emit_modes(
    monkeypatch,
    tmp_path,
):
    import pcc.cli_core as cli
    import pcc.py_frontend.pipeline as pipeline

    src = tmp_path / "prog.py"
    src.write_text("print('ok')\n", encoding="utf-8")
    executable = tmp_path / "prog.out"
    emitted_ir = tmp_path / "prog.ll"
    calls = []
    requested = ["-Wl,-rpath,/tmp/pcc-cli-link-arg", "/tmp/libclihelper.a"]

    def fake_compile(_src, _out, **kwargs):
        calls.append(
            (
                bool(kwargs.get("emit_llvm_only")),
                list(kwargs.get("link_args", ())),
            )
        )

    monkeypatch.setattr(pipeline, "compile_python", fake_compile)

    assert cli.cli_main(
        [
            "--link-arg=" + requested[0],
            "--link-arg",
            requested[1],
            str(src),
            "-o",
            str(executable),
        ]
    ) == 0
    assert cli.cli_main(
        [
            "--link-arg=" + requested[0],
            "--link-arg",
            requested[1],
            "--emit-llvm=" + str(emitted_ir),
            str(src),
        ]
    ) == 0

    assert calls == [(False, requested), (True, requested)]


def test_python_entry_infers_project_root_for_tests_dir(monkeypatch, tmp_path):
    import pcc.cli_core as cli

    project = tmp_path / "project"
    tests = project / "tests"
    tests.mkdir(parents=True)
    src = tests / "submission_tests.py"
    src.write_text("import perf_takehome\n", encoding="utf-8")
    (project / "perf_takehome.py").write_text("VALUE = 1\n", encoding="utf-8")

    monkeypatch.delenv("PCC_PACKAGE_SITE", raising=False)
    roots = cli._inferred_package_site_roots(str(src))

    assert str(tests.resolve()) in roots
    assert str(project.resolve()) in roots


def test_bootstrap_cli_formats_hard_error_as_json(monkeypatch, tmp_path, capsys):
    import pcc.cli_bootstrap as cli

    src = tmp_path / "bad.py"
    src.write_text("print('bad')\n", encoding="utf-8")
    profile = tmp_path / "profile.json"

    def fake_compile(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "_compile_python", fake_compile)
    status = cli.bootstrap_cli_main([
        "--diagnostic-format=json",
        "--profile-json", str(profile),
        "--explain-fallback",
        "--emit-llvm",
        str(src),
    ])

    assert status == 1
    err = capsys.readouterr().err
    data = json.loads(err)
    assert data["schema"] == "pcc.diagnostics.v1"
    diag = data["diagnostics"][0]
    assert diag["code"] == "PCC-PY-COMPILE-001"
    assert diag["phase"] == "python-frontend"
    assert "boom" in diag["message"]
    assert "fallback_explain" in "\n".join(diag["notes"])
    assert profile.exists()


def test_bootstrap_cli_pytest_mode_uses_pcc1_native_runner(monkeypatch, tmp_path):
    import pcc.cli_bootstrap as cli

    test_dir = tmp_path / "tests"
    test_dir.mkdir()
    (test_dir / "test_sample.py").write_text(
        "from pcc.test_runner import fixture\n\n"
        "@fixture\n"
        "def value() -> int:\n"
        "    return 7\n\n"
        "def test_sample(value: int) -> None:\n"
        "    assert value == 7\n",
        encoding="utf-8",
    )

    calls = []

    def fake_run(cmd, *, check, timeout=None):
        calls.append((cmd, check))
        if cmd[:2] == ["mkdir", "-p"]:
            Path(cmd[2]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli.sys, "executable", "/tmp/pcc1")
    monkeypatch.setenv("TMPDIR", str(tmp_path))

    status = cli.bootstrap_cli_main(["--pytest", str(test_dir), "-q", "-n0"])

    assert status == 0
    assert calls[0] == (
        ["mkdir", "-p", str(tmp_path / ("pcc1-pytest-" + str(os.getpid())))],
        True,
    )
    assert calls[1][0][0] == "/tmp/pcc1"
    assert calls[1][0][-2:] == ["--python-libpython=off", "--ir-scaffold=on"]
    assert calls[2][0][0].endswith(".out")
    assert not any("uv" in arg for cmd, _ in calls for arg in cmd)


def test_bootstrap_cli_pytest_mode_defaults_to_tests(monkeypatch, tmp_path):
    import pcc.cli_bootstrap as cli

    calls = []

    def fake_run(cmd, *, check, timeout=None):
        calls.append((cmd, check))
        if cmd[:2] == ["mkdir", "-p"]:
            Path(cmd[2]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli.sys, "executable", "/tmp/pcc1")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    test_dir = tmp_path / "tests"
    test_dir.mkdir()
    (test_dir / "test_sample.py").write_text(
        "def test_sample() -> None:\n"
        "    assert 1 + 1 == 2\n",
        encoding="utf-8",
    )

    status = cli.bootstrap_cli_main(["--pytest"])

    assert status == 0
    assert calls[1][0][0] == "/tmp/pcc1"


def test_bootstrap_cli_pytest_mode_reports_failure(monkeypatch, capsys):
    import pcc.cli_bootstrap as cli

    def fake_run(cmd, *, check, timeout=None):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    status = cli.bootstrap_cli_main(["--pytest", "-m", "slow"])

    assert status == 2
    assert "supports only -m integration" in capsys.readouterr().err


def test_bootstrap_cli_pytest_mode_honors_integration_marker(monkeypatch, tmp_path):
    import pcc.cli_bootstrap as cli

    test_dir = tmp_path / "tests"
    test_dir.mkdir()
    (test_dir / "test_sample.py").write_text(
        "import pytest\n"
        "pytestmark = pytest.mark.integration\n\n"
        "def test_unit() -> None:\n"
        "    assert True\n\n"
        "def test_integration() -> None:\n"
        "    assert True\n",
        encoding="utf-8",
    )
    calls = []

    def fake_run(cmd, *, check, timeout=None):
        calls.append((cmd, check))
        if cmd[:2] == ["mkdir", "-p"]:
            Path(cmd[2]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli.sys, "executable", "/tmp/pcc1")
    monkeypatch.setenv("TMPDIR", str(tmp_path))

    status = cli.bootstrap_cli_main(["--pytest", "-m", "integration", str(test_dir)])

    assert status == 0
    runner = tmp_path / ("pcc1-pytest-" + str(os.getpid()))
    generated = next(runner.glob("runner_*.py"))
    text = generated.read_text(encoding="utf-8")
    assert "pytestmark = None" in text
    assert "run_tests([test_unit, test_integration])" in text


def test_bootstrap_cli_pytest_mode_honors_literal_skipif(monkeypatch, tmp_path):
    import pcc.cli_bootstrap as cli

    test_dir = tmp_path / "tests"
    test_dir.mkdir()
    (test_dir / "test_sample.py").write_text(
        "import pytest\n\n"
        "@pytest.mark.skipif(True, reason='skip')\n"
        "def test_skipped() -> None:\n"
        "    assert False\n\n"
        "@pytest.mark.skipif(False, reason='run')\n"
        "def test_kept() -> None:\n"
        "    assert True\n",
        encoding="utf-8",
    )
    calls = []

    def fake_run(cmd, *, check, timeout=None):
        calls.append((cmd, check))
        if cmd[:2] == ["mkdir", "-p"]:
            Path(cmd[2]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli.sys, "executable", "/tmp/pcc1")
    monkeypatch.setenv("TMPDIR", str(tmp_path))

    status = cli.bootstrap_cli_main(["--pytest", str(test_dir), "-q", "-n0"])

    assert status == 0
    runner = tmp_path / ("pcc1-pytest-" + str(os.getpid()))
    generated = next(runner.glob("runner_*.py"))
    text = generated.read_text(encoding="utf-8")
    assert "run_tests([test_kept])" in text
    assert "test_skipped])" not in text


def test_bootstrap_cli_c_dispatch_uses_full_cli_in_process(monkeypatch):
    import pcc.cli_bootstrap as cli
    import pcc.cli_core as core

    calls = []

    monkeypatch.delenv("PCC_BACKEND", raising=False)
    monkeypatch.setenv("PCC_HOST_PCC", "/usr/bin/false")
    monkeypatch.setenv("PCC_HOST_PYTHON", "/usr/bin/false")
    monkeypatch.setattr(core, "cli_main", lambda argv: calls.append(argv) or 0)

    status = cli.bootstrap_cli_main(["hello.c", "--cpp-arg=-DTEST=1"])

    assert status == 0
    assert calls == [["--backend", "self", "hello.c", "--cpp-arg=-DTEST=1"]]


def test_bootstrap_cli_capi_symbol_tables_match_representative_symbols():
    import pcc.cli_bootstrap as cli

    assert cli._native_known_capi_header("PyLong_FromLong") == "longobject.h"
    assert cli._native_known_capi_header("PyObject_CallNoArgs") == "abstract.h"
    assert cli._native_known_capi_header("PyArray_API") == "numpy/arrayobject.h"
    assert cli._native_known_capi_header("PyArray_CustomThing") == (
        "numpy/arrayobject.h"
    )
    assert cli._native_known_capi_header("PyUFunc_CustomThing") == (
        "numpy/ufuncobject.h"
    )
    assert cli._native_known_capi_header("TotallyMissingSymbol") is None

    assert cli._native_capi_implemented("PyLong_FromLong") is True
    assert cli._native_capi_implemented("PyObject_CallNoArgs") is True
    assert cli._native_capi_implemented("PyArray_API") is True
    assert cli._native_capi_implemented("TotallyMissingSymbol") is False


def test_bootstrap_array_core_split_module_keeps_native_report_shape(capsys):
    from pcc.cli_bootstrap_array_core import _run_native_package_array_core_from_pcc1

    status = _run_native_package_array_core_from_pcc1([
        "--literal",
        "[[1,2,3],[4,5,6]]",
        "--json",
    ])

    assert status == 0
    report = json.loads(capsys.readouterr().out)
    assert report["schema"] == "pcc.array-core.v1"
    assert report["ok"] is True
    assert report["shape"] == [2, 3]
    assert report["strides"] == [24, 8]
    assert report["dtype"] == "int64"


def test_bootstrap_cli_routes_array_core_to_split_native_module(monkeypatch):
    import pcc.cli_bootstrap as cli

    calls = []

    def fake_array_core(args):
        calls.append(args)
        return 17

    monkeypatch.setattr(cli, "_run_native_package_array_core_from_pcc1", fake_array_core)

    assert cli._run_python_module_from_pcc1([
        "-m",
        "pcc.package.array_core",
        "--literal",
        "[1]",
    ]) == 17
    assert cli._run_python_module_from_pcc1([
        "-m",
        "pcc.package",
        "array-core",
        "--shape",
        "2,3",
    ]) == 17

    assert calls == [["--literal", "[1]"], ["--shape", "2,3"]]
