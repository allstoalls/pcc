"""Owned package actions and shared host/native command semantics."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from pcc.driver import cli_bootstrap
from pcc.package import build_exec


def _source(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "answer.c").write_text("int answer(void) { return 42; }\n")
    return root


def test_owned_c_plan_never_discovers_or_invokes_host_toolchain(tmp_path, monkeypatch):
    root = _source(tmp_path)
    monkeypatch.setattr(build_exec, "toolchain_report", lambda **kw: pytest.fail("host toolchain discovery"))
    monkeypatch.setattr(build_exec.subprocess, "run", lambda *a, **k: pytest.fail("plan executed a child"))
    report = build_exec.execute_build_actions("demo", root, abi_mode="cpython-compat")
    assert report["ok"] is True
    assert report["build_ownership"] == "owned"
    assert report["host_free_build_claim"] is False  # a plan is not execution proof
    assert report["actions"][0]["command"][:3] == [sys.executable, "-m", "pcc"]
    assert "--backend=self" in report["actions"][0]["command"]
    assert report["actions"][0]["execution_owner"] == "pcc"
    assert not (root / "build").exists()


def test_owned_c_action_emits_actual_elf_object_without_external_tools(tmp_path, monkeypatch):
    from pcc.backend.elf_x86_64 import parse_relocatable

    root = _source(tmp_path)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("PCC_HOST_PYTHON", str(tmp_path / "denied-python"))
    monkeypatch.setenv("PCC_HOST_PCC", str(tmp_path / "denied-pcc"))
    report = build_exec.execute_build_actions("demo", root, execute=True, abi_mode="cpython-compat", timeout=30)
    assert report["ok"], report
    action = report["actions"][0]
    assert action["status"] == "passed"
    obj = parse_relocatable(Path(action["output"]).read_bytes())
    assert obj.machine in {62, 183}
    assert any(symbol.name == "answer" and symbol.section_index != 0 for symbol in obj.symbols)
    assert report["compiler_execution"] == "cpython-hosted"
    assert report["host_free_build_claim"] is False


def test_owned_compile_commands_preserve_paths_defines_and_outputs(tmp_path, monkeypatch):
    root = _source(tmp_path)
    include = root / "headers with spaces"
    include.mkdir()
    (include / "value.h").write_text("#define OFFSET 2\n")
    (root / "answer.c").write_text('#include "value.h"\nint answer(void) { return VALUE + OFFSET; }\n')
    (root / "compile_commands.json").write_text(json.dumps([{
        "directory": str(root), "file": "answer.c",
        "arguments": ["cc", "-I", "headers with spaces", "-DVALUE=40", "-c", "answer.c", "-o", "objects with spaces/answer.o"],
    }]))
    monkeypatch.setenv("PATH", "")
    report = build_exec.execute_build_actions("demo", root, execute=True, from_compile_commands=True, abi_mode="cpython-compat")
    assert report["ok"], report
    assert Path(report["actions"][0]["output"]) == root / "objects with spaces" / "answer.o"
    assert Path(report["actions"][0]["output"]).read_bytes().startswith(b"\x7fELF")
    command = report["actions"][0]["command"]
    assert "--cpp-arg=-I" + str(include) in command
    assert "--cpp-arg=-DVALUE=40" in command


@pytest.mark.parametrize("extra, code", [
    ({"link_output": "module.so"}, "PCC-PKG-OWNED-SHARED-LINK-UNAVAILABLE"),
    ({"regenerate_cython": True}, "PCC-PKG-OWNED-CYTHON-UNAVAILABLE"),
    ({"run_f2py": True}, "PCC-PKG-OWNED-F2PY-UNAVAILABLE"),
])
def test_owned_unimplemented_boundaries_never_run_external_tools(tmp_path, monkeypatch, extra, code):
    root = _source(tmp_path)
    monkeypatch.setattr(build_exec.subprocess, "run", lambda *a, **k: pytest.fail("external fallback"))
    report = build_exec.execute_build_actions("demo", root, execute=True, abi_mode="cpython-compat", **extra)
    assert report["ok"] is False
    assert code in [row["code"] for row in report["diagnostics"]]
    assert report["host_free_build_claim"] is False
    assert not any(row["status"] == "passed" for row in report["actions"])


def test_native_entry_uses_shared_parser_without_corrupting_package_name(tmp_path, monkeypatch, capsys):
    received = []
    def execute(name, path, **kwargs):
        received.append((name, path, kwargs))
        return {"ok": True, "name": name, "jobs": kwargs["jobs"]}
    monkeypatch.setattr(build_exec, "execute_build_actions", execute)
    report = tmp_path / "report.json"
    args = ["demo", "--path", str(tmp_path), "--jobs", "2", "--timeout=7", "--build-mode", "host", "--meson-target=selected", "--report", str(report), "--json"]
    assert cli_bootstrap._run_native_package_build_exec_from_pcc1(args) == 0
    assert received[0][0] == "demo"
    assert received[0][2]["jobs"] == 2
    assert received[0][2]["timeout"] == 7
    assert received[0][2]["build_mode"] == "host"
    assert received[0][2]["meson_target"] == "selected"
    assert json.loads(report.read_text()) == json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("args", [["--unknown-option"], ["--jobs"], ["--jobs=0"], ["--timeout=-1"], ["--build-mode=invalid"], ["another-name"]])
def test_native_entry_rejects_invalid_options_without_executing(tmp_path, monkeypatch, capsys, args):
    monkeypatch.setattr(build_exec, "execute_build_actions", lambda *a, **k: pytest.fail("invalid request executed"))
    assert cli_bootstrap._run_native_package_build_exec_from_pcc1(["demo", "--path", str(tmp_path), *args]) == 2
    assert "error:" in capsys.readouterr().err


def test_native_eager_selection_reaches_the_shared_executor(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(build_exec, "execute_eager_meson_extensions", lambda *a, **k: calls.append((a, k)) or {"ok": True})
    assert cli_bootstrap._run_native_package_build_exec_from_pcc1(["demo", "--path", str(tmp_path), "--eager-meson-extensions", "--build-mode=owned", "--jobs=2", "--timeout=9"]) == 0
    assert calls[0][0] == ("demo", str(tmp_path))
    assert calls[0][1]["jobs"] == 2 and calls[0][1]["timeout"] == 9
    assert calls[0][1]["build_mode"] == "owned"
    assert json.loads(capsys.readouterr().out)["ok"]


def test_owned_timeout_does_not_publish_a_stale_object(tmp_path, monkeypatch):
    root = _source(tmp_path)
    old = root / "build/pcc-package/owned/answer.c.o"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"old output")
    def timeout(command, **kwargs):
        assert kwargs["timeout"] == 1
        raise subprocess.TimeoutExpired(command, 1)
    monkeypatch.setattr(build_exec.subprocess, "run", timeout)
    report = build_exec.execute_build_actions("demo", root, execute=True, timeout=1, abi_mode="cpython-compat")
    assert not report["ok"]
    assert report["actions"][0]["status"] == "timeout"
    assert old.read_bytes() == b"old output"


def _meson_fixture(root, *, generated=False):
    build = root / "build/pcc-package/meson-build"
    build.mkdir(parents=True)
    other = root / "other.c"
    other.write_text("int other(void) { return 7; }\n")
    (root / "compile_commands.json").write_text(json.dumps([
        {"directory": str(root), "file": str(root / "answer.c"), "arguments": ["cc", "-c", str(root / "answer.c"), "-o", str(build / "answer.o")]},
        {"directory": str(root), "file": str(other), "arguments": ["cc", "-c", str(other), "-o", str(build / "other.o")]},
    ]))
    (build / "build.ninja").write_text(
        "build answer.o: c_COMPILER " + str(root / "answer.c") + (" | generated.h" if generated else "") + "\n"
        "build other.o: c_COMPILER " + str(other) + "\n"
        "build chosen: phony answer.o\n"
        + ("build generated.h: CUSTOM_COMMAND generator.py\n" if generated else "")
    )
    return build


def test_owned_meson_target_replays_only_selected_c_source_without_ninja(tmp_path, monkeypatch):
    root = _source(tmp_path)
    build = _meson_fixture(root)
    monkeypatch.setenv("PATH", "")
    report = build_exec.execute_build_actions("demo", root, execute=True, from_compile_commands=True, meson_target="chosen", abi_mode="cpython-compat")
    assert report["ok"], report
    assert len(report["actions"]) == 1
    assert report["actions"][0]["source"] == str(root / "answer.c")
    output = Path(report["actions"][0]["output"])
    assert output.read_bytes().startswith(b"\x7fELF")
    assert output.parent.name == "owned-target"
    assert not (build / "other.o").exists()
    assert not (build / "answer.o").exists()


def test_owned_meson_generated_dependency_is_explicit_and_never_runs_ninja(tmp_path, monkeypatch):
    root = _source(tmp_path)
    _meson_fixture(root, generated=True)
    monkeypatch.setattr(build_exec.subprocess, "run", lambda *a, **k: pytest.fail("generated command fallback"))
    report = build_exec.execute_build_actions("demo", root, execute=True, from_compile_commands=True, meson_target="chosen", abi_mode="cpython-compat")
    assert not report["ok"]
    assert "PCC-PKG-OWNED-GENERATED-COMMAND-UNAVAILABLE" in [row["code"] for row in report["diagnostics"]]
    assert report["host_free_build_claim"] is False


def test_native_wrapper_returns_same_owned_plan_as_shared_api(tmp_path):
    root = _source(tmp_path)
    shared = build_exec.execute_build_actions("demo", root, abi_mode="cpython-compat")
    native = json.loads(cli_bootstrap._native_build_exec_json("demo", str(root), [], [], [], False, False, False, None, [], "cpython-compat", False, False, False, False))
    assert native == shared


def test_owned_object_executes_its_actual_c_function(tmp_path, monkeypatch):
    from pcc.backend.elf_x86_64 import link_static_executable, parse_relocatable
    from pcc.backend.owned_elf_link import assemble

    root = _source(tmp_path)
    monkeypatch.setenv("PATH", "")
    report = build_exec.execute_build_actions("demo", root, execute=True, abi_mode="cpython-compat")
    assert report["ok"], report
    obj = parse_relocatable(Path(report["actions"][0]["output"]).read_bytes())
    if obj.machine == 62:
        target = "x86_64-unknown-linux-gnu"
        assembly = ".intel_syntax noprefix\n.text\n.globl _start\n_start:\n  call answer\n  mov rdi, rax\n  mov eax, 60\n  syscall\n"
    else:
        target = "aarch64-unknown-linux-gnu"
        assembly = ".text\n.globl _start\n_start:\n  bl answer\n  mov x8, #93\n  svc #0\n"
    image = link_static_executable([assemble(assembly, target), obj], entry="_start")
    executable = tmp_path / "answer"
    executable.write_bytes(image)
    executable.chmod(0o755)
    result = subprocess.run([str(executable)], timeout=10, capture_output=True)
    assert result.returncode == 42, result.stderr


def test_owned_compile_command_structured_argv_and_joined_output_are_authoritative(tmp_path):
    root = _source(tmp_path)
    (root / "compile_commands.json").write_text(json.dumps([{
        "directory": str(root), "file": "answer.c",
        "command": "untrusted-tool --different-command",
        "arguments": ["cc", "-c", "answer.c", "-oobjects with spaces/answer.o"],
    }]))
    report = build_exec.execute_build_actions("demo", root, from_compile_commands=True, abi_mode="cpython-compat")
    assert report["ok"]
    assert report["actions"][0]["output"] == str(root / "objects with spaces/answer.o")
    assert "untrusted-tool" not in report["actions"][0]["command"]


def test_owned_capi_projection_removes_recorded_cpython_includes(tmp_path):
    root = _source(tmp_path)
    (root / "compile_commands.json").write_text(json.dumps([{
        "directory": str(root), "file": "answer.c",
        "arguments": ["cc", "-I/usr/include/python3.15", "-c", "answer.c", "-o", "answer.o"],
    }]))
    report = build_exec.execute_build_actions("demo", root, from_compile_commands=True)
    assert report["ok"], report
    command = report["actions"][0]["command"]
    assert not any("python3.15" in token for token in command[3:])
    assert any("pcc-capi-include" in token for token in command)
    assert not (root / "build").exists()


def test_shared_build_exec_help_does_not_require_a_source_path(capsys):
    assert cli_bootstrap._run_native_package_build_exec_from_pcc1(["--help"]) == 0
    output = capsys.readouterr().out
    assert "--build-mode owned|host" in output and "--jobs N" in output
