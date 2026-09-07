"""Owned optimizer kernels must run without importing an LLVM binding."""

from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


def test_owned_scalar_and_cfg_passes_without_llvm():
    source = '''import importlib.abc
import sys
class RejectLLVM(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "llvmlite" or fullname.startswith("llvmlite.") or fullname == "pcc.llvm_capi.binding":
            raise AssertionError("external LLVM import: " + fullname)
sys.meta_path.insert(0, RejectLLVM())
from pcc.native_ir.instsimplify import simplify_module_text
from pcc.native_ir.simplifycfg import simplify_cfg_text
from pcc.native_ir.instcombine import instcombine_text
ir = """define i64 @execute(i64 %x) {
entry:
  %same = add i64 %x, 0
  %condition = icmp eq i64 1, 1
  br i1 %condition, label %yes, label %no
yes:
  %twice = add i64 %same, %same
  ret i64 %twice
no:
  ret i64 9
}
"""
ir, _ = simplify_module_text(ir)
ir, _ = simplify_cfg_text(ir)
ir, _ = instcombine_text(ir)
assert "add i64 %x, 0" not in ir, ir
assert "ret i64 9" not in ir, ir
assert "shl i64 %x, 1" in ir, ir
print("owned-optimizer-ok")
'''
    result = subprocess.run([sys.executable, "-c", source], cwd=ROOT,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "owned-optimizer-ok"


@pytest.mark.parametrize("instruction", [
    "%unused = tail call i64 @effect()",
    "%unused = notail call i64 @effect()",
    "%unused = load volatile i64, ptr %p",
    "%unused = load atomic i64, ptr %p acquire, align 8",
])
def test_owned_dce_preserves_effects_without_result_users(instruction):
    from pcc.native_ir.dce import dce_module_text

    source = "declare i64 @effect()\ndefine i64 @f(ptr %p) {\nentry:\n  " + instruction + "\n  ret i64 0\n}\n"
    result, _ = dce_module_text(source)
    assert instruction in result


def test_function_cleanup_keeps_module_call_effect_contracts():
    from pcc.native_ir.simplifycfg import simplify_cfg_text

    source = '''declare i64 @pure() memory(none) willreturn nounwind
declare i64 @effect()
define i64 @first() {
entry:
  %dead = call i64 @pure()
  %used_for_effect = call i64 @effect()
  br i1 true, label %yes, label %no
yes:
  ret i64 7
no:
  ret i64 9
}
define i64 @second() {
entry:
  %dead = call i64 @effect()
  ret i64 %dead
}
'''
    result, changed = simplify_cfg_text(source)
    assert changed
    assert "call i64 @pure()" not in result
    assert result.count("call i64 @effect()") == 2
    assert "ret i64 %dead" in result


def test_owned_inliner_without_llvm():
    source = '''import importlib.abc
import sys
class RejectLLVM(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "llvmlite" or fullname.startswith("llvmlite.") or fullname == "pcc.llvm_capi.binding":
            raise AssertionError("external LLVM import: " + fullname)
sys.meta_path.insert(0, RejectLLVM())
from pcc.native_ir.inline import inline_module
ir = """define internal i64 @twice(i64 %x) {
entry:
  %result = mul i64 %x, 2
  ret i64 %result
}
define i64 @execute() {
entry:
  %result = call i64 @twice(i64 21)
  ret i64 %result
}
"""
out, changed = inline_module(ir)
assert changed
assert "call i64 @twice" not in out, out
assert "ret i64 42" in out, out
print("owned-inline-ok")
'''
    result = subprocess.run([sys.executable, "-c", source], cwd=ROOT,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_local_name_replacement_preserves_prefixes_and_literals():
    from pcc.native_ir.text_tokens import replace_local_names

    source = '%answer = add i64 %a, %a.field ; keep %a\ncall void @f(ptr c"%a")\n'
    expected = '%answer = add i64 %renamed, %a.field ; keep %a\ncall void @f(ptr c"%a")\n'
    assert replace_local_names(source, {"a": "%renamed"}) == expected


def test_defined_inlining_preserves_external_symbols_and_replacement_boundaries():
    from pcc.native_ir.inline import inline_module

    source = '''define i64 @small(i64 %x) {
entry:
  %result = add i64 %x, 1
  ret i64 %result
}
define weak i64 @replaceable(i64 %x) {
entry:
  ret i64 %x
}
define i64 @kept(i64 %x) noinline {
entry:
  ret i64 %x
}
define i64 @caller(i64 %x) {
entry:
  %a = call i64 @small(i64 %x)
  %b = call i64 @replaceable(i64 %a)
  %c = call i64 @kept(i64 %b)
  ret i64 %c
}
'''
    out, changed = inline_module(source, include_definitions=True)
    assert changed
    assert "call i64 @small" not in out
    assert "define i64 @small" in out
    assert "call i64 @replaceable" in out
    assert "call i64 @kept" in out
    interposable = source + '!llvm.module.flags = !{!0}\n!0 = !{i32 1, !"SemanticInterposition", i32 1}\n'
    out, _ = inline_module(interposable, include_definitions=True)
    assert "call i64 @small" in out


def test_pointer_inline_rewrites_earlier_backedge_phi_uses():
    from pcc.native_ir.inline import inline_module

    source = '''define internal ptr @identity(ptr %value) {
entry:
  ret ptr %value
}
define ptr @loop(ptr %initial, i64 %limit) {
entry:
  br label %header
header:
  %current = phi ptr [ %initial, %entry ], [ %next, %body ]
  %index = phi i64 [ 0, %entry ], [ %increment, %body ]
  %keep = icmp slt i64 %index, %limit
  br i1 %keep, label %body, label %exit
body:
  %next = call ptr @identity(ptr %current)
  %increment = add i64 %index, 1
  br label %header
exit:
  ret ptr %current
}
'''
    result, changed = inline_module(source)
    assert changed
    assert "call ptr @identity" not in result
    assert "%next" not in result
    assert "[ %current, %body ]" in result


def test_owned_pass_dispatch_preserves_order_and_needs_no_host(monkeypatch):
    from pcc.py_frontend import pipeline

    def reject(*args, **kwargs):
        raise AssertionError("owned optimizer called a host subprocess")

    monkeypatch.setattr(pipeline.subprocess, "run", reject)
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "mem2reg,sroa,instsimplify,dce")
    ir = 'define i64 @value() {\nentry:\n  %p = alloca i64\n  store i64 42, ptr %p\n  %v = load i64, ptr %p\n  %dead = add i64 %v, 1\n  ret i64 %v\n}\n'
    results = pipeline._apply_python_ir_pass_pipeline_many(
        [("first", ir), ("second", ir)], default_raw="default", strict_no_libpython=True,
    )
    assert [name for name, _ in results] == ["first", "second"]
    for _, text in results:
        assert "ret i64 42" in text
        assert "alloca" not in text
        assert "%dead" not in text


def test_compiled_simplifier_preserves_each_function(tmp_path, pcc_py_runtime_archive):
    from pcc.native_ir.instsimplify import simplify_module_text
    from pcc.native_ir.inline import inline_module
    from pcc.py_frontend.pipeline import compile_python

    ir = "\n".join(
        "define i64 @function_" + str(index) + "(i64 %x) {\nentry:\n"
        "  %copy = add i64 %x, 0\n  ret i64 %copy\n}\n"
        for index in range(32)
    )
    ir += '''
define i64 @negative() {
entry:
  %value = sub i64 0, 1
  ret i64 %value
}
define i128 @wide() {
entry:
  %value = add i128 18446744073709551615, 1
  ret i128 %value
}
define i64 @signed_overflow() {
entry:
  %value = add nsw i64 9223372036854775807, 1
  ret i64 %value
}
define internal i64 @twice(i64 %x) {
entry:
  %value = mul i64 %x, 2
  ret i64 %value
}
define i64 @inlined() {
entry:
  %value = call i64 @twice(i64 21)
  ret i64 %value
}
define internal i64 @choose(i1 %condition) {
entry:
  br i1 %condition, label %yes, label %no
yes:
  ret i64 7
no:
  ret i64 9
}
define i64 @inlined_cfg(i1 %condition) {
entry:
  %value = call i64 @choose(i1 %condition)
  ret i64 %value
}
'''
    ir += '''
define i1 @boolean_roundtrip(i1 %condition) {
entry:
  %wide = zext i1 %condition to i64
  %check = icmp ne i64 %wide, 0
  ret i1 %check
}
define i1 @inverted_comparison(i64 %left, i64 %right) {
entry:
  %comparison = icmp slt i64 %left, %right
  %inverse = xor i1 %comparison, true
  ret i1 %inverse
}
define internal void @finish_dotted(i1 %condition) {
entry:
  br label %while.cond.9961
while.cond.9961:
  br i1 %condition, label %while.body.9962, label %while.end.9963
while.body.9962:
  br label %while.end.9963
while.end.9963:
  ret void
}
define void @call_dotted(i1 %condition) {
entry:
  call void @finish_dotted(i1 %condition)
  ret void
}
define ptr @pointer_roundtrip(ptr %pointer) {
entry:
'''
    previous = "%pointer"
    for index in range(128):
        current = "%copy" + str(index)
        ir += "  " + current + " = bitcast ptr " + previous + " to ptr\n"
        previous = current
    ir += "  ret ptr " + previous + "\n}\n"
    expected, _ = inline_module(ir)
    expected, _ = simplify_module_text(expected)
    input_path = tmp_path / "input.ll"
    output_path = tmp_path / "output.ll"
    input_path.write_text(ir)
    driver = ROOT / "pcc" / "native_ir" / "driver.py"
    binary = tmp_path / "simplify_driver"
    compile_python(str(driver), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_py_runtime_archive))
    run = subprocess.run([str(binary), "inline,instsimplify", str(input_path), str(output_path)],
                         capture_output=True, text=True, timeout=15)
    assert run.returncode == 0, run.stderr
    assert output_path.read_text() == expected
