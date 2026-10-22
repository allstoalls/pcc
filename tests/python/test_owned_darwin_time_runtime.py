"""The public time provider's calendar ABI must be present on Darwin too.

Host tests cross-emit and link Mach-O objects with the owned toolchain. They
do not claim Darwin execution; the separate integration case requires it.
"""

from pathlib import Path
import os
import platform
import re
import sys
import time

import pytest

from pcc.frontends.python import owned_runtime_build as owned
from pcc.backend.ar_writer import _defined_symbols, write_archive
from pcc.backend.owned_object_emit import emit_owned_object
from pcc.tools import runtime_module_inventory
from tests.python.owned_regression_support import (
    assert_owned_program,
    assert_reference_program,
    explicit_owned_runtime,
)


ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "pcc/runtime"
CALENDAR = "freestanding_time_calendar"
DARWIN = "arm64-apple-darwin"
TARGETS = (
    "x86_64-unknown-linux-gnu",
    "aarch64-unknown-linux-gnu",
    DARWIN,
    "x86_64-pc-windows-msvc",
)
CALENDAR_EXPORTS = {
    "pcc_time_format_floor_div", "pcc_time_format_floor_mod",
    "pcc_time_days_from_civil", "pcc_time_breakdown",
}


def _compile_member(name, target, directory):
    from pcc.ir.optimization.driver import optimize_ir

    output = directory / (name + ".ll")
    owned._compile_runtime_module(
        name, str(RUNTIME / "py" / (name + ".py")), str(output), target,
    )
    text = optimize_ir(output.read_text(), owned.runtime_ir_passes(str(RUNTIME)))
    output.write_text(text)
    data = emit_owned_object(text, target)
    output.with_suffix(".o").write_bytes(data)
    return text, data


def _undefined(data):
    kind, _ = _defined_symbols(data)
    if kind == "macho":
        from pcc.backend import macho_spec as spec
        return {
            row["name"][1:] for row in spec.parse_object(data).symbols()
            if row["n_type"] & spec.N_TYPE == spec.N_UNDF
        }
    if kind == "elf":
        from pcc.backend.elf_x86_64 import parse_relocatable
        return {
            row.name for row in parse_relocatable(data).symbols
            if row.name and row.section_index == 0
        }
    from pcc.backend.coff_x86_64 import parse_object
    return {
        row.name for row in parse_object(data).symbols
        if row.external and not row.section
    }


@pytest.fixture(params=TARGETS)
def calendar_object(request, tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_WITH_THREADS", "0")
    monkeypatch.delenv("PCC_RUNTIME_IR_PASSES", raising=False)
    text, data = _compile_member(CALENDAR, request.param, tmp_path)
    return request.param, text, data


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("threads", (False, True))
def test_time_calendar_member_is_required_by_every_target_runtime(target, threads):
    names = owned.runtime_modules(str(RUNTIME), target, threads)
    assert names.count(CALENDAR) == 1
    assert len(names) == len(set(names))
    if target == DARWIN:
        assert "freestanding_time_format" not in names
    else:
        assert names.count("freestanding_time_format") == 1
    config = {"threads": threads, "refcount": "atomic"}
    manifest = {
        "target_triple": target,
        "members": [
            {"member": name + ".o", "runtime_build_config": dict(config)}
            for name in names
        ],
    }
    assert owned._manifest_matches_config(manifest, str(RUNTIME), target, config)
    manifest["members"] = [
        row for row in manifest["members"] if row["member"] != CALENDAR + ".o"
    ]
    assert not owned._manifest_matches_config(manifest, str(RUNTIME), target, config)


@pytest.mark.parametrize("threads", ("0", "1"))
def test_make_inventory_includes_darwin_calendar(threads, capsys):
    assert runtime_module_inventory.main([
        "--runtime-root", str(RUNTIME), "--target", DARWIN, "--threads", threads,
    ]) == 0
    assert capsys.readouterr().out.split().count(CALENDAR) == 1


def test_calendar_exports_exact_raw_abi_without_libc_dependency(calendar_object):
    target, text, data = calendar_object
    kind, symbols = _defined_symbols(data)
    prefix = "_" if kind == "macho" else ""
    assert set(symbols) == {prefix + name for name in CALENDAR_EXPORTS}
    assert not _undefined(data)
    assert re.search(
        r"^define[^\n]*i64 @pcc_time_breakdown\(i64 %seconds, i64 %offset, "
        r"i64 %daylight, ptr %zone, ptr %output\)", text, re.M,
    )


def test_non_darwin_formatter_links_to_the_same_calendar_abis(
    calendar_object, tmp_path,
):
    target, _, calendar = calendar_object
    if target == DARWIN:
        return  # Darwin deliberately retains libSystem strftime.
    _, formatter = _compile_member("freestanding_time_format", target, tmp_path)
    _, symbols = _defined_symbols(formatter)
    assert not (set(symbols) & CALENDAR_EXPORTS)
    assert "strftime" in symbols
    assert _undefined(formatter) == {
        "malloc", "pcc_time_format_floor_div", "pcc_time_format_floor_mod",
        "pcc_time_days_from_civil",
    }
    # Reversing the archive order must not strand the extracted formatter's
    # helper dependencies. Verify actual archive selection on both ELF ABIs.
    if "linux" in target:
        from pcc.backend.elf_x86_64 import (
            link_static_executable, parse_relocatable, ElfError,
        )
        caller = emit_owned_object("""declare i64 @strftime(ptr, i64, ptr, ptr)
define ptr @malloc(i64 %size) { ret ptr null }
define i32 @main() {
  %result = call i64 @strftime(ptr null, i64 0, ptr null, ptr null)
  %code = trunc i64 %result to i32
  ret i32 %code
}
""", target)
        caller = parse_relocatable(caller)
        for members in (
            [(CALENDAR + ".o", calendar), ("format.o", formatter)],
            [("format.o", formatter), (CALENDAR + ".o", calendar)],
        ):
            image = link_static_executable([caller], archives=[write_archive(members)], entry="main")
            assert image[:4] == b"\x7fELF"
        with pytest.raises(ElfError, match="pcc_time_"):
            link_static_executable(
                [caller], archives=[write_archive([("format.o", formatter)])], entry="main",
            )


def test_darwin_calendar_archive_resolves_missing_symbol_without_strftime(
    tmp_path, monkeypatch,
):
    from pcc.backend import macho_exec, macho_spec as spec
    from pcc.backend.macho_link import LinkError

    _, data = _compile_member(CALENDAR, DARWIN, tmp_path)
    caller = emit_owned_object("""declare i64 @pcc_time_breakdown(i64, i64, i64, ptr, ptr)
define i32 @main() {
  %tm = alloca [64 x i8], align 8
  %status = call i64 @pcc_time_breakdown(i64 0, i64 0, i64 0, ptr null, ptr %tm)
  %result = trunc i64 %status to i32
  ret i32 %result
}
""", DARWIN)
    # No dynamic provider is simulated: this component is fully defined.
    monkeypatch.setattr(macho_exec, "_libsystem_exports_symbol", lambda name: False)
    with pytest.raises(LinkError, match="'_pcc_time_breakdown'"):
        macho_exec.link_executable([caller])
    image = macho_exec.link_executable(
        [caller], archives=[write_archive([(CALENDAR + ".o", data)])],
    )
    (tmp_path / "calendar-link.macho").write_bytes(image)
    assert spec.parse_object(image).header["filetype"] == spec.MH_EXECUTE
    assert not _undefined(image)
    assert "_strftime" not in {row["name"] for row in spec.parse_object(image).symbols()}
    # Adding a strftime consumer still requires the real Darwin library;
    # calendar extraction must not silently replace its implementation.
    formatter_caller = emit_owned_object("""declare i64 @strftime(ptr, i64, ptr, ptr)
define i64 @format_probe() {
  %result = call i64 @strftime(ptr null, i64 0, ptr null, ptr null)
  ret i64 %result
}
""", DARWIN)
    with pytest.raises(LinkError, match="'_strftime'"):
        macho_exec.link_executable(
            [caller, formatter_caller],
            archives=[write_archive([(CALENDAR + ".o", data)])],
        )


@pytest.mark.parametrize("symbol", ("pcc_time_format_floor_div", "pcc_time_format_floor_mod"))
def test_calendar_cross_object_admission_remains_signature_exact(symbol):
    from pcc.frontends.python.pipeline_freestanding import freestanding_allowed_external_symbols

    declaration = 'operation = extern("' + symbol + '", (c_int64, c_int64), c_int64)\n'
    assert symbol in freestanding_allowed_external_symbols(declaration)
    assert symbol not in freestanding_allowed_external_symbols(
        declaration.replace('(c_int64, c_int64)', '(c_ptr, c_int64)'),
    )
    assert symbol not in freestanding_allowed_external_symbols(
        declaration.replace('), c_int64)', '), c_ptr)'),
    )


@pytest.mark.pcc_gate(probe=lambda: sys.platform.startswith("linux") and platform.machine() in ("x86_64", "amd64"))
def test_split_calendar_and_formatter_execute_native_linux(tmp_path):
    from pcc.backend.elf_x86_64 import link_static_executable, parse_relocatable
    from pcc.backend.x86_64_asm_driver import assemble_file
    from pcc.driver.project import TranslationUnit
    from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
    from tests.python.process_timeout import run_process_group_timeout

    target = TARGETS[0]
    objects = [
        (name + ".o", _compile_member(name, target, tmp_path)[1])
        for name in (CALENDAR, "freestanding_time_format")
    ]
    # malloc is a test boundary only: strftime/calendar never call it. The
    # null allocator deliberately rules out a hidden formatting allocation.
    source = r'''
struct tm_record {
    int sec, min, hour, mday, mon, year, wday, yday, isdst;
    long offset;
    char *zone;
};
long pcc_time_breakdown(long, long, long, char *, struct tm_record *);
long strftime(char *, long, char *, struct tm_record *);
void *malloc(unsigned long size) { return (void *)0; }
int equal(char *left, char *right) {
    while (*left && *left == *right) { ++left; ++right; }
    return *left == *right;
}
int main(void) {
    struct tm_record record;
    char buffer[160];
    char *format = "%Y-%m-%d %H:%M:%S %a %j %G-W%V-%u %z %Z %s";
    if (pcc_time_breakdown(0, 0, 0, "GMT", &record)) return 1;
    if (record.year != 70 || record.mon || record.mday != 1 ||
        record.wday != 4 || record.yday || record.offset || record.isdst) return 2;
    if (!strftime(buffer, 160, format, &record)) return 3;
    if (!equal(buffer, "1970-01-01 00:00:00 Thu 001 1970-W01-4 +0000 GMT 0")) return 4;
    if (pcc_time_breakdown(-1, 0, 0, "GMT", &record)) return 5;
    if (!strftime(buffer, 160, format, &record)) return 6;
    if (!equal(buffer, "1969-12-31 23:59:59 Wed 365 1970-W01-3 +0000 GMT -1")) return 7;
    if (pcc_time_breakdown(951782400, 19800, 0, "IST", &record)) return 8;
    if (!strftime(buffer, 160, "%F %T %z %Z", &record)) return 9;
    if (!equal(buffer, "2000-02-29 05:30:00 +0530 IST")) return 10;
    if (strftime(buffer, 1, "%Y", &record) != 0) return 11;
    if (pcc_time_breakdown(9223372036854775807L, 1, 0, "GMT", &record) != -75) return 12;
    return 0;
}
'''
    evaluator = CEvaluator(backend="self", target_triple=target)
    units = evaluator.compile_translation_units(
        [TranslationUnit("calendar.c", "calendar.c", source)],
        use_system_cpp=False, use_compile_cache=False,
    )
    caller = parse_relocatable(emit_owned_object(units[0][1], target))
    start = assemble_file(
        ".intel_syntax noprefix\n.text\n.globl _start\n_start:\n"
        " call main\n mov edi, eax\n mov eax, 60\n syscall\n",
    )
    image = link_static_executable([start, caller], archives=[write_archive(objects)])
    output = tmp_path / "calendar-linux"
    output.write_bytes(image)
    output.chmod(0o755)
    result = run_process_group_timeout([str(output)], timeout=10, env=dict(os.environ, PATH=""))
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def _darwin_time_program():
    # Keeping monotonic in an ordinary imported provider reproduces the user's
    # link boundary, while gmtime executes the previously missing definition.
    # CPython exposes libc's UTC abbreviation (Darwin uses UTC); the owned
    # provider uses GMT. Neither spelling permits a shifted calendar or offset.
    return """import time

def check_gmtime(seconds, expected):
    result = time.gmtime(seconds)
    observed = (seconds, tuple(result), result.tm_zone, result.tm_gmtoff)
    assert tuple(result) == expected, observed
    assert type(result.tm_zone) is str, observed
    assert result.tm_zone in ("GMT", "UTC"), observed
    assert type(result.tm_gmtoff) is int, observed
    assert result.tm_gmtoff == 0, observed

def main():
    assert time.monotonic() > 0
    check_gmtime(0, (1970, 1, 1, 0, 0, 0, 3, 1, 0))
    check_gmtime(-1, (1969, 12, 31, 23, 59, 59, 2, 365, 0))
    check_gmtime(951782400, (2000, 2, 29, 0, 0, 0, 1, 60, 0))
    print("DARWIN_TIME_OK")

main()
"""


def test_darwin_time_program_reference_contract(tmp_path):
    assert_reference_program(_darwin_time_program(), "DARWIN_TIME_OK\n", tmp_path)


@pytest.mark.parametrize("zone", ("GMT", "UTC"))
def test_darwin_time_program_accepts_utc_abbreviations(zone, monkeypatch, capsys):
    reference = time.gmtime
    calls = []

    def gmtime(seconds):
        calls.append(seconds)
        return time.struct_time(tuple(reference(seconds)), {
            "tm_zone": zone, "tm_gmtoff": 0,
        })

    monkeypatch.setattr(time, "gmtime", gmtime)
    exec(_darwin_time_program(), {})
    assert calls == [0, -1, 951782400]
    assert capsys.readouterr().out == "DARWIN_TIME_OK\n"


@pytest.mark.parametrize("seconds", (0, -1, 951782400))
@pytest.mark.parametrize("field, value", (
    *((index, None) for index in range(9)),
    (9, None), (9, ""), (9, "PST"), (9, b"GMT"),
    (10, None), (10, 3600), (10, 0.0),
))
def test_darwin_time_program_rejects_corrupt_records(
    seconds, field, value, monkeypatch,
):
    reference = time.gmtime

    def gmtime(stamp):
        result = reference(stamp)
        if stamp != seconds:
            return result
        fields = list(result) + [result.tm_zone, result.tm_gmtoff]
        fields[field] = fields[field] + 1 if field < 9 else value
        return time.struct_time(fields)

    monkeypatch.setattr(time, "gmtime", gmtime)
    with pytest.raises(AssertionError) as caught:
        exec(_darwin_time_program(), {})
    # Failures must identify the input and actual calendar/zone/offset, so a
    # future runner discrepancy cannot disappear behind a combined assertion.
    observed = caught.value.args[0]
    result = gmtime(seconds)
    assert observed == (seconds, tuple(result), result.tm_zone, result.tm_gmtoff)


@pytest.mark.integration
@pytest.mark.pcc_gate(probe=lambda: sys.platform == "darwin" and platform.machine() == "arm64")
def test_darwin_import_time_and_gmtime_execute(
    tmp_path, explicit_owned_runtime, python_program_compiler, request, capfd,
):
    assert_owned_program(
        _darwin_time_program(), "DARWIN_TIME_OK\n", tmp_path, python_program_compiler,
        request.node.callspec.params["python_program_compiler"],
        explicit_owned_runtime, capfd,
    )
