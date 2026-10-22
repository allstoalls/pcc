# Bounded host indexed-IR process handoff (unapplied)

This recovery packet contains source and focused regressions, not an executed
qualification. All new tests, split compilation, object equality, native code,
GC modes and Stage1 performance are **UNRUN**. It is not a production rollout.

Base: `a7282a82ef885412e90143e1cede90dcfd8c0d81`, PCC tree
`2d087588b3dce59d5f3ddceaa7af327b0f70f010`. The six existing production
preimages were also compared byte-for-byte with the later `db1830cf` source;
the float/doc increment changes none of them. `manifest.json` records complete
preimage/postimage hashes and modes. Apply `stage-bridge.patch` and the separate
`codec-packing.patch` to matching preimages only. The latter is independently
reviewable transport scratch reduction, not another backend algorithm change.

## Reason for the change

The completed a728 Mac Stage1 attempt still reached its 2400-second deadline.
Its ordinary singleton source/AST demand vector contained only two independently
varying dimensions; shared export bytes were constant. A completed
`pipeline_context` worker therefore supplied the same 1,262,317,568-byte padded
reservation to 31 later workers despite substantially different generated IR.
The source's MAX rule operated as written; another sort subdivision does not
repair that representation mismatch.

This candidate separates the lifetime of frontend/context graphs from backend
decode, stack preparation, root planning, emission and assembly. It does not
reduce the global cap, margins, retry limit, or validation. Actual benefit is
unknown. Windows' substantial assembler work is a separate measured owner;
this packet does not claim to optimize that algorithm.

## Exact route

The opt-in `PCC_HOST_INDEXED_PROCESS_SPLIT=1` applies only to ordinary host
workers. Default behavior and the existing native deferred route remain
selected otherwise. The initial route requires direct capture/emission,
zero-fallback policy, no-libpython, native object output, an artifact root and
the existing measured tree budget. Checkpoints, action-cache hits, in-process
codegen, text-oracle/validation and native deferred plans are not combined with
this host route.

1. Existing complete export/effect/AST preparation runs once. Each ordinary
   singleton frontend still receives its full production contextual view,
   module assignment, entry/sibling initialization and selected owned passes.
2. After the existing post-pass target, libpython and zero-fallback checks,
   frontend-only owners are released. The published indexed kernels and any
   required canonical IR text remain valid. The existing v1 codec serializes
   that exact pre-stackprep module to a temporary PIDX, then atomically publishes
   its seal. No second frontend or parent-side IR decode is introduced.
3. The same worker prefix invokes the existing indexed-object entrypoint in a
   fresh process. It verifies the sealed input, decodes once, checks the actual
   target/shape, and uses the existing owned emitter, stackmaps and object codec.
4. The existing resource pool owns both phases. For width W, the dependencies
   are FE_i -> BE_i -> FE_(i+W). Module identities do not move. The original
   frontend ordering chooses chain positions; mixed-phase admission still uses
   the existing chooser and MAX rule.
5. Only after backend exit zero, matching final PID/token RSS, and an
   attempt-bound output receipt/hash does the coordinator retire its PIDX and
   release the next frontend dependency. Cancellation leaves the input intact
   for the existing one exclusive retry. Stale output is reset at that retry.
6. Verified PCO paths then enter the unchanged collector/link contract.

The 47-component backend envelope is mechanically derived: cold-header bytes,
function/global counts, and sum/max per-function lengths of the codec's 22
fixed scalar arenas. It includes call/PHI/terminator/inline-error/value planes
without projecting instructions. Owner, collector, target, passes, codec and
emission policy remain class boundaries. Unknown shapes calibrate exclusively;
no old native-corpus fitted coefficient or warm peak database is substituted.

## Resource and semantic limits

- Width bounds unfinished module handoffs, not absolute disk bytes. A frontend
  retry can temporarily own its previous published sidecar and its one fixed
  replacement temporary. No random large-file orphan is permitted. The outer
  disk reserve and process-tree cap remain authoritative.
- A retained historical correct-target c_ast PIDX was 58,153,998 bytes. That
  pre-outline input is a transport-size example, not a current-module forecast.
  At width four, four such published inputs alone would use 232,615,992 bytes;
  objects, temporary replacements and other compiler artifacts are additional.
- Host packing scratch is at most 8,192 packed scalar objects plus a 64 KiB
  joined chunk. This is not a 64 KiB process-RSS bound. The whole JSON header,
  decoder seed reconstruction and per-module IR still exist. The codec retains
  its 512 MiB header limit and native raw-FD branch.
- The first implementation deliberately rechecks PIDX bytes at six handoff
  boundaries, in addition to decode and its original write. This adds real
  streaming I/O. More incomparable shape vectors can also add exclusive
  calibrations. Neither cost is called free or assumed to improve throughput.
- PIDX includes the existing complete globals/types/call/PHI/error/landing/value
  representation. Stack layout, roots and stackmaps are regenerated normally
  after decode. No runtime construction, safepoint, ownership cleanup or pass
  is removed. Exact object and executable semantics still need qualification.
- Parallel stage scheduling can change the cross-module first diagnostic.
  New invalid/stale-capsule errors intentionally fail at the handoff boundary;
  complete diagnostic-order identity is not claimed. Existing within-module
  frontend/pass checks precede serialization and backend emission.
- Host-only selection does not prove the new modules/signature changes compile
  in the native closed world. Native compiler, full five-GC, fixed point and
  Stage1 completion remain unqualified.

## Diagnostics and focused validation

The existing timing switch is reused. Frontend completion is explicitly marked
`artifact=PIDX`; backend completion is a separate `artifact=PCO` row. Phase
totals use the `indexed-sidecar` and `indexed-sidecar-backend` routes, so two
rows are not two whole-module compilations. Backend decode, capsule hashing and
receipt work are outside the existing named backend/assembler subphases and
remain included in the backend worker's overall elapsed time/RSS.

The new tests cover fixed-width dependency planning, preserved identities,
strict capsule/target/pass/shape/result checks, successful retirement ordering,
failed cleanup, stale attempts/output, pressure cancellation after publication,
exclusive retry, ordinary Mac temporary aliases and bounded wire packing.
They use fake process boundaries and do not establish actual child execution.

The sole execution owner may run the focused files under the existing shared
lock, 300-second/4-GiB host guard and 4-GiB free reserve:

```sh
uv run pytest -x -n0 -vv --tb=short \
  tests/python/test_host_indexed_process_split.py \
  tests/python/test_pipeline_indexed_handoff.py \
  tests/python/test_self_backend_indexed_codec_host_packing.py
```

Next evidence must include ordinary split-versus-unsplit output equality on
real frontend inputs, failed/cancelled handoffs, and actual CI process/peak/disk
observations. A count change, mock PASS or byte comparison alone is not a
Stage1 speedup or native acceptance claim. No cap increase is proposed.
