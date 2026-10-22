# Numeric-data batching: one historical real-corpus mechanism gate

Status: source-only proposal, independent review pending, UNRUN. Candidate V2
has a separately sealed 46/46 host result; this packet does not relabel that
result as native, corpus or performance qualification.

## Frozen operation and threshold

Run exactly one ordinary host encode_assembly_object(assembly,
"x86_64-pc-windows-msvc") using the qualified V2 source. Reuse the sealed
76,565,862-byte Windows c_ast assembly and its 12,228,800-byte COFF reference.
Do not regenerate frontend IR/assembly, switch targets, rebuild runtime or run
native code. The input is historical pre-class-outline compiler output. It
establishes applicability to this real corpus, not current CI frequencies.

The entry retains the normal COFF SEH preprocessing, shared assembler,
layout/final emission, schema validation, adaptation and serialization.
Require complete object byte identity with reference SHA256
4d910a121adc58f3f57f9d5041abcb406ff9ad3c6a5748581c6671d6cdcfcb6a.
Also parse both complete COFF objects and require dataclass equality, covering
all sections, bytes, symbols, relocations, alignment and flags. Require nonempty
.text, .pcc_stackmaps, .pdata and .xdata. The ordinary shared assembler still
validates the precise stack-map payload before COFF adaptation.

Predeclared screening rule: at least 50% fewer retained _Data records than
the original per-scalar representation. Failed equality or another invariant
is FAIL and stops the gate. Equality with less than 50% reduction is an honest
HOLD. Only equality plus the fixed threshold yields
MECHANISM_THRESHOLD_MET_REVIEW_ONLY; it does not activate the candidate.

## Counts and observer safety

V2 corrects one pre-execution V1 observer defect: _integer_payload is also used
by final emission for symbolic differences. It must forward those successful
calls unchanged without adding them to the parser-scalar denominator. Only
calls while parsing is active contribute to that count. V1 remains UNRUN; the
production candidate, fixed threshold, input, object proof and limits do not
change. The manifest binds the superseded frozen packet for recovery.

Count successful calls to the unchanged _integer_payload and the parser's
scalar float struct.pack calls. Each successful callback produced exactly one
_Data in the hash-bound original parser, so their sum is the equivalent legacy
record count for this actual invocation. This denominator is not a second
baseline parse or an inferred physical-line count. The 46-case prerequisite
includes a cap-zero control that retains the one-record-per-scalar path.

At the real parser return, count actual _Data records and payload bytes in each
section. All returned data must be immutable bytes with payload lengths from
1 through 8192, and total retained payload bytes must equal total successful
scalar payload bytes. Count once per scalar and once per resulting plan entry;
retain only fixed totals and per-section rows, not a per-item trace.

There are exactly three temporary hooks: _parse_file, _integer_payload and
module-level struct.pack. The latter is shared by Python modules; it forwards
every call unchanged and only counts calls while the parser is active, where
the pinned source permits <f/<d scalar formats. Instruction-span packing uses
the existing preconstructed Struct, and is untouched. Hard NPROC=0 prohibits
concurrent threads/processes in the admitted child. Restore all three exact
original objects in finally and verify their identities even after failure.
No cache policy, parser constant, source file or encoding option changes.

There are no performance timers. Counter/census overhead makes this unsuitable
for a speedup claim. If this gate passes, the next proposed measurement is the
already requested minimal order-balanced whole-entry timing with hooks absent;
that requires its own frozen command and admission, with no extra rounds.

## Source, artifacts and bounds

Candidate source inventory: 7430a30766c11a59e7185758b725b289388ae131498ce6a8a568bdf10ff773ba.
Candidate pcc tree: 930263bf204e578796c72dab5d4aa31fd7ab27ae.
Compiler identity: ce75a8770afcdad0679e4fb854f4bb6752fec786ae7558bf07fa46ec52152eb6.
Only the V2 assembler and new host test differ from the retained d233 production
subtree. Manifest pins bind the 46-case result, its final qualification, source,
input assembly, reference result and reference qualification. Historical input
came from cold-slot compiler 17e126ba; selected unchanged encoding files and
the assembler delta are explicit. This is not full new/old compiler equality.

Published files: check_corpus.py, manifest.json and this README. No private
workspace paths or raw logs are bundled. The coordinator supplies the pinned
input locations, a fresh output directory and an exact source cwd. Full source
inventories, interpreter/bootstrap identities and the shared lock are checked
externally before and after; the driver rechecks all manifest inputs and the
complete imported PCC closure. It preserves live supervisor reservations.

One process, one object entry, 300 seconds, hard AS 4 GiB, hard NPROC=0, continuous
4 GiB free reserve and the existing 512 MiB output-growth guard. Retained output
is one 12.23 MB object and a compact JSON result (16 MiB budget), not another
large decoded-contract copy. Existing strict process/FFI audit denials apply.
No resource cap is raised and no new execution capability is requested.
