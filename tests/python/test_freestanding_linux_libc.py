"""Owned ELF probes with a real admitted runtime closure; no host linker/libc."""
from __future__ import annotations

import platform
import stat
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pcc.backend.elf_x86_64 import link_static_executable, parse_relocatable, parse_static_executable
from pcc.backend.owned_object_emit import emit_owned_object
from pcc.frontends.python import pipeline
from tests.owned_runtime_c_fixture import admitted_runtime, runtime_ir

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "pcc/runtime/py/freestanding_linux_libc.py"
TARGETS = ("x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu")


def _object(source: Path, directory: Path, target: str) -> tuple[str, bytes]:
    output = directory / (source.stem + "." + target + ".ll")
    runtime_ir(source, output, target)
    text = output.read_text()
    data = emit_owned_object(text, target)
    output.with_suffix(".o").write_bytes(data)
    return text, data


# Test-only errno sink isolates publication at the explicit runtime boundary.
# It is deliberately not a replacement runtime or production TLS owner.
_LEAF_BOUNDARIES = '''
from pcc import i64
from pcc.extern import c_abi_export, c_abi_typed_export
from pcc.unsafe import define_global_i32, global_addr, load_i32, store_i32, syscall6
__pcc_freestanding__ = True
define_global_i32("leaf_errno", 0)
@c_abi_typed_export("pcc_errno_set", "void", ("i32",))
def errno_set(value: i64) -> None:
    store_i32(global_addr("leaf_errno"), 0, value)
@c_abi_typed_export("pcc_errno_get", "i32", ())
def errno_get() -> i64:
    return load_i32(global_addr("leaf_errno"), 0)
@c_abi_export("pcc_platform_abort")
def abort() -> None:
    syscall6(60, 134, 0, 0, 0, 0, 0)
'''


def _run_leaf(tmp_path: Path, source: Path, harness_text: str) -> bytes:
    target = TARGETS[0]
    harness = tmp_path / "leaf.py"
    # The freestanding verifier deliberately admits only finite production
    # cross-object boundaries. Keep this test entry and errno sink in the
    # same verified closure as the unchanged owner source, rather than
    # widening that production allowlist just for a probe.
    harness.write_text(source.read_text() + "\n" +
        _LEAF_BOUNDARIES.replace("__pcc_freestanding__ = True", "") + "\n" +
        harness_text.replace("__pcc_freestanding__ = True", ""))
    objects = [_object(harness, tmp_path, target)[1]]
    # The original errno sink remains a publication boundary control, not a
    # replacement runtime. New allocator/stdio/environment dependencies come
    # exclusively from the admitted owned archive; no test stubs satisfy them.
    image = link_static_executable(
        [parse_relocatable(data) for data in objects],
        archives=[admitted_runtime().read_bytes()],
    )
    parse_static_executable(image)
    executable = tmp_path / "leaf"
    executable.write_bytes(image)
    executable.chmod(0o755)
    run = subprocess.run([str(executable)], capture_output=True, timeout=30)
    assert run.returncode == 0, (run.returncode, run.stdout, run.stderr)
    assert run.stderr == b""
    return run.stdout


@pytest.mark.parametrize("target", TARGETS)
def test_linux_file_exports_have_owned_cross_target_abi(tmp_path, target):
    from pcc.frontends.python.owned_runtime_build import runtime_modules

    text, data = _object(SOURCE, tmp_path, target)
    assert "define external i32 @chmod(ptr " in text
    assert "define external i32 @utime(ptr " in text
    obj = parse_relocatable(data)
    defined = {symbol.name for symbol in obj.symbols if symbol.section_index != 0}
    undefined = {symbol.name for symbol in obj.symbols if symbol.section_index == 0 and symbol.name}
    assert {"chmod", "utime"} <= defined
    assert undefined == {
        "pcc_errno_set", "pcc_platform_abort", "malloc", "free", "fflush",
        "__fini_array_start", "__fini_array_end",
    }
    assert "freestanding_linux_libc" in runtime_modules(str(ROOT / "pcc/runtime"), target)


@pytest.mark.pcc_gate(probe=lambda: sys.platform.startswith("linux") and platform.machine() in ("x86_64", "amd64"))
def test_linux_file_exports_execute_mode_time_symlink_and_errno(tmp_path):
    regular = tmp_path / "regular"
    linked = tmp_path / "linked"
    link = tmp_path / "symlink"
    negative = tmp_path / "negative"
    current = tmp_path / "current"
    for path in (regular, linked, negative, current):
        path.write_bytes(b"file")
        path.chmod(0o644)
    link.symlink_to(linked)
    directory = tmp_path / "directory"
    directory.mkdir(mode=0o755)
    missing = tmp_path / "missing"
    not_directory = regular / "child"
    operations = [
        f"chmod(cstr({str(regular)!r}), 416)",
        f"chmod(cstr({str(link)!r}), 384)",
        f"chmod(cstr({str(directory)!r}), 448)",
        f"chmod(cstr({str(missing)!r}), 384)",
        f"chmod(cstr({str(not_directory)!r}), 384)",
        "chmod(null(), 384)",
        f"utime(cstr({str(regular)!r}), positive)",
        f"utime(cstr({str(link)!r}), positive)",
        f"utime(cstr({str(negative)!r}), before_epoch)",
        f"utime(cstr({str(current)!r}), null())",
        f"utime(cstr({str(missing)!r}), positive)",
        f"utime(cstr({str(not_directory)!r}), positive)",
        "utime(null(), null())",
    ]
    body = "\n".join(
        f"    errno_set(123)\n    result = {operation}\n"
        f"    store_i64(output, 0, result)\n    store_i64(output, 8, errno_get())\n"
        f"    syscall6(1, 1, output, 16, 0, 0, 0)"
        for operation in operations
    )
    source = f'''
from pcc import i64
from pcc.extern import extern, c_abi_export, c_int32, c_ptr, c_void
from pcc.unsafe import cstr, null, stack_alloc, store_i64, syscall6
__pcc_freestanding__ = True
chmod = extern("chmod", (c_ptr, c_int32), c_int32)
utime = extern("utime", (c_ptr, c_ptr), c_int32)
errno_set = extern("pcc_errno_set", (c_int32,), c_void)
errno_get = extern("pcc_errno_get", (), c_int32)
@c_abi_export("_start")
def start(initial_stack) -> None:
    output = stack_alloc(16)
    positive = stack_alloc(16)
    before_epoch = stack_alloc(16)
    store_i64(positive, 0, 123456789)
    store_i64(positive, 8, 2200000000)
    store_i64(before_epoch, 0, -1)
    store_i64(before_epoch, 8, -2)
{body}
    syscall6(60, 0, 0, 0, 0, 0, 0)
'''
    before = time.time_ns()
    output = _run_leaf(tmp_path, SOURCE, source)
    after = time.time_ns()
    assert list(struct.iter_unpack("<qq", output)) == [
        (0, 123), (0, 123), (0, 123), (-1, 2), (-1, 20), (-1, 14),
        (0, 123), (0, 123), (0, 123), (0, 123), (-1, 2), (-1, 20), (-1, 14),
    ]
    assert stat.S_IMODE(regular.stat().st_mode) == 0o640
    assert stat.S_IMODE(linked.stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert link.is_symlink()
    for path in (regular, linked):
        assert path.stat().st_atime_ns == 123456789000000000
        assert path.stat().st_mtime_ns == 2200000000000000000
    assert negative.stat().st_atime_ns == -1000000000
    assert negative.stat().st_mtime_ns == -2000000000
    assert before <= current.stat().st_atime_ns <= after
    assert before <= current.stat().st_mtime_ns <= after
