# P0: one threaded runtime, concurrent GC, protocol-free compiled code

Owner: maintainer-directed P0 (2026-10-22). Every package lands on
`shenming/master` as its own commit. The measurements below come from the
programs in `tests/benchmarks/threading/` (package 7).

## Why

Same Mac, same programs, median of 3 (seconds):

- fib(32): pcc single-thread runtime 1.08, threaded runtime 3.41, CPython 3.15 0.14
- dict/str churn: 1.90 / 7.12 / 0.40
- objects + methods (200k): 1.67 / 5.65 / 0.06
- 48 x fib(26) on 1/2/4/8 threads: threaded 11.75 / 11.60 / 20.06 / 23.03
  (negative scaling); CPython 0.38 at every thread count (GIL).

A sampled single-runtime fib (GC0) spends ~63% in the registered-slot GC
protocol (frame enter/leave 21%, slot zeroing memset 9.5%, store_root and
store plans 10%, foreign leases 5%, pins, root barriers, borrowed loads),
12.8% in the generic compare chain for `n < 2`, 5.5% in exception checks
and 3.3% in recursion counting. The threaded runtime makes every one of
those calls take the global object-graph lock (CAS, TLS lookups, no-park
leases) and bumps one global lease counter, which both triples the
single-thread cost and serializes all threads.

## What Go, Java and Fil-C do, and what pcc takes

- Roots: Go stack maps, HotSpot OopMaps and Fil-C's compiler-maintained
  frames let the collector find stack pointers; compiled code pays nothing
  per operation. pcc already emits precise stack maps
  (`pcc/backend/precise_stackmap.py`, `PCC_PRECISE_STACKMAP_TABLES`) and walks
  this thread's stack with them (`PCC_GC_STACKMAP_ROOTS`). pcc takes: the walk
  becomes the only root source for stack frames of every thread; heap frames
  (generators, continuations) keep the registry.
- Stopping threads: Fil-C's soft handshakes ("ragged safepoints") ask each
  thread to act at its next pollcheck and never stop the world; threads in
  syscalls are "exited" and the collector acts for them. Go has
  entersyscall/exitsyscall, HotSpot thread states. pcc takes: per-thread
  records with a parked/exited state and the parked frame pointer; blocking
  kernel waits (condition wait, join, mutex wait) run exited.
- Moving: Go and Fil-C never move objects, so they never pin. ZGC moves
  concurrently and heals references with a load barrier; JNI pins only for
  native code. pcc takes: GC0/1/2 never move, so foreign leases are free
  there; GC3/GC4 keep leases only for raw pointers held by foreign/runtime
  code until the runtime archive is itself walked by stack maps.
- Barriers: Go's write barrier is an inline flag test that is on only while
  marking; stack writes never have barriers (FUGC: grey stacks rescanned to a
  fixpoint). pcc takes: no barrier call for root (stack) slots; heap stores
  test the marking flag inline.
- Thread-local state: Go per-P mcache, HotSpot TLABs, libpas caches; no
  shared write on the allocation or rooting fast path. pcc takes: no global
  lease counter, no global frame list for stack frames, pin bookkeeping
  without shared counters, atomic header-flag updates.
- Reference counts (Python-specific; Go/Java have none): CPython 3.15t uses
  biased reference counting (owner thread non-atomic, others atomic) and
  defers counts of objects every thread touches. pcc must keep exact counts
  (finalizer/weakref timing is CPython semantics), so it takes biased counts,
  not deferred ones.
- Small ints: V8 Smis, OCaml/Lisp fixnums, CPython's compact-int specialized
  ops. pcc's tagged int `(value << 1) | 1` covers [-2**62, 2**62 - 1]; every
  operation on two tagged ints is decided inline without any root, lease or
  frame work.
- GC threads: Go background mark workers (25% of Ps) and sweeper, HotSpot
  parallel/concurrent GC threads, FUGC collector threads. pcc takes:
  collector worker threads for marking/sweeping/relocation in GC1-GC4; the
  mutator only answers handshakes.

## Work packages (each lands as its own commit with its tests)

1. Threaded runtime is the only runtime. `runtime_build_config()` is always
   threaded; `PCC_WITH_THREADS=0` fails explicitly
   (`pcc/frontends/python/thread_runtime_config.py`); implicit polls are on
   except in the modules that implement the poll or publish raw roots
   (`PCC_IMPLICIT_THREAD_SAFEPOINTS=0`, Makefile and owned builder);
   `freestanding_thread_kernel.py` leaves the inventory; tests and fixtures
   use the one archive. Tests: `tests/test_threaded_only_runtime.py`.
2. Lock-free rooting primitives. GC0/1/2 foreign leases return the no-op
   token without the lock or a shared counter; pin/unpin update the header
   with atomic or/and and keep no shared metric on the fast path; backend
   switches into a moving collector are refused while other threads exist.
   Tests: `tests/python/test_lockfree_rooting_primitives.py`.
3. Stack walks for every thread. Thread records (id, state, parked frame
   pointer, stack range) are linked at registration; parking and exited
   waits publish the frame pointer; the root visitor walks every thread when
   it owns a stopped world (and stops the world for the walk otherwise);
   stack-resident LIFO frames are not registered when the walk is active.
   Tests: `tests/python/test_stackmap_all_thread_roots.py` (five collectors,
   threads allocating under collection).
4. Protocol-free compiled code. Operands that are literals or rooted names
   are read in place; tagged-int arithmetic/compare/truth paths never stage;
   root-slot stores are inline reference-count moves; frame, lease and pin
   calls are emitted only for heap frames and foreign boundaries. Tests: IR
   shape (`tests/python/test_protocol_free_operand_paths.py`) plus five-GC
   execution.
5. Biased reference counts. The header refcount word splits into an
   owner-local and a shared count with the owner thread index; owner
   increments are plain stores, others atomic; merges preserve exact counts,
   so finalizers and weakref callbacks fire when CPython's would. Tests:
   `tests/python/test_biased_refcount_semantics.py` (cross-thread drops,
   `__del__` timing, weakref callbacks, sys.getrefcount).
6. Concurrent collector threads. GC1-GC4 marking/sweeping/relocation run on
   collector workers; mutators answer soft handshakes (scan own stack, flush
   buffers); exited threads are handled by the collector. Tests:
   `tests/python/test_concurrent_collector_workers.py`.
7. Measurement. `tests/benchmarks/threading/` programs and driver; every
   package records fib/alloc/objects/strings single-thread and 1/2/4/8
   thread scaling against CPython 3.15 in `docs/status.md`.

8. Synchronization primitives (OS and virtual threads). Lock, RLock,
   Condition, Event, Semaphore, BoundedSemaphore and Thread.join follow
   CPython 3.15 (arguments, timeouts, errors); blocking waits run exited
   (no polling, no spinning joins); a virtual thread that blocks parks on
   the primitive and an RLock names the virtual thread as owner. Tests:
   `tests/python/test_native_threading_lock_semantics.py`,
   `tests/python/test_virtual_thread_lock_mechanisms.py`. Remaining:
   `with` parking in resumable frames, Condition over a real lock
   (`Condition(lock)`, reentrant default, ownership errors, `notify(n)`,
   `wait_for`), Barrier/Timer.

## Invariants every package keeps

- Five collectors, one slot/root contract; CPython-visible semantics
  (identity, exceptions, finalizers, weakrefs, resurrection, refcounts as
  observed by `sys.getrefcount`).
- A frame not covered by a stack-map record keeps registered roots; a walk
  never skips registered slots it cannot see.
- Foreign (C extension) code keeps pins/handles; only pcc-compiled frames
  drop the protocol.
- No per-module or per-benchmark shortcuts.
