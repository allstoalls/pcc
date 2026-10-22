from __future__ import annotations

from pathlib import Path
import subprocess

from pcc.frontends.python import pipeline


REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE = REPO_ROOT / "pcc" / "runtime" / "py" / "freestanding_strtoint.py"


def test_freestanding_strtoint_matches_lp64_c_locale_edges(tmp_path: Path) -> None:
    llvm_ir = tmp_path / "freestanding_strtoint.ll"
    object_path = tmp_path / "freestanding_strtoint.o"
    harness = tmp_path / "strtoint_harness.c"
    executable = tmp_path / "strtoint_harness"
    pipeline.compile_python(
        str(SOURCE),
        str(llvm_ir),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    ir_text = llvm_ir.read_text(encoding="utf-8")
    assert "define i64 @strtol(" in ir_text
    assert "define i64 @strtoul(" in ir_text
    assert "@pcc_errno_set" in ir_text

    build = subprocess.run(
        ["clang", "-c", str(llvm_ir), "-o", str(object_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    undefined = subprocess.run(
        ["nm", "-u", str(object_path)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert undefined.returncode == 0, undefined.stdout + undefined.stderr
    assert {line.split()[-1].lstrip("_") for line in undefined.stdout.splitlines()} == {
        "pcc_errno_set"
    }

    harness.write_text(
        r"""
typedef unsigned long uint64_t;
static int seen_errno;
void pcc_errno_set(int value) { seen_errno = value; }
long strtol(const char *, char **, int);
unsigned long strtoul(const char *, char **, int);

int main(void) {
    char *end;
    const char *signed_hex = " \t-0x2a!";
    const char *prefix_only = "0x";
    const char *no_digits = " +";
    const char *positive_overflow = "9223372036854775808x";
    const char *signed_min = "-9223372036854775808";
    const char *unsigned_max = "18446744073709551615";
    const char *unsigned_overflow = "18446744073709551616x";
    if (strtol(signed_hex, &end, 0) != -42 || *end != '!') return 1;
    if (strtol("z", &end, 36) != 35 || *end != 0) return 2;
    if (strtol(prefix_only, &end, 0) != 0 || end != prefix_only + 1) return 3;
    if (strtol(no_digits, &end, 10) != 0 || end != no_digits) return 4;
    seen_errno = 0;
    if (strtol(positive_overflow, &end, 10) != 0x7fffffffffffffffL) return 5;
    if (seen_errno != 34 || *end != 'x') return 6;
    seen_errno = 0;
    if (strtol(signed_min, &end, 10) != (-0x7fffffffffffffffL - 1)) return 7;
    if (seen_errno != 0 || *end != 0) return 8;
    if (strtoul(unsigned_max, &end, 10) != ~(uint64_t)0 || *end != 0) return 9;
    seen_errno = 0;
    if (strtoul(unsigned_overflow, &end, 10) != ~(uint64_t)0) return 10;
    if (seen_errno != 34 || *end != 'x') return 11;
    if (strtoul("-2", &end, 10) != (~(uint64_t)0 - 1)) return 12;
    seen_errno = 0;
    if (strtol("10", &end, 1) != 0 || end[0] != '1') return 13;
    if (seen_errno != 22) return 14;
    return 0;
}
""",
        encoding="utf-8",
    )
    link = subprocess.run(
        [
            "clang",
            "-fno-builtin",
            str(harness),
            str(object_path),
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert link.returncode == 0, link.stdout + link.stderr
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=10
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_production_archive_plan_includes_freestanding_strtoint() -> None:
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
    assert "build_py/freestanding_strtoint.o" in archive_line


def test_strtoint_prefix_lookahead_stays_within_the_c_string(
    tmp_path: Path,
) -> None:
    """Empty/short prefixes at a guard page must not read past their NUL."""

    llvm_ir = tmp_path / "freestanding_strtoint_guard.ll"
    object_path = tmp_path / "freestanding_strtoint_guard.o"
    harness = tmp_path / "strtoint_guard_harness.c"
    executable = tmp_path / "strtoint_guard_harness"
    pipeline.compile_python(
        str(SOURCE),
        str(llvm_ir),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    build = subprocess.run(
        ["clang", "-c", str(llvm_ir), "-o", str(object_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr

    harness.write_text(
        r"""
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

#ifndef MAP_ANON
#define MAP_ANON MAP_ANONYMOUS
#endif

void pcc_errno_set(int value) { (void)value; }
long strtol(const char *, char **, int);

static int probe(const char *input, size_t length, int base, ptrdiff_t end_at) {
    long page = sysconf(_SC_PAGESIZE);
    char *region = mmap(0, (size_t)page * 2, PROT_READ | PROT_WRITE,
                        MAP_PRIVATE | MAP_ANON, -1, 0);
    if (region == MAP_FAILED) return 90;
    if (mprotect(region + page, (size_t)page, PROT_NONE) != 0) return 91;
    char *text = region + page - length - 1;
    memcpy(text, input, length);
    text[length] = 0;
    char *end = 0;
    long value = strtol(text, &end, base);
    int result = value == 0 && end == text + end_at ? 0 : 92;
    munmap(region, (size_t)page * 2);
    return result;
}

int main(void) {
    int result = probe("", 0, 16, 0);
    if (result != 0) return result;
    result = probe("0", 1, 0, 1);
    if (result != 0) return result;
    result = probe("0", 1, 16, 1);
    if (result != 0) return result;
    result = probe("0x", 2, 0, 1);
    if (result != 0) return result;
    return probe("0x", 2, 16, 1);
}
""",
        encoding="utf-8",
    )
    link = subprocess.run(
        [
            "clang",
            "-fno-builtin",
            str(harness),
            str(object_path),
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert link.returncode == 0, link.stdout + link.stderr
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=10
    )
    assert run.returncode == 0, run.stdout + run.stderr
