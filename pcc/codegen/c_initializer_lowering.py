"""Constant and runtime aggregate initializer lowering for the C frontend.

The mixin consumes type, layout, signedness, and conversion policy through the
LLVMCodeGenerator facade; it does not define those shared policies.
"""

import struct

from pcc.llvm_capi.compat import ir_c as ir

from .c_layout import BitFieldRef
from .c_layout import is_struct_ir_type as _is_struct_ir_type
from .c_types import int8_t, int32_t, int64_t
from ..ast import c_ast as c_ast


class CInitializerLoweringMixin:
    def _build_const_array_init(self, init_list, array_type, elem_ir_type):
        """Build a constant initializer for a global array."""
        actual_elem = (
            array_type.element if isinstance(array_type, ir.ArrayType) else elem_ir_type
        )
        values = []
        for expr in init_list.exprs:
            if isinstance(expr, c_ast.InitList):
                sub_type = actual_elem
                values.append(
                    self._build_const_array_init(expr, sub_type, elem_ir_type)
                )
            else:
                try:
                    val = self._eval_const_expr(expr)
                    c = self._ir_constant_from_value(actual_elem, val)
                    str(c)  # verify serializable
                    values.append(c)
                except Exception as exc:
                    raise ValueError(
                        "constant array initializer element could not be lowered"
                    ) from exc
        try:
            result = ir.Constant(array_type, values)
            str(result)  # verify
            return result
        except Exception as exc:
            raise ValueError(
                "constant array initializer could not be constructed"
            ) from exc

    def _zero_initializer(self, ir_type):
        if isinstance(ir_type, ir.PointerType):
            return ir.Constant(ir_type, None)
        if self._is_floating_ir_type(ir_type):
            return ir.Constant(ir_type, 0.0)
        if isinstance(ir_type, ir.IntType):
            return ir.Constant(ir_type, 0)
        return ir.Constant(ir_type, None)

    def _make_global_string_constant(self, raw, name_hint="str"):
        processed = self._process_escapes(raw)
        data = self._string_bytes(processed + "\00")
        arr_type = ir.ArrayType(int8_t, len(data))
        gv = ir.GlobalVariable(
            self.module, arr_type, self.module.get_unique_name(name_hint)
        )
        gv.initializer = ir.Constant(arr_type, data)
        gv.global_constant = True
        gv.linkage = "internal"
        return gv

    def _make_global_string_literal_constant(self, node, name_hint="str"):
        data = self._string_literal_data(node)
        is_wide = self._is_wide_string_constant(node)
        # Deduplicate identical string literals so that `foo(s)` and
        # `s + 2` — which both reference the same source-level
        # `const char *s = "...";` — compare equal at runtime. Without
        # dedup each reference created a fresh `@ssastr.N` and pointer
        # equality failed (gcc_torture pr34415.c).
        cache = getattr(self, "_string_literal_cache", None)
        if cache is None:
            cache = {}
            self._string_literal_cache = cache
        cache_key = (is_wide, tuple(data))
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        if is_wide:
            elem_type = int32_t
            values = [ir.Constant(int32_t, cp) for cp in data]
            arr_type = ir.ArrayType(elem_type, len(values))
            initializer = ir.Constant(arr_type, values)
        else:
            elem_type = int8_t
            arr_type = ir.ArrayType(elem_type, len(data))
            initializer = ir.Constant(arr_type, data)
        gv = ir.GlobalVariable(
            self.module, arr_type, self.module.get_unique_name(name_hint)
        )
        gv.initializer = initializer
        gv.global_constant = True
        gv.linkage = "internal"
        cache[cache_key] = gv
        return gv

    def _compound_literal_ir_type(self, ast_type, init_node=None):
        if isinstance(ast_type, c_ast.ArrayDecl):
            return self._build_array_ir_type(ast_type, init_node=init_node)
        return self._resolve_ast_type(ast_type)

    def _materialize_global_compound_literal(self, ast_type, init_node):
        cache_key = id(init_node)
        cached = self._global_compound_literal_cache.get(cache_key)
        if cached is not None:
            return cached

        ir_type = self._compound_literal_ir_type(ast_type, init_node)
        gv = ir.GlobalVariable(
            self.module,
            ir_type,
            self.module.get_unique_name("compoundlit"),
        )
        gv.initializer = self._build_const_init(init_node, ir_type)
        gv.linkage = "internal"
        self._global_compound_literal_cache[cache_key] = gv
        return gv

    def _const_pointer_to_first_elem(self, gv, target_type):
        idx0 = ir.Constant(int64_t, 0)
        ptr = gv.gep([idx0, idx0])
        return ptr if ptr.type == target_type else ptr.bitcast(target_type)

    def _is_little_endian(self):
        return not str(self.module.data_layout).startswith("E")

    def _zero_bytes(self, size):
        return [ir.Constant(int8_t, 0) for _ in range(size)]

    def _scalar_init_node(self, init_node):
        if not isinstance(init_node, c_ast.InitList):
            return init_node
        if not init_node.exprs:
            return None
        return self._scalar_init_node(init_node.exprs[0])

    def _initializer_slot_count(self, ir_type):
        if getattr(ir_type, "is_union", False):
            member_names = self._aggregate_member_names(ir_type)
            if not member_names:
                return 1
            return self._initializer_slot_count(
                self._aggregate_member_ir_type(ir_type, 0)
            )

        if isinstance(ir_type, ir.ArrayType):
            return ir_type.count * self._initializer_slot_count(ir_type.element)

        if _is_struct_ir_type(ir_type):
            if getattr(ir_type, "has_custom_layout", False):
                layouts = getattr(ir_type, "field_layouts_by_index", None) or []
                if layouts:
                    return sum(
                        self._initializer_slot_count(layout.semantic_ir_type)
                        for layout in layouts
                    )
            return sum(
                self._initializer_slot_count(member_type)
                for member_type in getattr(ir_type, "elements", ())
            )

        return 1

    def _initializer_expr_consumption(self, exprs, ir_type):
        if not exprs:
            return 0

        first_expr = exprs[0]
        if (
            isinstance(first_expr, c_ast.InitList)
            or self._is_array_string_initializer(first_expr, ir_type)
            or self._initializer_expr_matches_type(first_expr, ir_type)
        ):
            return 1

        if getattr(ir_type, "is_union", False):
            return 1

        if isinstance(ir_type, ir.ArrayType):
            consumed = 0
            remaining = list(exprs)
            for _ in range(ir_type.count):
                if not remaining:
                    break
                step = self._initializer_expr_consumption(remaining, ir_type.element)
                if step <= 0:
                    break
                consumed += step
                remaining = remaining[step:]
            return max(consumed, 1)

        if _is_struct_ir_type(ir_type):
            consumed = 0
            remaining = list(exprs)
            for member_type in self._aggregate_member_ir_types(ir_type):
                if not remaining:
                    break
                step = self._initializer_expr_consumption(remaining, member_type)
                if step <= 0:
                    break
                consumed += step
                remaining = remaining[step:]
            return max(consumed, 1)

        return 1

    def _is_char_array_string_initializer(self, init_node, ir_type):
        return (
            self._is_string_constant(init_node)
            and not self._is_wide_string_constant(init_node)
            and isinstance(ir_type, ir.ArrayType)
            and isinstance(ir_type.element, ir.IntType)
            and ir_type.element.width == 8
        )

    def _is_wchar_array_string_initializer(self, init_node, ir_type):
        return (
            self._is_wide_string_constant(init_node)
            and isinstance(ir_type, ir.ArrayType)
            and isinstance(ir_type.element, ir.IntType)
            and ir_type.element.width == 32
        )

    def _is_array_string_initializer(self, init_node, ir_type):
        return self._is_char_array_string_initializer(
            init_node, ir_type
        ) or self._is_wchar_array_string_initializer(init_node, ir_type)

    def _normalize_array_init_list(self, init_node, elem_ir_type):
        if not isinstance(init_node, c_ast.InitList):
            return init_node

        exprs = list(getattr(init_node, "exprs", None) or [])
        if not exprs:
            return init_node

        if any(
            isinstance(expr, (c_ast.InitList, c_ast.NamedInitializer))
            or self._is_array_string_initializer(expr, elem_ir_type)
            for expr in exprs
        ):
            return init_node

        slots = self._initializer_slot_count(elem_ir_type)
        if slots <= 1:
            return init_node

        grouped_exprs = []
        cursor = 0
        while cursor < len(exprs):
            consumed = self._initializer_expr_consumption(exprs[cursor:], elem_ir_type)
            if consumed <= 1:
                grouped_exprs.append(exprs[cursor])
                cursor += 1
                continue
            grouped_exprs.append(
                c_ast.InitList(exprs[cursor : cursor + consumed], init_node.coord)
            )
            cursor += consumed
        return c_ast.InitList(grouped_exprs, init_node.coord)

    def _designator_index_bounds(self, designator):
        if isinstance(designator, c_ast.RangeDesignator):
            try:
                start = int(self._eval_const_expr(designator.start))
                end = int(self._eval_const_expr(designator.end))
            except Exception:
                return None
            if end < start:
                start, end = end, start
            return start, end
        try:
            index = int(self._eval_const_expr(designator))
        except Exception:
            return None
        return index, index

    def _ordered_array_init_exprs(self, init_node, ir_type):
        exprs = list(getattr(init_node, "exprs", None) or [])
        if not exprs:
            return exprs

        if not any(isinstance(expr, c_ast.NamedInitializer) for expr in exprs):
            return exprs

        ordered = [None] * ir_type.count
        cursor = 0

        for expr in exprs:
            target_expr = expr
            if isinstance(expr, c_ast.NamedInitializer):
                designators = getattr(expr, "name", None) or []
                if not designators:
                    continue
                bounds = self._designator_index_bounds(designators[0])
                if bounds is None:
                    continue
                start, end = bounds
                if start < 0:
                    continue
                if end >= len(ordered):
                    ordered.extend([None] * (end + 1 - len(ordered)))
                cursor = start
                if len(designators) > 1:
                    target_expr = c_ast.InitList(
                        [
                            c_ast.NamedInitializer(
                                designators[1:],
                                expr.expr,
                                expr.coord,
                            )
                        ],
                        expr.coord,
                    )
                    normalized = self._normalize_initializer_for_type(
                        target_expr, ir_type.element
                    )
                else:
                    target_expr = expr.expr
                    normalized = self._normalize_initializer_for_type(
                        target_expr, ir_type.element
                    )
                for index in range(start, end + 1):
                    if len(designators) == 1:
                        ordered[index] = normalized
                    elif ordered[index] is None:
                        ordered[index] = normalized
                    else:
                        ordered[index] = self._merge_initializer_nodes(
                            ordered[index],
                            normalized,
                            expr.coord,
                        )
                cursor = end + 1
                continue

            while cursor < len(ordered) and ordered[cursor] is not None:
                cursor += 1
            if cursor >= len(ordered):
                break
            ordered[cursor] = self._normalize_initializer_for_type(
                target_expr, ir_type.element
            )
            cursor += 1

        return ordered

    def _merge_initializer_nodes(self, existing, new_expr, coord=None):
        if existing is None:
            return new_expr
        if new_expr is None:
            return existing

        merged_exprs = []
        if isinstance(existing, c_ast.InitList):
            merged_exprs.extend(list(existing.exprs or ()))
        else:
            merged_exprs.append(existing)

        if isinstance(new_expr, c_ast.InitList):
            merged_exprs.extend(list(new_expr.exprs or ()))
        else:
            merged_exprs.append(new_expr)

        merged_coord = (
            coord
            or getattr(existing, "coord", None)
            or getattr(new_expr, "coord", None)
        )
        return c_ast.InitList(merged_exprs, merged_coord)

    def _aggregate_member_ir_types(self, ir_type):
        if getattr(ir_type, "is_union", False):
            if getattr(ir_type, "elements", None):
                return [self._aggregate_member_ir_type(ir_type, 0)]
            return []

        if getattr(ir_type, "has_custom_layout", False):
            layouts = getattr(ir_type, "field_layouts_by_index", None) or []
            if layouts:
                return [layout.semantic_ir_type for layout in layouts]

        if _is_struct_ir_type(ir_type):
            return list(getattr(ir_type, "elements", ()) or [])

        return []

    def _normalize_struct_init_list(self, init_node, ir_type):
        if not isinstance(init_node, c_ast.InitList):
            return init_node

        exprs = list(getattr(init_node, "exprs", None) or [])
        if not exprs:
            return init_node

        if any(isinstance(expr, c_ast.NamedInitializer) for expr in exprs):
            return init_node

        member_types = self._aggregate_member_ir_types(ir_type)
        if not member_types:
            return init_node

        normalized = []
        cursor = 0
        for member_type in member_types:
            if cursor >= len(exprs):
                break

            expr = exprs[cursor]
            if isinstance(expr, c_ast.InitList):
                normalized.append(self._normalize_initializer_for_type(expr, member_type))
                cursor += 1
                continue

            if self._is_array_string_initializer(expr, member_type):
                normalized.append(expr)
                cursor += 1
                continue

            if self._initializer_expr_matches_type(expr, member_type):
                normalized.append(expr)
                cursor += 1
                continue

            consumed = self._initializer_expr_consumption(exprs[cursor:], member_type)
            if consumed <= 1:
                normalized.append(expr)
                cursor += 1
                continue

            member_init = c_ast.InitList(
                exprs[cursor : cursor + consumed], init_node.coord
            )
            normalized.append(self._normalize_initializer_for_type(member_init, member_type))
            cursor += consumed

        if cursor < len(exprs):
            normalized.extend(exprs[cursor:])

        return c_ast.InitList(normalized, init_node.coord)

    def _normalize_union_init_list(self, init_node, ir_type):
        if not isinstance(init_node, c_ast.InitList):
            return init_node

        exprs = list(getattr(init_node, "exprs", None) or [])
        if not exprs:
            return init_node

        field_index, field_type, member_init = self._select_union_initializer(
            init_node, ir_type
        )
        if field_index is None or member_init is None:
            return init_node

        normalized_member = self._normalize_initializer_for_type(member_init, field_type)
        if normalized_member is member_init:
            return init_node

        first_expr = exprs[0]
        if isinstance(first_expr, c_ast.NamedInitializer):
            rewritten = c_ast.NamedInitializer(
                first_expr.name,
                normalized_member,
                first_expr.coord,
            )
            return c_ast.InitList([rewritten] + exprs[1:], init_node.coord)

        return normalized_member

    def _initializer_expr_matches_type(self, expr, ir_type):
        if expr is None:
            return False
        try:
            expr_ir_type = self._infer_sizeof_operand_ir_type(expr)
        except Exception:
            return False

        if str(expr_ir_type) == str(ir_type):
            return True

        if isinstance(expr_ir_type, ir.ArrayType) and isinstance(ir_type, ir.ArrayType):
            return self._are_compatible_object_ir_types(expr_ir_type, ir_type)

        return False

    def _normalize_initializer_for_type(self, init_node, ir_type):
        if self._is_array_string_initializer(init_node, ir_type):
            return init_node

        if isinstance(init_node, c_ast.CompoundLiteral):
            if self._initializer_expr_matches_type(init_node, ir_type):
                return init_node
            init_node = init_node.init

        if not isinstance(init_node, c_ast.InitList):
            if (
                isinstance(ir_type, ir.ArrayType)
                and not self._initializer_expr_matches_type(init_node, ir_type)
            ):
                return c_ast.InitList([init_node], getattr(init_node, "coord", None))
            if (
                (_is_struct_ir_type(ir_type) or getattr(ir_type, "is_union", False))
                and not self._initializer_expr_matches_type(init_node, ir_type)
            ):
                return c_ast.InitList([init_node], getattr(init_node, "coord", None))
            return init_node

        exprs = list(getattr(init_node, "exprs", None) or [])
        if (
            len(exprs) == 1
            and self._is_array_string_initializer(exprs[0], ir_type)
        ):
            return exprs[0]

        if any(isinstance(expr, c_ast.NamedInitializer) for expr in exprs):
            if (
                isinstance(ir_type, ir.ArrayType)
                or getattr(ir_type, "is_union", False)
                or _is_struct_ir_type(ir_type)
            ):
                return init_node

        if isinstance(ir_type, ir.ArrayType):
            grouped = self._normalize_array_init_list(init_node, ir_type.element)
            exprs = list(getattr(grouped, "exprs", None) or [])
            normalized = []
            for expr in exprs:
                normalized.append(self._normalize_initializer_for_type(expr, ir_type.element))
            return c_ast.InitList(normalized, grouped.coord)

        if getattr(ir_type, "is_union", False):
            return self._normalize_union_init_list(init_node, ir_type)

        if _is_struct_ir_type(ir_type):
            grouped = self._normalize_struct_init_list(init_node, ir_type)
            exprs = list(getattr(grouped, "exprs", None) or [])
            member_types = self._aggregate_member_ir_types(ir_type)
            normalized = []
            for index, expr in enumerate(exprs):
                if index < len(member_types):
                    normalized.append(
                        self._normalize_initializer_for_type(expr, member_types[index])
                    )
                else:
                    normalized.append(expr)
            return c_ast.InitList(normalized, grouped.coord)

        return init_node

    def _struct_field_names(self, ir_type):
        member_names = list(getattr(ir_type, "members", ()) or [])
        if member_names:
            return member_names
        member_types = getattr(ir_type, "member_types", None)
        if isinstance(member_types, dict):
            return list(member_types.keys())
        return []

    def _ordered_struct_init_exprs(self, init_node, ir_type):
        exprs = list(getattr(init_node, "exprs", None) or [])
        field_names = self._struct_field_names(ir_type)
        if not exprs or not field_names:
            return exprs
        if not any(isinstance(expr, c_ast.NamedInitializer) for expr in exprs):
            return exprs

        ordered = [None] * len(field_names)
        cursor = 0
        index_by_name = {name: i for i, name in enumerate(field_names)}

        for expr in exprs:
            if isinstance(expr, c_ast.NamedInitializer):
                designators = getattr(expr, "name", None) or []
                if designators and isinstance(designators[0], c_ast.ID):
                    target = index_by_name.get(designators[0].name)
                    if target is not None:
                        if len(designators) == 1:
                            ordered[target] = expr.expr
                        else:
                            target_expr = c_ast.InitList(
                                [
                                    c_ast.NamedInitializer(
                                        designators[1:],
                                        expr.expr,
                                        expr.coord,
                                    )
                                ],
                                expr.coord,
                            )
                            if ordered[target] is None:
                                ordered[target] = target_expr
                            else:
                                ordered[target] = self._merge_initializer_nodes(
                                    ordered[target],
                                    target_expr,
                                    expr.coord,
                                )
                        cursor = target + 1
                continue

            while cursor < len(ordered) and ordered[cursor] is not None:
                cursor += 1
            if cursor >= len(ordered):
                break
            ordered[cursor] = expr
            cursor += 1

        return ordered

    def _build_const_address(self, init_node):
        if isinstance(init_node, c_ast.ID):
            try:
                _, sym = self.lookup(init_node.name)
            except Exception:
                return None
            if isinstance(sym, (ir.Function, ir.GlobalVariable)):
                return sym
            return None

        if isinstance(init_node, c_ast.CompoundLiteral):
            return self._materialize_global_compound_literal(
                init_node.type.type,
                init_node.init,
            )

        if isinstance(init_node, c_ast.Cast):
            return self._build_const_address(init_node.expr)

        # Plain string / wstring literal used as a const address base
        # (e.g. `void *foo[] = {(void *)&("X"[0])};` in gcc_torture
        # 921019-1.c). Return a pointer to the materialized global.
        if isinstance(init_node, c_ast.Constant) and init_node.type in ("string", "wstring"):
            gv = self._make_global_string_literal_constant(init_node, name_hint="ssastr")
            return gv

        if isinstance(init_node, c_ast.ArrayRef):
            base_addr = self._build_const_address(init_node.name)
            if base_addr is None or not isinstance(
                getattr(base_addr, "type", None), ir.PointerType
            ):
                return None
            try:
                idx_val = int(self._eval_const_expr(init_node.subscript))
            except Exception:
                return None
            idx0 = ir.Constant(int64_t, 0)
            idx = ir.Constant(int64_t, idx_val)
            pointee = base_addr.type.pointee
            try:
                if isinstance(pointee, ir.ArrayType):
                    return base_addr.gep([idx0, idx])
                return base_addr.gep([idx])
            except Exception:
                return None

        if isinstance(init_node, c_ast.BinaryOp) and init_node.op in ("+", "-"):
            base_addr = self._build_const_address(init_node.left)
            offset_node = init_node.right
            offset_sign = 1
            if base_addr is None and init_node.op == "+":
                base_addr = self._build_const_address(init_node.right)
                offset_node = init_node.left
            elif base_addr is None:
                return None
            if base_addr is None or not isinstance(
                getattr(base_addr, "type", None), ir.PointerType
            ):
                return None
            try:
                idx_val = int(self._eval_const_expr(offset_node))
            except Exception:
                return None
            if init_node.op == "-":
                idx_val = -idx_val
            idx0 = ir.Constant(int64_t, 0)
            idx = ir.Constant(int64_t, idx_val)
            pointee = base_addr.type.pointee
            try:
                if isinstance(pointee, ir.ArrayType):
                    return base_addr.gep([idx0, idx])
                return base_addr.gep([idx])
            except Exception:
                return None

        if isinstance(init_node, c_ast.StructRef):
            base_addr = self._build_const_address(init_node.name)
            if base_addr is None or not isinstance(
                getattr(base_addr, "type", None), ir.PointerType
            ):
                return None
            if (
                init_node.type == "->"
                and isinstance(base_addr.type.pointee, ir.ArrayType)
            ):
                idx0 = ir.Constant(int64_t, 0)
                try:
                    base_addr = base_addr.gep([idx0, idx0])
                except Exception:
                    return None
            aggregate_type = base_addr.type.pointee
            try:
                offset, field_type = self._get_aggregate_field_info(
                    aggregate_type, init_node.field.name
                )
            except Exception:
                return None

            if (
                hasattr(aggregate_type, "members")
                and init_node.field.name in aggregate_type.members
                and not getattr(aggregate_type, "has_custom_layout", False)
                and not getattr(aggregate_type, "is_union", False)
            ):
                idx0 = ir.Constant(int64_t, 0)
                field_index = aggregate_type.members.index(init_node.field.name)
                try:
                    return base_addr.gep(
                        [idx0, ir.Constant(ir.IntType(32), field_index)]
                    )
                except Exception:
                    return None

            try:
                byte_base = base_addr.bitcast(ir.PointerType(int8_t))
                byte_addr = byte_base.gep([ir.Constant(int64_t, offset)])
                return byte_addr.bitcast(ir.PointerType(field_type))
            except Exception:
                return None

        return None

    def _build_pointer_const(self, init_node, ir_type):
        if isinstance(init_node, c_ast.InitList):
            if init_node.exprs:
                return self._build_pointer_const(init_node.exprs[0], ir_type)
            return ir.Constant(ir_type, None)
        if isinstance(init_node, c_ast.Cast):
            return self._build_pointer_const(init_node.expr, ir_type)
        if self._is_string_constant(init_node):
            gv = self._make_global_string_literal_constant(init_node)
            return self._const_pointer_to_first_elem(gv, ir_type)
        if isinstance(init_node, c_ast.ID):
            try:
                _, sym = self.lookup(init_node.name)
            except Exception:
                sym = None
            if isinstance(sym, ir.Function):
                if sym.type == ir_type:
                    return sym
                try:
                    return sym.bitcast(ir_type)
                except AttributeError:
                    # llvmlite Function values do not expose bitcast().
                    # With opaque pointers, a function symbol is already a
                    # valid ptr constant for object-pointer initializers such
                    # as `void *p = fn` or `{ (void *)fn }`.
                    if isinstance(ir_type, ir.PointerType):
                        return sym
                    return None
            if isinstance(sym, ir.GlobalVariable):
                if isinstance(sym.value_type, ir.ArrayType):
                    return self._const_pointer_to_first_elem(sym, ir_type)
                if sym.type == ir_type:
                    return sym
                if isinstance(sym.type, ir.PointerType):
                    return sym.bitcast(ir_type)
        if (
            isinstance(init_node, c_ast.UnaryOp)
            and init_node.op == "&&"
            and isinstance(init_node.expr, c_ast.ID)
        ):
            return self._label_address_constant(init_node.expr.name, ir_type)
        if isinstance(init_node, c_ast.ArrayRef):
            addr = self._build_const_address(init_node)
            if addr is not None:
                if addr.type == ir_type:
                    return addr
                if isinstance(addr.type, ir.PointerType):
                    return addr.bitcast(ir_type)
        if (
            isinstance(init_node, c_ast.UnaryOp)
            and init_node.op == "&"
        ):
            addr = self._build_const_address(init_node.expr)
            if addr is not None:
                if addr.type == ir_type:
                    return addr
                if isinstance(addr.type, ir.PointerType):
                    return addr.bitcast(ir_type)
        try:
            val = self._eval_const_expr(init_node)
            if val == 0:
                return ir.Constant(ir_type, None)
        except Exception:
            return None
        return None

    def _const_int_to_bytes(self, value, byte_width):
        if byte_width <= 0:
            return []
        mask = (1 << (byte_width * 8)) - 1
        raw = int(value) & mask
        return [
            ir.Constant(int8_t, b)
            for b in raw.to_bytes(
                byte_width,
                byteorder="little" if self._is_little_endian() else "big",
                signed=False,
            )
        ]

    def _split_int_constant_to_bytes(self, int_const, byte_width):
        if byte_width <= 0:
            return []

        raw_const = getattr(int_const, "constant", None)
        if isinstance(raw_const, int):
            return self._const_int_to_bytes(raw_const, byte_width)

        int_bits = byte_width * 8
        if int_const.type.width != int_bits:
            if int_const.type.width < int_bits:
                int_const = int_const.zext(ir.IntType(int_bits))
            else:
                int_const = int_const.trunc(ir.IntType(int_bits))

        byte_values = []
        for i in range(byte_width):
            shift_bits = 8 * (i if self._is_little_endian() else (byte_width - 1 - i))
            part = int_const
            if shift_bits:
                part = part.lshr(ir.Constant(part.type, shift_bits))
            if part.type.width != 8:
                part = part.trunc(int8_t)
            byte_values.append(part)
        return byte_values

    def _pointer_const_to_bytes(self, ptr_const):
        if (
            isinstance(ptr_const, ir.Constant)
            and getattr(ptr_const, "constant", None) is None
        ):
            return self._zero_bytes(self._ir_type_size(ptr_const.type))
        return self._split_int_constant_to_bytes(
            ptr_const.ptrtoint(int64_t), self._ir_type_size(ptr_const.type)
        )

    def _bytes_to_int_constant(self, byte_values, int_type):
        byte_width = int_type.width // 8
        values = list(byte_values[:byte_width])
        if len(values) < byte_width:
            values.extend(self._zero_bytes(byte_width - len(values)))

        result = 0
        for i, byte_val in enumerate(values):
            shift_bits = 8 * (i if self._is_little_endian() else (byte_width - 1 - i))
            raw = getattr(byte_val, "constant", getattr(byte_val, "value", 0))
            if not isinstance(raw, int):
                raw = 0
            result |= (raw & 0xFF) << shift_bits

        bits = int_type.width
        mask = (1 << bits) - 1
        result &= mask
        sign_bit = 1 << (bits - 1)
        if result & sign_bit:
            result -= 1 << bits
        return ir.Constant(int_type, result)

    def _raw_bytes_to_unsigned_int(self, byte_values):
        result = 0
        width = len(byte_values)
        for i, byte_val in enumerate(byte_values):
            shift_bits = 8 * (i if self._is_little_endian() else (width - 1 - i))
            raw = getattr(byte_val, "constant", getattr(byte_val, "value", 0))
            if not isinstance(raw, int):
                raw = 0
            result |= (raw & 0xFF) << shift_bits
        return result

    def _const_init_bytes(self, init_node, ir_type):
        if isinstance(init_node, c_ast.CompoundLiteral):
            init_node = init_node.init
        size = self._ir_type_size(ir_type)
        if init_node is None:
            return self._zero_bytes(size)

        if getattr(ir_type, "is_union", False):
            init_node = self._normalize_initializer_for_type(init_node, ir_type)
            raw = self._zero_bytes(size)
            field_index, member_type, member_init = self._select_union_initializer(
                init_node, ir_type
            )
            if field_index is None:
                return raw

            member_bytes = self._const_init_bytes(member_init, member_type)
            raw[: min(size, len(member_bytes))] = member_bytes[:size]
            return raw

        if isinstance(ir_type, ir.PointerType):
            ptr_const = self._build_pointer_const(init_node, ir_type)
            if ptr_const is None:
                return self._zero_bytes(size)
            return self._pointer_const_to_bytes(ptr_const)

        if self._is_floating_ir_type(ir_type):
            try:
                scalar_node = self._scalar_init_node(init_node)
                if scalar_node is None:
                    value = 0.0
                elif isinstance(scalar_node, c_ast.Constant):
                    try:
                        value = self._parse_float_constant(scalar_node.value)
                    except ValueError:
                        value = float(self._eval_const_expr(scalar_node))
                else:
                    value = float(self._eval_const_expr(scalar_node))
                fmt = "d" if isinstance(ir_type, ir.DoubleType) else "f"
                packed = struct.pack(
                    ("<" if self._is_little_endian() else ">") + fmt,
                    value,
                )
                return [ir.Constant(int8_t, b) for b in packed]
            except Exception:
                return self._zero_bytes(size)

        if isinstance(ir_type, ir.IntType):
            scalar_node = self._scalar_init_node(init_node)
            if scalar_node is None:
                return self._zero_bytes(size)
            return self._const_int_to_bytes(self._eval_const_expr(scalar_node), size)

        if isinstance(ir_type, ir.ArrayType):
            if self._is_array_string_initializer(init_node, ir_type):
                data = self._string_literal_data(init_node)
                if len(data) < ir_type.count:
                    data.extend([0] * (ir_type.count - len(data)))
                else:
                    data = data[: ir_type.count]
                return [ir.Constant(ir_type.element, v) for v in data]

            if isinstance(init_node, c_ast.InitList):
                init_node = self._normalize_initializer_for_type(init_node, ir_type)
                values = []
                ordered_exprs = self._ordered_array_init_exprs(init_node, ir_type)
                for i in range(ir_type.count):
                    expr = ordered_exprs[i] if i < len(ordered_exprs) else None
                    values.extend(self._const_init_bytes(expr, ir_type.element))
                return values

            return self._zero_bytes(size)

        if _is_struct_ir_type(ir_type):
            if getattr(ir_type, "has_custom_layout", False):
                raw = self._zero_bytes(size)
                if not isinstance(init_node, c_ast.InitList):
                    return raw
                init_node = self._normalize_initializer_for_type(init_node, ir_type)

                exprs = self._ordered_struct_init_exprs(init_node, ir_type)
                for i, field_name in enumerate(getattr(ir_type, "members", ())):
                    if i >= len(exprs):
                        break
                    expr = exprs[i]
                    layout = ir_type.field_layouts.get(field_name)
                    if layout is None:
                        continue

                    if layout.is_bitfield:
                        scalar_node = self._scalar_init_node(expr)
                        if scalar_node is None:
                            continue
                        try:
                            field_value = int(self._eval_const_expr(scalar_node))
                        except Exception:
                            continue
                        storage_size = self._ir_type_size(layout.storage_ir_type)
                        start = layout.storage_byte_offset
                        current = self._raw_bytes_to_unsigned_int(
                            raw[start : start + storage_size]
                        )
                        field_mask = self._bitfield_mask(layout.bit_width)
                        clear_mask = ((1 << (storage_size * 8)) - 1) ^ (
                            field_mask << layout.bit_offset
                        )
                        current = (current & clear_mask) | (
                            (field_value & field_mask) << layout.bit_offset
                        )
                        raw[start : start + storage_size] = self._const_int_to_bytes(
                            current, storage_size
                        )
                        continue

                    field_size = self._ir_type_size(layout.semantic_ir_type)
                    field_bytes = self._const_init_bytes(expr, layout.semantic_ir_type)
                    start = layout.byte_offset
                    raw[start : start + field_size] = field_bytes[:field_size]
                return raw

            raw = self._zero_bytes(size)
            if not isinstance(init_node, c_ast.InitList):
                return raw
            init_node = self._normalize_initializer_for_type(init_node, ir_type)

            exprs = self._ordered_struct_init_exprs(init_node, ir_type)
            offset = 0
            for i, member_type in enumerate(ir_type.elements):
                align = self._ir_type_align(member_type)
                offset = (offset + align - 1) & ~(align - 1)
                expr = exprs[i] if i < len(exprs) else None
                field_bytes = self._const_init_bytes(expr, member_type)
                field_size = self._ir_type_size(member_type)
                raw[offset : offset + field_size] = field_bytes[:field_size]
                offset += field_size
            return raw

        scalar_node = self._scalar_init_node(init_node)
        if scalar_node is None:
            return self._zero_bytes(size)
        try:
            return self._const_int_to_bytes(self._eval_const_expr(scalar_node), size)
        except Exception:
            return self._zero_bytes(size)

    def _build_const_init(self, init_node, ir_type):
        if init_node is None:
            return self._zero_initializer(ir_type)

        if isinstance(init_node, c_ast.CompoundLiteral):
            init_node = init_node.init

        if getattr(ir_type, "is_union", False):
            try:
                init_node = self._normalize_initializer_for_type(init_node, ir_type)
                raw = self._const_init_bytes(init_node, ir_type)
                fields = []
                head_type = ir_type.elements[0]
                if not isinstance(head_type, ir.IntType):
                    return self._zero_initializer(ir_type)
                head_size = self._ir_type_size(head_type)
                fields.append(self._bytes_to_int_constant(raw[:head_size], head_type))
                if len(ir_type.elements) > 1:
                    tail_type = ir_type.elements[1]
                    tail_size = self._ir_type_size(tail_type)
                    tail_bytes = raw[head_size : head_size + tail_size]
                    fields.append(ir.Constant(tail_type, tail_bytes))
                return ir.Constant(ir_type, fields)
            except Exception:
                return self._zero_initializer(ir_type)

        if isinstance(ir_type, ir.PointerType):
            ptr_const = self._build_pointer_const(init_node, ir_type)
            if ptr_const is not None:
                return ptr_const
            raise ValueError("pointer initializer is not a compile-time constant")

        if self._is_floating_ir_type(ir_type):
            try:
                scalar_node = self._scalar_init_node(init_node)
                if scalar_node is None:
                    return self._zero_initializer(ir_type)
                if isinstance(scalar_node, c_ast.Constant):
                    try:
                        value = self._parse_float_constant(scalar_node.value)
                    except ValueError:
                        value = float(self._eval_const_expr(scalar_node))
                else:
                    value = float(self._eval_const_expr(scalar_node))
                return self._ir_constant_from_value(ir_type, value)
            except Exception as exc:
                raise ValueError(
                    "floating initializer is not a compile-time constant"
                ) from exc

        if isinstance(ir_type, ir.ArrayType):
            if self._is_array_string_initializer(init_node, ir_type):
                data = self._string_literal_data(init_node)
                if len(data) < ir_type.count:
                    data.extend([0] * (ir_type.count - len(data)))
                else:
                    data = data[: ir_type.count]
                try:
                    if self._is_wide_string_constant(init_node):
                        return ir.Constant(
                            ir_type, [ir.Constant(ir_type.element, v) for v in data]
                        )
                    return ir.Constant(ir_type, data)
                except Exception:
                    return self._zero_initializer(ir_type)

            if isinstance(init_node, c_ast.InitList):
                init_node = self._normalize_initializer_for_type(init_node, ir_type)
                values = []
                ordered_exprs = self._ordered_array_init_exprs(init_node, ir_type)
                for i in range(ir_type.count):
                    expr = ordered_exprs[i] if i < len(ordered_exprs) else None
                    values.append(self._build_const_init(expr, ir_type.element))
                try:
                    return ir.Constant(ir_type, values)
                except Exception:
                    return self._zero_initializer(ir_type)

            return self._zero_initializer(ir_type)

        if _is_struct_ir_type(ir_type):
            if getattr(ir_type, "has_custom_layout", False):
                try:
                    if isinstance(init_node, c_ast.InitList):
                        init_node = self._normalize_initializer_for_type(
                            init_node, ir_type
                        )
                    storage_segments = getattr(ir_type, "storage_segments", None)
                    if storage_segments is None:
                        raw = self._const_init_bytes(init_node, ir_type)
                        values = []
                        offset = 0
                        for member_type in ir_type.elements:
                            field_size = self._ir_type_size(member_type)
                            field_bytes = raw[offset : offset + field_size]
                            if isinstance(member_type, ir.IntType):
                                values.append(
                                    self._bytes_to_int_constant(
                                        field_bytes, member_type
                                    )
                                )
                            elif (
                                isinstance(member_type, ir.ArrayType)
                                and isinstance(member_type.element, ir.IntType)
                                and member_type.element.width == 8
                            ):
                                values.append(ir.Constant(member_type, field_bytes))
                            else:
                                values.append(self._zero_initializer(member_type))
                            offset += field_size
                        return ir.Constant(ir_type, values)
                    values = []
                    exprs = (
                        self._ordered_struct_init_exprs(init_node, ir_type)
                        if isinstance(init_node, c_ast.InitList)
                        else []
                    )
                    field_layouts_by_index = getattr(
                        ir_type, "field_layouts_by_index", None
                    ) or []
                    for segment in storage_segments:
                        member_type = segment.ir_type
                        if segment.kind == "padding":
                            values.append(self._zero_initializer(member_type))
                            continue
                        if segment.kind == "field":
                            expr = None
                            if segment.field_index is not None and segment.field_index < len(exprs):
                                expr = exprs[segment.field_index]
                            values.append(self._build_const_init(expr, member_type))
                            continue

                        storage_size = self._ir_type_size(member_type)
                        current = 0
                        for field_index in segment.bitfield_indices:
                            if field_index >= len(exprs):
                                continue
                            expr = exprs[field_index]
                            if expr is None:
                                continue
                            scalar_node = self._scalar_init_node(expr)
                            if scalar_node is None:
                                continue
                            try:
                                field_value = int(self._eval_const_expr(scalar_node))
                            except Exception:
                                continue
                            layout = field_layouts_by_index[field_index]
                            field_mask = self._bitfield_mask(layout.bit_width)
                            clear_mask = ((1 << (storage_size * 8)) - 1) ^ (
                                field_mask << layout.bit_offset
                            )
                            current = (current & clear_mask) | (
                                (field_value & field_mask) << layout.bit_offset
                            )
                        values.append(
                            self._bytes_to_int_constant(
                                self._const_int_to_bytes(current, storage_size),
                                member_type,
                            )
                        )
                    return ir.Constant(ir_type, values)
                except Exception:
                    return self._zero_initializer(ir_type)
            if isinstance(init_node, c_ast.InitList):
                init_node = self._normalize_initializer_for_type(init_node, ir_type)
                exprs = self._ordered_struct_init_exprs(init_node, ir_type)
                values = []
                for i, member_type in enumerate(ir_type.elements):
                    expr = exprs[i] if i < len(exprs) else None
                    values.append(self._build_const_init(expr, member_type))
                try:
                    return ir.Constant(ir_type, values)
                except Exception:
                    return self._zero_initializer(ir_type)
            return self._zero_initializer(ir_type)

        if isinstance(init_node, c_ast.InitList):
            if init_node.exprs:
                return self._build_const_init(init_node.exprs[0], ir_type)
            return self._zero_initializer(ir_type)

        try:
            val = self._eval_const_expr(init_node)
            result = self._ir_constant_from_value(ir_type, val)
            str(result)
            return result
        except Exception as exc:
            raise ValueError(
                "initializer is not a compile-time constant"
            ) from exc

    def _init_array(self, base_addr, init_list, elem_ir_type, prefix_idx):
        """Recursively initialize array elements from an InitList."""
        for i, expr in enumerate(init_list.exprs):
            idx = prefix_idx + [ir.Constant(int64_t, i)]
            if isinstance(expr, c_ast.InitList) and isinstance(elem_ir_type, ir.ArrayType):
                self._init_array(base_addr, expr, elem_ir_type.element, idx)
                continue
            elem_ptr = self.builder.gep(base_addr, idx, inbounds=True)
            self._init_runtime_value(elem_ptr, elem_ir_type, expr)

    def _init_runtime_value(self, dest_ptr, target_type, init_node):
        if dest_ptr is None or init_node is None:
            return

        if isinstance(init_node, c_ast.CompoundLiteral):
            init_node = init_node.init

        if isinstance(target_type, ir.ArrayType):
            if self._is_array_string_initializer(init_node, target_type):
                data = self._string_literal_data(init_node)
                idx0 = ir.Constant(int64_t, 0)
                for i, value in enumerate(data[: target_type.count]):
                    elem_ptr = self.builder.gep(
                        dest_ptr,
                        [idx0, ir.Constant(int64_t, i)],
                        inbounds=True,
                    )
                    self.builder.store(ir.Constant(target_type.element, value), elem_ptr)
                return
            if isinstance(init_node, c_ast.InitList):
                init_node = self._normalize_initializer_for_type(init_node, target_type)
                ordered_exprs = self._ordered_array_init_exprs(init_node, target_type)
                self._init_array(
                    dest_ptr,
                    c_ast.InitList(ordered_exprs, init_node.coord),
                    target_type.element,
                    [ir.Constant(int64_t, 0)],
                )
                return

        if getattr(target_type, "is_union", False) or _is_struct_ir_type(target_type):
            if isinstance(init_node, c_ast.InitList):
                self._init_runtime_aggregate(dest_ptr, init_node, target_type)
                return
            init_val, _ = self.codegen(init_node)
            if init_val is not None:
                if init_val.type != target_type:
                    if getattr(target_type, "is_union", False):
                        # A non-list scalar initializer for a union targets
                        # its FIRST member (C99 6.7.9p17), not the whole
                        # aggregate. Storing the scalar as the union type
                        # emits invalid IR (`store <union> <int>`). Route the
                        # already-evaluated value into the first member via a
                        # bitcast, mirroring the InitList path's
                        # _select_union_initializer behavior.
                        first_type = target_type.elements[0]
                        member_ptr = self.builder.bitcast(
                            dest_ptr,
                            ir.PointerType(first_type),
                            name="unioninit",
                        )
                        if init_val.type != first_type:
                            init_val = self._implicit_convert(init_val, first_type)
                        self._safe_store(init_val, member_ptr)
                        return
                    init_val = self._implicit_convert(init_val, target_type)
                self._safe_store(init_val, dest_ptr)
            return

        scalar_node = self._scalar_init_node(init_node)
        if scalar_node is None:
            return
        init_val, _ = self.codegen(scalar_node)
        if init_val is None:
            return
        if init_val.type != target_type:
            init_val = self._implicit_convert(init_val, target_type)
        self._safe_store(init_val, dest_ptr)

    def _init_runtime_aggregate(self, base_addr, init_node, ir_type):
        init_node = self._normalize_initializer_for_type(init_node, ir_type)
        exprs = list(getattr(init_node, "exprs", None) or [])
        if getattr(ir_type, "is_union", False):
            field_index, field_type, member_init = self._select_union_initializer(
                init_node, ir_type
            )
            if field_index is None or member_init is None:
                return
            field_ptr = self.builder.bitcast(
                base_addr,
                ir.PointerType(field_type),
                name="unioninit",
            )
            self._init_runtime_value(field_ptr, field_type, member_init)
            return

        if getattr(ir_type, "has_custom_layout", False):
            exprs = self._ordered_struct_init_exprs(init_node, ir_type)
            for i, field_name in enumerate(getattr(ir_type, "members", ())):
                if i >= len(exprs):
                    break
                expr = exprs[i]
                layout = ir_type.field_layouts.get(field_name)
                if layout is None:
                    continue
                if layout.is_bitfield:
                    scalar_node = self._scalar_init_node(expr)
                    if scalar_node is None:
                        continue
                    field_val, _ = self.codegen(scalar_node)
                    if field_val is None:
                        continue
                    if field_val.type != layout.semantic_ir_type:
                        field_val = self._implicit_convert(
                            field_val,
                            layout.semantic_ir_type,
                        )
                    ref = BitFieldRef(
                        container_ptr=self._byte_offset_ptr(
                            base_addr,
                            layout.storage_byte_offset,
                            ir.PointerType(layout.storage_ir_type),
                            name="bitfieldptr",
                        ),
                        storage_ir_type=layout.storage_ir_type,
                        bit_offset=layout.bit_offset,
                        bit_width=layout.bit_width,
                        semantic_ir_type=layout.semantic_ir_type,
                        is_unsigned=layout.is_unsigned,
                    )
                    self._store_bitfield(field_val, ref)
                    continue

                field_ptr = self._byte_offset_ptr(
                    base_addr,
                    layout.byte_offset,
                    ir.PointerType(layout.semantic_ir_type),
                    name="fieldptr",
                )
                self._init_runtime_value(field_ptr, layout.semantic_ir_type, expr)
            return

        exprs = self._ordered_struct_init_exprs(init_node, ir_type)
        for i, field_type in enumerate(ir_type.elements):
            if i >= len(exprs):
                break
            expr = exprs[i]
            field_addr = self.builder.gep(
                base_addr,
                [ir.Constant(int64_t, 0), ir.Constant(ir.IntType(32), i)],
                inbounds=True,
            )
            semantic_field_type = self._refine_member_ir_type(ir_type, i, field_type)
            typed_field_addr = field_addr
            target_ptr_type = ir.PointerType(semantic_field_type)
            if field_addr.type != target_ptr_type:
                try:
                    typed_field_addr = self.builder.bitcast(
                        field_addr,
                        target_ptr_type,
                    )
                except Exception:
                    typed_field_addr = field_addr
            self._init_runtime_value(typed_field_addr, semantic_field_type, expr)

    def _select_union_initializer(self, init_node, ir_type):
        member_names = self._aggregate_member_names(ir_type)
        if not member_names:
            return None, None, None

        field_index = 0
        field_type = self._refine_member_ir_type(
            ir_type,
            field_index,
            self._aggregate_member_ir_type(ir_type, field_index),
        )
        member_init = init_node

        if isinstance(init_node, c_ast.InitList):
            exprs = init_node.exprs or []
            if not exprs:
                return field_index, field_type, None

            first_expr = exprs[0]
            if isinstance(first_expr, c_ast.NamedInitializer):
                designators = getattr(first_expr, "name", None) or []
                if len(designators) == 1 and isinstance(designators[0], c_ast.ID):
                    candidate = designators[0].name
                    named_member_indices = getattr(
                        ir_type, "named_member_indices", None
                    ) or {}
                    if candidate in named_member_indices:
                        field_index = named_member_indices[candidate]
                        field_type = self._refine_member_ir_type(
                            ir_type,
                            field_index,
                            self._aggregate_member_ir_type(ir_type, field_index),
                        )
                        return field_index, field_type, first_expr.expr
                first_expr = first_expr.expr

            if isinstance(field_type, (ir.ArrayType, ir.IdentifiedStructType, ir.LiteralStructType)):
                member_init = (
                    first_expr
                    if len(exprs) == 1 and isinstance(first_expr, c_ast.InitList)
                    else init_node
                )
            else:
                member_init = first_expr

        return field_index, field_type, member_init

    def _build_array_ir_type(self, array_decl, init_node=None):
        dims = []
        node = array_decl
        top_elem_ir_type = None
        try:
            if isinstance(node.type, c_ast.ArrayDecl):
                top_elem_ir_type = self._build_array_ir_type(node.type)
            else:
                top_elem_ir_type = self._resolve_ast_type(node.type)
        except Exception:
            top_elem_ir_type = None
        inferred_top_dim = self._infer_array_count_from_initializer(
            init_node, top_elem_ir_type
        )
        is_top_level = True
        while isinstance(node, c_ast.ArrayDecl):
            dim = self._eval_dim(node.dim) if node.dim else 0
            if dim == 0 and is_top_level and inferred_top_dim is not None:
                dim = inferred_top_dim
            dims.append(dim)
            node = node.type
            is_top_level = False
        elem_ir_type = self._resolve_ast_type(node)
        if isinstance(elem_ir_type, ir.VoidType):
            elem_ir_type = int8_t
        arr_ir_type = elem_ir_type
        for dim in reversed(dims):
            arr_ir_type = self._checked_array_ir_type(arr_ir_type, dim)
        arr_ir_type.dim_array = dims
        return arr_ir_type
