"""Import statement lowering helpers for L1CodeGen."""

from __future__ import annotations

import os
import sys
from typing import Optional

from pcc.package_environment import package_site_roots
from pcc.llvm_capi.compat import ir

from ..py_ast import (
    Arg,
    Assign,
    Attr,
    AugAssign,
    BinOp,
    BoolExpr,
    BoolLit,
    BoolType,
    Break,
    Call,
    ClassDef,
    ClassType,
    Compare,
    Continue,
    Delete,
    DictExpr,
    DictType,
    DynType,
    ExceptHandler,
    Expr,
    ExprStmt,
    FloatLit,
    FloatType,
    For,
    FuncDef,
    FuncType,
    Global,
    If,
    IfExpr,
    Import,
    ImportFrom,
    IntLit,
    IntType,
    Lambda,
    ListExpr,
    ListType,
    Module,
    Name,
    Nonlocal,
    NoneLit,
    NoneType,
    Pass,
    Raise,
    Return,
    Slice,
    SourceSpan,
    Stmt,
    StrLit,
    StrType,
    Subscript,
    Try,
    TupleExpr,
    TupleType,
    Type,
    UnaryOp,
    ValueClassType,
    While,
    With,
)
from .layer1_support import (
    _import_from_level_or_zero,
    _import_from_module_or_empty,
)

_Name = Name


def _dataclass_field_value(obj, field_name: str, default=None):
    return getattr(obj, field_name, default)


def _dataclass_field_names(obj):
    if obj is None:
        return ()
    if (
        isinstance(obj, str)
        or isinstance(obj, int)
        or isinstance(obj, bool)
        or isinstance(obj, float)
        or isinstance(obj, bytes)
    ):
        return ()
    if isinstance(obj, SourceSpan):
        return ("file", "line", "col", "end_line", "end_col")
    if isinstance(obj, IntType) or isinstance(obj, FloatType):
        return ("name", "width")
    if isinstance(obj, BoolType):
        return ("name",)
    if isinstance(obj, NoneType):
        return ("name",)
    if isinstance(obj, StrType):
        return ("name",)
    if isinstance(obj, ListType):
        return ("name", "elem")
    if isinstance(obj, DictType):
        return ("name", "key", "value")
    if isinstance(obj, TupleType):
        return ("name", "elems")
    if isinstance(obj, FuncType):
        return ("name", "params", "ret")
    if isinstance(obj, ValueClassType):
        return (
            "name",
            "module",
            "fields",
            "bases",
            "properties",
            "valueclass",
            "flattened",
            "nullable_fields",
        )
    if isinstance(obj, ClassType):
        return ("name", "module", "fields", "bases", "properties", "valueclass")
    if isinstance(obj, DynType):
        return ("name",)
    if isinstance(obj, Type):
        return ("name",)
    if isinstance(obj, Expr):
        if isinstance(obj, NoneLit):
            return ("span", "ty")
        if (
            isinstance(obj, IntLit)
            or isinstance(obj, FloatLit)
            or isinstance(obj, BoolLit)
            or isinstance(obj, StrLit)
        ):
            return ("span", "ty", "value")
        if isinstance(obj, Name):
            return ("span", "ty", "ident")
        if isinstance(obj, BinOp):
            return ("span", "ty", "op", "lhs", "rhs")
        if isinstance(obj, UnaryOp):
            return ("span", "ty", "op", "operand")
        if isinstance(obj, Compare):
            return ("span", "ty", "op", "lhs", "rhs")
        if isinstance(obj, BoolExpr):
            return ("span", "ty", "op", "left", "right")
        if isinstance(obj, Call):
            return ("span", "ty", "func", "args", "kwargs")
        if isinstance(obj, Attr):
            return ("span", "ty", "obj", "name")
        if isinstance(obj, Subscript):
            return ("span", "ty", "obj", "idx")
        if isinstance(obj, Slice):
            return ("span", "ty", "lo", "hi", "step")
        if isinstance(obj, ListExpr):
            return ("span", "ty", "elems")
        if isinstance(obj, DictExpr):
            return ("span", "ty", "pairs")
        if isinstance(obj, TupleExpr):
            return ("span", "ty", "elems")
        if isinstance(obj, IfExpr):
            return ("span", "ty", "cond", "then_e", "else_e")
        if isinstance(obj, Lambda):
            return ("span", "ty", "params", "body")
    if isinstance(obj, Stmt):
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
        if isinstance(obj, Pass) or isinstance(obj, Break) or isinstance(obj, Continue):
            return ("span",)
        if isinstance(obj, Raise):
            return ("span", "exc", "cause")
        if isinstance(obj, Try):
            return ("span", "body", "handlers", "else_body", "finally_body")
        if isinstance(obj, With):
            return ("span", "items", "body")
        if isinstance(obj, Import):
            return ("span", "names")
        if isinstance(obj, ImportFrom):
            return ("span", "module", "names", "level")
        if isinstance(obj, Global):
            return ("span", "names")
        if isinstance(obj, Nonlocal):
            return ("span", "names")
        if isinstance(obj, Delete):
            return ("span", "targets")
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
            return (
                "span",
                "name",
                "bases",
                "keywords",
                "body",
                "decorators",
            )
    if isinstance(obj, Arg):
        return ("name", "annotation", "default", "kind", "has_default")
    if isinstance(obj, ExceptHandler):
        return ("exc_type", "name", "body", "span")
    if isinstance(obj, Module):
        return ("name", "body", "docstring")
    fields = getattr(obj, "__dataclass_fields__", None)
    if fields is not None:
        return fields.keys()
    return ()


class ImportLoweringMixin:
    _EXTERN_SCAFFOLD_MODULES = frozenset(
        {
            "pcc.extern",
            "pcc.llvm_capi",
            "pcc.llvm_capi.compat",
        }
    )
    # Modules pcc lowers natively for a plain ``import X``.  Kept as a named
    # constant so the dotted-form check below reads the same set.
    _NATIVE_BUILTIN_IMPORT_MODULES = frozenset(
        {
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
            "functools",
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
            "traceback",
        }
    )
    _IR_RUNTIME_COMPAT_MODULE = "pcc.llvm_capi.compat"
    # The module the ON-mode closure rewrite substitutes for compat: it holds
    # the real definitions behind every emitted ``user_pcc_llvm_capi_ir_*``
    # call, so its top-level init has to run before the first scaffold call.
    _IR_RUNTIME_PROVIDER_MODULE = "pcc.llvm_capi.ir"
    _IR_RUNTIME_COMPAT_HELPERS = ("add_raw_function_attribute", "set_struct_body")
    _UNSAFE_SCAFFOLD_MODULES = frozenset(
        {
            "pcc.unsafe",
        }
    )

    def _strict_no_libpython_import_fallback_enabled(self) -> bool:
        return bool(getattr(self, "_strict_no_libpython", False))

    def _emit_strict_no_libpython_import_error(
        self,
        module_name: str,
        span: Optional[SourceSpan],
    ) -> None:
        self._emit_builtin_exception_and_branch(
            "ImportError",
            "No module named " + repr(module_name),
            span,
        )

    def _is_extern_scaffold_import_module(
        self,
        module_name: Optional[str],
    ) -> bool:
        if module_name == "pcc.llvm_capi.compat":
            return self._ir_scaffold_enabled()
        return module_name in (
            "pcc.extern",
            "pcc.llvm_capi",
            "pcc.llvm_capi.compat",
        )

    _COMPILE_TIME_ONLY_MODULES = frozenset(
        {
            "__future__",
            "typing",
            # Abstract-base-class markers only affect CPython-side
            # introspection. pcc handles @abstractmethod as a no-op
            # decorator and does not need ABC at runtime.
            "abc",
            # ``click`` contributes decorators and marker values that pcc
            # treats as no-ops; see the decorator whitelist above. Tests
            # that actually need click-parsed CLI args run under CPython
            # and don't reach this path.
            "click",
            # pcc.extern exposes C FFI type markers (``c_int`` / ``c_ptr``
            # etc.) consumed at codegen time — the resulting binary never
            # resolves them at runtime. Drop the import so self-host
            # doesn't chase missing symbols.
            "pcc.extern",
        }
    )
    _COMPILE_TIME_ONLY_IMPORT_FROMS = {
        "abc": frozenset({"ABC", "abstractmethod"}),
        "dataclasses": frozenset({"dataclass", "field", "replace"}),
    }
    _TEST_FACADE_IMPORT_MODULES = frozenset(
        {
            "pytest",
            "pcc.test_runner",
        }
    )
    _ANNOTATION_ONLY_IMPORT_MODULES = frozenset(
        {
            "llvmlite.binding",
            "llvmlite.ir",
        }
    )

    def _is_test_facade_import_module(self, module_name: Optional[str]) -> bool:
        return module_name in ("pytest", "pcc.test_runner")

    def _name_used_at_runtime(self, ident: str) -> bool:
        """Return True if ``ident`` appears outside annotation slots.

        ``from __future__ import annotations`` is common in the pass
        sources. Imports that only feed annotations should not become
        runtime ``py_cpy_import`` edges in self-hosted binaries, but the
        same modules must still import normally when their binding is
        actually called or read.
        """
        annotation_slots = {"annotation", "return_ty"}

        def walk(x) -> bool:
            if x is None:
                return False
            if isinstance(x, Type):
                return False
            if isinstance(x, (Import, ImportFrom)):
                return False
            if isinstance(x, tuple):
                for it in x:
                    if walk(it):
                        return True
                return False
            if isinstance(x, _Name):
                return x.ident == ident
            for slot in _dataclass_field_names(x):
                if slot in annotation_slots:
                    continue
                if walk(_dataclass_field_value(x, slot, None)):
                    return True
            return False

        return walk(self.ast_module.body)

    def _filter_runtime_import_from_names(
        self, stmt: ImportFrom
    ) -> list[tuple[str, Optional[str]]]:
        """Drop ``from X import Y`` bindings consumed entirely at compile time.

        Keep this narrower than ``_COMPILE_TIME_ONLY_MODULES``: only
        names whose runtime binding is provably unused by pcc's lowered
        code should vanish here. ``dataclass`` and ``field`` are handled
        natively by class lowering / call lowering, while other
        dataclasses helpers such as ``replace`` still need a real runtime
        binding.
        """
        import_module = _import_from_module_or_empty(stmt)
        compile_only = None
        if import_module == "abc":
            compile_only = frozenset({"ABC", "abstractmethod"})
        elif import_module == "dataclasses":
            compile_only = frozenset({"dataclass", "field", "replace"})
        if not compile_only:
            return list(stmt.names)
        filtered: list[tuple[str, Optional[str]]] = []
        for attr_name, as_name in stmt.names:
            if attr_name in compile_only and (as_name is None or as_name == attr_name):
                continue
            filtered.append((attr_name, as_name))
        return filtered

    def _register_typing_compile_time_aliases(self, stmt: ImportFrom) -> None:
        for attr_name, as_name in stmt.names:
            local_name = as_name or attr_name
            if attr_name == "TYPE_CHECKING":
                self._register_native_builtin_value_alias(
                    local_name,
                    "typing.TYPE_CHECKING",
                )
                continue
            if attr_name in (
                "Generic",
                "Protocol",
                "TypeVar",
                "runtime_checkable",
                "get_origin",
                "get_args",
                "Optional",
                "Literal",
                "Union",
                "Any",
                "Type",
                "ClassVar",
                "Final",
                "NoReturn",
                "Callable",
                "Iterator",
                "Iterable",
                "Sequence",
                "Mapping",
                "List",
                "Dict",
                "Set",
                "Tuple",
                "SupportsIndex",
                "TypeAlias",
                "TypeAliasType",
                "TypedDict",
            ):
                self._register_native_builtin_value_alias(
                    local_name,
                    "typing." + attr_name,
                )

    def _emit_cpython_module_value(self, module_name: str) -> ir.Value:
        self._ensure_cpy_init()
        mod_ptr = self._ptr_to_cstr(
            self._cstr_global(module_name, f".cpy.mod.{module_name}")
        )
        mod_val = self.builder.call(
            self.runtime["py_cpy_import"],
            [mod_ptr],
            name=self._fresh(f"cpy.import.{module_name.replace('.', '_')}"),
        )
        # PyImport_ImportModule returns a new reference.  This helper is used
        # for ephemeral builtin-module expressions (persistent import bindings
        # are emitted through the separate module-global paths below), so the
        # expression consumer must eventually release it.
        return self._mark_owned_cpy_value(mod_val)

    def _native_extension_name_uses_cpython_abi(self, path: str) -> bool:
        lower = os.path.basename(path).lower()
        if ".cpython-" in lower or "-cpython-" in lower or "_cpython-" in lower:
            return True
        if ".abi3" in lower or "-abi3" in lower or "_abi3" in lower:
            return True
        first_cp = lower.find("-cp")
        return first_cp >= 0 and lower.find("-cp", first_cp + 3) >= 0

    def _resolve_pcc_native_extension_path(self, module_name: str) -> Optional[str]:
        rel = module_name.replace(".", os.sep)
        suffixes = (".so", ".dylib", ".pyd", ".dll")
        for site_root in package_site_roots():
            if not site_root:
                continue
            base = os.path.join(site_root, rel)
            for suffix in suffixes:
                candidate = base + suffix
                if os.path.isfile(
                    candidate
                ) and not self._native_extension_name_uses_cpython_abi(candidate):
                    return os.path.abspath(candidate)
            parent = os.path.dirname(base)
            leaf = os.path.basename(base)
            if not parent or not os.path.isdir(parent):
                continue
            try:
                names = sorted(os.listdir(parent))
            except OSError:
                names = []
            for name in names:
                full = os.path.join(parent, name)
                if not os.path.isfile(full):
                    continue
                if not name.startswith(leaf + "."):
                    continue
                if not name.lower().endswith(suffixes):
                    continue
                if self._native_extension_name_uses_cpython_abi(name):
                    continue
                return os.path.abspath(full)
        return None

    def _publish_module_scope_import_binding(
        self,
        local_name: str,
        value: ir.Value,
    ) -> None:
        """Publish an executed module-scope import at statement time.

        Imports are observable while a module is only partially initialized.
        The end-of-module globals publication remains the final namespace
        synchronization, but waiting for it here makes import cycles lose
        bindings that CPython exposes as soon as their statement completes.
        """
        if self.current_func_def is not None:
            return
        current_module = self.ast_module.name or "__main__"
        module_name_ptr = self._ptr_to_cstr(
            self._cstr_global(
                current_module,
                f".pcc.import.binding.module.{current_module}",
            )
        )
        self.builder.call(
            self.runtime["py_module_attr_set"],
            [module_name_ptr, self._attr_name_ptr(local_name), value],
            name=self._fresh(f"pcc.import.binding.publish.{local_name}"),
        )

    def _emit_native_extension_import(
        self,
        module_name: str,
        local_name: str,
        extension_path: str,
    ) -> None:
        mod_ptr = self._ptr_to_cstr(
            self._cstr_global(module_name, f".pcc.ext.mod.{module_name}")
        )
        path_ptr = self._ptr_to_cstr(
            self._cstr_global(extension_path, f".pcc.ext.path.{module_name}")
        )
        mod_val = self.builder.call(
            self.runtime["py_native_extension_import"],
            [mod_ptr, path_ptr],
            name=self._fresh(f"pcc.ext.import.{module_name.replace('.', '_')}"),
        )
        self._emit_post_call_err_check()
        gv = self._native_extension_module_global(local_name)
        self.builder.store(mod_val, gv)
        self._native_extension_modules()[local_name] = gv
        self._publish_module_scope_import_binding(local_name, mod_val)

    def _emit_compiled_module_import(
        self,
        module_name: str,
        local_name: str,
    ) -> None:
        """Initialize a compiled sibling and bind its real module object.

        Compiled-module initializers are registered before entry execution and
        run on demand here.  This preserves statement order and partially
        initialized package state across import cycles.
        """
        mod_ptr = self._ptr_to_cstr(
            self._cstr_global(module_name, f".pcc.compiled.mod.{module_name}")
        )
        mod_val = self.builder.call(
            self.runtime["py_compiled_module_import_by_name"],
            [mod_ptr],
            name=self._fresh(f"pcc.compiled.import.{module_name.replace('.', '_')}"),
        )
        self._emit_post_call_err_check()
        gv = self._native_extension_module_global(local_name)
        self.builder.store(mod_val, gv)
        self._native_extension_modules()[local_name] = gv
        self._publish_module_scope_import_binding(local_name, mod_val)

    def _emit_compiled_module_ensure_initialized(self, module_name: str) -> None:
        """Run a compiled sibling's guarded top-level initializer on import."""
        mod_ptr = self._ptr_to_cstr(
            self._cstr_global(module_name, f".pcc.compiled.ensure.{module_name}")
        )
        module = self.builder.call(
            self.runtime["py_compiled_module_import_by_name"],
            [mod_ptr],
            name=self._fresh(f"pcc.compiled.ensure.{module_name.replace('.', '_')}"),
        )
        self._emit_post_call_err_check()
        self._gc_release(module)

    def _emit_ir_scaffold_provider_init(self, import_module) -> None:
        """Run the IR provider's top-level init for an elided scaffold import.

        In ON mode ``_filter_ir_scaffold_closure`` drops
        ``pcc.llvm_capi.compat`` from the source closure and substitutes
        ``pcc.llvm_capi.ir`` as the link provider for the emitted
        ``user_pcc_llvm_capi_ir_*`` calls.  Registering compile-time markers
        is all this statement owes the imported *names*, but the provider
        also has module-level state: its class objects live in
        ``@.class.pcc_llvm_capi_ir.*`` globals that only
        ``_pcc_py_module_top_pcc_llvm_capi_ir`` fills in.  Eliding the import
        removed the one edge that ran it -- the provider was still registered
        with ``py_compiled_module_register_init``, but registration only
        records the initializer, it never executes it.  The first
        ``ir.IntType(1)`` then read ``cls._cache`` off a NULL class, which
        surfaced as ``'object' object has no attribute '_cache'`` and is why
        pcc1 could not compile C at pcc/codegen/c_types.py:22.
        """
        if import_module not in (
            self._IR_RUNTIME_COMPAT_MODULE, "pcc.llvm_capi"
        ):
            return
        provider = self._IR_RUNTIME_PROVIDER_MODULE
        if getattr(self.ast_module, "name", None) == provider:
            return
        if provider not in getattr(self, "_sibling_module_inits", ()):
            return
        self._emit_compiled_module_ensure_initialized(provider)

    def _emit_native_extension_import_from(
        self,
        module_name: str,
        extension_path: str,
        names: list[tuple[str, Optional[str]]],
        span=None,
    ) -> None:
        mod_ptr = self._ptr_to_cstr(
            self._cstr_global(module_name, f".pcc.ext.from.mod.{module_name}")
        )
        path_ptr = self._ptr_to_cstr(
            self._cstr_global(extension_path, f".pcc.ext.from.path.{module_name}")
        )
        module = self.builder.call(
            self.runtime["py_native_extension_import"],
            [mod_ptr, path_ptr],
            name=self._fresh(f"pcc.ext.from.import.{module_name.replace('.', '_')}"),
        )
        self._emit_post_call_err_check()
        for attr_name, as_name in names:
            if attr_name == "*":
                gv = self._native_extension_star_module_global(module_name)
                self.builder.store(module, gv)
                current_module = self.ast_module.name or "__main__"
                current_module_ptr = self._ptr_to_cstr(
                    self._cstr_global(
                        current_module,
                        f".pcc.ext.star.dest.{current_module}",
                    )
                )
                self.builder.call(
                    self.runtime["py_module_import_star"],
                    [current_module_ptr, module],
                    name=self._fresh("pcc.ext.star.import"),
                )
                self._emit_post_call_err_check()
                continue
            attr_ptr = self._attr_name_ptr(attr_name)
            value = self.builder.call(
                self.runtime["py_obj_getattr"],
                [module, attr_ptr],
                name=self._fresh(f"pcc.ext.from.{attr_name}"),
            )
            self._emit_post_call_err_check()
            local_name = as_name or attr_name
            if self.current_func_def is not None and local_name not in getattr(
                self, "_current_global_names", set()
            ):
                # An import statement binds in the executing function's local
                # scope.  A same-named module global declared elsewhere must
                # not capture this value (for example a helper importing
                # ``make_scanner`` before the module later assigns its public
                # ``make_scanner`` alias).
                self._store_unpack_target(
                    Name(
                        span=span,
                        ty=DynType(name="dyn"),
                        ident=local_name,
                    ),
                    value,
                    DynType(name="dyn"),
                    value_is_owned=True,
                )
                continue
            gv = self._native_extension_module_global(local_name)
            self.builder.store(value, gv)
            self._native_extension_modules()[local_name] = gv
            self._publish_module_scope_import_binding(local_name, value)
        self._gc_release(module)

    def _emit_compiled_module_import_from(
        self,
        module_name: str,
        names: list[tuple[str, Optional[str]]],
    ) -> None:
        """Load dynamic exports from an initialized sibling module.

        Static cross-module functions and classes are bound earlier through
        the native export table.  This path covers exports used as ordinary
        Python values, such as a class passed to ``isinstance`` or stored in a
        keyword dictionary, without routing them through libpython.
        """
        mod_ptr = self._ptr_to_cstr(
            self._cstr_global(module_name, f".pcc.compiled.from.mod.{module_name}")
        )
        module = self.builder.call(
            self.runtime["py_compiled_module_import_by_name"],
            [mod_ptr],
            name=self._fresh(
                f"pcc.compiled.from.import.{module_name.replace('.', '_')}"
            ),
        )
        self._emit_post_call_err_check()
        for attr_name, as_name in names:
            if attr_name == "*":
                raise NotImplementedError(
                    "star import from compiled sibling module is not supported"
                )
            attr_ptr = self._attr_name_ptr(attr_name)
            value = self.builder.call(
                self.runtime["py_obj_getattr"],
                [module, attr_ptr],
                name=self._fresh(f"pcc.compiled.from.{attr_name}"),
            )
            self._emit_post_call_err_check()
            local_name = as_name or attr_name
            gv = self._native_extension_module_global(local_name)
            self.builder.store(value, gv)
            self._native_extension_modules()[local_name] = gv
            self._publish_module_scope_import_binding(local_name, value)
        self._gc_release(module)

    def _emit_compiled_module_import_star(self, module_name: str) -> None:
        """Copy a compiled sibling's runtime namespace for ``import *``.

        The closed-world export table covers statically declared names.  Python
        modules may also publish names dynamically through ``globals()`` and
        extend ``__all__`` while executing; copy the live module dictionary so
        those names participate in the same module object graph.
        """
        mod_ptr = self._ptr_to_cstr(
            self._cstr_global(module_name, f".pcc.compiled.star.mod.{module_name}")
        )
        module = self.builder.call(
            self.runtime["py_compiled_module_import_by_name"],
            [mod_ptr],
            name=self._fresh(
                f"pcc.compiled.star.import.{module_name.replace('.', '_')}"
            ),
        )
        self._emit_post_call_err_check()
        current_module = self.ast_module.name or "__main__"
        current_module_ptr = self._ptr_to_cstr(
            self._cstr_global(
                current_module,
                f".pcc.compiled.star.dest.{current_module}",
            )
        )
        self.builder.call(
            self.runtime["py_module_import_star"],
            [current_module_ptr, module],
            name=self._fresh("pcc.compiled.star.copy"),
        )
        self._emit_post_call_err_check()
        self._gc_release(module)

    def _emit_import(self, stmt: Import) -> None:
        """Lower ``import a`` / ``import a.b`` / ``import a.b as c`` via
        py_cpy_import. For dotted names without an alias, we import the
        full path (to ensure submodules are loaded) but bind the
        top-level package under its short name, matching CPython's
        ``import a.b`` semantics (access via ``a.b``). With an
        ``as`` alias we bind the leaf module to that alias."""
        # Compile-time-only modules drop out entirely.
        stmt_names = []
        for m, a in stmt.names:
            if self._is_test_facade_import_module(m):
                continue
            if m == "typing":
                self._register_native_builtin_module_alias(a or m, "typing")
                continue
            if (
                m.split(".")[0] in ("__future__", "typing", "abc", "click")
                or m == "pcc.extern"
            ):
                continue
            local_name = a or m.split(".")[0]
            if m in (
                "llvmlite.binding",
                "llvmlite.ir",
            ) and not self._name_used_at_runtime(local_name):
                continue
            stmt_names.append((m, a))
        if not stmt_names:
            return
        # Issue 11.B.1.2: when recursive_stdlib pulled the imported
        # module into the multi-file native compile set, skip the
        # py_cpy_import call and register as a native module alias so
        # subsequent ``module.X`` access resolves to ``user_<mod>_<X>``.
        native_table = self._native_module_exports
        for mod_name, as_name in stmt_names:
            builtin_module = mod_name
            if (
                as_name is None
                and "." in mod_name
                and mod_name.split(".")[0] in self._NATIVE_BUILTIN_IMPORT_MODULES
            ):
                # ``import os.path`` binds ``os``, not the leaf -- the submodule
                # is reached as an attribute of the package.  Matching only the
                # full dotted name missed the builtin table, so the statement
                # fell through to the strict-mode error ``No module named
                # 'os.path'``.  ``pcc/ply/yacc.py`` is the site that blocked
                # pcc1 from compiling C.
                builtin_module = mod_name.split(".")[0]
            if builtin_module in self._NATIVE_BUILTIN_IMPORT_MODULES:
                mod_name = builtin_module
                if mod_name in getattr(self, "_sibling_module_inits", ()):
                    self._emit_compiled_module_ensure_initialized(mod_name)
                self._register_native_builtin_module_alias(
                    as_name or mod_name,
                    mod_name,
                )
                continue
            if native_table is not None and mod_name in native_table:
                # Native sibling: initialize it at the import statement, then
                # register the static alias.  An unaliased dotted import binds
                # the top-level package, not the leaf (``import a.b`` binds
                # ``a``).  The deep dotted lookup below still resolves
                # ``a.b.X`` against the leaf's export table.  Keeping the root
                # binding also means a later ``import a.b`` cannot erase
                # exports already available as ``a.X``.
                if mod_name in getattr(self, "_sibling_module_inits", ()):
                    self._emit_compiled_module_ensure_initialized(mod_name)
                local_name = as_name or mod_name.split(".")[0]
                alias_module = mod_name
                if as_name is None and "." in mod_name:
                    top_module = mod_name.split(".")[0]
                    if top_module in native_table:
                        alias_module = top_module
                self._register_native_module_alias(local_name, alias_module)
                continue
            if mod_name in getattr(self, "_sibling_module_inits", ()):
                local_name = as_name or mod_name.split(".")[0]
                self._emit_compiled_module_import(mod_name, local_name)
                continue
            extension_path = self._resolve_pcc_native_extension_path(mod_name)
            if extension_path is not None:
                local_name = as_name or mod_name.split(".")[0]
                self._emit_native_extension_import(mod_name, local_name, extension_path)
                continue
            if (
                self._strict_no_libpython_import_fallback_enabled()
                and mod_name not in self._ANNOTATION_ONLY_IMPORT_MODULES
            ):
                self._emit_strict_no_libpython_import_error(mod_name, stmt.span)
                return
            # llvmlite.binding / llvmlite.ir are pcc's own LLVM-backend deps:
            # when actually used at runtime (annotation-only use is already
            # dropped above) they import via CPython even in --python-libpython=off.
            # The self-backend bootstrap closure never uses them at runtime, so
            # this exemption stays inert there.
            self._ensure_cpy_init()
            cpy_modules = self._cpy_modules()
            # Always import the full dotted path so side-effect
            # submodule registration runs.
            full_ptr = self._ptr_to_cstr(
                self._cstr_global(mod_name, f".cpy.mod.{mod_name}")
            )
            leaf_val = self.builder.call(
                self.runtime["py_cpy_import"],
                [full_ptr],
                name=self._fresh(f"cpy.import.{mod_name.replace('.', '_')}"),
            )
            if as_name is None and "." in mod_name:
                # ``import urllib.parse`` — bind urllib (the top-level),
                # not the leaf, so ``urllib.parse.quote`` works.
                top_name = mod_name.split(".")[0]
                top_ptr = self._ptr_to_cstr(
                    self._cstr_global(top_name, f".cpy.mod.{top_name}")
                )
                top_val = self.builder.call(
                    self.runtime["py_cpy_import"],
                    [top_ptr],
                    name=self._fresh(f"cpy.import.{top_name}"),
                )
                gv = self._cpy_module_global(top_name)
                self.builder.store(top_val, gv)
                cpy_modules[top_name] = gv
                # Leaf reference is no longer needed — release.
                self.builder.call(self.runtime["py_cpy_decref"], [leaf_val])
            else:
                local_name = as_name or mod_name
                gv = self._cpy_module_global(local_name)
                self.builder.store(leaf_val, gv)
                cpy_modules[local_name] = gv

    def _import_cpython_module_single(
        self,
        module_name: str,
        local_name: str,
    ) -> None:
        """Bind ``local_name`` to a CPython module object."""
        if self._strict_no_libpython_import_fallback_enabled():
            self._emit_strict_no_libpython_import_error(module_name, None)
            return
        self._ensure_cpy_init()
        cpy_modules = self._cpy_modules()
        mod_ptr = self._ptr_to_cstr(
            self._cstr_global(module_name, f".cpy.mod.{module_name}")
        )
        mod_val = self.builder.call(
            self.runtime["py_cpy_import"],
            [mod_ptr],
            name=self._fresh(f"cpy.import.{module_name.replace('.', '_')}"),
        )
        gv = self._cpy_module_global(local_name)
        self.builder.store(mod_val, gv)
        cpy_modules[local_name] = gv

    def _import_from_cpython_single(
        self,
        stmt: ImportFrom,
        src_module: str,
        attr_name: str,
        as_name,
    ) -> None:
        """Route a single ``from X import Y`` entry through the
        existing CPython-import machinery — used when the multi-file
        path can't model the exported symbol (class / constant for
        now)."""
        self._ensure_cpy_init()
        cpy_modules = self._cpy_modules()
        mod_ptr = self._ptr_to_cstr(
            self._cstr_global(src_module, f".cpy.mod.{src_module}")
        )
        mod_val = self.builder.call(
            self.runtime["py_cpy_import"],
            [mod_ptr],
            name=self._fresh(f"cpy.fromimport.{src_module}"),
        )
        local_name = as_name or attr_name
        attr_ptr = self._ptr_to_cstr(
            self._cstr_global(attr_name, f".cpy.attr.{attr_name}")
        )
        val = self.builder.call(
            self.runtime["py_cpy_getattr"],
            [mod_val, attr_ptr],
            name=self._fresh(f"cpy.from.{local_name}"),
        )
        gv = self._cpy_module_global(local_name)
        self.builder.store(val, gv)
        cpy_modules[local_name] = gv

    def _resolve_relative_import(self, stmt: ImportFrom) -> str:
        """Turn a relative ``from .lib import X`` into its absolute
        dotted module name using ``self.ast_module.name`` as the
        current package context. Non-relative imports are returned
        unchanged."""
        level = _import_from_level_or_zero(stmt)
        if level == 0:
            return _import_from_module_or_empty(stmt)
        cur = self.ast_module.name or ""
        parts = cur.split(".") if cur else []
        cur_file = stmt.span.file or ""
        cur_file = cur_file.replace("\\", "/")
        is_package_init = cur_file == "__init__.py" or cur_file.endswith("/__init__.py")
        package_parts = parts if is_package_init else parts[:-1]
        # Relative imports count from the current package:
        # ``from .x`` means "stay in this package", ``from ..x`` pops
        # one package level, etc.
        up = level - 1
        if up > len(package_parts):
            # Over-dotted relative import; fall back to the raw name.
            return _import_from_module_or_empty(stmt)
        base_parts = package_parts[: len(package_parts) - up]
        import_module = _import_from_module_or_empty(stmt)
        if import_module:
            return ".".join(base_parts + [import_module])
        return ".".join(base_parts)

    def _path_dirname(self, path: str) -> str:
        path = (path or "").replace("\\", "/")
        if not path:
            return ""
        i = len(path) - 1
        while i >= 0 and path[i] == "/":
            i -= 1
        while i >= 0 and path[i] != "/":
            i -= 1
        if i < 0:
            return ""
        if i == 0:
            return "/"
        return path[:i]

    def _path_basename(self, path: str) -> str:
        path = (path or "").replace("\\", "/")
        i = len(path) - 1
        while i >= 0 and path[i] == "/":
            i -= 1
        if i < 0:
            return ""
        end = i + 1
        while i >= 0 and path[i] != "/":
            i -= 1
        return path[i + 1 : end]

    def _module_root_from_src_path(self, src_path: str, module_name: str) -> str:
        cur_dir = self._path_dirname(src_path)
        parts = module_name.split(".")
        up = (
            len(parts)
            if self._path_basename(src_path) == "__init__.py"
            else max(
                0,
                len(parts) - 1,
            )
        )
        i = 0
        while i < up and cur_dir:
            parent = self._path_dirname(cur_dir)
            if parent == cur_dir:
                break
            cur_dir = parent
            i += 1
        return cur_dir

    def _module_src_exists_under_root(self, root_dir: str, dotted_name: str) -> bool:
        if not root_dir or not dotted_name:
            return False
        rel = dotted_name.replace(".", "/")
        py_path = root_dir + "/" + rel + ".py"
        init_path = root_dir + "/" + rel + "/__init__.py"
        return os.path.isfile(py_path) or os.path.isfile(init_path)

    def _resolve_relative_import_submodule(
        self,
        stmt: ImportFrom,
        import_module: str,
        attr_name: str,
    ) -> Optional[str]:
        """Return ``pkg.mod.attr`` when a relative ``from`` import name
        refers to a real sibling submodule on disk.

        This keeps CPython fallback semantics correct for shapes such as
        ``from . import parser`` / ``from .codegen import layer1`` when
        the surrounding package was not compiled natively into the same
        multi-file closure."""
        if (
            not _import_from_level_or_zero(stmt)
            or not import_module
            or attr_name == "*"
        ):
            return None
        src_file = stmt.span.file or ""
        cur_mod = self.ast_module.name or ""
        if not src_file or not cur_mod:
            return None
        root_dir = self._module_root_from_src_path(src_file, cur_mod)
        full_name = import_module + "." + attr_name
        if self._module_src_exists_under_root(root_dir, full_name):
            return full_name
        return None

    def _emit_import_from(self, stmt: ImportFrom) -> None:
        """Lower ``from a import b`` via py_cpy_import + py_cpy_getattr,
        UNLESS ``a`` is one of the pcc compile-time scaffold modules
        (``pcc.extern`` / ``pcc.llvm_capi`` / ``pcc.unsafe``) — in that
        case the names are compile-time markers and we register each
        one in a per-module registry without emitting any runtime IR."""
        import_module = _import_from_module_or_empty(stmt)
        raw_import_module = import_module
        if self._is_extern_scaffold_import_module(import_module):
            self._register_extern_scaffold_imports(stmt)
            self._emit_ir_scaffold_provider_init(import_module)
            if import_module == self._IR_RUNTIME_COMPAT_MODULE:
                helpers = []
                for name, alias in stmt.names:
                    if name in self._IR_RUNTIME_COMPAT_HELPERS:
                        helpers.append((name, alias))
                if helpers:
                    self._emit_compiled_module_import_from(
                        self._IR_RUNTIME_PROVIDER_MODULE, helpers
                    )
            return
        if self._is_test_facade_import_module(import_module):
            return
        if import_module == "pcc.unsafe":
            self._register_unsafe_scaffold_imports(stmt)
            return
        if import_module and (
            import_module.split(".")[0] in ("__future__", "typing", "abc", "click")
            or import_module == "pcc.extern"
        ):
            if import_module == "typing":
                self._register_typing_compile_time_aliases(stmt)
            # Consumed at parse / type-inference time; no runtime IR.
            return
        stmt_names = self._filter_runtime_import_from_names(stmt)
        if not stmt_names:
            return

        # Multi-file compile: if the source module is a sibling being
        # compiled in the same invocation, declare each imported name
        # as an external function (for now only functions — classes and
        # constants are follow-ups) and register in the user-function
        # table so direct calls emit ``call @user_<mod>_<fn>``.
        native_table = self._native_module_exports
        if native_table is not None:
            import_module = self._resolve_relative_import(stmt)
            if import_module in getattr(self, "_sibling_module_inits", ()):
                self._emit_compiled_module_ensure_initialized(import_module)
            remaining_names: list[tuple[str, Optional[str]]] = []
            for attr_name, as_name in stmt_names:
                extension_submodule = None
                extension_submodule_path = None
                if import_module and attr_name != "*":
                    extension_submodule = import_module + "." + attr_name
                    extension_submodule_path = self._resolve_pcc_native_extension_path(
                        extension_submodule
                    )
                if extension_submodule_path is not None:
                    # ``from . import _native`` inside a package names the
                    # extension submodule itself.  Extension modules are not
                    # Python AST siblings and therefore do not appear in the
                    # closed-world export table; resolve their pinned artifact
                    # before treating a missing package export as ImportError.
                    self._emit_native_extension_import(
                        extension_submodule,
                        as_name or attr_name,
                        extension_submodule_path,
                    )
                    continue
                full_submodule = self._native_import_from_submodule(
                    import_module,
                    attr_name,
                )
                if full_submodule is None:
                    full_submodule = self._resolve_relative_import_submodule(
                        stmt,
                        import_module,
                        attr_name,
                    )
                if full_submodule is not None and full_submodule in native_table:
                    local_name = as_name or attr_name
                    if full_submodule in getattr(self, "_sibling_module_inits", ()):
                        # Keep the real module object as the import binding as
                        # well as the static alias used by direct cross-module
                        # calls.  Module namespace publication later needs the
                        # object so ``hasattr(wrapper, "submodule")`` observes
                        # the binding created by this statement.
                        self._emit_compiled_module_import(
                            full_submodule,
                            local_name,
                        )
                    self._register_native_module_alias(
                        local_name,
                        full_submodule,
                    )
                    continue
                remaining_names.append((attr_name, as_name))
            if not remaining_names:
                return
            stmt_names = remaining_names
            if (
                _import_from_level_or_zero(stmt) > 0
                and raw_import_module == ""
                and import_module in native_table
            ):
                exports = native_table.get(import_module, {})
                for attr_name, _as_name in stmt_names:
                    if attr_name == "*":
                        continue
                    if attr_name in exports:
                        continue
                    self._emit_builtin_exception_and_branch(
                        "ImportError",
                        "cannot import name "
                        + repr(attr_name)
                        + " from "
                        + repr(import_module),
                        stmt.span,
                    )
                    return
            if self._has_native_import_from_targets(
                stmt,
                import_module,
            ):
                self._bind_native_cross_module_imports(
                    stmt,
                    import_module,
                    native_table.get(import_module, {}),
                )
                return
        elif getattr(stmt, "level", 0):
            import_module = self._resolve_relative_import(stmt)
        if self._register_native_builtin_import_from_aliases(
            stmt,
            import_module,
        ):
            if os.environ.get("PCC_PY_DEBUG_IMPORTS"):
                sys.stderr.write(
                    f"[debug] _emit_import_from returning early for {import_module!r}\n"
                )
            return
        extension_path = self._resolve_pcc_native_extension_path(import_module)
        if extension_path is not None:
            self._emit_native_extension_import_from(
                import_module,
                extension_path,
                stmt_names,
                stmt.span,
            )
            return
        if import_module in getattr(self, "_sibling_module_inits", ()):
            self._emit_compiled_module_import_from(import_module, stmt_names)
            return
        if os.environ.get("PCC_DEBUG_BOOTSTRAP_TRACE"):
            sys.stderr.write(
                "debug: import_from_cpy_fallback module="
                + str(import_module)
                + " names="
                + ",".join((a or "") + ":" + (b or "") for a, b in stmt_names)
                + "\n"
            )
        if self._strict_no_libpython_import_fallback_enabled():
            self._emit_strict_no_libpython_import_error(import_module, stmt.span)
            return
        self._ensure_cpy_init()
        cpy_modules = self._cpy_modules()
        mod_ptr = self._ptr_to_cstr(
            self._cstr_global(import_module, f".cpy.mod.{import_module}")
        )
        mod_val = self.builder.call(
            self.runtime["py_cpy_import"],
            [mod_ptr],
            name=self._fresh(f"cpy.fromimport.{import_module}"),
        )
        for attr_name, as_name in stmt_names:
            if attr_name == "*":
                gv = self._cpy_star_module_global(import_module)
                self.builder.store(mod_val, gv)
                continue
            local_name = as_name or attr_name
            submodule_name = self._resolve_relative_import_submodule(
                stmt,
                import_module,
                attr_name,
            )
            if submodule_name is not None:
                sub_ptr = self._ptr_to_cstr(
                    self._cstr_global(
                        submodule_name,
                        f".cpy.mod.{submodule_name}",
                    )
                )
                val = self.builder.call(
                    self.runtime["py_cpy_import"],
                    [sub_ptr],
                    name=self._fresh(
                        f"cpy.fromimport.{submodule_name.replace('.', '_')}"
                    ),
                )
            else:
                attr_ptr = self._ptr_to_cstr(
                    self._cstr_global(attr_name, f".cpy.attr.{attr_name}")
                )
                val = self.builder.call(
                    self.runtime["py_cpy_getattr"],
                    [mod_val, attr_ptr],
                    name=self._fresh(f"cpy.from.{local_name}"),
                )
            gv = self._cpy_module_global(local_name)
            self.builder.store(val, gv)
            cpy_modules[local_name] = gv

    def _register_extern_scaffold_imports(self, stmt: "ImportFrom") -> None:
        """Track ``from pcc.extern import extern, c_int, ...`` bindings
        so the Name-based check in :meth:`_maybe_register_extern_assign`
        can recognize the ``extern`` factory call.
        """
        if not hasattr(self, "_extern_bindings"):
            self._extern_bindings: dict[str, str] = {}
        for attr_name, as_name in stmt.names:
            local = as_name or attr_name
            if (
                stmt.module == self._IR_RUNTIME_COMPAT_MODULE
                and attr_name in self._IR_RUNTIME_COMPAT_HELPERS
            ):
                provider = self._IR_RUNTIME_PROVIDER_MODULE
                exports = (self._native_module_exports or {}).get(provider, {})
                info = exports.get(attr_name)
                if info is None:
                    raise NotImplementedError(
                        "IR compatibility helper requires compiled provider: "
                        + provider + "." + attr_name
                    )
                self._bind_native_cross_module_export(
                    local_name=local, src_module=provider,
                    attr_name=attr_name, info=info,
                )
                continue
            self._extern_bindings[local] = attr_name

    def _register_unsafe_scaffold_imports(self, stmt: "ImportFrom") -> None:
        """Track ``from pcc.unsafe import load_i64, ...`` bindings.

        These names are compiler intrinsics; no runtime import or CPython
        module object should be emitted for them.
        """
        if not hasattr(self, "_unsafe_bindings"):
            self._unsafe_bindings: dict[str, str] = {}
        for attr_name, as_name in stmt.names:
            if attr_name == "*":
                continue
            if not self._is_unsafe_intrinsic(attr_name):
                raise NotImplementedError(f"unknown pcc.unsafe intrinsic {attr_name!r}")
            local = as_name or attr_name
            self._unsafe_bindings[local] = attr_name
