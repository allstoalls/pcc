"""Every native runtime is threaded (docs/plans/p0-threaded-concurrent-gc-runtime.md, package 1)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from pcc.frontends.python import owned_runtime_build
from pcc.frontends.python.thread_runtime_config import (
    implicit_thread_safepoints_enabled,
    reject_single_thread_runtime_request,
)

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "pcc" / "runtime"


@pytest.mark.parametrize("value", [None, "", "1", "true", "on"])
def test_runtime_configuration_is_always_threaded(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("PCC_WITH_THREADS", raising=False)
    else:
        monkeypatch.setenv("PCC_WITH_THREADS", value)
    assert owned_runtime_build.runtime_build_config()["threads"] is True
    assert implicit_thread_safepoints_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off"])
def test_single_thread_runtime_request_fails_explicitly(monkeypatch, value):
    monkeypatch.setenv("PCC_WITH_THREADS", value)
    with pytest.raises(ValueError, match="single-thread runtime was removed"):
        reject_single_thread_runtime_request()
    with pytest.raises(ValueError, match="single-thread runtime was removed"):
        owned_runtime_build.runtime_build_config()


def test_poll_owner_modules_disable_only_implicit_polls(monkeypatch):
    monkeypatch.delenv("PCC_WITH_THREADS", raising=False)
    monkeypatch.setenv("PCC_IMPLICIT_THREAD_SAFEPOINTS", "0")
    assert implicit_thread_safepoints_enabled() is False
    assert owned_runtime_build.runtime_build_config()["threads"] is True


def test_inventory_links_only_the_threaded_kernel():
    makefile = (RUNTIME / "Makefile").read_text(encoding="utf-8")
    assert "freestanding_thread_kernel\n" not in makefile
    assert "PCC_WITH_THREADS=0" not in makefile
    assert not (RUNTIME / "py" / "freestanding_thread_kernel.py").exists()
    for target in ("arm64-apple-darwin", "x86_64-unknown-linux-gnu", "x86_64-pc-windows-msvc"):
        names = owned_runtime_build.runtime_modules(str(RUNTIME), target)
        assert "freestanding_thread_kernel_pthread" in names
        assert "freestanding_thread_kernel" not in names


PROGRAM = '''import threading


def record(out):
    out.append(threading.get_ident())


def main():
    out = []
    worker = threading.Thread(target=record, args=(out,))
    worker.start()
    worker.join()
    print(len(out), out[0] != threading.get_ident())


main()
'''


@pytest.mark.integration
def test_thread_start_runs_the_target_on_another_native_thread(tmp_path):
    src = tmp_path / "threads.py"
    src.write_text(PROGRAM, encoding="utf-8")
    exe = tmp_path / "threads"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env.pop("PCC_WITH_THREADS", None)
    build = subprocess.run(
        ["uv", "run", "pcc", "--backend", "self", "--python-libpython=off", str(src), "-o", str(exe)],
        cwd=ROOT, text=True, capture_output=True, timeout=600, env=env,
    )
    assert build.returncode == 0, build.stderr
    run = subprocess.run([str(exe)], text=True, capture_output=True, timeout=60, env=env)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["1 True"]
