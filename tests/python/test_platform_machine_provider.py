"""Host delegation and emitted execution of the actual platform sibling."""

from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("system,machine", [
    ("linux", "x86_64"),
    ("linux", "aarch64"),
    ("darwin", "x86_64"),
    ("darwin", "arm64"),
    ("win32", "AMD64"),
])
def test_machine_host_delegates_to_python(monkeypatch, system, machine):
    from pcc.stdlib import platform as provider

    def forbidden(*args):
        pytest.fail("host platform.machine reached a native-only provider")

    monkeypatch.setattr(
        provider, "_sys",
        SimpleNamespace(platform=system, implementation=SimpleNamespace(name="cpython")),
    )
    monkeypatch.setattr(platform, "machine", lambda: machine)
    monkeypatch.setattr(provider, "_machine_native", forbidden)
    monkeypatch.setattr(provider, "_error_pending", forbidden)
    monkeypatch.setattr(provider, "ptr_is_null", forbidden)
    assert provider.machine() == machine
    assert provider.uname()[4] == machine


def test_native_compiled_sibling_machine(tmp_path, pcc_runtime_archive):
    from pcc.frontends.python.pipeline import compile_python_multi
    from pcc.frontends.python.pipeline_targets import host_target_triple

    root = Path(__file__).resolve().parents[2]
    source = tmp_path / "probe.py"
    source.write_text(
        "import platform\n"
        "from platform import machine as imported_machine\n"
        "\n"
        "def call_module(provider):\n"
        "    return provider.machine()\n"
        "\n"
        "def call_function(function):\n"
        "    return function()\n"
        "\n"
        "def main():\n"
        "    print(call_module(platform))\n"
        "    print(call_function(imported_machine))\n"
        "    print(platform.uname()[4])\n"
        "\n"
        "main()\n",
        encoding="utf-8",
    )
    executable = tmp_path / ("probe.exe" if os.name == "nt" else "probe")
    # Explicitly compile the real shim under the imported sibling's name.
    # Passing its module object into call_module prevents a direct builtin
    # platform.machine() call from standing in for the provider regression.
    compile_python_multi(
        [str(source), str(root / "pcc" / "stdlib" / "platform.py"),
         str(root / "pcc" / "driver" / "python_target.py")],
        str(executable),
        entry_module="probe",
        module_names=["probe", "platform", "pcc.driver.python_target"],
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
        runtime_archive=str(pcc_runtime_archive),
    )
    environment = os.environ.copy()
    environment.pop("LC_ALL", None)
    result = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30,
        env=environment,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    # The owned provider reports the executable ABI's architecture; this also
    # keeps the existing x86_64 spelling on native Windows (host Python: AMD64).
    expected = host_target_triple().split("-", 1)[0]
    assert result.stdout.splitlines() == [expected, expected, expected]
