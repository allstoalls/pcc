from __future__ import annotations

import textwrap

from pcc.ir.compat import ir
from pcc.frontends.python import low_ir
from pcc.frontends.python import parser
from pcc.frontends.python import type_infer
from pcc.frontends.python.codegen import layer1
from pcc.frontends.python.codegen.runtime_abi import declare_runtime
from pcc.frontends.python.py_ast import FuncDef, SourceSpan


def _typed_func(source: str, name: str) -> FuncDef:
    mod = type_infer.infer_module(parser.parse(source, name + ".py"))
    for stmt in mod.body:
        if isinstance(stmt, FuncDef) and stmt.name == name:
            return stmt
    raise AssertionError("missing function " + name)


def test_low_ir_builds_blocks_for_typed_int_loop():
    fd = _typed_func(
        textwrap.dedent(
            """
            def bench(n: int) -> int:
                acc: int = 0
                i: int = 0
                while i < n:
                    acc = acc + (i % 7)
                    i = i + 1
                return acc
            """
        ),
        "bench",
    )
    low_fn = layer1._low_ir_lower_typed_int_function(
        fd,
        "user_probe_bench",
        {"bench": "user_probe_bench"},
    )

    assert low_fn is not None
    assert low_fn.params == (("n", low_ir.LOW_I64),)
    assert ("acc", low_ir.LOW_I64) in low_fn.locals
    assert ("i", low_ir.LOW_I64) in low_fn.locals
    block_names = [block.name for block in low_fn.blocks]
    assert block_names == ["entry", "while.cond.1", "while.body.2", "while.end.3"]
    assert isinstance(low_fn.blocks[0].terminator, low_ir.LowBranch)
    assert isinstance(low_fn.blocks[1].terminator, low_ir.LowCondBranch)
    assert isinstance(low_fn.blocks[-1].terminator, low_ir.LowReturn)


def test_low_ir_direct_call_is_explicit_value_node():
    fd = _typed_func(
        textwrap.dedent(
            """
            def add(a: int, b: int) -> int:
                return a + b

            def bench(n: int) -> int:
                return add(n, 1)
            """
        ),
        "bench",
    )
    low_fn = layer1._low_ir_lower_typed_int_function(
        fd,
        "user_probe_bench",
        {"add": "user_probe_add", "bench": "user_probe_bench"},
    )

    assert low_fn is not None
    term = low_fn.blocks[0].terminator
    assert isinstance(term, low_ir.LowReturn)
    assert isinstance(term.value, low_ir.LowCallDirect)
    assert term.value.symbol == "user_probe_add"


def test_low_ir_return_keeps_same_typed_value_without_identity_coerce():
    fd = _typed_func(
        textwrap.dedent(
            """
            def main() -> int:
                return 7
            """
        ),
        "main",
    )
    low_fn = layer1._low_ir_lower_typed_int_function(
        fd,
        "user_probe_main",
        {"main": "user_probe_main"},
    )

    assert low_fn is not None
    term = low_fn.blocks[0].terminator
    assert isinstance(term, low_ir.LowReturn)
    assert isinstance(term.value, low_ir.LowConst)
    assert term.value.value == 7


def test_low_ir_may_raise_calls_post_call_hook():
    module = ir.Module(name="low_ir_hook")
    runtime = declare_runtime(module)
    fn_ty = ir.FunctionType(ir.IntType(64), [], var_arg=False)
    fn = ir.Function(module, fn_ty, name="probe")
    span = SourceSpan(file="probe.py", line=1, col=0, end_line=1, end_col=1)
    block = low_ir.LowBlock(
        name="entry",
        instrs=[
            low_ir.LowStoreLocal(
                name="flag",
                value=low_ir.LowCallRuntime(
                    ty=low_ir.LOW_I64,
                    name="py_err_occurred",
                    args=(),
                    may_raise=True,
                    span=span,
                ),
            )
        ],
        terminator=low_ir.LowReturn(
            value=low_ir.LowLocal(ty=low_ir.LOW_I64, name="flag"),
        ),
    )
    low_fn = low_ir.LowFunction(
        name="probe",
        symbol="probe",
        params=(),
        return_ty=low_ir.LOW_I64,
        blocks=(block,),
        locals=(("flag", low_ir.LOW_I64),),
    )
    seen = []

    layer1._low_ir_emit_function_to_llvm(
        low_fn,
        llvm_module=module,
        fn=fn,
        runtime=runtime,
        functions={"probe": fn},
        post_call_error_check=lambda call_span: seen.append(call_span),
    )

    assert seen == [span]
    text = str(module)
    assert "call i64 () @py_err_occurred()" in text
    assert "ret i64" in text
