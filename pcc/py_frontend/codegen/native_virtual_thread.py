"""Native ``pcc.virtual_thread`` lowering helpers."""

from __future__ import annotations

import os
import sys

from typing import Optional

from pcc.llvm_capi.compat import ir

from ..py_ast import Attr, Call, DynType, Expr, FuncDef, Name, NoneType
from . import marshal
from .errors import L1CodegenError
from .freestanding_abi_constants import PY_TYPE_GEN
from .generator_lowering import (
    emit_generator_may_park_child,
    generator_may_park_child_slot,
)
from .runtime_abi import declare_runtime_global

_I8 = ir.IntType(8)
_I32 = ir.IntType(32)
_I64 = ir.IntType(64)
_CSTR = _I8.as_pointer()


_VTHREAD_EXPORTS = (
    "spawn",
    "continuation",
    "completed",
    "continuation_factory",
    "call",
    "join",
    "cancel",
    "mpsc",
    "oneshot",
    "sender_clone",
    "send",
    "recv",
    "close_sender",
    "close_receiver",
    "select2",
    "run",
    "run_until_idle",
    "carrier_pool_start",
    "carrier_pool_stop",
    "io_backend",
    "current",
    "yield_now",
    "sleep_current",
    "block_current_on_fd",
    "readable",
    "writable",
    "tcp_listen",
    "tcp_accept",
    "tcp_connect",
    "tcp_recv",
    "tcp_send_all",
    "tcp_close",
    "result",
    "exception",
    "outcome",
    "state",
    "sleep",
    "block_on_fd",
)


def _is_vthread_export(name: str) -> bool:
    return (
        name == "spawn"
        or name == "continuation"
        or name == "completed"
        or name == "continuation_factory"
        or name == "call"
        or name == "join"
        or name == "cancel"
        or name == "mpsc"
        or name == "oneshot"
        or name == "sender_clone"
        or name == "send"
        or name == "recv"
        or name == "close_sender"
        or name == "close_receiver"
        or name == "select2"
        or name == "run"
        or name == "run_until_idle"
        or name == "carrier_pool_start"
        or name == "carrier_pool_stop"
        or name == "io_backend"
        or name == "current"
        or name == "yield_now"
        or name == "sleep_current"
        or name == "block_current_on_fd"
        or name == "readable"
        or name == "writable"
        or name == "tcp_listen"
        or name == "tcp_accept"
        or name == "tcp_connect"
        or name == "tcp_recv"
        or name == "tcp_send_all"
        or name == "tcp_close"
        or name == "result"
        or name == "exception"
        or name == "outcome"
        or name == "state"
        or name == "sleep"
        or name == "block_on_fd"
    )


class NativeVirtualThreadLoweringMixin:
    def _emit_native_virtual_thread_call(self, expr: Call) -> Optional[ir.Value]:
        attr = expr.func
        if not isinstance(attr, Attr):
            return None
        if not isinstance(attr.obj, Name):
            return None
        if self._native_builtin_module_for_name(attr.obj.ident) != "pcc.virtual_thread":
            return None
        if not _is_vthread_export(attr.name):
            return None
        return self._emit_native_virtual_thread_value_call(
            "pcc.virtual_thread." + attr.name,
            expr.args,
            expr.kwargs,
            expr,
        )

    def _emit_virtual_thread_callback_call(
        self,
        expr: Call,
        args: tuple[Expr, ...],
        kwargs: tuple[tuple[str, Expr], ...],
    ) -> ir.Value:
        if kwargs or len(args) < 1:
            raise L1CodegenError(
                "pcc.virtual_thread.call requires a callable and positional arguments"
            )
        if len(self._generator_ctx_stack) == 0:
            raise L1CodegenError(
                "pcc.virtual_thread.call requires a resumable parent"
            )

        callable_obj = self._emit_as_object(args[0])
        callable_root = self._enter_virtual_thread_operand_root(
            callable_obj,
            args[0],
            "vthread.call.callable",
        )
        callback_args = args[1:]
        args_root = self._emit_virtual_thread_dynamic_args_with_roots(
            callback_args,
            (callable_root,),
        )
        none_gv = declare_runtime_global(self.module, "py_None")
        none_obj = self.builder.load(
            none_gv,
            name=self._fresh("vthread.call.none"),
        )
        result = self.builder.call(
            self.runtime["py_obj_call"],
            [
                self._load_virtual_thread_operand_root(callable_root),
                self._load_virtual_thread_operand_root(args_root),
                none_obj,
            ],
            name=self._fresh("vthread.call.result"),
        )
        child_slot, child_root_ptr = generator_may_park_child_slot(
            self, expr, "pcc.virtual_thread.call"
        )
        # Stage the open-world callback result in a traced frame slot before
        # any cleanup call can safepoint.  Unlike pcc_gc_pin this preserves an
        # object's pre-existing pin state and works for tagged values too.
        result_is_null = self.builder.icmp_unsigned(
            "==",
            result,
            ir.Constant(_CSTR, None),
            name=self._fresh("vthread.call.result.is_null"),
        )
        parent_fn = self.current_function
        result_error_bb = parent_fn.append_basic_block(
            name=self._fresh("vthread.call.result.error")
        )
        result_ready_bb = parent_fn.append_basic_block(
            name=self._fresh("vthread.call.result.ready")
        )
        self.builder.cbranch(result_is_null, result_error_bb, result_ready_bb)

        self.builder.position_at_end(result_error_bb)
        self._release_rooted_pcc_lifetimes((callable_root, args_root))
        # py_obj_call guarantees NULL carries a pending exception. Reuse the
        # normal post-call router after cleanup so source-span traceback frames
        # and surrounding try targets remain identical to other native calls.
        self._emit_post_call_err_check(expr.span)
        self.builder.unreachable()

        self.builder.position_at_end(result_ready_bb)
        self.builder.call(
            self.runtime["pcc_gc_store_root"],
            [child_root_ptr, result],
        )
        self._gc_release(result)
        self._release_rooted_pcc_lifetimes((callable_root, args_root))
        rooted_result = self.builder.call(
            self.runtime["pcc_gc_load_ptr"],
            [ir.Constant(_CSTR, None), child_root_ptr],
            name=self._fresh("vthread.call.result.rooted"),
        )

        tag = self.builder.call(
            self.runtime["py_obj_type_tag"],
            [rooted_result],
            name=self._fresh("vthread.call.result.tag"),
        )
        is_generator = self.builder.icmp_signed(
            "==",
            tag,
            ir.Constant(_I64, PY_TYPE_GEN),
            name=self._fresh("vthread.call.result.is_generator"),
        )
        inspect_generator_bb = parent_fn.append_basic_block(
            name=self._fresh("vthread.call.inspect_generator")
        )
        continuation_bb = parent_fn.append_basic_block(
            name=self._fresh("vthread.call.continuation")
        )
        direct_bb = parent_fn.append_basic_block(
            name=self._fresh("vthread.call.direct")
        )
        done_bb = parent_fn.append_basic_block(
            name=self._fresh("vthread.call.done")
        )
        self.builder.cbranch(is_generator, inspect_generator_bb, direct_bb)

        self.builder.position_at_end(inspect_generator_bb)
        inspect_rooted_result = self.builder.call(
            self.runtime["pcc_gc_load_ptr"],
            [ir.Constant(_CSTR, None), child_root_ptr],
            name=self._fresh("vthread.call.inspect.rooted"),
        )
        effect_marker = self.builder.call(
            self.runtime["py_gen_is_may_park"],
            [inspect_rooted_result],
            name=self._fresh("vthread.call.result.effect_marker"),
        )
        is_continuation = self.builder.icmp_signed(
            "!=",
            effect_marker,
            ir.Constant(_I64, 0),
            name=self._fresh("vthread.call.result.is_continuation"),
        )
        self.builder.cbranch(is_continuation, continuation_bb, direct_bb)

        self.builder.position_at_end(direct_bb)
        # The marker check is a runtime call and may safepoint.  Reload from
        # the traced slot in each successor rather than carrying its pre-call
        # SSA value across a relocating collection.
        direct_rooted_result = self.builder.call(
            self.runtime["pcc_gc_load_ptr"],
            [ir.Constant(_CSTR, None), child_root_ptr],
            name=self._fresh("vthread.call.direct.rooted"),
        )
        direct_value = self._gc_retain(
            direct_rooted_result,
            name=self._fresh("vthread.call.direct.retain"),
        )
        self.builder.call(
            self.runtime["pcc_gc_store_root"],
            [child_root_ptr, none_obj],
        )
        direct_block = self.builder._block
        self.builder.branch(done_bb)

        self.builder.position_at_end(continuation_bb)
        continuation_rooted_result = self.builder.call(
            self.runtime["pcc_gc_load_ptr"],
            [ir.Constant(_CSTR, None), child_root_ptr],
            name=self._fresh("vthread.call.continuation.rooted"),
        )
        delegated = emit_generator_may_park_child(
            self,
            expr,
            "pcc.virtual_thread.call",
            continuation_rooted_result,
            child_already_rooted=True,
        )
        delegated_block = self.builder._block
        if not self._builder_block_is_terminated():
            self.builder.branch(done_bb)

        self.builder.position_at_end(done_bb)
        merged = self.builder.phi(
            _CSTR,
            name=self._fresh("vthread.call.value"),
        )
        merged.add_incoming(direct_value, direct_block)
        merged.add_incoming(delegated, delegated_block)
        return merged

    def _virtual_thread_frame_map(self, n_slots: int) -> ir.GlobalVariable:
        name = f".pcc.vthread.frame.map.{n_slots}"
        existing = self.module.globals.get(name)
        if existing is not None:
            return existing
        gv = ir.GlobalVariable(self.module, _I32, name=name)
        gv.linkage = "internal"
        gv.global_constant = True
        gv.initializer = ir.Constant(_I32, n_slots)
        return gv

    def _emit_virtual_thread_rc_check(self, rc: ir.Value, hint: str) -> None:
        failed = self.builder.icmp_signed(
            "<",
            rc,
            ir.Constant(_I64, 0),
            name=self._fresh("vthread.rc.failed"),
        )
        fail_bb = self.current_function.append_basic_block(
            name=self._fresh("vthread.rc.fail"),
        )
        ok_bb = self.current_function.append_basic_block(
            name=self._fresh("vthread.rc.ok"),
        )
        self.builder.cbranch(failed, fail_bb, ok_bb)
        self.builder.position_at_end(fail_bb)
        exc = self.builder.call(
            self.runtime["py_exc_new"],
            [
                ir.Constant(_I64, 7),
                self._ptr_to_cstr(
                    self._cstr_global(
                        hint + " failed",
                        self._fresh(".vthread.rc.err"),
                    )
                ),
            ],
            name=self._fresh("vthread.rc.exc"),
        )
        self.builder.call(self.runtime["py_raise"], [exc])
        self._gc_release(exc)
        err_target = getattr(self, "_try_err_block", None)
        if err_target is None:
            err_target = self._ensure_fn_err_exit()
        self.builder.branch(err_target)
        self.builder.position_at_end(ok_bb)

    def _release_virtual_thread_argument(
        self,
        obj: ir.Value,
        source_expr: Expr,
    ) -> None:
        """Drop a temporary object argument without touching borrowed Names."""

        producer = None
        if isinstance(source_expr, Call):
            producer = self._native_builtin_value_kind_for_expr(source_expr.func)
        if producer in (
            "pcc.virtual_thread.spawn",
            "pcc.virtual_thread.continuation",
            "pcc.virtual_thread.completed",
            "pcc.virtual_thread.call",
            "pcc.virtual_thread.join",
            "pcc.virtual_thread.current",
            "pcc.virtual_thread.result",
            "pcc.virtual_thread.exception",
            "pcc.virtual_thread.mpsc",
            "pcc.virtual_thread.oneshot",
            "pcc.virtual_thread.sender_clone",
            "pcc.virtual_thread.recv",
            "pcc.virtual_thread.select2",
            "pcc.virtual_thread.tcp_recv",
        ):
            # These native operations return a new/retained object even though
            # their public Any annotation is represented as DynType.  Generic
            # ownership inference deliberately cannot call all Dyn results
            # owned, so keep the exact intrinsic contract here.  Channel
            # constructors/clone and receive/select result tuples follow the
            # same owned-result contract.
            self._gc_release(obj)
            return
        self._gc_release_if_owned(obj, source_expr)

    def _enter_virtual_thread_operand_root(
        self,
        value: ir.Value,
        source_expr: Optional[Expr],
        label: str,
    ) -> tuple[ir.Value, bool]:
        """Trace an early pointer operand until all later operands exist."""
        root_slot = self._enter_container_temp_root(value, label)
        release_owned = source_expr is None
        if source_expr is not None:
            release_owned = self._pcc_pointer_source_is_owned(source_expr)
        return (root_slot, release_owned)

    def _load_virtual_thread_operand_root(
        self,
        root: tuple[ir.Value, bool],
    ) -> ir.Value:
        """Reload the post-relocation pointer represented by ``root``."""
        root_slot, _release_owned = root
        return self.builder.call(
            self.runtime["pcc_gc_load_ptr"],
            [
                ir.Constant(_CSTR, None),
                self._as_gc_ptr(
                    root_slot,
                    name=self._fresh("vthread.operand.root.ptr"),
                ),
            ],
            name=self._fresh("vthread.operand.rooted"),
        )

    def _emit_virtual_thread_dynamic_args_with_roots(
        self,
        args: tuple[Expr, ...],
        roots: tuple[tuple[ir.Value, bool], ...],
    ) -> tuple[ir.Value, bool]:
        """Build and return a still-rooted owned callback-argument tuple.

        The generic dynamic-call tuple builder keeps its partially populated
        tuple in one raw SSA value.  A later argument may allocate or park, so
        that shape is not valid for the relocating collector.  ``vt.call``
        keeps both the callable and this tuple in traced slots through the
        final ``py_obj_call``; populated tuple slots then keep earlier argument
        objects visible while later source operands are evaluated.
        """
        previous_cpy_cleanup = getattr(
            self,
            "_cpy_operand_cleanup_block",
            None,
        )
        previous_pcc_target = self._current_try_err_block()
        pcc_target = previous_pcc_target
        if pcc_target is None:
            pcc_target = self._ensure_fn_err_exit()
        pcc_cleanup = self._make_cpy_operand_cleanup_block(
            (),
            (),
            pcc_target,
            "vthread.args.pcc.cleanup",
            (),
            roots,
        )
        cpy_target = previous_cpy_cleanup
        if cpy_target is None:
            cpy_target = self._ensure_fn_err_exit()
        if cpy_target is pcc_target:
            cpy_cleanup = pcc_cleanup
        else:
            cpy_cleanup = self._make_cpy_operand_cleanup_block(
                (),
                (),
                cpy_target,
                "vthread.args.cpy.cleanup",
                (),
                roots,
            )
        self._try_err_block = pcc_cleanup
        self._cpy_operand_cleanup_block = cpy_cleanup
        try:
            return self._emit_virtual_thread_rooted_args_tuple(args)
        finally:
            self._try_err_block = previous_pcc_target
            self._cpy_operand_cleanup_block = previous_cpy_cleanup

    def _emit_virtual_thread_container_call_check(
        self,
        span,
        container_roots: tuple[tuple[ir.Value, bool], ...],
    ) -> None:
        """Route a container-construction error through relocation-safe roots."""
        previous_target = self._current_try_err_block()
        target = previous_target
        if target is None:
            target = self._ensure_fn_err_exit()
        cleanup = self._make_cpy_operand_cleanup_block(
            (),
            (),
            target,
            "vthread.args.container.cleanup",
            (),
            container_roots,
        )
        self._try_err_block = cleanup
        try:
            self._emit_post_call_err_check(span)
        finally:
            self._try_err_block = previous_target

    def _emit_virtual_thread_rooted_args_tuple(
        self,
        args: tuple[Expr, ...],
    ) -> tuple[ir.Value, bool]:
        """Materialize one owned args tuple without carrying movable SSA refs."""
        has_splat = self._has_starred_unpack(args)
        runtime_new = "py_list_new" if has_splat else "py_tuple_new"
        initial_size = 0 if has_splat else len(args)
        container = self.builder.call(
            self.runtime[runtime_new],
            [ir.Constant(_I64, initial_size)],
            name=self._fresh("vthread.call.args.container"),
        )
        container_root = self._enter_virtual_thread_operand_root(
            container,
            None,
            "vthread.call.args.container",
        )
        self._emit_virtual_thread_container_call_check(
            None,
            (container_root,),
        )

        for index, arg in enumerate(args):
            is_splat = has_splat and self._is_starred_unpack_expr(arg)
            source_expr = arg.args[0] if is_splat else arg
            value_obj = self._emit_expr_with_cpy_operand_cleanup(
                source_expr,
                (),
                as_pcc_object=True,
                rooted_pcc_lifetimes=(container_root,),
            )
            value_owned = self._container_store_temp_needs_release(
                source_expr,
                source_expr.ty,
                False,
            )
            value_root_slot = self._enter_container_temp_root(
                value_obj,
                "vthread.call.args.value",
            )
            value_root = (value_root_slot, value_owned)

            rooted_container = self._load_virtual_thread_operand_root(
                container_root
            )
            rooted_value = self._load_virtual_thread_operand_root(value_root)
            if has_splat:
                operation = "py_list_extend" if is_splat else "py_list_append"
                self.builder.call(
                    self.runtime[operation],
                    [rooted_container, rooted_value],
                )
            else:
                self.builder.call(
                    self.runtime["py_tuple_set_item"],
                    [rooted_container, ir.Constant(_I64, index), rooted_value],
                )
            self._emit_virtual_thread_container_call_check(
                getattr(source_expr, "span", None),
                (container_root, value_root),
            )
            self._release_rooted_pcc_lifetimes((value_root,))

        if not has_splat:
            return container_root

        tuple_value = self.builder.call(
            self.runtime["py_tuple_from_list"],
            [self._load_virtual_thread_operand_root(container_root)],
            name=self._fresh("vthread.call.args.splat.tuple"),
        )
        tuple_root = self._enter_virtual_thread_operand_root(
            tuple_value,
            None,
            "vthread.call.args.splat.tuple",
        )
        self._emit_virtual_thread_container_call_check(
            None,
            (container_root, tuple_root),
        )
        self._release_rooted_pcc_lifetimes((container_root,))
        return tuple_root

    def _emit_virtual_thread_current_fd_wait(
        self,
        fd: Expr,
        events: Expr | int,
        timeout_ms: Expr | int,
    ) -> ir.Value:
        current = self.builder.call(
            self.runtime["py_virtual_thread_current"],
            [],
            name=self._fresh("vthread.fd.current"),
        )
        current_root = self._enter_virtual_thread_operand_root(
            current,
            None,
            "vthread.fd.current",
        )
        fd_value = self._emit_expr_with_cpy_operand_cleanup(
            fd,
            (),
            as_i64=True,
            rooted_pcc_lifetimes=(current_root,),
        )
        events_value = (
            ir.Constant(_I64, events)
            if isinstance(events, int)
            else self._emit_expr_with_cpy_operand_cleanup(
                events,
                (),
                as_i64=True,
                rooted_pcc_lifetimes=(current_root,),
            )
        )
        timeout_value = (
            ir.Constant(_I64, timeout_ms)
            if isinstance(timeout_ms, int)
            else self._emit_expr_with_cpy_operand_cleanup(
                timeout_ms,
                (),
                as_i64=True,
                rooted_pcc_lifetimes=(current_root,),
            )
        )
        rc = self.builder.call(
            self.runtime["py_virtual_thread_block_on_fd"],
            [
                self._load_virtual_thread_operand_root(current_root),
                fd_value,
                events_value,
                timeout_value,
            ],
            name=self._fresh("vthread.fd.current.rc"),
        )
        self._release_rooted_pcc_lifetimes((current_root,))
        self._emit_virtual_thread_rc_check(rc, "virtual thread fd wait")
        if len(self._generator_ctx_stack) == 0:
            return self._emit_none_literal()
        parked = self.builder.icmp_signed(
            "==",
            rc,
            ir.Constant(_I64, 0),
            name=self._fresh("vthread.fd.parked"),
        )
        park_bb = self.current_function.append_basic_block(
            name=self._fresh("vthread.fd.park")
        )
        ready_bb = self.current_function.append_basic_block(
            name=self._fresh("vthread.fd.ready")
        )
        self.builder.cbranch(parked, park_bb, ready_bb)
        self.builder.position_at_end(park_bb)
        self._emit_generator_yield_value(self._emit_none_literal())
        if not self._builder_block_is_terminated():
            self.builder.branch(ready_bb)
        self.builder.position_at_end(ready_bb)
        return self._emit_none_literal()

    def _emit_virtual_thread_tcp_state(
        self,
        call_expr: Call,
        kind: str,
        args: tuple[Expr, ...],
        timeout_index: int,
        initial_progress: int,
    ) -> ir.Value:
        """Evaluate one TCP call once and publish relocatable retry state."""
        args_root = self._emit_virtual_thread_dynamic_args_with_roots(args, ())
        timeout_ms = ir.Constant(_I64, -1)
        if timeout_index < len(args):
            rooted_args = self._load_virtual_thread_operand_root(args_root)
            timeout_obj = self.builder.call(
                self.runtime["py_tuple_get_known"],
                [rooted_args, ir.Constant(_I64, timeout_index)],
                name=self._fresh("vthread.tcp.timeout.object"),
            )
            timeout_ms = self.builder.call(
                self.runtime["py_int_value_i64"],
                [timeout_obj],
                name=self._fresh("vthread.tcp.timeout.value"),
            )
            self._gc_release(timeout_obj)
            self._emit_virtual_thread_container_call_check(
                call_expr.span,
                (args_root,),
            )
        deadline = self.builder.call(
            self.runtime["py_virtual_thread_tcp_deadline"],
            [timeout_ms],
            name=self._fresh("vthread.tcp.deadline"),
        )
        self._emit_virtual_thread_container_call_check(
            call_expr.span,
            (args_root,),
        )

        state = self.builder.call(
            self.runtime["py_list_new"],
            [ir.Constant(_I64, 4)],
            name=self._fresh("vthread.tcp.state"),
        )
        state_root = self._enter_virtual_thread_operand_root(
            state,
            None,
            "vthread.tcp.state",
        )
        self._emit_virtual_thread_container_call_check(
            call_expr.span,
            (args_root, state_root),
        )
        self.builder.call(
            self.runtime["py_list_append"],
            [
                self._load_virtual_thread_operand_root(state_root),
                self._load_virtual_thread_operand_root(args_root),
            ],
        )
        self._emit_virtual_thread_container_call_check(
            call_expr.span,
            (args_root, state_root),
        )

        for label, value in (
            ("deadline", deadline),
            ("progress", ir.Constant(_I64, initial_progress)),
            ("generation", ir.Constant(_I64, -1)),
        ):
            boxed = self.builder.call(
                self.runtime["py_int_from_i64"],
                [value],
                name=self._fresh("vthread.tcp." + label + ".object"),
            )
            boxed_root = self._enter_virtual_thread_operand_root(
                boxed,
                None,
                "vthread.tcp." + label,
            )
            self._emit_virtual_thread_container_call_check(
                call_expr.span,
                (args_root, state_root, boxed_root),
            )
            self.builder.call(
                self.runtime["py_list_append"],
                [
                    self._load_virtual_thread_operand_root(state_root),
                    self._load_virtual_thread_operand_root(boxed_root),
                ],
            )
            self._emit_virtual_thread_container_call_check(
                call_expr.span,
                (args_root, state_root, boxed_root),
            )
            self._release_rooted_pcc_lifetimes((boxed_root,))

        _slot, frame_root = generator_may_park_child_slot(
            self,
            call_expr,
            kind,
        )
        self.builder.call(
            self.runtime["pcc_gc_store_root"],
            [frame_root, self._load_virtual_thread_operand_root(state_root)],
        )
        self._release_rooted_pcc_lifetimes((args_root, state_root))
        return frame_root

    def _emit_virtual_thread_tcp_state_item(
        self,
        frame_root: ir.Value,
        index: int,
    ) -> ir.Value:
        state = self.builder.call(
            self.runtime["pcc_gc_load_ptr"],
            [ir.Constant(_CSTR, None), frame_root],
            name=self._fresh("vthread.tcp.state.rooted"),
        )
        return self.builder.call(
            self.runtime["py_list_get"],
            [state, ir.Constant(_I64, index)],
            name=self._fresh("vthread.tcp.state.item"),
        )

    def _emit_virtual_thread_tcp_arg_item(
        self,
        frame_root: ir.Value,
        index: int,
    ) -> ir.Value:
        args_obj = self._emit_virtual_thread_tcp_state_item(frame_root, 0)
        item = self.builder.call(
            self.runtime["py_tuple_get_known"],
            [args_obj, ir.Constant(_I64, index)],
            name=self._fresh("vthread.tcp.arg.item"),
        )
        self._gc_release(args_obj)
        return item

    def _emit_virtual_thread_tcp_owned_i64(self, value: ir.Value) -> ir.Value:
        result = self.builder.call(
            self.runtime["py_int_value_i64"],
            [value],
            name=self._fresh("vthread.tcp.i64"),
        )
        self._gc_release(value)
        return result

    def _emit_virtual_thread_tcp_arg_i64(
        self,
        frame_root: ir.Value,
        index: int,
    ) -> ir.Value:
        return self._emit_virtual_thread_tcp_owned_i64(
            self._emit_virtual_thread_tcp_arg_item(frame_root, index)
        )

    def _emit_virtual_thread_tcp_state_i64(
        self,
        frame_root: ir.Value,
        index: int,
    ) -> ir.Value:
        return self._emit_virtual_thread_tcp_owned_i64(
            self._emit_virtual_thread_tcp_state_item(frame_root, index)
        )

    def _emit_virtual_thread_tcp_set_state_i64(
        self,
        frame_root: ir.Value,
        index: int,
        value: ir.Value,
        label: str,
        span,
    ) -> None:
        boxed = self.builder.call(
            self.runtime["py_int_from_i64"],
            [value],
            name=self._fresh("vthread.tcp." + label + ".next"),
        )
        self._emit_post_call_err_check(span)
        state = self.builder.call(
            self.runtime["pcc_gc_load_ptr"],
            [ir.Constant(_CSTR, None), frame_root],
            name=self._fresh("vthread.tcp.state.update"),
        )
        self.builder.call(
            self.runtime["py_list_set"],
            [state, ir.Constant(_I64, index), boxed],
        )
        self._gc_release(boxed)

    def _emit_virtual_thread_tcp_set_progress(
        self,
        frame_root: ir.Value,
        progress: ir.Value,
        span,
    ) -> None:
        self._emit_virtual_thread_tcp_set_state_i64(
            frame_root, 2, progress, "progress", span
        )

    def _emit_virtual_thread_tcp_capture_generation(
        self,
        frame_root: ir.Value,
        fd: ir.Value,
        span,
    ) -> ir.Value:
        generation = self.builder.call(
            self.runtime["py_virtual_thread_io_resource_generation"],
            [fd],
            name=self._fresh("vthread.tcp.generation"),
        )
        valid = self.builder.icmp_signed(
            ">",
            generation,
            ir.Constant(_I64, 0),
            name=self._fresh("vthread.tcp.generation.valid"),
        )
        rc = self.builder.select(
            valid,
            ir.Constant(_I64, 0),
            ir.Constant(_I64, -1),
            name=self._fresh("vthread.tcp.generation.rc"),
        )
        self._emit_virtual_thread_rc_check(rc, "virtual thread TCP generation")
        self._emit_virtual_thread_tcp_set_state_i64(
            frame_root, 3, generation, "generation", span
        )
        return generation

    def _emit_virtual_thread_tcp_clear_state(self, frame_root: ir.Value) -> None:
        none_gv = declare_runtime_global(self.module, "py_None")
        none_obj = self.builder.load(
            none_gv,
            name=self._fresh("vthread.tcp.none"),
        )
        self.builder.call(
            self.runtime["pcc_gc_store_root"],
            [frame_root, none_obj],
        )

    def _emit_virtual_thread_tcp_abandon_state(
        self,
        frame_root: ir.Value,
        close_progress: bool,
    ) -> None:
        if close_progress:
            fd = self._emit_virtual_thread_tcp_state_i64(frame_root, 2)
            should_close = self.builder.icmp_signed(
                ">=",
                fd,
                ir.Constant(_I64, 0),
                name=self._fresh("vthread.tcp.cleanup.has_fd"),
            )
            close_bb = self.current_function.append_basic_block(
                name=self._fresh("vthread.tcp.cleanup.close")
            )
            clear_bb = self.current_function.append_basic_block(
                name=self._fresh("vthread.tcp.cleanup.clear")
            )
            self.builder.cbranch(should_close, close_bb, clear_bb)
            self.builder.position_at_end(close_bb)
            self.builder.call(
                self.runtime["py_virtual_thread_tcp_close_quiet"],
                [fd],
            )
            self.builder.branch(clear_bb)
            self.builder.position_at_end(clear_bb)
        self._emit_virtual_thread_tcp_clear_state(frame_root)

    def _emit_virtual_thread_tcp_cleanup_block(
        self,
        frame_root: ir.Value,
        close_progress: bool,
    ) -> tuple[ir.Block, ir.Block]:
        outer = self._current_try_err_block()
        if outer is None:
            outer = self._ensure_fn_err_exit()
        cleanup = self.current_function.append_basic_block(
            name=self._fresh("vthread.tcp.unwind")
        )
        saved_block = self.builder._block
        self.builder.position_at_end(cleanup)
        self._emit_virtual_thread_tcp_abandon_state(
            frame_root,
            close_progress,
        )
        self.builder.branch(outer)
        self.builder.position_at_end(saved_block)
        return cleanup, outer

    def _emit_virtual_thread_tcp_status_guard(
        self,
        status: ir.Value,
        expected: int,
        label: str,
    ) -> None:
        valid = self.builder.icmp_signed(
            "==",
            status,
            ir.Constant(_I64, expected),
            name=self._fresh("vthread.tcp.status.valid"),
        )
        rc = self.builder.select(
            valid,
            ir.Constant(_I64, 0),
            ir.Constant(_I64, -1),
            name=self._fresh("vthread.tcp.status.rc"),
        )
        self._emit_virtual_thread_rc_check(rc, label)

    def _emit_virtual_thread_tcp_park_retry(
        self,
        fd: ir.Value,
        generation: ir.Value,
        events: int,
        deadline: ir.Value,
        frame_root: ir.Value,
        cleanup: ir.Block,
        outer_error: ir.Block,
        close_progress: bool,
        retry: ir.Block,
    ) -> None:
        remaining = self.builder.call(
            self.runtime["py_virtual_thread_tcp_remaining"],
            [deadline],
            name=self._fresh("vthread.tcp.remaining"),
        )
        expired = self.builder.icmp_signed(
            "==",
            remaining,
            ir.Constant(_I64, 0),
            name=self._fresh("vthread.tcp.expired"),
        )
        timeout_bb = self.current_function.append_basic_block(
            name=self._fresh("vthread.tcp.timeout")
        )
        wait_bb = self.current_function.append_basic_block(
            name=self._fresh("vthread.tcp.wait")
        )
        self.builder.cbranch(expired, timeout_bb, wait_bb)

        self.builder.position_at_end(timeout_bb)
        self._emit_virtual_thread_tcp_abandon_state(
            frame_root,
            close_progress,
        )
        self.builder.call(
            self.runtime["py_virtual_thread_tcp_raise_timeout"],
            [],
        )
        self.builder.branch(outer_error)

        self.builder.position_at_end(wait_bb)
        current = self.builder.call(
            self.runtime["py_virtual_thread_current"],
            [],
            name=self._fresh("vthread.tcp.current"),
        )
        current_root = self._enter_virtual_thread_operand_root(
            current,
            None,
            "vthread.tcp.current",
        )
        rc = self.builder.call(
            self.runtime["py_virtual_thread_block_on_fd_generation"],
            [
                self._load_virtual_thread_operand_root(current_root),
                fd,
                generation,
                ir.Constant(_I64, events),
                remaining,
            ],
            name=self._fresh("vthread.tcp.wait.rc"),
        )
        self._release_rooted_pcc_lifetimes((current_root,))
        self._emit_virtual_thread_rc_check(rc, "virtual thread TCP wait")
        parked = self.builder.icmp_signed(
            "==",
            rc,
            ir.Constant(_I64, 0),
            name=self._fresh("vthread.tcp.parked"),
        )
        park_bb = self.current_function.append_basic_block(
            name=self._fresh("vthread.tcp.park")
        )
        ready_bb = self.current_function.append_basic_block(
            name=self._fresh("vthread.tcp.ready")
        )
        self.builder.cbranch(parked, park_bb, ready_bb)
        self.builder.position_at_end(park_bb)
        self._emit_generator_yield_value(
            self._emit_none_literal(),
            resume_err_target=cleanup,
        )
        if not self._builder_block_is_terminated():
            self.builder.branch(retry)
        self.builder.position_at_end(ready_bb)
        self.builder.branch(retry)

    def _emit_virtual_thread_tcp_listen(
        self,
        args: tuple[Expr, ...],
        call_expr: Call,
    ) -> Optional[ir.Value]:
        if len(args) < 2 or len(args) > 3:
            return None
        host_obj = self._emit_expr_with_cpy_operand_cleanup(
            args[0],
            (),
            as_pcc_object=True,
        )
        host_root = self._enter_virtual_thread_operand_root(
            host_obj,
            args[0],
            "vthread.tcp.listen.host",
        )
        port_obj = self._emit_expr_with_cpy_operand_cleanup(
            args[1],
            (),
            as_pcc_object=True,
            rooted_pcc_lifetimes=(host_root,),
        )
        port_root = self._enter_virtual_thread_operand_root(
            port_obj,
            args[1],
            "vthread.tcp.listen.port",
        )
        backlog = ir.Constant(_I64, 128)
        if len(args) == 3:
            backlog = self._emit_expr_with_cpy_operand_cleanup(
                args[2],
                (),
                as_i64=True,
                rooted_pcc_lifetimes=(host_root, port_root),
            )
        fd = self.builder.call(
            self.runtime["py_virtual_thread_tcp_listen"],
            [
                self._load_virtual_thread_operand_root(host_root),
                self._load_virtual_thread_operand_root(port_root),
                backlog,
            ],
            name=self._fresh("vthread.tcp.listen.fd"),
        )
        self._emit_virtual_thread_container_call_check(
            call_expr.span,
            (host_root, port_root),
        )
        self._release_rooted_pcc_lifetimes((host_root, port_root))
        return fd

    def _emit_virtual_thread_tcp_close(
        self,
        args: tuple[Expr, ...],
        call_expr: Call,
    ) -> Optional[ir.Value]:
        if len(args) != 1:
            return None
        fd = self._emit_expr_as_i64(args[0])
        self.builder.call(
            self.runtime["py_virtual_thread_tcp_close"],
            [fd],
            name=self._fresh("vthread.tcp.close.rc"),
        )
        self._emit_post_call_err_check(call_expr.span)
        return self._emit_none_literal()

    def _emit_virtual_thread_tcp_accept(
        self,
        args: tuple[Expr, ...],
        call_expr: Call,
    ) -> Optional[ir.Value]:
        if len(args) < 1 or len(args) > 2:
            return None
        if len(self._generator_ctx_stack) == 0:
            raise L1CodegenError("tcp_accept requires a resumable parent")
        frame_root = self._emit_virtual_thread_tcp_state(
            call_expr,
            "pcc.virtual_thread.tcp_accept",
            args,
            1,
            -1,
        )
        cleanup, outer_error = self._emit_virtual_thread_tcp_cleanup_block(
            frame_root,
            False,
        )
        previous_error = getattr(self, "_try_err_block", None)
        self._try_err_block = cleanup
        try:
            output_fd = self._alloca_in_entry(
                _I64,
                name=self._fresh("vthread.tcp.accept.output"),
            )
            initial_listener_fd = self._emit_virtual_thread_tcp_arg_i64(
                frame_root, 0
            )
            self._emit_virtual_thread_tcp_capture_generation(
                frame_root, initial_listener_fd, call_expr.span
            )
            loop_bb = self.current_function.append_basic_block(
                name=self._fresh("vthread.tcp.accept.observe")
            )
            wait_bb = self.current_function.append_basic_block(
                name=self._fresh("vthread.tcp.accept.would_block")
            )
            done_bb = self.current_function.append_basic_block(
                name=self._fresh("vthread.tcp.accept.done")
            )
            self.builder.branch(loop_bb)
            self.builder.position_at_end(loop_bb)
            listener_fd = self._emit_virtual_thread_tcp_arg_i64(frame_root, 0)
            generation = self._emit_virtual_thread_tcp_state_i64(frame_root, 3)
            status = self.builder.call(
                self.runtime["py_virtual_thread_tcp_accept_observe"],
                [listener_fd, generation, output_fd],
                name=self._fresh("vthread.tcp.accept.status"),
            )
            self._emit_post_call_err_check(call_expr.span)
            accepted = self.builder.icmp_signed(
                "==",
                status,
                ir.Constant(_I64, 0),
                name=self._fresh("vthread.tcp.accept.accepted"),
            )
            self.builder.cbranch(accepted, done_bb, wait_bb)

            self.builder.position_at_end(wait_bb)
            self._emit_virtual_thread_tcp_status_guard(
                status,
                1,
                "virtual thread TCP accept status",
            )
            deadline = self._emit_virtual_thread_tcp_state_i64(frame_root, 1)
            self._emit_virtual_thread_tcp_park_retry(
                listener_fd,
                generation,
                1,
                deadline,
                frame_root,
                cleanup,
                outer_error,
                False,
                loop_bb,
            )

            self.builder.position_at_end(done_bb)
            result = self.builder.load(
                output_fd,
                name=self._fresh("vthread.tcp.accept.fd"),
            )
            register_rc = self.builder.call(
                self.runtime["py_virtual_thread_tcp_register_accepted"],
                [result],
                name=self._fresh("vthread.tcp.accept.register"),
            )
            self._emit_virtual_thread_rc_check(
                register_rc, "virtual thread TCP accepted descriptor"
            )
            self._emit_virtual_thread_tcp_clear_state(frame_root)
            return result
        finally:
            self._try_err_block = previous_error

    def _emit_virtual_thread_tcp_connect(
        self,
        args: tuple[Expr, ...],
        call_expr: Call,
    ) -> Optional[ir.Value]:
        if len(args) < 2 or len(args) > 3:
            return None
        if len(self._generator_ctx_stack) == 0:
            raise L1CodegenError("tcp_connect requires a resumable parent")
        frame_root = self._emit_virtual_thread_tcp_state(
            call_expr,
            "pcc.virtual_thread.tcp_connect",
            args,
            2,
            -1,
        )
        cleanup, outer_error = self._emit_virtual_thread_tcp_cleanup_block(
            frame_root,
            True,
        )
        previous_error = getattr(self, "_try_err_block", None)
        self._try_err_block = cleanup
        try:
            output_fd = self._alloca_in_entry(
                _I64,
                name=self._fresh("vthread.tcp.connect.output"),
            )
            host_obj = self._emit_virtual_thread_tcp_arg_item(frame_root, 0)
            port_obj = self._emit_virtual_thread_tcp_arg_item(frame_root, 1)
            start_status = self.builder.call(
                self.runtime["py_virtual_thread_tcp_connect_start"],
                [host_obj, port_obj, output_fd],
                name=self._fresh("vthread.tcp.connect.start.status"),
            )
            # The public signature restricts these to str/int. Their releases
            # cannot invoke user finalizers and therefore cannot replace the
            # explicit socket error set by the runtime wrapper.
            self._gc_release(port_obj)
            self._gc_release(host_obj)
            self._emit_post_call_err_check(call_expr.span)
            fd = self.builder.load(
                output_fd,
                name=self._fresh("vthread.tcp.connect.fd.initial"),
            )
            self._emit_virtual_thread_tcp_set_progress(
                frame_root,
                fd,
                call_expr.span,
            )
            self._emit_virtual_thread_tcp_capture_generation(
                frame_root, fd, call_expr.span
            )

            loop_bb = self.current_function.append_basic_block(
                name=self._fresh("vthread.tcp.connect.observe")
            )
            wait_start_bb = self.current_function.append_basic_block(
                name=self._fresh("vthread.tcp.connect.start.wait")
            )
            wait_bb = self.current_function.append_basic_block(
                name=self._fresh("vthread.tcp.connect.would_block")
            )
            done_bb = self.current_function.append_basic_block(
                name=self._fresh("vthread.tcp.connect.done")
            )
            started = self.builder.icmp_signed(
                "==",
                start_status,
                ir.Constant(_I64, 3),
                name=self._fresh("vthread.tcp.connect.immediate"),
            )
            self.builder.cbranch(started, done_bb, wait_start_bb)
            self.builder.position_at_end(wait_start_bb)
            self._emit_virtual_thread_tcp_status_guard(
                start_status,
                1,
                "virtual thread TCP connect start status",
            )
            self.builder.branch(loop_bb)

            self.builder.position_at_end(loop_bb)
            current_fd = self._emit_virtual_thread_tcp_state_i64(frame_root, 2)
            generation = self._emit_virtual_thread_tcp_state_i64(frame_root, 3)
            status = self.builder.call(
                self.runtime["py_virtual_thread_tcp_connect_observe"],
                [current_fd, generation],
                name=self._fresh("vthread.tcp.connect.status"),
            )
            self._emit_post_call_err_check(call_expr.span)
            connected = self.builder.icmp_signed(
                "==",
                status,
                ir.Constant(_I64, 3),
                name=self._fresh("vthread.tcp.connect.connected"),
            )
            self.builder.cbranch(connected, done_bb, wait_bb)

            self.builder.position_at_end(wait_bb)
            self._emit_virtual_thread_tcp_status_guard(
                status,
                1,
                "virtual thread TCP connect status",
            )
            deadline = self._emit_virtual_thread_tcp_state_i64(frame_root, 1)
            self._emit_virtual_thread_tcp_park_retry(
                current_fd,
                generation,
                4,
                deadline,
                frame_root,
                cleanup,
                outer_error,
                True,
                loop_bb,
            )

            self.builder.position_at_end(done_bb)
            result = self._emit_virtual_thread_tcp_state_i64(frame_root, 2)
            self._emit_virtual_thread_tcp_clear_state(frame_root)
            return result
        finally:
            self._try_err_block = previous_error

    def _emit_virtual_thread_tcp_recv(
        self,
        args: tuple[Expr, ...],
        call_expr: Call,
    ) -> Optional[ir.Value]:
        if len(args) < 2 or len(args) > 3:
            return None
        if len(self._generator_ctx_stack) == 0:
            raise L1CodegenError("tcp_recv requires a resumable parent")
        frame_root = self._emit_virtual_thread_tcp_state(
            call_expr,
            "pcc.virtual_thread.tcp_recv",
            args,
            2,
            -1,
        )
        cleanup, outer_error = self._emit_virtual_thread_tcp_cleanup_block(
            frame_root,
            False,
        )
        previous_error = getattr(self, "_try_err_block", None)
        self._try_err_block = cleanup
        try:
            output_status = self._alloca_in_entry(
                _I64,
                name=self._fresh("vthread.tcp.recv.status.output"),
            )
            initial_fd = self._emit_virtual_thread_tcp_arg_i64(frame_root, 0)
            self._emit_virtual_thread_tcp_capture_generation(
                frame_root, initial_fd, call_expr.span
            )
            loop_bb = self.current_function.append_basic_block(
                name=self._fresh("vthread.tcp.recv.observe")
            )
            wait_bb = self.current_function.append_basic_block(
                name=self._fresh("vthread.tcp.recv.would_block")
            )
            done_bb = self.current_function.append_basic_block(
                name=self._fresh("vthread.tcp.recv.done")
            )
            self.builder.branch(loop_bb)
            self.builder.position_at_end(loop_bb)
            fd = self._emit_virtual_thread_tcp_arg_i64(frame_root, 0)
            max_bytes = self._emit_virtual_thread_tcp_arg_i64(frame_root, 1)
            generation = self._emit_virtual_thread_tcp_state_i64(frame_root, 3)
            result = self.builder.call(
                self.runtime["py_virtual_thread_tcp_recv_observe"],
                [fd, generation, max_bytes, output_status],
                name=self._fresh("vthread.tcp.recv.result"),
            )
            self._emit_post_call_err_check(call_expr.span)
            would_block = self.builder.icmp_unsigned(
                "==",
                result,
                ir.Constant(_CSTR, None),
                name=self._fresh("vthread.tcp.recv.would_block.i1"),
            )
            self.builder.cbranch(would_block, wait_bb, done_bb)

            self.builder.position_at_end(wait_bb)
            status = self.builder.load(
                output_status,
                name=self._fresh("vthread.tcp.recv.status"),
            )
            self._emit_virtual_thread_tcp_status_guard(
                status,
                1,
                "virtual thread TCP recv status",
            )
            deadline = self._emit_virtual_thread_tcp_state_i64(frame_root, 1)
            self._emit_virtual_thread_tcp_park_retry(
                fd,
                generation,
                1,
                deadline,
                frame_root,
                cleanup,
                outer_error,
                False,
                loop_bb,
            )

            self.builder.position_at_end(done_bb)
            self._emit_virtual_thread_tcp_clear_state(frame_root)
            return result
        finally:
            self._try_err_block = previous_error

    def _emit_virtual_thread_tcp_send_all(
        self,
        args: tuple[Expr, ...],
        call_expr: Call,
    ) -> Optional[ir.Value]:
        if len(args) < 2 or len(args) > 3:
            return None
        if len(self._generator_ctx_stack) == 0:
            raise L1CodegenError("tcp_send_all requires a resumable parent")
        frame_root = self._emit_virtual_thread_tcp_state(
            call_expr,
            "pcc.virtual_thread.tcp_send_all",
            args,
            2,
            0,
        )
        cleanup, outer_error = self._emit_virtual_thread_tcp_cleanup_block(
            frame_root,
            False,
        )
        previous_error = getattr(self, "_try_err_block", None)
        self._try_err_block = cleanup
        try:
            output_count = self._alloca_in_entry(
                _I64,
                name=self._fresh("vthread.tcp.send.count.output"),
            )
            initial_fd = self._emit_virtual_thread_tcp_arg_i64(frame_root, 0)
            self._emit_virtual_thread_tcp_capture_generation(
                frame_root, initial_fd, call_expr.span
            )
            loop_bb = self.current_function.append_basic_block(
                name=self._fresh("vthread.tcp.send.observe")
            )
            progress_bb = self.current_function.append_basic_block(
                name=self._fresh("vthread.tcp.send.progress")
            )
            wait_bb = self.current_function.append_basic_block(
                name=self._fresh("vthread.tcp.send.would_block")
            )
            continue_bb = self.current_function.append_basic_block(
                name=self._fresh("vthread.tcp.send.continue")
            )
            done_bb = self.current_function.append_basic_block(
                name=self._fresh("vthread.tcp.send.done")
            )
            self.builder.branch(loop_bb)
            self.builder.position_at_end(loop_bb)
            fd = self._emit_virtual_thread_tcp_arg_i64(frame_root, 0)
            generation = self._emit_virtual_thread_tcp_state_i64(frame_root, 3)
            data_obj = self._emit_virtual_thread_tcp_arg_item(frame_root, 1)
            data_len = self.builder.call(
                self.runtime["py_bytes_len"],
                [data_obj],
                name=self._fresh("vthread.tcp.send.length"),
            )
            offset = self._emit_virtual_thread_tcp_state_i64(frame_root, 2)
            status = self.builder.call(
                self.runtime["py_virtual_thread_tcp_send_observe"],
                [fd, generation, data_obj, offset, output_count],
                name=self._fresh("vthread.tcp.send.status"),
            )
            self._gc_release(data_obj)
            self._emit_post_call_err_check(call_expr.span)
            made_progress = self.builder.icmp_signed(
                "==",
                status,
                ir.Constant(_I64, 0),
                name=self._fresh("vthread.tcp.send.progress.i1"),
            )
            self.builder.cbranch(made_progress, progress_bb, wait_bb)

            self.builder.position_at_end(progress_bb)
            sent = self.builder.load(
                output_count,
                name=self._fresh("vthread.tcp.send.count"),
            )
            next_offset = self.builder.add(
                offset,
                sent,
                name=self._fresh("vthread.tcp.send.offset.next"),
            )
            complete = self.builder.icmp_signed(
                "==",
                next_offset,
                data_len,
                name=self._fresh("vthread.tcp.send.complete"),
            )
            self.builder.cbranch(complete, done_bb, continue_bb)

            self.builder.position_at_end(continue_bb)
            self._emit_virtual_thread_tcp_set_progress(
                frame_root,
                next_offset,
                call_expr.span,
            )
            self.builder.branch(loop_bb)

            self.builder.position_at_end(wait_bb)
            self._emit_virtual_thread_tcp_status_guard(
                status,
                1,
                "virtual thread TCP send status",
            )
            deadline = self._emit_virtual_thread_tcp_state_i64(frame_root, 1)
            self._emit_virtual_thread_tcp_park_retry(
                fd,
                generation,
                4,
                deadline,
                frame_root,
                cleanup,
                outer_error,
                False,
                loop_bb,
            )

            self.builder.position_at_end(done_bb)
            self._emit_virtual_thread_tcp_clear_state(frame_root)
            return self._emit_none_literal()
        finally:
            self._try_err_block = previous_error

    def _emit_virtual_thread_resume_function(
        self,
        name: str,
        fn: ir.Function,
        ast_func_def: FuncDef,
        n_args: int,
    ) -> ir.Function:
        resume_name = f"{fn.name}__vthread_resume_{n_args}"
        existing = self.module.globals.get(resume_name)
        if isinstance(existing, ir.Function):
            return existing

        fnty = ir.FunctionType(_I64, [_CSTR, _CSTR])
        resume_fn = ir.Function(self.module, fnty, name=resume_name)
        resume_fn.linkage = "internal"
        resume_fn.args[0].name = "vthread"
        resume_fn.args[1].name = "continuation"

        saved_builder = self.builder
        saved_fn = self.current_function
        saved_fd = self.current_func_def

        entry = resume_fn.append_basic_block(name="entry")
        self.builder = ir.IRBuilder(entry)
        self.current_function = resume_fn
        self.current_func_def = ast_func_def

        runtime_formals = tuple(a for a in ast_func_def.args if a.name != "")
        call_args: list[ir.Value] = []
        loaded_slots: list[ir.Value] = []
        for idx, formal in enumerate(runtime_formals[:n_args]):
            slot_obj = self.builder.call(
                self.runtime["py_continuation_get_slot"],
                [resume_fn.args[1], ir.Constant(_I64, idx)],
                name=self._fresh(f"vthread.slot.{idx}"),
            )
            loaded_slots.append(slot_obj)
            if isinstance(fn.args[idx].type, ir.PointerType):
                # The spawned function was lowered with the boxed (ptr) calling
                # convention — it is passed to vt.spawn as a first-class value,
                # so its params are boxed PyObjects, not the annotation's native
                # scalar. Hand it the boxed slot object directly; unboxing to
                # i64/double/i1 here would pass a scalar where a ptr is declared
                # (an LLVM type error). The callee borrows the boxed arg; the
                # resume owns the slots and releases them after the call below.
                call_args.append(slot_obj)
            else:
                bind_ty = formal.annotation or DynType(name="dyn")
                call_args.append(
                    marshal.marshal_from_object(
                        self.builder,
                        self.module,
                        self.runtime,
                        slot_obj,
                        bind_ty,
                    )
                )

        # Ordinary (non-resumable) workers cannot unmount while arbitrary
        # native/user code runs.  Classify that boundary explicitly as pinned
        # in scheduler telemetry instead of presenting it as a virtual-thread
        # park.  Proven ``may_park`` workers take the generator path and never
        # enter this adapter.
        pin_reason = self._ptr_to_cstr(
            self._cstr_global(
                "closed-world ordinary worker: " + name,
                self._fresh(".vthread.ordinary.pin.reason"),
            )
        )
        self.builder.call(
            self.runtime["py_virtual_thread_pin_enter"],
            [resume_fn.args[0], pin_reason],
        )

        ret_ty = ast_func_def.return_ty
        if ret_ty is None or isinstance(ret_ty, NoneType):
            result_val = self.builder.call(fn, call_args)
        else:
            result_val = self.builder.call(
                fn,
                call_args,
                name=self._fresh(f"{name}.vthread.result"),
            )
        self.builder.call(
            self.runtime["py_virtual_thread_pin_leave"],
            [resume_fn.args[0]],
        )
        for loaded_slot in loaded_slots:
            self._gc_release(loaded_slot)

        failure = self.builder.call(
            self.runtime["py_current_exception"],
            [],
            name=self._fresh("vthread.worker.exception"),
        )
        failed = self.builder.icmp_unsigned(
            "!=",
            failure,
            ir.Constant(_CSTR, None),
            name=self._fresh("vthread.worker.failed"),
        )
        failed_bb = resume_fn.append_basic_block(
            name=self._fresh("vthread.worker.failure")
        )
        complete_bb = resume_fn.append_basic_block(
            name=self._fresh("vthread.worker.complete")
        )
        self.builder.cbranch(failed, failed_bb, complete_bb)

        self.builder.position_at_end(failed_bb)
        failed_rc = self.builder.call(
            self.runtime["py_virtual_thread_fail"],
            [resume_fn.args[0], failure],
            name=self._fresh("vthread.fail.rc"),
        )
        self.builder.call(self.runtime["py_clear_exception"], [])
        self.builder.ret(failed_rc)

        self.builder.position_at_end(complete_bb)
        if ret_ty is None or isinstance(ret_ty, NoneType):
            result_obj = self._emit_none_literal()
        elif isinstance(result_val.type, ir.PointerType):
            # Boxed-convention worker already returns a boxed PyObject; pass it
            # straight to py_virtual_thread_complete. Re-boxing via
            # marshal_to_object would treat the ptr as the annotation's native
            # scalar (e.g. ptrtoint of an int box), corrupting the result.
            result_obj = result_val
        else:
            result_obj = marshal.marshal_to_object(
                self.builder,
                self.module,
                self.runtime,
                result_val,
                ret_ty,
            )
        rc = self.builder.call(
            self.runtime["py_virtual_thread_complete"],
            [resume_fn.args[0], result_obj],
            name=self._fresh("vthread.complete.rc"),
        )
        if ret_ty is not None and not isinstance(ret_ty, NoneType):
            # complete() retains the published result in the vthread slot.  The
            # resume adapter still owns the function return / scalar box and
            # must drop that independent reference before returning.
            self._gc_release(result_obj)
        self.builder.ret(rc)

        self.builder = saved_builder
        self.current_function = saved_fn
        self.current_func_def = saved_fd
        return resume_fn

    def _emit_virtual_thread_spawn(
        self,
        args: tuple[Expr, ...],
        kwargs: tuple[tuple[str, Expr], ...],
    ) -> Optional[ir.Value]:
        if kwargs or len(args) < 1:
            return None
        target = args[0]
        if not isinstance(target, Name):
            raise L1CodegenError(
                "pcc.virtual_thread.spawn requires a closed-world function "
                "name; dynamic callable targets cannot prove may_park"
            )
        fn = self.functions.get(target.ident)
        if fn is None:
            raise L1CodegenError(
                "pcc.virtual_thread.spawn target is not a compiled "
                "closed-world function: "
                + target.ident
            )
        ast_func_def = self._find_user_funcdef(target.ident)
        if ast_func_def is None:
            raise L1CodegenError(
                "pcc.virtual_thread.spawn target has no closed-world "
                "FuncDef: "
                + target.ident
            )
        if self._funcdef_is_continuation_factory(ast_func_def):
            raise L1CodegenError("continuation factories cannot be spawn targets")

        rejected = getattr(self, "_vthread_rejected_park_boundaries", {})
        reject_reason = rejected.get(target.ident)
        if reject_reason is not None:
            if os.environ.get("PCC_DEBUG_VTHREAD_REJECTS", "").strip() not in ("", "0"):
                # The spawn error names only the last link of a rejection
                # chain; dump every rejected boundary so the first cause
                # (usually an unresolved callee of a may_park wrapper) is
                # visible without re-deriving the analysis by hand.
                for rejected_name in sorted(rejected):
                    sys.stderr.write(
                        "[pcc.vthread.reject] " + rejected_name + ": " + str(rejected[rejected_name]) + "\n"
                    )
            raise L1CodegenError(
                "pcc.virtual_thread.spawn cannot prove a resumable parking "
                "boundary for "
                + target.ident
                + ": "
                + reject_reason
                + "; use a directly-bound compiled function or keep this "
                "worker on an explicitly pinned carrier"
            )

        value_args = args[1:]
        runtime_formals = tuple(a for a in ast_func_def.args if a.name != "")
        if len(value_args) != len(runtime_formals):
            return None

        if target.ident in getattr(
            self,
            "_generator_func_names",
            set(),
        ) or self._funcdef_has_yield_sentinel(ast_func_def):
            return self._emit_virtual_thread_generator_spawn(
                target.ident,
                fn,
                ast_func_def,
                value_args,
                runtime_formals,
            )

        resume_fn = self._emit_virtual_thread_resume_function(
            target.ident,
            fn,
            ast_func_def,
            len(value_args),
        )
        frame_map = self._virtual_thread_frame_map(len(value_args))
        frame_map_ptr = self.builder.bitcast(
            frame_map,
            _CSTR,
            name=self._fresh("vthread.frame.map"),
        )

        boxed_slots: list[ir.Value] = []
        if value_args:
            slots_ty = ir.ArrayType(_CSTR, len(value_args))
            slots_arr = self._alloca_in_entry(
                slots_ty,
                name=self._fresh("vthread.slots"),
            )
            for idx, (arg_expr, formal) in enumerate(zip(value_args, runtime_formals)):
                raw = self._emit_expr(arg_expr)
                obj = marshal.marshal_to_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    raw,
                    formal.annotation or arg_expr.ty or DynType(name="dyn"),
                )
                boxed_slots.append(obj)
                gep = self.builder.gep(
                    slots_arr,
                    [ir.Constant(_I32, 0), ir.Constant(_I32, idx)],
                    inbounds=True,
                    name=self._fresh(f"vthread.slot.addr.{idx}"),
                )
                self.builder.store(obj, gep)
            slots_ptr = self.builder.gep(
                slots_arr,
                [ir.Constant(_I32, 0), ir.Constant(_I32, 0)],
                inbounds=True,
                name=self._fresh("vthread.slots.ptr"),
            )
            slots_arg = self.builder.bitcast(
                slots_ptr,
                _CSTR,
                name=self._fresh("vthread.slots.arg"),
            )
        else:
            slots_arg = ir.Constant(_CSTR, None)

        resume_ptr = self.builder.bitcast(
            resume_fn,
            _CSTR,
            name=self._fresh("vthread.resume.ptr"),
        )
        cont = self.builder.call(
            self.runtime["py_continuation_new_typed"],
            [frame_map_ptr, slots_arg, resume_ptr],
            name=self._fresh("vthread.cont"),
        )
        for boxed in boxed_slots:
            self._gc_release(boxed)
        vt = self.builder.call(
            self.runtime["py_virtual_thread_new"],
            [cont],
            name=self._fresh("vthread.new"),
        )
        self._gc_release(cont)
        rc = self.builder.call(
            self.runtime["py_virtual_thread_start"],
            [vt],
            name=self._fresh("vthread.start.rc"),
        )
        failed = self.builder.icmp_signed(
            "!=",
            rc,
            ir.Constant(_I64, 0),
            name=self._fresh("vthread.start.failed"),
        )
        ok_bb = self.current_function.append_basic_block(
            name=self._fresh("vthread.start.ok"),
        )
        fail_bb = self.current_function.append_basic_block(
            name=self._fresh("vthread.start.fail"),
        )
        self.builder.cbranch(failed, fail_bb, ok_bb)
        self.builder.position_at_end(fail_bb)
        # A failed enqueue never transfers the caller's vthread reference.
        # Release it before allocating the replacement RuntimeError so a
        # relocating collection cannot leave this SSA temporary stale.
        self._gc_release(vt)
        exc = self.builder.call(
            self.runtime["py_exc_new"],
            [
                ir.Constant(_I64, 7),
                self._ptr_to_cstr(
                    self._cstr_global(
                        "virtual thread start failed",
                        self._fresh(".vthread.start.err"),
                    )
                ),
            ],
            name=self._fresh("vthread.start.exc"),
        )
        self.builder.call(self.runtime["py_raise"], [exc])
        self._gc_release(exc)
        err_target = getattr(self, "_try_err_block", None)
        if err_target is None:
            err_target = self._ensure_fn_err_exit()
        self.builder.branch(err_target)
        self.builder.position_at_end(ok_bb)
        return vt

    def _emit_virtual_thread_generator_spawn(
        self,
        name: str,
        fn: ir.Function,
        ast_func_def: FuncDef,
        value_args: tuple[Expr, ...],
        runtime_formals: tuple,
    ) -> ir.Value:
        call_args: list[ir.Value] = []
        for arg_expr, formal in zip(value_args, runtime_formals):
            raw = self._emit_expr(arg_expr)
            call_args.append(
                self._coerce(
                    raw,
                    arg_expr.ty,
                    formal.annotation or DynType(name="dyn"),
                )
            )
        gen = self.builder.call(
            fn,
            call_args,
            name=self._fresh(f"{name}.vthread.gen"),
        )

        slots_ty = ir.ArrayType(_CSTR, 1)
        slots_arr = self._alloca_in_entry(
            slots_ty,
            name=self._fresh("vthread.gen.slots"),
        )
        gep = self.builder.gep(
            slots_arr,
            [ir.Constant(_I32, 0), ir.Constant(_I32, 0)],
            inbounds=True,
            name=self._fresh("vthread.gen.slot.addr"),
        )
        self.builder.store(gen, gep)
        slots_ptr = self.builder.gep(
            slots_arr,
            [ir.Constant(_I32, 0), ir.Constant(_I32, 0)],
            inbounds=True,
            name=self._fresh("vthread.gen.slots.ptr"),
        )
        slots_arg = self.builder.bitcast(
            slots_ptr,
            _CSTR,
            name=self._fresh("vthread.gen.slots.arg"),
        )
        frame_map = self._virtual_thread_frame_map(1)
        frame_map_ptr = self.builder.bitcast(
            frame_map,
            _CSTR,
            name=self._fresh("vthread.gen.frame.map"),
        )
        resume_ptr = self.builder.bitcast(
            self.runtime["py_virtual_thread_resume_generator"],
            _CSTR,
            name=self._fresh("vthread.gen.resume.ptr"),
        )
        cont = self.builder.call(
            self.runtime["py_continuation_new_typed"],
            [frame_map_ptr, slots_arg, resume_ptr],
            name=self._fresh("vthread.gen.cont"),
        )
        self._gc_release(gen)
        vt = self.builder.call(
            self.runtime["py_virtual_thread_new"],
            [cont],
            name=self._fresh("vthread.gen.new"),
        )
        self._gc_release(cont)
        rc = self.builder.call(
            self.runtime["py_virtual_thread_start"],
            [vt],
            name=self._fresh("vthread.gen.start.rc"),
        )
        failed = self.builder.icmp_signed(
            "!=",
            rc,
            ir.Constant(_I64, 0),
            name=self._fresh("vthread.gen.start.failed"),
        )
        ok_bb = self.current_function.append_basic_block(
            name=self._fresh("vthread.gen.start.ok"),
        )
        fail_bb = self.current_function.append_basic_block(
            name=self._fresh("vthread.gen.start.fail"),
        )
        self.builder.cbranch(failed, fail_bb, ok_bb)
        self.builder.position_at_end(fail_bb)
        self._gc_release(vt)
        exc = self.builder.call(
            self.runtime["py_exc_new"],
            [
                ir.Constant(_I64, 7),
                self._ptr_to_cstr(
                    self._cstr_global(
                        "virtual thread generator start failed",
                        self._fresh(".vthread.gen.start.err"),
                    )
                ),
            ],
            name=self._fresh("vthread.gen.start.exc"),
        )
        self.builder.call(self.runtime["py_raise"], [exc])
        self._gc_release(exc)
        err_target = getattr(self, "_try_err_block", None)
        if err_target is None:
            err_target = self._ensure_fn_err_exit()
        self.builder.branch(err_target)
        self.builder.position_at_end(ok_bb)
        return vt

    def _emit_native_virtual_thread_value_call(
        self,
        kind: str,
        args: tuple[Expr, ...],
        kwargs: tuple[tuple[str, Expr], ...],
        call_expr: Optional[Call] = None,
    ) -> Optional[ir.Value]:
        if (call_expr is not None
                and self._funcdef_is_continuation_factory(self.current_func_def)
                and kind not in ("pcc.virtual_thread.continuation", "pcc.virtual_thread.completed")
                and self._vthread_suspension_call(call_expr, self.current_func_def) is not None):
            raise L1CodegenError("factory methods must defer parking operations with continuation()")
        if kind in ("pcc.virtual_thread.continuation", "pcc.virtual_thread.completed"):
            if call_expr is None:
                raise L1CodegenError("continuation operations require a direct call site")
            if not self._funcdef_is_continuation_factory(self.current_func_def):
                raise L1CodegenError("continuation()/completed() require a continuation_factory method")
            if kwargs:
                raise L1CodegenError("continuation factories accept positional continuation arguments")
            if kind == "pcc.virtual_thread.completed":
                if len(args) != 1:
                    raise L1CodegenError("completed() requires one result value")
                value = self._emit_as_object(args[0])
                result = self.builder.call(self.runtime["py_gen_completed"], [value],
                                           name=self._fresh("vthread.completed"))
                if self._pcc_pointer_source_is_owned(args[0]):
                    result_slot = self._enter_container_temp_root(result, "vthread.completed")
                    self._release_virtual_thread_argument(value, args[0])
                    result = self._load_virtual_thread_operand_root((result_slot, False))
                    self._leave_container_temp_root(result_slot)
                self._emit_post_call_err_check(call_expr.span)
                return result
            if not args or not isinstance(args[0], Name):
                raise L1CodegenError("continuation() requires a closed-world function name")
            target_name = args[0].ident
            target_fn = self.functions.get(target_name)
            target_fd = self._find_user_funcdef(target_name)
            if (target_fn is None or target_fd is None
                    or self._funcdef_is_continuation_factory(target_fd)
                    or target_name not in self._vthread_may_park_func_names):
                raise L1CodegenError("continuation() requires an ordinary resumable function")
            return self._emit_direct_user_function_call(
                display_name=target_name, fn=target_fn, ast_func_def=target_fd,
                args=args[1:], kwargs=(),
            )
        if kind == "pcc.virtual_thread.spawn":
            return self._emit_virtual_thread_spawn(args, kwargs)
        if kind == "pcc.virtual_thread.call":
            if call_expr is None:
                raise L1CodegenError(
                    "pcc.virtual_thread.call is missing its source call site"
                )
            return self._emit_virtual_thread_callback_call(
                call_expr,
                args,
                kwargs,
            )
        if kind == "pcc.virtual_thread.tcp_listen":
            if kwargs or call_expr is None:
                return None
            return self._emit_virtual_thread_tcp_listen(args, call_expr)
        if kind == "pcc.virtual_thread.tcp_accept":
            if kwargs or call_expr is None:
                return None
            return self._emit_virtual_thread_tcp_accept(args, call_expr)
        if kind == "pcc.virtual_thread.tcp_connect":
            if kwargs or call_expr is None:
                return None
            return self._emit_virtual_thread_tcp_connect(args, call_expr)
        if kind == "pcc.virtual_thread.tcp_recv":
            if kwargs or call_expr is None:
                return None
            return self._emit_virtual_thread_tcp_recv(args, call_expr)
        if kind == "pcc.virtual_thread.tcp_send_all":
            if kwargs or call_expr is None:
                return None
            return self._emit_virtual_thread_tcp_send_all(args, call_expr)
        if kind == "pcc.virtual_thread.tcp_close":
            if kwargs or call_expr is None:
                return None
            return self._emit_virtual_thread_tcp_close(args, call_expr)
        if kwargs:
            return None
        if kind == "pcc.virtual_thread.mpsc":
            if len(args) != 1 or call_expr is None:
                return None
            result = self.builder.call(
                self.runtime["py_virtual_thread_channel_mpsc"],
                [self._emit_expr_as_i64(args[0])],
                name=self._fresh("vthread.channel.mpsc"),
            )
            self._emit_post_call_err_check(call_expr.span)
            return result
        if kind == "pcc.virtual_thread.oneshot":
            if args or call_expr is None:
                return None
            result = self.builder.call(
                self.runtime["py_virtual_thread_channel_oneshot"],
                [],
                name=self._fresh("vthread.channel.oneshot"),
            )
            self._emit_post_call_err_check(call_expr.span)
            return result
        if kind == "pcc.virtual_thread.sender_clone":
            if len(args) != 1 or call_expr is None:
                return None
            sender_obj = self._emit_as_object(args[0])
            result = self.builder.call(
                self.runtime["py_virtual_thread_channel_sender_clone"],
                [sender_obj],
                name=self._fresh("vthread.channel.sender.clone"),
            )
            self._release_virtual_thread_argument(sender_obj, args[0])
            self._emit_post_call_err_check(call_expr.span)
            return result
        if kind == "pcc.virtual_thread.send":
            if (
                len(args) != 2
                or len(self._generator_ctx_stack) == 0
                or call_expr is None
            ):
                return None
            current = self.builder.call(
                self.runtime["py_virtual_thread_current"],
                [],
                name=self._fresh("vthread.channel.send.current"),
            )
            current_root = self._enter_virtual_thread_operand_root(
                current,
                None,
                "vthread.channel.send.current",
            )
            sender_obj = self._emit_expr_with_cpy_operand_cleanup(
                args[0],
                (),
                as_object=True,
                rooted_pcc_lifetimes=(current_root,),
            )
            sender_root = self._enter_virtual_thread_operand_root(
                sender_obj,
                args[0],
                "vthread.channel.send.sender",
            )
            value_obj = self._emit_expr_with_cpy_operand_cleanup(
                args[1],
                (),
                as_object=True,
                rooted_pcc_lifetimes=(current_root, sender_root),
            )
            rc = self.builder.call(
                self.runtime["py_virtual_thread_channel_send_begin"],
                [
                    self._load_virtual_thread_operand_root(current_root),
                    self._load_virtual_thread_operand_root(sender_root),
                    value_obj,
                ],
                name=self._fresh("vthread.channel.send.begin.rc"),
            )
            self._release_virtual_thread_argument(value_obj, args[1])
            self._release_rooted_pcc_lifetimes(
                (current_root, sender_root)
            )
            self._emit_virtual_thread_rc_check(rc, "virtual thread channel send")
            parked = self.builder.icmp_signed(
                "==",
                rc,
                ir.Constant(_I64, 0),
                name=self._fresh("vthread.channel.send.parked"),
            )
            park_bb = self.current_function.append_basic_block(
                name=self._fresh("vthread.channel.send.park")
            )
            result_bb = self.current_function.append_basic_block(
                name=self._fresh("vthread.channel.send.result.block")
            )
            self.builder.cbranch(parked, park_bb, result_bb)
            self.builder.position_at_end(park_bb)
            self._emit_generator_yield_value(self._emit_none_literal())
            if not self._builder_block_is_terminated():
                self.builder.branch(result_bb)
            self.builder.position_at_end(result_bb)
            resumed = self.builder.call(
                self.runtime["py_virtual_thread_current"],
                [],
                name=self._fresh("vthread.channel.send.resumed"),
            )
            result_rc = self.builder.call(
                self.runtime["py_virtual_thread_channel_send_result"],
                [resumed],
                name=self._fresh("vthread.channel.send.result.rc"),
            )
            self._gc_release(resumed)
            self._emit_virtual_thread_rc_check(
                result_rc,
                "virtual thread channel send result",
            )
            return self.builder.icmp_signed(
                "!=",
                result_rc,
                ir.Constant(_I64, 0),
                name=self._fresh("vthread.channel.send.accepted"),
            )
        if kind == "pcc.virtual_thread.recv":
            if (
                len(args) != 1
                or len(self._generator_ctx_stack) == 0
                or call_expr is None
            ):
                return None
            current = self.builder.call(
                self.runtime["py_virtual_thread_current"],
                [],
                name=self._fresh("vthread.channel.recv.current"),
            )
            current_root = self._enter_virtual_thread_operand_root(
                current,
                None,
                "vthread.channel.recv.current",
            )
            receiver_obj = self._emit_expr_with_cpy_operand_cleanup(
                args[0],
                (),
                as_object=True,
                rooted_pcc_lifetimes=(current_root,),
            )
            rc = self.builder.call(
                self.runtime["py_virtual_thread_channel_recv_begin"],
                [
                    self._load_virtual_thread_operand_root(current_root),
                    receiver_obj,
                ],
                name=self._fresh("vthread.channel.recv.begin.rc"),
            )
            self._release_virtual_thread_argument(receiver_obj, args[0])
            self._release_rooted_pcc_lifetimes((current_root,))
            self._emit_virtual_thread_rc_check(rc, "virtual thread channel recv")
            parked = self.builder.icmp_signed(
                "==",
                rc,
                ir.Constant(_I64, 0),
                name=self._fresh("vthread.channel.recv.parked"),
            )
            park_bb = self.current_function.append_basic_block(
                name=self._fresh("vthread.channel.recv.park")
            )
            result_bb = self.current_function.append_basic_block(
                name=self._fresh("vthread.channel.recv.result.block")
            )
            self.builder.cbranch(parked, park_bb, result_bb)
            self.builder.position_at_end(park_bb)
            self._emit_generator_yield_value(self._emit_none_literal())
            if not self._builder_block_is_terminated():
                self.builder.branch(result_bb)
            self.builder.position_at_end(result_bb)
            resumed = self.builder.call(
                self.runtime["py_virtual_thread_current"],
                [],
                name=self._fresh("vthread.channel.recv.resumed"),
            )
            result = self.builder.call(
                self.runtime["py_virtual_thread_channel_recv_result"],
                [resumed],
                name=self._fresh("vthread.channel.recv.result"),
            )
            self._gc_release(resumed)
            self._emit_post_call_err_check(call_expr.span)
            return result
        if kind == "pcc.virtual_thread.close_sender":
            if len(args) != 1:
                return None
            sender_obj = self._emit_as_object(args[0])
            rc = self.builder.call(
                self.runtime["py_virtual_thread_channel_close_sender"],
                [sender_obj],
                name=self._fresh("vthread.channel.sender.close.rc"),
            )
            self._release_virtual_thread_argument(sender_obj, args[0])
            self._emit_virtual_thread_rc_check(
                rc,
                "virtual thread channel sender close",
            )
            return self.builder.icmp_signed(
                "!=",
                rc,
                ir.Constant(_I64, 0),
                name=self._fresh("vthread.channel.sender.close.changed"),
            )
        if kind == "pcc.virtual_thread.close_receiver":
            if len(args) != 1:
                return None
            receiver_obj = self._emit_as_object(args[0])
            rc = self.builder.call(
                self.runtime["py_virtual_thread_channel_close_receiver"],
                [receiver_obj],
                name=self._fresh("vthread.channel.receiver.close.rc"),
            )
            self._release_virtual_thread_argument(receiver_obj, args[0])
            self._emit_virtual_thread_rc_check(
                rc,
                "virtual thread channel receiver close",
            )
            return self.builder.icmp_signed(
                "!=",
                rc,
                ir.Constant(_I64, 0),
                name=self._fresh("vthread.channel.receiver.close.changed"),
            )
        if kind == "pcc.virtual_thread.select2":
            if (
                len(args) != 2
                or len(self._generator_ctx_stack) == 0
                or call_expr is None
            ):
                return None
            current = self.builder.call(
                self.runtime["py_virtual_thread_current"],
                [],
                name=self._fresh("vthread.channel.select2.current"),
            )
            current_root = self._enter_virtual_thread_operand_root(
                current,
                None,
                "vthread.channel.select2.current",
            )
            left_obj = self._emit_expr_with_cpy_operand_cleanup(
                args[0],
                (),
                as_object=True,
                rooted_pcc_lifetimes=(current_root,),
            )
            left_root = self._enter_virtual_thread_operand_root(
                left_obj,
                args[0],
                "vthread.channel.select2.left",
            )
            right_obj = self._emit_expr_with_cpy_operand_cleanup(
                args[1],
                (),
                as_object=True,
                rooted_pcc_lifetimes=(current_root, left_root),
            )
            rc = self.builder.call(
                self.runtime["py_virtual_thread_channel_select2_begin"],
                [
                    self._load_virtual_thread_operand_root(current_root),
                    self._load_virtual_thread_operand_root(left_root),
                    right_obj,
                ],
                name=self._fresh("vthread.channel.select2.begin.rc"),
            )
            self._release_virtual_thread_argument(right_obj, args[1])
            self._release_rooted_pcc_lifetimes((current_root, left_root))
            self._emit_virtual_thread_rc_check(
                rc,
                "virtual thread channel select2",
            )
            parked = self.builder.icmp_signed(
                "==",
                rc,
                ir.Constant(_I64, 0),
                name=self._fresh("vthread.channel.select2.parked"),
            )
            park_bb = self.current_function.append_basic_block(
                name=self._fresh("vthread.channel.select2.park")
            )
            result_bb = self.current_function.append_basic_block(
                name=self._fresh("vthread.channel.select2.result.block")
            )
            self.builder.cbranch(parked, park_bb, result_bb)
            self.builder.position_at_end(park_bb)
            self._emit_generator_yield_value(self._emit_none_literal())
            if not self._builder_block_is_terminated():
                self.builder.branch(result_bb)
            self.builder.position_at_end(result_bb)
            resumed = self.builder.call(
                self.runtime["py_virtual_thread_current"],
                [],
                name=self._fresh("vthread.channel.select2.resumed"),
            )
            result = self.builder.call(
                self.runtime["py_virtual_thread_channel_select2_result"],
                [resumed],
                name=self._fresh("vthread.channel.select2.result"),
            )
            self._gc_release(resumed)
            self._emit_post_call_err_check(call_expr.span)
            return result
        if kind == "pcc.virtual_thread.join":
            if len(args) != 1 or len(self._generator_ctx_stack) == 0:
                return None
            current = self.builder.call(
                self.runtime["py_virtual_thread_current"],
                [],
                name=self._fresh("vthread.join.current"),
            )
            current_root = self._enter_virtual_thread_operand_root(
                current,
                None,
                "vthread.join.current",
            )
            target_obj = self._emit_expr_with_cpy_operand_cleanup(
                args[0],
                (),
                as_object=True,
                rooted_pcc_lifetimes=(current_root,),
            )
            rc = self.builder.call(
                self.runtime["py_virtual_thread_join"],
                [
                    self._load_virtual_thread_operand_root(current_root),
                    target_obj,
                ],
                name=self._fresh("vthread.join.rc"),
            )
            self._release_virtual_thread_argument(target_obj, args[0])
            self._release_rooted_pcc_lifetimes((current_root,))
            self._emit_virtual_thread_rc_check(rc, "virtual thread join")
            parked = self.builder.icmp_signed(
                "==",
                rc,
                ir.Constant(_I64, 0),
                name=self._fresh("vthread.join.parked"),
            )
            park_bb = self.current_function.append_basic_block(
                name=self._fresh("vthread.join.park")
            )
            result_bb = self.current_function.append_basic_block(
                name=self._fresh("vthread.join.result.block")
            )
            self.builder.cbranch(parked, park_bb, result_bb)
            self.builder.position_at_end(park_bb)
            self._emit_generator_yield_value(self._emit_none_literal())
            if not self._builder_block_is_terminated():
                self.builder.branch(result_bb)
            self.builder.position_at_end(result_bb)
            resumed = self.builder.call(
                self.runtime["py_virtual_thread_current"],
                [],
                name=self._fresh("vthread.join.resumed"),
            )
            result = self.builder.call(
                self.runtime["py_virtual_thread_join_result"],
                [resumed],
                name=self._fresh("vthread.join.result"),
            )
            self._gc_release(resumed)
            self._emit_post_call_err_check(args[0].span)
            return result
        if kind == "pcc.virtual_thread.cancel":
            if len(args) != 1:
                return None
            target_obj = self._emit_as_object(args[0])
            rc = self.builder.call(
                self.runtime["py_virtual_thread_cancel"],
                [target_obj],
                name=self._fresh("vthread.cancel.rc"),
            )
            self._release_virtual_thread_argument(target_obj, args[0])
            self._emit_virtual_thread_rc_check(rc, "virtual thread cancel")
            return self.builder.icmp_signed(
                "!=",
                rc,
                ir.Constant(_I64, 0),
                name=self._fresh("vthread.cancel.accepted"),
            )
        if kind == "pcc.virtual_thread.run":
            if len(args) != 2:
                return None
            return self.builder.call(
                self.runtime["py_virtual_thread_run_carrier_pool"],
                [self._emit_expr_as_i64(args[0]), self._emit_expr_as_i64(args[1])],
                name=self._fresh("vthread.run"),
            )
        if kind == "pcc.virtual_thread.run_until_idle":
            if len(args) != 1:
                return None
            return self.builder.call(
                self.runtime["py_virtual_thread_run_until_idle"],
                [self._emit_expr_as_i64(args[0])],
                name=self._fresh("vthread.run_until_idle"),
            )
        if kind == "pcc.virtual_thread.carrier_pool_start":
            if len(args) != 1:
                return None
            return self.builder.call(
                self.runtime["py_virtual_thread_carrier_pool_start"],
                [self._emit_expr_as_i64(args[0])],
                name=self._fresh("vthread.pool.start"),
            )
        if kind == "pcc.virtual_thread.carrier_pool_stop":
            if args:
                return None
            return self.builder.call(
                self.runtime["py_virtual_thread_carrier_pool_stop"],
                [],
                name=self._fresh("vthread.pool.stop"),
            )
        if kind == "pcc.virtual_thread.io_backend":
            if args:
                return None
            return self.builder.call(
                self.runtime["py_virtual_thread_io_backend"],
                [],
                name=self._fresh("vthread.io.backend"),
            )
        if kind == "pcc.virtual_thread.current":
            if args:
                return None
            return self.builder.call(
                self.runtime["py_virtual_thread_current"],
                [],
                name=self._fresh("vthread.current"),
            )
        if kind == "pcc.virtual_thread.yield_now":
            if args:
                return None
            if len(self._generator_ctx_stack) > 0:
                self._emit_generator_yield_value(self._emit_none_literal())
            return self._emit_none_literal()
        if kind == "pcc.virtual_thread.sleep_current":
            if len(args) != 1:
                return None
            current = self.builder.call(
                self.runtime["py_virtual_thread_current"],
                [],
                name=self._fresh("vthread.sleep.current"),
            )
            current_root = self._enter_virtual_thread_operand_root(
                current,
                None,
                "vthread.sleep.current",
            )
            delay_ms = self._emit_expr_with_cpy_operand_cleanup(
                args[0],
                (),
                as_i64=True,
                rooted_pcc_lifetimes=(current_root,),
            )
            rc = self.builder.call(
                self.runtime["py_virtual_thread_sleep"],
                [
                    self._load_virtual_thread_operand_root(current_root),
                    delay_ms,
                ],
                name=self._fresh("vthread.sleep.current.rc"),
            )
            self._release_rooted_pcc_lifetimes((current_root,))
            self._emit_virtual_thread_rc_check(rc, "virtual thread sleep")
            if len(self._generator_ctx_stack) > 0:
                self._emit_generator_yield_value(self._emit_none_literal())
            return self._emit_none_literal()
        if kind == "pcc.virtual_thread.result":
            if len(args) != 1:
                return None
            target_obj = self._emit_as_object(args[0])
            result = self.builder.call(
                self.runtime["py_virtual_thread_result"],
                [target_obj],
                name=self._fresh("vthread.result"),
            )
            self._release_virtual_thread_argument(target_obj, args[0])
            return result
        if kind == "pcc.virtual_thread.exception":
            if len(args) != 1:
                return None
            target_obj = self._emit_as_object(args[0])
            result = self.builder.call(
                self.runtime["py_virtual_thread_exception"],
                [target_obj],
                name=self._fresh("vthread.exception"),
            )
            self._release_virtual_thread_argument(target_obj, args[0])
            return result
        if kind == "pcc.virtual_thread.outcome":
            if len(args) != 1:
                return None
            target_obj = self._emit_as_object(args[0])
            outcome = self.builder.call(
                self.runtime["py_virtual_thread_outcome"],
                [target_obj],
                name=self._fresh("vthread.outcome"),
            )
            self._release_virtual_thread_argument(target_obj, args[0])
            return outcome
        if kind == "pcc.virtual_thread.state":
            if len(args) != 1:
                return None
            target_obj = self._emit_as_object(args[0])
            state = self.builder.call(
                self.runtime["py_virtual_thread_state"],
                [target_obj],
                name=self._fresh("vthread.state"),
            )
            self._release_virtual_thread_argument(target_obj, args[0])
            return state
        if kind == "pcc.virtual_thread.sleep":
            if len(args) != 2:
                return None
            target_obj = self._emit_as_object(args[0])
            target_root = self._enter_virtual_thread_operand_root(
                target_obj,
                args[0],
                "vthread.sleep.target",
            )
            delay_ms = self._emit_expr_with_cpy_operand_cleanup(
                args[1],
                (),
                as_i64=True,
                rooted_pcc_lifetimes=(target_root,),
            )
            raw = self.builder.call(
                self.runtime["py_virtual_thread_sleep"],
                [
                    self._load_virtual_thread_operand_root(target_root),
                    delay_ms,
                ],
                name=self._fresh("vthread.sleep.rc"),
            )
            self._release_rooted_pcc_lifetimes((target_root,))
            _ = raw
            return self._emit_none_literal()
        if kind == "pcc.virtual_thread.block_on_fd":
            if len(args) != 4:
                return None
            target_obj = self._emit_as_object(args[0])
            target_root = self._enter_virtual_thread_operand_root(
                target_obj,
                args[0],
                "vthread.block_fd.target",
            )
            fd_value = self._emit_expr_with_cpy_operand_cleanup(
                args[1],
                (),
                as_i64=True,
                rooted_pcc_lifetimes=(target_root,),
            )
            events_value = self._emit_expr_with_cpy_operand_cleanup(
                args[2],
                (),
                as_i64=True,
                rooted_pcc_lifetimes=(target_root,),
            )
            timeout_value = self._emit_expr_with_cpy_operand_cleanup(
                args[3],
                (),
                as_i64=True,
                rooted_pcc_lifetimes=(target_root,),
            )
            raw = self.builder.call(
                self.runtime["py_virtual_thread_block_on_fd"],
                [
                    self._load_virtual_thread_operand_root(target_root),
                    fd_value,
                    events_value,
                    timeout_value,
                ],
                name=self._fresh("vthread.block_fd.rc"),
            )
            self._release_rooted_pcc_lifetimes((target_root,))
            _ = raw
            return self._emit_none_literal()
        if kind == "pcc.virtual_thread.block_current_on_fd":
            if len(args) != 3:
                return None
            return self._emit_virtual_thread_current_fd_wait(
                args[0],
                args[1],
                args[2],
            )
        if kind == "pcc.virtual_thread.readable":
            if len(args) != 1:
                return None
            return self._emit_virtual_thread_current_fd_wait(
                args[0],
                1,
                -1,
            )
        if kind == "pcc.virtual_thread.writable":
            if len(args) != 1:
                return None
            return self._emit_virtual_thread_current_fd_wait(
                args[0],
                4,
                -1,
            )
        return None


__all__ = ["NativeVirtualThreadLoweringMixin", "_VTHREAD_EXPORTS"]
