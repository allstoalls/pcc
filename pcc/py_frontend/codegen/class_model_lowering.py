"""Class hint, MRO, and external class helper lowering for L1CodeGen."""

from __future__ import annotations

from typing import Optional

from pcc.llvm_capi.compat import ir

from ..py_ast import (
    Attr,
    Call,
    ClassType,
    DynType,
    Expr,
    FuncDef,
    ImportFrom,
    ListExpr,
    Name,
    Return,
    StrLit,
    TupleExpr,
    TupleType,
)
from . import marshal

_I8 = ir.IntType(8)
_CSTR = _I8.as_pointer()


def _class_type_like(ty) -> Optional[ClassType]:
    if isinstance(ty, ClassType):
        return ty
    kind = type(ty).__name__
    if kind != "ClassType" and kind != "ValueClassType":
        return None
    name = getattr(ty, "name", None)
    if not isinstance(name, str):
        return None
    module = getattr(ty, "module", "") or ""
    fields = getattr(ty, "fields", ()) or ()
    bases = getattr(ty, "bases", ()) or ()
    properties = getattr(ty, "properties", ()) or ()
    valueclass = bool(getattr(ty, "valueclass", False))
    return ClassType(
        name=name,
        module=module,
        fields=tuple(fields),
        bases=tuple(bases),
        properties=tuple(properties),
        valueclass=valueclass,
    )


class ClassModelLoweringMixin:
    def _self_receiver_class_name(self) -> Optional[str]:
        # Prefer the inferred ``self`` slot type over ``current_class``.
        # ``current_class`` is the lexical class whose method body is being
        # lowered; for mixin/inherited methods the runtime receiver can be a
        # composed subclass such as ``L1CodeGen``.  Using the lexical class
        # first mis-lowers ``self.attr`` against the mixin's class layout and
        # broke the stage1 bootstrap smoke compile with ``AttributeError:
        # module``.  ``current_class`` is only a fallback when inference did
        # not provide a receiver type.
        slot = self.env.get("self")
        slot_ty = _class_type_like(slot[2]) if slot is not None else None
        if slot_ty is not None:
            cache_key = (slot_ty.module, slot_ty.name)
            cached = getattr(self, "_self_receiver_class_name_cache", None)
            if cached is not None and cached[0] == cache_key:
                return cached[1]
            registered = self._ensure_class_type_registered(slot_ty)
            if registered is not None:
                self._self_receiver_class_name_cache = (cache_key, registered)
                return registered
        current_class = self.current_class
        if current_class is not None:
            return current_class.name
        return None

    def _native_class_export_candidates(
        self,
        native_table,
        class_name: str,
        module_name: str,
    ):
        if module_name:
            exports = native_table.get(module_name)
            if exports is None:
                return ()
            info = exports.get(class_name)
            if isinstance(info, dict) and info.get("kind") == "class":
                return ((module_name, info),)
            return ()

        cached_source = getattr(self, "_native_class_export_index_source", None)
        index = getattr(self, "_native_class_export_index", None)
        if index is None or cached_source is not native_table:
            index = {}
            for export_module_name, exports in native_table.items():
                for export_name, info in exports.items():
                    if not isinstance(info, dict) or info.get("kind") != "class":
                        continue
                    entry = (export_module_name, info)
                    entries = index.get(export_name)
                    if entries is None:
                        index[export_name] = [entry]
                    else:
                        entries.append(entry)
            self._native_class_export_index_source = native_table
            self._native_class_export_index = index

        entries = index.get(class_name)
        if entries is None:
            return ()
        return tuple(entries)

    def _class_hint_for_expr(self, expr: Expr) -> Optional[str]:
        if isinstance(expr, Name):
            if expr.ident == "self" and self.current_class is not None:
                return self.current_class.name
            expr_ty = _class_type_like(expr.ty)
            if expr_ty is not None:
                alias_name = self._resolve_class_alias(expr_ty.name)
                if (
                    alias_name != expr_ty.name
                    and alias_name in self.class_lowering.classes
                ):
                    return alias_name
                if expr_ty.name.startswith("_") and (
                    self.ast_module.name or ""
                ).startswith("pcc.py_frontend."):
                    py_ast_name = expr_ty.name[1:]
                    py_ast_info = self.class_lowering.classes.get(py_ast_name)
                    if (
                        py_ast_info is not None
                        and getattr(py_ast_info, "owning_module", None)
                        == "pcc.py_frontend.py_ast"
                    ):
                        return py_ast_name
                registered = self._ensure_class_type_registered(expr_ty)
                if registered is not None:
                    return registered
            hint = self.env_class_hint.get(expr.ident)
            if hint is not None:
                return hint
            slot = self.env.get(expr.ident)
            slot_ty = _class_type_like(slot[2]) if slot is not None else None
            if slot_ty is not None:
                return self._ensure_class_type_registered(slot_ty)
        expr_ty = _class_type_like(expr.ty)
        if expr_ty is not None:
            return self._ensure_class_type_registered(expr_ty)
        if isinstance(expr, Attr):
            # Handle chained native-module class references like
            # ``datetime.datetime`` and ``pkg.submod.Class``.
            export = self._native_module_expr_export_info(expr.obj, expr.name)
            if export is not None:
                mod_name, info = export
                if isinstance(info, dict) and info.get("kind") == "class":
                    registered = self._ensure_class_type_registered(
                        ClassType(
                            name=expr.name,
                            module=mod_name,
                            fields=(),
                            bases=(),
                        )
                    )
                    if registered is not None:
                        return registered
                    expected_global = (
                        ".class."
                        + mod_name.replace(".", "_").replace("-", "_")
                        + "."
                        + expr.name
                    )
                    for registered in self.class_lowering.classes.values():
                        if registered.global_var.name == expected_global:
                            return registered.name
                    return expr.name
        if not isinstance(expr, Call):
            return None
        if isinstance(expr.func, Name):
            name = expr.func.ident
            if name in self.class_lowering.classes:
                return name
            # A rebind such as ``cls = Decoder; cls()`` stores a class object
            # in a normal Python local.  Its call result is an instance of the
            # hinted class even when stage inference widens the Call to DynType.
            class_object_hint = getattr(self, "env_class_object_hint", {}).get(name)
            if class_object_hint is not None:
                return class_object_hint
            return None
        if isinstance(expr.func, Attr):
            # Check if this is a cross-module class instantiation.
            export = self._native_module_expr_export_info(
                expr.func.obj,
                expr.func.name,
            )
            if export is not None:
                mod_name, info = export
                if isinstance(info, dict) and info.get("kind") == "class":
                    registered = self._ensure_class_type_registered(
                        ClassType(
                            name=expr.func.name,
                            module=mod_name,
                            fields=(),
                            bases=(),
                        )
                    )
                    if registered is not None:
                        return registered
                    expected_global = (
                        ".class."
                        + mod_name.replace(".", "_").replace("-", "_")
                        + "."
                        + expr.func.name
                    )
                    for registered in self.class_lowering.classes.values():
                        if registered.global_var.name == expected_global:
                            return registered.name
                    return expr.func.name

        if not isinstance(expr.func, Attr):
            return None
        receiver_hint = self._class_hint_for_expr(expr.func.obj)
        if receiver_hint is None:
            return None
        info = self._resolve_method_mro(receiver_hint, expr.func.name)
        if info is None:
            return None
        fd = self.class_lowering._find_method_def(info.name, expr.func.name)
        if fd is None:
            return None
        ret_hint = self._class_hint_from_annotation(fd.return_ty)
        if ret_hint is not None:
            return ret_hint
        if self._method_returns_receiver(fd):
            return receiver_hint
        return None

    def _ensure_class_type_registered(self, ty: ClassType) -> Optional[str]:
        coerced_ty = _class_type_like(ty)
        if coerced_ty is None:
            return None
        ty = coerced_ty
        # Check if the class is already registered locally.
        # Use the local key if it exists and matches the module.
        local_info = self.class_lowering.classes.get(ty.name)
        if local_info is not None:
            # Verify module ownership if available.
            owning = getattr(local_info, "owning_module", None)
            if owning is None or ty.module is None or owning == ty.module:
                return ty.name

        # Check by qualified name to handle shadowed classes.
        if ty.module:
            qualified = f"{ty.module}.{ty.name}"
            if qualified in self.class_lowering.classes:
                return qualified

        cache_key = (ty.module, ty.name)
        if cache_key in self._class_type_export_cache:
            return self._class_type_export_cache[cache_key]
        native_table = self._native_module_exports
        if native_table is None:
            self._class_type_export_cache[cache_key] = None
            return None
        candidates = list(
            self._native_class_export_candidates(native_table, ty.name, ty.module)
        )
        if len(candidates) != 1:
            self._class_type_export_cache[cache_key] = None
            return None
        module_name, info = candidates[0]
        owning_module = info.get("owning_module", module_name)
        for base_name in info.get("base_names", ()):
            for base_module, base_info in self._native_class_export_candidates(
                native_table,
                base_name,
                "",
            ):
                base_owner = base_info.get("owning_module", base_module)
                base_ty = ClassType(
                    name=base_name, module=base_module, fields=(), bases=()
                )
                if base_owner != base_module:
                    base_ty = ClassType(
                        name=base_name, module=base_owner, fields=(), bases=()
                    )
                self._ensure_class_type_registered(base_ty)
                break
        class_info = self.class_lowering.declare_extern_class(
            owning_module=owning_module,
            class_name=info["class_name"],
            field_names=info["field_names"],
            methods=info["methods"],
            local_name=ty.name,
            field_types=info.get("field_types", ()),
        )
        local_info = class_info
        if local_info is not None:
            from ..py_ast import Name as _BaseName
            from ..py_ast import SourceSpan as _Span

            _stub_span = _Span(file="<extern>", line=0, col=0, end_line=0, end_col=0)
            local_info.bases_ast = tuple(
                _BaseName(span=_stub_span, ty=DynType(name="dyn"), ident=bn)
                for bn in info.get("base_names", ())
            )
        self._class_type_export_cache[cache_key] = class_info.name
        return class_info.name

    def _maybe_emit_class_lowering_extern_method(
        self,
        expr: Call,
    ) -> Optional[ir.Value]:
        attr = expr.func
        if not isinstance(attr, Attr):
            return None
        obj_ty = attr.obj.ty
        obj_cls_ty = _class_type_like(obj_ty)
        if not (
            obj_cls_ty is not None
            and obj_cls_ty.name == "ClassLowering"
            and attr.name == "declare_extern_class"
        ):
            return None
        ordered = self._ordered_declare_extern_class_args(expr)
        if ordered is None:
            return None
        recv = self._emit_expr(attr.obj)
        args_ir: list[ir.Value] = [recv]
        for arg in ordered:
            raw = self._emit_expr(arg)
            args_ir.append(
                marshal.marshal_to_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    raw,
                    arg.ty,
                )
            )
        sym = (
            "user_pcc_py_frontend_codegen_class_gen_"
            "ClassLowering_declare_extern_class"
        )
        existing = self.module.globals.get(sym)
        if isinstance(existing, ir.Function):
            fn = existing
        else:
            param_tys: list[ir.Type] = []
            i = 0
            while i < len(args_ir):
                param_tys.append(_CSTR)
                i += 1
            fnty = ir.FunctionType(
                _CSTR,
                param_tys,
                var_arg=False,
            )
            fn = ir.Function(self.module, fnty, name=sym)
            fn.linkage = "external"
        return self._call_user(
            fn,
            args_ir,
            self._fresh("ClassLowering.declare_extern_class.ret"),
        )

    def _ordered_declare_extern_class_args(
        self,
        expr: Call,
    ) -> Optional[list[Expr]]:
        names = (
            "owning_module",
            "class_name",
            "field_names",
            "methods",
            "local_name",
            "field_types",
        )
        out: list[Expr] = []
        i = 0
        while i < len(expr.args):
            if i >= len(names):
                return None
            out.append(expr.args[i])
            i += 1
        while i < len(names):
            found: Optional[Expr] = None
            for name, value in expr.kwargs:
                if name == names[i]:
                    found = value
                    break
            if found is None:
                if names[i] == "field_types":
                    found = TupleExpr(
                        span=expr.span,
                        ty=TupleType(name="tuple", elems=()),
                        elems=(),
                    )
                else:
                    return None
            out.append(found)
            i += 1
        return out

    def _list_elem_class_hint_for_expr(self, expr: Expr) -> Optional[str]:
        if not isinstance(expr, ListExpr) or not expr.elems:
            return None
        hint: Optional[str] = None
        for elem in expr.elems:
            elem_hint = self._class_hint_for_expr(elem)
            if elem_hint is None:
                return None
            if hint is None:
                hint = elem_hint
            elif hint != elem_hint:
                return None
        return hint

    def _class_hint_from_annotation(self, ann) -> Optional[str]:
        ann_cls = _class_type_like(ann)
        if ann_cls is not None:
            registered = self._ensure_class_type_registered(ann_cls)
            if registered is not None:
                return registered
            if ann_cls.name in self.class_lowering.classes:
                return ann_cls.name
            return None
        ann_name = getattr(ann, "name", None)
        if ann_name in self.class_lowering.classes:
            return ann_name
        if ann_name is not None:
            registered = self._ensure_class_type_registered(
                ClassType(
                    name=ann_name,
                    module="",
                    fields=(),
                    bases=(),
                )
            )
            if registered is not None:
                return registered
        if isinstance(ann, Name) and ann.ident in self.class_lowering.classes:
            return ann.ident
        if isinstance(ann, StrLit) and ann.value in self.class_lowering.classes:
            return ann.value
        return None

    def _method_returns_receiver(self, fd: FuncDef) -> bool:
        if not fd.args:
            return False
        receiver_name = fd.args[0].name or "self"
        saw_return = False
        for stmt in fd.body:
            if isinstance(stmt, Return):
                saw_return = True
                if not (
                    isinstance(stmt.value, Name) and stmt.value.ident == receiver_name
                ):
                    return False
        return saw_return

    def _resolve_super_method(self, info, method_name: str):
        """Walk the bases of ``info`` and return the first one that
        defines ``method_name``. Models a single-inheritance ``super()``
        call — the multi-base case needs full C3 linearisation which
        remains TODO in :class:`ClassLowering`.
        """
        for base_expr in info.bases_ast:
            if not isinstance(base_expr, Name) or base_expr.ident == "object":
                continue
            found = self._resolve_method_mro(base_expr.ident, method_name)
            if found is not None:
                return found
        return None

    def _resolve_property_setter_mro(self, class_name: str, prop_name: str):
        """Walk the MRO of ``class_name`` for a ``@<prop>.setter``."""
        visited: set[str] = set()
        queue = [class_name]
        while queue:
            cname = queue.pop(0)
            if cname in visited:
                continue
            visited.add(cname)
            info = self.class_lowering.classes.get(cname)
            if info is None:
                continue
            if prop_name in info.property_setters:
                return info
            for base_expr in info.bases_ast:
                if isinstance(base_expr, Name) and base_expr.ident != "object":
                    queue.append(base_expr.ident)
        return None

    def _resolve_property_deleter_mro(self, class_name: str, prop_name: str):
        """Walk the MRO of ``class_name`` for a ``@<prop>.deleter``."""
        visited: set[str] = set()
        queue = [class_name]
        while queue:
            cname = queue.pop(0)
            if cname in visited:
                continue
            visited.add(cname)
            info = self.class_lowering.classes.get(cname)
            if info is None:
                continue
            if prop_name in info.property_deleters:
                return info
            for base_expr in info.bases_ast:
                if isinstance(base_expr, Name) and base_expr.ident != "object":
                    queue.append(base_expr.ident)
        return None

    def _resolve_property_mro(self, class_name: str, prop_name: str):
        """Walk the MRO of ``class_name`` for a ``@property`` ``prop_name``."""
        visited: set[str] = set()
        queue = [class_name]
        while queue:
            cname = queue.pop(0)
            if cname in visited:
                continue
            visited.add(cname)
            info = self.class_lowering.classes.get(cname)
            if info is None:
                continue
            if prop_name in info.properties:
                return info
            for base_expr in info.bases_ast:
                if isinstance(base_expr, Name) and base_expr.ident != "object":
                    queue.append(base_expr.ident)
        return None

    def _resolve_class_attr_mro(self, class_name: str, attr_name: str):
        visited: set[str] = set()
        queue = [class_name]
        while queue:
            cname = queue.pop(0)
            if cname in visited:
                continue
            visited.add(cname)
            info = self.class_lowering.classes.get(cname)
            if info is None:
                continue
            if attr_name in info.class_attrs:
                return info
            for base_expr in info.bases_ast:
                if isinstance(base_expr, Name) and base_expr.ident != "object":
                    queue.append(base_expr.ident)
        return None

    def _class_attr_descriptor_class(self, class_name: str, attr_name: str):
        owner_info = self._resolve_class_attr_mro(class_name, attr_name)
        if owner_info is None:
            return None
        value_expr = owner_info.class_attr_values.get(attr_name)
        if not (isinstance(value_expr, Call) and isinstance(value_expr.func, Name)):
            return None
        desc_name = value_expr.func.ident
        desc_info = self.class_lowering.classes.get(desc_name)
        if desc_info is None:
            return None
        return owner_info, desc_info

    def _import_from_module_for_local_name(self, local_name: str):
        """The module a ``from X import <local_name>`` bound, if any.

        Cross-module class lookup is keyed by bare class name, so without this
        the first module in the export table that happens to define the name
        wins.  ``pcc/ast/c_ast.py`` alone defines ``Enum``, ``If``, ``For``,
        ``Union``, ``Struct``, ``Return``, ``While``, ``ID``, ``Constant``,
        ``Cast``, ``Decl``, ``Label``, ``Case``, ``Default``, ``Break``,
        ``Continue``, ``Switch``, ``Typename`` and ``Assignment``; once the C
        frontend joined the self-host closure, ``class DiagnosticSeverity(str,
        Enum)`` in ``pcc/diagnostics.py`` resolved its base to the C AST's
        ``Enum`` and every ``DiagnosticSeverity(value)`` call was checked
        against ``__init__(self, name, values, coord=None)``.

        A name the module imported from a specific module can only be that
        module's, so the answer here is authoritative rather than a hint.
        """
        cache = getattr(self, "_import_from_module_cache", None)
        if cache is None:
            cache = {}
            body = getattr(self.ast_module, "body", ())
            i = 0
            while i < len(body):
                stmt = body[i]
                i += 1
                if not isinstance(stmt, ImportFrom):
                    continue
                module = stmt.module or ""
                if not module:
                    continue
                j = 0
                while j < len(stmt.names):
                    pair = stmt.names[j]
                    j += 1
                    bound = pair[1] or pair[0]
                    cache[bound] = module
            self._import_from_module_cache = cache
        return cache.get(local_name, None)

    def _resolve_method_mro(self, class_name: str, method_name: str):
        """Walk the declared bases of ``class_name`` looking for the
        first class that defines ``method_name``. Supports cross-module
        MRO resolution by consulting the native exports registry."""
        cache_key = (class_name, method_name)
        cache = getattr(self, "_method_mro_cache", None)
        if cache is not None:
            if cache_key in cache:
                return cache[cache_key]
        visited: set[str] = set()
        queue = [class_name]
        native_table = self._native_module_exports or {}

        def remember(info):
            cache = getattr(self, "_method_mro_cache", None)
            if cache is None:
                cache = {}
                self._method_mro_cache = cache
            cache[cache_key] = info
            return info

        def remember_missing():
            # Do not cache misses: a method may be absent only because not all
            # classes are registered yet (cross-module MRO), so a cached miss
            # could become stale.  Only positive results are memoized.
            return None

        while queue:
            cname = queue.pop(0)
            if cname in visited:
                continue
            visited.add(cname)

            # 1. Try local registry first.
            info = self.class_lowering.classes.get(cname)
            if info is not None:
                if method_name in info.methods or method_name in info.field_names:
                    return remember(info)
                for base_expr in info.bases_ast:
                    if isinstance(base_expr, Name) and base_expr.ident != "object":
                        # A base written as a bare name refers to THIS module's
                        # binding for it, so qualify it with the module it was
                        # imported from.  Bare-name lookup scans the whole
                        # export table and takes the first hit, and
                        # ``pcc/ast/c_ast.py`` alone defines ``Enum``, ``If``,
                        # ``For``, ``Union``, ``Struct``, ``Return``,
                        # ``While``, ``ID``, ``Constant``, ``Cast``, ``Decl``,
                        # ``Label``, ``Case``, ``Default``, ``Break``,
                        # ``Continue``, ``Switch``, ``Typename`` and
                        # ``Assignment``: once the C frontend joined the
                        # self-host closure, ``class DiagnosticSeverity(str,
                        # Enum)`` resolved its base to the C AST's ``Enum`` and
                        # every ``DiagnosticSeverity(value)`` was checked
                        # against ``__init__(self, name, values, coord=None)``.
                        #
                        # Only syntactic bases are qualified.  An inferred
                        # class hint carries a bare name with no import to
                        # attribute it to, so those keep the wide scan.
                        bound = self._import_from_module_for_local_name(
                            base_expr.ident
                        )
                        if bound is not None:
                            queue.append(bound + "." + base_expr.ident)
                        else:
                            queue.append(base_expr.ident)
                continue

            # 2. Try cross-module lookup if local fails.
            # Handle both short names and qualified names (mod.Class).
            for mod_name, mod_exports in native_table.items():
                # Check for direct match or mod.ClassName match.
                c_info = mod_exports.get(cname)
                if not c_info and "." in cname:
                    # Find last "." manually — str.rfind has no native
                    # closed-world dispatch; use a typed-int reverse scan.
                    idx = -1
                    i = len(cname) - 1
                    while i >= 0:
                        if cname[i] == ".":
                            idx = i
                            break
                        i -= 1
                    if idx >= 0:
                        prefix = cname[:idx]
                        short = cname[idx + 1 :]
                        if prefix == mod_name:
                            c_info = mod_exports.get(short)

                if isinstance(c_info, dict) and c_info.get("kind") == "class":
                    # We found the metadata. Does it define the method/field?
                    m_names = [m["name"] for m in c_info.get("methods", ())]
                    if method_name in m_names or method_name in c_info.get(
                        "field_names", ()
                    ):
                        # Synthetic ClassInfo for the remote class.
                        owning_module = c_info.get("owning_module", mod_name)
                        return remember(
                            self.class_lowering.declare_extern_class(
                                owning_module=owning_module,
                                class_name=c_info.get(
                                    "class_name",
                                    cname.split(".")[-1],
                                ),
                                field_names=c_info.get("field_names", ()),
                                methods=c_info.get("methods", ()),
                                local_name=cname,
                                field_types=c_info.get("field_types", ()),
                            )
                        )
                    # Not defined here, queue its bases for further MRO search.
                    for base_name in c_info.get("base_names", ()):
                        if base_name != "object":
                            queue.append(base_name)
                    break
        return remember_missing()

    _STR_METHOD_NATIVE = frozenset(
        {
            "upper",
            "lower",
            "strip",
            "lstrip",
            "rstrip",
            "split",
            "join",
            "replace",
            "find",
            "count",
            "encode",
            "startswith",
            "endswith",
            "splitlines",
            "isdigit",
            "isalpha",
            "isspace",
            "isalnum",
        }
    )
