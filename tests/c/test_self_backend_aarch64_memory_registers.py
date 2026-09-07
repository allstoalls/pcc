"""Memory instructions should consume existing register assignments directly."""

from pcc.backend.self_backend_aarch64_darwin import emit_aarch64_darwin_asm


_TRIPLE = 'target triple = "arm64-apple-darwin23.6.0"\n'


def test_scalar_load_and_store_use_allocated_value_register():
    source = _TRIPLE + '''
define i64 @copy(ptr %source, ptr %destination) {
entry:
  %value = load i64, ptr %source, align 1
  store i64 %value, ptr %destination, align 1
  ret i64 %value
}
'''
    assembly = emit_aarch64_darwin_asm(source, optimize=False)
    assert "ldr x1, [x9]" in assembly
    assert "str x1, [x9]" in assembly
    assert "mov x1, x10" not in assembly
    assert "mov x10, x1" not in assembly


def test_scalar_zero_store_uses_zero_register():
    source = _TRIPLE + '''
define void @clear(ptr %destination) {
entry:
  store i64 0, ptr %destination, align 1
  ret void
}
'''
    assembly = emit_aarch64_darwin_asm(source, optimize=False)
    assert "str xzr, [x9]" in assembly
    assert "movz x10, #0" not in assembly


def test_load_base_and_destination_can_share_a_register_without_writeback():
    source = _TRIPLE + '''
define i64 @twice(ptr %source) {
entry:
  %address = getelementptr i8, ptr %source, i64 8
  %first = load i64, ptr %address, align 1
  %second = load i64, ptr %address, align 1
  %sum = add i64 %first, %second
  ret i64 %sum
}
'''
    assembly = emit_aarch64_darwin_asm(source, optimize=False)
    assert "ldr x2, [x1]" in assembly
    assert "ldr x1, [x1]" in assembly


def test_indexed_memory_register_selection_executes(tmp_path):
    source = _TRIPLE + '''
@values = global [3 x i64] [i64 1, i64 23, i64 8], align 8
define i64 @twice(ptr %source, ptr %destination) {
entry:
  %address = getelementptr i8, ptr %source, i64 8
  %first = load i64, ptr %address, align 1
  %second = load i64, ptr %address, align 1
  %sum = add i64 %first, %second
  store i64 %sum, ptr %destination, align 1
  ret i64 %sum
}
define i32 @main() {
entry:
  %slot = alloca i64, align 8
  %sum = call i64 @twice(ptr @values, ptr %slot)
  %stored = load i64, ptr %slot
  %bad1 = icmp ne i64 %sum, 46
  %bad2 = icmp ne i64 %stored, 46
  %bad = or i1 %bad1, %bad2
  %status = zext i1 %bad to i32
  ret i32 %status
}
'''
    _execute_memory_ir(source, tmp_path)


def _execute_memory_ir(source, tmp_path):
    import platform
    import subprocess
    import sys
    import pytest
    from pcc.backend.macho_exec import link_executable
    from pcc.backend.native_object import NativeObject
    from pcc.backend.self_backend_aarch64_darwin import emit_aarch64_darwin_indexed_transport
    from pcc.backend.self_backend_parse import parse_self_backend_module

    if sys.platform != "darwin" or platform.machine() != "arm64":
        pytest.skip("AArch64 Darwin execution")
    for optimize in (False, True):
        transport = emit_aarch64_darwin_indexed_transport(parse_self_backend_module(source), optimize=optimize)
        sections, undefined = transport.assemble_sections()
        image = link_executable([NativeObject.from_sections(sections, undefined=undefined)])
        if transport.encoded_line_records is not None:
            transport.encoded_line_records.close()
        executable = tmp_path / ("memory-" + str(optimize))
        executable.write_bytes(image)
        executable.chmod(0o755)
        result = subprocess.run([str(executable)], capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr


def test_halfword_and_null_stores_preserve_surrounding_bytes(tmp_path):
    source = _TRIPLE + """
@source = global [4 x i8] [i8 17, i8 255, i8 255, i8 34], align 1
@destination = global [4 x i8] [i8 17, i8 0, i8 0, i8 34], align 1
@pointer = global ptr @source, align 8
define void @copy(ptr %source, ptr %destination, ptr %pointer) {
entry:
  %value = load i16, ptr %source, align 1
  store i16 %value, ptr %destination, align 1
  store ptr null, ptr %pointer, align 8
  ret void
}
define i32 @main() {
entry:
  %src = getelementptr i8, ptr @source, i64 1
  %dst = getelementptr i8, ptr @destination, i64 1
  call void @copy(ptr %src, ptr %dst, ptr @pointer)
  %word = load i32, ptr @destination, align 1
  %loaded_pointer = load ptr, ptr @pointer, align 8
  %badword = icmp ne i32 %word, 587202321
  %badptr = icmp ne ptr %loaded_pointer, null
  %bad = or i1 %badword, %badptr
  %status = zext i1 %bad to i32
  ret i32 %status
}
"""
    _execute_memory_ir(source, tmp_path)
