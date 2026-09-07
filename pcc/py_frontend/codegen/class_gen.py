"""Phase 3 class + method + super() lowering for pcc_py.

This module lives next to :mod:`layer1` and :mod:`layer2` and is invoked
by the top-level :class:`L1CodeGen` when it encounters a
:class:`~pcc.py_frontend.py_ast.ClassDef` at module scope.

Codegen strategy (Phase 3 scope — see python-frontend-plan.md §3):

* Each ClassDef is lowered to:
    1. A **global variable** that holds a ``PyClassObject*``. Name:
       ``.class.<module>.<name>``. Initialized to NULL; populated by the
       module-init function.
    2. A set of LLVM **method functions** — one per ``def`` in the class
       body. Signature: ``PyObject* user_<module>_<Class>_<method>(
         PyObject* self, <unboxed args...>)``.
    3. Contributions to the **module-init function** that:
       - Collect base class pointers from their own globals
       - Collect field names (a global const-char-pointer array)
       - Call ``py_class_new`` with the above plus ``name``
       - Call ``py_class_add_method`` for each method
       - Store the result in the class global.

* ``self.field`` reads:
    - If the field name is declared on the class, we emit
      ``py_instance_get_field(self, <idx>)``.
    - Otherwise fall back to ``py_obj_getattr``.

* ``self.field = value`` stores similarly via
  ``py_instance_set_field`` or ``py_obj_setattr``.

* ``super().method(args)`` lowers to
  ``py_super_lookup(cls_global, <enclosing_class>, "method")`` —
  currently we materialise the method PyObject* and then dispatch via
  a generic call; for Phase 3 single-dispatch single-class hierarchies
  this path is exercised but not fully tested end-to-end. Calling a
  super-resolved method is done through ``py_obj_call`` after wrapping
  self+args into a tuple.

* ``isinstance(x, Cls)`` uses ``py_isinstance`` on the class global.

* ``MyClass(args)`` is lowered by the caller in :mod:`layer1` via the
  Call-handler, which delegates here for construction.

The module exports a single entry point :class:`ClassLowering` that the
L1 codegen instantiates and calls into. Construction is stateful
because it needs access to the enclosing :class:`L1CodeGen`'s builder
and module.
"""

from __future__ import annotations

import os
import sys
from typing import Optional, cast

from pcc.llvm_capi.compat import ir
from pcc.llvm_capi.ir import (
    IRBuilder_current_instruction_count,
    IRBuilder_emit_raw,
    IRBuilder_instruction_text_at,
    IRBuilder_next_value,
    IRBuilder_publish_direct_raw_call,
)

from ..export_meta import decode_type
from ..py_ast import (
    Arg,
    Assign,
    Attr,
    AugAssign,
    BinOp,
    BoolExpr,
    BoolLit,
    BoolType,
    ByteArrayType,
    BytesType,
    Call,
    ClassDef,
    ClassType,
    ComplexType,
    Compare,
    Delete,
    DictType,
    DictExpr,
    DynType,
    Expr,
    ExprStmt,
    FloatLit,
    FloatType,
    For,
    FuncDef,
    FuncType,
    If,
    IfExpr,
    IntLit,
    IntType,
    ListExpr,
    ListType,
    MemoryViewType,
    Module as AstModule,
    Name,
    NoneLit,
    NoneType,
    SetType,
    Pass,
    Return,
    SourceSpan,
    StrLit,
    StrType,
    Subscript,
    Try,
    TupleExpr,
    TupleType,
    Type,
    While,
    With,
)
from ..py_ast_contract import PY_AST_FIELD_NAME_OVERRIDES
from . import marshal
from .exact_int_lowering import (
    allocate_forced_exact_int_locals,
    bind_forced_exact_int_parameter,
    forced_exact_int_local_names,
)
from .builtin_exceptions import builtin_exc_tag_or_missing
from .errors import L1CodegenError
from .host_contract import L1_CODEGEN_HOST_ATTRS
from .runtime_abi import declare_runtime_global
from .self_module_contracts import (
    L1_CODEGEN_HOST_ATTR_CONTRACT,
    PY_AST_FIELD_ORDER_CONTRACT,
    module_for_class_symbol_contract,
    module_has_contract,
)

_I1 = ir.IntType(1)
_I8 = ir.IntType(8)
_I32 = ir.IntType(32)
_I64 = ir.IntType(64)
_DOUBLE = ir.DoubleType()
_VOID = ir.VoidType()
_CSTR = _I8.as_pointer()  # i8*
_PTR = _I8.as_pointer()  # also i8* (opaque)
_METACLASS_CONFLICT = "__pcc_metaclass_conflict__"
_NO_METACLASS_RETURN = "__pcc_no_metaclass_return__"
_AMBIGUOUS_METACLASS_RETURN = "__pcc_ambiguous_metaclass_return__"
_NATIVE_DEFAULT_FUNC_SENTINEL = "__pcc_native_default_func_ref__"
_NATIVE_DEFAULT_GLOBAL_SENTINEL = "__pcc_native_default_global_ref__"


def _classgen_extern_default_expr(arg: dict, span: SourceSpan):
    ref = arg.get("default_native_func")
    if isinstance(ref, dict):
        owning_module = ref.get("owning_module")
        name = ref.get("name")
        if owning_module and name:
            return Call(
                span=span,
                ty=DynType(name="dyn"),
                func=Name(span, DynType(name="dyn"), _NATIVE_DEFAULT_FUNC_SENTINEL),
                args=(
                    StrLit(span, StrType(name="str"), str(owning_module)),
                    StrLit(span, StrType(name="str"), str(name)),
                ),
                kwargs=(),
            )
    factory = arg.get("default_factory")
    if isinstance(factory, str) and factory:
        # ``field(default_factory=list)`` and friends.  Rebuilding the call
        # here is what lets the importing module omit the field: the formal
        # keeps a default even though the original Call node did not survive
        # the export dictionary.
        return Call(
            span=span,
            ty=DynType(name="dyn"),
            func=Name(span, DynType(name="dyn"), factory),
            args=(),
            kwargs=(),
        )
    gref = arg.get("default_native_global")
    if isinstance(gref, dict):
        owning_module = gref.get("owning_module")
        name = gref.get("name")
        if owning_module and name:
            default_expr = Call(
                span=span,
                ty=DynType(name="dyn"),
                func=Name(span, DynType(name="dyn"), _NATIVE_DEFAULT_GLOBAL_SENTINEL),
                args=(
                    StrLit(span, StrType(name="str"), str(owning_module)),
                    StrLit(span, StrType(name="str"), str(name)),
                ),
                kwargs=(),
            )
            for attr_name in gref.get("attrs", ()):
                default_expr = Attr(
                    span=span,
                    ty=DynType(name="dyn"),
                    obj=default_expr,
                    name=str(attr_name),
                )
            return default_expr
    return arg.get("default")


def _classgen_extern_field_names(
    owning_module: str,
    class_name: str,
    field_names: tuple,
) -> tuple:
    if module_has_contract(owning_module, PY_AST_FIELD_ORDER_CONTRACT):
        override_names = PY_AST_FIELD_NAME_OVERRIDES.get(str(class_name))
        if override_names is not None and tuple(field_names) != tuple(override_names):
            return tuple(override_names)
    return tuple(field_names)


def _classgen_class_name_from_info(info) -> str:
    class_name = getattr(info, "export_class_name", None)
    if class_name is not None:
        return str(class_name)
    text = str(getattr(info, "name", ""))
    idx = -1
    i = len(text) - 1
    while i >= 0:
        if text[i] == ".":
            idx = i
            break
        i -= 1
    if idx >= 0:
        return text[idx + 1 :]
    return text


def _classgen_effective_field_names(info) -> tuple:
    fields = tuple(getattr(info, "field_names", ()) or ())
    owning_module = getattr(info, "owning_module", None)
    if not module_has_contract(owning_module, PY_AST_FIELD_ORDER_CONTRACT):
        try:
            global_name = str(info.global_var.name)
        except Exception:
            global_name = ""
        encoded_owner = module_for_class_symbol_contract(
            global_name,
            PY_AST_FIELD_ORDER_CONTRACT,
        )
        if encoded_owner is not None:
            owning_module = encoded_owner
    if module_has_contract(owning_module, PY_AST_FIELD_ORDER_CONTRACT):
        class_name = _classgen_class_name_from_info(info)
        override_names = PY_AST_FIELD_NAME_OVERRIDES.get(str(class_name))
        if override_names is not None:
            return tuple(override_names)
    return fields


_EXTERN_CLASS_DECL_PLAN_CACHE = {}


def _extern_class_decl_plan(
    owning_module: str,
    class_name: str,
    field_names: tuple,
    methods: tuple,
):
    cache_key = (
        owning_module,
        class_name,
        id(field_names),
        len(field_names),
        id(methods),
        len(methods),
    )
    cached = _EXTERN_CLASS_DECL_PLAN_CACHE.get(cache_key)
    if cached is not None:
        return cached

    from pcc.py_frontend.py_ast import (
        FuncDef as _FuncDef,
        Arg as _Arg,
        SourceSpan as _Span,
    )

    span = _Span(
        file="<extern>",
        line=0,
        col=0,
        end_line=0,
        end_col=0,
    )
    synth_defs = {}
    method_plans = []
    for mdesc in methods:
        call_sig = mdesc.get("call_sig")
        synth_args = []
        if call_sig is not None:
            for arg in call_sig:
                synth_args.append(
                    _Arg(
                        name=arg["name"],
                        annotation=decode_type(arg.get("annotation")),
                        default=_classgen_extern_default_expr(arg, span),
                        kind=arg.get("kind", "pos"),
                        has_default=bool(
                            arg.get("default_factory")
                            or arg.get(
                                "has_default",
                                arg.get("default") is not None,
                            )
                        ),
                    )
                )
        else:
            for i, ty in enumerate(mdesc["param_types"]):
                synth_args.append(
                    _Arg(
                        name=f"arg{i}" if i > 0 else "self",
                        annotation=decode_type(ty),
                        default=None,
                        kind="pos",
                        has_default=False,
                    )
                )
        mname = mdesc["name"]
        return_ty = decode_type(mdesc["return_ty"])
        synth_defs[mname] = _FuncDef(
            span=span,
            name=mname,
            args=tuple(synth_args),
            return_ty=return_ty,
            body=(),
            decorators=(),
            is_async=bool(mdesc.get("is_async", False)),
        )
        decoded_param_types = []
        for raw_ty in mdesc["param_types"]:
            decoded_param_types.append(decode_type(raw_ty))
        # A plain bool survives the schema round trip even where type
        # descriptors degrade to dyn under the self-hosted compiler; fall
        # back to the decoded node for schemas written before the field.
        if "returns_none" in mdesc:
            returns_none = bool(mdesc["returns_none"])
        else:
            returns_none = (
                _classgen_type_name(return_ty) in ("None", "NoneType")
                or _is_ast_node(return_ty, NoneType)
            )
        method_plans.append(
            (
                mname,
                mdesc["kind"],
                mdesc.get("box_int_abi", None),
                tuple(decoded_param_types),
                return_ty,
                mdesc.get("symbol")
                or f"user_{owning_module.replace('.', '_').replace('-', '_')}_{class_name}_{mname}",
                returns_none,
                bool(mdesc.get("may_park", False)),
            )
        )
    cached = (
        _classgen_extern_field_names(
            owning_module,
            class_name,
            field_names,
        ),
        synth_defs,
        tuple(method_plans),
    )
    _EXTERN_CLASS_DECL_PLAN_CACHE[cache_key] = cached
    return cached


def _classgen_log(parent, label: str) -> None:
    if not os.environ.get("PCC_DEBUG_CODEGEN_PHASES"):
        return
    mod_name = parent.ast_module.name or "<module>"
    sys.stderr.write("[pcc.classgen] " + mod_name + ":" + label + "\n")


def _classgen_function_arg_type(fn, index: int, arg) -> Optional[ir.Type]:
    try:
        return arg.type
    except AttributeError:
        pass
    out = None
    try:
        args = fn.function_type.args
        if index < len(args):
            out = args[index]
    except AttributeError:
        pass
    if out is None:
        try:
            args = fn.ftype.args
            if index < len(args):
                out = args[index]
        except AttributeError:
            pass
    if out is not None:
        try:
            arg.type = out
        except Exception:
            pass
    return out


def _classgen_ensure_value_type(value, ty: ir.Type) -> None:
    try:
        _ = value.type
        return
    except AttributeError:
        pass
    try:
        value.type = ty
    except Exception:
        pass


def _classgen_valueclass_payload_ir_type(ty: Type) -> Optional[ir.Type]:
    """Class-gen local mirror of the valueclass payload ABI.

    Calling back through ``parent._valueclass_payload_ir_type`` from this
    module regresses the raw per-module self-compile probe into CPython
    fallback. Keep this tiny mirror local until the L1 host protocol can
    express this helper as a fully typed native host method.
    """
    if not _is_ast_node(ty, ClassType):
        return None
    if not ty.valueclass:
        return None
    if len(ty.fields) == 0:
        return None
    field_ir_types: list[ir.Type] = []
    for _field_name, field_ty in ty.fields:
        field_ir_ty = _classgen_valueclass_field_payload_ir_type(field_ty)
        if field_ir_ty is None:
            return None
        field_ir_types.append(field_ir_ty)
    # Keep this self-host mirror on the same dynamic scaffold constructor as
    # TypeAbiLoweringMixin.  Unsupported field *types* still return None; an
    # otherwise valid valueclass never changes ABI merely at field eight.
    return ir.LiteralStructType(field_ir_types)


def _classgen_valueclass_field_payload_ir_type(field_ty: Type) -> Optional[ir.Type]:
    if _is_ast_node(field_ty, IntType):
        return _I64
    if _is_ast_node(field_ty, FloatType):
        return _DOUBLE
    if _is_ast_node(field_ty, BoolType):
        return _I1
    if _is_ast_node(field_ty, ClassType) and getattr(field_ty, "valueclass", False):
        return _classgen_valueclass_payload_ir_type(field_ty)
    if _is_ast_node(
        field_ty,
        (
            StrType,
            BytesType,
            ByteArrayType,
            MemoryViewType,
            ListType,
            SetType,
            DictType,
            TupleType,
            ClassType,
            NoneType,
            DynType,
            FuncType,
            ComplexType,
        ),
    ):
        return _PTR
    name = _classgen_type_name(field_ty)
    if name in (
        "str",
        "bytes",
        "bytearray",
        "memoryview",
        "list",
        "dict",
        "tuple",
        "None",
        "dyn",
        "Type",
        "complex",
    ):
        return _PTR
    return None


def _classgen_ir_type_is_pointer(ty) -> bool:
    if ty is None:
        return False
    if isinstance(ty, ir.PointerType):
        return True
    # During self-host, LLVM type objects can cross module boundaries and
    # miss the local isinstance() check. Avoid str(ty) here: formatting a
    # foreign type object can raise while compiling stage code.
    try:
        ty.pointee
        return True
    except AttributeError:
        return False


def _classgen_ir_type_is_void(ty) -> bool:
    if ty is None:
        return False
    if isinstance(ty, ir.VoidType):
        return True
    return str(ty) == "void"


def _classgen_ir_types_match(lhs, rhs) -> bool:
    if lhs is None or rhs is None:
        return False
    if lhs is rhs:
        return True
    if _classgen_ir_type_is_pointer(lhs) and _classgen_ir_type_is_pointer(rhs):
        return True
    try:
        lhs_width = lhs.width
        rhs_width = rhs.width
        return lhs_width == rhs_width
    except AttributeError:
        return False


def _classgen_type_name(ty: Type) -> str:
    if ty is None:
        return ""
    name = ty.name
    if name is None:
        return ""
    return name


def _classgen_str_eq(lhs: str, rhs: str) -> bool:
    lhs_text = str(lhs)
    rhs_text = str(rhs)
    if len(lhs_text) != len(rhs_text):
        return False
    i = 0
    while i < len(lhs_text):
        if lhs_text[i] != rhs_text[i]:
            return False
        i += 1
    return True


def _classgen_name_eq(name, expected: str) -> bool:
    if name is None:
        return False
    return _classgen_str_eq(str(name), expected)


def _classgen_name_endswith(name, suffix: str) -> bool:
    if name is None:
        return False
    return str(name).endswith(suffix)


def _classgen_lookup_class_info(classgen, name: str):
    if name is None:
        return None
    try:
        info = classgen.classes.get(name)
    except Exception:
        info = None
    if info is not None:
        return info
    try:
        for key in classgen.classes:
            if key == name or _classgen_str_eq(key, name):
                return classgen.classes[key]
            info = classgen.classes[key]
            info_name = getattr(info, "name", None)
            if info_name == name or (
                info_name is not None and _classgen_str_eq(info_name, name)
            ):
                return info
    except Exception:
        return None
    return None


def _classgen_dict_get_str(mapping, name: str):
    if mapping is None or name is None:
        return None
    try:
        value = mapping.get(name)
    except Exception:
        value = None
    if value is not None:
        return value
    try:
        for key in mapping:
            if key == name or _classgen_str_eq(key, name):
                return mapping[key]
    except Exception:
        return None
    return None


def _classgen_direct_field_index(info, name: str):
    if info is None or name is None:
        return None
    fields = _classgen_effective_field_names(info)
    i = 0
    while i < len(fields):
        if _classgen_str_eq(fields[i], name):
            return i
        i += 1
    return None


def _classgen_value_type_or_none(value):
    try:
        return value.type
    except AttributeError:
        return None


def _classgen_arg_type_or_none(args, index: int):
    try:
        arg = args[index]
        return arg.type
    except AttributeError:
        return None
    except IndexError:
        return None


def _classgen_annotation_is_object_param(parent, annotation: Optional[Type]) -> bool:
    if annotation is None:
        return True
    name = _classgen_type_name(annotation)
    if name in (
        "str",
        "bytes",
        "bytearray",
        "memoryview",
        "list",
        "dict",
        "tuple",
        "None",
        "complex",
        "dyn",
        "Type",
        "FuncType",
    ):
        return True
    if _is_ast_node(annotation, (StrType, TupleType, NoneType, DynType, ClassType)):
        return True
    if _is_ast_node(annotation, IntType) or name == "int":
        return parent._should_box_python_ints()
    return False


def _classgen_emit_int_literal_fallback(parent, expr):
    try:
        value = expr.value
    except AttributeError:
        return None
    if parent._int_exprs_are_boxed():
        return parent._emit_int_literal_object(int(value))
    return ir.Constant(_I64, int(value))


def _classgen_emit_bool_literal_fallback(parent, expr):
    try:
        value = expr.value
    except AttributeError:
        return None
    return ir.Constant(_I1, 1 if bool(value) else 0)


def _classgen_utf8_byte_values(payload: str) -> list[int]:
    out: list[int] = []
    i = 0
    n = len(payload)
    while i < n:
        cp = ord(payload[i])
        if cp <= 127:
            out.append(cp)
        elif cp <= 2047:
            out.append(192 | (cp >> 6))
            out.append(128 | (cp & 63))
        elif cp <= 65535:
            out.append(224 | (cp >> 12))
            out.append(128 | ((cp >> 6) & 63))
            out.append(128 | (cp & 63))
        else:
            out.append(240 | (cp >> 18))
            out.append(128 | ((cp >> 12) & 63))
            out.append(128 | ((cp >> 6) & 63))
            out.append(128 | (cp & 63))
        i += 1
    return out


def _classgen_emit_str_literal_object(parent, value: str):
    existing = parent._str_obj_pool.get(value)
    if existing is None:
        data = _classgen_utf8_byte_values(value)
        body = data + [0]
        data_ty = ir.ArrayType(_I8, len(body))
        obj_ty = ir.LiteralStructType(
            [
                _I64,
                _I32,
                _I32,
                _I64,
                _I64,
                _I64,
                data_ty,
            ]
        )
        parent._str_counter = parent._str_counter + 1
        gv = ir.GlobalVariable(
            parent.module,
            obj_ty,
            name=".pystr.obj." + str(parent._str_counter),
        )
        gv.linkage = "internal"
        gv.global_constant = False
        gv.initializer = ir.Constant(
            obj_ty,
            [
                ir.Constant(_I64, 1),
                ir.Constant(_I32, 4),
                ir.Constant(_I32, 1),
                ir.Constant(_I64, len(data)),
                ir.Constant(_I64, -1),
                ir.Constant(_I64, -1),
                ir.Constant(data_ty, body),
            ],
        )
        parent._str_obj_pool[value] = gv
        existing = gv
    return existing


def _classgen_emit_str_literal_fallback(parent, expr):
    try:
        value = expr.value
    except AttributeError:
        return None
    return _classgen_emit_str_literal_object(parent, value)


def _classgen_emit_none_literal_fallback(parent):
    gv = declare_runtime_global(parent.module, "py_None")
    return parent.builder.load(gv, name="classgen.none")


def _classgen_literal_fallback(parent, expr, annotation: Optional[Type] = None):
    if annotation is not None:
        ann_name = _classgen_type_name(annotation)
        if ann_name == "str":
            return _classgen_emit_str_literal_fallback(parent, expr)
        if _is_ast_node(annotation, StrType):
            return _classgen_emit_str_literal_fallback(parent, expr)
        if ann_name == "int":
            return _classgen_emit_int_literal_fallback(parent, expr)
        if _is_ast_node(annotation, IntType):
            return _classgen_emit_int_literal_fallback(parent, expr)
        if ann_name == "bool":
            return _classgen_emit_bool_literal_fallback(parent, expr)
        if _is_ast_node(annotation, BoolType):
            return _classgen_emit_bool_literal_fallback(parent, expr)
        if ann_name == "None":
            return _classgen_emit_none_literal_fallback(parent)
        if _is_ast_node(annotation, NoneType):
            return _classgen_emit_none_literal_fallback(parent)
    if _is_ast_node(expr, IntLit):
        if parent._int_exprs_are_boxed():
            return parent._emit_int_literal_object(int(expr.value))
        return ir.Constant(_I64, int(expr.value))
    if _is_ast_node(expr, BoolLit):
        return ir.Constant(_I1, 1 if bool(expr.value) else 0)
    if _is_ast_node(expr, StrLit):
        return _classgen_emit_str_literal_object(parent, expr.value)
    if _is_ast_node(expr, NoneLit):
        return _classgen_emit_none_literal_fallback(parent)
    try:
        ty = expr.ty
    except AttributeError:
        return None
    name = _classgen_type_name(ty)
    if name == "str":
        return _classgen_emit_str_literal_fallback(parent, expr)
    if _is_ast_node(ty, StrType):
        return _classgen_emit_str_literal_fallback(parent, expr)
    if name == "int":
        return _classgen_emit_int_literal_fallback(parent, expr)
    if _is_ast_node(ty, IntType):
        return _classgen_emit_int_literal_fallback(parent, expr)
    if name == "bool":
        return _classgen_emit_bool_literal_fallback(parent, expr)
    if _is_ast_node(ty, BoolType):
        return _classgen_emit_bool_literal_fallback(parent, expr)
    if name == "None":
        return _classgen_emit_none_literal_fallback(parent)
    if _is_ast_node(ty, NoneType):
        return _classgen_emit_none_literal_fallback(parent)
    return None


def _classgen_emit_container_literal_fallback(parent, expr):
    def expr_ty(node):
        try:
            return node.ty
        except AttributeError:
            return DynType(name="dyn")

    def emit_obj(node):
        raw = _classgen_emit_arg_expr(parent, node)
        raw_ty = _classgen_value_type_or_none(raw)
        if _classgen_value_is_null(raw):
            ident = getattr(node, "ident", None)
            if ident is not None:
                recovered = _classgen_current_name_load(
                    parent,
                    ident,
                    raw_ty,
                    expr_ty(node),
                )
                if recovered is not None:
                    return recovered
            return raw
        if _classgen_ir_type_is_pointer(raw_ty):
            return raw
        return marshal.marshal_to_object(
            parent.builder,
            parent.module,
            parent.runtime,
            raw,
            expr_ty(node),
        )

    kind = _node_kind_name(expr)
    if isinstance(expr, ListExpr) or _classgen_str_eq(kind, "ListExpr"):
        n = len(expr.elems)
        out = parent.builder.call(
            parent.runtime["py_list_new"],
            [ir.Constant(_I64, n)],
            name=parent._fresh("classgen.list"),
        )
        parent.builder.call(parent.runtime["pcc_gc_pin"], [out])
        idx = 0
        while idx < n:
            elem = expr.elems[idx]
            elem_obj = emit_obj(elem)
            parent.builder.call(
                parent.runtime["py_list_append"],
                [out, elem_obj],
            )
            idx += 1
        parent.builder.call(parent.runtime["pcc_gc_unpin"], [out])
        return out
    if isinstance(expr, TupleExpr) or _classgen_str_eq(kind, "TupleExpr"):
        n = len(expr.elems)
        out = parent.builder.call(
            parent.runtime["py_tuple_new"],
            [ir.Constant(_I64, n)],
            name=parent._fresh("classgen.tuple"),
        )
        parent.builder.call(parent.runtime["pcc_gc_pin"], [out])
        idx = 0
        while idx < n:
            elem = expr.elems[idx]
            elem_obj = emit_obj(elem)
            parent.builder.call(
                parent.runtime["py_tuple_set_item"],
                [out, ir.Constant(_I64, idx), elem_obj],
            )
            idx += 1
        parent.builder.call(parent.runtime["pcc_gc_unpin"], [out])
        return out
    if isinstance(expr, DictExpr) or _classgen_str_eq(kind, "DictExpr"):
        out = parent.builder.call(
            parent.runtime["py_dict_new"],
            [],
            name=parent._fresh("classgen.dict"),
        )
        parent.builder.call(parent.runtime["pcc_gc_pin"], [out])
        idx = 0
        n = len(expr.pairs)
        while idx < n:
            key_expr, val_expr = expr.pairs[idx]
            key_obj = emit_obj(key_expr)
            val_obj = emit_obj(val_expr)
            parent.builder.call(
                parent.runtime["py_dict_set"],
                [out, key_obj, val_obj],
            )
            idx += 1
        parent.builder.call(parent.runtime["pcc_gc_unpin"], [out])
        return out
    return None


def _classgen_i32_index_constant(idx: int) -> ir.Constant:
    if idx == 0:
        return ir.Constant(_I32, 0)
    if idx == 1:
        return ir.Constant(_I32, 1)
    if idx == 2:
        return ir.Constant(_I32, 2)
    if idx == 3:
        return ir.Constant(_I32, 3)
    if idx == 4:
        return ir.Constant(_I32, 4)
    if idx == 5:
        return ir.Constant(_I32, 5)
    if idx == 6:
        return ir.Constant(_I32, 6)
    if idx == 7:
        return ir.Constant(_I32, 7)
    return ir.Constant(_I32, idx)


def _classgen_expr_has_func(expr) -> bool:
    try:
        func = expr.func
    except AttributeError:
        return _classgen_str_eq(_node_kind_name(expr), "Call")
    return func is not None


def _classgen_emit_dynamic_attr_value(
    parent,
    expr,
    expected_ir_ty=None,
    annotation: Optional[Type] = None,
):
    kind = _node_kind_name(expr)
    if not _classgen_str_eq(kind, "Attr"):
        return None
    obj = expr.obj
    attr_name = expr.name
    if _classgen_str_eq(_node_kind_name(obj), "Name"):
        obj_ident = obj.ident
    else:
        obj_ident = ""
    if getattr(parent, "_native_module_exports", None) is not None:
        native_export = parent._native_module_expr_export_info(obj, attr_name)
        if native_export is not None:
            exact_value = parent._emit_expr(expr)
            return _classgen_maybe_unbox_recovered_arg(
                parent,
                exact_value,
                expected_ir_ty,
                annotation,
            )
    if obj_ident:
        env = getattr(parent, "env", None)
        slot = _classgen_dict_get_str(env, obj_ident)
        storage_ir_ty = None
        if slot is not None:
            try:
                storage_ir_ty = slot[1]
            except (IndexError, TypeError):
                storage_ir_ty = None
        if (
            storage_ir_ty is not None
            and not _classgen_ir_type_is_pointer(storage_ir_ty)
        ):
            try:
                exact_value = parent._emit_expr(expr)
            except Exception:
                exact_value = None
            exact_ir_ty = _classgen_value_type_or_none(exact_value)
            if (
                exact_ir_ty is not None
                and not _classgen_value_is_null(exact_value)
            ):
                return exact_value
    obj_val = None
    if obj_ident:
        obj_val = _classgen_current_name_load(parent, obj_ident, _PTR, None)
    if obj_val is None:
        if obj_ident:
            obj_val = parent._emit_name(obj)
        else:
            obj_val = parent._emit_expr(obj)
    attr_ptr = _classgen_attr_name_ptr(parent, attr_name)
    raw = IRBuilder_next_value(parent.builder, "classgen.arg." + attr_name, _PTR)
    obj_ref_text = _classgen_value_ref_text(obj_val)
    attr_ref_text = _classgen_value_ref_text(attr_ptr)
    record = IRBuilder_emit_raw(
        parent.builder,
        str(raw)
        + " = call ptr (ptr, ptr) @py_obj_getattr(ptr "
        + obj_ref_text
        + ", ptr "
        + attr_ref_text
        + ")",
    )
    IRBuilder_publish_direct_raw_call(
        parent.builder,
        record,
        str(raw),
        _PTR,
        "@py_obj_getattr",
        ("ptr", "ptr"),
        (obj_ref_text, attr_ref_text),
    )
    parent._emit_attribute_error_if_null(
        raw,
        attr_name,
        getattr(expr, "span", None),
    )
    return _classgen_maybe_unbox_recovered_arg(
        parent,
        raw,
        expected_ir_ty,
        annotation,
    )


def _classgen_attr_name_ptr(parent, name: str) -> ir.Value:
    stable_name: str = name
    attr_pool: dict[str, ir.GlobalVariable] = parent._attr_pool
    module: ir.Module = parent.module
    existing = attr_pool.get(stable_name)
    if existing is None:
        data = _classgen_utf8_byte_values(stable_name) + [0]
        arr_ty = ir.ArrayType(_I8, len(data))
        sym = ".pyattr." + stable_name
        if sym in module.globals:
            sym = ".pyattr." + stable_name + "." + str(len(attr_pool))
        gv = ir.GlobalVariable(module, arr_ty, name=sym)
        gv.linkage = "internal"
        gv.global_constant = True
        gv.initializer = ir.Constant(arr_ty, data)
        attr_pool[stable_name] = gv
        existing = gv
        name_text = sym
    else:
        typed_existing: ir.GlobalVariable = existing
        name_text = typed_existing.name
    expr = (
        "getelementptr inbounds ("
        + str(existing.value_type)
        + ", "
        + str(existing.type)
        + " @"
        + name_text
        + ", i32 0, i32 0)"
    )
    return ir.Value(ir.PointerType(_I8), expr)


def _classgen_emit_arg_expr(parent, expr, annotation: Optional[Type] = None):
    kind = _node_kind_name(expr)
    if _classgen_str_eq(kind, "Attr"):
        attr_value = _classgen_emit_dynamic_attr_value(parent, expr, None, annotation)
        if attr_value is not None:
            return attr_value
        classgen = getattr(parent, "class_lowering", None)
        if classgen is not None:
            recovered_attr = _classgen_recover_attr_value(
                classgen,
                parent,
                expr,
                None,
                annotation,
            )
            if recovered_attr is not None:
                return recovered_attr
    if _classgen_str_eq(kind, "ListExpr"):
        container_fallback = _classgen_emit_container_literal_fallback(parent, expr)
        if container_fallback is not None:
            return container_fallback
    if _classgen_str_eq(kind, "TupleExpr"):
        container_fallback = _classgen_emit_container_literal_fallback(parent, expr)
        if container_fallback is not None:
            return container_fallback
    if _classgen_str_eq(kind, "DictExpr"):
        container_fallback = _classgen_emit_container_literal_fallback(parent, expr)
        if container_fallback is not None:
            return container_fallback
    if _is_ast_node(expr, ListExpr):
        container_fallback = _classgen_emit_container_literal_fallback(parent, expr)
        if container_fallback is not None:
            return container_fallback
    if _is_ast_node(expr, TupleExpr):
        container_fallback = _classgen_emit_container_literal_fallback(parent, expr)
        if container_fallback is not None:
            return container_fallback
    if _is_ast_node(expr, DictExpr):
        container_fallback = _classgen_emit_container_literal_fallback(parent, expr)
        if container_fallback is not None:
            return container_fallback

    is_attr_expr = False
    try:
        _obj = expr.obj
        _name = expr.name
    except AttributeError:
        pass
    else:
        if _obj is not None and _name is not None:
            is_attr_expr = True
    try:
        value = parent._emit_expr(expr)
    except Exception:
        if is_attr_expr:
            return ir.Value(_PTR, "null")
        raise
    if _classgen_value_type_or_none(value) is not None:
        if not _classgen_value_is_null(value):
            return value
        fallback = _classgen_literal_fallback(parent, expr, annotation)
        if fallback is not None:
            return fallback
        container_fallback = _classgen_emit_container_literal_fallback(parent, expr)
        if container_fallback is not None:
            return container_fallback
        return value
    fallback = _classgen_literal_fallback(parent, expr, annotation)
    if fallback is not None:
        return fallback
    container_fallback = _classgen_emit_container_literal_fallback(parent, expr)
    if container_fallback is not None:
        return container_fallback
    return value


def _classgen_builder_call(builder, fn, args):
    n_args = len(args)
    if n_args == 0:
        return builder.call(fn, ())
    if n_args == 1:
        return builder.call(fn, (args[0],))
    if n_args == 2:
        return builder.call(fn, (args[0], args[1]))
    if n_args == 3:
        return builder.call(fn, (args[0], args[1], args[2]))
    if n_args == 4:
        return builder.call(fn, (args[0], args[1], args[2], args[3]))
    if n_args == 5:
        return builder.call(fn, (args[0], args[1], args[2], args[3], args[4]))
    if n_args == 6:
        return builder.call(
            fn,
            (args[0], args[1], args[2], args[3], args[4], args[5]),
        )
    if n_args == 7:
        return builder.call(
            fn,
            (args[0], args[1], args[2], args[3], args[4], args[5], args[6]),
        )
    return builder.call(fn, args)


def _classgen_fn_type_or_none(fn):
    fty = getattr(fn, "ftype", None)
    if fty is not None:
        return fty
    fty = getattr(fn, "function_type", None)
    if fty is not None:
        return fty
    ty = getattr(fn, "type", None)
    pointee = getattr(ty, "pointee", None)
    if pointee is not None and getattr(pointee, "return_type", None) is not None:
        return pointee
    return None


def _classgen_fn_ref(fn) -> str:
    name = getattr(fn, "name", None)
    if name is not None:
        return "@" + name
    return str(fn)


def _classgen_ir_type_text_or_ptr(ty) -> str:
    if ty is None:
        return "ptr"
    if not ty:
        return "ptr"
    text = str(ty)
    if not text:
        return "ptr"
    return text


def _classgen_value_ref_text(value) -> str:
    ref = getattr(value, "_ref", None)
    if ref:
        ref_text = str(ref)
        if ref_text == "null":
            ty = getattr(value, "type", None)
            ty_text = _classgen_ir_type_text_or_ptr(ty)
            if ty_text == "i1":
                return "false"
            if len(ty_text) > 1 and ty_text[0] == "i" and ty_text[1:].isdigit():
                return "0"
        return ref_text
    index = getattr(value, "index", None)
    if index is not None:
        return "%." + str(index + 1)
    text = str(value)
    if text == "null":
        ty = getattr(value, "type", None)
        ty_text = _classgen_ir_type_text_or_ptr(ty)
        if ty_text == "i1":
            return "false"
        if len(ty_text) > 1 and ty_text[0] == "i" and ty_text[1:].isdigit():
            return "0"
    if text:
        return text
    return "null"


def _classgen_ref_text_is_null(ref_text: str) -> bool:
    text = ref_text.strip()
    return text == "null" or text.endswith(" null")


def _classgen_bool_literal_ref_text(expr, expected_ir_ty) -> str:
    if _classgen_ir_type_text_or_ptr(expected_ir_ty) != "i1":
        return ""
    try:
        value = expr.value
    except AttributeError:
        return ""
    if _is_ast_node(expr, BoolLit):
        return "true" if bool(value) else "false"
    if _classgen_str_eq(_node_kind_name(expr), "BoolLit"):
        return "true" if bool(value) else "false"
    try:
        ty = expr.ty
    except AttributeError:
        return ""
    if _classgen_type_name(ty) == "bool" or _is_ast_node(ty, BoolType):
        return "true" if bool(value) else "false"
    return ""


def _classgen_value_is_null(value) -> bool:
    return _classgen_ref_text_is_null(_classgen_value_ref_text(value))


def _classgen_current_param_ref_text(parent, ident: str) -> str:
    fd = getattr(parent, "current_func_def", None)
    if fd is None:
        return ""
    runtime_args = fd.args
    runtime_index = 0
    i = 0
    while i < len(runtime_args):
        arg = runtime_args[i]
        if arg.name != "":
            if arg.name == ident:
                return "%." + str(runtime_index + 1)
            runtime_index += 1
        i += 1
    return ""


def _classgen_maybe_unbox_recovered_arg(
    parent,
    value,
    expected_ir_ty,
    annotation: Optional[Type],
):
    if value is None:
        return None
    if expected_ir_ty is None:
        return value
    value_ty = _classgen_value_type_or_none(value)
    if (
        expected_ir_ty is not None
        and not _classgen_ir_type_is_pointer(expected_ir_ty)
        and _classgen_ir_type_is_pointer(value_ty)
    ):
        target_ty = annotation
        if target_ty is None:
            expected_text = str(expected_ir_ty)
            if expected_text == "i64":
                target_ty = IntType(name="int")
            elif expected_text == "i1":
                target_ty = BoolType(name="bool")
        if target_ty is None:
            return value
        return marshal.marshal_from_object(
            parent.builder,
            parent.module,
            parent.runtime,
            value,
            target_ty,
        )
    return value


def _classgen_current_name_load(
    parent,
    ident: str,
    expected_ir_ty=None,
    annotation: Optional[Type] = None,
):
    env = parent.env
    if env is not None and ident in env:
        slot = env.get(ident)
        if slot is not None and len(slot) > 0:
            alloca = slot[0]
            if alloca is not None:
                value = parent.builder.load(alloca, name="classgen.arg." + ident)
                if expected_ir_ty is None or expected_ir_ty is _PTR:
                    return value
                return _classgen_maybe_unbox_recovered_arg(
                    parent,
                    value,
                    expected_ir_ty,
                    annotation,
                )
    module_globals = parent._module_globals
    if module_globals is not None and ident in module_globals:
        slot = module_globals.get(ident)
        if slot is not None and len(slot) > 0:
            gv = slot[0]
            if gv is not None:
                value = parent.builder.load(gv, name="classgen.arg." + ident)
                if expected_ir_ty is None or expected_ir_ty is _PTR:
                    return value
                return _classgen_maybe_unbox_recovered_arg(
                    parent,
                    value,
                    expected_ir_ty,
                    annotation,
                )
    return None


def _classgen_recover_attr_value(
    classgen,
    parent,
    expr,
    expected_ir_ty=None,
    annotation: Optional[Type] = None,
):
    kind = _node_kind_name(expr)
    if not _classgen_str_eq(kind, "Attr"):
        return None
    obj = expr.obj
    attr_name = expr.name
    if _classgen_str_eq(_node_kind_name(obj), "Name"):
        obj_ident = obj.ident
    else:
        obj_ident = ""
    if not obj_ident:
        return None
    info = None
    if obj_ident == "self" or obj_ident == "cls":
        info = getattr(parent, "current_class", None)
    else:
        env = getattr(parent, "env", None)
        if env is not None:
            slot = _classgen_dict_get_str(env, obj_ident)
            if slot is not None:
                try:
                    declared_ty = slot[2]
                except (IndexError, TypeError):
                    declared_ty = None
                if declared_ty is not None:
                    try:
                        declared_name = parent._class_hint_from_annotation(declared_ty)
                    except Exception:
                        declared_name = None
                    if declared_name is None:
                        declared_name = _classgen_type_name(declared_ty)
                    if declared_name is not None:
                        info = _classgen_lookup_class_info(classgen, declared_name)
        hint = None
        if info is None:
            env_class_hint = getattr(parent, "env_class_hint", None)
            hint = _classgen_dict_get_str(env_class_hint, obj_ident)
            if hint is not None:
                info = _classgen_lookup_class_info(classgen, hint)
        if info is None:
            try:
                hint = parent._class_hint_for_expr(obj)
            except Exception:
                hint = None
            if hint is None:
                env_class_hint = getattr(parent, "env_class_hint", None)
                if env_class_hint is not None:
                    hint = _classgen_dict_get_str(env_class_hint, obj_ident)
            if hint is not None:
                info = _classgen_lookup_class_info(classgen, hint)
        if info is None:
            info = _classgen_local_assignment_class_info(classgen, parent, obj_ident)
    if info is not None:
        idx = _classgen_direct_field_index(info, attr_name)
        if idx is None:
            idx = classgen.lookup_field_index(info, attr_name)
        if idx is not None:
            self_val = _classgen_current_name_load(parent, obj_ident, _PTR, None)
            if self_val is None:
                try:
                    self_val = parent._emit_expr(obj)
                except Exception:
                    return None
            raw = parent.builder.call(
                parent.runtime["py_instance_get_field"],
                [self_val, _classgen_i32_index_constant(idx)],
                name="classgen.arg." + attr_name,
            )
            return _classgen_maybe_unbox_recovered_arg(
                parent,
                raw,
                expected_ir_ty,
                annotation,
            )
    return _classgen_emit_dynamic_attr_value(parent, expr, expected_ir_ty, annotation)


def _classgen_local_assignment_class_info(classgen, parent, ident: str):
    fd = getattr(parent, "current_func_def", None)
    if fd is None:
        return None
    found = None
    work = [getattr(fd, "body", ())]
    while work:
        body = work.pop()
        i = 0
        while i < len(body):
            stmt = body[i]
            targets = getattr(stmt, "targets", ())
            for target in targets:
                try:
                    target_ident = target.ident
                except AttributeError:
                    target_ident = None
                target_matches = False
                if target_ident is not None:
                    target_text = str(target_ident)
                    ident_text = str(ident)
                    if len(target_text) == len(ident_text):
                        target_matches = True
                        text_i = 0
                        while text_i < len(target_text):
                            if target_text[text_i] != ident_text[text_i]:
                                target_matches = False
                                break
                            text_i += 1
                if target_matches:
                    try:
                        value = stmt.value
                    except AttributeError:
                        value = None
                    hint = _classgen_expr_class_hint(classgen, parent, value)
                    if hint is not None:
                        found = _classgen_lookup_class_info(classgen, hint)
            child = getattr(stmt, "body", ())
            if child:
                work.append(child)
            child = getattr(stmt, "else_body", ())
            if child:
                work.append(child)
            child = getattr(stmt, "finally_body", ())
            if child:
                work.append(child)
            handlers = getattr(stmt, "handlers", ())
            h_i = 0
            while h_i < len(handlers):
                handler = handlers[h_i]
                child = getattr(handler, "body", ())
                if child:
                    work.append(child)
                h_i += 1
            i += 1
    return found


def _classgen_expr_class_hint(classgen, parent, expr):
    if expr is None:
        return None
    try:
        func = expr.func
    except AttributeError:
        return None
    try:
        callee = func.ident
    except AttributeError:
        callee = None
    if callee is not None and callee in classgen.classes:
        return callee
    try:
        method_name = func.name
        receiver_expr = func.obj
    except AttributeError:
        return None
    receiver_hint = None
    try:
        receiver_ident = receiver_expr.ident
    except AttributeError:
        receiver_ident = None
    if receiver_ident == "self" or receiver_ident == "cls":
        cur = getattr(parent, "current_class", None)
        if cur is not None:
            receiver_hint = cur.name
    if receiver_hint is None:
        try:
            receiver_hint = parent._class_hint_for_expr(receiver_expr)
        except Exception:
            receiver_hint = None
    if receiver_hint is None:
        return None
    direct_info = _classgen_lookup_class_info(classgen, receiver_hint)
    direct_hint = _classgen_method_return_hint_from_info(
        classgen,
        parent,
        direct_info,
        method_name,
    )
    if direct_hint is not None:
        return direct_hint
    method_info = parent._resolve_method_mro(receiver_hint, method_name)
    if method_info is None:
        return None
    method_owner_name = getattr(method_info, "name", None)
    if method_owner_name is None:
        return None
    method_fd = classgen._find_method_def(method_owner_name, method_name)
    if method_fd is None:
        return None
    try:
        ret_hint = parent._class_hint_from_annotation(method_fd.return_ty)
    except Exception:
        ret_hint = None
    if ret_hint is not None:
        return ret_hint
    return None


def _classgen_method_return_hint_from_info(classgen, parent, info, method_name: str):
    if info is None:
        return None
    method_defs = getattr(info, "method_defs", ())
    i = 0
    while i < len(method_defs):
        pair = method_defs[i]
        name = pair[0]
        if name == method_name:
            fd = pair[1]
            try:
                ret_hint = parent._class_hint_from_annotation(fd.return_ty)
            except Exception:
                ret_hint = None
            if ret_hint is not None:
                return ret_hint
            ret_name = getattr(fd.return_ty, "name", None)
            if ret_name in classgen.classes:
                return ret_name
        i += 1
    return None


def _classgen_recover_method_call_arg(
    classgen,
    parent,
    expr,
    expected_ir_ty=None,
    annotation: Optional[Type] = None,
):
    start_index = IRBuilder_current_instruction_count(parent.builder)
    value = _classgen_emit_arg_expr(parent, expr, annotation)
    if _classgen_value_is_null(value):
        ident = getattr(expr, "ident", None)
        if ident is not None:
            recovered = _classgen_current_name_load(
                parent,
                ident,
                expected_ir_ty,
                annotation,
            )
            if recovered is not None:
                value = recovered
        elif getattr(expr, "obj", None) is not None and getattr(expr, "name", None):
            recovered = _classgen_recover_attr_value(
                classgen,
                parent,
                expr,
                expected_ir_ty,
                annotation,
            )
            if recovered is not None:
                value = recovered
    value_ty = _classgen_value_type_or_none(value)
    expected_is_object = expected_ir_ty is not None and _classgen_ir_type_is_pointer(
        expected_ir_ty
    )
    if (
        expected_is_object
        and value_ty is not None
        and not _classgen_ir_type_is_pointer(value_ty)
    ):
        value = marshal.marshal_to_object(
            parent.builder,
            parent.module,
            parent.runtime,
            value,
            expr.ty,
        )
    else:
        value = _classgen_maybe_unbox_recovered_arg(
            parent,
            value,
            expected_ir_ty,
            annotation,
        )
    if _classgen_value_is_null(value):
        recent_ref = _classgen_recent_value_ref_text(parent.builder, start_index)
        if recent_ref:
            value_ty = expected_ir_ty if expected_ir_ty is not None else _PTR
            value = ir.Value(value_ty, recent_ref)
    return value


def _classgen_recover_self_method_call_value(
    classgen,
    parent,
    expr,
    expected_ir_ty=None,
    annotation: Optional[Type] = None,
):
    func = getattr(expr, "func", None)
    method_name = getattr(func, "name", "")
    obj = getattr(func, "obj", None)
    obj_ident = getattr(obj, "ident", "")
    if not method_name or (obj_ident != "self" and obj_ident != "cls"):
        return None
    info = getattr(parent, "current_class", None)
    if info is None:
        return None
    method_fn = info.methods.get(method_name)
    if method_fn is None:
        return None
    self_val = _classgen_current_name_load(parent, obj_ident, _PTR, None)
    if self_val is None:
        return None
    method_ast = classgen._find_method_def(info.name, method_name)
    declared = [a for a in (method_ast.args[1:] if method_ast else ()) if a.name != ""]
    call_args = [self_val]
    fn_args = getattr(method_fn, "args", ())
    expr_args = getattr(expr, "args", ())
    i = 0
    while i < len(expr_args):
        arg_expr = expr_args[i]
        arg_ann = None
        if i < len(declared):
            arg_ann = declared[i].annotation
        arg_expected_ir_ty = None
        param_index = i + 1
        if param_index < len(fn_args):
            arg_expected_ir_ty = _classgen_arg_type_or_none(fn_args, param_index)
        value = _classgen_recover_method_call_arg(
            classgen,
            parent,
            arg_expr,
            arg_expected_ir_ty,
            arg_ann,
        )
        call_args.append(value)
        i += 1
    value = _classgen_builder_call(parent.builder, method_fn, call_args)
    return _classgen_maybe_unbox_recovered_arg(
        parent,
        value,
        expected_ir_ty,
        annotation,
    )


def _classgen_recover_call_value(
    classgen,
    parent,
    expr,
    expected_ir_ty=None,
    annotation: Optional[Type] = None,
):
    try:
        func = expr.func
    except AttributeError:
        return None
    recovered = _classgen_recover_self_method_call_value(
        classgen,
        parent,
        expr,
        expected_ir_ty,
        annotation,
    )
    if recovered is not None:
        return recovered
    try:
        if getattr(func, "obj", None) is not None and getattr(func, "name", None):
            value = parent._emit_method_call(expr)
        else:
            value = parent._emit_call(expr)
    except Exception:
        return None
    return _classgen_maybe_unbox_recovered_arg(
        parent,
        value,
        expected_ir_ty,
        annotation,
    )


def _classgen_recent_value_ref_text(builder, start_index: int) -> str:
    count = IRBuilder_current_instruction_count(builder)
    i = count - 1
    while i >= start_index:
        line = IRBuilder_instruction_text_at(builder, i)
        eq = line.find(" = ")
        if eq > 0 and line.startswith("%"):
            return line[:eq]
        i -= 1
    return ""


def _classgen_recent_attr_call_matches(builder, start_index: int, ref: str) -> bool:
    if not ref:
        return False
    count = IRBuilder_current_instruction_count(builder)
    i = start_index
    prefix = ref + " = "
    while i < count:
        line = IRBuilder_instruction_text_at(builder, i)
        if line.startswith(prefix):
            if "py_instance_get_field" in line or "py_obj_getattr" in line:
                return True
        i += 1
    return False


def _classgen_cname_const_ref(classgen, name: str) -> str:
    data = _classgen_utf8_byte_values(name) + [0]
    n = len(data)
    arr_ty = ir.ArrayType(_I8, n)
    gv_name = classgen._fresh(".classgen.attr.name")
    gv = ir.GlobalVariable(classgen.parent.module, arr_ty, name=gv_name)
    gv.linkage = "internal"
    gv.global_constant = True
    gv.initializer = ir.Constant(arr_ty, data)
    gv_name = getattr(gv, "name", "")
    if not gv_name:
        return "null"
    return (
        "getelementptr inbounds (["
        + str(n)
        + " x i8], ptr @"
        + gv_name
        + ", i32 0, i32 0)"
    )


def _classgen_emit_discarded_call(
    builder,
    fn,
    args,
    arg_type_texts,
    arg_ref_texts,
    returns_void: bool,
) -> None:
    ret_ty = _VOID if returns_void else _PTR
    n_args = len(args)
    arg_types = ""
    i = 0
    while i < n_args:
        if i > 0:
            arg_types = arg_types + ", "
        arg_types = arg_types + str(arg_type_texts[i])
        i += 1
    sig_text = str(ret_ty)
    sig_text = sig_text + " ("
    sig_text = sig_text + arg_types
    sig_text = sig_text + ")"

    args_text = ""
    i = 0
    while i < n_args:
        if i > 0:
            args_text = args_text + ", "
        args_text = args_text + str(arg_type_texts[i])
        args_text = args_text + " "
        args_text = args_text + str(arg_ref_texts[i])
        i += 1
    call_text = "call "
    call_text = call_text + sig_text
    call_text = call_text + " "
    call_text = call_text + _classgen_fn_ref(fn)
    call_text = call_text + "("
    call_text = call_text + args_text
    call_text = call_text + ")"
    callee_ref = _classgen_fn_ref(fn)
    if str(ret_ty) == "void":
        record = IRBuilder_emit_raw(builder, call_text)
        IRBuilder_publish_direct_raw_call(
            builder,
            record,
            None,
            ret_ty,
            callee_ref,
            arg_type_texts,
            arg_ref_texts,
        )
    else:
        result = IRBuilder_next_value(builder, "", ret_ty)
        record = IRBuilder_emit_raw(builder, str(result) + " = " + call_text)
        IRBuilder_publish_direct_raw_call(
            builder,
            record,
            str(result),
            ret_ty,
            callee_ref,
            arg_type_texts,
            arg_ref_texts,
        )


class ClassLoweringError(Exception):
    """Raised when a ClassDef shape is malformed or unsupported."""


def _builtin_exception_tag_for_base_name(name: str) -> Optional[int]:
    """Return the pcc builtin-exception tag for a class base name.

    This calls the shared codegen metadata module instead of reading through
    getattr(parent, ...): the self-hosted runtime does not yet expose Python
    class attributes through instance getattr, and losing this base breaks
    ``except Exception`` for user-defined exception subclasses.
    """

    tag = builtin_exc_tag_or_missing(name)
    if tag >= 0:
        return tag
    return None


def _expr_is_yield_sentinel(expr: Expr) -> bool:
    return (
        _is_ast_node(expr, Call)
        and _is_ast_node(expr.func, Name)
        and expr.func.ident
        in (
            "_yield",
            "__yield__",
            "_yield_from",
            "__yield_from__",
        )
    )


def _funcdef_has_yield_sentinel(fd: FuncDef) -> bool:
    work = list(fd.body)
    idx = 0
    while idx < len(work):
        stmt = work[idx]
        idx += 1
        if _is_ast_node(stmt, ExprStmt):
            if _expr_is_yield_sentinel(stmt.expr):
                return True
            continue
        if _is_ast_node(stmt, Return):
            if stmt.value is not None and _expr_is_yield_sentinel(stmt.value):
                return True
            continue
        if _is_ast_node(stmt, If):
            work.extend(stmt.body)
            work.extend(stmt.else_body)
            continue
        if _is_ast_node(stmt, While):
            work.extend(stmt.body)
            work.extend(stmt.else_body)
            continue
        if _is_ast_node(stmt, For):
            work.extend(stmt.body)
            work.extend(stmt.else_body)
            continue
        if _is_ast_node(stmt, With):
            work.extend(stmt.body)
            continue
        if _is_ast_node(stmt, Try):
            work.extend(stmt.body)
            for handler in stmt.handlers:
                work.extend(handler.body)
            work.extend(stmt.else_body)
            work.extend(stmt.finally_body)
            continue
    return False


class ClassInfo:
    """Per-class metadata gathered at declaration time.

    Attributes:
        name: Simple class name (no module prefix).
        global_var: ``i8*`` global variable holding the runtime
            PyClassObject* pointer. Initialised to NULL at emit-time.
        bases_ast: Base-class AST :class:`Name` nodes. Phase 3 only
            supports base names that resolve to ``ClassInfo`` entries in
            the enclosing module.
        field_names: Ordered list of declared instance-field names. The
            field index exposed to ``py_instance_get_field`` equals the
            index into this list.
        methods: Mapping ``method_name -> ir.Function``. Method functions
            are declared here but their bodies are emitted by the parent
            :class:`L1CodeGen` via its normal FuncDef path.
    """

    def __init__(
        self, name: str, global_var: ir.GlobalVariable, bases_ast: tuple[Expr, ...]
    ):
        self.name = name
        self.global_var = global_var
        self.bases_ast = bases_ast
        self.field_names: list[str] = []
        self.field_types: dict[str, Type] = {}
        self.owning_module: Optional[str] = None
        self.export_class_name: Optional[str] = name
        self.class_attrs: dict[str, tuple[ir.GlobalVariable, Type]] = {}
        self.class_attr_values: dict[str, Expr] = {}
        self.class_attr_initializers: list[tuple[str, Expr]] = []
        self.methods: dict[str, ir.Function] = {}
        self.method_defs: list[tuple[str, FuncDef]] = []
        self.init_fn: Optional[ir.Function] = None
        self.init_param_types: list[ir.Type] = []
        self.init_returns_void = False
        # Stable function operands for runtime method registration.
        # The self-hosted compiler can hit class-identity edge cases while
        # rendering Function objects; the LLVM symbol spelling is fixed at
        # declaration time, so keep that reference separately.
        self.method_refs: dict[str, ir.Value] = {}
        # Method kind map: 'instance' (default), 'static', 'classmethod',
        # 'property_getter'. Drives argument marshalling and call-site
        # dispatch. Populated by :meth:`_declare_method`.
        self.method_kinds: dict[str, str] = {}
        # Cross-module method descriptors carry the same closed-world
        # continuation ABI bit as function exports.  Local methods are keyed
        # by exact FuncDef ids on the parent codegen; extern methods use this
        # explicit name set because their FuncDef nodes are synthesized.
        self.may_park_methods: set[str] = set()
        # For @property methods, track the getter function separately
        # from the stored-name slot so attribute access fires the
        # getter rather than a field lookup.
        self.properties: dict[str, ir.Function] = {}
        # Matching @<name>.setter functions so ``obj.<name> = value``
        # dispatches to the right function.
        self.property_setters: dict[str, ir.Function] = {}
        # Matching @<name>.deleter functions so ``del obj.<name>``
        # dispatches to the right function.
        self.property_deleters: dict[str, ir.Function] = {}
        # The (possibly @dataclass-expanded) ClassDef AST — needed so
        # call-site lookups (e.g. ``_find_method_def``) see synthetic
        # methods, not just what the user wrote.
        self.expanded_cd: "ClassDef | None" = None
        # Cross-module extern classes synthesize FuncDef stubs from export
        # metadata so kwargs/default resolution can use normal method
        # lowering. Keep this as a declared pcc field; dynamic post-init
        # attributes are not reliable in the native object layout.
        self.extern_method_defs: dict[str, FuncDef] = {}
        self.enum_members: dict[str, int] = {}
        self.enum_string_members: dict[str, str] = {}
        self.protocol_members: list[str] = []
        self.runtime_decorators: tuple[Expr, ...] = ()
        self.metaclass_name: Optional[str] = None
        self.slots_only = False
        self.dataclass_frozen = False
        self.valueclass = False


class ClassLowering:
    """Codegen helper bound to an :class:`L1CodeGen` instance.

    The parent codegen creates one :class:`ClassLowering` per module
    (shared across the two compile passes), then calls
    :meth:`declare_class`, :meth:`emit_methods`, and finally
    :meth:`emit_module_init` in sequence.
    """

    def __init__(self, parent: L1CodeGen):
        # parent: L1CodeGen — avoid importing to sidestep a circular
        # dependency. We only use .module, .ast_module, .runtime,
        # .functions, ._user_symbol, etc.
        self.parent = parent
        # class_name -> ClassInfo
        self.classes: dict[str, ClassInfo] = {}
        # Counter for unique global names.
        self._uniq = 0
        # Pool of interned const-char*[] globals for field-name arrays.
        self._field_arr_pool: dict[tuple[str, ...], ir.GlobalVariable] = {}
        # Pool of interned const-char* globals for class/attr names.
        self._cname_pool: dict[str, ir.GlobalVariable] = {}
        # Pool for bases pointer arrays keyed by the tuple of global
        # variable names.
        self._base_arr_pool: dict[tuple[str, ...], ir.GlobalVariable] = {}
        self._class_defs: list[ClassDef] = []

    # ------------------------------------------------------ declaration

    def _fresh(self, hint: str) -> str:
        self._uniq += 1
        return hint + "." + str(self._uniq)

    def declare_class(self, cd: ClassDef) -> ClassInfo:
        """First-pass: register the class and declare all its methods.

        Populates ``self.classes[cd.name]`` and declares the module-level
        global + each method function. Returns the :class:`ClassInfo`.
        """
        if cd.name in self.classes:
            raise ClassLoweringError(f"duplicate class definition for {cd.name!r}")
        # ``@dataclass`` is supported by synthesizing ``__init__`` /
        # ``__repr__`` / ``__eq__`` into the class body. Native unary
        # decorators are applied to the completed class object at module-init
        # time; compile-time/no-op decorators remain stripped here.
        valueclass = _class_has_valueclass_decorator(cd)
        dataclass_options = _dataclass_options(cd)
        if valueclass:
            dataclass_options["frozen"] = True
        original_cd = cd
        runtime_decorators: list[Expr] = []
        for dec in original_cd.decorators:
            dname = _simple_decorator_name(dec)
            if (
                self._class_decorator_is_noop(dec)
                or _is_dataclass_decorator_name(dname)
                or _is_valueclass_decorator_name(dname)
            ):
                continue
            if _is_ast_node(dec, Name):
                runtime_decorators.append(dec)
                continue
            raise NotImplementedError(
                f"Layer 1 does not handle class decorator expression " f"on {cd.name!r}"
            )
        cd = self._maybe_expand_dataclass(cd)
        expanded = cd is not original_cd
        if cd.decorators:
            cd = ClassDef(
                span=cd.span,
                name=cd.name,
                bases=cd.bases,
                keywords=cd.keywords,
                decorators=(),
                body=cd.body,
            )
        if cd.keywords and any(
            not self._class_keyword_is_noop(cd, k) for k, _ in cd.keywords
        ):
            raise NotImplementedError(
                f"Layer 1 does not handle class keyword arguments on {cd.name!r} "
                "(metaclass / kw-based class bases are out of scope)"
            )

        module = self.parent.module
        g_name = self._class_global_name(cd.name)
        existing = module.globals.get(g_name)
        if isinstance(existing, ir.GlobalVariable):
            raise ClassLoweringError(
                f"class global {g_name!r} already exists — duplicate class?"
            )
        gv = ir.GlobalVariable(module, _PTR, name=g_name)
        # In multi-file compile mode, other modules may reference this
        # class via ``declare_extern_class`` — leave linkage as the
        # default (external) so the linker can resolve the reference.
        # Use ``internal`` only when this module is compiled solo.
        # Use direct attribute access rather than getattr-with-default:
        # under pcc-py self-host the latter can return the default for
        # instance attrs that ARE set externally (see investigation
        # ``pcc1-self-host-generator-ctx-slot``).
        if self.parent._native_module_exports is None:
            gv.linkage = "internal"
        gv.initializer = ir.Constant(_PTR, None)

        # Seed field_names with the parents' declared fields.
        # Use an order-preserving set-like approach via dict.fromkeys.
        fields_ordered = {}
        for base_expr in cd.bases:
            if not _is_ast_node(base_expr, Name) or base_expr.ident == "object":
                continue
            parent_info = self.classes.get(base_expr.ident)
            pf_names = []
            if parent_info is not None:
                pf_names = parent_info.field_names
            else:
                native_table = (
                    getattr(self.parent, "_native_module_exports", None) or {}
                )
                for mod_exports in native_table.values():
                    rem_info = mod_exports.get(base_expr.ident)
                    if isinstance(rem_info, dict) and rem_info.get("kind") == "class":
                        pf_names = rem_info.get("field_names", ())
                        break
            for pf in pf_names:
                fields_ordered[pf] = True

        info = ClassInfo(name=cd.name, global_var=gv, bases_ast=cd.bases)
        info.runtime_decorators = tuple(runtime_decorators)
        info.metaclass_name = self._resolve_metaclass_name_for_class(cd)
        info.dataclass_frozen = bool(dataclass_options.get("frozen", False))
        info.valueclass = valueclass
        info.field_names = list(fields_ordered.keys())
        if expanded:
            info.expanded_cd = cd
        self.classes[cd.name] = info
        self._class_defs.append(cd)

        # Walk the class body: collect fields from __init__ self-writes
        # and class-level annotations. Ensure __slots__ are added first.
        self._collect_fields_and_declare_methods(cd, info)
        source_module_name = self.parent.ast_module.name
        if module_has_contract(source_module_name, PY_AST_FIELD_ORDER_CONTRACT):
            info.owning_module = source_module_name
            info.export_class_name = cd.name
            override_names = PY_AST_FIELD_NAME_OVERRIDES.get(cd.name)
            if override_names is not None:
                info.field_names = list(override_names)
        if (
            module_has_contract(source_module_name, L1_CODEGEN_HOST_ATTR_CONTRACT)
            and cd.name == "L1CodeGen"
        ):
            for host_attr_name in L1_CODEGEN_HOST_ATTRS:
                if host_attr_name not in info.field_names:
                    info.field_names.append(host_attr_name)
        self._declare_metaclass_literal_attrs(cd, info)
        return info

    def _resolve_metaclass_name_for_class(self, cd: ClassDef) -> Optional[str]:
        for key, value in cd.keywords:
            if key == "metaclass":
                return self._resolve_metaclass_expr_name(cd, value)
        return self._resolve_metaclass_name_from_bases(cd)

    def _resolve_metaclass_expr_name(self, cd: ClassDef, value: Expr) -> Optional[str]:
        if _is_ast_node(value, Name):
            if value.ident in self.classes:
                return value.ident
            return self._resolve_module_class_alias_before(value.ident, cd)
        if _is_ast_node(value, Call) and _is_ast_node(value.func, Name):
            return self._resolve_function_returning_class_name_before(
                value.func.ident,
                cd,
            )
        if _is_ast_node(value, IfExpr):
            then_name = self._resolve_metaclass_expr_name(cd, value.then_e)
            else_name = self._resolve_metaclass_expr_name(cd, value.else_e)
            if then_name is not None and then_name == else_name:
                return then_name
        if _is_ast_node(value, BoolExpr):
            left_name = self._resolve_metaclass_expr_name(cd, value.left)
            right_name = self._resolve_metaclass_expr_name(cd, value.right)
            if left_name is not None and left_name == right_name:
                return left_name
            if (
                value.op == "or"
                and left_name is None
                and right_name is not None
                and self._resolve_metaclass_expr_is_falsey(cd, value.left)
            ):
                return right_name
        return None

    def _resolve_metaclass_expr_is_falsey(
        self,
        cd: ClassDef,
        value: Expr,
    ) -> bool:
        if _is_ast_node(value, NoneLit):
            return True
        if _is_ast_node(value, BoolLit):
            return getattr(value, "value", True) == False
        if _is_ast_node(value, Call) and _is_ast_node(value.func, Name):
            return self._resolve_function_returning_falsey_before(
                value.func.ident,
                cd,
            )
        if _is_ast_node(value, BoolExpr):
            if value.op == "and":
                return self._resolve_metaclass_expr_is_falsey(cd, value.left)
            if value.op == "or":
                return self._resolve_metaclass_expr_is_falsey(
                    cd, value.left
                ) and self._resolve_metaclass_expr_is_falsey(cd, value.right)
        return False

    def _resolve_function_returning_falsey_before(
        self,
        func_name: str,
        before_cd: ClassDef,
    ) -> bool:
        for stmt in self.parent.ast_module.body:
            if stmt is before_cd:
                break
            if not _is_ast_node(stmt, FuncDef) or stmt.name != func_name:
                continue
            for body_stmt in stmt.body:
                if _is_ast_node(body_stmt, Return):
                    return self._resolve_return_value_is_falsey(body_stmt.value)
            return False
        return False

    def _resolve_return_value_is_falsey(
        self,
        value: Optional[Expr],
    ) -> bool:
        if value is None:
            return True
        if _is_ast_node(value, NoneLit):
            return True
        if _is_ast_node(value, BoolLit):
            return getattr(value, "value", True) == False
        return False

    def _resolve_function_returning_class_name_before(
        self,
        func_name: str,
        before_cd: ClassDef,
    ) -> Optional[str]:
        for stmt in self.parent.ast_module.body:
            if stmt is before_cd:
                break
            if not _is_ast_node(stmt, FuncDef) or stmt.name != func_name:
                continue
            name = self._resolve_body_returning_class_name_before(
                stmt.body,
                before_cd,
            )
            if name == _NO_METACLASS_RETURN or name == _AMBIGUOUS_METACLASS_RETURN:
                return None
            return name
        return None

    def _resolve_return_value_class_name_before(
        self,
        value: Optional[Expr],
        before_cd: ClassDef,
    ) -> str:
        if not _is_ast_node(value, Name):
            return _AMBIGUOUS_METACLASS_RETURN
        if value.ident in self.classes:
            return value.ident
        alias = self._resolve_module_class_alias_before(value.ident, before_cd)
        if alias is None:
            return _AMBIGUOUS_METACLASS_RETURN
        return alias

    def _merge_metaclass_return_names(self, lhs: str, rhs: str) -> str:
        if lhs == _AMBIGUOUS_METACLASS_RETURN:
            return _AMBIGUOUS_METACLASS_RETURN
        if rhs == _AMBIGUOUS_METACLASS_RETURN:
            return _AMBIGUOUS_METACLASS_RETURN
        if lhs == _NO_METACLASS_RETURN:
            return rhs
        if rhs == _NO_METACLASS_RETURN:
            return lhs
        if lhs == rhs:
            return lhs
        return _AMBIGUOUS_METACLASS_RETURN

    def _resolve_stmt_returning_class_name_before(
        self,
        stmt,
        before_cd: ClassDef,
    ) -> str:
        if _is_ast_node(stmt, Return):
            return self._resolve_return_value_class_name_before(
                stmt.value,
                before_cd,
            )
        if _node_kind_name(stmt) == "If":
            body_name = self._resolve_body_returning_class_name_before(
                stmt.body,
                before_cd,
            )
            else_name = self._resolve_body_returning_class_name_before(
                stmt.else_body,
                before_cd,
            )
            if body_name == _NO_METACLASS_RETURN:
                if else_name == _NO_METACLASS_RETURN:
                    return _NO_METACLASS_RETURN
                return _AMBIGUOUS_METACLASS_RETURN
            if else_name == _NO_METACLASS_RETURN:
                return _AMBIGUOUS_METACLASS_RETURN
            if body_name == else_name:
                return body_name
            return _AMBIGUOUS_METACLASS_RETURN
        return _NO_METACLASS_RETURN

    def _resolve_body_returning_class_name_before(
        self,
        body,
        before_cd: ClassDef,
    ) -> str:
        selected = _NO_METACLASS_RETURN
        for body_stmt in body:
            stmt_name = self._resolve_stmt_returning_class_name_before(
                body_stmt,
                before_cd,
            )
            selected = self._merge_metaclass_return_names(selected, stmt_name)
            if selected == _AMBIGUOUS_METACLASS_RETURN:
                return selected
        return selected

    def _resolve_module_class_alias_before(
        self, alias_name: str, before_cd: ClassDef
    ) -> Optional[str]:
        for stmt in self.parent.ast_module.body:
            if stmt is before_cd:
                break
            if not _is_ast_node(stmt, Assign) or len(stmt.targets) != 1:
                continue
            target = stmt.targets[0]
            if not (
                _is_ast_node(target, Name)
                and target.ident == alias_name
                and _is_ast_node(stmt.value, Name)
            ):
                continue
            target_name = stmt.value.ident
            if target_name in self.classes:
                return target_name
        return None

    def _resolve_metaclass_name_from_bases(self, cd: ClassDef) -> Optional[str]:
        selected: Optional[str] = None
        for base_expr in cd.bases:
            if not _is_ast_node(base_expr, Name) or base_expr.ident == "object":
                continue
            base_info = self.classes.get(base_expr.ident)
            if base_info is None:
                continue
            candidate = base_info.metaclass_name
            if candidate is None:
                continue
            if selected is None:
                selected = candidate
                continue
            if candidate == selected:
                continue
            if self._class_info_is_subclass(candidate, selected):
                selected = candidate
                continue
            if self._class_info_is_subclass(selected, candidate):
                continue
            return _METACLASS_CONFLICT
        return selected

    def _class_info_is_subclass(
        self,
        class_name: str,
        base_name: str,
        seen: Optional[set[str]] = None,
    ) -> bool:
        if class_name == base_name:
            return True
        if seen is None:
            seen = set()
        if class_name in seen:
            return False
        seen.add(class_name)
        info = self.classes.get(class_name)
        if info is None:
            return False
        for base_expr in info.bases_ast:
            if not _is_ast_node(base_expr, Name) or base_expr.ident == "object":
                continue
            if self._class_info_is_subclass(base_expr.ident, base_name, seen):
                return True
        return False

    # ------------------------------------------------------ @dataclass

    def _dataclass_self_attr(self, span: SourceSpan, field_name: str) -> Attr:
        return Attr(
            span=span,
            ty=DynType(name="dyn"),
            obj=Name(span=span, ty=DynType(name="dyn"), ident="self"),
            name=field_name,
        )

    def _dataclass_other_attr(self, span: SourceSpan, field_name: str) -> Attr:
        return Attr(
            span=span,
            ty=DynType(name="dyn"),
            obj=Name(span=span, ty=DynType(name="dyn"), ident="other"),
            name=field_name,
        )

    def _dataclass_field_tuple(
        self,
        span: SourceSpan,
        var_name: str,
        field_names: list[str],
    ) -> TupleExpr:
        elem_ty = DynType(name="dyn")
        elems = tuple(
            Attr(
                span=span,
                ty=elem_ty,
                obj=Name(span=span, ty=DynType(name="dyn"), ident=var_name),
                name=field_name,
            )
            for field_name in field_names
        )
        return TupleExpr(
            span=span,
            ty=TupleType(name="tuple", elems=tuple(elem_ty for _ in elems)),
            elems=elems,
        )

    def _dataclass_synthetic_repr(
        self,
        cd: ClassDef,
        field_names: list[str],
    ) -> FuncDef:
        span = cd.span
        str_ty = StrType(name="str")
        expr: Expr = StrLit(span=span, ty=str_ty, value=cd.name + "(")
        for idx, field_name in enumerate(field_names):
            if idx:
                expr = BinOp(
                    span=span,
                    ty=str_ty,
                    op="+",
                    lhs=expr,
                    rhs=StrLit(span=span, ty=str_ty, value=", "),
                )
            expr = BinOp(
                span=span,
                ty=str_ty,
                op="+",
                lhs=expr,
                rhs=StrLit(span=span, ty=str_ty, value=field_name + "="),
            )
            field_repr = Call(
                span=span,
                ty=str_ty,
                func=Name(span=span, ty=DynType(name="dyn"), ident="repr"),
                args=(self._dataclass_self_attr(span, field_name),),
                kwargs=(),
            )
            expr = BinOp(span=span, ty=str_ty, op="+", lhs=expr, rhs=field_repr)
        expr = BinOp(
            span=span,
            ty=str_ty,
            op="+",
            lhs=expr,
            rhs=StrLit(span=span, ty=str_ty, value=")"),
        )
        return FuncDef(
            span=span,
            name="__repr__",
            args=(Arg("self", None, None, "pos", False),),
            return_ty=str_ty,
            body=(Return(span=span, value=expr),),
            decorators=(),
            is_method=True,
            is_async=False,
        )

    def _dataclass_synthetic_compare(
        self,
        cd: ClassDef,
        field_names: list[str],
        method_name: str,
        op: str,
    ) -> FuncDef:
        span = cd.span
        bool_ty = BoolType(name="bool")
        if field_names:
            lhs = self._dataclass_field_tuple(span, "self", field_names)
            rhs = self._dataclass_field_tuple(span, "other", field_names)
            result: Expr = Compare(
                span=span,
                ty=bool_ty,
                op=op,
                lhs=lhs,
                rhs=rhs,
            )
        else:
            result = BoolLit(span=span, ty=bool_ty, value=(op in ("==", "<=", ">=")))
        body = (Return(span=span, value=result),)
        if method_name == "__eq__":
            # CPython's generated dataclass equality first requires the same
            # concrete class.  Without this guard, enabling runtime instance
            # equality made unrelated compiler AST dataclasses call the left
            # synthetic method and immediately read ``other.<field>`` (for
            # example ``other.obj``), producing the systemic stage-2
            # ``AttributeError: obj`` lift failure.
            type_func = Name(
                span=span,
                ty=DynType(name="dyn"),
                ident="type",
            )
            same_class = Compare(
                span=span,
                ty=bool_ty,
                op="is",
                lhs=Call(
                    span=span,
                    ty=DynType(name="dyn"),
                    func=type_func,
                    args=(
                        Name(
                            span=span,
                            ty=DynType(name="dyn"),
                            ident="self",
                        ),
                    ),
                    kwargs=(),
                ),
                rhs=Call(
                    span=span,
                    ty=DynType(name="dyn"),
                    func=type_func,
                    args=(
                        Name(
                            span=span,
                            ty=DynType(name="dyn"),
                            ident="other",
                        ),
                    ),
                    kwargs=(),
                ),
            )
            body = (
                If(
                    span=span,
                    cond=same_class,
                    body=(Return(span=span, value=result),),
                    else_body=(
                        Return(
                            span=span,
                            value=BoolLit(span=span, ty=bool_ty, value=False),
                        ),
                    ),
                ),
            )
        return FuncDef(
            span=span,
            name=method_name,
            args=(
                Arg("self", None, None, "pos", False),
                Arg("other", DynType(name="dyn"), None, "pos", False),
            ),
            return_ty=bool_ty,
            body=body,
            decorators=(),
            is_method=True,
            is_async=False,
        )

    def _dataclass_synthetic_hash(
        self,
        cd: ClassDef,
        field_names: list[str],
    ) -> FuncDef:
        """CPython's @dataclass(frozen=True) with default eq synthesizes
        ``__hash__ = hash(field tuple)``.  Without this, the generic
        "__eq__ present and no __hash__ -> __hash__ = None" rule (correct
        for ordinary classes and unfrozen dataclasses) made every frozen
        dataclass unhashable: d[(Inner(1), Inner(2))] raised
        "unhashable type" where CPython hashes it.  Hashing the field
        tuple keeps the hash/eq contract, including for nested frozen
        dataclasses (their synthesized __hash__ recurses through the
        tuple hash)."""
        span = cd.span
        int_ty = IntType(name="int")
        tup = self._dataclass_field_tuple(span, "self", field_names)
        result = Call(
            span=span,
            ty=int_ty,
            func=Name(span=span, ty=DynType(name="dyn"), ident="hash"),
            args=(tup,),
            kwargs=(),
        )
        return FuncDef(
            span=span,
            name="__hash__",
            args=(Arg("self", None, None, "pos", False),),
            return_ty=int_ty,
            body=(Return(span=span, value=result),),
            decorators=(),
            is_method=True,
            is_async=False,
        )

    def _maybe_expand_dataclass(self, cd: ClassDef) -> ClassDef:
        """If ``cd`` carries a ``@dataclass`` decorator, synthesize
        ``__init__`` (and keep the other runtime-expected methods as
        TODO) and return a rewritten ClassDef with the decorator
        stripped. Otherwise return ``cd`` unchanged."""
        if not _class_has_dataclass_decorator(
            cd
        ) and not _class_has_valueclass_decorator(cd):
            return cd
        options = _dataclass_options(cd)
        if _class_has_valueclass_decorator(cd):
            options["frozen"] = True

        # Inherited @dataclass fields: walk each base that's itself an
        # expanded @dataclass in this module and prepend its fields
        # (fields declared on the base come FIRST in the synthetic
        # __init__, matching CPython's dataclass inheritance MRO).
        inherited_fields: list[tuple[str, Optional[Type], Optional[Expr]]] = []
        for base_expr in cd.bases:
            if not _is_ast_node(base_expr, Name):
                continue
            base_info = self.classes.get(base_expr.ident)
            if base_info is None or base_info.expanded_cd is None:
                continue
            for s in base_info.expanded_cd.body:
                if _is_ast_node(s, FuncDef) and s.name == "__init__":
                    for a in s.args:
                        if a.name in ("", "self"):
                            continue
                        inherited_fields.append(
                            (a.name, _classgen_annotation_or_none(a), a.default)
                        )
                    break

        # Collect ``name: annotation [= default]`` class-body entries.
        fields: list[tuple[str, Optional[Type], Optional[Expr]]] = list(
            inherited_fields
        )
        remaining_body: list = []
        for stmt in cd.body:
            ann = _classgen_annotation_or_none(stmt)
            is_dataclass_field = (
                _is_ast_node(stmt, Assign)
                and len(stmt.targets) == 1
                and _is_ast_node(stmt.targets[0], Name)
                and (ann is not None or _is_ast_node(stmt.value, NoneLit))
            )
            if is_dataclass_field:
                name = stmt.targets[0].ident
                default = stmt.value
                # pcc's parser lowers a bare ``x: int`` annotation to
                # ``Assign(targets=(Name,), value=NoneLit, annotation=ann)``.
                # It also lowers ``x: int = None`` the same way, so
                # the two cases are indistinguishable at AST level.
                # Pre-2026-04-22 pcc treated all NoneLit-valued
                # annotations as "no default", which made
                # ``MemoryAccess.__init__(self, kind, id, block,
                # pointer=None, ...)`` fail ``missing required
                # argument 'pointer'`` for any caller that relied on
                # the Optional default. Keep the NoneLit so the
                # dataclass-generated ``__init__`` has the default,
                # matching the more-permissive interpretation.
                fields.append((name, ann, default))
                continue
            remaining_body.append(stmt)

        if not fields:
            # Decorator present but no fields — still a valid class,
            # just strip the decorator and return.
            return ClassDef(
                span=cd.span,
                name=cd.name,
                bases=cd.bases,
                keywords=cd.keywords,
                decorators=(),
                body=cd.body,
            )

        # Build synthetic __init__(self, <fields>): self.<f> = <f>.
        span = cd.span
        init_args: list = [
            Arg(
                name="self",
                annotation=None,
                default=None,
                kind="pos",
                has_default=False,
            )
        ]
        init_stmts: list = []
        field_names: list[str] = []
        for fname, ann, default in fields:
            field_names.append(fname)
            init_args.append(
                Arg(
                    name=fname,
                    annotation=ann,
                    default=default,
                    kind="pos",
                    has_default=default is not None,
                )
            )
            init_stmts.append(
                Assign(
                    span=span,
                    targets=(
                        Attr(
                            span=span,
                            ty=DynType(name="dyn"),
                            obj=Name(span=span, ty=DynType(name="dyn"), ident="self"),
                            name=fname,
                        ),
                    ),
                    value=Name(span=span, ty=(ann or DynType(name="dyn")), ident=fname),
                    annotation=None,
                )
            )
        user_has_post_init = any(
            _is_ast_node(s, FuncDef) and s.name == "__post_init__"
            for s in remaining_body
        )
        inherited_post_init = False
        pending_post_init_bases = list(cd.bases)
        seen_post_init_bases = {}
        while pending_post_init_bases and not inherited_post_init:
            base_expr = pending_post_init_bases.pop()
            if not _is_ast_node(base_expr, Name):
                continue
            base_name = base_expr.ident
            if base_name in seen_post_init_bases:
                continue
            seen_post_init_bases[base_name] = True
            base_info = self.classes.get(base_name)
            if base_info is None or base_info.expanded_cd is None:
                continue
            for base_stmt in base_info.expanded_cd.body:
                if (
                    _is_ast_node(base_stmt, FuncDef)
                    and base_stmt.name == "__post_init__"
                ):
                    inherited_post_init = True
                    break
            if not inherited_post_init:
                pending_post_init_bases.extend(base_info.expanded_cd.bases)
        if user_has_post_init or inherited_post_init:
            init_stmts.append(
                ExprStmt(
                    span=span,
                    expr=Call(
                        span=span,
                        ty=DynType(name="dyn"),
                        func=Attr(
                            span=span,
                            ty=DynType(name="dyn"),
                            obj=Name(
                                span=span,
                                ty=DynType(name="dyn"),
                                ident="self",
                            ),
                            name="__post_init__",
                        ),
                        args=(),
                        kwargs=(),
                    ),
                )
            )
        if not init_stmts:
            init_stmts = [Pass(span=span)]

        synthetic_init = FuncDef(
            span=span,
            name="__init__",
            args=tuple(init_args),
            return_ty=NoneType(name="None"),
            body=tuple(init_stmts),
            decorators=(),
            is_method=True,
            is_async=False,
        )

        # Only add a synthetic __init__ if the user didn't define one
        # themselves.
        user_has_init = any(
            _is_ast_node(s, FuncDef) and s.name == "__init__" for s in remaining_body
        )
        new_body = list(remaining_body)
        if not user_has_init:
            new_body.insert(0, synthetic_init)
        existing_methods = {s.name for s in new_body if _is_ast_node(s, FuncDef)}
        insert_at = 1 if not user_has_init else 0
        if "__repr__" not in existing_methods:
            new_body.insert(insert_at, self._dataclass_synthetic_repr(cd, field_names))
            insert_at += 1
        if "__eq__" not in existing_methods:
            new_body.insert(
                insert_at,
                self._dataclass_synthetic_compare(cd, field_names, "__eq__", "=="),
            )
            insert_at += 1
        if (
            bool(options.get("frozen", False))
            and "__hash__" not in existing_methods
        ):
            # frozen + (default) eq: CPython synthesizes __hash__ rather
            # than clearing it; see _dataclass_synthetic_hash.
            new_body.insert(
                insert_at,
                self._dataclass_synthetic_hash(cd, field_names),
            )
            insert_at += 1
        if bool(options.get("order", False)):
            for method_name, op in (
                ("__lt__", "<"),
                ("__le__", "<="),
                ("__gt__", ">"),
                ("__ge__", ">="),
            ):
                if method_name not in existing_methods:
                    new_body.insert(
                        insert_at,
                        self._dataclass_synthetic_compare(
                            cd,
                            field_names,
                            method_name,
                            op,
                        ),
                    )
                    insert_at += 1

        return ClassDef(
            span=cd.span,
            name=cd.name,
            bases=cd.bases,
            keywords=cd.keywords,
            decorators=(),
            body=tuple(new_body),
        )

    def _class_decorator_is_noop(self, dec: Expr) -> bool:
        dname = _simple_decorator_name(dec)
        return dname in (
            "runtime_checkable",
            "typing.runtime_checkable",
            "final",
            "typing.final",
            "valueclass",
            "pcc.valueclass",
            "set_module",
            "_set_module",
        )

    def _base_expr_matches_name(
        self,
        expr: Expr,
        names: tuple[str, ...],
        seen: set[str],
    ) -> bool:
        if _is_ast_node(expr, Name):
            if expr.ident in names:
                return True
            if expr.ident in seen:
                return False
            seen.add(expr.ident)
            info = self.classes.get(expr.ident)
            if info is None:
                return False
            for base in info.bases_ast:
                if self._base_expr_matches_name(base, names, seen):
                    return True
            return False
        if _is_ast_node(expr, Attr):
            return expr.name in names
        if _is_ast_node(expr, Subscript):
            return self._base_expr_matches_name(expr.obj, names, seen)
        return False

    def _class_has_base_named(
        self,
        cd: ClassDef,
        names: tuple[str, ...],
    ) -> bool:
        for base in cd.bases:
            if self._base_expr_matches_name(base, names, set()):
                return True
        return False

    def _class_keyword_is_noop(self, cd: ClassDef, key: str) -> bool:
        if key == "metaclass":
            return True
        if self._class_keyword_supported_by_metaclass(cd, key):
            return True
        if key in ("total", "closed", "extra_items"):
            return self._class_has_base_named(cd, ("TypedDict",))
        return False

    def _class_keyword_supported_by_metaclass(
        self,
        cd: ClassDef,
        key: str,
    ) -> bool:
        metaclass_name = self._resolve_metaclass_name_for_class(cd)
        if metaclass_name is None:
            return False
        meta_info = self.classes.get(metaclass_name)
        if meta_info is None:
            return False
        for method_name in ("__prepare__", "__new__"):
            if method_name not in meta_info.methods:
                continue
            fd = self._find_method_def(metaclass_name, method_name)
            if fd is not None and self._func_accepts_keyword(fd, key):
                return True
        return False

    def _func_accepts_keyword(self, fd: FuncDef, key: str) -> bool:
        for arg in fd.args:
            if arg.kind == "**kwargs":
                return True
            if arg.name == key and arg.kind != "pos_only":
                return True
        return False

    def _class_global_name(self, cname: str) -> str:
        mod = self.parent.ast_module.name or "mod"
        sanitised_mod = mod.replace(".", "_").replace("-", "_")
        return f".class.{sanitised_mod}.{cname}"

    def _top_level_function_exists(self, name: str) -> bool:
        for stmt in self.parent.ast_module.body:
            if _is_ast_node(stmt, FuncDef) and stmt.name == name:
                return True
        return False

    def _method_symbol(self, class_name: str, method_name: str) -> str:
        mod = self.parent.ast_module.name or "mod"
        sanitised_mod = mod.replace(".", "_").replace("-", "_")
        if self._top_level_function_exists(class_name + "_" + method_name):
            return f"user_{sanitised_mod}_{class_name}__method_{method_name}"
        return f"user_{sanitised_mod}_{class_name}_{method_name}"

    def _collect_fields_and_declare_methods(
        self, cd: ClassDef, info: ClassInfo
    ) -> None:
        """Scan the class body to find declared fields and methods."""
        enum_like = self._is_enum_like_class(cd)
        protocol_like = self._is_protocol_like_class(cd)
        next_enum_value = 1
        abstract_methods: list[str] = []
        for stmt in cd.body:
            if _is_ast_node(stmt, Pass):
                continue
            if _is_ast_node(stmt, Assign):
                alias_source = None
                property_fget_alias = False
                alias_targets: list[Name] = []
                if _is_ast_node(stmt.value, Name) and stmt.value.ident in info.methods:
                    alias_source = stmt.value.ident
                    for target in stmt.targets:
                        if _is_ast_node(target, Name):
                            alias_targets.append(target)
                        else:
                            alias_source = None
                            break
                elif (
                    _is_ast_node(stmt.value, Attr)
                    and stmt.value.name == "fget"
                    and _is_ast_node(stmt.value.obj, Name)
                    and info.method_kinds.get(stmt.value.obj.ident) == "property_getter"
                ):
                    alias_source = stmt.value.obj.ident
                    property_fget_alias = True
                    for target in stmt.targets:
                        if _is_ast_node(target, Name):
                            alias_targets.append(target)
                        else:
                            alias_source = None
                            break
                elif self._is_walrus_sentinel(stmt.value):
                    chain_target, final_value = stmt.value.args
                    if (
                        _is_ast_node(final_value, Name)
                        and final_value.ident in info.methods
                    ):
                        alias_source = final_value.ident
                        for target in stmt.targets:
                            if _is_ast_node(target, Name):
                                alias_targets.append(target)
                            else:
                                alias_source = None
                                break
                        if alias_source is not None:
                            alias_targets.extend(
                                self._collect_chained_assignment_targets(chain_target)
                            )
                if alias_source is not None:
                    for target in alias_targets:
                        self._declare_method_alias(info, target.ident, alias_source)
                        if property_fget_alias:
                            info.method_kinds[target.ident] = "instance"
                    continue
                # Class-level assignments are class attributes. Instance
                # fields come from dataclass expansion or ``self.x`` writes.
                for t in stmt.targets:
                    unpack_pairs = self._class_attr_unpack_pairs(t, stmt.value)
                    if unpack_pairs is not None:
                        for attr_name, attr_value in unpack_pairs:
                            self._declare_class_attr(info, attr_name, attr_value)
                        continue
                    if _is_ast_node(t, Name):
                        if (
                            _is_ast_node(stmt.value, Name)
                            and stmt.value.ident in info.methods
                        ):
                            src_name = stmt.value.ident
                            info.methods[t.ident] = info.methods[src_name]
                            src_ref = info.method_refs.get(src_name)
                            if src_ref is not None:
                                info.method_refs[t.ident] = src_ref
                            info.method_kinds[t.ident] = info.method_kinds.get(
                                src_name,
                                "instance",
                            )
                            for method_name, method_def in reversed(info.method_defs):
                                if method_name == src_name:
                                    info.method_defs.append((t.ident, method_def))
                                    break
                            continue
                        if t.ident == "__slots__":
                            self._declare_slots(info, stmt.value)
                        if enum_like and not t.ident.startswith("_"):
                            enum_value = self._enum_member_value(
                                stmt.value,
                                next_enum_value,
                            )
                            if enum_value is not None:
                                info.enum_members[t.ident] = enum_value
                                next_enum_value = max(
                                    next_enum_value,
                                    enum_value + 1,
                                )
                            elif _is_ast_node(stmt.value, StrLit):
                                info.enum_string_members[t.ident] = str(
                                    stmt.value.value
                                )
                        if (
                            protocol_like
                            and _classgen_annotation_or_none(stmt) is not None
                            and not t.ident.startswith("_")
                            and t.ident not in info.protocol_members
                        ):
                            info.protocol_members.append(t.ident)
                        self._declare_class_attr(info, t.ident, stmt.value)
                        if self._is_walrus_sentinel(stmt.value):
                            chain_target, final_value = stmt.value.args
                            for (
                                chained_name
                            ) in self._collect_chained_assignment_targets(chain_target):
                                self._declare_class_attr(
                                    info,
                                    chained_name.ident,
                                    final_value,
                                )
                continue
            if _is_ast_node(stmt, Delete):
                for target in stmt.targets:
                    if _is_ast_node(target, Name):
                        info.class_attrs.pop(target.ident, None)
                        info.class_attr_values.pop(target.ident, None)
                        self._remove_class_attr_initializers(info, target.ident)
                continue
            if _is_ast_node(stmt, FuncDef):
                self._declare_method(cd, stmt, info)
                if self._func_is_abstract(stmt):
                    abstract_methods.append(stmt.name)
                self._collect_method_instance_fields(cd, stmt, info)
                continue
            # Ignore anything else (nested classes etc.) until a later
            # phase picks them up.
            # Docstrings at the top of the body are expression statements
            # with a StrLit — fine to drop silently.

        if abstract_methods:
            elem_ty = StrType(name="str")
            abstract_expr = TupleExpr(
                span=cd.span,
                ty=TupleType(
                    name="tuple", elems=tuple(elem_ty for _ in abstract_methods)
                ),
                elems=tuple(
                    StrLit(span=cd.span, ty=elem_ty, value=name)
                    for name in abstract_methods
                ),
            )
            self._declare_class_attr(info, "__abstractmethods__", abstract_expr)

    def _collect_method_instance_fields(
        self,
        cd: ClassDef,
        fd: FuncDef,
        info: ClassInfo,
    ) -> None:
        """Discover ``self`` writes across control flow and unpack targets."""
        from ..pipeline_exports import instance_field_assignment_statements

        def add_target(target, value_expr) -> None:
            if _is_ast_node(target, (TupleExpr, ListExpr)):
                for elem in target.elems:
                    add_target(elem, value_expr)
                return
            if not (
                _is_ast_node(target, Attr)
                and _is_ast_node(target.obj, Name)
                and target.obj.ident == "self"
            ):
                return
            field_name = _mangle_private_name(cd.name, target.name)
            if field_name not in info.field_names:
                info.field_names.append(field_name)
            field_ty = getattr(target, "ty", None)
            if field_ty is None or _is_ast_node(field_ty, DynType):
                field_ty = getattr(value_expr, "ty", None)
            if field_ty is not None:
                info.field_types[field_name] = field_ty

        for stmt in instance_field_assignment_statements(fd.body):
            if _is_ast_node(stmt, Assign):
                for target in stmt.targets:
                    add_target(target, stmt.value)
                continue
            if _is_ast_node(stmt, AugAssign):
                add_target(stmt.target, stmt.value)

    def _is_enum_like_class(self, cd: ClassDef) -> bool:
        for base in cd.bases:
            if _is_ast_node(base, Name) and base.ident in ("Enum", "IntEnum"):
                return True
            if (
                _is_ast_node(base, Attr)
                and base.name in ("Enum", "IntEnum")
                and _is_ast_node(base.obj, Name)
                and self.parent._native_builtin_module_for_name(base.obj.ident)
                == "enum"
            ):
                return True
        return False

    def _is_protocol_like_class(self, cd: ClassDef) -> bool:
        for base in cd.bases:
            if _is_ast_node(base, Name) and base.ident == "Protocol":
                return True
        return False

    def _enum_member_value(self, value_expr: Expr, next_auto: int) -> Optional[int]:
        if _is_ast_node(value_expr, BoolLit):
            return 1 if value_expr.value else 0
        if _is_ast_node(value_expr, IntLit):
            return int(value_expr.value)
        if (
            _is_ast_node(value_expr, Call)
            and not value_expr.args
            and not value_expr.kwargs
        ):
            if _is_ast_node(value_expr.func, Name) and value_expr.func.ident == "auto":
                return next_auto
            if (
                _is_ast_node(value_expr.func, Attr)
                and value_expr.func.name == "auto"
                and _is_ast_node(value_expr.func.obj, Name)
                and self.parent._native_builtin_module_for_name(
                    value_expr.func.obj.ident
                )
                == "enum"
            ):
                return next_auto
        return None

    def _func_is_abstract(self, fd: FuncDef) -> bool:
        for dec in fd.decorators:
            dname = _simple_decorator_name(dec)
            if (
                _classgen_name_eq(dname, "abstractmethod")
                or _classgen_name_eq(dname, "abc.abstractmethod")
                or _classgen_name_eq(dname, "abstractclassmethod")
                or _classgen_name_eq(dname, "abc.abstractclassmethod")
                or _classgen_name_eq(dname, "abstractstaticmethod")
                or _classgen_name_eq(dname, "abc.abstractstaticmethod")
                or _classgen_name_eq(dname, "abstractproperty")
                or _classgen_name_eq(dname, "abc.abstractproperty")
            ):
                return True
        return False

    def _declare_metaclass_literal_attrs(
        self,
        cd: ClassDef,
        info: ClassInfo,
    ) -> None:
        metaclass_name = info.metaclass_name
        if metaclass_name is None:
            return
        for stmt in self.parent.ast_module.body:
            if not _is_ast_node(stmt, ClassDef) or stmt.name != metaclass_name:
                continue
            for body_stmt in stmt.body:
                if not _is_ast_node(body_stmt, FuncDef):
                    continue
                if body_stmt.name == "__prepare__":
                    self._declare_prepare_literal_namespace_attrs(info, body_stmt)
                    continue
                if body_stmt.name != "__new__":
                    continue
                for inner in body_stmt.body:
                    if not _is_ast_node(inner, Assign) or len(inner.targets) != 1:
                        continue
                    target = inner.targets[0]
                    if not (
                        _is_ast_node(target, Subscript)
                        and _is_ast_node(target.obj, Name)
                        and target.obj.ident == "ns"
                        and _is_ast_node(target.idx, StrLit)
                    ):
                        continue
                    if _is_ast_node(inner.value, (StrLit, IntLit, BoolLit, NoneLit)):
                        self._declare_class_attr(
                            info,
                            target.idx.value,
                            inner.value,
                        )
            return

    def _declare_prepare_literal_namespace_attrs(
        self,
        info: ClassInfo,
        fd: FuncDef,
    ) -> None:
        for stmt in fd.body:
            if not _is_ast_node(stmt, Return):
                continue
            value = stmt.value
            if not _is_ast_node(value, DictExpr):
                continue
            for key_expr, value_expr in value.pairs:
                if not _is_ast_node(key_expr, StrLit):
                    continue
                attr_name = key_expr.value
                if attr_name in info.methods or attr_name in info.class_attr_values:
                    continue
                if _is_ast_node(value_expr, Name):
                    fn = self.parent.functions.get(value_expr.ident)
                    if fn is not None:
                        info.methods[attr_name] = fn
                        info.method_refs[attr_name] = ir.Value(
                            fn.type,
                            "@" + str(fn.name),
                        )
                        info.method_kinds[attr_name] = "instance"
                        continue
                if _is_ast_node(value_expr, (StrLit, IntLit, BoolLit, NoneLit)):
                    self._declare_class_attr(info, attr_name, value_expr)
            return

    def _resolve_preceding_class_literal_defaults(
        self,
        fd: FuncDef,
        info: ClassInfo,
    ) -> FuncDef:
        """Snapshot safe class-body constants used by method defaults.

        Python evaluates a method default while executing the surrounding
        class body, so a bare name may resolve to an earlier class attribute.
        Class lowering currently creates method signature objects before it
        initializes class attributes. Preserve the definition-point value for
        immutable literals without re-evaluating mutable class values or
        consulting a later class-body rebind.
        """
        resolved_args: list[Arg] = []
        changed = False
        for arg in fd.args:
            default = arg.default
            if _is_ast_node(default, Name):
                class_attr_name = self.mangle_private_attr_name(
                    info,
                    default.ident,
                )
                class_value = info.class_attr_values.get(class_attr_name)
                if _is_ast_node(
                    class_value,
                    (IntLit, FloatLit, StrLit, BoolLit, NoneLit),
                ):
                    default = class_value
                    changed = True
            resolved_args.append(
                Arg(
                    name=arg.name,
                    annotation=arg.annotation,
                    default=default,
                    kind=arg.kind,
                    has_default=arg.has_default,
                )
            )
        if not changed:
            return fd
        return FuncDef(
            span=fd.span,
            name=fd.name,
            args=tuple(resolved_args),
            return_ty=fd.return_ty,
            body=fd.body,
            decorators=fd.decorators,
            is_method=fd.is_method,
            is_async=fd.is_async,
        )

    def _declare_method(self, cd: ClassDef, fd: FuncDef, info: ClassInfo) -> None:
        """Declare one method function. Body is emitted separately."""
        fd = self._resolve_preceding_class_literal_defaults(fd, info)
        kind = "instance"
        for dec in fd.decorators:
            dname = _simple_decorator_name(dec)
            if _classgen_name_eq(dname, "staticmethod"):
                kind = "static"
            elif _classgen_name_eq(dname, "classmethod"):
                kind = "classmethod"
            elif self.parent._native_builtin_value_kind_for_expr(dec) == "pcc.virtual_thread.continuation_factory":
                continue
            elif (
                _classgen_name_eq(dname, "property")
                or _classgen_name_eq(dname, "cached_property")
                or _classgen_name_eq(dname, "functools.cached_property")
            ):
                kind = "property_getter"
            elif _classgen_name_endswith(dname, ".setter"):
                # ``@<name>.setter`` — Phase-3 follow-up: pair with the
                # existing property entry.
                kind = "property_setter"
            elif _classgen_name_endswith(dname, ".deleter"):
                kind = "property_deleter"
            elif (
                _classgen_name_eq(dname, "abstractmethod")
                or _classgen_name_eq(dname, "abc.abstractmethod")
                or _classgen_name_eq(dname, "abstractclassmethod")
                or _classgen_name_eq(dname, "abc.abstractclassmethod")
                or _classgen_name_eq(dname, "abstractstaticmethod")
                or _classgen_name_eq(dname, "abc.abstractstaticmethod")
                or _classgen_name_eq(dname, "abstractproperty")
                or _classgen_name_eq(dname, "abc.abstractproperty")
            ):
                # pcc doesn't enforce abstractness at compile time;
                # treat as a no-op decorator so the method is declared
                # as a regular instance method and the class lowering
                # proceeds. Runtime instantiation of a class with an
                # unimplemented abstract method will happily run the
                # (usually ``raise NotImplementedError``) body — pcc
                # matches Python's non-strict behaviour here.
                continue
            elif _classgen_name_eq(dname, "errstate") or _classgen_name_endswith(
                dname,
                ".errstate",
            ):
                # Warning/numeric-state decorators are metadata for pcc's
                # closed-world no-libpython path. They do not alter the native
                # method's call shape.
                continue
            elif (
                _classgen_name_eq(dname, "cache")
                or _classgen_name_eq(dname, "functools.cache")
                or _classgen_name_eq(dname, "lru_cache")
                or _classgen_name_eq(dname, "functools.lru_cache")
                or _classgen_name_endswith(dname, ".cache")
                or _classgen_name_endswith(dname, ".cached")
                or (
                    dname is not None
                    and "Cache" in str(dname)
                    and _classgen_name_endswith(dname, ".me")
                )
            ):
                # Memoization decorators preserve the method ABI. pcc can
                # safely compile the uncached body when the cache wrapper is
                # not needed for correctness.
                continue
            elif _is_ast_node(dec, Call) and (
                _classgen_name_eq(_simple_decorator_name(dec.func), "TOKEN")
                or _classgen_name_eq(_simple_decorator_name(dec.func), "lex.TOKEN")
            ):
                # PLY regex decorators only annotate token regexes for
                # runtime reflection. They do not change the method's
                # call shape, so a compile-time no-op is sufficient.
                continue
            else:
                raise NotImplementedError(
                    f"Layer 1 does not handle decorator {dname!r} on method "
                    f"{cd.name}.{fd.name}"
                )
        # For @<name>.setter / @<name>.deleter we keep the getter's
        # "property_getter" entry intact — mutators are looked up
        # separately by scanning the AST decorators at emit time.
        if self.parent._funcdef_is_continuation_factory(fd):
            if kind != "instance" or (fd.name.startswith("__") and fd.name.endswith("__")):
                raise L1CodegenError("continuation_factory requires an ordinary instance method")
            if not isinstance(fd.return_ty, DynType):
                raise L1CodegenError("continuation_factory requires an inferred or Any return type")
        if kind != "property_setter" and kind != "property_deleter":
            info.method_kinds[fd.name] = kind

        box_int_abi = self.parent._should_box_python_ints()
        if kind == "static":
            # No receiver prepended. All params are declared-only.
            decl_args = fd.args
        elif not fd.args:
            raise NotImplementedError(
                f"method {cd.name}.{fd.name} must have at least one argument "
                "(the 'self' receiver)"
            )
        else:
            decl_args = fd.args

        # Build LLVM signature: static methods omit the receiver, all
        # other kinds carry an i8* (PyInstance / PyClass pointer) as the
        # first param. Subsequent params go through the parent's type
        # logic.
        if kind == "static":
            param_types: list[ir.Type] = []
            iter_args = decl_args
        else:
            receiver_ty = _PTR
            if info.valueclass and decl_args:
                payload_ty = _classgen_valueclass_payload_ir_type(
                    decl_args[0].annotation
                )
                if payload_ty is not None:
                    receiver_ty = payload_ty
            param_types = [receiver_ty]
            iter_args = decl_args[1:]
        for arg in iter_args:
            # Bare ``*`` separator: no runtime slot — matches the
            # top-level ``_declare_user_function`` filter so the
            # generated method signature matches what the caller +
            # ``_resolve_call_kwargs`` produce.
            if arg.name == "":
                continue
            ir_ty, _ = self.parent._param_ir_and_bind_type(
                arg,
                require_annotation=False,
                owner_name=f"{cd.name}.{fd.name}",
                box_int_params=box_int_abi,
            )
            param_types.append(ir_ty)

        is_generator = (
            self.parent._funcdef_has_yield_sentinel(fd)
            or cd.name + "." + fd.name
            in getattr(self.parent, "_vthread_may_park_method_keys", set())
        )
        if is_generator:
            ret_ty = _PTR
        elif (
            _classgen_type_name(fd.return_ty) in ("None", "NoneType")
            or _is_ast_node(fd.return_ty, NoneType)
        ):
            ret_ty = _VOID
        elif box_int_abi and _is_ast_node(fd.return_ty, IntType):
            ret_ty = _PTR
        else:
            ret_ty = self.parent._map_type(fd.return_ty)
        if fd.is_async and isinstance(ret_ty, ir.VoidType):
            ret_ty = _PTR

        fnty = ir.FunctionType(ret_ty, param_types, var_arg=False)
        sym = self._method_symbol(cd.name, fd.name)
        if kind == "property_setter":
            sym = sym + ".setter"
        if kind == "property_deleter":
            sym = sym + ".deleter"
        existing = self.parent.module.globals.get(sym)
        if isinstance(existing, ir.Function):
            fn = existing
        else:
            fn = ir.Function(self.parent.module, fnty, name=sym)
            fn.linkage = "external"
        runtime_decl_args = [a for a in decl_args if a.name != ""]
        if kind == "static":
            for ir_arg, ast_arg in zip(fn.args, runtime_decl_args):
                ir_arg.name = ast_arg.name
        else:
            fn.args[0].name = runtime_decl_args[0].name  # "self" or "cls"
            for ir_arg, ast_arg in zip(fn.args[1:], runtime_decl_args[1:]):
                ir_arg.name = ast_arg.name
        # Methods named via ``@<name>.setter`` / ``@<name>.deleter``
        # re-declare a previously emitted FuncDef — their ``fd.name`` is
        # the property name, so we register them separately rather than
        # overwriting the getter in ``info.methods``.
        if kind == "property_setter":
            info.property_setters[fd.name] = fn
        elif kind == "property_deleter":
            info.property_deleters[fd.name] = fn
        else:
            info.method_defs.append((fd.name, fd))
            if fd.name == "__init__":
                info.init_fn = fn
                info.init_param_types = list(param_types)
                info.init_returns_void = _classgen_ir_type_is_void(ret_ty)
            info.methods[fd.name] = fn
            info.method_refs[fd.name] = ir.Value(fn.type, "@" + sym)
            if kind == "property_getter":
                info.properties[fd.name] = fn

    def _declare_method_alias(
        self,
        info: ClassInfo,
        alias_name: str,
        src_name: str,
    ) -> None:
        info.methods[alias_name] = info.methods[src_name]
        src_ref = info.method_refs.get(src_name)
        if src_ref is not None:
            info.method_refs[alias_name] = src_ref
        info.method_kinds[alias_name] = info.method_kinds.get(src_name, "instance")
        for method_name, method_def in reversed(info.method_defs):
            if method_name == src_name:
                info.method_defs.append((alias_name, method_def))
                break

    def _class_attr_global_name(self, class_name: str, attr_name: str) -> str:
        mod = self.parent.ast_module.name or "mod"
        sanitised_mod = mod.replace(".", "_").replace("-", "_")
        return f".classattr.{sanitised_mod}.{class_name}.{attr_name}"

    def mangle_private_attr_name(self, info: ClassInfo, attr_name: str) -> str:
        """Return the lexical Python spelling for a class-private attribute."""
        return _mangle_private_name(info.name, attr_name)

    def _class_attr_unpack_pairs(
        self,
        target: Expr,
        value_expr: Expr,
    ) -> Optional[list[tuple[str, Expr]]]:
        if not (
            _is_ast_node(target, TupleExpr) and _is_ast_node(value_expr, TupleExpr)
        ):
            return None
        target_elems = target.elems
        value_elems = value_expr.elems
        if len(target_elems) != len(value_elems):
            return None
        pairs: list[tuple[str, Expr]] = []
        for target_elem, value_elem in zip(target_elems, value_elems):
            if _is_ast_node(target_elem, Name):
                pairs.append((target_elem.ident, value_elem))
                continue
            nested = self._class_attr_unpack_pairs(target_elem, value_elem)
            if nested is None:
                return None
            pairs.extend(nested)
        return pairs

    def _declare_class_attr(
        self,
        info: ClassInfo,
        attr_name: str,
        value_expr: Expr,
    ) -> None:
        attr_name = self.mangle_private_attr_name(info, attr_name)
        info.class_attr_initializers.append((attr_name, value_expr))
        if attr_name in info.class_attrs:
            info.class_attr_values[attr_name] = value_expr
            return
        g_name = self._class_attr_global_name(info.name, attr_name)
        existing = self.parent.module.globals.get(g_name)
        if isinstance(existing, ir.GlobalVariable):
            gv = existing
        else:
            gv = ir.GlobalVariable(self.parent.module, _PTR, name=g_name)
            gv.linkage = "internal"
            gv.initializer = ir.Constant(_PTR, None)
        info.class_attrs[attr_name] = (gv, value_expr.ty)
        info.class_attr_values[attr_name] = value_expr

    def _remove_class_attr_initializers(
        self,
        info: ClassInfo,
        attr_name: str,
    ) -> None:
        attr_name = self.mangle_private_attr_name(info, attr_name)
        kept: list[tuple[str, Expr]] = []
        for name, expr in info.class_attr_initializers:
            if name != attr_name:
                kept.append((name, expr))
        info.class_attr_initializers = kept

    def _is_walrus_sentinel(self, expr: Expr) -> bool:
        return (
            _is_ast_node(expr, Call)
            and _is_ast_node(expr.func, Name)
            and expr.func.ident in ("_walrus", "__walrus__")
            and len(expr.args) == 2
        )

    def _collect_chained_assignment_targets(self, expr: Expr) -> list[Name]:
        """Return Name targets encoded in py_lift's chained-assign sentinel."""
        if _is_ast_node(expr, Name):
            return [cast(Name, expr)]
        if self._is_walrus_sentinel(expr):
            out: list[Name] = []
            for arg in expr.args:
                out.extend(self._collect_chained_assignment_targets(arg))
            return out
        return []

    def _declare_slots(self, info: ClassInfo, value_expr: Expr) -> None:
        slot_names = _slot_names_from_expr(value_expr)
        if slot_names is None:
            raise NotImplementedError(
                f"Layer 1 only supports literal string/list/tuple __slots__ "
                f"on class {info.name!r}"
            )
        info.slots_only = "__dict__" not in slot_names
        for slot_name in slot_names:
            if slot_name in ("__dict__", "__weakref__"):
                continue
            field_name = _mangle_private_name(info.name, slot_name)
            if field_name not in info.field_names:
                info.field_names.append(field_name)

    # ------------------------------------------------------ extern class

    def declare_extern_class(
        self,
        owning_module: str,
        class_name: str,
        field_names: tuple,
        methods: tuple,
        local_name: str = None,
        field_types: tuple = (),
    ) -> ClassInfo:
        """Declare a class imported from a sibling multi-file module.

        Creates an ``external`` class global and declares each method
        function with a matching signature. Registers the resulting
        :class:`ClassInfo` under ``local_name`` (or ``class_name``)
        in ``self.classes`` so ``MyClass(args)`` / ``obj.method()``
        dispatch through the normal pcc-native path.

        ``methods`` is a tuple of dicts with keys ``name``, ``kind``,
        ``param_types`` (tuple of ``Type`` including a leading
        placeholder for self/cls when kind != 'static'), ``return_ty``.
        """
        local = local_name or class_name
        sanitised_mod = owning_module.replace(".", "_").replace("-", "_")
        g_name = f".class.{sanitised_mod}.{class_name}"
        qualified = f"{owning_module}.{class_name}"

        # Extern declarations can be created before type-driven registration
        # reaches ``_ensure_class_type_registered``.  Recover the base graph
        # from the same closed-world export table here so an early declaration
        # does not permanently look like a root class.  That graph is needed
        # both for inherited field layout and for deciding whether a hinted
        # receiver can dispatch directly without skipping a subclass override.
        extern_bases_ast = ()
        native_table = getattr(self.parent, "_native_module_exports", None)
        if isinstance(native_table, dict):
            module_exports = native_table.get(owning_module)
            if isinstance(module_exports, dict):
                class_export = module_exports.get(class_name)
                if (
                    isinstance(class_export, dict)
                    and class_export.get("kind") == "class"
                ):
                    raw_base_names = class_export.get("base_names", ())
                    if isinstance(raw_base_names, (tuple, list)):
                        base_nodes = []
                        stub_span = SourceSpan(
                            file="<extern>",
                            line=0,
                            col=0,
                            end_line=0,
                            end_col=0,
                        )
                        for base_name in raw_base_names:
                            if base_name == "object":
                                continue
                            base_nodes.append(
                                Name(
                                    span=stub_span,
                                    ty=DynType(name="dyn"),
                                    ident=base_name,
                                )
                            )
                        extern_bases_ast = tuple(base_nodes)

        existing = self.classes.get(local)
        if existing is not None and existing.global_var.name == g_name:
            # Already declared correctly — return existing.
            if not existing.bases_ast and extern_bases_ast:
                existing.bases_ast = extern_bases_ast
            return existing
        qualified_existing = self.classes.get(qualified)
        if qualified_existing is not None:
            if not qualified_existing.bases_ast and extern_bases_ast:
                qualified_existing.bases_ast = extern_bases_ast
            if existing is None:
                self.classes[local] = qualified_existing
            return qualified_existing

        mod_module = self.parent.module
        existing_g = mod_module.globals.get(g_name)
        if isinstance(existing_g, ir.GlobalVariable):
            gv = existing_g
        else:
            gv = ir.GlobalVariable(mod_module, _PTR, name=g_name)
            gv.linkage = "external"

        # Collision check: if the short name is taken by a different class,
        # or if we are forced by local_name, use a qualified primary key.
        primary_key = local
        if existing is not None and existing.global_var.name != g_name:
            primary_key = f"{owning_module}.{class_name}"

        # Check if already declared under the qualified name.
        if primary_key in self.classes:
            primary_existing = self.classes[primary_key]
            if not primary_existing.bases_ast and extern_bases_ast:
                primary_existing.bases_ast = extern_bases_ast
            return primary_existing

        effective_field_names, synth_defs, method_plans = _extern_class_decl_plan(
            owning_module,
            class_name,
            field_names,
            methods,
        )

        info = ClassInfo(
            name=primary_key,
            global_var=gv,
            bases_ast=extern_bases_ast,
        )
        info.owning_module = owning_module
        info.export_class_name = class_name
        info.field_names = list(effective_field_names)
        if not isinstance(field_types, tuple):
            field_types = ()
        for field_entry in field_types:
            try:
                field_name, raw_field_ty = field_entry
            except Exception:
                continue
            field_ty = (
                raw_field_ty
                if _is_ast_node(raw_field_ty, Type)
                else decode_type(raw_field_ty)
            )
            if field_ty is not None:
                info.field_types[str(field_name)] = field_ty
        self.classes[primary_key] = info

        # Register qualified name as a secondary key if not already primary.
        if qualified != primary_key:
            self.classes[qualified] = info
        info.extern_method_defs = synth_defs
        if primary_key == local or local not in self.classes:
            self.classes[local] = info

        # Indexed access instead of a wide for-target unpack: 7-element
        # tuple unpacking in a for header is a known self-host hazard
        # (pcc1 tuple-unpack investigations).
        for method_plan in method_plans:
            mname = method_plan[0]
            kind = method_plan[1]
            raw_box_int_abi = method_plan[2]
            decoded_param_types = method_plan[3]
            ret = method_plan[4]
            sym = method_plan[5]
            plan_returns_none = method_plan[6]
            plan_may_park = bool(method_plan[7])
            info.method_kinds[mname] = kind
            if plan_may_park:
                info.may_park_methods.add(mname)
            box_int_abi = (
                self.parent._should_box_python_ints()
                if raw_box_int_abi is None
                else bool(raw_box_int_abi)
            )
            if kind == "static":
                param_types = [
                    self.parent._abi_ir_type(
                        t,
                        box_int_abi=box_int_abi,
                    )
                    for t in decoded_param_types
                ]
            else:
                receiver_ty = _PTR
                if decoded_param_types:
                    decoded_receiver_ty = decoded_param_types[0]
                    if (
                        _is_ast_node(decoded_receiver_ty, ClassType)
                        and decoded_receiver_ty.valueclass
                    ):
                        info.valueclass = True
                        payload_ty = _classgen_valueclass_payload_ir_type(
                            decoded_receiver_ty
                        )
                        if payload_ty is not None:
                            receiver_ty = payload_ty
                param_types = [receiver_ty] + [
                    self.parent._abi_ir_type(
                        t,
                        box_int_abi=box_int_abi,
                    )
                    for t in decoded_param_types[1:]
                ]
            # Mirror declare_method's return lowering exactly: a ``-> None``
            # (or unannotated) definition emits ``ret void``, so the
            # cross-module declaration must be void too — declaring it as
            # PyObject* made callers root leftover x0 (cross-module
            # return-ABI drift; see
            # docs/investigations/libpy-runtime-pcc-archive-pure-c-chain-crashes.md).
            # ``plan_returns_none`` is a schema-carried bool because type
            # descriptors degrade to dyn under the self-hosted compiler.
            method_is_async = bool(
                mname in synth_defs and synth_defs[mname].is_async
            )
            if plan_may_park:
                ret_ir = _PTR
            elif plan_returns_none and method_is_async:
                ret_ir = _PTR
            elif plan_returns_none:
                ret_ir = _VOID
            else:
                ret_ir = self.parent._abi_ir_type(ret, box_int_abi=box_int_abi)
            fnty = ir.FunctionType(ret_ir, param_types, var_arg=False)
            existing = mod_module.globals.get(sym)
            if isinstance(existing, ir.Function):
                fn = existing
            else:
                fn = ir.Function(mod_module, fnty, name=sym)
                fn.linkage = "external"
            info.methods[mname] = fn
            info.method_refs[mname] = ir.Value(fn.type, "@" + sym)
            if mname == "__init__":
                info.init_fn = fn
                info.init_param_types = list(param_types)
                info.init_returns_void = _classgen_ir_type_is_void(ret_ir)
            if kind == "property_getter":
                info.properties[mname] = fn
        return info

    # ------------------------------------------------------ method bodies

    def emit_methods(self, cd: ClassDef) -> None:
        """Second-pass: emit each method's body."""
        _classgen_log(self.parent, "methods begin " + cd.name)
        info = self.classes[cd.name]
        # If we rewrote the class (e.g. @dataclass), iterate over the
        # expanded body so synthetic methods get their bodies emitted.
        if info.expanded_cd is not None:
            cd = info.expanded_cd
        stmt_index = 0
        for stmt in cd.body:
            if _is_ast_node(stmt, FuncDef):
                _classgen_log(
                    self.parent,
                    "method begin " + cd.name + "." + stmt.name + " " + str(stmt_index),
                )
                if _funcdef_is_property_setter(stmt):
                    fn = info.property_setters.get(stmt.name)
                elif _funcdef_is_property_deleter(stmt):
                    fn = info.property_deleters.get(stmt.name)
                else:
                    fn = info.methods.get(stmt.name)
                if fn is None:
                    _classgen_log(
                        self.parent,
                        "method skipped "
                        + cd.name
                        + "."
                        + stmt.name
                        + " "
                        + str(stmt_index),
                    )
                    stmt_index += 1
                    continue
                try:
                    self._emit_method_body(cd, stmt, fn, info)
                except BaseException:
                    _classgen_log(self.parent, "method error")
                    raise
                _classgen_log(
                    self.parent,
                    "method end " + cd.name + "." + stmt.name + " " + str(stmt_index),
                )
            stmt_index += 1
        _classgen_log(self.parent, "methods end " + cd.name)

    def _emit_method_body(
        self,
        cd: ClassDef,
        fd: FuncDef,
        fn: ir.Function,
        info: ClassInfo,
    ) -> None:
        """Lower a method body. Reuses the parent codegen's statement
        machinery by temporarily rebinding its state.
        """
        if len(getattr(fn, "blocks", ())) > 0:
            return
        parent = self.parent
        # Preserve outer state — we may be called mid-module-init
        # bookkeeping and we must not clobber a function-in-progress.
        saved_builder = parent.builder
        saved_fn = parent.current_function
        saved_fd = parent.current_func_def
        saved_entry_block = getattr(parent, "_current_entry_block", None)
        saved_env = parent.env
        saved_env_class_hint = parent.env_class_hint
        saved_env_class_object_hint = parent.env_class_object_hint
        saved_env_list_elem_class_hint = parent.env_list_elem_class_hint
        saved_loops = parent.loop_stack
        saved_box_int_locals = parent._box_int_locals
        saved_exact_int_flags = parent._exact_int_env_flags
        saved_planned_exact_int_local_names = (
            parent._planned_exact_int_local_names
        )
        saved_ir_builder_flags = getattr(parent, "_ir_builder_env_flags", {})
        saved_class = getattr(parent, "current_class", None)
        saved_global_names = getattr(parent, "_current_global_names", set())
        saved_async_body_depth = getattr(parent, "_async_body_depth", 0)
        saved_kind = getattr(parent, "current_method_kind", None)
        saved_owned_local_names = parent._owned_local_names
        saved_owned_local_has_value = parent._owned_local_has_value
        saved_owned_local_flag_slots = parent._owned_local_flag_slots
        saved_owned_local_flag_allocas = getattr(
            parent,
            "_owned_local_flag_allocas",
            {},
        )
        saved_for_target_owned_names = getattr(
            parent,
            "_for_target_owned_names",
            set(),
        )
        saved_gc_rooted_local_names = parent._gc_rooted_local_names
        saved_gc_rooted_local_order = getattr(parent, "_gc_rooted_local_order", [])
        saved_borrowed_gc_rooted_local_names = getattr(
            parent,
            "_borrowed_gc_rooted_local_names",
            set(),
        )
        saved_pinned_gc_rooted_local_names = parent._pinned_gc_rooted_local_names
        saved_active_handler_excs = parent._active_handler_excs
        # Snapshot the user-function name table — a method body may
        # encounter ``from .sibling import name`` (handled by
        # ``_emit_import_from`` → ``_bind_native_cross_module_imports``)
        # which sets ``self.functions[name] = sibling_extern_fn``.  Without
        # restoring on exit, a later top-level ``def name(...)`` reads back
        # the polluted entry, attaches its body to the sibling extern, and
        # the local symbol stays declared-but-undefined → link error
        # ``Undefined symbols: _user_<this_module>_<name>``.  See
        # docs/investigations/python-class-method-body-from-import-leaks-functions-binding.md.
        saved_functions = dict(parent.functions)
        saved_container_temp_root_slot_names = getattr(
            parent,
            "_container_temp_root_slot_names",
            [],
        )
        saved_current_param_names = getattr(parent, "_current_param_names", set())
        saved_lambda_lexical_shadow_names = getattr(
            parent,
            "_lambda_lexical_shadow_names",
            set(),
        )
        if bool(getattr(parent, "_codegen_trace_enabled", False)):
            saved_trace_stmt_index = parent._codegen_current_stmt_index
            saved_trace_stmt_kind = parent._codegen_current_stmt_kind
            saved_trace_expr_kind = parent._codegen_current_expr_kind
            parent._codegen_current_stmt_index = -1
            parent._codegen_current_stmt_kind = ""
            parent._codegen_current_expr_kind = ""
            if getattr(parent, "_codegen_trace_set_stmt_context", None) is not None:
                parent._codegen_trace_set_stmt_context(-1, "")
            if getattr(parent, "_codegen_trace_push", None) is not None:
                parent._codegen_trace_push(
                    "function",
                    -1,
                    "",
                    "",
                    parent._codegen_trace_span(fd),
                )
        else:
            saved_trace_stmt_index = -1
            saved_trace_stmt_kind = ""
            saved_trace_expr_kind = ""
        kind = info.method_kinds.get(fd.name, "instance")

        try:
            if not parent._funcdef_is_continuation_factory(fd) and (
                parent._funcdef_has_yield_sentinel(fd)
                or cd.name + "." + fd.name
                in getattr(parent, "_vthread_may_park_method_keys", set())
            ):
                parent._emit_generator_wrapper_function(
                    fd,
                    fn,
                    self._method_symbol(cd.name, fd.name),
                    info,
                    kind,
                )
                return

            # Pick an entry-block name that doesn't collide with any
            # parameter. ``entry`` is the default, but methods like
            # ``__init__(self, entry, ...)`` would trip LLVM's label-vs-
            # SSA-name shared namespace.
            param_names = {a.name for a in fd.args if a.name != ""}
            entry_label = "entry"
            while entry_label in param_names:
                entry_label = entry_label + "_"
            entry = fn.append_basic_block(name=entry_label)
            parent.builder = ir.IRBuilder(entry)
            parent.current_function = fn
            parent.current_func_def = fd
            # Target-language handlers are scoped to this method.  Reset the
            # compiler's active-handler IR stack at the method boundary so a
            # value from another generated function cannot cross into ``fn``.
            parent._active_handler_excs = []
            parent._current_entry_block = entry
            parent._current_global_names = parent._collect_explicit_global_names(
                fd.body
            )
            forced_exact_int_names = forced_exact_int_local_names(
                parent,
                fd,
                parent._current_global_names,
            )
            parent.env = {}
            parent.env_class_hint = {}
            parent.env_class_object_hint = {}
            parent.env_list_elem_class_hint = {}
            parent._ir_builder_env_flags = {}
            parent.loop_stack = []
            box_int_abi = parent._should_box_python_ints()
            parent._box_int_locals = box_int_abi
            parent._owned_local_names = set()
            parent._owned_local_has_value = set()
            parent._owned_local_flag_slots = {}
            parent._owned_local_flag_allocas = {}
            parent._for_target_owned_names = set()
            parent._gc_rooted_local_names = set()
            parent._gc_rooted_local_order = []
            parent._borrowed_gc_rooted_local_names = set()
            parent._pinned_gc_rooted_local_names = set()
            parent._container_temp_root_slot_names = []
            parent._exact_int_env_flags = {
                name: True for name in forced_exact_int_names
            }
            parent._planned_exact_int_local_names = set(forced_exact_int_names)
            parent._async_body_depth = saved_async_body_depth + (
                1 if fd.is_async else 0
            )
            parent.current_class = info  # type: ignore[attr-defined]
            parent.current_method_kind = kind  # type: ignore[attr-defined]
            # Filter the bare ``*`` kw-only separator — it has no IR slot.
            runtime_args = [a for a in fd.args if a.name != ""]
            boxed_param_names = set(
                getattr(parent, "_closure_boxed_params", {}).get(
                    f"{cd.name}.{fd.name}",
                    getattr(parent, "_closure_boxed_params", {}).get(fd.name, ()),
                )
            )
            auto_root_borrowed_params = not getattr(
                parent,
                "_suppress_implicit_gc_roots",
                False,
            )

            def bind_method_arg(
                ir_arg: ir.Value,
                ast_arg: Arg,
                ir_ty: ir.Type,
                bind_ty: Type,
            ) -> bool:
                if ast_arg.name not in boxed_param_names:
                    return False
                cell = parent.builder.call(
                    parent.runtime["py_list_new"],
                    [ir.Constant(_I64, 0)],
                    name=parent._fresh(f"{ast_arg.name}.cell"),
                )
                initial = marshal.marshal_to_object(
                    parent.builder,
                    parent.module,
                    parent.runtime,
                    ir_arg,
                    bind_ty or DynType(name="dyn"),
                )
                parent.builder.call(
                    parent.runtime["py_list_append"],
                    [cell, initial],
                )
                slot = parent.builder.alloca(_CSTR, name=f"{ast_arg.name}.addr")
                parent.builder.store(cell, slot)
                parent.env[ast_arg.name] = (
                    slot,
                    _CSTR,
                    ListType(name="list", elem=DynType(name="dyn")),
                )
                if auto_root_borrowed_params:
                    parent._ensure_borrowed_local_gc_root(ast_arg.name, slot, _CSTR)
                return True

            if kind == "static":
                # No implicit receiver. Walk declared args directly.
                parent._current_param_names = {a.name for a in runtime_args}
                for arg_index, (ir_arg, ast_arg) in enumerate(
                    zip(fn.args, runtime_args)
                ):
                    ir_ty = _classgen_function_arg_type(fn, arg_index, ir_arg)
                    if ir_ty is None:
                        raise AttributeError("type")
                    _classgen_ensure_value_type(ir_arg, ir_ty)
                    _decl_ir_ty, bind_ty = parent._param_ir_and_bind_type(
                        ast_arg,
                        require_annotation=False,
                        owner_name=f"{cd.name}.{fd.name}",
                        box_int_params=box_int_abi,
                    )
                    if bind_method_arg(ir_arg, ast_arg, ir_ty, bind_ty):
                        continue
                    if bind_forced_exact_int_parameter(
                        parent,
                        ast_arg,
                        ir_arg,
                        ir_ty,
                        bind_ty,
                    ):
                        continue
                    slot = parent.builder.alloca(ir_ty, name=f"{ast_arg.name}.addr")
                    parent.builder.store(ir_arg, slot)
                    parent.env[ast_arg.name] = (slot, ir_ty, bind_ty)
                    if auto_root_borrowed_params and parent._is_object(bind_ty):
                        parent._ensure_borrowed_local_gc_root(
                            ast_arg.name,
                            slot,
                            ir_ty,
                        )
                    if (
                        auto_root_borrowed_params
                        and parent._is_valueclass_payload_type(bind_ty)
                    ):
                        parent._ensure_valueclass_payload_gc_roots(
                            ast_arg.name,
                            slot,
                            bind_ty,
                        )
            else:
                # First argument is the receiver (``self`` or ``cls``).
                recv_name = runtime_args[0].name if runtime_args else "self"
                parent._current_param_names = {a.name for a in runtime_args}
                recv_ir_ty = _PTR
                if info.valueclass and runtime_args:
                    payload_ty = _classgen_valueclass_payload_ir_type(
                        runtime_args[0].annotation
                    )
                    if payload_ty is not None:
                        recv_ir_ty = payload_ty
                recv_arg = fn.args[0]
                _classgen_ensure_value_type(recv_arg, recv_ir_ty)
                self_slot = parent.builder.alloca(recv_ir_ty, name=f"{recv_name}.addr")
                parent.builder.store(recv_arg, self_slot)
                # The receiver's bind type honours an explicit ``ClassType``
                # annotation when type-infer placed one on the first arg.
                # This wires through the multi-file ``derived_class_map``
                # propagation so a mixin method's ``self`` resolves against
                # the derived class's full schema rather than the bare
                # mixin's empty fields. Non-class annotations stay DynType
                # for back-compat with existing dispatch paths.
                recv_bind_ty: Type = DynType(name="dyn")
                if runtime_args and _is_ast_node(runtime_args[0].annotation, ClassType):
                    recv_bind_ty = runtime_args[0].annotation
                if runtime_args and bind_method_arg(
                    recv_arg,
                    runtime_args[0],
                    recv_ir_ty,
                    recv_bind_ty,
                ):
                    pass
                elif runtime_args and bind_forced_exact_int_parameter(
                    parent,
                    runtime_args[0],
                    recv_arg,
                    recv_ir_ty,
                    recv_bind_ty,
                ):
                    pass
                else:
                    parent.env[recv_name] = (self_slot, recv_ir_ty, recv_bind_ty)
                    if auto_root_borrowed_params and parent._is_object(recv_bind_ty):
                        parent._ensure_borrowed_local_gc_root(
                            recv_name,
                            self_slot,
                            recv_ir_ty,
                        )
                    if (
                        auto_root_borrowed_params
                        and parent._is_valueclass_payload_type(recv_bind_ty)
                    ):
                        parent._ensure_valueclass_payload_gc_roots(
                            recv_name,
                            self_slot,
                            recv_bind_ty,
                        )

                for offset, (ir_arg, ast_arg) in enumerate(
                    zip(fn.args[1:], runtime_args[1:])
                ):
                    arg_index = offset + 1
                    ir_ty = _classgen_function_arg_type(fn, arg_index, ir_arg)
                    if ir_ty is None:
                        raise AttributeError("type")
                    _classgen_ensure_value_type(ir_arg, ir_ty)
                    _decl_ir_ty, bind_ty = parent._param_ir_and_bind_type(
                        ast_arg,
                        require_annotation=False,
                        owner_name=f"{cd.name}.{fd.name}",
                        box_int_params=box_int_abi,
                    )
                    if bind_method_arg(ir_arg, ast_arg, ir_ty, bind_ty):
                        continue
                    if bind_forced_exact_int_parameter(
                        parent,
                        ast_arg,
                        ir_arg,
                        ir_ty,
                        bind_ty,
                    ):
                        continue
                    slot = parent.builder.alloca(ir_ty, name=f"{ast_arg.name}.addr")
                    parent.builder.store(ir_arg, slot)
                    parent.env[ast_arg.name] = (slot, ir_ty, bind_ty)
                    if auto_root_borrowed_params and parent._is_object(bind_ty):
                        parent._ensure_borrowed_local_gc_root(
                            ast_arg.name,
                            slot,
                            ir_ty,
                        )
                    if (
                        auto_root_borrowed_params
                        and parent._is_valueclass_payload_type(bind_ty)
                    ):
                        parent._ensure_valueclass_payload_gc_roots(
                            ast_arg.name,
                            slot,
                            bind_ty,
                        )

            allocate_forced_exact_int_locals(
                parent,
                forced_exact_int_names,
                parent._current_global_names,
            )
            parent._lambda_lexical_shadow_names = set(parent._current_param_names)
            parent._emit_thread_safepoint()

            # Emit statements via the parent's normal emitter. In debug mode,
            # mirror L1CodeGen._emit_user_function's direct statement tracing so
            # self-host crashes inside class methods identify the exact source
            # statement instead of only the enclosing method.
            if os.environ.get("PCC_DEBUG_CODEGEN_PHASES"):
                _classgen_log(
                    parent,
                    "method body len "
                    + cd.name
                    + "."
                    + fd.name
                    + " "
                    + str(len(fd.body)),
                )
                body_index = 0
                for raw_stmt in fd.body:
                    span = getattr(raw_stmt, "span", None)
                    loc = ""
                    if span is not None:
                        loc = ":" + str(span.line) + ":" + str(span.col)
                    _classgen_log(
                        parent,
                        "method raw "
                        + cd.name
                        + "."
                        + fd.name
                        + " "
                        + str(body_index)
                        + " "
                        + type(raw_stmt).__name__
                        + loc,
                    )
                    body_index += 1
                direct_index = 0
                for direct_stmt in fd.body:
                    if parent._builder_block_is_terminated():
                        _classgen_log(
                            parent,
                            "method stmt stop terminated "
                            + cd.name
                            + "."
                            + fd.name
                            + " "
                            + str(direct_index),
                        )
                        break
                    try:
                        parent._codegen_trace_set_stmt_context(
                            direct_index,
                            type(direct_stmt).__name__,
                        )
                    except Exception:
                        pass
                    span = getattr(direct_stmt, "span", None)
                    loc = ""
                    if span is not None:
                        loc = ":" + str(span.line) + ":" + str(span.col)
                    _classgen_log(
                        parent,
                        "method stmt begin "
                        + cd.name
                        + "."
                        + fd.name
                        + " "
                        + str(direct_index)
                        + " "
                        + type(direct_stmt).__name__
                        + loc,
                    )
                    parent._emit_stmt(direct_stmt)
                    _classgen_log(
                        parent,
                        "method stmt end "
                        + cd.name
                        + "."
                        + fd.name
                        + " "
                        + str(direct_index)
                        + " "
                        + type(direct_stmt).__name__,
                    )
                    direct_index += 1
            else:
                parent._emit_stmts(fd.body)

            # Default-return for missing terminator.
            if not parent._builder_block_is_terminated():
                if isinstance(fn.function_type.return_type, ir.VoidType):
                    parent._emit_owned_local_cleanup()
                    parent.builder.ret_void()
                elif isinstance(fn.function_type.return_type, ir.PointerType):
                    parent._emit_owned_local_cleanup()
                    none_gv = declare_runtime_global(parent.module, "py_None")
                    parent.builder.ret(parent.builder.load(none_gv))
                else:
                    parent._emit_owned_local_cleanup()
                    parent.builder.ret(parent._zero_of(fn.function_type.return_type))
        except BaseException as exc:
            parent._codegen_trace_dump(exc)
            raise
        finally:
            parent._strict_stub_user_function_with_cpy_fallback(fn, fd)
            parent.builder = saved_builder
            parent.current_function = saved_fn
            parent.current_func_def = saved_fd
            parent._current_entry_block = saved_entry_block
            parent._current_global_names = saved_global_names
            parent.env = saved_env
            parent.env_class_hint = saved_env_class_hint
            parent.env_class_object_hint = saved_env_class_object_hint
            parent.env_list_elem_class_hint = saved_env_list_elem_class_hint
            parent.loop_stack = saved_loops
            parent._box_int_locals = saved_box_int_locals
            parent._exact_int_env_flags = saved_exact_int_flags
            parent._planned_exact_int_local_names = (
                saved_planned_exact_int_local_names
            )
            parent._async_body_depth = saved_async_body_depth
            parent._ir_builder_env_flags = saved_ir_builder_flags
            parent.current_class = saved_class  # type: ignore[attr-defined]
            parent.current_method_kind = saved_kind  # type: ignore[attr-defined]
            parent._lambda_lexical_shadow_names = saved_lambda_lexical_shadow_names
            parent._owned_local_names = saved_owned_local_names
            parent._owned_local_has_value = saved_owned_local_has_value
            parent._owned_local_flag_slots = saved_owned_local_flag_slots
            parent._owned_local_flag_allocas = saved_owned_local_flag_allocas
            parent._for_target_owned_names = saved_for_target_owned_names
            parent._gc_rooted_local_names = saved_gc_rooted_local_names
            parent._gc_rooted_local_order = saved_gc_rooted_local_order
            parent._borrowed_gc_rooted_local_names = (
                saved_borrowed_gc_rooted_local_names
            )
            parent._pinned_gc_rooted_local_names = saved_pinned_gc_rooted_local_names
            parent._active_handler_excs = saved_active_handler_excs
            # Restore the saved function table; see saved_functions above.
            # Only undo OVERWRITES of pre-existing keys (the leak case).
            # New keys added during method emission (e.g. nested ``def``
            # helpers inside the method body that are legitimately exposed
            # to the rest of the module) must NOT be dropped — doing so
            # caused stage2 bootstrap to fail with
            # ``AttributeError: blocks`` when later code referenced those
            # missing entries.
            for _key, _val in saved_functions.items():
                parent.functions[_key] = _val
            parent._container_temp_root_slot_names = (
                saved_container_temp_root_slot_names
            )
            parent._current_param_names = saved_current_param_names
            if bool(getattr(parent, "_codegen_trace_enabled", False)):
                parent._codegen_current_stmt_index = saved_trace_stmt_index
                parent._codegen_current_stmt_kind = saved_trace_stmt_kind
                parent._codegen_current_expr_kind = saved_trace_expr_kind

    # ------------------------------------------------------ module init

    def emit_module_init(self) -> None:
        """Emit the one-shot ``_pcc_py_module_init`` function that
        populates every class global.

        The generated entrypoints call module-init explicitly in a
        deterministic order. Avoid also registering it as a global ctor:
        in multi-module executables ctor order is linker-defined, so a
        child class module can run before its base-class module and feed
        NULL bases into ``py_class_new`` / ``c3_linearize``.
        """
        if not self.classes:
            return  # nothing to do

        module = self.parent.module
        mod_name = self.parent.module.name or "mod"
        sanitised_mod = mod_name.replace(".", "_").replace("-", "_")
        fn_name = f"_pcc_py_module_init_{sanitised_mod}"
        existing = module.globals.get(fn_name)
        if isinstance(existing, ir.Function):
            # Already emitted — leave it.
            return
        fnty = ir.FunctionType(_VOID, [], var_arg=False)
        init_fn = ir.Function(module, fnty, name=fn_name)
        init_fn.linkage = "external"

        # Save parent state so we can re-use its builder abstraction.
        saved_builder = self.parent.builder
        saved_fn = self.parent.current_function
        saved_entry_block = getattr(self.parent, "_current_entry_block", None)
        entry = init_fn.append_basic_block(name="entry")
        self.parent.builder = ir.IRBuilder(entry)
        self.parent.current_function = init_fn
        self.parent._current_entry_block = entry

        try:
            # Emit per-class init in declaration order.
            for cd in self._iter_class_defs():
                if cd.name in getattr(
                    self.parent,
                    "_hoisted_class_capture_params",
                    {},
                ):
                    continue
                info = self.classes[cd.name]
                self._emit_class_init(cd, info)

            self.parent.builder.ret_void()
        finally:
            self.parent.builder = saved_builder
            self.parent.current_function = saved_fn
            self.parent._current_entry_block = saved_entry_block

    def emit_class_statement_init(self, cd: ClassDef) -> None:
        info = self.classes.get(cd.name)
        if info is None:
            return
        self._emit_class_init(cd, info)

    def emit_local_class_statement_init(self, cd: ClassDef) -> None:
        """Construct and bind a function-local class at its source position."""
        info = self.classes.get(cd.name)
        if info is None:
            raise L1CodegenError(
                "function-local class has no predeclared metadata: " + cd.name
            )
        cls_ptr = self._emit_class_init(cd, info, publish_global=False)
        if cls_ptr is None:
            return

        parent = self.parent
        slot = parent.env.get(cd.name)
        if slot is None:
            alloca = parent._alloca_in_entry(
                _PTR,
                name=cd.name + ".class.addr",
                init_null=True,
            )
            parent.env[cd.name] = (
                alloca,
                _PTR,
                ClassType(
                    name=cd.name,
                    module=parent.ast_module.name or "",
                ),
            )
        else:
            alloca, ir_ty, _declared_ty = slot
            if not _classgen_ir_type_is_pointer(ir_ty):
                raise L1CodegenError(
                    "function-local class binding has non-object storage: "
                    + cd.name
                )

        parent._ensure_owned_local_gc_root(cd.name, alloca, _PTR)
        owned_flag = parent._ensure_owned_local_flag(cd.name, alloca)
        parent.builder.call(parent.runtime["pcc_gc_pin"], [cls_ptr])
        parent.builder.call(
            parent.runtime["pcc_gc_store_root"],
            [parent._as_gc_ptr(alloca), cls_ptr],
        )
        parent.builder.call(
            parent.runtime["pcc_gc_unpin"],
            [parent._value_available_at_insertion_point(cls_ptr)],
        )
        # The root store retained the replacement and released any previous
        # binding.  Consume the fresh class-construction result.
        parent._gc_release(
            cls_ptr,
            parent._release_context_label("local-class:" + cd.name),
        )
        parent._owned_local_names.add(cd.name)
        parent._owned_local_has_value.add(cd.name)
        parent.builder.store(ir.Constant(_I1, 1), owned_flag)
        parent.env_class_object_hint[cd.name] = info.name

    def _iter_class_defs(self):
        """Return every top-level ``ClassDef`` in the module body."""
        out = []
        for stmt in self._class_defs:
            out.append(stmt)
        return out

    def _emit_metaclass_conflict_typeerror(self, cd: ClassDef) -> None:
        msg_ptr = self.parent._pooled_cstr_ptr("metaclass conflict", ".exc.msg")
        exc = self.parent.builder.call(
            self.parent.runtime["py_exc_new"],
            [ir.Constant(_I64, 3), msg_ptr],
            name=self._fresh("exc.TypeError"),
        )
        self.parent.builder.call(self.parent.runtime["py_raise"], [exc])
        frame_exc = self.parent.builder.call(
            self.parent.runtime["py_current_exception"],
            [],
            name=self._fresh("raise.frame.exc"),
        )
        self.parent._emit_exception_frame(frame_exc, cd.span)
        err_target = self.parent._current_try_err_block()
        if err_target is None:
            err_target = self.parent._ensure_fn_err_exit()
        self.parent.builder.branch(err_target)
        cont = self.parent.current_function.append_basic_block(
            name=self._fresh("metaclass.conflict.cont"),
        )
        self.parent.builder.position_at_end(cont)

    def _emit_class_init(
        self,
        cd: ClassDef,
        info: ClassInfo,
        *,
        publish_global: bool = True,
    ) -> Optional[ir.Value]:
        if info.metaclass_name == _METACLASS_CONFLICT:
            self._emit_metaclass_conflict_typeerror(cd)
            return None
        builder = self.parent.builder
        runtime = self.parent.runtime

        # 1. Class name C-string.
        name_ptr = self._cname_ptr(cd.name)

        # 2. Field names array — const char*[]. For n_fields == 0 pass NULL.
        field_names = _classgen_effective_field_names(info)
        n_fields = len(field_names)
        if n_fields == 0:
            field_arr = ir.Constant(_PTR, None)
        else:
            field_arr = self._field_names_global(tuple(field_names))

        # 3. Base classes array.
        base_values: list[ir.Value] = []
        for b in info.bases_ast:
            if not _is_ast_node(b, Name):
                # Foreign / qualified bases such as
                # ``ctypes.Structure`` are left out of the native
                # base array. The derived class still compiles and can
                # use attribute fallback paths, but native layout/MRO
                # does not attempt to model the external base.
                continue
            if b.ident == "object":
                # Skip — implicit object root is added by py_class_new
                # when n_bases == 0. A mixed "explicit object + other
                # bases" case would add object twice; unsupported here.
                continue
            base_info = self.classes.get(b.ident)
            if base_info is not None:
                base_values.append(
                    self._load_class_object(base_info, f".base.{b.ident}")
                )
                continue
            exc_tag = _builtin_exception_tag_for_base_name(b.ident)
            if exc_tag is not None:
                base_values.append(
                    builder.call(
                        runtime["py_exc_builtin_class"],
                        [ir.Constant(_I64, exc_tag)],
                        name=self._fresh(f".base.exc.{b.ident}"),
                    )
                )
                continue
            if base_info is None:
                # Foreign base class (imported from CPython-backed module
                # such as ``llvmlite.ir.ModulePass``). We cannot model its
                # layout on the pcc side, so skip it: the derived class
                # stays structurally compatible with the pcc runtime while
                # method calls through ``self`` fall through to the
                # CPython dispatch path.
                continue

        n_bases = len(base_values)
        if n_bases == 0:
            bases_ptr = ir.Constant(_PTR, None)
        else:
            bases_ptr = self._load_bases_array(cd.name, base_values)

        # 4. Create the class object. For class-header keyword arguments,
        # call the known metaclass hooks instead of dropping the keywords.
        # This keeps the focused pcc1 path aligned with CPython's
        # ``__prepare__(..., **kwargs)`` / ``__new__(..., **kwargs)``
        # propagation while leaving keyword-free static class lowering on
        # the historical fast path.
        bases_arg = bases_ptr
        if bases_arg.type != _PTR:
            bases_arg = builder.bitcast(bases_arg, _PTR, name=self._fresh(".bases.i8p"))
        field_arg = field_arr
        if field_arg.type != _PTR:
            field_arg = builder.bitcast(
                field_arg, _PTR, name=self._fresh(".fnames.i8p")
            )
        prepared_attr_objects: dict[str, ir.Value] = {}
        prepared = self._maybe_emit_metaclass_prepared_namespace_constructor(cd, info)
        if prepared is not None:
            cls_ptr, prepared_attr_objects = prepared
        else:
            generic_prepared = (
                self._maybe_emit_metaclass_generic_prepared_namespace_constructor(
                    cd,
                    info,
                )
            )
            if generic_prepared is not None:
                cls_ptr, prepared_attr_objects = generic_prepared
            else:
                cls_ptr = self._maybe_emit_metaclass_dynamic_constructor(cd, info)
            if cls_ptr is None:
                cls_ptr = self._maybe_emit_metaclass_static_constructor(cd, info)
            if cls_ptr is None:
                cls_ptr = self._maybe_emit_metaclass_inherited_constructor(cd, info)
            if cls_ptr is None:
                cls_ptr = self._maybe_emit_metaclass_keyword_constructor(cd, info)
        if cls_ptr is None:
            cls_ptr = builder.call(
                runtime["py_class_new"],
                [
                    name_ptr,
                    bases_arg,
                    ir.Constant(_I32, n_bases),
                    field_arg,
                    ir.Constant(_I32, n_fields),
                ],
                name=self._fresh(f"class.{cd.name}"),
            )
        self._maybe_emit_class_metaclass_slot(info, cls_ptr)
        if info.slots_only:
            builder.call(runtime["py_class_mark_slots_only"], [cls_ptr])
        if self._class_subclasses_dict(info):
            builder.call(runtime["py_class_mark_dict_subclass"], [cls_ptr])

        method_defs_by_name = {mname: fd for mname, fd in info.method_defs}

        # 5. py_class_add_method(cls, "method", func_as_PyObject_ptr) for each.
        for mname, mfunc in info.methods.items():
            mname_ptr = self._cname_ptr(mname)
            method_kind = info.method_kinds.get(mname, "instance")
            method_def = method_defs_by_name.get(mname)
            mref = info.method_refs.get(mname)
            if mref is None:
                mref = mfunc
            if method_kind == "instance" and method_def is not None:
                func_as_obj = self._emit_method_pyfunc_object(
                    cd,
                    mname,
                    mfunc,
                    method_def,
                    mname_ptr,
                    "method",
                )
            else:
                func_as_obj = builder.bitcast(
                    mref, _PTR, name=self._fresh(f"m.{mname}")
                )
            builder.call(
                runtime["py_class_add_method"], [cls_ptr, mname_ptr, func_as_obj]
            )
            if method_kind == "classmethod":
                if method_def is not None:
                    func_obj = self._emit_method_pyfunc_object(
                        cd,
                        mname,
                        mfunc,
                        method_def,
                        mname_ptr,
                        "classmethod",
                    )
                    classmethod_obj = builder.call(
                        runtime["py_classmethod_new"],
                        [func_obj],
                        name=self._fresh(f"classmethod.{mname}"),
                    )
                    builder.call(
                        runtime["py_class_setattr_raw"],
                        [cls_ptr, mname_ptr, classmethod_obj],
                    )
                    builder.call(runtime["py_decref"], [classmethod_obj])
                    builder.call(runtime["py_decref"], [func_obj])
        self._emit_property_descriptor_class_attrs(cd, info, cls_ptr)
        hash_attr = info.class_attr_values.get("__hash__")
        hash_is_none = _is_ast_node(hash_attr, NoneLit)
        eq_clears_hash = (
            "__eq__" in info.methods
            and "__hash__" not in info.methods
            and "__hash__" not in info.class_attr_values
        )
        if hash_is_none or eq_clears_hash:
            none_gv = declare_runtime_global(self.parent.module, "py_None")
            none_obj = builder.load(none_gv, name=self._fresh("hash.none"))
            builder.call(
                runtime["py_class_add_method"],
                [cls_ptr, self._cname_ptr("__hash__"), none_obj],
            )

        # 6. Initialize class-attribute storage. Class bodies execute
        # sequentially, so later attribute initializers can refer to
        # earlier attributes by bare name.
        missing_env = object()
        saved_class_attr_env = {}
        for attr_name in info.class_attr_values:
            saved_class_attr_env[attr_name] = self.parent.env.get(
                attr_name,
                missing_env,
            )
        try:
            for attr_name, value_expr in info.class_attr_initializers:
                gv, _attr_ty = info.class_attrs[attr_name]
                if attr_name in info.enum_members:
                    # Enum members load as their int value so identity /
                    # equality (``x == E.A``) behaves; a None placeholder
                    # made every member compare equal. ``E.A.name`` /
                    # ``E.A.value`` stay statically lowered in
                    # _maybe_emit_enum_member_attr.
                    obj = builder.call(
                        runtime["py_int_from_i64"],
                        [ir.Constant(_I64, int(info.enum_members[attr_name]))],
                        name=self._fresh("enum." + attr_name),
                    )
                elif attr_name in info.enum_string_members:
                    obj = self.parent._emit_str_literal(
                        info.enum_string_members[attr_name]
                    )
                elif attr_name in prepared_attr_objects:
                    obj = prepared_attr_objects[attr_name]
                else:
                    re_pattern = self.parent._native_re_class_compile_attr_string_value(
                        info.name,
                        attr_name,
                        value_expr,
                    )
                    if re_pattern is not None:
                        obj = self.parent._emit_str_literal(re_pattern)
                    else:
                        raw = self.parent._emit_expr(value_expr)
                        obj = marshal.marshal_to_object(
                            builder,
                            self.parent.module,
                            runtime,
                            raw,
                            value_expr.ty,
                        )
                builder.store(obj, gv)
                builder.call(
                    runtime["py_class_setattr_raw"],
                    [cls_ptr, self._cname_ptr(attr_name), obj],
                )
                self.parent.env[attr_name] = (gv, _PTR, value_expr.ty)
                self._maybe_emit_set_name(info, attr_name, obj, cls_ptr)
        finally:
            for attr_name, old_env in saved_class_attr_env.items():
                if old_env is missing_env:
                    if attr_name in self.parent.env:
                        del self.parent.env[attr_name]
                else:
                    self.parent.env[attr_name] = old_env

        # 7. Apply native unary class decorators in reverse source order,
        # matching Python's ``C = dec_n(...dec_1(C))`` rebinding rule.
        for decorator in reversed(info.runtime_decorators):
            decorator_fn = self.parent.functions.get(decorator.ident)
            if decorator_fn is None:
                raise NotImplementedError(
                    f"Layer 1 cannot resolve class decorator "
                    f"{decorator.ident!r} on {cd.name!r}"
                )
            previous_cls_ptr = cls_ptr
            builder.call(runtime["pcc_gc_pin"], [previous_cls_ptr])
            cls_ptr = self.parent._call_user(
                decorator_fn,
                [previous_cls_ptr],
                self._fresh(f"class.decorator.{cd.name}.{decorator.ident}"),
                cd.span,
                root_result=True,
                pinned_arg_temps=((previous_cls_ptr, True),),
            )
            builder.call(
                runtime["pcc_gc_unpin"],
                [parent._value_available_at_insertion_point(previous_cls_ptr)],
            )
            self.parent._gc_release(
                previous_cls_ptr,
                self.parent._release_context_label(
                    "class-decorator-input:" + cd.name
                ),
            )

        # 8. Module classes publish into their global.  Synthetic local
        # classes return the fresh object to a rooted function-local binding.
        if publish_global:
            builder.store(cls_ptr, info.global_var)
        return cls_ptr

    def _load_class_object(
        self,
        info: ClassInfo,
        name_hint: str,
    ) -> ir.Value:
        """Load a class object, preferring an active function-local binding."""
        parent = self.parent
        if parent.current_func_def is not None:
            slot = parent.env.get(info.name)
            if slot is not None:
                alloca, ir_ty, _declared_ty = slot
                if _classgen_ir_type_is_pointer(ir_ty):
                    if info.name in getattr(
                        parent,
                        "_gc_rooted_local_names",
                        set(),
                    ):
                        return parent.builder.call(
                            parent.runtime["pcc_gc_load_ptr"],
                            [
                                ir.Constant(_PTR, None),
                                parent._as_gc_ptr(alloca),
                            ],
                            name=self._fresh(name_hint),
                        )
                    return parent.builder.load(
                        alloca,
                        name=self._fresh(name_hint),
                    )
        loaded = parent.builder.load(
            info.global_var,
            name=self._fresh(name_hint),
        )
        # A class object loaded from its module-level global can be pinned in
        # one block and unpinned in a cleanup block a park boundary made
        # unreachable from it; record the source so the cleanup can re-derive.
        parent._note_global_backed_value(loaded, info.global_var)
        return loaded

    def _maybe_emit_class_metaclass_slot(
        self,
        info: ClassInfo,
        cls_ptr: ir.Value,
    ) -> None:
        metaclass_name = getattr(info, "metaclass_name", None)
        if not metaclass_name or metaclass_name == _METACLASS_CONFLICT:
            return
        meta_info = self.classes.get(metaclass_name)
        if meta_info is None or meta_info is info:
            return
        meta_cls = self._load_class_object(
            meta_info,
            f".metaclass.{info.name}.{metaclass_name}",
        )
        self.parent.builder.call(
            self.parent.runtime["py_class_set_metaclass"],
            [cls_ptr, meta_cls],
        )

    def _emit_property_descriptor_class_attrs(
        self,
        cd: ClassDef,
        info: ClassInfo,
        cls_ptr: ir.Value,
    ) -> None:
        for prop_name, getter_fn in info.properties.items():
            getter_def = self._find_method_def(info.name, prop_name)
            if getter_def is None:
                continue
            getter_obj = self._emit_property_accessor_func_obj(
                cd,
                info,
                prop_name,
                "getter",
                getter_fn,
                getter_def.return_ty,
            )
            null_obj = ir.Constant(_PTR, None)
            setter_obj = null_obj
            setter_fn = info.property_setters.get(prop_name)
            if setter_fn is not None:
                setter_def = self._find_property_accessor_def(
                    cd, info, prop_name, "setter"
                )
                if setter_def is not None:
                    setter_obj = self._emit_property_accessor_func_obj(
                        cd,
                        info,
                        prop_name,
                        "setter",
                        setter_fn,
                        setter_def.return_ty,
                    )
            deleter_obj = null_obj
            deleter_fn = info.property_deleters.get(prop_name)
            if deleter_fn is not None:
                deleter_def = self._find_property_accessor_def(
                    cd, info, prop_name, "deleter"
                )
                if deleter_def is not None:
                    deleter_obj = self._emit_property_accessor_func_obj(
                        cd,
                        info,
                        prop_name,
                        "deleter",
                        deleter_fn,
                        deleter_def.return_ty,
                    )
            prop_obj = self.parent.builder.call(
                self.parent.runtime["py_property_new"],
                [getter_obj, setter_obj, deleter_obj],
                name=self._fresh(f"property.{info.name}.{prop_name}"),
            )
            self.parent.builder.call(
                self.parent.runtime["py_class_setattr_raw"],
                [cls_ptr, self._cname_ptr(prop_name), prop_obj],
            )

    def _find_property_accessor_def(
        self,
        cd: ClassDef,
        info: ClassInfo,
        prop_name: str,
        accessor_kind: str,
    ):
        body_cd = info.expanded_cd if info.expanded_cd is not None else cd
        for stmt in body_cd.body:
            if not _is_ast_node(stmt, FuncDef) or stmt.name != prop_name:
                continue
            if accessor_kind == "setter" and _funcdef_is_property_setter(stmt):
                return stmt
            if accessor_kind == "deleter" and _funcdef_is_property_deleter(stmt):
                return stmt
        return None

    def _emit_method_pyfunc_object(
        self,
        cd: ClassDef,
        method_name: str,
        method_fn: ir.Function,
        method_def: FuncDef,
        method_name_ptr: ir.Value,
        suffix: str,
    ) -> ir.Value:
        runtime_args = tuple(a for a in method_def.args if a.name != "")
        body_adapter = self.parent._emit_native_func_adapter(
            f"{cd.name}_{method_name}_{suffix}",
            method_fn,
            runtime_args,
            (),
            method_def.return_ty,
        )
        adapter = (
            self.parent._emit_async_native_func_value_adapter(
                f"{cd.name}_{method_name}_{suffix}",
                body_adapter,
            )
            if method_def.is_async
            else body_adapter
        )
        captures = self.parent.builder.call(
            self.parent.runtime["py_tuple_new"],
            [ir.Constant(_I64, 0)],
            name=self._fresh(f"{suffix}.{method_name}.captures"),
        )
        signature = self.parent._emit_native_func_signature(runtime_args)
        wrapped_captures = self.parent.builder.call(
            self.parent.runtime["py_tuple_new"],
            [ir.Constant(_I64, 2)],
            name=self._fresh(f"{suffix}.{method_name}.signature.wrapper"),
        )
        self.parent.builder.call(
            self.parent.runtime["py_tuple_set_item"],
            [wrapped_captures, ir.Constant(_I64, 0), captures],
        )
        self.parent.builder.call(
            self.parent.runtime["py_tuple_set_item"],
            [wrapped_captures, ir.Constant(_I64, 1), signature],
        )
        func_obj = self.parent.builder.call(
            self.parent.runtime["py_func_new_named"],
            [adapter, wrapped_captures, method_name_ptr],
            name=self._fresh(f"{suffix}.{method_name}.func"),
        )
        self.parent._gc_release(captures)
        self.parent._gc_release(signature)
        self.parent._gc_release(wrapped_captures)
        return func_obj

    def _emit_property_accessor_func_obj(
        self,
        cd: ClassDef,
        info: ClassInfo,
        prop_name: str,
        accessor_kind: str,
        fn: ir.Function,
        return_ty,
    ) -> ir.Value:
        accessor_def = self._find_method_def(info.name, prop_name)
        if accessor_kind != "getter":
            accessor_def = self._find_property_accessor_def(
                cd, info, prop_name, accessor_kind
            )
        if accessor_def is None:
            return ir.Constant(_PTR, None)
        runtime_args = tuple(a for a in accessor_def.args if a.name != "")
        adapter_name = cd.name + "_" + prop_name + "_property"
        if accessor_kind != "getter":
            adapter_name = adapter_name + "_" + accessor_kind
        adapter = self.parent._emit_native_func_adapter(
            adapter_name,
            fn,
            runtime_args,
            (),
            return_ty,
        )
        captures = self.parent.builder.call(
            self.parent.runtime["py_tuple_new"],
            [ir.Constant(_I64, 0)],
            name=self._fresh(
                f"property.{info.name}.{prop_name}.{accessor_kind}.captures"
            ),
        )
        func_obj = self.parent.builder.call(
            self.parent.runtime["py_func_new_named"],
            [adapter, captures, self.parent._attr_name_ptr(prop_name)],
            name=self._fresh(f"property.{info.name}.{prop_name}.{accessor_kind}"),
        )
        self.parent._gc_release(captures)
        return func_obj

    def _class_header_kwargs(
        self,
        cd: ClassDef,
    ) -> tuple[tuple[str, Expr], ...]:
        out: list[tuple[str, Expr]] = []
        for key, value in cd.keywords:
            if key == "metaclass":
                continue
            out.append((key, value))
        return tuple(out)

    def _class_name_expr(self, cd: ClassDef) -> StrLit:
        return StrLit(span=cd.span, ty=StrType(name="str"), value=cd.name)

    def _class_bases_tuple_expr(self, cd: ClassDef) -> TupleExpr:
        return TupleExpr(
            span=cd.span,
            ty=TupleType(
                name="tuple",
                elems=tuple(DynType(name="dyn") for _base in cd.bases),
            ),
            elems=tuple(cd.bases),
        )

    def _class_namespace_expr(self, cd: ClassDef, info: ClassInfo) -> DictExpr:
        pairs: list[tuple[Expr, Expr]] = []
        for attr_name, value_expr in info.class_attr_values.items():
            pairs.append(
                (
                    StrLit(span=cd.span, ty=StrType(name="str"), value=attr_name),
                    value_expr,
                )
            )
        return DictExpr(
            span=cd.span,
            ty=DictType(
                name="dict",
                key=StrType(name="str"),
                value=DynType(name="dyn"),
            ),
            pairs=tuple(pairs),
        )

    def _class_metaclass_expr(self, cd: ClassDef) -> Optional[Expr]:
        for key, value in cd.keywords:
            if key == "metaclass":
                return value
        return None

    def _class_metaclass_expr_needs_runtime_eval(self, cd: ClassDef) -> bool:
        expr = self._class_metaclass_expr(cd)
        if expr is None:
            return False
        if _is_ast_node(expr, Name):
            return False
        return True

    def _prepare_namespace_class_name(
        self,
        meta_info: ClassInfo,
        before_cd: ClassDef,
    ) -> Optional[str]:
        fd = self._find_method_def(meta_info.name, "__prepare__")
        if fd is None:
            return None
        for stmt in fd.body:
            if not _is_ast_node(stmt, Return):
                continue
            value = stmt.value
            ns_name = self._resolve_namespace_class_from_call_expr(value, before_cd)
            if ns_name is not None:
                return ns_name
        return None

    def _resolve_namespace_class_from_call_expr(
        self,
        value: Optional[Expr],
        before_cd: ClassDef,
    ) -> Optional[str]:
        if not (_is_ast_node(value, Call) and _is_ast_node(value.func, Name)):
            return None
        ns_name = value.func.ident
        ns_info = self.classes.get(ns_name)
        if ns_info is not None and "__setitem__" in ns_info.methods:
            return ns_name
        alias_name = self._resolve_module_class_alias_before(ns_name, before_cd)
        if alias_name is not None:
            ns_info = self.classes.get(alias_name)
            if ns_info is not None and "__setitem__" in ns_info.methods:
                return alias_name
        return self._resolve_function_returning_namespace_class_name_before(
            ns_name,
            before_cd,
        )

    def _merge_namespace_return_names(
        self,
        lhs: Optional[str],
        rhs: Optional[str],
    ) -> Optional[str]:
        if lhs is None:
            return rhs
        if rhs is None:
            return lhs
        if lhs == rhs:
            return lhs
        return ""

    def _resolve_function_returning_namespace_class_name_before(
        self,
        func_name: str,
        before_cd: ClassDef,
    ) -> Optional[str]:
        for stmt in self.parent.ast_module.body:
            if stmt is before_cd:
                break
            if not _is_ast_node(stmt, FuncDef) or stmt.name != func_name:
                continue
            selected: Optional[str] = None
            local_names: dict[str, str] = {}
            for body_stmt in stmt.body:
                if _is_ast_node(body_stmt, Assign) and len(body_stmt.targets) == 1:
                    target = body_stmt.targets[0]
                    if _is_ast_node(target, Name):
                        ns_name = self._resolve_namespace_class_from_call_expr(
                            body_stmt.value,
                            before_cd,
                        )
                        if ns_name is None:
                            local_names.pop(target.ident, None)
                        else:
                            local_names[target.ident] = ns_name
                    continue
                if not _is_ast_node(body_stmt, Return):
                    continue
                if _is_ast_node(body_stmt.value, Name):
                    ns_name = local_names.get(body_stmt.value.ident)
                else:
                    ns_name = self._resolve_namespace_class_from_call_expr(
                        body_stmt.value,
                        before_cd,
                    )
                if ns_name is None:
                    return None
                selected = self._merge_namespace_return_names(selected, ns_name)
                if selected == "":
                    return None
            return selected
        return None

    def _emit_object_tuple_from_objects(
        self,
        values: tuple[ir.Value, ...],
        name: str,
    ) -> ir.Value:
        tup = self.parent.builder.call(
            self.parent.runtime["py_tuple_new"],
            [ir.Constant(_I64, len(values))],
            name=self._fresh(name),
        )
        i = 0
        while i < len(values):
            self.parent.builder.call(
                self.parent.runtime["py_tuple_set_item"],
                [tup, ir.Constant(_I64, i), values[i]],
            )
            i += 1
        return tup

    def _emit_namespace_setitem(
        self,
        ns_obj: ir.Value,
        ns_info: Optional[ClassInfo],
        key: str,
        value_obj: ir.Value,
    ) -> None:
        key_obj = self.parent._emit_str_literal(key)
        if ns_info is not None and "__setitem__" in ns_info.methods:
            self.parent._call_user(
                ns_info.methods["__setitem__"],
                [ns_obj, key_obj, value_obj],
                self._fresh(f"namespace.setitem.{key}.ret"),
                None,
            )
            return
        status = self.parent.builder.call(
            self.parent.runtime["py_obj_setitem"],
            [ns_obj, key_obj, value_obj],
            name=self._fresh(f"namespace.setitem.{key}"),
        )
        builder = self.parent.builder
        failed = builder.icmp_signed(
            "<",
            status,
            ir.Constant(_I64, 0),
            name=self._fresh(f"namespace.setitem.{key}.failed"),
        )
        fallback_bb = self.parent.current_function.append_basic_block(
            name=self._fresh(f"namespace.setitem.{key}.fallback"),
        )
        ok_bb = self.parent.current_function.append_basic_block(
            name=self._fresh(f"namespace.setitem.{key}.ok"),
        )
        builder.cbranch(failed, fallback_bb, ok_bb)

        builder.position_at_end(fallback_bb)
        err_flag = builder.call(
            self.parent.runtime["py_err_occurred"],
            [],
            name=self._fresh(f"namespace.setitem.{key}.err"),
        )
        has_err = builder.icmp_signed(
            "!=",
            err_flag,
            ir.Constant(_I64, 0),
            name=self._fresh(f"namespace.setitem.{key}.has_err"),
        )
        propagate_bb = self.parent.current_function.append_basic_block(
            name=self._fresh(f"namespace.setitem.{key}.propagate"),
        )
        call_bb = self.parent.current_function.append_basic_block(
            name=self._fresh(f"namespace.setitem.{key}.call_dunder"),
        )
        builder.cbranch(has_err, propagate_bb, call_bb)

        builder.position_at_end(propagate_bb)
        cur_exc = builder.call(
            self.parent.runtime["py_current_exception"],
            [],
            name=self._fresh(f"namespace.setitem.{key}.exc"),
        )
        self.parent._emit_exception_frame(cur_exc, None)
        err_target = self.parent._current_try_err_block()
        if err_target is None:
            err_target = self.parent._ensure_fn_err_exit()
        builder.branch(err_target)

        builder.position_at_end(call_bb)
        setitem_name = _classgen_attr_name_ptr(self.parent, "__setitem__")
        method = builder.call(
            self.parent.runtime["py_obj_getattr"],
            [ns_obj, setitem_name],
            name=self._fresh(f"namespace.setitem.{key}.method"),
        )
        self.parent._emit_attribute_error_if_null(method, "__setitem__", None)
        args = self._emit_object_tuple_from_objects(
            (key_obj, value_obj),
            self._fresh(f"namespace.setitem.{key}.args"),
        )
        none_obj = self.parent._emit_none_literal()
        builder.call(
            self.parent.runtime["py_obj_call"],
            [method, args, none_obj],
            name=self._fresh(f"namespace.setitem.{key}.call"),
        )
        self.parent._emit_post_call_err_check(None)
        if not self.parent._builder_block_is_terminated():
            builder.branch(ok_bb)
        builder.position_at_end(ok_bb)

    def _emit_namespace_delitem(
        self,
        ns_obj: ir.Value,
        ns_info: Optional[ClassInfo],
        key: str,
        span,
    ) -> None:
        key_obj = self.parent._emit_str_literal(key)
        if ns_info is not None and "__delitem__" in ns_info.methods:
            self.parent._call_user(
                ns_info.methods["__delitem__"],
                [ns_obj, key_obj],
                self._fresh(f"namespace.delitem.{key}.ret"),
                None,
            )
            return
        status = self.parent.builder.call(
            self.parent.runtime["py_obj_delitem"],
            [ns_obj, key_obj],
            name=self._fresh(f"namespace.delitem.{key}"),
        )
        builder = self.parent.builder
        failed = builder.icmp_signed(
            "<",
            status,
            ir.Constant(_I64, 0),
            name=self._fresh(f"namespace.delitem.{key}.failed"),
        )
        err_bb = self.parent.current_function.append_basic_block(
            name=self._fresh(f"namespace.delitem.{key}.err"),
        )
        ok_bb = self.parent.current_function.append_basic_block(
            name=self._fresh(f"namespace.delitem.{key}.ok"),
        )
        builder.cbranch(failed, err_bb, ok_bb)
        builder.position_at_end(err_bb)
        cur_exc = builder.call(
            self.parent.runtime["py_current_exception"],
            [],
            name=self._fresh(f"namespace.delitem.{key}.exc"),
        )
        self.parent._emit_exception_frame(cur_exc, span)
        err_target = self.parent._current_try_err_block()
        if err_target is None:
            err_target = self.parent._ensure_fn_err_exit()
        builder.branch(err_target)
        builder.position_at_end(ok_bb)

    def _emit_namespace_load_name_probe(
        self,
        ns_obj: ir.Value,
        ns_info: Optional[ClassInfo],
        name: str,
        span,
    ) -> None:
        builder = self.parent.builder
        if ns_info is not None and "__getitem__" in ns_info.methods:
            key_obj = self.parent._emit_str_literal(name)
            builder.call(
                ns_info.methods["__getitem__"],
                [ns_obj, key_obj],
                name=self._fresh(f"namespace.loadname.{name}.ret"),
            )
        elif ns_info is None:
            key_obj = self.parent._emit_str_literal(name)
            builder.call(
                self.parent.runtime["py_obj_getitem"],
                [ns_obj, key_obj],
                name=self._fresh(f"namespace.loadname.{name}.ret"),
            )
        else:
            return
        fn = self.parent.current_function
        err_flag = builder.call(
            self.parent.runtime["py_err_occurred"],
            [],
            name=self._fresh(f"namespace.loadname.{name}.err"),
        )
        has_err = builder.icmp_signed(
            "!=",
            err_flag,
            ir.Constant(_I64, 0),
            name=self._fresh(f"namespace.loadname.{name}.has_err"),
        )
        err_bb = fn.append_basic_block(
            name=self._fresh(f"namespace.loadname.{name}.errbb"),
        )
        ok_bb = fn.append_basic_block(
            name=self._fresh(f"namespace.loadname.{name}.ok"),
        )
        builder.cbranch(has_err, err_bb, ok_bb)

        builder.position_at_end(err_bb)
        cur_exc = builder.call(
            self.parent.runtime["py_current_exception"],
            [],
            name=self._fresh(f"namespace.loadname.{name}.exc"),
        )
        key_cls = builder.call(
            self.parent.runtime["py_exc_builtin_class"],
            [ir.Constant(_I64, 4)],
            name=self._fresh(f"namespace.loadname.{name}.key_cls"),
        )
        matches = builder.call(
            self.parent.runtime["py_exc_matches"],
            [cur_exc, key_cls],
            name=self._fresh(f"namespace.loadname.{name}.is_key"),
        )
        is_key = builder.icmp_signed(
            "!=",
            matches,
            ir.Constant(_I64, 0),
            name=self._fresh(f"namespace.loadname.{name}.is_key_i1"),
        )
        clear_bb = fn.append_basic_block(
            name=self._fresh(f"namespace.loadname.{name}.clear"),
        )
        propagate_bb = fn.append_basic_block(
            name=self._fresh(f"namespace.loadname.{name}.propagate"),
        )
        builder.cbranch(is_key, clear_bb, propagate_bb)

        builder.position_at_end(clear_bb)
        builder.call(self.parent.runtime["py_clear_exception"], [])
        builder.branch(ok_bb)

        builder.position_at_end(propagate_bb)
        self.parent._emit_exception_frame(cur_exc, span)
        err_target = self.parent._current_try_err_block()
        if err_target is None:
            err_target = self.parent._ensure_fn_err_exit()
        builder.branch(err_target)

        builder.position_at_end(ok_bb)

    def _emit_namespace_expr_object(self, expr: Expr) -> ir.Value:
        raw = self.parent._emit_expr_with_native_callable_values(expr)
        return marshal.marshal_to_object(
            self.parent.builder,
            self.parent.module,
            self.parent.runtime,
            raw,
            expr.ty,
        )

    def _emit_namespace_method_object(
        self,
        cd: ClassDef,
        fd: FuncDef,
        info: ClassInfo,
    ) -> Optional[ir.Value]:
        method_fn = info.methods.get(fd.name)
        if method_fn is None:
            return None
        runtime_args = tuple(a for a in fd.args if a.name != "")
        body_adapter = self.parent._emit_native_func_adapter(
            cd.name + "_" + fd.name + "_namespace",
            method_fn,
            runtime_args,
            (),
            fd.return_ty,
        )
        adapter = (
            self.parent._emit_async_native_func_value_adapter(
                cd.name + "_" + fd.name + "_namespace",
                body_adapter,
            )
            if fd.is_async
            else body_adapter
        )
        captures = self.parent.builder.call(
            self.parent.runtime["py_tuple_new"],
            [ir.Constant(_I64, 0)],
            name=self._fresh("namespace.method.captures"),
        )
        signature = self.parent._emit_native_func_signature(runtime_args)
        wrapped_captures = self.parent.builder.call(
            self.parent.runtime["py_tuple_new"],
            [ir.Constant(_I64, 2)],
            name=self._fresh(f"namespace.method.{fd.name}.signature.wrapper"),
        )
        self.parent.builder.call(
            self.parent.runtime["py_tuple_set_item"],
            [wrapped_captures, ir.Constant(_I64, 0), captures],
        )
        self.parent.builder.call(
            self.parent.runtime["py_tuple_set_item"],
            [wrapped_captures, ir.Constant(_I64, 1), signature],
        )
        fn_obj = self.parent.builder.call(
            self.parent.runtime["py_func_new_named"],
            [adapter, wrapped_captures, self.parent._attr_name_ptr(fd.name)],
            name=self._fresh(f"namespace.method.{fd.name}"),
        )
        self.parent._gc_release(captures)
        self.parent._gc_release(signature)
        self.parent._gc_release(wrapped_captures)
        return fn_obj

    def _emit_prepared_namespace_body_writes(
        self,
        cd: ClassDef,
        info: ClassInfo,
        ns_obj: ir.Value,
        ns_info: Optional[ClassInfo],
    ) -> dict[str, ir.Value]:
        precomputed: dict[str, ir.Value] = {}
        self._emit_namespace_load_name_probe(
            ns_obj,
            ns_info,
            "__name__",
            cd.span,
        )
        self._emit_namespace_setitem(
            ns_obj,
            ns_info,
            "__module__",
            self.parent._emit_str_literal("__main__"),
        )
        self._emit_namespace_setitem(
            ns_obj,
            ns_info,
            "__qualname__",
            self.parent._emit_str_literal(cd.name),
        )
        first_lineno = cd.span.line if cd.span is not None else 1
        first_lineno_expr = IntLit(
            span=cd.span,
            ty=IntType(name="int"),
            value=int(first_lineno),
        )
        self._emit_namespace_setitem(
            ns_obj,
            ns_info,
            "__firstlineno__",
            self._emit_namespace_expr_object(first_lineno_expr),
        )
        for stmt in cd.body:
            if _is_ast_node(stmt, Assign) and len(stmt.targets) == 1:
                target = stmt.targets[0]
                if _is_ast_node(target, Name):
                    if (
                        _is_ast_node(stmt.value, Name)
                        and stmt.value.ident in precomputed
                    ):
                        self._emit_namespace_load_name_probe(
                            ns_obj,
                            ns_info,
                            stmt.value.ident,
                            stmt.value.span,
                        )
                        value_obj = precomputed[stmt.value.ident]
                    else:
                        value_obj = self._emit_namespace_expr_object(stmt.value)
                    self._emit_namespace_setitem(
                        ns_obj,
                        ns_info,
                        target.ident,
                        value_obj,
                    )
                    precomputed[target.ident] = value_obj
                    continue
            if _is_ast_node(stmt, Delete):
                for target in stmt.targets:
                    if _is_ast_node(target, Name):
                        self._emit_namespace_delitem(
                            ns_obj,
                            ns_info,
                            target.ident,
                            stmt.span,
                        )
                        precomputed.pop(target.ident, None)
                continue
            if _is_ast_node(stmt, FuncDef):
                method_obj = self._emit_namespace_method_object(cd, stmt, info)
                if method_obj is not None:
                    self._emit_namespace_setitem(
                        ns_obj,
                        ns_info,
                        stmt.name,
                        method_obj,
                    )
        self._emit_namespace_setitem(
            ns_obj,
            ns_info,
            "__static_attributes__",
            self.parent._emit_empty_tuple("namespace.static.attrs"),
        )
        return precomputed

    def _maybe_emit_metaclass_prepared_namespace_constructor(
        self,
        cd: ClassDef,
        info: ClassInfo,
    ) -> Optional[tuple[ir.Value, dict[str, ir.Value]]]:
        if self._class_header_kwargs(cd):
            return None
        metaclass_name = info.metaclass_name
        if metaclass_name is None:
            return None
        meta_info = self.classes.get(metaclass_name)
        if meta_info is None:
            return None
        if "__prepare__" not in meta_info.methods or "__new__" not in meta_info.methods:
            return None
        ns_class_name = self._prepare_namespace_class_name(meta_info, cd)
        if ns_class_name is None:
            return None
        ns_info = self.classes.get(ns_class_name)
        if ns_info is None:
            return None
        meta_cls_ptr = self._load_class_object(
            meta_info,
            f".meta.{metaclass_name}",
        )
        name_expr = self._class_name_expr(cd)
        bases_expr = self._class_bases_tuple_expr(cd)
        ns_obj = self.parent._emit_direct_method_call(
            meta_info.methods["__prepare__"],
            meta_cls_ptr,
            meta_info,
            "__prepare__",
            (name_expr, bases_expr),
            kwargs=(),
        )
        precomputed = self._emit_prepared_namespace_body_writes(
            cd,
            info,
            ns_obj,
            ns_info,
        )
        name_obj = self.parent._emit_str_literal(cd.name)
        bases_obj = self.parent._emit_as_object(bases_expr)
        result = self.parent._call_user(
            meta_info.methods["__new__"],
            [meta_cls_ptr, name_obj, bases_obj, ns_obj],
            self._fresh(f"{metaclass_name}.__new__.ret"),
            None,
        )
        result_ty = _classgen_value_type_or_none(result)
        if result_ty is not None and _classgen_ir_type_is_pointer(result_ty):
            return result, precomputed
        return None

    def _maybe_emit_metaclass_generic_prepared_namespace_constructor(
        self,
        cd: ClassDef,
        info: ClassInfo,
    ) -> Optional[tuple[ir.Value, dict[str, ir.Value]]]:
        if self._class_header_kwargs(cd):
            return None
        if self._class_metaclass_expr_needs_runtime_eval(cd):
            return None
        metaclass_name = info.metaclass_name
        if metaclass_name is None:
            return None
        meta_info = self.classes.get(metaclass_name)
        if meta_info is None:
            return None
        if "__prepare__" not in meta_info.methods or "__new__" not in meta_info.methods:
            return None
        meta_cls_ptr = self._load_class_object(
            meta_info,
            f".meta.{metaclass_name}",
        )
        name_expr = self._class_name_expr(cd)
        bases_expr = self._class_bases_tuple_expr(cd)
        ns_obj = self.parent._emit_direct_method_call(
            meta_info.methods["__prepare__"],
            meta_cls_ptr,
            meta_info,
            "__prepare__",
            (name_expr, bases_expr),
            kwargs=(),
        )
        precomputed = self._emit_prepared_namespace_body_writes(
            cd,
            info,
            ns_obj,
            None,
        )
        name_obj = self.parent._emit_str_literal(cd.name)
        bases_obj = self.parent._emit_as_object(bases_expr)
        result = self.parent._call_user(
            meta_info.methods["__new__"],
            [meta_cls_ptr, name_obj, bases_obj, ns_obj],
            self._fresh(f"{metaclass_name}.__new__.ret"),
            None,
        )
        result_ty = _classgen_value_type_or_none(result)
        if result_ty is not None and _classgen_ir_type_is_pointer(result_ty):
            return result, precomputed
        return None

    def _maybe_emit_metaclass_dynamic_constructor(
        self,
        cd: ClassDef,
        info: ClassInfo,
    ) -> Optional[ir.Value]:
        if self._class_header_kwargs(cd):
            return None
        if not self._class_metaclass_expr_needs_runtime_eval(cd):
            return None
        metaclass_name = info.metaclass_name
        if metaclass_name is None:
            return None
        meta_info = self.classes.get(metaclass_name)
        if meta_info is None:
            return None
        if "__new__" not in meta_info.methods:
            return None
        metaclass_expr = self._class_metaclass_expr(cd)
        if metaclass_expr is None:
            return None
        meta_cls_ptr = self.parent._emit_as_object(metaclass_expr)
        name_expr = self._class_name_expr(cd)
        bases_expr = self._class_bases_tuple_expr(cd)
        if "__prepare__" in meta_info.methods:
            self.parent._emit_direct_method_call(
                meta_info.methods["__prepare__"],
                meta_cls_ptr,
                meta_info,
                "__prepare__",
                (name_expr, bases_expr),
                kwargs=(),
            )
        ns_expr = self._class_namespace_expr(cd, info)
        result = self.parent._emit_direct_method_call(
            meta_info.methods["__new__"],
            meta_cls_ptr,
            meta_info,
            "__new__",
            (name_expr, bases_expr, ns_expr),
            kwargs=(),
        )
        result_ty = _classgen_value_type_or_none(result)
        if result_ty is not None and _classgen_ir_type_is_pointer(result_ty):
            return result
        return None

    def _maybe_emit_metaclass_static_constructor(
        self,
        cd: ClassDef,
        info: ClassInfo,
    ) -> Optional[ir.Value]:
        if self._class_header_kwargs(cd):
            return None
        if self._class_metaclass_expr(cd) is None:
            return None
        if self._class_metaclass_expr_needs_runtime_eval(cd):
            return None
        metaclass_name = info.metaclass_name
        if metaclass_name is None:
            return None
        meta_info = self.classes.get(metaclass_name)
        if meta_info is None:
            return None
        if "__new__" not in meta_info.methods:
            return None
        meta_cls_ptr = self._load_class_object(
            meta_info,
            f".meta.{metaclass_name}",
        )
        name_expr = self._class_name_expr(cd)
        bases_expr = self._class_bases_tuple_expr(cd)
        if "__prepare__" in meta_info.methods:
            self.parent._emit_direct_method_call(
                meta_info.methods["__prepare__"],
                meta_cls_ptr,
                meta_info,
                "__prepare__",
                (name_expr, bases_expr),
                kwargs=(),
            )
        ns_expr = self._class_namespace_expr(cd, info)
        result = self.parent._emit_direct_method_call(
            meta_info.methods["__new__"],
            meta_cls_ptr,
            meta_info,
            "__new__",
            (name_expr, bases_expr, ns_expr),
            kwargs=(),
        )
        result_ty = _classgen_value_type_or_none(result)
        if result_ty is not None and _classgen_ir_type_is_pointer(result_ty):
            return result
        return None

    def _maybe_emit_metaclass_inherited_constructor(
        self,
        cd: ClassDef,
        info: ClassInfo,
    ) -> Optional[ir.Value]:
        if self._class_header_kwargs(cd):
            return None
        if self._class_metaclass_expr(cd) is not None:
            return None
        metaclass_name = info.metaclass_name
        if metaclass_name is None:
            return None
        meta_info = self.classes.get(metaclass_name)
        if meta_info is None:
            return None
        if "__new__" not in meta_info.methods:
            return None
        meta_cls_ptr = self._load_class_object(
            meta_info,
            f".meta.{metaclass_name}",
        )
        name_expr = self._class_name_expr(cd)
        bases_expr = self._class_bases_tuple_expr(cd)
        if "__prepare__" in meta_info.methods:
            self.parent._emit_direct_method_call(
                meta_info.methods["__prepare__"],
                meta_cls_ptr,
                meta_info,
                "__prepare__",
                (name_expr, bases_expr),
                kwargs=(),
            )
        ns_expr = self._class_namespace_expr(cd, info)
        result = self.parent._emit_direct_method_call(
            meta_info.methods["__new__"],
            meta_cls_ptr,
            meta_info,
            "__new__",
            (name_expr, bases_expr, ns_expr),
            kwargs=(),
        )
        result_ty = _classgen_value_type_or_none(result)
        if result_ty is not None and _classgen_ir_type_is_pointer(result_ty):
            return result
        return None

    def _maybe_emit_metaclass_keyword_constructor(
        self,
        cd: ClassDef,
        info: ClassInfo,
    ) -> Optional[ir.Value]:
        header_kwargs = self._class_header_kwargs(cd)
        if not header_kwargs:
            return None
        metaclass_name = info.metaclass_name
        if metaclass_name is None:
            return None
        meta_info = self.classes.get(metaclass_name)
        if meta_info is None:
            return None
        builder = self.parent.builder
        meta_cls_ptr = self._load_class_object(
            meta_info,
            f".meta.{metaclass_name}",
        )
        name_expr = self._class_name_expr(cd)
        bases_expr = self._class_bases_tuple_expr(cd)
        if "__prepare__" in meta_info.methods:
            self.parent._emit_direct_method_call(
                meta_info.methods["__prepare__"],
                meta_cls_ptr,
                meta_info,
                "__prepare__",
                (name_expr, bases_expr),
                kwargs=header_kwargs,
            )
        if "__new__" not in meta_info.methods:
            return None
        ns_expr = self._class_namespace_expr(cd, info)
        result = self.parent._emit_direct_method_call(
            meta_info.methods["__new__"],
            meta_cls_ptr,
            meta_info,
            "__new__",
            (name_expr, bases_expr, ns_expr),
            kwargs=header_kwargs,
        )
        result_ty = _classgen_value_type_or_none(result)
        if result_ty is not None and _classgen_ir_type_is_pointer(result_ty):
            return result
        return None

    def _maybe_emit_set_name(
        self,
        owner_info: ClassInfo,
        attr_name: str,
        attr_obj: ir.Value,
        cls_ptr: ir.Value,
    ) -> None:
        value_expr = owner_info.class_attr_values.get(attr_name)
        if not (_is_ast_node(value_expr, Call) and _is_ast_node(value_expr.func, Name)):
            return
        desc_info = self.classes.get(value_expr.func.ident)
        if desc_info is None:
            return
        set_name = desc_info.methods.get("__set_name__")
        if set_name is None:
            return
        name_obj = self.parent._emit_str_literal(attr_name)
        self.parent._call_user(
            set_name,
            [attr_obj, cls_ptr, name_obj],
            "",
            None,
        )

    # -- small-object globals ------------------------------------------

    def _cname_ptr(self, s: str) -> ir.Value:
        """Return an i8* pointing at a NUL-terminated UTF-8 C string.

        Interned in ``_cname_pool``.
        """
        existing = self._cname_pool.get(s)
        if existing is None:
            data = self.parent._utf8_byte_values(s) + [0]
            arr_ty = ir.ArrayType(_I8, len(data))
            base = self._fresh(f".class_name.{s}")
            # Make sure we don't collide.
            if base in self.parent.module.globals:
                base = self._fresh(".class_name")
            gv = ir.GlobalVariable(self.parent.module, arr_ty, name=base)
            gv.linkage = "internal"
            gv.global_constant = True
            gv.initializer = ir.Constant(arr_ty, data)
            self._cname_pool[s] = gv
            existing = gv
        zero = ir.Constant(_I32, 0)
        return self.parent.builder.gep(existing, [zero, zero], inbounds=True)

    def _field_names_global(self, names: tuple[str, ...]) -> ir.Value:
        """Return an i8** pointing at field-name C string pointers.

        ``py_class_new`` copies this pointer array immediately, so a
        module-init local array is enough. Building it with regular
        builder operations also avoids the non-scaffolded
        ``GlobalVariable.gep`` constant helper in the self-host path.
        """
        if not names:
            return ir.Constant(_PTR, None)
        arr_ty = ir.ArrayType(_PTR, len(names))
        slot = self.parent.builder.alloca(
            arr_ty,
            name=self._fresh(".field_names.local"),
        )
        zero = ir.Constant(_I32, 0)
        for i, s in enumerate(names):
            ptr = self.parent.builder.gep(
                slot,
                [zero, ir.Constant(_I32, i)],
                inbounds=True,
                name=self._fresh(".fname.slot"),
            )
            self.parent.builder.store(self._cname_ptr(s), ptr)
        return self.parent.builder.gep(
            slot,
            [zero, zero],
            inbounds=True,
            name=self._fresh(".fnames"),
        )

    def _global_cstring(self, s: str) -> ir.GlobalVariable:
        """Intern a ``const char[N]`` global (not just a pointer).

        Distinct from :meth:`_cname_ptr` in that it returns the *GV*
        rather than an i8* — suitable for building constant initializer
        arrays of pointers.
        """
        # Reuse the _cname_pool storage (they hold the same shape).
        existing = self._cname_pool.get(s)
        if existing is None:
            data = self.parent._utf8_byte_values(s) + [0]
            arr_ty = ir.ArrayType(_I8, len(data))
            base = self._fresh(f".cstr.{s}")
            if base in self.parent.module.globals:
                base = self._fresh(".cstr")
            gv = ir.GlobalVariable(self.parent.module, arr_ty, name=base)
            gv.linkage = "internal"
            gv.global_constant = True
            gv.initializer = ir.Constant(arr_ty, data)
            self._cname_pool[s] = gv
            existing = gv
        return existing

    def _load_bases_array(
        self, class_name: str, base_values: list[ir.Value]
    ) -> ir.Value:
        """Emit code that builds a transient ``i8**`` pointing at the
        base-class pointer values.
        """
        builder = self.parent.builder
        # Allocate a stack array of PyObject* pointers. We allocate on
        # stack because the array is only needed for the duration of
        # the py_class_new call.
        arr_ty = ir.ArrayType(_PTR, len(base_values))
        slot = builder.alloca(arr_ty, name=self._fresh(f".bases.{class_name}"))
        for i, base_val in enumerate(base_values):
            zero = ir.Constant(_I32, 0)
            idx = ir.Constant(_I32, i)
            elem_ptr = builder.gep(
                slot, [zero, idx], inbounds=True, name=self._fresh(f".baseslot.{i}")
            )
            stored = base_val
            if stored.type != _PTR:
                stored = builder.bitcast(
                    stored, _PTR, name=self._fresh(f".base.i8p.{i}")
                )
            builder.store(stored, elem_ptr)
        zero = ir.Constant(_I32, 0)
        return builder.gep(
            slot,
            [zero, zero],
            inbounds=True,
            name=self._fresh(f".baseptr.{class_name}"),
        )

    # ------------------------------------------------------ call/attr helpers

    def lookup_field_index(self, info: ClassInfo, name: str) -> Optional[int]:
        """Return the slot index for ``name`` on the class, or None."""
        name = _mangle_private_name(info.name, name)
        field_names = _classgen_effective_field_names(info)
        i = 0
        while i < len(field_names):
            if _classgen_str_eq(field_names[i], name):
                return i
            i += 1
        # Phase 3: also check direct bases for inherited field slots.
        for b in info.bases_ast:
            if _is_ast_node(b, Name):
                # Try local module first.
                base_info = self.classes.get(b.ident)
                if base_info is not None:
                    idx = self.lookup_field_index(base_info, name)
                    if idx is not None:
                        return idx

                # Cross-module lookup: check the native exports registry.
                # Use the module name from the ClassInfo if it's an extern class.
                native_table = (
                    getattr(self.parent, "_native_module_exports", None) or {}
                )
                owning_mod = getattr(info, "owning_module", None)
                if owning_mod is not None:
                    rem_info = native_table.get(owning_mod, {}).get(b.ident)
                    if isinstance(rem_info, dict) and rem_info.get("kind") == "class":
                        f_names = rem_info.get("field_names", ())
                        if name in f_names:
                            return f_names.index(name)
        return None

    def _derives_from(self, info: ClassInfo, base_name: str) -> bool:
        """True if ``base_name`` is a transitive base of ``info``.

        Mirrors :meth:`lookup_class_attr`'s base walk. Python forbids
        inheritance cycles, so no visited-set is needed.
        """
        for b in info.bases_ast:
            if _is_ast_node(b, Name):
                if b.ident == base_name:
                    return True
                base_info = self.classes.get(b.ident)
                if base_info is not None and self._derives_from(base_info, base_name):
                    return True
        return False

    def class_attr_overridden_by_subclass(
        self, info: ClassInfo, attr_name: str
    ) -> bool:
        """True if any subclass of ``info`` declares its own ``attr_name``.

        When a subclass overrides the class attribute, a *static* load from
        ``info``'s per-class global is unsound for an instance receiver: the
        instance may actually be that subclass, whose override must win
        (CPython resolves ``self.<attr>`` / ``inst.<attr>`` for a class
        attribute via ``type(inst).__mro__``). The caller must then fall
        through to the runtime ``py_obj_getattr`` MRO lookup rather than the
        static fast path. The common no-override case keeps the fast path,
        so emitted IR is unchanged there.
        """
        attr_name = self.mangle_private_attr_name(info, attr_name)
        for other in self.classes.values():
            if other is info:
                continue
            if attr_name not in other.class_attrs:
                continue
            if self._derives_from(other, info.name):
                return True
        return False

    def method_overridden_by_subclass(
        self, info: ClassInfo, method_name: str
    ) -> bool:
        """True if a possible runtime subclass overrides ``method_name``.

        A type hint identifies a compatible base class, not the receiver's
        exact runtime class.  Direct instance-method dispatch is therefore
        sound only when the closed-world class graph has no subclass override
        below the hinted receiver class.
        """
        for other in self.classes.values():
            if other is info:
                continue
            if method_name not in other.methods:
                continue
            if self._derives_from(other, info.name):
                return True
        return False

    # Dict-inherited methods the runtime serves for classes that subclass the
    # builtin ``dict`` (PY_CLASS_FLAG_DICT_SUBCLASS): py_dict_subclass_getattr
    # in py_protocol.c binds each of these against the instance's backing dict
    # when the user class does not define an override. Keep this set in sync
    # with py_dict_subclass_getattr.
    _DICT_SUBCLASS_RUNTIME_METHODS = frozenset(
        ("get", "keys", "values", "items", "pop", "setdefault", "clear")
    )

    def lookup_class_attr(
        self,
        info: ClassInfo,
        name: str,
    ) -> Optional[tuple[ir.GlobalVariable, Type]]:
        name = self.mangle_private_attr_name(info, name)
        if name in info.class_attrs:
            return info.class_attrs[name]
        for b in info.bases_ast:
            if _is_ast_node(b, Name):
                base_info = self.classes.get(b.ident)
                if base_info is not None:
                    found = self.lookup_class_attr(base_info, name)
                    if found is not None:
                        return found
        # Dict-subclass inherited methods (collections.Counter.get, ...) are
        # not statically declared anywhere — the ``dict`` base is dropped from
        # the native MRO — but the runtime resolves them per-instance via
        # py_dict_subclass_getattr. Report them as known-with-no-static-global
        # (``(None, DynType)``) so ``_class_attr_needs_runtime_lookup`` routes
        # ``obj.get(...)`` through py_obj_getattr + py_obj_call instead of
        # (a) statically mis-dispatching to an unrelated same-named method via
        # the closed-world any-class scan, or (b) falling into the CPython
        # py_cpy_* dispatch, which is a compile error under
        # ``--python-libpython=off``. emit_class_attr_load below checks the
        # None global and refuses to emit a static load for this sentinel.
        if name in self._DICT_SUBCLASS_RUNTIME_METHODS and self._class_subclasses_dict(
            info
        ):
            return (None, DynType(name="dyn"))
        return None

    def emit_class_attr_load(
        self,
        info: ClassInfo,
        attr_name: str,
    ) -> Optional[ir.Value]:
        found = self.lookup_class_attr(info, attr_name)
        if found is None:
            return None
        gv, _ty = found
        if gv is None:
            # dict-subclass runtime-served method sentinel — there is no
            # static class-attr global to load; let the caller fall through
            # to its runtime-lookup path.
            return None
        return self.parent.builder.load(
            gv,
            name=self._fresh(f"classattr.{info.name}.{attr_name}"),
        )

    def emit_class_attr_store(
        self,
        info: ClassInfo,
        attr_name: str,
        value: ir.Value,
        value_ty: Type,
    ) -> bool:
        attr_name = self.mangle_private_attr_name(info, attr_name)
        found = info.class_attrs.get(attr_name)
        if found is None:
            g_name = self._class_attr_global_name(info.name, attr_name)
            existing = self.parent.module.globals.get(g_name)
            if isinstance(existing, ir.GlobalVariable):
                gv = existing
            else:
                gv = ir.GlobalVariable(self.parent.module, _PTR, name=g_name)
                gv.linkage = "internal"
                gv.initializer = ir.Constant(_PTR, None)
            info.class_attrs[attr_name] = (gv, value_ty)
        else:
            gv, _ty = found
        obj = marshal.marshal_to_object(
            self.parent.builder,
            self.parent.module,
            self.parent.runtime,
            value,
            value_ty,
        )
        self.parent.builder.store(obj, gv)
        cls_ptr = self._load_class_object(
            info,
            f".cls.{info.name}.setattr",
        )
        self.parent.builder.call(
            self.parent.runtime["py_class_setattr"],
            [cls_ptr, self._cname_ptr(attr_name), obj],
            name=self._fresh(f"classattr.{info.name}.{attr_name}.setattr.rc"),
        )
        return True

    def class_global(self, class_name: str) -> Optional[ir.GlobalVariable]:
        info = self.classes.get(class_name)
        if info is None:
            return None
        return info.global_var

    # ------------------------------------------------------ self.attr emit

    def emit_self_attr_load(
        self, info: ClassInfo, attr_name: str, self_val: ir.Value
    ) -> ir.Value:
        """Emit a ``self.<attr>`` load inside a method body.

        If the attribute name matches a declared field we go through
        ``py_instance_get_field``. Otherwise fall back to
        ``py_obj_getattr``.
        """
        builder = self.parent.builder
        runtime = self.parent.runtime
        idx = self.lookup_field_index(info, attr_name)
        if idx is not None:
            result = builder.call(
                runtime["py_instance_get_field"],
                [self_val, ir.Constant(_I32, idx)],
                name=self._fresh(f"self.{attr_name}"),
            )
            # The runtime getter returns a new reference even when the field
            # has a dynamic type. Record the emitted owner's provenance.
            self.parent._note_owned_object_value(result)
            return result
        # A subclass override must win: ``self`` may be a subclass instance
        # even inside a base-class method. Only take the static class-attr
        # load when no subclass redeclares the attribute; otherwise fall
        # through to the runtime MRO lookup.
        if not self.class_attr_overridden_by_subclass(info, attr_name):
            class_attr = self.emit_class_attr_load(info, attr_name)
            if class_attr is not None:
                return class_attr
        runtime_attr_name = self.mangle_private_attr_name(info, attr_name)
        name_ptr = self._cname_ptr(runtime_attr_name)
        result = builder.call(
            runtime["py_obj_getattr"],
            [self_val, name_ptr],
            name=self._fresh(f"self.attr.{attr_name}"),
        )
        self.parent._note_owned_object_value(result)
        return result

    def emit_self_attr_store(
        self,
        info: ClassInfo,
        attr_name: str,
        self_val: ir.Value,
        value: ir.Value,
    ) -> Optional[ir.Value]:
        """Emit ``self.<attr> = value``."""
        builder = self.parent.builder
        runtime = self.parent.runtime
        idx = self.lookup_field_index(info, attr_name)
        if idx is not None:
            builder.call(
                runtime["py_instance_set_field"],
                [self_val, ir.Constant(_I32, idx), value],
            )
            return None
        runtime_attr_name = self.mangle_private_attr_name(info, attr_name)
        name_ptr = self._cname_ptr(runtime_attr_name)
        return builder.call(
            runtime["py_obj_setattr"],
            [self_val, name_ptr, value],
            name=self._fresh(f"self.setattr.{attr_name}"),
        )

    # ------------------------------------------------------ isinstance

    def emit_isinstance(self, obj_val: ir.Value, class_name: str) -> ir.Value:
        """Emit ``py_isinstance(obj, class_global)`` returning i1."""
        builder = self.parent.builder
        runtime = self.parent.runtime
        info = self.classes.get(class_name)
        if info is None:
            raise NotImplementedError(
                f"isinstance: class {class_name!r} not found in module"
            )
        cls_ptr = self._load_class_object(info, f".cls.{class_name}")
        res_i64 = builder.call(
            runtime["py_isinstance"],
            [obj_val, cls_ptr],
            name=self._fresh(f"isinstance.{class_name}"),
        )
        return builder.icmp_signed(
            "!=", res_i64, ir.Constant(_I64, 0), name=self._fresh("isinstance.i1")
        )

    # ------------------------------------------------------ super()

    def emit_super_start_class(
        self,
        self_val: ir.Value,
        receiver_is_class: bool = False,
    ) -> ir.Value:
        """Return the runtime start class for zero-argument ``super()``.

        In an instance method the first argument is an instance, so the
        start class is ``self.__class__``. In a classmethod the first
        argument is already the current class object, so using
        ``cls.__class__`` would incorrectly start lookup from the
        metaclass.
        """
        if receiver_is_class:
            return self_val
        builder = self.parent.builder
        runtime = self.parent.runtime
        cls_name_ptr = self._cname_ptr("__class__")
        return builder.call(
            runtime["py_obj_getattr"],
            [self_val, cls_name_ptr],
            name=self._fresh(".super.start"),
        )

    def emit_super_lookup_from_start(
        self,
        enclosing: ClassInfo,
        start_cls: ir.Value,
        method_name: str,
    ) -> ir.Value:
        """Emit ``py_super_lookup(start_cls, from_cls, method_name)``."""
        builder = self.parent.builder
        runtime = self.parent.runtime
        from_cls = builder.load(enclosing.global_var, name=self._fresh(".super.from"))
        name_ptr = self._cname_ptr(method_name)
        return builder.call(
            runtime["py_super_lookup"],
            [start_cls, from_cls, name_ptr],
            name=self._fresh(f"super.{method_name}"),
        )

    def emit_super_lookup(
        self,
        enclosing: ClassInfo,
        self_val: ir.Value,
        method_name: str,
        receiver_is_class: bool = False,
    ) -> ir.Value:
        """Emit ``py_super_lookup(start_cls, from_cls, method_name)``.

        ``from_cls`` is the enclosing class at codegen time.
        ``start_cls`` is the instance's actual class — we read it off
        the instance's header, which is at offset 0 of the instance's
        memory area, but for Phase 3 simplicity we take the slow path
        and use the enclosing class's global as start_cls too. This is
        only correct when ``self`` is actually an instance of the
        enclosing class (and not a subclass that overrides); a future
        phase should emit a load of ``inst->cls``.
        """
        start_cls = self.emit_super_start_class(
            self_val,
            receiver_is_class,
        )
        return self.emit_super_lookup_from_start(enclosing, start_cls, method_name)

    # ------------------------------------------------------ instantiation

    def _class_bases_include_exception(self, info) -> bool:
        """True if any direct base of ``info`` is a builtin exception class
        (Exception, ValueError, ...). Used to give a no-``__init__`` exception
        subclass BaseException's constructor-args storage semantics."""
        # Cross-module classes declared from the native export table can have
        # an empty ``bases_ast``.  Use the shared base-name lookup so imported
        # ``class Error(Exception): pass`` definitions retain BaseException's
        # constructor-args semantics too.
        for name in self._class_declared_base_names(info):
            if _builtin_exception_tag_for_base_name(name) is not None:
                return True
        return False

    def _class_declared_base_names(self, info) -> tuple:
        """Base-class names of ``info``: AST bases when present, else the
        cross-module native-exports entry for an extern class.

        Extern classes declared through ``declare_extern_class`` may carry an
        empty ``bases_ast`` (only the ``_ensure_class_type_registered`` path
        materializes Name stubs from the export's ``base_names``), so fall
        back to the exports registry keyed by ``owning_module`` — mirrors the
        cross-module lookup in :meth:`lookup_field_index`."""
        names: list[str] = []
        for base_expr in getattr(info, "bases_ast", ()) or ():
            name = getattr(base_expr, "ident", None)
            if name is not None:
                names.append(name)
        if names:
            return tuple(names)
        owning_mod = getattr(info, "owning_module", None)
        if owning_mod is None:
            return ()
        native_table = getattr(self.parent, "_native_module_exports", None) or {}
        export_name = getattr(info, "export_class_name", None) or info.name
        rem_info = native_table.get(owning_mod, {}).get(export_name)
        if isinstance(rem_info, dict) and rem_info.get("kind") == "class":
            return tuple(rem_info.get("base_names", ()))
        return ()

    def _class_subclasses_dict(self, info) -> bool:
        """True if ``info`` (transitively) subclasses the builtin ``dict``.

        The frontend drops the ``dict`` base from the native MRO (dict is not a
        user PyClassObject), so a class such as collections.Counter/OrderedDict/
        defaultdict is created as a plain instance with no dict item storage or
        methods. We mark such classes with ``py_class_mark_dict_subclass`` so
        the runtime routes dict-inherited operations (__setitem__/__getitem__/
        get/__missing__/__len__/__contains__) to a backing dict. Detection is
        transitive over user bases: a user class whose base is another
        dict-subclass user class is also a dict-subclass. Works for both local
        classes (AST bases) and extern/cross-module classes (exports registry
        ``base_names``) via :meth:`_class_declared_base_names`."""
        visited: set[str] = set()
        queue: list[str] = list(self._class_declared_base_names(info))
        native_table = getattr(self.parent, "_native_module_exports", None) or {}
        while queue:
            name = queue.pop(0)
            if name in visited:
                continue
            visited.add(name)
            if name == "dict":
                return True
            base_info = self.classes.get(name)
            if base_info is not None:
                for pname in self._class_declared_base_names(base_info):
                    queue.append(pname)
                continue
            # Base class not registered locally: consult the cross-module
            # exports registry directly (first module exporting that name).
            for mod_exports in native_table.values():
                rem_info = mod_exports.get(name)
                if isinstance(rem_info, dict) and rem_info.get("kind") == "class":
                    for pname in rem_info.get("base_names", ()):
                        queue.append(pname)
                    break
        return False

    def emit_instantiate(self, class_name: str, arg_exprs, parent) -> ir.Value:
        """Emit ``MyClass(args)``.

        Allocates an instance via ``py_instance_new`` and invokes
        ``__init__`` if declared.
        """
        builder = parent.builder
        runtime = parent.runtime
        info = self.classes.get(class_name)
        if info is None:
            raise NotImplementedError(
                "instantiation: class " + class_name + " not found in module"
            )
        cls_ptr = self._load_class_object(info, ".cls." + class_name)
        inst = builder.call(
            runtime["py_instance_new"],
            [cls_ptr],
            name=self._fresh("inst." + class_name),
        )
        # ``py_instance_new`` returns an owned object, but an SSA pointer is
        # not a GC root.  Argument lowering and ``__init__`` may allocate, so
        # keep the new instance pinned until construction finishes.  Without
        # this, a relocating collection can leave ``self`` stale and later
        # class fields (notably IR symbol strings during pcc1 -> pcc2) read as
        # NULL even though every store succeeded inside ``__init__``.
        builder.call(runtime["pcc_gc_pin"], [inst])
        init_info = info
        init_fn = info.init_fn
        if init_fn is None:
            init_fn = info.methods.get("__init__")
        if init_fn is None:
            direct_sym = self._method_symbol(info.name, "__init__")
            direct_fn = parent.module.globals.get(direct_sym)
            if direct_fn is not None:
                init_fn = direct_fn
        if init_fn is None:
            visited: set[str] = set()
            queue: list[str] = []
            for base_expr in info.bases_ast:
                if _is_ast_node(base_expr, Name) and base_expr.ident != "object":
                    queue.append(base_expr.ident)
            while queue and init_fn is None:
                base_name = queue.pop(0)
                if base_name in visited:
                    continue
                visited.add(base_name)
                base_info = self.classes.get(base_name)
                if base_info is None:
                    continue
                candidate = base_info.init_fn
                if candidate is None:
                    candidate = base_info.methods.get("__init__")
                if candidate is None:
                    direct_sym = self._method_symbol(base_info.name, "__init__")
                    direct_fn = parent.module.globals.get(direct_sym)
                    if direct_fn is not None:
                        candidate = direct_fn
                if candidate is not None:
                    init_info = base_info
                    init_fn = candidate
                    break
                for parent_expr in base_info.bases_ast:
                    if (
                        _is_ast_node(parent_expr, Name)
                        and parent_expr.ident != "object"
                    ):
                        queue.append(parent_expr.ident)
        init_ast_fd = self._find_method_def(init_info.name, "__init__")
        should_call_init = init_fn is not None
        init_called = False
        if not should_call_init and init_ast_fd is not None:
            should_call_init = True
        if not should_call_init and len(arg_exprs) > 0:
            should_call_init = True
        if should_call_init:
            # Marshal args to expected param types.
            init_args: list[ir.Value] = [inst]
            init_arg_type_texts: list[str] = ["ptr"]
            init_arg_ref_texts: list[str] = [_classgen_value_ref_text(inst)]
            # Skip `self` and the bare ``*`` kw-only separator when
            # walking declared params.
            declared = [
                a for a in (init_ast_fd.args[1:] if init_ast_fd else ()) if a.name != ""
            ]
            init_fn_args = getattr(init_fn, "args", ()) if init_fn is not None else ()
            init_param_types = init_info.init_param_types
            for i, arg_expr in enumerate(arg_exprs):
                declared_annotation = None
                if i < len(declared):
                    declared_annotation = declared[i].annotation
                arg_start_index = IRBuilder_current_instruction_count(builder)
                # Resolve the parameter's IR type BEFORE emitting the argument.
                # When the constructor takes the object projection for an `int`
                # field, the argument has to be emitted as an object too --
                # emitting it first yields an i64, and an i64 cannot carry a
                # value above 2**63-1, so `IntLit(span, ty, int(text, 0))` stored
                # 0.  Boxing that truncated i64 afterwards recovers nothing.
                expected_ir_ty = None
                param_index = i + 1
                if param_index < len(init_param_types):
                    expected_ir_ty = init_param_types[param_index]
                elif param_index < len(init_fn_args):
                    expected_ir_ty = _classgen_arg_type_or_none(
                        init_fn_args,
                        param_index,
                    )
                v = None
                if expected_ir_ty is not None and isinstance(
                    expected_ir_ty, ir.PointerType
                ):
                    arg_ty = getattr(arg_expr, "ty", None)
                    if isinstance(arg_ty, IntType):
                        v = parent._emit_as_object(arg_expr)
                if v is None:
                    v = _classgen_emit_arg_expr(
                        parent, arg_expr, declared_annotation
                    )
                if _classgen_value_is_null(v):
                    recovered_v = None
                    arg_ident = (
                        getattr(arg_expr, "ident", None)
                        if _is_ast_node(arg_expr, Name)
                        else None
                    )
                    if arg_ident is not None:
                        recovered_v = _classgen_current_name_load(
                            parent,
                            arg_ident,
                            expected_ir_ty,
                            declared_annotation,
                        )
                    elif getattr(arg_expr, "obj", None) is not None and getattr(
                        arg_expr,
                        "name",
                        None,
                    ):
                        recovered_v = _classgen_recover_attr_value(
                            self,
                            parent,
                            arg_expr,
                            expected_ir_ty,
                            declared_annotation,
                        )
                    if recovered_v is None:
                        recovered_v = _classgen_recover_call_value(
                            self,
                            parent,
                            arg_expr,
                            expected_ir_ty,
                            declared_annotation,
                        )
                    if recovered_v is not None:
                        v = recovered_v
                # The callee's ABI slot type is authoritative when it is
                # known: a sibling module that keeps ints raw exports
                # ``fd: int`` as ``i64`` even though THIS module boxes ints,
                # and the caller-side annotation heuristic below would
                # otherwise pass it a PyObject*.
                expected_is_object = (
                    expected_ir_ty is not None
                    and _classgen_ir_type_is_pointer(expected_ir_ty)
                )
                if (
                    not expected_is_object
                    and expected_ir_ty is None
                    and i < len(declared)
                ):
                    expected_is_object = _classgen_annotation_is_object_param(
                        parent,
                        declared[i].annotation,
                    )
                if expected_is_object:
                    v_ir_ty = _classgen_value_type_or_none(v)
                    if v_ir_ty is not None and not _classgen_ir_type_is_pointer(
                        v_ir_ty
                    ):
                        v = marshal.marshal_to_object(
                            builder, parent.module, runtime, v, arg_expr.ty
                        )
                elif declared_annotation is not None:
                    v_ir_ty = _classgen_value_type_or_none(v)
                    if (
                        expected_ir_ty is not None
                        and not _classgen_ir_type_is_pointer(expected_ir_ty)
                        and _classgen_ir_type_is_pointer(v_ir_ty)
                    ):
                        v = _classgen_unbox_into_scalar_slot(
                            parent, v, expected_ir_ty, declared_annotation
                        )
                    elif not _classgen_ir_types_match(v_ir_ty, expected_ir_ty):
                        v = parent._coerce(v, arg_expr.ty, declared_annotation)
                else:
                    v_ir_ty = _classgen_value_type_or_none(v)
                    if (
                        expected_ir_ty is not None
                        and not _classgen_ir_type_is_pointer(expected_ir_ty)
                    ):
                        # A sibling module's synthesized ``__init__`` carries the
                        # scalar ABI slot type but no source annotation; a boxed
                        # int/float/bool from this module must be unboxed into it
                        # (a negative literal is emitted as a boxed object in
                        # modules that keep ints as objects).
                        if _classgen_ir_type_is_pointer(v_ir_ty):
                            v = _classgen_unbox_into_scalar_slot(
                                parent, v, expected_ir_ty, arg_expr.ty
                            )
                    else:
                        # Untyped init param -> marshal to PyObject*.
                        v = marshal.marshal_to_object(
                            builder, parent.module, runtime, v, arg_expr.ty
                        )
                init_args.append(v)
                if expected_ir_ty is None:
                    expected_ir_ty = _PTR
                init_arg_type_texts.append(
                    _classgen_ir_type_text_or_ptr(expected_ir_ty)
                )
                arg_ref_text = _classgen_value_ref_text(v)
                bool_ref_text = _classgen_bool_literal_ref_text(
                    arg_expr,
                    expected_ir_ty,
                )
                if bool_ref_text:
                    arg_ref_text = bool_ref_text
                if (
                    _classgen_ref_text_is_null(arg_ref_text)
                    and _classgen_ir_type_text_or_ptr(expected_ir_ty) == "i1"
                ):
                    try:
                        arg_bool_value = arg_expr.value
                    except AttributeError:
                        arg_ref_text = "false"
                    else:
                        arg_ref_text = "true" if bool(arg_bool_value) else "false"
                arg_ident = getattr(arg_expr, "ident", None)
                if _classgen_ref_text_is_null(arg_ref_text) and arg_ident is not None:
                    param_ref_text = _classgen_current_param_ref_text(
                        parent,
                        arg_ident,
                    )
                    if param_ref_text:
                        arg_ref_text = param_ref_text
                if _classgen_ref_text_is_null(arg_ref_text):
                    recent_ref_text = _classgen_recent_value_ref_text(
                        builder,
                        arg_start_index,
                    )
                    if recent_ref_text:
                        arg_ref_text = recent_ref_text
                init_arg_ref_texts.append(arg_ref_text)
            # Fill in declared defaults for any positional parameters
            # the caller omitted (e.g. ``Config()`` with
            # ``__init__(self, x: int = 10, y: int = 20)``).
            for j in range(len(arg_exprs), len(declared)):
                arg = declared[j]
                arg_kind = getattr(arg, "kind", "pos")
                if arg_kind in ("*args", "**kwargs"):
                    # An unfilled ``*args`` / ``**kwargs`` is the empty
                    # case — pcc currently has no callee-side vararg
                    # bind in ``__init__`` lowering, so omit the extras
                    # entirely. The body of ``__init__`` runs without
                    # them, which matches the path ``PurePath('/x')``
                    # takes (no extra parts to join).
                    continue
                if not getattr(arg, "has_default", False):
                    raise NotImplementedError(
                        "instantiation: "
                        + class_name
                        + ".__init__ missing argument "
                        + arg.name
                        + " and has no default"
                    )
                arg_start_index = IRBuilder_current_instruction_count(builder)
                v = _classgen_emit_arg_expr(parent, arg.default, arg.annotation)
                expected_ir_ty = None
                param_index = j + 1
                if param_index < len(init_param_types):
                    expected_ir_ty = init_param_types[param_index]
                elif param_index < len(init_fn_args):
                    expected_ir_ty = _classgen_arg_type_or_none(
                        init_fn_args,
                        param_index,
                    )
                default_expr = arg.default
                if _classgen_value_is_null(v):
                    recovered_v = None
                    default_ident = (
                        getattr(default_expr, "ident", None)
                        if _is_ast_node(default_expr, Name)
                        else None
                    )
                    if default_ident is not None:
                        recovered_v = _classgen_current_name_load(
                            parent,
                            default_ident,
                            expected_ir_ty,
                            arg.annotation,
                        )
                    elif getattr(default_expr, "obj", None) is not None and getattr(
                        default_expr,
                        "name",
                        None,
                    ):
                        recovered_v = _classgen_recover_attr_value(
                            self,
                            parent,
                            default_expr,
                            expected_ir_ty,
                            arg.annotation,
                        )
                    if recovered_v is None:
                        recovered_v = _classgen_recover_call_value(
                            self,
                            parent,
                            default_expr,
                            expected_ir_ty,
                            arg.annotation,
                        )
                    if recovered_v is not None:
                        v = recovered_v
                # The callee's ABI slot type is authoritative when it is
                # known: a sibling module that keeps ints raw exports
                # ``fd: int`` as ``i64`` even though THIS module boxes ints,
                # and the caller-side annotation heuristic below would
                # otherwise pass it a PyObject*.
                expected_is_object = (
                    expected_ir_ty is not None
                    and _classgen_ir_type_is_pointer(expected_ir_ty)
                )
                if not expected_is_object and expected_ir_ty is None:
                    expected_is_object = _classgen_annotation_is_object_param(
                        parent,
                        arg.annotation,
                    )
                if expected_is_object:
                    v_ir_ty = _classgen_value_type_or_none(v)
                    if v_ir_ty is not None and not _classgen_ir_type_is_pointer(
                        v_ir_ty
                    ):
                        v = marshal.marshal_to_object(
                            builder, parent.module, runtime, v, arg.default.ty
                        )
                elif arg.annotation is not None:
                    v_ir_ty = _classgen_value_type_or_none(v)
                    if not _classgen_ir_types_match(v_ir_ty, expected_ir_ty):
                        v = parent._coerce(v, arg.default.ty, arg.annotation)
                else:
                    v = marshal.marshal_to_object(
                        builder, parent.module, runtime, v, arg.default.ty
                    )
                init_args.append(v)
                if expected_ir_ty is None:
                    expected_ir_ty = _PTR
                init_arg_type_texts.append(
                    _classgen_ir_type_text_or_ptr(expected_ir_ty)
                )
                arg_ref_text = _classgen_value_ref_text(v)
                bool_ref_text = _classgen_bool_literal_ref_text(
                    default_expr,
                    expected_ir_ty,
                )
                if bool_ref_text:
                    arg_ref_text = bool_ref_text
                if (
                    _classgen_ref_text_is_null(arg_ref_text)
                    and _classgen_ir_type_text_or_ptr(expected_ir_ty) == "i1"
                ):
                    try:
                        default_bool_value = default_expr.value
                    except AttributeError:
                        arg_ref_text = "false"
                    else:
                        arg_ref_text = "true" if bool(default_bool_value) else "false"
                if _classgen_ref_text_is_null(arg_ref_text):
                    recent_ref_text = _classgen_recent_value_ref_text(
                        builder,
                        arg_start_index,
                    )
                    if recent_ref_text:
                        arg_ref_text = recent_ref_text
                init_arg_ref_texts.append(arg_ref_text)
            if init_fn is None:
                # No __init__ was found in this class, in any pcc-known base
                # via the MRO walk above, or by direct symbol lookup. The
                # remaining bases are presumably builtins (Exception, etc.)
                # or extern classes whose init semantics live in the runtime
                # — synthesising a phantom call to
                # ``@user_<current_module>_<class>___init__`` produced an
                # undefined-symbol link error (the symbol is mangled with
                # the CURRENT module via ``_method_symbol`` and is never
                # emitted, since no body __init__ exists to lower).  This
                # capped the numpy auto-mode compile right after the
                # owned-flag cap closed (numpy.ma.mrecords.MAError /
                # numpy.testing._private.utils._Dummy). Skip the call: the
                # args' values were emitted from already-owned slots whose
                # lifecycle is managed by their slot ownership, so this is
                # safe.
                #
                # For an exception subclass with no user __init__ (e.g.
                # ``class PlainErr(Exception): pass``), the implicit
                # BaseException.__init__ stores the constructor args on the
                # instance — ``str(e)`` and ``e.args`` read them. Persist them
                # here so ``raise PlainErr("msg")`` keeps its message (without
                # this, str(e) -> "" since the raise path now instantiates
                # rather than calling py_exc_new_with_class with args[0]).
                # Investigation:
                # docs/investigations/python-class-init-phantom-symbol-link-fail.md
                if arg_exprs and self._class_bases_include_exception(info):
                    n_args = len(arg_exprs)
                    args_tuple = builder.call(
                        runtime["py_tuple_new"],
                        [ir.Constant(_I64, n_args)],
                        name=self._fresh("exc.args.tuple"),
                    )
                    arg_i = 0
                    for a_expr in arg_exprs:
                        a_obj = parent._emit_as_object(a_expr)
                        builder.call(
                            runtime["py_tuple_set_item"],
                            [args_tuple, ir.Constant(_I64, arg_i), a_obj],
                        )
                        arg_i += 1
                    args_name = parent._pooled_cstr_ptr("args", ".exc.args.name")
                    builder.call(
                        runtime["py_instance_setattr"],
                        [inst, args_name, args_tuple],
                    )
            else:
                _classgen_emit_discarded_call(
                    builder,
                    init_fn,
                    init_args,
                    init_arg_type_texts,
                    init_arg_ref_texts,
                    init_info.init_returns_void,
                )
                init_called = True
        builder.call(
            runtime["pcc_gc_unpin"],
            [parent._value_available_at_insertion_point(inst)],
        )
        if init_called:
            # A user ``__init__`` reports Python exceptions through TLS even
            # when its native return value is discarded.  Check only after
            # removing the construction pin, and release the owned, failed
            # instance on the error edge before entering the surrounding
            # try/except or function error epilogue.
            parent._emit_post_call_err_check(
                None,
                release_on_error=(inst,),
            )
        # Producer-side proof for the immediately enclosing expression only.
        # Consumers clear the slot before emitting a candidate and compare by
        # SSA identity, so aliases, factories, phi nodes, and imported/native
        # construction paths cannot inherit this provenance accidentally.
        parent._last_fresh_direct_native_ctor_value = inst
        return inst

    def _find_method_def(self, class_name: str, method_name: str):
        # Prefer the expanded ClassDef when we synthesized extras via
        # @dataclass etc., so callers see the synthetic methods.
        # ``in`` + subscript instead of ``dict.get`` throughout this
        # method: under the self-hosted compiler ``.get`` has returned
        # None for present keys (probe-proven 2026-08-01).
        info = None
        if class_name in self.classes:
            info = self.classes[class_name]
        if info is not None:
            for candidate_name, candidate_def in info.method_defs:
                if candidate_name == method_name:
                    return candidate_def
        if info is not None and info.expanded_cd is not None:
            for s in info.expanded_cd.body:
                if getattr(s, "name", None) == method_name:
                    return s
        # Cross-module extern class: consult the synthetic FuncDef
        # stubs registered by ``declare_extern_class`` when the class
        # isn't part of this module's AST.
        if info is not None and info.extern_method_defs:
            # ``in`` + subscript instead of ``dict.get``: under the
            # self-hosted compiler ``.get`` has returned None for present
            # keys (probe-proven 2026-08-01; same class as the recorded
            # dict.get mis-lowering pitfall).
            if method_name in info.extern_method_defs:
                synth = info.extern_method_defs[method_name]
                if synth is not None:
                    return synth
        for stmt in self.parent.ast_module.body:
            if (
                getattr(stmt, "body", None) is not None
                and getattr(stmt, "name", None) == class_name
            ):
                for s in stmt.body:
                    if getattr(s, "name", None) == method_name:
                        return s
        return None


def _classgen_unbox_into_scalar_slot(parent, value, expected_ir_ty, ty):
    """Unbox an object-shaped value into a sibling method's scalar ABI slot.

    ``marshal_from_object`` respects the *calling* module's int policy, so in a
    module that keeps ints as objects it returns the object unchanged; the
    callee's slot type is authoritative here (a raw-int module exports
    ``fd: int`` as ``i64``), so integer slots are unboxed explicitly.
    """
    if isinstance(expected_ir_ty, ir.IntType):
        as_i64 = parent._to_int64(value, ty)
        if expected_ir_ty.width == 64:
            return as_i64
        if expected_ir_ty.width < 64:
            return parent.builder.trunc(as_i64, expected_ir_ty, name=parent._fresh("init.arg.trunc"))
        return parent.builder.sext(as_i64, expected_ir_ty, name=parent._fresh("init.arg.sext"))
    return marshal.marshal_from_object(
        parent.builder, parent.module, parent.runtime, value, ty
    )


def _class_has_dataclass_decorator(cd: ClassDef) -> bool:
    for dec in cd.decorators:
        name = _simple_decorator_name(dec)
        if _is_dataclass_decorator_name(name):
            return True
        # ``@dataclass(...)`` with args: decorator is a Call whose
        # func is Name("dataclass") or Attr(Name("dataclasses"),"dataclass").
        if _is_call_node(dec):
            inner = _simple_decorator_name(dec.func)
            if _is_dataclass_decorator_name(inner):
                return True
    return False


def _class_has_valueclass_decorator(cd: ClassDef) -> bool:
    for dec in cd.decorators:
        name = _simple_decorator_name(dec)
        if _is_valueclass_decorator_name(name):
            return True
        if _is_call_node(dec):
            inner = _simple_decorator_name(dec.func)
            if _is_valueclass_decorator_name(inner):
                return True
    return False


def _dataclass_options(cd: ClassDef) -> dict:
    opts = {"frozen": False, "order": False}
    for dec in cd.decorators:
        if not _is_call_node(dec):
            continue
        inner = _simple_decorator_name(dec.func)
        if not _is_dataclass_decorator_name(inner):
            continue
        for name, value in dec.kwargs:
            if _classgen_str_eq(name, "frozen") and _is_ast_node(value, BoolLit):
                opts["frozen"] = bool(value.value)
            elif _classgen_str_eq(name, "order") and _is_ast_node(value, BoolLit):
                opts[name] = bool(value.value)
    return opts


def _mangle_private_name(class_name: str, name: str) -> str:
    if not name.startswith("__"):
        return name
    if name.endswith("__"):
        return name
    cls = class_name.lstrip("_")
    if not cls:
        return name
    return f"_{cls}{name}"


def _simple_decorator_name(dec) -> Optional[str]:
    """Return a bare decorator name (``"staticmethod"``, ``"property"``,
    ``"<name>.setter"``) or ``None`` if the decorator shape is out of
    the narrow Phase-3 subset."""
    if _is_name_node(dec):
        return dec.ident
    if _is_attr_node(dec) and _is_name_node(dec.obj):
        return f"{dec.obj.ident}.{dec.name}"
    if _is_call_node(dec):
        return _simple_decorator_name(dec.func)
    return None


def _node_kind_name(node) -> str:
    if node is None:
        return ""
    return type(node).__name__


def _classgen_annotation_or_none(node):
    try:
        return node.annotation
    except AttributeError:
        return None


def _is_ast_node(node, expected_types) -> bool:
    if node is None:
        return False
    if isinstance(expected_types, tuple):
        return any(_is_ast_node(node, et) for et in expected_types)
    if isinstance(node, expected_types):
        return True
    try:
        return _classgen_str_eq(type(node).__name__, expected_types.__name__)
    except AttributeError:
        return False


def _is_name_node(node) -> bool:
    return isinstance(node, Name) or _classgen_str_eq(_node_kind_name(node), "Name")


def _is_attr_node(node) -> bool:
    return isinstance(node, Attr) or _classgen_str_eq(_node_kind_name(node), "Attr")


def _is_call_node(node) -> bool:
    return isinstance(node, Call) or _classgen_str_eq(_node_kind_name(node), "Call")


def _is_dataclass_decorator_name(name: Optional[str]) -> bool:
    return _classgen_name_eq(name, "dataclass") or _classgen_name_eq(
        name, "dataclasses.dataclass"
    )


def _is_valueclass_decorator_name(name: Optional[str]) -> bool:
    return _classgen_name_eq(name, "valueclass") or _classgen_name_eq(
        name, "pcc.valueclass"
    )


def _slot_names_from_expr(expr: Expr) -> Optional[list[str]]:
    if _is_ast_node(expr, StrLit):
        return [expr.value]
    if _is_ast_node(expr, (TupleExpr, ListExpr)):
        out: list[str] = []
        for elem in expr.elems:
            if not _is_ast_node(elem, StrLit):
                return None
            out.append(elem.value)
        return out
    return None


def _funcdef_is_property_setter(fd: FuncDef) -> bool:
    for dec in fd.decorators:
        dname = _simple_decorator_name(dec)
        if _classgen_name_endswith(dname, ".setter"):
            return True
    return False


def _funcdef_is_property_deleter(fd: FuncDef) -> bool:
    for dec in fd.decorators:
        dname = _simple_decorator_name(dec)
        if _classgen_name_endswith(dname, ".deleter"):
            return True
    return False


__all__ = ["ClassLowering", "ClassLoweringError", "ClassInfo"]
