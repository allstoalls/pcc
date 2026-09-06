"""Auto-generated pure-data static method entries for L1CodeGen.

DO NOT EDIT BY HAND.  Regenerate via
``scripts/regen_l1_codegen_static_methods.py`` after changing
``host_contract.L1_CODEGEN_HOST_METHODS`` or a mixin source
signature.

This file lives in the no-libpython bootstrap closure for pcc1,
so it stays restricted to tuples, lists, dicts, string literals,
and the eager chunk functions below.

The generated source stores compact parameter triples and inflates
the original tuple-of-dicts schema eagerly.  Repeating that schema
inline made the previous parts total tens of megabytes of IR.
Chunk functions append into one list, avoiding repeated tuple
concatenation while keeping each lowering unit bounded.
"""
from __future__ import annotations

_DYN_TYPE = ('dyn',)

def _append_method(out, method_name, param_specs):
    param_types = []
    call_sig = []
    for param_spec in param_specs:
        param_name = param_spec[0]
        param_kind = param_spec[1]
        has_default = param_spec[2]
        call_sig.append({
            'name': param_name,
            'kind': param_kind,
            'annotation': _DYN_TYPE,
            'default': None,
            'has_default': has_default,
        })
        if param_kind != 'kw_only':
            param_types.append(_DYN_TYPE)
    out.append({
        'name': method_name,
        'kind': 'instance',
        'return_ty': _DYN_TYPE,
        'param_types': tuple(param_types),
        'call_sig': tuple(call_sig),
        'box_int_abi': False,
    })

def _part_0(out):
    _append_method(out, '_alloca_in_entry', (('self', 'pos', False), ('ir_ty', 'pos', False), ('name', 'pos', False), ('', 'kw_only', False), ('init_null', 'pos', True)))
    _append_method(out, '_as_gc_ptr', (('self', 'pos', False), ('value', 'pos', False), ('', 'kw_only', False), ('name', 'pos', True)))
    _append_method(out, '_attr_expr_returns_owned_object', (('self', 'pos', False), ('expr', 'pos', False)))
    _append_method(out, '_builder_block_is_terminated', (('self', 'pos', False),))
    _append_method(out, '_call_user', (('self', 'pos', False), ('fn', 'pos', False), ('args_ir', 'pos', False), ('call_name', 'pos', False), ('span', 'pos', True), ('root_result', 'pos', True), ('pinned_arg_temps', 'pos', True)))
    _append_method(out, '_callable_expr_returns_cpython', (('self', 'pos', False), ('expr', 'pos', False)))
    _append_method(out, '_class_attr_descriptor_class', (('self', 'pos', False), ('class_name', 'pos', False), ('attr_name', 'pos', False)))
    _append_method(out, '_class_hint_for_expr', (('self', 'pos', False), ('expr', 'pos', False)))


def _part_1(out):
    _append_method(out, '_class_hint_from_annotation', (('self', 'pos', False), ('ann', 'pos', False)))
    _append_method(out, '_coerce', (('self', 'pos', False), ('v', 'pos', False), ('from_ty', 'pos', False), ('to_ty', 'pos', False)))
    _append_method(out, '_collect_explicit_global_names', (('self', 'pos', False), ('stmts', 'pos', False)))
    _append_method(out, '_collect_return_exprs', (('self', 'pos', False), ('stmts', 'pos', False)))
    _append_method(out, '_container_store_temp_needs_release', (('self', 'pos', False), ('expr', 'pos', False), ('value_ty', 'pos', False), ('is_cpy', 'pos', False)))
    _append_method(out, '_pcc_pointer_source_needs_pin', (('self', 'pos', False), ('expr', 'pos', False)))
    _append_method(out, '_cpy_module_global', (('self', 'pos', False), ('local_name', 'pos', False)))
    _append_method(out, '_cpy_modules', (('self', 'pos', False),))


def _part_2(out):
    _append_method(out, '_cstr_global', (('self', 'pos', False), ('payload', 'pos', False), ('name', 'pos', False)))
    _append_method(out, '_cstr_literal', (('self', 'pos', False), ('payload', 'pos', False)))
    _append_method(out, '_current_try_err_block', (('self', 'pos', False),))
    _append_method(out, '_debug_check_release', (('self', 'pos', False), ('obj', 'pos', False), ('label', 'pos', False)))
    _append_method(out, '_declare_external_function', (('self', 'pos', False), ('name', 'pos', False), ('ret_ty', 'pos', False), ('param_tys', 'pos', False), ('', 'kw_only', False), ('var_arg', 'pos', True)))
    _append_method(out, '_declare_module_globals_for', (('self', 'pos', False), ('stmt', 'pos', False)))
    _append_method(out, '_declare_printf', (('self', 'pos', False),))
    _append_method(out, '_decorator_c_abi_export_symbol', (('self', 'pos', False), ('dec', 'pos', False)))


def _part_3(out):
    _append_method(out, '_decorator_c_abi_typed_signature', (('self', 'pos', False), ('dec', 'pos', False)))
    _append_method(out, '_decorator_is_noop_whitelist', (('self', 'pos', False), ('dec', 'pos', False)))
    _append_method(out, '_decorator_is_runtime_partial_factory', (('self', 'pos', False), ('dec', 'pos', False)))
    _append_method(out, '_decorator_qualname', (('self', 'pos', False), ('dec', 'pos', False)))
    _append_method(out, '_decorator_repr', (('self', 'pos', False), ('dec', 'pos', False)))
    _append_method(out, '_discard_owned_local_gc_root', (('self', 'pos', False), ('name', 'pos', False), ('alloca', 'pos', False)))
    _append_method(out, '_emit_as_object', (('self', 'pos', False), ('expr', 'pos', False)))
    _append_method(out, '_emit_arg_for_abi_param', (('self', 'pos', False), ('ast_arg', 'pos', False), ('target_ty', 'pos', False), ('param_ir_ty', 'pos', False)))


def _part_4(out):
    _append_method(out, '_emit_arg_for_abi_param_with_cleanup', (('self', 'pos', False), ('ast_arg', 'pos', False), ('target_ty', 'pos', False), ('param_ir_ty', 'pos', False), ('pinned_pcc', 'pos', False)))
    _append_method(out, '_emit_builtin_runtime_isinstance', (('self', 'pos', False), ('obj_expr', 'pos', False), ('class_ident', 'pos', False), ('obj_val', 'pos', True)))
    _append_method(out, '_emit_call_args_tuple', (('self', 'pos', False), ('args', 'pos', False)))
    _append_method(out, '_emit_class_global_root_enters', (('self', 'pos', False),))
    _append_method(out, '_emit_comprehension_generator', (('self', 'pos', False), ('kind', 'pos', False), ('container', 'pos', False), ('generators', 'pos', False), ('tuple_unpacks', 'pos', False), ('idx', 'pos', False), ('elt_expr', 'pos', False), ('key_expr', 'pos', False), ('val_expr', 'pos', False)))
    _append_method(out, '_emit_comprehension_innermost', (('self', 'pos', False), ('kind', 'pos', False), ('container', 'pos', False), ('elt_expr', 'pos', False), ('key_expr', 'pos', False), ('val_expr', 'pos', False)))
    _append_method(out, '_emit_coroutine_from_adapter', (('self', 'pos', False), ('display_name', 'pos', False), ('adapter', 'pos', False), ('args_tuple', 'pos', False), ('captures_tuple', 'pos', True)))
    _append_method(out, '_emit_cpy_attr', (('self', 'pos', False), ('obj_val', 'pos', False), ('name', 'pos', False)))


def _part_5(out):
    _append_method(out, '_emit_cpy_attr_with_cleanup', (('self', 'pos', False), ('obj_val', 'pos', False), ('attr_name', 'pos', False), ('live_owned', 'pos', False), ('rooted_pcc', 'pos', True), ('pinned_pcc', 'pos', True)))
    _append_method(out, '_emit_cpy_method_call1_value', (('self', 'pos', False), ('mod_val', 'pos', False), ('attr_name', 'pos', False), ('arg_val', 'pos', False), ('', 'kw_only', False), ('arg_owned', 'pos', False), ('receiver_owned', 'pos', False)))
    _append_method(out, '_emit_cpython_dict_items', (('self', 'pos', False), ('items', 'pos', False), ('pinned_pcc', 'pos', True)))
    _append_method(out, '_emit_cpython_list_ops', (('self', 'pos', False), ('ops', 'pos', False), ('pinned_pcc', 'pos', True)))
    _append_method(out, '_emit_cpython_tuple_ops', (('self', 'pos', False), ('ops', 'pos', False), ('pinned_pcc', 'pos', True)))
    _append_method(out, '_emit_current_gc_frame_enter', (('self', 'pos', False), ('frame_map', 'pos', False), ('slots', 'pos', False)))
    _append_method(out, '_emit_current_gc_frame_enter_lifo', (('self', 'pos', False), ('frame_map', 'pos', False), ('slots', 'pos', False)))
    _append_method(out, '_emit_dict_literal', (('self', 'pos', False), ('expr', 'pos', False)))


def _part_6(out):
    _append_method(out, '_emit_dynamic_call_args_tuple', (('self', 'pos', False), ('args', 'pos', False)))
    _append_method(out, '_emit_virtual_thread_container_call_check', (('self', 'pos', False), ('span', 'pos', False), ('container_roots', 'pos', False)))
    _append_method(out, '_emit_virtual_thread_dynamic_args_with_roots', (('self', 'pos', False), ('args', 'pos', False), ('roots', 'pos', False)))
    _append_method(out, '_emit_virtual_thread_rooted_args_tuple', (('self', 'pos', False), ('args', 'pos', False)))
    _append_method(out, '_emit_dynamic_call_kwargs_object', (('self', 'pos', False), ('kwargs', 'pos', False), ('kwargs_expr', 'pos', False), ('span', 'pos', False)))
    _append_method(out, '_binop_route_defers_pins', (('self', 'pos', False), ('op', 'pos', False), ('lhs_ty', 'pos', False), ('rhs_ty', 'pos', False), ('result_ty', 'pos', False)))
    _append_method(out, '_emit_binop_value_routed', (('self', 'pos', False), ('op', 'pos', False), ('lhs', 'pos', False), ('lhs_ty', 'pos', False), ('rhs', 'pos', False), ('rhs_ty', 'pos', False), ('result_ty', 'pos', False), ('pinned_pcc_on_error', 'pos', True), ('slow_pins', 'pos', True)))
    _append_method(out, '_emit_dyn_tagged_int_object_binop', (('self', 'pos', False), ('op', 'pos', False), ('lhs', 'pos', False), ('lhs_ty', 'pos', False), ('rhs', 'pos', False), ('rhs_ty', 'pos', False), ('', 'kw_only', False), ('pinned_pcc_on_error', 'pos', True), ('slow_pins', 'pos', True)))


def _part_7(out):
    _append_method(out, '_emit_empty_tuple', (('self', 'pos', False), ('name', 'pos', False)))
    _append_method(out, '_emit_entry_gc_frame_enter', (('self', 'pos', False), ('frame_map', 'pos', False), ('slots', 'pos', False)))
    _append_method(out, '_emit_post_call_error_cleanup', (('self', 'pos', False), ('error_dest', 'pos', False), ('', 'kw_only', False), ('release_on_error', 'pos', False), ('cpy_release_on_error', 'pos', False), ('rooted_release_on_error', 'pos', False), ('pinned_release_on_error', 'pos', False), ('lifo_owned_root_slots_on_error', 'pos', False), ('landing_slot', 'pos', True), ('payload', 'pos', True)))
    _append_method(out, '_direct_frame_landing', (('self', 'pos', False), ('err_target', 'pos', False), ('span', 'pos', False)))
    _append_method(out, '_finalize_traceback_index_tables', (('self', 'pos', False),))
    _append_method(out, '_traceback_index_for', (('self', 'pos', False), ('span', 'pos', False)))
    _append_method(out, '_traceback_source_text', (('self', 'pos', False), ('span', 'pos', False)))
    _append_method(out, '_emit_exact_container_subscript_load_object', (('self', 'pos', False), ('expr', 'pos', False), ('obj', 'pos', False)))


def _part_8(out):
    _append_method(out, '_emit_exact_int_compare', (('self', 'pos', False), ('expr', 'pos', False)))
    _append_method(out, '_emit_exact_int_operand_object', (('self', 'pos', False), ('expr', 'pos', False)))
    _append_method(out, '_emit_expr', (('self', 'pos', False), ('expr', 'pos', False)))
    _append_method(out, '_emit_expr_as_i64', (('self', 'pos', False), ('expr', 'pos', False)))
    _append_method(out, '_emit_expr_as_pcc_object', (('self', 'pos', False), ('expr', 'pos', False)))
    _append_method(out, '_emit_expr_stmt', (('self', 'pos', False), ('stmt', 'pos', False)))
    _append_method(out, '_emit_expr_with_native_callable_values', (('self', 'pos', False), ('expr', 'pos', False)))
    _append_method(out, '_emit_gc_frame_leave_for_slot', (('self', 'pos', False), ('alloca', 'pos', False)))


def _part_9(out):
    _append_method(out, '_emit_gc_frame_leave_lifo_for_slot', (('self', 'pos', False), ('alloca', 'pos', False)))
    _append_method(out, '_enter_container_temp_root', (('self', 'pos', False), ('value', 'pos', False), ('label', 'pos', False)))
    _append_method(out, '_emit_inline_tagged_int_binop_or_call', (('self', 'pos', False), ('op', 'pos', False), ('lhs_obj', 'pos', False), ('rhs_obj', 'pos', False), ('fn_name', 'pos', False), ('', 'kw_only', False), ('slow_pins', 'pos', True), ('slow_err_check', 'pos', True), ('slow_err_cleanup', 'pos', True)))
    _append_method(out, '_emit_ir_scaffold_isinstance', (('self', 'pos', False), ('obj_val', 'pos', False), ('class_name', 'pos', False)))
    _append_method(out, '_emit_isinstance_call', (('self', 'pos', False), ('expr', 'pos', False)))
    _append_method(out, '_emit_runtime_object_compare', (('self', 'pos', False), ('expr', 'pos', False), ('lhs_obj', 'pos', False), ('rhs_obj', 'pos', False), ('name_prefix', 'pos', False)))
    _append_method(out, '_emit_module_global_root_enters', (('self', 'pos', False),))
    _append_method(out, '_emit_module_root_enters', (('self', 'pos', False),))


def _part_10(out):
    _append_method(out, '_emit_module_teardown', (('self', 'pos', False),))
    _append_method(out, '_emit_module_teardown_call', (('self', 'pos', False), ('module_name', 'pos', True)))
    _append_method(out, '_emit_module_top_init', (('self', 'pos', False), ('body', 'pos', False)))
    _append_method(out, '_emit_static_literal_init_call', (('self', 'pos', False),))
    _append_method(out, '_finalize_static_literal_init', (('self', 'pos', False),))
    _append_method(out, '_static_literal_init_function', (('self', 'pos', False),))
    _append_method(out, '_emit_method_arg_as_pcc_object', (('self', 'pos', False), ('expr', 'pos', False)))
    _append_method(out, '_emit_name', (('self', 'pos', False), ('expr', 'pos', False)))


def _part_11(out):
    _append_method(out, '_emit_native_builtin_callable_type_error', (('self', 'pos', False), ('builder', 'pos', False), ('message', 'pos', False), ('name', 'pos', False), ('suffix', 'pos', False)))
    _append_method(out, '_emit_native_builtin_callable_value', (('self', 'pos', False), ('name', 'pos', False)))
    _append_method(out, '_emit_native_func_adapter', (('self', 'pos', False), ('orig_name', 'pos', False), ('full_fn', 'pos', False), ('original_args', 'pos', False), ('free_names', 'pos', False), ('return_ty', 'pos', False)))
    _append_method(out, '_emit_native_func_value', (('self', 'pos', False), ('orig_name', 'pos', False), ('resolved_name', 'pos', False), ('full_fn', 'pos', False), ('free_names', 'pos', False)))
    _append_method(out, '_emit_native_os_environ_setitem_store', (('self', 'pos', False), ('target', 'pos', False), ('value_expr', 'pos', False)))
    _append_method(out, '_emit_native_os_environ_setitem_value', (('self', 'pos', False), ('target', 'pos', False), ('value', 'pos', False), ('value_ty', 'pos', False), ('', 'kw_only', False), ('release_value', 'pos', True)))
    _append_method(out, '_emit_native_os_environ_subscript', (('self', 'pos', False), ('expr', 'pos', False)))
    _append_method(out, '_emit_native_os_call', (('self', 'pos', False), ('expr', 'pos', False)))


def _part_12(out):
    _append_method(out, '_emit_virtual_thread_callback_call', (('self', 'pos', False), ('expr', 'pos', False), ('args', 'pos', False), ('kwargs', 'pos', False)))
    _append_method(out, '_emit_virtual_thread_current_fd_wait', (('self', 'pos', False), ('fd', 'pos', False), ('events', 'pos', False), ('timeout_ms', 'pos', False)))
    _append_method(out, '_emit_virtual_thread_tcp_abandon_state', (('self', 'pos', False), ('frame_root', 'pos', False), ('close_progress', 'pos', False)))
    _append_method(out, '_emit_virtual_thread_tcp_accept', (('self', 'pos', False), ('args', 'pos', False), ('call_expr', 'pos', False)))
    _append_method(out, '_emit_virtual_thread_tcp_arg_i64', (('self', 'pos', False), ('frame_root', 'pos', False), ('index', 'pos', False)))
    _append_method(out, '_emit_virtual_thread_tcp_arg_item', (('self', 'pos', False), ('frame_root', 'pos', False), ('index', 'pos', False)))
    _append_method(out, '_emit_virtual_thread_tcp_cleanup_block', (('self', 'pos', False), ('frame_root', 'pos', False), ('close_progress', 'pos', False)))
    _append_method(out, '_emit_virtual_thread_tcp_capture_generation', (('self', 'pos', False), ('frame_root', 'pos', False), ('fd', 'pos', False), ('span', 'pos', False)))


def _part_13(out):
    _append_method(out, '_emit_virtual_thread_tcp_clear_state', (('self', 'pos', False), ('frame_root', 'pos', False)))
    _append_method(out, '_emit_virtual_thread_tcp_close', (('self', 'pos', False), ('args', 'pos', False), ('call_expr', 'pos', False)))
    _append_method(out, '_emit_virtual_thread_tcp_connect', (('self', 'pos', False), ('args', 'pos', False), ('call_expr', 'pos', False)))
    _append_method(out, '_emit_virtual_thread_tcp_listen', (('self', 'pos', False), ('args', 'pos', False), ('call_expr', 'pos', False)))
    _append_method(out, '_emit_virtual_thread_tcp_owned_i64', (('self', 'pos', False), ('value', 'pos', False)))
    _append_method(out, '_emit_virtual_thread_tcp_park_retry', (('self', 'pos', False), ('fd', 'pos', False), ('generation', 'pos', False), ('events', 'pos', False), ('deadline', 'pos', False), ('frame_root', 'pos', False), ('cleanup', 'pos', False), ('outer_error', 'pos', False), ('close_progress', 'pos', False), ('retry', 'pos', False)))
    _append_method(out, '_emit_virtual_thread_tcp_recv', (('self', 'pos', False), ('args', 'pos', False), ('call_expr', 'pos', False)))
    _append_method(out, '_emit_virtual_thread_tcp_send_all', (('self', 'pos', False), ('args', 'pos', False), ('call_expr', 'pos', False)))


def _part_14(out):
    _append_method(out, '_emit_virtual_thread_tcp_set_progress', (('self', 'pos', False), ('frame_root', 'pos', False), ('progress', 'pos', False), ('span', 'pos', False)))
    _append_method(out, '_emit_virtual_thread_tcp_set_state_i64', (('self', 'pos', False), ('frame_root', 'pos', False), ('index', 'pos', False), ('value', 'pos', False), ('label', 'pos', False), ('span', 'pos', False)))
    _append_method(out, '_emit_virtual_thread_tcp_state', (('self', 'pos', False), ('call_expr', 'pos', False), ('kind', 'pos', False), ('args', 'pos', False), ('timeout_index', 'pos', False), ('initial_progress', 'pos', False)))
    _append_method(out, '_emit_virtual_thread_tcp_state_i64', (('self', 'pos', False), ('frame_root', 'pos', False), ('index', 'pos', False)))
    _append_method(out, '_emit_virtual_thread_tcp_state_item', (('self', 'pos', False), ('frame_root', 'pos', False), ('index', 'pos', False)))
    _append_method(out, '_emit_virtual_thread_tcp_status_guard', (('self', 'pos', False), ('status', 'pos', False), ('expected', 'pos', False), ('label', 'pos', False)))
    _append_method(out, '_enter_virtual_thread_operand_root', (('self', 'pos', False), ('value', 'pos', False), ('source_expr', 'pos', False), ('label', 'pos', False)))
    _append_method(out, '_emit_native_virtual_thread_call', (('self', 'pos', False), ('expr', 'pos', False)))


def _part_15(out):
    _append_method(out, '_emit_native_virtual_thread_value_call', (('self', 'pos', False), ('kind', 'pos', False), ('args', 'pos', False), ('kwargs', 'pos', False), ('call_expr', 'pos', True)))
    _append_method(out, '_load_virtual_thread_operand_root', (('self', 'pos', False), ('root', 'pos', False)))
    _append_method(out, '_release_virtual_thread_argument', (('self', 'pos', False), ('obj', 'pos', False), ('source_expr', 'pos', False)))
    _append_method(out, '_release_rooted_pcc_lifetimes', (('self', 'pos', False), ('roots', 'pos', False)))
    _append_method(out, '_emit_none_literal', (('self', 'pos', False),))
    _append_method(out, '_emit_object_tuple_from_values', (('self', 'pos', False), ('values', 'pos', False), ('', 'kw_only', False), ('name', 'pos', False)))
    _append_method(out, '_emit_operator_getter', (('self', 'pos', False), ('getter_name', 'pos', False), ('key', 'pos', False)))
    _append_method(out, '_emit_owned_local_cleanup', (('self', 'pos', False), ('skip_name', 'pos', True)))


def _part_16(out):
    _append_method(out, '_emit_pcc_args_list', (('self', 'pos', False), ('arg_exprs', 'pos', False), ('name_hint', 'pos', False), ('cpy_live_owned', 'pos', True), ('cpy_temp_root_out', 'pos', True)))
    _append_method(out, '_emit_post_call_err_check', (('self', 'pos', False), ('span', 'pos', True), ('', 'kw_only', False), ('release_on_error', 'pos', True), ('cpy_release_on_error', 'pos', True), ('rooted_release_on_error', 'pos', True), ('pinned_release_on_error', 'pos', True), ('lifo_owned_root_slots_on_error', 'pos', True)))
    _append_method(out, '_emit_print_float_value', (('self', 'pos', False), ('value', 'pos', False)))
    _append_method(out, '_emit_print_many', (('self', 'pos', False), ('call', 'pos', False)))
    _append_method(out, '_emit_program_main', (('self', 'pos', False), ('body', 'pos', False)))
    _append_method(out, '_emit_release_owned_local_if_flagged', (('self', 'pos', False), ('name', 'pos', False), ('alloca', 'pos', False)))
    _append_method(out, '_emit_stmts', (('self', 'pos', False), ('stmts', 'pos', False)))
    _append_method(out, '_emit_strict_no_libpython_import_error', (('self', 'pos', False), ('module_name', 'pos', False), ('span', 'pos', False)))


def _part_17(out):
    _append_method(out, '_emit_str_literal', (('self', 'pos', False), ('value', 'pos', False)))
    _append_method(out, '_emit_unary', (('self', 'pos', False), ('expr', 'pos', False)))
    _append_method(out, '_emit_value_as_pcc_object_or_bridge', (('self', 'pos', False), ('value', 'pos', False), ('value_ty', 'pos', False), ('name_hint', 'pos', False), ('', 'kw_only', False), ('consume_valueclass_payload_fields', 'pos', True), ('cpy_owned_on_error', 'pos', True), ('rooted_pcc_on_error', 'pos', True), ('pinned_pcc_on_error', 'pos', True), ('pcc_release_on_error', 'pos', True)))
    _append_method(out, '_emit_value_array_subscript_load', (('self', 'pos', False), ('expr', 'pos', False)))
    _append_method(out, '_emit_virtual_thread_resume_function', (('self', 'pos', False), ('name', 'pos', False), ('fn', 'pos', False), ('ast_func_def', 'pos', False), ('n_args', 'pos', False)))
    _append_method(out, '_emit_virtual_thread_spawn', (('self', 'pos', False), ('args', 'pos', False), ('kwargs', 'pos', False)))
    _append_method(out, '_emit_walrus', (('self', 'pos', False), ('expr', 'pos', False)))
    _append_method(out, '_ensure_class_type_registered', (('self', 'pos', False), ('ty', 'pos', False)))


def _part_18(out):
    _append_method(out, '_ensure_fn_err_exit', (('self', 'pos', False),))
    _append_method(out, '_ensure_module_global_name', (('self', 'pos', False), ('name', 'pos', False), ('target_ty', 'pos', False)))
    _append_method(out, '_ensure_native_module_alias_class_export', (('self', 'pos', False), ('alias_name', 'pos', False), ('attr_name', 'pos', False)))
    _append_method(out, '_ensure_borrowed_local_gc_root', (('self', 'pos', False), ('name', 'pos', False), ('alloca', 'pos', False), ('ir_ty', 'pos', False)))
    _append_method(out, '_ensure_local_gc_frame_root', (('self', 'pos', False), ('name', 'pos', False), ('alloca', 'pos', False), ('ir_ty', 'pos', False), ('frame_map', 'pos', True)))
    _append_method(out, '_ensure_owned_local_flag', (('self', 'pos', False), ('name', 'pos', False), ('alloca', 'pos', True)))
    _append_method(out, '_ensure_owned_local_gc_root', (('self', 'pos', False), ('name', 'pos', False), ('alloca', 'pos', False), ('ir_ty', 'pos', False)))
    _append_method(out, '_owned_local_flag_for', (('self', 'pos', False), ('name', 'pos', False), ('alloca', 'pos', True)))


def _part_19(out):
    _append_method(out, '_expr_looks_cpython', (('self', 'pos', False), ('expr', 'pos', False)))
    _append_method(out, '_expr_returns_owned_object', (('self', 'pos', False), ('expr', 'pos', False)))
    _append_method(out, '_pcc_pointer_source_is_owned', (('self', 'pos', False), ('expr', 'pos', False)))
    _append_method(out, '_expr_returns_unsafe_raw_pointer', (('self', 'pos', False), ('expr', 'pos', False)))
    _append_method(out, '_find_user_funcdef', (('self', 'pos', False), ('name', 'pos', False)))
    _append_method(out, '_finish_cpy_call_kw', (('self', 'pos', False), ('fn_val', 'pos', False), ('name_hint', 'pos', False), ('pos_exprs', 'pos', False), ('kwargs', 'pos', False), ('operand_order', 'pos', True)))
    _append_method(out, '_fresh', (('self', 'pos', False), ('hint', 'pos', True)))
    _append_method(out, '_func_decorators', (('self', 'pos', False), ('fd', 'pos', False)))


def _part_20(out):
    _append_method(out, '_funcdef_has_yield_sentinel', (('self', 'pos', False), ('fd', 'pos', False)))
    _append_method(out, '_funcdef_is_continuation_factory', (('self', 'pos', False), ('fd', 'pos', False)))
    _append_method(out, '_funcdef_uses_boxed_int_abi', (('self', 'pos', False), ('fd', 'pos', False), ('', 'kw_only', False), ('c_abi_sym', 'pos', False)))
    _append_method(out, '_function_arg_ir_type_or_none', (('self', 'pos', False), ('fn', 'pos', False), ('index', 'pos', False), ('ir_arg', 'pos', False)))
    _append_method(out, '_gc_one_slot_borrowed_frame_map', (('self', 'pos', False),))
    _append_method(out, '_gc_one_slot_frame_map', (('self', 'pos', False),))
    _append_method(out, '_gc_release', (('self', 'pos', False), ('obj', 'pos', False), ('label', 'pos', True)))
    _append_method(out, '_gc_pin', (('self', 'pos', False), ('obj', 'pos', False)))


def _part_21(out):
    _append_method(out, '_gc_unpin', (('self', 'pos', False), ('obj', 'pos', False)))
    _append_method(out, '_note_global_backed_value', (('self', 'pos', False), ('value', 'pos', False), ('source', 'pos', False)))
    _append_method(out, '_value_available_at_insertion_point', (('self', 'pos', False), ('value', 'pos', False)))
    _append_method(out, '_gc_release_if_owned', (('self', 'pos', False), ('obj', 'pos', False), ('source_expr', 'pos', False)))
    _append_method(out, '_note_never_gc_object', (('self', 'pos', False), ('value', 'pos', False)))
    _append_method(out, '_value_is_never_gc_object', (('self', 'pos', False), ('value', 'pos', False)))
    _append_method(out, '_gc_retain', (('self', 'pos', False), ('obj', 'pos', False), ('name', 'pos', True)))
    _append_method(out, '_get_floor_intrinsic', (('self', 'pos', False),))


def _part_22(out):
    _append_method(out, '_get_fmt_bool_false', (('self', 'pos', False),))
    _append_method(out, '_get_fmt_bool_true', (('self', 'pos', False),))
    _append_method(out, '_get_fmt_float', (('self', 'pos', False),))
    _append_method(out, '_get_fmt_int', (('self', 'pos', False),))
    _append_method(out, '_has_starred_unpack', (('self', 'pos', False), ('arg_exprs', 'pos', False)))
    _append_method(out, '_init_l1_state', (('self', 'pos', False), ('module', 'pos', False), ('emit_cpy_main_exitcode', 'pos', False), ('ir_scaffold_mode', 'pos', False)))
    _append_method(out, '_instruction_is_terminator', (('self', 'pos', False), ('instr', 'pos', False)))
    _append_method(out, '_instruction_opname_text', (('self', 'pos', False), ('instr', 'pos', False)))


def _part_23(out):
    _append_method(out, '_int_expr_needs_exact_object_boundary', (('self', 'pos', False), ('expr', 'pos', False)))
    _append_method(out, '_int_exprs_are_boxed', (('self', 'pos', False),))
    _append_method(out, '_ir_module_symbol_target', (('self', 'pos', False), ('attr', 'pos', False)))
    _append_method(out, '_ir_scaffold_class_symbol', (('self', 'pos', False), ('expr', 'pos', False)))
    _append_method(out, '_ir_scaffold_enabled', (('self', 'pos', False),))
    _append_method(out, '_ir_type_matches', (('self', 'pos', False), ('actual', 'pos', False), ('expected', 'pos', False)))
    _append_method(out, '_is_extern_scaffold_import_module', (('self', 'pos', False), ('module_name', 'pos', False)))
    _append_method(out, '_is_object', (('self', 'pos', False), ('ty', 'pos', False)))


def _part_24(out):
    _append_method(out, '_is_scalar', (('self', 'pos', False), ('ty', 'pos', False)))
    _append_method(out, '_is_starred_unpack', (('self', 'pos', False), ('arg_exprs', 'pos', False)))
    _append_method(out, '_is_starred_unpack_expr', (('self', 'pos', False), ('arg', 'pos', False)))
    _append_method(out, '_is_test_facade_import_module', (('self', 'pos', False), ('module_name', 'pos', False)))
    _append_method(out, '_join_reversed_strs', (('self', 'pos', False), ('parts', 'pos', False)))
    _append_method(out, '_lambda_attr_chain', (('self', 'pos', False), ('expr', 'pos', False), ('param_name', 'pos', False)))
    _append_method(out, '_lambda_method_call', (('self', 'pos', False), ('expr', 'pos', False), ('param_name', 'pos', False)))
    _append_method(out, '_lambda_simple_subscript', (('self', 'pos', False), ('expr', 'pos', False), ('param_name', 'pos', False)))


def _part_25(out):
    _append_method(out, '_load_cpython_builtin', (('self', 'pos', False), ('name', 'pos', False)))
    _append_method(out, '_map_type', (('self', 'pos', False), ('ty', 'pos', False)))
    _append_method(out, '_mark_cpy_value', (('self', 'pos', False), ('value', 'pos', False)))
    _append_method(out, '_mark_owned_cpy_value', (('self', 'pos', False), ('value', 'pos', False)))
    _append_method(out, '_cpy_value_is_owned', (('self', 'pos', False), ('value', 'pos', False)))
    _append_method(out, '_forget_owned_cpy_value', (('self', 'pos', False), ('value', 'pos', False)))
    _append_method(out, '_release_cpy_callable_if_owned', (('self', 'pos', False), ('fn_val', 'pos', False)))
    _append_method(out, '_guard_cpy_value_not_null', (('self', 'pos', False), ('value', 'pos', False), ('owned_on_error', 'pos', True), ('rooted_pcc_on_error', 'pos', True), ('pinned_pcc_on_error', 'pos', True), ('pcc_release_on_error', 'pos', True)))


def _part_26(out):
    _append_method(out, '_guard_cpy_status_not_negative', (('self', 'pos', False), ('status', 'pos', False), ('owned_on_error', 'pos', True), ('rooted_pcc_on_error', 'pos', True), ('pinned_pcc_on_error', 'pos', True), ('pcc_release_on_error', 'pos', True)))
    _append_method(out, '_cpy_literal_cleanup_values', (('self', 'pos', False), ('container', 'pos', False), ('first_callable', 'pos', False), ('second_callable', 'pos', False), ('pending_owned', 'pos', False), ('extra_owned', 'pos', True)))
    _append_method(out, '_load_cpython_builtin_with_cleanup', (('self', 'pos', False), ('name', 'pos', False), ('live_owned', 'pos', False), ('pinned_pcc', 'pos', True)))
    _append_method(out, '_require_supported_cpy_kw_mapping', (('self', 'pos', False), ('kwargs_expr', 'pos', False)))
    _append_method(out, '_leave_container_temp_root', (('self', 'pos', False), ('slot', 'pos', False)))
    _append_method(out, '_make_cpy_operand_cleanup_block', (('self', 'pos', False), ('live_owned', 'pos', False), ('rooted_pcc', 'pos', False), ('target', 'pos', False), ('name', 'pos', False), ('pinned_pcc', 'pos', True), ('rooted_pcc_lifetimes', 'pos', True)))
    _append_method(out, '_emit_expr_with_cpy_operand_cleanup', (('self', 'pos', False), ('expr', 'pos', False), ('live_owned', 'pos', False), ('rooted_pcc', 'pos', True), ('pinned_pcc', 'pos', True), ('as_pcc_object', 'pos', True), ('as_object', 'pos', True), ('as_i64', 'pos', True), ('rooted_pcc_lifetimes', 'pos', True)))
    _append_method(out, '_begin_cpy_operand_evaluation', (('self', 'pos', False), ('fn_val', 'pos', False)))


def _part_27(out):
    _append_method(out, '_emit_checked_cpython_call_arg', (('self', 'pos', False), ('expr', 'pos', False), ('live_owned', 'pos', False), ('rooted_pcc_on_error', 'pos', True)))
    _append_method(out, '_bridge_cpy_arglist_operand', (('self', 'pos', False), ('value', 'pos', False), ('cpy_live_owned', 'pos', False), ('rooted_pcc_on_error', 'pos', True)))
    _append_method(out, '_mark_owned_local_for_unpack_target', (('self', 'pos', False), ('target', 'pos', False), ('value_ty', 'pos', False), ('value_is_owned', 'pos', True)))
    _append_method(out, '_mark_owned_local_if_object', (('self', 'pos', False), ('name', 'pos', False), ('ir_ty', 'pos', False), ('expr', 'pos', True)))
    _append_method(out, '_marshal_to_cpython', (('self', 'pos', False), ('v', 'pos', False), ('ty', 'pos', False)))
    _append_method(out, '_marshal_to_cpython_consuming_source', (('self', 'pos', False), ('value', 'pos', False), ('value_ty', 'pos', False), ('source_expr', 'pos', False), ('cpy_owned_on_error', 'pos', True), ('rooted_pcc_on_error', 'pos', True), ('pinned_pcc_on_error', 'pos', True), ('pcc_release_on_error', 'pos', True)))
    _append_method(out, '_maybe_emit_builtin_type_method', (('self', 'pos', False), ('expr', 'pos', False)))
    _append_method(out, '_maybe_emit_bytes_method_via_dyn', (('self', 'pos', False), ('expr', 'pos', False)))


def _part_28(out):
    _append_method(out, '_maybe_emit_discard_assignment', (('self', 'pos', False), ('target', 'pos', False), ('value_expr', 'pos', False)))
    _append_method(out, '_maybe_emit_exact_int_object', (('self', 'pos', False), ('expr', 'pos', False)))
    _append_method(out, '_maybe_emit_valueclass_constructor_payload', (('self', 'pos', False), ('target_ty', 'pos', False), ('value_expr', 'pos', False)))
    _append_method(out, '_maybe_emit_issubclass_builtin', (('self', 'pos', False), ('expr', 'pos', False)))
    _append_method(out, '_maybe_emit_protocol_isinstance', (('self', 'pos', False), ('obj_expr', 'pos', False), ('cls_ident', 'pos', False)))
    _append_method(out, '_maybe_register_class_alias_assign', (('self', 'pos', False), ('stmt', 'pos', False)))
    _append_method(out, '_method_arg_prefers_native_callable_value', (('self', 'pos', False), ('expr', 'pos', False)))
    _append_method(out, '_method_returns_receiver', (('self', 'pos', False), ('fd', 'pos', False)))


def _part_29(out):
    _append_method(out, '_module_global_valueclass_payload_field_slot', (('self', 'pos', False), ('gv', 'pos', False), ('field_path', 'pos', False), ('', 'kw_only', False), ('name', 'pos', False)))
    _append_method(out, '_module_global_needs_teardown', (('self', 'pos', False), ('gv', 'pos', False), ('declared_ty', 'pos', False)))
    _append_method(out, '_module_symbol_suffix', (('self', 'pos', False), ('module_name', 'pos', True)))
    _append_method(out, '_module_teardown_name', (('self', 'pos', False), ('module_name', 'pos', True)))
    _append_method(out, '_name_returns_native_builtin_callable_value', (('self', 'pos', False), ('name', 'pos', False)))
    _append_method(out, '_name_returns_owned_function_value', (('self', 'pos', False), ('ident', 'pos', False)))
    _append_method(out, '_native_builtin_module_for_name', (('self', 'pos', False), ('ident', 'pos', False)))
    _append_method(out, '_native_builtin_value_for_name', (('self', 'pos', False), ('ident', 'pos', False)))


def _part_30(out):
    _append_method(out, '_native_re_call_returns_owned_object', (('self', 'pos', False), ('expr', 'pos', False)))
    _append_method(out, '_note_owned_object_value', (('self', 'pos', False), ('value', 'pos', False)))
    _append_method(out, '_value_is_owned_object', (('self', 'pos', False), ('value', 'pos', False)))
    _append_method(out, '_note_owned_dynamic_call_value', (('self', 'pos', False), ('value', 'pos', False)))
    _append_method(out, '_value_is_owned_dynamic_call', (('self', 'pos', False), ('value', 'pos', False)))
    _append_method(out, '_native_re_compile_alias_for_name', (('self', 'pos', False), ('alias', 'pos', False)))
    _append_method(out, '_native_builtin_value_kind_for_expr', (('self', 'pos', False), ('expr', 'pos', False)))
    _append_method(out, '_native_module_attr_global', (('self', 'pos', False), ('module_name', 'pos', False), ('attr_name', 'pos', False)))


def _part_31(out):
    _append_method(out, '_native_module_expr_export_info', (('self', 'pos', False), ('module_expr', 'pos', False), ('attr_name', 'pos', False)))
    _append_method(out, '_ordered_declare_extern_class_args', (('self', 'pos', False), ('expr', 'pos', False)))
    _append_method(out, '_param_ir_and_bind_type', (('self', 'pos', False), ('arg', 'pos', False), ('', 'kw_only', False), ('require_annotation', 'pos', False), ('owner_name', 'pos', False), ('box_int_params', 'pos', True)))
    _append_method(out, '_patch_fn_err_exit_gc_root_leave', (('self', 'pos', False), ('name', 'pos', False), ('alloca', 'pos', False)))
    _append_method(out, '_position_at_entry_hoist_point', (('self', 'pos', False),))
    _append_method(out, '_prescan_function_module_globals', (('self', 'pos', False), ('fd', 'pos', False)))
    _append_method(out, '_ptr_to_cstr', (('self', 'pos', False), ('gv', 'pos', False)))
    _append_method(out, '_push_try_err_block', (('self', 'pos', False), ('err_bb', 'pos', False)))


def _part_32(out):
    _append_method(out, '_raw_scaffold_object_rhs_is_owned', (('self', 'pos', False), ('expr', 'pos', False)))
    _append_method(out, '_register_extern_scaffold_imports', (('self', 'pos', False), ('stmt', 'pos', False)))
    _append_method(out, '_register_native_builtin_import_from_aliases', (('self', 'pos', False), ('stmt', 'pos', False), ('import_module', 'pos', False)))
    _append_method(out, '_register_native_builtin_module_alias', (('self', 'pos', False), ('local_name', 'pos', False), ('module_name', 'pos', False)))
    _append_method(out, '_register_unsafe_scaffold_imports', (('self', 'pos', False), ('stmt', 'pos', False)))
    _append_method(out, '_release_context_label', (('self', 'pos', False), ('kind', 'pos', False)))
    _append_method(out, '_release_existing_owned_local', (('self', 'pos', False), ('name', 'pos', False)))
    _append_method(out, '_release_expr_label', (('self', 'pos', False), ('kind', 'pos', False), ('expr', 'pos', False)))


def _part_33(out):
    _append_method(out, '_resolve_call_kwargs', (('self', 'pos', False), ('positional', 'pos', False), ('kwargs_pairs', 'pos', False), ('formal_args', 'pos', False), ('skip_self', 'pos', True)))
    _append_method(out, '_resolve_class_alias', (('self', 'pos', False), ('name', 'pos', False)))
    _append_method(out, '_resolve_class_attr_mro', (('self', 'pos', False), ('class_name', 'pos', False), ('attr_name', 'pos', False)))
    _append_method(out, '_resolve_method_mro', (('self', 'pos', False), ('class_name', 'pos', False), ('method_name', 'pos', False)))
    _append_method(out, '_resolve_relative_import', (('self', 'pos', False), ('stmt', 'pos', False)))
    _append_method(out, '_restore_try_err_block', (('self', 'pos', False), ('prev_err_block', 'pos', False)))
    _append_method(out, '_return_expr_looks_cpython', (('self', 'pos', False), ('expr', 'pos', False), ('call_arg_map', 'pos', False)))
    _append_method(out, '_rewrite_traceback_handler_bindings', (('self', 'pos', False),))


def _part_34(out):
    _append_method(out, '_should_box_python_ints', (('self', 'pos', False),))
    _append_method(out, '_strict_no_libpython_import_fallback_enabled', (('self', 'pos', False),))
    _append_method(out, '_strict_stub_user_function_with_cpy_fallback', (('self', 'pos', False), ('fn', 'pos', False), ('fd', 'pos', False)))
    _append_method(out, '_storage_ir_type', (('self', 'pos', False), ('ty', 'pos', False)))
    _append_method(out, '_store_entry_initializer', (('self', 'pos', False), ('ptr', 'pos', False), ('value', 'pos', False)))
    _append_method(out, '_store_module_global_root_value', (('self', 'pos', False), ('gv', 'pos', False), ('value', 'pos', False), ('', 'kw_only', False), ('declared_ty', 'pos', True), ('value_is_owned', 'pos', True), ('is_cpy_value', 'pos', True), ('raw_pointer', 'pos', True)))
    _append_method(out, '_clear_module_global_valueclass_payload_roots', (('self', 'pos', False), ('gv', 'pos', False), ('declared_ty', 'pos', False)))
    _append_method(out, '_refresh_module_global_valueclass_payload_roots', (('self', 'pos', False), ('gv', 'pos', False), ('declared_ty', 'pos', False)))


def _part_35(out):
    _append_method(out, '_subprocess_check_output_text_mode', (('self', 'pos', False), ('expr', 'pos', False)))
    _append_method(out, '_threading_constructor_kind_for_expr', (('self', 'pos', False), ('expr', 'pos', False)))
    _append_method(out, '_threading_list_elem_kind_for_expr', (('self', 'pos', False), ('expr', 'pos', False)))
    _append_method(out, '_threading_list_elem_kind_for_type', (('self', 'pos', False), ('ty', 'pos', False)))
    _append_method(out, '_to_double', (('self', 'pos', False), ('v', 'pos', False), ('ty', 'pos', False)))
    _append_method(out, '_to_int64', (('self', 'pos', False), ('v', 'pos', False), ('ty', 'pos', False)))
    _append_method(out, '_truthy', (('self', 'pos', False), ('v', 'pos', False), ('ty', 'pos', False)))
    _append_method(out, '_try_emit_native_file_stream_print', (('self', 'pos', False), ('call', 'pos', False)))


def _part_36(out):
    _append_method(out, '_unpack_target_value_is_owned', (('self', 'pos', False), ('value_ty', 'pos', False)))
    _append_method(out, '_unbox_scalar_attr_result', (('self', 'pos', False), ('result', 'pos', False), ('result_ty', 'pos', False)))
    _append_method(out, '_unsafe_intrinsic_for_name', (('self', 'pos', False), ('name', 'pos', False)))
    _append_method(out, '_unsafe_void_result', (('self', 'pos', False),))
    _append_method(out, '_user_func_returns_cpython', (('self', 'pos', False), ('ast_fd', 'pos', False), ('formals', 'pos', True), ('actual_args', 'pos', True)))
    _append_method(out, '_utf8_byte_values', (('self', 'pos', False), ('payload', 'pos', False)))
    _append_method(out, '_valueclass_field_info', (('self', 'pos', False), ('ty', 'pos', False), ('attr_name', 'pos', False)))
    _append_method(out, '_valueclass_field_payload_ir_type', (('self', 'pos', False), ('field_ty', 'pos', False)))


def _part_37(out):
    _append_method(out, '_valueclass_payload_expr_type', (('self', 'pos', False), ('expr', 'pos', False)))
    _append_method(out, '_valueclass_payload_pointer_field_paths', (('self', 'pos', False), ('ty', 'pos', False), ('prefix', 'pos', True)))
    _append_method(out, '_emit_entry_valueclass_payload_field_slot', (('self', 'pos', False), ('payload_alloca', 'pos', False), ('field_path', 'pos', False), ('name', 'pos', False)))
    _append_method(out, '_ensure_valueclass_payload_gc_roots', (('self', 'pos', False), ('name', 'pos', False), ('payload_alloca', 'pos', False), ('ty', 'pos', False), ('', 'kw_only', False), ('borrowed', 'pos', True)))
    _append_method(out, '_emit_valueclass_payload_field_eq', (('self', 'pos', False), ('lhs_field', 'pos', False), ('rhs_field', 'pos', False), ('field_ty', 'pos', False)))
    _append_method(out, '_emit_valueclass_payload_fields_eq', (('self', 'pos', False), ('lhs', 'pos', False), ('rhs', 'pos', False), ('ty', 'pos', False)))
    _append_method(out, '_valueclass_payload_ir_type', (('self', 'pos', False), ('ty', 'pos', False)))
    _append_method(out, '_is_valueclass_payload_type', (('self', 'pos', False), ('ty', 'pos', False)))


def _part_38(out):
    _append_method(out, '_virtual_thread_frame_map', (('self', 'pos', False), ('n_slots', 'pos', False)))
    _append_method(out, '_weak_dict_constructor_kind_for_expr', (('self', 'pos', False), ('expr', 'pos', False)))
    _append_method(out, '_weakref_call_expr_returns_owned_object', (('self', 'pos', False), ('expr', 'pos', False)))
    _append_method(out, '_weakref_constructor_kind_for_expr', (('self', 'pos', False), ('expr', 'pos', False)))
    _append_method(out, '_abi_ir_type', (('self', 'pos', False), ('ty', 'pos', False), ('', 'kw_only', False), ('box_int_abi', 'pos', False)))
    _append_method(out, '_attr_name_ptr', (('self', 'pos', False), ('name', 'pos', False)))
    _append_method(out, '_codegen_trace_dump', (('self', 'pos', False), ('exc', 'pos', False)))
    _append_method(out, '_codegen_trace_push', (('self', 'pos', False), ('boundary', 'pos', False), ('stmt_index', 'pos', False), ('stmt_kind', 'pos', False), ('expr_kind', 'pos', False), ('span', 'pos', False)))


def _part_39(out):
    _append_method(out, '_codegen_trace_set_stmt_context', (('self', 'pos', False), ('stmt_index', 'pos', False), ('stmt_kind', 'pos', False)))
    _append_method(out, '_codegen_trace_span', (('self', 'pos', False), ('node', 'pos', False)))
    _append_method(out, '_emit_attribute_error_if_null', (('self', 'pos', False), ('value', 'pos', False), ('attr_name', 'pos', False), ('span', 'pos', False)))
    _append_method(out, '_emit_direct_method_call', (('self', 'pos', False), ('method_fn', 'pos', False), ('self_val', 'pos', False), ('info', 'pos', False), ('method_name', 'pos', False), ('arg_exprs', 'pos', False), ('kwargs', 'pos', True), ('park_expr', 'pos', True)))
    _append_method(out, '_emit_async_native_func_value_adapter', (('self', 'pos', False), ('orig_name', 'pos', False), ('body_adapter', 'pos', False)))
    _append_method(out, '_active_handler_exception_for_current_function', (('self', 'pos', False),))
    _append_method(out, '_emit_exception_frame', (('self', 'pos', False), ('exc', 'pos', False), ('span', 'pos', False)))
    _append_method(out, '_emit_generator_wrapper_function', (('self', 'pos', False), ('fd', 'pos', False), ('fn', 'pos', False), ('symbol_name', 'pos', True), ('class_info', 'pos', True), ('method_kind', 'pos', True)))


def _part_40(out):
    _append_method(out, '_emit_native_func_signature', (('self', 'pos', False), ('original_args', 'pos', False)))
    _append_method(out, '_emit_stmt', (('self', 'pos', False), ('stmt', 'pos', False)))
    _append_method(out, '_emit_thread_safepoint', (('self', 'pos', False),))
    _append_method(out, '_native_re_class_compile_attr_string_value', (('self', 'pos', False), ('class_name', 'pos', False), ('attr_name', 'pos', False), ('value_expr', 'pos', False)))
    _append_method(out, '_pooled_cstr_ptr', (('self', 'pos', False), ('payload', 'pos', False), ('prefix', 'pos', True)))
    _append_method(out, '_zero_of', (('self', 'pos', False), ('ir_ty', 'pos', False)))


def _build_static_methods():
    out = []
    _part_0(out)
    _part_1(out)
    _part_2(out)
    _part_3(out)
    _part_4(out)
    _part_5(out)
    _part_6(out)
    _part_7(out)
    _part_8(out)
    _part_9(out)
    _part_10(out)
    _part_11(out)
    _part_12(out)
    _part_13(out)
    _part_14(out)
    _part_15(out)
    _part_16(out)
    _part_17(out)
    _part_18(out)
    _part_19(out)
    _part_20(out)
    _part_21(out)
    _part_22(out)
    _part_23(out)
    _part_24(out)
    _part_25(out)
    _part_26(out)
    _part_27(out)
    _part_28(out)
    _part_29(out)
    _part_30(out)
    _part_31(out)
    _part_32(out)
    _part_33(out)
    _part_34(out)
    _part_35(out)
    _part_36(out)
    _part_37(out)
    _part_38(out)
    _part_39(out)
    _part_40(out)
    return tuple(out)

L1_CODEGEN_STATIC_METHODS = _build_static_methods()
