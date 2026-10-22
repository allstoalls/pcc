# pcc GPU-participating GC — first slice design (CPU-only oracle)

Status: **first slice — metadata-only CPU oracle.** No GPU kernels, no real
collector integration, no five-GC production wiring. This document specifies
the partitioned, mostly-non-moving design, the CPU-owned correctness control
plane, and the explicit claim boundary. Package: `pcc/gpu_gc/`. Tests:
`tests/gpu_gc/`.

Source of the design: `docs/refs_docs/deep-research/deep-research-gpu-gc.md`. The research
survey concluded that modern "GPU-participating GC" is **not** a wholesale
tracing collector on the GPU; it is a **partitioned, mostly-non-moving,
concurrent collector with a CPU-owned control plane and GPU-assisted data-plane
kernels** over stable, blockized metadata (the lesson distilled from
Unified-Shared-Memory managed heaps, vAttention's contiguous-virtual-memory KV
management, Jenga's heterogeneous block allocator, and Mooncake's
content-addressed KV reuse). This slice builds the **CPU control plane and its
correctness oracle** so the contract is testable before any device code exists.

---

## 1. Why a control plane first

The dominant failure mode of GPU-offloaded GC (older Berkeley/Jikes RVM work)
was that irregular pointer graphs plus atomic contention erased the parallel
win, and that **object movement under a running device kernel corrupts device
execution**. The Unified-Shared-Memory (MPLR 2023) result exists precisely
because ordinary managed heaps and GPU execution do not interoperate when the
collector may move data behind native execution.

Design consequence, adopted here: **correctness lives on the CPU**; the GPU only
ever accelerates *regular, bandwidth-heavy* work over metadata that the CPU has
already stabilized. Before we write a single kernel, the CPU side must be able
to state and *test* the whole correctness contract. That is this slice.

Two planes:

| Plane | Owner | Responsibilities |
|---|---|---|
| Control plane | CPU (this slice) | root discovery + snapshot, write-barrier discipline, epoch transitions, region-state transitions, final liveness decisions, residency intent, reuse/invalidation — every invariant whose failure corrupts memory |
| Data plane | GPU (future slices) | bitmap/card scan, mark-frontier expansion over regular regions, liveness reduction, evacuation scoring — over metadata the control plane fixed |

---

## 2. Blockized substrate (`substrate.py`)

The heap is **regions subdivided into fixed pages**. Each page carries a compact
descriptor: stable `BlockId`, `LayoutClass`, movable/pinned bit, refcount, a
modelled liveness bitmap (`live_slots`), a remembered-set/card summary
(`remembered`), last-touch epoch, and an optional content hash for reusable
immutable pages.

**Stable block identity is the load-bearing invariant.** A page's
`BlockId.key() == (region, index)` never changes for the substrate's lifetime,
even across free → reuse and eviction. A reuse generation is tracked by a
monotonic `serial` that does **not** change identity. This is exactly the
property a device-side handle needs so it can be held across a collection
without a pointer moving under it, and it mirrors vLLM's KV-block discipline:
*stable block identity, touch on reuse, tail insertion on free, metadata
invalidation at eviction*.

Page-state machine (enforced by `Substrate`, invariant-checked by
`check_invariants()`):

```
FREE      --allocate-->  ALLOCATED
ALLOCATED --touch------> ALLOCATED     (refcount++)
ALLOCATED --free-------> FREE          (only at refcount 0)
ALLOCATED --evacuate---> EVACUATING    (nursery/movable only, safe epoch)
EVACUATING--complete---> FREE          (source retired after model copy)
ALLOCATED --evict------> EVICTED       (cold reclaim; identity retained)
EVICTED   --touch------> ALLOCATED     (revive)
```

Region kinds encode the mostly-non-moving policy: `NURSERY` (movable, the only
evacuation-eligible region), `OLD` (non-moving by default), `PINNED` (never
moves). Immutable-layout pages are non-movable regardless of region.

The free/reuse queue uses **tail insertion** so low-reuse blocks are evicted
first, and the head is the eviction candidate — the vLLM eviction order.

## 3. CPU-first concurrent collector (`cpu_collector.py`)

A tricolor (WHITE/GREY/BLACK) partitioned concurrent-marking state machine over
the substrate. Epochs are linear and guarded:

```
IDLE -> ROOT_SNAPSHOT -> CONCURRENT_MARK -> REMARK -> SWEEP [-> EVACUATE] -> IDLE
```

`SWEEP` is **non-moving** (OLD/PINNED freed in place). `EVACUATE` is a
post-sweep safe point and only touches NURSERY regions — selective evacuation,
never global copying.

### SATB vs incremental-update — decision: **SATB (snapshot-at-the-beginning)**

We use a **snapshot-at-the-beginning deletion barrier** (`SATB_DELETE`), not
Dijkstra-style incremental-update. Rationale, tied directly to the GPU
data-plane constraint:

- SATB fixes the reachable set as a **snapshot** at mark-start. Anything live at
  the snapshot survives this cycle (it may *float* and is reclaimed next cycle).
  This gives a future GPU scan a **stable work frontier**: the set of objects the
  device must consider does not grow due to concurrent mutation, so a kernel can
  be launched over a fixed work list without re-synchronising with the mutator
  mid-scan.
- Incremental-update re-greys the *target* of a pointer store, which can **add**
  work to the frontier while the GPU is already scanning it — the exact
  "reachable set shifts under device execution" hazard that motivated the
  Unified-Shared-Memory paper.
- SATB's cost is a cheap, local barrier that shades the **overwritten** referent,
  plus some floating garbage. For a bandwidth-bound device-assisted scan we
  prefer *stable frontier + a little float* over *moving frontier + zero float*.
  This is the ZGC/G1 lineage rather than CMS.

An `INCREMENTAL_UPDATE` barrier is included **for comparison/telemetry only**;
the default and the tested soundness path is SATB.

**Soundness, tested:** if the mutator drops a reference `a → victim` during
concurrent marking, the SATB barrier shades `victim` grey; `REMARK` drains the
barrier buffer and re-scans it, so `victim` (live at snapshot) is **not** swept
this cycle. It becomes reclaimable on the following cycle once it is genuinely
unreachable. Both directions are regression-tested
(`test_satb_barrier_preserves_dropped_reference_this_cycle`,
`test_satb_floating_garbage_reclaimed_next_cycle`).

**Reachability oracle:** `reachable_from_snapshot()` computes the transitive
closure of the snapshot roots over remembered sets — the *truth* the marking
phase must reproduce. Tests assert the BLACK set equals this closure.

## 4. GPU-assist oracle + classifier (`assist.py`)

Do **not** offload arbitrary pointer graphs. A deterministic, table-driven
classifier routes each page by `LayoutClass`:

| LayoutClass | AssistClass | Why |
|---|---|---|
| `FLAT_ARRAY`, `OBJECT_VECTOR`, `RAW_PAYLOAD`, `IMMUTABLE` | `GPU_TRACEABLE` | regular/homogeneous — data-parallel scan fits |
| `POINTER_TABLE` | `GPU_SUMMARY_ONLY` | GPU may compute a liveness summary; CPU owns frontier expansion |
| `POINTER_GRAPH` | `CPU_ONLY` | irregular polymorphic — offload loses to atomic contention |

**Kernel parity is provable, not trusted.** `AssistOracle.mark_page` is the
authoritative CPU computation of a page's live slots — the contract a GPU kernel
must equal. `assisted_mark(page, gpu_kernel)` classifies, (models) dispatch for
eligible pages, and **verifies** the modelled kernel output against the oracle;
on mismatch or kernel-unavailable it falls back to the CPU answer and increments
fallback telemetry (`parity_mismatches`, `kernel_unavailable`, `cpu_fallbacks`,
`gpu_dispatched`). This is the roadmap's required operability signal, and it is
how a *future* real kernel is validated: `kernel_out == mark_page(page)`.

## 5. Metal residency adapter (`metal_adapter.py`)

Apple's path is **not CUDA**: vLLM-Metal is MLX-based, uses unified memory with
"true zero-copy operations", and has only experimental paged attention. So the
Metal strategy is *not* "port CUDA VMM" — it is to keep **one logical address
space** and treat pages as a **tiered residency problem**: annotate each page
with a residency class and let a backend adapter decide what a real
implementation *would* do.

Residency modes: `CPU_HOT`, `GPU_HOT`, `SHARED_HOT` (the unified-memory ideal),
`SPILLABLE`, `DISABLED_GPU`. Transitions are validated against a legal-transition
table (e.g. `DISABLED_GPU` may only return to `CPU_HOT`).

This slice ships a **NO-OP adapter**: `_metal_available()` reports absent, so
every residency op returns `AdapterStatus.SKIPPED_WITH_REASON` while still
recording CPU-owned residency **intent** and telemetry (spill/reload/skip/
by-mode). Callers can therefore prove the fallback path rather than silently
believe a device op happened.

## 6. Tiered reuse directory (`tiered.py`)

For **immutable, restartable** blocks (frozen metadata tables, deduplicated
serialized objects, cacheable subgraphs), reuse is content-addressed —
distilled from vLLM prefix-cache block hashing and Mooncake's
content-addressed KV pool. This slice is **local-only** (single process,
in-memory).

`content_hash` is a deterministic SHA-256 of the exact bytes. `BlockDirectory`
provides register/touch, hit/miss accounting, `invalidate`, and refcount-based
`release`. The central contract is `get_or_recompute(data, recompute)`:

- **hit** (present, not invalidated) → touch and return, no recompute;
- **miss** (absent or invalidated) → call `recompute()`, re-register under the
  content hash, return the fresh entry.

This is the Mooncake **"recompute on get failure"** invariant: the caller always
gets a valid, resident entry — never a stale or `None` one — so a failed reuse
never enters an inconsistent state.

## 7. Encoded invariants (what the tests actually assert)

- **Reachability preservation** — every page reachable from a root at snapshot
  survives the cycle; unreachable-at-snapshot-with-no-revival is reclaimed;
  BLACK set == snapshot closure. (`test_cpu_collector.py`,
  `test_integration_oracle.py`)
- **SATB soundness** — a reference dropped mid-mark is preserved this cycle and
  reclaimed next cycle.
- **Stable identity** — `BlockId.key()` invariant across free/reuse/eviction;
  no two live pages share a key; identity drift is caught by `check_invariants`.
- **Refcount / eviction correctness** — touch increments, free decrements,
  underflow rejected; eviction retains identity but drops metadata.
- **Mostly-non-moving guard** — only movable nursery pages evacuate; OLD/PINNED
  and immutable pages never move.
- **Deterministic classification** — same page → same AssistClass, table-driven.
- **Provable kernel parity** — kernel output trusted only when it equals the
  oracle; fallback counted otherwise.
- **Residency legality + no-op honesty** — illegal transitions rejected;
  everything else `SKIPPED_WITH_REASON`.
- **Hash stability / invalidation / recompute** — stable hash, invalidation
  forces a miss, recompute-on-miss re-registers.

## 8. CLAIM BOUNDARY (read before citing this work)

This is a **CPU-only research oracle**. It does **NOT**:

- run any GPU / Metal / CUDA / MLX kernel, allocate device memory, or touch a
  driver — "GPU-traceable", "GPU-hot", and "kernel parity" are *specifications a
  future kernel must satisfy*, modelled on the CPU;
- implement a **moving / relocating** collector — every "evacuation" is a
  metadata transition that retires a source page after the model has migrated
  its live slots; no real object is copied;
- integrate with pcc's **five production GC backends**
  (`PCC_GC_KIND_*` 0..4 in `pcc/runtime`); it only *borrows their vocabulary*
  so a later integration has a shared state model to target. It does not touch
  the C runtime, the pcc-Python ports, or any GC backend selection;
- claim **MLX / vLLM / vLLM-Metal / Mooncake** interoperability — those are the
  design references, not integration targets in this slice;
- claim **whole-Python-on-GPU** execution.

Every module under `pcc/gpu_gc/` is importable standalone, depends only on the
Python standard library, does not import from the rest of `pcc`, and does not
mutate `pcc/__init__`.

## 9. What the next slices would add (not in scope here)

CUDA-assisted marking/summary kernels for `GPU_TRACEABLE` pages (validated
against `AssistOracle`); a real Metal residency adapter behind the same
interface; selective cold-page packing at safe epochs; local CPU/SSD spill; a
cluster-aware shared-block directory; and finally an integration path onto one
of the five production GC backends. Each is gated on the CPU oracle here staying
green.

## 10. Gate command

```bash
env -u LC_ALL uv run pytest tests/gpu_gc -q -n0
```
