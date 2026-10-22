"""Host qualification of the worker's fresh-instance generation route.

Compiler imports stay inside the actual-frontend test. This uses the real
constructor, diagnostic wrapper, lowering, and owned IR, without native builds.
"""

import gc
import inspect

import pytest


_DIAGNOSTIC_ENV = (
    "PCC_PY_DEBUG_INFO", "PCC_DEBUG_CODEGEN_PHASES",
    "PCC_PY_FRONTEND_WORKER_TIMING", "PCC_DIRECT_INDEXED_KERNEL_CAPTURE",
    "PCC_DIRECT_INDEXED_KERNEL_EMIT", "PCC_TEXT_INDEXED_KERNEL_EMIT",
    "PCC_DIRECT_INDEXED_KERNEL_VALIDATE",
)


def _typed_module(name="first_use_fixture", value=42):
    from pcc.frontends.python.py_ast import (
        FuncDef, IntLit, IntType, Module, Return, SourceSpan,
    )

    span = SourceSpan("first_use_fixture.py", 1, 1, 2, 14)
    integer = IntType("int")
    function = FuncDef(
        span, "answer", (), integer,
        (Return(span, IntLit(span, integer, value)),),
        has_return_annotation=True,
    )
    return Module(name, (function,))


def _new_codegen(module):
    from pcc.frontends.python.codegen.layer1 import L1CodeGen

    codegen = L1CodeGen(module)
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    codegen._skip_program_main = True
    codegen._native_module_exports = {}
    codegen._module_source_path = "first_use_fixture.py"
    return codegen


def _check_initial_declaration_and_ir(monkeypatch):
    from pcc.frontends.python.codegen import generation_lowering, layer1_init, marshal

    calls = []
    resets = []
    real_declare = layer1_init.declare_runtime
    real_reset = marshal.reset_boxed_i64_constants

    def declaration(owner):
        calls.append(id(owner))
        return real_declare(owner)

    def reset():
        resets.append(True)
        real_reset()

    monkeypatch.setattr(layer1_init, "declare_runtime", declaration)
    monkeypatch.setattr(generation_lowering, "declare_runtime", declaration)
    monkeypatch.setattr(marshal, "reset_boxed_i64_constants", reset)
    private = _new_codegen(_typed_module())
    initial_module, initial_runtime, initial_printf = (
        private.module, private.runtime, private._printf,
    )
    private_text = private._generate_initial_module()
    assert len(calls) == 1
    assert len(resets) == 1
    assert private.module is initial_module
    assert private.runtime is initial_runtime
    assert private._printf is initial_printf
    assert all(function.module is private.module for function in private.runtime.values())
    assert private._printf.module is private.module
    public = _new_codegen(_typed_module())
    public_initial = public.module
    public_text = public.generate(public.ast_module)
    assert len(calls) == 3
    assert len(resets) == 2
    assert public.module is not public_initial
    assert private_text == public_text
    assert "define " in private_text


def _check_configured_ir(monkeypatch, *, debug, target, retain, direct_only):
    with monkeypatch.context() as patch:
        patch.setenv("PCC_PY_DEBUG_INFO", "1" if debug else "")
        patch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "1" if direct_only else "0")
        results = []
        for first_use in (False, True):
            codegen = _new_codegen(_typed_module())
            initial_module = codegen.module
            initial_file = codegen._di_file
            initial_unit = codegen._di_compile_unit
            codegen._target_triple = target
            codegen.module.data_layout = "e-p:64:64"
            codegen.module._direct_indexed_retain_text = retain
            codegen._sibling_module_inits = ("other",)
            exports = {"other": {}}
            codegen._native_module_exports = exports
            if first_use:
                text = codegen._generate_initial_module()
                assert codegen.module is initial_module
                assert codegen._di_file is initial_file
                assert codegen._di_compile_unit is initial_unit
            else:
                text = codegen.generate(codegen.ast_module)
                assert codegen.module is not initial_module
            assert codegen.module.triple == target
            assert codegen.module.data_layout == "e-p:64:64"
            assert codegen.module._direct_indexed_retain_text is retain
            assert codegen._sibling_module_inits == ("other",)
            assert codegen._native_module_exports is exports
            assert codegen._module_source_path == "first_use_fixture.py"
            assert codegen._strict_no_libpython and codegen._prefer_native_callable_values
            assert codegen._skip_program_main
            assert codegen._printf.module is codegen.module
            assert all(fn.module is codegen.module for fn in codegen.runtime.values())
            if debug:
                assert codegen._di_file in codegen.module.metadata
                assert codegen._di_compile_unit in codegen.module.metadata
                assert all(node in codegen.module.metadata for node in codegen._di_subprograms.values())
            else:
                assert codegen._di_file is None
            assert bool(text) is (not direct_only or retain)
            if text:
                assert ('target triple = "' + target + '"') in text
                assert ("DICompileUnit" in text) is debug
                assert ("DILocation" in text) is debug
            results.append(text)
        assert results[0] == results[1]


def _assert_private_rejected_without_reset(codegen):
    from pcc.frontends.python.codegen import marshal
    from pcc.frontends.python.codegen.errors import CodegenDiagnosticError
    from pcc.ir.compat import ir

    module, runtime = codegen.module, codegen.runtime
    marker = ir.Constant(ir.IntType(64), 123)
    marshal.note_boxed_i64_constant(marker, 123)
    expected = CodegenDiagnosticError if codegen._codegen_trace_is_enabled() else RuntimeError
    with pytest.raises(expected, match="requires a fresh codegen instance"):
        codegen._generate_initial_module()
    assert not codegen._initial_ir_available
    assert codegen.module is module and codegen.runtime is runtime
    assert id(marker) in marshal._BOXED_I64_CONSTANTS
    marshal.reset_boxed_i64_constants()


def _check_public_generation_and_marshal(monkeypatch):
    from pcc.frontends.python.codegen import marshal
    from pcc.ir.compat import ir

    results = []
    for route in ("private", "explicit", "default"):
        codegen = _new_codegen(_typed_module())
        initial_module = codegen.module
        marker = ir.Constant(ir.IntType(64), 7)
        marshal.note_boxed_i64_constant(marker, 7)
        if route == "private":
            text = codegen._generate_initial_module()
        elif route == "explicit":
            text = codegen.generate(codegen.ast_module)
        else:
            text = codegen.generate()
        assert (id(marker) in marshal._BOXED_I64_CONSTANTS) is (route == "default")
        assert (codegen.module is initial_module) is (route != "explicit")
        assert not codegen._initial_ir_available
        _assert_private_rejected_without_reset(codegen)
        # Repeated and overriding public generation retain their reset path.
        same_ast = codegen.ast_module
        previous_module = codegen.module
        repeated = codegen.generate(same_ast)
        assert codegen.module is not previous_module
        replacement = _typed_module("replacement_fixture", 43)
        previous_module = codegen.module
        overridden = codegen.generate(replacement)
        assert codegen.module is not previous_module
        assert codegen.ast_module is replacement
        results.append((text, repeated, overridden))
    assert results[0] == results[1] == results[2]


def _check_error_parity(monkeypatch, capsys, *, debug, failure_point):
    from pcc.frontends.python.codegen import marshal
    from pcc.frontends.python.codegen.errors import CodegenDiagnosticError
    from pcc.frontends.python.codegen.layer1 import L1CodeGen

    observations = []
    with monkeypatch.context() as environment:
        environment.setenv("PCC_DEBUG_CODEGEN_PHASES", "1" if debug else "")
        for first_use in (False, True):
            codegen = _new_codegen(_typed_module())
            failure = LookupError("injected " + failure_point + " failure")

            def fail(*_args, **_kwargs):
                raise failure

            capsys.readouterr()
            with monkeypatch.context() as patch:
                if failure_point == "reset":
                    patch.setattr(marshal, "reset_boxed_i64_constants", fail)
                else:
                    patch.setattr(L1CodeGen, "_emit_user_function", fail)
                expected = CodegenDiagnosticError if debug else LookupError
                with pytest.raises(expected) as caught:
                    if first_use:
                        codegen._generate_initial_module()
                    else:
                        codegen.generate(codegen.ast_module)
            error = caught.value
            if debug:
                assert error.__cause__ is failure
                assert error.original_exception_type == "LookupError"
            else:
                assert error is failure
            observations.append((
                type(error), str(error), getattr(error, "diagnostic_span", None),
                capsys.readouterr().err,
            ))
            assert not codegen._initial_ir_available
            _assert_private_rejected_without_reset(codegen)
            # A public override after the failed private attempt still works.
            codegen.generate(_typed_module("recovered_fixture", 44))
            del caught, error, failure, codegen
        assert observations[0] == observations[1]


def _check_setup_failure_consumes_freshness(monkeypatch, *, trace_failure):
    from pcc.frontends.python.codegen.layer1 import L1CodeGen

    class BrokenTarget:
        def __bool__(self):
            raise LookupError("target setup failure")

    with monkeypatch.context() as environment:
        environment.setenv("PCC_DEBUG_CODEGEN_PHASES", "1" if trace_failure else "")
        for first_use in (False, True):
            codegen = _new_codegen(_typed_module())
            with monkeypatch.context() as patch:
                if trace_failure:
                    def fail_trace(*_args, **_kwargs):
                        raise LookupError("trace setup failure")
                    patch.setattr(L1CodeGen, "_codegen_trace_push", fail_trace)
                else:
                    codegen._target_triple = BrokenTarget()
                with pytest.raises(LookupError):
                    if first_use:
                        codegen._generate_initial_module()
                    else:
                        codegen.generate(codegen.ast_module)
            assert not codegen._initial_ir_available
            codegen._target_triple = ""
            _assert_private_rejected_without_reset(codegen)


def _check_static_native_contract():
    from pcc.frontends.python.codegen import layer1_support
    from pcc.frontends.python.codegen.layer1 import L1CodeGen

    signature = inspect.signature(L1CodeGen.generate)
    assert tuple(signature.parameters) == ("self", "module")
    assert signature.parameters["module"].default is None
    exports = layer1_support._PCC_FRONTEND_STATIC_NATIVE_EXPORTS
    surfaces = (
        ("pcc.frontends.python.codegen.layer1", "L1CodeGen"),
        ("pcc.frontends.python.codegen.layer1_entrypoints", "L1CodeGenEntrypointMixin"),
    )
    for module_name, class_name in surfaces:
        methods = exports[module_name][class_name]["methods"]
        for name, arguments, types in (
            ("generate", ("self", "module"), (("dyn",), ("dyn",))),
            ("_generate_initial_module", ("self",), (("dyn",),)),
            ("_generate_with_diagnostics", ("self", "module", "reuse_initial_ir"),
             (("dyn",), ("dyn",), ("bool",))),
        ):
            selected = [method for method in methods if method["name"] == name]
            assert len(selected) == 1
            assert tuple(arg["name"] for arg in selected[0]["call_sig"]) == arguments
            assert selected[0]["param_types"] == types
            if name == "generate":
                assert selected[0]["call_sig"][1]["has_default"]
    lowering = exports["pcc.frontends.python.codegen.generation_lowering"]["GenerationLoweringMixin"]["methods"][0]
    assert lowering["name"] == "_generate_impl"
    assert lowering["param_types"] == (("dyn",), ("dyn",), ("bool",))
    assert lowering["call_sig"][-1]["name"] == "reuse_initial_ir"
    assert lowering["call_sig"][-1]["has_default"]


def test_actual_first_use_route_contract(monkeypatch, capsys):
    # One real-compiler case owns its process-global marshal/context state.
    # Pytest still uses the repository's ordinary six/loadgroup workers.
    for name in _DIAGNOSTIC_ENV:
        monkeypatch.delenv(name, raising=False)
    with monkeypatch.context() as patch:
        _check_initial_declaration_and_ir(patch)
    gc.collect()
    for debug, target, retain, direct_only in (
        (False, "x86_64-unknown-linux-gnu", False, False),
        (True, "aarch64-unknown-linux-gnu", True, False),
        (False, "x86_64-unknown-linux-gnu", False, True),
        (True, "aarch64-unknown-linux-gnu", True, True),
    ):
        _check_configured_ir(monkeypatch, debug=debug, target=target,
                             retain=retain, direct_only=direct_only)
        gc.collect()
    _check_public_generation_and_marshal(monkeypatch)
    gc.collect()
    for debug in (False, True):
        for failure_point in ("reset", "body"):
            _check_error_parity(monkeypatch, capsys, debug=debug, failure_point=failure_point)
            gc.collect()
    for trace_failure in (False, True):
        _check_setup_failure_consumes_freshness(monkeypatch, trace_failure=trace_failure)
        gc.collect()
    _check_static_native_contract()

