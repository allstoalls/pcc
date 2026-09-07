"""Cloned blocks and return PHIs need legal, function-unique IR names."""

from pcc.native_ir.inline import inline_module
from pcc.backend.self_backend_parse import parse_self_backend_module
from pcc.backend.self_backend_verify import verify_parsed_module


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
