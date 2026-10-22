
from tests.owned_ir_validation import verify_ir_text
"""Cloned blocks and return PHIs need legal, function-unique IR names."""

from pcc.ir.optimization.inline import inline_module
from pcc.backend.self_backend_parse import parse_self_backend_module
from pcc.backend.self_backend_verify import verify_parsed_module
import pytest


NATIVE_INLINE_REFERENCE_PROGRAM = '''from pcc.ir.optimization.inline import inline_module
from pcc.ir.optimization.text_tokens import global_name_counts
SOURCE = "@callback = global ptr @finish\\ndefine internal void @finish() {\\nentry:\\n  ret void\\n}\\ndefine void @caller() {\\nentry:\\n  call void @finish()\\n  ret void\\n}\\n"
def main():
    result, changed = inline_module(SOURCE, include_definitions=True)
    assert changed
    assert "call void @finish()" not in result
    assert "define internal void @finish()" in result
    assert global_name_counts(result)["finish"] == 2
    text = '@table = constant [3 x ptr] [ptr @finish, ptr @"finish", ptr @"\\\\66inish"]\\n'
    text = text + '@message = constant [8 x i8] c"@finish\\\\00"\\n; @finish\\n'
    references = global_name_counts(text)
    assert references["finish"] == 3
    assert references["table"] == 1
    assert references["message"] == 1
    print("INLINE_GLOBAL_REFERENCES_OK")
main()
'''


@pytest.mark.integration
def test_native_inliner_keeps_callback_and_exact_global_tokens(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    import os
    import subprocess

    source = tmp_path / "inline_references.py"
    binary = tmp_path / "inline_references"
    source.write_text(NATIVE_INLINE_REFERENCE_PROGRAM)
    python_program_compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for gc in range(5):
        environment = dict(os.environ, PCC_GC_BACKEND=str(gc),
                           PCC_GC_REFCOUNT_PROVENANCE_PROBE="2")
        environment.pop("LC_ALL", None)
        result = subprocess.run([str(binary)], capture_output=True, text=True,
                                timeout=30, env=environment)
        assert result.returncode == 0, (gc, result.stderr)
        assert result.stdout == "INLINE_GLOBAL_REFERENCES_OK\n"
        assert result.stderr == ""


@pytest.mark.parametrize("reference", [
    "@callback = global ptr @finish",
    "@callbacks = constant [1 x ptr] [ptr @finish]",
    "@llvm.global_ctors = appending global [1 x { i32, ptr, ptr }] "
    "[{ i32, ptr, ptr } { i32 65535, ptr @finish, ptr null }]",
])
def test_inline_retains_internal_function_referenced_as_a_value(reference):
    source = reference + '''
define internal void @finish() {
entry:
  ret void
}
define void @caller() {
entry:
  call void @finish()
  ret void
}
'''
    result, changed = inline_module(source)
    assert changed
    assert "call void @finish()" not in result
    assert "define internal void @finish()" in result


def test_global_reference_counts_preserve_token_and_literal_boundaries():
    from pcc.ir.optimization.text_tokens import global_name_counts

    text = '''@table = constant [3 x ptr] [ptr @finish, ptr @"finish", ptr @"\\66inish"]
@message = constant [8 x i8] c"@finish\\00"
; @finish is mentioned only in a comment
define internal void @finish() {
entry:
  call void @finish.more()
  ret void
}
'''
    references = global_name_counts(text)
    assert references["finish"] == 4
    assert references["finish.more"] == 1
    assert references["table"] == references["message"] == 1


def test_inline_removes_unreferenced_body_despite_comment_and_prefix_mentions():
    source = '''; @finish has no surviving reference
@message = constant [7 x i8] c"@finish"
declare void @finish.more()
define internal void @finish() {
entry:
  ret void
}
define void @caller() {
entry:
  call void @finish()
  call void @finish.more()
  ret void
}
'''
    result, changed = inline_module(source)
    assert changed
    assert "define internal void @finish()" not in result
    assert "call void @finish.more()" in result


def test_multiblock_inline_preserves_dotted_original_labels():
    source = '''define internal void @finish(i1 %condition) {
entry:
  br label %while.cond.9961
while.cond.9961:
  br i1 %condition, label %while.body.9962, label %while.end.9963
while.body.9962:
  br label %while.end.9963
while.end.9963:
  ret void
}
define void @caller(i1 %condition) {
entry:
  call void @finish(i1 %condition)
  ret void
}
'''
    result, changed = inline_module(source)
    assert changed
    assert "call void @finish" not in result
    verify_parsed_module(parse_self_backend_module('target triple = "arm64-apple-darwin23.6.0"\n' + result))


def test_two_calls_get_disjoint_blocks_and_return_values():
    source = '''define internal i32 @choose(i1 %condition) {
entry:
  br i1 %condition, label %yes, label %no
yes:
  ret i32 7
no:
  ret i32 9
}
define i32 @caller(i1 %r1, i1 %other) {
entry:
  br i1 %r1, label %left, label %right
left:
  %a = call i32 @choose(i1 %other)
  ret i32 %a
right:
  %b = call i32 @choose(i1 %other)
  ret i32 %b
}
'''
    result, changed = inline_module(source)
    assert changed
    assert "call i32 @choose" not in result
    verify_parsed_module(parse_self_backend_module('target triple = "arm64-apple-darwin23.6.0"\n' + result))


def test_multiblock_call_cannot_replace_an_unrelated_caller_return():
    source = '''define internal i32 @choose(i1 %condition) {
entry:
  br i1 %condition, label %yes, label %no
yes:
  ret i32 7
no:
  ret i32 9
}
define i32 @caller(i1 %condition) {
entry:
  %unused = call i32 @choose(i1 %condition)
  ret i32 44
}
'''
    result, _ = inline_module(source)
    assert "ret i32 44" in result
    verify_parsed_module(parse_self_backend_module('target triple = "arm64-apple-darwin23.6.0"\n' + result))


def test_two_exit_void_callee_preserves_nonvoid_caller_return():
    source = '''define internal void @visit(i1 %condition) {
entry:
  br i1 %condition, label %yes, label %no
yes:
  ret void
no:
  ret void
}
define i32 @caller(i1 %condition) {
entry:
  call void @visit(i1 %condition)
  ret i32 44
}
'''
    result, _ = inline_module(source)
    assert "ret i32 44" in result
    verify_parsed_module(parse_self_backend_module('target triple = "arm64-apple-darwin23.6.0"\n' + result))


def test_inliner_preserves_calls_with_different_argument_widths():
    # Opaque-pointer IR permits this ABI boundary. Substituting i32 into the
    # i64 body is invalid even though the direct call itself verifies.
    source = """declare void @sink(i64)
define void @record(i64 %value) {
entry:
  call void @sink(i64 %value)
  ret void
}
define void @caller(i32 %value) {
entry:
  call void @record(i32 %value)
  ret void
}
"""
    verify_ir_text(source)
    result, _ = inline_module(source, include_definitions=True)
    verify_ir_text(result)
    assert "call void @record(i32 %value)" in result


def test_multiblock_inline_splits_call_site_and_repairs_loop_phi(tmp_path):
    source = '''define i64 @choose(i1 %condition, i64 %value) {
entry:
  br i1 %condition, label %yes, label %no
yes:
  %increment = add i64 %value, 2
  ret i64 %increment
no:
  %decrement = sub i64 %value, 3
  ret i64 %decrement
}
define i64 @loop(i64 %limit) {
entry:
  br label %body
body:
  %index = phi i64 [0, %entry], [%next, %body]
  %acc = phi i64 [0, %entry], [%total, %body]
  %condition = icmp ult i64 %index, 2
  %value = call i64 @choose(i1 %condition, i64 %index)
  %total = add i64 %acc, %value
  %next = add i64 %index, 1
  %again = icmp ult i64 %next, %limit
  br i1 %again, label %body, label %exit
exit:
  ret i64 %total
}
'''
    verify_ir_text(source)
    result, changed = inline_module(source, include_definitions=True)
    assert changed
    assert "call i64 @choose" not in result
    verify_ir_text(result)
    verify_parsed_module(parse_self_backend_module('target triple = "arm64-apple-darwin23.6.0"\n' + result))
    import platform
    import subprocess
    import sys
    if sys.platform == "darwin" and platform.machine() == "arm64":
        from pcc.backend.self_backend_aarch64_darwin import emit_aarch64_darwin_indexed_transport
        from pcc.backend.macho_exec import link_executable
        from pcc.backend.native_object import NativeObject
        executable_ir = 'target triple = "arm64-apple-darwin23.6.0"\n' + result + """
define i32 @main() {
entry:
  %first = call i64 @loop(i64 1)
  %both = call i64 @loop(i64 5)
  %sum = add i64 %first, %both
  %ok = icmp eq i64 %sum, 7
  %status = select i1 %ok, i32 0, i32 1
  ret i32 %status
}
"""
        transport = emit_aarch64_darwin_indexed_transport(parse_self_backend_module(executable_ir), optimize=0)
        sections, undefined = transport.assemble_sections()
        image = link_executable([NativeObject.from_sections(sections, undefined=undefined)])
        if transport.encoded_line_records is not None:
            transport.encoded_line_records.close()
        binary = tmp_path / "inline_loop"
        binary.write_bytes(image)
        binary.chmod(0o755)
        ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
        assert ran.returncode == 0, ran.stderr


def test_cfg_inline_preserves_addressed_caller_blocks():
    source = """@address = constant ptr blockaddress(@caller, %body)
define i64 @choose(i1 %condition) {
entry:
  br i1 %condition, label %yes, label %no
yes:
  ret i64 7
no:
  ret i64 9
}
define i64 @caller(i1 %condition) {
entry:
  br label %body
body:
  %value = call i64 @choose(i1 %condition)
  %result = add i64 %value, 1
  ret i64 %result
}
"""
    verify_ir_text(source)
    result, _ = inline_module(source, include_definitions=True)
    verify_ir_text(result)
    assert "call i64 @choose" in result


def test_cfg_inline_reuses_namespace_and_avoids_return_label_collision(monkeypatch):
    from pcc.ir.optimization.ir_mutator import Function
    serializations = []
    defined_scans = []
    original_serialize = Function.serialize
    original_defined = Function.defined_names
    def serialize(fn):
        serializations.append(fn.name)
        return original_serialize(fn)
    def defined(fn):
        defined_scans.append(fn.name)
        return original_defined(fn)
    monkeypatch.setattr(Function, "serialize", serialize)
    monkeypatch.setattr(Function, "defined_names", defined)
    source = '''define i64 @choose(i1 %flag, i64 %value) {
entry:
  br i1 %flag, label %yes, label %no
yes:
  %continuation = add i64 %value, 1
  ret i64 %continuation
no:
  ret i64 %value
}
define i64 @caller(i1 %flag) {
entry:
'''
    previous = "0"
    for i in range(12):
        source += f"  %v{i} = call i64 @choose(i1 %flag, i64 {previous})\n"
        previous = f"%v{i}"
    source += f"  ret i64 {previous}\n}}\n"
    result, changed = inline_module(source, include_definitions=True)
    assert changed
    assert "call i64 @choose" not in result
    verify_ir_text(result)
    assert serializations.count("caller") <= 2
    assert defined_scans.count("caller") == 1
