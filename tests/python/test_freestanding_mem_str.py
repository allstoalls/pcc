from pathlib import Path
import subprocess

from pcc.frontends.python import pipeline
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
from pcc.driver.project import TranslationUnit


REPO_ROOT = Path(__file__).resolve().parents[2]
MEM_STR_SOURCE = REPO_ROOT / "pcc" / "runtime" / "py" / "freestanding_mem_str.py"
MEM_STR_SYMBOLS = (
    "memcpy",
    "memmove",
    "memset",
    "bzero",
    "explicit_bzero",
    "memcmp",
    "memchr",
    "memrchr",
    "strlen",
    "strnlen",
    "strchrnul",
    "strchr",
    "strrchr",
    "strcmp",
    "strncmp",
)


def _compile_mem_str_ir(tmp_path: Path) -> Path:
    out = tmp_path / "freestanding_mem_str.ll"
    pipeline.compile_python(
        str(MEM_STR_SOURCE),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    return out


def _build_mem_str_object(tmp_path: Path) -> Path:
    llvm_ir = _compile_mem_str_ir(tmp_path)
    obj = tmp_path / "freestanding_mem_str.o"
    build = subprocess.run(
        ["clang", "-c", str(llvm_ir), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return obj


def _build_mem_str_self_object(tmp_path: Path) -> Path:
    from pcc.backend.self_backend_dispatch import emit_self_asm

    llvm_ir = _compile_mem_str_ir(tmp_path)
    asm = tmp_path / "freestanding_mem_str.s"
    obj = tmp_path / "freestanding_mem_str_self.o"
    asm.write_text(emit_self_asm(llvm_ir.read_text(encoding="utf-8")), encoding="utf-8")
    build = subprocess.run(
        ["clang", "-c", str(asm), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return obj


def _compile_and_run_c(tmp_path: Path, name: str, source: str, *objects: Path) -> str:
    harness = tmp_path / (name + ".c")
    executable = tmp_path / name
    harness.write_text(source, encoding="utf-8")
    build = subprocess.run(
        [
            "clang",
            "-fno-builtin",
            str(harness),
            *(str(obj) for obj in objects),
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    return run.stdout


def _compile_and_run_c_through_freestanding_route(
    tmp_path: Path, name: str, source: str
) -> str:
    unit = TranslationUnit(
        name=name + ".c",
        path=str(tmp_path / (name + ".c")),
        source=source,
    )
    result = CEvaluator(
        backend="self",
        allow_unimplemented_backend=True,
    ).run_translation_units_with_system_cc(
        [unit],
        optimize=0,
        base_dir=str(tmp_path),
        use_system_cpp=True,
        freestanding_libc=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_freestanding_mem_str_compiles_to_closed_c_abi_object(tmp_path):
    obj = _build_mem_str_object(tmp_path)

    symbols = subprocess.run(
        ["nm", "-g", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert symbols.returncode == 0, symbols.stdout + symbols.stderr
    for symbol in MEM_STR_SYMBOLS:
        assert "_" + symbol in symbols.stdout

    undefined = subprocess.run(
        ["nm", "-u", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert undefined.returncode == 0, undefined.stdout + undefined.stderr
    assert undefined.stdout.strip() == ""


def test_freestanding_mem_str_matches_host_libc_for_portable_c_surface(tmp_path):
    source = r"""
#include <stddef.h>
#include <stdio.h>
#include <string.h>

static int signum(int value) {
    return (value > 0) - (value < 0);
}

int main(void) {
    unsigned char bytes[12] = {0, 1, 127, 128, 200, 255, 3, 4, 5, 6, 7, 8};
    unsigned char copy[12];
    memset(copy, 0x5a, sizeof(copy));
    memcpy(copy + 2, bytes + 1, 7);
    printf("copy:%u,%u,%u,%u\n", copy[0], copy[2], copy[5], copy[8]);
    printf("cmp:%d,%d,%d\n",
           signum(memcmp(bytes, copy + 1, 0)),
           signum(memcmp(bytes + 1, copy + 2, 7)),
           signum(memcmp(bytes + 3, bytes + 4, 1)));
    printf("memchr:%td,%td,%d\n",
           (unsigned char *)memchr(bytes, 200, sizeof(bytes)) - bytes,
           (unsigned char *)memchr(bytes, 0, sizeof(bytes)) - bytes,
           memchr(bytes, 99, sizeof(bytes)) == NULL);

    char right[20] = "abcdefgh";
    memmove(right + 2, right, 7);
    printf("right:%s\n", right);
    char left[20] = "abcdefgh";
    memmove(left, left + 2, 7);
    printf("left:%s\n", left);

    const char *text = "abca";
    printf("str:%zu,%td,%td,%td,%td\n",
           strlen(text),
           strchr(text, 'a') - text,
           strrchr(text, 'a') - text,
           strchr(text, '\0') - text,
           strrchr(text, '\0') - text);
    printf("strcmp:%d,%d,%d,%d\n",
           signum(strcmp("abc", "abc")),
           signum(strcmp("abc", "abd")),
           signum(strncmp("abz", "aba", 2)),
           signum(strncmp("abz", "aba", 3)));
    return 0;
}
"""
    host = _compile_and_run_c(tmp_path, "host_oracle", source)
    freestanding = _compile_and_run_c(
        tmp_path,
        "freestanding_candidate",
        source,
        _build_mem_str_object(tmp_path),
    )
    assert freestanding == host
    routed = _compile_and_run_c_through_freestanding_route(
        tmp_path,
        "c_frontend_freestanding_candidate",
        source,
    )
    assert routed == host


def test_freestanding_mem_str_extended_surface_and_zero_length_edges(tmp_path):
    source = r"""
#include <stddef.h>
#include <stdio.h>

void bzero(void *, size_t);
void explicit_bzero(void *, size_t);
void *memcpy(void *, const void *, size_t);
void *memmove(void *, const void *, size_t);
void *memset(void *, int, size_t);
void *memrchr(const void *, int, size_t);
size_t strnlen(const char *, size_t);
char *strchrnul(const char *, int);

int main(void) {
    unsigned char bytes[8] = {9, 2, 9, 4, 9, 0, 7, 9};
    printf("memrchr:%td,%td,%d,%d\n",
           (unsigned char *)memrchr(bytes, 9, sizeof(bytes)) - bytes,
           (unsigned char *)memrchr(bytes, 9, 5) - bytes,
           memrchr(bytes, 3, sizeof(bytes)) == NULL,
           memrchr(bytes, 9, 0) == NULL);

    const char *text = "abc";
    printf("bounded:%zu,%zu,%zu\n",
           strnlen(text, 0), strnlen(text, 2), strnlen(text, 20));
    printf("nul:%td,%td,%td\n",
           strchrnul(text, 'b') - text,
           strchrnul(text, 'z') - text,
           strchrnul(text, '\0') - text);

    unsigned char cleared[6] = {1, 2, 3, 4, 5, 6};
    bzero(cleared + 1, 3);
    explicit_bzero(cleared + 4, 2);
    printf("clear:%u,%u,%u,%u,%u,%u\n",
           cleared[0], cleared[1], cleared[2],
           cleared[3], cleared[4], cleared[5]);

    unsigned char dst[4] = {0, 0, 0, 0};
    unsigned char src[4] = {8, 7, 6, 5};
    printf("returns:%d,%d,%d\n",
           memcpy(dst, src, 0) == dst,
           memmove(dst, src, 0) == dst,
           memset(dst, 0, 0) == dst);
    return 0;
}
"""
    output = _compile_and_run_c(
        tmp_path,
        "freestanding_extended",
        source,
        _build_mem_str_object(tmp_path),
    )
    assert output == (
        "memrchr:7,4,1,1\n"
        "bounded:0,2,3\n"
        "nul:1,3,3\n"
        "clear:1,0,0,0,0,0\n"
        "returns:1,1,1\n"
    )


def test_self_backend_mem_str_object_is_closed_and_c_callable(tmp_path):
    obj = _build_mem_str_self_object(tmp_path)
    undefined = subprocess.run(
        ["nm", "-u", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert undefined.returncode == 0, undefined.stdout + undefined.stderr
    assert undefined.stdout.strip() == ""

    source = r"""
#include <stddef.h>
#include <stdio.h>
#include <string.h>

int main(void) {
    char value[16] = "abcdef";
    memmove(value + 1, value, 6);
    printf("%s:%zu:%d:%d\n",
           value,
           strlen(value),
           memcmp(value, "aabcdef", 8),
           strcmp(value, "aabcdef"));
    return 0;
}
"""
    output = _compile_and_run_c(tmp_path, "self_backend_consumer", source, obj)
    assert output == "aabcdef:7:0:0\n"


def test_pcc_c_frontend_object_resolves_mem_str_to_pcc_python_object(tmp_path):
    source = (
        "typedef unsigned long size_t;\n"
        "void *memcpy(void *, const void *, size_t);\n"
        "void *memmove(void *, const void *, size_t);\n"
        "int memcmp(const void *, const void *, size_t);\n"
        "size_t strlen(const char *);\n"
        "int main(void) {\n"
        "  char src[8] = {97,98,99,100,101,102,0,0};\n"
        "  char dst[8] = {0};\n"
        "  if (memcpy(dst, src, 7) != dst) return 10;\n"
        "  memmove(dst + 1, dst, 6);\n"
        "  if (strlen(dst) != 7) return 11;\n"
        "  return memcmp(dst, \"aabcdef\", 8) != 0;\n"
        "}\n"
    )
    unit = TranslationUnit(
        name="consumer.c",
        path=str(tmp_path / "consumer.c"),
        source=source,
    )
    evaluator = CEvaluator(backend="self", allow_unimplemented_backend=True)
    compiled = evaluator.compile_translation_units(
        [unit],
        use_system_cpp=False,
        frontend_opt_level=0,
    )
    consumer_obj = tmp_path / "pcc_c_consumer.o"
    evaluator.emit_compiled_units(compiled, emit_obj=str(consumer_obj), optimize=0)

    unresolved = subprocess.run(
        ["nm", "-u", str(consumer_obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert unresolved.returncode == 0, unresolved.stdout + unresolved.stderr
    assert "_memcpy" in unresolved.stdout
    assert "_memmove" in unresolved.stdout
    assert "_memcmp" in unresolved.stdout
    assert "_strlen" in unresolved.stdout

    executable = tmp_path / "pcc_c_with_pcc_python_mem_str"
    link = subprocess.run(
        [
            "clang",
            str(consumer_obj),
            str(_build_mem_str_self_object(tmp_path)),
            "-o",
            str(executable),
        ],
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

    linked_symbols = subprocess.run(
        ["nm", "-g", str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert linked_symbols.returncode == 0, linked_symbols.stdout + linked_symbols.stderr
    for symbol in ("memcpy", "memmove", "memcmp", "strlen"):
        assert " T _" + symbol in linked_symbols.stdout


def test_freestanding_mem_str_matches_host_across_size_alignment_overlap_matrix(
    tmp_path,
):
    source = r"""
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

static uint64_t digest = UINT64_C(1469598103934665603);

static void mix_u64(uint64_t value) {
    digest ^= value;
    digest *= UINT64_C(1099511628211);
}

static void mix_bytes(const unsigned char *data, size_t size) {
    size_t i;
    for (i = 0; i < size; ++i) mix_u64(data[i]);
}

static int signum(int value) {
    return (value > 0) - (value < 0);
}

int main(void) {
    unsigned char src[192];
    unsigned char dst[192];
    unsigned char work[192];
    size_t i, n, src_off, dst_off;
    int shift;

    for (i = 0; i < sizeof(src); ++i) src[i] = (unsigned char)(i * 37u + 11u);
    for (n = 0; n <= 80; ++n) {
        for (src_off = 0; src_off < 8; ++src_off) {
            for (dst_off = 0; dst_off < 8; ++dst_off) {
                for (i = 0; i < sizeof(dst); ++i) dst[i] = (unsigned char)(i ^ 0xa5u);
                mix_u64(memcpy(dst + dst_off, src + src_off, n) == dst + dst_off);
                mix_bytes(dst, sizeof(dst));
                mix_u64((uint64_t)(signum(memcmp(dst + dst_off, src + src_off, n)) + 1));
            }
        }
    }

    for (n = 0; n <= 80; ++n) {
        for (shift = -24; shift <= 24; ++shift) {
            size_t from = 64;
            size_t to = (size_t)((int)from + shift);
            for (i = 0; i < sizeof(work); ++i) work[i] = (unsigned char)(i * 13u + n);
            mix_u64(memmove(work + to, work + from, n) == work + to);
            mix_bytes(work, sizeof(work));
        }
    }

    for (n = 0; n <= 96; ++n) {
        int value = (int)n * 19 - 700;
        for (i = 0; i < sizeof(dst); ++i) dst[i] = (unsigned char)(255u - i);
        mix_u64(memset(dst + 3, value, n) == dst + 3);
        mix_bytes(dst, sizeof(dst));
        mix_u64(memchr(dst + 3, value, n) == dst + 3);
        mix_u64(memchr(dst + 3, value + 1, n) == NULL);
    }

    for (n = 0; n <= 96; ++n) {
        char text[128];
        for (i = 0; i < n; ++i) text[i] = (char)('a' + (i % 23));
        text[n] = '\0';
        mix_u64(strlen(text));
        mix_u64(strnlen(text, n / 2));
        mix_u64(strnlen(text, n + 9));
        mix_u64((uint64_t)(strchr(text, 'm') ? strchr(text, 'm') - text + 1 : 0));
        mix_u64((uint64_t)(strrchr(text, 'm') ? strrchr(text, 'm') - text + 1 : 0));
        mix_u64((uint64_t)(signum(strcmp(text, text)) + 1));
        mix_u64((uint64_t)(signum(strncmp(text, "abcdefghijklmnopqrstuvw", n)) + 1));
    }

    printf("%016llx\n", (unsigned long long)digest);
    return 0;
}
"""
    host = _compile_and_run_c(tmp_path, "matrix_host", source)
    freestanding = _compile_and_run_c(
        tmp_path,
        "matrix_freestanding",
        source,
        _build_mem_str_object(tmp_path),
    )
    assert freestanding == host


def test_explicit_bzero_remains_an_explicit_store_body(tmp_path):
    ir_text = _compile_mem_str_ir(tmp_path).read_text(encoding="utf-8")
    body = ir_text.split("@explicit_bzero", 1)[1].split("}\n", 1)[0]
    assert "store i8" in body
    assert "call ptr @memset" not in body
    assert "call void @bzero" not in body


def test_pcc_python_runtime_archive_plan_selects_python_mem_str_object():
    runtime_dir = REPO_ROOT / "pcc" / "runtime"
    plan = subprocess.run(
        ["make", "-B", "-n", "libpy_runtime_pcc_py.a"],
        cwd=runtime_dir,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert plan.returncode == 0, plan.stdout + plan.stderr
    archive_lines = [
        line for line in plan.stdout.splitlines()
        if "ar rcs libpy_runtime_pcc_py.a.tmp" in line
    ]
    assert len(archive_lines) == 1
    archive_line = archive_lines[0]
    assert "build_py/freestanding_mem_str.o" in archive_line
    for symbol in MEM_STR_SYMBOLS:
        assert "build_pcc/vendor_" + symbol + ".o" not in archive_line
