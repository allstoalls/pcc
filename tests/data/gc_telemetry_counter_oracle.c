/* Frozen metric -> counter mapping of the retired C runtime's
 * pcc_gc_telemetry() (pcc/runtime/src/py_gc_backend.c, removed 2026-09-25).
 * tests/python/test_freestanding_gc_telemetry.py links it as the reference
 * oracle that the pcc-Python py_gc_telemetry.py must keep matching for every
 * metric id.  Change it only together with a deliberate counter ABI change. */

int64_t pcc_gc_telemetry(int64_t metric) {
    pcc_gc_init_config();
    if (metric == PCC_GC_COUNTER_DEBT_BYTES) {
        return __atomic_load_n(&pcc_gc_debt_bytes, __ATOMIC_RELAXED);
    }
    if (metric == PCC_GC_COUNTER_MAX_PAUSE_US) {
        return __atomic_load_n(&pcc_gc_max_pause_us, __ATOMIC_RELAXED);
    }
    if (metric == PCC_GC_COUNTER_PAUSE_COUNT) {
        return __atomic_load_n(&pcc_gc_pause_count, __ATOMIC_RELAXED);
    }
    if (metric == PCC_GC_COUNTER_PAUSE_SUM_US) {
        return __atomic_load_n(&pcc_gc_pause_sum_us, __ATOMIC_RELAXED);
    }
    if (
        metric >= PCC_GC_COUNTER_PAUSE_HIST_LT_100US
        && metric <= PCC_GC_COUNTER_PAUSE_HIST_GE_10MS
    ) {
        return __atomic_load_n(
            &pcc_gc_pause_hist[metric - PCC_GC_COUNTER_PAUSE_HIST_LT_100US],
            __ATOMIC_RELAXED
        );
    }
    if (metric == PCC_GC_COUNTER_MINOR_ALLOCATIONS) {
        return __atomic_load_n(
            &pcc_gc_minor_allocations, __ATOMIC_RELAXED
        );
    }
    if (metric == PCC_GC_COUNTER_MINOR_COLLECTIONS) {
        return __atomic_load_n(
            &pcc_gc_minor_collections, __ATOMIC_RELAXED
        );
    }
    if (metric == PCC_GC_COUNTER_MINOR_BYTES) {
        return __atomic_load_n(&pcc_gc_minor_bytes, __ATOMIC_RELAXED);
    }
    if (metric == PCC_GC_COUNTER_CMS_WORKER_STARTS) {
        return __atomic_load_n(
            &pcc_gc_cms_worker_starts, __ATOMIC_RELAXED
        );
    }
    if (metric == PCC_GC_COUNTER_CMS_QUEUE_PUSHES) {
        return __atomic_load_n(
            &pcc_gc_cms_queue_pushes, __ATOMIC_RELAXED
        );
    }
    if (metric == PCC_GC_COUNTER_CMS_WORKER_DRAINS) {
        return __atomic_load_n(
            &pcc_gc_cms_worker_drains, __ATOMIC_RELAXED
        );
    }
    if (metric == PCC_GC_COUNTER_CMS_MUTATOR_ASSISTS) {
        return __atomic_load_n(
            &pcc_gc_cms_mutator_assists, __ATOMIC_RELAXED
        );
    }
    if (metric == PCC_GC_COUNTER_RELOCATION_FORWARDS) {
        return pcc_gc_relocation_forwards;
    }
    if (metric == PCC_GC_COUNTER_RELOCATION_BARRIER_FORWARDS) {
        return pcc_gc_relocation_barrier_forwards;
    }
    if (metric == PCC_GC_COUNTER_RELOCATION_PIN_REJECTS) {
        return pcc_gc_relocation_pin_rejects;
    }
    if (metric == PCC_GC_COUNTER_CMS_WORKER_TRACES) {
        return __atomic_load_n(
            &pcc_gc_cms_worker_traces, __ATOMIC_RELAXED
        );
    }
    if (metric == PCC_GC_COUNTER_MINOR_ARENA_REFILLS) {
        return __atomic_load_n(
            &pcc_gc_minor_arena_refills, __ATOMIC_RELAXED
        );
    }
    if (metric == PCC_GC_COUNTER_MINOR_ARENA_BUMPS) {
        return __atomic_load_n(
            &pcc_gc_minor_arena_bumps, __ATOMIC_RELAXED
        );
    }
    if (metric == PCC_GC_COUNTER_MINOR_ARENA_FALLBACKS) {
        return __atomic_load_n(
            &pcc_gc_minor_arena_fallbacks, __ATOMIC_RELAXED
        );
    }
    if (metric == PCC_GC_COUNTER_CMS_WORKER_STOPS) {
        return __atomic_load_n(
            &pcc_gc_cms_worker_stops, __ATOMIC_RELAXED
        );
    }
    if (metric == PCC_GC_COUNTER_CMS_WB_FLUSHES) {
        return __atomic_load_n(
            &pcc_gc_cms_wb_flushes, __ATOMIC_RELAXED
        );
    }
    if (metric == PCC_GC_COUNTER_RELOCATION_SET_SIZE) {
        return pcc_gc_relocation_set_size();
    }
    if (metric == PCC_GC_COUNTER_FORWARDING_ENTRIES) {
        return pcc_gc_backend4_forwarding_entries();
    }
    if (metric == PCC_GC_COUNTER_STABLE_IDS) {
        return pcc_gc_backend4_stable_id_entries();
    }
    if (metric == PCC_GC_COUNTER_RELOCATION_FRAGMENTATION_SCORE) {
        return pcc_gc_backend4_fragmentation_score();
    }
    if (metric == PCC_GC_COUNTER_SCHEDULER_ROOTS) {
        return pcc_gc_scheduler_root_count();
    }
    if (metric == PCC_GC_COUNTER_FRAME_ROOT_SLOTS) {
        return pcc_gc_frame_root_slot_count();
    }
    if (metric == PCC_GC_COUNTER_COROUTINE_ROOT_SCORE) {
        return pcc_gc_coroutine_root_score();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_STORE_BARRIERS) {
        return pcc_gc_backend4_generation_barrier_score();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_STORE_BUFFER_ENTRIES) {
        return pcc_gc_backend4_store_buffer_entries();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_YOUNG_PROMOTIONS) {
        return pcc_gc_backend4_generation_promotion_score();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_EVACUATION_CANDIDATES) {
        return pcc_gc_backend4_evacuation_candidate_score();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_EVACUATED_BYTES) {
        return pcc_gc_backend4_evacuated_bytes();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_PAGE_POLICY_SCORE) {
        return pcc_gc_backend4_page_policy_score();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_LARGE_OBJECT_DEFERS) {
        return pcc_gc_backend4_large_object_defer_score();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_LARGE_OBJECT_DEFERRED_BYTES) {
        return pcc_gc_backend4_large_object_deferred_bytes();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_SMALL_PAGE_CANDIDATES) {
        return pcc_gc_backend4_small_page_candidate_score();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_MEDIUM_PAGE_CANDIDATES) {
        return pcc_gc_backend4_medium_page_candidate_score();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_EVACUATION_CANDIDATE_BYTES) {
        return pcc_gc_backend4_evacuation_candidate_bytes();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_SMALL_PAGE_CANDIDATE_BYTES) {
        return pcc_gc_backend4_small_page_candidate_bytes();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_MEDIUM_PAGE_CANDIDATE_BYTES) {
        return pcc_gc_backend4_medium_page_candidate_bytes();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_EVACUATION_CANDIDATE_ZPAGE_BYTES) {
        return pcc_gc_backend4_evacuation_candidate_zpage_bytes();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_SMALL_PAGE_CANDIDATE_ZPAGE_BYTES) {
        return pcc_gc_backend4_small_page_candidate_zpage_bytes();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_MEDIUM_PAGE_CANDIDATE_ZPAGE_BYTES) {
        return pcc_gc_backend4_medium_page_candidate_zpage_bytes();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_EVACUATION_PAGE_CANDIDATES) {
        return pcc_gc_backend4_evacuation_page_candidate_score();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_STORE_BUFFER_DRAIN_BATCHES) {
        return pcc_gc_backend4_store_buffer_drain_batches();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_STORE_BUFFER_DRAINED_ENTRIES) {
        return pcc_gc_backend4_store_buffer_drained_entries();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_STORE_BUFFER_DUPLICATE_SKIPS) {
        return pcc_gc_backend4_store_buffer_duplicate_skips();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_STORE_BUFFER_HIGH_WATER) {
        return pcc_gc_backend4_store_buffer_high_water();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_PAGE_PRESSURE_SCORE) {
        return pcc_gc_backend4_page_pressure_score();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_STORE_BUFFER_OWNER_FANOUT_HIGH_WATER) {
        return pcc_gc_backend4_store_buffer_owner_fanout_high_water();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_STORE_BUFFER_OWNER_COUNT_HIGH_WATER) {
        return pcc_gc_backend4_store_buffer_owner_count_high_water();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_STORE_BUFFER_INCOMPLETE_DRAINS) {
        return pcc_gc_backend4_store_buffer_incomplete_drains();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_EVACUATION_INCOMPLETE_BATCHES) {
        return pcc_gc_backend4_evacuation_incomplete_batches();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_STORE_BUFFER_BATCH_CAPACITY) {
        return pcc_gc_backend4_store_buffer_batch_capacity();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_STORE_BUFFER_MAX_BATCH_SIZE) {
        return pcc_gc_backend4_store_buffer_max_batch_size();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_STORE_BUFFER_FULL_BATCHES) {
        return pcc_gc_backend4_store_buffer_full_batches();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_REMEMBERED_SET_ENTRIES) {
        return pcc_gc_backend4_remembered_set_entries();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_REMEMBERED_SET_DUPLICATE_SKIPS) {
        return pcc_gc_backend4_remembered_set_duplicate_skips();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_REMEMBERED_SET_HIGH_WATER) {
        return pcc_gc_backend4_remembered_set_high_water();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_REMEMBERED_PAGE_ENTRIES) {
        return pcc_gc_backend4_remembered_page_entries();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_REMEMBERED_PAGE_SLOT_ENTRIES) {
        return pcc_gc_backend4_remembered_page_slot_entries();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_REMEMBERED_PAGE_HIGH_WATER) {
        return pcc_gc_backend4_remembered_page_high_water();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_ZPAGE_COUNT) {
        return pcc_gc_backend4_zpage_count();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_ZPAGE_CAPACITY_BYTES) {
        return pcc_gc_backend4_zpage_capacity_bytes();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_ZPAGE_FRAGMENTATION_BYTES) {
        return pcc_gc_backend4_zpage_fragmentation_bytes();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_ZPAGE_LARGE_PAGES) {
        return pcc_gc_backend4_zpage_large_pages();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_ZPAGE_USED_BYTES) {
        return pcc_gc_backend4_zpage_used_bytes();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_ZPAGE_FRAGMENTATION_PER_MILLE) {
        return pcc_gc_backend4_zpage_fragmentation_per_mille();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_ZPAGE_POLICY_SCORE) {
        return pcc_gc_backend4_zpage_policy_score();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_ZPAGE_REMEMBERED_SLOTS) {
        return pcc_gc_backend4_zpage_remembered_slots();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_ZPAGE_REMEMBERED_CARDS) {
        return pcc_gc_backend4_zpage_remembered_cards();
    }
    if (
        metric == PCC_GC_COUNTER_GENZGC_ZPAGE_REMEMBERED_CARD_RATIO_PER_MILLE
    ) {
        return pcc_gc_backend4_zpage_remembered_card_ratio_per_mille();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_ZPAGE_DIRTY_PAGES) {
        return pcc_gc_backend4_zpage_dirty_pages();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_ZPAGE_FRAGMENTED_PAGES) {
        return pcc_gc_backend4_zpage_fragmented_pages();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_ZPAGE_YOUNG_PAGES) {
        return pcc_gc_backend4_zpage_young_pages();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_ZPAGE_OLD_PAGES) {
        return pcc_gc_backend4_zpage_old_pages();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_ZPAGE_FREE_PAGES) {
        return pcc_gc_backend4_zpage_free_pages();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_ZPAGE_FREE_CAPACITY_BYTES) {
        return pcc_gc_backend4_zpage_free_capacity_bytes();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_STORE_BUFFER_MEDIUM_CAPACITY) {
        return pcc_gc_backend4_store_buffer_medium_capacity();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_STORE_BUFFER_MEDIUM_PENDING) {
        return pcc_gc_backend4_store_buffer_medium_pending();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_STORE_BUFFER_MEDIUM_FLUSHES) {
        return pcc_gc_backend4_store_buffer_medium_flushes();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_STORE_BUFFER_MEDIUM_FLUSHED_ENTRIES) {
        return pcc_gc_backend4_store_buffer_medium_flushed_entries();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_STORE_BUFFER_MEDIUM_FULL_FLUSHES) {
        return pcc_gc_backend4_store_buffer_medium_full_flushes();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_STORE_BUFFER_CROSS_THREAD_MEDIUM_FLUSHES) {
        return pcc_gc_backend4_store_buffer_cross_thread_medium_flushes();
    }
    if (metric == PCC_GC_COUNTER_UNMANAGED_REFCOUNT_OPS) {
        return __atomic_load_n(
            &pcc_gc_unmanaged_refcount_ops, __ATOMIC_RELAXED
        );
    }
    if (metric == PCC_GC_COUNTER_REFCOUNT_PROVENANCE_PROBE) {
        return __atomic_load_n(
            &pcc_gc_refcount_provenance_probe, __ATOMIC_RELAXED
        );
    }
    if (metric == PCC_GC_COUNTER_GENZGC_STORE_BUFFER_CROSS_THREAD_MEDIUM_FLUSHED_ENTRIES) {
        return pcc_gc_backend4_store_buffer_cross_thread_medium_flushed_entries();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_EVACUATION_EFFICIENCY_PER_MILLE) {
        return pcc_gc_backend4_evacuation_efficiency_per_mille();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_FRAGMENTATION_BACKLOG_BYTES) {
        return pcc_gc_backend4_fragmentation_backlog_bytes();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_FRAGMENTATION_POLICY_SCORE) {
        return pcc_gc_backend4_fragmentation_policy_score();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_SMALL_PAGE_LIMIT_BYTES) {
        return pcc_gc_backend4_small_page_limit_bytes();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_MEDIUM_PAGE_LIMIT_BYTES) {
        return pcc_gc_backend4_medium_page_limit_bytes();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_LARGE_DEFER_LIMIT_BYTES) {
        return pcc_gc_backend4_large_defer_limit_bytes();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_LARGE_OBJECT_RECONSIDERATIONS) {
        return pcc_gc_backend4_large_object_reconsiderations();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_YOUNG_OBJECTS) {
        return pcc_gc_backend4_young_object_count();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_OLD_OBJECTS) {
        return pcc_gc_backend4_old_object_count();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_YOUNG_BYTES) {
        return pcc_gc_backend4_young_bytes();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_OLD_BYTES) {
        return pcc_gc_backend4_old_bytes();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_SMALL_PAGE_OBJECTS) {
        return pcc_gc_backend4_small_page_object_count();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_MEDIUM_PAGE_OBJECTS) {
        return pcc_gc_backend4_medium_page_object_count();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_LARGE_PAGE_OBJECTS) {
        return pcc_gc_backend4_large_page_object_count();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_SMALL_PAGE_BYTES) {
        return pcc_gc_backend4_small_page_live_bytes();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_MEDIUM_PAGE_BYTES) {
        return pcc_gc_backend4_medium_page_live_bytes();
    }
    if (metric == PCC_GC_COUNTER_GENZGC_LARGE_PAGE_BYTES) {
        return pcc_gc_backend4_large_page_live_bytes();
    }
    if (metric == PCC_GC_COUNTER_CMS_WORKBUFFER_SCORE) {
        return __atomic_load_n(
            &pcc_gc_cms_queue_pushes, __ATOMIC_RELAXED
        );
    }
    if (metric == PCC_GC_COUNTER_CMS_PRODUCTION_SCORE) {
        return __atomic_load_n(
            &pcc_gc_cms_queue_pushes, __ATOMIC_RELAXED
        ) + __atomic_load_n(&pcc_gc_cms_worker_starts, __ATOMIC_RELAXED);
    }
    if (metric == PCC_GC_COUNTER_GEN_MINOR_PRODUCTIVITY_SCORE) {
        return __atomic_load_n(
            &pcc_gc_minor_arena_refills, __ATOMIC_RELAXED
        ) + __atomic_load_n(&pcc_gc_minor_arena_bumps, __ATOMIC_RELAXED);
    }
    if (metric == PCC_GC_COUNTER_GEN_REMEMBERED_UPDATE_SCORE) {
        return __atomic_load_n(
            &pcc_gc_minor_arena_refills, __ATOMIC_RELAXED
        ) + __atomic_load_n(&pcc_gc_minor_arena_bumps, __ATOMIC_RELAXED);
    }
    return pcc_gc_metric_load(metric);
}
