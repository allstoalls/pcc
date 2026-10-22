/* Execute the current emitted copy transaction against isolated raw-ABI stubs.
 * This is not a managed-runtime/allocator/GC integration test. In particular,
 * zpage_find is a counted index-map stub; detach only unlinks this fixture's
 * owner node. PCC emits both the actual transaction and membership index.
 * No membership provider is stubbed: every list entry calls the real note API.
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "harness_globals.h"

#ifndef CAP
#define CAP 96
#endif
#define SIZE 64
#define SRC_FLAGS 9472u /* Sweep verdict plus two preserved/cleared source flag bits. */
typedef struct { uint64_t words[8]; } Object;
typedef struct { uint64_t words[10]; } ZNode;
typedef struct { uint64_t words[16]; } Page;
typedef struct { uint64_t words[2]; } Entry;
static Object objects[CAP], target;
static ZNode znodes[CAP], stale_global_node;
static Page pages[CAP];
static Entry relocation[CAP], selected_pages[CAP];
static void *index_map[CAP], *global_zhead;
static uint64_t finish_plan[3], saved_finish[3], payload_token, forwarding_token;
static int object_count = 3, relocation_count = 3, page_count = 3;
static int rel_ids[CAP] = {1, 2, 3}, page_ids[CAP] = {1, 2, 3};
static int threads, payload_fail, forwarding_fail, pinned, unsupported;
static int known_from = SIZE, known_to = SIZE, from_index_missing, null_page;
static int reenter, cursor_other, page_flag_clear;
static int find_calls, detach_calls, payload_calls, install_calls, forwarding_calls;
static int free_calls, finish_calls, unexpected_calls, stub_errors;
static int calloc_calls, fail_calloc;
static int64_t member_before;
static void *allocations[32];
static size_t allocation_sizes[32], live_bytes, peak_bytes;
extern void *__real_calloc(size_t, size_t);
extern void __real_free(void *);
extern void pcc_gc_relocation_member_note(void *);
extern void pcc_gc_relocation_member_forget(void *);
extern void pcc_gc_relocation_member_clear(void);
extern int64_t pcc_gc_relocation_member_count(void *);
extern int64_t pcc_gc_relocation_member_maybe(void *);
extern int64_t pcc_gc_index_py_hash_ptr(void *);
extern int64_t pcc_gc_index_py_find_slot(void *, int64_t, void *);
extern void *pcc_py_gc_relocation_member_slots;
extern int64_t pcc_py_gc_relocation_member_cap;
extern int64_t pcc_py_gc_relocation_member_count;
extern int64_t pcc_py_gc_relocation_member_used;
extern int64_t pcc_py_gc_relocation_member_incomplete;
static void *installed_forwarding;
extern void *pcc_gc_backend4_relocate_copy_preallocated_unlocked(
    void *, int64_t, void *, void *, void *, void *);

static uint64_t u64(const void *p, int offset) {
    uint64_t value; memcpy(&value, (const char *)p + offset, 8); return value;
}
static uint32_t u32(const void *p, int offset) {
    uint32_t value; memcpy(&value, (const char *)p + offset, 4); return value;
}
static void w64(void *p, int offset, uint64_t value) {
    memcpy((char *)p + offset, &value, 8);
}
static void w32(void *p, int offset, uint32_t value) {
    memcpy((char *)p + offset, &value, 4);
}
static void *ptr(const void *p, int offset) { return (void *)(uintptr_t)u64(p, offset); }
static void wp(void *p, int offset, void *value) { w64(p, offset, (uintptr_t)value); }
static int object_id(void *p) {
    uintptr_t address = (uintptr_t)p, base = (uintptr_t)&objects[0];
    if (address >= base && address < base + (uintptr_t)object_count * sizeof(Object) &&
        (address - base) % sizeof(Object) == 0)
        return (int)((address - base) / sizeof(Object)) + 1;
    return p == &target ? 999 : 0;
}
static int entry_id(void *p, Entry *entries, int count) {
    if (!p) return 0;
    for (int i = 0; i < count; i++) if (p == &entries[i]) return i + 1;
    stub_errors++; return -1;
}
static int znode_id(void *p) {
    if (!p) return 0;
    for (int i = 0; i < object_count; i++) if (p == &znodes[i]) return i + 1;
    stub_errors++; return -1;
}
static int page_id(void *p) {
    for (int i = 0; i < page_count; i++) if (p == &pages[i]) return i + 1;
    stub_errors++; return -1;
}

/* Retain nominal threaded fixture inputs; the locked transaction no longer
 * consults a threads provider. A call here is a regression. */
int64_t pcc_threads_enabled(void) { unexpected_calls++; return threads; }
void *pcc_allocator_object_side_slot(void *p, int64_t create) {
    (void)p; (void)create; return NULL;
}
void *pcc_allocator_granule_object_slot(void *p) { (void)p; return NULL; }
int64_t pcc_gc_object_is_address_pinned(void *p) {
    if (p != &objects[0]) stub_errors++;
    return pinned;
}
int64_t pcc_gc_backend4_relocate_copy_supported_tag(int64_t tag) {
    return tag == 2 && !unsupported;
}
int64_t pcc_gc_object_known_size(void *p) {
    if (p == &objects[0]) return known_from;
    if (p == &target) return known_to;
    stub_errors++; return 0;
}
void *pcc_gc_forwarding_find(void *p) {
    forwarding_calls++;
    if (p != &objects[0]) stub_errors++;
    return installed_forwarding;
}
int64_t pcc_gc_relocate_copy_payload_prepared_locked(
    void *from, void *to, int64_t tag, int64_t size, void *plan) {
    payload_calls++;
    if (from != &objects[0] || to != &target || tag != 2 || size != SIZE ||
        plan != &payload_token || u64(to, 0) != 1 || u32(to, 12) != 65792)
        stub_errors++;
    return !payload_fail;
}
int64_t pcc_gc_install_forwarding_preallocated_unlocked(void *from, void *to, void *plan) {
    install_calls++;
    if (from != &objects[0] || to != &target || plan != &forwarding_token ||
        !(u32(from, 12) & 1024)) stub_errors++;
    if (forwarding_fail) return 1;
    installed_forwarding = to;
    return 0;
}
void *pcc_gc_backend4_zpage_find(void *p) {
    find_calls++;
    int id = object_id(p);
    return id > 0 && id <= object_count ? index_map[id - 1] : NULL;
}
void *pcc_gc_backend4_zpage_detach_for_relocation(void *p) {
    detach_calls++;
    if (p != &objects[0] || installed_forwarding != &target ||
        !(u32(p, 12) & 1) || (u32(p, 12) & 1024)) stub_errors++;
    void *node = index_map[0];
    index_map[0] = NULL;
    if (!node) return NULL;
    void *page = ptr(node, 8);
    /* Match the real node's constant-time global/page backlink layout. */
    void *previous = ptr(node, 56), *next = ptr(node, 48);
    if (page) {
        if (previous) wp(previous, 48, next);
        else if (ptr(page, 112) == node) wp(page, 112, next);
        if (next) wp(next, 56, previous);
    }
    previous = ptr(node, 40); next = ptr(node, 16);
    if (previous) wp(previous, 16, next);
    else if (global_zhead == node) global_zhead = next;
    if (next) wp(next, 40, previous);
    wp(node, 16, NULL); wp(node, 40, NULL);
    wp(node, 48, NULL); wp(node, 56, NULL);
    return node;
}
/* These resolve the entire current module, including the public wrapper.
 * The preallocated entry must never call them or dispose of finish ownership.
 */
void *__wrap_calloc(size_t count, size_t size) {
    calloc_calls++;
    if (fail_calloc) return NULL;
    if (size && count > (8u * 1024u * 1024u) / size) { stub_errors++; return NULL; }
    size_t bytes = count * size;
    if (live_bytes + bytes > 8u * 1024u * 1024u) { stub_errors++; return NULL; }
    for (int i = 0; i < 32; i++) if (!allocations[i]) {
        void *p = __real_calloc(count, size);
        if (p) {
            allocations[i] = p; allocation_sizes[i] = bytes; live_bytes += bytes;
            if (live_bytes > peak_bytes) peak_bytes = live_bytes;
        }
        return p;
    }
    stub_errors++; return NULL;
}
void __wrap_free(void *p) {
    if (!p) return;
    free_calls++;
    for (int i = 0; i < 32; i++) if (allocations[i] == p) {
        live_bytes -= allocation_sizes[i]; allocations[i] = NULL;
        __real_free(p); return;
    }
    stub_errors++;
}
void pcc_gc_backend4_zpage_finish_relocation_detach(void *p) { (void)p; finish_calls++; }
void *pcc_gc_alloc(int64_t n, int32_t tag, int32_t flags) {
    (void)n; (void)tag; (void)flags; unexpected_calls++; return NULL;
}
int64_t pcc_gc_config_ensure(void) { unexpected_calls++; return 4; }
void *pcc_gc_forwarding_install_plan_prepare(void *a, void *b) {
    (void)a; (void)b; unexpected_calls++; return NULL;
}
void pcc_gc_forwarding_install_plan_finish(void *p) { (void)p; unexpected_calls++; }
int64_t pcc_gc_memoryview_refresh_owned_buffer(void *p) { (void)p; unexpected_calls++; return 1; }
int64_t pcc_gc_relocation_payload_slot_count_locked(void *p) { (void)p; unexpected_calls++; return 0; }
void *pcc_gc_relocation_payload_plan_prepare(int64_t n) { (void)n; unexpected_calls++; return NULL; }
int64_t pcc_gc_relocation_payload_raw_snapshot_locked(void *p, int64_t tag, int64_t size, void *plan) {
    (void)p; (void)tag; (void)size; (void)plan; unexpected_calls++; return 0;
}
int64_t pcc_gc_relocation_payload_raw_prepare(void *p) { (void)p; unexpected_calls++; return 0; }
int64_t pcc_gc_relocation_payload_raw_validate_locked(void *a, void *b, int64_t t, int64_t n, void *p) {
    (void)a; (void)b; (void)t; (void)n; (void)p; unexpected_calls++; return 0;
}
int64_t pcc_gc_relocation_payload_plan_validate_locked(void *a, void *b, int64_t n, void *p) {
    (void)a; (void)b; (void)n; (void)p; unexpected_calls++; return 0;
}
void pcc_gc_relocation_payload_plan_finish(void *p) { (void)p; unexpected_calls++; }
void pcc_py_gc_minor_graph_lock(void) { unexpected_calls++; }
void pcc_py_gc_minor_graph_unlock(void) { unexpected_calls++; }
void py_decref(void *p) { (void)p; unexpected_calls++; }

static void initialize_backlinks(void) {
    void *previous = NULL;
    for (void *node = global_zhead; node; node = ptr(node, 16)) {
        wp(node, 40, previous); previous = node;
    }
    for (int i = 0; i < page_count; i++) {
        previous = NULL;
        for (void *node = ptr(&pages[i], 112); node; node = ptr(node, 48)) {
            wp(node, 56, previous); previous = node;
        }
    }
}

static int configure(const char *mode) {
    if (!strcmp(mode, "singleton-head") || !strcmp(mode, "reentry")) {
        reenter = !strcmp(mode, "reentry");
    } else if (!strcmp(mode, "singleton-middle") || !strcmp(mode, "cursor-other")) {
        rel_ids[0] = 2; rel_ids[1] = 1;
        page_ids[0] = 2; page_ids[1] = 1;
        cursor_other = !strcmp(mode, "cursor-other");
    } else if (!strcmp(mode, "singleton-tail")) {
        rel_ids[0] = 2; rel_ids[1] = 3; rel_ids[2] = 1;
        page_ids[0] = 2; page_ids[1] = 3; page_ids[2] = 1;
    } else if (!strcmp(mode, "singleton-empty")) {
        object_count = relocation_count = page_count = 1;
    } else if (!strcmp(mode, "duplicate-head") || !strcmp(mode, "duplicate-tail") ||
               !strcmp(mode, "threaded-duplicate")) {
        relocation_count = 4;
        if (!strcmp(mode, "duplicate-head")) {
            rel_ids[0] = 1; rel_ids[1] = 1; rel_ids[2] = 2; rel_ids[3] = 3;
        } else {
            rel_ids[3] = 1;
            threads = !strcmp(mode, "threaded-duplicate");
        }
    } else if (!strcmp(mode, "multi-candidate") || !strcmp(mode, "multi-cleared-flags") ||
               !strcmp(mode, "multi-clear-page-flag") ||
               !strcmp(mode, "multi-no-candidate") || !strcmp(mode, "multi-source-tail")) {
        object_count = 4;
        if (!strcmp(mode, "multi-no-candidate") || !strcmp(mode, "multi-source-tail")) {
            rel_ids[1] = 3; rel_ids[2] = 4;
        }
        page_flag_clear = !strcmp(mode, "multi-clear-page-flag");
    } else if (!strcmp(mode, "stale-nonlinked") || !strcmp(mode, "threaded-stale")) {
        object_count = 4; relocation_count = 4;
        rel_ids[1] = 2; rel_ids[2] = 3; rel_ids[3] = 4;
        threads = !strcmp(mode, "threaded-stale");
    } else if (!strcmp(mode, "threaded")) threads = 1;
    else if (!strcmp(mode, "payload-failure")) payload_fail = 1;
    else if (!strcmp(mode, "forwarding-failure")) forwarding_fail = 1;
    else if (!strcmp(mode, "missing-source")) { rel_ids[0] = 2; rel_ids[1] = 3; relocation_count = 2; }
    else if (!strcmp(mode, "known-size-zero")) known_from = 0;
    else if (!strcmp(mode, "known-size-small")) known_from = SIZE - 1;
    else if (!strcmp(mode, "target-size-small")) known_to = SIZE - 1;
    else if (!strcmp(mode, "pinned")) pinned = 1;
    else if (!strcmp(mode, "unsupported")) unsupported = 1;
    else if (!strcmp(mode, "source-no-index")) from_index_missing = 1;
    else if (!strcmp(mode, "source-null-page")) null_page = 1;
    else if (!strcmp(mode, "scale")) {
        object_count = relocation_count = page_count = 65;
        for (int i = 0; i < object_count; i++) rel_ids[i] = page_ids[i] = i + 1;
    } else if (strcmp(mode, "pinned-flag") && strcmp(mode, "already-forwarded") &&
               strcmp(mode, "page-unselected") && strcmp(mode, "wrong-backend")) return 0;
    return 1;
}

static void print_chain(const char *name, void *head, Entry *entries, int count, int values) {
    printf("\"%s\":[", name);
    int n = 0;
    for (void *p = head; p; p = ptr(p, 8)) {
        if (n++ >= CAP) { stub_errors++; break; }
        if (n > 1) printf(",");
        printf("%d", values == 1 ? object_id(ptr(p, 0)) :
            values == 2 ? page_id(ptr(p, 0)) : entry_id(p, entries, count));
    }
    printf("],");
}


/* State-machine checks call the actual emitted index, including its generic
 * rehash/backshift implementation. Raw slot writes only inject impossible-to-
 * allocate overflow or deliberately corrupt state for fail-closed checks. */
static int membership_state_case(const char *mode) {
    static uint64_t keys[4096];
    if (!strcmp(mode, "member-counts")) {
        int64_t counts[7];
        counts[0] = pcc_gc_relocation_member_count(&objects[0]);
        int maybe_empty = (int)pcc_gc_relocation_member_maybe(&objects[0]);
        pcc_gc_relocation_member_note(&objects[0]);
        pcc_gc_relocation_member_note(&objects[0]);
        pcc_gc_relocation_member_note(&objects[0]);
        pcc_gc_relocation_member_note(&objects[1]);
        counts[1] = pcc_gc_relocation_member_count(&objects[0]);
        w32(&objects[0], 12, 2048); w32(&objects[0], 12, 0);
        counts[2] = pcc_gc_relocation_member_count(&objects[0]);
        int maybe_present = (int)pcc_gc_relocation_member_maybe(&objects[0]);
        pcc_gc_relocation_member_forget(&objects[0]);
        counts[3] = pcc_gc_relocation_member_count(&objects[0]);
        /* Model caller-owned reset pops. The managed reset itself is outside
         * this two-module boundary; each actual pop calls the emitted forget. */
        for (int i = 0; i < 3; i++) {
            wp(&relocation[i], 0, &objects[i == 1 ? 1 : 0]);
            wp(&relocation[i], 8, i < 2 ? &relocation[i + 1] : NULL);
        }
        void *head = &relocation[0];
        for (int i = 0; i < 3; i++) {
            void *popped = head, *owner = ptr(popped, 0);
            head = ptr(popped, 8); wp(popped, 8, NULL);
            pcc_gc_relocation_member_forget(owner);
            counts[4 + i] = pcc_gc_relocation_member_count(owner);
        }
        void *empty_table = pcc_py_gc_relocation_member_slots;
        int retained_before_clear = empty_table != NULL && pcc_py_gc_relocation_member_cap > 0;
        pcc_gc_relocation_member_clear();
        printf("{\"counts\":[%lld,%lld,%lld,%lld,%lld,%lld,%lld],\"maybe\":[%d,%d,%lld],",
            (long long)counts[0], (long long)counts[1], (long long)counts[2],
            (long long)counts[3], (long long)counts[4], (long long)counts[5],
            (long long)counts[6], maybe_empty, maybe_present,
            (long long)pcc_gc_relocation_member_maybe(&objects[0]));
        printf("\"retained_before_clear\":%d,\"same_empty_table\":%d,",
            retained_before_clear, empty_table == pcc_py_gc_relocation_member_slots);
    } else if (!strcmp(mode, "member-growth")) {
        int values_ok = 1;
        for (int i = 0; i < 300; i++) {
            pcc_gc_relocation_member_note(&keys[i]);
            if (i % 7 == 0) pcc_gc_relocation_member_note(&keys[i]);
        }
        int64_t grown_cap = pcc_py_gc_relocation_member_cap;
        for (int i = 0; i < 300; i++) {
            if (pcc_gc_relocation_member_count(&keys[i]) != (i % 7 == 0 ? 2 : 1)) values_ok = 0;
            pcc_gc_relocation_member_forget(&keys[i]);
        }
        int64_t after_once = pcc_py_gc_relocation_member_count;
        for (int i = 0; i < 300; i += 7) pcc_gc_relocation_member_forget(&keys[i]);
        void *empty_table = pcc_py_gc_relocation_member_slots;
        int retained_before_clear = empty_table != NULL && pcc_py_gc_relocation_member_cap > 0;
        pcc_gc_relocation_member_clear();
        printf("{\"grown_cap\":%lld,\"after_once\":%lld,\"values_ok\":%d,\"same_empty_table\":%d,\"retained_before_clear\":%d,\"peak_bytes\":%zu,",
            (long long)grown_cap, (long long)after_once, values_ok,
            empty_table == pcc_py_gc_relocation_member_slots, retained_before_clear, peak_bytes);
    } else if (!strcmp(mode, "member-collisions")) {
        void *collisions[4]; int found = 0;
        /* These APIs hash/compare keys without dereferencing them. Use fixed,
         * non-null, aligned opaque identities: an ASLR-positioned array's
         * 4096-address window need not contain four keys in bucket 255.
         * Keep selection through the emitted hash and test the real table. */
        for (uintptr_t raw = 8; raw <= 4096 * 8 && found < 4; raw += 8) {
            void *key = (void *)raw;
            if (((uint64_t)pcc_gc_index_py_hash_ptr(key) & 255) == 255)
                collisions[found++] = key;
        }
        if (found != 4) { fprintf(stderr, "could not construct collision cluster\n"); return 3; }
        for (int i = 0; i < 4; i++) pcc_gc_relocation_member_note(collisions[i]);
        pcc_gc_relocation_member_note(collisions[2]); pcc_gc_relocation_member_note(collisions[2]);
        int64_t before[4], shifted[3];
        for (int i = 0; i < 4; i++) before[i] = pcc_gc_index_py_find_slot(
            pcc_py_gc_relocation_member_slots, pcc_py_gc_relocation_member_cap, collisions[i]);
        pcc_gc_relocation_member_forget(collisions[0]);
        for (int i = 0; i < 3; i++) shifted[i] = pcc_gc_index_py_find_slot(
            pcc_py_gc_relocation_member_slots, pcc_py_gc_relocation_member_cap, collisions[i + 1]);
        int64_t moved_count = pcc_gc_relocation_member_count(collisions[2]);
        pcc_gc_relocation_member_forget(collisions[2]);
        int64_t decremented = pcc_gc_relocation_member_count(collisions[2]);
        pcc_gc_relocation_member_forget(collisions[1]);
        pcc_gc_relocation_member_forget(collisions[2]); pcc_gc_relocation_member_forget(collisions[2]);
        int64_t last_slot = pcc_gc_index_py_find_slot(
            pcc_py_gc_relocation_member_slots, pcc_py_gc_relocation_member_cap, collisions[3]);
        int64_t last_count = pcc_gc_relocation_member_count(collisions[3]);
        pcc_gc_relocation_member_forget(collisions[3]); pcc_gc_relocation_member_clear();
        printf("{\"before\":[%lld,%lld,%lld,%lld],\"shifted\":[%lld,%lld,%lld],\"moved_count\":%lld,\"decremented\":%lld,\"last_slot\":%lld,\"last_count\":%lld,",
            (long long)before[0], (long long)before[1], (long long)before[2], (long long)before[3],
            (long long)shifted[0], (long long)shifted[1], (long long)shifted[2],
            (long long)moved_count, (long long)decremented, (long long)last_slot, (long long)last_count);
    } else if (!strcmp(mode, "member-initial-failure")) {
        fail_calloc = 1; pcc_gc_relocation_member_note(&keys[0]); fail_calloc = 0;
        int64_t unknown = pcc_gc_relocation_member_count(&keys[0]);
        int64_t absent_unknown = pcc_gc_relocation_member_count(&keys[1]);
        pcc_gc_relocation_member_note(&keys[1]); pcc_gc_relocation_member_forget(&keys[0]);
        int attempts_before_clear = calloc_calls;
        int64_t maybe_unknown = pcc_gc_relocation_member_maybe(&keys[1]);
        pcc_gc_relocation_member_clear();
        int64_t recovered_empty = pcc_gc_relocation_member_count(&keys[0]);
        pcc_gc_relocation_member_note(&keys[0]);
        int64_t recovered_present = pcc_gc_relocation_member_count(&keys[0]);
        pcc_gc_relocation_member_forget(&keys[0]); pcc_gc_relocation_member_clear();
        printf("{\"unknown\":%lld,\"absent_unknown\":%lld,\"maybe_unknown\":%lld,\"attempts_before_clear\":%d,\"recovered_empty\":%lld,\"recovered_present\":%lld,",
            (long long)unknown, (long long)absent_unknown, (long long)maybe_unknown,
            attempts_before_clear, (long long)recovered_empty, (long long)recovered_present);
    } else if (!strcmp(mode, "member-growth-failure")) {
        for (int i = 0; i < 128; i++) pcc_gc_relocation_member_note(&keys[i]);
        fail_calloc = 1; pcc_gc_relocation_member_note(&keys[128]); fail_calloc = 0;
        int64_t unknown = pcc_gc_relocation_member_count(&keys[0]);
        for (int i = 0; i < 129; i++) pcc_gc_relocation_member_forget(&keys[i]);
        pcc_gc_relocation_member_note(&keys[129]);
        int64_t stale_keys = pcc_py_gc_relocation_member_count;
        pcc_gc_relocation_member_clear(); /* The modeled list is now empty. */
        int cleanup_frees = free_calls;
        int64_t cleanup_cap = pcc_py_gc_relocation_member_cap;
        pcc_gc_relocation_member_note(&keys[0]);
        int64_t recovered_present = pcc_gc_relocation_member_count(&keys[0]);
        pcc_gc_relocation_member_forget(&keys[0]); pcc_gc_relocation_member_clear();
        printf("{\"unknown\":%lld,\"stale_keys\":%lld,\"cleanup_frees\":%d,\"cleanup_cap\":%lld,\"recovered_present\":%lld,",
            (long long)unknown, (long long)stale_keys, cleanup_frees,
            (long long)cleanup_cap, (long long)recovered_present);
    } else if (!strcmp(mode, "member-overflow-invalid")) {
        pcc_gc_relocation_member_note(&keys[0]);
        int64_t slot = pcc_gc_index_py_find_slot(pcc_py_gc_relocation_member_slots,
            pcc_py_gc_relocation_member_cap, &keys[0]);
        w64(pcc_py_gc_relocation_member_slots, (int)(slot * 16 + 8), INT64_MAX);
        int64_t limit = pcc_gc_relocation_member_count(&keys[0]);
        pcc_gc_relocation_member_note(&keys[0]);
        int64_t overflow = pcc_gc_relocation_member_count(&keys[0]);
        pcc_gc_relocation_member_clear();
        pcc_gc_relocation_member_note(&keys[0]);
        slot = pcc_gc_index_py_find_slot(pcc_py_gc_relocation_member_slots,
            pcc_py_gc_relocation_member_cap, &keys[0]);
        w64(pcc_py_gc_relocation_member_slots, (int)(slot * 16 + 8), 0);
        int64_t invalid = pcc_gc_relocation_member_count(&keys[0]);
        pcc_gc_relocation_member_clear();
        pcc_gc_relocation_member_forget(&keys[0]);
        int64_t missing_forget = pcc_gc_relocation_member_count(&keys[0]);
        pcc_gc_relocation_member_clear();
        int64_t null_count = pcc_gc_relocation_member_count(NULL);
        int64_t tagged_count = pcc_gc_relocation_member_count((void *)((uintptr_t)&keys[0] | 1));
        pcc_gc_relocation_member_note(NULL);
        int64_t invalid_note = pcc_gc_relocation_member_count(&keys[0]);
        pcc_gc_relocation_member_clear();
        printf("{\"limit\":%lld,\"overflow\":%lld,\"invalid_value\":%lld,\"missing_forget\":%lld,\"null_count\":%lld,\"tagged_count\":%lld,\"invalid_note\":%lld,",
            (long long)limit, (long long)overflow, (long long)invalid,
            (long long)missing_forget, (long long)null_count, (long long)tagged_count, (long long)invalid_note);
    } else return 2;
    printf("\"keys\":%lld,\"used\":%lld,\"cap\":%lld,\"incomplete\":%lld,\"calloc_calls\":%d,\"free_calls\":%d,\"stub_errors\":%d}\n",
        (long long)pcc_py_gc_relocation_member_count, (long long)pcc_py_gc_relocation_member_used,
        (long long)pcc_py_gc_relocation_member_cap, (long long)pcc_py_gc_relocation_member_incomplete,
        calloc_calls, free_calls, stub_errors);
    return stub_errors ? 3 : 0;
}

int main(int argc, char **argv) {
    if (argc != 2) return 2;
    if (!strncmp(argv[1], "member-", 7)) return membership_state_case(argv[1]);
    const char *mode = argv[1];
    int corrupt_local = 0, bad_owner_index = 0;
    if (!strcmp(mode, "incomplete-head")) { mode = "singleton-head"; fail_calloc = 1; }
    else if (!strcmp(mode, "incomplete-duplicate")) { mode = "duplicate-tail"; fail_calloc = 1; }
    else if (!strcmp(mode, "incomplete-multi")) { mode = "multi-cleared-flags"; fail_calloc = 1; }
    else if (!strcmp(mode, "incomplete-stale")) { mode = "stale-nonlinked"; fail_calloc = 1; }
    else if (!strcmp(mode, "invalid-local-count")) { mode = "multi-cleared-flags"; corrupt_local = 1; }
    else if (!strcmp(mode, "owner-no-index")) { mode = "multi-cleared-flags"; bad_owner_index = 1; }
    else if (!strcmp(mode, "owner-other-page")) { mode = "multi-cleared-flags"; bad_owner_index = 2; }
    else if (!strcmp(mode, "duplicate-payload-failure")) { mode = "duplicate-tail"; payload_fail = 1; }
    if (!configure(mode)) return 2;
    for (int i = 0; i < object_count; i++) {
        w64(&objects[i], 0, i == 0 ? 7 : 1);
        w32(&objects[i], 8, 2); w32(&objects[i], 12, i == 0 ? SRC_FLAGS : 0);
        for (int k = 16; k < SIZE; k += 8) w64(&objects[i], k, 1000 + i * 100 + k);
        wp(&znodes[i], 0, &objects[i]);
        wp(&znodes[i], 8, &pages[i % page_count]);
        w64(&znodes[i], 32, SIZE);
        index_map[i] = &znodes[i];
        wp(&znodes[i], 16, global_zhead); global_zhead = &znodes[i];
        wp(&znodes[i], 48, ptr(&pages[i % page_count], 112));
        wp(&pages[i % page_count], 112, &znodes[i]);
    }
    if (!strncmp(mode, "multi-", 6)) {
        /* The read barrier may clear RELOCATE_CANDIDATE (2048) while the
         * owner remains in the relocation list. Membership must win. */
        w32(&objects[1], 12, !strcmp(mode, "multi-candidate") ? 2048 : 0);
        for (int i = 0; i < page_count; i++) wp(&pages[i], 112, NULL);
        wp(&znodes[0], 8, &pages[0]); wp(&znodes[1], 8, &pages[0]);
        wp(&znodes[2], 8, &pages[1]); wp(&znodes[3], 8, &pages[2]);
        wp(&pages[1], 112, &znodes[2]); wp(&pages[2], 112, &znodes[3]);
        wp(&znodes[2], 48, NULL); wp(&znodes[3], 48, NULL);
        if (!strcmp(mode, "multi-source-tail")) {
            wp(&pages[0], 112, &znodes[1]); wp(&znodes[1], 48, &znodes[0]); wp(&znodes[0], 48, NULL);
        } else {
            wp(&pages[0], 112, &znodes[0]); wp(&znodes[0], 48, &znodes[1]); wp(&znodes[1], 48, NULL);
        }
    }
    if (!strcmp(mode, "stale-nonlinked") || !strcmp(mode, "threaded-stale")) {
        /* Owners 2/3 occur in the relocation set but have no live index entry.
         * A stale global znode points at the source page but is not in its
         * page-local owner chain. This is not another live source-page owner.
         */
        index_map[1] = index_map[2] = NULL;
        wp(&znodes[3], 8, &pages[2]); wp(&znodes[3], 48, NULL);
        wp(&pages[2], 112, &znodes[3]);
        wp(&pages[0], 112, &znodes[0]); wp(&znodes[0], 48, NULL);
        wp(&stale_global_node, 0, &objects[2]); wp(&stale_global_node, 8, &pages[0]);
        wp(&stale_global_node, 16, global_zhead); global_zhead = &stale_global_node;
    }
    if (from_index_missing) index_map[0] = NULL;
    if (null_page) { wp(&znodes[0], 8, NULL); wp(&pages[0], 112, NULL); }
    if (!strcmp(mode, "pinned-flag")) w32(&objects[0], 12, SRC_FLAGS | 524288);
    if (!strcmp(mode, "already-forwarded")) installed_forwarding = &target;
    w64(&target, 0, 99); w32(&target, 8, 2); w32(&target, 12, 64 | 65536);
    for (int k = 16; k < SIZE; k += 8) w64(&target, k, 9000 + k);
    pcc_gc_backend_selected = !strcmp(mode, "wrong-backend") ? 3 : 4;
    pcc_gc_backend4_evacuated_bytes_count = 11;
    pcc_gc_backend4_reseed_relocation_revision = 37;
    pcc_gc_backend4_reseed_page_revision = 73;
    pcc_gc_backend4_reseed_commit_owner = 0;
    py_class_attr_cache_epoch = 23;
    for (int i = 0; i < relocation_count; i++) {
        wp(&relocation[i], 0, &objects[rel_ids[i] - 1]);
        wp(&relocation[i], 8, i + 1 < relocation_count ? &relocation[i + 1] : NULL);
        if (rel_ids[i] == 1 && !pcc_gc_backend4_reseed_relocation_cursor)
            pcc_gc_backend4_reseed_relocation_cursor = (uintptr_t)&relocation[i];
    }
    for (int i = 0; i < page_count; i++) {
        wp(&selected_pages[i], 0, &pages[page_ids[i] - 1]);
        wp(&selected_pages[i], 8, i + 1 < page_count ? &selected_pages[i + 1] : NULL);
        w32(&pages[i], 108, 1);
        if (page_ids[i] == 1) pcc_gc_backend4_reseed_page_count_cursor = (uintptr_t)&selected_pages[i];
    }
    pcc_gc_relocation_set_head = (uintptr_t)&relocation[0];
    pcc_gc_backend4_evacuation_page_head = (uintptr_t)&selected_pages[0];
    if (cursor_other) {
        pcc_gc_backend4_reseed_relocation_cursor = (uintptr_t)&relocation[0];
        pcc_gc_backend4_reseed_page_count_cursor = (uintptr_t)&selected_pages[0];
    }
    if (page_flag_clear) w32(&pages[0], 108, 0);
    if (!strcmp(mode, "page-unselected")) {
        pcc_gc_backend4_evacuation_page_head = (uintptr_t)&selected_pages[1];
        pcc_gc_backend4_reseed_page_count_cursor = (uintptr_t)&selected_pages[1];
        w32(&pages[0], 108, 0);
    }
    if (bad_owner_index == 1) index_map[1] = NULL;
    if (bad_owner_index == 2) {
        wp(&stale_global_node, 0, &objects[1]); wp(&stale_global_node, 8, &pages[1]);
        index_map[1] = &stale_global_node;
    }
    initialize_backlinks();
    for (int i = 0; i < relocation_count; i++)
        pcc_gc_relocation_member_note(ptr(&relocation[i], 0));
    member_before = pcc_gc_relocation_member_count(&objects[0]);
    if (corrupt_local) {
        int64_t slot = pcc_gc_index_py_find_slot(pcc_py_gc_relocation_member_slots,
            pcc_py_gc_relocation_member_cap, &objects[1]);
        if (slot < 0) return 3;
        w64(pcc_py_gc_relocation_member_slots, (int)(slot * 16 + 8), 0);
    }
    fail_calloc = 0;
    calloc_calls = free_calls = 0; /* Only commit-phase allocation/free counts. */
    finish_plan[0] = finish_plan[1] = finish_plan[2] = UINT64_C(0x12345678);
    void *result = pcc_gc_backend4_relocate_copy_preallocated_unlocked(
        &objects[0], SIZE, &target, &payload_token, &forwarding_token, finish_plan);
    memcpy(saved_finish, finish_plan, sizeof saved_finish);
    int second = -1, second_clear = -1;
    if (reenter) {
        second = pcc_gc_backend4_relocate_copy_preallocated_unlocked(
            &objects[0], SIZE, &target, &payload_token, &forwarding_token, finish_plan) != NULL;
        second_clear = finish_plan[0] == 0 && finish_plan[1] == 0 && finish_plan[2] == 0;
    }
    printf("{\"result\":%d,\"second\":%d,\"second_clear\":%d,", result == &target, second, second_clear);
    print_chain("rel_nodes", (void *)(uintptr_t)pcc_gc_relocation_set_head, relocation, relocation_count, 0);
    print_chain("rel_owners", (void *)(uintptr_t)pcc_gc_relocation_set_head, relocation, relocation_count, 1);
    print_chain("page_nodes", (void *)(uintptr_t)pcc_gc_backend4_evacuation_page_head, selected_pages, page_count, 0);
    print_chain("pages", (void *)(uintptr_t)pcc_gc_backend4_evacuation_page_head, selected_pages, page_count, 2);
    int detached_rel = entry_id((void *)(uintptr_t)saved_finish[0], relocation, relocation_count);
    int detached_page = entry_id((void *)(uintptr_t)saved_finish[1], selected_pages, page_count);
    printf("\"finish\":[%d,%d,%d],\"finish_next_clear\":%d,", detached_rel, detached_page,
        znode_id((void *)(uintptr_t)saved_finish[2]),
        (!detached_rel || ptr(&relocation[detached_rel - 1], 8) == NULL) &&
        (!detached_page || ptr(&selected_pages[detached_page - 1], 8) == NULL));
    printf("\"rel_cursor\":%d,\"page_cursor\":%d,\"rel_revision\":%llu,\"page_revision\":%llu,",
        entry_id((void *)(uintptr_t)pcc_gc_backend4_reseed_relocation_cursor, relocation, relocation_count),
        entry_id((void *)(uintptr_t)pcc_gc_backend4_reseed_page_count_cursor, selected_pages, page_count),
        (unsigned long long)pcc_gc_backend4_reseed_relocation_revision,
        (unsigned long long)pcc_gc_backend4_reseed_page_revision);
    printf("\"page_flag\":%u,\"evacuated\":%llu,\"source_flags\":%u,\"target_flags\":%u,",
        u32(&pages[0], 108), (unsigned long long)pcc_gc_backend4_evacuated_bytes_count,
        u32(&objects[0], 12), u32(&target, 12));
    printf("\"source_refs\":%llu,\"target_refs\":%llu,\"payload_copied\":%d,\"forwarded\":%d,",
        (unsigned long long)u64(&objects[0], 0), (unsigned long long)u64(&target, 0),
        !memcmp((char *)&objects[0] + 16, (char *)&target + 16, SIZE - 16), installed_forwarding == &target);
    printf("\"source_page_head\":%d,\"find_calls\":%d,\"detach_calls\":%d,\"payload_calls\":%d,\"install_calls\":%d,\"forwarding_calls\":%d,",
        znode_id(ptr(&pages[0], 112)), find_calls, detach_calls, payload_calls, install_calls, forwarding_calls);
    printf("\"other_flags\":%u,", u32(&objects[1], 12));
    printf("\"member_before\":%lld,\"member_after\":%lld,\"member_other\":%lld,",
        (long long)member_before, (long long)pcc_gc_relocation_member_count(&objects[0]),
        (long long)pcc_gc_relocation_member_count(&objects[1]));
    printf("\"member_keys\":%lld,\"member_cap\":%lld,\"member_incomplete\":%lld,\"calloc_calls\":%d,",
        (long long)pcc_py_gc_relocation_member_count,
        (long long)pcc_py_gc_relocation_member_cap,
        (long long)pcc_py_gc_relocation_member_incomplete, calloc_calls);
    printf("\"cache_epoch\":%llu,\"free_calls\":%d,\"finish_calls\":%d,\"unexpected_calls\":%d,\"stub_errors\":%d}\n",
        (unsigned long long)py_class_attr_cache_epoch, free_calls, finish_calls, unexpected_calls, stub_errors);
    return stub_errors || calloc_calls || free_calls || finish_calls || unexpected_calls ? 3 : 0;
}
