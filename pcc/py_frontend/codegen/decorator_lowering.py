"""Decorator helper lowering for Layer-1 Python codegen."""
from __future__ import annotations

from ..py_ast import Assign, Attr, Call, Name, StrLit, TupleExpr


_NOOP_DECORATOR_QUALIFIED = frozenset(
    {
        "click.command",
        "click.option",
        "click.argument",
        "click.pass_context",
        "click.group",
        "click.pass_obj",
        "functools.wraps",
        "functools.lru_cache",
        "functools.cache",
        "dataclasses.dataclass",
        "abc.abstractmethod",
        "contextlib.contextmanager",
        "pytest.fixture",
        "pytest.mark.parametrize",
        "pytest.mark.integration",
        "pytest.mark.skip",
        "pytest.mark.skipif",
        "pcc.test_runner.fixture",
        "pcc.test_runner.parametrize",
    }
)

_NOOP_DECORATOR_BARE = frozenset(
    {
        "wraps",
        "lru_cache",
        "cache",
        "dataclass",
        "abstractmethod",
        "contextmanager",
        "fixture",
        "parametrize",
        "_recursive_guard",
        "recursive_guard",
    }
)


class DecoratorLoweringMixin:
    def _decorator_root_name(self, dec) -> str | None:
        if isinstance(dec, Name):
            return dec.ident
        if isinstance(dec, Attr):
            try:
                cur = dec.obj
            except AttributeError:
                return None
            while isinstance(cur, Attr):
                try:
                    cur = cur.obj
                except AttributeError:
                    return None
            if isinstance(cur, Name):
                return cur.ident
            return None
        if isinstance(dec, Call):
            try:
                func = dec.func
            except AttributeError:
                return None
            return self._decorator_root_name(func)
        return None

    def _decorator_c_abi_export_symbol(self, dec) -> str | None:
        """Return the literal symbol from a C-ABI export decorator."""
        if not isinstance(dec, Call):
            return None
        try:
            func = dec.func
            args = dec.args
        except AttributeError:
            return None
        qn = self._decorator_qualname(func)
        if qn not in (
            "c_abi_export",
            "pcc.extern.c_abi_export",
            "extern.c_abi_export",
            "c_abi_variadic_export",
            "pcc.extern.c_abi_variadic_export",
            "extern.c_abi_variadic_export",
            "c_abi_typed_export",
            "pcc.extern.c_abi_typed_export",
            "extern.c_abi_typed_export",
        ):
            return None
        if qn in (
            "c_abi_typed_export",
            "pcc.extern.c_abi_typed_export",
            "extern.c_abi_typed_export",
        ):
            if len(args) != 3:
                return None
        elif len(args) != 1:
            return None
        arg = args[0]
        if not isinstance(arg, StrLit):
            return None
        try:
            return arg.value
        except AttributeError:
            return None

    def _decorator_c_abi_typed_signature(self, dec):
        """Return ``(result, args)`` from a typed export decorator."""
        if not isinstance(dec, Call):
            return None
        try:
            func = dec.func
            args = dec.args
        except AttributeError:
            return None
        qn = self._decorator_qualname(func)
        if qn not in (
            "c_abi_typed_export",
            "pcc.extern.c_abi_typed_export",
            "extern.c_abi_typed_export",
        ) or len(args) != 3:
            return None
        result_expr = args[1]
        arg_exprs = args[2]
        if not isinstance(result_expr, StrLit) or not isinstance(
            arg_exprs, TupleExpr
        ):
            return None
        names: list[str] = []
        for expr in arg_exprs.elems:
            if not isinstance(expr, StrLit):
                return None
            names.append(expr.value)
        return (result_expr.value, tuple(names))

    def _decorator_is_c_abi_variadic_export(self, dec) -> bool:
        if not isinstance(dec, Call):
            return False
        try:
            func = dec.func
            args = dec.args
        except AttributeError:
            return False
        qn = self._decorator_qualname(func)
        return (
            qn
            in (
                "c_abi_variadic_export",
                "pcc.extern.c_abi_variadic_export",
                "extern.c_abi_variadic_export",
            )
            and len(args) == 1
            and isinstance(args[0], StrLit)
        )

    def _decorator_qualname(self, dec):
        """Return a dotted identifier for a decorator expression or
        ``None`` if the decorator isn't a simple Name / Attr chain."""
        # ``@foo`` — bare Name
        if isinstance(dec, Name):
            return dec.ident
        # ``@foo.bar`` or deeper
        if isinstance(dec, Attr):
            parts: list[str] = [dec.name]
            try:
                cur = dec.obj
            except AttributeError:
                return None
            while isinstance(cur, Attr):
                parts.append(cur.name)
                try:
                    cur = cur.obj
                except AttributeError:
                    return None
            if isinstance(cur, Name):
                parts.append(cur.ident)
                return self._join_reversed_strs(parts)
            return None
        # ``@foo(args)`` or ``@foo.bar(args)`` — Call wrapping a name chain
        if isinstance(dec, Call):
            try:
                func = dec.func
            except AttributeError:
                return None
            return self._decorator_qualname(func)
        return None

    def _decorator_is_noop_whitelist(self, dec) -> bool:
        if self._native_builtin_value_kind_for_expr(dec) == "pcc.virtual_thread.continuation_factory":
            return True
        qn = self._decorator_qualname(dec)
        if qn is None:
            return False
        if qn in _NOOP_DECORATOR_QUALIFIED:
            return True
        if qn == "errstate" or qn.endswith(".errstate"):
            return True
        if "." not in qn and qn in _NOOP_DECORATOR_BARE:
            return True
        if self._decorator_is_runtime_partial_factory(dec):
            return False
        if self._decorator_is_external_metadata_factory(dec):
            return True
        return False

    def _decorator_is_runtime_partial_factory(self, dec) -> bool:
        """Whether ``dec`` calls a module-global ``functools.partial`` value.

        Package dispatch layers commonly bind a decorator factory with
        ``factory = functools.partial(...)`` and then use ``@factory(...)``.
        Unlike imported metadata-only factories, that call has Python-visible
        semantics: it can replace the decorated function.  Recognize the
        generic assignment shape so call lowering executes both factory calls.
        """
        if not isinstance(dec, Call):
            return False
        root = self._decorator_root_name(dec.func)
        if root is None:
            return False
        for stmt in self.ast_module.body:
            if not isinstance(stmt, Assign) or len(stmt.targets) != 1:
                continue
            target = stmt.targets[0]
            if not isinstance(target, Name) or target.ident != root:
                continue
            value = stmt.value
            if not isinstance(value, Call):
                return False
            partial_func = value.func
            if isinstance(partial_func, Attr):
                if partial_func.name != "partial":
                    return False
                partial_obj = partial_func.obj
                return bool(
                    isinstance(partial_obj, Name)
                    and self._native_builtin_module_for_name(partial_obj.ident)
                    == "functools"
                )
            if isinstance(partial_func, Name):
                return bool(
                    getattr(self, "_native_builtin_value_aliases", {}).get(
                        partial_func.ident
                    )
                    == "functools.partial"
                )
            return False
        return False

    def _decorator_is_external_metadata_factory(self, dec) -> bool:
        """Treat imported decorator factories as compile-time metadata.

        A common package pattern is ``@imported_factory(...metadata...)`` where
        the wrapper only affects CPython dispatch/reflection. pcc cannot execute
        arbitrary import-time decorator factories during native declaration, but
        it can still compile the underlying function body. Keep this limited to
        decorators whose root name came from another module; a same-module
        decorator remains semantic user code and is not ignored.

        Two shapes qualify: call-shaped ``@imported_factory(...)`` (any
        imported-name table), and bare ``@imported_name``.  A bare native
        sibling qualifies only when closed-world export analysis proved the
        function returns its first argument unchanged; otherwise it remains a
        semantic decorator.  CPython-imported bare decorators retain the
        existing metadata treatment.
        """
        if isinstance(dec, Name):
            ident = dec.ident
            if ident in getattr(self, "functions", {}):
                return bool(
                    getattr(self, "_cross_module_identity_decorators", {}).get(
                        ident,
                        False,
                    )
                )
            return ident in getattr(self, "_cpy_module_env", {})
        if not isinstance(dec, Call):
            return False
        root = self._decorator_root_name(dec.func)
        if root is None:
            return False
        if root in getattr(self, "_cross_module_func_defs", {}):
            return True
        if (
            root in getattr(self, "_module_globals", {})
            and root not in getattr(self, "functions", {})
        ):
            return True
        if root in getattr(self, "_cpy_module_env", {}):
            return True
        if root in getattr(self, "_native_module_aliases", {}):
            return True
        if root in getattr(self, "_native_builtin_module_aliases", {}):
            return True
        if root in getattr(self, "_native_builtin_value_aliases", {}):
            return True
        return False

    def _decorator_repr(self, dec) -> str:
        qn = self._decorator_qualname(dec)
        return qn if qn is not None else type(dec).__name__

    # -- For loops -----------------------------------------------------
