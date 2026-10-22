"""IR scaffold lowering for L1CodeGen.

This mixin owns the closed-world lowering for the owned IR builder
IRBuilder and ir.* constructor call sites used by pcc self-host paths.
"""
from __future__ import annotations

from typing import Optional

from pcc.ir.compat import ir

from pcc.frontends.python.codegen.self_module_contracts import (
    CLASS_LOWERING_RECEIVER_CONTRACT,
    IR_PROVIDER_BINDING_CONTRACT,
    IR_SCAFFOLD_CONTRACT,
    module_has_contract,
)
from pcc.frontends.python.export_meta import decode_type
from pcc.frontends.python.codegen.errors import L1CodegenError
from pcc.frontends.python.py_ast import (
    Arg,
    Attr,
    BoolLit,
    BoolType,
    Call,
    Expr,
    FloatLit,
    FloatType,
    ImportFrom,
    IntLit,
    IntType,
    ListExpr,
    Name,
    NoneLit,
    NoneType,
    RawPointerType,
    StrLit,
    StrType,
    TupleExpr,
)
from pcc.frontends.python.codegen.method_call_lowering import (
    _method_emit_ast_args,
    _method_pinned_arg_cleanup,
    _method_release_arg_provenance,
)


def _literal_fits_i64(value) -> bool:
    # ``value`` is unannotated so a literal beyond i64 stays an exact int.
    return value >= -(1 << 63) and value <= (1 << 63) - 1


_I1 = ir.IntType(1)
_I32 = ir.IntType(32)
_I64 = ir.IntType(64)
_DOUBLE = ir.DoubleType()
_VOID = ir.VoidType()
_CSTR = ir.IntType(8).as_pointer()


def _scaffold_emit_declared_call(
    host, symbol, ret_ty, arg_exprs, integer_positions=(), *,
    call_expr=None, owned_result=False,
):
    """Call a declared helper with its actual ABI and explicit result contract.

    Only a dispatcher for an ordinary Python object producer may request an
    owned result. A pointer-shaped prototype alone proves no ownership. The
    provider's raw/manual ABI metadata overrides that contract; scalar and
    value-payload results retain their actual physical representation.
    """
    exports = (host._native_module_exports or {}).get("pcc.ir.ir", {})
    exported_name = symbol[len("user_pcc_ir_ir_"):]
    info = exports.get(exported_name)
    if info is None and exported_name.startswith("IRBuilder_"):
        method_name = exported_name[len("IRBuilder_"):]
        class_info = exports.get("IRBuilder", {})
        for method in class_info.get("methods", ()):
            if method.get("name") == method_name:
                info = method
                break
    fn = host.module.globals.get(symbol)
    if not isinstance(fn, ir.Function):
        if isinstance(info, dict) and "param_types" in info:
            fn = host._declare_extern_user_function("pcc.ir.ir", exported_name, info)
        else:
            fn = host._declare_external_function(symbol, ret_ty, [_CSTR] * len(arg_exprs))
    actual_return_type = fn.function_type.return_type
    semantic_result = decode_type(info.get("return_ty")) if isinstance(info, dict) else None
    manual_result = (
        fn.name in host._manual_pointer_abi_functions
        or isinstance(info, dict) and bool(info.get("manual_pointer_abi", False))
    )
    managed_result = (
        owned_result and isinstance(actual_return_type, ir.PointerType)
        and not manual_result and not isinstance(semantic_result, RawPointerType)
        and not (semantic_result is not None and host._is_valueclass_payload_type(semantic_result))
    )
    sink = None if call_expr is None else host._slot_call_result_sink(call_expr)
    if sink is not None and isinstance(actual_return_type, ir.PointerType) and not managed_result:
        raise L1CodegenError("scaffold call has no managed result contract: " + symbol)
    output = sink if managed_result else None
    previous = None
    saved_cpy = None
    if managed_result:
        if output is None:
            output = host._new_slot_call_root("scaffold.result")
        previous = host._current_try_err_block()
        target = previous if previous is not None else host._ensure_fn_err_exit()
        saved_cpy = host._cpy_operand_cleanup_block
        roots = (output,) if sink is None else ()
        host._try_err_block = host._slot_call_cleanup_block(roots, target)
        host._cpy_operand_cleanup_block = host._try_err_block
    try:
        declared = []
        for index, argument in enumerate(arg_exprs):
            integer = index in integer_positions or isinstance(argument.ty, IntType)
            declared.append(Arg(name="arg" + str(index), annotation=IntType(name="int") if integer else None, default=None, kind="pos"))
        args, _types, provenance = _method_emit_ast_args(
            host, fn, "IR scaffold " + symbol, tuple(arg_exprs), declared,
            param_offset=0,
        )
        argument_cleanup = _method_pinned_arg_cleanup(provenance)
        result_error_target = host._current_try_err_block() if managed_result else None
        result_cpy_target = host._cpy_operand_cleanup_block if managed_result else None
        if managed_result:
            # Publication itself can fail before _call_user reaches its
            # ordinary pending-error check. Both errors must release the
            # same argument owners once, then clean up the result root.
            host._try_err_block = host._make_cpy_operand_cleanup_block(
                (), (), result_error_target, "scaffold.arguments.cleanup", argument_cleanup,
            )
            host._cpy_operand_cleanup_block = host._try_err_block
        result = host._call_user(
            fn, list(args), "" if isinstance(actual_return_type, ir.VoidType) else host._fresh("scaffold.call"),
            root_result=managed_result,
            pinned_arg_temps=() if managed_result else argument_cleanup,
            result_slot=output,
        )
        if managed_result:
            host._try_err_block = result_error_target
            host._cpy_operand_cleanup_block = result_cpy_target
        _method_release_arg_provenance(host, provenance)
        if managed_result:
            if sink is None:
                return host._take_slot_call_root(output)
            return host.builder.load(output, name=host._fresh("scaffold.current"))
        return ir.Constant(_CSTR, None) if isinstance(actual_return_type, ir.VoidType) else result
    finally:
        if managed_result:
            host._try_err_block = previous
            host._cpy_operand_cleanup_block = saved_cpy


def _scaffold_node_kind_name(node) -> str:
    try:
        return type(node).__name__
    except Exception:
        return ""


def _scaffold_str_eq(lhs: str, rhs: str) -> bool:
    return lhs == rhs


def _is_scaffold_attr(node) -> bool:
    if isinstance(node, Attr):
        return True
    return _scaffold_str_eq(_scaffold_node_kind_name(node), "Attr")


def _is_scaffold_name(node) -> bool:
    if isinstance(node, Name):
        return True
    return _scaffold_str_eq(_scaffold_node_kind_name(node), "Name")


def _is_scaffold_call(node) -> bool:
    if isinstance(node, Call):
        return True
    return _scaffold_str_eq(_scaffold_node_kind_name(node), "Call")


def _is_scaffold_list_or_tuple(node) -> bool:
    if isinstance(node, (ListExpr, TupleExpr)):
        return True
    kind = _scaffold_node_kind_name(node)
    if not (
        _scaffold_str_eq(kind, "ListExpr")
        or _scaffold_str_eq(kind, "TupleExpr")
    ):
        return False
    return True


class ScaffoldUnsupportedError(NotImplementedError):
    """Raised in ir_scaffold_mode='on' when codegen encounters an
    IRBuilder method or ``ir.X`` symbol that has not yet been migrated
    to native lowering (Path A / Issue 1).

    OFF mode never raises this — it falls back to ``py_cpy_*`` dispatch
    silently. The error surface only exists in ON mode so per-file
    migration can identify exactly which symbols still need coverage.
    """


# Set of ``IRBuilder`` instance methods that scaffold dispatch
# recognises. Membership only triggers detection (raise-or-route);
# whether a method is actually *implemented* is gated by
# ``_IR_SCAFFOLD_METHOD_IMPL`` below. Mirrors the surface listed in
# ``docs/issues/open-bootstrap-issues.md`` (Issue 4).
_IR_BUILDER_METHODS = frozenset(
    {
        # control flow positioning
        "position_at_end",
        "position_at_start",
        "position_before",
        "append_basic_block",
        # loads/stores
        "store",
        "load",
        "alloca",
        # branches / control flow
        "branch",
        "cbranch",
        "switch",
        "ret",
        "ret_void",
        "unreachable",
        # comparisons
        "icmp_signed",
        "icmp_unsigned",
        "fcmp_ordered",
        "fcmp_unordered",
        # arithmetic (int)
        "add",
        "sub",
        "mul",
        "sdiv",
        "srem",
        "udiv",
        "urem",
        # bitwise / logical
        "and_",
        "or_",
        "xor",
        "not_",
        "neg",
        "shl",
        "ashr",
        "lshr",
        # arithmetic (float)
        "fadd",
        "fsub",
        "fmul",
        "fdiv",
        "frem",
        "fneg",
        # float casts
        "fpext",
        "fptosi",
        "fptoui",
        "fptrunc",
        "sitofp",
        "uitofp",
        # pointer / cast
        "gep",
        "bitcast",
        "inttoptr",
        "ptrtoint",
        "sext",
        "zext",
        "trunc",
        # aggregate / phi / selection
        "phi",
        "select",
        "extract_value",
        "insert_value",
        "add_incoming",
        # exception handling
        "landingpad",
        "resume",
        "invoke",
        # atomic
        "fence",
        "atomic_rmw",
        "cmpxchg",
        "load_atomic",
        "store_atomic",
        "syscall6",
        # generic
        "call",
        "call4_i32",
        # Type methods (any ir.X type instance — routed by
        # _IR_UNAMBIGUOUS_METHODS regardless of receiver).
        "as_pointer",
    }
)

# Set of ``ir.X`` module-level symbols (types and value constructors)
# scaffold dispatch recognises. Mirrors the public class set in
# ``pcc/ir/ir.py`` — keep in sync if new types are exported.
_IR_MODULE_SYMBOLS = frozenset(
    {
        "IRBuilder",
        "IntType",
        "PointerType",
        "VoidType",
        "DoubleType",
        "FloatType",
        "HalfType",
        "X86FP80Type",
        "FP128Type",
        "ArrayType",
        "FunctionType",
        "Constant",
        "Function",
        "Module",
        "GlobalVariable",
        "IdentifiedStructType",
        "LiteralStructType",
        "Block",
        "Argument",
        "Context",
        "Value",
    }
)


# Set of methods/symbols whose scaffold lowering is currently
# implemented. Phase 2+ adds entries here as each method gets a
# native lowering rule and tests. Detection without implementation
# raises ScaffoldUnsupportedError so per-file migration sees a clean
# "method X needs lowering" signal.
# Table-driven IRBuilder method lowering (Phase 2/3).
# Maps method name → (extern return type, fixed positional arg count).
# All positional args are coerced to ``i8*`` opaque handles at the call
# site; kwargs (``name=``) are accepted but discarded — they're SSA
# naming hints, not semantically meaningful in the C runtime.
# Methods whose dispatch isn't a clean N-pointer-args shape (variadic
# ``call``/``gep``, op-string-prefixed ``icmp_*``, etc.) are handled by
# bespoke ``_emit_scaffold_<method>`` functions instead and excluded
# from this table.
_IR_SCAFFOLD_SIMPLE_METHODS: dict = {
    # Void-returning
    "store": ("void", 2),
    "ret_void": ("void", 0),
    "unreachable": ("void", 0),
    "branch": ("void", 1),
    "position_at_end": ("void", 1),
    "position_at_start": ("void", 1),
    "position_before": ("void", 1),
    "ret": ("void", 1),
    "cbranch": ("void", 3),
    "switch": ("ptr", 2),
    "fence": ("void", 1),
    "add_incoming": ("void", 2),
    # Pointer-returning (handle to an LLVM IR value)
    "load": ("ptr", 1),
    "alloca": ("ptr", 1),
    "bitcast": ("ptr", 2),
    "inttoptr": ("ptr", 2),
    "ptrtoint": ("ptr", 2),
    "sext": ("ptr", 2),
    "zext": ("ptr", 2),
    "trunc": ("ptr", 2),
    "sitofp": ("ptr", 2),
    "uitofp": ("ptr", 2),
    "fpext": ("ptr", 2),
    "fptosi": ("ptr", 2),
    "fptoui": ("ptr", 2),
    "fptrunc": ("ptr", 2),
    "add": ("ptr", 2),
    "sub": ("ptr", 2),
    "mul": ("ptr", 2),
    "sdiv": ("ptr", 2),
    "srem": ("ptr", 2),
    "udiv": ("ptr", 2),
    "urem": ("ptr", 2),
    "and_": ("ptr", 2),
    "or_": ("ptr", 2),
    "xor": ("ptr", 2),
    "shl": ("ptr", 2),
    "ashr": ("ptr", 2),
    "lshr": ("ptr", 2),
    "fadd": ("ptr", 2),
    "fsub": ("ptr", 2),
    "fmul": ("ptr", 2),
    "fdiv": ("ptr", 2),
    "frem": ("ptr", 2),
    "not_": ("ptr", 1),
    "neg": ("ptr", 1),
    "fneg": ("ptr", 1),
    "select": ("ptr", 3),
    "extract_value": ("ptr", 2),
    "icmp_signed": ("ptr", 3),
    "icmp_unsigned": ("ptr", 3),
    "fcmp_ordered": ("ptr", 3),
    "fcmp_unordered": ("ptr", 3),
    "atomic_rmw": ("ptr", 4),
    "cmpxchg": ("ptr", 5),
    "load_atomic": ("ptr", 3),
    "store_atomic": ("void", 4),
    "syscall6": ("ptr", 7),
    "invoke": ("ptr", 4),
    # Type method — receiver may be any ir.X type instance; routed
    # via _IR_UNAMBIGUOUS_METHODS with bespoke flexible-arity handler.
    "as_pointer": ("ptr", 0),
}

_IR_SCAFFOLD_METHOD_OPTIONAL_PARAMS: dict = {
    "load": ("name", "align"),
    "store": ("align",),
    "fence": ("syncscope",),
    "bitcast": ("name",),
    "inttoptr": ("name",),
    "ptrtoint": ("name",),
    "sext": ("name",),
    "zext": ("name",),
    "trunc": ("name",),
    "sitofp": ("name",),
    "uitofp": ("name",),
    "fpext": ("name",),
    "fptosi": ("name",),
    "fptoui": ("name",),
    "fptrunc": ("name",),
    "add": ("name",),
    "sub": ("name",),
    "mul": ("name",),
    "sdiv": ("name",),
    "srem": ("name",),
    "udiv": ("name",),
    "urem": ("name",),
    "and_": ("name",),
    "or_": ("name",),
    "xor": ("name",),
    "shl": ("name",),
    "ashr": ("name",),
    "lshr": ("name",),
    "not_": ("name",),
    "neg": ("name",),
    "fneg": ("name",),
    "fadd": ("name",),
    "fsub": ("name",),
    "fmul": ("name",),
    "fdiv": ("name",),
    "frem": ("name",),
    "select": ("name",),
    "extract_value": ("name",),
    "icmp_signed": ("name",),
    "icmp_unsigned": ("name",),
    "fcmp_ordered": ("name",),
    "fcmp_unordered": ("name",),
    "atomic_rmw": ("name",),
    "cmpxchg": ("name",),
    "load_atomic": ("name", "typ"),
    "syscall6": ("name",),
    "invoke": ("name",),
}

# Methods with variable arity that can be called on any receiver
# (not just ``self.builder.X``). ``append_basic_block`` accepts
# 0 or 1 positional ``name`` arg plus an optional ``name`` kwarg —
# bespoke handler picks an extern variant.
_IR_SCAFFOLD_VARIABLE_ARITY_METHODS = frozenset(
    {
        "append_basic_block",
    }
)


# Variadic / bespoke methods handled outside the simple table.
_IR_SCAFFOLD_BESPOKE_METHODS = frozenset(
    {
        "call",  # builder.call(fn, [args])
        "call4_i32",  # builder.call4_i32(fn, a0, a1, a2, raw_i32)
        "gep",  # builder.gep(ptr, [indices])
        "phi",  # builder.phi(ty)
        "add_case",  # SwitchInstr.add_case(value, block)
        "landingpad",  # builder.landingpad(ty)
        "append_basic_block",  # 0-1 positional + name kwarg
    }
)

# ir.X module-level constructors: same shape as IRBuilder methods but
# without a ``builder`` receiver. Maps name → (positional_arity,
# accepts_name_kwarg). When ``name=`` is present a separate
# ``..._named`` extern variant is emitted so the runtime side can
# distinguish the two surfaces. Variadic constructors
# (``Function``, ``LiteralStructType``) have their own bespoke
# handlers below.
_IR_SCAFFOLD_SIMPLE_SYMBOLS: dict = {
    # Types
    "IntType": (1, False),  # ir.IntType(width)
    "PointerType": (1, False),  # ir.PointerType(ty)
    "VoidType": (0, False),
    "DoubleType": (0, False),
    "FloatType": (0, False),
    "HalfType": (0, False),
    "X86FP80Type": (0, False),
    "FP128Type": (0, False),
    "ArrayType": (2, False),  # ir.ArrayType(elem_ty, count)
    # Values
    "Constant": (2, False),  # ir.Constant(ty, value)
    "GlobalVariable": (2, True),  # ir.GlobalVariable(module, ty, name=...)
    "Module": (0, True),  # ir.Module(name=...)
    "IRBuilder": (1, False),  # ir.IRBuilder(block)
    "IdentifiedStructType": (2, False),  # ir.IdentifiedStructType(ctx, name)
    "Context": (0, False),
}

# Variadic / bespoke ``ir.X`` constructors. ``Function`` already
# bespoke-handled below for the name= kwarg variant; ``FunctionType``
# is variadic in its param-types tuple.
_IR_SYMBOL_VARIADIC = frozenset({"FunctionType", "LiteralStructType"})

_IR_SCAFFOLD_BESPOKE_SYMBOLS = frozenset(
    {
        "Function",  # ir.Function(module, fn_ty, name=...)
        "Value",  # ir.Value(ty, ref)
        "LiteralStructType",  # ir.LiteralStructType([ty1, ty2, ...])
        "FunctionType",  # ir.FunctionType(return_ty, [param_tys...])
    }
)

_IR_SCAFFOLD_METHOD_IMPL: frozenset = frozenset(
    (
        "store",
        "ret_void",
        "unreachable",
        "branch",
        "position_at_end",
        "position_at_start",
        "position_before",
        "ret",
        "cbranch",
        "switch",
        "fence",
        "add_incoming",
        "load",
        "alloca",
        "bitcast",
        "inttoptr",
        "ptrtoint",
        "sext",
        "zext",
        "trunc",
        "sitofp",
        "uitofp",
        "fpext",
        "fptosi",
        "fptoui",
        "fptrunc",
        "add",
        "sub",
        "mul",
        "sdiv",
        "srem",
        "udiv",
        "urem",
        "and_",
        "or_",
        "xor",
        "shl",
        "ashr",
        "lshr",
        "fadd",
        "fsub",
        "fmul",
        "fdiv",
        "frem",
        "not_",
        "neg",
        "fneg",
        "select",
        "extract_value",
        "icmp_signed",
        "icmp_unsigned",
        "fcmp_ordered",
        "fcmp_unordered",
        "atomic_rmw",
        "cmpxchg",
        "load_atomic",
        "store_atomic",
        "syscall6",
        "invoke",
        "as_pointer",
        "call",
        "call4_i32",
        "gep",
        "phi",
        "add_case",
        "landingpad",
        "append_basic_block",
    )
)

_IR_SCAFFOLD_SYMBOL_IMPL: frozenset = frozenset(
    (
        "IntType",
        "PointerType",
        "VoidType",
        "DoubleType",
        "FloatType",
        "HalfType",
        "X86FP80Type",
        "FP128Type",
        "ArrayType",
        "Constant",
        "GlobalVariable",
        "Module",
        "IRBuilder",
        "IdentifiedStructType",
        "Context",
        "Function",
        "Value",
        "LiteralStructType",
        "FunctionType",
    )
)



class IrScaffoldLoweringMixin:
    def _scaffold_stmt_has_global_ir(self, stmt) -> bool:
        kind = _scaffold_node_kind_name(stmt)
        if kind == "Global":
            return "ir" in stmt.names
        if kind in ("FuncDef", "ClassDef"):
            return False
        for field in ("body", "else_body", "finally_body", "handlers"):
            for child in getattr(stmt, field, ()):
                if self._scaffold_stmt_has_global_ir(child):
                    return True
        return False

    def _scaffold_assignment_targets_ir(self, target) -> bool:
        if _is_scaffold_name(target):
            return target.ident == "ir"
        if _is_scaffold_attr(target):
            return self._scaffold_assignment_targets_ir(target.obj)
        for element in getattr(target, "elems", ()):
            if self._scaffold_assignment_targets_ir(element):
                return True
        return False

    def _scaffold_stmt_rebinds_ir(self, stmt) -> bool:
        kind = _scaffold_node_kind_name(stmt)
        if kind == "ImportFrom":
            return any((alias or imported) == "ir" for imported, alias in stmt.names)
        if kind == "Import":
            return any(
                (alias or imported.split(".")[0]) == "ir"
                for imported, alias in stmt.names
            )
        if kind in ("FuncDef", "ClassDef"):
            if stmt.name == "ir":
                return True
            if kind == "FuncDef":
                return any(self._scaffold_stmt_has_global_ir(inner) for inner in stmt.body)
            return False
        if kind == "Assign":
            return any(self._scaffold_assignment_targets_ir(target) for target in stmt.targets)
        if kind == "AugAssign":
            return self._scaffold_assignment_targets_ir(stmt.target)
        if kind == "Delete":
            return any(self._scaffold_assignment_targets_ir(target) for target in stmt.targets)
        if kind == "For":
            if self._scaffold_assignment_targets_ir(stmt.target):
                return True
        if kind == "With":
            for _manager, target in stmt.items:
                if target is not None and self._scaffold_assignment_targets_ir(target):
                    return True
        if kind == "ExprStmt":
            call = getattr(stmt, "expr", None)
            func = getattr(call, "func", None)
            if (
                _is_scaffold_call(call)
                and _is_scaffold_name(func)
                and func.ident in ("setattr", "delattr")
                and call.args
                and _is_scaffold_name(call.args[0])
                and call.args[0].ident == "ir"
            ):
                return True
        for field in ("body", "else_body", "finally_body", "handlers"):
            for child in getattr(stmt, field, ()):
                if self._scaffold_stmt_rebinds_ir(child):
                    return True
        return False

    def _scaffold_source_has_provider_binding(self) -> bool:
        """Whether this source declares an owned IR binding at module scope.

        This is only an admission filter for the migration. A provider import
        does not prove the type of every `builder` parameter or mutable field.
        Those sites still need receiver facts before this can replace the
        legacy scaffold matcher altogether.
        """
        cached = self._ir_scaffold_source_provider_binding
        if cached is not None:
            return cached
        found = module_has_contract(
            self.ast_module.name, IR_PROVIDER_BINDING_CONTRACT
        )
        unstable = False
        for statement in self.ast_module.body:
            owned_import = False
            if isinstance(statement, ImportFrom) and statement.module == "pcc.ir.compat":
                for imported, bound in statement.names:
                    if imported in ("ir", "ir_py", "ir_c") and (bound or imported) == "ir":
                        owned_import = True
            if isinstance(statement, ImportFrom) and statement.module == "pcc.ir":
                for imported, bound in statement.names:
                    if imported == "ir" and (bound or imported) == "ir":
                        owned_import = True
            if _scaffold_node_kind_name(statement) == "Import":
                for imported, bound in statement.names:
                    if imported == "pcc.ir.ir" and bound == "ir":
                        owned_import = True
            if owned_import:
                found = True
            elif self._scaffold_stmt_rebinds_ir(statement):
                unstable = True
        result = found and not unstable
        self._ir_scaffold_source_provider_binding = result
        return result

    def _scaffold_local_kind(self, name: str) -> str:
        fact = getattr(self, "_ir_builder_env_flags", {}).get(name)
        if fact is True:
            return "IRBuilder"
        if isinstance(fact, str):
            return fact
        function = getattr(self, "current_func_def", None)
        if (
            module_has_contract(
                self.ast_module.name, CLASS_LOWERING_RECEIVER_CONTRACT
            )
            and function is not None
        ):
            owner = self._scaffold_current_class_owner()
            # ClassLowering.emit_methods passes only its declared ir.Function
            # entries to _emit_method_body. The nested binder captures the
            # same method's `parent = self.parent` L1CodeGen value. The source
            # and direct-call shapes are guarded by the field-contract tests.
            if owner == "ClassLowering":
                if name == "fn" and function.name == "_emit_method_body":
                    if any(argument.name == "fn" for argument in function.args):
                        return "Function"
                if name == "parent" and function.name == "__nested_bind_method_arg":
                    if any(argument.name == "parent" for argument in function.args):
                        return "L1CodeGen"
            if (
                name == "parent"
                and function.name == "emit_instantiate"
                and owner == "ClassLowering"
            ):
                return "L1CodeGen"
            if name == "builder" and function.name == "_classgen_builder_call":
                return "IRBuilder"
            if name == "parent" and function.name.startswith("_classgen_"):
                for argument in function.args:
                    if argument.name == "parent":
                        return "L1CodeGen"
        return ""

    def _scaffold_field_owner_roots(self) -> dict:
        cached = self._ir_scaffold_field_owner_mro
        if cached is not None:
            return cached
        from pcc.frontends.python.codegen.layer1 import L1CodeGen
        from pcc.frontends.c.codegen.c_codegen import CCodeGenerator

        owners = {}
        for root, label in (
            (L1CodeGen, "L1CodeGen"),
            (CCodeGenerator, "CCodeGenerator"),
        ):
            for cls in root.__mro__:
                # Native class objects expose __name__ and __mro__, but not
                # __module__. The emitting ClassInfo provides its owning
                # source module separately at the decision point below.
                if cls.__name__ != "object":
                    previous = owners.get(cls.__name__)
                    if previous is not None and previous != label:
                        raise ValueError("ambiguous self-host mixin: " + cls.__name__)
                    owners[cls.__name__] = label
        self._ir_scaffold_field_owner_mro = owners
        return owners

    def _scaffold_current_class_owner(self) -> str:
        info = getattr(self, "current_class", None)
        if info is None:
            return ""
        module = getattr(info, "owning_module", None) or self.ast_module.name
        name = getattr(info, "export_class_name", None) or info.name
        if (
            name == "ClassLowering"
            and module_has_contract(module, CLASS_LOWERING_RECEIVER_CONTRACT)
        ):
            return "ClassLowering"
        if module != self.ast_module.name:
            return ""
        owner = self._scaffold_field_owner_roots().get(name, "")
        if owner == "L1CodeGen" and module.startswith("pcc.frontends.python.codegen."):
            return owner
        if owner == "CCodeGenerator" and module.startswith("pcc.frontends.c.codegen."):
            return owner
        return ""

    def _scaffold_field_receiver_kind(self, receiver: Expr) -> str:
        if not _is_scaffold_attr(receiver):
            return ""
        base = receiver.obj
        owner = self._scaffold_current_class_owner()
        if _is_scaffold_name(base) and base.ident == "self":
            if receiver.name == "builder" and owner in (
                "L1CodeGen", "CCodeGenerator"
            ):
                return "IRBuilder"
            if receiver.name == "function" and owner == "CCodeGenerator":
                return "Function"
            if receiver.name == "current_function" and owner == "L1CodeGen":
                return "Function"
            if receiver.name == "parent" and owner == "ClassLowering":
                return "L1CodeGen"
        base_kind = (
            self._scaffold_local_kind(base.ident)
            if _is_scaffold_name(base)
            else self._scaffold_field_receiver_kind(base)
        )
        if not base_kind and _is_scaffold_call(base):
            from pcc.frontends.python.codegen.hoist_boxing import CELL_READ

            function = getattr(self, "current_func_def", None)
            captures = (() if function is None else
                        self._hoisted_capture_params.get(function.name, ()))
            if (_is_scaffold_name(base.func) and base.func.ident == CELL_READ
                    and len(base.args) == 3 and not base.kwargs
                    and _is_scaffold_name(base.args[0])
                    and _scaffold_node_kind_name(base.args[1]) == "StrLit"
                    and base.args[1].value == base.args[0].ident
                    and _scaffold_node_kind_name(base.args[2]) == "BoolLit"
                    and base.args[2].value
                    and base.args[0].ident in captures):
                # Hoisting changes a verified captured value into an internal
                # free-cell read. Preserve only its existing receiver fact;
                # the impossible source identifier, exact intrinsic shape,
                # and hoister's capture table must all agree. The original
                # expression is still emitted, including bound/owner checks.
                base_kind = self._scaffold_local_kind(base.args[0].ident)
        if receiver.name == "builder" and base_kind == "L1CodeGen":
            return "IRBuilder"
        if receiver.name == "current_function" and base_kind == "L1CodeGen":
            return "Function"
        if receiver.name == "function" and base_kind == "IRBuilder":
            return "Function"
        return ""

    def _scaffold_kind_has_method(self, kind: str, member: str) -> bool:
        if member == "add_incoming":
            return kind == "PhiInstr"
        if member == "add_case":
            return kind == "SwitchInstr"
        if member == "append_basic_block":
            return kind in ("IRBuilder", "Function")
        if member == "as_pointer":
            return kind in ("IntType", "PointerType", "FunctionType")
        return kind == "IRBuilder"

    def _scaffold_result_class_for_call(self, expr: Expr) -> str:
        if not _is_scaffold_call(expr) or not _is_scaffold_attr(expr.func):
            return ""
        attr = expr.func
        if _is_scaffold_name(attr.obj) and attr.obj.ident == "ir":
            if self._scaffold_receiver_admitted(attr.obj, attr.name):
                symbol = self._ir_module_symbol_target(attr)
                if symbol in ("IRBuilder", "IntType", "Function", "FunctionType"):
                    return symbol
            return ""
        if not self._scaffold_receiver_admitted(attr.obj, attr.name):
            return ""
        if self._ir_scaffold_target(attr) is None:
            return ""
        if attr.name == "phi":
            return "PhiInstr"
        if attr.name == "switch":
            return "SwitchInstr"
        if attr.name == "append_basic_block":
            return "Block"
        if attr.name == "as_pointer":
            return "PointerType"
        return ""

    def _scaffold_receiver_admitted(self, receiver: Expr, member: str = "") -> bool:
        if self._scaffold_receiver_has_nonprovider_class(receiver):
            return False
        if not self._scaffold_source_has_provider_binding():
            return False
        if _is_scaffold_name(receiver):
            function = getattr(self, "current_func_def", None)
            if function is not None:
                for argument in function.args:
                    if argument.name == receiver.ident:
                        return self._scaffold_kind_has_method(
                            self._scaffold_local_kind(receiver.ident), member
                        )
            if receiver.ident == "ir":
                return self.env.get("ir") is None
            return self._scaffold_kind_has_method(
                self._scaffold_local_kind(receiver.ident), member
            )
        if (
            _is_scaffold_attr(receiver)
            and receiver.name == "values"
            and _is_scaffold_name(receiver.obj)
            and receiver.obj.ident == "ir"
        ):
            return self._scaffold_receiver_admitted(receiver.obj, member)
        if _is_scaffold_attr(receiver):
            return self._scaffold_kind_has_method(
                self._scaffold_field_receiver_kind(receiver), member
            )
        if _is_scaffold_call(receiver):
            return self._scaffold_kind_has_method(
                self._scaffold_result_class_for_call(receiver), member
            )
        return False

    def _scaffold_receiver_has_nonprovider_class(self, receiver: Expr) -> bool:
        """Leave calls on a known ordinary class to normal Python lookup.

        A class hint is insufficient to *prove* an IRBuilder receiver: aliases
        and reassignment can invalidate it. It is, however, sufficient to
        reject the old spelling heuristic. Normal method dispatch will then
        evaluate the runtime receiver and any override/descriptor normally.
        """
        ty = getattr(receiver, "ty", None)
        module = getattr(ty, "module", "")
        if _scaffold_node_kind_name(ty) != "ClassType" or not module:
            return False
        if module not in ("pcc.ir.ir", "pcc.ir.compat"):
            # Single-module inference can preserve a qualified annotation as
            # a lexical shell (e.g. module="ir" for ir.Function) when provider
            # export schemas are absent. Resolve only its source-verified
            # namespace here; an alias is not receiver or class-existence
            # proof. Admission still requires the independent local/field
            # facts and capability checks above. Rebindings invalidate this
            # shared alias map, and ordinary foreign classes remain rejected.
            from pcc.frontends.python.pipeline_exports import annotation_module_bindings

            root, separator, suffix = module.partition(".")
            imported = annotation_module_bindings(self.ast_module).get(root)
            if imported is not None:
                module = imported + (separator + suffix if separator else "")
        return module not in ("pcc.ir.ir", "pcc.ir.compat")

    # Method names so specific to LLVM IR types that any receiver
    # passing the same name should route through scaffold dispatch.
    # Catches ``self.current_function.append_basic_block(...)``,
    # ``phi.add_incoming(...)``, ``ty.as_pointer()`` patterns without
    # full receiver-type tracking.
    _IR_UNAMBIGUOUS_METHODS = frozenset(
        {
            "append_basic_block",
            "add_incoming",
            "add_case",
            "as_pointer",
        }
    )

    def _ir_scaffold_candidate(self, attr: Attr) -> Optional[str]:
        """Identify a legacy syntactic candidate for observation.

        Returns the recognised method name when:
        - ``self.builder.METHOD`` / ``host.builder.METHOD`` /
          ``builder.METHOD`` / ``parent.builder.METHOD`` /
          ``self.parent.builder.METHOD``
          and METHOD is in ``_IR_BUILDER_METHODS`` (general case), OR
        - ``<any expr>.METHOD`` and METHOD is in
          ``_IR_UNAMBIGUOUS_METHODS`` (chained-receiver IR-specific
          methods).
        """
        if not _is_scaffold_attr(attr):
            return None
        if attr.name in self._IR_UNAMBIGUOUS_METHODS:
            return attr.name
        if attr.name not in _IR_BUILDER_METHODS:
            return None
        obj = attr.obj
        # ``self.builder.METHOD`` / ``host.builder.METHOD`` /
        # ``parent.builder.METHOD`` / ``self.parent.builder.METHOD``.
        # The ``host`` form covers contextual helper functions extracted
        # from layer1; the latter two cover ClassLowering helper methods
        # that emit through their owning L1CodeGen instance.
        if (
            _is_scaffold_attr(obj)
            and obj.name == "builder"
            and (
                (
                    _is_scaffold_name(obj.obj)
                    and obj.obj.ident in ("self", "host", "parent")
                )
                or (
                    _is_scaffold_attr(obj.obj)
                    and obj.obj.name == "parent"
                    and _is_scaffold_name(obj.obj.obj)
                    and obj.obj.obj.ident == "self"
                )
            )
        ):
            return attr.name
        # ``builder.METHOD`` (legacy local alias) and locals assigned
        # from ``ir.IRBuilder(...)``.
        if _is_scaffold_name(obj) and (
            obj.ident == "builder"
            or getattr(self, "_ir_builder_env_flags", {}).get(obj.ident, False)
        ):
            return attr.name
        return None

    def _ir_scaffold_target(self, attr: Attr) -> Optional[str]:
        candidate = self._ir_scaffold_candidate(attr)
        if (candidate is None and _is_scaffold_attr(attr)
                and attr.name in _IR_BUILDER_METHODS
                and self._scaffold_field_receiver_kind(attr.obj) == "IRBuilder"):
            # Representation-preserving field proofs can outlive the legacy
            # Name/Attr spelling (notably captured receivers in cells). The
            # caller still checks provider binding and receiver admission.
            candidate = attr.name
        if candidate is None or self._scaffold_receiver_has_nonprovider_class(attr.obj):
            return None
        return candidate

    def _ir_module_symbol_target(self, attr: Attr) -> Optional[str]:
        """Resolve an IR symbol or its owned ``ir.values`` alias.

        Returns the recognised symbol name (e.g. ``IntType``,
        ``PointerType``) if ``attr`` looks like ``ir.SYMBOL`` and
        SYMBOL is in the recognised ``ir.X`` set.
        """
        if not _is_scaffold_attr(attr):
            return None
        if self._scaffold_receiver_has_nonprovider_class(attr.obj):
            return None
        if attr.name not in _IR_MODULE_SYMBOLS:
            return None
        if _is_scaffold_name(attr.obj) and attr.obj.ident == "ir":
            return attr.name
        namespace = attr.obj
        if (
            _is_scaffold_attr(namespace)
            and namespace.name == "values"
            and _is_scaffold_name(namespace.obj)
            and namespace.obj.ident == "ir"
            and attr.name in ("Value", "Constant", "Argument", "Function", "GlobalVariable")
        ):
            return attr.name
        return None

    def _maybe_emit_ir_scaffold_symbol_value(self, attr: Attr):
        """Lower a bare ``ir.SYMBOL`` -- an ``ir.X`` that is *not* being called.

        Every other scaffold entry point matches the call shape ``ir.X(...)``,
        so an ``ir.X`` used as a value had no lowering at all: the compile-time
        alias registered for ``ir`` has no runtime binding, and the reference
        fell through to a plain name lookup.  ``pcc/frontends/c/codegen/c_codegen.py:84``
        (``IRBuilder = ir.IRBuilder``) is the one such site in the tree, and it
        is what pcc1 hit after the provider-init fix, as
        ``NameError: name 'ir' is not defined``.

        The provider module publishes each class object in
        ``@.class.pcc_ir_ir.<Name>``, which is what the isinstance
        lowering already reads; loading it here hands back the same class
        object ``from pcc.ir.ir import X`` would have bound, so a later
        call through the alias runs the real class.
        """
        if not self._ir_scaffold_enabled():
            # OFF mode leaves ``ir`` a real imported module object; normal
            # attribute lowering is correct there.
            return None
        if not self._scaffold_receiver_admitted(attr.obj, attr.name):
            return None
        symbol = self._ir_module_symbol_target(attr)
        if symbol is None:
            # Any other provider export (``isinstance(t, ir.Type)``,
            # ``ir.PhiInstr``, the ``ir.Undefined`` singleton in the C
            # codegen) loads through the provider's export table; a plain
            # name lookup of the elided ``ir`` binding is a NameError.
            if not (
                _is_scaffold_attr(attr)
                and _is_scaffold_name(attr.obj)
                and attr.obj.ident == "ir"
            ):
                return None
            provider = "pcc.ir.ir"
            info = (self._native_module_exports or {}).get(provider, {}).get(
                attr.name
            )
            if not isinstance(info, dict):
                return None
            return self._emit_native_module_export_value(provider, attr.name, info)
        g_name = ".class.pcc_ir_ir." + symbol
        existing = self.module.globals.get(g_name)
        if existing is None:
            gv = ir.GlobalVariable(self.module, _CSTR, name=g_name)
            gv.linkage = "external"
        else:
            gv = existing
        return self.builder.load(
            gv,
            name=self._fresh("ir.symbol." + symbol),
        )

    def _expr_is_ir_builder_ctor(self, expr: Expr) -> bool:
        return (
            _is_scaffold_call(expr)
            and _is_scaffold_attr(expr.func)
            and self._ir_module_symbol_target(expr.func) == "IRBuilder"
        )

    def _maybe_emit_ir_scaffold_call(
        self,
        expr: Call,
    ) -> Optional[ir.Value]:
        """Path A scaffold dispatch entry point.

        Called from ``_emit_method_call`` when ``ir_scaffold_mode ==
        'on'``. Returns the lowered ``ir.Value`` if the call site
        matches a scaffold pattern AND the targeted method is
        implemented. Raises :class:`ScaffoldUnsupportedError` when the
        pattern matches but the method has not yet been migrated.
        Returns ``None`` when the call site is unrelated to scaffold
        lowering (caller falls through to existing dispatch).
        """
        attr = expr.func
        if not _is_scaffold_attr(attr):
            return None
        if not self._scaffold_receiver_admitted(attr.obj, attr.name):
            return None

        method = self._ir_scaffold_target(attr)
        if method is not None:
            if method in _IR_SCAFFOLD_METHOD_IMPL:
                return self._emit_ir_scaffold_method(method, expr)
            raise ScaffoldUnsupportedError(
                f"IRBuilder method '{method}' has no scaffold lowering "
                f"yet (ir_scaffold_mode=on). Add it to "
                f"_IR_SCAFFOLD_METHOD_IMPL and implement "
                f"_emit_ir_scaffold_method, or compile this file with "
                f"--ir-scaffold=off until coverage lands."
            )

        symbol = self._ir_module_symbol_target(attr)
        if symbol is not None:
            if symbol in _IR_SCAFFOLD_SYMBOL_IMPL:
                return self._emit_ir_scaffold_symbol(symbol, expr)
            raise ScaffoldUnsupportedError(
                f"ir.{symbol} has no scaffold lowering yet "
                f"(ir_scaffold_mode=on). Add it to "
                f"_IR_SCAFFOLD_SYMBOL_IMPL and implement "
                f"_emit_ir_scaffold_symbol, or compile this file with "
                f"--ir-scaffold=off until coverage lands."
            )

        return None

    def _emit_ir_scaffold_method(
        self,
        method: str,
        expr: Call,
    ) -> ir.Value:
        """Dispatch implemented IRBuilder methods to their lowering.

        Simple-shape methods (fixed arity, all-pointer args, void or
        ptr return) come from ``_IR_SCAFFOLD_SIMPLE_METHODS``. Methods
        with non-trivial argument shapes (variadic ``call``/``gep``,
        ``phi``, ``landingpad``) get bespoke handlers below.
        """
        if method == "alloca":
            return self._emit_scaffold_alloca(expr)
        sig = _IR_SCAFFOLD_SIMPLE_METHODS.get(method)
        if sig is not None:
            return self._emit_scaffold_simple(method, expr, sig)
        if method == "call":
            return self._emit_scaffold_call(expr)
        if method == "call4_i32":
            return self._emit_scaffold_call4_i32(expr)
        if method == "gep":
            return self._emit_scaffold_gep(expr)
        if method == "phi":
            return self._emit_scaffold_phi(expr)
        if method == "add_case":
            return self._emit_scaffold_switch_add_case(expr)
        if method == "landingpad":
            return self._emit_scaffold_landingpad(expr)
        if method == "append_basic_block":
            return self._emit_scaffold_append_basic_block(expr)
        raise ScaffoldUnsupportedError(
            f"_emit_ir_scaffold_method dispatch for {method!r} not " f"yet wired"
        )

    def _scaffold_unfold_list_literal(
        self,
        expr: Expr,
        what: str,
    ) -> list:
        """Pull the literal element list out of a ``ListExpr`` /
        ``TupleExpr`` argument. Scaffold mode rejects non-literal
        sequences for variadic methods because there's no way to fold
        runtime-iterated args into a fixed-arity extern signature
        without reintroducing dynamic dispatch.
        """
        if _is_scaffold_list_or_tuple(expr):
            return list(expr.elems)
        raise ScaffoldUnsupportedError(
            f"scaffold {what} requires a literal list/tuple of args; "
            f"got {type(expr).__name__}. Inline the list at the call "
            f"site or factor the dynamic case out before migrating."
        )

    def _emit_scaffold_call(self, expr: Call) -> ir.Value:
        """Lower ``builder.call(fn, args)``.

        Two paths:
        - Literal args list/tuple → per-arity ``call<N>`` variant
          (preferred — fully static).
        - Non-literal args (Name pointing to a list) → ``call`` with
          a dyn-list handle — the call dispatch is static, the list
          construction may still use py_cpy_*. Relief valve for
          per-file migration when a literal isn't immediately
          available.

        ``pcc.ir.ir.IRBuilder.call`` is the natively-compiled
        target. Per-arity variants and the dyn fallback assume
        helpers in ``pcc.ir.ir`` (or its scaffold helpers
        module) named ``IRBuilder_call<N>`` / ``IRBuilder_call_dyn``;
        these are introduced when Task 24 lands the multi-file pull.
        """
        # llvmlite's signature is ``call(fn, args, name='', cconv=None,
        # tail=False, ...)``, so the SSA name hint may arrive as the third
        # positional -- ``self.builder.call(intrinsic, [lhs, rhs],
        # "ubsan.ovf")`` in c_codegen -- or as ``name=``.  It is passed to
        # the native ``IRBuilder_call<N>`` / ``IRBuilder_call_dyn``: dropping
        # it made every call result pcc1 emits anonymous, so pcc1's IR
        # differed from the host compiler's for the same module.  The other
        # llvmlite hints stay ignorable; a fourth positional would be
        # ``cconv``, which is not.
        pos_args = list(expr.args)
        name_expr: Expr | None = None
        if len(pos_args) == 3:
            name_expr = pos_args[2]
            pos_args = pos_args[:2]
        if len(pos_args) != 2:
            raise ScaffoldUnsupportedError(
                f"builder.call expects (fn, args[, name]); got "
                f"{len(expr.args)}"
            )
        for key, val in expr.kwargs:
            if key == "name":
                if name_expr is not None:
                    raise ScaffoldUnsupportedError(
                        "builder.call got multiple values for 'name'"
                    )
                name_expr = val
                continue
            if key not in self._SCAFFOLD_IGNORABLE_KWARGS:
                raise ScaffoldUnsupportedError(
                    f"builder.call(...) scaffold accepts only "
                    f"{sorted(self._SCAFFOLD_IGNORABLE_KWARGS)} kwargs; "
                    f"got {key!r}"
                )
        receiver = self._scaffold_to_handle(expr.func.obj)
        fn_handle = self._scaffold_to_handle(pos_args[0])
        args_expr = pos_args[1]
        if _is_scaffold_list_or_tuple(args_expr):
            arg_handles = [self._scaffold_to_handle(a) for a in args_expr.elems]
            name_h = (
                self._emit_literal_str("")
                if name_expr is None
                else self._scaffold_to_handle(name_expr)
            )
            n = len(arg_handles)
            if n > self._IR_SCAFFOLD_CALL_MAX_ARITY:
                raise ScaffoldUnsupportedError(
                    f"builder.call with {n} literal arguments: pcc.ir.ir defines "
                    f"IRBuilder_call0..IRBuilder_call{self._IR_SCAFFOLD_CALL_MAX_ARITY}"
                )
            extern_name = f"{self._IR_BUILDER_SYMBOL_PREFIX}call{n}"
            param_tys = [_CSTR, _CSTR] + [_CSTR] * n + [_CSTR]
            fn = self._declare_external_function(
                extern_name,
                _CSTR,
                param_tys,
            )
            return self.builder.call(
                fn,
                [receiver, fn_handle] + arg_handles + [name_h],
                name=self._fresh("scaffold.call"),
            )
        # Non-literal args: dynamic-list extern. Args list still
        # gets constructed via py_cpy_* so this is a partial win,
        # but better than full py_cpy_call dispatch on the call site
        # itself.
        list_handle = self._scaffold_to_handle(args_expr)
        name_h = (
            self._emit_literal_str("")
            if name_expr is None
            else self._scaffold_to_handle(name_expr)
        )
        fn = self._declare_external_function(
            f"{self._IR_BUILDER_SYMBOL_PREFIX}call_dyn",
            _CSTR,
            [_CSTR, _CSTR, _CSTR, _CSTR],
        )
        return self.builder.call(
            fn,
            [receiver, fn_handle, list_handle, name_h],
            name=self._fresh("scaffold.call_dyn"),
        )

    def _emit_scaffold_call4_i32(self, expr: Call) -> ir.Value:
        """Lower ``builder.call4_i32(fn, a0, a1, a2, raw_i32)``.

        The final operand is passed as an integer, not as an IR Value
        handle. This is intentionally narrow: it exists for hot
        traceback-frame emission where the last argument is always a
        source line number.
        """
        for key, _ in expr.kwargs:
            if key not in self._SCAFFOLD_IGNORABLE_KWARGS:
                raise ScaffoldUnsupportedError(
                    f"builder.call4_i32(...) scaffold accepts only "
                    f"{sorted(self._SCAFFOLD_IGNORABLE_KWARGS)} kwargs; "
                    f"got {key!r}"
                )
        if len(expr.args) != 5:
            raise ScaffoldUnsupportedError(
                f"builder.call4_i32 expects (fn, a0, a1, a2, raw_i32); "
                f"got {len(expr.args)}"
            )
        return _scaffold_emit_declared_call(
            self,
            f"{self._IR_BUILDER_SYMBOL_PREFIX}call4_i32",
            _CSTR,
            (expr.func.obj,) + expr.args,
            (5,),
            call_expr=expr, owned_result=True,
        )

    def _emit_scaffold_alloca(self, expr: Call) -> ir.Value:
        """Lower ``builder.alloca(ty, size=None, name="")``.

        The compiled ``pcc.ir.ir.IRBuilder.alloca`` method has
        four native parameters: ``self``, ``ty``, ``size`` and ``name``.
        Scaffold lowering must therefore fill Python defaults at the
        call site instead of treating ``name=`` as an ignorable SSA hint.
        """
        if not (1 <= len(expr.args) <= 3):
            raise ScaffoldUnsupportedError(
                f"builder.alloca expects 1-3 positional args; got " f"{len(expr.args)}"
            )
        ty_expr = expr.args[0]
        size_expr: Expr | None = None
        name_expr: Expr | None = None
        if len(expr.args) >= 2:
            size_expr = expr.args[1]
        if len(expr.args) >= 3:
            name_expr = expr.args[2]
        for key, val in expr.kwargs:
            if key == "size":
                if size_expr is not None:
                    raise ScaffoldUnsupportedError(
                        "builder.alloca got multiple values for 'size'"
                    )
                size_expr = val
                continue
            if key == "name":
                if name_expr is not None:
                    raise ScaffoldUnsupportedError(
                        "builder.alloca got multiple values for 'name'"
                    )
                name_expr = val
                continue
            raise ScaffoldUnsupportedError(
                f"builder.alloca(...) scaffold accepts only 'size' and "
                f"'name' kwargs; got {key!r}"
            )

        receiver = self._scaffold_to_handle(expr.func.obj)
        ty_h = self._scaffold_to_handle(ty_expr)
        if size_expr is None:
            size_h = self._emit_none_literal()
        else:
            size_h = self._scaffold_to_handle(size_expr)
        if name_expr is None:
            name_h = self._emit_literal_str("")
        else:
            name_h = self._scaffold_to_handle(name_expr)

        fn = self._declare_external_function(
            f"{self._IR_BUILDER_SYMBOL_PREFIX}alloca",
            _CSTR,
            [_CSTR, _CSTR, _CSTR, _CSTR],
        )
        return self.builder.call(
            fn,
            [receiver, ty_h, size_h, name_h],
            name=self._fresh("scaffold.alloca"),
        )

    def _emit_scaffold_gep(self, expr: Call) -> ir.Value:
        """Lower ``builder.gep(ptr, indices, inbounds=...)``.

        Literal indices list → per-arity ``gep<N>[_inbounds]`` variant.
        Non-literal → ``gep_dyn[_inbounds]``.
        """
        inbounds = False
        name_expr: Expr | None = None
        for key, val in expr.kwargs:
            if key == "name":
                # Passed on like builder.call's: a dropped hint left every
                # pcc1 GEP anonymous where the host compiler names it.
                name_expr = val
                continue
            if key in self._SCAFFOLD_IGNORABLE_KWARGS:
                continue
            if key == "inbounds":
                if isinstance(val, BoolLit):
                    inbounds = bool(val.value)
                    continue
                raise ScaffoldUnsupportedError(
                    "builder.gep(inbounds=...) scaffold requires a "
                    "literal True/False; dynamic value not supported"
                )
            raise ScaffoldUnsupportedError(
                f"builder.gep(...) scaffold accepts kwargs "
                f"name/align/.../inbounds; got {key!r}"
            )
        if len(expr.args) != 2:
            raise ScaffoldUnsupportedError(
                f"builder.gep expects (ptr, indices); got " f"{len(expr.args)}"
            )
        receiver = self._scaffold_to_handle(expr.func.obj)
        ptr_handle = self._scaffold_to_handle(expr.args[0])
        idx_expr = expr.args[1]
        suffix = "_inbounds" if inbounds else ""
        if _is_scaffold_list_or_tuple(idx_expr):
            idx_handles = [self._scaffold_to_handle(a) for a in idx_expr.elems]
            name_h = (
                self._emit_literal_str("")
                if name_expr is None
                else self._scaffold_to_handle(name_expr)
            )
            n = len(idx_handles)
            extern_name = f"{self._IR_BUILDER_SYMBOL_PREFIX}gep{n}{suffix}"
            param_tys = [_CSTR, _CSTR] + [_CSTR] * n + [_CSTR]
            fn = self._declare_external_function(
                extern_name,
                _CSTR,
                param_tys,
            )
            return self.builder.call(
                fn,
                [receiver, ptr_handle] + idx_handles + [name_h],
                name=self._fresh("scaffold.gep"),
            )
        list_handle = self._scaffold_to_handle(idx_expr)
        name_h = (
            self._emit_literal_str("")
            if name_expr is None
            else self._scaffold_to_handle(name_expr)
        )
        extern_name = f"{self._IR_BUILDER_SYMBOL_PREFIX}gep_dyn{suffix}"
        fn = self._declare_external_function(
            extern_name,
            _CSTR,
            [_CSTR, _CSTR, _CSTR, _CSTR],
        )
        return self.builder.call(
            fn,
            [receiver, ptr_handle, list_handle, name_h],
            name=self._fresh("scaffold.gep_dyn"),
        )

    def _emit_scaffold_phi(self, expr: Call) -> ir.Value:
        """Lower ``builder.phi(ty)`` (the IRBuilder.phi shape — incoming
        edges are added later via ``.add_incoming(...)``).

        For Phase 3, only the constructor call shape is recognised.
        ``add_incoming`` would route through the same scaffold (Phase
        16+ if it surfaces).
        """
        name_expr = None
        for key, val in expr.kwargs:
            if key == "name":
                name_expr = val
                continue
            if key != "name":
                raise ScaffoldUnsupportedError(
                    f"builder.phi(...) scaffold ignores kwargs "
                    f"except 'name'; got {key!r}"
                )
        if not (1 <= len(expr.args) <= 2):
            raise ScaffoldUnsupportedError(
                f"builder.phi expects 1-2 positional args; got " f"{len(expr.args)}"
            )
        if len(expr.args) == 2:
            if name_expr is not None:
                raise ScaffoldUnsupportedError(
                    "builder.phi got multiple values for 'name'"
                )
            name_expr = expr.args[1]
        receiver = self._scaffold_to_handle(expr.func.obj)
        ty_handle = self._scaffold_to_handle(expr.args[0])
        name_h = (
            self._emit_literal_str("")
            if name_expr is None
            else self._scaffold_to_handle(name_expr)
        )
        fn = self._declare_external_function(
            f"{self._IR_BUILDER_SYMBOL_PREFIX}phi",
            _CSTR,
            [_CSTR, _CSTR, _CSTR],
        )
        return self.builder.call(
            fn,
            [receiver, ty_handle, name_h],
            name=self._fresh("scaffold.phi"),
        )

    def _emit_scaffold_switch_add_case(self, expr: Call) -> ir.Value:
        """Lower ``SwitchInstr.add_case(value, target)``.

        ``add_case`` is intentionally handled outside the simple
        IRBuilder table: the receiver is a ``SwitchInstr``, and the
        native self-host symbol is therefore
        ``user_pcc_ir_ir_SwitchInstr_add_case``.
        """
        if expr.kwargs or len(expr.args) != 2:
            raise ScaffoldUnsupportedError(
                "SwitchInstr.add_case scaffold expects " "(int_value, target_block)"
            )
        int_value_expr = expr.args[0]
        if (
            _is_scaffold_call(int_value_expr)
            and _is_scaffold_attr(int_value_expr.func)
            and self._ir_module_symbol_target(int_value_expr.func) == "Constant"
            and len(int_value_expr.args) == 2
            and isinstance(int_value_expr.args[1].ty, (IntType, BoolType))
        ):
            return _scaffold_emit_declared_call(
                self,
                f"{self._IR_TOPLEVEL_SYMBOL_PREFIX}"
                "scaffold_SwitchInstr_add_case_i64",
                _VOID,
                (expr.func.obj, int_value_expr.args[1], expr.args[1]),
                (1,),
                call_expr=expr, owned_result=True,
            )
        receiver = self._scaffold_to_handle(expr.func.obj)
        target = self._scaffold_to_handle(expr.args[1])
        int_value = self._scaffold_to_handle(int_value_expr)
        fn = self._declare_external_function(
            f"{self._IR_TOPLEVEL_SYMBOL_PREFIX}SwitchInstr_add_case",
            _VOID,
            [_CSTR, _CSTR, _CSTR],
        )
        self.builder.call(fn, [receiver, int_value, target])
        return ir.Constant(_CSTR, None)

    def _emit_scaffold_append_basic_block(self, expr: Call) -> ir.Value:
        """Lower ``<receiver>.append_basic_block([name], name=...)``.

        Accepts 0 or 1 positional name plus an optional ``name``
        kwarg. Always emit the same extern (matching the natively-
        compiled ``IRBuilder.append_basic_block(name="")``); when
        no name is given, synthesize an empty-string handle as the
        default. Receiver may be IRBuilder or Function — both have
        the same method signature in pcc.ir.ir.
        """
        for key, _ in expr.kwargs:
            if key not in self._SCAFFOLD_IGNORABLE_KWARGS and key != "name":
                raise ScaffoldUnsupportedError(
                    f"append_basic_block(...) scaffold accepts only "
                    f"'name' (positional or kwarg); got {key!r}"
                )
        if len(expr.args) > 1:
            raise ScaffoldUnsupportedError(
                f"append_basic_block expects 0 or 1 positional args; "
                f"got {len(expr.args)}"
            )
        name_expr = None
        if len(expr.args) == 1:
            name_expr = expr.args[0]
        else:
            for key, val in expr.kwargs:
                if key == "name":
                    name_expr = val
        receiver = self._scaffold_to_handle(expr.func.obj)
        if name_expr is None:
            name_h = self._emit_literal_str("")
        else:
            name_h = self._scaffold_to_handle(name_expr)
        receiver_expr = expr.func.obj
        suffix = "scaffold_Function_append_basic_block"
        if _is_scaffold_name(receiver_expr) and self._scaffold_local_kind(
            receiver_expr.ident
        ) == "IRBuilder":
            suffix = "scaffold_IRBuilder_append_basic_block"
        elif _is_scaffold_attr(receiver_expr) and receiver_expr.name == "builder":
            suffix = "scaffold_IRBuilder_append_basic_block"
        fn = self._declare_external_function(
            f"{self._IR_TOPLEVEL_SYMBOL_PREFIX}{suffix}",
            _CSTR,
            [_CSTR, _CSTR],
        )
        return self.builder.call(
            fn,
            [receiver, name_h],
            name=self._fresh("scaffold.append_basic_block"),
        )

    def _emit_scaffold_landingpad(self, expr: Call) -> ir.Value:
        """Lower ``builder.landingpad(ty)``. Cleanup / catch clauses are
        added by separate methods (out of Phase 3 scope)."""
        name_expr = None
        cleanup_value = ir.Constant(_I1, 0)
        for key, val in expr.kwargs:
            if key == "name":
                name_expr = val
                continue
            if key == "cleanup":
                if isinstance(val, BoolLit):
                    cleanup_value = ir.Constant(_I1, 1 if val.value else 0)
                    continue
                raise ScaffoldUnsupportedError(
                    "builder.landingpad(cleanup=...) scaffold requires "
                    "a literal bool"
                )
            if key != "name":
                raise ScaffoldUnsupportedError(
                    f"builder.landingpad(...) scaffold ignores kwargs "
                    f"except 'name'/'cleanup'; got {key!r}"
                )
        if not (1 <= len(expr.args) <= 3):
            raise ScaffoldUnsupportedError(
                f"builder.landingpad expects 1-3 positional args; got "
                f"{len(expr.args)}"
            )
        if len(expr.args) >= 2:
            if name_expr is not None:
                raise ScaffoldUnsupportedError(
                    "builder.landingpad got multiple values for 'name'"
                )
            name_expr = expr.args[1]
        if len(expr.args) == 3:
            if isinstance(expr.args[2], BoolLit):
                cleanup_value = ir.Constant(
                    _I1,
                    1 if expr.args[2].value else 0,
                )
            else:
                raise ScaffoldUnsupportedError(
                    "builder.landingpad positional cleanup scaffold "
                    "requires a literal bool"
                )
        receiver = self._scaffold_to_handle(expr.func.obj)
        ty_handle = self._scaffold_to_handle(expr.args[0])
        name_h = (
            self._emit_literal_str("")
            if name_expr is None
            else self._scaffold_to_handle(name_expr)
        )
        fn = self._declare_external_function(
            f"{self._IR_BUILDER_SYMBOL_PREFIX}landingpad",
            _CSTR,
            [_CSTR, _CSTR, _CSTR, _I1],
        )
        return self.builder.call(
            fn,
            [receiver, ty_handle, name_h, cleanup_value],
            name=self._fresh("scaffold.landingpad"),
        )

    def _scaffold_to_handle(self, expr: Expr) -> ir.Value:
        """Lower ``expr`` to an opaque ``i8*`` handle for scaffold
        extern calls. Object-backed values pass through as pointers;
        native bools are boxed because scaffold helpers consume Python
        truth values, not raw integer bit patterns. Other native scalar
        handles retain the established bit-preserving representation.

        Different source types reach the C runtime as different opaque
        pointers — the C side treats them as black boxes (the runtime
        is the source of truth for what each handle represents).
        """
        value = self._emit_expr(expr)
        ty = value.type
        if isinstance(ty, ir.PointerType):
            return self.builder.bitcast(value, _CSTR)
        if isinstance(ty, ir.IntType):
            expr_ty = getattr(expr, "ty", None)
            if isinstance(expr_ty, BoolType):
                if ty.width == 32:
                    bit = value
                elif ty.width < 32:
                    bit = self.builder.zext(
                        value,
                        _I32,
                        name=self._fresh("scaffold.bool.i32"),
                    )
                else:
                    bit = self.builder.trunc(
                        value,
                        _I32,
                        name=self._fresh("scaffold.bool.i32"),
                    )
                return self.builder.call(
                    self.runtime["py_bool_from_bit"],
                    [bit],
                    name=self._fresh("scaffold.bool.box"),
                )
            return self.builder.inttoptr(value, _CSTR)
        if isinstance(ty, ir.DoubleType):
            # Bitcast double → i64 (same bit width), then inttoptr to
            # i8*. Preserves bits so the C runtime can re-interpret if
            # it knows the slot was originally a double.
            as_i64 = self.builder.bitcast(value, _I64)
            return self.builder.inttoptr(as_i64, _CSTR)
        if isinstance(ty, ir.VoidType):
            raise ScaffoldUnsupportedError("scaffold extern call argument is void")
        raise ScaffoldUnsupportedError(
            f"scaffold extern call argument has unsupported type "
            f"{ty} ({type(ty).__name__})"
        )

    # kwargs the simple dispatcher accepts and discards (no semantic
    # impact at scaffold level — the C runtime re-derives them from
    # types/contextual info). Loud-fail on any kwarg outside this set
    # so per-file migration sees the gap.
    _SCAFFOLD_IGNORABLE_KWARGS = frozenset(
        {
            "name",  # SSA naming hint
            "align",  # alignment hint on load/store
            "flags",  # FP / int flag bag
            "fastmath",  # FP fast-math bag
            "tail",  # tail-call hint on call
            "cconv",  # calling convention
        }
    )

    # Pcc-Python symbol mangling for cross-module references. The
    # natively-compiled pcc.ir.ir uses these names. Path A
    # routes scaffold dispatch through them so the produced IR
    # references the REAL functions provided by pcc.ir —
    # not synthetic placeholders.
    _IR_BUILDER_SYMBOL_PREFIX = "user_pcc_ir_ir_IRBuilder_"
    _IR_TOPLEVEL_SYMBOL_PREFIX = "user_pcc_ir_ir_"
    # ``pcc.ir.ir`` defines ``IRBuilder_call0`` .. ``IRBuilder_call<MAX>``.
    # A wider literal argument list must fail here, not as an undefined
    # symbol when Stage1 links pcc1.
    _IR_SCAFFOLD_CALL_MAX_ARITY = 9

    # Semantic Python-int operands, not physical i64 slots. Actual operand
    # types come from the declared callee, including explicit machine ABIs.
    _IR_SCAFFOLD_METHOD_PY_INT_PARAMS = {
        "load_atomic": (2,),
        "store_atomic": (3,),
        "extract_value": (1,),
    }

    def _emit_scaffold_simple(
        self,
        method: str,
        expr: Call,
        sig: tuple,
    ) -> ir.Value:
        """Lower a simple-shape IRBuilder method via table dispatch.

        Emits ``call <ret> @user_pcc_ir_ir_IRBuilder_<method>(
        builder, args...)`` — references the real natively-compiled
        method provided by ``pcc.ir.ir``. Multi-file compile
        (Phase 6 Task 24) pulls that file into the same link so the
        symbol is satisfied; without that, link would fail with
        undefined symbol — that's the next step's problem.

        Optional Python parameters are made explicit here. The native
        pcc-compiled method symbols use the full Python signature, so
        a source call like ``builder.load(ptr, name="x")`` must become
        ``IRBuilder_load(builder, ptr, "x", None)`` rather than a
        short ABI call with only the required operands.
        """
        return_kind, required_count = sig
        optional_params = _IR_SCAFFOLD_METHOD_OPTIONAL_PARAMS.get(
            method,
            (),
        )
        if not (
            required_count <= len(expr.args) <= required_count + len(optional_params)
        ):
            raise ScaffoldUnsupportedError(
                f"builder.{method} expects {required_count}"
                f"-{required_count + len(optional_params)} positional "
                f"args; got {len(expr.args)}"
            )

        required_args = expr.args[:required_count]
        optional_values: dict = {}
        for idx, arg in enumerate(expr.args[required_count:]):
            optional_values[optional_params[idx]] = arg

        for key, val in expr.kwargs:
            if key in optional_params:
                if key in optional_values:
                    raise ScaffoldUnsupportedError(
                        f"builder.{method} got multiple values for " f"{key!r}"
                    )
                optional_values[key] = val
                continue
            if key in self._SCAFFOLD_IGNORABLE_KWARGS:
                continue
            raise ScaffoldUnsupportedError(
                f"builder.{method}(...) scaffold accepts only "
                f"{list(optional_params)} kwargs; got {key!r}"
            )

        python_int_params = self._IR_SCAFFOLD_METHOD_PY_INT_PARAMS.get(method, ())
        argument_exprs = [expr.func.obj] + list(required_args)
        integer_positions = tuple(index + 1 for index in python_int_params)
        for param in optional_params:
            val = optional_values.get(param)
            if val is None:
                if param == "name":
                    argument_exprs.append(StrLit(expr.span, StrType(name="str"), ""))
                    continue
                if param in ("align", "syncscope", "typ"):
                    argument_exprs.append(NoneLit(expr.span, NoneType(name="None")))
                    continue
                raise ScaffoldUnsupportedError(
                    f"builder.{method} has no scaffold default for " f"{param!r}"
                )
            argument_exprs.append(val)

        ret_ty = _VOID if return_kind == "void" else _CSTR
        extern_name = self._IR_BUILDER_SYMBOL_PREFIX + method
        return _scaffold_emit_declared_call(
            self, extern_name, ret_ty, argument_exprs, integer_positions,
            # A void LLVM operation can still return an owned Python Value
            # (store/ret/branch). The actual provider ABI decides whether the
            # explicit object-result contract needs a root.
            call_expr=expr, owned_result=True,
        )

    def _emit_ir_scaffold_symbol(
        self,
        symbol: str,
        expr: Call,
    ) -> ir.Value:
        """Dispatch implemented ``ir.X`` symbols to their lowering.

        Simple-shape symbols (fixed arity, all-pointer args) come
        from ``_IR_SCAFFOLD_SIMPLE_SYMBOLS``. Variadic constructors
        (``Function``, ``LiteralStructType``) get bespoke handlers.
        """
        if symbol == "IntType":
            return self._emit_scaffold_int_type(expr)
        if symbol == "PointerType":
            return self._emit_scaffold_pointer_type(expr)
        if symbol == "ArrayType":
            return self._emit_scaffold_array_type(expr)
        if symbol == "Constant":
            return self._emit_scaffold_constant(expr)
        if symbol == "Value":
            return self._emit_scaffold_value(expr)
        if symbol == "IRBuilder":
            return self._emit_scaffold_irbuilder_ctor(expr)
        if symbol == "Context":
            return self._emit_scaffold_context_ctor(expr)
        if symbol == "IdentifiedStructType":
            return self._emit_scaffold_identified_struct_type(expr)
        sig = _IR_SCAFFOLD_SIMPLE_SYMBOLS.get(symbol)
        if sig is not None:
            arity, accepts_name = sig
            return self._emit_scaffold_simple_symbol(
                symbol,
                expr,
                arity,
                accepts_name,
            )
        if symbol == "Function":
            return self._emit_scaffold_function_ctor(expr)
        if symbol == "LiteralStructType":
            return self._emit_scaffold_literal_struct(expr)
        if symbol == "FunctionType":
            return self._emit_scaffold_function_type_ctor(expr)
        raise ScaffoldUnsupportedError(
            f"_emit_ir_scaffold_symbol dispatch for {symbol!r} not " f"yet wired"
        )

    def _emit_scaffold_int_type(self, expr: Call) -> ir.Value:
        if expr.kwargs or len(expr.args) != 1:
            raise ScaffoldUnsupportedError("ir.IntType expects one width arg")
        return _scaffold_emit_declared_call(
            self,
            f"{self._IR_TOPLEVEL_SYMBOL_PREFIX}scaffold_IntType",
            _CSTR,
            expr.args,
            (0,),
            call_expr=expr, owned_result=True,
        )

    def _emit_scaffold_pointer_type(self, expr: Call) -> ir.Value:
        if expr.kwargs or len(expr.args) != 1:
            raise ScaffoldUnsupportedError(
                "ir.PointerType scaffold expects one pointee arg"
            )
        pointee = self._scaffold_to_handle(expr.args[0])
        fn = self._declare_external_function(
            f"{self._IR_TOPLEVEL_SYMBOL_PREFIX}scaffold_PointerType",
            _CSTR,
            [_CSTR],
        )
        return self.builder.call(
            fn,
            [pointee],
            name=self._fresh("scaffold.PointerType"),
        )

    def _emit_scaffold_array_type(self, expr: Call) -> ir.Value:
        if expr.kwargs or len(expr.args) != 2:
            raise ScaffoldUnsupportedError(
                "ir.ArrayType scaffold expects (element, count)"
            )
        return _scaffold_emit_declared_call(
            self,
            f"{self._IR_TOPLEVEL_SYMBOL_PREFIX}scaffold_ArrayType",
            _CSTR,
            expr.args,
            (1,),
            call_expr=expr, owned_result=True,
        )

    def _emit_scaffold_constant(self, expr: Call) -> ir.Value:
        if expr.kwargs or len(expr.args) != 2:
            raise ScaffoldUnsupportedError("ir.Constant scaffold expects (ty, value)")
        value_expr = expr.args[1]
        if isinstance(value_expr, (IntLit, BoolLit)) or isinstance(value_expr.ty, (IntType, BoolType)):
            return _scaffold_emit_declared_call(
                self,
                f"{self._IR_TOPLEVEL_SYMBOL_PREFIX}scaffold_Constant_i64",
                _CSTR,
                expr.args,
                (1,),
                call_expr=expr, owned_result=True,
            )
        ty = self._scaffold_to_handle(expr.args[0])
        if isinstance(value_expr, NoneLit):
            fn = self._declare_external_function(
                f"{self._IR_TOPLEVEL_SYMBOL_PREFIX}scaffold_Constant_none",
                _CSTR,
                [_CSTR],
            )
            return self.builder.call(
                fn,
                [ty],
                name=self._fresh("scaffold.Constant"),
            )
        if isinstance(value_expr, FloatLit):
            value = self._emit_expr(value_expr)
            fn = self._declare_external_function(
                f"{self._IR_TOPLEVEL_SYMBOL_PREFIX}scaffold_Constant_f64",
                _CSTR,
                [_CSTR, _DOUBLE],
            )
            return self.builder.call(
                fn,
                [ty, value],
                name=self._fresh("scaffold.Constant"),
            )
        if isinstance(value_expr.ty, FloatType):
            raw = self._emit_expr(value_expr)
            value = self._to_double(raw, value_expr.ty)
            fn = self._declare_external_function(
                f"{self._IR_TOPLEVEL_SYMBOL_PREFIX}scaffold_Constant_f64",
                _CSTR,
                [_CSTR, _DOUBLE],
            )
            return self.builder.call(
                fn,
                [ty, value],
                name=self._fresh("scaffold.Constant"),
            )
        raw_value = self._emit_expr(value_expr)
        if isinstance(raw_value.type, ir.IntType):
            value = raw_value
            if raw_value.type.width < 64:
                value = self.builder.sext(
                    raw_value, _I64, name=self._fresh("scaffold.const.i64")
                )
            elif raw_value.type.width > 64:
                value = self.builder.trunc(
                    raw_value, _I64, name=self._fresh("scaffold.const.i64")
                )
            info = (self._native_module_exports or {}).get("pcc.ir.ir", {}).get("scaffold_Constant_i64")
            if isinstance(info, dict):
                fn = self._declare_extern_user_function("pcc.ir.ir", "scaffold_Constant_i64", info)
            else:
                fn = self._declare_external_function(
                    f"{self._IR_TOPLEVEL_SYMBOL_PREFIX}scaffold_Constant_i64",
                    _CSTR, [_CSTR, _CSTR],
                )
            # An imprecisely typed expression can still produce a machine
            # integer. Adapt that already-evaluated value once; reevaluating
            # its AST would duplicate side effects.
            self._gc_pin(ty)
            boxed = isinstance(fn.args[1].type, ir.PointerType)
            cleanup = ((ty, False),)
            if boxed:
                value = self.builder.call(self.runtime["py_int_from_i64"], [value])
                self._gc_pin(value)
                cleanup = cleanup + ((value, True),)
                self._emit_post_call_err_check(expr.span, pinned_release_on_error=cleanup)
            # This known Constant producer returns an object owner, unless
            # the defining provider explicitly declares a raw/manual or value
            # payload ABI. A pointer-shaped return alone is not that contract.
            semantic_result = decode_type(info.get("return_ty")) if isinstance(info, dict) else None
            managed_result = (
                isinstance(fn.function_type.return_type, ir.PointerType)
                and fn.name not in self._manual_pointer_abi_functions
                and not (isinstance(info, dict) and info.get("manual_pointer_abi", False))
                and not isinstance(semantic_result, RawPointerType)
                and not (semantic_result is not None and self._is_valueclass_payload_type(semantic_result))
            )
            sink = self._slot_call_result_sink(expr)
            if sink is not None and isinstance(fn.function_type.return_type, ir.PointerType) and not managed_result:
                raise L1CodegenError("scaffold call has no managed result contract: " + fn.name)
            output = sink if managed_result else None
            previous = self._current_try_err_block()
            saved_cpy = self._cpy_operand_cleanup_block
            if managed_result:
                # Both arguments are pinned before registering a local output.
                # _call_user stores into it immediately, before publication's
                # fallible lease checks, TLS checks, or argument retirement.
                if output is None:
                    output = self._new_slot_call_root("scaffold.result")
                target = previous if previous is not None else self._ensure_fn_err_exit()
                roots = (output,) if sink is None else ()
                self._try_err_block = self._slot_call_cleanup_block(roots, target)
                self._cpy_operand_cleanup_block = self._try_err_block
            try:
                result = self._call_user(
                    fn, [ty, value], self._fresh("scaffold.Constant"),
                    root_result=managed_result,
                    pinned_arg_temps=cleanup,
                    result_slot=output,
                )
                if boxed:
                    self._gc_unpin(value)
                    self._gc_release(value)
                self._gc_unpin(ty)
                if managed_result:
                    if sink is None:
                        return self._take_slot_call_root(output)
                    return self.builder.load(output, name=self._fresh("scaffold.current"))
                return result
            finally:
                self._try_err_block = previous
                self._cpy_operand_cleanup_block = saved_cpy
        if isinstance(raw_value.type, ir.DoubleType):
            fn = self._declare_external_function(
                f"{self._IR_TOPLEVEL_SYMBOL_PREFIX}scaffold_Constant_f64",
                _CSTR,
                [_CSTR, _DOUBLE],
            )
            return self.builder.call(
                fn,
                [ty, raw_value],
                name=self._fresh("scaffold.Constant"),
            )
        if not isinstance(raw_value.type, ir.PointerType):
            raise ScaffoldUnsupportedError(
                "ir.Constant scaffold value has unsupported IR type "
                f"{raw_value.type}"
            )
        value = self.builder.bitcast(raw_value, _CSTR)
        fn = self._declare_external_function(
            f"{self._IR_TOPLEVEL_SYMBOL_PREFIX}scaffold_Constant_obj",
            _CSTR,
            [_CSTR, _CSTR],
        )
        return self.builder.call(
            fn,
            [ty, value],
            name=self._fresh("scaffold.Constant"),
        )

    def _emit_scaffold_constant_exact(self, ty: ir.Value, value_expr) -> ir.Value:
        """``ir.Constant(ty, <int beyond i64>)`` through the object helper."""
        if isinstance(value_expr, IntLit):
            obj = self._emit_int_literal_object(getattr(value_expr, "value"))
        else:
            obj = self._emit_exact_int_operand_object(value_expr)
        owned = (
            obj not in getattr(self, "_cpy_values", ())
            and self._pcc_pointer_source_is_owned(value_expr)
        )
        fn = self._declare_external_function(
            f"{self._IR_TOPLEVEL_SYMBOL_PREFIX}scaffold_Constant_obj",
            _CSTR,
            [_CSTR, _CSTR],
        )
        result = self.builder.call(
            fn,
            [ty, self.builder.bitcast(obj, _CSTR)],
            name=self._fresh("scaffold.Constant"),
        )
        if owned:
            self._gc_release(obj)
        return result

    def _emit_scaffold_value(self, expr: Call) -> ir.Value:
        if expr.kwargs or len(expr.args) != 2:
            raise ScaffoldUnsupportedError("ir.Value scaffold expects (ty, ref)")
        ty = self._scaffold_to_handle(expr.args[0])
        ref = self._scaffold_to_handle(expr.args[1])
        fn = self._declare_external_function(
            f"{self._IR_TOPLEVEL_SYMBOL_PREFIX}scaffold_Value",
            _CSTR,
            [_CSTR, _CSTR],
        )
        return self.builder.call(
            fn,
            [ty, ref],
            name=self._fresh("scaffold.Value"),
        )

    def _emit_scaffold_irbuilder_ctor(self, expr: Call) -> ir.Value:
        """``ir.IRBuilder(block)`` and the detached ``ir.IRBuilder()``.

        The provider's signature is ``__init__(self, block=None)`` and the C
        frontend builds one detached builder up front
        (``c_codegen.CCodeGen.global_builder``) before any block exists, so the
        no-arg form is passed the None object rather than being refused.
        """
        if expr.kwargs or len(expr.args) > 1:
            raise ScaffoldUnsupportedError(
                "ir.IRBuilder scaffold expects at most one block arg"
            )
        if len(expr.args) == 1:
            block = self._scaffold_to_handle(expr.args[0])
        else:
            block = self._emit_none_literal()
        fn = self._declare_external_function(
            f"{self._IR_TOPLEVEL_SYMBOL_PREFIX}scaffold_IRBuilder",
            _CSTR,
            [_CSTR],
        )
        return self.builder.call(
            fn,
            [block],
            name=self._fresh("scaffold.IRBuilder"),
        )

    def _emit_scaffold_context_ctor(self, expr: Call) -> ir.Value:
        if expr.kwargs or expr.args:
            raise ScaffoldUnsupportedError("ir.Context scaffold expects no args")
        fn = self._declare_external_function(
            f"{self._IR_TOPLEVEL_SYMBOL_PREFIX}scaffold_Context",
            _CSTR,
            [],
        )
        return self.builder.call(
            fn,
            [],
            name=self._fresh("scaffold.Context"),
        )

    def _emit_scaffold_identified_struct_type(self, expr: Call) -> ir.Value:
        if expr.kwargs or len(expr.args) != 2:
            raise ScaffoldUnsupportedError(
                "ir.IdentifiedStructType scaffold expects (context, name)"
            )
        context = self._scaffold_to_handle(expr.args[0])
        name = self._scaffold_to_handle(expr.args[1])
        fn = self._declare_external_function(
            f"{self._IR_TOPLEVEL_SYMBOL_PREFIX}scaffold_IdentifiedStructType",
            _CSTR,
            [_CSTR, _CSTR],
        )
        return self.builder.call(
            fn,
            [context, name],
            name=self._fresh("scaffold.IdentifiedStructType"),
        )

    def _emit_scaffold_simple_symbol(
        self,
        symbol: str,
        expr: Call,
        arity: int,
        accepts_name: bool,
    ) -> ir.Value:
        """Lower an ``ir.X(args...)`` constructor with fixed arity to
        ``call ptr @user_pcc_ir_ir_<symbol>___init__(arg0, ...)``.

        When ``accepts_name`` is True and a ``name=`` kwarg is present,
        a separate ``..._named`` variant is used so the runtime can
        runtime can distinguish named vs unnamed constructors.
        """
        name_arg = None
        for key, val in expr.kwargs:
            if key == "name" and accepts_name:
                name_arg = val
                continue
            raise ScaffoldUnsupportedError(
                f"ir.{symbol}(...) scaffold accepts only "
                + ("'name' " if accepts_name else "")
                + f"kwarg{'s' if accepts_name else ''}; got {key!r}"
            )
        pos_args = list(expr.args)
        if accepts_name and name_arg is None and len(pos_args) == arity + 1:
            # llvmlite takes the name positionally too
            # (``GlobalVariable(module, typ, name)``, ``Module(name)``), and
            # the C frontend spells it that way -- see
            # ``c_initializer_lowering._make_global_string_constant``.
            name_arg = pos_args.pop()
        if len(pos_args) != arity:
            raise ScaffoldUnsupportedError(
                f"ir.{symbol} expects {arity} positional args; got "
                f"{len(pos_args)}"
            )
        if symbol == "GlobalVariable" and name_arg is None:
            raise ScaffoldUnsupportedError(
                "ir.GlobalVariable scaffold requires a name"
            )
        lowered = [self._scaffold_to_handle(a) for a in pos_args]
        if name_arg is not None:
            name_h = self._scaffold_to_handle(name_arg)
            extern_name = f"{self._IR_TOPLEVEL_SYMBOL_PREFIX}{symbol}___init___named"
            param_tys = [_CSTR] * (arity + 1)
            fn = self._declare_external_function(
                extern_name,
                _CSTR,
                param_tys,
            )
            return self.builder.call(
                fn,
                lowered + [name_h],
                name=self._fresh(f"scaffold.{symbol}"),
            )
        extern_name = f"{self._IR_TOPLEVEL_SYMBOL_PREFIX}{symbol}___init__"
        if symbol == "Module":
            # Module.__init__ is the instance initializer, not a factory.
            # Its mangled symbol otherwise happens to satisfy this call with
            # the wrong receiver/return ABI. The scaffold factory allocates.
            extern_name = f"{self._IR_TOPLEVEL_SYMBOL_PREFIX}scaffold_Module___init__"
        param_tys = [_CSTR] * arity
        fn = self._declare_external_function(extern_name, _CSTR, param_tys)
        return self.builder.call(
            fn,
            lowered,
            name=self._fresh(f"scaffold.{symbol}"),
        )

    def _emit_scaffold_function_ctor(self, expr: Call) -> ir.Value:
        """``ir.Function(module, fn_ty, name=...)`` — 2 positional args
        plus an optional ``name`` kwarg that is meaningful here (it's
        the LLVM symbol name, not just an SSA hint). Map name kwarg to
        a third extern argument when present.
        """
        if len(expr.args) != 2:
            raise ScaffoldUnsupportedError(
                f"ir.Function expects (module, fn_ty); got " f"{len(expr.args)}"
            )
        name_arg = None
        for key, val in expr.kwargs:
            if key == "name":
                name_arg = val
            else:
                raise ScaffoldUnsupportedError(
                    f"ir.Function(...) scaffold accepts only 'name' "
                    f"kwarg; got {key!r}"
                )
        module_h = self._scaffold_to_handle(expr.args[0])
        fnty_h = self._scaffold_to_handle(expr.args[1])
        if name_arg is None:
            extern = self._declare_external_function(
                f"{self._IR_TOPLEVEL_SYMBOL_PREFIX}Function___init__",
                _CSTR,
                [_CSTR, _CSTR],
            )
            return self.builder.call(
                extern,
                [module_h, fnty_h],
                name=self._fresh("scaffold.Function"),
            )
        name_h = self._scaffold_to_handle(name_arg)
        extern = self._declare_external_function(
            f"{self._IR_TOPLEVEL_SYMBOL_PREFIX}Function___init___named",
            _CSTR,
            [_CSTR, _CSTR, _CSTR],
        )
        return self.builder.call(
            extern,
            [module_h, fnty_h, name_h],
            name=self._fresh("scaffold.Function"),
        )

    def _emit_scaffold_function_type_ctor(self, expr: Call) -> ir.Value:
        """``ir.FunctionType(return_ty, [param_tys...], var_arg=False)``.

        Variadic param-types tuple plus an optional ``var_arg`` boolean
        kwarg. Folds at codegen time when ``var_arg`` is a literal
        True/False; emits per-arity ``__init__<N>[_varargs]`` variants.
        """
        var_arg_expr = None
        var_arg_lit = None
        for key, val in expr.kwargs:
            if key == "name":
                continue
            if key == "var_arg":
                if isinstance(val, BoolLit):
                    var_arg_lit = bool(val.value)
                else:
                    # Dynamic var_arg flag — pass it through as an
                    # extra runtime argument to a ``_dyn_va`` variant.
                    var_arg_expr = val
                continue
            raise ScaffoldUnsupportedError(
                f"ir.FunctionType(...) scaffold accepts only 'name' / "
                f"'var_arg' kwargs; got {key!r}"
            )
        if len(expr.args) != 2:
            raise ScaffoldUnsupportedError(
                f"ir.FunctionType expects (return_ty, params); got " f"{len(expr.args)}"
            )
        ret_ty_h = self._scaffold_to_handle(expr.args[0])
        params_arg = expr.args[1]
        if not _is_scaffold_list_or_tuple(params_arg):
            # Non-literal params: dyn fallback. Pass list handle +
            # var_arg handle (or null) to a ``_dyn`` extern.
            params_h = self._scaffold_to_handle(params_arg)
            if var_arg_expr is not None:
                va_h = self._scaffold_to_handle(var_arg_expr)
            elif var_arg_lit:
                va_h = ir.Constant(_CSTR, 1)
            else:
                va_h = ir.Constant(_CSTR, None)
            extern = self._declare_external_function(
                f"{self._IR_TOPLEVEL_SYMBOL_PREFIX}FunctionType___init___dyn",
                _CSTR,
                [_CSTR, _CSTR, _CSTR],
            )
            return self.builder.call(
                extern,
                [ret_ty_h, params_h, va_h],
                name=self._fresh("scaffold.FunctionType_dyn"),
            )
        param_exprs = list(params_arg.elems)
        param_handles = [self._scaffold_to_handle(p) for p in param_exprs]
        n = len(param_handles)
        if var_arg_expr is not None:
            # Variadic-flag pass-through: emit
            # ``__init__<N>_dyn_va(ret, params..., var_arg_handle)``.
            va_h = self._scaffold_to_handle(var_arg_expr)
            extern = self._declare_external_function(
                f"{self._IR_TOPLEVEL_SYMBOL_PREFIX}FunctionType___init__" f"{n}_dyn_va",
                _CSTR,
                [_CSTR] + [_CSTR] * n + [_CSTR],
            )
            return self.builder.call(
                extern,
                [ret_ty_h] + param_handles + [va_h],
                name=self._fresh("scaffold.FunctionType"),
            )
        suffix = "_varargs" if var_arg_lit else ""
        extern = self._declare_external_function(
            f"{self._IR_TOPLEVEL_SYMBOL_PREFIX}FunctionType___init__" f"{n}{suffix}",
            _CSTR,
            [_CSTR] + [_CSTR] * n,
        )
        return self.builder.call(
            extern,
            [ret_ty_h] + param_handles,
            name=self._fresh("scaffold.FunctionType"),
        )

    def _emit_scaffold_literal_struct(self, expr: Call) -> ir.Value:
        """``ir.LiteralStructType([ty1, ty2, ...])`` — variadic
        element list. Uses per-arity extern variants the same way
        ``builder.call`` does.
        """
        for key, _ in expr.kwargs:
            if key != "name":
                raise ScaffoldUnsupportedError(
                    f"ir.LiteralStructType(...) scaffold accepts only "
                    f"'name' kwarg; got {key!r}"
                )
        if len(expr.args) != 1:
            raise ScaffoldUnsupportedError(
                f"ir.LiteralStructType expects (elements_list,); got "
                f"{len(expr.args)}"
            )
        if _is_scaffold_list_or_tuple(expr.args[0]):
            elem_exprs = self._scaffold_unfold_list_literal(
                expr.args[0],
                "ir.LiteralStructType elements",
            )
            elem_handles = [self._scaffold_to_handle(e) for e in elem_exprs]
            n = len(elem_handles)
            extern = self._declare_external_function(
                f"{self._IR_TOPLEVEL_SYMBOL_PREFIX}LiteralStructType___init__"
                f"{n}",
                _CSTR,
                [_CSTR] * n,
            )
            return self.builder.call(
                extern,
                elem_handles,
                name=self._fresh("scaffold.LiteralStructType"),
            )
        # Dynamic element list (runtime-built, e.g. a variadic global-def
        # intrinsic delegating to a helper). The list is constructed by the
        # caller; this is the struct analogue of ``builder.call_dyn``.
        list_handle = self._scaffold_to_handle(expr.args[0])
        extern = self._declare_external_function(
            f"{self._IR_TOPLEVEL_SYMBOL_PREFIX}LiteralStructType_dyn",
            _CSTR,
            [_CSTR],
        )
        return self.builder.call(
            extern,
            [list_handle],
            name=self._fresh("scaffold.LiteralStructType_dyn"),
        )

    def _ir_scaffold_enabled(self) -> bool:
        return (
            self.ir_scaffold_mode == "on"
            or module_has_contract(self.ast_module.name, IR_SCAFFOLD_CONTRACT)
        )
