"""Fold proven single-use byte GEPs into AArch64 memory operands."""

import pytest
import re

from pcc.backend.self_backend_aarch64_darwin import emit_aarch64_darwin_asm


def _load_source(offset: int, index_type: str = "i64", extra: str = "") -> str:
    return f'''
target triple = "arm64-apple-darwin23.6.0"
define i64 @read_field(ptr %base) {{
entry:
  %address = getelementptr i8, ptr %base, {index_type} {offset}
  %value = load i64, ptr %address, align 1
  {extra}
  ret i64 %value
}}
'''


@pytest.mark.parametrize("offset,operation", [
    (8, "ldr x10, [x9, #8]"),
    (-8, "ldur x10, [x9, #-8]"),
    (7, "ldur x10, [x9, #7]"),
    (32760, "ldr x10, [x9, #32760]"),
])
def test_single_use_gep_load_uses_memory_offset(offset, operation):
    assembly = emit_aarch64_darwin_asm(_load_source(offset), optimize=False)
    assert re.search(re.escape(operation).replace("x10", r"x\d+"), assembly)


def test_small_index_is_sign_extended_before_address_folding():
    assembly = emit_aarch64_darwin_asm(_load_source(255, "i8"), optimize=False)
    assert re.search(r"ldur x\d+, \[x9, #-1\]", assembly)


def test_gep_with_another_use_keeps_its_materialized_value():
    source = _load_source(8, extra="store i64 19, ptr %address, align 1")
    assembly = emit_aarch64_darwin_asm(source, optimize=False)
    assert "ldr x10, [x9, #8]" not in assembly


def test_unencodable_offset_keeps_general_gep_lowering():
    assembly = emit_aarch64_darwin_asm(_load_source(32768), optimize=False)
    assert "ldr x10, [x9, #32768]" not in assembly


def test_volatile_access_and_pointer_stored_as_value_are_not_folded():
    volatile = _load_source(8).replace("load i64", "load volatile i64")
    assert "ldr x10, [x9, #8]" not in emit_aarch64_darwin_asm(volatile, optimize=False)
    source = _load_source(8).replace(
        "%value = load i64, ptr %address, align 1",
        "store ptr %address, ptr %address, align 1\n  %value = ptrtoint ptr %address to i64",
    )
    assert "str x10, [x9, #8]" not in emit_aarch64_darwin_asm(source, optimize=False)


@pytest.mark.parametrize("optimize", [False, True])
def test_folded_indexed_stores_and_loads_execute_with_guards(tmp_path, optimize):
    import platform
    import subprocess
    import sys
    from pcc.backend.arm64_asm_driver import assemble_file
    from pcc.backend.macho_exec import link_executable
    from pcc.backend.native_object import NativeObject
    from pcc.backend.self_backend_aarch64_darwin import emit_aarch64_darwin_indexed_transport
    from pcc.backend.self_backend_parse import parse_self_backend_module

    if sys.platform != "darwin" or platform.machine() != "arm64":
        pytest.skip("AArch64 Darwin execution")
    source = '''
target triple = "arm64-apple-darwin23.6.0"
@buffer = global [32 x i8] zeroinitializer, align 16
define i32 @main() {
entry:
  %base = getelementptr i8, ptr @buffer, i64 8
  %before = getelementptr i8, ptr %base, i64 -1
  store i8 77, ptr %before, align 1
  %field = getelementptr i8, ptr %base, i64 7
  store i64 1234567, ptr %field, align 1
  %after = getelementptr i8, ptr %base, i64 15
  store i8 88, ptr %after, align 1
  %reload = getelementptr i8, ptr %base, i64 7
  %value = load i64, ptr %reload, align 1
  %prior = getelementptr i8, ptr %base, i8 255
  %guard1 = load i8, ptr %prior, align 1
  %next = getelementptr i8, ptr %base, i64 15
  %guard2 = load i8, ptr %next, align 1
  %bad1 = icmp ne i64 %value, 1234567
  %bad2 = icmp ne i8 %guard1, 77
  %bad3 = icmp ne i8 %guard2, 88
  %a = or i1 %bad1, %bad2
  %b = or i1 %a, %bad3
  %status = zext i1 %b to i32
  ret i32 %status
}
'''
    transport = emit_aarch64_darwin_indexed_transport(parse_self_backend_module(source), optimize=optimize)
    sections, undefined = transport.assemble_sections()
    image = link_executable([NativeObject.from_sections(sections, undefined=undefined)])
    if transport.encoded_line_records is not None:
        transport.encoded_line_records.close()
    executable = tmp_path / "fields"
    executable.write_bytes(image)
    executable.chmod(0o755)
    result = subprocess.run([str(executable)], capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
