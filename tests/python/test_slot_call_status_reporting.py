"""Shared cold status reporting keeps the original runtime/cleanup protocol.

These host checks inspect and interpret actual emitted IR. The tiny interpreter
models call ordering and identity only; it does not qualify a native GC backend.
"""
from __future__ import annotations

import re

import pytest

from pcc.frontends.python.codegen.errors import L1CodegenError
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.py_ast import FuncDef, Module, SourceSpan
from pcc.ir.compat import ir
from tests.owned_ir_validation import verify_ir_text


_I64 = ir.IntType(64)
_KEY = "__pcc_slot_call_status_report"


def test_reporter_method_has_matching_native_host_exports():
    from inspect import signature
    from pcc.frontends.python.codegen.host_contract import L1_CODEGEN_HOST_METHODS
    from pcc.frontends.python.codegen._l1_codegen_static_methods import L1_CODEGEN_STATIC_METHODS
    from pcc.frontends.python.codegen.layer1_support import _default_native_module_exports

    name = "_slot_call_status_report_helper"
    assert tuple(entry["name"] for entry in L1_CODEGEN_STATIC_METHODS) == L1_CODEGEN_HOST_METHODS
    static = {entry["name"]: entry for entry in L1_CODEGEN_STATIC_METHODS}
    exports = _default_native_module_exports("pcc.frontends.python.codegen.layer1")
    native = {entry["name"]: entry for entry in exports["pcc.frontends.python.codegen.layer1"]["L1CodeGen"]["methods"]}
    assert static[name] == native[name]
    assert tuple(signature(getattr(L1CodeGen, name)).parameters) == ("self",)
    assert tuple(item["name"] for item in static[name]["call_sig"]) == ("self",)
    assert all(item["kind"] == "pos" and not item["has_default"] for item in static[name]["call_sig"])


def test_generated_static_method_table_is_current(monkeypatch):
    import runpy
    import sys
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "scripts/regen_l1_codegen_static_methods.py"
    monkeypatch.setattr(sys, "argv", [str(script), "--check"])
    namespace = runpy.run_path(str(script))
    assert namespace["main"]() == 0


class _InlineReference(L1CodeGen):
    """The former per-site control flow, using unchanged real raise lowering."""

    def _slot_call_check_status(self, status, operation, span=None):
        failed = self.builder.icmp_signed("<", status, ir.Constant(_I64, 0))
        error = self.current_function.append_basic_block(self._fresh("call.slot.error"))
        ready = self.current_function.append_basic_block(self._fresh("call.slot.ready"))
        self.builder.cbranch(failed, error, ready)
        self.builder.position_at_end(error)
        pending = self.builder.call(self.runtime["py_err_occurred"], [])
        has_error = self.builder.icmp_signed("!=", pending, ir.Constant(_I64, 0))
        report = self.current_function.append_basic_block(self._fresh("call.slot.report"))
        target = self._current_try_err_block()
        if target is None:
            target = self._ensure_fn_err_exit()
        self.builder.cbranch(has_error, target, report)
        self.builder.position_at_end(report)
        self._emit_builtin_exception_and_branch(
            "RuntimeError", "slot-call " + operation + " failed", span,
        )
        self.builder.position_at_end(ready)


@pytest.fixture(autouse=True)
def _explicit_text_route(monkeypatch):
    # The instruction interpreter needs text. Direct/no-text is tested below
    # by an explicitly separate route, rather than inheriting runner flags.
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "0")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "0")
    monkeypatch.setenv("PCC_SELF_TARGET_PASSES", "off")


def _sites(cls=L1CodeGen, *, spans=(None,), function_name="caller", target=True, symbol="probe"):
    codegen = cls(Module(name="slot_status", body=()))
    codegen.module.triple = "aarch64-apple-darwin"
    function = ir.Function(codegen.module, ir.FunctionType(_I64, [_I64] * len(spans)), symbol)
    entry = function.append_basic_block("entry")
    codegen.current_function = function
    codegen.current_func_def = (
        None if function_name is None else FuncDef(
            span=SourceSpan("source.py", 1, 0, 1, 2), name=function_name,
            args=(), return_ty=None, body=(),
        )
    )
    codegen._current_entry_block = entry
    codegen.builder = ir.IRBuilder(entry)
    for index, span in enumerate(spans):
        if span is not None:
            codegen._source_file_lines_cache[span.file] = ["  call(value='雪')  "]
        error = function.append_basic_block("cleanup." + str(index)) if target else None
        codegen._try_err_block = error
        codegen._slot_call_check_status(function.args[index], "operation " + str(index), span)
        if error is not None:
            saved = codegen.builder.block
            codegen.builder.position_at_end(error)
            codegen.builder.ret(ir.Constant(_I64, -index - 1))
            codegen.builder.position_at_end(saved)
    codegen.builder.ret(ir.Constant(_I64, 17))
    return codegen, function


class _RuntimeTrace:
    def __init__(self, pending, new_exception, current_after_raise):
        self.current = pending
        self.new_exception = new_exception
        self.current_after_raise = current_after_raise
        self.events = []

    def call(self, name, arguments):
        self.events.append((name, tuple(arguments)))
        if name == "py_err_occurred":
            return int(self.current is not None)
        if name == "py_exc_new":
            return self.new_exception
        if name == "py_raise":
            self.current = self.current_after_raise
            return None
        if name == "py_current_exception":
            return self.current
        if name == "py_exc_append_frame_source":
            return None
        raise AssertionError("unexpected runtime call: " + name)


def _execute(function, arguments, runtime):
    """Fail-closed evaluator for only the emitted reporter's five opcodes."""
    module = function.module
    values = {str(arg): value for arg, value in zip(function.args, arguments)}

    def value(token):
        token = token.strip()
        if token in values:
            return values[token]
        if token == "null":
            return None
        if token in ("true", "false"):
            return token == "true"
        if token.startswith("@"):
            global_ = module.globals[token[1:]]
            return bytes(global_.initializer.value).decode("utf-8").removesuffix("\0")
        return int(token)

    blocks = {block.name: block for block in function.blocks}
    block = function.blocks[0]
    for _ in range(200):
        for record in block.instructions:
            instruction = str(record)
            result = None
            if " = " in instruction:
                result, instruction = instruction.split(" = ", 1)
            if instruction.startswith("call "):
                match = re.fullmatch(r"call (?:void|ptr|i64)(?: \([^()]*\))? @([^()]+)\((.*)\)", instruction)
                assert match is not None, instruction
                name, raw = match.groups()
                args = [] if not raw else [value(arg.split(" ", 1)[1]) for arg in raw.split(", ")]
                callee = module.globals[name]
                answer = _execute(callee, args, runtime) if callee.blocks else runtime.call(name, args)
                if result is not None:
                    values[result] = answer
            elif instruction.startswith("icmp "):
                match = re.fullmatch(r"icmp (slt|ne) i64 ([^,]+), (.+)", instruction)
                assert match is not None, instruction
                operation, left, right = match.groups()
                left, right = value(left), value(right)
                values[result] = left < right if operation == "slt" else left != right
            elif instruction.startswith("br label %"):
                block = blocks[instruction.removeprefix("br label %")]
                break
            elif instruction.startswith("br i1 "):
                match = re.fullmatch(r"br i1 ([^,]+), label %([^,]+), label %(.+)", instruction)
                assert match is not None, instruction
                condition, yes, no = match.groups()
                block = blocks[yes if value(condition) else no]
                break
            elif instruction == "ret void":
                return None
            elif instruction.startswith("ret i64 "):
                return value(instruction.removeprefix("ret i64 "))
            else:
                raise AssertionError("unexpected instruction: " + instruction)
        else:
            raise AssertionError("unterminated interpreted block")
    raise AssertionError("unexpected reporter loop")


@pytest.mark.parametrize("status", [-(1 << 63), -1, 0, 1, (1 << 63) - 1])
@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("span", [None, SourceSpan("source.py", 1, 0, 1, 2),
                                  SourceSpan("", 0, 0, 0, 0),
                                  SourceSpan("<generated>", -7, 0, -7, 0)])
def test_status_and_existing_exception_have_identical_runtime_trace(status, pending, span):
    original, allocated, current = object(), object(), object()
    traces = []
    for cls in (_InlineReference, L1CodeGen):
        codegen, function = _sites(cls, spans=(span,))
        verify_ir_text(str(codegen.module))
        trace = _RuntimeTrace(original if pending else None, allocated, current)
        result = _execute(function, [status], trace)
        traces.append((result, trace.current, trace.events))
    assert traces[0] == traces[1]
    result, selected, events = traces[1]
    assert result == (-1 if status < 0 else 17)
    if status >= 0:
        assert events == []
        assert selected is (original if pending else None)
    elif pending:
        assert events == [("py_err_occurred", ())]
        assert selected is original
    else:
        assert events[:4] == [
            ("py_err_occurred", ()),
            ("py_exc_new", (7, "slot-call operation 0 failed")),
            ("py_raise", (allocated,)),
            ("py_current_exception", ()),
        ]
        assert selected is current
        assert len(events) == (4 if span is None else 5)
        if span is not None:
            assert events[4] == ("py_exc_append_frame_source", (
                current, "caller", span.file or "<unknown>",
                "call(value='雪')" if span.line == 1 else "", span.line,
            ))


@pytest.mark.parametrize("function_name", [None, "named"])
@pytest.mark.parametrize("new_exception", [None, "allocated-exception"])
def test_null_allocation_module_frame_and_fallback_exit(function_name, new_exception):
    span = SourceSpan("source.py", 1, 0, 1, 9)
    traces = []
    for cls in (_InlineReference, L1CodeGen):
        codegen, function = _sites(cls, spans=(span,), function_name=function_name, target=False)
        verify_ir_text(str(codegen.module))
        trace = _RuntimeTrace(None, new_exception, new_exception)
        result = _execute(function, [-1], trace)
        traces.append((result, trace.events))
    assert traces[0] == traces[1]
    assert traces[1][0] == 0
    assert traces[1][1][-1][1][1] == (function_name or "<module>")


def test_distinct_operations_and_cleanup_targets_keep_site_order():
    spans = (None, SourceSpan("source.py", 1, 0, 1, 2), None)
    for fail_at in range(3):
        traces = []
        exception = object()
        for cls in (_InlineReference, L1CodeGen):
            codegen, function = _sites(cls, spans=spans)
            trace = _RuntimeTrace(None, exception, exception)
            result = _execute(function, [0] * fail_at + [-1] + [0] * (2 - fail_at), trace)
            traces.append((result, trace.events))
        assert traces[0] == traces[1]
        assert traces[1][0] == -fail_at - 1
        assert traces[1][1][1][1] == (7, "slot-call operation " + str(fail_at) + " failed")


def test_one_module_helper_preserves_builder_and_rejects_cross_module_owner():
    first, function = _sites(spans=(None,) * 12)
    helper = first.runtime[_KEY]
    builder, block = first.builder, first.builder.block
    assert first._slot_call_status_report_helper() is helper
    assert first.builder is builder and first.builder.block is block
    assert first.current_function is function
    assert helper.linkage == "internal"
    assert "noinline" in helper.render()
    assert not any(word in helper.render() for word in ("noreturn", "nounwind", "readnone", "readonly"))
    assert len([f for f in first.module.functions if "slot_call_status_report" in f.name]) == 1
    assert len([b for b in function.blocks if b.name.startswith("call.slot.error")]) == 12
    assert not any(b.name.startswith(("call.slot.report", "raise.cont")) for b in function.blocks)
    for error in (b for b in function.blocks if b.name.startswith("call.slot.error")):
        assert [r.opname for r in error.instructions] == ["call", "br"]
    second, _ = _sites()
    assert second.runtime[_KEY] is not helper
    second.runtime[_KEY] = helper
    with pytest.raises(L1CodegenError, match="^slot-status reporter belongs to another module$"):
        second._slot_call_status_report_helper()


def test_helper_build_failure_restores_caller_builder_and_is_not_cached(monkeypatch):
    codegen = L1CodeGen(Module(name="failed_helper", body=()))
    function = ir.Function(codegen.module, ir.FunctionType(ir.VoidType(), []), "caller")
    codegen.current_function = function
    builder = ir.IRBuilder(function.append_basic_block("entry"))
    codegen.builder = builder
    real_call = ir.IRBuilder.call

    def fail_helper(self, callee, args, *a, **kw):
        if callee.name == "py_raise":
            raise RuntimeError("injected helper construction failure")
        return real_call(self, callee, args, *a, **kw)

    monkeypatch.setattr(ir.IRBuilder, "call", fail_helper)
    with pytest.raises(RuntimeError, match="^injected helper construction failure$"):
        codegen._slot_call_status_report_helper()
    assert codegen.builder is builder and codegen.current_function is function
    assert _KEY not in codegen.runtime


def test_helper_symbol_collision_and_module_replacement():
    codegen = L1CodeGen(Module(name="collision", body=()))
    counter = codegen._tmp_counter
    name = codegen._fresh("_pcc_slot_call_status_report")
    codegen._tmp_counter = counter
    unrelated = ir.Function(codegen.module, ir.FunctionType(ir.VoidType(), []), name)
    caller = ir.Function(codegen.module, ir.FunctionType(ir.VoidType(), []), "caller")
    codegen.current_function = caller
    codegen.builder = ir.IRBuilder(caller.append_basic_block("entry"))
    codegen.builder.ret_void()
    helper = codegen._slot_call_status_report_helper()
    assert helper.name != name and codegen.module.globals[name] is unrelated
    old_module = codegen.module
    codegen.generate(Module(name="replacement", body=()))
    assert codegen.module is not old_module
    assert _KEY not in codegen.runtime
    assert codegen._slot_call_status_report_helper() is not helper


def test_live_roots_leases_and_cleanup_are_not_moved_into_reporter():
    codegen = L1CodeGen(Module(name="rooted_status", body=()))
    function = ir.Function(codegen.module, ir.FunctionType(_I64, [_I64]), "probe")
    entry = function.append_basic_block("entry")
    target = function.append_basic_block("outer.error")
    codegen.current_function = function
    codegen._current_entry_block = entry
    codegen.builder = ir.IRBuilder(entry)
    roots = [codegen._new_slot_call_root("operand." + str(i)) for i in range(2)]
    leases = ((roots[0], ir.Constant(_I64, 19)),)
    cleanup = codegen._slot_call_cleanup_block(roots, target, leases)
    cleanup_before = tuple(str(r) for r in cleanup.instructions)
    records = tuple(codegen._slot_call_root_records)
    cache = dict(codegen._slot_call_cleanup_blocks)
    codegen._try_err_block = cleanup
    codegen._slot_call_check_status(function.args[0], "rooted operand")
    assert tuple(codegen._slot_call_root_records) == records
    assert codegen._slot_call_cleanup_blocks == cache
    assert tuple(str(r) for r in cleanup.instructions) == cleanup_before
    error = next(b for b in function.blocks if b.name.startswith("call.slot.error"))
    assert str(error.instructions[-1]) == "br label %" + cleanup.name
    helper = codegen.runtime[_KEY].render()
    assert not any(name in helper for name in ("pcc_gc_", "py_tls_exc_swap_slot", "py_clear_exception"))
    # Both allocation/raising safepoints remain ordinary calls. Their caller's
    # root retirement, exception-preserving cleanup and lease order are intact.
    assert "@py_exc_new(" in helper and "@py_raise(" in helper
    codegen.builder.ret(ir.Constant(_I64, 17))
    codegen.builder.position_at_end(target)
    codegen.builder.ret(ir.Constant(_I64, -1))
    verify_ir_text(str(codegen.module))


@pytest.mark.parametrize("mode", ["inline", "inline-defined", "always-inline", "pipeline"])
def test_actual_owned_inlining_never_recreates_per_site_reporter(mode):
    from pcc.frontends.python.compiled_owned_passes import run_owned_passes
    from pcc.ir.optimization.inline import inline_module

    codegen, _ = _sites(spans=(SourceSpan("source.py", 1, 0, 1, 2),) * 8)
    helper = codegen.runtime[_KEY]
    text = str(codegen.module)
    if mode == "pipeline":
        result = run_owned_passes(text, ["instsimplify", "simplifycfg", "inline", "inline-defined", "dce"], True)
    else:
        result, _ = inline_module(text, include_definitions=mode == "inline-defined",
                                  require_alwaysinline=mode == "always-inline")
    verify_ir_text(result)
    assert len(re.findall(r"\bcall void[^\n@]*@" + re.escape(helper.name) + r"\(", result)) == 8
    assert len(re.findall(r"\bcall\b[^\n]*@py_exc_new\(", result)) == 1
    assert len(re.findall(r"\bcall\b[^\n]*@py_raise\(", result)) == 1
    assert "noinline" in result


def test_noinline_is_effective_and_module_instruction_reduction_is_structural():
    from pcc.ir.optimization.inline import inline_module

    owners = [_sites(cls, spans=(SourceSpan("source.py", 1, 0, 1, 2),) * 8)[0]
              for cls in (_InlineReference, L1CodeGen)]
    counts = [sum(len(b.instructions) for f in c.module.functions for b in f.blocks)
              for c in owners]
    assert counts[1] < counts[0], counts
    helper = owners[1].runtime[_KEY]
    text = str(owners[1].module)
    # Negative control: without the attribute the ordinary defined inliner
    # must actually consider and expand this real helper shape.
    expanded, changed = inline_module(text.replace(" noinline", ""), include_definitions=True)
    assert changed
    assert not re.search(r"\bcall void[^\n@]*@" + re.escape(helper.name) + r"\(", expanded)
    assert len(re.findall(r"\bcall\b[^\n]*@py_exc_new\(", expanded)) > 1


@pytest.mark.parametrize("target", ["x86_64-linux", "aarch64-darwin"])
@pytest.mark.parametrize("body", [
    "    try:\n        return callee(value=[value], later=[1, 2])\n"
    "    except RuntimeError:\n        return value\n",
    "    yield callee(value=[value])\n    yield callee(value=[value])\n",
])
def test_real_caller_roots_and_generator_flags_pass_precise_stackmaps(target, body):
    from pcc.backend.self_backend_precise_stackmaps import build_stack_map_plans
    from pcc.backend.self_backend_prepare import prepare_module_for_target
    from pcc.backend.self_backend_x86_64_linux import _aggregate_returned_indirect
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.type_infer import infer_module

    module = infer_module(parse_and_lift("def probe(callee, value):\n" + body, "status.py", "status"))
    for cls in (_InlineReference, L1CodeGen):
        codegen = cls(module, ir_scaffold_mode="on")
        codegen._strict_no_libpython = True
        codegen._prefer_native_callable_values = True
        text = str(codegen.generate(module))
        assert codegen._slot_call_root_records
        prepared = prepare_module_for_target(text, aggregate_returned_indirect=_aggregate_returned_indirect)
        plans = build_stack_map_plans(prepared.functions, prepared.globals_, target=target)
        assert len(plans) == len(prepared.functions)
        if cls is L1CodeGen:
            assert _KEY in codegen.runtime


@pytest.mark.parametrize("target", ["x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu",
                                    "aarch64-apple-darwin", "x86_64-pc-windows-msvc"])
def test_direct_no_text_helper_capture_verifies_and_emits_all_targets(monkeypatch, target):
    from pcc.backend.self_backend_verify import verify_parsed_module
    from pcc.backend.self_backend_parse import parse_self_backend_module
    from pcc.backend.target_objects import emit_indexed_assembly, encode_assembly_object

    text_owner, _ = _sites(spans=(None, SourceSpan("source.py", 1, 0, 1, 2)))
    text_owner.module.triple = target
    parsed = parse_self_backend_module(str(text_owner.module))
    verify_parsed_module(parsed)
    expected = encode_assembly_object(emit_indexed_assembly(parsed), target)
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "1")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK", "1")
    owner, _ = _sites(spans=(None, SourceSpan("source.py", 1, 0, 1, 2)))
    owner.module.triple = target
    direct = owner.module.direct_indexed_module()
    assert owner.module._direct_indexed_fallback_records == 0
    records = [r for f in owner.module.functions for b in f.blocks for r in b.instructions]
    assert records and all(r._direct_record_id >= 0 and not r.text for r in records)
    verify_parsed_module(direct)
    actual = encode_assembly_object(emit_indexed_assembly(direct), target)
    assert actual == expected
