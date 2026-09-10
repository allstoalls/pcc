"""Runtime-lib ABI declarations for pcc_py codegen.

Mirrors the C signatures in ``pcc/py_runtime/include/py_runtime.h``
(Section 3 of docs/plans/python-frontend-interfaces.md). Every function
declared here is a placeholder ``llvmlite.ir.Function`` with external
linkage — the definition lives in ``py_runtime.a`` and is linked in at
exe-build time.

Usage::

    from pcc.py_frontend.codegen.runtime_abi import declare_runtime
    module = ir.Module(name="my_module")
    rt = declare_runtime(module)
    builder.call(rt["py_print"], [obj])

Every entry in ``RUNTIME_SIGNATURES`` corresponds 1:1 with a prototype
in py_runtime.h. Changes to the C header MUST be reflected here (and
vice-versa) — the contract doc is the single source of truth.
"""

from __future__ import annotations

from pcc.llvm_capi.compat import ir

# -- Canonical LLVM IR types used in the runtime ABI -------------------------

_VOID = ir.VoidType()
_I1 = ir.IntType(1)
_I8 = ir.IntType(8)
_I32 = ir.IntType(32)
_I64 = ir.IntType(64)
_DOUBLE = ir.DoubleType()
# Opaque pointer. llvmlite emits typed pointers, but we use i8* as the
# generic ptr everywhere the ABI takes/returns a PyObject*. Subsequent
# opt passes ignore the pointee type for opaque-pointer LLVM versions.
_PTR = _I8.as_pointer()
_I32_PTR = _I32.as_pointer()
_CSTR = _I8.as_pointer()  # const char*
_CSTR_PTR = _CSTR.as_pointer()  # const char**

# ``PyObject*`` is spelled ``_PTR`` at the LLVM-IR level.
_PYOBJ = _PTR


# Table of (return_type, [param_types], var_arg). The names and
# signatures mirror py_runtime.h line-for-line. Whenever adding a new
# runtime function, add the C prototype there first, then mirror here.
def _runtime_signatures_part_0():
    return {
    # ---- GC interface ----------------------------------------------
    "pcc_gc_alloc": (_PYOBJ, [_I64, _I32, _I32], False),
    "pcc_gc_pointer_is_managed": (_I64, [_PYOBJ], False),
    "pcc_gc_pointer_register": (_I64, [_PYOBJ], False),
    "pcc_gc_pointer_unregister": (_I64, [_PYOBJ], False),
    "pcc_gc_retain": (_PYOBJ, [_PYOBJ], False),
    "pcc_gc_release": (_VOID, [_PYOBJ], False),
    "pcc_gc_retain_known": (_PYOBJ, [_PYOBJ], False),
    "pcc_gc_release_known": (_VOID, [_PYOBJ], False),
    "pcc_gc_set_known_ref_checks": (_VOID, [_I64], False),
    "pcc_debug_check_release": (_VOID, [_CSTR, _PYOBJ], False),
    "pcc_gc_load_ptr": (_PYOBJ, [_PYOBJ, _PTR], False),
    "pcc_gc_load_borrowed_ptr": (_PYOBJ, [_PYOBJ, _PTR], False),
    "pcc_gc_resolve_owned_ptr": (_PYOBJ, [_PYOBJ], False),
    "pcc_gc_store_ptr": (_VOID, [_PYOBJ, _PTR, _PYOBJ], False),
    "pcc_gc_store_root": (_VOID, [_PTR, _PYOBJ], False),
    "pcc_gc_note_write_barrier": (_VOID, [_PYOBJ, _PYOBJ], False),
    "pcc_gc_note_slot_write_barrier": (_VOID, [_PYOBJ, _PTR, _PYOBJ], False),
    "pcc_gc_scheduler_root_register_handle": (_PTR, [_PTR], False),
    "pcc_gc_scheduler_root_unregister_handle": (_VOID, [_PTR], False),
    "pcc_gc_scheduler_root_register": (_VOID, [_PTR], False),
    "pcc_gc_scheduler_root_unregister": (_VOID, [_PTR], False),
    "pcc_gc_register_continuation_root": (_VOID, [_PTR, _PTR], False),
    "pcc_gc_register_continuation_root_node": (_PTR, [_PTR, _PTR], False),
    "pcc_gc_unregister_continuation_root_node": (_VOID, [_PTR], False),
    "pcc_gc_unregister_continuation_root": (_VOID, [_PTR], False),
    "pcc_gc_trace_continuation_roots": (_I64, [], False),
    "pcc_gc_rewrite_continuation_roots": (_I64, [], False),
    "pcc_gc_scheduler_queue_new": (_PTR, [], False),
    "pcc_gc_scheduler_queue_free": (_VOID, [_PTR], False),
    "pcc_gc_scheduler_queue_push": (_I64, [_PTR, _PYOBJ], False),
    "pcc_gc_scheduler_queue_pop_into": (_I64, [_PTR, _PTR], False),
    "pcc_gc_scheduler_queue_len": (_I64, [_PTR], False),
    "pcc_gc_frame_enter": (_VOID, [_PTR, _PTR], False),
    "pcc_gc_frame_leave": (_VOID, [_PTR], False),
    "pcc_gc_frame_enter_lifo": (_VOID, [_PTR, _PTR], False),
    "pcc_gc_frame_leave_lifo": (_VOID, [_PTR], False),
    "pcc_gc_consume_precise_stackmap": (
        _I64, [_PTR, _I64, _I64, _PTR, _I64, _I64], False,
    ),
    "pcc_gc_safepoint": (_VOID, [], False),
    "pcc_gc_collect": (_I64, [_I32], False),
    "pcc_gc_pin": (_VOID, [_PYOBJ], False),
    "pcc_gc_unpin": (_VOID, [_PYOBJ], False),
    "pcc_gc_immortalize": (_VOID, [_PYOBJ], False),
    "pcc_gc_object_id": (_I64, [_PYOBJ], False),
    "pcc_gc_reset_relocation_set": (_VOID, [], False),
    "pcc_gc_select_relocation_set": (_I64, [_I64], False),
    "pcc_gc_backend4_evacuation_drain": (_I64, [_I64], False),
    "pcc_gc_backend4_evacuation_page_drain": (_I64, [_I64], False),
    "pcc_gc_backend4_reseed_plan_probe_config": (
        _VOID,
        [_I64, _I64],
        False,
    ),
    "pcc_gc_backend4_reseed_plan_probe_state": (_I64, [], False),
    "pcc_gc_backend3_remembered_scan_probe_config": (
        _VOID,
        [_I64],
        False,
    ),
    "pcc_gc_backend4_remap_and_retire_unlocked": (_VOID, [_PTR], False),
    "pcc_gc_backend4_remap_and_retire_stopped_world": (_I64, [], False),
    "pcc_gc_backend4_finish_retained_page_releases": (_VOID, [_PTR], False),
    "pcc_gc_backend4_finish_remap_retirement": (_VOID, [_PTR], False),
    "pcc_gc_relocation_set_contains": (_I64, [_PYOBJ], False),
    }


def _runtime_signatures_part_1():
    return {
    "pcc_gc_relocation_set_size": (_I64, [], False),
    "pcc_gc_install_forwarding": (_I64, [_PYOBJ, _PYOBJ], False),
    "pcc_gc_relocate_copy": (_PYOBJ, [_PYOBJ, _I64], False),
    "pcc_gc_backend": (_I64, [], False),
    "pcc_py_gc_minor_graph_lock": (_VOID, [], False),
    "pcc_py_gc_minor_graph_unlock": (_VOID, [], False),
    "py_obj_truthy": (_I64, [_PYOBJ], False),
    "py_obj_type_tag": (_I64, [_PYOBJ], False),
    "py_obj_eq": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_obj_eq_value": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_obj_lt": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_obj_le": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_obj_gt": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_obj_ge": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_obj_hash": (_I64, [_PYOBJ], False),
    "py_instance_set_field": (_VOID, [_PYOBJ, _I32, _PYOBJ], False),
    }


def _runtime_signatures_part_2():
    return {
    "pcc_gc_set_backend": (_I64, [_I64], False),
    "pcc_gc_backend_name": (_CSTR, [_I64], False),
    "pcc_gc_telemetry": (_I64, [_I64], False),
    "pcc_gc_telemetry_reset": (_VOID, [], False),
    "pcc_gc_step": (_I64, [_I64], False),
    "pcc_gc_backend4_verify_no_old_addresses": (_I64, [], False),
    "pcc_gc_backend4_fragmentation_score": (_I64, [], False),
    "pcc_gc_backend4_forwarding_entries": (_I64, [], False),
    "pcc_gc_backend4_stable_id_entries": (_I64, [], False),
    "pcc_gc_backend4_generation_barrier_score": (_I64, [], False),
    "pcc_gc_backend4_store_buffer_entries": (_I64, [], False),
    "pcc_gc_backend4_generation_promotion_score": (_I64, [], False),
    "pcc_gc_backend4_evacuation_candidate_score": (_I64, [], False),
    "pcc_gc_backend4_evacuated_bytes": (_I64, [], False),
    "pcc_gc_backend4_page_policy_score": (_I64, [], False),
    "pcc_gc_backend4_large_object_defer_score": (_I64, [], False),
    "pcc_gc_backend4_large_object_deferred_bytes": (_I64, [], False),
    "pcc_gc_backend4_small_page_candidate_score": (_I64, [], False),
    "pcc_gc_backend4_medium_page_candidate_score": (_I64, [], False),
    "pcc_gc_backend4_evacuation_candidate_bytes": (_I64, [], False),
    "pcc_gc_backend4_small_page_candidate_bytes": (_I64, [], False),
    "pcc_gc_backend4_medium_page_candidate_bytes": (_I64, [], False),
    "pcc_gc_backend4_evacuation_candidate_zpage_bytes": (_I64, [], False),
    "pcc_gc_backend4_small_page_candidate_zpage_bytes": (_I64, [], False),
    "pcc_gc_backend4_medium_page_candidate_zpage_bytes": (_I64, [], False),
    "pcc_gc_backend4_evacuation_page_candidate_score": (_I64, [], False),
    "pcc_gc_backend4_evacuation_page_candidate_bytes": (_I64, [], False),
    "pcc_gc_backend4_evacuation_page_dirty_cards": (_I64, [], False),
    "pcc_gc_backend4_store_buffer_drain_batches": (_I64, [], False),
    "pcc_gc_backend4_store_buffer_drained_entries": (_I64, [], False),
    "pcc_gc_backend4_store_buffer_duplicate_skips": (_I64, [], False),
    "pcc_gc_backend4_store_buffer_high_water": (_I64, [], False),
    "pcc_gc_backend4_page_pressure_score": (_I64, [], False),
    "pcc_gc_backend4_store_buffer_owner_fanout_high_water": (_I64, [], False),
    "pcc_gc_backend4_store_buffer_owner_count_high_water": (_I64, [], False),
    "pcc_gc_backend4_store_buffer_incomplete_drains": (_I64, [], False),
    "pcc_gc_backend4_evacuation_incomplete_batches": (_I64, [], False),
    "pcc_gc_backend4_store_buffer_batch_capacity": (_I64, [], False),
    "pcc_gc_backend4_store_buffer_max_batch_size": (_I64, [], False),
    "pcc_gc_backend4_store_buffer_full_batches": (_I64, [], False),
    "pcc_gc_backend4_store_buffer_medium_capacity": (_I64, [], False),
    "pcc_gc_backend4_store_buffer_medium_pending": (_I64, [], False),
    "pcc_gc_backend4_store_buffer_medium_flushes": (_I64, [], False),
    "pcc_gc_backend4_store_buffer_medium_flushed_entries": (_I64, [], False),
    "pcc_gc_backend4_store_buffer_medium_full_flushes": (_I64, [], False),
    "pcc_gc_backend4_store_buffer_cross_thread_medium_flushes": (_I64, [], False),
    "pcc_gc_backend4_store_buffer_cross_thread_medium_flushed_entries": (
        _I64,
        [],
        False,
    ),
    "pcc_gc_backend4_remembered_set_entries": (_I64, [], False),
    "pcc_gc_backend4_remembered_set_duplicate_skips": (_I64, [], False),
    "pcc_gc_backend4_remembered_set_high_water": (_I64, [], False),
    }


def _runtime_signatures_part_3():
    return {
    "pcc_gc_backend4_remembered_page_entries": (_I64, [], False),
    "pcc_gc_backend4_remembered_page_slot_entries": (_I64, [], False),
    "pcc_gc_backend4_remembered_page_high_water": (_I64, [], False),
    "pcc_gc_backend4_remembered_page_contains_slot": (_I64, [_PTR], False),
    "pcc_gc_backend4_remembered_page_clear_slot": (_I64, [_PTR], False),
    "pcc_gc_backend4_zpage_contains_remembered_card": (_I64, [_PTR, _PTR], False),
    "pcc_gc_backend4_zpage_clear_remembered_card": (_I64, [_PTR, _PTR], False),
    "pcc_gc_backend4_zpage_count": (_I64, [], False),
    "pcc_gc_backend4_zpage_capacity_bytes": (_I64, [], False),
    "pcc_gc_backend4_zpage_fragmentation_bytes": (_I64, [], False),
    "pcc_gc_backend4_zpage_large_pages": (_I64, [], False),
    "pcc_gc_backend4_zpage_used_bytes": (_I64, [], False),
    "pcc_gc_backend4_zpage_allocated_bytes": (_I64, [], False),
    "pcc_gc_backend4_zpage_reclaimable_gap_bytes": (_I64, [], False),
    "pcc_gc_backend4_zpage_span_bytes": (_I64, [], False),
    "pcc_gc_backend4_zpage_owner_offset_bytes": (_I64, [_PYOBJ], False),
    "pcc_gc_backend4_zpage_owner_size_bytes": (_I64, [_PYOBJ], False),
    "pcc_gc_backend4_zpage_owner_span_card": (_I64, [_PYOBJ], False),
    "pcc_gc_backend4_zpage_owner_slot_span_card": (_I64, [_PYOBJ, _PTR], False),
    "pcc_gc_backend4_zpage_register_owner_payload_span": (
        _I64,
        [_PYOBJ, _PTR, _I64],
        False,
    ),
    "pcc_gc_backend4_zpage_unregister_owner_payload_span": (
        _I64,
        [_PYOBJ, _PTR],
        False,
    ),
    "pcc_gc_backend4_zpage_retarget_owner_payload_span": (
        _I64,
        [_PYOBJ, _PTR, _PTR, _I64],
        False,
    ),
    "pcc_gc_backend4_zpage_fragmentation_per_mille": (_I64, [], False),
    "pcc_gc_backend4_zpage_policy_score": (_I64, [], False),
    "pcc_gc_backend4_zpage_remembered_slots": (_I64, [], False),
    "pcc_gc_backend4_zpage_remembered_cards": (_I64, [], False),
    "pcc_gc_backend4_zpage_remembered_card_ratio_per_mille": (_I64, [], False),
    "pcc_gc_backend4_zpage_dirty_pages": (_I64, [], False),
    "pcc_gc_backend4_zpage_fragmented_pages": (_I64, [], False),
    "pcc_gc_backend4_zpage_young_pages": (_I64, [], False),
    "pcc_gc_backend4_zpage_old_pages": (_I64, [], False),
    "pcc_gc_backend4_zpage_free_pages": (_I64, [], False),
    "pcc_gc_backend4_zpage_free_capacity_bytes": (_I64, [], False),
    "pcc_gc_backend4_zpage_free_span_bytes": (_I64, [], False),
    "pcc_gc_backend4_evacuation_efficiency_per_mille": (_I64, [], False),
    "pcc_gc_backend4_fragmentation_backlog_bytes": (_I64, [], False),
    "pcc_gc_backend4_fragmentation_policy_score": (_I64, [], False),
    "pcc_gc_backend4_small_page_limit_bytes": (_I64, [], False),
    "pcc_gc_backend4_medium_page_limit_bytes": (_I64, [], False),
    "pcc_gc_backend4_large_defer_limit_bytes": (_I64, [], False),
    "pcc_gc_backend4_large_object_reconsiderations": (_I64, [], False),
    "pcc_gc_backend4_young_object_count": (_I64, [], False),
    "pcc_gc_backend4_old_object_count": (_I64, [], False),
    "pcc_gc_backend4_young_bytes": (_I64, [], False),
    "pcc_gc_backend4_old_bytes": (_I64, [], False),
    "pcc_gc_backend4_small_page_object_count": (_I64, [], False),
    "pcc_gc_backend4_medium_page_object_count": (_I64, [], False),
    "pcc_gc_backend4_large_page_object_count": (_I64, [], False),
    "pcc_gc_backend4_small_page_live_bytes": (_I64, [], False),
    "pcc_gc_backend4_medium_page_live_bytes": (_I64, [], False),
    }


def _runtime_signatures_part_4():
    return {
    "pcc_gc_backend4_large_page_live_bytes": (_I64, [], False),
    "pcc_gc_backend2_production_score": (_I64, [], False),
    "pcc_gc_backend2_worker_buffer_score": (_I64, [], False),
    "pcc_gc_backend3_minor_productivity_score": (_I64, [], False),
    "pcc_gc_backend3_remembered_update_score": (_I64, [], False),
    "pcc_gc_scheduler_root_count": (_I64, [], False),
    "pcc_gc_frame_root_slot_count": (_I64, [], False),
    "pcc_gc_continuation_root_slot_count": (_I64, [], False),
    "pcc_gc_coroutine_root_score": (_I64, [], False),
    "pcc_gc_default_unlink_tracked_node": (_VOID, [_PTR], False),
    "pcc_gc_tracked_node_pool_cached_count": (_I64, [], False),
    "pcc_gc_tracked_node_pool_drain": (_VOID, [], False),
    "py_gc_index_insert": (_I64, [_PTR, _PTR], False),
    "py_gc_index_remove": (_PTR, [_PTR], False),
    "py_gc_callbacks_list": (_PYOBJ, [], False),
    "py_gc_callbacks_append": (_VOID, [_PYOBJ], False),
    "py_gc_callbacks_remove": (_VOID, [_PYOBJ], False),
    # ---- Runtime threading substrate -------------------------------
    "pcc_threads_enabled": (_I64, [], False),
    "pcc_current_thread_id": (_I64, [], False),
    "pcc_current_native_thread_token": (_PTR, [], False),
    "pcc_refcount_strategy": (_I64, [], False),
    "pcc_thread_safepoint": (_VOID, [], False),
    "pcc_thread_stop_requested_acquire": (_I64, [], False),
    "pcc_thread_no_park_enter": (_VOID, [], False),
    "pcc_thread_no_park_exit": (_VOID, [], False),
    "pcc_thread_no_park_depth": (_I64, [], False),
    "pcc_thread_owns_stopped_world": (_I64, [], False),
    "pcc_thread_registration_waiter_count": (_I64, [], False),
    "pcc_thread_unregister_current": (_VOID, [], False),
    "pcc_stop_the_world": (_I64, [], False),
    "pcc_resume_world": (_I64, [], False),
    # ---- Native threading module ---------------------------------
    "py_threading_get_ident": (_I64, [], False),
    "py_threading_current_thread": (_PYOBJ, [], False),
    "py_threading_lock_new": (_PYOBJ, [], False),
    "py_threading_lock_acquire": (_I64, [_PYOBJ], False),
    "py_threading_lock_acquire_vthread": (_I64, [_PYOBJ], False),
    "py_threading_lock_release": (_I64, [_PYOBJ], False),
    "py_threading_rlock_new": (_PYOBJ, [], False),
    "py_threading_rlock_acquire": (_I64, [_PYOBJ], False),
    "py_threading_rlock_release": (_I64, [_PYOBJ], False),
    "py_threading_event_new": (_PYOBJ, [], False),
    "py_threading_event_set": (_I64, [_PYOBJ], False),
    "py_threading_event_clear": (_I64, [_PYOBJ], False),
    "py_threading_event_is_set": (_I64, [_PYOBJ], False),
    "py_threading_event_wait": (_I64, [_PYOBJ], False),
    "py_threading_event_wait_vthread": (_I64, [_PYOBJ], False),
    "py_threading_condition_new": (_PYOBJ, [_PYOBJ], False),
    "py_threading_condition_acquire": (_I64, [_PYOBJ], False),
    "py_threading_condition_release": (_I64, [_PYOBJ], False),
    "py_threading_condition_wait": (_I64, [_PYOBJ], False),
    "py_threading_condition_wait_vthread": (_I64, [_PYOBJ], False),
    "py_threading_condition_notify": (_I64, [_PYOBJ], False),
    }


def _runtime_signatures_part_5():
    return {
    "py_threading_semaphore_new": (_PYOBJ, [_I64], False),
    "py_threading_semaphore_acquire": (_I64, [_PYOBJ], False),
    "py_threading_semaphore_acquire_vthread": (_I64, [_PYOBJ], False),
    "py_threading_semaphore_release": (_I64, [_PYOBJ], False),
    "py_threading_thread_new": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_threading_thread_start": (_I64, [_PYOBJ], False),
    "py_threading_thread_join": (_I64, [_PYOBJ], False),
    "py_bytes_join": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "pcc_gc_store_root_take": (_VOID, [_PTR, _PYOBJ], False),
    }


def _runtime_signatures_part_6():
    return {
    "py_threading_thread_is_alive": (_I64, [_PYOBJ], False),
    # ---- refcount --------------------------------------------------
    "py_incref": (_VOID, [_PYOBJ], False),
    "py_decref": (_VOID, [_PYOBJ], False),
    "pcc_py_type_of": (_I64, [_PYOBJ], False),
    # ---- Bool ------------------------------------------------------
    "py_bool_from_bit": (_PYOBJ, [_I32], False),
    # ---- Int (tagged + bignum) ------------------------------------
    "py_int_from_i64": (_PYOBJ, [_I64], False),
    "py_int_value_i64": (_I64, [_PYOBJ], False),
    "py_int_from_cstr": (_PYOBJ, [_CSTR, _I32], False),
    "py_int_from_cstr_or_raise": (_PYOBJ, [_CSTR, _I32], False),
    "py_obj_as_int_object": (_PYOBJ, [_PYOBJ, _I32], False),
    "py_int_to_i64": (_I64, [_PYOBJ, _I32_PTR], False),
    "py_int_bit_length": (_I64, [_PYOBJ], False),
    "py_int_bit_count": (_I64, [_PYOBJ], False),
    "py_int_add": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_int_sub": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_int_mul": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_int_floordiv": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_int_truediv": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_int_mod": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_int_pow": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_int_pow_mod": (_PYOBJ, [_PYOBJ, _PYOBJ, _PYOBJ], False),
    "py_int_isqrt": (_PYOBJ, [_PYOBJ], False),
    "py_obj_min_max": (_PYOBJ, [_PYOBJ, _I64], False),
    "py_tuple_count": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_tuple_index": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_tuple_index_range": (_I64, [_PYOBJ, _PYOBJ, _PYOBJ, _PYOBJ], False),
    "py_int_neg": (_PYOBJ, [_PYOBJ], False),
    "py_int_and": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_int_or": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_int_xor": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_int_shl": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_int_shr": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_int_cmp": (_I32, [_PYOBJ, _PYOBJ], False),
    "py_int_format_hex": (_PYOBJ, [_PYOBJ, _I64, _I64], False),
    "py_int_format_decimal": (_PYOBJ, [_PYOBJ, _I64, _I64, _I64], False),
    "py_obj_abs": (_PYOBJ, [_PYOBJ], False),
    "py_builtin_bin": (_PYOBJ, [_PYOBJ], False),
    "py_builtin_hex": (_PYOBJ, [_PYOBJ], False),
    "py_builtin_oct": (_PYOBJ, [_PYOBJ], False),
    "py_builtin_callable": (_PYOBJ, [_PYOBJ], False),
    # ---- Float -----------------------------------------------------
    "py_float_from_f64": (_PYOBJ, [_DOUBLE], False),
    "py_time_sleep": (_PYOBJ, [_PYOBJ], False),
    "py_float_to_f64": (_DOUBLE, [_PYOBJ], False),
    "py_float_value_of": (_DOUBLE, [_PYOBJ], False),
    "py_float_is_integer": (_I64, [_PYOBJ], False),
    # float.fromhex(str) -> float; may raise ValueError / OverflowError.
    "py_float_fromhex": (_PYOBJ, [_PYOBJ], False),
    "py_float_add": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_float_round_ndigits": (_PYOBJ, [_DOUBLE, _I64], False),
    "py_float_format_fixed": (_PYOBJ, [_PYOBJ, _I64], False),
    # ---- Complex ---------------------------------------------------
    "py_complex_new": (_PYOBJ, [_DOUBLE, _DOUBLE], False),
    "py_complex_real": (_PYOBJ, [_PYOBJ], False),
    }


def _runtime_signatures_part_7():
    return {
    "py_complex_imag": (_PYOBJ, [_PYOBJ], False),
    "py_complex_add": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_complex_sub": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_complex_mul": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_complex_div": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    # ``base ** exp`` for complex operands; may raise ZeroDivisionError.
    "py_complex_pow": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_complex_neg": (_PYOBJ, [_PYOBJ], False),
    "py_complex_conjugate": (_PYOBJ, [_PYOBJ], False),
    "py_complex_abs": (_PYOBJ, [_PYOBJ], False),
    # ---- Bytes / bytearray / memoryview ----------------------------
    "py_bytes_new": (_PYOBJ, [_CSTR, _I64], False),
    "py_bytearray_from_obj": (_PYOBJ, [_PYOBJ], False),
    "py_bytes_from_obj": (_PYOBJ, [_PYOBJ], False),
    "py_memoryview_new": (_PYOBJ, [_PYOBJ], False),
    "py_bytes_decode": (_PYOBJ, [_PYOBJ], False),
    "py_bytes_decode_utf8_ignore": (_PYOBJ, [_PYOBJ], False),
    "py_bytes_decode_with_encoding": (
        _PYOBJ,
        [_PYOBJ, _PYOBJ, _PYOBJ],
        False,
    ),
    "py_bytes_hex": (_PYOBJ, [_PYOBJ], False),
    "py_bytes_upper": (_PYOBJ, [_PYOBJ], False),
    "py_bytes_lower": (_PYOBJ, [_PYOBJ], False),
    "py_bytes_strip": (_PYOBJ, [_PYOBJ], False),
    "py_bytes_getitem": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_bytes_slice": (_PYOBJ, [_PYOBJ, _PYOBJ, _PYOBJ, _PYOBJ], False),
    "py_bytes_concat": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_bytes_repeat": (_PYOBJ, [_PYOBJ, _I64], False),
    "py_bytes_maketrans": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_bytes_translate": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_bytes_fromhex": (_PYOBJ, [_PYOBJ], False),
    "py_bytes_replace": (_PYOBJ, [_PYOBJ, _PYOBJ, _PYOBJ], False),
    "py_bytes_len": (_I64, [_PYOBJ], False),
    "py_i64_buffer_new": (_PYOBJ, [_I64], False),
    "py_i64_buffer_set_item": (_I64, [_PYOBJ, _I64, _PYOBJ], False),
    "py_i64_buffer_get_item": (_PYOBJ, [_PYOBJ, _I64], False),
    "py_i64_buffer_data": (_CSTR, [_PYOBJ], False),
    "py_i64_buffer_layout_version": (_I64, [_PYOBJ], False),
    "py_i64_buffer_version": (_I64, [_PYOBJ], False),
    "py_i64_buffer_dot_scalar": (_PYOBJ, [_PYOBJ, _PYOBJ, _I64], False),
    "py_guarded_loop_counter_add": (_I64, [_I64, _I64], False),
    "py_guarded_loop_counter_get": (_I64, [_I64], False),
    "py_bytes_find": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_bytes_rfind": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_bytes_count": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_bytes_split": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_bytes_partition": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_bytearray_extend": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_bytearray_append": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_bytearray_insert": (_PYOBJ, [_PYOBJ, _PYOBJ, _PYOBJ], False),
    "py_bytearray_pop": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_bytearray_setitem": (_I64, [_PYOBJ, _PYOBJ, _PYOBJ], False),
    "py_bytearray_del_slice": (_I64, [_PYOBJ, _PYOBJ, _PYOBJ, _PYOBJ], False),
    # ---- Str -------------------------------------------------------
    "py_str_new": (_PYOBJ, [_CSTR, _I64], False),
    }


def _runtime_signatures_part_8():
    return {
    "py_str_len": (_I64, [_PYOBJ], False),
    "py_str_byte_len": (_I64, [_PYOBJ], False),
    "py_str_utf8": (_CSTR, [_PYOBJ], False),
    "PyContextVar_New": (_PYOBJ, [_CSTR, _PYOBJ], False),
    "py_str_ord": (_I64, [_PYOBJ], False),
    "py_str_ord_at_i64": (_I64, [_PYOBJ, _I64], False),
    "py_str_byte_at_i64": (_I64, [_PYOBJ, _I64], False),
    "py_str_ascii_encode": (_PYOBJ, [_PYOBJ], False),
    "py_str_latin1_encode": (_PYOBJ, [_PYOBJ], False),
    "py_str_utf8_encode": (_PYOBJ, [_PYOBJ], False),
    "py_str_byte_slice_i64": (_PYOBJ, [_PYOBJ, _I64, _I64], False),
    "py_str_concat": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_str_repeat": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_str_slice": (_PYOBJ, [_PYOBJ, _PYOBJ, _PYOBJ, _PYOBJ], False),
    "py_str_index": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_str_eq": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_str_contains": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_str_find": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_str_rfind": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_str_find_range": (_I64, [_PYOBJ, _PYOBJ, _I64, _I64], False),
    "py_str_rfind_range": (_I64, [_PYOBJ, _PYOBJ, _I64, _I64], False),
    "py_str_upper": (_PYOBJ, [_PYOBJ], False),
    "py_str_lower": (_PYOBJ, [_PYOBJ], False),
    "py_str_capitalize": (_PYOBJ, [_PYOBJ], False),
    "py_str_swapcase": (_PYOBJ, [_PYOBJ], False),
    "py_str_title": (_PYOBJ, [_PYOBJ], False),
    "py_str_casefold": (_PYOBJ, [_PYOBJ], False),
    "py_textwrap_dedent": (_PYOBJ, [_PYOBJ], False),
    "py_str_strip": (_PYOBJ, [_PYOBJ], False),
    "py_str_split": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_str_partition": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_str_rpartition": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_str_translate": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_str_maketrans": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_str_removeprefix": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_str_removesuffix": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_str_rjust": (_PYOBJ, [_PYOBJ, _I64, _PYOBJ], False),
    "py_str_ljust": (_PYOBJ, [_PYOBJ, _I64, _PYOBJ], False),
    "py_str_center": (_PYOBJ, [_PYOBJ, _I64, _PYOBJ], False),
    "py_str_zfill": (_PYOBJ, [_PYOBJ, _I64], False),
    "py_str_expandtabs": (_PYOBJ, [_PYOBJ, _I64], False),
    "py_str_rsplit_maxsplit": (_PYOBJ, [_PYOBJ, _PYOBJ, _I64], False),
    "py_str_split_maxsplit": (_PYOBJ, [_PYOBJ, _PYOBJ, _I64], False),
    "py_str_splitlines": (_PYOBJ, [_PYOBJ], False),
    "py_str_splitlines_keepends": (_PYOBJ, [_PYOBJ, _I32], False),
    "py_str_lstrip": (_PYOBJ, [_PYOBJ], False),
    "py_str_rstrip": (_PYOBJ, [_PYOBJ], False),
    "py_str_strip_chars": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_str_lstrip_chars": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_str_rstrip_chars": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_str_count": (_I64, [_PYOBJ, _PYOBJ], False),
    }


def _runtime_signatures_part_9():
    return {
    "py_str_count_range": (_I64, [_PYOBJ, _PYOBJ, _PYOBJ, _PYOBJ], False),
    "py_str_isdigit": (_I64, [_PYOBJ], False),
    "py_str_isalpha": (_I64, [_PYOBJ], False),
    "py_str_isspace": (_I64, [_PYOBJ], False),
    "py_str_isalnum": (_I64, [_PYOBJ], False),
    "py_str_isupper": (_I64, [_PYOBJ], False),
    "py_str_islower": (_I64, [_PYOBJ], False),
    "py_str_isascii": (_I64, [_PYOBJ], False),
    "py_str_isidentifier": (_I64, [_PYOBJ], False),
    "py_str_isprintable": (_I64, [_PYOBJ], False),
    "py_str_isnumeric": (_I64, [_PYOBJ], False),
    "py_str_isdecimal": (_I64, [_PYOBJ], False),
    "py_str_istitle": (_I64, [_PYOBJ], False),
    "py_str_index_of": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_str_rindex_of": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_str_index_of_range": (_I64, [_PYOBJ, _PYOBJ, _I64, _I64], False),
    "py_str_rindex_of_range": (_I64, [_PYOBJ, _PYOBJ, _I64, _I64], False),
    "py_str_join": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_str_replace": (_PYOBJ, [_PYOBJ, _PYOBJ, _PYOBJ], False),
    "py_str_replace_count": (_PYOBJ, [_PYOBJ, _PYOBJ, _PYOBJ, _I64], False),
    "py_str_startswith": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_str_endswith": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_chr_from_i64": (_PYOBJ, [_I64], False),
    "py_json_loads": (_PYOBJ, [_PYOBJ], False),
    "py_json_dumps": (_PYOBJ, [_PYOBJ], False),
    "py_json_dumps_ex": (_PYOBJ, [_PYOBJ, _I64], False),
    "py_copy_copy": (_PYOBJ, [_PYOBJ], False),
    "py_copy_deepcopy": (_PYOBJ, [_PYOBJ], False),
    "py_pickle_dumps": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_pickle_loads": (_PYOBJ, [_PYOBJ], False),
    "py_os_urandom": (_PYOBJ, [_PYOBJ], False),
    # ---- List ------------------------------------------------------
    "py_list_new": (_PYOBJ, [_I64], False),
    "py_list_append": (_VOID, [_PYOBJ, _PYOBJ], False),
    "py_list_get": (_PYOBJ, [_PYOBJ, _I64], False),
    "py_list_getitem": (_PYOBJ, [_PYOBJ, _I64], False),
    "py_list_get_i64": (_I64, [_PYOBJ, _I64], False),
    "py_list_get_i64_nonnegative": (_I64, [_PYOBJ, _I64], False),
    "py_list_set": (_VOID, [_PYOBJ, _I64, _PYOBJ], False),
    "py_list_set_from_owned_root": (_VOID, [_PYOBJ, _I64, _PTR, _PTR], False),
    "py_list_get_for_frame": (_PYOBJ, [_PYOBJ, _I64], False),
    "py_list_setitem": (_I64, [_PYOBJ, _I64, _PYOBJ], False),
    "py_list_len": (_I64, [_PYOBJ], False),
    "py_list_slice": (_PYOBJ, [_PYOBJ, _PYOBJ, _PYOBJ, _PYOBJ], False),
    "py_list_set_slice": (
        _I64,
        [_PYOBJ, _PYOBJ, _PYOBJ, _PYOBJ, _PYOBJ],
        False,
    ),
    "py_list_del_slice": (_I64, [_PYOBJ, _PYOBJ, _PYOBJ, _PYOBJ], False),
    "py_list_concat": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_list_copy": (_PYOBJ, [_PYOBJ], False),
    "py_list_repeat": (_PYOBJ, [_PYOBJ, _I64], False),
    "py_list_extend": (_VOID, [_PYOBJ, _PYOBJ], False),
    "py_list_insert": (_VOID, [_PYOBJ, _I64, _PYOBJ], False),
    "py_list_pop": (_PYOBJ, [_PYOBJ, _I64], False),
    "py_list_remove": (_VOID, [_PYOBJ, _PYOBJ], False),
    }


def _runtime_signatures_part_10():
    return {
    "py_list_clear": (_VOID, [_PYOBJ], False),
    "py_obj_clear": (_VOID, [_PYOBJ], False),
    "py_list_contains": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_list_index": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_list_index_range": (_I64, [_PYOBJ, _PYOBJ, _I64, _I64], False),
    "py_list_count": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_list_reverse": (_VOID, [_PYOBJ], False),
    # ---- Dict ------------------------------------------------------
    "py_dict_new": (_PYOBJ, [], False),
    "py_dict_set": (_VOID, [_PYOBJ, _PYOBJ, _PYOBJ], False),
    "py_dict_get": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_dict_getitem": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_dict_fromkeys": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_dict_get_default": (_PYOBJ, [_PYOBJ, _PYOBJ, _PYOBJ], False),
    "py_dict_pop": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_dict_popitem": (_PYOBJ, [_PYOBJ], False),
    "py_dict_contains": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_dict_del": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_dict_clear": (_VOID, [_PYOBJ], False),
    "py_dict_len": (_I64, [_PYOBJ], False),
    "py_dict_entries_used": (_I64, [_PYOBJ], False),
    "py_dict_entry_key_at": (_PYOBJ, [_PYOBJ, _I64], False),
    "py_dict_entry_value_at": (_PYOBJ, [_PYOBJ, _I64], False),
    "py_dict_keys": (_PYOBJ, [_PYOBJ], False),
    "py_dict_values": (_PYOBJ, [_PYOBJ], False),
    "py_dict_items": (_PYOBJ, [_PYOBJ], False),
    "py_dict_update": (_VOID, [_PYOBJ, _PYOBJ], False),
    # ---- Tuple -----------------------------------------------------
    "py_tuple_new": (_PYOBJ, [_I64], False),
    "py_tuple_from_list": (_PYOBJ, [_PYOBJ], False),
    "py_tuple_from_splat": (_PYOBJ, [_PYOBJ], False),
    "py_tuple_set_item": (_VOID, [_PYOBJ, _I64, _PYOBJ], False),
    "py_tuple_get": (_PYOBJ, [_PYOBJ, _I64], False),
    "py_tuple_get_known": (_PYOBJ, [_PYOBJ, _I64], False),
    "py_tuple_getitem": (_PYOBJ, [_PYOBJ, _I64], False),
    "py_tuple_len": (_I64, [_PYOBJ], False),
    "py_tuple_concat": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_tuple_repeat": (_PYOBJ, [_PYOBJ, _I64], False),
    "py_tuple_slice": (_PYOBJ, [_PYOBJ, _PYOBJ, _PYOBJ, _PYOBJ], False),
    # ---- Descriptor wrappers --------------------------------------
    "py_property_new": (_PYOBJ, [_PYOBJ, _PYOBJ, _PYOBJ], False),
    "py_classmethod_new": (_PYOBJ, [_PYOBJ], False),
    "py_slice_new": (_PYOBJ, [_PYOBJ, _PYOBJ, _PYOBJ], False),
    # ---- Set -------------------------------------------------------
    "py_set_new": (_PYOBJ, [], False),
    "py_set_add": (_VOID, [_PYOBJ, _PYOBJ], False),
    "py_set_update": (_VOID, [_PYOBJ, _PYOBJ], False),
    "py_set_intersection": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_set_difference": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_set_symmetric_difference": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_set_intersection_update": (_VOID, [_PYOBJ, _PYOBJ], False),
    "py_set_difference_update": (_VOID, [_PYOBJ, _PYOBJ], False),
    "py_set_symmetric_difference_update": (_VOID, [_PYOBJ, _PYOBJ], False),
    "py_set_issubset": (_I64, [_PYOBJ, _PYOBJ], False),
    }


def _runtime_signatures_part_11():
    return {
    "py_set_issuperset": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_set_pop": (_PYOBJ, [_PYOBJ], False),
    "py_set_contains": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_set_remove": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_set_len": (_I64, [_PYOBJ], False),
    # ---- Generic object ops ---------------------------------------
    "py_obj_call": (_PYOBJ, [_PYOBJ, _PYOBJ, _PYOBJ], False),
    "py_obj_call_method1": (_PYOBJ, [_PYOBJ, _CSTR, _PYOBJ], False),
    "py_obj_add": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_obj_sub": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_obj_mul": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_obj_and": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_obj_or": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_obj_xor": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_obj_lshift": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_obj_rshift": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_obj_truediv": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_obj_mod": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_str_mod": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_weakref_new": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_weakref_call": (_PYOBJ, [_PYOBJ], False),
    "py_weakref_invalidate": (_VOID, [_PYOBJ], False),
    "py_weak_value_dict_new": (_PYOBJ, [], False),
    "py_weak_value_dict_set": (_I64, [_PYOBJ, _PYOBJ, _PYOBJ], False),
    "py_weak_value_dict_contains": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_weak_value_dict_len": (_I64, [_PYOBJ], False),
    "py_weak_key_dict_new": (_PYOBJ, [], False),
    "py_weak_key_dict_set": (_I64, [_PYOBJ, _PYOBJ, _PYOBJ], False),
    "py_weak_key_dict_len": (_I64, [_PYOBJ], False),
    "py_obj_getattr": (_PYOBJ, [_PYOBJ, _CSTR], False),
    "py_obj_getattr_default": (_PYOBJ, [_PYOBJ, _CSTR], False),
    "py_obj_getattr_maybe": (_PYOBJ, [_PYOBJ, _CSTR], False),
    "py_obj_vars": (_PYOBJ, [_PYOBJ], False),
    "py_obj_setattr": (_I64, [_PYOBJ, _CSTR, _PYOBJ], False),
    "py_obj_delattr": (_I64, [_PYOBJ, _CSTR], False),
    "py_obj_type_name": (_PYOBJ, [_PYOBJ], False),
    "py_type_builtin": (_PYOBJ, [_PYOBJ], False),
    "py_builtin_type_for_tag": (_PYOBJ, [_I64], False),
    "py_obj_getitem": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_obj_getitem_i64": (_PYOBJ, [_PYOBJ, _I64], False),
    "py_obj_subscript": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_obj_subscript_i64": (_PYOBJ, [_PYOBJ, _I64], False),
    "py_obj_slice": (_PYOBJ, [_PYOBJ, _PYOBJ, _PYOBJ, _PYOBJ], False),
    "py_obj_set_slice": (
        _I64,
        [_PYOBJ, _PYOBJ, _PYOBJ, _PYOBJ, _PYOBJ],
        False,
    ),
    "py_obj_del_slice": (_I64, [_PYOBJ, _PYOBJ, _PYOBJ, _PYOBJ], False),
    "py_obj_setitem": (_I64, [_PYOBJ, _PYOBJ, _PYOBJ], False),
    "py_obj_setitem_i64": (_I64, [_PYOBJ, _I64, _PYOBJ], False),
    "py_obj_delitem": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_obj_len": (_I64, [_PYOBJ], False),
    "py_obj_contains": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_obj_sorted": (_PYOBJ, [_PYOBJ], False),
    }


def _runtime_signatures_part_12():
    return {
    "py_obj_index_i64": (_I64, [_PYOBJ], False),
    "py_obj_repr": (_PYOBJ, [_PYOBJ], False),
    "py_obj_ascii": (_PYOBJ, [_PYOBJ], False),
    "py_obj_str": (_PYOBJ, [_PYOBJ], False),
    "py_obj_isinstance": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_obj_issubclass": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_obj_is_slice": (_I64, [_PYOBJ], False),
    "py_obj_iter": (_PYOBJ, [_PYOBJ], False),
    "py_obj_next": (_PYOBJ, [_PYOBJ], False),
    "py_iter_callable_new": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_user_str_dispatch": (_PYOBJ, [_PYOBJ], False),
    "py_user_repr_dispatch": (_PYOBJ, [_PYOBJ], False),
    "py_user_hash_dispatch": (_I64, [_PYOBJ, _PTR], False),
    "py_user_iter_dispatch": (_PYOBJ, [_PYOBJ], False),
    "py_user_next_dispatch": (_PYOBJ, [_PYOBJ], False),
    "py_user_matmul_dispatch": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_call_merge_posargs": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_call_merge_kwargs": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_zip_star": (_PYOBJ, [_PYOBJ], False),
    "py_obj_call_splat": (
        _PYOBJ,
        [_PYOBJ, _PYOBJ, _PYOBJ, _PYOBJ, _PYOBJ],
        False,
    ),
    "py_module_attrs_dict": (_PYOBJ, [_CSTR, _I64], False),
    "py_module_attr_set": (_I64, [_CSTR, _CSTR, _PYOBJ], False),
    "py_module_attr_get": (_PYOBJ, [_CSTR, _CSTR], False),
    "py_module_import_star": (_I64, [_CSTR, _PYOBJ], False),
    "py_compiled_module_register_init": (_I64, [_CSTR, _PTR], False),
    "py_compiled_module_import_by_name": (_PYOBJ, [_CSTR], False),
    "py_builtin_import": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_module_attr_value_or_default": (_PYOBJ, [_CSTR_PTR, _PYOBJ], False),
    "py_module_attr_del": (_I64, [_CSTR, _CSTR], False),
    "py_module_attr_len": (_I64, [_CSTR], False),
    "py_native_extension_import": (_PYOBJ, [_CSTR, _CSTR], False),
    "py_native_extension_import_by_name": (_PYOBJ, [_CSTR], False),
    # ---- Native function objects -----------------------------------
    "py_func_new": (_PYOBJ, [_PTR, _PYOBJ], False),
    "py_func_new_named": (_PYOBJ, [_PTR, _PYOBJ, _CSTR], False),
    "py_func_new_bound": (_PYOBJ, [_PTR, _PYOBJ, _CSTR, _PYOBJ], False),
    "py_func_call": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_func_call_kwargs": (_PYOBJ, [_PYOBJ, _PYOBJ, _PYOBJ], False),
    "py_functools_partial": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_functools_partial_kw": (_PYOBJ, [_PYOBJ, _PYOBJ, _PYOBJ], False),
    "py_functools_update_wrapper": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    # ---- Native generator objects ----------------------------------
    "py_gen_frame_new": (_PYOBJ, [_I64], False),
    "py_gen_frame_get": (_PYOBJ, [_PYOBJ, _I64], False),
    "py_gen_frame_set": (_VOID, [_PYOBJ, _I64, _PYOBJ], False),
    "py_gen_frame_save": (_I64, [_PYOBJ, _PTR, _I64], False),
    "py_gen_new": (_PYOBJ, [_PTR, _PYOBJ], False),
    "py_gen_completed": (_PYOBJ, [_PYOBJ], False),
    "py_gen_take_completed": (_PYOBJ, [_PYOBJ], False),
    "py_gen_set_may_park": (_VOID, [_PYOBJ], False),
    "py_gen_is_may_park": (_I64, [_PYOBJ], False),
    "py_gen_next": (_PYOBJ, [_PYOBJ], False),
    "py_gen_send": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_gen_throw": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_gen_close": (_PYOBJ, [_PYOBJ], False),
    "py_gen_close_preserving_exception": (_I64, [_PYOBJ], False),
    "py_gen_take_send": (_PYOBJ, [_PYOBJ], False),
    "py_gen_state": (_I64, [_PYOBJ], False),
    }


def _runtime_signatures_part_13():
    return {
    "py_gen_set_state": (_VOID, [_PYOBJ, _I64], False),
    "py_gen_set_done": (_VOID, [_PYOBJ], False),
    "py_gen_is_done": (_I64, [_PYOBJ], False),
    "py_gen_finish": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    # ---- Native coroutine shell objects ----------------------------
    "py_coroutine_new": (_PYOBJ, [_CSTR], False),
    "py_coroutine_new_native": (_PYOBJ, [_CSTR, _PTR, _PYOBJ, _PYOBJ], False),
    "py_coroutine_new_resumable": (_PYOBJ, [_CSTR, _PTR, _PYOBJ, _PYOBJ], False),
    "py_coroutine_send": (_PYOBJ, [_PYOBJ, _PYOBJ, _PYOBJ], False),
    "py_await_iterator": (_PYOBJ, [_PYOBJ], False),
    "py_await_step": (_PYOBJ, [_PYOBJ, _PYOBJ, _PYOBJ], False),
    "py_coroutine_run": (_PYOBJ, [_PYOBJ], False),
    "py_coroutine_close": (_PYOBJ, [_PYOBJ], False),
    "py_coroutine_class": (_PYOBJ, [], False),
    "py_coroutine_is_done": (_I64, [_PYOBJ], False),
    "py_coroutine_get_result": (_PYOBJ, [_PYOBJ], False),
    "py_await": (_PYOBJ, [_PYOBJ], False),
    "py_asyncio_sleep": (_PYOBJ, [_PYOBJ], False),
    "py_continuation_class": (_PYOBJ, [], False),
    "py_continuation_new": (_PYOBJ, [_PTR, _PTR, _PTR], False),
    "py_continuation_new_typed": (_PYOBJ, [_PTR, _PTR, _PTR], False),
    "py_continuation_mount": (_I64, [_PYOBJ, _PTR], False),
    "py_continuation_unmount": (_I64, [_PYOBJ, _PTR, _PTR], False),
    "py_continuation_is_mounted": (_I64, [_PYOBJ], False),
    "py_continuation_resume_pc": (_PTR, [_PYOBJ], False),
    "py_continuation_resume_abi": (_I64, [_PYOBJ], False),
    "py_continuation_slot_count": (_I64, [_PYOBJ], False),
    "py_continuation_get_slot": (_PYOBJ, [_PYOBJ, _I64], False),
    "py_continuation_set_slot": (_I64, [_PYOBJ, _I64, _PYOBJ], False),
    "py_virtual_thread_current": (_PYOBJ, [], False),
    "py_virtual_thread_resume_generator": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_virtual_thread_new": (_PYOBJ, [_PYOBJ], False),
    "py_virtual_thread_start": (_I64, [_PYOBJ], False),
    "py_virtual_thread_park": (_I64, [_PYOBJ], False),
    "py_virtual_thread_unpark": (_I64, [_PYOBJ], False),
    "py_virtual_thread_sleep": (_I64, [_PYOBJ, _I64], False),
    "py_virtual_thread_cancel": (_I64, [_PYOBJ], False),
    "py_virtual_thread_cancel_requested": (_I64, [_PYOBJ], False),
    "py_virtual_thread_cancel_complete": (_I64, [_PYOBJ], False),
    "py_virtual_thread_cancel_timer": (_I64, [_PYOBJ], False),
    "py_virtual_thread_poll_timers": (_I64, [], False),
    "py_virtual_thread_timer_count": (_I64, [], False),
    "py_virtual_thread_block_on_fd": (_I64, [_PYOBJ, _I64, _I64, _I64], False),
    "py_virtual_thread_poll_io": (_I64, [_I64], False),
    "py_virtual_thread_io_wait_count": (_I64, [], False),
    "py_virtual_thread_io_wait_active": (_I64, [], False),
    "py_virtual_thread_io_resource_register": (_I64, [_I64], False),
    "py_virtual_thread_io_resource_generation": (_I64, [_I64], False),
    "py_virtual_thread_io_resource_operation_begin": (
        _I64,
        [_I64, _I64],
        False,
    ),
    "py_virtual_thread_io_resource_operation_end": (_VOID, [], False),
    "py_virtual_thread_io_resource_close_begin": (_I64, [_I64], False),
    "py_virtual_thread_block_on_fd_generation": (
        _I64,
        [_PYOBJ, _I64, _I64, _I64, _I64],
        False,
    ),
    "py_virtual_thread_tcp_listen": (_I64, [_PYOBJ, _PYOBJ, _I64], False),
    "py_virtual_thread_tcp_accept_observe": (
        _I64,
        [_I64, _I64, _PTR],
        False,
    ),
    "py_virtual_thread_tcp_register_accepted": (_I64, [_I64], False),
    }


def _runtime_signatures_part_14():
    return {
    "py_virtual_thread_tcp_connect_start": (
        _I64,
        [_PYOBJ, _PYOBJ, _PTR],
        False,
    ),
    "py_virtual_thread_tcp_connect_observe": (_I64, [_I64, _I64], False),
    "py_virtual_thread_tcp_recv_observe": (
        _PYOBJ,
        [_I64, _I64, _I64, _PTR],
        False,
    ),
    "py_virtual_thread_tcp_send_observe": (
        _I64,
        [_I64, _I64, _PYOBJ, _I64, _PTR],
        False,
    ),
    "py_virtual_thread_tcp_close": (_I64, [_I64], False),
    "py_virtual_thread_tcp_close_quiet": (_I64, [_I64], False),
    "py_virtual_thread_tcp_deadline": (_I64, [_I64], False),
    "py_virtual_thread_tcp_remaining": (_I64, [_I64], False),
    "py_virtual_thread_tcp_raise_timeout": (_I64, [], False),
    "py_virtual_thread_io_backend": (_I64, [], False),
    "py_virtual_thread_pin_enter": (_I64, [_PYOBJ, _CSTR], False),
    "py_virtual_thread_pin_leave": (_I64, [_PYOBJ], False),
    "py_virtual_thread_pin_count": (_I64, [_PYOBJ], False),
    "py_virtual_thread_pinned_count": (_I64, [], False),
    "py_virtual_thread_pin_event_count": (_I64, [], False),
    "py_virtual_thread_poll_ready": (_PYOBJ, [], False),
    "py_virtual_thread_ready_count": (_I64, [], False),
    "py_virtual_thread_node_pool_stat": (_I64, [_I64, _I64], False),
    "py_virtual_thread_effect_reset": (_I64, [], False),
    "py_virtual_thread_effect_count": (_I64, [], False),
    "py_virtual_thread_effect_dropped": (_I64, [], False),
    "py_virtual_thread_effect_kind_at": (_I64, [_I64], False),
    "py_virtual_thread_effect_detail_at": (_I64, [_I64], False),
    "py_virtual_thread_effect_root_delta_at": (_I64, [_I64], False),
    "py_virtual_thread_effect_state_at": (_I64, [_I64], False),
    "py_virtual_thread_carrier_count": (_I64, [], False),
    "py_virtual_thread_carrier_steal_count": (_I64, [], False),
    "py_virtual_thread_run_once": (_I64, [], False),
    "py_virtual_thread_run_until_idle": (_I64, [_I64], False),
    "py_virtual_thread_run_carrier_pool": (_I64, [_I64, _I64], False),
    "py_virtual_thread_carrier_pool_start": (_I64, [_I64], False),
    "py_virtual_thread_carrier_pool_stop": (_I64, [], False),
    "py_virtual_thread_state": (_I64, [_PYOBJ], False),
    "py_virtual_thread_complete": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_virtual_thread_fail": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_virtual_thread_join": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_virtual_thread_join_result": (_PYOBJ, [_PYOBJ], False),
    "py_virtual_thread_channel_mpsc": (_PYOBJ, [_I64], False),
    "py_virtual_thread_channel_oneshot": (_PYOBJ, [], False),
    "py_virtual_thread_channel_sender_clone": (_PYOBJ, [_PYOBJ], False),
    "py_virtual_thread_channel_send_begin": (
        _I64,
        [_PYOBJ, _PYOBJ, _PYOBJ],
        False,
    ),
    "py_virtual_thread_channel_send_result": (_I64, [_PYOBJ], False),
    "py_virtual_thread_channel_recv_begin": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_virtual_thread_channel_recv_result": (_PYOBJ, [_PYOBJ], False),
    "py_virtual_thread_channel_close_sender": (_I64, [_PYOBJ], False),
    "py_virtual_thread_channel_close_receiver": (_I64, [_PYOBJ], False),
    "py_virtual_thread_channel_select2_begin": (
        _I64,
        [_PYOBJ, _PYOBJ, _PYOBJ],
        False,
    ),
    "py_virtual_thread_channel_select2_result": (_PYOBJ, [_PYOBJ], False),
    "py_virtual_thread_result": (_PYOBJ, [_PYOBJ], False),
    "py_virtual_thread_exception": (_PYOBJ, [_PYOBJ], False),
    }


def _runtime_signatures_part_15():
    return {
    "py_virtual_thread_outcome": (_I64, [_PYOBJ], False),
    "py_task_new": (_PYOBJ, [_PYOBJ], False),
    "py_task_step": (_PYOBJ, [_PYOBJ], False),
    "py_task_is_done": (_I64, [_PYOBJ], False),
    "py_task_set_result": (_VOID, [_PYOBJ, _PYOBJ], False),
    "py_task_set_waiter": (_VOID, [_PYOBJ, _PYOBJ], False),
    "py_task_get_coro": (_PYOBJ, [_PYOBJ], False),
    "py_task_get_result": (_PYOBJ, [_PYOBJ], False),
    "py_task_get_waiter": (_PYOBJ, [_PYOBJ], False),
    "py_context_enter": (_PYOBJ, [_PYOBJ], False),
    "py_context_exit": (_I64, [_PYOBJ, _PYOBJ, _PYOBJ, _PYOBJ], False),
    "py_obj_format": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    # ---- File I/O --------------------------------------------------
    "py_file_open": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_file_read_all": (_PYOBJ, [_PYOBJ], False),
    "py_file_read": (_PYOBJ, [_PYOBJ, _I64], False),
    "py_file_write": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_file_close": (_VOID, [_PYOBJ], False),
    "py_file_readline": (_PYOBJ, [_PYOBJ, _I64], False),
    "py_file_seek": (_PYOBJ, [_PYOBJ, _I64, _I64], False),
    "py_file_tell": (_PYOBJ, [_PYOBJ], False),
    "py_file_flush": (_PYOBJ, [_PYOBJ], False),
    "py_file_fileno": (_PYOBJ, [_PYOBJ], False),
    "py_fileinput_new": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_fileinput_readline": (_PYOBJ, [_PYOBJ], False),
    "py_fileinput_filename": (_PYOBJ, [_PYOBJ], False),
    "py_fileinput_lineno": (_PYOBJ, [_PYOBJ], False),
    "py_fileinput_filelineno": (_PYOBJ, [_PYOBJ], False),
    "py_fileinput_isfirstline": (_PYOBJ, [_PYOBJ], False),
    "py_fileinput_close": (_PYOBJ, [_PYOBJ], False),
    # ---- Printing --------------------------------------------------
    "py_print": (_VOID, [_PYOBJ], False),
    "py_print_many": (_VOID, [_PYOBJ, _PYOBJ, _PYOBJ], False),
    "py_sys_stdout_write": (_PYOBJ, [_PYOBJ], False),
    "py_sys_stderr_write": (_PYOBJ, [_PYOBJ], False),
    # ---- Process startup -------------------------------------------
    "py_set_program_args": (_VOID, [_I32, _CSTR_PTR], False),
    "py_program_argc": (_I64, [], False),
    "py_program_argv": (_CSTR, [_I64], False),
    "py_program_executable": (_CSTR, [], False),
    "py_program_mode": (_I32, [], False),
    "py_process_exit": (_VOID, [_I64], False),
    "py_sys_executable_str": (_PYOBJ, [], False),
    "py_sys_prefix_str": (_PYOBJ, [_I64], False),
    "py_os_getpid": (_PYOBJ, [], False),
    "py_subprocess_check_output": (_PYOBJ, [_PYOBJ], False),
    "py_subprocess_run": (_I64, [_PYOBJ, _I32], False),
    "py_subprocess_run_timeout": (_I64, [_PYOBJ, _I32, _I64], False),
    "py_sysconfig_get_config_var": (_PYOBJ, [_PYOBJ], False),
    "py_os_listdir": (_PYOBJ, [_PYOBJ], False),
    "py_shlex_split": (_PYOBJ, [_PYOBJ], False),
    "py_shutil_which": (_PYOBJ, [_PYOBJ], False),
    "py_tempdir_new": (_PYOBJ, [_PYOBJ], False),
    }


def _runtime_signatures_part_16():
    return {
    "py_tempdir_cleanup": (_VOID, [_PYOBJ], False),
    "py_re_escape": (_PYOBJ, [_PYOBJ], False),
    "py_re_match": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_re_match_flags": (_PYOBJ, [_PYOBJ, _PYOBJ, _I64], False),
    "py_re_fullmatch": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_re_fullmatch_flags": (_PYOBJ, [_PYOBJ, _PYOBJ, _I64], False),
    "py_re_search": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_re_search_flags": (_PYOBJ, [_PYOBJ, _PYOBJ, _I64], False),
    "py_re_findall_flags": (_PYOBJ, [_PYOBJ, _PYOBJ, _I64], False),
    "py_re_compile_method": (_PYOBJ, [_PYOBJ, _I64, _I64], False),
    "py_re_compile_obj": (_PYOBJ, [_PYOBJ, _I64], False),
    "py_re_engine_sub": (_PYOBJ, [_PYOBJ, _PYOBJ, _PYOBJ, _I64, _I64], False),
    "py_re_engine_split": (_PYOBJ, [_PYOBJ, _PYOBJ, _I64, _I64], False),
    "py_time_monotonic": (_PYOBJ, [], False),
    "py_time_perf_counter": (_PYOBJ, [], False),
    "py_time_time": (_PYOBJ, [], False),
    "py_time_strftime": (_PYOBJ, [_PYOBJ], False),
    "py_sys_stdin_readline": (_PYOBJ, [], False),
    # ---- Narrow os.path subset ------------------------------------
    "py_os_getenv": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_os_putenv": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_os_unsetenv": (_PYOBJ, [_PYOBJ], False),
    "py_os_environ_getitem": (_PYOBJ, [_PYOBJ], False),
    "py_os_environ_setitem": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_os_environ_contains": (_I32, [_PYOBJ], False),
    "py_os_path_join": (_PYOBJ, [_PYOBJ], False),
    "py_os_path_basename": (_PYOBJ, [_PYOBJ], False),
    "py_os_path_dirname": (_PYOBJ, [_PYOBJ], False),
    "py_os_path_split": (_PYOBJ, [_PYOBJ], False),
    "py_os_path_splitext": (_PYOBJ, [_PYOBJ], False),
    "py_os_path_normcase": (_PYOBJ, [_PYOBJ], False),
    "py_os_path_normpath": (_PYOBJ, [_PYOBJ], False),
    "py_os_path_splitdrive": (_PYOBJ, [_PYOBJ], False),
    "py_os_path_exists": (_I32, [_PYOBJ], False),
    "py_os_path_isabs": (_I32, [_PYOBJ], False),
    "py_os_path_isfile": (_I32, [_PYOBJ], False),
    "py_os_path_isdir": (_I32, [_PYOBJ], False),
    "py_os_path_getmtime": (_PYOBJ, [_PYOBJ], False),
    "py_os_path_abspath": (_PYOBJ, [_PYOBJ], False),
    "py_os_path_expanduser": (_PYOBJ, [_PYOBJ], False),
    "py_os_path_realpath": (_PYOBJ, [_PYOBJ], False),
    "py_os_path_commonpath": (_PYOBJ, [_PYOBJ], False),
    "py_os_path_expandvars": (_PYOBJ, [_PYOBJ], False),
    "py_os_path_relpath": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_os_path_commonprefix": (_PYOBJ, [_PYOBJ], False),
    "py_os_getcwd_str": (_PYOBJ, [], False),
    "py_os_makedirs": (_PYOBJ, [_PYOBJ, _I64, _I32], False),
    "py_os_unlink": (_PYOBJ, [_PYOBJ], False),
    "py_os_replace": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_os_chmod": (_PYOBJ, [_PYOBJ, _I64], False),
    "py_os_fsync": (_PYOBJ, [_I64], False),
    }


def _runtime_signatures_part_17():
    return {
    "py_os_access": (_I32, [_PYOBJ, _I32], False),
    "py_os_write": (_I32, [_I32, _PYOBJ], False),
    "py_http_download_to_file": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_sha256_file_hex": (_PYOBJ, [_PYOBJ], False),
    "py_sha256_file_hex_bounded": (_PYOBJ, [_PYOBJ, _I64], False),
    "py_os_cpu_count": (_PYOBJ, [], False),
    "py_os_uname": (_PYOBJ, [], False),
    "py_sys_platform_str": (_PYOBJ, [], False),
    "py_sys_path_list": (_PYOBJ, [], False),
    "py_platform_machine_str": (_PYOBJ, [], False),
    "py_platform_release_str": (_PYOBJ, [], False),
    # ---- Exceptions (Phase 3) -------------------------------------
    "py_raise": (_VOID, [_PYOBJ], False),
    "py_current_exception": (_PYOBJ, [], False),
    "py_clear_exception": (_VOID, [], False),
    "py_exc_new": (_PYOBJ, [_I64, _CSTR], False),
    "py_exc_new_with_value": (_PYOBJ, [_I64, _PYOBJ], False),
    "py_exc_new_with_class": (_PYOBJ, [_PYOBJ, _CSTR], False),
    # py_exc_builtin_class(tag) -> PyClassObject* for a builtin class tag.
    # i64 to match the pcc-Python port's default int lowering.
    "py_exc_builtin_class": (_PYOBJ, [_I64], False),
    # py_exc_matches(exc, class) -> 0/1 (walks MRO). i64 to match the
    # pcc-Python port's default `int` lowering.
    "py_exc_matches": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_exc_set_cause": (_VOID, [_PYOBJ, _PYOBJ], False),
    "py_exc_set_context": (_VOID, [_PYOBJ, _PYOBJ], False),
    "py_exc_append_frame": (_VOID, [_PYOBJ, _CSTR, _CSTR, _I32], False),
    "py_exc_append_frame_source": (
        _VOID,
        [_PYOBJ, _CSTR, _CSTR, _CSTR, _I32],
        False,
    ),
    "py_exc_append_frame_indexed": (
        _VOID,
        [_PYOBJ, _CSTR, _CSTR, _CSTR, _CSTR, _I32],
        False,
    ),
    "py_runtime_error_if_unset": (_PYOBJ, [_CSTR, _CSTR], False),
    "py_exc_print_unhandled": (_VOID, [_PYOBJ], False),
    # traceback.format_exc / traceback.print_exc over the PyFrameRecord
    # trail. The exc argument is the retained handler exception (or
    # NULL when no exception is being handled).
    "py_exc_traceback_format_exc": (_PYOBJ, [_PYOBJ], False),
    "py_exc_traceback_print_exc": (_VOID, [_PYOBJ], False),
    "py_exc_get_message": (_PYOBJ, [_PYOBJ], False),
    "py_exc_get_cause": (_PYOBJ, [_PYOBJ], False),
    "py_exc_get_context": (_PYOBJ, [_PYOBJ], False),
    "py_exc_traceback_len": (_I64, [_PYOBJ], False),
    # Return-code exception check (post-call branch target). i64 to
    # match the pcc-Python port's default `int` lowering.
    "py_err_occurred": (_I64, [], False),
    # ---- Classes / Instances (Phase 3) ----------------------------
    # py_class_new(name: const char*,
    #              bases: PyClassObject**, n_bases: i32,
    #              field_names: const char**, n_fields: i32)
    #   -> PyClassObject*
    "py_class_new": (_PYOBJ, [_CSTR, _PTR, _I32, _PTR, _I32], False),
    "py_class_new_from_objects": (_PYOBJ, [_PYOBJ, _PYOBJ, _PYOBJ], False),
    "py_class_mark_slots_only": (_VOID, [_PYOBJ], False),
    "py_class_mark_dict_subclass": (_VOID, [_PYOBJ], False),
    "py_class_add_method": (_VOID, [_PYOBJ, _CSTR, _PYOBJ], False),
    "py_class_set_metaclass": (_VOID, [_PYOBJ, _PYOBJ], False),
    "py_class_lookup": (_PYOBJ, [_PYOBJ, _CSTR], False),
    "py_class_getattr": (_PYOBJ, [_PYOBJ, _CSTR], False),
    "py_class_setattr": (_I64, [_PYOBJ, _CSTR, _PYOBJ], False),
    "py_class_setattr_raw": (_I64, [_PYOBJ, _CSTR, _PYOBJ], False),
    "py_class_apply_namespace_dict": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_class_delattr": (_I64, [_PYOBJ, _CSTR], False),
    "py_valuebox_new": (_PYOBJ, [_PYOBJ], False),
    "py_valuebox_get_field": (_PYOBJ, [_PYOBJ, _I32], False),
    "py_valuebox_set_field": (_VOID, [_PYOBJ, _I32, _PYOBJ], False),
    "py_instance_new": (_PYOBJ, [_PYOBJ], False),
    "py_instance_get_field": (_PYOBJ, [_PYOBJ, _I32], False),
    }


def _runtime_signatures_part_18():
    return {
    "py_instance_getattr": (_PYOBJ, [_PYOBJ, _CSTR], False),
    "py_instance_getattr_default": (_PYOBJ, [_PYOBJ, _CSTR], False),
    "py_instance_setattr": (_I64, [_PYOBJ, _CSTR, _PYOBJ], False),
    "py_dataclass_replace": (_PYOBJ, [_PYOBJ, _I64, _PTR, _PTR], False),
    "py_dataclass_replace_from_dict": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_isinstance": (_I64, [_PYOBJ, _PYOBJ], False),
    "py_super_lookup": (_PYOBJ, [_PYOBJ, _PYOBJ, _CSTR], False),
    # ---- GC --------------------------------------------------------
    "py_gc_init": (_VOID, [], False),
    "py_gc_collect": (_I64, [], False),
    "py_gc_track": (_VOID, [_PYOBJ], False),
    "py_gc_untrack": (_VOID, [_PYOBJ], False),
    "py_gc_enable": (_VOID, [], False),
    "py_gc_disable": (_VOID, [], False),
    "py_gc_is_enabled": (_I64, [], False),
    "py_gc_is_tracked": (_I64, [_PYOBJ], False),
    "py_gc_get_count": (_I64, [_I32], False),
    "py_gc_get_threshold": (_I64, [_I32], False),
    "py_gc_set_threshold": (_VOID, [_I32, _I32, _I32], False),
    "py_gc_freeze": (_VOID, [], False),
    "py_gc_unfreeze": (_VOID, [], False),
    "py_gc_get_freeze_count": (_I64, [], False),
    "py_gc_get_objects": (_PYOBJ, [], False),
    "py_gc_get_referents": (_PYOBJ, [_PYOBJ], False),
    "py_gc_get_referrers": (_PYOBJ, [_PYOBJ], False),
    # ---- Phase 4: CPython C-API fallback ---------------------------
    # All CPython pointers show as ``i8*`` at the IR boundary; the
    # distinction from pcc ``PyObject*`` is tracked type-side only.
    "py_cpy_ensure_init": (_VOID, [], False),
    "py_cpy_import": (_PTR, [_CSTR], False),
    "py_cpy_getattr": (_PTR, [_PTR, _CSTR], False),
    "py_cpy_binop": (_PTR, [_I64, _PTR, _PTR], False),
    "py_cpy_setattr": (_I32, [_PTR, _CSTR, _PTR], False),
    "py_cpy_main_exitcode": (_I32, [], False),
    "py_cpy_call_noargs": (_PTR, [_PTR], False),
    "py_cpy_call1": (_PTR, [_PTR, _PTR], False),
    "py_cpy_call2": (_PTR, [_PTR, _PTR, _PTR], False),
    "py_cpy_call3": (_PTR, [_PTR, _PTR, _PTR, _PTR], False),
    # (callable, n, argv[]) — PyTuple_SetItem steals each ref in argv.
    "py_cpy_call_argv": (_PTR, [_PTR, _I64, _PTR], False),
    # (callable, n_pos, argv[], n_kw, kw_names[], kw_vals[]) — pos
    # refs stolen; kw refs borrowed. kw_names are C strings.
    "py_cpy_call_kw": (
        _PTR,
        [_PTR, _I64, _PTR, _I64, _PTR, _PTR],
        False,
    ),
    # (callable, n_pos, argv[], kwargs_dict) — pos refs stolen into the
    # tuple; kwargs_dict is borrowed and passed through to PyObject_Call.
    "py_cpy_call_kwdict": (_PTR, [_PTR, _I64, _PTR, _PTR], False),
    # Like py_cpy_call_kwdict, but merges explicit kw pairs into the
    # borrowed kwargs_dict before the call.
    "py_cpy_call_kwdict_plus": (
        _PTR,
        [_PTR, _I64, _PTR, _I64, _PTR, _PTR, _PTR],
        False,
    ),
    # (callable, args_pcc_list) — convert pcc list/tuple to CPython tuple
    # then PyObject_Call. Used for ``fn(*args)`` unpack at call sites.
    "py_cpy_call_list": (_PTR, [_PTR, _PYOBJ], False),
    # Like py_cpy_call_list, but forwards a borrowed CPython kwargs
    # mapping to PyObject_Call as well.
    "py_cpy_call_list_kwdict": (_PTR, [_PTR, _PYOBJ, _PTR], False),
    # (fn_ptr) — wrap a pcc user FuncDef (DynType-in / DynType-out) as
    # a CPython callable via PyCapsule + PyCFunction. Used when a
    # lambda or nested def is passed as a value to a CPython API
    # (``sorted(xs, key=<fn>)``, ``re.sub(pat, <repl>, text)``, etc.).
    "py_cpy_wrap_pcc_0arg": (_PTR, [_PTR], False),
    "py_cpy_wrap_pcc_1arg": (_PTR, [_PTR], False),
    "py_cpy_wrap_pcc_2arg": (_PTR, [_PTR], False),
    "py_cpy_wrap_pcc_3arg": (_PTR, [_PTR], False),
    "py_cpy_wrap_pcc_4arg": (_PTR, [_PTR], False),
    "py_cpy_wrap_pcc_5arg": (_PTR, [_PTR], False),
    "py_cpy_wrap_pcc_6arg": (_PTR, [_PTR], False),
    "py_cpy_wrap_pcc_7arg": (_PTR, [_PTR], False),
    "py_cpy_wrap_pcc_8arg": (_PTR, [_PTR], False),
    "py_cpy_wrap_pcc_9arg": (_PTR, [_PTR], False),
    }


def _runtime_signatures_part_19():
    return {
    "pcc_capi_str_utf8_pinned": (_CSTR, [_PYOBJ], False),
    "py_cpy_len": (_I64, [_PTR], False),
    "py_cpy_getitem": (_PTR, [_PTR, _PTR], False),
    "py_cpy_setitem": (_I32, [_PTR, _PTR, _PTR], False),
    "py_cpy_truthy": (_I32, [_PTR], False),
    "py_cpy_iter": (_PTR, [_PTR], False),
    "py_cpy_handle_new": (_PTR, [_PTR], False),
    "py_obj_floordiv": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_obj_inplace_op": (_PYOBJ, [_PYOBJ, _PYOBJ, _I64], False),
    "pcc_os_current_rss_bytes": (_I64, [], False),
    "pcc_os_peak_rss_bytes": (_I64, [], False),
    "pcc_os_heap_in_use_bytes": (_I64, [], False),
    "pcc_os_heap_capacity_bytes": (_I64, [], False),
    "py_enumerate_list": (_PYOBJ, [_PYOBJ, _I64], False),
    "py_int_to_bytes": (_PYOBJ, [_PYOBJ, _I64, _PYOBJ], False),
    "py_int_from_bytes": (_PYOBJ, [_PYOBJ, _PYOBJ], False),
    "py_cpy_handle_get": (_PTR, [_PTR], False),
    "py_cpy_iter_next": (_PTR, [_PTR], False),
    "py_cpy_to_pcc_str": (_PYOBJ, [_PTR], False),
    "py_cpy_to_pcc_obj": (_PYOBJ, [_PTR], False),
    "py_cpy_decref": (_VOID, [_PTR], False),
    "py_cpy_incref": (_VOID, [_PTR], False),
    "py_cpy_from_i64": (_PTR, [_I64], False),
    "py_cpy_to_i64": (_I64, [_PTR], False),
    "py_cpy_from_f64": (_PTR, [_DOUBLE], False),
    "py_cpy_to_f64": (_DOUBLE, [_PTR], False),
    "py_cpy_from_pccstr": (_PTR, [_PYOBJ], False),
    "py_cpy_from_pcc_obj": (_PTR, [_PYOBJ], False),
    "py_list_append_fresh_native_instance": (_VOID, [_PYOBJ, _PYOBJ], False),
    }


RUNTIME_SIGNATURES: dict[str, tuple[ir.Type, list[ir.Type], bool]] = (
    _runtime_signatures_part_0()
)
RUNTIME_SIGNATURES.update(_runtime_signatures_part_1())
RUNTIME_SIGNATURES.update(_runtime_signatures_part_2())
RUNTIME_SIGNATURES.update(_runtime_signatures_part_3())
RUNTIME_SIGNATURES.update(_runtime_signatures_part_4())
RUNTIME_SIGNATURES.update(_runtime_signatures_part_5())
RUNTIME_SIGNATURES.update(_runtime_signatures_part_6())
RUNTIME_SIGNATURES.update(_runtime_signatures_part_7())
RUNTIME_SIGNATURES.update(_runtime_signatures_part_8())
RUNTIME_SIGNATURES.update(_runtime_signatures_part_9())
RUNTIME_SIGNATURES.update(_runtime_signatures_part_10())
RUNTIME_SIGNATURES.update(_runtime_signatures_part_11())
RUNTIME_SIGNATURES.update(_runtime_signatures_part_12())
RUNTIME_SIGNATURES.update(_runtime_signatures_part_13())
RUNTIME_SIGNATURES.update(_runtime_signatures_part_14())
RUNTIME_SIGNATURES.update(_runtime_signatures_part_15())
RUNTIME_SIGNATURES.update(_runtime_signatures_part_16())
RUNTIME_SIGNATURES.update(_runtime_signatures_part_17())
RUNTIME_SIGNATURES.update(_runtime_signatures_part_18())
RUNTIME_SIGNATURES.update(_runtime_signatures_part_19())

_PURE_RUNTIME_ATTRS = frozenset({"readnone", "willreturn", "nounwind"})
_READONLY_RUNTIME_ATTRS = frozenset({"readonly", "willreturn", "nounwind"})
RUNTIME_FUNCTION_ATTRS: dict[str, frozenset[str]] = {
    "py_bool_from_bit": _PURE_RUNTIME_ATTRS,
    "py_str_byte_len": _READONLY_RUNTIME_ATTRS,
    "py_str_utf8": _READONLY_RUNTIME_ATTRS,
    "py_list_len": _READONLY_RUNTIME_ATTRS,
    "py_list_get_i64": _READONLY_RUNTIME_ATTRS,
    "py_list_get_i64_nonnegative": _READONLY_RUNTIME_ATTRS,
    "py_tuple_len": _READONLY_RUNTIME_ATTRS,
    "py_dict_len": _READONLY_RUNTIME_ATTRS,
    "py_set_len": _READONLY_RUNTIME_ATTRS,
    "py_current_exception": _READONLY_RUNTIME_ATTRS,
    "py_err_occurred": _READONLY_RUNTIME_ATTRS,
    "py_gc_is_enabled": _READONLY_RUNTIME_ATTRS,
    "py_gc_is_tracked": _READONLY_RUNTIME_ATTRS,
    "py_gc_get_count": _READONLY_RUNTIME_ATTRS,
    "py_gc_get_threshold": _READONLY_RUNTIME_ATTRS,
    "pcc_gc_backend": _READONLY_RUNTIME_ATTRS,
    "pcc_gc_backend_name": _READONLY_RUNTIME_ATTRS,
    "pcc_gc_telemetry": _READONLY_RUNTIME_ATTRS,
    "pcc_threads_enabled": _READONLY_RUNTIME_ATTRS,
    "pcc_refcount_strategy": _READONLY_RUNTIME_ATTRS,
}


# Global constants (extern) from py_runtime.h:
#   extern PyObject *const py_None;
#   extern PyObject *const py_True;
#   extern PyObject *const py_False;
RUNTIME_GLOBALS: dict[str, ir.Type] = {
    "py_None": _PYOBJ,
    "py_NotImplemented": _PYOBJ,
    "py_True": _PYOBJ,
    "py_False": _PYOBJ,
    "py_func_code_class_cache": _PYOBJ,
    "pcc_thread_stop_requested": _I32,
}

# Raw five-GC storage is consumed only through pcc.unsafe.  Keep its
# finite validator registry string-only: these symbols are not managed
# globals for declare_runtime_global, and pcc1 must not materialize 130
# extra live LLVM type objects while importing this module.
FREESTANDING_GC_I32_GLOBALS: frozenset[str] = frozenset(
    {
        'pcc_gc_backend0_frame_roots_enabled',
        'pcc_gc_backend3_frame_root_scan_phase',
        'pcc_gc_backend3_remembered_overflow',
        'pcc_gc_backend3_scheduler_root_scan_phase',
        'pcc_gc_backend4_evacuated_bytes_count',
        'pcc_gc_backend4_evacuation_candidate_bytes_count',
        'pcc_gc_backend4_evacuation_candidate_zpage_bytes_count',
        'pcc_gc_backend4_evacuation_candidates',
        'pcc_gc_backend4_evacuation_incomplete_batches_count',
        'pcc_gc_backend4_genzgc_store_barriers',
        'pcc_gc_backend4_large_object_deferred_bytes_count',
        'pcc_gc_backend4_large_object_defers',
        'pcc_gc_backend4_large_object_reconsiderations_count',
        'pcc_gc_backend4_medium_page_candidate_bytes_count',
        'pcc_gc_backend4_medium_page_candidate_zpage_bytes_count',
        'pcc_gc_backend4_medium_page_candidates',
        'pcc_gc_backend4_remap_active',
        'pcc_gc_backend4_selector_page_allow_large',
        'pcc_gc_backend4_selector_page_seed_pending',
        'pcc_gc_backend4_selector_scan_allow_large',
        'pcc_gc_backend4_selector_scan_require_unselected',
        'pcc_gc_backend4_selector_scan_restart',
        'pcc_gc_backend4_remembered_set_duplicate_skips_count',
        'pcc_gc_backend4_remembered_set_entries_count',
        'pcc_gc_backend4_remembered_set_high_water_count',
        'pcc_gc_backend4_small_page_candidate_bytes_count',
        'pcc_gc_backend4_small_page_candidate_zpage_bytes_count',
        'pcc_gc_backend4_small_page_candidates',
        'pcc_gc_backend4_store_buffer_drain_batches_count',
        'pcc_gc_backend4_store_buffer_drained_entries_count',
        'pcc_gc_backend4_store_buffer_duplicate_skips_count',
        'pcc_gc_backend4_store_buffer_entries_count',
        'pcc_gc_backend4_store_buffer_full_batches_count',
        'pcc_gc_backend4_store_buffer_high_water_count',
        'pcc_gc_backend4_store_buffer_incomplete_drains_count',
        'pcc_gc_backend4_store_buffer_max_batch_size_count',
        'pcc_gc_backend4_store_buffer_medium_count',
        'pcc_gc_backend4_store_buffer_medium_flushed_entries_count',
        'pcc_gc_backend4_store_buffer_medium_flushes_count',
        'pcc_gc_backend4_store_buffer_medium_full_flushes_count',
        'pcc_gc_backend4_store_buffer_owner_count_high_water_count',
        'pcc_gc_backend4_store_buffer_owner_fanout_high_water_count',
        'pcc_gc_backend4_young_promotions',
        'pcc_gc_backend4_zpage_node_free_count',
        'pcc_gc_backend_selected',
        'pcc_gc_cms_mutator_assists',
        'pcc_gc_cms_queue_pushes',
        'pcc_gc_cms_wb_flushes',
        'pcc_gc_cms_worker_drains',
        'pcc_gc_cms_worker_started',
        'pcc_gc_cms_worker_starts',
        'pcc_gc_cms_worker_stop_requested',
        'pcc_gc_cms_worker_stops',
        'pcc_gc_cms_worker_traces',
        'pcc_gc_config_initialized',
        'pcc_gc_cycle_requested',
        'pcc_gc_debt_bytes',
        'pcc_gc_debt_threshold_override',
        'pcc_gc_explicit_collect_active',
        'pcc_gc_forwarding_population',
        'pcc_gc_gray_count',
        'pcc_gc_in_auto_step',
        'pcc_gc_last_alloc_bytes',
        'pcc_gc_live_bytes',
        'pcc_gc_mark_active',
        'pcc_gc_metric_alloc',
        'pcc_gc_metric_load',
        'pcc_gc_metric_max_pause_us',
        'pcc_gc_metric_pause_count',
        'pcc_gc_metric_pause_hist0',
        'pcc_gc_metric_pause_hist1',
        'pcc_gc_metric_pause_hist2',
        'pcc_gc_metric_pause_hist3',
        'pcc_gc_metric_pause_sum_us',
        'pcc_gc_metric_pin',
        'pcc_gc_metric_safepoint',
        'pcc_gc_metric_step',
        'pcc_gc_metric_store',
        'pcc_gc_minor_alloc_max',
        'pcc_gc_minor_allocations',
        'pcc_gc_minor_arena_bumps',
        'pcc_gc_minor_arena_fallbacks',
        'pcc_gc_minor_arena_refills',
        'pcc_gc_minor_bytes',
        'pcc_gc_minor_collections',
        'pcc_gc_minor_heap_size',
        'pcc_gc_next_object_id',
        'pcc_gc_object_node_free_count',
        'pcc_gc_pause',
        'pcc_gc_read_barrier_enabled',
        'pcc_gc_relocation_barrier_forwards',
        'pcc_gc_relocation_forwards',
        'pcc_gc_relocation_pin_rejects',
        'pcc_gc_root_count',
        'pcc_gc_stepmul',
        'pcc_gc_trace_extension_roots_pending',
        'pcc_gc_tracked_node_pool_count',
        'py_class_attr_cache_epoch',
        'py_gc_callbacks_firing',
        'py_gc_collecting',
        'py_gc_enabled',
        'py_gc_freeze_count',
        'py_gc_threshold0',
        'py_gc_threshold1',
        'py_gc_threshold2',
        'py_gc_tracked_count',
    }
)

FREESTANDING_GC_I64_GLOBALS: frozenset[str] = frozenset(
    {
        'pcc_gc_backend3_remembered_owner_allocation_limit',
        'pcc_gc_backend3_frame_root_scan_slot',
        'pcc_gc_backend3_promotion_revision',
        'pcc_gc_backend3_promotion_probe_pause',
        'pcc_gc_backend3_promotion_probe_state_value',
        'pcc_gc_backend3_remembered_scan_revision',
        'pcc_gc_backend3_scheduler_root_scan_slot',
        'pcc_gc_backend4_deferred_recycle_pages',
        'pcc_gc_backend4_candidate_fresh_skips_g',
        'pcc_gc_backend4_relocation_add_refusals_g',
        'pcc_gc_backend4_remap_epoch',
        # PCC_GC_COUNTER_UNMANAGED_REFCOUNT_OPS (py_runtime.h:116).  Written
        # by both refcount-prepare mirrors, read by pcc_gc_telemetry.
        'pcc_gc_unmanaged_refcount_ops',
        'pcc_gc_backend4_relocation_reset_owner',
        'pcc_gc_backend4_reseed_plan_probe_allocation_limit',
        'pcc_gc_backend4_reseed_plan_probe_pause',
        'pcc_gc_backend4_reseed_plan_probe_state_value',
        'pcc_gc_backend4_reseed_page_count_owner',
        'pcc_gc_backend4_reseed_commit_owner',
        'pcc_gc_backend4_reseed_page_revision',
        'pcc_gc_backend4_reseed_relocation_revision',
        'pcc_gc_backend4_selector_page_owner',
        'pcc_gc_backend4_selector_scan_best_score',
        'pcc_gc_backend4_selector_scan_owner',
        'pcc_gc_table_lock_owner_token',
        'pcc_gc_tracing_cycle_epoch',
        'pcc_gc_trace_extension_roots_backend',
        'pcc_gc_trace_extension_roots_epoch',
        'pcc_gc_trace_cext_pending_backend',
        'pcc_gc_trace_cext_pending_epoch',
        'pcc_gc_tracing_finish_claim_backend',
        'pcc_gc_tracing_finish_claim_epoch',
        'pcc_gc_tracing_finish_commits',
        'pcc_gc_object_list_revision',
        'pcc_gc_root_registry_revision',
    }
)

FREESTANDING_GC_PTR_GLOBALS: frozenset[str] = frozenset(
    {
        'pcc_dealloc_trash_head',
        'pcc_gc_backend3_remembered_owner_head',
        'pcc_gc_backend3_continuation_root_scan_cursor',
        'pcc_gc_backend3_frame_root_scan_cursor',
        'pcc_gc_backend3_promotion_head',
        'pcc_gc_backend3_promotion_tail',
        'pcc_gc_backend3_remembered_scan_cursor',
        'pcc_gc_backend3_scheduler_root_scan_cursor',
        'pcc_gc_backend3_young_head',
        'pcc_gc_backend4_active_medium_old_page',
        'pcc_gc_backend4_active_medium_young_page',
        'pcc_gc_backend4_active_small_old_page',
        'pcc_gc_backend4_active_small_young_page',
        'pcc_gc_backend4_evacuation_page_head',
        'pcc_gc_backend4_free_page_head',
        'pcc_gc_backend4_page_head',
        'pcc_gc_backend4_remap_pending_obj',
        'pcc_gc_backend4_parked_head',
        'pcc_gc_backend4_reset_object_cursor',
        'pcc_gc_backend4_reseed_page_count_cursor',
        'pcc_gc_backend4_reseed_relocation_cursor',
        'pcc_gc_backend4_remembered_slots_head',
        'pcc_gc_backend4_selector_page',
        'pcc_gc_backend4_selector_page_cursor',
        'pcc_gc_backend4_selector_page_seed',
        'pcc_gc_backend4_selector_scan_best',
        'pcc_gc_backend4_selector_scan_cursor',
        'pcc_gc_backend4_selector_scan_page',
        'pcc_gc_backend4_retained_page_head',
        'pcc_gc_backend4_store_buffer_head',
        'pcc_gc_backend4_store_buffer_medium_head',
        'pcc_gc_backend4_zpage_head',
        'pcc_gc_backend4_zpage_node_free_head',
        'pcc_gc_backend4_zpage_payload_span_head',
        'pcc_gc_continuation_root_head',
        'pcc_gc_cms_worker_handle',
        'pcc_gc_deferred_node_free_head',
        'pcc_gc_forwarding_head',
        'pcc_gc_frame_head',
        'pcc_gc_identity_head',
        'pcc_gc_last_alloc',
        'pcc_gc_minor_blocks',
        'pcc_gc_minor_current',
        'pcc_gc_object_head',
        'pcc_gc_object_node_free_head',
        'pcc_gc_tracked_node_pool',
        'pcc_gc_pending_minor_block',
        'pcc_gc_relocate_slot_pairs_ctx',
        'pcc_gc_relocation_set_head',
        'pcc_gc_root_slots',
        'pcc_gc_scheduler_root_head',
        'pcc_gc_trace_cursor',
        'pcc_gc_trace_cext_pending_obj',
        'py_gc_callbacks',
        'py_gc_head',
        'py_set_dummy',
        'py_weakref_head',
    }
)

FREESTANDING_GC_THREAD_LOCAL_GLOBALS: frozenset[str] = frozenset(
    {
        'pcc_gc_frame_node_pool_counts',
        'pcc_gc_frame_node_pool_heads',
        'pcc_gc_frame_node_pool_total',
    }
)

FREESTANDING_GC_RUNTIME_GLOBALS = (
    FREESTANDING_GC_I32_GLOBALS
    | FREESTANDING_GC_I64_GLOBALS
    | FREESTANDING_GC_PTR_GLOBALS
    | FREESTANDING_GC_THREAD_LOCAL_GLOBALS
)


def is_freestanding_gc_runtime_global(symbol: str) -> bool:
    """Return whether an unsafe literal names registered raw GC storage."""
    return symbol in FREESTANDING_GC_RUNTIME_GLOBALS


# Exact read-only managed-runtime queries that strict GC objects may bind as
# raw externs.  Keep this finite: verified unsafe intrinsics own platform and
# runtime-control boundaries such as ``pcc_gc_backend``.
FREESTANDING_GC_READONLY_RUNTIME_IMPORTS = frozenset(
    {
        "pcc_gc_backend4_evacuated_bytes",
        "pcc_gc_backend4_evacuation_candidate_bytes",
        "pcc_gc_backend4_evacuation_candidate_score",
        "pcc_gc_backend4_evacuation_candidate_zpage_bytes",
        "pcc_gc_backend4_evacuation_efficiency_per_mille",
        "pcc_gc_backend4_evacuation_incomplete_batches",
        "pcc_gc_backend4_evacuation_page_candidate_score",
        "pcc_gc_backend4_forwarding_entries",
        "pcc_gc_backend4_fragmentation_backlog_bytes",
        "pcc_gc_backend4_fragmentation_policy_score",
        "pcc_gc_backend4_fragmentation_score",
        "pcc_gc_backend4_generation_barrier_score",
        "pcc_gc_backend4_generation_promotion_score",
        "pcc_gc_backend4_large_defer_limit_bytes",
        "pcc_gc_backend4_large_object_defer_score",
        "pcc_gc_backend4_large_object_deferred_bytes",
        "pcc_gc_backend4_large_object_reconsiderations",
        "pcc_gc_backend4_large_page_live_bytes",
        "pcc_gc_backend4_large_page_object_count",
        "pcc_gc_backend4_medium_page_candidate_bytes",
        "pcc_gc_backend4_medium_page_candidate_score",
        "pcc_gc_backend4_medium_page_candidate_zpage_bytes",
        "pcc_gc_backend4_medium_page_limit_bytes",
        "pcc_gc_backend4_medium_page_live_bytes",
        "pcc_gc_backend4_medium_page_object_count",
        "pcc_gc_backend4_old_bytes",
        "pcc_gc_backend4_old_object_count",
        "pcc_gc_backend4_page_policy_score",
        "pcc_gc_backend4_page_pressure_score",
        "pcc_gc_backend4_remembered_page_entries",
        "pcc_gc_backend4_remembered_page_high_water",
        "pcc_gc_backend4_remembered_page_slot_entries",
        "pcc_gc_backend4_remembered_set_duplicate_skips",
        "pcc_gc_backend4_remembered_set_entries",
        "pcc_gc_backend4_remembered_set_high_water",
        "pcc_gc_backend4_small_page_candidate_bytes",
        "pcc_gc_backend4_small_page_candidate_score",
        "pcc_gc_backend4_small_page_candidate_zpage_bytes",
        "pcc_gc_backend4_small_page_limit_bytes",
        "pcc_gc_backend4_small_page_live_bytes",
        "pcc_gc_backend4_small_page_object_count",
        "pcc_gc_backend4_stable_id_entries",
        "pcc_gc_backend4_store_buffer_batch_capacity",
        "pcc_gc_backend4_store_buffer_cross_thread_medium_flushed_entries",
        "pcc_gc_backend4_store_buffer_cross_thread_medium_flushes",
        "pcc_gc_backend4_store_buffer_drain_batches",
        "pcc_gc_backend4_store_buffer_drained_entries",
        "pcc_gc_backend4_store_buffer_duplicate_skips",
        "pcc_gc_backend4_store_buffer_entries",
        "pcc_gc_backend4_store_buffer_full_batches",
        "pcc_gc_backend4_store_buffer_high_water",
        "pcc_gc_backend4_store_buffer_incomplete_drains",
        "pcc_gc_backend4_store_buffer_max_batch_size",
        "pcc_gc_backend4_store_buffer_medium_capacity",
        "pcc_gc_backend4_store_buffer_medium_flushed_entries",
        "pcc_gc_backend4_store_buffer_medium_flushes",
        "pcc_gc_backend4_store_buffer_medium_full_flushes",
        "pcc_gc_backend4_store_buffer_medium_pending",
        "pcc_gc_backend4_store_buffer_owner_count_high_water",
        "pcc_gc_backend4_store_buffer_owner_fanout_high_water",
        "pcc_gc_backend4_young_bytes",
        "pcc_gc_backend4_young_object_count",
        "pcc_gc_backend4_zpage_capacity_bytes",
        "pcc_gc_backend4_zpage_count",
        "pcc_gc_backend4_zpage_dirty_pages",
        "pcc_gc_backend4_zpage_fragmentation_bytes",
        "pcc_gc_backend4_zpage_fragmentation_per_mille",
        "pcc_gc_backend4_zpage_fragmented_pages",
        "pcc_gc_backend4_zpage_free_capacity_bytes",
        "pcc_gc_backend4_zpage_free_pages",
        "pcc_gc_backend4_zpage_large_pages",
        "pcc_gc_backend4_zpage_old_pages",
        "pcc_gc_backend4_zpage_policy_score",
        "pcc_gc_backend4_zpage_remembered_card_ratio_per_mille",
        "pcc_gc_backend4_zpage_remembered_cards",
        "pcc_gc_backend4_zpage_remembered_slots",
        "pcc_gc_backend4_zpage_used_bytes",
        "pcc_gc_backend4_zpage_young_pages",
        "pcc_gc_relocation_set_size",
    }
)


def is_freestanding_gc_readonly_runtime_import(symbol: str) -> bool:
    """Return whether a raw extern is an admitted read-only GC query."""
    if symbol not in FREESTANDING_GC_READONLY_RUNTIME_IMPORTS:
        return False
    signature = RUNTIME_SIGNATURES.get(symbol)
    if signature is None:
        return False
    return_type, parameter_types, var_arg = signature
    return str(return_type) == "i64" and not parameter_types and not var_arg

# Exact source-level extern shapes admitted across strict freestanding GC
# objects.  This is deliberately smaller than RUNTIME_SIGNATURES: adding a
# managed runtime ABI to the general registry must never make it an implicit
# freestanding escape.  Keeping raw-only object seams out of
# ``RUNTIME_SIGNATURES`` also prevents their declarations from perturbing the
# IR and self-object cache key of every unrelated Python module.
def _cross_object_signatures_part_0():
    return {
    "pcc_current_thread_id": ((), "c_int64"),
    "pcc_thread_safepoint": ((), "c_void"),
    "pcc_thread_stop_requested_acquire": ((), "c_int64"),
    "pcc_thread_no_park_enter": ((), "c_void"),
    "pcc_thread_no_park_exit": ((), "c_void"),
    "pcc_thread_no_park_depth": ((), "c_int64"),
    "pcc_thread_owns_stopped_world": ((), "c_int64"),
    "pcc_thread_registration_waiter_count": ((), "c_int64"),
    "pcc_thread_unregister_current": ((), "c_void"),
    "pcc_thread_start": (("c_ptr", "c_ptr", "c_ptr"), "c_int64"),
    "pcc_thread_join": (("c_ptr", "c_ptr"), "c_int64"),
    "pcc_platform_sleep_ns": (("c_int64",), "c_int64"),
    "pcc_platform_write": (("c_int64", "c_ptr", "c_int64"), "c_int64"),
    "pcc_platform_abort": ((), "c_void"),
    "pcc_threads_enabled": ((), "c_int64"),
    "pcc_platform_getenv": (("c_ptr",), "c_ptr"),
    "pcc_platform_monotonic_us": ((), "c_int64"),
    "pcc_current_native_thread_token": ((), "c_ptr"),
    "pcc_py_gc_minor_graph_lock": ((), "c_void"),
    "pcc_py_gc_minor_graph_unlock": ((), "c_void"),
    "pcc_dealloc_cascade_active": ((), "c_int64"),
    "pcc_gc_root_slot_count_from_map": (("c_ptr",), "c_int64"),
    "pcc_gc_root_map_is_borrowed": (("c_ptr",), "c_int64"),
    "pcc_gc_store_root_plan_init": (
        ("c_ptr", "c_int64"),
        "c_void",
    ),
    "pcc_gc_store_root_plan_commit_locked": (
        ("c_ptr", "c_ptr", "c_ptr"),
        "c_int64",
    ),
    "pcc_gc_store_root_plan_finish": (("c_ptr",), "c_void"),
    "pcc_gc_store_ptr_plan_commit_locked": (
        ("c_ptr", "c_ptr", "c_ptr", "c_ptr"),
        "c_int64",
    ),
    "pcc_gc_store_ptr_plan_finish": (("c_ptr",), "c_void"),
    "pcc_gc_retain_plan_prepare_locked": (
        ("c_ptr", "c_ptr"),
        "c_ptr",
    ),
    "pcc_gc_retain_plan_finish": (("c_ptr",), "c_void"),
    "pcc_gc_scheduler_root_link_locked": (("c_ptr",), "c_void"),
    "pcc_gc_scheduler_root_unlink_locked": (("c_ptr",), "c_int64"),
    "pcc_gc_scheduler_root_count": ((), "c_int64"),
    "pcc_gc_frame_root_slot_count": ((), "c_int64"),
    "pcc_gc_coroutine_root_score": ((), "c_int64"),
    "pcc_gc_cycle_requested_store_release": (("c_int64",), "c_void"),
    "pcc_gc_frame_index_find": (("c_ptr",), "c_ptr"),
    "pcc_gc_frame_index_insert": (("c_ptr", "c_ptr"), "c_int64"),
    "pcc_gc_frame_index_plan_capacity": (("c_int64",), "c_int64"),
    "pcc_gc_frame_index_plan_commit": (
        ("c_ptr", "c_int64", "c_int64"),
        "c_int64",
    ),
    "pcc_gc_frame_index_replace": (("c_ptr", "c_ptr"), "c_ptr"),
    "pcc_gc_frame_index_replace_preallocated": (
        ("c_ptr", "c_ptr"),
        "c_ptr",
    ),
    "pcc_gc_frame_index_remove": (("c_ptr",), "c_ptr"),
    "pcc_gc_frame_node_tls_pool_drain": ((), "c_void"),
    "pcc_gc_forwarding_index_find": (("c_ptr",), "c_ptr"),
    "pcc_gc_forwarding_index_insert": (("c_ptr", "c_ptr"), "c_int64"),
    "pcc_gc_forwarding_index_remove": (("c_ptr",), "c_ptr"),
    "pcc_gc_forwarding_index_clear": ((), "c_void"),
    "pcc_gc_forwarding_target_index_find": (("c_ptr",), "c_ptr"),
    "pcc_gc_forwarding_target_index_insert": (("c_ptr", "c_ptr"), "c_int64"),
    }


def _cross_object_signatures_part_1():
    return {
    "pcc_list_grow_for_mutation": (("c_ptr", "c_int64"), "c_ptr"),
    "pcc_gc_publish_initialized": (("c_ptr",), "c_void"),
    "pcc_gc_store_ptr_plan_init": (
        ("c_ptr", "c_ptr", "c_int64"),
        "c_void",
    ),
    "pcc_gc_backend4_retarget_mutator_payload_locked": (
        (
            "c_ptr",
            "c_ptr",
            "c_int64",
            "c_ptr",
            "c_int64",
            "c_ptr",
            "c_int64",
        ),
        "c_int64",
    ),
    "pcc_gc_forwarding_target_index_upsert": (("c_ptr", "c_ptr"), "c_int64"),
    "pcc_gc_forwarding_target_index_remove": (("c_ptr",), "c_ptr"),
    "pcc_gc_forwarding_target_index_clear": ((), "c_void"),
    "pcc_gc_identity_index_find": (("c_ptr",), "c_ptr"),
    "pcc_gc_identity_index_insert": (("c_ptr", "c_ptr"), "c_int64"),
    "pcc_gc_identity_index_remove": (("c_ptr",), "c_ptr"),
    "pcc_gc_identity_index_clear": ((), "c_void"),
    "pcc_gc_zpage_owner_index_find": (("c_ptr",), "c_ptr"),
    "pcc_gc_zpage_owner_index_upsert": (("c_ptr", "c_ptr"), "c_int64"),
    "pcc_gc_zpage_owner_index_plan_capacity": (("c_int64",), "c_int64"),
    "pcc_gc_zpage_owner_index_plan_commit": (
        ("c_ptr", "c_int64", "c_int64"),
        "c_int64",
    ),
    "pcc_gc_zpage_owner_index_upsert_preallocated": (
        ("c_ptr", "c_ptr"),
        "c_int64",
    ),
    "pcc_gc_zpage_owner_index_remove": (("c_ptr",), "c_ptr"),
    "pcc_gc_config_ensure": ((), "c_int64"),
    "pcc_gc_backend": ((), "c_int64"),
    "pcc_gc_alloc": (("c_int64", "c_int32", "c_int32"), "c_ptr"),
    "pcc_gc_pointer_is_managed": (("c_ptr",), "c_int64"),
    "pcc_gc_pointer_register": (("c_ptr",), "c_int64"),
    "pcc_gc_pointer_unregister": (("c_ptr",), "c_int64"),
    "pcc_capi_is_type_object_value": (("c_ptr",), "c_int64"),
    "py_incref": (("c_ptr",), "c_void"),
    "py_decref": (("c_ptr",), "c_void"),
    "pcc_gc_object_list_head": ((), "c_ptr"),
    }


def _cross_object_signatures_part_2():
    return {
    "pcc_gc_object_set_list_head": (("c_ptr",), "c_void"),
    "pcc_gc_trace_cursor_load": ((), "c_ptr"),
    "pcc_gc_trace_cursor_store": (("c_ptr",), "c_void"),
    "pcc_gc_backend3_young_list_head": ((), "c_ptr"),
    "pcc_gc_backend3_young_set_head": (("c_ptr",), "c_void"),
    "pcc_gc_object_node_size": (("c_ptr",), "c_int64"),
    "pcc_gc_object_node_next": (("c_ptr",), "c_ptr"),
    "pcc_gc_object_node_set_next": (("c_ptr", "c_ptr"), "c_void"),
    "pcc_gc_object_node_minor_block": (("c_ptr",), "c_ptr"),
    "pcc_gc_object_node_freeing": (("c_ptr",), "c_int64"),
    "pcc_gc_object_node_set_freeing": (("c_ptr", "c_int64"), "c_void"),
    "pcc_gc_object_node_prev": (("c_ptr",), "c_ptr"),
    "pcc_gc_object_node_set_prev": (("c_ptr", "c_ptr"), "c_void"),
    "pcc_gc_object_node_zpage": (("c_ptr",), "c_ptr"),
    "pcc_gc_object_node_set_zpage": (("c_ptr", "c_ptr"), "c_void"),
    "pcc_gc_object_node_gc_refs": (("c_ptr",), "c_int64"),
    "pcc_gc_object_node_set_gc_refs": (("c_ptr", "c_int64"), "c_void"),
    "pcc_gc_object_node_young_next": (("c_ptr",), "c_ptr"),
    "pcc_gc_object_node_set_young_next": (("c_ptr", "c_ptr"), "c_void"),
    "pcc_gc_object_node_young_prev": (("c_ptr",), "c_ptr"),
    "pcc_gc_object_node_set_young_prev": (("c_ptr", "c_ptr"), "c_void"),
    "pcc_gc_object_node_clear_promotion_state": (("c_ptr",), "c_void"),
    "pcc_gc_backend3_promotion_unlink": (("c_ptr",), "c_void"),
    "pcc_gc_object_node_alloc": ((), "c_ptr"),
    "pcc_gc_object_node_prepare": ((), "c_ptr"),
    "pcc_gc_object_node_plan_requires_prepare": ((), "c_int64"),
    "pcc_gc_object_node_take_prepared": (("c_ptr",), "c_ptr"),
    "pcc_gc_object_node_release": (("c_ptr",), "c_void"),
    "pcc_gc_object_node_finish_detached": (("c_ptr",), "c_void"),
    "pcc_gc_object_node_unlink": (("c_ptr",), "c_void"),
    "pcc_gc_backend3_young_link_head": (("c_ptr",), "c_void"),
    "pcc_gc_backend3_young_unlink": (("c_ptr",), "c_void"),
    "pcc_gc_backend3_young_rebuild": ((), "c_void"),
    "pcc_gc_object_known_size": (("c_ptr",), "c_int64"),
    "pcc_gc_live_bytes_subtract": (("c_int64",), "c_void"),
    "pcc_gc_forwarding_find": (("c_ptr",), "c_ptr"),
    "pcc_gc_forwarding_target_exists": (("c_ptr",), "c_int64"),
    "pcc_gc_install_forwarding_unlocked": (("c_ptr", "c_ptr"), "c_int64"),
    "pcc_gc_forwarding_install_plan_prepare": (
        ("c_ptr", "c_ptr"),
        "c_ptr",
    ),
    "pcc_gc_install_forwarding_preallocated_unlocked": (
        ("c_ptr", "c_ptr", "c_ptr"),
        "c_int64",
    ),
    "pcc_gc_forwarding_install_plan_finish": (("c_ptr",), "c_void"),
    "pcc_gc_forwarding_plan_index_capacity": (
        ("c_int64", "c_int64"),
        "c_int64",
    ),
    "pcc_gc_forwarding_plan_index_commit": (
        ("c_int64", "c_ptr", "c_int64", "c_int64"),
        "c_int64",
    ),
    "pcc_gc_forwarding_plan_index_insert": (
        ("c_int64", "c_ptr", "c_ptr"),
        "c_int64",
    ),
    "pcc_gc_identity_detach": (("c_ptr",), "c_ptr"),
    "pcc_gc_identity_finish_detached": (("c_ptr",), "c_void"),
    "pcc_gc_identity_remove": (("c_ptr",), "c_void"),
    "pcc_gc_relocate_copy_payload": (
        ("c_ptr", "c_ptr", "c_int64", "c_int64"),
        "c_int64",
    ),
    "pcc_gc_relocate_copy_payload_prepared_locked": (
        ("c_ptr", "c_ptr", "c_int64", "c_int64", "c_ptr"),
        "c_int64",
    ),
    "pcc_gc_relocation_payload_slot_count_locked": (
        ("c_ptr",),
        "c_int64",
    ),
    }


def _cross_object_signatures_part_3():
    return {
    "pcc_gc_relocation_payload_plan_prepare": (
        ("c_int64",),
        "c_ptr",
    ),
    "pcc_gc_relocation_payload_plan_validate_locked": (
        ("c_ptr", "c_ptr", "c_int64", "c_ptr"),
        "c_int64",
    ),
    "pcc_gc_relocation_payload_raw_snapshot_locked": (
        ("c_ptr", "c_int64", "c_int64", "c_ptr"),
        "c_int64",
    ),
    "pcc_gc_relocation_payload_raw_prepare": (
        ("c_ptr",),
        "c_int64",
    ),
    "pcc_gc_relocation_payload_raw_validate_locked": (
        ("c_ptr", "c_ptr", "c_int64", "c_int64", "c_ptr"),
        "c_int64",
    ),
    "pcc_gc_relocation_payload_plan_finish": (("c_ptr",), "c_void"),
    "pcc_capi_visit_extension_module_state_roots": (
        ("c_ptr", "c_ptr"),
        "c_void",
    ),
    "pcc_cpy_handle_move_owned_ref": (("c_ptr", "c_ptr"), "c_void"),
    "pcc_gc_generational_oldify_supported_tag": (("c_int64",), "c_int64"),
    "pcc_gc_generational_mark_forwarded_source_inactive": (
        ("c_ptr",),
        "c_void",
    ),
    "pcc_gc_generational_oldify_copy": (("c_ptr",), "c_ptr"),
    "pcc_gc_backend3_remembered_owner_list_head": ((), "c_ptr"),
    "pcc_gc_backend3_remembered_owner_list_set_head": (("c_ptr",), "c_void"),
    "pcc_gc_backend3_remember_owner": (("c_ptr", "c_int64"), "c_void"),
    "pcc_gc_backend3_clear_remembered_owners": ((), "c_ptr"),
    "pcc_gc_backend3_finish_detached_remembered_owners": (
        ("c_ptr",),
        "c_void",
    ),
    "pcc_gc_backend3_scan_remembered_owners": (("c_int64",), "c_int64"),
    "pcc_gc_backend3_drain_remembered_owners": (
        ("c_int64", "c_ptr"),
        "c_int64",
    ),
    "pcc_gc_trace_referents_for_promotion": (("c_ptr",), "c_void"),
    "pcc_gc_backend3_promotion_worklist_unlink": (("c_ptr",), "c_void"),
    "pcc_gc_backend3_enqueue_promotion_owner": (("c_ptr",), "c_void"),
    "pcc_gc_backend3_promote_cext_owner_referents": (
        ("c_ptr",),
        "c_void",
    ),
    "pcc_gc_visit_object_slots_slice": (
        ("c_ptr", "c_int64", "c_int64", "c_ptr", "c_ptr", "c_ptr"),
        "c_int64",
    ),
    "pcc_gc_backend3_drain_promotion_worklist": (
        ("c_int64",),
        "c_int64",
    ),
    "pcc_gc_backend3_promotion_probe_config": (
        ("c_int64",),
        "c_void",
    ),
    "pcc_gc_backend3_promotion_probe_state": ((), "c_int64"),
    "pcc_gc_generational_promote_young_if_known": (("c_ptr",), "c_void"),
    "pcc_gc_generational_promote_owned_slot_mode": (
        ("c_ptr", "c_int64", "c_int64"),
        "c_void",
    ),
    "pcc_gc_generational_promote_borrowed_slot_mode": (
        ("c_ptr", "c_int64", "c_int64"),
        "c_void",
    ),
    "pcc_gc_trace_referents_for_promotion_mode": (
        ("c_ptr", "c_int64"),
        "c_void",
    ),
    "pcc_gc_trace_mark_gray_if_known": (("c_ptr",), "c_void"),
    "pcc_gc_trace_cext_referents_unlocked": (
        ("c_ptr", "c_ptr"),
        "c_void",
    ),
    "pcc_gc_backend4_zpage_note_owner_promoted": (("c_ptr",), "c_void"),
    "pcc_gc_generational_promote_frame_roots": (("c_int64",), "c_void"),
    "pcc_gc_generational_promote_scheduler_roots": (
        ("c_int64",),
        "c_void",
    ),
    "pcc_gc_generational_promote_tls_exception_root": (
        ("c_ptr",),
        "c_void",
    ),
    }


def _cross_object_signatures_part_4():
    return {
    "pcc_gc_generational_step": (
        ("c_int64", "c_int64"),
        "c_int64",
    ),
    "pcc_gc_incremental_concurrent_step": (("c_int64",), "c_int64"),
    "pcc_gc_tracing_budget_from_debt": ((), "c_int64"),
    "pcc_gc_tracing_step_cycle": (("c_int64",), "c_int64"),
    "pcc_gc_tracing_record_pause": (
        ("c_int64", "c_int64"),
        "c_void",
    ),
    "pcc_gc_backend4_step_remembered_roots": (
        ("c_int64",),
        "c_int64",
    ),
    "pcc_gc_backend4_step_generation_aging": (
        ("c_int64",),
        "c_int64",
    ),
    "pcc_gc_backend4_store_buffer_enqueue": (
        ("c_ptr", "c_ptr", "c_ptr"),
        "c_int64",
    ),
    "pcc_gc_backend4_evacuation_page_drain": (
        ("c_int64",),
        "c_int64",
    ),
    "pcc_gc_backend4_relocate_copy_supported_tag": (
        ("c_int64",),
        "c_int64",
    ),
    "pcc_gc_backend4_remap_heal_slot": (
        ("c_ptr", "c_int64"),
        "c_void",
    ),
    "pcc_gc_backend4_remap_slot": (
        ("c_ptr", "c_int64", "c_ptr"),
        "c_void",
    ),
    "pcc_gc_backend4_remap_referents": (("c_ptr",), "c_void"),
    "pcc_gc_backend4_remap_cext_ctx_valid": (("c_ptr",), "c_int64"),
    "pcc_gc_backend4_remap_cext_referents_unlocked": (
        ("c_ptr", "c_ptr"),
        "c_void",
    ),
    "pcc_gc_backend4_remembered_set_retarget_slot": (
        ("c_ptr", "c_ptr", "c_ptr", "c_ptr"),
        "c_void",
    ),
    "pcc_gc_backend4_zpage_register_owner_payload_span": (
        ("c_ptr", "c_ptr", "c_int64"),
        "c_int64",
    ),
    "pcc_gc_backend4_zpage_payload_span_preflight_locked": (
        ("c_ptr", "c_int64"),
        "c_int64",
    ),
    "pcc_gc_backend4_zpage_publish_relocation_payload_spans_locked": (
        ("c_ptr", "c_ptr", "c_int64", "c_int64"),
        "c_int64",
    ),
    "pcc_gc_backend4_relocation_set_find": (("c_ptr",), "c_ptr"),
    "pcc_gc_backend4_relocation_set_add": (("c_ptr",), "c_int64"),
    "pcc_gc_backend4_relocation_set_remove": (("c_ptr",), "c_void"),
    "pcc_gc_backend4_owner_remembered_slots": (("c_ptr",), "c_int64"),
    "pcc_gc_backend4_evacuation_page_find": (("c_ptr",), "c_ptr"),
    "pcc_gc_backend4_evacuation_page_add": (("c_ptr",), "c_int64"),
    "pcc_gc_backend4_zpage_page_for_owner": (("c_ptr",), "c_ptr"),
    "pcc_gc_backend4_relocation_set_contains_page": (("c_ptr",), "c_int64"),
    "pcc_gc_backend4_evacuation_page_remove": (("c_ptr",), "c_void"),
    "pcc_gc_backend4_free_page_count_for_class": (
        ("c_int64",),
        "c_int64",
    ),
    "pcc_gc_backend4_free_page_limit_for_class": (
        ("c_int64",),
        "c_int64",
    ),
    "pcc_gc_backend4_zpage_clear_reusable_state": (("c_ptr",), "c_void"),
    "pcc_gc_backend4_zpage_cache": (("c_ptr",), "c_void"),
    "pcc_gc_backend4_zpage_destroy": (("c_ptr",), "c_void"),
    "pcc_gc_backend4_zpage_recycle": (("c_ptr",), "c_void"),
    "pcc_gc_backend4_zpage_page_head": (("c_ptr",), "c_ptr"),
    "pcc_gc_backend4_zpage_set_page_head": (
        ("c_ptr", "c_ptr"),
        "c_void",
    ),
    "pcc_gc_backend4_zpage_unlink_node": (("c_ptr",), "c_void"),
    "pcc_gc_backend4_zpage_find": (("c_ptr",), "c_ptr"),
    "pcc_gc_backend4_zpage_unlink_page": (("c_ptr",), "c_void"),
    "pcc_gc_backend4_zpage_find_owner_for_page": (("c_ptr",), "c_ptr"),
    "pcc_gc_backend4_zpage_remove_payload_spans": (("c_ptr",), "c_void"),
    "pcc_gc_backend4_zpage_remove_payload_span_base": (
        ("c_ptr", "c_ptr"),
        "c_int64",
    ),
    "pcc_gc_backend4_zpage_remove": (("c_ptr",), "c_void"),
    "pcc_gc_backend4_zpage_detach_for_relocation": (
        ("c_ptr",),
        "c_ptr",
    ),
    "pcc_gc_backend4_zpage_finish_relocation_detach": (
        ("c_ptr",),
        "c_void",
    ),
    "pcc_gc_backend4_source_side_table_plan_prepare": (
        ("c_ptr",),
        "c_ptr",
    ),
    "pcc_gc_backend4_source_side_table_plan_commit": (
        ("c_ptr",),
        "c_int64",
    ),
    "pcc_gc_backend4_source_side_table_plan_finish": (
        ("c_ptr", "c_ptr"),
        "c_void",
    ),
    "pcc_gc_backend4_relocate_copy_preallocated_unlocked": (
        ("c_ptr", "c_int64", "c_ptr", "c_ptr", "c_ptr", "c_ptr"),
        "c_ptr",
    ),
    "pcc_gc_relocate_copy": (("c_ptr", "c_int64"), "c_ptr"),
    }


def _cross_object_signatures_part_5():
    return {
    "pcc_gc_backend4_remap_and_retire_unlocked": (("c_ptr",), "c_void"),
    "pcc_gc_backend4_remap_and_retire_stopped_world": ((), "c_int64"),
    "pcc_gc_backend4_finish_retained_page_releases": (("c_ptr",), "c_void"),
    "pcc_gc_backend4_finish_remap_retirement": (("c_ptr",), "c_void"),
    "pcc_gc_backend4_park_page": (("c_ptr",), "c_void"),
    "pcc_gc_backend4_drain_parked_pages": ((), "c_void"),
    "pcc_gc_backend4_note_forwarding_removed_on_page": (
        ("c_ptr",),
        "c_void",
    ),
    "pcc_gc_backend4_zpage_note_forwarding_removed": (
        ("c_ptr",),
        "c_void",
    ),
    "pcc_gc_forwarding_list_head": ((), "c_ptr"),
    "pcc_gc_forwarding_target_unlink": (("c_ptr",), "c_void"),
    "pcc_gc_forwarding_unlink_main": (("c_ptr",), "c_void"),
    "pcc_gc_forwarding_remove": (("c_ptr",), "c_void"),
    "pcc_gc_forwarding_detach_into_finish": (("c_ptr", "c_ptr"), "c_void"),
    }


def _cross_object_signatures_part_6():
    return {
    "pcc_gc_forwarding_remove_target": (("c_ptr", "c_ptr"), "c_void"),
    "pcc_gc_relocation_retire_source_payload_for_target_death_into_finish": (
        ("c_ptr", "c_ptr", "c_ptr"),
        "c_int64",
    ),
    "pcc_gc_backend4_select_relocation_pages": (("c_int64",), "c_int64"),
    "pcc_gc_backend4_zpage_active_page": (
        ("c_int64", "c_int64"),
        "c_ptr",
    ),
    "pcc_gc_backend4_zpage_set_active_page": (("c_ptr",), "c_void"),
    "pcc_gc_backend4_zpage_clear_active_page": (("c_ptr",), "c_void"),
    "pcc_gc_backend4_zpage_find_reusable_page_for_gen": (
        ("c_int64", "c_int64"),
        "c_ptr",
    ),
    "pcc_gc_backend4_zpage_find_reusable_page": (
        ("c_ptr", "c_int64"),
        "c_ptr",
    ),
    "pcc_gc_backend4_zpage_pop_free_page": (("c_int64",), "c_ptr"),
    "pcc_gc_backend4_zpage_reset": (
        ("c_ptr", "c_ptr", "c_int64"),
        "c_void",
    ),
    "pcc_gc_backend4_zpage_node_alloc": ((), "c_ptr"),
    "pcc_gc_backend4_zpage_node_prepare": ((), "c_ptr"),
    "pcc_gc_backend4_zpage_node_plan_requires_prepare": ((), "c_int64"),
    "pcc_gc_backend4_zpage_node_take_prepared": (("c_ptr",), "c_ptr"),
    "pcc_gc_backend4_zpage_node_release": (("c_ptr",), "c_void"),
    "pcc_gc_backend4_zpage_link_node_preallocated": (
        ("c_ptr",),
        "c_int64",
    ),
    "pcc_gc_backend4_zpage_track_page_prepare": (
        ("c_ptr", "c_ptr", "c_int64"),
        "c_ptr",
    ),
    "pcc_gc_backend4_zpage_track_alloc_preallocated": (
        ("c_ptr", "c_int64", "c_ptr", "c_ptr", "c_int64"),
        "c_ptr",
    ),
    "pcc_gc_backend4_zpage_link_node": (("c_ptr",), "c_void"),
    "pcc_gc_backend4_zpage_find_page_for_addr": (
        ("c_ptr", "c_int64"),
        "c_ptr",
    ),
    "pcc_gc_relocation_payload_slot_pairs_dispose": (("c_ptr",), "c_void"),
    "pcc_gc_relocation_payload_count_slot": (
        ("c_ptr", "c_int64", "c_ptr"),
        "c_void",
    ),
    "pcc_gc_relocation_payload_from_slot": (
        ("c_ptr", "c_int64", "c_ptr"),
        "c_void",
    ),
    "pcc_gc_relocation_payload_to_slot": (
        ("c_ptr", "c_int64", "c_ptr"),
        "c_void",
    ),
    "pcc_gc_relocation_payload_retire_count_slot": (
        ("c_ptr", "c_int64", "c_ptr"),
        "c_void",
    ),
    "pcc_gc_relocation_payload_retire_collect_slot": (
        ("c_ptr", "c_int64", "c_ptr"),
        "c_void",
    ),
    "pcc_gc_relocation_retire_source_payload": (
        ("c_ptr",),
        "c_int64",
    ),
    "pcc_gc_relocation_retire_source_payload_into_finish": (
        ("c_ptr", "c_ptr"),
        "c_int64",
    ),
    "pcc_gc_relocation_finish_source_payloads": (("c_ptr",), "c_void"),
    "py_mem_free": (("c_ptr",), "c_void"),
    "pcc_gc_relocation_payload_slot_pairs_prepare": (
        ("c_ptr", "c_ptr", "c_int64"),
        "c_ptr",
    ),
    "pcc_gc_relocation_payload_copy_slots": (
        ("c_ptr", "c_ptr", "c_ptr"),
        "c_int64",
    ),
    "pcc_gc_relocation_payload_retarget_continuation_root_slots": (
        ("c_ptr", "c_ptr", "c_ptr", "c_ptr"),
        "c_void",
    ),
    "pcc_gc_relocation_payload_fail": (("c_ptr",), "c_int64"),
    "pcc_gc_relocation_payload_finish": (
        ("c_ptr", "c_ptr", "c_int64", "c_ptr", "c_ptr", "c_ptr", "c_int64"),
        "c_int64",
    ),
    "py_tls_exc_get": ((), "c_ptr"),
    "py_tls_exc_set": (("c_ptr",), "c_void"),
    "pcc_gc_object_index_find": (("c_ptr",), "c_ptr"),
    "pcc_gc_object_index_insert": (("c_ptr", "c_ptr"), "c_int64"),
    "pcc_gc_object_index_plan_capacity": (("c_int64",), "c_int64"),
    "pcc_gc_object_index_plan_commit": (
        ("c_ptr", "c_int64", "c_int64"),
        "c_int64",
    ),
    "pcc_gc_object_index_insert_preallocated": (
        ("c_ptr", "c_ptr"),
        "c_int64",
    ),
    "pcc_gc_object_index_remove": (("c_ptr",), "c_ptr"),
    "pcc_gc_managed_pointer_index_contains": (("c_ptr",), "c_int64"),
    "pcc_gc_managed_pointer_index_insert": (("c_ptr",), "c_int64"),
    "pcc_gc_managed_pointer_index_remove": (("c_ptr",), "c_int64"),
    "pcc_gc_granule_is_object_start": (("c_ptr",), "c_int64"),
    "pcc_gc_granule_object_retire": (("c_ptr",), "c_int64"),
    "pcc_runtime_tripwire_fail": (
        ("c_ptr", "c_ptr", "c_int32"),
        "c_void",
    ),
    "pcc_py_gc_defer_tripwire": (
        ("c_ptr", "c_ptr", "c_int32"),
        "c_void",
    ),
    }


def _cross_object_signatures_part_7():
    return {
    "pcc_gc_tripwire_defer_or_fail": (
        ("c_ptr", "c_ptr", "c_int32"),
        "c_int32",
    ),
    "pcc_gc_object_is_known_no_lock": (("c_ptr",), "c_int64"),
    "pcc_gc_object_node_is_active": (("c_ptr",), "c_int64"),
    "pcc_gc_mark_root_gray_if_known": (("c_ptr",), "c_void"),
    "pcc_gc_gray_count_load_acquire": ((), "c_int64"),
    "pcc_gc_gray_count_increment_acq_rel": ((), "c_void"),
    "pcc_gc_gray_count_decrement_acq_rel": ((), "c_void"),
    "pcc_gc_gray_count_store_release": (("c_int64",), "c_void"),
    "pcc_gc_gray_current_roots": ((), "c_void"),
    "pcc_gc_gray_refcount_external_roots": ((), "c_void"),
    "pcc_gc_prepare_object_list_mark": (("c_int64",), "c_void"),
    "pcc_gc_subtract_referent_refs": (("c_ptr",), "c_void"),
    "pcc_gc_visit_object_slots": (
        ("c_ptr", "c_ptr", "c_ptr"),
        "c_int64",
    ),
    "pcc_gc_memoryview_initialize_owned_buffer": (
        ("c_ptr", "c_ptr"),
        "c_int64",
    ),
    "pcc_gc_memoryview_refresh_owned_buffer": (("c_ptr",), "c_int64"),
    "pcc_errno_get": ((), "c_int32"),
    "pcc_errno_message_into": (
        ("c_int32", "c_ptr", "c_int64"),
        "c_int32",
    ),
    "pcc_gc_load_ptr": (("c_ptr", "c_ptr"), "c_ptr"),
    "pcc_gc_note_object_freeing": (("c_ptr",), "c_void"),
    "pcc_gc_trace_continuation_roots": ((), "c_int64"),
    "pcc_stop_the_world": ((), "c_int64"),
    }


def _cross_object_signatures_part_8():
    return {
    "pcc_resume_world": ((), "c_int64"),
    "pcc_capi_is_cext_type_tag": (("c_int64",), "c_int64"),
    "pcc_capi_dealloc_cext_object": (
        ("c_ptr", "c_int64"),
        "c_int64",
    ),
    "pcc_capi_visit_cext_object_slots_i64": (
        ("c_ptr", "c_ptr", "c_ptr"),
        "c_int32",
    ),
    "pcc_gc_promote_cached_frame_slot": (
        ("c_ptr", "c_int64", "c_ptr", "c_int64"),
        "c_void",
    ),
    "pcc_gc_resolve_root_slot_unlocked": (
        ("c_ptr", "c_int64"),
        "c_ptr",
    ),
    "pcc_gc_visit_mapped_root_slot": (
        (
            "c_ptr",
            "c_int64",
            "c_ptr",
            "c_int64",
            "c_int64",
            "c_int64",
        ),
        "c_int64",
    ),
    "pcc_gc_visit_registered_root_slots": (
        ("c_int64", "c_int64"),
        "c_int64",
    ),
    "pcc_gc_root_registry_note_mutation_locked": ((), "c_void"),
    "py_decref": (("c_ptr",), "c_void"),
    "py_incref": (("c_ptr",), "c_void"),
    "py_subs_exc_cache_slot": (("c_int64",), "c_ptr"),
    "py_gc_index_insert": (("c_ptr", "c_ptr"), "c_int64"),
    "py_gc_index_find": (("c_ptr",), "c_ptr"),
    "py_gc_index_remove": (("c_ptr",), "c_ptr"),
    "pcc_gc_default_unlink_tracked_node": (("c_ptr",), "c_void"),
    "pcc_gc_default_drain_deferred_nodes": ((), "c_void"),
    "pcc_gc_default_table_lock": ((), "c_void"),
    "pcc_gc_default_table_unlock": ((), "c_void"),
    "pcc_gc_tracked_node_pool_cached_count": ((), "c_int64"),
    "pcc_gc_tracked_node_pool_drain": ((), "c_void"),
    "pcc_gc_backend0_visit_subtract": (("c_ptr",), "c_void"),
    "pcc_gc_backend0_is_unreachable": (("c_ptr",), "c_int64"),
    "pcc_gc_backend0_mark_reachable": (("c_ptr",), "c_void"),
    "pcc_gc_backend0_clear_referents": (("c_ptr",), "c_void"),
    "pcc_gc_tracing_has_sweep_candidate": ((), "c_int64"),
    "pcc_gc_tracing_sweep_unreachable": (("c_int64",), "c_int64"),
    "pcc_gc_trace_referents": (("c_ptr",), "c_void"),
    "pcc_gc_begin_mark_cycle": ((), "c_void"),
    "pcc_gc_finish_tracing_cycle": (
        ("c_int64", "c_int64"),
        "c_int64",
    ),
    "pcc_gc_complete_claimed_tracing_cycle": (
        ("c_int64", "c_int64"),
        "c_int64",
    ),
    "pcc_gc_tracing_cycle_epoch_advance_unlocked": ((), "c_int64"),
    "pcc_gc_tracing_finish_claim_clear_unlocked": (
        ("c_int64", "c_int64"),
        "c_void",
    ),
    "pcc_gc_config_ensure": ((), "c_int64"),
    "pcc_gc_maybe_start_cms_worker": ((), "c_void"),
    "pcc_gc_tracing_clear_unreachable": (("c_ptr",), "c_void"),
    "pcc_gc_tracing_finalize_unreachable": (("c_ptr",), "c_void"),
    "pcc_gc_tracing_recheck_reachability_after_finalizers": ((), "c_void"),
    "pcc_gc_seed_roots": ((), "c_void"),
    "pcc_gc_drain_all_gray_unlocked": ((), "c_int64"),
    "pcc_gc_drain_all_gray_locked_slice": ((), "c_int64"),
    "pcc_refcount_forget": (("c_ptr",), "c_void"),
    "py_gc_untrack": (("c_ptr",), "c_void"),
    "py_user_del_dispatch": (("c_ptr",), "c_void"),
    "py_weakref_invalidate": (("c_ptr",), "c_void"),
    "py_dealloc_int": (("c_ptr",), "c_void"),
    "py_dealloc_float": (("c_ptr",), "c_void"),
    "py_dealloc_str": (("c_ptr",), "c_void"),
    "py_dealloc_list": (("c_ptr",), "c_void"),
    "py_dealloc_tuple": (("c_ptr",), "c_void"),
    "py_dealloc_dict": (("c_ptr",), "c_void"),
    "py_dealloc_set": (("c_ptr",), "c_void"),
    }


def _cross_object_signatures_part_9():
    return {
    "py_dealloc_func": (("c_ptr",), "c_void"),
    "py_class_dealloc": (("c_ptr",), "c_void"),
    "py_instance_dealloc": (("c_ptr",), "c_void"),
    "py_descriptor_dealloc": (("c_ptr",), "c_void"),
    "py_dealloc_exc": (("c_ptr",), "c_void"),
    "py_dealloc_file": (("c_ptr",), "c_void"),
    }


def _cross_object_signatures_part_10():
    return {
    "py_dealloc_iter": (("c_ptr",), "c_void"),
    "py_dealloc_gen": (("c_ptr",), "c_void"),
    "py_dealloc_coroutine": (("c_ptr",), "c_void"),
    "py_dealloc_continuation": (("c_ptr",), "c_void"),
    "py_dealloc_task": (("c_ptr",), "c_void"),
    "py_dealloc_virtual_thread": (("c_ptr",), "c_void"),
    "py_dealloc_vthread_channel": (("c_ptr",), "c_void"),
    "py_dealloc_memoryview": (("c_ptr",), "c_void"),
    "py_dealloc_weakref": (("c_ptr",), "c_void"),
    "py_dealloc_thread_lock": (("c_ptr",), "c_void"),
    "py_dealloc_thread_rlock": (("c_ptr",), "c_void"),
    "py_dealloc_thread_event": (("c_ptr",), "c_void"),
    "py_dealloc_thread_condition": (("c_ptr",), "c_void"),
    "py_dealloc_thread_semaphore": (("c_ptr",), "c_void"),
    "py_dealloc_thread_thread": (("c_ptr",), "c_void"),
    "py_dealloc_generic": (("c_ptr",), "c_void"),
    }


FREESTANDING_GC_CROSS_OBJECT_SIGNATURES: dict[
    str, tuple[tuple[str, ...], str]
] = _cross_object_signatures_part_0()
FREESTANDING_GC_CROSS_OBJECT_SIGNATURES.update(_cross_object_signatures_part_1())
FREESTANDING_GC_CROSS_OBJECT_SIGNATURES.update(_cross_object_signatures_part_2())
FREESTANDING_GC_CROSS_OBJECT_SIGNATURES.update(_cross_object_signatures_part_3())
FREESTANDING_GC_CROSS_OBJECT_SIGNATURES.update(_cross_object_signatures_part_4())
FREESTANDING_GC_CROSS_OBJECT_SIGNATURES.update(_cross_object_signatures_part_5())
FREESTANDING_GC_CROSS_OBJECT_SIGNATURES.update(_cross_object_signatures_part_6())
FREESTANDING_GC_CROSS_OBJECT_SIGNATURES.update(_cross_object_signatures_part_7())
FREESTANDING_GC_CROSS_OBJECT_SIGNATURES.update(_cross_object_signatures_part_8())
FREESTANDING_GC_CROSS_OBJECT_SIGNATURES.update(_cross_object_signatures_part_9())
FREESTANDING_GC_CROSS_OBJECT_SIGNATURES.update(_cross_object_signatures_part_10())


def is_freestanding_gc_cross_object_runtime_import(
    symbol: str, parameters_source: str, return_source: str
) -> bool:
    """Validate one exact raw ABI call between freestanding GC objects."""
    source_signature = FREESTANDING_GC_CROSS_OBJECT_SIGNATURES.get(symbol)
    if source_signature is None:
        return False
    source_parameter_types, source_return_type = source_signature
    expected_parameters_source = "(" + ",".join(source_parameter_types)
    if len(source_parameter_types) == 1:
        expected_parameters_source += ","
    expected_parameters_source += ")"
    return (
        parameters_source == expected_parameters_source
        and return_source == source_return_type
    )


RUNTIME_MUTABLE_GLOBALS = frozenset(
    {
        "pcc_thread_stop_requested",
        "py_func_code_class_cache",
    }
)


def declare_runtime(module: ir.Module) -> dict[str, ir.Function]:
    """Declare all runtime library functions in ``module``.

    Returns a mapping ``name -> ir.Function`` for every entry in
    :data:`RUNTIME_SIGNATURES`. Declarations are idempotent: calling
    twice on the same module returns the same ``ir.Function`` objects.

    The returned dict intentionally only contains functions. To fetch a
    runtime global (``py_None`` etc.), call :func:`declare_runtime_global`.
    """
    funcs: dict[str, ir.Function] = {}
    for item in RUNTIME_SIGNATURES.items():
        name = item[0]
        sig = item[1]
        ret_ty = sig[0]
        param_tys = sig[1]
        var_arg = sig[2]
        existing = module.globals.get(name)
        if existing is not None and isinstance(existing, ir.Function):
            funcs[name] = existing
            continue
        fnty = ir.FunctionType(ret_ty, param_tys, var_arg=var_arg)
        fn = ir.Function(module, fnty, name=name)
        fn.linkage = "external"
        _apply_runtime_function_attrs(fn, name)
        funcs[name] = fn
    return funcs


def _apply_runtime_function_attrs(fn: ir.Function, name: str) -> None:
    attr_set = RUNTIME_FUNCTION_ATTRS.get(name)
    if not attr_set:
        return
    fa = getattr(fn, "attributes", None)
    if fa is None:
        return
    # NOTE: avoid ``fa.add(attr)`` — pcc-py codegen mis-dispatches the
    # ``.add`` method as ``py_set_add`` regardless of receiver type,
    # which crashes against ``FunctionAttributes._attrs`` (a list).
    # See docs/investigations/pcc1-stage2-runtime-abi-set-segfault.md.
    bag = fa._attrs
    for attr in sorted(attr_set):
        try:
            if attr not in bag:
                bag.append(attr)
        except ValueError:
            # Two run modes for this code path:
            #
            # * Host CPython + llvmlite 0.46+: AttributeSet.add has a
            #   whitelist that lags LLVM upstream and rejects e.g.
            #   `willreturn`. The previous workaround was
            #   ``set.add(fn_attrs, attr)`` — an unbound-method-call
            #   that bypassed the wrapper via MRO. This pattern can't
            #   be lowered natively by pcc's codegen, so when pcc
            #   self-compiles runtime_abi.py the call site routes
            #   through libpython (py_cpy_*), which in turn fails the
            #   no-libpython detector and blocks the self-compile
            #   tests in tests/test_py_multi_file_bootstrap_shim.py.
            #
            # * pcc-compiled binary using pcc.llvm_capi.ir: the local
            #   FunctionAttributes.add (ir.py:972) has no whitelist
            #   and never raises, so this except branch is dead code
            #   — there is no whitelist to bypass.
            #
            # Trade-off: silently dropping the attribute under host
            # CPython loses a perf hint (e.g. willreturn → ADCE may
            # leave a few `py_err_occurred` checks in place that P-7
            # would have eliminated). Other attributes in the same
            # set (readnone, readonly, nounwind) are accepted by
            # llvmlite directly, so they still take effect. Net cost
            # is a small perf miss; net benefit is that pcc can
            # self-compile this module without a libpython link.
            pass


def declare_runtime_global(module: ir.Module, name: str) -> ir.GlobalVariable:
    """Declare (or fetch) one of the runtime's extern constant globals.

    Raises :class:`KeyError` if ``name`` is not a known runtime global.
    """
    if name not in RUNTIME_GLOBALS:
        raise KeyError(f"unknown runtime global: {name!r}")
    existing = module.globals.get(name)
    if existing is not None and isinstance(existing, ir.GlobalVariable):
        return existing
    gv = ir.GlobalVariable(module, RUNTIME_GLOBALS[name], name=name)
    gv.linkage = "external"
    gv.global_constant = name not in RUNTIME_MUTABLE_GLOBALS
    return gv


__all__ = [
    "FREESTANDING_GC_CROSS_OBJECT_SIGNATURES",
    "FREESTANDING_GC_I32_GLOBALS",
    "FREESTANDING_GC_I64_GLOBALS",
    "FREESTANDING_GC_PTR_GLOBALS",
    "FREESTANDING_GC_RUNTIME_GLOBALS",
    "FREESTANDING_GC_THREAD_LOCAL_GLOBALS",
    "is_freestanding_gc_runtime_global",
    "RUNTIME_SIGNATURES",
    "RUNTIME_FUNCTION_ATTRS",
    "RUNTIME_GLOBALS",
    "declare_runtime",
    "declare_runtime_global",
]
