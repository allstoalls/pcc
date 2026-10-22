"""Fresh-pcc1 package C execution proof; run after the build-exec patch freeze."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def native_build(tmp_path):
    selected = os.environ.get("PCC1_BINARY")
    assert selected, "set PCC1_BINARY to a compiler built after the build-exec patch"
    compiler = Path(selected).resolve()
    assert compiler.is_file()
    project = tmp_path / "project"
    project.mkdir()
    (project / "answer.c").write_text("int answer(void) { return 42; }\n")
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PATH"] = ""
    env["PCC_HOST_PYTHON"] = str(tmp_path / "denied-python")
    env["PCC_HOST_PCC"] = str(tmp_path / "denied-pcc")
    return compiler, project, env


def test_native_package_owned_c_object_links_and_executes_without_external_tools(native_build, tmp_path):
    from pcc.backend.elf_x86_64 import link_static_executable, parse_relocatable
    from pcc.backend.owned_elf_link import assemble

    compiler, project, env = native_build
    report_path = tmp_path / "report.json"
    result = subprocess.run([
        str(compiler), "-m", "pcc.package", "build-exec", "demo",
        "--path", str(project), "--execute", "--jobs=2", "--timeout=30",
        "--abi=cpython-compat", "--report", str(report_path), "--json",
    ], env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report == json.loads(report_path.read_text())
    assert report["ok"] and report["host_free_build_claim"]
    assert report["name"] == "demo" and report["jobs"] == 2
    assert report["compiler_execution"] == "native-pcc"
    assert report["actions"][0]["command"][0] == str(compiler)
    obj = parse_relocatable(Path(report["actions"][0]["output"]).read_bytes())
    assert any(symbol.name == "answer" and symbol.section_index for symbol in obj.symbols)
    if obj.machine == 62:
        triple = "x86_64-unknown-linux-gnu"
        source = ".intel_syntax noprefix\n.text\n.globl _start\n_start:\n call answer\n mov rdi, rax\n mov eax, 60\n syscall\n"
    else:
        triple = "aarch64-unknown-linux-gnu"
        source = ".text\n.globl _start\n_start:\n bl answer\n mov x8, #93\n svc #0\n"
    executable = tmp_path / "answer"
    executable.write_bytes(link_static_executable([assemble(source, triple), obj], entry="_start"))
    executable.chmod(0o755)
    run = subprocess.run([str(executable)], env=env, timeout=10, capture_output=True)
    assert run.returncode == 42, run.stderr


def test_native_package_owned_parser_rejects_unknown_option(native_build):
    compiler, project, env = native_build
    result = subprocess.run([
        str(compiler), "-m", "pcc.package.build_exec", "demo", "--path", str(project),
        "--unknown-option", "--jobs", "2",
    ], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 2
    assert "unrecognized argument: --unknown-option" in result.stderr
    assert not (project / "build").exists()
