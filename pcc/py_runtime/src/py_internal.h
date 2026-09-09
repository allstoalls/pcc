/* pcc/py_runtime/src/py_internal.h
 *
 * Private layouts shared across the runtime .c files.
 * NOT part of the public ABI — only py_runtime.h is.
 */
#ifndef PY_INTERNAL_H
#define PY_INTERNAL_H

#include "../include/py_runtime.h"
#include <stdint.h>
#include <stddef.h>

/* Transitional C helpers that remain in the pcc-Python archive must observe
 * the same owned environment table as pcc-Python semantics.  The C-oracle
 * archive deliberately leaves this macro unset and continues to call host
 * libc, so this bridge does not make the oracle depend on the production
 * freestanding module. */
#ifdef PCC_USE_FREESTANDING_PLATFORM_ENV
char *pcc_platform_getenv(const char *name);
char **pcc_platform_env_snapshot(void);
void pcc_platform_env_snapshot_free(char **snapshot);
#define pcc_runtime_getenv pcc_platform_getenv
#else
#define pcc_runtime_getenv getenv
#endif

#ifdef PCC_USE_FREESTANDING_PLATFORM_TIME
int64_t pcc_platform_wall_time_us(void);
int64_t pcc_platform_monotonic_us(void);
int64_t pcc_platform_sleep_ns(int64_t delay_ns);
#endif

#ifdef PCC_USE_FREESTANDING_PLATFORM_PROCESS
int64_t pcc_platform_waitpid(int64_t pid, int32_t *status, int64_t options);
int64_t pcc_platform_kill(int64_t pid, int64_t signal_number);
void pcc_platform_exit(int64_t status);
void pcc_platform_abort(void);
#endif

#ifdef PCC_USE_FREESTANDING_PLATFORM_IO
int64_t pcc_platform_read(int64_t fd, void *buffer, int64_t size);
int64_t pcc_platform_write(int64_t fd, const void *buffer, int64_t size);
int64_t pcc_platform_close(int64_t fd);
#endif

#ifdef PCC_USE_FREESTANDING_PLATFORM_SOCKET
int64_t pcc_platform_tcp_connect(const char *host, const char *port);
int64_t pcc_platform_tcp_listen(
    const char *host, const char *port, int64_t reuse_port
);
int64_t pcc_platform_tcp_listen_with_backlog(
    const char *host, const char *port, int64_t reuse_port, int64_t backlog
);
int64_t pcc_platform_tcp_accept_observe(int64_t fd, int64_t *output_fd);
int64_t pcc_platform_tcp_connect_start(
    const char *host, const char *port, int64_t *output_fd
);
int64_t pcc_platform_socket_connect_observe(int64_t fd, int64_t timeout_ms);
int64_t pcc_platform_socket_read_observe(
    int64_t fd, void *buffer, int64_t size, int64_t flags,
    int64_t *output_count
);
int64_t pcc_platform_socket_write_observe(
    int64_t fd, const void *buffer, int64_t size, int64_t flags,
    int64_t *output_count
);
int64_t pcc_platform_socket_send(
    int64_t fd, const void *buffer, int64_t size, int64_t flags
);
int64_t pcc_platform_socket_recv(
    int64_t fd, void *buffer, int64_t size, int64_t flags
);
int64_t pcc_platform_socket_format_numeric_address(
    const void *address, int64_t length, char *output, int64_t capacity
);
int64_t pcc_platform_socket_peer_text(
    int64_t fd, char *output, int64_t capacity
);
#endif

/* ---- Flags ------------------------------------------------------------- */
#define PY_FLAG_IMMORTAL    0x1
#define PY_FLAG_GC_TRACKED  0x2
#define PY_FLAG_FINALIZED   0x4
#define PY_FLAG_GC_WHITE    0x8
#define PY_FLAG_GC_GRAY     0x10
#define PY_FLAG_GC_BLACK    0x20
#define PY_FLAG_GC_PINNED   0x40
#define PY_FLAG_GC_YOUNG    0x80
#define PY_FLAG_GC_OLD      0x100
#define PY_FLAG_GC_REMEMBERED 0x200
#define PY_FLAG_GC_SWEEP_CANDIDATE 0x400
#define PY_FLAG_GC_RELOCATION_CANDIDATE 0x800
#define PY_FLAG_GC_MINOR_ARENA 0x1000
#define PY_FLAG_GC_RELOCATION_TARGET 0x2000
#define PY_FLAG_GC_FRESH_ALLOC 0x4000
#define PY_FLAG_GC_LARGE_DEFERRED 0x8000
#define PY_FLAG_GC_ZPAGE_ALLOC 0x10000
/* Remap-phase epoch marker on relocated OLD shells: set at the remap
 * that healed the heap, consumed (entry retired) at the NEXT remap, so
 * stale SSA pointers from the in-between window still resolve. */
#define PY_FLAG_GC_FORWARD_RETIRING 0x20000
#define PY_FLAG_GC_MALLOC_ALLOC 0x40000
#define PY_FLAG_GC_DEALLOCATING 0x80000
/* Generator-only semantic bit: this generator is a compiler-proven
 * ``may_park`` continuation and may be transparently delegated by
 * pcc.virtual_thread.call.  Ordinary Python generators never carry it. */
#define PY_FLAG_GEN_MAY_PARK 0x100000
#define PY_FLAG_GC_COLOR_MASK \
    (PY_FLAG_GC_WHITE | PY_FLAG_GC_GRAY | PY_FLAG_GC_BLACK)

static inline int32_t py_header_flags_load(PyObjectHeader *h) {
    return __atomic_load_n(&h->flags, __ATOMIC_ACQUIRE);
}

static inline void py_header_flags_store(PyObjectHeader *h, int32_t flags) {
    __atomic_store_n(&h->flags, flags, __ATOMIC_RELEASE);
}

static inline void py_header_flags_or(PyObjectHeader *h, int32_t flags) {
    (void)__atomic_or_fetch(&h->flags, flags, __ATOMIC_ACQ_REL);
}

static inline void py_header_flags_and(PyObjectHeader *h, int32_t flags) {
    (void)__atomic_and_fetch(&h->flags, flags, __ATOMIC_ACQ_REL);
}

static inline int32_t py_header_flags_update(
    PyObjectHeader *h,
    int32_t clear_mask,
    int32_t set_mask
) {
    int32_t old_flags = py_header_flags_load(h);
    for (;;) {
        int32_t new_flags = (old_flags & ~clear_mask) | set_mask;
        if (__atomic_compare_exchange_n(
                &h->flags,
                &old_flags,
                new_flags,
                0,
                __ATOMIC_ACQ_REL,
                __ATOMIC_ACQUIRE
            )) {
            return new_flags;
        }
    }
}

/* ---- Type-specific deallocators (extern so py_obj.py can dispatch) -- */
void py_dealloc_int(PyObject *o);
void py_dealloc_float(PyObject *o);
void py_dealloc_str(PyObject *o);
void py_dealloc_list(PyObject *o);
void py_dealloc_tuple(PyObject *o);
void py_dealloc_dict(PyObject *o);
void py_dealloc_set(PyObject *o);
void py_dealloc_func(PyObject *o);
void py_dealloc_iter(PyObject *o);
void py_dealloc_gen(PyObject *o);
void py_dealloc_coroutine(PyObject *o);
void py_dealloc_continuation(PyObject *o);
void py_dealloc_task(PyObject *o);
void py_dealloc_memoryview(PyObject *o);
void py_dealloc_weakref(PyObject *o);
void py_dealloc_file(PyObject *o);
void py_dealloc_thread_lock(PyObject *o);
void py_dealloc_thread_rlock(PyObject *o);
void py_dealloc_thread_event(PyObject *o);
void py_dealloc_thread_condition(PyObject *o);
void py_dealloc_thread_semaphore(PyObject *o);
void py_dealloc_thread_thread(PyObject *o);
void py_dealloc_virtual_thread(PyObject *o);
void py_dealloc_vthread_channel(PyObject *o);
void py_dealloc_generic(PyObject *o);
void pcc_vthread_waiter_pool_note_allocation(void);
void pcc_vthread_waiter_pool_note_reuse(void);
void pcc_vthread_waiter_pool_note_cached(int64_t count);
void pcc_vthread_effect_note_waiter_root_enter(void);
void pcc_vthread_effect_note_waiter_root_leave(void);

/* ---- GC backend selector/telemetry internals ------------------------- */
void pcc_gc_note_alloc(int64_t bytes);
void *pcc_gc_try_minor_alloc(int64_t bytes);
void *pcc_gc_backend4_try_zpage_alloc(int64_t bytes, int32_t flags);
void pcc_gc_note_object_allocated(PyObject *o);
void pcc_gc_note_object_allocated_sized(PyObject *o, int64_t size);
void pcc_gc_note_object_freeing(PyObject *o);
void pcc_gc_free_object_memory(PyObject *o);
int64_t pcc_capi_is_cext_type_tag(int64_t type_tag);
int64_t pcc_capi_dealloc_cext_object(PyObject *o, int64_t type_tag);
PyObject *pcc_capi_call_cext_object(
    PyObject *callable,
    PyObject *args,
    PyObject *kwargs
);
PyObject *pcc_capi_cext_subtract(PyObject *left, PyObject *right);
PyObject *pcc_capi_cext_binary_number(
    PyObject *left,
    PyObject *right,
    int64_t op
);
PyObject *pcc_capi_cext_inplace_number(
    PyObject *left,
    PyObject *right,
    int64_t op
);
PyObject *pcc_capi_cext_absolute(PyObject *value);
int64_t pcc_capi_cext_truthy(PyObject *value);
int64_t pcc_capi_cext_richcompare_bool(
    PyObject *left,
    PyObject *right,
    int64_t op
);
int64_t pcc_capi_cext_object_is_callable(PyObject *callable);
PyObject *pcc_capi_cext_object_iter(PyObject *o);
PyObject *pcc_capi_cext_object_next(PyObject *o);
int64_t pcc_capi_cext_object_is_iterator(PyObject *o);
PyObject *pcc_capi_cext_object_getitem(PyObject *o, PyObject *key);
int64_t pcc_capi_cext_object_setitem(
    PyObject *o,
    PyObject *key,
    PyObject *value
);
/* len() for a cext object; -1 when the type exposes no length slot. */
int64_t pcc_capi_cext_object_length(PyObject *o);
/* tp_repr text for a cext object; NULL when the type has no repr slot. */
PyObject *pcc_capi_cext_object_repr(PyObject *o);
PyObject *pcc_capi_cext_object_getattr(PyObject *o, const char *name);
int64_t pcc_capi_cext_object_setattr(
    PyObject *o,
    const char *name,
    PyObject *value
);
PyObject *pcc_capi_call_type_object(
    PyObject *callable,
    PyObject *args,
    PyObject *kwargs
);
int64_t pcc_capi_type_object_is_callable(PyObject *callable);
int64_t pcc_capi_is_type_object_value(PyObject *value);
int64_t pcc_capi_type_object_issubclass(PyObject *derived, PyObject *cls);
PyObject *pcc_capi_type_object_getattr(PyObject *type_object, const char *name);
PyObject *pcc_capi_builtin_object_getattr(PyObject *o, const char *name);
void pcc_gc_note_load(void);
PyObject *pcc_gc_note_relocation_read(PyObject *o);
void pcc_gc_note_store(void);
void pcc_gc_root_slot_lock(void);
void pcc_gc_root_slot_unlock(void);
void pcc_gc_note_safepoint(void);
void pcc_gc_note_pin(int32_t delta);
void pcc_gc_note_write_barrier(PyObject *owner, PyObject *value);
void pcc_gc_note_slot_write_barrier(
    PyObject *owner,
    PyObject **slot,
    PyObject *value
);
/* Internal trusted-provenance store.  The caller must prove that value is the
 * current pointer for a freshly allocated native PY_TYPE_INSTANCE.  The
 * generic slot barrier and borrowed-value retain semantics are unchanged. */
void pcc_gc_store_ptr_fresh_native_instance(
    PyObject *owner,
    PyObject **slot,
    PyObject *value
);
/* Internal split-phase root publication.  The plan owns every scalar and
 * refcount token needed after the caller releases its outermost graph lock;
 * it is deliberately absent from the public py_runtime.h ABI. */
typedef struct {
    uint64_t opaque[16];
} PccGcStoreRootPlan;
typedef struct {
    uint64_t opaque[7];
} PccGcRetainPlan;
PyObject *pcc_gc_retain_plan_prepare_locked(
    PccGcRetainPlan *plan,
    PyObject *value
);
void pcc_gc_retain_plan_finish(PccGcRetainPlan *plan);
void pcc_gc_store_root_plan_init(
    PccGcStoreRootPlan *plan,
    int64_t backend
);
void pcc_gc_store_ptr_plan_init(
    PccGcStoreRootPlan *plan,
    PyObject *owner,
    int64_t backend
);
int64_t pcc_gc_store_root_plan_commit_locked(
    PccGcStoreRootPlan *plan,
    PyObject **slot,
    PyObject *value
);
void pcc_gc_store_root_plan_finish(PccGcStoreRootPlan *plan);
int64_t pcc_gc_store_ptr_plan_commit_locked(
    PccGcStoreRootPlan *plan,
    PyObject *owner,
    PyObject **slot,
    PyObject *value
);
int64_t pcc_gc_store_ptr_plan_commit_sentinel_aware_locked(
    PccGcStoreRootPlan *plan,
    PyObject *owner,
    PyObject **slot,
    PyObject *value,
    PyObject *sentinel
);
void pcc_gc_store_ptr_plan_finish(PccGcStoreRootPlan *plan);
int64_t pcc_gc_backend4_retarget_mutator_payload_locked(
    PyObject *owner,
    void *old_base,
    int64_t old_size_bytes,
    void *new_base,
    int64_t new_size_bytes,
    void *slot_pairs,
    int64_t pair_count
);
int64_t pcc_gc_backend4_remap_and_retire_stopped_world(void);
int64_t pcc_gc_backend4_step_remembered_roots(int64_t budget);
int64_t pcc_gc_backend4_relocation_set_add(PyObject *obj);
void pcc_gc_thread_unregister_buffers(void);
int64_t pcc_gc_frame_node_tls_pool_cached_count(void);
void pcc_gc_frame_node_tls_pool_drain(void);
int64_t pcc_gc_has_tracing_sweep(void);
int64_t pcc_gc_sweep_owed(void);
int64_t pcc_gc_collect_tracing(void);
void pcc_gc_begin_explicit_tracing_collect(void);
void pcc_gc_end_explicit_tracing_collect(void);
int32_t pcc_gc_explicit_collect_is_active(void);
typedef void (*PccGcRootVisitor)(PyObject *root, void *ctx);
void pcc_gc_visit_runtime_roots(PccGcRootVisitor visit, void *ctx);
void pcc_gc_note_frame_enter(const void *frame_map, PyObject **slots);
void pcc_gc_note_frame_leave(PyObject **slots);
void pcc_gc_note_frame_enter_lifo(const void *frame_map, PyObject **slots);
void pcc_gc_note_frame_leave_lifo(PyObject **slots);

/* ---- Threading/refcount substrate internals --------------------------- */
typedef void *(*PccThreadMain)(void *arg);
typedef struct PccThreadHandle PccThreadHandle;
typedef struct PccMutex PccMutex;
typedef struct PccCond PccCond;

/* Runtime log sink used by C GC/allocation hot paths. The implementation is
 * guarded by PCC_LOG at runtime, so calls are cheap when disabled. */
int  pcc_runtime_log_enabled(const char *category);
void pcc_runtime_log_event(const char *category, const char *event,
                           int64_t value0, int64_t value1, const void *ptr);
/* Integer-coded variant used by pcc-Python runtime ports that cannot cheaply
 * materialize borrowed C string literals in every hot path. */
extern int32_t pcc_runtime_log_fast_state;
void pcc_runtime_log_event_code(int32_t category, int32_t event, int64_t value0, int64_t value1, const void *ptr);

/* ---- Production-safe runtime tripwires --------------------------------
 *
 * PCC_RT_TRIPWIRE(cond, msg) asserts a C-KERNEL object-graph invariant:
 * object-header sanity, refcount discipline, and type-tag range at
 * collector / decref / trace fan-out points. These are machine-boundary
 * safety checks only (never Python semantics), so they are C-only and have
 * NO pcc-Python mirror.
 *
 * Production-safe by construction:
 *   - Default (production) build does NOT define PCC_RUNTIME_TRIPWIRES, so
 *     the macro expands to ((void)0): zero cost, zero behavior change, and
 *     `cond` is not even evaluated.
 *   - Build with -DPCC_RUNTIME_TRIPWIRES to arm the checks. On a violated
 *     invariant the message (with __FILE__/__LINE__) is routed through the
 *     existing pcc_runtime_log entrypoint (category "runtime") and the
 *     process aborts.
 *
 * The macro wraps its body in do/while(0) so it is a valid statement in
 * every C context, evaluates `cond` exactly once, and does not depend on
 * libpython. The fatal sink lives in pcc_runtime_log.c (the logging TU) so
 * the abort path reuses the single logging entrypoint rather than inventing
 * a second one. */
void pcc_runtime_tripwire_fail(const char *msg, const char *file, int32_t line);
/* One cross-TU mixed-tripwire seam for helper files whose checks can fire
 * while their caller owns the GC graph lock: armed builds defer the fatal
 * report to the outer unlock and return 1; unlocked callers fail directly
 * and do not return.  Unarmed builds compile every call site out. */
int pcc_gc_tripwire_defer_or_fail(
    const char *msg,
    const char *file,
    int32_t line
);

#ifdef PCC_RUNTIME_TRIPWIRES
/* Cross-TU mixed tripwire for helper files whose checks can fire while
 * their caller owns the GC graph lock.  Same shape as PCC_RT_TRIPWIRE but
 * a graph-lock owner records the violation in the deferred slot and the
 * enclosing VOID function returns instead of entering the log sink while
 * locked; unlocked callers fail immediately.  Unarmed builds compile the
 * macro out entirely. */
#define PCC_GC_OWNER_TRIPWIRE(cond, msg)                                   \
    do {                                                                   \
        if (!(cond)                                                        \
            && pcc_gc_tripwire_defer_or_fail(                              \
                (msg), __FILE__, __LINE__                                  \
            )) {                                                           \
            return;                                                        \
        }                                                                  \
    } while (0)
#else
#define PCC_GC_OWNER_TRIPWIRE(cond, msg) ((void)0)
#endif

#ifdef PCC_RUNTIME_TRIPWIRES
#define PCC_RT_TRIPWIRE(cond, msg)                                           \
    do {                                                                     \
        if (!(cond)) {                                                       \
            pcc_runtime_tripwire_fail((msg), __FILE__, __LINE__);            \
        }                                                                    \
    } while (0)
#else
#define PCC_RT_TRIPWIRE(cond, msg) ((void)0)
#endif

int64_t pcc_refcount_incref(int64_t *slot);
int64_t pcc_refcount_decref(int64_t *slot);
int64_t pcc_refcount_load(int64_t *slot);
void    pcc_refcount_forget(int64_t *slot);

int64_t pcc_thread_start(
    PccThreadHandle **out,
    PccThreadMain entry,
    void *arg
);
int64_t pcc_thread_join(PccThreadHandle *thread, void **result);
void    pcc_thread_detach(PccThreadHandle *thread);

PccMutex *pcc_mutex_new(void);
void      pcc_mutex_free(PccMutex *mutex);
int64_t   pcc_mutex_lock(PccMutex *mutex);
int64_t   pcc_mutex_unlock(PccMutex *mutex);

PccCond  *pcc_cond_new(void);
void      pcc_cond_free(PccCond *cond);
int64_t   pcc_cond_wait(PccCond *cond, PccMutex *mutex);
int64_t   pcc_cond_timedwait_ms(PccCond *cond, PccMutex *mutex, int64_t timeout_ms);
int64_t   pcc_cond_signal(PccCond *cond);
int64_t   pcc_cond_broadcast(PccCond *cond);

/* ---- GC tracker nodes for the refcount+cycle collector ---------------- */
typedef struct PyGcNode {
    PyObject *obj;
    int64_t gc_refs;
    int32_t reachable;
    struct PyGcNode *prev;
    struct PyGcNode *next;
} PyGcNode;

PyGcNode *py_gc_index_find(PyObject *obj);
int64_t py_gc_index_insert(PyObject *obj, PyGcNode *node);
PyGcNode *py_gc_index_remove(PyObject *obj);
void *pcc_gc_object_index_find(PyObject *obj);
int64_t pcc_gc_object_index_insert(PyObject *obj, void *node);
int64_t pcc_gc_object_index_plan_capacity(int64_t extra);
int64_t pcc_gc_object_index_plan_commit(
    void **prepared_slots_io, int64_t prepared_cap, int64_t extra
);
int64_t pcc_gc_object_index_insert_preallocated(PyObject *obj, void *node);
void *pcc_gc_object_index_remove(PyObject *obj);
void pcc_gc_object_index_clear(void);
void *pcc_gc_object_node_prepare(void);
int64_t pcc_gc_object_node_plan_requires_prepare(void);
void *pcc_gc_object_node_take_prepared(void **prepared_io);
int64_t pcc_gc_managed_pointer_index_contains(PyObject *obj);
int64_t pcc_gc_managed_pointer_index_insert(PyObject *obj);
int64_t pcc_gc_managed_pointer_index_remove(PyObject *obj);
void pcc_gc_ptr_index_tls_pool_drain(void);
typedef enum {
    PY_OBJ_SLOT_OWNED = 1,
    PY_OBJ_SLOT_BORROWED_TRACED = 2,
    PY_OBJ_SLOT_BORROWED_UPDATE_ONLY = 3
} PyObjSlotRole;
typedef void (*PyObjSlotVisitor)(
    PyObject **slot,
    int32_t role,
    void *ctx
);
typedef void (*PccPyObjSlotVisitorI64)(
    PyObject **slot,
    int64_t role,
    void *ctx
);
int py_obj_visit_slots(PyObject *o, PyObjSlotVisitor visit, void *ctx);
int64_t pcc_gc_visit_object_slots_slice(
    PyObject *o,
    int64_t cursor,
    int64_t limit,
    PyObjSlotVisitor visit,
    void *ctx,
    int64_t *state_out
);
void py_obj_update_slot(PyObject **slot);
int pcc_capi_visit_cext_object_slots(
    PyObject *o,
    PyObjSlotVisitor visit,
    void *ctx
);
int pcc_capi_visit_cext_object_slots_i64(
    PyObject *o,
    PccPyObjSlotVisitorI64 visit,
    void *ctx
);
/* Slot-address referent walker for the backend-4 remap phase
 * (docs/plans/gc4-relocation-remap-plan.md). Coverage mirrors
 * pcc_gc_trace_referents. */
void pcc_gc_update_referents(PyObject *o, void (*update)(PyObject **slot));
void *pcc_gc_forwarding_index_find(PyObject *obj);
int64_t pcc_gc_forwarding_index_insert(PyObject *obj, void *node);
void *pcc_gc_forwarding_index_remove(PyObject *obj);
void pcc_gc_forwarding_index_clear(void);
void *pcc_gc_forwarding_target_index_find(PyObject *obj);
int64_t pcc_gc_forwarding_target_index_insert(PyObject *obj, void *node);
int64_t pcc_gc_forwarding_target_index_upsert(PyObject *obj, void *node);
void *pcc_gc_forwarding_target_index_remove(PyObject *obj);
void pcc_gc_forwarding_target_index_clear(void);
void *pcc_gc_identity_index_find(PyObject *obj);
int64_t pcc_gc_identity_index_insert(PyObject *obj, void *node);
void *pcc_gc_identity_index_remove(PyObject *obj);
void pcc_gc_identity_index_clear(void);
int64_t pcc_gc_forwarding_plan_index_capacity(int64_t kind, int64_t extra);
int64_t pcc_gc_forwarding_plan_index_commit(
    int64_t kind,
    void **prepared_slots_io,
    int64_t prepared_cap,
    int64_t extra
);
int64_t pcc_gc_forwarding_plan_index_insert(
    int64_t kind,
    PyObject *obj,
    void *node
);
void *pcc_gc_frame_index_find(void *slots);
int64_t pcc_gc_frame_index_insert(void *slots, void *node);
int64_t pcc_gc_frame_index_plan_capacity(int64_t extra);
int64_t pcc_gc_frame_index_plan_commit(
    void **prepared_slots_io,
    int64_t prepared_cap,
    int64_t extra
);
void *pcc_gc_frame_index_replace(void *slots, void *node);
void *pcc_gc_frame_index_replace_preallocated(void *slots, void *node);
void *pcc_gc_frame_index_remove(void *slots);
void pcc_gc_frame_index_clear(void);
extern int32_t pcc_gc_read_barrier_enabled;
void *pcc_gc_zpage_owner_index_find(PyObject *obj);
int64_t pcc_gc_zpage_owner_index_insert(PyObject *obj, void *node);
int64_t pcc_gc_zpage_owner_index_upsert(PyObject *obj, void *node);
int64_t pcc_gc_zpage_owner_index_plan_capacity(int64_t extra);
int64_t pcc_gc_zpage_owner_index_plan_commit(
    void **prepared_slots_io, int64_t prepared_cap, int64_t extra
);
int64_t pcc_gc_zpage_owner_index_upsert_preallocated(
    PyObject *obj, void *node
);
void *pcc_gc_backend4_zpage_node_prepare(void);
int64_t pcc_gc_backend4_zpage_node_plan_requires_prepare(void);
void *pcc_gc_backend4_zpage_node_take_prepared(void **prepared_io);
int64_t pcc_gc_backend4_zpage_link_node_preallocated(void *node);
void *pcc_gc_backend4_zpage_track_page_prepare(
    void *page, PyObject *owner, int64_t size
);
void *pcc_gc_backend4_zpage_track_alloc_preallocated(
    PyObject *owner,
    int64_t size,
    void **prepared_node_io,
    void **prepared_page_io,
    int64_t prepared_page_from_free
);
void *pcc_gc_zpage_owner_index_remove(PyObject *obj);
void pcc_gc_zpage_owner_index_clear(void);
void *pcc_gc_zpage_page_index_find(void *page);
int64_t pcc_gc_zpage_page_index_insert(void *page, void *node);
int64_t pcc_gc_zpage_page_index_upsert(void *page, void *node);
void *pcc_gc_zpage_page_index_remove(void *page);
void pcc_gc_zpage_page_index_clear(void);
int64_t pcc_gc_object_is_known(PyObject *obj);
int64_t pcc_gc_object_is_known_no_lock(PyObject *obj);
int64_t pcc_gc_pointer_is_managed_no_lock(PyObject *obj);
int64_t pcc_gc_backend4_slot_needs_resolve(PyObject *value);
int64_t pcc_gc_forwarding_population_load(void);
int64_t pcc_gc_relocation_set_active_load(void);
int64_t pcc_gc_slot_is_runtime_root(PyObject **slot);

/* ---- Tagged-int helpers ------------------------------------------------ */
/* Encoding: low bit = 1 means value; shift right arithmetic to recover the
 * int63 payload. Low bit = 0 means a real PyObject* pointer. Since malloc
 * returns at least 8-byte-aligned pointers on every platform we care about,
 * bit 0 of a real pointer is always 0. */

#define PY_IS_TAGGED_INT(p)  (((uintptr_t)(p) & 1u) == 1u)

/* Encode int63 payload (already range-checked) into a tagged PyObject*. */
static inline PyObject *py_tag_int(int64_t v) {
    /* Shift left by 1, set low bit. */
    return (PyObject *)(uintptr_t)(((uint64_t)v << 1) | 1u);
}

/* Decode tagged int payload via arithmetic right shift (sign-preserving). */
static inline int64_t py_untag_int(PyObject *p) {
    /* Cast to intptr_t so >>1 is arithmetic. */
    return (int64_t)((intptr_t)(uintptr_t)p >> 1);
}

/* Tagged-int range: 63-bit signed. */
#define PY_TAGGED_INT_MIN  ((int64_t)INT64_MIN >> 1)
#define PY_TAGGED_INT_MAX  ((int64_t)INT64_MAX >> 1)

/* ---- Concrete object layouts ------------------------------------------ */

/* The public PyObject is opaque; concretely every real (heap) object
 * starts with a PyObjectHeader and is followed by type-specific fields. */

/* Heap-allocated int: bignum representation (Phase 2).
 *
 * Sign-magnitude bignum with base 2^32 digits stored little-endian
 * (digits[0] is least significant). `sign` is -1, 0, or +1. `ndigits` is the
 * number of meaningful digits; the value 0 is encoded as sign=0, ndigits=0.
 *
 * Invariants (maintained by all constructors/operations that return a
 * canonical bignum):
 *   - If sign == 0: ndigits == 0.
 *   - If sign != 0: ndigits >= 1 and digits[ndigits - 1] != 0.
 *
 * Values that fit in the tagged range (PY_TAGGED_INT_MIN .. PY_TAGGED_INT_MAX)
 * should be stored as tagged ints, not as PyIntObjects. Routines that build
 * a bignum but want the canonical PyObject form go through
 * py_bigint_to_pyobject().
 */
typedef struct {
    PyObjectHeader h;
    int32_t  sign;      /* -1, 0, +1 */
    int32_t  ndigits;   /* number of base-2^32 digits in use */
    uint32_t digits[];  /* flexible array — length == ndigits */
} PyIntObject;

typedef struct {
    PyObjectHeader h;
    double value;
} PyFloatObject;

typedef struct {
    PyObjectHeader h;
    double real;
    double imag;
} PyComplexObject;

typedef struct {
    PyObjectHeader h;
    int64_t byte_len;
    char data[];
} PyBytesObject;

typedef struct {
    PyObjectHeader h;
    int64_t byte_len;
    char data[];
} PyByteArrayObject;

typedef struct {
    PyObjectHeader h;
    PyObject *base;
} PyMemoryViewObject;

/* PyStrObject: UTF-8 encoded string.
 *
 * Phase 2 layout uses a flexible-array-member tail for the UTF-8 payload,
 * so the bytes + NUL terminator live inline with the header. This removes
 * a second allocation and makes bounds/hash/len lookups cache-friendly.
 *
 *   sizeof(PyStrObject) = header + 3 * int64
 *   total alloc size    = sizeof(PyStrObject) + byte_len + 1
 */
typedef struct {
    PyObjectHeader h;
    int64_t byte_len;   /* UTF-8 bytes, not codepoints */
    int64_t cp_len;     /* cached codepoint count, -1 = unset (lazy) */
    int64_t hash;       /* cached FNV-1a hash, -1 if not yet computed */
    char    data[];     /* UTF-8 bytes followed by NUL terminator */
} PyStrObject;

PyObject *py_str_new(const char *data, int64_t byte_len);
int64_t py_str_byte_len(PyObject *s);
const char *py_str_utf8(PyObject *s);
PyObject *py_obj_repr(PyObject *o);
PyObject *py_obj_ascii(PyObject *o);
PyObject *py_obj_str(PyObject *o);
PyObject *py_format_obj_to_str(PyObject *o, int use_repr);
PyObject *py_float_to_str_obj(PyObject *o);

typedef struct {
    PyObjectHeader h;
    int64_t length;
    int64_t capacity;
    PyObject **items;   /* owned array of owned refs */
} PyListObject;

/* Tuple: header + len + inline flexible array of owned refs.
 *
 * Phase 2 switches to a flexible array member so the header and the items
 * live in one contiguous allocation. This matches the contract exactly
 * (see docs/plans/python-frontend-interfaces.md §3) and saves one malloc
 * per tuple. Deallocation walks items[0..len-1] and frees the whole block.
 */
typedef struct {
    PyObjectHeader h;
    int64_t  len;
    PyObject *items[];   /* flexible array — owned refs */
} PyTupleObject;

/* ---- Dict -------------------------------------------------------------- */
/* Dict entry (insertion-ordered compact array). */
typedef struct {
    int64_t   hash;
    PyObject *key;      /* NULL marks a dead entry (deleted post-insert) */
    PyObject *value;
} DictEntry;

/* Dict with split index-table + insertion-ordered entries array
 * (PEP 468 / PEP 509 compact-dict style).
 *
 *   indices[]     : probe table, size = capacity (power of 2).
 *                   Each cell holds an index into entries[], or:
 *                     -1  (PY_DICT_EMPTY)     -> slot has never held a key
 *                     -2  (PY_DICT_TOMBSTONE) -> slot held a deleted key
 *   entries[]     : insertion order preserved; length == capacity so we
 *                   never overflow while honoring the 2/3 load factor.
 *   entries_used  : high-water mark into entries[] (including dead slots
 *                   created by del). Next insert lands at
 *                   entries[entries_used]. Rehash compacts the array.
 *   size          : number of live entries.
 */
typedef struct {
    PyObjectHeader h;
    int64_t     size;
    int64_t     capacity;
    int64_t    *indices;
    DictEntry  *entries;
    int64_t     entries_used;
} PyDictObject;

#define PY_DICT_EMPTY      ((int64_t)-1)
#define PY_DICT_TOMBSTONE  ((int64_t)-2)

/* ---- Set --------------------------------------------------------------- */
/* Set entry — open-addressing table with no value side.
 *
 *   key == NULL            -> empty slot (never written)
 *   key == py_set_dummy    -> tombstone (deleted)
 *   otherwise              -> live entry with cached `hash`
 */
typedef struct {
    int64_t   hash;
    PyObject *key;
} SetEntry;

typedef struct {
    PyObjectHeader h;
    int64_t   size;        /* live entries */
    int64_t   capacity;    /* power of 2 */
    int64_t   fill;        /* live + tombstones; triggers rehash */
    SetEntry *entries;
} PySetObject;

/* Sentinel pointer used as a set tombstone. Distinct from any heap object
 * (it does not carry a valid header) and from any tagged int (low bit is
 * always zero on a static-data address). py_incref/py_decref must never
 * see it — py_set code is responsible for keeping it out of their way.
 * Defined in py_set.c. */
extern PyObject *const py_set_dummy;

/* ---- Class / Instance (Phase 3) --------------------------------------- */

/* Method table entry (name -> PyObject* function). For Phase 3 the stored
 * PyObject is not a real callable PyObject yet; it is a cast of the LLVM
 * function pointer the codegen emitted. py_obj_call checks class lookups
 * and invokes the pointer through the right ABI trampoline. */
typedef struct PyClassMethod {
    const char *name;
    PyObject   *func;       /* borrowed — points at a user_* LLVM function */
} PyClassMethod;

/* Class object: describes a user-defined Python class.
 *
 * Layout matches docs/plans/python-frontend-interfaces.md §3 addendum.
 * Every field is set at module-init time by the codegen-emitted class
 * initializer function; nothing in here is mutated after init.
 *
 *   name              : interned C string; never freed.
 *   bases / n_bases   : direct bases in declaration order.
 *   mro / n_mro       : C3 linearization; mro[0] == this class,
 *                       mro[n_mro - 1] == <root object> (or NULL if we
 *                       never installed a root).
 *   methods           : array of {name, PyObject* func}. Lookup is linear;
 *                       Phase 3 doesn't bother with a hash table because
 *                       classes are small and the cost is dwarfed by call.
 *   field_names       : declared instance-field names in slot order.
 *   instance_size     : total bytes of a PyInstanceObject carrying
 *                       n_fields slots.
 *   type_tag_alloc    : the type tag allocated for this class
 *                       (PY_TYPE_USER_CLASS_START + n). Tags immediately
 *                       above PY_TYPE_USER are reserved for descriptors and
 *                       must never enter instance-layout dispatch. Instances
 *                       carry this allocated tag in their header so
 *                       isinstance and dispatch stay O(1) for pointer
 *                       identity checks.
 */
typedef struct PyClassObject {
    PyObjectHeader           h;
    const char              *name;
    int32_t                  n_bases;
    struct PyClassObject   **bases;
    int32_t                  n_mro;
    struct PyClassObject   **mro;
    int32_t                  n_methods;
    PyClassMethod           *methods;
    int32_t                  n_fields;
    const char             **field_names;
    int32_t                  instance_size;
    int32_t                  type_tag_alloc;
    /* Borrowed update-only alias of the method-table __del__ entry.  GC
     * forwarding rewrites it, but semantic dispatch always performs the MRO
     * lookup so this alias cannot become a second finalizer cache policy. */
    PyObject               *del_method;
    PyObject               *attrs;      /* owned dict for class-level variables */
    struct PyClassObject   *metaclass;  /* borrowed class object for type attrs */
} PyClassObject;

#define PCC_ASSERT_CLASS_OFFSET(field, expected) \
    _Static_assert(offsetof(PyClassObject, field) == (expected), \
                   "PyClassObject." #field " offset drift")

_Static_assert(sizeof(PyClassObject) == 120,
               "PyClassObject size drift from pcc-Python ABI");
PCC_ASSERT_CLASS_OFFSET(h, 0);
PCC_ASSERT_CLASS_OFFSET(name, 16);
PCC_ASSERT_CLASS_OFFSET(n_bases, 24);
PCC_ASSERT_CLASS_OFFSET(bases, 32);
PCC_ASSERT_CLASS_OFFSET(n_mro, 40);
PCC_ASSERT_CLASS_OFFSET(mro, 48);
PCC_ASSERT_CLASS_OFFSET(n_methods, 56);
PCC_ASSERT_CLASS_OFFSET(methods, 64);
PCC_ASSERT_CLASS_OFFSET(n_fields, 72);
PCC_ASSERT_CLASS_OFFSET(field_names, 80);
PCC_ASSERT_CLASS_OFFSET(instance_size, 88);
PCC_ASSERT_CLASS_OFFSET(type_tag_alloc, 92);
PCC_ASSERT_CLASS_OFFSET(del_method, 96);
PCC_ASSERT_CLASS_OFFSET(attrs, 104);
PCC_ASSERT_CLASS_OFFSET(metaclass, 112);
_Static_assert(sizeof(PyClassMethod) == 16, "PyClassMethod size drift");
_Static_assert(offsetof(PyClassMethod, name) == 0,
               "PyClassMethod.name offset drift");
_Static_assert(offsetof(PyClassMethod, func) == 8,
               "PyClassMethod.func offset drift");

#undef PCC_ASSERT_CLASS_OFFSET

/* Instance object: header + pointer to class + flexible field slot array.
 *
 * Each slot holds a PyObject* (owned reference; may be NULL if the field
 * has not been assigned yet). Deallocation walks fields[0..n_fields - 1]
 * and decrements each non-NULL slot before freeing the block.
 */
typedef struct PyInstanceObject {
    PyObjectHeader          h;
    PyClassObject          *cls;
    PyObject               *fields[];
} PyInstanceObject;

/* Shared module class owned by the compiled-module runtime layer. */
PyClassObject *pcc_runtime_module_class(void);

/* ValueBox objects are runtime object boxes for valueclass payloads at
 * object/object-boundary crossings. The layout matches PyInstanceObject so
 * that slots, GC tracing, and class dispatch can reuse instance behavior.
 */
typedef PyInstanceObject PyValueBoxObject;

/* ---- Native function object -------------------------------------------- */

typedef PyObject *(*PyNativeFuncEntry)(PyObject *captures, PyObject *args);

typedef struct {
    PyObjectHeader h;
    /* CPython-compatible PyCFunctionObject prefix. Native Python functions
     * leave these fields null; C-extension method wrappers populate
     * capi_method/capi_self so direct fake-header field reads are valid. */
    void *capi_method;
    PyObject *capi_self;
    PyObject *capi_module;
    PyObject *capi_weakreflist;
    void *capi_vectorcall;
    /* pcc-private native-function payload follows the public C-API prefix. */
    PyNativeFuncEntry entry;
    PyObject *captures;
    const char *name;
    PyObject *self_obj;
    PyObject *attrs;
} PyFuncObject;

typedef struct PyWeakRefObject {
    PyObjectHeader h;
    PyObject *target;      /* borrowed weak target; NULL after invalidation */
    PyObject *callback;    /* owned, may be NULL */
    struct PyWeakRefObject *prev;
    struct PyWeakRefObject *next;
} PyWeakRefObject;

/* ---- Native generator object ------------------------------------------- */

typedef PyObject *(*PyNativeGenResume)(PyObject *gen, PyObject *frame);

typedef struct {
    PyObjectHeader h;
    PyNativeGenResume resume;
    PyObject *frame;
    int64_t state;
    int64_t done;
    PyObject *send_value;
} PyGenObject;

typedef struct {
    PyObjectHeader h;
    PyObject *coro;
    PyObject *result;
    PyObject *waiter;
    int64_t done;
} PyTaskObject;

/* All PY_TYPE_VTHREAD_CHANNEL variants share this prefix. */
typedef struct {
    PyObjectHeader h;
    int64_t kind;
} PyVThreadChannelObject;

/* Sender/receiver endpoint layout (40 bytes). */
typedef struct {
    PyObjectHeader h;
    int64_t kind;
    PyObject *core;
    int64_t closed;
} PyVThreadChannelEndpointObject;

/* Shared bounded-channel layout.  The capacity slots are part of the GC
 * allocation itself, not a separately owned payload, so relocation can use
 * the ordinary object copy plus slot-pair remapping contract. */
typedef struct {
    PyObjectHeader h;
    int64_t kind;
    int64_t capacity;
    int64_t length;
    int64_t head;
    int64_t tail;
    int64_t sender_count;
    int64_t receiver_closed;
    int64_t oneshot;
    int64_t oneshot_sent;
    void *send_head;
    void *send_tail;
    void *recv_head;
    void *recv_tail;
    int64_t flags;
    PyObject *items[];
} PyVThreadChannelCoreObject;

_Static_assert(
    sizeof(PyVThreadChannelEndpointObject) == 40,
    "PyVThreadChannelEndpointObject ABI"
);
_Static_assert(
    offsetof(PyVThreadChannelEndpointObject, core) == 24,
    "PyVThreadChannelEndpointObject.core ABI"
);
_Static_assert(
    sizeof(PyVThreadChannelCoreObject) == 128,
    "PyVThreadChannelCoreObject ABI"
);
_Static_assert(
    offsetof(PyVThreadChannelCoreObject, items) == 128,
    "PyVThreadChannelCoreObject.items ABI"
);

struct PyContinuationStackChunk {
    int32_t root_map_slot_count;
    int32_t reserved;
    int64_t slot_count;
    PyObject **slots;
    /* The continuation-root registration node, so unregistering is O(1)
     * instead of a walk of the whole root list.  Appended: the leading bytes
     * double as this continuation's frame map (see
     * continuation_chunk_frame_map), so nothing may be inserted before
     * ``slots``.  The pcc-Python mirror in py_coroutine.py calloc's 32 bytes
     * and addresses this field at [24]. */
    void *root_node;
};

_Static_assert(
    sizeof(struct PyContinuationStackChunk) == 32,
    "PyContinuationStackChunk ABI drift against the pcc-Python mirror"
);
_Static_assert(
    offsetof(struct PyContinuationStackChunk, root_node) == 24,
    "PyContinuationStackChunk.root_node ABI drift"
);

typedef struct {
    PyObjectHeader h;
    PyObject *continuation;
    PyObject *result;
    int64_t state;
    int64_t queued;
    int64_t pinned;
    /* Non-GC backpointer to the active scheduler timer node.  The node owns
     * the GC root; this pointer exists only so cancel/complete/unpark can
     * remove that registration immediately.  It must be NULL when unqueued. */
    void *timer_entry;
    /* Non-GC backpointer to the active scheduler IO-wait node. As with the
     * timer backpointer, the node owns the registered GC root and this field
     * must be NULL whenever the virtual thread is not IO-queued. */
    void *io_entry;
    /* Task-local terminal payload.  An uncaught worker exception is moved
     * out of carrier TLS into this traced slot before the scheduler continues
     * with unrelated ready work. */
    PyObject *exception;
    /* PCC_VTHREAD_OUTCOME_*; execution state remains DONE for every terminal
     * outcome so the existing state ABI stays backward compatible. */
    int64_t outcome;
    /* Raw, stable scheduler nodes. Each node owns an updateable root for its
     * joining virtual thread; these pointers themselves are not GC slots. */
    void *join_waiters;
    void *join_wait_tail;
    void *join_entry;
    /* Target retained while this task is parked in join and until join_result
     * consumes the terminal payload. */
    PyObject *join_target;
    int64_t wait_kind;
    int64_t cancel_requested;
    /* Channel/select wait state.  Owner and value fields are traced; arm
     * entries are raw scheduler backpointers and must be NULL whenever the
     * virtual thread is not actively parked on a channel operation. */
    PyObject *channel_owner_a;
    PyObject *channel_owner_b;
    void *channel_arm_a;
    void *channel_arm_b;
    PyObject *channel_value;
    int64_t channel_status;
    int64_t channel_index;
} PyVirtualThreadObject;

_Static_assert(sizeof(PyVirtualThreadObject) == 192,
               "PyVirtualThreadObject ABI");
_Static_assert(offsetof(PyVirtualThreadObject, channel_owner_a) == 136,
               "PyVirtualThreadObject.channel_owner_a ABI");
_Static_assert(offsetof(PyVirtualThreadObject, channel_owner_b) == 144,
               "PyVirtualThreadObject.channel_owner_b ABI");
_Static_assert(offsetof(PyVirtualThreadObject, channel_value) == 168,
               "PyVirtualThreadObject.channel_value ABI");

/* ---- Class / Instance API (py_class.c) -------------------------------- */

/* Construct a new class. Computes a C3 linearization over `bases`.
 *
 *   name        : interned; class keeps a borrowed pointer to it.
 *   bases       : array of PyClassObject* (direct bases). May be NULL if
 *                 n_bases == 0 (root class).
 *   n_bases     : number of direct bases.
 *   field_names : declared instance-field names, in slot order. Borrowed
 *                 pointers (caller keeps them alive for the class's life).
 *   n_fields    : number of declared instance fields. NOTE: this is the
 *                 count declared on THIS class only; the class inherits
 *                 its bases' fields through MRO lookup at attribute time,
 *                 not by slot duplication.
 *
 * Returns a new reference that the caller owns.
 */
PyClassObject *py_class_new(const char *name,
                            PyClassObject **bases, int32_t n_bases,
                            const char **field_names, int32_t n_fields);
PyObject *py_class_new_from_objects(PyObject *name,
                                    PyObject *bases,
                                    PyObject *ns);
void py_class_mark_slots_only(PyClassObject *cls);
void py_class_mark_dict_subclass(PyClassObject *cls);
/* dict-subclass inherited-behavior fallback (py_protocol.c). */
PyObject *py_dict_subclass_getattr(PyObject *o, const char *name);
PyObject *py_dict_subclass_getitem(PyObject *o, PyObject *key);

/* Install a method on the class. `name` is borrowed and must remain immutable
 * and alive for the class lifetime; `func` is borrowed (caller retains
 * ownership). Class construction/mutation is externally serialized and must
 * not race with lookup. Methods added after py_class_new are visible to
 * subsequent lookups. */
void py_class_add_method(PyClassObject *cls, const char *name, PyObject *func);
void py_class_set_metaclass(PyClassObject *cls, PyClassObject *metaclass);

/* Walk the class's MRO and return the first method with the given name,
 * or NULL if none is found. Borrowed reference. */
PyObject *py_class_lookup(PyClassObject *cls, const char *name);

PyObject *py_class_attrs_dict(PyClassObject *cls, int64_t create);
PyObject *py_class_getattr(PyClassObject *cls, const char *name);
int64_t py_class_setattr(PyClassObject *cls, const char *name, PyObject *value);
int64_t py_class_setattr_raw(PyClassObject *cls, const char *name, PyObject *value);
int64_t py_class_apply_namespace_dict(PyClassObject *cls, PyObject *ns);
int64_t py_class_delattr(PyClassObject *cls, const char *name);
void py_class_attrs_dispose(PyClassObject *cls);
int64_t py_class_attrs_retarget(PyClassObject *from, PyClassObject *to);

/* Allocate an instance of `cls`. All field slots start at NULL. Returns a
 * new reference. Calls py_class_new implicit logic — no __init__ is
 * invoked from here; callers (py_obj_call for class-as-callable) run
 * __init__ separately. */
PyObject *py_instance_new(PyClassObject *cls);

/* Direct field accessors by slot index. No bounds check at runtime in the
 * fast path — codegen is expected to emit a valid index. A defensive
 * check is kept (returns NULL / is a no-op on out-of-range) so malformed
 * IR cannot segfault us. */
PyObject *py_instance_get_field(PyInstanceObject *inst, int32_t idx);
void      py_instance_set_field(PyInstanceObject *inst, int32_t idx, PyObject *value);

PyObject *py_valuebox_new(PyClassObject *cls);
PyObject *py_valuebox_get_field(PyValueBoxObject *box, int32_t idx);
void      py_valuebox_set_field(PyValueBoxObject *box, int32_t idx, PyObject *value);

/* Generic attribute dispatch for PY_TYPE_INSTANCE and allocated user-class
 * tags (PY_TYPE_USER_CLASS_START and above).
 * Tries `inst->cls->field_names` first, then MRO method lookup. Returns
 * a borrowed reference (caller may py_incref if keeping). */
PyObject *py_instance_getattr(PyInstanceObject *inst, const char *name);
PyObject *py_instance_getattr_default(PyInstanceObject *inst, const char *name);
PyObject *py_instance_vars(PyInstanceObject *inst);

/* Attribute assignment. Returns 0 on success, -1 on failure (e.g. unknown
 * field and no method slot to accept). */
int64_t py_instance_setattr(PyInstanceObject *inst, const char *name, PyObject *value);
int64_t py_instance_delattr(PyInstanceObject *inst, const char *name);

/* Shallow-copy a native instance and override named fields. Used by the
 * dataclasses.replace fast path on pcc-native class instances. */
PyObject *py_dataclass_replace(PyObject *obj, int64_t n_overrides,
                               const char **names, PyObject **values);
PyObject *py_dataclass_replace_from_dict(PyObject *obj, PyObject *overrides);

/* isinstance(obj, cls) — walks obj's class's MRO looking for cls.
 * Returns 1 if obj is an instance of cls or any subclass, 0 otherwise.
 * Non-instance objects get 0. */
int64_t py_isinstance(PyObject *obj, PyClassObject *cls);

/* super() lookup: find the first method named `name` in `start_cls`'s
 * MRO strictly AFTER `from_cls`. This is the standard super() semantic —
 * a super() call inside a method defined on `from_cls` invoked on an
 * instance whose class is `start_cls`. Returns a borrowed reference or
 * NULL if no such method exists. */
PyObject *py_super_lookup(PyClassObject *start_cls,
                          PyClassObject *from_cls,
                          const char *name);

/* Compute the C3 linearization of `bases`. The first element of the
 * returned MRO is always a freshly introduced "self" placeholder supplied
 * by py_class_new (it handles prepending); this function only linearizes
 * the bases' MROs plus the `bases` list itself. Writes an owned array to
 * *out_mro (malloc'd) and the length to *out_n. Returns 0 on success,
 * -1 on MRO inconsistency (raises nothing in Phase 3; marks out_n = -1).
 *
 * Callers retain ownership of the returned array and must free() it. */
int c3_linearize(PyClassObject **bases, int32_t n_bases,
                 PyClassObject ***out_mro, int32_t *out_n);

/* Destructor helper used by py_decref when a class's refcount drops. */
void py_class_dealloc(PyObject *o);
void py_instance_dealloc(PyObject *o);
void py_descriptor_dealloc(PyObject *o);

/* ---- Descriptors (Phase 3 — property / classmethod / staticmethod) ----
 *
 * The three descriptor wrappers wear their own user-type tags so the
 * descriptor protocol in py_obj_getattr can recognise them without a
 * string match on the function's name. Tags 101..103 are reserved between
 * PY_TYPE_USER and PY_TYPE_USER_CLASS_START and must stay stable — codegen
 * and the runtime both test against these constants. */
typedef struct {
    PyObjectHeader h;       /* type_tag = PY_TYPE_PROPERTY */
    PyObject *fget;         /* function or NULL */
    PyObject *fset;         /* function or NULL */
    PyObject *fdel;         /* function or NULL */
} PyPropertyObject;

typedef struct {
    PyObjectHeader h;       /* type_tag = PY_TYPE_CLASSMETHOD */
    PyObject *func;
} PyClassMethodObject;

typedef struct {
    PyObjectHeader h;       /* type_tag = PY_TYPE_STATICMETHOD */
    PyObject *func;
} PyStaticMethodObject;

typedef struct {
    PyObjectHeader h;       /* type_tag = PY_TYPE_ITER */
    PyObject *seq;          /* list / tuple / str / materialised dict keys */
    int64_t index;          /* next index to return */
} PyIterObject;

/* Constructors implemented by py_class_attrs.c. All return new references.
 * Static methods currently lower directly to their wrapped callable, so tag
 * PY_TYPE_STATICMETHOD remains part of the runtime layout/GC contract but has
 * no public constructor. */
PyObject *py_property_new(PyObject *fget, PyObject *fset, PyObject *fdel);
PyObject *py_classmethod_new(PyObject *func);
PyObject *py_instance_bind_method(PyObject *method, PyObject *self, const char *name);

/* In-place setter/deleter replacement — used by the @name.setter /
 * @name.deleter decorator form where a second `def` with the same
 * attribute name updates the already-installed property's fset/fdel
 * slot instead of creating a new property object.
 *
 * Returns 0 on success, -1 if ``prop`` is not a property. Acquires
 * a new reference to ``func`` (NULL is accepted to clear the slot). */
int py_property_set_fset(PyObject *prop, PyObject *func);
int py_property_set_fdel(PyObject *prop, PyObject *func);

/* ---- Iteration + extended generic ops (Phase 3) ----------------------- */
PyObject *py_obj_iter(PyObject *o);
PyObject *py_obj_next(PyObject *it);
int64_t   py_obj_contains(PyObject *container, PyObject *item);

/* Numeric / comparison dunders. Each first tries the native fast path
 * for built-in types, then falls through to ``__op__`` on LHS and
 * ``__rop__`` on RHS. */
PyObject *py_obj_add(PyObject *a, PyObject *b);
PyObject *py_obj_sub(PyObject *a, PyObject *b);
PyObject *py_obj_mul(PyObject *a, PyObject *b);
PyObject *py_obj_and(PyObject *a, PyObject *b);
PyObject *py_obj_or(PyObject *a, PyObject *b);
PyObject *py_obj_xor(PyObject *a, PyObject *b);
PyObject *py_obj_lshift(PyObject *a, PyObject *b);
PyObject *py_obj_rshift(PyObject *a, PyObject *b);
PyObject *py_obj_truediv(PyObject *a, PyObject *b);
PyObject *py_obj_floordiv(PyObject *a, PyObject *b);
PyObject *py_obj_inplace_op(PyObject *a, PyObject *b, int64_t op_code);
void pcc_gc_record_explicit_pause(int64_t start_us, int64_t end_us);
PyObject *py_obj_mod(PyObject *a, PyObject *b);
PyObject *py_obj_pow(PyObject *a, PyObject *b);
PyObject *py_obj_abs(PyObject *o);
PyObject *py_obj_neg(PyObject *a);
PyObject *py_obj_pos(PyObject *a);
PyObject *py_obj_invert(PyObject *a);
int64_t py_obj_eq(PyObject *a, PyObject *b);
int64_t py_obj_lt(PyObject *a, PyObject *b);
int64_t py_obj_le(PyObject *a, PyObject *b);
int64_t py_obj_gt(PyObject *a, PyObject *b);
int64_t py_obj_ge(PyObject *a, PyObject *b);

/* ---- Exceptions (Phase 3) --------------------------------------------- */

/* Traceback frame record.
 *
 * Each entry captures a single activation: the function name, source
 * filename, source-line text, and line number of the call site or throw point. Stored by
 * value inside a traceback array owned by the PyExceptionObject.
 *
 * Borrowed-pointer semantics: `func_name`, `filename`, and `source_line` reference
 * static rodata globals emitted by the frontend. The exception object
 * never frees them. */
typedef struct PyFrameRecord {
    const char *func_name;
    const char *filename;
    const char *source_line;
    int32_t     line;
    int32_t     _pad;       /* keep pointer alignment */
} PyFrameRecord;

/* Exception object layout.
 *
 * PY_TYPE_EXC is the header type tag for every builtin exception; user
 * subclasses carry the same tag but name a subclass in exc_class, so
 * isinstance walks stay uniform. Frontend-emitted landingpads only
 * ever peek through this view — they read exc_class for matching, then
 * the python-level handler body reads .args / .__cause__ etc. via
 * getattr.
 *
 *   exc_class : concrete class. NULL is legal transiently (py_exc_alloc
 *               sets it to py_exc_builtin_class(PY_EXC_EXCEPTION) if
 *               the caller leaves it unset), but in steady state is
 *               always non-NULL. Owns its ref.
 *   message   : args[0] — a PyStrObject* or py_None. Owns its ref.
 *   cause     : `raise X from Y` target. NULL = no explicit cause.
 *               Owns its ref.
 *   context   : implicit context captured when a new exception replaces
 *               an active one (`raise Y` inside except-block). Owns its
 *               ref.
 *   traceback : heap-allocated growable array of PyFrameRecord. NULL
 *               until a frame is appended. */
typedef struct PyExceptionObject {
    PyObjectHeader      h;
    PyClassObject      *exc_class;
    PyObject           *message;
    PyObject           *cause;
    PyObject           *context;
    PyFrameRecord      *traceback;
    int32_t             n_frames;
    int32_t             cap_frames;
} PyExceptionObject;

/* Lazily allocate and cache the builtin exception class for `tag`.
 * Returns a borrowed reference — the runtime holds a permanent ref on
 * every builtin class so callers need not incref. */
PyClassObject *py_exc_builtin_class(int64_t tag);

/* Allocate a PyExceptionObject wired to `cls` with `msg` as message
 * (may be NULL). Returns a new owned reference; installs header
 * type_tag = PY_TYPE_EXC. */
PyExceptionObject *py_exc_alloc(PyClassObject *cls, const char *msg);

/* Deallocation hook (called from py_decref via py_dealloc_exc). */
void py_dealloc_exc(PyObject *o);

/* Exception model is now return-code based (see py_exc.c header
 * comment). No Itanium C++ ABI symbols are exported from py_exc.c
 * anymore. */

/* ---- Helpers ----------------------------------------------------------- */

/* Return the int64 value from either a tagged int or a small heap int.
 *
 * Precondition: the object must actually fit in int64. Callers that may
 * encounter a large bignum should use py_int_to_i64 (public ABI) with the
 * overflow out-param instead. */
int64_t py_int_value_i64(PyObject *o);
int64_t py_int_bit_length(PyObject *o);
int64_t py_int_bit_count(PyObject *o);

/* Access typed fields. */
static inline PyObjectHeader *py_header(PyObject *o) {
    /* Only valid if !PY_IS_TAGGED_INT(o). */
    return (PyObjectHeader *)o;
}

static inline int32_t py_type_of(PyObject *o) {
    if (PY_IS_TAGGED_INT(o)) return PY_TYPE_INT;
    return py_header(o)->type_tag;
}

/* ---- Bignum helpers (Phase 2) ----------------------------------------- */

/* Allocate a raw bignum object with storage for `ndigits` base-2^32 digits.
 * Caller must fill sign + ndigits + digits[]. Does not normalize. */
PyIntObject *py_bigint_alloc(int32_t ndigits);

/* Build a bignum from an int64 value. Always returns a non-tagged
 * PyIntObject heap object — callers that want the canonical tagged form
 * should use py_int_from_i64 or py_bigint_to_pyobject. */
PyIntObject *py_bigint_from_i64(int64_t v);

/* Convert a bignum to the canonical PyObject*: a tagged int if the value
 * fits in the tagged range, else the same bignum pointer cast back to
 * PyObject*. May free `b` when collapsing to a tagged int. Always returns
 * a new reference that the caller owns. */
PyObject *py_bigint_to_pyobject(PyIntObject *b);

/* Promote any int (tagged or heap bignum) to a fresh bignum copy. Caller
 * owns the result and must py_decref it. */
PyIntObject *py_bigint_from_any(PyObject *o);

/* Schoolbook arithmetic. All return new references (or NULL on malloc fail). */
PyIntObject *py_bigint_add(const PyIntObject *a, const PyIntObject *b);
PyIntObject *py_bigint_sub(const PyIntObject *a, const PyIntObject *b);
PyIntObject *py_bigint_mul(const PyIntObject *a, const PyIntObject *b);
PyIntObject *py_bigint_neg(const PyIntObject *a);

/* Compare two bignums. Returns -1/0/+1. */
int py_bigint_cmp(const PyIntObject *a, const PyIntObject *b);

/* If the bignum fits in int64, return the value and set *overflow = 0.
 * Otherwise return 0 and set *overflow = 1. */
int64_t py_bigint_to_i64(const PyIntObject *b, int *overflow);

/* Convert to a double. Precision may be lost for very large magnitudes. */
double py_bigint_to_double(const PyIntObject *b);

/* Decimal string conversion. Returned buffer is malloc'd; caller owns and
 * must free(). Returns NULL on allocation failure. */
char *py_bigint_to_cstr(const PyIntObject *b);
/* Full base-{2,8,16} string for a bignum: "[-]0<prefix_ch><digits>" (lowercase
 * a-f). malloc'd, NUL-terminated; caller frees. NULL on allocation failure. */
char *py_bigint_to_base_cstr(const PyIntObject *b, unsigned base, char prefix_ch);

/* Parse a (possibly signed) decimal string. Returns a new bignum or NULL on
 * parse / allocation failure. */
PyIntObject *py_bigint_from_cstr(const char *s);

/* Dynamic dunder helpers implemented in py_dunder.c. */
PyObject *py_int_to_str_obj(PyObject *o);
PyObject *py_user_str_dispatch(PyObject *o);
PyObject *py_user_repr_dispatch(PyObject *o);
void py_user_del_dispatch(PyObject *o);
int64_t py_user_hash_dispatch(PyObject *o, int64_t *handled);
PyObject *py_user_iter_dispatch(PyObject *o);
PyObject *py_user_next_dispatch(PyObject *o);
PyObject *py_user_matmul_dispatch(PyObject *a, PyObject *b);
PyObject *py_user_binop_dispatch(PyObject *a, PyObject *b, const char *name, const char *rname, const char *type_err_msg);
PyObject *py_obj_floordiv(PyObject *a, PyObject *b);
PyObject *py_obj_inplace_op(PyObject *a, PyObject *b, int64_t op_code);
void pcc_gc_record_explicit_pause(int64_t start_us, int64_t end_us);
int64_t py_user_len_dispatch(PyObject *o, int64_t *handled);
int64_t py_user_bool_dispatch(PyObject *o, int64_t *handled);
PyObject *py_user_abs_dispatch(PyObject *o);
int64_t py_obj_index_i64(PyObject *o);
int64_t py_user_contains_dispatch(PyObject *o, PyObject *item, int64_t *handled);
PyObject *py_user_getitem_dispatch(PyObject *o, PyObject *key);
int64_t py_user_setitem_dispatch(PyObject *o, PyObject *key, PyObject *value, int64_t *handled);
int64_t py_user_delitem_dispatch(PyObject *o, PyObject *key, int64_t *handled);

/* Bitwise ops (treat operands as two's-complement of infinite width). */
PyIntObject *py_bigint_and(const PyIntObject *a, const PyIntObject *b);
PyIntObject *py_bigint_or (const PyIntObject *a, const PyIntObject *b);
PyIntObject *py_bigint_xor(const PyIntObject *a, const PyIntObject *b);

/* Shifts by a non-negative bit count. */
PyIntObject *py_bigint_shl(const PyIntObject *a, uint64_t bits);
PyIntObject *py_bigint_shr(const PyIntObject *a, uint64_t bits);

/* base ** exp, both bignums, exp must be non-negative. Returns NULL on
 * negative exponent or alloc failure. */
PyIntObject *py_bigint_pow(const PyIntObject *base, const PyIntObject *exp);

/* Divmod — Python floor semantics: q = floor(a / b); r = a - q*b.
 * Writes new refs to *q_out and *r_out; returns 0 on success, -1 on
 * divide-by-zero or alloc failure. */
int py_bigint_divmod(const PyIntObject *a, const PyIntObject *b,
                     PyIntObject **q_out, PyIntObject **r_out);

/* Multi-phase C-extension init (py_capi_shim.c): a PyInit_* that returns
 * PyModuleDef_Init(&def) yields a module DEF, not a ready module. The loader
 * detects it (pcc_capi_is_moduledef) and runs the Py_mod_exec slots to build the
 * real module (pcc_capi_module_exec). numpy's _multiarray_umath needs this. */
int pcc_capi_is_moduledef(PyObject *o);
PyObject *pcc_capi_module_exec(PyObject *def_as_obj);
/* Split phases so the extension loader can register the module in its
 * load-once cache between creation and exec (PEP 489 sys.modules-before-exec
 * contract; prevents nested-import re-init of e.g. numpy). */
PyObject *pcc_capi_module_from_def(PyObject *def_as_obj);
int pcc_capi_module_run_exec_slots(PyObject *def_as_obj, PyObject *module);
void pcc_capi_visit_extension_module_state_roots(
    PccGcRootVisitor visit,
    void *ctx
);

#endif /* PY_INTERNAL_H */
