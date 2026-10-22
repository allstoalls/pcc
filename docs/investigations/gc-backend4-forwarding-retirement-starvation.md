# Investigation: backend 4 forwarding entries never retire once the relocation set keeps a residue

## Status
active

## Problem Description
Under `PCC_GC_BACKEND=4`, a program that allocates 64 objects and then steps the
collector reaches a fixed point where nothing further happens: 56 forwarding
entries are retained forever and 8 objects stay in the relocation set. Every
subsequent `pcc_gc_step` reports the same 64 units of work and changes no
state.

This is a long-running-efficiency defect, not a crash: the forwarding table is
a permanent leak, and the relocation phase cannot advance past it. It is the
kind of thing the project's fifth obligation (pause / RSS / throughput over
time) exists to catch, and single-shot tests do not see it.

Retirement is `pcc_gc_backend4_remap_and_retire_stopped_world`
(`pcc/py_runtime/src/py_gc_backend.c:13321`). Its entry predicate is:

```c
    if (
        pcc_gc_selected_backend == PCC_GC_KIND_COLORED_RELOCATING
        && pcc_gc_relocation_set == NULL
        && pcc_gc_forwarding_population > 0
        && pcc_gc_backend4_remap_active == 0
        && pcc_gc_backend4_remap_epoch < INT64_MAX
    ) {
```

`pcc_gc_relocation_set == NULL` requires that **no object anywhere is still
awaiting evacuation** before any forwarding entry may retire. A running program
essentially always has such a residue, so that clause makes retirement
starvation the normal case rather than an edge case.

## Repro
`tests/python/test_gc_backend4_production.py::test_backend4_relocation_stress_stable_ids_and_no_old_addresses`,
which fails with `return 14` (`FORWARDING_ENTRIES != 0` after 16 steps):

```bash
gtimeout 900s env -u LC_ALL PCC_GC_BACKEND=4 uv run pytest -q -x -n0 \
  "tests/python/test_gc_backend4_production.py::test_backend4_relocation_stress_stable_ids_and_no_old_addresses"
```

```
round=0  work=184 relocation_set=8 forwardings=56
round=1  work=64  relocation_set=8 forwardings=56
...
round=15 work=64  relocation_set=8 forwardings=56
```

The probe builds its objects with `py_list_new`, which publishes, so this is
**not** the `PY_FLAG_GC_FRESH_ALLOC` publication class that the rest of this
file's probes needed. It reproduces from a standalone C probe against the
cached C runtime with no pytest involved.

## Test [CONFIRMED]
Measured 2026-09-08 with standalone C probes against the C runtime.

**The residue is the last 8 objects allocated, and nothing will evacuate them.**

```
after 3 steps          set=8 fwd=56 pagecand=2
page_drain(64)=0       set=8 fwd=56 pagecand=2      (x6, no change)
select_pages(64)=0     set=8 fwd=56 pagecand=2      (x3, no change)
stuck root[56..63] flags=0x10122 tag=5
```

`0x10122` is `PY_FLAG_GC_ZPAGE_ALLOC | PY_FLAG_GC_OLD | 0x20 |
PY_FLAG_GC_TRACKED`. `PY_FLAG_GC_RELOCATION_CANDIDATE` (0x800) is **clear**,
yet `pcc_gc_relocation_set_contains` returns 1 for all eight: they are in the
relocation-set list without the flag.

**That divergence is by design, and is not itself the bug.** The read barrier
clears the flag for a candidate that has no forwarding entry yet
(`pcc_gc_note_relocation_read_unlocked`, line 6237, and
`pcc_gc_resolve_root_slot_unlocked`, lines 10787 and 10792), and
`test_colored_relocating_gc_read_barrier_clears_candidate` in
`tests/python/test_gc_abstraction_surface.py` asserts exactly that transition
`2048 -> 0` for an object that never moved. The memoization is
memory-safe because `pcc_gc_relocate_copy` re-sets the flag on the source when
the copy actually happens (lines 9040 and 9126), so a later read still heals.
Changing the barrier to unlink the node would contradict that test and would
put an O(n) list walk on a read barrier; it is the wrong end of the problem.

**The retirement precondition is where it stalls.** Reading the exact state
immediately before a direct retirement call:

```
remap_active=0  fwd_entries=56  set=8
owns_stw=1  thread_id=1
retire=0  remap_active_after=0  fwd_after=56
```

`remap_active` is 0 and `remap_epoch` is nowhere near `INT64_MAX`, so of the
five clauses only `pcc_gc_relocation_set == NULL` can be false — and `set=8`
confirms it is.

## Proposals
- No.1 stop gating retirement on an empty relocation set [DENIED as written]
- No.2 reconcile `pcc_gc_forwarding_population` with the forwarding list [pending]
- No.3 give the object-granular selection a page to drain through [pending]

## No.1 stop gating retirement on an empty relocation set [DENIED as written]
### Code Change
Applied as an experiment and reverted:

```c
        pcc_gc_selected_backend == PCC_GC_KIND_COLORED_RELOCATING
-       && pcc_gc_relocation_set == NULL
        && pcc_gc_forwarding_population > 0
```

### Result
**No change.** Same probe, freshly built archive
(`~/.cache/pcc/test-artifacts/runtime-builds/768e427868a565503055a1fe-c-default`
created by the run itself, so this is not the stale-archive trap):

```
direct retire=0 fwd_after=56
second retire=0 fwd_after=56
```

The reasoning behind the proposal still stands — an object in the relocation
set that was never copied has no forwarding entry, so retirement has nothing to
say about it, and gating on the set being empty makes starvation the default —
but removing that clause alone does not lift the stall. It is necessary and
not sufficient, so it is recorded here as denied *as written* rather than as a
refuted idea.

## No.2 reconcile `pcc_gc_forwarding_population` with the forwarding list
### Why this is the next candidate
With No.1's clause removed, the only remaining falsifiable clause is
`pcc_gc_forwarding_population > 0`, and retirement still returned 0. So that
static counter reads 0 while the list holds 56 entries.

There are two counters for one structure:

```
pcc_gc_forwarding_population              py_gc_backend.c:605, static int64_t,
                                          maintained by ++/-- (see the
                                          decrements at lines 1859 and 1949)
pcc_gc_backend4_forwarding_entries()      py_gc_backend.c:3582, walks the
                                          actual list under the graph lock
```

`PCC_GC_COUNTER_FORWARDING_ENTRIES` telemetry reports the second (line 9593),
which is what the failing test and every probe above observe as 56. The
predicate consults the first. A counter maintained by increment/decrement
alongside a list that other paths splice is exactly the shape that drifts, and
the measurement says it has drifted to zero.

### Code Change
Not written. Confirm the divergence first: `pcc_gc_forwarding_population` is
`static`, so it cannot be read from a probe. Either add a temporary tagged
diagnostic accessor (and remove it, per the debug-instrumentation rule), or
have the predicate call `pcc_gc_backend4_forwarding_entries_unlocked()` and see
whether the stall lifts. If it does, the counter is the defect and the fix is
to derive the predicate from the structure rather than keep a shadow count —
not to add a second correction to the counter.

## No.3 give the object-granular selection a page to drain through
### The structural half, independent of retirement
`pcc_gc_select_relocation_set(budget)` (object-granular) and
`pcc_gc_backend4_select_relocation_pages(page_budget)` (page-granular) are two
selection APIs over one page-based evacuator, and only the second registers
evacuation pages. Measured consequences:

1. The 8 residue objects are in the relocation set with no evacuation page, so
   `pcc_gc_backend4_evacuation_page_drain` returns 0 forever (six consecutive
   calls above, no state change).
2. `pcc_gc_select_relocation_set`'s own `while (selected < budget)` loop cannot
   advance past one page. Its page search passes
   `require_unselected_page = 0` (line 8273), so every iteration re-finds the
   same best-scoring page, all of whose objects are already selected, `added`
   comes back 0 and the loop breaks. The page-granular sibling passes 1 for
   exactly this reason (line 8132). Changing the 0 to a 1 was tried and did
   **not** change the residue (the probe still reported `set=8`), so the
   one-page limit is real but is not what strands these eight; recorded so the
   next reader does not re-run it.

Whether the object-granular API should exist at all is the design question
underneath: it looks like a pre-page-evacuation remnant. Answering that is a
larger change than this investigation's stall.

## 2026-10-13: user-visible retention at source e62cef29
Observed on macOS arm64, host pcc compile (`compile_python(..., backend="self",
libpython_mode="off")`, `PCC_PYTHON_IR_PASSES=off`), current runtime archive.
Program: one function builds two lists of 1000 instances whose class has
`__del__` (and a 4096-byte bytes field), returns, then `gc.collect()` three times.

- GC0-GC3: all 2000 finalizers run; retained heap 266 KB.
- GC4: 314 of 2000 finalizers run and the heap stays at +9448 KB after the
  collections, so the objects are still treated as reachable rather than freed
  without finalization. In a shorter variant, further collections never raised
  the count (6x `gc.collect()` stayed at 166 of 2000); a second identical batch
  in the same process was finalized completely. Creating a weakref to every
  item made GC4 finalize all 2000.
- Separately, `tests/python/test_class_init_argument_ownership.py::
  test_classmethod_constructor_consumes_temporary_tuples` (now compilable)
  aborts with `[BAD_INCREF]` (rc 134) under GC4 only.

Repro programs: `/Users/jiamo/pcc-recovery-archive/2026-10-13/gc4/`.

### [CONFIRMED] explicit collect never retired forwarding entries
Telemetry (`pcc_gc_telemetry` 15/24/25) on the 4096-byte variant under GC4:
after `run()`, 1747 relocation forwards, relocation set 0, 1688 forwarding
entries; 312 = 2000 - 1688 finalized; three `gc.collect()` calls changed
nothing. Calling `pcc_gc_backend4_remap_and_retire_stopped_world()` twice from
the program (returns 1, then 1688) and collecting again finalized all 2000.
`pcc_gc_step`'s backend-4 explicit-collect branch (`pcc_gc_explicit_collect_active`)
only drained promotion and ran tracing cycles; remap/retirement existed only on
the background branch when a step was idle. The relocation-set residue starvation
described above is a separate stall.

### [PARKED] retiring inside the explicit collect
Calling remap/retirement from the explicit branch when tracing is idle makes
the retention program finalize all 2000 with 0 entries left on all five GCs
(patch and five-GC test in the archive:
`patches/gc4_explicit_collect_retire_forwarding.patch`,
`patches/test_gc_explicit_collect_retires_forwarding.py`). With it, the GC4
relocation test files run here passed (relocating, exit-list UAF, long-run exit
UAF, leaf roots, forwarding/relocation unit files); `test_gc_backend4_production.py`
and `test_gc_relocation_sweep_verdict.py` failed, and both fail identically
without the patch on a freshly built runtime, so they are pre-existing.

The patch is parked because retirement itself over-releases a live
replacement. `test_gc_relocation_sweep_verdict.py::
test_relocation_copy_clears_stale_sweep_verdict_backend4[pcc_python]` fails
with code 23 `finalizer-ran-on-live-copy` on the unpatched runtime. Instrumented
probe (`/Users/jiamo/pcc-recovery-archive/2026-10-13/gc4/sweep_verdict_probe_instrumented.c`):
before the explicit collect root=source, copy rc 2, source rc 1, 1 forwarding
entry; after it the root is healed to the copy, yet copy rc is 0, the entry is
gone, and `__del__` ran on both the live copy and the old shell (`del_other=1`).
That collect reached retirement through another remap caller. Retiring at
every explicit collect would widen exposure to this over-release, so fix it
first.

### [OPEN] BAD_INCREF while freeing during the sweep
`tuple([...1000 instances with __del__...])` in a function, then
`gc.collect()`, aborts under GC4 only (before and after the fix above):

    py_decref <- pcc_gc_forwarding_finish_detached
      <- pcc_gc_backend4_finish_remap_retirement <- pcc_gc_note_object_freeing
      <- pcc_gc_free_object_memory <- py_instance_dealloc
      <- pcc_gc_tracing_finalize_unreachable <- pcc_gc_tracing_sweep_unreachable
      <- pcc_gc_collect_tracing <- pcc_gc_collect

Freeing a forwarding *source* moves its entry to the detached list, whose
finish decrefs the entry's target; freeing a *target* moves its entry to
`dead_targets` (no decref). The abort means a detached entry's target was
already invalid. Not yet established which free path missed
`_forwarding_remove_target` for that target.

### 2026-10-14: more finalizer calls than objects in ordinary list programs
Source `de777b21`, macOS arm64, host pcc compile with `PCC_PYTHON_IR_PASSES=off`
and a source-matched runtime archive. Program: the body of
`tests/python/test_list_iterator_ownership.py` (2000 instances whose class
has `__del__`, built in a function, then `gc.collect()`), one shape per run.

- GC0-GC3: every shape finalizes 2000 then 4000 objects.
- GC4: `list(xs)` and `list(reversed(xs))` abort with `[BAD_INCREF]` before
  the first count; `list(enumerate(xs))` and an `enumerate(...)` value print
  2472 and 2483 finalizer calls for 2000 objects, then abort with
  `[BAD_INCREF]`. More calls than objects is consistent with the
  `finalizer-ran-on-live-copy` over-release above (a relocated object's
  `__del__` running on both the copy and the old shell), now reached by
  ordinary list programs, not only by the sweep-verdict probe. Not yet
  established that it is the same path.

Repro (run from the repository root):

```bash
env -u LC_ALL uv run python /Users/jiamo/pcc-recovery-archive/2026-10-13/scripts/gc4_list_shapes_probe.py \
  /tmp/gc4-list-shapes enumerate reversed borrowed enumerate_value
```

### 2026-10-14: no list copy needed; threshold 200-500 objects
Source `984cb2d4`, same setup. A function that only builds and drops
`[Item(i) for i in range(n)]` (class with `__del__`), then `gc.collect()`:

- n = 50 and 200: GC4 matches CPython.
- n = 500, 1000, 2000: GC4 aborts with `[BAD_INCREF]` in the collect; GC0-3
  match CPython. Without `__del__` (n = 2000) GC4 matches.
- Without the `gc.collect()` call, GC4 has finalized only 359 of 500, 680 of
  1000 and 1324 of 2000 objects when the function has returned; CPython and
  GC0-3 have finalized all of them. So once a background cycle has relocated
  part of the list, a refcount-dead object whose relocation is not yet retired
  is not finalized on release.

Repro programs and runner: `/Users/jiamo/pcc-recovery-archive/2026-10-13/gc4/min/`
(`probe.py DIR NAME...` compiles DIR/NAME.py with pcc0 and runs GC0-4).

### 2026-10-15: the classmethod-tuples test flips with unrelated runtime changes
`tests/python/test_class_init_argument_ownership.py::
test_classmethod_constructor_consumes_temporary_tuples[pcc0-tuple(left),
tuple(right)]` passes on `bad776a3` (3/3) and fails under GC4 (3/3,
`assert destroyed == created == 2000` after two `gc.collect()` calls) on
`ea678a14`, whose runtime differs only in paths this program never executes:
`_py_obj_call_body` gained an exception-subclass branch, and with that branch
removed again (incremental runtime build) the test passes. Moving the work
before instance creation, or reducing it to one flag load and a branch, both
still fail. GC4 scheduling is allocation-driven (the scheduler reads the
clock only to record pauses), so the likely variable is layout: the change
adds globals and module variables, which moves later addresses and so which
pages relocation selects.

The program itself is not the trigger: a copy that prints the counts instead
of asserting and reads `pcc_gc_telemetry(15/25)` behaves identically on both
runtimes (2000 then 4000 finalized, 1527 then 3055 relocation forwards,
forwarding entries back to 0 after each collect). So which objects relocate
before the collect decides whether the unretired-relocation retention above
shows up, and this test is a layout-sensitive probe of that defect, not a
check of the code being changed.

Files: recovery archive `2026-10-13/gc4/layout/` (`owners.py`,
`owners_exact.py`, `run.py`, `run_exact.py`: compile with
`PCC_PYTHON_IR_PASSES=off` against a source-matched archive and run GC4;
`bisect_dispatch_without_call.py` is the passing dispatch module).

## What this investigation does not claim
`owns_stw=1` was observed on the sole thread of a single-threaded probe before
any `pcc_stop_the_world` call of its own. That may be trivially true when there
is no other thread to park, or it may be a stopped world left behind by
`pcc_gc_step`'s backend-4 trace-cycle branch (line ~14500, which resumes only
when its own `pcc_stop_the_world()` returned 0). It was not chased and is not
part of the mechanism above.
