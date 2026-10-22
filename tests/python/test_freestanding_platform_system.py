from pathlib import Path
import os
import platform
import subprocess
import sys

from pcc.frontends.python import pipeline


REPO_ROOT = Path(__file__).resolve().parents[2]
PLATFORM_SOURCE = (
    REPO_ROOT / "pcc" / "runtime" / "py" / "freestanding_platform_system.py"
)


def _compile_platform_ir(tmp_path: Path) -> Path:
    out = tmp_path / "freestanding_platform_system.ll"
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
    obj = tmp_path / ("platform_system_self.o" if self_backend else "platform_system.o")
    source = llvm_ir
    if self_backend:
        from pcc.backend.self_backend_dispatch import emit_self_asm

        source = tmp_path / "platform_system.s"
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


def _run_system_harness(tmp_path: Path, name: str, obj: Path) -> None:
    harness = tmp_path / (name + ".c")
    executable = tmp_path / name
    harness.write_text(
        r"""
#include <string.h>
#include <sys/utsname.h>
#include <unistd.h>

long pcc_platform_uname(void *buffer);
char *pcc_platform_uname_field(void *buffer, long index);
long pcc_platform_cpu_count(void);

int main(void) {
    unsigned char buffer[2048];
    struct utsname expected;
    if (uname(&expected) != 0) return 1;
    if (pcc_platform_uname(buffer) != 0) return 2;
    const char *fields[5] = {
        expected.sysname, expected.nodename, expected.release,
        expected.version, expected.machine
    };
    for (long i = 0; i < 5; i++) {
        if (strcmp(pcc_platform_uname_field(buffer, i), fields[i]) != 0) {
            return (int)(10 + i);
        }
    }
    long expected_cpus = sysconf(_SC_NPROCESSORS_ONLN);
    long actual_cpus = pcc_platform_cpu_count();
    if (expected_cpus > 0 && actual_cpus != expected_cpus) return 20;
    if (actual_cpus <= 0) return 21;
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


def test_platform_system_llvm_matches_host_uname_and_cpu_count(tmp_path):
    _run_system_harness(
        tmp_path,
        "platform_system_llvm",
        _build_platform_object(tmp_path, self_backend=False),
    )


def test_platform_system_self_matches_host_uname_and_cpu_count(tmp_path):
    _run_system_harness(
        tmp_path,
        "platform_system_self",
        _build_platform_object(tmp_path, self_backend=True),
    )


def test_platform_system_object_has_only_named_darwin_boundary(tmp_path):
    obj = _build_platform_object(tmp_path, self_backend=False)
    undefined = subprocess.run(
        ["nm", "-u", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert undefined.returncode == 0, undefined.stdout + undefined.stderr
    if sys.platform == "darwin":
        assert set(undefined.stdout.split()) == {"_sysctlbyname", "_uname"}
    else:
        assert sys.platform.startswith("linux") and platform.machine() == "x86_64"
        assert not undefined.stdout.strip()


def test_linux_platform_system_uses_raw_syscalls(tmp_path, monkeypatch):
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
    for symbol in ("uname", "sysconf", "sysctlbyname", "sched_getaffinity"):
        assert all("@" + symbol + "(" not in line for line in declarations)
    linux_ir = "\n".join(
        'target triple = "x86_64-unknown-linux-gnu"'
        if line.startswith("target triple = ")
        else line
        for line in ir_text.splitlines()
    )
    asm = emit_self_asm(linux_ir, "x86_64-unknown-linux-gnu")
    assert "syscall" in asm


def test_runtime_archive_plan_selects_platform_system_objects():
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
    assert "build_py/freestanding_platform_system.o" in archive_line
    assert "build_py/py_os_system.o" in archive_line


def test_runtime_archive_system_symbols_are_owned_by_python_port(
    pcc_runtime_archive,
):
    symbols = subprocess.run(
        ["nm", "-A", "-g", str(pcc_runtime_archive)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert symbols.returncode == 0, symbols.stdout + symbols.stderr
    decorated = "_" if sys.platform == "darwin" else ""
    for symbol in ("py_os_uname", "py_os_cpu_count"):
        owners = [
            line
            for line in symbols.stdout.splitlines()
            if line.rstrip().endswith(" T " + decorated + symbol)
        ]
        assert len(owners) == 1, owners
        assert ":py_os_system.o:" in owners[0], owners


def test_default_runtime_uses_owned_uname_and_cpu_count(
    tmp_path,
    pcc_runtime_archive,
):
    source = tmp_path / "platform_system_runtime_smoke.py"
    executable = tmp_path / "platform_system_runtime_smoke"
    source.write_text(
        "import os\n"
        "def main() -> None:\n"
        "    print(os.cpu_count())\n"
        "    print(os.uname().machine)\n"
        "main()\n",
        encoding="utf-8",
    )
    pipeline.compile_python(
        str(source),
        str(executable),
        backend="self",
        ir_scaffold_mode="on",
        libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    run = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.splitlines() == [str(os.cpu_count()), os.uname().machine]
