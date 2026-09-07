/* pcc/py_runtime/src/pcc_threads.c
 *
 * Host-C oracle for the shared threading substrate. Production pcc-Python
 * archives own this ABI in freestanding_thread_kernel.py (default mode) or
 * freestanding_thread_kernel_pthread.py (PCC_WITH_THREADS=1); this source is
 * retained for differential and C-runtime tests, not linked into that archive.
 *
 * Default builds are deliberately single-threaded. Build with
 * PCC_WITH_THREADS=1 to enable the pthread-backed wrappers. Atomic
 * refcount can also be selected independently with
 * PCC_REFCOUNT_STRATEGY=PCC_REFCOUNT_KIND_ATOMIC.
 */

#include "py_internal.h"
#include "py_io_waitset.h"
#include "py_timer_heap.h"
#include <errno.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <poll.h>
#include <sys/time.h>
#if defined(__APPLE__) || defined(__linux__)
#include <execinfo.h>
#endif

/* ``pcc_gc_thread_unregister_buffers`` is defined for real in
 * ``py_gc_backend.c``. The previous fallback here was
 * ``__attribute__((weak)) void pcc_gc_thread_unregister_buffers(void)
 * {}``, which let system clang/gcc dedupe against the strong
 * definition. pcc's own C frontend doesn't lower the ``weak``
 * attribute (yet), so under pcc's libpy_runtime_pcc.a build the
 * placeholder + real definition collided as duplicate strong
 * symbols. Both ``pcc_threads.c`` and ``py_gc_backend.c`` are always
 * compiled into every archive variant, so an ``extern`` declaration
 * is sufficient: the call at the bottom of this file resolves to
 * the real definition via the linker. */
extern void pcc_gc_thread_unregister_buffers(void);

#ifndef PCC_WITH_THREADS
#define PCC_WITH_THREADS 0
#endif

#ifndef PCC_REFCOUNT_STRATEGY
#if PCC_WITH_THREADS
#define PCC_REFCOUNT_STRATEGY PCC_REFCOUNT_KIND_ATOMIC
#else
#define PCC_REFCOUNT_STRATEGY PCC_REFCOUNT_KIND_NONATOMIC
#endif
#endif

#if PCC_REFCOUNT_STRATEGY < PCC_REFCOUNT_KIND_NONATOMIC \
    || PCC_REFCOUNT_STRATEGY > PCC_REFCOUNT_KIND_DEFERRED
#error "PCC_REFCOUNT_STRATEGY must be NONATOMIC, ATOMIC, BIASED, or DEFERRED."
#endif

int64_t pcc_threads_enabled(void) {
    return PCC_WITH_THREADS ? 1 : 0;
}

int64_t pcc_refcount_strategy(void) {
    return (int64_t)PCC_REFCOUNT_STRATEGY;
}

/* Generated loop/function safepoints poll this global directly and only call
 * pcc_thread_safepoint() on the slow path.  Keep it exported even when
 * PCC_WITH_THREADS=0 so the same generated IR links against threaded and
 * non-threaded runtime archives. */
int32_t pcc_thread_stop_requested = 0;

int64_t pcc_thread_stop_requested_acquire(void) {
#if PCC_WITH_THREADS
    return __atomic_load_n(
        &pcc_thread_stop_requested, __ATOMIC_ACQUIRE
    ) != 0 ? 1 : 0;
#else
    return 0;
#endif
}

/* A no-park region is a small, bounded native critical section whose caller
 * may retain an owner-derived raw pointer.  The depth is TLS in both the
 * pthread and single-threaded variants: single-threaded builds still need to
 * reject recursive collector entry from the current native stack. */
static _Thread_local int64_t pcc_tls_no_park_depth = 0;

/* Native-thread identity must stay distinct even in PCC_WITH_THREADS=0
 * archives: C extensions and embedding hosts may still call lock-protected
 * runtime ABIs from raw pthreads.  The address of one C11 TLS byte is a
 * process-local identity token for the lifetime of the native thread. */
static _Thread_local unsigned char pcc_native_thread_identity_token = 0;

void *pcc_current_native_thread_token(void) {
    return (void *)&pcc_native_thread_identity_token;
}

#if PCC_REFCOUNT_STRATEGY == PCC_REFCOUNT_KIND_BIASED \
    || PCC_REFCOUNT_STRATEGY == PCC_REFCOUNT_KIND_DEFERRED
/* Refcount strategy metadata.
 *
 * BIASED and DEFERRED are implemented without changing the public object
 * header in this slice.  The metadata is keyed by the address of the
 * refcount slot; py_decref calls pcc_refcount_forget() before storage is
 * released so a later malloc reuse cannot inherit stale owner state.
 *
 * BIASED keeps the PEP-703 shape (owner thread + local/shared counters).
 * A small spin lock protects the side table; this favours correctness and
 * ThreadSanitizer cleanliness over the final no-lock fast path.  A future
 * ABI migration can move owner/local/shared into PyObjectHeader and remove
 * this lock from same-owner INCREF/DECREF.
 *
 * DEFERRED keeps an explicit pending counter.  The synchronous dealloc
 * contract still requires exact zero detection, so decref flushes before it
 * returns; increments may batch until the next decrement/forget/flush. */
typedef struct PccRefcountMeta {
    int64_t *slot;
    int64_t owner_tid;
    int64_t local;
    int64_t shared;
    int64_t pending;
    struct PccRefcountMeta *next;
} PccRefcountMeta;

static PccRefcountMeta *pcc_refmeta_head = NULL;
static unsigned char pcc_refmeta_lock_word = 0;

static void pcc_refmeta_lock(void) {
    while (__atomic_test_and_set(&pcc_refmeta_lock_word, __ATOMIC_ACQUIRE)) {
        pcc_thread_safepoint();
    }
}

static void pcc_refmeta_unlock(void) {
    __atomic_clear(&pcc_refmeta_lock_word, __ATOMIC_RELEASE);
}

static PccRefcountMeta *pcc_refmeta_find_locked(
    int64_t *slot,
    int create,
    int64_t owner_tid
) {
    for (PccRefcountMeta *m = pcc_refmeta_head; m != NULL; m = m->next) {
        if (m->slot == slot) return m;
    }
    if (!create) return NULL;
    PccRefcountMeta *m = (PccRefcountMeta *)calloc(1, sizeof(PccRefcountMeta));
    if (m == NULL) return NULL;
    m->slot = slot;
    m->owner_tid = owner_tid;
    m->local = __atomic_load_n(slot, __ATOMIC_ACQUIRE);
    m->shared = 0;
    m->pending = 0;
    m->next = pcc_refmeta_head;
    pcc_refmeta_head = m;
    return m;
}

static int64_t pcc_refmeta_sync_locked(PccRefcountMeta *m) {
    if (m == NULL || m->slot == NULL) return 0;
    int64_t total = m->local + m->shared + m->pending;
    if (total < 0) total = 0;
    m->local = total;
    m->shared = 0;
    m->pending = 0;
    __atomic_store_n(m->slot, total, __ATOMIC_RELEASE);
    return total;
}

static int64_t pcc_refcount_biased_delta(int64_t *slot, int64_t delta) {
    if (slot == NULL) return 0;
    /* Registration may wait for an active stopped-world epoch.  It must not
     * do so while holding the refcount metadata lock. */
    int64_t self = pcc_current_thread_id();
    pcc_refmeta_lock();
    PccRefcountMeta *m = pcc_refmeta_find_locked(slot, 1, self);
    if (m == NULL) {
        pcc_refmeta_unlock();
        return __atomic_add_fetch(slot, delta, __ATOMIC_ACQ_REL);
    }
    if (m->owner_tid == 0) m->owner_tid = self;
    if (m->owner_tid == self) {
        m->local += delta;
    } else {
        m->shared += delta;
    }
    int64_t total = pcc_refmeta_sync_locked(m);
    pcc_refmeta_unlock();
    return total;
}

static int64_t pcc_refcount_deferred_delta(int64_t *slot, int64_t delta) {
    if (slot == NULL) return 0;
    /* See pcc_refcount_biased_delta: never wait for newcomer admission while
     * owning the metadata lock. */
    int64_t self = pcc_current_thread_id();
    pcc_refmeta_lock();
    PccRefcountMeta *m = pcc_refmeta_find_locked(slot, 1, self);
    if (m == NULL) {
        pcc_refmeta_unlock();
        return __atomic_add_fetch(slot, delta, __ATOMIC_ACQ_REL);
    }
    m->pending += delta;
    /* Decrements must report an exact count so py_decref can decide
     * whether to deallocate immediately.  Increments can batch, but a
     * flush threshold of 32 keeps observable refcount close and bounded. */
    if (delta < 0 || m->pending >= 32 || m->pending <= -32) {
        (void)pcc_refmeta_sync_locked(m);
    }
    int64_t total = m->local + m->shared + m->pending;
    if (delta < 0) total = pcc_refmeta_sync_locked(m);
    pcc_refmeta_unlock();
    return total < 0 ? 0 : total;
}
#endif

int64_t pcc_refcount_incref(int64_t *slot) {
    if (slot == NULL) return 0;
#if PCC_REFCOUNT_STRATEGY == PCC_REFCOUNT_KIND_ATOMIC
    return __atomic_add_fetch(slot, 1, __ATOMIC_RELAXED);
#elif PCC_REFCOUNT_STRATEGY == PCC_REFCOUNT_KIND_BIASED
    return pcc_refcount_biased_delta(slot, 1);
#elif PCC_REFCOUNT_STRATEGY == PCC_REFCOUNT_KIND_DEFERRED
    return pcc_refcount_deferred_delta(slot, 1);
#else
    *slot += 1;
    return *slot;
#endif
}

int64_t pcc_refcount_decref(int64_t *slot) {
    if (slot == NULL) return 0;
#if PCC_REFCOUNT_STRATEGY == PCC_REFCOUNT_KIND_ATOMIC
    return __atomic_sub_fetch(slot, 1, __ATOMIC_ACQ_REL);
#elif PCC_REFCOUNT_STRATEGY == PCC_REFCOUNT_KIND_BIASED
    return pcc_refcount_biased_delta(slot, -1);
#elif PCC_REFCOUNT_STRATEGY == PCC_REFCOUNT_KIND_DEFERRED
    return pcc_refcount_deferred_delta(slot, -1);
#else
    *slot -= 1;
    return *slot;
#endif
}

int64_t pcc_refcount_load(int64_t *slot) {
    if (slot == NULL) return 0;
#if PCC_REFCOUNT_STRATEGY == PCC_REFCOUNT_KIND_ATOMIC
    return __atomic_load_n(slot, __ATOMIC_ACQUIRE);
#elif PCC_REFCOUNT_STRATEGY == PCC_REFCOUNT_KIND_BIASED
    return pcc_refcount_biased_delta(slot, 0);
#elif PCC_REFCOUNT_STRATEGY == PCC_REFCOUNT_KIND_DEFERRED
    return pcc_refcount_deferred_delta(slot, 0);
#else
    return *slot;
#endif
}

void pcc_refcount_forget(int64_t *slot) {
    if (slot == NULL) return;
#if PCC_REFCOUNT_STRATEGY == PCC_REFCOUNT_KIND_BIASED \
    || PCC_REFCOUNT_STRATEGY == PCC_REFCOUNT_KIND_DEFERRED
    pcc_refmeta_lock();
    PccRefcountMeta **cur = &pcc_refmeta_head;
    while (*cur != NULL) {
        if ((*cur)->slot == slot) {
            PccRefcountMeta *dead = *cur;
            *cur = dead->next;
            free(dead);
            break;
        }
        cur = &(*cur)->next;
    }
    pcc_refmeta_unlock();
#else
    (void)slot;
#endif
}

#if PCC_WITH_THREADS

#include <errno.h>
#include <pthread.h>
#include <sched.h>

struct PccThreadHandle {
    pthread_t thread;
    pthread_mutex_t state_lock;
    int32_t done;
    int32_t detached;
    void *result;
};

struct PccMutex {
    pthread_mutex_t mutex;
};

struct PccCond {
    pthread_cond_t cond;
};

typedef struct {
    PccThreadMain entry;
    void *arg;
    PccThreadHandle *handle;
} PccThreadStart;

static pthread_mutex_t pcc_world_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t pcc_world_cond = PTHREAD_COND_INITIALIZER;
static int64_t pcc_next_thread_id = 1;
static int64_t pcc_live_thread_count = 0;
static int64_t pcc_parked_thread_count = 0;
static int64_t pcc_stop_owner_thread_id = 0;
static int64_t pcc_stop_epoch = 0;
static int64_t pcc_stop_depth = 0;
static int64_t pcc_registration_waiter_count = 0;
static _Thread_local int64_t pcc_tls_thread_id = 0;
static _Thread_local int32_t pcc_tls_thread_parked = 0;
static _Thread_local int64_t pcc_tls_parked_epoch = 0;
static _Thread_local int32_t pcc_tls_unregister_in_progress = 0;

void pcc_thread_unregister_current(void) {
    if (pcc_tls_no_park_depth != 0) {
        abort();
        return;
    }
    if (pcc_tls_unregister_in_progress) {
        abort();
        return;
    }
    if (pcc_tls_thread_id == 0) return;
    /* An STW owner must resume the epoch before it can leave the live set.
     * Check before buffer cleanup: cleanup can release references and reenter
     * runtime code, while clearing the owner would strand stop_requested. */
    if (pcc_thread_owns_stopped_world()) {
        abort();
        return;
    }
    pcc_tls_unregister_in_progress = 1;
    pcc_gc_thread_unregister_buffers();
    pthread_mutex_lock(&pcc_world_lock);
    /* Cleanup can decref and reenter.  Revalidate the teardown lease while
     * holding the same lock that protects stop ownership and live counts. */
    if (pcc_tls_no_park_depth != 0
        || (pcc_tls_thread_id != 0
            && pcc_thread_stop_requested
            && pcc_stop_owner_thread_id == pcc_tls_thread_id)) {
        pthread_mutex_unlock(&pcc_world_lock);
        abort();
        return;
    }
    if (pcc_tls_thread_id != 0) {
        if (pcc_tls_thread_parked) {
            pcc_tls_thread_parked = 0;
            if (pcc_tls_parked_epoch == pcc_stop_epoch
                && pcc_parked_thread_count > 0) {
                pcc_parked_thread_count--;
            }
            pcc_tls_parked_epoch = 0;
        }
        pcc_tls_thread_id = 0;
        if (pcc_live_thread_count > 0) pcc_live_thread_count--;
        pthread_cond_broadcast(&pcc_world_cond);
    }
    pthread_mutex_unlock(&pcc_world_lock);
    pcc_tls_unregister_in_progress = 0;
}

int64_t pcc_current_thread_id(void) {
    if (pcc_tls_thread_id != 0) return pcc_tls_thread_id;
    pthread_mutex_lock(&pcc_world_lock);
    /* A first-time raw/extension pthread is not part of the stopped epoch's
     * live count.  Admit it only after the owner resumes the world; otherwise
     * it could return into user code after the owner had already observed all
     * pre-existing live threads parked. */
    int waiting_for_admission = 0;
    while (pcc_tls_thread_id == 0 && pcc_thread_stop_requested) {
        if (!waiting_for_admission) {
            pcc_registration_waiter_count++;
            waiting_for_admission = 1;
        }
        pthread_cond_wait(&pcc_world_cond, &pcc_world_lock);
    }
    if (waiting_for_admission) {
        pcc_registration_waiter_count--;
    }
    if (pcc_tls_thread_id == 0) {
        pcc_tls_thread_id = pcc_next_thread_id++;
        pcc_live_thread_count++;
        pthread_cond_broadcast(&pcc_world_cond);
    }
    int64_t tid = pcc_tls_thread_id;
    pthread_mutex_unlock(&pcc_world_lock);
    return tid;
}

void pcc_thread_safepoint(void) {
    if (pcc_tls_no_park_depth != 0) return;
    int64_t self = pcc_current_thread_id();
    pthread_mutex_lock(&pcc_world_lock);
    while (pcc_thread_stop_requested && pcc_stop_owner_thread_id != self) {
        if (!pcc_tls_thread_parked
            || pcc_tls_parked_epoch != pcc_stop_epoch) {
            pcc_tls_thread_parked = 1;
            pcc_tls_parked_epoch = pcc_stop_epoch;
            pcc_parked_thread_count++;
            pthread_cond_broadcast(&pcc_world_cond);
        }
        pthread_cond_wait(&pcc_world_cond, &pcc_world_lock);
    }
    if (!pcc_thread_stop_requested && pcc_tls_thread_parked) {
        pcc_tls_thread_parked = 0;
        pcc_tls_parked_epoch = 0;
    }
    pthread_mutex_unlock(&pcc_world_lock);
}

int64_t pcc_stop_the_world(void) {
    int64_t self = pcc_current_thread_id();
    pthread_mutex_lock(&pcc_world_lock);
    /* Serialize concurrent STW requesters.  A requester that loses the
     * ownership race must participate in the current stop, otherwise the
     * owner waits forever for this live thread to park.  Once the current
     * owner resumes the world, the waiter acquires a fresh stop of its own.
     * This is the same contract as Go's worldsema: concurrent stop requests
     * are safe and each executes in turn. */
    while (pcc_thread_stop_requested
           && pcc_stop_owner_thread_id != self) {
        if (!pcc_tls_thread_parked
            || pcc_tls_parked_epoch != pcc_stop_epoch) {
            pcc_tls_thread_parked = 1;
            pcc_tls_parked_epoch = pcc_stop_epoch;
            pcc_parked_thread_count++;
            pthread_cond_broadcast(&pcc_world_cond);
        }
        pthread_cond_wait(&pcc_world_cond, &pcc_world_lock);
    }
    if (!pcc_thread_stop_requested && pcc_tls_thread_parked) {
        pcc_tls_thread_parked = 0;
        pcc_tls_parked_epoch = 0;
    }
    if (pcc_thread_stop_requested) {
        pcc_stop_depth++;
        pthread_mutex_unlock(&pcc_world_lock);
        return 0;
    }
    __atomic_store_n(
        &pcc_thread_stop_requested, 1, __ATOMIC_RELEASE
    );
    pcc_stop_owner_thread_id = self;
    pcc_stop_depth = 1;
    pcc_stop_epoch++;
    if (pcc_stop_epoch <= 0) pcc_stop_epoch = 1;
    pcc_parked_thread_count = 0;
    pthread_cond_broadcast(&pcc_world_cond);
    while (pcc_live_thread_count > 1
           && pcc_parked_thread_count < pcc_live_thread_count - 1) {
        pthread_cond_wait(&pcc_world_cond, &pcc_world_lock);
    }
    pthread_mutex_unlock(&pcc_world_lock);
    return 0;
}

int64_t pcc_resume_world(void) {
    int64_t self = pcc_current_thread_id();
    pthread_mutex_lock(&pcc_world_lock);
    if (!pcc_thread_stop_requested) {
        pthread_mutex_unlock(&pcc_world_lock);
        return -1;
    }
    if (pcc_stop_owner_thread_id != self) {
        pthread_mutex_unlock(&pcc_world_lock);
        return -1;
    }
    if (pcc_stop_depth > 1) {
        pcc_stop_depth--;
        pthread_mutex_unlock(&pcc_world_lock);
        return 0;
    }
    __atomic_store_n(
        &pcc_thread_stop_requested, 0, __ATOMIC_RELEASE
    );
    pcc_stop_owner_thread_id = 0;
    pcc_stop_depth = 0;
    pcc_parked_thread_count = 0;
    pthread_cond_broadcast(&pcc_world_cond);
    pthread_mutex_unlock(&pcc_world_lock);
    return 0;
}

int64_t pcc_thread_owns_stopped_world(void) {
    int64_t self = pcc_tls_thread_id;
    if (self == 0) return 0;
    pthread_mutex_lock(&pcc_world_lock);
    int64_t owns = pcc_thread_stop_requested
        && pcc_stop_owner_thread_id == self;
    pthread_mutex_unlock(&pcc_world_lock);
    return owns;
}

int64_t pcc_thread_registration_waiter_count(void) {
    /* The diagnostic is itself a runtime entry: an unregistered raw pthread
     * must not use it to run through an active stopped-world epoch. */
    (void)pcc_current_thread_id();
    pthread_mutex_lock(&pcc_world_lock);
    int64_t count = pcc_registration_waiter_count;
    pthread_mutex_unlock(&pcc_world_lock);
    return count;
}

/* Unlike the other handle users, the trampoline is part of thread teardown.
 * It must remain registered until the handle commit is complete, while also
 * staying able to park if another thread owns the handle lock. */
static void pcc_thread_handle_state_lock_for_teardown(PccThreadHandle *handle) {
    for (;;) {
        int rc = pthread_mutex_trylock(&handle->state_lock);
        if (rc == 0) return;
        if (rc != EBUSY) {
            abort();
            return;
        }
        pcc_thread_safepoint();
        sched_yield();
    }
}

static void *pcc_thread_trampoline(void *opaque) {
    PccThreadStart *start = (PccThreadStart *)opaque;
    PccThreadMain entry = start->entry;
    void *arg = start->arg;
    PccThreadHandle *handle = start->handle;
    free(start);

    (void)pcc_current_thread_id();
    pcc_thread_safepoint();
    void *result = entry(arg);
    int should_free = 0;
    pcc_thread_handle_state_lock_for_teardown(handle);
    handle->result = result;
    handle->done = 1;
    should_free = handle->detached;
    if (pthread_mutex_unlock(&handle->state_lock) != 0) {
        abort();
        return result;
    }
    if (should_free) {
        pthread_mutex_destroy(&handle->state_lock);
        free(handle);
    }
    /* Keep this the final runtime action: any lock/safepoint after teardown
     * could register the pthread again and leak it from the live count. */
    pcc_thread_unregister_current();
    return result;
}

int64_t pcc_thread_start(
    PccThreadHandle **out,
    PccThreadMain entry,
    void *arg
) {
    if (out == NULL || entry == NULL) return -1;
    PccThreadHandle *handle = (PccThreadHandle *)calloc(1, sizeof(PccThreadHandle));
    if (handle == NULL) return -1;
    if (pthread_mutex_init(&handle->state_lock, NULL) != 0) {
        free(handle);
        return -1;
    }
    PccThreadStart *start = (PccThreadStart *)calloc(1, sizeof(PccThreadStart));
    if (start == NULL) {
        pthread_mutex_destroy(&handle->state_lock);
        free(handle);
        return -1;
    }
    start->entry = entry;
    start->arg = arg;
    start->handle = handle;
    int rc = pthread_create(&handle->thread, NULL, pcc_thread_trampoline, start);
    if (rc != 0) {
        free(start);
        pthread_mutex_destroy(&handle->state_lock);
        free(handle);
        return -1;
    }
    *out = handle;
    return 0;
}

int64_t pcc_thread_join(PccThreadHandle *thread, void **result) {
    if (thread == NULL) return -1;
    for (;;) {
        pthread_mutex_lock(&thread->state_lock);
        int32_t done = thread->done;
        void *local_result = thread->result;
        pthread_mutex_unlock(&thread->state_lock);
        if (done) {
            void *joined_result = NULL;
            int rc = pthread_join(thread->thread, &joined_result);
            if (result != NULL) {
                *result = rc == 0 ? joined_result : local_result;
            }
            pthread_mutex_destroy(&thread->state_lock);
            free(thread);
            return rc == 0 ? 0 : -1;
        }
        pcc_thread_safepoint();
        sched_yield();
    }
}

void pcc_thread_detach(PccThreadHandle *thread) {
    if (thread == NULL) return;
    (void)pthread_detach(thread->thread);
    int should_free = 0;
    pthread_mutex_lock(&thread->state_lock);
    thread->detached = 1;
    should_free = thread->done;
    pthread_mutex_unlock(&thread->state_lock);
    if (should_free) {
        pthread_mutex_destroy(&thread->state_lock);
        free(thread);
    }
}

PccMutex *pcc_mutex_new(void) {
    PccMutex *m = (PccMutex *)calloc(1, sizeof(PccMutex));
    if (m == NULL) return NULL;
    if (pthread_mutex_init(&m->mutex, NULL) != 0) {
        free(m);
        return NULL;
    }
    return m;
}

void pcc_mutex_free(PccMutex *mutex) {
    if (mutex == NULL) return;
    (void)pthread_mutex_destroy(&mutex->mutex);
    free(mutex);
}

int64_t pcc_mutex_lock(PccMutex *mutex) {
    if (mutex == NULL) return -1;
    for (;;) {
        int rc = pthread_mutex_trylock(&mutex->mutex);
        if (rc == 0) return 0;
        if (rc != EBUSY) return -1;
        pcc_thread_safepoint();
        sched_yield();
    }
}

int64_t pcc_mutex_unlock(PccMutex *mutex) {
    if (mutex == NULL) return -1;
    return pthread_mutex_unlock(&mutex->mutex) == 0 ? 0 : -1;
}

PccCond *pcc_cond_new(void) {
    PccCond *c = (PccCond *)calloc(1, sizeof(PccCond));
    if (c == NULL) return NULL;
    if (pthread_cond_init(&c->cond, NULL) != 0) {
        free(c);
        return NULL;
    }
    return c;
}

void pcc_cond_free(PccCond *cond) {
    if (cond == NULL) return;
    (void)pthread_cond_destroy(&cond->cond);
    free(cond);
}

int64_t pcc_cond_wait(PccCond *cond, PccMutex *mutex) {
    if (cond == NULL || mutex == NULL) return -1;
    return pthread_cond_wait(&cond->cond, &mutex->mutex) == 0 ? 0 : -1;
}

/* Bounded condition wait. Returns 0 if signaled, 1 if it timed out, -1 on
 * error. Callers that hold a runtime lock while blocking on a Python-level
 * primitive (e.g. threading.Lock.acquire) must use this plus
 * pcc_thread_safepoint() instead of pcc_cond_wait, so a thread waiting for the
 * lock can still reach a safepoint and park during stop-the-world collection.
 * An unbounded pcc_cond_wait deadlocks: the STW initiator waits for this
 * thread to park, this thread waits for the lock, and the lock owner is itself
 * parked at a safepoint and cannot release. See
 * docs/investigations/pcc1-threaded-explicit-gc-backend0-double-free-highscale.md */
int64_t pcc_cond_timedwait_ms(PccCond *cond, PccMutex *mutex, int64_t timeout_ms) {
    if (cond == NULL || mutex == NULL) return -1;
    if (timeout_ms < 0) timeout_ms = 0;
    int64_t now_us = pcc_runtime_now_us();
    long add_ns = (long)(timeout_ms % 1000) * 1000000L
        + (long)(now_us % 1000000LL) * 1000L;
    struct timespec ts;
    ts.tv_sec = (time_t)(now_us / 1000000LL)
        + (time_t)(timeout_ms / 1000)
        + (time_t)(add_ns / 1000000000L);
    ts.tv_nsec = add_ns % 1000000000L;
    int rc = pthread_cond_timedwait(&cond->cond, &mutex->mutex, &ts);
    if (rc == 0) return 0;
    if (rc == ETIMEDOUT) return 1;
    return -1;
}

int64_t pcc_cond_signal(PccCond *cond) {
    if (cond == NULL) return -1;
    return pthread_cond_signal(&cond->cond) == 0 ? 0 : -1;
}

int64_t pcc_cond_broadcast(PccCond *cond) {
    if (cond == NULL) return -1;
    return pthread_cond_broadcast(&cond->cond) == 0 ? 0 : -1;
}

#else  /* !PCC_WITH_THREADS */

struct PccThreadHandle { int unused; };
struct PccMutex { int unused; };
struct PccCond { int unused; };

int64_t pcc_current_thread_id(void) {
    return 1;
}

void pcc_thread_safepoint(void) {
    if (pcc_tls_no_park_depth != 0) return;
}

int64_t pcc_stop_the_world(void) {
    return 0;
}

int64_t pcc_resume_world(void) {
    return 0;
}

int64_t pcc_thread_owns_stopped_world(void) {
    return 1;
}

int64_t pcc_thread_registration_waiter_count(void) {
    return 0;
}

void pcc_thread_unregister_current(void) {
    if (pcc_tls_no_park_depth != 0) {
        abort();
        return;
    }
}

int64_t pcc_thread_start(
    PccThreadHandle **out,
    PccThreadMain entry,
    void *arg
) {
    (void)out;
    (void)entry;
    (void)arg;
    return -1;
}

int64_t pcc_thread_join(PccThreadHandle *thread, void **result) {
    (void)thread;
    if (result != NULL) *result = NULL;
    return -1;
}

void pcc_thread_detach(PccThreadHandle *thread) {
    (void)thread;
}

/* Non-NULL sentinel: callers gate on NULL-check, so a stub mutex still needs
 * to look "allocated" without dereferencing. The lock/unlock no-ops never
 * read through the pointer. */
static char _pcc_mutex_stub;
static char _pcc_cond_stub;
PccMutex *pcc_mutex_new(void) { return (PccMutex *)&_pcc_mutex_stub; }
void pcc_mutex_free(PccMutex *mutex) { (void)mutex; }
int64_t pcc_mutex_lock(PccMutex *mutex) { (void)mutex; return 0; }
int64_t pcc_mutex_unlock(PccMutex *mutex) { (void)mutex; return 0; }

PccCond *pcc_cond_new(void) { return (PccCond *)&_pcc_cond_stub; }
void pcc_cond_free(PccCond *cond) { (void)cond; }
int64_t pcc_cond_wait(PccCond *cond, PccMutex *mutex) {
    (void)cond;
    (void)mutex;
    return 0;
}
int64_t pcc_cond_timedwait_ms(PccCond *cond, PccMutex *mutex, int64_t timeout_ms) {
    (void)cond;
    (void)mutex;
    (void)timeout_ms;
    return 0;
}
int64_t pcc_cond_signal(PccCond *cond) { (void)cond; return 0; }
int64_t pcc_cond_broadcast(PccCond *cond) { (void)cond; return 0; }

#endif  /* PCC_WITH_THREADS */

void pcc_thread_no_park_enter(void) {
    /* Registration is defensive and must happen before publishing the TLS
     * lease.  A raw newcomer must register before acquiring any managed
     * owner/slot (see the public header); this call cannot repair a pointer
     * acquired before registration.  For an already-registered live caller,
     * deliberately do not safepoint here: it may have canonicalized the owner
     * immediately before entering, and live/unparked accounting makes an STW
     * owner wait for the bounded region to reach its outer exit. */
    if (pcc_tls_no_park_depth < 0
        || pcc_tls_no_park_depth == INT64_MAX) {
        abort();
        return;
    }
    if (pcc_tls_no_park_depth != 0) {
        pcc_tls_no_park_depth++;
        return;
    }
    (void)pcc_current_thread_id();
    pcc_tls_no_park_depth++;
}

void pcc_thread_no_park_exit(void) {
    if (pcc_tls_no_park_depth <= 0) {
        abort();
        return;
    }
    pcc_tls_no_park_depth--;
    if (pcc_tls_no_park_depth == 0) {
        /* Always take the real safepoint path.  Besides avoiding a racy plain
         * stop-flag poll, this makes the live thread either park in the active
         * epoch or prove that no stop is currently pending. */
        pcc_thread_safepoint();
    }
}

int64_t pcc_thread_no_park_depth(void) {
    return pcc_tls_no_park_depth;
}

enum {
    PCC_VTHREAD_NEW = 0,
    PCC_VTHREAD_READY = 1,
    PCC_VTHREAD_RUNNING = 2,
    PCC_VTHREAD_PARKED = 3,
    PCC_VTHREAD_DONE = 4
};

enum {
    PCC_VTHREAD_QUEUE_ENTRY_MALLOC = 0,
    PCC_VTHREAD_QUEUE_ENTRY_READY_POOL = 1,
    PCC_VTHREAD_QUEUE_ENTRY_JOIN_POOL = 2
};

typedef struct PccVirtualThreadQueueEntry {
    PyObject *thread;
    struct PccVirtualThreadQueueEntry *next;
    void *root_handle;
    int entry_kind;
} PccVirtualThreadQueueEntry;

typedef PccVirtualThreadQueueEntry PccVirtualThreadJoinEntry;

typedef struct PccVirtualThreadCarrierQueue {
    PccVirtualThreadQueueEntry *head;
    PccVirtualThreadQueueEntry *tail;
} PccVirtualThreadCarrierQueue;

/* Timer-parked virtual thread. Each entry is a stable heap-allocated node that
 * owns a GC root handle for its parked ``thread`` slot (root retention). The
 * scheduler's timer *ordering* now lives in a binary min-heap
 * (``pcc_vthread_timer_heap`` below, see py_timer_heap.h), replacing the
 * former O(n)-insert sorted singly-linked list. The min-heap only stores an
 * opaque ``int64_t timer_id``; we use the entry's own (stable) address as that
 * id so a popped id casts straight back to its node -- the heap never holds a
 * ``PyObject *`` so heap reordering can never move a GC-rooted slot (the roots
 * stay at the fixed ``&entry->thread`` addresses, exactly as before). */
typedef struct PccVirtualThreadTimerEntry {
    PyObject *thread;
    int64_t deadline_ms;
    struct PccVirtualThreadTimerEntry *next_free;
    void *root_handle;
} PccVirtualThreadTimerEntry;

typedef struct PccVirtualThreadPollEntry {
    PyObject *thread;
    int64_t fd;
    int64_t events;
    int64_t deadline_ms;
    struct PccVirtualThreadPollEntry *next;
    void *root_handle;
} PccVirtualThreadPollEntry;

typedef struct PccVirtualThreadIoResource {
    int64_t fd;
    int64_t generation;
    struct PccVirtualThreadIoResource *next;
} PccVirtualThreadIoResource;

static PccMutex *pcc_vthread_lock = NULL;
static PccVirtualThreadQueueEntry *pcc_vthread_ready_queue = NULL;
static PccVirtualThreadQueueEntry *pcc_vthread_ready_tail = NULL;
static PccVirtualThreadCarrierQueue *pcc_vthread_carrier_queues = NULL;
static int64_t pcc_vthread_carrier_queue_count = 0;
static int64_t pcc_vthread_next_carrier_enqueue = 0;
static int64_t pcc_vthread_carrier_steal_count = 0;
static PccVirtualThreadQueueEntry *pcc_vthread_ready_entry_free_head = NULL;
static int64_t pcc_vthread_ready_entry_free_count = 0;
static int64_t pcc_vthread_ready_entry_alloc_count = 0;
static int64_t pcc_vthread_ready_entry_reuse_count = 0;
static int64_t pcc_vthread_waiter_entry_alloc_count = 0;
static int64_t pcc_vthread_waiter_entry_reuse_count = 0;
static int64_t pcc_vthread_waiter_entry_cached_count = 0;
/* Min-heap timer index (py_timer_heap.h). ``pcc_vthread_timer_heap_ready``
 * gates one-time init; all access is under ``pcc_vthread_lock``. */
static PccTimerHeap pcc_vthread_timer_heap;
static int pcc_vthread_timer_heap_ready = 0;
static PccVirtualThreadTimerEntry *pcc_vthread_timer_entry_free_head = NULL;
static int64_t pcc_vthread_timer_entry_free_count = 0;
static int64_t pcc_vthread_timer_entry_alloc_count = 0;
static int64_t pcc_vthread_timer_entry_reuse_count = 0;
static PccVirtualThreadPollEntry *pcc_vthread_poll_queue = NULL;
static PccVirtualThreadPollEntry *pcc_vthread_poll_entry_free_head = NULL;
static int64_t pcc_vthread_poll_entry_free_count = 0;
static int64_t pcc_vthread_poll_entry_alloc_count = 0;
static int64_t pcc_vthread_poll_entry_reuse_count = 0;
static PccVirtualThreadJoinEntry *pcc_vthread_join_entry_free_head = NULL;
static int64_t pcc_vthread_join_entry_free_count = 0;
/* One production waitset indexes the GC-rooted poll-entry queue by live fd.
 * On Darwin/BSD it owns kqueue; elsewhere (or when explicitly forced) it owns
 * the one-poll-call fallback. Multiple vthreads may still wait on one fd: the
 * waitset registration aggregates their interest/deadline, while the stable
 * poll entries retain per-vthread roots and semantics. */
static PccIoWaitSet pcc_vthread_io_waitset;
static int pcc_vthread_io_waitset_ready = 0;
/* Exactly one carrier may own the live kernel wait. The owner releases the
 * scheduler mutex while blocked; mutations under that mutex signal the
 * waitset's compiler-owned interrupt channel. */
static int pcc_vthread_io_wait_active = 0;
static int64_t pcc_vthread_io_backend_value = PCC_VTHREAD_IO_BACKEND_POLL;
static struct pollfd *pcc_vthread_live_pollfds = NULL;
static int64_t pcc_vthread_live_pollfds_cap = 0;
static int64_t pcc_vthread_ready_count_value = 0;
/* (timer count is now the min-heap live-set size; see py_virtual_thread_timer_count) */
static int64_t pcc_vthread_io_wait_count_value = 0;
static PccVirtualThreadIoResource *pcc_vthread_io_resources = NULL;
static int64_t pcc_vthread_io_resource_generation = 0;
static int64_t pcc_vthread_carrier_count = 1;
static int64_t pcc_vthread_pin_depth_total = 0;
static int64_t pcc_vthread_pin_events = 0;
static _Thread_local PyObject *pcc_current_virtual_thread = NULL;
static _Thread_local int64_t pcc_current_virtual_thread_carrier = -1;
static PccThreadHandle **pcc_vthread_persistent_carriers = NULL;
static int64_t *pcc_vthread_persistent_carrier_indices = NULL;
static int64_t pcc_vthread_persistent_carrier_count = 0;
static int64_t pcc_vthread_bounded_pool_running = 0;
static int64_t pcc_vthread_persistent_pool_running = 0;
static int64_t pcc_vthread_persistent_pool_stop = 0;
static int64_t pcc_vthread_persistent_pool_failures = 0;
static int64_t pcc_vthread_persistent_joined_count = 0;
static int64_t pcc_vthread_persistent_cleanup_active = 0;

/* Allocation-free production effect trace. Scheduler tests reset/read it only
 * while carriers are quiescent; writers may be concurrent and reserve slots
 * atomically. Once the bounded buffer fills, further events are counted as
 * dropped rather than allocating or overwriting evidence. */
#define PCC_VTHREAD_EFFECT_EVENT_CAPACITY 4096
typedef struct PccVirtualThreadEffectEvent {
    int64_t kind;
    int64_t detail;
    int64_t root_delta;
    int64_t state;
} PccVirtualThreadEffectEvent;

static PccVirtualThreadEffectEvent pcc_vthread_effect_events[
    PCC_VTHREAD_EFFECT_EVENT_CAPACITY
];
static int64_t pcc_vthread_effect_event_count = 0;
static int64_t pcc_vthread_effect_event_dropped = 0;

static void pcc_vthread_effect_emit(
    int64_t kind,
    int64_t detail,
    int64_t root_delta,
    int64_t state
) {
    int64_t index = __atomic_fetch_add(
        &pcc_vthread_effect_event_count, 1, __ATOMIC_ACQ_REL
    );
    if (index < 0 || index >= PCC_VTHREAD_EFFECT_EVENT_CAPACITY) {
        (void)__atomic_add_fetch(
            &pcc_vthread_effect_event_dropped, 1, __ATOMIC_ACQ_REL
        );
        return;
    }
    pcc_vthread_effect_events[index].kind = kind;
    pcc_vthread_effect_events[index].detail = detail;
    pcc_vthread_effect_events[index].root_delta = root_delta;
    pcc_vthread_effect_events[index].state = state;
    __atomic_thread_fence(__ATOMIC_RELEASE);
}

static int64_t pcc_vthread_effect_read_field(int64_t index, int field) {
    int64_t count = __atomic_load_n(
        &pcc_vthread_effect_event_count, __ATOMIC_ACQUIRE
    );
    if (
        index < 0 || index >= count
        || index >= PCC_VTHREAD_EFFECT_EVENT_CAPACITY
    ) {
        return -1;
    }
    PccVirtualThreadEffectEvent *event = &pcc_vthread_effect_events[index];
    if (field == 0) return event->kind;
    if (field == 1) return event->detail;
    if (field == 2) return event->root_delta;
    if (field == 3) return event->state;
    return -1;
}

int64_t py_virtual_thread_effect_reset(void) {
    __atomic_store_n(&pcc_vthread_effect_event_count, 0, __ATOMIC_RELEASE);
    __atomic_store_n(&pcc_vthread_effect_event_dropped, 0, __ATOMIC_RELEASE);
    return 0;
}

int64_t py_virtual_thread_effect_count(void) {
    int64_t count = __atomic_load_n(
        &pcc_vthread_effect_event_count, __ATOMIC_ACQUIRE
    );
    return count < PCC_VTHREAD_EFFECT_EVENT_CAPACITY
        ? count : PCC_VTHREAD_EFFECT_EVENT_CAPACITY;
}

int64_t py_virtual_thread_effect_dropped(void) {
    return __atomic_load_n(
        &pcc_vthread_effect_event_dropped, __ATOMIC_ACQUIRE
    );
}

int64_t py_virtual_thread_effect_kind_at(int64_t index) {
    return pcc_vthread_effect_read_field(index, 0);
}
int64_t py_virtual_thread_effect_detail_at(int64_t index) {
    return pcc_vthread_effect_read_field(index, 1);
}
int64_t py_virtual_thread_effect_root_delta_at(int64_t index) {
    return pcc_vthread_effect_read_field(index, 2);
}
int64_t py_virtual_thread_effect_state_at(int64_t index) {
    return pcc_vthread_effect_read_field(index, 3);
}

void pcc_vthread_effect_note_waiter_root_enter(void) {
    pcc_vthread_effect_emit(
        PCC_VTHREAD_EFFECT_ROOT_ENTER,
        PCC_VTHREAD_NODE_WAITER,
        1,
        -1
    );
}

void pcc_vthread_effect_note_waiter_root_leave(void) {
    pcc_vthread_effect_emit(
        PCC_VTHREAD_EFFECT_ROOT_LEAVE,
        PCC_VTHREAD_NODE_WAITER,
        -1,
        -1
    );
}

static int pcc_vthread_scheduler_init(void) {
    if (pcc_vthread_lock != NULL) return 0;
    pcc_vthread_lock = pcc_mutex_new();
    return pcc_vthread_lock != NULL ? 0 : -1;
}

static int64_t pcc_vthread_now_ms(void) {
    return pcc_runtime_monotonic_us() / 1000;
}

void pcc_vthread_waiter_pool_note_allocation(void) {
    (void)__atomic_add_fetch(
        &pcc_vthread_waiter_entry_alloc_count, 1, __ATOMIC_ACQ_REL
    );
}

void pcc_vthread_waiter_pool_note_reuse(void) {
    (void)__atomic_add_fetch(
        &pcc_vthread_waiter_entry_reuse_count, 1, __ATOMIC_ACQ_REL
    );
}

void pcc_vthread_waiter_pool_note_cached(int64_t count) {
    __atomic_store_n(
        &pcc_vthread_waiter_entry_cached_count, count, __ATOMIC_RELEASE
    );
}

static PyVirtualThreadObject *checked_vthread(PyObject *vthread) {
    if (vthread == NULL || PY_IS_TAGGED_INT(vthread)) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "object is not a virtual thread"));
        return NULL;
    }
    vthread = pcc_gc_note_relocation_read(vthread);
    if (py_type_of(vthread) != PY_TYPE_VIRTUAL_THREAD) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "object is not a virtual thread"));
        return NULL;
    }
    return (PyVirtualThreadObject *)vthread;
}

#define PCC_VTHREAD_READY_ENTRY_POOL_LIMIT 4096

static void pcc_vthread_join_entry_recycle_locked(
    PccVirtualThreadJoinEntry *entry
);

static void pcc_vthread_ready_entry_clear(PccVirtualThreadQueueEntry *entry) {
    if (entry == NULL) return;
    entry->thread = NULL;
    entry->next = NULL;
    entry->root_handle = NULL;
    entry->entry_kind = PCC_VTHREAD_QUEUE_ENTRY_MALLOC;
}

static PccVirtualThreadQueueEntry *pcc_vthread_ready_entry_alloc_locked(
    int use_ready_pool
) {
    PccVirtualThreadQueueEntry *entry = NULL;
    if (use_ready_pool && pcc_vthread_ready_entry_free_head != NULL) {
        entry = pcc_vthread_ready_entry_free_head;
        pcc_vthread_ready_entry_free_head = entry->next;
        pcc_vthread_ready_entry_reuse_count++;
        if (pcc_vthread_ready_entry_free_count > 0) {
            pcc_vthread_ready_entry_free_count--;
        }
    }
    if (entry == NULL) {
        entry = (PccVirtualThreadQueueEntry *)malloc(
            sizeof(PccVirtualThreadQueueEntry)
        );
        if (entry != NULL) pcc_vthread_ready_entry_alloc_count++;
    }
    pcc_vthread_ready_entry_clear(entry);
    if (entry != NULL) {
        entry->entry_kind = use_ready_pool
            ? PCC_VTHREAD_QUEUE_ENTRY_READY_POOL
            : PCC_VTHREAD_QUEUE_ENTRY_MALLOC;
    }
    return entry;
}

static void pcc_vthread_ready_entry_recycle_locked(
    PccVirtualThreadQueueEntry *entry
) {
    if (entry == NULL) return;
    int entry_kind = entry->entry_kind;
    if (entry_kind == PCC_VTHREAD_QUEUE_ENTRY_JOIN_POOL) {
        pcc_vthread_join_entry_recycle_locked(entry);
        return;
    }
    pcc_vthread_ready_entry_clear(entry);
    if (entry_kind != PCC_VTHREAD_QUEUE_ENTRY_READY_POOL) {
        free(entry);
        return;
    }
    if (
        pcc_vthread_ready_entry_free_count
        >= PCC_VTHREAD_READY_ENTRY_POOL_LIMIT
    ) {
        free(entry);
        return;
    }
    entry->entry_kind = PCC_VTHREAD_QUEUE_ENTRY_READY_POOL;
    entry->next = pcc_vthread_ready_entry_free_head;
    pcc_vthread_ready_entry_free_head = entry;
    pcc_vthread_ready_entry_free_count++;
}

static void pcc_vthread_ready_entry_release_locked(
    PccVirtualThreadQueueEntry *entry
) {
    if (entry == NULL) return;
    if (entry->root_handle != NULL) {
        pcc_gc_scheduler_root_unregister_handle(entry->root_handle);
        pcc_vthread_effect_emit(
            PCC_VTHREAD_EFFECT_ROOT_LEAVE,
            entry->entry_kind == PCC_VTHREAD_QUEUE_ENTRY_JOIN_POOL
                ? PCC_VTHREAD_NODE_WAITER
                : PCC_VTHREAD_NODE_READY,
            -1,
            -1
        );
    }
    entry->root_handle = NULL;
    pcc_gc_store_root(&entry->thread, NULL);
    pcc_vthread_ready_entry_recycle_locked(entry);
}

static void pcc_vthread_queue_push_entry_locked(
    PccVirtualThreadQueueEntry **head,
    PccVirtualThreadQueueEntry **tail,
    PccVirtualThreadQueueEntry *entry
) {
    entry->next = NULL;
    if (*tail == NULL) {
        *head = entry;
        *tail = entry;
    } else {
        (*tail)->next = entry;
        *tail = entry;
    }
}

#define PCC_VTHREAD_JOIN_ENTRY_POOL_LIMIT 4096

static void pcc_vthread_join_entry_clear(PccVirtualThreadJoinEntry *entry) {
    if (entry == NULL) return;
    entry->thread = NULL;
    entry->next = NULL;
    entry->root_handle = NULL;
    entry->entry_kind = PCC_VTHREAD_QUEUE_ENTRY_JOIN_POOL;
}

static PccVirtualThreadJoinEntry *pcc_vthread_join_entry_alloc_locked(void) {
    PccVirtualThreadJoinEntry *entry = pcc_vthread_join_entry_free_head;
    if (entry != NULL) {
        pcc_vthread_join_entry_free_head = entry->next;
        if (pcc_vthread_join_entry_free_count > 0) {
            pcc_vthread_join_entry_free_count--;
        }
        pcc_vthread_waiter_pool_note_reuse();
        pcc_vthread_waiter_pool_note_cached(
            pcc_vthread_join_entry_free_count
        );
    } else {
        entry = (PccVirtualThreadJoinEntry *)malloc(sizeof(*entry));
        if (entry != NULL) pcc_vthread_waiter_pool_note_allocation();
    }
    pcc_vthread_join_entry_clear(entry);
    return entry;
}

static void pcc_vthread_join_entry_recycle_locked(
    PccVirtualThreadJoinEntry *entry
) {
    if (entry == NULL) return;
    pcc_vthread_join_entry_clear(entry);
    if (
        pcc_vthread_join_entry_free_count
        >= PCC_VTHREAD_JOIN_ENTRY_POOL_LIMIT
    ) {
        free(entry);
        return;
    }
    entry->next = pcc_vthread_join_entry_free_head;
    pcc_vthread_join_entry_free_head = entry;
    pcc_vthread_join_entry_free_count++;
    pcc_vthread_waiter_pool_note_cached(pcc_vthread_join_entry_free_count);
}

static void pcc_vthread_join_entry_release_locked(
    PccVirtualThreadJoinEntry *entry
) {
    if (entry == NULL) return;
    if (entry->root_handle != NULL) {
        pcc_gc_scheduler_root_unregister_handle(entry->root_handle);
        pcc_vthread_effect_note_waiter_root_leave();
    }
    entry->root_handle = NULL;
    pcc_gc_store_root(&entry->thread, NULL);
    pcc_vthread_join_entry_recycle_locked(entry);
}

static void pcc_vthread_push_ready_entry_locked(
    PccVirtualThreadQueueEntry *entry
) {
    if (
        pcc_vthread_carrier_queues != NULL
        && pcc_vthread_carrier_queue_count > 0
    ) {
        int64_t idx = pcc_vthread_next_carrier_enqueue
            % pcc_vthread_carrier_queue_count;
        pcc_vthread_next_carrier_enqueue++;
        pcc_vthread_queue_push_entry_locked(
            &pcc_vthread_carrier_queues[idx].head,
            &pcc_vthread_carrier_queues[idx].tail,
            entry
        );
        return;
    }
    pcc_vthread_queue_push_entry_locked(
        &pcc_vthread_ready_queue,
        &pcc_vthread_ready_tail,
        entry
    );
}

static PccVirtualThreadQueueEntry *pcc_vthread_prepare_ready_entry_locked(
    PyObject *vthread
) {
    PccVirtualThreadQueueEntry *entry = (
        PccVirtualThreadQueueEntry *
    )pcc_vthread_ready_entry_alloc_locked(1);
    if (entry == NULL) return NULL;
    entry->root_handle = pcc_gc_scheduler_root_register_handle(&entry->thread);
    if (entry->root_handle == NULL) {
        pcc_vthread_ready_entry_recycle_locked(entry);
        return NULL;
    }
    pcc_vthread_effect_emit(
        PCC_VTHREAD_EFFECT_ROOT_ENTER,
        PCC_VTHREAD_NODE_READY,
        1,
        -1
    );
    pcc_gc_store_root(&entry->thread, vthread);
    return entry;
}

static void pcc_vthread_publish_ready_entry_locked(
    PyVirtualThreadObject *vt,
    PccVirtualThreadQueueEntry *entry
) {
    vt->state = PCC_VTHREAD_READY;
    pcc_vthread_push_ready_entry_locked(entry);
    vt->queued = 1;
    pcc_vthread_ready_count_value++;
    pcc_vthread_effect_emit(
        PCC_VTHREAD_EFFECT_READY_ENQUEUE,
        PCC_VTHREAD_NODE_READY,
        0,
        vt->state
    );
}

static int pcc_vthread_enqueue_locked(PyObject *vthread) {
    PyVirtualThreadObject *vt = (PyVirtualThreadObject *)vthread;
    if (vt->queued != 0 || vt->state != PCC_VTHREAD_READY) return 0;
    PccVirtualThreadQueueEntry *entry =
        pcc_vthread_prepare_ready_entry_locked(vthread);
    if (entry == NULL) return -1;
    pcc_vthread_publish_ready_entry_locked(vt, entry);
    return 0;
}

static int pcc_vthread_make_ready_locked(PyVirtualThreadObject *vt) {
    if (vt == NULL || vt->state == PCC_VTHREAD_DONE) return 0;
    vt->state = PCC_VTHREAD_READY;
    return pcc_vthread_enqueue_locked((PyObject *)vt);
}

static int pcc_vthread_join_enqueue_locked(
    PyVirtualThreadObject *target,
    PyVirtualThreadObject *waiter
) {
    PccVirtualThreadJoinEntry *entry = pcc_vthread_join_entry_alloc_locked();
    if (entry == NULL) return -1;
    entry->root_handle = pcc_gc_scheduler_root_register_handle(&entry->thread);
    if (entry->root_handle == NULL) {
        pcc_vthread_join_entry_recycle_locked(entry);
        return -1;
    }
    pcc_vthread_effect_note_waiter_root_enter();
    pcc_gc_store_root(&entry->thread, (PyObject *)waiter);
    entry->next = NULL;
    PccVirtualThreadJoinEntry *tail = (
        PccVirtualThreadJoinEntry *
    )target->join_wait_tail;
    if (tail == NULL) {
        target->join_waiters = entry;
    } else {
        tail->next = entry;
    }
    target->join_wait_tail = entry;
    waiter->join_entry = entry;
    waiter->wait_kind = PCC_VTHREAD_WAIT_JOIN;
    waiter->queued = 0;
    waiter->state = PCC_VTHREAD_PARKED;
    pcc_vthread_effect_emit(
        PCC_VTHREAD_EFFECT_PARK,
        PCC_VTHREAD_NODE_WAITER,
        0,
        waiter->state
    );
    return 0;
}

static int pcc_vthread_join_wake_all_locked(PyVirtualThreadObject *target) {
    while (target->join_waiters != NULL) {
        PccVirtualThreadJoinEntry *entry = (
            PccVirtualThreadJoinEntry *
        )target->join_waiters;
        PyObject *waiter_obj = pcc_gc_load_ptr(NULL, &entry->thread);
        PyVirtualThreadObject *waiter = NULL;
        if (
            waiter_obj != NULL
            && !PY_IS_TAGGED_INT(waiter_obj)
            && py_type_of(waiter_obj) == PY_TYPE_VIRTUAL_THREAD
        ) {
            waiter = (PyVirtualThreadObject *)waiter_obj;
        }
        if (
            waiter != NULL
            && waiter->join_entry == entry
            && waiter->state == PCC_VTHREAD_PARKED
            && waiter->queued == 0
        ) {
            PccVirtualThreadJoinEntry *next = entry->next;
            target->join_waiters = next;
            if (next == NULL) target->join_wait_tail = NULL;
            waiter->join_entry = NULL;
            waiter->wait_kind = PCC_VTHREAD_WAIT_NONE;
            waiter->state = PCC_VTHREAD_READY;
            pcc_vthread_push_ready_entry_locked(entry);
            waiter->queued = 1;
            pcc_vthread_ready_count_value++;
            pcc_vthread_effect_emit(
                PCC_VTHREAD_EFFECT_READY_ENQUEUE,
                PCC_VTHREAD_NODE_READY,
                0,
                waiter->state
            );
            pcc_vthread_effect_emit(
                PCC_VTHREAD_EFFECT_UNPARK,
                PCC_VTHREAD_NODE_WAITER,
                0,
                waiter->state
            );
            continue;
        }
        target->join_waiters = entry->next;
        if (target->join_waiters == NULL) target->join_wait_tail = NULL;
        pcc_vthread_join_entry_release_locked(entry);
    }
    return 0;
}

/* Detach one parked joiner while retaining ownership of its scheduler-root
 * node.  The caller must either transfer the returned node to the ready queue
 * or release it before dropping the scheduler lock. */
static PccVirtualThreadJoinEntry *pcc_vthread_join_unlink_locked(
    PyVirtualThreadObject *waiter
) {
    PccVirtualThreadJoinEntry *entry = (
        PccVirtualThreadJoinEntry *
    )waiter->join_entry;
    PyObject *target_obj = pcc_gc_load_ptr(
        (PyObject *)waiter,
        &waiter->join_target
    );
    if (
        entry == NULL
        || target_obj == NULL
        || PY_IS_TAGGED_INT(target_obj)
        || py_type_of(target_obj) != PY_TYPE_VIRTUAL_THREAD
    ) {
        return NULL;
    }
    PyVirtualThreadObject *target = (PyVirtualThreadObject *)target_obj;
    PccVirtualThreadJoinEntry *previous = NULL;
    PccVirtualThreadJoinEntry *current = (
        PccVirtualThreadJoinEntry *
    )target->join_waiters;
    while (current != NULL && current != entry) {
        previous = current;
        current = current->next;
    }
    if (current == NULL) return NULL;
    if (previous == NULL) {
        target->join_waiters = current->next;
    } else {
        previous->next = current->next;
    }
    if (target->join_wait_tail == current) {
        target->join_wait_tail = previous;
    }
    waiter->join_entry = NULL;
    waiter->wait_kind = PCC_VTHREAD_WAIT_NONE;
    pcc_gc_store_ptr((PyObject *)waiter, &waiter->join_target, NULL);
    current->next = NULL;
    return current;
}

/* Cancellation must not allocate after removing a waiter: otherwise an
 * allocation failure could leave the task parked forever with no remaining
 * event capable of waking it.  Join nodes share the ready-node prefix, so the
 * existing rooted node can be transferred directly. */
static int pcc_vthread_join_cancel_locked(PyVirtualThreadObject *waiter) {
    PccVirtualThreadJoinEntry *entry =
        pcc_vthread_join_unlink_locked(waiter);
    if (entry == NULL) return -1;
    pcc_vthread_publish_ready_entry_locked(waiter, entry);
    pcc_vthread_effect_emit(
        PCC_VTHREAD_EFFECT_UNPARK,
        PCC_VTHREAD_NODE_WAITER,
        0,
        waiter->state
    );
    return 0;
}

static PyObject *pcc_vthread_dequeue_from_queue_locked(
    PccVirtualThreadQueueEntry **head,
    PccVirtualThreadQueueEntry **tail
) {
    while (*head != NULL) {
        PccVirtualThreadQueueEntry *entry = *head;
        *head = entry->next;
        if (*head == NULL) *tail = NULL;
        if (pcc_vthread_ready_count_value > 0) pcc_vthread_ready_count_value--;

        PyObject *thread = pcc_gc_load_ptr(NULL, &entry->thread);
        PyVirtualThreadObject *vt = NULL;
        if (
            thread != NULL
            && !PY_IS_TAGGED_INT(thread)
            && py_type_of(thread) == PY_TYPE_VIRTUAL_THREAD
        ) {
            vt = (PyVirtualThreadObject *)thread;
            vt->queued = 0;
        }
        py_incref(thread);
        pcc_vthread_ready_entry_release_locked(entry);
        if (vt != NULL && vt->state == PCC_VTHREAD_READY) {
            vt->state = PCC_VTHREAD_RUNNING;
            pcc_vthread_effect_emit(
                PCC_VTHREAD_EFFECT_RESUME,
                PCC_VTHREAD_NODE_READY,
                0,
                vt->state
            );
            return thread;
        }
        py_decref(thread);
    }
    return NULL;
}

static PyObject *pcc_vthread_dequeue_locked(void) {
    int64_t own = pcc_current_virtual_thread_carrier;
    int64_t n_queues = pcc_vthread_carrier_queue_count;
    if (
        pcc_vthread_carrier_queues != NULL
        && own >= 0
        && own < n_queues
    ) {
        PyObject *thread = pcc_vthread_dequeue_from_queue_locked(
            &pcc_vthread_carrier_queues[own].head,
            &pcc_vthread_carrier_queues[own].tail
        );
        if (thread != NULL) return thread;
        for (int64_t offset = 1; offset < n_queues; offset++) {
            int64_t idx = (own + offset) % n_queues;
            thread = pcc_vthread_dequeue_from_queue_locked(
                &pcc_vthread_carrier_queues[idx].head,
                &pcc_vthread_carrier_queues[idx].tail
            );
            if (thread != NULL) {
                __atomic_add_fetch(
                    &pcc_vthread_carrier_steal_count,
                    1,
                    __ATOMIC_ACQ_REL
                );
                return thread;
            }
        }
    } else if (pcc_vthread_carrier_queues != NULL && n_queues > 0) {
        for (int64_t idx = 0; idx < n_queues; idx++) {
            PyObject *thread = pcc_vthread_dequeue_from_queue_locked(
                &pcc_vthread_carrier_queues[idx].head,
                &pcc_vthread_carrier_queues[idx].tail
            );
            if (thread != NULL) return thread;
        }
    }
    return pcc_vthread_dequeue_from_queue_locked(
        &pcc_vthread_ready_queue,
        &pcc_vthread_ready_tail
    );
}

static int pcc_vthread_carrier_queues_open_locked(int64_t carrier_count) {
    if (carrier_count <= 0) return 0;
    if (pcc_vthread_carrier_queues != NULL) return 0;
    PccVirtualThreadCarrierQueue *queues = (
        PccVirtualThreadCarrierQueue *
    )calloc((size_t)carrier_count, sizeof(PccVirtualThreadCarrierQueue));
    if (queues == NULL) return -1;
    pcc_vthread_carrier_queues = queues;
    pcc_vthread_carrier_queue_count = carrier_count;
    pcc_vthread_next_carrier_enqueue = 0;
    return 0;
}

static void pcc_vthread_carrier_queues_close_locked(void) {
    PccVirtualThreadCarrierQueue *queues = pcc_vthread_carrier_queues;
    int64_t count = pcc_vthread_carrier_queue_count;
    if (queues == NULL || count <= 0) {
        pcc_vthread_carrier_queues = NULL;
        pcc_vthread_carrier_queue_count = 0;
        return;
    }
    for (int64_t i = 0; i < count; i++) {
        if (queues[i].head == NULL) continue;
        if (pcc_vthread_ready_tail == NULL) {
            pcc_vthread_ready_queue = queues[i].head;
            pcc_vthread_ready_tail = queues[i].tail;
        } else {
            pcc_vthread_ready_tail->next = queues[i].head;
            pcc_vthread_ready_tail = queues[i].tail;
        }
        queues[i].head = NULL;
        queues[i].tail = NULL;
    }
    pcc_vthread_carrier_queues = NULL;
    pcc_vthread_carrier_queue_count = 0;
    free(queues);
}

#define PCC_VTHREAD_TIMER_ENTRY_POOL_LIMIT 4096
#define PCC_VTHREAD_POLL_ENTRY_POOL_LIMIT 4096

static void pcc_vthread_timer_entry_clear(PccVirtualThreadTimerEntry *entry) {
    if (entry == NULL) return;
    entry->thread = NULL;
    entry->deadline_ms = 0;
    entry->next_free = NULL;
    entry->root_handle = NULL;
}

static PccVirtualThreadTimerEntry *pcc_vthread_timer_entry_alloc_locked(void) {
    PccVirtualThreadTimerEntry *entry = pcc_vthread_timer_entry_free_head;
    if (entry != NULL) {
        pcc_vthread_timer_entry_free_head = entry->next_free;
        pcc_vthread_timer_entry_reuse_count++;
        if (pcc_vthread_timer_entry_free_count > 0) {
            pcc_vthread_timer_entry_free_count--;
        }
    } else {
        entry = (PccVirtualThreadTimerEntry *)malloc(
            sizeof(PccVirtualThreadTimerEntry)
        );
        if (entry != NULL) pcc_vthread_timer_entry_alloc_count++;
    }
    pcc_vthread_timer_entry_clear(entry);
    return entry;
}

static void pcc_vthread_timer_entry_recycle_locked(
    PccVirtualThreadTimerEntry *entry
) {
    if (entry == NULL) return;
    pcc_vthread_timer_entry_clear(entry);
    if (
        pcc_vthread_timer_entry_free_count
        >= PCC_VTHREAD_TIMER_ENTRY_POOL_LIMIT
    ) {
        free(entry);
        return;
    }
    entry->next_free = pcc_vthread_timer_entry_free_head;
    pcc_vthread_timer_entry_free_head = entry;
    pcc_vthread_timer_entry_free_count++;
}

static void pcc_vthread_timer_entry_release_locked(
    PccVirtualThreadTimerEntry *entry
) {
    if (entry == NULL) return;
    if (entry->root_handle != NULL) {
        pcc_gc_scheduler_root_unregister_handle(entry->root_handle);
        pcc_vthread_effect_emit(
            PCC_VTHREAD_EFFECT_ROOT_LEAVE,
            PCC_VTHREAD_NODE_TIMER,
            -1,
            -1
        );
    }
    entry->root_handle = NULL;
    pcc_gc_store_root(&entry->thread, NULL);
    pcc_vthread_timer_entry_recycle_locked(entry);
}

/* Lazily init the timer min-heap. Caller must hold pcc_vthread_lock.
 * Returns 0 on success, -1 on allocation failure. */
static int pcc_vthread_timer_heap_ensure_locked(void) {
    if (pcc_vthread_timer_heap_ready) return 0;
    if (pcc_timer_heap_init(&pcc_vthread_timer_heap) != 0) return -1;
    pcc_vthread_timer_heap_ready = 1;
    return 0;
}

/* Map a stable timer-entry node <-> the opaque int64 id stored in the heap.
 * The entry address is unique and stable for the entry's lifetime, and the
 * heap never dereferences the id, so this round-trips exactly. */
static int64_t pcc_vthread_timer_entry_id(const PccVirtualThreadTimerEntry *e) {
    return (int64_t)(intptr_t)e;
}
static PccVirtualThreadTimerEntry *pcc_vthread_timer_entry_from_id(int64_t id) {
    return (PccVirtualThreadTimerEntry *)(intptr_t)id;
}

/* Cancel and retire one active timer while holding pcc_vthread_lock.  The
 * heap's cancellation is lazy, but the scheduler node and its GC root are not:
 * they are released immediately.  A later timer may reuse the node address;
 * the heap's authoritative (deadline, seq) live-map tuple distinguishes that
 * new registration from the cancelled stale heap tuple. */
static int pcc_vthread_timer_cancel_locked(PyVirtualThreadObject *vt) {
    PccVirtualThreadTimerEntry *entry =
        (PccVirtualThreadTimerEntry *)vt->timer_entry;
    if (entry == NULL) return 0;
    if (
        !pcc_vthread_timer_heap_ready
        || pcc_timer_heap_cancel(
            &pcc_vthread_timer_heap,
            pcc_vthread_timer_entry_id(entry)
        ) != 1
    ) {
        return -1;
    }
    vt->timer_entry = NULL;
    vt->queued = 0;
    if (vt->wait_kind == PCC_VTHREAD_WAIT_TIMER) {
        vt->wait_kind = PCC_VTHREAD_WAIT_NONE;
    }
    pcc_vthread_timer_entry_release_locked(entry);
    pcc_vthread_effect_emit(
        PCC_VTHREAD_EFFECT_CANCEL_TIMER,
        PCC_VTHREAD_NODE_TIMER,
        0,
        vt->state
    );
    return 1;
}

static void pcc_vthread_poll_entry_clear(PccVirtualThreadPollEntry *entry) {
    if (entry == NULL) return;
    entry->thread = NULL;
    entry->fd = 0;
    entry->events = 0;
    entry->deadline_ms = 0;
    entry->next = NULL;
    entry->root_handle = NULL;
}

static PccVirtualThreadPollEntry *pcc_vthread_poll_entry_alloc_locked(void) {
    PccVirtualThreadPollEntry *entry = pcc_vthread_poll_entry_free_head;
    if (entry != NULL) {
        pcc_vthread_poll_entry_free_head = entry->next;
        pcc_vthread_poll_entry_reuse_count++;
        if (pcc_vthread_poll_entry_free_count > 0) {
            pcc_vthread_poll_entry_free_count--;
        }
    } else {
        entry = (PccVirtualThreadPollEntry *)malloc(
            sizeof(PccVirtualThreadPollEntry)
        );
        if (entry != NULL) pcc_vthread_poll_entry_alloc_count++;
    }
    pcc_vthread_poll_entry_clear(entry);
    return entry;
}

static void pcc_vthread_poll_entry_recycle_locked(
    PccVirtualThreadPollEntry *entry
) {
    if (entry == NULL) return;
    pcc_vthread_poll_entry_clear(entry);
    if (
        pcc_vthread_poll_entry_free_count >= PCC_VTHREAD_POLL_ENTRY_POOL_LIMIT
    ) {
        free(entry);
        return;
    }
    entry->next = pcc_vthread_poll_entry_free_head;
    pcc_vthread_poll_entry_free_head = entry;
    pcc_vthread_poll_entry_free_count++;
}

static void pcc_vthread_poll_entry_release_locked(
    PccVirtualThreadPollEntry *entry
) {
    if (entry == NULL) return;
    if (entry->root_handle != NULL) {
        pcc_gc_scheduler_root_unregister_handle(entry->root_handle);
        pcc_vthread_effect_emit(
            PCC_VTHREAD_EFFECT_ROOT_LEAVE,
            PCC_VTHREAD_NODE_IO,
            -1,
            -1
        );
    }
    entry->root_handle = NULL;
    pcc_gc_store_root(&entry->thread, NULL);
    pcc_vthread_poll_entry_recycle_locked(entry);
}

/* Select the live-fd owner once per process. ``auto`` (and an unset variable)
 * selects kqueue or epoll when the compiled platform provides it. ``poll``
 * forces the portable fallback so its production path stays continuously
 * testable. A requested-but-unavailable live backend falls back and remains
 * observable through py_virtual_thread_io_backend(). Caller holds the
 * scheduler lock. */
static int pcc_vthread_io_refresh_registered_fd_locked(int64_t fd);

/* End one initialized waitset lifetime without touching the GC-rooted poll
 * queue.  The queue is the scheduler's durable source of truth; a later pool
 * start rehydrates kernel registrations from it.  Refuse to close the wake
 * channel while any waiter can still be outside the scheduler mutex. */
static int pcc_vthread_io_waitset_dispose_locked(void) {
    if (pcc_vthread_io_wait_active) return -1;
    if (pcc_vthread_io_waitset_ready) {
        pcc_io_waitset_dispose(&pcc_vthread_io_waitset);
    }
    pcc_vthread_io_waitset_ready = 0;
    pcc_vthread_io_wait_active = 0;
    pcc_vthread_io_backend_value = PCC_VTHREAD_IO_BACKEND_POLL;
    free(pcc_vthread_live_pollfds);
    pcc_vthread_live_pollfds = NULL;
    pcc_vthread_live_pollfds_cap = 0;
    return 0;
}

static int pcc_vthread_io_waitset_ensure_locked(void) {
    if (pcc_vthread_io_waitset_ready) return 0;
    const char *requested = pcc_runtime_getenv("PCC_VTHREAD_IO_BACKEND");
    int force_poll = requested != NULL && strcmp(requested, "poll") == 0;
    int force_kqueue = requested != NULL && strcmp(requested, "kqueue") == 0;
    int force_epoll = requested != NULL && strcmp(requested, "epoll") == 0;
    PccIoWaitSetBackend backend = PCC_IO_WAITSET_BACKEND_POLL;
    if (force_kqueue && pcc_io_waitset_kqueue_available()) {
        backend = PCC_IO_WAITSET_BACKEND_KQUEUE;
    } else if (force_epoll && pcc_io_waitset_epoll_available()) {
        backend = PCC_IO_WAITSET_BACKEND_EPOLL;
    } else if (
        !force_poll && !force_kqueue && !force_epoll
        && pcc_io_waitset_kqueue_available()
    ) {
        backend = PCC_IO_WAITSET_BACKEND_KQUEUE;
    } else if (
        !force_poll && !force_kqueue && !force_epoll
        && pcc_io_waitset_epoll_available()
    ) {
        backend = PCC_IO_WAITSET_BACKEND_EPOLL;
    }
    if (pcc_io_waitset_init(&pcc_vthread_io_waitset, backend) != 0) {
        if (backend == PCC_IO_WAITSET_BACKEND_POLL) return -1;
        backend = PCC_IO_WAITSET_BACKEND_POLL;
        if (pcc_io_waitset_init(&pcc_vthread_io_waitset, backend) != 0) {
            return -1;
        }
    }
    pcc_vthread_io_backend_value =
        backend == PCC_IO_WAITSET_BACKEND_KQUEUE
            ? PCC_VTHREAD_IO_BACKEND_KQUEUE
            : (backend == PCC_IO_WAITSET_BACKEND_EPOLL
                ? PCC_VTHREAD_IO_BACKEND_EPOLL
                : PCC_VTHREAD_IO_BACKEND_POLL);
    pcc_vthread_io_wait_active = 0;
    pcc_vthread_io_waitset_ready = 1;
    /* A previous carrier-pool stop intentionally preserved these roots while
     * disposing the kernel owner.  Recreate every aggregate fd registration
     * before allowing the restarted pool to run. */
    PccVirtualThreadPollEntry *entry = pcc_vthread_poll_queue;
    while (entry != NULL) {
        if (pcc_vthread_io_refresh_registered_fd_locked(entry->fd) != 0) {
            (void)pcc_vthread_io_waitset_dispose_locked();
            return -1;
        }
        entry = entry->next;
    }
    return 0;
}

static int64_t pcc_vthread_io_interest(int64_t events) {
    return events == 0 ? (int64_t)POLLIN : events;
}

static int pcc_vthread_io_interrupt_locked(void) {
    if (!pcc_vthread_io_wait_active || !pcc_vthread_io_waitset_ready) return 0;
    return pcc_io_waitset_interrupt(&pcc_vthread_io_waitset);
}

/* Recompute the one waitset registration for fd from every per-vthread entry
 * that waits on it. This preserves the old API's same-fd multi-waiter
 * semantics while kqueue/poll own only one kernel registration per fd. */
static int pcc_vthread_io_refresh_registered_fd_locked(int64_t fd) {
    int found = 0;
    int64_t interest = 0;
    int64_t deadline = -1;
    PccVirtualThreadPollEntry *entry = pcc_vthread_poll_queue;
    while (entry != NULL) {
        if (entry->fd == fd) {
            found = 1;
            interest |= pcc_vthread_io_interest(entry->events);
            if (
                entry->deadline_ms >= 0
                && (deadline < 0 || entry->deadline_ms < deadline)
            ) {
                deadline = entry->deadline_ms;
            }
        }
        entry = entry->next;
    }
    int rc = 0;
    if (!found) {
        (void)pcc_io_waitset_remove(&pcc_vthread_io_waitset, fd);
    } else {
        rc = pcc_io_waitset_add(
            &pcc_vthread_io_waitset, fd, interest, deadline, 0
        );
    }
    if (pcc_vthread_io_interrupt_locked() != 0) rc = -1;
    return rc;
}

static int pcc_vthread_io_refresh_fd_locked(int64_t fd) {
    if (pcc_vthread_io_waitset_ensure_locked() != 0) return -1;
    return pcc_vthread_io_refresh_registered_fd_locked(fd);
}

/* Remove one vthread's active IO wait immediately. The per-vthread root node
 * is retired before returning; the aggregate kernel registration is then
 * refreshed for any other waiters on the same fd. Caller holds the scheduler
 * lock. */
static int pcc_vthread_poll_cancel_locked(PyVirtualThreadObject *vt) {
    PccVirtualThreadPollEntry *entry =
        (PccVirtualThreadPollEntry *)vt->io_entry;
    if (entry == NULL) return 0;
    PccVirtualThreadPollEntry **cur = &pcc_vthread_poll_queue;
    while (*cur != NULL && *cur != entry) cur = &(*cur)->next;
    if (*cur == NULL) return -1;
    int64_t fd = entry->fd;
    *cur = entry->next;
    if (pcc_vthread_io_wait_count_value > 0) {
        pcc_vthread_io_wait_count_value--;
    }
    vt->io_entry = NULL;
    vt->queued = 0;
    if (
        vt->wait_kind == PCC_VTHREAD_WAIT_IO_READ
        || vt->wait_kind == PCC_VTHREAD_WAIT_IO_WRITE
    ) {
        vt->wait_kind = PCC_VTHREAD_WAIT_NONE;
    }
    pcc_vthread_poll_entry_release_locked(entry);
    pcc_vthread_effect_emit(
        PCC_VTHREAD_EFFECT_CANCEL_IO,
        PCC_VTHREAD_NODE_IO,
        0,
        vt->state
    );
    return pcc_vthread_io_refresh_fd_locked(fd) == 0 ? 1 : -1;
}

static PccVirtualThreadIoResource *pcc_vthread_io_resource_find_locked(
    int64_t fd,
    PccVirtualThreadIoResource ***link_out
) {
    PccVirtualThreadIoResource **link = &pcc_vthread_io_resources;
    while (*link != NULL && (*link)->fd != fd) link = &(*link)->next;
    if (link_out != NULL) *link_out = link;
    return *link;
}

/* Wake and retire every still-parked waiter before its fd is closed.  Ready
 * nodes are reserved for every valid waiter before mutating the IO list, so an
 * allocation failure leaves the original wait registrations intact.  A task
 * that was already made ready is protected by the resource-generation check
 * around its next nonblocking operation.  Caller holds the scheduler lock. */
static int pcc_vthread_io_close_waiters_locked(int64_t fd) {
    PccVirtualThreadQueueEntry *reserved = NULL;
    PccVirtualThreadQueueEntry *reserved_tail = NULL;
    PccVirtualThreadPollEntry *scan = pcc_vthread_poll_queue;
    while (scan != NULL) {
        PyVirtualThreadObject *vt = NULL;
        PyObject *thread = scan->thread;
        if (
            scan->fd == fd
            && thread != NULL
            && !PY_IS_TAGGED_INT(thread)
            && py_type_of(thread) == PY_TYPE_VIRTUAL_THREAD
        ) {
            vt = (PyVirtualThreadObject *)thread;
        }
        if (
            vt != NULL
            && vt->io_entry == scan
            && vt->state == PCC_VTHREAD_PARKED
            && (
                vt->wait_kind == PCC_VTHREAD_WAIT_IO_READ
                || vt->wait_kind == PCC_VTHREAD_WAIT_IO_WRITE
            )
        ) {
            PccVirtualThreadQueueEntry *ready =
                pcc_vthread_prepare_ready_entry_locked(thread);
            if (ready == NULL) {
                while (reserved != NULL) {
                    PccVirtualThreadQueueEntry *next = reserved->next;
                    pcc_vthread_ready_entry_release_locked(reserved);
                    reserved = next;
                }
                return -1;
            }
            ready->next = NULL;
            if (reserved_tail == NULL) reserved = ready;
            else reserved_tail->next = ready;
            reserved_tail = ready;
        }
        scan = scan->next;
    }

    PccVirtualThreadPollEntry **link = &pcc_vthread_poll_queue;
    while (*link != NULL) {
        PccVirtualThreadPollEntry *entry = *link;
        if (entry->fd != fd) {
            link = &entry->next;
            continue;
        }
        PyVirtualThreadObject *vt = NULL;
        PyObject *thread = entry->thread;
        if (
            thread != NULL
            && !PY_IS_TAGGED_INT(thread)
            && py_type_of(thread) == PY_TYPE_VIRTUAL_THREAD
        ) {
            vt = (PyVirtualThreadObject *)thread;
        }
        int valid = (
            vt != NULL
            && vt->io_entry == entry
            && vt->state == PCC_VTHREAD_PARKED
            && (
                vt->wait_kind == PCC_VTHREAD_WAIT_IO_READ
                || vt->wait_kind == PCC_VTHREAD_WAIT_IO_WRITE
            )
        );
        *link = entry->next;
        if (pcc_vthread_io_wait_count_value > 0) {
            pcc_vthread_io_wait_count_value--;
        }
        if (valid) {
            PccVirtualThreadQueueEntry *ready = reserved;
            reserved = ready->next;
            ready->next = NULL;
            vt->io_entry = NULL;
            vt->wait_kind = PCC_VTHREAD_WAIT_NONE;
            pcc_vthread_publish_ready_entry_locked(vt, ready);
            pcc_vthread_effect_emit(
                PCC_VTHREAD_EFFECT_IO_WAKE,
                PCC_VTHREAD_NODE_IO,
                0,
                vt->state
            );
        } else if (vt != NULL && vt->io_entry == entry) {
            vt->io_entry = NULL;
            if (
                vt->wait_kind == PCC_VTHREAD_WAIT_IO_READ
                || vt->wait_kind == PCC_VTHREAD_WAIT_IO_WRITE
            ) {
                vt->wait_kind = PCC_VTHREAD_WAIT_NONE;
            }
        }
        pcc_vthread_poll_entry_release_locked(entry);
    }
    while (reserved != NULL) {
        PccVirtualThreadQueueEntry *next = reserved->next;
        pcc_vthread_ready_entry_release_locked(reserved);
        reserved = next;
    }
    if (pcc_vthread_io_waitset_ready) {
        (void)pcc_io_waitset_remove(&pcc_vthread_io_waitset, fd);
        (void)pcc_vthread_io_interrupt_locked();
    }
    return 0;
}

static int pcc_vthread_live_pollfds_reserve_locked(int64_t need) {
    if (need <= pcc_vthread_live_pollfds_cap) return 0;
    int64_t cap = pcc_vthread_live_pollfds_cap > 0
        ? pcc_vthread_live_pollfds_cap : 8;
    while (cap < need) cap *= 2;
    struct pollfd *grown = (struct pollfd *)realloc(
        pcc_vthread_live_pollfds,
        (size_t)cap * sizeof(struct pollfd)
    );
    if (grown == NULL) return -1;
    pcc_vthread_live_pollfds = grown;
    pcc_vthread_live_pollfds_cap = cap;
    return 0;
}

/* Portable production fallback: one poll(2) call over the waitset's unique
 * live fds, then feed the returned level state into the common waitset drain.
 * This replaces the old one-poll-syscall-per-vthread linked-list walk. */
static int pcc_vthread_io_feed_poll_locked(int64_t timeout_ms) {
    int64_t count = pcc_io_waitset_count(&pcc_vthread_io_waitset);
    if (count <= 0) return 0;
    if (pcc_vthread_live_pollfds_reserve_locked(count) != 0) return -1;
    int64_t n = 0;
    for (int64_t i = 0; i < pcc_vthread_io_waitset.len; i++) {
        PccIoWaitSlot *slot = &pcc_vthread_io_waitset.slots[i];
        if (slot->state != 1) continue;
        if (slot->fd < 0 || slot->fd > INT_MAX) return -1;
        pcc_vthread_live_pollfds[n].fd = (int)slot->fd;
        pcc_vthread_live_pollfds[n].events = (short)slot->interest;
        pcc_vthread_live_pollfds[n].revents = 0;
        n++;
    }
    int timeout = 0;
    if (timeout_ms < 0) timeout = -1;
    else if (timeout_ms > INT_MAX) timeout = INT_MAX;
    else timeout = (int)timeout_ms;
    int rc = poll(pcc_vthread_live_pollfds, (nfds_t)n, timeout);
    if (rc < 0) return errno == EINTR ? 0 : -1;
    for (int64_t i = 0; i < n; i++) {
        if (pcc_vthread_live_pollfds[i].revents == 0) continue;
        pcc_io_waitset_set_ready(
            &pcc_vthread_io_waitset,
            (int64_t)pcc_vthread_live_pollfds[i].fd,
            (int64_t)pcc_vthread_live_pollfds[i].revents
        );
    }
    return 0;
}

static int pcc_vthread_timer_add_locked(
    PyObject *vthread,
    int64_t deadline_ms
) {
    PyVirtualThreadObject *vt = (PyVirtualThreadObject *)vthread;
    if (pcc_vthread_timer_cancel_locked(vt) < 0) return -1;
    if (pcc_vthread_timer_heap_ensure_locked() != 0) return -1;
    PccVirtualThreadTimerEntry *entry = pcc_vthread_timer_entry_alloc_locked();
    if (entry == NULL) return -1;
    entry->root_handle = pcc_gc_scheduler_root_register_handle(&entry->thread);
    if (entry->root_handle == NULL) {
        pcc_vthread_timer_entry_recycle_locked(entry);
        return -1;
    }
    pcc_vthread_effect_emit(
        PCC_VTHREAD_EFFECT_ROOT_ENTER,
        PCC_VTHREAD_NODE_TIMER,
        1,
        -1
    );
    pcc_gc_store_root(&entry->thread, vthread);
    entry->deadline_ms = deadline_ms;
    /* O(log n) sift-up into the min-heap, keyed on (deadline_ms, seq); the seq
     * tiebreak reproduces the old sorted-list FIFO-among-equal-deadlines walk
     * (`<= deadline_ms`). The heap stores the entry's stable address as the
     * opaque timer id. */
    if (pcc_timer_heap_insert(
            &pcc_vthread_timer_heap,
            deadline_ms,
            pcc_vthread_timer_entry_id(entry)
        ) != 0) {
        pcc_vthread_timer_entry_release_locked(entry);
        return -1;
    }
    vt->timer_entry = entry;
    vt->queued = 1;
    vt->wait_kind = PCC_VTHREAD_WAIT_TIMER;
    pcc_vthread_effect_emit(
        PCC_VTHREAD_EFFECT_TIMER_PARK,
        PCC_VTHREAD_NODE_TIMER,
        0,
        vt->state
    );
    return 0;
}

static int pcc_vthread_poll_add_locked(
    PyObject *vthread,
    int64_t fd,
    int64_t events,
    int64_t deadline_ms
) {
    PyVirtualThreadObject *vt = (PyVirtualThreadObject *)vthread;
    if (pcc_vthread_poll_cancel_locked(vt) < 0) return -1;
    if (pcc_vthread_io_waitset_ensure_locked() != 0) return -1;
    PccVirtualThreadPollEntry *entry = pcc_vthread_poll_entry_alloc_locked();
    if (entry == NULL) return -1;
    entry->root_handle = pcc_gc_scheduler_root_register_handle(&entry->thread);
    if (entry->root_handle == NULL) {
        pcc_vthread_poll_entry_recycle_locked(entry);
        return -1;
    }
    pcc_vthread_effect_emit(
        PCC_VTHREAD_EFFECT_ROOT_ENTER,
        PCC_VTHREAD_NODE_IO,
        1,
        -1
    );
    pcc_gc_store_root(&entry->thread, vthread);
    entry->fd = fd;
    entry->events = events;
    entry->deadline_ms = deadline_ms;
    entry->next = pcc_vthread_poll_queue;
    pcc_vthread_poll_queue = entry;
    if (pcc_vthread_io_refresh_fd_locked(fd) != 0) {
        pcc_vthread_poll_queue = entry->next;
        pcc_vthread_poll_entry_release_locked(entry);
        (void)pcc_vthread_io_refresh_fd_locked(fd);
        return -1;
    }
    vt->io_entry = entry;
    vt->queued = 1;
    vt->wait_kind = (events & POLLOUT) != 0
        ? PCC_VTHREAD_WAIT_IO_WRITE
        : PCC_VTHREAD_WAIT_IO_READ;
    pcc_vthread_io_wait_count_value++;
    pcc_vthread_effect_emit(
        PCC_VTHREAD_EFFECT_IO_PARK,
        PCC_VTHREAD_NODE_IO,
        0,
        vt->state
    );
    return 0;
}

static int pcc_vthread_fd_ready(
    int64_t fd,
    int64_t events,
    int64_t timeout_ms
) {
    if (fd < 0 || fd > INT_MAX) return -1;
    if (events == 0) events = POLLIN;
    int timeout = 0;
    if (timeout_ms < 0) {
        timeout = -1;
    } else if (timeout_ms > INT_MAX) {
        timeout = INT_MAX;
    } else {
        timeout = (int)timeout_ms;
    }
    struct pollfd pfd;
    pfd.fd = (int)fd;
    pfd.events = (short)events;
    pfd.revents = 0;
    int rc = poll(&pfd, 1, timeout);
    if (rc < 0) return -1;
    if (rc == 0) return 0;
    return (pfd.revents & (events | POLLERR | POLLHUP | POLLNVAL)) != 0 ? 1 : 0;
}

PyObject *py_virtual_thread_new(PyObject *continuation) {
    PyVirtualThreadObject *vt = (PyVirtualThreadObject *)pcc_gc_alloc(
        (int64_t)sizeof(PyVirtualThreadObject),
        PY_TYPE_VIRTUAL_THREAD,
        0
    );
    if (vt == NULL) return NULL;
    vt->continuation = NULL;
    vt->result = NULL;
    vt->state = PCC_VTHREAD_NEW;
    vt->queued = 0;
    vt->pinned = 0;
    vt->timer_entry = NULL;
    vt->io_entry = NULL;
    vt->exception = NULL;
    vt->outcome = PCC_VTHREAD_OUTCOME_PENDING;
    vt->join_waiters = NULL;
    vt->join_wait_tail = NULL;
    vt->join_entry = NULL;
    vt->join_target = NULL;
    vt->wait_kind = PCC_VTHREAD_WAIT_NONE;
    vt->cancel_requested = 0;
    vt->channel_owner_a = NULL;
    vt->channel_owner_b = NULL;
    vt->channel_arm_a = NULL;
    vt->channel_arm_b = NULL;
    vt->channel_value = NULL;
    vt->channel_status = -1;
    vt->channel_index = -1;
    pcc_gc_store_ptr(
        (PyObject *)vt,
        &vt->continuation,
        continuation == NULL ? py_None : continuation
    );
    py_gc_track((PyObject *)vt);
    return (PyObject *)vt;
}

int64_t py_virtual_thread_start(PyObject *vthread) {
    PyVirtualThreadObject *vt = checked_vthread(vthread);
    if (vt == NULL) return -1;
    if (vt->state == PCC_VTHREAD_DONE) return -1;
    if (pcc_vthread_scheduler_init() != 0) return -1;
    if (pcc_mutex_lock(pcc_vthread_lock) != 0) return -1;
    if (vt->state == PCC_VTHREAD_DONE) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return -1;
    }
    if (
        vt->wait_kind == PCC_VTHREAD_WAIT_JOIN
        || vt->join_entry != NULL
    ) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return -1;
    }
    if (pcc_vthread_timer_cancel_locked(vt) < 0) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return -1;
    }
    if (pcc_vthread_poll_cancel_locked(vt) < 0) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return -1;
    }
    if (vt->state == PCC_VTHREAD_NEW || vt->state == PCC_VTHREAD_PARKED) {
        vt->state = PCC_VTHREAD_READY;
    }
    int rc = pcc_vthread_enqueue_locked((PyObject *)vt);
    if (rc == 0) {
        pcc_vthread_effect_emit(
            PCC_VTHREAD_EFFECT_START, 0, 0, vt->state
        );
    }
    (void)pcc_mutex_unlock(pcc_vthread_lock);
    return rc == 0 ? 0 : -1;
}

int64_t py_virtual_thread_park(PyObject *vthread) {
    PyVirtualThreadObject *vt = checked_vthread(vthread);
    if (vt == NULL || vt->state == PCC_VTHREAD_DONE) return -1;
    if (pcc_vthread_scheduler_init() != 0) return -1;
    if (pcc_mutex_lock(pcc_vthread_lock) != 0) return -1;
    if (vt->state == PCC_VTHREAD_DONE) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return -1;
    }
    vt->state = PCC_VTHREAD_PARKED;
    pcc_vthread_effect_emit(PCC_VTHREAD_EFFECT_PARK, 0, 0, vt->state);
    (void)pcc_mutex_unlock(pcc_vthread_lock);
    return 0;
}

int64_t py_virtual_thread_unpark(PyObject *vthread) {
    PyVirtualThreadObject *vt = checked_vthread(vthread);
    if (vt == NULL || vt->state == PCC_VTHREAD_DONE) return -1;
    if (pcc_vthread_scheduler_init() != 0) return -1;
    if (pcc_mutex_lock(pcc_vthread_lock) != 0) return -1;
    if (vt->state == PCC_VTHREAD_DONE) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return -1;
    }
    if (
        vt->wait_kind == PCC_VTHREAD_WAIT_JOIN
        || vt->join_entry != NULL
    ) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return -1;
    }
    if (pcc_vthread_timer_cancel_locked(vt) < 0) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return -1;
    }
    if (pcc_vthread_poll_cancel_locked(vt) < 0) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return -1;
    }
    int rc = pcc_vthread_make_ready_locked(vt);
    if (rc == 0) {
        pcc_vthread_effect_emit(
            PCC_VTHREAD_EFFECT_UNPARK, 0, 0, vt->state
        );
    }
    (void)pcc_mutex_unlock(pcc_vthread_lock);
    return rc == 0 ? 0 : -1;
}

int64_t py_virtual_thread_sleep(PyObject *vthread, int64_t delay_ms) {
    PyVirtualThreadObject *vt = checked_vthread(vthread);
    if (vt == NULL || vt->state == PCC_VTHREAD_DONE) return -1;
    if (pcc_vthread_scheduler_init() != 0) return -1;
    if (pcc_mutex_lock(pcc_vthread_lock) != 0) return -1;
    if (vt->state == PCC_VTHREAD_DONE) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return -1;
    }
    if (
        vt->wait_kind == PCC_VTHREAD_WAIT_JOIN
        || vt->join_entry != NULL
    ) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return -1;
    }
    if (pcc_vthread_poll_cancel_locked(vt) < 0) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return -1;
    }
    int rc = 0;
    if (delay_ms <= 0) {
        rc = pcc_vthread_timer_cancel_locked(vt);
        if (rc >= 0) rc = pcc_vthread_make_ready_locked(vt);
        if (rc == 0) {
            pcc_vthread_effect_emit(
                PCC_VTHREAD_EFFECT_UNPARK, 0, 0, vt->state
            );
        }
    } else {
        vt->state = PCC_VTHREAD_PARKED;
        rc = pcc_vthread_timer_add_locked(
            (PyObject *)vt,
            pcc_vthread_now_ms() + delay_ms
        );
    }
    (void)pcc_mutex_unlock(pcc_vthread_lock);
    return rc == 0 ? 0 : -1;
}

int64_t py_virtual_thread_cancel_timer(PyObject *vthread) {
    PyVirtualThreadObject *vt = checked_vthread(vthread);
    if (vt == NULL) return -1;
    if (vt->timer_entry == NULL) return 0;
    if (pcc_vthread_scheduler_init() != 0) return -1;
    if (pcc_mutex_lock(pcc_vthread_lock) != 0) return -1;
    if (vt->state == PCC_VTHREAD_DONE) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return -1;
    }
    int rc = pcc_vthread_timer_cancel_locked(vt);
    (void)pcc_mutex_unlock(pcc_vthread_lock);
    return rc;
}

int64_t py_virtual_thread_poll_timers(void) {
    if (pcc_vthread_scheduler_init() != 0) return 0;
    int64_t now = pcc_vthread_now_ms();
    int64_t woken = 0;
    if (pcc_mutex_lock(pcc_vthread_lock) != 0) return 0;
    if (!pcc_vthread_timer_heap_ready) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return 0;
    }
    int allocation_failed = 0;
    /* Reserve a ready node/root before removing each due id from the timer
     * heap. A failed reservation leaves the heap registration and timer root
     * untouched, so a later poll can retry instead of losing the wake. */
    for (;;) {
        int64_t deadline = 0;
        if (
            pcc_timer_heap_peek(&pcc_vthread_timer_heap, &deadline) != 1
            || deadline > now
        ) {
            break;
        }
        PccVirtualThreadQueueEntry *ready_entry =
            pcc_vthread_prepare_ready_entry_locked(NULL);
        if (ready_entry == NULL) {
            allocation_failed = 1;
            break;
        }
        int64_t timer_id = 0;
        int64_t got = pcc_timer_heap_pop_expired(
            &pcc_vthread_timer_heap, now, &timer_id, 1
        );
        if (got != 1) {
            pcc_vthread_ready_entry_release_locked(ready_entry);
            break;
        }
        PccVirtualThreadTimerEntry *entry =
            pcc_vthread_timer_entry_from_id(timer_id);
        PyObject *thread = entry->thread;
        PyVirtualThreadObject *vt = NULL;
        if (
            thread != NULL
            && !PY_IS_TAGGED_INT(thread)
            && py_type_of(thread) == PY_TYPE_VIRTUAL_THREAD
        ) {
            vt = (PyVirtualThreadObject *)thread;
        }
        int valid = (
            vt != NULL
            && vt->timer_entry == entry
            && vt->state == PCC_VTHREAD_PARKED
            && vt->wait_kind == PCC_VTHREAD_WAIT_TIMER
        );
        if (valid) {
            /* Transfer retention before detaching the timer/root. */
            pcc_gc_store_root(&ready_entry->thread, thread);
            vt->timer_entry = NULL;
            vt->wait_kind = PCC_VTHREAD_WAIT_NONE;
            pcc_vthread_publish_ready_entry_locked(vt, ready_entry);
            woken++;
            pcc_vthread_effect_emit(
                PCC_VTHREAD_EFFECT_TIMER_WAKE,
                PCC_VTHREAD_NODE_TIMER,
                0,
                vt->state
            );
        } else {
            if (vt != NULL && vt->timer_entry == entry) {
                vt->timer_entry = NULL;
                if (vt->wait_kind == PCC_VTHREAD_WAIT_TIMER) {
                    vt->wait_kind = PCC_VTHREAD_WAIT_NONE;
                }
            }
            pcc_vthread_ready_entry_release_locked(ready_entry);
        }
        pcc_vthread_timer_entry_release_locked(entry);
    }
    (void)pcc_mutex_unlock(pcc_vthread_lock);
    return allocation_failed ? -1 : woken;
}

int64_t py_virtual_thread_timer_count(void) {
    if (pcc_vthread_scheduler_init() != 0) return 0;
    if (pcc_mutex_lock(pcc_vthread_lock) != 0) return 0;
    /* Registered (not-yet-expired) timers = the heap's live-set size. */
    int64_t count =
        pcc_vthread_timer_heap_ready
            ? pcc_timer_heap_size(&pcc_vthread_timer_heap)
            : 0;
    (void)pcc_mutex_unlock(pcc_vthread_lock);
    return count;
}

int64_t py_virtual_thread_io_resource_register(int64_t fd) {
    if (fd < 0) return -1;
    if (pcc_vthread_scheduler_init() != 0) return -1;
    if (pcc_mutex_lock(pcc_vthread_lock) != 0) return -1;
    if (
        pcc_vthread_io_resource_find_locked(fd, NULL) != NULL
        || pcc_vthread_io_resource_generation == INT64_MAX
    ) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return -1;
    }
    PccVirtualThreadIoResource *resource =
        (PccVirtualThreadIoResource *)malloc(sizeof(*resource));
    if (resource == NULL) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return -1;
    }
    resource->fd = fd;
    resource->generation = ++pcc_vthread_io_resource_generation;
    resource->next = pcc_vthread_io_resources;
    pcc_vthread_io_resources = resource;
    int64_t generation = resource->generation;
    (void)pcc_mutex_unlock(pcc_vthread_lock);
    return generation;
}

int64_t py_virtual_thread_io_resource_generation(int64_t fd) {
    if (fd < 0 || pcc_vthread_scheduler_init() != 0) {
        py_raise_owned(py_exc_new(PY_EXC_OSERROR, "TCP descriptor is not open"));
        return -1;
    }
    if (pcc_mutex_lock(pcc_vthread_lock) != 0) return -1;
    PccVirtualThreadIoResource *resource =
        pcc_vthread_io_resource_find_locked(fd, NULL);
    int64_t generation = resource == NULL ? -1 : resource->generation;
    (void)pcc_mutex_unlock(pcc_vthread_lock);
    if (generation < 0) {
        py_raise_owned(py_exc_new(PY_EXC_OSERROR, "TCP descriptor is not open"));
    }
    return generation;
}

int64_t py_virtual_thread_io_resource_operation_begin(
    int64_t fd,
    int64_t generation
) {
    if (fd < 0 || generation <= 0 || pcc_vthread_scheduler_init() != 0) {
        py_raise_owned(py_exc_new(PY_EXC_OSERROR, "TCP descriptor was closed"));
        return -1;
    }
    if (pcc_mutex_lock(pcc_vthread_lock) != 0) return -1;
    PccVirtualThreadIoResource *resource =
        pcc_vthread_io_resource_find_locked(fd, NULL);
    if (resource == NULL || resource->generation != generation) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        py_raise_owned(py_exc_new(PY_EXC_OSERROR, "TCP descriptor was closed"));
        return -1;
    }
    /* Success deliberately returns with pcc_vthread_lock held. */
    return 0;
}

void py_virtual_thread_io_resource_operation_end(void) {
    if (pcc_vthread_lock != NULL) (void)pcc_mutex_unlock(pcc_vthread_lock);
}

int64_t py_virtual_thread_io_resource_close_begin(int64_t fd) {
    if (fd < 0 || pcc_vthread_scheduler_init() != 0) return -1;
    if (pcc_mutex_lock(pcc_vthread_lock) != 0) return -1;
    PccVirtualThreadIoResource **link = NULL;
    PccVirtualThreadIoResource *resource =
        pcc_vthread_io_resource_find_locked(fd, &link);
    if (resource == NULL || link == NULL) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return -1;
    }
    if (pcc_vthread_io_close_waiters_locked(fd) != 0) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return -1;
    }
    *link = resource->next;
    free(resource);
    /* Success deliberately returns with pcc_vthread_lock held until close. */
    return 0;
}

int64_t py_virtual_thread_block_on_fd_generation(
    PyObject *vthread,
    int64_t fd,
    int64_t generation,
    int64_t events,
    int64_t timeout_ms
) {
    PyVirtualThreadObject *vt = checked_vthread(vthread);
    if (
        vt == NULL || vt->state == PCC_VTHREAD_DONE
        || fd < 0 || generation <= 0
    ) return -1;
    if (pcc_vthread_scheduler_init() != 0) return -1;
    if (pcc_mutex_lock(pcc_vthread_lock) != 0) return -1;
    PccVirtualThreadIoResource *resource =
        pcc_vthread_io_resource_find_locked(fd, NULL);
    if (resource == NULL || resource->generation != generation) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        py_raise_owned(py_exc_new(PY_EXC_OSERROR, "TCP descriptor was closed"));
        return -1;
    }
    if (
        vt->state == PCC_VTHREAD_DONE
        || vt->wait_kind == PCC_VTHREAD_WAIT_JOIN
        || vt->join_entry != NULL
    ) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return -1;
    }
    int ready = pcc_vthread_fd_ready(fd, events, 0);
    if (ready < 0) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return -1;
    }
    if (
        pcc_vthread_timer_cancel_locked(vt) < 0
        || pcc_vthread_poll_cancel_locked(vt) < 0
    ) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return -1;
    }
    if (ready != 0) {
        if (vt->state == PCC_VTHREAD_RUNNING) {
            (void)pcc_mutex_unlock(pcc_vthread_lock);
            return 1;
        }
        int rc = pcc_vthread_make_ready_locked(vt);
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return rc == 0 ? 1 : -1;
    }
    vt->state = PCC_VTHREAD_PARKED;
    int64_t deadline = timeout_ms >= 0
        ? pcc_vthread_now_ms() + timeout_ms
        : -1;
    int rc = pcc_vthread_poll_add_locked(
        (PyObject *)vt, fd, events, deadline
    );
    (void)pcc_mutex_unlock(pcc_vthread_lock);
    return rc == 0 ? 0 : -1;
}

int64_t py_virtual_thread_block_on_fd(
    PyObject *vthread,
    int64_t fd,
    int64_t events,
    int64_t timeout_ms
) {
    PyVirtualThreadObject *vt = checked_vthread(vthread);
    if (vt == NULL || vt->state == PCC_VTHREAD_DONE) return -1;
    if (pcc_vthread_scheduler_init() != 0) return -1;
    if (pcc_mutex_lock(pcc_vthread_lock) != 0) return -1;
    if (vt->state == PCC_VTHREAD_DONE) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return -1;
    }
    if (
        vt->wait_kind == PCC_VTHREAD_WAIT_JOIN
        || vt->join_entry != NULL
    ) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return -1;
    }
    int ready = pcc_vthread_fd_ready(fd, events, 0);
    if (ready < 0) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return -1;
    }
    if (
        pcc_vthread_timer_cancel_locked(vt) < 0
        || pcc_vthread_poll_cancel_locked(vt) < 0
    ) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return -1;
    }
    int rc = 0;
    if (ready != 0) {
        if (vt->state == PCC_VTHREAD_RUNNING) {
            (void)pcc_mutex_unlock(pcc_vthread_lock);
            return 1;
        }
        rc = pcc_vthread_make_ready_locked(vt);
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return rc == 0 ? 1 : -1;
    }
    vt->state = PCC_VTHREAD_PARKED;
    int64_t deadline = timeout_ms >= 0 ? pcc_vthread_now_ms() + timeout_ms : -1;
    rc = pcc_vthread_poll_add_locked((PyObject *)vt, fd, events, deadline);
    (void)pcc_mutex_unlock(pcc_vthread_lock);
    return rc == 0 ? 0 : -1;
}

static int64_t pcc_vthread_io_result_bits(
    const PccIoWaitResult *result,
    int64_t fd
) {
    int64_t bits = 0;
    for (int64_t i = 0; i < result->ready_len; i++) {
        if (result->ready[i].fd == fd) bits |= result->ready[i].events;
    }
    return bits;
}

int64_t py_virtual_thread_poll_io(int64_t timeout_ms) {
    if (pcc_vthread_scheduler_init() != 0) return -1;
    if (pcc_mutex_lock(pcc_vthread_lock) != 0) return -1;
    if (timeout_ms == 0 && pcc_vthread_io_wait_count_value == 0) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return 0;
    }
    if (pcc_vthread_io_waitset_ensure_locked() != 0) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return -1;
    }
    PccIoWaitResult result;
    int64_t now = pcc_vthread_now_ms();
    int64_t wait_deadline = now;
    if (pcc_vthread_io_backend_value == PCC_VTHREAD_IO_BACKEND_POLL) {
        if (pcc_vthread_io_feed_poll_locked(timeout_ms < 0 ? 0 : timeout_ms) != 0) {
            (void)pcc_mutex_unlock(pcc_vthread_lock);
            return -1;
        }
        if (pcc_io_waitset_wait_until(
            &pcc_vthread_io_waitset, now, wait_deadline, &result
        ) != 0) {
            (void)pcc_mutex_unlock(pcc_vthread_lock);
            return -1;
        }
    } else {
        /* One kernel waiter is enough for a shared readiness notifier. Other
         * carriers keep running scheduler work instead of entering a second
         * wait or contending on one event batch. */
        if (pcc_vthread_io_wait_active) {
            (void)pcc_mutex_unlock(pcc_vthread_lock);
            return 0;
        }
        wait_deadline = timeout_ms < 0 ? -1 : now + timeout_ms;
        PccIoWaitBatch batch;
        if (pcc_io_waitset_wait_prepare(
            &pcc_vthread_io_waitset, now, wait_deadline, &batch
        ) != 0) {
            (void)pcc_mutex_unlock(pcc_vthread_lock);
            return -1;
        }
        pcc_vthread_io_wait_active = 1;
        (void)pcc_mutex_unlock(pcc_vthread_lock);

        (void)pcc_io_waitset_wait_block(&pcc_vthread_io_waitset, &batch);

        if (pcc_mutex_lock(pcc_vthread_lock) != 0) {
            __atomic_store_n(&pcc_vthread_io_wait_active, 0, __ATOMIC_RELEASE);
            pcc_io_waitset_wait_discard(&batch);
            return -1;
        }
        pcc_vthread_io_wait_active = 0;
        if (pcc_io_waitset_wait_finish(
            &pcc_vthread_io_waitset, &batch, &result
        ) != 0) {
            (void)pcc_mutex_unlock(pcc_vthread_lock);
            return -1;
        }
    }
    now = pcc_vthread_now_ms();

    int64_t woken = 0;
    int allocation_failed = 0;
    PccVirtualThreadPollEntry **cur = &pcc_vthread_poll_queue;
    while (*cur != NULL) {
        PccVirtualThreadPollEntry *entry = *cur;
        int64_t ready_bits = pcc_vthread_io_result_bits(&result, entry->fd);
        int64_t hit = ready_bits & (
            pcc_vthread_io_interest(entry->events)
            | (int64_t)PCC_IO_ALWAYS_REPORTED
        );
        int expired = entry->deadline_ms >= 0 && entry->deadline_ms <= now;
        if (hit == 0 && !expired) {
            cur = &(*cur)->next;
            continue;
        }
        PyObject *thread = entry->thread;
        PyVirtualThreadObject *vt = NULL;
        if (
            thread != NULL
            && !PY_IS_TAGGED_INT(thread)
            && py_type_of(thread) == PY_TYPE_VIRTUAL_THREAD
        ) {
            vt = (PyVirtualThreadObject *)thread;
        }
        int valid = (
            vt != NULL
            && vt->io_entry == entry
            && vt->state == PCC_VTHREAD_PARKED
            && (
                vt->wait_kind == PCC_VTHREAD_WAIT_IO_READ
                || vt->wait_kind == PCC_VTHREAD_WAIT_IO_WRITE
            )
        );
        PccVirtualThreadQueueEntry *ready_entry = NULL;
        if (valid) {
            /* The waitset drain is one-shot, but this per-task wait/root stays
             * linked until its replacement ready root is reserved. On OOM the
             * refresh pass below re-arms the retained entry. */
            ready_entry = pcc_vthread_prepare_ready_entry_locked(thread);
            if (ready_entry == NULL) {
                allocation_failed = 1;
                cur = &(*cur)->next;
                continue;
            }
        }
        *cur = entry->next;
        if (pcc_vthread_io_wait_count_value > 0) {
            pcc_vthread_io_wait_count_value--;
        }
        if (valid) {
            vt->io_entry = NULL;
            vt->wait_kind = PCC_VTHREAD_WAIT_NONE;
            pcc_vthread_publish_ready_entry_locked(vt, ready_entry);
            woken++;
            pcc_vthread_effect_emit(
                PCC_VTHREAD_EFFECT_IO_WAKE,
                PCC_VTHREAD_NODE_IO,
                0,
                vt->state
            );
        } else if (vt != NULL && vt->io_entry == entry) {
            vt->io_entry = NULL;
            if (
                vt->wait_kind == PCC_VTHREAD_WAIT_IO_READ
                || vt->wait_kind == PCC_VTHREAD_WAIT_IO_WRITE
            ) {
                vt->wait_kind = PCC_VTHREAD_WAIT_NONE;
            }
        }
        pcc_vthread_poll_entry_release_locked(entry);
    }

    /* wait() removes every delivered/timed-out aggregate fd registration.
     * Re-arm only those fds that still have later or differently-interested
     * vthread entries. */
    int refresh_failed = 0;
    for (int64_t i = 0; i < result.ready_len; i++) {
        if (pcc_vthread_io_refresh_fd_locked(result.ready[i].fd) != 0) {
            refresh_failed = 1;
        }
    }
    for (int64_t i = 0; i < result.timeout_len; i++) {
        if (pcc_vthread_io_refresh_fd_locked(result.timed_out[i]) != 0) {
            refresh_failed = 1;
        }
    }
    (void)pcc_mutex_unlock(pcc_vthread_lock);
    return (refresh_failed || allocation_failed) ? -1 : woken;
}

int64_t py_virtual_thread_io_wait_count(void) {
    if (pcc_vthread_scheduler_init() != 0) return 0;
    if (pcc_mutex_lock(pcc_vthread_lock) != 0) return 0;
    int64_t count = pcc_vthread_io_wait_count_value;
    (void)pcc_mutex_unlock(pcc_vthread_lock);
    return count;
}

int64_t py_virtual_thread_io_wait_active(void) {
    if (pcc_vthread_scheduler_init() != 0) return 0;
    if (pcc_mutex_lock(pcc_vthread_lock) != 0) return 0;
    int64_t active = pcc_vthread_io_wait_active ? 1 : 0;
    (void)pcc_mutex_unlock(pcc_vthread_lock);
    return active;
}

int64_t py_virtual_thread_io_backend(void) {
    if (pcc_vthread_scheduler_init() != 0) return -1;
    if (pcc_mutex_lock(pcc_vthread_lock) != 0) return -1;
    int rc = pcc_vthread_io_waitset_ensure_locked();
    int64_t backend = rc == 0 ? pcc_vthread_io_backend_value : -1;
    (void)pcc_mutex_unlock(pcc_vthread_lock);
    return backend;
}

PyObject *py_virtual_thread_poll_ready(void) {
    if (pcc_vthread_scheduler_init() != 0) return NULL;
    if (pcc_mutex_lock(pcc_vthread_lock) != 0) return NULL;
    PyObject *thread = pcc_vthread_dequeue_locked();
    (void)pcc_mutex_unlock(pcc_vthread_lock);
    return thread;
}

int64_t py_virtual_thread_ready_count(void) {
    if (pcc_vthread_scheduler_init() != 0) return 0;
    if (pcc_mutex_lock(pcc_vthread_lock) != 0) return 0;
    int64_t count = pcc_vthread_ready_count_value;
    (void)pcc_mutex_unlock(pcc_vthread_lock);
    return count;
}

int64_t py_virtual_thread_node_pool_stat(int64_t family, int64_t metric) {
    if (family == PCC_VTHREAD_NODE_WAITER) {
        if (metric == PCC_VTHREAD_POOL_ALLOCATIONS) {
            return __atomic_load_n(
                &pcc_vthread_waiter_entry_alloc_count, __ATOMIC_ACQUIRE
            );
        }
        if (metric == PCC_VTHREAD_POOL_REUSES) {
            return __atomic_load_n(
                &pcc_vthread_waiter_entry_reuse_count, __ATOMIC_ACQUIRE
            );
        }
        if (metric == PCC_VTHREAD_POOL_CACHED) {
            return __atomic_load_n(
                &pcc_vthread_waiter_entry_cached_count, __ATOMIC_ACQUIRE
            );
        }
        return -1;
    }
    if (
        metric != PCC_VTHREAD_POOL_ALLOCATIONS
        && metric != PCC_VTHREAD_POOL_REUSES
        && metric != PCC_VTHREAD_POOL_CACHED
    ) return -1;
    if (pcc_vthread_scheduler_init() != 0) return -1;
    if (pcc_mutex_lock(pcc_vthread_lock) != 0) return -1;
    int64_t value = -1;
    if (family == PCC_VTHREAD_NODE_READY) {
        if (metric == PCC_VTHREAD_POOL_ALLOCATIONS) {
            value = pcc_vthread_ready_entry_alloc_count;
        } else if (metric == PCC_VTHREAD_POOL_REUSES) {
            value = pcc_vthread_ready_entry_reuse_count;
        } else {
            value = pcc_vthread_ready_entry_free_count;
        }
    } else if (family == PCC_VTHREAD_NODE_TIMER) {
        if (metric == PCC_VTHREAD_POOL_ALLOCATIONS) {
            value = pcc_vthread_timer_entry_alloc_count;
        } else if (metric == PCC_VTHREAD_POOL_REUSES) {
            value = pcc_vthread_timer_entry_reuse_count;
        } else {
            value = pcc_vthread_timer_entry_free_count;
        }
    } else if (family == PCC_VTHREAD_NODE_IO) {
        if (metric == PCC_VTHREAD_POOL_ALLOCATIONS) {
            value = pcc_vthread_poll_entry_alloc_count;
        } else if (metric == PCC_VTHREAD_POOL_REUSES) {
            value = pcc_vthread_poll_entry_reuse_count;
        } else {
            value = pcc_vthread_poll_entry_free_count;
        }
    }
    (void)pcc_mutex_unlock(pcc_vthread_lock);
    return value;
}

int64_t py_virtual_thread_carrier_count(void) {
    return __atomic_load_n(&pcc_vthread_carrier_count, __ATOMIC_ACQUIRE);
}

int64_t py_virtual_thread_carrier_steal_count(void) {
    return __atomic_load_n(&pcc_vthread_carrier_steal_count, __ATOMIC_ACQUIRE);
}

typedef void (*PccVirtualThreadResumeFn)(void);
typedef int64_t (*PccVirtualThreadTypedResumeFn)(PyObject *, PyObject *);

typedef struct PccVirtualThreadCarrierPoolRun {
    int64_t max_steps;
    int64_t claimed_steps;
    int64_t ran_steps;
    int64_t failures;
} PccVirtualThreadCarrierPoolRun;

typedef struct PccVirtualThreadCarrierPoolWorker {
    PccVirtualThreadCarrierPoolRun *run;
    int64_t carrier_index;
} PccVirtualThreadCarrierPoolWorker;

static int64_t resume_generator_owned(PyObject *vthread, PyObject *gen);

int64_t py_virtual_thread_run_once(void) {
    (void)py_virtual_thread_poll_timers();
    (void)py_virtual_thread_poll_io(0);

    PyObject *ready = py_virtual_thread_poll_ready();
    if (ready == NULL) return 0;

    PyVirtualThreadObject *vt = checked_vthread(ready);
    if (vt == NULL) {
        py_decref(ready);
        return -1;
    }

    PyObject *continuation = pcc_gc_load_ptr(ready, &vt->continuation);
    int continuation_kind = 0;
    if (continuation != NULL && !PY_IS_TAGGED_INT(continuation)) {
        continuation_kind = py_type_of(continuation);
    }
    if (continuation_kind == PY_TYPE_CONTINUATION || continuation_kind == PY_TYPE_GEN) {
        void *resume_pc = NULL;
        if (continuation_kind == PY_TYPE_CONTINUATION) {
            resume_pc = py_continuation_resume_pc(continuation);
        }
        if (continuation_kind == PY_TYPE_GEN || resume_pc != NULL) {
            PyObject *saved_current = pcc_current_virtual_thread;
            pcc_current_virtual_thread = ready;
            int64_t resume_rc = 0;
            if (continuation_kind == PY_TYPE_GEN) {
                py_incref(continuation);
                resume_rc = resume_generator_owned(ready, continuation);
            } else if (
                py_continuation_resume_abi(continuation)
                == PCC_CONTINUATION_RESUME_ABI_VTHREAD
            ) {
                resume_rc = ((PccVirtualThreadTypedResumeFn)resume_pc)(
                    ready,
                    continuation
                );
            } else {
                ((PccVirtualThreadResumeFn)resume_pc)();
            }
            pcc_current_virtual_thread = saved_current;
            PyObject *failure = py_current_exception();
            if (failure != NULL) {
                int64_t fail_rc = py_virtual_thread_fail(ready, failure);
                if (fail_rc == 0) py_clear_exception();
                py_decref(ready);
                return fail_rc == 0 ? 1 : -1;
            }
            if (resume_rc != 0) {
                py_decref(ready);
                return -1;
            }
        }
    }

    int64_t state = py_virtual_thread_state(ready);
    if (state == PCC_VTHREAD_RUNNING) {
        if (py_virtual_thread_complete(ready, py_None) != 0) {
            py_decref(ready);
            return -1;
        }
    }
    py_decref(ready);
    return 1;
}

int64_t py_virtual_thread_run_until_idle(int64_t max_steps) {
    if (max_steps <= 0) return 0;
    int64_t ran = 0;
    int64_t steps = 0;
    while (steps < max_steps) {
        int64_t step = py_virtual_thread_run_once();
        if (step < 0) return -1;
        if (step == 0) {
            /* A sequential socket task is idle only from the carrier's point
             * of view.  Let the owned reactor block until readiness/deadline
             * instead of forcing user code into a sleep or busy-spin loop. */
            if (py_virtual_thread_io_wait_count() <= 0) break;
            if (py_virtual_thread_poll_io(-1) < 0) return -1;
            continue;
        }
        ran += step;
        steps++;
    }
    return ran;
}

static int pcc_vthread_carrier_pool_claim(PccVirtualThreadCarrierPoolRun *run) {
    if (run == NULL) return 0;
    for (;;) {
        int64_t old = __atomic_load_n(&run->claimed_steps, __ATOMIC_ACQUIRE);
        if (old >= run->max_steps) return 0;
        if (__atomic_compare_exchange_n(
                &run->claimed_steps,
                &old,
                old + 1,
                0,
                __ATOMIC_ACQ_REL,
                __ATOMIC_ACQUIRE
            )) {
            return 1;
        }
    }
}

static void *pcc_vthread_carrier_pool_main(void *opaque) {
    PccVirtualThreadCarrierPoolWorker *worker = (
        PccVirtualThreadCarrierPoolWorker *
    )opaque;
    PccVirtualThreadCarrierPoolRun *run = worker == NULL ? NULL : worker->run;
    int64_t saved_carrier = pcc_current_virtual_thread_carrier;
    pcc_current_virtual_thread_carrier = (
        worker == NULL ? -1 : worker->carrier_index
    );
    __atomic_add_fetch(&pcc_vthread_carrier_count, 1, __ATOMIC_ACQ_REL);
    while (pcc_vthread_carrier_pool_claim(run)) {
        int64_t step = py_virtual_thread_run_once();
        if (step < 0) {
            __atomic_add_fetch(&run->failures, 1, __ATOMIC_ACQ_REL);
            break;
        }
        if (step == 0) break;
        __atomic_add_fetch(&run->ran_steps, step, __ATOMIC_ACQ_REL);
    }
    __atomic_sub_fetch(&pcc_vthread_carrier_count, 1, __ATOMIC_ACQ_REL);
    pcc_current_virtual_thread_carrier = saved_carrier;
    return NULL;
}

int64_t py_virtual_thread_run_carrier_pool(
    int64_t carrier_count,
    int64_t max_steps
) {
    if (max_steps <= 0) return 0;
    if (carrier_count <= 1 || !pcc_threads_enabled()) {
        return py_virtual_thread_run_until_idle(max_steps);
    }
    if (carrier_count > 64) carrier_count = 64;

    PccThreadHandle **handles = (
        PccThreadHandle **
    )calloc((size_t)carrier_count, sizeof(PccThreadHandle *));
    if (handles == NULL) return -1;
    PccVirtualThreadCarrierPoolWorker *workers = (
        PccVirtualThreadCarrierPoolWorker *
    )calloc((size_t)carrier_count, sizeof(PccVirtualThreadCarrierPoolWorker));
    if (workers == NULL) {
        free(handles);
        return -1;
    }

    int opened_queues = 0;
    if (pcc_vthread_scheduler_init() != 0) {
        free(workers);
        free(handles);
        return -1;
    }
    if (pcc_mutex_lock(pcc_vthread_lock) != 0) {
        free(workers);
        free(handles);
        return -1;
    }
    if (
        pcc_vthread_persistent_pool_running != 0
        || pcc_vthread_bounded_pool_running != 0
        || pcc_vthread_persistent_cleanup_active != 0
    ) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        free(workers);
        free(handles);
        return -1;
    }
    pcc_vthread_bounded_pool_running = 1;
    if (pcc_vthread_carrier_queues == NULL) {
        if (pcc_vthread_carrier_queues_open_locked(carrier_count) != 0) {
            pcc_vthread_bounded_pool_running = 0;
            (void)pcc_mutex_unlock(pcc_vthread_lock);
            free(workers);
            free(handles);
            return -1;
        }
        opened_queues = 1;
    }
    (void)pcc_mutex_unlock(pcc_vthread_lock);

    PccVirtualThreadCarrierPoolRun run;
    run.max_steps = max_steps;
    run.claimed_steps = 0;
    run.ran_steps = 0;
    run.failures = 0;

    int64_t started = 0;
    for (int64_t i = 0; i < carrier_count; i++) {
        workers[i].run = &run;
        workers[i].carrier_index = i;
        if (pcc_thread_start(
                &handles[i],
                pcc_vthread_carrier_pool_main,
                &workers[i]
            ) != 0) {
            __atomic_add_fetch(&run.failures, 1, __ATOMIC_ACQ_REL);
            break;
        }
        started++;
    }

    for (int64_t i = 0; i < started; i++) {
        if (pcc_thread_join(handles[i], NULL) != 0) {
            __atomic_add_fetch(&run.failures, 1, __ATOMIC_ACQ_REL);
        }
    }
    if (pcc_mutex_lock(pcc_vthread_lock) == 0) {
        if (opened_queues) pcc_vthread_carrier_queues_close_locked();
        pcc_vthread_bounded_pool_running = 0;
        (void)pcc_mutex_unlock(pcc_vthread_lock);
    } else {
        __atomic_store_n(
            &pcc_vthread_bounded_pool_running, 0, __ATOMIC_RELEASE
        );
    }
    free(workers);
    free(handles);
    if (started == 0) return -1;
    if (__atomic_load_n(&run.failures, __ATOMIC_ACQUIRE) != 0) return -1;
    return __atomic_load_n(&run.ran_steps, __ATOMIC_ACQUIRE);
}

static void *pcc_vthread_persistent_carrier_main(void *opaque) {
    int64_t saved_carrier = pcc_current_virtual_thread_carrier;
    int64_t carrier_index = opaque == NULL ? -1 : *((int64_t *)opaque);
    pcc_current_virtual_thread_carrier = carrier_index;
    __atomic_add_fetch(&pcc_vthread_carrier_count, 1, __ATOMIC_ACQ_REL);
    while (
        __atomic_load_n(&pcc_vthread_persistent_pool_stop, __ATOMIC_ACQUIRE)
        == 0
    ) {
        int64_t step = py_virtual_thread_run_once();
        if (step < 0) {
            __atomic_add_fetch(
                &pcc_vthread_persistent_pool_failures,
                1,
                __ATOMIC_ACQ_REL
            );
            break;
        }
        if (step == 0) {
            (void)py_virtual_thread_poll_timers();
            (void)py_virtual_thread_poll_io(1);
            pcc_thread_safepoint();
            (void)poll(NULL, 0, 1);
        }
    }
    __atomic_sub_fetch(&pcc_vthread_carrier_count, 1, __ATOMIC_ACQ_REL);
    pcc_current_virtual_thread_carrier = saved_carrier;
    return NULL;
}

int64_t py_virtual_thread_carrier_pool_start(int64_t carrier_count) {
    if (carrier_count <= 0) return 0;
    if (!pcc_threads_enabled()) return 0;
    if (carrier_count > 64) carrier_count = 64;
    PccThreadHandle **handles = (
        PccThreadHandle **
    )calloc((size_t)carrier_count, sizeof(PccThreadHandle *));
    if (handles == NULL) return -1;
    int64_t *indices = (int64_t *)calloc((size_t)carrier_count, sizeof(int64_t));
    if (indices == NULL) {
        free(handles);
        return -1;
    }
    if (pcc_vthread_scheduler_init() != 0) {
        free(indices);
        free(handles);
        return -1;
    }
    if (pcc_mutex_lock(pcc_vthread_lock) != 0) {
        free(indices);
        free(handles);
        return -1;
    }
    if (pcc_vthread_persistent_pool_running != 0) {
        int64_t existing = pcc_vthread_persistent_carrier_count;
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        free(indices);
        free(handles);
        return existing;
    }
    if (pcc_vthread_bounded_pool_running != 0) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        free(indices);
        free(handles);
        return -1;
    }
    if (pcc_vthread_persistent_cleanup_active != 0) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        free(indices);
        free(handles);
        return -1;
    }
    pcc_vthread_persistent_cleanup_active = 1;
    if (pcc_vthread_io_waitset_ensure_locked() != 0) {
        pcc_vthread_persistent_cleanup_active = 0;
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        free(indices);
        free(handles);
        return -1;
    }
    if (pcc_vthread_carrier_queues_open_locked(carrier_count) != 0) {
        (void)pcc_vthread_io_waitset_dispose_locked();
        pcc_vthread_persistent_cleanup_active = 0;
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        free(indices);
        free(handles);
        return -1;
    }
    pcc_vthread_persistent_carriers = handles;
    pcc_vthread_persistent_carrier_indices = indices;
    pcc_vthread_persistent_pool_stop = 0;
    pcc_vthread_persistent_pool_failures = 0;
    pcc_vthread_persistent_carrier_count = 0;
    pcc_vthread_persistent_joined_count = 0;
    pcc_vthread_persistent_pool_running = 1;
    (void)pcc_mutex_unlock(pcc_vthread_lock);

    int64_t started = 0;
    for (int64_t i = 0; i < carrier_count; i++) {
        indices[i] = i;
        if (pcc_thread_start(
                &handles[i],
                pcc_vthread_persistent_carrier_main,
                &indices[i]
            ) != 0) {
            break;
        }
        started++;
        __atomic_store_n(
            &pcc_vthread_persistent_carrier_count,
            started,
            __ATOMIC_RELEASE
        );
    }
    if (started == carrier_count) {
        __atomic_store_n(
            &pcc_vthread_persistent_cleanup_active, 0, __ATOMIC_RELEASE
        );
        return started;
    }
    {
        __atomic_store_n(&pcc_vthread_persistent_pool_stop, 1, __ATOMIC_RELEASE);
        if (pcc_mutex_lock(pcc_vthread_lock) != 0) {
            __atomic_store_n(
                &pcc_vthread_persistent_cleanup_active, 0, __ATOMIC_RELEASE
            );
            return -1;
        }
        int interrupt_failed = pcc_vthread_io_interrupt_locked() != 0;
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        if (interrupt_failed) {
            __atomic_store_n(
                &pcc_vthread_persistent_cleanup_active, 0, __ATOMIC_RELEASE
            );
            return -1;
        }
        int join_failed = 0;
        for (int64_t i = 0; i < started; i++) {
            if (handles[i] != NULL && pcc_thread_join(handles[i], NULL) == 0) {
                handles[i] = NULL;
                __atomic_add_fetch(
                    &pcc_vthread_persistent_joined_count,
                    1,
                    __ATOMIC_ACQ_REL
                );
            } else {
                join_failed = 1;
            }
        }
        if (join_failed) {
            __atomic_store_n(
                &pcc_vthread_persistent_cleanup_active, 0, __ATOMIC_RELEASE
            );
            return -1;
        }
        if (pcc_mutex_lock(pcc_vthread_lock) != 0) {
            __atomic_store_n(
                &pcc_vthread_persistent_cleanup_active, 0, __ATOMIC_RELEASE
            );
            return -1;
        }
        if (pcc_vthread_io_waitset_dispose_locked() != 0) {
            pcc_vthread_persistent_cleanup_active = 0;
            (void)pcc_mutex_unlock(pcc_vthread_lock);
            return -1;
        }
        pcc_vthread_carrier_queues_close_locked();
        pcc_vthread_persistent_carrier_indices = NULL;
        pcc_vthread_persistent_carriers = NULL;
        pcc_vthread_persistent_carrier_count = 0;
        pcc_vthread_persistent_pool_running = 0;
        pcc_vthread_persistent_pool_stop = 0;
        pcc_vthread_persistent_pool_failures = 0;
        pcc_vthread_persistent_joined_count = 0;
        pcc_vthread_persistent_cleanup_active = 0;
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        free(indices);
        free(handles);
        return -1;
    }
    return -1;
}

int64_t py_virtual_thread_carrier_pool_stop(void) {
    if (
        __atomic_load_n(
            &pcc_vthread_persistent_pool_running,
            __ATOMIC_ACQUIRE
        ) == 0
    ) {
        return 0;
    }
    int64_t expected_cleanup = 0;
    if (!__atomic_compare_exchange_n(
            &pcc_vthread_persistent_cleanup_active,
            &expected_cleanup,
            1,
            0,
            __ATOMIC_ACQ_REL,
            __ATOMIC_ACQUIRE
        )) {
        return -1;
    }
    __atomic_store_n(&pcc_vthread_persistent_pool_stop, 1, __ATOMIC_RELEASE);
    if (pcc_mutex_lock(pcc_vthread_lock) != 0) {
        __atomic_store_n(
            &pcc_vthread_persistent_cleanup_active, 0, __ATOMIC_RELEASE
        );
        return -1;
    }
    int interrupt_failed = pcc_vthread_io_interrupt_locked() != 0;
    (void)pcc_mutex_unlock(pcc_vthread_lock);
    if (interrupt_failed) {
        __atomic_store_n(
            &pcc_vthread_persistent_cleanup_active, 0, __ATOMIC_RELEASE
        );
        return -1;
    }
    PccThreadHandle **handles = pcc_vthread_persistent_carriers;
    int64_t *indices = pcc_vthread_persistent_carrier_indices;
    int64_t count = __atomic_load_n(
        &pcc_vthread_persistent_carrier_count,
        __ATOMIC_ACQUIRE
    );
    int64_t joined = 0;
    int join_failed = 0;
    for (int64_t i = 0; i < count; i++) {
        if (handles != NULL && handles[i] != NULL) {
            if (pcc_thread_join(handles[i], NULL) == 0) {
                handles[i] = NULL;
                joined++;
            } else {
                join_failed = 1;
            }
        }
    }
    __atomic_add_fetch(
        &pcc_vthread_persistent_joined_count, joined, __ATOMIC_ACQ_REL
    );
    if (join_failed) {
        __atomic_store_n(
            &pcc_vthread_persistent_cleanup_active, 0, __ATOMIC_RELEASE
        );
        return -1;
    }
    if (pcc_mutex_lock(pcc_vthread_lock) != 0) {
        __atomic_store_n(
            &pcc_vthread_persistent_cleanup_active, 0, __ATOMIC_RELEASE
        );
        return -1;
    }
    if (pcc_vthread_io_waitset_dispose_locked() != 0) {
        pcc_vthread_persistent_cleanup_active = 0;
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return -1;
    }
    pcc_vthread_carrier_queues_close_locked();
    int64_t joined_total = pcc_vthread_persistent_joined_count;
    int64_t pool_failures = pcc_vthread_persistent_pool_failures;
    pcc_vthread_persistent_carriers = NULL;
    pcc_vthread_persistent_carrier_indices = NULL;
    pcc_vthread_persistent_carrier_count = 0;
    pcc_vthread_persistent_pool_running = 0;
    pcc_vthread_persistent_pool_stop = 0;
    pcc_vthread_persistent_pool_failures = 0;
    pcc_vthread_persistent_joined_count = 0;
    pcc_vthread_persistent_cleanup_active = 0;
    (void)pcc_mutex_unlock(pcc_vthread_lock);
    free(handles);
    free(indices);
    if (pool_failures != 0) return -1;
    return joined_total;
}

PyObject *py_virtual_thread_current(void) {
    PyObject *current = pcc_current_virtual_thread;
    if (current == NULL) current = py_None;
    py_incref(current);
    return current;
}

int64_t py_virtual_thread_cancel_requested(PyObject *vthread) {
    PyVirtualThreadObject *vt = checked_vthread(vthread);
    if (vt == NULL) return -1;
    if (pcc_vthread_scheduler_init() != 0) return -1;
    if (pcc_mutex_lock(pcc_vthread_lock) != 0) return -1;
    int64_t requested = vt->cancel_requested;
    (void)pcc_mutex_unlock(pcc_vthread_lock);
    return requested;
}

int64_t py_virtual_thread_cancel(PyObject *vthread) {
    PyVirtualThreadObject *vt = checked_vthread(vthread);
    if (vt == NULL) return -1;
    if (pcc_vthread_scheduler_init() != 0) return -1;
    if (pcc_mutex_lock(pcc_vthread_lock) != 0) return -1;
    if (vt->state == PCC_VTHREAD_DONE || vt->cancel_requested != 0) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return 0;
    }

    if (vt->state == PCC_VTHREAD_PARKED && vt->join_entry != NULL) {
        if (pcc_vthread_join_cancel_locked(vt) != 0) {
            (void)pcc_mutex_unlock(pcc_vthread_lock);
            return -1;
        }
        vt->cancel_requested = 1;
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return 1;
    }

    int needs_ready = (
        vt->state == PCC_VTHREAD_NEW
        || vt->state == PCC_VTHREAD_PARKED
        || (vt->state == PCC_VTHREAD_READY && vt->queued == 0)
    );
    PccVirtualThreadQueueEntry *ready_entry = NULL;
    if (needs_ready) {
        /* Reserve both memory and the new scheduler root before retiring the
         * timer/IO root.  Once the old registration is gone, no later event
         * can recover an allocation-failed wakeup. */
        ready_entry = pcc_vthread_prepare_ready_entry_locked(vthread);
        if (ready_entry == NULL) {
            (void)pcc_mutex_unlock(pcc_vthread_lock);
            return -1;
        }
    }

    vt->cancel_requested = 1;
    if (ready_entry != NULL) {
        int timer_rc = pcc_vthread_timer_cancel_locked(vt);
        int io_rc = pcc_vthread_poll_cancel_locked(vt);
        /* poll cancellation may report a waitset-refresh failure after this
         * task's node/root was already retired.  The cancellation must still
         * publish its prepared ready root; a stale aggregate registration has
         * no live task node and a later refresh removes it. */
        if (
            (timer_rc < 0 && vt->timer_entry != NULL)
            || (io_rc < 0 && vt->io_entry != NULL)
        ) {
            vt->cancel_requested = 0;
            pcc_vthread_ready_entry_release_locked(ready_entry);
            (void)pcc_mutex_unlock(pcc_vthread_lock);
            return -1;
        }
        pcc_vthread_publish_ready_entry_locked(vt, ready_entry);
        pcc_vthread_effect_emit(
            PCC_VTHREAD_EFFECT_UNPARK,
            0,
            0,
            vt->state
        );
    }
    (void)pcc_mutex_unlock(pcc_vthread_lock);
    return 1;
}

static int pcc_vthread_terminal_wait_cleanup_locked(
    PyVirtualThreadObject *vt
) {
    if (vt->join_entry != NULL) {
        PccVirtualThreadJoinEntry *entry =
            pcc_vthread_join_unlink_locked(vt);
        if (entry == NULL) return -1;
        pcc_vthread_join_entry_release_locked(entry);
    }
    int timer_rc = pcc_vthread_timer_cancel_locked(vt);
    if (timer_rc < 0 && vt->timer_entry != NULL) return -1;
    int io_rc = pcc_vthread_poll_cancel_locked(vt);
    if (io_rc < 0 && vt->io_entry != NULL) return -1;
    pcc_gc_store_ptr((PyObject *)vt, &vt->join_target, NULL);
    vt->join_entry = NULL;
    vt->wait_kind = PCC_VTHREAD_WAIT_NONE;
    return 0;
}

static int pcc_vthread_publish_cancelled_locked(PyVirtualThreadObject *vt) {
    pcc_gc_store_ptr((PyObject *)vt, &vt->result, py_None);
    pcc_gc_store_ptr((PyObject *)vt, &vt->exception, NULL);
    pcc_gc_store_ptr((PyObject *)vt, &vt->join_target, NULL);
    vt->outcome = PCC_VTHREAD_OUTCOME_CANCELLED;
    vt->state = PCC_VTHREAD_DONE;
    vt->queued = 0;
    vt->wait_kind = PCC_VTHREAD_WAIT_NONE;
    vt->cancel_requested = 0;
    if (pcc_vthread_join_wake_all_locked(vt) != 0) return -1;
    pcc_vthread_effect_emit(
        PCC_VTHREAD_EFFECT_CANCEL_COMPLETE,
        0,
        0,
        vt->state
    );
    return 0;
}

int64_t py_virtual_thread_cancel_complete(PyObject *vthread) {
    PyVirtualThreadObject *vt = checked_vthread(vthread);
    if (vt == NULL) return -1;
    if (pcc_vthread_scheduler_init() != 0) return -1;
    if (pcc_mutex_lock(pcc_vthread_lock) != 0) return -1;
    if (vt->state == PCC_VTHREAD_DONE) {
        int64_t already_cancelled = (
            vt->outcome == PCC_VTHREAD_OUTCOME_CANCELLED
        );
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return already_cancelled ? 0 : -1;
    }
    if (pcc_vthread_terminal_wait_cleanup_locked(vt) != 0) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return -1;
    }
    int rc = pcc_vthread_publish_cancelled_locked(vt);
    (void)pcc_mutex_unlock(pcc_vthread_lock);
    return rc == 0 ? 0 : -1;
}

int64_t py_virtual_thread_resume_generator(
    PyObject *vthread,
    PyObject *continuation
) {
    PyObject *gen = py_continuation_get_slot(continuation, 0);
    return resume_generator_owned(vthread, gen);
}

static int64_t resume_generator_owned(PyObject *vthread, PyObject *gen) {
    if (gen == NULL) return -1;

    int64_t cancel_pending = py_virtual_thread_cancel_requested(vthread);
    if (cancel_pending < 0) {
        py_decref(gen);
        return -1;
    }
    if (cancel_pending != 0) {
        int64_t generator_state = py_gen_state(gen);
        if (generator_state < 0) {
            py_decref(gen);
            return -1;
        }
        if (generator_state == 0) {
            /* An unstarted generator has not entered its try/finally scope.
             * Do not execute user code merely because it was cancelled. */
            py_gen_set_done(gen);
            int64_t rc = py_virtual_thread_cancel_complete(vthread);
            py_decref(gen);
            return rc == 0 ? 0 : -1;
        }
        PyObject *closed = py_gen_close(gen);
        if (closed == NULL) {
            /* Cleanup failure stays in task-local TLS. run_once publishes it
             * as RAISED; yielding from close is rejected by py_gen_close. */
            py_decref(gen);
            return -1;
        }
        py_decref(closed);
        int64_t rc = py_virtual_thread_cancel_complete(vthread);
        py_decref(gen);
        return rc == 0 ? 0 : -1;
    }

    PyObject *yielded = py_gen_next(gen);
    if (yielded != NULL) {
        py_decref(yielded);
        int64_t state = py_virtual_thread_state(vthread);
        cancel_pending = py_virtual_thread_cancel_requested(vthread);
        if (state < 0 || cancel_pending < 0) {
            py_decref(gen);
            return -1;
        }
        if (cancel_pending != 0) {
            /* cancel() may have observed RUNNING immediately before this
             * generator installed a timer/IO/join wait. Close on the current
             * carrier, then retire that registration in cancel_complete. */
            PyObject *closed = py_gen_close(gen);
            if (closed == NULL) {
                py_decref(gen);
                return -1;
            }
            py_decref(closed);
            int64_t rc = py_virtual_thread_cancel_complete(vthread);
            py_decref(gen);
            return rc == 0 ? 0 : -1;
        }
        int64_t rc = 0;
        if (state == PCC_VTHREAD_RUNNING) {
            rc = py_virtual_thread_unpark(vthread);
        }
        py_decref(gen);
        return rc == 0 ? 0 : -1;
    }

    PyObject *cur = py_current_exception();
    PyObject *stop_cls = (PyObject *)py_exc_builtin_class(PY_EXC_STOPITERATION);
    if (py_exc_matches(cur, stop_cls)) {
        PyObject *value = py_exc_get_message(cur);
        if (value == NULL) value = py_None;
        py_incref(value);
        py_clear_exception();
        int64_t rc = py_virtual_thread_complete(vthread, value);
        py_decref(value);
        py_decref(gen);
        return rc == 0 ? 0 : -1;
    }

    py_decref(gen);
    return -1;
}

int64_t py_virtual_thread_join(PyObject *vthread, PyObject *target) {
    PyVirtualThreadObject *waiter = checked_vthread(vthread);
    PyVirtualThreadObject *joined = checked_vthread(target);
    if (waiter == NULL || joined == NULL || waiter == joined) return -1;
    PyObject *current = pcc_current_virtual_thread;
    if (current != NULL) current = pcc_gc_note_relocation_read(current);
    if (current != (PyObject *)waiter) return -1;
    if (pcc_vthread_scheduler_init() != 0) return -1;
    if (pcc_mutex_lock(pcc_vthread_lock) != 0) return -1;
    if (
        waiter->state != PCC_VTHREAD_RUNNING
        || waiter->join_entry != NULL
        || pcc_gc_load_ptr((PyObject *)waiter, &waiter->join_target) != NULL
    ) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return -1;
    }
    pcc_gc_store_ptr(
        (PyObject *)waiter,
        &waiter->join_target,
        (PyObject *)joined
    );
    if (joined->state == PCC_VTHREAD_DONE) {
        waiter->wait_kind = PCC_VTHREAD_WAIT_NONE;
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return 1;
    }
    if (pcc_vthread_join_enqueue_locked(joined, waiter) != 0) {
        pcc_gc_store_ptr((PyObject *)waiter, &waiter->join_target, NULL);
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return -1;
    }
    (void)pcc_mutex_unlock(pcc_vthread_lock);
    return 0;
}

PyObject *py_virtual_thread_join_result(PyObject *vthread) {
    PyVirtualThreadObject *waiter = checked_vthread(vthread);
    if (waiter == NULL) return NULL;
    if (pcc_vthread_scheduler_init() != 0) return NULL;
    if (pcc_mutex_lock(pcc_vthread_lock) != 0) return NULL;
    PyObject *target = pcc_gc_load_ptr(
        (PyObject *)waiter,
        &waiter->join_target
    );
    if (
        target == NULL
        || PY_IS_TAGGED_INT(target)
        || py_type_of(target) != PY_TYPE_VIRTUAL_THREAD
    ) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        py_raise_owned(py_exc_new(PY_EXC_RUNTIMEERROR, "join has no target"));
        return NULL;
    }
    PyVirtualThreadObject *joined = (PyVirtualThreadObject *)target;
    if (joined->state != PCC_VTHREAD_DONE) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        py_raise_owned(py_exc_new(PY_EXC_RUNTIMEERROR, "join target is not done"));
        return NULL;
    }
    int64_t outcome = joined->outcome;
    PyObject *payload = outcome == PCC_VTHREAD_OUTCOME_RAISED
        ? pcc_gc_load_ptr(target, &joined->exception)
        : pcc_gc_load_ptr(target, &joined->result);
    if (payload == NULL) payload = py_None;
    py_incref(payload);
    pcc_gc_store_ptr((PyObject *)waiter, &waiter->join_target, NULL);
    waiter->join_entry = NULL;
    waiter->wait_kind = PCC_VTHREAD_WAIT_NONE;
    (void)pcc_mutex_unlock(pcc_vthread_lock);

    if (outcome == PCC_VTHREAD_OUTCOME_RETURNED) return payload;
    if (outcome == PCC_VTHREAD_OUTCOME_RAISED) {
        py_raise(payload);
        py_decref(payload);
        return NULL;
    }
    py_decref(payload);
    if (outcome == PCC_VTHREAD_OUTCOME_CANCELLED) {
        py_raise_owned(py_exc_new(PY_EXC_RUNTIMEERROR, "virtual thread cancelled"));
    } else {
        py_raise_owned(py_exc_new(PY_EXC_RUNTIMEERROR, "join target has no outcome"));
    }
    return NULL;
}

int64_t py_virtual_thread_state(PyObject *vthread) {
    PyVirtualThreadObject *vt = checked_vthread(vthread);
    if (vt == NULL) return -1;
    if (pcc_vthread_scheduler_init() != 0) return -1;
    if (pcc_mutex_lock(pcc_vthread_lock) != 0) return -1;
    int64_t state = vt->state;
    (void)pcc_mutex_unlock(pcc_vthread_lock);
    return state;
}

int64_t py_virtual_thread_complete(PyObject *vthread, PyObject *result) {
    PyVirtualThreadObject *vt = checked_vthread(vthread);
    if (vt == NULL) return -1;
    if (pcc_vthread_scheduler_init() != 0) return -1;
    if (pcc_mutex_lock(pcc_vthread_lock) != 0) return -1;
    if (
        vt->state == PCC_VTHREAD_DONE
        && vt->outcome != PCC_VTHREAD_OUTCOME_PENDING
    ) {
        int64_t already_returned = (
            vt->outcome == PCC_VTHREAD_OUTCOME_RETURNED
        );
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return already_returned ? 0 : -1;
    }
    if (pcc_vthread_terminal_wait_cleanup_locked(vt) != 0) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return -1;
    }
    if (vt->cancel_requested != 0) {
        int rc = pcc_vthread_publish_cancelled_locked(vt);
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return rc == 0 ? 0 : -1;
    }
    pcc_gc_store_ptr((PyObject *)vt, &vt->result, result == NULL ? py_None : result);
    pcc_gc_store_ptr((PyObject *)vt, &vt->exception, NULL);
    pcc_gc_store_ptr((PyObject *)vt, &vt->join_target, NULL);
    vt->outcome = PCC_VTHREAD_OUTCOME_RETURNED;
    vt->state = PCC_VTHREAD_DONE;
    vt->queued = 0;
    vt->wait_kind = PCC_VTHREAD_WAIT_NONE;
    vt->cancel_requested = 0;
    if (pcc_vthread_join_wake_all_locked(vt) != 0) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return -1;
    }
    pcc_vthread_effect_emit(PCC_VTHREAD_EFFECT_COMPLETE, 0, 0, vt->state);
    (void)pcc_mutex_unlock(pcc_vthread_lock);
    return 0;
}

int64_t py_virtual_thread_fail(PyObject *vthread, PyObject *exception) {
    PyVirtualThreadObject *vt = checked_vthread(vthread);
    if (vt == NULL || exception == NULL) return -1;
    if (pcc_vthread_scheduler_init() != 0) return -1;
    if (pcc_mutex_lock(pcc_vthread_lock) != 0) return -1;
    if (vt->state == PCC_VTHREAD_DONE) {
        int64_t already_raised = (
            vt->outcome == PCC_VTHREAD_OUTCOME_RAISED
        );
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return already_raised ? 0 : -1;
    }
    if (pcc_vthread_terminal_wait_cleanup_locked(vt) != 0) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return -1;
    }
    pcc_gc_store_ptr((PyObject *)vt, &vt->result, py_None);
    pcc_gc_store_ptr((PyObject *)vt, &vt->exception, exception);
    pcc_gc_store_ptr((PyObject *)vt, &vt->join_target, NULL);
    vt->outcome = PCC_VTHREAD_OUTCOME_RAISED;
    vt->state = PCC_VTHREAD_DONE;
    vt->queued = 0;
    vt->wait_kind = PCC_VTHREAD_WAIT_NONE;
    vt->cancel_requested = 0;
    if (pcc_vthread_join_wake_all_locked(vt) != 0) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return -1;
    }
    pcc_vthread_effect_emit(
        PCC_VTHREAD_EFFECT_FAIL,
        0,
        0,
        vt->state
    );
    (void)pcc_mutex_unlock(pcc_vthread_lock);
    return 0;
}

PyObject *py_virtual_thread_result(PyObject *vthread) {
    PyVirtualThreadObject *vt = checked_vthread(vthread);
    if (vt == NULL) return NULL;
    if (pcc_vthread_scheduler_init() != 0) return NULL;
    if (pcc_mutex_lock(pcc_vthread_lock) != 0) return NULL;
    PyObject *result = pcc_gc_load_ptr(vthread, &vt->result);
    if (result == NULL) result = py_None;
    py_incref(result);
    (void)pcc_mutex_unlock(pcc_vthread_lock);
    return result;
}

PyObject *py_virtual_thread_exception(PyObject *vthread) {
    PyVirtualThreadObject *vt = checked_vthread(vthread);
    if (vt == NULL) return NULL;
    if (pcc_vthread_scheduler_init() != 0) return NULL;
    if (pcc_mutex_lock(pcc_vthread_lock) != 0) return NULL;
    PyObject *exception = pcc_gc_load_ptr(vthread, &vt->exception);
    if (exception == NULL) exception = py_None;
    py_incref(exception);
    (void)pcc_mutex_unlock(pcc_vthread_lock);
    return exception;
}

int64_t py_virtual_thread_outcome(PyObject *vthread) {
    PyVirtualThreadObject *vt = checked_vthread(vthread);
    if (vt == NULL) return -1;
    if (pcc_vthread_scheduler_init() != 0) return -1;
    if (pcc_mutex_lock(pcc_vthread_lock) != 0) return -1;
    int64_t outcome = vt->outcome;
    (void)pcc_mutex_unlock(pcc_vthread_lock);
    return outcome;
}

int64_t py_virtual_thread_pin_enter(PyObject *vthread, const char *reason) {
    (void)reason;
    PyVirtualThreadObject *vt = checked_vthread(vthread);
    if (vt == NULL || vt->state == PCC_VTHREAD_DONE) return -1;
    if (pcc_vthread_scheduler_init() != 0) return -1;
    if (pcc_mutex_lock(pcc_vthread_lock) != 0) return -1;
    if (vt->state == PCC_VTHREAD_DONE) {
        (void)pcc_mutex_unlock(pcc_vthread_lock);
        return -1;
    }
    if (vt->pinned == 0) pcc_gc_pin((PyObject *)vt);
    vt->pinned++;
    pcc_vthread_pin_depth_total++;
    pcc_vthread_pin_events++;
    (void)pcc_mutex_unlock(pcc_vthread_lock);
    return vt->pinned;
}

int64_t py_virtual_thread_pin_leave(PyObject *vthread) {
    PyVirtualThreadObject *vt = checked_vthread(vthread);
    if (vt == NULL) return -1;
    if (pcc_vthread_scheduler_init() != 0) return -1;
    if (pcc_mutex_lock(pcc_vthread_lock) != 0) return -1;
    if (vt->pinned > 0) {
        vt->pinned--;
        if (pcc_vthread_pin_depth_total > 0) pcc_vthread_pin_depth_total--;
        if (vt->pinned == 0) pcc_gc_unpin((PyObject *)vt);
    }
    int64_t pinned = vt->pinned;
    (void)pcc_mutex_unlock(pcc_vthread_lock);
    return pinned;
}

int64_t py_virtual_thread_pin_count(PyObject *vthread) {
    PyVirtualThreadObject *vt = checked_vthread(vthread);
    if (vt == NULL) return -1;
    if (pcc_vthread_scheduler_init() != 0) return -1;
    if (pcc_mutex_lock(pcc_vthread_lock) != 0) return -1;
    int64_t pinned = vt->pinned;
    (void)pcc_mutex_unlock(pcc_vthread_lock);
    return pinned;
}

int64_t py_virtual_thread_pinned_count(void) {
    return pcc_vthread_pin_depth_total;
}

int64_t py_virtual_thread_pin_event_count(void) {
    return pcc_vthread_pin_events;
}

void py_dealloc_virtual_thread(PyObject *o) {
    PyVirtualThreadObject *vt = (PyVirtualThreadObject *)o;
    PyObject *continuation = pcc_gc_load_ptr(o, &vt->continuation);
    PyObject *result = pcc_gc_load_ptr(o, &vt->result);
    PyObject *exception = pcc_gc_load_ptr(o, &vt->exception);
    PyObject *join_target = pcc_gc_load_ptr(o, &vt->join_target);
    PyObject *channel_owner_a = pcc_gc_load_ptr(o, &vt->channel_owner_a);
    PyObject *channel_owner_b = pcc_gc_load_ptr(o, &vt->channel_owner_b);
    PyObject *channel_value = pcc_gc_load_ptr(o, &vt->channel_value);
    if (continuation != NULL) py_decref(continuation);
    if (result != NULL) py_decref(result);
    if (exception != NULL) py_decref(exception);
    if (join_target != NULL) py_decref(join_target);
    if (channel_owner_a != NULL) py_decref(channel_owner_a);
    if (channel_owner_b != NULL) py_decref(channel_owner_b);
    if (channel_value != NULL) py_decref(channel_value);
    pcc_gc_free_object_memory(o);
}

void py_dealloc_vthread_channel(PyObject *o) {
    if (o == NULL || PY_IS_TAGGED_INT(o)) return;
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
        ) {
            PCC_RT_TRIPWIRE(
                0,
                "py_dealloc_vthread_channel: core still has active scheduler state"
            );
            /* Scheduler-node ownership cannot be recovered here without
             * taking the scheduler lock.  Leak on a corrupt production
             * invariant rather than freeing memory still named by a root. */
            return;
        }
        int64_t capacity = core->capacity;
        if (
            capacity < 0
            || capacity > PCC_VTHREAD_CHANNEL_MAX_CAPACITY
        ) {
            PCC_RT_TRIPWIRE(
                0,
                "py_dealloc_vthread_channel: invalid inline capacity"
            );
            return;
        }
        core->capacity = 0;
        core->length = 0;
        for (int64_t i = 0; i < capacity; i++) {
            PyObject *item = pcc_gc_load_ptr(o, &core->items[i]);
            core->items[i] = NULL;
            if (item != NULL) py_decref(item);
        }
        pcc_gc_free_object_memory(o);
        return;
    }
    if (
        channel->kind == PCC_VTHREAD_CHANNEL_KIND_SENDER
        || channel->kind == PCC_VTHREAD_CHANNEL_KIND_RECEIVER
    ) {
        PyVThreadChannelEndpointObject *endpoint =
            (PyVThreadChannelEndpointObject *)o;
        PyObject *core = pcc_gc_load_ptr(o, &endpoint->core);
        endpoint->core = NULL;
        if (core != NULL) py_decref(core);
        pcc_gc_free_object_memory(o);
        return;
    }
    PCC_RT_TRIPWIRE(
        0,
        "py_dealloc_vthread_channel: invalid channel kind"
    );
}

/* DEBUG: catch py_incref called on a pointer that's not a valid PyObject. */
void pcc_debug_bad_incref(void *o, int32_t tag) {
    fprintf(stderr, "[BAD_INCREF] o=%p tag=%d\n", o, tag);
    if (o != NULL && tag >= 0) {
        PyObjectHeader *h = py_header((PyObject *)o);
        fprintf(
            stderr,
            "[BAD_INCREF_HEADER] refcount=%lld type_tag=%d flags=%d\n",
            (long long)pcc_refcount_load(&h->refcount),
            h->type_tag,
            py_header_flags_load(h)
        );
    }
#if defined(__APPLE__) || defined(__linux__)
    const char *bt = pcc_runtime_getenv("PCC_DEBUG_BAD_BACKTRACE");
    if (bt != NULL && bt[0] != '\0' && bt[0] != '0') {
        void *frames[64];
        int n = backtrace(frames, 64);
        backtrace_symbols_fd(frames, n, 2);
    }
#endif
    fflush(stderr);
    __builtin_trap();
}

void pcc_debug_bad_dict_slot(
    void *dict,
    int64_t index,
    int64_t offset,
    void *obj,
    int64_t tag
) {
    fprintf(
        stderr,
        "[DEBUG-dict-slot] dict=%p index=%lld offset=%lld obj=%p tag=%lld\n",
        dict,
        (long long)index,
        (long long)offset,
        obj,
        (long long)tag
    );
    if (obj != NULL) {
        PyObjectHeader *h = py_header((PyObject *)obj);
        fprintf(
            stderr,
            "[DEBUG-dict-slot-header] refcount=%lld type_tag=%d flags=%d\n",
            (long long)pcc_refcount_load(&h->refcount),
            h->type_tag,
            py_header_flags_load(h)
        );
    }
#if defined(__APPLE__) || defined(__linux__)
    const char *bt = pcc_runtime_getenv("PCC_DEBUG_BAD_BACKTRACE");
    if (bt != NULL && bt[0] != '\0' && bt[0] != '0') {
        void *frames[64];
        int n = backtrace(frames, 64);
        backtrace_symbols_fd(frames, n, 2);
    }
#endif
    fflush(stderr);
    __builtin_trap();
}

typedef struct PccDebugAllocEntry {
    void *ptr;
    int64_t size;
} PccDebugAllocEntry;

#define PCC_DEBUG_ALLOC_TABLE_SIZE 1048576u
#define PCC_DEBUG_ALLOC_PROBE_LIMIT 64u

static PccDebugAllocEntry *pcc_debug_alloc_table = NULL;
static int pcc_debug_runtime_enabled_cache = -1;

static int pcc_debug_runtime_enabled(void) {
    int cached = __atomic_load_n(
        &pcc_debug_runtime_enabled_cache, __ATOMIC_ACQUIRE
    );
    if (cached < 0) {
        int value = pcc_runtime_getenv("PCC_DEBUG_RUNTIME") != NULL ? 1 : 0;
        int expected = -1;
        (void)__atomic_compare_exchange_n(
            &pcc_debug_runtime_enabled_cache,
            &expected,
            value,
            0,
            __ATOMIC_ACQ_REL,
            __ATOMIC_ACQUIRE
        );
        cached = __atomic_load_n(
            &pcc_debug_runtime_enabled_cache, __ATOMIC_ACQUIRE
        );
    }
    return cached;
}

static uint64_t pcc_debug_alloc_hash(void *ptr) {
    uint64_t x = ((uint64_t)(uintptr_t)ptr) >> 4;
    x ^= x >> 33;
    x *= 0xff51afd7ed558ccdULL;
    x ^= x >> 33;
    return x & (PCC_DEBUG_ALLOC_TABLE_SIZE - 1u);
}

void pcc_debug_note_alloc_size(void *ptr, int64_t size) {
    if (!pcc_debug_runtime_enabled()) return;
    if (ptr == NULL || size <= 0) return;
    if (pcc_debug_alloc_table == NULL) {
        pcc_debug_alloc_table = (PccDebugAllocEntry *)calloc(
            PCC_DEBUG_ALLOC_TABLE_SIZE, sizeof(PccDebugAllocEntry)
        );
        if (pcc_debug_alloc_table == NULL) return;
    }
    uint64_t h = pcc_debug_alloc_hash(ptr);
    for (uint64_t n = 0; n < PCC_DEBUG_ALLOC_PROBE_LIMIT; n++) {
        PccDebugAllocEntry *entry =
            &pcc_debug_alloc_table[(h + n) & (PCC_DEBUG_ALLOC_TABLE_SIZE - 1u)];
        if (entry->ptr == NULL || entry->ptr == ptr) {
            entry->ptr = ptr;
            entry->size = size;
            return;
        }
    }
    pcc_debug_alloc_table[h].ptr = ptr;
    pcc_debug_alloc_table[h].size = size;
}

static int64_t pcc_debug_alloc_size_exact(void *ptr) {
    if (!pcc_debug_runtime_enabled()) return 0;
    if (ptr == NULL || pcc_debug_alloc_table == NULL) return 0;
    uint64_t h = pcc_debug_alloc_hash(ptr);
    for (uint64_t n = 0; n < PCC_DEBUG_ALLOC_PROBE_LIMIT; n++) {
        PccDebugAllocEntry *entry =
            &pcc_debug_alloc_table[(h + n) & (PCC_DEBUG_ALLOC_TABLE_SIZE - 1u)];
        if (entry->ptr == ptr) return entry->size;
        if (entry->ptr == NULL) return 0;
    }
    return 0;
}

static int pcc_debug_type_tag_is_valid(int32_t tag) {
    return (
        tag == PY_TYPE_NONE || tag == PY_TYPE_BOOL || tag == PY_TYPE_INT
        || tag == PY_TYPE_FLOAT || tag == PY_TYPE_STR || tag == PY_TYPE_LIST
        || tag == PY_TYPE_DICT || tag == PY_TYPE_TUPLE || tag == PY_TYPE_SET
        || tag == PY_TYPE_FUNC || tag == PY_TYPE_CLASS || tag == PY_TYPE_INSTANCE
        || tag == PY_TYPE_EXC || tag == PY_TYPE_FILE || tag == PY_TYPE_ITER
        || tag == PY_TYPE_GEN || tag == PY_TYPE_COMPLEX || tag == PY_TYPE_BYTES
        || tag == PY_TYPE_BYTEARRAY || tag == PY_TYPE_MEMORYVIEW
        || tag == PY_TYPE_COROUTINE || tag == PY_TYPE_WEAKREF
        || tag == PY_TYPE_THREAD_LOCK || tag == PY_TYPE_THREAD_RLOCK
        || tag == PY_TYPE_THREAD_EVENT || tag == PY_TYPE_THREAD_CONDITION
        || tag == PY_TYPE_THREAD_SEMAPHORE || tag == PY_TYPE_THREAD
        || tag == PY_TYPE_TASK
        || tag == PY_TYPE_CONTINUATION
        || tag == PY_TYPE_VIRTUAL_THREAD
        || tag == PY_TYPE_VTHREAD_CHANNEL
        || tag >= PY_TYPE_USER
    );
}

int32_t pcc_debug_check_tuple_slot(
    void *tuple, int64_t i, int64_t len, void *item
) {
    if (!pcc_debug_runtime_enabled()) return 0;
    int32_t reason = 0;
    int64_t exact_size = pcc_debug_alloc_size_exact(tuple);
    int64_t slot_end = 0;
    int64_t tuple_end = 0;
    if (tuple == NULL) {
        return 0;
    }
    if (i < 0 || len < 0 || i >= len) {
        reason = 1;
    } else if (len > (9223372036854775807LL - 24) / 8) {
        reason = 2;
    } else {
        slot_end = 24 + (i + 1) * 8;
        tuple_end = 24 + len * 8;
        if (exact_size > 0 && (slot_end > exact_size || tuple_end > exact_size)) {
            reason = 3;
        }
    }
    if (reason != 0) {
        fprintf(
            stderr,
            "[BAD_TUPLE_SLOT] tuple=%p i=%lld len=%lld item=%p exact_size=%lld slot_end=%lld tuple_end=%lld reason=%d\n",
            tuple,
            (long long)i,
            (long long)len,
            item,
            (long long)exact_size,
            (long long)slot_end,
            (long long)tuple_end,
            reason
        );
        fflush(stderr);
        __builtin_trap();
    }
    return 0;
}

void pcc_debug_check_release(const char *name, void *obj) {
    if (!pcc_debug_runtime_enabled()) return;
    if (obj == NULL || PY_IS_TAGGED_INT((PyObject *)obj)) return;
    int64_t exact_size = pcc_debug_alloc_size_exact(obj);
    if (pcc_gc_pointer_is_managed((PyObject *)obj) == 0) {
        fprintf(
            stderr,
            "[BAD_RELEASE] name=%s obj=%p exact_size=%lld reason=unmanaged-pointer\n",
            name != NULL ? name : "<null>",
            obj,
            (long long)exact_size
        );
        fflush(stderr);
        __builtin_trap();
    }
    void *resolved = pcc_gc_note_relocation_read((PyObject *)obj);
    if (resolved != NULL) obj = resolved;
    if (pcc_capi_is_type_object_value((PyObject *)obj) != 0) return;
    PyObjectHeader *h = py_header((PyObject *)obj);
    if (pcc_gc_backend() == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR) {
        int32_t flags = py_header_flags_load(h);
        if (
            (flags & PY_FLAG_GC_MINOR_ARENA) != 0
            && (flags & PY_FLAG_GC_OLD) != 0
            && pcc_refcount_load(&h->refcount) <= 0
        ) {
            return;
        }
    }
    if (
        h->refcount <= 0
        || !pcc_debug_type_tag_is_valid(h->type_tag)
        || (
            h->type_tag > 500
            && pcc_capi_is_cext_type_tag((int64_t)h->type_tag) == 0
        )
    ) {
        fprintf(
            stderr,
            "[BAD_RELEASE] name=%s obj=%p exact_size=%lld refcount=%lld tag=%d flags=%d reason=bad-header\n",
            name != NULL ? name : "<null>",
            obj,
            (long long)exact_size,
            (long long)h->refcount,
            h->type_tag,
            h->flags
        );
        fflush(stderr);
        __builtin_trap();
    }
}

void pcc_debug_bad_str_concat(
    void *a, void *b, int64_t tag_a, int64_t tag_b
) {
    if (!pcc_debug_runtime_enabled()) return;
    void *resolved_a = pcc_gc_note_relocation_read((PyObject *)a);
    void *resolved_b = pcc_gc_note_relocation_read((PyObject *)b);
    int64_t size_a = pcc_debug_alloc_size_exact(a);
    int64_t size_b = pcc_debug_alloc_size_exact(b);
    fprintf(
        stderr,
        "[BAD_STR_CONCAT] a=%p b=%p tag_a=%lld tag_b=%lld "
        "resolved_a=%p resolved_b=%p size_a=%lld size_b=%lld\n",
        a,
        b,
        (long long)tag_a,
        (long long)tag_b,
        resolved_a,
        resolved_b,
        (long long)size_a,
        (long long)size_b
    );
    if (size_a > 0) {
        PyObjectHeader *ha = py_header((PyObject *)a);
        fprintf(
            stderr,
            "[BAD_STR_CONCAT_A_HEADER] refcount=%lld tag=%d flags=%d\n",
            (long long)ha->refcount,
            ha->type_tag,
            ha->flags
        );
    }
    if (size_b > 0) {
        PyObjectHeader *hb = py_header((PyObject *)b);
        fprintf(
            stderr,
            "[BAD_STR_CONCAT_B_HEADER] refcount=%lld tag=%d flags=%d\n",
            (long long)hb->refcount,
            hb->type_tag,
            hb->flags
        );
    }
    fflush(stderr);
    __builtin_trap();
}
