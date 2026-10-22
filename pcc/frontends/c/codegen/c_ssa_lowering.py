"""Internal SSA-to-LLVM lowering for the C frontend.

This mixin owns the complete SSA lowering seam. C integer signedness and
usual-arithmetic-conversion policy deliberately remain on CCodeGenerator;
SSA lowering calls that single policy owner through self.
"""

from pcc.ir.compat import ir_c as ir

from pcc.frontends.c.codegen.c_types import get_ir_type_from_names, int8_t, int32_t, int64_t
from pcc.frontends.c.ast import c_ast as c_ast


class CSSALoweringMixin:
    def _get_var_alloc_strategy(self, var_name):
        """Query PassContext for the allocation strategy of a variable."""
        if self._pass_ctx is None:
            return None
        func_name = self._function_display_name
        if func_name is None:
            return None
        func_info = self._pass_ctx.functions.get(func_name)
        if func_info is None:
            return None
        var_info = func_info.var_infos.get(var_name)
        if var_info is None:
            return None
        return var_info.alloc_strategy

    def _should_ssa_promote(self, var_name):
        """Check if a variable should be promoted to SSA (no alloca).

        Returns True only for variables the escape analysis proved safe:
        single-def, non-escaping scalars. _safe_load already handles
        non-pointer values by returning them as-is, so this works
        transparently with the rest of codegen.
        """
        try:
            from pcc.frontends.c.passes.context import AllocStrategy
        except ImportError:
            return False
        strategy = self._get_var_alloc_strategy(var_name)
        return strategy == AllocStrategy.SSA

    def _ssa_ir_type(self, type_name):
        if not type_name:
            return int32_t
        resolved = self._resolve_type_str(type_name)
        if isinstance(resolved, ir.Type):
            return resolved
        if type_name.endswith("*"):
            depth = 0
            base = type_name
            while base.endswith("*"):
                depth += 1
                base = base[:-1].strip()
            ir_type = self._ssa_ir_type(base)
            if isinstance(ir_type, ir.VoidType):
                ir_type = int8_t
            for _ in range(depth):
                ir_type = ir.PointerType(ir_type)
            return ir_type
        if isinstance(resolved, str):
            resolved_text = resolved.strip()
            if resolved_text.startswith("struct "):
                tag_name = resolved_text.split(" ", 1)[1]
                tag_key = self._tag_type_key(tag_name)
                if tag_key in self.env:
                    return self.env[tag_key][0]
                return self.module.context.get_identified_type(
                    self._aggregate_type_name("struct", tag_name)
                )
            if resolved_text.startswith("union "):
                tag_name = resolved_text.split(" ", 1)[1]
                tag_key = self._tag_type_key(tag_name)
                if tag_key in self.env:
                    return self.env[tag_key][0]
                return self.module.context.get_identified_type(
                    self._aggregate_type_name("union", tag_name)
                )
            if resolved_text.startswith("enum "):
                return int32_t
            resolved_names = resolved_text.split()
        else:
            resolved_names = resolved
        return get_ir_type_from_names(resolved_names, str(self.module.triple))

    def _ssa_resolve_int_typedef(self, type_name):
        """Walk typedef chain for integer typedef names so `size_t` /
        `uint32_t` / `lu_byte` etc. reach their canonical integer
        spelling. Pointer/array forms pass through unchanged. Used by
        the SSA comparison path where the common promoted type must be
        correct for signedness.
        """
        if not type_name or type_name.endswith("*"):
            return type_name
        seen = set()
        current = type_name
        for _ in range(16):
            if current in seen:
                break
            seen.add(current)
            tokens = current.split()
            if len(tokens) == 1:
                node = self._lookup_typedef_ast_type(tokens[0])
                if node is not None:
                    # Unwrap TypeDecl → IdentifierType
                    import pcc.frontends.c.ast.c_ast as _cast
                    inner = node
                    if isinstance(inner, _cast.TypeDecl):
                        inner = inner.type
                    if isinstance(inner, _cast.IdentifierType):
                        current = " ".join(inner.names)
                        continue
            break
        return current

    def _ssa_is_unsigned_type(self, type_name):
        if not type_name or type_name.endswith("*"):
            return False
        resolved = self._resolve_type_str(type_name)
        if isinstance(resolved, list):
            return self._is_unsigned_type_names(resolved)
        if isinstance(resolved, str):
            names = resolved.split()
            return self._is_unsigned_type_names(names if len(names) > 1 else resolved)
        return False

    # ------------------------------------------------------------------
    # Phase 2: SSA → LLVM IR lowering
    #
    # LLVM reference boundary:
    #   PromoteMemoryToRegister.cpp achieves the same result bottom-up:
    #   alloca/load/store → IDF phi placement → renaming pass.
    #   We go top-down: C AST → internal SSA (builder.py already placed
    #   phis via structured CFG construction) → LLVM values + phi nodes.
    #   The output is equivalent: promotable scalar locals become LLVM
    #   SSA values with phi nodes at join points, no alloca needed.
    #
    # Subset intentionally narrower than LLVM mem2reg:
    #   - only functions the SSA builder accepted (structured scalar CFG)
    #   - scalar integers plus direct-ID calls and pointer-typed values that
    #     stay inside the builder/lowering subset
    #   - no attempt yet to model LLVM mem2reg's full promotable-allocation
    #     surface, address-taken locals, or arbitrary CFG / MemorySSA cases
    # ------------------------------------------------------------------

    def _has_ssa_function(self, func_name):
        """Check if internal SSA IR is available for a function."""
        if self._pass_ctx is None:
            return False
        return func_name in getattr(self._pass_ctx, "ssa_functions", {})

    def _lower_ssa_function(self, func_name, return_type):
        """Lower a function from internal SSA IR directly to LLVM IR.

        Replaces _codegen_compound_items for eligible functions.
        Returns True if lowering succeeded, False to fall back to AST codegen.
        """
        from pcc.frontends.c.ssa.ir import SSABinaryOp, SSABlock, SSABranch, SSACast, SSACall, SSAConstant, SSAFieldAddr, SSAJump, SSALoad, SSAParam, SSAPhi, SSAReturn, SSASwitch, SSAUndef, SSAStore, SSAUnaryOp

        ssa_func = self._pass_ctx.ssa_functions[func_name]

        # --- Phase 3 integration: consume SCCP results ---
        # If SCCP proved values constant or branches foldable, use that
        # during lowering to emit simpler code.
        sccp_constants: dict[str, int] = {}
        sccp_folded_branches: dict[str, str] = {}
        sccp_reachable: set[str] | None = None
        sccp_result = getattr(self._pass_ctx, "ssa_sccp_results", {}).get(func_name)
        if sccp_result is not None:
            # Only consume safe-fold constants here. Unsafe folds (ordered
            # compares, arithmetic under unsigned wrap, shifts) yield
            # Python ints that would be wrong for the unsigned cases at
            # LLVM level — see unsigned_cmp.c regression.
            sccp_constants = sccp_result.safe_constant_value_names()
            sccp_folded_branches = sccp_result.folded_branches
            sccp_reachable = sccp_result.reachable_blocks

        ssa_value_types = {}
        for param in ssa_func.params:
            ssa_value_types[param.name] = param.type_name
        for block in ssa_func.blocks:
            for inst in block.instructions:
                ssa_value_types[inst.name] = getattr(inst, "type_name", "int")

        # --- Step 1: Create LLVM basic blocks ---
        llvm_blocks: dict[str, ir.Block] = {}
        for ssa_block in ssa_func.blocks:
            if ssa_block.name == ssa_func.entry_block:
                # Entry block already exists (created by codegen_FuncDef)
                llvm_blocks[ssa_block.name] = self.builder.block
            else:
                llvm_blocks[ssa_block.name] = self.function.append_basic_block(
                    ssa_block.name,
                )

        # --- Step 2: Map SSA params to LLVM function args ---
        value_map: dict[str, ir.Value] = {}
        for i, param in enumerate(ssa_func.params):
            if i < len(self.function.args):
                value_map[param.name] = self.function.args[i]

        # --- Step 3: Lower each block ---
        # First pass: create phi nodes (they must come first in LLVM blocks)
        phi_nodes: dict[str, ir.PhiInstr] = {}
        for ssa_block in ssa_func.blocks:
            self.builder.position_at_end(llvm_blocks[ssa_block.name])
            for inst in ssa_block.instructions:
                if isinstance(inst, SSAPhi):
                    phi = self.builder.phi(
                        self._ssa_ir_type(getattr(inst, "type_name", "int")),
                        name=inst.name,
                    )
                    phi_nodes[inst.name] = phi
                    value_map[inst.name] = phi

        # Pre-populate value_map with SCCP-proven constants so that
        # downstream instructions and terminators see folded values.
        for ssa_name, const_val in sccp_constants.items():
            if ssa_name not in value_map:
                value_map[ssa_name] = self._ssa_constant_ir_value(
                    ssa_value_types.get(ssa_name, "int"),
                    const_val,
                )

        # Compute reverse postorder so every instruction's SSA operands
        # have been emitted into value_map by the time the user block is
        # lowered. Short-circuit chains and complex loop shapes create
        # blocks in outside-in creation order but execute inside-out —
        # using ssa_func.blocks order makes the outer block reference
        # inner defs that are not yet in value_map and fall back to 0.
        def _rpo_block_order(func):
            name_to_block = {b.name: b for b in func.blocks}
            visited: set[str] = set()
            post: list = []

            def visit(name):
                if name in visited or name not in name_to_block:
                    return
                visited.add(name)
                blk = name_to_block[name]
                for succ in blk.successors:
                    visit(succ)
                post.append(blk)

            visit(func.entry_block)
            ordered = list(reversed(post))
            for b in func.blocks:
                if b.name not in visited:
                    ordered.append(b)
            return ordered

        block_order = _rpo_block_order(ssa_func)

        # Second pass: lower instructions and terminators
        emitted_successors: dict[str, set[str]] = {}
        for ssa_block in block_order:
            # Skip unreachable blocks (SCCP dead code)
            if sccp_reachable is not None and ssa_block.name not in sccp_reachable:
                if ssa_block.name in llvm_blocks:
                    self.builder.position_at_end(llvm_blocks[ssa_block.name])
                    self.builder.unreachable()
                emitted_successors[ssa_block.name] = set()
                continue

            self.builder.position_at_end(llvm_blocks[ssa_block.name])

            for inst in ssa_block.instructions:
                if isinstance(inst, SSAPhi):
                    continue  # already created above

                # SCCP constant fold: if the result is already known
                # constant, skip emitting the instruction.
                if inst.name in sccp_constants:
                    value_map[inst.name] = self._ssa_constant_ir_value(
                        getattr(inst, "type_name", "int"),
                        sccp_constants[inst.name],
                    )
                    continue

                val = self._lower_ssa_instruction(
                    inst, value_map,
                )
                if val is not None:
                    value_map[inst.name] = val

            # Lower terminator
            term = ssa_block.terminator
            if isinstance(term, SSAReturn):
                if term.value is None:
                    self.builder.ret_void()
                else:
                    ret_val = self._resolve_ssa_value(
                        term.value, value_map,
                    )
                    if isinstance(return_type, ir.VoidType):
                        self.builder.ret_void()
                    else:
                        if ret_val.type != return_type:
                            ret_val = self._ssa_convert(
                                ret_val,
                                return_type,
                                source_type_name=getattr(term.value, "type_name", None),
                            )
                        self.builder.ret(ret_val)
            elif isinstance(term, SSAJump):
                self.builder.branch(llvm_blocks[term.target])
                emitted_successors[ssa_block.name] = {term.target}
            elif isinstance(term, SSABranch):
                # Jump-threading: if SCCP folded this branch, emit
                # unconditional jump to the known target.
                folded_target = sccp_folded_branches.get(ssa_block.name)
                if folded_target is not None and folded_target in llvm_blocks:
                    self.builder.branch(llvm_blocks[folded_target])
                    emitted_successors[ssa_block.name] = {folded_target}
                else:
                    cond = self._resolve_ssa_value(
                        term.condition, value_map,
                    )
                    cond_i1 = self._to_bool(cond)
                    self.builder.cbranch(
                        cond_i1,
                        llvm_blocks[term.true_target],
                        llvm_blocks[term.false_target],
                    )
                    emitted_successors[ssa_block.name] = {
                        term.true_target,
                        term.false_target,
                    }
            elif isinstance(term, SSASwitch):
                folded_target = sccp_folded_branches.get(ssa_block.name)
                if folded_target is not None and folded_target in llvm_blocks:
                    self.builder.branch(llvm_blocks[folded_target])
                    emitted_successors[ssa_block.name] = {folded_target}
                else:
                    switch_val = self._resolve_ssa_value(term.value, value_map)
                    default_block = llvm_blocks[term.default_target]
                    sw = self.builder.switch(switch_val, default_block)
                    for case_const, target_name in term.cases:
                        case_ir = ir.Constant(switch_val.type, case_const)
                        sw.add_case(case_ir, llvm_blocks[target_name])
                    emitted_successors[ssa_block.name] = {
                        term.default_target, *(t for _, t in term.cases),
                    }
            else:
                emitted_successors[ssa_block.name] = set()

        # --- Step 4: Fill in phi incoming values ---
        # `_resolve_ssa_value` may emit a `load` instruction when the
        # value is an SSAGlobalRef or string literal — that load must
        # land in the PREDECESSOR block (so it dominates the phi), not
        # at wherever the builder happened to be after step 3. Position
        # just before the predecessor's terminator before each resolve.
        for ssa_block in ssa_func.blocks:
            for inst in ssa_block.instructions:
                if isinstance(inst, SSAPhi):
                    phi = phi_nodes[inst.name]
                    for pred_name, ssa_val in inst.incomings:
                        if ssa_block.name not in emitted_successors.get(pred_name, set()):
                            continue
                        pred_llvm = llvm_blocks.get(pred_name)
                        if pred_llvm is not None and pred_llvm.instructions:
                            term = pred_llvm.instructions[-1]
                            self.builder.position_before(term)
                        elif pred_llvm is not None:
                            self.builder.position_at_end(pred_llvm)
                        incoming_val = self._resolve_ssa_value(
                            ssa_val, value_map,
                        )
                        phi.add_incoming(incoming_val, llvm_blocks[pred_name])

        return True

    def _lower_ssa_instruction(self, inst, value_map):
        """Lower a single SSA instruction to an LLVM value."""
        from pcc.frontends.c.ssa.ir import SSABinaryOp, SSACall, SSACast, SSAFieldAddr, SSAFieldExtract, SSAGlobalRef, SSALoad, SSAStackAlloc, SSAStore, SSAUnaryOp

        if isinstance(inst, SSABinaryOp):
            left = self._resolve_ssa_value(inst.left, value_map)
            right = self._resolve_ssa_value(inst.right, value_map)
            # Pointer arithmetic is a special case: keep the integer index as
            # an integer and let _lower_ssa_binop translate it to GEP.
            is_pointer_arith = (
                inst.op in {"+", "-"}
                and (
                    (
                        isinstance(left.type, ir.PointerType)
                        and isinstance(right.type, ir.IntType)
                    )
                    or (
                        inst.op == "+"
                        and isinstance(left.type, ir.IntType)
                        and isinstance(right.type, ir.PointerType)
                    )
                )
            )
            # Ensure both operands have the same type for the non-pointer
            # case. Widen BOTH toward the SSA-declared result type rather
            # than blindly matching left's type: when `int - long` yields
            # `long`, narrowing the long operand to int produces wrong
            # values (see gcc_torture nestfunc-4.c).
            result_type_name = getattr(inst, "type_name", "int")
            result_ir_type = self._ssa_ir_type(result_type_name)
            left_tn = getattr(inst.left, "type_name", "int")
            right_tn = getattr(inst.right, "type_name", "int")
            is_cmp = inst.op in {"==", "!=", "<", ">", "<=", ">="}
            if (
                not is_pointer_arith
                and isinstance(result_ir_type, ir.IntType)
                and not is_cmp
            ):
                if isinstance(left.type, ir.IntType) and left.type != result_ir_type:
                    left = self._ssa_convert(
                        left,
                        result_ir_type,
                        source_type_name=left_tn,
                    )
                # Shift: C11 6.5.7p3 promotes both operands independently,
                # so the shift amount also widens to the result type. LLVM
                # requires shift operands to share a type.
                if isinstance(right.type, ir.IntType) and right.type != result_ir_type:
                    right = self._ssa_convert(
                        right,
                        result_ir_type,
                        source_type_name=right_tn,
                    )
            elif (
                is_cmp
                and isinstance(left.type, ir.IntType)
                and isinstance(right.type, ir.IntType)
            ):
                # Comparison result is `int` but both operands must be
                # compared at their common promoted type per C11 6.3.1.8
                # (usual arithmetic conversions) — e.g. `unsigned short ==
                # signed char` promotes both to int, making 65535 != -1
                # instead of 0xffff == 0xffff (gcc_torture 20080813-1.c).
                # Resolve typedef spellings (e.g. `size_t` → `unsigned long`)
                # on the operand sides first so the rank table picks the
                # right common type (test_size_t_still_uses_unsigned_*).
                from pcc.frontends.c.ssa.builder import SSABuilder as _B
                resolver = getattr(self, "_ssa_resolve_int_typedef", None)
                if resolver is not None:
                    class _V:
                        def __init__(self, tn):
                            self.type_name = tn
                    common_name = _B._binary_result_type(
                        "+",
                        _V(resolver(getattr(inst.left, "type_name", "int"))),
                        _V(resolver(getattr(inst.right, "type_name", "int"))),
                        target_triple=str(self.module.triple),
                    )
                else:
                    common_name = _B._binary_result_type("+", inst.left, inst.right, target_triple=str(self.module.triple))
                common_ir = self._ssa_ir_type(common_name)
                if isinstance(common_ir, ir.IntType):
                    # Decide signedness of the compare using the common
                    # (post-promotion) type, so the same rules in
                    # `_binary_result_type` apply here. `_ssa_is_unsigned_type`
                    # resolves typedefs (e.g. `size_t` → unsigned long)
                    # and handles qualified names. A mixed
                    # `signed vs unsigned` operand pair converges on the
                    # correct C11 6.3.1.8 common type via the rank-based
                    # _binary_result_type. Using only the common type
                    # avoids the "any operand unsigned → do unsigned"
                    # mistake (test_unsigned_loads covers both failing
                    # and still-unsigned shapes).
                    if left.type != common_ir:
                        left = self._ssa_convert(
                            left, common_ir, source_type_name=left_tn,
                        )
                    if right.type != common_ir:
                        right = self._ssa_convert(
                            right, common_ir, source_type_name=right_tn,
                        )
                    left_tn = right_tn = common_name
            elif left.type != right.type and not is_pointer_arith:
                right = self._ssa_convert(
                    right,
                    left.type,
                    source_type_name=right_tn,
                )
            return self._lower_ssa_binop(
                inst.op,
                left,
                right,
                inst.name,
                result_type_name=result_type_name,
                left_type_name=left_tn,
                right_type_name=right_tn,
            )

        if isinstance(inst, SSAUnaryOp):
            operand = self._resolve_ssa_value(inst.operand, value_map)
            return self._lower_ssa_unop(
                inst.op,
                operand,
                inst.name,
                result_type_name=getattr(inst, "type_name", "int"),
                operand_type_name=getattr(inst.operand, "type_name", "int"),
            )

        if isinstance(inst, SSACast):
            operand = self._resolve_ssa_value(inst.operand, value_map)
            return self._lower_ssa_cast(
                operand,
                result_type_name=getattr(inst, "type_name", "int"),
                operand_type_name=getattr(inst.operand, "type_name", "int"),
            )

        if isinstance(inst, SSALoad):
            # If the load's base is a bare SSAGlobalRef and the load
            # targets the *same* scalar type the global stores, we need
            # the global's ADDRESS (pointer) rather than the
            # auto-loaded value that `_resolve_ssa_value(SSAGlobalRef)`
            # returns — that happens when the SSA builder snapshots a
            # scalar global into a decl initializer (`long tmp = level;`
            # in nestfunc-4.c). For pointer globals where the load's
            # result differs from the base's declared pointee (e.g.
            # `*p` where `p: int*` loads `int`), we still need the
            # default two-step resolution: load `p` first to get the
            # pointer, then load through it. Same for arrays.
            from pcc.frontends.c.ssa.ir import SSAGlobalRef as _SSAGlobalRef
            base = None
            if isinstance(inst.base, _SSAGlobalRef):
                base_type_name = getattr(inst.base, "type_name", "")
                result_type_name = getattr(inst, "type_name", "int")
                try:
                    value_type, binding = self.lookup(inst.base.symbol_name)
                except Exception:
                    value_type, binding = None, None
                if (
                    binding is not None
                    and not isinstance(value_type, ir.ArrayType)
                    and isinstance(getattr(binding, "type", None), ir.PointerType)
                    and not base_type_name.endswith("*")
                    and base_type_name == result_type_name
                ):
                    base = binding
            if base is None:
                base = self._resolve_ssa_value(inst.base, value_map)
            index = (
                self._resolve_ssa_value(inst.index, value_map)
                if inst.index is not None
                else None
            )
            index_type_name = (
                getattr(inst.index, "type_name", None)
                if inst.index is not None
                else None
            )
            return self._lower_ssa_load(
                base,
                index,
                result_type_name=getattr(inst, "type_name", "int"),
                index_type_name=index_type_name,
            )

        if isinstance(inst, SSAFieldAddr):
            base = self._resolve_ssa_value(inst.base, value_map)
            return self._lower_ssa_field_addr(
                base,
                inst.field_name,
                result_type_name=getattr(inst, "type_name", "int"),
            )

        if isinstance(inst, SSAFieldExtract):
            base = self._resolve_ssa_value(inst.base, value_map)
            return self._lower_ssa_field_extract(
                base,
                inst.field_name,
                result_type_name=getattr(inst, "type_name", "int"),
            )

        if isinstance(inst, SSAGlobalRef):
            return self._resolve_ssa_value(inst, value_map)

        if isinstance(inst, SSAStackAlloc):
            elem_type = self._ssa_ir_type(getattr(inst, "elem_type_name", "int"))
            size = None if getattr(inst, "count", 1) == 1 else ir.Constant(int32_t, inst.count)
            return self.builder.alloca(elem_type, size=size, name=inst.name)

        if isinstance(inst, SSAStore):
            addr = self._resolve_ssa_value(inst.addr, value_map)
            value = self._resolve_ssa_value(inst.value, value_map)
            self._lower_ssa_store(addr, value)
            return None

        if isinstance(inst, SSACall):
            return self._lower_ssa_call(inst, value_map)

        return None

    def _ssa_constant_ir_value(self, type_name, value):
        ir_type = self._ssa_ir_type(type_name)
        if isinstance(ir_type, ir.PointerType):
            if value == 0:
                return ir.Constant(ir_type, None)
            # Non-zero pointer constant (e.g. `(void *) 1`): emit an
            # `inttoptr` expression rather than a literal integer, which
            # LLVM rejects as "integer constant must have integer type"
            # (gcc_torture pr86231.c).
            int_const = ir.Constant(ir.IntType(64), value)
            return int_const.inttoptr(ir_type)
        return ir.Constant(ir_type, value)

    def _resolve_ssa_value(self, ssa_val, value_map):
        """Resolve an SSA value to an LLVM IR value."""
        from pcc.frontends.c.ssa.ir import SSAConstant, SSAGlobalRef, SSAStringConstant, SSAUndef

        if isinstance(ssa_val, SSAConstant):
            return self._ssa_constant_ir_value(ssa_val.type_name, ssa_val.value)
        if isinstance(ssa_val, SSAStringConstant):
            literal_node = c_ast.Constant(ssa_val.literal_kind, ssa_val.value)
            gv = self._make_global_string_literal_constant(literal_node, name_hint="ssastr")
            target_type = self._ssa_ir_type(getattr(ssa_val, "type_name", "char*"))
            return self._const_pointer_to_first_elem(gv, target_type)
        if isinstance(ssa_val, SSAGlobalRef):
            value_type, binding = self.lookup(ssa_val.symbol_name)
            if isinstance(binding, ir.values.Constant):
                return binding
            if isinstance(binding, ir.Function):
                return binding
            if isinstance(value_type, ir.ArrayType):
                return self.builder.gep(
                    binding,
                    [ir.Constant(int64_t, 0), ir.Constant(int64_t, 0)],
                    name="ssaglobal.arraydecay",
                )
            if isinstance(getattr(binding, "type", None), ir.PointerType):
                return self._safe_load(binding)
            return binding
        if isinstance(ssa_val, SSAUndef):
            return ir.Constant(self._ssa_ir_type(ssa_val.type_name), ir.Undefined)
        if ssa_val.name in value_map:
            return value_map[ssa_val.name]
        # Fallback: zero
        return ir.Constant(self._ssa_ir_type(getattr(ssa_val, "type_name", "int")), 0)

    def _lower_ssa_binop(
        self,
        op,
        left,
        right,
        name,
        *,
        result_type_name,
        left_type_name,
        right_type_name,
    ):
        """Lower a binary operation to LLVM IR.

        LLVM reference: LLVM distinguishes integer instructions
        (Instruction::Add/Sub/Mul/UDiv/SDiv/URem/SRem, ICmpInst) from
        floating-point ones (Instruction::FAdd/FSub/FMul/FDiv/FRem,
        FCmpInst). FCmpInst asserts `isFPOrFPVectorTy()` on operands —
        mismatching int vs fp dispatch produces `icmp requires integer
        operands` verifier errors. See
        /tmp/llvm-src/.../lib/IR/Instructions.cpp FCmpInst::AssertOK.
        """
        b = self.builder
        _FLOAT_TYPES = (ir.HalfType, ir.FloatType, ir.DoubleType)
        is_float = isinstance(left.type, _FLOAT_TYPES) or isinstance(right.type, _FLOAT_TYPES)
        if (
            op == "+"
            and isinstance(left.type, ir.IntType)
            and isinstance(right.type, ir.PointerType)
        ):
            left, right = right, left
        if (
            op == "-"
            and isinstance(left.type, ir.PointerType)
            and isinstance(right.type, ir.PointerType)
        ):
            # C11 6.5.6p9: the pointer difference is in ELEMENTS of the
            # pointed-to type, not bytes. Divide the byte difference by
            # sizeof(*ptr). gcc_torture 20010116-1.c has `last - first`
            # with Data (sizeof 12) where only the element count (4) is
            # meaningful.
            left_int = self.builder.ptrtoint(left, int64_t, name=f"{name}.lhs")
            right_int = self.builder.ptrtoint(right, int64_t, name=f"{name}.rhs")
            byte_diff = self.builder.sub(left_int, right_int, name=f"{name}.bytes")
            pointee = left.type.pointee
            elem_size = None
            try:
                elem_size = pointee.get_abi_size(self._target_data)
            except Exception:
                elem_size = None
            if elem_size is None or elem_size <= 0:
                elem_size = 1
            if elem_size == 1:
                diff = byte_diff
            else:
                diff = self.builder.sdiv(
                    byte_diff,
                    ir.Constant(int64_t, elem_size),
                    name=name,
                )
            target_type = self._ssa_ir_type(result_type_name)
            if isinstance(target_type, ir.IntType) and diff.type != target_type:
                return self._ssa_convert(
                    diff,
                    target_type,
                    source_type_name="long",
                )
            return diff
        if isinstance(left.type, ir.PointerType) and isinstance(right.type, ir.IntType):
            index = right
            if index.type.width < int64_t.width:
                # Pointer index widening must preserve unsigned-ness so
                # `x[(unsigned char)i]` wraps correctly for 0xe8-style
                # values instead of sign-extending to a negative index
                # (gcc_torture 20030916-1.c).
                if self._ssa_is_unsigned_type(right_type_name):
                    index = b.zext(index, int64_t)
                else:
                    index = b.sext(index, int64_t)
            elif index.type.width > int64_t.width:
                index = b.trunc(index, int64_t)
            if op == "-":
                index = b.neg(index, name=f"{name}.neg")
            if op in {"+", "-"}:
                return b.gep(left, [index], name=name)
        if is_float:
            # Unordered fcmp (fcmp_unordered) matches C semantics for NaN:
            # `x != x` is true when x is NaN, so use unordered for `!=` and
            # ordered for the rest, aligning with how the existing
            # AST-path codegen handles float compare (see line ~2066 and
            # ~7470 for fcmp_ordered usage).
            if op == "+":
                return b.fadd(left, right, name=name)
            if op == "-":
                return b.fsub(left, right, name=name)
            if op == "*":
                return b.fmul(left, right, name=name)
            if op == "/":
                return b.fdiv(left, right, name=name)
            if op == "%":
                return b.frem(left, right, name=name)
            if op in ("==", "<", ">", "<=", ">="):
                cmp = b.fcmp_ordered(op, left, right, name=name)
                return b.zext(cmp, self._ssa_ir_type(result_type_name), name=f"{name}.i")
            if op == "!=":
                cmp = b.fcmp_unordered(op, left, right, name=name)
                return b.zext(cmp, self._ssa_ir_type(result_type_name), name=f"{name}.i")
            # Bitwise / shift ops are not defined on floats in C.
            raise ValueError(f"unsupported float binop {op!r}")
        # For comparison ops the caller has already converted both
        # operands to the common (promoted) type and set both
        # {left,right}_type_name to that common type's name
        # (see usual-arithmetic-conversions block above). Decide
        # signedness from that common type only — "any operand
        # unsigned" would be wrong for e.g. `long < unsigned int` where
        # the unsigned side promotes to the signed wider type. For
        # other ops the individual operand types are still meaningful;
        # keep the "any unsigned" heuristic for `/` and `%`.
        is_cmp_here = op in ("==", "!=", "<", ">", "<=", ">=")
        if is_cmp_here:
            is_unsigned = self._ssa_is_unsigned_type(left_type_name)
        else:
            is_unsigned = (
                self._ssa_is_unsigned_type(left_type_name)
                or self._ssa_is_unsigned_type(right_type_name)
            )
        # SEC-P1-UBSAN guards (no-op unless the flag is enabled). Signedness of
        # `+ - *` uses the common (result) type, matching the sdiv/srem choice.
        _ubsan_signed = not self._ssa_is_unsigned_type(result_type_name)
        if op in ("+", "-", "*"):
            self._maybe_ubsan_guard_arith(left, right, op, signed=_ubsan_signed)
        elif op in ("/", "%"):
            self._maybe_ubsan_guard_div(left, right, signed=not is_unsigned)
        if op == "+":
            return b.add(left, right, name=name)
        if op == "-":
            return b.sub(left, right, name=name)
        if op == "*":
            return b.mul(left, right, name=name)
        if op == "/":
            return b.udiv(left, right, name=name) if is_unsigned else b.sdiv(left, right, name=name)
        if op == "%":
            return b.urem(left, right, name=name) if is_unsigned else b.srem(left, right, name=name)
        if op in ("==", "!=", "<", ">", "<=", ">="):
            cmp = (
                b.icmp_unsigned(op, left, right, name=name)
                if is_unsigned
                else b.icmp_signed(op, left, right, name=name)
            )
            return b.zext(cmp, self._ssa_ir_type(result_type_name), name=f"{name}.i")
        if op == "&":
            return b.and_(left, right, name=name)
        if op == "|":
            return b.or_(left, right, name=name)
        if op == "^":
            return b.xor(left, right, name=name)
        if op == "<<":
            self._maybe_ubsan_guard_shift(left, right)  # SEC-P1-UBSAN (no-op if off)
            return b.shl(left, right, name=name)
        if op == ">>":
            self._maybe_ubsan_guard_shift(left, right)  # SEC-P1-UBSAN (no-op if off)
            return b.lshr(left, right, name=name) if self._ssa_is_unsigned_type(left_type_name) else b.ashr(left, right, name=name)
        # Fallback
        return b.add(left, right, name=name)

    def _lower_ssa_unop(self, op, operand, name, *, result_type_name, operand_type_name):
        """Lower a unary operation to LLVM IR."""
        b = self.builder
        _FLOAT_TYPES = (ir.HalfType, ir.FloatType, ir.DoubleType)
        if op == "-":
            if isinstance(operand.type, _FLOAT_TYPES):
                return b.fneg(operand, name=name)
            return b.neg(operand, name=name)
        if op == "!":
            cmp = self._to_bool(operand)
            zero = ir.Constant(ir.IntType(1), 0)
            inv = b.icmp_unsigned("==", cmp, zero, name=name)
            return b.zext(inv, self._ssa_ir_type(result_type_name))
        if op == "~":
            return b.not_(operand, name=name)
        return operand

    def _lower_ssa_cast(self, operand, *, result_type_name, operand_type_name):
        """Lower an explicit SSA cast for the current scalar subset."""
        target_type = self._ssa_ir_type(result_type_name)
        if operand.type == target_type:
            return operand
        if isinstance(operand.type, ir.IntType) and isinstance(target_type, ir.IntType):
            return self._ssa_convert(
                operand,
                target_type,
                source_type_name=operand_type_name,
            )
        # Float -> integer: explicit SSA casts keep their own declared-type
        # path, matching the generic implicit conversion's signedness choice.
        _FLOAT_TYPES = (ir.HalfType, ir.FloatType, ir.DoubleType)
        if isinstance(operand.type, _FLOAT_TYPES) and isinstance(target_type, ir.IntType):
            if self._ssa_is_unsigned_type(result_type_name):
                return self.builder.fptoui(operand, target_type)
            return self.builder.fptosi(operand, target_type)
        # Integer → float: pick signed vs unsigned based on source.
        if isinstance(operand.type, ir.IntType) and isinstance(target_type, _FLOAT_TYPES):
            if self._ssa_is_unsigned_type(operand_type_name):
                return self.builder.uitofp(operand, target_type)
            return self.builder.sitofp(operand, target_type)
        return self._implicit_convert(operand, target_type)

    def _lower_ssa_load(self, base, index, *, result_type_name, index_type_name=None):
        """Lower a side-effect-free SSA load from a pointer base.

        Widening the index must preserve unsignedness — `hist[data[i]]`
        where `data[i]` is `unsigned char` but sign-extended as a GEP
        index would read from `hist[-1]` instead of `hist[0xNN]`, a
        byte_histogram bench regression we were measuring under Phase 3.
        """
        if not isinstance(getattr(base, "type", None), ir.PointerType):
            return ir.Constant(self._ssa_ir_type(result_type_name), 0)

        elem_ptr = base
        if index is not None:
            if not isinstance(index.type, ir.IntType):
                index = self.builder.fptoui(index, int64_t)
            elif index.type.width < int64_t.width:
                if index_type_name and self._ssa_is_unsigned_type(index_type_name):
                    index = self.builder.zext(index, int64_t)
                else:
                    index = self.builder.sext(index, int64_t)
            elif index.type.width > int64_t.width:
                index = self.builder.trunc(index, int64_t)
            elem_ptr = self.builder.gep(base, [index], name="ssaload.idx")

        loaded = self._safe_load(elem_ptr)
        return self._implicit_convert(loaded, self._ssa_ir_type(result_type_name))

    def _lower_ssa_field_addr(self, base, field_name, *, result_type_name):
        """Lower a read-only aggregate field address inside the SSA subset."""
        if not isinstance(getattr(base, "type", None), ir.PointerType):
            return ir.Constant(self._ssa_ir_type(result_type_name), ir.Undefined)
        aggregate_type = base.type.pointee
        field_offset, semantic_field_type = self._get_aggregate_field_info(
            aggregate_type,
            field_name,
        )
        target_ptr_type = ir.PointerType(semantic_field_type)
        if result_type_name:
            target_ptr_type = self._ssa_ir_type(result_type_name)
        return self._byte_offset_ptr(
            base,
            field_offset,
            target_ptr_type,
            name="ssafieldptr",
        )

    def _lower_ssa_field_extract(self, base, field_name, *, result_type_name):
        """Lower a scalar field extract from an aggregate SSA value."""
        aggregate_type = getattr(base, "type", None)
        if not self._is_aggregate_ir_type(aggregate_type):
            return ir.Constant(self._ssa_ir_type(result_type_name), 0)
        field_path = self._aggregate_field_path(aggregate_type, field_name)
        if field_path is None:
            return ir.Constant(self._ssa_ir_type(result_type_name), 0)
        extracted = self.builder.extract_value(base, field_path, name="ssafield")
        return self._implicit_convert(extracted, self._ssa_ir_type(result_type_name))

    def _lower_ssa_store(self, addr, value):
        """Lower a narrow SSA memory store."""
        if not isinstance(getattr(addr, "type", None), ir.PointerType):
            return
        self._safe_store(value, addr)

    def _lower_ssa_call(self, inst, value_map):
        """Lower an SSA call instruction to an LLVM call."""
        callee_func = None
        ftype = None
        if inst.callee is not None:
            callee_func = self._resolve_ssa_value(inst.callee, value_map)
            if (
                isinstance(getattr(callee_func, "type", None), ir.PointerType)
                and isinstance(callee_func.type.pointee, ir.FunctionType)
            ):
                ftype = callee_func.type.pointee
        else:
            try:
                _, callee_func = self.lookup(inst.callee_name)
            except (KeyError, Exception):
                pass

            if callee_func is None or not isinstance(callee_func, ir.Function):
                _, callee_func = self._declare_implicit_function(
                    inst.callee_name,
                    call_arg_count=len(inst.args),
                )

            if isinstance(callee_func, ir.Function):
                ftype = callee_func.function_type
            elif (
                isinstance(getattr(callee_func, "type", None), ir.PointerType)
                and isinstance(callee_func.type.pointee, ir.FunctionType)
            ):
                ftype = callee_func.type.pointee

        if callee_func is None or ftype is None:
            return ir.Constant(self._ssa_ir_type(getattr(inst, "type_name", "int")), 0)

        call_args = []
        is_variadic = bool(getattr(ftype, "var_arg", False))
        _FLOAT_TYPES = (ir.HalfType, ir.FloatType, ir.DoubleType)
        for i, ssa_arg in enumerate(inst.args):
            arg_val = self._resolve_ssa_value(ssa_arg, value_map)
            if i < len(ftype.args):
                expected = ftype.args[i]
                if arg_val.type != expected:
                    arg_val = self._ssa_convert(
                        arg_val,
                        expected,
                        source_type_name=getattr(ssa_arg, "type_name", "int"),
                    )
            elif is_variadic:
                # C11 6.5.2.2p6: default argument promotions apply to
                # extra variadic args. char/short → int, float → double.
                # Without this, `printf("%x", u8_value)` passes i8 and
                # the callee reads wrong-width data (see c-testsuite
                # 00216.c `flow: 809 ...` regression).
                if (
                    isinstance(arg_val.type, ir.IntType)
                    and arg_val.type.width < 32
                ):
                    source_type_name = getattr(ssa_arg, "type_name", "int")
                    if self._ssa_is_unsigned_type(source_type_name):
                        arg_val = self.builder.zext(arg_val, ir.IntType(32))
                    else:
                        arg_val = self.builder.sext(arg_val, ir.IntType(32))
                elif isinstance(arg_val.type, (ir.HalfType, ir.FloatType)):
                    arg_val = self.builder.fpext(arg_val, ir.DoubleType())
            call_args.append(arg_val)

        # Emit the call
        is_void = isinstance(ftype.return_type, ir.VoidType)
        call_target = self._direct_call_callee(callee_func, call_args)
        if is_void:
            self.builder.call(call_target, call_args)
            return None

        result = self.builder.call(call_target, call_args, name=inst.name)
        # If the actual LLVM return type doesn't match the SSA metadata
        # type (e.g. builder defaulted to `int` for an undeclared
        # `strlen` but LLVM knows it returns `size_t`/i64), convert the
        # result. Otherwise a downstream phi typed from `inst.type_name`
        # would mismatch the actual value type — see test_separate_tus
        # lua `*len = s ? strlen(s) : 0;` regression.
        declared_type = self._ssa_ir_type(getattr(inst, "type_name", "int"))
        if (
            isinstance(result.type, ir.IntType)
            and isinstance(declared_type, ir.IntType)
            and result.type != declared_type
        ):
            result = self._ssa_convert(
                result,
                declared_type,
                source_type_name=None,
            )
        return result

    def _ssa_convert(self, val, target_type, source_type_name=None):
        """Convert an LLVM value to target type (int width change)."""
        if val.type == target_type:
            return val
        if isinstance(target_type, ir.IntType) and target_type.width == 1:
            return self._tag_unsigned(self._to_bool(val))
        if isinstance(val.type, ir.IntType) and isinstance(target_type, ir.IntType):
            if val.type.width < target_type.width:
                if self._ssa_is_unsigned_type(source_type_name or ""):
                    return self.builder.zext(val, target_type)
                return self.builder.sext(val, target_type)
            if val.type.width > target_type.width:
                return self.builder.trunc(val, target_type)
        return self._implicit_convert(val, target_type)
