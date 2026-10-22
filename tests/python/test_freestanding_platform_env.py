from pathlib import Path
import os
import platform
import subprocess
import sys

from pcc.frontends.python import pipeline


REPO_ROOT = Path(__file__).resolve().parents[2]
PLATFORM_SOURCE = (
    REPO_ROOT / "pcc" / "runtime" / "py" / "freestanding_platform_env.py"
)


def _compile_platform_ir(tmp_path: Path) -> Path:
    out = tmp_path / "freestanding_platform_env.ll"
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
    obj = tmp_path / "freestanding_platform_env.o"
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
    asm = tmp_path / "freestanding_platform_env.s"
    obj = tmp_path / "freestanding_platform_env_self.o"
    asm.write_text(emit_self_asm(llvm_ir.read_text(encoding="utf-8")), encoding="utf-8")
    build = subprocess.run(
        ["clang", "-c", str(asm), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return obj


def _run_env_harness(tmp_path: Path, name: str, obj: Path) -> None:
    harness = tmp_path / (name + ".c")
    executable = tmp_path / name
    harness.write_text(
        r"""
#include <string.h>

long pcc_platform_env_init(char **envp);
char *pcc_platform_getenv(const char *name);
long pcc_platform_setenv(const char *name, const char *value, long overwrite);
long pcc_platform_unsetenv(const char *name);
char **pcc_platform_env_snapshot(void);
void pcc_platform_env_snapshot_free(char **snapshot);

static int snapshot_has(char **snapshot, const char *entry) {
    if (snapshot == 0) return 0;
    for (long i = 0; snapshot[i] != 0; i++) {
        if (strcmp(snapshot[i], entry) == 0) return 1;
    }
    return 0;
}

int main(void) {
    char original[] = "OWNED_ORIGINAL=first";
    char *initial[] = {original, "SECOND=value", 0};
    if (pcc_platform_env_init(initial) != 0) return 1;
    if (strcmp(pcc_platform_getenv("OWNED_ORIGINAL"), "first") != 0) return 2;
    original[15] = 'X';
    if (strcmp(pcc_platform_getenv("OWNED_ORIGINAL"), "first") != 0) return 3;
    if (pcc_platform_setenv("OWNED_ORIGINAL", "ignored", 0) != 0) return 4;
    if (strcmp(pcc_platform_getenv("OWNED_ORIGINAL"), "first") != 0) return 5;
    if (pcc_platform_setenv("OWNED_ORIGINAL", "next", 1) != 0) return 6;
    if (strcmp(pcc_platform_getenv("OWNED_ORIGINAL"), "next") != 0) return 7;
    if (pcc_platform_setenv("THIRD", "three", 1) != 0) return 8;
    if (strcmp(pcc_platform_getenv("THIRD"), "three") != 0) return 9;
    if (pcc_platform_unsetenv("SECOND") != 0) return 10;
    if (pcc_platform_getenv("SECOND") != 0) return 11;
    if (pcc_platform_setenv("", "bad", 1) == 0) return 12;
    if (pcc_platform_setenv("BAD=NAME", "bad", 1) == 0) return 13;
    if (pcc_platform_unsetenv("BAD=NAME") == 0) return 14;
    char **snapshot = pcc_platform_env_snapshot();
    if (snapshot == 0) return 15;
    if (!snapshot_has(snapshot, "OWNED_ORIGINAL=next")) return 16;
    if (!snapshot_has(snapshot, "THIRD=three")) return 17;
    if (snapshot_has(snapshot, "SECOND=value")) return 18;
    if (pcc_platform_setenv("OWNED_ORIGINAL", "live", 1) != 0) return 19;
    if (pcc_platform_unsetenv("THIRD") != 0) return 20;
    if (!snapshot_has(snapshot, "OWNED_ORIGINAL=next")) return 21;
    if (!snapshot_has(snapshot, "THIRD=three")) return 22;
    pcc_platform_env_snapshot_free(snapshot);
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


def test_platform_env_owns_copy_set_overwrite_and_unset_semantics(tmp_path):
    obj = _build_platform_object(tmp_path)
    _run_env_harness(tmp_path, "platform_env_llvm", obj)


def test_platform_env_self_backend_runs_same_owned_environment_abi(tmp_path):
    obj = _build_platform_self_object(tmp_path)
    _run_env_harness(tmp_path, "platform_env_self", obj)


def test_platform_env_object_has_only_allocator_and_initial_env_boundary(tmp_path):
    obj = _build_platform_object(tmp_path)
    undefined = subprocess.run(
        ["nm", "-u", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert undefined.returncode == 0, undefined.stdout + undefined.stderr
    if sys.platform == "darwin":
        assert set(undefined.stdout.split()) == {"_environ", "_free", "_malloc"}
    else:
        assert sys.platform.startswith("linux") and platform.machine() == "x86_64"
        assert set(undefined.stdout.split()) == {"free", "malloc"}


def test_linux_platform_env_reads_compiler_owned_initial_envp(tmp_path, monkeypatch):
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
    for symbol in ("environ", "getenv", "setenv", "unsetenv"):
        assert all("@" + symbol + "(" not in line for line in declarations)
    assert "@pcc_initial_envp = global ptr null" in ir_text
    linux_ir = "\n".join(
        'target triple = "x86_64-unknown-linux-gnu"'
        if line.startswith("target triple = ")
        else line
        for line in ir_text.splitlines()
    )
    asm = emit_self_asm(linux_ir, "x86_64-unknown-linux-gnu")
    assert "pcc_initial_envp" in asm
    assert ".extern environ" not in asm


def test_runtime_archive_plan_selects_platform_env_object():
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
    assert "build_py/freestanding_platform_env.o" in archive_line


def test_runtime_archive_c_helpers_consume_owned_platform_environment(
    pcc_runtime_archive,
):
    undefined = subprocess.run(
        ["nm", "-A", "-u", str(pcc_runtime_archive)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert undefined.returncode == 0, undefined.stdout + undefined.stderr
    helper_names = (
        "py_os_path.o",
        "pcc_threads.o",
        "py_runtime_log.o",
        "py_extension_loader.o",
    )
    helper_lines = [
        line
        for line in undefined.stdout.splitlines()
        if any((":" + name + ":") in line for name in helper_names)
    ]
    getenv_symbol = "_getenv" if sys.platform == "darwin" else "getenv"
    platform_symbol = (
        "_pcc_platform_getenv" if sys.platform == "darwin" else "pcc_platform_getenv"
    )
    assert not any(line.rstrip().endswith(" " + getenv_symbol) for line in helper_lines)
    assert any(line.rstrip().endswith(" " + platform_symbol) for line in helper_lines)


def test_default_runtime_routes_os_environment_through_owned_platform_table(
    tmp_path,
    pcc_runtime_archive,
):
    source = tmp_path / "platform_env_runtime_smoke.py"
    executable = tmp_path / "platform_env_runtime_smoke"
    source.write_text(
        "import os\n"
        "def main() -> None:\n"
        "    print(os.getenv('PCC_OWNED_INHERITED'))\n"
        "    os.environ['PCC_OWNED_SET'] = 'next'\n"
        "    print(os.getenv('PCC_OWNED_SET'))\n"
        "    print('PCC_OWNED_SET' in os.environ)\n"
        "    del os.environ['PCC_OWNED_SET']\n"
        "    print(os.getenv('PCC_OWNED_SET', 'missing'))\n"
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
    run_env = dict(os.environ)
    run_env["PCC_OWNED_INHERITED"] = "host-value"
    run = subprocess.run(
        [str(executable)],
        env=run_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout == "host-value\nnext\nTrue\nmissing\n"
    missing = []
    for symbol in (
        "pcc_platform_getenv",
        "pcc_platform_setenv",
        "pcc_platform_unsetenv",
    ):
        decorated = "_" + symbol if sys.platform == "darwin" else symbol
        if not any(
            line.endswith(" T " + decorated) for line in symbols.stdout.splitlines()
        ):
            missing.append(decorated)
    assert not missing, "missing platform env definitions: " + ", ".join(missing)
