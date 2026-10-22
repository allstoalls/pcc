"""Host-only coverage for generation attribution, without native provisioning.

Execute the production generation method with small phase doubles. Loading its
source AST avoids importing every lowering mixin in each default xdist worker;
these tests establish timing/control-flow behavior, not native code correctness.
"""

import ast
import io
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


SOURCE = (Path(__file__).resolve().parents[2] / "pcc" / "frontends" / "python"
          / "codegen" / "generation_lowering.py")
PHASES = ("prepare", "block_bodies", "module_init", "finalize")
DIAGNOSTIC_ENV = (
    "PCC_DEBUG_CODEGEN_PHASES", "PCC_DIRECT_INDEXED_KERNEL_CAPTURE",
    "PCC_DIRECT_INDEXED_KERNEL_EMIT", "PCC_TEXT_INDEXED_KERNEL_EMIT",
    "PCC_DIRECT_INDEXED_KERNEL_VALIDATE",
)


class _Clock:
    def __init__(self):
        self.calls = 0

    def monotonic(self):
        self.calls += 1
        return float(self.calls)


def _production_generation():
    """Use the actual method and its local helpers, with explicit phase owners."""
    parsed = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    selected = [node for node in parsed.body if isinstance(
        node, (ast.FunctionDef, ast.ClassDef),
    )]
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, *selected], type_ignores=[]))
    clock, output, events = _Clock(), io.StringIO(), []
    node_names = (
        "Assign", "AugAssign", "ClassDef", "Delete", "Raise", "DynType",
        "ExprStmt", "For", "FuncDef", "If", "ImportFrom", "Module", "Name",
        "Stmt", "StrLit", "Try", "While", "With", "Call", "ListExpr", "TupleExpr",
    )
    kinds = {name: type(name, (), {}) for name in node_names}

    def node(kind, **values):
        value = kinds[kind]()
        value.__dict__.update(values)
        return value

    class IRModule:
        def __init__(self, name):
            self.name = name
            self.triple = "unknown-unknown-unknown"
            self.data_layout = ""
            self._direct_indexed_retain_text = False

        def __str__(self):
            events.append("render")
            return "unchanged IR"

    def operation(name, result=None):
        def run(*_args, **_kwargs):
            events.append(name)
            return result
        return run

    namespace = dict(kinds)
    namespace.update({
        "os": os, "sys": SimpleNamespace(stderr=output), "time": clock,
        "ir": SimpleNamespace(Module=IRModule),
        "marshal": SimpleNamespace(reset_boxed_i64_constants=operation("reset_constants")),
        "declare_runtime": operation("declare_runtime", {}),
        "hoist_nested_funcdefs": operation("hoist", []),
        "emit_module_str_default_cells": operation("str_default_cells"),
        "compute_vthread_may_park_callables": operation("effects", (set(), set(), set(), set())),
        "classify_vthread_park_boundaries": operation("boundaries", {}),
        "compute_bounded_int_abi_function_names": operation("bounded_int", []),
        "postprocess_varargs_ir": lambda text: text,
        "L1CodegenError": RuntimeError,
        "_is_import_stmt": lambda statement: False,
        "_is_import_from_stmt": lambda statement: False,
    })
    exec(compile(module, str(SOURCE), "exec"), namespace)

    class Host(namespace["GenerationLoweringMixin"]):
        def __init__(self, *, skip_main=True, freestanding=False, fail_body=False):
            function = node("FuncDef", name="work", body=())
            klass = node("ClassDef", name="Example", body=())
            block_function = node("FuncDef", name="conditional", body=())
            block_class = node("ClassDef", name="ConditionalClass", body=())
            block = node("If", cond=None, body=(block_function, block_class), else_body=())
            body = () if freestanding else (function, klass, block)
            self.ast_module = node("Module", name="timing_fixture", body=body)
            self.module = IRModule("timing_fixture")
            self.module.triple = "selected-target"
            self.module.data_layout = "selected-layout"
            self._skip_program_main = skip_main
            self._freestanding_module = freestanding
            self._runtime_port_module = False
            self._sibling_module_inits = ("sibling",)
            self._native_module_exports = {"sibling": {}}
            self._hoisted_class_capture_params = {}
            self.fail_body = fail_body
            self.class_lowering = SimpleNamespace(
                prepare_class_init_outlines=operation("prepare_classes"),
                declare_class=operation("declare_class"),
                emit_methods=lambda statement: events.append("class:" + statement.name),
                emit_module_init=operation("class_init"),
            )

        def _emit_user_function(self, statement):
            events.append("function:" + statement.name)
            if self.fail_body:
                raise LookupError("original lowering failure")

        def _emit_module_top_init(self, body):
            events.append("module_init")

        def _emit_program_main(self, body):
            events.append("program_main")

    for name in (
        "_di_init", "_declare_printf", "_rewrite_traceback_handler_bindings",
        "_collect_module_del_targets", "_prescan_function_module_globals",
        "_declare_user_function", "_prescan_nested_imports", "_ensure_module_global_name",
        "_predeclare_native_builtin_module_attr_stores", "_emit_module_teardown",
        "_finalize_traceback_index_tables", "_finalize_static_literal_init",
    ):
        setattr(Host, name, operation(name))
    Host._module_imports_raw_int_scaffold = operation("raw_int", False)
    Host._module_imports_c_abi_export = operation("c_abi", False)
    Host._compute_typed_int_abi_call_arg_safety = operation("call_safety", [])
    Host._static_bool_condition = lambda self, condition: None
    # The module-block declaration helper calls this semantic type constructor.
    namespace["DynType"] = lambda name: SimpleNamespace(name=name)
    return Host, namespace, clock, output, events


@pytest.fixture(autouse=True)
def _disable_unrelated_diagnostics(monkeypatch):
    for name in DIAGNOSTIC_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("value", [None, "", "0", "false", "yesplease"])
def test_disabled_generation_timing_has_no_output_or_clock_calls(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("PCC_PY_FRONTEND_WORKER_TIMING", raising=False)
    else:
        monkeypatch.setenv("PCC_PY_FRONTEND_WORKER_TIMING", value)
    Host, _namespace, clock, output, events = _production_generation()
    host = Host()
    assert host._generate_impl(host.ast_module) == "unchanged IR"
    assert output.getvalue() == ""
    assert clock.calls == 0
    assert events.count("reset_constants") == 1
    assert host.module.triple == "selected-target"
    assert host.module.data_layout == "selected-layout"
    assert host._sibling_module_inits == ("sibling",)
    assert host._native_module_exports == {"sibling": {}}


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", " TRUE "])
def test_enabled_generation_timing_attributes_phases_and_keeps_labels(monkeypatch, value):
    monkeypatch.setenv("PCC_PY_FRONTEND_WORKER_TIMING", value)
    Host, _namespace, clock, output, _events = _production_generation()
    host = Host()
    assert host._generate_impl(host.ast_module) == "unchanged IR"
    assert output.getvalue().splitlines() == [
        "pcc frontend phase module=timing_fixture name=prepare elapsed_ms=1000",
        "pcc frontend function start module=timing_fixture name=work",
        "pcc frontend function done module=timing_fixture name=work elapsed_ms=1000",
        "pcc frontend class start module=timing_fixture name=Example",
        "pcc frontend class done module=timing_fixture name=Example elapsed_ms=1000",
        "pcc frontend phase module=timing_fixture name=block_bodies elapsed_ms=1000",
        "pcc frontend phase module=timing_fixture name=module_init elapsed_ms=1000",
        "pcc frontend phase module=timing_fixture name=finalize elapsed_ms=1000",
        "pcc llvm text render module=timing_fixture elapsed_ms=1000",
    ]
    assert clock.calls == 14


@pytest.mark.parametrize("options", [
    {}, {"skip_main": False}, {"freestanding": True},
])
def test_timing_preserves_generation_work_and_output(monkeypatch, options):
    observations = []
    for enabled in ("0", "1"):
        monkeypatch.setenv("PCC_PY_FRONTEND_WORKER_TIMING", enabled)
        Host, _namespace, _clock, output, events = _production_generation()
        host = Host(**options)
        result = host._generate_impl(host.ast_module)
        observations.append((result, list(events)))
        if enabled == "1":
            phases = [line.split(" name=", 1)[1].split()[0]
                      for line in output.getvalue().splitlines()
                      if line.startswith("pcc frontend phase ")]
            assert tuple(phases) == PHASES
    assert observations[0] == observations[1]


def test_timing_preserves_direct_no_text_and_retain_text_routes(monkeypatch):
    monkeypatch.setenv("PCC_PY_FRONTEND_WORKER_TIMING", "1")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "1")
    for retain in (False, True):
        Host, _namespace, _clock, output, events = _production_generation()
        host = Host()
        host.module._direct_indexed_retain_text = retain
        assert host._generate_impl(host.ast_module) == ("unchanged IR" if retain else "")
        assert ("render" in events) is retain
        assert host.module._direct_indexed_retain_text is retain
        assert "pcc llvm text render module=timing_fixture elapsed_ms=1000" in output.getvalue()


def test_failure_does_not_report_unfinished_phases(monkeypatch):
    monkeypatch.setenv("PCC_PY_FRONTEND_WORKER_TIMING", "1")
    Host, _namespace, _clock, output, events = _production_generation()
    host = Host(fail_body=True)
    with pytest.raises(LookupError, match="original lowering failure"):
        host._generate_impl(host.ast_module)
    text = output.getvalue()
    assert "name=prepare elapsed_ms=1000" in text
    assert "pcc frontend function start module=timing_fixture name=work" in text
    assert "pcc frontend function done" not in text
    assert "name=module_init" not in text and "name=finalize" not in text
    assert "module_init" not in events and "render" not in events


def test_generation_without_override_keeps_constructor_state(monkeypatch):
    monkeypatch.setenv("PCC_PY_FRONTEND_WORKER_TIMING", "1")
    Host, _namespace, _clock, _output, events = _production_generation()
    host = Host(freestanding=True)
    original_module = host.module
    assert host._generate_impl() == "unchanged IR"
    assert host.module is original_module
    assert "reset_constants" not in events and "declare_runtime" not in events
