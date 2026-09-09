/* Pluggable-GC backend selector and shared telemetry.
 *
 * The non-refcount backends start as selectable skeletons: they reuse
 * the refcount semantics while exposing the barrier/safepoint counters
 * that the real Lua/Go/OCaml/ZGC implementations will drive. Keeping
 * this separate from py_obj.c lets each backend grow without changing
 * the public pcc_gc_* ABI again.
 */

#include "py_internal.h"
#include <errno.h>
#include <sched.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include <unistd.h>

void py_weakref_invalidate(PyObject *target);
int64_t py_weakref_retarget(PyObject *from, PyObject *to);
void py_gc_untrack(PyObject *o);
extern int32_t py_class_attr_cache_epoch;

typedef struct PccGcForwardingInstallPlan PccGcForwardingInstallPlan;
typedef struct PccGcTraceCextCtx {
    PyObject *obj;
    int64_t epoch;
    int64_t backend;
} PccGcTraceCextCtx;
typedef struct PccGcBackend4RemapCextCtx {
    PyObject *obj;
    int64_t epoch;
    int64_t object_revision;
    struct PccGcForwardNode *forwarding_head;
    int64_t forwarding_population;
    int64_t page_revision;
    int64_t relocation_revision;
} PccGcBackend4RemapCextCtx;
static int pcc_gc_trace_cext_claim_unlocked(
    PyObject *obj,
    PccGcTraceCextCtx *ctx
);
static int pcc_gc_trace_cext_complete(PccGcTraceCextCtx *ctx);

static int64_t pcc_gc_install_forwarding_unlocked(
    PyObject *from, PyObject *to
);
static PccGcForwardingInstallPlan *pcc_gc_forwarding_install_plan_prepare(
    PyObject *from,
    PyObject *to
);
static int64_t pcc_gc_install_forwarding_preallocated_unlocked(
    PyObject *from,
    PyObject *to,
    PccGcForwardingInstallPlan *plan
);
static void pcc_gc_forwarding_install_plan_finish(
    PccGcForwardingInstallPlan *plan
);

static int64_t pcc_gc_selected_backend = PCC_GC_KIND_REFCOUNT_CYCLE;
int32_t pcc_gc_read_barrier_enabled = 1;
static int64_t pcc_gc_metrics[6] = {0, 0, 0, 0, 0, 0};
static int32_t pcc_gc_mark_active = 0;
static int32_t pcc_gc_cycle_requested = 0;
/* The graph lock is the linearization boundary for tracing-cycle identity and
 * its single-finisher claim.  These have external linkage so the strict
 * pcc-Python runtime and focused differential probes expose the same raw ABI. */
int64_t pcc_gc_tracing_cycle_epoch = 0;
int64_t pcc_gc_tracing_finish_claim_epoch = 0;
int64_t pcc_gc_tracing_finish_claim_backend = -1;
int64_t pcc_gc_tracing_finish_commits = 0;
int32_t pcc_gc_trace_extension_roots_pending = 0;
int64_t pcc_gc_trace_extension_roots_epoch = 0;
int64_t pcc_gc_trace_extension_roots_backend = -1;
PyObject *pcc_gc_trace_cext_pending_obj = NULL;
int64_t pcc_gc_trace_cext_pending_epoch = 0;
int64_t pcc_gc_trace_cext_pending_backend = -1;
int32_t pcc_gc_backend4_remap_active = 0;
/* Not static: py_obj.c's refcount prepare paths are the only writers, and the
 * pcc-Python mirror reaches the same symbol through global_addr(). */
int64_t pcc_gc_unmanaged_refcount_ops = 0;
int64_t pcc_gc_backend4_remap_epoch = 0;
PyObject *pcc_gc_backend4_remap_pending_obj = NULL;
static int32_t pcc_gc_config_initialized = 0;
static int32_t pcc_gc_backend0_frame_roots_enabled = 0;
static _Thread_local int32_t pcc_gc_in_auto_step = 0;
static _Thread_local int32_t pcc_gc_explicit_collect_active = 0;
static int64_t pcc_gc_debt_bytes = 0;
static int64_t pcc_gc_live_bytes = 0;
static int64_t pcc_gc_gcpause = 1000;
static int64_t pcc_gc_gcstepmul = 10000;
static int64_t pcc_gc_debt_threshold_override = 0;
static int64_t pcc_gc_max_pause_us = 0;
static int64_t pcc_gc_pause_count = 0;
static int64_t pcc_gc_pause_sum_us = 0;
static int64_t pcc_gc_pause_hist[4] = {0, 0, 0, 0};
static int64_t pcc_gc_minor_heap_size = 33554432;
static int64_t pcc_gc_minor_alloc_max = 16;
static int64_t pcc_gc_minor_allocations = 0;
static int64_t pcc_gc_minor_collections = 0;
static int64_t pcc_gc_minor_bytes = 0;

static int pcc_gc_pointer_can_have_header(PyObject *obj) {
    return pcc_gc_pointer_is_managed(obj) != 0;
}

typedef struct PccGcMinorBlock {
    uint8_t *base;
    uint8_t *ptr;
    uint8_t *end;
    int64_t live_objects;
    int64_t owner_thread_id;
    struct PccGcMinorBlock *next;
} PccGcMinorBlock;

struct PccGcZPageNode;

typedef struct PccGcObjectNode {
    PyObject *obj;
    int64_t size;
    int32_t freeing;
    PccGcMinorBlock *minor_block;
    struct PccGcObjectNode *next;
    struct PccGcObjectNode *prev;
    struct PccGcZPageNode *zpage_node;
    int64_t gc_refs;
    struct PccGcObjectNode *young_next;
    struct PccGcObjectNode *young_prev;
} PccGcObjectNode;

typedef struct PccGcFrameNode {
    const int32_t *frame_map;
    PyObject **slots;
    struct PccGcFrameNode *next;
    struct PccGcFrameNode *prev;
    struct PccGcFrameNode *dup_next;
    int64_t root_count;
    int32_t borrowed;
    PyObject **stable_values;
} PccGcFrameNode;

/* freestanding_gc_frame_registry.py mirrors this fixed prefix byte-for-byte. */
_Static_assert(sizeof(PccGcFrameNode) == 64, "PccGcFrameNode ABI drift");

typedef struct PccGcContinuationRootNode {
    const int32_t *frame_map;
    PyObject **slots;
    struct PccGcContinuationRootNode *next;
    int64_t root_count;
    int32_t borrowed;
    PyObject **stable_values;
    /* Last on purpose: the pcc-Python mirror in
     * freestanding_gc_root_registry.py addresses this node by literal offset
     * and expects prev at [48] with the slot storage starting at [56]. */
    struct PccGcContinuationRootNode *prev;
} PccGcContinuationRootNode;

_Static_assert(
    sizeof(PccGcContinuationRootNode) == 56,
    "PccGcContinuationRootNode ABI drift against the pcc-Python mirror"
);
_Static_assert(
    offsetof(PccGcContinuationRootNode, prev) == 48,
    "PccGcContinuationRootNode.prev ABI drift"
);

typedef struct PccGcSchedulerRootNode {
    PyObject **slot;
    struct PccGcSchedulerRootNode *next;
    struct PccGcSchedulerRootNode *prev;
} PccGcSchedulerRootNode;

typedef struct PccGcRememberedOwnerNode {
    PyObject *owner;
    struct PccGcRememberedOwnerNode *next;
} PccGcRememberedOwnerNode;

typedef struct PccGcSchedulerQueueEntry {
    PyObject *value;
    struct PccGcSchedulerQueueEntry *next;
    void *root_handle;
} PccGcSchedulerQueueEntry;

struct PccGcSchedulerQueue {
    PccMutex *mutex;
    PccGcSchedulerQueueEntry *head;
    PccGcSchedulerQueueEntry *tail;
    int64_t length;
    PccGcSchedulerQueueEntry *free_head;
    int64_t free_count;
};

typedef struct PccGcForwardNode {
    PyObject *from;
    PyObject *to;
    struct PccGcForwardNode *next;
    struct PccGcForwardNode *prev;
    struct PccGcForwardNode *target_next;
    struct PccGcForwardNode *target_prev;
    /* page owning `from`'s span, captured at install time so the
     * retirement decrement is O(1) instead of an O(pages) addr walk */
    struct PccGcZPage *from_page;
} PccGcForwardNode;

typedef struct PccGcIdentityNode {
    PyObject *obj;
    int64_t id;
    struct PccGcIdentityNode *next;
    struct PccGcIdentityNode *prev;
} PccGcIdentityNode;

typedef struct PccGcRelocationNode {
    PyObject *obj;
    struct PccGcRelocationNode *next;
} PccGcRelocationNode;

typedef struct PccGcStoreBufferNode {
    PyObject *owner;
    PyObject **slot;
    PyObject *value;
    struct PccGcStoreBufferNode *next;
} PccGcStoreBufferNode;

typedef struct PccGcRememberedSlotNode {
    PyObject *owner;
    PyObject **slot;
    struct PccGcRememberedSlotNode *next;
} PccGcRememberedSlotNode;

typedef struct {
    PyObject *owner;
    PyObject **slot;
    PyObject *value;
} PccGcStoreBufferEntry;

typedef struct {
    PyObjectHeader h;
    const char *name;
    PyNativeFuncEntry entry;
    PyObject *captures;
    PyObject *args;
    PyObject *result;
    int32_t closed;
    int32_t done;
} PccGcCoroutineObject;

/* py_threading.c keeps its concrete thread object private.  The tracing
 * backends still need to walk and clear the PyObject* children so Thread
 * wrappers do not pin their target/captures forever.  Keep this view in sync
 * with PyThreadObject in py_threading.c; it only exposes pointer slots. */
typedef struct {
    PyObjectHeader h;
    PccThreadHandle *handle;
    PyObject *callable;
    PyObject *args;
    PyObject *result;
    int64_t started;
    int64_t joined;
    int64_t finished;
} PccGcThreadObject;

static PccGcObjectNode *pcc_gc_objects = NULL;
static PccGcObjectNode *pcc_gc_trace_cursor = NULL;
/* Cross-step Backend-3 overflow scans retain an intrusive-list cursor.  Every
 * object-list mutation advances this revision, and unlink moves the cursor
 * before a node can enter the recycle pool. */
static int64_t pcc_gc_object_list_revision = 0;
static PccGcObjectNode *pcc_gc_backend3_remembered_scan_cursor = NULL;
static int64_t pcc_gc_backend3_remembered_scan_revision = 0;
/* Backends #3/#4 share one intrusive pending-young worklist.  Allocations
 * publish final-YOUNG objects and promotion/free detaches them under the graph
 * lock, so generation work never rescans the full tracked-object list. */
static PccGcObjectNode *pcc_gc_backend3_young_head = NULL;
/* Recursive owner promotion is represented by an intrusive, restartable
 * owner queue.  The logical slot cursor is resolved from the owner's current
 * layout on every tenure; no raw payload-slot address survives an unlock. */
static PccGcObjectNode *pcc_gc_backend3_promotion_head = NULL;
static PccGcObjectNode *pcc_gc_backend3_promotion_tail = NULL;
static int64_t pcc_gc_backend3_promotion_revision = 0;
static int64_t pcc_gc_backend3_promotion_probe_pause = 0;
static int64_t pcc_gc_backend3_promotion_probe_state_value = 0;
static PccGcObjectNode *pcc_gc_object_node_free_list = NULL;
static int64_t pcc_gc_object_node_free_count = 0;
static int64_t pcc_gc_gray_count = 0;
static PccGcFrameNode *pcc_gc_frames = NULL;
/* GC3/GC4 recycle exact-size 0..16-slot nodes in a per-thread cache.  The
 * total cap bounds retained memory across all 17 buckets; larger nodes and
 * GC0/1/2 always stay on the allocator/free path. */
#define PCC_GC_FRAME_NODE_POOL_MAX_ROOTS 16
#define PCC_GC_FRAME_NODE_POOL_LIMIT 1024
#define PCC_GC_FRAME_NODE_FLAG_BORROWED 1
#define PCC_GC_FRAME_NODE_FLAG_LIFO 2
static _Thread_local PccGcFrameNode *pcc_gc_frame_node_free_lists[
    PCC_GC_FRAME_NODE_POOL_MAX_ROOTS + 1
] = {0};
static _Thread_local int64_t pcc_gc_frame_node_free_counts[
    PCC_GC_FRAME_NODE_POOL_MAX_ROOTS + 1
] = {0};
static _Thread_local int64_t pcc_gc_frame_node_free_total = 0;
static PccGcContinuationRootNode *pcc_gc_continuation_roots = NULL;
static PccGcSchedulerRootNode *pcc_gc_scheduler_roots = NULL;
/* GC3 root promotion keeps resumable cursors so no registry walk can make an
 * outer graph-lock tenure proportional to the total live frame population.
 * Registry removals repair a cursor under the same lock before freeing its
 * node.  The revision detects reentrant mutation during slot promotion. */
static int64_t pcc_gc_root_registry_revision = 0;
static int32_t pcc_gc_backend3_frame_root_scan_phase = 0;
static int64_t pcc_gc_backend3_frame_root_scan_slot = -1;
static PccGcFrameNode *pcc_gc_backend3_frame_root_scan_cursor = NULL;
static PccGcContinuationRootNode *
    pcc_gc_backend3_continuation_root_scan_cursor = NULL;
static int32_t pcc_gc_backend3_scheduler_root_scan_phase = 0;
static int64_t pcc_gc_backend3_scheduler_root_scan_slot = -1;
static PccGcSchedulerRootNode *
    pcc_gc_backend3_scheduler_root_scan_cursor = NULL;
static int64_t pcc_gc_runtime_root_snapshot_owner = 0;
static int64_t pcc_gc_runtime_root_snapshot_probe_pause = 0;
static int64_t pcc_gc_runtime_root_snapshot_probe_state_value = 0;
static int32_t pcc_gc_runtime_root_snapshot_phase = 0;
static int64_t pcc_gc_runtime_root_snapshot_slot = -1;
static PccGcFrameNode *pcc_gc_runtime_root_snapshot_frame_cursor = NULL;
static PccGcContinuationRootNode *
    pcc_gc_runtime_root_snapshot_continuation_cursor = NULL;
static PccGcSchedulerRootNode *
    pcc_gc_runtime_root_snapshot_scheduler_cursor = NULL;
static PccGcForwardNode *pcc_gc_forwardings = NULL;
static PccGcIdentityNode *pcc_gc_identities = NULL;
static PccGcRelocationNode *pcc_gc_relocation_set = NULL;
static int64_t pcc_gc_backend4_relocation_reset_owner = 0;
static PccGcObjectNode *pcc_gc_backend4_reset_object_cursor = NULL;
static int64_t pcc_gc_backend4_reseed_plan_probe_pause = 0;
static int64_t pcc_gc_backend4_reseed_plan_probe_state_value = 0;
static int64_t pcc_gc_backend4_reseed_plan_probe_allocation_limit = -1;
static int64_t pcc_gc_backend4_reseed_page_count_owner = 0;
static int64_t pcc_gc_backend4_reseed_commit_owner = 0;
static int64_t pcc_gc_backend4_reseed_page_revision = 0;
static struct PccGcZPageEvacuationNode *
    pcc_gc_backend4_reseed_page_count_cursor = NULL;
static int64_t pcc_gc_backend4_reseed_relocation_revision = 0;
static PccGcRelocationNode *pcc_gc_backend4_reseed_relocation_cursor = NULL;
static PccGcStoreBufferNode *pcc_gc_backend4_store_buffer = NULL;
static PccGcRememberedSlotNode *pcc_gc_backend4_remembered_slots = NULL;
#define PCC_GC_BACKEND4_REMEMBERED_PAGE_SLOT_BITS 512
#define PCC_GC_BACKEND4_REMEMBERED_PAGE_WORDS \
    (PCC_GC_BACKEND4_REMEMBERED_PAGE_SLOT_BITS / 64)
#define PCC_GC_BACKEND4_ZPAGE_CARD_SLOT_BITS 8
#define PCC_GC_BACKEND4_ZPAGE_CARD_COUNT \
    (PCC_GC_BACKEND4_REMEMBERED_PAGE_SLOT_BITS / \
     PCC_GC_BACKEND4_ZPAGE_CARD_SLOT_BITS)
#ifndef PCC_GC_BACKEND4_ZPAGE_SPAN_CARD_BYTES
#define PCC_GC_BACKEND4_ZPAGE_SPAN_CARD_BYTES 512
#endif

typedef struct PccGcRememberedPageNode {
    uintptr_t page_key;
    uint64_t slot_bitmap[PCC_GC_BACKEND4_REMEMBERED_PAGE_WORDS];
    int64_t slots;
    struct PccGcRememberedPageNode *next;
} PccGcRememberedPageNode;
static PccGcRememberedPageNode *pcc_gc_backend4_remembered_pages = NULL;
typedef struct PccGcZPage {
    PyObject *primary_owner;
    int64_t used_bytes;
    int64_t capacity_bytes;
    int64_t allocated_bytes;
    int32_t page_class;
    int32_t generation;
    int64_t object_count;
    int64_t remembered_slots;
    int64_t remembered_cards;
    uint64_t remembered_card_bitmap;
    uint16_t remembered_card_refcounts[PCC_GC_BACKEND4_ZPAGE_CARD_COUNT];
    uint8_t *span_base;
    int64_t span_capacity_bytes;
    int64_t pending_alloc_count;
    /* Forwarding entries whose OLD address lies in this page's span.
     * The lazy-heal read barrier reads the old copy's header, so the
     * span must stay mapped until this reaches zero; pages that are
     * otherwise empty defer destruction via `zombie`. */
    int64_t pending_forwardings;
    int32_t zombie;
    /* Occupies the former 4-byte alignment hole before object_head. */
    int32_t evacuation_selected;
    struct PccGcZPageNode *object_head;
    struct PccGcZPage *next;
} PccGcZPage;

typedef struct PccGcZPageNode {
    PyObject *owner;
    PccGcZPage *page;
    int64_t offset_bytes;
    int64_t size_bytes;
    struct PccGcZPageNode *next;
    struct PccGcZPageNode *prev;
    struct PccGcZPageNode *page_next;
    struct PccGcZPageNode *page_prev;
    /* per-owner payload-span chain: O(own spans) register/remove/query
     * instead of a global O(all spans) list walk (the global walk was
     * 95%% of gc4 churn wall time once containers registered spans) */
    struct PccGcZPagePayloadSpanNode *payload_spans;
    int64_t remembered_slots;
} PccGcZPageNode;

typedef struct PccGcZPagePayloadSpanNode {
    PyObject *owner;
    uint8_t *base;
    int64_t size_bytes;
    int64_t offset_bytes;
    PccGcZPage *page;
    struct PccGcZPagePayloadSpanNode *next;
} PccGcZPagePayloadSpanNode;

typedef struct PccGcZPageEvacuationCandidate {
    PccGcZPageNode *mapping;
    PccGcZPage *page;
    PyObject *owner;
    int64_t used_bytes;
    int64_t capacity_bytes;
    int64_t fragmentation_bytes;
    int64_t remembered_slots;
    int64_t remembered_cards;
    int64_t score;
    int32_t owner_flags;
} PccGcZPageEvacuationCandidate;

typedef struct PccGcZPageEvacuationNode {
    PccGcZPage *page;
    struct PccGcZPageEvacuationNode *next;
} PccGcZPageEvacuationNode;

typedef struct {
    PccGcRelocationNode *relocation_nodes;
    PccGcZPageEvacuationNode *page_nodes;
} PccGcRelocationSelectionPlan;

typedef struct {
    PccGcZPage *released_pages;
    PccGcForwardNode *forwardings;
    PccGcIdentityNode *identities;
    PccGcObjectNode *object_nodes;
    void *payload_plans;
    PccGcForwardNode *dead_target_forwardings;
} PccGcBackend4RemapFinish;

_Static_assert(
    sizeof(PccGcBackend4RemapFinish) == 48,
    "PccGcBackend4RemapFinish ABI drift"
);
_Static_assert(
    offsetof(PccGcBackend4RemapFinish, released_pages) == 0,
    "PccGcBackend4RemapFinish.released_pages ABI drift"
);
_Static_assert(
    offsetof(PccGcBackend4RemapFinish, forwardings) == 8,
    "PccGcBackend4RemapFinish.forwardings ABI drift"
);
_Static_assert(
    offsetof(PccGcBackend4RemapFinish, identities) == 16,
    "PccGcBackend4RemapFinish.identities ABI drift"
);
_Static_assert(
    offsetof(PccGcBackend4RemapFinish, object_nodes) == 24,
    "PccGcBackend4RemapFinish.object_nodes ABI drift"
);
_Static_assert(
    offsetof(PccGcBackend4RemapFinish, payload_plans) == 32,
    "PccGcBackend4RemapFinish.payload_plans ABI drift"
);
_Static_assert(
    offsetof(PccGcBackend4RemapFinish, dead_target_forwardings) == 40,
    "PccGcBackend4RemapFinish.dead_target_forwardings ABI drift"
);

static PccGcZPageNode *pcc_gc_backend4_zpages = NULL;
static PccGcZPageNode *pcc_gc_backend4_zpage_node_free_list = NULL;
/* One selector scan may span several graph-lock tenures.  The cursor and best
 * node must therefore be owned by the runtime rather than a caller stack:
 * zpage unlink advances/invalidates them before recycling node storage. */
static PccGcZPageNode *pcc_gc_backend4_selector_scan_cursor = NULL;
static PccGcZPageNode *pcc_gc_backend4_selector_scan_best = NULL;
static PccGcZPage *pcc_gc_backend4_selector_scan_page = NULL;
static int64_t pcc_gc_backend4_selector_scan_owner = 0;
static int64_t pcc_gc_backend4_selector_scan_best_score = -1;
static int32_t pcc_gc_backend4_selector_scan_allow_large = 0;
static int32_t pcc_gc_backend4_selector_scan_require_unselected = 0;
static int32_t pcc_gc_backend4_selector_scan_restart = 0;
static PccGcZPageNode *pcc_gc_backend4_selector_page_cursor = NULL;
static PccGcZPageNode *pcc_gc_backend4_selector_page_seed = NULL;
static PccGcZPage *pcc_gc_backend4_selector_page = NULL;
static int64_t pcc_gc_backend4_selector_page_owner = 0;
static int32_t pcc_gc_backend4_selector_page_allow_large = 0;
static int32_t pcc_gc_backend4_selector_page_seed_pending = 0;
static int64_t pcc_gc_backend4_zpage_node_free_count = 0;

_Static_assert(
    offsetof(PccGcZPage, evacuation_selected) == 236,
    "PccGcZPage evacuation_selected ABI drift"
);
_Static_assert(
    offsetof(PccGcZPage, object_head) == 240,
    "PccGcZPage object_head ABI drift"
);
_Static_assert(
    offsetof(PccGcZPageNode, remembered_slots) == 72,
    "PccGcZPageNode remembered_slots ABI drift"
);
_Static_assert(
    sizeof(PccGcZPageNode) == 80,
    "PccGcZPageNode size ABI drift"
);
static PccGcZPage *pcc_gc_backend4_pages = NULL;
static PccGcZPage *pcc_gc_backend4_free_pages = NULL;
static PccGcZPage *pcc_gc_backend4_retained_pages = NULL;
static PccGcZPage *pcc_gc_backend4_active_pages[3][3] = {{NULL}};
static PccGcZPageEvacuationNode *pcc_gc_backend4_evacuation_pages = NULL;
static void pcc_gc_backend4_zpage_note_remembered_slot_unlocked(
    PyObject *owner,
    int64_t delta
);
static void pcc_gc_backend4_zpage_note_remembered_card_unlocked(
    PyObject *owner,
    PyObject **slot,
    int64_t delta
);
static void pcc_gc_backend4_zpage_remove_unlocked(PyObject *owner);
static PccGcZPageNode *pcc_gc_backend4_zpage_node_alloc_unlocked(void);
static void pcc_gc_backend4_zpage_node_release_unlocked(
    PccGcZPageNode *node
);
typedef struct PccGcStoreBufferMediumState {
    PccGcStoreBufferEntry *entries;
    int32_t *count;
    struct PccGcStoreBufferMediumState *next;
} PccGcStoreBufferMediumState;
typedef struct {
    PyObject *owner;
    PyObject **values;
    int64_t count;
    int32_t committed;
} PccGcSourceSideTablePlan;
static PccGcStoreBufferMediumState *pcc_gc_backend4_store_buffer_medium_states = NULL;
static PccGcRememberedOwnerNode *pcc_gc_backend3_remembered_owners = NULL;
static int32_t pcc_gc_backend3_remembered_overflow = 0;
static int64_t pcc_gc_backend3_remembered_owner_allocation_limit = -1;
static PccGcMinorBlock *pcc_gc_minor_blocks = NULL;
static _Thread_local PccGcMinorBlock *pcc_gc_minor_current = NULL;
static _Thread_local PccGcMinorBlock *pcc_gc_pending_minor_block = NULL;
static int64_t pcc_gc_next_object_id = 1;
static int pcc_gc_tracks_objects(void);
static int pcc_gc_is_known_object(PyObject *o);
static void pcc_gc_gray_object(PyObject *o);
static int pcc_gc_has_sweep_candidate(void);
static int pcc_gc_backend3_graph_leaf_tag(int32_t tag);
static void pcc_gc_backend4_evacuation_page_finish_detached(
    PccGcZPageEvacuationNode *head
);
static int64_t pcc_gc_backend4_evacuation_page_nodes_prepare(
    PccGcZPageEvacuationNode **head,
    int64_t capacity
);
static void pcc_gc_relocation_reset_finish(
    PccGcRelocationNode *relocation_nodes,
    PccGcZPageEvacuationNode *evacuation_nodes
);
static PccGcZPageEvacuationNode *
pcc_gc_backend4_evacuation_page_find_unlocked(PccGcZPage *page);
static int64_t pcc_gc_known_object_size_unlocked(PyObject *obj);
static int64_t pcc_gc_cms_trace_gray_object_unlocked(PyObject *o);
static int64_t pcc_gc_cms_worker_trace_cycle_unlocked(
    int64_t budget,
    int64_t *claim_epoch,
    int64_t *claim_backend
);
static int64_t pcc_gc_drain_all_gray_unlocked(void);
static int64_t pcc_gc_drain_all_gray_locked_slice(void);
static int64_t pcc_gc_drain_all_gray_stopped_world(
    int64_t claim_epoch,
    int64_t claim_backend
);
static int pcc_gc_complete_mark_cycle_seed(
    int64_t claim_epoch,
    int64_t claim_backend
);
static int pcc_gc_complete_claimed_tracing_cycle(
    int64_t claim_epoch,
    int64_t claim_backend
);
static int64_t pcc_gc_step_trace_cycle(int64_t budget);
int64_t pcc_gc_backend4_remap_and_retire_stopped_world(void);
static PccGcRememberedOwnerNode *
pcc_gc_backend3_remembered_owners_clear_unlocked(void);
static void pcc_gc_backend3_finish_detached_remembered_owners(
    PccGcRememberedOwnerNode *head
);
static void pcc_gc_backend4_remap_and_retire_unlocked(
    PccGcBackend4RemapFinish *finish
);
static void pcc_gc_backend4_finish_retained_page_releases(
    PccGcZPage *pages
);
static void pcc_gc_backend4_finish_remap_retirement(
    PccGcBackend4RemapFinish *finish
);
static void pcc_gc_backend4_park_page_unlocked(struct PccGcZPage *page);
static void pcc_gc_retire_forwarded_source_into_finish_unlocked(
    PyObject *from,
    PccGcBackend4RemapFinish *finish
);
static void pcc_gc_retire_forwarded_source_unlocked(PyObject *from);
static int64_t pcc_gc_relocation_retire_source_payload_into_finish(
    PyObject *from,
    PccGcBackend4RemapFinish *finish
);
static int64_t
pcc_gc_relocation_retire_source_payload_for_target_death_into_finish(
    PyObject *from,
    PyObject *target,
    PccGcBackend4RemapFinish *finish
);
static void pcc_gc_relocation_finish_source_payloads(void *opaque_plans);
static int64_t pcc_gc_forwarding_population = 0;
#define PCC_GC_BACKEND4_REMAP_THRESHOLD 4096

#define PCC_GC_SAFEPOINT_BATCH 16
#define PCC_GC_DEFAULT_DEBT_THRESHOLD 65536LL
#define PCC_GC_WORK_BYTES 64LL
#define PCC_GC_MAX_AUTO_STEP_BUDGET 65536LL
#define PCC_GC_CMS_QUEUE_CAPACITY 256
#define PCC_GC_CMS_WB_BUFFER_CAPACITY 32
#define PCC_GC_CMS_RESCAN_WORK INT64_MAX
#define PCC_GC_BACKEND4_STORE_BUFFER_BATCH_CAPACITY 8
#define PCC_GC_BACKEND4_STORE_BUFFER_MEDIUM_CAPACITY 32
#define PCC_GC_BACKEND4_SMALL_PAGE_LIMIT 4096
#define PCC_GC_BACKEND4_MEDIUM_PAGE_LIMIT 65536
#define PCC_GC_BACKEND4_FREE_SMALL_PAGE_LIMIT 8
#define PCC_GC_BACKEND4_FREE_MEDIUM_PAGE_LIMIT 4
#define PCC_GC_BACKEND4_ZPAGE_SPAN_GUARD_BYTES 256

static int64_t pcc_gc_cms_queue[PCC_GC_CMS_QUEUE_CAPACITY];
static int32_t pcc_gc_cms_queue_head = 0;
static int32_t pcc_gc_cms_queue_tail = 0;
static unsigned char pcc_gc_cms_queue_lock_word = 0;
static _Thread_local PyObject *pcc_gc_cms_wb_buffer[
    PCC_GC_CMS_WB_BUFFER_CAPACITY
];
static _Thread_local int32_t pcc_gc_cms_wb_buffer_count = 0;
static _Thread_local int32_t pcc_gc_cms_wb_flush_pending = 0;
static _Thread_local int32_t pcc_gc_cms_wb_overflow_pending = 0;
static _Thread_local int32_t pcc_gc_cms_wb_flush_active = 0;
static _Thread_local int64_t pcc_gc_cms_wb_epoch = 0;
static int64_t pcc_gc_cms_queue_epoch = 1;
static _Thread_local PccGcStoreBufferEntry pcc_gc_backend4_store_buffer_medium[
    PCC_GC_BACKEND4_STORE_BUFFER_MEDIUM_CAPACITY
];
static _Thread_local int32_t pcc_gc_backend4_store_buffer_medium_count = 0;
static _Thread_local PccGcStoreBufferMediumState *pcc_gc_backend4_store_buffer_medium_state = NULL;
static int32_t pcc_gc_cms_worker_started = 0;
static int32_t pcc_gc_cms_worker_stop_requested = 0;
static PccThreadHandle *pcc_gc_cms_worker_handle = NULL;
static int64_t pcc_gc_cms_worker_starts = 0;
static int64_t pcc_gc_cms_worker_stops = 0;
static int64_t pcc_gc_cms_queue_pushes = 0;
static int64_t pcc_gc_cms_worker_drains = 0;
static int64_t pcc_gc_cms_mutator_assists = 0;
static int64_t pcc_gc_cms_worker_traces = 0;
static int64_t pcc_gc_cms_wb_flushes = 0;
#if PCC_WITH_THREADS
static int32_t pcc_gc_graph_lock_state = 0;
static _Thread_local int32_t pcc_gc_graph_lock_depth = 0;
#endif
#ifdef PCC_RUNTIME_TRIPWIRES
static _Thread_local const char *pcc_gc_deferred_tripwire_message = NULL;
static _Thread_local const char *pcc_gc_deferred_tripwire_file = NULL;
static _Thread_local int32_t pcc_gc_deferred_tripwire_line = 0;
#endif
static int64_t pcc_gc_minor_arena_refills = 0;
static int64_t pcc_gc_minor_arena_bumps = 0;
static int64_t pcc_gc_minor_arena_fallbacks = 0;
static int64_t pcc_gc_relocation_forwards = 0;
static int64_t pcc_gc_relocation_barrier_forwards = 0;
static int64_t pcc_gc_relocation_pin_rejects = 0;
static int64_t pcc_gc_backend4_genzgc_store_barriers = 0;
static int64_t pcc_gc_backend4_store_buffer_entries_count = 0;
static int64_t pcc_gc_backend4_young_promotions = 0;
static int64_t pcc_gc_backend4_evacuation_candidates = 0;
static int64_t pcc_gc_backend4_evacuated_bytes_count = 0;
static int64_t pcc_gc_backend4_large_object_defers = 0;
static int64_t pcc_gc_backend4_candidate_fresh_skips = 0;
static int64_t pcc_gc_backend4_relocation_add_refusals = 0;
static int64_t pcc_gc_backend4_large_object_deferred_bytes_count = 0;
static int64_t pcc_gc_backend4_large_object_reconsiderations_count = 0;
static int64_t pcc_gc_backend4_small_page_candidates = 0;
static int64_t pcc_gc_backend4_medium_page_candidates = 0;
static int64_t pcc_gc_backend4_evacuation_candidate_bytes_count = 0;
static int64_t pcc_gc_backend4_small_page_candidate_bytes_count = 0;
static int64_t pcc_gc_backend4_medium_page_candidate_bytes_count = 0;
static int64_t pcc_gc_backend4_evacuation_candidate_zpage_bytes_count = 0;
static int64_t pcc_gc_backend4_small_page_candidate_zpage_bytes_count = 0;
static int64_t pcc_gc_backend4_medium_page_candidate_zpage_bytes_count = 0;
static int64_t pcc_gc_backend4_store_buffer_drain_batches_count = 0;
static int64_t pcc_gc_backend4_store_buffer_drained_entries_count = 0;
static int64_t pcc_gc_backend4_store_buffer_duplicate_skips_count = 0;
static int64_t pcc_gc_backend4_store_buffer_high_water_count = 0;
static int64_t pcc_gc_backend4_store_buffer_owner_fanout_high_water_count = 0;
static int64_t pcc_gc_backend4_store_buffer_owner_count_high_water_count = 0;
static int64_t pcc_gc_backend4_store_buffer_incomplete_drains_count = 0;
static int64_t pcc_gc_backend4_evacuation_incomplete_batches_count = 0;
static int64_t pcc_gc_backend4_store_buffer_max_batch_size_count = 0;
static int64_t pcc_gc_backend4_store_buffer_full_batches_count = 0;
static int64_t pcc_gc_backend4_remembered_set_entries_count = 0;
static int64_t pcc_gc_backend4_remembered_set_duplicate_skips_count = 0;
static int64_t pcc_gc_backend4_remembered_set_high_water_count = 0;
static int64_t pcc_gc_backend4_remembered_page_entries_count = 0;
static int64_t pcc_gc_backend4_remembered_page_slot_entries_count = 0;
static int64_t pcc_gc_backend4_remembered_page_high_water_count = 0;
static int64_t pcc_gc_backend4_store_buffer_medium_flushes_count = 0;
static int64_t pcc_gc_backend4_store_buffer_medium_flushed_entries_count = 0;
static int64_t pcc_gc_backend4_store_buffer_medium_full_flushes_count = 0;
static int64_t pcc_gc_backend4_store_buffer_cross_thread_medium_flushes_count = 0;
static int64_t pcc_gc_backend4_store_buffer_cross_thread_medium_flushed_entries_count = 0;

static void pcc_gc_cms_maybe_start_worker(void);
static void pcc_gc_cms_flush_wb_buffer(void);

static int32_t pcc_gc_mark_active_load(void) {
    return __atomic_load_n(&pcc_gc_mark_active, __ATOMIC_ACQUIRE);
}

static void pcc_gc_mark_active_store(int32_t value) {
    __atomic_store_n(&pcc_gc_mark_active, value, __ATOMIC_RELEASE);
}

static int32_t pcc_gc_cycle_requested_load(void) {
    return __atomic_load_n(&pcc_gc_cycle_requested, __ATOMIC_ACQUIRE);
}

static void pcc_gc_cycle_requested_store(int32_t value) {
    __atomic_store_n(&pcc_gc_cycle_requested, value, __ATOMIC_RELEASE);
}

static int64_t pcc_gc_tracing_cycle_epoch_load(void) {
    return __atomic_load_n(&pcc_gc_tracing_cycle_epoch, __ATOMIC_ACQUIRE);
}

static int64_t pcc_gc_tracing_finish_claim_epoch_load(void) {
    return __atomic_load_n(
        &pcc_gc_tracing_finish_claim_epoch, __ATOMIC_ACQUIRE
    );
}

static int64_t pcc_gc_tracing_finish_claim_backend_load(void) {
    return __atomic_load_n(
        &pcc_gc_tracing_finish_claim_backend, __ATOMIC_ACQUIRE
    );
}

static void pcc_gc_tracing_finish_claim_store(
    int64_t claim_epoch,
    int64_t claim_backend
) {
    __atomic_store_n(
        &pcc_gc_tracing_finish_claim_backend,
        claim_backend,
        __ATOMIC_RELEASE
    );
    __atomic_store_n(
        &pcc_gc_tracing_finish_claim_epoch,
        claim_epoch,
        __ATOMIC_RELEASE
    );
}

static void pcc_gc_tracing_finish_claim_clear_unlocked(
    int64_t claim_epoch,
    int64_t claim_backend
) {
    if (
        pcc_gc_tracing_finish_claim_epoch_load() != claim_epoch
        || pcc_gc_tracing_finish_claim_backend_load() != claim_backend
    ) {
        return;
    }
    __atomic_store_n(
        &pcc_gc_tracing_finish_claim_epoch, 0, __ATOMIC_RELEASE
    );
    __atomic_store_n(
        &pcc_gc_tracing_finish_claim_backend, -1, __ATOMIC_RELEASE
    );
}

/* Called only while the object-graph lock is held.  Epoch identities are
 * never reused: a dormant captured token must not become valid after wrap. */
int64_t pcc_gc_tracing_cycle_epoch_advance_unlocked(void) {
    int64_t current = pcc_gc_tracing_cycle_epoch_load();
    if (current < 0) {
        abort();
        return 0;
    }
    if (current == INT64_MAX) {
        abort();
        return 0;
    }
    int64_t next = current + 1;
    __atomic_store_n(&pcc_gc_tracing_cycle_epoch, next, __ATOMIC_RELEASE);
    return next;
}

static void pcc_gc_cms_queue_lock(void) {
    while (__atomic_test_and_set(
            &pcc_gc_cms_queue_lock_word, __ATOMIC_ACQUIRE
        )) {
        pcc_thread_safepoint();
    }
}

static void pcc_gc_cms_queue_unlock(void) {
    __atomic_clear(&pcc_gc_cms_queue_lock_word, __ATOMIC_RELEASE);
}

static void pcc_gc_metric_add(int64_t metric, int64_t delta) {
    if (metric < 0 || metric > PCC_GC_COUNTER_WORK_STEPS) return;
    __atomic_add_fetch(&pcc_gc_metrics[metric], delta, __ATOMIC_RELAXED);
}

static int64_t pcc_gc_metric_load(int64_t metric) {
    if (metric < 0 || metric > PCC_GC_COUNTER_WORK_STEPS) return -1;
    return __atomic_load_n(&pcc_gc_metrics[metric], __ATOMIC_RELAXED);
}

static void pcc_gc_live_bytes_subtract(int64_t size) {
    if (size <= 0) return;
    int64_t old_live = __atomic_load_n(&pcc_gc_live_bytes, __ATOMIC_RELAXED);
    while (old_live > 0) {
        int64_t new_live = size >= old_live ? 0 : old_live - size;
        if (__atomic_compare_exchange_n(
                &pcc_gc_live_bytes,
                &old_live,
                new_live,
                0,
                __ATOMIC_ACQ_REL,
                __ATOMIC_RELAXED
            )) {
            return;
        }
    }
}

static int pcc_gc_object_node_is_freeing(PccGcObjectNode *n) {
    if (n == NULL) return 1;
    return __atomic_load_n(&n->freeing, __ATOMIC_ACQUIRE) != 0;
}

static void pcc_gc_object_node_set_freeing(PccGcObjectNode *n) {
    if (n == NULL) return;
    __atomic_store_n(&n->freeing, 1, __ATOMIC_RELEASE);
}

static int pcc_gc_object_node_is_active(PccGcObjectNode *n) {
    if (
        n == NULL
        || pcc_gc_object_node_is_freeing(n)
        || n->obj == NULL
        || PY_IS_TAGGED_INT(n->obj)
    ) {
        return 0;
    }
    /* Backend #4 intentionally keeps zpage ownership/index metadata until
     * after the type-specific deallocator has consumed the object's fields.
     * A concurrent collector must exclude that retained node, but refcount
     * zero alone is too broad: relocation shells can legitimately keep a
     * zero count until forwarding retirement.  py_decref publishes this
     * dedicated bit only for the delayed zpage-deallocation window. */
    return (
        py_header_flags_load(py_header(n->obj)) & PY_FLAG_GC_DEALLOCATING
    ) == 0;
}

static void pcc_gc_object_list_revision_advance_unlocked(void) {
    if (pcc_gc_object_list_revision == INT64_MAX) {
        pcc_gc_object_list_revision = 1;
    } else {
        pcc_gc_object_list_revision++;
    }
}

static void pcc_gc_root_registry_revision_advance_unlocked(void) {
    if (pcc_gc_root_registry_revision == INT64_MAX) {
        pcc_gc_root_registry_revision = 1;
    } else {
        pcc_gc_root_registry_revision++;
    }
}

static void pcc_gc_object_node_link_head(PccGcObjectNode *n) {
    if (n == NULL) return;
    n->prev = NULL;
    n->next = pcc_gc_objects;
    if (pcc_gc_objects != NULL) {
        pcc_gc_objects->prev = n;
    }
    pcc_gc_objects = n;
    pcc_gc_object_list_revision_advance_unlocked();
}

static void pcc_gc_backend3_young_link_head(PccGcObjectNode *n) {
    if (n == NULL) return;
    n->young_prev = NULL;
    n->young_next = pcc_gc_backend3_young_head;
    if (pcc_gc_backend3_young_head != NULL) {
        pcc_gc_backend3_young_head->young_prev = n;
    }
    pcc_gc_backend3_young_head = n;
}

static void pcc_gc_backend3_young_unlink(PccGcObjectNode *n) {
    if (n == NULL) return;
    if (n->young_prev != NULL) {
        n->young_prev->young_next = n->young_next;
    } else if (pcc_gc_backend3_young_head == n) {
        pcc_gc_backend3_young_head = n->young_next;
    } else {
        n->young_next = NULL;
        n->young_prev = NULL;
        return;
    }
    if (n->young_next != NULL) {
        n->young_next->young_prev = n->young_prev;
    }
    n->young_next = NULL;
    n->young_prev = NULL;
}

static void pcc_gc_backend3_promotion_unlink_unlocked(
    PccGcObjectNode *n
) {
    if (n == NULL || n->obj == NULL || PY_IS_TAGGED_INT(n->obj)) return;
    if ((py_header_flags_load(py_header(n->obj)) & PY_FLAG_GC_YOUNG) != 0) {
        return;
    }
    if (
        pcc_gc_backend3_promotion_head != n
        && pcc_gc_backend3_promotion_tail != n
        && n->young_prev == NULL
        && n->young_next == NULL
    ) return;
    if (n->young_prev != NULL) {
        n->young_prev->young_next = n->young_next;
    } else if (pcc_gc_backend3_promotion_head == n) {
        pcc_gc_backend3_promotion_head = n->young_next;
    }
    if (n->young_next != NULL) {
        n->young_next->young_prev = n->young_prev;
    } else if (pcc_gc_backend3_promotion_tail == n) {
        pcc_gc_backend3_promotion_tail = n->young_prev;
    }
    n->young_next = NULL;
    n->young_prev = NULL;
    n->gc_refs = 0;
}

static void pcc_gc_object_node_clear_promotion_state(
    PccGcObjectNode *n
) {
    if (n == NULL) return;
    n->young_next = NULL;
    n->young_prev = NULL;
    n->gc_refs = 0;
}

static void pcc_gc_object_node_unlink(PccGcObjectNode *n) {
    if (n == NULL) return;
    if (pcc_gc_trace_cursor == n) {
        pcc_gc_trace_cursor = n->next;
    }
    if (pcc_gc_backend4_reset_object_cursor == n) {
        pcc_gc_backend4_reset_object_cursor = n->next;
    }
    if (pcc_gc_backend3_remembered_scan_cursor == n) {
        pcc_gc_backend3_remembered_scan_cursor = n->next;
    }
    pcc_gc_backend3_promotion_unlink_unlocked(n);
    pcc_gc_backend3_young_unlink(n);
    if (n->prev != NULL) {
        n->prev->next = n->next;
    } else if (pcc_gc_objects == n) {
        pcc_gc_objects = n->next;
    } else {
        PccGcObjectNode **cur = &pcc_gc_objects;
        while (*cur != NULL && *cur != n) {
            cur = &(*cur)->next;
        }
        if (*cur == n) {
            *cur = n->next;
        }
    }
    if (n->next != NULL) {
        n->next->prev = n->prev;
    }
    n->prev = NULL;
    n->next = NULL;
    pcc_gc_object_list_revision_advance_unlocked();
}

#define PCC_GC_OBJECT_NODE_FREE_LIMIT 8192

void *pcc_gc_object_node_prepare(void) {
    return malloc(sizeof(PccGcObjectNode));
}

int64_t pcc_gc_object_node_plan_requires_prepare(void) {
    return pcc_gc_object_node_free_list == NULL ? 1 : 0;
}

void *pcc_gc_object_node_take_prepared(void **prepared_io) {
    if (prepared_io == NULL) return NULL;
    PccGcObjectNode *n = pcc_gc_object_node_free_list;
    if (n != NULL) {
        pcc_gc_object_node_free_list = n->next;
        if (pcc_gc_object_node_free_count > 0) {
            pcc_gc_object_node_free_count--;
        }
        pcc_gc_object_node_clear_promotion_state(n);
        return n;
    }
    n = (PccGcObjectNode *)*prepared_io;
    *prepared_io = NULL;
    pcc_gc_object_node_clear_promotion_state(n);
    return n;
}

static PccGcObjectNode *pcc_gc_object_node_alloc(void) {
    PccGcObjectNode *n = pcc_gc_object_node_free_list;
    if (n != NULL) {
        pcc_gc_object_node_free_list = n->next;
        if (pcc_gc_object_node_free_count > 0) {
            pcc_gc_object_node_free_count--;
        }
        pcc_gc_object_node_clear_promotion_state(n);
        return n;
    }
    n = (PccGcObjectNode *)malloc(sizeof(PccGcObjectNode));
    pcc_gc_object_node_clear_promotion_state(n);
    return n;
}

static void pcc_gc_object_node_release(PccGcObjectNode *n) {
    if (n == NULL) return;
    pcc_gc_object_node_clear_promotion_state(n);
    if (pcc_gc_object_node_free_count >= PCC_GC_OBJECT_NODE_FREE_LIMIT) {
        free(n);
        return;
    }
    n->next = pcc_gc_object_node_free_list;
    pcc_gc_object_node_free_list = n;
    pcc_gc_object_node_free_count++;
}

static void pcc_gc_object_node_finish_detached(PccGcObjectNode *nodes) {
    while (nodes != NULL) {
        PccGcObjectNode *node = nodes;
        nodes = node->next;
        node->next = NULL;
        free(node);
    }
}

static int64_t pcc_gc_gray_count_load(void) {
    return __atomic_load_n(&pcc_gc_gray_count, __ATOMIC_ACQUIRE);
}

static void pcc_gc_gray_count_store(int64_t value) {
    __atomic_store_n(&pcc_gc_gray_count, value, __ATOMIC_RELEASE);
}

static void pcc_gc_gray_count_inc(void) {
    __atomic_add_fetch(&pcc_gc_gray_count, 1, __ATOMIC_ACQ_REL);
}

static void pcc_gc_gray_count_dec(void) {
    int64_t old = __atomic_load_n(&pcc_gc_gray_count, __ATOMIC_ACQUIRE);
    while (old > 0) {
        if (__atomic_compare_exchange_n(
                &pcc_gc_gray_count,
                &old,
                old - 1,
                0,
                __ATOMIC_ACQ_REL,
                __ATOMIC_ACQUIRE
            )) {
            return;
        }
    }
}

static int64_t pcc_gc_now_us(void) {
    struct timeval tv;
    if (gettimeofday(&tv, NULL) != 0) return 0;
    return (int64_t)tv.tv_sec * 1000000LL + (int64_t)tv.tv_usec;
}

static int64_t pcc_gc_parse_env_i64(
    const char *name,
    int64_t default_value,
    int64_t min_value,
    int64_t max_value
) {
    const char *raw = getenv(name);
    if (raw == NULL || raw[0] == '\0') return default_value;
    errno = 0;
    char *end = NULL;
    long long value = strtoll(raw, &end, 10);
    if (errno != 0 || end == raw || *end != '\0') return default_value;
    if ((int64_t)value < min_value) return min_value;
    if ((int64_t)value > max_value) return max_value;
    return (int64_t)value;
}

static int64_t pcc_gc_parse_backend_env(void) {
    const char *raw = getenv("PCC_GC_BACKEND");
    if (raw == NULL) return pcc_gc_selected_backend;
    if (raw[0] >= '0' && raw[0] <= '4' && raw[1] == '\0') {
        return (int64_t)(raw[0] - '0');
    }
    static const char message[] =
        "pcc runtime: invalid PCC_GC_BACKEND; expected one of 0,1,2,3,4\n";
    (void)write(2, message, sizeof(message) - 1);
    abort();
}

static int pcc_gc_backend_kind_uses_forwarding(int64_t backend) {
    return backend == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
        || backend == PCC_GC_KIND_COLORED_RELOCATING;
}

static void pcc_gc_update_read_barrier_enabled(int64_t backend) {
    __atomic_store_n(
        &pcc_gc_read_barrier_enabled,
        pcc_gc_backend_kind_uses_forwarding(backend) ? 1 : 0,
        __ATOMIC_RELEASE
    );
}

static void pcc_gc_init_config(void) {
    if (pcc_gc_config_initialized) return;
    pcc_gc_config_initialized = 1;
    int64_t backend = pcc_gc_parse_backend_env();
    pcc_gc_selected_backend = backend;
    pcc_gc_update_read_barrier_enabled(backend);
    pcc_gc_gcpause = pcc_gc_parse_env_i64("PCC_GC_PAUSE", 1000, 50, 1000);
    pcc_gc_gcstepmul = pcc_gc_parse_env_i64("PCC_GC_STEPMUL", 10000, 1, 10000);
    pcc_gc_gcstepmul = pcc_gc_parse_env_i64(
        "PCC_GC_STEP_MUL",
        pcc_gc_gcstepmul,
        1,
        10000
    );
    pcc_gc_debt_threshold_override = pcc_gc_parse_env_i64(
        "PCC_GC_DEBT_THRESHOLD",
        0,
        0,
        1LL << 40
    );
    pcc_gc_minor_heap_size = pcc_gc_parse_env_i64(
        "PCC_GC_MINOR_HEAP_SIZE",
        33554432,
        256,
        1LL << 40
    );
    pcc_gc_minor_alloc_max = pcc_gc_parse_env_i64(
        "PCC_GC_MINOR_ALLOC_MAX",
        16,
        16,
        1LL << 30
    );
    if (pcc_gc_selected_backend != PCC_GC_KIND_REFCOUNT_CYCLE) {
        pcc_gc_cycle_requested_store(1);
    }
    pcc_gc_cms_maybe_start_worker();
}

static int64_t pcc_gc_debt_threshold(void) {
    if (pcc_gc_debt_threshold_override > 0) {
        return pcc_gc_debt_threshold_override;
    }
    int64_t threshold = PCC_GC_DEFAULT_DEBT_THRESHOLD;
    int64_t live_bytes = __atomic_load_n(
        &pcc_gc_live_bytes, __ATOMIC_RELAXED
    );
    if (live_bytes > 0 && pcc_gc_gcpause > 100) {
        int64_t live_pause = (
            live_bytes * (pcc_gc_gcpause - 100)
        ) / 100;
        if (live_pause > threshold) threshold = live_pause;
    }
    return threshold;
}

static int64_t pcc_gc_budget_from_debt(void) {
    int64_t debt = __atomic_load_n(&pcc_gc_debt_bytes, __ATOMIC_RELAXED);
    int64_t budget = debt / PCC_GC_WORK_BYTES;
    budget = (budget * pcc_gc_gcstepmul) / 100;
    if (budget < 1) budget = 1;
    if (budget > PCC_GC_MAX_AUTO_STEP_BUDGET) {
        budget = PCC_GC_MAX_AUTO_STEP_BUDGET;
    }
    return budget;
}

static void pcc_gc_discharge_debt(int64_t processed) {
    if (processed <= 0) return;
    int64_t credit = (
        processed * PCC_GC_WORK_BYTES * pcc_gc_gcstepmul
    ) / 100;
    if (credit < PCC_GC_WORK_BYTES) credit = PCC_GC_WORK_BYTES;
    int64_t old_debt = __atomic_load_n(
        &pcc_gc_debt_bytes, __ATOMIC_RELAXED
    );
    while (old_debt > 0) {
        int64_t new_debt = credit >= old_debt ? 0 : old_debt - credit;
        if (__atomic_compare_exchange_n(
                &pcc_gc_debt_bytes,
                &old_debt,
                new_debt,
                0,
                __ATOMIC_ACQ_REL,
                __ATOMIC_RELAXED
            )) {
            return;
        }
    }
}

static void pcc_gc_record_pause(int64_t start_us, int64_t end_us) {
    if (start_us <= 0 || end_us < start_us) return;
    int64_t pause = end_us - start_us;
    /* G-P3-LONGRUN: count + sum + fixed histogram alongside the max.
     * Endpoint-only atomics — no hot-path cost. */
    __atomic_fetch_add(&pcc_gc_pause_count, 1, __ATOMIC_RELAXED);
    __atomic_fetch_add(&pcc_gc_pause_sum_us, pause, __ATOMIC_RELAXED);
    int hist_idx = pause < 100 ? 0 : (pause < 1000 ? 1 : (pause < 10000 ? 2 : 3));
    __atomic_fetch_add(&pcc_gc_pause_hist[hist_idx], 1, __ATOMIC_RELAXED);
    int64_t old_pause = __atomic_load_n(
        &pcc_gc_max_pause_us, __ATOMIC_RELAXED
    );
    while (pause > old_pause) {
        if (__atomic_compare_exchange_n(
                &pcc_gc_max_pause_us,
                &old_pause,
                pause,
                0,
                __ATOMIC_ACQ_REL,
                __ATOMIC_RELAXED
            )) {
            return;
        }
    }
}

/* Public endpoint for explicit-collect pause timing from outside this
 * translation unit (backend 0's cycle collect in pcc_gc_collect). */
void pcc_gc_record_explicit_pause(int64_t start_us, int64_t end_us) {
    pcc_gc_record_pause(start_us, end_us);
}

#if PCC_WITH_THREADS
static int pcc_gc_graph_try_lock(void) {
    int32_t expected = 0;
    return __atomic_compare_exchange_n(
        &pcc_gc_graph_lock_state,
        &expected,
        1,
        0,
        __ATOMIC_ACQ_REL,
        __ATOMIC_ACQUIRE
    );
}
#endif

static void pcc_gc_graph_lock(void) {
#if !PCC_WITH_THREADS
    return;
#else
    if (pcc_gc_graph_lock_depth > 0) {
        pcc_gc_graph_lock_depth++;
        return;
    }
    if (pcc_current_thread_id() <= 0) {
        abort();
        return;
    }
    while (!pcc_gc_graph_try_lock()) {
        pcc_thread_safepoint();
        usleep(100);
    }
    pcc_thread_no_park_enter();
    pcc_gc_graph_lock_depth = 1;
#endif
}

#ifdef PCC_RUNTIME_TRIPWIRES
static void pcc_gc_defer_tripwire_locked(
    int condition,
    const char *message,
    const char *file,
    int32_t line
) {
    if (condition || pcc_gc_deferred_tripwire_message != NULL) return;
    pcc_gc_deferred_tripwire_message = message;
    pcc_gc_deferred_tripwire_file = file;
    pcc_gc_deferred_tripwire_line = line;
}
#define PCC_GC_DEFER_TRIPWIRE(cond, msg) \
    pcc_gc_defer_tripwire_locked((cond), (msg), __FILE__, __LINE__)
#else
#define PCC_GC_DEFER_TRIPWIRE(cond, msg) ((void)0)
#endif

#ifdef PCC_RUNTIME_TRIPWIRES
static void pcc_gc_mixed_tripwire(
    int condition,
    const char *message,
    const char *file,
    int32_t line
) {
    if (condition) return;
#if PCC_WITH_THREADS
    if (pcc_gc_graph_lock_depth > 0) {
        pcc_gc_defer_tripwire_locked(0, message, file, line);
        return;
    }
#endif
    pcc_runtime_tripwire_fail(message, file, line);
}
#define PCC_GC_MIXED_TRIPWIRE(cond, msg) \
    pcc_gc_mixed_tripwire((cond), (msg), __FILE__, __LINE__)
#else
#define PCC_GC_MIXED_TRIPWIRE(cond, msg) ((void)0)
#endif

static void pcc_gc_finish_deferred_tripwire(void) {
#ifdef PCC_RUNTIME_TRIPWIRES
    const char *message = pcc_gc_deferred_tripwire_message;
    const char *file = pcc_gc_deferred_tripwire_file;
    int32_t line = pcc_gc_deferred_tripwire_line;
    pcc_gc_deferred_tripwire_message = NULL;
    pcc_gc_deferred_tripwire_file = NULL;
    pcc_gc_deferred_tripwire_line = 0;
    if (message != NULL) {
        pcc_runtime_tripwire_fail(message, file, line);
    }
#endif
}

static int pcc_gc_graph_lock_owned_by_current_thread(void) {
#if PCC_WITH_THREADS
    return pcc_gc_graph_lock_depth > 0;
#else
    return 0;
#endif
}

/* One cross-TU mixed-tripwire seam for helper files whose checks can fire
 * while their caller owns the GC graph lock.  Armed builds record the first
 * violation in the owner's thread-local slot and return 1 so the caller can
 * bail before consuming corrupt layout; unlocked callers enter the fatal
 * runtime sink directly.  Unarmed builds never reach a call site (all of
 * them are compiled out) and the body is a no-op returning 0. */
int pcc_gc_tripwire_defer_or_fail(
    const char *msg,
    const char *file,
    int32_t line
) {
#ifdef PCC_RUNTIME_TRIPWIRES
    if (pcc_gc_graph_lock_owned_by_current_thread()) {
        pcc_gc_defer_tripwire_locked(0, msg, file, line);
        return 1;
    }
    pcc_runtime_tripwire_fail(msg, file, line);
#endif
    return 0;
}
static void pcc_gc_graph_unlock(void) {
#if !PCC_WITH_THREADS
    if (
        pcc_gc_cms_wb_flush_pending != 0
        && pcc_gc_cms_wb_flush_active == 0
    ) {
        pcc_gc_cms_flush_wb_buffer();
    }
    pcc_gc_finish_deferred_tripwire();
    return;
#else
    if (pcc_gc_graph_lock_depth <= 0) return;
    pcc_gc_graph_lock_depth--;
    if (pcc_gc_graph_lock_depth > 0) return;
    __atomic_store_n(&pcc_gc_graph_lock_state, 0, __ATOMIC_RELEASE);
    if (
        pcc_gc_cms_wb_flush_pending != 0
        && pcc_gc_cms_wb_flush_active == 0
    ) {
        pcc_gc_cms_flush_wb_buffer();
    }
    pcc_gc_finish_deferred_tripwire();
    pcc_thread_no_park_exit();
#endif
}

void pcc_gc_root_slot_lock(void) {
    pcc_gc_graph_lock();
}

void pcc_gc_root_slot_unlock(void) {
    pcc_gc_graph_unlock();
}

static int pcc_gc_cms_queue_push_unlocked(int64_t work) {
    if (work == 0) work = 1;
    int32_t tail = pcc_gc_cms_queue_tail;
    int32_t head = pcc_gc_cms_queue_head;
    int32_t next = tail + 1;
    if (next >= PCC_GC_CMS_QUEUE_CAPACITY) next = 0;
    if (next == head) {
        return 0;
    }
    pcc_gc_cms_queue[tail] = work;
    pcc_gc_cms_queue_tail = next;
    return 1;
}

static int pcc_gc_cms_queue_push(int64_t work) {
    pcc_gc_cms_queue_lock();
    int pushed = pcc_gc_cms_queue_push_unlocked(work);
    pcc_gc_cms_queue_unlock();
    if (pushed) {
        __atomic_add_fetch(
            &pcc_gc_cms_queue_pushes, 1, __ATOMIC_RELAXED
        );
    }
    return pushed;
}

static int64_t pcc_gc_cms_queue_epoch_advance(void) {
    int64_t current = __atomic_load_n(
        &pcc_gc_cms_queue_epoch, __ATOMIC_ACQUIRE
    );
    for (;;) {
        if (current <= 0 || current == INT64_MAX) {
            abort();
            return 0;
        }
        int64_t next = current + 1;
        if (__atomic_compare_exchange_n(
                &pcc_gc_cms_queue_epoch,
                &current,
                next,
                0,
                __ATOMIC_ACQ_REL,
                __ATOMIC_ACQUIRE
            )) {
            return next;
        }
    }
}

static void pcc_gc_cms_wb_discard_tls(void) {
    int32_t count = pcc_gc_cms_wb_buffer_count;
    if (count < 0) count = 0;
    if (count > PCC_GC_CMS_WB_BUFFER_CAPACITY) {
        count = PCC_GC_CMS_WB_BUFFER_CAPACITY;
    }
    for (int32_t i = 0; i < count; i++) {
        pcc_gc_cms_wb_buffer[i] = NULL;
    }
    pcc_gc_cms_wb_buffer_count = 0;
    pcc_gc_cms_wb_flush_pending = 0;
    pcc_gc_cms_wb_overflow_pending = 0;
    pcc_gc_cms_wb_epoch = 0;
}

static void pcc_gc_cms_flush_wb_buffer(void) {
    if (pcc_gc_cms_wb_flush_active != 0) return;
#if PCC_WITH_THREADS
    if (pcc_gc_graph_lock_depth > 0) {
        pcc_gc_cms_wb_flush_pending = 1;
        return;
    }
#endif
    int32_t count = pcc_gc_cms_wb_buffer_count;
    if (count < 0) count = 0;
    if (count > PCC_GC_CMS_WB_BUFFER_CAPACITY) {
        count = PCC_GC_CMS_WB_BUFFER_CAPACITY;
    }
    if (count <= 0 && pcc_gc_cms_wb_overflow_pending == 0) {
        pcc_gc_cms_wb_flush_pending = 0;
        return;
    }

    pcc_gc_cms_wb_flush_active = 1;
    int32_t consumed = 0;
    int32_t pushed = 0;
    int32_t pushed_rescan = 0;
    pcc_gc_cms_queue_lock();
    int64_t queue_epoch = __atomic_load_n(
        &pcc_gc_cms_queue_epoch, __ATOMIC_ACQUIRE
    );
    if (
        pcc_gc_selected_backend != PCC_GC_KIND_CONCURRENT_MARK_SWEEP
        || pcc_gc_cms_wb_epoch != queue_epoch
    ) {
        pcc_gc_cms_wb_discard_tls();
        pcc_gc_cms_queue_unlock();
        pcc_gc_cms_wb_flush_active = 0;
        return;
    }

    while (consumed < count) {
        PyObject *o = pcc_gc_cms_wb_buffer[consumed];
        if (o == NULL || PY_IS_TAGGED_INT(o)) {
            consumed++;
            continue;
        }
        uintptr_t raw = (uintptr_t)o;
        if (raw == 0 || raw > (uintptr_t)INT64_MAX) {
            consumed++;
            continue;
        }
        if (!pcc_gc_cms_queue_push_unlocked(-((int64_t)raw))) break;
        consumed++;
        pushed++;
    }
    if (consumed > 0) {
        int32_t remaining = count - consumed;
        if (remaining > 0) {
            memmove(
                pcc_gc_cms_wb_buffer,
                &pcc_gc_cms_wb_buffer[consumed],
                (size_t)remaining * sizeof(PyObject *)
            );
        }
        for (int32_t i = remaining; i < count; i++) {
            pcc_gc_cms_wb_buffer[i] = NULL;
        }
        pcc_gc_cms_wb_buffer_count = remaining;
        count = remaining;
    }
    if (count == 0 && pcc_gc_cms_wb_overflow_pending != 0) {
        if (pcc_gc_cms_queue_push_unlocked(PCC_GC_CMS_RESCAN_WORK)) {
            pcc_gc_cms_wb_overflow_pending = 0;
            pushed_rescan = 1;
            pushed++;
        }
    }
    pcc_gc_cms_wb_flush_pending = (
        pcc_gc_cms_wb_buffer_count != 0
        || pcc_gc_cms_wb_overflow_pending != 0
    );
    pcc_gc_cms_queue_unlock();
    if (pushed > 0) {
        __atomic_add_fetch(
            &pcc_gc_cms_queue_pushes, pushed, __ATOMIC_RELAXED
        );
    }
    if (consumed > 0 || pushed_rescan != 0) {
        __atomic_add_fetch(
            &pcc_gc_cms_wb_flushes, 1, __ATOMIC_RELAXED
        );
    }
    pcc_gc_cms_wb_flush_active = 0;
}

static int pcc_gc_cms_buffer_gray(PyObject *o) {
    if (o == NULL || PY_IS_TAGGED_INT(o)) return 0;
    if (pcc_gc_selected_backend != PCC_GC_KIND_CONCURRENT_MARK_SWEEP) {
        return 0;
    }
    int64_t queue_epoch = __atomic_load_n(
        &pcc_gc_cms_queue_epoch, __ATOMIC_ACQUIRE
    );
    if (pcc_gc_cms_wb_epoch != queue_epoch) {
        pcc_gc_cms_wb_discard_tls();
        pcc_gc_cms_wb_epoch = queue_epoch;
    }
    int32_t count = pcc_gc_cms_wb_buffer_count;
    if (count >= PCC_GC_CMS_WB_BUFFER_CAPACITY) {
        pcc_gc_cms_wb_overflow_pending = 1;
        pcc_gc_cms_wb_flush_pending = 1;
        return 1;
    }
    pcc_gc_cms_wb_buffer[count] = o;
    count++;
    pcc_gc_cms_wb_buffer_count = count;
    if (count >= PCC_GC_CMS_WB_BUFFER_CAPACITY) {
        pcc_gc_cms_wb_flush_pending = 1;
        return 1;
    }
    return 0;
}

static int pcc_gc_cms_queue_pop(int64_t *work) {
    pcc_gc_cms_queue_lock();
    int32_t head = pcc_gc_cms_queue_head;
    int32_t tail = pcc_gc_cms_queue_tail;
    if (head == tail) {
        pcc_gc_cms_queue_unlock();
        return 0;
    }
    if (work != NULL) *work = pcc_gc_cms_queue[head];
    head++;
    if (head >= PCC_GC_CMS_QUEUE_CAPACITY) head = 0;
    pcc_gc_cms_queue_head = head;
    pcc_gc_cms_queue_unlock();
    return 1;
}

static void *pcc_gc_cms_worker_main(void *arg) {
    (void)arg;
    for (;;) {
        int64_t work = 0;
        int drained = 0;
        while (drained < 64 && pcc_gc_cms_queue_pop(&work)) {
            int64_t traced = 0;
            int64_t claim_epoch = 0;
            int64_t claim_backend = -1;
            int64_t followup_budget = 0;
            PccGcTraceCextCtx cext_ctx = {0};
            int64_t seed_epoch = 0;
            int64_t seed_backend = -1;
            int64_t worker_stw = pcc_stop_the_world();
            if (worker_stw != 0) {
                drained++;
                pcc_thread_safepoint();
                continue;
            }
            pcc_gc_graph_lock();
            if (
                pcc_gc_selected_backend
                != PCC_GC_KIND_CONCURRENT_MARK_SWEEP
            ) {
                pcc_gc_graph_unlock();
                if (worker_stw == 0) (void)pcc_resume_world();
                break;
            }
            if (work < 0) {
                PyObject *gray = (PyObject *)(uintptr_t)(-work);
                traced = pcc_gc_cms_trace_gray_object_unlocked(gray);
            } else if (work == PCC_GC_CMS_RESCAN_WORK) {
                /* Overflow is coalesced only into a whole-gray-set drain.
                 * A budget-1 ticket would lose the 33rd-and-later objects
                 * whose individual pointers could not fit in the TLS batch. */
                int64_t rescan_epoch = pcc_gc_tracing_cycle_epoch_load();
                int64_t rescan_backend = pcc_gc_selected_backend;
                pcc_gc_graph_unlock();
                traced = pcc_gc_drain_all_gray_stopped_world(
                    rescan_epoch,
                    rescan_backend
                );
                pcc_gc_graph_lock();
            } else {
                int64_t budget = work / PCC_GC_WORK_BYTES;
                if (budget < 1) budget = 1;
                if (budget > 64) budget = 64;
                followup_budget = budget;
                traced = pcc_gc_cms_worker_trace_cycle_unlocked(
                    budget,
                    &claim_epoch,
                    &claim_backend
                );
            }
            if (pcc_gc_trace_extension_roots_pending == 4) {
                seed_epoch = pcc_gc_trace_extension_roots_epoch;
                seed_backend = pcc_gc_trace_extension_roots_backend;
            } else if (pcc_gc_trace_cext_pending_obj != NULL) {
                cext_ctx.obj = pcc_gc_trace_cext_pending_obj;
                cext_ctx.epoch = pcc_gc_trace_cext_pending_epoch;
                cext_ctx.backend = pcc_gc_trace_cext_pending_backend;
            }
            pcc_gc_graph_unlock();
            if (seed_epoch != 0) {
                (void)pcc_gc_complete_mark_cycle_seed(
                    seed_epoch,
                    seed_backend
                );
            }
            if (cext_ctx.obj != NULL) {
                (void)pcc_gc_trace_cext_complete(&cext_ctx);
            }
            if (
                followup_budget > 0
                && traced == 0
                && claim_epoch == 0
            ) {
                traced += pcc_gc_step_trace_cycle(followup_budget);
            }
            if (claim_epoch != 0) {
                (void)pcc_gc_complete_claimed_tracing_cycle(
                    claim_epoch, claim_backend
                );
            }
            if (worker_stw == 0) (void)pcc_resume_world();
            if (traced > 0) {
                __atomic_add_fetch(
                    &pcc_gc_cms_worker_traces,
                    traced,
                    __ATOMIC_RELAXED
                );
            }
            __atomic_add_fetch(
                &pcc_gc_cms_worker_drains, 1, __ATOMIC_RELAXED
            );
            drained++;
        }
        pcc_thread_safepoint();
        if (__atomic_load_n(
                &pcc_gc_cms_worker_stop_requested,
                __ATOMIC_ACQUIRE
            ) != 0) {
            break;
        }
        if (drained == 0) usleep(1000);
    }
    __atomic_add_fetch(
        &pcc_gc_cms_worker_stops, 1, __ATOMIC_RELAXED
    );
    return NULL;
}

static void pcc_gc_cms_maybe_start_worker(void) {
    if (pcc_gc_selected_backend != PCC_GC_KIND_CONCURRENT_MARK_SWEEP) return;
    if (!pcc_threads_enabled()) return;
    __atomic_store_n(
        &pcc_gc_cms_worker_stop_requested, 0, __ATOMIC_RELEASE
    );
    int32_t expected = 0;
    if (!__atomic_compare_exchange_n(
            &pcc_gc_cms_worker_started,
            &expected,
            1,
            0,
            __ATOMIC_ACQ_REL,
            __ATOMIC_ACQUIRE
        )) {
        return;
    }
    PccThreadHandle *handle = NULL;
    if (pcc_thread_start(&handle, pcc_gc_cms_worker_main, NULL) == 0) {
        pcc_gc_cms_worker_handle = handle;
        __atomic_add_fetch(
            &pcc_gc_cms_worker_starts, 1, __ATOMIC_RELAXED
        );
        return;
    }
    __atomic_store_n(&pcc_gc_cms_worker_started, 0, __ATOMIC_RELEASE);
}

static void pcc_gc_cms_pause_worker_preserve_queue(void) {
    PccThreadHandle *handle = pcc_gc_cms_worker_handle;
    if (
        __atomic_load_n(&pcc_gc_cms_worker_started, __ATOMIC_ACQUIRE) != 0
        && handle != NULL
    ) {
        __atomic_store_n(
            &pcc_gc_cms_worker_stop_requested, 1, __ATOMIC_RELEASE
        );
        (void)pcc_thread_join(handle, NULL);
    }
    pcc_gc_cms_worker_handle = NULL;
    __atomic_store_n(&pcc_gc_cms_worker_started, 0, __ATOMIC_RELEASE);
    __atomic_store_n(
        &pcc_gc_cms_worker_stop_requested, 0, __ATOMIC_RELEASE
    );
}

static void pcc_gc_cms_reset_queue_and_tls(void) {
    pcc_gc_cms_queue_lock();
    (void)pcc_gc_cms_queue_epoch_advance();
    pcc_gc_cms_queue_head = 0;
    pcc_gc_cms_queue_tail = 0;
    pcc_gc_cms_queue_unlock();
    pcc_gc_cms_wb_discard_tls();
}

static PccGcForwardNode *pcc_gc_forwarding_find(PyObject *from) {
    if (from == NULL || PY_IS_TAGGED_INT(from)) return NULL;
    return (PccGcForwardNode *)pcc_gc_forwarding_index_find(from);
}

static PccGcForwardNode *pcc_gc_forwarding_target_find(PyObject *target) {
    if (target == NULL || PY_IS_TAGGED_INT(target)) return NULL;
    return (PccGcForwardNode *)pcc_gc_forwarding_target_index_find(target);
}

static int pcc_gc_forwarding_target_exists(PyObject *target) {
    if (target == NULL || PY_IS_TAGGED_INT(target)) return 0;
    return pcc_gc_forwarding_target_find(target) != NULL;
}

static int pcc_gc_forwarding_target_prepare(
    PyObject *target,
    PccGcForwardNode *node,
    PccGcForwardNode **head_out
) {
    if (
        target == NULL
        || PY_IS_TAGGED_INT(target)
        || node == NULL
        || head_out == NULL
    ) return -1;
    PccGcForwardNode *head = pcc_gc_forwarding_target_find(target);
    int64_t rc = (
        head == NULL
        ? pcc_gc_forwarding_target_index_insert(target, node)
        : pcc_gc_forwarding_target_index_upsert(target, node)
    );
    if (rc < 0) return -1;
    *head_out = head;
    return 0;
}

static void pcc_gc_forwarding_target_attach_prepared(
    PccGcForwardNode *node,
    PccGcForwardNode *old_head
) {
    if (node == NULL) return;
    node->target_next = old_head;
    node->target_prev = NULL;
    if (old_head != NULL) old_head->target_prev = node;
}

static void pcc_gc_forwarding_target_unlink(PccGcForwardNode *node) {
    if (node == NULL || node->to == NULL || PY_IS_TAGGED_INT(node->to)) {
        return;
    }
    PccGcForwardNode *prev = node->target_prev;
    PccGcForwardNode *next = node->target_next;
    if (prev != NULL) {
        prev->target_next = next;
    } else if (next != NULL) {
        (void)pcc_gc_forwarding_target_index_upsert(node->to, next);
    } else {
        (void)pcc_gc_forwarding_target_index_remove(node->to);
    }
    if (next != NULL) next->target_prev = prev;
    node->target_next = NULL;
    node->target_prev = NULL;
}

static void pcc_gc_forwarding_unlink_main(PccGcForwardNode *node) {
    if (node == NULL) return;
    if (node->prev != NULL) {
        node->prev->next = node->next;
    } else {
        pcc_gc_forwardings = node->next;
    }
    if (node->next != NULL) node->next->prev = node->prev;
    node->prev = NULL;
    node->next = NULL;
}

static void pcc_gc_backend4_zpage_note_forwarding_removed_unlocked(
    PyObject *from
);
static void pcc_gc_backend4_note_forwarding_removed_on_page_unlocked(
    struct PccGcZPage *page
);

static PccGcForwardNode *pcc_gc_forwarding_detach(PyObject *from) {
    if (from == NULL || PY_IS_TAGGED_INT(from)) return NULL;
    PccGcForwardNode *dead = (
        PccGcForwardNode *
    )pcc_gc_forwarding_index_remove(from);
    if (dead == NULL) return NULL;
    pcc_gc_forwarding_target_unlink(dead);
    pcc_gc_forwarding_unlink_main(dead);
    PccGcZPage *from_page = dead->from_page;
    if (pcc_gc_forwarding_population > 0) pcc_gc_forwarding_population--;
    if (from_page != NULL) {
        pcc_gc_backend4_note_forwarding_removed_on_page_unlocked(from_page);
    } else {
        pcc_gc_backend4_zpage_note_forwarding_removed_unlocked(from);
    }
    return dead;
}

static void pcc_gc_forwarding_finish_detached(PccGcForwardNode *nodes) {
    while (nodes != NULL) {
        PccGcForwardNode *node = nodes;
        nodes = node->next;
        node->next = NULL;
        py_decref(node->to);
        free(node);
    }
}

static void pcc_gc_forwarding_finish_dead_targets(PccGcForwardNode *nodes) {
    while (nodes != NULL) {
        PccGcForwardNode *node = nodes;
        nodes = node->next;
        node->next = NULL;
        /* Target death has already consumed the target's logical count. */
        node->to = NULL;
        free(node);
    }
}

static void pcc_gc_forwarding_detach_into_finish(
    PyObject *from,
    PccGcBackend4RemapFinish *finish
) {
    if (finish == NULL) return;
    PccGcForwardNode *dead = pcc_gc_forwarding_detach(from);
    if (dead == NULL) return;
    dead->next = finish->forwardings;
    finish->forwardings = dead;
}

static void pcc_gc_forwarding_remove(PyObject *from) {
    pcc_gc_forwarding_finish_detached(pcc_gc_forwarding_detach(from));
}

static void pcc_gc_forwarding_remove_target(
    PyObject *target,
    PccGcBackend4RemapFinish *finish
) {
    if (
        target == NULL
        || PY_IS_TAGGED_INT(target)
        || finish == NULL
    ) return;
    /* Detach the reverse index before cleanup so decref reentry cannot walk
     * this dying target twice. The source index/main edge and flags remain
     * live for healing; preparation failure is unconditional fail-stop, not
     * recoverable whole-transaction rollback. */
    PccGcForwardNode *n = (
        PccGcForwardNode *
    )pcc_gc_forwarding_target_index_remove(target);
    while (n != NULL) {
        PccGcForwardNode *next = n->target_next;
        PyObject *from = n->from;
        if (
            pcc_gc_selected_backend == PCC_GC_KIND_COLORED_RELOCATING
            && pcc_gc_relocation_retire_source_payload_for_target_death_into_finish(
                from,
                target,
                finish
            ) == 0
        ) {
            PCC_GC_DEFER_TRIPWIRE(
                0,
                "forwarded-source payload retirement failed before target teardown"
            );
            return;
        }
        (void)pcc_gc_forwarding_index_remove(from);
        pcc_gc_forwarding_unlink_main(n);
        /* The target died before the normal remap-retirement epoch.  Retire
         * its old shell now; otherwise the object index keeps reporting a
         * freed forwarding source as managed. */
        pcc_gc_retire_forwarded_source_into_finish_unlocked(from, finish);
        n->target_next = NULL;
        n->target_prev = NULL;
        PccGcZPage *fp = n->from_page;
        n->to = NULL;
        n->next = finish->dead_target_forwardings;
        finish->dead_target_forwardings = n;
        if (pcc_gc_forwarding_population > 0) pcc_gc_forwarding_population--;
        if (fp != NULL) {
            pcc_gc_backend4_note_forwarding_removed_on_page_unlocked(fp);
        } else {
            pcc_gc_backend4_zpage_note_forwarding_removed_unlocked(from);
        }
        n = next;
    }
}

static void pcc_gc_forwarding_clear_all(void) {
    PccGcForwardNode *n = pcc_gc_forwardings;
    pcc_gc_forwardings = NULL;
    pcc_gc_forwarding_index_clear();
    pcc_gc_forwarding_target_index_clear();
    while (n != NULL) {
        PccGcForwardNode *next = n->next;
        py_decref(n->to);
        free(n);
        n = next;
    }
    pcc_gc_forwarding_population = 0;
}

int64_t pcc_gc_forwarding_population_load(void) {
    return pcc_gc_forwarding_population;
}

int64_t pcc_gc_relocation_set_active_load(void) {
    return pcc_gc_relocation_set != NULL ? 1 : 0;
}

static PccGcIdentityNode *pcc_gc_identity_find(PyObject *obj) {
    if (obj == NULL || PY_IS_TAGGED_INT(obj)) return NULL;
    return (PccGcIdentityNode *)pcc_gc_identity_index_find(obj);
}

static PccGcIdentityNode *pcc_gc_identity_ensure(PyObject *obj) {
    if (obj == NULL || PY_IS_TAGGED_INT(obj)) return NULL;
    PccGcIdentityNode *existing = pcc_gc_identity_find(obj);
    if (existing != NULL) return existing;

    PccGcIdentityNode *n = (
        PccGcIdentityNode *
    )calloc(1, sizeof(PccGcIdentityNode));
    if (n == NULL) return NULL;
    if (pcc_gc_next_object_id <= 0) pcc_gc_next_object_id = 1;
    n->obj = obj;
    n->id = pcc_gc_next_object_id++;
    n->next = pcc_gc_identities;
    n->prev = NULL;
    if (pcc_gc_identities != NULL) pcc_gc_identities->prev = n;
    pcc_gc_identities = n;
    if (pcc_gc_identity_index_insert(obj, n) < 0) {
        pcc_gc_identities = n->next;
        if (n->next != NULL) n->next->prev = NULL;
        free(n);
        return NULL;
    }
    return n;
}

static int pcc_gc_identity_assign(PyObject *obj, int64_t id) {
    if (obj == NULL || PY_IS_TAGGED_INT(obj) || id <= 0) return 0;
    PccGcIdentityNode *existing = pcc_gc_identity_find(obj);
    if (existing != NULL) {
        existing->id = id;
        return 1;
    }

    PccGcIdentityNode *n = (
        PccGcIdentityNode *
    )calloc(1, sizeof(PccGcIdentityNode));
    if (n == NULL) return 0;
    n->obj = obj;
    n->id = id;
    n->next = pcc_gc_identities;
    n->prev = NULL;
    if (pcc_gc_identities != NULL) pcc_gc_identities->prev = n;
    pcc_gc_identities = n;
    if (pcc_gc_identity_index_insert(obj, n) < 0) {
        pcc_gc_identities = n->next;
        if (n->next != NULL) n->next->prev = NULL;
        free(n);
        return 0;
    }
    return 1;
}

static PccGcIdentityNode *pcc_gc_identity_detach(PyObject *obj) {
    if (obj == NULL || PY_IS_TAGGED_INT(obj)) return NULL;
    PccGcIdentityNode *dead = (
        PccGcIdentityNode *
    )pcc_gc_identity_index_remove(obj);
    if (dead == NULL) return NULL;
    if (dead->prev != NULL) {
        dead->prev->next = dead->next;
    } else {
        pcc_gc_identities = dead->next;
    }
    if (dead->next != NULL) dead->next->prev = dead->prev;
    dead->prev = NULL;
    dead->next = NULL;
    return dead;
}

static void pcc_gc_identity_finish_detached(PccGcIdentityNode *nodes) {
    while (nodes != NULL) {
        PccGcIdentityNode *node = nodes;
        nodes = node->next;
        node->next = NULL;
        free(node);
    }
}

static void pcc_gc_identity_remove(PyObject *obj) {
    pcc_gc_identity_finish_detached(pcc_gc_identity_detach(obj));
}

static void pcc_gc_identity_clear_all(void) {
    PccGcIdentityNode *n = pcc_gc_identities;
    pcc_gc_identities = NULL;
    pcc_gc_identity_index_clear();
    while (n != NULL) {
        PccGcIdentityNode *next = n->next;
        free(n);
        n = next;
    }
}

int64_t pcc_gc_object_id(PyObject *o) {
    pcc_gc_init_config();
    if (o == NULL || PY_IS_TAGGED_INT(o)) return 0;
    PccGcIdentityNode *identity = pcc_gc_identity_ensure(o);
    if (identity == NULL) return 0;
    return identity->id;
}

static PccGcRelocationNode *pcc_gc_relocation_set_find(PyObject *obj) {
    if (obj == NULL || PY_IS_TAGGED_INT(obj)) return NULL;
    for (
        PccGcRelocationNode *n = pcc_gc_relocation_set;
        n != NULL;
        n = n->next
    ) {
        if (n->obj == obj) return n;
    }
    return NULL;
}

static int pcc_gc_relocation_set_add(PyObject *obj) {
    if (obj == NULL || PY_IS_TAGGED_INT(obj)) return 0;
    if (
        pcc_gc_backend4_relocation_reset_owner != 0
        || pcc_gc_backend4_reseed_commit_owner != 0
    ) return 0;
    PyObjectHeader *h = py_header(obj);
    int32_t flags = py_header_flags_load(h);
    if (
        (flags & (
            PY_FLAG_GC_RELOCATION_CANDIDATE
            | PY_FLAG_GC_RELOCATION_TARGET
            | PY_FLAG_GC_PINNED
            | PY_FLAG_GC_FRESH_ALLOC
            | PY_FLAG_GC_DEALLOCATING
        )) != 0
    ) {
        return 0;
    }
    if (pcc_gc_forwarding_find(obj) != NULL) return 0;
    if (pcc_gc_forwarding_target_exists(obj)) return 0;
    PccGcRelocationNode *n = (
        PccGcRelocationNode *
    )calloc(1, sizeof(PccGcRelocationNode));
    if (n == NULL) return 0;
    n->obj = obj;
    n->next = pcc_gc_relocation_set;
    pcc_gc_relocation_set = n;
    pcc_gc_backend4_reseed_relocation_revision++;
    py_header_flags_or(h, PY_FLAG_GC_RELOCATION_CANDIDATE);
    return 1;
}

static int pcc_gc_relocation_set_add_preallocated(
    PyObject *obj,
    PccGcRelocationNode **available
) {
    if (
        obj == NULL
        || PY_IS_TAGGED_INT(obj)
        || available == NULL
        || *available == NULL
        || pcc_gc_backend4_relocation_reset_owner != 0
        || pcc_gc_backend4_reseed_commit_owner != 0
    ) return 0;
    PyObjectHeader *h = py_header(obj);
    int32_t flags = py_header_flags_load(h);
    if (
        (flags & (
            PY_FLAG_GC_RELOCATION_CANDIDATE
            | PY_FLAG_GC_RELOCATION_TARGET
            | PY_FLAG_GC_PINNED
            | PY_FLAG_GC_FRESH_ALLOC
            | PY_FLAG_GC_DEALLOCATING
        )) != 0
    ) return 0;
    if (pcc_gc_forwarding_find(obj) != NULL) return 0;
    if (pcc_gc_forwarding_target_exists(obj)) return 0;
    PccGcRelocationNode *n = *available;
    *available = n->next;
    n->obj = obj;
    n->next = pcc_gc_relocation_set;
    pcc_gc_relocation_set = n;
    pcc_gc_backend4_reseed_relocation_revision++;
    py_header_flags_or(h, PY_FLAG_GC_RELOCATION_CANDIDATE);
    return 1;
}

int64_t pcc_gc_backend4_relocation_set_add(PyObject *obj) {
    pcc_gc_init_config();
    if (pcc_gc_selected_backend != PCC_GC_KIND_COLORED_RELOCATING) return 0;
    if (obj == NULL || PY_IS_TAGGED_INT(obj)) return 0;
    PccGcRelocationNode *available = (
        PccGcRelocationNode *
    )calloc(1, sizeof(PccGcRelocationNode));
    if (available == NULL) return 0;
    pcc_gc_graph_lock();
    int added = pcc_gc_relocation_set_add_preallocated(obj, &available);
    pcc_gc_graph_unlock();
    free(available);
    return added;
}

static PccGcRelocationNode *pcc_gc_relocation_set_detach(PyObject *obj) {
    if (obj == NULL || PY_IS_TAGGED_INT(obj)) return NULL;
    PccGcRelocationNode **cur = &pcc_gc_relocation_set;
    while (*cur != NULL) {
        if ((*cur)->obj == obj) {
            PccGcRelocationNode *dead = *cur;
            *cur = dead->next;
            if (pcc_gc_backend4_reseed_relocation_cursor == dead) {
                pcc_gc_backend4_reseed_relocation_cursor = dead->next;
            }
            pcc_gc_backend4_reseed_relocation_revision++;
            dead->next = NULL;
            if (pcc_gc_forwarding_find(obj) == NULL) {
                py_header_flags_and(
                    py_header(obj), ~PY_FLAG_GC_RELOCATION_CANDIDATE
                );
            }
            return dead;
        }
        cur = &(*cur)->next;
    }
    return NULL;
}

static void pcc_gc_relocation_set_remove(PyObject *obj) {
    PccGcRelocationNode *dead = pcc_gc_relocation_set_detach(obj);
    if (dead != NULL) free(dead);
}

static void pcc_gc_backend4_store_buffer_dec_unlocked(void) {
    int64_t pending = __atomic_load_n(
        &pcc_gc_backend4_store_buffer_entries_count,
        __ATOMIC_RELAXED
    );
    if (pending <= 0) return;
    __atomic_sub_fetch(
        &pcc_gc_backend4_store_buffer_entries_count,
        1,
        __ATOMIC_RELAXED
    );
}

static void pcc_gc_backend4_store_buffer_note_high_water(int64_t current) {
    int64_t old = __atomic_load_n(
        &pcc_gc_backend4_store_buffer_high_water_count,
        __ATOMIC_RELAXED
    );
    while (current > old) {
        if (__atomic_compare_exchange_n(
                &pcc_gc_backend4_store_buffer_high_water_count,
                &old,
                current,
                0,
                __ATOMIC_RELAXED,
                __ATOMIC_RELAXED
            )) {
            return;
        }
    }
}

static void pcc_gc_backend4_store_buffer_note_owner_fanout_high_water(
    int64_t current
) {
    int64_t old = __atomic_load_n(
        &pcc_gc_backend4_store_buffer_owner_fanout_high_water_count,
        __ATOMIC_RELAXED
    );
    while (current > old) {
        if (__atomic_compare_exchange_n(
                &pcc_gc_backend4_store_buffer_owner_fanout_high_water_count,
                &old,
                current,
                0,
                __ATOMIC_RELAXED,
                __ATOMIC_RELAXED
            )) {
            return;
        }
    }
}

static PccGcStoreBufferMediumState *
pcc_gc_backend4_store_buffer_register_medium_locked(void) {
    if (pcc_gc_backend4_store_buffer_medium_state != NULL) {
        return pcc_gc_backend4_store_buffer_medium_state;
    }
    PccGcStoreBufferMediumState *state = (
        PccGcStoreBufferMediumState *
    )calloc(1, sizeof(PccGcStoreBufferMediumState));
    if (state == NULL) return NULL;
    state->entries = pcc_gc_backend4_store_buffer_medium;
    state->count = &pcc_gc_backend4_store_buffer_medium_count;
    state->next = pcc_gc_backend4_store_buffer_medium_states;
    pcc_gc_backend4_store_buffer_medium_states = state;
    pcc_gc_backend4_store_buffer_medium_state = state;
    return state;
}

static int64_t pcc_gc_backend4_store_buffer_owner_fanout(PyObject *owner) {
    if (owner == NULL || PY_IS_TAGGED_INT(owner)) return 0;
    int64_t count = 0;
    for (
        PccGcStoreBufferMediumState *state =
            pcc_gc_backend4_store_buffer_medium_states;
        state != NULL;
        state = state->next
    ) {
        int32_t medium_count = state->count == NULL ? 0 : *state->count;
        for (int32_t i = 0; i < medium_count; i++) {
            if (state->entries[i].owner == owner) count++;
        }
    }
    for (
        PccGcStoreBufferNode *n = pcc_gc_backend4_store_buffer;
        n != NULL;
        n = n->next
    ) {
        if (n->owner == owner) count++;
    }
    return count;
}

static int64_t pcc_gc_backend4_store_buffer_owner_count(void) {
    int64_t count = 0;
    for (
        PccGcStoreBufferMediumState *state =
            pcc_gc_backend4_store_buffer_medium_states;
        state != NULL;
        state = state->next
    ) {
        int32_t medium_count = state->count == NULL ? 0 : *state->count;
        for (int32_t i = 0; i < medium_count; i++) {
            PyObject *owner = state->entries[i].owner;
            int seen = 0;
            for (
                PccGcStoreBufferMediumState *prev =
                    pcc_gc_backend4_store_buffer_medium_states;
                prev != NULL;
                prev = prev->next
            ) {
                int32_t prev_count = prev->count == NULL ? 0 : *prev->count;
                int32_t stop = prev == state ? i : prev_count;
                for (int32_t p = 0; p < stop; p++) {
                    if (prev->entries[p].owner == owner) {
                        seen = 1;
                        break;
                    }
                }
                if (seen || prev == state) break;
            }
            if (!seen) count++;
        }
    }
    for (
        PccGcStoreBufferNode *n = pcc_gc_backend4_store_buffer;
        n != NULL;
        n = n->next
    ) {
        int seen = 0;
        for (
            PccGcStoreBufferMediumState *state =
                pcc_gc_backend4_store_buffer_medium_states;
            state != NULL;
            state = state->next
        ) {
            int32_t medium_count = state->count == NULL ? 0 : *state->count;
            for (int32_t i = 0; i < medium_count; i++) {
                if (state->entries[i].owner == n->owner) {
                    seen = 1;
                    break;
                }
            }
            if (seen) break;
        }
        for (
            PccGcStoreBufferNode *p = pcc_gc_backend4_store_buffer;
            p != NULL && p != n;
            p = p->next
        ) {
            if (p->owner == n->owner) {
                seen = 1;
                break;
            }
        }
        if (!seen) count++;
    }
    return count;
}

static int64_t pcc_gc_backend4_store_buffer_entry_count(void) {
    int64_t count = 0;
    for (
        PccGcStoreBufferMediumState *state =
            pcc_gc_backend4_store_buffer_medium_states;
        state != NULL;
        state = state->next
    ) {
        count += state->count == NULL ? 0 : *state->count;
    }
    for (
        PccGcStoreBufferNode *n = pcc_gc_backend4_store_buffer;
        n != NULL;
        n = n->next
    ) {
        count++;
    }
    return count;
}

static int64_t pcc_gc_backend4_store_buffer_max_owner_fanout(void) {
    int64_t max_fanout = 0;
    for (
        PccGcStoreBufferMediumState *state =
            pcc_gc_backend4_store_buffer_medium_states;
        state != NULL;
        state = state->next
    ) {
        int32_t medium_count = state->count == NULL ? 0 : *state->count;
        for (int32_t i = 0; i < medium_count; i++) {
            int64_t fanout = pcc_gc_backend4_store_buffer_owner_fanout(
                state->entries[i].owner
            );
            if (fanout > max_fanout) max_fanout = fanout;
        }
    }
    for (
        PccGcStoreBufferNode *n = pcc_gc_backend4_store_buffer;
        n != NULL;
        n = n->next
    ) {
        int64_t fanout = pcc_gc_backend4_store_buffer_owner_fanout(n->owner);
        if (fanout > max_fanout) max_fanout = fanout;
    }
    return max_fanout;
}

static void pcc_gc_backend4_reset_store_buffer_epoch_state(void) {
    pcc_gc_graph_lock();
    int64_t entries = pcc_gc_backend4_store_buffer_entry_count();
    int64_t owner_fanout = pcc_gc_backend4_store_buffer_max_owner_fanout();
    int64_t owner_count = pcc_gc_backend4_store_buffer_owner_count();
    __atomic_store_n(
        &pcc_gc_backend4_store_buffer_entries_count,
        entries,
        __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_store_buffer_high_water_count,
        entries,
        __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_store_buffer_owner_fanout_high_water_count,
        owner_fanout,
        __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_store_buffer_owner_count_high_water_count,
        owner_count,
        __ATOMIC_RELAXED
    );
    pcc_gc_graph_unlock();
}

static void pcc_gc_backend4_store_buffer_note_owner_count_high_water(
    int64_t current
) {
    int64_t old = __atomic_load_n(
        &pcc_gc_backend4_store_buffer_owner_count_high_water_count,
        __ATOMIC_RELAXED
    );
    while (current > old) {
        if (__atomic_compare_exchange_n(
                &pcc_gc_backend4_store_buffer_owner_count_high_water_count,
                &old,
                current,
                0,
                __ATOMIC_RELAXED,
                __ATOMIC_RELAXED
            )) {
            return;
        }
    }
}

static void pcc_gc_backend4_store_buffer_note_max_batch(int64_t current) {
    int64_t old = __atomic_load_n(
        &pcc_gc_backend4_store_buffer_max_batch_size_count,
        __ATOMIC_RELAXED
    );
    while (current > old) {
        if (__atomic_compare_exchange_n(
                &pcc_gc_backend4_store_buffer_max_batch_size_count,
                &old,
                current,
                0,
                __ATOMIC_RELAXED,
                __ATOMIC_RELAXED
            )) {
            return;
        }
    }
}

static void pcc_gc_backend4_remembered_set_note_high_water(int64_t current) {
    int64_t old = __atomic_load_n(
        &pcc_gc_backend4_remembered_set_high_water_count,
        __ATOMIC_RELAXED
    );
    while (current > old) {
        if (__atomic_compare_exchange_n(
                &pcc_gc_backend4_remembered_set_high_water_count,
                &old,
                current,
                0,
                __ATOMIC_RELAXED,
                __ATOMIC_RELAXED
            )) {
            return;
        }
    }
}

static uintptr_t pcc_gc_backend4_remembered_page_key(PyObject **slot) {
    return ((uintptr_t)slot) & ~(uintptr_t)(PCC_GC_BACKEND4_SMALL_PAGE_LIMIT - 1);
}

static int32_t pcc_gc_backend4_remembered_page_slot_bit(PyObject **slot) {
    uintptr_t offset = ((uintptr_t)slot)
        & (uintptr_t)(PCC_GC_BACKEND4_SMALL_PAGE_LIMIT - 1);
    return (int32_t)(offset / sizeof(PyObject *));
}

static void pcc_gc_backend4_remembered_page_note_high_water(int64_t current) {
    int64_t old = __atomic_load_n(
        &pcc_gc_backend4_remembered_page_high_water_count,
        __ATOMIC_RELAXED
    );
    while (current > old) {
        if (__atomic_compare_exchange_n(
                &pcc_gc_backend4_remembered_page_high_water_count,
                &old,
                current,
                0,
                __ATOMIC_RELAXED,
                __ATOMIC_RELAXED
            )) {
            return;
        }
    }
}

static PccGcRememberedPageNode *pcc_gc_backend4_remembered_page_find(
    uintptr_t page_key
) {
    for (
        PccGcRememberedPageNode *n = pcc_gc_backend4_remembered_pages;
        n != NULL;
        n = n->next
    ) {
        if (n->page_key == page_key) return n;
    }
    return NULL;
}

static int pcc_gc_backend4_remembered_page_contains_slot_unlocked(
    PyObject **slot
) {
    if (slot == NULL) return 0;
    uintptr_t page_key = pcc_gc_backend4_remembered_page_key(slot);
    PccGcRememberedPageNode *page =
        pcc_gc_backend4_remembered_page_find(page_key);
    if (page == NULL) return 0;
    int32_t bit = pcc_gc_backend4_remembered_page_slot_bit(slot);
    if (bit < 0 || bit >= 512) return 0;
    int32_t word = bit / 64;
    uint64_t mask = (uint64_t)1 << (uint64_t)(bit % 64);
    return (page->slot_bitmap[word] & mask) != 0;
}

static void pcc_gc_backend4_remembered_page_add(PyObject **slot) {
    if (slot == NULL) return;
    uintptr_t page_key = pcc_gc_backend4_remembered_page_key(slot);
    PccGcRememberedPageNode *page =
        pcc_gc_backend4_remembered_page_find(page_key);
    if (page == NULL) {
        page = (
            PccGcRememberedPageNode *
        )calloc(1, sizeof(PccGcRememberedPageNode));
        if (page == NULL) return;
        page->page_key = page_key;
        page->next = pcc_gc_backend4_remembered_pages;
        pcc_gc_backend4_remembered_pages = page;
        int64_t pages = __atomic_add_fetch(
            &pcc_gc_backend4_remembered_page_entries_count,
            1,
            __ATOMIC_RELAXED
        );
        pcc_gc_backend4_remembered_page_note_high_water(pages);
    }
    int32_t bit = pcc_gc_backend4_remembered_page_slot_bit(slot);
    if (bit < 0 || bit >= 512) return;
    int32_t word = bit / 64;
    uint64_t mask = (uint64_t)1 << (uint64_t)(bit % 64);
    if ((page->slot_bitmap[word] & mask) != 0) return;
    page->slot_bitmap[word] |= mask;
    page->slots++;
    __atomic_add_fetch(
        &pcc_gc_backend4_remembered_page_slot_entries_count,
        1,
        __ATOMIC_RELAXED
    );
}

static void pcc_gc_backend4_remembered_page_remove_slot(PyObject **slot) {
    if (slot == NULL) return;
    uintptr_t page_key = pcc_gc_backend4_remembered_page_key(slot);
    PccGcRememberedPageNode **cur = &pcc_gc_backend4_remembered_pages;
    while (*cur != NULL) {
        if ((*cur)->page_key == page_key) {
            PccGcRememberedPageNode *page = *cur;
            int32_t bit = pcc_gc_backend4_remembered_page_slot_bit(slot);
            if (bit < 0 || bit >= 512) return;
            int32_t word = bit / 64;
            uint64_t mask = (uint64_t)1 << (uint64_t)(bit % 64);
            if ((page->slot_bitmap[word] & mask) == 0) return;
            page->slot_bitmap[word] &= ~mask;
            if (page->slots > 0) page->slots--;
            int64_t slots = __atomic_load_n(
                &pcc_gc_backend4_remembered_page_slot_entries_count,
                __ATOMIC_RELAXED
            );
            if (slots > 0) {
                __atomic_sub_fetch(
                    &pcc_gc_backend4_remembered_page_slot_entries_count,
                    1,
                    __ATOMIC_RELAXED
                );
            }
            if (page->slots <= 0) {
                *cur = page->next;
                int64_t pages = __atomic_load_n(
                    &pcc_gc_backend4_remembered_page_entries_count,
                    __ATOMIC_RELAXED
                );
                if (pages > 0) {
                    __atomic_sub_fetch(
                        &pcc_gc_backend4_remembered_page_entries_count,
                        1,
                        __ATOMIC_RELAXED
                    );
                }
                free(page);
            }
            return;
        }
        cur = &(*cur)->next;
    }
}

static int pcc_gc_backend4_remembered_set_contains(
    PyObject *owner,
    PyObject **slot
) {
    if (owner == NULL || PY_IS_TAGGED_INT(owner)) return 0;
    if (slot == NULL) return 0;
    for (
        PccGcRememberedSlotNode *n = pcc_gc_backend4_remembered_slots;
        n != NULL;
        n = n->next
    ) {
        if (n->owner == owner && n->slot == slot) return 1;
    }
    return 0;
}

static int pcc_gc_backend4_remembered_set_add(
    PyObject *owner,
    PyObject **slot
) {
    if (owner == NULL || PY_IS_TAGGED_INT(owner)) return 0;
    if (slot == NULL) return 0;
    if (pcc_gc_backend4_remembered_set_contains(owner, slot)) {
        __atomic_add_fetch(
            &pcc_gc_backend4_remembered_set_duplicate_skips_count,
            1,
            __ATOMIC_RELAXED
        );
        return 0;
    }
    PccGcRememberedSlotNode *n = (
        PccGcRememberedSlotNode *
    )calloc(1, sizeof(PccGcRememberedSlotNode));
    if (n == NULL) return 0;
    n->owner = owner;
    n->slot = slot;
    n->next = pcc_gc_backend4_remembered_slots;
    pcc_gc_backend4_remembered_slots = n;
    int64_t entries = __atomic_add_fetch(
        &pcc_gc_backend4_remembered_set_entries_count,
        1,
        __ATOMIC_RELAXED
    );
    pcc_gc_backend4_remembered_set_note_high_water(entries);
    pcc_gc_backend4_remembered_page_add(slot);
    pcc_gc_backend4_zpage_note_remembered_slot_unlocked(owner, 1);
    pcc_gc_backend4_zpage_note_remembered_card_unlocked(owner, slot, 1);
    return 1;
}

static int pcc_gc_backend4_remembered_set_remove_slot(PyObject **slot) {
    if (slot == NULL) return 0;
    int removed = 0;
    PccGcRememberedSlotNode **cur = &pcc_gc_backend4_remembered_slots;
    while (*cur != NULL) {
        if ((*cur)->slot == slot) {
            PccGcRememberedSlotNode *dead = *cur;
            *cur = dead->next;
            pcc_gc_backend4_remembered_page_remove_slot(dead->slot);
            pcc_gc_backend4_zpage_note_remembered_slot_unlocked(
                dead->owner,
                -1
            );
            pcc_gc_backend4_zpage_note_remembered_card_unlocked(
                dead->owner,
                dead->slot,
                -1
            );
            int64_t entries = __atomic_load_n(
                &pcc_gc_backend4_remembered_set_entries_count,
                __ATOMIC_RELAXED
            );
            if (entries > 0) {
                __atomic_sub_fetch(
                    &pcc_gc_backend4_remembered_set_entries_count,
                    1,
                    __ATOMIC_RELAXED
                );
            }
            free(dead);
            removed = 1;
            continue;
        }
        cur = &(*cur)->next;
    }
    return removed;
}

static void pcc_gc_backend4_remembered_set_remove(PyObject *owner) {
    if (owner == NULL || PY_IS_TAGGED_INT(owner)) return;
    PccGcRememberedSlotNode **cur = &pcc_gc_backend4_remembered_slots;
    while (*cur != NULL) {
        if ((*cur)->owner == owner) {
            PccGcRememberedSlotNode *dead = *cur;
            *cur = dead->next;
            pcc_gc_backend4_remembered_page_remove_slot(dead->slot);
            pcc_gc_backend4_zpage_note_remembered_slot_unlocked(
                dead->owner,
                -1
            );
            pcc_gc_backend4_zpage_note_remembered_card_unlocked(
                dead->owner,
                dead->slot,
                -1
            );
            int64_t entries = __atomic_load_n(
                &pcc_gc_backend4_remembered_set_entries_count,
                __ATOMIC_RELAXED
            );
            if (entries > 0) {
                __atomic_sub_fetch(
                    &pcc_gc_backend4_remembered_set_entries_count,
                    1,
                    __ATOMIC_RELAXED
                );
            }
            free(dead);
            continue;
        }
        cur = &(*cur)->next;
    }
}

static int64_t pcc_gc_backend4_remembered_set_entry_count(void) {
    int64_t count = 0;
    for (
        PccGcRememberedSlotNode *n = pcc_gc_backend4_remembered_slots;
        n != NULL;
        n = n->next
    ) {
        count++;
    }
    return count;
}

static void pcc_gc_backend4_reset_remembered_set_epoch_state(void) {
    pcc_gc_graph_lock();
    int64_t entries = pcc_gc_backend4_remembered_set_entry_count();
    int64_t pages = __atomic_load_n(
        &pcc_gc_backend4_remembered_page_entries_count,
        __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_remembered_set_entries_count,
        entries,
        __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_remembered_set_high_water_count,
        entries,
        __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_remembered_page_high_water_count,
        pages,
        __ATOMIC_RELAXED
    );
    pcc_gc_graph_unlock();
}

static void pcc_gc_backend4_remembered_set_clear(void) {
    PccGcRememberedSlotNode *n = pcc_gc_backend4_remembered_slots;
    pcc_gc_backend4_remembered_slots = NULL;
    while (n != NULL) {
        PccGcRememberedSlotNode *next = n->next;
        pcc_gc_backend4_zpage_note_remembered_slot_unlocked(n->owner, -1);
        pcc_gc_backend4_zpage_note_remembered_card_unlocked(
            n->owner,
            n->slot,
            -1
        );
        free(n);
        n = next;
    }
    PccGcRememberedPageNode *page = pcc_gc_backend4_remembered_pages;
    pcc_gc_backend4_remembered_pages = NULL;
    while (page != NULL) {
        PccGcRememberedPageNode *next = page->next;
        free(page);
        page = next;
    }
    __atomic_store_n(
        &pcc_gc_backend4_remembered_set_entries_count, 0, __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_remembered_set_high_water_count, 0, __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_remembered_page_entries_count, 0, __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_remembered_page_slot_entries_count, 0, __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_remembered_page_high_water_count, 0, __ATOMIC_RELAXED
    );
}

static int pcc_gc_backend4_store_buffer_owner_pending(PyObject *owner) {
    if (owner == NULL || PY_IS_TAGGED_INT(owner)) return 0;
    for (
        PccGcStoreBufferMediumState *state =
            pcc_gc_backend4_store_buffer_medium_states;
        state != NULL;
        state = state->next
    ) {
        int32_t medium_count = state->count == NULL ? 0 : *state->count;
        for (int32_t i = 0; i < medium_count; i++) {
            if (state->entries[i].owner == owner) return 1;
        }
    }
    for (
        PccGcStoreBufferNode *n = pcc_gc_backend4_store_buffer;
        n != NULL;
        n = n->next
    ) {
        if (n->owner == owner) return 1;
    }
    return 0;
}

static int pcc_gc_backend4_store_buffer_contains(
    PyObject *owner,
    PyObject **slot,
    PyObject *value
) {
    if (owner == NULL || PY_IS_TAGGED_INT(owner)) return 0;
    if (value == NULL || PY_IS_TAGGED_INT(value)) return 0;
    for (
        PccGcStoreBufferMediumState *state =
            pcc_gc_backend4_store_buffer_medium_states;
        state != NULL;
        state = state->next
    ) {
        int32_t medium_count = state->count == NULL ? 0 : *state->count;
        for (int32_t i = 0; i < medium_count; i++) {
            PccGcStoreBufferEntry *entry = &state->entries[i];
            if (
                entry->owner == owner
                && entry->slot == slot
                && entry->value == value
            ) {
                return 1;
            }
        }
    }
    for (
        PccGcStoreBufferNode *n = pcc_gc_backend4_store_buffer;
        n != NULL;
        n = n->next
    ) {
        if (n->owner == owner && n->slot == slot && n->value == value) {
            return 1;
        }
    }
    return 0;
}

static void pcc_gc_backend4_store_buffer_append_global_owned(
    PyObject *owner,
    PyObject **slot,
    PyObject *value
) {
    PccGcStoreBufferNode *n = (
        PccGcStoreBufferNode *
    )calloc(1, sizeof(PccGcStoreBufferNode));
    if (n == NULL) {
        pcc_gc_backend4_store_buffer_dec_unlocked();
        py_decref(value);
        return;
    }
    n->owner = owner;
    n->slot = slot;
    n->value = value;
    n->next = pcc_gc_backend4_store_buffer;
    pcc_gc_backend4_store_buffer = n;
}

static void pcc_gc_backend4_store_buffer_flush_medium_state_locked(
    PccGcStoreBufferMediumState *state
) {
    if (state == NULL || state->count == NULL || state->entries == NULL) {
        return;
    }
    int32_t count = *state->count;
    if (count <= 0) return;
    for (int32_t i = 0; i < count; i++) {
        PccGcStoreBufferEntry *entry = &state->entries[i];
        pcc_gc_backend4_store_buffer_append_global_owned(
            entry->owner,
            entry->slot,
            entry->value
        );
        entry->owner = NULL;
        entry->slot = NULL;
        entry->value = NULL;
    }
    *state->count = 0;
    __atomic_add_fetch(
        &pcc_gc_backend4_store_buffer_medium_flushes_count,
        1,
        __ATOMIC_RELAXED
    );
    __atomic_add_fetch(
        &pcc_gc_backend4_store_buffer_medium_flushed_entries_count,
        count,
        __ATOMIC_RELAXED
    );
    if (count >= PCC_GC_BACKEND4_STORE_BUFFER_MEDIUM_CAPACITY) {
        __atomic_add_fetch(
            &pcc_gc_backend4_store_buffer_medium_full_flushes_count,
            1,
            __ATOMIC_RELAXED
        );
    }
}

static int pcc_gc_backend4_store_buffer_enqueue(
    PyObject *owner,
    PyObject **slot,
    PyObject *value
) {
    if (owner == NULL || PY_IS_TAGGED_INT(owner)) return 0;
    if (value == NULL || PY_IS_TAGGED_INT(value)) return 0;
    PccGcStoreBufferMediumState *state =
        pcc_gc_backend4_store_buffer_register_medium_locked();
    if (state == NULL || state->count == NULL || state->entries == NULL) {
        return 0;
    }
    if (pcc_gc_backend4_store_buffer_contains(owner, slot, value)) {
        __atomic_add_fetch(
            &pcc_gc_backend4_store_buffer_duplicate_skips_count,
            1,
            __ATOMIC_RELAXED
        );
        return 0;
    }
    PyObjectHeader *owner_h = py_header(owner);
    if (
        *state->count
        >= PCC_GC_BACKEND4_STORE_BUFFER_MEDIUM_CAPACITY
    ) {
        pcc_gc_backend4_store_buffer_flush_medium_state_locked(state);
    }
    if (
        *state->count
        >= PCC_GC_BACKEND4_STORE_BUFFER_MEDIUM_CAPACITY
    ) {
        return 0;
    }
    py_incref(value);
    PccGcStoreBufferEntry *entry = &state->entries[*state->count];
    entry->owner = owner;
    entry->slot = slot;
    entry->value = value;
    *state->count = *state->count + 1;
    (void)pcc_gc_backend4_remembered_set_add(owner, slot);
    py_header_flags_or(owner_h, PY_FLAG_GC_REMEMBERED);
    int64_t pending = __atomic_add_fetch(
        &pcc_gc_backend4_store_buffer_entries_count,
        1,
        __ATOMIC_RELAXED
    );
    pcc_gc_backend4_store_buffer_note_high_water(pending);
    pcc_gc_backend4_store_buffer_note_owner_fanout_high_water(
        pcc_gc_backend4_store_buffer_owner_fanout(owner)
    );
    pcc_gc_backend4_store_buffer_note_owner_count_high_water(
        pcc_gc_backend4_store_buffer_owner_count()
    );
    return 1;
}

static void pcc_gc_backend4_store_buffer_remove(PyObject *owner) {
    if (owner == NULL || PY_IS_TAGGED_INT(owner)) return;
    for (
        PccGcStoreBufferMediumState *state =
            pcc_gc_backend4_store_buffer_medium_states;
        state != NULL;
        state = state->next
    ) {
        int32_t write = 0;
        int32_t medium_count = state->count == NULL ? 0 : *state->count;
        for (int32_t read = 0; read < medium_count; read++) {
            PccGcStoreBufferEntry *entry = &state->entries[read];
            if (entry->owner == owner) {
                pcc_gc_backend4_store_buffer_dec_unlocked();
                py_decref(entry->value);
                continue;
            }
            if (write != read) {
                state->entries[write] = *entry;
            }
            write++;
        }
        for (int32_t i = write; i < medium_count; i++) {
            state->entries[i].owner = NULL;
            state->entries[i].slot = NULL;
            state->entries[i].value = NULL;
        }
        if (state->count != NULL) *state->count = write;
    }
    PccGcStoreBufferNode **cur = &pcc_gc_backend4_store_buffer;
    while (*cur != NULL) {
        if ((*cur)->owner == owner) {
            PccGcStoreBufferNode *dead = *cur;
            *cur = dead->next;
            pcc_gc_backend4_store_buffer_dec_unlocked();
            py_decref(dead->value);
            free(dead);
            continue;
        }
        cur = &(*cur)->next;
    }
}

void *pcc_gc_backend4_source_side_table_plan_prepare(PyObject *owner) {
    if (owner == NULL || PY_IS_TAGGED_INT(owner)) return NULL;
    int64_t count = 0;
    for (
        PccGcStoreBufferMediumState *state =
            pcc_gc_backend4_store_buffer_medium_states;
        state != NULL;
        state = state->next
    ) {
        int32_t medium_count = state->count == NULL ? 0 : *state->count;
        for (int32_t i = 0; i < medium_count; i++) {
            if (state->entries[i].owner != owner) continue;
            if (count == INT64_MAX) return NULL;
            count++;
        }
    }
    for (
        PccGcStoreBufferNode *node = pcc_gc_backend4_store_buffer;
        node != NULL;
        node = node->next
    ) {
        if (node->owner != owner) continue;
        if (count == INT64_MAX) return NULL;
        count++;
    }
    if (count > INT64_MAX / (int64_t)sizeof(PyObject *)) return NULL;
    PccGcSourceSideTablePlan *plan = (
        PccGcSourceSideTablePlan *
    )calloc(1, sizeof(PccGcSourceSideTablePlan));
    if (plan == NULL) return NULL;
    if (count > 0) {
        plan->values = (PyObject **)calloc(
            (size_t)count, sizeof(PyObject *)
        );
        if (plan->values == NULL) {
            free(plan);
            return NULL;
        }
    }
    int64_t index = 0;
    for (
        PccGcStoreBufferMediumState *state =
            pcc_gc_backend4_store_buffer_medium_states;
        state != NULL;
        state = state->next
    ) {
        int32_t medium_count = state->count == NULL ? 0 : *state->count;
        for (int32_t i = 0; i < medium_count; i++) {
            if (state->entries[i].owner != owner) continue;
            if (index >= count) {
                free(plan->values);
                free(plan);
                return NULL;
            }
            plan->values[index++] = state->entries[i].value;
        }
    }
    for (
        PccGcStoreBufferNode *node = pcc_gc_backend4_store_buffer;
        node != NULL;
        node = node->next
    ) {
        if (node->owner != owner) continue;
        if (index >= count) {
            free(plan->values);
            free(plan);
            return NULL;
        }
        plan->values[index++] = node->value;
    }
    if (index != count) {
        free(plan->values);
        free(plan);
        return NULL;
    }
    plan->owner = owner;
    plan->count = count;
    return plan;
}

int64_t pcc_gc_backend4_source_side_table_plan_commit(void *opaque_plan) {
    PccGcSourceSideTablePlan *plan = (
        PccGcSourceSideTablePlan *
    )opaque_plan;
    if (
        plan == NULL
        || plan->committed != 0
        || plan->owner == NULL
        || PY_IS_TAGGED_INT(plan->owner)
        || plan->count < 0
        || (plan->count > 0 && plan->values == NULL)
    ) return 0;
    PyObject *owner = plan->owner;

    /* Re-verify the caller-held-lock snapshot before the first mutation. */
    int64_t index = 0;
    for (
        PccGcStoreBufferMediumState *state =
            pcc_gc_backend4_store_buffer_medium_states;
        state != NULL;
        state = state->next
    ) {
        int32_t medium_count = state->count == NULL ? 0 : *state->count;
        for (int32_t i = 0; i < medium_count; i++) {
            PccGcStoreBufferEntry *entry = &state->entries[i];
            if (entry->owner != owner) continue;
            if (index >= plan->count || plan->values[index] != entry->value) {
                return 0;
            }
            index++;
        }
    }
    for (
        PccGcStoreBufferNode *node = pcc_gc_backend4_store_buffer;
        node != NULL;
        node = node->next
    ) {
        if (node->owner != owner) continue;
        if (index >= plan->count || plan->values[index] != node->value) {
            return 0;
        }
        index++;
    }
    if (index != plan->count) return 0;

    /* Stable plan storage owns every removed value token.  Compact every
     * medium state and unlink the complete heap list with no decref. */
    int64_t removed = 0;
    for (
        PccGcStoreBufferMediumState *state =
            pcc_gc_backend4_store_buffer_medium_states;
        state != NULL;
        state = state->next
    ) {
        int32_t medium_count = state->count == NULL ? 0 : *state->count;
        int32_t write = 0;
        for (int32_t read = 0; read < medium_count; read++) {
            PccGcStoreBufferEntry entry = state->entries[read];
            if (entry.owner == owner) {
                pcc_gc_backend4_store_buffer_dec_unlocked();
                removed++;
                continue;
            }
            if (write != read) state->entries[write] = entry;
            write++;
        }
        for (int32_t i = write; i < medium_count; i++) {
            state->entries[i].owner = NULL;
            state->entries[i].slot = NULL;
            state->entries[i].value = NULL;
        }
        if (state->count != NULL) *state->count = write;
    }
    PccGcStoreBufferNode **link = &pcc_gc_backend4_store_buffer;
    while (*link != NULL) {
        PccGcStoreBufferNode *node = *link;
        if (node->owner != owner) {
            link = &node->next;
            continue;
        }
        *link = node->next;
        pcc_gc_backend4_store_buffer_dec_unlocked();
        free(node);
        removed++;
    }
    if (removed != plan->count) {
        /* Public ABI (py_runtime.h): an unlocked future caller must keep
         * immediate fatals, so route by lock ownership instead of DEFER. */
        PCC_GC_MIXED_TRIPWIRE(
            0,
            "source side-table commit detached count mismatch"
        );
        return 0;
    }
    pcc_gc_backend4_remembered_set_remove(owner);
    pcc_gc_backend4_zpage_remove_unlocked(owner);
    plan->committed = 1;
    return 1;
}

void pcc_gc_backend4_source_side_table_plan_finish(
    void *opaque_plan,
    PyObject *decref_exclusion
) {
    PccGcSourceSideTablePlan *plan = (
        PccGcSourceSideTablePlan *
    )opaque_plan;
    if (plan == NULL || plan->committed != 1) return;
    PyObject **values = plan->values;
    int64_t count = plan->count;
    plan->owner = NULL;
    plan->values = NULL;
    plan->count = 0;
    plan->committed = 2;
    for (int64_t i = 0; i < count; i++) {
        if (values[i] != decref_exclusion || decref_exclusion == NULL) {
            py_decref(values[i]);
        }
    }
    free(values);
    free(plan);
}

static void pcc_gc_backend4_store_buffer_clear(void) {
    for (
        PccGcStoreBufferMediumState *state =
            pcc_gc_backend4_store_buffer_medium_states;
        state != NULL;
        state = state->next
    ) {
        int32_t medium_count = state->count == NULL ? 0 : *state->count;
        for (int32_t i = 0; i < medium_count; i++) {
            py_decref(state->entries[i].value);
            state->entries[i].owner = NULL;
            state->entries[i].slot = NULL;
            state->entries[i].value = NULL;
        }
        if (state->count != NULL) *state->count = 0;
    }
    PccGcStoreBufferNode *n = pcc_gc_backend4_store_buffer;
    pcc_gc_backend4_store_buffer = NULL;
    while (n != NULL) {
        PccGcStoreBufferNode *next = n->next;
        if (pcc_gc_is_known_object(n->owner)) {
            py_header_flags_and(py_header(n->owner), ~PY_FLAG_GC_REMEMBERED);
        }
        py_decref(n->value);
        free(n);
        n = next;
    }
    __atomic_store_n(
        &pcc_gc_backend4_store_buffer_entries_count,
        0,
        __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_store_buffer_high_water_count, 0, __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_store_buffer_owner_fanout_high_water_count,
        0,
        __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_store_buffer_owner_count_high_water_count,
        0,
        __ATOMIC_RELAXED
    );
    pcc_gc_backend4_remembered_set_clear();
}

void pcc_gc_thread_unregister_buffers(void) {
    /* Drain every bounded per-thread GC cache before the backend4
     * store-buffer early return below. */
    pcc_gc_frame_node_tls_pool_drain();
    pcc_gc_ptr_index_tls_pool_drain();
    /* A departing thread may make one graph-free delivery attempt, but TLS
     * ownership ends here even when a full queue accepts only a prefix. */
    pcc_gc_cms_flush_wb_buffer();
    pcc_gc_cms_wb_discard_tls();
    PccGcStoreBufferMediumState *state =
        pcc_gc_backend4_store_buffer_medium_state;
    if (state == NULL) return;
    PccGcStoreBufferMediumState *detached = NULL;
    pcc_gc_graph_lock();
    pcc_gc_backend4_store_buffer_flush_medium_state_locked(state);
    PccGcStoreBufferMediumState **cur =
        &pcc_gc_backend4_store_buffer_medium_states;
    while (*cur != NULL) {
        if (*cur == state) {
            *cur = state->next;
            detached = state;
            break;
        }
        cur = &(*cur)->next;
    }
    pcc_gc_backend4_store_buffer_medium_state = NULL;
    pcc_gc_graph_unlock();
    free(detached);
}

void pcc_gc_reset_relocation_set(void) {
    pcc_gc_init_config();
    int64_t owner = pcc_current_thread_id();
    if (owner <= 0) return;
    for (;;) {
        pcc_gc_graph_lock();
        if (pcc_gc_backend4_relocation_reset_owner == 0) {
            pcc_gc_backend4_relocation_reset_owner = owner;
            break;
        }
        if (pcc_gc_backend4_relocation_reset_owner == owner) {
            pcc_gc_graph_unlock();
            return;
        }
        pcc_gc_graph_unlock();
        pcc_thread_safepoint();
    }

    for (;;) {
        PccGcRelocationNode *batch = NULL;
        int64_t examined = 0;
        while (
            pcc_gc_relocation_set != NULL
            && examined < PCC_GC_SAFEPOINT_BATCH
        ) {
            PccGcRelocationNode *node = pcc_gc_relocation_set;
            pcc_gc_relocation_set = node->next;
            if (pcc_gc_backend4_reseed_relocation_cursor == node) {
                pcc_gc_backend4_reseed_relocation_cursor = node->next;
            }
            pcc_gc_backend4_reseed_relocation_revision++;
            node->next = batch;
            batch = node;
            if (node->obj != NULL && !PY_IS_TAGGED_INT(node->obj)) {
                if (pcc_gc_forwarding_find(node->obj) == NULL) {
                    py_header_flags_and(
                        py_header(node->obj),
                        ~PY_FLAG_GC_RELOCATION_CANDIDATE
                    );
                }
            }
            examined++;
        }
        int complete = pcc_gc_relocation_set == NULL;
        pcc_gc_graph_unlock();
        pcc_gc_relocation_reset_finish(batch, NULL);
        if (complete) break;
        pcc_thread_safepoint();
        pcc_gc_graph_lock();
    }

    pcc_gc_graph_lock();
    for (;;) {
        PccGcZPageEvacuationNode *batch = NULL;
        int64_t examined = 0;
        while (
            pcc_gc_backend4_evacuation_pages != NULL
            && examined < PCC_GC_SAFEPOINT_BATCH
        ) {
            PccGcZPageEvacuationNode *node =
                pcc_gc_backend4_evacuation_pages;
            pcc_gc_backend4_evacuation_pages = node->next;
            if (pcc_gc_backend4_reseed_page_count_cursor == node) {
                pcc_gc_backend4_reseed_page_count_cursor = node->next;
            }
            pcc_gc_backend4_reseed_page_revision++;
            node->next = batch;
            batch = node;
            if (node->page != NULL) node->page->evacuation_selected = 0;
            examined++;
        }
        int complete = pcc_gc_backend4_evacuation_pages == NULL;
        pcc_gc_graph_unlock();
        pcc_gc_backend4_evacuation_page_finish_detached(batch);
        if (complete) break;
        pcc_thread_safepoint();
        pcc_gc_graph_lock();
    }

    pcc_gc_graph_lock();
    pcc_gc_backend4_reset_object_cursor = pcc_gc_objects;
    for (;;) {
        int64_t examined = 0;
        while (
            pcc_gc_backend4_reset_object_cursor != NULL
            && examined < PCC_GC_SAFEPOINT_BATCH
        ) {
            PccGcObjectNode *obj_node =
                pcc_gc_backend4_reset_object_cursor;
            pcc_gc_backend4_reset_object_cursor = obj_node->next;
            if (obj_node->obj != NULL && !PY_IS_TAGGED_INT(obj_node->obj)) {
                py_header_flags_and(
                    py_header(obj_node->obj), ~PY_FLAG_GC_RELOCATION_TARGET
                );
            }
            examined++;
        }
        if (pcc_gc_backend4_reset_object_cursor == NULL) {
            __atomic_store_n(
                &pcc_gc_backend4_evacuation_candidates,
                0,
                __ATOMIC_RELAXED
            );
            __atomic_store_n(
                &pcc_gc_backend4_evacuation_candidate_bytes_count,
                0,
                __ATOMIC_RELAXED
            );
            __atomic_store_n(
                &pcc_gc_backend4_small_page_candidates,
                0,
                __ATOMIC_RELAXED
            );
            __atomic_store_n(
                &pcc_gc_backend4_medium_page_candidates,
                0,
                __ATOMIC_RELAXED
            );
            __atomic_store_n(
                &pcc_gc_backend4_small_page_candidate_bytes_count,
                0,
                __ATOMIC_RELAXED
            );
            __atomic_store_n(
                &pcc_gc_backend4_medium_page_candidate_bytes_count,
                0,
                __ATOMIC_RELAXED
            );
            __atomic_store_n(
                &pcc_gc_backend4_evacuation_candidate_zpage_bytes_count,
                0,
                __ATOMIC_RELAXED
            );
            __atomic_store_n(
                &pcc_gc_backend4_small_page_candidate_zpage_bytes_count,
                0,
                __ATOMIC_RELAXED
            );
            __atomic_store_n(
                &pcc_gc_backend4_medium_page_candidate_zpage_bytes_count,
                0,
                __ATOMIC_RELAXED
            );
            pcc_gc_backend4_relocation_reset_owner = 0;
            pcc_gc_graph_unlock();
            return;
        }
        pcc_gc_graph_unlock();
        pcc_thread_safepoint();
        pcc_gc_graph_lock();
    }
}

static void pcc_gc_relocation_reset_finish(
    PccGcRelocationNode *relocation_nodes,
    PccGcZPageEvacuationNode *evacuation_nodes
) {
    while (relocation_nodes != NULL) {
        PccGcRelocationNode *next = relocation_nodes->next;
        free(relocation_nodes);
        relocation_nodes = next;
    }
    pcc_gc_backend4_evacuation_page_finish_detached(evacuation_nodes);
}

int64_t pcc_gc_relocation_set_contains(PyObject *o) {
    pcc_gc_init_config();
    pcc_gc_graph_lock();
    int64_t contains = pcc_gc_relocation_set_find(o) != NULL ? 1 : 0;
    pcc_gc_graph_unlock();
    return contains;
}

int64_t pcc_gc_relocation_set_size(void) {
    pcc_gc_init_config();
    pcc_gc_graph_lock();
    int64_t size = 0;
    for (
        PccGcRelocationNode *n = pcc_gc_relocation_set;
        n != NULL;
        n = n->next
    ) {
        size++;
    }
    pcc_gc_graph_unlock();
    return size;
}

static int64_t pcc_gc_backend4_forwarding_entries_unlocked(void) {
    int64_t count = 0;
    for (
        PccGcForwardNode *n = pcc_gc_forwardings;
        n != NULL;
        n = n->next
    ) {
        count++;
    }
    return count;
}

static int64_t pcc_gc_backend4_stable_id_entries_unlocked(void) {
    int64_t count = 0;
    for (
        PccGcIdentityNode *n = pcc_gc_identities;
        n != NULL;
        n = n->next
    ) {
        count++;
    }
    return count;
}

int64_t pcc_gc_backend4_verify_no_old_addresses(void) {
    pcc_gc_init_config();
    if (pcc_gc_selected_backend != PCC_GC_KIND_COLORED_RELOCATING) {
        return 1;
    }
    pcc_gc_graph_lock();
    for (
        PccGcForwardNode *n = pcc_gc_forwardings;
        n != NULL;
        n = n->next
    ) {
        PyObject *from = n->from;
        PyObject *to = n->to;
        if (from == NULL || to == NULL) {
            pcc_gc_graph_unlock();
            return 0;
        }
        if (from == to) {
            pcc_gc_graph_unlock();
            return 0;
        }
        if ((py_header_flags_load(py_header(to)) & PY_FLAG_GC_OLD) != 0) {
            pcc_gc_graph_unlock();
            return 0;
        }
    }
    pcc_gc_graph_unlock();
    return 1;
}

int64_t pcc_gc_backend4_forwarding_entries(void) {
    pcc_gc_init_config();
    pcc_gc_graph_lock();
    int64_t count = pcc_gc_backend4_forwarding_entries_unlocked();
    pcc_gc_graph_unlock();
    return count;
}

int64_t pcc_gc_backend4_stable_id_entries(void) {
    pcc_gc_init_config();
    pcc_gc_graph_lock();
    int64_t count = pcc_gc_backend4_stable_id_entries_unlocked();
    pcc_gc_graph_unlock();
    return count;
}

int64_t pcc_gc_backend4_fragmentation_score(void) {
    return pcc_gc_relocation_set_size()
        + pcc_gc_backend4_forwarding_entries();
}

int64_t pcc_gc_backend4_generation_barrier_score(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_genzgc_store_barriers,
        __ATOMIC_RELAXED
    );
}

int64_t pcc_gc_backend4_store_buffer_entries(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_store_buffer_entries_count,
        __ATOMIC_RELAXED
    );
}

int64_t pcc_gc_backend4_generation_promotion_score(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_young_promotions,
        __ATOMIC_RELAXED
    );
}

int64_t pcc_gc_backend4_evacuation_candidate_score(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_evacuation_candidates,
        __ATOMIC_RELAXED
    );
}

int64_t pcc_gc_backend4_evacuated_bytes(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_evacuated_bytes_count,
        __ATOMIC_RELAXED
    );
}

int64_t pcc_gc_backend4_page_policy_score(void) {
    return pcc_gc_backend4_evacuation_candidate_score()
        + pcc_gc_backend4_evacuated_bytes();
}

int64_t pcc_gc_backend4_page_pressure_score(void) {
    return pcc_gc_backend4_evacuation_candidate_bytes()
        + pcc_gc_backend4_large_object_deferred_bytes();
}

int64_t pcc_gc_backend4_fragmentation_backlog_bytes(void) {
    int64_t candidates = pcc_gc_backend4_evacuation_candidate_bytes();
    int64_t evacuated = pcc_gc_backend4_evacuated_bytes();
    int64_t deferred = pcc_gc_backend4_large_object_deferred_bytes();
    int64_t pending = candidates > evacuated ? candidates - evacuated : 0;
    return pending + deferred;
}

int64_t pcc_gc_backend4_evacuation_efficiency_per_mille(void) {
    int64_t candidates = pcc_gc_backend4_evacuation_candidate_bytes();
    if (candidates <= 0) return 1000;
    int64_t evacuated = pcc_gc_backend4_evacuated_bytes();
    if (evacuated <= 0) return 0;
    if (evacuated >= candidates) return 1000;
    return (evacuated * 1000) / candidates;
}

int64_t pcc_gc_backend4_fragmentation_policy_score(void) {
    return pcc_gc_backend4_fragmentation_backlog_bytes()
        + pcc_gc_backend4_evacuation_incomplete_batches();
}

int64_t pcc_gc_backend4_small_page_limit_bytes(void) {
    return PCC_GC_BACKEND4_SMALL_PAGE_LIMIT;
}

int64_t pcc_gc_backend4_medium_page_limit_bytes(void) {
    return PCC_GC_BACKEND4_MEDIUM_PAGE_LIMIT;
}

int64_t pcc_gc_backend4_large_defer_limit_bytes(void) {
    return PCC_GC_BACKEND4_MEDIUM_PAGE_LIMIT;
}

int64_t pcc_gc_backend4_large_object_defer_score(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_large_object_defers,
        __ATOMIC_RELAXED
    );
}

int64_t pcc_gc_backend4_large_object_deferred_bytes(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_large_object_deferred_bytes_count,
        __ATOMIC_RELAXED
    );
}

int64_t pcc_gc_backend4_large_object_reconsiderations(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_large_object_reconsiderations_count,
        __ATOMIC_RELAXED
    );
}

static int64_t pcc_gc_backend4_generation_count(int32_t flag) {
    int64_t count = 0;
    pcc_gc_graph_lock();
    for (PccGcObjectNode *n = pcc_gc_objects; n != NULL; n = n->next) {
        if (!pcc_gc_object_node_is_active(n)) continue;
        PyObject *o = n->obj;
        if (o == NULL || PY_IS_TAGGED_INT(o)) continue;
        if ((py_header_flags_load(py_header(o)) & flag) != 0) count++;
    }
    pcc_gc_graph_unlock();
    return count;
}

static int64_t pcc_gc_backend4_generation_bytes(int32_t flag) {
    int64_t bytes = 0;
    pcc_gc_graph_lock();
    for (PccGcObjectNode *n = pcc_gc_objects; n != NULL; n = n->next) {
        if (!pcc_gc_object_node_is_active(n)) continue;
        PyObject *o = n->obj;
        if (o == NULL || PY_IS_TAGGED_INT(o)) continue;
        if ((py_header_flags_load(py_header(o)) & flag) != 0) bytes += n->size;
    }
    pcc_gc_graph_unlock();
    return bytes;
}

int64_t pcc_gc_backend4_candidate_fresh_skips_count(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_candidate_fresh_skips, __ATOMIC_RELAXED
    );
}

int64_t pcc_gc_backend4_relocation_add_refusals_count(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_relocation_add_refusals, __ATOMIC_RELAXED
    );
}

int64_t pcc_gc_backend4_young_object_count(void) {
    return pcc_gc_backend4_generation_count(PY_FLAG_GC_YOUNG);
}

int64_t pcc_gc_backend4_old_object_count(void) {
    return pcc_gc_backend4_generation_count(PY_FLAG_GC_OLD);
}

int64_t pcc_gc_backend4_young_bytes(void) {
    return pcc_gc_backend4_generation_bytes(PY_FLAG_GC_YOUNG);
}

int64_t pcc_gc_backend4_old_bytes(void) {
    return pcc_gc_backend4_generation_bytes(PY_FLAG_GC_OLD);
}

static int32_t pcc_gc_backend4_page_class_for_size(int64_t size) {
    if (size <= PCC_GC_BACKEND4_SMALL_PAGE_LIMIT) return 0;
    if (size <= PCC_GC_BACKEND4_MEDIUM_PAGE_LIMIT) return 1;
    return 2;
}

static int64_t pcc_gc_backend4_align_alloc_size(int64_t size) {
    if (size <= 0) return 0;
    return (size + 7) & ~(int64_t)7;
}

static int32_t pcc_gc_backend4_generation_for_flags(int32_t flags) {
    return (flags & PY_FLAG_GC_OLD) != 0 ? 2 : 1;
}

static int32_t pcc_gc_backend4_generation_for_owner(PyObject *owner) {
    if (
        owner != NULL
        && !PY_IS_TAGGED_INT(owner)
        && (py_header_flags_load(py_header(owner)) & PY_FLAG_GC_OLD) != 0
    ) {
        return 2;
    }
    return 1;
}

static PccGcZPage *pcc_gc_backend4_active_page_unlocked(
    int32_t page_class,
    int32_t generation
) {
    if (page_class < 0 || page_class > 2) return NULL;
    if (generation < 0 || generation > 2) return NULL;
    return pcc_gc_backend4_active_pages[page_class][generation];
}

static void pcc_gc_backend4_active_page_set_unlocked(PccGcZPage *page) {
    if (page == NULL) return;
    if (page->page_class < 0 || page->page_class > 2) return;
    if (page->generation < 0 || page->generation > 2) return;
    pcc_gc_backend4_active_pages[page->page_class][page->generation] = page;
}

static void pcc_gc_backend4_active_page_clear_unlocked(PccGcZPage *page) {
    if (page == NULL) return;
    for (int32_t page_class = 0; page_class < 3; page_class++) {
        for (int32_t generation = 0; generation < 3; generation++) {
            if (pcc_gc_backend4_active_pages[page_class][generation] == page) {
                pcc_gc_backend4_active_pages[page_class][generation] = NULL;
            }
        }
    }
}

static int pcc_gc_backend4_zpage_accepts_alloc_unlocked(
    PccGcZPage *page,
    int32_t page_class,
    int32_t generation,
    int64_t alloc_size
) {
    if (page == NULL) return 0;
    if (
        pcc_gc_backend4_evacuation_pages != NULL
        && pcc_gc_backend4_evacuation_page_find_unlocked(page) != NULL
    ) {
        return 0;
    }
    if (page->page_class != page_class) return 0;
    if (page->generation != generation) return 0;
    return page->capacity_bytes - page->allocated_bytes >= alloc_size;
}

static int64_t pcc_gc_backend4_page_class_population(
    int32_t page_class,
    int32_t count_bytes
) {
    int64_t total = 0;
    pcc_gc_graph_lock();
    for (PccGcObjectNode *n = pcc_gc_objects; n != NULL; n = n->next) {
        if (!pcc_gc_object_node_is_active(n)) continue;
        PyObject *o = n->obj;
        if (o == NULL || PY_IS_TAGGED_INT(o)) continue;
        if (pcc_gc_backend4_page_class_for_size(n->size) != page_class) {
            continue;
        }
        total += count_bytes != 0 ? n->size : 1;
    }
    pcc_gc_graph_unlock();
    return total;
}

int64_t pcc_gc_backend4_small_page_object_count(void) {
    return pcc_gc_backend4_page_class_population(0, 0);
}

int64_t pcc_gc_backend4_medium_page_object_count(void) {
    return pcc_gc_backend4_page_class_population(1, 0);
}

int64_t pcc_gc_backend4_large_page_object_count(void) {
    return pcc_gc_backend4_page_class_population(2, 0);
}

int64_t pcc_gc_backend4_small_page_live_bytes(void) {
    return pcc_gc_backend4_page_class_population(0, 1);
}

int64_t pcc_gc_backend4_medium_page_live_bytes(void) {
    return pcc_gc_backend4_page_class_population(1, 1);
}

int64_t pcc_gc_backend4_large_page_live_bytes(void) {
    return pcc_gc_backend4_page_class_population(2, 1);
}

static int64_t pcc_gc_backend4_zpage_capacity_for_size(int64_t size) {
    if (size <= PCC_GC_BACKEND4_SMALL_PAGE_LIMIT) {
        return PCC_GC_BACKEND4_SMALL_PAGE_LIMIT;
    }
    if (size <= PCC_GC_BACKEND4_MEDIUM_PAGE_LIMIT) {
        return PCC_GC_BACKEND4_MEDIUM_PAGE_LIMIT;
    }
    int64_t unit = PCC_GC_BACKEND4_MEDIUM_PAGE_LIMIT;
    int64_t pages = (size + unit - 1) / unit;
    if (pages < 1) pages = 1;
    return pages * unit;
}

static PccGcZPage *pcc_gc_backend4_zpage_find_reusable_page_unlocked(
    PyObject *owner,
    int64_t size
) {
    if (size <= 0 || size > PCC_GC_BACKEND4_MEDIUM_PAGE_LIMIT) {
        return NULL;
    }
    int32_t page_class = pcc_gc_backend4_page_class_for_size(size);
    int32_t generation = pcc_gc_backend4_generation_for_owner(owner);
    int64_t alloc_size = pcc_gc_backend4_align_alloc_size(size);
    PccGcZPage *active = pcc_gc_backend4_active_page_unlocked(
        page_class,
        generation
    );
    if (
        pcc_gc_backend4_zpage_accepts_alloc_unlocked(
            active,
            page_class,
            generation,
            alloc_size
        )
    ) {
        return active;
    }
    pcc_gc_backend4_active_page_clear_unlocked(active);
    return NULL;
}

static PccGcZPage *pcc_gc_backend4_zpage_find_reusable_page_for_gen_unlocked(
    int64_t size,
    int32_t generation
) {
    if (size <= 0 || size > PCC_GC_BACKEND4_MEDIUM_PAGE_LIMIT) {
        return NULL;
    }
    int32_t page_class = pcc_gc_backend4_page_class_for_size(size);
    int64_t alloc_size = pcc_gc_backend4_align_alloc_size(size);
    PccGcZPage *active = pcc_gc_backend4_active_page_unlocked(
        page_class,
        generation
    );
    if (
        pcc_gc_backend4_zpage_accepts_alloc_unlocked(
            active,
            page_class,
            generation,
            alloc_size
        )
    ) {
        return active;
    }
    pcc_gc_backend4_active_page_clear_unlocked(active);
    return NULL;
}

static PccGcZPage *pcc_gc_backend4_zpage_pop_free_page_unlocked(
    int64_t size
) {
    if (size <= 0 || size > PCC_GC_BACKEND4_MEDIUM_PAGE_LIMIT) {
        return NULL;
    }
    int32_t page_class = pcc_gc_backend4_page_class_for_size(size);
    int64_t capacity = pcc_gc_backend4_zpage_capacity_for_size(size);
    PccGcZPage **cur = &pcc_gc_backend4_free_pages;
    while (*cur != NULL) {
        PccGcZPage *page = *cur;
        if (
            page->page_class == page_class
            && page->capacity_bytes == capacity
        ) {
            *cur = page->next;
            page->next = NULL;
            return page;
        }
        cur = &(*cur)->next;
    }
    return NULL;
}

static void pcc_gc_backend4_zpage_reset_unlocked(
    PccGcZPage *page,
    PyObject *owner,
    int64_t size
) {
    if (page == NULL) return;
    page->primary_owner = owner;
    page->used_bytes = 0;
    page->allocated_bytes = 0;
    page->capacity_bytes = pcc_gc_backend4_zpage_capacity_for_size(size);
    page->page_class = pcc_gc_backend4_page_class_for_size(size);
    page->generation = pcc_gc_backend4_generation_for_owner(owner);
    page->object_count = 0;
    page->remembered_slots = 0;
    page->remembered_cards = 0;
    page->remembered_card_bitmap = 0;
    page->pending_alloc_count = 0;
    page->pending_forwardings = 0;
    page->zombie = 0;
    page->evacuation_selected = 0;
    memset(
        page->remembered_card_refcounts,
        0,
        sizeof(page->remembered_card_refcounts)
    );
    if (
        page->span_base == NULL
        || page->span_capacity_bytes < page->capacity_bytes
    ) {
        page->span_base = (uint8_t *)calloc(
            1,
            (size_t)(
                page->capacity_bytes + PCC_GC_BACKEND4_ZPAGE_SPAN_GUARD_BYTES
            )
        );
        page->span_capacity_bytes =
            page->span_base == NULL ? 0 : page->capacity_bytes;
    } else if (page->capacity_bytes > 0) {
        memset(page->span_base, 0, (size_t)page->capacity_bytes);
    }
}

static PccGcZPage *pcc_gc_backend4_zpage_find_page_for_addr_unlocked(
    void *ptr,
    int64_t size,
    int64_t *offset_out
) {
    if (ptr == NULL || size <= 0) return NULL;
    uintptr_t addr = (uintptr_t)ptr;
    int64_t alloc_size = pcc_gc_backend4_align_alloc_size(size);
    int32_t page_class = pcc_gc_backend4_page_class_for_size(size);
    if (page_class < 2) {
        for (int32_t generation = 1; generation <= 2; generation++) {
            PccGcZPage *active = pcc_gc_backend4_active_page_unlocked(
                page_class,
                generation
            );
            if (active == NULL || active->span_base == NULL) continue;
            if (active->span_capacity_bytes <= 0) continue;
            uintptr_t base = (uintptr_t)active->span_base;
            uintptr_t span = (uintptr_t)active->span_capacity_bytes;
            if (addr >= base && addr - base + (uintptr_t)alloc_size <= span) {
                if (offset_out != NULL) *offset_out = (int64_t)(addr - base);
                return active;
            }
        }
    }
    for (
        PccGcZPage *page = pcc_gc_backend4_pages;
        page != NULL;
        page = page->next
    ) {
        if (page->span_base == NULL || page->span_capacity_bytes <= 0) {
            continue;
        }
        uintptr_t base = (uintptr_t)page->span_base;
        uintptr_t span = (uintptr_t)page->span_capacity_bytes;
        if (addr >= base && addr - base + (uintptr_t)alloc_size <= span) {
            if (offset_out != NULL) *offset_out = (int64_t)(addr - base);
            return page;
        }
    }
    return NULL;
}

static int32_t pcc_gc_backend4_zpage_list_owns_addr_unlocked(
    PccGcZPage *head,
    void *ptr
) {
    if (ptr == NULL) return 0;
    uintptr_t addr = (uintptr_t)ptr;
    for (PccGcZPage *page = head; page != NULL; page = page->next) {
        if (page->span_base == NULL || page->span_capacity_bytes <= 0) {
            continue;
        }
        uintptr_t base = (uintptr_t)page->span_base;
        uintptr_t span = (uintptr_t)page->span_capacity_bytes;
        if (addr >= base && addr < base + span) return 1;
    }
    return 0;
}

static int32_t pcc_gc_backend4_zpage_owns_addr_unlocked(void *ptr) {
    if (pcc_gc_selected_backend != PCC_GC_KIND_COLORED_RELOCATING) return 0;
    if (pcc_gc_backend4_zpage_list_owns_addr_unlocked(
            pcc_gc_backend4_pages,
            ptr
        )) {
        return 1;
    }
    if (pcc_gc_backend4_zpage_list_owns_addr_unlocked(
        pcc_gc_backend4_free_pages,
        ptr
    )) {
        return 1;
    }
    return pcc_gc_backend4_zpage_list_owns_addr_unlocked(
        pcc_gc_backend4_retained_pages,
        ptr
    );
}

void *pcc_gc_backend4_try_zpage_alloc(int64_t size, int32_t flags) {
    pcc_gc_init_config();
    if (pcc_gc_selected_backend != PCC_GC_KIND_COLORED_RELOCATING) {
        return NULL;
    }
    if (size < (int64_t)sizeof(PyObjectHeader)) return NULL;
    int64_t alloc_size = pcc_gc_backend4_align_alloc_size(size);
    if (alloc_size <= 0) return NULL;
    int32_t generation = pcc_gc_backend4_generation_for_flags(flags);
    int32_t page_class = pcc_gc_backend4_page_class_for_size(size);
    /* A page removed from the free list is private until it is published.
     * Reset/allocation can therefore run after graph unlock.  The retry then
     * either publishes that complete page or restores/discards it when a
     * competing allocator installed an active page first. */
    PccGcZPage *prepared_page = NULL;
    int32_t prepared_from_free = 0;
    for (;;) {
        PccGcZPage *unused_fresh_page = NULL;
        pcc_gc_graph_lock();
        if (
            pcc_gc_selected_backend
            != PCC_GC_KIND_COLORED_RELOCATING
        ) {
            pcc_gc_graph_unlock();
            if (prepared_page != NULL) {
                free(prepared_page->span_base);
                free(prepared_page);
            }
            return NULL;
        }
        PccGcZPage *page = NULL;
        PccGcZPage *active = pcc_gc_backend4_active_page_unlocked(
            page_class,
            generation
        );
        if (
            pcc_gc_backend4_zpage_accepts_alloc_unlocked(
                active,
                page_class,
                generation,
                alloc_size
            )
        ) {
            page = active;
        } else {
            pcc_gc_backend4_active_page_clear_unlocked(active);
        }
        if (page == NULL) {
            page = pcc_gc_backend4_zpage_find_reusable_page_for_gen_unlocked(
                size,
                generation
            );
        }
        if (page != NULL && prepared_page != NULL) {
            if (prepared_from_free != 0) {
                prepared_page->next = pcc_gc_backend4_free_pages;
                pcc_gc_backend4_free_pages = prepared_page;
            } else {
                unused_fresh_page = prepared_page;
            }
            prepared_page = NULL;
            prepared_from_free = 0;
        }
        if (page == NULL && prepared_page != NULL) {
            if (
                prepared_page->span_base == NULL
                || prepared_page->span_capacity_bytes
                    < prepared_page->capacity_bytes
            ) {
                PccGcZPage *failed_page = prepared_page;
                prepared_page = NULL;
                pcc_gc_graph_unlock();
                free(failed_page->span_base);
                free(failed_page);
                return NULL;
            }
            page = prepared_page;
            prepared_page = NULL;
            prepared_from_free = 0;
            page->next = pcc_gc_backend4_pages;
            pcc_gc_backend4_pages = page;
        }
        if (page != NULL) {
            if (
                page->span_base == NULL
                || page->span_capacity_bytes < page->capacity_bytes
                || page->allocated_bytes < 0
                || page->capacity_bytes - page->allocated_bytes < alloc_size
            ) {
                pcc_gc_graph_unlock();
                if (unused_fresh_page != NULL) {
                    free(unused_fresh_page->span_base);
                    free(unused_fresh_page);
                }
                return NULL;
            }
            uint8_t *ptr = page->span_base + page->allocated_bytes;
            /* Reserve before unlock.  pending_alloc_count keeps collectors
             * from selecting or recycling the page while the private range
             * is cleared and its object header is not yet published. */
            page->allocated_bytes += alloc_size;
            page->pending_alloc_count++;
            pcc_gc_backend4_active_page_set_unlocked(page);
            pcc_gc_graph_unlock();
            if (unused_fresh_page != NULL) {
                free(unused_fresh_page->span_base);
                free(unused_fresh_page);
            }
            memset(ptr, 0, (size_t)alloc_size);
            return ptr;
        }

        page = pcc_gc_backend4_zpage_pop_free_page_unlocked(size);
        if (page != NULL) {
            pcc_gc_graph_unlock();
            pcc_gc_backend4_zpage_reset_unlocked(page, NULL, size);
            page->generation = generation;
            if (
                page->span_base == NULL
                || page->span_capacity_bytes < page->capacity_bytes
            ) {
                free(page->span_base);
                free(page);
                return NULL;
            }
            prepared_page = page;
            prepared_from_free = 1;
            continue;
        }
        pcc_gc_graph_unlock();

        page = (PccGcZPage *)calloc(1, sizeof(PccGcZPage));
        if (page == NULL) return NULL;
        pcc_gc_backend4_zpage_reset_unlocked(page, NULL, size);
        page->generation = generation;
        if (
            page->span_base == NULL
            || page->span_capacity_bytes < page->capacity_bytes
        ) {
            free(page->span_base);
            free(page);
            return NULL;
        }
        prepared_page = page;
        prepared_from_free = 0;
    }
}

static int64_t pcc_gc_backend4_free_page_count_for_class_unlocked(
    int32_t page_class
) {
    int64_t count = 0;
    for (
        PccGcZPage *page = pcc_gc_backend4_free_pages;
        page != NULL;
        page = page->next
    ) {
        if (page->page_class == page_class) count++;
    }
    return count;
}

static int64_t pcc_gc_backend4_free_page_limit_for_class(int32_t page_class) {
    if (page_class == 0) return PCC_GC_BACKEND4_FREE_SMALL_PAGE_LIMIT;
    if (page_class == 1) return PCC_GC_BACKEND4_FREE_MEDIUM_PAGE_LIMIT;
    return 0;
}

static void pcc_gc_backend4_zpage_clear_reusable_state_unlocked(
    PccGcZPage *page
) {
    if (page == NULL) return;
    page->primary_owner = NULL;
    page->used_bytes = 0;
    page->allocated_bytes = 0;
    page->object_count = 0;
    page->pending_alloc_count = 0;
    page->pending_forwardings = 0;
    page->zombie = 0;
    page->remembered_slots = 0;
    page->remembered_cards = 0;
    page->remembered_card_bitmap = 0;
    page->object_head = NULL;
    memset(
        page->remembered_card_refcounts,
        0,
        sizeof(page->remembered_card_refcounts)
    );
}

static void pcc_gc_backend4_zpage_cache_unlocked(PccGcZPage *page) {
    if (page == NULL) return;
    pcc_gc_backend4_active_page_clear_unlocked(page);
    pcc_gc_backend4_zpage_clear_reusable_state_unlocked(page);
    page->next = pcc_gc_backend4_free_pages;
    pcc_gc_backend4_free_pages = page;
}

static void pcc_gc_backend4_zpage_destroy_unlocked(PccGcZPage *page) {
    if (page == NULL) return;
    pcc_gc_backend4_active_page_clear_unlocked(page);
    /* Correctness-first two-epoch quarantine: old SSA values, delayed
     * trashcan entries, and stale borrowed pointers can outlive owner-index
     * membership. Remove the page from reusable caches now; the forwarding
     * retirement pass releases this retained generation only after another
     * complete remap/root-healing epoch. */
    pcc_gc_backend4_zpage_clear_reusable_state_unlocked(page);
    page->next = pcc_gc_backend4_retained_pages;
    pcc_gc_backend4_retained_pages = page;
}

static void pcc_gc_backend4_zpage_recycle_unlocked(PccGcZPage *page) {
    if (page == NULL) return;
    pcc_gc_backend4_active_page_clear_unlocked(page);
    if (page->page_class > 1) {
        pcc_gc_backend4_zpage_destroy_unlocked(page);
        return;
    }
    int64_t limit = pcc_gc_backend4_free_page_limit_for_class(
        page->page_class
    );
    if (
        limit <= 0
        || pcc_gc_backend4_free_page_count_for_class_unlocked(
            page->page_class
        ) >= limit
    ) {
        pcc_gc_backend4_zpage_destroy_unlocked(page);
        return;
    }
    pcc_gc_backend4_zpage_cache_unlocked(page);
}

static PccGcZPageNode *pcc_gc_backend4_zpage_page_head_unlocked(
    PccGcZPage *page
) {
    return page != NULL ? page->object_head : NULL;
}

static void pcc_gc_backend4_zpage_set_page_head_unlocked(
    PccGcZPage *page,
    PccGcZPageNode *head
) {
    if (page == NULL) return;
    page->object_head = head;
    if (head != NULL) {
        page->primary_owner = head->owner;
    } else {
        page->primary_owner = NULL;
    }
}

static void pcc_gc_backend4_zpage_link_node_unlocked(PccGcZPageNode *node) {
    if (node == NULL) return;
    node->prev = NULL;
    node->next = pcc_gc_backend4_zpages;
    if (node->next != NULL) node->next->prev = node;
    pcc_gc_backend4_zpages = node;
    (void)pcc_gc_zpage_owner_index_upsert(node->owner, node);

    node->page_prev = NULL;
    node->page_next = pcc_gc_backend4_zpage_page_head_unlocked(node->page);
    if (node->page_next != NULL) node->page_next->page_prev = node;
    pcc_gc_backend4_zpage_set_page_head_unlocked(node->page, node);
}

int64_t pcc_gc_backend4_zpage_link_node_preallocated(void *raw_node) {
    PccGcZPageNode *node = (PccGcZPageNode *)raw_node;
    if (node == NULL) return -1;
    if (
        pcc_gc_zpage_owner_index_upsert_preallocated(
            node->owner, node
        ) < 0
    ) {
        return -1;
    }
    node->prev = NULL;
    node->next = pcc_gc_backend4_zpages;
    if (node->next != NULL) node->next->prev = node;
    pcc_gc_backend4_zpages = node;
    node->page_prev = NULL;
    node->page_next = pcc_gc_backend4_zpage_page_head_unlocked(node->page);
    if (node->page_next != NULL) node->page_next->page_prev = node;
    pcc_gc_backend4_zpage_set_page_head_unlocked(node->page, node);
    return 1;
}

static void pcc_gc_backend4_zpage_unlink_node_unlocked(PccGcZPageNode *node) {
    if (node == NULL) return;
    if (pcc_gc_backend4_selector_scan_cursor == node) {
        pcc_gc_backend4_selector_scan_cursor = node->next;
    }
    if (pcc_gc_backend4_selector_scan_best == node) {
        pcc_gc_backend4_selector_scan_best = NULL;
        pcc_gc_backend4_selector_scan_best_score = -1;
        pcc_gc_backend4_selector_scan_restart = 1;
    }
    if (pcc_gc_backend4_selector_page_cursor == node) {
        pcc_gc_backend4_selector_page_cursor = node->page_next;
    }
    if (pcc_gc_backend4_selector_page_seed == node) {
        pcc_gc_backend4_selector_page_seed = NULL;
        pcc_gc_backend4_selector_page_seed_pending = 0;
    }
    (void)pcc_gc_zpage_owner_index_remove(node->owner);
    if (node->prev != NULL) {
        node->prev->next = node->next;
    } else if (pcc_gc_backend4_zpages == node) {
        pcc_gc_backend4_zpages = node->next;
    }
    if (node->next != NULL) node->next->prev = node->prev;

    PccGcZPage *page = node->page;
    if (page != NULL) {
        if (node->page_prev != NULL) {
            node->page_prev->page_next = node->page_next;
        } else {
            pcc_gc_backend4_zpage_set_page_head_unlocked(page, node->page_next);
        }
        if (node->page_next != NULL) {
            node->page_next->page_prev = node->page_prev;
        }
        if (node->page_prev != NULL) {
            PccGcZPageNode *head =
                pcc_gc_backend4_zpage_page_head_unlocked(page);
            page->primary_owner = head != NULL ? head->owner : NULL;
        }
    }
    node->next = NULL;
    node->prev = NULL;
    node->page_next = NULL;
    node->page_prev = NULL;
}

static PccGcZPageNode *pcc_gc_backend4_zpage_track_alloc_unlocked(
    PyObject *owner,
    int64_t size
) {
    if (owner == NULL || PY_IS_TAGGED_INT(owner)) return NULL;
    if (pcc_gc_selected_backend != PCC_GC_KIND_COLORED_RELOCATING) return NULL;
    PccGcZPageNode *page = pcc_gc_backend4_zpage_node_alloc_unlocked();
    if (page == NULL) return NULL;
    PccGcZPage *zpage = NULL;
    int64_t existing_offset = -1;
    if ((py_header_flags_load(py_header(owner)) & PY_FLAG_GC_ZPAGE_ALLOC) != 0) {
        zpage = pcc_gc_backend4_zpage_find_page_for_addr_unlocked(
            owner,
            size,
            &existing_offset
        );
    }
    if (zpage == NULL) {
        zpage = pcc_gc_backend4_zpage_find_reusable_page_unlocked(
            owner,
            size
        );
    }
    if (zpage == NULL) {
        zpage = pcc_gc_backend4_zpage_pop_free_page_unlocked(size);
    }
    if (zpage == NULL) {
        zpage = (PccGcZPage *)calloc(1, sizeof(PccGcZPage));
        if (zpage == NULL) {
            pcc_gc_backend4_zpage_node_release_unlocked(page);
            return NULL;
        }
    }
    if (existing_offset < 0 && zpage->object_count <= 0) {
        pcc_gc_backend4_zpage_reset_unlocked(zpage, owner, size);
        zpage->next = pcc_gc_backend4_pages;
        pcc_gc_backend4_pages = zpage;
    }
    page->owner = owner;
    page->page = zpage;
    page->offset_bytes =
        existing_offset >= 0 ? existing_offset : zpage->allocated_bytes;
    page->size_bytes = size;
    page->payload_spans = NULL;
    page->remembered_slots = 0;
    if (existing_offset >= 0 && zpage->pending_alloc_count > 0) {
        zpage->pending_alloc_count--;
    }
    if (existing_offset < 0) {
        zpage->allocated_bytes += pcc_gc_backend4_align_alloc_size(size);
    }
    zpage->used_bytes += size;
    zpage->object_count++;
    pcc_gc_backend4_active_page_set_unlocked(zpage);
    pcc_gc_backend4_zpage_link_node_unlocked(page);
    return page;
}

void *pcc_gc_backend4_zpage_track_page_prepare(
    void *raw_page,
    PyObject *owner,
    int64_t size
) {
    PccGcZPage *page = (PccGcZPage *)raw_page;
    if (size <= 0) return NULL;
    if (page == NULL) {
        page = (PccGcZPage *)calloc(1, sizeof(PccGcZPage));
        if (page == NULL) return NULL;
    }
    pcc_gc_backend4_zpage_reset_unlocked(page, owner, size);
    if (
        page->span_base == NULL
        || page->span_capacity_bytes < page->capacity_bytes
    ) {
        free(page->span_base);
        free(page);
        return NULL;
    }
    return page;
}

static void pcc_gc_backend4_zpage_track_restore_prepared_unlocked(
    PccGcZPage **prepared_page_io,
    int32_t *prepared_from_free_io
) {
    if (prepared_page_io == NULL || prepared_from_free_io == NULL) return;
    if (*prepared_page_io != NULL && *prepared_from_free_io != 0) {
        (*prepared_page_io)->next = pcc_gc_backend4_free_pages;
        pcc_gc_backend4_free_pages = *prepared_page_io;
        *prepared_page_io = NULL;
    }
    *prepared_from_free_io = 0;
}

static void pcc_gc_backend4_zpage_track_finish_prepared(
    PccGcZPage *prepared_page
) {
    if (prepared_page == NULL) return;
    free(prepared_page->span_base);
    free(prepared_page);
}

void *pcc_gc_backend4_zpage_track_alloc_preallocated(
    PyObject *owner,
    int64_t size,
    void **prepared_node_io,
    void **prepared_page_io,
    int64_t prepared_page_from_free
) {
    if (
        owner == NULL
        || PY_IS_TAGGED_INT(owner)
        || prepared_node_io == NULL
        || prepared_page_io == NULL
    ) {
        return NULL;
    }
    if (pcc_gc_selected_backend != PCC_GC_KIND_COLORED_RELOCATING) {
        return NULL;
    }
    PccGcZPage *zpage = NULL;
    int64_t existing_offset = -1;
    if ((py_header_flags_load(py_header(owner)) & PY_FLAG_GC_ZPAGE_ALLOC) != 0) {
        zpage = pcc_gc_backend4_zpage_find_page_for_addr_unlocked(
            owner,
            size,
            &existing_offset
        );
    }
    if (zpage == NULL) {
        zpage = pcc_gc_backend4_zpage_find_reusable_page_unlocked(
            owner,
            size
        );
    }
    PccGcZPage *prepared_page = (PccGcZPage *)*prepared_page_io;
    int32_t uses_prepared_page = 0;
    if (zpage == NULL) {
        if (prepared_page == NULL) return NULL;
        zpage = prepared_page;
        uses_prepared_page = 1;
    } else if (prepared_page != NULL && prepared_page_from_free != 0) {
        prepared_page->next = pcc_gc_backend4_free_pages;
        pcc_gc_backend4_free_pages = prepared_page;
        *prepared_page_io = NULL;
        prepared_page = NULL;
    }

    PccGcZPageNode *node = (
        PccGcZPageNode *
    )pcc_gc_backend4_zpage_node_take_prepared(prepared_node_io);
    if (node == NULL) return NULL;
    node->owner = owner;
    node->page = zpage;
    node->offset_bytes = (
        existing_offset >= 0 ? existing_offset : zpage->allocated_bytes
    );
    node->size_bytes = size;
    node->payload_spans = NULL;
    node->remembered_slots = 0;
    if (pcc_gc_backend4_zpage_link_node_preallocated(node) < 0) {
        if (*prepared_node_io == NULL) {
            *prepared_node_io = node;
        } else {
            pcc_gc_backend4_zpage_node_release_unlocked(node);
        }
        return NULL;
    }
    if (uses_prepared_page != 0) {
        *prepared_page_io = NULL;
        zpage->next = pcc_gc_backend4_pages;
        pcc_gc_backend4_pages = zpage;
    }
    if (existing_offset >= 0 && zpage->pending_alloc_count > 0) {
        zpage->pending_alloc_count--;
    }
    if (existing_offset < 0) {
        zpage->allocated_bytes += pcc_gc_backend4_align_alloc_size(size);
    }
    zpage->used_bytes += size;
    zpage->object_count++;
    pcc_gc_backend4_active_page_set_unlocked(zpage);
    return node;
}

static void pcc_gc_backend4_zpage_unlink_page_unlocked(PccGcZPage *page) {
    if (page == NULL) return;
    PccGcZPage **cur = &pcc_gc_backend4_pages;
    while (*cur != NULL) {
        if (*cur == page) {
            *cur = page->next;
            return;
        }
        cur = &(*cur)->next;
    }
}

static PyObject *pcc_gc_backend4_zpage_find_owner_for_page_unlocked(
    PccGcZPage *page
) {
    if (page == NULL) return NULL;
    PccGcZPageNode *node = pcc_gc_backend4_zpage_page_head_unlocked(page);
    return node != NULL ? node->owner : NULL;
}

static void pcc_gc_backend4_zpage_account_payload_spans_removed_unlocked(
    PccGcZPageNode *owner_node
) {
    if (owner_node == NULL) return;
    PccGcZPagePayloadSpanNode *node = owner_node->payload_spans;
    while (node != NULL) {
        if (
            node->page != NULL
            && node->size_bytes > 0
            && node->offset_bytes >= 0
        ) {
            if (
                node->offset_bytes >= 0
                && node->page->allocated_bytes
                    == node->offset_bytes + node->size_bytes
            ) {
                node->page->allocated_bytes = node->offset_bytes;
            }
            if (node->page->used_bytes >= node->size_bytes) {
                node->page->used_bytes -= node->size_bytes;
            } else {
                node->page->used_bytes = 0;
            }
        }
        node = node->next;
    }
}

static void pcc_gc_backend4_zpage_free_detached_payload_spans(
    PccGcZPageNode *owner_node
) {
    if (owner_node == NULL) return;
    PccGcZPagePayloadSpanNode *node = owner_node->payload_spans;
    owner_node->payload_spans = NULL;
    while (node != NULL) {
        PccGcZPagePayloadSpanNode *next = node->next;
        free(node);
        node = next;
    }
}

static int64_t pcc_gc_backend4_zpage_remove_payload_span_base_unlocked(
    PccGcZPageNode *owner_node,
    void *base
) {
    if (owner_node == NULL || base == NULL) return 0;
    int64_t removed = 0;
    PccGcZPagePayloadSpanNode **link = &owner_node->payload_spans;
    while (*link != NULL) {
        PccGcZPagePayloadSpanNode *node = *link;
        if (node->base != (uint8_t *)base) {
            link = &node->next;
            continue;
        }
        *link = node->next;
        if (
            node->page != NULL
            && node->size_bytes > 0
            && node->offset_bytes >= 0
        ) {
            if (node->page->used_bytes >= node->size_bytes) {
                node->page->used_bytes -= node->size_bytes;
            } else {
                node->page->used_bytes = 0;
            }
        }
        free(node);
        removed++;
    }
    return removed;
}

static PccGcZPageNode *pcc_gc_backend4_zpage_detach_for_relocation_unlocked(
    PyObject *owner
) {
    if (owner == NULL || PY_IS_TAGGED_INT(owner)) return NULL;
    if (pcc_gc_selected_backend != PCC_GC_KIND_COLORED_RELOCATING) return NULL;
    PccGcObjectNode *owner_obj_node =
        (PccGcObjectNode *)pcc_gc_object_index_find(owner);
    PccGcZPageNode *dead =
        owner_obj_node != NULL ? owner_obj_node->zpage_node : NULL;
    if (owner_obj_node != NULL) owner_obj_node->zpage_node = NULL;
    PccGcZPageNode *indexed =
        (PccGcZPageNode *)pcc_gc_zpage_owner_index_find(owner);
    if (dead == NULL) {
        dead = indexed;
    }
    if (dead == NULL) return NULL;
    PccGcZPage *page = dead->page;
    pcc_gc_backend4_zpage_unlink_node_unlocked(dead);
    if (page != NULL) {
        pcc_gc_backend4_zpage_account_payload_spans_removed_unlocked(dead);
        int64_t size = dead->size_bytes;
        if (size <= 0) {
            size = pcc_gc_known_object_size_unlocked(owner);
        }
        /* Payload removal above rewinds consecutive payload reservations at
         * the virtual bump tail.  Reclaim the aligned owner reservation when
         * that exposes it as the new tail, completing the owner+payload
         * transaction in O(1).  Interior holes stay reserved because live or
         * pending allocations may still occupy bytes above them; a forwarding
         * shell stays reserved until remap retirement. */
        int64_t alloc_size = pcc_gc_backend4_align_alloc_size(size);
        if (
            size > 0
            && dead->offset_bytes >= 0
            && page->allocated_bytes == dead->offset_bytes + alloc_size
            && page->pending_forwardings <= 0
        ) {
            page->allocated_bytes = dead->offset_bytes;
        }
        if (size > 0 && page->used_bytes >= size) {
            page->used_bytes -= size;
        } else if (size > 0) {
            page->used_bytes = 0;
        }
        if (page->object_count > 0) page->object_count--;
        if (page->primary_owner == owner) {
            page->primary_owner =
                pcc_gc_backend4_zpage_find_owner_for_page_unlocked(page);
        }
        if (
            page->object_count <= 0
            && page->pending_alloc_count <= 0
        ) {
            if (page->pending_forwardings <= 0) {
                pcc_gc_backend4_zpage_unlink_page_unlocked(page);
                pcc_gc_backend4_zpage_recycle_unlocked(page);
            } else {
                /* Defer: un-healed slots may still reference this span
                 * through forwarding entries; destroying it would free
                 * memory the lazy-heal read barrier must still read.
                 * The page stays on the page list (addr lookup) but is
                 * never handed out for allocation. */
                page->zombie = 1;
                pcc_gc_backend4_active_page_clear_unlocked(page);
                /* Both disjuncts hold by construction in this branch, so a
                 * production tripwire here could never fire; the zombie
                 * retention itself is the invariant being documented. */
            }
        }
    }
    return dead;
}

static void pcc_gc_backend4_zpage_finish_relocation_detach(
    PccGcZPageNode *node
) {
    if (node == NULL) return;
    pcc_gc_backend4_zpage_free_detached_payload_spans(node);
    free(node);
}

static void pcc_gc_backend4_zpage_remove_unlocked(PyObject *owner) {
    PccGcZPageNode *dead =
        pcc_gc_backend4_zpage_detach_for_relocation_unlocked(owner);
    if (dead == NULL) return;
    pcc_gc_backend4_zpage_free_detached_payload_spans(dead);
    pcc_gc_backend4_zpage_node_release_unlocked(dead);
}

static PccGcZPageNode *pcc_gc_backend4_zpage_find_unlocked(PyObject *owner) {
    if (owner == NULL || PY_IS_TAGGED_INT(owner)) return NULL;
    PccGcObjectNode *node = (PccGcObjectNode *)pcc_gc_object_index_find(owner);
    if (node != NULL && !pcc_gc_object_node_is_freeing(node)) {
        if (node->zpage_node != NULL) return node->zpage_node;
    }
    return (PccGcZPageNode *)pcc_gc_zpage_owner_index_find(owner);
}

#define PCC_GC_BACKEND4_ZPAGE_NODE_FREE_LIMIT 8192

static PccGcZPageNode *pcc_gc_backend4_zpage_node_alloc_unlocked(void) {
    PccGcZPageNode *node = pcc_gc_backend4_zpage_node_free_list;
    if (node != NULL) {
        pcc_gc_backend4_zpage_node_free_list = node->next;
        if (pcc_gc_backend4_zpage_node_free_count > 0) {
            pcc_gc_backend4_zpage_node_free_count--;
        }
        return node;
    }
    return (PccGcZPageNode *)malloc(sizeof(PccGcZPageNode));
}

void *pcc_gc_backend4_zpage_node_prepare(void) {
    return malloc(sizeof(PccGcZPageNode));
}

int64_t pcc_gc_backend4_zpage_node_plan_requires_prepare(void) {
    return pcc_gc_backend4_zpage_node_free_list == NULL ? 1 : 0;
}

void *pcc_gc_backend4_zpage_node_take_prepared(void **prepared_io) {
    PccGcZPageNode *node = pcc_gc_backend4_zpage_node_free_list;
    if (node != NULL) {
        pcc_gc_backend4_zpage_node_free_list = node->next;
        if (pcc_gc_backend4_zpage_node_free_count > 0) {
            pcc_gc_backend4_zpage_node_free_count--;
        }
        return node;
    }
    if (prepared_io == NULL || *prepared_io == NULL) return NULL;
    node = (PccGcZPageNode *)*prepared_io;
    *prepared_io = NULL;
    return node;
}

static void pcc_gc_backend4_zpage_node_release_unlocked(
    PccGcZPageNode *node
) {
    if (node == NULL) return;
    if (
        pcc_gc_backend4_zpage_node_free_count
        >= PCC_GC_BACKEND4_ZPAGE_NODE_FREE_LIMIT
    ) {
        free(node);
        return;
    }
    node->next = pcc_gc_backend4_zpage_node_free_list;
    pcc_gc_backend4_zpage_node_free_list = node;
    pcc_gc_backend4_zpage_node_free_count++;
}

static void pcc_gc_backend4_note_forwarding_removed_on_page_unlocked(
    PccGcZPage *page
) {
    if (page == NULL) return;
    PCC_GC_DEFER_TRIPWIRE(
        page->pending_forwardings > 0,
        "pcc_gc_backend4_note_forwarding_removed_on_page_unlocked: forwarding count underflow / duplicate removal"
    );
    if (page->pending_forwardings > 0) page->pending_forwardings--;
    if (
        page->zombie != 0
        && page->pending_forwardings <= 0
        && page->object_count <= 0
        && page->pending_alloc_count <= 0
    ) {
        page->zombie = 0;
        pcc_gc_backend4_zpage_unlink_page_unlocked(page);
        /* one-epoch defer: recycling would let the span be reused (or
         * freed) while stale SSA/borrowed pointers from the current
         * step window can still read old headers — park instead; the
         * NEXT remap destroys parked pages. */
        pcc_gc_backend4_park_page_unlocked(page);
    }
}

static void pcc_gc_backend4_zpage_note_forwarding_removed_unlocked(
    PyObject *from
) {
    if (pcc_gc_selected_backend != PCC_GC_KIND_COLORED_RELOCATING) return;
    if (from == NULL || PY_IS_TAGGED_INT(from)) return;
    if (
        (py_header_flags_load(py_header(from)) & PY_FLAG_GC_ZPAGE_ALLOC) == 0
    ) {
        return;
    }
    pcc_gc_backend4_note_forwarding_removed_on_page_unlocked(
        pcc_gc_backend4_zpage_find_page_for_addr_unlocked(
            (void *)from,
            (int64_t)sizeof(PyObjectHeader),
            NULL
        )
    );
}

static void pcc_gc_backend4_zpage_note_owner_promoted_unlocked(
    PyObject *owner
) {
    PccGcZPageNode *node = pcc_gc_backend4_zpage_find_unlocked(owner);
    if (node != NULL && node->page != NULL) {
        node->page->generation = 2;
    }
}

static int32_t pcc_gc_backend4_zpage_card_for_node_unlocked(
    PccGcZPageNode *node
) {
    if (node == NULL || node->page == NULL) return -1;
    if (node->offset_bytes < 0) return -1;
    int64_t card =
        (node->offset_bytes / PCC_GC_BACKEND4_ZPAGE_SPAN_CARD_BYTES)
        % PCC_GC_BACKEND4_ZPAGE_CARD_COUNT;
    if (card < 0 || card >= PCC_GC_BACKEND4_ZPAGE_CARD_COUNT) return -1;
    return (int32_t)card;
}

static int64_t pcc_gc_backend4_zpage_payload_offset_for_slot_unlocked(
    PccGcZPageNode *owner_node,
    PyObject **slot
) {
    if (owner_node == NULL || slot == NULL) return -1;
    uintptr_t slot_addr = (uintptr_t)slot;
    for (
        PccGcZPagePayloadSpanNode *span = owner_node->payload_spans;
        span != NULL;
        span = span->next
    ) {
        if (span->base == NULL) continue;
        if (span->size_bytes <= 0 || span->offset_bytes < 0) continue;
        uintptr_t base = (uintptr_t)span->base;
        uintptr_t size = (uintptr_t)span->size_bytes;
        if (slot_addr >= base && slot_addr - base < size) {
            return span->offset_bytes + (int64_t)(slot_addr - base);
        }
    }
    return -1;
}

static int32_t pcc_gc_backend4_zpage_card_for_node_slot_unlocked(
    PccGcZPageNode *node,
    PyObject **slot
) {
    if (node == NULL || node->page == NULL) return -1;
    if (node->offset_bytes < 0) return -1;
    int64_t span_offset = node->offset_bytes;
    if (slot != NULL && node->owner != NULL && node->size_bytes > 0) {
        uintptr_t base = (uintptr_t)node->owner;
        uintptr_t slot_addr = (uintptr_t)slot;
        uintptr_t size = (uintptr_t)node->size_bytes;
        if (slot_addr >= base && slot_addr - base < size) {
            span_offset += (int64_t)(slot_addr - base);
        } else {
            int64_t payload_offset =
                pcc_gc_backend4_zpage_payload_offset_for_slot_unlocked(
                    node,
                    slot
                );
            if (payload_offset >= 0) span_offset = payload_offset;
        }
    }
    int64_t card =
        (span_offset / PCC_GC_BACKEND4_ZPAGE_SPAN_CARD_BYTES)
        % PCC_GC_BACKEND4_ZPAGE_CARD_COUNT;
    if (card < 0 || card >= PCC_GC_BACKEND4_ZPAGE_CARD_COUNT) return -1;
    return (int32_t)card;
}

static void pcc_gc_backend4_zpage_note_remembered_slot_unlocked(
    PyObject *owner,
    int64_t delta
) {
    if (owner == NULL || PY_IS_TAGGED_INT(owner)) return;
    if (delta == 0) return;
    PccGcZPageNode *node = pcc_gc_backend4_zpage_find_unlocked(owner);
    if (node == NULL || node->page == NULL) return;
    PccGcZPage *page = node->page;
    int64_t next = page->remembered_slots + delta;
    if (next < 0) next = 0;
    page->remembered_slots = next;
    next = node->remembered_slots + delta;
    if (next < 0) next = 0;
    node->remembered_slots = next;
}

static void pcc_gc_backend4_zpage_note_remembered_card_unlocked(
    PyObject *owner,
    PyObject **slot,
    int64_t delta
) {
    if (owner == NULL || PY_IS_TAGGED_INT(owner)) return;
    if (slot == NULL) return;
    if (delta == 0) return;
    PccGcZPageNode *node = pcc_gc_backend4_zpage_find_unlocked(owner);
    if (node == NULL || node->page == NULL) return;
    PccGcZPage *page = node->page;
    int32_t card = pcc_gc_backend4_zpage_card_for_node_slot_unlocked(
        node,
        slot
    );
    if (card < 0) return;
    uint64_t mask = (uint64_t)1 << (uint64_t)card;
    if (delta > 0) {
        if (page->remembered_card_refcounts[card] == 0) {
            page->remembered_card_bitmap |= mask;
            page->remembered_cards++;
        }
        if (page->remembered_card_refcounts[card] < UINT16_MAX) {
            page->remembered_card_refcounts[card]++;
        }
        return;
    }
    if (page->remembered_card_refcounts[card] == 0) return;
    page->remembered_card_refcounts[card]--;
    if (page->remembered_card_refcounts[card] == 0) {
        page->remembered_card_bitmap &= ~mask;
        if (page->remembered_cards > 0) page->remembered_cards--;
    }
}

static int32_t pcc_gc_backend4_zpage_card_for_slot_unlocked(
    PyObject **slot
) {
    if (slot == NULL) return -1;
    int32_t slot_bit = pcc_gc_backend4_remembered_page_slot_bit(slot);
    if (
        slot_bit < 0
        || slot_bit >= PCC_GC_BACKEND4_REMEMBERED_PAGE_SLOT_BITS
    ) return -1;
    int32_t card = slot_bit / PCC_GC_BACKEND4_ZPAGE_CARD_SLOT_BITS;
    if (card < 0 || card >= PCC_GC_BACKEND4_ZPAGE_CARD_COUNT) return -1;
    return card;
}

static int pcc_gc_backend4_zpage_contains_remembered_card_unlocked(
    PyObject *owner,
    PyObject **slot
) {
    if (owner == NULL || PY_IS_TAGGED_INT(owner)) return 0;
    PccGcZPageNode *node = pcc_gc_backend4_zpage_find_unlocked(owner);
    if (node == NULL || node->page == NULL) return 0;
    if (slot == NULL) return 0;
    int32_t card = pcc_gc_backend4_zpage_card_for_node_slot_unlocked(
        node,
        slot
    );
    if (card < 0) return 0;
    uint64_t mask = (uint64_t)1 << (uint64_t)card;
    return (node->page->remembered_card_bitmap & mask) != 0;
}

static void pcc_gc_backend4_remembered_set_retarget_slot_unlocked(
    PyObject *from_owner,
    PyObject *to_owner,
    PyObject **from_slot,
    PyObject **to_slot
) {
    if (from_owner == NULL || to_owner == NULL) return;
    if (PY_IS_TAGGED_INT(from_owner) || PY_IS_TAGGED_INT(to_owner)) return;
    if (from_slot == NULL || to_slot == NULL) return;
    if (from_owner == to_owner && from_slot == to_slot) return;
    for (
        PccGcRememberedSlotNode *n = pcc_gc_backend4_remembered_slots;
        n != NULL;
        n = n->next
    ) {
        if (n->owner != from_owner || n->slot != from_slot) continue;
        pcc_gc_backend4_remembered_page_remove_slot(n->slot);
        pcc_gc_backend4_zpage_note_remembered_slot_unlocked(n->owner, -1);
        pcc_gc_backend4_zpage_note_remembered_card_unlocked(
            n->owner, n->slot, -1
        );
        n->owner = to_owner;
        n->slot = to_slot;
        pcc_gc_backend4_remembered_page_add(n->slot);
        pcc_gc_backend4_zpage_note_remembered_slot_unlocked(n->owner, 1);
        pcc_gc_backend4_zpage_note_remembered_card_unlocked(
            n->owner, n->slot, 1
        );
    }
}

static void pcc_gc_retarget_continuation_root_slots_unlocked(
    PyObject **from_slots,
    const int32_t *from_frame_map,
    PyObject **to_slots,
    const int32_t *to_frame_map
) {
    if (from_slots == NULL || to_slots == NULL || to_frame_map == NULL) return;
    int changed = 0;
    for (
        PccGcContinuationRootNode *n = pcc_gc_continuation_roots;
        n != NULL;
        n = n->next
    ) {
        if (n->slots != from_slots) continue;
        if (from_frame_map != NULL && n->frame_map != from_frame_map) continue;
        n->slots = to_slots;
        n->frame_map = to_frame_map;
        if (pcc_gc_backend3_continuation_root_scan_cursor == n) {
            pcc_gc_backend3_frame_root_scan_slot = 0;
        }
        if (pcc_gc_runtime_root_snapshot_continuation_cursor == n) {
            pcc_gc_runtime_root_snapshot_slot = 0;
        }
        changed = 1;
    }
    if (changed) pcc_gc_root_registry_revision_advance_unlocked();
}

static int64_t pcc_gc_backend4_zpage_population(int32_t metric) {
    int64_t total = 0;
    pcc_gc_graph_lock();
    for (
        PccGcZPage *page = pcc_gc_backend4_pages;
        page != NULL;
        page = page->next
    ) {
        if (metric == 0) {
            total++;
        } else if (metric == 1) {
            total += page->capacity_bytes;
        } else if (metric == 2) {
            if (page->capacity_bytes > page->used_bytes) {
                total += page->capacity_bytes - page->used_bytes;
            }
        } else if (metric == 3) {
            if (page->page_class == 2) total++;
        } else if (metric == 4) {
            total += page->remembered_slots;
        } else if (metric == 5) {
            if (page->remembered_cards > 0) total++;
        } else if (metric == 6) {
            if (page->capacity_bytes > page->used_bytes) total++;
        } else if (metric == 7) {
            if (page->generation == 1) total++;
        } else if (metric == 8) {
            if (page->generation == 2) total++;
        } else if (metric == 9) {
            total += page->remembered_cards;
        } else if (metric == 10) {
            total += page->allocated_bytes;
        } else if (metric == 11) {
            if (page->allocated_bytes > page->used_bytes) {
                total += page->allocated_bytes - page->used_bytes;
            }
        } else if (metric == 12) {
            total += page->span_capacity_bytes;
        }
    }
    pcc_gc_graph_unlock();
    return total;
}

int64_t pcc_gc_backend4_zpage_count(void) {
    return pcc_gc_backend4_zpage_population(0);
}

int64_t pcc_gc_backend4_zpage_capacity_bytes(void) {
    return pcc_gc_backend4_zpage_population(1);
}

int64_t pcc_gc_backend4_zpage_fragmentation_bytes(void) {
    return pcc_gc_backend4_zpage_population(2);
}

int64_t pcc_gc_backend4_zpage_large_pages(void) {
    return pcc_gc_backend4_zpage_population(3);
}

int64_t pcc_gc_backend4_zpage_remembered_slots(void) {
    return pcc_gc_backend4_zpage_population(4);
}

int64_t pcc_gc_backend4_zpage_remembered_cards(void) {
    return pcc_gc_backend4_zpage_population(9);
}

int64_t pcc_gc_backend4_zpage_remembered_card_ratio_per_mille(void) {
    /* Read-only density telemetry: keep selector policy on absolute
     * slot/card/page pressure until the production matrix has enough data to
     * tune a density threshold. */
    int64_t pages = pcc_gc_backend4_zpage_count();
    if (pages <= 0) return 0;
    int64_t capacity = pages * PCC_GC_BACKEND4_ZPAGE_CARD_COUNT;
    if (capacity <= 0) return 0;
    int64_t cards = pcc_gc_backend4_zpage_remembered_cards();
    if (cards <= 0) return 0;
    if (cards >= capacity) return 1000;
    return (cards * 1000) / capacity;
}

int64_t pcc_gc_backend4_zpage_dirty_pages(void) {
    return pcc_gc_backend4_zpage_population(5);
}

int64_t pcc_gc_backend4_zpage_fragmented_pages(void) {
    return pcc_gc_backend4_zpage_population(6);
}

int64_t pcc_gc_backend4_zpage_young_pages(void) {
    return pcc_gc_backend4_zpage_population(7);
}

int64_t pcc_gc_backend4_zpage_old_pages(void) {
    return pcc_gc_backend4_zpage_population(8);
}

static int64_t pcc_gc_backend4_zpage_free_population(int32_t metric) {
    int64_t total = 0;
    pcc_gc_graph_lock();
    for (
        PccGcZPage *page = pcc_gc_backend4_free_pages;
        page != NULL;
        page = page->next
    ) {
        if (metric == 0) {
            total++;
        } else if (metric == 1) {
            total += page->capacity_bytes;
        } else if (metric == 2) {
            total += page->span_capacity_bytes;
        }
    }
    pcc_gc_graph_unlock();
    return total;
}

int64_t pcc_gc_backend4_zpage_free_pages(void) {
    return pcc_gc_backend4_zpage_free_population(0);
}

int64_t pcc_gc_backend4_zpage_free_capacity_bytes(void) {
    return pcc_gc_backend4_zpage_free_population(1);
}

int64_t pcc_gc_backend4_zpage_free_span_bytes(void) {
    return pcc_gc_backend4_zpage_free_population(2);
}

int64_t pcc_gc_backend4_zpage_used_bytes(void) {
    int64_t capacity = pcc_gc_backend4_zpage_capacity_bytes();
    int64_t fragmentation = pcc_gc_backend4_zpage_fragmentation_bytes();
    if (capacity <= fragmentation) return 0;
    return capacity - fragmentation;
}

int64_t pcc_gc_backend4_zpage_allocated_bytes(void) {
    return pcc_gc_backend4_zpage_population(10);
}

int64_t pcc_gc_backend4_zpage_reclaimable_gap_bytes(void) {
    return pcc_gc_backend4_zpage_population(11);
}

int64_t pcc_gc_backend4_zpage_span_bytes(void) {
    return pcc_gc_backend4_zpage_population(12);
}

int64_t pcc_gc_backend4_zpage_owner_offset_bytes(PyObject *owner) {
    pcc_gc_init_config();
    if (owner == NULL || PY_IS_TAGGED_INT(owner)) return -1;
    pcc_gc_graph_lock();
    PccGcZPageNode *node = pcc_gc_backend4_zpage_find_unlocked(owner);
    int64_t out = -1;
    if (node != NULL) out = node->offset_bytes;
    pcc_gc_graph_unlock();
    return out;
}

int64_t pcc_gc_backend4_zpage_owner_size_bytes(PyObject *owner) {
    pcc_gc_init_config();
    if (owner == NULL || PY_IS_TAGGED_INT(owner)) return -1;
    pcc_gc_graph_lock();
    PccGcZPageNode *node = pcc_gc_backend4_zpage_find_unlocked(owner);
    int64_t out = -1;
    if (node != NULL) out = node->size_bytes;
    pcc_gc_graph_unlock();
    return out;
}

int64_t pcc_gc_backend4_zpage_owner_span_card(PyObject *owner) {
    int64_t offset = pcc_gc_backend4_zpage_owner_offset_bytes(owner);
    if (offset < 0) return -1;
    return (offset / PCC_GC_BACKEND4_ZPAGE_SPAN_CARD_BYTES)
        % PCC_GC_BACKEND4_ZPAGE_CARD_COUNT;
}

int64_t pcc_gc_backend4_zpage_owner_slot_span_card(
    PyObject *owner,
    PyObject **slot
) {
    pcc_gc_init_config();
    if (owner == NULL || PY_IS_TAGGED_INT(owner) || slot == NULL) return -1;
    pcc_gc_graph_lock();
    PccGcZPageNode *node = pcc_gc_backend4_zpage_find_unlocked(owner);
    int32_t card = pcc_gc_backend4_zpage_card_for_node_slot_unlocked(
        node,
        slot
    );
    pcc_gc_graph_unlock();
    return card;
}

static int64_t pcc_gc_backend4_zpage_register_owner_payload_span_unlocked(
    PyObject *owner,
    void *base,
    int64_t size_bytes
) {
    if (owner == NULL || PY_IS_TAGGED_INT(owner)) return -1;
    if (base == NULL || size_bytes <= 0) return -1;
    PccGcZPageNode *node = pcc_gc_backend4_zpage_find_unlocked(owner);
    if (node == NULL || node->page == NULL) return -1;
    PccGcZPage *page = node->page;
    for (PccGcZPagePayloadSpanNode *span = node->payload_spans;
         span != NULL;
         span = span->next) {
        if (span->base != (uint8_t *)base) continue;
        if (span->page != page) return -1;
        if (span->offset_bytes < 0) {
            span->size_bytes = size_bytes;
            return 0;
        }
        if (size_bytes > page->capacity_bytes - span->offset_bytes) {
            if (page->used_bytes >= span->size_bytes) {
                page->used_bytes -= span->size_bytes;
            } else {
                page->used_bytes = 0;
            }
            span->offset_bytes = -1;
            span->size_bytes = size_bytes;
            return 0;
        }
        if (size_bytes >= span->size_bytes) {
            page->used_bytes += size_bytes - span->size_bytes;
        } else {
            int64_t delta = span->size_bytes - size_bytes;
            if (page->used_bytes >= delta) page->used_bytes -= delta;
            else page->used_bytes = 0;
        }
        span->size_bytes = size_bytes;
        int64_t end = span->offset_bytes + size_bytes;
        if (page->allocated_bytes < end) page->allocated_bytes = end;
        return span->offset_bytes;
    }
    if (page->allocated_bytes > page->capacity_bytes) return -1;
    int64_t available = page->capacity_bytes - page->allocated_bytes;
    PccGcZPagePayloadSpanNode *span =
        (PccGcZPagePayloadSpanNode *)calloc(1, sizeof(PccGcZPagePayloadSpanNode));
    if (span == NULL) return -1;
    span->owner = owner;
    span->base = (uint8_t *)base;
    span->size_bytes = size_bytes;
    span->offset_bytes = size_bytes <= available ? page->allocated_bytes : -1;
    span->page = page;
    if (span->offset_bytes >= 0) {
        page->allocated_bytes += size_bytes;
        page->used_bytes += size_bytes;
    }
    span->next = node->payload_spans;
    node->payload_spans = span;
    return span->offset_bytes >= 0 ? span->offset_bytes : 0;
}

enum {
    PCC_GC_RELOCATION_PAYLOAD_SPAN_MAX = 4,
};

static int pcc_gc_backend4_zpage_payload_span_preflight_unlocked(
    PyObject *owner,
    int64_t total_size_bytes
) {
    if (owner == NULL || PY_IS_TAGGED_INT(owner) || total_size_bytes < 0) {
        return 0;
    }
    if (total_size_bytes == 0) return 1;
    PccGcZPageNode *node = pcc_gc_backend4_zpage_find_unlocked(owner);
    if (node == NULL || node->page == NULL) return 0;
    if (node->payload_spans != NULL) return 0;
    PccGcZPage *page = node->page;
    if (page->allocated_bytes < 0) return 0;
    if (page->allocated_bytes > page->capacity_bytes) return 0;
    return total_size_bytes <= page->capacity_bytes - page->allocated_bytes;
}

static int pcc_gc_backend4_zpage_publish_relocation_payload_spans_unlocked(
    PyObject *owner,
    PccGcZPagePayloadSpanNode *span_head,
    int64_t span_count,
    int64_t total_size_bytes
) {
    if (owner == NULL || PY_IS_TAGGED_INT(owner)) return 0;
    if (
        span_head == NULL
        || span_count <= 0
        || span_count > PCC_GC_RELOCATION_PAYLOAD_SPAN_MAX
        || total_size_bytes <= 0
    ) return 0;
    PccGcZPageNode *node = pcc_gc_backend4_zpage_find_unlocked(owner);
    if (node == NULL || node->page == NULL) return 0;
    if (node->payload_spans != NULL) return 0;
    PccGcZPage *page = node->page;
    int64_t computed_size_bytes = 0;
    PccGcZPagePayloadSpanNode *span = span_head;
    for (int64_t i = 0; i < span_count; i++) {
        if (span == NULL || span->base == NULL || span->size_bytes <= 0) {
            return 0;
        }
        if (span->size_bytes > INT64_MAX - computed_size_bytes) return 0;
        computed_size_bytes += span->size_bytes;
        span = span->next;
    }
    if (span != NULL || computed_size_bytes != total_size_bytes) return 0;
    if (page->allocated_bytes < 0) return 0;
    if (page->allocated_bytes > page->capacity_bytes) return 0;
    if (total_size_bytes > page->capacity_bytes - page->allocated_bytes) return 0;
    if (
        page->used_bytes < 0
        || total_size_bytes > INT64_MAX - page->used_bytes
    ) return 0;

    int64_t offset_bytes = page->allocated_bytes;
    span = span_head;
    for (int64_t i = 0; i < span_count; i++) {
        span->owner = owner;
        span->offset_bytes = offset_bytes;
        span->page = page;
        offset_bytes += span->size_bytes;
        span = span->next;
    }
    node->payload_spans = span_head;
    page->allocated_bytes = offset_bytes;
    page->used_bytes += total_size_bytes;
    return 1;
}

static int64_t pcc_gc_backend4_zpage_retarget_owner_payload_span_unlocked(
    PyObject *owner,
    void *old_base,
    void *new_base,
    int64_t size_bytes
) {
    if (owner == NULL || PY_IS_TAGGED_INT(owner)) return -1;
    if (old_base == NULL || new_base == NULL || size_bytes <= 0) return -1;
    PccGcZPageNode *node = pcc_gc_backend4_zpage_find_unlocked(owner);
    if (node == NULL || node->page == NULL) return -1;
    PccGcZPage *page = node->page;
    for (PccGcZPagePayloadSpanNode *span = node->payload_spans;
         span != NULL;
         span = span->next) {
        if (span->base != (uint8_t *)old_base) continue;
        if (span->page != page) return -1;
        if (span->offset_bytes < 0) {
            span->base = (uint8_t *)new_base;
            span->size_bytes = size_bytes;
            return 0;
        }
        if (size_bytes > page->capacity_bytes - span->offset_bytes) {
            if (page->used_bytes >= span->size_bytes) {
                page->used_bytes -= span->size_bytes;
            } else {
                page->used_bytes = 0;
            }
            span->base = (uint8_t *)new_base;
            span->size_bytes = size_bytes;
            span->offset_bytes = -1;
            return 0;
        }
        if (size_bytes >= span->size_bytes) {
            page->used_bytes += size_bytes - span->size_bytes;
        } else {
            int64_t delta = span->size_bytes - size_bytes;
            if (page->used_bytes >= delta) page->used_bytes -= delta;
            else page->used_bytes = 0;
        }
        span->base = (uint8_t *)new_base;
        span->size_bytes = size_bytes;
        int64_t end = span->offset_bytes + size_bytes;
        if (page->allocated_bytes < end) page->allocated_bytes = end;
        return span->offset_bytes;
    }
    return -1;
}

static int pcc_gc_slot_in_raw_span(
    PyObject **slot,
    void *base,
    int64_t size_bytes,
    int64_t *out_offset
) {
    if (
        slot == NULL
        || base == NULL
        || size_bytes < (int64_t)sizeof(PyObject *)
    ) return 0;
    uintptr_t slot_addr = (uintptr_t)(void *)slot;
    uintptr_t base_addr = (uintptr_t)base;
    uintptr_t size = (uintptr_t)size_bytes;
    if (base_addr > UINTPTR_MAX - size) return 0;
    uintptr_t end = base_addr + size;
    if (slot_addr < base_addr || slot_addr >= end) return 0;
    uintptr_t offset = slot_addr - base_addr;
    if (offset > size - sizeof(PyObject *)) return 0;
    if ((offset % sizeof(PyObject *)) != 0) return 0;
    if (out_offset != NULL) *out_offset = (int64_t)offset;
    return 1;
}

static PyObject **pcc_gc_backend4_map_mutator_payload_slot(
    PyObject **slot,
    void *old_base,
    int64_t old_size_bytes,
    void *new_base,
    int64_t new_size_bytes,
    void *slot_pairs,
    int64_t pair_count
) {
    int64_t fallback_offset = 0;
    if (!pcc_gc_slot_in_raw_span(
            slot, old_base, old_size_bytes, &fallback_offset
        )) return slot;
    PyObject ***pairs = (PyObject ***)slot_pairs;
    for (int64_t i = 0; i < pair_count; i++) {
        if (pairs[i * 2] == slot) return pairs[i * 2 + 1];
    }
    if (fallback_offset < 0 || fallback_offset >= new_size_bytes) return NULL;
    return (PyObject **)((uint8_t *)new_base + fallback_offset);
}

int64_t pcc_gc_backend4_retarget_mutator_payload_locked(
    PyObject *owner,
    void *old_base,
    int64_t old_size_bytes,
    void *new_base,
    int64_t new_size_bytes,
    void *slot_pairs,
    int64_t pair_count
) {
    if (pcc_gc_selected_backend != PCC_GC_KIND_COLORED_RELOCATING) return 1;
    if (owner == NULL || PY_IS_TAGGED_INT(owner)) return 0;
    if (old_base == NULL || new_base == NULL) return 0;
    if (old_size_bytes <= 0 || new_size_bytes < old_size_bytes) return 0;
    if (pair_count < 0 || (pair_count > 0 && slot_pairs == NULL)) return 0;

    PccGcZPageNode *owner_node = pcc_gc_backend4_zpage_find_unlocked(owner);
    if (owner_node == NULL || owner_node->page == NULL) return 0;
    PccGcZPage *page = owner_node->page;
    PccGcZPagePayloadSpanNode *payload_span = owner_node->payload_spans;
    while (payload_span != NULL && payload_span->base != old_base) {
        payload_span = payload_span->next;
    }
    int has_payload_span = payload_span != NULL;
    if (
        has_payload_span
        && (
            payload_span->page != page
            || payload_span->size_bytes != old_size_bytes
            || payload_span->offset_bytes < -1
        )
    ) return 0;

    PyObject ***pairs = (PyObject ***)slot_pairs;
    for (int64_t i = 0; i < pair_count; i++) {
        int64_t old_offset = 0;
        int64_t new_offset = 0;
        if (!pcc_gc_slot_in_raw_span(
                pairs[i * 2], old_base, old_size_bytes, &old_offset
            )) return 0;
        if (!pcc_gc_slot_in_raw_span(
                pairs[i * 2 + 1], new_base, new_size_bytes, &new_offset
            )) return 0;
        (void)old_offset;
        (void)new_offset;
    }

    for (
        PccGcStoreBufferMediumState *state =
            pcc_gc_backend4_store_buffer_medium_states;
        state != NULL;
        state = state->next
    ) {
        int32_t count = state->count == NULL ? 0 : *state->count;
        for (int32_t i = 0; i < count; i++) {
            PccGcStoreBufferEntry *entry = &state->entries[i];
            if (entry->owner != owner) continue;
            PyObject **mapped = pcc_gc_backend4_map_mutator_payload_slot(
                entry->slot,
                old_base,
                old_size_bytes,
                new_base,
                new_size_bytes,
                slot_pairs,
                pair_count
            );
            entry->slot = mapped;
        }
    }
    for (
        PccGcStoreBufferNode *entry = pcc_gc_backend4_store_buffer;
        entry != NULL;
        entry = entry->next
    ) {
        if (entry->owner != owner) continue;
        PyObject **mapped = pcc_gc_backend4_map_mutator_payload_slot(
            entry->slot,
            old_base,
            old_size_bytes,
            new_base,
            new_size_bytes,
            slot_pairs,
            pair_count
        );
        entry->slot = mapped;
    }

    for (
        PccGcRememberedSlotNode *entry = pcc_gc_backend4_remembered_slots;
        entry != NULL;
        entry = entry->next
    ) {
        if (entry->owner != owner) continue;
        int64_t old_offset = 0;
        if (!pcc_gc_slot_in_raw_span(
                entry->slot, old_base, old_size_bytes, &old_offset
            )) continue;
        PyObject **mapped = pcc_gc_backend4_map_mutator_payload_slot(
            entry->slot,
            old_base,
            old_size_bytes,
            new_base,
            new_size_bytes,
            slot_pairs,
            pair_count
        );
        if (mapped == NULL) return 0;
        pcc_gc_backend4_remembered_page_remove_slot(entry->slot);
        if (has_payload_span) {
            pcc_gc_backend4_zpage_note_remembered_slot_unlocked(owner, -1);
            pcc_gc_backend4_zpage_note_remembered_card_unlocked(
                owner, entry->slot, -1
            );
        }
        entry->slot = mapped;
    }

    if (has_payload_span) {
        if (
            payload_span->offset_bytes >= 0
            && new_size_bytes
                > page->capacity_bytes - payload_span->offset_bytes
        ) {
            if (page->used_bytes >= payload_span->size_bytes) {
                page->used_bytes -= payload_span->size_bytes;
            } else {
                page->used_bytes = 0;
            }
            payload_span->offset_bytes = -1;
        } else if (payload_span->offset_bytes >= 0) {
            if (new_size_bytes >= payload_span->size_bytes) {
                page->used_bytes += new_size_bytes - payload_span->size_bytes;
            } else {
                int64_t delta = payload_span->size_bytes - new_size_bytes;
                if (page->used_bytes >= delta) page->used_bytes -= delta;
                else page->used_bytes = 0;
            }
        }
        payload_span->base = (uint8_t *)new_base;
        payload_span->size_bytes = new_size_bytes;
        if (payload_span->offset_bytes >= 0) {
            int64_t span_end = payload_span->offset_bytes + new_size_bytes;
            if (page->allocated_bytes < span_end) page->allocated_bytes = span_end;
        }
    }

    for (
        PccGcRememberedSlotNode *entry = pcc_gc_backend4_remembered_slots;
        entry != NULL;
        entry = entry->next
    ) {
        if (entry->owner != owner) continue;
        int64_t new_offset = 0;
        if (!pcc_gc_slot_in_raw_span(
                entry->slot, new_base, new_size_bytes, &new_offset
            )) continue;
        pcc_gc_backend4_remembered_page_add(entry->slot);
        if (has_payload_span) {
            pcc_gc_backend4_zpage_note_remembered_slot_unlocked(owner, 1);
            pcc_gc_backend4_zpage_note_remembered_card_unlocked(
                owner, entry->slot, 1
            );
        }
    }
    return has_payload_span ? 1 : 2;
}

int64_t pcc_gc_backend4_zpage_register_owner_payload_span(
    PyObject *owner,
    void *base,
    int64_t size_bytes
) {
    pcc_gc_init_config();
    if (pcc_gc_selected_backend != PCC_GC_KIND_COLORED_RELOCATING) return -1;
    pcc_gc_graph_lock();
    int64_t offset = pcc_gc_backend4_zpage_register_owner_payload_span_unlocked(
        owner,
        base,
        size_bytes
    );
    pcc_gc_graph_unlock();
    return offset;
}

int64_t pcc_gc_backend4_zpage_unregister_owner_payload_span(
    PyObject *owner,
    void *base
) {
    pcc_gc_init_config();
    if (owner == NULL || PY_IS_TAGGED_INT(owner) || base == NULL) return -1;
    pcc_gc_graph_lock();
    PccGcZPageNode *node = pcc_gc_backend4_zpage_find_unlocked(owner);
    int64_t removed = 0;
    if (node != NULL) {
        removed = pcc_gc_backend4_zpage_remove_payload_span_base_unlocked(
            node,
            base
        );
    }
    pcc_gc_graph_unlock();
    return removed;
}

int64_t pcc_gc_backend4_zpage_retarget_owner_payload_span(
    PyObject *owner,
    void *old_base,
    void *new_base,
    int64_t size_bytes
) {
    pcc_gc_init_config();
    pcc_gc_graph_lock();
    int64_t offset = pcc_gc_backend4_zpage_retarget_owner_payload_span_unlocked(
        owner,
        old_base,
        new_base,
        size_bytes
    );
    pcc_gc_graph_unlock();
    return offset;
}

int64_t pcc_gc_backend4_zpage_fragmentation_per_mille(void) {
    int64_t capacity = pcc_gc_backend4_zpage_capacity_bytes();
    if (capacity <= 0) return 0;
    int64_t fragmentation = pcc_gc_backend4_zpage_fragmentation_bytes();
    if (fragmentation <= 0) return 0;
    if (fragmentation >= capacity) return 1000;
    return (fragmentation * 1000) / capacity;
}

int64_t pcc_gc_backend4_zpage_policy_score(void) {
    return pcc_gc_backend4_zpage_fragmentation_bytes()
        + pcc_gc_backend4_fragmentation_backlog_bytes()
        + pcc_gc_backend4_evacuation_incomplete_batches()
        + pcc_gc_backend4_zpage_remembered_slots()
        + pcc_gc_backend4_zpage_remembered_cards()
        + pcc_gc_backend4_zpage_dirty_pages()
        + pcc_gc_backend4_zpage_fragmented_pages()
        + pcc_gc_backend4_zpage_old_pages();
}

int64_t pcc_gc_backend4_small_page_candidate_score(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_small_page_candidates,
        __ATOMIC_RELAXED
    );
}

int64_t pcc_gc_backend4_medium_page_candidate_score(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_medium_page_candidates,
        __ATOMIC_RELAXED
    );
}

int64_t pcc_gc_backend4_evacuation_candidate_bytes(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_evacuation_candidate_bytes_count,
        __ATOMIC_RELAXED
    );
}

int64_t pcc_gc_backend4_small_page_candidate_bytes(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_small_page_candidate_bytes_count,
        __ATOMIC_RELAXED
    );
}

int64_t pcc_gc_backend4_medium_page_candidate_bytes(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_medium_page_candidate_bytes_count,
        __ATOMIC_RELAXED
    );
}

int64_t pcc_gc_backend4_evacuation_candidate_zpage_bytes(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_evacuation_candidate_zpage_bytes_count,
        __ATOMIC_RELAXED
    );
}

int64_t pcc_gc_backend4_small_page_candidate_zpage_bytes(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_small_page_candidate_zpage_bytes_count,
        __ATOMIC_RELAXED
    );
}

int64_t pcc_gc_backend4_medium_page_candidate_zpage_bytes(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_medium_page_candidate_zpage_bytes_count,
        __ATOMIC_RELAXED
    );
}

static int64_t pcc_gc_backend4_evacuation_page_population(int32_t metric) {
    int64_t total = 0;
    pcc_gc_graph_lock();
    for (
        PccGcZPageEvacuationNode *n = pcc_gc_backend4_evacuation_pages;
        n != NULL;
        n = n->next
    ) {
        PccGcZPage *page = n->page;
        if (page == NULL) continue;
        if (metric == 0) {
            total++;
        } else if (metric == 1) {
            total += page->used_bytes;
        } else if (metric == 2) {
            total += page->remembered_cards;
        }
    }
    pcc_gc_graph_unlock();
    return total;
}

int64_t pcc_gc_backend4_evacuation_page_candidate_score(void) {
    return pcc_gc_backend4_evacuation_page_population(0);
}

int64_t pcc_gc_backend4_evacuation_page_candidate_bytes(void) {
    return pcc_gc_backend4_evacuation_page_population(1);
}

int64_t pcc_gc_backend4_evacuation_page_dirty_cards(void) {
    return pcc_gc_backend4_evacuation_page_population(2);
}

int64_t pcc_gc_backend4_store_buffer_drain_batches(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_store_buffer_drain_batches_count,
        __ATOMIC_RELAXED
    );
}

int64_t pcc_gc_backend4_store_buffer_drained_entries(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_store_buffer_drained_entries_count,
        __ATOMIC_RELAXED
    );
}

int64_t pcc_gc_backend4_store_buffer_duplicate_skips(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_store_buffer_duplicate_skips_count,
        __ATOMIC_RELAXED
    );
}

int64_t pcc_gc_backend4_store_buffer_high_water(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_store_buffer_high_water_count,
        __ATOMIC_RELAXED
    );
}

int64_t pcc_gc_backend4_store_buffer_owner_fanout_high_water(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_store_buffer_owner_fanout_high_water_count,
        __ATOMIC_RELAXED
    );
}

int64_t pcc_gc_backend4_store_buffer_owner_count_high_water(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_store_buffer_owner_count_high_water_count,
        __ATOMIC_RELAXED
    );
}

int64_t pcc_gc_backend4_store_buffer_incomplete_drains(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_store_buffer_incomplete_drains_count,
        __ATOMIC_RELAXED
    );
}

int64_t pcc_gc_backend4_evacuation_incomplete_batches(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_evacuation_incomplete_batches_count,
        __ATOMIC_RELAXED
    );
}

int64_t pcc_gc_backend4_store_buffer_batch_capacity(void) {
    return PCC_GC_BACKEND4_STORE_BUFFER_BATCH_CAPACITY;
}

int64_t pcc_gc_backend4_store_buffer_max_batch_size(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_store_buffer_max_batch_size_count,
        __ATOMIC_RELAXED
    );
}

int64_t pcc_gc_backend4_store_buffer_full_batches(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_store_buffer_full_batches_count,
        __ATOMIC_RELAXED
    );
}

int64_t pcc_gc_backend4_store_buffer_medium_capacity(void) {
    return PCC_GC_BACKEND4_STORE_BUFFER_MEDIUM_CAPACITY;
}

int64_t pcc_gc_backend4_store_buffer_medium_pending(void) {
    int64_t count = 0;
    pcc_gc_graph_lock();
    for (
        PccGcStoreBufferMediumState *state =
            pcc_gc_backend4_store_buffer_medium_states;
        state != NULL;
        state = state->next
    ) {
        count += state->count == NULL ? 0 : *state->count;
    }
    pcc_gc_graph_unlock();
    return count;
}

int64_t pcc_gc_backend4_store_buffer_medium_flushes(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_store_buffer_medium_flushes_count,
        __ATOMIC_RELAXED
    );
}

int64_t pcc_gc_backend4_store_buffer_medium_flushed_entries(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_store_buffer_medium_flushed_entries_count,
        __ATOMIC_RELAXED
    );
}

int64_t pcc_gc_backend4_store_buffer_medium_full_flushes(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_store_buffer_medium_full_flushes_count,
        __ATOMIC_RELAXED
    );
}

int64_t pcc_gc_backend4_store_buffer_cross_thread_medium_flushes(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_store_buffer_cross_thread_medium_flushes_count,
        __ATOMIC_RELAXED
    );
}

int64_t pcc_gc_backend4_store_buffer_cross_thread_medium_flushed_entries(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_store_buffer_cross_thread_medium_flushed_entries_count,
        __ATOMIC_RELAXED
    );
}

int64_t pcc_gc_backend4_remembered_set_entries(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_remembered_set_entries_count,
        __ATOMIC_RELAXED
    );
}

int64_t pcc_gc_backend4_remembered_set_duplicate_skips(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_remembered_set_duplicate_skips_count,
        __ATOMIC_RELAXED
    );
}

int64_t pcc_gc_backend4_remembered_set_high_water(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_remembered_set_high_water_count,
        __ATOMIC_RELAXED
    );
}

int64_t pcc_gc_backend4_remembered_page_entries(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_remembered_page_entries_count,
        __ATOMIC_RELAXED
    );
}

int64_t pcc_gc_backend4_remembered_page_slot_entries(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_remembered_page_slot_entries_count,
        __ATOMIC_RELAXED
    );
}

int64_t pcc_gc_backend4_remembered_page_high_water(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_remembered_page_high_water_count,
        __ATOMIC_RELAXED
    );
}

int64_t pcc_gc_backend4_remembered_page_contains_slot(PyObject **slot) {
    pcc_gc_init_config();
    if (slot == NULL) return 0;
    pcc_gc_graph_lock();
    int present = pcc_gc_backend4_remembered_page_contains_slot_unlocked(slot);
    pcc_gc_graph_unlock();
    return present;
}

int64_t pcc_gc_backend4_remembered_page_clear_slot(PyObject **slot) {
    pcc_gc_init_config();
    if (slot == NULL) return 0;
    pcc_gc_graph_lock();
    int removed = pcc_gc_backend4_remembered_set_remove_slot(slot);
    pcc_gc_graph_unlock();
    return removed;
}

int64_t pcc_gc_backend4_zpage_contains_remembered_card(
    PyObject *owner,
    PyObject **slot
) {
    pcc_gc_init_config();
    if (owner == NULL || PY_IS_TAGGED_INT(owner) || slot == NULL) return 0;
    pcc_gc_graph_lock();
    int present = pcc_gc_backend4_zpage_contains_remembered_card_unlocked(
        owner,
        slot
    );
    pcc_gc_graph_unlock();
    return present;
}

int64_t pcc_gc_backend4_zpage_clear_remembered_card(
    PyObject *owner,
    PyObject **slot
) {
    pcc_gc_init_config();
    if (owner == NULL || PY_IS_TAGGED_INT(owner) || slot == NULL) return 0;
    pcc_gc_graph_lock();
    PccGcZPageNode *owner_node = pcc_gc_backend4_zpage_find_unlocked(owner);
    int32_t card = pcc_gc_backend4_zpage_card_for_node_slot_unlocked(
        owner_node,
        slot
    );
    if (card < 0) {
        pcc_gc_graph_unlock();
        return 0;
    }
    int64_t removed = 0;
    PccGcRememberedSlotNode **cur = &pcc_gc_backend4_remembered_slots;
    while (*cur != NULL) {
        PccGcRememberedSlotNode *node = *cur;
        if (
            node->owner == owner
            && pcc_gc_backend4_zpage_card_for_node_slot_unlocked(
                owner_node,
                node->slot
            ) == card
        ) {
            *cur = node->next;
            pcc_gc_backend4_remembered_page_remove_slot(node->slot);
            pcc_gc_backend4_zpage_note_remembered_slot_unlocked(
                node->owner,
                -1
            );
            pcc_gc_backend4_zpage_note_remembered_card_unlocked(
                node->owner,
                node->slot,
                -1
            );
            int64_t entries = __atomic_load_n(
                &pcc_gc_backend4_remembered_set_entries_count,
                __ATOMIC_RELAXED
            );
            if (entries > 0) {
                __atomic_sub_fetch(
                    &pcc_gc_backend4_remembered_set_entries_count,
                    1,
                    __ATOMIC_RELAXED
                );
            }
            free(node);
            removed++;
            continue;
        }
        cur = &(*cur)->next;
    }
    pcc_gc_graph_unlock();
    return removed;
}

static int pcc_gc_relocate_copy_supported_tag(int32_t tag) {
    switch (tag) {
        case PY_TYPE_INT:
        case PY_TYPE_FLOAT:
        case PY_TYPE_STR:
        case PY_TYPE_COMPLEX:
        case PY_TYPE_BYTES:
        case PY_TYPE_BYTEARRAY:
        /* CpyHandle has no pcc pointer slots (foreign ref only) — a
         * shallow relocation copy is exactly as safe as for str. */
        case PY_TYPE_CPY_HANDLE:
            return 1;
        default:
            return 0;
    }
}

static int pcc_gc_colored_relocate_copy_supported_tag(int32_t tag) {
    if (tag == PY_TYPE_PROPERTY) return 1;
    if (tag == PY_TYPE_CLASSMETHOD) return 1;
    if (tag == PY_TYPE_STATICMETHOD) return 1;
    if (tag == PY_TYPE_MEMORYVIEW) return 1;
    if (tag == PY_TYPE_FUNC) return 1;
    if (tag == PY_TYPE_ITER) return 1;
    if (tag == PY_TYPE_GEN) return 1;
    if (tag == PY_TYPE_COROUTINE) return 1;
    if (tag == PY_TYPE_CONTINUATION) return 1;
    if (tag == PY_TYPE_EXC) return 1;
    if (tag == PY_TYPE_CLASS) return 1;
    if (tag == PY_TYPE_WEAKREF) return 1;
    if (tag == PY_TYPE_THREAD) return 1;
    if (tag == PY_TYPE_LIST) return 1;
    if (tag == PY_TYPE_TUPLE) return 1;
    if (tag == PY_TYPE_DICT) return 1;
    if (tag == PY_TYPE_SET) return 1;
    if (tag == PY_TYPE_TASK) return 1;
    if (tag == PY_TYPE_VIRTUAL_THREAD) return 1;
    if (tag == PY_TYPE_VTHREAD_CHANNEL) return 1;
    if (pcc_capi_is_cext_type_tag((int64_t)tag) != 0) return 0;
    if (tag == PY_TYPE_INSTANCE || tag >= PY_TYPE_USER_CLASS_START) return 1;
    return pcc_gc_relocate_copy_supported_tag(tag);
}

static PyObject *pcc_gc_note_relocation_read_unlocked(PyObject *o) {
    if (o == NULL || PY_IS_TAGGED_INT(o)) return o;
    if (!pcc_gc_is_known_object(o)) {
        PccGcForwardNode *unknown_forwarding = pcc_gc_forwarding_find(o);
        if (unknown_forwarding != NULL && unknown_forwarding->to != NULL) {
#ifdef PCC_RUNTIME_TRIPWIRES
            int unknown_entry_valid = 1;
            int heal_despite_violation = 0;
            if (unknown_forwarding->from != o) {
                PCC_GC_MIXED_TRIPWIRE(
                    0,
                    "pcc_gc_note_relocation_read_unlocked: UNKNOWN forwarding lookup returned the wrong source"
                );
                unknown_entry_valid = 0;
            } else if (!pcc_gc_pointer_can_have_header(unknown_forwarding->to)) {
                PCC_GC_MIXED_TRIPWIRE(
                    0,
                    "pcc_gc_note_relocation_read_unlocked: UNKNOWN forwarding target cannot contain an object header"
                );
                unknown_entry_valid = 0;
            } else if (
                py_header(o)->type_tag
                    != py_header(unknown_forwarding->to)->type_tag
            ) {
                PCC_GC_MIXED_TRIPWIRE(
                    0,
                    "pcc_gc_note_relocation_read_unlocked: UNKNOWN forwarding source/target type_tag mismatch"
                );
                unknown_entry_valid = 0;
            } else if (
                (
                    py_header_flags_load(py_header(o))
                    & PY_FLAG_GC_ZPAGE_ALLOC
                ) != 0
                && pcc_gc_backend4_zpage_owns_addr_unlocked(o) == 0
            ) {
                PCC_GC_MIXED_TRIPWIRE(
                    0,
                    "pcc_gc_note_relocation_read_unlocked: UNKNOWN zpage forwarding source lost its retained span"
                );
                /* The source's own span may already be unshared/returned, so
                 * handing `o` back to the caller risks crashing before the
                 * deferred report fires.  The target passed the header and
                 * tag checks above; heal through it instead. */
                unknown_entry_valid = 0;
                heal_despite_violation = 1;
            }
            if (!unknown_entry_valid && !heal_despite_violation) {
                /* A graph-lock owner defers the fatal report to its outer
                 * unlock; never heal through an entry that failed one of
                 * the validations above, and never count a forward that did
                 * not happen. */
                return o;
            }
#endif
            pcc_gc_relocation_barrier_forwards++;
            return unknown_forwarding->to;
        }
        return o;
    }
    PyObjectHeader *h = py_header(o);
    int32_t flags = py_header_flags_load(h);
    PccGcForwardNode *forwarding = pcc_gc_forwarding_find(o);
    if (forwarding != NULL && forwarding->to != NULL) {
#ifdef PCC_RUNTIME_TRIPWIRES
        if (
            py_header(o)->type_tag != py_header(forwarding->to)->type_tag
        ) {
            PCC_GC_MIXED_TRIPWIRE(
                0,
                "pcc_gc_note_relocation_read_unlocked: forwarding source/target type_tag mismatch (stale/corrupt relocation forwarding entry)"
            );
            /* Deferred lock owners must not heal or count a forward here. */
            return o;
        }
#endif
        pcc_gc_relocation_barrier_forwards++;
        return forwarding->to;
    }
    if ((flags & PY_FLAG_GC_RELOCATION_CANDIDATE) != 0) {
        py_header_flags_and(h, ~PY_FLAG_GC_RELOCATION_CANDIDATE);
    }
    return o;
}

typedef struct {
    PyObject **from_slot;
    PyObject **to_slot;
    int32_t from_role;
    int32_t to_role;
    PccGcRetainPlan retain;
} PccGcRelocateSlotPair;

enum {
    PCC_GC_RELOCATE_RAW_MAX_BUFFERS = PCC_GC_RELOCATION_PAYLOAD_SPAN_MAX,
};

typedef struct {
    void *source;
    int64_t alloc_bytes;
    int64_t copy_bytes;
    int64_t destination_offset;
    int64_t span_bytes;
    int32_t zero_fill;
    int32_t reserved;
} PccGcRelocateRawDescriptor;

typedef struct {
    int32_t tag;
    int32_t valid;
    int64_t object_size;
    int64_t count;
    int64_t scalars[8];
    PccGcRelocateRawDescriptor descriptors[PCC_GC_RELOCATE_RAW_MAX_BUFFERS];
} PccGcRelocateRawSnapshot;

typedef struct {
    PccGcRelocateRawSnapshot snapshot;
    void *buffers[PCC_GC_RELOCATE_RAW_MAX_BUFFERS];
    PccGcZPagePayloadSpanNode *span_nodes[PCC_GC_RELOCATE_RAW_MAX_BUFFERS];
    int32_t prepared;
} PccGcRelocateRawPlan;

typedef struct {
    PccGcRelocateSlotPair *entries;
    PyObject *from;
    int64_t size;
    int64_t count;
    int64_t index;
    int32_t valid;
    PccGcRelocateRawPlan raw;
} PccGcRelocateSlotPairs;

static void pcc_gc_relocate_count_slot(
    PyObject **slot,
    int32_t role,
    void *ctx
) {
    PccGcRelocateSlotPairs *pairs = (PccGcRelocateSlotPairs *)ctx;
    (void)slot;
    (void)role;
    if (pairs == NULL || !pairs->valid) return;
    if (pairs->count == INT64_MAX) {
        pairs->valid = 0;
        return;
    }
    pairs->count++;
}

static void pcc_gc_relocate_collect_from_slot(
    PyObject **slot,
    int32_t role,
    void *ctx
) {
    PccGcRelocateSlotPairs *pairs = (PccGcRelocateSlotPairs *)ctx;
    if (
        pairs == NULL
        || !pairs->valid
        || pairs->index >= pairs->count
        || pairs->entries == NULL
    ) {
        if (pairs != NULL) pairs->valid = 0;
        return;
    }
    pairs->entries[pairs->index].from_slot = slot;
    pairs->entries[pairs->index].from_role = role;
    pairs->index++;
}

static void pcc_gc_relocate_collect_to_slot(
    PyObject **slot,
    int32_t role,
    void *ctx
) {
    PccGcRelocateSlotPairs *pairs = (PccGcRelocateSlotPairs *)ctx;
    if (
        pairs == NULL
        || !pairs->valid
        || pairs->index >= pairs->count
        || pairs->entries == NULL
    ) {
        if (pairs != NULL) pairs->valid = 0;
        return;
    }
    pairs->entries[pairs->index].to_slot = slot;
    pairs->entries[pairs->index].to_role = role;
    pairs->index++;
}

static int64_t pcc_gc_relocate_slot_count_locked(PyObject *from) {
    if (from == NULL || PY_IS_TAGGED_INT(from)) return -1;
    PccGcRelocateSlotPairs counted;
    memset(&counted, 0, sizeof(counted));
    counted.valid = 1;
    if (!py_obj_visit_slots(from, pcc_gc_relocate_count_slot, &counted)) {
        return -1;
    }
    if (!counted.valid || counted.count < 0) return -1;
    return counted.count;
}

static int pcc_gc_relocate_raw_add_descriptor(
    PccGcRelocateRawSnapshot *snapshot,
    void *source,
    int64_t alloc_bytes,
    int64_t copy_bytes,
    int64_t destination_offset,
    int64_t span_bytes,
    int32_t zero_fill
) {
    if (snapshot == NULL || !snapshot->valid) return -1;
    if (
        alloc_bytes <= 0
        || copy_bytes < 0
        || copy_bytes > alloc_bytes
        || span_bytes < 0
        || span_bytes > alloc_bytes
        || snapshot->count < 0
        || snapshot->count >= PCC_GC_RELOCATE_RAW_MAX_BUFFERS
    ) return -1;
    PccGcRelocateRawDescriptor *descriptor =
        &snapshot->descriptors[snapshot->count++];
    descriptor->source = source;
    descriptor->alloc_bytes = alloc_bytes;
    descriptor->copy_bytes = copy_bytes;
    descriptor->destination_offset = destination_offset;
    descriptor->span_bytes = span_bytes;
    descriptor->zero_fill = zero_fill;
    return 0;
}

static int pcc_gc_relocate_raw_snapshot_fill_locked(
    PyObject *from,
    int64_t size,
    PccGcRelocateRawSnapshot *snapshot
) {
    if (snapshot == NULL) return -1;
    memset(snapshot, 0, sizeof(*snapshot));
    if (from == NULL || PY_IS_TAGGED_INT(from) || size < (int64_t)sizeof(PyObjectHeader)) {
        return -1;
    }
    snapshot->valid = 1;
    snapshot->tag = py_header(from)->type_tag;
    snapshot->object_size = size;

    if (snapshot->tag == PY_TYPE_CONTINUATION) {
        if (size < (int64_t)sizeof(PyContinuationObject)) return -1;
        PyContinuationObject *src = (PyContinuationObject *)from;
        PyContinuationStackChunk *chunk = src->stack_chunk;
        snapshot->scalars[2] = src->mounted;
        if (chunk == NULL) return 0;
        if (chunk->slot_count < 0) return -1;
        if (
            chunk->slot_count > INT64_MAX / (int64_t)sizeof(PyObject *)
            || (chunk->slot_count > 0 && chunk->slots == NULL)
        ) return -1;
        snapshot->scalars[0] = chunk->root_map_slot_count;
        snapshot->scalars[1] = chunk->slot_count;
        if (
            pcc_gc_relocate_raw_add_descriptor(
                snapshot,
                chunk,
                (int64_t)sizeof(PyContinuationStackChunk),
                0,
                (int64_t)offsetof(PyContinuationObject, stack_chunk),
                0,
                1
            ) != 0
        ) return -1;
        if (
            chunk->slot_count > 0
            && pcc_gc_relocate_raw_add_descriptor(
                snapshot,
                chunk->slots,
                chunk->slot_count * (int64_t)sizeof(PyObject *),
                chunk->slot_count * (int64_t)sizeof(PyObject *),
                -1,
                chunk->slot_count * (int64_t)sizeof(PyObject *),
                1
            ) != 0
        ) return -1;
        return 0;
    }

    if (snapshot->tag == PY_TYPE_EXC) {
        if (size < (int64_t)sizeof(PyExceptionObject)) return -1;
        PyExceptionObject *src = (PyExceptionObject *)from;
        int64_t n_frames = src->n_frames;
        int64_t cap_frames = src->cap_frames;
        if (n_frames < 0 || cap_frames < 0 || n_frames > cap_frames) return -1;
        if (
            cap_frames > INT64_MAX / (int64_t)sizeof(PyFrameRecord)
            || (cap_frames > 0 && src->traceback == NULL)
        ) return -1;
        snapshot->scalars[0] = n_frames;
        snapshot->scalars[1] = cap_frames;
        if (
            cap_frames > 0
            && pcc_gc_relocate_raw_add_descriptor(
                snapshot,
                src->traceback,
                cap_frames * (int64_t)sizeof(PyFrameRecord),
                cap_frames * (int64_t)sizeof(PyFrameRecord),
                (int64_t)offsetof(PyExceptionObject, traceback),
                0,
                0
            ) != 0
        ) return -1;
        return 0;
    }

    if (snapshot->tag == PY_TYPE_CLASS) {
        if (size < (int64_t)sizeof(PyClassObject)) return -1;
        PyClassObject *src = (PyClassObject *)from;
        int64_t counts[4] = {
            src->n_bases, src->n_mro, src->n_methods, src->n_fields
        };
        void *sources[4] = {
            src->bases, src->mro, src->methods, (void *)src->field_names
        };
        int64_t widths[4] = {
            (int64_t)sizeof(PyClassObject *),
            (int64_t)sizeof(PyClassObject *),
            (int64_t)sizeof(PyClassMethod),
            (int64_t)sizeof(const char *)
        };
        int64_t offsets[4] = {
            (int64_t)offsetof(PyClassObject, bases),
            (int64_t)offsetof(PyClassObject, mro),
            (int64_t)offsetof(PyClassObject, methods),
            (int64_t)offsetof(PyClassObject, field_names)
        };
        for (int32_t i = 0; i < 4; i++) {
            if (counts[i] < 0 || counts[i] > INT64_MAX / widths[i]) return -1;
            snapshot->scalars[i] = counts[i];
            if (counts[i] == 0) continue;
            if (sources[i] == NULL) return -1;
            int64_t bytes = counts[i] * widths[i];
            if (
                pcc_gc_relocate_raw_add_descriptor(
                    snapshot,
                    sources[i],
                    bytes,
                    bytes,
                    offsets[i],
                    i < 3 ? bytes : 0,
                    0
                ) != 0
            ) return -1;
        }
        return 0;
    }

    if (snapshot->tag == PY_TYPE_DICT) {
        if (size < (int64_t)sizeof(PyDictObject)) return -1;
        PyDictObject *src = (PyDictObject *)from;
        int64_t capacity = src->capacity;
        if (
            src->size < 0
            || capacity < 0
            || src->entries_used < 0
            || src->entries_used > capacity
            || src->size > src->entries_used
        ) return -1;
        snapshot->scalars[0] = src->size;
        snapshot->scalars[1] = capacity;
        snapshot->scalars[2] = src->entries_used;
        if (capacity == 0) return 0;
        if (
            src->indices == NULL
            || src->entries == NULL
            || capacity > INT64_MAX / (int64_t)sizeof(int64_t)
            || capacity > INT64_MAX / (int64_t)sizeof(DictEntry)
        ) return -1;
        if (
            pcc_gc_relocate_raw_add_descriptor(
                snapshot,
                src->indices,
                capacity * (int64_t)sizeof(int64_t),
                capacity * (int64_t)sizeof(int64_t),
                (int64_t)offsetof(PyDictObject, indices),
                0,
                0
            ) != 0
            || pcc_gc_relocate_raw_add_descriptor(
                snapshot,
                src->entries,
                capacity * (int64_t)sizeof(DictEntry),
                capacity * (int64_t)sizeof(DictEntry),
                (int64_t)offsetof(PyDictObject, entries),
                capacity * (int64_t)sizeof(DictEntry),
                0
            ) != 0
        ) return -1;
        return 0;
    }

    if (snapshot->tag == PY_TYPE_SET) {
        if (size < (int64_t)sizeof(PySetObject)) return -1;
        PySetObject *src = (PySetObject *)from;
        int64_t capacity = src->capacity;
        if (capacity < 0) return -1;
        snapshot->scalars[0] = src->size;
        snapshot->scalars[1] = capacity;
        snapshot->scalars[2] = src->fill;
        if (capacity == 0) return 0;
        if (
            src->entries == NULL
            || capacity > INT64_MAX / (int64_t)sizeof(SetEntry)
        ) return -1;
        int64_t bytes = capacity * (int64_t)sizeof(SetEntry);
        return pcc_gc_relocate_raw_add_descriptor(
            snapshot,
            src->entries,
            bytes,
            bytes,
            (int64_t)offsetof(PySetObject, entries),
            bytes,
            0
        );
    }

    if (snapshot->tag == PY_TYPE_LIST) {
        if (size < (int64_t)sizeof(PyListObject)) return -1;
        PyListObject *src = (PyListObject *)from;
        int64_t length = src->length;
        int64_t capacity = src->capacity;
        if (length < 0 || capacity < length) return -1;
        if (capacity > INT64_MAX / (int64_t)sizeof(PyObject *)) return -1;
        snapshot->scalars[0] = length;
        snapshot->scalars[1] = capacity;
        if (capacity == 0) return 0;
        if (src->items == NULL) return -1;
        return pcc_gc_relocate_raw_add_descriptor(
            snapshot,
            src->items,
            capacity * (int64_t)sizeof(PyObject *),
            length * (int64_t)sizeof(PyObject *),
            (int64_t)offsetof(PyListObject, items),
            capacity * (int64_t)sizeof(PyObject *),
            1
        );
    }

    return 0;
}

static int pcc_gc_relocate_raw_snapshot_locked(
    PyObject *from,
    int64_t size,
    PccGcRelocateSlotPairs *pairs
) {
    if (pairs == NULL) return -1;
    memset(&pairs->raw, 0, sizeof(pairs->raw));
    return pcc_gc_relocate_raw_snapshot_fill_locked(
        from, size, &pairs->raw.snapshot
    );
}

static void pcc_gc_relocate_raw_finish(PccGcRelocateRawPlan *raw) {
    if (raw == NULL) return;
    for (int32_t i = 0; i < PCC_GC_RELOCATE_RAW_MAX_BUFFERS; i++) {
        free(raw->buffers[i]);
        raw->buffers[i] = NULL;
        free(raw->span_nodes[i]);
        raw->span_nodes[i] = NULL;
    }
    memset(raw, 0, sizeof(*raw));
}

static int pcc_gc_relocate_raw_prepare(PccGcRelocateSlotPairs *pairs) {
    if (pairs == NULL || !pairs->raw.snapshot.valid) return -1;
    PccGcRelocateRawPlan *raw = &pairs->raw;
    for (int64_t i = 0; i < raw->snapshot.count; i++) {
        PccGcRelocateRawDescriptor *descriptor =
            &raw->snapshot.descriptors[i];
        raw->buffers[i] = descriptor->zero_fill
            ? calloc(1, (size_t)descriptor->alloc_bytes)
            : malloc((size_t)descriptor->alloc_bytes);
        if (raw->buffers[i] == NULL) return -1;
        if (descriptor->span_bytes > 0) {
            raw->span_nodes[i] = (PccGcZPagePayloadSpanNode *)calloc(
                1, sizeof(PccGcZPagePayloadSpanNode)
            );
            if (raw->span_nodes[i] == NULL) return -1;
        }
    }
    raw->prepared = 1;
    return 0;
}

static int pcc_gc_relocate_raw_validate_locked(
    PyObject *from,
    PyObject *to,
    int64_t size,
    PccGcRelocateSlotPairs *pairs
) {
    if (pairs == NULL || !pairs->raw.prepared) return -1;
    PccGcRelocateRawSnapshot current;
    if (pcc_gc_relocate_raw_snapshot_fill_locked(from, size, &current) != 0) {
        return -1;
    }
    if (memcmp(&current, &pairs->raw.snapshot, sizeof(current)) != 0) return -1;
    int64_t total_span_bytes = 0;
    for (int64_t i = 0; i < current.count; i++) {
        int64_t span_bytes = current.descriptors[i].span_bytes;
        if (span_bytes > INT64_MAX - total_span_bytes) return -1;
        total_span_bytes += span_bytes;
    }
    return pcc_gc_backend4_zpage_payload_span_preflight_unlocked(
        to, total_span_bytes
    ) ? 0 : -1;
}

static void pcc_gc_relocate_raw_clear_destination(
    PyObject *to,
    int32_t tag
) {
    if (tag == PY_TYPE_CONTINUATION) {
        ((PyContinuationObject *)to)->stack_chunk = NULL;
    } else if (tag == PY_TYPE_EXC) {
        PyExceptionObject *dst = (PyExceptionObject *)to;
        dst->traceback = NULL;
        dst->n_frames = 0;
        dst->cap_frames = 0;
    } else if (tag == PY_TYPE_CLASS) {
        PyClassObject *dst = (PyClassObject *)to;
        dst->n_bases = 0;
        dst->bases = NULL;
        dst->n_mro = 0;
        dst->mro = NULL;
        dst->n_methods = 0;
        dst->methods = NULL;
        dst->n_fields = 0;
        dst->field_names = NULL;
        dst->attrs = NULL;
    } else if (tag == PY_TYPE_DICT) {
        PyDictObject *dst = (PyDictObject *)to;
        dst->size = 0;
        dst->capacity = 0;
        dst->indices = NULL;
        dst->entries = NULL;
        dst->entries_used = 0;
    } else if (tag == PY_TYPE_SET) {
        PySetObject *dst = (PySetObject *)to;
        dst->size = 0;
        dst->capacity = 0;
        dst->fill = 0;
        dst->entries = NULL;
    } else if (tag == PY_TYPE_LIST) {
        PyListObject *dst = (PyListObject *)to;
        dst->length = 0;
        dst->capacity = 0;
        dst->items = NULL;
    }
}

static int pcc_gc_relocate_raw_publish_locked(
    PyObject *to,
    PccGcRelocateSlotPairs *pairs
) {
    if (to == NULL || pairs == NULL || !pairs->raw.prepared) return -1;
    PccGcRelocateRawPlan *raw = &pairs->raw;
    PccGcRelocateRawSnapshot *snapshot = &raw->snapshot;
    void *published[PCC_GC_RELOCATE_RAW_MAX_BUFFERS] = {NULL, NULL, NULL, NULL};
    PccGcZPagePayloadSpanNode *span_head = NULL;
    PccGcZPagePayloadSpanNode *span_tail = NULL;
    int64_t span_count = 0;
    int64_t total_span_bytes = 0;
    pcc_gc_relocate_raw_clear_destination(to, snapshot->tag);
    for (int64_t i = 0; i < snapshot->count; i++) {
        PccGcRelocateRawDescriptor *descriptor = &snapshot->descriptors[i];
        void *buffer = raw->buffers[i];
        if (buffer == NULL) return -1;
        if (descriptor->copy_bytes > 0) {
            memcpy(buffer, descriptor->source, (size_t)descriptor->copy_bytes);
        }
        published[i] = buffer;
        if (descriptor->span_bytes > 0) {
            PccGcZPagePayloadSpanNode *span = raw->span_nodes[i];
            if (
                span == NULL
                || descriptor->span_bytes > INT64_MAX - total_span_bytes
            ) return -1;
            memset(span, 0, sizeof(*span));
            span->base = (uint8_t *)buffer;
            span->size_bytes = descriptor->span_bytes;
            if (span_tail != NULL) {
                span_tail->next = span;
            } else {
                span_head = span;
            }
            span_tail = span;
            span_count++;
            total_span_bytes += descriptor->span_bytes;
        }
    }
    if (
        span_count > 0
        && !pcc_gc_backend4_zpage_publish_relocation_payload_spans_unlocked(
            to, span_head, span_count, total_span_bytes
        )
    ) return -1;
    for (int64_t i = 0; i < snapshot->count; i++) {
        if (snapshot->descriptors[i].span_bytes > 0) {
            raw->span_nodes[i] = NULL;
        }
    }

    if (snapshot->tag == PY_TYPE_CONTINUATION && snapshot->count > 0) {
        PyContinuationStackChunk *chunk =
            (PyContinuationStackChunk *)published[0];
        chunk->root_map_slot_count = (int32_t)snapshot->scalars[0];
        chunk->slot_count = snapshot->scalars[1];
        chunk->slots = snapshot->count > 1
            ? (PyObject **)published[1]
            : NULL;
        ((PyContinuationObject *)to)->stack_chunk = chunk;
    } else {
        for (int64_t i = 0; i < snapshot->count; i++) {
            PccGcRelocateRawDescriptor *descriptor = &snapshot->descriptors[i];
            *(void **)((uint8_t *)to + descriptor->destination_offset) = published[i];
        }
    }

    if (snapshot->tag == PY_TYPE_EXC) {
        PyExceptionObject *dst = (PyExceptionObject *)to;
        dst->n_frames = (int32_t)snapshot->scalars[0];
        dst->cap_frames = (int32_t)snapshot->scalars[1];
    } else if (snapshot->tag == PY_TYPE_CLASS) {
        PyClassObject *dst = (PyClassObject *)to;
        dst->n_bases = (int32_t)snapshot->scalars[0];
        dst->n_mro = (int32_t)snapshot->scalars[1];
        dst->n_methods = (int32_t)snapshot->scalars[2];
        dst->n_fields = (int32_t)snapshot->scalars[3];
    } else if (snapshot->tag == PY_TYPE_DICT) {
        PyDictObject *dst = (PyDictObject *)to;
        dst->size = snapshot->scalars[0];
        dst->capacity = snapshot->scalars[1];
        dst->entries_used = snapshot->scalars[2];
    } else if (snapshot->tag == PY_TYPE_SET) {
        PySetObject *dst = (PySetObject *)to;
        dst->size = snapshot->scalars[0];
        dst->capacity = snapshot->scalars[1];
        dst->fill = snapshot->scalars[2];
    } else if (snapshot->tag == PY_TYPE_LIST) {
        PyListObject *dst = (PyListObject *)to;
        dst->length = snapshot->scalars[0];
        dst->capacity = snapshot->scalars[1];
    }

    for (int64_t i = 0; i < snapshot->count; i++) raw->buffers[i] = NULL;
    return 0;
}

static void pcc_gc_relocate_slot_pairs_finish(PccGcRelocateSlotPairs *pairs) {
    if (pairs == NULL) return;
    pcc_gc_relocate_raw_finish(&pairs->raw);
    for (int64_t i = 0; i < pairs->count; i++) {
        pcc_gc_retain_plan_finish(&pairs->entries[i].retain);
    }
    free(pairs->entries);
    memset(pairs, 0, sizeof(*pairs));
}

static int pcc_gc_relocate_slot_pairs_prepare(
    int64_t count,
    PccGcRelocateSlotPairs *pairs
) {
    if (pairs == NULL || count < 0) return -1;
    memset(pairs, 0, sizeof(*pairs));
    pairs->valid = 1;
    pairs->count = count;
    if (count > INT64_MAX / (int64_t)sizeof(*pairs->entries)) return -1;
    if (count > 0) {
        pairs->entries = (PccGcRelocateSlotPair *)calloc(
            (size_t)count, sizeof(*pairs->entries)
        );
        if (pairs->entries == NULL) return -1;
    }
    return 0;
}

static int pcc_gc_relocate_slot_pairs_validate_locked(
    PyObject *from,
    PyObject *to,
    int64_t size,
    PccGcRelocateSlotPairs *pairs
) {
    if (
        from == NULL
        || to == NULL
        || pairs == NULL
        || size < 0
        || !pairs->valid
    ) return -1;
    pairs->from = from;
    pairs->size = size;
    pairs->index = 0;
    (void)py_obj_visit_slots(from, pcc_gc_relocate_collect_from_slot, pairs);
    if (!pairs->valid || pairs->index != pairs->count) return -1;
    return 0;
}

static void pcc_gc_relocate_slot_pairs_clear_destination_owned(
    PyObject *to,
    PccGcRelocateSlotPairs *pairs
) {
    if (to == NULL || pairs == NULL || pairs->from == NULL) return;
    uintptr_t from_start = (uintptr_t)pairs->from;
    uintptr_t from_end = from_start + (uintptr_t)pairs->size;
    for (int64_t i = 0; i < pairs->count; i++) {
        uintptr_t slot_addr = (uintptr_t)pairs->entries[i].from_slot;
        if (
            pairs->entries[i].from_role == PY_OBJ_SLOT_OWNED
            && slot_addr >= from_start
            && slot_addr + sizeof(PyObject *) <= from_end
        ) {
            uintptr_t offset = slot_addr - from_start;
            *(PyObject **)((uintptr_t)to + offset) = NULL;
        }
    }
}

static int pcc_gc_relocate_copy_slots(
    PyObject *from,
    PyObject *to,
    PccGcRelocateSlotPairs *pairs
) {
    if (from == NULL || to == NULL || pairs == NULL) return -1;
    pairs->index = 0;
    if (!py_obj_visit_slots(to, pcc_gc_relocate_collect_to_slot, pairs)) {
        return -1;
    }
    if (!pairs->valid || pairs->index != pairs->count) return -1;
    for (int64_t i = 0; i < pairs->count; i++) {
        if (
            pairs->entries[i].from_role != pairs->entries[i].to_role
        ) return -1;
    }
    for (int64_t i = 0; i < pairs->count; i++) {
        PccGcRelocateSlotPair *entry = &pairs->entries[i];
        PyObject **from_slot = entry->from_slot;
        PyObject **to_slot = entry->to_slot;
        py_obj_update_slot(from_slot);
        PyObject *value = *from_slot;
        if (value == from) value = to;
        if (entry->from_role == PY_OBJ_SLOT_OWNED) {
            value = pcc_gc_retain_plan_prepare_locked(
                &entry->retain,
                value
            );
        }
        *to_slot = value;
        pcc_gc_backend4_remembered_set_retarget_slot_unlocked(
            from, to, from_slot, to_slot
        );
    }
    return 0;
}

typedef struct {
    PyObject **slot;
    PyObject *value;
} PccGcRetirePayloadRecord;

typedef struct {
    PccGcRetirePayloadRecord *records;
    int64_t count;
    int64_t index;
    int32_t valid;
} PccGcRetirePayloadCtx;

typedef struct PccGcRetirePayloadPlan {
    PccGcRetirePayloadCtx retire;
    PccGcSourceSideTablePlan *side_plan;
    void *raw_payloads[4];
    PyObject *decref_exclusion;
    struct PccGcRetirePayloadPlan *next;
} PccGcRetirePayloadPlan;

static void pcc_gc_retire_payload_count_owned_slot(
    PyObject **slot,
    int32_t role,
    void *ctx
) {
    PccGcRetirePayloadCtx *retire = (PccGcRetirePayloadCtx *)ctx;
    if (slot == NULL || retire == NULL) return;
    /* Heal every role before filtering.  The instance visitor reloads its
     * borrowed class slot and derives the owned field count from that class. */
    py_obj_update_slot(slot);
    if (role != PY_OBJ_SLOT_OWNED) return;
    if (retire->count == INT64_MAX) {
        retire->valid = 0;
        return;
    }
    retire->count++;
}

static void pcc_gc_retire_payload_collect_owned_slot(
    PyObject **slot,
    int32_t role,
    void *ctx
) {
    PccGcRetirePayloadCtx *retire = (PccGcRetirePayloadCtx *)ctx;
    if (slot == NULL || retire == NULL) return;
    py_obj_update_slot(slot);
    if (role != PY_OBJ_SLOT_OWNED) return;
    if (retire->index >= retire->count || retire->records == NULL) {
        retire->valid = 0;
        return;
    }
    retire->records[retire->index].slot = slot;
    retire->index++;
}

static int64_t pcc_gc_relocation_retire_source_payload_into_finish_impl(
    PyObject *from,
    PccGcBackend4RemapFinish *finish,
    PyObject *decref_exclusion
) {
    if (
        from == NULL
        || PY_IS_TAGGED_INT(from)
        || finish == NULL
    ) return 0;
    int32_t tag = py_header(from)->type_tag;
    if (pcc_capi_is_cext_type_tag((int64_t)tag) != 0) return 0;

    /* Callers hold the GC graph lock and keep the source forwarding edge and
     * relocation flags live through this transaction.  The first pass may
     * perform count-neutral forwarding heals.  Allocation and both visitor
     * passes still finish before any ownership or raw-payload mutation. */
    PccGcRetirePayloadPlan *plan = (
        PccGcRetirePayloadPlan *
    )calloc(1, sizeof(PccGcRetirePayloadPlan));
    if (plan == NULL) return 0;
    PccGcRetirePayloadCtx *retire = &plan->retire;
    retire->valid = 1;
    if (!py_obj_visit_slots(
        from, pcc_gc_retire_payload_count_owned_slot, retire
    )) {
        free(plan);
        return 0;
    }
    if (
        !retire->valid
        || retire->count < 0
        || retire->count > INT64_MAX / (int64_t)sizeof(*retire->records)
    ) {
        free(plan);
        return 0;
    }
    if (retire->count > 0) {
        retire->records = (PccGcRetirePayloadRecord *)calloc(
            (size_t)retire->count, sizeof(*retire->records)
        );
        if (retire->records == NULL) {
            free(plan);
            return 0;
        }
    }
    if (!py_obj_visit_slots(
        from, pcc_gc_retire_payload_collect_owned_slot, retire
    )) {
        free(retire->records);
        free(plan);
        return 0;
    }
    if (!retire->valid || retire->index != retire->count) {
        free(retire->records);
        free(plan);
        return 0;
    }
    PccGcSourceSideTablePlan *side_plan = (
        PccGcSourceSideTablePlan *
    )pcc_gc_backend4_source_side_table_plan_prepare(from);
    if (side_plan == NULL) {
        free(retire->records);
        free(plan);
        return 0;
    }
    plan->side_plan = side_plan;

    /* Save and NULL every owned slot without decref.  Saved values are raw
     * ownership tokens, kept stable by the caller-held graph lock until the
     * final decref loop. */
    for (int64_t i = 0; i < retire->count; i++) {
        PyObject **slot = retire->records[i].slot;
        retire->records[i].value = *slot;
        *slot = NULL;
    }

    /* Detach all independently allocated source payloads, retaining their raw
     * bases locally.  Side-table commit still sees allocated slot storage,
     * while any nested cleanup sees only an inert source shell. */
    void **raw_payloads = plan->raw_payloads;
    if (tag == PY_TYPE_CONTINUATION) {
        PyContinuationObject *continuation = (PyContinuationObject *)from;
        PyContinuationStackChunk *chunk = continuation->stack_chunk;
        continuation->stack_chunk = NULL;
        if (chunk != NULL) {
            PyObject **slots = chunk->slots;
            chunk->root_map_slot_count = 0;
            chunk->reserved = 0;
            chunk->slot_count = 0;
            chunk->slots = NULL;
            raw_payloads[0] = slots;
            raw_payloads[1] = chunk;
        }
    } else if (tag == PY_TYPE_EXC) {
        PyExceptionObject *exc = (PyExceptionObject *)from;
        PyFrameRecord *traceback = exc->traceback;
        exc->traceback = NULL;
        exc->n_frames = 0;
        exc->cap_frames = 0;
        raw_payloads[0] = traceback;
    } else if (tag == PY_TYPE_CLASS) {
        PyClassObject *cls = (PyClassObject *)from;
        PyClassObject **bases = cls->bases;
        PyClassObject **mro = cls->mro;
        PyClassMethod *methods = cls->methods;
        const char **field_names = cls->field_names;
        cls->n_bases = 0;
        cls->bases = NULL;
        cls->n_mro = 0;
        cls->mro = NULL;
        cls->n_methods = 0;
        cls->methods = NULL;
        cls->n_fields = 0;
        cls->field_names = NULL;
        raw_payloads[0] = bases;
        raw_payloads[1] = mro;
        raw_payloads[2] = methods;
        raw_payloads[3] = (void *)field_names;
    } else if (tag == PY_TYPE_DICT) {
        PyDictObject *dict = (PyDictObject *)from;
        int64_t *indices = dict->indices;
        DictEntry *entries = dict->entries;
        dict->size = 0;
        dict->capacity = 0;
        dict->indices = NULL;
        dict->entries = NULL;
        dict->entries_used = 0;
        raw_payloads[0] = entries;
        raw_payloads[1] = indices;
    } else if (tag == PY_TYPE_SET) {
        PySetObject *set = (PySetObject *)from;
        SetEntry *entries = set->entries;
        set->size = 0;
        set->capacity = 0;
        set->fill = 0;
        set->entries = NULL;
        raw_payloads[0] = entries;
    } else if (tag == PY_TYPE_TUPLE) {
        ((PyTupleObject *)from)->len = 0;
    } else if (tag == PY_TYPE_LIST) {
        PyListObject *list = (PyListObject *)from;
        PyObject **items = list->items;
        list->length = 0;
        list->capacity = 0;
        list->items = NULL;
        raw_payloads[0] = items;
    }
    /* pcc-Python transfers and NULLs memoryview's raw Py_buffer at forwarding
     * commit.  The C oracle has no such field.  All other supported payloads
     * are inline or borrowed and intentionally require no raw free here. */

    /* With the source inert, detach all store/remembered/card entries and
     * remove the complete zpage owner/span accounting bundle.  Commit neither
     * allocates nor decrefs; relocate-copy makes zpage removal idempotent,
     * while public direct forwarding exercises the live removal path. */
    if (!pcc_gc_backend4_source_side_table_plan_commit(side_plan)) {
        PCC_GC_DEFER_TRIPWIRE(
            0,
            "source side-table commit failed after payload detachment"
        );
        return 0;
    }
    plan->decref_exclusion = decref_exclusion;
    plan->next = (PccGcRetirePayloadPlan *)finish->payload_plans;
    finish->payload_plans = plan;
    return 1;
}

static void pcc_gc_relocation_finish_source_payloads(void *opaque_plans) {
    PccGcRetirePayloadPlan *plans = (
        PccGcRetirePayloadPlan *
    )opaque_plans;
    while (plans != NULL) {
        PccGcRetirePayloadPlan *plan = plans;
        plans = plan->next;
        plan->next = NULL;
        for (int32_t i = 0; i < 4; i++) {
            free(plan->raw_payloads[i]);
            plan->raw_payloads[i] = NULL;
        }

        /* Store-buffer values are stable in the opaque plan and are released
         * only after raw storage and owner metadata are gone. Source-slot
         * ownership follows last, so decref reentry sees an inert source. */
        pcc_gc_backend4_source_side_table_plan_finish(
            plan->side_plan,
            plan->decref_exclusion
        );
        plan->side_plan = NULL;
        for (int64_t i = 0; i < plan->retire.count; i++) {
            PyObject *value = plan->retire.records[i].value;
            if (
                value != plan->decref_exclusion
                || plan->decref_exclusion == NULL
            ) {
                py_decref(value);
            }
        }
        free(plan->retire.records);
        plan->retire.records = NULL;
        free(plan);
    }
}

static int64_t pcc_gc_relocation_retire_source_payload_into_finish(
    PyObject *from,
    PccGcBackend4RemapFinish *finish
) {
    return pcc_gc_relocation_retire_source_payload_into_finish_impl(
        from,
        finish,
        NULL
    );
}

static int64_t
pcc_gc_relocation_retire_source_payload_for_target_death_into_finish(
    PyObject *from,
    PyObject *target,
    PccGcBackend4RemapFinish *finish
) {
    if (target == NULL || PY_IS_TAGGED_INT(target)) return 0;
    return pcc_gc_relocation_retire_source_payload_into_finish_impl(
        from,
        finish,
        target
    );
}

int64_t pcc_gc_relocation_retire_source_payload(PyObject *from) {
    PccGcBackend4RemapFinish finish = {0};
    if (!pcc_gc_relocation_retire_source_payload_into_finish(from, &finish)) {
        return 0;
    }
    pcc_gc_relocation_finish_source_payloads(finish.payload_plans);
    return 1;
}

static int pcc_gc_relocate_copy_payload_prepared_locked(
    PyObject *from,
    PyObject *to,
    int64_t size,
    PccGcRelocateSlotPairs *pairs
) {
    if (from == NULL || to == NULL || pairs == NULL) return -1;
    int32_t tag = py_header(from)->type_tag;
    int result = -1;
    PyContinuationStackChunk *continuation_src_chunk = NULL;
    PyContinuationStackChunk *continuation_dst_chunk = NULL;
    pcc_gc_relocate_slot_pairs_clear_destination_owned(to, pairs);
    if (pcc_gc_relocate_raw_publish_locked(to, pairs) != 0) goto done;
    if (tag == PY_TYPE_CONTINUATION) {
        continuation_src_chunk = ((PyContinuationObject *)from)->stack_chunk;
        continuation_dst_chunk = ((PyContinuationObject *)to)->stack_chunk;
    }
    if (tag == PY_TYPE_WEAKREF) {
        PyWeakRefObject *dst = (PyWeakRefObject *)to;
        dst->prev = NULL;
        dst->next = NULL;
    }
    if (tag == PY_TYPE_THREAD) {
        PccGcThreadObject *src = (PccGcThreadObject *)from;
        PccGcThreadObject *dst = (PccGcThreadObject *)to;
        if (src->handle != NULL) goto done;
        dst->handle = NULL;
    }
    if (tag == PY_TYPE_VIRTUAL_THREAD) {
        PyVirtualThreadObject *src = (PyVirtualThreadObject *)from;
        PyVirtualThreadObject *dst = (PyVirtualThreadObject *)to;
        /* Make relocation rollback safe before inspecting source scheduler
         * state: the destination must never own copied raw queue entries. */
        dst->queued = 0;
        dst->timer_entry = NULL;
        dst->io_entry = NULL;
        dst->join_waiters = NULL;
        dst->join_wait_tail = NULL;
        dst->join_entry = NULL;
        dst->channel_arm_a = NULL;
        dst->channel_arm_b = NULL;
        dst->wait_kind = PCC_VTHREAD_WAIT_NONE;
        if (
            src->queued != 0
            || src->timer_entry != NULL
            || src->io_entry != NULL
            || src->join_waiters != NULL
            || src->join_wait_tail != NULL
            || src->join_entry != NULL
            || src->channel_arm_a != NULL
            || src->channel_arm_b != NULL
            || src->wait_kind != PCC_VTHREAD_WAIT_NONE
        ) goto done;
    }
    if (tag == PY_TYPE_VTHREAD_CHANNEL) {
        PyVThreadChannelObject *channel = (PyVThreadChannelObject *)from;
        if (channel->kind == PCC_VTHREAD_CHANNEL_KIND_CORE) {
            PyVThreadChannelCoreObject *core =
                (PyVThreadChannelCoreObject *)from;
            PyVThreadChannelCoreObject *dst_core =
                (PyVThreadChannelCoreObject *)to;
            /* Wait nodes remain owned by the source.  Clear the shallow
             * destination copy before any failure can decref/deallocate it. */
            dst_core->send_head = NULL;
            dst_core->send_tail = NULL;
            dst_core->recv_head = NULL;
            dst_core->recv_tail = NULL;
            if (
                size < (int64_t)sizeof(*core)
                || core->capacity < 0
                || core->capacity > PCC_VTHREAD_CHANNEL_MAX_CAPACITY
                || size < (int64_t)sizeof(*core)
                    + core->capacity * (int64_t)sizeof(PyObject *)
                || core->send_head != NULL
                || core->send_tail != NULL
                || core->recv_head != NULL
                || core->recv_tail != NULL
                || core->flags != 0
            ) goto done;
        } else if (
            channel->kind == PCC_VTHREAD_CHANNEL_KIND_SENDER
            || channel->kind == PCC_VTHREAD_CHANNEL_KIND_RECEIVER
        ) {
            if (size < (int64_t)sizeof(PyVThreadChannelEndpointObject)) {
                goto done;
            }
        } else {
            goto done;
        }
    }
    if (tag == PY_TYPE_INSTANCE || tag >= PY_TYPE_USER_CLASS_START) {
        PyInstanceObject *src = (PyInstanceObject *)from;
        PyClassObject *cls = (PyClassObject *)pcc_gc_load_ptr(
            from,
            (PyObject **)&src->cls
        );
        if (size < (int64_t)sizeof(PyInstanceObject)) goto done;
        if (cls == NULL || py_header((PyObject *)cls)->type_tag != PY_TYPE_CLASS) {
            goto done;
        }

        int32_t n_fields = cls->n_fields;
        if (n_fields < 0) n_fields = 0;
        int64_t n_slots = (int64_t)n_fields;
        if ((py_header((PyObject *)cls)->flags & 2) == 0) n_slots++;
        if (n_slots < 0) goto done;
        if (n_slots > (
            INT64_MAX - (int64_t)sizeof(PyInstanceObject)
        ) / (int64_t)sizeof(PyObject *)) {
            goto done;
        }
        int64_t required = (int64_t)sizeof(PyInstanceObject)
            + n_slots * (int64_t)sizeof(PyObject *);
        if (size < required) goto done;
    }
    if (tag == PY_TYPE_TUPLE) {
        PyTupleObject *src = (PyTupleObject *)from;
        int64_t length = src->len;
        if (length < 0) goto done;
        if (length > (
            INT64_MAX - (int64_t)sizeof(PyTupleObject)
        ) / (int64_t)sizeof(PyObject *)) {
            goto done;
        }
        int64_t required = (int64_t)sizeof(PyTupleObject)
            + length * (int64_t)sizeof(PyObject *);
        if (size < required) goto done;
    }
    if (pcc_gc_relocate_copy_slots(from, to, pairs) != 0) goto done;
    if (
        tag == PY_TYPE_CLASS
        && py_class_attrs_retarget((PyClassObject *)from, (PyClassObject *)to) != 0
    ) goto done;
    if (tag == PY_TYPE_WEAKREF && py_weakref_retarget(from, to) != 0) goto done;
    if (
        tag == PY_TYPE_CONTINUATION
        && continuation_src_chunk != NULL
        && ((PyContinuationObject *)from)->mounted == 0
    ) {
        pcc_gc_retarget_continuation_root_slots_unlocked(
            continuation_src_chunk->slots,
            &continuation_src_chunk->root_map_slot_count,
            continuation_dst_chunk->slots,
            &continuation_dst_chunk->root_map_slot_count
        );
    }
    result = 0;

done:
    return result;
}

/* GC3 oldification still owns its enclosing graph-lock holder.  Keep the
 * historical wrapper explicit until that later A3b slice can prepare the
 * shared slot plan before entering the generational transaction. */
static int pcc_gc_relocate_copy_payload(
    PyObject *from,
    PyObject *to,
    int64_t size
) {
    int64_t count = pcc_gc_relocate_slot_count_locked(from);
    PccGcRelocateSlotPairs pairs;
    if (pcc_gc_relocate_slot_pairs_prepare(count, &pairs) != 0) return -1;
    if (
        pcc_gc_relocate_raw_snapshot_locked(from, size, &pairs) != 0
        || pcc_gc_relocate_raw_prepare(&pairs) != 0
    ) {
        pcc_gc_relocate_slot_pairs_finish(&pairs);
        return -1;
    }
    if (
        pcc_gc_relocate_slot_pairs_validate_locked(
            from, to, size, &pairs
        ) != 0
        || pcc_gc_relocate_raw_validate_locked(
            from, to, size, &pairs
        ) != 0
    ) {
        pcc_gc_relocate_slot_pairs_finish(&pairs);
        return -1;
    }
    int result = pcc_gc_relocate_copy_payload_prepared_locked(
        from, to, size, &pairs
    );
    pcc_gc_relocate_slot_pairs_finish(&pairs);
    return result;
}

static int pcc_gc_backend_uses_forwarding(void) {
    return pcc_gc_backend_kind_uses_forwarding(pcc_gc_selected_backend);
}

static int pcc_gc_backend4_evacuation_policy_accept(int64_t size) {
    if (size <= 0) return 0;
    if (size <= PCC_GC_BACKEND4_SMALL_PAGE_LIMIT) return 1;
    if (size <= PCC_GC_BACKEND4_MEDIUM_PAGE_LIMIT) return 1;
    return 0;
}

static int pcc_gc_backend4_evacuation_policy_defer_large(int64_t size) {
    return size > PCC_GC_BACKEND4_MEDIUM_PAGE_LIMIT ? 1 : 0;
}

static int pcc_gc_backend4_large_page_evacuation_policy_accept(
    PccGcZPage *page,
    int64_t size
) {
    if (page == NULL) return 0;
    if (size <= PCC_GC_BACKEND4_MEDIUM_PAGE_LIMIT) return 0;
    if (page->page_class != 2) return 0;
    return page->capacity_bytes > page->used_bytes ? 1 : 0;
}

static void pcc_gc_backend4_note_page_candidate(
    int64_t size,
    PccGcZPage *page
) {
    if (size <= 0) return;
    __atomic_add_fetch(
        &pcc_gc_backend4_evacuation_candidate_bytes_count,
        size,
        __ATOMIC_RELAXED
    );
    if (size <= PCC_GC_BACKEND4_SMALL_PAGE_LIMIT) {
        __atomic_add_fetch(
            &pcc_gc_backend4_small_page_candidates,
            1,
            __ATOMIC_RELAXED
        );
        __atomic_add_fetch(
            &pcc_gc_backend4_small_page_candidate_bytes_count,
            size,
            __ATOMIC_RELAXED
        );
    } else if (size <= PCC_GC_BACKEND4_MEDIUM_PAGE_LIMIT) {
        __atomic_add_fetch(
            &pcc_gc_backend4_medium_page_candidates,
            1,
            __ATOMIC_RELAXED
        );
        __atomic_add_fetch(
            &pcc_gc_backend4_medium_page_candidate_bytes_count,
            size,
            __ATOMIC_RELAXED
        );
    }
    if (page == NULL) return;
    int64_t page_bytes = page->used_bytes;
    if (page_bytes <= 0) return;
    __atomic_add_fetch(
        &pcc_gc_backend4_evacuation_candidate_zpage_bytes_count,
        page_bytes,
        __ATOMIC_RELAXED
    );
    if (page->page_class == 0) {
        __atomic_add_fetch(
            &pcc_gc_backend4_small_page_candidate_zpage_bytes_count,
            page_bytes,
            __ATOMIC_RELAXED
        );
    } else if (page->page_class == 1) {
        __atomic_add_fetch(
            &pcc_gc_backend4_medium_page_candidate_zpage_bytes_count,
            page_bytes,
            __ATOMIC_RELAXED
        );
    }
}

static int pcc_gc_backend4_relocation_set_contains_page_unlocked(
    PccGcZPage *page
) {
    if (page == NULL) return 0;
    for (
        PccGcRelocationNode *n = pcc_gc_relocation_set;
        n != NULL;
        n = n->next
    ) {
        PccGcZPageNode *zp = pcc_gc_backend4_zpage_find_unlocked(n->obj);
        if (zp != NULL && zp->page == page) return 1;
    }
    return 0;
}

static PccGcZPageEvacuationNode *
pcc_gc_backend4_evacuation_page_find_unlocked(PccGcZPage *page) {
    if (page == NULL) return NULL;
    for (
        PccGcZPageEvacuationNode *n = pcc_gc_backend4_evacuation_pages;
        n != NULL;
        n = n->next
    ) {
        if (n->page == page) return n;
    }
    return NULL;
}

static int pcc_gc_backend4_evacuation_page_add_unlocked(PccGcZPage *page) {
    if (page == NULL) return 0;
    if (page->evacuation_selected) return 0;
    pcc_gc_backend4_active_page_clear_unlocked(page);
    PccGcZPageEvacuationNode *n = (PccGcZPageEvacuationNode *)calloc(
        1, sizeof(PccGcZPageEvacuationNode)
    );
    if (n == NULL) return 0;
    n->page = page;
    n->next = pcc_gc_backend4_evacuation_pages;
    pcc_gc_backend4_evacuation_pages = n;
    page->evacuation_selected = 1;
    pcc_gc_backend4_reseed_page_revision++;
    return 1;
}

static int pcc_gc_backend4_evacuation_page_add_preallocated_unlocked(
    PccGcZPage *page,
    PccGcZPageEvacuationNode **available
) {
    if (page == NULL || available == NULL || *available == NULL) return 0;
    if (page->evacuation_selected) return 0;
    pcc_gc_backend4_active_page_clear_unlocked(page);
    PccGcZPageEvacuationNode *n = *available;
    *available = n->next;
    n->page = page;
    n->next = pcc_gc_backend4_evacuation_pages;
    pcc_gc_backend4_evacuation_pages = n;
    page->evacuation_selected = 1;
    pcc_gc_backend4_reseed_page_revision++;
    return 1;
}

static int64_t pcc_gc_relocation_selection_plan_init(
    PccGcRelocationSelectionPlan *plan,
    int64_t capacity
) {
    if (plan == NULL || capacity <= 0) return 0;
    plan->relocation_nodes = NULL;
    plan->page_nodes = NULL;
    int64_t allocated = 0;
    while (allocated < capacity) {
        PccGcRelocationNode *relocation = (
            PccGcRelocationNode *
        )calloc(1, sizeof(PccGcRelocationNode));
        if (relocation == NULL) break;
        PccGcZPageEvacuationNode *page = (
            PccGcZPageEvacuationNode *
        )calloc(1, sizeof(PccGcZPageEvacuationNode));
        if (page == NULL) {
            free(relocation);
            break;
        }
        relocation->next = plan->relocation_nodes;
        plan->relocation_nodes = relocation;
        page->next = plan->page_nodes;
        plan->page_nodes = page;
        allocated++;
    }
    return allocated;
}

static int64_t pcc_gc_relocation_page_selection_plan_init(
    PccGcRelocationSelectionPlan *plan,
    int64_t capacity
) {
    if (plan == NULL || capacity <= 0) return 0;
    plan->relocation_nodes = NULL;
    plan->page_nodes = (
        PccGcZPageEvacuationNode *
    )calloc(1, sizeof(PccGcZPageEvacuationNode));
    if (plan->page_nodes == NULL) return 0;
    int64_t allocated = 0;
    while (allocated < capacity) {
        PccGcRelocationNode *relocation = (
            PccGcRelocationNode *
        )calloc(1, sizeof(PccGcRelocationNode));
        if (relocation == NULL) break;
        relocation->next = plan->relocation_nodes;
        plan->relocation_nodes = relocation;
        allocated++;
    }
    return allocated;
}

static void pcc_gc_relocation_selection_plan_finish(
    PccGcRelocationSelectionPlan *plan
) {
    if (plan == NULL) return;
    while (plan->relocation_nodes != NULL) {
        PccGcRelocationNode *next = plan->relocation_nodes->next;
        free(plan->relocation_nodes);
        plan->relocation_nodes = next;
    }
    while (plan->page_nodes != NULL) {
        PccGcZPageEvacuationNode *next = plan->page_nodes->next;
        free(plan->page_nodes);
        plan->page_nodes = next;
    }
}

static PccGcZPageEvacuationNode *
pcc_gc_backend4_evacuation_page_detach_unlocked(PccGcZPage *page) {
    if (page == NULL) return NULL;
    PccGcZPageEvacuationNode **cur = &pcc_gc_backend4_evacuation_pages;
    while (*cur != NULL) {
        if ((*cur)->page == page) {
            PccGcZPageEvacuationNode *dead = *cur;
            *cur = dead->next;
            if (pcc_gc_backend4_reseed_page_count_cursor == dead) {
                pcc_gc_backend4_reseed_page_count_cursor = dead->next;
            }
            dead->next = NULL;
            page->evacuation_selected = 0;
            pcc_gc_backend4_reseed_page_revision++;
            return dead;
        }
        cur = &(*cur)->next;
    }
    return NULL;
}

static void pcc_gc_backend4_evacuation_page_finish_detached(
    PccGcZPageEvacuationNode *head
) {
    while (head != NULL) {
        PccGcZPageEvacuationNode *next = head->next;
        free(head);
        head = next;
    }
}

static int64_t pcc_gc_backend4_evacuation_page_nodes_prepare(
    PccGcZPageEvacuationNode **head,
    int64_t capacity
) {
    if (head == NULL || capacity <= 0) return 0;
    int64_t allocated = 0;
    while (allocated < capacity) {
        int64_t allocation_limit = __atomic_load_n(
            &pcc_gc_backend4_reseed_plan_probe_allocation_limit,
            __ATOMIC_ACQUIRE
        );
        if (allocation_limit == 0) break;
        PccGcZPageEvacuationNode *node = (
            PccGcZPageEvacuationNode *
        )calloc(1, sizeof(PccGcZPageEvacuationNode));
        if (node == NULL) break;
        node->next = *head;
        *head = node;
        allocated++;
        if (allocation_limit > 0) {
            __atomic_sub_fetch(
                &pcc_gc_backend4_reseed_plan_probe_allocation_limit,
                1,
                __ATOMIC_RELEASE
            );
        }
    }
    return allocated;
}

void pcc_gc_backend4_reseed_plan_probe_config(
    int64_t pause,
    int64_t allocation_limit
) {
    __atomic_store_n(
        &pcc_gc_backend4_reseed_plan_probe_allocation_limit,
        allocation_limit,
        __ATOMIC_RELEASE
    );
    __atomic_store_n(
        &pcc_gc_backend4_reseed_plan_probe_pause,
        pause != 0,
        __ATOMIC_RELEASE
    );
}

int64_t pcc_gc_backend4_reseed_plan_probe_state(void) {
    return __atomic_load_n(
        &pcc_gc_backend4_reseed_plan_probe_state_value,
        __ATOMIC_ACQUIRE
    );
}

static void pcc_gc_backend4_reseed_plan_probe_wait(int64_t phase) {
    if (
        (__atomic_load_n(
            &pcc_gc_backend4_reseed_plan_probe_pause,
            __ATOMIC_ACQUIRE
        ) & phase) == 0
    ) return;
    __atomic_store_n(
        &pcc_gc_backend4_reseed_plan_probe_state_value,
        1,
        __ATOMIC_RELEASE
    );
    while (
        (__atomic_load_n(
            &pcc_gc_backend4_reseed_plan_probe_pause,
            __ATOMIC_ACQUIRE
        ) & phase) != 0
    ) {
        pcc_thread_safepoint();
    }
    __atomic_store_n(
        &pcc_gc_backend4_reseed_plan_probe_state_value,
        0,
        __ATOMIC_RELEASE
    );
}

static int pcc_gc_backend4_zpage_candidate_snapshot(
    PccGcZPageNode *zp,
    PccGcZPageEvacuationCandidate *candidate,
    int allow_large_pages
) {
    if (zp == NULL || candidate == NULL) return 0;
    PyObject *o = zp->owner;
    if (o == NULL || PY_IS_TAGGED_INT(o)) return 0;
    PccGcZPage *page = zp->page;
    if (page == NULL) return 0;
    if (page->pending_alloc_count > 0) return 0;
    PyObjectHeader *h = py_header(o);
    int32_t flags = py_header_flags_load(h);
    if (
        (flags & (
            PY_FLAG_GC_PINNED | PY_FLAG_GC_RELOCATION_CANDIDATE
        )) != 0
    ) return 0;
    if ((flags & PY_FLAG_GC_RELOCATION_TARGET) != 0) return 0;
    if ((flags & PY_FLAG_GC_DEALLOCATING) != 0) return 0;
    if ((flags & PY_FLAG_GC_FRESH_ALLOC) != 0) {
        /* The relocation-set add refuses FRESH_ALLOC unconditionally (a
         * half-initialized object must never relocate), so a fresh owner
         * can never become a candidate: without this early test the
         * selector picked the page, walked every object on it, and the add
         * refused them all — select() returned 0 with no indication why
         * (GC-P1-BACKEND4-FRESH-ALLOC-FILTER-DISAGREEMENT).  Counted so a
         * refused scan is diagnosable; the widened FRESH tag list
         * (2026-08-27) makes this window much more common. */
        __atomic_add_fetch(
            &pcc_gc_backend4_candidate_fresh_skips, 1, __ATOMIC_RELAXED
        );
        return 0;
    }
    if (!pcc_gc_colored_relocate_copy_supported_tag(h->type_tag)) return 0;
    if (
        h->type_tag == PY_TYPE_THREAD
        && ((PccGcThreadObject *)o)->handle != NULL
    ) return 0;
    if (h->type_tag == PY_TYPE_VIRTUAL_THREAD) {
        PyVirtualThreadObject *thread = (PyVirtualThreadObject *)o;
        if (
            thread->queued != 0
            || thread->timer_entry != NULL
            || thread->io_entry != NULL
            || thread->join_waiters != NULL
            || thread->join_wait_tail != NULL
            || thread->join_entry != NULL
            || thread->channel_arm_a != NULL
            || thread->channel_arm_b != NULL
            || thread->wait_kind != PCC_VTHREAD_WAIT_NONE
        ) return 0;
    }
    if (h->type_tag == PY_TYPE_VTHREAD_CHANNEL) {
        PyVThreadChannelObject *channel = (PyVThreadChannelObject *)o;
        if (channel->kind == PCC_VTHREAD_CHANNEL_KIND_CORE) {
            PyVThreadChannelCoreObject *core =
                (PyVThreadChannelCoreObject *)o;
            if (
                core->send_head != NULL
                || core->send_tail != NULL
                || core->recv_head != NULL
                || core->recv_tail != NULL
                || core->flags != 0
            ) return 0;
        } else if (
            channel->kind != PCC_VTHREAD_CHANNEL_KIND_SENDER
            && channel->kind != PCC_VTHREAD_CHANNEL_KIND_RECEIVER
        ) {
            return 0;
        }
    }
    int64_t owner_size = zp->size_bytes;
    if (owner_size <= 0) return 0;
    int large_page_accepted = 0;
    if (!pcc_gc_backend4_evacuation_policy_accept(owner_size)) {
        large_page_accepted =
            allow_large_pages
            && pcc_gc_backend4_large_page_evacuation_policy_accept(
                page,
                owner_size
            );
    }
    if (
        !pcc_gc_backend4_evacuation_policy_accept(owner_size)
        && !large_page_accepted
    ) {
        if (
            pcc_gc_backend4_evacuation_policy_defer_large(owner_size)
            && (flags & PY_FLAG_GC_LARGE_DEFERRED) == 0
        ) {
            py_header_flags_or(h, PY_FLAG_GC_LARGE_DEFERRED);
            __atomic_add_fetch(
                &pcc_gc_backend4_large_object_defers,
                1,
                __ATOMIC_RELAXED
            );
            __atomic_add_fetch(
                &pcc_gc_backend4_large_object_deferred_bytes_count,
                owner_size,
                __ATOMIC_RELAXED
            );
        }
        return 0;
    }
    int64_t score = page->capacity_bytes - page->used_bytes;
    if (score < 0) score = 0;
    score += page->remembered_slots;
    score += page->remembered_cards;
    score += zp->remembered_slots;
    if ((flags & PY_FLAG_GC_OLD) != 0) score++;
    if (score <= 0) return 0;
    candidate->mapping = zp;
    candidate->page = page;
    candidate->owner = o;
    candidate->used_bytes = owner_size;
    candidate->capacity_bytes = page->capacity_bytes;
    candidate->fragmentation_bytes = (
        page->capacity_bytes > page->used_bytes
        ? page->capacity_bytes - page->used_bytes
        : 0
    );
    candidate->remembered_slots = page->remembered_slots;
    candidate->remembered_cards = page->remembered_cards;
    candidate->score = score;
    candidate->owner_flags = flags;
    return 1;
}

static int pcc_gc_backend4_select_one_page_object_unlocked(
    PccGcZPageNode *zp,
    int allow_large_pages,
    PccGcRelocationSelectionPlan *plan
) {
    PccGcZPageEvacuationCandidate candidate;
    if (
        !pcc_gc_backend4_zpage_candidate_snapshot(
            zp, &candidate, allow_large_pages
        )
    ) return 0;
    int count_page = !candidate.page->evacuation_selected;
    int added = plan == NULL
        ? pcc_gc_relocation_set_add(candidate.owner)
        : pcc_gc_relocation_set_add_preallocated(
            candidate.owner,
            &plan->relocation_nodes
        );
    if (!added) {
        /* The snapshot said yes and the add said no: the two filters
         * disagree.  Post-FRESH-fix this counts only residual causes
         * (forwarding races, concurrent owners); a nonzero value is the
         * diagnosable form of the silent select()==0. */
        __atomic_add_fetch(
            &pcc_gc_backend4_relocation_add_refusals, 1, __ATOMIC_RELAXED
        );
        return 0;
    }
    __atomic_add_fetch(
        &pcc_gc_backend4_evacuation_candidates,
        1,
        __ATOMIC_RELAXED
    );
    if (count_page) {
        count_page = plan == NULL
            ? pcc_gc_backend4_evacuation_page_add_unlocked(candidate.page)
            : pcc_gc_backend4_evacuation_page_add_preallocated_unlocked(
                candidate.page,
                &plan->page_nodes
            );
    }
    pcc_gc_backend4_note_page_candidate(
        candidate.used_bytes,
        count_page ? candidate.page : NULL
    );
    return 1;
}

static void pcc_gc_backend4_selector_page_scan_reset_unlocked(void) {
    pcc_gc_backend4_selector_page_cursor = NULL;
    pcc_gc_backend4_selector_page_seed = NULL;
    pcc_gc_backend4_selector_page = NULL;
    pcc_gc_backend4_selector_page_owner = 0;
    pcc_gc_backend4_selector_page_allow_large = 0;
    pcc_gc_backend4_selector_page_seed_pending = 0;
}

static int pcc_gc_backend4_selector_page_scan_begin_unlocked(
    int64_t owner_thread_id,
    PccGcZPageEvacuationCandidate *seed,
    int allow_large_pages
) {
    if (
        owner_thread_id <= 0
        || seed == NULL
        || seed->mapping == NULL
        || seed->page == NULL
        || pcc_gc_backend4_selector_page_owner != 0
    ) return 0;
    pcc_gc_backend4_selector_page_owner = owner_thread_id;
    pcc_gc_backend4_selector_page = seed->page;
    pcc_gc_backend4_selector_page_seed = seed->mapping;
    pcc_gc_backend4_selector_page_cursor = seed->page->object_head;
    pcc_gc_backend4_selector_page_allow_large = allow_large_pages;
    pcc_gc_backend4_selector_page_seed_pending = 1;
    return 1;
}

/* Select from one page while charging every visited mapping.  A tenure stops
 * after 16 visits even when all entries are stale or ineligible. */
static int64_t pcc_gc_backend4_select_page_objects_batch_unlocked(
    int64_t owner_thread_id,
    PccGcZPageEvacuationCandidate *seed,
    int64_t budget,
    int allow_large_pages,
    PccGcRelocationSelectionPlan *plan,
    int64_t *examined_out,
    int *complete_out
) {
    if (examined_out == NULL || complete_out == NULL || budget <= 0) {
        return -1;
    }
    *examined_out = 0;
    *complete_out = 0;
    if (pcc_gc_backend4_selector_page_owner == 0) {
        if (!pcc_gc_backend4_selector_page_scan_begin_unlocked(
                owner_thread_id, seed, allow_large_pages
            )) {
            *complete_out = 1;
            return -1;
        }
    } else if (
        pcc_gc_backend4_selector_page_owner != owner_thread_id
        || pcc_gc_backend4_selector_page_allow_large != allow_large_pages
        || (
            seed != NULL
            && seed->page != pcc_gc_backend4_selector_page
        )
    ) {
        *complete_out = 1;
        return -1;
    }

    int64_t examined = 0;
    int64_t selected = 0;
    if (
        pcc_gc_backend4_selector_page_seed_pending
        && examined < PCC_GC_SAFEPOINT_BATCH
        && selected < budget
    ) {
        PccGcZPageNode *seed_node = pcc_gc_backend4_selector_page_seed;
        pcc_gc_backend4_selector_page_seed_pending = 0;
        examined++;
        if (seed_node != NULL) {
            selected += pcc_gc_backend4_select_one_page_object_unlocked(
                seed_node, allow_large_pages, plan
            );
        }
    }
    while (
        pcc_gc_backend4_selector_page_cursor != NULL
        && examined < PCC_GC_SAFEPOINT_BATCH
        && selected < budget
    ) {
        PccGcZPageNode *zp = pcc_gc_backend4_selector_page_cursor;
        pcc_gc_backend4_selector_page_cursor = zp->page_next;
        examined++;
        if (zp == pcc_gc_backend4_selector_page_seed) continue;
        selected += pcc_gc_backend4_select_one_page_object_unlocked(
            zp, allow_large_pages, plan
        );
    }
    *examined_out = examined;
    if (
        selected >= budget
        || (
            !pcc_gc_backend4_selector_page_seed_pending
            && pcc_gc_backend4_selector_page_cursor == NULL
        )
    ) {
        pcc_gc_backend4_selector_page_scan_reset_unlocked();
        *complete_out = 1;
    }
    return selected;
}

static void pcc_gc_backend4_selector_scan_reset_unlocked(void) {
    pcc_gc_backend4_selector_scan_cursor = NULL;
    pcc_gc_backend4_selector_scan_best = NULL;
    pcc_gc_backend4_selector_scan_page = NULL;
    pcc_gc_backend4_selector_scan_owner = 0;
    pcc_gc_backend4_selector_scan_best_score = -1;
    pcc_gc_backend4_selector_scan_allow_large = 0;
    pcc_gc_backend4_selector_scan_require_unselected = 0;
    pcc_gc_backend4_selector_scan_restart = 0;
}

/* Scan at most PCC_GC_SAFEPOINT_BATCH zpage nodes in one graph-lock tenure.
 * The caller repeats this helper, unlocking and polling between chunks. */
static int pcc_gc_backend4_best_relocation_page_batch_unlocked(
    int64_t owner_thread_id,
    PccGcZPage *page_token,
    int require_unselected_page,
    int allow_large_pages,
    PccGcZPageEvacuationCandidate *best,
    int64_t *examined_out,
    int *complete_out
) {
    if (best == NULL || examined_out == NULL || complete_out == NULL) {
        return -1;
    }
    *examined_out = 0;
    *complete_out = 0;
    if (owner_thread_id <= 0) {
        *complete_out = 1;
        return -1;
    }
    if (pcc_gc_backend4_selector_scan_owner == 0) {
        pcc_gc_backend4_selector_scan_owner = owner_thread_id;
        pcc_gc_backend4_selector_scan_page = page_token;
        pcc_gc_backend4_selector_scan_allow_large = allow_large_pages;
        pcc_gc_backend4_selector_scan_require_unselected = (
            require_unselected_page
        );
        pcc_gc_backend4_selector_scan_cursor = pcc_gc_backend4_zpages;
        pcc_gc_backend4_selector_scan_best = NULL;
        pcc_gc_backend4_selector_scan_best_score = -1;
        pcc_gc_backend4_selector_scan_restart = 0;
    } else if (
        pcc_gc_backend4_selector_scan_owner != owner_thread_id
        || pcc_gc_backend4_selector_scan_page != page_token
        || pcc_gc_backend4_selector_scan_allow_large != allow_large_pages
        || pcc_gc_backend4_selector_scan_require_unselected
            != require_unselected_page
    ) {
        *complete_out = 1;
        return -1;
    }
    if (pcc_gc_backend4_selector_scan_restart) {
        pcc_gc_backend4_selector_scan_cursor = pcc_gc_backend4_zpages;
        pcc_gc_backend4_selector_scan_best = NULL;
        pcc_gc_backend4_selector_scan_best_score = -1;
        pcc_gc_backend4_selector_scan_restart = 0;
    }

    int64_t examined = 0;
    while (
        pcc_gc_backend4_selector_scan_cursor != NULL
        && examined < PCC_GC_SAFEPOINT_BATCH
    ) {
        PccGcZPageNode *zp = pcc_gc_backend4_selector_scan_cursor;
        pcc_gc_backend4_selector_scan_cursor = zp->next;
        examined++;
        if (page_token != NULL && zp->page != page_token) continue;
        PccGcZPageEvacuationCandidate candidate;
        if (
            !pcc_gc_backend4_zpage_candidate_snapshot(
                zp, &candidate, allow_large_pages
            )
        ) {
            continue;
        }
        if (
            require_unselected_page && candidate.page->evacuation_selected
        ) {
            continue;
        }
        if (candidate.score > pcc_gc_backend4_selector_scan_best_score) {
            pcc_gc_backend4_selector_scan_best = zp;
            pcc_gc_backend4_selector_scan_best_score = candidate.score;
        }
    }
    *examined_out = examined;
    if (pcc_gc_backend4_selector_scan_cursor != NULL) return 0;

    int has_best = 0;
    PccGcZPageNode *best_node = pcc_gc_backend4_selector_scan_best;
    if (
        best_node != NULL
        && (page_token == NULL || best_node->page == page_token)
        && pcc_gc_backend4_zpage_candidate_snapshot(
            best_node, best, allow_large_pages
        )
        && (
            !require_unselected_page || !best->page->evacuation_selected
        )
    ) {
        has_best = 1;
    }
    pcc_gc_backend4_selector_scan_reset_unlocked();
    *complete_out = 1;
    return has_best;
}

int64_t pcc_gc_backend4_select_relocation_pages(int64_t page_budget) {
    if (pcc_gc_selected_backend != PCC_GC_KIND_COLORED_RELOCATING) return 0;
    if (page_budget <= 0) return 0;
    int64_t owner_thread_id = pcc_current_thread_id();
    if (owner_thread_id <= 0) return 0;
    int64_t selected = 0;
    int64_t pages = 0;
    while (pages < page_budget) {
        PccGcZPage *page_token = NULL;
        int64_t object_budget = 0;
        PccGcZPageEvacuationCandidate preflight;
        int preflight_complete = 0;
        while (!preflight_complete) {
            int64_t examined = 0;
            pcc_gc_graph_lock();
            int has_preflight = (
                pcc_gc_backend4_best_relocation_page_batch_unlocked(
                    owner_thread_id,
                    NULL,
                    1,
                    1,
                    &preflight,
                    &examined,
                    &preflight_complete
                )
            );
            if (preflight_complete && has_preflight > 0) {
                page_token = preflight.page;
                object_budget = page_token->object_count;
                if (object_budget < 1) object_budget = 1;
            }
            pcc_gc_graph_unlock();
            if (!preflight_complete && examined > 0) {
                pcc_thread_safepoint();
            }
        }
        if (page_token == NULL || object_budget <= 0) break;

        PccGcRelocationSelectionPlan plan = {0};
        int64_t allocated = pcc_gc_relocation_page_selection_plan_init(
            &plan,
            object_budget
        );
        if (allocated != object_budget) {
            pcc_gc_relocation_selection_plan_finish(&plan);
            break;
        }

        int64_t page_selected = 0;
        int retry_preflight = 0;
        while (page_selected < object_budget) {
            int64_t batch_budget = object_budget - page_selected;
            if (batch_budget > PCC_GC_SAFEPOINT_BATCH) {
                batch_budget = PCC_GC_SAFEPOINT_BATCH;
            }
            PccGcZPageEvacuationCandidate current;
            int64_t added = 0;
            int current_complete = 0;
            int page_commit_complete = 1;
            while (!current_complete) {
                int64_t examined = 0;
                pcc_gc_graph_lock();
                int has_current = (
                    pcc_gc_backend4_best_relocation_page_batch_unlocked(
                        owner_thread_id,
                        page_token,
                        0,
                        1,
                        &current,
                        &examined,
                        &current_complete
                    )
                );
                if (
                    current_complete
                    && has_current > 0
                    && page_selected == 0
                    && page_token->object_count > object_budget
                ) {
                    retry_preflight = 1;
                } else if (current_complete && has_current > 0) {
                    page_commit_complete = (
                        !pcc_gc_backend4_selector_page_scan_begin_unlocked(
                            owner_thread_id, &current, 1
                        )
                    );
                }
                pcc_gc_graph_unlock();
                if (
                    (!current_complete || !page_commit_complete)
                    && examined > 0
                ) {
                    pcc_thread_safepoint();
                }
            }
            while (!retry_preflight && !page_commit_complete) {
                int64_t page_examined = 0;
                pcc_gc_graph_lock();
                int64_t page_added = (
                    pcc_gc_backend4_select_page_objects_batch_unlocked(
                        owner_thread_id,
                        NULL,
                        batch_budget - added,
                        1,
                        &plan,
                        &page_examined,
                        &page_commit_complete
                    )
                );
                pcc_gc_graph_unlock();
                if (page_added < 0) {
                    added = page_added;
                    break;
                }
                added += page_added;
                if (!page_commit_complete && page_examined > 0) {
                    pcc_thread_safepoint();
                }
            }
            if (retry_preflight || added <= 0) break;
            page_selected += added;
            selected += added;
            pcc_thread_safepoint();
        }
        pcc_gc_relocation_selection_plan_finish(&plan);
        if (retry_preflight) continue;
        if (page_selected <= 0) break;
        pages++;
    }
    return selected;
}

int64_t pcc_gc_select_relocation_set(int64_t budget) {
    pcc_gc_init_config();
    if (pcc_gc_selected_backend != PCC_GC_KIND_COLORED_RELOCATING) return 0;
    if (budget <= 0) return 0;
    int64_t owner_thread_id = pcc_current_thread_id();
    if (owner_thread_id <= 0) return 0;
    int64_t selected = 0;
    while (selected < budget) {
        int64_t batch_budget = budget - selected;
        if (batch_budget > PCC_GC_SAFEPOINT_BATCH) {
            batch_budget = PCC_GC_SAFEPOINT_BATCH;
        }
        PccGcRelocationSelectionPlan plan = {0};
        batch_budget = pcc_gc_relocation_selection_plan_init(
            &plan,
            batch_budget
        );
        if (batch_budget <= 0) break;
        int64_t added = 0;
        int scan_complete = 0;
        int page_commit_complete = 1;
        while (!scan_complete) {
            int64_t examined = 0;
            PccGcZPageEvacuationCandidate best;
            pcc_gc_graph_lock();
            int has_best = pcc_gc_backend4_best_relocation_page_batch_unlocked(
                owner_thread_id,
                NULL,
                0,
                0,
                &best,
                &examined,
                &scan_complete
            );
            if (scan_complete && has_best > 0) {
                page_commit_complete = (
                    !pcc_gc_backend4_selector_page_scan_begin_unlocked(
                        owner_thread_id, &best, 0
                    )
                );
            }
            pcc_gc_graph_unlock();
            if (
                (!scan_complete || !page_commit_complete)
                && examined > 0
            ) {
                pcc_thread_safepoint();
            }
        }
        while (!page_commit_complete) {
            int64_t page_examined = 0;
            pcc_gc_graph_lock();
            int64_t page_added = (
                pcc_gc_backend4_select_page_objects_batch_unlocked(
                    owner_thread_id,
                    NULL,
                    batch_budget - added,
                    0,
                    &plan,
                    &page_examined,
                    &page_commit_complete
                )
            );
            pcc_gc_graph_unlock();
            if (page_added < 0) {
                added = page_added;
                break;
            }
            added += page_added;
            if (!page_commit_complete && page_examined > 0) {
                pcc_thread_safepoint();
            }
        }
        pcc_gc_relocation_selection_plan_finish(&plan);
        if (added <= 0) break;
        selected += added;
        pcc_thread_safepoint();
    }
    return selected;
}

static int64_t pcc_gc_known_object_size_unlocked(PyObject *obj) {
    if (obj == NULL || PY_IS_TAGGED_INT(obj)) return 0;
    PccGcObjectNode *indexed =
        (PccGcObjectNode *)pcc_gc_object_index_find(obj);
    if (indexed != NULL && !pcc_gc_object_node_is_freeing(indexed)) {
        return indexed->size;
    }
    for (PccGcObjectNode *n = pcc_gc_objects; n != NULL; n = n->next) {
        if (n->obj == obj && !pcc_gc_object_node_is_freeing(n)) {
            return n->size;
        }
    }
    return 0;
}

static int64_t pcc_gc_known_object_size(PyObject *obj) {
    pcc_gc_graph_lock();
    int64_t size = pcc_gc_known_object_size_unlocked(obj);
    pcc_gc_graph_unlock();
    return size;
}

static int pcc_gc_relocate_copy_snapshot_unlocked(
    PyObject *from,
    int64_t size,
    int32_t *tag_out,
    int32_t *flags_out
) {
    if (pcc_gc_selected_backend != PCC_GC_KIND_COLORED_RELOCATING) {
        return 0;
    }
    if (pcc_gc_backend4_reseed_commit_owner != 0) return 0;
    if (from == NULL || PY_IS_TAGGED_INT(from)) return 0;
    if (size < (int64_t)sizeof(PyObjectHeader)) return 0;
    if (pcc_gc_forwarding_find(from) != NULL) return 0;
    if (pcc_gc_relocation_set_find(from) == NULL) return 0;
    PyObjectHeader *from_h = py_header(from);
    int32_t from_flags = py_header_flags_load(from_h);
    if (
        (from_flags & (
            PY_FLAG_GC_PINNED | PY_FLAG_GC_DEALLOCATING
        )) != 0
    ) return 0;
    if (!pcc_gc_colored_relocate_copy_supported_tag(from_h->type_tag)) {
        return 0;
    }

    int64_t known_size = pcc_gc_known_object_size_unlocked(from);
    if (known_size <= 0 || size > known_size) return 0;
    if (tag_out != NULL) *tag_out = from_h->type_tag;
    if (flags_out != NULL) *flags_out = from_flags;
    return 1;
}

typedef struct {
    PccGcRelocationNode *relocation_node;
    PccGcZPageEvacuationNode *evacuation_node;
    PccGcZPageNode *source_zpage_node;
} PccGcRelocationCopyFinish;

_Static_assert(
    sizeof(PccGcRelocationCopyFinish) == 24,
    "PccGcRelocationCopyFinish ABI drift"
);
_Static_assert(
    offsetof(PccGcRelocationCopyFinish, relocation_node) == 0,
    "PccGcRelocationCopyFinish.relocation_node ABI drift"
);
_Static_assert(
    offsetof(PccGcRelocationCopyFinish, evacuation_node) == 8,
    "PccGcRelocationCopyFinish.evacuation_node ABI drift"
);
_Static_assert(
    offsetof(PccGcRelocationCopyFinish, source_zpage_node) == 16,
    "PccGcRelocationCopyFinish.source_zpage_node ABI drift"
);

static void pcc_gc_relocate_copy_finish(
    PccGcRelocationCopyFinish *finish
) {
    if (finish == NULL) return;
    if (finish->relocation_node != NULL) {
        free(finish->relocation_node);
        finish->relocation_node = NULL;
    }
    if (finish->evacuation_node != NULL) {
        free(finish->evacuation_node);
        finish->evacuation_node = NULL;
    }
    if (finish->source_zpage_node != NULL) {
        pcc_gc_backend4_zpage_finish_relocation_detach(
            finish->source_zpage_node
        );
        finish->source_zpage_node = NULL;
    }
}

static PyObject *pcc_gc_relocate_copy_preallocated_unlocked(
    PyObject *from,
    int64_t size,
    PyObject *to,
    PccGcRelocateSlotPairs *pairs,
    PccGcForwardingInstallPlan *forwarding_plan,
    PccGcRelocationCopyFinish *finish
) {
    if (finish == NULL) return NULL;
    finish->relocation_node = NULL;
    finish->evacuation_node = NULL;
    finish->source_zpage_node = NULL;
    int32_t from_tag = 0;
    if (
        to == NULL
        || PY_IS_TAGGED_INT(to)
        || !pcc_gc_relocate_copy_snapshot_unlocked(
            from, size, &from_tag, NULL
        )
    ) return NULL;
    int64_t to_size = pcc_gc_known_object_size_unlocked(to);
    if (
        to_size < size
        || py_header(to)->type_tag != from_tag
        || (py_header_flags_load(py_header(to)) & PY_FLAG_GC_PINNED) == 0
    ) return NULL;
    PyObjectHeader *from_h = py_header(from);
    /* The header memcpy below clobbers `to`'s flags with `from`'s.
     * Allocation-origin bits describe WHERE `to` physically lives and
     * must survive the copy: losing ZPAGE_ALLOC undercounts the page's
     * pending_forwardings on chained relocations (page destroyed while
     * forwarded -> UAF), and inheriting a stale bit leaks/mis-frees.
     * SWEEP_CANDIDATE is a finished-cycle "was unreachable" verdict,
     * not residency: relocation proves the value is live memory being
     * kept, so carrying the stale verdict onto the copy lets a later
     * no-re-mark sweep (pcc_gc_collect_tracing consumes pending
     * candidates verbatim) run PASS-0 __del__ on a reachable object
     * (gc-backend4-concurrent-entry-loss.md CONFIRMED capture). */
    int32_t to_residency = py_header_flags_load(py_header(to))
        & (
            PY_FLAG_GC_ZPAGE_ALLOC
            | PY_FLAG_GC_MINOR_ARENA
            | PY_FLAG_GC_MALLOC_ALLOC
        );
    memcpy(to, from, (size_t)size);
    PyObjectHeader *to_h = py_header(to);
    to_h->refcount = 1;
    py_header_flags_and(
        to_h,
        ~(
            PY_FLAG_GC_RELOCATION_CANDIDATE
            | PY_FLAG_GC_RELOCATION_TARGET
            | PY_FLAG_GC_ZPAGE_ALLOC
            | PY_FLAG_GC_MINOR_ARENA
            | PY_FLAG_GC_MALLOC_ALLOC
            | PY_FLAG_GC_SWEEP_CANDIDATE
        )
    );
    py_header_flags_or(to_h, to_residency);
    if (
        pcc_gc_relocate_copy_payload_prepared_locked(
            from, to, size, pairs
        ) != 0
    ) {
        return NULL;
    }
    if (
        pcc_gc_install_forwarding_preallocated_unlocked(
            from, to, forwarding_plan
        ) != 0
    ) {
        return NULL;
    }
    /* Only now is relocation committed.  Clearing this before payload copy
     * and forwarding installation made their rollback paths lose a legitimate
     * source verdict and leak an unreachable source object. */
    py_header_flags_and(from_h, ~PY_FLAG_GC_SWEEP_CANDIDATE);
    /* The finite instance-field cache keys the raw class address.  A
     * forwarding shell can later be retired and its address reused, so
     * invalidate that cache before reuse becomes possible.  Relocation is
     * rare and already serialized by the GC graph lock. */
    if (from_h->type_tag == PY_TYPE_CLASS) {
        __atomic_add_fetch(
            &py_class_attr_cache_epoch, 1, __ATOMIC_RELEASE
        );
    }
    /* Count-on-NEW (remap design R2): move the OLD copy's entire
     * outstanding refcount onto the new copy now, and make the old
     * copy an immortal shell. From here on every incref/decref through
     * a stale pointer resolves to the new copy (py_incref/py_decref
     * candidate branches), slot heals are count-neutral, and the shell
     * is freed by page retirement after the remap pass — never by
     * refcount. */
    {
        int64_t outstanding = pcc_refcount_load(&from_h->refcount);
        if (outstanding > 0) {
            __atomic_add_fetch(
                &py_header(to)->refcount, outstanding, __ATOMIC_ACQ_REL
            );
        }
        py_header_flags_or(from_h, PY_FLAG_IMMORTAL);
    }
    PccGcZPage *from_page = NULL;
    PccGcZPageNode *from_zpage = pcc_gc_backend4_zpage_find_unlocked(from);
    if (from_zpage != NULL) {
        from_page = from_zpage->page;
    }
    __atomic_add_fetch(
        &pcc_gc_backend4_evacuated_bytes_count,
        size,
        __ATOMIC_RELAXED
    );
    PccGcRelocationNode *detached_relocation =
        pcc_gc_relocation_set_detach(from);
    finish->relocation_node = detached_relocation;
    if (
        from_page != NULL
        && !pcc_gc_backend4_relocation_set_contains_page_unlocked(from_page)
    ) {
        PccGcZPageEvacuationNode *detached_page =
            pcc_gc_backend4_evacuation_page_detach_unlocked(from_page);
        finish->evacuation_node = detached_page;
    }
    finish->source_zpage_node =
        pcc_gc_backend4_zpage_detach_for_relocation_unlocked(from);
    return to;
}

PyObject *pcc_gc_relocate_copy(PyObject *from, int64_t size) {
    pcc_gc_init_config();
    int32_t from_tag = 0;
    int32_t from_flags = 0;
    int64_t slot_count = -1;
    pcc_gc_graph_lock();
    int eligible = pcc_gc_relocate_copy_snapshot_unlocked(
        from, size, &from_tag, &from_flags
    );
    if (eligible) {
        slot_count = pcc_gc_relocate_slot_count_locked(from);
        if (slot_count < 0) eligible = 0;
    }
    pcc_gc_graph_unlock();
    if (!eligible) return NULL;

    PccGcRelocateSlotPairs pairs;
    if (pcc_gc_relocate_slot_pairs_prepare(slot_count, &pairs) != 0) {
        return NULL;
    }

    pcc_gc_graph_lock();
    int raw_snapshot = pcc_gc_relocate_raw_snapshot_locked(
        from, size, &pairs
    );
    pcc_gc_graph_unlock();
    if (raw_snapshot != 0 || pcc_gc_relocate_raw_prepare(&pairs) != 0) {
        pcc_gc_relocate_slot_pairs_finish(&pairs);
        return NULL;
    }

    PyObject *to = pcc_gc_alloc(
        size,
        from_tag,
        (
            from_flags
            & ~(
                PY_FLAG_GC_RELOCATION_CANDIDATE
                | PY_FLAG_GC_RELOCATION_TARGET
            )
        ) | PY_FLAG_GC_PINNED
    );
    if (to == NULL) {
        pcc_gc_relocate_slot_pairs_finish(&pairs);
        return NULL;
    }

    PccGcForwardingInstallPlan *forwarding_plan =
        pcc_gc_forwarding_install_plan_prepare(from, to);
    if (forwarding_plan == NULL) {
        pcc_gc_relocate_slot_pairs_finish(&pairs);
        py_decref(to);
        return NULL;
    }

    PccGcRelocationCopyFinish finish = { 0 };
    pcc_gc_graph_lock();
    int valid_pairs = pcc_gc_relocate_slot_pairs_validate_locked(
        from, to, size, &pairs
    ) == 0 && pcc_gc_relocate_raw_validate_locked(
        from, to, size, &pairs
    ) == 0;
    PyObject *committed = valid_pairs
        ? pcc_gc_relocate_copy_preallocated_unlocked(
            from, size, to, &pairs, forwarding_plan, &finish
        )
        : NULL;
    pcc_gc_graph_unlock();
    pcc_gc_relocate_slot_pairs_finish(&pairs);
    pcc_gc_relocate_copy_finish(&finish);
    pcc_gc_forwarding_install_plan_finish(forwarding_plan);
    if (committed == NULL) py_decref(to);
    return committed;
}

static int64_t pcc_gc_backend4_snapshot_relocation_batch_unlocked(
    PyObject **sources,
    int64_t source_capacity
) {
    if (sources == NULL || source_capacity <= 0) return 0;
    int64_t captured = 0;
    PccGcRelocationNode *n = pcc_gc_relocation_set;
    while (n != NULL && captured < source_capacity) {
        sources[captured] = n->obj;
        captured++;
        n = n->next;
    }
    return captured;
}

static int64_t pcc_gc_relocate_selected(int64_t budget) {
    if (pcc_gc_selected_backend != PCC_GC_KIND_COLORED_RELOCATING) return 0;
    if (pcc_gc_backend4_remap_active != 0) return 0;
    if (budget <= 0) return 0;
    int64_t moved = 0;
    int stalled = 0;
    while (moved < budget && !stalled) {
        PyObject *sources[PCC_GC_SAFEPOINT_BATCH];
        int64_t capacity = budget - moved;
        if (capacity > PCC_GC_SAFEPOINT_BATCH) {
            capacity = PCC_GC_SAFEPOINT_BATCH;
        }
        pcc_gc_graph_lock();
        int64_t captured =
            pcc_gc_backend4_snapshot_relocation_batch_unlocked(
                sources,
                capacity
            );
        pcc_gc_graph_unlock();

        int64_t batch_moved = 0;
        for (int64_t i = 0; i < captured; i++) {
            int64_t size = pcc_gc_known_object_size(sources[i]);
            PyObject *to = pcc_gc_relocate_copy(sources[i], size);
            if (to != NULL) {
                py_decref(to);
                moved++;
                batch_moved++;
            }
        }
        if (captured == PCC_GC_SAFEPOINT_BATCH) {
            pcc_thread_safepoint();
        }
        if (captured <= 0 || batch_moved <= 0) stalled = 1;
    }

    int should_remap = 0;
    pcc_gc_graph_lock();
    if (moved > 0 && pcc_gc_relocation_set != NULL) {
        __atomic_add_fetch(
            &pcc_gc_backend4_evacuation_incomplete_batches_count,
            1,
            __ATOMIC_RELAXED
        );
    }
    if (
        pcc_gc_relocation_set == NULL
        && pcc_gc_forwarding_population > 0
    ) {
        should_remap = 1;
    }
    pcc_gc_graph_unlock();
    if (should_remap) {
        (void)pcc_gc_backend4_remap_and_retire_stopped_world();
    }
    return moved;
}

int64_t pcc_gc_backend4_evacuation_drain(int64_t budget) {
    pcc_gc_init_config();
    return pcc_gc_relocate_selected(budget);
}

static int64_t pcc_gc_backend4_snapshot_selected_page_batch_unlocked(
    PccGcZPage *page,
    PyObject **sources,
    int64_t source_capacity
) {
    if (
        page == NULL
        || sources == NULL
        || source_capacity <= 0
    ) return 0;
    int64_t captured = 0;
    int64_t examined = 0;
    PccGcRelocationNode *n = pcc_gc_relocation_set;
    while (
        n != NULL
        && examined < PCC_GC_SAFEPOINT_BATCH
        && captured < source_capacity
    ) {
        PccGcRelocationNode *next = n->next;
        examined++;
        PccGcZPageNode *zp = pcc_gc_backend4_zpage_find_unlocked(n->obj);
        if (zp != NULL && zp->page == page) {
            sources[captured] = n->obj;
            captured++;
        }
        n = next;
    }
    return captured;
}

int64_t pcc_gc_backend4_evacuation_page_drain(int64_t page_budget) {
    pcc_gc_init_config();
    if (pcc_gc_selected_backend != PCC_GC_KIND_COLORED_RELOCATING) return 0;
    if (pcc_gc_backend4_remap_active != 0) return 0;
    if (page_budget <= 0) return 0;
    int64_t moved = 0;
    int64_t pages = 0;
    int stalled = 0;
    PccGcZPage *page = NULL;
    while (pages < page_budget && !stalled) {
        PyObject *sources[PCC_GC_SAFEPOINT_BATCH];
        pcc_gc_graph_lock();
        if (page == NULL && pcc_gc_backend4_evacuation_pages != NULL) {
            page = pcc_gc_backend4_evacuation_pages->page;
        }
        if (page == NULL) {
            pcc_gc_graph_unlock();
            break;
        }
        int64_t captured =
            pcc_gc_backend4_snapshot_selected_page_batch_unlocked(
                page,
                sources,
                PCC_GC_SAFEPOINT_BATCH
            );
        pcc_gc_graph_unlock();

        int64_t batch_moved = 0;
        for (int64_t i = 0; i < captured; i++) {
            int64_t size = pcc_gc_known_object_size(sources[i]);
            PyObject *to = pcc_gc_relocate_copy(sources[i], size);
            if (to != NULL) {
                py_decref(to);
                batch_moved++;
            }
        }
        moved += batch_moved;
        if (captured == PCC_GC_SAFEPOINT_BATCH) {
            pcc_thread_safepoint();
        }
        pcc_gc_graph_lock();
        PccGcZPageEvacuationNode *head =
            pcc_gc_backend4_evacuation_pages;
        int page_complete = head == NULL || head->page != page;
        pcc_gc_graph_unlock();
        if (page_complete) {
            if (page != NULL && batch_moved > 0) pages++;
            page = NULL;
        } else if (captured <= 0 || batch_moved <= 0) {
            /* A selected entry that cannot be copied must not make the
             * public drain spin forever.  Leave it selected for the caller's
             * existing fail-closed handling. */
            stalled = 1;
        }
    }
    int should_remap = 0;
    pcc_gc_graph_lock();
    if (moved > 0 && pcc_gc_relocation_set != NULL) {
        __atomic_add_fetch(
            &pcc_gc_backend4_evacuation_incomplete_batches_count,
            1,
            __ATOMIC_RELAXED
        );
    }
    if (
        pcc_gc_relocation_set == NULL
        && pcc_gc_forwarding_population > 0
    ) {
        should_remap = 1;
    }
    pcc_gc_graph_unlock();
    if (should_remap) {
        (void)pcc_gc_backend4_remap_and_retire_stopped_world();
    }
    return moved;
}

struct PccGcForwardingInstallPlan {
    PccGcIdentityNode *from_identity;
    PccGcIdentityNode *to_identity;
    PccGcForwardNode *forwarding;
    void *identity_slots;
    int64_t identity_cap;
    void *forwarding_slots;
    int64_t forwarding_cap;
    void *target_slots;
    int64_t target_cap;
};

_Static_assert(
    sizeof(PccGcForwardingInstallPlan) == 72,
    "PccGcForwardingInstallPlan ABI drift"
);

static void pcc_gc_forwarding_install_plan_finish(
    PccGcForwardingInstallPlan *plan
) {
    if (plan == NULL) return;
    free(plan->from_identity);
    free(plan->to_identity);
    free(plan->forwarding);
    free(plan->identity_slots);
    free(plan->forwarding_slots);
    free(plan->target_slots);
    free(plan);
}

static PccGcForwardingInstallPlan *pcc_gc_forwarding_install_plan_prepare(
    PyObject *from,
    PyObject *to
) {
    int valid = 1;
    int64_t identity_cap = -1;
    int64_t forwarding_cap = -1;
    int64_t target_cap = -1;
    pcc_gc_graph_lock();
    if (
        !pcc_gc_backend_uses_forwarding()
        || (
            pcc_gc_selected_backend == PCC_GC_KIND_COLORED_RELOCATING
            && pcc_gc_backend4_reseed_commit_owner != 0
        )
        || from == NULL
        || to == NULL
        || PY_IS_TAGGED_INT(from)
        || PY_IS_TAGGED_INT(to)
        || from == to
        || !pcc_gc_is_known_object(from)
        || !pcc_gc_is_known_object(to)
        || (py_header_flags_load(py_header(from)) & PY_FLAG_GC_PINNED) != 0
        || pcc_gc_forwarding_find(from) != NULL
        || pcc_gc_forwarding_target_find(to) != NULL
    ) {
        valid = 0;
    } else {
        identity_cap = pcc_gc_forwarding_plan_index_capacity(2, 2);
        forwarding_cap = pcc_gc_forwarding_plan_index_capacity(0, 1);
        target_cap = pcc_gc_forwarding_plan_index_capacity(1, 1);
        if (
            identity_cap < 0
            || forwarding_cap < 0
            || target_cap < 0
        ) {
            valid = 0;
        }
    }
    pcc_gc_graph_unlock();
    if (!valid) return NULL;

    PccGcForwardingInstallPlan *plan = (
        PccGcForwardingInstallPlan *
    )calloc(1, sizeof(PccGcForwardingInstallPlan));
    if (plan == NULL) return NULL;
    plan->from_identity = (
        PccGcIdentityNode *
    )calloc(1, sizeof(PccGcIdentityNode));
    plan->to_identity = (
        PccGcIdentityNode *
    )calloc(1, sizeof(PccGcIdentityNode));
    plan->forwarding = (
        PccGcForwardNode *
    )calloc(1, sizeof(PccGcForwardNode));
    plan->identity_cap = identity_cap;
    plan->forwarding_cap = forwarding_cap;
    plan->target_cap = target_cap;
    if (identity_cap > 0) {
        plan->identity_slots = calloc((size_t)identity_cap, 24);
    }
    if (forwarding_cap > 0) {
        plan->forwarding_slots = calloc((size_t)forwarding_cap, 24);
    }
    if (target_cap > 0) {
        plan->target_slots = calloc((size_t)target_cap, 24);
    }
    if (
        plan->from_identity == NULL
        || plan->to_identity == NULL
        || plan->forwarding == NULL
        || (identity_cap > 0 && plan->identity_slots == NULL)
        || (forwarding_cap > 0 && plan->forwarding_slots == NULL)
        || (target_cap > 0 && plan->target_slots == NULL)
    ) {
        pcc_gc_forwarding_install_plan_finish(plan);
        return NULL;
    }
    return plan;
}

static int64_t pcc_gc_install_forwarding_preallocated_unlocked(
    PyObject *from,
    PyObject *to,
    PccGcForwardingInstallPlan *plan
) {
    if (
        pcc_gc_selected_backend == PCC_GC_KIND_COLORED_RELOCATING
        && (
            pcc_gc_backend4_relocation_reset_owner != 0
            || pcc_gc_backend4_reseed_commit_owner != 0
        )
    ) return -1;
    if (!pcc_gc_backend_uses_forwarding()) return -1;
    if (from == NULL || to == NULL || plan == NULL) return -1;
    if (PY_IS_TAGGED_INT(from) || PY_IS_TAGGED_INT(to)) return -1;
    if (from == to) return -1;
    if (!pcc_gc_is_known_object(from) || !pcc_gc_is_known_object(to)) {
        return -1;
    }
    PyObjectHeader *from_h = py_header(from);
    if ((py_header_flags_load(from_h) & PY_FLAG_GC_PINNED) != 0) {
        pcc_gc_relocation_pin_rejects++;
        return -2;
    }
    if (
        pcc_gc_forwarding_find(from) != NULL
        || pcc_gc_forwarding_target_find(to) != NULL
    ) {
        return -1;
    }
    if (
        pcc_gc_forwarding_plan_index_commit(
            2, &plan->identity_slots, plan->identity_cap, 2
        ) < 0
        || pcc_gc_forwarding_plan_index_commit(
            0, &plan->forwarding_slots, plan->forwarding_cap, 1
        ) < 0
        || pcc_gc_forwarding_plan_index_commit(
            1, &plan->target_slots, plan->target_cap, 1
        ) < 0
    ) {
        return -1;
    }

    PccGcIdentityNode *from_identity = pcc_gc_identity_find(from);
    if (from_identity == NULL) {
        PccGcIdentityNode *node = plan->from_identity;
        if (node == NULL) return -1;
        if (pcc_gc_next_object_id <= 0) pcc_gc_next_object_id = 1;
        node->obj = from;
        node->id = pcc_gc_next_object_id;
        node->next = pcc_gc_identities;
        node->prev = NULL;
        if (
            pcc_gc_forwarding_plan_index_insert(2, from, node) != 1
        ) {
            return -1;
        }
        pcc_gc_next_object_id++;
        if (pcc_gc_identities != NULL) pcc_gc_identities->prev = node;
        pcc_gc_identities = node;
        plan->from_identity = NULL;
        from_identity = node;
    }

    PccGcIdentityNode *to_identity = pcc_gc_identity_find(to);
    if (to_identity == NULL) {
        PccGcIdentityNode *node = plan->to_identity;
        if (node == NULL) return -1;
        node->obj = to;
        node->id = from_identity->id;
        node->next = pcc_gc_identities;
        node->prev = NULL;
        if (pcc_gc_forwarding_plan_index_insert(2, to, node) != 1) {
            return -1;
        }
        if (pcc_gc_identities != NULL) pcc_gc_identities->prev = node;
        pcc_gc_identities = node;
        plan->to_identity = NULL;
    } else {
        to_identity->id = from_identity->id;
    }

    PccGcForwardNode *node = plan->forwarding;
    if (node == NULL) return -1;
    node->from = from;
    node->to = to;
    node->next = pcc_gc_forwardings;
    node->prev = NULL;
    node->target_next = NULL;
    node->target_prev = NULL;
    node->from_page = NULL;
    if (pcc_gc_forwarding_plan_index_insert(0, from, node) != 1) {
        return -1;
    }
    if (pcc_gc_forwarding_plan_index_insert(1, to, node) != 1) {
        (void)pcc_gc_forwarding_index_remove(from);
        return -1;
    }

    py_incref(to);
    if (pcc_gc_forwardings != NULL) pcc_gc_forwardings->prev = node;
    pcc_gc_forwardings = node;
    plan->forwarding = NULL;
    pcc_gc_forwarding_population++;
    if (
        pcc_gc_selected_backend == PCC_GC_KIND_COLORED_RELOCATING
        && (py_header_flags_load(from_h) & PY_FLAG_GC_ZPAGE_ALLOC) != 0
    ) {
        PccGcZPageNode *znode = pcc_gc_backend4_zpage_find_unlocked(from);
        if (znode != NULL && znode->page != NULL) {
            znode->page->pending_forwardings++;
            node->from_page = znode->page;
        }
    }
    py_header_flags_or(from_h, PY_FLAG_GC_RELOCATION_CANDIDATE);
    py_header_flags_or(py_header(to), PY_FLAG_GC_RELOCATION_TARGET);
    pcc_gc_relocation_forwards++;
    return 0;
}

static int64_t pcc_gc_install_forwarding_unlocked(PyObject *from, PyObject *to) {
    if (
        pcc_gc_selected_backend == PCC_GC_KIND_COLORED_RELOCATING
        && (
            pcc_gc_backend4_relocation_reset_owner != 0
            || pcc_gc_backend4_reseed_commit_owner != 0
        )
    ) return -1;
    if (!pcc_gc_backend_uses_forwarding()) return -1;
    if (from == NULL || to == NULL) return -1;
    if (PY_IS_TAGGED_INT(from) || PY_IS_TAGGED_INT(to)) return -1;
    if (from == to) return -1;
    if (!pcc_gc_is_known_object(from) || !pcc_gc_is_known_object(to)) {
        return -1;
    }
    PyObjectHeader *from_h = py_header(from);
    if ((from_h->flags & PY_FLAG_GC_PINNED) != 0) {
        pcc_gc_relocation_pin_rejects++;
        return -2;
    }
    PccGcIdentityNode *from_identity = pcc_gc_identity_ensure(from);
    if (from_identity == NULL) return -1;
    if (!pcc_gc_identity_assign(to, from_identity->id)) return -1;

    PccGcForwardNode *existing = pcc_gc_forwarding_find(from);
    if (existing != NULL) {
        if (existing->to != to) {
            PccGcForwardNode *target_head = NULL;
            if (
                pcc_gc_forwarding_target_prepare(
                    to, existing, &target_head
                ) < 0
            ) return -1;
            py_incref(to);
            pcc_gc_forwarding_target_unlink(existing);
            PyObject *old = existing->to;
            existing->to = to;
            pcc_gc_forwarding_target_attach_prepared(existing, target_head);
            py_decref(old);
        }
    } else {
        PccGcForwardNode *n = (
            PccGcForwardNode *
        )calloc(1, sizeof(PccGcForwardNode));
        if (n == NULL) return -1;
        py_incref(to);
        n->from = from;
        n->to = to;
        n->next = pcc_gc_forwardings;
        n->prev = NULL;
        if (pcc_gc_forwardings != NULL) pcc_gc_forwardings->prev = n;
        pcc_gc_forwardings = n;
        if (pcc_gc_forwarding_index_insert(from, n) < 0) {
            pcc_gc_forwardings = n->next;
            if (n->next != NULL) n->next->prev = NULL;
            py_decref(to);
            free(n);
            return -1;
        }
        PccGcForwardNode *target_head = NULL;
        if (pcc_gc_forwarding_target_prepare(to, n, &target_head) < 0) {
            (void)pcc_gc_forwarding_index_remove(from);
            pcc_gc_forwarding_unlink_main(n);
            py_decref(to);
            free(n);
            return -1;
        }
        pcc_gc_forwarding_target_attach_prepared(n, target_head);
        pcc_gc_forwarding_population++;
        if (
            pcc_gc_selected_backend == PCC_GC_KIND_COLORED_RELOCATING
            && (py_header_flags_load(from_h) & PY_FLAG_GC_ZPAGE_ALLOC) != 0
        ) {
            PccGcZPageNode *zn = pcc_gc_backend4_zpage_find_unlocked(from);
            if (zn != NULL && zn->page != NULL) {
                zn->page->pending_forwardings++;
                n->from_page = zn->page;
            }
        }
    }
    py_header_flags_or(from_h, PY_FLAG_GC_RELOCATION_CANDIDATE);
    py_header_flags_or(py_header(to), PY_FLAG_GC_RELOCATION_TARGET);
    pcc_gc_relocation_forwards++;
    return 0;
}

int64_t pcc_gc_install_forwarding(PyObject *from, PyObject *to) {
    pcc_gc_init_config();
    pcc_gc_graph_lock();
    int reject = 0;
    if (
        from != NULL
        && to != NULL
        && !PY_IS_TAGGED_INT(from)
        && !PY_IS_TAGGED_INT(to)
        && pcc_gc_is_known_object(from)
        && pcc_gc_is_known_object(to)
    ) {
        int32_t from_tag = py_header(from)->type_tag;
        int32_t to_tag = py_header(to)->type_tag;
        if (pcc_gc_selected_backend == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR) {
            if (
                from_tag != to_tag
                || !pcc_gc_relocate_copy_supported_tag(from_tag)
            ) {
                reject = 1;
            }
        } else if (
            pcc_gc_selected_backend == PCC_GC_KIND_COLORED_RELOCATING
            && (
                from_tag == PY_TYPE_CLASS
                || from_tag == PY_TYPE_WEAKREF
                || from_tag == PY_TYPE_CONTINUATION
                || from_tag == PY_TYPE_MEMORYVIEW
                || from_tag == PY_TYPE_CPY_HANDLE
                || from_tag == PY_TYPE_THREAD
                || from_tag == PY_TYPE_VIRTUAL_THREAD
                || from_tag == PY_TYPE_VTHREAD_CHANNEL
            )
        ) {
            reject = 1;
        }
    }
    /* -3 means the raw public seam lacks a required payload/side-index
     * commit.  Rejection happens before identity, edge, flag, or refcount
     * mutation.  Internal relocation/oldification uses the unlocked seam
     * only after its payload transaction is prepared. */
    int64_t rc = reject ? -3 : pcc_gc_install_forwarding_unlocked(from, to);
    pcc_gc_graph_unlock();
    return rc;
}

static int64_t pcc_gc_step_generational_promotion(
    int64_t budget,
    int promote_all_young
);

static void pcc_gc_minor_collect_reset(void) {
    __atomic_add_fetch(
        &pcc_gc_minor_collections, 1, __ATOMIC_RELAXED
    );
    if (pcc_gc_selected_backend == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR) {
        (void)pcc_gc_step_generational_promotion(1024, 0);
    }
    __atomic_store_n(&pcc_gc_minor_bytes, 0, __ATOMIC_RELEASE);
}

static void pcc_gc_note_minor_alloc(int64_t bytes) {
    if (bytes <= 0 || bytes > pcc_gc_minor_alloc_max) return;
    __atomic_add_fetch(
        &pcc_gc_minor_allocations, 1, __ATOMIC_RELAXED
    );
    __atomic_add_fetch(&pcc_gc_minor_bytes, bytes, __ATOMIC_ACQ_REL);
}

static int64_t pcc_gc_align16(int64_t bytes) {
    if (bytes <= 0) return 0;
    if (bytes > INT64_MAX - 15) return 0;
    return (bytes + 15) & ~15LL;
}

static PccGcMinorBlock *pcc_gc_minor_new_block(int64_t min_bytes) {
    int64_t block_bytes = pcc_gc_minor_heap_size;
    if (block_bytes < min_bytes) block_bytes = min_bytes;
    block_bytes = pcc_gc_align16(block_bytes);
    if (block_bytes <= 0) return NULL;

    PccGcMinorBlock *block = (PccGcMinorBlock *)calloc(
        1, sizeof(PccGcMinorBlock)
    );
    if (block == NULL) return NULL;
    block->base = (uint8_t *)calloc(1, (size_t)block_bytes);
    if (block->base == NULL) {
        free(block);
        return NULL;
    }
    block->ptr = block->base;
    block->end = block->base + block_bytes;
    block->owner_thread_id = pcc_current_thread_id();
    pcc_gc_graph_lock();
    block->next = pcc_gc_minor_blocks;
    pcc_gc_minor_blocks = block;
    pcc_gc_graph_unlock();
    pcc_gc_minor_current = block;
    __atomic_add_fetch(
        &pcc_gc_minor_arena_refills, 1, __ATOMIC_RELAXED
    );
    return block;
}

static PccGcMinorBlock *pcc_gc_minor_find_reusable_block(int64_t min_bytes) {
    int64_t owner_thread_id = pcc_current_thread_id();
    pcc_gc_graph_lock();
    for (PccGcMinorBlock *block = pcc_gc_minor_blocks; block != NULL; block = block->next) {
        if (block->owner_thread_id != owner_thread_id) continue;
        if (__atomic_load_n(&block->live_objects, __ATOMIC_ACQUIRE) != 0) {
            continue;
        }
        if ((int64_t)(block->end - block->base) < min_bytes) continue;
        block->ptr = block->base;
        pcc_gc_graph_unlock();
        pcc_gc_minor_current = block;
        __atomic_store_n(&pcc_gc_minor_bytes, 0, __ATOMIC_RELEASE);
        return block;
    }
    pcc_gc_graph_unlock();
    return NULL;
}

void *pcc_gc_try_minor_alloc(int64_t bytes) {
    pcc_gc_init_config();
    if (pcc_gc_selected_backend != PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR) {
        return NULL;
    }
    pcc_gc_pending_minor_block = NULL;
    int64_t aligned = pcc_gc_align16(bytes);
    if (aligned <= 0 || aligned > pcc_gc_minor_alloc_max) {
        __atomic_add_fetch(
            &pcc_gc_minor_arena_fallbacks, 1, __ATOMIC_RELAXED
        );
        return NULL;
    }

    PccGcMinorBlock *block = pcc_gc_minor_current;
    if (
        block != NULL
        && (int64_t)(block->end - block->ptr) < aligned
    ) {
        if (
            block->owner_thread_id == pcc_current_thread_id()
            && __atomic_load_n(&block->live_objects, __ATOMIC_ACQUIRE) == 0
            && (int64_t)(block->end - block->base) >= aligned
        ) {
            block->ptr = block->base;
            __atomic_store_n(&pcc_gc_minor_bytes, 0, __ATOMIC_RELEASE);
        } else {
            pcc_gc_minor_collect_reset();
            block = pcc_gc_minor_find_reusable_block(aligned);
        }
    }
    if (block == NULL) {
        block = pcc_gc_minor_find_reusable_block(aligned);
    }
    if (block == NULL) {
        block = pcc_gc_minor_new_block(aligned);
        if (block == NULL) {
            __atomic_add_fetch(
                &pcc_gc_minor_arena_fallbacks, 1, __ATOMIC_RELAXED
            );
            return NULL;
        }
    }

    void *mem = block->ptr;
    block->ptr += aligned;
    __atomic_add_fetch(&block->live_objects, 1, __ATOMIC_ACQ_REL);
    pcc_gc_pending_minor_block = block;
    __atomic_add_fetch(
        &pcc_gc_minor_arena_bumps, 1, __ATOMIC_RELAXED
    );
    pcc_gc_note_minor_alloc(aligned);
    memset(mem, 0, (size_t)bytes);
    return mem;
}

static int pcc_gc_backend_valid(int64_t backend) {
    return backend >= PCC_GC_KIND_REFCOUNT_CYCLE
        && backend <= PCC_GC_KIND_COLORED_RELOCATING;
}

static int pcc_gc_should_track_frame_roots(void) {
    return pcc_gc_selected_backend != PCC_GC_KIND_REFCOUNT_CYCLE
        || pcc_gc_backend0_frame_roots_enabled != 0;
}

static int pcc_gc_frame_roots_disabled_fast(void) {
    return pcc_gc_config_initialized != 0
        && pcc_gc_selected_backend == PCC_GC_KIND_REFCOUNT_CYCLE
        && pcc_gc_backend0_frame_roots_enabled == 0;
}

int64_t pcc_gc_backend(void) {
    if (pcc_gc_config_initialized) return pcc_gc_selected_backend;
    pcc_gc_init_config();
    return pcc_gc_selected_backend;
}

int64_t pcc_gc_set_backend(int64_t backend) {
    pcc_gc_init_config();
    if (!pcc_gc_backend_valid(backend)) return -1;

    int64_t observed_backend = pcc_gc_selected_backend;
#if PCC_WITH_THREADS
    if (
        pcc_gc_graph_lock_depth > 0
        && (
            observed_backend == PCC_GC_KIND_CONCURRENT_MARK_SWEEP
            || backend == PCC_GC_KIND_CONCURRENT_MARK_SWEEP
        )
    ) return -1;
#endif
    if (
        observed_backend == PCC_GC_KIND_CONCURRENT_MARK_SWEEP
        && pcc_threads_enabled()
    ) {
        /* Joining the CMS worker cannot make progress from a no-park scope,
         * and it deadlocks when this caller owns the stopped world that parks
         * the worker.  Check depth first: owns-world itself queries the thread
         * registry mutex and is forbidden while already no-park. */
        if (pcc_thread_no_park_depth() > 0) return -1;
        if (pcc_thread_owns_stopped_world() != 0) return -1;
    }

    /* Preflight the non-mutating transition blocker before pausing CMS.  The
     * graph-locked commit below revalidates it after the pause. */
    pcc_gc_graph_lock();
    int64_t preflight_backend = pcc_gc_selected_backend;
    if (
        pcc_gc_trace_extension_roots_pending == 4
        || pcc_gc_backend4_remap_active != 0
        || (
            backend != preflight_backend
            && (
                pcc_gc_forwardings != NULL
                || pcc_gc_forwarding_population != 0
            )
        )
    ) {
        /* GC3 oldification and GC4 two-epoch relocation share a node layout,
         * but not ownership/refcount policy.  Roots must be healed and every
         * source retired before any collector change can commit. */
        pcc_gc_graph_unlock();
        return -1;
    }
    pcc_gc_graph_unlock();

    /* Pause preserves the residual queue, its epoch, and caller TLS.  Only a
     * successful graph commit is allowed to reset them. */
    int cms_worker_paused = (
        preflight_backend == PCC_GC_KIND_CONCURRENT_MARK_SWEEP
    );
    if (cms_worker_paused) pcc_gc_cms_pause_worker_preserve_queue();

    pcc_gc_graph_lock();
    int64_t old_backend = pcc_gc_selected_backend;
    if (
        old_backend != preflight_backend
        || pcc_gc_trace_extension_roots_pending == 4
        || pcc_gc_backend4_remap_active != 0
        || (
            backend != old_backend
            && (
                pcc_gc_forwardings != NULL
                || pcc_gc_forwarding_population != 0
            )
        )
    ) {
        pcc_gc_graph_unlock();
        if (cms_worker_paused) pcc_gc_cms_maybe_start_worker();
        return -1;
    }
    if (backend == PCC_GC_KIND_REFCOUNT_CYCLE) {
        /* Backend 0 does not retain object nodes.  Object-family LIVE slots
         * already carry exact granule provenance; transfer every other live
         * origin to the exact set before changing the selected backend, so
         * concurrent allocators can never observe an unindexed object. */
        for (PccGcObjectNode *n = pcc_gc_objects; n != NULL; n = n->next) {
            if (pcc_gc_granule_is_object_start(n->obj) != 1) {
                if (pcc_gc_managed_pointer_index_insert(n->obj) < 0) {
                    pcc_gc_graph_unlock();
                    if (cms_worker_paused) {
                        pcc_gc_cms_maybe_start_worker();
                    }
                    return -1;
                }
            }
        }
    }
    if (pcc_gc_tracing_cycle_epoch_advance_unlocked() == 0) {
        pcc_gc_graph_unlock();
        if (cms_worker_paused) pcc_gc_cms_maybe_start_worker();
        return -1;
    }
    pcc_gc_selected_backend = backend;
    if (backend == PCC_GC_KIND_REFCOUNT_CYCLE) {
        pcc_gc_backend0_frame_roots_enabled = 1;
    }
    pcc_gc_update_read_barrier_enabled(backend);
    pcc_gc_mark_active_store(0);
    pcc_gc_cycle_requested_store(1);
    pcc_gc_trace_extension_roots_pending = 0;
    pcc_gc_trace_extension_roots_epoch = 0;
    pcc_gc_trace_extension_roots_backend = -1;
    pcc_gc_trace_cursor = NULL;
    pcc_gc_gray_count_store(0);
    __atomic_store_n(&pcc_gc_debt_bytes, 0, __ATOMIC_RELEASE);
    if (!pcc_gc_tracks_objects()) {
        pcc_gc_backend3_promotion_head = NULL;
        pcc_gc_backend3_promotion_tail = NULL;
        pcc_gc_backend3_promotion_revision = pcc_gc_object_list_revision;
        pcc_gc_backend4_reset_object_cursor = NULL;
        pcc_gc_backend3_remembered_scan_cursor = NULL;
        pcc_gc_backend3_remembered_scan_revision = 0;
        pcc_gc_object_list_revision_advance_unlocked();
        while (pcc_gc_objects != NULL) {
            PccGcObjectNode *next = pcc_gc_objects->next;
            free(pcc_gc_objects);
            pcc_gc_objects = next;
        }
        pcc_gc_backend3_young_head = NULL;
        pcc_gc_object_index_clear();
        __atomic_store_n(&pcc_gc_live_bytes, 0, __ATOMIC_RELEASE);
    }
    pcc_gc_graph_unlock();
    if (
        old_backend == PCC_GC_KIND_CONCURRENT_MARK_SWEEP
        || pcc_gc_selected_backend == PCC_GC_KIND_CONCURRENT_MARK_SWEEP
    ) {
        pcc_gc_cms_reset_queue_and_tls();
    }
    if (!pcc_gc_backend_uses_forwarding()) {
        pcc_gc_forwarding_clear_all();
        pcc_gc_identity_clear_all();
    }
    if (pcc_gc_selected_backend != PCC_GC_KIND_COLORED_RELOCATING) {
        pcc_gc_reset_relocation_set();
        pcc_gc_backend4_store_buffer_clear();
    }
    pcc_gc_cms_maybe_start_worker();
    return 0;
}

const char *pcc_gc_backend_name(int64_t backend) {
    switch (backend) {
        case PCC_GC_KIND_REFCOUNT_CYCLE:
            return "refcount-cycle";
        case PCC_GC_KIND_INCREMENTAL_TRICOLOR:
            return "incremental-tricolor";
        case PCC_GC_KIND_CONCURRENT_MARK_SWEEP:
            return "concurrent-mark-sweep";
        case PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR:
            return "generational-minor-major";
        case PCC_GC_KIND_COLORED_RELOCATING:
            return "colored-relocating";
        default:
            return "unknown";
    }
}

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

static void pcc_gc_backend4_clear_large_deferred_flags(void) {
    pcc_gc_graph_lock();
    for (PccGcObjectNode *n = pcc_gc_objects; n != NULL; n = n->next) {
        if (!pcc_gc_object_node_is_active(n)) continue;
        PyObject *o = n->obj;
        if (o == NULL || PY_IS_TAGGED_INT(o)) continue;
        PyObjectHeader *h = py_header(o);
        int32_t flags = py_header_flags_load(h);
        if ((flags & PY_FLAG_GC_LARGE_DEFERRED) != 0) {
            __atomic_add_fetch(
                &pcc_gc_backend4_large_object_reconsiderations_count,
                1,
                __ATOMIC_RELAXED
            );
            py_header_flags_and(h, ~PY_FLAG_GC_LARGE_DEFERRED);
        }
    }
    pcc_gc_graph_unlock();
}

static void pcc_gc_backend4_reseed_relocation_epoch_state(void) {
    if (pcc_gc_selected_backend != PCC_GC_KIND_COLORED_RELOCATING) return;
    int64_t owner = pcc_current_thread_id();
    if (owner <= 0) return;
    PccGcZPageEvacuationNode *prepared_nodes = NULL;
    int64_t prepared_count = 0;
    for (;;) {
        pcc_gc_graph_lock();
        if (pcc_gc_backend4_reseed_page_count_owner == 0) {
            pcc_gc_backend4_reseed_page_count_owner = owner;
            break;
        }
        if (pcc_gc_backend4_reseed_page_count_owner == owner) {
            pcc_gc_graph_unlock();
            return;
        }
        pcc_gc_graph_unlock();
        pcc_thread_safepoint();
    }
    for (;;) {
        int64_t required = 0;
        int64_t observed_revision = pcc_gc_backend4_reseed_page_revision;
        pcc_gc_backend4_reseed_page_count_cursor =
            pcc_gc_backend4_evacuation_pages;
        for (;;) {
            int64_t examined = 0;
            while (
                pcc_gc_backend4_reseed_page_count_cursor != NULL
                && examined < PCC_GC_SAFEPOINT_BATCH
            ) {
                pcc_gc_backend4_reseed_page_count_cursor =
                    pcc_gc_backend4_reseed_page_count_cursor->next;
                required++;
                examined++;
            }
            int complete =
                pcc_gc_backend4_reseed_page_count_cursor == NULL;
            if (pcc_gc_backend4_reseed_page_revision != observed_revision) {
                required = 0;
                observed_revision = pcc_gc_backend4_reseed_page_revision;
                pcc_gc_backend4_reseed_page_count_cursor =
                    pcc_gc_backend4_evacuation_pages;
                complete = 0;
            }
            if (complete) break;
            pcc_gc_graph_unlock();
            pcc_gc_backend4_reseed_plan_probe_wait(1);
            pcc_thread_safepoint();
            pcc_gc_graph_lock();
        }
        if (required > prepared_count) {
            pcc_gc_graph_unlock();
            pcc_gc_backend4_reseed_plan_probe_wait(1);
            prepared_count += pcc_gc_backend4_evacuation_page_nodes_prepare(
                &prepared_nodes, required - prepared_count
            );
            if (prepared_count < required) {
                pcc_gc_graph_lock();
                pcc_gc_backend4_reseed_page_count_cursor = NULL;
                pcc_gc_backend4_reseed_relocation_cursor = NULL;
                pcc_gc_backend4_reseed_commit_owner = 0;
                pcc_gc_backend4_reseed_page_count_owner = 0;
                pcc_gc_graph_unlock();
                pcc_gc_backend4_evacuation_page_finish_detached(
                    prepared_nodes
                );
                return;
            }
            pcc_gc_graph_lock();
            continue;
        }

        for (;;) {
            int64_t candidates = 0;
            int64_t candidate_bytes = 0;
            int64_t small_candidates = 0;
            int64_t medium_candidates = 0;
            int64_t small_bytes = 0;
            int64_t medium_bytes = 0;
            int64_t zpage_bytes = 0;
            int64_t small_zpage_bytes = 0;
            int64_t medium_zpage_bytes = 0;
            int64_t observed_relocation_revision =
                pcc_gc_backend4_reseed_relocation_revision;
            pcc_gc_backend4_reseed_relocation_cursor = pcc_gc_relocation_set;
            for (;;) {
                int64_t examined = 0;
                while (
                    pcc_gc_backend4_reseed_relocation_cursor != NULL
                    && examined < PCC_GC_SAFEPOINT_BATCH
                ) {
                    PccGcRelocationNode *n =
                        pcc_gc_backend4_reseed_relocation_cursor;
                    pcc_gc_backend4_reseed_relocation_cursor = n->next;
                    int64_t size = pcc_gc_known_object_size_unlocked(n->obj);
                    if (size > 0) {
                        candidates++;
                        candidate_bytes += size;
                        if (size <= PCC_GC_BACKEND4_SMALL_PAGE_LIMIT) {
                            small_candidates++;
                            small_bytes += size;
                        } else if (
                            size <= PCC_GC_BACKEND4_MEDIUM_PAGE_LIMIT
                        ) {
                            medium_candidates++;
                            medium_bytes += size;
                        }
                    }
                    examined++;
                }
                int complete =
                    pcc_gc_backend4_reseed_relocation_cursor == NULL;
                if (
                    pcc_gc_backend4_reseed_relocation_revision
                    != observed_relocation_revision
                ) {
                    candidates = 0;
                    candidate_bytes = 0;
                    small_candidates = 0;
                    medium_candidates = 0;
                    small_bytes = 0;
                    medium_bytes = 0;
                    observed_relocation_revision =
                        pcc_gc_backend4_reseed_relocation_revision;
                    pcc_gc_backend4_reseed_relocation_cursor =
                        pcc_gc_relocation_set;
                    complete = 0;
                }
                if (complete) break;
                pcc_gc_graph_unlock();
                pcc_gc_backend4_reseed_plan_probe_wait(2);
                pcc_thread_safepoint();
                pcc_gc_graph_lock();
            }

            /* Freeze candidate admission and relocation commit while the
             * already-published evacuation list is read in bounded batches.
             * The cursor is repaired by every unlink path before a node or
             * its page storage can be recycled; no raw page pointer crosses
             * the unlock below. */
            pcc_gc_backend4_reseed_commit_owner = owner;
            int64_t observed_page_revision =
                pcc_gc_backend4_reseed_page_revision;
            int64_t observed_commit_relocation_revision =
                pcc_gc_backend4_reseed_relocation_revision;
            int restart_commit = 0;
            pcc_gc_backend4_reseed_page_count_cursor =
                pcc_gc_backend4_evacuation_pages;
            for (;;) {
                int64_t examined = 0;
                while (
                    pcc_gc_backend4_reseed_page_count_cursor != NULL
                    && examined < PCC_GC_SAFEPOINT_BATCH
                ) {
                    PccGcZPageEvacuationNode *page_node =
                        pcc_gc_backend4_reseed_page_count_cursor;
                    pcc_gc_backend4_reseed_page_count_cursor =
                        page_node->next;
                    PccGcZPage *page = page_node->page;
                    if (page != NULL) {
                        int64_t page_bytes = page->used_bytes;
                        if (page_bytes > 0) {
                            zpage_bytes += page_bytes;
                            if (page->page_class == 0) {
                                small_zpage_bytes += page_bytes;
                            } else if (page->page_class == 1) {
                                medium_zpage_bytes += page_bytes;
                            }
                        }
                    }
                    examined++;
                }
                int complete =
                    pcc_gc_backend4_reseed_page_count_cursor == NULL;
                if (
                    pcc_gc_backend4_reseed_page_revision
                        != observed_page_revision
                    || pcc_gc_backend4_reseed_relocation_revision
                        != observed_commit_relocation_revision
                    || pcc_gc_backend4_relocation_reset_owner != 0
                ) {
                    restart_commit = 1;
                    break;
                }
                if (complete) break;
                pcc_gc_graph_unlock();
                pcc_gc_backend4_reseed_plan_probe_wait(4);
                pcc_thread_safepoint();
                pcc_gc_graph_lock();
            }
            if (restart_commit) {
                pcc_gc_backend4_reseed_page_count_cursor = NULL;
                pcc_gc_backend4_reseed_relocation_cursor = NULL;
                pcc_gc_graph_unlock();
                pcc_thread_safepoint();
                pcc_gc_graph_lock();
                continue;
            }

            __atomic_store_n(
                &pcc_gc_backend4_evacuation_candidates,
                candidates,
                __ATOMIC_RELAXED
            );
            __atomic_store_n(
                &pcc_gc_backend4_evacuation_candidate_bytes_count,
                candidate_bytes,
                __ATOMIC_RELAXED
            );
            __atomic_store_n(
                &pcc_gc_backend4_small_page_candidates,
                small_candidates,
                __ATOMIC_RELAXED
            );
            __atomic_store_n(
                &pcc_gc_backend4_medium_page_candidates,
                medium_candidates,
                __ATOMIC_RELAXED
            );
            __atomic_store_n(
                &pcc_gc_backend4_small_page_candidate_bytes_count,
                small_bytes,
                __ATOMIC_RELAXED
            );
            __atomic_store_n(
                &pcc_gc_backend4_medium_page_candidate_bytes_count,
                medium_bytes,
                __ATOMIC_RELAXED
            );
            __atomic_store_n(
                &pcc_gc_backend4_evacuation_candidate_zpage_bytes_count,
                zpage_bytes,
                __ATOMIC_RELAXED
            );
            __atomic_store_n(
                &pcc_gc_backend4_small_page_candidate_zpage_bytes_count,
                small_zpage_bytes,
                __ATOMIC_RELAXED
            );
            __atomic_store_n(
                &pcc_gc_backend4_medium_page_candidate_zpage_bytes_count,
                medium_zpage_bytes,
                __ATOMIC_RELAXED
            );
            pcc_gc_backend4_reseed_page_count_cursor = NULL;
            pcc_gc_backend4_reseed_relocation_cursor = NULL;
            pcc_gc_backend4_reseed_commit_owner = 0;
            pcc_gc_backend4_reseed_page_count_owner = 0;
            pcc_gc_graph_unlock();
            pcc_gc_backend4_evacuation_page_finish_detached(prepared_nodes);
            return;
        }
    }
}

void pcc_gc_telemetry_reset(void) {
    pcc_gc_init_config();
    PccGcRememberedOwnerNode *detached_remembered = NULL;
    pcc_gc_graph_lock();
    detached_remembered =
        pcc_gc_backend3_remembered_owners_clear_unlocked();
    pcc_gc_graph_unlock();
    pcc_gc_backend3_finish_detached_remembered_owners(detached_remembered);
    for (int i = 0; i <= PCC_GC_COUNTER_WORK_STEPS; i++) {
        __atomic_store_n(&pcc_gc_metrics[i], 0, __ATOMIC_RELAXED);
    }
    __atomic_store_n(&pcc_gc_unmanaged_refcount_ops, 0, __ATOMIC_RELAXED);
    __atomic_store_n(&pcc_gc_max_pause_us, 0, __ATOMIC_RELAXED);
    __atomic_store_n(&pcc_gc_pause_count, 0, __ATOMIC_RELAXED);
    __atomic_store_n(&pcc_gc_pause_sum_us, 0, __ATOMIC_RELAXED);
    for (int hi = 0; hi < 4; hi++) {
        __atomic_store_n(&pcc_gc_pause_hist[hi], 0, __ATOMIC_RELAXED);
    }
    __atomic_store_n(&pcc_gc_minor_allocations, 0, __ATOMIC_RELAXED);
    __atomic_store_n(&pcc_gc_minor_collections, 0, __ATOMIC_RELAXED);
    __atomic_store_n(&pcc_gc_minor_bytes, 0, __ATOMIC_RELAXED);
    __atomic_store_n(&pcc_gc_minor_arena_refills, 0, __ATOMIC_RELAXED);
    __atomic_store_n(&pcc_gc_minor_arena_bumps, 0, __ATOMIC_RELAXED);
    __atomic_store_n(&pcc_gc_minor_arena_fallbacks, 0, __ATOMIC_RELAXED);
    __atomic_store_n(&pcc_gc_cms_queue_pushes, 0, __ATOMIC_RELAXED);
    __atomic_store_n(&pcc_gc_cms_worker_drains, 0, __ATOMIC_RELAXED);
    __atomic_store_n(&pcc_gc_cms_mutator_assists, 0, __ATOMIC_RELAXED);
    __atomic_store_n(&pcc_gc_cms_worker_traces, 0, __ATOMIC_RELAXED);
    __atomic_store_n(&pcc_gc_cms_worker_stops, 0, __ATOMIC_RELAXED);
    __atomic_store_n(&pcc_gc_cms_wb_flushes, 0, __ATOMIC_RELAXED);
    pcc_gc_relocation_forwards = 0;
    pcc_gc_relocation_barrier_forwards = 0;
    pcc_gc_relocation_pin_rejects = 0;
    __atomic_store_n(
        &pcc_gc_backend4_genzgc_store_barriers, 0, __ATOMIC_RELAXED
    );
    pcc_gc_backend4_reset_store_buffer_epoch_state();
    __atomic_store_n(
        &pcc_gc_backend4_young_promotions, 0, __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_evacuation_candidates, 0, __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_evacuated_bytes_count, 0, __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_large_object_defers, 0, __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_candidate_fresh_skips, 0, __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_relocation_add_refusals, 0, __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_large_object_deferred_bytes_count, 0, __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_large_object_reconsiderations_count, 0, __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_small_page_candidates, 0, __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_medium_page_candidates, 0, __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_evacuation_candidate_bytes_count, 0, __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_small_page_candidate_bytes_count, 0, __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_medium_page_candidate_bytes_count, 0, __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_evacuation_candidate_zpage_bytes_count,
        0,
        __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_small_page_candidate_zpage_bytes_count,
        0,
        __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_medium_page_candidate_zpage_bytes_count,
        0,
        __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_store_buffer_drain_batches_count, 0, __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_store_buffer_drained_entries_count, 0, __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_store_buffer_duplicate_skips_count, 0, __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_store_buffer_incomplete_drains_count,
        0,
        __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_evacuation_incomplete_batches_count,
        0,
        __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_store_buffer_max_batch_size_count,
        0,
        __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_store_buffer_full_batches_count,
        0,
        __ATOMIC_RELAXED
    );
    pcc_gc_backend4_reset_remembered_set_epoch_state();
    __atomic_store_n(
        &pcc_gc_backend4_remembered_set_duplicate_skips_count,
        0,
        __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_store_buffer_medium_flushes_count,
        0,
        __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_store_buffer_medium_flushed_entries_count,
        0,
        __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_store_buffer_medium_full_flushes_count,
        0,
        __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_store_buffer_cross_thread_medium_flushes_count,
        0,
        __ATOMIC_RELAXED
    );
    __atomic_store_n(
        &pcc_gc_backend4_store_buffer_cross_thread_medium_flushed_entries_count,
        0,
        __ATOMIC_RELAXED
    );
    pcc_gc_backend4_reseed_relocation_epoch_state();
    pcc_gc_backend4_clear_large_deferred_flags();
}

int64_t pcc_gc_backend2_worker_buffer_score(void) {
    return __atomic_load_n(&pcc_gc_cms_queue_pushes, __ATOMIC_RELAXED);
}

int64_t pcc_gc_backend2_production_score(void) {
    return pcc_gc_backend2_worker_buffer_score()
        + __atomic_load_n(&pcc_gc_cms_worker_starts, __ATOMIC_RELAXED);
}

int64_t pcc_gc_backend3_minor_productivity_score(void) {
    return __atomic_load_n(&pcc_gc_minor_arena_refills, __ATOMIC_RELAXED)
        + __atomic_load_n(&pcc_gc_minor_arena_bumps, __ATOMIC_RELAXED);
}

int64_t pcc_gc_backend3_remembered_update_score(void) {
    return pcc_gc_backend3_minor_productivity_score();
}

static int pcc_gc_is_tracing_backend(void) {
    return pcc_gc_selected_backend == PCC_GC_KIND_INCREMENTAL_TRICOLOR
        || pcc_gc_selected_backend == PCC_GC_KIND_CONCURRENT_MARK_SWEEP;
}

static int pcc_gc_tracks_objects(void) {
    return pcc_gc_selected_backend != PCC_GC_KIND_REFCOUNT_CYCLE;
}

static int pcc_gc_backend3_graph_leaf_tag(int32_t tag) {
    switch (tag) {
        case PY_TYPE_NONE:
        case PY_TYPE_BOOL:
        case PY_TYPE_INT:
        case PY_TYPE_FLOAT:
        case PY_TYPE_STR:
        case PY_TYPE_COMPLEX:
        case PY_TYPE_BYTES:
        case PY_TYPE_BYTEARRAY:
        case PY_TYPE_CPY_HANDLE:
            return 1;
        default:
            return 0;
    }
}

static int pcc_gc_is_known_object(PyObject *o) {
    if (!pcc_gc_tracks_objects()) return 0;
    if (o == NULL || PY_IS_TAGGED_INT(o)) return 0;
    PccGcObjectNode *indexed = (PccGcObjectNode *)pcc_gc_object_index_find(o);
    if (indexed != NULL) {
        return pcc_gc_object_node_is_active(indexed) ? 1 : 0;
    }
    /* The object index is authoritative; the historical O(N) list
     * fallback turned every index miss into a full-heap walk (95%+ of
     * cc-tier gc4 churn wall time through the resolve-first paths). */
    return 0;
}

int64_t pcc_gc_object_is_known_no_lock(PyObject *obj) {
    return pcc_gc_is_known_object(obj) ? 1 : 0;
}

int64_t pcc_gc_granule_s2_candidate_positive(PyObject *obj) {
    /* Expose the same fail-closed exact-positive predicate used by S2. */
    if (obj == NULL || PY_IS_TAGGED_INT(obj)) return 0;
    return pcc_gc_granule_is_object_start(obj) == 1 ? 1 : 0;
}

int64_t pcc_gc_pointer_is_managed_no_lock(PyObject *obj) {
    if (obj == NULL || PY_IS_TAGGED_INT(obj)) return 0;
    if (
        obj == py_None
        || obj == py_NotImplemented
        || obj == py_True
        || obj == py_False
    ) {
        return 1;
    }
    /* Only a fully initialized LIVE object-family slot is an exact positive.
     * Unknown, reserved, free, raw, large, foreign and moving-arena/zpage
     * addresses continue through every pre-existing provenance source. */
    if (pcc_gc_granule_is_object_start(obj) == 1) return 1;
    /* Ordering matters and is not arbitrary.  This is a disjunction of
     * side-effect-free lookups, so any order returns the same answer, but the
     * costs differ by an order of magnitude: the managed-pointer index is one
     * hash probe and is the case that actually hits, while
     * pcc_capi_is_type_object_value walks a linear list of every registered
     * builtin type object and almost always fails.  With the scan first, every
     * GC barrier in the program paid that walk before reaching the answer.
     * Mirrors the pcc-Python port in py/py_gc_backend.py. */
    if (pcc_gc_managed_pointer_index_contains(obj) != 0) return 1;
    /* The object/forwarding indexes compare pointer values only.  They are
     * safe for arbitrary raw C pointers and must precede every header read. */
    if (pcc_gc_object_index_find(obj) != NULL) return 1;
    if (pcc_capi_is_type_object_value(obj) != 0) return 1;
    if (pcc_gc_forwarding_find(obj) != NULL) return 1;
    if (pcc_gc_forwarding_target_exists(obj)) return 1;
    return 0;
}

int64_t pcc_gc_pointer_is_managed(PyObject *obj) {
    /* Answer the value-only cases before taking the graph lock, mirroring the
     * pcc-Python port.  pcc_gc_pointer_is_managed_no_lock returns 0 for
     * exactly these inputs as its first act and both tests read only the
     * pointer bits, so hoisting them cannot change the answer.  This is the
     * hot path: _ptr_is_class/_ptr_is_instance run it on every attribute
     * access and method dispatch, and a tagged small int is a very common
     * argument there.  pcc_gc_pointer_register below already had this. */
    if (obj == NULL || PY_IS_TAGGED_INT(obj)) return 0;
    /* Hoisting the four immortal-singleton compares ahead of the granule probe
     * was measured and DENIED on 2026-09-06 (+3.2% instructions on the
     * cli_bootstrap ASM worker; singleton traffic too rare to pay for four
     * extra compares on every probe).  Mirrors py/py_gc_backend.py. */
    /* LIVE is release-published only after header initialization.  Accept the
     * exact positive before locking; every non-positive result takes the graph
     * lock and executes the complete historical provenance chain. */
    if (pcc_gc_granule_is_object_start(obj) == 1) return 1;
    pcc_gc_graph_lock();
    int64_t managed = pcc_gc_pointer_is_managed_no_lock(obj);
    pcc_gc_graph_unlock();
    return managed;
}

int64_t pcc_gc_pointer_register(PyObject *obj) {
    if (obj == NULL || PY_IS_TAGGED_INT(obj)) return -1;
    int64_t granule_result = pcc_gc_granule_object_publish(obj);
    if (granule_result < 0) return -1;
    if (granule_result > 0) return 0;
    pcc_gc_graph_lock();
    int64_t result = pcc_gc_managed_pointer_index_insert(obj);
    pcc_gc_graph_unlock();
    return result;
}

int64_t pcc_gc_pointer_unregister(PyObject *obj) {
    if (obj == NULL || PY_IS_TAGGED_INT(obj)) return 0;
    int64_t granule_was_live = pcc_gc_granule_is_object_start(obj);
    int64_t granule_result = pcc_gc_granule_object_retire(obj);
    if (granule_result < 0) return -1;
    if (granule_result > 0) {
        /* Constructor/error cleanup can retire a RESERVED/FREE cell after
         * note_object_freeing conservatively inserted an exact key.  Remove
         * that key before the address is eligible for reuse. */
        if (granule_was_live != 1) {
            pcc_gc_graph_lock();
            (void)pcc_gc_managed_pointer_index_remove(obj);
            pcc_gc_graph_unlock();
        }
        return 0;
    }
    pcc_gc_graph_lock();
    int64_t result = pcc_gc_managed_pointer_index_remove(obj);
    pcc_gc_graph_unlock();
    return result;
}

/* Backend-4 read-barrier safe candidate decision (G-P0-LONGRUN exit UAF).
 *
 * The read barrier used to decide "does this slot value need relocation
 * resolution?" by loading the value's header flags directly
 * (py_gc_relocation_candidate -> py_header_flags_load). Under backend #4
 * churn a slot can hold a STALE reference: a freed malloc'd child, or an
 * old copy whose address the object index / forwarding table never mapped.
 * The address heuristic (py_pointer_can_have_header) cannot tell a
 * plausible-looking-but-unmapped address apart from a live one, so the
 * header load faulted at exit-time list dealloc (No.6/No.9 in
 * docs/investigations/gc-backend4-churn-exit-list-item-uaf.md).
 *
 * The object index and forwarding table are pointer-VALUE hash lookups; they
 * never dereference the pointer. Consult them FIRST and only touch the
 * header of a proven-mapped (known-live) object:
 *   - forwarded stale reference   -> resolve (no header deref)
 *   - known-live object           -> safe to read its candidate flag
 *   - unknown & unforwarded        -> a dead pointer leaked into the slot;
 *                                     do NOT dereference, do not resolve.
 * This bounds the stale-borrow window (No.10 case 3) without a fault and
 * without weakening relocation semantics for live objects. */
int64_t pcc_gc_backend4_slot_needs_resolve(PyObject *value) {
    if (value == NULL || PY_IS_TAGGED_INT(value)) return 0;
    if (pcc_gc_forwarding_find(value) != NULL) return 1;
    if (pcc_gc_is_known_object(value)) {
        return (
            py_header_flags_load(py_header(value))
            & PY_FLAG_GC_RELOCATION_CANDIDATE
        ) != 0 ? 1 : 0;
    }
    return 0;
}

int64_t pcc_gc_object_is_known(PyObject *obj) {
    pcc_gc_graph_lock();
    int64_t known = pcc_gc_is_known_object(obj) ? 1 : 0;
    pcc_gc_graph_unlock();
    return known;
}

static int pcc_gc_is_sweep_candidate(PyObject *o) {
    if (!pcc_gc_tracks_objects()) return 0;
    if (o == NULL || PY_IS_TAGGED_INT(o)) return 0;
    if (!pcc_gc_is_known_object(o)) return 0;
    PyObjectHeader *h = py_header(o);
    return (py_header_flags_load(h) & PY_FLAG_GC_SWEEP_CANDIDATE) != 0;
}

static int pcc_gc_has_sweep_candidate_unlocked(void) {
    if (!pcc_gc_tracks_objects()) return 0;
    for (PccGcObjectNode *n = pcc_gc_objects; n != NULL; n = n->next) {
        if (!pcc_gc_object_node_is_active(n)) continue;
        PyObject *o = n->obj;
        if (
            (py_header_flags_load(py_header(o)) & PY_FLAG_GC_SWEEP_CANDIDATE)
            != 0
        ) {
            return 1;
        }
    }
    return 0;
}

static int pcc_gc_has_sweep_candidate(void) {
    pcc_gc_graph_lock();
    int has_candidate = pcc_gc_has_sweep_candidate_unlocked();
    pcc_gc_graph_unlock();
    return has_candidate;
}

static void pcc_gc_clear_slot(PyObject **slot) {
    if (slot == NULL) return;
    PyObject *child = *slot;
    *slot = NULL;
    if (child == NULL || PY_IS_TAGGED_INT(child)) return;
    if (pcc_gc_is_sweep_candidate(child)) return;
    py_decref(child);
}

static void pcc_gc_clear_owned_slot(
    PyObject **slot,
    int32_t role,
    void *ctx
) {
    (void)ctx;
    if (role != PY_OBJ_SLOT_OWNED) return;
    pcc_gc_clear_slot(slot);
}

static void pcc_gc_clear_referents(PyObject *o) {
    if (o == NULL || PY_IS_TAGGED_INT(o)) return;
    int32_t tag = py_header(o)->type_tag;
    (void)py_obj_visit_slots(o, pcc_gc_clear_owned_slot, NULL);
    if (tag == PY_TYPE_LIST) {
        PyListObject *l = (PyListObject *)o;
        l->length = 0;
    } else if (tag == PY_TYPE_TUPLE) {
        PyTupleObject *t = (PyTupleObject *)o;
        t->len = 0;
    } else if (tag == PY_TYPE_DICT) {
        PyDictObject *d = (PyDictObject *)o;
        if (d->entries != NULL) {
            for (int64_t i = 0; i < d->entries_used; i++) {
                DictEntry *e = &d->entries[i];
                e->hash = 0;
            }
        }
        if (d->indices != NULL) {
            for (int64_t i = 0; i < d->capacity; i++) {
                d->indices[i] = PY_DICT_EMPTY;
            }
        }
        d->size = 0;
        d->entries_used = 0;
    } else if (tag == PY_TYPE_SET) {
        PySetObject *s = (PySetObject *)o;
        if (s->entries != NULL) {
            for (int64_t i = 0; i < s->capacity; i++) {
                s->entries[i].key = NULL;
                s->entries[i].hash = 0;
            }
        }
        s->size = 0;
        s->fill = 0;
    } else if (tag == PY_TYPE_VTHREAD_CHANNEL) {
        PyVThreadChannelObject *channel = (PyVThreadChannelObject *)o;
        if (channel->kind == PCC_VTHREAD_CHANNEL_KIND_CORE) {
            PyVThreadChannelCoreObject *core =
                (PyVThreadChannelCoreObject *)o;
            core->length = 0;
            core->head = 0;
            core->tail = 0;
        } else if (
            channel->kind == PCC_VTHREAD_CHANNEL_KIND_SENDER
            || channel->kind == PCC_VTHREAD_CHANNEL_KIND_RECEIVER
        ) {
            ((PyVThreadChannelEndpointObject *)o)->closed = 1;
        }
    }
}

/* PASS-1 of the two-phase sweep: invalidate weakrefs and clear referent slots
 * WITHOUT freeing or clearing the sweep-candidate flag, so every pending object
 * stays a recognizable sweep candidate while cycles are broken. Mirror of the
 * pcc-Python port _clear_unreachable. See investigation
 * gc-5backend-object-lifetime-contract-no-libpython.md. */
static void pcc_gc_clear_unreachable(PyObject *o) {
    if (o == NULL || PY_IS_TAGGED_INT(o)) return;
    py_weakref_invalidate(o);
    pcc_gc_clear_referents(o);
}

/* PASS-2 of the two-phase sweep: free an object whose referents were ALREADY
 * cleared by pcc_gc_clear_unreachable. Must not run before every pending
 * object has been cleared. */
static void pcc_gc_finalize_unreachable(PyObject *o) {
    if (o == NULL || PY_IS_TAGGED_INT(o)) return;
    PyObjectHeader *h = py_header(o);
    int32_t flags = py_header_flags_load(h);
    int delay_zpage_freeing_note = (
        pcc_gc_selected_backend == PCC_GC_KIND_COLORED_RELOCATING
        && (flags & PY_FLAG_GC_ZPAGE_ALLOC) != 0
    );
    /* Publish logical death before any type-specific deallocator can reach a
     * safepoint.  Refcount-zero forwarding shells do not set this bit; only an
     * object that is actually entering finalization does. */
    py_header_flags_or(h, PY_FLAG_GC_DEALLOCATING);
    if (!delay_zpage_freeing_note) {
        pcc_gc_note_object_freeing(o);
    }
    pcc_refcount_forget(&h->refcount);
    py_gc_untrack(o);
    h->refcount = 0;
    switch (h->type_tag) {
        case PY_TYPE_INT:       py_dealloc_int(o);       break;
        case PY_TYPE_FLOAT:     py_dealloc_float(o);     break;
        case PY_TYPE_STR:       py_dealloc_str(o);       break;
        case PY_TYPE_LIST:      py_dealloc_list(o);      break;
        case PY_TYPE_TUPLE:     py_dealloc_tuple(o);     break;
        case PY_TYPE_DICT:      py_dealloc_dict(o);      break;
        case PY_TYPE_SET:       py_dealloc_set(o);       break;
        case PY_TYPE_FUNC:      py_dealloc_func(o);      break;
        case PY_TYPE_CLASS:     py_class_dealloc(o);     break;
        case PY_TYPE_INSTANCE:  py_instance_dealloc(o);  break;
        case PY_TYPE_EXC:       py_dealloc_exc(o);       break;
        case PY_TYPE_FILE:      py_dealloc_file(o);      break;
        case PY_TYPE_ITER:      py_dealloc_iter(o);      break;
        case PY_TYPE_GEN:       py_dealloc_gen(o);       break;
        case PY_TYPE_COROUTINE: py_dealloc_coroutine(o); break;
        case PY_TYPE_CONTINUATION: py_dealloc_continuation(o); break;
        case PY_TYPE_TASK:      py_dealloc_task(o);      break;
        case PY_TYPE_VIRTUAL_THREAD: py_dealloc_virtual_thread(o); break;
        case PY_TYPE_VTHREAD_CHANNEL: py_dealloc_vthread_channel(o); break;
        case PY_TYPE_MEMORYVIEW: py_dealloc_memoryview(o); break;
        case PY_TYPE_WEAKREF:   py_dealloc_weakref(o);   break;
        case PY_TYPE_THREAD_LOCK: py_dealloc_thread_lock(o); break;
        case PY_TYPE_THREAD_RLOCK: py_dealloc_thread_rlock(o); break;
        case PY_TYPE_THREAD_EVENT: py_dealloc_thread_event(o); break;
        case PY_TYPE_THREAD_CONDITION: py_dealloc_thread_condition(o); break;
        case PY_TYPE_THREAD_SEMAPHORE: py_dealloc_thread_semaphore(o); break;
        case PY_TYPE_THREAD: py_dealloc_thread_thread(o); break;
        case PY_TYPE_CPY_HANDLE: py_dealloc_cpy_handle(o); break;
        case PY_TYPE_PROPERTY:
        case PY_TYPE_CLASSMETHOD:
        case PY_TYPE_STATICMETHOD: py_descriptor_dealloc(o); break;
        default:
            if (pcc_capi_dealloc_cext_object(o, (int64_t)h->type_tag) != 0) break;
            if (h->type_tag >= PY_TYPE_USER_CLASS_START) py_instance_dealloc(o);
            else py_dealloc_generic(o);
            break;
    }
    if (delay_zpage_freeing_note) {
        pcc_gc_note_object_freeing(o);
    }
}

static void pcc_gc_recheck_reachability_after_finalizers(void);

static int64_t pcc_gc_sweep_unreachable(int64_t budget) {
    if (budget <= 0) return 0;
    /* PASS 0 (CPython PEP 442), mirror of the port: run __del__ on unreachable
     * members BEFORE any clear/free, while their fields are intact.
     * py_user_del_dispatch runs __del__ at most once and sets PY_FLAG_FINALIZED,
     * so the PASS-2 dealloc does not re-run it. See investigation
     * gc-5backend-cycle-finalizer-not-run-no-libpython.md. */
    for (PccGcObjectNode *fn = pcc_gc_objects; fn != NULL; fn = fn->next) {
        if (!pcc_gc_object_node_is_active(fn)) continue;
        PyObject *o = fn->obj;
        int32_t flags = py_header_flags_load(py_header(o));
        if ((flags & PY_FLAG_GC_SWEEP_CANDIDATE) != 0 &&
            (flags & (PY_FLAG_GC_PINNED | PY_FLAG_GC_FRESH_ALLOC)) == 0 &&
            pcc_capi_is_cext_type_tag((int64_t)py_header(o)->type_tag) == 0) {
            py_user_del_dispatch(o);
        }
    }
    /* PEP 442: after finalizers, exclude any object a __del__ resurrected from
     * the clear/free passes below. Mirror of the pcc-Python port. */
    pcc_gc_recheck_reachability_after_finalizers();
    /* Two-phase sweep (clear-then-free), mirror of the pcc-Python port. PASS 1
     * clears the referents of up to `budget` unreachable objects WITHOUT
     * freeing any, so every still-pending object keeps PY_FLAG_GC_SWEEP_CANDIDATE
     * and pcc_gc_clear_slot's candidate guard skips the decref of sibling cycle
     * members. PASS 2 frees the SAME objects (same list order + flag filter,
     * flags unchanged by PASS 1). Interleaved clear+free (the old single pass)
     * caused a use-after-free / refcount underflow when one unreachable object
     * referenced another that had already been finalized. See investigation
     * gc-5backend-object-lifetime-contract-no-libpython.md. */
    int64_t cleared = 0;
    PccGcObjectNode *n = pcc_gc_objects;
    while (n != NULL && cleared < budget) {
        PccGcObjectNode *next = n->next;
        if (pcc_gc_object_node_is_active(n)) {
            PyObject *o = n->obj;
            int32_t flags = py_header_flags_load(py_header(o));
            if ((flags & PY_FLAG_GC_SWEEP_CANDIDATE) != 0 &&
                (flags & (PY_FLAG_GC_PINNED | PY_FLAG_GC_FRESH_ALLOC)) == 0) {
                pcc_gc_clear_unreachable(o);
                cleared++;
            }
        }
        n = next;
    }
    int64_t reclaimed = 0;
    n = pcc_gc_objects;
    while (n != NULL) {
        PccGcObjectNode *next = n->next;
        if (pcc_gc_object_node_is_active(n)) {
            PyObject *o = n->obj;
            PyObjectHeader *h = py_header(o);
            int32_t flags = py_header_flags_load(h);
            if ((flags & PY_FLAG_GC_SWEEP_CANDIDATE) != 0) {
                if ((flags & (PY_FLAG_GC_PINNED | PY_FLAG_GC_FRESH_ALLOC)) != 0) {
                    py_header_flags_and(h, ~PY_FLAG_GC_SWEEP_CANDIDATE);
                } else if (reclaimed < cleared) {
                    pcc_gc_finalize_unreachable(o);
                    reclaimed++;
                }
            }
        }
        n = next;
    }
    return reclaimed;
}

static void pcc_gc_gray_object(PyObject *o) {
    if (o != NULL && !PY_IS_TAGGED_INT(o)) {
        PccGcForwardNode *forwarding = pcc_gc_forwarding_find(o);
        if (forwarding != NULL && forwarding->to != NULL) {
            o = forwarding->to;
        }
    }
    if (!pcc_gc_is_known_object(o)) return;
    PyObjectHeader *h = py_header(o);
    int32_t flags = py_header_flags_load(h);
    if ((flags & PY_FLAG_GC_BLACK) != 0) return;
    if ((flags & PY_FLAG_GC_GRAY) == 0) {
        pcc_gc_gray_count_inc();
    }
    py_header_flags_update(h, PY_FLAG_GC_COLOR_MASK, PY_FLAG_GC_GRAY);
}

static void pcc_gc_gray_root_object(PyObject *o) {
    if (o != NULL && !PY_IS_TAGGED_INT(o)) {
        PccGcForwardNode *forwarding = pcc_gc_forwarding_find(o);
        if (forwarding != NULL && forwarding->to != NULL) {
            o = forwarding->to;
        }
    }
    if (!pcc_gc_is_known_object(o)) return;
    PyObjectHeader *h = py_header(o);
    int32_t flags = py_header_flags_load(h);
    if ((flags & PY_FLAG_GC_GRAY) == 0) {
        pcc_gc_gray_count_inc();
    }
    py_header_flags_update(h, PY_FLAG_GC_COLOR_MASK, PY_FLAG_GC_GRAY);
}

static PyObject *pcc_gc_resolve_root_slot_unlocked(PyObject **slot) {
    if (slot == NULL) return NULL;
    PyObject *value = *slot;
    if (value == NULL || PY_IS_TAGGED_INT(value)) return value;
    if (!pcc_gc_is_known_object(value)) {
        PccGcForwardNode *unknown_forwarding = pcc_gc_forwarding_find(value);
        if (unknown_forwarding == NULL || unknown_forwarding->to == NULL) {
            return value;
        }
        PyObject *unknown_resolved = unknown_forwarding->to;
        if (unknown_resolved == value) return value;
        if (pcc_gc_selected_backend == PCC_GC_KIND_COLORED_RELOCATING) {
            *slot = unknown_resolved;
            return unknown_resolved;
        }
        py_incref(unknown_resolved);
        *slot = unknown_resolved;
        py_decref(value);
        return unknown_resolved;
    }
    PyObjectHeader *h = py_header(value);
    int32_t flags = py_header_flags_load(h);
    if ((flags & PY_FLAG_GC_RELOCATION_CANDIDATE) == 0) return value;
    PccGcForwardNode *forwarding = pcc_gc_forwarding_find(value);
    if (forwarding == NULL || forwarding->to == NULL) {
        py_header_flags_and(h, ~PY_FLAG_GC_RELOCATION_CANDIDATE);
        return value;
    }
    PyObject *resolved = forwarding->to;
    if (resolved == value) {
        py_header_flags_and(h, ~PY_FLAG_GC_RELOCATION_CANDIDATE);
        return value;
    }
    if (pcc_gc_selected_backend == PCC_GC_KIND_COLORED_RELOCATING) {
        *slot = resolved;
        return resolved;
    }
    py_incref(resolved);
    *slot = resolved;
    py_decref(value);
    return resolved;
}

static void pcc_gc_mark_forwarded_source_inactive(PyObject *from) {
    if (from == NULL || PY_IS_TAGGED_INT(from)) return;
    PccGcObjectNode *n = (PccGcObjectNode *)pcc_gc_object_index_find(from);
    if (n == NULL) {
        for (PccGcObjectNode *scan = pcc_gc_objects; scan != NULL; scan = scan->next) {
            if (scan->obj == from) {
                n = scan;
                break;
            }
        }
    }
    if (n == NULL || pcc_gc_object_node_is_freeing(n)) return;
    if (n->size > 0) pcc_gc_live_bytes_subtract(n->size);
    pcc_gc_backend4_zpage_remove_unlocked(from);
    pcc_gc_object_node_set_freeing(n);
}

static void pcc_gc_retire_forwarded_source_into_finish_unlocked(
    PyObject *from,
    PccGcBackend4RemapFinish *finish
) {
    if (from == NULL || PY_IS_TAGGED_INT(from) || finish == NULL) return;
    PccGcIdentityNode *identity = pcc_gc_identity_detach(from);
    if (identity != NULL) {
        identity->next = finish->identities;
        finish->identities = identity;
    }
    /* Moving collectors can fall back to ordinary object-family slabs.  Such
     * a source is represented by its LIVE granule marker rather than by the
     * exact set, so retirement must clear that marker.  cc-mode reports
     * unknown and therefore preserves the exact-set oracle path unchanged. */
    int64_t granule_was_live = pcc_gc_granule_is_object_start(from);
    int64_t retire_result = pcc_gc_granule_object_retire(from);
    if (
        retire_result < 0
        || (retire_result > 0 && granule_was_live != 1)
    ) {
        PCC_GC_DEFER_TRIPWIRE(
            0,
            "forwarded-source granule retirement invariant violated"
        );
        return;
    }
    if (retire_result == 0) {
        (void)pcc_gc_managed_pointer_index_remove(from);
    }
    PccGcObjectNode *dead =
        (PccGcObjectNode *)pcc_gc_object_index_find(from);
    if (dead == NULL) {
        for (
            PccGcObjectNode *scan = pcc_gc_objects;
            scan != NULL;
            scan = scan->next
        ) {
            if (scan->obj == from) {
                dead = scan;
                break;
            }
        }
    }
    if (dead == NULL) return;
    if (!pcc_gc_object_node_is_freeing(dead) && dead->size > 0) {
        pcc_gc_live_bytes_subtract(dead->size);
    }
    (void)pcc_gc_object_index_remove(from);
    pcc_gc_object_node_unlink(dead);
    dead->next = finish->object_nodes;
    finish->object_nodes = dead;
}

static void pcc_gc_retire_forwarded_source_unlocked(PyObject *from) {
    PccGcBackend4RemapFinish finish = {0};
    pcc_gc_retire_forwarded_source_into_finish_unlocked(from, &finish);
    pcc_gc_backend4_finish_remap_retirement(&finish);
}

static PyObject *pcc_gc_generational_oldify_copy(PyObject *from) {
    if (
        pcc_gc_selected_backend != PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
        || from == NULL
        || PY_IS_TAGGED_INT(from)
    ) {
        return NULL;
    }
    if (!pcc_gc_is_known_object(from)) {
        PccGcForwardNode *unknown_existing = pcc_gc_forwarding_find(from);
        if (unknown_existing != NULL) {
            return unknown_existing->to;
        }
        return NULL;
    }
    PyObjectHeader *from_h = py_header(from);
    int32_t from_flags = py_header_flags_load(from_h);
    PccGcForwardNode *existing = pcc_gc_forwarding_find(from);
    if (existing != NULL && existing->to != NULL) {
        return existing->to;
    }
    if ((from_flags & PY_FLAG_GC_YOUNG) == 0) return NULL;
    if ((from_flags & PY_FLAG_GC_PINNED) != 0) return NULL;
    if (!pcc_gc_relocate_copy_supported_tag(from_h->type_tag)) return NULL;

    int64_t size = pcc_gc_known_object_size(from);
    if (size < (int64_t)sizeof(PyObjectHeader)) return NULL;

    PyObject *to = (PyObject *)calloc(1, (size_t)size);
    if (to == NULL) return NULL;
    memcpy(to, from, (size_t)size);

    PyObjectHeader *to_h = py_header(to);
    to_h->refcount = 1;
    /* Same stale-verdict rule as the colored relocation copy: the
     * header memcpy inherits SWEEP_CANDIDATE from a finished cycle that
     * predates promotion, and a pending-candidate sweep would then run
     * PASS-0 __del__ on the live promoted copy. Promotion proves
     * liveness; the verdict dies here. */
    to_h->flags = (
        to_h->flags
        & ~(
            PY_FLAG_GC_YOUNG
            | PY_FLAG_GC_MINOR_ARENA
            | PY_FLAG_GC_REMEMBERED
            | PY_FLAG_GC_RELOCATION_CANDIDATE
            | PY_FLAG_GC_MALLOC_ALLOC
            | PY_FLAG_GC_SWEEP_CANDIDATE
        )
    ) | PY_FLAG_GC_OLD | PY_FLAG_GC_MALLOC_ALLOC;
    if (pcc_gc_relocate_copy_payload(from, to, size) != 0) {
        py_decref(to);
        return NULL;
    }
    to_h->refcount = 0;

    PccGcObjectNode *n = pcc_gc_object_node_alloc();
    if (n == NULL) {
        free(to);
        return NULL;
    }
    n->obj = to;
    n->size = size;
    n->freeing = 0;
    n->minor_block = NULL;
    n->next = NULL;
    n->prev = NULL;
    n->zpage_node = NULL;
    n->young_next = NULL;
    n->young_prev = NULL;
    pcc_gc_object_node_link_head(n);
    if (pcc_gc_object_index_insert(to, n) < 0) {
        pcc_gc_object_node_unlink(n);
        pcc_gc_object_node_release(n);
        free(to);
        return NULL;
    }
    __atomic_add_fetch(&pcc_gc_live_bytes, size, __ATOMIC_ACQ_REL);

    int moved_cpy_ref = from_h->type_tag == PY_TYPE_CPY_HANDLE;
    if (moved_cpy_ref) {
        pcc_cpy_handle_move_owned_ref(from, to);
    }
    if (pcc_gc_install_forwarding_unlocked(from, to) != 0) {
        if (moved_cpy_ref) {
            pcc_cpy_handle_move_owned_ref(to, from);
        }
        (void)pcc_gc_object_index_remove(to);
        pcc_gc_object_node_unlink(n);
        pcc_gc_live_bytes_subtract(size);
        pcc_gc_identity_remove(to);
        pcc_gc_object_node_release(n);
        free(to);
        return NULL;
    }

    PccGcObjectNode *from_node = (PccGcObjectNode *)pcc_gc_object_index_find(from);
    pcc_gc_backend3_young_unlink(from_node);
    pcc_gc_mark_forwarded_source_inactive(from);
    py_header_flags_update(from_h, PY_FLAG_GC_YOUNG, PY_FLAG_GC_OLD);
    return to;
}

static void pcc_gc_promote_owner_referents(PyObject *o, int recurse);
static void pcc_gc_promote_remembered_owner_referents(PyObject *o);
static int64_t pcc_gc_backend3_drain_promotion_worklist(int64_t budget);

static void pcc_gc_promote_young_object(PyObject *o) {
    if (!pcc_gc_is_known_object(o)) return;
    PyObjectHeader *h = py_header(o);
    if ((py_header_flags_load(h) & PY_FLAG_GC_YOUNG) != 0) {
        PyObject *oldified = pcc_gc_generational_oldify_copy(o);
        if (oldified != NULL) {
            pcc_gc_promote_remembered_owner_referents(oldified);
            return;
        }
        if (
            pcc_gc_selected_backend == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
            && (py_header_flags_load(h) & PY_FLAG_GC_MINOR_ARENA) != 0
        ) {
            py_header_flags_update(
                h,
                PY_FLAG_GC_YOUNG | PY_FLAG_GC_REMEMBERED,
                PY_FLAG_GC_OLD
            );
            pcc_gc_promote_remembered_owner_referents(o);
            return;
        }
        int32_t promote_flags = py_header_flags_load(h);
        PCC_GC_DEFER_TRIPWIRE(
            (promote_flags & PY_FLAG_GC_OLD) == 0,
            "pcc_gc_promote_young_object: promoting a YOUNG object already marked OLD (young->old generation invariant violated)"
        );
        if ((promote_flags & PY_FLAG_GC_OLD) != 0) return;
        pcc_gc_backend3_young_unlink(
            (PccGcObjectNode *)pcc_gc_object_index_find(o)
        );
        if (
            pcc_gc_selected_backend == PCC_GC_KIND_COLORED_RELOCATING
            && (promote_flags & PY_FLAG_GC_OLD) == 0
        ) {
            /* YOUNG and OLD are adjacent bits.  On the valid GC4 transition,
             * adding YOUNG atomically clears it, carries into OLD, and
             * preserves concurrently published unrelated header flags. */
            __atomic_add_fetch(
                &h->flags,
                PY_FLAG_GC_YOUNG,
                __ATOMIC_ACQ_REL
            );
            pcc_gc_backend4_zpage_note_owner_promoted_unlocked(o);
            return;
        }
        py_header_flags_update(h, PY_FLAG_GC_YOUNG, PY_FLAG_GC_OLD);
        if (pcc_gc_selected_backend == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR) {
            pcc_gc_promote_remembered_owner_referents(o);
        }
    }
}

static void pcc_gc_promote_young_slot_with_mode(PyObject **slot, int recurse) {
    if (slot == NULL) return;
    PyObject *child = *slot;
    if (child == NULL || PY_IS_TAGGED_INT(child)) return;
    if (!pcc_gc_pointer_can_have_header(child)) return;
    if (
        !pcc_gc_is_known_object(child)
        && pcc_gc_forwarding_find(child) == NULL
    ) {
        return;
    }
    int32_t child_flags = py_header_flags_load(py_header(child));
    if (
        (child_flags & (
            PY_FLAG_GC_YOUNG | PY_FLAG_GC_RELOCATION_CANDIDATE
        )) == 0
    ) {
        return;
    }
    PyObject *oldified = pcc_gc_generational_oldify_copy(child);
    if (oldified != NULL && oldified != child) {
        py_incref(oldified);
        *slot = oldified;
        pcc_gc_promote_remembered_owner_referents(oldified);
        py_decref(child);
        return;
    }
    if (!recurse) return;
    pcc_gc_promote_young_object(child);
}

static void pcc_gc_promote_young_slot(PyObject **slot) {
    pcc_gc_promote_young_slot_with_mode(slot, 1);
}

static void pcc_gc_promote_young_borrowed_slot_with_mode(
    PyObject **slot,
    int recurse
) {
    if (slot == NULL) return;
    PyObject *child = *slot;
    if (child == NULL || PY_IS_TAGGED_INT(child)) return;
    if (!pcc_gc_pointer_can_have_header(child)) return;
    if (
        !pcc_gc_is_known_object(child)
        && pcc_gc_forwarding_find(child) == NULL
    ) {
        return;
    }
    int32_t child_flags = py_header_flags_load(py_header(child));
    if (
        (child_flags & (
            PY_FLAG_GC_YOUNG | PY_FLAG_GC_RELOCATION_CANDIDATE
        )) == 0
    ) {
        return;
    }
    PyObject *oldified = pcc_gc_generational_oldify_copy(child);
    if (oldified != NULL && oldified != child) {
        *slot = oldified;
        pcc_gc_promote_remembered_owner_referents(oldified);
        return;
    }
    if (!recurse) return;
    pcc_gc_promote_young_object(child);
}

static void pcc_gc_promote_young_borrowed_slot(PyObject **slot) {
    pcc_gc_promote_young_borrowed_slot_with_mode(slot, 1);
}

static int pcc_gc_root_slot_value_is_stable(PyObject *value) {
    if (value == NULL || PY_IS_TAGGED_INT(value)) return 1;
    if (!pcc_gc_pointer_can_have_header(value)) return 1;
    if (!pcc_gc_is_known_object(value)) {
        return pcc_gc_forwarding_find(value) == NULL;
    }
    int32_t flags = py_header_flags_load(py_header(value));
    return (
        flags & (PY_FLAG_GC_YOUNG | PY_FLAG_GC_RELOCATION_CANDIDATE)
    ) == 0;
}

static void pcc_gc_promote_cached_frame_slot(
    PyObject **slot,
    PyObject **stable_slot,
    int borrowed
) {
    if (slot == NULL) return;
    PyObject *before = *slot;
    if (stable_slot != NULL && *stable_slot == before) return;
    if (borrowed) {
        pcc_gc_promote_young_borrowed_slot(slot);
    } else {
        pcc_gc_promote_young_slot(slot);
    }
    if (stable_slot == NULL) return;
    PyObject *after = *slot;
    *stable_slot = pcc_gc_root_slot_value_is_stable(after) ? after : NULL;
}

static void pcc_gc_trace_referents(
    PyObject *o,
    void (*visit)(PyObject *child)
);
static int64_t pcc_gc_root_slot_count_from_map(const int32_t *frame_map);
static int pcc_gc_root_map_is_borrowed(const int32_t *frame_map);

typedef void (*PccGcMappedRootSlotVisitor)(
    PyObject **slot,
    PyObject **stable_slot,
    int borrowed,
    void *ctx
);

static int64_t pcc_gc_visit_mapped_root_slots_unlocked(
    int64_t root_count,
    PyObject **slots,
    PyObject **stable_values,
    int borrowed,
    PccGcMappedRootSlotVisitor visit,
    void *ctx
) {
    int64_t n_slots = root_count;
    if (n_slots <= 0 || slots == NULL || visit == NULL) return 0;
    for (int64_t i = 0; i < n_slots; i++) {
        visit(
            &slots[i],
            stable_values == NULL ? NULL : &stable_values[i],
            borrowed,
            ctx
        );
    }
    return n_slots;
}

static void pcc_gc_promote_mapped_root_slot(
    PyObject **slot,
    PyObject **stable_slot,
    int borrowed,
    void *ctx
) {
    (void)ctx;
    pcc_gc_promote_cached_frame_slot(slot, stable_slot, borrowed);
}

typedef struct {
    int resolve;
} PccGcGrayMappedRootSlotCtx;

static void pcc_gc_gray_mapped_root_slot(
    PyObject **slot,
    PyObject **stable_slot,
    int borrowed,
    void *ctx
) {
    (void)stable_slot;
    (void)borrowed;
    PccGcGrayMappedRootSlotCtx *gray_ctx = (PccGcGrayMappedRootSlotCtx *)ctx;
    int resolve = gray_ctx == NULL || gray_ctx->resolve != 0;
    PyObject *root = resolve ? pcc_gc_resolve_root_slot_unlocked(slot) : *slot;
    pcc_gc_gray_root_object(root);
}

typedef struct {
    PyObject **roots;
    int64_t capacity;
    int64_t count;
    int32_t overflow;
} PccGcRuntimeRootSnapshotCtx;

static void pcc_gc_snapshot_runtime_mapped_root_slot(
    PyObject **slot,
    PyObject **stable_slot,
    int borrowed,
    void *ctx
) {
    (void)stable_slot;
    (void)borrowed;
    PccGcRuntimeRootSnapshotCtx *snapshot_ctx = (
        PccGcRuntimeRootSnapshotCtx *
    )ctx;
    if (slot == NULL || snapshot_ctx == NULL) return;
    if (snapshot_ctx->count >= snapshot_ctx->capacity) {
        snapshot_ctx->overflow = 1;
        return;
    }
    PyObject *root = *slot;
    if (root != NULL) py_incref(root);
    snapshot_ctx->roots[snapshot_ctx->count] = root;
    snapshot_ctx->count++;
}

typedef struct {
    int64_t rewritten;
} PccGcRewriteMappedRootCtx;

static void pcc_gc_rewrite_mapped_root_slot(
    PyObject **slot,
    PyObject **stable_slot,
    int borrowed,
    void *ctx
) {
    (void)stable_slot;
    (void)borrowed;
    if (slot == NULL) return;
    PccGcRewriteMappedRootCtx *rewrite_ctx = (
        PccGcRewriteMappedRootCtx *
    )ctx;
    PyObject *before = *slot;
    PyObject *after = pcc_gc_resolve_root_slot_unlocked(slot);
    if (rewrite_ctx != NULL && after != before) rewrite_ctx->rewritten++;
}

static int64_t pcc_gc_visit_scheduler_root_slots_unlocked(
    PccGcMappedRootSlotVisitor visit,
    void *ctx
) {
    if (visit == NULL) return 0;
    int64_t n_slots = 0;
    for (
        PccGcSchedulerRootNode *r = pcc_gc_scheduler_roots;
        r != NULL;
        r = r->next
    ) {
        PCC_GC_DEFER_TRIPWIRE(
            r->slot != NULL,
            "pcc_gc_visit_scheduler_root_slots_unlocked: registered scheduler root has a NULL slot address"
        );
        if (r->slot == NULL) continue;
        visit(r->slot, NULL, 0, ctx);
        n_slots++;
    }
    return n_slots;
}

static int64_t pcc_gc_visit_builtin_exception_cache_slots_unlocked(
    PccGcMappedRootSlotVisitor visit,
    void *ctx
) {
    if (visit == NULL) return 0;
    int64_t n_slots = 0;
    for (int32_t tag = 0; tag < PY_EXC_N_BUILTIN; tag++) {
        PyObject **slot = (PyObject **)py_subs_exc_cache_slot(tag);
        if (slot == NULL) continue;
        visit(slot, NULL, 0, ctx);
        n_slots++;
    }
    return n_slots;
}

static void pcc_gc_backend3_frame_root_scan_reset_unlocked(void) {
    pcc_gc_backend3_frame_root_scan_phase = 0;
    pcc_gc_backend3_frame_root_scan_slot = -1;
    pcc_gc_backend3_frame_root_scan_cursor = NULL;
    pcc_gc_backend3_continuation_root_scan_cursor = NULL;
}

void pcc_gc_generational_promote_frame_roots(int64_t budget) {
    if (budget <= 0) return;
    pcc_gc_graph_lock();
    int64_t examined = 0;
    while (examined < budget) {
        if (pcc_gc_backend3_frame_root_scan_phase == 0) {
            if (pcc_gc_backend3_frame_root_scan_slot < 0) {
                pcc_gc_backend3_frame_root_scan_cursor = pcc_gc_frames;
                pcc_gc_backend3_frame_root_scan_slot = 0;
            }
            PccGcFrameNode *f = pcc_gc_backend3_frame_root_scan_cursor;
            if (f == NULL) {
                pcc_gc_backend3_frame_root_scan_phase = 1;
                pcc_gc_backend3_frame_root_scan_slot = -1;
                continue;
            }
            int64_t slot_index = pcc_gc_backend3_frame_root_scan_slot;
            if (slot_index >= f->root_count) {
                pcc_gc_backend3_frame_root_scan_cursor = f->next;
                pcc_gc_backend3_frame_root_scan_slot = 0;
                continue;
            }
            int64_t revision_before = pcc_gc_root_registry_revision;
            int borrowed = (
                f->borrowed & PCC_GC_FRAME_NODE_FLAG_BORROWED
            );
            pcc_gc_promote_mapped_root_slot(
                &f->slots[slot_index],
                &f->stable_values[slot_index],
                borrowed,
                NULL
            );
            examined++;
            if (pcc_gc_root_registry_revision != revision_before) {
                continue;
            }
            pcc_gc_backend3_frame_root_scan_slot = slot_index + 1;
            if (
                pcc_gc_backend3_frame_root_scan_slot >= f->root_count
            ) {
                pcc_gc_backend3_frame_root_scan_cursor = f->next;
                pcc_gc_backend3_frame_root_scan_slot = 0;
            }
            continue;
        }

        if (pcc_gc_backend3_frame_root_scan_slot < 0) {
            pcc_gc_backend3_continuation_root_scan_cursor =
                pcc_gc_continuation_roots;
            pcc_gc_backend3_frame_root_scan_slot = 0;
        }
        PccGcContinuationRootNode *c =
            pcc_gc_backend3_continuation_root_scan_cursor;
        if (c == NULL) {
            pcc_gc_backend3_frame_root_scan_reset_unlocked();
            break;
        }
        int64_t slot_index = pcc_gc_backend3_frame_root_scan_slot;
        if (slot_index >= c->root_count) {
            pcc_gc_backend3_continuation_root_scan_cursor = c->next;
            pcc_gc_backend3_frame_root_scan_slot = 0;
            continue;
        }
        int64_t revision_before = pcc_gc_root_registry_revision;
        pcc_gc_promote_mapped_root_slot(
            &c->slots[slot_index],
            &c->stable_values[slot_index],
            c->borrowed,
            NULL
        );
        examined++;
        if (pcc_gc_root_registry_revision != revision_before) {
            continue;
        }
        pcc_gc_backend3_frame_root_scan_slot = slot_index + 1;
        if (
            pcc_gc_backend3_frame_root_scan_slot >= c->root_count
        ) {
            pcc_gc_backend3_continuation_root_scan_cursor = c->next;
            pcc_gc_backend3_frame_root_scan_slot = 0;
        }
    }
    pcc_gc_graph_unlock();
    if (examined < budget) {
        (void)pcc_gc_backend3_drain_promotion_worklist(budget - examined);
    }
}

static void pcc_gc_backend3_scheduler_root_scan_reset_unlocked(void) {
    pcc_gc_backend3_scheduler_root_scan_phase = 0;
    pcc_gc_backend3_scheduler_root_scan_slot = -1;
    pcc_gc_backend3_scheduler_root_scan_cursor = NULL;
}

void pcc_gc_generational_promote_scheduler_roots(int64_t budget) {
    if (budget <= 0) return;
    pcc_gc_graph_lock();
    int64_t examined = 0;
    while (examined < budget) {
        if (pcc_gc_backend3_scheduler_root_scan_phase == 0) {
            if (pcc_gc_backend3_scheduler_root_scan_slot < 0) {
                pcc_gc_backend3_scheduler_root_scan_cursor =
                    pcc_gc_scheduler_roots;
                pcc_gc_backend3_scheduler_root_scan_slot = 0;
            }
            PccGcSchedulerRootNode *r =
                pcc_gc_backend3_scheduler_root_scan_cursor;
            if (r == NULL) {
                pcc_gc_backend3_scheduler_root_scan_phase = 1;
                pcc_gc_backend3_scheduler_root_scan_slot = 0;
                continue;
            }
            PccGcSchedulerRootNode *next = r->next;
            int64_t revision_before = pcc_gc_root_registry_revision;
            if (r->slot != NULL) {
                pcc_gc_promote_mapped_root_slot(
                    r->slot, NULL, 0, NULL
                );
            }
            examined++;
            if (pcc_gc_root_registry_revision != revision_before) {
                continue;
            }
            pcc_gc_backend3_scheduler_root_scan_cursor = next;
            continue;
        }

        int64_t slot_index = pcc_gc_backend3_scheduler_root_scan_slot;
        if (slot_index >= PY_EXC_N_BUILTIN) {
            pcc_gc_backend3_scheduler_root_scan_reset_unlocked();
            break;
        }
        PyObject **slot = (PyObject **)py_subs_exc_cache_slot(
            (int32_t)slot_index
        );
        if (slot != NULL) {
            pcc_gc_promote_mapped_root_slot(slot, NULL, 0, NULL);
        }
        pcc_gc_backend3_scheduler_root_scan_slot = slot_index + 1;
        examined++;
    }
    pcc_gc_graph_unlock();
    if (examined < budget) {
        (void)pcc_gc_backend3_drain_promotion_worklist(budget - examined);
    }
}

static void pcc_gc_promote_remembered_owner_referents(PyObject *o);

static void pcc_gc_backend3_remember_owner_unlocked(
    PyObject *owner,
    PyObjectHeader *owner_h
) {
    if (owner == NULL || owner_h == NULL || PY_IS_TAGGED_INT(owner)) return;
    int32_t flags = py_header_flags_load(owner_h);
    if ((flags & PY_FLAG_GC_REMEMBERED) != 0) return;
    int64_t allocation_limit = __atomic_load_n(
        &pcc_gc_backend3_remembered_owner_allocation_limit,
        __ATOMIC_ACQUIRE
    );
    PccGcRememberedOwnerNode *n = NULL;
    if (allocation_limit != 0) {
        n = (PccGcRememberedOwnerNode *)calloc(
            1, sizeof(PccGcRememberedOwnerNode)
        );
        if (n != NULL && allocation_limit > 0) {
            __atomic_sub_fetch(
                &pcc_gc_backend3_remembered_owner_allocation_limit,
                1,
                __ATOMIC_RELEASE
            );
        }
    }
    if (n == NULL) {
        pcc_gc_backend3_remembered_overflow = 1;
        /* A newly flagged owner can precede a retained cursor.  NULL makes
         * the next drain detach queued nodes and restart from the head. */
        pcc_gc_backend3_remembered_scan_cursor = NULL;
        pcc_gc_backend3_remembered_scan_revision = 0;
        py_header_flags_or(owner_h, PY_FLAG_GC_REMEMBERED);
        return;
    }
    n->owner = owner;
    n->next = pcc_gc_backend3_remembered_owners;
    pcc_gc_backend3_remembered_owners = n;
    py_header_flags_or(owner_h, PY_FLAG_GC_REMEMBERED);
}

static PccGcRememberedOwnerNode *
pcc_gc_backend3_remembered_owners_clear_unlocked(void) {
    PccGcRememberedOwnerNode *n = pcc_gc_backend3_remembered_owners;
    pcc_gc_backend3_remembered_owners = NULL;
    pcc_gc_backend3_remembered_overflow = 0;
    pcc_gc_backend3_remembered_scan_cursor = NULL;
    pcc_gc_backend3_remembered_scan_revision = 0;
    return n;
}

void pcc_gc_backend3_remembered_scan_probe_config(
    int64_t allocation_limit
) {
    __atomic_store_n(
        &pcc_gc_backend3_remembered_owner_allocation_limit,
        allocation_limit,
        __ATOMIC_RELEASE
    );
}

static void pcc_gc_backend3_finish_detached_remembered_owners(
    PccGcRememberedOwnerNode *head
) {
    while (head != NULL) {
        PccGcRememberedOwnerNode *next = head->next;
        free(head);
        head = next;
    }
}

static int64_t pcc_gc_backend3_scan_remembered_owners(int64_t budget) {
    int64_t examined = 0;
    if (
        pcc_gc_backend3_remembered_scan_revision
        != pcc_gc_object_list_revision
    ) {
        pcc_gc_backend3_remembered_scan_cursor = pcc_gc_objects;
        pcc_gc_backend3_remembered_scan_revision =
            pcc_gc_object_list_revision;
    }
    while (
        pcc_gc_backend3_remembered_scan_cursor != NULL
        && examined < budget
    ) {
        PccGcObjectNode *n = pcc_gc_backend3_remembered_scan_cursor;
        pcc_gc_backend3_remembered_scan_cursor = n->next;
        examined++;
        if (!pcc_gc_object_node_is_active(n)) continue;
        PyObjectHeader *h = py_header(n->obj);
        int32_t flags = py_header_flags_load(h);
        if ((flags & PY_FLAG_GC_REMEMBERED) != 0) {
            pcc_gc_promote_remembered_owner_referents(n->obj);
            py_header_flags_and(h, ~PY_FLAG_GC_REMEMBERED);
        }
    }
    return examined;
}

static int64_t pcc_gc_backend3_drain_remembered_owners(
    int64_t budget,
    PccGcRememberedOwnerNode **detached_out
) {
    if (detached_out == NULL || *detached_out != NULL) return 0;
    int64_t processed = 0;
    if (pcc_gc_backend3_remembered_overflow != 0) {
        if (pcc_gc_backend3_remembered_scan_cursor == NULL) {
            *detached_out =
                pcc_gc_backend3_remembered_owners_clear_unlocked();
            pcc_gc_backend3_remembered_overflow = 1;
            pcc_gc_backend3_remembered_scan_cursor = pcc_gc_objects;
            pcc_gc_backend3_remembered_scan_revision =
                pcc_gc_object_list_revision;
        }
        processed += pcc_gc_backend3_scan_remembered_owners(budget);
        if (pcc_gc_backend3_remembered_scan_cursor == NULL) {
            pcc_gc_backend3_remembered_overflow = 0;
            pcc_gc_backend3_remembered_scan_revision = 0;
        }
        return processed;
    }
    while (
        pcc_gc_backend3_remembered_owners != NULL
        && processed < budget
    ) {
        PccGcRememberedOwnerNode *n = pcc_gc_backend3_remembered_owners;
        pcc_gc_backend3_remembered_owners = n->next;
        PyObject *owner = n->owner;
        PCC_GC_DEFER_TRIPWIRE(
            owner != NULL,
            "pcc_gc_backend3_drain_remembered_owners: remembered-owner entry has NULL owner (insertion rejects NULL; NULL here is a corrupt remembered-set node)"
        );
        n->next = *detached_out;
        *detached_out = n;
        if (owner == NULL) continue;
        if (!pcc_gc_is_known_object(owner)) continue;
        PyObjectHeader *h = py_header(owner);
        int32_t flags = py_header_flags_load(h);
        if ((flags & PY_FLAG_GC_REMEMBERED) == 0) continue;
        pcc_gc_promote_remembered_owner_referents(owner);
        py_header_flags_and(h, ~PY_FLAG_GC_REMEMBERED);
        processed++;
    }
    return processed;
}

static void pcc_gc_promote_tls_exception_root(PyObject **cleanup_out) {
    if (cleanup_out == NULL || *cleanup_out != NULL) return;
    PyObject *cur = (PyObject *)py_tls_exc_get();
    if (cur == NULL || PY_IS_TAGGED_INT(cur)) return;
    PyObject *oldified = pcc_gc_generational_oldify_copy(cur);
    if (oldified != NULL && oldified != cur) {
        py_incref(oldified);
        py_tls_exc_set(oldified);
        pcc_gc_promote_remembered_owner_referents(oldified);
        *cleanup_out = cur;
        return;
    }
    pcc_gc_promote_young_object(cur);
}

static void pcc_gc_promote_extension_module_state_root(
    PyObject *root,
    void *ctx
) {
    (void)ctx;
    if (root == NULL || PY_IS_TAGGED_INT(root)) return;
    pcc_gc_graph_lock();
    pcc_gc_promote_young_object(root);
    pcc_gc_graph_unlock();
    (void)pcc_gc_backend3_drain_promotion_worklist(PCC_GC_SAFEPOINT_BATCH);
}

typedef void (*PccGcOwnerSlotVisitor)(PyObject **slot, void *ctx);

static int pcc_gc_visit_core_container_owner_slots(
    PyObject *o,
    PccGcOwnerSlotVisitor visit,
    void *ctx
) {
    if (o == NULL || PY_IS_TAGGED_INT(o) || visit == NULL) return 0;
    int32_t tag = py_header(o)->type_tag;
    if (tag == PY_TYPE_LIST) {
        PyListObject *l = (PyListObject *)o;
        for (int64_t i = 0; i < l->length; i++) visit(&l->items[i], ctx);
        return 1;
    }
    if (tag == PY_TYPE_TUPLE) {
        PyTupleObject *t = (PyTupleObject *)o;
        for (int64_t i = 0; i < t->len; i++) visit(&t->items[i], ctx);
        return 1;
    }
    if (tag == PY_TYPE_DICT) {
        PyDictObject *d = (PyDictObject *)o;
        if (d->entries != NULL) {
            for (int64_t i = 0; i < d->entries_used; i++) {
                DictEntry *e = &d->entries[i];
                if (e->key != NULL) {
                    visit(&e->key, ctx);
                    visit(&e->value, ctx);
                }
            }
        }
        return 1;
    }
    if (tag == PY_TYPE_SET) {
        PySetObject *s = (PySetObject *)o;
        if (s->entries != NULL) {
            for (int64_t i = 0; i < s->capacity; i++) {
                PyObject **k = &s->entries[i].key;
                if (*k != NULL && *k != py_set_dummy) visit(k, ctx);
            }
        }
        return 1;
    }
    if (tag == PY_TYPE_VTHREAD_CHANNEL) {
        PyVThreadChannelObject *channel = (PyVThreadChannelObject *)o;
        if (channel->kind == PCC_VTHREAD_CHANNEL_KIND_CORE) {
            PyVThreadChannelCoreObject *core =
                (PyVThreadChannelCoreObject *)o;
            if (
                core->capacity < 0
                || core->capacity > PCC_VTHREAD_CHANNEL_MAX_CAPACITY
            ) return 1;
            for (int64_t i = 0; i < core->capacity; i++) {
                visit(&core->items[i], ctx);
            }
            return 1;
        }
        if (
            channel->kind == PCC_VTHREAD_CHANNEL_KIND_SENDER
            || channel->kind == PCC_VTHREAD_CHANNEL_KIND_RECEIVER
        ) {
            PyVThreadChannelEndpointObject *endpoint =
                (PyVThreadChannelEndpointObject *)o;
            visit(&endpoint->core, ctx);
        }
        return 1;
    }
    return 0;
}

static int pcc_gc_visit_fixed_owner_slots(
    PyObject *o,
    PccGcOwnerSlotVisitor visit,
    void *ctx
) {
    if (o == NULL || PY_IS_TAGGED_INT(o) || visit == NULL) return 0;
    int32_t tag = py_header(o)->type_tag;
    if (tag == PY_TYPE_FUNC) {
        PyFuncObject *f = (PyFuncObject *)o;
        visit(&f->capi_self, ctx);
        visit(&f->capi_module, ctx);
        visit(&f->capi_weakreflist, ctx);
        visit(&f->captures, ctx);
        visit(&f->self_obj, ctx);
        visit(&f->attrs, ctx);
        return 1;
    }
    if (tag == PY_TYPE_ITER) {
        PyIterObject *it = (PyIterObject *)o;
        visit(&it->seq, ctx);
        return 1;
    }
    if (tag == PY_TYPE_GEN) {
        PyGenObject *g = (PyGenObject *)o;
        visit(&g->frame, ctx);
        visit(&g->send_value, ctx);
        return 1;
    }
    if (tag == PY_TYPE_COROUTINE) {
        PccGcCoroutineObject *c = (PccGcCoroutineObject *)o;
        visit(&c->captures, ctx);
        visit(&c->args, ctx);
        visit(&c->result, ctx);
        return 1;
    }
    if (tag == PY_TYPE_TASK) {
        PyTaskObject *t = (PyTaskObject *)o;
        visit(&t->coro, ctx);
        visit(&t->result, ctx);
        visit(&t->waiter, ctx);
        return 1;
    }
    if (tag == PY_TYPE_VIRTUAL_THREAD) {
        PyVirtualThreadObject *t = (PyVirtualThreadObject *)o;
        visit(&t->continuation, ctx);
        visit(&t->result, ctx);
        visit(&t->exception, ctx);
        visit(&t->join_target, ctx);
        visit(&t->channel_owner_a, ctx);
        visit(&t->channel_owner_b, ctx);
        visit(&t->channel_value, ctx);
        return 1;
    }
    if (tag == PY_TYPE_EXC) {
        PyExceptionObject *e = (PyExceptionObject *)o;
        visit((PyObject **)&e->exc_class, ctx);
        visit(&e->message, ctx);
        visit(&e->cause, ctx);
        visit(&e->context, ctx);
        return 1;
    }
    if (tag == PY_TYPE_PROPERTY) {
        PyPropertyObject *p = (PyPropertyObject *)o;
        visit(&p->fget, ctx);
        visit(&p->fset, ctx);
        visit(&p->fdel, ctx);
        return 1;
    }
    if (tag == PY_TYPE_CLASSMETHOD) {
        PyClassMethodObject *m = (PyClassMethodObject *)o;
        visit(&m->func, ctx);
        return 1;
    }
    if (tag == PY_TYPE_STATICMETHOD) {
        PyStaticMethodObject *m = (PyStaticMethodObject *)o;
        visit(&m->func, ctx);
        return 1;
    }
    if (tag == PY_TYPE_MEMORYVIEW) {
        PyMemoryViewObject *mv = (PyMemoryViewObject *)o;
        visit(&mv->base, ctx);
        return 1;
    }
    if (tag == PY_TYPE_THREAD) {
        PccGcThreadObject *t = (PccGcThreadObject *)o;
        visit(&t->callable, ctx);
        visit(&t->args, ctx);
        visit(&t->result, ctx);
        return 1;
    }
    return 0;
}

static int pcc_gc_visit_weakref_slots(
    PyObject *o,
    PccGcOwnerSlotVisitor visit_owned,
    PccGcOwnerSlotVisitor visit_borrowed_update_only,
    void *ctx
) {
    if (
        o == NULL
        || PY_IS_TAGGED_INT(o)
        || (visit_owned == NULL && visit_borrowed_update_only == NULL)
    ) return 0;
    int32_t tag = py_header(o)->type_tag;
    if (tag != PY_TYPE_WEAKREF) return 0;
    PyWeakRefObject *wr = (PyWeakRefObject *)o;
    if (visit_borrowed_update_only != NULL) {
        visit_borrowed_update_only(&wr->target, ctx);
    }
    if (visit_owned != NULL) {
        visit_owned(&wr->callback, ctx);
    }
    return 1;
}

static int pcc_gc_visit_continuation_owner_slots(
    PyObject *o,
    PccGcOwnerSlotVisitor visit,
    void *ctx
) {
    if (o == NULL || PY_IS_TAGGED_INT(o) || visit == NULL) return 0;
    int32_t tag = py_header(o)->type_tag;
    if (tag != PY_TYPE_CONTINUATION) return 0;
    PyContinuationObject *c = (PyContinuationObject *)o;
    PyContinuationStackChunk *chunk = c->stack_chunk;
    if (chunk != NULL && chunk->slots != NULL) {
        for (int64_t i = 0; i < chunk->slot_count; i++) {
            visit(&chunk->slots[i], ctx);
        }
    }
    return 1;
}

static int pcc_gc_visit_instance_owner_slots(
    PyObject *o,
    PccGcOwnerSlotVisitor visit_owned,
    PccGcOwnerSlotVisitor visit_borrowed,
    void *ctx
) {
    if (
        o == NULL
        || PY_IS_TAGGED_INT(o)
        || (visit_owned == NULL && visit_borrowed == NULL)
    ) return 0;
    int32_t tag = py_header(o)->type_tag;
    if (pcc_capi_is_cext_type_tag((int64_t)tag) != 0) return 0;
    if (
        tag != PY_TYPE_INSTANCE
        && tag != PY_TYPE_VALUEBOX
        && tag < PY_TYPE_USER_CLASS_START
    ) return 0;
    PyInstanceObject *inst = (PyInstanceObject *)o;
    if (inst->cls == NULL) return 1;
    if (visit_borrowed != NULL) {
        visit_borrowed((PyObject **)&inst->cls, ctx);
    }
    PyClassObject *cls = inst->cls;
    if (cls == NULL || visit_owned == NULL) return 1;
    int class_is_live = (
        py_header((PyObject *)cls)->type_tag == PY_TYPE_CLASS
    );
    PCC_GC_MIXED_TRIPWIRE(
        class_is_live,
        "pcc_gc_visit_instance_owner_slots: instance->cls is not a live PY_TYPE_CLASS (class freed/over-released; n_fields slot walk would read a zeroed/corrupt class)"
    );
    if (!class_is_live) return 1;
    int32_t n_fields = cls->n_fields;
    if (n_fields < 0) n_fields = 0;
    for (int32_t i = 0; i < n_fields; i++) {
        visit_owned(&inst->fields[i], ctx);
    }
    if ((cls->h.flags & 2) == 0) {
        visit_owned(&inst->fields[n_fields], ctx);
    }
    return 1;
}

static int pcc_gc_visit_class_slots(
    PyObject *o,
    PccGcOwnerSlotVisitor visit_owned,
    PccGcOwnerSlotVisitor visit_borrowed_traced,
    PccGcOwnerSlotVisitor visit_borrowed_update_only,
    void *ctx
) {
    if (
        o == NULL
        || PY_IS_TAGGED_INT(o)
        || (
            visit_owned == NULL
            && visit_borrowed_traced == NULL
            && visit_borrowed_update_only == NULL
        )
    ) return 0;
    int32_t tag = py_header(o)->type_tag;
    if (tag != PY_TYPE_CLASS) return 0;
    PyClassObject *cls = (PyClassObject *)o;
    if (visit_borrowed_traced != NULL) {
        if (cls->bases != NULL) {
            for (int32_t i = 0; i < cls->n_bases; i++) {
                visit_borrowed_traced((PyObject **)&cls->bases[i], ctx);
            }
        }
        if (cls->mro != NULL) {
            for (int32_t i = 0; i < cls->n_mro; i++) {
                visit_borrowed_traced((PyObject **)&cls->mro[i], ctx);
            }
        }
    }
    if (visit_borrowed_update_only != NULL) {
        if (cls->methods != NULL) {
            for (int32_t i = 0; i < cls->n_methods; i++) {
                visit_borrowed_update_only(&cls->methods[i].func, ctx);
            }
        }
        visit_borrowed_update_only(&cls->del_method, ctx);
    }
    if (visit_owned != NULL) {
        visit_owned(&cls->attrs, ctx);
    }
    if (visit_borrowed_traced != NULL) {
        visit_borrowed_traced((PyObject **)&cls->metaclass, ctx);
    }
    return 1;
}

typedef struct {
    PyObjSlotVisitor visit;
    void *ctx;
} PyObjVisitCtx;

static void py_obj_visit_role_slot(
    PyObject **slot,
    int32_t role,
    void *ctx
) {
    PyObjVisitCtx *visit_ctx = (PyObjVisitCtx *)ctx;
    if (
        slot == NULL
        || visit_ctx == NULL
        || visit_ctx->visit == NULL
    ) return;
    visit_ctx->visit(slot, role, visit_ctx->ctx);
}

static void py_obj_visit_owned_slot(PyObject **slot, void *ctx) {
    py_obj_visit_role_slot(slot, PY_OBJ_SLOT_OWNED, ctx);
}

static void py_obj_visit_borrowed_traced_slot(PyObject **slot, void *ctx) {
    py_obj_visit_role_slot(slot, PY_OBJ_SLOT_BORROWED_TRACED, ctx);
}

static void py_obj_visit_borrowed_update_only_slot(
    PyObject **slot,
    void *ctx
) {
    py_obj_visit_role_slot(slot, PY_OBJ_SLOT_BORROWED_UPDATE_ONLY, ctx);
}

static int py_obj_has_no_pointer_slots(PyObject *o) {
    if (o == NULL || PY_IS_TAGGED_INT(o)) return 0;
    switch (py_header(o)->type_tag) {
        case PY_TYPE_NONE:
        case PY_TYPE_BOOL:
        case PY_TYPE_INT:
        case PY_TYPE_FLOAT:
        case PY_TYPE_STR:
        case PY_TYPE_COMPLEX:
        case PY_TYPE_BYTES:
        case PY_TYPE_BYTEARRAY:
        case PY_TYPE_FILE:
        case PY_TYPE_CPY_HANDLE:
            return 1;
        /* Thread wait queues store virtual threads in external waiter nodes
         * registered as scheduler roots, not object-inline PyObject slots. */
        case PY_TYPE_THREAD_LOCK:
        case PY_TYPE_THREAD_RLOCK:
        case PY_TYPE_THREAD_EVENT:
        case PY_TYPE_THREAD_CONDITION:
        case PY_TYPE_THREAD_SEMAPHORE:
            return 1;
        default:
            return 0;
    }
}

int py_obj_visit_slots(PyObject *o, PyObjSlotVisitor visit, void *ctx) {
    if (o == NULL || PY_IS_TAGGED_INT(o) || visit == NULL) return 0;
    int type_tag_is_valid = py_header(o)->type_tag >= 0;
    PCC_GC_MIXED_TRIPWIRE(
        type_tag_is_valid,
        "py_obj_visit_slots: negative type_tag at trace fan-out entry (corrupt object header)"
    );
    if (!type_tag_is_valid) return 0;
    int64_t state[2] = {-1, 0};
    if (
        pcc_gc_visit_object_slots_slice(
            o, 0, INT64_MAX, visit, ctx, state
        )
    ) return 1;
    return pcc_capi_visit_cext_object_slots(o, visit, ctx) ? 1 : 0;
}

/* Visit a bounded slice of the built-in slot contract using a logical cursor.
 * The cursor is a physical slot ordinal, not a raw PyObject **: list/dict/set,
 * class and continuation payload bases are reloaded from the current owner on
 * every call.  state_out[0] is the next cursor (-1 when complete) and
 * state_out[1] is the number of examined ordinals.  C-extension traversal is
 * deliberately reported as unsupported because its external callback remains
 * a separately classified A3b holder. */
int64_t pcc_gc_visit_object_slots_slice(
    PyObject *o,
    int64_t cursor,
    int64_t limit,
    PyObjSlotVisitor visit,
    void *ctx,
    int64_t *state_out
) {
    if (state_out == NULL) return 0;
    state_out[0] = -1;
    state_out[1] = 0;
    if (
        o == NULL
        || PY_IS_TAGGED_INT(o)
        || visit == NULL
        || cursor < 0
        || limit <= 0
    ) return 0;
    int32_t tag = py_header(o)->type_tag;
    if (tag < 0) return 0;
    if (py_obj_has_no_pointer_slots(o)) return 1;
    if (pcc_capi_is_cext_type_tag((int64_t)tag) != 0) return 0;

    int64_t total = 0;
    int64_t family = 0;
    PyObject **fixed_slots[7] = {0};
    int32_t fixed_roles[7] = {0};
    if (tag == PY_TYPE_LIST) {
        PyListObject *l = (PyListObject *)o;
        total = l->length < 0 ? 0 : l->length;
        family = 1;
    } else if (tag == PY_TYPE_TUPLE) {
        PyTupleObject *t = (PyTupleObject *)o;
        total = t->len < 0 ? 0 : t->len;
        family = 2;
    } else if (tag == PY_TYPE_DICT) {
        PyDictObject *d = (PyDictObject *)o;
        total = d->entries_used < 0 ? 0 : d->entries_used * 2;
        family = 3;
    } else if (tag == PY_TYPE_SET) {
        PySetObject *s = (PySetObject *)o;
        total = s->capacity < 0 ? 0 : s->capacity;
        family = 4;
    } else if (tag == PY_TYPE_VTHREAD_CHANNEL) {
        PyVThreadChannelObject *channel = (PyVThreadChannelObject *)o;
        if (channel->kind == PCC_VTHREAD_CHANNEL_KIND_CORE) {
            PyVThreadChannelCoreObject *core =
                (PyVThreadChannelCoreObject *)o;
            total = (
                core->capacity < 0
                || core->capacity > PCC_VTHREAD_CHANNEL_MAX_CAPACITY
            ) ? 0 : core->capacity;
            family = 5;
        } else if (
            channel->kind == PCC_VTHREAD_CHANNEL_KIND_SENDER
            || channel->kind == PCC_VTHREAD_CHANNEL_KIND_RECEIVER
        ) {
            total = 1;
            family = 6;
        } else {
            return 1;
        }
    } else if (tag == PY_TYPE_FUNC) {
        PyFuncObject *f = (PyFuncObject *)o;
        fixed_slots[0] = &f->capi_self;
        fixed_slots[1] = &f->capi_module;
        fixed_slots[2] = &f->capi_weakreflist;
        fixed_slots[3] = &f->captures;
        fixed_slots[4] = &f->self_obj;
        fixed_slots[5] = &f->attrs;
        total = 6;
        family = 7;
    } else if (tag == PY_TYPE_ITER) {
        fixed_slots[0] = &((PyIterObject *)o)->seq;
        total = 1;
        family = 7;
    } else if (tag == PY_TYPE_GEN) {
        PyGenObject *g = (PyGenObject *)o;
        fixed_slots[0] = &g->frame;
        fixed_slots[1] = &g->send_value;
        total = 2;
        family = 7;
    } else if (tag == PY_TYPE_COROUTINE) {
        PccGcCoroutineObject *c = (PccGcCoroutineObject *)o;
        fixed_slots[0] = &c->captures;
        fixed_slots[1] = &c->args;
        fixed_slots[2] = &c->result;
        total = 3;
        family = 7;
    } else if (tag == PY_TYPE_TASK) {
        PyTaskObject *t = (PyTaskObject *)o;
        fixed_slots[0] = &t->coro;
        fixed_slots[1] = &t->result;
        fixed_slots[2] = &t->waiter;
        total = 3;
        family = 7;
    } else if (tag == PY_TYPE_VIRTUAL_THREAD) {
        PyVirtualThreadObject *t = (PyVirtualThreadObject *)o;
        fixed_slots[0] = &t->continuation;
        fixed_slots[1] = &t->result;
        fixed_slots[2] = &t->exception;
        fixed_slots[3] = &t->join_target;
        fixed_slots[4] = &t->channel_owner_a;
        fixed_slots[5] = &t->channel_owner_b;
        fixed_slots[6] = &t->channel_value;
        total = 7;
        family = 7;
    } else if (tag == PY_TYPE_EXC) {
        PyExceptionObject *e = (PyExceptionObject *)o;
        fixed_slots[0] = (PyObject **)&e->exc_class;
        fixed_slots[1] = &e->message;
        fixed_slots[2] = &e->cause;
        fixed_slots[3] = &e->context;
        total = 4;
        family = 7;
    } else if (tag == PY_TYPE_PROPERTY) {
        PyPropertyObject *p = (PyPropertyObject *)o;
        fixed_slots[0] = &p->fget;
        fixed_slots[1] = &p->fset;
        fixed_slots[2] = &p->fdel;
        total = 3;
        family = 7;
    } else if (tag == PY_TYPE_CLASSMETHOD) {
        fixed_slots[0] = &((PyClassMethodObject *)o)->func;
        total = 1;
        family = 7;
    } else if (tag == PY_TYPE_STATICMETHOD) {
        fixed_slots[0] = &((PyStaticMethodObject *)o)->func;
        total = 1;
        family = 7;
    } else if (tag == PY_TYPE_MEMORYVIEW) {
        fixed_slots[0] = &((PyMemoryViewObject *)o)->base;
        total = 1;
        family = 7;
    } else if (tag == PY_TYPE_THREAD) {
        PccGcThreadObject *t = (PccGcThreadObject *)o;
        fixed_slots[0] = &t->callable;
        fixed_slots[1] = &t->args;
        fixed_slots[2] = &t->result;
        total = 3;
        family = 7;
    } else if (tag == PY_TYPE_WEAKREF) {
        PyWeakRefObject *wr = (PyWeakRefObject *)o;
        fixed_slots[0] = &wr->target;
        fixed_roles[0] = PY_OBJ_SLOT_BORROWED_UPDATE_ONLY;
        fixed_slots[1] = &wr->callback;
        total = 2;
        family = 7;
    } else if (tag == PY_TYPE_CONTINUATION) {
        PyContinuationObject *c = (PyContinuationObject *)o;
        PyContinuationStackChunk *chunk = c->stack_chunk;
        total = (
            chunk == NULL || chunk->slots == NULL || chunk->slot_count < 0
        ) ? 0 : chunk->slot_count;
        family = 8;
    } else if (tag == PY_TYPE_CLASS) {
        PyClassObject *cls = (PyClassObject *)o;
        int64_t n_bases = cls->n_bases < 0 ? 0 : cls->n_bases;
        int64_t n_mro = cls->n_mro < 0 ? 0 : cls->n_mro;
        int64_t n_methods = cls->n_methods < 0 ? 0 : cls->n_methods;
        total = n_bases + n_mro + n_methods + 3;
        family = 9;
    } else if (
        tag == PY_TYPE_INSTANCE
        || tag == PY_TYPE_VALUEBOX
        || tag >= PY_TYPE_USER_CLASS_START
    ) {
        PyInstanceObject *inst = (PyInstanceObject *)o;
        if (inst->cls == NULL) return 1;
        int class_is_live = (
            py_header((PyObject *)inst->cls)->type_tag == PY_TYPE_CLASS
        );
        PCC_GC_MIXED_TRIPWIRE(
            class_is_live,
            "pcc_gc_visit_object_slots_slice: instance->cls is not a live PY_TYPE_CLASS"
        );
        if (!class_is_live) return 1;
        int64_t n_fields = inst->cls->n_fields;
        if (n_fields < 0) n_fields = 0;
        total = 1 + n_fields + ((inst->cls->h.flags & 2) == 0 ? 1 : 0);
        family = 10;
    } else {
        return 0;
    }

    int64_t examined = 0;
    while (cursor < total && examined < limit) {
        PyObject **slot = NULL;
        int32_t role = PY_OBJ_SLOT_OWNED;
        if (family == 1) {
            PyListObject *l = (PyListObject *)o;
            if (l->items != NULL) slot = &l->items[cursor];
        } else if (family == 2) {
            slot = &((PyTupleObject *)o)->items[cursor];
        } else if (family == 3) {
            PyDictObject *d = (PyDictObject *)o;
            int64_t entry_index = cursor / 2;
            if (
                d->entries != NULL
                && d->entries[entry_index].key != NULL
            ) {
                slot = (cursor & 1)
                    ? &d->entries[entry_index].value
                    : &d->entries[entry_index].key;
            }
        } else if (family == 4) {
            PySetObject *s = (PySetObject *)o;
            if (s->entries != NULL) {
                PyObject **candidate = &s->entries[cursor].key;
                if (*candidate != NULL && *candidate != py_set_dummy) {
                    slot = candidate;
                }
            }
        } else if (family == 5) {
            slot = &((PyVThreadChannelCoreObject *)o)->items[cursor];
        } else if (family == 6) {
            slot = &((PyVThreadChannelEndpointObject *)o)->core;
        } else if (family == 7) {
            slot = fixed_slots[cursor];
            if (fixed_roles[cursor] != 0) role = fixed_roles[cursor];
        } else if (family == 8) {
            PyContinuationStackChunk *chunk =
                ((PyContinuationObject *)o)->stack_chunk;
            if (chunk != NULL && chunk->slots != NULL) {
                slot = &chunk->slots[cursor];
            }
        } else if (family == 9) {
            PyClassObject *cls = (PyClassObject *)o;
            int64_t n_bases = cls->n_bases < 0 ? 0 : cls->n_bases;
            int64_t n_mro = cls->n_mro < 0 ? 0 : cls->n_mro;
            int64_t n_methods = cls->n_methods < 0 ? 0 : cls->n_methods;
            if (cursor < n_bases) {
                if (cls->bases != NULL) slot = (PyObject **)&cls->bases[cursor];
                role = PY_OBJ_SLOT_BORROWED_TRACED;
            } else if (cursor < n_bases + n_mro) {
                int64_t index = cursor - n_bases;
                if (cls->mro != NULL) slot = (PyObject **)&cls->mro[index];
                role = PY_OBJ_SLOT_BORROWED_TRACED;
            } else if (cursor < n_bases + n_mro + n_methods) {
                int64_t index = cursor - n_bases - n_mro;
                if (cls->methods != NULL) slot = &cls->methods[index].func;
                role = PY_OBJ_SLOT_BORROWED_UPDATE_ONLY;
            } else if (cursor == n_bases + n_mro + n_methods) {
                slot = &cls->del_method;
                role = PY_OBJ_SLOT_BORROWED_UPDATE_ONLY;
            } else if (cursor == n_bases + n_mro + n_methods + 1) {
                slot = &cls->attrs;
            } else {
                slot = (PyObject **)&cls->metaclass;
                role = PY_OBJ_SLOT_BORROWED_TRACED;
            }
        } else if (family == 10) {
            PyInstanceObject *inst = (PyInstanceObject *)o;
            if (cursor == 0) {
                slot = (PyObject **)&inst->cls;
                role = PY_OBJ_SLOT_BORROWED_TRACED;
            } else {
                slot = &inst->fields[cursor - 1];
            }
        }
        if (slot != NULL) visit(slot, role, ctx);
        cursor++;
        examined++;
    }
    state_out[0] = cursor >= total ? -1 : cursor;
    state_out[1] = examined;
    return 1;
}

typedef struct {
    void (*visit)(PyObject *child);
} PccGcTraceOwnerSlotCtx;

static void pcc_gc_trace_owner_slot(
    PyObject **slot,
    int32_t role,
    void *ctx
) {
    PccGcTraceOwnerSlotCtx *trace_ctx = (PccGcTraceOwnerSlotCtx *)ctx;
    if (role == PY_OBJ_SLOT_BORROWED_UPDATE_ONLY) return;
    if (slot == NULL || trace_ctx == NULL || trace_ctx->visit == NULL) return;
    PyObject *child = pcc_gc_load_ptr(NULL, slot);
    if (child == NULL) return;
    trace_ctx->visit(child);
}

static int pcc_gc_trace_cext_claim_unlocked(
    PyObject *obj,
    PccGcTraceCextCtx *ctx
) {
    if (
        obj == NULL
        || pcc_gc_trace_cext_pending_obj != NULL
        || pcc_capi_is_cext_type_tag((int64_t)py_header(obj)->type_tag) == 0
    ) return 0;
    py_incref(obj);
    int64_t epoch = pcc_gc_tracing_cycle_epoch_load();
    int64_t backend = pcc_gc_selected_backend;
    if (ctx != NULL) {
        ctx->obj = obj;
        ctx->epoch = epoch;
        ctx->backend = backend;
    }
    pcc_gc_trace_cext_pending_obj = obj;
    pcc_gc_trace_cext_pending_epoch = epoch;
    pcc_gc_trace_cext_pending_backend = backend;
    return 1;
}

static void pcc_gc_trace_cext_slot_transaction(
    PyObject **slot,
    int32_t role,
    void *raw_ctx
) {
    PccGcTraceCextCtx *ctx = (PccGcTraceCextCtx *)raw_ctx;
    if (ctx == NULL || role == PY_OBJ_SLOT_BORROWED_UPDATE_ONLY) return;
    pcc_gc_graph_lock();
    if (
        pcc_gc_trace_cext_pending_obj == ctx->obj
        && pcc_gc_trace_cext_pending_epoch == ctx->epoch
        && pcc_gc_trace_cext_pending_backend == ctx->backend
        && pcc_gc_tracing_cycle_epoch_load() == ctx->epoch
        && pcc_gc_selected_backend == ctx->backend
        && pcc_gc_mark_active_load() != 0
    ) {
        PccGcTraceOwnerSlotCtx trace_ctx = { pcc_gc_gray_object };
        pcc_gc_trace_owner_slot(slot, role, &trace_ctx);
    }
    pcc_gc_graph_unlock();
}

static int pcc_gc_trace_cext_complete(PccGcTraceCextCtx *ctx) {
    if (ctx == NULL || ctx->obj == NULL) return 0;
    (void)py_obj_visit_slots(
        ctx->obj,
        pcc_gc_trace_cext_slot_transaction,
        ctx
    );
    int committed = 0;
    pcc_gc_graph_lock();
    if (
        pcc_gc_trace_cext_pending_obj == ctx->obj
        && pcc_gc_trace_cext_pending_epoch == ctx->epoch
        && pcc_gc_trace_cext_pending_backend == ctx->backend
        && pcc_gc_tracing_cycle_epoch_load() == ctx->epoch
        && pcc_gc_selected_backend == ctx->backend
        && pcc_gc_mark_active_load() != 0
        && pcc_gc_is_known_object(ctx->obj)
    ) {
        PyObjectHeader *h = py_header(ctx->obj);
        if ((py_header_flags_load(h) & PY_FLAG_GC_GRAY) != 0) {
            pcc_gc_gray_count_dec();
            py_header_flags_update(
                h, PY_FLAG_GC_COLOR_MASK, PY_FLAG_GC_BLACK
            );
            committed = 1;
        }
    }
    if (pcc_gc_trace_cext_pending_obj == ctx->obj) {
        pcc_gc_trace_cext_pending_obj = NULL;
        pcc_gc_trace_cext_pending_epoch = 0;
        pcc_gc_trace_cext_pending_backend = -1;
    }
    pcc_gc_graph_unlock();
    py_decref(ctx->obj);
    ctx->obj = NULL;
    return committed;
}

typedef struct {
    void (*update)(PyObject **slot);
} PccGcUpdateOwnerSlotCtx;

static void pcc_gc_update_owner_slot(
    PyObject **slot,
    int32_t role,
    void *ctx
) {
    PccGcUpdateOwnerSlotCtx *update_ctx = (PccGcUpdateOwnerSlotCtx *)ctx;
    (void)role;
    if (slot == NULL || update_ctx == NULL || update_ctx->update == NULL) {
        return;
    }
    update_ctx->update(slot);
}

typedef struct {
    int recurse;
} PccGcPromoteOwnerSlotCtx;

static void pcc_gc_backend3_enqueue_promotion_owner(PyObject *o) {
    if (
        (
            pcc_gc_selected_backend != PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
            && pcc_gc_selected_backend != PCC_GC_KIND_COLORED_RELOCATING
        )
        || o == NULL
        || PY_IS_TAGGED_INT(o)
    ) return;
    PccGcObjectNode *node =
        (PccGcObjectNode *)pcc_gc_object_index_find(o);
    if (
        !pcc_gc_object_node_is_active(node)
        || node->obj != o
        || (py_header_flags_load(py_header(o)) & PY_FLAG_GC_YOUNG) != 0
        || node == pcc_gc_backend3_promotion_head
        || node == pcc_gc_backend3_promotion_tail
        || node->young_next != NULL
        || node->young_prev != NULL
    ) return;
    node->young_next = NULL;
    node->young_prev = pcc_gc_backend3_promotion_tail;
    node->gc_refs = 0;
    if (pcc_gc_backend3_promotion_tail != NULL) {
        pcc_gc_backend3_promotion_tail->young_next = node;
    } else {
        pcc_gc_backend3_promotion_head = node;
    }
    pcc_gc_backend3_promotion_tail = node;
    pcc_gc_backend3_promotion_revision = pcc_gc_object_list_revision;
}

static void pcc_gc_promote_owner_slot(
    PyObject **slot,
    int32_t role,
    void *ctx
) {
    PccGcPromoteOwnerSlotCtx *promote_ctx = (
        PccGcPromoteOwnerSlotCtx *
    )ctx;
    if (slot == NULL || promote_ctx == NULL) return;
    if (role == PY_OBJ_SLOT_OWNED) {
        pcc_gc_promote_young_slot_with_mode(slot, promote_ctx->recurse);
    } else {
        pcc_gc_promote_young_borrowed_slot_with_mode(
            slot,
            promote_ctx->recurse
        );
    }
}

static void pcc_gc_promote_cext_slot_transaction(
    PyObject **slot,
    int32_t role,
    void *ctx
) {
    pcc_gc_graph_lock();
    pcc_gc_promote_owner_slot(slot, role, ctx);
    pcc_gc_graph_unlock();
}

static void pcc_gc_promote_cext_owner_referents_unlocked(PyObject *o) {
    PccGcPromoteOwnerSlotCtx promote_ctx = {1};
    (void)py_obj_visit_slots(
        o,
        pcc_gc_promote_cext_slot_transaction,
        &promote_ctx
    );
}

void pcc_gc_backend3_promotion_probe_config(int64_t pause) {
    __atomic_store_n(
        &pcc_gc_backend3_promotion_probe_state_value,
        0,
        __ATOMIC_RELEASE
    );
    __atomic_store_n(
        &pcc_gc_backend3_promotion_probe_pause,
        pause,
        __ATOMIC_RELEASE
    );
}

int64_t pcc_gc_backend3_promotion_probe_state(void) {
    return __atomic_load_n(
        &pcc_gc_backend3_promotion_probe_state_value,
        __ATOMIC_ACQUIRE
    );
}

static int64_t pcc_gc_backend3_drain_promotion_worklist(int64_t budget) {
    if (budget <= 0) return 0;
    int64_t total_examined = 0;
    while (total_examined < budget) {
        int64_t batch_limit = budget - total_examined;
        if (batch_limit > PCC_GC_SAFEPOINT_BATCH) {
            batch_limit = PCC_GC_SAFEPOINT_BATCH;
        }
        int64_t batch_examined = 0;
        int more_work = 0;
        PyObject *callback_owner = NULL;
        pcc_gc_graph_lock();
        while (
            pcc_gc_backend3_promotion_head != NULL
            && batch_examined < batch_limit
        ) {
            PccGcObjectNode *node = pcc_gc_backend3_promotion_head;
            if (
                !pcc_gc_object_node_is_active(node)
                || pcc_gc_object_index_find(node->obj) != node
            ) {
                pcc_gc_backend3_promotion_unlink_unlocked(node);
                batch_examined++;
                total_examined++;
                continue;
            }
            if (
                pcc_gc_backend3_promotion_revision
                != pcc_gc_object_list_revision
            ) {
                /* Object-list mutation is harmless after the node/index/live
                 * revalidation above: promotion is monotonic and the logical
                 * cursor never retains another node or a raw slot address. */
                pcc_gc_backend3_promotion_revision =
                    pcc_gc_object_list_revision;
            }
            int64_t state[2] = {-1, 0};
            int64_t handled = pcc_gc_visit_object_slots_slice(
                node->obj,
                node->gc_refs,
                batch_limit - batch_examined,
                pcc_gc_promote_owner_slot,
                &(PccGcPromoteOwnerSlotCtx){1},
                state
            );
            if (!handled) {
                /* C-extension slots are external callbacks and remain a
                 * separately classified holder.  Pin this non-moving owner,
                 * detach it from the worklist, and invoke tp_traverse only
                 * after the graph lock is released.  Each reported slot
                 * re-enters one short graph transaction. */
                py_incref(node->obj);
                callback_owner = node->obj;
                pcc_gc_backend3_promotion_unlink_unlocked(node);
                state[0] = -1;
                state[1] = 1;
            }
            if (state[1] <= 0) {
                state[0] = -1;
                state[1] = 1;
            }
            batch_examined += state[1];
            total_examined += state[1];
            if (state[0] < 0) {
                pcc_gc_backend3_promotion_unlink_unlocked(node);
            } else {
                node->gc_refs = state[0];
                pcc_gc_backend3_promotion_revision =
                    pcc_gc_object_list_revision;
            }
            if (callback_owner != NULL) break;
        }
        more_work = pcc_gc_backend3_promotion_head != NULL;
        pcc_gc_graph_unlock();
        if (callback_owner != NULL) {
            pcc_gc_promote_cext_owner_referents_unlocked(callback_owner);
            py_decref(callback_owner);
            pcc_gc_graph_lock();
            more_work = pcc_gc_backend3_promotion_head != NULL;
            pcc_gc_graph_unlock();
        }
        if (
            more_work
            && total_examined >= PCC_GC_SAFEPOINT_BATCH
            && __atomic_load_n(
                &pcc_gc_backend3_promotion_probe_pause,
                __ATOMIC_ACQUIRE
            ) != 0
        ) {
            __atomic_store_n(
                &pcc_gc_backend3_promotion_probe_state_value,
                1,
                __ATOMIC_RELEASE
            );
            while (__atomic_load_n(
                &pcc_gc_backend3_promotion_probe_pause,
                __ATOMIC_ACQUIRE
            ) != 0) {
                sched_yield();
            }
            __atomic_store_n(
                &pcc_gc_backend3_promotion_probe_state_value,
                2,
                __ATOMIC_RELEASE
            );
        }
        if (batch_examined > 0) pcc_thread_safepoint();
        if (batch_examined == 0 || !more_work) break;
    }
    return total_examined;
}

static void pcc_gc_promote_owner_referents(PyObject *o, int recurse) {
    if (o == NULL || PY_IS_TAGGED_INT(o)) return;
    if (!recurse) return;
    pcc_gc_backend3_enqueue_promotion_owner(o);
}

static void pcc_gc_promote_remembered_owner_referents(PyObject *o) {
    pcc_gc_promote_owner_referents(o, 1);
}

static void pcc_gc_trace_referents(
    PyObject *o,
    void (*visit)(PyObject *child)
) {
    if (o == NULL || PY_IS_TAGGED_INT(o)) return;
    PccGcTraceOwnerSlotCtx trace_ctx = { visit };
    (void)py_obj_visit_slots(o, pcc_gc_trace_owner_slot, &trace_ctx);
}

/* Slot-ADDRESS flavored sibling of pcc_gc_trace_referents, for the
 * backend-4 remap phase (docs/plans/gc4-relocation-remap-plan.md):
 * `update` receives pointer-slot addresses that may need forwarding
 * rewrites. Object-reference slots that participate in tracing must stay
 * in sync with pcc_gc_trace_referents above: a type traced but not updated
 * keeps stale pointers alive past forwarding retirement. Some additional
 * non-owning borrowed metadata slots are intentionally update-only for
 * relocation healing/promotion and must not become mark roots. */
void pcc_gc_update_referents(
    PyObject *o,
    void (*update)(PyObject **slot)
) {
    if (o == NULL || PY_IS_TAGGED_INT(o)) return;
    PccGcUpdateOwnerSlotCtx update_ctx = { update };
    (void)py_obj_visit_slots(o, pcc_gc_update_owner_slot, &update_ctx);
}

static int64_t pcc_gc_cms_trace_gray_object_unlocked(PyObject *o) {
    if (pcc_gc_mark_active_load() == 0) return 0;
    if (o == NULL || PY_IS_TAGGED_INT(o)) return 0;
    if (!pcc_gc_is_known_object(o)) return 0;
    PyObjectHeader *h = py_header(o);
    if ((py_header_flags_load(h) & PY_FLAG_GC_GRAY) == 0) return 0;
    if (
        pcc_capi_is_cext_type_tag((int64_t)h->type_tag) != 0
        && pcc_gc_trace_cext_claim_unlocked(o, NULL)
    ) {
        return 1;
    }
    pcc_gc_trace_referents(o, pcc_gc_gray_object);
    py_header_flags_update(h, PY_FLAG_GC_COLOR_MASK, PY_FLAG_GC_BLACK);
    return 1;
}

int64_t pcc_gc_cms_direct_gray_probe_run(PyObject *o) {
    if (o == NULL || PY_IS_TAGGED_INT(o)) return 0;
    PccGcTraceCextCtx cext_ctx = {0};
    pcc_gc_graph_lock();
    if (
        (
            pcc_gc_selected_backend != PCC_GC_KIND_INCREMENTAL_TRICOLOR
            && pcc_gc_selected_backend != PCC_GC_KIND_CONCURRENT_MARK_SWEEP
        )
        || pcc_gc_mark_active_load() == 0
        || !pcc_gc_is_known_object(o)
    ) {
        pcc_gc_graph_unlock();
        return 0;
    }
    pcc_gc_gray_object(o);
    int64_t traced = pcc_gc_cms_trace_gray_object_unlocked(o);
    if (pcc_gc_trace_cext_pending_obj != NULL) {
        cext_ctx.obj = pcc_gc_trace_cext_pending_obj;
        cext_ctx.epoch = pcc_gc_trace_cext_pending_epoch;
        cext_ctx.backend = pcc_gc_trace_cext_pending_backend;
    }
    pcc_gc_graph_unlock();
    if (cext_ctx.obj != NULL) {
        (void)pcc_gc_trace_cext_complete(&cext_ctx);
    }
    return traced;
}

static int pcc_gc_gray_exists(void) {
    for (PccGcObjectNode *n = pcc_gc_objects; n != NULL; n = n->next) {
        if (!pcc_gc_object_node_is_active(n)) continue;
        if (
            (py_header_flags_load(py_header(n->obj)) & PY_FLAG_GC_GRAY)
            != 0
        ) return 1;
    }
    return 0;
}

static int64_t pcc_gc_root_slot_count_from_map(const int32_t *frame_map) {
    if (frame_map == NULL) return 0;
    int32_t n_slots = frame_map[0];
    if (n_slots == -2147483647 - 1) return 0;
    if (n_slots < 0) n_slots = -n_slots;
    if (n_slots > 100000) return 0;
    return (int64_t)n_slots;
}

static int pcc_gc_root_map_is_borrowed(const int32_t *frame_map) {
    return frame_map != NULL && frame_map[0] < 0;
}

static void pcc_gc_gray_runtime_root(PyObject *root, void *ctx) {
    (void)ctx;
    pcc_gc_gray_root_object(root);
}

typedef struct {
    int64_t epoch;
    int64_t backend;
} PccGcTraceExtensionRootCtx;

static void pcc_gc_trace_extension_state_root(
    PyObject *root,
    void *ctx
) {
    if (root == NULL || PY_IS_TAGGED_INT(root) || ctx == NULL) return;
    PccGcTraceExtensionRootCtx *root_ctx = (
        PccGcTraceExtensionRootCtx *
    )ctx;
    pcc_gc_graph_lock();
    if (
        pcc_gc_trace_extension_roots_pending == 2
        && pcc_gc_trace_extension_roots_epoch == root_ctx->epoch
        && pcc_gc_trace_extension_roots_backend == root_ctx->backend
        && pcc_gc_tracing_cycle_epoch_load() == root_ctx->epoch
        && pcc_gc_selected_backend == root_ctx->backend
        && pcc_gc_mark_active_load() != 0
    ) {
        pcc_gc_gray_root_object(root);
    }
    pcc_gc_graph_unlock();
}

static void pcc_gc_trace_final_extension_state_root(
    PyObject *root,
    void *ctx
) {
    if (root == NULL || PY_IS_TAGGED_INT(root) || ctx == NULL) return;
    PccGcTraceExtensionRootCtx *root_ctx = (
        PccGcTraceExtensionRootCtx *
    )ctx;
    pcc_gc_graph_lock();
    if (
        pcc_gc_trace_extension_roots_pending == 3
        && pcc_gc_trace_extension_roots_epoch == root_ctx->epoch
        && pcc_gc_trace_extension_roots_backend == root_ctx->backend
        && pcc_gc_tracing_finish_claim_epoch_load() == root_ctx->epoch
        && pcc_gc_tracing_finish_claim_backend_load() == root_ctx->backend
        && pcc_gc_tracing_cycle_epoch_load() == root_ctx->epoch
        && pcc_gc_selected_backend == root_ctx->backend
        && pcc_gc_mark_active_load() != 0
    ) {
        pcc_gc_gray_root_object(root);
    }
    pcc_gc_graph_unlock();
}

static int pcc_gc_trace_extension_roots_claim_unlocked(
    PccGcTraceExtensionRootCtx *root_ctx
) {
    if (root_ctx == NULL) return 0;
    if (
        pcc_gc_trace_extension_roots_pending != 1
        || pcc_gc_mark_active_load() == 0
        || pcc_gc_trace_extension_roots_epoch
            != pcc_gc_tracing_cycle_epoch_load()
        || pcc_gc_trace_extension_roots_backend != pcc_gc_selected_backend
    ) {
        return 0;
    }
    root_ctx->epoch = pcc_gc_trace_extension_roots_epoch;
    root_ctx->backend = pcc_gc_trace_extension_roots_backend;
    pcc_gc_trace_extension_roots_pending = 2;
    return 1;
}

static int pcc_gc_trace_extension_roots_complete(
    PccGcTraceExtensionRootCtx *root_ctx
) {
    if (root_ctx == NULL) return 0;
    pcc_capi_visit_extension_module_state_roots(
        pcc_gc_trace_extension_state_root,
        root_ctx
    );
    pcc_gc_graph_lock();
    int valid = (
        pcc_gc_trace_extension_roots_pending == 2
        && pcc_gc_trace_extension_roots_epoch == root_ctx->epoch
        && pcc_gc_trace_extension_roots_backend == root_ctx->backend
        && pcc_gc_tracing_cycle_epoch_load() == root_ctx->epoch
        && pcc_gc_selected_backend == root_ctx->backend
        && pcc_gc_mark_active_load() != 0
    );
    if (valid) pcc_gc_trace_extension_roots_pending = 0;
    pcc_gc_graph_unlock();
    return valid;
}

static void pcc_gc_gray_current_roots(void) {
    for (PccGcObjectNode *n = pcc_gc_objects; n != NULL; n = n->next) {
        if (!pcc_gc_object_node_is_active(n)) continue;
        PyObjectHeader *h = py_header(n->obj);
        if ((py_header_flags_load(h) & PY_FLAG_GC_PINNED) != 0) {
            pcc_gc_gray_root_object(n->obj);
        }
    }
    PccGcGrayMappedRootSlotCtx root_ctx = { 1 };
    for (PccGcFrameNode *f = pcc_gc_frames; f != NULL; f = f->next) {
        (void)pcc_gc_visit_mapped_root_slots_unlocked(
            f->root_count,
            f->slots,
            NULL,
            f->borrowed & PCC_GC_FRAME_NODE_FLAG_BORROWED,
            pcc_gc_gray_mapped_root_slot,
            &root_ctx
        );
    }
    for (
        PccGcContinuationRootNode *c = pcc_gc_continuation_roots;
        c != NULL;
        c = c->next
    ) {
        (void)pcc_gc_visit_mapped_root_slots_unlocked(
            c->root_count,
            c->slots,
            NULL,
            c->borrowed,
            pcc_gc_gray_mapped_root_slot,
            &root_ctx
        );
    }
    (void)pcc_gc_visit_scheduler_root_slots_unlocked(
        pcc_gc_gray_mapped_root_slot,
        &root_ctx
    );
    (void)pcc_gc_visit_builtin_exception_cache_slots_unlocked(
        pcc_gc_gray_mapped_root_slot,
        &root_ctx
    );
}

static void pcc_gc_subtract_known_child_ref(PyObject *child) {
    if (child == NULL || PY_IS_TAGGED_INT(child)) return;
    PccGcForwardNode *forwarding = pcc_gc_forwarding_find(child);
    if (forwarding != NULL && forwarding->to != NULL) {
        child = forwarding->to;
    }
    PccGcObjectNode *child_node = (PccGcObjectNode *)pcc_gc_object_index_find(child);
    if (child_node == NULL || !pcc_gc_object_node_is_active(child_node)) return;
    child_node->gc_refs--;
}

static void pcc_gc_gray_refcount_external_roots(void) {
    for (PccGcObjectNode *n = pcc_gc_objects; n != NULL; n = n->next) {
        if (!pcc_gc_object_node_is_active(n)) continue;
        n->gc_refs = pcc_refcount_load(&py_header(n->obj)->refcount);
    }
    for (PccGcObjectNode *n = pcc_gc_objects; n != NULL; n = n->next) {
        if (!pcc_gc_object_node_is_active(n)) continue;
        pcc_gc_trace_referents(n->obj, pcc_gc_subtract_known_child_ref);
    }
    for (PccGcObjectNode *n = pcc_gc_objects; n != NULL; n = n->next) {
        if (!pcc_gc_object_node_is_active(n)) continue;
        if (n->gc_refs > 0) {
            pcc_gc_gray_root_object(n->obj);
        }
    }
}

static void pcc_gc_runtime_root_snapshot_reset_unlocked(void) {
    pcc_gc_runtime_root_snapshot_phase = 0;
    pcc_gc_runtime_root_snapshot_slot = -1;
    pcc_gc_runtime_root_snapshot_frame_cursor = NULL;
    pcc_gc_runtime_root_snapshot_continuation_cursor = NULL;
    pcc_gc_runtime_root_snapshot_scheduler_cursor = NULL;
}

void pcc_gc_runtime_root_snapshot_probe_config(int64_t pause) {
    __atomic_store_n(
        &pcc_gc_runtime_root_snapshot_probe_pause,
        pause != 0,
        __ATOMIC_RELEASE
    );
}

int64_t pcc_gc_runtime_root_snapshot_probe_state(void) {
    return __atomic_load_n(
        &pcc_gc_runtime_root_snapshot_probe_state_value,
        __ATOMIC_ACQUIRE
    );
}

static void pcc_gc_runtime_root_snapshot_probe_wait(void) {
    if (__atomic_load_n(
            &pcc_gc_runtime_root_snapshot_probe_pause,
            __ATOMIC_ACQUIRE
        ) == 0) return;
    __atomic_store_n(
        &pcc_gc_runtime_root_snapshot_probe_state_value,
        1,
        __ATOMIC_RELEASE
    );
    while (__atomic_load_n(
            &pcc_gc_runtime_root_snapshot_probe_pause,
            __ATOMIC_ACQUIRE
        ) != 0) {
        pcc_thread_safepoint();
    }
    __atomic_store_n(
        &pcc_gc_runtime_root_snapshot_probe_state_value,
        0,
        __ATOMIC_RELEASE
    );
}

static int64_t pcc_gc_runtime_root_snapshot_fill_batch_unlocked(
    PyObject **roots,
    int64_t capacity,
    int64_t *count,
    int64_t budget,
    int32_t *complete
) {
    if (
        roots == NULL
        || capacity < 0
        || count == NULL
        || budget <= 0
        || complete == NULL
        || pcc_gc_runtime_root_snapshot_owner == 0
    ) return -1;
    PccGcRuntimeRootSnapshotCtx snapshot_ctx = {
        roots, capacity, *count, 0
    };
    int64_t examined = 0;
    *complete = 0;
    while (examined < budget && snapshot_ctx.count < capacity) {
        if (pcc_gc_runtime_root_snapshot_phase == 0) {
            if (pcc_gc_runtime_root_snapshot_slot < 0) {
                pcc_gc_runtime_root_snapshot_frame_cursor = pcc_gc_frames;
                pcc_gc_runtime_root_snapshot_slot = 0;
            }
            PccGcFrameNode *f = pcc_gc_runtime_root_snapshot_frame_cursor;
            if (f == NULL) {
                pcc_gc_runtime_root_snapshot_phase = 1;
                pcc_gc_runtime_root_snapshot_slot = -1;
                continue;
            }
            int64_t slot_index = pcc_gc_runtime_root_snapshot_slot;
            if (slot_index >= f->root_count) {
                pcc_gc_runtime_root_snapshot_frame_cursor = f->next;
                pcc_gc_runtime_root_snapshot_slot = 0;
                continue;
            }
            pcc_gc_snapshot_runtime_mapped_root_slot(
                &f->slots[slot_index], NULL, 0, &snapshot_ctx
            );
            examined++;
            pcc_gc_runtime_root_snapshot_slot = slot_index + 1;
            if (pcc_gc_runtime_root_snapshot_slot >= f->root_count) {
                pcc_gc_runtime_root_snapshot_frame_cursor = f->next;
                pcc_gc_runtime_root_snapshot_slot = 0;
            }
            continue;
        }

        if (pcc_gc_runtime_root_snapshot_phase == 1) {
            if (pcc_gc_runtime_root_snapshot_slot < 0) {
                pcc_gc_runtime_root_snapshot_continuation_cursor =
                    pcc_gc_continuation_roots;
                pcc_gc_runtime_root_snapshot_slot = 0;
            }
            PccGcContinuationRootNode *c =
                pcc_gc_runtime_root_snapshot_continuation_cursor;
            if (c == NULL) {
                pcc_gc_runtime_root_snapshot_phase = 2;
                pcc_gc_runtime_root_snapshot_slot = -1;
                continue;
            }
            int64_t slot_index = pcc_gc_runtime_root_snapshot_slot;
            if (slot_index >= c->root_count) {
                pcc_gc_runtime_root_snapshot_continuation_cursor = c->next;
                pcc_gc_runtime_root_snapshot_slot = 0;
                continue;
            }
            pcc_gc_snapshot_runtime_mapped_root_slot(
                &c->slots[slot_index], NULL, 0, &snapshot_ctx
            );
            examined++;
            pcc_gc_runtime_root_snapshot_slot = slot_index + 1;
            if (pcc_gc_runtime_root_snapshot_slot >= c->root_count) {
                pcc_gc_runtime_root_snapshot_continuation_cursor = c->next;
                pcc_gc_runtime_root_snapshot_slot = 0;
            }
            continue;
        }

        if (pcc_gc_runtime_root_snapshot_phase == 2) {
            if (pcc_gc_runtime_root_snapshot_slot < 0) {
                pcc_gc_runtime_root_snapshot_scheduler_cursor =
                    pcc_gc_scheduler_roots;
                pcc_gc_runtime_root_snapshot_slot = 0;
            }
            PccGcSchedulerRootNode *r =
                pcc_gc_runtime_root_snapshot_scheduler_cursor;
            if (r == NULL) {
                pcc_gc_runtime_root_snapshot_phase = 3;
                pcc_gc_runtime_root_snapshot_slot = 0;
                continue;
            }
            pcc_gc_runtime_root_snapshot_scheduler_cursor = r->next;
            if (r->slot != NULL) {
                pcc_gc_snapshot_runtime_mapped_root_slot(
                    r->slot, NULL, 0, &snapshot_ctx
                );
            }
            examined++;
            continue;
        }

        int64_t slot_index = pcc_gc_runtime_root_snapshot_slot;
        if (slot_index >= PY_EXC_N_BUILTIN) {
            pcc_gc_runtime_root_snapshot_reset_unlocked();
            *complete = 1;
            break;
        }
        PyObject **slot = (PyObject **)py_subs_exc_cache_slot(
            (int32_t)slot_index
        );
        if (slot != NULL) {
            pcc_gc_snapshot_runtime_mapped_root_slot(
                slot, NULL, 0, &snapshot_ctx
            );
        }
        pcc_gc_runtime_root_snapshot_slot = slot_index + 1;
        examined++;
    }
    *count = snapshot_ctx.count;
    if (snapshot_ctx.overflow != 0) return -1;
    return examined;
}

void pcc_gc_visit_runtime_roots(PccGcRootVisitor visit, void *ctx) {
    if (visit == NULL) return;
    pcc_gc_init_config();
    PyObject *stack_roots[64];
    PyObject **roots = stack_roots;
    int64_t capacity = 64;
    int64_t count = 0;
    int using_heap = 0;
    int64_t owner = pcc_current_thread_id();
    if (owner <= 0) {
        pcc_runtime_tripwire_fail(
            "pcc_gc_visit_runtime_roots: caller has no runtime thread identity",
            __FILE__,
            __LINE__
        );
        return;
    }
    for (;;) {
        pcc_gc_graph_lock();
        if (pcc_gc_runtime_root_snapshot_owner == 0) {
            pcc_gc_runtime_root_snapshot_owner = owner;
            pcc_gc_runtime_root_snapshot_reset_unlocked();
            pcc_gc_graph_unlock();
            break;
        }
        if (pcc_gc_runtime_root_snapshot_owner == owner) {
            pcc_gc_graph_unlock();
            pcc_runtime_tripwire_fail(
                "pcc_gc_visit_runtime_roots: recursive snapshot owner",
                __FILE__,
                __LINE__
            );
            return;
        }
        pcc_gc_graph_unlock();
        pcc_thread_safepoint();
    }

    int32_t complete = 0;
    int failed = 0;
    while (!complete) {
        if (count >= capacity) {
            if (capacity > (int64_t)(SIZE_MAX / (2 * sizeof(PyObject *)))) {
                failed = 1;
                break;
            }
            int64_t next_capacity = capacity * 2;
            PyObject **next_roots = (PyObject **)malloc(
                (size_t)next_capacity * sizeof(PyObject *)
            );
            if (next_roots == NULL) {
                failed = 1;
                break;
            }
            for (int64_t index = 0; index < count; index++) {
                next_roots[index] = roots[index];
            }
            if (using_heap) free(roots);
            roots = next_roots;
            capacity = next_capacity;
            using_heap = 1;
        }
        pcc_gc_graph_lock();
        int64_t examined =
            pcc_gc_runtime_root_snapshot_fill_batch_unlocked(
                roots,
                capacity,
                &count,
                PCC_GC_SAFEPOINT_BATCH,
                &complete
            );
        pcc_gc_graph_unlock();
        if (examined >= 0 && !complete) {
            pcc_gc_runtime_root_snapshot_probe_wait();
        }
        if (examined < 0 || (examined == 0 && !complete)) {
            failed = 1;
            break;
        }
    }

    pcc_gc_graph_lock();
    if (pcc_gc_runtime_root_snapshot_owner == owner) {
        pcc_gc_runtime_root_snapshot_owner = 0;
        pcc_gc_runtime_root_snapshot_reset_unlocked();
    }
    pcc_gc_graph_unlock();

    if (failed) {
        for (int64_t index = 0; index < count; index++) {
            if (roots[index] != NULL) py_decref(roots[index]);
        }
        if (using_heap) free(roots);
        pcc_runtime_tripwire_fail(
            "pcc_gc_visit_runtime_roots: bounded snapshot failed",
            __FILE__,
            __LINE__
        );
        return;
    }

    for (int64_t index = 0; index < count; index++) {
        visit(roots[index], ctx);
        if (roots[index] != NULL) py_decref(roots[index]);
    }
    if (using_heap) free(roots);
    pcc_capi_visit_extension_module_state_roots(visit, ctx);
}

/* ----- backend-4 remap phase (gc4-relocation-remap-plan.md stage 2) -----
 *
 * Count-on-NEW accounting (relocate_copy moves the outstanding count to
 * the new copy; old copies are immortal shells) means refcounts never
 * drain old copies. Instead, this pass runs at a safepoint when an
 * evacuation drain has emptied the relocation set: it rewrites every
 * candidate pointer (objects' slots, frames, continuation/scheduler
 * roots) through the forwarding table, then retires the table and the
 * shells' bookkeeping. Pages whose last forwarding entry is removed are
 * PARKED and only destroyed at the NEXT remap (one-epoch defer), so
 * stale SSA/borrowed pointers from the interrupted frame can still
 * read old headers safely until then. */

static PccGcZPage *pcc_gc_backend4_parked_pages = NULL;

static void pcc_gc_backend4_park_page_unlocked(struct PccGcZPage *page) {
    if (page == NULL) return;
    pcc_gc_backend4_active_page_clear_unlocked(page);
    page->object_head = NULL;
    page->next = pcc_gc_backend4_parked_pages;
    pcc_gc_backend4_parked_pages = page;
}

static void pcc_gc_backend4_drain_parked_pages_unlocked(void) {
    while (pcc_gc_backend4_parked_pages != NULL) {
        PccGcZPage *page = pcc_gc_backend4_parked_pages;
        pcc_gc_backend4_parked_pages = page->next;
        page->next = NULL;
        pcc_gc_backend4_zpage_destroy_unlocked(page);
    }
}

static PccGcZPage *pcc_gc_backend4_release_retained_pages_unlocked(void) {
    PccGcZPage *page = pcc_gc_backend4_retained_pages;
    PccGcZPage *released_pages = NULL;
    pcc_gc_backend4_retained_pages = NULL;
    while (page != NULL) {
        PccGcZPage *next = page->next;
        page->next = NULL;
        if (
            page->object_count > 0
            || page->pending_alloc_count > 0
            || page->pending_forwardings > 0
        ) {
            /* Fail closed on lifecycle drift: an unsafe page stays
             * quarantined instead of being physically released. */
            page->next = pcc_gc_backend4_retained_pages;
            pcc_gc_backend4_retained_pages = page;
        } else {
            page->next = released_pages;
            released_pages = page;
        }
        page = next;
    }
    return released_pages;
}

static void pcc_gc_backend4_finish_retained_page_releases(
    PccGcZPage *pages
) {
    while (pages != NULL) {
        PccGcZPage *page = pages;
        pages = page->next;
        page->next = NULL;
        free(page->span_base);
        page->span_base = NULL;
        page->span_capacity_bytes = 0;
        free(page);
    }
}

static void pcc_gc_backend4_finish_remap_retirement(
    PccGcBackend4RemapFinish *finish
) {
    if (finish == NULL) return;
    PccGcZPage *released_pages = finish->released_pages;
    PccGcForwardNode *forwardings = finish->forwardings;
    PccGcIdentityNode *identities = finish->identities;
    PccGcObjectNode *object_nodes = finish->object_nodes;
    void *payload_plans = finish->payload_plans;
    PccGcForwardNode *dead_targets = finish->dead_target_forwardings;
    finish->released_pages = NULL;
    finish->forwardings = NULL;
    finish->identities = NULL;
    finish->object_nodes = NULL;
    finish->payload_plans = NULL;
    finish->dead_target_forwardings = NULL;
    pcc_gc_backend4_finish_retained_page_releases(released_pages);
    pcc_gc_relocation_finish_source_payloads(payload_plans);
    pcc_gc_forwarding_finish_detached(forwardings);
    pcc_gc_forwarding_finish_dead_targets(dead_targets);
    pcc_gc_identity_finish_detached(identities);
    pcc_gc_object_node_finish_detached(object_nodes);
}

static void pcc_gc_backend4_remap_heal_slot(PyObject **slot) {
    PyObject *v = *slot;
    if (v == NULL || PY_IS_TAGGED_INT(v)) return;
    if (
        (py_header_flags_load(py_header(v)) & PY_FLAG_GC_RELOCATION_CANDIDATE)
        == 0
    ) {
        return;
    }
    PccGcForwardNode *n = pcc_gc_forwarding_find(v);
    if (n == NULL || n->to == NULL) return;
    /* bits only: under count-on-NEW the slot's reference is already
     * accounted on the new copy */
    int target_type_matches = (
        py_header(v)->type_tag == py_header(n->to)->type_tag
    );
    PCC_GC_MIXED_TRIPWIRE(
        target_type_matches,
        "pcc_gc_backend4_remap_heal_slot: remap target type_tag differs from the old shell (zeroed/corrupt relocation target)"
    );
    if (!target_type_matches) return;
    *slot = n->to;
}

void py_obj_update_slot(PyObject **slot) {
    pcc_gc_backend4_remap_heal_slot(slot);
}

static int pcc_gc_backend4_remap_cext_ctx_valid_unlocked(
    PccGcBackend4RemapCextCtx *ctx
) {
    return ctx != NULL
        && pcc_gc_backend4_remap_active != 0
        && pcc_gc_backend4_remap_epoch == ctx->epoch
        && pcc_gc_backend4_remap_pending_obj == ctx->obj
        && pcc_gc_selected_backend == PCC_GC_KIND_COLORED_RELOCATING
        && pcc_gc_object_list_revision == ctx->object_revision
        && pcc_gc_forwardings == ctx->forwarding_head
        && pcc_gc_forwarding_population == ctx->forwarding_population
        && pcc_gc_backend4_reseed_page_revision == ctx->page_revision
        && pcc_gc_backend4_reseed_relocation_revision
            == ctx->relocation_revision;
}

static void pcc_gc_backend4_remap_cext_slot_transaction(
    PyObject **slot,
    int32_t role,
    void *raw_ctx
) {
    (void)role;
    PccGcBackend4RemapCextCtx *ctx = (
        PccGcBackend4RemapCextCtx *
    )raw_ctx;
    if (slot == NULL || ctx == NULL) return;
    pcc_gc_graph_lock();
    if (pcc_gc_backend4_remap_cext_ctx_valid_unlocked(ctx)) {
        pcc_gc_backend4_remap_heal_slot(slot);
    }
    pcc_gc_graph_unlock();
}

static int pcc_gc_backend4_remap_cext_complete(
    PccGcBackend4RemapCextCtx *ctx
) {
    if (ctx == NULL || ctx->obj == NULL) return 0;
    (void)py_obj_visit_slots(
        ctx->obj,
        pcc_gc_backend4_remap_cext_slot_transaction,
        ctx
    );
    pcc_gc_graph_lock();
    int valid = pcc_gc_backend4_remap_cext_ctx_valid_unlocked(ctx);
    if (pcc_gc_backend4_remap_pending_obj == ctx->obj) {
        pcc_gc_backend4_remap_pending_obj = NULL;
    }
    pcc_gc_graph_unlock();
    py_decref(ctx->obj);
    ctx->obj = NULL;
    return valid;
}

int64_t pcc_gc_backend4_remap_and_retire_stopped_world(void) {
    int64_t owns_stopped_world = pcc_thread_owns_stopped_world();
    int acquired_stopped_world = 0;
    if (owns_stopped_world == 0) {
        if (pcc_stop_the_world() != 0) return 0;
        acquired_stopped_world = 1;
    }

    PccGcBackend4RemapFinish finish = {0};
    PccGcBackend4RemapCextCtx ctx = {0};
    PccGcObjectNode *cursor = NULL;
    int valid = 0;
    pcc_gc_graph_lock();
    if (
        pcc_gc_selected_backend == PCC_GC_KIND_COLORED_RELOCATING
        && pcc_gc_relocation_set == NULL
        && pcc_gc_forwarding_population > 0
        && pcc_gc_backend4_remap_active == 0
        && pcc_gc_backend4_remap_epoch < INT64_MAX
    ) {
        pcc_gc_backend4_remap_epoch++;
        pcc_gc_backend4_remap_active = 1;
        pcc_gc_backend4_remap_pending_obj = NULL;
        ctx.epoch = pcc_gc_backend4_remap_epoch;
        ctx.object_revision = pcc_gc_object_list_revision;
        ctx.forwarding_head = pcc_gc_forwardings;
        ctx.forwarding_population = pcc_gc_forwarding_population;
        ctx.page_revision = pcc_gc_backend4_reseed_page_revision;
        ctx.relocation_revision =
            pcc_gc_backend4_reseed_relocation_revision;
        cursor = pcc_gc_objects;
        valid = 1;
    }
    pcc_gc_graph_unlock();

    if (!valid) {
        if (acquired_stopped_world) (void)pcc_resume_world();
        pcc_gc_backend4_finish_remap_retirement(&finish);
        return 0;
    }

    while (valid && cursor != NULL) {
        pcc_gc_graph_lock();
        valid = pcc_gc_backend4_remap_active != 0
            && pcc_gc_backend4_remap_epoch == ctx.epoch
            && pcc_gc_backend4_remap_pending_obj == NULL
            && pcc_gc_selected_backend == PCC_GC_KIND_COLORED_RELOCATING
            && pcc_gc_object_list_revision == ctx.object_revision
            && pcc_gc_forwardings == ctx.forwarding_head
            && pcc_gc_forwarding_population == ctx.forwarding_population
            && pcc_gc_backend4_reseed_page_revision == ctx.page_revision
            && pcc_gc_backend4_reseed_relocation_revision
                == ctx.relocation_revision;
        while (valid && cursor != NULL) {
            PccGcObjectNode *node = cursor;
            cursor = cursor->next;
            if (!pcc_gc_object_node_is_active(node)) continue;
            PyObject *obj = node->obj;
            if (
                pcc_capi_is_cext_type_tag(
                    (int64_t)py_header(obj)->type_tag
                ) == 0
            ) continue;
            py_incref(obj);
            ctx.obj = obj;
            pcc_gc_backend4_remap_pending_obj = obj;
            break;
        }
        pcc_gc_graph_unlock();
        if (!valid || ctx.obj == NULL) break;
        valid = pcc_gc_backend4_remap_cext_complete(&ctx);
    }

    pcc_gc_graph_lock();
    int64_t before = pcc_gc_forwarding_population;
    valid = valid
        && cursor == NULL
        && pcc_gc_backend4_remap_active != 0
        && pcc_gc_backend4_remap_epoch == ctx.epoch
        && pcc_gc_backend4_remap_pending_obj == NULL
        && pcc_gc_selected_backend == PCC_GC_KIND_COLORED_RELOCATING
        && pcc_gc_relocation_set == NULL
        && pcc_gc_object_list_revision == ctx.object_revision
        && pcc_gc_forwardings == ctx.forwarding_head
        && pcc_gc_forwarding_population == ctx.forwarding_population
        && pcc_gc_backend4_reseed_page_revision == ctx.page_revision
        && pcc_gc_backend4_reseed_relocation_revision
            == ctx.relocation_revision;
    if (valid) pcc_gc_backend4_remap_and_retire_unlocked(&finish);
    int64_t after = pcc_gc_forwarding_population;
    pcc_gc_backend4_remap_pending_obj = NULL;
    pcc_gc_backend4_remap_active = 0;
    pcc_gc_graph_unlock();

    if (acquired_stopped_world) (void)pcc_resume_world();
    pcc_gc_backend4_finish_remap_retirement(&finish);
    if (!valid) return 0;
    return before > after ? before - after : (before > 0 ? 1 : 0);
}

static void pcc_gc_backend4_remap_and_retire_unlocked(
    PccGcBackend4RemapFinish *finish
) {
    if (finish == NULL) return;
    if (pcc_gc_selected_backend != PCC_GC_KIND_COLORED_RELOCATING) return;
    /* Two-epoch quarantine. Release the prior retained generation first;
     * pages parked by the previous remap only enter retained below and cannot
     * be physically released until the following remap. */
    finish->released_pages =
        pcc_gc_backend4_release_retained_pages_unlocked();
    pcc_gc_backend4_drain_parked_pages_unlocked();
    if (pcc_gc_forwardings == NULL) return;

    /* 1. heal every object's referent slots */
    for (PccGcObjectNode *n = pcc_gc_objects; n != NULL; n = n->next) {
        if (!pcc_gc_object_node_is_active(n)) continue;
        if (
            pcc_capi_is_cext_type_tag(
                (int64_t)py_header(n->obj)->type_tag
            ) != 0
        ) continue;
        pcc_gc_update_referents(n->obj, py_obj_update_slot);
    }
    /* 2. heal frames, continuation roots, scheduler roots (the same
     * sets the gray pass walks; resolve_root_slot rewrites in place) */
    PccGcRewriteMappedRootCtx rewrite_ctx = { 0 };
    for (PccGcFrameNode *f = pcc_gc_frames; f != NULL; f = f->next) {
        (void)pcc_gc_visit_mapped_root_slots_unlocked(
            f->root_count,
            f->slots,
            NULL,
            f->borrowed & PCC_GC_FRAME_NODE_FLAG_BORROWED,
            pcc_gc_rewrite_mapped_root_slot,
            &rewrite_ctx
        );
    }
    for (
        PccGcContinuationRootNode *c = pcc_gc_continuation_roots;
        c != NULL;
        c = c->next
    ) {
        (void)pcc_gc_visit_mapped_root_slots_unlocked(
            c->root_count,
            c->slots,
            NULL,
            c->borrowed,
            pcc_gc_rewrite_mapped_root_slot,
            &rewrite_ctx
        );
    }
    (void)pcc_gc_visit_scheduler_root_slots_unlocked(
        pcc_gc_rewrite_mapped_root_slot,
        &rewrite_ctx
    );
    (void)pcc_gc_visit_builtin_exception_cache_slots_unlocked(
        pcc_gc_rewrite_mapped_root_slot,
        &rewrite_ctx
    );

    /* 3. retire forwarding entries + shell bookkeeping — ONE EPOCH
     * LATE: entries are only retired at the remap AFTER the one that
     * healed the heap (RETIRING marker on the old shell). Stale SSA
     * pointers held across the healing remap can be stored into slots
     * afterwards; they keep resolving until the next remap's heal pass
     * (which runs above, before this retirement) rewrites them.
     * forwarding_remove unlinks from the main list, drops the table's
     * reference on the new copy, and decrements the page's
     * pending_forwardings (parking the page via the zombie hook when
     * it reaches zero). */
    PccGcForwardNode *fn = pcc_gc_forwardings;
    while (fn != NULL) {
        PccGcForwardNode *next = fn->next;
        PyObject *old = fn->from;
        if (old == NULL || PY_IS_TAGGED_INT(old)) {
            /* defensive: never installed in practice; unlink manually */
            pcc_gc_forwarding_target_unlink(fn);
            pcc_gc_forwarding_unlink_main(fn);
            py_decref(fn->to);
            free(fn);
            if (pcc_gc_forwarding_population > 0) pcc_gc_forwarding_population--;
            fn = next;
            continue;
        }
        PyObjectHeader *old_h = py_header(old);
        if ((py_header_flags_load(old_h) & PY_FLAG_GC_FORWARD_RETIRING) == 0) {
            /* first remap after this entry's installation: mark only */
            py_header_flags_or(old_h, PY_FLAG_GC_FORWARD_RETIRING);
            fn = next;
            continue;
        }
        if (
            pcc_gc_relocation_retire_source_payload_into_finish(old, finish)
            == 0
        ) {
            PCC_GC_DEFER_TRIPWIRE(
                0,
                "forwarded-source payload retirement failed before normal teardown"
            );
            return;
        }
        py_header_flags_and(
            old_h,
            ~(PY_FLAG_GC_RELOCATION_CANDIDATE | PY_FLAG_GC_FORWARD_RETIRING)
        );
        pcc_gc_retire_forwarded_source_into_finish_unlocked(old, finish);
        PccGcForwardNode *dead = pcc_gc_forwarding_detach(old);
        if (dead != NULL) {
            dead->next = finish->forwardings;
            finish->forwardings = dead;
        }
        fn = next;
    }
}

static void pcc_gc_seed_roots(void) {
    pcc_gc_gray_count_store(0);
    for (PccGcObjectNode *n = pcc_gc_objects; n != NULL; n = n->next) {
        if (!pcc_gc_object_node_is_active(n)) continue;
        PyObjectHeader *h = py_header(n->obj);
        int32_t flags = py_header_flags_load(h);
        if (
            (flags & PY_FLAG_GC_FRESH_ALLOC) != 0
            && pcc_gc_explicit_collect_active == 0
        ) {
            py_header_flags_update(
                h,
                PY_FLAG_GC_COLOR_MASK | PY_FLAG_GC_FRESH_ALLOC,
                PY_FLAG_GC_BLACK
            );
        } else {
            py_header_flags_update(
                h,
                PY_FLAG_GC_COLOR_MASK | PY_FLAG_GC_FRESH_ALLOC,
                PY_FLAG_GC_WHITE
            );
        }
    }
    pcc_gc_gray_refcount_external_roots();
    pcc_gc_gray_current_roots();
}

static int64_t pcc_gc_drain_all_gray_unlocked(void) {
    int64_t processed = 0;
    for (;;) {
        int64_t pass = 0;
        for (PccGcObjectNode *n = pcc_gc_objects; n != NULL; n = n->next) {
            if (!pcc_gc_object_node_is_active(n)) continue;
            PyObjectHeader *h = py_header(n->obj);
            if ((py_header_flags_load(h) & PY_FLAG_GC_GRAY) == 0) continue;
            pcc_gc_trace_referents(n->obj, pcc_gc_gray_object);
            pcc_gc_gray_count_dec();
            py_header_flags_update(
                h, PY_FLAG_GC_COLOR_MASK, PY_FLAG_GC_BLACK
            );
            processed++;
            pass++;
        }
        if (pass == 0) break;
    }
    return processed;
}

/* Caller owns the graph lock. Process the complete built-in gray closure, but
 * stop after claiming one C-extension object so no external tp_traverse runs
 * in the lock tenure. The retained exact trace token owns the unfinished
 * color/gray-count commit. */
static int64_t pcc_gc_drain_all_gray_locked_slice(void) {
    int64_t processed = 0;
    for (;;) {
        int64_t pass = 0;
        for (PccGcObjectNode *n = pcc_gc_objects; n != NULL; n = n->next) {
            if (!pcc_gc_object_node_is_active(n)) continue;
            PyObjectHeader *h = py_header(n->obj);
            if ((py_header_flags_load(h) & PY_FLAG_GC_GRAY) == 0) continue;
            if (
                pcc_capi_is_cext_type_tag((int64_t)h->type_tag) != 0
                && pcc_gc_trace_cext_claim_unlocked(n->obj, NULL)
            ) {
                return processed + 1;
            }
            pcc_gc_trace_referents(n->obj, pcc_gc_gray_object);
            pcc_gc_gray_count_dec();
            py_header_flags_update(
                h, PY_FLAG_GC_COLOR_MASK, PY_FLAG_GC_BLACK
            );
            processed++;
            pass++;
        }
        if (pass == 0) break;
    }
    return processed;
}

/* Caller owns a stopped world but no graph lock. A callback may acquire the
 * production graph lock or re-enter non-moving runtime operations; each slice
 * therefore revalidates exact tracing identity before touching the gray set. */
static int64_t pcc_gc_drain_all_gray_stopped_world(
    int64_t claim_epoch,
    int64_t claim_backend
) {
    int64_t processed = 0;
    for (;;) {
        PccGcTraceCextCtx cext_ctx = {0};
        pcc_gc_graph_lock();
        int valid = (
            pcc_gc_tracing_cycle_epoch_load() == claim_epoch
            && pcc_gc_selected_backend == claim_backend
            && pcc_gc_mark_active_load() != 0
            && pcc_gc_trace_cext_pending_obj == NULL
        );
        if (valid) {
            processed += pcc_gc_drain_all_gray_locked_slice();
            if (pcc_gc_trace_cext_pending_obj != NULL) {
                cext_ctx.obj = pcc_gc_trace_cext_pending_obj;
                cext_ctx.epoch = pcc_gc_trace_cext_pending_epoch;
                cext_ctx.backend = pcc_gc_trace_cext_pending_backend;
            }
        }
        pcc_gc_graph_unlock();
        if (!valid || cext_ctx.obj == NULL) return processed;
        (void)pcc_gc_trace_cext_complete(&cext_ctx);
    }
}

/* PEP 442 reachability recheck (mirror of the pcc-Python port). A __del__
 * dispatched in PASS 0 may have RESURRECTED an unreachable object (stored it
 * where a root reaches it). Re-mark from roots (seed whitens all but preserves
 * PY_FLAG_GC_SWEEP_CANDIDATE; gray roots; drain) and clear the candidate flag on
 * any object that is now reachable (no longer white), so PASS 1/PASS 2 skip it
 * — otherwise we clear+free a live object (heap corruption / double-free on
 * #1/#2, cleared fields -> AttributeError on #3/#4). Objects still unreachable
 * stay white|candidate and are reclaimed exactly as before. See
 * gc-5backend-finalizer-resurrection-no-libpython.md. */
static void pcc_gc_recheck_reachability_after_finalizers(void) {
    pcc_gc_seed_roots();
    pcc_capi_visit_extension_module_state_roots(
        pcc_gc_gray_runtime_root,
        NULL
    );
    (void)pcc_gc_drain_all_gray_unlocked();
    for (PccGcObjectNode *n = pcc_gc_objects; n != NULL; n = n->next) {
        if (!pcc_gc_object_node_is_active(n)) continue;
        PyObjectHeader *h = py_header(n->obj);
        int32_t flags = py_header_flags_load(h);
        if ((flags & PY_FLAG_GC_SWEEP_CANDIDATE) != 0 &&
            (flags & PY_FLAG_GC_WHITE) == 0) {
            py_header_flags_and(h, ~PY_FLAG_GC_SWEEP_CANDIDATE);
        }
    }
}

static int pcc_gc_begin_mark_cycle_claim_unlocked(void) {
    if (pcc_gc_trace_extension_roots_pending != 0) return 0;
    if (pcc_gc_tracing_cycle_epoch_advance_unlocked() == 0) {
        return 0;
    }
    pcc_gc_trace_extension_roots_epoch =
        pcc_gc_tracing_cycle_epoch_load();
    pcc_gc_trace_extension_roots_backend = pcc_gc_selected_backend;
    pcc_gc_trace_extension_roots_pending = 4;
    return 1;
}

static int pcc_gc_complete_mark_cycle_seed(
    int64_t claim_epoch,
    int64_t claim_backend
) {
    int64_t owns_stopped_world = pcc_thread_owns_stopped_world();
    int acquired_stopped_world = 0;
    if (owns_stopped_world == 0) {
        if (pcc_stop_the_world() != 0) {
            pcc_gc_graph_lock();
            if (
                pcc_gc_trace_extension_roots_pending == 4
                && pcc_gc_trace_extension_roots_epoch == claim_epoch
                && pcc_gc_trace_extension_roots_backend == claim_backend
            ) {
                pcc_gc_trace_extension_roots_pending = 0;
                pcc_gc_trace_extension_roots_epoch = 0;
                pcc_gc_trace_extension_roots_backend = -1;
            }
            pcc_gc_graph_unlock();
            return 0;
        }
        acquired_stopped_world = 1;
    }

    pcc_gc_graph_lock();
    int valid = (
        pcc_gc_trace_extension_roots_pending == 4
        && pcc_gc_trace_extension_roots_epoch == claim_epoch
        && pcc_gc_trace_extension_roots_backend == claim_backend
        && pcc_gc_tracing_cycle_epoch_load() == claim_epoch
        && pcc_gc_selected_backend == claim_backend
        && pcc_gc_mark_active_load() == 0
        && pcc_gc_cycle_requested_load() != 0
    );
    pcc_gc_graph_unlock();

    if (valid) pcc_gc_seed_roots();

    pcc_gc_graph_lock();
    valid = (
        valid
        && pcc_gc_trace_extension_roots_pending == 4
        && pcc_gc_trace_extension_roots_epoch == claim_epoch
        && pcc_gc_trace_extension_roots_backend == claim_backend
        && pcc_gc_tracing_cycle_epoch_load() == claim_epoch
        && pcc_gc_selected_backend == claim_backend
        && pcc_gc_mark_active_load() == 0
        && pcc_gc_cycle_requested_load() != 0
    );
    if (valid) {
        pcc_gc_mark_active_store(1);
        pcc_gc_cycle_requested_store(0);
        pcc_gc_trace_extension_roots_pending = 1;
        pcc_gc_trace_cursor = pcc_gc_objects;
        if (pcc_gc_gray_count_load() == 0) {
            pcc_gc_trace_cursor = NULL;
        }
    } else if (
        pcc_gc_trace_extension_roots_pending == 4
        && pcc_gc_trace_extension_roots_epoch == claim_epoch
        && pcc_gc_trace_extension_roots_backend == claim_backend
    ) {
        pcc_gc_trace_extension_roots_pending = 0;
        pcc_gc_trace_extension_roots_epoch = 0;
        pcc_gc_trace_extension_roots_backend = -1;
    }
    pcc_gc_graph_unlock();
    if (acquired_stopped_world) (void)pcc_resume_world();
    return valid;
}

/* Pure final-cut owner: the caller already owns a stopped world and holds the
 * object-graph lock.  Captured claim identity prevents reset/backend ABA. */
static int pcc_gc_finish_tracing_cycle(
    int64_t claim_epoch,
    int64_t claim_backend
) {
    if (
        pcc_gc_tracing_finish_claim_epoch_load() != claim_epoch
        || pcc_gc_tracing_finish_claim_backend_load() != claim_backend
    ) {
        return 0;
    }
    if (
        pcc_gc_tracing_cycle_epoch_load() != claim_epoch
        || pcc_gc_selected_backend != claim_backend
        || pcc_gc_mark_active_load() == 0
    ) {
        pcc_gc_tracing_finish_claim_clear_unlocked(
            claim_epoch, claim_backend
        );
        return 0;
    }
    int64_t commits = __atomic_load_n(
        &pcc_gc_tracing_finish_commits, __ATOMIC_ACQUIRE
    );
    if (commits < 0 || commits == INT64_MAX) {
        abort();
        return 0;
    }
    /* The stopped-world owner has already rescanned current roots and drained
     * their complete gray closure with graph-lock-free C-extension callbacks.
     * This pure commit publishes the atomic white->sweep-candidate cut for the
     * tracing skeletons (#1/#2).  Default builds take the no-op path. */
    for (PccGcObjectNode *n = pcc_gc_objects; n != NULL; n = n->next) {
        if (!pcc_gc_object_node_is_active(n)) continue;
        PyObjectHeader *h = py_header(n->obj);
        if ((py_header_flags_load(h) & PY_FLAG_GC_WHITE) != 0) {
            py_header_flags_or(h, PY_FLAG_GC_SWEEP_CANDIDATE);
        } else {
            py_header_flags_and(h, ~PY_FLAG_GC_SWEEP_CANDIDATE);
        }
    }
    pcc_gc_trace_cursor = NULL;
    pcc_gc_gray_count_store(0);
    pcc_gc_mark_active_store(0);
    pcc_gc_trace_extension_roots_pending = 0;
    pcc_gc_trace_extension_roots_epoch = 0;
    pcc_gc_trace_extension_roots_backend = -1;
    /* Do not clear cycle_requested: root/barrier/reset work published while
     * this claimant waited for STW belongs to the next tracing epoch. */
    __atomic_store_n(
        &pcc_gc_tracing_finish_commits, commits + 1, __ATOMIC_RELEASE
    );
    pcc_gc_tracing_finish_claim_clear_unlocked(claim_epoch, claim_backend);
    return 1;
}

static int pcc_gc_complete_claimed_tracing_cycle(
    int64_t claim_epoch,
    int64_t claim_backend
) {
    int64_t owns_stopped_world = pcc_thread_owns_stopped_world();
    int acquired_stopped_world = 0;
    if (owns_stopped_world == 0) {
        if (pcc_stop_the_world() != 0) {
            /* Stop failed before a final cut.  Re-enter only to release this
             * exact token; a reset/successor claimant must remain untouched. */
            pcc_gc_graph_lock();
            pcc_gc_tracing_finish_claim_clear_unlocked(
                claim_epoch, claim_backend
            );
            pcc_gc_graph_unlock();
            return 0;
        }
        acquired_stopped_world = 1;
    }

    PccGcTraceExtensionRootCtx extension_ctx = {
        claim_epoch, claim_backend
    };
    int visit_extension_roots = 0;
    pcc_gc_graph_lock();
    if (
        pcc_gc_tracing_finish_claim_epoch_load() == claim_epoch
        && pcc_gc_tracing_finish_claim_backend_load() == claim_backend
        && pcc_gc_tracing_cycle_epoch_load() == claim_epoch
        && pcc_gc_selected_backend == claim_backend
        && pcc_gc_mark_active_load() != 0
        && pcc_gc_trace_extension_roots_pending == 0
    ) {
        pcc_gc_trace_extension_roots_pending = 3;
        pcc_gc_trace_extension_roots_epoch = claim_epoch;
        pcc_gc_trace_extension_roots_backend = claim_backend;
        visit_extension_roots = 1;
    } else {
        if (
            pcc_gc_trace_extension_roots_pending == 3
            && pcc_gc_trace_extension_roots_epoch == claim_epoch
            && pcc_gc_trace_extension_roots_backend == claim_backend
        ) {
            pcc_gc_trace_extension_roots_pending = 0;
            pcc_gc_trace_extension_roots_epoch = 0;
            pcc_gc_trace_extension_roots_backend = -1;
        }
        pcc_gc_tracing_finish_claim_clear_unlocked(
            claim_epoch, claim_backend
        );
    }
    pcc_gc_graph_unlock();

    if (visit_extension_roots) {
        pcc_capi_visit_extension_module_state_roots(
            pcc_gc_trace_final_extension_state_root,
            &extension_ctx
        );
    }

    pcc_gc_graph_lock();
    int ready_to_drain = (
        visit_extension_roots
        && pcc_gc_trace_extension_roots_pending == 3
        && pcc_gc_trace_extension_roots_epoch == claim_epoch
        && pcc_gc_trace_extension_roots_backend == claim_backend
        && pcc_gc_tracing_finish_claim_epoch_load() == claim_epoch
        && pcc_gc_tracing_finish_claim_backend_load() == claim_backend
        && pcc_gc_tracing_cycle_epoch_load() == claim_epoch
        && pcc_gc_selected_backend == claim_backend
        && pcc_gc_mark_active_load() != 0
    );
    if (ready_to_drain) {
        pcc_gc_trace_extension_roots_pending = 0;
        /* Roots can change while #1/#2 tracing runs incrementally or
         * concurrently. Rescan under the stopped-world cut, then release only
         * the graph lock for callback-capable whole-gray slices. */
        pcc_gc_gray_current_roots();
    } else {
        if (
            pcc_gc_trace_extension_roots_pending == 3
            && pcc_gc_trace_extension_roots_epoch == claim_epoch
            && pcc_gc_trace_extension_roots_backend == claim_backend
        ) {
            pcc_gc_trace_extension_roots_pending = 0;
            pcc_gc_trace_extension_roots_epoch = 0;
            pcc_gc_trace_extension_roots_backend = -1;
        }
        pcc_gc_tracing_finish_claim_clear_unlocked(
            claim_epoch, claim_backend
        );
    }
    pcc_gc_graph_unlock();

    if (ready_to_drain) {
        (void)pcc_gc_drain_all_gray_stopped_world(
            claim_epoch,
            claim_backend
        );
    }

    pcc_gc_graph_lock();
    int final_token_valid = (
        ready_to_drain
        && pcc_gc_trace_cext_pending_obj == NULL
        && pcc_gc_tracing_finish_claim_epoch_load() == claim_epoch
        && pcc_gc_tracing_finish_claim_backend_load() == claim_backend
        && pcc_gc_tracing_cycle_epoch_load() == claim_epoch
        && pcc_gc_selected_backend == claim_backend
        && pcc_gc_mark_active_load() != 0
    );
    int committed = 0;
    if (final_token_valid) {
        committed = pcc_gc_finish_tracing_cycle(
            claim_epoch, claim_backend
        );
    } else if (ready_to_drain) {
        if (
            pcc_gc_trace_extension_roots_epoch == claim_epoch
            && pcc_gc_trace_extension_roots_backend == claim_backend
        ) {
            pcc_gc_trace_extension_roots_pending = 0;
            pcc_gc_trace_extension_roots_epoch = 0;
            pcc_gc_trace_extension_roots_backend = -1;
        }
        pcc_gc_tracing_finish_claim_clear_unlocked(
            claim_epoch, claim_backend
        );
    }
    pcc_gc_graph_unlock();
    if (acquired_stopped_world) {
        (void)pcc_resume_world();
    }
    return committed;
}

static int64_t pcc_gc_step_trace_cycle_unlocked(
    int64_t budget,
    int finish_cycle,
    int64_t *claim_epoch,
    int64_t *claim_backend
) {
    *claim_epoch = 0;
    *claim_backend = -1;
    if (budget <= 0) {
        return 0;
    }
    if (pcc_gc_trace_cext_pending_obj != NULL) return 0;

    int64_t processed = 0;

    if (pcc_gc_mark_active_load() == 0) {
        if (pcc_gc_cycle_requested_load() == 0) {
            return processed;
        }
        if (pcc_gc_tracing_finish_claim_epoch_load() != 0) {
            return processed;
        }
        if (pcc_threads_enabled() && pcc_gc_in_auto_step) {
            return processed;
        }
        (void)pcc_gc_begin_mark_cycle_claim_unlocked();
        return processed;
    }

    if (pcc_gc_trace_extension_roots_pending != 0) return processed;

    if (pcc_gc_trace_cursor == NULL) {
        pcc_gc_trace_cursor = pcc_gc_objects;
    }

    PccGcObjectNode *n = pcc_gc_trace_cursor;
    while (n != NULL && processed < budget) {
        PccGcObjectNode *next = n->next;
        if (pcc_gc_object_node_is_active(n)) {
            PyObjectHeader *h = py_header(n->obj);
            if ((py_header_flags_load(h) & PY_FLAG_GC_GRAY) != 0) {
                PccGcTraceCextCtx cext_ctx = {0};
                if (pcc_gc_trace_cext_claim_unlocked(n->obj, &cext_ctx)) {
                    processed++;
                    n = next;
                    break;
                } else {
                    pcc_gc_trace_referents(n->obj, pcc_gc_gray_object);
                    pcc_gc_gray_count_dec();
                    py_header_flags_update(
                        h, PY_FLAG_GC_COLOR_MASK, PY_FLAG_GC_BLACK
                    );
                    processed++;
                }
            }
        }
        n = next;
    }
    pcc_gc_trace_cursor = n;

    if (finish_cycle && pcc_gc_trace_cursor == NULL) {
        if (pcc_gc_gray_count_load() != 0) {
            pcc_gc_trace_cursor = pcc_gc_objects;
        } else if (pcc_gc_tracing_finish_claim_epoch_load() == 0) {
            int64_t cycle_epoch = pcc_gc_tracing_cycle_epoch_load();
            if (cycle_epoch > 0) {
                int64_t cycle_backend = pcc_gc_selected_backend;
                pcc_gc_tracing_finish_claim_store(
                    cycle_epoch, cycle_backend
                );
                *claim_epoch = cycle_epoch;
                *claim_backend = cycle_backend;
            }
        }
    }

    return processed;
}

static int64_t pcc_gc_cms_worker_trace_cycle_unlocked(
    int64_t budget,
    int64_t *claim_epoch,
    int64_t *claim_backend
) {
    return pcc_gc_step_trace_cycle_unlocked(
        budget, 1, claim_epoch, claim_backend
    );
}

static int64_t pcc_gc_step_trace_cycle(int64_t budget) {
    if (budget <= 0) return 0;
    int64_t claim_epoch = 0;
    int64_t claim_backend = -1;
    PccGcTraceExtensionRootCtx extension_ctx = {0, -1};
    PccGcTraceExtensionRootCtx seed_ctx = {0, -1};
    PccGcTraceCextCtx cext_ctx = {0};
    int visit_extension_roots = 0;
    pcc_gc_graph_lock();
    int64_t processed = pcc_gc_step_trace_cycle_unlocked(
        budget, 1, &claim_epoch, &claim_backend
    );
    if (pcc_gc_trace_extension_roots_pending == 4) {
        seed_ctx.epoch = pcc_gc_trace_extension_roots_epoch;
        seed_ctx.backend = pcc_gc_trace_extension_roots_backend;
    } else if (pcc_gc_trace_cext_pending_obj != NULL) {
        cext_ctx.obj = pcc_gc_trace_cext_pending_obj;
        cext_ctx.epoch = pcc_gc_trace_cext_pending_epoch;
        cext_ctx.backend = pcc_gc_trace_cext_pending_backend;
    } else {
        visit_extension_roots =
            pcc_gc_trace_extension_roots_claim_unlocked(&extension_ctx);
    }
    pcc_gc_graph_unlock();
    if (seed_ctx.epoch != 0) {
        if (pcc_gc_complete_mark_cycle_seed(
                seed_ctx.epoch, seed_ctx.backend
            )) {
            return processed + pcc_gc_step_trace_cycle(budget - processed);
        }
        return processed;
    }
    if (cext_ctx.obj != NULL) {
        (void)pcc_gc_trace_cext_complete(&cext_ctx);
        return processed;
    }
    if (
        visit_extension_roots
        && pcc_gc_trace_extension_roots_complete(&extension_ctx)
    ) {
        pcc_gc_graph_lock();
        processed += pcc_gc_step_trace_cycle_unlocked(
            budget - processed, 1, &claim_epoch, &claim_backend
        );
        pcc_gc_graph_unlock();
    }
    if (claim_epoch != 0) {
        (void)pcc_gc_complete_claimed_tracing_cycle(
            claim_epoch, claim_backend
        );
    }
    return processed;
}

static int64_t pcc_gc_step_generational_promotion(
    int64_t budget,
    int promote_all_young
) {
    if (budget <= 0) return 0;
    int64_t batch_budget = budget;
    if (batch_budget > PCC_GC_SAFEPOINT_BATCH) {
        batch_budget = PCC_GC_SAFEPOINT_BATCH;
    }
    int64_t processed = 0;
    PccGcRememberedOwnerNode *detached_remembered = NULL;
    PyObject *tls_cleanup = NULL;
    pcc_gc_generational_promote_frame_roots(batch_budget);
    pcc_gc_generational_promote_scheduler_roots(batch_budget);
    pcc_gc_graph_lock();
    pcc_gc_promote_tls_exception_root(&tls_cleanup);
    processed += pcc_gc_backend3_drain_remembered_owners(
        batch_budget - processed, &detached_remembered
    );
    if (promote_all_young) {
        while (
            pcc_gc_backend3_young_head != NULL
            && processed < batch_budget
        ) {
            PccGcObjectNode *n = pcc_gc_backend3_young_head;
            pcc_gc_backend3_young_unlink(n);
            if (!pcc_gc_object_node_is_active(n)) continue;
            PyObjectHeader *h = py_header(n->obj);
            int32_t flags = py_header_flags_load(h);
            if ((flags & PY_FLAG_GC_YOUNG) == 0) continue;
            if (pcc_gc_forwarding_find(n->obj) != NULL) continue;
            pcc_gc_promote_young_object(n->obj);
            int32_t after_flags = py_header_flags_load(h);
            if (
                (after_flags & PY_FLAG_GC_YOUNG) == 0
                || pcc_gc_forwarding_find(n->obj) != NULL
            ) {
                processed++;
            } else {
                /* A transient promotion failure must remain schedulable. */
                pcc_gc_backend3_young_link_head(n);
                break;
            }
        }
    }
    pcc_gc_graph_unlock();
    pcc_gc_backend3_finish_detached_remembered_owners(detached_remembered);
    if (tls_cleanup != NULL) py_decref(tls_cleanup);
    if (processed < budget) {
        processed += pcc_gc_backend3_drain_promotion_worklist(
            budget - processed
        );
    }
    pcc_capi_visit_extension_module_state_roots(
        pcc_gc_promote_extension_module_state_root,
        NULL
    );

    if (processed > 0) {
        pcc_thread_safepoint();
    }
    return processed;
}

static int64_t pcc_gc_step_colored_remembered_roots(int64_t budget) {
    if (budget <= 0) return 0;
    int64_t batch_limit = budget;
    if (batch_limit > PCC_GC_BACKEND4_STORE_BUFFER_BATCH_CAPACITY) {
        batch_limit = PCC_GC_BACKEND4_STORE_BUFFER_BATCH_CAPACITY;
    }

    /* Materialize at most one medium-buffer capacity outside the graph lock.
     * The locked transfer consumes only already-allocated nodes and retains
     * any suffix when allocation cannot cover the observed snapshot. */
    int32_t medium_needed = 0;
    pcc_gc_graph_lock();
    for (
        PccGcStoreBufferMediumState *state =
            pcc_gc_backend4_store_buffer_medium_states;
        state != NULL
            && medium_needed < PCC_GC_BACKEND4_STORE_BUFFER_MEDIUM_CAPACITY;
        state = state->next
    ) {
        int32_t count = state->count == NULL ? 0 : *state->count;
        int32_t room =
            PCC_GC_BACKEND4_STORE_BUFFER_MEDIUM_CAPACITY - medium_needed;
        medium_needed += count < room ? count : room;
    }
    pcc_gc_graph_unlock();

    PccGcStoreBufferNode *preallocated = NULL;
    int32_t preallocated_count = 0;
    for (int32_t i = 0; i < medium_needed; i++) {
        PccGcStoreBufferNode *n = (
            PccGcStoreBufferNode *
        )calloc(1, sizeof(PccGcStoreBufferNode));
        if (n == NULL) break;
        n->next = preallocated;
        preallocated = n;
        preallocated_count++;
    }

    PccGcStoreBufferEntry batch[
        PCC_GC_BACKEND4_STORE_BUFFER_BATCH_CAPACITY
    ];
    PccGcStoreBufferNode *batch_nodes[
        PCC_GC_BACKEND4_STORE_BUFFER_BATCH_CAPACITY
    ] = {NULL};
    int64_t drained = 0;

    pcc_gc_graph_lock();
    for (
        PccGcStoreBufferMediumState *state =
            pcc_gc_backend4_store_buffer_medium_states;
        state != NULL && preallocated_count > 0;
        state = state->next
    ) {
        if (state->count == NULL || state->entries == NULL) continue;
        int32_t before = *state->count;
        if (before <= 0) continue;
        int32_t move = before;
        if (move > preallocated_count) move = preallocated_count;
        int32_t first = before - move;
        for (int32_t i = first; i < before; i++) {
            PccGcStoreBufferEntry *entry = &state->entries[i];
            PccGcStoreBufferNode *n = preallocated;
            preallocated = n->next;
            preallocated_count--;
            n->owner = entry->owner;
            n->slot = entry->slot;
            n->value = entry->value;
            n->next = pcc_gc_backend4_store_buffer;
            pcc_gc_backend4_store_buffer = n;
            entry->owner = NULL;
            entry->slot = NULL;
            entry->value = NULL;
        }
        *state->count = first;
        __atomic_add_fetch(
            &pcc_gc_backend4_store_buffer_medium_flushes_count,
            1,
            __ATOMIC_RELAXED
        );
        __atomic_add_fetch(
            &pcc_gc_backend4_store_buffer_medium_flushed_entries_count,
            move,
            __ATOMIC_RELAXED
        );
        if (
            before >= PCC_GC_BACKEND4_STORE_BUFFER_MEDIUM_CAPACITY
            && move == before
        ) {
            __atomic_add_fetch(
                &pcc_gc_backend4_store_buffer_medium_full_flushes_count,
                1,
                __ATOMIC_RELAXED
            );
        }
        if (state != pcc_gc_backend4_store_buffer_medium_state) {
            __atomic_add_fetch(
                &pcc_gc_backend4_store_buffer_cross_thread_medium_flushes_count,
                1,
                __ATOMIC_RELAXED
            );
            __atomic_add_fetch(
                &pcc_gc_backend4_store_buffer_cross_thread_medium_flushed_entries_count,
                move,
                __ATOMIC_RELAXED
            );
        }
    }

    while (pcc_gc_backend4_store_buffer != NULL && drained < batch_limit) {
        PccGcStoreBufferNode *n = pcc_gc_backend4_store_buffer;
        pcc_gc_backend4_store_buffer = n->next;
        PccGcStoreBufferEntry *entry = &batch[drained];
        entry->owner = n->owner;
        entry->slot = n->slot;
        entry->value = n->value;
        batch_nodes[drained] = n;
        pcc_gc_backend4_store_buffer_dec_unlocked();
        drained++;
        PyObject *owner = entry->owner;
        if (!pcc_gc_is_known_object(owner)) {
            continue;
        }
        PyObjectHeader *h = py_header(owner);
        int32_t flags = py_header_flags_load(h);
        if ((flags & PY_FLAG_GC_REMEMBERED) == 0) {
            continue;
        }
        PyObject *value = entry->value;
        pcc_gc_promote_young_object(value);
        if (entry->slot != NULL) {
            pcc_gc_promote_young_slot(entry->slot);
        } else {
            pcc_gc_promote_remembered_owner_referents(owner);
        }
        if (!pcc_gc_backend4_store_buffer_owner_pending(owner)) {
            py_header_flags_and(h, ~PY_FLAG_GC_REMEMBERED);
        }
    }
    if (drained > 0) {
        __atomic_add_fetch(
            &pcc_gc_backend4_store_buffer_drain_batches_count,
            1,
            __ATOMIC_RELAXED
        );
        __atomic_add_fetch(
            &pcc_gc_backend4_store_buffer_drained_entries_count,
            drained,
            __ATOMIC_RELAXED
        );
        if (drained >= PCC_GC_BACKEND4_STORE_BUFFER_BATCH_CAPACITY) {
            __atomic_add_fetch(
                &pcc_gc_backend4_store_buffer_full_batches_count,
                1,
                __ATOMIC_RELAXED
            );
        }
        if (
            __atomic_load_n(
                &pcc_gc_backend4_store_buffer_entries_count,
                __ATOMIC_RELAXED
            ) > 0
        ) {
            __atomic_add_fetch(
                &pcc_gc_backend4_store_buffer_incomplete_drains_count,
                1,
                __ATOMIC_RELAXED
            );
        }
    }
    pcc_gc_graph_unlock();

    if (drained > 0) {
        pcc_gc_backend4_store_buffer_note_max_batch(drained);
    }

    while (preallocated != NULL) {
        PccGcStoreBufferNode *next = preallocated->next;
        free(preallocated);
        preallocated = next;
    }
    for (int64_t i = 0; i < drained; i++) {
        free(batch_nodes[i]);
        py_decref(batch[i].value);
    }
    int64_t promotion_examined = 0;
    if (drained < budget) {
        promotion_examined = pcc_gc_backend3_drain_promotion_worklist(
            budget - drained
        );
    }
    if (drained > 0) pcc_thread_safepoint();
    return drained + promotion_examined;
}

int64_t pcc_gc_backend4_step_remembered_roots(int64_t budget) {
    return pcc_gc_step_colored_remembered_roots(budget);
}

static int64_t pcc_gc_step_colored_generation_aging(int64_t budget) {
    if (budget <= 0) return 0;
    int64_t examined = 0;
    while (examined < budget) {
        int64_t batch_limit = budget - examined;
        if (batch_limit > PCC_GC_SAFEPOINT_BATCH) {
            batch_limit = PCC_GC_SAFEPOINT_BATCH;
        }
        int64_t batch_examined = 0;
        int tripwire_invalid_generation = 0;
        int more_work = 0;

        pcc_gc_graph_lock();
        while (
            pcc_gc_backend3_young_head != NULL
            && batch_examined < batch_limit
        ) {
            PccGcObjectNode *n = pcc_gc_backend3_young_head;
            pcc_gc_backend3_young_unlink(n);
            batch_examined++;
            examined++;
            if (!pcc_gc_object_node_is_active(n)) continue;
            PyObject *o = n->obj;
            PyObjectHeader *h = py_header(o);
            int32_t flags = py_header_flags_load(h);
            if ((flags & PY_FLAG_GC_YOUNG) == 0) continue;
            if ((flags & PY_FLAG_GC_OLD) != 0) {
                tripwire_invalid_generation = 1;
                break;
            }
            /* YOUNG and OLD are adjacent single bits.  With YOUNG set and
             * OLD clear, adding YOUNG atomically carries into OLD while
             * preserving every unrelated concurrently-published flag. */
            __atomic_add_fetch(
                &h->flags, PY_FLAG_GC_YOUNG, __ATOMIC_ACQ_REL
            );
            PccGcZPageNode *zpage_node = n->zpage_node;
            if (zpage_node != NULL && zpage_node->page != NULL) {
                zpage_node->page->generation = 2;
            }
            __atomic_add_fetch(
                &pcc_gc_backend4_young_promotions, 1, __ATOMIC_RELAXED
            );
        }
        more_work = pcc_gc_backend3_young_head != NULL;
        pcc_gc_graph_unlock();

        PCC_RT_TRIPWIRE(
            tripwire_invalid_generation == 0,
            "pcc_gc_backend4 young-promotion drain: promoting a YOUNG object already marked OLD (young->old generation invariant violated)"
        );
        if (batch_examined > 0) pcc_thread_safepoint();
        if (
            tripwire_invalid_generation != 0
            || batch_examined == 0
            || !more_work
        ) {
            break;
        }
    }
    return examined;
}

/* Public mirror of the pcc-Python port's @c_abi_export of the same name in
 * freestanding_gc_generational_scheduler.py.  The C runtime reached this phase
 * only through pcc_gc_step, whose backend-4 path runs the colored
 * remembered-roots phase first and legitimately parks at that phase's own
 * safepoint -- so a caller wanting to exercise generation aging on its own had
 * no equivalent entry point in C, and the two mirrors could not be compared.
 * That asymmetry is what made the C arm of
 * test_colored_generation_aging_polls_only_after_releasing_graph_lock measure
 * a different phase than its port arm. */
int64_t pcc_gc_backend4_step_generation_aging(int64_t budget) {
    return pcc_gc_step_colored_generation_aging(budget);
}

int64_t pcc_gc_step(int64_t budget) {
    pcc_gc_init_config();
    if (budget <= 0) return 0;
    if (
        pcc_gc_selected_backend == PCC_GC_KIND_COLORED_RELOCATING
        && pcc_gc_backend4_remap_active != 0
    ) return 0;
    pcc_gc_metric_add(PCC_GC_COUNTER_WORK_STEPS, 1);
    int64_t start_us = pcc_gc_now_us();
    int64_t processed = 0;
    if (pcc_gc_selected_backend == PCC_GC_KIND_CONCURRENT_MARK_SWEEP) {
        pcc_gc_cms_flush_wb_buffer();
    }
    if (
        pcc_gc_selected_backend == PCC_GC_KIND_INCREMENTAL_TRICOLOR
        || pcc_gc_selected_backend == PCC_GC_KIND_CONCURRENT_MARK_SWEEP
    ) {
        processed += pcc_gc_step_trace_cycle(budget);
    } else if (
        pcc_gc_selected_backend == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
    ) {
        processed += pcc_gc_step_generational_promotion(budget, 1);
        if (
            pcc_gc_explicit_collect_active
            && processed < budget
            && (
                pcc_gc_cycle_requested_load() != 0
                || pcc_gc_mark_active_load() != 0
                || pcc_gc_has_sweep_candidate() != 0
            )
        ) {
            processed += pcc_gc_step_trace_cycle(budget - processed);
        }
    } else if (
        pcc_gc_selected_backend == PCC_GC_KIND_COLORED_RELOCATING
    ) {
        if (pcc_gc_explicit_collect_active) {
            processed += pcc_gc_backend3_drain_promotion_worklist(
                budget - processed
            );
            if (processed < budget) {
                processed += pcc_gc_step_trace_cycle(budget - processed);
            }
        } else {
            processed += pcc_gc_step_colored_remembered_roots(
                budget - processed
            );
            if (__atomic_load_n(
                    &pcc_gc_backend4_store_buffer_entries_count,
                    __ATOMIC_ACQUIRE
                ) == 0) {
                if (processed < budget) {
                    processed += pcc_gc_step_colored_generation_aging(
                        budget - processed
                    );
                }
                if (processed < budget) {
                    processed += pcc_gc_backend4_evacuation_page_drain(
                        budget - processed
                    );
                }
                if (processed < budget) {
                    int64_t selected = pcc_gc_backend4_select_relocation_pages(
                        budget - processed
                    );
                    if (selected > 0) {
                        int64_t moved = pcc_gc_backend4_evacuation_page_drain(
                            budget - processed
                        );
                        processed += moved > 0 ? moved : selected;
                    }
                }

                if (
                    processed < budget
                    && (
                        pcc_gc_cycle_requested_load() != 0
                        || pcc_gc_mark_active_load() != 0
                        || pcc_gc_has_sweep_candidate() != 0
                    )
                ) {
                    /* Colored relocation changes the interpretation of
                     * read-barrier state. Keep the phase transition STW even
                     * though this backend still uses a side-table candidate
                     * flag instead of multi-mapping. */
                    int64_t stw = pcc_stop_the_world();
                    processed += pcc_gc_step_trace_cycle(budget - processed);
                    if (stw == 0) (void)pcc_resume_world();
                }
                if (
                    processed == 0
                    && pcc_gc_forwarding_population_load() > 0
                ) {
                    processed +=
                        pcc_gc_backend4_remap_and_retire_stopped_world();
                }
            }
        }
    }
    pcc_gc_record_pause(start_us, pcc_gc_now_us());
    if (
        pcc_gc_selected_backend == PCC_GC_KIND_INCREMENTAL_TRICOLOR
        || pcc_gc_selected_backend == PCC_GC_KIND_CONCURRENT_MARK_SWEEP
    ) {
        pcc_gc_discharge_debt(processed);
        if (
            pcc_gc_mark_active_load() == 0
            && pcc_gc_cycle_requested_load() == 0
        ) {
            __atomic_store_n(&pcc_gc_debt_bytes, 0, __ATOMIC_RELEASE);
        }
    }
    return processed;
}

/* "A mark cycle completed and its sweep has not run yet."
 *
 * pcc_gc_finish_tracing_cycle is the only writer of PY_FLAG_GC_SWEEP_CANDIDATE
 * and it publishes the white->candidate cut atomically with clearing
 * mark_active, so the flag can only be set by a finished cycle.  Consulting
 * mark_active alongside it is therefore exactly a mark-complete test.
 *
 * pcc_gc_has_tracing_sweep() deliberately does NOT do this -- it answers "are
 * there candidates", which callers use for reporting.  Gating a sweep on that
 * alone is unsound: candidates left over from a previous cycle whose sweep did
 * not finish read true while a NEW mark is in flight, and sweeping there frees
 * live objects.  That was measured. */
int64_t pcc_gc_sweep_owed(void) {
    pcc_gc_init_config();
    if (pcc_gc_selected_backend == PCC_GC_KIND_REFCOUNT_CYCLE) return 0;
    if (pcc_gc_mark_active_load() != 0) return 0;
    return pcc_gc_has_sweep_candidate() != 0 ? 1 : 0;
}

int64_t pcc_gc_has_tracing_sweep(void) {
    pcc_gc_init_config();
    if (
        pcc_gc_selected_backend != PCC_GC_KIND_INCREMENTAL_TRICOLOR
        && pcc_gc_selected_backend != PCC_GC_KIND_CONCURRENT_MARK_SWEEP
        && pcc_gc_selected_backend != PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
        && pcc_gc_selected_backend != PCC_GC_KIND_COLORED_RELOCATING
    ) {
        return 0;
    }
    return pcc_gc_has_sweep_candidate() != 0 ? 1 : 0;
}

int64_t pcc_gc_collect_tracing(void) {
    pcc_gc_init_config();
    if (
        pcc_gc_selected_backend != PCC_GC_KIND_INCREMENTAL_TRICOLOR
        && pcc_gc_selected_backend != PCC_GC_KIND_CONCURRENT_MARK_SWEEP
        && pcc_gc_selected_backend != PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
        && pcc_gc_selected_backend != PCC_GC_KIND_COLORED_RELOCATING
    ) {
        return 0;
    }

    if (pcc_gc_has_sweep_candidate() == 0) return 0;
    int64_t stw = pcc_stop_the_world();
    if (stw != 0) return 0;
    /* This entrypoint implements the explicit full-heap gc.collect()
     * boundary.  A scheduler step is deliberately bounded, but truncating
     * this sweep to 1024 candidates leaves the rest live and under-reports
     * the result.  Sweep the complete tracked graph in the existing single
     * STW PASS-0/PASS-1/PASS-2 transaction. */
    int64_t reclaimed = pcc_gc_sweep_unreachable(INT64_MAX);
    if (stw == 0) (void)pcc_resume_world();
    return reclaimed;
}

void pcc_gc_begin_explicit_tracing_collect(void) {
    pcc_gc_init_config();
    pcc_gc_explicit_collect_active = 1;
    if (pcc_gc_selected_backend != PCC_GC_KIND_REFCOUNT_CYCLE) {
        pcc_gc_cycle_requested_store(1);
    }
}

void pcc_gc_end_explicit_tracing_collect(void) {
    pcc_gc_explicit_collect_active = 0;
}

/* Reentrancy probe for pcc_gc_collect: pcc_gc_explicit_collect_active is set
 * across the whole begin..end (mark+sweep) window of an explicit tracing
 * collect, so a non-zero value means a collect is already in progress. The flag
 * is file-static here, so py_obj.c (the pcc_gc_collect reentrancy guard, cc
 * mode) reads it through this getter. The pcc-Python port reads the shared
 * global directly via global_addr. See
 * gc-5backend-reentrant-collect-during-finalizer-no-libpython.md. */
int32_t pcc_gc_explicit_collect_is_active(void) {
    return pcc_gc_explicit_collect_active;
}

static void pcc_gc_maybe_auto_step(void) {
    if (pcc_gc_in_auto_step) return;
    if (pcc_gc_selected_backend != PCC_GC_KIND_INCREMENTAL_TRICOLOR) return;
    if (pcc_threads_enabled()) return;
    if (
        __atomic_load_n(&pcc_gc_debt_bytes, __ATOMIC_RELAXED)
        < pcc_gc_debt_threshold()
    ) return;
    pcc_gc_in_auto_step = 1;
    (void)pcc_gc_step(pcc_gc_budget_from_debt());
    pcc_gc_in_auto_step = 0;
}

static void pcc_gc_cms_maybe_assist(void) {
    if (pcc_gc_in_auto_step) return;
    if (pcc_gc_selected_backend != PCC_GC_KIND_CONCURRENT_MARK_SWEEP) return;
    if (
        __atomic_load_n(&pcc_gc_debt_bytes, __ATOMIC_RELAXED)
        < pcc_gc_debt_threshold()
    ) return;
    __atomic_add_fetch(
        &pcc_gc_cms_mutator_assists, 1, __ATOMIC_RELAXED
    );
    pcc_gc_in_auto_step = 1;
    (void)pcc_gc_step(pcc_gc_budget_from_debt());
    pcc_gc_in_auto_step = 0;
}

void pcc_gc_note_alloc(int64_t bytes) {
    pcc_gc_init_config();
    if (bytes < 0) bytes = 0;
    pcc_gc_metric_add(PCC_GC_COUNTER_ALLOCATIONS, 1);
    if (pcc_gc_selected_backend == PCC_GC_KIND_INCREMENTAL_TRICOLOR) {
        __atomic_add_fetch(&pcc_gc_debt_bytes, bytes, __ATOMIC_ACQ_REL);
        pcc_gc_maybe_auto_step();
    } else if (
        pcc_gc_selected_backend == PCC_GC_KIND_CONCURRENT_MARK_SWEEP
    ) {
        __atomic_add_fetch(&pcc_gc_debt_bytes, bytes, __ATOMIC_ACQ_REL);
        if (pcc_threads_enabled()) {
            pcc_gc_cms_maybe_start_worker();
        }
        pcc_gc_cms_queue_push(bytes);
        pcc_gc_cms_maybe_assist();
    }
}

void pcc_gc_note_object_allocated_sized(PyObject *o, int64_t size) {
    pcc_gc_init_config();
    if (o == NULL || PY_IS_TAGGED_INT(o)) return;
    if (size < (int64_t)sizeof(PyObjectHeader)) {
        size = (int64_t)sizeof(PyObjectHeader);
    }
    if (!pcc_gc_tracks_objects()) return;
    PccGcObjectNode *prepared_node = NULL;
    void *prepared_slots = NULL;
    int64_t prepared_cap = 0;
    PccGcZPageNode *prepared_zpage_node = NULL;
    void *prepared_zpage_slots = NULL;
    int64_t prepared_zpage_cap = 0;
    PccGcZPage *prepared_zpage = NULL;
    int32_t prepared_zpage_from_free = 0;
    int32_t prepared_zpage_ready = 0;
    for (;;) {
        pcc_gc_graph_lock();
        int32_t backend = pcc_gc_selected_backend;
        int32_t graph_leaf = (
            (
                backend == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
                || backend == PCC_GC_KIND_COLORED_RELOCATING
            )
            && pcc_gc_pending_minor_block == NULL
            && pcc_gc_backend3_graph_leaf_tag(py_header(o)->type_tag)
        );
        if (!graph_leaf) {
            int64_t required = pcc_gc_object_index_plan_capacity(1);
            int64_t need_node = pcc_gc_object_node_plan_requires_prepare();
            int64_t zpage_required = 0;
            int64_t need_zpage_node = 0;
            int32_t need_zpage_page = 0;
            if (backend == PCC_GC_KIND_COLORED_RELOCATING) {
                zpage_required = pcc_gc_zpage_owner_index_plan_capacity(1);
                need_zpage_node =
                    pcc_gc_backend4_zpage_node_plan_requires_prepare();
                PccGcZPage *current_page = NULL;
                int64_t current_offset = -1;
                if (
                    (py_header_flags_load(py_header(o))
                        & PY_FLAG_GC_ZPAGE_ALLOC) != 0
                ) {
                    current_page =
                        pcc_gc_backend4_zpage_find_page_for_addr_unlocked(
                            o, size, &current_offset
                        );
                }
                if (current_page == NULL) {
                    current_page =
                        pcc_gc_backend4_zpage_find_reusable_page_unlocked(
                            o, size
                        );
                }
                if (current_page == NULL && prepared_zpage == NULL) {
                    prepared_zpage =
                        pcc_gc_backend4_zpage_pop_free_page_unlocked(size);
                    prepared_zpage_from_free =
                        prepared_zpage != NULL ? 1 : 0;
                    prepared_zpage_ready = 0;
                }
                if (current_page == NULL && prepared_zpage_ready == 0) {
                    need_zpage_page = 1;
                }
            }
            if (required < 0 || zpage_required < 0) {
                pcc_gc_pending_minor_block = NULL;
                pcc_gc_backend4_zpage_track_restore_prepared_unlocked(
                    &prepared_zpage, &prepared_zpage_from_free
                );
                pcc_gc_graph_unlock();
                free(prepared_node);
                free(prepared_slots);
                free(prepared_zpage_node);
                free(prepared_zpage_slots);
                pcc_gc_backend4_zpage_track_finish_prepared(prepared_zpage);
                return;
            }
            if (
                (need_node != 0 && prepared_node == NULL)
                || (
                    required > 0
                    && (prepared_slots == NULL || prepared_cap < required)
                )
                || (
                    need_zpage_node != 0 && prepared_zpage_node == NULL
                )
                || (
                    zpage_required > 0
                    && (
                        prepared_zpage_slots == NULL
                        || prepared_zpage_cap < zpage_required
                    )
                )
                || need_zpage_page != 0
            ) {
                pcc_gc_graph_unlock();
                if (need_node != 0 && prepared_node == NULL) {
                    prepared_node = (
                        PccGcObjectNode *
                    )pcc_gc_object_node_prepare();
                    if (prepared_node == NULL) {
                        pcc_gc_pending_minor_block = NULL;
                        free(prepared_slots);
                        free(prepared_zpage_node);
                        free(prepared_zpage_slots);
                        pcc_gc_graph_lock();
                        pcc_gc_backend4_zpage_track_restore_prepared_unlocked(
                            &prepared_zpage, &prepared_zpage_from_free
                        );
                        pcc_gc_graph_unlock();
                        pcc_gc_backend4_zpage_track_finish_prepared(
                            prepared_zpage
                        );
                        return;
                    }
                }
                if (
                    required > 0
                    && (prepared_slots == NULL || prepared_cap < required)
                ) {
                    free(prepared_slots);
                    prepared_slots = calloc((size_t)required, 24);
                    if (prepared_slots == NULL) {
                        pcc_gc_pending_minor_block = NULL;
                        free(prepared_node);
                        free(prepared_zpage_node);
                        free(prepared_zpage_slots);
                        pcc_gc_graph_lock();
                        pcc_gc_backend4_zpage_track_restore_prepared_unlocked(
                            &prepared_zpage, &prepared_zpage_from_free
                        );
                        pcc_gc_graph_unlock();
                        pcc_gc_backend4_zpage_track_finish_prepared(
                            prepared_zpage
                        );
                        return;
                    }
                    prepared_cap = required;
                }
                if (
                    need_zpage_node != 0
                    && prepared_zpage_node == NULL
                ) {
                    prepared_zpage_node = (
                        PccGcZPageNode *
                    )pcc_gc_backend4_zpage_node_prepare();
                    if (prepared_zpage_node == NULL) {
                        pcc_gc_pending_minor_block = NULL;
                        free(prepared_node);
                        free(prepared_slots);
                        free(prepared_zpage_slots);
                        pcc_gc_graph_lock();
                        pcc_gc_backend4_zpage_track_restore_prepared_unlocked(
                            &prepared_zpage, &prepared_zpage_from_free
                        );
                        pcc_gc_graph_unlock();
                        pcc_gc_backend4_zpage_track_finish_prepared(
                            prepared_zpage
                        );
                        return;
                    }
                }
                if (
                    zpage_required > 0
                    && (
                        prepared_zpage_slots == NULL
                        || prepared_zpage_cap < zpage_required
                    )
                ) {
                    free(prepared_zpage_slots);
                    prepared_zpage_slots = calloc(
                        (size_t)zpage_required, 24
                    );
                    if (prepared_zpage_slots == NULL) {
                        pcc_gc_pending_minor_block = NULL;
                        free(prepared_node);
                        free(prepared_slots);
                        free(prepared_zpage_node);
                        pcc_gc_graph_lock();
                        pcc_gc_backend4_zpage_track_restore_prepared_unlocked(
                            &prepared_zpage, &prepared_zpage_from_free
                        );
                        pcc_gc_graph_unlock();
                        pcc_gc_backend4_zpage_track_finish_prepared(
                            prepared_zpage
                        );
                        return;
                    }
                    prepared_zpage_cap = zpage_required;
                }
                if (need_zpage_page != 0) {
                    prepared_zpage = (
                        PccGcZPage *
                    )pcc_gc_backend4_zpage_track_page_prepare(
                        prepared_zpage, o, size
                    );
                    if (prepared_zpage == NULL) {
                        prepared_zpage_from_free = 0;
                        pcc_gc_pending_minor_block = NULL;
                        free(prepared_node);
                        free(prepared_slots);
                        free(prepared_zpage_node);
                        free(prepared_zpage_slots);
                        return;
                    }
                    prepared_zpage_ready = 1;
                }
                continue;
            }
            void *slots_owner = prepared_slots;
            int64_t commit_result = pcc_gc_object_index_plan_commit(
                &slots_owner, prepared_cap, 1
            );
            prepared_slots = slots_owner;
            if (commit_result < 0) {
                pcc_gc_pending_minor_block = NULL;
                pcc_gc_backend4_zpage_track_restore_prepared_unlocked(
                    &prepared_zpage, &prepared_zpage_from_free
                );
                pcc_gc_graph_unlock();
                free(prepared_node);
                free(prepared_slots);
                free(prepared_zpage_node);
                free(prepared_zpage_slots);
                pcc_gc_backend4_zpage_track_finish_prepared(prepared_zpage);
                return;
            }
            if (backend == PCC_GC_KIND_COLORED_RELOCATING) {
                void *zpage_slots_owner = prepared_zpage_slots;
                int64_t zpage_commit_result =
                    pcc_gc_zpage_owner_index_plan_commit(
                        &zpage_slots_owner, prepared_zpage_cap, 1
                    );
                prepared_zpage_slots = zpage_slots_owner;
                if (zpage_commit_result < 0) {
                    pcc_gc_pending_minor_block = NULL;
                    pcc_gc_backend4_zpage_track_restore_prepared_unlocked(
                        &prepared_zpage, &prepared_zpage_from_free
                    );
                    pcc_gc_graph_unlock();
                    free(prepared_node);
                    free(prepared_slots);
                    free(prepared_zpage_node);
                    free(prepared_zpage_slots);
                    pcc_gc_backend4_zpage_track_finish_prepared(
                        prepared_zpage
                    );
                    return;
                }
            }
        }

        if (
            backend == PCC_GC_KIND_INCREMENTAL_TRICOLOR
            || backend == PCC_GC_KIND_CONCURRENT_MARK_SWEEP
        ) {
            PyObjectHeader *h = py_header(o);
            int32_t color = (
                pcc_gc_mark_active_load() != 0
                ? PY_FLAG_GC_BLACK
                : PY_FLAG_GC_WHITE
            );
            h->flags = (
                h->flags & ~PY_FLAG_GC_COLOR_MASK
            ) | color | PY_FLAG_GC_FRESH_ALLOC;
            pcc_gc_cycle_requested_store(1);
        } else if (backend == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR) {
            PyObjectHeader *h = py_header(o);
            h->flags = (h->flags & ~(
                PY_FLAG_GC_COLOR_MASK
                | PY_FLAG_GC_YOUNG
                | PY_FLAG_GC_OLD
            )) | (PY_FLAG_GC_YOUNG | PY_FLAG_GC_WHITE);
            if (pcc_gc_pending_minor_block != NULL) {
                h->flags |= PY_FLAG_GC_MINOR_ARENA;
            }
            pcc_gc_cycle_requested_store(1);
        } else if (backend == PCC_GC_KIND_COLORED_RELOCATING) {
            PyObjectHeader *h = py_header(o);
            int32_t gen_flags = h->flags & (
                PY_FLAG_GC_YOUNG | PY_FLAG_GC_OLD
            );
            h->flags = (h->flags & ~(
                PY_FLAG_GC_COLOR_MASK
                | PY_FLAG_GC_RELOCATION_CANDIDATE
                | PY_FLAG_GC_RELOCATION_TARGET
            )) | PY_FLAG_GC_WHITE;
            if (gen_flags == 0) h->flags |= PY_FLAG_GC_YOUNG;
            pcc_gc_cycle_requested_store(1);
        }
        if (graph_leaf) {
            pcc_gc_pending_minor_block = NULL;
            pcc_gc_backend4_zpage_track_restore_prepared_unlocked(
                &prepared_zpage, &prepared_zpage_from_free
            );
            pcc_gc_graph_unlock();
            free(prepared_node);
            free(prepared_slots);
            free(prepared_zpage_node);
            free(prepared_zpage_slots);
            pcc_gc_backend4_zpage_track_finish_prepared(prepared_zpage);
            return;
        }

        PccGcObjectNode *n = (
            PccGcObjectNode *
        )pcc_gc_object_node_take_prepared((void **)&prepared_node);
        if (n == NULL) {
            pcc_gc_pending_minor_block = NULL;
            pcc_gc_backend4_zpage_track_restore_prepared_unlocked(
                &prepared_zpage, &prepared_zpage_from_free
            );
            pcc_gc_graph_unlock();
            free(prepared_slots);
            free(prepared_zpage_node);
            free(prepared_zpage_slots);
            pcc_gc_backend4_zpage_track_finish_prepared(prepared_zpage);
            return;
        }
        n->size = size;
        n->freeing = 0;
        __atomic_add_fetch(&pcc_gc_live_bytes, n->size, __ATOMIC_ACQ_REL);
        n->minor_block = pcc_gc_pending_minor_block;
        pcc_gc_pending_minor_block = NULL;
        n->obj = o;
        n->next = NULL;
        n->prev = NULL;
        n->zpage_node = NULL;
        n->young_next = NULL;
        n->young_prev = NULL;
        pcc_gc_object_node_link_head(n);
        int64_t index_result = pcc_gc_object_index_insert_preallocated(o, n);
        if (
            index_result >= 0
            && pcc_gc_granule_is_object_start(o) != 1
        ) {
            (void)pcc_gc_managed_pointer_index_remove(o);
        }
        int32_t final_generation = py_header_flags_load(py_header(o)) & (
            PY_FLAG_GC_YOUNG | PY_FLAG_GC_OLD
        );
        if (final_generation == PY_FLAG_GC_YOUNG) {
            pcc_gc_backend3_young_link_head(n);
        }
        if (backend == PCC_GC_KIND_COLORED_RELOCATING) {
            void *zpage_node_owner = prepared_zpage_node;
            void *zpage_owner = prepared_zpage;
            n->zpage_node = (
                PccGcZPageNode *
            )pcc_gc_backend4_zpage_track_alloc_preallocated(
                o,
                n->size,
                &zpage_node_owner,
                &zpage_owner,
                prepared_zpage_from_free
            );
            prepared_zpage_node = (PccGcZPageNode *)zpage_node_owner;
            prepared_zpage = (PccGcZPage *)zpage_owner;
            if (prepared_zpage == NULL) prepared_zpage_from_free = 0;
        }
        pcc_gc_backend4_zpage_track_restore_prepared_unlocked(
            &prepared_zpage, &prepared_zpage_from_free
        );
        pcc_gc_graph_unlock();
        free(prepared_node);
        free(prepared_slots);
        free(prepared_zpage_node);
        free(prepared_zpage_slots);
        pcc_gc_backend4_zpage_track_finish_prepared(prepared_zpage);
        return;
    }
}

void pcc_gc_note_object_allocated(PyObject *o) {
    pcc_gc_note_object_allocated_sized(o, (int64_t)sizeof(PyObjectHeader));
}

void pcc_gc_note_object_freeing(PyObject *o) {
    pcc_gc_init_config();
    if (o == NULL || PY_IS_TAGGED_INT(o)) return;
    PccGcBackend4RemapFinish finish = {0};
    pcc_gc_graph_lock();
    /* Preserve provenance while the object-index entry is removed.  A LIVE
     * object-family marker already provides it; every other origin needs the
     * exact set.  A failed insertion leaves the object tracked rather than
     * permitting an unchecked header read/free through a provenance gap. */
    if (pcc_gc_granule_is_object_start(o) != 1) {
        if (pcc_gc_managed_pointer_index_insert(o) < 0) {
            goto done;
        }
    }
    if (pcc_gc_backend_uses_forwarding()) {
        pcc_gc_forwarding_detach_into_finish(o, &finish);
        pcc_gc_forwarding_remove_target(o, &finish);
    }
    pcc_gc_identity_remove(o);
    if (pcc_gc_selected_backend == PCC_GC_KIND_COLORED_RELOCATING) {
        pcc_gc_relocation_set_remove(o);
        pcc_gc_backend4_store_buffer_remove(o);
        pcc_gc_backend4_remembered_set_remove(o);
        int32_t zpage_flags = py_header_flags_load(py_header(o))
            & PY_FLAG_GC_ZPAGE_ALLOC;
        PccGcObjectNode *zpage_owner_node =
            (PccGcObjectNode *)pcc_gc_object_index_find(o);
        int32_t zpage_indexed = (
            zpage_owner_node != NULL
            && zpage_owner_node->zpage_node != NULL
        );
        /* Allocation origin is published on the header and, while tracked,
         * in the O(1) object index.  Do not turn every object release into a
         * scan of all live, free, and retained zpages. */
        if (zpage_flags != 0 || zpage_indexed != 0) {
            if (zpage_flags == 0) {
                py_header_flags_or(py_header(o), PY_FLAG_GC_ZPAGE_ALLOC);
            }
            pcc_gc_backend4_zpage_remove_unlocked(o);
        }
    }
    if (!pcc_gc_tracks_objects()) {
        goto done;
    }
    PccGcObjectNode *dead = (PccGcObjectNode *)pcc_gc_object_index_find(o);
    if (dead == NULL) {
        for (PccGcObjectNode *scan = pcc_gc_objects; scan != NULL; scan = scan->next) {
            if (scan->obj == o) {
                dead = scan;
                break;
            }
        }
    }
    if (dead != NULL) {
        if (!pcc_gc_object_node_is_freeing(dead) && dead->size > 0) {
            pcc_gc_live_bytes_subtract(dead->size);
        }
        pcc_gc_object_node_set_freeing(dead);
        if (dead->minor_block != NULL) {
            goto done;
        }
        (void)pcc_gc_object_index_remove(o);
        pcc_gc_object_node_unlink(dead);
        pcc_gc_object_node_release(dead);
    }
done:
    pcc_gc_graph_unlock();
    /* The remap-finish struct is written only under
     * pcc_gc_backend_uses_forwarding() above, so on the non-forwarding
     * backends it is all-null at every exit and the six-way retirement
     * fan-out is pure per-free overhead.  Mirror of the ``moving`` gate in
     * py/py_gc_backend.py::pcc_gc_note_object_freeing. */
    if (pcc_gc_backend_uses_forwarding()) {
        pcc_gc_backend4_finish_remap_retirement(&finish);
    }
}

static void pcc_gc_minor_release_block(PccGcMinorBlock *block) {
    if (block == NULL) return;
    int64_t live = __atomic_load_n(
        &block->live_objects, __ATOMIC_ACQUIRE
    );
    while (live > 0) {
        if (__atomic_compare_exchange_n(
                &block->live_objects,
                &live,
                live - 1,
                0,
                __ATOMIC_ACQ_REL,
                __ATOMIC_ACQUIRE
            )) {
            live--;
            break;
        }
    }
    if (live != 0) return;

    /* Minor blocks are span-retained: stale SSA/root pointers can outlive the
     * object index entry, so the span must remain recognizable until a safer
     * epoch reclamation scheme exists. */
    if (block->owner_thread_id == pcc_current_thread_id()) {
        block->ptr = block->base;
        pcc_gc_minor_current = block;
        __atomic_store_n(&pcc_gc_minor_bytes, 0, __ATOMIC_RELEASE);
    }
}

static PccGcMinorBlock *pcc_gc_minor_block_containing_unlocked(void *ptr) {
    if (ptr == NULL) return NULL;
    uintptr_t p = (uintptr_t)ptr;
    for (PccGcMinorBlock *block = pcc_gc_minor_blocks; block != NULL; block = block->next) {
        uintptr_t base = (uintptr_t)block->base;
        uintptr_t end = (uintptr_t)block->end;
        if (p >= base && p < end) return block;
    }
    return NULL;
}

void pcc_gc_free_object_memory(PyObject *o) {
    if (o == NULL || PY_IS_TAGGED_INT(o)) return;
    if (pcc_gc_pointer_is_managed(o) == 0) return;
    PyObjectHeader *h = py_header(o);
    int32_t flags = py_header_flags_load(h);
    /* Constructors may discard a freshly allocated object before the normal
     * decref dispatcher emits its freeing event.  Make this public free ABI
     * self-contained; note_object_freeing is idempotent for the usual path. */
    pcc_gc_note_object_freeing(o);
    /* Fail closed on a corrupt object-family lifecycle: the allocation must
     * remain quarantined rather than being returned to a freelist/mapping. */
    if (pcc_gc_pointer_unregister(o) < 0) return;
    if ((flags & PY_FLAG_GC_ZPAGE_ALLOC) != 0) {
        return;
    }
    if (
        pcc_gc_selected_backend == PCC_GC_KIND_COLORED_RELOCATING
        && (flags & PY_FLAG_GC_MALLOC_ALLOC) == 0
    ) {
        /* Backend 4 has exactly two owned allocation origins: zpage and
         * malloc.  The zpage case returned above.  An unlabelled/foreign
         * pointer must fail closed instead of being guessed via an O(pages)
         * address scan or handed to free(3). */
        return;
    }
    if (
        (
            pcc_gc_selected_backend == PCC_GC_KIND_INCREMENTAL_TRICOLOR
            || pcc_gc_selected_backend == PCC_GC_KIND_CONCURRENT_MARK_SWEEP
        )
        && flags == 0
    ) {
        return;
    }
    /* GC3 allocation origin is explicit.  MINOR_ARENA identifies a live arena
     * object and MALLOC_ALLOC identifies fallback/oldified heap storage.  The
     * object index remains authoritative when semantic GC flags overwrite the
     * minor bit; never infer malloc ownership merely from non-zero color or
     * generation bits. */
    if (
        (flags & PY_FLAG_GC_MINOR_ARENA) != 0
        || pcc_gc_selected_backend == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
    ) {
        pcc_gc_graph_lock();
        PccGcObjectNode *dead = (PccGcObjectNode *)pcc_gc_object_index_find(o);
        if (dead == NULL) {
            for (PccGcObjectNode *scan = pcc_gc_objects; scan != NULL; scan = scan->next) {
                if (scan->obj == o) {
                    dead = scan;
                    break;
                }
            }
        }
        if (dead != NULL) {
            if (!pcc_gc_object_node_is_freeing(dead) && dead->size > 0) {
                pcc_gc_live_bytes_subtract(dead->size);
            }
            if (pcc_gc_selected_backend == PCC_GC_KIND_COLORED_RELOCATING) {
                pcc_gc_backend4_zpage_remove_unlocked(o);
            }
            PccGcMinorBlock *block = dead->minor_block;
            (void)pcc_gc_object_index_remove(o);
            pcc_gc_object_node_unlink(dead);
            pcc_gc_object_node_release(dead);
            if (block != NULL || (flags & PY_FLAG_GC_MINOR_ARENA) != 0) {
                pcc_gc_minor_release_block(block);
                pcc_gc_graph_unlock();
                return;
            }
            pcc_gc_graph_unlock();
            if (
                pcc_gc_selected_backend
                    == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
                && (flags & PY_FLAG_GC_MALLOC_ALLOC) == 0
            ) {
                return;
            }
            free(o);
            return;
        }
        pcc_gc_graph_unlock();
    }
    if (pcc_gc_selected_backend == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR) {
        pcc_gc_graph_lock();
        PccGcMinorBlock *block =
            pcc_gc_minor_block_containing_unlocked((void *)o);
        pcc_gc_graph_unlock();
        if (block != NULL) {
            pcc_gc_minor_release_block(block);
            return;
        }
        /* Only an explicit allocation-origin bit authorizes system free(). */
        if ((flags & PY_FLAG_GC_MALLOC_ALLOC) == 0) {
            return;
        }
    }
    free(o);
}

void pcc_gc_note_load(void) {
    pcc_gc_metric_add(PCC_GC_COUNTER_READ_BARRIERS, 1);
}

PyObject *pcc_gc_note_relocation_read(PyObject *o) {
    if (o == NULL || PY_IS_TAGGED_INT(o)) return o;
    pcc_gc_init_config();
    /* Non-moving backends have nothing to resolve, so the chain below is the
     * identity for them.  Exact, not optimistic: pcc_gc_install_forwarding
     * refuses unless the selected backend is 3 or 4, and pcc_gc_set_backend
     * refuses to leave 3/4 while a forwarding node or population remains.  The
     * only work skipped is clearing a stale RELOCATION_CANDIDATE hint, which no
     * backend outside 3/4 reads.  Mirrors the port gate in
     * freestanding_gc_forwarding_identity.py. */
    if (
        pcc_gc_selected_backend != PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
        && pcc_gc_selected_backend != PCC_GC_KIND_COLORED_RELOCATING
    ) {
        return o;
    }
    if (pcc_gc_is_known_object(o)) {
        PyObjectHeader *h = py_header(o);
        if (
            (py_header_flags_load(h) & PY_FLAG_GC_RELOCATION_CANDIDATE)
            == 0
        ) {
            return o;
        }
    }
    pcc_gc_graph_lock();
    PyObject *resolved = pcc_gc_note_relocation_read_unlocked(o);
    pcc_gc_graph_unlock();
    return resolved;
}

void pcc_gc_note_store(void) {
    pcc_gc_metric_add(PCC_GC_COUNTER_WRITE_BARRIERS, 1);
}

void pcc_gc_note_slot_write_barrier(
    PyObject *owner,
    PyObject **slot,
    PyObject *value
) {
    if (value == NULL || PY_IS_TAGGED_INT(value)) return;
    int64_t barrier_backend = pcc_gc_selected_backend;
    if (owner == NULL) {
        if (
            barrier_backend == PCC_GC_KIND_INCREMENTAL_TRICOLOR
            || barrier_backend == PCC_GC_KIND_CONCURRENT_MARK_SWEEP
            || barrier_backend == PCC_GC_KIND_COLORED_RELOCATING
        ) {
            pcc_gc_graph_lock();
            if (!pcc_gc_is_known_object(value)) {
                pcc_gc_graph_unlock();
                return;
            }
            PyObjectHeader *value_h = py_header(value);
            int32_t value_flags = py_header_flags_load(value_h);
            int should_gray = (value_flags & PY_FLAG_GC_WHITE) != 0;
            if (barrier_backend == PCC_GC_KIND_CONCURRENT_MARK_SWEEP) {
                should_gray = (value_flags & PY_FLAG_GC_GRAY) == 0;
            }
            if (pcc_gc_mark_active_load() != 0 && should_gray) {
                py_header_flags_update(
                    value_h, PY_FLAG_GC_COLOR_MASK, PY_FLAG_GC_GRAY
                );
                pcc_gc_mark_active_store(1);
                if (
                    barrier_backend
                    == PCC_GC_KIND_CONCURRENT_MARK_SWEEP
                ) {
                    (void)pcc_gc_cms_buffer_gray(value);
                }
            }
            pcc_gc_graph_unlock();
        }
        return;
    }
    if (PY_IS_TAGGED_INT(owner)) return;
    if (
        barrier_backend == PCC_GC_KIND_INCREMENTAL_TRICOLOR
        || barrier_backend == PCC_GC_KIND_CONCURRENT_MARK_SWEEP
    ) {
        pcc_gc_graph_lock();
        if (!pcc_gc_is_known_object(owner) || !pcc_gc_is_known_object(value)) {
            pcc_gc_graph_unlock();
            return;
        }
        PyObjectHeader *owner_h = py_header(owner);
        PyObjectHeader *value_h = py_header(value);
        int should_shade = 0;
        if (
            barrier_backend == PCC_GC_KIND_CONCURRENT_MARK_SWEEP
        ) {
            should_shade = pcc_gc_mark_active_load() != 0;
        } else {
            /* Shading a black owner's white referent keeps the incremental
             * tricolor invariant DURING a cycle.  Outside a cycle there is no
             * invariant to keep, and the store below would fabricate an active
             * cycle with no epoch, no whitening pass and no seeded roots; the
             * next explicit collect then skipped pcc_gc_begin_mark_cycle,
             * traced nothing, finished that phantom cycle with an empty
             * candidate set and reclaimed nothing at all. */
            should_shade =
                pcc_gc_mark_active_load() != 0
                && (py_header_flags_load(owner_h) & PY_FLAG_GC_BLACK) != 0;
        }
        int32_t value_flags = py_header_flags_load(value_h);
        int should_gray_value = (value_flags & PY_FLAG_GC_WHITE) != 0;
        if (barrier_backend == PCC_GC_KIND_CONCURRENT_MARK_SWEEP) {
            should_gray_value = (value_flags & PY_FLAG_GC_GRAY) == 0;
        }
        if (should_shade && should_gray_value) {
            py_header_flags_update(
                value_h, PY_FLAG_GC_COLOR_MASK, PY_FLAG_GC_GRAY
            );
            pcc_gc_mark_active_store(1);
            if (
                barrier_backend
                == PCC_GC_KIND_CONCURRENT_MARK_SWEEP
            ) {
                (void)pcc_gc_cms_buffer_gray(value);
            }
        }
        pcc_gc_graph_unlock();
    } else if (
        barrier_backend == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
        || barrier_backend == PCC_GC_KIND_COLORED_RELOCATING
    ) {
        if (
            !pcc_gc_pointer_can_have_header(owner)
            || !pcc_gc_pointer_can_have_header(value)
        ) {
            return;
        }
        PyObjectHeader *owner_h = py_header(owner);
        PyObjectHeader *value_h = py_header(value);
        if (
            (py_header_flags_load(owner_h) & PY_FLAG_GC_OLD) == 0
            || (py_header_flags_load(value_h) & PY_FLAG_GC_YOUNG) == 0
        ) {
            return;
        }
        pcc_gc_graph_lock();
        if (!pcc_gc_is_known_object(owner) || !pcc_gc_is_known_object(value)) {
            pcc_gc_graph_unlock();
            return;
        }
        if (
            (py_header_flags_load(owner_h) & PY_FLAG_GC_OLD) != 0
            && (py_header_flags_load(value_h) & PY_FLAG_GC_YOUNG) != 0
        ) {
            if (barrier_backend == PCC_GC_KIND_COLORED_RELOCATING) {
                if (pcc_gc_backend4_store_buffer_enqueue(owner, slot, value)) {
                    __atomic_add_fetch(
                        &pcc_gc_backend4_genzgc_store_barriers,
                        1,
                        __ATOMIC_RELAXED
                    );
                }
            } else if (
                barrier_backend == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
            ) {
                pcc_gc_backend3_remember_owner_unlocked(owner, owner_h);
            }
        }
        pcc_gc_graph_unlock();
    }
}

void pcc_gc_note_write_barrier(PyObject *owner, PyObject *value) {
    pcc_gc_note_slot_write_barrier(owner, NULL, value);
}

void pcc_gc_note_safepoint(void) {
    pcc_gc_metric_add(PCC_GC_COUNTER_SAFEPOINTS, 1);
}

void pcc_gc_note_pin(int32_t delta) {
    pcc_gc_metric_add(PCC_GC_COUNTER_PIN_BALANCE, delta);
}

static int32_t pcc_gc_scheduler_root_link_locked(
    PccGcSchedulerRootNode *node
) {
    if (node == NULL) return 0;
    node->prev = NULL;
    node->next = pcc_gc_scheduler_roots;
    if (pcc_gc_scheduler_roots != NULL) {
        pcc_gc_scheduler_roots->prev = node;
    }
    pcc_gc_scheduler_roots = node;
    pcc_gc_root_registry_revision_advance_unlocked();
    int32_t link_error = 0;
#ifdef PCC_RUNTIME_TRIPWIRES
    if (node->slot == NULL) link_error |= 1;
    if (
        node->prev != NULL
        || (node->next != NULL && node->next->prev != node)
    ) {
        link_error |= 2;
    }
#endif
    return link_error;
}

static void pcc_gc_scheduler_root_link_tripwire_fail(
    int32_t link_error
) {
#ifdef PCC_RUNTIME_TRIPWIRES
    if ((link_error & 1) != 0) {
        pcc_runtime_tripwire_fail(
            "pcc_gc_scheduler_root_link_locked: scheduler root has a NULL slot address",
            __FILE__,
            __LINE__
        );
    }
    if ((link_error & 2) != 0) {
        pcc_runtime_tripwire_fail(
            "pcc_gc_scheduler_root_link_locked: scheduler root list links are inconsistent",
            __FILE__,
            __LINE__
        );
    }
#else
    (void)link_error;
#endif
}

static void pcc_gc_scheduler_root_unlink_locked(PccGcSchedulerRootNode *node) {
    if (node == NULL) return;
    if (pcc_gc_backend3_scheduler_root_scan_cursor == node) {
        pcc_gc_backend3_scheduler_root_scan_cursor = node->next;
        pcc_gc_backend3_scheduler_root_scan_slot = 0;
    }
    if (pcc_gc_runtime_root_snapshot_scheduler_cursor == node) {
        pcc_gc_runtime_root_snapshot_scheduler_cursor = node->next;
        pcc_gc_runtime_root_snapshot_slot = 0;
    }
    if (node->prev != NULL) {
        node->prev->next = node->next;
    } else if (pcc_gc_scheduler_roots == node) {
        pcc_gc_scheduler_roots = node->next;
    } else {
        PccGcSchedulerRootNode **cur = &pcc_gc_scheduler_roots;
        while (*cur != NULL && *cur != node) {
            cur = &(*cur)->next;
        }
        if (*cur == NULL) return;
        *cur = node->next;
    }
    if (node->next != NULL) node->next->prev = node->prev;
    node->next = NULL;
    node->prev = NULL;
    pcc_gc_root_registry_revision_advance_unlocked();
}

static PccGcSchedulerRootNode *pcc_gc_scheduler_root_node_alloc(
    PyObject **slot
) {
    pcc_gc_init_config();
    if (slot == NULL) return NULL;
    PccGcSchedulerRootNode *node = (
        PccGcSchedulerRootNode *
    )calloc(1, sizeof(PccGcSchedulerRootNode));
    if (node == NULL) return NULL;
    node->slot = slot;
    return node;
}

static void pcc_gc_scheduler_root_node_free(
    PccGcSchedulerRootNode *node
) {
    free(node);
}

void *pcc_gc_scheduler_root_register_handle(PyObject **slot) {
    PccGcSchedulerRootNode *node = pcc_gc_scheduler_root_node_alloc(slot);
    if (node == NULL) return NULL;
    pcc_gc_graph_lock();
    int32_t link_error = pcc_gc_scheduler_root_link_locked(node);
    pcc_gc_graph_unlock();
    pcc_gc_scheduler_root_link_tripwire_fail(link_error);
    pcc_gc_cycle_requested_store(1);
    return node;
}

void pcc_gc_scheduler_root_register(PyObject **slot) {
    (void)pcc_gc_scheduler_root_register_handle(slot);
}

void pcc_gc_scheduler_root_unregister_handle(void *handle) {
    pcc_gc_init_config();
    if (handle == NULL) return;
    PccGcSchedulerRootNode *node = (PccGcSchedulerRootNode *)handle;
    pcc_gc_graph_lock();
    pcc_gc_scheduler_root_unlink_locked(node);
    pcc_gc_graph_unlock();
    pcc_gc_scheduler_root_node_free(node);
    pcc_gc_cycle_requested_store(1);
}

void pcc_gc_scheduler_root_unregister(PyObject **slot) {
    pcc_gc_init_config();
    if (slot == NULL) return;
    PccGcSchedulerRootNode *dead = NULL;
    pcc_gc_graph_lock();
    PccGcSchedulerRootNode **cur = &pcc_gc_scheduler_roots;
    while (*cur != NULL) {
        if ((*cur)->slot == slot) {
            dead = *cur;
            pcc_gc_scheduler_root_unlink_locked(dead);
            break;
        }
        cur = &(*cur)->next;
    }
    pcc_gc_graph_unlock();
    if (dead != NULL) {
        pcc_gc_scheduler_root_node_free(dead);
        pcc_gc_cycle_requested_store(1);
    }
}

PccGcSchedulerQueue *pcc_gc_scheduler_queue_new(void) {
    PccGcSchedulerQueue *queue = (
        PccGcSchedulerQueue *
    )calloc(1, sizeof(PccGcSchedulerQueue));
    if (queue == NULL) return NULL;
    queue->mutex = pcc_mutex_new();
    if (queue->mutex == NULL) {
        free(queue);
        return NULL;
    }
    return queue;
}

static void pcc_gc_scheduler_queue_entry_free(PccGcSchedulerQueueEntry *entry) {
    if (entry == NULL) return;
    int64_t backend = pcc_gc_backend();
    PccGcStoreRootPlan clear_plan;
    pcc_gc_store_root_plan_init(&clear_plan, backend);
    PccGcSchedulerRootNode *root_node = (
        PccGcSchedulerRootNode *
    )entry->root_handle;
    pcc_gc_graph_lock();
    int64_t barrier_before = pcc_gc_relocation_barrier_forwards;
    int had_value = entry->value != NULL;
    (void)pcc_gc_store_root_plan_commit_locked(
        &clear_plan, &entry->value, NULL
    );
    if (
        backend == PCC_GC_KIND_COLORED_RELOCATING
        && had_value
        && pcc_gc_relocation_forwards > 0
        && pcc_gc_relocation_barrier_forwards == barrier_before
    ) {
        pcc_gc_relocation_barrier_forwards++;
    }
    pcc_gc_scheduler_root_unlink_locked(root_node);
    entry->root_handle = NULL;
    pcc_gc_graph_unlock();
    if (root_node != NULL) {
        pcc_gc_cycle_requested_store(1);
        pcc_gc_scheduler_root_node_free(root_node);
    }
    free(entry);
    pcc_gc_store_root_plan_finish(&clear_plan);
}

#define PCC_GC_SCHEDULER_QUEUE_ENTRY_POOL_LIMIT 4096

static PccGcSchedulerQueueEntry *pcc_gc_scheduler_queue_entry_alloc(
    PccGcSchedulerQueue *queue
) {
    PccGcSchedulerQueueEntry *entry = NULL;
    if (queue != NULL && queue->mutex != NULL) {
        if (pcc_mutex_lock(queue->mutex) == 0) {
            entry = queue->free_head;
            if (entry != NULL) {
                queue->free_head = entry->next;
                if (queue->free_count > 0) queue->free_count--;
            }
            (void)pcc_mutex_unlock(queue->mutex);
        }
    }
    if (entry == NULL) {
        entry = (PccGcSchedulerQueueEntry *)malloc(
            sizeof(PccGcSchedulerQueueEntry)
        );
    }
    if (entry != NULL) memset(entry, 0, sizeof(PccGcSchedulerQueueEntry));
    return entry;
}

static void pcc_gc_scheduler_queue_entry_recycle(
    PccGcSchedulerQueue *queue,
    PccGcSchedulerQueueEntry *entry
) {
    if (entry == NULL) return;
    memset(entry, 0, sizeof(PccGcSchedulerQueueEntry));
    if (queue == NULL || queue->mutex == NULL) {
        free(entry);
        return;
    }
    if (pcc_mutex_lock(queue->mutex) != 0) {
        free(entry);
        return;
    }
    if (queue->free_count >= PCC_GC_SCHEDULER_QUEUE_ENTRY_POOL_LIMIT) {
        (void)pcc_mutex_unlock(queue->mutex);
        free(entry);
        return;
    }
    entry->next = queue->free_head;
    queue->free_head = entry;
    queue->free_count++;
    (void)pcc_mutex_unlock(queue->mutex);
}

static void pcc_gc_scheduler_queue_entry_release(
    PccGcSchedulerQueue *queue,
    PccGcSchedulerQueueEntry *entry
) {
    if (entry == NULL) return;
    PccGcStoreRootPlan clear_plan;
    pcc_gc_store_root_plan_init(&clear_plan, pcc_gc_backend());
    PccGcSchedulerRootNode *root_node = (
        PccGcSchedulerRootNode *
    )entry->root_handle;
    pcc_gc_graph_lock();
    (void)pcc_gc_store_root_plan_commit_locked(
        &clear_plan, &entry->value, NULL
    );
    pcc_gc_scheduler_root_unlink_locked(root_node);
    entry->root_handle = NULL;
    pcc_gc_graph_unlock();
    if (root_node != NULL) {
        pcc_gc_cycle_requested_store(1);
        pcc_gc_scheduler_root_node_free(root_node);
    }
    pcc_gc_scheduler_queue_entry_recycle(queue, entry);
    pcc_gc_store_root_plan_finish(&clear_plan);
}

void pcc_gc_scheduler_queue_free(PccGcSchedulerQueue *queue) {
    if (queue == NULL) return;
    if (queue->mutex != NULL) (void)pcc_mutex_lock(queue->mutex);
    PccGcSchedulerQueueEntry *entry = queue->head;
    queue->head = NULL;
    queue->tail = NULL;
    queue->length = 0;
    if (queue->mutex != NULL) (void)pcc_mutex_unlock(queue->mutex);
    while (entry != NULL) {
        PccGcSchedulerQueueEntry *next = entry->next;
        pcc_gc_scheduler_queue_entry_free(entry);
        entry = next;
    }
    entry = queue->free_head;
    while (entry != NULL) {
        PccGcSchedulerQueueEntry *next = entry->next;
        free(entry);
        entry = next;
    }
    queue->free_head = NULL;
    queue->free_count = 0;
    pcc_mutex_free(queue->mutex);
    free(queue);
}

int64_t pcc_gc_scheduler_queue_push(
    PccGcSchedulerQueue *queue, PyObject *value
) {
    if (queue == NULL) return -1;
    PccGcSchedulerQueueEntry *entry = pcc_gc_scheduler_queue_entry_alloc(queue);
    if (entry == NULL) return -1;
    PccGcSchedulerRootNode *root_node = (
        pcc_gc_scheduler_root_node_alloc(&entry->value)
    );
    if (root_node == NULL) {
        pcc_gc_scheduler_queue_entry_recycle(queue, entry);
        return -1;
    }
    PccGcStoreRootPlan store_plan;
    pcc_gc_store_root_plan_init(&store_plan, pcc_gc_backend());
    int32_t link_error = 0;
    pcc_gc_graph_lock();
    int64_t published = pcc_gc_store_root_plan_commit_locked(
        &store_plan, &entry->value, value
    );
    if (published != 0) {
        entry->root_handle = root_node;
        link_error = pcc_gc_scheduler_root_link_locked(root_node);
    }
    pcc_gc_graph_unlock();
    pcc_gc_scheduler_root_link_tripwire_fail(link_error);
    if (published != 0) pcc_gc_cycle_requested_store(1);
    if (published == 0) {
        pcc_gc_scheduler_root_node_free(root_node);
        pcc_gc_scheduler_queue_entry_recycle(queue, entry);
    }
    pcc_gc_store_root_plan_finish(&store_plan);
    if (published == 0) return -1;
    if (pcc_mutex_lock(queue->mutex) != 0) {
        pcc_gc_scheduler_queue_entry_release(queue, entry);
        return -1;
    }
    if (queue->tail == NULL) {
        queue->head = entry;
        queue->tail = entry;
    } else {
        queue->tail->next = entry;
        queue->tail = entry;
    }
    queue->length++;
    return pcc_mutex_unlock(queue->mutex);
}

int64_t pcc_gc_scheduler_queue_pop_into(
    PccGcSchedulerQueue *queue, PyObject **out_slot
) {
    if (queue == NULL) return -1;
    if (pcc_mutex_lock(queue->mutex) != 0) return -1;
    PccGcSchedulerQueueEntry *entry = queue->head;
    if (entry == NULL) {
        (void)pcc_mutex_unlock(queue->mutex);
        return 0;
    }
    queue->head = entry->next;
    if (queue->head == NULL) queue->tail = NULL;
    queue->length--;
    (void)pcc_mutex_unlock(queue->mutex);
    int64_t backend = pcc_gc_backend();
    PccGcStoreRootPlan out_plan;
    if (out_slot != NULL) {
        pcc_gc_store_root_plan_init(&out_plan, backend);
    }
    PccGcStoreRootPlan clear_plan;
    pcc_gc_store_root_plan_init(&clear_plan, backend);
    PccGcSchedulerRootNode *root_node = (
        PccGcSchedulerRootNode *
    )entry->root_handle;
    pcc_gc_graph_lock();
    PyObject *value = entry->value;
    if (out_slot != NULL) {
        (void)pcc_gc_store_root_plan_commit_locked(
            &out_plan, out_slot, value
        );
    }
    (void)pcc_gc_store_root_plan_commit_locked(
        &clear_plan, &entry->value, NULL
    );
    pcc_gc_scheduler_root_unlink_locked(root_node);
    entry->root_handle = NULL;
    pcc_gc_graph_unlock();
    if (root_node != NULL) {
        pcc_gc_cycle_requested_store(1);
        pcc_gc_scheduler_root_node_free(root_node);
    }
    pcc_gc_scheduler_queue_entry_recycle(queue, entry);
    if (out_slot != NULL) pcc_gc_store_root_plan_finish(&out_plan);
    pcc_gc_store_root_plan_finish(&clear_plan);
    return 1;
}

int64_t pcc_gc_scheduler_queue_len(PccGcSchedulerQueue *queue) {
    if (queue == NULL) return 0;
    if (pcc_mutex_lock(queue->mutex) != 0) return -1;
    int64_t length = queue->length;
    (void)pcc_mutex_unlock(queue->mutex);
    return length;
}

int64_t pcc_gc_scheduler_root_count(void) {
    pcc_gc_init_config();
    pcc_gc_graph_lock();
    int64_t count = 0;
#ifdef PCC_RUNTIME_TRIPWIRES
    PccGcSchedulerRootNode *prev = NULL;
#endif
    for (
        PccGcSchedulerRootNode *n = pcc_gc_scheduler_roots;
        n != NULL;
        n = n->next
    ) {
        PCC_GC_DEFER_TRIPWIRE(
            n->slot != NULL,
            "pcc_gc_scheduler_root_count: scheduler root has a NULL slot address"
        );
        PCC_GC_DEFER_TRIPWIRE(
            n->prev == prev,
            "pcc_gc_scheduler_root_count: scheduler root prev/next linkage mismatch"
        );
#ifdef PCC_RUNTIME_TRIPWIRES
        prev = n;
#endif
        count++;
    }
    pcc_gc_graph_unlock();
    return count;
}

int64_t pcc_gc_frame_root_slot_count(void) {
    pcc_gc_init_config();
    pcc_gc_graph_lock();
    int64_t slots = 0;
    for (
        PccGcFrameNode *n = pcc_gc_frames;
        n != NULL;
        n = n->next
    ) {
        slots += n->root_count;
    }
    pcc_gc_graph_unlock();
    return slots;
}

int64_t pcc_gc_continuation_root_slot_count(void) {
    pcc_gc_init_config();
    pcc_gc_graph_lock();
    int64_t slots = 0;
    for (
        PccGcContinuationRootNode *n = pcc_gc_continuation_roots;
        n != NULL;
        n = n->next
    ) {
        PCC_GC_DEFER_TRIPWIRE(
            n->frame_map != NULL && n->slots != NULL,
            "pcc_gc_continuation_root_slot_count: continuation root lost its map or slot base"
        );
        PCC_GC_DEFER_TRIPWIRE(
            n->root_count > 0 && n->stable_values != NULL,
            "pcc_gc_continuation_root_slot_count: continuation root count/stable buffer is invalid"
        );
        PCC_GC_DEFER_TRIPWIRE(
            n->root_count == pcc_gc_root_slot_count_from_map(n->frame_map),
            "pcc_gc_continuation_root_slot_count: continuation root map/count drift"
        );
        slots += n->root_count;
    }
    pcc_gc_graph_unlock();
    return slots;
}

int64_t pcc_gc_coroutine_root_score(void) {
    return pcc_gc_scheduler_root_count()
        + pcc_gc_frame_root_slot_count()
        + pcc_gc_continuation_root_slot_count();
}

static int pcc_gc_slot_in_root_range(
    PyObject **slot,
    PyObject **slots,
    int64_t count
) {
    if (slot == NULL || slots == NULL || count <= 0) return 0;
    if (count > (INT64_MAX / (int64_t)sizeof(PyObject *))) return 0;
    uintptr_t slot_addr = (uintptr_t)slot;
    uintptr_t base_addr = (uintptr_t)slots;
    uintptr_t bytes = (uintptr_t)count * (uintptr_t)sizeof(PyObject *);
    uintptr_t end_addr = base_addr + bytes;
    if (end_addr < base_addr) return 0;
    if (slot_addr < base_addr || slot_addr >= end_addr) return 0;
    return ((slot_addr - base_addr) % sizeof(PyObject *)) == 0;
}

int64_t pcc_gc_slot_is_runtime_root(PyObject **slot) {
    pcc_gc_init_config();
    if (slot == NULL) return 0;
    pcc_gc_graph_lock();
    for (
        PccGcFrameNode *n = pcc_gc_frames;
        n != NULL;
        n = n->next
    ) {
        if (pcc_gc_slot_in_root_range(slot, n->slots, n->root_count)) {
            pcc_gc_graph_unlock();
            return 1;
        }
    }
    for (
        PccGcContinuationRootNode *n = pcc_gc_continuation_roots;
        n != NULL;
        n = n->next
    ) {
        if (pcc_gc_slot_in_root_range(slot, n->slots, n->root_count)) {
            pcc_gc_graph_unlock();
            return 1;
        }
    }
    for (
        PccGcSchedulerRootNode *n = pcc_gc_scheduler_roots;
        n != NULL;
        n = n->next
    ) {
        if (n->slot == slot) {
            pcc_gc_graph_unlock();
            return 1;
        }
    }
    pcc_gc_graph_unlock();
    return 0;
}

void *pcc_gc_register_continuation_root_node(
    const void *frame_map,
    PyObject **slots
) {
    pcc_gc_init_config();
    if (frame_map == NULL || slots == NULL) return NULL;
    int64_t n_slots = pcc_gc_root_slot_count_from_map((const int32_t *)frame_map);
    if (n_slots <= 0) return NULL;
    size_t stable_bytes = (size_t)n_slots * sizeof(PyObject *);
    PccGcContinuationRootNode *n = (
        PccGcContinuationRootNode *
    )calloc(1, sizeof(PccGcContinuationRootNode) + stable_bytes);
    if (n == NULL) return NULL;
    n->frame_map = (const int32_t *)frame_map;
    n->slots = slots;
    n->root_count = n_slots;
    n->borrowed = pcc_gc_root_map_is_borrowed((const int32_t *)frame_map);
    n->stable_values = (PyObject **)((uint8_t *)n + sizeof(*n));
    PCC_RT_TRIPWIRE(
        n->root_count == n_slots && n->stable_values != NULL,
        "pcc_gc_register_continuation_root: continuation root allocation metadata is inconsistent"
    );
    pcc_gc_graph_lock();
    n->next = pcc_gc_continuation_roots;
    n->prev = NULL;
    if (pcc_gc_continuation_roots != NULL) {
        pcc_gc_continuation_roots->prev = n;
    }
    pcc_gc_continuation_roots = n;
    pcc_gc_root_registry_revision_advance_unlocked();
    pcc_gc_cycle_requested_store(1);
    pcc_gc_graph_unlock();
    return n;
}

void pcc_gc_register_continuation_root(
    const void *frame_map,
    PyObject **slots
) {
    (void)pcc_gc_register_continuation_root_node(frame_map, slots);
}

/* Caller holds the graph lock.  Mirrors
 * pcc_gc_continuation_root_unlink_locked in the pcc-Python registry. */
static void pcc_gc_continuation_root_unlink_locked(
    PccGcContinuationRootNode *dead
) {
    if (pcc_gc_backend3_continuation_root_scan_cursor == dead) {
        pcc_gc_backend3_continuation_root_scan_cursor = dead->next;
        pcc_gc_backend3_frame_root_scan_slot = 0;
    }
    if (pcc_gc_runtime_root_snapshot_continuation_cursor == dead) {
        pcc_gc_runtime_root_snapshot_continuation_cursor = dead->next;
        pcc_gc_runtime_root_snapshot_slot = 0;
    }
    if (dead->prev == NULL) {
        pcc_gc_continuation_roots = dead->next;
    } else {
        dead->prev->next = dead->next;
    }
    if (dead->next != NULL) {
        dead->next->prev = dead->prev;
    }
    pcc_gc_root_registry_revision_advance_unlocked();
    pcc_gc_cycle_requested_store(1);
}

/* O(1) removal for a caller that kept the node.  The caller must clear its
 * stored pointer first: the node is freed here. */
void pcc_gc_unregister_continuation_root_node(void *node) {
    pcc_gc_init_config();
    if (node == NULL) return;
    pcc_gc_graph_lock();
    pcc_gc_continuation_root_unlink_locked(
        (PccGcContinuationRootNode *)node
    );
    pcc_gc_graph_unlock();
    free(node);
}

void pcc_gc_unregister_continuation_root(PyObject **slots) {
    pcc_gc_init_config();
    if (slots == NULL) return;
    PccGcContinuationRootNode *dead = NULL;
    pcc_gc_graph_lock();
    for (
        PccGcContinuationRootNode *cur = pcc_gc_continuation_roots;
        cur != NULL;
        cur = cur->next
    ) {
        if (cur->slots == slots) {
            dead = cur;
            pcc_gc_continuation_root_unlink_locked(dead);
            break;
        }
    }
    pcc_gc_graph_unlock();
    free(dead);
}

int64_t pcc_gc_trace_continuation_roots(void) {
    pcc_gc_init_config();
    pcc_gc_graph_lock();
    int64_t traced = 0;
    PccGcGrayMappedRootSlotCtx root_ctx = { 0 };
    for (
        PccGcContinuationRootNode *n = pcc_gc_continuation_roots;
        n != NULL;
        n = n->next
    ) {
        traced += pcc_gc_visit_mapped_root_slots_unlocked(
            n->root_count,
            n->slots,
            NULL,
            n->borrowed,
            pcc_gc_gray_mapped_root_slot,
            &root_ctx
        );
    }
    pcc_gc_graph_unlock();
    return traced;
}

int64_t pcc_gc_rewrite_continuation_roots(void) {
    pcc_gc_init_config();
    pcc_gc_graph_lock();
    int64_t rewritten = 0;
    for (
        PccGcContinuationRootNode *n = pcc_gc_continuation_roots;
        n != NULL;
        n = n->next
    ) {
        PccGcRewriteMappedRootCtx rewrite_ctx = { 0 };
        (void)pcc_gc_visit_mapped_root_slots_unlocked(
            n->root_count,
            n->slots,
            NULL,
            n->borrowed,
            pcc_gc_rewrite_mapped_root_slot,
            &rewrite_ctx
        );
        rewritten += rewrite_ctx.rewritten;
    }
    pcc_gc_graph_unlock();
    return rewritten;
}

static void pcc_gc_frame_node_unlink(PccGcFrameNode *node) {
    if (node == NULL) return;
    PccGcFrameNode *prev = node->prev;
    PccGcFrameNode *next = node->next;
    if (pcc_gc_backend3_frame_root_scan_cursor == node) {
        pcc_gc_backend3_frame_root_scan_cursor = next;
        pcc_gc_backend3_frame_root_scan_slot = 0;
    }
    if (pcc_gc_runtime_root_snapshot_frame_cursor == node) {
        pcc_gc_runtime_root_snapshot_frame_cursor = next;
        pcc_gc_runtime_root_snapshot_slot = 0;
    }
    if (prev != NULL) {
        prev->next = next;
    } else if (pcc_gc_frames == node) {
        pcc_gc_frames = next;
    }
    if (next != NULL) next->prev = prev;
    node->prev = NULL;
    node->next = NULL;
    node->dup_next = NULL;
    pcc_gc_root_registry_revision_advance_unlocked();
}

static int64_t pcc_gc_frame_node_bucket(int64_t root_count) {
    /* Bucket zero keeps the allocation helper's 0..16 contract inclusive;
     * note_frame_enter still rejects zero-root maps before allocating. */
    if (
        root_count < 0
        || root_count > PCC_GC_FRAME_NODE_POOL_MAX_ROOTS
    ) {
        return -1;
    }
    return root_count;
}

static size_t pcc_gc_frame_node_size(int64_t root_count) {
    size_t stable_bytes = (size_t)root_count * sizeof(PyObject *);
    return sizeof(PccGcFrameNode) + stable_bytes;
}

static PccGcFrameNode *pcc_gc_frame_node_alloc_unlocked(int64_t root_count) {
    int64_t bucket = pcc_gc_frame_node_bucket(root_count);
    size_t bytes = pcc_gc_frame_node_size(root_count);
    if (
        (pcc_gc_selected_backend == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
         || pcc_gc_selected_backend == PCC_GC_KIND_COLORED_RELOCATING)
        && bucket >= 0
    ) {
        PccGcFrameNode *node = pcc_gc_frame_node_free_lists[bucket];
        if (node != NULL) {
            pcc_gc_frame_node_free_lists[bucket] = node->next;
            if (pcc_gc_frame_node_free_counts[bucket] > 0) {
                pcc_gc_frame_node_free_counts[bucket]--;
            }
            if (pcc_gc_frame_node_free_total > 0) {
                pcc_gc_frame_node_free_total--;
            }
            memset(node, 0, bytes);
            return node;
        }
    }
    return (PccGcFrameNode *)calloc(1, bytes);
}

static PccGcFrameNode *pcc_gc_frame_node_create_unlocked(
    const void *frame_map,
    PyObject **slots,
    int64_t n_slots,
    int32_t extra_flags
) {
    PccGcFrameNode *n = pcc_gc_frame_node_alloc_unlocked(n_slots);
    if (n == NULL) return NULL;
    n->frame_map = (const int32_t *)frame_map;
    n->slots = slots;
    n->root_count = n_slots;
    n->borrowed = (
        pcc_gc_root_map_is_borrowed((const int32_t *)frame_map)
        | extra_flags
    );
    n->stable_values = (PyObject **)((uint8_t *)n + sizeof(*n));
    return n;
}

static void pcc_gc_frame_node_link_unlocked(PccGcFrameNode *node) {
    if (node == NULL) return;
    node->next = pcc_gc_frames;
    node->prev = NULL;
    if (pcc_gc_frames != NULL) pcc_gc_frames->prev = node;
    pcc_gc_frames = node;
    pcc_gc_root_registry_revision_advance_unlocked();
}

static void pcc_gc_frame_node_release_unlocked(PccGcFrameNode *node) {
    if (node == NULL) return;
    int64_t bucket = pcc_gc_frame_node_bucket(node->root_count);
    if (
        (pcc_gc_selected_backend != PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
         && pcc_gc_selected_backend != PCC_GC_KIND_COLORED_RELOCATING)
        || bucket < 0
        || pcc_gc_frame_node_free_total >= PCC_GC_FRAME_NODE_POOL_LIMIT
    ) {
        free(node);
        return;
    }
    size_t bytes = pcc_gc_frame_node_size(node->root_count);
    memset(node, 0, bytes);
    node->next = pcc_gc_frame_node_free_lists[bucket];
    pcc_gc_frame_node_free_lists[bucket] = node;
    pcc_gc_frame_node_free_counts[bucket]++;
    pcc_gc_frame_node_free_total++;
}

int64_t pcc_gc_frame_node_tls_pool_cached_count(void) {
    return pcc_gc_frame_node_free_total;
}

void pcc_gc_frame_node_tls_pool_drain(void) {
    for (
        int64_t bucket = 0;
        bucket <= PCC_GC_FRAME_NODE_POOL_MAX_ROOTS;
        bucket++
    ) {
        PccGcFrameNode *node = pcc_gc_frame_node_free_lists[bucket];
        pcc_gc_frame_node_free_lists[bucket] = NULL;
        pcc_gc_frame_node_free_counts[bucket] = 0;
        while (node != NULL) {
            PccGcFrameNode *next = node->next;
            free(node);
            node = next;
        }
    }
    pcc_gc_frame_node_free_total = 0;
}

void pcc_gc_note_frame_enter(const void *frame_map, PyObject **slots) {
    if (pcc_gc_frame_roots_disabled_fast()) return;
    pcc_gc_init_config();
    if (!pcc_gc_should_track_frame_roots()) return;
    if (frame_map == NULL || slots == NULL) return;
    int64_t n_slots = pcc_gc_root_slot_count_from_map((const int32_t *)frame_map);
    if (n_slots <= 0) return;
    PccGcFrameNode *n = pcc_gc_frame_node_create_unlocked(
        frame_map,
        slots,
        n_slots,
        0
    );
    if (n == NULL) return;
    void *prepared_slots = NULL;
    int64_t prepared_cap = 0;
    for (;;) {
        pcc_gc_graph_lock();
        int64_t required = pcc_gc_frame_index_plan_capacity(1);
        if (required < 0) {
            pcc_gc_graph_unlock();
            free(prepared_slots);
            pcc_gc_frame_node_release_unlocked(n);
            return;
        }
        if (
            required > 0
            && (prepared_slots == NULL || prepared_cap < required)
        ) {
            pcc_gc_graph_unlock();
            free(prepared_slots);
            prepared_slots = calloc((size_t)required, 24);
            if (prepared_slots == NULL) {
                pcc_gc_frame_node_release_unlocked(n);
                return;
            }
            prepared_cap = required;
            continue;
        }
        if (
            required > 0
            && pcc_gc_frame_index_plan_commit(
                &prepared_slots, prepared_cap, 1
            ) < 0
        ) {
            pcc_gc_graph_unlock();
            free(prepared_slots);
            prepared_slots = NULL;
            prepared_cap = 0;
            continue;
        }
        pcc_gc_frame_node_link_unlocked(n);
        PccGcFrameNode *duplicate = (
            PccGcFrameNode *
        )pcc_gc_frame_index_replace_preallocated((void *)slots, n);
        if (duplicate == n) {
            pcc_gc_frame_node_unlink(n);
            pcc_gc_graph_unlock();
            free(prepared_slots);
            pcc_gc_frame_node_release_unlocked(n);
            return;
        }
        n->dup_next = duplicate;
        pcc_gc_cycle_requested_store(1);
        pcc_gc_graph_unlock();
        free(prepared_slots);
        return;
    }
}

void pcc_gc_note_frame_enter_lifo(const void *frame_map, PyObject **slots) {
    if (pcc_gc_frame_roots_disabled_fast()) return;
    pcc_gc_init_config();
    if (!pcc_gc_should_track_frame_roots()) return;
    if (frame_map == NULL || slots == NULL) return;
    int64_t n_slots = pcc_gc_root_slot_count_from_map((const int32_t *)frame_map);
    if (n_slots <= 0) return;
    PccGcFrameNode *n = pcc_gc_frame_node_create_unlocked(
        frame_map,
        slots,
        n_slots,
        PCC_GC_FRAME_NODE_FLAG_LIFO
    );
    if (n == NULL) return;
    pcc_gc_graph_lock();
    pcc_gc_frame_node_link_unlocked(n);
    pcc_gc_cycle_requested_store(1);
    pcc_gc_graph_unlock();
}

void pcc_gc_note_frame_leave_lifo(PyObject **slots) {
    if (pcc_gc_frame_roots_disabled_fast()) return;
    pcc_gc_init_config();
    if (!pcc_gc_should_track_frame_roots()) return;
    if (slots == NULL) return;
    PccGcFrameNode *released = NULL;
    pcc_gc_graph_lock();
    PccGcFrameNode *node = pcc_gc_frames;
    if (
        node != NULL
        && node->slots == slots
        && (node->borrowed & PCC_GC_FRAME_NODE_FLAG_LIFO) != 0
    ) {
        pcc_gc_frame_node_unlink(node);
        pcc_gc_cycle_requested_store(1);
        released = node;
    } else {
        for (node = pcc_gc_frames; node != NULL; node = node->next) {
            if (
                node->slots == slots
                && (node->borrowed & PCC_GC_FRAME_NODE_FLAG_LIFO) != 0
            ) {
                pcc_gc_frame_node_unlink(node);
                pcc_gc_cycle_requested_store(1);
                released = node;
                break;
            }
        }
    }
    pcc_gc_graph_unlock();
    pcc_gc_frame_node_release_unlocked(released);
}

void pcc_gc_note_frame_leave(PyObject **slots) {
    if (pcc_gc_frame_roots_disabled_fast()) return;
    pcc_gc_init_config();
    if (!pcc_gc_should_track_frame_roots()) return;
    if (slots == NULL) return;
    PccGcFrameNode *released = NULL;
    pcc_gc_graph_lock();
    if (
        pcc_gc_selected_backend == PCC_GC_KIND_REFCOUNT_CYCLE
        && pcc_gc_frames == NULL
    ) {
        pcc_gc_graph_unlock();
        return;
    }
    PccGcFrameNode *indexed = (
        PccGcFrameNode *
    )pcc_gc_frame_index_find((void *)slots);
    if (indexed == NULL) {
        pcc_gc_graph_unlock();
        return;
    }
    if (indexed->slots == slots) {
        PccGcFrameNode *duplicate = indexed->dup_next;
        pcc_gc_frame_node_unlink(indexed);
        if (duplicate != NULL) {
            (void)pcc_gc_frame_index_replace_preallocated(
                (void *)slots, duplicate
            );
        } else {
            (void)pcc_gc_frame_index_remove((void *)slots);
        }
        released = indexed;
        pcc_gc_cycle_requested_store(1);
    } else {
        (void)pcc_gc_frame_index_remove((void *)slots);
        (void)pcc_gc_frame_index_replace_preallocated(
            (void *)indexed->slots, indexed
        );
    }
    pcc_gc_graph_unlock();
    pcc_gc_frame_node_release_unlocked(released);
}
