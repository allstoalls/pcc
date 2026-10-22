"""Format lowering helpers for Layer-1 Python codegen."""

from __future__ import annotations

from typing import Optional

from pcc.ir.compat import ir

from pcc.frontends.python.py_ast import Assign, Attr, AugAssign, Call, ClassDef, DictType, Expr, For, FuncDef, If, Name, StrLit, StrType, Try, While, With
from pcc.frontends.python.codegen import marshal

_I64 = ir.IntType(64)


class FormatLoweringMixin:
    def _parse_auto_format_literal(
        self,
        fmt: str,
        argc: int,
    ) -> Optional[list[str | tuple[str]]]:
        parts: list = []
        buf: list[str] = []
        placeholders = 0
        saw_auto = False
        saw_manual = False
        i = 0
        while i < len(fmt):
            ch = fmt[i]
            if ch == "{":
                if i + 1 < len(fmt) and fmt[i + 1] == "{":
                    buf.append("{")
                    i += 2
                    continue
                end = -1
                j = i + 1
                depth = 0
                while j < len(fmt):
                    if fmt[j] == "{":
                        depth += 1
                    elif fmt[j] == "}":
                        if depth == 0:
                            end = j
                            break
                        depth -= 1
                    j += 1
                if end < 0:
                    return None
                field = fmt[i + 1 : end]
                if ":" in field:
                    field_pieces = field.split(":", 1)
                    ref_part = field_pieces[0]
                    spec = field_pieces[1]
                else:
                    ref_part = field
                    spec = ""
                # ``{name!conv:spec}`` — the conversion (``!r``/``!s``/``!a``)
                # comes after the field name and before the spec. Strip it from
                # ref_part; emit applies repr/str/ascii before formatting.
                conv = None
                if "!" in ref_part:
                    rp = ref_part.split("!", 1)
                    ref_part = rp[0]
                    conv = rp[1]
                    if conv != "r" and conv != "s" and conv != "a":
                        return None
                if "{" in spec or "}" in spec:
                    # A nested replacement field inside the spec (e.g. the
                    # ``>{}`` in ``"{:>{}}"``). Parse one level so the width /
                    # precision argument can be resolved at runtime instead of
                    # forcing a libpython fallback.
                    nested = self._parse_format_spec_with_nested(spec)
                    if nested is None:
                        return None
                    spec, n_auto, n_has_index, n_has_name = nested
                    if n_auto:
                        saw_auto = True
                        placeholders += n_auto
                    if n_has_index:
                        saw_manual = True
                    # Nested named fields ({name}) mix freely with auto/manual
                    # outer numbering in CPython, so they set no flag here.
                if ref_part == "":
                    kind = "auto"
                    saw_auto = True
                elif ref_part.isdigit():
                    kind = "index"
                    saw_manual = True
                else:
                    kind = "name"
                    saw_manual = True
                if buf:
                    parts.append("".join(buf))
                    buf = []
                parts.append((spec, kind, ref_part, conv))
                placeholders += 1
                i = end + 1
                continue
            if ch == "}":
                if i + 1 < len(fmt) and fmt[i + 1] == "}":
                    buf.append("}")
                    i += 2
                    continue
                return None
            buf.append(ch)
            i += 1
        if buf:
            parts.append("".join(buf))
        if saw_auto and saw_manual:
            return None  # CPython forbids mixing auto and manual
        if saw_auto and argc >= 0 and placeholders != argc:
            return None
        return parts

    def _parse_format_spec_with_nested(self, spec: str):
        """Parse one level of nested replacement fields inside a format spec.

        The spec of ``"{:>{}}"`` is ``">{}"``; this returns the pieces
        ``[">", ("field", "auto", "")]`` so the width argument can be resolved
        and formatted at runtime. Returns ``(pieces, n_auto, has_index,
        has_name)`` or ``None`` when the spec is outside the supported bounded
        subset. CPython allows only one nesting level, so a nested field must
        not carry its own ``:spec``, ``!conv``, or a further ``{``.
        """
        pieces: list = []
        buf: list[str] = []
        n_auto = 0
        has_index = False
        has_name = False
        i = 0
        n = len(spec)
        while i < n:
            ch = spec[i]
            if ch == "{":
                # Manual scan for the closing brace (self-host safe: no 2-arg
                # str.find on the no-libpython path).
                j = i + 1
                close = -1
                while j < n:
                    if spec[j] == "}":
                        close = j
                        break
                    j += 1
                if close < 0:
                    return None
                inner = spec[i + 1 : close]
                if "{" in inner or ":" in inner or "!" in inner:
                    return None
                if buf:
                    pieces.append("".join(buf))
                    buf = []
                if inner == "":
                    pieces.append(("field", "auto", ""))
                    n_auto += 1
                elif inner.isdigit():
                    pieces.append(("field", "index", inner))
                    has_index = True
                else:
                    # Any non-empty, non-numeric token is a named field; an
                    # unresolvable name falls back at emit time. Mirrors the
                    # permissive name handling in _parse_auto_format_literal.
                    pieces.append(("field", "name", inner))
                    has_name = True
                i = close + 1
                continue
            if ch == "}":
                return None
            buf.append(ch)
            i += 1
        if buf:
            pieces.append("".join(buf))
        return pieces, n_auto, has_index, has_name

    def _emit_structured_spec_obj(
        self,
        spec_pieces,
        args,
        kw_map,
        auto_idx: int,
    ) -> tuple[Optional[ir.Value], int]:
        """Build the runtime format-spec string for a spec with nested fields.

        Returns ``(spec_value, new_auto_idx)``; ``spec_value`` is ``None`` when a
        nested field cannot be resolved (the caller then falls back). Nested
        auto fields advance ``auto_idx`` so positional numbering matches
        CPython's textual order: the outer field first, then its spec fields.
        """
        out: Optional[ir.Value] = None
        for piece in spec_pieces:
            if isinstance(piece, str):
                sval = self._emit_str_literal(piece)
            else:
                _, nkind, nref = piece
                if nkind == "auto":
                    if auto_idx >= len(args):
                        return None, auto_idx
                    narg = args[auto_idx]
                    auto_idx += 1
                elif nkind == "index":
                    nidx = int(nref)
                    if nidx < 0 or nidx >= len(args):
                        return None, auto_idx
                    narg = args[nidx]
                else:  # name
                    if nref not in kw_map:
                        return None, auto_idx
                    narg = kw_map[nref]
                nobj = self._emit_as_object(narg)
                sval = self.builder.call(
                    self.runtime["py_obj_format"],
                    [nobj, self._emit_str_literal("")],
                    name=self._fresh("format.nested.field"),
                )
                self._emit_post_call_err_check(getattr(narg, "span", None))
            if out is None:
                out = sval
            else:
                out = self.builder.call(
                    self.runtime["py_str_concat"],
                    [out, sval],
                    name=self._fresh("format.spec.concat"),
                )
        if out is None:
            out = self._emit_str_literal("")
        return out, auto_idx

    def _emit_format_arg_as_str(self, arg: Expr, spec: str = "") -> ir.Value:
        obj = self._emit_as_object(arg)
        return self._emit_format_obj_as_str(
            obj,
            spec,
            getattr(arg, "span", None),
        )

    def _emit_format_obj_as_str(
        self,
        obj: ir.Value,
        spec: str = "",
        span=None,
    ) -> ir.Value:
        spec_obj = self._emit_str_literal(spec)
        formatted = self.builder.call(
            self.runtime["py_obj_format"],
            [obj, spec_obj],
            name=self._fresh("format.field"),
        )
        self._emit_post_call_err_check(span)
        return formatted

    def _emit_apply_conversion(self, obj: ir.Value, conv, span=None) -> ir.Value:
        """Apply a ``{!r}``/``{!s}``/``{!a}`` conversion to ``obj`` (returns a
        str object). ``conv`` is None / "r" / "s" / "a"."""
        if conv is None:
            return obj
        fn = {"r": "py_obj_repr", "s": "py_obj_str", "a": "py_obj_ascii"}[conv]
        converted = self.builder.call(
            self.runtime[fn], [obj], name=self._fresh("format.conv")
        )
        self._emit_post_call_err_check(span)
        return converted

    def _emit_obj_format_call(self, value_expr: Expr, spec_expr: Expr) -> ir.Value:
        value_obj = self._emit_as_object(value_expr)
        spec_obj = self._emit_as_object(spec_expr)
        result = self.builder.call(
            self.runtime["py_obj_format"],
            [value_obj, spec_obj],
            name=self._fresh("format.obj"),
        )
        self._emit_post_call_err_check(getattr(value_expr, "span", None))
        return result

    def _emit_obj_format_literal_spec(self, value_expr: Expr, spec: str) -> ir.Value:
        value_obj = self._emit_as_object(value_expr)
        spec_obj = self._emit_str_literal(spec)
        return self.builder.call(
            self.runtime["py_obj_format"],
            [value_obj, spec_obj],
            name=self._fresh("format.obj"),
        )

    def _resolve_str_literal_value(self, expr: Expr) -> Optional[str]:
        """Return the static string value backing ``expr`` when it can be
        resolved to a single literal.

        - ``StrLit``: the literal directly.
        - ``Name``: tries the current function body first; if the name
          isn't bound there at all, falls back to the module's top-level
          body (descending into top-level If/Try/With/While/For without
          stepping into nested FuncDef / ClassDef). In either scope,
          requires exactly one ``Name = <StrLit>`` Assign and no other
          rebind (AugAssign, For-target, except-handler name, With-as,
          or non-StrLit Assign).

        Returning None means the caller must fall back to the libpython
        path. This helper exists so that ``fmt = "{x}"; fmt.format(x=...)``
        and the module-level ``_MSG = "..."; comprehension {... _MSG.format(...) ...}``
        idioms (common in CPython packages such as numpy at
        ``numpy/__init__.py:637-663``) take the native format fast path.
        """
        if isinstance(expr, StrLit):
            return expr.value
        if not isinstance(expr, Name):
            return None
        target = expr.ident
        use_line = getattr(getattr(expr, "span", None), "line", 0)
        fd = getattr(self, "current_func_def", None)
        if fd is not None and self._body_binds_name(fd.body, target):
            value = self._scan_body_for_str_literal(fd.body, target)
            if value is not None:
                return value
            found, value = self._resolve_str_literal_on_line(
                fd.body,
                target,
                use_line,
            )
            if found:
                return value
            return None
        ast_module = getattr(self, "ast_module", None)
        if ast_module is None:
            return None
        module_body = getattr(ast_module, "body", ())
        value = self._scan_body_for_str_literal(module_body, target)
        if value is not None:
            return value
        found, value = self._resolve_str_literal_on_line(
            module_body,
            target,
            use_line,
        )
        if found:
            return value
        return None

    def _format_child_bodies(self, stmt) -> list:
        bodies: list = []
        if isinstance(stmt, (If, While, For)):
            bodies.append(stmt.body)
            bodies.append(stmt.else_body)
        elif isinstance(stmt, Try):
            bodies.append(stmt.body)
            for handler in stmt.handlers:
                bodies.append(handler.body)
            bodies.append(stmt.else_body)
            bodies.append(stmt.finally_body)
        elif isinstance(stmt, With):
            bodies.append(stmt.body)
        return bodies

    def _format_body_line_bounds(self, body) -> tuple[int, int]:
        lo = 0
        hi = 0
        for stmt in body:
            line = getattr(getattr(stmt, "span", None), "line", 0)
            if line > 0 and (lo == 0 or line < lo):
                lo = line
            if line > hi:
                hi = line
            for child in self._format_child_bodies(stmt):
                child_lo, child_hi = self._format_body_line_bounds(child)
                if child_lo > 0 and (lo == 0 or child_lo < lo):
                    lo = child_lo
                if child_hi > hi:
                    hi = child_hi
        return lo, hi

    def _resolve_str_literal_on_line(
        self,
        body,
        target_name: str,
        use_line: int,
    ) -> tuple[bool, Optional[str]]:
        """Resolve a literal binding that lexically dominates ``use_line``.

        Lifted statement spans currently retain only their starting line, so
        recurse through child-body line bounds instead of trusting a compound
        statement's ``end_line``. This is deliberately path-sensitive: an
        assignment in a sibling branch never becomes the format template.
        """
        found = False
        value: Optional[str] = None
        for stmt in body:
            line = getattr(getattr(stmt, "span", None), "line", 0)
            if use_line > 0 and line > use_line:
                break
            if isinstance(stmt, Assign):
                for target in stmt.targets:
                    if isinstance(target, Name) and target.ident == target_name:
                        found = True
                        if isinstance(stmt.value, StrLit):
                            value = stmt.value.value
                        else:
                            value = None
                        break
            for child in self._format_child_bodies(stmt):
                child_lo, child_hi = self._format_body_line_bounds(child)
                if child_lo <= use_line <= child_hi:
                    child_found, child_value = self._resolve_str_literal_on_line(
                        child,
                        target_name,
                        use_line,
                    )
                    if child_found:
                        return True, child_value
                    return found, value
        return found, value

    def _scan_body_for_str_literal(self, body, target_name: str) -> Optional[str]:
        found = None
        rebind_count = 0
        for stmt in self._iter_function_str_lit_bindings(body, target_name):
            rebind_count += 1
            if rebind_count > 1:
                return None
            found = stmt
        return found

    def _body_binds_name(self, body, target_name: str) -> bool:
        """Return True iff ``target_name`` is the target of ANY binding
        construct in ``body`` (Assign, AugAssign, For-target, except-
        handler name, With-as). Used to decide whether to shadow the
        module-level resolution: if a function rebinds the name locally,
        we must NOT fall back to the module-level value.
        """
        pending = list(body)
        while pending:
            s = pending.pop()
            if isinstance(s, (FuncDef, ClassDef)):
                continue
            if isinstance(s, Assign):
                for t in s.targets:
                    if isinstance(t, Name) and t.ident == target_name:
                        return True
                continue
            if isinstance(s, AugAssign) and isinstance(s.target, Name):
                if s.target.ident == target_name:
                    return True
                continue
            if isinstance(s, If):
                pending.extend(s.body)
                pending.extend(s.else_body)
                continue
            if isinstance(s, While):
                pending.extend(s.body)
                pending.extend(s.else_body)
                continue
            if isinstance(s, For):
                if isinstance(s.target, Name) and s.target.ident == target_name:
                    return True
                pending.extend(s.body)
                pending.extend(s.else_body)
                continue
            if isinstance(s, Try):
                pending.extend(s.body)
                pending.extend(s.else_body)
                pending.extend(s.finally_body)
                for h in s.handlers:
                    if h.name == target_name:
                        return True
                    pending.extend(h.body)
                continue
            if isinstance(s, With):
                for ctx_pair in s.items:
                    as_var = ctx_pair[1]
                    if isinstance(as_var, Name) and as_var.ident == target_name:
                        return True
                pending.extend(s.body)
                continue
        return False

    def _iter_function_str_lit_bindings(self, body, target_name: str):
        """Yield the StrLit value for every ``Assign(targets=[Name(target_name)])``
        whose value is a ``StrLit``; yield a sentinel ``None`` and stop
        iteration when ``target_name`` is rebound by any other shape so
        the caller can treat the binding as not-a-pure-literal.

        Walks the transitive body (descending into If/For/While/Try/With
        bodies) but does NOT descend into nested FuncDef / ClassDef
        scopes — those have their own name bindings.
        """
        pending = list(body)
        while pending:
            s = pending.pop()
            if isinstance(s, (FuncDef, ClassDef)):
                continue
            if isinstance(s, Assign):
                if len(s.targets) == 1 and isinstance(s.targets[0], Name):
                    if s.targets[0].ident == target_name:
                        if isinstance(s.value, StrLit):
                            yield s.value.value
                        else:
                            yield None
                            return
                continue
            if isinstance(s, AugAssign) and isinstance(s.target, Name):
                if s.target.ident == target_name:
                    yield None
                    return
                continue
            if isinstance(s, If):
                pending.extend(s.body)
                pending.extend(s.else_body)
                continue
            if isinstance(s, While):
                pending.extend(s.body)
                pending.extend(s.else_body)
                continue
            if isinstance(s, For):
                if isinstance(s.target, Name) and s.target.ident == target_name:
                    yield None
                    return
                pending.extend(s.body)
                pending.extend(s.else_body)
                continue
            if isinstance(s, Try):
                pending.extend(s.body)
                pending.extend(s.else_body)
                pending.extend(s.finally_body)
                for h in s.handlers:
                    if h.name == target_name:
                        yield None
                        return
                    pending.extend(h.body)
                continue
            if isinstance(s, With):
                for ctx_pair in s.items:
                    as_var = ctx_pair[1]
                    if isinstance(as_var, Name) and as_var.ident == target_name:
                        yield None
                        return
                pending.extend(s.body)
                continue

    def _maybe_emit_literal_str_format(self, expr: Call) -> Optional[ir.Value]:
        attr = expr.func
        assert isinstance(attr, Attr)
        if attr.name != "format":
            return None
        fmt_str = self._resolve_str_literal_value(attr.obj)
        if fmt_str is None:
            return None
        star_arg = None
        direct_args = expr.args
        starred_count = 0
        for arg in expr.args:
            if self._is_starred_unpack_expr(arg):
                starred_count += 1
        if starred_count:
            # Bounded native mixed-splat lane: one trailing ``*iterable``.
            # Logical positional indices before the splat map to direct_args;
            # later ones index the runtime iterable.  A suffix after the splat
            # would need runtime tuple concatenation and remains outside this
            # focused lowering.
            if starred_count != 1 or not self._is_starred_unpack_expr(expr.args[-1]):
                return None
            star_arg = expr.args[-1].args[0]
            direct_args = expr.args[:-1]
        parts = self._parse_auto_format_literal(
            fmt_str, -1 if star_arg is not None else len(direct_args)
        )
        if parts is None:
            return None
        kw_map = {}
        for kw in expr.kwargs:
            kname = kw[0]
            if kname is None or kname == "":
                return None
            if kname.startswith("*"):
                return None  # **splat: not handled here
            kw_map[kname] = kw[1]
        if (
            star_arg is None
            and all(not isinstance(part, tuple) or isinstance(part[0], str) for part in parts)
            and not any(self._expr_looks_cpython(argument) for argument in expr.args)
            and not any(self._expr_looks_cpython(argument) for _key, argument in expr.kwargs)
        ):
            # Evaluate every argument once before conversion/__format__,
            # then publish each NEW field and concatenation into an owner.
            # Repeated fields reuse argument roots; other existing format
            # shapes retain their separate lowering below.
            fields = []
            auto_index = 0
            for part in parts:
                if not isinstance(part, tuple):
                    fields.append(part)
                    continue
                spec, kind, ref, conv = part
                if kind == "auto":
                    ref = auto_index
                    auto_index += 1
                elif kind == "index":
                    ref = int(ref)
                if kind == "name":
                    if ref not in kw_map:
                        return None
                elif ref < 0 or ref >= len(expr.args):
                    return None
                fields.append((spec, kind, ref, conv))

            sink = self._slot_call_result_sink(expr)
            output = sink
            roots = []
            if output is None:
                output = self._new_slot_call_root("str.format.result")
                roots.append(output)
            previous = self._current_try_err_block()
            target = previous if previous is not None else self._ensure_fn_err_exit()
            saved_cpy = self._cpy_operand_cleanup_block
            # Inputs remain owned for the complete call. Register their empty
            # slots before evaluation, so one cleanup handles every argument
            # prefix instead of rebuilding a growing cleanup for each input.
            argument_exprs = [expr.func.obj] + list(expr.args)
            argument_exprs.extend(argument for _key, argument in expr.kwargs)
            arguments = []
            for argument in argument_exprs:
                slot = self._new_slot_call_root("str.format.argument")
                arguments.append(slot)
                roots.append(slot)
            # These five owners are reused by every field. Clearing a scratch
            # owner retires its value, not its lexical frame registration.
            converted = self._new_slot_call_root("str.format.converted")
            spec_root = self._new_slot_call_root("str.format.spec")
            piece = self._new_slot_call_root("str.format.field")
            current = self._new_slot_call_root("str.format.accumulator")
            combined = self._new_slot_call_root("str.format.concat")
            roots.extend((converted, spec_root, piece, current, combined))
            cleanup = self._slot_call_cleanup_block(tuple(roots), target)
            try:
                self._try_err_block = cleanup
                self._cpy_operand_cleanup_block = cleanup
                for argument, destination in zip(argument_exprs, arguments):
                    temporary = self._emit_slot_call_operand(argument, "str.format.input")
                    self._try_err_block = self._slot_call_cleanup_block((temporary,), cleanup)
                    self._cpy_operand_cleanup_block = self._try_err_block
                    moved = self.builder.call(
                        self.runtime["pcc_gc_root_move"],
                        [self._as_gc_ptr(destination), self._as_gc_ptr(temporary)],
                        name=self._fresh("str.format.argument.move"),
                    )
                    self._slot_call_check_status(moved, "format argument move", expr.span)
                    self._release_slot_call_roots((temporary,))
                    self._try_err_block = cleanup
                    self._cpy_operand_cleanup_block = cleanup
                positional = arguments[1:1 + len(expr.args)]
                keywords = {}
                for index, (key, _argument) in enumerate(expr.kwargs):
                    keywords[key] = arguments[1 + len(expr.args) + index]
                # Multiple fields copy each transient result before the
                # next callback. A sole field keeps Python's result identity,
                # including a str subclass returned by __format__.
                single_field = len(fields) == 1
                if not single_field:
                    empty = self._emit_str_literal("")
                    self.builder.call(self.runtime["py_incref"], [self._as_gc_ptr(empty)])
                    self._publish_slot_call_owned(current, empty, label="format empty literal")
                for field in fields:
                    if isinstance(field, str):
                        literal = self._emit_str_literal(field)
                        self.builder.call(self.runtime["py_incref"], [self._as_gc_ptr(literal)])
                        self._publish_slot_call_owned(piece, literal, label="format literal")
                    else:
                        spec, kind, ref, conv = field
                        value = keywords[ref] if kind == "name" else positional[ref]
                        if conv is not None:
                            runtime_name = {"r": "py_obj_repr", "s": "py_obj_str", "a": "py_obj_ascii"}[conv]
                            self._slot_call_runtime_call(
                                runtime_name, (value,), result_slot=converted, span=expr.span,
                            )
                            value = converted
                        literal = self._emit_str_literal(spec)
                        self.builder.call(self.runtime["py_incref"], [self._as_gc_ptr(literal)])
                        self._publish_slot_call_owned(spec_root, literal, label="format spec literal")
                        self._slot_call_runtime_call(
                            "py_obj_format", (value, spec_root), result_slot=piece, span=expr.span,
                        )
                    if single_field:
                        # CPython returns the field itself only when it is
                        # nonempty. Validate the runtime value before reading
                        # its native string length; a callback may be invalid.
                        checked = self._slot_call_runtime_call("py_str_check", (piece,), span=expr.span)
                        is_string = self.builder.icmp_signed("!=", checked, ir.Constant(_I64, 0))
                        typed = self.current_function.append_basic_block(self._fresh("str.format.string"))
                        invalid = self.current_function.append_basic_block(self._fresh("str.format.invalid"))
                        self.builder.cbranch(is_string, typed, invalid)
                        self.builder.position_at_end(invalid)
                        self._emit_builtin_exception_and_branch(
                            "TypeError", "__format__ must return a str", expr.span,
                        )
                        self.builder.position_at_end(typed)
                        length = self._slot_call_runtime_call("py_str_byte_len", (piece,), span=expr.span)
                        is_empty = self.builder.icmp_signed("==", length, ir.Constant(_I64, 0))
                        empty_result = self.current_function.append_basic_block(self._fresh("str.format.empty"))
                        keep_result = self.current_function.append_basic_block(self._fresh("str.format.keep"))
                        result_ready = self.current_function.append_basic_block(self._fresh("str.format.ready"))
                        self.builder.cbranch(is_empty, empty_result, keep_result)
                        self.builder.position_at_end(empty_result)
                        empty = self._emit_str_literal("")
                        self.builder.call(self.runtime["py_incref"], [self._as_gc_ptr(empty)])
                        self._publish_slot_call_owned(current, empty, label="format empty field")
                        self.builder.branch(result_ready)
                        self.builder.position_at_end(keep_result)
                        next_value = piece
                    else:
                        self._slot_call_runtime_call(
                            "py_str_concat", (current, piece), result_slot=combined, span=expr.span,
                        )
                        self.builder.call(
                            self.runtime["pcc_gc_store_root"],
                            [self._as_gc_ptr(current), ir.Constant(ir.IntType(8).as_pointer(), None)],
                        )
                        next_value = combined
                    moved = self.builder.call(
                        self.runtime["pcc_gc_root_move"],
                        [self._as_gc_ptr(current), self._as_gc_ptr(next_value)],
                        name=self._fresh("str.format.accumulator.move"),
                    )
                    self._slot_call_check_status(moved, "format accumulator move", expr.span)
                    if single_field:
                        self.builder.branch(result_ready)
                        self.builder.position_at_end(result_ready)
                    # Match field-consumption lifetime: the result and its
                    # conversion temporary retire before the next callback.
                    for scratch in (piece, converted, spec_root):
                        self.builder.call(
                            self.runtime["pcc_gc_store_root"],
                            [self._as_gc_ptr(scratch), ir.Constant(ir.IntType(8).as_pointer(), None)],
                        )
                moved = self.builder.call(
                    self.runtime["pcc_gc_root_move"],
                    [self._as_gc_ptr(output), self._as_gc_ptr(current)],
                    name=self._fresh("str.format.move"),
                )
                self._slot_call_note_published(output)
                self._slot_call_check_status(moved, "formatted result move", expr.span)
                self._release_slot_call_roots(tuple(roots[1:] if sink is None else roots))
                if sink is None:
                    return self._take_slot_call_root(output)
                return self.builder.load(output, name=self._fresh("str.format.current"))
            finally:
                self._try_err_block = previous
                self._cpy_operand_cleanup_block = saved_cpy
        star_obj = self._emit_as_object(star_arg) if star_arg is not None else None
        out: Optional[ir.Value] = None
        auto_idx = 0
        for part in parts:
            if isinstance(part, tuple):
                spec = part[0]
                kind = part[1]
                ref = part[2]
                conv = part[3] if len(part) > 3 else None
                arg_obj = None
                if kind == "index":
                    idx = int(ref)
                    if star_arg is not None and idx >= len(direct_args):
                        if not isinstance(spec, str):
                            return None  # nested spec + *args: fall back
                        assert star_obj is not None
                        idx_obj = self.builder.call(
                            self.runtime["py_int_from_i64"],
                            [ir.Constant(_I64, idx - len(direct_args))],
                            name=self._fresh("format.star.idx"),
                        )
                        arg_obj = self.builder.call(
                            self.runtime["py_obj_getitem"],
                            [star_obj, idx_obj],
                            name=self._fresh("format.star.item"),
                        )
                        self._emit_post_call_err_check(getattr(star_arg, "span", None))
                    else:
                        if idx < 0 or idx >= len(direct_args):
                            return None
                        argexpr = direct_args[idx]
                elif kind == "name":
                    if ref not in kw_map:
                        return None
                    argexpr = kw_map[ref]
                else:
                    idx = auto_idx
                    auto_idx += 1
                    if star_arg is not None and idx >= len(direct_args):
                        if not isinstance(spec, str):
                            return None
                        assert star_obj is not None
                        idx_obj = self.builder.call(
                            self.runtime["py_int_from_i64"],
                            [ir.Constant(_I64, idx - len(direct_args))],
                            name=self._fresh("format.star.idx"),
                        )
                        arg_obj = self.builder.call(
                            self.runtime["py_obj_getitem"],
                            [star_obj, idx_obj],
                            name=self._fresh("format.star.item"),
                        )
                        self._emit_post_call_err_check(getattr(star_arg, "span", None))
                    else:
                        if idx < 0 or idx >= len(direct_args):
                            return None
                        argexpr = direct_args[idx]
                if arg_obj is not None:
                    arg_obj = self._emit_apply_conversion(
                        arg_obj, conv, getattr(star_arg, "span", None)
                    )
                    piece = self._emit_format_obj_as_str(
                        arg_obj,
                        spec,
                        getattr(star_arg, "span", None),
                    )
                elif isinstance(spec, str):
                    if conv is not None:
                        obj = self._emit_as_object(argexpr)
                        obj = self._emit_apply_conversion(
                            obj, conv, getattr(argexpr, "span", None)
                        )
                        piece = self._emit_format_obj_as_str(
                            obj, spec, getattr(argexpr, "span", None)
                        )
                    else:
                        piece = self._emit_format_arg_as_str(argexpr, spec)
                else:
                    value_obj = self._emit_as_object(argexpr)
                    value_obj = self._emit_apply_conversion(
                        value_obj, conv, getattr(argexpr, "span", None)
                    )
                    spec_obj, auto_idx = self._emit_structured_spec_obj(
                        spec, direct_args, kw_map, auto_idx
                    )
                    if spec_obj is None:
                        return None
                    piece = self.builder.call(
                        self.runtime["py_obj_format"],
                        [value_obj, spec_obj],
                        name=self._fresh("str.format.field"),
                    )
                    self._emit_post_call_err_check(getattr(argexpr, "span", None))
            else:
                piece = self._emit_str_literal(part)
            if out is None:
                out = piece
            else:
                out = self.builder.call(
                    self.runtime["py_str_concat"],
                    [out, piece],
                    name=self._fresh("str.format.concat"),
                )
        if out is None:
            return self._emit_str_literal("")
        return out

    def _maybe_emit_literal_str_format_map(self, expr: Call) -> Optional[ir.Value]:
        """Lower ``"...{name}...".format_map(mapping)`` natively.

        Same compile-time field parse as ``.format()``, but each named field is
        resolved at runtime against the single mapping argument via the generic
        ``py_obj_getitem`` (works for dict and any mapping, and raises
        ``KeyError`` on a missing key exactly like CPython ``format_map``).

        Bounded scope (matches the common idiom): the format string must be a
        resolvable literal and every replacement field must be a ``{name}``
        named field. Auto ``{}`` and indexed ``{0}`` fields, ``**`` splats, or a
        non-literal format string bail to ``None`` so the caller can fall back.
        """
        attr = expr.func
        assert isinstance(attr, Attr)
        if attr.name != "format_map":
            return None
        if len(expr.args) != 1 or expr.kwargs:
            return None
        fmt_str = self._resolve_str_literal_value(attr.obj)
        if fmt_str is None:
            return None
        # argc=0 rejects auto ``{}`` fields at parse time; format_map keys are
        # explicit, never positional auto-numbered.
        parts = self._parse_auto_format_literal(fmt_str, 0)
        if parts is None:
            return None
        for part in parts:
            if isinstance(part, tuple) and part[1] != "name":
                return None  # only named fields handled natively
            if isinstance(part, tuple) and not isinstance(part[0], str):
                return None  # nested spec field: fall back

        map_arg = expr.args[0]
        map_obj = self._emit_as_object(map_arg)
        span = getattr(map_arg, "span", None)
        # A real dict resolves missing keys to a catchable KeyError via
        # py_dict_getitem (CPython format_map semantics). Any other mapping
        # goes through the generic py_obj_getitem.
        getitem_fn = (
            "py_dict_getitem"
            if isinstance(getattr(map_arg, "ty", None), DictType)
            else "py_obj_getitem"
        )
        out: Optional[ir.Value] = None
        for part in parts:
            if isinstance(part, tuple):
                spec = part[0]
                ref = part[2]
                conv = part[3] if len(part) > 3 else None
                key_obj = self._emit_str_literal(ref)
                val = self.builder.call(
                    self.runtime[getitem_fn],
                    [map_obj, key_obj],
                    name=self._fresh("format_map.get"),
                )
                self._emit_post_call_err_check(span)
                val = self._emit_apply_conversion(val, conv, span)
                spec_obj = self._emit_str_literal(spec)
                piece = self.builder.call(
                    self.runtime["py_obj_format"],
                    [val, spec_obj],
                    name=self._fresh("format_map.field"),
                )
                self._emit_post_call_err_check(span)
            else:
                piece = self._emit_str_literal(part)
            if out is None:
                out = piece
            else:
                out = self.builder.call(
                    self.runtime["py_str_concat"],
                    [out, piece],
                    name=self._fresh("format_map.concat"),
                )
        if out is None:
            return self._emit_str_literal("")
        return out

    def _emit_format_spec_builtin(self, expr: Call) -> Optional[ir.Value]:
        if len(expr.args) != 2 or expr.kwargs:
            return None
        value_expr, spec_expr = expr.args
        if not isinstance(spec_expr, StrLit):
            return None
        spec = spec_expr.value
        if spec.endswith("x"):
            body = spec[:-1]
            zero_pad = body.startswith("0") and len(body) > 0
            width_text = body[1:] if zero_pad else body
            if width_text == "" or width_text.isdigit():
                width = int(width_text) if width_text else 0
                value_obj = self._emit_as_object(value_expr)
                return self.builder.call(
                    self.runtime["py_int_format_hex"],
                    [
                        value_obj,
                        ir.Constant(_I64, width),
                        ir.Constant(_I64, 1 if zero_pad else 0),
                    ],
                    name=self._fresh("format.hex"),
                )
        if spec == "," or spec == "_" or spec.endswith("d"):
            body = spec[:-1] if spec.endswith("d") else spec
            comma = 0
            # ``comma`` carries the grouping separator *byte* (0 = none), so
            # py_int_format_decimal inserts the right char: ',' (44) or '_'
            # (95). Passing a bare 1 here would make the runtime emit chr(1).
            if body.endswith(","):
                comma = 44
                body = body[:-1]
            elif body.endswith("_"):
                comma = 95
                body = body[:-1]
            zero_pad = body.startswith("0") and len(body) > 0
            width_text = body[1:] if zero_pad else body
            if width_text == "" or width_text.isdigit():
                width = int(width_text) if width_text else 0
                value_obj = self._emit_as_object(value_expr)
                return self.builder.call(
                    self.runtime["py_int_format_decimal"],
                    [
                        value_obj,
                        ir.Constant(_I64, width),
                        ir.Constant(_I64, 1 if zero_pad else 0),
                        ir.Constant(_I64, comma),
                    ],
                    name=self._fresh("format.decimal"),
                )
        if (
            len(spec) >= 3
            and spec[0] == "."
            and spec[-1] == "f"
            and spec[1:-1].isdigit()
        ):
            precision = int(spec[1:-1])
            value_obj = self._emit_as_object(value_expr)
            return self.builder.call(
                self.runtime["py_float_format_fixed"],
                [value_obj, ir.Constant(_I64, precision)],
                name=self._fresh("format.fixed"),
            )
        result = self._emit_obj_format_literal_spec(value_expr, spec)
        self._emit_post_call_err_check(getattr(value_expr, "span", None))
        return result
