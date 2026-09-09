# Investigation: native HTTP compilation exceeds 6 GiB

## Status
active — 2026-09-09. The earlier combined pcc1 completed the HTTP example
at 5.42 GB; later ownership and parser changes reduce the standalone server
optimizer to 816 MB. The corrected full pcc1 passes 63 native ownership cases and the gateway
canaries; its latest handler comparison still trails asyncio at C100. No shared
compiler installation, default-flag promotion or fixed point is claimed.

## Problem Description
The gateway's local HTTP example exceeds a 6 GiB process-tree cap when compiled
by a fresh pcc1. This is compiler memory, not the gateway's serving RSS. The
example imports 20 modules; its pre-pass IR totals 55,865,367 bytes. The server
module is 36,258,936 bytes, and `_proxy_exchange_attempt__gen_resume` alone is
19,174,990 bytes / 234,334 lines. Its 88 frame locals and 73 suspension sites
produce 6,424 frame-list stores. The application does not request this IR size.

This prerequisite arose while continuing [the asyncio throughput work](vthread-asyncio-throughput-gap.md).
The same-run flag A/B reaches 49,453.5 QPS versus asyncio 83,757.6 at zero wait,
concurrency 100; compiler memory fixes are not evidence of closing that gap.

## Repro
Base core commit: `d87f85940fe8fc6301f428f74e9b89bdfbac061a`, with preserved
working-tree changes. Frozen sources, binaries, raw IR, profiles, process-tree
receipts and commands are under the sibling gateway's
`benchmarks/build/2026-09-08-asyncio-challenge/`. Checked-in receipts and the
per-file source hashes are in
`../pcc-gateway/benchmarks/results/2026-09-08-compiler-memory-audit.json`.

All heavy runs use `scripts/run_process_tree_sample.py`, the performance lock,
timeouts and RSS caps. The exact executed command is embedded in each JSON
receipt. Native optimizer execution sets `PATH=/nonexistent`,
`PCC_HOST_PYTHON=/usr/bin/false`, `CC=/usr/bin/false`, `PCC_GC_BACKEND=0`.
The runtime archive is SHA-256
`31e27e4188d466a477dce18da9e91fe04dd6c49db88bb0dab6a275305339a094`.
Its matching runtime sources were copied into qualification snapshots. This
archive uses external LLVM object emission, and stage1 uses host orchestration;
these experiments do not establish the native toolchain independence contract.

## Test [CONFIRMED]
The original standalone native optimizer exceeds 6 GiB on the extracted 19 MB
function in 16.07 seconds. Samples of full HTTP compilation at 1 and 3 GiB
identify `mem2reg_text`, `MutableModule.parse`, `_collect_uses` and allocations.
The string-only repaired pcc1 reaches later object emission, but full HTTP
compilation still hits 6,557,007,872 bytes after 147.04 seconds.

## Proposals
- Balance emitted ownership at its generic producer/consumer, prove cleanup
  with bounded native regressions, and replay identical real IR [confirmed for
  the changes below; residual parser retention and full HTTP gate remain open].
- Reduce the inflated resumable function at its lowering owner [pending; no
  handler rewrite or skipped child work was introduced].

## Update: 2026-09-08 compiler pipeline and ownership fixes
The standalone driver now delegates to the production owned-pass dispatcher,
preserving the adjacent `mem2reg,sroa` unit. Native mem2reg places PHIs only in
live-in blocks; the dense text-name index mixes numeric suffix keys before
bucket selection without changing stored identity. A previously timed-out
native optimizer build completes, and seven native memory-pass probes match
host output. These are separate from handler throughput: the pruned-SSA
handler A/B is effectively flat (48,104 versus 48,674 QPS).

Four emitted ownership paths were isolated:

1. Exact string predicates left freshly sliced operands unreleased. Equality
   alone retained 4,200,000 bytes per 100,000 iterations. Operand evaluation now
   preserves source order, protects the left operand across RHS rebinding/GC,
   and cleans up on success and RHS failure.
2. The raw scaffold classifier discarded the existing native-regex NEW-ref
   fact. It now honors the emitter-proven regex rule; five native regex shapes
   include direct calls, compiled patterns and the IR parser's patterns.
3. Indexed list/tuple iteration lacked an independent iterable owner. It now
   uses an updateable owned slot, reloads it after safepoints, and clears it on
   exhaustion, break and error; function cleanup handles return. Borrowed
   sources survive rebinding. Dict-key lists explicitly transfer their owner.
4. Constructor call staging stored fresh arguments into unowned hidden slots.
   Arguments now use ordinary expression emission in source order, existing
   pointer-provenance classification and shared operand-error cleanup. Temporary
   owners survive later arguments and `__init__`, then release. Builtin
   dataclass factories record their actual NEW refs. The new instance stays
   pinned while argument finalizers run.

The constructor-only reproducer originally retained 384,056 bytes per 2,000
iterations; after repair it reports 0 then 56 bytes. A first indexed-loop
attempt double-released its transferred slot at normal exit and is rejected;
the corrected implementation clears that slot once. Separating constructor
allocation from iteration exposed this instead of accepting a compensating
double release as a fix.

Focused gates: 21 tests covering constructor temporaries, typed iteration,
existing constructor-attribute lowering and fresh-instance append passed.
Additional constructor-finalizer GC follow-up: 4 passed. These include native
GC0–4 executions, failure cleanup, identity, finalizers and weakrefs. String
predicate, regex and memory-pass receipts remain separately scoped.

## Update: identical server IR replay
The full 36 MB server module completes after the combined fixes in 72.42 s,
with peak RSS 2,574,909,440 bytes and final live requested heap 1,471,005,425.
The output is byte-identical to both earlier successful fixed optimizers:
SHA-256 `19c8dbd264941d80050315990f5a1bf934d3b2a6fd302def2b97e22f4b83c2fb`.
This is a compiler-memory improvement, not a measured optimizer speedup.
Repeated parsing of the unchanged 100-function escaped-alloca fixture still
retains about 480,339 live bytes per pass. It is not a zero-leak result.

The combined stage1 and complete HTTP compile are the next acceptance boundary;
standalone optimizer success must not be reported as the full gateway gate.


## Update: 2026-09-09 exact heap census and sampled allocation callers

The 2026-09-08 combined compiler built `local_http_app.py` in 205.69 s at
5,417,435,136 peak process-tree RSS bytes; the emitted program printed
`PCC1_GATEWAY_HTTP1_LOCAL_OK`. CPython running the same pcc source/runtime
compiled it in 93.15 s at 1,730,723,840 bytes. This is a compiler comparison,
not CPython interpreting an HTTP server. It predates the fixes below.

A diagnostic C observer walks the pinned allocator's live object slabs, raw
free lists and large managed-object index at quiescent GC0 checkpoints.
Requested/usable/mapped totals stay unchanged across each walk and malformed
header counts are zero. Ordinary libmalloc history cannot identify these
individual objects because pcc obtains slabs directly with mmap.

The 2.12 GB optimizer profile contained 4,207,412 strings and 2,112,286 lists.
Its live requested heap was 1,196,335,262 bytes. The capacity gap was largely
48-byte allocation headers (about 555 MB), size-class rounding (309 MB) and
allocator metadata (39 MB), not mostly idle cached pages. Fixing eager
`dict.get(key, [])` operand cleanup reduced lists to 164,690 and process peak
to 1.21 GB, but still left 488 MB of strings.

The subsequent same-input results are below. Units are decimal MB; each run
uses the same 36,258,936-byte server IR, the same verified historical runtime
and `mem2reg,sroa`. All outputs have SHA-256
`19c8dbd264941d80050315990f5a1bf934d3b2a6fd302def2b97e22f4b83c2fb`.

| Source increment | Seconds | Peak RSS MB | Live strings MB |
|---|---:|---:|---:|
| dict.get cleanup | 62.22 | 1,214.5 | 488.2 |
| comprehension source/element and emitted call-argument ownership | 62.50 | 1,165.7 | 471.3 |
| literal/list insertion consumes emitter-proven NEW values | 62.30 | 954.3 | 348.9 |
| early opcode rejection; lazy SROA defined-name inventory; set literal cleanup | 52.15 | 815.8 | 275.3 |

The comprehension work initially failed native optimizer emission: an f-string
conversion error bypassed its active source root and reached `err.exit` with
an inconsistent root set. Redirecting both native and CPython conversion
error paths through the comprehension cleanup resolved the emitted failure.
The failing IR and root-state diagnostic are retained in the artifact folder.

A tuple `(text.strip(),)` on a dynamic string reproduced 124,000 bytes retained
per 2,000 iterations. String-method lowering already recorded a NEW reference,
but literal insertion considered only AST types. Consumers now accept the
emitted value's ownership fact, including pending operands when a later
expression raises. Set literals use the shared retaining-insertion cleanup.
The method-factory append control passed before the change; do not describe
that small control as a newly fixed defect.

For allocation attribution, the observer samples one in 1,024 string allocations
through a diagnostic interposition of the runtime event callback and removes
samples on object death. The 471 MB-string source made 244,332,467 string
allocations during one replay; this is cumulative allocation count, not live
object count. The final snapshot has 3,765 retained samples and no table
overflow. The largest sampled caller groups involve `_split_assignment`,
`Instruction.from_text`, and module parsing. Unwinding skips some frames;
first-visible caller addresses were checked against the binary's disassembly.
These are sampled caller weights, not exact byte totals by allocation site.
The two complete input/output strings account for about 72.4 MB.

The bounded parsers previously split assignment names/RHS/indent strings before
checking the opcode. Necessary-token guards reject unrelated lines before those
allocations while preserving the original parser on every possible match.
SROA collects all defined names only once a valid aggregate candidate needs
fresh slot names. This retains pass coverage and the original failure behavior
for IR that reaches its parser, without repeatedly constructing unused pieces.

Focused follow-up: 24 tests pass, including native optimizer execution, pruned
SSA branch/loop execution, literal insertion and GC0–4 literal-error cleanup.
A separate AST alias-constructor regression reproduces `AttributeError:
properties`: its forced no-init path omitted default fields. The alias path
now uses the same complete-field guard as ordinary named class construction;
the small emitted default-field test passes. Full pcc1 qualification follows.

Compact receipts and allocation attribution summary:
`../pcc-gateway/benchmarks/results/2026-09-09-string-memory-followup.json`.
Detailed commands, observer source, frozen sources, disassembly and raw TSVs:
`../pcc-gateway/benchmarks/build/2026-09-08-asyncio-challenge/`.
The observer is an external C diagnostic; the pinned runtime uses external
LLVM emission. Neither replaces the native toolchain independence or fixed-point
qualification gates. The final candidate uses the separately pinned current
runtime archive, so its results will be labeled separately from this memory A/B.

## Acceptance boundary recorded before full qualification

- Rebuild the full pcc1 from the final frozen source and execute the changed
  ownership shapes with that compiler, including GC0–4/error cleanup.
- Rerun the complete HTTP compile and execute its protocol/lifecycle canary.
- Run matched gateway/asyncio arms and update the gateway README with current
  receipts. The last clean C100 handler result remains about 50,259 versus
  asyncio 83,742 QPS; memory improvements do not establish throughput parity.
- Residual string/object retention and repeated text representations remain
  measurable debt. No zero-leak, complete bootstrap, installed-compiler, live
  HTTP or HTTPS qualification is implied by the standalone optimizer runs.


## Update: native qualification exposed a walrus binding lifetime defect

The first full follow-up Stage1 build succeeded in 297.54 s with compiler SHA
`2dd5fcb55d8beee62c1c669ec3ffe82750c6b990a210f059c12b7117ba4f7b43`.
Building the compiler itself peaked at 6,543,081,472 process-tree RSS bytes;
this is not the HTTP application compile. Native qualification stopped after
five passing cases when the constructor-failure program failed to compile.
The top-level diagnostic was masked by `RuntimeError.__init__` raising
`AttributeError: __init__`; direct backend-worker replay of the captured IR
revealed `AttributeError: group`. Host emission of the same IR succeeded.

A small compiled-pattern reproducer confirmed the ownership edge:
`if match := pattern.match("alpha"): gc.collect(); match.group("word")`.
The direct `re.match` control passed, while the compiled-pattern form failed
under GC0 before the fix. The truth-test consumer now correctly releases a
NEW temporary, but the walrus name's plain pointer store had not acquired an
independent reference or registered that local's ownership. This had been
masked by the earlier missing temporary releases.

`stmt_misc_lowering` now normalizes managed walrus expression results to NEW
and gives each local binding an independent owned slot using the existing
replaceable-target protocol. Raw/foreign pointers retain their original
boundary. Borrowed RHS aliases, parameter rebinding, chained assignments,
false/raising truth tests and exactly-once finalization pass under GC0–4.
Four regex/walrus-focused checks and the additional alias/finalizer gate pass.

A standalone native backend built from that corrected source emits the formerly
failing 114 KB IR successfully. Its 203,023-byte assembly is byte-identical to
the host backend, SHA-256
`711b8b2c917d4dc4baf38b68b5a0f28c7b12d90048784ca8347c58fe0eecdf82`.
A second isolated full compiler build is in progress; the first candidate
remains unqualified. The error-wrapper masking defect is separately unresolved.
Receipts: `native-memory-final-gates-v2`, `native-constructor-emit-worker`,
`walrus-owner-patterns`, `walrus-owner-after`, `walrus-alias-gc`, and
`native-backend-probe-run` under the experiment artifact directory.


## Update: final scoped validation and gateway results (2026-09-09)

The corrected Stage1 compiler is
`build/asyncio-memory-walrus-stage1-20260909/pcc1`, SHA-256
`0f66351daf95cf9201f6d8e859a5107e32a9e7af2650fbff0dd24f54f61d1a65`.
Source manifest SHA-256:
`fafea499679be3130af8d773dc3b990a95872bec97d485c37e7553d94e630ba5`.
Runtime bundle identity:
`18665269c8ba5a2a054b6f51292894679fa9e794f9ae7a2e254ea4fd12af0141`;
archive SHA-256:
`1457c4e642b17f29661a2fe27bb012549c3e88942ba468364b909e9cb6a59821`.
This is the current runtime, distinct from the verified historical runtime in
the isolated optimizer A/B above. Private source defaults enable the three
previous vthread flags; live defaults and the shared installed compiler were
not promoted.

The formerly failing constructor case passes with this pcc1 under GC0–4.
The remaining native ownership matrix completes with 62 passed, one intentionally
excluded already-passed case: 63 total. The full local HTTP example compiles
in 187.11 s with 2,349,793,280 bytes peak tree RSS and prints its expected
protocol/lifecycle marker. Host pcc using the exact same source/runtime
compiles it in 81.46 s at 1,760,198,656 bytes and prints the same marker.
The earlier 5.42 GB successful HTTP compiler peak is reduced by about 57%,
but that historical source/runtime pair differs; the two current arms are
matched. Building the whole compiler itself still costs 7,331,217,408 peak
RSS bytes and remains separate unresolved work.

Gateway validation: 294 default tests passed, 20 integration cases deselected.
Native dashboard and structured failure/cancellation/draining canaries both
pass. An earlier broad `-k pcc1` attempt also matched `PCC1_...` marker text
in host parameter IDs and exhausted its outer timeout; its partial run is not
green evidence. The corrected runs select exact native parameter IDs.

The final matched 90-run handler comparison gives zero-wait C100 medians:
host pcc 48,398.1, native pcc1 48,003.8, asyncio 88,735.6 QPS. At C1, native
pcc1 is 31,189.9 versus asyncio 9,304.9. The high-concurrency goal remains
unmet. The final source/runtime identities, QPS ranges and latencies are in
`../pcc-gateway/benchmarks/results/2026-09-09-final-three-way.json`.

A fresh profile of that exact native application binary validates 1,000,000
requests and collects 3,842 CPU samples. Disjoint leaf groups attribute 22.31%
to provenance, 18.01% to refcount operations, 7.44% to graph locks and 6.61%
to retain/release/pin/load/store barriers (54.37% combined). This is owner
attribution, not permission to remove checks or proof of an attainable gain.
The earlier singleton-check hoist and adjacent frame-helper experiments remain
denied; they were not reintroduced. Closing a 1.85x throughput gap requires a
larger change to the repeated object-bookkeeping path while preserving the
five-GC and raw/managed-pointer boundaries.

The main gateway README now contains the current table, full compile-memory
measurements and qualification limits. No commit, push, shared installation,
new-source fixed point, live HTTP socket or HTTPS claim was performed here.


## Current open work

- The high-concurrency handler gap is still 1.85x; target the measured repeated
  object-bookkeeping owner while preserving all five collectors and pointer
  provenance. Prior denied singleton/frame micro-experiments remain denied.
- The final standalone optimizer still retains 275 MB of strings and the
  complete compiler's own Stage1 build still peaks at 7.33 GB. Further memory
  work needs retained-owner/IR-representation evidence, not a zero-leak claim.
- Repair error-wrapper diagnostic masking (`RuntimeError.__init__`) separately.
- New-source fixed point, independent toolchain construction, live HTTP and
  HTTPS are distinct gates not established by this run.
