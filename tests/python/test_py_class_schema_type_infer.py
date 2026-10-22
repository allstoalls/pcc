from __future__ import annotations

import textwrap

import pytest

from pcc.frontends.python.py_lift import parse_and_lift
from pcc.frontends.python import type_infer
from pcc.frontends.python.export_meta import encode_type
from pcc.frontends.python.pipeline import _contextual_host_params_for_module
from pcc.frontends.python.py_ast import (
    Assign,
    Attr,
    ClassDef,
    ClassType,
    DictType,
    DynType,
    For,
    FuncDef,
    ListType,
    Module,
    Name,
    Return,
    StrType,
    TupleType,
)


@pytest.mark.parametrize("owner", ["pcc.frontends.python.py_parse", "other.parser"])
def test_isinstance_underscore_name_uses_the_bound_class(owner):
    ctx = type_infer._InferCtx(Module("pcc.frontends.python.py_lift", (), None))
    parser_type = ClassType("_Call", owner, (), ())
    ast_type = ClassType("Call", "pcc.frontends.python.py_ast", (), ())
    ctx.class_types.update({"_Call": parser_type, "Call": ast_type})
    expr = Name(span=None, ty=DynType("dyn"), ident="_Call")

    assert type_infer._type_from_isinstance_arg(ctx, expr) is parser_type


def test_isinstance_real_underscore_ast_alias_keeps_its_binding():
    ctx = type_infer._InferCtx(Module("pcc.frontends.python.codegen.reader", (), None))
    ast_type = ClassType("Call", "pcc.frontends.python.py_ast", (), ())
    ctx.class_types["_Call"] = ast_type
    expr = Name(span=None, ty=DynType("dyn"), ident="_Call")

    assert type_infer._type_from_isinstance_arg(ctx, expr) is ast_type


def _infer(
    src: str,
    *,
    external_exports=None,
    unique_external_class_preload=None,
):
    mod = parse_and_lift(textwrap.dedent(src).lstrip(), "schema.py", "schema")
    return type_infer.infer_module(
        mod,
        external_exports=external_exports,
        unique_external_class_preload=unique_external_class_preload,
    )


def _func(mod, name: str) -> FuncDef:
    for stmt in mod.body:
        if isinstance(stmt, FuncDef) and stmt.name == name:
            return stmt
    raise AssertionError(f"function {name!r} not found")


def _method(mod, class_name: str, name: str) -> FuncDef:
    for stmt in mod.body:
        if isinstance(stmt, ClassDef) and stmt.name == class_name:
            for body_stmt in stmt.body:
                if isinstance(body_stmt, FuncDef) and body_stmt.name == name:
                    return body_stmt
    raise AssertionError(f"method {class_name}.{name!r} not found")


def _return_type(fn: FuncDef):
    for stmt in fn.body:
        if isinstance(stmt, Return) and stmt.value is not None:
            return stmt.value.ty
    raise AssertionError(f"function {fn.name!r} has no value return")


def test_indexed_unique_class_preload_preserves_global_ambiguity_decision():
    external_exports = {
        "unique.owner": {
            "Solo": {
                "kind": "class",
                "class_name": "Solo",
                "base_names": (),
                "field_names": ("value",),
                "field_types": (("value", ("int", 64, True)),),
            }
        },
        "duplicate.visible": {
            "Shared": {
                "kind": "class",
                "class_name": "Shared",
                "base_names": (),
                "field_names": ("wrong",),
                "field_types": (("wrong", ("str",)),),
            }
        },
    }
    mod = _infer(
        """
        def read_solo(value: Solo) -> int:
            return value.value

        def read_shared(value: Shared):
            return value.wrong
        """,
        external_exports=external_exports,
        unique_external_class_preload=(
            type_infer.build_unique_external_class_preload(
                {"unique.owner": external_exports["unique.owner"]}
            )
        ),
    )

    solo = _func(mod, "read_solo")
    assert isinstance(solo.args[0].annotation, ClassType)
    assert solo.args[0].annotation.fields[0][0] == "value"
    shared = _func(mod, "read_shared")
    assert isinstance(shared.args[0].annotation, ClassType)
    assert shared.args[0].annotation.fields == ()


def test_contextual_l1_codegen_host_param_types_host_methods():
    mod = parse_and_lift(
        textwrap.dedent(
            """
            def helper(host):
                name = host._fresh("probe")
                return name
            """
        ).lstrip(),
        "host_helper.py",
        "host_helper",
    )
    typed = type_infer.infer_module(
        mod,
        contextual_host_params={"helper": ("host",)},
    )
    fn = _func(typed, "helper")
    assert isinstance(fn.args[0].annotation, ClassType)
    assign = fn.body[0]
    assert isinstance(assign, Assign)
    assert isinstance(assign.value.ty, StrType)
    ret = fn.body[1]
    assert isinstance(ret, Return)
    assert ret.value is not None
    assert isinstance(ret.value.ty, StrType)


def test_contextual_l1_codegen_host_param_auto_detects_codegen_helpers():
    mod = parse_and_lift(
        textwrap.dedent(
            """
            def helper(host):
                return host._fresh("probe")
            """
        ).lstrip(),
        "host_helper.py",
        "pcc.frontends.python.codegen.host_helper",
    )
    assert _contextual_host_params_for_module(
        mod,
        "pcc.frontends.python.codegen.host_helper",
    ) == {"helper": ("host",)}
    assert _contextual_host_params_for_module(mod, "user.host_helper") is None


def test_local_class_schema_resolves_inherited_fields_and_upcasts():
    mod = _infer(
        """
        class Base:
            base: str

        class Child(Base):
            own: int

        def get_base(x: Child) -> str:
            return x.base

        def as_base(x: Child) -> Base:
            return x
        """
    )

    assert isinstance(_func(mod, "get_base").args[0].annotation, ClassType)
    assert isinstance(_return_type(_func(mod, "get_base")), StrType)
    assert isinstance(_func(mod, "as_base").return_ty, ClassType)


def test_init_assignment_adds_instance_field_type():
    mod = _infer(
        """
        class Node:
            def __init__(self, name: str):
                self.name = name

        def get_name(n: Node) -> str:
            return n.name
        """
    )

    assert isinstance(_return_type(_func(mod, "get_name")), StrType)


def test_method_self_uses_enclosing_class_schema():
    mod = _infer(
        """
        class Parent:
            label: str

        class Holder:
            def __init__(self, parent: Parent):
                self.parent = parent

            def get_parent(self) -> Parent:
                return self.parent
        """
    )

    assert isinstance(_return_type(_method(mod, "Holder", "get_parent")), ClassType)


def test_isinstance_and_condition_narrows_positive_branch():
    mod = _infer(
        """
        class Stmt:
            pass

        class FuncDef(Stmt):
            name: str

        def pick(stmt: Stmt) -> FuncDef:
            if isinstance(stmt, FuncDef) and stmt.name == "target":
                return stmt
            return FuncDef()
        """
    )

    assert isinstance(_return_type(_func(mod, "pick")), ClassType)


def test_imported_class_schema_uses_exported_bases_and_fields():
    stmt_ty = ClassType(name="Stmt", module="", fields=(), bases=())
    external_exports = {
        "ast_pkg": {
            "Stmt": {
                "kind": "class",
                "class_name": "Stmt",
                "base_names": (),
                "field_types": (("kind", encode_type(StrType(name="str"))),),
            },
            "FuncDef": {
                "kind": "class",
                "class_name": "FuncDef",
                "base_names": ("Stmt",),
                "field_types": (
                    (
                        "body",
                        encode_type(
                            TupleType(name="tuple_variadic", elems=(stmt_ty,))
                        ),
                    ),
                ),
            },
        },
    }
    mod = _infer(
        """
        from ast_pkg import FuncDef, Stmt

        def get_kind(fd: FuncDef) -> str:
            return fd.kind

        def as_stmt(fd: FuncDef) -> Stmt:
            return fd

        def get_body(fd: FuncDef) -> tuple[Stmt, ...]:
            return fd.body
        """,
        external_exports=external_exports,
    )

    assert isinstance(_return_type(_func(mod, "get_kind")), StrType)
    assert isinstance(_func(mod, "as_stmt").return_ty, ClassType)
    assert isinstance(_return_type(_func(mod, "get_body")), TupleType)


def test_imported_class_schema_preserves_untyped_slot_order():
    external_exports = {
        "ir_mod": {
            "Function": {
                "kind": "class",
                "class_name": "Function",
                "base_names": (),
                "field_names": (
                    "type",
                    "_ref",
                    "_instr",
                    "_flags",
                    "_is_unsigned",
                    "_pcc_unsigned_pointee",
                    "_pcc_unsigned_return",
                    "module",
                    "ftype",
                    "function_type",
                ),
                "field_types": (
                    ("type", encode_type(StrType(name="str"))),
                    ("function_type", encode_type(StrType(name="str"))),
                ),
            },
        },
    }
    mod = _infer(
        """
        from ir_mod import Function

        def get_function_type(fn: Function) -> str:
            return fn.function_type
        """,
        external_exports=external_exports,
    )

    fn_arg_ty = _func(mod, "get_function_type").args[0].annotation
    assert isinstance(fn_arg_ty, ClassType)
    assert tuple(name for name, _ty in fn_arg_ty.fields) == (
        "type",
        "_ref",
        "_instr",
        "_flags",
        "_is_unsigned",
        "_pcc_unsigned_pointee",
        "_pcc_unsigned_return",
        "module",
        "ftype",
        "function_type",
    )
    assert isinstance(fn_arg_ty.fields[5][1], DynType)
    assert isinstance(_return_type(_func(mod, "get_function_type")), StrType)


def test_imported_tuple_string_annotation_resolves_loop_element_schema():
    from pcc.frontends.python.pipeline import _normalise_export_annotation_text

    args_ty = _normalise_export_annotation_text(
        "tuple[pcc.frontends.python.py_ast.Arg, ...]"
    )
    assert isinstance(args_ty, TupleType)
    external_exports = {
        "pcc.frontends.python.py_ast": {
            "FuncDef": {
                "kind": "class",
                "class_name": "FuncDef",
                "base_names": (),
                "field_types": (
                    ("args", encode_type(args_ty)),
                    (
                        "return_ty",
                        encode_type(
                            ClassType(
                                name="Type",
                                module="pcc.frontends.python.py_ast",
                                fields=(),
                                bases=(),
                            )
                        ),
                    ),
                ),
            },
            "Arg": {
                "kind": "class",
                "class_name": "Arg",
                "base_names": (),
                "field_types": (
                    (
                        "annotation",
                        encode_type(
                            ClassType(
                                name="Type",
                                module="pcc.frontends.python.py_ast",
                                fields=(),
                                bases=(),
                            )
                        ),
                    ),
                ),
            },
            "Type": {
                "kind": "class",
                "class_name": "Type",
                "base_names": (),
                "field_types": (("name", encode_type(StrType(name="str"))),),
            },
        },
    }
    mod = _infer(
        """
        from pcc.frontends.python.py_ast import FuncDef, Type

        def first_arg_annotation(fn: FuncDef) -> Type:
            for a in fn.args:
                return a.annotation
            return fn.return_ty
        """,
        external_exports=external_exports,
    )

    fn = _func(mod, "first_arg_annotation")
    loop = fn.body[0]
    assert isinstance(loop, For)
    assert isinstance(loop.body[0], Return)
    value = loop.body[0].value
    assert isinstance(value, Attr)
    assert isinstance(value.ty, ClassType)
    assert value.ty.name == "Type"


def test_imported_py_ast_augassign_static_schema_resolves_target():
    external_exports = {
        "pcc.frontends.python.py_ast": {
            "AugAssign": {
                "kind": "class",
                "class_name": "AugAssign",
                "base_names": ("Stmt",),
                "field_names": ("span", "target", "op", "value"),
                "field_types": (),
            },
            "Stmt": {
                "kind": "class",
                "class_name": "Stmt",
                "base_names": (),
                "field_names": ("span",),
                "field_types": (),
            },
            "Expr": {
                "kind": "class",
                "class_name": "Expr",
                "base_names": (),
                "field_names": ("span", "ty"),
                "field_types": (),
            },
        },
    }
    mod = _infer(
        """
        from pcc.frontends.python.py_ast import AugAssign, Expr

        def aug_target(stmt: AugAssign) -> Expr:
            return stmt.target
        """,
        external_exports=external_exports,
    )

    ret_ty = _return_type(_func(mod, "aug_target"))
    assert isinstance(ret_ty, ClassType)
    assert ret_ty.name == "Expr"


def test_imported_py_ast_compare_static_schema_resolves_rhs_type():
    external_exports = {
        "pcc.frontends.python.py_ast": {
            "Compare": {
                "kind": "class",
                "class_name": "Compare",
                "base_names": ("Expr",),
                "field_names": ("span", "ty", "op", "lhs", "rhs"),
                "field_types": (),
            },
            "Expr": {
                "kind": "class",
                "class_name": "Expr",
                "base_names": (),
                "field_names": ("span", "ty"),
                "field_types": (),
            },
            "Type": {
                "kind": "class",
                "class_name": "Type",
                "base_names": (),
                "field_names": ("name",),
                "field_types": (),
            },
        },
    }
    mod = _infer(
        """
        from pcc.frontends.python.py_ast import Compare, Expr, Type

        def compare_rhs_type(expr: Compare) -> Type:
            rhs = expr.rhs
            return rhs.ty
        """,
        external_exports=external_exports,
    )

    fn = _func(mod, "compare_rhs_type")
    assign = fn.body[0]
    assert isinstance(assign, Assign)
    assert isinstance(assign.value.ty, ClassType)
    assert assign.value.ty.name == "Expr"
    ret_ty = _return_type(fn)
    assert isinstance(ret_ty, ClassType)
    assert ret_ty.name == "Type"


def test_imported_py_ast_static_base_resolves_positive_isinstance():
    external_exports = {
        "pcc.frontends.python.py_ast": {
            "Module": {
                "kind": "class",
                "class_name": "Module",
                "base_names": (),
                "field_names": ("name", "body", "docstring"),
                "field_types": (),
            },
            "Stmt": {
                "kind": "class",
                "class_name": "Stmt",
                "base_names": (),
                "field_names": ("span",),
                "field_types": (),
            },
            "Import": {
                "kind": "class",
                "class_name": "Import",
                "base_names": (),
                "field_names": ("span", "names"),
                "field_types": (),
            },
        },
    }
    mod = _infer(
        """
        from pcc.frontends.python.py_ast import Import, Module

        def first_import_name(module: Module) -> str:
            for stmt in module.body:
                if isinstance(stmt, Import):
                    for name, _asname in stmt.names:
                        return name
            return ""
        """,
        external_exports=external_exports,
    )

    fn = _func(mod, "first_import_name")
    outer_loop = fn.body[0]
    assert isinstance(outer_loop, For)
    cond_body = outer_loop.body[0].body
    inner_loop = cond_body[0]
    assert isinstance(inner_loop, For)
    assert isinstance(inner_loop.iter, Attr)
    assert isinstance(inner_loop.iter.ty, TupleType)
    assert isinstance(inner_loop.target.ty, TupleType)


def test_ir_compat_alias_annotation_resolves_exported_schema_chain():
    external_exports = {
        "pcc.ir.ir": {
            "IRBuilder": {
                "kind": "class",
                "class_name": "IRBuilder",
                "base_names": (),
                "field_names": ("_block", "_pos", "_fn"),
                "field_types": (),
            },
            "Block": {
                "kind": "class",
                "class_name": "Block",
                "base_names": (),
                "field_names": ("function", "_instrs"),
                "field_types": (),
            },
            "Function": {
                "kind": "class",
                "class_name": "Function",
                "base_names": (),
                "field_names": ("blocks",),
                "field_types": (),
            },
            "InstructionRecord": {
                "kind": "class",
                "class_name": "InstructionRecord",
                "base_names": (),
                "field_names": ("opname",),
                "field_types": (),
            },
        },
    }
    mod = _infer(
        """
        from pcc.ir.compat import ir

        def first_opname(builder: ir.IRBuilder) -> str:
            block = builder._block
            fn = block.function
            entry = fn.blocks[0]
            for instr in entry._instrs:
                return instr.opname
            return ""
        """,
        external_exports=external_exports,
    )

    fn = _func(mod, "first_opname")
    assert isinstance(fn.args[0].annotation, ClassType)
    assert fn.args[0].annotation.name == "IRBuilder"
    assert isinstance(fn.body[0], Assign)
    assert isinstance(fn.body[0].value.ty, ClassType)
    assert fn.body[0].value.ty.name == "Block"
    assert isinstance(fn.body[1], Assign)
    assert isinstance(fn.body[1].value.ty, ClassType)
    assert fn.body[1].value.ty.name == "Function"
    assert isinstance(fn.body[2], Assign)
    assert isinstance(fn.body[2].value.ty, ClassType)
    assert fn.body[2].value.ty.name == "Block"
    loop = fn.body[3]
    assert isinstance(loop, For)
    assert isinstance(loop.target.ty, ClassType)
    assert loop.target.ty.name == "InstructionRecord"
    assert isinstance(loop.body[0], Return)
    assert loop.body[0].value is not None
    assert isinstance(loop.body[0].value.ty, StrType)


def test_bare_builtin_container_annotations_are_not_user_classes():
    from pcc.frontends.python.pipeline import _normalise_export_annotation

    assert isinstance(
        _normalise_export_annotation(
            ClassType(name="dict", module="", fields=(), bases=())
        ),
        DictType,
    )

    mod = _infer(
        """
        def make_list() -> list:
            xs: list = []
            return xs

        def make_dict() -> dict:
            d: dict = {}
            return d
        """
    )

    assert isinstance(_func(mod, "make_list").return_ty, ListType)
    assert isinstance(_return_type(_func(mod, "make_list")), ListType)
    assert isinstance(_func(mod, "make_dict").return_ty, DictType)
    assert isinstance(_return_type(_func(mod, "make_dict")), DictType)
