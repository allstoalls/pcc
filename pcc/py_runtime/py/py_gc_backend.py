"""GC backend selector and telemetry for pcc-Python runtime archives.

Names are algorithmic, not project-branded:

0. refcount-cycle
1. incremental-tricolor
2. concurrent-mark-sweep
3. generational-minor-major
4. colored-relocating
"""

__pcc_runtime_port__ = True

from pcc.extern import c_abi_export, c_int32, c_int64, c_ptr, c_void, extern
from pcc.py_runtime.py.py_abi_constants import (
    PY_TYPE_BOOL,
    PY_TYPE_BYTEARRAY,
    PY_TYPE_BYTES,
    PY_TYPE_COMPLEX,
    PY_TYPE_CPY_HANDLE,
    PY_TYPE_FLOAT,
    PY_TYPE_INT,
    PY_TYPE_NONE,
    PY_TYPE_STR,
)
from pcc.unsafe import (
    atomic_cas_i64,
    atomic_load_i32,
    atomic_load_i64,
    atomic_rmw_i32,
    atomic_rmw_i64,
    atomic_store_i32,
    atomic_store_i64,
    cstr,
    global_addr,
    global_load_ptr,
    global_store_ptr,
    is_tagged_int,
    load_i32,
    load_i64,
    load_ptr,
    memset,
    memmove,
    free,
    malloc,
    null,
    ptr_add,
    ptr_diff,
    ptr_eq,
    ptr_is_null,
    stack_alloc,
    store_ptr,
    store_i64,
    store_i32,
)

# Slot-role / visit-mode values, inlined as literals at every use site
# (module-level int constants get zeroed in stripped library .o builds):
#   _PY_OBJ_SLOT_OWNED = 1
#   _PY_OBJ_SLOT_BORROWED_TRACED = 2
#   _PY_OBJ_SLOT_BORROWED_UPDATE_ONLY = 3
#   _PY_OBJ_VISIT_TRACE = 1
#   _PY_OBJ_VISIT_PROMOTE = 2
#   _PY_OBJ_VISIT_UPDATE = 3
#   _PY_OBJ_VISIT_SUBTRACT = 4
#   _PY_OBJ_VISIT_CLEAR = 5
#   _PY_OBJ_VISIT_RELOCATE_COUNT = 6
#   _PY_OBJ_VISIT_RELOCATE_FROM = 7
#   _PY_OBJ_VISIT_RELOCATE_TO = 8

# Shared ABI entry points used by backend-level finalization.
py_incref = extern("py_incref", (c_ptr,), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)
pcc_py_gc_defer_tripwire = extern(
    "pcc_py_gc_defer_tripwire", (c_ptr, c_ptr, c_int32), c_void
)
pcc_gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_ptr)
pcc_gc_object_index_find = extern("pcc_gc_object_index_find", (c_ptr,), c_ptr)
pcc_gc_object_index_plan_capacity = extern(
    "pcc_gc_object_index_plan_capacity", (c_int64,), c_int64
)
pcc_gc_object_index_plan_commit = extern(
    "pcc_gc_object_index_plan_commit",
    (c_ptr, c_int64, c_int64),
    c_int64,
)
pcc_gc_object_index_insert_preallocated = extern(
    "pcc_gc_object_index_insert_preallocated", (c_ptr, c_ptr), c_int64
)
pcc_gc_object_index_remove = extern("pcc_gc_object_index_remove", (c_ptr,), c_ptr)
pcc_gc_object_index_clear = extern("pcc_gc_object_index_clear", (), c_void)
pcc_gc_managed_pointer_index_contains = extern(
    "pcc_gc_managed_pointer_index_contains", (c_ptr,), c_int64
)
pcc_gc_granule_is_object_start = extern(
    "pcc_gc_granule_is_object_start", (c_ptr,), c_int64
)
pcc_gc_granule_object_publish = extern(
    "pcc_gc_granule_object_publish", (c_ptr,), c_int64
)
pcc_gc_granule_object_retire = extern(
    "pcc_gc_granule_object_retire", (c_ptr,), c_int64
)
pcc_gc_managed_pointer_index_insert = extern(
    "pcc_gc_managed_pointer_index_insert", (c_ptr,), c_int64
)
pcc_gc_managed_pointer_index_remove = extern(
    "pcc_gc_managed_pointer_index_remove", (c_ptr,), c_int64
)
pcc_capi_is_type_object_value = extern(
    "pcc_capi_is_type_object_value", (c_ptr,), c_int64
)
pcc_gc_forwarding_index_find = extern("pcc_gc_forwarding_index_find", (c_ptr,), c_ptr)
pcc_gc_forwarding_index_insert = extern(
    "pcc_gc_forwarding_index_insert",
    (c_ptr, c_ptr),
    c_int64,
)
pcc_gc_forwarding_index_remove = extern(
    "pcc_gc_forwarding_index_remove",
    (c_ptr,),
    c_ptr,
)
pcc_gc_forwarding_index_clear = extern("pcc_gc_forwarding_index_clear", (), c_void)
pcc_gc_forwarding_target_index_find = extern(
    "pcc_gc_forwarding_target_index_find", (c_ptr,), c_ptr
)
pcc_gc_forwarding_target_index_insert = extern(
    "pcc_gc_forwarding_target_index_insert",
    (c_ptr, c_ptr),
    c_int64,
)
pcc_gc_forwarding_target_index_upsert = extern(
    "pcc_gc_forwarding_target_index_upsert",
    (c_ptr, c_ptr),
    c_int64,
)
pcc_gc_forwarding_target_index_remove = extern(
    "pcc_gc_forwarding_target_index_remove",
    (c_ptr,),
    c_ptr,
)
pcc_gc_forwarding_target_index_clear = extern(
    "pcc_gc_forwarding_target_index_clear", (), c_void
)
pcc_gc_identity_index_find = extern("pcc_gc_identity_index_find", (c_ptr,), c_ptr)
pcc_gc_identity_index_insert = extern(
    "pcc_gc_identity_index_insert",
    (c_ptr, c_ptr),
    c_int64,
)
pcc_gc_identity_index_remove = extern("pcc_gc_identity_index_remove", (c_ptr,), c_ptr)
pcc_gc_identity_index_clear = extern("pcc_gc_identity_index_clear", (), c_void)
_forwarding_head = extern("pcc_gc_forwarding_list_head", (), c_ptr)
_forwarding_find = extern("pcc_gc_forwarding_find", (c_ptr,), c_ptr)
_forwarding_target_exists = extern(
    "pcc_gc_forwarding_target_exists", (c_ptr,), c_int64
)
_forwarding_target_unlink = extern(
    "pcc_gc_forwarding_target_unlink", (c_ptr,), c_void
)
_forwarding_unlink_main = extern(
    "pcc_gc_forwarding_unlink_main", (c_ptr,), c_void
)
_forwarding_clear_all = extern("pcc_gc_forwarding_clear_all", (), c_void)
_identity_head = extern("pcc_gc_identity_list_head", (), c_ptr)
_identity_ensure = extern("pcc_gc_identity_ensure", (c_ptr,), c_ptr)
_identity_remove = extern("pcc_gc_identity_remove", (c_ptr,), c_void)
_identity_clear_all = extern("pcc_gc_identity_clear_all", (), c_void)
_install_forwarding_unlocked = extern(
    "pcc_gc_install_forwarding_unlocked", (c_ptr, c_ptr), c_int64
)
_object_head = extern("pcc_gc_object_list_head", (), c_ptr)
_set_object_head = extern("pcc_gc_object_set_list_head", (c_ptr,), c_void)
_trace_cursor = extern("pcc_gc_trace_cursor_load", (), c_ptr)
_set_trace_cursor = extern("pcc_gc_trace_cursor_store", (c_ptr,), c_void)
_backend3_young_head = extern("pcc_gc_backend3_young_list_head", (), c_ptr)
_set_backend3_young_head = extern(
    "pcc_gc_backend3_young_set_head", (c_ptr,), c_void
)
_object_node_size = extern("pcc_gc_object_node_size", (c_ptr,), c_int64)
_object_node_next = extern("pcc_gc_object_node_next", (c_ptr,), c_ptr)
_set_object_node_next = extern(
    "pcc_gc_object_node_set_next", (c_ptr, c_ptr), c_void
)
_object_node_minor_block = extern(
    "pcc_gc_object_node_minor_block", (c_ptr,), c_ptr
)
_object_node_freeing = extern("pcc_gc_object_node_freeing", (c_ptr,), c_int64)
_set_object_node_freeing = extern(
    "pcc_gc_object_node_set_freeing", (c_ptr, c_int64), c_void
)
_object_node_prev = extern("pcc_gc_object_node_prev", (c_ptr,), c_ptr)
_set_object_node_prev = extern(
    "pcc_gc_object_node_set_prev", (c_ptr, c_ptr), c_void
)
_object_node_zpage = extern("pcc_gc_object_node_zpage", (c_ptr,), c_ptr)
_set_object_node_zpage = extern(
    "pcc_gc_object_node_set_zpage", (c_ptr, c_ptr), c_void
)
_object_node_gc_refs = extern("pcc_gc_object_node_gc_refs", (c_ptr,), c_int64)
_set_object_node_gc_refs = extern(
    "pcc_gc_object_node_set_gc_refs", (c_ptr, c_int64), c_void
)
_object_node_young_next = extern(
    "pcc_gc_object_node_young_next", (c_ptr,), c_ptr
)
_set_object_node_young_next = extern(
    "pcc_gc_object_node_set_young_next", (c_ptr, c_ptr), c_void
)
_object_node_young_prev = extern(
    "pcc_gc_object_node_young_prev", (c_ptr,), c_ptr
)
_set_object_node_young_prev = extern(
    "pcc_gc_object_node_set_young_prev", (c_ptr, c_ptr), c_void
)
_object_node_prepare = extern("pcc_gc_object_node_prepare", (), c_ptr)
_object_node_plan_requires_prepare = extern(
    "pcc_gc_object_node_plan_requires_prepare", (), c_int64
)
_object_node_take_prepared = extern(
    "pcc_gc_object_node_take_prepared", (c_ptr,), c_ptr
)
_object_node_release = extern("pcc_gc_object_node_release", (c_ptr,), c_void)
_unlink_object_node = extern("pcc_gc_object_node_unlink", (c_ptr,), c_void)
_backend3_young_link_head = extern(
    "pcc_gc_backend3_young_link_head", (c_ptr,), c_void
)
_backend3_young_unlink = extern(
    "pcc_gc_backend3_young_unlink", (c_ptr,), c_void
)
_object_known_size = extern("pcc_gc_object_known_size", (c_ptr,), c_int64)
_live_bytes_subtract = extern("pcc_gc_live_bytes_subtract", (c_int64,), c_void)
_relocate_copy_supported_tag = extern(
    "pcc_gc_generational_oldify_supported_tag", (c_int64,), c_int64
)
_mark_forwarded_source_inactive = extern(
    "pcc_gc_generational_mark_forwarded_source_inactive", (c_ptr,), c_void
)
_generational_oldify_copy = extern(
    "pcc_gc_generational_oldify_copy", (c_ptr,), c_ptr
)
_colored_relocate_copy_supported_tag = extern(
    "pcc_gc_backend4_relocate_copy_supported_tag", (c_int64,), c_int64
)
_remap_heal_slot = extern(
    "pcc_gc_backend4_remap_heal_slot", (c_ptr, c_int64), c_void
)
_remap_referents = extern(
    "pcc_gc_backend4_remap_referents", (c_ptr,), c_void
)
_relocate_copy_payload = extern(
    "pcc_gc_relocate_copy_payload",
    (c_ptr, c_ptr, c_int64, c_int64),
    c_int64,
)
pcc_gc_relocate_copy = extern(
    "pcc_gc_relocate_copy", (c_ptr, c_int64), c_ptr
)
_backend4_select_relocation_pages = extern(
    "pcc_gc_backend4_select_relocation_pages", (c_int64,), c_int64
)
pcc_gc_select_relocation_set = extern(
    "pcc_gc_select_relocation_set", (c_int64,), c_int64
)
pcc_gc_backend4_evacuation_drain = extern(
    "pcc_gc_backend4_evacuation_drain", (c_int64,), c_int64
)
pcc_gc_backend4_evacuation_page_drain = extern(
    "pcc_gc_backend4_evacuation_page_drain", (c_int64,), c_int64
)
pcc_gc_backend4_try_zpage_alloc = extern(
    "pcc_gc_backend4_try_zpage_alloc", (c_int64, c_int64), c_ptr
)
_backend4_zpage_track_alloc = extern(
    "pcc_gc_backend4_zpage_track_alloc", (c_ptr, c_int64), c_ptr
)
_backend4_zpage_track_page_prepare = extern(
    "pcc_gc_backend4_zpage_track_page_prepare",
    (c_ptr, c_ptr, c_int64),
    c_ptr,
)
_backend4_zpage_track_alloc_preallocated = extern(
    "pcc_gc_backend4_zpage_track_alloc_preallocated",
    (c_ptr, c_int64, c_ptr, c_ptr, c_int64),
    c_ptr,
)
_backend4_active_page = extern(
    "pcc_gc_backend4_zpage_active_page", (c_int64, c_int64), c_ptr
)
_backend4_set_active_page = extern(
    "pcc_gc_backend4_zpage_set_active_page", (c_ptr,), c_void
)
_backend4_clear_active_page = extern(
    "pcc_gc_backend4_zpage_clear_active_page", (c_ptr,), c_void
)
_backend4_zpage_find_reusable_page_for_gen = extern(
    "pcc_gc_backend4_zpage_find_reusable_page_for_gen",
    (c_int64, c_int64),
    c_ptr,
)
_backend4_zpage_find_reusable_page = extern(
    "pcc_gc_backend4_zpage_find_reusable_page", (c_ptr, c_int64), c_ptr
)
_backend4_zpage_pop_free_page = extern(
    "pcc_gc_backend4_zpage_pop_free_page", (c_int64,), c_ptr
)
_backend4_zpage_reset = extern(
    "pcc_gc_backend4_zpage_reset", (c_ptr, c_ptr, c_int64), c_void
)
_backend4_zpage_node_alloc = extern(
    "pcc_gc_backend4_zpage_node_alloc", (), c_ptr
)
_backend4_zpage_node_prepare = extern(
    "pcc_gc_backend4_zpage_node_prepare", (), c_ptr
)
_backend4_zpage_node_plan_requires_prepare = extern(
    "pcc_gc_backend4_zpage_node_plan_requires_prepare", (), c_int64
)
_backend4_zpage_node_release = extern(
    "pcc_gc_backend4_zpage_node_release", (c_ptr,), c_void
)
_backend4_zpage_link_node = extern(
    "pcc_gc_backend4_zpage_link_node", (c_ptr,), c_void
)
_backend4_zpage_find_page_for_addr = extern(
    "pcc_gc_backend4_zpage_find_page_for_addr", (c_ptr, c_int64), c_ptr
)
_backend4_free_page_count_for_class = extern(
    "pcc_gc_backend4_free_page_count_for_class", (c_int64,), c_int64
)
_backend4_free_page_limit_for_class = extern(
    "pcc_gc_backend4_free_page_limit_for_class", (c_int64,), c_int64
)
_backend4_zpage_clear_reusable_state = extern(
    "pcc_gc_backend4_zpage_clear_reusable_state", (c_ptr,), c_void
)
_backend4_zpage_cache = extern(
    "pcc_gc_backend4_zpage_cache", (c_ptr,), c_void
)
_backend4_zpage_destroy = extern(
    "pcc_gc_backend4_zpage_destroy", (c_ptr,), c_void
)
_backend4_zpage_recycle = extern(
    "pcc_gc_backend4_zpage_recycle", (c_ptr,), c_void
)
_backend4_zpage_page_head = extern(
    "pcc_gc_backend4_zpage_page_head", (c_ptr,), c_ptr
)
_backend4_zpage_set_page_head = extern(
    "pcc_gc_backend4_zpage_set_page_head", (c_ptr, c_ptr), c_void
)
_backend4_zpage_unlink_node = extern(
    "pcc_gc_backend4_zpage_unlink_node", (c_ptr,), c_void
)
_backend4_zpage_find = extern(
    "pcc_gc_backend4_zpage_find", (c_ptr,), c_ptr
)
_backend4_zpage_unlink_page = extern(
    "pcc_gc_backend4_zpage_unlink_page", (c_ptr,), c_void
)
_backend4_zpage_find_owner_for_page = extern(
    "pcc_gc_backend4_zpage_find_owner_for_page", (c_ptr,), c_ptr
)
_backend4_zpage_remove_payload_spans = extern(
    "pcc_gc_backend4_zpage_remove_payload_spans", (c_ptr,), c_void
)
_backend4_zpage_remove_payload_span_base = extern(
    "pcc_gc_backend4_zpage_remove_payload_span_base",
    (c_ptr, c_ptr),
    c_int64,
)
_backend4_zpage_remove = extern(
    "pcc_gc_backend4_zpage_remove", (c_ptr,), c_void
)
_forwarding_remove = extern("pcc_gc_forwarding_remove", (c_ptr,), c_void)
_forwarding_detach_into_finish = extern(
    "pcc_gc_forwarding_detach_into_finish", (c_ptr, c_ptr), c_void
)
_forwarding_remove_target = extern(
    "pcc_gc_forwarding_remove_target", (c_ptr, c_ptr), c_void
)
_backend4_park_page = extern(
    "pcc_gc_backend4_park_page", (c_ptr,), c_void
)
_backend4_drain_parked_pages = extern(
    "pcc_gc_backend4_drain_parked_pages", (), c_void
)
_backend4_finish_retained_page_releases = extern(
    "pcc_gc_backend4_finish_retained_page_releases", (c_ptr,), c_void
)
_backend4_finish_remap_retirement = extern(
    "pcc_gc_backend4_finish_remap_retirement", (c_ptr,), c_void
)
_backend4_note_forwarding_removed_on_page = extern(
    "pcc_gc_backend4_note_forwarding_removed_on_page", (c_ptr,), c_void
)
_backend4_zpage_note_forwarding_removed = extern(
    "pcc_gc_backend4_zpage_note_forwarding_removed", (c_ptr,), c_void
)
pcc_gc_step = extern("pcc_gc_step", (c_int64,), c_int64)
pcc_gc_note_slot_write_barrier = extern(
    "pcc_gc_note_slot_write_barrier", (c_ptr, c_ptr, c_ptr), c_void
)
pcc_gc_note_write_barrier = extern(
    "pcc_gc_note_write_barrier", (c_ptr, c_ptr), c_void
)
_backend3_remember_owner = extern(
    "pcc_gc_backend3_remember_owner", (c_ptr, c_int64), c_void
)
_backend3_clear_remembered_owners = extern(
    "pcc_gc_backend3_clear_remembered_owners", (), c_ptr
)
_backend3_finish_detached_remembered_owners = extern(
    "pcc_gc_backend3_finish_detached_remembered_owners", (c_ptr,), c_void
)
_promote_young_if_known = extern(
    "pcc_gc_generational_promote_young_if_known", (c_ptr,), c_void
)
_promote_young_slot_mode = extern(
    "pcc_gc_generational_promote_owned_slot_mode",
    (c_ptr, c_int64, c_int64),
    c_void,
)
_promote_young_borrowed_slot_mode = extern(
    "pcc_gc_generational_promote_borrowed_slot_mode",
    (c_ptr, c_int64, c_int64),
    c_void,
)
_trace_referents_for_promotion_mode = extern(
    "pcc_gc_trace_referents_for_promotion_mode", (c_ptr, c_int64), c_void
)
_trace_referents_for_promotion = extern(
    "pcc_gc_trace_referents_for_promotion", (c_ptr,), c_void
)
_step_generational_promotion = extern(
    "pcc_gc_generational_step", (c_int64, c_int64), c_int64
)
pcc_gc_frame_index_insert = extern(
    "pcc_gc_frame_index_insert",
    (c_ptr, c_ptr),
    c_int64,
)
pcc_gc_frame_index_find = extern("pcc_gc_frame_index_find", (c_ptr,), c_ptr)
pcc_gc_frame_index_replace = extern(
    "pcc_gc_frame_index_replace",
    (c_ptr, c_ptr),
    c_ptr,
)
pcc_gc_frame_index_remove = extern("pcc_gc_frame_index_remove", (c_ptr,), c_ptr)
pcc_gc_frame_node_tls_pool_drain = extern(
    "pcc_gc_frame_node_tls_pool_drain",
    (),
    c_void,
)
pcc_gc_zpage_owner_index_find = extern(
    "pcc_gc_zpage_owner_index_find",
    (c_ptr,),
    c_ptr,
)
pcc_gc_zpage_owner_index_insert = extern(
    "pcc_gc_zpage_owner_index_insert",
    (c_ptr, c_ptr),
    c_int64,
)
pcc_gc_zpage_owner_index_upsert = extern(
    "pcc_gc_zpage_owner_index_upsert",
    (c_ptr, c_ptr),
    c_int64,
)
pcc_gc_zpage_owner_index_plan_capacity = extern(
    "pcc_gc_zpage_owner_index_plan_capacity", (c_int64,), c_int64
)
pcc_gc_zpage_owner_index_plan_commit = extern(
    "pcc_gc_zpage_owner_index_plan_commit",
    (c_ptr, c_int64, c_int64),
    c_int64,
)
pcc_gc_zpage_owner_index_remove = extern(
    "pcc_gc_zpage_owner_index_remove",
    (c_ptr,),
    c_ptr,
)
pcc_gc_zpage_page_index_find = extern(
    "pcc_gc_zpage_page_index_find",
    (c_ptr,),
    c_ptr,
)
pcc_gc_zpage_page_index_insert = extern(
    "pcc_gc_zpage_page_index_insert",
    (c_ptr, c_ptr),
    c_int64,
)
pcc_gc_zpage_page_index_upsert = extern(
    "pcc_gc_zpage_page_index_upsert",
    (c_ptr, c_ptr),
    c_int64,
)
pcc_gc_zpage_page_index_remove = extern(
    "pcc_gc_zpage_page_index_remove",
    (c_ptr,),
    c_ptr,
)
pcc_gc_load_ptr_extern = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_gc_store_root_plan_init = extern(
    "pcc_gc_store_root_plan_init", (c_ptr, c_int64), c_void
)
pcc_gc_store_root_plan_commit_locked = extern(
    "pcc_gc_store_root_plan_commit_locked",
    (c_ptr, c_ptr, c_ptr),
    c_int64,
)
pcc_gc_store_root_plan_finish = extern(
    "pcc_gc_store_root_plan_finish", (c_ptr,), c_void
)
py_tls_exc_get = extern("py_tls_exc_get", (), c_ptr)
py_tls_exc_set = extern("py_tls_exc_set", (c_ptr,), c_void)
pcc_mutex_new = extern("pcc_mutex_new", (), c_ptr)
pcc_mutex_free = extern("pcc_mutex_free", (c_ptr,), c_void)
pcc_mutex_lock = extern("pcc_mutex_lock", (c_ptr,), c_int64)
pcc_mutex_unlock = extern("pcc_mutex_unlock", (c_ptr,), c_int64)
pcc_capi_is_cext_type_tag = extern("pcc_capi_is_cext_type_tag", (c_int64,), c_int64)
pcc_gc_visit_object_slots = extern(
    "pcc_gc_visit_object_slots", (c_ptr, c_ptr, c_ptr), c_int64
)
abort_extern = extern("pcc_platform_abort", (), c_void)
pcc_threads_enabled = extern("pcc_threads_enabled", (), c_int64)
pcc_current_thread_id = extern("pcc_current_thread_id", (), c_int64)
pcc_thread_safepoint = extern("pcc_thread_safepoint", (), c_void)
pcc_stop_the_world = extern("pcc_stop_the_world", (), c_int64)
pcc_resume_world = extern("pcc_resume_world", (), c_int64)
pcc_runtime_now_us = extern("pcc_platform_monotonic_us", (), c_int64)
pcc_py_gc_minor_current_get = extern("pcc_py_gc_minor_current_get", (), c_ptr)
pcc_py_gc_minor_current_set = extern("pcc_py_gc_minor_current_set", (c_ptr,), c_void)
pcc_py_gc_pending_minor_block_get = extern(
    "pcc_py_gc_pending_minor_block_get", (), c_ptr
)
pcc_py_gc_pending_minor_block_set = extern(
    "pcc_py_gc_pending_minor_block_set", (c_ptr,), c_void
)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)
pcc_gc_scheduler_root_link_locked = extern(
    "pcc_gc_scheduler_root_link_locked", (c_ptr,), c_void
)
pcc_gc_scheduler_root_unlink_locked = extern(
    "pcc_gc_scheduler_root_unlink_locked", (c_ptr,), c_int64
)
pcc_gc_cycle_requested_store_release = extern(
    "pcc_gc_cycle_requested_store_release", (c_int64,), c_void
)
_py_visit_registered_root_slots = extern(
    "pcc_gc_visit_registered_root_slots",
    (c_int64, c_int64),
    c_int64,
)
_gray_mapped_roots = extern(
    "pcc_gc_gray_mapped_roots", (c_ptr, c_ptr, c_int64), c_int64
)
_rewrite_mapped_roots = extern(
    "pcc_gc_rewrite_mapped_roots", (c_ptr, c_ptr), c_int64
)
_gc_gray_count_load_acquire = extern(
    "pcc_gc_gray_count_load_acquire", (), c_int64
)
_gc_gray_count_store_release = extern(
    "pcc_gc_gray_count_store_release", (c_int64,), c_void
)
_gc_gray_count_increment_acq_rel = extern(
    "pcc_gc_gray_count_increment_acq_rel", (), c_void
)
_gc_gray_count_decrement_acq_rel = extern(
    "pcc_gc_gray_count_decrement_acq_rel", (), c_void
)
_gc_object_is_known_no_lock = extern(
    "pcc_gc_object_is_known_no_lock", (c_ptr,), c_int64
)
_mark_root_gray_if_known = extern(
    "pcc_gc_mark_root_gray_if_known", (c_ptr,), c_void
)
_resolve_root_slot_unlocked = extern(
    "pcc_gc_resolve_root_slot_unlocked", (c_ptr, c_int64), c_ptr
)
_object_node_is_active = extern(
    "pcc_gc_object_node_is_active", (c_ptr,), c_int64
)
_trace_referents = extern("pcc_gc_trace_referents", (c_ptr,), c_void)
_seed_roots = extern("pcc_gc_seed_roots", (), c_void)
_drain_all_gray_unlocked = extern(
    "pcc_gc_drain_all_gray_unlocked", (), c_int64
)
_begin_mark_cycle = extern("pcc_gc_begin_mark_cycle", (), c_void)
_finish_tracing_cycle = extern(
    "pcc_gc_finish_tracing_cycle", (c_int64, c_int64), c_int64
)
_tracing_cycle_epoch_advance_unlocked = extern(
    "pcc_gc_tracing_cycle_epoch_advance_unlocked", (), c_int64
)
_clear_unreachable = extern("pcc_gc_tracing_clear_unreachable", (c_ptr,), c_void)
_has_sweep_candidate = extern(
    "pcc_gc_tracing_has_sweep_candidate", (), c_int64
)
_sweep_unreachable = extern(
    "pcc_gc_tracing_sweep_unreachable", (c_int64,), c_int64
)
_init_config = extern("pcc_gc_config_ensure", (), c_int64)
_maybe_start_cms_worker = extern("pcc_gc_maybe_start_cms_worker", (), c_void)
_record_pause = extern(
    "pcc_gc_tracing_record_pause", (c_int64, c_int64), c_void
)
_stop_cms_worker = extern("pcc_gc_cms_stop_worker", (), c_void)
_step_tracing = extern("pcc_gc_tracing_step_cycle", (c_int64,), c_int64)
_step_incremental_concurrent = extern(
    "pcc_gc_incremental_concurrent_step", (c_int64,), c_int64
)

def _atomic_i32_load(slot) -> int:
    if ptr_is_null(slot) != 0:
        return 0
    return atomic_load_i32(slot, 0, "relaxed")


def _atomic_i32_store(slot, value: int) -> None:
    if ptr_is_null(slot) != 0:
        return
    atomic_store_i32(slot, 0, value, "relaxed")


def _atomic_i32_add_fetch(slot, delta: int) -> int:
    if ptr_is_null(slot) != 0:
        return 0
    old: int = atomic_rmw_i32("add", slot, 0, delta, "relaxed")
    return old + delta


def _atomic_i64_load(slot) -> int:
    if ptr_is_null(slot) != 0:
        return 0
    return atomic_load_i64(slot, 0, "acquire")


def _atomic_i64_store(slot, value: int) -> None:
    if ptr_is_null(slot) != 0:
        return
    atomic_store_i64(slot, 0, value, "release")


def _atomic_i64_add_fetch(slot, delta: int) -> int:
    if ptr_is_null(slot) != 0:
        return 0
    old: int = atomic_rmw_i64("add", slot, 0, delta, "acq_rel")
    return old + delta


def _atomic_i64_dec_if_positive(slot) -> int:
    if ptr_is_null(slot) != 0:
        return 0
    live: int = atomic_load_i64(slot, 0, "acquire")
    while live > 0:
        observed: int = atomic_cas_i64(
            slot,
            0,
            live,
            live - 1,
            "acq_rel",
            "acquire",
        )
        if observed == live:
            return live - 1
        live = observed
    return live


def _gc_ptr_can_have_header(o) -> bool:
    return pcc_gc_pointer_is_managed(o) != 0


def _minor_collect_reset() -> None:
    _atomic_i32_add_fetch(global_addr("pcc_gc_minor_collections"), 1)
    if load_i32(global_addr("pcc_gc_backend_selected"), 0) == 3:
        _step_generational_promotion(1024, 0)
    _atomic_i32_store(global_addr("pcc_gc_minor_bytes"), 0)


def _note_minor_alloc(bytes: int) -> None:
    max_size: int = load_i32(global_addr("pcc_gc_minor_alloc_max"), 0)
    if bytes <= 0 or bytes > max_size:
        return
    _atomic_i32_add_fetch(global_addr("pcc_gc_minor_allocations"), 1)
    _atomic_i32_add_fetch(global_addr("pcc_gc_minor_bytes"), bytes)


def _align16(bytes: int) -> int:
    if bytes <= 0:
        return 0
    return (bytes + 15) & ~15


def _minor_blocks_head():
    return global_load_ptr("pcc_gc_minor_blocks")


def _set_minor_blocks_head(head) -> None:
    global_store_ptr("pcc_gc_minor_blocks", head)


def _minor_current():
    return pcc_py_gc_minor_current_get()


def _set_minor_current(block) -> None:
    pcc_py_gc_minor_current_set(block)


def _pending_minor_block():
    return pcc_py_gc_pending_minor_block_get()


def _set_pending_minor_block(block) -> None:
    pcc_py_gc_pending_minor_block_set(block)


def _minor_block_base(block):
    return load_ptr(block, 0)


def _minor_block_used(block) -> int:
    return load_i64(block, 8)


def _set_minor_block_used(block, used: int) -> None:
    store_i64(block, 8, used)


def _minor_block_size(block) -> int:
    return load_i64(block, 16)


def _minor_block_next(block):
    return load_ptr(block, 24)


def _set_minor_block_next(block, nxt) -> None:
    store_ptr(block, 24, nxt)


def _minor_block_live(block) -> int:
    return _atomic_i64_load(ptr_add(block, 32))


def _set_minor_block_live(block, live: int) -> None:
    _atomic_i64_store(ptr_add(block, 32), live)


def _minor_block_owner(block) -> int:
    return load_i64(block, 40)


def _minor_new_block(min_bytes: int):
    block_bytes: int = load_i32(global_addr("pcc_gc_minor_heap_size"), 0)
    if block_bytes < min_bytes:
        block_bytes = min_bytes
    block_bytes = _align16(block_bytes)
    if block_bytes <= 0:
        return null()

    block = malloc(48)
    if ptr_is_null(block) != 0:
        return null()
    base = malloc(block_bytes)
    if ptr_is_null(base) != 0:
        free(block)
        return null()
    memset(base, 0, block_bytes)
    store_ptr(block, 0, base)
    store_i64(block, 8, 0)
    store_i64(block, 16, block_bytes)
    store_i64(block, 32, 0)
    store_i64(block, 40, pcc_current_thread_id())
    pcc_py_gc_minor_graph_lock()
    store_ptr(block, 24, _minor_blocks_head())
    _set_minor_blocks_head(block)
    pcc_py_gc_minor_graph_unlock()
    _set_minor_current(block)
    _atomic_i32_add_fetch(global_addr("pcc_gc_minor_arena_refills"), 1)
    return block


def _minor_release_block(block) -> None:
    if ptr_is_null(block) != 0:
        return
    live: int = _atomic_i64_dec_if_positive(ptr_add(block, 32))
    if live != 0:
        return
    # Span-retain empty minor blocks so stale SSA/root pointers stay
    # recognizable by the free-path span fallback.
    if _minor_block_owner(block) == pcc_current_thread_id():
        _atomic_i64_store(ptr_add(block, 8), 0)
        _set_minor_current(block)
        _atomic_i32_store(global_addr("pcc_gc_minor_bytes"), 0)
    return


def _minor_block_containing(ptr):
    if ptr_is_null(ptr) != 0:
        return null()
    node = _minor_blocks_head()
    while ptr_is_null(node) == 0:
        base = _minor_block_base(node)
        delta: int = ptr_diff(ptr, base)
        if delta >= 0 and delta < _minor_block_size(node):
            return node
        node = _minor_block_next(node)
    return null()


def _minor_find_reusable_block(min_bytes: int):
    owner: int = pcc_current_thread_id()
    pcc_py_gc_minor_graph_lock()
    node = _minor_blocks_head()
    while ptr_is_null(node) == 0:
        if (
            _minor_block_owner(node) == owner
            and _minor_block_live(node) == 0
            and _minor_block_size(node) >= min_bytes
        ):
            _set_minor_block_used(node, 0)
            pcc_py_gc_minor_graph_unlock()
            _set_minor_current(node)
            _atomic_i32_store(global_addr("pcc_gc_minor_bytes"), 0)
            return node
        node = _minor_block_next(node)
    pcc_py_gc_minor_graph_unlock()
    return null()


@c_abi_export("pcc_gc_try_minor_alloc")
def pcc_gc_try_minor_alloc(bytes: int):
    backend: int = 0
    if load_i32(global_addr("pcc_gc_config_initialized"), 0) == 0:
        backend = _init_config()
    else:
        backend = load_i32(global_addr("pcc_gc_backend_selected"), 0)
    _set_pending_minor_block(null())
    if backend != 3:
        return null()

    aligned: int = _align16(bytes)
    max_size: int = load_i32(global_addr("pcc_gc_minor_alloc_max"), 0)
    if aligned <= 0 or aligned > max_size:
        _atomic_i32_add_fetch(global_addr("pcc_gc_minor_arena_fallbacks"), 1)
        return null()

    block = _minor_current()
    if ptr_is_null(block) == 0:
        used: int = _minor_block_used(block)
        total: int = _minor_block_size(block)
        if total - used < aligned:
            if (
                _minor_block_owner(block) == pcc_current_thread_id()
                and _minor_block_live(block) == 0
                and total >= aligned
            ):
                _set_minor_block_used(block, 0)
                _atomic_i32_store(global_addr("pcc_gc_minor_bytes"), 0)
            else:
                _minor_collect_reset()
                block = _minor_find_reusable_block(aligned)
    if ptr_is_null(block) != 0:
        block = _minor_find_reusable_block(aligned)
    if ptr_is_null(block) != 0:
        block = _minor_new_block(aligned)
        if ptr_is_null(block) != 0:
            _atomic_i32_add_fetch(global_addr("pcc_gc_minor_arena_fallbacks"), 1)
            return null()

    used2: int = _minor_block_used(block)
    mem = ptr_add(_minor_block_base(block), used2)
    _set_minor_block_used(block, used2 + aligned)
    _atomic_i64_add_fetch(ptr_add(block, 32), 1)
    _set_pending_minor_block(block)
    _atomic_i32_add_fetch(global_addr("pcc_gc_minor_arena_bumps"), 1)
    _note_minor_alloc(aligned)
    memset(mem, 0, bytes)
    return mem


def _counter_global(metric: int):
    if metric == 0:
        return global_addr("pcc_gc_metric_alloc")
    if metric == 1:
        return global_addr("pcc_gc_metric_store")
    if metric == 2:
        return global_addr("pcc_gc_metric_load")
    if metric == 3:
        return global_addr("pcc_gc_metric_safepoint")
    if metric == 4:
        return global_addr("pcc_gc_metric_pin")
    if metric == 5:
        return global_addr("pcc_gc_metric_step")
    return global_addr("pcc_gc_metric_step")


def _counter_inc(metric: int, delta: int) -> None:
    slot = _counter_global(metric)
    v: int = load_i32(slot, 0)
    store_i32(slot, 0, v + delta)


def _gray_count() -> int:
    return _gc_gray_count_load_acquire()


def _set_gray_count(value: int) -> None:
    _gc_gray_count_store_release(value)


def _inc_gray_count() -> None:
    _gc_gray_count_increment_acq_rel()


def _dec_gray_count() -> None:
    _gc_gray_count_decrement_acq_rel()


def _object_graph_lock() -> None:
    pcc_py_gc_minor_graph_lock()


def _object_graph_unlock() -> None:
    pcc_py_gc_minor_graph_unlock()


def _gc_tracks_objects() -> int:
    return pcc_gc_backend() != 0


def _backend3_graph_leaf_tag(tag: int) -> int:
    if tag == PY_TYPE_NONE:
        return 1
    if tag == PY_TYPE_BOOL:
        return 1
    if tag == PY_TYPE_INT:
        return 1
    if tag == PY_TYPE_FLOAT:
        return 1
    if tag == PY_TYPE_STR:
        return 1
    if tag == PY_TYPE_COMPLEX:
        return 1
    if tag == PY_TYPE_BYTES:
        return 1
    if tag == PY_TYPE_BYTEARRAY:
        return 1
    if tag == PY_TYPE_CPY_HANDLE:
        return 1
    return 0


def _should_track_frame_roots() -> int:
    if load_i32(global_addr("pcc_gc_backend_selected"), 0) != 0:
        return 1
    return load_i32(global_addr("pcc_gc_backend0_frame_roots_enabled"), 0)


def _frame_roots_disabled_fast() -> int:
    if load_i32(global_addr("pcc_gc_config_initialized"), 0) == 0:
        return 0
    if load_i32(global_addr("pcc_gc_backend_selected"), 0) != 0:
        return 0
    if load_i32(global_addr("pcc_gc_backend0_frame_roots_enabled"), 0) != 0:
        return 0
    return 1


def _clear_object_list() -> None:
    _object_graph_lock()
    global_store_ptr("pcc_gc_backend4_reset_object_cursor", null())
    global_store_ptr("pcc_gc_backend3_remembered_scan_cursor", null())
    store_i64(global_addr("pcc_gc_backend3_remembered_scan_revision"), 0, 0)
    node = _object_head()
    while ptr_is_null(node) == 0:
        nxt = _object_node_next(node)
        free(node)
        node = nxt
    _set_object_head(null())
    _set_trace_cursor(null())
    _set_backend3_young_head(null())
    _set_gray_count(0)
    pcc_gc_object_index_clear()
    _object_graph_unlock()
    global_store_ptr("pcc_gc_last_alloc", null())


def _relocation_set_head():
    return global_load_ptr("pcc_gc_relocation_set_head")


def _set_relocation_set_head(head) -> None:
    global_store_ptr("pcc_gc_relocation_set_head", head)


def _store_buffer_head():
    return global_load_ptr("pcc_gc_backend4_store_buffer_head")


def _set_store_buffer_head(head) -> None:
    global_store_ptr("pcc_gc_backend4_store_buffer_head", head)


def _store_buffer_medium_head():
    return global_load_ptr("pcc_gc_backend4_store_buffer_medium_head")


def _set_store_buffer_medium_head(head) -> None:
    global_store_ptr("pcc_gc_backend4_store_buffer_medium_head", head)


def _zpage_head():
    return global_load_ptr("pcc_gc_backend4_zpage_head")


def _set_zpage_head(head) -> None:
    global_store_ptr("pcc_gc_backend4_zpage_head", head)


def _zpage_payload_span_head():
    return global_load_ptr("pcc_gc_backend4_zpage_payload_span_head")


def _set_zpage_payload_span_head(head) -> None:
    global_store_ptr("pcc_gc_backend4_zpage_payload_span_head", head)


def _zpage_page_head():
    return global_load_ptr("pcc_gc_backend4_page_head")


def _set_zpage_page_head(head) -> None:
    global_store_ptr("pcc_gc_backend4_page_head", head)


def _zpage_free_page_head():
    return global_load_ptr("pcc_gc_backend4_free_page_head")


def _set_zpage_free_page_head(head) -> None:
    global_store_ptr("pcc_gc_backend4_free_page_head", head)


def _zpage_retained_page_head():
    return global_load_ptr("pcc_gc_backend4_retained_page_head")


def _set_zpage_retained_page_head(head) -> None:
    global_store_ptr("pcc_gc_backend4_retained_page_head", head)


def _evacuation_page_head():
    return global_load_ptr("pcc_gc_backend4_evacuation_page_head")


def _set_evacuation_page_head(head) -> None:
    global_store_ptr("pcc_gc_backend4_evacuation_page_head", head)


@c_abi_export("pcc_gc_backend4_relocation_set_find")
def _relocation_set_find(obj):
    if ptr_is_null(obj) != 0 or is_tagged_int(obj) != 0:
        return null()
    node = _relocation_set_head()
    while ptr_is_null(node) == 0:
        if ptr_eq(load_ptr(node, 0), obj) != 0:
            return node
        node = load_ptr(node, 8)
    return null()


@c_abi_export("pcc_gc_backend4_relocation_set_add")
def _relocation_set_add(obj) -> int:
    if ptr_is_null(obj) != 0 or is_tagged_int(obj) != 0:
        return 0
    if load_i64(
        global_addr("pcc_gc_backend4_relocation_reset_owner"), 0
    ) != 0:
        return 0
    flags: int = load_i32(obj, 12)
    if (flags & (64 | 8192 | 16384 | 524288)) != 0:
        return 0
    if ptr_is_null(_forwarding_find(obj)) == 0:
        return 0
    if _forwarding_target_exists(obj) != 0:
        return 0
    if ptr_is_null(_relocation_set_find(obj)) == 0:
        return 0
    node = malloc(16)
    if ptr_is_null(node) != 0:
        return 0
    store_ptr(node, 0, obj)
    store_ptr(node, 8, _relocation_set_head())
    _set_relocation_set_head(node)
    revision: int = load_i64(
        global_addr("pcc_gc_backend4_reseed_relocation_revision"), 0
    )
    store_i64(
        global_addr("pcc_gc_backend4_reseed_relocation_revision"),
        0,
        revision + 1,
    )
    store_i32(obj, 12, flags | 2048)
    return 1


@c_abi_export("pcc_gc_backend4_relocation_set_remove")
def _relocation_set_remove(obj) -> None:
    if ptr_is_null(obj) != 0 or is_tagged_int(obj) != 0:
        return
    prev = null()
    node = _relocation_set_head()
    while ptr_is_null(node) == 0:
        nxt = load_ptr(node, 8)
        if ptr_eq(load_ptr(node, 0), obj) != 0:
            if ptr_eq(
                global_load_ptr("pcc_gc_backend4_reseed_relocation_cursor"),
                node,
            ) != 0:
                global_store_ptr(
                    "pcc_gc_backend4_reseed_relocation_cursor", nxt
                )
            revision: int = load_i64(
                global_addr("pcc_gc_backend4_reseed_relocation_revision"), 0
            )
            store_i64(
                global_addr("pcc_gc_backend4_reseed_relocation_revision"),
                0,
                revision + 1,
            )
            if ptr_is_null(prev) != 0:
                _set_relocation_set_head(nxt)
            else:
                store_ptr(prev, 8, nxt)
            if ptr_is_null(_forwarding_find(obj)) != 0:
                flags: int = load_i32(obj, 12)
                store_i32(obj, 12, flags & ~2048)
            free(node)
            return
        prev = node
        node = nxt


def _backend4_store_buffer_dec() -> None:
    pending: int = load_i32(
        global_addr("pcc_gc_backend4_store_buffer_entries_count"), 0
    )
    if pending > 0:
        store_i32(
            global_addr("pcc_gc_backend4_store_buffer_entries_count"),
            0,
            pending - 1,
        )


def _backend4_store_buffer_contains(owner, slot, value) -> int:
    if ptr_is_null(owner) != 0 or is_tagged_int(owner) != 0:
        return 0
    if ptr_is_null(value) != 0 or is_tagged_int(value) != 0:
        return 0
    node = _store_buffer_medium_head()
    while ptr_is_null(node) == 0:
        if ptr_eq(load_ptr(node, 0), owner) != 0:
            if ptr_eq(load_ptr(node, 8), slot) != 0:
                if ptr_eq(load_ptr(node, 16), value) != 0:
                    return 1
        node = load_ptr(node, 24)
    node = _store_buffer_head()
    while ptr_is_null(node) == 0:
        if ptr_eq(load_ptr(node, 0), owner) != 0:
            if ptr_eq(load_ptr(node, 8), slot) != 0:
                if ptr_eq(load_ptr(node, 16), value) != 0:
                    return 1
        node = load_ptr(node, 24)
    return 0


def _backend4_store_buffer_medium_capacity() -> int:
    return 32


def _backend4_store_buffer_medium_count() -> int:
    return load_i32(global_addr("pcc_gc_backend4_store_buffer_medium_count"), 0)


def _backend4_store_buffer_medium_set_count(count: int) -> None:
    store_i32(global_addr("pcc_gc_backend4_store_buffer_medium_count"), 0, count)


def _backend4_store_buffer_append_global_owned(owner, slot, value) -> None:
    node = malloc(32)
    if ptr_is_null(node) != 0:
        _backend4_store_buffer_dec()
        py_decref(value)
        return
    store_ptr(node, 0, owner)
    store_ptr(node, 8, slot)
    store_ptr(node, 16, value)
    store_ptr(node, 24, _store_buffer_head())
    _set_store_buffer_head(node)


def _backend4_store_buffer_flush_medium_locked() -> None:
    count: int = _backend4_store_buffer_medium_count()
    if count <= 0:
        return
    node = _store_buffer_medium_head()
    _set_store_buffer_medium_head(null())
    while ptr_is_null(node) == 0:
        nxt = load_ptr(node, 24)
        _backend4_store_buffer_append_global_owned(
            load_ptr(node, 0),
            load_ptr(node, 8),
            load_ptr(node, 16),
        )
        free(node)
        node = nxt
    _backend4_store_buffer_medium_set_count(0)
    flushes: int = load_i32(
        global_addr("pcc_gc_backend4_store_buffer_medium_flushes_count"), 0
    )
    store_i32(
        global_addr("pcc_gc_backend4_store_buffer_medium_flushes_count"), 0, flushes + 1
    )
    flushed: int = load_i32(
        global_addr("pcc_gc_backend4_store_buffer_medium_flushed_entries_count"), 0
    )
    store_i32(
        global_addr("pcc_gc_backend4_store_buffer_medium_flushed_entries_count"),
        0,
        flushed + count,
    )
    if count >= _backend4_store_buffer_medium_capacity():
        full: int = load_i32(
            global_addr("pcc_gc_backend4_store_buffer_medium_full_flushes_count"), 0
        )
        store_i32(
            global_addr("pcc_gc_backend4_store_buffer_medium_full_flushes_count"),
            0,
            full + 1,
        )


@c_abi_export("pcc_gc_backend4_store_buffer_enqueue")
def _backend4_store_buffer_enqueue(owner, slot, value) -> int:
    if ptr_is_null(owner) != 0 or is_tagged_int(owner) != 0:
        return 0
    if ptr_is_null(value) != 0 or is_tagged_int(value) != 0:
        return 0
    if _backend4_store_buffer_contains(owner, slot, value) != 0:
        skips: int = load_i32(
            global_addr("pcc_gc_backend4_store_buffer_duplicate_skips_count"), 0
        )
        store_i32(
            global_addr("pcc_gc_backend4_store_buffer_duplicate_skips_count"),
            0,
            skips + 1,
        )
        return 0
    flags: int = load_i32(owner, 12)
    if (
        _backend4_store_buffer_medium_count()
        >= _backend4_store_buffer_medium_capacity()
    ):
        _backend4_store_buffer_flush_medium_locked()
    if (
        _backend4_store_buffer_medium_count()
        >= _backend4_store_buffer_medium_capacity()
    ):
        return 0
    node = malloc(32)
    if ptr_is_null(node) != 0:
        return 0
    py_incref(value)
    store_ptr(node, 0, owner)
    store_ptr(node, 8, slot)
    store_ptr(node, 16, value)
    store_ptr(node, 24, _store_buffer_medium_head())
    _set_store_buffer_medium_head(node)
    _backend4_store_buffer_medium_set_count(_backend4_store_buffer_medium_count() + 1)
    _backend4_remembered_set_add(owner, slot)
    store_i32(owner, 12, flags | 512)
    pending: int = load_i32(
        global_addr("pcc_gc_backend4_store_buffer_entries_count"), 0
    )
    store_i32(
        global_addr("pcc_gc_backend4_store_buffer_entries_count"),
        0,
        pending + 1,
    )
    high_water: int = load_i32(
        global_addr("pcc_gc_backend4_store_buffer_high_water_count"), 0
    )
    if pending + 1 > high_water:
        store_i32(
            global_addr("pcc_gc_backend4_store_buffer_high_water_count"),
            0,
            pending + 1,
        )
    owner_fanout: int = _backend4_store_buffer_owner_fanout(owner)
    owner_high_water: int = load_i32(
        global_addr("pcc_gc_backend4_store_buffer_owner_fanout_high_water_count"), 0
    )
    if owner_fanout > owner_high_water:
        store_i32(
            global_addr("pcc_gc_backend4_store_buffer_owner_fanout_high_water_count"),
            0,
            owner_fanout,
        )
    owner_count: int = _backend4_store_buffer_owner_count()
    owner_count_high_water: int = load_i32(
        global_addr("pcc_gc_backend4_store_buffer_owner_count_high_water_count"), 0
    )
    if owner_count > owner_count_high_water:
        store_i32(
            global_addr("pcc_gc_backend4_store_buffer_owner_count_high_water_count"),
            0,
            owner_count,
        )
    return 1


def _backend4_store_buffer_remove(owner) -> None:
    if ptr_is_null(owner) != 0 or is_tagged_int(owner) != 0:
        return
    prev = null()
    node = _store_buffer_medium_head()
    while ptr_is_null(node) == 0:
        nxt = load_ptr(node, 24)
        if ptr_eq(load_ptr(node, 0), owner) != 0:
            if ptr_is_null(prev) != 0:
                _set_store_buffer_medium_head(nxt)
            else:
                store_ptr(prev, 24, nxt)
            _backend4_store_buffer_dec()
            count: int = _backend4_store_buffer_medium_count()
            if count > 0:
                _backend4_store_buffer_medium_set_count(count - 1)
            py_decref(load_ptr(node, 16))
            free(node)
            node = nxt
            continue
        prev = node
        node = nxt
    prev = null()
    node = _store_buffer_head()
    while ptr_is_null(node) == 0:
        nxt = load_ptr(node, 24)
        if ptr_eq(load_ptr(node, 0), owner) != 0:
            if ptr_is_null(prev) != 0:
                _set_store_buffer_head(nxt)
            else:
                store_ptr(prev, 24, nxt)
            _backend4_store_buffer_dec()
            py_decref(load_ptr(node, 16))
            free(node)
            node = nxt
            continue
        prev = node
        node = nxt


@c_abi_export("pcc_gc_backend4_source_side_table_plan_prepare")
def pcc_gc_backend4_source_side_table_plan_prepare(owner):
    """Prepare stable buffered-value records without mutating side tables."""
    if ptr_is_null(owner) != 0 or is_tagged_int(owner) != 0:
        return null()
    count: int = 0
    node = _store_buffer_medium_head()
    while ptr_is_null(node) == 0:
        if ptr_eq(load_ptr(node, 0), owner) != 0:
            if count >= 1152921504606846975:
                return null()
            count = count + 1
        node = load_ptr(node, 24)
    node = _store_buffer_head()
    while ptr_is_null(node) == 0:
        if ptr_eq(load_ptr(node, 0), owner) != 0:
            if count >= 1152921504606846975:
                return null()
            count = count + 1
        node = load_ptr(node, 24)
    plan = malloc(32)
    if ptr_is_null(plan) != 0:
        return null()
    memset(plan, 0, 32)
    values = null()
    if count > 0:
        values = malloc(count * 8)
        if ptr_is_null(values) != 0:
            free(plan)
            return null()
        memset(values, 0, count * 8)
    index: int = 0
    node = _store_buffer_medium_head()
    while ptr_is_null(node) == 0:
        if ptr_eq(load_ptr(node, 0), owner) != 0:
            if index >= count:
                free(values)
                free(plan)
                return null()
            store_ptr(values, index * 8, load_ptr(node, 16))
            index = index + 1
        node = load_ptr(node, 24)
    node = _store_buffer_head()
    while ptr_is_null(node) == 0:
        if ptr_eq(load_ptr(node, 0), owner) != 0:
            if index >= count:
                free(values)
                free(plan)
                return null()
            store_ptr(values, index * 8, load_ptr(node, 16))
            index = index + 1
        node = load_ptr(node, 24)
    if index != count:
        free(values)
        free(plan)
        return null()
    store_ptr(plan, 0, owner)
    store_ptr(plan, 8, values)
    store_i64(plan, 16, count)
    store_i32(plan, 24, 0)
    return plan


@c_abi_export("pcc_gc_backend4_source_side_table_plan_commit")
def pcc_gc_backend4_source_side_table_plan_commit(plan) -> int:
    """Detach owner metadata with no allocation and no reference release."""
    if ptr_is_null(plan) != 0 or load_i32(plan, 24) != 0:
        return 0
    owner = load_ptr(plan, 0)
    values = load_ptr(plan, 8)
    count: int = load_i64(plan, 16)
    if ptr_is_null(owner) != 0 or is_tagged_int(owner) != 0 or count < 0:
        return 0
    if count > 0 and ptr_is_null(values) != 0:
        return 0

    # Re-verify the complete stable snapshot before the first side-table
    # mutation.  The caller-held graph lock makes an exact mismatch fatal.
    index: int = 0
    node = _store_buffer_medium_head()
    while ptr_is_null(node) == 0:
        if ptr_eq(load_ptr(node, 0), owner) != 0:
            if index >= count:
                return 0
            if ptr_eq(load_ptr(values, index * 8), load_ptr(node, 16)) == 0:
                return 0
            index = index + 1
        node = load_ptr(node, 24)
    node = _store_buffer_head()
    while ptr_is_null(node) == 0:
        if ptr_eq(load_ptr(node, 0), owner) != 0:
            if index >= count:
                return 0
            if ptr_eq(load_ptr(values, index * 8), load_ptr(node, 16)) == 0:
                return 0
            index = index + 1
        node = load_ptr(node, 24)
    if index != count:
        return 0

    # All references live in plan-owned stable storage now.  Remove every
    # owner entry from both visible queues before any later decref can reenter.
    removed: int = 0
    prev = null()
    node = _store_buffer_medium_head()
    while ptr_is_null(node) == 0:
        nxt = load_ptr(node, 24)
        if ptr_eq(load_ptr(node, 0), owner) != 0:
            if ptr_is_null(prev) != 0:
                _set_store_buffer_medium_head(nxt)
            else:
                store_ptr(prev, 24, nxt)
            _backend4_store_buffer_dec()
            medium_count: int = _backend4_store_buffer_medium_count()
            if medium_count > 0:
                _backend4_store_buffer_medium_set_count(medium_count - 1)
            free(node)
            removed = removed + 1
            node = nxt
            continue
        prev = node
        node = nxt
    prev = null()
    node = _store_buffer_head()
    while ptr_is_null(node) == 0:
        nxt = load_ptr(node, 24)
        if ptr_eq(load_ptr(node, 0), owner) != 0:
            if ptr_is_null(prev) != 0:
                _set_store_buffer_head(nxt)
            else:
                store_ptr(prev, 24, nxt)
            _backend4_store_buffer_dec()
            free(node)
            removed = removed + 1
            node = nxt
            continue
        prev = node
        node = nxt
    if removed != count:
        pcc_py_gc_defer_tripwire(
            cstr("source side-table commit detached count mismatch"),
            cstr("pcc/py_runtime/py/py_gc_backend.py"),
            1417,
        )
        return 0
    _backend4_remembered_set_remove(owner)
    _backend4_zpage_remove(owner)
    store_i32(plan, 24, 1)
    return 1


@c_abi_export("pcc_gc_backend4_source_side_table_plan_finish")
def pcc_gc_backend4_source_side_table_plan_finish(
    plan, decref_exclusion
) -> None:
    """Release detached store-buffer references after raw payload teardown."""
    if ptr_is_null(plan) != 0 or load_i32(plan, 24) != 1:
        return
    values = load_ptr(plan, 8)
    count: int = load_i64(plan, 16)
    store_ptr(plan, 0, null())
    store_ptr(plan, 8, null())
    store_i64(plan, 16, 0)
    store_i32(plan, 24, 2)
    index: int = 0
    while index < count:
        value = load_ptr(values, index * 8)
        if (
            ptr_is_null(decref_exclusion) != 0
            or ptr_eq(value, decref_exclusion) == 0
        ):
            py_decref(value)
        index = index + 1
    free(values)
    free(plan)


def _backend4_store_buffer_owner_pending(owner) -> int:
    if ptr_is_null(owner) != 0 or is_tagged_int(owner) != 0:
        return 0
    node = _store_buffer_medium_head()
    while ptr_is_null(node) == 0:
        if ptr_eq(load_ptr(node, 0), owner) != 0:
            return 1
        node = load_ptr(node, 24)
    node = _store_buffer_head()
    while ptr_is_null(node) == 0:
        if ptr_eq(load_ptr(node, 0), owner) != 0:
            return 1
        node = load_ptr(node, 24)
    return 0


def _backend4_store_buffer_owner_fanout(owner) -> int:
    if ptr_is_null(owner) != 0 or is_tagged_int(owner) != 0:
        return 0
    node = _store_buffer_medium_head()
    count: int = 0
    while ptr_is_null(node) == 0:
        if ptr_eq(load_ptr(node, 0), owner) != 0:
            count = count + 1
        node = load_ptr(node, 24)
    node = _store_buffer_head()
    while ptr_is_null(node) == 0:
        if ptr_eq(load_ptr(node, 0), owner) != 0:
            count = count + 1
        node = load_ptr(node, 24)
    return count


def _backend4_store_buffer_owner_count() -> int:
    head = _store_buffer_medium_head()
    node = head
    count: int = 0
    while ptr_is_null(node) == 0:
        owner = load_ptr(node, 0)
        prev = head
        seen: int = 0
        while ptr_is_null(prev) == 0:
            if ptr_eq(prev, node) != 0:
                break
            if ptr_eq(load_ptr(prev, 0), owner) != 0:
                seen = 1
                break
            prev = load_ptr(prev, 24)
        if seen == 0:
            count = count + 1
        node = load_ptr(node, 24)
    global_head = _store_buffer_head()
    node = global_head
    while ptr_is_null(node) == 0:
        owner = load_ptr(node, 0)
        seen = 0
        prev = head
        while ptr_is_null(prev) == 0:
            if ptr_eq(load_ptr(prev, 0), owner) != 0:
                seen = 1
                break
            prev = load_ptr(prev, 24)
        prev = global_head
        while ptr_is_null(prev) == 0:
            if ptr_eq(prev, node) != 0:
                break
            if ptr_eq(load_ptr(prev, 0), owner) != 0:
                seen = 1
                break
            prev = load_ptr(prev, 24)
        if seen == 0:
            count = count + 1
        node = load_ptr(node, 24)
    return count


def _backend4_store_buffer_entry_count() -> int:
    node = _store_buffer_medium_head()
    count: int = 0
    while ptr_is_null(node) == 0:
        count = count + 1
        node = load_ptr(node, 24)
    node = _store_buffer_head()
    while ptr_is_null(node) == 0:
        count = count + 1
        node = load_ptr(node, 24)
    return count


def _backend4_store_buffer_max_owner_fanout() -> int:
    node = _store_buffer_medium_head()
    max_fanout: int = 0
    while ptr_is_null(node) == 0:
        fanout: int = _backend4_store_buffer_owner_fanout(load_ptr(node, 0))
        if fanout > max_fanout:
            max_fanout = fanout
        node = load_ptr(node, 24)
    node = _store_buffer_head()
    while ptr_is_null(node) == 0:
        fanout: int = _backend4_store_buffer_owner_fanout(load_ptr(node, 0))
        if fanout > max_fanout:
            max_fanout = fanout
        node = load_ptr(node, 24)
    return max_fanout


def _backend4_reset_store_buffer_epoch_state() -> None:
    _object_graph_lock()
    entries: int = _backend4_store_buffer_entry_count()
    owner_fanout: int = _backend4_store_buffer_max_owner_fanout()
    owner_count: int = _backend4_store_buffer_owner_count()
    store_i32(global_addr("pcc_gc_backend4_store_buffer_entries_count"), 0, entries)
    store_i32(global_addr("pcc_gc_backend4_store_buffer_high_water_count"), 0, entries)
    store_i32(
        global_addr("pcc_gc_backend4_store_buffer_owner_fanout_high_water_count"),
        0,
        owner_fanout,
    )
    store_i32(
        global_addr("pcc_gc_backend4_store_buffer_owner_count_high_water_count"),
        0,
        owner_count,
    )
    _object_graph_unlock()


def _backend4_store_buffer_clear() -> None:
    node = _store_buffer_medium_head()
    _set_store_buffer_medium_head(null())
    while ptr_is_null(node) == 0:
        nxt = load_ptr(node, 24)
        py_decref(load_ptr(node, 16))
        free(node)
        node = nxt
    _backend4_store_buffer_medium_set_count(0)
    node = _store_buffer_head()
    _set_store_buffer_head(null())
    while ptr_is_null(node) == 0:
        nxt = load_ptr(node, 24)
        owner = load_ptr(node, 0)
        if _is_known_object(owner) != 0:
            flags: int = load_i32(owner, 12)
            store_i32(owner, 12, flags & ~512)
        py_decref(load_ptr(node, 16))
        free(node)
        node = nxt
    store_i32(global_addr("pcc_gc_backend4_store_buffer_entries_count"), 0, 0)
    store_i32(global_addr("pcc_gc_backend4_store_buffer_high_water_count"), 0, 0)
    store_i32(
        global_addr("pcc_gc_backend4_store_buffer_owner_fanout_high_water_count"), 0, 0
    )
    store_i32(
        global_addr("pcc_gc_backend4_store_buffer_owner_count_high_water_count"), 0, 0
    )
    _backend4_remembered_set_clear()


def _backend4_store_buffer_batch_capacity() -> int:
    return 8


def _backend4_store_buffer_note_max_batch(batch_size: int) -> None:
    max_batch: int = load_i32(
        global_addr("pcc_gc_backend4_store_buffer_max_batch_size_count"), 0
    )
    if batch_size > max_batch:
        store_i32(
            global_addr("pcc_gc_backend4_store_buffer_max_batch_size_count"),
            0,
            batch_size,
        )


def _remembered_set_head():
    return global_load_ptr("pcc_gc_backend4_remembered_slots_head")


def _set_remembered_set_head(head) -> None:
    global_store_ptr("pcc_gc_backend4_remembered_slots_head", head)


def _backend4_remembered_set_contains(owner, slot) -> int:
    if ptr_is_null(owner) != 0 or is_tagged_int(owner) != 0:
        return 0
    if ptr_is_null(slot) != 0:
        return 0
    node = _remembered_set_head()
    while ptr_is_null(node) == 0:
        if ptr_eq(load_ptr(node, 0), owner) != 0:
            if ptr_eq(load_ptr(node, 8), slot) != 0:
                return 1
        node = load_ptr(node, 16)
    return 0


@c_abi_export("pcc_gc_backend4_owner_remembered_slots")
def _backend4_owner_remembered_slots(owner) -> int:
    if ptr_is_null(owner) != 0 or is_tagged_int(owner) != 0:
        return 0
    node = _remembered_set_head()
    total: int = 0
    while ptr_is_null(node) == 0:
        if ptr_eq(load_ptr(node, 0), owner) != 0:
            total = total + 1
        node = load_ptr(node, 16)
    return total


def _backend4_zpage_note_remembered_slot(owner, delta: int) -> None:
    if ptr_is_null(owner) != 0 or is_tagged_int(owner) != 0:
        return
    if delta == 0:
        return
    node = _backend4_zpage_find(owner)
    if ptr_is_null(node) != 0:
        return
    page = load_ptr(node, 8)
    if ptr_is_null(page) != 0:
        return
    current: int = load_i64(page, 40)
    current = current + delta
    if current < 0:
        current = 0
    store_i64(page, 40, current)
    owner_current: int = load_i64(node, 72) + delta
    if owner_current < 0:
        owner_current = 0
    store_i64(node, 72, owner_current)


def _backend4_zpage_note_remembered_card(owner, delta: int) -> None:
    if ptr_is_null(owner) != 0 or is_tagged_int(owner) != 0:
        return
    if delta == 0:
        return
    node = _backend4_zpage_find(owner)
    if ptr_is_null(node) != 0:
        return
    page = load_ptr(node, 8)
    if ptr_is_null(page) != 0:
        return
    current: int = load_i64(page, 48)
    current = current + delta
    if current < 0:
        current = 0
    store_i64(page, 48, current)


def _backend4_remembered_set_add(owner, slot) -> int:
    if ptr_is_null(owner) != 0 or is_tagged_int(owner) != 0:
        return 0
    if ptr_is_null(slot) != 0:
        return 0
    if _backend4_remembered_set_contains(owner, slot) != 0:
        skips: int = load_i32(
            global_addr("pcc_gc_backend4_remembered_set_duplicate_skips_count"), 0
        )
        store_i32(
            global_addr("pcc_gc_backend4_remembered_set_duplicate_skips_count"),
            0,
            skips + 1,
        )
        return 0
    node = malloc(24)
    if ptr_is_null(node) != 0:
        return 0
    store_ptr(node, 0, owner)
    store_ptr(node, 8, slot)
    store_ptr(node, 16, _remembered_set_head())
    _set_remembered_set_head(node)
    entries: int = load_i32(
        global_addr("pcc_gc_backend4_remembered_set_entries_count"), 0
    )
    entries = entries + 1
    store_i32(
        global_addr("pcc_gc_backend4_remembered_set_entries_count"),
        0,
        entries,
    )
    high_water: int = load_i32(
        global_addr("pcc_gc_backend4_remembered_set_high_water_count"), 0
    )
    if entries > high_water:
        store_i32(
            global_addr("pcc_gc_backend4_remembered_set_high_water_count"),
            0,
            entries,
        )
    _backend4_zpage_note_remembered_slot(owner, 1)
    _backend4_zpage_note_remembered_card(owner, 1)
    return 1


def _backend4_remembered_set_remove(owner) -> None:
    if ptr_is_null(owner) != 0 or is_tagged_int(owner) != 0:
        return
    prev = null()
    node = _remembered_set_head()
    while ptr_is_null(node) == 0:
        nxt = load_ptr(node, 16)
        if ptr_eq(load_ptr(node, 0), owner) != 0:
            _backend4_zpage_note_remembered_slot(load_ptr(node, 0), -1)
            _backend4_zpage_note_remembered_card(load_ptr(node, 0), -1)
            if ptr_is_null(prev) != 0:
                _set_remembered_set_head(nxt)
            else:
                store_ptr(prev, 16, nxt)
            entries: int = load_i32(
                global_addr("pcc_gc_backend4_remembered_set_entries_count"), 0
            )
            if entries > 0:
                store_i32(
                    global_addr("pcc_gc_backend4_remembered_set_entries_count"),
                    0,
                    entries - 1,
                )
            free(node)
            node = nxt
            continue
        prev = node
        node = nxt


def _backend4_remembered_set_remove_slot(slot) -> int:
    if ptr_is_null(slot) != 0:
        return 0
    prev = null()
    node = _remembered_set_head()
    removed: int = 0
    while ptr_is_null(node) == 0:
        nxt = load_ptr(node, 16)
        if ptr_eq(load_ptr(node, 8), slot) != 0:
            _backend4_zpage_note_remembered_slot(load_ptr(node, 0), -1)
            _backend4_zpage_note_remembered_card(load_ptr(node, 0), -1)
            if ptr_is_null(prev) != 0:
                _set_remembered_set_head(nxt)
            else:
                store_ptr(prev, 16, nxt)
            entries: int = load_i32(
                global_addr("pcc_gc_backend4_remembered_set_entries_count"), 0
            )
            if entries > 0:
                store_i32(
                    global_addr("pcc_gc_backend4_remembered_set_entries_count"),
                    0,
                    entries - 1,
                )
            free(node)
            removed = 1
            node = nxt
            continue
        prev = node
        node = nxt
    return removed


@c_abi_export("pcc_gc_backend4_remembered_set_retarget_slot")
def _backend4_remembered_set_retarget_slot(
    from_owner, to_owner, from_slot, to_slot
) -> None:
    if ptr_is_null(from_owner) != 0 or ptr_is_null(to_owner) != 0:
        return
    if is_tagged_int(from_owner) != 0 or is_tagged_int(to_owner) != 0:
        return
    if ptr_is_null(from_slot) != 0 or ptr_is_null(to_slot) != 0:
        return
    if ptr_eq(from_owner, to_owner) != 0 and ptr_eq(from_slot, to_slot) != 0:
        return
    node = _remembered_set_head()
    while ptr_is_null(node) == 0:
        if ptr_eq(load_ptr(node, 0), from_owner) != 0:
            if ptr_eq(load_ptr(node, 8), from_slot) != 0:
                _backend4_zpage_note_remembered_slot(load_ptr(node, 0), -1)
                _backend4_zpage_note_remembered_card(load_ptr(node, 0), -1)
                store_ptr(node, 0, to_owner)
                store_ptr(node, 8, to_slot)
                _backend4_zpage_note_remembered_slot(to_owner, 1)
                _backend4_zpage_note_remembered_card(to_owner, 1)
        node = load_ptr(node, 16)


def _backend4_remembered_set_retarget_inline_slot(
    from_owner, to_owner, offset: int
) -> None:
    _backend4_remembered_set_retarget_slot(
        from_owner,
        to_owner,
        ptr_add(from_owner, offset),
        ptr_add(to_owner, offset),
    )


def _backend4_remembered_set_entry_count() -> int:
    node = _remembered_set_head()
    count: int = 0
    while ptr_is_null(node) == 0:
        count = count + 1
        node = load_ptr(node, 16)
    return count


def _backend4_reset_remembered_set_epoch_state() -> None:
    _object_graph_lock()
    entries: int = _backend4_remembered_set_entry_count()
    store_i32(global_addr("pcc_gc_backend4_remembered_set_entries_count"), 0, entries)
    store_i32(
        global_addr("pcc_gc_backend4_remembered_set_high_water_count"), 0, entries
    )
    _object_graph_unlock()


def _backend4_remembered_set_clear() -> None:
    node = _remembered_set_head()
    _set_remembered_set_head(null())
    while ptr_is_null(node) == 0:
        nxt = load_ptr(node, 16)
        _backend4_zpage_note_remembered_slot(load_ptr(node, 0), -1)
        _backend4_zpage_note_remembered_card(load_ptr(node, 0), -1)
        free(node)
        node = nxt
    store_i32(global_addr("pcc_gc_backend4_remembered_set_entries_count"), 0, 0)
    store_i32(global_addr("pcc_gc_backend4_remembered_set_high_water_count"), 0, 0)


def _is_known_object(o) -> int:
    return _gc_object_is_known_no_lock(o)


@c_abi_export("pcc_gc_granule_s2_candidate_positive")
def _granule_s2_candidate_positive(o) -> int:
    """Expose the same fail-closed exact-positive predicate used by S2."""
    if ptr_is_null(o) != 0 or is_tagged_int(o) != 0:
        return 0
    if pcc_gc_granule_is_object_start(o) == 1:
        return 1
    return 0


def _pointer_is_managed_no_lock(o) -> int:
    if ptr_is_null(o) != 0 or is_tagged_int(o) != 0:
        return 0
    if ptr_eq(o, global_load_ptr("py_None")) != 0:
        return 1
    if ptr_eq(o, global_load_ptr("py_NotImplemented")) != 0:
        return 1
    if ptr_eq(o, global_load_ptr("py_True")) != 0:
        return 1
    if ptr_eq(o, global_load_ptr("py_False")) != 0:
        return 1
    # A granule hit is an exact positive only for a fully initialized LIVE
    # object-family slot.  Unknown, reserved, free, raw, large, foreign and
    # moving-arena/zpage addresses all continue through the exact/index/type/
    # forwarding chain below.
    if pcc_gc_granule_is_object_start(o) == 1:
        return 1
    # Ordering matters and is not arbitrary.  This is a disjunction of
    # side-effect-free lookups, so any order returns the same answer, but the
    # costs differ by an order of magnitude: the managed-pointer index is one
    # hash probe and is the case that actually hits, while
    # `pcc_capi_is_type_object_value` walks a linear list of every registered
    # builtin type object and almost always fails.  With the scan first, every
    # GC barrier in the program paid that walk before reaching the answer, and
    # it was the second hottest leaf in a self-hosted pcc1 compile.
    if pcc_gc_managed_pointer_index_contains(o) != 0:
        return 1
    # These indexes compare pointer values only.  Unknown raw C pointers are
    # never dereferenced while provenance is being decided.
    if ptr_is_null(pcc_gc_object_index_find(o)) == 0:
        return 1
    if pcc_capi_is_type_object_value(o) != 0:
        return 1
    if ptr_is_null(_forwarding_find(o)) == 0:
        return 1
    if _forwarding_target_exists(o) != 0:
        return 1
    return 0


@c_abi_export("pcc_gc_pointer_is_managed")
def pcc_gc_pointer_is_managed(o) -> int:
    # Answer the value-only cases before taking the object-graph lock.
    # `_pointer_is_managed_no_lock` returns 0 for exactly these inputs as its
    # first act, and both tests are pure bit checks on the value itself — they
    # read no shared state, so hoisting them cannot change the answer.
    #
    # This is the hot path: `_ptr_is_class`/`_ptr_is_instance` run this query
    # on every attribute access and method dispatch, and a tagged small int is
    # a very common argument there.  Those calls used to pay a full lock /
    # unlock pair plus the managed-pointer hash probe
    # (`pcc_gc_managed_pointer_find_slot` was the #1 leaf, 705 of ~10000
    # samples, when pcc1 compiles a real module) to learn something decidable
    # from the pointer bits alone.
    if ptr_is_null(o) != 0 or is_tagged_int(o) != 0:
        return 0
    # Hoisting the four immortal-singleton compares ahead of the granule probe
    # was measured and DENIED on 2026-09-06: on the cli_bootstrap ASM worker it
    # added 3.2% instructions (four extra compares on every probe) and saved
    # less, so singleton refcount traffic is too rare to pay for it here.
    # The allocator publishes LIVE with release ordering only after the object
    # header is complete.  Readers may therefore accept this exact positive
    # without the graph lock.  Every other result takes the lock and executes
    # the complete historical provenance chain.
    if pcc_gc_granule_is_object_start(o) == 1:
        return 1
    _object_graph_lock()
    managed: int = _pointer_is_managed_no_lock(o)
    _object_graph_unlock()
    return managed


@c_abi_export("pcc_gc_pointer_register")
def pcc_gc_pointer_register(o) -> int:
    if ptr_is_null(o) != 0 or is_tagged_int(o) != 0:
        return -1
    granule_result: int = pcc_gc_granule_object_publish(o)
    if granule_result < 0:
        return -1
    if granule_result > 0:
        return 0
    _object_graph_lock()
    result: int = pcc_gc_managed_pointer_index_insert(o)
    _object_graph_unlock()
    return result


@c_abi_export("pcc_gc_pointer_unregister")
def pcc_gc_pointer_unregister(o) -> int:
    if ptr_is_null(o) != 0 or is_tagged_int(o) != 0:
        return 0
    granule_was_live: int = pcc_gc_granule_is_object_start(o)
    granule_result: int = pcc_gc_granule_object_retire(o)
    if granule_result < 0:
        return -1
    if granule_result > 0:
        # Constructor/error cleanup may retire a RESERVED/FREE object-family
        # slot after note_object_freeing conservatively inserted an exact key.
        # Do not let that key survive when the address returns to the freelist.
        if granule_was_live != 1:
            _object_graph_lock()
            pcc_gc_managed_pointer_index_remove(o)
            _object_graph_unlock()
        return 0
    _object_graph_lock()
    result: int = pcc_gc_managed_pointer_index_remove(o)
    _object_graph_unlock()
    return result


@c_abi_export("pcc_gc_object_is_known")
def pcc_gc_object_is_known(o) -> int:
    _object_graph_lock()
    known: int = _is_known_object(o)
    _object_graph_unlock()
    return known


def _mark_gray_if_known(o) -> None:
    if ptr_is_null(o) != 0:
        return
    if is_tagged_int(o) != 0:
        return
    forwarding = _forwarding_find(o)
    if ptr_is_null(forwarding) == 0:
        resolved = load_ptr(forwarding, 8)
        if ptr_is_null(resolved) == 0:
            if ptr_eq(resolved, o) == 0:
                o = resolved
    if _is_known_object(o) == 0:
        return
    flags: int = load_i32(o, 12)
    if (flags & 32) == 0:
        if (flags & 16) == 0:
            _inc_gray_count()
        store_i32(o, 12, (flags & ~56) | 16)


def _promote_young_slot(slot_base, slot_offset: int) -> None:
    _promote_young_slot_mode(slot_base, slot_offset, 1)


def _promote_young_borrowed_slot(slot_base, slot_offset: int) -> None:
    _promote_young_borrowed_slot_mode(slot_base, slot_offset, 1)


def _gray_exists() -> int:
    node = _object_head()
    while ptr_is_null(node) == 0:
        nxt = load_ptr(node, 16)
        if load_i64(node, 32) != 0:
            node = nxt
            continue
        o = load_ptr(node, 0)
        if ptr_is_null(o) != 0 or is_tagged_int(o) != 0:
            node = nxt
            continue
        flags: int = load_i32(o, 12)
        if (flags & 16) != 0:
            return 1
        node = nxt
    return 0


def _py_obj_visit_slot(
    slot_base,
    slot_offset: int,
    role: int,
    mode: int,
    recurse: int,
) -> None:
    if mode == 3:  # _PY_OBJ_VISIT_UPDATE
        _remap_heal_slot(slot_base, slot_offset)
        return
    if mode == 4:  # _PY_OBJ_VISIT_SUBTRACT
        if role != 3:  # _PY_OBJ_SLOT_BORROWED_UPDATE_ONLY
            child = pcc_gc_load_ptr_extern(
                null(),
                ptr_add(slot_base, slot_offset),
            )
            _subtract_known_child_ref(child)
        return


def _py_obj_visit_update_slot(slot, role: int, ctx) -> None:
    _py_obj_visit_slot(slot, 0, role, 3, 0)


def _py_obj_visit_subtract_slot(slot, role: int, ctx) -> None:
    _py_obj_visit_slot(slot, 0, role, 4, 0)


def _py_obj_visit_covered_slots(o, mode: int, recurse: int) -> int:
    if mode == 3:
        return pcc_gc_visit_object_slots(o, _py_obj_visit_update_slot, null())
    if mode == 4:
        return pcc_gc_visit_object_slots(o, _py_obj_visit_subtract_slot, null())
    return 0


def _subtract_known_child_ref(child) -> None:
    if ptr_is_null(child) != 0:
        return
    if is_tagged_int(child) != 0:
        return
    forwarding = _forwarding_find(child)
    if ptr_is_null(forwarding) == 0:
        resolved = load_ptr(forwarding, 8)
        if ptr_is_null(resolved) == 0:
            child = resolved
    node = pcc_gc_object_index_find(child)
    if ptr_is_null(node) != 0:
        return
    if _object_node_is_active(node) == 0:
        return
    _set_object_node_gc_refs(node, _object_node_gc_refs(node) - 1)


@c_abi_export("pcc_gc_subtract_referent_refs")
def _subtract_referent_refs(o) -> None:
    if ptr_is_null(o) != 0 or is_tagged_int(o) != 0:
        return
    if _py_obj_visit_covered_slots(o, 4, 0) != 0:  # _PY_OBJ_VISIT_SUBTRACT
        return


@c_abi_export("pcc_gc_backend")
def pcc_gc_backend() -> int:
    return _init_config()


@c_abi_export("pcc_gc_set_backend")
def pcc_gc_set_backend(backend: int) -> int:
    _init_config()
    if backend < 0 or backend > 4:
        return -1
    # Forwarding policy is collector-specific: GC3 oldification and GC4
    # two-epoch relocation share a node layout but not ownership semantics.
    # Never change collectors while either representation is active.  A
    # same-backend reset remains legal.
    _object_graph_lock()
    old_backend: int = load_i32(global_addr("pcc_gc_backend_selected"), 0)
    if load_i32(
        global_addr("pcc_gc_trace_extension_roots_pending"), 0
    ) == 4:
        _object_graph_unlock()
        return -1
    if load_i32(global_addr("pcc_gc_backend4_remap_active"), 0) != 0:
        _object_graph_unlock()
        return -1
    if (
        backend != old_backend
        and (
            ptr_is_null(_forwarding_head()) == 0
            or load_i32(global_addr("pcc_gc_forwarding_population"), 0) != 0
        )
    ):
        _object_graph_unlock()
        return -1
    if backend == 0:
        # Preserve provenance before backend 0 discards the object index.
        # Object-family LIVE slots need no duplicate exact key; every other
        # origin is inserted.  The selected-backend store occurs under the
        # same lock, so no tracked allocation can slip into the list after
        # this migration.
        migration_node = _object_head()
        while ptr_is_null(migration_node) == 0:
            migration_obj = load_ptr(migration_node, 0)
            if pcc_gc_granule_is_object_start(migration_obj) != 1:
                if pcc_gc_managed_pointer_index_insert(migration_obj) < 0:
                    _object_graph_unlock()
                    return -1
            migration_node = _object_node_next(migration_node)
    if _tracing_cycle_epoch_advance_unlocked() == 0:
        _object_graph_unlock()
        return -1
    store_i32(global_addr("pcc_gc_backend_selected"), 0, backend)
    store_i32(global_addr("pcc_gc_mark_active"), 0, 0)
    store_i32(global_addr("pcc_gc_cycle_requested"), 0, 1)
    store_i32(
        global_addr("pcc_gc_trace_extension_roots_pending"), 0, 0
    )
    store_i64(global_addr("pcc_gc_trace_extension_roots_epoch"), 0, 0)
    store_i64(global_addr("pcc_gc_trace_extension_roots_backend"), 0, -1)
    global_store_ptr("pcc_gc_trace_cursor", null())
    _set_gray_count(0)
    _object_graph_unlock()
    if backend == 3 or backend == 4:
        store_i32(global_addr("pcc_gc_read_barrier_enabled"), 0, 1)
    if backend == 0:
        store_i32(global_addr("pcc_gc_backend0_frame_roots_enabled"), 0, 1)
    if backend != 3 and backend != 4:
        store_i32(global_addr("pcc_gc_read_barrier_enabled"), 0, 0)
    store_i32(global_addr("pcc_gc_debt_bytes"), 0, 0)
    store_i32(global_addr("pcc_gc_last_alloc_bytes"), 0, 0)
    if backend == 0:
        global_store_ptr("pcc_gc_backend3_promotion_head", null())
        global_store_ptr("pcc_gc_backend3_promotion_tail", null())
        store_i64(
            global_addr("pcc_gc_backend3_promotion_revision"),
            0,
            load_i64(global_addr("pcc_gc_object_list_revision"), 0),
        )
        _clear_object_list()
        store_i32(global_addr("pcc_gc_live_bytes"), 0, 0)
    if _backend_uses_forwarding() == 0:
        _forwarding_clear_all()
        _identity_clear_all()
    if backend != 4:
        pcc_gc_reset_relocation_set()
        _backend4_store_buffer_clear()
    if old_backend == 2 and backend != 2:
        _stop_cms_worker()
    _maybe_start_cms_worker()
    return 0


@c_abi_export("pcc_gc_backend_name")
def pcc_gc_backend_name(backend: int):
    if backend == 0:
        return cstr("refcount-cycle")
    if backend == 1:
        return cstr("incremental-tricolor")
    if backend == 2:
        return cstr("concurrent-mark-sweep")
    if backend == 3:
        return cstr("generational-minor-major")
    if backend == 4:
        return cstr("colored-relocating")
    return cstr("unknown")


@c_abi_export("pcc_gc_telemetry_reset")
def pcc_gc_telemetry_reset() -> None:
    _init_config()
    _object_graph_lock()
    detached_remembered = _backend3_clear_remembered_owners()
    _object_graph_unlock()
    _backend3_finish_detached_remembered_owners(detached_remembered)
    i: int = 0
    while i <= 5:
        store_i32(_counter_global(i), 0, 0)
        i = i + 1
    store_i64(global_addr("pcc_gc_unmanaged_refcount_ops"), 0, 0)
    store_i32(global_addr("pcc_gc_metric_max_pause_us"), 0, 0)
    store_i32(global_addr("pcc_gc_metric_pause_count"), 0, 0)
    store_i32(global_addr("pcc_gc_metric_pause_sum_us"), 0, 0)
    store_i32(global_addr("pcc_gc_metric_pause_hist0"), 0, 0)
    store_i32(global_addr("pcc_gc_metric_pause_hist1"), 0, 0)
    store_i32(global_addr("pcc_gc_metric_pause_hist2"), 0, 0)
    store_i32(global_addr("pcc_gc_metric_pause_hist3"), 0, 0)
    _atomic_i32_store(global_addr("pcc_gc_minor_allocations"), 0)
    _atomic_i32_store(global_addr("pcc_gc_minor_collections"), 0)
    _atomic_i32_store(global_addr("pcc_gc_minor_bytes"), 0)
    store_i32(global_addr("pcc_gc_cms_queue_pushes"), 0, 0)
    store_i32(global_addr("pcc_gc_cms_worker_drains"), 0, 0)
    store_i32(global_addr("pcc_gc_cms_mutator_assists"), 0, 0)
    store_i32(global_addr("pcc_gc_cms_worker_traces"), 0, 0)
    _atomic_i32_store(global_addr("pcc_gc_minor_arena_refills"), 0)
    _atomic_i32_store(global_addr("pcc_gc_minor_arena_bumps"), 0)
    _atomic_i32_store(global_addr("pcc_gc_minor_arena_fallbacks"), 0)
    store_i32(global_addr("pcc_gc_cms_worker_stops"), 0, 0)
    store_i32(global_addr("pcc_gc_cms_wb_flushes"), 0, 0)
    store_i32(global_addr("pcc_gc_relocation_forwards"), 0, 0)
    store_i32(global_addr("pcc_gc_relocation_barrier_forwards"), 0, 0)
    store_i32(global_addr("pcc_gc_relocation_pin_rejects"), 0, 0)
    store_i32(global_addr("pcc_gc_backend4_genzgc_store_barriers"), 0, 0)
    _backend4_reset_store_buffer_epoch_state()
    store_i32(global_addr("pcc_gc_backend4_young_promotions"), 0, 0)
    store_i32(global_addr("pcc_gc_backend4_evacuation_candidates"), 0, 0)
    store_i32(global_addr("pcc_gc_backend4_evacuated_bytes_count"), 0, 0)
    store_i32(global_addr("pcc_gc_backend4_large_object_defers"), 0, 0)
    store_i64(
        global_addr("pcc_gc_backend4_candidate_fresh_skips_g"), 0, 0
    )
    store_i64(
        global_addr("pcc_gc_backend4_relocation_add_refusals_g"), 0, 0
    )
    store_i32(global_addr("pcc_gc_backend4_large_object_deferred_bytes_count"), 0, 0)
    store_i32(global_addr("pcc_gc_backend4_large_object_reconsiderations_count"), 0, 0)
    store_i32(global_addr("pcc_gc_backend4_small_page_candidates"), 0, 0)
    store_i32(global_addr("pcc_gc_backend4_medium_page_candidates"), 0, 0)
    store_i32(global_addr("pcc_gc_backend4_evacuation_candidate_bytes_count"), 0, 0)
    store_i32(global_addr("pcc_gc_backend4_small_page_candidate_bytes_count"), 0, 0)
    store_i32(global_addr("pcc_gc_backend4_medium_page_candidate_bytes_count"), 0, 0)
    store_i32(
        global_addr("pcc_gc_backend4_evacuation_candidate_zpage_bytes_count"), 0, 0
    )
    store_i32(
        global_addr("pcc_gc_backend4_small_page_candidate_zpage_bytes_count"), 0, 0
    )
    store_i32(
        global_addr("pcc_gc_backend4_medium_page_candidate_zpage_bytes_count"), 0, 0
    )
    store_i32(global_addr("pcc_gc_backend4_store_buffer_drain_batches_count"), 0, 0)
    store_i32(global_addr("pcc_gc_backend4_store_buffer_drained_entries_count"), 0, 0)
    store_i32(global_addr("pcc_gc_backend4_store_buffer_duplicate_skips_count"), 0, 0)
    store_i32(global_addr("pcc_gc_backend4_store_buffer_incomplete_drains_count"), 0, 0)
    store_i32(global_addr("pcc_gc_backend4_evacuation_incomplete_batches_count"), 0, 0)
    store_i32(global_addr("pcc_gc_backend4_store_buffer_max_batch_size_count"), 0, 0)
    store_i32(global_addr("pcc_gc_backend4_store_buffer_full_batches_count"), 0, 0)
    _backend4_reset_remembered_set_epoch_state()
    store_i32(global_addr("pcc_gc_backend4_remembered_set_duplicate_skips_count"), 0, 0)
    store_i32(global_addr("pcc_gc_backend4_store_buffer_medium_flushes_count"), 0, 0)
    store_i32(
        global_addr("pcc_gc_backend4_store_buffer_medium_flushed_entries_count"), 0, 0
    )
    store_i32(
        global_addr("pcc_gc_backend4_store_buffer_medium_full_flushes_count"), 0, 0
    )
    _backend4_reseed_relocation_epoch_state()
    _backend4_clear_large_deferred_flags()


@c_abi_export("pcc_gc_backend4_fragmentation_score")
def pcc_gc_backend4_fragmentation_score() -> int:
    return pcc_gc_relocation_set_size() + pcc_gc_backend4_forwarding_entries()


@c_abi_export("pcc_gc_backend4_generation_barrier_score")
def pcc_gc_backend4_generation_barrier_score() -> int:
    return load_i32(global_addr("pcc_gc_backend4_genzgc_store_barriers"), 0)


@c_abi_export("pcc_gc_backend4_store_buffer_entries")
def pcc_gc_backend4_store_buffer_entries() -> int:
    return load_i32(global_addr("pcc_gc_backend4_store_buffer_entries_count"), 0)


@c_abi_export("pcc_gc_backend4_generation_promotion_score")
def pcc_gc_backend4_generation_promotion_score() -> int:
    return load_i32(global_addr("pcc_gc_backend4_young_promotions"), 0)


@c_abi_export("pcc_gc_backend4_evacuation_candidate_score")
def pcc_gc_backend4_evacuation_candidate_score() -> int:
    return load_i32(global_addr("pcc_gc_backend4_evacuation_candidates"), 0)


@c_abi_export("pcc_gc_backend4_evacuated_bytes")
def pcc_gc_backend4_evacuated_bytes() -> int:
    return load_i32(global_addr("pcc_gc_backend4_evacuated_bytes_count"), 0)


@c_abi_export("pcc_gc_backend4_page_policy_score")
def pcc_gc_backend4_page_policy_score() -> int:
    return (
        pcc_gc_backend4_evacuation_candidate_score() + pcc_gc_backend4_evacuated_bytes()
    )


@c_abi_export("pcc_gc_backend4_page_pressure_score")
def pcc_gc_backend4_page_pressure_score() -> int:
    return (
        pcc_gc_backend4_evacuation_candidate_bytes()
        + pcc_gc_backend4_large_object_deferred_bytes()
    )


@c_abi_export("pcc_gc_backend4_fragmentation_backlog_bytes")
def pcc_gc_backend4_fragmentation_backlog_bytes() -> int:
    candidates: int = pcc_gc_backend4_evacuation_candidate_bytes()
    evacuated: int = pcc_gc_backend4_evacuated_bytes()
    deferred: int = pcc_gc_backend4_large_object_deferred_bytes()
    pending: int = 0
    if candidates > evacuated:
        pending = candidates - evacuated
    return pending + deferred


@c_abi_export("pcc_gc_backend4_evacuation_efficiency_per_mille")
def pcc_gc_backend4_evacuation_efficiency_per_mille() -> int:
    candidates: int = pcc_gc_backend4_evacuation_candidate_bytes()
    if candidates <= 0:
        return 1000
    evacuated: int = pcc_gc_backend4_evacuated_bytes()
    if evacuated <= 0:
        return 0
    if evacuated >= candidates:
        return 1000
    return (evacuated * 1000) // candidates


@c_abi_export("pcc_gc_backend4_fragmentation_policy_score")
def pcc_gc_backend4_fragmentation_policy_score() -> int:
    return (
        pcc_gc_backend4_fragmentation_backlog_bytes()
        + pcc_gc_backend4_evacuation_incomplete_batches()
    )


@c_abi_export("pcc_gc_backend4_small_page_limit_bytes")
def pcc_gc_backend4_small_page_limit_bytes() -> int:
    return 4096


@c_abi_export("pcc_gc_backend4_medium_page_limit_bytes")
def pcc_gc_backend4_medium_page_limit_bytes() -> int:
    return 65536


@c_abi_export("pcc_gc_backend4_large_defer_limit_bytes")
def pcc_gc_backend4_large_defer_limit_bytes() -> int:
    return 65536


@c_abi_export("pcc_gc_backend4_large_object_defer_score")
def pcc_gc_backend4_large_object_defer_score() -> int:
    return load_i32(global_addr("pcc_gc_backend4_large_object_defers"), 0)


@c_abi_export("pcc_gc_backend4_large_object_deferred_bytes")
def pcc_gc_backend4_large_object_deferred_bytes() -> int:
    return load_i32(global_addr("pcc_gc_backend4_large_object_deferred_bytes_count"), 0)


@c_abi_export("pcc_gc_backend4_large_object_reconsiderations")
def pcc_gc_backend4_large_object_reconsiderations() -> int:
    return load_i32(
        global_addr("pcc_gc_backend4_large_object_reconsiderations_count"), 0
    )


def _backend4_generation_count(flag: int) -> int:
    _object_graph_lock()
    node = _object_head()
    count: int = 0
    while ptr_is_null(node) == 0:
        if _object_node_is_active(node) != 0:
            obj = load_ptr(node, 0)
            if ptr_is_null(obj) == 0:
                if is_tagged_int(obj) == 0:
                    flags: int = load_i32(obj, 12)
                    if (flags & flag) != 0:
                        count = count + 1
        node = _object_node_next(node)
    _object_graph_unlock()
    return count


def _backend4_generation_bytes(flag: int) -> int:
    _object_graph_lock()
    node = _object_head()
    total: int = 0
    while ptr_is_null(node) == 0:
        if _object_node_is_active(node) != 0:
            obj = load_ptr(node, 0)
            if ptr_is_null(obj) == 0:
                if is_tagged_int(obj) == 0:
                    flags: int = load_i32(obj, 12)
                    if (flags & flag) != 0:
                        total = total + _object_node_size(node)
        node = _object_node_next(node)
    _object_graph_unlock()
    return total


@c_abi_export("pcc_gc_backend4_young_object_count")
def pcc_gc_backend4_young_object_count() -> int:
    return _backend4_generation_count(128)


@c_abi_export("pcc_gc_backend4_old_object_count")
def pcc_gc_backend4_old_object_count() -> int:
    return _backend4_generation_count(256)


@c_abi_export("pcc_gc_backend4_young_bytes")
def pcc_gc_backend4_young_bytes() -> int:
    return _backend4_generation_bytes(128)


@c_abi_export("pcc_gc_backend4_old_bytes")
def pcc_gc_backend4_old_bytes() -> int:
    return _backend4_generation_bytes(256)


def _backend4_page_class_for_size(size: int) -> int:
    if size <= 4096:
        return 0
    if size <= 65536:
        return 1
    return 2


def _backend4_align_alloc_size(size: int) -> int:
    if size <= 0:
        return 0
    return (size + 7) & -8


def _backend4_generation_for_flags(flags: int) -> int:
    if (flags & 256) != 0:
        return 2
    return 1


def _backend4_page_class_population(page_class: int, count_bytes: int) -> int:
    _object_graph_lock()
    node = _object_head()
    total: int = 0
    while ptr_is_null(node) == 0:
        if _object_node_is_active(node) != 0:
            obj = load_ptr(node, 0)
            if ptr_is_null(obj) == 0:
                if is_tagged_int(obj) == 0:
                    size: int = _object_node_size(node)
                    if _backend4_page_class_for_size(size) == page_class:
                        if count_bytes != 0:
                            total = total + size
                        else:
                            total = total + 1
        node = _object_node_next(node)
    _object_graph_unlock()
    return total


@c_abi_export("pcc_gc_backend4_small_page_object_count")
def pcc_gc_backend4_small_page_object_count() -> int:
    return _backend4_page_class_population(0, 0)


@c_abi_export("pcc_gc_backend4_medium_page_object_count")
def pcc_gc_backend4_medium_page_object_count() -> int:
    return _backend4_page_class_population(1, 0)


@c_abi_export("pcc_gc_backend4_large_page_object_count")
def pcc_gc_backend4_large_page_object_count() -> int:
    return _backend4_page_class_population(2, 0)


@c_abi_export("pcc_gc_backend4_small_page_live_bytes")
def pcc_gc_backend4_small_page_live_bytes() -> int:
    return _backend4_page_class_population(0, 1)


@c_abi_export("pcc_gc_backend4_medium_page_live_bytes")
def pcc_gc_backend4_medium_page_live_bytes() -> int:
    return _backend4_page_class_population(1, 1)


@c_abi_export("pcc_gc_backend4_large_page_live_bytes")
def pcc_gc_backend4_large_page_live_bytes() -> int:
    return _backend4_page_class_population(2, 1)


@c_abi_export("pcc_gc_backend4_zpage_note_owner_promoted")
def _backend4_zpage_note_owner_promoted(owner) -> None:
    if ptr_is_null(owner) != 0 or is_tagged_int(owner) != 0:
        return
    node = _backend4_zpage_find(owner)
    if ptr_is_null(node) != 0:
        return
    page = load_ptr(node, 8)
    if ptr_is_null(page) == 0:
        store_i32(page, 28, 2)


def _backend4_zpage_population(metric: int) -> int:
    _object_graph_lock()
    page = _zpage_page_head()
    total: int = 0
    while ptr_is_null(page) == 0:
        if ptr_is_null(page) == 0:
            used: int = load_i64(page, 8)
            capacity: int = load_i64(page, 16)
            allocated: int = load_i64(page, 64)
            page_class: int = load_i32(page, 24)
            if metric == 0:
                total = total + 1
            elif metric == 1:
                total = total + capacity
            elif metric == 2:
                if capacity > used:
                    total = total + capacity - used
            elif metric == 3:
                if page_class == 2:
                    total = total + 1
            elif metric == 4:
                total = total + load_i64(page, 40)
            elif metric == 5:
                if load_i64(page, 48) > 0:
                    total = total + 1
            elif metric == 6:
                if capacity > used:
                    total = total + 1
            elif metric == 7:
                if load_i32(page, 28) == 1:
                    total = total + 1
            elif metric == 8:
                if load_i32(page, 28) == 2:
                    total = total + 1
            elif metric == 9:
                total = total + load_i64(page, 48)
            elif metric == 10:
                total = total + allocated
            elif metric == 11:
                if allocated > used:
                    total = total + allocated - used
            elif metric == 12:
                total = total + load_i64(page, 80)
        page = load_ptr(page, 56)
    _object_graph_unlock()
    return total


@c_abi_export("pcc_gc_backend4_zpage_count")
def pcc_gc_backend4_zpage_count() -> int:
    return _backend4_zpage_population(0)


@c_abi_export("pcc_gc_backend4_zpage_capacity_bytes")
def pcc_gc_backend4_zpage_capacity_bytes() -> int:
    return _backend4_zpage_population(1)


@c_abi_export("pcc_gc_backend4_zpage_fragmentation_bytes")
def pcc_gc_backend4_zpage_fragmentation_bytes() -> int:
    return _backend4_zpage_population(2)


@c_abi_export("pcc_gc_backend4_zpage_large_pages")
def pcc_gc_backend4_zpage_large_pages() -> int:
    return _backend4_zpage_population(3)


@c_abi_export("pcc_gc_backend4_zpage_remembered_slots")
def pcc_gc_backend4_zpage_remembered_slots() -> int:
    return _backend4_zpage_population(4)


@c_abi_export("pcc_gc_backend4_zpage_remembered_cards")
def pcc_gc_backend4_zpage_remembered_cards() -> int:
    return _backend4_zpage_population(9)


@c_abi_export("pcc_gc_backend4_zpage_remembered_card_ratio_per_mille")
def pcc_gc_backend4_zpage_remembered_card_ratio_per_mille() -> int:
    # Read-only density telemetry; selector policy stays on absolute pressure.
    pages: int = pcc_gc_backend4_zpage_count()
    if pages <= 0:
        return 0
    capacity: int = pages * 64
    if capacity <= 0:
        return 0
    cards: int = pcc_gc_backend4_zpage_remembered_cards()
    if cards <= 0:
        return 0
    if cards >= capacity:
        return 1000
    return (cards * 1000) // capacity


@c_abi_export("pcc_gc_backend4_zpage_dirty_pages")
def pcc_gc_backend4_zpage_dirty_pages() -> int:
    return _backend4_zpage_population(5)


@c_abi_export("pcc_gc_backend4_zpage_fragmented_pages")
def pcc_gc_backend4_zpage_fragmented_pages() -> int:
    return _backend4_zpage_population(6)


@c_abi_export("pcc_gc_backend4_zpage_young_pages")
def pcc_gc_backend4_zpage_young_pages() -> int:
    return _backend4_zpage_population(7)


@c_abi_export("pcc_gc_backend4_zpage_old_pages")
def pcc_gc_backend4_zpage_old_pages() -> int:
    return _backend4_zpage_population(8)


def _backend4_zpage_free_population(metric: int) -> int:
    _object_graph_lock()
    page = _zpage_free_page_head()
    total: int = 0
    while ptr_is_null(page) == 0:
        if metric == 0:
            total = total + 1
        elif metric == 1:
            total = total + load_i64(page, 16)
        elif metric == 2:
            total = total + load_i64(page, 80)
        page = load_ptr(page, 56)
    _object_graph_unlock()
    return total


@c_abi_export("pcc_gc_backend4_zpage_free_pages")
def pcc_gc_backend4_zpage_free_pages() -> int:
    return _backend4_zpage_free_population(0)


@c_abi_export("pcc_gc_backend4_zpage_free_capacity_bytes")
def pcc_gc_backend4_zpage_free_capacity_bytes() -> int:
    return _backend4_zpage_free_population(1)


@c_abi_export("pcc_gc_backend4_zpage_free_span_bytes")
def pcc_gc_backend4_zpage_free_span_bytes() -> int:
    return _backend4_zpage_free_population(2)


@c_abi_export("pcc_gc_backend4_zpage_used_bytes")
def pcc_gc_backend4_zpage_used_bytes() -> int:
    capacity: int = pcc_gc_backend4_zpage_capacity_bytes()
    fragmentation: int = pcc_gc_backend4_zpage_fragmentation_bytes()
    if capacity <= fragmentation:
        return 0
    return capacity - fragmentation


@c_abi_export("pcc_gc_backend4_zpage_allocated_bytes")
def pcc_gc_backend4_zpage_allocated_bytes() -> int:
    return _backend4_zpage_population(10)


@c_abi_export("pcc_gc_backend4_zpage_reclaimable_gap_bytes")
def pcc_gc_backend4_zpage_reclaimable_gap_bytes() -> int:
    return _backend4_zpage_population(11)


@c_abi_export("pcc_gc_backend4_zpage_span_bytes")
def pcc_gc_backend4_zpage_span_bytes() -> int:
    return _backend4_zpage_population(12)


@c_abi_export("pcc_gc_backend4_zpage_owner_offset_bytes")
def pcc_gc_backend4_zpage_owner_offset_bytes(owner) -> int:
    _init_config()
    if ptr_is_null(owner) != 0 or is_tagged_int(owner) != 0:
        return -1
    node = _backend4_zpage_find(owner)
    if ptr_is_null(node) != 0:
        return -1
    return load_i64(node, 24)


@c_abi_export("pcc_gc_backend4_zpage_owner_size_bytes")
def pcc_gc_backend4_zpage_owner_size_bytes(owner) -> int:
    _init_config()
    if ptr_is_null(owner) != 0 or is_tagged_int(owner) != 0:
        return -1
    node = _backend4_zpage_find(owner)
    if ptr_is_null(node) != 0:
        return -1
    return load_i64(node, 32)


@c_abi_export("pcc_gc_backend4_zpage_owner_span_card")
def pcc_gc_backend4_zpage_owner_span_card(owner) -> int:
    offset: int = pcc_gc_backend4_zpage_owner_offset_bytes(owner)
    if offset < 0:
        return -1
    return (offset // 512) % 64


def _backend4_zpage_payload_offset_for_slot(owner_node, slot) -> int:
    if ptr_is_null(owner_node) != 0:
        return -1
    if ptr_is_null(slot) != 0:
        return -1
    span = load_ptr(owner_node, 64)
    while ptr_is_null(span) == 0:
        base = load_ptr(span, 8)
        size: int = load_i64(span, 16)
        offset: int = load_i64(span, 24)
        if ptr_is_null(base) == 0 and size > 0 and offset >= 0:
            delta: int = ptr_diff(slot, base)
            if delta >= 0 and delta < size:
                return offset + delta
        span = load_ptr(span, 40)
    return -1


@c_abi_export("pcc_gc_backend4_zpage_owner_slot_span_card")
def pcc_gc_backend4_zpage_owner_slot_span_card(owner, slot) -> int:
    _init_config()
    if ptr_is_null(owner) != 0 or is_tagged_int(owner) != 0:
        return -1
    if ptr_is_null(slot) != 0:
        return -1
    node = _backend4_zpage_find(owner)
    if ptr_is_null(node) != 0:
        return -1
    span_offset: int = load_i64(node, 24)
    size: int = load_i64(node, 32)
    if span_offset < 0:
        return -1
    if size > 0:
        inline_delta: int = ptr_diff(slot, owner)
        if inline_delta >= 0 and inline_delta < size:
            span_offset = span_offset + inline_delta
        else:
            payload_offset: int = _backend4_zpage_payload_offset_for_slot(
                node,
                slot,
            )
            if payload_offset >= 0:
                span_offset = payload_offset
    return (span_offset // 512) % 64


@c_abi_export("pcc_gc_backend4_zpage_payload_span_preflight_locked")
def pcc_gc_backend4_zpage_payload_span_preflight_locked(
    owner, total_size_bytes: int
) -> int:
    if (
        ptr_is_null(owner) != 0
        or is_tagged_int(owner) != 0
        or total_size_bytes < 0
    ):
        return 0
    if total_size_bytes == 0:
        return 1
    node = _backend4_zpage_find(owner)
    if ptr_is_null(node) != 0:
        return 0
    if ptr_is_null(load_ptr(node, 64)) == 0:
        return 0
    page = load_ptr(node, 8)
    if ptr_is_null(page) != 0:
        return 0
    allocated: int = load_i64(page, 64)
    capacity: int = load_i64(page, 16)
    if allocated < 0 or allocated > capacity:
        return 0
    if total_size_bytes > capacity - allocated:
        return 0
    return 1


@c_abi_export(
    "pcc_gc_backend4_zpage_publish_relocation_payload_spans_locked"
)
def pcc_gc_backend4_zpage_publish_relocation_payload_spans_locked(
    owner, span_head, span_count: int, total_size_bytes: int
) -> int:
    if ptr_is_null(owner) != 0 or is_tagged_int(owner) != 0:
        return 0
    if (
        ptr_is_null(span_head) != 0
        or span_count <= 0
        or span_count > 4
        or total_size_bytes <= 0
    ):
        return 0
    node = _backend4_zpage_find(owner)
    if ptr_is_null(node) != 0:
        return 0
    if ptr_is_null(load_ptr(node, 64)) == 0:
        return 0
    page = load_ptr(node, 8)
    if ptr_is_null(page) != 0:
        return 0
    computed_size_bytes: int = 0
    span = span_head
    index: int = 0
    while index < span_count:
        if ptr_is_null(span) != 0 or ptr_is_null(load_ptr(span, 8)) != 0:
            return 0
        size_bytes: int = load_i64(span, 16)
        if size_bytes <= 0 or size_bytes > 9223372036854775807 - computed_size_bytes:
            return 0
        computed_size_bytes = computed_size_bytes + size_bytes
        span = load_ptr(span, 40)
        index = index + 1
    if ptr_is_null(span) == 0 or computed_size_bytes != total_size_bytes:
        return 0
    allocated: int = load_i64(page, 64)
    capacity: int = load_i64(page, 16)
    if allocated < 0 or allocated > capacity:
        return 0
    if total_size_bytes > capacity - allocated:
        return 0
    used: int = load_i64(page, 8)
    if used < 0 or total_size_bytes > 9223372036854775807 - used:
        return 0

    offset_bytes: int = allocated
    span = span_head
    index = 0
    while index < span_count:
        store_ptr(span, 0, owner)
        store_i64(span, 24, offset_bytes)
        store_ptr(span, 32, page)
        offset_bytes = offset_bytes + load_i64(span, 16)
        span = load_ptr(span, 40)
        index = index + 1
    store_ptr(node, 64, span_head)
    store_i64(page, 64, offset_bytes)
    store_i64(page, 8, used + total_size_bytes)
    return 1


@c_abi_export("pcc_gc_backend4_zpage_register_owner_payload_span")
def pcc_gc_backend4_zpage_register_owner_payload_span(
    owner, base, size_bytes: int
) -> int:
    if ptr_is_null(owner) != 0 or is_tagged_int(owner) != 0:
        return -1
    if ptr_is_null(base) != 0 or size_bytes <= 0:
        return -1
    backend: int = _init_config()
    if backend != 4:
        return -1
    node = _backend4_zpage_find(owner)
    if ptr_is_null(node) != 0:
        return -1
    page = load_ptr(node, 8)
    if ptr_is_null(page) != 0:
        return -1
    span_existing = load_ptr(node, 64)
    while ptr_is_null(span_existing) == 0:
        if ptr_eq(load_ptr(span_existing, 8), base) != 0:
            if ptr_eq(load_ptr(span_existing, 32), page) == 0:
                return -1
            offset_existing: int = load_i64(span_existing, 24)
            if offset_existing < 0:
                store_i64(span_existing, 16, size_bytes)
                return 0
            capacity_existing: int = load_i64(page, 16)
            if size_bytes > capacity_existing - offset_existing:
                overflow_old_size: int = load_i64(span_existing, 16)
                used_existing: int = load_i64(page, 8)
                if used_existing >= overflow_old_size:
                    store_i64(page, 8, used_existing - overflow_old_size)
                else:
                    store_i64(page, 8, 0)
                store_i64(span_existing, 24, -1)
                store_i64(span_existing, 16, size_bytes)
                return 0
            old_size: int = load_i64(span_existing, 16)
            used_existing: int = load_i64(page, 8)
            if size_bytes >= old_size:
                store_i64(page, 8, used_existing + size_bytes - old_size)
            else:
                delta_existing: int = old_size - size_bytes
                if used_existing >= delta_existing:
                    store_i64(page, 8, used_existing - delta_existing)
                else:
                    store_i64(page, 8, 0)
            store_i64(span_existing, 16, size_bytes)
            end_existing: int = offset_existing + size_bytes
            allocated_existing: int = load_i64(page, 64)
            if allocated_existing < end_existing:
                store_i64(page, 64, end_existing)
            return offset_existing
        span_existing = load_ptr(span_existing, 40)
    allocated: int = load_i64(page, 64)
    capacity: int = load_i64(page, 16)
    if allocated > capacity:
        return -1
    span = malloc(48)
    if ptr_is_null(span) != 0:
        return -1
    store_ptr(span, 0, owner)
    store_ptr(span, 8, base)
    store_i64(span, 16, size_bytes)
    external: int = 0
    if size_bytes > capacity - allocated:
        external = 1
    store_i64(span, 24, -1 if external != 0 else allocated)
    store_ptr(span, 32, page)
    store_ptr(span, 40, load_ptr(node, 64))
    store_ptr(node, 64, span)
    if external == 0:
        store_i64(page, 64, allocated + size_bytes)
        store_i64(page, 8, load_i64(page, 8) + size_bytes)
        return allocated
    return 0


@c_abi_export("pcc_gc_backend4_zpage_unregister_owner_payload_span")
def pcc_gc_backend4_zpage_unregister_owner_payload_span(owner, base) -> int:
    if ptr_is_null(owner) != 0 or is_tagged_int(owner) != 0:
        return -1
    if ptr_is_null(base) != 0:
        return -1
    _init_config()
    node = _backend4_zpage_find(owner)
    if ptr_is_null(node) != 0:
        return 0
    return _backend4_zpage_remove_payload_span_base(node, base)


@c_abi_export("pcc_gc_backend4_zpage_retarget_owner_payload_span")
def pcc_gc_backend4_zpage_retarget_owner_payload_span(
    owner, old_base, new_base, size_bytes: int
) -> int:
    if ptr_is_null(owner) != 0 or is_tagged_int(owner) != 0:
        return -1
    if ptr_is_null(old_base) != 0 or ptr_is_null(new_base) != 0:
        return -1
    if size_bytes <= 0:
        return -1
    _init_config()
    node = _backend4_zpage_find(owner)
    if ptr_is_null(node) != 0:
        return -1
    page = load_ptr(node, 8)
    if ptr_is_null(page) != 0:
        return -1
    span = load_ptr(node, 64)
    while ptr_is_null(span) == 0:
        if ptr_eq(load_ptr(span, 8), old_base) != 0:
            if ptr_eq(load_ptr(span, 32), page) == 0:
                return -1
            offset: int = load_i64(span, 24)
            if offset < 0:
                store_ptr(span, 8, new_base)
                store_i64(span, 16, size_bytes)
                return 0
            capacity: int = load_i64(page, 16)
            if size_bytes > capacity - offset:
                overflow_old_size: int = load_i64(span, 16)
                used: int = load_i64(page, 8)
                if used >= overflow_old_size:
                    store_i64(page, 8, used - overflow_old_size)
                else:
                    store_i64(page, 8, 0)
                store_ptr(span, 8, new_base)
                store_i64(span, 16, size_bytes)
                store_i64(span, 24, -1)
                return 0
            old_size: int = load_i64(span, 16)
            used: int = load_i64(page, 8)
            if size_bytes >= old_size:
                store_i64(page, 8, used + size_bytes - old_size)
            else:
                delta: int = old_size - size_bytes
                if used >= delta:
                    store_i64(page, 8, used - delta)
                else:
                    store_i64(page, 8, 0)
            store_ptr(span, 8, new_base)
            store_i64(span, 16, size_bytes)
            end: int = offset + size_bytes
            allocated: int = load_i64(page, 64)
            if allocated < end:
                store_i64(page, 64, end)
            return offset
        span = load_ptr(span, 40)
    return -1


def _backend4_map_mutator_payload_slot(
    slot,
    old_base,
    old_size_bytes: int,
    new_base,
    new_size_bytes: int,
    slot_pairs,
    pair_count: int,
):
    if ptr_is_null(slot) != 0:
        return slot
    offset: int = ptr_diff(slot, old_base)
    if offset < 0 or offset > old_size_bytes - 8:
        return slot
    if (offset & 7) != 0:
        return null()
    i: int = 0
    while i < pair_count:
        old_slot = load_ptr(slot_pairs, i * 16)
        if ptr_eq(old_slot, slot) != 0:
            return load_ptr(slot_pairs, i * 16 + 8)
        i = i + 1
    if offset >= new_size_bytes:
        return null()
    return ptr_add(new_base, offset)


@c_abi_export("pcc_gc_backend4_retarget_mutator_payload_locked")
def pcc_gc_backend4_retarget_mutator_payload_locked(
    owner,
    old_base,
    old_size_bytes: int,
    new_base,
    new_size_bytes: int,
    slot_pairs,
    pair_count: int,
) -> int:
    if load_i32(global_addr("pcc_gc_backend_selected"), 0) != 4:
        return 1
    if ptr_is_null(owner) != 0 or is_tagged_int(owner) != 0:
        return 0
    if ptr_is_null(old_base) != 0 or ptr_is_null(new_base) != 0:
        return 0
    if old_size_bytes <= 0 or new_size_bytes < old_size_bytes:
        return 0
    if pair_count < 0:
        return 0
    if pair_count > 0 and ptr_is_null(slot_pairs) != 0:
        return 0

    owner_node = _backend4_zpage_find(owner)
    if ptr_is_null(owner_node) != 0:
        return 0
    page = load_ptr(owner_node, 8)
    if ptr_is_null(page) != 0:
        return 0
    payload_span = load_ptr(owner_node, 64)
    while ptr_is_null(payload_span) == 0:
        if ptr_eq(load_ptr(payload_span, 8), old_base) != 0:
            break
        payload_span = load_ptr(payload_span, 40)
    has_payload_span: int = 0
    span_offset: int = 0
    if ptr_is_null(payload_span) == 0:
        has_payload_span = 1
        if ptr_eq(load_ptr(payload_span, 32), page) == 0:
            return 0
        if load_i64(payload_span, 16) != old_size_bytes:
            return 0
        span_offset = load_i64(payload_span, 24)
        if span_offset < -1:
            return 0

    i = 0
    while i < pair_count:
        old_slot = load_ptr(slot_pairs, i * 16)
        new_slot = load_ptr(slot_pairs, i * 16 + 8)
        old_offset: int = ptr_diff(old_slot, old_base)
        new_offset: int = ptr_diff(new_slot, new_base)
        if old_offset < 0 or old_offset > old_size_bytes - 8:
            return 0
        if new_offset < 0 or new_offset > new_size_bytes - 8:
            return 0
        if (old_offset & 7) != 0 or (new_offset & 7) != 0:
            return 0
        i = i + 1

    entry = _store_buffer_medium_head()
    while ptr_is_null(entry) == 0:
        if ptr_eq(load_ptr(entry, 0), owner) != 0:
            mapped = _backend4_map_mutator_payload_slot(
                load_ptr(entry, 8),
                old_base,
                old_size_bytes,
                new_base,
                new_size_bytes,
                slot_pairs,
                pair_count,
            )
            store_ptr(entry, 8, mapped)
        entry = load_ptr(entry, 24)
    entry = _store_buffer_head()
    while ptr_is_null(entry) == 0:
        if ptr_eq(load_ptr(entry, 0), owner) != 0:
            mapped = _backend4_map_mutator_payload_slot(
                load_ptr(entry, 8),
                old_base,
                old_size_bytes,
                new_base,
                new_size_bytes,
                slot_pairs,
                pair_count,
            )
            store_ptr(entry, 8, mapped)
        entry = load_ptr(entry, 24)

    entry = _remembered_set_head()
    while ptr_is_null(entry) == 0:
        if ptr_eq(load_ptr(entry, 0), owner) != 0:
            mapped = _backend4_map_mutator_payload_slot(
                load_ptr(entry, 8),
                old_base,
                old_size_bytes,
                new_base,
                new_size_bytes,
                slot_pairs,
                pair_count,
            )
            if ptr_is_null(mapped) != 0:
                return 0
            store_ptr(entry, 8, mapped)
        entry = load_ptr(entry, 16)

    if has_payload_span != 0:
        old_span_size: int = load_i64(payload_span, 16)
        page_capacity: int = load_i64(page, 16)
        if span_offset >= 0 and new_size_bytes > page_capacity - span_offset:
            used: int = load_i64(page, 8)
            if used >= old_span_size:
                store_i64(page, 8, used - old_span_size)
            else:
                store_i64(page, 8, 0)
            span_offset = -1
            store_i64(payload_span, 24, -1)
        elif span_offset >= 0:
            used = load_i64(page, 8)
            if new_size_bytes >= old_span_size:
                store_i64(page, 8, used + new_size_bytes - old_span_size)
            else:
                delta: int = old_span_size - new_size_bytes
                if used >= delta:
                    store_i64(page, 8, used - delta)
                else:
                    store_i64(page, 8, 0)
        store_ptr(payload_span, 8, new_base)
        store_i64(payload_span, 16, new_size_bytes)
        if span_offset >= 0:
            span_end: int = span_offset + new_size_bytes
            allocated: int = load_i64(page, 64)
            if allocated < span_end:
                store_i64(page, 64, span_end)
        return 1
    return 2


@c_abi_export("pcc_gc_backend4_zpage_fragmentation_per_mille")
def pcc_gc_backend4_zpage_fragmentation_per_mille() -> int:
    capacity: int = pcc_gc_backend4_zpage_capacity_bytes()
    if capacity <= 0:
        return 0
    fragmentation: int = pcc_gc_backend4_zpage_fragmentation_bytes()
    if fragmentation <= 0:
        return 0
    if fragmentation >= capacity:
        return 1000
    return (fragmentation * 1000) // capacity


@c_abi_export("pcc_gc_backend4_zpage_policy_score")
def pcc_gc_backend4_zpage_policy_score() -> int:
    return (
        pcc_gc_backend4_zpage_fragmentation_bytes()
        + pcc_gc_backend4_fragmentation_backlog_bytes()
        + pcc_gc_backend4_evacuation_incomplete_batches()
        + pcc_gc_backend4_zpage_remembered_slots()
        + pcc_gc_backend4_zpage_remembered_cards()
        + pcc_gc_backend4_zpage_dirty_pages()
        + pcc_gc_backend4_zpage_fragmented_pages()
        + pcc_gc_backend4_zpage_old_pages()
    )


@c_abi_export("pcc_gc_backend4_small_page_candidate_score")
def pcc_gc_backend4_small_page_candidate_score() -> int:
    return load_i32(global_addr("pcc_gc_backend4_small_page_candidates"), 0)


@c_abi_export("pcc_gc_backend4_medium_page_candidate_score")
def pcc_gc_backend4_medium_page_candidate_score() -> int:
    return load_i32(global_addr("pcc_gc_backend4_medium_page_candidates"), 0)


@c_abi_export("pcc_gc_backend4_evacuation_candidate_bytes")
def pcc_gc_backend4_evacuation_candidate_bytes() -> int:
    return load_i32(global_addr("pcc_gc_backend4_evacuation_candidate_bytes_count"), 0)


@c_abi_export("pcc_gc_backend4_small_page_candidate_bytes")
def pcc_gc_backend4_small_page_candidate_bytes() -> int:
    return load_i32(global_addr("pcc_gc_backend4_small_page_candidate_bytes_count"), 0)


@c_abi_export("pcc_gc_backend4_medium_page_candidate_bytes")
def pcc_gc_backend4_medium_page_candidate_bytes() -> int:
    return load_i32(global_addr("pcc_gc_backend4_medium_page_candidate_bytes_count"), 0)


@c_abi_export("pcc_gc_backend4_evacuation_candidate_zpage_bytes")
def pcc_gc_backend4_evacuation_candidate_zpage_bytes() -> int:
    return load_i32(
        global_addr("pcc_gc_backend4_evacuation_candidate_zpage_bytes_count"), 0
    )


@c_abi_export("pcc_gc_backend4_small_page_candidate_zpage_bytes")
def pcc_gc_backend4_small_page_candidate_zpage_bytes() -> int:
    return load_i32(
        global_addr("pcc_gc_backend4_small_page_candidate_zpage_bytes_count"), 0
    )


@c_abi_export("pcc_gc_backend4_medium_page_candidate_zpage_bytes")
def pcc_gc_backend4_medium_page_candidate_zpage_bytes() -> int:
    return load_i32(
        global_addr("pcc_gc_backend4_medium_page_candidate_zpage_bytes_count"), 0
    )


@c_abi_export("pcc_gc_backend4_evacuation_page_candidate_score")
def pcc_gc_backend4_evacuation_page_candidate_score() -> int:
    return _backend4_evacuation_page_population(0)


def _backend4_evacuation_page_population(metric: int) -> int:
    node = _evacuation_page_head()
    total: int = 0
    while ptr_is_null(node) == 0:
        page = load_ptr(node, 0)
        if ptr_is_null(page) == 0:
            if metric == 0:
                total = total + 1
            elif metric == 1:
                total = total + load_i64(page, 8)
            elif metric == 2:
                total = total + load_i64(page, 48)
        node = load_ptr(node, 8)
    return total


@c_abi_export("pcc_gc_backend4_evacuation_page_candidate_bytes")
def pcc_gc_backend4_evacuation_page_candidate_bytes() -> int:
    return _backend4_evacuation_page_population(1)


@c_abi_export("pcc_gc_backend4_evacuation_page_dirty_cards")
def pcc_gc_backend4_evacuation_page_dirty_cards() -> int:
    return _backend4_evacuation_page_population(2)


@c_abi_export("pcc_gc_backend4_store_buffer_drain_batches")
def pcc_gc_backend4_store_buffer_drain_batches() -> int:
    return load_i32(global_addr("pcc_gc_backend4_store_buffer_drain_batches_count"), 0)


@c_abi_export("pcc_gc_backend4_store_buffer_drained_entries")
def pcc_gc_backend4_store_buffer_drained_entries() -> int:
    return load_i32(
        global_addr("pcc_gc_backend4_store_buffer_drained_entries_count"), 0
    )


@c_abi_export("pcc_gc_backend4_store_buffer_duplicate_skips")
def pcc_gc_backend4_store_buffer_duplicate_skips() -> int:
    return load_i32(
        global_addr("pcc_gc_backend4_store_buffer_duplicate_skips_count"), 0
    )


@c_abi_export("pcc_gc_backend4_store_buffer_high_water")
def pcc_gc_backend4_store_buffer_high_water() -> int:
    return load_i32(global_addr("pcc_gc_backend4_store_buffer_high_water_count"), 0)


@c_abi_export("pcc_gc_backend4_store_buffer_owner_fanout_high_water")
def pcc_gc_backend4_store_buffer_owner_fanout_high_water() -> int:
    return load_i32(
        global_addr("pcc_gc_backend4_store_buffer_owner_fanout_high_water_count"), 0
    )


@c_abi_export("pcc_gc_backend4_store_buffer_owner_count_high_water")
def pcc_gc_backend4_store_buffer_owner_count_high_water() -> int:
    return load_i32(
        global_addr("pcc_gc_backend4_store_buffer_owner_count_high_water_count"), 0
    )


@c_abi_export("pcc_gc_backend4_store_buffer_incomplete_drains")
def pcc_gc_backend4_store_buffer_incomplete_drains() -> int:
    return load_i32(
        global_addr("pcc_gc_backend4_store_buffer_incomplete_drains_count"), 0
    )


@c_abi_export("pcc_gc_backend4_evacuation_incomplete_batches")
def pcc_gc_backend4_evacuation_incomplete_batches() -> int:
    return load_i32(
        global_addr("pcc_gc_backend4_evacuation_incomplete_batches_count"), 0
    )


@c_abi_export("pcc_gc_backend4_store_buffer_batch_capacity")
def pcc_gc_backend4_store_buffer_batch_capacity() -> int:
    return _backend4_store_buffer_batch_capacity()


@c_abi_export("pcc_gc_backend4_store_buffer_max_batch_size")
def pcc_gc_backend4_store_buffer_max_batch_size() -> int:
    return load_i32(global_addr("pcc_gc_backend4_store_buffer_max_batch_size_count"), 0)


@c_abi_export("pcc_gc_backend4_store_buffer_full_batches")
def pcc_gc_backend4_store_buffer_full_batches() -> int:
    return load_i32(global_addr("pcc_gc_backend4_store_buffer_full_batches_count"), 0)


@c_abi_export("pcc_gc_backend4_store_buffer_medium_capacity")
def pcc_gc_backend4_store_buffer_medium_capacity() -> int:
    return _backend4_store_buffer_medium_capacity()


@c_abi_export("pcc_gc_backend4_store_buffer_medium_pending")
def pcc_gc_backend4_store_buffer_medium_pending() -> int:
    return _backend4_store_buffer_medium_count()


@c_abi_export("pcc_gc_backend4_store_buffer_medium_flushes")
def pcc_gc_backend4_store_buffer_medium_flushes() -> int:
    return load_i32(global_addr("pcc_gc_backend4_store_buffer_medium_flushes_count"), 0)


@c_abi_export("pcc_gc_backend4_store_buffer_medium_flushed_entries")
def pcc_gc_backend4_store_buffer_medium_flushed_entries() -> int:
    return load_i32(
        global_addr("pcc_gc_backend4_store_buffer_medium_flushed_entries_count"), 0
    )


@c_abi_export("pcc_gc_backend4_store_buffer_medium_full_flushes")
def pcc_gc_backend4_store_buffer_medium_full_flushes() -> int:
    return load_i32(
        global_addr("pcc_gc_backend4_store_buffer_medium_full_flushes_count"), 0
    )


@c_abi_export("pcc_gc_backend4_store_buffer_cross_thread_medium_flushes")
def pcc_gc_backend4_store_buffer_cross_thread_medium_flushes() -> int:
    return 0


@c_abi_export("pcc_gc_backend4_store_buffer_cross_thread_medium_flushed_entries")
def pcc_gc_backend4_store_buffer_cross_thread_medium_flushed_entries() -> int:
    return 0


@c_abi_export("pcc_gc_backend4_remembered_set_entries")
def pcc_gc_backend4_remembered_set_entries() -> int:
    return load_i32(global_addr("pcc_gc_backend4_remembered_set_entries_count"), 0)


@c_abi_export("pcc_gc_backend4_remembered_set_duplicate_skips")
def pcc_gc_backend4_remembered_set_duplicate_skips() -> int:
    return load_i32(
        global_addr("pcc_gc_backend4_remembered_set_duplicate_skips_count"), 0
    )


@c_abi_export("pcc_gc_backend4_remembered_set_high_water")
def pcc_gc_backend4_remembered_set_high_water() -> int:
    return load_i32(global_addr("pcc_gc_backend4_remembered_set_high_water_count"), 0)


@c_abi_export("pcc_gc_backend4_remembered_page_entries")
def pcc_gc_backend4_remembered_page_entries() -> int:
    return 0


@c_abi_export("pcc_gc_backend4_remembered_page_slot_entries")
def pcc_gc_backend4_remembered_page_slot_entries() -> int:
    return 0


@c_abi_export("pcc_gc_backend4_remembered_page_high_water")
def pcc_gc_backend4_remembered_page_high_water() -> int:
    return 0


@c_abi_export("pcc_gc_backend4_remembered_page_contains_slot")
def pcc_gc_backend4_remembered_page_contains_slot(slot) -> int:
    node = _remembered_set_head()
    while ptr_is_null(node) == 0:
        if ptr_eq(load_ptr(node, 8), slot) != 0:
            return 1
        node = load_ptr(node, 16)
    return 0


@c_abi_export("pcc_gc_backend4_remembered_page_clear_slot")
def pcc_gc_backend4_remembered_page_clear_slot(slot) -> int:
    return _backend4_remembered_set_remove_slot(slot)


@c_abi_export("pcc_gc_backend4_zpage_contains_remembered_card")
def pcc_gc_backend4_zpage_contains_remembered_card(owner, slot) -> int:
    # Mirror fallback: the pcc-Python runtime does not yet model pointer-page
    # card grouping, so this answers exact owner+slot membership.
    return _backend4_remembered_set_contains(owner, slot)


@c_abi_export("pcc_gc_backend4_zpage_clear_remembered_card")
def pcc_gc_backend4_zpage_clear_remembered_card(owner, slot) -> int:
    # Mirror fallback: exact owner+slot clear, not full card clear.
    if _backend4_remembered_set_contains(owner, slot) == 0:
        return 0
    return _backend4_remembered_set_remove_slot(slot)


@c_abi_export("pcc_gc_backend4_verify_no_old_addresses")
def pcc_gc_backend4_verify_no_old_addresses() -> int:
    if pcc_gc_backend() != 4:
        return 1
    node = _forwarding_head()
    while ptr_is_null(node) == 0:
        from_obj = load_ptr(node, 0)
        to_obj = load_ptr(node, 8)
        if ptr_is_null(from_obj) != 0:
            return 0
        if ptr_is_null(to_obj) != 0:
            return 0
        if ptr_eq(from_obj, to_obj) != 0:
            return 0
        if (load_i32(to_obj, 12) & 256) != 0:
            return 0
        node = load_ptr(node, 16)
    return 1


@c_abi_export("pcc_gc_free_object_memory")
def pcc_gc_free_object_memory(o) -> None:
    if ptr_is_null(o) != 0:
        return
    if is_tagged_int(o) != 0:
        return
    if pcc_gc_pointer_is_managed(o) == 0:
        return
    backend: int = 0
    if load_i32(global_addr("pcc_gc_config_initialized"), 0) == 0:
        backend = _init_config()
    else:
        backend = load_i32(global_addr("pcc_gc_backend_selected"), 0)
    flags: int = load_i32(o, 12)
    # Direct constructor cleanup does not necessarily pass through decref.
    # The ordinary dealloc path already emitted this idempotent event.
    pcc_gc_note_object_freeing(o)
    # A structurally invalid object-family lifecycle must remain quarantined:
    # never hand the pointer back to an allocator after retirement failed.
    if pcc_gc_pointer_unregister(o) < 0:
        return
    if (flags & 65536) != 0:
        return
    if (backend == 1 or backend == 2) and flags == 0:
        return
    if backend == 4 and (flags & 262144) == 0:
        # Backend 4 publishes either the zpage or malloc allocation-origin
        # flag.  The zpage case returned above.  Unknown/foreign origin must
        # fail closed instead of scanning every page or calling free().
        return
    if (flags & 4096) == 0 and backend != 3:
        free(o)
        return
    if (flags & 4096) != 0 or backend == 3:
        _object_graph_lock()
        node = pcc_gc_object_index_find(o)
        if ptr_is_null(node) == 0:
            block = _object_node_minor_block(node)
            if _object_node_freeing(node) == 0:
                live: int = load_i32(global_addr("pcc_gc_live_bytes"), 0)
                size: int = _object_node_size(node)
                if size >= live:
                    store_i32(global_addr("pcc_gc_live_bytes"), 0, 0)
                else:
                    store_i32(global_addr("pcc_gc_live_bytes"), 0, live - size)
            if backend == 4:
                _backend4_zpage_remove(o)
            pcc_gc_object_index_remove(o)
            _unlink_object_node(node)
            _object_node_release(node)
            if ptr_is_null(block) == 0 or (flags & 4096) != 0:
                _object_graph_unlock()
                _minor_release_block(block)
                return
            _object_graph_unlock()
            if backend == 3 and (flags & 262144) == 0:
                return
            free(o)
            return
        _object_graph_unlock()
    if backend == 3:
        _object_graph_lock()
        owner_block = _minor_block_containing(o)
        _object_graph_unlock()
        if ptr_is_null(owner_block) == 0:
            _minor_release_block(owner_block)
            return
        # Only an explicit allocation-origin bit authorizes system free().
        if (flags & 262144) == 0:
            return
    free(o)


@c_abi_export("pcc_gc_note_object_allocated_sized")
def pcc_gc_note_object_allocated_sized(o, size: int) -> None:
    backend: int = 0
    if load_i32(global_addr("pcc_gc_config_initialized"), 0) == 0:
        backend = _init_config()
    else:
        backend = load_i32(global_addr("pcc_gc_backend_selected"), 0)
    if ptr_is_null(o) != 0:
        return
    if is_tagged_int(o) != 0:
        return
    if size < 16:
        size = 16
    if backend == 0:
        return
    pending_block = null()
    if backend == 3:
        pending_block = _pending_minor_block()
    prepared_node = null()
    prepared_slots = null()
    prepared_cap: int = 0
    prepared_zpage_node = null()
    prepared_zpage_slots = null()
    prepared_zpage_cap: int = 0
    prepared_zpage = null()
    prepared_zpage_from_free: int = 0
    prepared_zpage_ready: int = 0
    node_owner = stack_alloc(8)
    slots_owner = stack_alloc(8)
    zpage_node_owner = stack_alloc(8)
    zpage_slots_owner = stack_alloc(8)
    zpage_owner = stack_alloc(8)
    while 1:
        _object_graph_lock()
        backend = load_i32(global_addr("pcc_gc_backend_selected"), 0)
        graph_leaf: int = 0
        if (
            (backend == 3 or backend == 4)
            and ptr_is_null(pending_block) != 0
            and _backend3_graph_leaf_tag(load_i32(o, 8)) != 0
        ):
            graph_leaf = 1
        if graph_leaf == 0:
            required: int = pcc_gc_object_index_plan_capacity(1)
            need_node: int = _object_node_plan_requires_prepare()
            zpage_required: int = 0
            need_zpage_node: int = 0
            need_zpage_page: int = 0
            if backend == 4:
                zpage_required = pcc_gc_zpage_owner_index_plan_capacity(1)
                need_zpage_node = (
                    _backend4_zpage_node_plan_requires_prepare()
                )
                current_page = null()
                if (load_i32(o, 12) & 65536) != 0:
                    current_page = _backend4_zpage_find_page_for_addr(
                        o, size
                    )
                if ptr_is_null(current_page) != 0:
                    current_page = _backend4_zpage_find_reusable_page(
                        o, size
                    )
                if (
                    ptr_is_null(current_page) != 0
                    and ptr_is_null(prepared_zpage) != 0
                ):
                    prepared_zpage = _backend4_zpage_pop_free_page(size)
                    if ptr_is_null(prepared_zpage) == 0:
                        prepared_zpage_from_free = 1
                    else:
                        prepared_zpage_from_free = 0
                    prepared_zpage_ready = 0
                if (
                    ptr_is_null(current_page) != 0
                    and prepared_zpage_ready == 0
                ):
                    need_zpage_page = 1
            if required < 0 or zpage_required < 0:
                if (
                    ptr_is_null(prepared_zpage) == 0
                    and prepared_zpage_from_free != 0
                ):
                    store_ptr(
                        prepared_zpage,
                        56,
                        global_load_ptr("pcc_gc_backend4_free_page_head"),
                    )
                    global_store_ptr(
                        "pcc_gc_backend4_free_page_head", prepared_zpage
                    )
                    prepared_zpage = null()
                    prepared_zpage_from_free = 0
                _object_graph_unlock()
                free(prepared_node)
                free(prepared_slots)
                free(prepared_zpage_node)
                free(prepared_zpage_slots)
                if ptr_is_null(prepared_zpage) == 0:
                    free(load_ptr(prepared_zpage, 72))
                    free(prepared_zpage)
                _set_pending_minor_block(null())
                global_store_ptr("pcc_gc_last_alloc", o)
                return
            if (
                (need_node != 0 and ptr_is_null(prepared_node) != 0)
                or (
                    required > 0
                    and (
                        ptr_is_null(prepared_slots) != 0
                        or prepared_cap < required
                    )
                )
                or (
                    need_zpage_node != 0
                    and ptr_is_null(prepared_zpage_node) != 0
                )
                or (
                    zpage_required > 0
                    and (
                        ptr_is_null(prepared_zpage_slots) != 0
                        or prepared_zpage_cap < zpage_required
                    )
                )
                or need_zpage_page != 0
            ):
                _object_graph_unlock()
                if need_node != 0 and ptr_is_null(prepared_node) != 0:
                    prepared_node = _object_node_prepare()
                    if ptr_is_null(prepared_node) != 0:
                        free(prepared_slots)
                        free(prepared_zpage_node)
                        free(prepared_zpage_slots)
                        if (
                            ptr_is_null(prepared_zpage) == 0
                            and prepared_zpage_from_free != 0
                        ):
                            _object_graph_lock()
                            store_ptr(
                                prepared_zpage,
                                56,
                                global_load_ptr(
                                    "pcc_gc_backend4_free_page_head"
                                ),
                            )
                            global_store_ptr(
                                "pcc_gc_backend4_free_page_head",
                                prepared_zpage,
                            )
                            _object_graph_unlock()
                            prepared_zpage = null()
                        if ptr_is_null(prepared_zpage) == 0:
                            free(load_ptr(prepared_zpage, 72))
                            free(prepared_zpage)
                        _set_pending_minor_block(null())
                        global_store_ptr("pcc_gc_last_alloc", o)
                        return
                if (
                    required > 0
                    and (
                        ptr_is_null(prepared_slots) != 0
                        or prepared_cap < required
                    )
                ):
                    free(prepared_slots)
                    prepared_slots = malloc(required * 24)
                    if ptr_is_null(prepared_slots) != 0:
                        free(prepared_node)
                        free(prepared_zpage_node)
                        free(prepared_zpage_slots)
                        if (
                            ptr_is_null(prepared_zpage) == 0
                            and prepared_zpage_from_free != 0
                        ):
                            _object_graph_lock()
                            store_ptr(
                                prepared_zpage,
                                56,
                                global_load_ptr(
                                    "pcc_gc_backend4_free_page_head"
                                ),
                            )
                            global_store_ptr(
                                "pcc_gc_backend4_free_page_head",
                                prepared_zpage,
                            )
                            _object_graph_unlock()
                            prepared_zpage = null()
                        if ptr_is_null(prepared_zpage) == 0:
                            free(load_ptr(prepared_zpage, 72))
                            free(prepared_zpage)
                        _set_pending_minor_block(null())
                        global_store_ptr("pcc_gc_last_alloc", o)
                        return
                    memset(prepared_slots, 0, required * 24)
                    prepared_cap = required
                if (
                    need_zpage_node != 0
                    and ptr_is_null(prepared_zpage_node) != 0
                ):
                    prepared_zpage_node = _backend4_zpage_node_prepare()
                    if ptr_is_null(prepared_zpage_node) != 0:
                        free(prepared_node)
                        free(prepared_slots)
                        free(prepared_zpage_slots)
                        if (
                            ptr_is_null(prepared_zpage) == 0
                            and prepared_zpage_from_free != 0
                        ):
                            _object_graph_lock()
                            store_ptr(
                                prepared_zpage,
                                56,
                                global_load_ptr(
                                    "pcc_gc_backend4_free_page_head"
                                ),
                            )
                            global_store_ptr(
                                "pcc_gc_backend4_free_page_head",
                                prepared_zpage,
                            )
                            _object_graph_unlock()
                            prepared_zpage = null()
                        if ptr_is_null(prepared_zpage) == 0:
                            free(load_ptr(prepared_zpage, 72))
                            free(prepared_zpage)
                        _set_pending_minor_block(null())
                        global_store_ptr("pcc_gc_last_alloc", o)
                        return
                if (
                    zpage_required > 0
                    and (
                        ptr_is_null(prepared_zpage_slots) != 0
                        or prepared_zpage_cap < zpage_required
                    )
                ):
                    free(prepared_zpage_slots)
                    prepared_zpage_slots = malloc(zpage_required * 24)
                    if ptr_is_null(prepared_zpage_slots) != 0:
                        free(prepared_node)
                        free(prepared_slots)
                        free(prepared_zpage_node)
                        if (
                            ptr_is_null(prepared_zpage) == 0
                            and prepared_zpage_from_free != 0
                        ):
                            _object_graph_lock()
                            store_ptr(
                                prepared_zpage,
                                56,
                                global_load_ptr(
                                    "pcc_gc_backend4_free_page_head"
                                ),
                            )
                            global_store_ptr(
                                "pcc_gc_backend4_free_page_head",
                                prepared_zpage,
                            )
                            _object_graph_unlock()
                            prepared_zpage = null()
                        if ptr_is_null(prepared_zpage) == 0:
                            free(load_ptr(prepared_zpage, 72))
                            free(prepared_zpage)
                        _set_pending_minor_block(null())
                        global_store_ptr("pcc_gc_last_alloc", o)
                        return
                    memset(prepared_zpage_slots, 0, zpage_required * 24)
                    prepared_zpage_cap = zpage_required
                if need_zpage_page != 0:
                    prepared_zpage = _backend4_zpage_track_page_prepare(
                        prepared_zpage, o, size
                    )
                    if ptr_is_null(prepared_zpage) != 0:
                        prepared_zpage_from_free = 0
                        free(prepared_node)
                        free(prepared_slots)
                        free(prepared_zpage_node)
                        free(prepared_zpage_slots)
                        _set_pending_minor_block(null())
                        global_store_ptr("pcc_gc_last_alloc", o)
                        return
                    prepared_zpage_ready = 1
                continue
            store_ptr(slots_owner, 0, prepared_slots)
            commit_result: int = pcc_gc_object_index_plan_commit(
                slots_owner, prepared_cap, 1
            )
            prepared_slots = load_ptr(slots_owner, 0)
            if commit_result < 0:
                if (
                    ptr_is_null(prepared_zpage) == 0
                    and prepared_zpage_from_free != 0
                ):
                    store_ptr(
                        prepared_zpage,
                        56,
                        global_load_ptr("pcc_gc_backend4_free_page_head"),
                    )
                    global_store_ptr(
                        "pcc_gc_backend4_free_page_head", prepared_zpage
                    )
                    prepared_zpage = null()
                _object_graph_unlock()
                free(prepared_node)
                free(prepared_slots)
                free(prepared_zpage_node)
                free(prepared_zpage_slots)
                if ptr_is_null(prepared_zpage) == 0:
                    free(load_ptr(prepared_zpage, 72))
                    free(prepared_zpage)
                _set_pending_minor_block(null())
                global_store_ptr("pcc_gc_last_alloc", o)
                return
            if backend == 4:
                store_ptr(zpage_slots_owner, 0, prepared_zpage_slots)
                zpage_commit_result: int = (
                    pcc_gc_zpage_owner_index_plan_commit(
                        zpage_slots_owner, prepared_zpage_cap, 1
                    )
                )
                prepared_zpage_slots = load_ptr(zpage_slots_owner, 0)
                if zpage_commit_result < 0:
                    if (
                        ptr_is_null(prepared_zpage) == 0
                        and prepared_zpage_from_free != 0
                    ):
                        store_ptr(
                            prepared_zpage,
                            56,
                            global_load_ptr(
                                "pcc_gc_backend4_free_page_head"
                            ),
                        )
                        global_store_ptr(
                            "pcc_gc_backend4_free_page_head",
                            prepared_zpage,
                        )
                        prepared_zpage = null()
                    _object_graph_unlock()
                    free(prepared_node)
                    free(prepared_slots)
                    free(prepared_zpage_node)
                    free(prepared_zpage_slots)
                    if ptr_is_null(prepared_zpage) == 0:
                        free(load_ptr(prepared_zpage, 72))
                        free(prepared_zpage)
                    _set_pending_minor_block(null())
                    global_store_ptr("pcc_gc_last_alloc", o)
                    return

        if backend == 1 or backend == 2:
            flags: int = load_i32(o, 12)
            color: int = 8
            if load_i32(global_addr("pcc_gc_mark_active"), 0) != 0:
                color = 32
            store_i32(o, 12, (flags & ~56) | color | 16384)
            store_i32(global_addr("pcc_gc_cycle_requested"), 0, 1)
        elif backend == 3:
            flags: int = load_i32(o, 12)
            new_flags: int = (flags & ~(56 | 384)) | 136
            if ptr_is_null(pending_block) == 0:
                new_flags = new_flags | 4096
            store_i32(o, 12, new_flags)
            store_i32(global_addr("pcc_gc_cycle_requested"), 0, 1)
        elif backend == 4:
            flags: int = load_i32(o, 12)
            new_flags: int = (flags & ~(56 | 2048 | 8192)) | 8
            if (flags & 384) == 0:
                new_flags = new_flags | 128
            store_i32(o, 12, new_flags)
            store_i32(global_addr("pcc_gc_cycle_requested"), 0, 1)
        if graph_leaf != 0:
            if (
                ptr_is_null(prepared_zpage) == 0
                and prepared_zpage_from_free != 0
            ):
                store_ptr(
                    prepared_zpage,
                    56,
                    global_load_ptr("pcc_gc_backend4_free_page_head"),
                )
                global_store_ptr(
                    "pcc_gc_backend4_free_page_head", prepared_zpage
                )
                prepared_zpage = null()
            _object_graph_unlock()
            free(prepared_node)
            free(prepared_slots)
            free(prepared_zpage_node)
            free(prepared_zpage_slots)
            if ptr_is_null(prepared_zpage) == 0:
                free(load_ptr(prepared_zpage, 72))
                free(prepared_zpage)
            _set_pending_minor_block(null())
            global_store_ptr("pcc_gc_last_alloc", o)
            return

        store_ptr(node_owner, 0, prepared_node)
        node = _object_node_take_prepared(node_owner)
        prepared_node = load_ptr(node_owner, 0)
        if ptr_is_null(node) != 0:
            if (
                ptr_is_null(prepared_zpage) == 0
                and prepared_zpage_from_free != 0
            ):
                store_ptr(
                    prepared_zpage,
                    56,
                    global_load_ptr("pcc_gc_backend4_free_page_head"),
                )
                global_store_ptr(
                    "pcc_gc_backend4_free_page_head", prepared_zpage
                )
                prepared_zpage = null()
            _object_graph_unlock()
            free(prepared_node)
            free(prepared_slots)
            free(prepared_zpage_node)
            free(prepared_zpage_slots)
            if ptr_is_null(prepared_zpage) == 0:
                free(load_ptr(prepared_zpage, 72))
                free(prepared_zpage)
            _set_pending_minor_block(null())
            global_store_ptr("pcc_gc_last_alloc", o)
            return
        old_head = _object_head()
        store_ptr(node, 0, o)
        store_i64(node, 8, size)
        store_ptr(node, 16, old_head)
        store_ptr(node, 24, pending_block)
        store_i64(node, 32, 0)
        store_ptr(node, 40, null())
        store_ptr(node, 48, null())
        _set_object_node_gc_refs(node, 0)
        _set_object_node_young_next(node, null())
        _set_object_node_young_prev(node, null())
        if ptr_is_null(old_head) == 0:
            _set_object_node_prev(old_head, node)
        _set_object_head(node)
        index_result: int = pcc_gc_object_index_insert_preallocated(o, node)
        if index_result >= 0:
            if pcc_gc_granule_is_object_start(o) != 1:
                pcc_gc_managed_pointer_index_remove(o)
        final_generation: int = load_i32(o, 12) & 384
        if final_generation == 128:
            _backend3_young_link_head(node)
        live: int = load_i32(global_addr("pcc_gc_live_bytes"), 0)
        store_i32(global_addr("pcc_gc_live_bytes"), 0, live + size)
        if backend == 4:
            store_ptr(zpage_node_owner, 0, prepared_zpage_node)
            store_ptr(zpage_owner, 0, prepared_zpage)
            zpage_node = _backend4_zpage_track_alloc_preallocated(
                o,
                size,
                zpage_node_owner,
                zpage_owner,
                prepared_zpage_from_free,
            )
            prepared_zpage_node = load_ptr(zpage_node_owner, 0)
            prepared_zpage = load_ptr(zpage_owner, 0)
            if ptr_is_null(prepared_zpage) != 0:
                prepared_zpage_from_free = 0
            _set_object_node_zpage(node, zpage_node)
        if (
            ptr_is_null(prepared_zpage) == 0
            and prepared_zpage_from_free != 0
        ):
            store_ptr(
                prepared_zpage,
                56,
                global_load_ptr("pcc_gc_backend4_free_page_head"),
            )
            global_store_ptr(
                "pcc_gc_backend4_free_page_head", prepared_zpage
            )
            prepared_zpage = null()
        _object_graph_unlock()
        free(prepared_node)
        free(prepared_slots)
        free(prepared_zpage_node)
        free(prepared_zpage_slots)
        if ptr_is_null(prepared_zpage) == 0:
            free(load_ptr(prepared_zpage, 72))
            free(prepared_zpage)
        _set_pending_minor_block(null())
        global_store_ptr("pcc_gc_last_alloc", o)
        return


@c_abi_export("pcc_gc_note_object_allocated")
def pcc_gc_note_object_allocated(o) -> None:
    pcc_gc_note_object_allocated_sized(o, 16)


@c_abi_export("pcc_gc_note_object_freeing")
def pcc_gc_note_object_freeing(o) -> None:
    backend: int = 0
    if load_i32(global_addr("pcc_gc_config_initialized"), 0) == 0:
        backend = _init_config()
    else:
        backend = load_i32(global_addr("pcc_gc_backend_selected"), 0)
    if ptr_is_null(o) != 0:
        return
    # The remap-finish struct is written only by the forwarding backends
    # (3/4): every writer below sits under that backend check, so on 0/1/2 it
    # is all-null at every exit and the six-way retirement fan-out is pure
    # per-free overhead.  Gate the fan-out, not the struct init, and keep the
    # C mirror in py_gc_backend.c the same shape.
    moving: int = 0
    if backend == 3 or backend == 4:
        moving = 1
    finish = stack_alloc(48)
    store_ptr(finish, 0, null())
    store_ptr(finish, 8, null())
    store_ptr(finish, 16, null())
    store_ptr(finish, 24, null())
    store_ptr(finish, 32, null())
    store_ptr(finish, 40, null())
    _object_graph_lock()
    if pcc_gc_granule_is_object_start(o) != 1:
        if pcc_gc_managed_pointer_index_insert(o) < 0:
            _object_graph_unlock()
            if moving != 0:
                _backend4_finish_remap_retirement(finish)
            return
    if moving != 0:
        _forwarding_detach_into_finish(o, finish)
        _forwarding_remove_target(o, finish)
    _identity_remove(o)
    if backend == 4:
        _relocation_set_remove(o)
        _backend4_store_buffer_remove(o)
        _backend4_remembered_set_remove(o)
        zpage_flags: int = load_i32(o, 12) & 65536
        zpage_owner_node = pcc_gc_object_index_find(o)
        zpage_indexed: int = 0
        if ptr_is_null(zpage_owner_node) == 0:
            if ptr_is_null(_object_node_zpage(zpage_owner_node)) == 0:
                zpage_indexed = 1
        # Header origin plus the O(1) object index are authoritative; a
        # per-release scan of live/free/retained zpage lists is not.
        if zpage_flags != 0 or zpage_indexed != 0:
            if zpage_flags == 0:
                store_i32(o, 12, load_i32(o, 12) | 65536)
            _backend4_zpage_remove(o)
    if _gc_tracks_objects() == 0:
        _object_graph_unlock()
        if moving != 0:
            _backend4_finish_remap_retirement(finish)
        return
    node = pcc_gc_object_index_find(o)
    if ptr_is_null(node) == 0:
        if _object_node_freeing(node) == 0:
            _live_bytes_subtract(_object_node_size(node))
        _set_object_node_freeing(node, 1)
        if ptr_is_null(_object_node_minor_block(node)) == 0:
            _object_graph_unlock()
            if moving != 0:
                _backend4_finish_remap_retirement(finish)
            return
        pcc_gc_object_index_remove(o)
        _unlink_object_node(node)
        _object_node_release(node)
        _object_graph_unlock()
        if moving != 0:
            _backend4_finish_remap_retirement(finish)
        return
    last = global_load_ptr("pcc_gc_last_alloc")
    if ptr_eq(last, o) != 0:
        global_store_ptr("pcc_gc_last_alloc", null())
    _object_graph_unlock()
    if moving != 0:
        _backend4_finish_remap_retirement(finish)


@c_abi_export("pcc_gc_reset_relocation_set")
def pcc_gc_reset_relocation_set() -> None:
    _init_config()
    owner: int = pcc_current_thread_id()
    if owner <= 0:
        return
    while 1:
        _object_graph_lock()
        reset_owner: int = load_i64(
            global_addr("pcc_gc_backend4_relocation_reset_owner"), 0
        )
        if reset_owner == 0:
            store_i64(
                global_addr("pcc_gc_backend4_relocation_reset_owner"),
                0,
                owner,
            )
            break
        if reset_owner == owner:
            _object_graph_unlock()
            return
        _object_graph_unlock()
        pcc_thread_safepoint()

    while 1:
        batch = null()
        examined: int = 0
        while ptr_is_null(_relocation_set_head()) == 0 and examined < 16:
            node = _relocation_set_head()
            _set_relocation_set_head(load_ptr(node, 8))
            if ptr_eq(
                global_load_ptr("pcc_gc_backend4_reseed_relocation_cursor"),
                node,
            ) != 0:
                global_store_ptr(
                    "pcc_gc_backend4_reseed_relocation_cursor",
                    load_ptr(node, 8),
                )
            revision: int = load_i64(
                global_addr("pcc_gc_backend4_reseed_relocation_revision"), 0
            )
            store_i64(
                global_addr("pcc_gc_backend4_reseed_relocation_revision"),
                0,
                revision + 1,
            )
            store_ptr(node, 8, batch)
            batch = node
            obj = load_ptr(node, 0)
            if ptr_is_null(obj) == 0:
                if is_tagged_int(obj) == 0:
                    if ptr_is_null(_forwarding_find(obj)) != 0:
                        flags: int = load_i32(obj, 12)
                        store_i32(obj, 12, flags & ~2048)
            examined = examined + 1
        complete: int = ptr_is_null(_relocation_set_head())
        _object_graph_unlock()
        _relocation_reset_finish(batch, null())
        if complete != 0:
            break
        pcc_thread_safepoint()
        _object_graph_lock()

    _object_graph_lock()
    while 1:
        page_batch = null()
        examined = 0
        while ptr_is_null(_evacuation_page_head()) == 0 and examined < 16:
            page_node = _evacuation_page_head()
            _set_evacuation_page_head(load_ptr(page_node, 8))
            if ptr_eq(
                global_load_ptr("pcc_gc_backend4_reseed_page_count_cursor"),
                page_node,
            ) != 0:
                global_store_ptr(
                    "pcc_gc_backend4_reseed_page_count_cursor",
                    load_ptr(page_node, 8),
                )
            revision = load_i64(
                global_addr("pcc_gc_backend4_reseed_page_revision"), 0
            )
            store_i64(
                global_addr("pcc_gc_backend4_reseed_page_revision"),
                0,
                revision + 1,
            )
            store_ptr(page_node, 8, page_batch)
            page_batch = page_node
            page = load_ptr(page_node, 0)
            if ptr_is_null(page) == 0:
                store_i32(page, 108, 0)
            examined = examined + 1
        complete = ptr_is_null(_evacuation_page_head())
        _object_graph_unlock()
        _backend4_evacuation_page_finish_detached(page_batch)
        if complete != 0:
            break
        pcc_thread_safepoint()
        _object_graph_lock()

    _object_graph_lock()
    global_store_ptr("pcc_gc_backend4_reset_object_cursor", _object_head())
    while 1:
        examined = 0
        while (
            ptr_is_null(
                global_load_ptr("pcc_gc_backend4_reset_object_cursor")
            ) == 0
            and examined < 16
        ):
            obj_node = global_load_ptr(
                "pcc_gc_backend4_reset_object_cursor"
            )
            global_store_ptr(
                "pcc_gc_backend4_reset_object_cursor",
                _object_node_next(obj_node),
            )
            obj = load_ptr(obj_node, 0)
            if ptr_is_null(obj) == 0:
                if is_tagged_int(obj) == 0:
                    flags = load_i32(obj, 12)
                    store_i32(obj, 12, flags & ~8192)
            examined = examined + 1
        if ptr_is_null(
            global_load_ptr("pcc_gc_backend4_reset_object_cursor")
        ) != 0:
            store_i32(
                global_addr("pcc_gc_backend4_evacuation_candidates"), 0, 0
            )
            store_i32(
                global_addr(
                    "pcc_gc_backend4_evacuation_candidate_bytes_count"
                ),
                0,
                0,
            )
            store_i32(
                global_addr("pcc_gc_backend4_small_page_candidates"), 0, 0
            )
            store_i32(
                global_addr("pcc_gc_backend4_medium_page_candidates"), 0, 0
            )
            store_i32(
                global_addr(
                    "pcc_gc_backend4_small_page_candidate_bytes_count"
                ),
                0,
                0,
            )
            store_i32(
                global_addr(
                    "pcc_gc_backend4_medium_page_candidate_bytes_count"
                ),
                0,
                0,
            )
            store_i32(
                global_addr(
                    "pcc_gc_backend4_evacuation_candidate_zpage_bytes_count"
                ),
                0,
                0,
            )
            store_i32(
                global_addr(
                    "pcc_gc_backend4_small_page_candidate_zpage_bytes_count"
                ),
                0,
                0,
            )
            store_i32(
                global_addr(
                    "pcc_gc_backend4_medium_page_candidate_zpage_bytes_count"
                ),
                0,
                0,
            )
            store_i64(
                global_addr("pcc_gc_backend4_relocation_reset_owner"), 0, 0
            )
            _object_graph_unlock()
            return
        _object_graph_unlock()
        pcc_thread_safepoint()
        _object_graph_lock()


def _relocation_reset_finish(relocation_nodes, evacuation_nodes) -> None:
    node = relocation_nodes
    while ptr_is_null(node) == 0:
        nxt = load_ptr(node, 8)
        free(node)
        node = nxt
    _backend4_evacuation_page_finish_detached(evacuation_nodes)


@c_abi_export("pcc_gc_relocation_set_contains")
def pcc_gc_relocation_set_contains(o) -> int:
    _init_config()
    _object_graph_lock()
    if ptr_is_null(_relocation_set_find(o)) == 0:
        _object_graph_unlock()
        return 1
    _object_graph_unlock()
    return 0


@c_abi_export("pcc_gc_relocation_set_size")
def pcc_gc_relocation_set_size() -> int:
    _init_config()
    _object_graph_lock()
    size: int = 0
    node = _relocation_set_head()
    while ptr_is_null(node) == 0:
        size = size + 1
        node = load_ptr(node, 8)
    _object_graph_unlock()
    return size


def _backend_uses_forwarding() -> int:
    backend: int = load_i32(global_addr("pcc_gc_backend_selected"), 0)
    if backend == 3 or backend == 4:
        return 1
    return 0


def _backend4_clear_large_deferred_flags() -> None:
    _object_graph_lock()
    node = _object_head()
    while ptr_is_null(node) == 0:
        if _object_node_is_active(node) != 0:
            obj = load_ptr(node, 0)
            if ptr_is_null(obj) == 0:
                if is_tagged_int(obj) == 0:
                    flags: int = load_i32(obj, 12)
                    if (flags & 32768) != 0:
                        reconsidered: int = load_i32(
                            global_addr(
                                "pcc_gc_backend4_large_object_reconsiderations_count"
                            ),
                            0,
                        )
                        store_i32(
                            global_addr(
                                "pcc_gc_backend4_large_object_reconsiderations_count"
                            ),
                            0,
                            reconsidered + 1,
                        )
                        store_i32(obj, 12, flags & ~32768)
        node = _object_node_next(node)
    _object_graph_unlock()


@c_abi_export("pcc_gc_backend4_reseed_plan_probe_config")
def pcc_gc_backend4_reseed_plan_probe_config(
    pause: int, allocation_limit: int
) -> None:
    atomic_store_i64(
        global_addr("pcc_gc_backend4_reseed_plan_probe_allocation_limit"),
        0,
        allocation_limit,
        "release",
    )
    atomic_store_i64(
        global_addr("pcc_gc_backend4_reseed_plan_probe_pause"),
        0,
        pause != 0,
        "release",
    )


@c_abi_export("pcc_gc_backend4_reseed_plan_probe_state")
def pcc_gc_backend4_reseed_plan_probe_state() -> int:
    return atomic_load_i64(
        global_addr("pcc_gc_backend4_reseed_plan_probe_state_value"),
        0,
        "acquire",
    )


def _backend4_reseed_plan_probe_wait(phase: int) -> None:
    if (atomic_load_i64(
        global_addr("pcc_gc_backend4_reseed_plan_probe_pause"), 0, "acquire"
    ) & phase) == 0:
        return
    atomic_store_i64(
        global_addr("pcc_gc_backend4_reseed_plan_probe_state_value"),
        0,
        1,
        "release",
    )
    while (atomic_load_i64(
        global_addr("pcc_gc_backend4_reseed_plan_probe_pause"), 0, "acquire"
    ) & phase) != 0:
        pcc_thread_safepoint()
    atomic_store_i64(
        global_addr("pcc_gc_backend4_reseed_plan_probe_state_value"),
        0,
        0,
        "release",
    )


def _backend4_reseed_relocation_epoch_state() -> None:
    if pcc_gc_backend() != 4:
        return
    owner: int = pcc_current_thread_id()
    if owner <= 0:
        return
    prepared_nodes = null()
    prepared_count: int = 0
    while 1:
        _object_graph_lock()
        count_owner: int = load_i64(
            global_addr("pcc_gc_backend4_reseed_page_count_owner"), 0
        )
        if count_owner == 0:
            store_i64(
                global_addr("pcc_gc_backend4_reseed_page_count_owner"),
                0,
                owner,
            )
            break
        if count_owner == owner:
            _object_graph_unlock()
            return
        _object_graph_unlock()
        pcc_thread_safepoint()
    while 1:
        required: int = 0
        observed_revision: int = load_i64(
            global_addr("pcc_gc_backend4_reseed_page_revision"), 0
        )
        global_store_ptr(
            "pcc_gc_backend4_reseed_page_count_cursor",
            _evacuation_page_head(),
        )
        while 1:
            examined: int = 0
            while (
                ptr_is_null(
                    global_load_ptr(
                        "pcc_gc_backend4_reseed_page_count_cursor"
                    )
                ) == 0
                and examined < 16
            ):
                page_node = global_load_ptr(
                    "pcc_gc_backend4_reseed_page_count_cursor"
                )
                global_store_ptr(
                    "pcc_gc_backend4_reseed_page_count_cursor",
                    load_ptr(page_node, 8),
                )
                required = required + 1
                examined = examined + 1
            complete: int = ptr_is_null(
                global_load_ptr("pcc_gc_backend4_reseed_page_count_cursor")
            )
            revision: int = load_i64(
                global_addr("pcc_gc_backend4_reseed_page_revision"), 0
            )
            if revision != observed_revision:
                required = 0
                observed_revision = revision
                global_store_ptr(
                    "pcc_gc_backend4_reseed_page_count_cursor",
                    _evacuation_page_head(),
                )
                complete = 0
            if complete != 0:
                break
            _object_graph_unlock()
            _backend4_reseed_plan_probe_wait(1)
            pcc_thread_safepoint()
            _object_graph_lock()
        if required > prepared_count:
            _object_graph_unlock()
            _backend4_reseed_plan_probe_wait(1)
            prepared_nodes = _backend4_evacuation_page_nodes_prepare(
                prepared_nodes, required - prepared_count
            )
            prepared_count = _backend4_evacuation_page_nodes_count(
                prepared_nodes
            )
            if prepared_count < required:
                _object_graph_lock()
                global_store_ptr(
                    "pcc_gc_backend4_reseed_page_count_cursor", null()
                )
                global_store_ptr(
                    "pcc_gc_backend4_reseed_relocation_cursor", null()
                )
                store_i64(
                    global_addr("pcc_gc_backend4_reseed_commit_owner"),
                    0,
                    0,
                )
                store_i64(
                    global_addr("pcc_gc_backend4_reseed_page_count_owner"),
                    0,
                    0,
                )
                _object_graph_unlock()
                _backend4_evacuation_page_finish_detached(prepared_nodes)
                return
            _object_graph_lock()
            continue

        while 1:
            candidates: int = 0
            candidate_bytes: int = 0
            small_candidates: int = 0
            medium_candidates: int = 0
            small_bytes: int = 0
            medium_bytes: int = 0
            zpage_bytes: int = 0
            small_zpage_bytes: int = 0
            medium_zpage_bytes: int = 0
            observed_relocation_revision: int = load_i64(
                global_addr("pcc_gc_backend4_reseed_relocation_revision"), 0
            )
            global_store_ptr(
                "pcc_gc_backend4_reseed_relocation_cursor",
                _relocation_set_head(),
            )
            while 1:
                examined = 0
                while (
                    ptr_is_null(
                        global_load_ptr(
                            "pcc_gc_backend4_reseed_relocation_cursor"
                        )
                    ) == 0
                    and examined < 16
                ):
                    rel_node = global_load_ptr(
                        "pcc_gc_backend4_reseed_relocation_cursor"
                    )
                    global_store_ptr(
                        "pcc_gc_backend4_reseed_relocation_cursor",
                        load_ptr(rel_node, 8),
                    )
                    obj = load_ptr(rel_node, 0)
                    size: int = _object_known_size(obj)
                    if size > 0:
                        candidates = candidates + 1
                        candidate_bytes = candidate_bytes + size
                        if size <= 4096:
                            small_candidates = small_candidates + 1
                            small_bytes = small_bytes + size
                        elif size <= 65536:
                            medium_candidates = medium_candidates + 1
                            medium_bytes = medium_bytes + size
                    examined = examined + 1
                complete = ptr_is_null(
                    global_load_ptr(
                        "pcc_gc_backend4_reseed_relocation_cursor"
                    )
                )
                revision = load_i64(
                    global_addr(
                        "pcc_gc_backend4_reseed_relocation_revision"
                    ),
                    0,
                )
                if revision != observed_relocation_revision:
                    candidates = 0
                    candidate_bytes = 0
                    small_candidates = 0
                    medium_candidates = 0
                    small_bytes = 0
                    medium_bytes = 0
                    observed_relocation_revision = revision
                    global_store_ptr(
                        "pcc_gc_backend4_reseed_relocation_cursor",
                        _relocation_set_head(),
                    )
                    complete = 0
                if complete != 0:
                    break
                _object_graph_unlock()
                _backend4_reseed_plan_probe_wait(2)
                pcc_thread_safepoint()
                _object_graph_lock()

            # Freeze candidate admission and relocation commit while scanning
            # the authoritative evacuation list.  The unlink paths repair the
            # cursor before recycling a node/page, and raw page locals are
            # cleared before every unlock.
            store_i64(
                global_addr("pcc_gc_backend4_reseed_commit_owner"),
                0,
                owner,
            )
            observed_page_revision: int = load_i64(
                global_addr("pcc_gc_backend4_reseed_page_revision"), 0
            )
            observed_commit_relocation_revision: int = load_i64(
                global_addr("pcc_gc_backend4_reseed_relocation_revision"), 0
            )
            restart_commit: int = 0
            global_store_ptr(
                "pcc_gc_backend4_reseed_page_count_cursor",
                _evacuation_page_head(),
            )
            while 1:
                examined = 0
                page_node = null()
                page = null()
                while (
                    ptr_is_null(
                        global_load_ptr(
                            "pcc_gc_backend4_reseed_page_count_cursor"
                        )
                    ) == 0
                    and examined < 16
                ):
                    page_node = global_load_ptr(
                        "pcc_gc_backend4_reseed_page_count_cursor"
                    )
                    global_store_ptr(
                        "pcc_gc_backend4_reseed_page_count_cursor",
                        load_ptr(page_node, 8),
                    )
                    page = load_ptr(page_node, 0)
                    if ptr_is_null(page) == 0:
                        page_bytes: int = load_i64(page, 8)
                        page_class: int = load_i32(page, 24)
                        if page_bytes > 0:
                            zpage_bytes = zpage_bytes + page_bytes
                            if page_class == 0:
                                small_zpage_bytes = (
                                    small_zpage_bytes + page_bytes
                                )
                            elif page_class == 1:
                                medium_zpage_bytes = (
                                    medium_zpage_bytes + page_bytes
                                )
                    examined = examined + 1
                page_node = null()
                page = null()
                complete = ptr_is_null(
                    global_load_ptr(
                        "pcc_gc_backend4_reseed_page_count_cursor"
                    )
                )
                revision = load_i64(
                    global_addr("pcc_gc_backend4_reseed_page_revision"), 0
                )
                relocation_revision: int = load_i64(
                    global_addr(
                        "pcc_gc_backend4_reseed_relocation_revision"
                    ),
                    0,
                )
                reset_owner: int = load_i64(
                    global_addr("pcc_gc_backend4_relocation_reset_owner"), 0
                )
                if (
                    revision != observed_page_revision
                    or relocation_revision
                    != observed_commit_relocation_revision
                    or reset_owner != 0
                ):
                    restart_commit = 1
                    break
                if complete != 0:
                    break
                _object_graph_unlock()
                _backend4_reseed_plan_probe_wait(4)
                pcc_thread_safepoint()
                _object_graph_lock()
            if restart_commit != 0:
                global_store_ptr(
                    "pcc_gc_backend4_reseed_page_count_cursor", null()
                )
                global_store_ptr(
                    "pcc_gc_backend4_reseed_relocation_cursor", null()
                )
                _object_graph_unlock()
                pcc_thread_safepoint()
                _object_graph_lock()
                continue

            store_i32(
                global_addr("pcc_gc_backend4_evacuation_candidates"),
                0,
                candidates,
            )
            store_i32(
                global_addr(
                    "pcc_gc_backend4_evacuation_candidate_bytes_count"
                ),
                0,
                candidate_bytes,
            )
            store_i32(
                global_addr("pcc_gc_backend4_small_page_candidates"),
                0,
                small_candidates,
            )
            store_i32(
                global_addr("pcc_gc_backend4_medium_page_candidates"),
                0,
                medium_candidates,
            )
            store_i32(
                global_addr(
                    "pcc_gc_backend4_small_page_candidate_bytes_count"
                ),
                0,
                small_bytes,
            )
            store_i32(
                global_addr(
                    "pcc_gc_backend4_medium_page_candidate_bytes_count"
                ),
                0,
                medium_bytes,
            )
            store_i32(
                global_addr(
                    "pcc_gc_backend4_evacuation_candidate_zpage_bytes_count"
                ),
                0,
                zpage_bytes,
            )
            store_i32(
                global_addr(
                    "pcc_gc_backend4_small_page_candidate_zpage_bytes_count"
                ),
                0,
                small_zpage_bytes,
            )
            store_i32(
                global_addr(
                    "pcc_gc_backend4_medium_page_candidate_zpage_bytes_count"
                ),
                0,
                medium_zpage_bytes,
            )
            global_store_ptr(
                "pcc_gc_backend4_reseed_page_count_cursor", null()
            )
            global_store_ptr(
                "pcc_gc_backend4_reseed_relocation_cursor", null()
            )
            store_i64(
                global_addr("pcc_gc_backend4_reseed_commit_owner"), 0, 0
            )
            store_i64(
                global_addr("pcc_gc_backend4_reseed_page_count_owner"), 0, 0
            )
            _object_graph_unlock()
            _backend4_evacuation_page_finish_detached(prepared_nodes)
            return


@c_abi_export("pcc_gc_backend4_evacuation_page_find")
def _backend4_evacuation_page_find(page):
    if ptr_is_null(page) != 0:
        return null()
    node = _evacuation_page_head()
    while ptr_is_null(node) == 0:
        if ptr_eq(load_ptr(node, 0), page) != 0:
            return node
        node = load_ptr(node, 8)
    return null()


@c_abi_export("pcc_gc_backend4_evacuation_page_add")
def _backend4_evacuation_page_add(page) -> int:
    if ptr_is_null(page) != 0:
        return 0
    if load_i32(page, 108) != 0:
        return 0
    _backend4_clear_active_page(page)
    node = malloc(16)
    if ptr_is_null(node) != 0:
        return 0
    store_ptr(node, 0, page)
    store_ptr(node, 8, _evacuation_page_head())
    _set_evacuation_page_head(node)
    store_i32(page, 108, 1)
    revision: int = load_i64(
        global_addr("pcc_gc_backend4_reseed_page_revision"), 0
    )
    store_i64(
        global_addr("pcc_gc_backend4_reseed_page_revision"),
        0,
        revision + 1,
    )
    return 1


@c_abi_export("pcc_gc_backend4_evacuation_page_remove")
def _backend4_evacuation_page_remove(page) -> None:
    if ptr_is_null(page) != 0:
        return
    prev = null()
    node = _evacuation_page_head()
    while ptr_is_null(node) == 0:
        nxt = load_ptr(node, 8)
        if ptr_eq(load_ptr(node, 0), page) != 0:
            if ptr_is_null(prev) != 0:
                _set_evacuation_page_head(nxt)
            else:
                store_ptr(prev, 8, nxt)
            if ptr_eq(
                global_load_ptr("pcc_gc_backend4_reseed_page_count_cursor"),
                node,
            ) != 0:
                global_store_ptr(
                    "pcc_gc_backend4_reseed_page_count_cursor", nxt
                )
            revision: int = load_i64(
                global_addr("pcc_gc_backend4_reseed_page_revision"), 0
            )
            store_i64(
                global_addr("pcc_gc_backend4_reseed_page_revision"),
                0,
                revision + 1,
            )
            store_i32(page, 108, 0)
            free(node)
            return
        prev = node
        node = nxt


def _backend4_evacuation_page_nodes_count(node) -> int:
    count: int = 0
    while ptr_is_null(node) == 0:
        count = count + 1
        node = load_ptr(node, 8)
    return count


def _backend4_evacuation_page_nodes_prepare(head, capacity: int):
    while capacity > 0:
        allocation_limit: int = atomic_load_i64(
            global_addr("pcc_gc_backend4_reseed_plan_probe_allocation_limit"),
            0,
            "acquire",
        )
        if allocation_limit == 0:
            return head
        node = malloc(16)
        if ptr_is_null(node) != 0:
            return head
        store_ptr(node, 0, null())
        store_ptr(node, 8, head)
        head = node
        capacity = capacity - 1
        if allocation_limit > 0:
            atomic_store_i64(
                global_addr(
                    "pcc_gc_backend4_reseed_plan_probe_allocation_limit"
                ),
                0,
                allocation_limit - 1,
                "release",
            )
    return head


def _backend4_evacuation_page_add_preallocated(page, available) -> int:
    if ptr_is_null(page) != 0 or ptr_is_null(available) != 0:
        return 0
    if load_i32(page, 108) != 0:
        return 0
    node = load_ptr(available, 0)
    if ptr_is_null(node) != 0:
        return 0
    store_ptr(available, 0, load_ptr(node, 8))
    _backend4_clear_active_page(page)
    store_ptr(node, 0, page)
    store_ptr(node, 8, _evacuation_page_head())
    _set_evacuation_page_head(node)
    store_i32(page, 108, 1)
    revision: int = load_i64(
        global_addr("pcc_gc_backend4_reseed_page_revision"), 0
    )
    store_i64(
        global_addr("pcc_gc_backend4_reseed_page_revision"),
        0,
        revision + 1,
    )
    return 1


def _backend4_evacuation_page_finish_detached(node) -> None:
    while ptr_is_null(node) == 0:
        nxt = load_ptr(node, 8)
        free(node)
        node = nxt


@c_abi_export("pcc_gc_backend4_relocation_set_contains_page")
def _backend4_relocation_set_contains_page(page) -> int:
    if ptr_is_null(page) != 0:
        return 0
    rel = _relocation_set_head()
    while ptr_is_null(rel) == 0:
        obj = load_ptr(rel, 0)
        node = _backend4_zpage_find(obj)
        if ptr_is_null(node) == 0 and ptr_eq(load_ptr(node, 8), page) != 0:
            return 1
        rel = load_ptr(rel, 8)
    return 0


@c_abi_export("pcc_gc_backend4_zpage_page_for_owner")
def _backend4_zpage_page_for_owner(owner):
    if ptr_is_null(owner) != 0:
        return null()
    node = _backend4_zpage_find(owner)
    if ptr_is_null(node) != 0:
        return null()
    return load_ptr(node, 8)


@c_abi_export("pcc_gc_note_load")
def pcc_gc_note_load() -> None:
    slot = global_addr("pcc_gc_metric_load")
    v: int = load_i32(slot, 0)
    store_i32(slot, 0, v + 1)


@c_abi_export("pcc_gc_note_store")
def pcc_gc_note_store() -> None:
    slot = global_addr("pcc_gc_metric_store")
    v: int = load_i32(slot, 0)
    store_i32(slot, 0, v + 1)


@c_abi_export("pcc_gc_note_safepoint")
def pcc_gc_note_safepoint() -> None:
    _counter_inc(3, 1)


@c_abi_export("pcc_gc_note_pin")
def pcc_gc_note_pin(delta: int) -> None:
    _counter_inc(4, delta)


def _scheduler_root_node_alloc(slot):
    if ptr_is_null(slot) != 0:
        return null()
    node = malloc(24)
    if ptr_is_null(node) != 0:
        return null()
    memset(node, 0, 24)
    store_ptr(node, 0, slot)
    return node


def _scheduler_root_node_free(node) -> None:
    if ptr_is_null(node) == 0:
        free(node)


def _scheduler_queue_entry_free(entry) -> None:
    if ptr_is_null(entry):
        return
    backend: int = pcc_gc_backend()
    clear_plan = stack_alloc(128)
    pcc_gc_store_root_plan_init(clear_plan, backend)
    root_node = load_ptr(entry, 16)
    _object_graph_lock()
    barrier_before: int = load_i32(global_addr("pcc_gc_relocation_barrier_forwards"), 0)
    had_value: int = 0
    if ptr_is_null(load_ptr(entry, 0)) == 0:
        had_value = 1
    pcc_gc_store_root_plan_commit_locked(clear_plan, entry, null())
    if backend == 4:
        if had_value != 0:
            if load_i32(global_addr("pcc_gc_relocation_forwards"), 0) > 0:
                if (
                    load_i32(global_addr("pcc_gc_relocation_barrier_forwards"), 0)
                    == barrier_before
                ):
                    store_i32(
                        global_addr("pcc_gc_relocation_barrier_forwards"),
                        0,
                        barrier_before + 1,
                    )
    pcc_gc_scheduler_root_unlink_locked(root_node)
    store_ptr(entry, 16, null())
    _object_graph_unlock()
    if ptr_is_null(root_node) == 0:
        pcc_gc_cycle_requested_store_release(1)
        _scheduler_root_node_free(root_node)
    free(entry)
    pcc_gc_store_root_plan_finish(clear_plan)


def _scheduler_queue_entry_alloc(queue):
    entry = null()
    if ptr_is_null(queue) == 0:
        mutex = load_ptr(queue, 0)
        if ptr_is_null(mutex) == 0 and pcc_mutex_lock(mutex) == 0:
            entry = load_ptr(queue, 32)
            if ptr_is_null(entry) == 0:
                store_ptr(queue, 32, load_ptr(entry, 8))
                count: int = load_i64(queue, 40)
                if count > 0:
                    store_i64(queue, 40, count - 1)
            pcc_mutex_unlock(mutex)
    if ptr_is_null(entry) != 0:
        entry = malloc(24)
    if ptr_is_null(entry) == 0:
        memset(entry, 0, 24)
    return entry


def _scheduler_queue_entry_recycle(queue, entry) -> None:
    if ptr_is_null(entry) != 0:
        return
    memset(entry, 0, 24)
    if ptr_is_null(queue) != 0:
        free(entry)
        return
    mutex = load_ptr(queue, 0)
    if ptr_is_null(mutex) != 0 or pcc_mutex_lock(mutex) != 0:
        free(entry)
        return
    count: int = load_i64(queue, 40)
    # 4096 == C #define PCC_GC_SCHEDULER_QUEUE_ENTRY_POOL_LIMIT; inlined because a
    # module-level const emits a `.modvar.` global that is zeroed in the stripped
    # runtime-library .o build (see test_runtime_substrate_spike).
    if count >= 4096:
        pcc_mutex_unlock(mutex)
        free(entry)
        return
    store_ptr(entry, 8, load_ptr(queue, 32))
    store_ptr(queue, 32, entry)
    store_i64(queue, 40, count + 1)
    pcc_mutex_unlock(mutex)


def _scheduler_queue_entry_release(queue, entry) -> None:
    if ptr_is_null(entry) != 0:
        return
    clear_plan = stack_alloc(128)
    pcc_gc_store_root_plan_init(clear_plan, pcc_gc_backend())
    root_node = load_ptr(entry, 16)
    _object_graph_lock()
    pcc_gc_store_root_plan_commit_locked(clear_plan, entry, null())
    pcc_gc_scheduler_root_unlink_locked(root_node)
    store_ptr(entry, 16, null())
    _object_graph_unlock()
    if ptr_is_null(root_node) == 0:
        pcc_gc_cycle_requested_store_release(1)
        _scheduler_root_node_free(root_node)
    _scheduler_queue_entry_recycle(queue, entry)
    pcc_gc_store_root_plan_finish(clear_plan)


@c_abi_export("pcc_gc_scheduler_queue_new")
def pcc_gc_scheduler_queue_new():
    queue = malloc(48)
    if ptr_is_null(queue):
        return null()
    mutex = pcc_mutex_new()
    if ptr_is_null(mutex):
        free(queue)
        return null()
    store_ptr(queue, 0, mutex)
    store_ptr(queue, 8, null())  # head
    store_ptr(queue, 16, null())  # tail
    store_i64(queue, 24, 0)  # length
    store_ptr(queue, 32, null())  # free_head
    store_i64(queue, 40, 0)  # free_count
    return queue


@c_abi_export("pcc_gc_scheduler_queue_free")
def pcc_gc_scheduler_queue_free(queue) -> None:
    if ptr_is_null(queue):
        return
    mutex = load_ptr(queue, 0)
    if ptr_is_null(mutex) == 0:
        pcc_mutex_lock(mutex)
    entry = load_ptr(queue, 8)
    store_ptr(queue, 8, null())
    store_ptr(queue, 16, null())
    store_i64(queue, 24, 0)
    if ptr_is_null(mutex) == 0:
        pcc_mutex_unlock(mutex)
    while ptr_is_null(entry) == 0:
        nxt = load_ptr(entry, 8)
        _scheduler_queue_entry_free(entry)
        entry = nxt
    entry = load_ptr(queue, 32)
    while ptr_is_null(entry) == 0:
        nxt = load_ptr(entry, 8)
        free(entry)
        entry = nxt
    store_ptr(queue, 32, null())
    store_i64(queue, 40, 0)
    if ptr_is_null(mutex) == 0:
        pcc_mutex_free(mutex)
    free(queue)


@c_abi_export("pcc_gc_scheduler_queue_push")
def pcc_gc_scheduler_queue_push(queue, value) -> int:
    if ptr_is_null(queue):
        return -1
    entry = _scheduler_queue_entry_alloc(queue)
    if ptr_is_null(entry):
        return -1
    root_node = _scheduler_root_node_alloc(entry)
    if ptr_is_null(root_node) != 0:
        _scheduler_queue_entry_recycle(queue, entry)
        return -1
    store_plan = stack_alloc(128)
    pcc_gc_store_root_plan_init(store_plan, pcc_gc_backend())
    _object_graph_lock()
    published: int = pcc_gc_store_root_plan_commit_locked(
        store_plan, entry, value
    )
    if published != 0:
        store_ptr(entry, 16, root_node)
        pcc_gc_scheduler_root_link_locked(root_node)
    _object_graph_unlock()
    if published != 0:
        pcc_gc_cycle_requested_store_release(1)
    if published == 0:
        _scheduler_root_node_free(root_node)
        _scheduler_queue_entry_recycle(queue, entry)
    pcc_gc_store_root_plan_finish(store_plan)
    if published == 0:
        return -1
    mutex = load_ptr(queue, 0)
    if pcc_mutex_lock(mutex) != 0:
        _scheduler_queue_entry_release(queue, entry)
        return -1
    tail = load_ptr(queue, 16)
    if ptr_is_null(tail):
        store_ptr(queue, 8, entry)
        store_ptr(queue, 16, entry)
    else:
        store_ptr(tail, 8, entry)
        store_ptr(queue, 16, entry)
    store_i64(queue, 24, load_i64(queue, 24) + 1)
    return pcc_mutex_unlock(mutex)


@c_abi_export("pcc_gc_scheduler_queue_pop_into")
def pcc_gc_scheduler_queue_pop_into(queue, out_slot) -> int:
    if ptr_is_null(queue):
        return -1
    mutex = load_ptr(queue, 0)
    if pcc_mutex_lock(mutex) != 0:
        return -1
    entry = load_ptr(queue, 8)
    if ptr_is_null(entry):
        pcc_mutex_unlock(mutex)
        return 0
    nxt = load_ptr(entry, 8)
    store_ptr(queue, 8, nxt)
    if ptr_is_null(nxt):
        store_ptr(queue, 16, null())
    store_i64(queue, 24, load_i64(queue, 24) - 1)
    pcc_mutex_unlock(mutex)
    backend: int = pcc_gc_backend()
    out_plan = stack_alloc(128)
    if ptr_is_null(out_slot) == 0:
        pcc_gc_store_root_plan_init(out_plan, backend)
    clear_plan = stack_alloc(128)
    pcc_gc_store_root_plan_init(clear_plan, backend)
    root_node = load_ptr(entry, 16)
    _object_graph_lock()
    value = load_ptr(entry, 0)
    if ptr_is_null(out_slot) == 0:
        pcc_gc_store_root_plan_commit_locked(
            out_plan, out_slot, value
        )
    pcc_gc_store_root_plan_commit_locked(clear_plan, entry, null())
    pcc_gc_scheduler_root_unlink_locked(root_node)
    store_ptr(entry, 16, null())
    _object_graph_unlock()
    if ptr_is_null(root_node) == 0:
        pcc_gc_cycle_requested_store_release(1)
        _scheduler_root_node_free(root_node)
    _scheduler_queue_entry_recycle(queue, entry)
    if ptr_is_null(out_slot) == 0:
        pcc_gc_store_root_plan_finish(out_plan)
    pcc_gc_store_root_plan_finish(clear_plan)
    return 1


@c_abi_export("pcc_gc_scheduler_queue_len")
def pcc_gc_scheduler_queue_len(queue) -> int:
    if ptr_is_null(queue):
        return 0
    mutex = load_ptr(queue, 0)
    if pcc_mutex_lock(mutex) != 0:
        return -1
    length: int = load_i64(queue, 24)
    pcc_mutex_unlock(mutex)
    return length


@c_abi_export("pcc_gc_thread_unregister_buffers")
def pcc_gc_thread_unregister_buffers() -> None:
    pcc_gc_frame_node_tls_pool_drain()
    # Called from pcc_threads.c::pcc_thread_trampoline on thread exit. The C
    # runtime (py_gc_backend.c) flushes+frees a PER-THREAD backend-4 medium
    # store-buffer state here. The pcc-Python runtime mirror keeps the
    # backend-4 store buffer as GLOBAL state (see _store_buffer_medium_head;
    # there is no per-thread TLS buffer), so there is nothing per-thread to
    # flush or free on thread exit. This mirror MUST exist: pcc_threads.c is
    # always compiled into every archive variant and references this symbol,
    # but for PCC_RUNTIME_HIGH=py the archive uses py_gc_backend.py (not the
    # .c), so without this stub libpy_runtime_pcc_py.a fails to link with
    # `Undefined symbols: _pcc_gc_thread_unregister_buffers`, breaking every
    # pcc1 / high=py test. The freestanding frame registry does own a bounded
    # per-thread cache, and it is drained above before this backend-4 no-op.
    return
