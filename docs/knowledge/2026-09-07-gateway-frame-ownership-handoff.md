# Gateway throughput and frame ownership checkpoint (2026-09-07)

> Historical session snapshot, not current task instructions. HEAD, dirty state,
> blockers and artifact paths below describe the recorded session. Check current
> source, `git status` and execution before reusing them; later user instructions
> take precedence. Search relevant sections instead of loading this whole file.

The user requires reproducible pcc/pcc1/CPython 3.15.0rc1 comparisons and
continued profile-based optimization toward exceeding asyncio. Changes belong
in ~/my/pcc or ~/my/pcc-gateway as appropriate; verified progress is authorized
for commit/push in both repositories. Issue: allstoalls/pcc#188 remains open.
No shared pcc1 installation promotion has occurred.

## Published source and evidence

- Core implementation: cea29593; backend diagnostic: edc910d1.
- Gateway latest comparison: ce6d0ac; backend diagnostic: 8dde262.
- Core and gateway working trees were clean after those pushes.
- Current main README uses benchmarks/results/2026-09-07-field-owners-three-way.json/.md.
  All 90 runs / 241,650 requests validate. Zero wait/C100 medians are
  49,087.9 / 48,457.6 / 86,611.5 QPS (pcc/pcc1/asyncio); peak RSS
  7.88 / 7.98 / 27.62 MiB. The pcc1 gap is 1.79x, not an asyncio win.
- A separate ownership A/B at 20,000 requests/run lowers peak RSS from
  141.31 to 17.48 MiB but costs 4.9% QPS (53,489.8 to 50,870.8).
- benchmarks/lifetime.py and heap_observer.py retain a same-process probe.
  Tracked counts before repair: 28/78084/156140/234196; after: 28/32/36/40.
  The +4 residual is open. Unsafe imports must stay outside the workload:
  putting the raw observer into it changes compilation/ownership mode.

## Current private compiler

Directory: build/gateway-stage1-field-owners-20260907 in the core repository.
Native SHA-256: 0ff76d8bf13985abfc04c9fe25bcbee5aa3043a8bed29b140f79163deb418bff.
Its source-snapshot, runtime-bundle and build-receipt.json are retained.
Stage1 completed in 375.83 s, peak tree RSS 5,104,893,952 bytes.
The application wrapper /tmp/pcc_gateway_field_pcc1 selects this exact source
and runtime and enables PCC_GENERATOR_FIRST_ENTRY_INIT,
PCC_FAST_COMPLETED_CONTINUATIONS and PCC_DIRECT_GENERATOR_TASKS.
These flags remain opt-in in source. The shared ~/.local/bin/pcc1 is still
the v84 historical installation; follow the installer qualification protocol
before an atomic versioned promotion.

Matching current runtime cache: ~/.cache/pcc/test-artifacts/runtime-builds/
a8feaa180e6fc84f76c70b33-pcc-py/libpy_runtime_pcc_py.a.
Archive SHA-256: 9814efd78e4c6a62ddc07fc54c53573cc28f5dfbe7cf022703d1258bfd70ea2f.
Do not pair stale runtime archives with later runtime edits.

## Verification and open qualification

- New field/iterator tests pass with host pcc and fresh pcc1: 8 source cases,
  40 actual GC0–4 executions per compiler. Native pcc1 adapter:
  /tmp/pcc_field_pcc1_gate.py; log /tmp/pcc_field_pcc1_gate.log.
- Fresh pcc1 HTTP/dashboard/failure-cleanup canaries: 3 passed, 292.32 s;
  /tmp/pcc_gateway_field_pcc1_examples.log. Host examples also passed.
- Gateway default suite: 290 passed, 20 integration cases deselected.
- New direct-generator runtime tests cover C and Python mirrors, GC0–4,
  return/yield/cancellation/failure. Existing real TCP/cleanup tests pass.
- IR fallback suite: 8 passed, 29.07 s. Its old three-line adjacency check
  was replaced with exact field-result data flow plus ownership consumption;
  layout, subclass routing and fallback-count guards remain intact.
- Recorded bootstrap/metadata/knowledge checks passed, but those inspect
  historical bootstrap binaries, not the new source's fixed point.
- Full test_fallback_baseline.py hit its 120-second outer timeout after
  20 passed nodes, inside test_closure_per_module_codegen_passes. No failing
  assertion, no final green summary, and no remaining children. The next
  attempt must shard phases and enable PCC_TEST_LIVE_PROGRESS=1 (-s), not
  repeat the same full-file timeout. /tmp/pcc_field_fallback_gate.log.
- New-source Stage2/Stage3 and full installation qualification remain open.

## Next performance owner

Read vthread-asyncio-throughput-gap.md, instance-field-iteration-owner-leak.md
and vthread-direct-generator-task.md before edits, plus the required repository
playbook/denied-experiments pages. The latest native profile has 2,303 on-CPU
samples, 2,098 including py_gen_next and 403 including py_list_set (17.5%).
Counts overlap. Reproduction tools and folded stacks are in gateway benchmarks.

The explicit self/LLVM application A/B completed 42 valid runs with one
compiler source/runtime: 51,056.2 / 51,288.2 / 89,852.4 QPS. A +0.45% native
difference is denied as a speed fix; runtime Python objects were already
emitted with llvmlite. Focus subsequent work on frontend-generated suspended
frames and ownership transfer, not compiler-build speed or backend switching.
No new frame-borrowing, liveness elision, barrier removal or eager scheduling
implementation has been attempted or accepted. Bulk frame construction remains
a previously denied speed claim. Preserve finalizers, cancellation, identity,
and all five GC contracts when designing the next measured candidate.
