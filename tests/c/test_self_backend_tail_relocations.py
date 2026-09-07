"""Direct sibling jumps use Mach-O branch relocations across symbol atoms."""

import platform
import struct
import subprocess
import sys

import pytest

from pcc.backend.arm64_encode import PackedAArch64TextBuilder, assemble_text
from pcc.backend import macho_spec


def test_text_and_packed_external_jump_relocations_match():
    text = assemble_text("_wrapper:\n  b _callee\n")
    builder = PackedAArch64TextBuilder(["_callee"])
    builder.append_label("_wrapper")
    builder.append_branch(0x14000000, 26, 0)
    packed = builder.finish()
    assert text.code == packed.code == struct.pack("<I", 0x14000000)
    assert text.relocations == packed.relocations
    assert text.undefined == packed.undefined == ["_callee"]
    assert len(text.relocations) == 1
    assert text.relocations[0].type == macho_spec.ARM64_RELOC_BRANCH26


def test_symbol_atoms_relocate_but_local_and_recursive_jumps_stay_relative():
    cross = assemble_text("_wrapper:\n  b _callee\n_callee:\n  ret\n")
    assert len(cross.relocations) == 1
    assert struct.unpack_from("<I", cross.code)[0] == 0x14000000
    local = assemble_text("_wrapper:\n  b L_next\nL_next:\n  ret\n")
    assert not local.relocations
    assert struct.unpack_from("<I", local.code)[0] == 0x14000001
    recursive = assemble_text("_wrapper:\n  b _wrapper\n")
    assert not recursive.relocations
    assert struct.unpack_from("<I", recursive.code)[0] == 0x14000000


def test_tail_transfer_links_and_executes_across_native_objects(tmp_path):
    from pcc.backend.self_backend_aarch64_darwin import emit_aarch64_darwin_indexed_transport
    from pcc.backend.self_backend_parse import parse_self_backend_module
    from pcc.backend.native_object import NativeObject
    from pcc.backend.macho_exec import link_executable

    triple = 'target triple = "arm64-apple-darwin23.6.0"\n'
    caller = triple + """
declare i64 @callee(i64)
define i64 @wrapper(i64 %x) {
entry:
  %result = call i64 @callee(i64 %x)
  ret i64 %result
}
define i32 @main() {
entry:
  %value = call i64 @wrapper(i64 41)
  %bad = icmp ne i64 %value, 42
  %status = zext i1 %bad to i32
  ret i32 %status
}
"""
    callee = triple + """
define i64 @callee(i64 %x) {
entry:
  %result = add i64 %x, 1
  ret i64 %result
}
"""
    objects = []
    for source in (caller, callee):
        transport = emit_aarch64_darwin_indexed_transport(parse_self_backend_module(source), optimize=True)
        sections, undefined = transport.assemble_sections()
        objects.append(NativeObject.from_sections(sections, undefined=undefined))
        if transport.encoded_line_records is not None:
            transport.encoded_line_records.close()
    if sys.platform != "darwin" or platform.machine() != "arm64":
        pytest.skip("AArch64 Darwin execution")
    for index, ordered in enumerate((objects, list(reversed(objects)))):
        executable = tmp_path / ("tail-object-" + str(index))
        executable.write_bytes(link_executable(ordered))
        executable.chmod(0o755)
        result = subprocess.run([str(executable)], capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr
