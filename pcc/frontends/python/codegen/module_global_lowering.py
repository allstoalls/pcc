"""Module-global and top-level prescan helpers for L1CodeGen."""

from __future__ import annotations

from pcc.ir.compat import ir

from pcc.frontends.python.py_ast import Assign, Attr, AugAssign, BoolLit, DynType, Expr, For, FuncDef, If, Import, ImportFrom, IntLit, IntType, ListExpr, Name, Stmt, Try, Type, TupleExpr, UnaryOp, While, With
from pcc.frontends.python.codegen import marshal
from pcc.frontends.python.codegen.errors import L1CodegenError
from pcc.frontends.python.codegen.import_lowering import _dataclass_field_names, _dataclass_field_value
from pcc.frontends.python.codegen.layer1_support import _import_from_module_or_empty
from pcc.frontends.python.py_ast import assignment_storage_annotation

_I8 = ir.IntType(8)
_I1 = ir.IntType(1)
_I64 = ir.IntType(64)
_I32 = ir.IntType(32)
_CSTR = _I8.as_pointer()


def _scalar_literal_initializer(expr) -> tuple[bool, int]:
    """Return a signed integer or Boolean literal for static storage."""
    if isinstance(expr, BoolLit):
        return (True, int(expr.value))
    if isinstance(expr, IntLit):
        value = int(expr.value)
        if -(1 << 63) <= value <= (1 << 63) - 1:
            return (True, value)
        return (False, 0)
    if isinstance(expr, UnaryOp) and expr.op in ("+", "-"):
        found, value = _scalar_literal_initializer(expr.operand)
        if not found:
            return (False, 0)
        if expr.op == "-":
            value = -value
        if -(1 << 63) <= value <= (1 << 63) - 1:
            return (True, value)
    return (False, 0)


def _import_names_from_stmt(stmt):
    pairs = []
    raw_names = getattr(stmt, "names", ())
    if not isinstance(raw_names, (tuple, list)):
        return tuple(pairs)
    for raw_name in raw_names:
        if isinstance(raw_name, (tuple, list)) and len(raw_name) >= 2:
            pairs.append((raw_name[0], raw_name[1]))
        elif isinstance(raw_name, (tuple, list)) and len(raw_name) >= 1:
            pairs.append((raw_name[0], None))
        elif hasattr(raw_name, "asname") or hasattr(raw_name, "name"):
            pairs.append(
                (
                    getattr(raw_name, "name", None),
                    getattr(raw_name, "asname", None),
                )
            )
        elif isinstance(raw_name, str):
            pairs.append((raw_name, None))
    return tuple(pairs)


def _is_import_stmt(stmt):
    if type(stmt).__name__ in {"Global", "Nonlocal"}:
        return False
    if type(stmt).__name__ in {"Import", "ImportFrom"}:
        return True
    if isinstance(stmt, (Import, ImportFrom)):
        return True
    raw_names = getattr(stmt, "names", ())
    if not raw_names:
        return False
    if not isinstance(raw_names, (tuple, list)):
        return False
    return all(
        isinstance(item, (tuple, list, str)) or hasattr(item, "name")
        for item in raw_names
    )


def _is_import_from_stmt(stmt):
    if type(stmt).__name__ == "ImportFrom":
        return True
    if isinstance(stmt, ImportFrom):
        return True
    if not _is_import_stmt(stmt):
        return False
    names = getattr(stmt, "names", ())
    return (
        hasattr(stmt, "module")
        and hasattr(stmt, "names")
        and isinstance(names, (tuple, list))
        and bool(names)
    )


def _zero_initializer_for(ir_ty):
    if isinstance(ir_ty, ir.IntType):
        return 0
    if isinstance(ir_ty, (ir.FloatType, ir.DoubleType)):
        return 0.0
    if isinstance(ir_ty, ir.PointerType):
        return None
    if isinstance(ir_ty, ir.LiteralStructType):
        return tuple(_zero_initializer_for(elem_ty) for elem_ty in ir_ty.elements)
    return 0


def _maybe_declare_native_module_attr_store(host, target: Expr) -> None:
    if not isinstance(target, Attr):
        return
    if not isinstance(target.obj, Name):
        return
    module_name = host._native_builtin_module_for_name(target.obj.ident)
    if module_name is None:
        module_name = getattr(
            host,
            "_native_module_aliases",
            {},
        ).get(target.obj.ident)
    if module_name is None:
        return
    host._native_module_attr_global(module_name, target.name)


def _predeclare_owned_builtin_import_globals(host, stmt, scope_names) -> bool:
    """Plan runtime builtin bindings before emitting consumers of the module."""
    module = host._resolve_relative_import(stmt)
    names = _import_names_from_stmt(stmt)
    if module not in ("builtins", "sys"):
        return False
    for imported_name, as_name in names:
        if module == "sys" and imported_name != "modules":
            continue
        if imported_name == "*":
            continue
        local_name = as_name or imported_name
        if scope_names is not None and local_name not in scope_names:
            continue
        slot, _declared = host._ensure_module_global_name(local_name, DynType(name="dyn"))
        if module == "sys" and imported_name == "modules":
            # Its executing producer uses _bind_owned_import_root. Plan the
            # same live binding before any function consumer is generated.
            host._native_extension_modules()[local_name] = slot
        # Imports execute at their source position and may be conditional or
        # fail part way through. Reuse the existing global bound-check set.
        if getattr(host, "_module_del_target_names", None) is None:
            host._module_del_target_names = set()
        host._module_del_target_names.add(local_name)
    return module == "builtins" or all(name == "modules" for name, _alias in names)


class ModuleGlobalLoweringMixin:
    def _module_global_symbol_name(
        self,
        module_name: str,
        name: str,
    ) -> str:
        mod_suffix = self._module_symbol_suffix(module_name)
        name_suffix = name.replace(".", "_").replace("-", "_")
        return f".modvar.{mod_suffix}.{name_suffix}"

    def _prescan_nested_imports(self, stmt) -> None:
        """Walk ``stmt``'s transitive body for Import / ImportFrom
        statements and seed ``_cpy_module_env`` so downstream user
        function bodies can resolve the names. The runtime import
        still runs inside the original stmt's body at main_body
        execution time; the scan only registers compile-time globals.
        """
        from pcc.frontends.python.py_ast import Import as _Import, ImportFrom as _ImportFrom

        pending = [(stmt, None)]
        while pending:
            s, fields = pending.pop()
            if _is_import_stmt(s):
                if _is_import_from_stmt(s):
                    import_module = _import_from_module_or_empty(s)
                    # ``continue`` (not ``return``) — the pending queue
                    # may still hold other nested ImportFrom stmts from
                    # sibling control-flow branches. Pre-2026-05-28 these
                    # branches used ``return`` and silently abandoned the
                    # rest of the queue when one nested ImportFrom hit a
                    # scaffold/test-facade/unsafe/native-builtin shape.
                    if self._is_extern_scaffold_import_module(import_module):
                        self._register_extern_scaffold_imports(s)
                        continue
                    if self._is_test_facade_import_module(import_module):
                        continue
                    if import_module == "pcc.unsafe":
                        self._register_unsafe_scaffold_imports(s)
                        continue
                    if _predeclare_owned_builtin_import_globals(self, s, None):
                        continue
                    if self._resolve_relative_import(s) == "sys":
                        remaining = tuple(item for item in _import_names_from_stmt(s) if item[0] != "modules")
                        if len(remaining) != len(_import_names_from_stmt(s)):
                            s = ImportFrom(s.span, s.module, remaining, s.level)
                    if self._register_native_builtin_import_from_aliases(
                        s,
                        self._resolve_relative_import(s),
                    ):
                        continue
                    resolved_extension = self._resolve_relative_import(s)
                    if (
                        self._resolve_pcc_native_extension_path(resolved_extension)
                        is not None
                    ):
                        for attr_name, as_name in _import_names_from_stmt(s):
                            if attr_name == "*":
                                self._native_extension_star_module_global(
                                    resolved_extension
                                )
                                continue
                            local_name = as_name or attr_name
                            gv = self._native_extension_module_global(local_name)
                            self._native_extension_modules()[local_name] = gv
                        continue
                    # Multi-file native cross-module pre-declare —
                    # mirror generation_lowering.py:315-367's top-level
                    # branch. Without this, an ``ImportFrom`` nested in
                    # a top-level ``if/try/with`` block (e.g.
                    # ``numpy/__init__.py``'s ``from ._core import (
                    # ones, ...)`` inside ``if not __NUMPY_SETUP__:``)
                    # only seeds ``_cpy_module_env`` and never binds
                    # ``self.functions[name]`` for native sibling
                    # exports — so a nested ``def`` that calls one of
                    # those names emits a static NameError, capping
                    # real-package self-host on the import path. See
                    # docs/investigations/
                    # python-native-module-alias-module-global-attr-attribute-error.md
                    # for the matching attribute-access blocker; the
                    # name-resolution blocker is its sibling.
                    native_table = self._native_module_exports
                    resolved = (
                        self._resolve_relative_import(s)
                        if native_table is not None
                        else None
                    )
                    handled_as_native_submodule = False
                    if native_table is not None:
                        remaining_names = []
                        for attr_name, as_name in _import_names_from_stmt(s):
                            full_submodule = self._native_import_from_submodule(
                                resolved,
                                attr_name,
                            )
                            if full_submodule is None:
                                full_submodule = (
                                    self._resolve_relative_import_submodule(
                                        s,
                                        resolved,
                                        attr_name,
                                    )
                                )
                            if (
                                full_submodule is not None
                                and full_submodule in native_table
                            ):
                                self._register_native_module_alias(
                                    as_name or attr_name,
                                    full_submodule,
                                )
                                continue
                            remaining_names.append((attr_name, as_name))
                        handled_as_native_submodule = not remaining_names
                    if handled_as_native_submodule:
                        continue
                    if (
                        native_table is not None
                        and self._has_native_import_from_targets(
                            s,
                            resolved,
                        )
                    ):
                        self._predeclare_native_cross_module(
                            s,
                            resolved,
                            native_table.get(resolved, {}),
                        )
                        continue
                    for attr_name, as_name in _import_names_from_stmt(s):
                        if attr_name == "*":
                            self._cpy_star_module_global(import_module)
                            continue
                        local_name = as_name or attr_name
                        self._cpy_module_global(local_name)
                        self._cpy_modules()[local_name] = self._cpy_module_global(
                            local_name
                        )
                    continue
                for mod_name, as_name in _import_names_from_stmt(s):
                    if self._is_test_facade_import_module(mod_name):
                        continue
                    if (
                        mod_name.split(".")[0]
                        in ("__future__", "typing", "abc", "click")
                        or mod_name == "pcc.extern"
                    ):
                        continue
                    if mod_name in (
                        "builtins",
                        "sys",
                        "os",
                        "time",
                        "string",
                        "platform",
                        "subprocess",
                        "tempfile",
                        "fileinput",
                        "shutil",
                        "shlex",
                        "math",
                        "json",
                        "re",
                        "codecs",
                        "gc",
                        "weakref",
                        "copy",
                        "pickle",
                        "threading",
                        "pcc.virtual_thread",
                        "pcc",
                        "inspect",
                        "contextlib",
                        "contextvars",
                        "enum",
                        "warnings",
                        "textwrap",
                    ):
                        self._register_native_builtin_module_alias(
                            as_name or mod_name,
                            mod_name,
                        )
                        continue
                    if as_name is None and "." in mod_name:
                        local_name = mod_name.split(".")[0]
                    else:
                        local_name = as_name or mod_name
                    if mod_name in getattr(self, "_sibling_module_inits", ()):
                        self._register_native_module_alias(local_name, mod_name)
                        gv = self._native_extension_module_global(local_name)
                        self._native_extension_modules()[local_name] = gv
                        continue
                    if self._resolve_pcc_native_extension_path(mod_name) is not None:
                        gv = self._native_extension_module_global(local_name)
                        self._native_extension_modules()[local_name] = gv
                        continue
                    self._cpy_module_global(local_name)
                    self._cpy_modules()[local_name] = self._cpy_module_global(
                        local_name
                    )
                continue
            if fields is None:
                fields = _dataclass_field_names(s)
            for slot in fields:
                if slot in ("span",):
                    continue
                v = _dataclass_field_value(s, slot, None)
                if isinstance(v, tuple):
                    for it in v:
                        child_fields = _dataclass_field_names(it)
                        if child_fields:
                            pending.append((it, child_fields))
                elif v is not None:
                    child_fields = _dataclass_field_names(v)
                    if child_fields:
                        pending.append((v, child_fields))

    def _predeclare_native_builtin_module_attr_stores(
        self,
        stmts: tuple[Stmt, ...],
    ) -> None:
        """Seed storage for native imports and ``sys.x = value`` attrs.

        Top-level module root registration runs before top-level statements,
        so slots that may be written there must exist before ``@main`` is
        emitted.  Function-body stores are also cheap to predeclare and share
        the same root/teardown path.
        """

        for stmt in stmts:
            pending = [(stmt, None)]
            while pending:
                s, fields = pending.pop()
                if isinstance(s, FuncDef) or type(s).__name__ == "ClassDef":
                    continue
                if isinstance(s, Import):
                    for module_name, alias in _import_names_from_stmt(s):
                        if (module_name in getattr(self, "_sibling_module_inits", ())
                                or self._resolve_pcc_native_extension_path(module_name) is not None):
                            name = alias or module_name.split(".", 1)[0]
                            slot = self._native_extension_module_global(name)
                            self._native_extension_modules()[name] = slot
                elif isinstance(s, ImportFrom):
                    module_name = self._resolve_relative_import(s)
                    scaffold = (
                        self._is_extern_scaffold_import_module(module_name)
                        or module_name == "pcc.unsafe"
                        or self._is_test_facade_import_module(module_name)
                        or module_name.split(".", 1)[0] in ("__future__", "typing", "abc", "click")
                    )
                    if not scaffold and (
                        module_name in getattr(self, "_sibling_module_inits", ())
                        or self._resolve_pcc_native_extension_path(module_name) is not None
                    ):
                        exports = (self._native_module_exports or {}).get(module_name, {})
                        for imported, alias in self._filter_runtime_import_from_names(s):
                            if imported == "*" or exports.get(imported, {}).get("kind") == "typing_metadata":
                                continue
                            if module_name == "pcc" and imported in ("valueclass", "i64", "u64"):
                                continue
                            name = alias or imported
                            slot = self._native_extension_module_global(name)
                            self._native_extension_modules()[name] = slot
                if isinstance(s, Assign):
                    for target in s.targets:
                        _maybe_declare_native_module_attr_store(self, target)
                elif isinstance(s, AugAssign):
                    _maybe_declare_native_module_attr_store(self, s.target)
                if fields is None:
                    fields = _dataclass_field_names(s)
                for slot in fields:
                    if slot in ("span",):
                        continue
                    v = _dataclass_field_value(s, slot, None)
                    if isinstance(v, tuple):
                        for it in v:
                            child_fields = _dataclass_field_names(it)
                            if child_fields:
                                pending.append((it, child_fields))
                    elif v is not None:
                        child_fields = _dataclass_field_names(v)
                        if child_fields:
                            pending.append((v, child_fields))

    def _collect_explicit_global_names(
        self,
        stmts: tuple[Stmt, ...],
    ) -> set[str]:
        """Return names declared ``global`` in ``stmts``.

        Nested blocks participate in the same function scope; nested
        ``def`` / ``class`` bodies do not.
        """
        from pcc.frontends.python.py_ast import ClassDef as _ClassDef, FuncDef as _FuncDef, Global as _Global

        names: set[str] = set()

        pending = [stmts]
        while pending:
            items = pending.pop()
            for s in items:
                if isinstance(s, _Global):
                    names.update(s.names)
                    continue
                if isinstance(s, (_FuncDef, _ClassDef)):
                    continue
                if isinstance(s, If):
                    pending.append(s.else_body)
                    pending.append(s.body)
                    continue
                if isinstance(s, While):
                    pending.append(s.else_body)
                    pending.append(s.body)
                    continue
                if isinstance(s, For):
                    pending.append(s.else_body)
                    pending.append(s.body)
                    continue
                if isinstance(s, With):
                    pending.append(s.body)
                    continue
                if isinstance(s, Try):
                    pending.append(s.finally_body)
                    pending.append(s.else_body)
                    for h in s.handlers:
                        pending.append(h.body)
                    pending.append(s.body)
        return names

    def _ensure_module_global_name(
        self,
        name: str,
        target_ty: Type,
    ) -> tuple[ir.GlobalVariable, Type]:
        """Return the storage slot for a module-global name."""
        existing = self._module_globals.get(name)
        if existing is not None:
            return existing
        if not (
            self._is_scalar(target_ty)
            or self._is_object(target_ty)
            or self._is_valueclass_payload_type(target_ty)
        ):
            raise NotImplementedError(
                f"Layer 1/2 cannot allocate module global {name!r} "
                f"of type {type(target_ty).__name__}"
            )
        if isinstance(target_ty, IntType) and (
            self._should_box_python_ints()
            or name in self._boxed_int_module_global_names
        ):
            ir_ty = _CSTR
        else:
            ir_ty = self._storage_ir_type(target_ty)
        gv = ir.GlobalVariable(
            self.module,
            ir_ty,
            name=self._module_global_symbol_name(
                self.ast_module.name or self.module.name or "mod",
                name,
            ),
        )
        gv.initializer = ir.Constant(ir_ty, _zero_initializer_for(ir_ty))
        self._module_globals[name] = (gv, target_ty)
        init_flag = ir.GlobalVariable(
            self.module,
            _I1,
            name=self._module_global_symbol_name(
                self.ast_module.name or self.module.name or "mod",
                name + ".initialized",
            ),
        )
        init_flag.initializer = ir.Constant(_I1, 0)
        self._module_global_init_flags[name] = init_flag
        return self._module_globals[name]

    def _emit_module_global_bound_check(self, name: str, expr) -> None:
        """Raise ``NameError`` when an unbound module global is read.

        Without this the slot cleared by ``del`` reads back as null, which is
        neither the stale value nor the error Python promises.
        """
        flag = getattr(self, "_module_global_init_flags", {}).get(name)
        if flag is None:
            return
        bound = self.builder.load(flag, name=self._fresh(f"{name}.bound"))
        raise_bb = self.current_function.append_basic_block(
            name=self._fresh(f"{name}.unbound")
        )
        ok_bb = self.current_function.append_basic_block(
            name=self._fresh(f"{name}.bound.ok")
        )
        self.builder.cbranch(bound, ok_bb, raise_bb)

        self.builder.position_at_end(raise_bb)
        message = f"name '{name}' is not defined"
        msg_gv, _ = self._cstr_literal(message)
        exc = self.builder.call(
            self.runtime["py_exc_new"],
            [ir.Constant(_I64, 10), self._ptr_to_cstr(msg_gv)],
            name=self._fresh(f"{name}.nameerror"),
        )
        self.builder.call(self.runtime["py_raise"], [exc])
        # py_raise increfs for the TLS slot; this caller created the exception
        # and still owns its own reference.
        self._gc_release(exc)
        self._emit_post_call_err_check(getattr(expr, "span", None))
        self.builder.branch(ok_bb)

        self.builder.position_at_end(ok_bb)

    def _collect_module_del_targets(self, stmt) -> None:
        """Record every module-level name used as a ``del`` target.

        The existing read-side check set also includes executing imports that
        can fail or remain unexecuted. Globals outside that set keep plain loads.
        """
        from pcc.frontends.python.py_ast import Delete as _Delete, Name as _Name

        targets = getattr(self, "_module_del_target_names", None)
        if targets is None:
            targets = set()
            self._module_del_target_names = targets
        stack = [stmt]
        while stack:
            node = stack.pop()
            if isinstance(node, _Delete):
                for target in node.targets:
                    if isinstance(target, _Name):
                        targets.add(target.ident)
                continue
            for attr in ("body", "orelse", "finalbody", "handlers", "items"):
                child = getattr(node, attr, None)
                # Bodies are tuples on these AST nodes, not lists; matching
                # only ``list`` silently skipped every function body.
                if isinstance(child, (list, tuple)):
                    stack.extend(x for x in child if hasattr(x, "__dict__"))

    def _module_global_needs_bound_check(self, name: str) -> bool:
        return name in getattr(self, "_module_del_target_names", set())

    def _mark_module_global_initialized(self, gv: ir.GlobalVariable) -> None:
        for name, (candidate, _declared_ty) in self._module_globals.items():
            if candidate is not gv:
                continue
            flag = self._module_global_init_flags.get(name)
            if flag is not None:
                self.builder.store(ir.Constant(_I1, 1), flag)
            return

    def _prescan_function_module_globals(self, fd: FuncDef) -> None:
        """Seed module-global storage for names assigned under
        ``global`` inside ``fd`` so sibling functions can resolve them.
        """
        global_names = self._collect_explicit_global_names(fd.body)
        if not global_names:
            return
        from pcc.frontends.python.py_ast import ClassDef as _ClassDef, FuncDef as _FuncDef

        pending = [fd.body]
        while pending:
            items = pending.pop()
            for s in items:
                if isinstance(s, (_FuncDef, _ClassDef)):
                    continue
                if isinstance(s, Assign):
                    target_ty = s.annotation if s.annotation is not None else s.value.ty
                    for t in s.targets:
                        if isinstance(t, Name) and t.ident in global_names:
                            if (
                                isinstance(target_ty, IntType)
                                and self._int_expr_needs_exact_object_boundary(
                                    s.value
                                )
                            ):
                                self._boxed_int_module_global_names.add(t.ident)
                            self._ensure_module_global_name(
                                t.ident,
                                target_ty,
                            )
                    continue
                if isinstance(s, AugAssign):
                    if isinstance(s.target, Name) and s.target.ident in global_names:
                        self._ensure_module_global_name(
                            s.target.ident,
                            s.target.ty,
                        )
                    continue
                if isinstance(s, Import):
                    for mod_name, as_name in s.names:
                        bound = as_name or mod_name.split(".", 1)[0]
                        if bound in global_names:
                            if (
                                mod_name in getattr(self, "_sibling_module_inits", ())
                                or mod_name in (self._native_module_exports or {})
                                or self._resolve_pcc_native_extension_path(mod_name) is not None
                            ):
                                gv = self._native_extension_module_global(bound)
                                self._native_extension_modules()[bound] = gv
                                continue
                            gv = self._cpy_module_global(bound)
                            self._cpy_modules()[bound] = gv
                    continue
                if isinstance(s, ImportFrom):
                    if _predeclare_owned_builtin_import_globals(self, s, global_names):
                        continue
                    for imported_name, as_name in s.names:
                        if self._resolve_relative_import(s) == "sys" and imported_name == "modules":
                            continue
                        if imported_name == "*":
                            continue
                        bound = as_name or imported_name
                        if bound in global_names:
                            native_table = self._native_module_exports
                            resolved = self._resolve_relative_import(s)
                            if (
                                resolved in (native_table or {})
                                or resolved in getattr(self, "_sibling_module_inits", ())
                                or self._resolve_pcc_native_extension_path(resolved) is not None
                            ):
                                # The executing import binds a pcc object into
                                # this global through the ordinary assignment
                                # owner. A CPython alias here would misclassify
                                # later loads even before this function runs.
                                gv = self._native_extension_module_global(bound)
                                self._native_extension_modules()[bound] = gv
                                continue
                            gv = self._cpy_module_global(bound)
                            self._cpy_modules()[bound] = gv
                    continue
                if isinstance(s, If):
                    pending.append(s.else_body)
                    pending.append(s.body)
                    continue
                if isinstance(s, While):
                    pending.append(s.else_body)
                    pending.append(s.body)
                    continue
                if isinstance(s, For):
                    pending.append(s.else_body)
                    pending.append(s.body)
                    continue
                if isinstance(s, With):
                    pending.append(s.body)
                    continue
                if isinstance(s, Try):
                    pending.append(s.finally_body)
                    pending.append(s.else_body)
                    for h in s.handlers:
                        pending.append(h.body)
                    pending.append(s.body)

    def _declare_module_globals_for(self, stmt: Assign) -> None:
        """Allocate globals for every name bound by a module assignment.

        Tuple/list destructuring binds each leaf at module scope just like a
        simple assignment.  Declaring only the aggregate target left those
        leaves as function-local allocas during module initialisation, so a
        compiled sibling module could not import them through the module
        namespace proxy.
        """
        pending_targets = list(stmt.targets)
        while pending_targets:
            t = pending_targets.pop()
            if isinstance(t, (TupleExpr, ListExpr)):
                pending_targets.extend(reversed(t.elems))
                continue
            if not isinstance(t, Name):
                continue
            if not stmt.has_value:
                # Reserve a lookup slot, without making an annotation into
                # an initialized module binding or replacing an earlier one.
                self._ensure_module_global_name(t.ident, t.ty)
                if getattr(self, "_module_del_target_names", None) is None:
                    self._module_del_target_names = set()
                self._module_del_target_names.add(t.ident)
                continue
            target_ty = (
                assignment_storage_annotation(stmt.annotation, stmt.value, stmt.has_value)
                if stmt.annotation is not None and len(stmt.targets) == 1
                else t.ty
            )
            if not (
                self._is_scalar(target_ty)
                or self._is_object(target_ty)
                or self._is_valueclass_payload_type(target_ty)
            ):
                continue
            if (
                isinstance(target_ty, IntType)
                and self._int_expr_needs_exact_object_boundary(stmt.value)
            ):
                self._boxed_int_module_global_names.add(t.ident)
            gv, _declared_ty = self._ensure_module_global_name(t.ident, target_ty)
            if (
                self._python_library
                and self._module_has_c_abi_export
                and isinstance(gv.value_type, ir.IntType)
            ):
                found, value = _scalar_literal_initializer(stmt.value)
                if not found:
                    value = 0
                gv.initializer = ir.Constant(gv.value_type, value)
            if len(stmt.targets) != 1 or not isinstance(stmt.targets[0], Name):
                continue
            threading_kind = self._threading_constructor_kind_for_expr(stmt.value)
            if threading_kind is not None:
                self._threading_env_flags[t.ident] = threading_kind
            else:
                self._threading_env_flags.pop(t.ident, None)
            threading_elem_kind = self._threading_list_elem_kind_for_type(
                target_ty
            ) or self._threading_list_elem_kind_for_expr(stmt.value)
            if threading_elem_kind is not None:
                self._threading_list_elem_flags[t.ident] = threading_elem_kind
            else:
                self._threading_list_elem_flags.pop(t.ident, None)
            weak_dict_kind = self._weak_dict_constructor_kind_for_expr(stmt.value)
            if weak_dict_kind is not None:
                self._weak_dict_env_flags[t.ident] = weak_dict_kind
            else:
                self._weak_dict_env_flags.pop(t.ident, None)
            weakref_kind = self._weakref_constructor_kind_for_expr(stmt.value)
            if weakref_kind is not None:
                self._weakref_env_flags[t.ident] = True
            else:
                self._weakref_env_flags.pop(t.ident, None)

    def _module_global_needs_teardown(
        self,
        gv: ir.GlobalVariable,
        declared_ty: Type,
    ) -> bool:
        if self._is_object(declared_ty):
            return True
        value_ty = getattr(gv, "value_type", None)
        return value_ty is not None and self._ir_type_matches(value_ty, _CSTR)

    def _store_module_global_root_value(
        self,
        gv: ir.GlobalVariable,
        value: ir.Value,
        *,
        declared_ty: Type | None = None,
        value_is_owned: bool = False,
        is_cpy_value: bool = False,
        raw_pointer: bool = False,
    ) -> None:
        if (
            not is_cpy_value
            and declared_ty is not None
            and self._is_valueclass_payload_type(declared_ty)
        ):
            self._copy_valueclass_payload(gv, value, declared_ty, module_destination=True)
            self._mark_module_global_initialized(gv)
            return
        # Raw pcc.unsafe/extern pointers (stack_alloc/calloc/ptr_add/...) are NOT
        # GC-managed PyObjects; pinning/rooting them writes PY_FLAG_GC_PINNED
        # into the pointed-to buffer (obj+12 flags word) and corrupts it.
        if is_cpy_value or raw_pointer or not isinstance(value.type, ir.PointerType):
            self.builder.store(value, gv)
            self._mark_module_global_initialized(gv)
            return
        if declared_ty is not None and not isinstance(gv.value_type, ir.PointerType):
            # A scalar module cell (an i64 global of a raw-int module) can
            # receive a boxed result, e.g. ``global n; n += 1`` whose add took
            # the tagged/bignum object path. Store its native value; rooting
            # the pointer would put object bits in the scalar cell.
            self._gc_pin(value)
            native = marshal.marshal_from_object(
                self.builder, self.module, self.runtime, value, declared_ty,
            )
            # An int beyond the cell's lane raises OverflowError here.
            self._emit_post_call_err_check()
            self._gc_unpin(value)
            if not self._ir_type_matches(native.type, gv.value_type):
                raise L1CodegenError(
                    "module global " + gv.name + " stores " + str(gv.value_type)
                    + " but its boxed value unboxes to " + str(native.type)
                )
            self.builder.store(native, gv)
            self._mark_module_global_initialized(gv)
            if value_is_owned:
                self._gc_release(value, self._release_context_label("module_store_tmp"))
            return
        old_value = self.builder.load(
            gv,
            name=self._fresh("mod.global.old"),
        )
        self._gc_unpin(old_value)
        self._gc_pin(value)
        self.builder.call(
            self.runtime["pcc_gc_store_root"],
            [
                self._as_gc_ptr(gv, name=self._fresh("mod.global.slot")),
                value,
            ],
        )
        self._mark_module_global_initialized(gv)
        if value_is_owned:
            self._gc_release(value, self._release_context_label("module_store_tmp"))

    def _module_global_valueclass_payload_field_slot(
        self,
        gv: ir.GlobalVariable,
        field_path: tuple[int, ...],
        *,
        name: str,
    ) -> ir.Value:
        indices = [ir.Constant(_I32, 0)]
        for idx in field_path:
            indices.append(ir.Constant(_I32, idx))
        return self.builder.gep(
            gv,
            indices,
            inbounds=True,
            name=self._fresh(name),
        )

    def _clear_module_global_valueclass_payload_roots(
        self,
        gv: ir.GlobalVariable,
        declared_ty: Type,
    ) -> None:
        for path in self._valueclass_payload_pointer_field_paths(declared_ty):
            field_slot = self._module_global_valueclass_payload_field_slot(
                gv,
                path,
                name="mod.global.value.clear",
            )
            self.builder.call(
                self.runtime["pcc_gc_store_root"],
                [
                    self._as_gc_ptr(
                        field_slot,
                        name=self._fresh("mod.global.value.clear.slot"),
                    ),
                    ir.Constant(_CSTR, None),
                ],
            )

    def _refresh_module_global_valueclass_payload_roots(
        self,
        gv: ir.GlobalVariable,
        declared_ty: Type,
    ) -> None:
        for path in self._valueclass_payload_pointer_field_paths(declared_ty):
            field_slot = self._module_global_valueclass_payload_field_slot(
                gv,
                path,
                name="mod.global.value.refresh",
            )
            value = self.builder.load(
                field_slot,
                name=self._fresh("mod.global.value.refresh.value"),
            )
            self.builder.call(
                self.runtime["pcc_gc_store_root"],
                [
                    self._as_gc_ptr(
                        field_slot,
                        name=self._fresh("mod.global.value.refresh.slot"),
                    ),
                    value,
                ],
            )

    def _emit_module_global_root_enters(self) -> None:
        frame_map = self._gc_one_slot_frame_map()
        for _name, item in self._module_globals.items():
            gv, declared_ty = item
            if self._is_valueclass_payload_type(declared_ty):
                for path in self._valueclass_payload_pointer_field_paths(
                    declared_ty,
                ):
                    field_slot = self._module_global_valueclass_payload_field_slot(
                        gv,
                        path,
                        name=f"mod.global.value.root.{_name}",
                    )
                    self._emit_current_gc_frame_enter(frame_map, field_slot)
                continue
            if not self._module_global_needs_teardown(gv, declared_ty):
                continue
            self._emit_current_gc_frame_enter(frame_map, gv)
        for gv in getattr(self, "_native_module_attr_globals", {}).values():
            self._emit_current_gc_frame_enter(frame_map, gv)

    def _emit_class_global_root_enters(self) -> None:
        frame_map = self._gc_one_slot_frame_map()
        entered_globals: set[str] = set()
        for info in self.class_lowering.classes.values():
            global_name = str(info.global_var.name)
            if global_name not in entered_globals:
                self._emit_current_gc_frame_enter(frame_map, info.global_var)
                entered_globals.add(global_name)
            cell_var = info.class_cell_var
            if cell_var is not None and str(cell_var.name) not in entered_globals:
                self._emit_current_gc_frame_enter(frame_map, cell_var)
                entered_globals.add(str(cell_var.name))
            for gv, _attr_ty in info.class_attrs.values():
                attr_global_name = str(gv.name)
                if attr_global_name in entered_globals:
                    continue
                self._emit_current_gc_frame_enter(frame_map, gv)
                entered_globals.add(attr_global_name)

    def _emit_module_root_enters(self) -> None:
        self._emit_module_global_root_enters()
        self._emit_class_global_root_enters()
