"""Generator lowering helpers for L1CodeGen."""

from __future__ import annotations

import os
import sys
from typing import Optional

from pcc.llvm_capi.compat import ir

from ..py_ast import (
    Assign,
    Attr,
    AugAssign,
    BinOp,
    BoolType,
    Call,
    ClassDef,
    Compare,
    DynType,
    Expr,
    ExprStmt,
    For,
    FuncDef,
    If,
    IntType,
    Name,
    NoneType,
    Return,
    Stmt,
    Try,
    TupleExpr,
    While,
    With,
)
from . import marshal
from .errors import L1CodegenError
from .hoist_analysis import _dataclass_field_names as _ast_field_names
from .runtime_abi import declare_runtime_global
from .vthread_effect_analysis import (
    vthread_delegate_frame_name,
    vthread_method_owner_for_funcdef,
    vthread_proven_direct_name_call,
    vthread_proven_export_method_call_key,
    vthread_proven_method_call_key,
    vthread_proven_suspension_call_key,
)

_I1 = ir.IntType(1)
_I8 = ir.IntType(8)
_I32 = ir.IntType(32)
_I64 = ir.IntType(64)
_VOID = ir.VoidType()
_CSTR = _I8.as_pointer()
_STOP_ITERATION_TAG = 8


def emit_generator_may_park_call(
    host,
    expr: Call,
    callee_name: str,
    *,
    fn=None,
    ast_func_def: Optional[FuncDef] = None,
    effect_proven: bool = False,
) -> Optional[ir.Value]:
    """Delegate one ordinary call to a transitive ``may_park`` generator.

    Effect analysis has already changed both caller and callee to the existing
    native generator ABI.  This routine supplies the missing transparent-call
    bridge: it drives the child until it yields or completes, forwards each
    suspension through the parent, and returns ``StopIteration.value`` in the
    source expression's semantic representation.

    The child generator lives in a hidden managed frame slot.  Consequently a
    parent suspension never leaves the only live child reference in an SSA
    temporary or raw stack address, and relocating collectors can update it
    through the normal generator-frame/root contract.
    """
    if len(getattr(host, "_generator_ctx_stack", ())) == 0:
        return None
    if not effect_proven:
        may_park_names = getattr(host, "_vthread_may_park_func_names", set())
        if callee_name not in may_park_names:
            return None
        current_fd = getattr(host, "current_func_def", None)
        if (
            current_fd is None
            or vthread_proven_direct_name_call(
                current_fd,
                expr,
                may_park_names,
                getattr(host, "_vthread_binding_cache", None),
            )
            is None
        ):
            return None
    elif callee_name == "pcc.virtual_thread.call":
        current_fd = getattr(host, "current_func_def", None)
        if (
            current_fd is None
            or vthread_proven_suspension_call_key(
                host.ast_module,
                current_fd,
                expr,
                getattr(host, "_vthread_binding_cache", None),
            )
            != "pcc.virtual_thread.call"
        ):
            return None
    if fn is None:
        fn = host.functions.get(callee_name)
    if fn is None:
        return None
    if ast_func_def is None:
        ast_func_def = host._find_user_funcdef(callee_name)
    if ast_func_def is None:
        return None
    if ast_func_def.is_async:
        raise L1CodegenError(
            "may_park delegation does not accept async function "
            + repr(callee_name)
        )

    child = host._emit_direct_user_function_call(
        display_name=callee_name,
        fn=fn,
        ast_func_def=ast_func_def,
        args=expr.args,
        kwargs=expr.kwargs,
    )
    return emit_generator_may_park_child(
        host,
        expr,
        callee_name,
        child,
    )


def emit_generator_may_park_child(
    host,
    expr: Call,
    callee_name: str,
    child: ir.Value,
    pinned_arg_provenance=(),
    child_already_rooted: bool = False,
) -> ir.Value:
    """Drive an already-created child continuation through its parent."""
    if len(getattr(host, "_generator_ctx_stack", ())) == 0:
        raise L1CodegenError(
            "may_park child delegation requires a generator parent: "
            + callee_name
        )
    child_slot, initial_child_root_ptr = generator_may_park_child_slot(
        host, expr, callee_name
    )
    if not child_already_rooted:
        host.builder.call(host.runtime["pcc_gc_pin"], [child])
        host.builder.call(
            host.runtime["pcc_gc_store_root"],
            [initial_child_root_ptr, child],
        )
        host.builder.call(
            host.runtime["pcc_gc_unpin"],
            [host._value_available_at_insertion_point(child)],
        )
        # The root store retained the replacement and released the slot's prior
        # owner. Consume the direct call's owned temporary so the hidden slot
        # is the single local owner.
        host._gc_release(child)
    # Direct method calls pin receiver/argument temporaries across the call.
    # Keep those pins until the returned child has entered its traced frame
    # slot; releasing them earlier would put the only child reference in SSA
    # across collector-visible cleanup calls.
    for value, managed, raw, owned in pinned_arg_provenance:
        if managed:
            host.builder.call(
                host.runtime["pcc_gc_unpin"],
                [host._value_available_at_insertion_point(value)],
            )
            if owned:
                host._gc_release(
                    value,
                    host._release_context_label("method_call_arg"),
                )
        elif raw and owned:
            raise L1CodegenError(
                "raw method argument cannot carry pcc ownership"
            )

    parent_fn = host.current_function
    next_bb = parent_fn.append_basic_block(
        name=host._fresh("vthread.delegate.next")
    )
    yielded_bb = parent_fn.append_basic_block(
        name=host._fresh("vthread.delegate.yielded")
    )
    stopped_bb = parent_fn.append_basic_block(
        name=host._fresh("vthread.delegate.stopped")
    )
    completed_bb = parent_fn.append_basic_block(
        name=host._fresh("vthread.delegate.completed")
    )
    propagate_bb = parent_fn.append_basic_block(
        name=host._fresh("vthread.delegate.propagate")
    )
    fast_completed = str(
        os.environ.get("PCC_FAST_COMPLETED_CONTINUATIONS", "0") or "0"
    ).strip().lower() in ("1", "true", "yes", "on")
    result_ready_bb = None
    if fast_completed:
        result_ready_bb = parent_fn.append_basic_block(
            name=host._fresh("vthread.delegate.result.ready")
        )

    host.builder.branch(next_bb)
    host.builder.position_at_end(next_bb)
    current_child = host.builder.load(
        child_slot,
        name=host._fresh("vthread.delegate.child"),
    )
    if fast_completed:
        immediate = host.builder.call(
            host.runtime["py_gen_take_completed"],
            [current_child],
            name=host._fresh("vthread.delegate.completed.borrowed"),
        )
        is_immediate = host.builder.icmp_unsigned(
            "!=", immediate, ir.Constant(_CSTR, None),
            name=host._fresh("vthread.delegate.completed.ready"),
        )
        immediate_bb = parent_fn.append_basic_block(
            name=host._fresh("vthread.delegate.completed.capture")
        )
        step_bb = parent_fn.append_basic_block(
            name=host._fresh("vthread.delegate.step")
        )
        host.builder.cbranch(is_immediate, immediate_bb, step_bb)
        host.builder.position_at_end(immediate_bb)
        # The generator still owns its borrowed value. Capture it before
        # this retaining root store releases the generator in the same slot.
        host.builder.call(
            host.runtime["pcc_gc_store_root"],
            [host._as_gc_ptr(child_slot), immediate],
        )
        host.builder.branch(result_ready_bb)
        host.builder.position_at_end(step_bb)
        current_child = host.builder.call(
            host.runtime["pcc_gc_load_ptr"],
            [ir.Constant(_CSTR, None), host._as_gc_ptr(child_slot)],
            name=host._fresh("vthread.delegate.step.child"),
        )
    yielded = host.builder.call(
        host.runtime["py_gen_next"],
        [current_child],
        name=host._fresh("vthread.delegate.value"),
    )
    is_stopped = host.builder.icmp_unsigned(
        "==",
        yielded,
        ir.Constant(_CSTR, None),
        name=host._fresh("vthread.delegate.is_stopped"),
    )
    host.builder.cbranch(is_stopped, stopped_bb, yielded_bb)

    host.builder.position_at_end(yielded_bb)
    outer_err_target = getattr(host, "_try_err_block", None)
    if outer_err_target is None:
        outer_err_target = host._ensure_fn_err_exit()
    child_unwind_bb = parent_fn.append_basic_block(
        name=host._fresh("vthread.delegate.child.unwind")
    )
    host._emit_generator_yield_value(
        yielded,
        resume_err_target=child_unwind_bb,
    )
    resume_ok_bb = host.builder._block
    host.builder.position_at_end(child_unwind_bb)
    unwind_child = host.builder.load(
        child_slot,
        name=host._fresh("vthread.delegate.child.unwind.value"),
    )
    host.builder.call(
        host.runtime["py_gen_close_preserving_exception"],
        [unwind_child],
        name=host._fresh("vthread.delegate.child.close.rc"),
    )
    unwind_root_ptr = host._as_gc_ptr(
        child_slot,
        name=host._fresh("vthread.delegate.child.unwind.root"),
    )
    host.builder.call(
        host.runtime["pcc_gc_store_root"],
        [unwind_root_ptr, host._emit_none_literal()],
    )
    host.builder.branch(outer_err_target)
    host.builder.position_at_end(resume_ok_bb)
    if not host._builder_block_is_terminated():
        host.builder.branch(next_bb)

    host.builder.position_at_end(stopped_bb)
    current_exc = host.builder.call(
        host.runtime["py_current_exception"],
        [],
        name=host._fresh("vthread.delegate.exc"),
    )
    stop_cls = host.builder.call(
        host.runtime["py_exc_builtin_class"],
        [ir.Constant(_I64, _STOP_ITERATION_TAG)],
        name=host._fresh("vthread.delegate.stop_cls"),
    )
    stop_match = host.builder.call(
        host.runtime["py_exc_matches"],
        [current_exc, stop_cls],
        name=host._fresh("vthread.delegate.stop_match"),
    )
    is_stop_iteration = host.builder.icmp_signed(
        "!=",
        stop_match,
        ir.Constant(_I64, 0),
        name=host._fresh("vthread.delegate.is_stop_iteration"),
    )
    host.builder.cbranch(is_stop_iteration, completed_bb, propagate_bb)

    host.builder.position_at_end(propagate_bb)
    err_target = getattr(host, "_try_err_block", None)
    if err_target is None:
        err_target = host._ensure_fn_err_exit()
    host.builder.branch(err_target)

    host.builder.position_at_end(completed_bb)
    result_obj = host.builder.call(
        host.runtime["py_exc_get_message"],
        [current_exc],
        name=host._fresh("vthread.delegate.result.borrowed"),
    )
    result_is_null = host.builder.icmp_unsigned(
        "==",
        result_obj,
        ir.Constant(_CSTR, None),
        name=host._fresh("vthread.delegate.result.is_null"),
    )
    result_or_none = host.builder.select(
        result_is_null,
        host._emit_none_literal(),
        result_obj,
        name=host._fresh("vthread.delegate.result"),
    )
    result_root_ptr = host._as_gc_ptr(
        child_slot,
        name=host._fresh("vthread.delegate.result.root"),
    )
    # Move the StopIteration value into the same traced frame slot before
    # clearing the exception that currently owns it.  The slot store also
    # releases the completed child generator.
    host.builder.call(
        host.runtime["pcc_gc_store_root"],
        [result_root_ptr, result_or_none],
    )
    host.builder.call(host.runtime["py_clear_exception"], [])
    if fast_completed:
        host.builder.branch(result_ready_bb)
        host.builder.position_at_end(result_ready_bb)
        result_root_ptr = host._as_gc_ptr(
            child_slot, name=host._fresh("vthread.delegate.result.ready.root")
        )
    rooted_result = host.builder.call(
        host.runtime["pcc_gc_load_ptr"],
        [ir.Constant(_CSTR, None), result_root_ptr],
        name=host._fresh("vthread.delegate.result.rooted"),
    )

    result_ty = getattr(expr, "ty", None)
    if result_ty is None or isinstance(result_ty, NoneType):
        host.builder.call(
            host.runtime["pcc_gc_store_root"],
            [result_root_ptr, host._emit_none_literal()],
        )
        return host._emit_none_literal()
    if host._is_valueclass_payload_type(result_ty):
        payload = host._emit_object_to_valueclass_payload(
            rooted_result,
            result_ty,
        )
        if payload is None:
            raise L1CodegenError(
                "may_park call cannot restore value payload from "
                + repr(callee_name)
            )
        host._emit_post_call_err_check(expr.span)
        host.builder.call(
            host.runtime["pcc_gc_store_root"],
            [result_root_ptr, host._emit_none_literal()],
        )
        return payload
    if host._is_object(result_ty):
        owned_result = host._gc_retain(
            rooted_result,
            name=host._fresh("vthread.delegate.result.retain"),
        )
        host.builder.call(
            host.runtime["pcc_gc_store_root"],
            [result_root_ptr, host._emit_none_literal()],
        )
        return owned_result
    native_result = marshal.marshal_from_object(
        host.builder,
        host.module,
        host.runtime,
        rooted_result,
        result_ty,
    )
    host._emit_post_call_err_check(expr.span)
    host.builder.call(
        host.runtime["pcc_gc_store_root"],
        [result_root_ptr, host._emit_none_literal()],
    )
    return native_result


def generator_may_park_child_slot(host, expr: Call, callee_name: str):
    """Return the compiler-managed frame slot for one delegation call site."""
    if len(getattr(host, "_generator_ctx_stack", ())) == 0:
        raise L1CodegenError(
            "may_park child delegation requires a generator parent: "
            + callee_name
        )
    ctx = host._generator_ctx_stack[-1]
    hidden_name = vthread_delegate_frame_name(expr, callee_name)
    frame_entry = ctx["frame_slots"].get(hidden_name)
    if frame_entry is None:
        planned = sorted(
            name for name in ctx["frame_slots"] if str(name).startswith("__pcc_vthread")
        )
        raise L1CodegenError(
            "may_park call missing managed child frame slot: " + callee_name
            + " (wanted " + hidden_name + "; planned delegation slots: "
            + (", ".join(planned) if planned else "none") + ")"
        )
    child_slot = frame_entry[1]
    # This address is reused by sibling retry, cleanup and resume blocks.  A
    # cast emitted at the current insertion point does not necessarily
    # dominate those blocks (the TCP observe/park/retry lowering exposed that
    # with the cast in one retry arm and uses in another).  Materialize the
    # stable frame-slot address in the function entry, before its terminator,
    # just like the entry GC-frame registration helpers do.
    saved_block = host.builder._block
    host._position_at_entry_hoist_point()
    root_ptr = host._as_gc_ptr(
        child_slot,
        name=host._fresh("vthread.delegate.child.root"),
    )
    host.builder.position_at_end(saved_block)
    return child_slot, root_ptr


def _dataclass_field_value(obj, field_name: str, default=None):
    return getattr(obj, field_name, default)


def _dataclass_field_names(obj):
    if obj is None:
        return ()
    if isinstance(obj, Call):
        return ("span", "ty", "func", "args", "kwargs")
    if isinstance(obj, Name):
        return ("span", "ty", "ident")
    if isinstance(obj, TupleExpr):
        return ("span", "ty", "elems")
    if isinstance(obj, Expr):
        # Native dataclasses do not expose CPython's reflection dictionary.
        # Use the shared AST schema so nested expressions retain their child
        # continuation slots in both the host and self-hosted compiler.
        return _ast_field_names(obj)
    if isinstance(obj, Assign):
        return ("span", "targets", "value", "annotation")
    if isinstance(obj, AugAssign):
        return ("span", "target", "op", "value")
    if isinstance(obj, ExprStmt):
        return ("span", "expr")
    if isinstance(obj, If):
        return ("span", "cond", "body", "else_body")
    if isinstance(obj, While):
        return ("span", "cond", "body", "else_body")
    if isinstance(obj, For):
        return ("span", "target", "iter", "body", "else_body")
    if isinstance(obj, Return):
        return ("span", "value")
    if isinstance(obj, Try):
        return ("span", "body", "handlers", "else_body", "finally_body")
    if isinstance(obj, With):
        return ("span", "items", "body")
    if isinstance(obj, FuncDef):
        return (
            "span",
            "name",
            "args",
            "return_ty",
            "body",
            "decorators",
            "is_method",
            "is_async",
        )
    if isinstance(obj, ClassDef):
        return ("span", "name", "bases", "keywords", "body", "decorators")
    if isinstance(obj, Stmt):
        return _ast_field_names(obj)
    return ()


class GeneratorLoweringMixin:
    def _vthread_suspension_call(
        self,
        expr: Expr,
        fd: Optional[FuncDef] = None,
    ) -> bool:
        if not isinstance(expr, Call):
            return False
        if fd is None:
            fd = getattr(self, "current_func_def", None)
        if fd is None:
            return False
        return (
            vthread_proven_suspension_call_key(
                self.ast_module,
                fd,
                expr,
                self._vthread_binding_cache,
            )
            is not None
        )

    def _vthread_dynamic_callback_call(
        self,
        expr: Expr,
        fd: Optional[FuncDef] = None,
    ) -> bool:
        if fd is None:
            fd = getattr(self, "current_func_def", None)
        if fd is None:
            return False
        return (
            vthread_proven_suspension_call_key(
                self.ast_module,
                fd,
                expr,
                self._vthread_binding_cache,
            )
            == "pcc.virtual_thread.call"
        )

    def _funcdef_is_continuation_factory(self, fd: FuncDef) -> bool:
        if fd is None:
            return False
        for decorator in fd.decorators:
            if self._native_builtin_value_kind_for_expr(decorator) == "pcc.virtual_thread.continuation_factory":
                return True
        return False

    def _funcdef_has_yield_sentinel(self, fd: FuncDef) -> bool:
        """Return True when ``fd`` contains a parser-lifted yield call.

        This is deliberately iterative rather than a nested walker so
        self-hosted pcc does not need closure conversion just to
        recognize generator functions.
        """
        cache_key = id(fd)
        if cache_key in self._funcdef_yield_sentinel_cache:
            return self._funcdef_yield_sentinel_cache[cache_key]
        if (
            cache_key in getattr(self, "_vthread_may_park_func_ids", set())
            or cache_key
            in getattr(self, "_vthread_may_park_method_ids", set())
        ):
            # ``may_park`` functions use the same heap-owned state-machine
            # shape as source generators, but calls between them are
            # transparently delegated by ``emit_generator_may_park_call``.
            self._funcdef_yield_sentinel_cache[cache_key] = True
            return True
        stack = []
        for stmt in fd.body:
            stack.append(stmt)
        while stack:
            node = stack.pop()
            if node is None:
                continue
            if isinstance(node, FuncDef) or isinstance(node, ClassDef):
                continue
            if (
                isinstance(node, Call)
                and isinstance(node.func, Name)
                and node.func.ident
                in (
                    "_yield",
                    "_yield_from",
                    "__yield__",
                    "__yield_from__",
                )
            ):
                self._funcdef_yield_sentinel_cache[cache_key] = True
                return True
            if self._vthread_suspension_call(node, fd):
                self._funcdef_yield_sentinel_cache[cache_key] = True
                return True
            for slot in _dataclass_field_names(node):
                if slot == "span":
                    continue
                value = _dataclass_field_value(node, slot, None)
                if isinstance(value, tuple):
                    for item in value:
                        stack.append(item)
                else:
                    stack.append(value)
        self._funcdef_yield_sentinel_cache[cache_key] = False
        return False

    def _yield_sentinel_call(self, expr: Expr) -> Optional[tuple[str, Call]]:
        if (
            isinstance(expr, Call)
            and isinstance(expr.func, Name)
            and expr.func.ident
            in (
                "_yield",
                "__yield__",
                "_yield_from",
                "__yield_from__",
            )
        ):
            if expr.func.ident in ("_yield_from", "__yield_from__"):
                return ("yield_from", expr)
            return ("yield", expr)
        return None

    def _generator_yield_from_iter_name(self, expr: Expr) -> str:
        span = expr.span
        return f"__pcc_yield_from_iter_{span.line}_{span.col}"

    def _generator_for_iter_name(self, stmt: For) -> str:
        span = stmt.span
        return f"__pcc_for_iter_{span.line}_{span.col}"

    def _generator_enum_cnt_name(self, stmt: For) -> str:
        # ``for ... in enumerate(...)`` desugars (in _normalise_for_enumerate)
        # to a synthetic running counter that is created *during* _emit_for,
        # i.e. after _collect_generator_frame_names has already walked the
        # original AST.  Both sides agree on this deterministic span-keyed
        # name so the counter gets a persisted generator frame slot and
        # survives yields (otherwise the index resets to NULL on resume).
        span = stmt.span
        return f"__pcc_enum_cnt_{span.line}_{span.col}"

    def _generator_return_value_name(self, stmt: Return) -> str:
        span = stmt.span
        return f"__pcc_return_value_{span.line}_{span.col}"

    def _generator_print_args_name(self, call: Call) -> str:
        """Name the persisted tuple used by a multi-argument ``print``.

        A print argument may suspend after earlier arguments have already
        populated the tuple.  Generator resume dispatch enters after that
        suspension, so an SSA-only tuple created in ``gen.start`` would not
        dominate the remaining tuple writes.  A deterministic hidden frame
        name lets print lowering keep the partially populated tuple in the
        same heap-owned state as ordinary generator locals.
        """
        span = call.span
        return f"__pcc_print_args_{span.line}_{span.col}"

    def _generator_finally_exception_name(self, stmt: Try) -> str:
        span = stmt.span
        return f"__pcc_finally_exception_{span.line}_{span.col}"

    def _generator_handler_exception_name(self, stmt: Try, index: int) -> str:
        span = stmt.span
        return f"__pcc_handler_exception_{span.line}_{span.col}_{index}"

    def _collect_generator_target_names(
        self,
        names: list[str],
        target: Expr,
    ) -> None:
        stack = [target]
        idx = 0
        while idx < len(stack):
            cur = stack[idx]
            idx += 1
            if isinstance(cur, Name):
                if cur.ident and cur.ident not in names:
                    names.append(cur.ident)
                continue
            if isinstance(cur, TupleExpr):
                for item in cur.elems:
                    stack.append(item)

    def _collect_generator_exact_int_frame_names(
        self,
        names: list[str],
        body,
    ) -> None:
        """Reserve managed operand roots independently of parking effects.

        Exact-int binary/comparison lowering evaluates the lhs before the rhs.
        In a generator that lhs must live in the heap frame while the rhs may
        allocate or trigger a relocating collection, even when the function
        has no virtual-thread ``may_park`` calls.  Keeping this inventory under
        the parking-effect guard made ordinary generators such as ``yield i*i``
        request a frame slot that had never been allocated.
        """
        work = list(body)
        idx = 0
        while idx < len(work):
            node = work[idx]
            idx += 1
            if node is None:
                continue
            if isinstance(node, tuple):
                work.extend(node)
                continue
            if isinstance(node, (FuncDef, ClassDef)):
                continue
            if (
                isinstance(node, BinOp)
                and isinstance(node.lhs.ty, (IntType, BoolType))
                and isinstance(node.rhs.ty, (IntType, BoolType))
            ):
                hidden = vthread_delegate_frame_name(
                    node,
                    "pcc.exact_int.lhs",
                )
                if hidden not in names:
                    names.append(hidden)
            if (
                isinstance(node, Compare)
                and isinstance(node.lhs.ty, (IntType, BoolType))
                and isinstance(node.rhs.ty, (IntType, BoolType))
            ):
                hidden = vthread_delegate_frame_name(
                    node,
                    "pcc.exact_int.compare.lhs",
                )
                if hidden not in names:
                    names.append(hidden)
            for slot_name in _dataclass_field_names(node):
                if slot_name == "span":
                    continue
                value = _dataclass_field_value(node, slot_name, None)
                if isinstance(value, tuple):
                    work.extend(value)
                else:
                    work.append(value)

    def _collect_generator_frame_names(
        self,
        fd: FuncDef,
        owner_class_name: Optional[str] = None,
    ) -> list[str]:
        names: list[str] = []

        for a in fd.args:
            if a.name != "":
                if a.name not in names:
                    names.append(a.name)

        work = []
        for stmt in fd.body:
            work.append(stmt)
        idx = 0
        while idx < len(work):
            s = work[idx]
            idx += 1
            if isinstance(s, FuncDef) or isinstance(s, ClassDef):
                continue
            if isinstance(s, ExprStmt):
                sentinel = self._yield_sentinel_call(s.expr)
                if sentinel is not None and sentinel[0] == "yield_from":
                    hidden = self._generator_yield_from_iter_name(s.expr)
                    if hidden not in names:
                        names.append(hidden)
                if (
                    isinstance(s.expr, Call)
                    and isinstance(s.expr.func, Name)
                    and s.expr.func.ident == "print"
                    and len(s.expr.args) > 1
                ):
                    hidden = self._generator_print_args_name(s.expr)
                    if hidden not in names:
                        names.append(hidden)
                continue
            if isinstance(s, Assign):
                for t in s.targets:
                    self._collect_generator_target_names(names, t)
                continue
            if isinstance(s, AugAssign):
                self._collect_generator_target_names(names, s.target)
                continue
            if isinstance(s, Return):
                if s.value is not None:
                    hidden = self._generator_return_value_name(s)
                    if hidden not in names:
                        names.append(hidden)
                continue
            if isinstance(s, For):
                hidden = self._generator_for_iter_name(s)
                if hidden not in names:
                    names.append(hidden)
                it = s.iter
                if (
                    isinstance(it, Call)
                    and isinstance(it.func, Name)
                    and it.func.ident == "enumerate"
                ):
                    enum_cnt = self._generator_enum_cnt_name(s)
                    if enum_cnt not in names:
                        names.append(enum_cnt)
                self._collect_generator_target_names(names, s.target)
                for item in s.body:
                    work.append(item)
                for item in s.else_body:
                    work.append(item)
                continue
            if isinstance(s, If):
                for item in s.body:
                    work.append(item)
                for item in s.else_body:
                    work.append(item)
                continue
            if isinstance(s, While):
                for item in s.body:
                    work.append(item)
                for item in s.else_body:
                    work.append(item)
                continue
            if isinstance(s, With):
                for _ctx, as_var in s.items:
                    if as_var is not None:
                        self._collect_generator_target_names(names, as_var)
                for item in s.body:
                    work.append(item)
                continue
            if isinstance(s, Try):
                if s.finally_body:
                    hidden = self._generator_finally_exception_name(s)
                    if hidden not in names:
                        names.append(hidden)
                for item in s.body:
                    work.append(item)
                for handler_index, h in enumerate(s.handlers):
                    hidden = self._generator_handler_exception_name(s, handler_index)
                    if hidden not in names:
                        names.append(hidden)
                    if h.name:
                        if h.name not in names:
                            names.append(h.name)
                    for item in h.body:
                        work.append(item)
                for item in s.else_body:
                    work.append(item)
                for item in s.finally_body:
                    work.append(item)

        self._collect_generator_exact_int_frame_names(names, fd.body)

        # A transitive parking call is an expression, not a source ``yield``.
        # Reserve one managed child-generator slot for every such call site,
        # including calls nested inside larger expressions.  This second walk
        # is intentionally generic so future expression forms do not silently
        # leave a child continuation in an SSA-only temporary across a park.
        may_park_names = getattr(self, "_vthread_may_park_func_names", set())
        may_park_method_keys = getattr(
            self,
            "_vthread_may_park_method_keys",
            set(),
        )
        if may_park_names or may_park_method_keys:
            current_class_name = vthread_method_owner_for_funcdef(
                self.ast_module,
                fd,
                self._vthread_binding_cache,
            )
            if current_class_name is None:
                current_class_name = owner_class_name
            work = []
            for stmt in fd.body:
                work.append(stmt)
            idx = 0
            while idx < len(work):
                node = work[idx]
                idx += 1
                if node is None:
                    continue
                if isinstance(node, tuple):
                    for item in node:
                        work.append(item)
                    continue
                if isinstance(node, FuncDef) or isinstance(node, ClassDef):
                    continue
                if isinstance(node, Try):
                    # Handler bodies are the one statement list the generic
                    # dataclass descent below did not reach (ExceptHandler is
                    # not a Stmt); a delegation call inside ``except`` needs
                    # its frame slot like any other.
                    for handler in getattr(node, "handlers", ()) or ():
                        for handler_stmt in getattr(handler, "body", ()) or ():
                            work.append(handler_stmt)
                if (
                    isinstance(node, Call)
                    and isinstance(node.func, Attr)
                    and node.func.name in ("acquire", "wait", "join")
                ):
                    hidden = vthread_delegate_frame_name(node, "pcc.threading.receiver")
                    if hidden not in names:
                        names.append(hidden)
                primitive_key = vthread_proven_suspension_call_key(
                    self.ast_module,
                    fd,
                    node,
                    self._vthread_binding_cache,
                )
                if primitive_key in (
                    "pcc.virtual_thread.tcp_accept",
                    "pcc.virtual_thread.tcp_connect",
                    "pcc.virtual_thread.tcp_recv",
                    "pcc.virtual_thread.tcp_send_all",
                ):
                    # Sequential TCP evaluates its Python operands once, then
                    # keeps them and operation progress in this traced hidden
                    # frame slot while readiness parks the parent generator.
                    hidden = vthread_delegate_frame_name(node, primitive_key)
                    if hidden not in names:
                        names.append(hidden)
                elif self._vthread_dynamic_callback_call(node, fd):
                    hidden = vthread_delegate_frame_name(
                        node,
                        "pcc.virtual_thread.call",
                    )
                    if hidden not in names:
                        names.append(hidden)
                elif (
                    isinstance(node, Call)
                    and isinstance(node.func, Name)
                    and vthread_proven_direct_name_call(
                        fd,
                        node,
                        may_park_names,
                        self._vthread_binding_cache,
                    )
                    is not None
                ):
                    hidden = vthread_delegate_frame_name(node, node.func.ident)
                    if hidden not in names:
                        names.append(hidden)
                elif isinstance(node, Call) and isinstance(node.func, Attr):
                    method_key = vthread_proven_method_call_key(
                        node,
                        current_class_name,
                        may_park_method_keys,
                        self.ast_module,
                        fd,
                        proof_cache=self._vthread_binding_cache,
                    )
                    if method_key is not None:
                        hidden = vthread_delegate_frame_name(
                            node,
                            method_key,
                        )
                        if hidden not in names:
                            names.append(hidden)
                        # A local method cannot simultaneously be a compiled
                        # sibling module export, so skip that independent
                        # resolution path.
                        for slot_name in _dataclass_field_names(node):
                            if slot_name == "span":
                                continue
                            value = _dataclass_field_value(node, slot_name, None)
                            if isinstance(value, tuple):
                                for item in value:
                                    work.append(item)
                            else:
                                work.append(value)
                        continue
                    export_method_key = vthread_proven_export_method_call_key(
                        node,
                        getattr(self, "_native_module_exports", None),
                        self.ast_module,
                        self._vthread_binding_cache,
                    )
                    if export_method_key is not None:
                        hidden = vthread_delegate_frame_name(
                            node,
                            export_method_key,
                        )
                        if hidden not in names:
                            names.append(hidden)
                        for slot_name in _dataclass_field_names(node):
                            if slot_name == "span":
                                continue
                            value = _dataclass_field_value(node, slot_name, None)
                            if isinstance(value, tuple):
                                for item in value:
                                    work.append(item)
                            else:
                                work.append(value)
                        continue
                    export = self._native_module_expr_export_info(
                        node.func.obj,
                        node.func.name,
                    )
                    if export is not None:
                        export_module, export_info = export
                        if bool(export_info.get("may_park", False)):
                            effect_name = export_module + "." + node.func.name
                            hidden = vthread_delegate_frame_name(
                                node,
                                effect_name,
                            )
                            if hidden not in names:
                                names.append(hidden)
                for slot_name in _dataclass_field_names(node):
                    if slot_name == "span":
                        continue
                    value = _dataclass_field_value(node, slot_name, None)
                    if isinstance(value, tuple):
                        for item in value:
                            work.append(item)
                    else:
                        work.append(value)

        return names

    def _emit_generator_wrapper_function(
        self,
        fd: FuncDef,
        fn: ir.Function,
        symbol_name: Optional[str] = None,
        class_info=None,
        method_kind: Optional[str] = None,
    ) -> None:
        worker_timing = str(
            os.environ.get("PCC_PY_FRONTEND_WORKER_TIMING", "") or ""
        ).strip().lower() in ("1", "true", "yes", "on")
        owner_class_name = None
        if class_info is not None:
            owner_class_name = class_info.name
        frame_names = self._collect_generator_frame_names(
            fd,
            owner_class_name,
        )
        if worker_timing:
            sys.stderr.write(
                "pcc frontend generator frames function="
                + fd.name
                + " count="
                + str(len(frame_names))
                + "\n"
            )
            sys.stderr.write(
                "pcc frontend generator resume start function=" + fd.name + "\n"
            )
        resume_fn = self._emit_generator_resume_function(
            fd,
            frame_names,
            symbol_name,
            class_info,
            method_kind,
        )
        if worker_timing:
            sys.stderr.write(
                "pcc frontend generator resume done function=" + fd.name + "\n"
            )

        saved_builder = self.builder
        saved_fn = self.current_function
        saved_fd = self.current_func_def
        saved_env = self.env
        saved_loops = self.loop_stack
        saved_box_int_locals = self._box_int_locals
        saved_exact_int_flags = self._exact_int_env_flags
        saved_global_names = self._current_global_names

        self.current_function = fn
        self.current_func_def = fd
        self._current_global_names = self._collect_explicit_global_names(fd.body)
        entry = fn.append_basic_block(name="entry")
        self.builder = ir.IRBuilder(entry)
        self.env = {}
        self.loop_stack = []
        self._box_int_locals = self._should_box_python_ints()
        self._exact_int_env_flags = {}

        self._emit_thread_safepoint()

        bulk_frame_init = str(
            os.environ.get("PCC_DISABLE_BULK_GENERATOR_FRAME_INIT", "1") or "1"
        ).strip().lower() not in ("1", "true", "yes", "on")
        if bulk_frame_init:
            frame = self.builder.call(
                self.runtime["py_gen_frame_new"],
                [ir.Constant(_I64, len(frame_names))],
                name=self._fresh("gen.frame"),
            )
        else:
            frame = self.builder.call(
                self.runtime["py_list_new"],
                [ir.Constant(_I64, 0)],
                name=self._fresh("gen.frame"),
            )
        runtime_args = [a for a in fd.args if a.name != ""]
        arg_by_name = {
            ast_arg.name: (ir_arg, ast_arg)
            for ir_arg, ast_arg in zip(fn.args, runtime_args)
        }
        none_gv = declare_runtime_global(self.module, "py_None")
        none_obj = self.builder.load(none_gv, name=self._fresh("gen.none"))
        for frame_index, name in enumerate(frame_names):
            arg_entry = arg_by_name.get(name)
            if arg_entry is None:
                if bulk_frame_init:
                    continue
                obj = none_obj
            else:
                ir_arg, ast_arg = arg_entry
                obj = marshal.marshal_to_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    ir_arg,
                    ast_arg.annotation or DynType(name="dyn"),
                )
            if bulk_frame_init:
                self.builder.call(
                    self.runtime["py_list_set"],
                    [frame, ir.Constant(_I64, frame_index), obj],
                )
            else:
                self.builder.call(self.runtime["py_list_append"], [frame, obj])

        resume_ptr = self.builder.bitcast(
            resume_fn,
            _CSTR,
            name=self._fresh(f"{fd.name}.resume.ptr"),
        )
        gen = self.builder.call(
            self.runtime["py_gen_new"],
            [resume_ptr, frame],
            name=self._fresh(f"{fd.name}.gen"),
        )
        if (
            id(fd) in getattr(self, "_vthread_may_park_func_ids", set())
            or id(fd) in getattr(self, "_vthread_may_park_method_ids", set())
        ):
            self.builder.call(
                self.runtime["py_gen_set_may_park"],
                [gen],
            )
        self._gc_release(frame)
        self.builder.ret(gen)

        self._strict_stub_user_function_with_cpy_fallback(resume_fn, fd)

        self.builder = saved_builder
        self.current_function = saved_fn
        self.current_func_def = saved_fd
        self.env = saved_env
        self.loop_stack = saved_loops
        self._box_int_locals = saved_box_int_locals
        self._exact_int_env_flags = saved_exact_int_flags
        self._current_global_names = saved_global_names

    def _emit_generator_resume_function(
        self,
        fd: FuncDef,
        frame_names: list[str],
        symbol_name: Optional[str] = None,
        class_info=None,
        method_kind: Optional[str] = None,
    ) -> ir.Function:
        base_name = symbol_name or self._user_symbol(fd.name)
        name = f"{base_name}__gen_resume"
        existing = self.module.globals.get(name)
        if isinstance(existing, ir.Function):
            return existing
        fnty = ir.FunctionType(_CSTR, [_CSTR, _CSTR])
        fn = ir.Function(self.module, fnty, name=name)
        fn.linkage = "internal"
        fn.args[0].name = "gen"
        fn.args[1].name = "frame"

        saved_builder = self.builder
        saved_fn = self.current_function
        saved_fd = self.current_func_def
        saved_env = self.env
        saved_env_class_hint = self.env_class_hint
        saved_env_class_object_hint = self.env_class_object_hint
        saved_env_list_elem_class_hint = self.env_list_elem_class_hint
        saved_loops = self.loop_stack
        saved_box_int_locals = self._box_int_locals
        saved_exact_int_flags = self._exact_int_env_flags
        saved_global_names = self._current_global_names
        saved_current_param_names = self._current_param_names
        saved_owned_local_names = self._owned_local_names
        saved_owned_local_has_value = self._owned_local_has_value
        saved_owned_local_flag_slots = self._owned_local_flag_slots
        saved_owned_local_flag_allocas = getattr(
            self,
            "_owned_local_flag_allocas",
            {},
        )
        saved_gc_rooted_local_names = self._gc_rooted_local_names
        saved_gc_rooted_local_order = getattr(self, "_gc_rooted_local_order", [])
        saved_borrowed_gc_rooted_local_names = getattr(
            self,
            "_borrowed_gc_rooted_local_names",
            set(),
        )
        saved_pinned_gc_rooted_local_names = self._pinned_gc_rooted_local_names
        saved_class = self.current_class
        saved_method_kind = self.current_method_kind

        entry = fn.append_basic_block(name="entry")
        self.builder = ir.IRBuilder(entry)
        self.current_function = fn
        self.current_func_def = fd
        self.env = {}
        self.env_class_hint = {}
        self.env_class_object_hint = {}
        self.env_list_elem_class_hint = {}
        self.loop_stack = []
        self._box_int_locals = self._should_box_python_ints()
        self._exact_int_env_flags = {}
        self._current_global_names = self._collect_explicit_global_names(fd.body)
        self._current_param_names = set()
        self._owned_local_names = set()
        self._owned_local_has_value = set()
        self._owned_local_flag_slots = {}
        self._owned_local_flag_allocas = {}
        self._gc_rooted_local_names = set()
        self._gc_rooted_local_order = []
        self._borrowed_gc_rooted_local_names = set()
        self._pinned_gc_rooted_local_names = set()
        self.current_class = class_info
        self.current_method_kind = method_kind

        self._emit_thread_safepoint()

        first_entry_init = str(
            os.environ.get("PCC_GENERATOR_FIRST_ENTRY_INIT", "0") or "0"
        ).strip().lower() in ("1", "true", "yes", "on")
        argument_names = {arg.name for arg in fd.args if arg.name != ""}
        if len(frame_names) == len(argument_names):
            # No placeholders means there is no first-entry work to elide.
            first_entry_init = False
        entry_state = None
        if first_entry_init:
            entry_state = self.builder.call(
                self.runtime["py_gen_state"],
                [fn.args[0]],
                name=self._fresh("gen.entry.state"),
            )

        frame_slots: dict[str, tuple[int, ir.Value]] = {}
        for idx, local_name in enumerate(frame_names):
            slot = self._alloca_in_entry(_CSTR, name=f"{local_name}.addr")
            if first_entry_init and local_name not in argument_names:
                # The private factory filled these slots with immortal None.
                # Keep the same owned locals and roots without a retaining
                # list read before the function has executed its first line.
                item = self._emit_none_literal()
            else:
                item = self.builder.call(
                    self.runtime["py_list_get"],
                    [fn.args[1], ir.Constant(_I64, idx)],
                    name=self._fresh(f"gen.frame.{local_name}"),
                )
            self.builder.store(item, slot)
            self.env[local_name] = (slot, _CSTR, DynType(name="dyn"))
            frame_slots[local_name] = (idx, slot)
            self._owned_local_names.add(local_name)
            self._owned_local_has_value.add(local_name)
            flag = self._ensure_owned_local_flag(local_name, slot)
            self.builder.store(ir.Constant(_I1, 1), flag)
            self._ensure_owned_local_gc_root(local_name, slot, _CSTR)

        dispatch_bb = fn.append_basic_block(name="gen.dispatch")
        start_bb = fn.append_basic_block(name="gen.start")
        if first_entry_init:
            restore_bb = fn.append_basic_block(name="gen.restore.locals")
            is_first = self.builder.icmp_signed(
                "==", entry_state, ir.Constant(_I64, 0),
                name=self._fresh("gen.first.entry"),
            )
            self.builder.cbranch(is_first, dispatch_bb, restore_bb)
            self.builder.position_at_end(restore_bb)
            for local_name, (idx, slot) in frame_slots.items():
                if local_name in argument_names:
                    continue
                item = self.builder.call(
                    self.runtime["py_list_get"],
                    [fn.args[1], ir.Constant(_I64, idx)],
                    name=self._fresh(f"gen.frame.{local_name}"),
                )
                # This root still contains only its initial immortal None.
                self.builder.store(item, slot)
        self.builder.branch(dispatch_bb)
        self.builder.position_at_end(dispatch_bb)
        state = entry_state
        if state is None:
            state = self.builder.call(
                self.runtime["py_gen_state"],
                [fn.args[0]],
                name=self._fresh("gen.state"),
            )
        switch_inst = self.builder.switch(state, start_bb)
        self.builder.position_at_end(start_bb)
        self._generator_ctx_stack.append(
            {
                "gen": fn.args[0],
                "frame": fn.args[1],
                "frame_slots": frame_slots,
                "dispatch_bb": dispatch_bb,
                "switch": switch_inst,
                "next_state": 1,
                "active_exception_unwind_roots": [],
            }
        )

        worker_timing = str(
            os.environ.get("PCC_PY_FRONTEND_WORKER_TIMING", "") or ""
        ).strip().lower() in ("1", "true", "yes", "on")
        if worker_timing:
            sys.stderr.write(
                "pcc frontend generator body start function=" + fd.name + "\n"
            )
        self._emit_stmts(fd.body)
        if worker_timing:
            sys.stderr.write(
                "pcc frontend generator body done function=" + fd.name + "\n"
            )
        if not self._builder_block_is_terminated():
            self._emit_generator_finish()

        self.builder = saved_builder
        self.current_function = saved_fn
        self.current_func_def = saved_fd
        self.env = saved_env
        self.env_class_hint = saved_env_class_hint
        self.env_class_object_hint = saved_env_class_object_hint
        self.env_list_elem_class_hint = saved_env_list_elem_class_hint
        self.loop_stack = saved_loops
        self._box_int_locals = saved_box_int_locals
        self._exact_int_env_flags = saved_exact_int_flags
        self._current_global_names = saved_global_names
        self._current_param_names = saved_current_param_names
        self._owned_local_names = saved_owned_local_names
        self._owned_local_has_value = saved_owned_local_has_value
        self._owned_local_flag_slots = saved_owned_local_flag_slots
        self._owned_local_flag_allocas = saved_owned_local_flag_allocas
        self._gc_rooted_local_names = saved_gc_rooted_local_names
        self._gc_rooted_local_order = saved_gc_rooted_local_order
        self._borrowed_gc_rooted_local_names = saved_borrowed_gc_rooted_local_names
        self._pinned_gc_rooted_local_names = saved_pinned_gc_rooted_local_names
        self._generator_ctx_stack.pop()
        self.current_class = saved_class
        self.current_method_kind = saved_method_kind
        return fn

    def _emit_generator_add_case(
        self,
        state_id: int,
        target: ir.Block,
    ) -> None:
        ctx = self._generator_ctx_stack[-1]
        cur = self.builder._block
        self.builder.position_at_end(ctx["dispatch_bb"])
        ctx["switch"].add_case(ir.Constant(_I64, state_id), target)
        self.builder.position_at_end(cur)

    def _emit_generator_save_frame(self) -> None:
        ctx = self._generator_ctx_stack[-1]
        frame = ctx["frame"]
        # cpy for-loop targets hold raw libpython pointers while their
        # loop is being emitted; those must never be stored into the
        # frame py_list (its barriers/dealloc dereference pcc headers).
        # The cpy for lowering proves the name is not read across a
        # suspension before registering it here.
        skip_names = ctx.get("cpy_skip_save_names", ())
        transfer_owners = str(
            os.environ.get("PCC_TRANSFER_GENERATOR_FRAME_OWNERS", "0") or "0"
        ).strip().lower() in ("1", "true", "yes", "on")
        for name, (idx, slot) in ctx["frame_slots"].items():
            if name in skip_names:
                continue
            if transfer_owners:
                owned_flag = self._ensure_owned_local_flag(name, slot)
                self.builder.call(
                    self.runtime["py_list_set_from_owned_root"],
                    [frame, ir.Constant(_I64, idx),
                     self._as_gc_ptr(slot, name=self._fresh("gen.save.source")),
                     self._as_gc_ptr(owned_flag, name=self._fresh("gen.save.owned"))],
                )
                continue
            value = self.builder.load(slot, name=self._fresh("gen.save"))
            self.builder.call(
                self.runtime["py_list_set"],
                [frame, ir.Constant(_I64, idx), value],
            )

    def _emit_generator_stop_iteration(
        self,
        value: Optional[ir.Value] = None,
    ) -> None:
        msg = ir.Constant(_CSTR, None)
        tag = ir.Constant(_I64, _STOP_ITERATION_TAG)
        if value is None:
            exc = self.builder.call(
                self.runtime["py_exc_new"],
                [tag, msg],
                name=self._fresh("gen.stop.exc"),
            )
        else:
            exc = self.builder.call(
                self.runtime["py_exc_new_with_value"],
                [tag, value],
                name=self._fresh("gen.stop.exc"),
            )
        self.builder.call(self.runtime["py_raise"], [exc])
        self.builder.ret(ir.Constant(_CSTR, None))

    def _emit_generator_finish(
        self,
        value: Optional[ir.Value] = None,
    ) -> None:
        ctx = self._generator_ctx_stack[-1]
        if value is None:
            value = self._emit_none_literal()
        for root_ptr in ctx.get("active_exception_unwind_roots", ()):
            self.builder.call(
                self.runtime["pcc_gc_store_root"],
                [root_ptr, self._emit_none_literal()],
            )
        # Construct StopIteration while every live local is still registered
        # as an updateable root.  In particular, a return value preserved
        # across a parking finally block must not survive only as an SSA value
        # while py_gen_finish allocates the exception.
        result = self.builder.call(
            self.runtime["py_gen_finish"],
            [ctx["gen"], value],
            name=self._fresh("gen.finish"),
        )
        self._emit_owned_local_cleanup()
        self.builder.ret(result)

    def _emit_generator_return(self, stmt: Return) -> None:
        if stmt.value is None:
            self._emit_pending_finally_blocks()
            if not self._builder_block_is_terminated():
                self._emit_generator_finish()
            return

        ctx = self._generator_ctx_stack[-1]
        hidden = self._generator_return_value_name(stmt)
        frame_entry = ctx["frame_slots"].get(hidden)
        if frame_entry is None:
            raise L1CodegenError("generator return missing managed value slot")
        value_slot = frame_entry[1]
        value = self._emit_as_object(stmt.value)
        # Normalize the expression to one owned reference, then transfer that
        # reference into the heap-frame slot.  The slot is saved by any park in
        # a pending finally block and is rewritten by relocating collectors.
        if (
            isinstance(stmt.value, Name)
            and stmt.value.ident in self._owned_local_names
            and not self._value_is_owned_object(value)
        ):
            # Unlike an ordinary return, generator completion cleans every
            # local (and finally may overwrite this name). Capture a separate
            # owner instead of transferring the local's still-tracked owner.
            value = self._gc_retain(value, name=self._fresh("gen.return.retain"))
        else:
            value = self._retain_borrowed_return_value(value, stmt)
        value_root_ptr = self._as_gc_ptr(
            value_slot,
            name=self._fresh("gen.return.root"),
        )
        self.builder.call(
            self.runtime["pcc_gc_store_root"],
            [value_root_ptr, value],
        )
        self._gc_release(value)

        self._emit_pending_finally_blocks()
        if self._builder_block_is_terminated():
            return
        # A finally block may park and resume through a separate entry edge.
        # The pre-finally cast no longer dominates that continuation.
        value_root_ptr = self._as_gc_ptr(
            value_slot,
            name=self._fresh("gen.return.resume.root"),
        )
        rooted_value = self.builder.call(
            self.runtime["pcc_gc_load_ptr"],
            [ir.Constant(_CSTR, None), value_root_ptr],
            name=self._fresh("gen.return.value"),
        )
        self._emit_generator_finish(rooted_value)

    def _emit_generator_yield_value(
        self,
        value: ir.Value,
        *,
        resume_err_target: Optional[ir.Block] = None,
    ) -> None:
        ctx = self._generator_ctx_stack[-1]
        worker_timing = str(
            os.environ.get("PCC_PY_FRONTEND_WORKER_TIMING", "") or ""
        ).strip().lower() in ("1", "true", "yes", "on")
        state_id = ctx["next_state"]
        ctx["next_state"] = state_id + 1
        cont_bb = self.current_function.append_basic_block(
            name=self._fresh(f"gen.resume.{state_id}"),
        )
        if worker_timing:
            sys.stderr.write("pcc frontend generator yield save-frame start\n")
        self._emit_generator_save_frame()
        if worker_timing:
            sys.stderr.write("pcc frontend generator yield save-frame done\n")
            sys.stderr.write("pcc frontend generator yield cleanup start\n")
        self._emit_owned_local_cleanup()
        if worker_timing:
            sys.stderr.write("pcc frontend generator yield cleanup done\n")
        self.builder.call(
            self.runtime["py_gen_set_state"],
            [ctx["gen"], ir.Constant(_I64, state_id)],
        )
        if worker_timing:
            sys.stderr.write("pcc frontend generator yield add-case start\n")
        self._emit_generator_add_case(state_id, cont_bb)
        if worker_timing:
            sys.stderr.write("pcc frontend generator yield add-case done\n")
        self.builder.ret(value)
        self.builder.position_at_end(cont_bb)
        pending = self.builder.call(
            self.runtime["py_err_occurred"],
            [],
            name=self._fresh("gen.resume.err"),
        )
        has_pending = self.builder.icmp_signed(
            "!=",
            pending,
            ir.Constant(_I64, 0),
            name=self._fresh("gen.resume.err.i1"),
        )
        ok_bb = self.current_function.append_basic_block(
            name=self._fresh("gen.resume.ok"),
        )
        err_target = resume_err_target
        if err_target is None:
            err_target = getattr(self, "_try_err_block", None)
            if err_target is None:
                err_target = self._ensure_fn_err_exit()
        self.builder.cbranch(has_pending, err_target, ok_bb)
        self.builder.position_at_end(ok_bb)

    def _emit_generator_take_send(self) -> ir.Value:
        ctx = self._generator_ctx_stack[-1]
        return self.builder.call(
            self.runtime["py_gen_take_send"],
            [ctx["gen"]],
            name=self._fresh("gen.send.value"),
        )

    def _emit_generator_discard_send(self) -> None:
        value = self._emit_generator_take_send()
        self._gc_release(value)

    def _generator_yield_value_needs_retain(
        self,
        value: ir.Value,
        expr: Expr,
    ) -> bool:
        if not isinstance(value.type, ir.PointerType):
            return False
        if value in getattr(self, "_cpy_values", ()):
            return False
        if self._expr_returns_unsafe_raw_pointer(expr):
            return False
        if isinstance(expr, Name):
            if expr.ident in getattr(self, "_owned_local_names", set()):
                return True
            if expr.ident in getattr(self, "_current_param_names", set()):
                return True
            if expr.ident in getattr(self, "_module_globals", {}):
                return True
            if expr.ident in self.env:
                return True
        if self._expr_returns_owned_object(expr):
            return False
        expr_ty = getattr(expr, "ty", None)
        return expr_ty is not None and self._is_object(expr_ty)

    def _emit_generator_yield_expr(self, expr: Call) -> ir.Value:
        if len(expr.args) > 1:
            raise NotImplementedError("yield accepts at most one value")
        if expr.args:
            value = self._emit_as_object(expr.args[0])
            if self._generator_yield_value_needs_retain(value, expr.args[0]):
                value = self._gc_retain(
                    value,
                    name=self._fresh("gen.yield.retain"),
                )
        else:
            value = self._emit_none_literal()
        self._emit_generator_yield_value(value)
        return self._emit_generator_take_send()

    def _emit_generator_yield(self, expr: Call) -> None:
        sent = self._emit_generator_yield_expr(expr)
        self._gc_release(sent)

    def _emit_generator_yield_from(self, expr: Call) -> None:
        if len(expr.args) != 1:
            raise NotImplementedError("yield from expects one iterable")
        ctx = self._generator_ctx_stack[-1]
        hidden = self._generator_yield_from_iter_name(expr)
        frame_entry = ctx["frame_slots"].get(hidden)
        if frame_entry is None:
            raise L1CodegenError("yield from missing generator frame slot")
        iter_slot = frame_entry[1]
        iterable = self._emit_as_object(expr.args[0])
        iterator = self.builder.call(
            self.runtime["py_obj_iter"],
            [iterable],
            name=self._fresh("gen.yf.iter"),
        )
        self._emit_post_call_err_check(expr.span)
        self.builder.store(iterator, iter_slot)

        fn = self.current_function
        header_bb = fn.append_basic_block(name=self._fresh("gen.yf.next"))
        body_bb = fn.append_basic_block(name=self._fresh("gen.yf.body"))
        maybe_end_bb = fn.append_basic_block(name=self._fresh("gen.yf.maybe_end"))
        clear_bb = fn.append_basic_block(name=self._fresh("gen.yf.clear"))
        propagate_bb = fn.append_basic_block(name=self._fresh("gen.yf.propagate"))
        end_bb = fn.append_basic_block(name=self._fresh("gen.yf.end"))

        self.builder.branch(header_bb)
        self.builder.position_at_end(header_bb)
        iterator_cur = self.builder.load(
            iter_slot,
            name=self._fresh("gen.yf.iter.cur"),
        )
        item = self.builder.call(
            self.runtime["py_obj_next"],
            [iterator_cur],
            name=self._fresh("gen.yf.item"),
        )
        is_null = self.builder.icmp_unsigned(
            "==",
            item,
            ir.Constant(_CSTR, None),
            name=self._fresh("gen.yf.null"),
        )
        self.builder.cbranch(is_null, maybe_end_bb, body_bb)

        self.builder.position_at_end(body_bb)
        self._emit_generator_yield_value(item)
        self._emit_generator_discard_send()
        if not self._builder_block_is_terminated():
            self.builder.branch(header_bb)

        self.builder.position_at_end(maybe_end_bb)
        current_exc = self.builder.call(
            self.runtime["py_current_exception"],
            [],
            name=self._fresh("gen.yf.cur_exc"),
        )
        stop_cls = self.builder.call(
            self.runtime["py_exc_builtin_class"],
            [ir.Constant(_I64, _STOP_ITERATION_TAG)],
            name=self._fresh("gen.yf.stop_cls"),
        )
        match_i64 = self.builder.call(
            self.runtime["py_exc_matches"],
            [current_exc, stop_cls],
            name=self._fresh("gen.yf.stop_match"),
        )
        is_stop = self.builder.icmp_signed(
            "!=",
            match_i64,
            ir.Constant(_I64, 0),
            name=self._fresh("gen.yf.stop_i1"),
        )
        self.builder.cbranch(is_stop, clear_bb, propagate_bb)

        self.builder.position_at_end(clear_bb)
        self.builder.call(self.runtime["py_clear_exception"], [])
        self.builder.branch(end_bb)

        self.builder.position_at_end(propagate_bb)
        err_target = getattr(self, "_try_err_block", None)
        if err_target is None:
            err_target = self._ensure_fn_err_exit()
        self.builder.branch(err_target)

        self.builder.position_at_end(end_bb)
