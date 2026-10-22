# Stage1 phase attribution on d2334624

## Finding

The new production timers distinguish two large owners that the previous
`indexed emit` line could not separate: text assembly on the non-Darwin routes,
and function setup/body plus stack-map planning inside the backend. Final object
serialization and file writes are small in the completed-worker observations.
No leaf-function speedup is established by these totals.

Source: `d2334624197b5fc14aaca93bb916683fb1afcb79`, pcc tree
`ee02ba6bf0d79e9706c1791aa650093f5f91c338`.
[Actual CI run](https://github.com/ikshengmin/pcc/actions/runs/38014379032).
The separate macOS pcc0 job passed; all four Stage1 jobs reached their existing
deadlines without a final pcc1 output. No retry or new execution was performed
for this readback.

## Non-overlapping completed-worker totals

Times below are sums of worker elapsed time, in seconds. Percentages use each
platform's sum of completed-worker `codegen_ms`. Concurrent worker times cannot
be interpreted as stage wall time or CPU time.

| Scope | macOS ARM | Linux ARM | Linux x86 | Windows x86 |
|---|---:|---:|---:|---:|
| Completed modules | 405 | 441 | 452 | 411 |
| Completed codegen | 3037.856 | 6478.148 | 6718.752 | 6228.158 |
| Frontend generation | 459.362 (15.12%) | 941.035 (14.53%) | 1314.991 (19.57%) | 934.618 (15.01%) |
| Backend verification | 268.011 (8.82%) | 600.361 (9.27%) | 766.659 (11.41%) | 551.149 (8.85%) |
| Backend layout/slots | 77.992 (2.57%) | 163.755 (2.53%) | 329.717 (4.91%) | 226.902 (3.64%) |
| Stack-map planning | 485.180 (15.97%) | 1087.563 (16.79%) | 1135.667 (16.90%) | 777.730 (12.49%) |
| Function setup | 391.542 (12.89%) | 883.153 (13.63%) | 0.377 (0.006%) | 0.483 (0.008%) |
| Function emission minus setup | 998.872 (32.88%) | 1016.047 (15.68%) | 1008.788 (15.01%) | 738.146 (11.85%) |
| Backend finalization | 192.368 (6.33%) | 303.047 (4.68%) | 1.945 (0.03%) | 305.477 (4.90%) |
| Later assembly | 0.004 (0.0001%) | 1343.387 (20.74%) | 2013.502 (29.97%) | 2554.886 (41.02%) |
| Object serialization | 52.256 (1.72%) | 69.470 (1.07%) | 64.975 (0.97%) | 12.647 (0.20%) |

Other disjoint fields and integer nanoseconds are in `PHASE-ATTRIBUTION.json`.
macOS frontend release/GC totals 73.332 seconds (2.41%); the other three routes
record zero for these optional scopes. Object writes are at most 0.024%.
Completed inference totals are only 8.809/17.018/21.077/15.562 worker-seconds in
the same platform order.

### Boundaries that change the interpretation

- `backend_call_inclusive_ns` contains verification, layout, stack-map planning,
  function emission and finalization. It is not added to those children.
  `function_setup_ns` is a child of `function_emit_inclusive_ns`; the displayed
  body residual subtracts it. Every recorded module satisfied both nesting
  checks. Backend and outer-worker remainders are reported separately.
- macOS uses native structured AArch64 sections. Instruction encoding and
  canonical append work occur during function emission; fixup/packing and
  stack-map/unwind finalization occur in `backend_finalize_ns`. Its later
  `object_assemble_ns` is only a finalized-section-list copy. The 998.872-second
  residual includes producers, operand projections, fragments, publication and
  machine encoding; it is not one hot function or a removable transport cost.
- ARM setup includes register allocation through the prologue. x86 setup only
  covers its prologue/kernel entry; its slot assignment is earlier and other
  work remains in emission. The similarly named fields do not measure identical
  register-allocation algorithms.
- Linux x86 emits packed stack-map metadata during assembly, while Windows
  still renders symbolic metadata in backend finalization. Thus the assembly
  field includes more than instruction encoding, and finalizer shares are not
  directly interchangeable across targets.
- The older `direct indexed emit` interval ends after optional post-backend
  release/GC/freeze and before the later object-assembly path. The new mutually
  separated release and object fields resolve that ambiguity.

## Coverage, retries and censored work

Only raw artifact members were read: macOS `stage1.process.tqzo2m17/target.stderr`
and the other platforms' `shared/stage1.log`. Console-tail replay was excluded.
All 1,709 recorded summaries are unique within their platform, have
`codegen_complete=1` and `clock_ok=1`, and correspond to a worker-done record.
The JSON binds every ZIP/member hash, source-file hash, phase sum and line-based
top-module observation.

There are 457 discovered roots, including the new timing helper. At timeout:

| Platform | Started but incomplete | Not started |
|---|---:|---:|
| macOS ARM | 1 | 51 |
| Linux ARM | 2 | 14 |
| Linux x86 | 1 | 4 |
| Windows x86 | 4 | 42 |

macOS cancelled and retried two genuine attempts, for
`c_declaration_lowering` and `pipeline_frontend_parallel`. Their completed
retries are counted once; cancelled attempts without a final summary are not
invented as additional phase totals. Other unfinished workers are likewise
censored. Linux ARM `hoist_lowering` and three Windows workers had printed the
older indexed-emit completion before timeout but had no final codegen/phase
summary. This is direct evidence that later work still matters, without proving
which individual later operation was active at the deadline.

The JSON names every incomplete and not-started module. A module name is the
input being compiled, not the host compiler function consuming the time.
Different completion sets, runners and admission conditions prevent a controlled
speedup claim against earlier CI runs. Enabled diagnostic overhead was not
benchmarked. These observations do not qualify pcc1 execution, fixed-point
self-hosting, helper native behavior or the full GC suite.

## At most two next source-grounded directions

These are bounded hypotheses, not implemented or admitted experiments. Neither
has a measured removable fraction yet.

### 1. Avoid discarded x86 sizing encodes on context-sensitive instructions

The shared Linux/Windows x86 assembler is a 29.97%/41.02% owner. In
`x86_64_asm_driver.py:497–585`, `_measure_sections` traverses the parsed plan and
uses full `encode_instruction(..., labels={})` results only for their length on
size-cache misses (`:547–550`). Final emission traverses the plan again and
encodes at the real PC, with the final labels and section (`:672–737`). The size
cache is bounded and per section, keyed by complete instruction text. Branches
are not separately pre-sized before this call. Consequently the extra encoder
calls number sizing misses, not every emitted instruction.

Current branch/relocation forms cannot populate the machine-byte cache, so their
sizing results construct bytes/relocation objects that are discarded before
final encoding. A shared validated instruction-shape path with an integer-size
consumer could avoid that construction. Keep ordinary cache-admitted sizing
unchanged, because it warms the already published machine-byte cache. Keep
fixed-near branch widths, instruction validation order, final PC/section/range
checks, lock-prefix offsets, relocation addends and all metadata contracts.
Do not add a second approximate length table or a whole-file decoded graph.

The smallest discriminating observation is per-file parse/layout/final-emit
elapsed totals plus sizing-miss counts split by cache-admitted versus sensitive
forms. No per-instruction clock is required. Only a substantial measured layout
fraction and sensitive-miss workload would justify implementation. Any later
candidate must retain exact ELF/COFF bytes, relocations, symbols, stack maps,
Windows unwind data and first-error behavior on malformed inputs, then pass a
predeclared uncounted end-to-end gate. The current assembler owner percentage
is not an estimated speedup.

### 2. Bound ARM allocator liveness to actual register candidates

ARM setup is a 12.89–13.63% owner. Current
`self_backend_aarch64_darwin_regalloc.py` proves redundant fact collection:
`_function_level_facts` computes block safety (`:500–515`) that allocation
recomputes (`:1328–1338`); it collects call positions which allocation rebuilds
locally (`:1354–1362`), although those local lists are unused in the default
function-level route. Fusion-use rejection and fact collection also walk many
of the same operands.

The larger algorithmic opportunity is `:633–655`: it solves backward predecessor
reachability separately for every used SSA value, before later eligibility
checks discard many values (`:1381–1499`, with argument/PHI checks at `:901–1028`).
Hoist only liveness-independent eligibility into a conservative candidate mask;
keep every use of each candidate, including uses in otherwise ineligible or
unsafe instructions. Each value's equation is independent, so an excluded
value's own solution cannot change another candidate's interval. Sharing safety
facts alone is not presented as the major win.

The early predicate must be read-only and conservative: admit unknown/fallback
shapes rather than moving `instruction_data` decoding, type interning or their
possible diagnostics ahead of their existing encounter point. A mechanically
copied version of all late eligibility code would not satisfy this contract.

Keep the work invocation-local after reload destinations are recorded. Preserve
PHI edge uses, inline-error successors, loops/backedges, layout-before-definition
rejection, call-result barriers, MADD extensions, alias/interval ordering and
stack-slot fallback. Managed-root liveness and forward root-protocol validation
have different domains and equations; they must not be replaced with allocator
facts or omitted.

The minimum falsifiable observation is `_function_level_facts` elapsed time and
predecessor visits split by the exact conservative candidate predicate. Actual
candidate/excluded coverage remains unknown. If most propagation belongs to
eligible values, stop this direction rather than tuning another worklist. Later
equality must cover intervals/register assignments, machine bytes, safepoint
roots and errors, before any performance or native-behavior claim.

## Deliberate limits

Stack-map planning is substantial on all four targets, but its managed liveness,
root-protocol validation and allocator liveness are not identical computations.
This readback supplies no safe basis for deleting one. The larger macOS body
residual also remains only a phase attribution: source shows intermediate
publication, but current timing cannot establish its removable share. Neither
is a third speculative optimization proposal.

No source mutation, compiler/test invocation, new profile or native diagnostic
was used in this analysis. Any next implementation or execution requires its
own reviewed scope under the existing resource limits.
