"""Proven scalar tail positions restore the frame before branching."""

from pcc.backend.self_backend_aarch64_darwin import emit_aarch64_darwin_asm


_TRIPLE = 'target triple = "arm64-apple-darwin23.6.0"\n'


def test_direct_scalar_tail_position_uses_jump():
    source = _TRIPLE + '''
declare i64 @callee(i64, ptr)
define i64 @wrapper(i64 %value, ptr %pointer) {
entry:
  %result = call i64 @callee(i64 %value, ptr %pointer)
  ret i64 %result
}
'''
    assembly = emit_aarch64_darwin_asm(source, optimize=True)
    assert "  bl _callee" not in assembly
    assert "  autiasp\n  b _callee" in assembly
    assert "  bl _callee" in emit_aarch64_darwin_asm(source, optimize=False)


def _execute(source, path, optimize):
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
    transport = emit_aarch64_darwin_indexed_transport(parse_self_backend_module(source), optimize=optimize)
    sections, undefined = transport.assemble_sections()
    image = link_executable([NativeObject.from_sections(sections, undefined=undefined)])
    if transport.encoded_line_records is not None:
        transport.encoded_line_records.close()
    path.write_bytes(image)
    path.chmod(0o755)
    result = subprocess.run([str(path)], capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_tail_recursion_executes_without_growing_frames(tmp_path):
    for optimize in (False, True):
        depth = 100000 if optimize else 300
        source = _TRIPLE + f'''
define i64 @sum(i64 %n, i64 %acc) {{
entry:
  %done = icmp eq i64 %n, 0
  br i1 %done, label %exit, label %recurse
exit:
  ret i64 %acc
recurse:
  %next = sub i64 %n, 1
  %added = add i64 %acc, %n
  %result = call i64 @sum(i64 %next, i64 %added)
  ret i64 %result
}}
define i32 @main() {{
entry:
  %result = call i64 @sum(i64 {depth}, i64 0)
  %bad = icmp ne i64 %result, {depth * (depth + 1) // 2}
  %status = zext i1 %bad to i32
  ret i32 %status
}}
'''
        _execute(source, tmp_path / ("recursive-" + str(optimize)), optimize)


def test_tail_call_forwards_eight_register_arguments(tmp_path):
    source = _TRIPLE + '''
@number = global i64 8
define i64 @callee(i64 %a, i64 %b, i64 %c, i64 %d, i64 %e, i64 %f, i64 %g, ptr %p) {
entry:
  %h = load i64, ptr %p
  %bw = mul i64 %b, 10
  %cw = mul i64 %c, 100
  %dw = mul i64 %d, 1000
  %ew = mul i64 %e, 10000
  %fw = mul i64 %f, 100000
  %gw = mul i64 %g, 1000000
  %hw = mul i64 %h, 10000000
  %ab = add i64 %a, %bw
  %cd = add i64 %cw, %dw
  %ef = add i64 %ew, %fw
  %gh = add i64 %gw, %hw
  %abcd = add i64 %ab, %cd
  %efgh = add i64 %ef, %gh
  %sum = add i64 %abcd, %efgh
  ret i64 %sum
}
define i64 @wrapper(ptr %pointer, i64 %a, i64 %b, i64 %c, i64 %d, i64 %e, i64 %f, i64 %g) {
entry:
  %result = call i64 @callee(i64 %g, i64 %f, i64 %e, i64 %d, i64 %c, i64 %b, i64 %a, ptr %pointer)
  ret i64 %result
}
define i32 @main() {
entry:
  %result = call i64 @wrapper(ptr @number, i64 1, i64 2, i64 3, i64 4, i64 5, i64 6, i64 7)
  %bad = icmp ne i64 %result, 81234567
  %status = zext i1 %bad to i32
  ret i32 %status
}
'''
    for optimize in (False, True):
        _execute(source, tmp_path / ("arguments-" + str(optimize)), optimize)


def test_stack_arguments_varargs_local_addresses_and_non_tail_returns_keep_calls():
    sources = [
        '''declare i64 @callee(i64, i64, i64, i64, i64, i64, i64, i64, i64)
define i64 @wrapper() {
entry:
  %r = call i64 @callee(i64 1, i64 2, i64 3, i64 4, i64 5, i64 6, i64 7, i64 8, i64 9)
  ret i64 %r
}''',
        '''declare i64 @callee(i64, ...)
define i64 @wrapper() {
entry:
  %r = call i64 (i64, ...) @callee(i64 1, i64 2)
  ret i64 %r
}''',
        '''declare i64 @callee(ptr)
define i64 @wrapper() {
entry:
  %slot = alloca i64
  store i64 19, ptr %slot
  %r = call i64 @callee(ptr %slot)
  ret i64 %r
}''',
        '''declare i64 @callee(i64)
define i64 @wrapper(i64 %x) {
entry:
  %r = call i64 @callee(i64 %x)
  ret i64 7
}''',
        '''declare i64 @callee(i64)
declare i64 @llvm.bswap.i64(i64)
define i64 @wrapper(i64 %x) {
entry:
  %swapped = call i64 @llvm.bswap.i64(i64 %x)
  %r = call i64 @callee(i64 %swapped)
  ret i64 %r
}''',
    ]
    for source in sources:
        assembly = emit_aarch64_darwin_asm(_TRIPLE + source + "\n", optimize=True)
        assert "  bl _callee" in assembly
        assert "  b _callee" not in assembly


def test_zero_argument_tail_call_and_reused_module_metadata():
    import pcc.backend.self_backend_aarch64_darwin as backend
    from pcc.backend.self_backend_parse import parse_self_backend_module

    source = _TRIPLE + """
declare void @callee()
define void @wrapper() {
entry:
  call void @callee()
  ret void
}
"""
    assert "  b _callee" in emit_aarch64_darwin_asm(source, optimize=True)

    def prepared():
        return backend.prepare_parsed_module_for_target(
            parse_self_backend_module(source),
            aggregate_returned_indirect=backend._aggregate_returned_indirect,
            aggregate_returned_indirect_indexed=backend._aggregate_returned_indirect_indexed,
            materialize_legacy_slots=False,
        )

    module = prepared()
    # Public transport consumes its native arenas. This internal lane
    # explicitly preserves them to exercise target-plan reset on reuse.
    backend._emit_prepared_aarch64_darwin_lines(module, True, close_native_tables=False)
    reused = backend._emit_prepared_aarch64_darwin_lines(module, False, close_native_tables=False)
    fresh = backend._emit_prepared_aarch64_darwin_lines(prepared(), False)
    assert reused == fresh


def test_special_call_protocols_and_narrow_return_keep_calls():
    for declaration, result_type, name in (
        ("declare i64 @py_err_occurred()", "i64", "py_err_occurred"),
        ("declare i8 @callee()", "i8", "callee"),
    ):
        source = _TRIPLE + declaration + f'''
define {result_type} @wrapper() {{
entry:
  %result = call {result_type} @{name}()
  ret {result_type} %result
}}
'''
        assert "  bl _" + name in emit_aarch64_darwin_asm(source, optimize=True)


def test_indirect_float_and_aggregate_final_calls_keep_call_abi():
    sources = [
        ("""define i64 @wrapper(ptr %fn, i64 %x) {
entry:
  %result = call i64 %fn(i64 %x)
  ret i64 %result
}""", "  blr "),
        ("""declare i64 @callee(double)
define i64 @wrapper(double %x) {
entry:
  %result = call i64 @callee(double %x)
  ret i64 %result
}""", "  bl _callee"),
        ("""declare { i64, i64 } @callee(i64)
define { i64, i64 } @wrapper(i64 %x) {
entry:
  %result = call { i64, i64 } @callee(i64 %x)
  ret { i64, i64 } %result
}""", "  bl _callee"),
        ("""declare i64 @callee(i64)
define i64 @wrapper(i64 %x, ...) {
entry:
  %result = call i64 @callee(i64 %x)
  ret i64 %result
}""", "  bl _callee"),
    ]
    for source, expected in sources:
        assert expected in emit_aarch64_darwin_asm(_TRIPLE + source + "\n", optimize=True)


def test_registered_frame_preserves_calls_and_root_records():
    from pcc.backend.self_backend_aarch64_darwin import emit_aarch64_darwin_indexed_transport
    from pcc.backend.self_backend_parse import parse_self_backend_module
    from pcc.backend.precise_stackmap import decode_stack_map, SAFEPOINT_CALL

    source = _TRIPLE + """
@frame_map = internal constant i32 1
declare void @pcc_gc_frame_enter(ptr, ptr)
declare void @pcc_gc_frame_leave(ptr)
declare void @opaque_call(ptr)
declare i64 @callee(i64)
define i64 @wrapper(ptr %obj) {
entry:
  %root = alloca ptr, align 8
  store ptr %obj, ptr %root, align 8
  call void @pcc_gc_frame_enter(ptr @frame_map, ptr %root)
  call void @opaque_call(ptr %obj)
  call void @pcc_gc_frame_leave(ptr %root)
  %result = call i64 @callee(i64 42)
  ret i64 %result
}
"""
    assembly = emit_aarch64_darwin_asm(source, optimize=True)
    assert "  bl _callee" in assembly
    transport = emit_aarch64_darwin_indexed_transport(parse_self_backend_module(source), optimize=True)
    sections, _undefined = transport.assemble_sections()
    if transport.encoded_line_records is not None:
        transport.encoded_line_records.close()
    payload = next(section.data for section in sections if section.sectname == "__pcc_stackmaps")
    records = decode_stack_map(payload).functions[0].records
    assert len(records) == 3
    assert sum(record.kind == SAFEPOINT_CALL for record in records) == 2
    assert any(record.locations for record in records)
