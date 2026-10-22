# Unapplied x86 indexed-instruction bridge candidate

Status: source-only candidate; all tests, object comparisons, performance and
native qualification are UNRUN. Active production is unchanged. The production
preimage is the exact backend file published in `02b9bc2d3c071bbc3e547a63e045daa73cc6e1bc`.
No OSError or AArch64 optimization is included.

## Mechanism and scope

Direct indexed x86 input currently constructs a complete legacy instruction
arena for each function, including decoded operand tuples, `ParsedInstr`
wrappers and another operand-interning pass, then reads temporary instruction
views for selection. This candidate retains the block, PHI and terminator
shells but decodes each instruction's operands at its emission point and feeds
the existing tuple-based memory/compute handlers through the existing indexed
callback. Supplied legacy blocks keep the former path.

This is removal of the eager instruction arena and extra interning, not a
claim that x86 handlers are fully record-native or that operand decoding is
eliminated. A canonical arithmetic-flag read remains for every instruction
before its block shell and before any selector; missing flag-map entries must
still fail closed. The flag check is a retained scalar validation traversal.

The patch contains one production path, one new focused host-test file and an
existing lifetime test's injection-hook update. That test now injects at the
actual block-shell owner; exception identity and block/map/global cleanup
assertions are retained. Allocation, stack-map construction, frame/root
protocols, instruction selection, relocation handling and object writers are
unchanged.

## Diagnostic and qualification limits

Delayed operand decoding can change which error is reported first when a
function contains multiple independent faults. For example, an earlier
unsupported `extractelement` may now be reported before a later corrupt
binop opcode-text index; the eager route decoded that later operand first.
The candidate does not promise complete diagnostic-order equivalence for
multiply-invalid indexed input. It must still reject malformed input; this
ordering caveat is an explicit review item before production activation.

Independent source review identified and closed the initial missing-flag-read
problem in this V2 successor. It found no additional valid-input selector,
PHI/terminator, root/stack-map or ABI change by inspection. That is not executed
native or object equivalence. The new callback and its union-typed operand
parameter still require the actual native closed-world boundary before a
native-compiler qualification claim.

The new host tests cover Linux ELF (symbolic and packed stack maps), Windows
COFF, decoded sections/relocations/symbols/stack maps and Windows unwind data;
scalar/call/atomic/floating/aggregate/PHI/switch fixtures; actual direct capture;
all dispatcher kind spellings and both payload forms; ordinary malformed or
unsupported inputs; corrupt arithmetic flags; and restoration after failure.
The 31-kind by two-payload helper matrix uses mocked selectors and checks
adapter dispatch, not real emission of every opcode. Actual `va_arg` and
`syscall6` emission is outside this focused corpus. COFF unwind coverage is
whole-object byte equality plus `.pdata`/`.xdata` presence, not independent
unwind interpretation. The separate real-module gate must compare exact object bytes.

Counters must name instruction-arena and `ParsedInstr` bridge construction.
The broad `diagnostic_projections` counter changes accounting when a wrapper
is removed, so its reduction cannot be described as eliminating all decoded
operands. Current CI instruction-row counts establish coverage only, not saved
time or an Amdahl fraction. No speedup is claimed.

## Minimal staged gate

1. Materialize one ordinary independent candidate from an exact source whose
   production backend matches the manifest. Apply only `candidate.patch`;
   verify all changed and unchanged bytes/modes. Keep baseline and prior
   qualification sources immutable.
2. Run the focused host checks serially under the admitted single-process
   supervisor: 300 seconds total, hard AS 4 GiB, hard NPROC=0, shared exclusive
   lock, at least 4 GiB free reserve, durable output and exact source seals.
   These pytest arguments select no native executable test:

   ```text
   -x -n0 -vv --tb=short -o addopts=
   tests/python/test_x86_64_indexed_instruction_bridge.py
   tests/python/test_x86_64_emission_lifetime.py
   tests/python/test_stackmap_retirement.py
   -k "not retired_indexed_functions_link_and_execute_owned_elf"
   ```

3. After source review and host acceptance, prepare real `pcc.frontends.c.ast.c_ast`
   once per correct target through its normal complete single-source library
   frontend. This module imports only compiler-owned `sys`; no missing provider
   exports need fabrication. Preserve the same source filename and flags, and
   hash the complete indexed input. Compare baseline/candidate emission of
   that exact input for Linux x86 and Windows x86 with exact object bytes,
   decoded metadata and instruction-arena counters. A separately frozen driver
   and each admitted 300-second/4-GiB arm are required before this stage runs.
   No new 456-module discovery/context build is required for this scoped module.

Stop on a failed bound or contract. Do not increase caps or infer native,
Windows-device, full Stage1, five-GC or fixed-point acceptance from host object
identity. A future speed claim needs isolated, order-aware whole-worker data,
not observer-instrumented counters or the former cold-slot IR reduction.

## Bundled files

- `candidate.patch`: exact three-path production/test change
- `manifest.json`: preimage/postimage/mode identities and public scope
- `README.md`: this scope and gate proposal

The directory is a recovery artifact, not an applied production checkout.
