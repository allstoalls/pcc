# Unapplied ARM managed-liveness worklist candidate

Source-only proposal. No test, compiler, profile or native execution has run for
this candidate. It is not production-active and has no measured speedup.

The prior x86 bridge experiment was held after a counter-free four-arm result
of 4.16% combined emission/object improvement, below its predeclared 5% floor.
This independent candidate targets a different structural owner: repeated
whole-CFG sweeps in ARM managed liveness. It does not incorporate that bridge
patch, parked record caches, ARM local-reuse changes or other pending fixes.

## Exact recovery and scope

`production.patch` changes only
`pcc/backend/self_backend_precise_stackmaps.py` at the convergence loop inside
`_native_managed_liveness`. The exact preimage is from production commit
`02b9bc2d3c071bbc3e547a63e045daa73cc6e1bc`:

- Preimage SHA256: `bfc3630f958d6c63a1c4f0677b2d55082dfb4d90960978df84cf2c83a72eaa64`
- Postimage SHA256: `f1882fa4c29ad15c8843d30a9bf4d4bebf5b52307d0dc4daa155157b8d8289fe`

Recover by obtaining that exact source file, checking its preimage hash,
applying this patch in a separate source tree and checking the postimage hash.
Do not apply it to a mismatched preimage or a qualifying frozen tree.
The manifest binds patch/README bytes and file sizes. It does not claim that
the old runtime or any existing source inventory qualifies the new compiler.

The packed planner is selected by the internal `aarch64-darwin` stack-map
target. Both Darwin ARM and Linux AArch64 owned emission use it. x86 retains
its distinct set-based path and receives no direct benefit from this change.

## Repeated work and changed schedule

Let B be blocks, E successor occurrences, W tracked-value words, and K the
existing convergence sweeps. The old loop evaluates all B rows and E edges on
every sweep, including a complete final no-change sweep. Dense row work is
proportional to K × W × (B + E). The current real workload's K, W, queue
remainder and this solver's present time share have not been measured.

The candidate preserves the original transfer equation, PHI live-out seeds,
tracked-value numbering and row-update helper. For W=0 it retains the original
loop and all successor validation, with no worklist allocation.

For W>0 a FIFO starts with every block, B−1 through 0. Pending membership limits
the ring to B entries. During a block's first evaluation, the original successor
walk also records its reverse edges. On a row change, only known predecessors
are scheduled, suppressing duplicate requests. The first B pops still occur
in the exact old order; new notifications are appended behind them.

A missing reverse link belongs to a predecessor not yet visited. Its initial
entry is still pending and will read the latest successor state. After that
visit, the reverse link exists for every later notification. This preserves
fair propagation of the same monotone finite bitset equations. The existing
helper's bool reports either IN or OUT changes; scheduling on either remains
conservative and avoids changing its ABI.

## Semantic and ownership constraints

- All blocks, including unreachable blocks, are initially processed.
- The first scan preserves descending block / ascending successor diagnostic
  order, and duplicate edges remain represented.
- Self-loop notification works because pending membership is cleared before
  row evaluation.
- Roots, pointer provenance, PHI seeding and the final backward instruction
  scan are unchanged. Inline cleanup live-ins are still injected at their
  exact trigger before publishing that call's live-after state.
- No persistent cache, kernel field, class layout, helper return ABI, root
  semantics, collector behavior or output-record format changes.
- One local raw arena stores three B-scalar spans and two scalars per edge.
  Its indices survive growth; no native address is retained across appends.
  Initialization and use are inside try/finally. It closes before final
  scanning and on errors. Actual resident memory is not the logical scalar
  payload size, and the new allocation can itself fail.

Independent source review found no correctness blocker in the exact production
hunk, including B=0/B=1, ring wrap, duplicate edges, lazy indexing and cleanup.
Host execution, native lowering and native error-path behavior remain UNRUN.
Existing unrelated scratch-arena error ownership is unchanged.

## Falsifiable next gate, not yet admitted

Focused host regressions are being prepared separately and are not bundled
here. They must cover the independent set oracle, PHI/loop cases, initial
visit order, zero tracked values, unreachable/duplicate/self-loop edges,
malformed-successor error identity, arena cleanup, and an inline-trigger case
compared with an explicitly split CFG. Synthetic chain counts prove only the
mechanism, never workload performance.

Before uncounted A/B, use one ordinary correctly targeted production host
module to compare complete liveness/planner outputs and object bytes. Record
actual B/E/W, baseline sweeps, candidate row visits and weighted row work;
do not assume favorable chain structure. If the real module does not show a
material decrease in weighted row work, stop without timing repetitions.
Any later uncounted comparison must preserve the original resource limits,
inputs and output contracts, use both orders, and retain the same explicit
whole-pipeline benefit threshold. No runtime build or full Stage1 is requested
by this preservation artifact.

Bundled files: `production.patch`, `README.md`, `manifest.json`. Production
pre/postimages and in-progress tests are not duplicated in this public set.
