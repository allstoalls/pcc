# Investigation: GC4 frees walk the whole relocation set

## Status
active — 2026-10-14. The free-path walk is fixed by a member filter; the
page selector's whole-heap scan per selected page remains (below).

## Problem Description
Under `PCC_GC_BACKEND=4` the pcc1 five-collector canary compile exceeded its
6000 s deadline on Linux aarch64 and x86_64 (run 38530860670, `9ad29d80`),
while GC0-GC3 finished (aarch64: GC0 326 s, GC1 747 s, GC2 880 s, GC3 845 s).
Per-phase on aarch64: exporting `pcc.unsafe` took 371 s on GC4 against 2.3 s
on GC0, the summary 51 s against 0.8 s, codegen of the canary module 257 s
against 3.3 s. It is slow, not hung. Expected: GC4 within a small factor of
the other collectors.

## Repro
macOS arm64, host pcc compile (`compile_python(..., backend="self",
libpython_mode="off")`, `PCC_PYTHON_IR_PASSES=off`), source-matched runtime.
Program: keep four trees of `Node(kind, value, children)` (depth 5, width 6:
about 37k nodes) alive, then 200 rounds of building 2000 short strings and a
dict from them. Archive: `/Users/jiamo/pcc-recovery-archive/2026-10-13/gc4/perf/`
(`live_heap_bench*.py`; argument is the tree depth).

## Test [CONFIRMED]
Churn time at `ca703ae0`: GC0 0.6 s, GC3 1.6 s, GC4 186 s. With depth 7
(1.3M nodes) GC4 churn is 1.8 s, so the cost is not proportional to the heap.
`pcc_gc_relocation_set_size()` printed during churn: 0 between cycles, 70987
during a selected cycle. `sample` of the GC4 process: 36%
`pcc_gc_backend4_relocation_set_remove`, 35%
`pcc_gc_relocation_selector_best_page_batch`, the rest in the selector's
candidate scoring.

## Proposals
- No.1 skip the relocation-set walk for a non-member free [CONFIRMED]
- No.2 continue a selected page from its own owner list [DENIED]
- No.3 rank pages once per selection call [pending]

## No.1 skip the relocation-set walk for a non-member free [CONFIRMED]
`pcc_gc_note_object_freeing` calls `_relocation_set_remove(o)` for every
backend-4 free; the set is a singly-linked list, so each free walked up to
71k nodes. A key-only member filter (an open-addressing index like the other
GC indexes) records objects where they join the set
(`_relocation_set_add` and the selector's inline add). A free of a
non-member skips the walk, a member's walk proves it gone, an empty set
clears the filter, and a failed insertion marks the filter incomplete so
every walk is kept until the set is empty. Probe churn: 186 s -> 16.7 s.
A/B over the GC4 native and relocation suites: no regression (the failures
in `test_gc_backend4_production.py` and
`test_relocation_copy_clears_stale_sweep_verdict_backend4` fail identically
on `ca703ae0`).

## No.2 continue a selected page from its own owner list [DENIED]
`pcc_gc_backend4_select_relocation_pages` selects a page in 16-object
batches and starts each batch with a whole-heap scan for a seed node on that
page (`_best_relocation_page_batch` with the page token), so a page of B
candidates costs B/16 heap scans. The attempted change continued later
batches from `load_ptr(page_token, 112)` after checking the page with
`pcc_gc_zpage_page_index_find`. That index is never populated (no caller of
`pcc_gc_zpage_page_index_insert`), so the continuation never ran and each
page stopped after its first batch: 17 of 48 candidates were selected
instead of 48, and `test_list_iterator_releases_owners_on_success_and_error[pcc0-raising]`
then finalized fewer than 2000 objects under GC4. The page-policy contract
test also pins the 16-object batch (`if batch_budget > 16:`). Dropped;
`test_colored_relocating_page_selection_takes_every_page_candidate` now
guards whole-page selection.

## No.3 rank pages once per selection call [pending]
After No.1 the profile is the selector: one whole-heap preflight scan per
selected page, and the dispatcher passes its remaining step budget (up to
65536 work units at the default step multiplier) as the page budget, so one
step can rank every candidate page by a heap scan each. Ranking candidate
pages in one scan per call (or bounding scans per step) is the next
candidate; it changes which pages are evacuated first and needs a design
decision against the page-policy contract.
