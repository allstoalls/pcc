"""Installed-role pcc1 CLI/tooling differential with host Python disabled.

The interactive REPL/debugger/profiler/coverage surfaces remain explicit
fail-closed boundaries.  The supported script/module/command/stdin,
traceback, inspect and compiler-profile paths execute through current pcc1.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from pcc.tools.runtime_archive_provenance import verify_runtime_archive_manifest
from tests.python.pcc1_gate import find_current_pcc1


pytestmark = [pytest.mark.integration, pytest.mark.pcc_gate(probe="pcc1")]

ROOT = Path(__file__).resolve().parents[2]


def _run(command, *, cwd: Path, env=None, input_text=None, timeout=300):
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _install_runtime_bundle(source: Path, destination: Path) -> Path:
    destination.mkdir(parents=True)
    archive = destination / source.name
    shutil.copy2(source, archive)
    for suffix in (".provenance.json", ".capi_syms", ".target"):
        shutil.copy2(Path(str(source) + suffix), Path(str(archive) + suffix))
    manifest = Path(str(archive) + ".provenance.json")
    inventory = Path(str(archive) + ".capi_syms")
    target = Path(str(archive) + ".target").read_text(encoding="utf-8").strip()
    Path(str(archive) + ".wheel").write_text(
        "pcc.runtime-wheel-artifact.v2\n"
        + "target="
        + target
        + "\narchive-sha256="
        + _sha256(archive)
        + "\nmanifest-sha256="
        + _sha256(manifest)
        + "\ncapi-inventory-sha256="
        + _sha256(inventory)
        + "\n",
        encoding="utf-8",
    )
    return archive


@pytest.fixture(scope="module")
def installed_python3(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("installed-pcc1-tooling")
    current = find_current_pcc1(ROOT)
    assert current is not None, "receipt-current pcc1 is required"
    executable = root / "bin" / "python3"
    executable.parent.mkdir()
    shutil.copy2(current, executable)
    executable.chmod(executable.stat().st_mode | 0o111)

    source_runtime = ROOT / "pcc/runtime/libpy_runtime_pcc_py.a"
    verify_runtime_archive_manifest(source_runtime, runtime_root=source_runtime.parent)
    runtime = _install_runtime_bundle(source_runtime, root / "runtime")

    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env.pop("PYTHONPATH", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    env["PCC_HOST_PCC"] = "/usr/bin/false"
    env["PCC_COMPAT_PYTHON"] = "/usr/bin/false"
    env["PCC_RUNTIME_ARCHIVE"] = str(runtime)
    env["PCC_RUNTIME_CC"] = "pcc"
    env["PCC_PY_RUN_CACHE_DIR"] = str(root / "run-cache")
    env["TMPDIR"] = str(root / "tmp")
    Path(env["TMPDIR"]).mkdir()
    return executable, env, root


def test_installed_pcc1_cli_traceback_and_tooling_contract(
    installed_python3,
):
    pcc1, env, root = installed_python3
    work = root / "work"
    work.mkdir()
    script = work / "cli_probe.py"
    script.write_text(
        "import sys\n"
        "print(sys.argv[0])\n"
        "print('|'.join(sys.argv[1:]))\n"
        "print(sys.path[0])\n"
        "print(sys.executable)\n",
        encoding="utf-8",
    )

    native_script = _run(
        [str(pcc1), str(script), "left", "right"], cwd=work, env=env
    )
    assert native_script.returncode == 0, native_script.stdout + native_script.stderr
    oracle_script = _run(
        [sys.executable, str(script), "left", "right"], cwd=work
    )
    assert oracle_script.returncode == 0, oracle_script.stderr
    assert native_script.stdout.splitlines()[:3] == oracle_script.stdout.splitlines()[:3]
    assert native_script.stdout.splitlines()[3] == str(pcc1)

    inline = (
        "import sys; print(sys.argv[0]); print('|'.join(sys.argv[1:])); "
        "print(sys.path[0]); print(sys.executable)"
    )
    native_command = _run(
        [str(pcc1), "-c", inline, "tail"], cwd=work, env=env
    )
    assert native_command.returncode == 0, native_command.stderr
    oracle_command = _run([sys.executable, "-c", inline, "tail"], cwd=work)
    assert oracle_command.returncode == 0, oracle_command.stderr
    assert native_command.stdout.splitlines()[:3] == oracle_command.stdout.splitlines()[:3]
    assert native_command.stdout.splitlines()[3] == str(pcc1)

    native_stdin = _run(
        [str(pcc1), "-", "stdin-tail"],
        cwd=work,
        env=env,
        input_text=inline,
    )
    assert native_stdin.returncode == 0, native_stdin.stderr
    oracle_stdin = _run(
        [sys.executable, "-", "stdin-tail"], cwd=work, input_text=inline
    )
    assert oracle_stdin.returncode == 0, oracle_stdin.stderr
    assert native_stdin.stdout.splitlines()[:3] == oracle_stdin.stdout.splitlines()[:3]
    assert native_stdin.stdout.splitlines()[3] == str(pcc1)

    package = work / "tooling_module"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(
        "import sys\n"
        "print(sys.argv[0])\n"
        "print('|'.join(sys.argv[1:]))\n"
        "print(sys.path[0])\n",
        encoding="utf-8",
    )
    module_env = dict(env)
    module_env["PYTHONPATH"] = str(work)
    native_module = _run(
        [str(pcc1), "-m", "tooling_module", "module-tail"],
        cwd=work,
        env=module_env,
    )
    assert native_module.returncode == 0, native_module.stderr
    oracle_module = _run(
        [sys.executable, "-m", "tooling_module", "module-tail"],
        cwd=work,
        env={**os.environ, "PYTHONPATH": str(work)},
    )
    assert oracle_module.returncode == 0, oracle_module.stderr
    assert native_module.stdout == oracle_module.stdout

    failing = work / "traceback_probe.py"
    failing.write_text(
        "def outer():\n"
        "    def inner():\n"
        "        raise ValueError('tooling-boom')\n"
        "    inner()\n"
        "outer()\n",
        encoding="utf-8",
    )
    traceback_run = _run([str(pcc1), str(failing)], cwd=work, env=env)
    assert traceback_run.returncode != 0
    assert "Traceback (most recent call last):" in traceback_run.stderr
    assert str(failing) in traceback_run.stderr
    assert "in outer" in traceback_run.stderr
    assert "in inner" in traceback_run.stderr
    assert "ValueError: tooling-boom" in traceback_run.stderr

    profile = work / "profile.json"
    output = work / "profile.out"
    profiled = _run(
        [
            str(pcc1),
            "--profile-json",
            str(profile),
            "-o",
            str(output),
            str(script),
        ],
        cwd=work,
        env=env,
    )
    assert profiled.returncode == 0, profiled.stderr
    profile_payload = json.loads(profile.read_text(encoding="utf-8"))
    assert profile_payload["schema"] == "pcc.profile.v1"
    assert profile_payload["metadata"]["backend"] == "self"
    assert profile_payload["metadata"]["python_libpython"] == "off"

    capabilities = _run(
        [str(pcc1), "--tooling-capabilities"], cwd=work, env=env
    )
    assert capabilities.returncode == 0, capabilities.stderr
    assert json.loads(capabilities.stdout)["claim_mode"] == (
        "pcc1/pcc-native/self/no-libpython"
    )

    unsupported = (
        ([], "PCC-CPY-UNSUPPORTED-L3-TOOLING-INTERACTIVE-REPL"),
        (["-m", "pdb"], "PCC-CPY-UNSUPPORTED-L3-TOOLING-DEBUGGER"),
        (["-m", "cProfile"], "PCC-CPY-UNSUPPORTED-L3-TOOLING-PROFILER"),
        (["-m", "coverage"], "PCC-CPY-UNSUPPORTED-L3-TOOLING-COVERAGE"),
    )
    for arguments, diagnostic in unsupported:
        result = _run([str(pcc1), *arguments], cwd=work, env=env)
        assert result.returncode == 2
        assert diagnostic in result.stderr

    assert not list(Path(env["TMPDIR"]).glob("pcc1-python-input-*"))
    assert not list(Path(env["TMPDIR"]).glob("pcc1-module-*"))
