"""Raw tracing allocation pacing and rooted GC4 safepoint scheduling."""

from pcc import i64
from pcc.extern import c_abi_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    atomic_cas_i32,
    atomic_cas_i64,
    atomic_load_i32,
    atomic_load_i64,
    atomic_rmw_i32,
    atomic_rmw_i64,
    atomic_store_i32,
    function_addr,
    global_addr,
    global_load_ptr,
    global_store_ptr,
    is_tagged_int,
    load_i32,
    load_i64,
    load_ptr,
    null,
    ptr_eq,
    ptr_is_null,
    stack_alloc,
    store_i32,
    store_i64,
    store_ptr,
    unsigned_div_i64,
)


__pcc_freestanding__ = True

pcc_gc_set_mark_color = extern("pcc_gc_set_mark_color", (c_ptr, c_int64), c_void)


pcc_threads_enabled = extern("pcc_threads_enabled", (), c_int64)
pcc_thread_no_park_depth = extern("pcc_thread_no_park_depth", (), c_int64)
pcc_py_gc_minor_graph_lock_depth = extern("pcc_py_gc_minor_graph_lock_depth", (), c_int64)
pcc_gc_step = extern("pcc_gc_step", (c_int64,), c_int64)
pcc_gc_is_cms_worker = extern("pcc_gc_is_cms_worker", (), c_int64)
pcc_thread_join = extern("pcc_thread_join", (c_ptr, c_ptr), c_int64)
pcc_platform_monotonic_us = extern("pcc_platform_monotonic_us", (), c_int64)
pcc_gc_config_ensure = extern("pcc_gc_config_ensure", (), c_int64)
pcc_gc_maybe_start_cms_worker = extern(
    "pcc_gc_maybe_start_cms_worker", (), c_void
)
pcc_py_gc_minor_graph_lock = extern(
    "pcc_py_gc_minor_graph_lock", (), c_void
)
pcc_py_gc_minor_graph_unlock = extern(
    "pcc_py_gc_minor_graph_unlock", (), c_void
)
pcc_gc_begin_mark_cycle = extern("pcc_gc_begin_mark_cycle", (), c_void)
pcc_gc_seed_roots = extern("pcc_gc_seed_roots", (), c_void)
pcc_gc_finish_tracing_cycle = extern(
    "pcc_gc_finish_tracing_cycle", (c_int64, c_int64), c_int64
)
pcc_gc_tracing_finish_claim_clear_unlocked = extern(
    "pcc_gc_tracing_finish_claim_clear_unlocked", (c_int64, c_int64), c_void
)
pcc_thread_owns_stopped_world = extern(
    "pcc_thread_owns_stopped_world", (), c_int64
)
pcc_stop_the_world = extern("pcc_stop_the_world", (), c_int64)
pcc_resume_world = extern("pcc_resume_world", (), c_int64)
pcc_gc_trace_referents = extern("pcc_gc_trace_referents", (c_ptr,), c_void)
pcc_gc_trace_cext_referents_unlocked = extern(
    "pcc_gc_trace_cext_referents_unlocked", (c_ptr, c_ptr), c_void
)
pcc_gc_drain_all_gray_locked_slice = extern(
    "pcc_gc_drain_all_gray_locked_slice", (), c_int64
)
pcc_gc_gray_current_roots = extern("pcc_gc_gray_current_roots", (), c_void)
pcc_capi_is_cext_type_tag = extern("pcc_capi_is_cext_type_tag", (c_int64,), c_int64)
pcc_gc_object_is_known_no_lock = extern("pcc_gc_object_is_known_no_lock", (c_ptr,), c_int64)
pcc_gc_trace_mark_gray_if_known = extern(
    "pcc_gc_trace_mark_gray_if_known", (c_ptr,), c_void
)
pcc_capi_visit_extension_module_state_roots = extern(
    "pcc_capi_visit_extension_module_state_roots", (c_ptr, c_ptr), c_void
)
pcc_gc_gray_count_load_acquire = extern(
    "pcc_gc_gray_count_load_acquire", (), c_int64
)
pcc_gc_gray_count_decrement_acq_rel = extern(
    "pcc_gc_gray_count_decrement_acq_rel", (), c_void
)
py_incref = extern("py_incref", (c_ptr,), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)


@c_abi_export("pcc_gc_tracing_gray_extension_root")
def pcc_gc_tracing_gray_extension_root(root, ctx) -> None:
    if ptr_is_null(root) != 0 or is_tagged_int(root) != 0:
        return
    if ptr_is_null(ctx) != 0:
        return
    epoch: i64 = load_i64(ctx, 0)
    backend: i64 = load_i64(ctx, 8)
    pcc_py_gc_minor_graph_lock()
    if (
        load_i32(
            global_addr("pcc_gc_trace_extension_roots_pending"), 0
        ) == 2
        and load_i64(
            global_addr("pcc_gc_trace_extension_roots_epoch"), 0
        ) == epoch
        and load_i64(
            global_addr("pcc_gc_trace_extension_roots_backend"), 0
        ) == backend
        and load_i64(global_addr("pcc_gc_tracing_cycle_epoch"), 0)
        == epoch
        and load_i32(global_addr("pcc_gc_backend_selected"), 0)
        == backend
        and load_i32(global_addr("pcc_gc_mark_active"), 0) != 0
    ):
        pcc_gc_trace_mark_gray_if_known(root)
    pcc_py_gc_minor_graph_unlock()


@c_abi_export("pcc_gc_tracing_gray_final_extension_root")
def pcc_gc_tracing_gray_final_extension_root(root, ctx) -> None:
    if ptr_is_null(root) != 0 or is_tagged_int(root) != 0:
        return
    if ptr_is_null(ctx) != 0:
        return
    epoch: i64 = load_i64(ctx, 0)
    backend: i64 = load_i64(ctx, 8)
    pcc_py_gc_minor_graph_lock()
    if (
        load_i32(
            global_addr("pcc_gc_trace_extension_roots_pending"), 0
        ) == 3
        and load_i64(
            global_addr("pcc_gc_trace_extension_roots_epoch"), 0
        ) == epoch
        and load_i64(
            global_addr("pcc_gc_trace_extension_roots_backend"), 0
        ) == backend
        and load_i64(
            global_addr("pcc_gc_tracing_finish_claim_epoch"), 0
        ) == epoch
        and load_i64(
            global_addr("pcc_gc_tracing_finish_claim_backend"), 0
        ) == backend
        and load_i64(global_addr("pcc_gc_tracing_cycle_epoch"), 0)
        == epoch
        and load_i32(global_addr("pcc_gc_backend_selected"), 0)
        == backend
        and load_i32(global_addr("pcc_gc_mark_active"), 0) != 0
    ):
        pcc_gc_trace_mark_gray_if_known(root)
    pcc_py_gc_minor_graph_unlock()


@c_abi_export("pcc_gc_complete_mark_cycle_seed")
def _pcc_gc_complete_mark_cycle_seed(
    claim_epoch: i64, claim_backend: i64
) -> i64:
    owns_stopped_world: i64 = pcc_thread_owns_stopped_world()
    acquired_stopped_world: i64 = 0
    if owns_stopped_world == 0:
        if pcc_stop_the_world() != 0:
            pcc_py_gc_minor_graph_lock()
            if (
                load_i32(
                    global_addr("pcc_gc_trace_extension_roots_pending"), 0
                ) == 4
                and load_i64(
                    global_addr("pcc_gc_trace_extension_roots_epoch"), 0
                ) == claim_epoch
                and load_i64(
                    global_addr("pcc_gc_trace_extension_roots_backend"), 0
                ) == claim_backend
            ):
                store_i32(
                    global_addr("pcc_gc_trace_extension_roots_pending"),
                    0,
                    0,
                )
                store_i64(
                    global_addr("pcc_gc_trace_extension_roots_epoch"), 0, 0
                )
                store_i64(
                    global_addr("pcc_gc_trace_extension_roots_backend"),
                    0,
                    -1,
                )
            pcc_py_gc_minor_graph_unlock()
            return 0
        acquired_stopped_world = 1

    pcc_py_gc_minor_graph_lock()
    valid: i64 = 0
    if (
        load_i32(
            global_addr("pcc_gc_trace_extension_roots_pending"), 0
        ) == 4
        and load_i64(
            global_addr("pcc_gc_trace_extension_roots_epoch"), 0
        ) == claim_epoch
        and load_i64(
            global_addr("pcc_gc_trace_extension_roots_backend"), 0
        ) == claim_backend
        and load_i64(global_addr("pcc_gc_tracing_cycle_epoch"), 0)
        == claim_epoch
        and load_i32(global_addr("pcc_gc_backend_selected"), 0)
        == claim_backend
        and load_i32(global_addr("pcc_gc_mark_active"), 0) == 0
        and load_i32(global_addr("pcc_gc_cycle_requested"), 0) != 0
    ):
        valid = 1
    pcc_py_gc_minor_graph_unlock()

    if valid != 0:
        pcc_gc_seed_roots()

    pcc_py_gc_minor_graph_lock()
    if (
        valid != 0
        and load_i32(
            global_addr("pcc_gc_trace_extension_roots_pending"), 0
        ) == 4
        and load_i64(
            global_addr("pcc_gc_trace_extension_roots_epoch"), 0
        ) == claim_epoch
        and load_i64(
            global_addr("pcc_gc_trace_extension_roots_backend"), 0
        ) == claim_backend
        and load_i64(global_addr("pcc_gc_tracing_cycle_epoch"), 0)
        == claim_epoch
        and load_i32(global_addr("pcc_gc_backend_selected"), 0)
        == claim_backend
        and load_i32(global_addr("pcc_gc_mark_active"), 0) == 0
        and load_i32(global_addr("pcc_gc_cycle_requested"), 0) != 0
    ):
        store_i32(global_addr("pcc_gc_mark_active"), 0, 1)
        store_i32(global_addr("pcc_gc_cycle_requested"), 0, 0)
        store_i32(
            global_addr("pcc_gc_trace_extension_roots_pending"), 0, 1
        )
        global_store_ptr(
            "pcc_gc_trace_cursor",
            global_load_ptr("pcc_gc_object_head"),
        )
        if pcc_gc_gray_count_load_acquire() == 0:
            global_store_ptr("pcc_gc_trace_cursor", null())
        valid = 1
    else:
        valid = 0
        if (
            load_i32(
                global_addr("pcc_gc_trace_extension_roots_pending"), 0
            ) == 4
            and load_i64(
                global_addr("pcc_gc_trace_extension_roots_epoch"), 0
            ) == claim_epoch
            and load_i64(
                global_addr("pcc_gc_trace_extension_roots_backend"), 0
            ) == claim_backend
        ):
            store_i32(
                global_addr("pcc_gc_trace_extension_roots_pending"), 0, 0
            )
            store_i64(
                global_addr("pcc_gc_trace_extension_roots_epoch"), 0, 0
            )
            store_i64(
                global_addr("pcc_gc_trace_extension_roots_backend"), 0, -1
            )
    pcc_py_gc_minor_graph_unlock()
    if acquired_stopped_world != 0:
        pcc_resume_world()
    return valid


@c_abi_export("pcc_gc_trace_cext_complete_context")
def _pcc_gc_trace_cext_complete_context(cext_ctx) -> i64:
    cext_obj = load_ptr(cext_ctx, 0)
    if ptr_is_null(cext_obj) != 0:
        return 0
    pcc_gc_trace_cext_referents_unlocked(cext_obj, cext_ctx)
    epoch: i64 = load_i64(cext_ctx, 8)
    backend: i64 = load_i64(cext_ctx, 16)
    committed: i64 = 0
    pcc_py_gc_minor_graph_lock()
    if (
        ptr_eq(
            global_load_ptr("pcc_gc_trace_cext_pending_obj"), cext_obj
        ) != 0
        and load_i64(
            global_addr("pcc_gc_trace_cext_pending_epoch"), 0
        ) == epoch
        and load_i64(
            global_addr("pcc_gc_trace_cext_pending_backend"), 0
        ) == backend
        and load_i64(global_addr("pcc_gc_tracing_cycle_epoch"), 0)
        == epoch
        and load_i32(global_addr("pcc_gc_backend_selected"), 0)
        == backend
        and load_i32(global_addr("pcc_gc_mark_active"), 0) != 0
        and pcc_gc_object_is_known_no_lock(cext_obj) != 0
    ):
        cext_flags: i64 = load_i32(cext_obj, 12)
        if (cext_flags & 16) != 0:
            pcc_gc_gray_count_decrement_acq_rel()
            pcc_gc_set_mark_color(cext_obj, 32)
            committed = 1
    if ptr_eq(
        global_load_ptr("pcc_gc_trace_cext_pending_obj"), cext_obj
    ) != 0:
        global_store_ptr("pcc_gc_trace_cext_pending_obj", null())
        store_i64(global_addr("pcc_gc_trace_cext_pending_epoch"), 0, 0)
        store_i64(
            global_addr("pcc_gc_trace_cext_pending_backend"), 0, -1
        )
    pcc_py_gc_minor_graph_unlock()
    py_decref(cext_obj)
    store_ptr(cext_ctx, 0, null())
    return committed


@c_abi_export("pcc_gc_drain_all_gray_stopped_world_py")
def _pcc_gc_drain_all_gray_stopped_world(
    claim_epoch: i64, claim_backend: i64
) -> i64:
    processed: i64 = 0
    cext_ctx = stack_alloc(24)
    while True:
        store_ptr(cext_ctx, 0, null())
        pcc_py_gc_minor_graph_lock()
        valid: i64 = 0
        if (
            load_i64(global_addr("pcc_gc_tracing_cycle_epoch"), 0)
            == claim_epoch
            and load_i32(global_addr("pcc_gc_backend_selected"), 0)
            == claim_backend
            and load_i32(global_addr("pcc_gc_mark_active"), 0) != 0
            and ptr_is_null(
                global_load_ptr("pcc_gc_trace_cext_pending_obj")
            ) != 0
        ):
            valid = 1
            processed = processed + pcc_gc_drain_all_gray_locked_slice()
            cext_obj = global_load_ptr("pcc_gc_trace_cext_pending_obj")
            if ptr_is_null(cext_obj) == 0:
                store_ptr(cext_ctx, 0, cext_obj)
                store_i64(
                    cext_ctx,
                    8,
                    load_i64(
                        global_addr("pcc_gc_trace_cext_pending_epoch"), 0
                    ),
                )
                store_i64(
                    cext_ctx,
                    16,
                    load_i64(
                        global_addr("pcc_gc_trace_cext_pending_backend"), 0
                    ),
                )
        pcc_py_gc_minor_graph_unlock()
        if valid == 0 or ptr_is_null(load_ptr(cext_ctx, 0)) != 0:
            return processed
        _pcc_gc_trace_cext_complete_context(cext_ctx)


@c_abi_export("pcc_gc_complete_claimed_tracing_cycle")
def pcc_gc_complete_claimed_tracing_cycle(
    claim_epoch: i64, claim_backend: i64
) -> i64:
    owns_stopped_world: i64 = pcc_thread_owns_stopped_world()
    acquired_stopped_world: i64 = 0
    if owns_stopped_world == 0:
        if pcc_stop_the_world() != 0:
            pcc_py_gc_minor_graph_lock()
            pcc_gc_tracing_finish_claim_clear_unlocked(
                claim_epoch, claim_backend
            )
            pcc_py_gc_minor_graph_unlock()
            return 0
        acquired_stopped_world = 1

    extension_ctx = stack_alloc(16)
    store_i64(extension_ctx, 0, claim_epoch)
    store_i64(extension_ctx, 8, claim_backend)
    visit_extension_roots: i64 = 0
    pcc_py_gc_minor_graph_lock()
    if (
        load_i64(
            global_addr("pcc_gc_tracing_finish_claim_epoch"), 0
        ) == claim_epoch
        and load_i64(
            global_addr("pcc_gc_tracing_finish_claim_backend"), 0
        ) == claim_backend
        and load_i64(global_addr("pcc_gc_tracing_cycle_epoch"), 0)
        == claim_epoch
        and load_i32(global_addr("pcc_gc_backend_selected"), 0)
        == claim_backend
        and load_i32(global_addr("pcc_gc_mark_active"), 0) != 0
        and load_i32(
            global_addr("pcc_gc_trace_extension_roots_pending"), 0
        ) == 0
    ):
        store_i32(
            global_addr("pcc_gc_trace_extension_roots_pending"), 0, 3
        )
        store_i64(
            global_addr("pcc_gc_trace_extension_roots_epoch"),
            0,
            claim_epoch,
        )
        store_i64(
            global_addr("pcc_gc_trace_extension_roots_backend"),
            0,
            claim_backend,
        )
        visit_extension_roots = 1
    else:
        pcc_gc_tracing_finish_claim_clear_unlocked(
            claim_epoch, claim_backend
        )
    pcc_py_gc_minor_graph_unlock()

    if visit_extension_roots != 0:
        pcc_capi_visit_extension_module_state_roots(
            function_addr("pcc_gc_tracing_gray_final_extension_root"),
            extension_ctx,
        )

    pcc_py_gc_minor_graph_lock()
    ready_to_drain: i64 = 0
    if (
        visit_extension_roots != 0
        and load_i32(
            global_addr("pcc_gc_trace_extension_roots_pending"), 0
        ) == 3
        and load_i64(
            global_addr("pcc_gc_trace_extension_roots_epoch"), 0
        ) == claim_epoch
        and load_i64(
            global_addr("pcc_gc_trace_extension_roots_backend"), 0
        ) == claim_backend
        and load_i64(
            global_addr("pcc_gc_tracing_finish_claim_epoch"), 0
        ) == claim_epoch
        and load_i64(
            global_addr("pcc_gc_tracing_finish_claim_backend"), 0
        ) == claim_backend
        and load_i64(global_addr("pcc_gc_tracing_cycle_epoch"), 0)
        == claim_epoch
        and load_i32(global_addr("pcc_gc_backend_selected"), 0)
        == claim_backend
        and load_i32(global_addr("pcc_gc_mark_active"), 0) != 0
    ):
        ready_to_drain = 1
        store_i32(
            global_addr("pcc_gc_trace_extension_roots_pending"), 0, 0
        )
        pcc_gc_gray_current_roots()
    else:
        if (
            load_i32(
                global_addr("pcc_gc_trace_extension_roots_pending"), 0
            ) == 3
            and load_i64(
                global_addr("pcc_gc_trace_extension_roots_epoch"), 0
            ) == claim_epoch
            and load_i64(
                global_addr("pcc_gc_trace_extension_roots_backend"), 0
            ) == claim_backend
        ):
            store_i32(
                global_addr("pcc_gc_trace_extension_roots_pending"), 0, 0
            )
            store_i64(
                global_addr("pcc_gc_trace_extension_roots_epoch"), 0, 0
            )
            store_i64(
                global_addr("pcc_gc_trace_extension_roots_backend"), 0, -1
            )
        pcc_gc_tracing_finish_claim_clear_unlocked(
            claim_epoch, claim_backend
        )
    pcc_py_gc_minor_graph_unlock()

    if ready_to_drain != 0:
        _pcc_gc_drain_all_gray_stopped_world(
            claim_epoch, claim_backend
        )

    pcc_py_gc_minor_graph_lock()
    final_token_valid: i64 = 0
    if (
        ready_to_drain != 0
        and ptr_is_null(
            global_load_ptr("pcc_gc_trace_cext_pending_obj")
        ) != 0
        and load_i64(
            global_addr("pcc_gc_tracing_finish_claim_epoch"), 0
        ) == claim_epoch
        and load_i64(
            global_addr("pcc_gc_tracing_finish_claim_backend"), 0
        ) == claim_backend
        and load_i64(global_addr("pcc_gc_tracing_cycle_epoch"), 0)
        == claim_epoch
        and load_i32(global_addr("pcc_gc_backend_selected"), 0)
        == claim_backend
        and load_i32(global_addr("pcc_gc_mark_active"), 0) != 0
    ):
        final_token_valid = 1
    committed: i64 = 0
    if final_token_valid != 0:
        committed = pcc_gc_finish_tracing_cycle(
            claim_epoch, claim_backend
        )
    elif ready_to_drain != 0:
        if (
            load_i64(
                global_addr("pcc_gc_trace_extension_roots_epoch"), 0
            ) == claim_epoch
            and load_i64(
                global_addr("pcc_gc_trace_extension_roots_backend"), 0
            ) == claim_backend
        ):
            store_i32(
                global_addr("pcc_gc_trace_extension_roots_pending"), 0, 0
            )
            store_i64(
                global_addr("pcc_gc_trace_extension_roots_epoch"), 0, 0
            )
            store_i64(
                global_addr("pcc_gc_trace_extension_roots_backend"), 0, -1
            )
        pcc_gc_tracing_finish_claim_clear_unlocked(
            claim_epoch, claim_backend
        )
    pcc_py_gc_minor_graph_unlock()
    if acquired_stopped_world != 0:
        pcc_resume_world()
    return committed


@c_abi_export("pcc_gc_tracing_debt_threshold")
def pcc_gc_tracing_debt_threshold() -> i64:
    override: i64 = load_i64(global_addr("pcc_gc_debt_threshold_override"), 0)
    if override > 0:
        return override
    threshold: i64 = 65536
    # The pause applies to what the last completed mark found reachable,
    # not to the current heap: that includes the garbage awaiting a sweep,
    # and with the default pause of 1000 the threshold then grew faster
    # than the debt and automatic collection stopped for good.
    live: i64 = load_i64(global_addr("pcc_gc_tracing_marked_bytes"), 0)
    pause: i64 = load_i32(global_addr("pcc_gc_pause"), 0)
    if live > 0 and pause > 100:
        # Divide first without discarding the fractional contribution.  A
        # large heap can overflow live * factor even when its final /100
        # threshold fits.  Saturate only the final nonnegative byte count.
        factor: i64 = pause - 100
        whole: i64 = unsigned_div_i64(live, 100)
        remainder: i64 = live - whole * 100
        tail: i64 = unsigned_div_i64(remainder * factor, 100)
        live_pause: i64 = 9223372036854775807
        if whole <= unsigned_div_i64(9223372036854775807 - tail, factor):
            live_pause = whole * factor + tail
        if live_pause > threshold:
            threshold = live_pause
    return threshold


@c_abi_export("pcc_gc_tracing_budget_from_debt")
def pcc_gc_tracing_budget_from_debt() -> i64:
    debt: i64 = load_i64(global_addr("pcc_gc_debt_bytes"), 0)
    stepmul: i64 = load_i32(global_addr("pcc_gc_stepmul"), 0)
    units: i64 = unsigned_div_i64(debt, 64)
    # stepmul is configured in [1, 10000].  At this point the final budget
    # is already capped for every legal multiplier; do not overflow an
    # intermediate product merely to clamp it afterwards.
    if units >= 6553600:
        return 65536
    budget: i64 = unsigned_div_i64(units * stepmul, 100)
    if budget < 1:
        budget: i64 = 1
    if budget > 65536:
        budget: i64 = 65536
    return budget


@c_abi_export("pcc_gc_tracing_discharge_debt")
def pcc_gc_tracing_discharge_debt(processed: i64) -> None:
    if processed <= 0:
        return
    debt: i64 = load_i64(global_addr("pcc_gc_debt_bytes"), 0)
    stepmul: i64 = load_i32(global_addr("pcc_gc_stepmul"), 0)
    credit: i64 = unsigned_div_i64(processed * 64 * stepmul, 100)
    if credit < 64:
        credit: i64 = 64
    # Mutator threads add debt concurrently with the collector's discharge.
    slot = global_addr("pcc_gc_debt_bytes")
    while True:
        debt = atomic_load_i64(slot, 0, "acquire")
        remaining: i64 = 0
        if credit < debt:
            remaining = debt - credit
        if atomic_cas_i64(slot, 0, debt, remaining, "acq_rel", "acquire") == debt:
            return


@c_abi_export("pcc_gc_tracing_record_pause")
def pcc_gc_tracing_record_pause(start_us: i64, end_us: i64) -> None:
    if start_us <= 0:
        return
    if end_us < start_us:
        return
    pause: i64 = end_us - start_us
    if pause <= 0:
        pause: i64 = 1
    store_i64(
        global_addr("pcc_gc_metric_pause_count"),
        0,
        load_i64(global_addr("pcc_gc_metric_pause_count"), 0) + 1,
    )
    store_i64(
        global_addr("pcc_gc_metric_pause_sum_us"),
        0,
        load_i64(global_addr("pcc_gc_metric_pause_sum_us"), 0) + pause,
    )
    if pause < 100:
        store_i32(
            global_addr("pcc_gc_metric_pause_hist0"),
            0,
            load_i32(global_addr("pcc_gc_metric_pause_hist0"), 0) + 1,
        )
    elif pause < 1000:
        store_i32(
            global_addr("pcc_gc_metric_pause_hist1"),
            0,
            load_i32(global_addr("pcc_gc_metric_pause_hist1"), 0) + 1,
        )
    elif pause < 10000:
        store_i32(
            global_addr("pcc_gc_metric_pause_hist2"),
            0,
            load_i32(global_addr("pcc_gc_metric_pause_hist2"), 0) + 1,
        )
    else:
        store_i32(
            global_addr("pcc_gc_metric_pause_hist3"),
            0,
            load_i32(global_addr("pcc_gc_metric_pause_hist3"), 0) + 1,
        )
    current: i64 = load_i64(global_addr("pcc_gc_metric_max_pause_us"), 0)
    if pause > current:
        store_i64(global_addr("pcc_gc_metric_max_pause_us"), 0, pause)


@c_abi_export("pcc_gc_record_explicit_pause")
def pcc_gc_record_explicit_pause(start_us: i64, end_us: i64) -> None:
    pcc_gc_tracing_record_pause(start_us, end_us)


@c_abi_export("pcc_gc_tracing_step_cycle")
def pcc_gc_tracing_step_cycle(remaining_budget: i64) -> i64:
    if remaining_budget <= 0:
        return 0

    claim_epoch: i64 = 0
    claim_backend: i64 = -1
    extension_ctx = stack_alloc(16)
    store_i64(extension_ctx, 0, 0)
    store_i64(extension_ctx, 8, -1)
    cext_ctx = stack_alloc(24)
    store_ptr(cext_ctx, 0, null())
    store_i64(cext_ctx, 8, 0)
    store_i64(cext_ctx, 16, -1)
    pcc_py_gc_minor_graph_lock()
    local_processed: i64 = 0
    active: i64 = load_i32(global_addr("pcc_gc_mark_active"), 0)
    requested: i64 = load_i32(global_addr("pcc_gc_cycle_requested"), 0)
    if active == 0:
        if requested == 0:
            pcc_py_gc_minor_graph_unlock()
            return local_processed
        if (
            load_i64(
                global_addr("pcc_gc_tracing_finish_claim_epoch"), 0
            )
            != 0
        ):
            pcc_py_gc_minor_graph_unlock()
            return local_processed
        if pcc_threads_enabled() != 0:
            if (
                load_i32(global_addr("pcc_gc_in_auto_step"), 0) != 0
                and pcc_thread_owns_stopped_world() == 0
            ):
                pcc_py_gc_minor_graph_unlock()
                return local_processed
        pcc_gc_begin_mark_cycle()
        seed_epoch: i64 = load_i64(
            global_addr("pcc_gc_trace_extension_roots_epoch"), 0
        )
        seed_backend: i64 = load_i64(
            global_addr("pcc_gc_trace_extension_roots_backend"), 0
        )
        pcc_py_gc_minor_graph_unlock()
        if _pcc_gc_complete_mark_cycle_seed(
            seed_epoch, seed_backend
        ) == 0:
            return local_processed
        pcc_py_gc_minor_graph_lock()

    if ptr_is_null(global_load_ptr("pcc_gc_trace_cext_pending_obj")) == 0:
        pcc_py_gc_minor_graph_unlock()
        return local_processed

    extension_pending: i64 = load_i32(
        global_addr("pcc_gc_trace_extension_roots_pending"), 0
    )
    if extension_pending != 0:
        visit_extension_roots: i64 = 0
        if (
            extension_pending == 1
            and load_i32(global_addr("pcc_gc_mark_active"), 0) != 0
            and load_i64(
                global_addr("pcc_gc_trace_extension_roots_epoch"), 0
            ) == load_i64(global_addr("pcc_gc_tracing_cycle_epoch"), 0)
            and load_i64(
                global_addr("pcc_gc_trace_extension_roots_backend"), 0
            ) == load_i32(global_addr("pcc_gc_backend_selected"), 0)
        ):
            store_i64(
                extension_ctx,
                0,
                load_i64(
                    global_addr("pcc_gc_trace_extension_roots_epoch"), 0
                ),
            )
            store_i64(
                extension_ctx,
                8,
                load_i64(
                    global_addr("pcc_gc_trace_extension_roots_backend"), 0
                ),
            )
            store_i32(
                global_addr("pcc_gc_trace_extension_roots_pending"), 0, 2
            )
            visit_extension_roots = 1
        pcc_py_gc_minor_graph_unlock()
        if visit_extension_roots == 0:
            return local_processed
        pcc_capi_visit_extension_module_state_roots(
            function_addr("pcc_gc_tracing_gray_extension_root"),
            extension_ctx,
        )
        pcc_py_gc_minor_graph_lock()
        extension_epoch: i64 = load_i64(extension_ctx, 0)
        extension_backend: i64 = load_i64(extension_ctx, 8)
        if (
            load_i32(
                global_addr("pcc_gc_trace_extension_roots_pending"), 0
            ) != 2
            or load_i64(
                global_addr("pcc_gc_trace_extension_roots_epoch"), 0
            ) != extension_epoch
            or load_i64(
                global_addr("pcc_gc_trace_extension_roots_backend"), 0
            ) != extension_backend
            or load_i64(global_addr("pcc_gc_tracing_cycle_epoch"), 0)
            != extension_epoch
            or load_i32(global_addr("pcc_gc_backend_selected"), 0)
            != extension_backend
            or load_i32(global_addr("pcc_gc_mark_active"), 0) == 0
        ):
            pcc_py_gc_minor_graph_unlock()
            return local_processed
        store_i32(
            global_addr("pcc_gc_trace_extension_roots_pending"), 0, 0
        )

    node = global_load_ptr("pcc_gc_trace_cursor")
    if ptr_is_null(node) != 0:
        node = global_load_ptr("pcc_gc_object_head")
    while ptr_is_null(node) == 0 and local_processed < remaining_budget:
        nxt = load_ptr(node, 16)
        if load_i64(node, 32) != 0:
            node = nxt
            continue
        obj = load_ptr(node, 0)
        if ptr_is_null(obj) != 0 or is_tagged_int(obj) != 0:
            node = nxt
            continue
        flags: i64 = load_i32(obj, 12)
        if (flags & 16) != 0:
            if pcc_capi_is_cext_type_tag(load_i32(obj, 8)) != 0:
                py_incref(obj)
                epoch: i64 = load_i64(
                    global_addr("pcc_gc_tracing_cycle_epoch"), 0
                )
                backend: i64 = load_i32(
                    global_addr("pcc_gc_backend_selected"), 0
                )
                global_store_ptr("pcc_gc_trace_cext_pending_obj", obj)
                store_i64(
                    global_addr("pcc_gc_trace_cext_pending_epoch"), 0, epoch
                )
                store_i64(
                    global_addr("pcc_gc_trace_cext_pending_backend"), 0, backend
                )
                store_ptr(cext_ctx, 0, obj)
                store_i64(cext_ctx, 8, epoch)
                store_i64(cext_ctx, 16, backend)
                local_processed = local_processed + 1
                node = nxt
                break
            else:
                pcc_gc_trace_referents(obj)
                pcc_gc_gray_count_decrement_acq_rel()
                pcc_gc_set_mark_color(obj, 32)
                local_processed = local_processed + 1
        node = nxt
    global_store_ptr("pcc_gc_trace_cursor", node)

    cext_obj = load_ptr(cext_ctx, 0)
    if ptr_is_null(cext_obj) == 0:
        pcc_py_gc_minor_graph_unlock()
        _pcc_gc_trace_cext_complete_context(cext_ctx)
        return local_processed

    if ptr_is_null(global_load_ptr("pcc_gc_trace_cursor")) != 0:
        if pcc_gc_gray_count_load_acquire() != 0:
            global_store_ptr("pcc_gc_trace_cursor", global_load_ptr("pcc_gc_object_head"))
        elif (
            load_i64(
                global_addr("pcc_gc_tracing_finish_claim_epoch"), 0
            )
            == 0
        ):
            cycle_epoch: i64 = load_i64(
                global_addr("pcc_gc_tracing_cycle_epoch"), 0
            )
            if cycle_epoch > 0:
                cycle_backend: i64 = load_i32(
                    global_addr("pcc_gc_backend_selected"), 0
                )
                store_i64(
                    global_addr("pcc_gc_tracing_finish_claim_backend"),
                    0,
                    cycle_backend,
                )
                store_i64(
                    global_addr("pcc_gc_tracing_finish_claim_epoch"),
                    0,
                    cycle_epoch,
                )
                claim_epoch = cycle_epoch
                claim_backend = cycle_backend

    pcc_py_gc_minor_graph_unlock()
    if claim_epoch != 0:
        pcc_gc_complete_claimed_tracing_cycle(claim_epoch, claim_backend)
    return local_processed


@c_abi_export("pcc_gc_incremental_concurrent_step")
def pcc_gc_incremental_concurrent_step(budget: i64) -> i64:
    if budget <= 0:
        return 0
    steps: i64 = load_i64(global_addr("pcc_gc_metric_step"), 0)
    store_i64(global_addr("pcc_gc_metric_step"), 0, steps + 1)
    start_us: i64 = pcc_platform_monotonic_us()
    processed: i64 = pcc_gc_tracing_step_cycle(budget)
    pcc_gc_tracing_discharge_debt(processed)
    if load_i32(global_addr("pcc_gc_mark_active"), 0) == 0:
        if load_i32(global_addr("pcc_gc_cycle_requested"), 0) == 0:
            store_i64(global_addr("pcc_gc_debt_bytes"), 0, 0)
    pcc_gc_tracing_record_pause(start_us, pcc_platform_monotonic_us())
    return processed


@c_abi_export("pcc_gc_incremental_maybe_auto_step")
def pcc_gc_incremental_maybe_auto_step() -> None:
    if load_i32(global_addr("pcc_gc_in_auto_step"), 0) != 0:
        return
    if load_i32(global_addr("pcc_gc_backend_selected"), 0) != 1:
        return
    # GC1 marks on its collector thread, concurrently with every mutator;
    # allocation sites record debt there (pcc_gc_cms_note_alloc).
    pcc_gc_maybe_start_cms_worker()
    if pcc_threads_enabled() != 0:
        return
    debt: i64 = load_i64(global_addr("pcc_gc_debt_bytes"), 0)
    if debt < pcc_gc_tracing_debt_threshold():
        return
    store_i32(global_addr("pcc_gc_in_auto_step"), 0, 1)
    pcc_gc_incremental_concurrent_step(pcc_gc_tracing_budget_from_debt())
    store_i32(global_addr("pcc_gc_in_auto_step"), 0, 0)


@c_abi_export("pcc_gc_backend4_maybe_auto_step")
def pcc_gc_backend4_maybe_auto_step() -> None:
    # Called only after application arguments/locals have entered their root
    # frames. Allocation records debt but cannot move half-built objects.
    if pcc_thread_no_park_depth() != 0 or pcc_py_gc_minor_graph_lock_depth() != 0:
        return
    pcc_py_gc_minor_graph_lock()
    if (
        load_i32(global_addr("pcc_gc_backend_selected"), 0) != 4
        or load_i32(global_addr("pcc_gc_in_auto_step"), 0) != 0
        or load_i32(global_addr("pcc_gc_explicit_collect_active"), 0) != 0
        or load_i32(global_addr("pcc_gc_backend4_remap_active"), 0) != 0
        or load_i64(global_addr("pcc_gc_backend4_relocation_reset_owner"), 0) != 0
        or load_i64(global_addr("pcc_gc_backend4_reseed_page_count_owner"), 0) != 0
        or load_i64(global_addr("pcc_gc_backend4_reseed_commit_owner"), 0) != 0
        or load_i64(global_addr("pcc_gc_tracing_finish_claim_epoch"), 0) != 0
        or load_i32(global_addr("pcc_gc_trace_extension_roots_pending"), 0) != 0
    ):
        pcc_py_gc_minor_graph_unlock()
        return
    debt: i64 = atomic_load_i64(global_addr("pcc_gc_debt_bytes"), 0, "acquire")
    if debt < pcc_gc_tracing_debt_threshold():
        atomic_store_i32(global_addr("pcc_gc_backend4_auto_step_pending"), 0, 0, "release")
        pcc_py_gc_minor_graph_unlock()
        return
    budget: i64 = pcc_gc_tracing_budget_from_debt()
    store_i32(global_addr("pcc_gc_in_auto_step"), 0, 1)
    atomic_store_i32(global_addr("pcc_gc_backend4_auto_step_pending"), 0, 0, "release")
    pcc_py_gc_minor_graph_unlock()

    # Never wait for the stopped world while holding the object graph lock.
    # The stop spans copying, root remapping, retirement and user callbacks.
    stopped: i64 = pcc_stop_the_world()
    processed: i64 = 0
    if stopped == 0:
        processed = pcc_gc_step(budget)
        pcc_gc_tracing_discharge_debt(processed)
        if processed == 0:
            # An idle collector has no work to pay the debt with. Keeping the
            # debt re-armed this step at every safepoint: after one large
            # allocation each loop back-edge ran a stop-the-world step and a
            # full relocation-candidate ranking. Clear it so the next step
            # waits for another threshold of allocation.
            atomic_rmw_i64("xchg", global_addr("pcc_gc_debt_bytes"), 0, 0, "acq_rel")

    pcc_py_gc_minor_graph_lock()
    store_i32(global_addr("pcc_gc_in_auto_step"), 0, 0)
    debt = atomic_load_i64(global_addr("pcc_gc_debt_bytes"), 0, "acquire")
    if stopped != 0 or debt >= pcc_gc_tracing_debt_threshold():
        atomic_store_i32(global_addr("pcc_gc_backend4_auto_step_pending"), 0, 1, "release")
    pcc_py_gc_minor_graph_unlock()
    if stopped == 0:
        pcc_resume_world()


@c_abi_export("pcc_gc_cms_stop_worker")
def pcc_gc_cms_stop_worker() -> i64:
    # A collector callback cannot join its own thread, even outside the
    # graph lock. Busy lifecycle owners are refused, never waited on.
    if pcc_gc_is_cms_worker() != 0:
        return -1
    state: i64 = atomic_load_i32(
        global_addr("pcc_gc_cms_worker_started"), 0, "acquire"
    )
    if state == 0:
        if atomic_cas_i32(
            global_addr("pcc_gc_cms_worker_started"), 0, 0, 4,
            "acq_rel", "acquire",
        ) == 0:
            return 0
        return -1
    if state != 1:
        return -1
    if atomic_cas_i32(
        global_addr("pcc_gc_cms_worker_started"), 0, 1, 3,
        "acq_rel", "acquire",
    ) != 1:
        return -1
    handle = global_load_ptr("pcc_gc_cms_worker_handle")
    if ptr_is_null(handle) != 0:
        return -1
    atomic_store_i32(
        global_addr("pcc_gc_cms_worker_stop_requested"), 0, 1, "release"
    )
    if pcc_thread_join(handle, null()) != 0:
        # Join failure does not specify whether it freed the handle. Keep
        # state 3 quarantined: no retry, handle access, or new worker.
        return -1
    global_store_ptr("pcc_gc_cms_worker_handle", null())
    atomic_store_i32(
        global_addr("pcc_gc_cms_worker_started"), 0, 4, "release"
    )
    # The backend setter owns pause 4 until admission and publication finish.
    return 0


@c_abi_export("pcc_gc_cms_note_alloc")
def pcc_gc_cms_note_alloc(bytes: i64) -> None:
    """Record allocation debt for the tracing collector thread (GC1/GC2).

    The collector thread does the marking. A mutator assists only once its
    allocation outruns the collector (debt past four thresholds) and only
    while a cycle is active: it then marks one batch, which bounds the heap
    without making mutators begin cycles or stop the world themselves.
    """
    if bytes <= 0:
        bytes: i64 = 1
    atomic_rmw_i32(
        "add", global_addr("pcc_gc_cms_queue_pushes"), 0, 1, "release"
    )
    previous: i64 = atomic_rmw_i64("add", global_addr("pcc_gc_debt_bytes"), 0, bytes, "acq_rel")
    debt: i64 = previous + bytes
    # An assist is automatic collection too: none while gc.disable() or
    # threshold0 == 0 holds.
    if (
        debt >= pcc_gc_tracing_debt_threshold() * 4
        and load_i32(global_addr("pcc_gc_mark_active"), 0) != 0
        and load_i32(global_addr("py_gc_enabled"), 0) != 0
        and load_i32(global_addr("py_gc_threshold0"), 0) > 0
    ):
        atomic_rmw_i32("add", global_addr("pcc_gc_cms_mutator_assists"), 0, 1, "relaxed")
        budget: i64 = pcc_gc_tracing_budget_from_debt()
        if budget > 512:
            budget = 512
        commits: i64 = load_i64(global_addr("pcc_gc_tracing_finish_commits"), 0)
        processed: i64 = pcc_gc_tracing_step_cycle(budget)
        pcc_gc_tracing_discharge_debt(processed)
        if load_i64(global_addr("pcc_gc_tracing_finish_commits"), 0) != commits:
            # As on the collector thread: the next application gate sweeps it.
            atomic_store_i32(
                global_addr("pcc_gc_backend4_auto_step_pending"), 0, 1, "release"
            )


@c_abi_export("pcc_gc_note_alloc")
def pcc_gc_note_alloc(bytes: i64) -> None:
    backend: i64 = 0
    if load_i32(global_addr("pcc_gc_config_initialized"), 0) != 0:
        backend = load_i32(global_addr("pcc_gc_backend_selected"), 0)
    else:
        backend = pcc_gc_config_ensure()
    if bytes < 0:
        bytes: i64 = 0
    allocations: i64 = load_i32(global_addr("pcc_gc_metric_alloc"), 0)
    store_i32(global_addr("pcc_gc_metric_alloc"), 0, allocations + 1)
    if backend == 1 or backend == 2:
        # Both tracing collectors mark on the collector thread.
        pcc_gc_maybe_start_cms_worker()
        pcc_gc_cms_note_alloc(bytes)
    elif backend == 4:
        previous: i64 = atomic_rmw_i64("add", global_addr("pcc_gc_debt_bytes"), 0, bytes, "acq_rel")
        if previous + bytes >= pcc_gc_tracing_debt_threshold():
            atomic_store_i32(global_addr("pcc_gc_backend4_auto_step_pending"), 0, 1, "release")
