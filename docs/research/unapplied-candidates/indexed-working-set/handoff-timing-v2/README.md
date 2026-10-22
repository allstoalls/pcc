# Core handoff timing

Unexecuted, source-only successor to the frozen V1 timing proposal. The scope is reduced to three coordinator operations and backend decode/shape. No current CI snapshot, workflow, resource limit or validation rule changes.

Apply `source.patch` only to the exact preimages in `manifest.json`. Its three production preimages are from `8aa2881c005d11dfeba05c0275386b954eeb312c`, PCC tree `e0fe5ee61793cc74d4b1564d4cb6787c13d884a4`; the later `cc5eaa9118399c3ef63e3c2423b7fbfb403dc651` changes unrelated tests. Verify preimages and preserve those corrections. The fourth path is a new test. V1 remains an unapplied reference; V2 is a complete patch against the production preimages, not a delta to V1.

## Boundaries

The existing worker-timing switch enables `pool_prepare`, `pool_reset`, `pool_retire` and `backend_decode_shape`. All original operations remain once, in order. No hash/read is removed. The handoff provider, codec, ownership, sealed formats, retirement and retry logic stay unchanged; the original 13-field timing provider prefix is byte-identical.

Each operation records its completion status, inclusive nanoseconds, module, task and PID. Pool task is the actual scheduling index; worker task is -1 and joins existing admission records by module/PID. Prelaunch prepare/reset use PID 0. Labels are bounded and escaped; normal counters/times are exact. Clock or reporting errors cannot replace operation or cleanup errors. Timing off adds no new clocks, artifact reads or reports.

The three synchronous pool scopes have one cumulative progress snapshot after each operation, plus a terminal snapshot after cleanup. Use only the latest valid snapshot per pool invocation, never sum snapshots. `blocking_fraction_ppm / 1000000` is their combined share of the observed scheduling-loop wall. The denominator excludes command/spec/order setup and includes waits, overlapping child work, earlier diagnostic output and cleanup when reached. Child timing must not be added to pool wall. A deadline can leave only the previous completed snapshot; the current unfinished operation and subsequent time remain unknown.

`backend_decode_shape` includes codec reads, JSON/scalar construction, kernel reconstruction and shape validation. It does not independently measure temporary-arena reconstruction or hashing. Backend validation and final receipt are left inside the existing backend-worker elapsed residual, along with uninstrumented overhead. Likewise frontend publication remains in the existing codegen residual. These residuals are only upper bounds for their contained operations. Subtract only disjoint parent timings from the same completed attempt; retain integer-millisecond rounding uncertainty. No speedup claim follows from these diagnostics.

## Focused validation

The proposed coordinator-only host selection is 39 new cases plus the existing 75 contracts, with 44 subtests counted separately:

```
pytest -x -n0 -vv --tb=short \
  tests/python/test_indexed_handoff_timing.py \
  tests/python/test_self_backend_indexed_codec_host_packing.py \
  tests/python/test_pipeline_indexed_handoff.py \
  tests/python/test_host_indexed_process_split.py
```

The new tests cover fake clocks/sinks, bounded output, cumulative fractions, unchanged original provider, source placement and the actual mocked pool lifecycle with timing off/on and cleanup success/failure. No children or compiler run in this selection. Counts and outcomes remain unverified until execution.

The helper additions enlarge an existing compiled provider. Host tests cannot establish native closure, real FE/BE execution, cancelled-process behavior, full object parity, GC, pcc1 or Stage1 qualification. Existing real CI gates remain necessary. The active cc5 gate's result does not qualify this new timing source.
