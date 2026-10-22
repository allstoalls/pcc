"""Freestanding process-RSS sampling on Darwin and Linux."""

from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path

from pcc.frontends.python import pipeline


REPO_ROOT = Path(__file__).resolve().parents[2]
PLATFORM_SOURCE = (
    REPO_ROOT / "pcc" / "runtime" / "py" / "freestanding_platform_rss.py"
)


def _compile_platform_ir(tmp_path: Path) -> Path:
    out = tmp_path / "freestanding_platform_rss.ll"
    pipeline.compile_python(
        str(PLATFORM_SOURCE),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    return out


def _build_platform_object(tmp_path: Path, *, self_backend: bool) -> Path:
    llvm_ir = _compile_platform_ir(tmp_path)
    obj = tmp_path / ("platform_rss_self.o" if self_backend else "platform_rss.o")
    source = llvm_ir
    if self_backend:
        from pcc.backend.self_backend_dispatch import emit_self_asm

        source = tmp_path / "platform_rss.s"
        source.write_text(
            emit_self_asm(llvm_ir.read_text(encoding="utf-8")),
            encoding="utf-8",
        )
    build = subprocess.run(
        ["clang", "-c", str(source), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return obj


def _run_rss_harness(tmp_path: Path, name: str, obj: Path) -> None:
    harness = tmp_path / (name + ".c")
    executable = tmp_path / name
    harness.write_text(
        r"""
#include <stdint.h>
#include <stdlib.h>

int64_t pcc_os_current_rss_bytes(void);
int64_t pcc_os_peak_rss_bytes(void);

int main(void) {
    int64_t current = pcc_os_current_rss_bytes();
    int64_t peak = pcc_os_peak_rss_bytes();
    if (current <= 1024 * 1024) return 1;
    if (peak < current) return 2;

    size_t size = 16u * 1024u * 1024u;
    volatile unsigned char *block = (volatile unsigned char *)malloc(size);
    if (block == 0) return 3;
    for (size_t offset = 0; offset < size; offset += 4096) {
        block[offset] = (unsigned char)(offset >> 12);
    }
    int64_t after = pcc_os_current_rss_bytes();
    int64_t peak_after = pcc_os_peak_rss_bytes();
    free((void *)block);
    if (after <= 0) return 4;
    if (peak_after < after || peak_after < peak) return 5;
    return 0;
}
""",
        encoding="utf-8",
    )
    link = subprocess.run(
        ["clang", str(harness), str(obj), "-o", str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert link.returncode == 0, link.stdout + link.stderr
    run = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_platform_rss_llvm_matches_host_process(tmp_path: Path) -> None:
    _run_rss_harness(
        tmp_path,
        "platform_rss_llvm",
        _build_platform_object(tmp_path, self_backend=False),
    )


def test_platform_rss_self_matches_host_process(tmp_path: Path) -> None:
    _run_rss_harness(
        tmp_path,
        "platform_rss_self",
        _build_platform_object(tmp_path, self_backend=True),
    )


def test_platform_rss_has_only_named_darwin_boundaries(tmp_path: Path) -> None:
    obj = _build_platform_object(tmp_path, self_backend=False)
    undefined = subprocess.run(
        ["nm", "-u", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert undefined.returncode == 0, undefined.stdout + undefined.stderr
    if sys.platform == "darwin":
        assert set(undefined.stdout.split()) == {
            "___error",
            "_close",
            "_getrusage",
            "_mach_task_self_",
            "_open",
            "_read",
            "_task_info",
        }
    else:
        assert sys.platform.startswith("linux") and platform.machine() == "x86_64"
        assert not undefined.stdout.strip()


def test_linux_platform_rss_uses_proc_status_and_raw_syscalls(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from pcc.backend.self_backend_dispatch import emit_self_asm
    from pcc.frontends.python.codegen.unsafe_lowering import UnsafeIntrinsicMixin

    monkeypatch.setattr(
        UnsafeIntrinsicMixin,
        "_target_sys_platform_text",
        lambda self: "linux",
    )
    monkeypatch.setattr(
        UnsafeIntrinsicMixin,
        "_target_machine_text",
        lambda self: "x86_64",
    )
    ir_text = _compile_platform_ir(tmp_path).read_text(encoding="utf-8")
    declarations = [line for line in ir_text.splitlines() if line.startswith("declare ")]
    for symbol in (
        "fopen",
        "fscanf",
        "fclose",
        "sysconf",
        "getrusage",
        "task_info",
        "mach_task_self_",
    ):
        assert all("@" + symbol not in line for line in declarations)
    assert "/proc/self/status" in ir_text
    linux_ir = "\n".join(
        'target triple = "x86_64-unknown-linux-gnu"'
        if line.startswith("target triple = ")
        else line
        for line in ir_text.splitlines()
    )
    asm = emit_self_asm(linux_ir, "x86_64-unknown-linux-gnu")
    assert asm.count("syscall") >= 3


def test_runtime_archive_rss_symbols_are_owned_by_python_port(
    tmp_path: Path,
    pcc_runtime_archive: Path,
) -> None:
    members = subprocess.run(
        ["ar", "-t", str(pcc_runtime_archive)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert members.returncode == 0, members.stdout + members.stderr
    assert "py_os_rss.o" not in members.stdout.splitlines()
    assert "freestanding_platform_rss.o" in members.stdout.splitlines()
    _run_rss_harness(
        tmp_path,
        "platform_rss_archive",
        pcc_runtime_archive,
    )
