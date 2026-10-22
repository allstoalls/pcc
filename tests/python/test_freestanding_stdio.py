from pathlib import Path
import platform
import struct
import subprocess
import sys

from pcc.frontends.python import pipeline
from tests.owned_runtime_c_fixture import (
    runtime_ir, runtime_member, object_symbols, link_c_harness,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
STDIO_SOURCE = REPO_ROOT / "pcc" / "runtime" / "py" / "freestanding_stdio.py"
ALLOCATOR_SOURCE = (
    REPO_ROOT / "pcc" / "runtime" / "py" / "freestanding_allocator.py"
)


def _compile_stdio_ir(tmp_path: Path) -> Path:
    return runtime_ir(STDIO_SOURCE, tmp_path / "freestanding_stdio.ll")


def _build_stdio_object(tmp_path: Path) -> Path:
    return runtime_member(tmp_path, "freestanding_stdio")


def _build_stdio_self_object(tmp_path: Path) -> Path:
    # Admission verifies every member was emitted by the owned object writer.
    return runtime_member(tmp_path, "freestanding_stdio")


def _build_allocator_object(tmp_path: Path) -> Path:
    return runtime_member(tmp_path, "freestanding_allocator")


def test_remove_owns_file_and_empty_directory_semantics(tmp_path):
    obj = _build_stdio_object(tmp_path)
    symbols = object_symbols(obj, undefined=False)
    assert symbols.returncode == 0, symbols.stdout + symbols.stderr
    decorated_remove = "_remove" if sys.platform == "darwin" else "remove"
    assert any(line.endswith(" T " + decorated_remove) for line in symbols.stdout.splitlines())

    undefined = object_symbols(obj, undefined=True)
    assert undefined.returncode == 0, undefined.stdout + undefined.stderr
    if sys.platform == "darwin":
        assert set(undefined.stdout.split()) == {
            "___error",
            "_close",
            "_environ",
            "_fcntl",
            "_free",
            "_lseek",
            "_malloc",
            "_open",
            "_pcc_errno_get",
            "_pcc_errno_set",
            "_pipe",
            "_posix_spawn",
            "_posix_spawn_file_actions_addclose",
            "_posix_spawn_file_actions_adddup2",
            "_posix_spawn_file_actions_destroy",
            "_posix_spawn_file_actions_init",
            "_read",
            "_unlinkat",
            "_waitpid",
            "_write",
        }
    else:
        assert sys.platform.startswith("linux") and platform.machine() == "x86_64"
        assert set(undefined.stdout.split()) == {
            "free",
            "malloc",
            "pcc_initial_envp",
            "pcc_errno_get",
            "pcc_errno_set",
        }

    file_path = tmp_path / "remove-file.txt"
    directory_path = tmp_path / "remove-directory"
    missing_path = tmp_path / "remove-missing"
    nonempty_path = tmp_path / "remove-nonempty"
    file_path.write_text("owned\n", encoding="utf-8")
    directory_path.mkdir()
    nonempty_path.mkdir()
    (nonempty_path / "child").write_text("keep\n", encoding="utf-8")

    harness = tmp_path / "remove_harness.c"
    executable = tmp_path / "remove_harness"
    harness.write_text(
        r"""
int remove(const char *path);

int main(int argc, char **argv) {
    if (argc != 5) return 1;
    if (remove(argv[1]) != 0) return 2;
    if (remove(argv[2]) != 0) return 3;
    if (remove(argv[3]) == 0) return 4;
    if (remove(argv[4]) == 0) return 5;
    return 0;
}
""",
        encoding="utf-8",
    )
    link = link_c_harness(harness, executable, defines=())
    assert link.returncode == 0, link.stdout + link.stderr
    run = subprocess.run(
        [
            str(executable),
            str(file_path),
            str(directory_path),
            str(missing_path),
            str(nonempty_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert not file_path.exists()
    assert not directory_path.exists()
    assert nonempty_path.is_dir()


def test_default_runtime_archive_selects_freestanding_stdio_object():
    from pcc.frontends.python.owned_runtime_build import runtime_modules
    from pcc.frontends.python.pipeline_targets import host_target_triple
    assert "freestanding_stdio" in runtime_modules(str(REPO_ROOT / "pcc/runtime"), host_target_triple())


def test_basic_file_lifecycle_matches_c_stdio_contract(tmp_path):
    obj = _build_stdio_object(tmp_path)
    allocator = _build_allocator_object(tmp_path)
    symbols = object_symbols(obj, undefined=False)
    assert symbols.returncode == 0, symbols.stdout + symbols.stderr
    prefix = "_" if sys.platform == "darwin" else ""
    for symbol in (
        "fopen",
        "fwrite",
        "fread",
        "fseek",
        "ftell",
        "fflush",
        "ferror",
        "fclose",
    ):
        assert any(
            line.endswith(" T " + prefix + symbol)
            for line in symbols.stdout.splitlines()
        ), symbol
    file_path = tmp_path / "owned-basic.bin"
    harness = tmp_path / "stdio_basic_harness.c"
    executable = tmp_path / "stdio_basic_harness"
    harness.write_text(
        r"""
typedef unsigned long size_t;
typedef void FILE;

FILE *fopen(const char *path, const char *mode);
size_t fwrite(const void *ptr, size_t size, size_t count, FILE *stream);
size_t fread(void *ptr, size_t size, size_t count, FILE *stream);
int fseek(FILE *stream, long offset, int whence);
long ftell(FILE *stream);
int fflush(FILE *stream);
int ferror(FILE *stream);
int fclose(FILE *stream);

int main(int argc, char **argv) {
    const char payload[5] = {'o', 'w', 'n', 'e', 'd'};
    char readback[8] = {0, 0, 0, 0, 0, 0, 0, 0};
    FILE *file;
    if (argc != 2) return 1;
    file = fopen(argv[1], "w+b");
    if (!file) return 2;
    if (fwrite(payload, 1, 5, file) != 5) return 3;
    if (ftell(file) != 5) return 4;
    if (fseek(file, 0, 0) != 0) return 5;
    if (ftell(file) != 0) return 6;
    if (fread(readback, 1, 5, file) != 5) return 7;
    if (readback[0] != 'o' || readback[4] != 'd') return 8;
    if (fseek(file, -2, 2) != 0) return 9;
    if (ftell(file) != 3) return 10;
    if (ferror(file) != 0) return 11;
    if (fclose(file) != 0) return 12;

    file = fopen(argv[1], "rb");
    if (!file) return 13;
    if (fread(readback, 1, sizeof(readback), file) != 5) return 14;
    if (readback[0] != 'o' || readback[4] != 'd') return 15;
    if (fread(readback, 1, 1, file) != 0) return 16;
    if (ferror(file) != 0) return 17;
    if (fclose(file) != 0) return 18;
    if (fopen(argv[1], "invalid") != (FILE *)0) return 19;
    return 0;
}
""",
        encoding="utf-8",
    )
    link = link_c_harness(harness, executable, defines=())
    assert link.returncode == 0, link.stdout + link.stderr
    run = subprocess.run(
        [str(executable), str(file_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert file_path.read_bytes() == b"owned"


def test_append_positioning_matches_guarded_c_stdio_oracle(tmp_path):
    obj = _build_stdio_object(tmp_path)
    allocator = _build_allocator_object(tmp_path)
    harness = tmp_path / "stdio_append_position_harness.c"
    oracle_executable = tmp_path / "stdio_append_position_oracle"
    owned_executable = tmp_path / "stdio_append_position_owned"
    harness.write_text(
        r"""
#include <fcntl.h>
#include <stddef.h>
#include <unistd.h>

#if defined(PCC_OWNED_STDIO)
typedef void PCC_FILE;
PCC_FILE *fopen(const char *path, const char *mode);
size_t fwrite(const void *ptr, size_t size, size_t count, PCC_FILE *stream);
size_t fread(void *ptr, size_t size, size_t count, PCC_FILE *stream);
int fseek(PCC_FILE *stream, long offset, int whence);
long ftell(PCC_FILE *stream);
int fflush(PCC_FILE *stream);
int fclose(PCC_FILE *stream);
#else
#include <stdio.h>
typedef FILE PCC_FILE;
#endif

static long observations[32];
static int observation_count = 0;

static int write_all(int fd, const void *raw, size_t length) {
    const char *bytes = (const char *)raw;
    size_t offset = 0;
    while (offset < length) {
        long written = (long)write(fd, bytes + offset, length - offset);
        if (written <= 0) return -1;
        offset += (size_t)written;
    }
    return 0;
}

static int seed_file(const char *path, const char *payload, size_t length) {
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    int result = 0;
    if (fd < 0) return -1;
    if (length > 0 && write_all(fd, payload, length) != 0) result = -1;
    if (close(fd) != 0) result = -1;
    return result;
}

static int record_position(PCC_FILE *stream) {
    long position = ftell(stream);
    if (position < 0 || observation_count >= 32) return -1;
    observations[observation_count++] = position;
    return 0;
}

static int scenario_a_existing(const char *path) {
    PCC_FILE *stream;
    if (seed_file(path, "seed", 4) != 0) return 1;
    stream = fopen(path, "a");
    if (!stream) return 2;
    if (fwrite("XY", 1, 2, stream) != 2) return 3;
    if (record_position(stream) != 0) return 4;
    if (fseek(stream, 1, 0) != 0) return 5;
    if (record_position(stream) != 0) return 6;
    if (fwrite("Z", 1, 1, stream) != 1) return 7;
    if (record_position(stream) != 0) return 8;
    if (fflush(stream) != 0) return 9;
    if (record_position(stream) != 0) return 10;
    if (fseek(stream, -2, 2) != 0) return 11;
    if (record_position(stream) != 0) return 12;
    if (fwrite("Q", 1, 1, stream) != 1) return 13;
    if (record_position(stream) != 0) return 14;
    if (fclose(stream) != 0) return 15;
    return 0;
}

static int scenario_a_empty(const char *path) {
    PCC_FILE *stream;
    if (seed_file(path, "", 0) != 0) return 1;
    stream = fopen(path, "a");
    if (!stream) return 2;
    if (fwrite("AB", 1, 2, stream) != 2) return 3;
    if (record_position(stream) != 0) return 4;
    if (fseek(stream, 0, 0) != 0) return 5;
    if (record_position(stream) != 0) return 6;
    if (fwrite("C", 1, 1, stream) != 1) return 7;
    if (record_position(stream) != 0) return 8;
    if (fclose(stream) != 0) return 9;
    return 0;
}

static int scenario_aplus_existing(const char *path) {
    PCC_FILE *stream;
    char first = 0;
    char contents[5] = {0, 0, 0, 0, 0};
    if (seed_file(path, "seed", 4) != 0) return 1;
    stream = fopen(path, "a+");
    if (!stream) return 2;
    if (fseek(stream, 0, 0) != 0) return 3;
    if (fread(&first, 1, 1, stream) != 1 || first != 's') return 4;
    if (record_position(stream) != 0) return 5;
    if (fseek(stream, 1, 0) != 0) return 6;
    if (fwrite("M", 1, 1, stream) != 1) return 7;
    if (record_position(stream) != 0) return 8;
    if (fflush(stream) != 0) return 9;
    if (record_position(stream) != 0) return 10;
    if (fseek(stream, 0, 0) != 0) return 11;
    if (fread(contents, 1, 5, stream) != 5) return 12;
    if (contents[0] != 's' || contents[3] != 'd' || contents[4] != 'M') return 13;
    if (record_position(stream) != 0) return 14;
    if (fseek(stream, 2, 0) != 0) return 15;
    if (fwrite("N", 1, 1, stream) != 1) return 16;
    if (record_position(stream) != 0) return 17;
    if (fseek(stream, 0, 1) != 0) return 18;
    if (record_position(stream) != 0) return 19;
    if (fclose(stream) != 0) return 20;
    return 0;
}

static int scenario_aplus_empty(const char *path) {
    PCC_FILE *stream;
    char value = 0;
    if (seed_file(path, "", 0) != 0) return 1;
    stream = fopen(path, "a+");
    if (!stream) return 2;
    if (fseek(stream, 0, 0) != 0) return 3;
    if (fwrite("U", 1, 1, stream) != 1) return 4;
    if (record_position(stream) != 0) return 5;
    if (fflush(stream) != 0) return 6;
    if (fseek(stream, 0, 0) != 0) return 7;
    if (fread(&value, 1, 1, stream) != 1 || value != 'U') return 8;
    if (record_position(stream) != 0) return 9;
    if (fseek(stream, 0, 0) != 0) return 10;
    if (fwrite("V", 1, 1, stream) != 1) return 11;
    if (record_position(stream) != 0) return 12;
    if (fclose(stream) != 0) return 13;
    return 0;
}

int main(int argc, char **argv) {
    int fd;
    if (argc != 6) return 90;
    if (scenario_a_existing(argv[1]) != 0) return 91;
    if (scenario_a_empty(argv[2]) != 0) return 92;
    if (scenario_aplus_existing(argv[3]) != 0) return 93;
    if (scenario_aplus_empty(argv[4]) != 0) return 94;
    if (observation_count != 18) return 95;
    fd = open(argv[5], O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd < 0) return 96;
    if (write_all(fd, observations, sizeof(long) * 18) != 0) return 97;
    if (close(fd) != 0) return 98;
    return 0;
}
""",
        encoding="utf-8",
    )
    oracle_link = subprocess.run(
        [
            "clang",
            "-fno-builtin",
            str(harness),
            "-o",
            str(oracle_executable),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert oracle_link.returncode == 0, oracle_link.stdout + oracle_link.stderr
    owned_link = link_c_harness(harness, owned_executable, defines=('PCC_OWNED_STDIO=1',))
    assert owned_link.returncode == 0, owned_link.stdout + owned_link.stderr

    results = {}
    for owner, executable in (
        ("oracle", oracle_executable),
        ("owned", owned_executable),
    ):
        run_root = tmp_path / owner
        run_root.mkdir()
        paths = {
            "a_existing": run_root / "a-existing.bin",
            "a_empty": run_root / "a-empty.bin",
            "aplus_existing": run_root / "aplus-existing.bin",
            "aplus_empty": run_root / "aplus-empty.bin",
            "observations": run_root / "observations.bin",
        }
        run = subprocess.run(
            [
                str(executable),
                str(paths["a_existing"]),
                str(paths["a_empty"]),
                str(paths["aplus_existing"]),
                str(paths["aplus_empty"]),
                str(paths["observations"]),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert run.returncode == 0, run.stdout + run.stderr
        results[owner] = {
            name: path.read_bytes()
            for name, path in paths.items()
        }

    assert results["owned"] == results["oracle"]
    expected_positions = (
        6,
        1,
        2,
        7,
        5,
        6,
        2,
        0,
        1,
        1,
        2,
        5,
        5,
        3,
        3,
        1,
        1,
        1,
    )
    assert (
        struct.unpack("=18q", results["owned"]["observations"])
        == expected_positions
    )
    assert results["owned"]["a_existing"] == b"seedXYZQ"
    assert results["owned"]["a_empty"] == b"ABC"
    assert results["owned"]["aplus_existing"] == b"seedMN"
    assert results["owned"]["aplus_empty"] == b"UV"


def test_linux_stdio_seek_is_owned_by_raw_syscall_intrinsic(tmp_path, monkeypatch):
    from pcc.frontends.python.codegen.unsafe_lowering import UnsafeIntrinsicMixin

    monkeypatch.setattr(
        UnsafeIntrinsicMixin, "_target_sys_platform_text", lambda self: "linux"
    )
    monkeypatch.setattr(
        UnsafeIntrinsicMixin, "_target_machine_text", lambda self: "x86_64"
    )
    llvm_ir = tmp_path / "freestanding_stdio_linux.ll"
    runtime_ir(STDIO_SOURCE, llvm_ir, "x86_64-unknown-linux-gnu")
    text = llvm_ir.read_text(encoding="utf-8")
    import re
    assert re.search(r"define (?:external )?i32 @fseek\(", text)
    assert re.search(r"define (?:external )?i64 @ftell\(", text)
    assert "@lseek" not in text
    assert "unsafe.seek_file.syscall" in text
    assert 'asm sideeffect "syscall"' in text


def test_owned_integer_string_formatting_and_fprintf(tmp_path):
    obj = _build_stdio_object(tmp_path)
    allocator = _build_allocator_object(tmp_path)
    symbols = object_symbols(obj, undefined=False)
    assert symbols.returncode == 0, symbols.stdout + symbols.stderr
    prefix = "_" if sys.platform == "darwin" else ""
    for symbol in ("snprintf", "vsnprintf", "fprintf"):
        assert any(
            line.endswith(" T " + prefix + symbol)
            for line in symbols.stdout.splitlines()
        ), symbol

    output_path = tmp_path / "formatted.txt"
    harness = tmp_path / "stdio_format_harness.c"
    executable = tmp_path / "stdio_format_harness"
    harness.write_text(
        r"""
#include <stdarg.h>
#include <stddef.h>
#include <string.h>
typedef void FILE;

int snprintf(char *out, size_t size, const char *format, ...);
int vsnprintf(char *out, size_t size, const char *format, va_list ap);
int fprintf(FILE *stream, const char *format, ...);
FILE *fopen(const char *path, const char *mode);
int fclose(FILE *stream);

static int call_vsnprintf(char *out, size_t size, const char *format, ...) {
    int result;
    va_list ap;
    va_start(ap, format);
    result = vsnprintf(out, size, format, ap);
    va_end(ap);
    return result;
}

int main(int argc, char **argv) {
    char out[128];
    char via_v[128];
    char small[5] = {'x', 'x', 'x', 'x', 'x'};
    FILE *stream;
    int result;
    if (argc != 2) return 1;
    result = snprintf(
        out, sizeof(out),
        "name=%s signed=%d unsigned=%u hex=%x/%X oct=%o char=%c %%",
        "pcc", -42, 42u, 42u, 42u, 42u, '!'
    );
    if (result != (int)strlen(out)) return 2;
    if (strcmp(out, "name=pcc signed=-42 unsigned=42 hex=2a/2A oct=52 char=! %") != 0) return 3;
    if (call_vsnprintf(via_v, sizeof(via_v), "%s:%d:%x", "owned", 7, 255u) != 10) return 4;
    if (strcmp(via_v, "owned:7:ff") != 0) return 5;
    if (snprintf(small, sizeof(small), "abcdef") != 6) return 6;
    if (small[0] != 'a' || small[3] != 'd' || small[4] != '\0') return 7;
    if (snprintf((char *)0, 0, "x:%d", 123) != 5) return 8;
    stream = fopen(argv[1], "w");
    if (!stream) return 9;
    if (fprintf(stream, "%s:%d:%X\n", "owned", -7, 255u) != 12) return 10;
    if (fclose(stream) != 0) return 11;
    return 0;
}
""",
        encoding="utf-8",
    )
    link = link_c_harness(harness, executable, defines=())
    assert link.returncode == 0, link.stdout + link.stderr
    run = subprocess.run(
        [str(executable), str(output_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert output_path.read_text(encoding="utf-8") == "owned:-7:FF\n"


def test_owned_format_width_precision_flags_and_pointer(tmp_path):
    obj = _build_stdio_object(tmp_path)
    allocator = _build_allocator_object(tmp_path)
    harness = tmp_path / "stdio_format_width_harness.c"
    executable = tmp_path / "stdio_format_width_harness"
    harness.write_text(
        r"""
#include <stddef.h>
#include <string.h>
int snprintf(char *out, size_t size, const char *format, ...);

int main(void) {
    char out[256];
    int result = snprintf(
        out,
        sizeof(out),
        "[%08x][%-6s][%+d][% d][%#x][%#o][%.4d][%.*s][%*u][%p]",
        42u, "xy", 7, 7, 42u, 42u, 7, 3, "abcdef", 5, 9u,
        (void *)0x1234
    );
    const char *expected =
        "[0000002a][xy    ][+7][ 7][0x2a][052][0007][abc][    9][0x1234]";
    if (result != (int)strlen(expected)) return 1;
    if (strcmp(out, expected) != 0) return 2;
    return 0;
}
""",
        encoding="utf-8",
    )
    link = link_c_harness(harness, executable, defines=())
    assert link.returncode == 0, link.stdout + link.stderr
    run = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_owned_float_formatting_fixed_scientific_general_and_specials(tmp_path):
    obj = _build_stdio_object(tmp_path)
    allocator = _build_allocator_object(tmp_path)
    harness = tmp_path / "stdio_float_format_harness.c"
    executable = tmp_path / "stdio_float_format_harness"
    harness.write_text(
        r"""
#include <math.h>
#include <stddef.h>
#include <string.h>
#include <unistd.h>
int snprintf(char *out, size_t size, const char *format, ...);

int main(void) {
    char out[256];
    const char *expected =
        "3.500000|3.14|1.23e+03|1234|+1.5|00001.50|INF|nan";
    int result = snprintf(
        out,
        sizeof(out),
        "%f|%.2f|%.2e|%.4g|%+.1f|%08.2f|%F|%g",
        3.5, 3.14159, 1234.0, 1234.0, 1.5, 1.5, INFINITY, NAN
    );
    if (result != (int)strlen(expected) || strcmp(out, expected) != 0) {
        write(2, out, (size_t)result);
        write(2, "\n", 1);
        return 2;
    }
    return 0;
}
""",
        encoding="utf-8",
    )
    link = link_c_harness(harness, executable, defines=())
    assert link.returncode == 0, link.stdout + link.stderr
    run = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_self_backend_owns_mixed_snprintf_abi(tmp_path):
    obj = _build_stdio_self_object(tmp_path)
    harness = tmp_path / "stdio_self_format_harness.c"
    executable = tmp_path / "stdio_self_format_harness"
    harness.write_text(
        r"""
#include <stddef.h>
#include <string.h>
int snprintf(char *out, size_t size, const char *format, ...);
int main(void) {
    char out[64];
    int result = snprintf(out, sizeof(out), "%s:%d:%.2f", "self", -7, 3.5);
    if (result != 12) return 1;
    return strcmp(out, "self:-7:3.50") == 0 ? 0 : 2;
}
""",
        encoding="utf-8",
    )
    link = link_c_harness(harness, executable, defines=())
    assert link.returncode == 0, link.stdout + link.stderr
    run = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_owned_popen_pclose_pipe_modes_and_child_status(tmp_path):
    obj = _build_stdio_object(tmp_path)
    allocator = _build_allocator_object(tmp_path)
    harness = tmp_path / "stdio_popen_harness.c"
    executable = tmp_path / "stdio_popen_harness"
    harness.write_text(
        r"""
#include <stddef.h>
#include <sys/wait.h>
typedef void FILE;
FILE *popen(const char *command, const char *mode);
int pclose(FILE *stream);
size_t fread(void *ptr, size_t size, size_t count, FILE *stream);
size_t fwrite(const void *ptr, size_t size, size_t count, FILE *stream);

int main(void) {
    char buffer[8] = {0, 0, 0, 0, 0, 0, 0, 0};
    const char payload[5] = {'o', 'w', 'n', 'e', 'd'};
    FILE *stream = popen("printf owned", "r");
    int status;
    if (!stream) return 1;
    if (fread(buffer, 1, 2, stream) != 2) return 2;
    if (fread(buffer + 2, 1, 6, stream) != 3) return 3;
    if (buffer[0] != 'o' || buffer[4] != 'd') return 4;
    status = pclose(stream);
    if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) return 5;

    stream = popen("cat >/dev/null", "w");
    if (!stream) return 6;
    if (fwrite(payload, 1, 5, stream) != 5) return 7;
    status = pclose(stream);
    if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) return 8;

    stream = popen("exit 7", "r");
    if (!stream) return 9;
    status = pclose(stream);
    if (!WIFEXITED(status) || WEXITSTATUS(status) != 7) return 10;
    if (popen("true", "invalid") != (FILE *)0) return 11;
    return 0;
}
""",
        encoding="utf-8",
    )
    link = link_c_harness(harness, executable, defines=())
    assert link.returncode == 0, link.stdout + link.stderr
    run = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_self_backend_owns_popen_pipe_and_wait_status(tmp_path):
    obj = _build_stdio_self_object(tmp_path)
    harness = tmp_path / "stdio_self_popen_harness.c"
    executable = tmp_path / "stdio_self_popen_harness"
    harness.write_text(
        r"""
#include <stddef.h>
#include <sys/wait.h>
typedef void FILE;
FILE *popen(const char *command, const char *mode);
int pclose(FILE *stream);
size_t fread(void *ptr, size_t size, size_t count, FILE *stream);
int main(void) {
    char out[5] = {0, 0, 0, 0, 0};
    FILE *stream = popen("printf self", "r");
    int status;
    if (!stream) return 1;
    if (fread(out, 1, 4, stream) != 4) return 2;
    status = pclose(stream);
    if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) return 3;
    return out[0] == 's' && out[3] == 'f' ? 0 : 4;
}
""",
        encoding="utf-8",
    )
    link = link_c_harness(harness, executable, defines=())
    assert link.returncode == 0, link.stdout + link.stderr
    run = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_owned_output_buffer_flush_and_close_ordering(tmp_path):
    obj = _build_stdio_object(tmp_path)
    allocator = _build_allocator_object(tmp_path)
    first_path = tmp_path / "buffered-visible.bin"
    second_path = tmp_path / "buffered-close.bin"
    harness = tmp_path / "stdio_buffering_harness.c"
    executable = tmp_path / "stdio_buffering_harness"
    harness.write_text(
        r"""
#include <fcntl.h>
#include <stddef.h>
#include <unistd.h>
typedef void FILE;
FILE *fopen(const char *path, const char *mode);
size_t fwrite(const void *ptr, size_t size, size_t count, FILE *stream);
int fflush(FILE *stream);
int fclose(FILE *stream);

static int raw_size(const char *path) {
    char buffer[32];
    int fd = open(path, O_RDONLY);
    int total = 0;
    int got;
    if (fd < 0) return -1;
    while ((got = (int)read(fd, buffer, sizeof(buffer))) > 0) total += got;
    close(fd);
    return total;
}

int main(int argc, char **argv) {
    const char payload[5] = {'o', 'w', 'n', 'e', 'd'};
    FILE *stream;
    if (argc != 3) return 1;
    stream = fopen(argv[1], "w");
    if (!stream) return 2;
    if (fwrite(payload, 1, 5, stream) != 5) return 3;
    if (raw_size(argv[1]) != 0) return 4;
    if (fflush(stream) != 0) return 5;
    if (raw_size(argv[1]) != 5) return 6;
    if (fclose(stream) != 0) return 7;

    stream = fopen(argv[2], "w");
    if (!stream) return 8;
    if (fwrite(payload, 1, 5, stream) != 5) return 9;
    if (fclose(stream) != 0) return 10;
    if (raw_size(argv[2]) != 5) return 11;
    return 0;
}
""",
        encoding="utf-8",
    )
    link = link_c_harness(harness, executable, defines=())
    assert link.returncode == 0, link.stdout + link.stderr
    run = subprocess.run(
        [str(executable), str(first_path), str(second_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_owned_fwrite_reports_partial_nonblocking_pipe_and_error_state(tmp_path):
    obj = _build_stdio_object(tmp_path)
    allocator = _build_allocator_object(tmp_path)
    harness = tmp_path / "stdio_partial_write_harness.c"
    executable = tmp_path / "stdio_partial_write_harness"
    harness.write_text(
        r"""
#include <fcntl.h>
#include <stddef.h>
#include <stdlib.h>
#include <unistd.h>
#include "pcc_stdio_abi.h"

size_t fwrite(const void *ptr, size_t size, size_t count, void *stream);
int ferror(void *stream);

int main(void) {
    const size_t total = 1024u * 1024u;
    int fds[2];
    char *payload;
    size_t written;
    PccOwnedFile stream;
    if (pipe(fds) != 0) return 1;
    if (fcntl(fds[1], F_SETFL, O_NONBLOCK) != 0) return 2;
    payload = (char *)malloc(total);
    if (!payload) return 3;
    stream.magic = PCC_STDIO_FILE_MAGIC;
    stream.fd = fds[1];
    stream.flags = PCC_STDIO_FLAG_WRITABLE;
    stream.aux = 0;
    stream.buffer = (void *)0;
    stream.buffer_capacity = 0;
    stream.buffer_length = 0;
    stream.buffer_position = 0;
    written = fwrite(payload, 1, total, &stream);
    free(payload);
    close(fds[0]);
    close(fds[1]);
    if (written == 0 || written >= total) return 4;
    if (!ferror(&stream)) return 5;
    return 0;
}
""",
        encoding="utf-8",
    )
    link = link_c_harness(harness, executable, defines=())
    assert link.returncode == 0, link.stdout + link.stderr
    run = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_fgetc_preserves_unsigned_bytes_and_eof_state(tmp_path):
    obj = _build_stdio_object(tmp_path)
    allocator = _build_allocator_object(tmp_path)
    symbols = object_symbols(obj, undefined=False)
    assert symbols.returncode == 0, symbols.stdout + symbols.stderr
    decorated = ("_" if sys.platform == "darwin" else "") + "fgetc"
    assert any(line.endswith(" T " + decorated) for line in symbols.stdout.splitlines())

    file_path = tmp_path / "owned-fgetc.bin"
    file_path.write_bytes(bytes((65, 255)))
    harness = tmp_path / "fgetc_harness.c"
    executable = tmp_path / "fgetc_harness"
    harness.write_text(
        r"""
typedef void FILE;
FILE *fopen(const char *path, const char *mode);
int fgetc(FILE *stream);
int ferror(FILE *stream);
int fclose(FILE *stream);

int main(int argc, char **argv) {
    FILE *file;
    if (argc != 2) return 1;
    file = fopen(argv[1], "rb");
    if (!file) return 2;
    if (fgetc(file) != 65) return 3;
    if (fgetc(file) != 255) return 4;
    if (fgetc(file) != -1) return 5;
    if (ferror(file) != 0) return 6;
    if (fclose(file) != 0) return 7;
    return 0;
}
""",
        encoding="utf-8",
    )
    link = link_c_harness(harness, executable, defines=())
    assert link.returncode == 0, link.stdout + link.stderr
    run = subprocess.run(
        [str(executable), str(file_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_standard_stream_globals_are_owned_and_stderr_writes_fd_two(tmp_path):
    obj = _build_stdio_object(tmp_path)
    allocator = _build_allocator_object(tmp_path)
    symbols = object_symbols(obj, undefined=False)
    assert symbols.returncode == 0, symbols.stdout + symbols.stderr
    if sys.platform == "darwin":
        required = {"___stdinp", "___stdoutp", "___stderrp"}
    else:
        required = {"stdin", "stdout", "stderr"}
    defined = {
        line.split()[-1]
        for line in symbols.stdout.splitlines()
        if " S " in line or " D " in line
    }
    assert required <= defined

    harness = tmp_path / "stderr_harness.c"
    executable = tmp_path / "stderr_harness"
    harness.write_text(
        r"""
typedef unsigned long size_t;
typedef void FILE;
#ifdef __APPLE__
extern FILE *__stderrp;
#define PCC_STDERR __stderrp
#else
extern FILE *stderr;
#define PCC_STDERR stderr
#endif
size_t fwrite(const void *ptr, size_t size, size_t count, FILE *stream);
int fflush(FILE *stream);

int main(void) {
    const char payload[5] = {'o', 'w', 'n', 'e', 'd'};
    if (fwrite(payload, 1, 5, PCC_STDERR) != 5) return 1;
    if (fflush(PCC_STDERR) != 0) return 2;
    return 0;
}
""",
        encoding="utf-8",
    )
    link = link_c_harness(harness, executable, defines=())
    assert link.returncode == 0, link.stdout + link.stderr
    run = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stderr == "owned"
