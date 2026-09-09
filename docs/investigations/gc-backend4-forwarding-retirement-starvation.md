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

## What this investigation does not claim
`owns_stw=1` was observed on the sole thread of a single-threaded probe before
any `pcc_stop_the_world` call of its own. That may be trivially true when there
is no other thread to park, or it may be a stopped world left behind by
`pcc_gc_step`'s backend-4 trace-cycle branch (line ~14500, which resumes only
when its own `pcc_stop_the_world()` returned 0). It was not chased and is not
part of the mechanism above.
