from pathlib import Path
import platform
import subprocess
import sys

from pcc.frontends.python import pipeline


REPO_ROOT = Path(__file__).resolve().parents[2]
PLATFORM_SOURCE = (
    REPO_ROOT / "pcc" / "runtime" / "py" / "freestanding_platform_time.py"
)


def _compile_platform_ir(tmp_path: Path) -> Path:
    out = tmp_path / "freestanding_platform_time.ll"
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
    obj = tmp_path / ("platform_time_self.o" if self_backend else "platform_time.o")
    source = llvm_ir
    if self_backend:
        from pcc.backend.self_backend_dispatch import emit_self_asm

        source = tmp_path / "platform_time.s"
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


def _run_time_harness(tmp_path: Path, name: str, obj: Path) -> None:
    harness = tmp_path / (name + ".c")
    executable = tmp_path / name
    harness.write_text(
        r"""
#include <stdint.h>
#include <sys/time.h>

int64_t pcc_platform_wall_time_us(void);
int64_t pcc_platform_monotonic_us(void);
int64_t pcc_platform_sleep_ns(int64_t ns);

int main(void) {
    struct timeval tv;
    if (gettimeofday(&tv, 0) != 0) return 1;
    int64_t expected = (int64_t)tv.tv_sec * 1000000 + tv.tv_usec;
    int64_t actual = pcc_platform_wall_time_us();
    int64_t delta = actual > expected ? actual - expected : expected - actual;
    if (delta > 2000000) return 2;
    int64_t before = pcc_platform_monotonic_us();
    if (pcc_platform_sleep_ns(5000000) != 0) return 3;
    int64_t after = pcc_platform_monotonic_us();
    if (before <= 0 || after - before < 3000) return 4;
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
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_platform_time_llvm_matches_host_and_sleeps(tmp_path):
    _run_time_harness(
        tmp_path,
        "platform_time_llvm",
        _build_platform_object(tmp_path, self_backend=False),
    )


def test_platform_time_self_matches_host_and_sleeps(tmp_path):
    _run_time_harness(
        tmp_path,
        "platform_time_self",
        _build_platform_object(tmp_path, self_backend=True),
    )


def test_platform_time_object_has_only_named_darwin_boundary(tmp_path):
    obj = _build_platform_object(tmp_path, self_backend=False)
    undefined = subprocess.run(
        ["nm", "-u", str(obj)], capture_output=True, text=True, timeout=30
    )
    assert undefined.returncode == 0, undefined.stdout + undefined.stderr
    if sys.platform == "darwin":
        assert set(undefined.stdout.split()) == {"_clock_gettime", "_nanosleep"}
    else:
        assert sys.platform.startswith("linux") and platform.machine() == "x86_64"
        assert not undefined.stdout.strip()


def test_linux_platform_time_uses_raw_syscalls(tmp_path, monkeypatch):
    from pcc.backend.self_backend_dispatch import emit_self_asm
    from pcc.frontends.python.codegen.unsafe_lowering import UnsafeIntrinsicMixin

    monkeypatch.setattr(
        UnsafeIntrinsicMixin, "_target_sys_platform_text", lambda self: "linux"
    )
    monkeypatch.setattr(
        UnsafeIntrinsicMixin, "_target_machine_text", lambda self: "x86_64"
    )
    ir_text = _compile_platform_ir(tmp_path).read_text(encoding="utf-8")
    declarations = [line for line in ir_text.splitlines() if line.startswith("declare ")]
    for symbol in ("clock_gettime", "gettimeofday", "nanosleep", "time"):
        assert all("@" + symbol + "(" not in line for line in declarations)
    linux_ir = "\n".join(
        'target triple = "x86_64-unknown-linux-gnu"'
        if line.startswith("target triple = ")
        else line
        for line in ir_text.splitlines()
    )
    assert "syscall" in emit_self_asm(linux_ir, "x86_64-unknown-linux-gnu")


def test_runtime_archive_plan_selects_platform_time_object():
    runtime_dir = REPO_ROOT / "pcc" / "runtime"
    plan = subprocess.run(
        ["make", "-B", "-n", "libpy_runtime_pcc_py.a"],
        cwd=runtime_dir,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert plan.returncode == 0, plan.stdout + plan.stderr
    archive_line = next(
        line
        for line in plan.stdout.splitlines()
        if "ar rcs libpy_runtime_pcc_py.a.tmp" in line
    )
    assert "build_py/freestanding_platform_time.o" in archive_line


def test_runtime_archive_c_helpers_consume_platform_time(
    pcc_runtime_archive,
):
    undefined = subprocess.run(
        ["nm", "-A", "-u", str(pcc_runtime_archive)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert undefined.returncode == 0, undefined.stdout + undefined.stderr
    helper_names = ("pcc_threads.o", "py_runtime_log.o", "py_process_timeout.o")
    helper_lines = [
        line
        for line in undefined.stdout.splitlines()
        if any((":" + name + ":") in line for name in helper_names)
    ]
    forbidden = ("clock_gettime", "gettimeofday", "nanosleep", "time")
    assert not any(
        line.rstrip().split()[-1].lstrip("_") in forbidden for line in helper_lines
    )
    assert any("pcc_platform_monotonic_us" in line for line in helper_lines)
