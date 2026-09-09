"""GC telemetry split out from py_gc_backend.

The core GC backend archive member is always pulled into ordinary programs via
allocation/barrier symbols. Keep the read-only telemetry switch in a separate
member so programs that do not query GC counters do not also carry the large
``pcc_gc_telemetry`` dispatch body.

``pcc_gc_telemetry_reset`` intentionally stays in ``py_gc_backend.py`` because
it reseeds backend4 epoch state and clears deferred object flags.
"""

from pcc import i64
from pcc.extern import c_abi_export, c_int64, extern
from pcc.unsafe import (
    atomic_load_i32,
    gc_backend_current,
    global_addr,
    load_i32,
    load_i64,
)


__pcc_freestanding__ = True


pcc_gc_relocation_set_size = extern("pcc_gc_relocation_set_size", (), c_int64)
pcc_gc_scheduler_root_count = extern("pcc_gc_scheduler_root_count", (), c_int64)
pcc_gc_frame_root_slot_count = extern("pcc_gc_frame_root_slot_count", (), c_int64)
pcc_gc_coroutine_root_score = extern("pcc_gc_coroutine_root_score", (), c_int64)

pcc_gc_backend4_forwarding_entries = extern(
    "pcc_gc_backend4_forwarding_entries", (), c_int64
)
pcc_gc_backend4_stable_id_entries = extern(
    "pcc_gc_backend4_stable_id_entries", (), c_int64
)
pcc_gc_backend4_fragmentation_score = extern(
    "pcc_gc_backend4_fragmentation_score", (), c_int64
)
pcc_gc_backend4_generation_barrier_score = extern(
    "pcc_gc_backend4_generation_barrier_score", (), c_int64
)
pcc_gc_backend4_store_buffer_entries = extern(
    "pcc_gc_backend4_store_buffer_entries", (), c_int64
)
pcc_gc_backend4_generation_promotion_score = extern(
    "pcc_gc_backend4_generation_promotion_score", (), c_int64
)
pcc_gc_backend4_evacuation_candidate_score = extern(
    "pcc_gc_backend4_evacuation_candidate_score", (), c_int64
)
pcc_gc_backend4_evacuated_bytes = extern(
    "pcc_gc_backend4_evacuated_bytes", (), c_int64
)
pcc_gc_backend4_page_policy_score = extern(
    "pcc_gc_backend4_page_policy_score", (), c_int64
)
pcc_gc_backend4_large_object_defer_score = extern(
    "pcc_gc_backend4_large_object_defer_score", (), c_int64
)
pcc_gc_backend4_large_object_deferred_bytes = extern(
    "pcc_gc_backend4_large_object_deferred_bytes", (), c_int64
)
pcc_gc_backend4_small_page_candidate_score = extern(
    "pcc_gc_backend4_small_page_candidate_score", (), c_int64
)
pcc_gc_backend4_medium_page_candidate_score = extern(
    "pcc_gc_backend4_medium_page_candidate_score", (), c_int64
)
pcc_gc_backend4_evacuation_candidate_bytes = extern(
    "pcc_gc_backend4_evacuation_candidate_bytes", (), c_int64
)
pcc_gc_backend4_small_page_candidate_bytes = extern(
    "pcc_gc_backend4_small_page_candidate_bytes", (), c_int64
)
pcc_gc_backend4_medium_page_candidate_bytes = extern(
    "pcc_gc_backend4_medium_page_candidate_bytes", (), c_int64
)
pcc_gc_backend4_evacuation_candidate_zpage_bytes = extern(
    "pcc_gc_backend4_evacuation_candidate_zpage_bytes", (), c_int64
)
pcc_gc_backend4_small_page_candidate_zpage_bytes = extern(
    "pcc_gc_backend4_small_page_candidate_zpage_bytes", (), c_int64
)
pcc_gc_backend4_medium_page_candidate_zpage_bytes = extern(
    "pcc_gc_backend4_medium_page_candidate_zpage_bytes", (), c_int64
)
pcc_gc_backend4_evacuation_page_candidate_score = extern(
    "pcc_gc_backend4_evacuation_page_candidate_score", (), c_int64
)
pcc_gc_backend4_store_buffer_drain_batches = extern(
    "pcc_gc_backend4_store_buffer_drain_batches", (), c_int64
)
pcc_gc_backend4_store_buffer_drained_entries = extern(
    "pcc_gc_backend4_store_buffer_drained_entries", (), c_int64
)
pcc_gc_backend4_store_buffer_duplicate_skips = extern(
    "pcc_gc_backend4_store_buffer_duplicate_skips", (), c_int64
)
pcc_gc_backend4_store_buffer_high_water = extern(
    "pcc_gc_backend4_store_buffer_high_water", (), c_int64
)
pcc_gc_backend4_page_pressure_score = extern(
    "pcc_gc_backend4_page_pressure_score", (), c_int64
)
pcc_gc_backend4_store_buffer_owner_fanout_high_water = extern(
    "pcc_gc_backend4_store_buffer_owner_fanout_high_water", (), c_int64
)
pcc_gc_backend4_store_buffer_owner_count_high_water = extern(
    "pcc_gc_backend4_store_buffer_owner_count_high_water", (), c_int64
)
pcc_gc_backend4_store_buffer_incomplete_drains = extern(
    "pcc_gc_backend4_store_buffer_incomplete_drains", (), c_int64
)
pcc_gc_backend4_evacuation_incomplete_batches = extern(
    "pcc_gc_backend4_evacuation_incomplete_batches", (), c_int64
)
pcc_gc_backend4_store_buffer_batch_capacity = extern(
    "pcc_gc_backend4_store_buffer_batch_capacity", (), c_int64
)
pcc_gc_backend4_store_buffer_max_batch_size = extern(
    "pcc_gc_backend4_store_buffer_max_batch_size", (), c_int64
)
pcc_gc_backend4_store_buffer_full_batches = extern(
    "pcc_gc_backend4_store_buffer_full_batches", (), c_int64
)
pcc_gc_backend4_remembered_set_entries = extern(
    "pcc_gc_backend4_remembered_set_entries", (), c_int64
)
pcc_gc_backend4_remembered_set_duplicate_skips = extern(
    "pcc_gc_backend4_remembered_set_duplicate_skips", (), c_int64
)
pcc_gc_backend4_remembered_set_high_water = extern(
    "pcc_gc_backend4_remembered_set_high_water", (), c_int64
)
pcc_gc_backend4_store_buffer_medium_capacity = extern(
    "pcc_gc_backend4_store_buffer_medium_capacity", (), c_int64
)
pcc_gc_backend4_store_buffer_medium_pending = extern(
    "pcc_gc_backend4_store_buffer_medium_pending", (), c_int64
)
pcc_gc_backend4_store_buffer_medium_flushes = extern(
    "pcc_gc_backend4_store_buffer_medium_flushes", (), c_int64
)
pcc_gc_backend4_store_buffer_medium_flushed_entries = extern(
    "pcc_gc_backend4_store_buffer_medium_flushed_entries", (), c_int64
)
pcc_gc_backend4_store_buffer_medium_full_flushes = extern(
    "pcc_gc_backend4_store_buffer_medium_full_flushes", (), c_int64
)
pcc_gc_backend4_evacuation_efficiency_per_mille = extern(
    "pcc_gc_backend4_evacuation_efficiency_per_mille", (), c_int64
)
pcc_gc_backend4_fragmentation_backlog_bytes = extern(
    "pcc_gc_backend4_fragmentation_backlog_bytes", (), c_int64
)
pcc_gc_backend4_fragmentation_policy_score = extern(
    "pcc_gc_backend4_fragmentation_policy_score", (), c_int64
)
pcc_gc_backend4_small_page_limit_bytes = extern(
    "pcc_gc_backend4_small_page_limit_bytes", (), c_int64
)
pcc_gc_backend4_medium_page_limit_bytes = extern(
    "pcc_gc_backend4_medium_page_limit_bytes", (), c_int64
)
pcc_gc_backend4_large_defer_limit_bytes = extern(
    "pcc_gc_backend4_large_defer_limit_bytes", (), c_int64
)
pcc_gc_backend4_large_object_reconsiderations = extern(
    "pcc_gc_backend4_large_object_reconsiderations", (), c_int64
)
pcc_gc_backend4_young_object_count = extern(
    "pcc_gc_backend4_young_object_count", (), c_int64
)
pcc_gc_backend4_old_object_count = extern(
    "pcc_gc_backend4_old_object_count", (), c_int64
)
pcc_gc_backend4_young_bytes = extern("pcc_gc_backend4_young_bytes", (), c_int64)
pcc_gc_backend4_old_bytes = extern("pcc_gc_backend4_old_bytes", (), c_int64)
pcc_gc_backend4_small_page_object_count = extern(
    "pcc_gc_backend4_small_page_object_count", (), c_int64
)
pcc_gc_backend4_medium_page_object_count = extern(
    "pcc_gc_backend4_medium_page_object_count", (), c_int64
)
pcc_gc_backend4_large_page_object_count = extern(
    "pcc_gc_backend4_large_page_object_count", (), c_int64
)
pcc_gc_backend4_small_page_live_bytes = extern(
    "pcc_gc_backend4_small_page_live_bytes", (), c_int64
)
pcc_gc_backend4_medium_page_live_bytes = extern(
    "pcc_gc_backend4_medium_page_live_bytes", (), c_int64
)
pcc_gc_backend4_large_page_live_bytes = extern(
    "pcc_gc_backend4_large_page_live_bytes", (), c_int64
)
pcc_gc_backend4_store_buffer_cross_thread_medium_flushes = extern(
    "pcc_gc_backend4_store_buffer_cross_thread_medium_flushes", (), c_int64
)
pcc_gc_backend4_store_buffer_cross_thread_medium_flushed_entries = extern(
    "pcc_gc_backend4_store_buffer_cross_thread_medium_flushed_entries", (), c_int64
)
pcc_gc_backend4_remembered_page_entries = extern(
    "pcc_gc_backend4_remembered_page_entries", (), c_int64
)
pcc_gc_backend4_remembered_page_slot_entries = extern(
    "pcc_gc_backend4_remembered_page_slot_entries", (), c_int64
)
pcc_gc_backend4_remembered_page_high_water = extern(
    "pcc_gc_backend4_remembered_page_high_water", (), c_int64
)
pcc_gc_backend4_zpage_count = extern("pcc_gc_backend4_zpage_count", (), c_int64)
pcc_gc_backend4_zpage_capacity_bytes = extern(
    "pcc_gc_backend4_zpage_capacity_bytes", (), c_int64
)
pcc_gc_backend4_zpage_fragmentation_bytes = extern(
    "pcc_gc_backend4_zpage_fragmentation_bytes", (), c_int64
)
pcc_gc_backend4_zpage_large_pages = extern(
    "pcc_gc_backend4_zpage_large_pages", (), c_int64
)
pcc_gc_backend4_zpage_used_bytes = extern(
    "pcc_gc_backend4_zpage_used_bytes", (), c_int64
)
pcc_gc_backend4_zpage_fragmentation_per_mille = extern(
    "pcc_gc_backend4_zpage_fragmentation_per_mille", (), c_int64
)
pcc_gc_backend4_zpage_policy_score = extern(
    "pcc_gc_backend4_zpage_policy_score", (), c_int64
)
pcc_gc_backend4_zpage_remembered_slots = extern(
    "pcc_gc_backend4_zpage_remembered_slots", (), c_int64
)
pcc_gc_backend4_zpage_remembered_cards = extern(
    "pcc_gc_backend4_zpage_remembered_cards", (), c_int64
)
pcc_gc_backend4_zpage_remembered_card_ratio_per_mille = extern(
    "pcc_gc_backend4_zpage_remembered_card_ratio_per_mille", (), c_int64
)
pcc_gc_backend4_zpage_dirty_pages = extern(
    "pcc_gc_backend4_zpage_dirty_pages", (), c_int64
)
pcc_gc_backend4_zpage_fragmented_pages = extern(
    "pcc_gc_backend4_zpage_fragmented_pages", (), c_int64
)
pcc_gc_backend4_zpage_young_pages = extern(
    "pcc_gc_backend4_zpage_young_pages", (), c_int64
)
pcc_gc_backend4_zpage_old_pages = extern(
    "pcc_gc_backend4_zpage_old_pages", (), c_int64
)
pcc_gc_backend4_zpage_free_pages = extern(
    "pcc_gc_backend4_zpage_free_pages", (), c_int64
)
pcc_gc_backend4_zpage_free_capacity_bytes = extern(
    "pcc_gc_backend4_zpage_free_capacity_bytes", (), c_int64
)


@c_abi_export("pcc_gc_telemetry")
def pcc_gc_telemetry(metric: i64) -> i64:
    gc_backend_current()
    if metric == 116:
        return load_i64(global_addr("pcc_gc_unmanaged_refcount_ops"), 0)
    if metric == 6:
        return load_i32(global_addr("pcc_gc_debt_bytes"), 0)
    if metric == 7:
        return load_i32(global_addr("pcc_gc_metric_max_pause_us"), 0)
    if metric == 32:
        return load_i32(global_addr("pcc_gc_metric_pause_count"), 0)
    if metric == 33:
        return load_i32(global_addr("pcc_gc_metric_pause_sum_us"), 0)
    if metric == 34:
        return load_i32(global_addr("pcc_gc_metric_pause_hist0"), 0)
    if metric == 35:
        return load_i32(global_addr("pcc_gc_metric_pause_hist1"), 0)
    if metric == 36:
        return load_i32(global_addr("pcc_gc_metric_pause_hist2"), 0)
    if metric == 37:
        return load_i32(global_addr("pcc_gc_metric_pause_hist3"), 0)
    if metric == 8:
        return atomic_load_i32(global_addr("pcc_gc_minor_allocations"), 0, "relaxed")
    if metric == 9:
        return atomic_load_i32(global_addr("pcc_gc_minor_collections"), 0, "relaxed")
    if metric == 10:
        return atomic_load_i32(global_addr("pcc_gc_minor_bytes"), 0, "relaxed")
    if metric == 11:
        return load_i32(global_addr("pcc_gc_cms_worker_starts"), 0)
    if metric == 12:
        return load_i32(global_addr("pcc_gc_cms_queue_pushes"), 0)
    if metric == 13:
        return load_i32(global_addr("pcc_gc_cms_worker_drains"), 0)
    if metric == 14:
        return load_i32(global_addr("pcc_gc_cms_mutator_assists"), 0)
    if metric == 15:
        return load_i32(global_addr("pcc_gc_relocation_forwards"), 0)
    if metric == 16:
        return load_i32(global_addr("pcc_gc_relocation_barrier_forwards"), 0)
    if metric == 17:
        return load_i32(global_addr("pcc_gc_relocation_pin_rejects"), 0)
    if metric == 18:
        return load_i32(global_addr("pcc_gc_cms_worker_traces"), 0)
    if metric == 19:
        return atomic_load_i32(global_addr("pcc_gc_minor_arena_refills"), 0, "relaxed")
    if metric == 20:
        return atomic_load_i32(global_addr("pcc_gc_minor_arena_bumps"), 0, "relaxed")
    if metric == 21:
        return atomic_load_i32(global_addr("pcc_gc_minor_arena_fallbacks"), 0, "relaxed")
    if metric == 22:
        return load_i32(global_addr("pcc_gc_cms_worker_stops"), 0)
    if metric == 23:
        return load_i32(global_addr("pcc_gc_cms_wb_flushes"), 0)
    if metric == 24:
        return pcc_gc_relocation_set_size()
    if metric == 25:
        return pcc_gc_backend4_forwarding_entries()
    if metric == 26:
        return pcc_gc_backend4_stable_id_entries()
    if metric == 27:
        return pcc_gc_backend4_fragmentation_score()
    if metric == 38:
        return pcc_gc_scheduler_root_count()
    if metric == 39:
        return pcc_gc_frame_root_slot_count()
    if metric == 40:
        return pcc_gc_coroutine_root_score()
    if metric == 41:
        return pcc_gc_backend4_generation_barrier_score()
    if metric == 42:
        return pcc_gc_backend4_store_buffer_entries()
    if metric == 43:
        return pcc_gc_backend4_generation_promotion_score()
    if metric == 44:
        return pcc_gc_backend4_evacuation_candidate_score()
    if metric == 45:
        return pcc_gc_backend4_evacuated_bytes()
    if metric == 46:
        return pcc_gc_backend4_page_policy_score()
    if metric == 47:
        return pcc_gc_backend4_large_object_defer_score()
    if metric == 48:
        return pcc_gc_backend4_large_object_deferred_bytes()
    if metric == 49:
        return pcc_gc_backend4_small_page_candidate_score()
    if metric == 50:
        return pcc_gc_backend4_medium_page_candidate_score()
    if metric == 51:
        return pcc_gc_backend4_evacuation_candidate_bytes()
    if metric == 52:
        return pcc_gc_backend4_small_page_candidate_bytes()
    if metric == 53:
        return pcc_gc_backend4_medium_page_candidate_bytes()
    if metric == 112:
        return pcc_gc_backend4_evacuation_candidate_zpage_bytes()
    if metric == 113:
        return pcc_gc_backend4_small_page_candidate_zpage_bytes()
    if metric == 114:
        return pcc_gc_backend4_medium_page_candidate_zpage_bytes()
    if metric == 115:
        return pcc_gc_backend4_evacuation_page_candidate_score()
    if metric == 54:
        return pcc_gc_backend4_store_buffer_drain_batches()
    if metric == 55:
        return pcc_gc_backend4_store_buffer_drained_entries()
    if metric == 56:
        return pcc_gc_backend4_store_buffer_duplicate_skips()
    if metric == 57:
        return pcc_gc_backend4_store_buffer_high_water()
    if metric == 58:
        return pcc_gc_backend4_page_pressure_score()
    if metric == 59:
        return pcc_gc_backend4_store_buffer_owner_fanout_high_water()
    if metric == 60:
        return pcc_gc_backend4_store_buffer_owner_count_high_water()
    if metric == 61:
        return pcc_gc_backend4_store_buffer_incomplete_drains()
    if metric == 62:
        return pcc_gc_backend4_evacuation_incomplete_batches()
    if metric == 63:
        return pcc_gc_backend4_store_buffer_batch_capacity()
    if metric == 64:
        return pcc_gc_backend4_store_buffer_max_batch_size()
    if metric == 65:
        return pcc_gc_backend4_store_buffer_full_batches()
    if metric == 66:
        return pcc_gc_backend4_remembered_set_entries()
    if metric == 67:
        return pcc_gc_backend4_remembered_set_duplicate_skips()
    if metric == 68:
        return pcc_gc_backend4_remembered_set_high_water()
    if metric == 69:
        return pcc_gc_backend4_store_buffer_medium_capacity()
    if metric == 70:
        return pcc_gc_backend4_store_buffer_medium_pending()
    if metric == 71:
        return pcc_gc_backend4_store_buffer_medium_flushes()
    if metric == 72:
        return pcc_gc_backend4_store_buffer_medium_flushed_entries()
    if metric == 73:
        return pcc_gc_backend4_store_buffer_medium_full_flushes()
    if metric == 74:
        return pcc_gc_backend4_evacuation_efficiency_per_mille()
    if metric == 75:
        return pcc_gc_backend4_fragmentation_backlog_bytes()
    if metric == 76:
        return pcc_gc_backend4_fragmentation_policy_score()
    if metric == 77:
        return pcc_gc_backend4_small_page_limit_bytes()
    if metric == 78:
        return pcc_gc_backend4_medium_page_limit_bytes()
    if metric == 79:
        return pcc_gc_backend4_large_defer_limit_bytes()
    if metric == 80:
        return pcc_gc_backend4_large_object_reconsiderations()
    if metric == 81:
        return pcc_gc_backend4_young_object_count()
    if metric == 82:
        return pcc_gc_backend4_old_object_count()
    if metric == 83:
        return pcc_gc_backend4_young_bytes()
    if metric == 84:
        return pcc_gc_backend4_old_bytes()
    if metric == 85:
        return pcc_gc_backend4_small_page_object_count()
    if metric == 86:
        return pcc_gc_backend4_medium_page_object_count()
    if metric == 87:
        return pcc_gc_backend4_large_page_object_count()
    if metric == 88:
        return pcc_gc_backend4_small_page_live_bytes()
    if metric == 89:
        return pcc_gc_backend4_medium_page_live_bytes()
    if metric == 90:
        return pcc_gc_backend4_large_page_live_bytes()
    if metric == 91:
        return pcc_gc_backend4_store_buffer_cross_thread_medium_flushes()
    if metric == 92:
        return pcc_gc_backend4_store_buffer_cross_thread_medium_flushed_entries()
    if metric == 93:
        return pcc_gc_backend4_remembered_page_entries()
    if metric == 94:
        return pcc_gc_backend4_remembered_page_slot_entries()
    if metric == 95:
        return pcc_gc_backend4_remembered_page_high_water()
    if metric == 96:
        return pcc_gc_backend4_zpage_count()
    if metric == 97:
        return pcc_gc_backend4_zpage_capacity_bytes()
    if metric == 98:
        return pcc_gc_backend4_zpage_fragmentation_bytes()
    if metric == 99:
        return pcc_gc_backend4_zpage_large_pages()
    if metric == 100:
        return pcc_gc_backend4_zpage_used_bytes()
    if metric == 101:
        return pcc_gc_backend4_zpage_fragmentation_per_mille()
    if metric == 102:
        return pcc_gc_backend4_zpage_policy_score()
    if metric == 103:
        return pcc_gc_backend4_zpage_remembered_slots()
    if metric == 108:
        return pcc_gc_backend4_zpage_remembered_cards()
    if metric == 109:
        return pcc_gc_backend4_zpage_remembered_card_ratio_per_mille()
    if metric == 104:
        return pcc_gc_backend4_zpage_dirty_pages()
    if metric == 105:
        return pcc_gc_backend4_zpage_fragmented_pages()
    if metric == 106:
        return pcc_gc_backend4_zpage_young_pages()
    if metric == 107:
        return pcc_gc_backend4_zpage_old_pages()
    if metric == 110:
        return pcc_gc_backend4_zpage_free_pages()
    if metric == 111:
        return pcc_gc_backend4_zpage_free_capacity_bytes()
    if metric == 28:
        return load_i32(global_addr("pcc_gc_cms_queue_pushes"), 0) + load_i32(
            global_addr("pcc_gc_cms_worker_starts"), 0
        )
    if metric == 29:
        return load_i32(global_addr("pcc_gc_cms_queue_pushes"), 0)
    if metric == 30:
        return atomic_load_i32(
            global_addr("pcc_gc_minor_arena_refills"), 0, "relaxed"
        ) + atomic_load_i32(
            global_addr("pcc_gc_minor_arena_bumps"), 0, "relaxed"
        )
    if metric == 31:
        return atomic_load_i32(
            global_addr("pcc_gc_minor_arena_refills"), 0, "relaxed"
        ) + atomic_load_i32(
            global_addr("pcc_gc_minor_arena_bumps"), 0, "relaxed"
        )
    if metric == 0:
        return load_i32(global_addr("pcc_gc_metric_alloc"), 0)
    if metric == 1:
        return load_i32(global_addr("pcc_gc_metric_store"), 0)
    if metric == 2:
        return load_i32(global_addr("pcc_gc_metric_load"), 0)
    if metric == 3:
        return load_i32(global_addr("pcc_gc_metric_safepoint"), 0)
    if metric == 4:
        return load_i32(global_addr("pcc_gc_metric_pin"), 0)
    if metric == 5:
        return load_i32(global_addr("pcc_gc_metric_step"), 0)
    return -1


@c_abi_export("pcc_gc_backend2_worker_buffer_score")
def pcc_gc_backend2_worker_buffer_score() -> i64:
    return pcc_gc_telemetry(29)


@c_abi_export("pcc_gc_backend2_production_score")
def pcc_gc_backend2_production_score() -> i64:
    return pcc_gc_telemetry(28)


@c_abi_export("pcc_gc_backend3_minor_productivity_score")
def pcc_gc_backend3_minor_productivity_score() -> i64:
    return pcc_gc_telemetry(30)


@c_abi_export("pcc_gc_backend3_remembered_update_score")
def pcc_gc_backend3_remembered_update_score() -> i64:
    return pcc_gc_telemetry(31)
