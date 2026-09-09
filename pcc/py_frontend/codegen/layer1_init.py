"""Constructor-state initialization for ``L1CodeGen``."""

from __future__ import annotations

import os
from typing import Optional

from pcc.llvm_capi.compat import ir

from ..py_ast import FuncDef, Module, Type
from .class_gen import ClassLowering
from .layer1_support import _default_native_module_exports
from .runtime_abi import declare_runtime


class Layer1InitMixin:
    def _init_l1_state(
        self,
        module: Module,
        emit_cpy_main_exitcode: bool,
        ir_scaffold_mode: str,
    ) -> None:
        self.ast_module = module
        self._ast_body = module.body
        self._try_err_block = None
        self._cpy_operand_cleanup_block = None
        self._finally_stack = []
        # Bare re-raise lowering consults this compiler-state stack after a
        # handler clears the runtime TLS exception.  Host Python can create
        # the attribute lazily, but self-hosted L1CodeGen has a fixed layout:
        # make the stack real constructor state so pcc1 observes handler
        # pushes performed by exception lowering.
        self._active_handler_excs: list = []
        self._emitting_finally = False
        self._prefer_native_callable_values = False
        self._cpy_values = set()
        # `_cpy_values` is a domain tag only: it also contains borrowed
        # loads from CPython-backed locals and module globals.  Keep the
        # unconsumed new-reference subset separate so call boundaries can
        # release fresh values without touching borrowed owners.
        self._owned_cpy_values = set()
        self.emit_cpy_main_exitcode = emit_cpy_main_exitcode
        self._strict_no_libpython = False
        if ir_scaffold_mode not in ("off", "on"):
            raise ValueError(
                "invalid ir_scaffold_mode "
                + repr(ir_scaffold_mode)
                + "; expected 'off' or 'on'"
            )
        self.ir_scaffold_mode = ir_scaffold_mode
        self.module = ir.Module(name=module.name or "pcc_py_module")
        # Opt-in No.100 differential artifact.  Ordinary codegen never builds
        # it; explicit capture keeps the fixed-layout pcc1 object honest while
        # the direct kernel plane is brought up beside the text oracle.
        self._direct_indexed_module = None
        self._di_init(getattr(module, "path", None) or module.name or "<module>")
        self.runtime: dict[str, ir.Function] = declare_runtime(self.module)
        self._codegen_trace_enabled = bool(os.environ.get("PCC_DEBUG_CODEGEN_PHASES"))
        self._codegen_trace_capacity = 64
        self._codegen_trace_ring: list[tuple] = []
        self._codegen_trace_next: int = 0
        self._codegen_trace_diagnosed: bool = False
        self._codegen_current_stmt_index: int = -1
        self._codegen_current_stmt_kind: str = ""
        self._codegen_current_expr_kind: str = ""
        self._codegen_current_module_name: str = module.name or "<module>"
        self._known_object_refcounts = str(os.environ.get("PCC_KNOWN_OBJECT_REFS", "0")).lower() in ("1", "true", "yes", "on")
        self._debug_release_checks = bool(
            os.environ.get("PCC_DEBUG_RELEASES", "").strip()
            or os.environ.get("PCC_DEBUG_RUNTIME", "").strip()
        )
        self._printf = self._declare_printf()
        self.functions: dict[str, ir.Function] = {}
        # A Python name may be rebound by a later ``def``.  Keep the final
        # name lookup in ``functions`` while retaining the distinct native
        # body belonging to every executable FuncDef statement.
        self._funcdef_functions: dict[int, ir.Function] = {}
        self._native_symbol_funcdefs: dict[str, FuncDef] = {}
        self._function_definition_ordinals: dict[str, int] = {}
        self._duplicate_module_function_names: set[str] = set()
        self._c_abi_export_symbols: set[str] = set()
        self._module_has_c_abi_export = False
        self._fn_err_exit_blocks: dict[str, ir.Block] = {}
        self._fn_err_exit_gc_root_names: dict[str, set[str]] = {}
        # GC frame-root registration is ALLOCA-keyed per function (a local
        # name can be re-bound to a fresh alloca mid-function); these keep
        # the physical enter/leave ledger balanced per slot. Dedup is by
        # object identity (never value-name strings, whose uniquification
        # timing differs between host and self-hosted stages).
        self._fn_gc_root_slot_registry: dict[str, list] = {}
        self._fn_err_exit_gc_root_slots: dict[str, list] = {}
        self._fn_err_exit_for_target_slots: dict[str, list] = {}
        self._fn_valueclass_payload_root_slots: dict[str, list] = {}
        # Function-exit blocks whose cleanup already emitted root leaves;
        # a slot registered later retro-patches its leave into each site
        # (entry enters always run, so every exit must leave every slot).
        self._fn_gc_root_exit_sites: dict[str, list] = {}
        # id(fn) -> err-target name -> line -> block.
        # Nested plain keys, not one tuple key: see
        # ExceptionLoweringMixin._ensure_post_call_frame_block.
        self._post_call_frame_blocks: dict[int, dict] = {}
        # Source text for traceback frames, read once per file: see
        # ExceptionLoweringMixin._emit_exception_frame.
        self._source_file_lines_cache: dict[str, list] = {}
        # Values produced by `py_obj_call` on the generic dynamic-dispatch
        # path.  Always a new reference; not inferable from the AST shape.
        self._owned_dynamic_call_values: set = set()
        # Compile-time, one-shot provenance marker.  It never enters emitted
        # IR: a list.append consumer clears it before lowering its argument,
        # then accepts only the exact SSA returned by emit_instantiate.
        self._last_fresh_direct_native_ctor_value = None
        self._funcdef_yield_sentinel_cache: dict[int, bool] = {}
        self._vthread_binding_cache: dict = {}
        self._generator_ctx_stack: list = []
        self._generator_func_names: set[str] = set()
        # Closed-world virtual-thread effect analysis fills these before
        # function declaration.  Keep exact FuncDef identities separate from
        # direct-call names so a pcc1 build never has to infer the effect from
        # a host-only Python generator transform.
        self._vthread_may_park_func_ids: set[int] = set()
        self._vthread_may_park_func_names: set[str] = set()
        self._vthread_may_park_method_ids: set[int] = set()
        self._vthread_may_park_method_keys: set[str] = set()
        self._vthread_rejected_park_boundaries: dict[str, str] = {}
        self.builder: Optional[ir.IRBuilder] = None
        self.current_function: Optional[ir.Function] = None
        self.current_func_def: Optional[FuncDef] = None
        self._current_entry_block = None
        self._entry_alloca_insert_before_function = None
        self._entry_alloca_insert_index = -1
        # First inline error edge published from a function's entry block.
        # Entry-hoisted GC protocol code (root frame enters, slot
        # initializers) must land before it so every exceptional path sees
        # the same registered roots the text oracle's split block gives it.
        self._entry_inline_edge_anchor_function = None
        self._entry_inline_edge_anchor_record = None
        # Direct-mode shared frame landings: one block per (function, error
        # target) reads a payload index; the module tables below map that
        # index to the raise site's (line, source text).
        self._direct_frame_landings: dict[int, dict[str, tuple]] = {}
        self._tb_index_lines: list[int] = []
        self._tb_index_sources: list = []
        self._tb_index_by_file: dict[str, dict[int, int]] = {}
        self._tb_lines_global = None
        self._tb_sources_global = None
        self.current_class = None
        self.current_method_kind = None
        self._async_body_depth = 0
        self.class_lowering: ClassLowering = ClassLowering(self)
        self._current_global_names: set[str] = set()
        self.env: dict[str, tuple[ir.AllocaInstr, ir.Type, Type]] = {}
        self._module_globals: dict[str, tuple[ir.GlobalVariable, Type]] = {}
        self._module_global_init_flags: dict[str, ir.GlobalVariable] = {}
        # The pipeline fills this with the parser input path.  Keep it explicit
        # constructor state because an empty package has no statement span from
        # which module/resource ``__file__`` could otherwise be recovered.
        self._module_source_path: str = ""
        # Set by the pipeline before lowering.  Target-sensitive unsafe
        # intrinsics must not consult the host platform during cross-target
        # codegen; the IR triple is only serialized after generation.
        self._target_triple: str = ""
        self._cpy_module_flags: dict[str, bool] = {}
        self.env_class_hint: dict[str, str] = {}
        self.env_class_object_hint: dict[str, str] = {}
        # Runtime mutation of a known class attribute invalidates static
        # instance-attribute loads. Host Python can create these fields lazily,
        # but self-hosted L1CodeGen instances use a fixed object layout.
        self._class_attr_runtime_state: dict[tuple[str, str], str] = {}
        self._class_attr_mutation_in_loop_depth = 0
        self._literal_dict_expr_bindings: dict[str, object] = {}
        self._virtual_literal_dict_expr_bindings: set[str] = set()
        self.env_list_elem_class_hint: dict[str, str] = {}
        self._ir_builder_env_flags: dict[str, bool] = {}
        self._class_aliases: dict[str, str] = {}
        # Scaffold bindings are populated by import lowering and read back by
        # assignment/call lowering.  They cannot be lazy on a self-hosted
        # fixed-layout L1CodeGen: ``hasattr`` sees the declared slot even when
        # its value is still NULL, so the first import would mutate NULL.
        self._extern_bindings: dict[str, str] = {}
        self._unsafe_bindings: dict[str, str] = {}
        self._extern_decls: dict[str, tuple[str, list[str], str, bool]] = {}
        self._module_uses_raw_int_scaffold = False
        self._box_int_locals = False
        self._exact_int_env_flags: dict[str, bool] = {}
        self._boxed_int_module_global_names: set[str] = set()
        # Immutable per-function representation plan.  `_exact_int_env_flags`
        # tracks the semantic type of the current binding and can be cleared
        # by an intervening object assignment; this set keeps a later int
        # rebind on the entry-planned object slot.
        self._planned_exact_int_local_names: set[str] = set()
        self.loop_stack: list[tuple[ir.Block, ir.Block]] = []
        self._fmt_int: Optional[ir.GlobalVariable] = None
        self._fmt_float: Optional[ir.GlobalVariable] = None
        self._fmt_bool_true: Optional[ir.GlobalVariable] = None
        self._fmt_bool_false: Optional[ir.GlobalVariable] = None
        self._str_pool: dict[str, ir.GlobalVariable] = {}
        self._str_obj_pool: dict[str, ir.GlobalVariable] = {}
        self._static_literal_init_fn = None
        self._attr_pool: dict[str, ir.GlobalVariable] = {}
        self._cstr_pool: dict[str, ir.GlobalVariable] = {}
        self._str_counter = 0
        self._cstr_counter = 0
        # These counters are read by lambda lowering after an ``hasattr``
        # guard.  A self-hosted L1CodeGen has a fixed class layout, so a
        # declared-but-uninitialized slot can appear present while still
        # containing NULL.  Initialize them as ordinary constructor state.
        self._native_lambda_func_counter = 0
        self._native_lambda_callback_counter = 0
        self._lambda_counter: list[str] = []
        self._class_type_export_cache: dict[tuple[str, str], Optional[str]] = {}
        self._tmp_counter = 0
        self._skip_program_main: bool = False
        self._python_library: bool = False
        self._freestanding_module: bool = False
        # Runtime ports (``__pcc_runtime_port__ = True``) keep raw pointers in
        # the pointer lane.  Derived from the module body here so every codegen
        # construction site (single source, multi-module, frontend workers)
        # agrees with type inference without pipeline plumbing.
        self._runtime_port_module: bool = _module_declares_runtime_port(module)
        # id(value) -> (module-level source global, value): lets cleanup sites
        # re-derive a global-backed value that their block does not dominate.
        self._global_backed_values: dict = {}
        self._sibling_module_inits: tuple[str, ...] = ()
        self._native_module_exports: Optional[dict] = _default_native_module_exports(
            module.name
        )
        self._native_function_object_exports: dict[str, bool] = {}
        self._native_module_aliases: dict[str, str] = {}
        self._native_module_constant_bindings: dict[str, dict] = {}
        self._native_module_object_aliases: dict[str, str] = {}
        self._native_re_compile_aliases: dict = {}
        self._native_re_compile_local_aliases: dict = {}
        self._native_re_static_flag_aliases: dict[str, int] = {}
        self._native_extension_module_env: dict[str, ir.GlobalVariable] = {}
        self._native_builtin_module_aliases: dict[str, str] = {}
        self._native_builtin_value_aliases: dict[str, str] = {}
        self._typing_typevar_aliases: dict[str, str] = {}
        self._typing_optional_aliases: dict[str, str] = {}
        self._typing_metadata_aliases: set = set()
        self._inspect_signature_aliases: dict = {}
        self._inspect_fullargspec_aliases: dict = {}
        self._native_module_attr_globals: dict[
            tuple[str, str],
            ir.GlobalVariable,
        ] = {}
        self._native_file_values: set = set()
        self._native_file_env_flags: dict[str, bool] = {}
        self._native_fileinput_values: set = set()
        self._native_fileinput_env_flags: dict[str, bool] = {}
        self._threading_env_flags: dict[str, str] = {}
        self._threading_list_elem_flags: dict[str, str] = {}
        self._thread_safepoints_enabled = (
            str(os.environ.get("PCC_WITH_THREADS", "")).strip().lower()
            in ("1", "true", "yes", "on")
        )
        self._runtime_threads_enabled = (
            self._thread_safepoints_enabled or self._module_imports_threading(module)
        )
        self._weak_dict_env_flags: dict[str, str] = {}
        self._weakref_env_flags: dict[str, bool] = {}
        self._cpy_env_flags: dict[str, bool] = {}
        self._cross_module_func_defs: dict[str, FuncDef] = {}
        self._cross_module_identity_decorators: dict[str, bool] = {}
        self._cross_module_semantic_functions: dict[str, tuple[str, str]] = {}
        self._module_block_func_defs: dict[str, FuncDef] = {}
        self._module_block_funcdef_ids: set[int] = set()
        self._owned_local_names: set[str] = set()
        self._owned_local_has_value: set[str] = set()
        self._owned_local_flag_slots: dict[str, ir.Value] = {}
        self._owned_local_flag_allocas: dict[str, ir.Value] = {}
        self._for_target_owned_names: set[str] = set()
        self._gc_rooted_local_names: set[str] = set()
        self._gc_rooted_local_order: list[str] = []
        self._borrowed_gc_rooted_local_names: set[str] = set()
        self._pinned_gc_rooted_local_names: set[str] = set()
        self._container_temp_root_slot_names: list[str] = []
        self._lambda_lexical_shadow_names: set[str] = set()
        self._suppress_implicit_gc_roots = False
        self._suppress_borrowed_return_retain = False
        # Names bound by `except ... as <name>`. A local assigned the value of
        # one of these (e.g. `saved = e`) borrows a caught exception whose only
        # surviving reference is that local, so it must be GC-rooted or the
        # tracing collect sweeps the exception's referents. See
        # gc-5backend-exception-referent-roots-no-libpython.md.
        self._except_binding_names: set[str] = set()
        self._current_param_names: set[str] = set()
        self._unboxed_typed_int_abi_cache: dict[str, bool] = {}
        self._typed_int_abi_call_arg_safety: list[tuple[str, list[bool]]] = []
        self._bounded_int_abi_function_names: list[str] = []
        self._hoisted_capture_params = {}
        self._hoisted_class_capture_params = {}
        self._hoisted_enclosing_class = {}
        self._hoisted_enclosing_method_kind = {}
        self._closure_boxed_params = {}
        self._hoist_wrap_caps = {}


def _module_declares_runtime_port(module) -> bool:
    """Mirror of ``type_infer._InferCtx``: a module-scope
    ``__pcc_runtime_port__ = True`` assignment marks a runtime port."""
    for stmt in getattr(module, "body", ()) or ():
        if type(stmt).__name__ != "Assign":
            continue
        value = getattr(stmt, "value", None)
        if type(value).__name__ != "BoolLit" or not getattr(value, "value", False):
            continue
        for target in getattr(stmt, "targets", ()):
            if getattr(target, "ident", None) == "__pcc_runtime_port__":
                return True
    return False
