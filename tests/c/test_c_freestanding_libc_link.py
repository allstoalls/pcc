"""Public C-frontend coverage for the shared pcc-Python libc link mode."""

from __future__ import annotations

import json
import platform
import re
import subprocess
import sys
from pathlib import Path

import pytest

from pcc.cli_core import cli_main
from pcc.evaluater.c_evaluator import CEvaluator, FREESTANDING_C_LIBC_PY_MODULES


@pytest.mark.parametrize(
    "link_args",
    [
        ["-pie"],
        ["-static-pie"],
        ["-Wl,-pie"],
        ["-Wl,--pic-executable"],
        ["-Wl,-static,-pie"],
        ["-Xlinker", "-pie"],
        ["-Xlinker=--pic-executable"],
        ["-shared"],
        ["-Wl,--shared"],
        ["-Wl,-Bshareable"],
        ["-Xlinker", "--relocatable"],
    ],
)
def test_linux_freestanding_libc_rejects_pie_link_args_before_link_setup(
    monkeypatch, link_args
):
    evaluator = CEvaluator(target_triple="x86_64-unknown-linux-gnu")

    def unexpected_link_setup(*_args, **_kwargs):
        raise AssertionError("PIE link args must fail before freestanding setup")

    monkeypatch.setattr(evaluator, "_freestanding_link_inputs", unexpected_link_setup)

    with pytest.raises(RuntimeError, match="fixed ET_EXEC.*PIE/static-PIE"):
        evaluator.run_compiled_translation_units_with_system_cc(
            [],
            optimize=False,
            link_args=link_args,
            freestanding_libc=True,
        )


def test_c_cli_linux_freestanding_libc_rejects_pie_before_compilation(
    tmp_path, monkeypatch, capsys
):
    source = tmp_path / "main.c"
    source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
    monkeypatch.delenv("PCC_RUNTIME_CC", raising=False)
    monkeypatch.delenv("PCC_RUNTIME_HIGH", raising=False)

    def unexpected_compile(*_args, **_kwargs):
        raise AssertionError("PIE link args must fail before C compilation")

    monkeypatch.setattr(CEvaluator, "compile_translation_units", unexpected_compile)

    result = cli_main(
        [
            "--target=x86_64-unknown-linux-gnu",
            "--freestanding-libc",
            "--link-arg=-pie",
            str(source),
        ]
    )

    assert result == 1
    error = capsys.readouterr().err
    assert "fixed ET_EXEC" in error
    assert "PIE/static-PIE" in error
    assert "-pie" in error


def test_linux_freestanding_libc_forwards_non_pie_link_args(
    monkeypatch,
):
    evaluator = CEvaluator(target_triple="x86_64-unknown-linux-gnu")
    link_calls = []

    monkeypatch.setattr(
        evaluator,
        "_freestanding_link_inputs",
        lambda _tmpdir, *, enabled, timeout: (
            ["start.s"],
            ["-nostdlib", "-static", "-no-pie"],
            ["runtime.a"],
        ),
    )

    def fake_run(command, **_kwargs):
        link_calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("pcc.evaluater.c_evaluator.subprocess.run", fake_run)
    allowed = ["-Wl,-Map,freestanding.map", "-no-pie", "./libpie-helper.a"]

    result = evaluator.run_compiled_translation_units_with_system_cc(
        [],
        optimize=False,
        link_args=allowed,
        freestanding_libc=True,
    )

    assert result.returncode == 0
    assert link_calls[0][-4:-1] == allowed


def test_c_cli_freestanding_libc_runs_pcc_python_mem_and_allocator(tmp_path):
    source = tmp_path / "freestanding_consumer.c"
    source.write_text(
        "typedef unsigned long size_t;\n"
        "void *malloc(size_t);\n"
        "void free(void *);\n"
        "void *memcpy(void *, const void *, size_t);\n"
        "size_t strlen(const char *);\n"
        "int main(void) {\n"
        "    const char *source = \"pcc-python\";\n"
        "    char *copy = (char *)malloc(16);\n"
        "    if (copy == (void *)0) return 10;\n"
        "    memcpy(copy, source, 11);\n"
        "    int ok = strlen(copy) == 10 && copy[4] == 'p';\n"
        "    free(copy);\n"
        "    return ok ? 0 : 11;\n"
        "}\n",
        encoding="utf-8",
    )

    result = cli_main(["--backend=self", "--freestanding-libc", str(source)])

    assert result == 0


def test_c_cli_freestanding_libc_link_map_selects_only_pcc_python_libc(
    tmp_path,
):
    source = tmp_path / "link_map_consumer.c"
    link_map = tmp_path / "freestanding.map"
    source.write_text(
        "typedef unsigned long size_t;\n"
        "void *malloc(size_t);\n"
        "void free(void *);\n"
        "size_t strlen(const char *);\n"
        "int main(void) {\n"
        "    char *value = (char *)malloc(8);\n"
        "    if (value == (void *)0) return 20;\n"
        "    value[0] = 'o'; value[1] = 'k'; value[2] = 0;\n"
        "    int ok = strlen(value) == 2;\n"
        "    free(value);\n"
        "    return ok ? 0 : 21;\n"
        "}\n",
        encoding="utf-8",
    )

    if sys.platform == "darwin":
        link_map_arg = "--link-arg=-Wl,-map," + str(link_map)
    else:
        assert sys.platform.startswith("linux")
        assert platform.machine().lower() in {"x86_64", "amd64"}
        link_map_arg = "--link-arg=-Wl,-Map," + str(link_map)

    result = cli_main(
        ["--backend=self", "--freestanding-libc", link_map_arg, str(source)]
    )

    assert result == 0
    ownership = link_map.read_text(encoding="utf-8")
    assert "vendor_" not in ownership
    if sys.platform == "darwin":
        assert "libpy_runtime_pcc_py.a(freestanding_allocator.o)" in ownership
        assert "libpy_runtime_pcc_py.a(freestanding_mem_str.o)" in ownership
        object_paths = {}
        for line in ownership.splitlines():
            match = re.match(r"\[\s*(\d+)\]\s+(.+)$", line)
            if match:
                object_paths[match.group(1)] = match.group(2)
        system_owners = {
            owner
            for owner, path in object_paths.items()
            if path.endswith(".tbd")
        }
        assert {
            Path(object_paths[owner]).name for owner in system_owners
        } == {"libSystem.tbd", "libsystem_kernel.tbd"}

        system_symbols = set()
        symbol_table = ownership.split("# Symbols:\n", 1)[1]
        for line in symbol_table.splitlines():
            match = re.search(r"\[\s*(\d+)\]\s+(\S+)$", line)
            if match and match.group(1) in system_owners:
                system_symbols.add(match.group(2))
        # The contract is *which* libc the program still depends on, not how
        # the linker spells each reference.  `_mmap.got` and `_mmap.stub` are
        # the indirect-address and the call-thunk entry for one symbol, so
        # pinning the suffix made a linker that emits both fail a test about
        # library ownership.  Compare the symbols; keep the set exact so a
        # third system symbol still fails.
        system_symbol_names = {
            name.rsplit(".", 1)[0] if name.endswith((".got", ".stub")) else name
            for name in system_symbols
        }
        assert system_symbol_names == {"_mmap", "_munmap"}, system_symbols
    else:
        assert "libpcc_freestanding_c.a(freestanding_allocator.o)" in ownership
        assert "libpcc_freestanding_c.a(freestanding_mem_str.o)" in ownership
        assert "libc.a" not in ownership


def test_c_freestanding_libc_link_view_lists_only_pcc_python_sources():
    runtime_py = Path(__file__).resolve().parents[2] / "pcc" / "py_runtime" / "py"
    assert FREESTANDING_C_LIBC_PY_MODULES == (
        "freestanding_mem_str",
        "freestanding_allocator",
        "freestanding_platform_io",
        "freestanding_platform_fs",
        "freestanding_platform_env",
        "freestanding_platform_system",
        "freestanding_platform_time",
        "freestanding_platform_process",
        "freestanding_platform_socket",
        "freestanding_stdio",
    )
    for module_name in FREESTANDING_C_LIBC_PY_MODULES:
        source = runtime_py / (module_name + ".py")
        assert source.is_file(), module_name
        assert "__pcc_freestanding__ = True" in source.read_text(encoding="utf-8")
        assert not source.with_suffix(".c").exists(), module_name
        assert not source.with_suffix(".s").exists(), module_name


def test_c_cli_freestanding_libc_runs_representative_project_consumers(tmp_path):
    sqlite_path = tmp_path / "sqlite-style.bin"
    consumers = {
        "lua": r"""
typedef unsigned long size_t;
void *calloc(size_t, size_t);
void *realloc(void *, size_t);
void free(void *);
void *memcpy(void *, const void *, size_t);
void *memmove(void *, const void *, size_t);
int strcmp(const char *, const char *);

int main(void) {
    char *buffer = (char *)calloc(8, 1);
    if (buffer == (void *)0) return 40;
    memcpy(buffer, "lua", 4);
    buffer = (char *)realloc(buffer, 16);
    if (buffer == (void *)0) return 41;
    memmove(buffer + 4, buffer, 4);
    int ok = strcmp(buffer, "lua") == 0 && strcmp(buffer + 4, "lua") == 0;
    free(buffer);
    return ok ? 0 : 42;
}
""",
        "sqlite": r"""
typedef unsigned long size_t;
typedef void FILE;
int snprintf(char *, size_t, const char *, ...);
FILE *fopen(const char *, const char *);
size_t fwrite(const void *, size_t, size_t, FILE *);
size_t fread(void *, size_t, size_t, FILE *);
int fclose(FILE *);
int remove(const char *);

int main(void) {
    const char *path = __PCC_SQLITE_PATH__;
    char row[16];
    if (snprintf(row, sizeof(row), "%s:%d", "row", 7) != 5) return 50;
    FILE *stream = fopen(path, "wb");
    if (stream == (void *)0) return 51;
    if (fwrite(row, 1, 5, stream) != 5 || fclose(stream) != 0) return 52;
    stream = fopen(path, "rb");
    if (stream == (void *)0) return 53;
    char copy[8] = {0};
    int ok = fread(copy, 1, 5, stream) == 5 && copy[3] == ':' && copy[4] == '7';
    if (fclose(stream) != 0 || remove(path) != 0) return 54;
    return ok ? 0 : 55;
}
""".replace("__PCC_SQLITE_PATH__", json.dumps(str(sqlite_path))),
        "zlib": r"""
typedef unsigned long size_t;
void *memset(void *, int, size_t);
void *memcpy(void *, const void *, size_t);
void *memmove(void *, const void *, size_t);
int memcmp(const void *, const void *, size_t);

int main(void) {
    unsigned char window[32];
    unsigned char expected[8] = {1, 2, 3, 4, 1, 2, 3, 4};
    unsigned char seed[4] = {1, 2, 3, 4};
    memset(window, 0, sizeof(window));
    memcpy(window, seed, sizeof(seed));
    memmove(window + 4, window, sizeof(seed));
    return memcmp(window, expected, sizeof(expected)) == 0 ? 0 : 60;
}
""",
    }

    for name, text in consumers.items():
        source = tmp_path / (name + "_style.c")
        source.write_text(text, encoding="utf-8")
        result = cli_main(["--backend=self", "--freestanding-libc", str(source)])
        assert result == 0, name


def test_c_cli_freestanding_libc_rejects_non_pcc_python_runtime(
    tmp_path, monkeypatch, capsys
):
    source = tmp_path / "main.c"
    source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
    monkeypatch.setenv("PCC_RUNTIME_CC", "cc")

    result = cli_main(["--freestanding-libc", str(source)])

    assert result == 1
    assert "requires PCC_RUNTIME_CC=pcc" in capsys.readouterr().err


def test_c_cli_freestanding_libc_rejects_python_and_emit_only(
    tmp_path, capsys
):
    python_source = tmp_path / "main.py"
    python_source.write_text("print('no')\n", encoding="utf-8")
    c_source = tmp_path / "main.c"
    c_source.write_text("int main(void) { return 0; }\n", encoding="utf-8")

    assert cli_main(["--freestanding-libc", str(python_source)]) == 1
    assert cli_main(
        [
            "--freestanding-libc",
            "--emit-obj",
            str(tmp_path / "main.o"),
            str(c_source),
        ]
    ) == 1

    errors = capsys.readouterr().err
    assert "only valid for C inputs" in errors
    assert "requires a final link/run" in errors
