"""Lexical boundness, separate from value representation and reference ownership."""

from pcc.ir.compat import ir
from pcc.frontends.python.py_ast import (
    RawPointerType,
    Assign,
    Call,
    ClassDef,
    DynType,
    FuncDef,
    Import,
    ImportFrom,
    Lambda,
    Name,
    TupleExpr,
    ListExpr,
)
from pcc.frontends.python.codegen.definite_assignment import definitely_bound_loads, load_key
from pcc.frontends.python.codegen.exact_int_lowering import _collect_local_binding_types
from pcc.frontends.python.codegen.hoist_boxing import function_local_bindings
from pcc.frontends.python.codegen.hoist_analysis import _dataclass_field_names, _dataclass_field_value

_I1 = ir.IntType(1)


def local_bound_flag(host, name):
    return getattr(host, "_owned_local_flag_slots", {}).get(".bound." + name)


def mark_local_bound(host, name, bound=True) -> None:
    flag = local_bound_flag(host, name)
    if flag is not None and not host._builder_block_is_terminated():
        host.builder.store(ir.Constant(_I1, 1 if bound else 0), flag)


def mark_bound_target(host, target, bound=True) -> None:
    if isinstance(target, Name):
        mark_local_bound(host, target.ident, bound)
    elif isinstance(target, (TupleExpr, ListExpr)):
        for child in target.elems:
            mark_bound_target(host, child, bound)
    elif isinstance(target, Call) and isinstance(target.func, Name):
        if target.func.ident in ("*", "__starred__", "_walrus", "__walrus__"):
            for child in target.args:
                mark_bound_target(host, child, bound)


def prepare_local_boundness(host, fd) -> None:
    if host._freestanding_module or host._runtime_port_module or host._module_has_c_abi_export:
        return
    names = function_local_bindings(fd)
    params = tuple(arg.name for arg in fd.args if arg.name)
    # Keys are source positions, so one module-wide set serves every function.
    host._definitely_bound_loads |= definitely_bound_loads(fd)
    for name in names:
        flag = host._ensure_owned_local_flag(".bound." + name)
        if name in params:
            host._store_entry_initializer(flag, ir.Constant(_I1, 1))

    # Reuse the existing representation inventory and entry-slot policy.
    # Compile-time imports/aliases own metadata, so a placeholder env entry
    # must never displace those bindings.
    metadata_names = []
    pending = list(fd.body)
    while pending:
        node = pending.pop()
        if isinstance(node, Import):
            metadata_names.extend(alias or module.split(".", 1)[0] for module, alias in node.names)
            continue
        if isinstance(node, ImportFrom):
            if node.module != "builtins":
                metadata_names.extend(alias or name for name, alias in node.names)
            continue
        if isinstance(node, Assign):
            value = node.value
            marker = host._native_builtin_value_kind_for_expr(value)
            if (marker is not None or isinstance(value, Call) and isinstance(value.func, Name)
                    and getattr(host, "_extern_bindings", {}).get(value.func.ident) == "extern"):
                for target in node.targets:
                    if isinstance(target, Name):
                        metadata_names.append(target.ident)
        if node is None or isinstance(node, (str, int, float, bool, bytes)):
            continue
        if isinstance(node, (tuple, list)):
            pending.extend(node)
            continue
        # A descendant's body/default target bindings are its own scope.
        if isinstance(node, (FuncDef, ClassDef, Lambda)):
            continue
        for field in _dataclass_field_names(node):
            if field not in ("span", "ty", "annotation", "return_ty"):
                pending.append(_dataclass_field_value(node, field, None))
    bindings = []
    _collect_local_binding_types(fd.body, bindings)
    for name, ty in bindings:
        if name not in names or name in host.env or name in metadata_names:
            continue
        if ty is None:
            ty = DynType(name="dyn")
        if not (host._is_scalar(ty) or host._is_object(ty) or host._is_valueclass_payload_type(ty)):
            continue
        ir_ty = host._local_slot_ir_type(name, ty)
        slot = host._alloca_in_entry(ir_ty, name=name + ".addr", init_null=isinstance(ir_ty, ir.PointerType))
        host.env[name] = (slot, ir_ty, host._local_slot_decl_type(name, ty))
        if (isinstance(ir_ty, ir.PointerType)
                and not isinstance(ty, RawPointerType)
                and host._ir_type_matches(ir_ty, ir.IntType(8).as_pointer())):
            host._ensure_owned_local_gc_root(name, slot, ir_ty)
            host._ensure_owned_local_flag(name, slot)


def check_local_bound(host, expr) -> None:
    flag = local_bound_flag(host, expr.ident)
    if flag is None:
        return
    if load_key(expr) in host._definitely_bound_loads:
        return
    ready = host.builder.load(flag, name=host._fresh(expr.ident + ".bound"))
    ok = host.current_function.append_basic_block(name=host._fresh(expr.ident + ".bound.ok"))
    missing = host.current_function.append_basic_block(name=host._fresh(expr.ident + ".bound.error"))
    host.builder.cbranch(ready, ok, missing)
    host.builder.position_at_end(missing)
    host._emit_builtin_exception_and_branch(
        "UnboundLocalError", "cannot access local variable '" + expr.ident + "' where it is not associated with a value", expr.span,
    )
    host.builder.position_at_end(ok)
