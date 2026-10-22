from pathlib import Path
import platform
import subprocess
import sys

import pytest

from pcc.frontends.python import pipeline


REPO_ROOT = Path(__file__).resolve().parents[2]
PLATFORM_SOURCE = (
    REPO_ROOT / "pcc" / "runtime" / "py" / "freestanding_platform_fs.py"
)
PLATFORM_SYMBOLS = (
    "pcc_platform_access",
    "pcc_platform_getcwd",
    "pcc_platform_stat_kind",
    "pcc_platform_stat_mtime",
    "pcc_platform_realpath",
    "pcc_platform_mkdtemp",
)


def _compile_platform_ir(tmp_path: Path) -> Path:
    out = tmp_path / "freestanding_platform_fs.ll"
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
    obj = tmp_path / "freestanding_platform_fs.o"
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
    asm = tmp_path / "freestanding_platform_fs.s"
    obj = tmp_path / "freestanding_platform_fs_self.o"
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
    probe = tmp_path / "owned-stat-probe.txt"
    probe.write_text("owned\n", encoding="utf-8")
    harness = tmp_path / (name + ".c")
    executable = tmp_path / name
    harness.write_text(
        r"""
#include <stdint.h>
#include <string.h>
#include <unistd.h>

long pcc_platform_access(const char *path, long mode);
void *pcc_platform_getcwd(char *buffer, long size);
long pcc_platform_stat_kind(const char *path);
double pcc_platform_stat_mtime(const char *path);

int main(int argc, char **argv) {
    char owned[4096];
    char native[4096];
    if (argc != 2) return 1;
    if (pcc_platform_getcwd(owned, sizeof(owned)) != owned) return 2;
    if (getcwd(native, sizeof(native)) != native) return 3;
    if (strcmp(owned, native) != 0) return 4;
    if (pcc_platform_access(argv[1], 4) != 0) return 5;
    if (pcc_platform_stat_kind(argv[1]) != 1) return 6;
    if (pcc_platform_stat_kind(".") != 2) return 7;
    if (pcc_platform_stat_kind("owned-definitely-missing") != 0) return 8;
    if (!(pcc_platform_stat_mtime(argv[1]) > 0.0)) return 9;
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
        [str(executable), str(probe)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr


def _run_realpath_harness(tmp_path: Path, name: str, obj: Path) -> None:
    root = tmp_path / (name + "-tree")
    nested = root / "target" / "nested"
    nested.mkdir(parents=True)
    (nested / "probe.txt").write_text("owned\n", encoding="utf-8")
    (root / "alias").symlink_to("target/nested")
    input_path = root / "alias" / ".." / "nested" / "probe.txt"
    harness = tmp_path / (name + ".c")
    executable = tmp_path / name
    harness.write_text(
        r"""
#include <stdlib.h>
#include <string.h>

void *pcc_platform_realpath(const char *path, char *buffer, long size);

int main(int argc, char **argv) {
    char owned[8192];
    char native[8192];
    if (argc != 2) return 1;
    if (pcc_platform_realpath(argv[1], owned, sizeof(owned)) != owned) return 2;
    if (realpath(argv[1], native) != native) return 3;
    if (strcmp(owned, native) != 0) return 4;
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
        [str(executable), str(input_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr


def _run_mkdtemp_harness(tmp_path: Path, name: str, obj: Path) -> None:
    harness = tmp_path / (name + ".c")
    executable = tmp_path / name
    template_prefix = tmp_path / (name + "-owned-")
    harness.write_text(
        r"""
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

void *pcc_platform_mkdtemp(char *path_template);

int main(int argc, char **argv) {
    char first[8192];
    char second[8192];
    struct stat st;
    if (argc != 2) return 1;
    if (snprintf(first, sizeof(first), "%sXXXXXX", argv[1]) <= 0) return 2;
    if (snprintf(second, sizeof(second), "%sXXXXXX", argv[1]) <= 0) return 3;
    if (pcc_platform_mkdtemp(first) != first) return 4;
    if (pcc_platform_mkdtemp(second) != second) return 5;
    if (strcmp(first, second) == 0) return 6;
    if (stat(first, &st) != 0 || !S_ISDIR(st.st_mode) || (st.st_mode & 0777) != 0700) return 7;
    if (stat(second, &st) != 0 || !S_ISDIR(st.st_mode) || (st.st_mode & 0777) != 0700) return 8;
    if (rmdir(first) != 0 || rmdir(second) != 0) return 9;
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
        [str(executable), str(template_prefix)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_platform_fs_object_owns_path_queries_with_platform_boundary(tmp_path):
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
        assert set(undefined.stdout.split()) == {
            # path queries
            "_access",
            "_getcwd",
            "_getpid",
            "_mkdir",
            "_readlink",
            "_stat",
            # mkstemp-style exclusive files and directory-tree removal
            "_chmod",
            "_openat",
            "_opendir",
            "_readdir",
            "_closedir",
            "_unlinkat",
            "___error",
            "_malloc",
            "_free",
        }
    else:
        assert sys.platform.startswith("linux") and platform.machine() == "x86_64"
        assert undefined.stdout.strip() == ""
    _run_platform_harness(tmp_path, "platform_fs_llvm", obj)


def test_platform_fs_self_backend_runs_same_path_query_abi(tmp_path):
    obj = _build_platform_self_object(tmp_path)
    _run_platform_harness(tmp_path, "platform_fs_self", obj)
    _run_realpath_harness(tmp_path, "platform_fs_realpath_self", obj)
    _run_mkdtemp_harness(tmp_path, "platform_fs_mkdtemp_self", obj)


def test_platform_fs_realpath_resolves_relative_symlink_components(tmp_path):
    obj = _build_platform_object(tmp_path)
    _run_realpath_harness(tmp_path, "platform_fs_realpath", obj)


def test_platform_fs_mkdtemp_creates_unique_directories(tmp_path):
    obj = _build_platform_object(tmp_path)
    _run_mkdtemp_harness(tmp_path, "platform_fs_mkdtemp", obj)


def test_linux_platform_fs_lowers_to_raw_syscalls(tmp_path, monkeypatch):
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
    for symbol in ("access", "getcwd", "mkdir", "readlink", "stat"):
        assert all("@" + symbol + "(" not in line for line in declarations)
    assert ir_text.count("syscall") >= 6
    linux_ir = "\n".join(
        'target triple = "x86_64-unknown-linux-gnu"'
        if line.startswith("target triple = ")
        else line
        for line in ir_text.splitlines()
    )
    asm = emit_self_asm(linux_ir, "x86_64-unknown-linux-gnu")
    assert asm.count("syscall") >= 6


def test_runtime_archive_plan_selects_platform_fs_object():
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
    assert "build_py/freestanding_platform_fs.o" in archive_line


def test_default_runtime_routes_path_queries_through_platform_fs_object(
    tmp_path,
    pcc_runtime_archive,
):
    source = tmp_path / "platform_fs_runtime_smoke.py"
    executable = tmp_path / "platform_fs_runtime_smoke"
    run_cwd = tmp_path / "runtime-cwd"
    run_cwd.mkdir()
    (run_cwd / "probe.txt").write_text("owned\n", encoding="utf-8")
    nested = run_cwd / "target" / "nested"
    nested.mkdir(parents=True)
    (nested / "probe.txt").write_text("owned\n", encoding="utf-8")
    (run_cwd / "alias").symlink_to("target/nested")
    source.write_text(
        "import os\n"
        "import tempfile\n"
        "def main() -> None:\n"
        "    print(os.getcwd().endswith('runtime-cwd'))\n"
        "    print(os.access('probe.txt', 4))\n"
        "    print(os.path.isfile('probe.txt'))\n"
        "    print(os.path.isdir('.'))\n"
        "    print(os.path.getmtime('probe.txt') > 0.0)\n"
        "    print(os.path.realpath('alias/../nested/probe.txt').endswith('target/nested/probe.txt'))\n"
        "    temp_path = ''\n"
        "    with tempfile.TemporaryDirectory(prefix='pcc_owned_') as temp:\n"
        "        temp_path = temp\n"
        "        print(os.path.isdir(temp))\n"
        "        print('pcc_owned_' in temp)\n"
        "    print(os.path.exists(temp_path))\n"
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
    for symbol in PLATFORM_SYMBOLS:
        decorated = "_" + symbol if sys.platform == "darwin" else symbol
        assert any(line.endswith(" T " + decorated) for line in symbols.stdout.splitlines())
    run = subprocess.run(
        [str(executable)],
        cwd=run_cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout == (
        "True\nTrue\nTrue\nTrue\nTrue\nTrue\nTrue\nTrue\nFalse\n"
    )


@pytest.mark.parametrize('target', ['x86_64-unknown-linux-gnu','aarch64-unknown-linux-gnu','arm64-apple-macosx11.0'])
def test_complete_platform_module_owns_new_export(tmp_path, target):
    import re
    from pcc.frontends.python.pipeline import compile_python
    from tests.owned_ir_validation import verify_ir_text
    source=PLATFORM_SOURCE
    output=tmp_path/'platform.ll'
    compile_python(str(source),str(output),emit_llvm_only=True,python_library=True,libpython_mode='off',target_triple=target)
    text=output.read_text();verify_ir_text(text)
    assert re.search(r'define[^\n]*i64 @pcc_platform_mkstemp_suffix\(ptr ',text)
    assert not re.search(r'\bcall[^\n]*@py_cpy_',text)
