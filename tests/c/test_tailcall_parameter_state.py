"""Host-owned IR proofs, not target execution or native-pcc1 qualification."""
from __future__ import annotations

import re

import pytest

from pcc.backend.self_backend_parse import parse_self_backend_module
from pcc.backend.self_backend_verify import verify_parsed_module
from pcc.ir.optimization.ir_mutator import MutableModule
from pcc.ir.optimization.tailcall_accumulator import rewrite_accumulator_tailcalls
from pcc.ir.optimization.tailcall_ir import (
    analyze_self_tailcalls,
    rewrite_simple_void_self_tailcalls,
)


COUNTDOWN = """@seen = global i64 0
define void @count(i64 %n) {
entry:
  store i64 %n, ptr @seen
  %done = icmp eq i64 %n, 0
  br i1 %done, label %exit, label %recurse
exit:
  ret void
recurse:
  %next = sub i64 %n, 1
  call void @count(i64 %next)
  ret void
}
"""
SWAP = """@left = global i64 0
@right = global i64 0
define void @swap(i64 %n, i64 %a, i64 %b) {
start:
  %done = icmp eq i64 %n, 0
  br i1 %done, label %exit, label %recurse
exit:
  store i64 %a, ptr @left
  store i64 %b, ptr @right
  ret void
recurse:
  %next = sub i64 %n, 1
  call void @swap(i64 %next, i64 %b, i64 %a)
  ret void
}
"""
SUM = """define i64 @sum(i64 %n, i64 %acc) {
start:
  %done = icmp eq i64 %n, 0
  br i1 %done, label %exit, label %recurse
exit:
  ret i64 %acc
recurse:
  %next = sub i64 %n, 1
  %added = add i64 %acc, %n
  %r = call i64 @sum(i64 %next, i64 %added)
  ret i64 %r
}
"""


def _verify(text):
    module = parse_self_backend_module('target triple = "x86_64-unknown-linux-gnu"\n' + text)
    verify_parsed_module(module)
    assert module.functions


def _evaluate(text, name, args, *, budget=20000):
    """A bounded reference evaluator for the test corpus's integer/CFG subset.

    It uses the existing mutable IR parser, models ordinary recursive frames
    and simultaneous PHIs, and records observable stores. It neither calls the
    rewrite's matching helpers nor claims to model target stack allocation.
    """
    module = MutableModule.parse(text)
    trace = []
    max_depth = 0
    steps = 0

    def run(function_name, arguments, depth):
        nonlocal max_depth, steps
        max_depth = max(max_depth, depth)
        function = module.function(function_name)
        assert function is not None
        values = {arg.name: value for arg, value in zip(function.args, arguments)}
        predecessor = None
        block = function.blocks[0]

        def value(operand):
            operand = operand.strip()
            if operand.startswith("%"):
                return values[operand[1:]]
            if operand in {"true", "false"}:
                return int(operand == "true")
            return int(operand)

        while True:
            pending_phis = {}
            for instruction in block.instructions:
                if instruction.opcode == "phi":
                    edges = re.findall(r"\[\s*([^,]+),\s*%([^\s\]]+)\s*\]", instruction.text)
                    incoming = [operand for operand, source in edges if source == predecessor]
                    assert len(incoming) == 1
                    pending_phis[instruction.result_name] = value(incoming[0])
            values.update(pending_phis)
            for instruction in block.instructions:
                code = instruction.text.split(";", 1)[0].strip()
                if not code or instruction.opcode == "phi":
                    continue
                steps += 1
                assert steps <= budget, "IR execution did not terminate within the bounded budget"
                rhs = code.split("=", 1)[-1].strip()
                if instruction.opcode in {"add", "sub", "mul", "and"}:
                    op, width, left, right = re.fullmatch(r"(\w+) i(\d+) ([^,]+), (.+)", rhs).groups()
                    a, b = value(left), value(right)
                    result = {"add": lambda: a + b, "sub": lambda: a - b,
                              "mul": lambda: a * b, "and": lambda: a & b}[op]()
                    values[instruction.result_name] = result & ((1 << int(width)) - 1)
                elif instruction.opcode == "icmp":
                    pred, width, left, right = re.fullmatch(r"icmp (\w+) i(\d+) ([^,]+), (.+)", rhs).groups()
                    mask = (1 << int(width)) - 1
                    a, b = value(left) & mask, value(right) & mask
                    assert pred in {"eq", "ne", "ult"}
                    values[instruction.result_name] = int({"eq": a == b, "ne": a != b, "ult": a < b}[pred])
                elif instruction.opcode == "store":
                    operand, destination = re.fullmatch(r"store i\d+ ([^,]+), ptr @([^, ]+)", code).groups()
                    trace.append((destination, value(operand)))
                elif instruction.opcode == "call":
                    callee, operands = re.fullmatch(r"call (?:void|i\d+) @([^ (]+)\((.*)\)", rhs).groups()
                    actual = [value(arg.split(None, 1)[1]) for arg in operands.split(",") if arg.strip()]
                    result = run(callee, actual, depth + 1)
                    if instruction.result_name is not None:
                        values[instruction.result_name] = result
                elif instruction.opcode == "ret":
                    return None if code == "ret void" else value(code.split(None, 2)[2])
                elif instruction.opcode == "br":
                    destinations = re.findall(r"label %([^, ]+)", code)
                    if len(destinations) == 2:
                        condition = code.split(",", 1)[0].split(None, 2)[2]
                        destination = destinations[0 if value(condition) else 1]
                    else:
                        destination = destinations[0]
                    predecessor = block.name
                    block = function.block(destination)
                    assert block is not None
                    break
                else:
                    raise AssertionError(f"unsupported reference instruction: {code}")
            else:
                raise AssertionError("unterminated reference block")

    result = run(name, args, 1)
    return result, trace, max_depth


@pytest.mark.parametrize("entry", ["entry", "start"])
def test_changed_arguments_preserve_store_trace_and_form_valid_ssa(entry):
    source = COUNTDOWN.replace("entry:", entry + ":")
    result = rewrite_simple_void_self_tailcalls(source)
    assert result.rewritten
    _verify(result.ir_text)
    assert "%n = phi i64" in result.ir_text
    assert f"; pcc.tailcall.self\n  br label %{entry}" in result.ir_text
    before = _evaluate(source, "count", [7])
    after = _evaluate(result.ir_text, "count", [7])
    assert before[:2] == after[:2] == (None, [("seen", n) for n in range(7, -1, -1)])
    assert before[2] == 8
    assert after[2] == 1
    assert _evaluate(result.ir_text, "count", [1000])[2] == 1


@pytest.mark.parametrize("n", [0, 1, 2, 7])
def test_parameter_permutation_is_parallel(n):
    result = rewrite_simple_void_self_tailcalls(SWAP)
    assert result.rewritten
    _verify(result.ir_text)
    assert "[ %b, %recurse ]" in result.ir_text
    assert "[ %a, %recurse ]" in result.ir_text
    expected = [("left", 23 if n % 2 else 11), ("right", 11 if n % 2 else 23)]
    assert _evaluate(SWAP, "swap", [n, 11, 23])[:2] == (None, expected)
    assert _evaluate(result.ir_text, "swap", [n, 11, 23])[:2] == (None, expected)


def test_multiple_tail_edges_and_returns_keep_the_selected_arguments():
    source = """@seen = global i64 0
define void @walk(i64 %n) {
entry:
  store i64 %n, ptr @seen
  %zero = icmp eq i64 %n, 0
  br i1 %zero, label %done, label %check
check:
  %one = icmp eq i64 %n, 1
  br i1 %one, label %other_done, label %choose
choose:
  %bit = and i64 %n, 1
  %odd = icmp ne i64 %bit, 0
  br i1 %odd, label %odd_path, label %even_path
odd_path:
  %less_one = sub i64 %n, 1
  call void @walk(i64 %less_one)
  ret void
even_path:
  %less_two = sub i64 %n, 2
  call void @walk(i64 %less_two)
  ret void
done:
  ret void
other_done:
  ret void
}
"""
    result = rewrite_simple_void_self_tailcalls(source)
    assert result.rewritten
    _verify(result.ir_text)
    assert "[ %less_one, %odd_path ], [ %less_two, %even_path ]" in result.ir_text
    for n in (0, 1, 6, 7):
        assert _evaluate(source, "walk", [n])[:2] == _evaluate(result.ir_text, "walk", [n])[:2]


@pytest.mark.parametrize("n,acc", [(0, 9), (1, 9), (12, 9), (3, (1 << 64) - 2)])
def test_accumulator_returns_updated_state_without_a_followup_pass(n, acc):
    transformed, reports = rewrite_accumulator_tailcalls(SUM)
    assert reports[0].rewritten
    _verify(transformed)
    expected = (acc + n * (n + 1) // 2) % (1 << 64)
    assert _evaluate(SUM, "sum", [n, acc])[0] == expected
    assert _evaluate(transformed, "sum", [n, acc]) == (expected, [], 1)


def test_fresh_names_do_not_capture_existing_locals_or_labels():
    source = SWAP.replace("%a", "%pcc.tailcall.arg.0").replace("start:", "pcc.tailcall.preheader:")
    result = rewrite_simple_void_self_tailcalls(source)
    assert result.rewritten
    _verify(result.ir_text)
    assert "pcc.tailcall.preheader.1:" in result.ir_text
    assert _evaluate(result.ir_text, "swap", [3, 11, 23])[:2] == _evaluate(source, "swap", [3, 11, 23])[:2]


@pytest.mark.parametrize("operation", [
    "call void @pcc_gc_frame_enter(ptr null, ptr null)",
    "call void @pcc_gc_frame_leave()",
    "call void @pcc_gc_root_push(ptr null)",
    "%root = load ptr, ptr @live_root",
    "%slot = alloca i64",
    "store ptr %slot, ptr @escaped",
    "call void @finally_cleanup()",
    "call void @llvm.experimental.gc.statepoint()",
    "call void %indirect()",
    "fence seq_cst",
    "store volatile i64 %n, ptr @seen",
    "%phi = phi i64 [ 0, %entry ]",
    "%x = invoke i64 @throwing() to label %exit unwind label %exception",
    "%exc = landingpad { ptr, i32 } cleanup",
    "resume { ptr, i32 } zeroinitializer",
])
@pytest.mark.parametrize("accumulator", [False, True])
def test_unproven_frame_root_exception_or_local_state_is_unchanged(operation, accumulator):
    source = (SUM if accumulator else COUNTDOWN).replace("  %next = sub", f"  {operation}\n  %next = sub")
    if accumulator:
        text, reports = rewrite_accumulator_tailcalls(source)
    else:
        result = rewrite_simple_void_self_tailcalls(source)
        text, reports = result.ir_text, result.candidates
    assert text == source
    assert reports and not any(report.rewritten for report in reports)
    assert reports[0].reason


@pytest.mark.parametrize("old,new", [
    ("i64 %n)", "i64 %n, ...)"),
    ("define void", "define fastcc void"),
    ("i64 %n)", "i64 signext %n)"),
    ("i64 %n)", "ptr %n)"),
    ("i64 %n)", "{ i64, i64 } %n)"),
    ("i64 %n) {", 'i64 %n) gc "statepoint-example" {'),
    ("i64 %n) {", 'i64 %n) personality ptr @personality {'),
    ("i64 %n) {", "i64 %n) #0 {"),
    ("call void @count(i64 %next)", "call void @count(i32 %next)"),
    ("call void @count(i64 %next)", "call void @count(i64 %next, i64 0)"),
    ("call void @count(i64 %next)", "musttail call void @count(i64 %next)"),
    ("call void @count(i64 %next)", "notail call void @count(i64 %next)"),
    ("call void @count(i64 %next)", 'call void @count(i64 %next) [ "deopt"(i64 %n) ]'),
    ("entry:", "0:"),
])
def test_unsupported_signature_and_call_contracts_are_unchanged(old, new):
    source = COUNTDOWN.replace(old, new)
    result = rewrite_simple_void_self_tailcalls(source)
    assert not result.rewritten
    assert result.ir_text == source


def test_intervening_side_effect_is_not_swallowed_by_tail_matching():
    source = COUNTDOWN.replace("  call void @count(i64 %next)\n  ret void", "  call void @count(i64 %next)\n  store i64 99, ptr @seen\n  ret void")
    result = rewrite_simple_void_self_tailcalls(source)
    assert not result.rewritten
    assert result.ir_text == source
    assert _evaluate(source, "count", [2])[1] == [("seen", 2), ("seen", 1), ("seen", 0), ("seen", 99), ("seen", 99)]


def test_no_cross_function_replacement_and_repeated_pass_is_stable():
    unrelated = "define void @untouched() {\nentry:\n  ret void\n}\n"
    source = unrelated + COUNTDOWN + unrelated.replace("untouched", "also_untouched")
    once = rewrite_simple_void_self_tailcalls(source)
    assert once.rewritten
    assert once.ir_text.startswith(unrelated)
    assert once.ir_text.endswith(unrelated.replace("untouched", "also_untouched"))
    assert rewrite_simple_void_self_tailcalls(once.ir_text).ir_text == once.ir_text
    assert analyze_self_tailcalls(once.ir_text) == []
    _verify(once.ir_text)


def test_public_opt_in_pipeline_preserves_changed_argument_semantics(monkeypatch):
    from pcc.frontends.python.ir_pass_pipeline import run_python_ir_pass_pipeline

    monkeypatch.setenv("PCC_ENABLE_TAILCALL_REWRITE", "1")
    output = run_python_ir_pass_pipeline(COUNTDOWN, pass_names=(), module_name="countdown")
    assert "call void @count" not in output
    _verify(output)
    assert _evaluate(output, "count", [7]) == (None, [("seen", n) for n in range(7, -1, -1)], 1)


@pytest.mark.parametrize("declaration,operation", [
    ("declare void @pcc_gc_frame_enter(ptr, ptr)\n", "call void @pcc_gc_frame_enter(ptr null, ptr null)"),
    ("declare void @pcc_gc_root_push(ptr)\n", "call void @pcc_gc_root_push(ptr null)"),
    ("@live_root = global ptr null\n", "%root_value = load ptr, ptr @live_root"),
    ("declare void @exception_cleanup()\n", "call void @exception_cleanup()"),
    ("", "%local = alloca i64"),
])
def test_valid_ir_with_unproven_per_call_state_is_refused(declaration, operation):
    source = declaration + COUNTDOWN.replace("  %next = sub", f"  {operation}\n  %next = sub")
    _verify(source)
    result = rewrite_simple_void_self_tailcalls(source)
    assert not result.rewritten
    assert result.ir_text == source
    assert "state is not proven empty" in result.candidates[0].reason


def test_constant_argument_and_scalar_width_are_carried_to_header():
    source = COUNTDOWN.replace("i64", "i32").replace("@count(i32 %next)", "@count(i32 0)")
    result = rewrite_simple_void_self_tailcalls(source)
    assert result.rewritten
    assert "[ 0, %recurse ]" in result.ir_text
    _verify(result.ir_text)
    assert _evaluate(source, "count", [7])[:2] == _evaluate(result.ir_text, "count", [7])[:2] == (None, [("seen", 7), ("seen", 0)])


def test_nonmatching_scalar_return_is_not_rewritten():
    source = SUM.replace("  ret i64 %r", "  ret i64 %acc")
    text, reports = rewrite_accumulator_tailcalls(source)
    assert text == source
    assert reports and not reports[0].rewritten
    _verify(source)


def test_detector_does_not_stop_at_a_different_callee():
    source = COUNTDOWN.replace("  %next = sub", "  call void @other()\n  %next = sub")
    assert analyze_self_tailcalls(source)[0].function == "count"
    assert not rewrite_simple_void_self_tailcalls(source).rewritten
