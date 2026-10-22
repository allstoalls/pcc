"""Callable object materialization helpers for Layer-1 codegen."""
from __future__ import annotations

from typing import Optional

from pcc.ir.compat import ir
from pcc.runtime.py.py_abi_constants import (
    PY_TYPE_BYTEARRAY,
    PY_TYPE_BYTES,
    PY_TYPE_LIST,
    PY_TYPE_MEMORYVIEW,
    PY_TYPE_STR,
    PY_TYPE_TUPLE,
)

from pcc.frontends.python.py_ast import (
    Attr,
    BinOp,
    BoolExpr,
    BoolLit,
    Call,
    DictExpr,
    DictType,
    DynType,
    Expr,
    FloatLit,
    IfExpr,
    IntLit,
    IntType,
    ListExpr,
    Name,
    NoneLit,
    NoneType,
    RawPointerType,
    Slice,
    SourceSpan,
    StrLit,
    StrType,
    Subscript,
    TupleExpr,
    TupleType,
    Type,
    UnaryOp,
    ValueArrayType,
)
from pcc.frontends.python.codegen import marshal
from pcc.frontends.python.codegen.errors import CPythonValueRequired, L1CodegenError
from pcc.frontends.python.codegen.local_bound_lowering import check_local_bound
from pcc.frontends.python.codegen.cpy_import_state import (
    live_import_expr_binding,
    live_import_name_slot,
)
from pcc.frontends.python.codegen.runtime_abi import declare_runtime_global


_I1 = ir.IntType(1)
_I8 = ir.IntType(8)
_I32 = ir.IntType(32)
_I64 = ir.IntType(64)
_CSTR = _I8.as_pointer()


class CallObjectLoweringMixin:
    def _slot_call_published_module_ref(self, expr):
        """Locate an already-published callable without evaluating operands."""
        if isinstance(expr, Attr):
            base = expr.obj
            while isinstance(base, Attr):
                base = base.obj
            if isinstance(base, Name):
                if base.ident in self.env or base.ident in self._module_globals:
                    return None
                if base.ident in getattr(self, "_native_extension_module_env", {}):
                    return None
                alias = getattr(self, "_native_module_aliases", {}).get(base.ident)
                if alias is not None and alias in getattr(self, "_sibling_module_inits", ()):
                    return None
            export = self._native_module_expr_export_info(expr.obj, expr.name)
            if export is not None and export[1].get("kind") in ("function", "class"):
                return export[0], expr.name
            return None
        if not isinstance(expr, Name):
            return None
        name = expr.ident
        if name in self.env or name in self._module_globals:
            return None
        fn = self.functions.get(name)
        if fn is None:
            return None
        if name in self._cross_module_func_defs:
            # Compare the authoritative provider symbol, not an ambiguous
            # bare exported name. This also handles aliased and star imports.
            for module_name, exports in (self._native_module_exports or {}).items():
                for exported_name, info in exports.items():
                    if info.get("kind") != "function":
                        continue
                    owner = info.get("owning_module", module_name)
                    target = info.get("export_name", exported_name)
                    symbol = "user_" + self._module_symbol_suffix(owner) + "_" + target
                    if fn.name == symbol:
                        return owner, target
            raise L1CodegenError("imported callable has no authoritative published owner: " + name)
        if name in getattr(self, "_hoisted_capture_params", {}):
            return None
        fd = self._find_user_funcdef(name)
        for statement in self.ast_module.body:
            if statement is fd:
                return self.ast_module.name or "__main__", name
        return None

    def _emit_slot_call_module_value(self, module_name, attr_name, span, label):
        """Publish the lookup's new owner before null checks or cleanup."""
        output = self._new_slot_call_root(label)
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        self._try_err_block = self._slot_call_cleanup_block((output,), target)
        self._cpy_operand_cleanup_block = self._try_err_block
        try:
            module_ptr = self._pooled_cstr_ptr(module_name, ".call.module")
            value = self.builder.call(
                self.runtime["py_module_attr_get"],
                [module_ptr, self._attr_name_ptr(attr_name)],
                name=self._fresh(label + ".lookup"),
            )
            self._publish_slot_call_owned(output, value, label="published callable")
            current = self.builder.load(output, name=self._fresh(label + ".current"))
            self._emit_attribute_error_if_null(current, attr_name, span)
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy
        return output

    def _new_slot_call_root(self, label: str, synchronous_lexical: bool = False):
        """Register an empty owning operand slot before evaluating its value.

        Operand roots have lexical LIFO lifetimes. Their caller must install
        a ``_slot_call_cleanup_block`` before any fallible operation and leave
        them through release or take on success. Enrolling each temporary in
        function-wide ownership would duplicate its cleanup at every return,
        including returns emitted before the operand exists.

        Generator resume bodies retain the physical-slot/ownership registry:
        their operands can span suspension and need the existing frame-save
        protocol. Neither route treats a borrowed raw value as a new owner.
        """
        if getattr(self, "_freestanding_module", False):
            raise L1CodegenError("slot-call roots require the managed runtime")
        name = self._fresh(label + ".operand")
        flag = None
        generator_contexts = getattr(self, "_generator_ctx_stack", ())
        generator_resume = bool(generator_contexts) and (
            generator_contexts[-1].get("resume_function") is self.current_function
        )
        # Opt in only for audited fixed-emission regions with no generated
        # suspension or continuation entry between activation and retirement.
        # Runtime calls may still collect or reenter; the lexical frame stays
        # registered until the existing release/take/error protocol leaves it.
        lifo = not generator_resume or synchronous_lexical
        if lifo:
            slot = self._alloca_in_entry(_CSTR, name=name, init_null=True)
        else:
            from pcc.frontends.python.codegen.generator_lowering import (
                allocate_generator_operand_root,
            )
            slot = allocate_generator_operand_root(self, name)
        if lifo:
            self._emit_current_gc_frame_enter_lifo(self._gc_one_slot_frame_map(), slot)
        else:
            self.env[name] = (slot, _CSTR, DynType(name="dyn"))
            self._owned_local_names.add(name)
            self._owned_local_has_value.add(name)
            self._ensure_owned_local_gc_root(name, slot, _CSTR)
            flag = self._ensure_owned_local_flag(name, slot)
            # Empty counts as owned too: an output-slot runtime call can
            # publish an owner before reporting failure to its caller.
            self.builder.store(ir.Constant(_I1, 1), flag)
            from pcc.frontends.python.codegen.generator_lowering import (
                register_generator_operand_root,
            )
            register_generator_operand_root(self, name, slot, flag)
        if not hasattr(self, "_slot_call_root_records"):
            self._slot_call_root_records = []
        record = (slot, flag, lifo)
        record_index = len(self._slot_call_root_records)
        self._slot_call_root_records.append(record)
        if not hasattr(self, "_slot_call_root_record_index"):
            self._slot_call_root_record_index = {}
        self._slot_call_root_record_index[id(slot)] = (record_index, record)
        return slot

    def _slot_call_root_is_registered(self, slot) -> bool:
        """Whether ``slot`` is an operand root this emitter registered."""
        indexed = getattr(self, "_slot_call_root_record_index", {}).get(id(slot))
        if indexed is None:
            return False
        position, record = indexed
        records = getattr(self, "_slot_call_root_records", ())
        return position < len(records) and records[position] is record and record[0] is slot

    def _slot_call_root_record(self, slot):
        # The list remains the ownership ledger. Index its exact slot identity
        # without comparing IR values, retaining the record strongly so an id
        # cannot outlive its key. Validate the position against the ledger to
        # preserve rejection when a diagnostic replaces or edits that list.
        records = getattr(self, "_slot_call_root_records", ())
        if not hasattr(self, "_slot_call_root_record_index"):
            self._slot_call_root_record_index = {}
        indexed = self._slot_call_root_record_index.get(id(slot))
        if indexed is not None:
            position, record = indexed
            if position < len(records) and records[position] is record and record[0] is slot:
                return record
        for position, record in enumerate(records):
            if record[0] is slot:
                self._slot_call_root_record_index[id(slot)] = (position, record)
                return record
        raise L1CodegenError("slot-call root was not registered by this emitter")

    def _slot_call_result_sink(self, expr):
        for candidate, slot, _published in reversed(getattr(self, "_slot_call_result_sinks", ())):
            if candidate is expr:
                return slot
        return None

    def _slot_call_note_published(self, slot) -> None:
        _slot, flag, _lifo = self._slot_call_root_record(slot)
        if flag is not None:
            self.builder.store(ir.Constant(_I1, 1), flag)
        sinks = getattr(self, "_slot_call_result_sinks", ())
        for index, entry in enumerate(sinks):
            if entry[1] is slot:
                sinks[index] = (entry[0], entry[1], True)

    def _release_slot_call_roots(self, roots) -> None:
        for slot in reversed(roots):
            _slot, flag, lifo = self._slot_call_root_record(slot)
            if flag is not None:
                self.builder.store(ir.Constant(_I1, 0), flag)
            # Slot clearing precedes terminal decref/finalizers. No raw
            # managed pointer survives this release or another root's release.
            self.builder.call(
                self.runtime["pcc_gc_store_root"],
                [self._as_gc_ptr(slot), ir.Constant(_CSTR, None)],
            )
            if lifo:
                self._emit_gc_frame_leave_lifo_for_slot(slot)

    def _slot_call_cleanup_block(self, roots, target, leases=()):
        if not roots and not leases:
            return target
        # Share only this function's identical cleanup program. Keep all
        # identity-key owners alive in the value: IR Values do not define a
        # suitable structural equality, and bare ids can otherwise be reused.
        if not hasattr(self, "_slot_call_cleanup_function"):
            self._slot_call_cleanup_function = None
            self._slot_call_cleanup_blocks = {}
        if self._slot_call_cleanup_function is not self.current_function:
            self._slot_call_cleanup_function = self.current_function
            self._slot_call_cleanup_blocks = {}
        root_owners = []
        root_keys = []
        for slot in roots:
            record = self._slot_call_root_record(slot)
            root_owners.append((slot, record[1], bool(record[2])))
            root_keys.append((id(slot), id(record[1]), bool(record[2])))
        lease_owners = tuple(leases)
        lease_keys = tuple((id(slot), id(token)) for slot, token in lease_owners)
        entry = getattr(self, "_current_entry_block", None)
        key = (tuple(root_keys), id(target), lease_keys, id(entry))
        prior = self._slot_call_cleanup_blocks.get(key)
        if prior is not None:
            return prior[0]
        cleanup = self.current_function.append_basic_block(self._fresh("call.slot.cleanup"))
        saved = self.builder._block
        self.builder.position_at_end(cleanup)
        # Keep caller-frame retirement visible to precise stack-map analysis.
        # Only the helper's own balanced exception frame is hidden by this call.
        one_root = (len(root_owners) == 1 and not lease_owners
                    and root_owners[0][1] is None and root_owners[0][2])
        two_roots = (len(root_owners) == 2 and not lease_owners
                     and root_owners[0][1] is None and root_owners[0][2]
                     and root_owners[1][1] is None and root_owners[1][2])
        one_lease = not root_owners and len(lease_owners) == 1
        if one_root or two_roots or one_lease:
            if one_root:
                slot = root_owners[0][0]
                self.builder.call(
                    self.runtime["py_cleanup_one_root_preserving_exception"],
                    [self._as_gc_ptr(slot)],
                )
                self._emit_gc_frame_leave_lifo_for_slot(slot)
            elif two_roots:
                first, second = root_owners[0][0], root_owners[1][0]
                # One exception scope spans both releases. Chaining single-root
                # helpers would hide the first callback's error from the next.
                self.builder.call(
                    self.runtime["py_cleanup_two_roots_preserving_exception"],
                    [self._as_gc_ptr(first), self._as_gc_ptr(second)],
                )
                self._emit_gc_frame_leave_lifo_for_slot(second)
                self._emit_gc_frame_leave_lifo_for_slot(first)
            else:
                slot, token = lease_owners[0]
                self.builder.call(
                    self.runtime["py_cleanup_one_lease_preserving_exception"],
                    [self._as_gc_ptr(slot), token],
                )
            self.builder.branch(target)
            self.builder.position_at_end(saved)
            self._slot_call_cleanup_blocks[key] = (
                cleanup, tuple(root_owners), target, lease_owners, entry,
            )
            return cleanup
        exception_slot = None
        swap_exception = None
        if roots or leases:
            # Register an empty cleanup-local owner before touching TLS. The
            # swap transfers its owned reference under the graph lock; no
            # borrowed exception pointer crosses registration or disposal.
            # This error edge cannot suspend, including in a generator resume.
            exception_slot = self._alloca_in_entry(
                _CSTR, name=self._fresh("call.slot.exception"), init_null=True,
            )
            self._emit_current_gc_frame_enter_lifo(
                self._gc_one_slot_frame_map(), exception_slot,
            )
            swap_exception = self.module.globals.get("py_tls_exc_swap_slot")
            if swap_exception is None:
                swap_exception = ir.Function(
                    self.module, ir.FunctionType(ir.VoidType(), [_CSTR]),
                    name="py_tls_exc_swap_slot",
                )
            self.builder.call(swap_exception, [self._as_gc_ptr(exception_slot)])
        for slot, token in reversed(leases):
            # Cleanup must preserve the exception which selected this edge.
            self.builder.call(
                self.runtime["pcc_gc_foreign_lease_release"],
                [self._as_gc_ptr(slot), token],
            )
        # The exception frame is above the operand frames. Clear their owners
        # now, but retain every frame until the exception frame has left.
        # Reentrant disposal sees only registered, authoritative slots, and a
        # generator's function-owned flag is cleared before its value drops.
        for slot in reversed(roots):
            _slot, flag, _lifo = self._slot_call_root_record(slot)
            if flag is not None:
                self.builder.store(ir.Constant(_I1, 0), flag)
            self.builder.call(
                self.runtime["pcc_gc_store_root"],
                [self._as_gc_ptr(slot), ir.Constant(_CSTR, None)],
            )
        if exception_slot is not None:
            # Weakref callbacks can clear TLS; finalizers can replace it. Drop
            # those errors before returning the original owner to TLS. The
            # second swap leaves this slot empty, so retiring its frame cannot
            # decref the restored exception or run another finalizer.
            self.builder.call(self.runtime["py_clear_exception"], [])
            self.builder.call(swap_exception, [self._as_gc_ptr(exception_slot)])
            self._emit_gc_frame_leave_lifo_for_slot(exception_slot)
        # All operand slots are empty, so these strict reverse-order frame
        # leaves cannot invoke another finalizer or clobber restored TLS.
        for slot in reversed(roots):
            _slot, _flag, lifo = self._slot_call_root_record(slot)
            if lifo:
                self._emit_gc_frame_leave_lifo_for_slot(slot)
        self.builder.branch(target)
        self.builder.position_at_end(saved)
        # Publish only a completed block. A failed build cannot leave a
        # partially emitted cleanup available to a later caller.
        self._slot_call_cleanup_blocks[key] = (
            cleanup, tuple(root_owners), target, lease_owners, entry,
        )
        return cleanup

    def _slot_call_status_report_helper(self) -> ir.Function:
        # Keep this private generated function in the existing module-owned
        # declaration map. A new generation replaces that map together with
        # its IR module; no native host-layout field or runtime ABI is added.
        key = "__pcc_slot_call_status_report"
        existing = self.runtime.get(key)
        if existing is not None:
            if existing.module is not self.module:
                raise L1CodegenError("slot-status reporter belongs to another module")
            return existing
        name = self._fresh("_pcc_slot_call_status_report")
        while name in self.module.globals:
            name = self._fresh("_pcc_slot_call_status_report")
        helper: ir.Function = ir.Function(
            self.module,
            ir.FunctionType(ir.VoidType(), [_CSTR, _CSTR, _CSTR, _CSTR, _I32, _I1]),
            name=name,
        )
        helper.linkage = "internal"
        # Both ordinary owned inlining modes honor this attribute. Without
        # it, later optimization could recreate the per-site cold expansion.
        helper.attributes.add("noinline")
        message: ir.Value = helper.args[0]
        function_name: ir.Value = helper.args[1]
        filename: ir.Value = helper.args[2]
        source_line: ir.Value = helper.args[3]
        line: ir.Value = helper.args[4]
        has_frame: ir.Value = helper.args[5]
        entry = helper.append_basic_block("entry")
        report = helper.append_basic_block("report")
        frame = helper.append_basic_block("frame")
        done = helper.append_basic_block("done")
        saved_builder: ir.IRBuilder = self.builder
        self.builder = ir.IRBuilder(entry)
        try:
            pending = self.builder.call(self.runtime["py_err_occurred"], [])
            self.builder.cbranch(
                self.builder.icmp_signed("!=", pending, ir.Constant(_I64, 0)),
                done, report,
            )
            self.builder.position_at_end(report)
            exception = self.builder.call(
                self.runtime["py_exc_new"], [ir.Constant(_I64, 7), message],
            )
            # Preserve the original borrowed py_raise protocol and call order.
            # py_runtime_error_if_unset uses py_raise_owned and adds its own
            # runtime traceback frame, so it is not an equivalent substitute.
            self.builder.call(self.runtime["py_raise"], [exception])
            current = self.builder.call(self.runtime["py_current_exception"], [])
            self.builder.cbranch(has_frame, frame, done)
            self.builder.position_at_end(frame)
            self.builder.call(
                self.runtime["py_exc_append_frame_source"],
                [current, function_name, filename, source_line, line],
            )
            self.builder.branch(done)
            self.builder.position_at_end(done)
            self.builder.ret_void()
        finally:
            self.builder = saved_builder
        # Only publish a complete helper. Its parameters are static C strings
        # and fixed scalars. Caller roots stay registered across this call;
        # no managed owner or lease is transferred to the helper.
        self.runtime[key] = helper
        return helper

    def _slot_call_check_status(self, status, operation: str, span=None) -> None:
        failed = self.builder.icmp_signed("<", status, ir.Constant(_I64, 0))
        error = self.current_function.append_basic_block(self._fresh("call.slot.error"))
        ready = self.current_function.append_basic_block(self._fresh("call.slot.ready"))
        self.builder.cbranch(failed, error, ready)
        self.builder.position_at_end(error)
        target = self._current_try_err_block()
        if target is None:
            target = self._ensure_fn_err_exit()
        helper: ir.Function = self._slot_call_status_report_helper()
        message = self._pooled_cstr_ptr("slot-call " + operation + " failed", ".exc.msg")
        function_name = ir.Constant(_CSTR, None)
        filename = ir.Constant(_CSTR, None)
        source_line = ir.Constant(_CSTR, None)
        line = ir.Constant(_I32, 0)
        if span is not None:
            name = "<module>" if self.current_func_def is None else self.current_func_def.name
            function_name = self._pooled_cstr_ptr(name, ".tb.func")
            filename = self._pooled_cstr_ptr(span.file or "<unknown>", ".tb.file")
            source_line = self._pooled_cstr_ptr(self._traceback_source_text(span), ".tb.source")
            line = ir.Constant(_I32, int(span.line))
        # Only the already-cold status failure edge calls the reporter. It
        # returns with the selected TLS exception intact; each site keeps its
        # exact cleanup successor and its existing registered root/lease state.
        self.builder.call(
            helper,
            [message, function_name, filename, source_line, line,
             ir.Constant(_I1, 1 if span is not None else 0)],
        )
        self.builder.branch(target)
        self.builder.position_at_end(ready)

    def _slot_call_copy_source(self, destination, source, borrowed=False, span=None) -> None:
        helper = "pcc_gc_root_copy_borrowed_lease" if borrowed else "pcc_gc_root_copy_lease"
        token = self.builder.call(
            self.runtime[helper], [self._as_gc_ptr(destination), self._as_gc_ptr(source)],
            name=self._fresh("call.slot.copy.lease"),
        )
        self._slot_call_check_status(token, "operand copy", span)
        # The binder needs an independently owned authoritative slot, not a
        # raw address between operand evaluations. Retire the transfer lease
        # now; the binder will acquire its own counted address lease.
        released = self.builder.call(
            self.runtime["pcc_gc_foreign_lease_release"],
            [self._as_gc_ptr(destination), token],
            name=self._fresh("call.slot.copy.release"),
        )
        self._slot_call_check_status(released, "operand lease release", span)

    def _slot_call_name_source(self, expr):
        """Return an existing native object slot, never a newly rooted load."""
        check_local_bound(self, expr)
        if live_import_name_slot(self, expr.ident) is not None:
            return None
        entry = self.env.get(expr.ident)
        if entry is not None:
            slot, ir_ty, declared_ty = entry
            if getattr(self, "_cpy_env_flags", {}).get(expr.ident, False):
                raise CPythonValueRequired(
                    "slot-call CPython name requires an output-slot bridge: " + expr.ident
                )
            if isinstance(declared_ty, RawPointerType):
                raise L1CodegenError("raw pointer cannot be a slot-call operand: " + expr.ident)
            if not self._ir_type_matches(ir_ty, _CSTR):
                return None
            # Globals may also be installed in env inside a global statement.
            global_entry = self._module_globals.get(expr.ident)
            if global_entry is not None and global_entry[0] is slot:
                if self._module_global_needs_bound_check(expr.ident):
                    self._emit_module_global_bound_check(expr.ident, expr)
                if getattr(self, "_cpy_module_flags", {}).get(expr.ident, False):
                    raise CPythonValueRequired("slot-call CPython global requires an output-slot bridge: " + expr.ident)
                return slot, False
            registry = getattr(self, "_fn_gc_root_slot_registry", {}).get(self.current_function.name, ())
            if not any(item[1] is slot for item in registry):
                if self._slot_call_root_is_registered(slot):
                    # A name bound to an owning operand root (an inlined
                    # lambda's parameter) reads that root while it is bound.
                    return slot, False
                raise L1CodegenError("slot-call name has no authoritative registered root: " + expr.ident)
            # Rebinding/loop promotion can replace a parameter's slot while
            # its lexical borrowed-name marker remains. Ownership belongs to
            # this physical slot; its matching owned flag outranks that marker.
            borrowed = (
                expr.ident in getattr(self, "_borrowed_gc_rooted_local_names", ())
                and self._owned_local_flag_for(expr.ident, slot) is None
            )
            return slot, borrowed
        if expr.ident == "__class__" and self.current_class is not None:
            # The class cell can still be empty; the Name lowering checks it.
            return None
        entry = self._module_globals.get(expr.ident)
        if entry is not None:
            slot, declared_ty = entry
            if getattr(self, "_cpy_module_flags", {}).get(expr.ident, False):
                raise CPythonValueRequired("slot-call CPython global requires an output-slot bridge: " + expr.ident)
            if isinstance(declared_ty, RawPointerType):
                raise L1CodegenError("raw pointer cannot be a slot-call operand: " + expr.ident)
            if self._module_global_needs_bound_check(expr.ident):
                self._emit_module_global_bound_check(expr.ident, expr)
            if self._module_global_needs_teardown(slot, declared_ty):
                return slot, False
            return None
        classes = getattr(getattr(self, "class_lowering", None), "classes", {})
        if expr.ident in classes:
            return classes[expr.ident].global_var, False
        return None

    def _publish_slot_call_owned(self, slot, value, *, label="owned result") -> None:
        """Immediately transfer a fresh result into an already empty root.

        An earlier SSA result returned after cleanup is not fresh. Reject it
        rather than registering a possibly stale pointer after a safepoint.
        Output-slot producers should instead write directly or root_move.
        """
        self._slot_call_root_record(slot)
        if not isinstance(value.type, ir.PointerType):
            raise L1CodegenError("slot-call publication requires an object: " + label)
        record = getattr(value, "_instr", value)
        instructions = self.builder._block._instrs
        immediate = bool(instructions) and instructions[-1] is record
        if instructions and not immediate:
            last = instructions[-1]
            # Most owned-builder calls/loads do not set Value._instr. Match
            # the actual destination, never merely the last opcode: a later
            # cleanup call must not make an older raw result look fresh.
            immediate = self._record_defines_value(last, value)
        immortal = isinstance(value, (ir.Constant, ir.GlobalVariable)) or self._value_is_never_gc_object(value)
        if not immediate and not immortal:
            raise L1CodegenError(
                "slot-call " + label + " lacks an immediate owned-result handoff; "
                "its producer must publish into the output slot before parking cleanup"
            )
        if value.type != _CSTR:
            value = self.builder.bitcast(value, _CSTR, name=self._fresh("call.slot.value"))
        # No runtime call, pin, registration, header probe or lock acquisition
        # may be inserted between a producer's return and this first store.
        self.builder.store(value, slot)
        self._slot_call_note_published(slot)
        token = self.builder.call(
            self.runtime["pcc_gc_foreign_lease_acquire"], [self._as_gc_ptr(slot)],
            name=self._fresh("call.slot.publish.lease"),
        )
        self._slot_call_check_status(token, "owned-result lease")
        current = self.builder.load(slot, name=self._fresh("call.slot.published"))
        self.builder.call(
            self.runtime["pcc_gc_note_slot_write_barrier"],
            [ir.Constant(_CSTR, None), self._as_gc_ptr(slot), current],
        )
        released = self.builder.call(
            self.runtime["pcc_gc_foreign_lease_release"], [self._as_gc_ptr(slot), token],
            name=self._fresh("call.slot.publish.release"),
        )
        self._slot_call_check_status(released, "owned-result lease release")

    def _slot_call_builtin_power_expr(self, expr):
        """Recognize only an unshadowed two-argument builtin power call."""
        if (not isinstance(expr, Call) or not isinstance(expr.func, Name)
                or len(expr.args) != 2 or expr.kwargs
                or self._has_unpacked_operands(expr.args, expr.kwargs)):
            return None
        ident = expr.func.ident
        if (ident in self.env or ident in self._module_globals
                or ident in self.functions
                or ident in getattr(getattr(self, "class_lowering", None), "classes", {})
                or live_import_name_slot(self, ident) is not None):
            return None
        builtin = self._native_builtin_value_for_name(ident)
        if builtin != "builtins.pow":
            # Plain pow is not in the builtin *type* value-name table.
            # Accept its unbound spelling, never an imported/assigned alias.
            if (ident != "pow" or builtin is not None
                    or ident in self._native_builtin_value_aliases):
                return None
        # Reuse the exact numeric producer, with authoritative input roots and
        # immediate NEW-result publication. A negative exponent stays boxed.
        return BinOp(span=expr.span, ty=expr.ty, op="**",
                     lhs=expr.args[0], rhs=expr.args[1])

    def _emit_slot_call_operand(self, expr: Expr, label: str):
        """Evaluate one operand into an independent owning authoritative root."""
        if self._expr_returns_unsafe_raw_pointer(expr):
            raise L1CodegenError(
                "raw pointer cannot be a slot-call operand: " + type(expr).__name__
                + " in " + str(self.current_function.name)
                + " at " + expr.span.file + ":" + str(expr.span.line)
            )
        # Resume-body operand roots are enrolled in the owning heap frame by
        # _new_slot_call_root, including results created while evaluating a
        # suspending expression. They are restored before resume dispatch.
        if isinstance(expr, IfExpr):
            return self._emit_slot_call_conditional(expr, label)
        class_value = self._emit_class_namespace_name_root(expr, label)
        if class_value is not None:
            return class_value
        adapter_value = self._emit_lambda_adapter_name_root(expr, label)
        if adapter_value is not None:
            return adapter_value
        if isinstance(expr, BoolExpr):
            return self._emit_slot_call_short_circuit(expr, label)[0]
        published = self._slot_call_published_module_ref(expr)
        if published is not None:
            return self._emit_slot_call_module_value(published[0], published[1], expr.span, label)
        payload_value = self._emit_slot_call_valueclass_name(expr, label)
        if payload_value is not None:
            return payload_value
        if isinstance(expr, (UnaryOp, BinOp)):
            if self._slot_call_literal_integer_kind(expr):
                return self._emit_slot_call_literal_integer(expr, label)
            if isinstance(expr, UnaryOp):
                runtime_unary = self._slot_call_unary_runtime(expr)
                if runtime_unary is not None:
                    return self._emit_slot_call_unary(expr, label, runtime_unary)
            if isinstance(expr, BinOp):
                runtime_binary = self._slot_call_binary_runtime(expr, object_boundary=True)
                if runtime_binary is not None:
                    return self._emit_slot_call_binary(expr, label, runtime_binary)
            machine_integer = isinstance(expr.ty, IntType) and expr.ty.name != "int"
            if (not machine_integer and (isinstance(expr.ty, IntType)
                    or isinstance(expr, UnaryOp) and expr.op in ("+", "-", "~"))):
                raise L1CodegenError(
                    "slot-call arithmetic requires a proven literal-derived integer tree; "
                    "annotations and callback results are not value-kind proof"
                )
            # An explicit machine lane retains its pre-existing lowering and
            # ownership boundary; it never enters the exact-int producer.
        if isinstance(expr, (TupleExpr, ListExpr)):
            return self._emit_slot_call_sequence(expr.elems, label, isinstance(expr, TupleExpr))
        if isinstance(expr, DictExpr):
            return self._emit_slot_call_dict(expr.pairs, expr.span, label)
        if isinstance(expr, Attr):
            return self._emit_slot_call_attribute(expr, label)
        if (isinstance(expr, Subscript)
                and not self._expr_looks_cpython(expr.obj)
                and not isinstance(expr.obj.ty, ValueArrayType)
                and not self._is_valueclass_payload_type(expr.obj.ty)
                and self._native_module_name_for_object_expr(expr) is None):
            return self._emit_slot_call_subscript(expr, label)
        if (isinstance(expr, Call) and isinstance(expr.func, Name)
                and self._native_builtin_value_for_name(expr.func.ident) == "builtins.int"
                and expr.func.ident not in self.functions
                and expr.func.ident not in getattr(getattr(self, "class_lowering", None), "classes", {})
                and len(expr.args) in (1, 2) and not expr.kwargs
                and not self._has_unpacked_operands(expr.args, expr.kwargs)
                and not (isinstance(expr.ty, IntType) and expr.ty.name != "int")):
            return self._emit_slot_call_int_constructor(expr, label)
        power = self._slot_call_builtin_power_expr(expr)
        if power is not None:
            runtime_power = self._slot_call_binary_runtime(power, object_boundary=True)
            if runtime_power is not None:
                return self._emit_slot_call_binary(power, label, runtime_power)
        if isinstance(expr, Call) and expr.is_set_literal:
            return self._emit_slot_call_set(expr, label)
        if (isinstance(expr, Call) and isinstance(expr.func, Name)
                and expr.func.ident == "set" and len(expr.args) <= 1 and not expr.kwargs
                and "set" not in self.env and "set" not in self._module_globals
                and "set" not in self.functions
                and "set" not in getattr(getattr(self, "class_lowering", None), "classes", {})
                and not self._has_unpacked_operands(expr.args, expr.kwargs)):
            return self._emit_slot_call_set(expr, label)
        slot = self._new_slot_call_root(label)
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        self._try_err_block = self._slot_call_cleanup_block((slot,), target)
        self._cpy_operand_cleanup_block = self._try_err_block
        try:
            source = self._slot_call_name_source(expr) if isinstance(expr, Name) else None
            if source is not None:
                self._slot_call_copy_source(slot, source[0], source[1], expr.span)
            else:
                scalar = isinstance(expr, (BoolLit, FloatLit, IntLit, NoneLit))
                if isinstance(expr, Name):
                    entry = self.env.get(expr.ident)
                    if entry is not None:
                        scalar = not isinstance(entry[1], ir.PointerType)
                    else:
                        entry = self._module_globals.get(expr.ident)
                        if entry is not None:
                            scalar = not isinstance(entry[0].value_type, ir.PointerType)
                if not hasattr(self, "_slot_call_result_sinks"):
                    self._slot_call_result_sinks = []
                self._slot_call_result_sinks.append((expr, slot, False))
                try:
                    value = self._emit_call_arg_object(expr)
                finally:
                    _candidate, _output, published = self._slot_call_result_sinks.pop()
                if published:
                    return slot
                owned = self._owned_release_needed(value, expr)
                if not owned:
                    # A namespace Name can be produced by the same registered
                    # static string pool as a StrLit. Prove that exact producer,
                    # then retain a separate owner before publishing it. Never
                    # infer this from a name's spelling or an arbitrary global:
                    # shadowed names took the authoritative slot-copy path.
                    for literal in self._str_obj_pool.values():
                        if value is literal:
                            self.builder.call(
                                self.runtime["py_incref"], [self._as_gc_ptr(value)],
                            )
                            owned = True
                            break
                if not owned and not scalar and not self._value_is_never_gc_object(value):
                    raise L1CodegenError(
                        "slot-call operand has no authoritative source or owned result: "
                        + type(expr).__name__
                    )
                self._publish_slot_call_owned(slot, value, label=type(expr).__name__)
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy
        return slot

    def _emit_slot_call_int_constructor(self, expr, label):
        """Convert to an integer object without passing through a scalar lane.

        Runtime conversion owns type dispatch and returns a new reference,
        including the identity case. An omitted base is NULL; an explicit
        None remains an ordinary argument and must raise in the runtime.
        """
        output = self._new_slot_call_root(label)
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        roots = [output]
        try:
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            arguments = []
            for argument in expr.args:
                item = self._emit_slot_call_operand(argument, label + ".int.argument")
                arguments.append(item)
                roots.append(item)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
            omitted_base = (ir.Constant(_CSTR, None),) if len(arguments) == 1 else ()
            self._slot_call_runtime_call(
                "py_obj_as_int_object_args", tuple(arguments), result_slot=output,
                suffix_args=omitted_base, span=expr.span,
            )
            self._release_slot_call_roots(tuple(roots[1:]))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy
        return output

    def _emit_owned_text_conversion(self, expr, runtime_name):
        """Publish a unary text conversion through the shared NEW-result ABI."""
        return self._emit_owned_unary_runtime_call(expr, runtime_name)

    def _emit_owned_object_constructor(self, expr):
        """Allocate an instance of canonical object, publishing both NEW owners."""
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        sink = self._slot_call_result_sink(expr)
        output = sink
        roots = []
        if output is None:
            output = self._new_slot_call_root("object.result")
            roots.append(output)
        try:
            cls = self._new_slot_call_root("object.class")
            roots.append(cls)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            self._slot_call_runtime_call(
                "py_builtin_type_for_tag", (), result_slot=cls,
                suffix_args=(ir.Constant(_I64, -1),), span=expr.span,
            )
            self._slot_call_runtime_call(
                "py_instance_new", (cls,), result_slot=output, span=expr.span,
            )
            self._release_slot_call_roots((cls,))
            if sink is None:
                return self._take_slot_call_root(output)
            return self.builder.load(output, name=self._fresh("object.current"))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy

    def _emit_owned_unary_runtime_call(self, expr, runtime_name):
        """Keep one operand owned and publish the runtime's actual NEW result."""
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        sink = self._slot_call_result_sink(expr)
        output = sink
        roots = []
        if output is None:
            output = self._new_slot_call_root(runtime_name + ".result")
            roots.append(output)
        try:
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            # A zero-argument method supplies its receiver as the unary ABI
            # operand. Keep the original Call for its output-slot sink.
            argument_expr = (
                expr.func.obj
                if isinstance(expr.func, Attr) and not expr.args
                else expr.args[0]
            )
            argument = self._emit_slot_call_operand(argument_expr, runtime_name + ".argument")
            roots.append(argument)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            self._slot_call_runtime_call(
                runtime_name, (argument,), result_slot=output, span=expr.span,
            )
            self._release_slot_call_roots((argument,))
            if sink is None:
                return self._take_slot_call_root(output)
            return self.builder.load(output, name=self._fresh("unary.result.current"))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy

    def _emit_owned_operand_runtime_call(self, expr, runtime_name, arity, operand_exprs=None):
        """Own ``arity`` operands left to right and publish the runtime's NEW result.

        The operands are ``expr``'s first ``arity`` arguments, or the given
        ``operand_exprs`` (a method's receiver). A protocol callback
        may return any operand; that result is the runtime's own new
        reference, independent of the operand owners.
        """
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        sink = self._slot_call_result_sink(expr)
        output = sink
        roots = []
        if output is None:
            output = self._new_slot_call_root(runtime_name + ".result")
            roots.append(output)
        try:
            operands = []
            sources = tuple(operand_exprs) if operand_exprs is not None else expr.args[:arity]
            for index, argument_expr in enumerate(sources):
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                operand = self._emit_slot_call_operand(
                    argument_expr, runtime_name + (".left", ".right", ".third")[index],
                )
                roots.append(operand)
                operands.append(operand)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            self._slot_call_runtime_call(
                runtime_name, tuple(operands), result_slot=output, span=expr.span,
            )
            self._release_slot_call_roots(tuple(operands))
            if sink is None:
                return self._take_slot_call_root(output)
            return self.builder.load(output, name=self._fresh("binary.result.current"))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy

    def _emit_owned_descriptor_constructor(self, expr, runtime_name, arity):
        """Retain accessors and publish the NEW descriptor before cleanup."""
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        saved_preference = self._prefer_native_callable_values
        sink = self._slot_call_result_sink(expr)
        output = sink
        roots = []
        if output is None:
            output = self._new_slot_call_root(runtime_name + ".result")
            roots.append(output)
        operands = []
        try:
            self._prefer_native_callable_values = True
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            for argument_expr in expr.args:
                argument = self._emit_slot_call_operand(
                    argument_expr, runtime_name + ".accessor",
                )
                operands.append(argument)
                roots.append(argument)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
            # A NULL optional property accessor means absent. Explicit None
            # remains an ordinary owned operand understood by the runtime.
            omitted = tuple(ir.Constant(_CSTR, None) for _ in range(arity - len(operands)))
            self._slot_call_runtime_call(
                runtime_name, tuple(operands), result_slot=output,
                suffix_args=omitted, span=expr.span,
            )
            self._release_slot_call_roots(tuple(operands))
            if sink is None:
                return self._take_slot_call_root(output)
            return self.builder.load(output, name=self._fresh("descriptor.current"))
        finally:
            self._prefer_native_callable_values = saved_preference
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy

    def _emit_owned_format_call(self, expr):
        """Evaluate value then spec into owners and publish the runtime result."""
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        sink = self._slot_call_result_sink(expr)
        output = sink
        roots = []
        if output is None:
            output = self._new_slot_call_root("format.result")
            roots.append(output)
        try:
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            spec_expr = (expr.args[1] if len(expr.args) == 2
                         else StrLit(span=expr.span, ty=StrType(name="str"), value=""))
            operands = []
            for argument, label in ((expr.args[0], "format.value"), (spec_expr, "format.spec")):
                if self._expr_looks_cpython(argument):
                    # Preserve the selected foreign expression route. Its
                    # native projection is the bridge's NEW owner, never the
                    # raw CPython pointer or an annotation-based ownership
                    # guess. Keep earlier operands alive while this runs.
                    item = self._new_slot_call_root(label)
                    roots.append(item)
                    self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                    self._cpy_operand_cleanup_block = self._try_err_block
                    if not hasattr(self, "_slot_call_result_sinks"):
                        self._slot_call_result_sinks = []
                    self._slot_call_result_sinks.append((argument, item, False))
                    try:
                        result = self._emit_call_arg_object(argument)
                    finally:
                        _argument, _slot, published = self._slot_call_result_sinks.pop()
                    if not published:
                        # The lookahead is advisory; a native producer may
                        # still win. Demand its existing ownership proof and
                        # immediate handoff rather than evaluating it again.
                        if (not self._owned_release_needed(result, argument)
                                and not self._value_is_never_gc_object(result)):
                            raise L1CodegenError("format operand has no authoritative owned result")
                        self._publish_slot_call_owned(item, result, label="format operand")
                else:
                    item = self._emit_slot_call_operand(argument, label)
                    roots.append(item)
                operands.append(item)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
            value, spec = operands
            self._slot_call_runtime_call("py_builtin_format", (value, spec), result_slot=output, span=expr.span)
            self._release_slot_call_roots((value, spec))
            if sink is None:
                return self._take_slot_call_root(output)
            return self.builder.load(output, name=self._fresh("format.current"))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy

    def _emit_owned_hasattr_call(self, expr):
        """hasattr(object, name) over owned operands: an i1 result.

        The runtime validates the name type and clears only AttributeError;
        every other exception from a property or __getattr__ propagates.
        """
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        roots = []
        try:
            for argument, label in ((expr.args[0], "hasattr.object"), (expr.args[1], "hasattr.name")):
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                roots.append(self._emit_slot_call_operand(argument, label))
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            status = self._slot_call_runtime_call("py_builtin_hasattr", tuple(roots), span=expr.span)
            self._release_slot_call_roots(tuple(roots))
            return self.builder.icmp_signed(
                "==", status, ir.Constant(_I64, 1), name=self._fresh("hasattr.result"),
            )
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy

    def _emit_slot_call_conditional(self, expr, label):
        """Evaluate one selected branch into a shared authoritative owner.

        A pointer phi does not establish ownership and must never be rooted
        after another operand has run. Each branch instead uses the normal
        operand producer, transfers its owner into the pre-registered result,
        and retires its temporary before joining. Unselected branches have
        no runtime evaluations, registrations, or releases.
        """
        known = self._static_bool_condition(expr.cond)
        if known is not None:
            return self._emit_slot_call_operand(expr.then_e if known else expr.else_e, label)
        output = self._new_slot_call_root(label + ".conditional")
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        self._try_err_block = self._slot_call_cleanup_block((output,), target)
        self._cpy_operand_cleanup_block = self._try_err_block
        try:
            condition = self._emit_condition_value(expr.cond)
            yes = self.current_function.append_basic_block(self._fresh("call.slot.if.true"))
            no = self.current_function.append_basic_block(self._fresh("call.slot.if.false"))
            done = self.current_function.append_basic_block(self._fresh("call.slot.if.done"))
            self.builder.cbranch(condition, yes, no)
            for block, branch in ((yes, expr.then_e), (no, expr.else_e)):
                self.builder.position_at_end(block)
                self._try_err_block = self._slot_call_cleanup_block((output,), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                value = self._emit_slot_call_operand(branch, label + ".branch")
                self._try_err_block = self._slot_call_cleanup_block((output, value), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                moved = self.builder.call(
                    self.runtime["pcc_gc_root_move"],
                    [self._as_gc_ptr(output), self._as_gc_ptr(value)],
                    name=self._fresh("call.slot.if.move"),
                )
                self._slot_call_check_status(moved, "conditional result move", expr.span)
                self._release_slot_call_roots((value,))
                self.builder.branch(done)
            self.builder.position_at_end(done)
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy
        return output

    def _emit_slot_call_short_circuit(self, expr, label, need_truth=False):
        """Move the selected operand owner, preserving identity and order."""
        if expr.op not in ("and", "or"):
            raise L1CodegenError("unsupported slot-call boolean operation: " + expr.op)
        known = self._static_bool_condition(expr.left)
        if known is not None and ((expr.op == "and" and not known)
                                  or (expr.op == "or" and known)):
            # The left operand is the result. Preserve its actual owner and
            # do not lower a statically unreachable RHS, which may contain
            # an unsupported operation that Python would never evaluate.
            left = self._emit_slot_call_operand(expr.left, label + ".left")
            return left, ir.Constant(_I1, known) if need_truth else None
        output = self._new_slot_call_root(label + ".boolean")
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        try:
            self._try_err_block = self._slot_call_cleanup_block((output,), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            if isinstance(expr.left, BoolExpr):
                # Reuse the branch truth which selected this inner value.
                # Re-testing it would invoke __bool__ twice in mixed chains.
                left, condition = self._emit_slot_call_short_circuit(
                    expr.left, label + ".left", True,
                )
            else:
                left = self._emit_slot_call_operand(expr.left, label + ".left")
                self._try_err_block = self._slot_call_cleanup_block((output, left), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                truth = self._slot_call_runtime_call("py_obj_truthy", (left,), span=expr.left.span)
                condition = self.builder.icmp_signed("!=", truth, ir.Constant(_I64, 0))
            self._try_err_block = self._slot_call_cleanup_block((output, left), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            short = self.current_function.append_basic_block(self._fresh("call.slot.bool.short"))
            rhs = self.current_function.append_basic_block(self._fresh("call.slot.bool.rhs"))
            done = self.current_function.append_basic_block(self._fresh("call.slot.bool.done"))
            if expr.op == "and":
                self.builder.cbranch(condition, rhs, short)
            else:
                self.builder.cbranch(condition, short, rhs)
            self.builder.position_at_end(short)
            moved = self.builder.call(
                self.runtime["pcc_gc_root_move"],
                [self._as_gc_ptr(output), self._as_gc_ptr(left)],
                name=self._fresh("call.slot.bool.move"),
            )
            self._slot_call_check_status(moved, "boolean left owner move", expr.span)
            self._release_slot_call_roots((left,))
            short_exit = self.builder.block
            self.builder.branch(done)
            self.builder.position_at_end(rhs)
            self._release_slot_call_roots((left,))
            self._try_err_block = self._slot_call_cleanup_block((output,), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            right_truth = None
            if isinstance(expr.right, BoolExpr) and need_truth:
                right, right_truth = self._emit_slot_call_short_circuit(
                    expr.right, label + ".right", True,
                )
            else:
                right = self._emit_slot_call_operand(expr.right, label + ".right")
            self._try_err_block = self._slot_call_cleanup_block((output, right), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            if need_truth and right_truth is None:
                truth = self._slot_call_runtime_call("py_obj_truthy", (right,), span=expr.right.span)
                right_truth = self.builder.icmp_signed("!=", truth, ir.Constant(_I64, 0))
            moved = self.builder.call(
                self.runtime["pcc_gc_root_move"],
                [self._as_gc_ptr(output), self._as_gc_ptr(right)],
                name=self._fresh("call.slot.bool.move"),
            )
            self._slot_call_check_status(moved, "boolean right owner move", expr.span)
            self._release_slot_call_roots((right,))
            right_exit = self.builder.block
            self.builder.branch(done)
            self.builder.position_at_end(done)
            result_truth = None
            if need_truth:
                result_truth = self.builder.phi(_I1, name=self._fresh("call.slot.bool.truth"))
                result_truth.add_incoming(ir.Constant(_I1, expr.op == "or"), short_exit)
                result_truth.add_incoming(right_truth, right_exit)
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy
        return output, result_truth

    def _slot_call_literal_integer_kind(self, expr):
        """Return 1 for a proven int tree, 2 for a bool literal, else 0.

        IntType describes an annotation or representation, not an exact
        runtime class. Only literal provenance admits the primitive kernels.
        Do not evaluate the tree on the compiler host: shifts and powers can
        describe arbitrarily large values. Bool-only bitwise trees and powers
        without a literal nonnegative exponent remain explicit boundaries.
        """
        if isinstance(expr.ty, IntType) and expr.ty.name != "int":
            return 0
        if isinstance(expr, BoolLit):
            return 2
        if isinstance(expr, IntLit):
            return 1
        if isinstance(expr, UnaryOp) and expr.op in ("+", "-", "~"):
            operand_kind = self._slot_call_literal_integer_kind(expr.operand)
            if expr.op == "~" and operand_kind == 2:
                # Python 3.15 warns for ~bool. This bounded producer has no
                # warning dispatch, so do not silently normalize that case.
                return 0
            return 1 if operand_kind else 0
        if not isinstance(expr, BinOp):
            return 0
        if expr.op not in ("+", "-", "*", "//", "%", "**", "&", "|", "^", "<<", ">>"):
            return 0
        left = self._slot_call_literal_integer_kind(expr.lhs)
        right = self._slot_call_literal_integer_kind(expr.rhs)
        if not left or not right:
            return 0
        if expr.op in ("&", "|", "^") and left == 2 and right == 2:
            # Python returns bool here; py_int_* returns int. Reject the
            # complete containing tree instead of trusting inferred IntType.
            return 0
        if expr.op == "**":
            if not isinstance(expr.rhs, (IntLit, BoolLit)) or expr.rhs.value < 0:
                # A negative exponent can produce a float even though the
                # frontend inferred IntType. Never feed it to an int kernel.
                return 0
        return 1

    def _emit_slot_call_literal_integer(self, expr, label):
        """Publish each exact integer owner before checks or operand release."""
        if not self._slot_call_literal_integer_kind(expr):
            raise L1CodegenError("slot-call integer producer requires literal provenance")
        output = self._new_slot_call_root(label)
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        roots = [output]
        try:
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            if isinstance(expr, (IntLit, BoolLit)):
                if isinstance(expr, BoolLit):
                    # Some primitive kernels inspect integer layout or
                    # return the operand for an identity operation. Only
                    # arithmetic leaves are normalized; bare bool operands
                    # retain their ordinary singleton publication path.
                    value = self._emit_int_literal_object(1 if expr.value else 0)
                else:
                    value = self._emit_int_literal_object(expr.value)
                self._publish_slot_call_owned(output, value, label="integer literal")
            else:
                first_expr = expr.operand if isinstance(expr, UnaryOp) else expr.lhs
                first = self._emit_slot_call_literal_integer(first_expr, label + ".left")
                roots.append(first)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                identity = (isinstance(expr, UnaryOp) and expr.op == "+"
                            and self._slot_call_literal_integer_kind(first_expr) == 1)
                if identity:
                    self._slot_call_copy_source(output, first, span=expr.span)
                else:
                    arguments = [first]
                    if isinstance(expr, UnaryOp):
                        runtime_name = "py_int_neg"
                        if expr.op in ("+", "~"):
                            # +bool produces int; ~int uses a rooted -1,
                            # avoiding the legacy generic unary helper's
                            # result-across-decref interval.
                            second_expr = IntLit(span=expr.span, ty=IntType(name="int"),
                                                 value=0 if expr.op == "+" else -1)
                            runtime_name = "py_int_add" if expr.op == "+" else "py_int_xor"
                        else:
                            second_expr = None
                    else:
                        second_expr = expr.rhs
                        runtime_name = {
                            "+": "py_int_add", "-": "py_int_sub", "*": "py_int_mul",
                            "//": "py_int_floordiv", "%": "py_int_mod", "**": "py_int_pow",
                            "&": "py_int_and", "|": "py_int_or", "^": "py_int_xor",
                            "<<": "py_int_shl", ">>": "py_int_shr",
                        }[expr.op]
                    if second_expr is not None:
                        second = self._emit_slot_call_literal_integer(second_expr, label + ".right")
                        roots.append(second)
                        arguments.append(second)
                        self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                        self._cpy_operand_cleanup_block = self._try_err_block
                    if isinstance(expr, BinOp) and expr.op in ("<<", ">>"):
                        zero_expr = IntLit(span=expr.span, ty=IntType(name="int"), value=0)
                        zero = self._emit_slot_call_literal_integer(zero_expr, label + ".zero")
                        roots.append(zero)
                        self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                        self._cpy_operand_cleanup_block = self._try_err_block
                        sign = self._slot_call_runtime_call("py_int_cmp", (second, zero), span=expr.span)
                        sign = self.builder.sext(sign, _I64, name=self._fresh("call.slot.shift.sign"))
                        self._emit_negative_shift_count_check(sign)
                    self._slot_call_runtime_call(
                        runtime_name, tuple(arguments), result_slot=output, span=expr.span,
                    )
            # Runtime calls have already published and checked pending errors.
            # Inspect only a fresh root load here, never their earlier SSA.
            current = self.builder.load(output, name=self._fresh("call.slot.integer.result"))
            if isinstance(expr, BinOp) and expr.op in ("//", "%"):
                self._emit_zero_division_if_null(current, "division by zero")
            else:
                self._guard_cpy_value_not_null(current)
            self._release_slot_call_roots(tuple(roots[1:]))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy
        return output

    def _take_slot_call_root(self, slot, keep_pinned=False):
        """Finish every parking operation before returning the root's owner.

        A tagged int or NULL is no managed object: it needs no graph lock,
        read barrier or pin, so that word skips them. Both words and objects
        then leave the root through the same pinned transfer, which keeps the
        result's root provenance single for precise stack-map planning.
        """
        _slot, flag, lifo = self._slot_call_root_record(slot)
        raw = self.builder.load(slot, name=self._fresh("call.slot.take.raw"))
        raw_bits = self.builder.ptrtoint(raw, _I64, name=self._fresh("call.slot.take.bits"))
        tagged = self.builder.icmp_unsigned(
            "!=", self.builder.and_(raw_bits, ir.Constant(_I64, 1)), ir.Constant(_I64, 0),
            name=self._fresh("call.slot.take.tagged"),
        )
        empty = self.builder.icmp_unsigned(
            "==", raw_bits, ir.Constant(_I64, 0), name=self._fresh("call.slot.take.empty"),
        )
        immediate = self.builder.or_(tagged, empty, name=self._fresh("call.slot.take.immediate"))
        word_block = self.current_function.append_basic_block(self._fresh("call.slot.take.word"))
        object_block = self.current_function.append_basic_block(self._fresh("call.slot.take.object"))
        done_block = self.current_function.append_basic_block(self._fresh("call.slot.take.done"))
        self.builder.cbranch(immediate, word_block, object_block)
        self.builder.position_at_end(object_block)
        self.builder.call(self.runtime["pcc_py_gc_minor_graph_lock"], [])
        current = self.builder.call(
            self.runtime["pcc_gc_load_ptr"],
            [ir.Constant(_CSTR, None), self._as_gc_ptr(slot)],
            name=self._fresh("call.slot.take.current"),
        )
        prior = ir.Constant(_I64, 64) if keep_pinned else self._extern_prior_pin(current)
        self._gc_pin(current)
        self.builder.call(self.runtime["pcc_py_gc_minor_graph_unlock"], [])
        object_end = self.builder.block
        self.builder.branch(done_block)
        self.builder.position_at_end(word_block)
        self.builder.branch(done_block)
        self.builder.position_at_end(done_block)
        prior_pin = self.builder.phi(_I64, name=self._fresh("call.slot.take.prior"))
        prior_pin.add_incoming(prior, object_end)
        prior_pin.add_incoming(ir.Constant(_I64, 64) if keep_pinned else ir.Constant(_I64, 0), word_block)
        if lifo:
            self._emit_gc_frame_leave_lifo_for_slot(slot)
        if flag is not None:
            self.builder.store(ir.Constant(_I1, 0), flag)
        value = self.builder.call(
            self.runtime["pcc_gc_take_pinned_slot"], [self._as_gc_ptr(slot), prior_pin],
            name=self._fresh("call.slot.take"),
        )
        self._note_owned_object_value(value)
        return value

    def _slot_call_runtime_call(
        self, runtime_name, roots, *, result_slot=None, suffix_args=(), argument_order=(), span=None,
        exception_slot=None,
    ):
        """Expose raw operands only while independent counted leases live.

        Every input is an existing owning root. Results go directly to an
        empty output root before any lease release, error check, or cleanup.
        The scalar token/status values are the only SSA values crossing waits.
        """
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        leases = []
        result = None
        try:
            for slot in roots:
                self._try_err_block = self._slot_call_cleanup_block((), target, tuple(leases))
                self._cpy_operand_cleanup_block = self._try_err_block
                token = self.builder.call(
                    self.runtime["pcc_gc_foreign_lease_acquire"], [self._as_gc_ptr(slot)],
                    name=self._fresh("call.slot.argument.lease"),
                )
                self._slot_call_check_status(token, "argument lease", span)
                leases.append((slot, token))
            self._try_err_block = self._slot_call_cleanup_block((), target, tuple(leases))
            self._cpy_operand_cleanup_block = self._try_err_block
            values = [self.builder.load(slot, name=self._fresh("call.slot.argument")) for slot in roots]
            arguments = values + list(suffix_args)
            if argument_order:
                arguments = [arguments[index] for index in argument_order]
            result = self.builder.call(
                self.runtime[runtime_name], arguments,
                name=self._fresh("call.slot.runtime") if result_slot is not None else "",
            )
            if result_slot is not None:
                self._publish_slot_call_owned(result_slot, result, label=runtime_name)
            elif exception_slot is None:
                # Scalar results have no publication step. Check TLS before
                # retiring a lease, and let the cleanup edge preserve the
                # exact error while releasing every remaining operand lease.
                self._emit_post_call_err_check(span)
            if exception_slot is not None:
                # Iteration protocols inspect StopIteration themselves. Move
                # the exact TLS owner out before retiring operands can run a
                # finalizer and replace or clear that pending exception.
                swap_exception = self.module.globals.get("py_tls_exc_swap_slot")
                if swap_exception is None:
                    swap_exception = ir.Function(
                        self.module, ir.FunctionType(ir.VoidType(), [_CSTR]),
                        name="py_tls_exc_swap_slot",
                    )
                self.builder.call(swap_exception, [self._as_gc_ptr(exception_slot)])
                self._slot_call_note_published(exception_slot)
            while leases:
                slot, token = leases.pop()
                released = self.builder.call(
                    self.runtime["pcc_gc_foreign_lease_release"],
                    [self._as_gc_ptr(slot), token],
                    name=self._fresh("call.slot.argument.release"),
                )
                self._try_err_block = self._slot_call_cleanup_block((), target, tuple(leases))
                self._cpy_operand_cleanup_block = self._try_err_block
                self._slot_call_check_status(released, "argument lease release", span)
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy
        if exception_slot is None:
            self._emit_post_call_err_check(span)
        return None if result_slot is not None else result

    def _emit_slot_call_sequence(self, elems, label, tuple_result):
        """Build a list/tuple directly in roots, expanding each splat in place."""
        output = self._new_slot_call_root(label)
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        roots = [output]
        try:
            if tuple_result:
                sequence = self._new_slot_call_root(label + ".list")
                roots.append(sequence)
            else:
                sequence = output
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            value = self.builder.call(
                self.runtime["py_list_new"], [ir.Constant(_I64, 0)],
                name=self._fresh("call.slot.list"),
            )
            self._publish_slot_call_owned(sequence, value, label="argument list")
            for elem in elems:
                splat = (isinstance(elem, Call) and isinstance(elem.func, Name)
                         and elem.func.ident in ("*", "__starred__") and len(elem.args) == 1)
                source = elem.args[0] if splat else elem
                item = self._emit_slot_call_operand(source, label + ".item")
                roots.append(item)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                if splat:
                    self._slot_call_runtime_call("py_call_require_star_iterable", (item,), span=source.span)
                self._slot_call_runtime_call(
                    "py_list_extend" if splat else "py_list_append",
                    (sequence, item), span=source.span,
                )
                self._release_slot_call_roots((item,))
                roots.pop()
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
            if tuple_result:
                self._slot_call_runtime_call("py_tuple_from_list", (sequence,), result_slot=output)
                self._release_slot_call_roots((sequence,))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy
        return output

    def _emit_slot_call_args_tuple(self, args, label="call.args"):
        return self._emit_slot_call_sequence(args, label, True)

    def _slot_call_deferred_star(self, args):
        """A sole * operand is converted only after the keyword merge."""
        if len(args) != 1:
            return None
        argument = args[0]
        if (isinstance(argument, Call) and isinstance(argument.func, Name)
                and argument.func.ident in ("*", "__starred__")
                and len(argument.args) == 1 and not argument.kwargs):
            return argument.args[0]
        return None

    def _finish_slot_call_deferred_star(self, args_root, span, label):
        """Replace the sole iterable owner with its argument tuple in place.

        Keeping the original physical slot lets module LIFO frames remain
        balanced while keyword roots are above it. Clear the iterable owner
        before invocation, so its finalizer runs at Python's conversion point.
        """
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        sequence = self._new_slot_call_root(label + ".list")
        output = self._new_slot_call_root(label + ".tuple")
        self._try_err_block = self._slot_call_cleanup_block((sequence, output), target)
        self._cpy_operand_cleanup_block = self._try_err_block
        try:
            value = self.builder.call(self.runtime["py_list_new"], [ir.Constant(_I64, 0)])
            self._publish_slot_call_owned(sequence, value, label="deferred positional list")
            self._slot_call_runtime_call("py_call_require_star_iterable", (args_root,), span=span)
            self._slot_call_runtime_call("py_list_extend", (sequence, args_root), span=span)
            self._slot_call_runtime_call("py_tuple_from_list", (sequence,), result_slot=output, span=span)
            self.builder.call(self.runtime["pcc_gc_store_root"],
                              [self._as_gc_ptr(args_root), ir.Constant(_CSTR, None)])
            moved = self.builder.call(self.runtime["pcc_gc_root_move"],
                                      [self._as_gc_ptr(args_root), self._as_gc_ptr(output)])
            self._slot_call_check_status(moved, "deferred positional tuple move", span)
            self._release_slot_call_roots((sequence, output))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy

    def _emit_slot_call_set(self, expr, label):
        """Root the set constructor/literal before element callbacks run."""
        output = self._new_slot_call_root(label)
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        roots = [output]
        try:
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            if expr.args and not expr.is_set_literal:
                source = self._emit_slot_call_operand(expr.args[0], label + ".iterable")
                roots.append(source)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                self._slot_call_runtime_call(
                    "py_set_from_iterable", (source,), result_slot=output, span=expr.span,
                )
                self._release_slot_call_roots((source,))
            else:
                value = self.builder.call(self.runtime["py_set_new"], [], name=self._fresh("call.slot.set"))
                self._publish_slot_call_owned(output, value, label="set literal")
                # Only syntax-marked set displays insert as they evaluate
                # elements. Explicit set(list/tuple) must first finish the
                # whole sequence, even if the first member's hash will raise.
                elems = expr.args[0].elems if expr.args else ()
                for elem in elems:
                    splat = (isinstance(elem, Call) and isinstance(elem.func, Name)
                             and elem.func.ident in ("*", "__starred__") and len(elem.args) == 1)
                    source_expr = elem.args[0] if splat else elem
                    item = self._emit_slot_call_operand(source_expr, label + ".item")
                    roots.append(item)
                    self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                    self._cpy_operand_cleanup_block = self._try_err_block
                    self._slot_call_runtime_call(
                        "py_set_update" if splat else "py_set_add", (output, item), span=expr.span,
                    )
                    self._release_slot_call_roots((item,))
                    roots.pop()
                    self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                    self._cpy_operand_cleanup_block = self._try_err_block
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy
        return output

    def _slot_call_split_operands(self, expr):
        # Hoisting carries closure operands as synthetic direct-ABI keywords.
        # A callable already owns those captures; only its source operands
        # belong to the public signature binder. Do not filter by spelling:
        # an explicit keyword matching a capture must still bind or raise.
        capture_indices = {
            index for kind, index in expr.operand_order if kind == "capture"
        }
        source_keywords = tuple(
            pair for index, pair in enumerate(expr.kwargs)
            if index not in capture_indices
        )
        source_order = tuple(
            entry for entry in expr.operand_order if entry[0] != "capture"
        )
        unpack = self._split_starstar_kwargs_unpack(expr.args)
        if unpack is None:
            return expr.args, source_keywords
        arguments, _merged = unpack
        keywords = []
        if source_order:
            for kind, index in source_order:
                if kind == "kw":
                    keywords.append(expr.kwargs[index])
                elif kind == "arg":
                    argument = expr.args[index]
                    if (isinstance(argument, Call) and isinstance(argument.func, Name)
                            and argument.func.ident == "**"):
                        keywords.append(("**", argument.args[0]))
        else:
            if source_keywords:
                raise L1CodegenError("slot-call unpack is missing keyword operand-order metadata")
            for argument in expr.args:
                if (isinstance(argument, Call) and isinstance(argument.func, Name)
                        and argument.func.ident == "**"):
                    keywords.append(("**", argument.args[0]))
        return arguments, tuple(keywords)

    def _emit_slot_call_object(self, expr, label="object.call", module_name=None, attr_name=None):
        """Native callable dispatch with authoritative inputs and output."""
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        sink = self._slot_call_result_sink(expr)
        output = sink
        roots = []
        if output is None:
            output = self._new_slot_call_root(label + ".result")
            roots.append(output)
        try:
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            if module_name is None:
                callable_root = self._emit_slot_call_operand(expr.func, label + ".callable")
            else:
                callable_root = self._emit_slot_call_module_value(
                    module_name, attr_name, expr.span, label + ".callable",
                )
            roots.append(callable_root)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            positional, keywords = self._slot_call_split_operands(expr)
            deferred_star = self._slot_call_deferred_star(positional)
            args = (self._emit_slot_call_operand(deferred_star, label + ".args")
                    if deferred_star is not None else self._emit_slot_call_args_tuple(positional, label + ".args"))
            roots.append(args)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            kwargs = self._emit_slot_call_kwargs_object(
                keywords, None, expr.span, label + ".kwargs", callable_root,
            )
            roots.append(kwargs)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            if deferred_star is not None:
                self._finish_slot_call_deferred_star(args, expr.span, label + ".star")
            status = self.builder.call(
                self.runtime["py_obj_call_slots"],
                [self._as_gc_ptr(callable_root), self._as_gc_ptr(args),
                 self._as_gc_ptr(kwargs), self._as_gc_ptr(output)],
                name=self._fresh(label + ".invoke"),
            )
            self._slot_call_note_published(output)
            self._slot_call_check_status(status, "object call", expr.span)
            self._emit_post_call_err_check(expr.span)
            self._release_slot_call_roots(tuple(roots[1:]) if sink is None else tuple(roots))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy
        if sink is not None:
            return self.builder.load(output, name=self._fresh(label + ".output"))
        return self._take_slot_call_root(output)

    def _static_import_call_abi(self, fn) -> bool:
        """Whether ``fn``'s ABI takes and returns only objects or scalars."""
        function_type = fn.function_type
        for argument_type in function_type.args:
            if not (isinstance(argument_type, (ir.PointerType, ir.DoubleType))
                    or isinstance(argument_type, ir.IntType)
                    and argument_type.width in (1, 64)):
                return False
        return_type = function_type.return_type
        return (isinstance(return_type, (ir.PointerType, ir.DoubleType, ir.VoidType))
                or isinstance(return_type, ir.IntType) and return_type.width in (1, 64))

    def _emit_slot_call_from_roots(self, callable_root, positional_roots, keyword_roots,
                                   output, held_roots, target, span, label):
        """The dynamic call of already evaluated roots, publishing ``output``."""
        roots = list(held_roots)

        def guard():
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block

        args = self._new_slot_call_root(label + ".args")
        roots.append(args)
        sequence = self._new_slot_call_root(label + ".args.list")
        roots.append(sequence)
        guard()
        value = self.builder.call(
            self.runtime["py_list_new"], [ir.Constant(_I64, 0)],
            name=self._fresh("call.slot.list"),
        )
        self._publish_slot_call_owned(sequence, value, label="argument list")
        for root in positional_roots:
            self._slot_call_runtime_call("py_list_append", (sequence, root), span=span)
        self._slot_call_runtime_call("py_tuple_from_list", (sequence,), result_slot=args)
        self._release_slot_call_roots((sequence,))
        roots.pop()
        guard()
        if keyword_roots:
            kwargs = self._emit_slot_call_dict((), span, label + ".kwargs")
            roots.append(kwargs)
            guard()
            for keyword, root in keyword_roots:
                key = self._emit_slot_call_operand(
                    StrLit(span=span, ty=StrType(name="str"), value=keyword), label + ".key",
                )
                roots.append(key)
                guard()
                self._slot_call_runtime_call("py_dict_set", (kwargs, key, root), span=span)
                self._release_slot_call_roots((key,))
                roots.pop()
                guard()
        else:
            kwargs = self._emit_slot_call_operand(NoneLit(span=span, ty=NoneType(name="None")),
                                                  label + ".kwargs")
            roots.append(kwargs)
            guard()
        status = self.builder.call(
            self.runtime["py_obj_call_slots"],
            [self._as_gc_ptr(callable_root), self._as_gc_ptr(args),
             self._as_gc_ptr(kwargs), self._as_gc_ptr(output)],
            name=self._fresh(label + ".invoke"),
        )
        self._slot_call_note_published(output)
        self._slot_call_check_status(status, "object call", span)
        self._emit_post_call_err_check(span)
        self._release_slot_call_roots((args, kwargs))

    def _emit_slot_call_static_import(self, expr, name):
        """Call a live ``from m import f`` binding directly while it is ``f``.

        Python evaluates the callee before the arguments, so the binding is
        read first and every argument is then evaluated once into its own
        root. While the binding is the provider's plain function object those
        roots feed its native body; otherwise they become the argument tuple
        and keyword dict of the ordinary dynamic call. Returns None when the
        call needs the generic binder anyway.
        """
        adapter_name = self._cross_module_static_entries.get(name)
        fn = self.functions.get(name)
        fd = self._cross_module_func_defs.get(name)
        if adapter_name is None or not isinstance(fn, ir.Function) or fd is None:
            return None
        if (not self._strict_no_libpython
                or len(getattr(self, "_generator_ctx_stack", ())) > 0
                or self._has_unpacked_operands(expr.args, expr.kwargs)
                or fd.is_async
                or isinstance(fd.return_ty, RawPointerType)
                or fn.name in self._manual_pointer_abi_functions
                or not self._static_import_call_abi(fn)
                or self._ordinary_call_needs_runtime_binding(expr, fd)):
            return None
        try:
            self._resolve_call_kwargs(expr.args, expr.kwargs, fd.args)
        except L1CodegenError:
            return None
        span = expr.span
        label = name + ".import"
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        sink = self._slot_call_result_sink(expr)
        output = sink
        roots = []
        if output is None:
            output = self._new_slot_call_root(label + ".result")
            roots.append(output)
        placeholders = []

        def bind(root):
            # The root stays authoritative; a placeholder Name reads it.
            placeholder = self._fresh(".import.operand")
            self.env[placeholder] = (root, _CSTR, DynType(name="dyn"))
            placeholders.append(placeholder)
            return Name(span=span, ty=DynType(name="dyn"), ident=placeholder)

        try:
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            callable_root = self._emit_slot_call_operand(expr.func, label + ".callable")
            roots.append(callable_root)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            positional = []
            positional_roots = []
            for argument in expr.args:
                root = self._emit_slot_call_operand(argument, label + ".arg")
                roots.append(root)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                positional.append(bind(root))
                positional_roots.append(root)
            keywords = []
            keyword_roots = []
            for keyword, argument in expr.kwargs:
                root = self._emit_slot_call_operand(argument, label + ".kwarg")
                roots.append(root)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                keywords.append((keyword, bind(root)))
                keyword_roots.append((keyword, root))
            adapter = self.module.globals.get(adapter_name)
            if not isinstance(adapter, ir.Function):
                adapter = ir.Function(
                    self.module, ir.FunctionType(_CSTR, [_CSTR, _CSTR]), name=adapter_name,
                )
            is_static = self.builder.call(
                self.runtime["py_func_slot_has_native_entry"],
                [self._as_gc_ptr(callable_root), self.builder.bitcast(adapter, _CSTR)],
                name=self._fresh(label + ".is.static"),
            )
            function = self.current_function
            direct_bb = function.append_basic_block(self._fresh(label + ".direct"))
            dynamic_bb = function.append_basic_block(self._fresh(label + ".dynamic"))
            done_bb = function.append_basic_block(self._fresh(label + ".done"))
            self.builder.cbranch(
                self.builder.icmp_signed("!=", is_static, ir.Constant(_I64, 0)),
                direct_bb, dynamic_bb,
            )
            if not hasattr(self, "_slot_call_result_sinks"):
                self._slot_call_result_sinks = []
            # Both branches publish one owner into the same output root.
            self.builder.position_at_end(direct_bb)
            direct = Call(span=span, ty=expr.ty, func=expr.func,
                          args=tuple(positional), kwargs=tuple(keywords))
            self._slot_call_result_sinks.append((direct, output, False))
            try:
                raw = self._emit_static_user_function_call(direct, name, fn)
            finally:
                _candidate, _slot, published = self._slot_call_result_sinks.pop()
            if not published:
                # Pointer results were published at the call; a scalar or void
                # result boxes from the callee's declared return type.
                if isinstance(raw.type, ir.PointerType):
                    raise L1CodegenError("direct import call left its result unpublished: " + name)
                result_ty = NoneType(name="None") if isinstance(raw.type, ir.VoidType) else fd.return_ty
                boxed = marshal.marshal_to_object(
                    self.builder, self.module, self.runtime, raw, result_ty,
                )
                self._publish_slot_call_owned(output, boxed, label="direct import result")
                self._emit_post_call_err_check(span)
            self.builder.branch(done_bb)
            self.builder.position_at_end(dynamic_bb)
            self._emit_slot_call_from_roots(
                callable_root, positional_roots, keyword_roots, output,
                tuple(roots), target, span, label,
            )
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            self.builder.branch(done_bb)
            self.builder.position_at_end(done_bb)
            self._slot_call_note_published(output)
            self._release_slot_call_roots(tuple(roots[1:]) if sink is None else tuple(roots))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy
            for placeholder in placeholders:
                self.env.pop(placeholder, None)
        if sink is not None:
            return self.builder.load(output, name=self._fresh(label + ".output"))
        return self._take_slot_call_root(output)

    def _emit_slot_call_dict(self, pairs, span, label):
        output = self._new_slot_call_root(label)
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        roots = [output]
        try:
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            value = self.builder.call(self.runtime["py_dict_new"], [], name=self._fresh("call.slot.dict"))
            self._publish_slot_call_owned(output, value, label="keyword dict")
            for key_expr, value_expr in pairs:
                if isinstance(key_expr, Name) and key_expr.ident == "**":
                    raise L1CodegenError("slot-call dict literal mapping expansion requires a mapping-only slot producer")
                key = self._emit_slot_call_operand(key_expr, label + ".key")
                roots.append(key)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                item = self._emit_slot_call_operand(value_expr, label + ".value")
                roots.append(item)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                self._slot_call_runtime_call("py_dict_set", (output, key, item), span=span)
                self._release_slot_call_roots((key, item))
                roots = [output]
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy
        return output

    def _emit_slot_call_subscript(self, expr, label):
        """Publish native object indexing before cleanup; preserve separate bridge/payload routes."""
        output = self._new_slot_call_root(label)
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        roots = [output]
        try:
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            native_environ = self._is_os_environ_attr(expr.obj)
            if not native_environ:
                receiver = self._emit_slot_call_operand(expr.obj, label + ".receiver")
                roots.append(receiver)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
            if isinstance(expr.idx, Slice):
                bounds = []
                for part in (expr.idx.lo, expr.idx.hi, expr.idx.step):
                    if part is None:
                        part = NoneLit(span=expr.idx.span, ty=NoneType(name="None"))
                    bound = self._emit_slot_call_operand(part, label + ".slice.bound")
                    bounds.append(bound)
                    roots.append(bound)
                    self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                    self._cpy_operand_cleanup_block = self._try_err_block
                key = self._new_slot_call_root(label + ".slice")
                roots.append(key)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                self._slot_call_runtime_call(
                    "py_slice_new", tuple(bounds), result_slot=key, span=expr.idx.span,
                )
            else:
                key = self._emit_slot_call_operand(expr.idx, label + ".key")
                roots.append(key)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
            if native_environ:
                self._slot_call_runtime_call(
                    "py_os_environ_getitem", (key,), result_slot=output, span=expr.span,
                )
            else:
                if isinstance(expr.idx, Slice):
                    # Built-in sequence getitem helpers expect integer keys.
                    # Select their existing slice ABI by the actual receiver
                    # tag; mappings and user instances keep the slice key and
                    # ordinary __getitem__ dispatch, including custom errors.
                    tag = self._slot_call_runtime_call(
                        "py_obj_type_tag", (receiver,), span=expr.span,
                    )
                    sequence = ir.Constant(_I1, 0)
                    for sequence_tag in (
                        PY_TYPE_LIST, PY_TYPE_TUPLE, PY_TYPE_STR,
                        PY_TYPE_BYTES, PY_TYPE_BYTEARRAY, PY_TYPE_MEMORYVIEW,
                    ):
                        sequence = self.builder.or_(sequence, self.builder.icmp_signed(
                            "==", tag, ir.Constant(_I64, sequence_tag),
                        ))
                    sliced = self.current_function.append_basic_block(self._fresh("call.slice.sequence"))
                    indexed = self.current_function.append_basic_block(self._fresh("call.slice.mapping"))
                    ready = self.current_function.append_basic_block(self._fresh("call.slice.ready"))
                    self.builder.cbranch(sequence, sliced, indexed)
                    self.builder.position_at_end(sliced)
                    self._slot_call_runtime_call(
                        "py_obj_slice", (receiver,) + tuple(bounds),
                        result_slot=output, span=expr.span,
                    )
                    self.builder.branch(ready)
                    self.builder.position_at_end(indexed)
                    self._slot_call_runtime_call(
                        "py_obj_subscript", (receiver, key),
                        result_slot=output, span=expr.span,
                    )
                    self.builder.branch(ready)
                    self.builder.position_at_end(ready)
                else:
                    self._slot_call_runtime_call(
                        "py_obj_subscript", (receiver, key), result_slot=output, span=expr.span,
                    )
            self._release_slot_call_roots(tuple(roots[1:]))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy
        return output

    def _emit_slot_call_attribute(self, expr, label):
        runtime_name = expr.name
        if live_import_expr_binding(self, expr.obj):
            runtime_name = self.class_lowering.private_field_key(runtime_name)
        uname = self._emit_slot_call_os_uname_attr(expr, label)
        if uname is not None:
            return uname
        namespace = self._emit_slot_call_namespace_attribute(expr, label)
        if namespace is not None:
            return namespace
        projected = self._emit_slot_call_valueclass_attribute(expr, label)
        if projected is not None:
            return projected
        if self._is_valueclass_payload_type(expr.obj.ty):
            raise L1CodegenError("slot-call valueclass attribute requires a payload-slot producer")
        output = self._new_slot_call_root(label)
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        try:
            self._try_err_block = self._slot_call_cleanup_block((output,), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            receiver = self._emit_slot_call_operand(expr.obj, label + ".receiver")
            self._try_err_block = self._slot_call_cleanup_block((output, receiver), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            self._slot_call_runtime_call(
                "py_obj_getattr", (receiver,), result_slot=output,
                suffix_args=(self._attr_name_ptr(runtime_name),), span=expr.span,
            )
            self._release_slot_call_roots((receiver,))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy
        return output

    def _emit_slot_call_kwargs_object(self, kwargs, kwargs_expr, span, label="call.kwargs", callable_root=None):
        self._reject_kwargs_merge_with_explicit_keywords(kwargs_expr, kwargs)
        if not kwargs and kwargs_expr is None:
            return self._emit_slot_call_operand(NoneLit(span=span, ty=NoneType(name="None")), label)
        operands = []
        pairs = []
        for name, expr in kwargs:
            if name == "**":
                if pairs:
                    operands.append(("explicit", tuple(pairs)))
                    pairs = []
                operands.append(("mapping", expr))
            else:
                pairs.append((StrLit(span=expr.span, ty=StrType(name="str"), value=name), expr))
        if pairs:
            operands.append(("explicit", tuple(pairs)))
        if kwargs_expr is not None:
            if self._is_kwargs_merge(kwargs_expr):
                for expr in kwargs_expr.args:
                    operands.append(("mapping", expr))
            else:
                operands.append(("mapping", kwargs_expr))
        output = self._emit_slot_call_dict((), span, label)
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        try:
            for kind, operand in operands:
                self._try_err_block = self._slot_call_cleanup_block((output,), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                source = (self._emit_slot_call_dict(operand, span, label + ".explicit")
                          if kind == "explicit" else self._emit_slot_call_operand(operand, label + ".mapping"))
                merged = self._new_slot_call_root(label + ".merged")
                self._try_err_block = self._slot_call_cleanup_block((output, source, merged), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                if callable_root is None:
                    self._slot_call_runtime_call(
                        "py_call_merge_kwargs_unique", (output, source), result_slot=merged, span=span,
                    )
                else:
                    self._slot_call_runtime_call(
                        "py_call_merge_kwargs_for_call", (output, source, callable_root),
                        result_slot=merged, span=span,
                    )
                # Preserve LIFO module-root order: move the completed merge
                # back into the oldest root before retiring either temporary.
                self.builder.call(self.runtime["pcc_gc_store_root"],
                                  [self._as_gc_ptr(output), ir.Constant(_CSTR, None)])
                moved = self.builder.call(self.runtime["pcc_gc_root_move"],
                                          [self._as_gc_ptr(output), self._as_gc_ptr(merged)])
                self._slot_call_check_status(moved, "keyword merge move", span)
                self._release_slot_call_roots((source, merged))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy
        return output

    def _emit_call_arg_object(self, arg: Expr) -> ir.Value:
        valueclass_payload = self._maybe_emit_valueclass_constructor_payload(
            arg.ty,
            arg,
        )
        if valueclass_payload is not None:
            boxed_valueclass = self._emit_valueclass_payload_to_object(
                valueclass_payload,
                arg.ty,
                result_slot=self._slot_call_result_sink(arg),
            )
            if boxed_valueclass is not None:
                return boxed_valueclass

        raw = self._emit_expr(arg)
        if raw in getattr(self, "_cpy_values", ()):
            return self._emit_value_as_pcc_object_or_bridge(
                raw,
                arg.ty,
                "call.arg.bridge",
                result_slot=self._slot_call_result_sink(arg),
            )

        boxed_valueclass = self._emit_valueclass_payload_to_object(raw, arg.ty, result_slot=self._slot_call_result_sink(arg))
        if boxed_valueclass is not None:
            return boxed_valueclass

        boxed = marshal.marshal_to_object(
            self.builder,
            self.module,
            self.runtime,
            raw,
            arg.ty,
        )
        output = self._slot_call_result_sink(arg)
        if output is not None and isinstance(
            raw.type, (ir.IntType, ir.FloatType, ir.DoubleType, ir.VoidType)
        ):
            # A real void ABI result marshals to the immortal None singleton.
            # The actual scalar-to-object producer establishes this owner.
            # Pointer pass-through and annotations alone provide no such
            # evidence and retain the caller's strict provenance checks.
            self._publish_slot_call_owned(output, boxed, label="scalar boxing")
            if isinstance(arg, Call):
                self._emit_post_call_err_check(arg.span)
        return boxed

    def _emit_call_args_tuple(self, args: tuple[Expr, ...]) -> ir.Value:
        # Argument tuples have the same retaining slot contract as ordinary
        # tuple literals. Reuse their marshalling, temporary-owner release,
        # moving-GC protection and cleanup when a later argument raises.
        return self._emit_tuple_literal(TupleExpr(
            span=args[0].span if args else None,
            ty=TupleType(name="tuple", elems=tuple(arg.ty for arg in args)),
            elems=args,
        ))

    def _emit_dynamic_call_args_tuple(self, args: tuple[Expr, ...]) -> ir.Value:
        """Return one owned tuple for the native callable argument ABI."""
        if self._is_starred_unpack(args):
            source = args[0].args[0]
            seq = self._emit_as_object(source)
            tag = self.builder.call(self.runtime["py_obj_type_tag"], [seq])
            exact_tuple = self.builder.icmp_signed("==", tag, ir.Constant(_I64, PY_TYPE_TUPLE))
            function = self.current_function
            reuse_bb = function.append_basic_block(self._fresh("call.splat.tuple"))
            convert_bb = function.append_basic_block(self._fresh("call.splat.iterable"))
            done_bb = function.append_basic_block(self._fresh("call.splat.done"))
            self.builder.cbranch(exact_tuple, reuse_bb, convert_bb)
            self.builder.position_at_end(reuse_bb)
            retained = self._gc_retain(seq)
            self._gc_release_if_owned(seq, source)
            reuse_exit = self.builder.block
            self.builder.branch(done_bb)
            self.builder.position_at_end(convert_bb)
            self._gc_pin(seq)
            converted = self.builder.call(
                self.runtime["py_tuple_from_splat"], [seq],
                name=self._fresh("call.splat.normalized"),
            )
            self._gc_pin(converted)
            self._gc_unpin(seq)
            self._gc_release_if_owned(seq, source)
            self._gc_unpin(converted)
            self._emit_post_call_err_check(source.span)
            convert_exit = self.builder.block
            self.builder.branch(done_bb)
            self.builder.position_at_end(done_bb)
            result = self.builder.phi(_CSTR, name=self._fresh("call.splat.args"))
            result.add_incoming(retained, reuse_exit)
            result.add_incoming(converted, convert_exit)
            return result
        if self._has_starred_unpack(args):
            lst = self._emit_pcc_args_list(args, "dyn")
            tup = self.builder.call(
                self.runtime["py_tuple_from_list"],
                [lst],
                name=self._fresh("call.args.splat.tuple"),
            )
            self._gc_release(lst)
            return tup
        return self._emit_call_args_tuple(args)

    def _emit_dynamic_call_kwargs_object(
        self,
        kwargs: tuple[tuple[str, Expr], ...],
        kwargs_expr: Optional[Expr],
        span: SourceSpan,
    ) -> ir.Value:
        # Keep explicit-keyword runs together: Python evaluates a run's
        # values before merging it with a preceding **mapping. Every merge
        # rejects duplicate/non-string keys and preserves source order.
        self._reject_kwargs_merge_with_explicit_keywords(kwargs_expr, kwargs)
        if kwargs_expr is None and not kwargs:
            none_gv = declare_runtime_global(self.module, "py_None")
            return self.builder.load(none_gv, name=self._fresh("none"))
        operands = []
        pairs = []
        for kw_name, kw_expr in kwargs:
            if kw_name == "**":
                if pairs:
                    operands.append(("explicit", tuple(pairs)))
                    pairs = []
                operands.append(("mapping", kw_expr))
            else:
                pairs.append((
                    StrLit(span=kw_expr.span, ty=StrType(name="str"), value=kw_name),
                    kw_expr,
                ))
        if pairs:
            operands.append(("explicit", tuple(pairs)))
        if kwargs_expr is not None:
            if self._is_kwargs_merge(kwargs_expr):
                for mapping_expr in kwargs_expr.args:
                    operands.append(("mapping", mapping_expr))
            else:
                operands.append(("mapping", kwargs_expr))

        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy_error = self._cpy_operand_cleanup_block
        roots = []
        accumulator = None
        try:
            for kind, operand in operands:
                self._try_err_block = self._extern_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                if kind == "explicit":
                    value = self._emit_dynamic_call_kwargs_dict_literal(operand, span)
                    owned = True
                else:
                    value = self._emit_as_object(operand)
                    owned = self._owned_release_needed(value, operand)
                source_root = self._extern_enter_root(value, owned, "call.kwargs.source")
                roots.append(source_root)
                self._try_err_block = self._extern_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                if accumulator is None and kind == "explicit":
                    accumulator = source_root
                    continue
                base = (
                    ir.Constant(_CSTR, None) if accumulator is None
                    else self._extern_load_root(accumulator)
                )
                merged = self.builder.call(
                    self.runtime["py_call_merge_kwargs_unique"],
                    [base, self._extern_load_root(source_root)],
                    name=self._fresh("call.kwargs.merge.unique"),
                )
                self._emit_post_call_err_check(span)
                merged_root = self._extern_enter_root(merged, True, "call.kwargs.merged")
                # Merging produces a distinct dict. Protect it while retiring
                # both the previous accumulator and the temporary mapping;
                # either release can invoke a finalizer and collect.
                self._extern_release_roots(tuple(roots))
                roots = [merged_root]
                accumulator = merged_root
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy_error
        result = self._extern_take_root(accumulator)
        self._note_owned_object_value(result)
        return result

    def _emit_dynamic_call_kwargs_dict_literal(
        self,
        pairs: tuple[tuple[Expr, Expr], ...],
        span: SourceSpan,
    ) -> ir.Value:
        return self._emit_dict_literal(
            DictExpr(
                span=span,
                ty=DictType(
                    name="dict",
                    key=StrType(name="str"),
                    value=DynType(name="dyn"),
                ),
                pairs=pairs,
            )
        )

    def _emit_object_tuple_from_values(
        self,
        values: tuple[tuple[ir.Value, Type], ...],
        *,
        name: str,
    ) -> ir.Value:
        tup = self.builder.call(
            self.runtime["py_tuple_new"],
            [ir.Constant(_I64, len(values))],
            name=self._fresh(name),
        )
        for i, (raw, ty) in enumerate(values):
            obj = marshal.marshal_to_object(
                self.builder,
                self.module,
                self.runtime,
                raw,
                ty,
            )
            self.builder.call(
                self.runtime["py_tuple_set_item"],
                [tup, ir.Constant(_I64, i), obj],
            )
        return tup

    def _emit_empty_tuple(self, name: str) -> ir.Value:
        return self.builder.call(
            self.runtime["py_tuple_new"],
            [ir.Constant(_I64, 0)],
            name=self._fresh(name),
        )
