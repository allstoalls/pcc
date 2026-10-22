# Numeric-data batching: fixed uncounted whole-entry comparison

Status: source-only proposal, independent review pending, all four timing slots
UNRUN. The separately sealed 46 host cases and one historical corpus mechanism
gate passed. The latter reduced 1,162,090 validated scalar records to 114,742
actual _Data records (90.1262%) with full COFF byte/decoded equality. Those
counts do not establish elapsed-time improvement.

## Fixed experiment

Exactly four fresh single-process slots, in this order: b1, c1, c2, b2.
Baseline uses retained d233 source inventory 568c44d8; candidate uses V2 source
inventory 7430a307. Only the assembler production file and its new host test
differ. Both sources, their full hashes, compiler identities, the interpreter,
resource guards and input are frozen. No source copy or runtime build is needed.

Each slot invokes the ordinary encode_assembly_object exactly once for
x86_64-pc-windows-msvc, with the same sealed historical 76,565,862-byte c_ast
assembly. No hooks, wrappers around compiler functions, counters, altered
batch bounds, warm-up calls, retries or extra measurement rounds are permitted.
The external coordinator wrapper enforces slot order and each predecessor's
complete cleanup, exact bytes and zero audit denials before admitting the next.

Time the complete function entry: SEH rewrite, parser, layout, final emission,
object/schema validation, COFF adaptation, unwind metadata and serialization.
Module imports, source checks, input loading, reference hashing, result decode,
output writing and postseals are outside the interval. Wall and process CPU
clocks bracket that one entry. Baseline and candidate use the same warmed-import
boundary; this is not a cold-import or frontend/whole-worker benchmark.

Every slot must produce the complete reference object byte-for-byte and pass
complete COFF dataclass equality. The reference SHA256 is
4d910a121adc58f3f57f9d5041abcb406ff9ad3c6a5748581c6671d6cdcfcb6a.
Each full 12,228,800-byte object is retained. One contract, guard, source or
cleanup failure stops the sequence. After c1, a wall-time regression greater
than 10% against b1 is a fixed early HOLD and c2/b2 are not run.

## Predeclared result rule

After all four successful slots, compare baseline mean(b1,b2) with candidate
mean(c1,c2). The pure compare_rows helper performs only result arithmetic:

- Mean whole-entry wall reduction must be at least 5%.
- Both normalized order pairs b1 versus c1 and b2 versus c2 must improve.
- Mean wall reduction must exceed the baseline spread, defined as
  abs(b1-b2) / mean(b1,b2).

Only all three conditions permit WHOLE_ENTRY_THRESHOLD_MET_REVIEW_ONLY.
Otherwise record HOLD_NO_MATERIAL_ENTRY_BENEFIT_ESTABLISHED with every actual
number. No additional round may be added to change that decision. CPU and RSS
are reported separately; no speed result is inferred from record counts.

RSS is Linux ru_maxrss immediately before and after the entry: a process
high-water that includes imports and input loading. It is not current RSS,
phase allocation, or the later complete supervisor high-water. The latter is
retained independently by the guard. Full object decode is after the reported
through-entry sample and outside timing.

## Bounds and source scope

Each slot retains the original 300-second, hard 4-GiB AS, hard NPROC=0,
4-GiB continuous free reserve, 512-MiB growth and exclusive shared-lock envelope.
The same pinned interpreter and strict process/FFI audit apply. Finish and reap
each slot before the next, and avoid concurrent source hashing/copies or other
compiler/test work during timed calls. Input checks before/after each call are
outside timing. Expected retained output is under 64 MiB for all four complete
objects and small receipts; no redundant large decoded-contract files.

Published files are measure_entry.py, manifest.json and this README. The driver
runs one chosen slot; the reviewed coordinator wrapper supplies fixed paths,
prior-stage guards and full inventories, and uses the same file's pure helper
outside the interval for first-pair stop-loss and final comparison. Its manifest
binds the actual corpus result and final seal, not an assumed prerequisite.

Passing would support a modest host assembler result for this one historical
pre-class-outline corpus. It cannot establish current CI module frequencies,
current CI speed, ARM benefit, native runtime/GC behavior, native pcc1 execution
or full Stage1 success. A production two-path proposal and real CI still need
separate review and authorization; no application is performed by this packet.
