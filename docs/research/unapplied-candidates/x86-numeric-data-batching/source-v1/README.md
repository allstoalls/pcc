# Bounded numeric-data batching: unfinished source preservation

Status: UNAPPLIED, review pending, all tests/compilation/native execution and
performance qualification UNRUN. This packet preserves newly authored source
before review or test preparation; it is not a production activation proposal.
The preceding size-only observation remains HOLD and is not reopened here.

## Exact base and recovery

Production base: d2334624197b5fc14aaca93bb916683fb1afcb79, pcc tree
ee02ba6bf0d79e9706c1791aa650093f5f91c338. Only
pcc/backend/x86_64_asm_driver.py changes. The manifest binds its exact preimage
and postimage. Apply production.patch to a separate exact-base source; verify
both hashes. Do not patch a sealed source or combine another candidate.
Published files are production.patch, manifest.json and this README. Local
preimages/postimages are recovery aids, not separately bundled public files.

## Mechanism and boundary

The unchanged parser validates each integer/float scalar in its original order.
It then appends that already encoded payload to a function-local bytearray,
flushing into the existing immutable _Data(bytes) representation. Flushes occur
at every nonnumeric source-line boundary, before every symbol/difference item,
at the fixed payload bound and at parser return. No raw data bytearray enters
the plan; that type is already reserved for instruction-span records.

The fixed 8192-byte logical scratch bound follows the existing private plan
chunk scale and accommodates the dialect's 1/2/4/8-byte scalar widths. It is not
selected from timing trials, a new environment knob, or a section/output cap.
Python/runtime allocation overhead is not claimed to equal 8192 bytes. A
nonpositive private bound preserves per-item records for differential host
controls; an individual payload larger than a positive test bound remains an
ordinary single _Data record, without truncation or rejection.

Whole-list empty-item validation, int/float conversion, integer bounds and
little-endian packing stay at their existing encounter points. NOBITS checks
remain in layout; numeric zeros stay file data. Labels, alignments, section
switches, zero-fill, size directives, instructions and symbol expressions are
barriers. Layout, final emission, relocation/symbol construction and packed
stack-map insertion are unchanged. Allocation counts intentionally differ, so
identical allocation-failure timing is not promised. The new native parser
closure/bytearray lifetime has not yet been compiled or executed.

## Existing evidence, not a benefit claim

The sealed historical pre-class-outline Windows c_ast assembly contains
1,162,090 numeric items. Current source makes a _Data record per item and
visits it in layout and final emission. A read-only physical-line census finds
114,682 numeric runs; batching can remove many retained records and downstream
visits while preserving all scalar parsing. This is one historical real corpus,
not the new CI frequency distribution, and no elapsed-time improvement has
been measured. The previous observed parse4.539s and shared payload/symbol/
relocation/teardown5.044s are inclusive owners, not _Data leaf attribution.

## Required next evidence

Before execution: independently review this exact production delta and freeze
focused regression tests. Cover first-error ordering, integer/float boundaries,
NOBITS and later parse errors, symbol expressions, section revisits, labels,
alignment, zero-fill, immutable records, fixed batching bounds and parse/error
lifetime. Then the coordinator may admit focused host controls and a separately
reviewed real-corpus full-object comparison under original resource limits.
Do not add timing rounds, native claims, or a source promotion automatically.
