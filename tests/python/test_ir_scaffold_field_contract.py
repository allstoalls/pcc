"""Self-host builder fields have checkable closed-world owners and writes."""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace

from pcc.frontends.c.codegen.c_codegen import CCodeGenerator
from pcc.ir import compat, ir
from pcc.frontends.python.codegen.layer1 import L1CodeGen


def _class_sources(root):
    seen = set()
    for cls in root.__mro__:
        if not cls.__module__.startswith("pcc."):
            continue
        path = inspect.getsourcefile(cls)
        if path is None:
            continue
        key = (Path(path), cls.__name__)
        if key in seen:
            continue
        seen.add(key)
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == cls.__name__:
                yield key[0], node


def _self_field(target, name):
    return (
        isinstance(target, ast.Attribute)
        and target.attr == name
        and isinstance(target.value, ast.Name)
        and target.value.id == "self"
    )


def _is_irbuilder_ctor(value):
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and value.func.attr == "IRBuilder"
        and isinstance(value.func.value, ast.Name)
        and value.func.value.id == "ir"
    )


def test_owned_compat_bindings_are_the_real_provider():
    assert compat.ir is ir
    assert compat.ir_py is ir
    assert compat.ir_c is ir


def test_field_owner_uses_real_mro_name_and_source_module():
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.type_infer import infer_module

    ast_module = infer_module(
        parse_and_lift("x = 1\n", "<contract>", "pcc.frontends.python.codegen.unsafe_lowering")
    )
    codegen = L1CodeGen(ast_module, ir_scaffold_mode="on")
    codegen.current_class = SimpleNamespace(
        name="UnsafeIntrinsicMixin", owning_module=None, export_class_name=None
    )
    assert codegen._scaffold_current_class_owner() == "L1CodeGen"
    codegen.ast_module = replace(ast_module, name="pcc.frontends.c.codegen.c_codegen")
    codegen.current_class = SimpleNamespace(
        name="CCodeGenerator", owning_module=None, export_class_name=None
    )
    assert codegen._scaffold_current_class_owner() == "CCodeGenerator"
    codegen.ast_module = replace(ast_module, name="application")
    assert codegen._scaffold_current_class_owner() == ""


def test_l1_and_c_codegen_builder_writes_stay_within_verified_facts():
    # The slot-based and legacy lambda adapters each restore the caller's
    # saved builder on rejection, in addition to the shared successful exit.
    # The aggregate adapter also restores the saved builder in its finally block.
    # The outlined slot-call status reporter builds its own function and then
    # restores the caller's builder (02b9bc2d3; 9007dbd1d moved one restore).
    expected_count = {"L1CodeGen": 37, "CCodeGenerator": 4}
    for root in (L1CodeGen, CCodeGenerator):
        writes = []
        for path, cls in _class_sources(root):
            for method in cls.body:
                if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                alias_writes = {}
                for node in ast.walk(method):
                    if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                        continue
                    targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
                    for target in targets:
                        if isinstance(target, ast.Name):
                            alias_writes.setdefault(target.id, []).append(node.value)
                for node in ast.walk(method):
                    if isinstance(node, (ast.AugAssign, ast.Delete)):
                        targets = (node.target,) if isinstance(node, ast.AugAssign) else node.targets
                        assert not any(_self_field(target, "builder") for target in targets), (path, node.lineno)
                    if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                        continue
                    targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
                    for target in targets:
                        if not _self_field(target, "builder"):
                            continue
                        writes.append((path, node.lineno))
                        value = node.value
                        if isinstance(value, ast.Constant) and value.value is None:
                            continue
                        if _is_irbuilder_ctor(value):
                            continue
                        assert isinstance(value, ast.Name), (path, node.lineno, ast.unparse(value))
                        sources = alias_writes.get(value.id, [])
                        assert sources, (path, node.lineno, value.id)
                        for source in sources:
                            assert _self_field(source, "builder") or _is_irbuilder_ctor(source), (
                                path, node.lineno, value.id, ast.unparse(source)
                            )
        assert len(writes) == expected_count[root.__name__], (root.__name__, writes)


def test_lambda_adapter_builder_restores_keep_the_callers_verified_owner():
    from pcc.frontends.python.codegen.lambda_helpers_lowering import LambdaHelperLoweringMixin

    tree = ast.parse(Path(inspect.getsourcefile(LambdaHelperLoweringMixin)).read_text())
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    method = next(
        node for node in owner.body
        if isinstance(node, ast.FunctionDef) and node.name == "_maybe_emit_native_lambda_func"
    )
    saved = [
        node.value for node in ast.walk(method)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "saved_builder" for target in node.targets)
    ]
    assert len(saved) == 1 and _self_field(saved[0], "builder")
    writes = [
        node.value for node in ast.walk(method)
        if isinstance(node, ast.Assign)
        and any(_self_field(target, "builder") for target in node.targets)
    ]
    assert sum(_is_irbuilder_ctor(value) for value in writes) == 1
    assert sum(isinstance(value, ast.Name) and value.id == "saved_builder" for value in writes) == 3
    handlers = [node for node in ast.walk(method) if isinstance(node, ast.ExceptHandler)]
    assert len(handlers) == 2
    for handler in handlers:
        assert isinstance(handler.type, ast.Name) and handler.type.id == "NotImplementedError"
        restoration = handler.body[0]
        assert isinstance(restoration, ast.Assign)
        assert len(restoration.targets) == 1 and _self_field(restoration.targets[0], "builder")
        assert isinstance(restoration.value, ast.Name) and restoration.value.id == "saved_builder"


def test_class_lowering_parent_is_constructed_from_l1_codegen():
    from pcc.frontends.python.codegen.class_gen import ClassLowering

    path = Path(inspect.getsourcefile(ClassLowering))
    tree = ast.parse(path.read_text(encoding="utf-8"))
    writes = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
            if any(_self_field(target, "parent") for target in targets):
                writes.append(node.value)
    assert len(writes) == 1
    assert isinstance(writes[0], ast.Name) and writes[0].id == "parent"

    init_path = Path(inspect.getsourcefile(L1CodeGen)) .with_name("layer1_init.py")
    init_tree = ast.parse(init_path.read_text(encoding="utf-8"))
    calls = [
        node for node in ast.walk(init_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ClassLowering"
    ]
    assert len(calls) == 1
    assert len(calls[0].args) == 1
    assert isinstance(calls[0].args[0], ast.Name) and calls[0].args[0].id == "self"


def test_classgen_helper_parameters_have_only_proven_callers():
    from pcc.frontends.python.codegen.class_gen import ClassLowering

    path = Path(inspect.getsourcefile(ClassLowering))
    tree = ast.parse(path.read_text(encoding="utf-8"))
    helpers = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and any(argument.arg == "parent" for argument in node.args.args)
    }
    expected_helper_calls = {
        "_classgen_annotation_is_object_param": 2,
        "_classgen_attr_name_ptr": 2,
        "_classgen_current_name_load": 7,
        "_classgen_current_param_ref_text": 1,
        "_classgen_dataclass_factory_default": 1,
        "_classgen_emit_arg_expr": 4,
        "_classgen_emit_bool_literal_fallback": 4,
        "_classgen_emit_container_literal_fallback": 8,
        "_classgen_emit_dynamic_attr_value": 2,
        "_classgen_emit_int_literal_fallback": 4,
        "_classgen_emit_none_literal_fallback": 5,
        "_classgen_emit_str_literal_fallback": 4,
        "_classgen_emit_str_literal_object": 2,
        "_classgen_expr_class_hint": 1,
        "_classgen_literal_fallback": 2,
        "_classgen_local_assignment_class_info": 1,
        "_classgen_log": 11,
        "_classgen_maybe_unbox_recovered_arg": 8,
        "_classgen_method_return_hint_from_info": 1,
        "_classgen_recover_attr_value": 4,
        "_classgen_recover_call_value": 2,
        "_classgen_recover_method_call_arg": 1,
        "_classgen_recover_self_method_call_value": 1,
        "_classgen_unbox_into_scalar_slot": 2,
    }

    def is_self_parent(value):
        return (
            isinstance(value, ast.Attribute)
            and value.attr == "parent"
            and isinstance(value.value, ast.Name)
            and value.value.id == "self"
        )

    class Calls(ast.NodeVisitor):
        def __init__(self):
            self.owner_class = ""
            self.functions = []
            self.parent_calls = {}
            self.builder_calls = 0

        def visit_ClassDef(self, node):
            previous = self.owner_class
            self.owner_class = node.name
            self.generic_visit(node)
            self.owner_class = previous

        def visit_FunctionDef(self, node):
            self.functions.append(node)
            self.generic_visit(node)
            self.functions.pop()

        def visit_Call(self, node):
            name = node.func.id if isinstance(node.func, ast.Name) else ""
            if name in helpers:
                args = helpers[name].args.args
                index = next(i for i, argument in enumerate(args) if argument.arg == "parent")
                assert index < len(node.args), (name, node.lineno)
                actual = node.args[index]
                if isinstance(actual, ast.Name) and actual.id == "parent":
                    anchors = [i for i, function in enumerate(self.functions) if function.name in helpers]
                    if not anchors:
                        assert self.owner_class == "ClassLowering" and self.functions, (
                            name, node.lineno
                        )
                        if self.functions[0].name == "emit_instantiate":
                            assert any(arg.arg == "parent" for arg in self.functions[0].args.args)
                        else:
                            writes = [
                                inner.value
                                for inner in ast.walk(self.functions[0])
                                if isinstance(inner, ast.Assign)
                                and any(
                                    isinstance(target, ast.Name) and target.id == "parent"
                                    for target in inner.targets
                                )
                            ]
                            assert writes and all(is_self_parent(value) for value in writes), (
                                name, node.lineno
                            )
                    for nested in self.functions[anchors[-1] + 1:] if anchors else self.functions[1:]:
                        assert all(arg.arg != "parent" for arg in nested.args.args)
                        assert not any(
                            isinstance(inner, (ast.Assign, ast.AnnAssign))
                            and any(
                                isinstance(target, ast.Name) and target.id == "parent"
                                for target in (inner.targets if isinstance(inner, ast.Assign) else (inner.target,))
                            )
                            for inner in ast.walk(nested)
                        ), f"{name}:{node.lineno} shadows parent"
                else:
                    assert is_self_parent(actual) and self.owner_class == "ClassLowering", (
                        name, node.lineno, ast.unparse(actual)
                    )
                self.parent_calls[name] = self.parent_calls.get(name, 0) + 1
            if name == "_classgen_builder_call":
                assert any(function.name in helpers for function in self.functions)
                assert len(node.args) >= 1
                receiver = node.args[0]
                assert (
                    isinstance(receiver, ast.Attribute)
                    and receiver.attr == "builder"
                    and isinstance(receiver.value, ast.Name)
                    and receiver.value.id == "parent"
                ), (node.lineno, ast.unparse(receiver))
                self.builder_calls += 1
            self.generic_visit(node)

    calls = Calls()
    calls.visit(tree)
    assert set(helpers) == set(expected_helper_calls)
    assert calls.parent_calls == expected_helper_calls
    assert calls.builder_calls == 1

    for source in path.parents[2].rglob("*.py"):
        if source == path:
            continue
        text = source.read_text(encoding="utf-8")
        assert not any(name in text for name in helpers), source

    instantiate_calls = []
    for source in path.parent.glob("*.py"):
        if source == path:
            continue
        source_tree = ast.parse(source.read_text(encoding="utf-8"))
        call_owners = {}
        for function in ast.walk(source_tree):
            if isinstance(function, ast.FunctionDef):
                for call in ast.walk(function):
                    if isinstance(call, ast.Call):
                        call_owners[id(call)] = function.name
        for node in ast.walk(source_tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "emit_instantiate":
                continue
            assert (
                isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "class_lowering"
                and isinstance(node.func.value.value, ast.Name)
                and node.func.value.value.id == "self"
            ), (source, node.lineno)
            assert node.args and isinstance(node.args[-1], ast.Name) and node.args[-1].id == "self", (
                source, node.lineno
            )
            instantiate_calls.append((source.name, call_owners[id(node)]))
    assert sorted(instantiate_calls) == [
        ("call_expression_lowering.py", "_emit_call"),
        ("call_expression_lowering.py", "_emit_class_init_call"),
        ("exception_lowering.py", "_build_exception_value"),
        ("native_modules.py", "_maybe_emit_native_module_alias_call"),
        ("unary_call_lowering.py", "_maybe_emit_builtin_type_method"),
    ]


def test_classgen_nested_builder_and_method_function_provenance():
    from pcc.frontends.python.codegen.class_gen import ClassLowering

    path = Path(inspect.getsourcefile(ClassLowering))
    tree = ast.parse(path.read_text(encoding="utf-8"))
    owner = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ClassLowering"
    )
    methods = {
        node.name: node for node in owner.body if isinstance(node, ast.FunctionDef)
    }
    body = methods["_emit_method_body"]
    parent_writes = [
        node.value for node in ast.walk(body)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "parent" for target in node.targets)
    ]
    assert len(parent_writes) == 1 and ast.unparse(parent_writes[0]) == "self.parent"
    nested = [node for node in ast.walk(body) if isinstance(node, ast.FunctionDef) and node.name == "bind_method_arg"]
    assert len(nested) == 1
    assert not any(
        isinstance(node, (ast.Assign, ast.AnnAssign, ast.Delete))
        and any(
            isinstance(target, ast.Name) and target.id == "parent"
            for target in (node.targets if isinstance(node, (ast.Assign, ast.Delete)) else (node.target,))
        )
        for node in ast.walk(nested[0])
    )
    assert not any(
        isinstance(node, (ast.Assign, ast.AnnAssign))
        and any(
            isinstance(target, ast.Name) and target.id == "fn"
            for target in (node.targets if isinstance(node, ast.Assign) else (node.target,))
        )
        for node in ast.walk(body)
    )
    calls = [
        node for node in ast.walk(owner)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_emit_method_body"
    ]
    assert len(calls) == 1 and isinstance(calls[0].args[2], ast.Name)
    assert calls[0].args[2].id == "fn"
    info = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ClassInfo")
    declared = {
        node.target.attr: ast.unparse(node.annotation)
        for node in ast.walk(info)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Attribute)
        and isinstance(node.target.value, ast.Name)
        and node.target.value.id == "self"
    }
    assert all(declared[name] == "dict[str, ir.Function]" for name in ("methods", "property_setters", "property_deleters"))


def test_classgen_proven_ir_receivers_keep_direct_calls():
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.type_infer import infer_module
    from pcc.frontends.python.codegen.class_gen import ClassLowering

    source = Path(inspect.getsourcefile(ClassLowering))
    module = infer_module(parse_and_lift(source.read_text(encoding="utf-8"), str(source), "pcc.frontends.python.codegen.class_gen"))
    text = str(L1CodeGen(module, emit_cpy_main_exitcode=False, ir_scaffold_mode="off").generate(module))

    def body(symbol):
        match = re.search(r"(?ms)^define [^\n]*@" + re.escape(symbol) + r"\([^\n]*\) \{\n(.*?)^\}", text)
        assert match, symbol
        return match.group(1)

    method = body("user_pcc_frontends_python_codegen_class_gen_ClassLowering__emit_method_body")
    nested = body("user_pcc_frontends_python_codegen_class_gen___nested_bind_method_arg")
    if "@user_pcc_ir_ir_scaffold_Function_append_basic_block" not in method:
        raise AssertionError("proven ir.Function receiver lost direct append_basic_block")
    if "%cpy.fn.append_basic_block" in method:
        raise AssertionError("proven ir.Function receiver entered CPython dispatch")
    for symbol in (
        "@user_pcc_ir_ir_IRBuilder_call1",
        "@user_pcc_ir_ir_IRBuilder_alloca",
        "@user_pcc_ir_ir_IRBuilder_store",
    ):
        if symbol not in nested:
            raise AssertionError("proven nested IRBuilder receiver lost " + symbol)
    if "@py_cpy_" in nested:
        raise AssertionError("proven nested IRBuilder receiver entered CPython dispatch")
