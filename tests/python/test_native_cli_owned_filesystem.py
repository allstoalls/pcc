"""Native CLI ownership checks against an explicitly selected fresh pcc1.

Run after building pcc1 with PCC1_BINARY and its matching PCC_RUNTIME_ARCHIVE.
An empty PATH denies external filesystem/compiler helpers; every program has a
function and must print its actual native result.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

pytestmark = pytest.mark.integration

_PROGRAM = "def main():\n    print(6 * 7)\n\nmain()\n"


@pytest.fixture
def native_cli(tmp_path):
    selected = os.environ.get("PCC1_BINARY")
    assert selected, "set PCC1_BINARY to a compiler built from the tested source"
    compiler = Path(selected).resolve()
    assert compiler.is_file(), compiler
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    cache = tmp_path / "run-cache"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env.pop("PCC_DISABLE_PY_RUN_CACHE", None)
    env.update({
        "PATH": "",
        "PCC_HOST_PYTHON": str(tmp_path / "denied-host-python"),
        "PCC_HOST_PCC": str(tmp_path / "denied-host-pcc"),
        "PCC_PY_RUN_CACHE_DIR": str(cache),
        "TMPDIR": str(scratch),
    })
    return compiler, env, scratch, cache


def _run(native_cli, argv, *, source=None):
    compiler, env, _scratch, _cache = native_cli
    result = subprocess.run(
        [str(compiler), *argv], env=env, input=source,
        capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr == ""
    return result.stdout


def test_native_sync_help_preserves_command_dispatch(native_cli):
    result = _run(native_cli, ["sync", "--help"])
    assert "usage: pcc sync" in result
    assert "--lock" in result


@pytest.mark.parametrize("kind", ["command", "stdin"])
def test_native_inline_source_executes_without_external_filesystem_tools(native_cli, kind):
    if kind == "command":
        output = _run(native_cli, ["-c", _PROGRAM])
    else:
        output = _run(native_cli, ["-"], source=_PROGRAM)
    assert output == "42\n"
    assert list(native_cli[2].iterdir()) == []


def test_native_run_cache_publishes_and_reuses_without_external_filesystem_tools(native_cli, tmp_path):
    source = tmp_path / "program.py"
    source.write_text(_PROGRAM)
    assert _run(native_cli, [str(source)]) == "42\n"
    cache = native_cli[3]
    files = sorted(path for path in cache.rglob("*") if path.is_file())
    assert files, "script execution must publish a cached native executable"
    assert not any(".tmp." in path.name for path in files)
    identity = [(path, path.stat().st_mtime_ns, path.read_bytes()) for path in files]
    assert _run(native_cli, [str(source)]) == "42\n"
    assert identity == [(path, path.stat().st_mtime_ns, path.read_bytes()) for path in files]
    assert list(native_cli[2].iterdir()) == []
