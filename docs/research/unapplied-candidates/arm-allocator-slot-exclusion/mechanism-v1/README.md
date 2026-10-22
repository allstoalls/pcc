# ARM allocator slot-exclusion: one retained-input mechanism screen

Status: source-only packet; independent review pending; all execution of this
packet is UNRUN. The candidate remains unapplied. This is a fixed two-entry
screen, baseline then candidate, with no timing rounds or benefit claim.

## Question and fixed decision

Does the reviewed negative-slot rule remove a substantial number of the
allocator's actual per-used-value predecessor visits on the retained real ARM
module? It does not eliminate repeated calls to `_function_level_facts`: there
is normally one such call per function-wide allocator invocation. The repeated
work is the backward predecessor walk performed for individual used values.

Before using any counts, require both entries to reproduce the complete retained
packed object and normalized decoded contract. The candidate must also equal the
new baseline object byte for byte. Require identical input CFG, function coverage,
allocator/facts-call coverage, transport counts, stack maps and compact unwind.

The predeclared mechanism screen is:

1. Baseline predecessor-entry visits must be positive.
2. Candidate predecessor-entry visits must fall by at least 25% in aggregate.
   Integer comparison is `candidate * 4 <= baseline * 3`.
3. Pending pops and predecessor-entry visits must not increase for any function
   or in aggregate. Visits include repeated entries, not just distinct edges.

An output, provenance, source, observer or cleanup contract failure stops the
sequence. Output equality with zero/insufficient work reduction or nonmonotone
counts is HOLD. A successful screen permits review of a next step; it does not
authorize timing, additional modules/targets, native execution or production
application. Do not tune the threshold or append rounds after seeing the result.

## Exact source and historical input

The baseline is the retained numeric-batching production source with inventory
`7430a30766c11a59e7185758b725b289388ae131498ce6a8a568bdf10ff773ba`,
whose complete pcc subtree is the published bf1bf82d tree
`930263bf204e578796c72dab5d4aa31fd7ab27ae`.
The V2 candidate inventory is
`dce88d3b8b29e7364e7faec2e71694ae577aeeee1aec0f6a4866b98974eb00d0`,
pcc tree `992c49dc67063865c2b3b26a7092d3dcc6348a96`.
Only the reviewed regalloc postimage and its host test differ. Its actual 44/44
host result and immutable V1 failure are preserved separately. The retained
whole snapshots include older unrelated docs/bootstrap/test bytes; neither is
described as a complete current-master checkout.

Use the existing `arm64-apple-darwin23.6.0` c_ast indexed input, SHA256
`d5f8711ac0e6fdf09d9836938aedf590314527e947c1d8cf59b9258de97ee14d`.
It contains 231 functions, 48,161 blocks and 219,614 instructions. Its ordinary
single-source library preparation used the real owned sys-provider policy,
with zero fallback and no synthetic exports. The input was generated before
class-init outlining. Its frequencies are historical and cannot establish
coverage or speed for the current outlined CI corpus. No input is regenerated,
retagged, reconstructed from a denied diagnostic, or combined with a new context.

The retained reference object is 18,577,664 bytes, SHA256
`2a684530426482465ec2a808f6621093ac4cc357d84fc440db33eed105392154`.
The reference-producing compiler was 17e126ba, whereas the current baseline is
ce75a877. These compilers are not identical. The manifest enumerates every
backend-source difference found between their complete inventories:

- The ARM emitter and shared preparation gained optional phase-timing arguments
  and guarded measurements. This gate explicitly passes `phase_timing=None`.
  The source diff leaves preparation, validation, slot assignment, stack-map
  planning, allocator, emission and object algorithms in the same order.
- The new timing helper is inactive; the x86/COFF/target-object changes are not
  called by this direct ARM transport plus native-object entry.
- Regalloc, indexed codec, kernel, ARM encoding/assembly, stack maps and native
  packed-object encoding remain byte-identical to the old reference owners.
  Frontend class-init changes are not called when decoding this frozen pidx.

This source audit justifies trying the retained reference; it does not certify
new bytes. The current baseline must actually reproduce that reference before
the candidate may run. Any mismatch stops rather than relaxing the old contract.

## Observer, same equations and actual owner

The driver parses the exact hash-bound `_function_level_facts` source. It checks
the one `pending: list[int] = []` initializer and the entire original while-loop
AST. It replaces only that empty-list initializer with an observed list factory;
restoring that expression must restore the full original function AST exactly.
The isolated host function keeps its original globals, signature/defaults,
equations, scans, PHI handling, CFG construction, visited stamps and return value.

The list subclass inherits append, clear, truth testing and ordering. Its sole
overridden operation calls `list.pop` once, records the actual pop and the length
of that block's predecessor list, and returns the same block ID. In these exact
sources the remainder of every successful iteration visits every entry in that
built-in predecessor list with no break or return. The lists are not mutated
after their construction. Thus the sum counts actual predecessor-loop entries
on successful complete runs. An exception could interrupt the loop after a
count, so no counts from a failing entry are accepted.

The second wrapper is installed at the real prologue's imported allocator alias,
not an unused definition. It binds the actual function/kernel identity and checks
one facts call for each eligible invocation. Vararg or non-function-wide early
returns retain zero counters. The facts wrapper requires that same kernel and
one pending-list instance. Both hooks are restored in `finally`, including on
an emission exception. No predicate, solver, root planner, instruction emitter
or object encoder is replaced; no added eligibility rule or second solve occurs.

Observer allocations and Python dispatch have overhead. There are no entry or
phase benchmark timers in this driver; any supervisor duration is only a resource
receipt. Success applies to this valid retained input. The observer is not a
diagnostic-order proof for malformed input; those focused controls are separate.

## Ordinary route and equality

Both arms use the same supported route:

`decode_indexed_module_file` → `emit_aarch64_darwin_indexed_transport`
(`optimize=False`, `structured_instructions=True`, `phase_timing=None`) →
`transport.assemble_sections` → `encode_native_object_from_sections`.

The historic input/effective target-pass settings are retained. Active MADD or
tail-call plans are not covered by this `optimize=False` corpus. No LLVM, frontend
worker, runtime archive, external assembler or executable is invoked.

Each packed object is retained. Complete raw-byte comparison is supplemented by
the same full normalized decoded object and decoded ARM stack-map digest used
by the retained reference, covering all sections, symbols, relocations, stack
maps and unwind payloads. Its approximately 267 MB JSON representation is hashed
as a stream instead of writing two redundant copies. No emitted functions are
sampled or dropped from the comparison.

## Coordinator execution contract

Use the already admitted fixed interpreter and strict single-process bootstrap.
Each arm has its own original 300-second outer guard, hard 4 GiB AS, NPROC=0,
4 GiB free-space reserve and 512 MiB output-growth cap. Preserve the live worker
reservation/state variables, exact source cwd and shared exclusive lock. The
ordinary existing sources are reused without copies or edits. Check both full
byte/mode inventories before and after each arm, outside the observed entry.
Require a clean, reaped, audit-free baseline guard before starting the candidate.

Driver arguments are `--source`, `--arm baseline|candidate`,
`--manifest-sha256`, `--reference-root` and a fresh `--output`. Only the candidate
also accepts the exact preceding `--baseline-result` and
`--baseline-result-sha256`. A pinned coordinator wrapper binds all actual paths,
the 44-PASS receipt, guard receipts and preserved packet identities; review that
wrapper before execution. No environment or resource cap is expanded.

About 19 MB of object data plus small scalar/function records is retained per
arm; the 512 MiB cap also covers guard logs/staging. The reference and all previous
failed/HOLD evidence remain untouched. Stop on any cap or contract failure.

Native closure/ABI, native pcc1, actual collector execution, async/gateway and
full Stage1 remain outside this packet. Host cross-compilation is not Mac-device
performance or native acceptance. This screen never attributes the entire
12–14% CI function-setup phase to predecessor walks.
