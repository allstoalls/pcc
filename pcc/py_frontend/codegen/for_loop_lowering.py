"""For-loop lowering helpers for L1CodeGen."""

from __future__ import annotations

from typing import Optional

from pcc.llvm_capi.compat import ir

from ..py_ast import (
    Assign,
    Attr,
    BoolLit,
    BoolType,
    Break,
    Call,
    DictType,
    DynType,
    Expr,
    For,
    If,
    IntType,
    ListType,
    Name,
    SetType,
    StrType,
    Try,
    TupleExpr,
    TupleType,
    Type,
    While,
)
from . import marshal
from .builtin_exceptions import BUILTIN_EXC_TAG as _BUILTIN_EXC_TAG
from .errors import L1CodegenError

# AST-bearing fields walked by the cpy-for cross-yield read scan
# (audited against the full py_ast dataclass field inventory).
_CPY_SCAN_FIELDS = (
    "args",
    "bases",
    "body",
    "cause",
    "cond",
    "decorators",
    "default",
    "elems",
    "else_body",
    "else_e",
    "exc",
    "expr",
    "fields",
    "finally_body",
    "func",
    "handlers",
    "hi",
    "idx",
    "items",
    "iter",
    "key",
    "keywords",
    "kwargs",
    "left",
    "lhs",
    "lo",
    "obj",
    "operand",
    "pairs",
    "params",
    "right",
    "rhs",
    "step",
    "target",
    "targets",
    "then_e",
    "value",
)


_I1 = ir.IntType(1)
_I8 = ir.IntType(8)
_I32 = ir.IntType(32)
_I64 = ir.IntType(64)
_CSTR = _I8.as_pointer()


def _for_loop_has_attr(obj, name: str) -> bool:
    return hasattr(obj, name)


def _for_loop_type_name(obj) -> str:
    try:
        return str(obj.ty.name)
    except AttributeError:
        return ""


def _for_loop_is_name(obj) -> bool:
    return isinstance(obj, Name) or _for_loop_has_attr(obj, "ident")


def _for_loop_is_tuple_expr(obj) -> bool:
    if isinstance(obj, TupleExpr):
        return True
    ty_name = _for_loop_type_name(obj)
    return _for_loop_has_attr(obj, "elems") and (
        ty_name == "tuple" or ty_name == "tuple_variadic"
    )


def _for_loop_is_call(obj) -> bool:
    return isinstance(obj, Call) or (
        _for_loop_has_attr(obj, "func")
        and _for_loop_has_attr(obj, "args")
        and _for_loop_has_attr(obj, "kwargs")
    )


def _for_loop_is_call_name(obj, names: tuple[str, ...]) -> bool:
    if not _for_loop_is_call(obj):
        return False
    try:
        return _for_loop_is_name(obj.func) and obj.func.ident in names
    except AttributeError:
        return False


def _for_loop_dict_items_target_names(target) -> Optional[tuple[str, str]]:
    if not _for_loop_is_tuple_expr(target):
        return None
    elems = getattr(target, "elems", ())
    if len(elems) != 2:
        return None
    left = elems[0]
    right = elems[1]
    if _for_loop_is_name(left) and _for_loop_is_name(right):
        return (left.ident, right.ident)
    return None


def _for_loop_dict_items_object(iter_expr):
    if not _for_loop_is_call(iter_expr):
        return None
    if getattr(iter_expr, "args", ()) or getattr(iter_expr, "kwargs", ()):
        return None
    func = getattr(iter_expr, "func", None)
    if not isinstance(func, Attr):
        return None
    if func.name != "items":
        return None
    return func.obj


def _for_target_error_cleanup(host, target_ident: str, alloca: ir.Value) -> None:
    """Make a replaceable owned for-target safe on the shared error exit.

    Most for-targets are discovered while lowering the body, after an earlier
    expression may already have materialised ``err.exit``.  Record the target
    separately from exact-int locals and retro-patch a branchless release at
    the *start* of an existing error block, before any frame-leave calls.
    """
    if not hasattr(host, "_for_target_owned_names"):
        host._for_target_owned_names = set()
    host._for_target_owned_names.add(target_ident)
    fn = host.current_function
    if fn is None:
        return
    if not hasattr(host, "_fn_err_exit_for_target_slots"):
        host._fn_err_exit_for_target_slots = {}
    patched = host._fn_err_exit_for_target_slots.setdefault(fn.name, [])
    for done in patched:
        if done is alloca:
            return
    err_bb = host._fn_err_exit_blocks.get(fn.name)
    if err_bb is None:
        # Creation sees _for_target_owned_names and emits the same cleanup;
        # it also back-patches every already-registered root leave.
        host._ensure_fn_err_exit()
        patched.append(alloca)
        return
    save_block = host.builder._block
    if err_bb._instrs:
        host.builder.position_before(err_bb._instrs[0])
    else:
        host.builder.position_at_end(err_bb)
    owned_flag = host._ensure_owned_local_flag(target_ident, alloca)
    is_owned = host.builder.load(
        owned_flag,
        name=host._fresh(target_ident + ".for.err.owned"),
    )
    current = host.builder.call(
        host.runtime["pcc_gc_load_ptr"],
        [
            ir.Constant(_CSTR, None),
            host._as_gc_ptr(
                alloca,
                name=host._fresh(target_ident + ".for.err.gc.slot"),
            ),
        ],
        name=host._fresh(target_ident + ".for.err.current"),
    )
    release_value = host.builder.select(
        is_owned,
        current,
        ir.Constant(_CSTR, None),
        name=host._fresh(target_ident + ".for.err.release.value"),
    )
    host._gc_release(
        release_value,
        host._release_context_label("for-target:" + target_ident),
    )
    host.builder.store(ir.Constant(_I1, 0), owned_flag)
    patched.append(alloca)
    host.builder.position_at_end(save_block)


def _for_prepare_owned_object_target(host, target_ident: str, target_ty: Type):
    """Return one owned, updateable object slot for all loop-entry edges.

    If the name already denotes a scalar, box that value before the loop so a
    zero-iteration edge preserves it.  Borrowed object bindings are promoted
    into a fresh owned slot.  An already-owned object slot is reused.  The
    resulting runtime flag distinguishes an actually bound value from an
    unexecuted loop whose target had no pre-loop binding.
    """
    existing = host.env.get(target_ident)
    if (
        existing is not None
        and isinstance(existing[1], ir.PointerType)
        and target_ident in getattr(host, "_owned_local_names", set())
        and target_ident
        not in getattr(host, "_borrowed_gc_rooted_local_names", set())
    ):
        alloca = existing[0]
        host.env[target_ident] = (alloca, _CSTR, target_ty)
        host._owned_local_has_value.add(target_ident)
        if not hasattr(host, "_for_target_owned_names"):
            host._for_target_owned_names = set()
        host._for_target_owned_names.add(target_ident)
        host._ensure_owned_local_gc_root(target_ident, alloca, _CSTR)
        host._ensure_owned_local_flag(target_ident, alloca)
        _for_target_error_cleanup(host, target_ident, alloca)
        if isinstance(target_ty, IntType):
            host._exact_int_env_flags[target_ident] = True
        else:
            host._exact_int_env_flags.pop(target_ident, None)
        host._clear_cpy_for_target_binding(target_ident)
        return host.env[target_ident]

    initial_obj = None
    if existing is not None:
        old_alloca, old_ir_ty, old_decl_ty = existing
        if isinstance(old_ir_ty, ir.PointerType):
            if target_ident in getattr(host, "_cpy_env_flags", {}):
                raise L1CodegenError(
                    "cannot join a CPython-backed for-target with a native "
                    f"object binding for {target_ident!r}"
                )
            old_value = host.builder.call(
                host.runtime["pcc_gc_load_ptr"],
                [
                    ir.Constant(_CSTR, None),
                    host._as_gc_ptr(
                        old_alloca,
                        name=host._fresh(target_ident + ".for.pre.gc.slot"),
                    ),
                ],
                name=host._fresh(target_ident + ".for.pre.object"),
            )
            initial_obj = host._gc_retain(
                old_value,
                name=host._fresh(target_ident + ".for.pre.retain"),
            )
        else:
            old_value = host.builder.load(
                old_alloca,
                name=host._fresh(target_ident + ".for.pre.scalar"),
            )
            initial_obj = marshal.marshal_to_object(
                host.builder,
                host.module,
                host.runtime,
                old_value,
                old_decl_ty,
            )

    alloca = host._alloca_in_entry(
        _CSTR,
        name=f"{target_ident}.for.obj.addr",
        init_null=True,
    )
    host.env[target_ident] = (alloca, _CSTR, target_ty)
    host._owned_local_names.add(target_ident)
    host._owned_local_has_value.add(target_ident)
    if not hasattr(host, "_for_target_owned_names"):
        host._for_target_owned_names = set()
    host._for_target_owned_names.add(target_ident)
    host._ensure_owned_local_gc_root(target_ident, alloca, _CSTR)
    owned_flag = host._ensure_owned_local_flag(target_ident, alloca)
    if initial_obj is not None:
        host.builder.call(host.runtime["pcc_gc_pin"], [initial_obj])
        host.builder.call(
            host.runtime["pcc_gc_store_root"],
            [host._as_gc_ptr(alloca), initial_obj],
        )
        host.builder.store(ir.Constant(_I1, 1), owned_flag)
        host.builder.call(
            host.runtime["pcc_gc_unpin"],
            [host._value_available_at_insertion_point(initial_obj)],
        )
        # ``pcc_gc_store_root`` retains the value for the slot.  The boxed
        # scalar / promoted borrowed binding supplied ``initial_obj`` as an
        # owned temporary, so transfer that original reference after the
        # rooted owner is established.
        host._gc_release(
            initial_obj,
            host._release_context_label("for-target-initial:" + target_ident),
        )
    _for_target_error_cleanup(host, target_ident, alloca)
    if isinstance(target_ty, IntType):
        host._exact_int_env_flags[target_ident] = True
    else:
        host._exact_int_env_flags.pop(target_ident, None)
    host._threading_env_flags.pop(target_ident, None)
    host._threading_list_elem_flags.pop(target_ident, None)
    host._clear_cpy_for_target_binding(target_ident)
    return host.env[target_ident]


def _for_store_owned_target(host, target_ident: str, slot, value: ir.Value) -> None:
    """Transfer one owned runtime object into a replaceable loop target."""
    alloca = slot[0]
    # The target slot is either null or owns its current value.  The
    # ownership-transferring root store is the single replacement operation:
    # the getter/iterator result arrived owned, its reference moves into the
    # slot, and the prior slot owner is released (relocation-aware on the
    # moving backends).  This replaces the pin / store_root / unpin / release
    # quartet -- four runtime calls per loop iteration, two of them refcount
    # round trips on the same object (per-op row ``for_over_list``).
    host.builder.call(
        host.runtime["pcc_gc_store_root_take"],
        [host._as_gc_ptr(alloca), value],
    )
    owned_flag = host._ensure_owned_local_flag(target_ident, alloca)
    host.builder.store(ir.Constant(_I1, 1), owned_flag)


class ForLoopLoweringMixin:
    def _clear_cpy_for_target_binding(self, target_ident: str) -> None:
        """Native for-target rebinding must overwrite CPython local state."""
        if hasattr(self, "_cpy_env_flags"):
            self._cpy_env_flags.pop(target_ident, None)

    def _cpy_for_scan_node(self, node, events: list) -> bool:
        """Append ordered events for the cross-yield read check to
        ``events`` ("Y" for a yield sentinel, "R:<ident>" for a Name)
        and return True when the subtree contains a yield. Pre-order
        with two adjustments: a yield sentinel's argument reads precede
        its "Y" (``yield line`` reads ``line`` while still running),
        and a loop subtree that contains a yield emits "Y" FIRST — the
        back-edge makes every read inside happen after a suspension at
        runtime. Written in the bootstrap-safe dialect (no generators,
        no genexprs, no reflection)."""
        if node is None:
            return False
        if isinstance(node, list) or isinstance(node, tuple):
            has_y = False
            i = 0
            while i < len(node):
                if self._cpy_for_scan_node(node[i], events):
                    has_y = True
                i += 1
            return has_y
        if isinstance(node, str):
            return False
        if isinstance(node, Name):
            events.append("R:" + node.ident)
            return False
        if isinstance(node, For) or isinstance(node, While):
            sub: list = []
            has_y = self._cpy_for_scan_fields(node, sub)
            if has_y:
                events.append("Y")
            i = 0
            while i < len(sub):
                events.append(sub[i])
                i += 1
            return has_y
        if isinstance(node, Call):
            sentinel = None
            try:
                sentinel = self._yield_sentinel_call(node)
            except AttributeError:
                sentinel = None
            if sentinel is not None:
                self._cpy_for_scan_node(node.args, events)
                events.append("Y")
                return True
        return self._cpy_for_scan_fields(node, events)

    def _cpy_for_scan_fields(self, node, events: list) -> bool:
        # Explicit AST-bearing field table (audited against the full
        # py_ast dataclass field set on 2026-06-10); metadata fields
        # (span/ty/ident/name/line/...) are deliberately absent. A new
        # structural field added to py_ast must be added here or the
        # cross-yield check under-approximates reads.
        has_y = False
        i = 0
        while i < len(_CPY_SCAN_FIELDS):
            child = getattr(node, _CPY_SCAN_FIELDS[i], None)
            if child is not None:
                if self._cpy_for_scan_node(child, events):
                    has_y = True
            i += 1
        return has_y

    def _cpy_for_target_read_crosses_yield(self, stmt: For, target: str) -> bool:
        """True when the loop target may be read after a yield
        suspension inside the loop body (the unsupported-by-J1 shape).
        The target is re-stored at the top of every iteration, so the
        loop's own back-edge is safe; only reads AFTER a yield (in
        source order, with yielding nested loops treated as
        yield-before-everything) are crossings."""
        events: list = []
        body_stmts = []
        i = 0
        while i < len(stmt.body):
            body_stmts.append(stmt.body[i])
            i += 1
        self._cpy_for_scan_node(body_stmts, events)
        saw_yield = False
        marker = "R:" + target
        i = 0
        while i < len(events):
            ev = events[i]
            i += 1
            if ev == "Y":
                saw_yield = True
                continue
            if saw_yield and ev == marker:
                return True
        return False

    def _emit_for_cpython_iter(
        self,
        stmt: For,
        iter_src_val: ir.Value,
    ) -> None:
        """Lower ``for <name> in <cpython_iterable>:`` via PyObject_GetIter
        + PyIter_Next. Each iteration binds the target name to the
        returned CPython PyObject* (tagged as cpy).

        Inside a generator (J2', see
        docs/investigations/generator-cpython-iteration-dominance.md):
        the iterator AND the single-name loop target are boxed into
        CpyHandle pcc objects (raw libpython pointers must never enter
        the frame py_list — store barriers and frame dealloc
        dereference pcc headers; the handle's dealloc releases the
        foreign ref, so dropping a suspended generator releases its
        live iterator/item). The central name-load helper unboxes
        names registered in ``cpy_boxed_names``, so the target may be
        read across yield suspensions like CPython. TUPLE-UNPACK
        targets still use the J1 skip-save + precise cross-yield guard
        (full unpack support is the tracked second stage)."""
        gen_ctx = None
        target_name = stmt.target.ident
        # ``for (a, b) in cpy:`` was normalised into a synthetic single
        # target plus a leading unpack assignment; the unpack targets
        # receive cpy element pointers, so they are cpy locals too.
        # FLAT single-level all-Name unpacks are handled natively in a
        # generator (J2' stage 2: the loop emits the element extraction
        # itself and consumes body[0]); nested tuple targets keep the
        # J1 skip-save + precise guard.
        flat_unpack_names: list = []
        nested_unpack_names: list = []
        has_unpack = False
        if stmt.body:
            first = stmt.body[0]
            if (
                isinstance(first, Assign)
                and isinstance(first.value, Name)
                and first.value.ident == target_name
                and len(first.targets) == 1
                and isinstance(first.targets[0], TupleExpr)
            ):
                has_unpack = True
                flat_ok = True
                for el in first.targets[0].elems:
                    if isinstance(el, Name):
                        flat_unpack_names.append(el.ident)
                    else:
                        flat_ok = False
                if not flat_ok:
                    flat_unpack_names = []
                    work = [first.targets[0]]
                    while work:
                        cur = work.pop()
                        if isinstance(cur, TupleExpr):
                            work.extend(cur.elems)
                        elif isinstance(cur, Name):
                            nested_unpack_names.append(cur.ident)
        if len(getattr(self, "_generator_ctx_stack", ())) > 0:
            gen_ctx = self._generator_ctx_stack[-1]
            for nm in nested_unpack_names:
                if self._cpy_for_target_read_crosses_yield(stmt, nm):
                    raise NotImplementedError(
                        "Layer 1 does not support reading the "
                        f"CPython-backed loop variable {nm!r} after a "
                        "yield suspension yet"
                    )
        pre_target = self.env.get(target_name)
        if pre_target is not None:
            pre_is_cpy = target_name in getattr(self, "_cpy_env_flags", {})
            if (
                not isinstance(pre_target[1], ir.PointerType)
                or (gen_ctx is None and not pre_is_cpy)
            ):
                # Raw CPython pointers and native pcc objects/scalars cannot
                # share one slot or one compile-time domain flag.  Refuse this
                # mixed-domain zero/nonzero join rather than storing a pointer
                # into a scalar slot or mis-tagging a preserved native value.
                raise L1CodegenError(
                    "CPython for-target representation join requires an "
                    f"already-CPython binding for {target_name!r}"
                )
        fn = self.current_function
        iter_obj = self.builder.call(
            self.runtime["py_cpy_iter"],
            [iter_src_val],
            name=self._fresh("cpy.iter"),
        )
        # Spill the iterator to a slot so the def dominates the
        # header/after uses. In a generator the slot must live in the
        # persisted frame (a plain entry alloca is rebuilt per resume
        # call), and the frame only holds pcc objects — so the handle
        # is boxed as a pcc int and unboxed at each use.
        iter_slot = None
        frame_iter_slot = None
        if gen_ctx is not None:
            hidden = self._generator_for_iter_name(stmt)
            frame_entry = gen_ctx["frame_slots"].get(hidden)
            if frame_entry is None:
                raise L1CodegenError("generator for-loop missing iterator frame slot")
            frame_iter_slot = frame_entry[1]
            # CpyHandle takes ownership of the py_cpy_iter ref; its
            # dealloc (frame drop / save overwrite) releases it.
            boxed = self.builder.call(
                self.runtime["py_cpy_handle_new"],
                [iter_obj],
                name=self._fresh("cpy.iter.box"),
            )
            self.builder.store(boxed, frame_iter_slot)
            if nested_unpack_names:
                skip = gen_ctx.setdefault("cpy_skip_save_names", set())
                skip.update(nested_unpack_names)
            # Pre-clear the target slot (and the flat-unpack slots):
            # the per-iteration store decrefs the previous box, and the
            # first iteration must not decref alloca garbage
            # (py_decref(NULL) is a no-op). Prior same-named local
            # values are discarded like any for-target rebind.
            for pre_ident in [stmt.target.ident] + flat_unpack_names:
                pre_slot = self.env.get(pre_ident)
                if pre_slot is None:
                    pre_alloca = self._alloca_in_entry(_CSTR, name=f"{pre_ident}.addr")
                    self.env[pre_ident] = (
                        pre_alloca,
                        _CSTR,
                        DynType(name="dyn"),
                    )
                    pre_slot = self.env[pre_ident]
                self.builder.store(ir.Constant(_CSTR, None), pre_slot[0])
        else:
            iter_slot = self._alloca_in_entry(
                iter_obj.type,
                name=self._fresh("cpy.iter.slot"),
            )
            self.builder.store(iter_obj, iter_slot)

        header_bb = fn.append_basic_block(name=self._fresh("for.cpy.header"))
        body_bb = fn.append_basic_block(name=self._fresh("for.cpy.body"))
        latch_bb = fn.append_basic_block(name=self._fresh("for.cpy.latch"))
        after_bb = fn.append_basic_block(name=self._fresh("for.cpy.after"))

        self.builder.branch(header_bb)
        self.builder.position_at_end(header_bb)
        if frame_iter_slot is not None:
            iter_cur = self._cpy_iter_unbox_from_frame(frame_iter_slot)
        else:
            iter_cur = self.builder.load(
                iter_slot,
                name=self._fresh("cpy.iter.cur"),
            )
        item = self.builder.call(
            self.runtime["py_cpy_iter_next"],
            [iter_cur],
            name=self._fresh("cpy.next"),
        )
        is_null = self.builder.icmp_signed(
            "==",
            item,
            ir.Constant(_CSTR, None),
            name=self._fresh("cpy.next.isnull"),
        )
        self.builder.cbranch(is_null, after_bb, body_bb)

        self.builder.position_at_end(body_bb)
        # Bind the target name: alloca if new, then store.
        target_ident = stmt.target.ident
        slot = self.env.get(target_ident)
        if slot is None:
            alloca = self._alloca_in_entry(_CSTR, name=f"{target_ident}.addr")
            self.env[target_ident] = (alloca, _CSTR, DynType(name="dyn"))
            slot = self.env[target_ident]
        if gen_ctx is not None:
            # J2': the target slot holds a CpyHandle box (frame-safe;
            # the central name-load helper unboxes). The handle takes
            # ownership of the PyIter_Next ref; the PREVIOUS
            # iteration's box loses its local reference here (the
            # frame may still hold one from the last save).
            old_box = self.builder.load(slot[0], name=self._fresh("cpy.item.old"))
            item_box = self.builder.call(
                self.runtime["py_cpy_handle_new"],
                [item],
                name=self._fresh("cpy.item.box"),
            )
            self.builder.store(item_box, slot[0])
            self.builder.call(self.runtime["py_decref"], [old_box])
            boxed_names = gen_ctx.setdefault("cpy_boxed_names", set())
            boxed_names.add(target_ident)
        else:
            self.builder.store(item, slot[0])
        # Mark target as CPython-backed.
        if not hasattr(self, "_cpy_env_flags"):
            self._cpy_env_flags = {}
        self._cpy_env_flags[target_ident] = True

        body_stmts = stmt.body
        if gen_ctx is not None and has_unpack and flat_unpack_names:
            # J2' stage 2: emit the flat tuple unpack OURSELVES from the
            # raw cpy item (cpy-bridge element extraction, each element
            # boxed like the target) and consume body[0] — the generic
            # assignment path neither tracks element cpy-ness nor boxes.
            #
            # Arity check first (CPython raises ValueError on
            # mismatch); py_cpy_len returns -1 for unsized items, in
            # which case the check is skipped (conservative).
            expected_n = len(flat_unpack_names)
            item_len = self.builder.call(
                self.runtime["py_cpy_len"],
                [item],
                name=self._fresh("cpy.unpack.len"),
            )
            unsized = self.builder.icmp_signed(
                "<",
                item_len,
                ir.Constant(_I64, 0),
                name=self._fresh("cpy.unpack.unsized"),
            )
            len_match = self.builder.icmp_signed(
                "==",
                item_len,
                ir.Constant(_I64, expected_n),
                name=self._fresh("cpy.unpack.match"),
            )
            arity_ok = self.builder.or_(
                unsized, len_match, name=self._fresh("cpy.unpack.ok")
            )
            arity_ok_bb = fn.append_basic_block(name=self._fresh("for.cpy.unpack.ok"))
            arity_bad_bb = fn.append_basic_block(name=self._fresh("for.cpy.unpack.bad"))
            self.builder.cbranch(arity_ok, arity_ok_bb, arity_bad_bb)
            self.builder.position_at_end(arity_bad_bb)
            arity_msg = self._ptr_to_cstr(
                self._cstr_global(
                    "cannot unpack CPython sequence: arity mismatch "
                    f"(expected {expected_n})",
                    ".cpy.unpack.arity",
                )
            )
            arity_exc = self.builder.call(
                self.runtime["py_exc_new"],
                [ir.Constant(_I64, 2), arity_msg],
                name=self._fresh("cpy.unpack.exc"),
            )
            self.builder.call(self.runtime["py_raise"], [arity_exc])
            arity_err_target = (
                getattr(self, "_try_err_block", None) or self._ensure_fn_err_exit()
            )
            self.builder.branch(arity_err_target)
            self.builder.position_at_end(arity_ok_bb)
            boxed_names = gen_ctx.setdefault("cpy_boxed_names", set())
            for uj, unm in enumerate(flat_unpack_names):
                idx_cpy = self.builder.call(
                    self.runtime["py_cpy_from_i64"],
                    [ir.Constant(_I64, uj)],
                    name=self._fresh(f"cpy.unpack.idx.{uj}"),
                )
                elem = self.builder.call(
                    self.runtime["py_cpy_getitem"],
                    [item, idx_cpy],
                    name=self._fresh(f"cpy.unpack.{uj}"),
                )
                self.builder.call(self.runtime["py_cpy_decref"], [idx_cpy])
                un_slot = self.env.get(unm)
                if un_slot is None:
                    un_alloca = self._alloca_in_entry(_CSTR, name=f"{unm}.addr")
                    self.env[unm] = (un_alloca, _CSTR, DynType(name="dyn"))
                    un_slot = self.env[unm]
                old_elem_box = self.builder.load(
                    un_slot[0], name=self._fresh(f"cpy.unpack.old.{uj}")
                )
                elem_box = self.builder.call(
                    self.runtime["py_cpy_handle_new"],
                    [elem],
                    name=self._fresh(f"cpy.unpack.box.{uj}"),
                )
                self.builder.store(elem_box, un_slot[0])
                self.builder.call(self.runtime["py_decref"], [old_elem_box])
                self._cpy_env_flags[unm] = True
                boxed_names.add(unm)
            body_stmts = stmt.body[1:]

        # Loop control stack: continue -> header, break -> after.
        self.loop_stack.append((latch_bb, after_bb, self._loop_finally_base()))
        self._emit_stmts(body_stmts)
        self.loop_stack.pop()
        if not self._builder_block_is_terminated():
            # Release item (we took ownership from PyIter_Next).
            # Note: storing into the slot didn't bump ref; we hold
            # exactly one.
            self.builder.branch(latch_bb)

        self.builder.position_at_end(latch_bb)
        self._emit_thread_safepoint()
        self.builder.branch(header_bb)

        self.builder.position_at_end(after_bb)
        if frame_iter_slot is not None:
            # J2': the iterator's foreign ref belongs to its CpyHandle
            # box (released on frame drop / save overwrite) — no manual
            # py_cpy_decref. The target slot keeps the LAST item's box
            # so reads after the loop see the final item like CPython
            # (the generator frame's save/dealloc balances its ref).
            # Drop the iterator box's local reference now and clear the
            # frame slot so the iterator is released at loop exit, not
            # at generator drop.
            iter_box_done = self.builder.load(
                frame_iter_slot, name=self._fresh("cpy.iter.box.done")
            )
            self.builder.store(ir.Constant(_CSTR, None), frame_iter_slot)
            self.builder.call(self.runtime["py_decref"], [iter_box_done])
            if nested_unpack_names:
                # J1 behavior for NESTED unpack names: clear raw cpy
                # pointers before frame saves resume, then re-arm
                # normal saves. (Flat unpack names hold CpyHandle boxes
                # and stay readable after the loop like the target.)
                none_obj = self._emit_none_literal()
                for nm in nested_unpack_names:
                    nm_slot = self.env.get(nm)
                    if nm_slot is not None and nm_slot[1] == _CSTR:
                        self.builder.store(none_obj, nm_slot[0])
                    gen_ctx["cpy_skip_save_names"].discard(nm)
        else:
            iter_done = self.builder.load(
                iter_slot,
                name=self._fresh("cpy.iter.done"),
            )
            self.builder.call(self.runtime["py_cpy_decref"], [iter_done])

    def _cpy_iter_unbox_from_frame(self, frame_iter_slot) -> ir.Value:
        boxed = self.builder.load(
            frame_iter_slot,
            name=self._fresh("cpy.iter.boxed"),
        )
        return self.builder.call(
            self.runtime["py_cpy_handle_get"],
            [boxed],
            name=self._fresh("cpy.iter.ptr"),
        )

    def _emit_for_list_index(
        self,
        stmt: For,
        iter_val: ir.Value,
        iter_ty: Type,
        source_owned: Optional[bool] = None,
    ) -> None:
        """Lower ``for <name> in <list|tuple>:`` via index + length.

        Covers ``ListType`` / ``TupleType`` iters where the runtime
        value is a PyObject* tuple/list. Element type flows from
        ``iter_ty.elem`` (list) or ``DynType`` (tuple — element types
        differ per slot, so we fall back to Dyn here).
        """
        fn = self.current_function
        iter_obj = marshal.marshal_to_object(
            self.builder,
            self.module,
            self.runtime,
            iter_val,
            iter_ty,
        )
        if source_owned is None:
            source_owned = self._owned_release_needed(iter_val, stmt.iter)
        if not source_owned:
            iter_obj = self._gc_retain(iter_obj, name=self._fresh("for.lst.retain"))
        # The indexed loop needs the same independent, updateable owner as
        # an iterator. Field reads arrive owned; local reads need a retain so
        # rebinding the source in the body cannot destroy the active list.
        source_name = self._fresh("for.lst.source")
        source_slot = _for_prepare_owned_object_target(self, source_name, iter_ty)
        _for_store_owned_target(self, source_name, source_slot, iter_obj)
        outer_err = getattr(self, "_try_err_block", None)
        source_err = fn.append_basic_block(name=self._fresh("for.lst.error"))
        self._try_err_block = source_err
        if isinstance(iter_ty, ListType):
            len_helper = "py_list_len"
            get_helper = "py_list_get"
            elem_ty: Type = iter_ty.elem
        else:
            len_helper = "py_tuple_len"
            get_helper = "py_tuple_get"
            elem_ty = DynType(name="dyn")
            if isinstance(iter_ty, TupleType) and iter_ty.elems:
                first = iter_ty.elems[0]
                if iter_ty.name == "tuple_variadic" or self._tuple_elems_are_uniform(
                    iter_ty.elems, first
                ):
                    elem_ty = first
        n_val = self.builder.call(
            self.runtime[len_helper],
            [iter_obj],
            name=self._fresh("for.len"),
        )

        idx_slot = self._alloca_in_entry(_I64, name="for.idx.addr")
        self.builder.store(ir.Constant(_I64, 0), idx_slot)

        target_ident = stmt.target.ident
        target_ty = stmt.target.ty
        target_ir_ty = self._storage_ir_type(target_ty)
        existing = self.env.get(target_ident)
        target_is_object = isinstance(target_ir_ty, ir.PointerType) or (
            existing is not None and isinstance(existing[1], ir.PointerType)
        )
        if target_is_object:
            slot = _for_prepare_owned_object_target(
                self,
                target_ident,
                target_ty,
            )
        else:
            slot = existing
            if slot is None:
                alloca = self._alloca_in_entry(
                    target_ir_ty,
                    name=f"{target_ident}.addr",
                )
                self.env[target_ident] = (alloca, target_ir_ty, target_ty)
                slot = self.env[target_ident]
            elif not self._ir_type_matches(slot[1], target_ir_ty):
                raise L1CodegenError(
                    "for-target representation join was not boxed for "
                    f"{target_ident!r}"
                )
        threading_target_kind = self._threading_kind_for_type(elem_ty)
        if threading_target_kind is None and isinstance(stmt.iter, Name):
            threading_target_kind = self._threading_list_elem_flags.get(stmt.iter.ident)
        if threading_target_kind is not None:
            self._threading_env_flags[target_ident] = threading_target_kind
        else:
            self._threading_env_flags.pop(target_ident, None)
        threading_list_elem_kind = self._threading_list_elem_kind_for_type(elem_ty)
        if threading_list_elem_kind is not None:
            self._threading_list_elem_flags[target_ident] = threading_list_elem_kind
        else:
            self._threading_list_elem_flags.pop(target_ident, None)
        self._clear_cpy_for_target_binding(target_ident)

        cond_bb = fn.append_basic_block(name=self._fresh("for.lst.cond"))
        body_bb = fn.append_basic_block(name=self._fresh("for.lst.body"))
        step_bb = fn.append_basic_block(name=self._fresh("for.lst.step"))
        end_bb = fn.append_basic_block(name=self._fresh("for.lst.end"))
        self.builder.branch(cond_bb)

        self.builder.position_at_end(cond_bb)
        cur = self.builder.load(idx_slot, name=self._fresh("for.idx"))
        cond = self.builder.icmp_signed(
            "<",
            cur,
            n_val,
            name=self._fresh("for.cond"),
        )
        self.builder.cbranch(cond, body_bb, end_bb)

        self.builder.position_at_end(body_bb)
        iter_obj = self.builder.call(
            self.runtime["pcc_gc_load_ptr"],
            [ir.Constant(_CSTR, None), self._as_gc_ptr(source_slot[0])],
            name=self._fresh("for.lst.current"),
        )
        target_alloca, target_ir_ty, _ = slot
        if (
            isinstance(iter_ty, ListType)
            and isinstance(elem_ty, IntType)
            and not isinstance(target_ir_ty, ir.PointerType)
        ):
            # Native-i64 fast path: valid only when the loop-variable slot is
            # itself native i64. Under int-boxing (_int_exprs_are_boxed),
            # _storage_ir_type(IntType) is a PyObject* (boxed PyInt), so the
            # slot is a pointer; storing a raw i64 from
            # py_list_get_i64_nonnegative into it is an "i64 but expected ptr"
            # type error (silently a zero-iteration loop under the self
            # backend). When the slot is boxed, fall through to the boxed
            # py_list_get path below, which stores the PyObject* element.
            native_val = self.builder.call(
                self.runtime["py_list_get_i64_nonnegative"],
                [iter_obj, cur],
                name=self._fresh("for.elem.i64"),
            )
            self.builder.store(native_val, target_alloca)
        else:
            elem_obj = self.builder.call(
                self.runtime[get_helper],
                [iter_obj, cur],
                name=self._fresh("for.elem"),
            )
            self._emit_post_call_err_check(stmt.span)
            if isinstance(target_ir_ty, ir.PointerType):
                _for_store_owned_target(
                    self,
                    target_ident,
                    slot,
                    elem_obj,
                )
            elif self._is_valueclass_payload_type(elem_ty):
                native_val = self._emit_object_to_valueclass_payload(
                    elem_obj,
                    elem_ty,
                )
                if native_val is None:
                    native_val = marshal.marshal_from_object(
                        self.builder,
                        self.module,
                        self.runtime,
                        elem_obj,
                        elem_ty,
                    )
                self.builder.store(native_val, target_alloca)
                self._gc_release(
                    elem_obj,
                    self._release_context_label("for-list-element"),
                )
            else:
                native_val = marshal.marshal_from_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    elem_obj,
                    elem_ty,
                )
                self.builder.store(native_val, target_alloca)
                self._gc_release(
                    elem_obj,
                    self._release_context_label("for-list-element"),
                )
        if self._is_valueclass_payload_type(slot[2]):
            self._ensure_valueclass_payload_gc_roots(
                target_ident,
                target_alloca,
                slot[2],
            )

        self.loop_stack.append((step_bb, end_bb, self._loop_finally_base()))
        self._emit_stmts(stmt.body)
        self.loop_stack.pop()
        if not self._builder_block_is_terminated():
            self.builder.branch(step_bb)

        self.builder.position_at_end(step_bb)
        cur2 = self.builder.load(idx_slot, name=self._fresh("for.idx2"))
        nxt = self.builder.add(
            cur2,
            ir.Constant(_I64, 1),
            name=self._fresh("for.idx.next"),
        )
        self.builder.store(nxt, idx_slot)
        self._emit_thread_safepoint()
        self.builder.branch(cond_bb)

        self._try_err_block = outer_err
        self.builder.position_at_end(source_err)
        self.builder.call(
            self.runtime["pcc_gc_store_root"],
            [self._as_gc_ptr(source_slot[0]), ir.Constant(_CSTR, None)],
        )
        source_flag = self._ensure_owned_local_flag(source_name, source_slot[0])
        self.builder.store(ir.Constant(_I1, 0), source_flag)
        self.builder.branch(outer_err if outer_err is not None else self._ensure_fn_err_exit())

        self.builder.position_at_end(end_bb)
        self.builder.call(
            self.runtime["pcc_gc_store_root"],
            [self._as_gc_ptr(source_slot[0]), ir.Constant(_CSTR, None)],
        )
        self.builder.store(ir.Constant(_I1, 0), source_flag)

    def _ensure_object_for_target(self, target_ident: str):
        return _for_prepare_owned_object_target(
            self,
            target_ident,
            DynType(name="dyn"),
        )

    def _emit_for_dict_items_direct(self, stmt: For, dict_expr: Expr) -> None:
        target_names = _for_loop_dict_items_target_names(stmt.target)
        if target_names is None:
            raise L1CodegenError("dict.items fast path requires two-name target")
        key_name, value_name = target_names
        dict_val = self._emit_expr(dict_expr)
        if not isinstance(dict_val.type, ir.PointerType):
            dict_val = marshal.marshal_to_object(
                self.builder,
                self.module,
                self.runtime,
                dict_val,
                dict_expr.ty,
            )
        n_val = self.builder.call(
            self.runtime["py_dict_entries_used"],
            [dict_val],
            name=self._fresh("for.dict.items.used"),
        )
        idx_slot = self._alloca_in_entry(_I64, name="for.dict.items.idx.addr")
        self.builder.store(ir.Constant(_I64, 0), idx_slot)

        key_slot = self._ensure_object_for_target(key_name)
        value_slot = self._ensure_object_for_target(value_name)

        fn = self.current_function
        cond_bb = fn.append_basic_block(name=self._fresh("for.dict.items.cond"))
        load_bb = fn.append_basic_block(name=self._fresh("for.dict.items.load"))
        body_bb = fn.append_basic_block(name=self._fresh("for.dict.items.body"))
        step_bb = fn.append_basic_block(name=self._fresh("for.dict.items.step"))
        end_bb = fn.append_basic_block(name=self._fresh("for.dict.items.end"))
        self.builder.branch(cond_bb)

        self.builder.position_at_end(cond_bb)
        cur = self.builder.load(idx_slot, name=self._fresh("for.dict.items.idx"))
        cond = self.builder.icmp_signed(
            "<",
            cur,
            n_val,
            name=self._fresh("for.dict.items.cond.i1"),
        )
        self.builder.cbranch(cond, load_bb, end_bb)

        self.builder.position_at_end(load_bb)
        key_obj = self.builder.call(
            self.runtime["py_dict_entry_key_at"],
            [dict_val, cur],
            name=self._fresh("for.dict.items.key"),
        )
        key_is_null = self.builder.icmp_unsigned(
            "==",
            key_obj,
            ir.Constant(_CSTR, None),
            name=self._fresh("for.dict.items.key.null"),
        )
        self.builder.cbranch(key_is_null, step_bb, body_bb)

        self.builder.position_at_end(body_bb)
        _for_store_owned_target(self, key_name, key_slot, key_obj)
        value_obj = self.builder.call(
            self.runtime["py_dict_entry_value_at"],
            [dict_val, cur],
            name=self._fresh("for.dict.items.value"),
        )
        self._emit_post_call_err_check(stmt.span)
        _for_store_owned_target(self, value_name, value_slot, value_obj)
        self.loop_stack.append((step_bb, end_bb, self._loop_finally_base()))
        self._emit_stmts(stmt.body)
        self.loop_stack.pop()
        if not self._builder_block_is_terminated():
            self.builder.branch(step_bb)

        self.builder.position_at_end(step_bb)
        cur2 = self.builder.load(idx_slot, name=self._fresh("for.dict.items.idx2"))
        nxt = self.builder.add(
            cur2,
            ir.Constant(_I64, 1),
            name=self._fresh("for.dict.items.next"),
        )
        self.builder.store(nxt, idx_slot)
        self._emit_thread_safepoint()
        self.builder.branch(cond_bb)

        self.builder.position_at_end(end_bb)

    def _emit_for_obj_index(self, stmt: For, iter_val: ir.Value) -> None:
        """DynType for-loop: iterate by index using ``py_obj_len`` +
        ``py_obj_getitem``. Each iteration binds the target to a
        PyObject*; downstream callers see it as DynType."""
        fn = self.current_function
        # If inference pegged the iter as DynType but the IR value is
        # a native scalar (i1 from a short-circuit ``or`` branch,
        # i64 from an unboxed DynType int, etc.), box before calling
        # py_obj_len — the helper expects a pointer operand.
        if not isinstance(iter_val.type, ir.PointerType):
            iter_val = marshal.marshal_to_object(
                self.builder,
                self.module,
                self.runtime,
                iter_val,
                stmt.iter.ty,
            )
        n_val = self.builder.call(
            self.runtime["py_obj_len"],
            [iter_val],
            name=self._fresh("for.obj.len"),
        )
        idx_slot = self._alloca_in_entry(_I64, name="for.obj.idx.addr")
        self.builder.store(ir.Constant(_I64, 0), idx_slot)

        target_ident = stmt.target.ident
        slot = _for_prepare_owned_object_target(
            self,
            target_ident,
            stmt.target.ty,
        )

        cond_bb = fn.append_basic_block(name=self._fresh("for.obj.cond"))
        body_bb = fn.append_basic_block(name=self._fresh("for.obj.body"))
        step_bb = fn.append_basic_block(name=self._fresh("for.obj.step"))
        end_bb = fn.append_basic_block(name=self._fresh("for.obj.end"))
        self.builder.branch(cond_bb)

        self.builder.position_at_end(cond_bb)
        cur = self.builder.load(idx_slot, name=self._fresh("for.obj.idx"))
        cond = self.builder.icmp_signed(
            "<",
            cur,
            n_val,
            name=self._fresh("for.obj.cond.i1"),
        )
        self.builder.cbranch(cond, body_bb, end_bb)

        self.builder.position_at_end(body_bb)
        # Box the index as a PyObject* int for py_obj_getitem.
        idx_box = self.builder.call(
            self.runtime["py_int_from_i64"],
            [cur],
            name=self._fresh("for.obj.idx.box"),
        )
        elem = self.builder.call(
            self.runtime["py_obj_getitem"],
            [iter_val, idx_box],
            name=self._fresh("for.obj.elem"),
        )
        self._gc_release(
            idx_box,
            self._release_context_label("for-index-box"),
        )
        self._emit_post_call_err_check(stmt.span)
        _for_store_owned_target(self, target_ident, slot, elem)
        self.loop_stack.append((step_bb, end_bb, self._loop_finally_base()))
        self._emit_stmts(stmt.body)
        self.loop_stack.pop()
        if not self._builder_block_is_terminated():
            self.builder.branch(step_bb)

        self.builder.position_at_end(step_bb)
        cur2 = self.builder.load(idx_slot, name=self._fresh("for.obj.idx2"))
        nxt = self.builder.add(
            cur2,
            ir.Constant(_I64, 1),
            name=self._fresh("for.obj.next"),
        )
        self.builder.store(nxt, idx_slot)
        self._emit_thread_safepoint()
        self.builder.branch(cond_bb)

        self.builder.position_at_end(end_bb)

    def _emit_for_obj_iterator(self, stmt: For, iter_val: ir.Value) -> None:
        """DynType for-loop through the native iterator protocol."""
        fn = self.current_function
        source_owned = self._owned_release_needed(iter_val, stmt.iter)
        if not isinstance(iter_val.type, ir.PointerType):
            iter_val = marshal.marshal_to_object(
                self.builder,
                self.module,
                self.runtime,
                iter_val,
                stmt.iter.ty,
            )
            source_owned = True
        source_root = None
        if source_owned:
            source_root = self._enter_container_temp_root(
                iter_val, self._fresh("for.obj.source")
            )
        iterator = self.builder.call(
            self.runtime["py_obj_iter"],
            [iter_val],
            name=self._fresh("for.obj.iter"),
        )
        iter_slot = None
        owned_iter_name = None
        if len(self._generator_ctx_stack) > 0:
            hidden = self._generator_for_iter_name(stmt)
            frame_entry = self._generator_ctx_stack[-1]["frame_slots"].get(hidden)
            if frame_entry is None:
                raise L1CodegenError("generator for-loop missing iterator frame slot")
            iter_slot = frame_entry[1]
            self.builder.store(iterator, iter_slot)
        else:
            # py_obj_iter returns an owned GC object.  A plain function used
            # to keep it only in an SSA value, so a tracing step during a
            # long loop could not prove the iterator live and the loop also
            # leaked the owned reference at exhaustion.  Give it the same
            # updateable rooted-local contract as an ordinary object binding.
            owned_iter_name = self._fresh("for.obj.iter.owner")
            iter_slot = self._alloca_in_entry(
                _CSTR,
                name=self._fresh("for.obj.iter.root"),
            )
            self._store_entry_initializer(iter_slot, ir.Constant(_CSTR, None))
            self.env[owned_iter_name] = (
                iter_slot,
                _CSTR,
                DynType(name="dyn"),
            )
            self._ensure_owned_local_gc_root(
                owned_iter_name,
                iter_slot,
                _CSTR,
            )
            self.builder.call(
                self.runtime["pcc_gc_store_root"],
                [iter_slot, iterator],
            )
            self._owned_local_names.add(owned_iter_name)
            self._owned_local_has_value.add(owned_iter_name)
            owned_flag = self._ensure_owned_local_flag(
                owned_iter_name,
                iter_slot,
            )
            self.builder.store(ir.Constant(_I1, 1), owned_flag)

        # The iterator now owns its source and has a rooted lifetime. Consume
        # the iterable expression's separate owner on success and failure;
        # otherwise a field getter retains the entire completed task tree.
        if source_root is not None:
            self._leave_container_temp_root(source_root)
            self._gc_release(iter_val, self._release_context_label("for.source"))
        self._emit_post_call_err_check(stmt.span)

        target_ident = stmt.target.ident
        slot = _for_prepare_owned_object_target(
            self,
            target_ident,
            stmt.target.ty,
        )

        header_bb = fn.append_basic_block(name=self._fresh("for.obj.next"))
        body_bb = fn.append_basic_block(name=self._fresh("for.obj.body"))
        latch_bb = fn.append_basic_block(name=self._fresh("for.obj.latch"))
        maybe_end_bb = fn.append_basic_block(name=self._fresh("for.obj.maybe_end"))
        clear_bb = fn.append_basic_block(name=self._fresh("for.obj.clear"))
        propagate_bb = fn.append_basic_block(name=self._fresh("for.obj.propagate"))
        end_bb = fn.append_basic_block(name=self._fresh("for.obj.end"))

        self.builder.branch(header_bb)
        self.builder.position_at_end(header_bb)
        iterator_cur = iterator
        if iter_slot is not None:
            iterator_cur = self.builder.call(
                self.runtime["pcc_gc_load_ptr"],
                [ir.Constant(_CSTR, None), iter_slot],
                name=self._fresh("for.obj.iter.cur"),
            )
        item = self.builder.call(
            self.runtime["py_obj_next"],
            [iterator_cur],
            name=self._fresh("for.obj.item"),
        )
        is_null = self.builder.icmp_unsigned(
            "==",
            item,
            ir.Constant(_CSTR, None),
            name=self._fresh("for.obj.null"),
        )
        self.builder.cbranch(is_null, maybe_end_bb, body_bb)

        self.builder.position_at_end(body_bb)
        _for_store_owned_target(self, target_ident, slot, item)
        self.loop_stack.append((latch_bb, end_bb, self._loop_finally_base()))
        self._emit_stmts(stmt.body)
        self.loop_stack.pop()
        if not self._builder_block_is_terminated():
            self.builder.branch(latch_bb)

        self.builder.position_at_end(latch_bb)
        self._emit_thread_safepoint()
        self.builder.branch(header_bb)

        self.builder.position_at_end(maybe_end_bb)
        current_exc = self.builder.call(
            self.runtime["py_current_exception"],
            [],
            name=self._fresh("for.obj.cur_exc"),
        )
        stop_cls = self.builder.call(
            self.runtime["py_exc_builtin_class"],
            [ir.Constant(_I64, _BUILTIN_EXC_TAG["StopIteration"])],
            name=self._fresh("for.obj.stop_cls"),
        )
        match_i64 = self.builder.call(
            self.runtime["py_exc_matches"],
            [current_exc, stop_cls],
            name=self._fresh("for.obj.stop_match"),
        )
        is_stop = self.builder.icmp_signed(
            "!=",
            match_i64,
            ir.Constant(_I64, 0),
            name=self._fresh("for.obj.stop_i1"),
        )
        self.builder.cbranch(is_stop, clear_bb, propagate_bb)

        self.builder.position_at_end(clear_bb)
        self.builder.call(self.runtime["py_clear_exception"], [])
        self.builder.branch(end_bb)

        self.builder.position_at_end(propagate_bb)
        if owned_iter_name is not None:
            self._emit_release_owned_local_if_flagged(
                owned_iter_name,
                iter_slot,
            )
            self.builder.call(
                self.runtime["pcc_gc_store_root"],
                [iter_slot, ir.Constant(_CSTR, None)],
            )
        err_target = getattr(self, "_try_err_block", None)
        if err_target is None:
            err_target = self._ensure_fn_err_exit()
        self.builder.branch(err_target)

        self.builder.position_at_end(end_bb)
        if owned_iter_name is not None:
            self._emit_release_owned_local_if_flagged(
                owned_iter_name,
                iter_slot,
            )
            self.builder.call(
                self.runtime["pcc_gc_store_root"],
                [iter_slot, ir.Constant(_CSTR, None)],
            )

    def _emit_for_str_chars(self, stmt: For, iter_val: ir.Value) -> None:
        """StrType for-loop: iterate codepoints via ``py_str_slice(s, i, i+1, 1)``.
        Target binds to a 1-char StrType slice each iteration."""
        fn = self.current_function
        n_val = self.builder.call(
            self.runtime["py_str_len"],
            [iter_val],
            name=self._fresh("for.str.len"),
        )
        idx_slot = self._alloca_in_entry(_I64, name="for.str.idx.addr")
        self.builder.store(ir.Constant(_I64, 0), idx_slot)

        target_ident = stmt.target.ident
        slot = _for_prepare_owned_object_target(
            self,
            target_ident,
            stmt.target.ty,
        )

        cond_bb = fn.append_basic_block(name=self._fresh("for.str.cond"))
        body_bb = fn.append_basic_block(name=self._fresh("for.str.body"))
        step_bb = fn.append_basic_block(name=self._fresh("for.str.step_bb"))
        end_bb = fn.append_basic_block(name=self._fresh("for.str.end"))
        self.builder.branch(cond_bb)

        self.builder.position_at_end(cond_bb)
        cur = self.builder.load(idx_slot, name=self._fresh("for.str.idx"))
        cond = self.builder.icmp_signed(
            "<",
            cur,
            n_val,
            name=self._fresh("for.str.cond.i1"),
        )
        self.builder.cbranch(cond, body_bb, end_bb)

        self.builder.position_at_end(body_bb)
        lo_box = self.builder.call(
            self.runtime["py_int_from_i64"],
            [cur],
            name=self._fresh("for.str.lo"),
        )
        hi = self.builder.add(
            cur,
            ir.Constant(_I64, 1),
            name=self._fresh("for.str.hi.i64"),
        )
        hi_box = self.builder.call(
            self.runtime["py_int_from_i64"],
            [hi],
            name=self._fresh("for.str.hi"),
        )
        ch = self.builder.call(
            self.runtime["py_str_slice"],
            [iter_val, lo_box, hi_box, ir.Constant(_CSTR, None)],
            name=self._fresh("for.str.ch"),
        )
        self._gc_release(lo_box, self._release_context_label("for-str-lo"))
        self._gc_release(hi_box, self._release_context_label("for-str-hi"))
        self._emit_post_call_err_check(stmt.span)
        _for_store_owned_target(self, target_ident, slot, ch)
        self.loop_stack.append((step_bb, end_bb, self._loop_finally_base()))
        self._emit_stmts(stmt.body)
        self.loop_stack.pop()
        if not self._builder_block_is_terminated():
            self.builder.branch(step_bb)

        self.builder.position_at_end(step_bb)
        cur2 = self.builder.load(idx_slot, name=self._fresh("for.str.idx2"))
        nxt = self.builder.add(
            cur2,
            ir.Constant(_I64, 1),
            name=self._fresh("for.str.next"),
        )
        self.builder.store(nxt, idx_slot)
        self._emit_thread_safepoint()
        self.builder.branch(cond_bb)

        self.builder.position_at_end(end_bb)

    def _emit_for_native_iterator(
        self,
        stmt: For,
        iter_val: ir.Value,
        class_hint: str,
    ) -> None:
        iter_info = self._resolve_method_mro(class_hint, "__iter__")
        if iter_info is None:
            return self._emit_for_obj_index(stmt, iter_val)
        iter_fn = iter_info.methods["__iter__"]
        iterator = self._emit_direct_method_call(
            iter_fn,
            iter_val,
            iter_info,
            "__iter__",
            (),
        )
        iter_fd = self.class_lowering._find_method_def(
            iter_info.name,
            "__iter__",
        )
        iterator_hint = class_hint
        if iter_fd is not None:
            ann_hint = self._class_hint_from_annotation(iter_fd.return_ty)
            if ann_hint is not None:
                iterator_hint = ann_hint
        next_info = self._resolve_method_mro(iterator_hint, "__next__")
        if next_info is None:
            return self._emit_for_obj_index(stmt, iterator)
        next_fn = next_info.methods["__next__"]
        # Inside a generator the iterator must live in the persisted
        # frame slot (same idiom as _emit_for_obj_iterator): the raw SSA
        # value does not dominate the loop header once the generator
        # transform splits the body at yields, and the suspended frame
        # must keep the iterator GC-visible across resumes.
        iter_slot = None
        if len(self._generator_ctx_stack) > 0:
            hidden = self._generator_for_iter_name(stmt)
            frame_entry = self._generator_ctx_stack[-1]["frame_slots"].get(hidden)
            if frame_entry is None:
                raise L1CodegenError("generator for-loop missing iterator frame slot")
            iter_slot = frame_entry[1]
            self.builder.store(iterator, iter_slot)
        next_fd = self.class_lowering._find_method_def(
            next_info.name,
            "__next__",
        )
        next_target_ty: Type = DynType(name="dyn")
        if next_fd is not None and isinstance(next_fd.return_ty, Type):
            next_target_ty = next_fd.return_ty
        target_ty: Type = stmt.target.ty
        if isinstance(target_ty, DynType) and self.env.get(stmt.target.ident) is None:
            target_ty = next_target_ty
        target_ir_ty = (
            _CSTR
            if isinstance(target_ty, IntType) and self._int_exprs_are_boxed()
            else self._storage_ir_type(target_ty)
        )

        target_ident = stmt.target.ident
        existing = self.env.get(target_ident)
        if isinstance(target_ir_ty, ir.PointerType) or (
            existing is not None and isinstance(existing[1], ir.PointerType)
        ):
            slot = _for_prepare_owned_object_target(
                self,
                target_ident,
                target_ty,
            )
            target_ir_ty = _CSTR
        else:
            slot = existing
            if slot is None:
                alloca = self._alloca_in_entry(
                    target_ir_ty,
                    name=f"{target_ident}.addr",
                )
                self.env[target_ident] = (alloca, target_ir_ty, target_ty)
                slot = self.env[target_ident]
            elif not self._ir_type_matches(slot[1], target_ir_ty):
                raise L1CodegenError(
                    "native iterator target requires an object representation "
                    f"join for {target_ident!r}"
                )
            self._clear_cpy_for_target_binding(target_ident)

        fn = self.current_function
        header_bb = fn.append_basic_block(name=self._fresh("for.iter.header"))
        body_bb = fn.append_basic_block(name=self._fresh("for.iter.body"))
        latch_bb = fn.append_basic_block(name=self._fresh("for.iter.latch"))
        err_bb = fn.append_basic_block(name=self._fresh("for.iter.err"))
        after_bb = fn.append_basic_block(name=self._fresh("for.iter.after"))

        self.builder.branch(header_bb)
        self.builder.position_at_end(header_bb)
        iterator_cur = iterator
        if iter_slot is not None:
            iterator_cur = self.builder.load(
                iter_slot,
                name=self._fresh("for.iter.cur"),
            )
        prev_err_block = getattr(self, "_try_err_block", None)
        self._try_err_block = err_bb
        item = self._emit_direct_method_call(
            next_fn,
            iterator_cur,
            next_info,
            "__next__",
            (),
        )
        self._try_err_block = prev_err_block
        if isinstance(target_ir_ty, ir.PointerType):
            if not isinstance(item.type, ir.PointerType):
                item = marshal.marshal_to_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    item,
                    next_target_ty,
                )
            _for_store_owned_target(self, target_ident, slot, item)
        else:
            if item.type != target_ir_ty:
                item = self._coerce(item, next_target_ty, target_ty)
            self.builder.store(item, slot[0])
        self.builder.branch(body_bb)

        self.builder.position_at_end(body_bb)
        self.loop_stack.append((latch_bb, after_bb, self._loop_finally_base()))
        self._emit_stmts(stmt.body)
        self.loop_stack.pop()
        if not self._builder_block_is_terminated():
            self.builder.branch(latch_bb)

        self.builder.position_at_end(latch_bb)
        self._emit_thread_safepoint()
        self.builder.branch(header_bb)

        self.builder.position_at_end(err_bb)
        current_exc = self.builder.call(
            self.runtime["py_current_exception"],
            [],
            name=self._fresh("for.iter.cur_exc"),
        )
        stop_cls = self.builder.call(
            self.runtime["py_exc_builtin_class"],
            [ir.Constant(_I64, _BUILTIN_EXC_TAG["StopIteration"])],
            name=self._fresh("for.iter.stop_cls"),
        )
        match_i64 = self.builder.call(
            self.runtime["py_exc_matches"],
            [current_exc, stop_cls],
            name=self._fresh("for.iter.stop_match"),
        )
        is_stop = self.builder.icmp_signed(
            "!=",
            match_i64,
            ir.Constant(_I64, 0),
            name=self._fresh("for.iter.stop_i1"),
        )
        clear_bb = fn.append_basic_block(name=self._fresh("for.iter.clear"))
        propagate_bb = fn.append_basic_block(name=self._fresh("for.iter.propagate"))
        self.builder.cbranch(is_stop, clear_bb, propagate_bb)
        self.builder.position_at_end(clear_bb)
        self.builder.call(self.runtime["py_clear_exception"], [])
        self.builder.branch(after_bb)
        self.builder.position_at_end(propagate_bb)
        outer = prev_err_block or self._ensure_fn_err_exit()
        self.builder.branch(outer)

        self.builder.position_at_end(after_bb)

    def _emit_async_for(self, stmt: For) -> None:
        if isinstance(stmt.target, TupleExpr):
            stmt = self._normalise_for_tuple_target(stmt)
        if not isinstance(stmt.target, Name):
            raise NotImplementedError(
                "Layer 1 async for target must be a plain Name or TupleExpr"
            )
        class_hint = self._class_hint_for_expr(stmt.iter)
        if class_hint is None:
            raise NotImplementedError(
                "Layer 1 async for needs a pcc-native async iterator"
            )
        aiter_info = self._resolve_method_mro(class_hint, "__aiter__")
        if aiter_info is None:
            raise NotImplementedError("async iterator needs __aiter__")
        aiter_fn = aiter_info.methods.get("__aiter__")
        if aiter_fn is None:
            raise NotImplementedError("async iterator needs __aiter__")

        src_obj = self._emit_expr(stmt.iter)
        iterator = self._emit_direct_method_call(
            aiter_fn,
            src_obj,
            aiter_info,
            "__aiter__",
            (),
        )
        iterator_hint = class_hint
        aiter_fd = self.class_lowering._find_method_def(
            aiter_info.name,
            "__aiter__",
        )
        if aiter_fd is not None:
            ann_hint = self._class_hint_from_annotation(aiter_fd.return_ty)
            if ann_hint is not None:
                iterator_hint = ann_hint
        anext_info = self._resolve_method_mro(iterator_hint, "__anext__")
        if anext_info is None:
            raise NotImplementedError("async iterator needs __anext__")
        anext_fn = anext_info.methods.get("__anext__")
        if anext_fn is None:
            raise NotImplementedError("async iterator needs __anext__")

        target_ident = stmt.target.ident
        slot = _for_prepare_owned_object_target(
            self,
            target_ident,
            stmt.target.ty,
        )

        fn = self.current_function
        header_bb = fn.append_basic_block(name=self._fresh("async.for.next"))
        body_bb = fn.append_basic_block(name=self._fresh("async.for.body"))
        latch_bb = fn.append_basic_block(name=self._fresh("async.for.latch"))
        err_bb = fn.append_basic_block(name=self._fresh("async.for.err"))
        clear_bb = fn.append_basic_block(name=self._fresh("async.for.clear"))
        propagate_bb = fn.append_basic_block(name=self._fresh("async.for.propagate"))
        end_bb = fn.append_basic_block(name=self._fresh("async.for.end"))

        self.builder.branch(header_bb)
        self.builder.position_at_end(header_bb)
        prev_err_block = getattr(self, "_try_err_block", None)
        self._try_err_block = err_bb
        next_coro = self._emit_direct_method_call(
            anext_fn,
            iterator,
            anext_info,
            "__anext__",
            (),
        )
        item = self.builder.call(
            self.runtime["py_await"],
            [next_coro],
            name=self._fresh("async.for.item"),
        )
        self._emit_post_call_err_check(stmt.span)
        self._try_err_block = prev_err_block
        self.builder.branch(body_bb)

        self.builder.position_at_end(body_bb)
        _for_store_owned_target(self, target_ident, slot, item)
        self.loop_stack.append((latch_bb, end_bb, self._loop_finally_base()))
        self._emit_stmts(stmt.body)
        self.loop_stack.pop()
        if not self._builder_block_is_terminated():
            self.builder.branch(latch_bb)

        self.builder.position_at_end(latch_bb)
        self._emit_thread_safepoint()
        self.builder.branch(header_bb)

        self.builder.position_at_end(err_bb)
        current_exc = self.builder.call(
            self.runtime["py_current_exception"],
            [],
            name=self._fresh("async.for.cur_exc"),
        )
        stop_cls = self.builder.call(
            self.runtime["py_exc_builtin_class"],
            [ir.Constant(_I64, _BUILTIN_EXC_TAG["StopAsyncIteration"])],
            name=self._fresh("async.for.stop_cls"),
        )
        match_i64 = self.builder.call(
            self.runtime["py_exc_matches"],
            [current_exc, stop_cls],
            name=self._fresh("async.for.stop_match"),
        )
        is_stop = self.builder.icmp_signed(
            "!=",
            match_i64,
            ir.Constant(_I64, 0),
            name=self._fresh("async.for.stop_i1"),
        )
        self.builder.cbranch(is_stop, clear_bb, propagate_bb)

        self.builder.position_at_end(clear_bb)
        self.builder.call(self.runtime["py_clear_exception"], [])
        self.builder.branch(end_bb)

        self.builder.position_at_end(propagate_bb)
        outer = prev_err_block or self._ensure_fn_err_exit()
        self.builder.branch(outer)

        self.builder.position_at_end(end_bb)

    def _emit_for(self, stmt: For) -> None:
        if stmt.else_body:
            # Desugar for-else into a flag-guarded post-loop if:
            #
            #   for <t> in <iter>:
            #       <body>
            #   else:
            #       <else_body>
            #
            # becomes
            #
            #   __forelse_broke__<k> = False
            #   for <t> in <iter>:
            #       <body>  # any ``break`` in body also sets broke=True
            #   if not __forelse_broke__<k>:
            #       <else_body>
            #
            # Every ``break`` stmt inside ``<body>`` gets rewritten to
            # ``<broke> = True; break``. We don't descend into nested
            # For/While because those have their own iteration scope;
            # a break in a nested loop breaks the inner, not outer.
            from dataclasses import replace as _replace

            broke_name = self._fresh("forelse_broke")
            span = stmt.span
            broke_lit_false = BoolLit(span=span, ty=BoolType(name="bool"), value=False)
            broke_lit_true = BoolLit(span=span, ty=BoolType(name="bool"), value=True)
            broke_ref = Name(
                span=span,
                ty=BoolType(name="bool"),
                ident=broke_name,
            )
            set_broke_true = Assign(
                span=span,
                targets=(broke_ref,),
                value=broke_lit_true,
                annotation=BoolType(name="bool"),
            )

            def tag_breaks(stmts):
                out = []
                for s in stmts:
                    if isinstance(s, Break):
                        out.append(set_broke_true)
                        out.append(s)
                        continue
                    if isinstance(s, If):
                        out.append(
                            _replace(
                                s,
                                body=tag_breaks(s.body),
                                else_body=tag_breaks(s.else_body),
                            )
                        )
                        continue
                    if isinstance(s, Try):
                        # Avoid generator expression — pcc-py self-host
                        # mis-hoists ``for h in ...`` here, leaving ``h``
                        # unbound in the synthesized closure.
                        new_handlers_list = []
                        for h in s.handlers:
                            new_handlers_list.append(
                                _replace(h, body=tag_breaks(h.body))
                            )
                        new_handlers = tuple(new_handlers_list)
                        out.append(
                            _replace(
                                s,
                                body=tag_breaks(s.body),
                                else_body=tag_breaks(s.else_body),
                                finally_body=tag_breaks(s.finally_body),
                                handlers=new_handlers,
                            )
                        )
                        continue
                    out.append(s)
                return tuple(out)

            # Initialise the broke flag in the enclosing scope.
            ir_ty = self._map_type(BoolType(name="bool"))
            alloca = self._alloca_in_entry(
                ir_ty,
                name=f"{broke_name}.addr",
            )
            self.builder.store(ir.Constant(ir_ty, 0), alloca)
            self.env[broke_name] = (alloca, ir_ty, BoolType(name="bool"))

            new_stmt = _replace(
                stmt,
                body=tag_breaks(stmt.body),
                else_body=(),
            )
            self._emit_for(new_stmt)
            # Emit the post-loop ``if not broke:`` guard directly on
            # the native i1 flag. Routing this through a synthesized
            # Python-level bool comparison would box the values and can
            # accidentally use object comparison helpers.
            broke_val = self.builder.load(
                alloca,
                name=self._fresh("forelse.broke"),
            )
            should_else = self.builder.icmp_unsigned(
                "==",
                broke_val,
                ir.Constant(ir_ty, 0),
                name=self._fresh("forelse.should_else"),
            )
            else_bb = self.current_function.append_basic_block(
                name=self._fresh("forelse.else"),
            )
            end_bb = self.current_function.append_basic_block(
                name=self._fresh("forelse.end"),
            )
            self.builder.cbranch(should_else, else_bb, end_bb)
            self.builder.position_at_end(else_bb)
            self._emit_stmts(stmt.else_body)
            if not self._builder_block_is_terminated():
                self.builder.branch(end_bb)
            self.builder.position_at_end(end_bb)
            return
        if getattr(stmt, "is_async", False):
            self._emit_async_for(stmt)
            return
        # ``for (i, x) in enumerate(xs):`` — desugar to an indexed
        # iteration so the rest of this function never sees
        # ``enumerate`` as a special iter form.
        if self._for_iter_is_enumerate(stmt):
            stmt = self._normalise_for_enumerate(stmt)
        # ``for (a, b, ...) in zip(xs, ys, ...):`` — desugar to indexed
        # iteration over the shortest-length iterable. The strict=True
        # kwarg is accepted and dropped (pcc doesn't yet raise on
        # length mismatch, but CPython-matching min-length is close
        # enough for stdlib-style usage).
        if self._for_iter_is_zip(stmt):
            stmt = self._normalise_for_zip(stmt)
        # ``for k, v in d.items():`` can walk pcc-native dict entries
        # directly. The normal pcc-native ``.items()`` path materialises a
        # list of 2-tuples via py_dict_items(); this avoids that allocation
        # while keeping target names typed as DynType PyObject* values.
        dict_items_obj = _for_loop_dict_items_object(stmt.iter)
        if (
            dict_items_obj is not None
            and _for_loop_dict_items_target_names(stmt.target) is not None
            and len(self._generator_ctx_stack) == 0
        ):
            return self._emit_for_dict_items_direct(stmt, dict_items_obj)
        # ``for (a, b) in items:`` — normalise by introducing a fresh
        # scalar target and prepending an unpack assign to the loop body.
        if _for_loop_is_tuple_expr(stmt.target):
            stmt = self._normalise_for_tuple_target(stmt)
        if not _for_loop_is_name(stmt.target):
            raise NotImplementedError(
                "Layer 1 for-loop target must be a plain Name or a "
                "TupleExpr of Names"
            )
        # ``for <name> in range(...)`` stays on the L1 fast path.
        is_range_call = _for_loop_is_call_name(stmt.iter, ("range", "xrange"))
        # Inside a generator, range(...) must NOT use the inline induction
        # fast path: that path keeps its loop counter in a raw entry-block
        # alloca which is not part of the persisted generator frame, so after
        # a ``yield`` the resume re-enters with a fresh (reset) counter and the
        # loop terminates after the first item. Materialise the range as a list
        # and drive it through the resumable object-iterator path, whose
        # iterator pointer IS stored in the per-loop frame slot and reloaded on
        # each resume (the same mechanism that makes list/tuple iteration work
        # inside generators). Regression:
        # tests/python/test_python_generator_parity.py
        # ::test_generator_range_loop_resumes.
        if is_range_call and len(self._generator_ctx_stack) > 0:
            range_list = self._emit_range_value_call(stmt.iter)
            return self._emit_for_obj_iterator(stmt, range_list)
        if not is_range_call:
            # CPython iterable? Use PyObject_GetIter + PyIter_Next.
            iter_val = self._emit_expr(stmt.iter)
            if iter_val in getattr(self, "_cpy_values", ()):
                return self._emit_for_cpython_iter(stmt, iter_val)
            if len(self._generator_ctx_stack) > 0 and isinstance(
                stmt.iter.ty,
                (ListType, TupleType, DictType, SetType, StrType, DynType),
            ):
                return self._emit_for_obj_iterator(stmt, iter_val)
            # ListType / TupleType iteration via index: length from
            # ``py_{list,tuple}_len``, element via ``py_{list,tuple}_get``.
            iter_ty = stmt.iter.ty
            if isinstance(iter_ty, (ListType, TupleType)):
                return self._emit_for_list_index(
                    stmt,
                    iter_val,
                    iter_ty,
                )
            # DictType: ``for k in d:`` iterates keys. Materialise
            # ``py_dict_keys(d)`` (returns a list) and reuse the
            # list-index loop with the key type.
            if isinstance(iter_ty, DictType):
                keys_val = self.builder.call(
                    self.runtime["py_dict_keys"],
                    [iter_val],
                    name=self._fresh("for.dict.keys"),
                )
                self._gc_release_if_owned(iter_val, stmt.iter)
                synthetic_ty = ListType(name="list", elem=iter_ty.key)
                return self._emit_for_list_index(
                    stmt,
                    keys_val,
                    synthetic_ty,
                    source_owned=True,
                )
            # StrType: ``for ch in s:`` iterates codepoints. Slice each
            # index into a 1-char str — keeps the whole loop libpython-
            # free. The bound target is typed str.
            if isinstance(iter_ty, StrType):
                return self._emit_for_str_chars(stmt, iter_val)
            class_hint = self._class_hint_for_expr(stmt.iter)
            if (
                class_hint is not None
                and self._resolve_method_mro(class_hint, "__next__") is not None
            ):
                return self._emit_for_native_iterator(
                    stmt,
                    iter_val,
                    class_hint,
                )
            # DynType: fall back to ``py_obj_len`` + ``py_obj_getitem``
            # — works for any pcc-native sequence (list, tuple, dict
            # keys, etc.) and stays libpython-free. The bound target is
            # tagged DynType, so subsequent uses see a PyObject*.
            if isinstance(iter_ty, DynType):
                return self._emit_for_obj_iterator(stmt, iter_val)
            if isinstance(iter_val.type, ir.PointerType):
                return self._emit_for_obj_iterator(stmt, iter_val)
            raise NotImplementedError(
                "Layer 1 only handles 'for <name> in range(...)', a "
                "CPython-backed iterable, a list/tuple/dict/dyn "
                "container; other iterables need L3"
            )
        call = stmt.iter
        if call.kwargs:
            raise NotImplementedError("Layer 1 range() has no keyword args")
        if len(call.args) == 1:
            start_val: ir.Value = ir.Constant(_I64, 0)
            stop_val = self._emit_expr_as_i64(call.args[0])
            step_val: ir.Value = ir.Constant(_I64, 1)
        elif len(call.args) == 2:
            start_val = self._emit_expr_as_i64(call.args[0])
            stop_val = self._emit_expr_as_i64(call.args[1])
            step_val = ir.Constant(_I64, 1)
        elif len(call.args) == 3:
            start_val = self._emit_expr_as_i64(call.args[0])
            stop_val = self._emit_expr_as_i64(call.args[1])
            step_val = self._emit_expr_as_i64(call.args[2])
        else:
            raise L1CodegenError(f"range() takes 1–3 args; got {len(call.args)}")

        # The range induction counter is compiler state, not the Python-visible
        # loop target.  Keep them separate on every path.  Reusing a fresh
        # target's alloca for the counter made assignments in the loop body
        # change iteration itself (``for i in range(3): i = 100``), and a
        # nested same-name loop overwrote the outer counter.  It also published
        # ``start`` on a zero-iteration edge even though the target had never
        # been bound by the loop.
        target_name = stmt.target.ident
        target_ty = stmt.target.ty
        existing = self.env.get(target_name)
        target_storage_ty = self._storage_ir_type(target_ty)
        boxed_range_target = (
            self._int_exprs_are_boxed()
            or isinstance(target_storage_ty, ir.PointerType)
            or (existing is not None and isinstance(existing[1], ir.PointerType))
        )
        counter_alloca = self._alloca_in_entry(
            _I64,
            name=f"{target_name}.range.addr",
        )
        if boxed_range_target:
            target_slot = _for_prepare_owned_object_target(
                self,
                target_name,
                target_ty,
            )
            target_alloca = target_slot[0]
        else:
            if existing is None:
                target_alloca = self._alloca_in_entry(
                    _I64,
                    name=f"{target_name}.addr",
                )
                self.env[target_name] = (
                    target_alloca,
                    _I64,
                    IntType(name="int"),
                )
            else:
                target_alloca, ir_ty, _decl = existing
                if not self._ir_type_matches(ir_ty, _I64):
                    raise L1CodegenError(
                        "range for-target representation join was not boxed "
                        f"for {target_name!r}"
                    )
        self._clear_cpy_for_target_binding(target_name)
        self.builder.store(start_val, counter_alloca)

        fn = self.current_function
        cond_bb = fn.append_basic_block(name=self._fresh("for.cond"))
        body_bb = fn.append_basic_block(name=self._fresh("for.body"))
        step_bb = fn.append_basic_block(name=self._fresh("for.step"))
        end_bb = fn.append_basic_block(name=self._fresh("for.end"))

        # Hoist step as a stable SSA value — we already have it in
        # ``step_val`` so no further work.
        self.builder.branch(cond_bb)

        self.builder.position_at_end(cond_bb)
        cur = self.builder.load(counter_alloca, name=self._fresh(target_name))
        # Condition depends on step sign: positive step -> i<stop,
        # negative step -> i>stop. We emit both and select.
        zero64 = ir.Constant(_I64, 0)
        step_pos = self.builder.icmp_signed(
            ">", step_val, zero64, name=self._fresh("step_pos")
        )
        cond_pos = self.builder.icmp_signed(
            "<", cur, stop_val, name=self._fresh("fwd_cmp")
        )
        cond_neg = self.builder.icmp_signed(
            ">", cur, stop_val, name=self._fresh("bwd_cmp")
        )
        cond_i1 = self.builder.select(
            step_pos, cond_pos, cond_neg, name=self._fresh("for_cond")
        )
        self.builder.cbranch(cond_i1, body_bb, end_bb)

        self.loop_stack.append((step_bb, end_bb, self._loop_finally_base()))
        self.builder.position_at_end(body_bb)
        if boxed_range_target:
            cur_body = self.builder.load(
                counter_alloca,
                name=self._fresh(f"{target_name}.body"),
            )
            cur_obj = self.builder.call(
                self.runtime["py_int_from_i64"],
                [cur_body],
                name=self._fresh("range.int.obj"),
            )
            _for_store_owned_target(
                self,
                target_name,
                target_slot,
                cur_obj,
            )
        else:
            cur_body = self.builder.load(
                counter_alloca,
                name=self._fresh(f"{target_name}.body"),
            )
            self.builder.store(cur_body, target_alloca)
        self._emit_stmts(stmt.body)
        if not self._builder_block_is_terminated():
            self.builder.branch(step_bb)
        self.loop_stack.pop()

        self.builder.position_at_end(step_bb)
        cur2 = self.builder.load(counter_alloca, name=self._fresh(target_name))
        next_val = self.builder.add(cur2, step_val, name=self._fresh("next"))
        self.builder.store(next_val, counter_alloca)
        self._emit_thread_safepoint()
        self.builder.branch(cond_bb)

        self.builder.position_at_end(end_bb)

    def _emit_range_loop(
        self,
        target: Name,
        call: Call,
        kind: str,
        container: ir.Value,
        generators: list,
        tuple_unpacks: list,
        idx: int,
        elt_expr,
        key_expr,
        val_expr,
    ) -> None:
        if call.kwargs:
            raise NotImplementedError(
                "range() with keyword args not supported in comprehension"
            )
        if len(call.args) == 1:
            start_val: ir.Value = ir.Constant(_I64, 0)
            stop_val = self._emit_expr_as_i64(call.args[0])
            step_val: ir.Value = ir.Constant(_I64, 1)
        elif len(call.args) == 2:
            start_val = self._emit_expr_as_i64(call.args[0])
            stop_val = self._emit_expr_as_i64(call.args[1])
            step_val = ir.Constant(_I64, 1)
        elif len(call.args) == 3:
            start_val = self._emit_expr_as_i64(call.args[0])
            stop_val = self._emit_expr_as_i64(call.args[1])
            step_val = self._emit_expr_as_i64(call.args[2])
        else:
            raise L1CodegenError(f"range() takes 1–3 args; got {len(call.args)}")
        target_name = target.ident
        existing = self.env.get(target_name)
        if existing is None:
            alloca = self._alloca_in_entry(_I64, name=f"{target_name}.addr")
            self.env[target_name] = (alloca, _I64, IntType(name="int"))
        else:
            alloca, ir_ty, _decl = existing
            if ir_ty is not _I64:
                # Python comprehension targets rebind like normal locals. A
                # previous object-typed binding should not poison a later
                # range fast path.
                alloca = self._alloca_in_entry(_I64, name=f"{target_name}.addr")
                self.env[target_name] = (alloca, _I64, IntType(name="int"))
        self.builder.store(start_val, alloca)
        fn = self.current_function
        cond_bb = fn.append_basic_block(name=self._fresh("comp.cond"))
        body_bb = fn.append_basic_block(name=self._fresh("comp.body"))
        step_bb = fn.append_basic_block(name=self._fresh("comp.step"))
        end_bb = fn.append_basic_block(name=self._fresh("comp.end"))
        self.builder.branch(cond_bb)

        self.builder.position_at_end(cond_bb)
        cur = self.builder.load(alloca, name=self._fresh(target_name))
        zero64 = ir.Constant(_I64, 0)
        step_pos = self.builder.icmp_signed(
            ">",
            step_val,
            zero64,
            name=self._fresh("step_pos"),
        )
        cond_pos = self.builder.icmp_signed(
            "<",
            cur,
            stop_val,
            name=self._fresh("fwd_cmp"),
        )
        cond_neg = self.builder.icmp_signed(
            ">",
            cur,
            stop_val,
            name=self._fresh("bwd_cmp"),
        )
        cond_i1 = self.builder.select(
            step_pos,
            cond_pos,
            cond_neg,
            name=self._fresh("comp_cond"),
        )
        self.builder.cbranch(cond_i1, body_bb, end_bb)

        self.builder.position_at_end(body_bb)
        self._emit_comprehension_after_bind(
            kind,
            container,
            generators,
            tuple_unpacks,
            idx,
            elt_expr,
            key_expr,
            val_expr,
        )
        if not self._builder_block_is_terminated():
            self.builder.branch(step_bb)

        self.builder.position_at_end(step_bb)
        cur2 = self.builder.load(alloca, name=self._fresh(target_name))
        next_val = self.builder.add(cur2, step_val, name=self._fresh("next"))
        self.builder.store(next_val, alloca)
        self._emit_thread_safepoint()
        self.builder.branch(cond_bb)

        self.builder.position_at_end(end_bb)

    def _emit_cpy_iter_loop(
        self,
        target: Name,
        iter_src: ir.Value,
        kind: str,
        container: ir.Value,
        generators: list,
        tuple_unpacks: list,
        idx: int,
        elt_expr,
        key_expr,
        val_expr,
    ) -> None:
        """Shared CPython-iteration loop for comprehensions."""
        target_name = target.ident
        fn = self.current_function
        iter_obj = self.builder.call(
            self.runtime["py_cpy_iter"],
            [iter_src],
            name=self._fresh("comp.iter"),
        )
        cond_bb = fn.append_basic_block(name=self._fresh("comp.cond"))
        body_bb = fn.append_basic_block(name=self._fresh("comp.body"))
        latch_bb = fn.append_basic_block(name=self._fresh("comp.latch"))
        end_bb = fn.append_basic_block(name=self._fresh("comp.end"))
        self.builder.branch(cond_bb)
        self.builder.position_at_end(cond_bb)
        nxt = self.builder.call(
            self.runtime["py_cpy_iter_next"],
            [iter_obj],
            name=self._fresh("comp.next"),
        )
        null_p = ir.Constant(nxt.type, None)
        is_done = self.builder.icmp_unsigned(
            "==",
            nxt,
            null_p,
            name=self._fresh("comp.done"),
        )
        self.builder.cbranch(is_done, end_bb, body_bb)
        self.builder.position_at_end(body_bb)
        existing = self.env.get(target_name)
        if existing is None:
            alloca = self._alloca_in_entry(
                nxt.type,
                name=f"{target_name}.addr",
            )
            self.env[target_name] = (alloca, nxt.type, DynType(name="dyn"))
            if not hasattr(self, "_cpy_env_flags"):
                self._cpy_env_flags = {}
            self._cpy_env_flags[target_name] = True
        else:
            alloca, _, _ = existing
        self.builder.store(nxt, alloca)
        self._emit_comprehension_after_bind(
            kind,
            container,
            generators,
            tuple_unpacks,
            idx,
            elt_expr,
            key_expr,
            val_expr,
        )
        if not self._builder_block_is_terminated():
            self.builder.branch(latch_bb)
        self.builder.position_at_end(latch_bb)
        self._emit_thread_safepoint()
        self.builder.branch(cond_bb)
        self.builder.position_at_end(end_bb)
