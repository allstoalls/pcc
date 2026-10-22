# Counter-free x86 bridge pipeline comparison

This is an external, unapplied experiment packet. Production and tests stay at
the already preserved bridge V2 + test-only V3 candidate. The 109-case host
gate and both real `c_ast` Linux/Windows object-equality gates have passed.
This experiment is UNRUN; those counted runs do not establish a speedup.

## Exact scope and reused inputs

Reuse the sealed, normally generated Linux `c_ast` indexed module, SHA256
`6bc6763aa8c44457aeb28f88b8aa8fbc49e8a3a0953bc504104eaa9950680741`,
58,153,998 bytes. It contains 231 functions, 48,161 blocks and 219,614
instructions. No frontend generation, context reconstruction, input rewrite,
runtime build, link or native execution is part of this experiment.

The baseline complete source inventory is `ea395e18...`; the V3 candidate
inventory is `febc3d32...`. The manifest gives their full hashes, exact emitter
pre/postimages, codegen checksums and shared source pins. The baseline's pcc
production subtree is exact published `02b9bc2d`; its complete snapshot retains
the previously disclosed older unrelated native test and documentation.
The candidate differs by the same one production file and two test files.

The earlier mechanism gate actually removed 219,614 diagnostic instruction
constructions, 219,614 additional operand-interning calls and 48,161 instruction
arena projections. Both sides still decoded and checked arithmetic flags once
per instruction. This gate asks whether that removal materially reduces the
whole backend/object pipeline cost, without those observation wrappers.

## Four fixed serial arms

Run exactly this order, each in a fresh process:

1. `b1`: baseline
2. `c1`: candidate
3. `c2`: candidate
4. `b2`: baseline

Every arm keeps the existing 300-second total guard, hard 4-GiB address-space
limit, NPROC0, 4-GiB free reserve, exclusive shared lock, process/FFI audit
denials and one-GiB per-arm disk-growth ceiling. The coordinator owns execution
and validates the complete source inventories before and after each arm.
Do not overlap another compiler, test, copy or full-tree hash operation with
the timed calls. These are at most four executions, with no warmup, automatic
retry, cap increase or follow-on measurement chosen after seeing results.

Stop at the first timeout, resource/audit failure, source/input mutation,
incorrect output or incomplete cleanup. Also stop before `c2` if `c1` is at
least 10% worse than `b1` in both combined wall and combined CPU time. Preserve
the two results as an incomplete regression stop-loss; do not call that a
balanced comparison and do not rerun to obtain a favorable order.

## Measurement boundary

`run_arm.py` decodes the exact pidx and checks all CFG rows before timing.
It then calls ordinary `emit_indexed_assembly(..., optimize=False,
stack_map_plans_out=plans)` followed immediately by
`encode_assembly_object(..., stack_map_plans=plans)`. Module and plans remain
live across both calls, as required by that production route.

There are no monkeypatches, per-instruction counters, profiler callbacks,
forced collections, serialization, hashing, progress writes or output
persistence within this interval. Automatic host GC keeps its ordinary policy.
Six clock reads record contiguous emission, encoding and combined wall/CPU
durations. The two subintervals add exactly to the combined interval; do not
add either subinterval to the combined total. Clock overhead is included.

Input decoding, full-source verification, decoded-contract validation and
output persistence are excluded. The coordinator's whole-process elapsed time
is reported separately and includes that qualification overhead. Neither is
a frontend-inclusive worker or whole-Stage1 benchmark.

Linux `ru_maxrss` is recorded before and immediately after the timed pipeline.
These are process-lifetime high-water marks including imports/input/preflight,
not isolated phase RSS and not values to subtract or add. The unchanged guard
also records sampled process RSS, including post-timing contract validation.
The hard enforced resource bound is AS; sampled RSS is an observation.

## Correctness and storage

Every arm must reproduce the previously sealed complete CFG, assembly hash,
and entire 12,591,320-byte ELF, including exact comparison with the reference
object bytes. Its complete parsed sections, symbols, relocations and decoded
stackmaps are normalized with the same JSON encoder as the passed object gate.
All 219,989,781 normalized bytes are hashed outside timing and must reproduce
the exact reference contract hash. No reduced contract or selective section
comparison substitutes for this.

Retain every arm's assembly, object and result. Hash the redundant normalized
contract instead of retaining four additional 220-MB copies; the original
complete contract is already sealed. Expected new assembly/object storage is
about 210 MB across all four arms. Reserve one GiB total conservatively while
keeping the separate four-GiB filesystem floor.

Each stage directory is `<comparison-root>/<b1|c1|c2|b2>/` with `payload/`,
`guard/` and `wrapper.json`. The existing strict bootstrap invokes `run_arm.py`
with its exact manifest hash, slot number, source, pidx, sealed baseline
reference result/object and fresh payload path. A narrow coordinator wrapper
enforces the fixed order, prior clean receipts, stop-loss and full inventories.
It must retain the live supervisor reservation variables unchanged.

After all four terminal successes, `compare_results.py` reads those receipts,
requires clean/reaped/ECHILD guards with zero audit denials, rehashes retained
assembly/object outputs and checks the complete source seals. It never imports
the compiler. All harness code and manifest must be preserved before execution.

## Predeclared interpretation

Report both normalized baseline-to-candidate order pairs, two-sample means,
and baseline spread `(max - min) / mean`. A useful scoped signal requires:

- Both order pairs improve combined wall time.
- Mean combined wall improvement is at least the larger of 5% or baseline spread.
- Mean combined CPU and emission wall time improve.
- Mean process high-water through the pipeline grows by no more than 10%.

Otherwise hold this candidate as a mechanism-correct cleanup with no material
pipeline benefit established, and do not launch more performance runs merely
to improve the result. This is a conservative decision rule, not a statistical
confidence interval or proof of whole-Stage1 improvement. Even a positive
signal only supports the next source/native qualification review; it does not
automatically activate production. Native compiler ABI/GC behavior and the
V2 multiply-invalid diagnostic-order caveat remain open.

Bundled files: `run_arm.py`, `compare_results.py`, `manifest.json`, `README.md`.
The production/test patches and existing reference artifacts are not duplicated.
