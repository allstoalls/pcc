from pathlib import Path
import platform
import subprocess
import sys

from pcc.frontends.python import pipeline


REPO_ROOT = Path(__file__).resolve().parents[2]
PLATFORM_SOURCE = (
    REPO_ROOT / "pcc" / "runtime" / "py" / "freestanding_platform_io.py"
)
PLATFORM_SYMBOLS = (
    "pcc_platform_read",
    "pcc_platform_write",
    "pcc_platform_close",
    "pcc_platform_getpid",
)


def _compile_platform_ir(tmp_path: Path) -> Path:
    out = tmp_path / "freestanding_platform_io.ll"
    pipeline.compile_python(
        str(PLATFORM_SOURCE),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    return out


def _build_platform_object(tmp_path: Path) -> Path:
    llvm_ir = _compile_platform_ir(tmp_path)
    obj = tmp_path / "freestanding_platform_io.o"
    build = subprocess.run(
        ["clang", "-c", str(llvm_ir), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return obj


def _build_platform_self_object(tmp_path: Path) -> Path:
    from pcc.backend.self_backend_dispatch import emit_self_asm

    llvm_ir = _compile_platform_ir(tmp_path)
    asm = tmp_path / "freestanding_platform_io.s"
    obj = tmp_path / "freestanding_platform_io_self.o"
    asm.write_text(emit_self_asm(llvm_ir.read_text(encoding="utf-8")), encoding="utf-8")
    build = subprocess.run(
        ["clang", "-c", str(asm), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return obj


def _run_platform_harness(tmp_path: Path, name: str, obj: Path) -> None:
    harness = tmp_path / (name + ".c")
    executable = tmp_path / name
    harness.write_text(
        r"""
#include <stdint.h>
#include <unistd.h>

long pcc_platform_read(int fd, void *buf, unsigned long size);
long pcc_platform_write(int fd, const void *buf, unsigned long size);
long pcc_platform_close(int fd);
long pcc_platform_getpid(void);

int main(void) {
    int fds[2];
    char out[7] = {'o', 'w', 'n', 'e', 'd', '!', '\0'};
    char in[7] = {0, 0, 0, 0, 0, 0, 0};
    if (pipe(fds) != 0) return 1;
    if (pcc_platform_write(fds[1], out, 6) != 6) return 2;
    if (pcc_platform_close(fds[1]) != 0) return 3;
    if (pcc_platform_read(fds[0], in, 6) != 6) return 4;
    if (pcc_platform_close(fds[0]) != 0) return 5;
    if (in[0] != 'o' || in[5] != '!') return 6;
    if (pcc_platform_getpid() != (long)getpid()) return 7;
    return 0;
}
""",
        encoding="utf-8",
    )
    link = subprocess.run(
        ["clang", "-fno-builtin", str(harness), str(obj), "-o", str(executable)],
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


def test_platform_io_object_has_only_named_darwin_boundary(tmp_path):
    obj = _build_platform_object(tmp_path)
    symbols = subprocess.run(
        ["nm", "-g", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert symbols.returncode == 0, symbols.stdout + symbols.stderr
    for symbol in PLATFORM_SYMBOLS:
        assert "_" + symbol in symbols.stdout
    undefined = subprocess.run(
        ["nm", "-u", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert undefined.returncode == 0, undefined.stdout + undefined.stderr
    if sys.platform == "darwin":
        assert set(undefined.stdout.split()) == {"_close", "_getpid", "_read", "_write"}
    else:
        assert sys.platform.startswith("linux") and platform.machine() == "x86_64"
        assert undefined.stdout.strip() == ""
    _run_platform_harness(tmp_path, "platform_io_llvm", obj)


def test_platform_io_self_backend_runs_same_abi(tmp_path):
    obj = _build_platform_self_object(tmp_path)
    _run_platform_harness(tmp_path, "platform_io_self", obj)


def test_linux_platform_io_lowers_to_raw_syscalls(tmp_path, monkeypatch):
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
    for symbol in ("read", "write", "close", "getpid"):
        assert all("@" + symbol + "(" not in line for line in declarations)
    assert ir_text.count("syscall") >= 4
    linux_ir = "\n".join(
        'target triple = "x86_64-unknown-linux-gnu"'
        if line.startswith("target triple = ")
        else line
        for line in ir_text.splitlines()
    )
    asm = emit_self_asm(linux_ir, "x86_64-unknown-linux-gnu")
    assert "syscall" in asm


def test_runtime_archive_plan_selects_platform_io_object():
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
    assert "build_py/freestanding_platform_io.o" in archive_line


def test_default_runtime_routes_sys_write_and_getpid_through_platform_object(
    tmp_path,
    pcc_runtime_archive,
):
    source = tmp_path / "platform_runtime_smoke.py"
    executable = tmp_path / "platform_runtime_smoke"
    source.write_text(
        "import os\n"
        "import sys\n"
        "def main() -> None:\n"
        "    sys.stdout.write('platform-owned\\n')\n"
        "    print(os.getpid() > 0)\n"
        "if __name__ == '__main__':\n"
        "    main()\n",
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
    symbols = subprocess.run(
        ["nm", "-g", str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert symbols.returncode == 0, symbols.stdout + symbols.stderr
    for symbol in ("pcc_platform_write", "pcc_platform_getpid"):
        decorated = "_" + symbol if sys.platform == "darwin" else symbol
        assert any(line.endswith(" T " + decorated) for line in symbols.stdout.splitlines())
    run = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout == "platform-owned\nTrue\n"
