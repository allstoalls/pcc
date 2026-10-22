# One x86 assembler observation over sealed real assembly

Status: external observation harness, UNRUN. No production change or size-only
implementation. Preserve this packet before any separately admitted execution.

## Purpose and fixed stop rule

Current d233 Stage1 CI attributes 29.97% of completed Linux x86 codegen and
41.02% of completed Windows codegen to later assembly. This one observation
tests whether discarded sizing encodes are a sufficiently large opportunity to
continue source design. It does not measure an optimization or establish a
speedup. The predeclared screen requires both:

1. Layout is at least 25% of the entire `encode_assembly_object` call.
2. Relocation-bearing plus no-relocation control-flow layout calls are at least
   10% of final instruction calls.

Both met means `SIZE_ONLY_OPPORTUNITY_SCREEN_MET_REVIEW_ONLY`; otherwise HOLD.
Any source, input, count, timing-nesting or output contract failure stops the
gate. There is one call, no added rounds, automatic implementation, runtime
build or target compilation. Counts are not multiplied by durations to estimate
saved time. A met screen is only a reason for further source review; timer and
counter overhead, especially near the boundary, can invalidate a benefit
inference and is never subtracted using a guessed correction.

## Input and source relation

Use the existing real `pcc.frontends.c.ast.c_ast` Windows assembly, target
`x86_64-pc-windows-msvc`: 76,565,862 bytes, SHA-256
`8ad079c23208ef05f5385e975d45421ac5c50f882a14b68d942cf9134567f919`.
Its sealed complete COFF reference is 12,228,800 bytes, SHA-256
`4d910a121adc58f3f57f9d5041abcb406ff9ad3c6a5748581c6671d6cdcfcb6a`.
The original gate proved assembly/object/decoded equality on 231 functions,
48,161 blocks and 219,614 IR instructions with zero capture fallback.

This corpus was emitted before class-init outlining, with the older cold-slot
compiler. It is not newly compiled d233 frontend output and cannot establish
current module or whole-Stage1 instruction frequencies. It is used solely as a
large retained real assembly opportunity screen. Windows is chosen because its
text contains complete symbolic stackmaps and SEH directives; the retained
Linux text needs external packed stack-map plans and is not substituted.

The driver imports the current d233 production pcc subtree, bound by its full
source inventory. `x86_64_asm_driver.py`, `x86_64_encode.py` and `elf_x86_64.py`
are byte-identical to the input's original source. Current `coff_x86_64.py`
changes only `assemble_object` timing plumbing, preserving the ordinary
`phase_timing=None` path; `target_objects.py` likewise adds timing forwarding.
The manifest binds old/current hashes explicitly, without claiming that entire
old and new compilers are equal. It also binds the original complete group seal
and baseline result. No frontend, context, IR, assembly or runtime is regenerated.

## Ordinary production call and observational hooks

The single entry remains
`target_objects.encode_assembly_object(assembly, "x86_64-pc-windows-msvc")`.
It uses the unchanged complete COFF assembler, shared two-pass x86 assembler,
encoder, relocation conversion, SEH metadata construction and serializer.
There is no replacement assembler, altered cache policy, or replay of partial
private compiler components.

Only coarse wrappers record elapsed nanoseconds for:

- SEH text extraction/marker rewrite (`coff._unwind_source`)
- shared parse (`asm._parse_file`)
- shared layout (`asm._measure_sections`)
- shared assembler inclusive (`asm._assemble_file`)
- unwind table construction (`coff._append_unwind`)
- complete COFF assembly and COFF serialization

Shared inclusive minus parse/layout is explicitly payload emission, symbol and
relocation construction, validation and teardown, not pure final instruction
encoding. COFF inclusive minus shared assembly, SEH text and unwind tables is
adaptation/marker handling/other work. The disjoint partition must exactly sum
to the measured public entry. Expected coarse call counts are each one.

A wrapper only around `asm.encode_instruction` counts layout size-cache misses
and final top-level instructions. It calls the original encoder once and
returns the exact original result. Encoder recursion for `lock` prefixes is
not counted as a new top-level instruction. It reads no clock per instruction
and retains no instruction, operand, byte-result, label or relocation graph.
Its four disjoint layout buckets are:

1. Result has relocations, including symbolic branches under empty layout labels
2. No-relocation control flow, mainly register-indirect `call`/`jmp`
3. Other mnemonic outside the existing machine-byte-cache allowlist
4. Plain-string/no-relocation shape admitted by that existing allowlist

Buckets 1+2 form the chosen sizing-opportunity count. Bucket 2 is not semantically
PC-sensitive. Bucket 4 does not prove an actual machine-byte-cache hit. A size
cache miss can still be a machine-byte-cache hit; these caches are not confused.
Bucket sums must equal layout calls, and opportunity ≤ layout calls ≤ final
calls, with no calls outside the expected scopes. Cache caps/clearing, operand
validation, final PCs, labels, exception order and section behavior stay in the
original functions. All wrappers are restored and identity-checked in `finally`.

## Equality, outputs and limits

Require complete encoded bytes equal the sealed COFF, not just `.text`. This
covers every symbol, relocation, stackmap and unwind byte. After timing, the
ordinary COFF parser must validate it and nonempty `.text`, `.pcc_stackmaps`,
`.pdata` and `.xdata` must be present. Retain only the 12.23 MB output and a small
scalar JSON receipt; do not copy the 76.6 MB input or a redundant 216 MB decoded
contract. File loading/hashing, final decoding, checks and writes are outside
the timed call, but inside the process guard. RSS is process-lifetime high water,
not per-phase memory. No native execution or malformed-input differential is
claimed by this observation-only replay.

Use the existing reviewed strict single-process bootstrap and shared exclusive
lock: 300 seconds, hard 4 GiB address space and NPROC=0, 4 GiB continuous free
reserve, 512 MiB maximum output growth. Process starts, threads and FFI remain
denied. Preserve the live supervisor reservation variables and explicit
no-provisioning/no-auto-pcc1 flags. The coordinator checks complete source
inventories before and after; the driver verifies selected files, all actual
PCC imports and every input/hash before and after. No resource cap is raised.

## Coordinator invocation

Bind the exact interpreter/bootstrap/adapter and fresh output in a reviewed
wrapper. Invoke `observe_assembler.py` with the pinned manifest hash and these
explicit path arguments: `--source`, `--assembly`, `--reference-object`,
`--reference-result`, `--reference-qualification`, `--output`.
The source argument is the sealed d233 timing candidate, not a mixed or newly
modified checkout. Reference arguments point to the original Windows baseline
payload and complete group seal. Do not overwrite any earlier artifact.

No tests, PCC imports, compiler invocation, assembly, native execution or
profiling was performed while preparing this packet. Independent source review
and exact coordinator-wrapper review precede any actual admission.
