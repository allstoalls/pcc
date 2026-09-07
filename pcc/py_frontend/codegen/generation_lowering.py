"""Top-level module generation entrypoint for L1CodeGen."""

from __future__ import annotations

import os
import sys
import time
from typing import Optional

from pcc.codegen.c_varargs import postprocess_varargs_ir
from pcc.llvm_capi.compat import ir

from ..py_ast import (
    Assign,
    AugAssign,
    ClassDef,
    Delete,
    DynType,
    ExprStmt,
    For,
    FuncDef,
    If,
    Module,
    Name,
    Stmt,
    StrLit,
    Try,
    While,
    With,
)
from .layer1_support import (
    _import_from_module_or_empty,
    _import_names_from_stmt,
    _is_import_from_stmt,
    _is_import_stmt,
)
from . import marshal
from .hoist_lowering import hoist_nested_funcdefs
from .errors import L1CodegenError
from .runtime_abi import declare_runtime
from .typed_int_bounded_proof import compute_bounded_int_abi_function_names
from .vthread_effect_analysis import (
    classify_vthread_park_boundaries,
    compute_vthread_may_park_callables,
)

_UNSAFE_SCAFFOLD_MODULES = frozenset({"pcc.unsafe"})
_TYPE_SCAFFOLD_MODULES = frozenset({"pcc"})
_COMPILE_TIME_SCAFFOLD_MODULES = frozenset(
    {"pcc.py_runtime.py.py_abi_constants"}
)
_FREESTANDING_MARKER = "__pcc_freestanding__"


def _is_freestanding_marker_assign(stmt: Stmt) -> bool:
    if not isinstance(stmt, Assign) or len(stmt.targets) != 1:
        return False
    target = stmt.targets[0]
    return isinstance(target, Name) and target.ident == _FREESTANDING_MARKER


def _codegen_log(parent, enabled: bool, label: str) -> None:
    if not enabled:
        return
    mod_name = parent.ast_module.name or "<module>"
    sys.stderr.write("[pcc.codegen] " + mod_name + ":" + label + "\n")


def _iter_module_block_decls(stmt: Stmt, static_bool_condition=None):
    """Yield ``def``/``class`` statements nested in module-scope blocks.

    Python treats ``def`` and ``class`` as executable statements, so packages
    commonly put them under import-time ``if`` blocks. pcc lowers the bodies
    statically, then statement lowering publishes the callable/class binding
    only when its containing path executes.
    """
    if isinstance(stmt, (FuncDef, ClassDef)):
        yield stmt
        return
    if isinstance(stmt, If):
        static_cond = (
            static_bool_condition(stmt.cond)
            if static_bool_condition is not None
            else None
        )
        if static_cond is True:
            selected_bodies = (stmt.body,)
        elif static_cond is False:
            selected_bodies = (stmt.else_body,)
        else:
            selected_bodies = (stmt.body, stmt.else_body)
        for body in selected_bodies:
            for child in body:
                yield from _iter_module_block_decls(child, static_bool_condition)
        return
    if isinstance(stmt, (While, For)):
        for child in stmt.body:
            yield from _iter_module_block_decls(child, static_bool_condition)
        for child in stmt.else_body:
            yield from _iter_module_block_decls(child, static_bool_condition)
        return
    if isinstance(stmt, Try):
        for child in stmt.body:
            yield from _iter_module_block_decls(child, static_bool_condition)
        for handler in stmt.handlers:
            for child in handler.body:
                yield from _iter_module_block_decls(child, static_bool_condition)
        for child in stmt.else_body:
            yield from _iter_module_block_decls(child, static_bool_condition)
        for child in stmt.finally_body:
            yield from _iter_module_block_decls(child, static_bool_condition)
        return
    if isinstance(stmt, With):
        for child in stmt.body:
            yield from _iter_module_block_decls(child, static_bool_condition)


def _iter_module_block_name_assigns(stmt: Stmt):
    """Yield simple Name assignments nested in a module-scope block."""
    if isinstance(stmt, (FuncDef, ClassDef)):
        return
    if isinstance(stmt, Assign):
        for target in stmt.targets:
            if isinstance(target, Name):
                yield target.ident
        return
    if isinstance(stmt, (If, While, For)):
        for child in stmt.body:
            yield from _iter_module_block_name_assigns(child)
        for child in stmt.else_body:
            yield from _iter_module_block_name_assigns(child)
        return
    if isinstance(stmt, Try):
        for child in stmt.body:
            yield from _iter_module_block_name_assigns(child)
        for handler in stmt.handlers:
            for child in handler.body:
                yield from _iter_module_block_name_assigns(child)
        for child in stmt.else_body:
            yield from _iter_module_block_name_assigns(child)
        for child in stmt.finally_body:
            yield from _iter_module_block_name_assigns(child)
        return
    if isinstance(stmt, With):
        for child in stmt.body:
            yield from _iter_module_block_name_assigns(child)


class GenerationLoweringMixin:
    def _generate_impl(self, module: Optional[Module] = None) -> str:
        """Lower the AST module to an LLVM IR text blob.

        ``module`` may be supplied to override the one given to the
        constructor, matching the task contract.
        """
        debug_codegen = bool(os.environ.get("PCC_DEBUG_CODEGEN_PHASES"))

        _codegen_log(self, debug_codegen, "start")
        saved_skip_program_main = self._skip_program_main
        saved_freestanding_module = self._freestanding_module
        saved_runtime_port_module = self._runtime_port_module
        saved_sibling_module_inits = self._sibling_module_inits
        saved_native_module_exports = self._native_module_exports
        if module is not None:
            setattr(self, "ast_module", module)
            setattr(self, "_ast_body", module.body)
            setattr(self, "_try_err_block", None)
            setattr(self, "module", ir.Module(name=module.name or "pcc_py_module"))
            # The constructor already built a compile unit, but for the module
            # it was handed; this is a different ``ir.Module``, and debug nodes
            # are owned per module.  Rebuild against the one that gets rendered.
            self._di_init(getattr(module, "path", None) or module.name or "<module>")
            setattr(self, "runtime", declare_runtime(self.module))
            setattr(self, "_printf", self._declare_printf())
            setattr(self, "functions", {})
            setattr(self, "_funcdef_functions", {})
            setattr(self, "_native_symbol_funcdefs", {})
            setattr(self, "_function_definition_ordinals", {})
            setattr(self, "_duplicate_module_function_names", set())
            setattr(self, "_c_abi_export_symbols", set())
            setattr(self, "_module_has_c_abi_export", False)
            setattr(self, "_fn_err_exit_blocks", {})
            setattr(self, "_fn_err_exit_gc_root_names", {})
            setattr(self, "_fn_gc_root_slot_registry", {})
            setattr(self, "_fn_err_exit_gc_root_slots", {})
            setattr(self, "_fn_gc_root_exit_sites", {})
            setattr(self, "_post_call_frame_blocks", {})
            setattr(self, "_source_file_lines_cache", {})
            setattr(self, "_owned_dynamic_call_values", set())
            setattr(self, "_last_fresh_direct_native_ctor_value", None)
            marshal.reset_boxed_i64_constants()
            setattr(self, "_fmt_int", None)
            setattr(self, "_fmt_float", None)
            setattr(self, "_fmt_bool_true", None)
            setattr(self, "_fmt_bool_false", None)
            setattr(self, "_str_pool", {})
            setattr(self, "_str_obj_pool", {})
            setattr(self, "_static_literal_init_fn", None)
            setattr(self, "_attr_pool", {})
            setattr(self, "_cstr_pool", {})
            setattr(self, "_str_counter", 0)
            setattr(self, "_cstr_counter", 0)
            setattr(self, "_native_lambda_func_counter", 0)
            setattr(self, "_native_lambda_callback_counter", 0)
            setattr(self, "_lambda_counter", [])
            setattr(self, "_class_type_export_cache", {})
            setattr(self, "_class_aliases", {})
            setattr(self, "_extern_bindings", {})
            setattr(self, "_unsafe_bindings", {})
            setattr(self, "_extern_decls", {})
            setattr(self, "_boxed_int_module_global_names", set())
            setattr(self, "_native_module_aliases", {})
            setattr(self, "_native_module_constant_bindings", {})
            setattr(self, "_native_builtin_module_aliases", {})
            setattr(self, "_native_builtin_value_aliases", {})
            setattr(self, "_native_module_attr_globals", {})
            setattr(self, "_native_re_compile_aliases", {})
            setattr(self, "_native_re_compile_local_aliases", {})
            setattr(self, "_native_re_static_flag_aliases", {})
            setattr(self, "_native_file_values", set())
            setattr(self, "_native_file_env_flags", {})
            setattr(self, "_weakref_env_flags", {})
            setattr(self, "_cross_module_func_defs", {})
            setattr(self, "_cross_module_identity_decorators", {})
            setattr(self, "_cross_module_semantic_functions", {})
            setattr(self, "_module_block_func_defs", {})
            setattr(self, "_module_block_funcdef_ids", set())
            setattr(self, "_unboxed_typed_int_abi_cache", {})
            setattr(self, "_typed_int_abi_call_arg_safety", [])
            setattr(self, "_bounded_int_abi_function_names", [])
            setattr(self, "_funcdef_yield_sentinel_cache", {})
            setattr(self, "_vthread_binding_cache", {})
            setattr(self, "_generator_func_names", set())
            setattr(self, "_vthread_may_park_func_ids", set())
            setattr(self, "_vthread_may_park_func_names", set())
            setattr(self, "_vthread_may_park_method_ids", set())
            setattr(self, "_vthread_may_park_method_keys", set())
            setattr(self, "_vthread_rejected_park_boundaries", {})
            setattr(self, "current_class", None)
            setattr(self, "current_method_kind", None)
            setattr(self, "_skip_program_main", saved_skip_program_main)
            setattr(self, "_freestanding_module", saved_freestanding_module)
            setattr(self, "_runtime_port_module", saved_runtime_port_module)
            setattr(self, "_sibling_module_inits", saved_sibling_module_inits)
            setattr(self, "_native_module_exports", saved_native_module_exports)

        # ``self.class_lowering`` is constructed once in __init__; do
        # NOT reassign here — under pcc-py self-host the default-None
        # → ClassLowering(self) reassignment dropped the new value
        # and made every ``self.class_lowering.X(...)`` fall through
        # to dynamic ``py_cpy_*`` dispatch (forcing libpython).

        # Pre-pass: hoist nested ``def`` blocks out of outer FuncDef /
        # ClassDef method bodies to the module's top level. pcc has
        # no closure-conversion path yet, so the hoisted function is
        # rewritten with a unique ``__nested_<outer>_<name>`` symbol
        # and the original binding in the enclosing body is replaced
        # by an alias-Assign — ``<inner_name> = <hoisted_name>`` —
        # so direct calls ``inner_name(arg)`` continue to route
        # through the existing user-function call path.
        _codegen_log(self, debug_codegen, "hoist begin")
        hoisted = hoist_nested_funcdefs(self)
        _codegen_log(self, debug_codegen, "hoist end " + str(len(hoisted)))
        # This fixed point must run after closure hoisting (so direct nested
        # calls have their final symbols) and before declarations (so affected
        # functions receive the resumable pointer ABI).  Both the analysis and
        # the resulting lowering are compiled into pcc1; there is no host-only
        # source rewrite in this path.
        _codegen_log(self, debug_codegen, "vthread may-park begin")
        (
            may_park_ids,
            may_park_names,
            may_park_method_ids,
            may_park_method_keys,
        ) = compute_vthread_may_park_callables(
            self.ast_module, self._native_module_exports
        )
        setattr(self, "_vthread_may_park_func_ids", may_park_ids)
        setattr(self, "_vthread_may_park_func_names", may_park_names)
        setattr(self, "_vthread_may_park_method_ids", may_park_method_ids)
        setattr(self, "_vthread_may_park_method_keys", may_park_method_keys)
        rejected_park_boundaries = classify_vthread_park_boundaries(
            self.ast_module,
            may_park_names,
            self._native_module_exports,
            may_park_method_keys,
        )
        setattr(
            self,
            "_vthread_rejected_park_boundaries",
            rejected_park_boundaries,
        )
        for method_key in may_park_method_keys:
            reason = rejected_park_boundaries.get(method_key)
            if reason:
                raise L1CodegenError(
                    "may_park method boundary is not statically resumable: "
                    + method_key
                    + ": "
                    + reason
                )
        _codegen_log(
            self,
            debug_codegen,
            "vthread may-park end " + str(len(may_park_names)),
        )
        # Modules importing ``traceback``: synthesize handler bindings so
        # ``traceback.format_exc()``/``print_exc()`` can read the handled
        # exception (see NativeModuleAliasMixin._rewrite_traceback_handler_bindings).
        self._rewrite_traceback_handler_bindings()

        # ``def`` is an executable rebinding statement.  Distinct same-named
        # definitions need distinct native bodies, and all reads/calls of that
        # name must use the live module binding so an earlier function can
        # escape before the later definition executes.  Preserve source order
        # when declaring the widened globals; set iteration would make host
        # and self-host IR order depend on hash-table details.
        module_func_defs: list[FuncDef] = []
        for candidate in self.ast_module.body:
            if isinstance(candidate, FuncDef):
                module_func_defs.append(candidate)
                continue
            if isinstance(candidate, (Try, With, If, While, For)):
                for decl in _iter_module_block_decls(candidate):
                    if isinstance(decl, FuncDef):
                        module_func_defs.append(decl)

        module_function_counts: dict[str, int] = {}
        for candidate in module_func_defs:
            module_function_counts[candidate.name] = (
                module_function_counts.get(candidate.name, 0) + 1
            )
        duplicate_names: list[str] = []
        duplicate_seen: set[str] = set()
        for candidate in module_func_defs:
            if module_function_counts.get(candidate.name, 0) < 2:
                continue
            if candidate.name in duplicate_seen:
                continue
            duplicate_seen.add(candidate.name)
            duplicate_names.append(candidate.name)
        self._duplicate_module_function_names = set(duplicate_names)
        for duplicate_name in duplicate_names:
            self._ensure_module_global_name(
                duplicate_name,
                DynType(name="dyn"),
            )

        _codegen_log(self, debug_codegen, "module flags raw-int begin")
        setattr(
            self,
            "_module_uses_raw_int_scaffold",
            self._module_imports_raw_int_scaffold(),
        )
        _codegen_log(self, debug_codegen, "module flags raw-int end")
        _codegen_log(self, debug_codegen, "module flags c-abi begin")
        setattr(self, "_module_has_c_abi_export", self._module_imports_c_abi_export())
        _codegen_log(self, debug_codegen, "module flags c-abi end")
        _codegen_log(self, debug_codegen, "module flags typed-int begin")
        setattr(
            self,
            "_bounded_int_abi_function_names",
            compute_bounded_int_abi_function_names(self.ast_module),
        )
        setattr(
            self,
            "_typed_int_abi_call_arg_safety",
            self._compute_typed_int_abi_call_arg_safety(),
        )
        _codegen_log(self, debug_codegen, "module flags typed-int end")
        _codegen_log(self, debug_codegen, "module flags done")

        # Predeclare function/class bodies, then queue every executable
        # module-level statement in source order.  A ``def`` is executable:
        # its callable binding becomes visible at that exact point, including
        # to modules reached through an import cycle.
        main_body: list[Stmt] = []
        module_block_decls: list[Stmt] = []
        declared_module_func_ids: set[int] = set()
        declared_module_classes: set[str] = set()

        stmt_index = 0
        for stmt in self.ast_module.body:
            if debug_codegen:
                _codegen_log(self, debug_codegen, "declare begin")
            # Record every ``del <name>`` target anywhere in the module,
            # including inside function bodies where ``global x; del x``
            # unbinds a module global.  Only a name deleted somewhere can be
            # unbound, so reads of every other global stay a plain load.
            self._collect_module_del_targets(stmt)
            if isinstance(stmt, FuncDef):
                if (
                    self._freestanding_module
                    and self._func_c_abi_export_symbol(stmt) is None
                ):
                    raise RuntimeError(
                        "freestanding module functions require "
                        "@c_abi_export: "
                        + stmt.name
                    )
                declared_module_func_ids.add(id(stmt))
                self._prescan_function_module_globals(stmt)
                self._declare_user_function(stmt)
                # Closure conversion appends synthetic ``__nested_*``
                # declarations to the module body so their native bodies can
                # be emitted.  They were never source-level module statements:
                # executing them here would evaluate closure/default values in
                # the wrong (module) scope.
                if (
                    not self._freestanding_module
                    and not stmt.name.startswith("__nested_")
                ):
                    main_body.append(stmt)
            elif isinstance(stmt, ClassDef):
                if self._freestanding_module:
                    raise RuntimeError(
                        "freestanding modules do not support class definitions: "
                        + stmt.name
                    )
                declared_module_classes.add(stmt.name)
                for class_stmt in stmt.body:
                    if isinstance(class_stmt, FuncDef):
                        self._prescan_function_module_globals(class_stmt)
                self.class_lowering.declare_class(stmt)
                # Synthetic function-local classes are duplicated at module
                # scope only so their method bodies can be declared/emitted.
                # Their class object must be constructed by the executable
                # ClassDef retained in the enclosing function body.
                if stmt.name not in self._hoisted_class_capture_params:
                    main_body.append(stmt)
            elif _is_import_stmt(stmt) and not _is_import_from_stmt(stmt):
                if self._freestanding_module:
                    raise RuntimeError(
                        "freestanding modules only support scaffold from-imports"
                    )
                for mod_name, as_name in _import_names_from_stmt(stmt):
                    if self._is_test_facade_import_module(mod_name):
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
                        "copy",
                        "functools",
                        "pickle",
                        "gc",
                        "weakref",
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
                    ):
                        self._register_native_builtin_module_alias(
                            as_name or mod_name,
                            mod_name,
                        )
                        continue
                    # ``import os.path`` (no alias): the binding is
                    # ``os`` (top-level), which should still be treated
                    # as the native ``os`` module — register it as an
                    # alias so ``os.path.X(...)`` and ``os.X(...)``
                    # both reach native dispatch.
                    if as_name is None and "." in mod_name:
                        top = mod_name.split(".")[0]
                        if top in (
                            "builtins",
                            "sys",
                            "os",
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
                            "copy",
                            "functools",
                            "pickle",
                            "gc",
                            "weakref",
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
                                top,
                                top,
                            )
                            continue
                    native_table = self._native_module_exports
                    if native_table is not None and mod_name in native_table:
                        local_name = (
                            mod_name.split(".")[0]
                            if as_name is None and "." in mod_name
                            else as_name or mod_name
                        )
                        self._register_native_module_alias(local_name, mod_name)
                        continue
                    # Match _emit_import's binding convention: bind
                    # the top-level for ``import a.b`` (no alias) so
                    # ``a.b.c`` lookups via getattr succeed.
                    if as_name is None and "." in mod_name:
                        local_name = mod_name.split(".")[0]
                    else:
                        local_name = as_name or mod_name
                    if self._resolve_pcc_native_extension_path(mod_name) is not None:
                        gv = self._native_extension_module_global(local_name)
                        self._native_extension_modules()[local_name] = gv
                        continue
                    self._cpy_module_global(local_name)
                    self._cpy_modules()[local_name] = self._cpy_module_global(
                        local_name
                    )
                main_body.append(stmt)
            elif _is_import_from_stmt(stmt):
                # Compile-time scaffold imports (pcc.extern / pcc.llvm_capi)
                # carry no runtime CPython globals — their names are
                # consumed by codegen during the emit pass. Seed the
                # binding set now so extern decls that follow (and
                # extern calls in user functions) see them.
                import_module = _import_from_module_or_empty(stmt)
                compile_time_scaffold = (
                    import_module in _COMPILE_TIME_SCAFFOLD_MODULES
                    and import_module in (self._native_module_exports or {})
                )
                if self._freestanding_module and not (
                    self._is_extern_scaffold_import_module(import_module)
                    or import_module in _UNSAFE_SCAFFOLD_MODULES
                    or import_module in _TYPE_SCAFFOLD_MODULES
                    or compile_time_scaffold
                ):
                    raise RuntimeError(
                        "freestanding modules only support imports from "
                        "pcc.extern, pcc.unsafe, and registered compile-time "
                        "scaffolds: "
                        + import_module
                    )
                if self._is_extern_scaffold_import_module(import_module):
                    self._register_extern_scaffold_imports(stmt)
                elif self._is_test_facade_import_module(import_module):
                    pass
                elif import_module in _UNSAFE_SCAFFOLD_MODULES:
                    self._register_unsafe_scaffold_imports(stmt)
                elif import_module in _TYPE_SCAFFOLD_MODULES:
                    # ``i64``/``u64`` are annotation-only compile-time
                    # markers.  They deliberately create no runtime import.
                    pass
                elif self._register_native_builtin_import_from_aliases(
                    stmt,
                    self._resolve_relative_import(stmt),
                ):
                    pass
                elif (
                    self._resolve_pcc_native_extension_path(
                        self._resolve_relative_import(stmt)
                    )
                    is not None
                ):
                    resolved_extension = self._resolve_relative_import(stmt)
                    for attr_name, as_name in _import_names_from_stmt(stmt):
                        if attr_name == "*":
                            self._native_extension_star_module_global(
                                resolved_extension
                            )
                            continue
                        local_name = as_name or attr_name
                        gv = self._native_extension_module_global(local_name)
                        self._native_extension_modules()[local_name] = gv
                else:
                    # Multi-file compile: pre-register native sibling
                    # imports in the first pass so user function bodies
                    # emitted immediately after see the extern binding.
                    # The regular CPython-backed side-effect (allocating
                    # a module global) is skipped for native siblings.
                    native_table = self._native_module_exports
                    resolved = (
                        self._resolve_relative_import(stmt)
                        if native_table is not None
                        else None
                    )
                    handled_as_native_submodule = False
                    if native_table is not None:
                        remaining_names = []
                        for attr_name, as_name in _import_names_from_stmt(stmt):
                            full_submodule = self._native_import_from_submodule(
                                resolved,
                                attr_name,
                            )
                            if full_submodule is None:
                                full_submodule = (
                                    self._resolve_relative_import_submodule(
                                        stmt,
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
                        pass
                    elif (
                        native_table is not None
                        and self._has_native_import_from_targets(
                            stmt,
                            resolved,
                        )
                    ):
                        self._predeclare_native_cross_module(
                            stmt,
                            resolved,
                            native_table.get(resolved, {}),
                        )
                    else:
                        for attr_name, as_name in _import_names_from_stmt(stmt):
                            if attr_name == "*":
                                self._cpy_star_module_global(import_module)
                                continue
                            local_name = as_name or attr_name
                            self._cpy_module_global(local_name)
                            self._cpy_modules()[local_name] = self._cpy_module_global(
                                local_name
                            )
                if not self._freestanding_module:
                    main_body.append(stmt)
            elif self._freestanding_module and _is_freestanding_marker_assign(stmt):
                # A compile-time contract marker, not a runtime module global.
                pass
            elif (
                self._freestanding_module
                and stmt_index == 0
                and isinstance(stmt, ExprStmt)
                and isinstance(stmt.expr, StrLit)
            ):
                # Module docstrings are compile-time metadata, not executable
                # freestanding state.
                pass
            elif isinstance(stmt, Assign) and self._maybe_register_extern_assign(stmt):
                # Pre-register extern("symbol", ...) decls during the
                # declare pass so user-function bodies emitted next can
                # resolve the extern callable. Do NOT append to
                # main_body — nothing runtime to emit.
                pass
            elif isinstance(stmt, ExprStmt) and self._maybe_define_unsafe_global_stmt(
                stmt
            ):
                # Compile-time data definition for runtime substrate
                # modules. The generated library object must contain the
                # global symbol even though its synthetic main() is later
                # stripped by the runtime Makefile.
                pass
            elif isinstance(
                stmt, (ExprStmt, Assign, AugAssign, If, While, For, Try, With, Delete)
            ):
                if self._freestanding_module:
                    raise RuntimeError(
                        "freestanding modules do not support executable "
                        "module-scope statements: "
                        + type(stmt).__name__
                    )
                # Top-level statements that belong in the synthetic
                # module-main function so they execute at program
                # start. Top-level ``Name = <expr>`` also declares a
                # module-level global so other functions can read it.
                if isinstance(stmt, Assign):
                    self._maybe_register_class_alias_assign(stmt)
                    self._declare_module_globals_for(stmt)
                # Nested Try/With/If bodies may contain ``import X`` /
                # ``from X import Y`` statements whose bindings need to
                # be registered as module globals so downstream function
                # bodies can resolve them. Pre-scan the transitive body
                # for imports without altering runtime semantics.
                if isinstance(stmt, (Try, With, If, While, For)):
                    self._prescan_nested_imports(stmt)
                    for target_name in _iter_module_block_name_assigns(stmt):
                        self._ensure_module_global_name(
                            target_name,
                            DynType("dyn"),
                        )
                    for decl in _iter_module_block_decls(
                        stmt,
                        self._static_bool_condition,
                    ):
                        if isinstance(decl, FuncDef):
                            decl_id = id(decl)
                            if decl_id in declared_module_func_ids:
                                continue
                            declared_module_func_ids.add(decl_id)
                            self._ensure_module_global_name(
                                decl.name,
                                DynType("dyn"),
                            )
                            self._prescan_function_module_globals(decl)
                            self._declare_user_function(decl)
                            self._module_block_func_defs[decl.name] = decl
                            self._module_block_funcdef_ids.add(decl_id)
                            module_block_decls.append(decl)
                            continue
                        if isinstance(decl, ClassDef):
                            if decl.name in declared_module_classes:
                                continue
                            declared_module_classes.add(decl.name)
                            for class_stmt in decl.body:
                                if isinstance(class_stmt, FuncDef):
                                    self._prescan_function_module_globals(class_stmt)
                            self.class_lowering.declare_class(decl)
                            module_block_decls.append(decl)
                main_body.append(stmt)
            else:
                mod_name = self.ast_module.name or "<mod>"
                raise NotImplementedError(
                    "Layer 1 only supports top-level FuncDef / ClassDef / "
                    f"Import / Assign / AugAssign / ExprStmt / If / While / "
                    f"For / Try / With at module scope; got {type(stmt).__name__}"
                    f" (in module {mod_name!r}, stmt_index={stmt_index})"
                )
            if debug_codegen:
                _codegen_log(self, debug_codegen, "declare end")
            stmt_index += 1

        self._predeclare_native_builtin_module_attr_stores(tuple(self.ast_module.body))

        worker_timing = str(
            os.environ.get("PCC_PY_FRONTEND_WORKER_TIMING", "") or ""
        ).strip().lower() in ("1", "true", "yes", "on")
        stmt_index = 0
        for stmt in self.ast_module.body:
            if isinstance(stmt, FuncDef):
                emit_started = time.monotonic() if worker_timing else 0.0
                if worker_timing:
                    sys.stderr.write(
                        "pcc frontend function start module="
                        + (self.ast_module.name or "<module>")
                        + " name="
                        + stmt.name
                        + "\n"
                    )
                _codegen_log(
                    self,
                    debug_codegen,
                    "emit func begin " + str(stmt_index) + " " + stmt.name,
                )
                self._emit_user_function(stmt)
                if worker_timing:
                    sys.stderr.write(
                        "pcc frontend function done module="
                        + (self.ast_module.name or "<module>")
                        + " name="
                        + stmt.name
                        + " elapsed_ms="
                        + str(int((time.monotonic() - emit_started) * 1000))
                        + "\n"
                    )
                _codegen_log(
                    self,
                    debug_codegen,
                    "emit func end " + str(stmt_index) + " " + stmt.name,
                )
            elif isinstance(stmt, ClassDef):
                emit_started = time.monotonic() if worker_timing else 0.0
                if worker_timing:
                    sys.stderr.write(
                        "pcc frontend class start module="
                        + (self.ast_module.name or "<module>")
                        + " name="
                        + stmt.name
                        + "\n"
                    )
                _codegen_log(
                    self,
                    debug_codegen,
                    "emit class begin " + str(stmt_index) + " " + stmt.name,
                )
                self.class_lowering.emit_methods(stmt)
                if worker_timing:
                    sys.stderr.write(
                        "pcc frontend class done module="
                        + (self.ast_module.name or "<module>")
                        + " name="
                        + stmt.name
                        + " elapsed_ms="
                        + str(int((time.monotonic() - emit_started) * 1000))
                        + "\n"
                    )
                _codegen_log(
                    self,
                    debug_codegen,
                    "emit class end " + str(stmt_index) + " " + stmt.name,
                )
            stmt_index += 1
        for decl in module_block_decls:
            if isinstance(decl, FuncDef):
                _codegen_log(self, debug_codegen, "emit block func begin " + decl.name)
                self._emit_user_function(decl)
                _codegen_log(self, debug_codegen, "emit block func end " + decl.name)
            elif isinstance(decl, ClassDef):
                _codegen_log(self, debug_codegen, "emit block class begin " + decl.name)
                self.class_lowering.emit_methods(decl)
                _codegen_log(self, debug_codegen, "emit block class end " + decl.name)

        if not self._freestanding_module:
            _codegen_log(self, debug_codegen, "class module init begin")
            self.class_lowering.emit_module_init()
            _codegen_log(self, debug_codegen, "class module init end")
        # Multi-file compile mode: non-entry modules emit a
        # ``_pcc_py_module_top_<mod>()`` initialiser instead of the
        # program entry ``@main``. The entry module's @main is
        # responsible for calling each other module's top-level init
        # before its own body runs.
        if self._freestanding_module:
            pass
        elif self._skip_program_main:
            _codegen_log(self, debug_codegen, "module top init begin")
            self._emit_module_top_init(main_body)
            _codegen_log(self, debug_codegen, "module top init end")
        else:
            _codegen_log(self, debug_codegen, "program main begin")
            self._emit_program_main(main_body)
            _codegen_log(self, debug_codegen, "program main end")

        if not self._freestanding_module:
            _codegen_log(self, debug_codegen, "module teardown begin")
            self._emit_module_teardown()
            _codegen_log(self, debug_codegen, "module teardown end")

        # Direct-mode shared frame landings reference two module tables whose
        # length is known only now; size them before any rendering/capture.
        self._finalize_traceback_index_tables()
        # The static str literal pool is complete only now as well.
        self._finalize_static_literal_init()

        if str(
            os.environ.get("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "") or ""
        ).strip().lower() in ("1", "true", "yes", "on"):
            direct_started = time.monotonic() if worker_timing else 0.0
            self._direct_indexed_module = self.module.direct_indexed_module()
            if worker_timing:
                from pcc.llvm_capi.direct_indexed_kernel import (
                    direct_indexed_module_cfg_stats,
                    direct_indexed_module_cfg_stats_text,
                )

                sys.stderr.write(
                    "pcc direct indexed capture module="
                    + (self.ast_module.name or "<module>")
                    + " elapsed_ms="
                    + str(int((time.monotonic() - direct_started) * 1000))
                    + " supported_records="
                    + str(self.module._direct_indexed_supported_records)
                    + " fallback_records="
                    + str(self.module._direct_indexed_fallback_records)
                    + "\n"
                )
                # CFG sizing for representation receipts (blocks, edges and
                # the post-call block families), on the same timing channel.
                sys.stderr.write(
                    "pcc direct indexed kernel cfg module="
                    + (self.ast_module.name or "<module>")
                    + " "
                    + direct_indexed_module_cfg_stats_text(
                        direct_indexed_module_cfg_stats(
                            self._direct_indexed_module
                        )
                    )
                    + "\n"
                )
        _codegen_log(self, debug_codegen, "module str begin")
        render_started = time.monotonic() if worker_timing else 0.0
        direct_only = (
            str(os.environ.get("PCC_DIRECT_INDEXED_KERNEL_EMIT", "") or "")
            .strip()
            .lower()
            in ("1", "true", "yes", "on")
            and not (
                str(
                    os.environ.get("PCC_DIRECT_INDEXED_KERNEL_VALIDATE", "")
                    or ""
                )
                .strip()
                .lower()
                in ("1", "true", "yes", "on")
            )
        )
        if direct_only:
            out = ""
        else:
            _mark_freestanding_no_builtins(self)
            out = str(self.module)
            out = postprocess_varargs_ir(out)
        if worker_timing:
            sys.stderr.write(
                "pcc llvm text render module="
                + (self.ast_module.name or "<module>")
                + " elapsed_ms="
                + str(int((time.monotonic() - render_started) * 1000))
                + "\n"
            )
        _codegen_log(self, debug_codegen, "module str end " + str(len(out)))
        return out


def _mark_freestanding_no_builtins(codegen) -> None:
    """Tell any optimizer not to synthesize libc calls in the runtime.

    A freestanding module or runtime port *is* the libc implementation: it
    defines ``memset``, ``memcpy``, ``bzero`` and friends.  Without a marker,
    an optimizer is entitled to recognize the byte-fill loop inside
    ``@memset`` and rewrite it into a call to ``memset`` -- itself.  A real
    compiler avoids that with ``-ffreestanding``/``-fno-builtin``; the IR
    spelling is the ``"no-builtins"`` function attribute.

    pcc emitted no function attributes at all, which is why this went
    unnoticed: pcc's own pass tier does not perform that transform, so nothing
    in the normal path exercised it.  LLVM's ``default<O2>`` does: applied to
    all 170 runtime archive members it produced a runtime whose every program
    hung at startup, spinning inside ``bzero``.  The gap is in pcc's emission,
    not in the optimizer that found it, and it would bite the first owned pass
    that learns to recognize a memset shape.

    The attribute is rendered after the signature's closing paren, which the
    self backend's function-header decoder ignores, so the self path is
    unaffected.
    """
    if not (
        getattr(codegen, "_freestanding_module", False)
        or getattr(codegen, "_runtime_port_module", False)
    ):
        return
    module = getattr(codegen, "module", None)
    functions = getattr(module, "_functions", None) if module is not None else None
    if not functions:
        return
    for fn in functions:
        if fn.is_declaration:
            continue
        attributes = getattr(fn, "attributes", None)
        if attributes is None:
            continue
        attributes.add('"no-builtins"')
