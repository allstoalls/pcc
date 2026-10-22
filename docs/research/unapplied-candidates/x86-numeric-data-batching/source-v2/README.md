# Bounded numeric-data batching: V2 source and host regressions

Status: UNAPPLIED; independent review pending; all execution UNRUN. This is a
recoverable source/test candidate, not an activation or performance result.
The preceding size-only screen remains HOLD.

## Recovery and exact source

Base production commit: d2334624197b5fc14aaca93bb916683fb1afcb79.
Base pcc subtree: ee02ba6bf0d79e9706c1791aa650093f5f91c338.
The manifest binds the existing assembler preimage, both postimages and the
base inventory. Apply candidate.patch to a separate exact-base tree, then
verify both postimage hashes. It is a complete two-path patch against the base,
not a delta to apply on top of V1. No other candidate belongs in this source.
The published set is this README, manifest.json and candidate.patch. Local
preimages/postimages are recovery aids and are not additional public files.

V1 was preserved without execution in commit
331bf644e031b7cbdb26d15ead8eb46f041e2ce1. Its parser flushed at blank lines,
which would change parsed-record equality in the existing mixed-line-ending
regression. V2 preserves the original empty-line skip before the new flush
check. The old regression and its equality assertions are unchanged. This is
a source-review finding; V1 has no executed failure or pass to relabel.

## Mechanism and semantic boundary

Every integer/float item still goes through the original whole-list validation,
conversion, range checks and little-endian packing at the original encounter
point. Only its already encoded bytes enter one per-parse bytearray. A flush
freezes those bytes into the existing immutable _Data representation. No raw
bytearray enters the plan, where that type means packed instruction spans.

Flushes occur before every nonempty nonnumeric source line, every symbolic or
symbol-difference item, a full batch, and successful parser return. Ignored
empty lines remain ignored. Labels, section changes/revisits, alignment,
zero-fill, size/metadata directives and instructions retain their order and
positions. Symbol expressions are never combined with numeric data. NOBITS
validation stays in layout, so even numeric zeros remain file data and a later
parse error retains its precedence over a NOBITS layout error. Layout, final
emission, relocations, symbol construction, structured stack-map insertion and
object validators are unchanged.

The fixed 8192-byte logical scratch limit follows the existing private plan
chunk scale and fits all 1/2/4/8-byte scalar widths. It was not tuned by timing;
it bounds outstanding payload independent of the size of a numeric run and
is neither an environment setting nor an output/resource cap. Allocation
capacity/overhead can exceed the logical payload count. A nonpositive private
limit provides the original one-record-per-item host control; an atom larger
than an artificially small positive test limit stays a single ordinary record.

There is no cross-invocation cache or returned mutable payload. An exception
traceback may retain ordinary local state, including the bounded pending
buffer; eager destruction on error is not claimed. Allocation-failure timing
can change. The new closure/shared current-section cell and bytearray operations
still need native closed-world qualification. No native ABI/GC or Stage1 pass
is asserted by this source packet.

## Evidence and bounded next gate

The sealed historical pre-class-outline Windows c_ast assembly contains
1,162,090 numeric items, presently materialized separately and revisited by
layout/final emission. A conservative read-only physical-line census finds
114,682 numeric runs. This motivates testing fewer records; it does not assign
the observed parse/shared-emission owners to _Data alone, establish current CI
frequencies, or promise elapsed-time gains.

The new file statically defines 37 host cases. They cover exact scalar-call
counts, fixed and tiny/disabled bounds, integer limits/endianness, floating
special values/overflow, blank lines, barriers, matching first exception type
and message, NOBITS parse-vs-layout ordering, immutable/local buffers, and full
ELF/COFF bytes including a valid precise stack-map payload and SEH sections.
The COFF fixture is a validated object-format fixture, not native GC execution.

Proposed first selection: the 37 new cases plus the unchanged three mixed-line
ending object/plan comparisons and six malformed mixed-line diagnostics:

```sh
python -m pytest -x -n0 -vv --tb=short -o addopts= \
  tests/python/test_x86_64_numeric_data_batching.py \
  tests/python/test_x86_64_assembler_lifetime.py::test_mixed_separators_preserve_parsed_records_and_complete_object_bytes \
  tests/python/test_x86_64_assembler_lifetime.py::test_invalid_mixed_separators_preserve_exact_exception_and_diagnostic
```

This is a proposed 46-case, host-only gate. Actual collection must equal 46;
no skips, xfails, native nodes or hidden exclusions are intended. Only the
execution coordinator may run it after source review and admission, under the
existing serial 300-second / 4-GiB AS / NPROC=0 boundary, shared lock and 4-GiB
free-space reserve. No compiler, runtime build or FFI is needed by this scope.

Only after this focused gate passes should a separately reviewed single real
corpus re-encode compare complete object bytes and decoded contracts to the
sealed reference. No extra timing rounds, threshold changes, native/full-CI
claim or production promotion is automatic.
