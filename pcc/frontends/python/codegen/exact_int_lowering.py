"""Exact Python integer object-boundary lowering for L1CodeGen."""
from __future__ import annotations

import sys
import os
from typing import Optional

from pcc.ir.compat import ir

from pcc.frontends.python.py_ast import Assign, AugAssign, BinOp, BoolExpr, BoolLit, BoolType, Attr, Call, Compare, DictType, DynType, StrType, Expr, FloatType, For, FuncDef, If, IfExpr, ImportFrom, IntLit, IntType, ListType, Name, Slice, Subscript, TupleType, TupleExpr, Try, UnaryOp, While, With
from pcc.frontends.python.codegen import marshal
from pcc.frontends.python.codegen.errors import L1CodegenError
from pcc.frontends.python.codegen.cpy_import_state import live_import_expr_binding
from pcc.frontends.python.codegen.vthread_effect_analysis import vthread_delegate_frame_name
from pcc.frontends.python.py_ast import assignment_storage_annotation


_I1 = ir.IntType(1)
_I32 = ir.IntType(32)
_I64 = ir.IntType(64)
_CSTR = ir.IntType(8).as_pointer()


def _exact_int_is_walrus_call(expr) -> bool:
    return (
        isinstance(expr, Call)
        and isinstance(expr.func, Name)
        and expr.func.ident in ("_walrus", "__walrus__")
        and len(expr.args) == 2
    )


def _collect_walrus_target_binding_types(target, out) -> None:
    """Inventory every hidden target in ``a = b = value`` lowering."""
    if _exact_int_is_walrus_call(target):
        _collect_walrus_target_binding_types(target.args[1], out)
        _collect_walrus_target_binding_types(target.args[0], out)
        return
    _collect_local_binding_types_from_target(target, getattr(target, "ty", None), out)


def _collect_walrus_exact_int_candidates(target, value_expr, out) -> None:
    """Associate every hidden chained-assignment target with the real RHS."""
    if _exact_int_is_walrus_call(target):
        _collect_walrus_exact_int_candidates(target.args[1], value_expr, out)
        _collect_walrus_exact_int_candidates(target.args[0], value_expr, out)
        return
    _collect_exact_int_assignment_target(target, value_expr, None, out)


def _collect_local_binding_types_from_target(target, target_ty, out) -> None:
    """Collect the semantic type of each plain-name binding target."""
    if isinstance(target, Name):
        out.append((target.ident, target_ty if target_ty is not None else target.ty))
        return
    if isinstance(target, TupleExpr):
        for elem in target.elems:
            _collect_local_binding_types_from_target(elem, elem.ty, out)


def _collect_local_binding_types(stmts, out) -> None:
    """Collect function-local binding representations without nested scopes.

    This is deliberately a representation inventory rather than a type join.
    Inference is allowed to keep the Python semantic type of each statement;
    codegen still needs to know before the entry block whether one local name
    is written through both the raw-int and object projections.
    """
    for stmt in stmts:
        if isinstance(stmt, Assign):
            for target in stmt.targets:
                target_ty = (
                    assignment_storage_annotation(stmt.annotation, stmt.value, stmt.has_value)
                    if stmt.has_value and stmt.annotation is not None else target.ty
                )
                _collect_local_binding_types_from_target(target, target_ty, out)
            if _exact_int_is_walrus_call(stmt.value):
                _collect_walrus_target_binding_types(stmt.value.args[0], out)
        elif isinstance(stmt, AugAssign):
            _collect_local_binding_types_from_target(
                stmt.target,
                stmt.target.ty,
                out,
            )
        elif isinstance(stmt, ImportFrom):
            # A native from-import binds a Python object in the executing
            # function, even when another write to this name is a raw scalar.
            # Plan one compatible slot before either branch is emitted.
            for imported, alias in stmt.names:
                if imported != "*":
                    out.append((alias or imported, DynType(name="dyn")))
        elif isinstance(stmt, For):
            is_enumerate_tuple = (
                isinstance(stmt.iter, Call)
                and isinstance(stmt.iter.func, Name)
                and stmt.iter.func.ident == "enumerate"
                and isinstance(stmt.target, TupleExpr)
                and len(stmt.target.elems) == 2
            )
            if is_enumerate_tuple:
                # `_normalise_for_enumerate` emits the first binding as an
                # explicitly annotated Python int even when the original
                # tuple target was inferred as Dyn.  Inventory the emitted
                # representation, not the pre-normalisation placeholder.
                _collect_local_binding_types_from_target(
                    stmt.target.elems[0],
                    IntType(name="int"),
                    out,
                )
                _collect_local_binding_types_from_target(
                    stmt.target.elems[1],
                    stmt.target.elems[1].ty,
                    out,
                )
            else:
                _collect_local_binding_types_from_target(
                    stmt.target,
                    stmt.target.ty,
                    out,
                )

        if isinstance(stmt, (If, While, For)):
            _collect_local_binding_types(stmt.body, out)
            _collect_local_binding_types(stmt.else_body, out)
        elif isinstance(stmt, Try):
            _collect_local_binding_types(stmt.body, out)
            for handler in stmt.handlers:
                _collect_local_binding_types(handler.body, out)
            _collect_local_binding_types(stmt.else_body, out)
            _collect_local_binding_types(stmt.finally_body, out)
        elif isinstance(stmt, With):
            _collect_local_binding_types(stmt.body, out)
        elif isinstance(stmt, FuncDef):
            continue


def _collect_exact_int_assignment_candidates(stmts, out, box_iteration_ints: bool = True) -> None:
    """Collect simple int-local writes without entering nested functions.

    The result stays in source order so entry-slot allocation is deterministic
    across pcc0/pcc1/pcc2.  Control-flow joins are representation joins, not
    semantic type joins: inference has already (correctly) kept every target as
    Python ``int`` here.
    """
    for stmt in stmts:
        if isinstance(stmt, Assign):
            value_expr = stmt.value
            if _exact_int_is_walrus_call(value_expr):
                value_expr = value_expr.args[1]
            for target in stmt.targets:
                _collect_exact_int_assignment_target(
                    target,
                    value_expr,
                    assignment_storage_annotation(stmt.annotation, stmt.value, stmt.has_value)
                    if stmt.has_value else None,
                    out,
                )
            if _exact_int_is_walrus_call(stmt.value):
                _collect_walrus_exact_int_candidates(
                    stmt.value.args[0],
                    value_expr,
                    out,
                )
        elif isinstance(stmt, AugAssign) and isinstance(stmt.target, Name):
            if isinstance(stmt.target.ty, IntType):
                # Any integer augmented assignment can escape the i64 lane
                # based on the incoming value (``x <<= n``, ``x *= y``, ...).
                # Once a name is written this way, use one object projection
                # on every control-flow edge rather than trying to prove a
                # range from the RHS alone.
                out.append((stmt.target.ident, stmt.value, True))
        elif isinstance(stmt, For) and box_iteration_ints:
            # Sequence elements already have the Python object projection.
            # A loop's inferred ``int`` target is no range proof: preserve
            # arbitrary precision before indexed iteration or normalized
            # tuple unpacking can attempt an i64 conversion.
            if isinstance(stmt.target, TupleExpr) or isinstance(
                stmt.iter.ty, (TupleType, ListType)
            ):
                pending = [stmt.target]
                while pending:
                    target = pending.pop()
                    if isinstance(target, TupleExpr):
                        pending.extend(reversed(target.elems))
                    elif (
                        isinstance(target, Name)
                        and isinstance(target.ty, IntType)
                        and target.ty.name == "int"
                    ):
                        out.append((target.ident, stmt.iter, True))

        if isinstance(stmt, (If, While, For)):
            _collect_exact_int_assignment_candidates(stmt.body, out, box_iteration_ints)
            _collect_exact_int_assignment_candidates(stmt.else_body, out, box_iteration_ints)
        elif isinstance(stmt, Try):
            _collect_exact_int_assignment_candidates(stmt.body, out, box_iteration_ints)
            for handler in stmt.handlers:
                _collect_exact_int_assignment_candidates(handler.body, out, box_iteration_ints)
            _collect_exact_int_assignment_candidates(stmt.else_body, out, box_iteration_ints)
            _collect_exact_int_assignment_candidates(stmt.finally_body, out, box_iteration_ints)
        elif isinstance(stmt, With):
            _collect_exact_int_assignment_candidates(stmt.body, out, box_iteration_ints)
        elif isinstance(stmt, FuncDef):
            # A nested function owns a distinct local/representation analysis.
            continue


def _collect_exact_int_assignment_target(
    target,
    value_expr,
    annotation,
    out,
) -> None:
    """Collect exact-int candidates from plain and destructuring targets."""
    if isinstance(target, Name):
        target_ty = annotation if annotation is not None else target.ty
        if isinstance(target_ty, IntType):
            out.append((target.ident, value_expr, False))
        return
    if not isinstance(target, TupleExpr):
        return
    value_elems = ()
    if isinstance(value_expr, TupleExpr) and len(value_expr.elems) == len(
        target.elems
    ):
        value_elems = value_expr.elems
    index = 0
    while index < len(target.elems):
        child_value = value_expr
        child_force = True
        if value_elems:
            child_value = value_elems[index]
            child_force = False
        child = target.elems[index]
        if isinstance(child, Name) and isinstance(child.ty, IntType):
            out.append((child.ident, child_value, child_force))
        elif isinstance(child, TupleExpr):
            _collect_exact_int_assignment_target(
                child,
                child_value,
                None,
                out,
            )
        index += 1


def mixed_scalar_object_local_names(host, fd: FuncDef, global_names) -> tuple[str, ...]:
    """Locals written through both an unboxed float and an object.

    ``forced_exact_int_local_names`` already does this for ``int``, because a
    raw i64 and a PyInt cannot share one slot.  The same is true of a raw
    double and any object, and nothing covered it: the slot took the shape of
    the first binding and every later store was *coerced* into it, so
    assigning a class instance to a name previously bound to a float emitted
    ``py_float_to_f64`` on the instance -- a silently wrong value, caught only
    because the self backend's verifier later saw the double reach a call
    expecting an object.

    ``pcc/frontends/c/codegen/c_codegen.py::_eval_const_expr`` is the real instance: it
    writes ``lhs = float(...)`` on the floating branch and
    ``lhs = integer_promotion(...)`` (a ``ConstIntValue``) on the integer one,
    then reads ``lhs.width``.

    ``int`` is deliberately NOT included here; it keeps its own planner, whose
    consumers project the exact-int object lane.

    Boolean and floating bindings share one object slot with later object
    writes. Store consumers must box raw scalars and preserve already-boxed
    pointers rather than coercing them back into the first semantic type.
    """
    if getattr(host, "_freestanding_module", False):
        # Same boundary as the exact-int planner: the freestanding subset
        # cannot allocate objects or register GC roots.
        return ()
    binding_types = []
    for arg in fd.args:
        if arg.name != "":
            binding_types.append(
                (arg.name, arg.annotation or DynType(name="dyn"))
            )
    _collect_local_binding_types(fd.body, binding_types)

    scalar_bindings = set()
    object_bindings = set()
    order = []
    seen = set()
    for name, binding_ty in binding_types:
        if name in global_names:
            continue
        if name not in seen:
            seen.add(name)
            order.append(name)
        if isinstance(binding_ty, (FloatType, BoolType)):
            scalar_bindings.add(name)
        elif isinstance(binding_ty, IntType):
            # An int binding is an object only when ints are boxed; either way
            # the int planner owns that name.
            continue
        elif host._is_object(binding_ty):
            object_bindings.add(name)

    out = []
    for name in order:
        if name in scalar_bindings and name in object_bindings:
            out.append(name)
    return tuple(out)


def forced_exact_int_local_names(host, fd: FuncDef, global_names, box_int_abi: bool) -> tuple[str, ...]:
    """Return locals that need one exact-object representation on every edge.

    Seeds are writes whose RHS already requires the exact-int object boundary.
    Re-running the predicate with the growing name set propagates that boundary
    through local copies and augmented assignments.  The monotone iteration is
    bounded by the number of candidate local names.
    """
    if getattr(host, "_freestanding_module", False):
        # The freestanding subset is the compiler-owned machine boundary: it
        # cannot allocate Python integer objects or register managed GC roots.
        # Its typed integer locals therefore remain in their raw C-ABI lane;
        # range/overflow-sensitive Python semantics belong above this layer.
        return ()
    candidates = []
    _collect_exact_int_assignment_candidates(
        fd.body, candidates,
        fd.name not in host._bounded_int_abi_function_names,
    )
    boxed_int_parameters = set()
    if box_int_abi:
        for arg in fd.args:
            if arg.name != "" and isinstance(arg.annotation, IntType) and arg.annotation.name == "int":
                boxed_int_parameters.add(arg.name)
    binding_types = []
    for arg in fd.args:
        if arg.name != "":
            binding_types.append(
                (arg.name, arg.annotation or DynType(name="dyn"))
            )
    _collect_local_binding_types(fd.body, binding_types)

    int_bindings = set()
    object_bindings = set()
    binding_order = []
    binding_seen = set()
    for name, binding_ty in binding_types:
        if name in global_names:
            continue
        if name not in binding_seen:
            binding_seen.add(name)
            binding_order.append(name)
        if isinstance(binding_ty, IntType):
            int_bindings.add(name)
        elif host._is_object(binding_ty):
            object_bindings.add(name)
    mixed_representation_names = set()
    for name in int_bindings:
        if name in object_bindings:
            mixed_representation_names.add(name)

    ordered_names = []
    mixed_ordered_names = []
    seen_names = set()
    for name in binding_order:
        if name not in mixed_representation_names or name in seen_names:
            continue
        seen_names.add(name)
        ordered_names.append(name)
        mixed_ordered_names.append(name)
    for name, _expr, _force in candidates:
        if name in global_names or name in seen_names:
            continue
        seen_names.add(name)
        ordered_names.append(name)

    forced = list(mixed_ordered_names)
    candidate_names = {name for name, _expr, _force in candidates}
    for name in binding_order:
        if (
            name in boxed_int_parameters
            and name in candidate_names
            and name not in forced
        ):
            forced.append(name)
    forced_lookup = set(forced)
    saved_flags = host._exact_int_env_flags
    try:
        rounds_left = len(ordered_names) + 1
        changed = True
        while changed and rounds_left > 0:
            rounds_left -= 1
            changed = False
            host._exact_int_env_flags = {
                name: True for name in forced
            }
            for name, expr, force_exact in candidates:
                if name in global_names or name in forced_lookup:
                    continue
                if force_exact or host._int_expr_needs_exact_object_boundary(expr):
                    forced_lookup.add(name)
                    forced.append(name)
                    changed = True
    finally:
        host._exact_int_env_flags = saved_flags

    # Preserve first-write source order rather than discovery-round order.
    return tuple(name for name in ordered_names if name in forced_lookup)


def bind_forced_exact_int_parameter(
    host,
    ast_arg,
    ir_arg,
    ir_ty,
    bind_ty,
) -> bool:
    """Bind a planned exact-int parameter as one owned, rooted object local."""
    if not isinstance(bind_ty, IntType):
        return False
    if not host._exact_int_env_flags.get(ast_arg.name, False):
        return False

    if isinstance(ir_ty, ir.PointerType):
        # Pointer-ABI parameters are borrowed.  Promote at entry so the slot,
        # its owned frame-map kind, rebind cleanup, and pointer-return transfer
        # all share one invariant instead of changing ownership dynamically.
        param_value = host._gc_retain(
            ir_arg,
            name=host._fresh(ast_arg.name + ".param.retain"),
        )
    else:
        param_value = marshal.marshal_to_object(
            host.builder,
            host.module,
            host.runtime,
            ir_arg,
            bind_ty,
        )

    slot = host._alloca_in_entry(
        _CSTR,
        name=ast_arg.name + ".addr",
        init_null=True,
    )
    host.builder.store(param_value, slot)
    host.env[ast_arg.name] = (slot, _CSTR, bind_ty)
    host._owned_local_names.add(ast_arg.name)
    host._owned_local_has_value.add(ast_arg.name)
    host._ensure_local_gc_frame_root(
        ast_arg.name,
        slot,
        _CSTR,
        host._gc_one_slot_frame_map(),
    )
    owned_flag = host._ensure_owned_local_flag(ast_arg.name, slot)
    host.builder.store(ir.Constant(_I1, 1), owned_flag)
    return True


def allocate_forced_exact_int_locals(host, names, global_names) -> None:
    """Allocate planned non-parameter exact-int locals in the entry block."""
    for name in names:
        if name in host.env or name in global_names:
            continue
        slot = host._alloca_in_entry(
            _CSTR,
            name=name + ".addr",
            init_null=True,
        )
        host.env[name] = (slot, _CSTR, IntType(name="int"))
        host._owned_local_names.add(name)
        host._ensure_owned_local_gc_root(name, slot, _CSTR)
        host._ensure_owned_local_flag(name, slot)


class ExactIntLoweringMixin:
    def _call_is_int_builtin(self, expr) -> bool:
        """True for a plain `int(...)` call of the unshadowed builtin, with
        positional operands only (``int(s, **kw)`` calls the type object)."""
        if expr.kwargs or self._has_unpacked_operands(expr.args, expr.kwargs):
            return False
        func = expr.func
        if not isinstance(func, Name) or func.ident != "int":
            return False
        if "int" in self.env or "int" in self._module_globals or "int" in self.functions:
            return False
        return "int" not in getattr(getattr(self, "class_lowering", None), "classes", {})

    def _maybe_emit_exact_int_object(
        self,
        expr: Expr,
    ) -> Optional[ir.Value]:
        if not isinstance(expr.ty, IntType):
            return None
        if isinstance(expr, Attr):
            if live_import_expr_binding(self, expr.obj):
                return self._take_slot_call_root(
                    self._emit_slot_call_attribute(expr, "import.exact.attribute"),
                )
            if self._is_valueclass_payload_type(expr.obj.ty):
                # Payload fields belong to their explicit machine lane.
                # Decline before evaluating a possibly effectful receiver;
                # the scalar fallback must evaluate it exactly once.
                return None
            # An `int`-typed attribute read must not go through i64: the
            # ordinary attr lowering marshals the field object to the declared
            # type, and `py_int_to_i64` yields 0 above 2**63-1 -- so a bignum
            # stored in an `int` field read back as 0, even though the field
            # itself holds the object (`py_instance_set_field` stores a ptr).
            #
            # Two primitives, picked by what is statically known:
            #   * `py_instance_get_field` when the container's class and the
            #     field index are known -- BORROWED, so nothing to release.
            #   * `py_obj_getattr` otherwise (e.g. inside a function where the
            #     class hint does not resolve) -- returns a NEW reference, so it
            #     is registered as an owned dynamic-call value.
            container = self._emit_expr(expr.obj)
            if isinstance(container.type, ir.PointerType):
                field_index = None
                if isinstance(expr.obj, Name):
                    hint = self._class_hint_for_expr(expr.obj)
                    if hint is not None:
                        class_info = self.class_lowering.classes.get(hint)
                        if class_info is not None:
                            field_index = self.class_lowering.lookup_field_index(
                                class_info, expr.name
                            )
                if field_index is not None:
                    got = self.builder.call(
                        self.runtime["py_instance_get_field"],
                        [container, ir.Constant(_I32, field_index)],
                        name=self._fresh("exact.int.field.obj"),
                    )
                    self._emit_attribute_error_if_null(got, expr.name, expr.span)
                    self._note_owned_dynamic_call_value(got)
                    return got
                name_gv, _ = self._cstr_literal(expr.name)
                got = self.builder.call(
                    self.runtime["py_obj_getattr"],
                    [container, self._ptr_to_cstr(name_gv)],
                    name=self._fresh("exact.int.attr.obj"),
                )
                self._emit_post_call_err_check(getattr(expr, "span", None))
                self._note_owned_dynamic_call_value(got)
                return got
        if isinstance(expr, Call) and self._call_is_int_builtin(expr):
            # `int(<str>)` has an object-returning form.  Taking it here keeps a
            # bignum intact: the ordinary lowering unboxes to i64 to satisfy the
            # builtin's contract, and above 2**63-1 that is 0 -- which is how
            # every over-i64 literal in compiled source became 0, since the
            # parser lifts literals through `int(e.text, 0)`.
            obj = self.emit_int_builtin_as_object(expr)
            if obj is not None:
                return obj
        if (
            isinstance(expr, Call)
            and self._native_builtin_value_kind_for_expr(expr.func)
            == "pcc.guarded_i64_dot"
        ):
            value = self._emit_expr(expr)
            if isinstance(value.type, ir.PointerType):
                return value
            raise L1CodegenError(
                "pcc.guarded_i64_dot must return the exact int object projection"
            )
        if (
            isinstance(expr, Name)
            and getattr(
                self,
                "_exact_int_env_flags",
                {},
            ).get(expr.ident, False)
        ):
            value = self._emit_expr(expr)
            if isinstance(value.type, ir.PointerType):
                return value
        if isinstance(expr, Name) and expr.ident not in self.env:
            module_global = self._module_globals.get(expr.ident)
            if (
                module_global is not None
                and isinstance(module_global[1], IntType)
                and isinstance(module_global[0].value_type, ir.PointerType)
            ):
                value = self._emit_expr(expr)
                if isinstance(value.type, ir.PointerType):
                    return value
        if isinstance(expr, IfExpr):
            return self._emit_if_expr_as_pcc_object(expr)
        if isinstance(expr, BinOp):
            if (
                expr.op == "**"
                and isinstance(expr.lhs, IntLit)
                and isinstance(expr.rhs, IntLit)
                and expr.rhs.value >= 0
            ):
                lhs_value = int(expr.lhs.value)
                rhs_value = int(expr.rhs.value)
                # Constant folding here is only an i64-lane shortcut.  Once
                # the result cannot fit that lane, the runtime exact-int
                # helper is both semantically authoritative and bounded in
                # compiler memory.  In particular, never materialise a
                # billion-bit host-Python integer merely to discover that it
                # does not fit i64.
                if rhs_value <= 63 or lhs_value in (-1, 0, 1):
                    folded = pow(lhs_value, rhs_value)
                    if -(1 << 63) <= folded <= (1 << 63) - 1:
                        return self._emit_int_literal_object(folded)
            fn_name = {
                "+": "py_int_add",
                "-": "py_int_sub",
                "*": "py_int_mul",
                "//": "py_int_floordiv",
                "%": "py_int_mod",
                "**": "py_int_pow",
                "&": "py_int_and",
                "|": "py_int_or",
                "^": "py_int_xor",
                "<<": "py_int_shl",
                ">>": "py_int_shr",
            }.get(expr.op)
            if fn_name is None:
                return None
            if not (
                isinstance(expr.lhs.ty, (IntType, BoolType))
                and isinstance(expr.rhs.ty, (IntType, BoolType))
            ):
                return None
            if expr.op == "**":
                self._guard_integer_power_operand_provenance(expr.lhs, expr.rhs)
            lhs = self._emit_exact_int_operand_object(expr.lhs)
            lhs_owned = (
                isinstance(lhs.type, ir.PointerType)
                and lhs not in getattr(self, "_cpy_values", ())
                and self._pcc_pointer_source_is_owned(expr.lhs)
            )
            lhs_pinned = (
                isinstance(lhs.type, ir.PointerType)
                and lhs not in getattr(self, "_cpy_values", ())
            )
            lhs_cleanup = ()
            lhs_root_slot = None
            lhs_root_lifetimes = ()
            lhs_generator_slot = None
            lhs_quiet_unpinned = False
            if lhs_pinned:
                generator_stack = getattr(self, "_generator_ctx_stack", ())
                if generator_stack:
                    hidden = vthread_delegate_frame_name(
                        expr,
                        "pcc.exact_int.lhs",
                    )
                    frame_entry = generator_stack[-1]["frame_slots"].get(hidden)
                    if frame_entry is None:
                        raise L1CodegenError(
                            "exact-int binary operation is missing its managed "
                            "lhs frame slot"
                        )
                    lhs_generator_slot = frame_entry[1]
                    lhs_generator_root = self._as_gc_ptr(
                        lhs_generator_slot,
                        name=self._fresh("exact.int.lhs.frame.ptr"),
                    )
                    self._gc_pin(lhs)
                    self.builder.call(
                        self.runtime["pcc_gc_store_root"],
                        [lhs_generator_root, lhs],
                    )
                    self._gc_unpin(lhs)
                    if lhs_owned:
                        self._gc_release(lhs)

                    prior_pcc_target = self._current_try_err_block()
                    pcc_target = prior_pcc_target
                    if pcc_target is None:
                        pcc_target = self._ensure_fn_err_exit()
                    save_block = self.builder._block
                    pcc_cleanup = self.current_function.append_basic_block(
                        name=self._fresh("exact.int.lhs.frame.err")
                    )
                    self.builder.position_at_end(pcc_cleanup)
                    pcc_cleanup_root = self._as_gc_ptr(
                        lhs_generator_slot,
                        name=self._fresh("exact.int.lhs.frame.err.ptr"),
                    )
                    self.builder.call(
                        self.runtime["pcc_gc_store_root"],
                        [pcc_cleanup_root, self._emit_none_literal()],
                    )
                    self.builder.branch(pcc_target)
                    self.builder.position_at_end(save_block)

                    prior_cpy_target = getattr(
                        self,
                        "_cpy_operand_cleanup_block",
                        None,
                    )
                    cpy_target = prior_cpy_target
                    if cpy_target is None:
                        cpy_target = pcc_target
                    cpy_cleanup = pcc_cleanup
                    if cpy_target is not pcc_target:
                        cpy_cleanup = self.current_function.append_basic_block(
                            name=self._fresh("exact.int.lhs.frame.cpy.err")
                        )
                        self.builder.position_at_end(cpy_cleanup)
                        cpy_cleanup_root = self._as_gc_ptr(
                            lhs_generator_slot,
                            name=self._fresh("exact.int.lhs.frame.cpy.err.ptr"),
                        )
                        self.builder.call(
                            self.runtime["pcc_gc_store_root"],
                            [cpy_cleanup_root, self._emit_none_literal()],
                        )
                        self.builder.branch(cpy_target)
                        self.builder.position_at_end(save_block)
                    self._try_err_block = pcc_cleanup
                    self._cpy_operand_cleanup_block = cpy_cleanup
                    try:
                        rhs = self._emit_expr_as_pcc_object(expr.rhs)
                    finally:
                        self._try_err_block = prior_pcc_target
                        self._cpy_operand_cleanup_block = prior_cpy_target

                    resumed_lhs_root = self._as_gc_ptr(
                        lhs_generator_slot,
                        name=self._fresh("exact.int.lhs.frame.resume.ptr"),
                    )
                    rooted_lhs = self.builder.call(
                        self.runtime["pcc_gc_load_ptr"],
                        [ir.Constant(_CSTR, None), resumed_lhs_root],
                        name=self._fresh("exact.int.lhs.frame.rooted"),
                    )
                    lhs = self._gc_retain(
                        rooted_lhs,
                        name=self._fresh("exact.int.lhs.frame.retain"),
                    )
                    self.builder.call(
                        self.runtime["pcc_gc_store_root"],
                        [resumed_lhs_root, self._emit_none_literal()],
                    )
                    lhs_owned = True
                    self._gc_pin(lhs)
                    lhs_cleanup = ((lhs, True),)
                elif self._exact_int_operand_is_gc_quiet(expr.rhs):
                    # The rhs is a local/global load or a literal: nothing
                    # between here and the operation can allocate, raise or
                    # relocate, so the temporary root frame (store_root,
                    # frame_enter_lifo, three reloads, frame_leave_lifo) was
                    # pure protocol.  The operands are pinned only around the
                    # slow runtime call below.
                    lhs_quiet_unpinned = True
                else:
                    # A pin keeps the object immobile, but later operand
                    # lowering can still relocate it on another thread.  Keep
                    # the early operand in an updateable stack-frame root and
                    # reload it after RHS evaluation.
                    lhs_root_slot = self._enter_container_temp_root(
                        lhs,
                        "exact.int.lhs",
                    )
                    lhs_root_lifetimes = ((lhs_root_slot, lhs_owned),)
            if lhs_generator_slot is None:
                rhs = self._emit_expr_with_cpy_operand_cleanup(
                    expr.rhs,
                    (),
                    as_pcc_object=True,
                    rooted_pcc_lifetimes=lhs_root_lifetimes,
                )
            if lhs_root_slot is not None:
                lhs = self.builder.call(
                    self.runtime["pcc_gc_load_ptr"],
                    [
                        ir.Constant(_CSTR, None),
                        self._as_gc_ptr(
                            lhs_root_slot,
                            name=self._fresh("exact.int.lhs.root.ptr"),
                        ),
                    ],
                    name=self._fresh("exact.int.lhs.rooted"),
                )
                # The root held a temporary retain in addition to the source
                # owner's reference.  Clear only that retain here; the
                # reloaded pointer now carries the original ownership bit.
                self._release_rooted_pcc_lifetimes(((lhs_root_slot, False),))
                self._gc_pin(lhs)
                lhs_cleanup = ((lhs, lhs_owned),)
            if lhs_quiet_unpinned:
                lhs_cleanup = ((lhs, lhs_owned),)
            rhs_owned = (
                isinstance(rhs.type, ir.PointerType)
                and rhs not in getattr(self, "_cpy_values", ())
                and self._pcc_pointer_source_is_owned(expr.rhs)
            )
            rhs_pinned = (
                isinstance(rhs.type, ir.PointerType)
                and rhs not in getattr(self, "_cpy_values", ())
            )
            # Both operands are exact-int objects here whether or not the
            # enclosing function boxes every int local, so the tagged fast
            # path applies to planned exact-int locals in plain functions too.
            inline_capable = expr.op in ("+", "-", "*", "&", "|", "^")
            # With an inline fast path, pins are deferred into the slow block;
            # otherwise the runtime call is unconditional and pins wrap it.
            slow_pins: tuple[ir.Value, ...] = ()
            if inline_capable:
                if lhs_quiet_unpinned:
                    slow_pins = slow_pins + (lhs,)
                if rhs_pinned:
                    slow_pins = slow_pins + (rhs,)
            else:
                if lhs_quiet_unpinned:
                    self._gc_pin(lhs)
                if rhs_pinned:
                    self._gc_pin(rhs)
            operand_cleanup = lhs_cleanup
            if rhs_pinned:
                operand_cleanup = operand_cleanup + ((rhs, rhs_owned),)
            # Shift kernels validate the exact count. A preliminary i64
            # projection would reject valid huge right/zero-left shifts and
            # misclassify huge negative counts before the runtime sees them.
            if inline_capable:
                inline = self._emit_inline_tagged_int_binop_or_call(
                    expr.op,
                    lhs,
                    rhs,
                    fn_name,
                    slow_pins=slow_pins,
                    slow_err_check=True,
                    slow_err_cleanup=operand_cleanup,
                )
                if inline is not None:
                    if lhs_pinned and not lhs_quiet_unpinned:
                        self._gc_unpin(lhs)
                    if lhs_owned:
                        self._gc_release(lhs)
                    if rhs_owned:
                        self._gc_release(rhs)
                    return inline
                # No inline shape after all: the unconditional call below
                # needs the deferred pins in place.
                for pinned in slow_pins:
                    self._gc_pin(pinned)
            result = self.builder.call(
                self.runtime[fn_name],
                [lhs, rhs],
                name=self._fresh("exact.int"),
            )
            self._emit_post_call_err_check(
                None,
                pinned_release_on_error=operand_cleanup,
            )
            if expr.op == "//" or expr.op == "%":
                # py_int_floordiv / py_int_mod return NULL (no exception) on a
                # zero divisor; surface ZeroDivisionError so it is catchable.
                self._emit_zero_division_if_null(
                    result,
                    "division by zero",
                    pinned_release_on_error=operand_cleanup,
                )
            else:
                self._guard_cpy_value_not_null(
                    result,
                    pinned_pcc_on_error=operand_cleanup,
                )
            if lhs_pinned:
                self._gc_unpin(lhs)
            if lhs_owned:
                self._gc_release(lhs)
            if rhs_pinned:
                self._gc_unpin(rhs)
            if rhs_owned:
                self._gc_release(rhs)
            return result
        if (
            isinstance(expr, UnaryOp)
            and expr.op == "-"
            and isinstance(expr.operand, (IntLit, BoolLit))
        ):
            # ``-<literal>`` is a compile-time constant.  Going through
            # py_int_neg costs the call, an operand pin/unpin pair, a
            # post-call error check and a NULL guard -- about ten basic
            # blocks per negative literal.  ``pcc/frontends/c/parse/c_parsetab.py``
            # alone holds 9,443 of them, and its module-level table
            # construction was 88.9 MB of IR / 175,239 blocks, of which the
            # post-call error checks were 60,670.
            literal = expr.operand
            if isinstance(literal, BoolLit):
                return self._emit_int_literal_object(
                    -1 if bool(literal.value) else 0
                )
            return self._emit_int_literal_object(-int(literal.value))
        if (
            isinstance(expr, UnaryOp)
            and expr.op in ("-", "~")
            and isinstance(expr.operand.ty, (IntType, BoolType))
        ):
            operand = self._emit_exact_int_operand_object(expr.operand)
            operand_owned = (
                isinstance(operand.type, ir.PointerType)
                and operand not in getattr(self, "_cpy_values", ())
                and self._pcc_pointer_source_is_owned(expr.operand)
            )
            operand_pinned = (
                isinstance(operand.type, ir.PointerType)
                and operand not in getattr(self, "_cpy_values", ())
            )
            operand_cleanup = ()
            if operand_pinned:
                self._gc_pin(operand)
                operand_cleanup = ((operand, operand_owned),)
            if expr.op == "~":
                result = self.builder.call(
                    self.runtime["py_int_xor"],
                    [operand, self._emit_int_literal_object(-1)],
                    name=self._fresh("exact.int.invert"),
                )
            else:
                result = self.builder.call(
                    self.runtime["py_int_neg"],
                    [operand],
                    name=self._fresh("exact.int.neg"),
                )
            self._emit_post_call_err_check(
                None,
                pinned_release_on_error=operand_cleanup,
            )
            self._guard_cpy_value_not_null(
                result,
                pinned_pcc_on_error=operand_cleanup,
            )
            if operand_pinned:
                self._gc_unpin(operand)
            if operand_owned:
                self._gc_release(operand)
            return result
        if isinstance(expr, IntLit):
            value = int(expr.value)
            if value < -(1 << 63) or value > (1 << 63) - 1:
                return self._emit_int_literal_object(value)
        if isinstance(expr, Subscript):
            if live_import_expr_binding(self, expr.obj):
                return self._take_slot_call_root(self._emit_slot_call_subscript(expr, "import.exact.item"))
            return self._emit_subscript_load_object(expr)
        return None

    def _int_expr_needs_exact_object_boundary(self, expr: Expr) -> bool:
        if not isinstance(expr.ty, IntType) or expr.ty.name in (
            "pcc.i64",
            "pcc.u64",
        ):
            return False
        if isinstance(expr, Attr):
            # An ordinary object's int field has no machine-width range
            # proof. Preserve its object projection when planning a local;
            # valueclass payload fields keep their explicit fixed-width lane.
            return not self._is_valueclass_payload_type(expr.obj.ty)
        if (
            isinstance(expr, Call)
            and self._native_builtin_value_kind_for_expr(expr.func)
            == "pcc.guarded_i64_dot"
        ):
            return True
        if isinstance(expr, Name):
            if getattr(self, "_exact_int_env_flags", {}).get(
                expr.ident,
                False,
            ):
                return True
            if expr.ident not in self.env:
                module_global = self._module_globals.get(expr.ident)
                if (
                    module_global is not None
                    and isinstance(module_global[1], IntType)
                    and isinstance(module_global[0].value_type, ir.PointerType)
                ):
                    return True
            return False
        if isinstance(expr, Call) and self._call_is_int_builtin(expr):
            # ADMISSION DEMOTION.  `int(<str>)` and `int(<dyn>)` have an
            # UNBOUNDED result range -- the text or the object can hold any
            # magnitude -- so the name they are written to must carry the object
            # projection, exactly as an over-i64 `IntLit` already does below.
            #
            # Without this the i64 lane silently truncated the value in every
            # mode where `_int_exprs_are_boxed()` is False, which is the mode
            # pcc1 itself is built in: the parser lifts literals through
            # `int(e.text, 0)`, so every source literal above 2**63-1 became 0
            # while the object-projection emit paths were never even reached.
            # `int(<int>)`, `int(<bool>)` and `int(<float>)` stay bounded and
            # keep the i64 lane.
            if expr.args:
                arg_ty = expr.args[0].ty
                if isinstance(arg_ty, (StrType, DynType)):
                    return True
                if isinstance(arg_ty, FloatType) and self._int_exprs_are_boxed():
                    # A float's integer value is unbounded (int(1e20)); boxed
                    # ints take the exact object, the raw lane keeps its
                    # checked conversion (``_emit_float_to_i64_lane``).
                    return True
                return self._int_expr_needs_exact_object_boundary(expr.args[0])
            return False
        if (
            isinstance(expr, Call)
            and isinstance(expr.func, Name)
            and expr.func.ident == "pow"
            and expr.func.ident not in self.functions
            and len(expr.args) == 2
            and not expr.kwargs
        ):
            # Two-argument ``pow`` is ``**`` spelled as a call: its range is
            # unbounded, and the call lowering already returns the py_int_pow
            # object.  Planned into the i64 lane, ``folded = pow(10, 30)``
            # unboxed that object to 0 (``py_int_to_i64`` above 2**63-1), so
            # pcc1's own ``**`` literal folder emitted ``10 ** 30`` as 0.
            return True
        if (
            isinstance(expr, Call)
            and isinstance(expr.func, Name)
            and expr.func.ident == "pow"
            and expr.func.ident not in self.functions
            and len(expr.args) == 3
            and not expr.kwargs
        ):
            # Three-argument ``pow`` returns the protocol's int object, bounded
            # by its modulus: it needs the exact object when the modulus does.
            return self._int_expr_needs_exact_object_boundary(expr.args[2])
        if isinstance(expr, Call) and isinstance(expr.func, Name):
            fn = self.functions.get(expr.func.ident)
            if fn is not None and isinstance(
                fn.function_type.return_type, ir.PointerType
            ):
                return True
        if isinstance(expr, IntLit):
            value = int(expr.value)
            return value < -(1 << 63) or value > (1 << 63) - 1
        if isinstance(expr, UnaryOp) and expr.op in ("-", "~"):
            # ``-x`` / ``~x`` of an exact operand is exact: the object
            # emitter above negates it with py_int_neg / py_int_xor, while
            # the i64 lane would unbox the operand first and raise (formerly
            # read 0) for anything outside the lane.  A literal operand folds
            # first: ``-0x8000000000000000`` is i64 min and stays in the lane.
            if isinstance(expr.operand, (IntLit, BoolLit)):
                literal = int(expr.operand.value)
                folded = -literal if expr.op == "-" else ~literal
                return folded < -(1 << 63) or folded > (1 << 63) - 1
            return self._int_expr_needs_exact_object_boundary(expr.operand)
        if isinstance(expr, IfExpr):
            return self._int_expr_needs_exact_object_boundary(
                expr.then_e
            ) or self._int_expr_needs_exact_object_boundary(expr.else_e)
        if isinstance(expr, BinOp):
            if expr.op == "**":
                return True
            if self._int_expr_needs_exact_object_boundary(
                expr.lhs
            ) or self._int_expr_needs_exact_object_boundary(expr.rhs):
                return True
            if isinstance(expr.lhs, IntLit) and isinstance(expr.rhs, IntLit):
                try:
                    lhs = int(expr.lhs.value)
                    rhs = int(expr.rhs.value)
                    if expr.op == "+":
                        value = lhs + rhs
                    elif expr.op == "-":
                        value = lhs - rhs
                    elif expr.op == "*":
                        value = lhs * rhs
                    elif expr.op == "//" and rhs != 0:
                        value = lhs // rhs
                    elif expr.op == "%" and rhs != 0:
                        value = lhs % rhs
                    elif expr.op == "<<":
                        if rhs < 0:
                            return True
                        if lhs == 0:
                            value = 0
                        elif rhs > 63:
                            # Classification only: avoid constructing an
                            # enormous host-Python integer just to prove that
                            # it cannot fit the raw i64 projection.
                            return True
                        else:
                            value = lhs << rhs
                    elif expr.op == ">>":
                        if rhs < 0:
                            return True
                        if rhs > 63:
                            value = -1 if lhs < 0 else 0
                        else:
                            value = lhs >> rhs
                    elif expr.op == "&":
                        value = lhs & rhs
                    elif expr.op == "|":
                        value = lhs | rhs
                    elif expr.op == "^":
                        value = lhs ^ rhs
                    else:
                        return False
                except (OverflowError, ValueError):
                    return True
                return value < -(1 << 63) or value > (1 << 63) - 1
            if expr.op == "<<":
                # A non-constant ordinary Python left shift has no i64 range
                # proof.  AArch64 masks a large machine shift count (1049 ->
                # 25), which silently wraps instead of promoting to bigint.
                # Literal/literal shifts were classified above; explicitly
                # typed pcc.i64/pcc.u64 expressions returned at function entry.
                return True
            return False
        if isinstance(expr, UnaryOp) and expr.op in ("-", "~"):
            if isinstance(expr.operand, IntLit):
                # Classify the folded signed literal, not its positive
                # operand.  2**63 needs an exact object by itself, while
                # -2**63 is the valid lower endpoint of the raw i64 lane.
                value = int(expr.operand.value)
                value = -value if expr.op == "-" else ~value
                return value < -(1 << 63) or value > (1 << 63) - 1
            if self._int_expr_needs_exact_object_boundary(expr.operand):
                return True
            return False
        return False

    def _emit_exact_int_compare(
        self,
        expr: Compare,
    ) -> Optional[ir.Value]:
        if expr.op not in ("==", "!=", "<", "<=", ">", ">="):
            return None
        if not (
            isinstance(expr.lhs.ty, (IntType, BoolType))
            and isinstance(expr.rhs.ty, (IntType, BoolType))
        ):
            return None
        if not (
            self._int_expr_needs_exact_object_boundary(expr.lhs)
            or self._int_expr_needs_exact_object_boundary(expr.rhs)
        ):
            return None
        lhs = self._emit_exact_int_operand_object(expr.lhs)
        lhs_owned = (
            isinstance(lhs.type, ir.PointerType)
            and lhs not in getattr(self, "_cpy_values", ())
            and self._pcc_pointer_source_is_owned(expr.lhs)
        )
        lhs_pinned = (
            isinstance(lhs.type, ir.PointerType)
            and lhs not in getattr(self, "_cpy_values", ())
        )
        lhs_cleanup = ()
        lhs_root_slot = None
        lhs_root_lifetimes = ()
        lhs_generator_slot = None
        lhs_quiet_unpinned = False
        if lhs_pinned:
            generator_stack = getattr(self, "_generator_ctx_stack", ())
            if generator_stack:
                hidden = vthread_delegate_frame_name(
                    expr,
                    "pcc.exact_int.compare.lhs",
                )
                frame_entry = generator_stack[-1]["frame_slots"].get(hidden)
                if frame_entry is None:
                    raise L1CodegenError(
                        "exact-int comparison is missing its managed lhs "
                        "frame slot"
                    )
                lhs_generator_slot = frame_entry[1]
                lhs_generator_root = self._as_gc_ptr(
                    lhs_generator_slot,
                    name=self._fresh("exact.int.compare.lhs.frame.ptr"),
                )
                self._gc_pin(lhs)
                self.builder.call(
                    self.runtime["pcc_gc_store_root"],
                    [lhs_generator_root, lhs],
                )
                self._gc_unpin(lhs)
                if lhs_owned:
                    self._gc_release(lhs)

                prior_pcc_target = self._current_try_err_block()
                pcc_target = prior_pcc_target
                if pcc_target is None:
                    pcc_target = self._ensure_fn_err_exit()
                save_block = self.builder._block
                pcc_cleanup = self.current_function.append_basic_block(
                    name=self._fresh("exact.int.compare.lhs.frame.err")
                )
                self.builder.position_at_end(pcc_cleanup)
                pcc_cleanup_root = self._as_gc_ptr(
                    lhs_generator_slot,
                    name=self._fresh("exact.int.compare.lhs.frame.err.ptr"),
                )
                self.builder.call(
                    self.runtime["pcc_gc_store_root"],
                    [pcc_cleanup_root, self._emit_none_literal()],
                )
                self.builder.branch(pcc_target)
                self.builder.position_at_end(save_block)

                prior_cpy_target = getattr(
                    self,
                    "_cpy_operand_cleanup_block",
                    None,
                )
                cpy_target = prior_cpy_target
                if cpy_target is None:
                    cpy_target = pcc_target
                cpy_cleanup = pcc_cleanup
                if cpy_target is not pcc_target:
                    cpy_cleanup = self.current_function.append_basic_block(
                        name=self._fresh("exact.int.compare.lhs.frame.cpy.err")
                    )
                    self.builder.position_at_end(cpy_cleanup)
                    cpy_cleanup_root = self._as_gc_ptr(
                        lhs_generator_slot,
                        name=self._fresh(
                            "exact.int.compare.lhs.frame.cpy.err.ptr"
                        ),
                    )
                    self.builder.call(
                        self.runtime["pcc_gc_store_root"],
                        [cpy_cleanup_root, self._emit_none_literal()],
                    )
                    self.builder.branch(cpy_target)
                    self.builder.position_at_end(save_block)
                self._try_err_block = pcc_cleanup
                self._cpy_operand_cleanup_block = cpy_cleanup
                try:
                    rhs = self._emit_expr_as_pcc_object(expr.rhs)
                finally:
                    self._try_err_block = prior_pcc_target
                    self._cpy_operand_cleanup_block = prior_cpy_target

                resumed_lhs_root = self._as_gc_ptr(
                    lhs_generator_slot,
                    name=self._fresh("exact.int.compare.lhs.frame.resume.ptr"),
                )
                rooted_lhs = self.builder.call(
                    self.runtime["pcc_gc_load_ptr"],
                    [ir.Constant(_CSTR, None), resumed_lhs_root],
                    name=self._fresh("exact.int.compare.lhs.frame.rooted"),
                )
                lhs = self._gc_retain(
                    rooted_lhs,
                    name=self._fresh("exact.int.compare.lhs.frame.retain"),
                )
                self.builder.call(
                    self.runtime["pcc_gc_store_root"],
                    [resumed_lhs_root, self._emit_none_literal()],
                )
                lhs_owned = True
                self._gc_pin(lhs)
                lhs_cleanup = ((lhs, True),)
            elif self._exact_int_operand_is_gc_quiet(expr.rhs):
                lhs_quiet_unpinned = True
            else:
                lhs_root_slot = self._enter_container_temp_root(
                    lhs,
                    "exact.int.compare.lhs",
                )
                lhs_root_lifetimes = ((lhs_root_slot, lhs_owned),)
        if lhs_generator_slot is None:
            rhs = self._emit_expr_with_cpy_operand_cleanup(
                expr.rhs,
                (),
                as_pcc_object=True,
                rooted_pcc_lifetimes=lhs_root_lifetimes,
            )
        if lhs_root_slot is not None:
            lhs = self.builder.call(
                self.runtime["pcc_gc_load_ptr"],
                [
                    ir.Constant(_CSTR, None),
                    self._as_gc_ptr(
                        lhs_root_slot,
                        name=self._fresh("exact.int.compare.lhs.root.ptr"),
                    ),
                ],
                name=self._fresh("exact.int.compare.lhs.rooted"),
            )
            self._release_rooted_pcc_lifetimes(((lhs_root_slot, False),))
            self._gc_pin(lhs)
            lhs_cleanup = ((lhs, lhs_owned),)
        rhs_owned = (
            isinstance(rhs.type, ir.PointerType)
            and rhs not in getattr(self, "_cpy_values", ())
            and self._pcc_pointer_source_is_owned(expr.rhs)
        )
        rhs_pinned = (
            isinstance(rhs.type, ir.PointerType)
            and rhs not in getattr(self, "_cpy_values", ())
        )
        pred = {
            "==": "==",
            "!=": "!=",
            "<": "<",
            "<=": "<=",
            ">": ">",
            ">=": ">=",
        }[expr.op]
        slow_pins: tuple[ir.Value, ...] = ()
        if lhs_quiet_unpinned:
            slow_pins = slow_pins + (lhs,)
        if rhs_pinned:
            slow_pins = slow_pins + (rhs,)
        result_i1 = self._emit_inline_tagged_int_compare_or_call(
            pred,
            lhs,
            rhs,
            slow_pins=slow_pins,
        )
        if lhs_pinned and not lhs_quiet_unpinned:
            self._gc_unpin(lhs)
        if lhs_owned:
            self._gc_release(lhs)
        if rhs_owned:
            self._gc_release(rhs)
        return result_i1

    def _exact_int_operand_is_gc_quiet(self, expr: Expr) -> bool:
        """True when lowering *expr* cannot allocate, raise or run a collector.

        A bound local or module global is a plain (rooted) load and an int or
        bool literal is a constant.  Everything else -- calls, subscripts,
        attribute loads, nested arithmetic -- may reach the runtime and keeps
        the temporary-root protocol around the earlier operand.
        """
        if isinstance(expr, (IntLit, BoolLit)):
            return True
        if isinstance(expr, Name):
            if expr.ident in getattr(self, "_cpy_env_flags", {}):
                return False
            return expr.ident in self.env or expr.ident in self._module_globals
        if isinstance(expr, Attr) and isinstance(expr.obj, Name):
            # A slot read (`py_instance_get_field`) on a class-hinted receiver
            # retains and returns the field; it never allocates or raises.
            hint = self._class_hint_for_expr(expr.obj)
            if hint is None or not hasattr(self, "class_lowering"):
                return False
            info = self.class_lowering.classes.get(hint)
            if info is None:
                return False
            return self.class_lowering.lookup_field_index(info, expr.name) is not None
        return False

    def _emit_inline_tagged_int_compare_or_call(
        self,
        pred: str,
        lhs: ir.Value,
        rhs: ir.Value,
        *,
        slow_pins: tuple[ir.Value, ...] = (),
    ) -> ir.Value:
        """``lhs <pred> rhs`` on exact ints: compare tagged bits inline, else
        pin the operands around ``py_int_cmp`` (which cannot raise)."""
        fn = self.current_function
        ptr_one = ir.Constant(_I64, 1)
        lhs_bits = self.builder.ptrtoint(lhs, _I64, name=self._fresh("cmp.l.bits"))
        rhs_bits = self.builder.ptrtoint(rhs, _I64, name=self._fresh("cmp.r.bits"))
        both_tagged = self.builder.and_(
            self.builder.icmp_signed(
                "==",
                self.builder.and_(lhs_bits, ptr_one, name=self._fresh("cmp.l.low")),
                ptr_one,
                name=self._fresh("cmp.l.ok"),
            ),
            self.builder.icmp_signed(
                "==",
                self.builder.and_(rhs_bits, ptr_one, name=self._fresh("cmp.r.low")),
                ptr_one,
                name=self._fresh("cmp.r.ok"),
            ),
            name=self._fresh("cmp.both"),
        )
        fast_bb = fn.append_basic_block(name=self._fresh("int.cmp.fast"))
        slow_bb = fn.append_basic_block(name=self._fresh("int.cmp.slow"))
        join_bb = fn.append_basic_block(name=self._fresh("int.cmp.join"))
        self.builder.cbranch(both_tagged, fast_bb, slow_bb)

        self.builder.position_at_end(fast_bb)
        # Tagged values keep their order under the shift, so compare the raw
        # bits directly.
        fast = self.builder.icmp_signed(pred, lhs_bits, rhs_bits, name=self._fresh("cmp.fast"))
        fast_exit = self.builder._block
        self.builder.branch(join_bb)

        self.builder.position_at_end(slow_bb)
        for pinned in slow_pins:
            self._gc_pin(pinned)
        cmp_i32 = self.builder.call(
            self.runtime["py_int_cmp"],
            [lhs, rhs],
            name=self._fresh("exact.int.cmp"),
        )
        for pinned in reversed(slow_pins):
            self._gc_unpin(pinned)
        slow = self.builder.icmp_signed(
            pred,
            cmp_i32,
            ir.Constant(_I32, 0),
            name=self._fresh("exact.int.cmp.i1"),
        )
        slow_exit = self.builder._block
        self.builder.branch(join_bb)

        self.builder.position_at_end(join_bb)
        phi = self.builder.phi(_I1, name=self._fresh("int.cmp.result"))
        phi.add_incoming(fast, fast_exit)
        phi.add_incoming(slow, slow_exit)
        return phi

    def _emit_exact_int_operand_object(self, expr: Expr) -> ir.Value:
        exact = self._maybe_emit_exact_int_object(expr)
        if exact is not None:
            return exact
        if isinstance(expr, IntLit):
            # ``getattr``: the literal's exact object, never an i64-lane read.
            return self._emit_int_literal_object(getattr(expr, "value"))
        if isinstance(expr, BoolLit):
            return self.builder.call(
                self.runtime["py_int_from_i64"],
                [ir.Constant(_I64, 1 if bool(expr.value) else 0)],
                name=self._fresh("print.int.box"),
            )
        value = self._emit_expr(expr)
        if isinstance(value.type, ir.PointerType):
            return self._emit_value_as_pcc_object_or_bridge(
                value,
                expr.ty,
                "exact.int.cpy.bridge",
            )
        i64 = self._to_int64(value, expr.ty)
        return self.builder.call(
            self.runtime["py_int_from_i64"],
            [i64],
            name=self._fresh("exact.int.box"),
        )

    def _emit_expr_as_pcc_object(self, expr: Expr) -> ir.Value:
        if isinstance(expr.ty, IntType):
            exact = self._maybe_emit_exact_int_object(expr)
            if exact is not None:
                return exact
        if isinstance(expr, IfExpr):
            return self._emit_if_expr_as_pcc_object(expr)
        if isinstance(expr, BoolExpr):
            return self._emit_boolexpr_as_pcc_object(expr)
        valueclass_payload = self._maybe_emit_valueclass_constructor_payload(
            expr.ty,
            expr,
        )
        if valueclass_payload is not None:
            boxed_valueclass = self._emit_valueclass_payload_to_object(
                valueclass_payload,
                expr.ty,
                consume_fields=True,
            )
            if boxed_valueclass is not None:
                return boxed_valueclass
        value = self._emit_expr(expr)
        if value in getattr(self, "_cpy_values", ()):
            # CPython-bridge result (e.g. ``random.randint(...)`` as a
            # comprehension element) — convert to a pcc object before it
            # is stored into a pcc container, or the raw foreign pointer
            # would flow into native int/str ops and fail there.
            return self._emit_value_as_pcc_object_or_bridge(
                value,
                expr.ty,
                "as_pcc.cpy.bridge",
            )
        boxed_valueclass = self._emit_valueclass_payload_to_object(value, expr.ty)
        if boxed_valueclass is not None:
            return boxed_valueclass
        return marshal.marshal_to_object(
            self.builder,
            self.module,
            self.runtime,
            value,
            expr.ty,
        )

    def _emit_subscript_load_object(self, expr: Subscript) -> Optional[ir.Value]:
        if isinstance(expr.idx, Slice):
            return None
        native_os_environ_item = self._emit_native_os_environ_subscript(expr)
        if native_os_environ_item is not None:
            return native_os_environ_item
        obj_ty = expr.obj.ty
        obj = self._emit_expr(expr.obj)
        if obj in self._cpy_values:
            return None
        exact_container = self._emit_exact_container_subscript_load_object(expr, obj)
        if exact_container is not None:
            got, _, _root, _root_ptr = exact_container
            return got
        if isinstance(obj_ty, DynType):
            key_obj = self._emit_subscript_key_object(expr.idx)
            got = self.builder.call(
                self.runtime["py_obj_getitem"],
                [obj, key_obj],
                name=self._fresh("obj.getitem.obj"),
            )
            self._gc_release_if_owned(obj, expr.obj)
            return got
        return None
