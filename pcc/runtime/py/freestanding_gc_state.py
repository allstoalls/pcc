"""Raw storage for the five production GC backends.

This definition-only module keeps collector state in the strict freestanding
kernel.  Managed GC APIs and telemetry formatting live in their existing
pcc-Python modules and reference these symbols through ``pcc.unsafe``.
"""

from pcc import i64
from pcc.unsafe import define_global_i32, define_global_i64, define_global_ptr_null

__pcc_freestanding__ = True


# Backend-0 cycle collector and public gc-module policy.
define_global_i32("py_gc_enabled", 1)
# CPython 3.15: gc.get_threshold() == (2000, 10, 10).
define_global_i32("py_gc_threshold0", 2000)
define_global_i32("py_gc_threshold1", 10)
define_global_i32("py_gc_threshold2", 10)
define_global_i32("py_gc_freeze_count", 0)
define_global_ptr_null("py_gc_head")
define_global_ptr_null("pcc_gc_deferred_node_free_head")
# Recycled 40-byte tracking nodes (freestanding_gc_tracking.py).  Every
# container/instance allocation tracked one node with malloc and freed it on
# untrack; recycling under the table lock removes that malloc/free pair from
# the alloc/free path.  Collector-side frees keep using free().
define_global_ptr_null("pcc_gc_tracked_node_pool")
define_global_i32("pcc_gc_tracked_node_pool_count", 0)
define_global_i64("pcc_gc_table_lock_owner_token", 0)
define_global_ptr_null("pcc_gc_backend4_parked_head")
define_global_i32("pcc_gc_forwarding_population", 0)
define_global_i32("py_gc_tracked_count", 0)
# Tracked objects left after the last automatic collection (GC0..GC3): the
# next is scheduled once tracking has grown by max(threshold0, this) since.
define_global_i64("pcc_gc_tracked_after_collect", 0)
define_global_i32("py_gc_collecting", 0)
define_global_ptr_null("py_gc_callbacks")
define_global_i32("py_gc_callbacks_firing", 0)

# Shared selector, accounting, pacing, and configuration state.
define_global_i32("pcc_gc_backend_selected", 0)
# Mirror of py_gc_backend.c's pcc_gc_unmanaged_refcount_ops.  i64 because it
# counts every refcount operation that missed provenance, which on a long-lived
# service is not bounded by anything an i32 covers.
define_global_i64("pcc_gc_unmanaged_refcount_ops", 0)
# Mirror of py_gc_backend.c's pcc_gc_refcount_provenance_probe: whether the
# refcount hot path asks pcc_gc_pointer_is_managed before it touches a header.
# 0 trusts the caller (default on the non-moving collectors 0-2), 1 probes and
# counts misses in pcc_gc_unmanaged_refcount_ops (default on the relocating
# collectors 3-4), 2 probes, counts and reports the first miss on stderr, 3
# also aborts on that first miss so the crash report names the site.
# Starts at 1 so a refcount that runs before pcc_gc_config_ensure has read
# PCC_GC_REFCOUNT_PROVENANCE_PROBE keeps the historical check.
define_global_i32("pcc_gc_refcount_provenance_probe", 1)
# 1 while py_incref/py_decref may update an ordinary object's count in place:
# backend 0, no provenance probe and the single-threaded kernel (a plain
# load/store instead of the kernel call).  Set wherever the backend or the
# probe is chosen; 0 before configuration keeps every call on the full path.
define_global_i32("pcc_gc_refcount_fast", 0)
# Backend 0 without the provenance probe: ordinary objects take inline
# atomic reference-count updates (any number of threads), and only a
# count that would reach zero takes the full release path.
define_global_i32("pcc_gc_refcount_atomic", 0)
define_global_i32("pcc_gc_refcount_provenance_probe_reported", 0)
define_global_i32("pcc_gc_metric_alloc", 0)
define_global_i32("pcc_gc_metric_store", 0)
define_global_i32("pcc_gc_metric_load", 0)
define_global_i32("pcc_gc_metric_safepoint", 0)
define_global_i32("pcc_gc_metric_pin", 0)
# Recorded collector-call totals can exceed 2**31 during one compiler link.
# These are independent scalar symbols, not offsets within a packed layout.
define_global_i64("pcc_gc_metric_step", 0)
define_global_i64("pcc_gc_metric_max_pause_us", 0)
define_global_i64("pcc_gc_metric_pause_count", 0)
define_global_i64("pcc_gc_metric_pause_sum_us", 0)
define_global_i32("pcc_gc_metric_pause_hist0", 0)
define_global_i32("pcc_gc_metric_pause_hist1", 0)
define_global_i32("pcc_gc_metric_pause_hist2", 0)
define_global_i32("pcc_gc_metric_pause_hist3", 0)
# Byte accounting must cover a heap and allocation debt beyond 2 GiB.
# The public byte-count APIs are i64; retain that width in raw state too.
define_global_i64("pcc_gc_debt_bytes", 0)
define_global_i32("pcc_gc_backend4_auto_step_pending", 0)
define_global_i32("pcc_gc_last_alloc_bytes", 0)
define_global_i64("pcc_gc_live_bytes", 0)
# Bytes the last completed tracing mark found reachable (Go's heap marked):
# the pacing threshold grows with it, not with garbage awaiting its sweep.
define_global_i64("pcc_gc_tracing_marked_bytes", 0)
define_global_i32("pcc_gc_pause", 1000)
define_global_i32("pcc_gc_stepmul", 10000)
define_global_i64("pcc_gc_debt_threshold_override", 0)
define_global_i32("pcc_gc_config_initialized", 0)
define_global_i32("pcc_gc_read_barrier_enabled", 1)
define_global_i32("pcc_gc_backend0_frame_roots_enabled", 0)
define_global_i32("pcc_gc_in_auto_step", 0)
define_global_i32("pcc_gc_explicit_collect_active", 0)
define_global_i32("pcc_gc_gray_count", 0)

# Backend-2 concurrent and backend-3 generational state.
define_global_i32("pcc_gc_minor_heap_size", 1048576)
define_global_i32("pcc_gc_minor_alloc_max", 256)
define_global_i32("pcc_gc_minor_allocations", 0)
define_global_i32("pcc_gc_minor_collections", 0)
define_global_i32("pcc_gc_minor_bytes", 0)
define_global_i32("pcc_gc_cms_worker_started", 0)
define_global_i32("pcc_gc_cms_worker_stop_requested", 0)
define_global_ptr_null("pcc_gc_cms_worker_handle")
define_global_i32("pcc_gc_cms_worker_starts", 0)
define_global_i32("pcc_gc_cms_queue_pushes", 0)
define_global_i32("pcc_gc_cms_worker_drains", 0)
define_global_i32("pcc_gc_cms_mutator_assists", 0)
define_global_i32("pcc_gc_cms_worker_traces", 0)
define_global_i32("pcc_gc_minor_arena_refills", 0)
define_global_i32("pcc_gc_minor_arena_bumps", 0)
define_global_i32("pcc_gc_minor_arena_fallbacks", 0)
define_global_i32("pcc_gc_cms_worker_stops", 0)
define_global_i32("pcc_gc_cms_wb_flushes", 0)
define_global_ptr_null("pcc_gc_minor_blocks")
define_global_ptr_null("pcc_gc_minor_current")
define_global_ptr_null("pcc_gc_pending_minor_block")

# Backend-4 relocation, page, store-buffer, and remembered-set metrics.
define_global_i32("pcc_gc_relocation_forwards", 0)
define_global_i32("pcc_gc_relocation_barrier_forwards", 0)
define_global_i32("pcc_gc_relocation_pin_rejects", 0)
define_global_i32("pcc_gc_backend4_genzgc_store_barriers", 0)
define_global_i32("pcc_gc_backend4_store_buffer_entries_count", 0)
define_global_i32("pcc_gc_backend4_young_promotions", 0)
define_global_i32("pcc_gc_backend4_evacuation_candidates", 0)
define_global_i32("pcc_gc_backend4_evacuated_bytes_count", 0)
define_global_i32("pcc_gc_backend4_large_object_defers", 0)
define_global_i32("pcc_gc_backend4_large_object_deferred_bytes_count", 0)
define_global_i32("pcc_gc_backend4_large_object_reconsiderations_count", 0)
define_global_i32("pcc_gc_backend4_small_page_candidates", 0)
define_global_i32("pcc_gc_backend4_medium_page_candidates", 0)
define_global_i32("pcc_gc_backend4_evacuation_candidate_bytes_count", 0)
define_global_i32("pcc_gc_backend4_small_page_candidate_bytes_count", 0)
define_global_i32("pcc_gc_backend4_medium_page_candidate_bytes_count", 0)
define_global_i32("pcc_gc_backend4_evacuation_candidate_zpage_bytes_count", 0)
define_global_i32("pcc_gc_backend4_small_page_candidate_zpage_bytes_count", 0)
define_global_i32("pcc_gc_backend4_medium_page_candidate_zpage_bytes_count", 0)
define_global_i32("pcc_gc_backend4_store_buffer_drain_batches_count", 0)
define_global_i32("pcc_gc_backend4_store_buffer_drained_entries_count", 0)
define_global_i32("pcc_gc_backend4_store_buffer_duplicate_skips_count", 0)
define_global_i32("pcc_gc_backend4_store_buffer_high_water_count", 0)
define_global_i32(
    "pcc_gc_backend4_store_buffer_owner_fanout_high_water_count", 0
)
define_global_i32(
    "pcc_gc_backend4_store_buffer_owner_count_high_water_count", 0
)
define_global_i32("pcc_gc_backend4_store_buffer_incomplete_drains_count", 0)
define_global_i32("pcc_gc_backend4_evacuation_incomplete_batches_count", 0)
define_global_i32("pcc_gc_backend4_store_buffer_max_batch_size_count", 0)
define_global_i32("pcc_gc_backend4_store_buffer_full_batches_count", 0)
define_global_i32("pcc_gc_backend4_store_buffer_medium_count", 0)
define_global_i32("pcc_gc_backend4_store_buffer_medium_flushes_count", 0)
define_global_i32(
    "pcc_gc_backend4_store_buffer_medium_flushed_entries_count", 0
)
define_global_i32(
    "pcc_gc_backend4_store_buffer_medium_full_flushes_count", 0
)
define_global_i32(
    "pcc_gc_backend4_store_buffer_cross_thread_medium_flushes_count", 0
)
define_global_i32(
    "pcc_gc_backend4_store_buffer_cross_thread_medium_flushed_entries_count", 0
)
define_global_i32("pcc_gc_backend4_remembered_set_entries_count", 0)
define_global_i32("pcc_gc_backend4_remembered_set_duplicate_skips_count", 0)
define_global_i32("pcc_gc_backend4_remembered_set_high_water_count", 0)
define_global_i32("pcc_gc_backend4_remembered_page_entries_count", 0)
define_global_i32("pcc_gc_backend4_remembered_page_slot_entries_count", 0)
define_global_i32("pcc_gc_backend4_remembered_page_high_water_count", 0)
define_global_i32("pcc_gc_backend3_remembered_overflow", 0)
define_global_i64("pcc_gc_backend3_remembered_owner_allocation_limit", -1)
define_global_i32("pcc_gc_next_object_id", 1)

# Raw linked structures and root registries shared by all five backends.
define_global_ptr_null("pcc_gc_last_alloc")
define_global_ptr_null("pcc_gc_forwarding_head")
define_global_ptr_null("pcc_gc_identity_head")
define_global_ptr_null("pcc_gc_relocation_set_head")
define_global_i64("pcc_gc_backend4_relocation_reset_owner", 0)
define_global_i64("pcc_gc_backend4_reseed_plan_probe_pause", 0)
define_global_i64("pcc_gc_backend4_reseed_plan_probe_state_value", 0)
define_global_i64("pcc_gc_backend4_reseed_plan_probe_allocation_limit", -1)
define_global_i64("pcc_gc_backend4_reseed_page_count_owner", 0)
define_global_i64("pcc_gc_backend4_reseed_commit_owner", 0)
define_global_i64("pcc_gc_backend4_reseed_page_revision", 0)
define_global_i64("pcc_gc_backend4_reseed_relocation_revision", 0)
define_global_ptr_null("pcc_gc_backend4_reset_object_cursor")
define_global_ptr_null("pcc_gc_backend4_reseed_page_count_cursor")
define_global_ptr_null("pcc_gc_backend4_reseed_relocation_cursor")
define_global_ptr_null("pcc_gc_backend3_remembered_owner_head")
define_global_ptr_null("pcc_gc_backend3_remembered_scan_cursor")
define_global_i64("pcc_gc_backend3_remembered_scan_revision", 0)
define_global_i64("pcc_gc_object_list_revision", 0)
define_global_ptr_null("pcc_gc_backend4_store_buffer_head")
define_global_ptr_null("pcc_gc_backend4_store_buffer_medium_head")
define_global_ptr_null("pcc_gc_backend4_zpage_head")
define_global_ptr_null("pcc_gc_backend4_selector_scan_cursor")
define_global_ptr_null("pcc_gc_backend4_selector_scan_best")
define_global_ptr_null("pcc_gc_backend4_selector_scan_page")
define_global_i64("pcc_gc_backend4_selector_scan_owner", 0)
define_global_i64("pcc_gc_backend4_selector_scan_best_score", -1)
define_global_i32("pcc_gc_backend4_selector_scan_allow_large", 0)
define_global_i32("pcc_gc_backend4_selector_scan_require_unselected", 0)
define_global_i32("pcc_gc_backend4_selector_scan_restart", 0)
define_global_ptr_null("pcc_gc_backend4_selector_page_cursor")
define_global_ptr_null("pcc_gc_backend4_selector_page_seed")
define_global_ptr_null("pcc_gc_backend4_selector_page")
define_global_i64("pcc_gc_backend4_selector_page_owner", 0)
define_global_i32("pcc_gc_backend4_selector_page_allow_large", 0)
define_global_i32("pcc_gc_backend4_selector_page_seed_pending", 0)
define_global_ptr_null("pcc_gc_backend4_zpage_payload_span_head")
define_global_ptr_null("pcc_gc_backend4_page_head")
# Count of pages whose recycle was deferred (flag at page+104) while a
# dealloc trash cascade was active; lets the post-drain sweep no-op in O(1)
# when nothing deferred instead of walking the live page list per cascade.
define_global_i64("pcc_gc_backend4_deferred_recycle_pages", 0)
define_global_ptr_null("pcc_gc_backend4_free_page_head")
define_global_ptr_null("pcc_gc_backend4_retained_page_head")
define_global_ptr_null("pcc_gc_backend4_evacuation_page_head")
# Last single-threaded evacuation_page_find answer, valid while
# pcc_gc_backend4_reseed_page_revision (bumped by every list mutation) holds.
define_global_ptr_null("pcc_gc_backend4_evacuation_find_memo_page")
define_global_ptr_null("pcc_gc_backend4_evacuation_find_memo_node")
define_global_i64("pcc_gc_backend4_evacuation_find_memo_revision", 0)
define_global_ptr_null("pcc_gc_backend4_active_small_young_page")
define_global_ptr_null("pcc_gc_backend4_active_small_old_page")
define_global_ptr_null("pcc_gc_backend4_active_medium_young_page")
define_global_ptr_null("pcc_gc_backend4_active_medium_old_page")
define_global_ptr_null("pcc_gc_backend4_remembered_slots_head")
define_global_ptr_null("pcc_gc_backend4_remembered_page_head")
define_global_ptr_null("pcc_gc_object_node_free_head")
define_global_i32("pcc_gc_object_node_free_count", 0)
define_global_ptr_null("pcc_gc_backend4_zpage_node_free_head")
define_global_i32("pcc_gc_backend4_zpage_node_free_count", 0)
define_global_i32("pcc_gc_mark_active", 0)
define_global_i32("pcc_gc_cycle_requested", 0)
define_global_i64("pcc_gc_tracing_cycle_epoch", 0)
define_global_i64("pcc_gc_tracing_finish_claim_epoch", 0)
define_global_i64("pcc_gc_tracing_finish_claim_backend", -1)
define_global_i64("pcc_gc_tracing_finish_commits", 0)
define_global_i32("pcc_gc_trace_extension_roots_pending", 0)
define_global_i64("pcc_gc_trace_extension_roots_epoch", 0)
define_global_i64("pcc_gc_trace_extension_roots_backend", -1)
define_global_i32("pcc_gc_root_count", 0)
define_global_ptr_null("pcc_gc_root_slots")
define_global_ptr_null("pcc_gc_frame_head")
# Start of the linked precise stack-map table, published by a table-enabled
# executable's main through pcc_gc_register_precise_stackmap_table.
define_global_ptr_null("pcc_gc_precise_stackmap_table")
# Mapped managed objects collected by the step-3 differential stack walk.
define_global_ptr_null("pcc_gc_stack_diff_values")
define_global_i64("pcc_gc_stack_diff_count", 0)
define_global_i64("pcc_gc_stack_diff_capacity", 0)
# Address index of the published table: (start, end, function offset)
# triples sorted by start; count -1 marks a table that failed validation.
define_global_ptr_null("pcc_gc_stackmap_index")
define_global_i64("pcc_gc_stackmap_index_count", 0)
define_global_i64("pcc_gc_stackmap_location_base", 0)
# PCC_GC_STACKMAP_ROOTS, read once: -1 unread, 0 registered slots root
# every frame, 1 the stack walk roots this thread's frames, 2 with a report.
define_global_i32("pcc_gc_stackmap_roots_setting", -1)
define_global_ptr_null("pcc_gc_continuation_root_head")
define_global_ptr_null("pcc_gc_scheduler_root_head")
define_global_i64("pcc_gc_root_registry_revision", 0)
define_global_i32("pcc_gc_backend3_frame_root_scan_phase", 0)
define_global_i64("pcc_gc_backend3_frame_root_scan_slot", -1)
define_global_ptr_null("pcc_gc_backend3_frame_root_scan_cursor")
define_global_ptr_null("pcc_gc_backend3_continuation_root_scan_cursor")
define_global_i32("pcc_gc_backend3_scheduler_root_scan_phase", 0)
define_global_i64("pcc_gc_backend3_scheduler_root_scan_slot", -1)
define_global_ptr_null("pcc_gc_backend3_scheduler_root_scan_cursor")
define_global_ptr_null("pcc_gc_object_head")
define_global_ptr_null("pcc_gc_trace_cursor")
define_global_ptr_null("pcc_gc_trace_cext_pending_obj")
define_global_i64("pcc_gc_trace_cext_pending_epoch", 0)
define_global_i64("pcc_gc_trace_cext_pending_backend", -1)
define_global_i32("pcc_gc_backend4_remap_active", 0)
define_global_i64("pcc_gc_backend4_remap_epoch", 0)
define_global_ptr_null("pcc_gc_backend4_remap_pending_obj")
define_global_ptr_null("pcc_gc_backend3_young_head")
define_global_ptr_null("pcc_gc_backend3_promotion_head")
define_global_ptr_null("pcc_gc_backend3_promotion_tail")
define_global_i64("pcc_gc_backend3_promotion_revision", 0)
define_global_i64("pcc_gc_backend3_promotion_probe_pause", 0)
define_global_i64("pcc_gc_backend3_promotion_probe_state_value", 0)
define_global_ptr_null("pcc_gc_relocate_slot_pairs_ctx")
