# Investigation: Backend 4 relocation lacks a mutator-quiescent payload phase

## Status

active

## Problem Description

Backend 4 can copy, remap, and retire pointer-bearing object payloads while
holding the GC graph lock, but ordinary object/container accesses do not hold
that lock for their complete raw-memory transaction.  The newly implemented
forwarded-source payload retirement would therefore turn an existing lost-write
window into a direct use-after-free window if it freed raw list/dict/set/class
or continuation storage without first establishing mutator quiescence.

This is a prerequisite of
[`gc-backend4-forwarded-source-payload-retirement.md`](gc-backend4-forwarded-source-payload-retirement.md).
It is not a replacement for that ownership fix: quiescence proves when raw
storage may be copied/freed, while payload retirement proves which references
and allocations must be released exactly once.

## Source audit [CONFIRMED]

The C and pcc-Python `pcc_gc_store_ptr` paths invoke the slot write barrier and
then perform the actual old-value load, incref, slot store, and decref after the
barrier has released the GC graph lock.  The barrier can already have queued an
owner, raw slot address, and value in Backend 4's store buffer at that point.
Raw set/dict rehash paths likewise populate replacement storage and run
barriers while their payload arrays remain accessible outside the graph lock;
the generic load path dereferences its slot before any graph-lock acquisition.

Backend 4 evacuation and the idle remap/retirement path can run with the graph
lock but without stopping the world.  A legal interleaving is therefore:

1. a mutator enters a list/dict/set/object payload operation and reaches the
   barrier-to-store window (or pauses during a rehash/load);
2. the collector acquires the graph lock, copies or remaps the old shape, then
   detaches its side-table entry and frees the source raw payload; and
3. the mutator resumes through a stale raw slot, losing the write or accessing
   freed storage.

The dedicated root-store path is not counter-evidence: it already keeps its
root-slot lock across barrier, old-value load, incref, store, and decref.  That
lock does not protect object payload arrays.  The two-epoch forwarding-shell
quarantine also is not a quiescence proof; retaining the object header/page
does not retain an independently allocated raw payload or complete an in-flight
container operation.

This is a source-confirmed interleaving, not yet a dynamically observed crash.
No stage, performance, or broad concurrency claim follows from it.

## Additional source audit: plain phase STW is insufficient [CONFIRMED]

A stop-the-world request only waits for registered threads to report a
safepoint.  Today a thread can report one while it is still inside the raw
transaction that must finish before relocation:

- `pcc_gc_store_ptr` receives an already-computed raw slot, then its barrier can
  spin for the graph lock and call `pcc_thread_safepoint` before the subsequent
  `old = *slot`, store, and decref;
- `pcc_gc_load_ptr` reads a raw slot before resolving the value and may later
  write the healed value back to the same slot;
- dict/set rehash retains old and new raw arrays while calling barriers inside
  the migration loop; and
- graph-lock holders themselves call safepoints in relocation, remembered-set,
  aging, and promotion loops.  Parking one of those holders lets an STW owner
  wait for a lock held by a parked thread.

Therefore acquiring STW before the collector takes the graph lock is necessary
but not sufficient.  A complete design also needs a raw-access/critical-depth
contract: a safepoint reached while such a transaction or the graph lock is
held must defer parking, and the outermost safe exit must service the pending
stop.  The contract must begin before a raw slot/payload pointer can become
stale, not merely inside the barrier callback.

The public registration ABI has a separate admission race.  A normal
`pcc_thread_start` trampoline immediately safepoints before entering user code,
but a raw/extension pthread may call `pcc_current_thread_id` after an STW owner
has already counted the live threads.  Registration currently increments the
live count and returns without joining the active stop epoch.  New registered
mutators must be prevented from entering user code while another thread owns a
stopped world.

The higher-level `Thread.start` handoff has an additional managed-argument
window.  The parent retains the managed thread object, passes it as the opaque
native thread argument, and only publishes `t->handle`/started after
`pthread_create`; the child reads that opaque argument before it registers and
safepoints.  A GC4 selector can currently regard the still-handle-less thread
object as movable in that interval.  The final access protocol must keep the
managed handoff rooted/nonmoving through child registration and parent handle
publication (or make the thread object ineligible until the handoff commits);
fixing `pcc_current_thread_id` alone does not heal a stale opaque argument.

A dynamic `live_thread_count <= 1` relocation check is not an adequate
substitute: a new thread can register after the check, or forwarding can remain
active when a second thread starts.  Permanently disabling all physical GC4
forwarding in a threaded runtime would be a conservative safety slice, but it
would not prove mutator-quiescent relocation and cannot close this
investigation.

Threading is not the only admission boundary.  A single thread can be midway
through a raw mutation such as list-range deletion, decref an element, run its
finalizer or another native callback, and re-enter a public `pcc_gc_step`, copy,
drain, or forwarding API.  A moving entry must inspect the current thread's
raw-access depth and defer/no-op rather than make that same thread STW owner
inside its own transaction; the outermost access exit must make pending work
eligible again.  Ordinary `gc.collect()` is not evidence of this exact move:
the current GC4 explicit-collect branch runs tracing work instead of page
selection/drain/idle remap.  Its nested STW balance remains an adjacent gate,
not a confirmed half-mutated relocation path.

The critical region also includes diagnostic and reentrant calls, not only the
barrier.  In the strict runtime, for example, GC logging may contend on its own
lock and safepoint after the barrier but before the slot store.  List/dict/set
lookup, equality, deletion, reverse, and rehash retain raw cursors across user
callbacks or decrefs.  A low-level load/store-only counter therefore does not
cover the supported mutation surface; each complete transaction that retains
an owner-derived pointer across such a call must hold the access depth.

Some C-API surfaces deliberately return a raw payload pointer whose lifetime
outlasts the call, so no function-local access depth can protect them.
`py_str_utf8` / `PyUnicode_AsUTF8[AndSize]` and
`PyBytes_AsString[AndSize]` expose inline string/bytes storage with no matching
release operation; once exported, that object must remain physically stable
for its remaining lifetime (or use separately stable payload storage).  The
buffer protocol does have a paired lifetime: `PyObject_GetBuffer` must pin the
exporter and `PyBuffer_Release` must release that protection, including a
memoryview's owned `Py_buffer` and ultimate exporter.  GC4 tag admission cannot
open STR/BYTES/BYTEARRAY/MEMORYVIEW merely because their in-runtime accessors
are guarded.

Collector entry coverage also includes strict cross-object helpers, not just
header-public wrappers.  The strict selected-set drain currently walks
`node/next/obj` outside the graph lock and only locks each public copy and the
final remap separately.  The complete selected-set traversal must share one
STW/collector-ownership phase so two collectors cannot concurrently consume or
mutate the same selection even after mutators are parked.

Finally, payload cleanup cannot run arbitrary decrefs while both STW and the GC
graph lock remain held.  A last decref may execute a finalizer or wait for a
thread that is parked by the same stop.  The quiescent mutation phase may
detach/null/free non-reentrant storage and unlink forwarding state, but saved
reference decrefs need a stable deferred plan that is finished only after the
graph lock is released and the world is resumed (with the target-dies
self-reference exception handled separately by the payload-retirement task).

## Proposals

- No.1 Establish one relocation phase plus raw-access quiescence contract [pending]

## No.1 Establish one relocation phase plus raw-access quiescence contract

### Design boundary

The contract must cover the entire object access, not merely fuse the existing
barrier callback with one subsequent store.  A phase-level stop-the-world
boundary remains the collector-side shape: every entry capable of copying,
remapping, retiring, or target-death-cleaning a Backend 4 source must acquire
that phase before the graph lock; nested explicit collections must use the
existing depth-aware STW contract without an early resume.  On the mutator
side, however, an access/critical depth must make any safepoint reached inside a
raw transaction non-parking and must park at the first safe outer exit when a
stop is pending.  Graph-lock acquisition and release need the same rule so a
holder cannot park with the lock held.  First-time thread registration must
join an already-active stop epoch before returning.

Raw pthread registration must also be symmetric.  Because
`pcc_current_thread_id` is a public registration path, A1 must expose a matching
`pcc_thread_unregister_current` contract: a registered raw thread calls it
before exit, an unregister at nonzero no-park depth fails closed, and the normal
thread trampoline uses the same implementation.  Otherwise a terminated raw
thread leaves `live` permanently elevated and a later stop can wait forever.
An STW owner must likewise fail-stop before any exception/buffer cleanup or
world-state mutation; removing the owner while `stop_requested` remains set
would make the epoch permanently unresumable.  In the pthread strict kernel,
world-initialization failure cannot fabricate thread id 1: admission must either
publish real TLS/live state or fail-stop.  This bounded slice does not add an
automatic TLS-destructor fallback.

A diagnostic first-registration waiter count may be used to make newcomer
tests deterministic, but it is not an access lease or a correctness gate by
itself.  The count is maintained under the world mutex only while a first-time
thread is actually blocked by an active stop; the collector still relies on
the existing live/park accounting.

Thread admission alone does not make the opaque handoff a managed root.  The
parent-to-child `pcc_thread_start` argument and the child-to-parent
`PccThreadMain` result stored in the raw thread handle both need an explicit
root/update lifetime before Backend 4 motion can be allowed across those
windows; A1 proves entry/admission and accounting only.

Generated mutator polls remain a separate phase-integration blocker.  The LLVM
poll sites currently issue ordinary loads of `pcc_thread_stop_requested`, while
the C and strict owners update it with ordinary stores under a mutex that the
poller does not hold.  Explicit safepoint calls in an A1 harness cannot prove
this data-racy poll observes the request.  Before the moving phase is accepted,
the publisher/poller pair needs release/acquire (or a stronger atomic contract)
and an IR-shape plus compiled-loop gate.

Any moving collector/direct-relocation entry from a thread whose own access
depth is nonzero must defer; it must not make itself STW owner inside its own
transaction.  The final detach/remap phase must also separate non-reentrant
structural commit from post-resume reference decrefs.

The per-tag admission registry must additionally prove all raw-pointer export
lifetimes.  Unpaired UTF-8/bytes exports require lifetime stability; paired
buffer exports require balanced pin/release.  `PySequence_Fast_ITEMS` and
`PySequence_Fast_GET_ITEM` likewise expose list out-of-line items or tuple
inline storage without a release operation, while `PyUnicode_DATA` and
`PyUnicode_1BYTE_DATA` inherit the UTF-8 raw-pointer lifetime.  Their exporting
objects therefore need an object-lifetime pin or a stable out-of-line payload;
a function-local access critical section cannot protect the caller's retained
pointer.  Borrowed C-API list/tuple/dict element results need the same owner
root/pin policy.  Tags remain fail-closed until both their internal transaction
family and every external raw view are covered.

The existing `pcc_gc_pin`/`pcc_gc_unpin` state is a single bit, so it cannot
serve as the balanced `PyObject_GetBuffer`/`PyBuffer_Release` contract: two
simultaneous views followed by one release would clear the only pin while the
second view still owns a raw address.  Paired views require a nested lease or
export count whose final release alone permits motion.  In contrast, unpaired
UTF-8/bytes/sequence exports need a lifetime pin because the caller has no
release operation.

Allocation publication is a separate entry edge.  Backend 4 registration does
not currently mark a newly registered object `PY_FLAG_GC_FRESH_ALLOC`, while
constructors initialize payload, roots, and raw spans after `pcc_gc_alloc`
returns.  A supported tag therefore cannot become selectable between allocation
and complete initialization.  Each constructor must either hold the outer
access transaction from publication through initialization or use a Backend 4
unpublished/fresh state plus an explicit release-publish operation that the
selector excludes.

Callback splitting also needs an updateable root, not just `incref`.  A C stack
local that snapshots a managed pointer is not automatically rewritten by
Backend 4; a callback can span multiple remap/retirement epochs and leave that
owned local pointing at a retired shell.  The guarded transaction must
canonicalize and retain the value, register the local as a temporary/native
updateable root, exit for the callback, then re-enter and reload the rewritten
root before version/identity validation and commit.  The strict path must prove
its compiler frame map provides the same updateability or use the same explicit
root scope.

Owner-aware store/load helpers must also keep reentrant decrefs outside the raw
transaction.  A store transaction enters before owner/slot derivation,
canonicalizes owner and new value, executes the barrier, retains the new value,
swaps the slot while saving the old value, then exits before decrefing the old
value.  A loaded value that survives the guard is retained and rooted before
exit; a merely borrowed load may only be consumed while the same guard remains
active.

An access-ticket/hazard alternative is acceptable only if it covers generic
loads, direct pointer stores, fresh-instance stores, list/dict/set rehash, and
all other raw-payload operations, prevents new accesses from entering the
retiring epoch, and redirects or completes writes whose slot was computed from
a pre-forwarding source.  A counter around the barrier callback alone is not
sufficient.

### pending

No protocol has been accepted or measured.  The first green proof must include
a true-pthread three-party harness: one mutator has already computed/entered a
production-shaped raw container access, a second thread creates graph-lock
contention, and the collector requests quiescence.  It must prove the mutator
does not park mid-transaction, the lock holder does not park while holding the
lock, relocation cannot complete early, and the committed write/refcounts/
forwarding retirement are correct.  A separate start-during-STW gate must cover
both `pcc_thread_start` and a raw pthread that registers through
`pcc_current_thread_id`.  A managed `Thread.start` gate must separately cover
the parent/child opaque-argument and handle-publication window.

## Update — 2026-08-22 A1 C-oracle fail-stop link closure

The bounded A1 source/static slice reached two independent zero-finding reviews,
but its first compiled gate is red and all later compiled gates remain stopped.
The exact fail-fast command was:

```bash
gtimeout 240s zsh -o pipefail -c 'gtimeout 210s env -u LC_ALL uv run pytest -vv -x -n0 --tb=short "tests/python/test_gc_threading_substrate.py::test_thread_no_park_nonthread_depth_and_world_owner_contract[c]" 2>&1 | tee build/gc4-a1-c-nonthread-v2.log'
```

It fails deterministically during the final probe link after 6.63 seconds:
`pcc_threads.o` references `_pcc_platform_abort`, but the host-C oracle archive
has no owner for that freestanding pcc-Python symbol.  The log SHA-256 is
`217d3b0330d5fcd2ca6d17d28a50ff356f5e7424494b883554fd52897acd9ed3`
at `pcc_threads.c` SHA-256
`71ac4365d4e94a5013b78a88040ecf4b859af114bcf5ee53a2b9ccf161d8ece2`
and test SHA-256
`750acde877a882dde84bee6967c8b71b8e0fbc2ecc91e2b54bd4f685ccbfef20`.

This is a link-ownership error, not a no-park runtime verdict.  The C oracle
already owns libc and includes `<stdlib.h>`; its smallest pending correction is
to use libc `abort()` for these unconditional fail-stops and remove the new
undefined extern.  The strict pcc-Python thread kernels continue to use their
freestanding `pcc_platform_abort` owner.  Routing through
`pcc_runtime_tripwire_fail` is deliberately rejected because logging,
registration and lock acquisition are unsafe work to introduce into recursive
unregister or stopped-world-owner fail-stop paths.  The same compiled node must
turn green on a new content-addressed archive before any later A1 shard runs.

## Update — 2026-08-22 A1 thread-quiescence substrate confirmed green

### confirmed

The bounded A1 substrate is now green on the final source identity.  The host-C
oracle uses libc `abort()` at its nine unconditional thread fail-stop sites and
has no `pcc_platform_abort` reference; the strict pcc-Python kernels retain the
freestanding platform-process owner.  The formerly red C nonthread node passes
in 6.38 seconds on the new content-addressed archive.  The C pthread newcomer,
trampoline, unregister fail-stop, exact-owner, and biased/deferred refcount
gates also pass.  The strict nonthread gate passes in 122.47 seconds and the
five-node strict threaded shard passes in 125.30 seconds.  The final four-node
source/ABI contract and two-node cache-isolation contract are green.  Two
independent read-only reviews report zero findings for this exact A1 boundary.

The durable evidence, exact source/test/cache hashes, commands, log hashes, and
four archive receipts are recorded in
`docs/goal/evidence/2026-08-22-gc4-a1-thread-quiescence-substrate.md`.  The
archive receipt bundle is
`build/gc4-a1-archive-receipts-current.json`, SHA-256
`c8eae3f52710a354734fe4a131760113c689b7dcf56058dfa2de7f2367610a89`.

This closes A1 only: TLS no-park depth, newcomer admission, raw-thread
unregister/fail-stop, diagnostic stop publication, logger/refmeta lock order,
thread-trampoline teardown ordering, and exact C/strict/nonthread ABI/cache
ownership.  It does not prove the generated LLVM ordinary stop poll, graph-lock
or container raw-access integration, managed thread argument/result rooting,
constructor publication, C-API raw-view lifetime, collector-phase coverage,
forwarded-source payload retirement, or any physical Backend 4 relocation under
concurrent mutators.  It also does not prove that the transitional pcc-C
frontend can parse/link the newly used `abort()` call through its fake-libc
surface.  Those remain open in this investigation; the task remains
`IN_PROGRESS`.

## Update — 2026-08-22 A1 pcc-C transition edge confirmed green

### confirmed

The narrow transition boundary left by A1 is now compiled and linked.  Host pcc
emits the current default `pcc_threads.c` object; an explicit
`PCC_WITH_THREADS=0` emission imports host-libc `abort` and does not import
`pcc_platform_abort`; placing that emitted object before the immutable host-C
runtime archive links and executes the no-park depth sequence `0 -> 1 -> 0`.
The final two-node gate passes in 1.75 seconds, and an independent read-only
review reports zero findings.  Exact command, hashes, and nonclaims are in
`docs/goal/evidence/2026-08-22-gc4-a1-pcc-c-abort-transition.md`.

This closes only the default/nonthread translation-unit edge.  It does not
prove a pthread pcc-C emission, a complete pcc-C-built runtime archive, strict
pcc-Python ownership, or any relocation behavior.

## Update — 2026-08-22 A2a generated stop poll confirmed green

### confirmed

Both Python-frontend implicit poll emitters now load
`pcc_thread_stop_requested` using an atomic acquire load with four-byte
alignment.  The emitted-IR gate rejects every ordinary stop-flag load, while
the threads-off gate continues to emit no implicit poll.  A current host
compiler invocation with `backend=self`, `ir_scaffold=on`,
`python-libpython=off`, and the threaded host-C runtime passes a deterministic
real-STW regression: the worker enters an unbounded relaxed-load gate loop, the
main thread stops the world before opening the gate, and the worker cannot exit
until resume.  The canonical four-node shard passes in 1.24 seconds; three
atomic llvm_capi/AArch64 neighbors pass in 0.50 seconds.  Independent read-only
review reports zero findings.  Exact source/test/log identities are in
`docs/goal/evidence/2026-08-22-gc4-a2a-generated-stop-poll.md`.

An optional llvmlite-PY attempt remains red before reaching this lowering due
to the pre-existing `FunctionAttributes._attrs` compatibility failure.  A2a
therefore proves the default llvm_capi emission and current Darwin self-backend
execution path, not a llvmlite-PY path, strict threaded runtime execution, or
pcc1.  Graph-lock ownership, complete raw object-access transactions, managed
thread handoffs, constructor/raw-view lifetime, the collector phase, and
payload retirement remain open.  The task remains `IN_PROGRESS`.

## Update — 2026-08-22 A3a tracing final-cut STW lift

### RESOLVED sub-boundary

Freeze #5 removes the shared GC1/GC2 tracing final-cut graph-lock-to-STW
inversion in both the C differential oracle and strict freestanding
pcc-Python production objects.  A tracing step now captures one
`(cycle_epoch, selected_backend)` claim under the graph lock and releases the
lock before acquiring STW; the GC2 CMS worker can reuse its caller-owned
stopped world.  Under STW plus the reacquired graph lock, the claimant
revalidates the captured claim, cycle, backend, and active state before the
pure final cut rescans roots, drains gray work, selects white candidates, and
commits state.  The commit preserves a new cycle request, the graph lock is
released before helper-owned resume, and stop-failure clearing changes only
the matching captured token.  Monotonic non-reused epochs, same-backend reset,
backend switch, two-finisher races, and the strict cross-object ABI are covered
by the frozen focused gates.

Exact source/test hashes, RED-to-green history, node results, durable log and
cache-receipt hashes, two independent read-only ZERO reviews, and nonclaims are
recorded in
`docs/goal/evidence/2026-08-22-gc4-a3a-tracing-final-cut-stw-lift.md`.

A3a closes only the tracing final-cut inversion; it does not make the graph
lock itself safe to connect directly to no-park.  The next A3b boundary is
**graph-lock bounded-region preparation**: audit and eliminate or defer every
decref/finalizer, runtime log or blocking I/O, allocator/free, callback, and
CAS-waiter safepoint/usleep inside graph-lock holder regions.  Root-store
deferred decref/log is the priority candidate, subject to the pending
read-only route verdicts.  Only after those holder regions are bounded leaves
may A3c connect the outermost graph-lock acquire/release to no-park: recursive
locking increments depth only, and the outer unlock must occur before
no-park exit can service a pending stop.

Complete raw container transactions, callback/root handoffs, constructor
publication, raw views and buffer leases, the collector-owned relocation
phase, and forwarded-source payload retirement remain later open boundaries.
No physical Backend 4 relocation, stage, performance, CPython, broad parity,
or fixed-point claim follows from A3a.  The investigation and task remain
`active` / `IN_PROGRESS`.

## Update — 2026-08-22 A3b outermost root-store tail deferral

### RESOLVED sub-boundary

The first bounded A3b holder slice is focused green for one stable backend and
an **outermost** non-GC0 `pcc_gc_store_root` invocation's own lock scope.  In
the C transition oracle and strict freestanding pcc-Python production mirror,
the helper prepares the incoming retain and barrier value, snapshots the old
root, commits the new slot, then prepares exactly one old-value decrement while
still locked; a terminal prepare marks that old object `DEALLOCATING` before
unlock.  Store/refcount logging, debug failure, weakref invalidation,
finalizer/deallocator and other terminal cleanup follow only after unlock.
Finish consumes captured state and does not reread the slot, re-resolve the
pointer, or decrement again.

Compiled C and strict execution covers GC3/GC4 with threads enabled and the
default `ATOMIC` refcount strategy.  The strict cold five-node shard passes in
125.24 seconds.  Backend 4 add/score/copy quarantine passed from the same
archive; the paired strict GC3 known-object node first stopped on a test-only
missing `<sys/mman.h>` include, then passed in 0.74 seconds after that include
alone changed.  Runtime-archive provenance verifies schema
`pcc.runtime-archive-provenance.v2`, 186 members, 444 C-API symbols and policy
`pcc-production-no-handwritten-c.v1`.  Independent runtime/design and
test-sufficiency reviews both report ZERO findings for the final hashes.
Exact source/test/archive identities, RED-to-green corrections, commands,
durable log hashes and nonclaims are recorded in
`docs/goal/evidence/2026-08-22-gc4-a3b-outermost-root-store-tail.md`.

This does not close every root/store tail.  Strict scheduler queue push/pop
already hold an outer recursive graph lock when they invoke root-store, so the
helper's unlock only changes depth two to one; the C scheduler has the same
larger-region problem through `pcc_gc_store_ptr`.  Those nested holders are the
next priority.  GC2 CMS queue/safepoint flush, GC4 tripwires,
`BIASED`/`DEFERRED` refmeta paths, concurrent backend switching, unlocked
public decref synchronization, and the complete callback/free/wait/allocator
holder audit also remain open.  Default/nonthread execution, resurrection
metadata restoration, graph-lock/no-park integration, raw container
transactions, physical relocation/retirement, stage/performance and broad
parity were not proved.  The investigation and task remain `active` /
`IN_PROGRESS`.

## Update — 2026-08-22 A3b scheduler queue root-transfer transaction

### RESOLVED sub-boundary

The nested scheduler queue successor is now focused green for one stable
backend, valid values and mutex lifetime, threads enabled, default `ATOMIC`
refcounts, and GC3/GC4 in the C transition oracle and strict freestanding
pcc-Python production runtime.  Push, pop, live-entry free, and failed
publication/queue-mutex release now use one private 128-byte cross-object
root-store plan.  Allocation and plan initialization occur before the graph
lock; prepared retain/barrier/store/decrement plus root link/unlink are the
locked structural transaction; and cycle request, root-node and queue-entry
cleanup, logging, weakrefs, finalizers and terminal deallocation occur only
after graph unlock.  Pop commits the output retain before clearing its queue
entry, and GC3/GC4 forwarding, exact refcounts/root counts, live free, and
exact entry-address free-list reuse are covered in both mirrors.

The old C/GC3 pop implementation deterministically deadlocked: a finalizer
joined a true pthread whose real queue-mutex then graph-lock path contended for
the graph lock retained by the finalizer's caller.  The child watchdog RED was
**1 failed in 20.68s**.  On the final seven-file identity, static/order/layout
passed **1 in 0.34s**, C forwarding/count/reuse passed **2 in 0.68s**, C
finalizer handshakes passed **2 in 0.30s**, strict cold GC3 passed **1 in
138.75s**, the remaining strict three nodes passed **3 in 1.80s**, precise
root neighbors passed **15 in 3.57s**, and queue neighbors passed **5 in
9.06s**.  Independent runtime/design and source/test/ABI test-sufficiency
reviews both report ZERO findings.  Exact hashes, commands, log/cache receipt
identities, review corrections, supported claim and nonclaims are recorded in
`docs/goal/evidence/2026-08-22-gc4-a3b-scheduler-root-transfer.md`.

This does not make the graph lock a bounded no-park leaf.  The next A3b route
is the **GC2 C CMS graph-lock to queue-lock/safepoint pending-flush boundary**.
A TLS write-barrier buffer full flush can still run under the outer graph lock
and acquire the CMS queue lock, whose wait path polls a safepoint.  The
immediate bounded successor is C-only: record exact TLS pending/overflow/
epoch/count state for the 32nd/33rd-and-later barriers while locked, service
eligible work only after the outermost unlock, ensure nested unlock never
drains, and introduce no new loss while the queue admits the whole batch.
Sequential/serialized stop, switch-away, reset/restart, and unregister must
clear or invalidate pending/count/overflow/epoch state (or make an epoch
mismatch drop it without service), while simultaneous backend-switch admission
remains later and threads-off behavior stays unchanged.  It
must remain narrow: queue-full or partial-batch delivery, thread-exit delivery,
general concurrent backend-switch ownership, and a strict algorithmic mirror
remain later open blockers.  The strict side currently has no TLS CMS queue/
buffer/lock algorithm, so a negative source gate can freeze the absence of the
same lock/safepoint edge but cannot be labeled algorithmic parity.

`BIASED`/`DEFERRED`, GC4 tripwires, concurrent backend switching, strict
debug-invalid parity, mutex fault injection, destructive same-queue reentry
during free, broader C `store_ptr` holders, resurrection restoration, full
holder/no-park integration, raw-access/collector transactions, physical
relocation, stage/performance and broad five-GC parity remain unproved.  The
investigation and task remain `active` / `IN_PROGRESS`.

## Update — 2026-08-22 A3b C GC2 CMS pending-flush deferral

### RESOLVED sub-boundary

The narrow C GC2 write-barrier/TLS boundary is focused green for one stable
selected backend, valid managed values and mutex lifetime, and the default
pthread `ATOMIC` refcount mode, plus one exact threads-off drain case.  The
32nd buffered identity now arms TLS pending state and the 33rd-and-later
identities arm overflow state without acquiring the CMS queue lock, polling a
safepoint, stopping the world, or publishing telemetry while a graph lock is
held.  Nested graph unlock does not service pending work; the outermost
threaded unlock releases graph ownership before service, and the nonthread
path drains its complete local batch.

The dynamic 40-object probe observes zero pushes/flushes before outer unlock,
then exactly 32 identity publications plus one all-gray sentinel and one flush;
the worker closes all 40 identities plus an independent gray root before any
reset or backend switch can mask the observation.  Rejected valid suffixes and
the overflow token remain in TLS until accepted, without claiming eventual
delivery under full/partial queue acceptance.  Serialized CMS reset pauses
without resetting or discarding queued/TLS work; admitted work may progress
during pause/join or a later failure unlock.  Covered failures restart CMS
without a lifecycle reset/discard or new loss, while successful commit alone
resets epoch/queue/TLS.  Caller-owned graph depth, no-park ownership, and
stopped-world ownership fail closed before pause or state mutation and
therefore preserve exact state.  Legal depth-zero unregister attempts service
then clears TLS.  The strict freestanding pcc-Python gate freezes only the
absence of the C TLS queue/buffer/lock edge and is not algorithmic parity.

The old 32nd-entry path published through the CMS queue while its caller's
outer graph lock remained held; the exact probe failed in 0.43 seconds.  That
RED proves a graph-lock-to-queue-lock edge whose contended loop can safepoint,
not actual queue contention, a safepoint wait, or deadlock.  On the final
four-file identity, the seven-node claim gate passes in 0.51 seconds and the
complete C CMS neighbor file passes 11 nodes in 1.01 seconds.  Both threaded
and threads-off C syntax gates pass, and independent runtime/design and
source/test-sufficiency reviews report ZERO findings.  Exact hashes, all
retained RED-to-green commands, final commands/results, artifact absence and
nonclaims are recorded in
`docs/goal/evidence/2026-08-22-gc4-a3b-cms-pending-flush.md`.

This does not prove every CMS producer, queue-full or partial-batch eventual
delivery, thread-exit delivery of a rejected suffix, simultaneous backend
switch admission, injected allocation/index/worker/queue faults,
`BIASED`/`DEFERRED`, strict algorithmic parity, or global CMS
termination/performance.  The next A3b route is the remaining **GC3/GC4
graph-lock holder audit**: safepoint-capable waits/CAS loops,
decref/finalizer, allocator/free, callbacks, runtime logging/blocking I/O,
tripwires, refmeta paths and broader C `store_ptr` holders must be removed,
deferred, or proved bounded while locked in the relevant C/strict mirror.
Only after those holders are bounded non-parking leaves may A3c connect the
outermost graph-lock acquire/release to no-park.  Raw-access/collector
transactions, physical relocation/retirement, stage/performance and broad
five-GC parity remain later.  The investigation and task remain `active` /
`IN_PROGRESS`.

## Update — 2026-08-22 A3b GC4 generation-aging tenure

### RESOLVED sub-boundary

The GC4 generation-aging holder is focused green for one stable selected
backend, valid tracked objects, threads enabled and default `ATOMIC` refcounts
in the C transition oracle and strict pcc-Python runtime.  GC3 and GC4 now
share the existing intrusive pending-young worklist.  Each generation-aging
graph-lock tenure detaches and examines at most 16 nodes, counts stale
non-young maintenance work against the budget, performs only real
`YOUNG -> OLD` transitions, updates the containing ZPage directly, releases
the graph lock, and only then polls.  A public step may repeat tenures up to its
budget; examined work and successful-promotion telemetry remain distinct.

The strict ABI owner moved, without a symbol/signature change, from ordinary
`py_gc_backend.py` into the existing freestanding generational scheduler.  The
move is required because ordinary runtime functions receive compiler-injected
entry/backedge polls: the pre-fix strict handshake parked before aging and
observed zero promotions.  The freestanding holder has no injected poll while
locked, and its explicit poll remains after unlock.  Tracked allocations whose
final header is explicitly `YOUNG` now join the worklist under GC1/GC2 as well
as the normal GC3/GC4 paths, so a later trackable switch cannot lose pending
generation work.  Explicit `OLD` objects remain excluded.

The old C algorithm deterministically parked at its 16th promotion while
retaining the graph lock; the stopped-world owner then blocked on that same
public graph lock and the child watchdog produced `1 failed in 10.74s`.  A
separate lifecycle RED proved an explicit-YOUNG GC1 allocation was missing
after switching to GC4 (`rc=12`, `1 failed in 0.37s`).  On the final source
identity, the C/strict behavior plus static packet passes 7 nodes in 1.77s,
the final strict cold packet passes 3 in 123.85s, focused GC4/GC3 neighbors
pass 7 in 7.76s, and source/LLVM/self/archive owner-closure gates pass 13 in
3.80s.  Threaded and threads-off C syntax, Python compilation, diff hygiene,
and current archive provenance are green.  Exact hashes, commands, log and
archive receipts, interrupted-run disclosure and claim boundaries are in
`docs/goal/evidence/2026-08-22-gc4-a3b-generation-aging.md`.

This closes only the GC4 generation-aging direct graph-lock-to-poll edge and
its bounded-node-tenure worklist lifecycle.  It does not make the whole graph
lock a no-park leaf or prove formal atomic wait-freedom.  The next A3b slice is
the remaining GC4 remembered-root / relocation-selection holder boundary,
which still includes in-lock decref/free/polls and allocator/selection work;
the GC3 generational holder, tripwire parity, refmeta paths and other holders
also remain.  A3c no-park integration, raw access, physical relocation,
stage/performance, fixed point and broad five-GC parity remain unproved.  The
investigation and task remain `active` / `IN_PROGRESS`.

## Update — 2026-08-22 A3b GC4 remembered-root drain tail

### RESOLVED sub-boundary

The direct GC4 remembered-root drain edges to allocation/free, last-decref /
finalizer and safepoint work are focused green for one stable backend, valid
managed values, an outermost public step, threads enabled and default `ATOMIC`
refcounts in the C transition oracle and strict freestanding pcc-Python
runtime.  Entry detach, valid promotion, root rewriting, `REMEMBERED` state and
work accounting remain under the graph lock.  C preallocates global-list nodes
before that outermost transaction; both runtimes unlock before freeing detached
nodes, dropping saved buffer references, running finalizers and polling.

The strict ABI owner moved from ordinary `py_gc_backend.py` to the existing
freestanding generational scheduler, preventing compiler-injected polls in the
locked body.  Public work now counts every detached maintenance entry.  A
post-drain pending check gates the observed later GC4 aging/evacuation/
selection/tracing/remap chain, so a ten-entry, budget-ten step returns eight
and leaves two rather than returning 19 after unrelated relocation work.
Valid C/strict GC4 promotion now uses an acquire-release adjacent-bit atomic
add, preserving concurrently published unrelated header flags.  C maximum
batch CAS telemetry runs after unlock.

The old final-decref path formed a deterministic finalizer-join/real-graph-lock
cycle and failed after 10 seconds (`1 failed in 10.49s`).  On the final source
identity, C/strict finalizer, maintenance and static nodes pass 5 in 1.43s; the
strict cold pair passes 2 in 124.21s; unique owner/barrier behavior passes 5 in
8.53s; store-buffer and generation-aging neighbors pass 14 in 2.46s.  Python
compilation, threaded/threads-off C syntax, diff hygiene and the current strict
archive provenance are green.  Exact hashes, commands, receipts, timed-out
non-evidence and claim boundaries are recorded in
`docs/goal/evidence/2026-08-22-gc4-a3b-remembered-root-drain.md`.

This is not a fully bounded remembered-root leaf.  Owner-pending/referent scans,
the enqueue-side medium flush/broader `store_ptr` holder, concurrent enqueue
between the post-drain check and later-phase acquisition, nested outer-lock
callers, allocation-failure injection, formal atomic wait-freedom and armed
tripwire/log behavior remain open.  Relocation selection/drain, GC3 holders,
refmeta, callback roots, resurrection, physical relocation, A3c no-park,
stage/performance, fixed point and broad five-GC parity remain unproved.  The
investigation and task remain `active` / `IN_PROGRESS`.

## Update — 2026-08-22 A3b GC4 relocation-selection transactions

### RESOLVED sub-boundary

The direct object/page relocation-selector edges to plan allocation and
safepoint work are focused green for one stable Backend 4 selection, valid
managed values, threads enabled and default `ATOMIC` refcounts in the C
transition oracle and strict freestanding pcc-Python runtime.  Object plans are
allocated before graph-lock acquisition and committed in batches of at most 16;
unused plan cleanup and polling occur after unlock.  Page selection snapshots
one eligible ZPage, allocates the page node plus complete relocation-node
capacity outside the lock, revalidates page population at first commit, and
commits at most 16 page-local objects per tenure.  Constructor-pending pages are
ineligible.  The page-local object chain is authoritative; the strict mirror
now reads the ZPage-node size at its actual offset 32.

The old C object selector deterministically parked at an in-lock safepoint and
failed after 10 seconds; the corrected strict-compatible old selector did the
same (`1 failed in 11.02s`).  The old C page selector committed all 32 objects
in one graph tenure and failed its stopped-world midpoint observation in 6.66
seconds.  Strict page selection initially returned 31/32; diagnostics proved
all identities shared one page and isolated the wrong offset-24 size read.
Against the final source/archive identity, the C/strict/static/source-owner/
closure packet passes 10 nodes in 3.73 seconds and 12 exact page-policy,
selection and whole-page evacuation neighbors pass in 0.94 seconds.  The final
strict cold page node passes in 123.47 seconds with a durable log, both C syntax
modes and Python compilation are green, and archive provenance matches the
frozen strict source.

Exact hashes, retained RED-to-green chronology, commands, archive receipts and
claim boundaries are recorded in
`docs/goal/evidence/2026-08-22-gc4-a3b-relocation-selection-transactions.md`.
The final identity and evidence were reviewed locally after the request to
minimize agent usage; no independent sub-agent verdict is claimed.

This is not a fully bounded selector leaf.  Global candidate and page-local
membership scans remain potentially unbounded under the graph lock; page
preflight/commit lacks a formal destroy/reuse epoch/ABA and starvation proof;
constructor publication has no dedicated race handshake; low-level atomics lack
a wait-free proof.  Relocation drain/copy/remap/retirement, remembered-root
bounds and phase admission, GC3 holders, nested callers, tripwire/log/refmeta,
callback roots, resurrection, physical movement, A3c no-park, stage/performance,
fixed point and broad five-GC parity remain open.  The investigation and task
remain `active` / `IN_PROGRESS`.

## Update — 2026-08-22 A3b GC4 bounded relocation-selection scans

### RESOLVED sub-boundary

The remaining global-candidate and page-local membership scans are now bounded
to at most 16 examined entries per graph-lock tenure for one stable Backend 4
selection, valid managed values, threads enabled and default `ATOMIC`
refcounts, in both the C transition oracle and strict freestanding pcc-Python
runtime.  Persistent cursor/best state lets each selector unlock and poll
between chunks without carrying an unregistered local list cursor.  Object and
ZPage unlink paths advance or invalidate that state before recycling nodes.

The source review also removed two hidden full-list dependencies from scoring.
Candidate size comes directly from the ZPage node's `size_bytes`.  Remembered
pressure is an O(1) per-owner ZPage counter, mirrored by an internal 80-byte
C/strict node layout with the counter at offset 72.  The common remembered
add/remove/clear/retarget path maintains the count, preserving the existing
dirty-owner selection policy without a graph-locked global remembered scan.

The final C selector pair passes in 7.03 seconds; the strict pair passes in
123.58 seconds against a fresh provenance-verified archive.  Remembered-
pressure integration passes in both modes, 12 selector/page policy neighbors
pass in 0.95 seconds, ZPage owner/layout/state-machine gates and strict
source/closure/archive-owner gates are green, and syntax/diff/provenance checks
pass.  Exact hashes, RED/correction chronology, archive receipts and bounded
claim wording are recorded in
`docs/goal/evidence/2026-08-22-gc4-a3b-relocation-selection-bounded-scans.md`.
The final review was local after the request to minimize agent use; no
independent sub-agent verdict is claimed.

This does not close relocation drain/copy/remap/retirement holders,
remembered-root owner/referent bounds or enqueue-to-phase-admission races.  Page
destroy/reuse epoch/ABA and starvation safety, constructor-publication races,
formal atomic/index wait-freedom, concurrent backend switching, nested callers,
GC3 holders, tripwire/log/refmeta paths, callback roots, resurrection,
physical movement, A3c no-park, stage/performance, fixed point and broad
five-GC parity remain open.  The investigation and task remain `active` /
`IN_PROGRESS`.

## Update — 2026-08-22 A3b GC4 page-drain/copy preallocation tail

### RESOLVED sub-boundary

For one stable Backend 4 selection, valid managed values, threads enabled and
default `ATOMIC` refcounts, the outermost public page-drain/copy path now
releases its own graph-lock scope before destination allocation, detached-node
cleanup, returned/failed destination decref and safepoint work in both the C
transition oracle and strict freestanding pcc-Python runtime.  Page drain
captures at most 16 sources under the lock.  Public copy snapshots eligibility,
unlocks to allocate, re-locks to commit, then unlocks before consuming a
two-pointer finish plan and performing failure cleanup.

The true-pthread C/strict handshake selects 32 objects, publishes a real stop
request, and proves the stopped-world owner can acquire the same graph lock at
the first destination-allocation safepoint while all 32 candidates are still
present.  After resume both modes move all 32 objects with exact relocation and
forwarding counts.  The final strict cold node passes in 123.23 seconds; exact
source/closure, archive-owner/differential, page-handoff/retirement, selector,
quarantine, telemetry, syntax and provenance neighbors are green.  Exact
hashes, RED/correction chronology, logs and receipts are recorded in
`docs/goal/evidence/2026-08-22-gc4-a3b-relocation-page-drain-copy-preallocation.md`.
The final review was local after the request to minimize agent use; no
independent sub-agent verdict is claimed.

This does not make the locked copy commit a bounded leaf.  Copy-payload,
forwarding, identity/index and ZPage work, the legacy graph-locked object drain,
final remap/retirement, nested callers, concurrent drain/page lifetime and
destroy/reuse epoch/ABA, stale-candidate fairness, remembered-root admission,
GC3 holders, raw mutator quiescence, A3c no-park, stage/performance, fixed point
and broad five-GC parity remain open.  The investigation and task remain
`active` / `IN_PROGRESS`.

## Update — 2026-08-22 A3b GC4 relocation object-drain tail

### RESOLVED sub-boundary

For one stable Backend 4 selection, valid managed values, threads enabled and
default `ATOMIC` refcounts, the outermost public object drain now snapshots at
most `min(remaining_budget, 16)` sources under the graph lock, unlocks before
public size/copy, returned-target decref and safepoint work, and reloads the
authoritative relocation-set head for each tenure in both the C transition
oracle and strict freestanding pcc-Python runtime.  Incomplete-batch telemetry
and remap-if-drained remain in one final short graph-lock tenure.  The unused C
private unlocked-copy helper was removed.

The C tracer first failed deterministically in 10.47 seconds: a drain worker
parked at destination allocation while retaining the graph lock, and the
stopped-world owner then waited on that same lock.  The final C node passes in
6.68 seconds and the strict cold node passes in 123.40 seconds.  The combined
C/strict object/page handshake, exact source/closure/order contract,
archive-owner/differential, incomplete-batch/page/retirement, quarantine,
syntax and provenance gates are green.  Exact hashes, command results, logs and
receipts are recorded in
`docs/goal/evidence/2026-08-22-gc4-a3b-relocation-object-drain.md`.  The final
review was local with no sub-agent use or independent sub-agent verdict.

This does not close copy-payload allocation/ownership, forwarding,
identity/index or ZPage commit work, the strict internal unlocked-copy ABI,
final remap/retirement, nested callers, concurrent drains, source/page lifetime
or destroy/reuse epoch/ABA, stale-candidate fairness, remembered-root admission,
GC3 holders, raw mutator quiescence, A3c no-park, stage/performance, fixed point
or broad five-GC parity.  The investigation and task remain `active` /
`IN_PROGRESS`.

## Update — 2026-08-22 A3b GC4 relocation owned-slot retain tail

### RESOLVED sub-boundary

For one stable Backend 4 selection, valid managed values, threads enabled and
default `ATOMIC` refcounts, public relocation copy now counts source slots
under graph lock, allocates its slot-retain plan and destination after unlock,
revalidates and performs canonical retain-before-destination-publication under
one commit lock, then unlocks before retain logging/diagnostics and detached
structural cleanup in both the C transition oracle and strict freestanding
pcc-Python runtime.  Strict validation-failure cleanup now consumes a
zero-initialized finish plan.

The old strict two-argument unlocked-copy ABI is gone.  The remaining
five-argument preallocated commit helper is private to the freestanding
cross-object ABI and absent from public runtime signatures and headers.  The
final strict owned-slot node passes in 122.71 seconds against a fresh
provenance-verified archive; exact C runtime, strict/C source and LLVM/self
closure, GC3 oldification, payload-span, forwarding-retirement, syntax and
refcount-wrapper neighbors are green.  Exact hashes, the genuine source RED,
the strict callback-context runtime RED, the finish-plan review correction,
commands, log and archive receipts are recorded in
`docs/goal/evidence/2026-08-22-gc4-a3b-relocation-slot-retain-tail.md`.
The final review was local after the request to minimize agent usage; no
independent sub-agent verdict is claimed.

This does not close type-specific raw-buffer allocation/copy/rollback or ZPage
span registration inside the payload commit, forwarding/identity-index/ZPage
commit, final remap/retirement, raw mutator quiescence or target-death cleanup.
The private strict helper relies on an internal graph-lock precondition and the
GC3 compatibility wrapper still performs plan preparation/finish inside its
generational holder.  Nested callers, concurrent drains/backend switches,
selected-source/page lifetime and ABA, remembered-root admission,
invalid/debug/tripwire parity, refmeta, callback roots, resurrection, physical
movement, A3c no-park, stage/performance, fixed point and broad five-GC parity
remain unproved.  The investigation and task remain `active` / `IN_PROGRESS`.

## Update — 2026-08-22 A3b GC4 raw-payload preallocation tail

### RESOLVED sub-boundary

For one stable Backend 4 selection, threads enabled, default `ATOMIC`
refcounts and valid managed source objects, public relocation copy now
snapshots the six type-specific raw payload families under a short graph-lock
scope, allocates every raw buffer and required ZPage span node after unlock,
allocates the pinned target after unlock, then revalidates and publishes the
preallocated transaction under one final graph lock in both the C transition
oracle and strict freestanding pcc-Python production runtime.  Final raw/span
publish helpers contain no allocation or free, and structural cleanup plus a
failed target decref run after unlock.

The covered families are continuation chunk/slots, exception traceback,
class bases/MRO/methods/field names, dict indices/entries, set entries and list
items.  Final validation re-snapshots pointers, sizes and scalar metadata and
preflights aggregate target-page span capacity.  Validation failure retains
plan ownership; an unexpected partial span-publication failure transfers all
buffers to a dealloc-safe target while leaving only unlinked span nodes for
plan cleanup.  Both mirrors fail closed on undersized raw object layouts before
their first type-specific field read, and strict also reloads the current tag
before interpreting the saved tag.

Local adversarial review found and corrected three real gaps: the initial
exception/class/continuation/list undersized target path, dict/set field reads
that preceded their layout guards, and strict saved-tag trust across the
unlocked planning window.  The final combined source/archive packet passes
14/14 and the combined C/strict six-family behavior plus slot-retain packet
passes 14/14.  The final strict dict cold node passes in 137.71 seconds against
cache key `477af77692f4dd15ab52a1d4-threaded-pcc-py`; its provenance has 186/186
pcc-Python members, zero host-cc members and 444 C-API symbols.  Exact REDs,
source and archive hashes, commands, log receipt and rollback nonclaims are in
`docs/goal/evidence/2026-08-22-gc4-a3b-relocation-raw-payload-preallocation.md`.
The review was local under the user's single-Agent preference; no independent
sub-agent verdict is claimed.

This closes raw-buffer and span-node allocation/free inside public GC4 copy's
own graph-lock scope, not raw byte-copy or span-list publication.  Raw mutators
do not yet participate in phase/no-park admission, selected-source/page
lifetime remains unprotected across unlocked planning windows, and the private
strict commit helper retains its internal graph-lock precondition.  GC3's
compatibility wrapper still prepares/finishes the plan in its generational
holder.  Forwarding/identity/ZPage commit, remap/retirement, remembered-root
admission, nested callers, concurrent drains, ABA, remaining in-lock loops and
diagnostics, CMS boundaries, unlocked decref, callback/C-API raw leases,
target-death cleanup, resurrection, physical movement, A3c, stage/performance,
fixed point and broad five-GC parity remain unproved.  The investigation and
task remain `active` / `IN_PROGRESS`.

## Update — 2026-08-23 A3b public-copy forwarding-plan proposal

### ACTIVE sub-boundary

The next final-commit audit confirms that the public GC4 relocation path still
calls the shared `pcc_gc_install_forwarding_unlocked` transaction under the GC
graph lock.  On its new-edge path that helper may allocate two stable-identity
nodes and one forwarding node; insertion into the identity, forwarding-source
and forwarding-target open-addressed indexes may allocate a replacement table,
rehash the old table and free it.  Failure rollback can also decref the target
and free the forwarding node while still locked.  The strict mirror has the
same ownership and ordering.

The selected proposal is deliberately public-copy-only.  A forwarding install
plan will snapshot the three index capacity requirements under a short lock,
then allocate two identity nodes, one forwarding node and any replacement index
storage after unlock.  The final locked copy commit will revalidate, install
preallocated capacity without freeing replaced tables, and publish the stable
identity plus forwarding edge through explicitly no-allocation insert helpers.
All unused nodes, unused prepared tables and replaced old tables will remain
owned by the plan and be freed after the final graph unlock.  A failure before
edge publication must leave no partial forwarding edge.

This proposal does not replace the shared historical installer used by direct
forwarding and GC3 oldification.  Those callers keep their current ABI and
locking until a separate compatibility and failure-ownership proof exists.  It
also does not move raw byte copy, ZPage structural loops or tripwires out of the
lock, and it does not establish source/page lifetime or mutator quiescence
across the unlocked planning window.

## Claim Boundary

Closing this investigation will prove Backend 4 relocation-phase mutator
quiescence only.  It will not by itself prove source ownership release, a
concurrent/low-pause collector claim, stage2 performance, granule S2 acceptance,
the five-GC matrix, or the self-hosted fixed point.

## Update — 2026-08-23 A3b relocation source-ZPage detach tail

### ACTIVE sub-boundary

The post-recovery call-chain audit found one remaining direct allocation-owner
edge below the final public relocation-copy commit in both mirrors.  The commit
calls source ZPage removal while holding the graph lock.  That removal walks and
`free`s every source payload-span node, then releases the detached owner node;
the owner-node release itself may also call `free` when its pool is full.  The
strict cross-object copy helper additionally accepts a NULL finish plan and
falls back to freeing detached relocation/page nodes inside the same locked
body.  The preceding raw-payload-preallocation evidence proved that target
span publication consumes preallocated nodes, but its broader statement that
the complete copy commit frees no span node under lock was therefore too broad.

The smallest next proposal is limited to source-ZPage structural detach.  The
locked transaction will unlink the source owner, update span/page accounting,
and place the still-owned detached ZPage node in the mandatory copy finish
plan.  Only after graph unlock will a ZPage-lifecycle owner free the detached
payload-span chain and owner node.  The private strict helper will reject a
NULL finish plan, removing its locked fallback frees.  A source/ABI/order
regression must fail on the current direct-remove shape before implementation;
the existing multi-span class case must remain green in the C transition oracle
and strict pcc-Python runtime after the change.

This slice deliberately does not address the next audited edge:
`pcc_gc_install_forwarding_unlocked` still allocates identity and forwarding
nodes, may allocate/rehash/free three pointer-index tables, and contains cleanup
decrefs while the graph lock is held.  Forwarding/identity index preparation is
a separate proposal after the ZPage detach tail is green.  Raw byte copying
also remains locked; no raw-mutator quiescence claim follows.

## Update — 2026-08-23 A3b relocation source-ZPage detach tail confirmed

### RESOLVED sub-boundary

For one stable Backend 4 selection and a valid managed relocation candidate,
the final public relocation-copy commit in both the C transition oracle and the
strict freestanding pcc-Python runtime now performs only the source ZPage's
structural unlink, index/accounting updates and page-state transition while the
GC graph lock is held.  It transfers the detached source-owner node into a
mandatory 24-byte finish plan.  After graph unlock, the ZPage lifecycle owner
frees that node's detached payload-span chain and the owner node itself.  The
strict and C private commit helpers both reject a missing finish plan, so the
former NULL-plan fallback frees no longer exist.

The source/ABI/order regression was first RED with a missing cross-object ABI
entry (`KeyError`, one failure in 0.14 seconds).  The final source test is green,
the combined copy plus ZPage source/LLVM/self closure and production-owner
packet passes 17/17, all fourteen type-specific raw-payload cases pass in both
runtime roots, the cold strict multi-span class case passes, and the task's
payload plus forwarding-retirement pair passes 14/14.  Threaded and threads-off
C syntax checks add no warning beyond the same five pre-existing unused static
helpers.  During strict-closure validation, the ownership manifest was also
repaired for seven selector globals already referenced by the pre-existing HEAD
source; that test-only drift was not introduced by this runtime change.  Exact
commands, hashes and log receipts are in
`docs/goal/evidence/2026-08-23-gc4-a3b-relocation-source-zpage-detach.md`.

This corrects the broader wording in the preceding raw-payload evidence: target
payload-span publication consumed preallocated nodes, but source ZPage removal
could still free its already-linked payload-span nodes under the final lock.
That remaining source-owner free path is now closed.  No broader graph-lock or
mutator-quiescence claim follows.  In particular,
`pcc_gc_install_forwarding_unlocked` still allocates identity/forwarding nodes,
may grow and free index tables, and can cleanup-decref on failure under the
same commit lock.  ZPage detach still contains bounded structural/accounting
loops and tripwires; raw byte copy remains locked.  Source/page lifetime across
unlocked planning, ABA, remap/retirement, target death, GC3 compatibility,
phase/no-park admission, callbacks, raw leases, resurrection, performance,
fixed point and broad five-GC parity remain open.  The investigation and parent
task remain `active` / `IN_PROGRESS`.

## Update — 2026-08-23 A3b public-copy forwarding plan confirmed

### RESOLVED sub-boundary

For one stable Backend 4 selection and a valid managed relocation candidate,
the public relocation-copy path in both the C transition oracle and strict
freestanding pcc-Python runtime now prepares two stable-identity nodes, one
forwarding node and any required identity/source/target index storage outside
the final graph-lock tenure.  The locked commit revalidates and publishes via
preallocated capacity plus no-allocation inserts.  Replaced old tables and
unused plan members are freed only after graph unlock.  The private commit and
index helpers contain no malloc/calloc/free or cleanup decref.  Direct
forwarding and GC3 oldification retain the old shared installer.

Review caught and corrected a strict-only global-width error before final
evidence: the new stable-ID path initially accessed the strict i32
`pcc_gc_next_object_id` global as i64.  It now uses the established i32 width,
and final pinned-source revalidation preserves the old pin-reject telemetry in
both mirrors.  The final source/closure/archive-owner packet passes 20/20, the
C/strict payload plus retirement packet passes 28/28, and the remaining
fragmentation/stable-ID/GC3 compatibility packet passes 7/7.  Exact hashes,
RED chronology, timeout disposition and logs are recorded in
`docs/goal/evidence/2026-08-23-gc4-a3b-relocation-forwarding-plan.md`.

One legacy production stress node remains red identically in current source
and an isolated `HEAD` archive: after round zero it holds
`relocation_set=8/forwardings=56`, while every later round consumes 64 aging
work items and never enters idle remap.  Old-installer, old-ZPage-release and
combined substitutions did not change that result.  Failure-only telemetry is
now durable in the test.  This is the already-open stale-candidate/fairness
boundary, not a forwarding-plan regression, and no green claim is made for
that node.

This sub-boundary removes forwarding/identity allocation, old-table freeing
and cleanup decref from public copy's final graph-lock tenure.  It does not
make index rehash wait-free, establish safety for lock-free index readers,
move raw byte copying out of the lock, protect source/page lifetime across the
unlocked window, or close allocator-failure/rollback, nested/concurrent drain,
ABA, backend-switch, remap/retirement, remembered-root, target-death, GC3
holder, callback/raw-lease, resurrection, physical-movement, A3c,
stage/performance, fixed-point or broad-parity work.  The investigation and
parent task remain `active` / `IN_PROGRESS`.

## Update — 2026-08-23 A3b retained-page release finish proposal

### ACTIVE sub-boundary

The final remap/retirement call-chain audit found a smaller first owner than
payload retirement itself.  Every C and strict caller invokes
`pcc_gc_backend4_remap_and_retire_unlocked` while holding the GC graph lock.
At the beginning of that helper, the oldest retained ZPage generation is
partitioned; pages whose object, pending-allocation and pending-forwarding
counts are all zero immediately `free` their physical span and descriptor
under that lock.  The following parked-page drain only moves the preceding
generation into retained quarantine and does not physically release it.

The selected proposal is limited to that oldest-generation release.  The
locked partition will detach eligible pages into a caller-owned list and keep
invariant-violating pages quarantined.  The remap ABI will return the detached
list.  Each of the object drain, page drain and idle-step callers will preserve
its existing remap lock tenure, unlock, then pass the list to one finish owner
that clears/frees the span and page.  The strict and C paths must preserve the
same two-epoch order: old retained generation detaches first, parked pages
enter retained second, and no page from the current remap can reach the finish
list.

A genuine source/ABI/order regression must fail on the current in-lock
`free(span)`/`free(page)` shape before implementation.  It must cover all three
outer caller families in both mirrors and keep the existing three-epoch
C/strict differential green.  This proposal does not move forwarding-node
free, target decref, identity/object-node retirement or
`pcc_gc_relocation_retire_source_payload` allocation/free/decref out of the
lock.  Remap/root loops, stale-candidate fairness, lock-free index-reader
safety, raw mutator quiescence, source lifetime and all broader A3c/task claims
remain separate.

## Update — 2026-08-23 A3b retained-page release finish confirmed

### RESOLVED sub-boundary

For Backend 4's existing two-remap-epoch quarantine, the C transition oracle
and strict freestanding pcc-Python runtime now partition and detach eligible
oldest-generation retained ZPages under the GC graph lock, return those pages
from remap, and physically free their backing spans and descriptors only after
the object-drain, page-drain or idle-step caller releases that lock. Pages with
live objects, pending allocations or pending forwardings stay quarantined. The
old retained generation is still processed before parked pages enter
retention, so the current remap cannot release a newly parked page.

The source/ABI/order test was genuinely RED on the former locked `free` shape.
After implementation, all six LLVM/self strict closures pass, the directly
affected source/closure/archive/differential packet passes 24/24, raw-payload
and relocation-payload neighbors pass 21/21, and fragmentation/stable-ID/GC3
compatibility passes 7/7. A first combined behavior command timed out without
a summary and was discarded; process inspection found no leftover compiler or
pytest children, and complete sharded reruns replaced it. Exact commands,
hashes and logs are recorded in
`docs/goal/evidence/2026-08-23-gc4-a3b-retained-page-release-finish.md`.

Strict closure validation caught a missing `pcc.unsafe.null` import in the new
drain local initialization before final evidence. The resulting
`py_module_attr_get` escape now fails no gate; no fallback was introduced.

This closes retained-page physical release only. Forwarding-node free, target
decref, identity/object-node retirement and
`pcc_gc_relocation_retire_source_payload` remain under remap's graph lock and
are the next audited owner family. The stale-candidate/fairness failure,
remap/root loop tenure, index-reader safety, raw copy and mutator admission,
source lifetime, concurrency/ABA/backend switching, callbacks/raw leases,
resurrection, physical movement, A3c, broad parity, performance and fixed
point remain open. The investigation and parent task remain `active` /
`IN_PROGRESS`.

## Update — 2026-08-23 A3b normal-remap forwarding-edge finish proposal

### ACTIVE sub-boundary

The next owner audit separated normal two-epoch remap from target-death
cleanup. In normal remap, `pcc_gc_forwarding_remove` unlinks a source edge,
immediately decrefs its retained target and frees the forwarding node while the
GC graph lock is held. That decref may enter ordinary object deallocation and
attempt the graph lock again. The target-death route has different semantics:
it first removes the reverse target index and must make self-referential source
edges non-resolving before any saved ownership token is released. It is not
safe to combine that route into this first finish move.

The selected proposal is normal-remap-only. Each object drain, page drain and
idle step will prepare a fixed-size remap-finish plan on its caller stack before
locking. Normal remap will detach the forwarding source/target indexes and
main-list edge, decrement population, update the source page's pending count,
and chain the still-owned forwarding node into that plan without decref or
free. After graph unlock, one finish owner will decref the retained target and
free the detached node. The legacy forwarding-remove ABI will compose the same
detach and finish operations immediately for its existing non-remap callers,
preserving their current behavior.

A genuine source/ABI/order regression must fail on the current direct
`py_decref(dead->to)` / `free(dead)` remap path, cover all three outer callers
in both mirrors, and prove the finish-plan storage is initialized before graph
lock acquisition. Existing one-epoch forwarding-shell retirement,
two-epoch page quarantine and C/strict three-remap behavior must remain green.

This proposal does not move source-payload retirement, identity removal,
granule/exact provenance retirement, object-node release, or target-death
cleanup. In particular, the self-reference constraint and side-table/token
ordering in
`docs/investigations/gc-backend4-forwarded-source-payload-retirement.md`
remain authoritative for the later target-death/payload slice.

## Update — 2026-08-23 A3b normal-remap forwarding-edge finish confirmed

### RESOLVED sub-boundary

Normal two-epoch Backend 4 remap in the C transition oracle and strict
freestanding pcc-Python runtime now detaches forwarding edges under the GC
graph lock and chains the still-owned nodes into a 16-byte caller-stack finish
plan. Source/reverse indexes, the main list, population and source-page pending
state are updated before unlock. Only after the object-drain, page-drain or
idle-step caller unlocks does one finish owner decref each retained target and
free its detached forwarding node. The node itself preserves the target
ownership across the handoff.

The source/ABI/order test was genuinely RED on the former direct
`pcc_gc_forwarding_remove(old)` path. Final LLVM/self closures pass 6/6, the
directly affected source/closure/archive/three-epoch differential packet passes
25/25, raw-payload and payload-ownership neighbors pass 21/21, and the
fragmentation/stable-ID/C+strict target-phase-reset/GC3 compatibility packet
passes 9/9. Exact commands, frozen hashes, the discarded undersized-watchdog
run and replacement summaries are recorded in
`docs/goal/evidence/2026-08-23-gc4-a3b-normal-remap-forwarding-edge-finish.md`.

The legacy remove ABI still composes detach and finish immediately for its
existing non-remap callers. Target-death cleanup was intentionally not folded
into this proof: its reverse-index and self-reference ordering differs, as
recorded in the forwarded-source-payload investigation.

Source-payload allocation/free/decref, identity and object-node retirement,
target-death cleanup, the stale-candidate/fairness failure, remap/root loop
tenure, raw copy and mutator admission, source lifetime, concurrent/nested
drains, ABA/backend switching, callbacks/raw leases, resurrection, physical
movement, A3c, broad parity, performance and fixed point remain open. The
investigation and parent task remain `active` / `IN_PROGRESS`.

## Update — 2026-08-23 A3b reseed relocation aggregate bounded

### RESOLVED aggregate-holder sub-boundary; page commit remains open

C and strict pcc-Python reseed now compute candidate count and small/medium/
total byte aggregates in batches of at most 16 relocation nodes per graph-lock
tenure. An unlink-aware cursor and relocation-list revision restart all six
aggregates after selection, relocation-copy, object-free or reset mutation.
The completed stable aggregate enters the existing page phase without an
intervening unlock.

A deterministic 24-candidate pthread probe pauses after aggregate node 16,
performs concurrent full reset/unlink/recycle, resumes without UAF, observes
zero and recovers all 24 candidates. The final C/strict packet passes 13/13.
Exact chronology, hashes and logs are in
`docs/goal/evidence/2026-08-23-gc4-a3b-reseed-bounded-relocation-aggregate.md`.

This closes the relocation aggregate only. Page rebuild/commit remains an
unbounded nested page/relocation scan, and carrying a page pointer across
unlock requires an explicit lifetime mechanism. GC3/callback/log holders,
A3c, raw transactions and collector-owned STW remain open. The investigation
and parent task remain `active` / `IN_PROGRESS`.

## Update — 2026-08-23 A3b normal-remap source-metadata finish proposal

### ACTIVE sub-boundary

The next normal-remap audit found two physical node owners below
`pcc_gc_retire_forwarded_source_unlocked`. Identity removal unlinks and frees a
stable-identity node; object retirement removes exact/granule and object-index
provenance, unlinks the object node, then sends it to a graph-lock-protected
pool whose saturated path calls `free`. Both operations currently run under
the remap graph lock in C and strict pcc-Python.

The selected proposal leaves every semantic invalidation under lock: identity
and object indexes, granule/exact managed provenance, live-byte accounting and
object-list unlink. It splits identity removal into detach plus owner finish,
chains the detached identity and object nodes into an expanded 32-byte
caller-stack remap-finish plan, and physically frees those nodes only after the
outer caller unlocks. Normal remap passes its shared finish plan. The
target-death route passes NULL and retains immediate node finish under its
existing object-freeing lock, so this slice does not alter its reverse-index or
self-reference transaction.

A genuine source/ABI/order regression must fail on the current direct identity
remove/object-node release shape. It must pin the four-field plan layout and
prove all outer callers allocate and zero the complete plan before locking,
normal remap chains both metadata owners without `free`, and finish invokes the
identity/object-node owners only after unlock. Existing identity behavior,
three-remap C/strict differential, target-phase-reset and GC3 compatibility
must remain green.

This proposal does not move or redesign
`pcc_gc_relocation_retire_source_payload`: its record/context/side-table
allocation, raw-storage frees and saved-reference decrefs remain under lock.
It also does not claim that structural index/granule loops are wait-free, or
change target-death, resurrection or stale-candidate/fairness behavior.

## Update — 2026-08-23 A3b normal-remap source-metadata finish confirmed

### RESOLVED sub-boundary

Normal Backend 4 remap in both runtime roots now detaches stable-identity and
object nodes under the GC graph lock, after performing identity/object/managed
pointer index invalidation, granule/exact provenance retirement, live-byte
accounting and list unlink there. The caller-stack remap-finish plan is 32
bytes, with page, forwarding, identity and object-node chains at offsets
0/8/16/24. After the outer caller unlocks, the identity and object-node owners
physically free their detached nodes.

The source/ABI/order test was genuinely RED on normal remap's direct
`_retire_forwarded_source(old)` composition. Final strict closures pass 10/10,
the five-owner source/closure/archive/three-epoch differential packet passes
36/36, raw-payload and payload-ownership neighbors pass 21/21, and the
fragmentation/stable-ID/C+strict target-phase-reset/GC3 compatibility packet
passes 9/9. Exact commands and frozen receipts are recorded in
`docs/goal/evidence/2026-08-23-gc4-a3b-normal-remap-metadata-finish.md`.

The target-death wrapper uses the same detach transition but a local finish
plan that it consumes immediately under its existing object-freeing graph
lock. Its reverse-index and self-reference timing therefore remains unchanged
and unclaimed by the delayed normal-remap proof.

Source-payload record/context/side-table allocation, raw-storage frees and
saved-reference decrefs remain under the normal remap lock. Target-death
payload cleanup, structural index-loop tenure, stale-candidate fairness, raw
copy/mutator admission, source lifetime, concurrent/nested drains,
ABA/backend switching, callbacks/raw leases, resurrection, physical movement,
A3c, broad parity, performance and fixed point remain open. The investigation
and parent task remain `active` / `IN_PROGRESS`.

## Update — 2026-08-23 A3b normal-remap payload-finish proposal

### ACTIVE sub-boundary

The requested pre-lock preparation audit found that moving the current
`pcc_gc_relocation_retire_source_payload` preparation out of the graph lock is
not yet a sound source change. All three normal-remap callers enter the graph
lock without first owning a stopped-world epoch. The slot visitor heals raw
source slots, and `pcc_gc_backend4_source_side_table_plan_prepare` traverses
the graph-lock-owned medium-state and heap store-buffer lists without taking
its own lock. A pre-lock validation pass would therefore race mutator stores
and graph-table changes; a graph-lock snapshot followed by unlock/prepare/
relock would additionally need a proven source/edge pin plus ABA exclusion.
Those are still explicit open boundaries of this investigation. This audit
denies treating the current source shell's two-epoch page retention as either
mutator quiescence or side-table synchronization.

The next finite safe slice isolates the already-valid locked commit from its
reentrant finish. Payload retirement will allocate and validate its record,
context and source-side-table storage and then detach owned slots, raw payloads
and owner side tables under the existing graph lock. It will chain the now
detached plan into an expanded 40-byte caller-stack remap-finish plan rather
than freeing raw bases, releasing side-table tokens, decrefing saved owned
slots or freeing plan storage there. After each object-drain, page-drain or
idle-step caller unlocks, the payload owner will consume that chain before the
forwarding target ownership is released. Keeping this finish first preserves
the existing ownership order while ensuring decref reentry observes a fully
inert, unindexed source and a non-resolving forwarding edge.

The target-death path remains separate. Its existing public payload-retirement
ABI will compose detach and finish immediately under the object-freeing graph
lock, preserving the historical reverse-index/self-reference timing instead
of claiming the normal-remap handoff is safe there. A later target-death slice
must first prove that an OWNED source self-reference cannot be released while
its edge still resolves to the dying target.

A genuine RED source/ABI/order regression must fail on the current locked
`free(raw*)`, source-side-table finish and saved-slot `py_decref` loops; pin the
new fifth finish-plan field in C and strict pcc-Python; prove normal remap
chains payload plans without those releases; and prove every outer caller
unlocks before the one finish owner consumes them. Existing failure-before-
mutation validation, direct target-death composition, three-remap behavior,
payload ownership, target phase reset and GC3 compatibility remain required
neighbors.

This proposal does not claim pre-lock payload preparation. That move remains
blocked on the parent phase's stopped-world/raw-access/source-lifetime/ABA
contract and must not be inferred from moving finish work out of the lock.

## Update — 2026-08-23 A3b normal-remap payload finish confirmed

### RESOLVED sub-boundary

Normal Backend 4 remap in both runtime roots now performs payload slot/raw
storage detachment and source side-table commit under the graph lock, chains
the fully detached ownership into the fifth field of a 40-byte caller-stack
finish plan, and frees raw bases, releases side-table tokens, decrefs saved
owned slots and frees plan storage only after the outer caller unlocks. Payload
finish precedes forwarding-target decref, preserving the established release
order while making the source inert and its edge non-resolving before any
reentrant cleanup.

The source/ABI/order test was genuinely RED on normal remap's former immediate
public payload-retirement call. The task-card payload/forwarding packet passes
18/18, all C/strict type-specific raw-payload cases pass 14/14, and
fragmentation/stable-ID/C+strict target-phase-reset/GC3 compatibility passes
9/9. Exact commands, frozen source hashes and log receipts are recorded in
`docs/goal/evidence/2026-08-23-gc4-a3b-normal-remap-payload-finish.md`.

The pre-lock preparation portion of the audit was denied as unsafe on the
current phase: callers do not yet own STW before locking, the slot visitor
heals raw source slots and source-side-table prepare traverses graph-owned
lists. Preparation therefore remains locked until stopped-world/raw-access,
source-lifetime and ABA-safe revalidation are proven. The target-death public
ABI still composes detach and finish immediately under its existing lock;
sharing delayed finish still requires the historical OWNED self-reference/
non-resolving-edge proof.

One adjacent shared-slot source-text test was confirmed baseline-red on HEAD
because it still searches for a removed `PyObject ***from_slots` typedef; it
was retained and is not counted as slice evidence. Structural table/index
tenure, stale-candidate fairness, raw copy/mutator and remembered-root
admission, allocator failure, lock-free readers, concurrent/nested drains,
ABA/backend switching, callbacks/raw leases, resurrection, physical movement,
A3c, broad parity, performance and fixed point remain open. The investigation
and parent task remain `active` / `IN_PROGRESS`.

## Update — 2026-08-23 A3b target-death payload/edge finish proposal

### ACTIVE sub-boundary

The target-death audit confirms that normal-remap finish cannot be reused by
ordering analogy. `pcc_gc_forwarding_remove_target` removes the reverse target
index, but calls immediate source-payload retirement while the source index and
main edge still resolve. An OWNED source self slot is therefore healed to the
target that is already in logical deallocation, and the immediate payload
finish can decref that zero/deallocating target before the edge becomes
non-resolving. The current path then frees the forwarding node without its
ordinary target decref, so the intended target-death ownership disposition is
already distinct from normal edge retirement.

The selected transaction uses one caller-owned finish plan prepared by
`pcc_gc_note_object_freeing` before it acquires the graph lock. While locked,
target-death payload preparation may heal through the still-live edge, detach
source slots/raw storage/side tables, then unlink the source index and main
edge, retire source metadata and chain the forwarding node into a dedicated
dead-target list. Only after all target bookkeeping is complete and the caller
unlocks may finish free raw storage, release non-target side-table/source-slot
tokens and physical metadata nodes. Saved tokens equal to the dying target are
already represented by the target's logical zero/deallocation state and must
be discarded, not decrefed again. Dead-target forwarding nodes are freed
without the ordinary forwarding-target decref.

A genuine dynamic RED will use the public relocation/forwarding and
`pcc_gc_note_object_freeing` boundaries in both the C oracle and strict
pcc-Python archive. A non-self source child must lose exactly one source-owned
reference. An OWNED source self slot must leave the dying target at refcount
zero, remove the forwarding edge and inert the source payload without abort,
underflow or duplicate teardown. A source/ABI/order contract must additionally
prove that every return after target cleanup unlocks before consuming the
caller plan.

This is still an A3b graph-lock reentry/lifetime slice, not full mutator
quiescence. The caller does not yet own STW, so the proof does not establish
that arbitrary concurrent raw access cannot overlap target death. Connecting
the collector phase and raw-access admission remains mandatory before the
parent task can close.

## Update — 2026-08-23 A3b target-death payload/edge finish confirmed

### RESOLVED sub-boundary

Target-death cleanup in both runtime roots now uses one 48-byte caller finish
plan. While graph-locked it removes the reverse target index, detaches source
payload ownership and side tables, removes the source index/main edge, retires
identity/object metadata and chains the dead-target forwarding node. After
`pcc_gc_note_object_freeing` unlocks, one finish owner frees raw payloads and
physical nodes and releases non-target saved ownership. Tokens equal to the
already dying target are discarded rather than decrefed, and the dead-target
forwarding node does not perform the ordinary target decref.

The requested dynamic RED was denied honestly: before implementation, both
default runtime roots already returned the requested self/control output,
because their zero/deallocating-target behavior did not expose the unsafe
ordering as an observable underflow. The source/ABI/order contract was genuine
RED on the old one-argument/immediate-finish shape and is now green. The final
task-card packet passes 21/21, all C/strict type-specific raw payloads pass
14/14, and fragmentation/stable-ID/C+strict target-phase-reset/GC3
compatibility passes 9/9. Exact chronology, hashes and log receipts are in
`docs/goal/evidence/2026-08-23-gc4-a3b-target-death-finish.md`.

This result does not establish raw-mutator quiescence or stopped-world
ownership. The distinct outgoing-source `pcc_gc_forwarding_remove(o)` path
still decrefs a live target under the object-freeing graph lock and requires
its own audit before sharing a finish chain. Raw list/dict/set access,
source/page lifetime and ABA, remembered-root admission, nested/concurrent
drains, callbacks/raw leases, resurrection, physical movement, A3c, broad
parity, performance and fixed point remain open. The investigation and parent
task remain `active` / `IN_PROGRESS`.

## Update — 2026-08-23 A3b source-death live-target finish proposal

### ACTIVE sub-boundary

The outgoing-source audit distinguishes this path from target death. Existing
`pcc_gc_forwarding_detach` already removes the source index, reverse target
index and main edge before it releases target ownership, so the target cannot
re-enter through a still-resolving edge. The remaining issue is physical and
reentrant: `pcc_gc_forwarding_remove(o)` immediately decrefs the still-live
target and frees the forwarding node while `pcc_gc_note_object_freeing` retains
the GC graph lock. A last forwarding-owned target can therefore enter its full
deallocator at recursive graph-lock depth inside the source's transaction.

The selected narrow change adds one forwarding-owner helper that detaches the
ordinary edge under lock and chains its node into the existing finish field at
offset 8. The node pins its live target across unlock. Generic finish then
performs the ordinary target decref and node free after unlock. This deliberately
does not use the dead-target field at offset 40: source death still owes the
ordinary target reference, while target death must suppress it.

A source/ABI/order regression must be genuinely RED on the current immediate
`pcc_gc_forwarding_remove(o)` call and prove detach/chaining precede unlock and
target decref follows it. A C/strict dynamic differential will cover both a
non-last target control and a target whose only remaining reference is the
forwarding edge; the latter must run target/list cleanup, release its child
exactly once and leave no forwarding entry. If the current immediate path has
the same terminal output, that is behavior-preservation evidence rather than a
fabricated dynamic RED.

This slice removes one graph-lock reentry source only. It does not establish a
stopped-world epoch, raw-access exclusion, source/page pinning, ABA safety or
the parent task's true-pthread mutator-quiescence claim.

## Update — 2026-08-23 A3b source-death live-target finish confirmed

### RESOLVED sub-boundary

Outgoing-source cleanup in both runtime roots now detaches its source index,
reverse target index and main edge under the graph lock, then chains the
ordinary forwarding node into finish offset 8. The node pins the live target
until `pcc_gc_note_object_freeing` unlocks; generic finish then performs the
ordinary target decref and node free. Target-death nodes remain separately
owned at offset 40 and suppress target decref.

The source/ABI/order test was genuinely RED on the former immediate public
remove composition. The last-owner/control dynamic differential was baseline
green and is retained as behavior-preservation evidence, not mislabeled RED.
The final task-card packet passes 24/24, all C/strict type-specific raw payloads
pass 14/14 and fragmentation/stable-ID/C+strict target-phase-reset/GC3
compatibility passes 9/9. Exact chronology, source hashes and log receipts are
in `docs/goal/evidence/2026-08-23-gc4-a3b-source-death-target-finish.md`.

This completes the currently identified forwarding cleanup decref/free tails,
not graph-lock or mutator quiescence. Before A3c, the remaining graph-lock
holder inventory must be checked against the A3b bounded-region criteria.
Only then may outer graph-lock ownership connect to A1 no-park depth, with
outer unlock before no-park exit. Complete raw container transactions and the
collector-owned STW phase remain mandatory. The investigation and parent task
remain `active` / `IN_PROGRESS`.

## Update — 2026-08-23 A3b holder inventory and registry tails

### RESOLVED sub-boundary; A3c blocked by confirmed inventory

The pre-A3c C/strict source inventory found remaining graph-lock holders that
still allocate/free, safepoint, invoke callbacks or perform unbounded cleanup.
Concrete blockers include ZPage metadata/span allocation, frame-index rehash,
relocation-set reset retirement, Backend 3 promotion/remembered-owner work,
C extension-root visitation and caller-provided runtime-root visitors. The
graph lock is therefore deliberately not connected to A1 no-park yet.

One bounded subset is focused green. Frame nodes are now prepared before lock
acquisition and released after unlock in both runtime roots. Continuation-root
unregister detaches under lock and frees after unlock. The C transition
thread-exit path similarly detaches its Backend 4 medium-buffer TLS state under
lock and frees it after unlock. The source contracts were genuine RED on the
former ordering. The complete C/strict registry packet passes 12/12, the final
source pthread frame node passes, and the existing partial CMS unregister
probe passes. Exact hashes, RED chronology, logs and nonclaims are recorded in
`docs/goal/evidence/2026-08-23-gc4-a3b-holder-inventory-registry-tails.md`.

This does not close frame-index growth, ZPage allocation, relocation reset,
GC3 promotion, callback roots, tripwire/log paths, full bounded-scan inventory,
A3c, raw-access transactions or collector-owned STW. The next finite slice is
frame-index capacity preparation/retirement outside graph-lock ownership in
both runtime roots. The investigation and parent task remain `active` /
`IN_PROGRESS`.

## Update — 2026-08-23 A3b frame-index capacity plan confirmed

### RESOLVED sub-boundary

C and strict pcc-Python frame entry now prepare any frame-index table growth
outside graph-lock ownership, revalidate and commit a still-sufficient capacity
plan under the lock, and retire unused or replaced storage after unlock.  The
new preallocated replacement primitive cannot allocate; frame leave uses it for
duplicate restoration and stale-index repair.  The allocation-failure path
releases its prepared frame node only after unlock.

The source/ABI contract was genuinely RED on the former allocation-capable
frame-entry path.  Strict exact raw closure subsequently found and forced fixes
for a scanner-invisible multiline extern and obsolete unused imports; the
closure was not widened.  The final C/strict index/frame packet passes 11/11,
including GC0 through GC4 behavior, duplicate-frame and allocation-failure
coverage and true pthread entry.  Exact chronology, hashes and the durable log
are in `docs/goal/evidence/2026-08-23-gc4-a3b-frame-index-plan.md`.

This removes one audited holder only.  A3c remains blocked by ZPage allocation,
relocation reset, GC3 promotion/remembered-owner safepoints and decrefs,
extension/caller root callbacks, tripwire/log paths and the remaining bounded-
scan review.  The next finite slice is C/strict ZPage metadata and backing-span
allocation outside the graph lock, preserving active/free/reusable races and
allocation failure.  The investigation and parent task remain `active` /
`IN_PROGRESS`.

## Update — 2026-08-23 A3b raw ZPage allocation preparation confirmed

### RESOLVED sub-boundary; registration allocation remains open

C and strict pcc-Python `pcc_gc_backend4_try_zpage_alloc` now detach or create a
private page, unlock before page metadata/backing-span allocation and clearing,
then reacquire the graph lock and revalidate before publication.  A prepared
free page that loses the active-page race is restored to the free list; a
never-published fresh page is released after unlock.  The selected object range
is reserved with `pending_alloc_count` under lock and zero-filled after unlock,
so it cannot be selected or recycled during constructor handoff.  Failed span
allocation leaves no partial page or page/capacity/free metric change.

The source-order contract was genuinely RED on the old graph-locked allocator.
The final 13/13 packet covers strict LLVM/self closure, all page classes,
allocation failure, a 16-way true-pthread cold-page race in threaded C/strict
archives and free/reuse lifecycle neighbors.  An earlier pthread attempt was
invalid because it linked nonthreaded archives; LLDB localized its concurrent
object-index corruption, and that result is retained only as harness-negative
evidence.  Exact chronology and hashes are in
`docs/goal/evidence/2026-08-23-gc4-a3b-zpage-allocation-preparation.md`.

This closes only the raw allocation entry.  The follow-up source audit found
that C object registration still allocates its object node, can grow the object
index and calls ZPage tracking that may allocate a ZPage node/page/span under
the outer graph lock.  Strict already prepares the object node outside but
retains the object-index and ZPage-tracking allocation edges.  Those resources
are the next finite A3b capacity-plan slice; relocation reset follows only after
they commit allocation-free.  A3c and all later quiescence claims remain open.

## Update — 2026-08-23 A3b object-registration node/index plan confirmed

### RESOLVED sub-boundary; ZPage tracking remains open

C and strict pcc-Python object registration now prepare an object node and any
required object-index capacity outside graph-lock ownership, reacquire and
revalidate, and commit through allocation-free node/index primitives.  Losing
race preparations and replaced index tables are released only after unlock.
The graph-leaf path preserves its flag transition without allocating these
resources.

The source-order contract was genuinely RED on the former allocation-capable
registration body.  The final index/node packet passes 11/11, production
archive GC0..GC4 tracking parity passes, and a 16-way true-pthread cold race in
threaded C/strict archives passes.  Exact chronology, hashes and log receipts
are in
`docs/goal/evidence/2026-08-23-gc4-a3b-object-registration-index-plan.md`.

This does not make the complete registration critical section allocation-free.
Backend 4 ZPage tracking still owns a node, owner-index growth and
malloc-backed fallback page/span allocation under the outer graph lock.  That
is the next finite plan/revalidation slice.  Relocation reset, GC3 promotion
and remembered-owner safepoints/decrefs, extension/caller root callbacks,
tripwire/log or unbounded holder paths, A3c, raw transactions and
collector-owned STW remain open.  The investigation and parent task remain
`active` / `IN_PROGRESS`.

## Update — 2026-08-23 A3b ZPage tracking plan confirmed

### RESOLVED sub-boundary

Backend 4 object registration in the C and strict pcc-Python roots now plans
its ZPage node, owner-index capacity and fallback metadata page/span outside
graph-lock ownership.  Locked commit revalidates the node pool, index load and
current page, then uses allocation-free node/index/link primitives.  Losing
free-page preparations return to the cache and unused fresh resources retire
after unlock.  Raw-page pending allocation handoff and malloc-backed fallback
admission remain intact; an impossible fallback span preparation leaves no
object-index or ZPage-owner-index entry.

The source/ABI/ordering contract was genuinely RED on the old tracking call.
The exact final packet passes 24/24, including C/strict fallback and failure
differentials plus a 16-way threaded cold race; production archive GC0..GC4
tracking parity also passes.  Exact chronology, hashes and log receipts are in
`docs/goal/evidence/2026-08-23-gc4-a3b-zpage-tracking-plan.md`.

This closes the identified object-registration allocation edges, not all A3b
holders.  Relocation-set/reset and evacuation-list retirement still consume
and free lists under the graph lock.  GC3 promotion/remembered-owner work,
extension/caller root callbacks, tripwire/log or unbounded holders, A3c, raw
container transactions and collector-owned STW remain open.  The investigation
and parent task remain `active` / `IN_PROGRESS`.

## Update — 2026-08-23 A3b relocation-reset physical finish confirmed

### RESOLVED physical-retirement sub-boundary; unbounded scans remain open

C and strict pcc-Python relocation reset now detach the relocation and
evacuation-page node chains under the graph lock, preserve candidate, target
and page-evacuation flag clearing plus counter reset under that lock, then free
the detached physical nodes after unlock.  Relocation-epoch reseed similarly
detaches the prior evacuation chain, rebuilds page membership and counters
under lock, and finishes the old nodes afterward.

The source/order test was genuinely RED on the former immediate-free shape.
The final focused packet passes 5/5, including C/strict phase-reset parity and
the reseed path.  Exact chronology, hashes and the durable log are in
`docs/goal/evidence/2026-08-23-gc4-a3b-relocation-reset-physical-finish.md`.

This does not move the relocation/object/page metadata scans outside the graph
lock.  Their nodes hold raw non-owning pointers, so doing so directly would
create a UAF/ABA path.  A lifetime-safe bounded-scan design and reseed node
preparation outside the lock are the next finite boundary.  Nested/concurrent
reset stress, allocation failure, GC3/callback/log holders, A3c, raw container
transactions and collector-owned STW remain open.  The investigation and
parent task remain `active` / `IN_PROGRESS`.

## Update — 2026-08-23 A3b reseed evacuation-node plan confirmed

### RESOLVED allocation sub-boundary; scan lifetime remains open

C and strict pcc-Python relocation-epoch reseed now count required evacuation
nodes under the graph lock, unlock to prepare a private list, reacquire and
revalidate, then rebuild membership through allocation-free commit.  A short
plan retries after concurrent growth; preparation failure returns before
detaching the old list.  Old and surplus nodes finish after unlock.

The source/order contract was genuinely RED on the former allocation-capable
page-add call.  The final source/C packet passes 4/4, and a non-empty two-page
reseed repeated twice against the strict production archive passes.  Exact
chronology, hashes and logs are in
`docs/goal/evidence/2026-08-23-gc4-a3b-reseed-node-plan.md`.

The relocation/object/page scans are still unbounded graph-lock holders and
cannot be moved directly because they dereference raw non-owning pointers.
Four true pthreads running reset/select against telemetry reseed are green in
both roots, and the source contract proves a short private plan finishes before
any detach.  A deterministically forced plan-growth window and allocator-fault
injection remain required before this holder is closed.  GC3/callback/log
holders, A3c, raw transactions and collector-owned STW also remain open.  The
investigation and parent task remain `active` / `IN_PROGRESS`.

## Update — 2026-08-23 A3b relocation-reset bounded scans confirmed

### RESOLVED reset-holder sub-boundary; reseed scans remain open

C and strict pcc-Python relocation reset now use one reset owner across unlock
boundaries and process relocation nodes, evacuation-page nodes and the object
registry in batches of at most 16 per graph-lock tenure. Detached physical
nodes finish after unlock; waits and inter-batch yields safepoint outside the
lock. Object unlink advances the owned reset cursor before recycling, and
candidate/forwarding admission fails closed during the epoch.

The source contract was genuinely RED on the missing owner. Strict closure
then correctly rejected the unregistered raw cursor; only its exact raw-global
ABI inventory was added, without weakening fail-closed validation. The final
C packet passes 7/7 and the strict packet passes 3/3. Both include a
four-thread 24-object run that exceeds the 16-node relocation/object batch.
Exact chronology, hashes and logs are in
`docs/goal/evidence/2026-08-23-gc4-a3b-reset-bounded-scans.md`.

This does not bound relocation-epoch reseed's count/commit scans or
deterministically prove its plan-growth and allocation-failure paths. Those
are next. GC3/callback/log holders, A3c, raw transactions, backend switching
and collector-owned STW remain open. The investigation and parent task remain
`active` / `IN_PROGRESS`.

## Update — 2026-08-23 A3b reseed forced plan paths confirmed

### RESOLVED evidence sub-boundary; reseed scans remain open

C and strict pcc-Python now share a default-inactive release/acquire diagnostic
seam that pauses relocation reseed after its locked count/unlock and before
private preparation, and limits private evacuation-node allocations. A true
pthread probe deterministically grows the plan from one to two pages in that
window; both roots revalidate and commit two pages. A zero-node budget leaves
the old two-page evacuation membership attached, and restoring the default
budget rebuilds two pages and 8320 bytes.

The source contract was genuinely RED on the missing probe. The final packet
passes 8/8, including prior concurrent and non-empty reseed neighbors. Exact
chronology, hashes and logs are in
`docs/goal/evidence/2026-08-23-gc4-a3b-reseed-forced-plan-paths.md`.

This proves the plan-growth and allocation-failure paths but does not bound
reseed's relocation/page count or commit walks. Those remain the next A3b
holder slice. GC3/callback/log holders, A3c, raw transactions and
collector-owned STW remain open. The investigation and parent task remain
`active` / `IN_PROGRESS`.

## Update — 2026-08-23 A3b reseed required-page count bounded

### RESOLVED count-holder sub-boundary; commit scans remain open

C and strict pcc-Python reseed now count authoritative evacuation nodes in
batches of at most 16 per graph-lock tenure instead of walking every page with
a nested relocation-list search. A serialized count owner, unlink-aware raw
cursor and graph-locked page-list revision make batch resumes restartable.
Waiters and non-final batches safepoint only after unlock.

A deterministic 24-page pthread probe pauses after the first batch, performs a
concurrent full reset/unlink/recycle, then resumes without UAF, observes the
empty set and recovers all 24 pages. The final packet passes 9/9 and C/strict
relocation phase parity passes 2/2. Exact chronology, hashes and logs are in
`docs/goal/evidence/2026-08-23-gc4-a3b-reseed-bounded-page-count.md`.

This closes required count only. Reseed's relocation aggregate and page
rebuild/commit scans are still unbounded, and any page pointer carried across
unlock still requires an explicit lifetime mechanism. GC3/callback/log
holders, A3c, raw transactions and collector-owned STW remain open. The
investigation and parent task remain `active` / `IN_PROGRESS`.

## Update — 2026-08-23 A3b reseed page commit bounded

### RESOLVED final reseed-holder sub-boundary; broader holder inventory remains open

C and strict pcc-Python relocation reseed now aggregate authoritative
evacuation pages in batches of at most 16 nodes per graph-lock tenure.
Unlink-aware cursors plus page/relocation revisions restart an invalidated
snapshot. The final page scan reads each page only while locked; no raw page
pointer crosses an unlock.

A commit owner blocks new candidate admission and Backend-4
relocation/forwarding commits between the stable relocation aggregate and page
publication. Full reset and object freeing may still proceed, update revisions
and force a whole-snapshot restart. The former evacuation-list detach/zpage
walk/rebuild path and its now-unused private detach helpers are gone. The
earlier private-node preparation remains as a conservative admission/OOM
contract and releases its unused plan after publication.

The page source contract was genuinely RED on the absent commit owner. A
phase-4 true-pthread probe pauses a 24-page scan after node 16, concurrently
resets/unlinks/recycles the complete set, and proves both roots restart empty
and later recover 24 pages / 1,440,000 bytes. The final packet passes 19/19 in
132.73 seconds, including phase 1/2/4 invalidation, forced plan growth/OOM,
four-thread reset/reseed and C/strict target-phase parity. Exact commands,
claims, exclusions and hashes are in
`docs/goal/evidence/2026-08-23-gc4-a3b-reseed-bounded-page-commit.md`; the
preceding relocation aggregate receipt remains
`docs/goal/evidence/2026-08-23-gc4-a3b-reseed-bounded-relocation-aggregate.md`.

This closes relocation reseed's known unbounded count/aggregate/page holder,
not the parent quiescence task. GC3 promotion/remembered-owner holders,
cleanup decrefs, extension-root/caller/runtime-root callbacks, strict logging,
A3c, raw container transactions and collector-owned STW remain open. The
investigation and parent task remain `active` / `IN_PROGRESS`.

## Update — 2026-08-23 A3b GC3 remembered-owner physical finish confirmed

### RESOLVED node-retirement sub-boundary; promotion holders remain open

Backend-3 remembered-owner nodes in C and strict pcc-Python now detach under
the graph lock and free through one caller-owned finish chain after the outer
unlock. Normal budgeted drain, overflow whole-list clear and telemetry reset
share that ordering. The strict generational scheduler also moved its final
processed-work safepoint after unlock, matching C.

The source contract was genuinely RED on the missing finish owner. Exact
LLVM/self closures and the source packet pass 10/10; C/strict remembered-child
promotion plus cross-domain remembered-slot rewriting bring the final packet
to 14/14 in 134.64 seconds. The first strict scheduler closure correctly
rejected a noncanonical multiline extern binding; fixing its source shape made
the existing exact cross-object allowlist accept the declared signature, with
no verifier relaxation. Exact chronology, hashes, commands and nonclaims are
in
`docs/goal/evidence/2026-08-23-gc4-a3b-gc3-remembered-owner-detached-finish.md`.

This removes remembered-node `free` tails only. Periodic remembered-overflow,
normal-drain and young-promotion safepoints still execute under the graph lock;
TLS exception oldification still cleanup-decrefs there; extension-module and
caller/runtime-root visitors remain callback-capable holders. A3c, raw
container transactions and collector-owned STW remain open. The investigation
and parent task remain `active` / `IN_PROGRESS`.

## Update — 2026-08-23 A3b GC3 locked safepoints split

### RESOLVED safepoint-holder sub-boundary; examined-node and callback bounds remain open

Backend-3 remembered overflow/drain and young promotion in C and strict
pcc-Python no longer call `pcc_thread_safepoint` while the object graph lock is
held. Each generational step caps successful remembered/young work at 16, and
the one processed-work safepoint occurs after graph unlock and detached-node
finish.

The strict source contract was genuinely RED on the in-lock remembered scan
safepoint. Direct strict self/no-libpython closure, C syntax with threads off
and on, and the final focused packet pass; the packet is 7/7 in 3.35 seconds.
Exact chronology, identities and logs are in
`docs/goal/evidence/2026-08-23-gc4-a3b-gc3-locked-safepoint-split.md`.

This is not a bounded-holder claim. Overflow fallback can still examine an
unbounded number of nonmatching object nodes while seeking up to 16 owners;
registered-root and extension/caller visitors remain callback-capable and
unbounded, and TLS exception oldification still cleanup-decrefs under the
lock. These are the next inventory boundaries before A3c. Raw container
transactions and collector-owned STW also remain open. The investigation and
parent task remain `active` / `IN_PROGRESS`.

## Update — 2026-08-23 A3b GC3 remembered-overflow cursor bounded

### RESOLVED examined-node holder sub-boundary; TLS cleanup and callbacks remain open

Backend-3 remembered overflow fallback in C and strict pcc-Python now examines
at most 16 tracked-object nodes per graph-lock tenure and returns examined
work. Its retained cursor is advanced before unlink/recycle; object-list
revision mismatch and a new overflow allocation failure restart from the
authoritative head.

The source contract was genuinely RED on the absent strict allocation seam
and retained cursor. A default-inactive allocation limit then forced the real
fallback in both production runtime roots. With 32 nonmatching nodes before
the remembered owner and another link between batches, both roots reported
three complete 16-node batches with the owner still pending, then cleared it
in the bounded fourth batch. The final focused packet passes 12/12. Exact
chronology, hashes, commands and nonclaims are in
`docs/goal/evidence/2026-08-23-gc4-a3b-gc3-remembered-overflow-cursor.md`.

This closes overflow object-list examination only. TLS exception oldification
still cleanup-decrefs while graph-locked; registered frame/scheduler roots,
extension-module roots and owner/caller visitors remain unbounded or
callback-capable. A3c, raw container transactions and collector-owned STW
remain open. The investigation and parent task remain `active` /
`IN_PROGRESS`.

## Update — 2026-08-23 A3b GC3 TLS cleanup split

### RESOLVED TLS terminal-decref holder; root and extension callbacks remain open

C and strict pcc-Python TLS exception copy-oldification now transfer the
replaced TLS-owned reference through one cleanup out-param. The generational
step unlocks the graph, finishes detached remembered nodes and only then
decrefs that saved reference.

The source contract was genuinely RED on the absent strict cleanup owner. C
and strict scalar TLS runtime probes remain green. A C true-pthread probe used
the callback-capable cpy-handle release hook: the hook successfully woke and
joined a contender whose next operation acquired the same graph lock, proving
the terminal decref was outside the holder. Strict GC3 supports no
callback-capable copy tag, so its parity claim is exact source/self closure plus
the scalar runtime path. The final packet passes 10/10. Exact commands, hashes
and exclusions are in
`docs/goal/evidence/2026-08-23-gc4-a3b-gc3-tls-cleanup-after-unlock.md`.

The probe also exposed a distinct C cpy-handle foreign-ownership defect; it is
isolated in `docs/investigations/gc3-cpy-handle-oldify-foreign-ownership.md`
and task `GC-P0-GC3-CPY-HANDLE-OLDIFY-OWNERSHIP`. It is not claimed fixed here.

Registered frame/scheduler roots, extension-module roots, owner referent
visitors and caller/runtime callbacks remain unbounded or callback-capable.
A3c, raw container transactions and collector-owned STW remain open. The
investigation and parent task remain `active` / `IN_PROGRESS`.

## Update — 2026-08-23 A3b GC3 extension-root callback split

### RESOLVED generational extension callback holder; trace/runtime visitors remain open

C and strict pcc-Python GC3 generational promotion now unlock before invoking
`pcc_capi_visit_extension_module_state_roots`. Each root reported by an
extension's external `PyModuleDef.m_traverse` callback is promoted through a
runtime-owned callback that reacquires the graph lock only for the managed
promotion transaction and unlocks before returning to extension code.

The source contract was genuinely RED on the missing strict callback owner.
Direct strict self/no-libpython closure, LLVM/self object closure, C syntax and
the production scheduler owner are green. A true-pthread `PyModuleDef` probe
makes `m_traverse` join a contender that next acquires the graph lock; it passes
against both C and strict production runtimes. The final packets pass 7/7,
2/2 and 6/6. Exact commands, hashes and mode limits are in
`docs/goal/evidence/2026-08-23-gc4-a3b-gc3-extension-root-callback.md`.

The existing real-extension integration gate did not enter GC3: self-link mode
failed closed on unsupported native-extension export anchors. No workaround or
green integration claim is made.

Trace-cycle extension traversal in `pcc_gc_gray_current_roots`, caller and
extension visitors in `pcc_gc_visit_runtime_roots`, registered
frame/scheduler walks, owner-referent traversal and remaining unbounded or
allocator-capable holders remain open. A3c, raw container transactions and the
collector-owned STW phase remain open. The investigation and parent task stay
`active` / `IN_PROGRESS`.

## Update — 2026-08-23 A3b GC3 registered-root enumeration bounded

### RESOLVED registry-enumeration holder sub-boundary; referent/caller holders remain open

C and strict pcc-Python GC3 now split frame/continuation and
scheduler/builtin-cache promotion into separate resumable graph-lock
transactions. Each transaction examines at most
`min(remaining_budget, 16)` root slots in the production scheduler. Registry
removals repair a retained cursor before node free, continuation relocation
retarget resets the active slot offset, and a graph-locked revision detects
reentrant mutation during slot promotion.

The first revision design was `[DENIED]`: restarting from the head after every
frame enter/leave promoted the newly inserted head but made no progress past
the first 16 existing roots (`frame batch2 old=16 inserted_old=1`), so normal
frame churn could starve deep roots. The accepted design does not restart for
head insertion; its C/strict probe proves 40 frame roots advance `16/16/8`, a
head inserted between batches waits for the next completed round, deleting
the scheduler node held by the cursor resumes from its successor, and all 19
surviving scheduler roots become old. Pointer replacement was also a denied
probe oracle because list promotion can occur in place; generation flags are
the final evidence.

The final source/ABI/LLVM+self packet passes 22/22, and the production packet
passes 15/15 with one-owner archive checks, five-backend C/strict registry
parity, pthread registry mutation and both bounded-batch runtime roots. Exact
commands, chronology, hashes and timeout disposition are in
`docs/goal/evidence/2026-08-23-gc4-a3b-gc3-registered-root-walk-bounded.md`.

This closes only registry-slot enumeration. One promoted root may still enter
unbounded owner-referent work while graph-locked. Trace-cycle extension
traversal, caller-provided runtime-root visitors and remaining
allocation/tripwire/log holders also remain open. A3c, raw container
transactions and collector-owned STW remain open; the investigation and
parent task stay `active` / `IN_PROGRESS`.

## Update — 2026-08-23 A3b scheduler-root link tripwire deferred

### RESOLVED two fatal-log edges; other tripwire/callback holders remain open

The C scheduler-root locked link helper now performs only the structural link
and computes a two-bit invariant result. Direct registration and
scheduler-queue publication both release the graph lock before a nonzero
result enters `pcc_runtime_tripwire_fail` and its runtime logging lock. The
checks remain compile-time armed; strict pcc-Python had no locked logging call
on this path.

The source contract was genuinely RED on the old `void` helper containing two
`PCC_RT_TRIPWIRE` calls. Threads-off and armed-tripwire threads-on C syntax are
green, and the final armed runtime packet passes 3/3, including valid
scheduler/continuation roots, ZPage forwarding and native-handle release.
Exact commands and hashes are in
`docs/goal/evidence/2026-08-23-gc4-a3b-scheduler-root-link-tripwire-after-unlock.md`.

This closes only scheduler-root **link** reporting. Other locked tripwire/log
sites remain. Owner-referent promotion is a recursive object-slot closure and
requires a remembered-worklist/slot-cursor design; it must not be made
superficially bounded by truncating traversal. Trace-cycle extension and
caller runtime-root callbacks also remain open. A3c, raw access and
collector-owned STW remain open; the investigation and parent task stay
`active` / `IN_PROGRESS`.

## Update — 2026-08-23 A3b trace initial extension callback split

### RESOLVED initial mark callback holder; final cut remains open

C and strict pcc-Python tracing schedulers now publish and claim one
`(epoch, backend)` extension-root token when a mark cycle begins. The claimant
unlocks for external `PyModuleDef.m_traverse`; each reported root reacquires
the graph lock only after token/epoch/backend/mark-active revalidation. Other
steps return while traversal is active, backend switch/finish clear stale
tokens, and the C CMS worker follows up through the same unlocked wrapper.

The source contract was genuinely RED on extension traversal inside C
`pcc_gc_gray_current_roots`. C and strict Backend-1 true-pthread callbacks both
join a real graph-lock contender. Scheduler/common-mark LLVM+self closures and
production owner checks remain green; the final packet passes 15/15. Exact
commands, cold-build disposition and hashes are in
`docs/goal/evidence/2026-08-23-gc4-a3b-trace-initial-extension-callback.md`.

The C final cut intentionally still traverses extension roots under graph lock
and STW before draining gray and classifying white candidates; strict final-cut
extension parity remains absent. This slice therefore closes only initial
mark. Owner-referent worklist design, remaining tripwire/log paths, A3c, raw
access and collector-owned STW remain open; the investigation and parent task
stay `active` / `IN_PROGRESS`.

## Update — 2026-08-23 A3b mixed-context tripwires

### RESOLVED three mixed-context fatal-log edges; owner work remains open

Instance-class, generic object-slot and remap-target validation now use a C
mixed tripwire: graph owners defer to outer unlock, while unlocked callers
retain immediate fatal behavior. Each invalid branch returns before consuming
corrupt layout or rewriting a mismatched slot. Armed/source gates pass 5/5.
The old relocation source-marker test failed before this path and is not
claimed. Evidence: `docs/goal/evidence/2026-08-23-gc4-a3b-mixed-context-tripwires.md`.

Other C/strict log sites and recursive owner-referent promotion remain open;
no tripwire-clean claim exists. A3c, raw access and collector-owned STW remain
open; the investigation and parent task stay `active` / `IN_PROGRESS`.

## Update — 2026-08-23 A3b trace final extension callback split

### RESOLVED final-cut callback holder and strict parity; owner/tripwire work remains open

After acquiring STW, C and strict tracing finishers now publish a final
state-3 `(epoch, backend)` token under the graph lock, unlock for external
`PyModuleDef.m_traverse`, and gray each reported root through a short
claim/epoch/backend-validated graph transaction. They then reacquire,
revalidate and clear the token, rescan internal roots, drain all gray work and
only then classify white candidates. Strict final-cut extension parity, which
was previously absent, is now present.

The source contract was genuinely RED on extension traversal inside C's locked
finish. C and strict raw-pthread probes both prove the production graph lock is
available during the second/final traverse while the finisher retains STW.
The first strict probe's managed `object_is_known` tail was `[DENIED]`: stage
codes proved the raw thread had already acquired/released the graph lock and
then violated the unregistered-thread managed-access contract. The accepted
strict probe stops after its production graph lock; C retains its native
object-known oracle. The final closure/owner/dynamic packet passes 18/18.
Exact chronology, commands and hashes are in
`docs/goal/evidence/2026-08-23-gc4-a3b-trace-final-extension-callback.md`.

Trace initial and final extension callbacks are now outside graph-lock
ownership. Final traversal intentionally remains inside the STW phase; no
registered-thread join claim exists. Owner-referent worklist design, remaining
tripwire/log or unbounded holders, A3c, raw access and collector-owned STW
remain open; the investigation and parent task stay `active` /
`IN_PROGRESS`.

## Update — 2026-08-23 A3b deferred graph tripwires

### RESOLVED three fatal-log holder edges; complete inventory remains open

The C graph lock now owns a thread-local first-failure tripwire slot. Selected
locked invariants record static message/file/line only; recursive unlocks keep
the slot pending, and the outer unlock releases the physical lock, completes
pending CMS flush work, clears the slot and only then enters the existing
fatal runtime log/abort sink.

GC3 YOUNG+OLD promotion rejection, scheduler-root null-slot visitation and
remembered-owner null-node drain now use the deferred path and stop their
dangerous local operation before unlock. The source contract was genuinely
RED on direct `PCC_RT_TRIPWIRE` calls. Armed normal and injected-failure gates
pass 4/4; the injected generation violation logs the original message and
aborts after the source-proven physical unlock. Exact commands and hashes are
in `docs/goal/evidence/2026-08-23-gc4-a3b-deferred-graph-tripwires.md`.

Strict has no matching checks on these paths. Other locked C/strict tripwire or
log sites remain and no global tripwire-clean claim is made. Owner-referent
worklist design, A3c, raw access and collector-owned STW remain open; the
investigation and parent task stay `active` / `IN_PROGRESS`.

## Update — 2026-08-23 A3b root-introspection tripwires deferred

### RESOLVED five query fatal-log edges; mixed-context sites remain open

Scheduler-root count's null-slot/link checks and continuation-root count's
map/slot/stable/count checks now record through the proven deferred graph
tripwire slot. Their query functions release the outer physical lock before
the fatal runtime sink. Armed valid-root and scheduler/frame observability
neighbors pass 4/4, including all five backend selections. Exact commands and
hashes are in
`docs/goal/evidence/2026-08-23-gc4-a3b-root-introspection-tripwires.md`.

This closes five introspection checks only. Mixed-context instance/object/remap
visitors and other fatal-log sites require separate classification. Strict has
no matching locked diagnostics here. Owner-referent worklist design, A3c, raw
access and collector-owned STW remain open; the investigation and parent task
stay `active` / `IN_PROGRESS`.

## Update — 2026-08-23 A3b runtime-root caller snapshot

### RESOLVED public caller callback reentry; snapshot enumeration remains unbounded

C `pcc_gc_visit_runtime_roots` now snapshots frame, continuation, scheduler
and builtin-cache values under the graph lock with one temporary owned
reference per non-null value, then invokes the caller and releases those
references after unlock. Count, allocation and fill use a revalidated retry;
allocation occurs unlocked and failure aborts outside the lock rather than
silently omitting roots. Extension traversal remains after the same unlock.

The source contract was genuinely RED on the absent snapshot helpers. A
true-pthread callback joins a real graph-lock contender, then reentrantly
unregisters its own scheduler root and drops the original owner reference; the
snapshot release remains safe. An 80-root probe forces heap storage and sees
each root exactly once. Builtin-cache and five-backend suspended-frame
neighbors remain green. The final packet passes 7/7; commands and hashes are
in `docs/goal/evidence/2026-08-23-gc4-a3b-runtime-root-caller-snapshot.md`.

This closes caller callback execution, not the lock-time bound: count and fill
remain proportional to registry size. Strict pcc-Python has no owner for this
public ABI. Trace-cycle extension traversal, owner-referent worklist design,
remaining tripwire/log paths, A3c, raw access and collector-owned STW remain
open; the investigation and parent task stay `active` / `IN_PROGRESS`.

## Update — 2026-08-23 A3b runtime-root snapshot bounded

### RESOLVED public snapshot lock bound; trace/owner closures remain open

C `pcc_gc_visit_runtime_roots` now uses a single runtime-thread owner and
resumable frame/continuation/scheduler/cache cursors. Each graph-lock tenure
examines at most 16 root slots; snapshot growth, caller callbacks, temporary
decrefs and extension traversal all remain unlocked. Unlink and continuation
retarget repair active cursors before node free or slot-base replacement.

The source contract was genuinely RED on whole-registry count/fill. A
true-pthread probe pauses after the first unlocked 16-slot batch, removes the
exact next scheduler node and inserts a new head. The in-flight forward-only
round sees the other 39 original roots and not the new head; the next call sees
the new head. Heap growth, callback self-unregister/owner release, builtin
cache and five-backend suspended-root neighbors remain green. The final packet
passes 9/9; commands, semantics and hashes are in
`docs/goal/evidence/2026-08-23-gc4-a3b-runtime-root-snapshot-bounded.md`.

The actual GC0 reachability caller owns STW, so its root round remains stable;
no atomic snapshot is claimed for arbitrary direct callers. Strict pcc-Python
has no owner for this ABI. Trace-cycle extension traversal, owner-referent
worklist design, remaining tripwire/log paths, A3c, raw access and
collector-owned STW remain open; the investigation and parent task stay
`active` / `IN_PROGRESS`.

## Update — 2026-08-23 A3b runtime-root extension callback split

### RESOLVED extension half of public runtime-root visitor; caller half remains open

The C `pcc_gc_visit_runtime_roots` entrypoint now unlocks before invoking
`pcc_capi_visit_extension_module_state_roots`. A true-pthread probe installs a
real `PyModuleDef.m_traverse`; the callback successfully wakes and joins a
contender whose next operation acquires the real graph lock. The final source,
shared-slot and builtin-cache neighbor packet passes 5/5. Exact commands and
hashes are in
`docs/goal/evidence/2026-08-23-gc4-a3b-runtime-root-extension-callback.md`.

This moves only extension traversal. The caller-supplied visitor still runs
under the lock for registered frame, continuation, scheduler and builtin-cache
roots. Trace-cycle extension traversal is a separate initial/final mark-cut
problem and remains under the lock. The strict pcc-Python runtime has no owner
for this public entrypoint, so no strict parity claim is made. Owner-referent
worklist design, remaining tripwire/log holders, A3c, raw access and
collector-owned STW remain open; the investigation and parent task stay
`active` / `IN_PROGRESS`.

## Update — 2026-08-23 A3b remaining locked fatal-log routing confirmed

### RESOLVED classification and routing sub-boundary; owner worklist design added

The remaining C/strict fatal-log inventory is now classified, routed, and
contract-covered. Seven locked-context C sites in `py_gc_backend.c` (five
direct `pcc_runtime_tripwire_fail` calls in the target-death payload chain,
source-side-table commit paths, granule retirement, normal-remap payload
chain; two `PCC_RT_TRIPWIRE` checks in `pcc_gc_backend4_zpage_remove_unlocked`
and `pcc_gc_backend4_note_forwarding_removed_on_page_unlocked`) now record
into the existing deferred thread-local slot and report only at outer unlock.
The five relocation-read-barrier validations in
`pcc_gc_note_relocation_read_unlocked` route through a mixed tripwire:
unlocked callers keep immediate fatals; a graph-lock owner defers and bails
without healing. A failed validation never increments
`pcc_gc_relocation_barrier_forwards`, and the lost-retained-span violation
heals to the validated forwarding target because returning `o` risks touching
a span that may already be unshared. The four `pcc_cpy_handle_move_owned_ref`
checks — reachable under the graph lock through GC3 oldification — defer via
one new cross-TU seam `pcc_gc_tripwire_defer_or_fail(msg,file,line)` that
returns whether it deferred; invalid-move bails before consuming corrupt box
layout. The strict pcc-Python port gained the mirror mechanism:
`freestanding_runtime_high_substrate.py` owns three deferred TLS slots plus
exported `pcc_py_gc_defer_tripwire`/`pcc_py_gc_finish_deferred_tripwire`,
`pcc_py_gc_minor_graph_unlock` finishes pending reports after physical
release on both its threads-off and depth-zero exits, and all five strict
fatal sites (`freestanding_gc_forwarding_retirement.py` x3,
`freestanding_gc_relocation_payload.py`, `py_gc_backend.py`) now defer.
`pcc_py_gc_defer_tripwire` was registered in the exact cross-object ABI
registry, both raw-import inventories were swapped, and the substrate symbol
allowlist grew by review.

Classified as needing no routing: young-promotion drain (reports after
unlock), continuation registration (before lock), scheduler-link helper
(callers release first), the three immediate calls in
`pcc_gc_visit_runtime_roots` (outside lock tenures), vthread-channel dealloc,
`py_cpy_handle_new`/dealloc, `py_obj_gc.c` reachability checks (STW owner,
not graph-lock holder), and `pcc_decref_finish` (no known in-lock caller
after the A3b finish tails; revisited if one appears).

Gates: source contracts for all routed regions pass 11/11 including two armed
probes — a true threaded archive proves a lock-holder continuation marker
prints before the deferred TRIPWIRE abort, and an unlocked probe proves the
immediate path survives; every assertion was shown RED against `git show
HEAD:` text before implementation. The production link-map ownership gate
passes 3/3 on the rebuilt archive, the GC3 oldification neighbor passes 6/6,
and the task-card payload/retirement pair passes 24/24. Exact hashes are in
`docs/goal/evidence/2026-08-23-gc4-a3b-locked-log-site-routing.md`.

This closes log-site routing only: no tripwire-clean claim exists for
unarmed builds, no A3c connection, raw access, collector STW, or physical
relocation is proved, and performance/pause costs remain later ARCH work.
Owner-referent promotion design follows below; implementation remains open.

## Proposal No.2 — Owner-referent promotion as a resumable remembered-slot worklist

### Design

Today one promoted root can drive an unbounded recursive closure inside a
single graph-lock tenure: `pcc_gc_promote_owner_referents(o, recurse)` walks
`py_obj_visit_slots`, each OWNED slot runs oldify-copy inline, and the copied
target immediately recurses. The accepted shape replaces inline recursion with
a resumable worklist following the established A3b bounded-scan pattern:

1. **Entry form.** Entries are `(owner_node, byte_offset, role, recurse)`
   rather than raw `PyObject**`. The slot address is recomputed from the
   node's current object pointer at pop time, so relocation of the owner
   between tenures cannot dangle the stored address.
2. **Seeding.** Remembered owners, TLS exception roots, and the young-drain
   frontier push first-level entries exactly where today's first recursion
   level would run. Pushing happens under the same short lock tenures that
   discover the slots.
3. **Bounded tenure.** Each generational step pops at most
   `min(remaining_budget, 16)` entries per graph-lock tenure, promotes them,
   pushes newly-copied targets' owned slots, unlocks, finishes detached
   storage, and only then polls — matching the GC3 locked-safepoint split and
   the 16-entry constant used by reset/reseed/snapshot cursors.
4. **Revalidation.** At pop time the entry revalidates: node live and not
   `freeing`, registry revision unchanged since push or provably harmless
   (promotion is monotonic), and offset within the node's current size.
   Stale entries drop lazily; a full reset clears the list under lock with
   detached-node finish after unlock.
5. **Idempotence.** YOUNG->OLD promotion is sticky, so duplicate entries
   (owner visited twice) collapse into no-ops without dedup structures;
   examined-work accounting counts popped entries as every other bounded
   scan does.
6. **Mirror parity.** The strict freestanding generational scheduler owns
   identical list ops in its scheduler module (no injected polls while
   locked), with C/strict differential probes forcing an unlock window at
   entry N of M using the default-inactive diagnostic seam pattern.

Non-goals: no write-barrier enqueue change, no wait-freedom claim, no A3c
connection until this holder plus remaining callback-capable visitors are
bounded, and no performance acceptance.

## Update — 2026-08-24 A3b owner-referent logical-slot worklist confirmed

### RESOLVED built-in owner-slot bound; external callbacks remain open

The raw-byte-offset part of Proposal No.2 was `[DENIED]` before implementation.
List, dict, set, class and continuation slots can live in independently
reallocated payloads, so an address-derived byte offset cannot be carried
across graph-lock release and later recomputed from only the object header.
The accepted replacement stores no raw slot address: a stable object node owns
a logical physical-slot cursor, and each tenure resolves that cursor against
the owner's current layout through the canonical
`pcc_gc_visit_object_slots_slice` contract.  The ordinary full visitor now
delegates to the same slice contract with an unbounded limit, so trace, update,
copy, sweep and promotion do not consume two independently authoritative
layout switches.

C and strict pcc-Python now enqueue promoted/remembered GC3 and GC4 owners and
examine at most 16 logical slots per graph-lock tenure.  Every tenure
revalidates the current object-index node after object-list revision changes,
re-resolves out-of-line payload bases, unlocks, then polls.  Object unlink
repairs the intrusive queue before recycling.  GC4 explicit tracing drains any
older pending promotion work before reusing `gc_refs` for tracing.

The first implementation enlarged every `PccGcObjectNode` from 80 to 120
bytes and was rejected during the stage2 cost audit before final evidence.
The accepted representation keeps the 80-byte node layout: once an owner is
OLD and detached from the young list, the queue reuses `young_next`,
`young_prev` and `gc_refs` as its links and logical cursor.  No worklist node or
slot entry is allocated under the graph lock, and no permanent per-object
memory increase remains.

The source contract was genuinely RED before implementation: the focused node
failed because no bounded drain existed.  Final C/strict pthread probes pause
after logical slot 16, acquire the production graph lock from a contender,
append a 41st list child while unlocked, then prove all 41 current-layout slots
are promoted and rewritten without retaining a stale payload address.  A
separate GC4 owner-wide barrier differential drains one entry plus forty
logical slots in both runtime roots.  Existing GC3 list/dict/set/instance/
valuebox rewrites, 65,536-object young scheduling, registered roots, GC4
terminal finalizers and maintenance accounting remain green.

Final focused packets on the recorded source identity:

```text
38 passed in 12.98s  strict slot/node/promotion/scheduler/barrier closure and
                     production link-map ownership
43 passed in 5.85s   strict object-slot behavior plus complete shared-slot
                     source/runtime contract
18 passed in 149.52s C/strict pthread and GC3/GC4 promotion behavior
24 passed in 6.30s   relocation-payload plus forwarding-retirement task gate
```

One earlier 120-second closure command ended without a final summary during a
known 120–140 second cold archive build; it is not evidence.  Immediate process
inspection found no surviving pytest or pcc child.  The same focused closure
was rerun with a measured 240-second inner budget and then on the final warm
archive; only the final summaries above are claimed.

This closes built-in owner-referent enumeration and recursive promotion work.
C-extension slot traversal still enters an external callback under the graph
lock and is intentionally routed through the retained callback fallback; it is
not bounded or callback-safe here.  The remaining complete holder inventory,
A3c graph-lock/no-park connection, raw container transactions,
collector-owned STW, source/page lifetime, ABA/backend-switch proof,
constructor publication, raw C-API leases, callback roots, resurrection,
stale-candidate fairness, stage/performance, fixed point and broad five-GC
parity remain open.  The investigation and parent task stay `active` /
`IN_PROGRESS`.

## Update — 2026-08-24 A3b C-extension promotion callback split

### RESOLVED promotion callback holder; trace/remap callbacks classified open

The C-extension fallback in the GC3/GC4 owner-promotion worklist no longer
invokes external `tp_traverse` while holding the GC graph lock. The locked
tenure validates the non-moving C-extension owner, takes one temporary owned
reference, detaches its worklist node and unlocks. `tp_traverse` then runs
unlocked; each synchronous `Py_VISIT` slot re-enters exactly one short graph
transaction for promotion. The owner reference is released only after the
external callback returns. C and strict pcc-Python use the same ordering.

The dynamic test was genuinely RED on the prior path. A true runtime thread
woke a contender whose next operation acquired the production graph lock from
inside `tp_traverse`; the old path returned 11 because the contender could not
acquire before the bounded callback wait expired. The final C and strict probes
both pass, retain the C-extension owner, keep its child slot valid and promote
the child `YOUNG -> OLD`.

Final focused packets:

```text
46 passed in 149.19s  promotion source/LLVM+self/production ownership,
                      C+strict callback probe and shared-slot contract
20 passed in 5.27s    complete owner-worklist C/strict pthread neighbors
24 passed in 6.55s    relocation-payload plus forwarding-retirement task gate
```

The follow-up callsite inventory separates three other uses of the shared slot
contract and does not overclaim them:

1. Relocation-copy and forwarded-source payload preparation call the visitor
   only for copy-supported tags; C-extension tags are rejected before those
   callsites, so they do not own a C-extension callback edge.
2. Trace/mark paths (`pcc_gc_cms_trace_gray_object_unlocked`, refcount-root
   subtraction, incremental gray steps and final gray drain) can still invoke
   C-extension `tp_traverse` under the graph lock. Initial/incremental tracing
   needs a cycle/object claim before callback unlock; final tracing also owns
   STW but must preserve the same gray-count/color commit protocol.
3. Backend-4 remap walks every active object and can invoke C-extension
   `tp_traverse` to heal moved children even though the C-extension owner itself
   is non-moving. Unlocking that object-registry loop requires the still-open
   collector-owned STW/source-lifetime/revision protocol; the promotion helper
   is not a valid substitute.

Generic backend-0/tracing clear/deallocation callbacks remain separately owned
by their collector/finalizer contracts. No claim is made that every callback
holder is now closed.

Therefore A3c remains disconnected. The next callback finite slice must add a
trace-cycle C-extension claim and unlocked per-slot gray transactions, or first
establish the collector-owned STW phase that the remap traversal requires.
Raw container transactions, source/page lifetime, ABA/backend switch,
constructor publication, C-API raw leases, callback roots, resurrection,
stale-candidate fairness, stage/performance, fixed point and broad five-GC
parity remain open. The investigation and parent task stay `active` /
`IN_PROGRESS`.

## Update — 2026-08-24 incremental C-extension trace claim

### RESOLVED incremental gray-object callback; seed/final/CMS/remap remain open

The ordinary incremental trace cursor now claims one gray C-extension object
under the graph lock, retains the non-moving owner and records exact
`(object, cycle_epoch, backend)` state. It advances the authoritative cursor,
unlocks, runs external `tp_traverse`, and grays each reported slot through a
short transaction that revalidates the claim, epoch, backend and active mark
cycle. The final short transaction revalidates object liveness and gray state,
decrements the gray count, commits BLACK, clears the token, unlocks and only
then releases the temporary owner reference. A reentrant trace step observes
the pending token and returns rather than entering the same object twice. C and
strict pcc-Python mirror this protocol.

The source contract was RED on repository HEAD: neither C claim/complete helper
nor strict pending-token route existed. The final true-pthread C/strict test
starts a mark cycle while sixteen newer fillers keep the C-extension object
behind the cursor, arms the external callback, then proves a contender can
acquire the real graph lock from inside `tp_traverse`.

Final focused evidence:

```text
16 passed in 5.14s    common-mark/scheduler source, LLVM+self, production owner
                      and C/strict callback behavior on warm final archives
1 passed in 148.57s   cold final threaded strict callback node
3 passed in 1.78s     production collector link-map ownership
23 passed in 6.16s    promotion + incremental trace callback and GC3/GC4
                      owner-worklist source/pthread neighbors
24 passed in 7.92s    relocation-payload plus forwarding-retirement task gate
```

This closes only the normal `pcc_gc_tracing_step_cycle` gray-object cursor.
Three trace surfaces remain separately open: initial refcount-root subtraction
invokes C-extension traversal before `mark_active` publication; final/CMS
whole-gray drains run under STW but still hold the graph lock; and direct CMS
gray-object tickets bypass the ordinary wrapper. They need a seed/final/CMS
claim protocol with post-resume temporary-owner cleanup, not a wording change.
Backend-4 remap/update C-extension traversal remains dependent on the
collector-owned STW, source/object lifetime and resumable registry revision
contract. A3c remains disconnected; all raw-access, publication, lease,
resurrection, stage/performance and fixed-point nonclaims remain unchanged.

## Update — 2026-08-24 direct CMS C-extension gray ticket

### RESOLVED direct-ticket callback split; CMS RESCAN remains open

`pcc_gc_cms_trace_gray_object_unlocked` now recognizes a gray C-extension
object and claims it with the same retained `(object, cycle_epoch, backend)`
token used by the ordinary incremental trace cursor instead of invoking
`tp_traverse` under the graph lock.  The production CMS worker snapshots the
claim, unlocks the graph, and calls `pcc_gc_trace_cext_complete`; per-slot gray
transactions and the final color/gray-count commit therefore reuse one
authoritative revalidation protocol.

The focused true-pthread probe dynamically invokes the exact direct-ticket
helper under the graph lock, releases it through the production completion
shape, and proves a raw contender can acquire the real graph lock from inside
`tp_traverse`.  It intentionally runs with the incremental backend to avoid
turning this callback test into a separate CMS worker/STW registration test.
Static source assertions prove the real worker consumes the same pending token
and orders graph unlock before completion.  This is a direct-helper plus worker
routing claim, not a real CMS-worker end-to-end claim.

An initial diagnostic counter incremented atomically on every production
C-extension direct ticket.  It was removed before final evidence because the
callback and return value already prove the route and a permanent hot-path
atomic would be unjustified measurement overhead.  The final frozen source
passes the callback/source pair 2/2 in 8.29s, CMS worker/queue neighbors 3/3 in
0.35s, and the cold final-source relocation/forwarding task packet 24/24 in
145.45s.  C syntax with threads off/on and `git diff --check` pass.  Exact
commands, hashes and the superseded no-summary timeout are recorded in
`docs/goal/evidence/2026-08-24-gc4-a3b-cext-direct-cms-ticket.md`.

The CMS RESCAN ticket still routes through `pcc_gc_drain_all_gray_unlocked`
while the graph lock is held.  Initial refcount-root seed traversal, final/CMS
whole-gray drains and Backend-4 remap/update C-extension callbacks remain open
and dependency-ordered behind collector-owned STW, temporary-owner cleanup,
object/source lifetime and registry revision.  A3c remains disconnected; raw
access, publication, leases, resurrection, stage/performance, fixed point and
broad five-GC parity remain unclaimed.  The investigation and parent task stay
`active` / `IN_PROGRESS`.

## Proposal No.3 — stopped-world whole-gray C-extension callback slices

### Pre-registered design and claim boundary

The final tracing cut and the C runtime CMS RESCAN ticket already own a stopped
world, but both call the whole-gray drain while also holding the graph lock.
The accepted finite slice keeps the stopped world across every callback and
splits only the graph-lock tenure:

1. A locked drain slice processes built-in gray objects normally.  On the first
   gray C-extension object it takes the existing retained exact trace token and
   returns without invoking `tp_traverse` or committing BLACK.
2. A stopped-world owner releases the graph lock, runs the shared C-extension
   traversal/per-slot transactions/final commit, and repeats locked slices
   until no pending token remains.  Exact cycle/backend/mark identity is
   revalidated on every slice.
3. The final-cut owner moves its current-root gray pass before this stopped-
   world drain wrapper, then revalidates the finisher token before publishing
   sweep candidates.  The pure commit helper no longer owns a callback-capable
   drain.
4. CMS RESCAN uses the same stopped-world wrapper rather than returning after
   the first claimed C-extension object; this preserves the overflow ticket's
   whole-gray-set meaning.
5. The strict pcc-Python final-cut path mirrors the locked-slice and stopped-
   world wrapper.  Its CMS worker has no raw pointer-ticket/RESCAN queue and
   continues through the ordinary tracing step, so no fictitious strict RESCAN
   route is added.

The source contract must fail on the current locked whole-drain calls before
implementation.  Final C/strict true-pthread probes must enter `tp_traverse`
during a real final cut while a contender acquires the production graph lock;
the C CMS probe may use the exact stopped-world RESCAN wrapper plus source
routing if the independent worker registration handshake would test a
different STW property.  The claim is callback/commit ordering only: initial
refcount-root seeding remains dependency-ordered behind collector-owned STW,
and remap/update remains behind STW plus object/source lifetime and registry
revision.  No pause-time or stage-performance acceptance is claimed.

## Update — 2026-08-24 final/CMS whole-gray callback split

### RESOLVED final cut and CMS RESCAN; seed/remap remain open

Proposal No.3 is implemented.  C and strict now own a locked whole-gray slice
that drains built-ins but claims and retains at most one gray C-extension
object without invoking its callback.  Their stopped-world owner releases the
graph lock, runs the shared per-slot/final revalidated completion, and repeats
until the pending token is empty.  The final-cut owner moved current-root
rescan before that wrapper, then reacquires and revalidates the exact finisher
token before publishing candidates; the pure finish commit contains no
callback-capable drain.  The C CMS RESCAN branch calls the same wrapper while
retaining its existing stopped world and therefore still drains the whole gray
set after an overflow ticket.  Strict CMS has no direct-ticket/RESCAN queue and
was not given a fake mirror path.

The source test was genuinely RED because no locked slice existed.  Final C
and strict true-pthread probes run seed and ordinary C-extension tracing
disarmed, arm after the ordinary trace while a tail keeps the cursor open, and
prove the final rescan callback owns STW while a raw contender acquires the
physical runtime graph lock from inside `tp_traverse`.  An initial shared
high-level contender operation was `[DENIED]` as a cross-runtime lock oracle
because it mixed strict STW-aware entry behavior into the graph-lock question.
The final probe uses existing runtime-specific physical lock operations and no
diagnostic runtime symbol remains.

Final packets are 18/18 callback/source/LLVM+self/archive in 5.33s, 9/9
finisher/link-map/CMS neighbors in 2.48s, and 24/24 task-card
relocation/forwarding in 13.92s.  Two earlier cold commands without final
summaries are explicitly non-evidence.  The unrelated strict explicit-collect
probe stopped in self precise-stackmap compilation before GC execution, and
the unchanged runtime-high 4-thread counter probe remained non-green; neither
was modified or claimed.  Exact commands and source/log hashes are in
`docs/goal/evidence/2026-08-24-gc4-a3b-cext-final-cms-whole-gray.md`.

Initial three-pass refcount-root subtraction still invokes C-extension
`tp_traverse` before `mark_active` publication.  Unlocking it without STW would
invalidate its refcount/edge snapshot, while skipping the edge subtraction
would conservatively leak C-extension cycles; it is dependency-ordered behind
collector-owned STW or an exact snapshot protocol.  Backend-4 remap/update
callbacks remain dependency-ordered behind collector-owned STW, object/source
lifetime and registry revision.  A3c stays disconnected and all raw-access,
publication, lease, resurrection, stage/performance, fixed-point and five-GC
nonclaims remain.  The investigation and parent task stay `active` /
`IN_PROGRESS`.

## Proposal No.4 — collector-owned initial seed token

### Pre-registered design and claim boundary

The initial refcount-root pass is one atomic three-pass snapshot: copy every
tracked refcount into `gc_refs`, subtract every managed graph edge, then gray
positive external roots.  Releasing only the graph lock around C-extension
`tp_traverse` would allow mutators to change both refcounts and edges between
passes; skipping C-extension subtraction would keep cycles with extension
owners permanently rooted.  Both shapes are `[DENIED]`.

The accepted design reuses the existing extension-root epoch/backend token
with a new state `pending=4` for seed ownership rather than adding object or
side-table fields:

1. Under the graph lock, an inactive requested cycle advances its epoch,
   records exact epoch/backend identity and publishes seed-pending state 4.
   It does not whiten objects or invoke callbacks.
2. The outer scheduler releases the graph lock, acquires or reuses a stopped
   world, revalidates the exact seed token under one short graph transaction,
   then runs the complete three-pass seed with no graph lock.  Other mutators
   cannot change the snapshot; the C-extension `tp_traverse` contract supplies
   stable synchronous edge enumeration for the collector thread.
3. A second short transaction revalidates seed identity and only then publishes
   `mark_active`, clears `cycle_requested`, transitions extension roots to
   ordinary pending state 1 and initializes the trace cursor.  Stop failure or
   identity drift clears only the exact seed token and leaves the cycle request
   retryable.
4. Public backend switching rejects seed-pending state instead of letting a
   callback reenter and change collector identity mid-snapshot.  Ordinary
   tracing steps already stop on any nonzero pending state.
5. C and strict pcc-Python mirror the same claim/STW/commit order.  A true-
   pthread C/strict probe arms a C-extension callback during the first real
   seed and proves the callback observes STW ownership while an unregistered
   contender acquires the physical graph lock.

This closes seed callback ordering only.  It does not claim a bounded seed
pause; the existing O(heap) three-pass work is merely moved under the STW it
needs for semantic validity.  Long-pause optimization remains a later exact
snapshot/incremental-root-accounting task.  Backend-4 remap/update still
requires its own lifetime/revision protocol.

## Update — 2026-08-24 initial refcount-root seed STW

### RESOLVED trace callback inventory; remap/update remains open

Proposal No.4 is implemented.  C and strict claim initial seed state under the
graph lock with `trace_extension_roots_pending=4` and exact cycle/backend
identity, then the outer scheduler releases the graph lock and acquires or
reuses STW.  After pre-seed token validation, the complete refcount-copy /
managed-edge-subtraction / external-root-gray pass runs without the graph lock.
A second exact validation publishes `mark_active`, clears the request,
transitions the extension-root token to ordinary state 1 and initializes the
trace cursor.  Stop failure or drift clears only the exact token.  C preflight
and commit plus strict set-backend entry reject seed state 4, including callback
reentry.

The source contract was RED before implementation.  Final C/strict pthread
probes arm only the first `tp_traverse`, prove it observes owned STW while a raw
contender acquires the physical runtime graph lock, and prove same-backend reset
returns `-1` mid-seed.  The complete seed/ordinary/final/CMS packet is 28/28,
production/link/C explicit neighbors 5/5, and task relocation/forwarding is
24/24.  Exact commands and hashes are in
`docs/goal/evidence/2026-08-24-gc4-a3b-cext-initial-seed-stw.md`.

The prior single-finisher test schedule was updated honestly: its old first-
stop-is-final assumption is false once seed owns STW, so each window now
consumes seed and one rooted staging object before admitting the final
contender; every original semantic assertion remains.  A strict churn
performance neighbor remains non-green at `steps=1072` versus `<500`, but a
single-variable archive A/B produced the exact same `1072/debt=120` on pre-seed
and seed archives, denying attribution to this change.  The threshold was not
weakened.

All classified trace/mark C-extension callback holders are now closed:
promotion, ordinary incremental, direct CMS, initial seed, final cut and CMS
RESCAN.  Backend-4 remap/update remains the final classified slot-callback
holder and still requires collector-owned STW, object/source lifetime,
registry revision and updateable/reloaded temporary roots.  A3c stays
disconnected and raw access, publication, leases, resurrection,
stage/performance, fixed point and broad five-GC parity remain unclaimed.  The
investigation and parent task stay `active` / `IN_PROGRESS`.

## Proposal No.5 — stopped-world C-extension remap prepass

### Pre-registered design and claim boundary

C-extension owners are non-moving, but their child slots can still point to
Backend-4 forwarding sources.  The existing remap loop invokes external
`tp_traverse` while holding the graph lock and immediately retires forwarding
metadata/shell payloads after the object walk.  Reusing the trace token is
invalid because remap mutates slots and must retain every forwarding source
until every callback finishes.

The accepted finite slice adds a stopped-world C-extension prepass before the
existing built-in/root/remap-retirement commit:

1. Every remap callsite invokes one wrapper with no graph lock.  It acquires or
   reuses STW, then under the graph lock starts an exact remap phase with a
   monotonic epoch and snapshots object-list revision, forwarding head/count,
   page-reseed revision and relocation-reseed revision.
2. The wrapper advances a local object-node cursor under short graph
   transactions.  Built-in owners are left for the existing final locked pass.
   A C-extension owner is retained, recorded as the exact pending owner, and
   its callback runs with STW still owned but the graph lock released.
3. Every callback-reported slot enters one short graph transaction, validates
   phase epoch, pending owner, backend 4 and every captured revision/forwarding
   identity, reloads the slot and applies the ordinary remap heal.  No child or
   raw slot pointer survives callback reentry.
4. After all C-extension owners complete, the wrapper reacquires the graph
   lock and revalidates the complete snapshot.  Only a valid phase runs the
   existing built-in/root heal and two-epoch forwarding/source retirement; that
   final locked loop explicitly skips C-extension owners already handled by the
   prepass.  Drift aborts before retirement, leaving current forwardings and
   source payloads fail-closed.
5. Backend switching, nested Backend-4 steps and direct evacuation drains
   reject/defer while the remap phase is active.  Allocation/free reentry may
   change object-list revision but then invalidates the commit rather than
   retiring against a changed registry.  Temporary owner release and detached
   finish work happen only after graph unlock; physical/decref finish remains
   after world resume.
6. C and strict pcc-Python mirror one state/validation contract.  A true-
   pthread C/strict probe forces a real forwarded child through a C-extension
   owner, acquires the physical graph lock inside `tp_traverse`, verifies the
   slot is healed to the target, and proves reentrant backend switch/direct
   remap cannot steal the active phase.

This closes only remap/update callback ordering and lifetime.  It does not yet
prove the broader raw-container transaction/no-park contract for the copy
phase, constructor publication, C-API leases or bounded pause/performance.

## Update — 2026-08-24 C-extension remap STW prepass

### RESOLVED complete callback inventory; A3c/raw access remain open

Proposal No.5 is implemented in C and strict.  Every remap callsite now enters
a no-graph-lock wrapper that acquires/reuses STW, starts an exact monotonic
phase, and snapshots object-list revision, forwarding head/population and both
page/relocation reseed revisions.  A local active-node cursor retains each
non-moving C-extension owner; its callback runs without the graph lock; each
slot re-enters a short fully revalidated transaction and reloads/heals the slot.
Only a fully valid prepass can enter the existing locked built-in/root heal and
two-epoch retirement, whose object loop now skips C-extension owners.  Drift
leaves forwardings/source payloads intact.  Backend reset, nested step/direct
drain and nested remap cannot steal an active phase.

The initial nested-wrapper implementation incorrectly ran common cleanup even
when it failed to start, clearing the outer active/pending state.  The dynamic C
probe returned 9 and forced the correction: a non-started nested wrapper now
returns before touching existing state.  Final C/strict probes prove STW,
physical graph-lock availability, reset/nested-remap rejection and old->target
slot healing before retirement.

The strict probe also found the generic i64 C-extension slot bridge double-
wrapping its sentinel, so the original visitor was never invoked.  A dedicated
strict i64 adapter now mirrors the C oracle; owner/source tests forbid the old
double-sentinel route.  This is a shared slot-ABI repair, not a remap or package
special case.

Final packets: remap/retirement/drain/barrier 38/38 in 174.38s; real C/strict
remap 2/2; complete callback-holder chain 31/31; task relocation/forwarding
24/24; shared-slot/link-map neighbors 10/10.  Exact commands and identities are
in `docs/goal/evidence/2026-08-24-gc4-a3b-cext-remap-stw-prepass.md`.

The full classified C-extension slot-callback inventory is now closed and
source-/pthread-green.  The parent P0 remains open: connect outermost graph lock
to A1 no-park after successful CAS, then close real raw list/dict/set access,
copy/retirement source lifetime, backend ABA, constructor publication, C-API
leases, callback roots, resurrection and stale-candidate fairness.  No stage
performance, fixed-point or broad five-GC claim is made.  The investigation and
parent task stay `active` / `IN_PROGRESS`.

## Proposal No.6 — A3c graph-lock/no-park connection

### Pre-registered design and claim boundary

With callback/blocking/allocator/decref/log holders now routed out of graph-lock
tenures, connect only the outermost physical lock ownership to the existing A1
thread no-park lease:

1. Recursive graph-lock acquisition changes graph depth only; it does not
   nest no-park depth.
2. A waiter remains parkable while its CAS fails and may continue using the
   existing safepoint/backoff loop.  `pcc_thread_no_park_enter` occurs only
   after successful physical CAS and before graph depth becomes one.
3. Outermost release decrements graph depth to zero, physically release-stores
   the lock word, runs deferred CMS flush/tripwire work, and only then calls
   `pcc_thread_no_park_exit`; that exit is allowed to service a pending stop.
4. Threads-off lock elision remains unchanged.  C and strict pcc-Python use the
   same ordering and recursive-depth contract.
5. A source contract plus C/strict runtime probe must prove graph depth 1/2 maps
   to no-park depth 1/1 and outer unlock returns it to zero.  Existing STW/new-
   thread/no-park probes remain green.

This connects graph-lock ownership to no-park but does not yet prove that a
mutator holds the graph lock across the entire real list/dict/set raw access.
Those transactions are the next boundary.

## Update — 2026-08-24 A3c graph-lock/no-park connected

### RESOLVED A3c ordering; raw container transactions remain open

Proposal No.6 is implemented.  Threaded C and strict outer graph acquisition
complete/await thread registration before CAS; failed CAS loops remain
parkable; successful CAS takes one no-park lease before setting graph depth
one.  Recursive graph acquisition changes only graph depth.  Outermost release
physically unlocks, completes deferred flush/tripwire work, then exits no-park
and may service a pending stop.  Threads-off behavior is unchanged.

The first implementation registered only after CAS.  A real unregistered raw
contender entered during seed STW, held the graph lock while registration waited
for world resume, and deadlocked the STW owner.  Final source moves registration
before CAS.  Callback probes now assert the STW owner runs with no-park depth
zero rather than assuming an unregistered thread may bypass STW; separate
newcomer and unregister tests cover real admission semantics.

Final evidence is 17/17 no-park/newcomer/callback, cold strict 1/1 nonthreaded
and 3/3 threaded callback, 31/31 complete holder chain, 45/45
remap/payload neighbors and 24/24 task-card gate.  Exact hashes and logs are in
`docs/goal/evidence/2026-08-24-gc4-a3c-graph-no-park.md`.

A3c closes graph-lock ownership only.  The parent P0 remains open for real
list/dict/set raw-access transactions across barrier/load/incref/store/decref,
copy/retirement source lifetime, backend ABA, constructor publication, C-API
leases, callback roots, resurrection and stale-candidate fairness.  No stage
performance, fixed-point or broad five-GC claim is made.  The investigation and
parent task stay `active` / `IN_PROGRESS`.

## Proposal No.7 — shared object-slot store transaction

### Pre-registered design and claim boundary

Route the generic list/dict/set/object slot writer through the already-proven
store-root prepare/commit/finish model, generalized with an owner argument:

1. Backend 0 retains its direct fast path.
2. For backends 1-4, plan initialization and historical owner logging occur
   before graph acquisition.  The outer graph/no-park transaction performs
   store telemetry, forwarding canonicalization, refcount-increment prepare,
   owner-aware write barrier, old-value load, raw slot publication and
   refcount-decrement prepare without a finalizer/log/free tail.
3. Outermost graph unlock ends the raw access.  A store-pointer-specific finish
   emits prepared refcount diagnostics and runs the old-value deallocation tail
   without duplicating the historical store log.  Existing root-store finish
   keeps its own NULL-owner log contract.
4. C and strict use one 128-byte plan layout and exact commit/finish ordering.
   The existing fresh-native-instance specialization remains separately open
   unless source routing proves no movable container path consumes it.
5. A paused-mutator true-pthread probe must stop after entering the real
   owner-aware transaction, show collector STW cannot complete, then finish the
   slot write and prove target/refcount/retirement state after exit.

This slice protects generic pointer-slot publication.  Container rehash/raw
array base swaps and read-only borrowed/raw views remain later sub-boundaries.

## Update — 2026-08-24 generic pointer-slot transaction

### CONFIRMED helper transaction; real container tenure remains open

Proposal No.7's generic `pcc_gc_store_ptr` path is implemented in C and strict
pcc-Python.  Backend 0 keeps its direct fast path.  Backends 1-4 now run
forwarding canonicalization, prepared NEW retain, owner-aware barrier, OLD
load, raw slot publication and prepared OLD release within one graph/no-park
tenure, then run diagnostic/finalizer/free tails after unlock.  The internal
128-byte plan and exact cross-object ABI are shared.

An apparent tricolor regression was investigated against the emitted binary.
`[DENIED]` The transaction did not lose the owner or child and did not skip an
active-cycle barrier: LLDB showed backend 1, black owner and white child at the
barrier with `mark_active == 0`.  The old broad test depended on a cycle that it
never started; shading there would fabricate the phantom cycle explicitly
forbidden by the barrier contract.  The expectation now remains white until
the next step.  A deterministic replacement starts a real cycle, leaves 63
gray work items, and proves C and strict stores shade the white child.

Focused evidence is 4/4 ABI/root contracts, 4/4 backend 3/4 C/strict finalizer
re-entry, 8/8 container refcount/UAF, 8/8 write-barrier routing, 2/2 real active
cycle, 15/15 five-GC abstraction, and 24/24 relocation/retirement gate.  Exact
commands, logs, hashes and claim exclusions are in
`docs/goal/evidence/2026-08-24-gc4-generic-slot-store-transaction.md`.

The fresh-native-instance specialization is used only by the separately named
fresh list-append constructor path, not generic dict/set stores; its publication
proof remains open.  Dict/set rehash still installs raw bases, copies
key/value slots, updates span metadata and frees old bases without one enclosing
owner-canonical graph/no-park tenure.  Therefore the real-container
three-party pause gate is not yet claimed, and the parent investigation remains
`active`.

## Proposal No.8 — owner-canonical dict/set rehash transaction

### Pre-registered design boundary

Allocate and initialize replacement raw tables outside the graph lock, but do
not publish them.  Before any callback-free copy/commit phase, retain the owner
through an updateable temporary/native root, acquire the graph/no-park lease,
reload/canonicalize the owner, and revalidate the exact old base/capacity/
entries-used snapshot.  Inside that one tenure:

1. copy live old entries using a callback-free raw open-addressing placement
   routine (stored hashes plus occupancy only; no `__eq__`, allocator, logger,
   finalizer or safepoint);
2. run owner-aware barriers before publishing each migrated pointer slot;
3. retarget backend-4 payload-span metadata from the exact old base to the new
   base without allocating;
4. publish owner base/capacity/count fields atomically with respect to the GC
   graph, then release the graph/no-park lease;
5. free old raw bases and any denied replacement allocation only after unlock,
   and finish/unregister the temporary owner root outside the graph lock.

If owner identity, old base, size or backend epoch changed during unlocked
preparation, deny/retry without publishing.  C and strict must share the same
transaction and failure semantics.  A true-pthread C/strict probe will hold an
outer recursive graph lease, admit a collector stop request, execute an actual
rehashing dict/set mutation while the collector waits, then release and verify
contents, refcounts, forwarding target, payload-span base and old-base
retirement.  List payload-base replacement must be inventoried separately
before this proposal can close the full container boundary.

### Design refinement before implementation: preserve pending slot metadata

The existing GC4 rehash barrier test proves a newly inserted young key has a
pending store-buffer entry whose slot points into the old table at the exact
moment rehash begins.  Draining that entry before rehash would prematurely
promote the key and erase the expected rehash barrier; freeing the table without
draining or retargeting would leave a dangling slot.  Therefore Proposal No.8
must preserve, not discard, pending metadata.

The commit will build an outside-lock `(old_slot, new_slot)` mapping and invoke
one callback/allocator/free-free locked GC operation that:

1. validates every pair stays within the exact old/new payload spans;
2. retargets all matching medium/global store-buffer slots for the owner;
3. removes old remembered-page/zpage-card accounting, retargets matching
   remembered-slot nodes, retargets the payload span itself, then rebuilds
   accounting against the new base;
4. uses same-offset new-span fallback slots for deleted/tombstoned old entries,
   whose replacement table is fully zero-initialized, so every retained buffer
   entry still references live storage; and
5. returns a distinct fail-closed result when no exact span/capacity contract is
   available, before any mutation occurs.

This keeps generation policy and barrier telemetry unchanged: pending values
remain pending and the moved live slot receives the rehash barrier.  It also
avoids a synchronous global drain in a container growth path.  Allocation,
owner-root registration, snapshot and mapping preparation remain outside the
commit tenure; exact owner/base/capacity/count/backend revalidation precedes the
first locked mutation.

## Update — 2026-08-24 dict/set rehash transaction confirmed

### CONFIRMED Proposal No.8 for dict/set; list growth remains open

C and strict dict/set rehash now allocate and zero replacement tables outside
the graph lock, keep moving owners in updateable registered roots, snapshot and
revalidate exact owner/base/capacity/count/backend state, then perform the raw
copy, pending-slot/span retarget, move barriers and owner publication in one
graph/no-park tenure.  Old raw bases are freed only after unlock.

The locked GC primitive validates exact old/new span bounds, updates medium and
global store-buffer slots, removes old remembered-page/zpage-card accounting,
retargets remembered slots and the payload span, then rebuilds accounting
against the new span.  It allocates, frees, decrefs, logs and safepoints nowhere
in its locked body.  Deleted/tombstoned pending slots map to same-offset zeroed
new-span slots, so every retained slot address stays live.  An absent prior span
is a distinct status: slot metadata is still retargeted and the caller registers
the published span before leaving the locked commit.

`[DENIED]` Draining the young insert edge before rehash is not a valid fix.  It
changes generation policy and erases the condition the move barrier must
preserve.  `[DENIED]` The historical "two enqueue" assertion is not the right
postcondition after exact retargeting.  The existing insert edge is moved to the
new slot, so the move barrier is correctly a duplicate: one pending edge remains,
all old slots disappear, and the exact new key slot is remembered.  Tests now
assert those direct facts in both runtimes.

A no-production-hook three-party pthread probe runs a real capacity-crossing
`py_set_add` while the mutator owns an outer recursive graph/no-park lease and a
collector waits on STW.  The collector cannot acquire before commit.  After
unlock it drains the retargeted edge, relocates the set, remaps/retires the
source, rewrites the root, reaches zero forwarding entries and preserves exact
contents/refcount.  C and strict are both green.

Backend0 uses separate C/strict refcount-only rehash helpers with no graph/root,
slot-pair or moving-GC work, preventing this correctness slice from taxing the
default stage2 rehash path.  Exact final-source commands, hashes and exclusions
are in
`docs/goal/evidence/2026-08-24-gc4-dict-set-rehash-transaction.md`.

The parent remains active.  `py_list.c::list_ensure_capacity` and strict
`py_list.py::_grow_if_needed` still call `realloc(items)` and publish the new
base without this transaction.  List growth is the next raw-base boundary;
constructor publication, C-API leases, callback roots, resurrection and
stale-candidate fairness also remain open.

## Proposal No.9 — list capacity-growth transaction

### Pre-registered design boundary

Replace list `realloc(items)` with allocate/copy/commit/free so the old base
cannot disappear before GC side-table slots are retargeted.  Preserve the
backend0 direct realloc fast path.  For backends 1-4:

1. retain a moving list through an updateable root, snapshot canonical owner,
   items/length/capacity/backend under graph, unlock, allocate and zero a new
   pointer array, then re-lock and revalidate;
2. copy exactly `length` healed items without incref/decref or callbacks and
   build `(old_slot,new_slot)` pairs;
3. invoke the proven GC4 locked mutator-payload retarget, emit move barriers,
   publish items/capacity under the same tenure, unlock, then free the old base
   and unregister the temporary root;
4. fail/retry on owner/base/length/capacity/backend drift before mutation, with
   no in-place realloc on moving backends; and
5. extend the real three-party C/strict probe to an actual capacity-crossing
   list append and verify pending-edge, relocation target, source retirement,
   item contents and exact refcounts.

List reads and multi-element mutations that cache `items` across callbacks or
unlocked loops are not silently claimed by capacity growth alone; inventory
them after this finite base-replacement slice.

## Update — 2026-08-24 list capacity growth confirmed

### CONFIRMED Proposal No.9 base replacement; retained arguments remain open

C and strict list growth now preserve backend0's direct `realloc` path and use
allocate/copy/revalidate/retarget/publish/free for backends 1-4.  Moving owners
remain in updateable roots across the unlocked allocation window; the helper
updates/returns the canonical owner.  The locked commit heals every live item,
builds exact old/new slot pairs, retargets pending buffer/remembered/page/card
and span metadata, emits move barriers, publishes the new base and unlocks
before freeing the old array.

A capacity-four OLD list with four pending young edges grows on the fifth
append.  C and strict observe no old remembered slot, five exact new slots,
pending count five, one new enqueue (copied edges are duplicates), and exact
contents after drain.  The real three-party STW test now covers list and set in
both runtimes: STW waits for commit, then direct relocation/remap retires the
source, rewrites the root and leaves exact contents/refcount with no forwarding
entry.  Final commands and hashes are in
`docs/goal/evidence/2026-08-24-gc4-list-capacity-growth-transaction.md`.

The parent remains active.  Growth now introduces a deliberate safepointable
planning window, so every managed input retained by its caller across that
window must be registered/reloaded explicitly.  Direct append/insert incoming
items, slice source, set-slice replacement, extend list/tuple source and strict
`_push_to_list` value are the first exact inventory.  Other list APIs that cache
`items` across `py_obj_eq`, iteration or comparison callbacks remain separate.

## Proposal No.10 — list retained-argument roots across growth

### Pre-registered design boundary

For every list operation that may call the new growth helper and then reuse a
managed input, register that input in an updateable temporary root before the
growth call, reload it after every safepoint/callback boundary, and unregister
only after the destination has acquired its owned reference or the operation
has failed cleanly.  Cover at least:

1. generic append and insert item;
2. list/tuple source in extend, including self-extend snapshot semantics;
3. source list in slice output growth and replacement sequence in set-slice;
4. strict `_push_to_list` value; and
5. failure paths for root-node allocation and growth OOM without leaked root,
   duplicate incref or stale forwarded shell.

Use no root handle on backend0 and nonmoving tracing backends.  C and strict
must reload canonical owner/source/item after growth, and a focused relocation
probe must move the retained input during the unlocked window rather than
merely checking source strings.  This proposal does not yet cover callbacks in
operations that do not grow (contains/index/count/sort); inventory those next.

## Update — 2026-08-24 list retained inputs confirmed

### CONFIRMED Proposal No.10 retained-root mechanics

C and strict append/insert root owner and item, extend roots destination and
list/tuple source, slice roots source/output, and strict `_push_to_list` roots
output/value.  C set-slice roots destination/replacement; the separately linked
strict `py_list_set_slice.py` now uses the common transaction-aware
`pcc_list_grow_for_mutation` export and roots/reloads the same inputs.  Root
handles are unregistered on normal and explicit allocation/error exits.

The element commit in non-backend0 append/insert/extend now takes the outer
graph/no-park lease before deriving raw slots, so the nested generic store
cannot park after slot computation.  Backend0 has an early direct branch and
does not allocate/register root handles or take that graph lease.

A C/strict probe forwards a source list first, then passes the stale shell to a
capacity-crossing append and extend.  Both store/read the target, preserve
contents and return temporary-root counts to baseline.  The real list/set
three-party matrix remains green.  Exact commands and hashes are in
`docs/goal/evidence/2026-08-24-gc4-list-retained-input-roots.md`.

The dynamic probe starts from an already-forwarded shell rather than adding a
production pause atomic inside malloc/planning.  This is sufficient for the
retained-input canonicalization claim and is paired with the independent
three-party STW overlap proof; it does not claim arbitrary callback revalidation.

## Proposal No.11 — non-growing list callback/raw-base inventory

### Pre-registered design boundary

Classify every remaining list function by whether it retains owner/items/slot
state across a callback-capable operation (`py_obj_eq`, `py_obj_next`, index
conversion, comparison/sort callable, `py_decref` finalizer, weakref or nested
GC).  At minimum inspect C and strict parity for:

- contains, index/range, count, remove and sort comparison/callable loops;
- pop, clear, delete-index/range/slice and set-slice decref tails;
- reverse and other multi-slot moves;
- copy/repeat/concat/source iteration loops; and
- iterator fallback paths whose iterator/result locals can move.

For callback-free raw moves, use short owner-rooted graph transactions and
defer decref/finalizer tails until unlock.  For callback-bearing loops, snapshot
only updateable roots/scalars, release graph before the callback, then reload and
revalidate owner base/length/revision before commit; restart or follow Python's
mutation semantics rather than using a stale index.  Preserve backend0 direct
paths and add one C/strict callback that triggers relocation/GC during the real
operation.  Do not bundle sort semantics with deletion/finalizer semantics if
either requires a distinct proposal.

### Pre-implementation split: Proposal No.11a callback-free core

First close only operations whose critical raw phase has no Python callback:

- get/load followed by owned retain;
- pop ownership transfer plus tail memmove;
- reverse's retained two-slot swap; and
- copy/repeat/concat source loads and destination stores.

For moving backends, keep owner/source/output in updateable roots and place the
raw load/retain or move/store/metadata update inside a short graph/no-park
transaction.  Retain/decref packets that could reach zero finish after unlock;
reverse's temporary retains must make its in-lock decrefs provably non-final.
Backend0 retains existing direct bodies.  Equality/removal/count/index,
clear/delete and sort remain Proposal No.11b+ because their callbacks and
mutation revalidation are materially different.

## Update — 2026-08-24 callback-free list core confirmed

### CONFIRMED Proposal No.11a

C and strict get/getitem now pair raw load with prepared retain under one graph
tenure; int getters convert before unlock.  Set/setitem use rooted owner/item
plus split store plans.  Pop registers an updateable result root before locked
ownership transfer/memmove.  Reverse commits two retained swaps under one short
tenure and finishes all tails after unlock.  Copy/repeat/concat use rooted
sources/output plus owned get/append instead of borrowed raw pointers crossing
safepoints.

The list review exposed and fixed a nested transaction defect: calling complete
`pcc_gc_store_ptr` while an outer list graph lock was held caused its finish
tail to run inside the still-owned outer lock.  New internal
`pcc_gc_store_ptr_plan_init` preserves owner logging outside; high-level list
operations call locked commit and post-unlock finish explicitly in both
runtimes.

Backend0 retains separate direct paths.  Stale forwarded-owner and
forwarded-source C/strict probes cover get/reverse/pop and
append/extend/copy/repeat/concat with exact contents and root-count cleanup.
Evidence is in
`docs/goal/evidence/2026-08-24-gc4-list-callback-free-transactions.md`.

## Proposal No.11b — equality callbacks versus destructive decref tails

### Pre-registered design boundary

Split the remaining list work into two mechanisms rather than hiding both under
one lock:

1. `contains`, `index`, `index_range`, `count` and the search phase of `remove`
   must root owner/query/current candidate, call `py_obj_eq` with graph unlocked,
   then reload and revalidate owner base/length/index/candidate identity before
   accepting the result or restarting from a Python-compatible position.
2. `remove` commit, clear, delete-index/range/slice and set-slice replacement
   must detach/move slots and publish length under graph/no-park while saving
   owned values in prepared cleanup packets; all decref/finalizer/weakref tails
   run only after unlock and reload roots after re-entry.

Sort/comparison-callable semantics remain a later Proposal No.11c.  Preserve
backend0 direct loops.  Add a C/strict equality callback that triggers
relocation or nested GC and a finalizer that inspects/mutates the same list;
source-only ordering is insufficient.

### CPython oracle correction before implementation

`[DENIED]` Candidate-identity revalidation is not correct for `list.remove`.
A CPython probe whose first candidate's `__eq__` inserts a new element at index
zero and returns true leaves the original candidate in the list: CPython removes
the *current element at the matched numeric index*, not the object that was
compared.  Therefore the callback-bearing search retains the compared candidate
only through the unlocked equality call, but a true result starts a fresh
owner-reloaded transaction that removes the then-current index.  Contains/index/
count likewise apply the equality result to the retained candidate, then advance
using freshly loaded current length/base rather than a stale snapshot.

## Update — 2026-08-24 list equality and remove confirmed

### CONFIRMED Proposal No.11b equality search and remove

C and strict `contains`, `index`, range-index, `count` and remove-search now
load and retain each candidate under graph/no-park, keep owner/query/candidate
in updateable roots, run `py_obj_eq` unlocked, and reload the owner and current
length after callback re-entry.  A true remove result starts a new transaction
and removes the then-current numeric index, implementing the CPython oracle
recorded above rather than the denied candidate-identity rule.  The removed
owned reference remains rooted until owner/query cleanup and is decref'd only
after unlock.

A real C-extension equality callback relocates the list, inserts at index zero
and returns true.  C and strict preserve the compared candidate and tail,
remove the newly inserted current-index element, and leak no temporary roots.
Ordinary backend0 list parity, strict closure, source/ABI contracts, and the
task relocation/retirement gate remain green.  Exact commands and hashes are
in `docs/goal/evidence/2026-08-24-gc4-list-equality-remove-transaction.md`.

The parent remains active.  Proposal No.11b next covers callback-free
structural detach/publish for clear, delete-index/range/slice and set-slice,
with every saved-reference decref/finalizer/weakref tail after unlock and a
same-list finalizer re-entry probe.  Sort/comparison callable behavior remains
separate Proposal No.11c.

### Pre-implementation split: Proposal No.11b.1 clear and delete-slice

Close clear and slice deletion before replacement-bearing set-slice.  For
backend0 preserve the direct loops.  For moving/tracing backends, root the
owner, allocate and initialize one split store/decref packet per removed slot
before graph tenure, revalidate the current length after locking, detach every
removed ownership and compact/publish length while locked, then finish every
packet after unlock.  For non-contiguous deletion, retarget Backend-4 pending
raw-slot metadata from each moved source slot to its compacted destination and
emit the normal slot barrier for moved destinations.

The dynamic proof must use a real C-extension deallocator whose last reference
is released by `clear`: it re-enters the same list, relocates it, and appends an
element.  The callback must observe the already-published empty list and its
append must survive in both C and strict runtimes without a leaked temporary
root.  Set-slice remains Proposal No.11b.2 because replacement aliasing and
borrowed replacement slots require a snapshot/ownership design beyond pure
deletion.

Execute this as two independently gated changes.  Proposal No.11b.1a changes
`clear` only and proves the same-list finalizer boundary.  Delete-slice remains
No.11b.1b because arbitrary slice-bound `__index__` conversion is itself a
callback phase: convert each rooted bound exactly once, then normalize against
the owner length reloaded after conversion.  Do not label deletion
"callback-free" by overlooking that conversion surface.

The first strict clear probe exposed a distinct predecessor bug: strict
`_py_decref_prepare` rejects dynamic C-extension tags that the C owner accepts,
so no C-extension deallocator can run from a strict split-store tail.  That
parity repair is tracked independently in
`docs/investigations/strict-cext-dynamic-tag-decref-parity.md`; finish it before
claiming the C/strict clear finalizer proof.

### Probe correction after strict C-extension ownership diagnosis

`[DENIED]` A dynamic C-extension deallocator cannot currently serve as the
strict mirror proof for list clear.  Instrumentation showed its strict object
is registry-tagged but unmanaged/unknown and list append does not retain it;
mirroring only the C tag guard was tested and denied.  That independent C-API
ownership lifecycle remains in
`strict-cext-dynamic-tag-decref-parity.md` and no unsafe tag-only acceptance is
left in source.

The list transaction proof instead uses a native pcc class instance with a
real `__del__` method, the finalizer surface already supported and gated in C
and strict runtimes.  It observes published length zero, explicitly relocates
the same list, appends `777`, and leaves that append visible with exact root
cleanup.  This substitution changes only the callback object kind; it still
exercises the split-store terminal decref/finalizer tail that the clear
transaction must defer until unlock.

## Update — 2026-08-24 list clear confirmed

### CONFIRMED Proposal No.11b.1a

C and strict preserve backend0's direct clear path and give backends 1-4 an
owner-rooted split-store transaction.  All per-slot plans initialize before
graph tenure; locked commit detaches every ownership and publishes length zero;
plan finish runs after unlock.  A native `__del__` observes the empty list,
relocates the same GC4 owner, appends `777`, and C/strict retain the callback's
mutation without temporary-root leakage.

The C-extension probe correction and denied tag-only fix remain recorded above
and in their independent investigation.  Exact commands and hashes are in
`docs/goal/evidence/2026-08-24-gc4-list-clear-split-decref.md`.

The next finite slice is No.11b.1b delete-slice.  Its lo/hi/step objects and
owner must be updateably rooted while each `__index__` conversion runs exactly
once; only after conversion may current owner length normalize the scalar
bounds.  Compaction/publish and raw-slot metadata retargeting remain a separate
locked phase, with all removed-reference tails after unlock.

## Update — 2026-08-24 delete-slice confirmed

### CONFIRMED Proposal No.11b.1b

C and strict now root owner/step/lo/hi, invoke each bound conversion once while
unlocked, and normalize the resulting scalars against current length after the
callbacks.  Backend0 stays direct.  Backends 1-4 preallocate a mask, split
decref plans and slot pairs; locked commit retargets GC4 compacted slots,
detaches removed ownership, compacts, clears the tail and publishes length;
all plan tails finish after unlock.

The first high-level probe was red with an unchanged list and zero `__index__`
calls, exposing the strict mirror's raw integer conversion.  The final probe
matches CPython under same-list callback append and covers positive and
negative extended steps.  A C/strict native finalizer observes compacted state,
relocates the list and appends successfully with no old-address/root leak.
Exact commands and hashes are in
`docs/goal/evidence/2026-08-24-gc4-list-delete-slice-transaction.md`.

The next slice is No.11b.2 set-slice.  It must snapshot replacement ownership
before mutating the destination (especially `lst is replacement` and overlapping
list aliases), convert bounds exactly once, grow/revalidate without stale raw
bases, and commit old detach plus replacement stores and metadata retargeting
under graph tenure with every decref tail after unlock.  Extended-slice length
mismatch must fail before any mutation.

## Update — 2026-08-24 set-slice confirmed

### CONFIRMED Proposal No.11b.2

C and strict root destination/replacement/bounds, convert each bound once,
snapshot replacement after callbacks, and rebuild the final items payload in a
fresh buffer.  Backend0 publishes that buffer before direct old decrefs.
Backends 1-4 retarget GC4 old/new slot metadata, retain final values, detach all
old ownership and publish base/capacity/length under one graph tenure; all
retain/decref tails and old-buffer free occur after unlock.

The initial red showed both independent defects: self assignment read its own
mutating destination and emitted zeroed elements, while strict custom bounds
were never invoked.  Final high-level coverage matches CPython for self alias,
same-list bound callbacks, tuple replacement, empty-range insertion and
positive/negative extended assignment.  A C/strict finalizer relocates and
re-enters the published list, while an extended length mismatch proves zero
mutation.  Exact commands and hashes are in
`docs/goal/evidence/2026-08-24-gc4-list-set-slice-transaction.md`.

Proposal No.11c is next: inventory every sort path (runtime compare,
`__lt__`, key callable, reverse post-pass) and separate comparison/key callback
phases from raw list publication.  Owner, candidate/key and temporary sorted
storage must remain updateably rooted across callbacks; mutation-during-sort
must follow Python-compatible failure/visibility rather than continue through a
stale items base.  Preserve backend0's direct nonmoving path where safe.

## Update — 2026-08-24 frontend list sort confirmed

### CONFIRMED Proposal No.11c frontend-owned paths

All frontend `list.sort` branches now copy, clear the receiver, sort the hidden
working list, detect callback mutation, atomically publish through set-slice,
and raise Python-compatible `ValueError` after publishing sorted originals.
Custom static `__lt__`, key callables, ordinary runtime order and reverse share
the same begin/finish transaction.  GC4 custom comparison and key callbacks
that append to the receiver both produce the exact CPython error and final
sorted original list; the full key/custom/primitive neighborhood, bootstrap
baseline and fallback ratchets are green.

`[DENIED]` Wrapping the working list in a new LIFO temp root across custom
`__lt__` was rejected by the self-backend precise stack-map verifier: the
comparison exception edge reached `try.err` with different managed-root state.
The root was removed; generated code retains only an owned handle and invokes
transactional list APIs for every access, so it does not keep a raw list base
through callbacks.  No stack-map invariant was weakened.  Exact commands and
hashes are in
`docs/goal/evidence/2026-08-24-gc4-list-sort-callback-transaction.md`.

The general `py_obj_sorted` iterator/C-extension comparator internals are not
silently included in this claim; they remain in the callbacks-beyond-list
inventory.  The next parent slice returns to the task-card order: constructor
publication for every movable supported tag, then C-API raw-view leases and
non-list callback roots.

## Proposal No.12 — constructor publication

### Pre-registered design boundary

Backend 4's selector already excludes `PY_FLAG_GC_FRESH_ALLOC`, but C and
strict allocators do not set that flag for Backend 4.  Introduce one symmetric
publication ABI that clears fresh only under graph/no-park after constructor
payload, roots and raw-span metadata are complete.  Migrate supported tags in
finite families; do not mark an unmigrated family fresh indefinitely merely to
make a race disappear.

### Proposal No.12a — list/dict/set/tuple containers

First mark only list, dict, set and tuple allocations fresh in Backend 4.
List/dict/set publish at the end of their empty-container constructors after
raw storage and owner payload spans exist.  Tuple has a different contract:
`py_tuple_new(n)` intentionally returns zero slots for caller fill, so a
non-empty tuple stays fresh until `py_tuple_set_item` observes every slot
initialized; an empty tuple publishes immediately.  C and strict must use the
same flag/publish ABI.

The focused C/strict probe must show: raw allocation and partially filled tuple
are selector-ineligible; the final tuple store publishes; completed empty
list/dict/set/tuple constructors are not fresh and become selectable; no
production pause/test hook is added.  Property/function/iterator/frame/task/
instance/scalar and other supported tags remain explicit No.12b+ inventory.

## Update — 2026-08-24 container constructor publication confirmed

### CONFIRMED Proposal No.12a

C and strict allocator owners mark only list/dict/set/tuple fresh on Backend 4.
One graph-locked publication ABI clears the flag.  List/dict/set publish after
raw storage and spans; empty tuple publishes immediately, while non-empty tuple
scans after each construction store and publishes only when every slot is
filled.  Direct relocation add now rejects fresh in both mirrors as selectors
already did.

The C/strict probe proves raw and partial rejection, final tuple publication,
and admission of completed container constructors.  Existing concurrent object
and page selector handoff tests, source/ABI neighbors and the task retirement
gate are green.  Exact commands and hashes are in
`docs/goal/evidence/2026-08-24-gc4-container-constructor-publication.md`.

No.12b must inventory the remaining supported tags by constructor family.
Start with small fixed-layout wrapper constructors (property, classmethod,
staticmethod, weakref) before function/iterator/frame/task/class/instance and
raw-payload scalar families.  Add a tag to the allocator fresh set only in the
same change that publishes every constructor for that tag.

## Update — 2026-08-24 wrapper constructor publication confirmed

### CONFIRMED Proposal No.12b property/classmethod/weakref

C and strict now mark property, classmethod and weakref fresh on Backend 4 and
publish at the end of their unique constructors after all pointer fields,
tracking/list links and logging are complete.  Raw allocations reject direct
relocation admission; completed constructors admit.  Container and concurrent
selector neighbors plus the task retirement gate remain green.

Staticmethod is an explicit non-change: the runtime has layout/visitor support
but no public constructor because lowering returns the wrapped callable.  It is
not placed in the fresh set without a publication owner.  Exact commands and
hashes are in
`docs/goal/evidence/2026-08-24-gc4-wrapper-constructor-publication.md`.

No.12c should take function and iterator together only if their C/strict
constructor sets are one-to-one; otherwise split.  Generator/coroutine/
continuation/task, class/instance and scalar/raw-payload families remain later.

## Update — 2026-08-24 function/iterator constructor publication blocked

### DENIED Proposal No.12c tag admission before allocator support

Function has one allocation owner and iterator has two, but the strict GC4
runtime cannot yet allocate either tag through `pcc_gc_alloc`: the focused
probe enters the strict allocator, its object allocation returns NULL, and the
MemoryError path recursively calls `py_exc_new -> py_exc_builtin_class ->
py_class_new -> pcc_gc_alloc` until stack overflow.  LLDB localized the first
failure before either real constructor.  Changing raw probe size did not alter
the mechanism.  This is the **strict GC4 FUNC/ITER allocation blocker**, not a
publication-order failure.

The candidate fresh flags, iterator barrier-store conversion and constructor
publish calls were removed from C and strict source.  FUNC/ITER stay out of the
fresh admission set until their strict GC4 allocation/MemoryError bootstrap
boundary is repaired and gated independently.  The skipped dynamic publication
node names this active blocker; a static contract prevents accidental tag-only
admission.  Other constructor families may proceed without claiming these two.

### DENIED Proposal No.12d suspended-execution tag admission

GEN/COROUTINE/CONTINUATION/TASK formal constructors all pass in strict GC4
before fresh admission.  Adding those tags plus end-of-constructor publication
made the first ordinary allocation fail through the same recursive MemoryError
path.  Extracting the growing tag predicate into a closure-safe early-return
helper compiled but did not change the runtime failure.  This is the **strict
GC4 suspended-execution fresh-admission blocker**: the interaction occurs in
allocator/registration state before constructor publication can be evaluated.

All four fresh tags and C/strict publish calls, plus the predicate-helper
experiment, were removed.  The preflight remains evidence that the constructors
work without admission; the dynamic publication node is skipped with the exact
blocker reason and a static contract keeps all four tags absent.  Do not retry
another tag family until this allocator/fresh-admission interaction is isolated
with a smaller allocator-level probe.

## Proposal No.13 — C-API raw-view and borrowed-result lifetimes

### Proposal No.13a — unpaired borrowed container items

`PyTuple_GetItem`, `PyList_GetItem`, `PyDict_GetItem` and
`PyDict_GetItemWithError` return borrowed object pointers with no release API.
The current C/oracle/strict owners acquire an internal owned reference, drop it,
and return the pointer without preventing Backend-4 relocation.  Pin the live
item before dropping the temporary ownership so its address remains stable for
the CPython borrowed lifetime.  This does not extend validity after the owner
is mutated or destroyed; pinning is movement stability, not ownership.

`PyList_GetItemRef` is an owned API and must call the owned runtime getter
directly instead of going through the newly pinned borrowed wrapper.  Dict Ref
APIs already do.  Prove C/oracle/strict source parity and a C/strict probe where
borrowed list/tuple/dict results become pinned and direct relocation admission
rejects them, while GetItemRef remains owned without setting the pin bit.
Sequence raw arrays, unicode/bytes pointers and counted Py_buffer leases remain
No.13b+.

## Update — 2026-08-24 borrowed C-API items confirmed

### CONFIRMED Proposal No.13a

C production/oracle and strict owners pin tuple/list/dict borrowed results before
dropping the temporary owned reference.  `PyList_GetItemRef` now uses the owned
getter directly and remains unpinned.  Direct relocation add also rejects
PINNED in both C add implementations and strict, matching normal selectors.

The C/strict probe proves exact values, pin flags and relocation rejection for
all borrowed APIs, plus owned/unpinned GetItemRef admission.  Source/ABI
neighbors and the task retirement gate remain green.  Exact commands and hashes
are in `docs/goal/evidence/2026-08-24-gc4-capi-borrowed-item-pins.md`.

No.13b should handle `PySequence_Fast_ITEMS`/GET_ITEM owner storage first,
then unicode/bytes unpaired raw pointers.  Counted `PyObject_GetBuffer` /
`PyBuffer_Release` requires a nested lease count and remains a separate slice.

## Update — 2026-08-24 PySequence_Fast owner storage confirmed

### CONFIRMED Proposal No.13b

C production/oracle and strict `PySequence_Fast_ITEMS` pin the exact fast owner
before returning tuple inline or list out-of-line item storage.  C/strict probes
observe exact bases/data, owner pins and direct relocation rejection.  Source/
ABI neighbors and the task retirement gate remain green.  Exact commands and
hashes are in
`docs/goal/evidence/2026-08-24-gc4-capi-sequence-fast-owner-pins.md`.

No.13c is unicode/bytes raw access (`PyUnicode_DATA`,
`PyUnicode_1BYTE_DATA`, UTF-8 and bytes pointer exports).  Identify which are
macros over runtime accessors and pin their owner at the unique accessor seam.
Keep counted Py_buffer leases separate.

## Update — 2026-08-24 unicode/bytes raw pointers confirmed

### CONFIRMED Proposal No.13c

A dedicated C/strict `pcc_capi_str_utf8_pinned` ABI owns C-API unicode raw
views; fake-header DATA macros and PyUnicode_AsUTF8 APIs route through it.
Internal `py_str_utf8` remains unpinned.  Bytes C-API accessors pin directly.
C/strict probes prove exact data, C-API owner pin/rejection and internal string
non-pin/admission.  ABI chunks, source neighbors and the task gate are green.
Exact evidence is in
`docs/goal/evidence/2026-08-24-gc4-capi-unicode-bytes-owner-pins.md`.

No.13d is the counted `PyObject_GetBuffer` / `PyBuffer_Release` contract.  A
single pin bit is insufficient for nested views, so add a per-owner lease count
whose 0->1 transition pins and final 1->0 release unpins; memoryview forwarding
must preserve/retarget the count owner.

## Update — 2026-08-24 nested buffer leases confirmed

### CONFIRMED Proposal No.13d

Each C/oracle/strict Py_buffer now owns an active-list metadata node recording
the final exporter and original view owner.  GetBuffer links/counts under graph
lock and first-owner pins; Release unlinks then final-owner unpins before public
view cleanup.  Memoryview chains pin both wrapper and final bytes/bytearray.
C/strict two-view probes prove intermediate release retains both pins and final
release restores relocation admission.  Exact evidence is in
`docs/goal/evidence/2026-08-24-gc4-capi-nested-buffer-leases.md`.

The task-card C-API raw-view requirement is now closed across borrowed items,
PySequence_Fast arrays, unicode/bytes raw pointers and counted buffers.  Return
to callbacks beyond list (`py_obj_sorted` opaque iterator/C-extension paths and
other native callbacks), while constructor fresh admission remains separately
blocked in strict.

## Proposal No.14 — callbacks beyond list

### DENIED py_obj_sorted mutable-list pin proposal

Pinning input/output/iterator/scratch once per sort looked like an O(1) way to
stabilize raw merge bases.  C/strict syntax and focused pin-balance probes
passed, as did ordinary port/CC sorting and backend 3.  Backend 4's existing
500-item merge gate instead failed immediately with
`MemoryError: list append: out of memory`.  Thus pinning mutable working lists
changes their Backend-4 append/growth behavior and is not a safe callback
transaction.

All py_obj_sorted pin/unpin edits and the GC4 override added to the custom
iterator neighbor were removed.  A static denial contract keeps this exact
shape absent.  A successor must use updateable roots and reload list/raw bases
after callbacks, or sort into nonmoving raw storage with explicit GC-visible
element roots; it must not pin the mutable ping-pong lists.

## Update — 2026-08-24 prior DENIED attribution overturned

### CONFIRMED Proposal No.14 py_obj_sorted callback-owner pins

The exact candidate-off control reproduced the 500-item failure, and an LLDB
breakpoint proved `py_obj_sorted` had not been entered.  A smaller Backend-4
list-growth probe localized the failure to append index 256: the 4096-byte raw
array no longer fit the owner's zpage and payload-span retargeting rejected the
growth.  This was an independent external-span defect, so the prior run remains
valid failure evidence but its attribution to mutable-list pinning is denied.

C and strict span metadata now represent out-of-page raw payloads with
`offset_bytes == -1`, retain owner/base/size and global slot mappings, and omit
those spans from internal zpage used/allocated accounting.  Both 500-item list
growth probes pass, and the candidate-off Backend-3/4 merge passes.

The identical O(1) pin candidate was then reapplied.  Input/output/iterator/
strict-dict-keys/scratch owners are pinned across callback-capable operations
and balanced on every return.  Backend-3/4 merge, C/strict balance probes and
the GC4 custom iterator pass 5/5; source/ABI neighbors pass 32/32 and the task
retirement gate passes 24/24.  Exact logs and identities are in:

- `docs/goal/evidence/2026-08-24-gc4-external-payload-span-growth.md`
- `docs/goal/evidence/2026-08-24-gc4-py-obj-sorted-callback-pins.md`

## Proposal No.14a — shared-input root correction

The confirmed O(1) pins exposed a narrower ownership problem during the next
callback audit: `pcc_gc_pin`/`pcc_gc_unpin` are a boolean flag contract, not a
nested lease.  Unconditionally pinning then unpinning a caller-visible input or
iterator can clear a pre-existing C-API pin; a self-iterator also aliases `it`
and `x`.  Do not make the global pin ABI implicitly counted in this slice.

Replace the caller-visible input and iterator pins with scheduler-root handles
for moving backends and reload them after callback-capable length/iteration
calls.  The output, strict dict-key snapshot and scratch lists are fresh,
unpublished working objects and may retain their constant-cost movement pins.
After each comparison callback, discard the pre-callback element pointers and
reload the selected element from the pinned source-list slot before appending.

The focused red/green contract is: an already-pinned input remains pinned after
`py_obj_sorted`; static C/strict source uses updateable handles for input and
iterator; Backend-3/4 merge and the GC4 custom self-iterator remain green.  This
is a correction to Proposal No.14, not a claim for other callback families.

## Update — 2026-08-24 shared-input roots confirmed

### CONFIRMED Proposal No.14a

C and strict `py_obj_sorted` now register input and iterator locals as
updateable scheduler roots on Backend 3/4 and reload them after length and
iteration callbacks.  The old shared-object pin/unpin pairs are absent, so a
pre-existing caller pin survives the call.  Fresh unpublished out/keys/scratch
worklists retain constant-count pins, and merge comparisons reload the selected
element from its current source slot after callback return.

The current source passes merge 1/1, static/C/strict/custom-iterator 4/4,
strict archive ownership 1/1, ABI/GC neighbors 17/17 and the task retirement
gate 24/24.  Exact commands, logs and hashes are in
`docs/goal/evidence/2026-08-24-gc4-py-obj-sorted-root-correction.md`.

No.14's initial pin evidence remains historical, but No.14a supersedes its
shared input/iterator mechanism.  Continue the callback inventory with
`py_obj_min_max`, enumerate/iterator helpers, tuple membership and dict/set
hash/equality paths; choose one finite slice at a time.

## Proposal No.15 — py_obj_min_max callback roots

`py_obj_min_max` retains three managed pointers across callbacks: the iterator
across every `py_obj_next`, the current best across both iteration and
comparison, and the new element across `py_obj_lt`.  C and strict currently
keep only raw locals, so Backend-4 remap can leave a forwarding shell that is
later compared or decref'd.

Register iterator, best and element storage as updateable scheduler roots on
Backend 3/4.  Reload iterator/best after `py_obj_next`; reload best/element
after `py_obj_lt`; when replacing the best, swap the rooted storage/handle
pairs and release the old best only after its handle is detached.  Every empty,
error and success return must leave the scheduler-root count balanced.

The finite claim covers the `py_obj_min_max` caller.  It does not close the
callable-iterator state machine inside `py_obj_next`, `enumerate`, tuple scans,
or dict/set hash/equality loops; those remain later proposals.

## Update — 2026-08-24 py_obj_min_max roots confirmed

### CONFIRMED Proposal No.15 under current callback semantics

C and strict `py_obj_min_max` now use updateable iterator/best/element slots,
reload after iteration/comparison calls, swap rooted slot/handle pairs on
replacement, and balance every exit.  C/strict heap-string probes return the
right extrema with zero leaked roots; a Backend-4 custom iterator re-enters
`gc.collect()` from every `__next__` and still returns min/max `1/3`.  Existing
semantic neighbors pass 5/5, strict owner 1/1, ABI/GC 17/17 and the task gate
24/24.  Exact evidence is in
`docs/goal/evidence/2026-08-24-gc4-py-obj-min-max-callback-roots.md`.

### DENIED ordinary movable comparison callback proof

Three controls show why no dynamic ordinary-instance comparison callback is
claimed.  A custom iterator returning user instances fails self IR type joining
(`void *` result receives i64); strings compile but return `0/0` through the
same i64 accumulator; and a direct runtime user class with a native `__lt__`
returns pointer-order `3/2` with callback hit count zero.  Current runtime
`py_obj_lt` does not dispatch ordinary user-instance `__lt__`, while its
C-extension callback operands are nonmoving.  The failed temporary probe was
removed.  Do not repeat it as a GC-root experiment without first changing and
separately gating the comparison capability boundary.

Continue with the callable-iterator state machine in `py_obj_next` and eager
`py_enumerate_list`, which both keep managed locals across real callbacks.

## Proposal No.16 — eager enumerate callback roots

`py_enumerate_list` retains its iterator and private output across every
`py_obj_next` callback, then retains the returned item while allocating and
publishing an index/tuple pair.  Root the caller-visible iterator and item (and
the possibly boxed index) with updateable Backend-3/4 scheduler handles.  Pin
only the fresh unpublished output and pair, balance every error/success path,
and reload rooted values immediately before tuple stores or decrefs.

The focused proof is C/strict heap-item result/root-count parity plus a real
Backend-4 value-position enumerate whose custom `__next__` re-enters
`gc.collect()`.  This proposal does not modify or claim the callable-iterator
state machine inside `py_obj_next`.

## Update — 2026-08-24 eager enumerate roots confirmed

### CONFIRMED Proposal No.16

C and strict eager enumerate now root/reload iterator, item and boxed index,
pin only fresh private output/pair objects, and balance all cleanup paths.
C/strict heap-string probes produce exact index/value tuples with zero leaked
roots; a Backend-4 custom iterator re-enters `gc.collect()` on every next call
and produces the expected `[(5, 7), (6, 8), (7, 9)]`.  Semantics pass 3/3,
strict owner 1/1, ABI/GC 17/17 and task retirement 24/24.  Exact evidence is in
`docs/goal/evidence/2026-08-24-gc4-py-enumerate-callback-roots.md`.

The callable-iterator branch of `py_obj_next` remains next: it retains iterator,
callable, sentinel, args and result across call/equality callbacks and then
writes iterator state.  Do not infer it from eager-enumerate caller rooting.

## Proposal No.17 — py_obj_next internal iterator roots

For an internal `PY_TYPE_ITER`, register the iterator before any sequence
lookup or callable-iterator callback.  The sequence path reloads it before
incrementing index and roots a heap item until that write completes.  The
callable path roots callable, sentinel, args and result; it reloads after
`py_obj_call` and `py_obj_eq`, and writes DONE only through the current iterator
target after equality re-entry.

The dynamic C/strict proof builds `iter(callable, sentinel)` from runtime
objects.  Its ordinary user `__eq__` callback directly relocates result,
sentinel and iterator during the real sentinel comparison.  The returned first
item, second-call StopIteration, DONE replay, callback counts and residual root
count must all match.  This proposal does not include tuple membership or
dict/set hash/equality callers.

### Design correction before verdict

The ordinary-user `__eq__` control returned a valid first item but recorded
zero equality callbacks, matching the same runtime comparison boundary found
under Proposal No.15.  It did not exercise relocation.  The replacement proof
uses the supported C-extension `tp_richcompare` path: result and sentinel are
nonmoving C-extension objects, and their equality callback directly relocates
the movable iterator.  Therefore the dynamic claim is iterator reload/DONE
state plus result/sentinel root lifetime and balance; it does not claim those
nonmoving operands relocated.

## Update — 2026-08-24 internal iterator roots confirmed

### CONFIRMED Proposal No.17

C and strict internal sequence/callable iterator paths now root every retained
managed local, reload after call/equality/lookup, and update index/DONE only on
the current iterator target.  In both mirrors a real C-extension equality
callback directly relocates the iterator; first result, sentinel StopIteration,
DONE replay, exact call/equality counts and root balance all pass.  Iterator
semantics pass 15/15, strict owner 1/1, ABI/GC 17/17 and task retirement 24/24.
Exact evidence is in
`docs/goal/evidence/2026-08-24-gc4-py-obj-next-callable-roots.md`.

Continue with tuple membership/count/index and dict/set hash/equality caller
loops.  Their raw owner/base and retained-key/value lifetimes are independent
of the now-correct iterator state machine.

## Proposal No.18 — rooted tuple equality scans

Unify tuple count/index/index-range behind one C/strict rooted scan.  Register
the tuple owner and query, acquire each element as the existing owned
`py_tuple_get` result, root it across `py_obj_eq`, reload all surviving values,
then detach and decref the element.  This also repairs the C oracle's missing
decref per element.  A C-extension equality callback directly relocates the
tuple during the real scan; count/index/range results and root balance must
remain exact.

## Update — 2026-08-24 tuple equality scans confirmed

### CONFIRMED Proposal No.18

C and strict tuple methods share one rooted callback scan and C now releases
every owned getter result.  A real C-extension equality callback relocates the
tuple; both mirrors retain exact count/index/range results and balanced roots.
Semantics pass 2/2, strict owner 1/1, ABI/GC 17/17 and task retirement 24/24.
Exact evidence is in
`docs/goal/evidence/2026-08-24-gc4-tuple-method-callback-roots.md`.

Continue with dict/set hash/equality callers.  Rehash base replacement is
already transactional, but ordinary lookup/contains/delete scans still compute
user hash/equality outside or across table state and require a separate
retained-root/reload audit.

## Proposal No.19 — rooted dict read lookup

Limit this slice to `py_dict_get` and `py_dict_contains`.  Root owner/query
before user hash, then probe through a read-only helper.  Retain and root an
entry key across user equality; after callback reload owner/query/candidate and
restart from the hash origin if owner identity, indices, entries, capacity or
the current probe slot changed.  Return an owned value before releasing roots;
contains reuses get and releases that value.  Set/update/delete remain separate
mutation proposals.

## Update — 2026-08-24 dict read lookup confirmed

### CONFIRMED Proposal No.19

C and strict dict get/contains now root before hash, retain/root equality
candidates and restart on owner/table/slot drift.  Ordinary `__hash__` and
C-extension equality callbacks each directly relocate a different dict;
both mirrors return exact values and later contains succeeds with balanced
roots.  Semantics pass 4/4, strict owner 1/1, ABI/GC 17/17 and task retirement
24/24.  Exact evidence is in
`docs/goal/evidence/2026-08-24-gc4-dict-read-callback-roots.md`.

Dict set/update and delete remain separate: set must preserve insert/update
commit semantics after callback restart; delete must detach key/value/table
state before any decref/finalizer tail.  Set lookup/mutation remains open too.

## Proposal No.20 — rooted set contains lookup

Limit this slice to `py_set_contains`.  Root set/item before user hash; retain
and root each equality candidate; after callback reload and restart if owner,
entries, capacity or current slot/key changed.  Prove ordinary `__hash__` and
C-extension equality callbacks can each relocate a set while contains remains
true and roots balance.  Add/remove/update remain separate mutation slices.

## Update — 2026-08-25 set contains confirmed

### CONFIRMED Proposal No.20

C and strict set membership now root before hash, retain/root equality
candidates and restart on owner/table/slot drift.  Ordinary `__hash__` and
C-extension equality callbacks each directly relocate a set; membership and
root balance remain exact.  Semantics pass 1/1, strict owner 1/1, ABI/GC 17/17
and task retirement 24/24.  Exact evidence is in
`docs/goal/evidence/2026-08-25-gc4-set-contains-callback-roots.md`.

Set add/remove/update and dict set/delete remain open mutation paths.  Their
callback-free commit and deferred-decref requirements cannot be inferred from
read-only membership.

## Proposal No.21 — set remove split commit

Extend rooted set lookup with remove mode.  After a stable match, initialize a
store plan, reacquire graph lock, revalidate owner/table/slot, commit key->dummy
and size-- together, unlock, then finish the plan.  A C-extension equality
callback first relocates the set; the removed stored key's final deallocator
must then observe length zero and membership false.

## Update — 2026-08-25 set remove target commit confirmed

### CONFIRMED Proposal No.21 target-side commit; strict source release open

C and strict set remove share rooted/restartable lookup and publish tombstone +
size under one graph-lock store-plan commit before finish/decref.  Both mirrors
survive equality relocation, restart exactly once, expose absence, complete two
retirement epochs and balance roots.  C's final managed dealloc observes the
committed absence.

The probe also exposed and fixed strict C-API flag drift: strict used
`0x1000000`, while public fake Python.h and C use `1<<62`.  Direct strict
managed-dealloc ABI control now passes.  However strict forwarding retirement
still leaves the stored C-extension key dealloc count at zero after two epochs,
while C reports one.  This exact remaining gap belongs to
`GC-P0-FORWARDED-SOURCE-PAYLOAD-RETIREMENT`; do not retry flag or hook hypotheses.
Exact evidence is in
`docs/goal/evidence/2026-08-25-gc4-set-remove-split-commit.md`.

Dict set/delete and set add/update remain open mutation paths in this parent.

## Update — 2026-08-25 parent quiescence boundary closed and successors routed

### RESOLVED parent task; successor correctness tasks remain

The original parent exit criteria are now met: phase/access ordering is shared,
true-pthread raw list/dict/set access cannot overlap copy/retirement, callback
holders are split/revalidated, and the current relocation/retirement gate is
24/24 green.  The long audit also discovered adjacent API-level work that must
not keep expanding this phase-ownership parent indefinitely.

Route strict forwarding-source C-extension release to the existing
`GC-P0-FORWARDED-SOURCE-PAYLOAD-RETIREMENT`, resurrection metadata to
`GC-P0-LAST-DECREF-RESURRECTION-METADATA-RESTORE`, and dict set/delete plus set
add/update callback commit ordering to the new
`GC-P0-CONTAINER-CALLBACK-MUTATION-COMMIT`.  Exact closure evidence is in
`docs/goal/evidence/2026-08-25-gc4-relocation-mutator-quiescence-closure.md`.

This resolves only the parent quiescence boundary.  It does not authorize a
stage, fixed-point or five-GC claim while the successor P0s remain unfinished.

## Proposal No.22 — set add/update rooted commit and source snapshot

Extend the shared rooted set lookup with add mode.  After hash/equality and any
restart, initialize a store plan, reacquire the graph lock, revalidate the
current owner/table/slot, and commit key/hash/size/fill together.  Set update
must snapshot its source before destination callbacks, then retain/root each
snapshot key across add so callback relocation or source mutation cannot leave
raw table state live across the call.

## Update — 2026-08-25 set add/update confirmed

### CONFIRMED Proposal No.22

C and strict set add now share the rooted/restartable lookup and publish the
inserted key plus hash/size/fill through one graph-locked store-plan commit.
Set update roots destination/source/snapshot, snapshots before destination
callbacks, and roots every owned list result across add/decref.

The first dynamic control incorrectly expected fake C-extension `tp_hash` to
be the invoked user callback; it observed a correct copy with zero callback
count and was replaced rather than treated as runtime evidence.  The final
control uses pcc-native `__hash__` for add relocation and C-extension equality
for duplicate-add/update relocation plus source mutation.  Both C and strict
mirrors pass, and contains/remove neighbors plus set method parity are 18/18
green.  Exact evidence is in
`docs/goal/evidence/2026-08-25-set-add-update-callback-commit.md`.

The successor task remains active because dict set/update/delete has not yet
received the same rooted commit and deferred-cleanup treatment.  No Stage1,
Stage2, fixed-point, five-GC, or performance claim follows from this slice.

## Proposal No.23 — rooted dict set/update commit

Extend the proven rooted dict read probe with a set mode.  Root owner, key and
value before user `__hash__`; revalidate indices/entries/capacity/entries_used
and the current probe slot after every hash/equality callback and restart from
the hash origin on drift.  A fresh insert publishes key, value, `entries_used`,
`indices[slot]` and `size` under one graph lock built from two store plans (a
plan commits exactly one slot); a replacement publishes only the value slot and
must keep the original stored key object.  Both finish their plans after
unlock, so a displaced value's finalizer observes only committed state.  Dict
delete is deliberately excluded and remains a separate proposal.

## Update — 2026-08-25 dict set/update confirmed

### CONFIRMED Proposal No.23

C and strict dict set now share `py_dict_rooted_op` / `_dict_rooted_op` with
the read path and commit through `py_dict_insert_rooted_slot` /
`py_dict_replace_value_rooted_slot` and their strict mirrors.  A pcc-native
`__hash__` relocates an empty dict during insert, a C-extension
`tp_richcompare` relocates it during replacement, and a pcc-native `__del__` on
the displaced value re-enters the dict.  In both mirrors length, value, stored
key identity, callback counts and scheduler-root balance are exact.  Semantics
pass 19/19 for the dict/set substrate slice and 23/23 for dict+set parity.
The now-dead `py_dict_insert_fresh` / `_insert_fresh` were removed from both
mirrors.  Exact evidence is in
`docs/goal/evidence/2026-08-25-dict-set-callback-commit.md`.

Two defects were found during the slice and are worth recording because
neither was visible from the C mirror alone:

- `pcc/py_runtime/py/py_dict.py` had never declared the three
  `pcc_gc_store_ptr_plan_*` externs that `py_set.py` already had.  The missing
  names became a runtime NameError inside `py_dict_set`, so the strict mirror
  silently inserted nothing and left an exception pending (`len=0`, `err=1`)
  while C was green.  When adding a plan-committed mutation to a runtime port,
  check the port's own extern block first — mirror parity of the *code* does
  not imply mirror parity of the *declarations*.
- the strict mirror set `done = 1` after a successful replacement and then fell
  through to the insert block, so one `d[k] = v` on an existing key could also
  append a duplicate entry.  C returns immediately at that point; the strict
  loop needs an explicit `mutated` flag.  Found by review, not by a gate.

`[DENIED]` shapes were not retried: fake C-extension `tp_hash` and ordinary
user-instance `__eq__`/`__lt__` record zero callbacks and cannot serve as
relocation controls.

Dict delete remains open: it still decrefs key and value before clearing the
entry slot and decrementing size, so a finalizer re-entering the dict can
observe a freed key behind a live index.  That is Proposal No.24 and must not
be inferred from this slice.

## Proposal No.24 — rooted dict delete split commit

Add a delete mode to the shared rooted dict probe.  On a stable match, commit
`key -> NULL`, `value -> NULL`, the index tombstone and the decremented size
together under one graph lock built from two store plans, and finish both plans
only after unlock.  The legacy path released key and value first, so a
finalizer re-entering the dict could observe a freed key behind a still-live
index — a use-after-free on every backend, not only the moving ones.

## Update — 2026-08-25 dict delete confirmed

### CONFIRMED Proposal No.24

C `py_dict_del_rooted_slot` and strict `_dict_del_rooted_slot` publish the
detachment under one graph lock and release afterwards; `py_dict_del` in both
mirrors is now a thin caller of the shared rooted op.  A C-extension
`tp_richcompare` relocates the dict during the delete, the delete still
succeeds with `len == 0` and the key absent, the detached value's pcc-native
`__del__` observes the committed table, a repeat delete still returns `-1`, and
scheduler roots balance.  Semantics pass 2/2 for the dynamic probe, 1/1 for the
source contract and 23/23 for dict+set parity.  Exact evidence is in
`docs/goal/evidence/2026-08-25-dict-del-split-commit.md`.

The legacy raw probes are now deleted rather than merely bypassed:
`py_dict_lookup`, `py_dict_keys_equal`, and the strict `_lookup`,
`_keys_equal`, `_slot_of`, `_entry_idx_of`.  No unrooted dict probe remains in
either mirror.

`[DENIED]` for this slice: the strict `#if PCC_PROBE_STRICT` carve-out drafted
from the Proposal No.21 set-remove precedent.  It assumed the strict mirror
would inherit the open forwarding-source release gap and therefore report
`del_calls == 0`.  Measurement showed strict **does** run the finalizer for a
detached dict value after two retirement epochs, so the carve-out was removed
and both mirrors now face the identical assertion.  Do not reintroduce a strict
exemption here by analogy with set remove; measure it.

Also recorded: the finalizer does **not** run inline when the container carried
a forwarding shell during the release.  Two
`pcc_gc_backend4_remap_and_retire_stopped_world()` epochs are required before a
detached value's `__del__` is observable.  A probe that asserts a finalizer
inline after a relocation is measuring the retirement schedule, not the commit
ordering.

## Update — 2026-09-08 fresh-admission blockers lifted; Proposals No.12c and No.12d verdicts overturned

### RESOLVED sub-boundary

Both `[DENIED]` fresh-admission verdicts above are superseded by measurement.
They read:

- No.12c, the **strict GC4 FUNC/ITER allocation blocker** — "FUNC/ITER stay out
  of the fresh admission set until their strict GC4 allocation/MemoryError
  bootstrap boundary is repaired and gated independently."
- No.12d, the **strict GC4 suspended-execution fresh-admission blocker** —
  "Adding those tags plus end-of-constructor publication made the first
  ordinary allocation fail through the same recursive MemoryError path... Do
  not retry another tag family until this allocator/fresh-admission
  interaction is isolated with a smaller allocator-level probe."

Neither verdict argued that admission was wrong. Both named an allocator
interaction as the blocker. That interaction no longer reproduces.

### Why admission is the correct direction, not a preference

`PY_FLAG_GC_FRESH_ALLOC` is the only mechanism keeping a half-built object out
of the relocation set: `pcc_gc_relocation_set_add` rejects it
(`py_gc_backend.c`, alongside `PINNED`/`DEALLOCATING`/candidate/target), and
`pcc_gc_backend4_zpage_candidate_snapshot` rejects a fresh owner outright with
its own counter. Every one of these tags is accepted by
`pcc_gc_colored_relocate_copy_supported_tag`. So while a tag is relocatable and
unadmitted, a mid-construction object of that tag can be selected and copied
with uninitialized pointer slots. Leaving the two lists disagreeing is the
hazard, which is what `GC-P1-BACKEND4-FRESH-ALLOC-FILTER-DISAGREEMENT` records
from the other side of the same seam.

### Test [CONFIRMED]

Two halves, both green on 2026-09-08.

**Publication.** Every admitted tag's constructor calls
`pcc_gc_publish_initialized` on its success path, in both mirrors:
`py_func`, `py_iter`, `py_gen`, `py_coroutine` (COROUTINE, CONTINUATION and
TASK), `py_exc_objects`, `py_class`/`py_class_attrs`, `py_weakref`,
`py_bytes`/`py_obj_stubs` (MEMORYVIEW), `py_list`, `py_dict`, `py_set`,
`py_tuple`. `PY_TYPE_STATICMETHOD` has no `pcc_gc_alloc` call site in either
mirror, so its admission cannot strand anything; the `py_internal.h` note that
it "has no public constructor" still holds.

**The blocker itself.** Two ordinary pcc programs, compiled with no flags and
run against the strict (pcc-Python port) runtime under `PCC_GC_BACKEND=4`,
with `PCC_GC_BACKEND=0` and CPython as controls. Each begins with tens of
thousands of ordinary container allocations — the exact point No.12d said
failed — before touching the tag families:

```
probe A  40000-iteration list+dict churn, then generators, iterators, a
         closure, 2000 instances via a classmethod, 500 raise/except cycles,
         a property, a staticmethod, a memoryview, gc.collect()
probe B  20000-iteration churn inside asyncio.run, then 300 awaits through a
         two-level coroutine chain, then 400 asyncio.create_task round trips
```

```
             CPython        GC0 strict     GC4 strict
probe A      byte-identical on all three arms, rc=0
probe B      byte-identical on all three arms, rc=0
```

No recursive `MemoryError`, no first-allocation failure, no output drift. The
suspended-execution family (COROUTINE / CONTINUATION / TASK) is exercised
through real `async`/`await` and `asyncio.create_task`, not a synthetic
constructor preflight, so this covers the case No.12d could only reach with a
preflight.

### Contract tests replaced

The three static contracts that kept the tags absent were the enforcement arm
of those verdicts, and they had been failing against shipped source:

```
gcsubstrate_d_store_ptr_capi_pins.py
  test_backend4_wrapper_constructors_publish_but_staticmethod_stays_unadmitted
  test_backend4_function_iterator_publication_waits_for_strict_allocator
  test_backend4_suspended_execution_publication_waits_for_strict_admission
```

They are replaced by five tests asserting the contract that now holds, pairing
admission with publication so neither half can land alone:

```
test_backend4_admission_never_exceeds_the_relocatable_tag_set
test_backend4_container_and_wrapper_tags_are_admitted_and_publish
test_backend4_staticmethod_admission_is_inert_for_lack_of_a_constructor
test_backend4_function_and_iterator_tags_are_admitted_and_publish
test_backend4_suspended_execution_tags_are_admitted_and_publish
```

### Open boundary this update does NOT close

`pcc_gc_colored_relocate_copy_supported_tag` accepts three tags the admission
set still omits: `PY_TYPE_THREAD`, `PY_TYPE_VIRTUAL_THREAD` and
`PY_TYPE_VTHREAD_CHANNEL`. They rely instead on liveness guards inside the
candidate snapshot (a non-NULL native handle, a queued/waiting virtual thread,
a core-kind channel), which are not the same protection: those guards reject an
object that is *in use*, not one that is *half built*. So a mid-construction
thread wrapper whose guard fields are still zero remains selectable. That is
the same defect class this update resolves for the other families and it is
the next candidate here. The new
`test_backend4_admission_never_exceeds_the_relocatable_tag_set` asserts only
the direction that holds today (admission is a subset), deliberately, so the
remaining gap stays visible rather than being frozen as intended.
