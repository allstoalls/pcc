from pathlib import Path
import platform
import subprocess
import sys

from pcc.frontends.python import pipeline


REPO_ROOT = Path(__file__).resolve().parents[2]
PLATFORM_SOURCE = (
    REPO_ROOT / "pcc" / "runtime" / "py" / "freestanding_platform_process.py"
)


def _compile_platform_ir(tmp_path: Path) -> Path:
    out = tmp_path / "freestanding_platform_process.ll"
    pipeline.compile_python(
        str(PLATFORM_SOURCE),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    return out


def test_raw_cstr_ternary_stays_out_of_managed_gc(tmp_path):
    text = _compile_platform_ir(tmp_path).read_text(encoding="utf-8")
    assert "@pcc_platform_process_find_path" in text
    assert "ternary.then.retain" not in text
    assert "ternary.else.retain" not in text
    assert "call ptr (ptr) @pcc_gc_retain(" not in text, "raw ternary retained a managed object"


def _build_platform_object(tmp_path: Path, *, self_backend: bool) -> Path:
    llvm_ir = _compile_platform_ir(tmp_path)
    obj = tmp_path / (
        "platform_process_self.o" if self_backend else "platform_process.o"
    )
    source = llvm_ir
    if self_backend:
        from pcc.backend.self_backend_dispatch import emit_self_asm

        source = tmp_path / "platform_process.s"
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


def _run_process_harness(tmp_path: Path, name: str, obj: Path) -> None:
    harness = tmp_path / (name + ".c")
    executable = tmp_path / name
    harness.write_text(
        r"""
#include <stdint.h>
#include <signal.h>
#include <unistd.h>

extern char **environ;

int64_t pcc_platform_waitpid(int64_t pid, int32_t *status, int64_t options);
int64_t pcc_platform_kill(int64_t pid, int64_t signal_number);
void pcc_platform_exit(int64_t status);
void pcc_platform_abort(void);
int64_t pcc_platform_spawnp(char **argv, char **envp, int64_t capture_output);
int64_t py_process_normalize_wait_status(int64_t raw_status);

int main(void) {
    pid_t child = fork();
    if (child < 0) return 1;
    if (child == 0) _exit(7);
    int32_t status = 0;
    if (pcc_platform_waitpid(child, &status, 0) != child) return 2;
    if (py_process_normalize_wait_status(status) != 7) return 3;

    child = fork();
    if (child < 0) return 4;
    if (child == 0) {
        for (;;) pause();
    }
    if (pcc_platform_kill(child, SIGTERM) != 0) return 5;
    status = 0;
    if (pcc_platform_waitpid(child, &status, 0) != child) return 6;
    if (py_process_normalize_wait_status(status) != -SIGTERM) return 7;

    if (py_process_normalize_wait_status(0x7f) != 127) return 8;
    if (pcc_platform_waitpid(-1, &status, 1) >= 0) return 9;

    child = fork();
    if (child < 0) return 10;
    if (child == 0) pcc_platform_exit(23);
    status = 0;
    if (pcc_platform_waitpid(child, &status, 0) != child) return 11;
    if (py_process_normalize_wait_status(status) != 23) return 12;

    child = fork();
    if (child < 0) return 13;
    if (child == 0) pcc_platform_abort();
    status = 0;
    if (pcc_platform_waitpid(child, &status, 0) != child) return 14;
    if (py_process_normalize_wait_status(status) != -SIGABRT) return 15;

    char *spawn_argv[] = {"sh", "-c", "exit 31", 0};
    int64_t spawned = pcc_platform_spawnp(spawn_argv, environ, 0);
    if (spawned <= 0) return 16;
    status = 0;
    if (pcc_platform_waitpid(spawned, &status, 0) != spawned) return 17;
    if (py_process_normalize_wait_status(status) != 31) return 18;

    char *quiet_argv[] = {"sh", "-c", "printf should-not-be-visible", 0};
    spawned = pcc_platform_spawnp(quiet_argv, environ, 1);
    if (spawned <= 0) return 19;
    status = 0;
    if (pcc_platform_waitpid(spawned, &status, 0) != spawned) return 20;
    if (py_process_normalize_wait_status(status) != 0) return 21;

    char *missing_argv[] = {"pcc-command-that-does-not-exist-7f6e", 0};
    if (pcc_platform_spawnp(missing_argv, environ, 0) >= 0) return 22;
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
    assert run.stdout == ""


def test_platform_process_llvm_waits_kills_and_normalizes(tmp_path):
    _run_process_harness(
        tmp_path,
        "platform_process_llvm",
        _build_platform_object(tmp_path, self_backend=False),
    )


def test_platform_process_self_waits_kills_and_normalizes(tmp_path):
    _run_process_harness(
        tmp_path,
        "platform_process_self",
        _build_platform_object(tmp_path, self_backend=True),
    )


def test_platform_process_object_has_only_named_darwin_boundary(tmp_path):
    obj = _build_platform_object(tmp_path, self_backend=False)
    undefined = subprocess.run(
        ["nm", "-u", str(obj)], capture_output=True, text=True, timeout=30
    )
    assert undefined.returncode == 0, undefined.stdout + undefined.stderr
    if sys.platform == "darwin":
        assert set(undefined.stdout.split()) == {
            "___error",
            "__exit",
            "_getpid",
            "_kill",
            "_access",
            "_free",
            "_malloc",
            "_posix_spawn",
            "_posix_spawn_file_actions_addopen",
            "_posix_spawn_file_actions_destroy",
            "_posix_spawn_file_actions_init",
            "_posix_spawnattr_destroy",
            "_posix_spawnattr_init",
            "_posix_spawnattr_setflags",
            "_posix_spawnattr_setpgroup",
            "_waitpid",
        }
    else:
        assert sys.platform.startswith("linux") and platform.machine() == "x86_64"
        assert set(undefined.stdout.split()) == {"free", "malloc"}


def test_linux_platform_process_uses_raw_wait4_and_kill_syscalls(
    tmp_path, monkeypatch
):
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
    for symbol in (
        "__error",
        "_exit",
        "access",
        "exit",
        "getpid",
        "kill",
        "posix_spawn",
        "posix_spawnp",
        "posix_spawn_file_actions_init",
        "posix_spawnattr_init",
        "waitpid",
        "wait4",
    ):
        assert all("@" + symbol + "(" not in line for line in declarations)
    linux_ir = "\n".join(
        'target triple = "x86_64-unknown-linux-gnu"'
        if line.startswith("target triple = ")
        else line
        for line in ir_text.splitlines()
    )
    assembly = emit_self_asm(linux_ir, "x86_64-unknown-linux-gnu")
    assert assembly.count("syscall") >= 2


def test_runtime_archive_plan_selects_platform_process_object():
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
    assert "build_py/freestanding_platform_process.o" in archive_line


def test_runtime_archive_builds_timeout_owner_from_pcc_python():
    runtime_dir = REPO_ROOT / "pcc" / "runtime"
    plan = subprocess.run(
        ["make", "-B", "-n", "libpy_runtime_pcc_py.a"],
        cwd=runtime_dir,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert plan.returncode == 0, plan.stdout + plan.stderr
    assert "py/py_process_timeout.py" in plan.stdout
    assert "src/py_process_timeout.c -o build_py/py_process_timeout.o" not in plan.stdout


def test_runtime_archive_process_symbols_are_owned_by_python_port(
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
    owners = [
        line
        for line in symbols.stdout.splitlines()
        if line.rstrip().endswith(
            " T " + decorated + "py_process_normalize_wait_status"
        )
    ]
    assert len(owners) == 1, owners
    assert ":freestanding_platform_process.o:" in owners[0], owners

    undefined = subprocess.run(
        ["nm", "-A", "-u", str(pcc_runtime_archive)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert undefined.returncode == 0, undefined.stdout + undefined.stderr
    timeout_lines = [
        line
        for line in undefined.stdout.splitlines()
        if ":py_process_timeout.o:" in line
    ]
    assert not any(
        line.rstrip().split()[-1].lstrip("_") in {"kill", "waitpid"}
        for line in timeout_lines
    )
    assert any("pcc_platform_kill" in line for line in timeout_lines)
    assert any("pcc_platform_waitpid" in line for line in timeout_lines)

    substrate_lines = [
        line
        for line in undefined.stdout.splitlines()
        if ":py_process_substrate.o:" in line
    ]
    assert not any(
        line.rstrip().split()[-1].lstrip("_") == "system"
        for line in substrate_lines
    )
    assert any("pcc_platform_spawnp" in line for line in substrate_lines)
    assert any("pcc_platform_waitpid" in line for line in substrate_lines)

    routed_helpers = (
        "py_process.o",
        "py_runtime_log.o",
        "py_obj_gc.o",
        "py_gc_backend.o",
    )
    routed_lines = [
        line
        for line in undefined.stdout.splitlines()
        if any((":" + name + ":") in line for name in routed_helpers)
    ]
    assert not any(
        line.rstrip().split()[-1].lstrip("_") in {"abort", "exit"}
        for line in routed_lines
    )
    abort_owners = {
        name
        for name in routed_helpers
        if any(
            (":" + name + ":") in line and "pcc_platform_abort" in line
            for line in routed_lines
        )
    }
    exit_owners = {
        name
        for name in routed_helpers
        if any(
            (":" + name + ":") in line and "pcc_platform_exit" in line
            for line in routed_lines
        )
    }
    assert abort_owners == {"py_runtime_log.o"}
    assert exit_owners == {"py_process.o"}


def test_default_runtime_sys_exit_uses_platform_process_owner(
    tmp_path, pcc_runtime_archive
):
    source = tmp_path / "owned_exit.py"
    executable = tmp_path / "owned_exit"
    source.write_text("import sys\nsys.exit(23)\n", encoding="utf-8")
    pipeline.compile_python(
        str(source),
        str(executable),
        backend="self",
        ir_scaffold_mode="on",
        libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 23, run.stdout + run.stderr
