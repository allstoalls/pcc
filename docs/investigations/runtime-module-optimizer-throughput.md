# Investigation: runtime object emission omits full module optimization

## Status
active

## Problem Description
Continue pcc #188 after three frame protocol candidates fail to materially
improve the full workload. The latest published pcc1 gap is 1.79x. The runtime
owns almost all sampled leaves; py_obj alone has 677/2303 (29.4%), py_list
101/2303 (4.4%), py_gc_backend 254/2303 (11.0%), and the GC index table
95/2303 (4.1%). These are disjoint leaf counts mapped through the exact
baseline archive's nm inventory. Predecessor: generator-bulk-frame-save.md.

## Repro
pcc/tools/ir_to_obj.py verifies IR, creates a target machine and emits an
object. It does not run LLVM's full module pipeline. Runtime source emission
uses the frontend's bounded default mem2reg/sroa cleanup. The earlier self/
LLVM application comparison only changed emission backend; a new explicit
Clang -O2 application comparison also has no gain (51,647.0 / 51,338.8 QPS).
That does not bound optimizing the runtime's helper call chains.

## Test [CONFIRMED]
The pilot consumes the exact existing IR from the baseline runtime archive,
checks every input IR against its receipt, optimizes selected modules with
LLVM 20.1.8's O2 module pipeline, and emits objects with the existing llvmlite
target-machine path. It rebuilds/verifies archive provenance and asserts that
only selected members changed. It changes no Python runtime source and uses
no host C compiler. scripts/reoptimize_runtime_ir.py retains a complete report.

## Proposals
- No.1 optimize py_obj, py_list and py_gen [CONFIRMED for host application]
- No.2 extend to py_gc_backend and the GC index table [pending]

## No.1 optimize py_obj, py_list and py_gen
### Code Change
Diagnostic re-emission of only these three objects, preserving all other
archive member bytes. Source runtime: a8feaa180e6fc84f76c70b33-pcc-py.
Candidate: ~/.cache/pcc/gateway-optimization-20260907/runtime-ir-o2-v2.
The report retains optimizer level/version, input and output hashes, and an
exact optimizer-script.py snapshot. Initial attempt rejected the logical
source-path prefix; v2 uses the provenance module's canonical path resolver.

### CONFIRMED
The candidate passes eight field/suspended-iterator sources under GC0–4
(40 executions, 27.48 s) and the gateway failure/cancellation/rejected-fork
canary (7.72 s). Source and compiler selection are bound to the frozen
field-owners tree, whose runtime source matches the baseline archive.

All 42 full-workload A/B runs pass. Zero-wait/C100 medians are 49,193.0 /
55,135.9 / 86,648.8 QPS (control/candidate/asyncio), a 12.1% native gain.
Instructions/request fall 309,673 to 287,679 (7.1%); user CPU falls 20 to
18 us. At 100 ms: 950.1 / 960.2 / 960.7 QPS. Raw report is gateway
benchmarks/results/2026-09-07-runtime-ir-o2-ab.json. This is an application
runtime gain; fresh pcc1 application checks and production-build integration
are still pending. It does not close the whole asyncio gap.

## No.2 extend to py_gc_backend and the GC index table
### Code Change
The exact baseline nm inventory maps pointer validation/freeing to
py_gc_backend.o and GC-index removal to freestanding_gc_index_table.o.
Optimize those two additional modules with the same pipeline. The allocator
is deliberately excluded at this step: it defines malloc/calloc/free and
requires a separate no-builtin/libcall recursion check before optimization.

### pending
Candidate runtime-ir-o2-five has exactly the five selected changed members.
Run the same GC/application gates before its matched A/B. Do not label this
an accepted gain from IR-size changes alone.

## No.2 verdict [CONFIRMED for host application]
Five-module runtime passes 8 sources / 40 GC0–4 executions (26.43 s) and the
native cleanup canary (7.43 s). Its 42-run A/B uses the three-module optimized
runtime as control. Zero-wait/C100 medians are 55,401.7 / 59,032.4 / 88,549.8
QPS (control/candidate/asyncio), a further 6.6% gain. Instructions/request
fall 287,548 to 276,886; user CPU falls 18 to 17 us. At 100 ms: 937.9 /
940.0 / 946.6 QPS. Report: gateway benchmarks/results/2026-09-07-runtime-ir-o2-five-ab.json.
Do not multiply gains from different reports into a newly measured number.

## Update: normal build integration
ir_to_obj now selects the O2 module pipeline for exactly the five qualified
runtime sources when invoked with their runtime/source provenance metadata.
Other object-emission callers and libc/allocator implementations keep the old
path. This is a fixed, source-versioned policy, not an ambient compiler flag.
Runtime object codegen checksums now include the emitter implementation and
LLVM version, because frontend bootstrap identity deliberately excludes
pcc/tools. Otherwise changed optimizer policy could reuse old object receipts.
New tests cover the policy boundary, real emitted execution, elimination of
helper stack work, and cache invalidation. Normal runtime rebuild and pcc1
application qualification are the next gates; no installation promotion yet.

## Update: optimizer/provenance gates
A combined 90-second test invocation stopped at its outer timeout after 62
passing nodes, without a failing assertion or final summary. The two file
shards then completed: optimizer/provenance 52 passed (56.22 s), runtime
archive consumers 24 passed (72.44 s). No stale object/manifest bypass was
introduced. A normal cached runtime rebuild now drives the native generator/
field/ownership regressions with the fixed five-module policy.

## Update: normal runtime artifact
The regular cached build produces 0c07182db3ffebe0338c29ec-pcc-py/
libpy_runtime_pcc_py.a with the fixed five-module optimizer policy. Native
field/generator/ownership regressions pass (21 cases, 140.64 s including the
build). Prior frame experiments remain callable diagnostic oracles, while
normal generator code uses the original frame path; negative IR tests enforce
that the withdrawn flags do not activate them.

Source is frozen at ~/.cache/pcc/gateway-optimization-20260907/runtime-optimized/compiler.
A fresh Stage1 uses that source and regular optimized archive, two workers,
and an 8 GiB process-tree cap. Shared installation remains unchanged.

## Update: fresh pcc1 and final application comparison
Stage1 completed in 387.74 s, peak tree RSS 5,026,562,048 bytes. Directory:
build/gateway-stage1-runtime-o2-20260907; compiler SHA-256
2b08f3a7aac1c6a69a13ba7ee0014856d9e83fac2f48b5b9a0946909f25a4d7b.
The normal runtime archive SHA-256 is
1092c06a9c3bc55ed7ba7dd589113414710b42834449563f25b85e8f3dbeffdd.

Fresh pcc1 passes all eight field/suspended-iterator source cases under GC0–4
(40 executions, 77.43 s), and HTTP/dashboard/failure-cleanup canaries
(3 cases, 261.24 s). Gateway default tests: 290 passed, 20 integration cases
deselected (3.66 s). IR fallback plus recorded-bootstrap checks: 10 passed,
2 unavailable cases deselected (31.99 s). These recorded bootstrap checks are
not a new-source Stage2/Stage3 fixed point.

The normal-build 90-run comparison validates all 241,650 requests. Zero-wait/
C100 medians are 57,469.9 / 57,662.8 / 85,437.0 QPS (pcc/pcc1/asyncio),
with peak RSS 7.97 / 7.95 / 27.83 MiB. At C10: 55,892.2 / 55,807.9 /
48,116.3 QPS. The remaining C100 pcc1 gap is 1.48x; do not claim an asyncio
win. Gateway report: benchmarks/results/2026-09-07-runtime-o2-three-way.json/.md.
Shared installation and new-source Stage2/Stage3 qualification remain pending.

## Update: profile after the accepted optimizer policy
The fresh pcc1 application's 1M-request diagnostic completed. Its CPU profile
has 2,302 samples: object-start validation is the leaf on 368 (16.0%),
py_decref_prepare on 174, pointer-is-managed on 136, py_incref_prepare on 103,
and py_decref_finish on 71. These are sampled shares, not cross-run absolute
cost comparisons. Remaining helper chains and allocator validation warrant
further attribution; do not reopen the rejected frame micro-experiments.
Profile and folded stacks: gateway benchmarks/results/2026-09-07-runtime-o2-profile.json/.folded.

## Update: default LLVM policy withdrawn; attribution corrected (2026-09-07)

The normal-build integration in a64ad422 crossed the self-backend ownership
boundary. Its automatic five-module LLVM O2 policy is withdrawn. Keep LLVM
optimization available only as an explicit diagnostic oracle. The measurements
above remain valid for their recorded artifacts, but are not the current
default's throughput or proof of LLVM-free runtime construction.

The same commit also made codegen_checksum import ir_to_obj merely to check
cache freshness, loading llvmlite into a read-only verification path. With
llvmlite imports blocked, the new regression first failed because the checksum
became `unknown`. The fix hashes emitter source bytes without importing its
implementation; source changes still invalidate receipts. Optimizer/provenance
gates now pass 53 tests (60.07 s). A separate installed v84 pcc1 canary compiled
and ran a square-sum program (285) with host-helper llvmlite imports blocked;
no rejected imports were recorded. This is a tested compilation path, not a
clean-room rebuild of the runtime or new-source bootstrap qualification.

Attribution must distinguish available transforms from the selected pipeline.
pipeline_pass_config.py selects mem2reg,sroa by default. The self request runs
the compiled, bounded implementation in compiled_default_passes.py. These
experiments did not compare all translated pcc optimizations against LLVM O2:
both runtime A/B arms used the same LLVM target-machine emitter, with only
the candidate receiving the additional O2 module pipeline.

Exact saved IR shows pcc_gc_pointer_is_managed losing redundant boolean
conversions and branch blocks, with graph-lock wrappers inlined. py_incref's
finish helper is inlined, but its 56-byte prepared record and prepare call
remain; py_decref retains the record and both calls. Those changes explain
possible local savings, not which pass accounts for the measured total gain.

The five leading disjoint leaves in the optimized application's folded profile
sum to 852/2302 samples (37.0%): object-start validation 368, decref_prepare
174, pointer-is-managed 136, incref_prepare 103, decref_finish 71. A leaf share
cannot distinguish expensive operations from too many operations per request.
The next attribution must count task/object creation, frame operations and
ownership/validation calls per validated request in a separate diagnostic run,
then measure uninstrumented QPS. Compare the actually selected pcc transforms
on the same hot IR before changing any optimizer defaults. Preserve all five
GC and cleanup contracts; do not treat LLVM O2 as the main cause of the gap.

## Update: native emission capability and remaining pass owners

scripts/probe_pcc1_self_runtime.py makes the next boundary reproducible. Using
native pcc1 2b08f3a7aac1, all five profiled runtime sources passed source-to-IR,
ARM64 assembly and indexed PCO emission (15 successful native compiler calls).
Those calls set PCC_HOST_PYTHON and PCC_RUNTIME_CC to /usr/bin/false. The host
orchestrator prepares indexed inputs with pcc's parser/codec, with llvmlite and
pcc.llvm_capi.binding imports rejected. It structurally decodes every PCO.

The five emitted PCOs then linked into a generator canary through pcc's own
linker, with LLVM imports still rejected, and executed with exact output 42.
Remaining runtime members were prebuilt. This proves useful native emission
and execution, not a full zero-dependency runtime rebuild or O2 parity.
The linker was pcc-owned Python running on CPython, which remains a dependency
to eliminate under the maintainer's stronger pcc1 contract added to AGENTS.md.

Explicit simplifycfg and inline probes both fail in ir_pass_pipeline.py's
text runner while importing llvmlite. The current compiled default tier is
mem2reg,sroa; those additional passes are not independently executable by pcc1
through the tested entry. The needed work is to complete/wire pcc's native
optimization execution path, not install LLVM. The runtime object generator
already exists and must be reused. Core focused validation: 53 provenance/
optimizer tests, 24 archive-consumer tests and 17 default-tier/knowledge tests.

Verified report: gateway benchmarks/results/2026-09-07-self-runtime-capability.json.
The new AGENTS.md contract covers C processing too: host pcc may use only
CPython and its standard library; pcc1 may not require external interpreters,
compilers or toolchain utilities. Earlier descriptions of host helpers and
LLVM policies are historical observations, not exceptions to that contract.

## Update: native pass execution work, uncommitted (2026-09-07)

The maintainer pauses commit/push in both repositories. First complete native
execution of the useful O2 transformations, including optimizer runtime and
memory costs; only then continue the per-request ownership-cost investigation.
Do not call a subset a full LLVM O2 implementation.

The dependency-denial regression in tests/python/test_owned_ir_passes.py fails
before implementation (ModuleNotFoundError for pcc.native_ir, 0.22 s). First
proposal: extract existing scalar/CFG transformation kernels into an importable
standard-library-only package while retaining legacy LLVM verifier adapters
for differential tests. DCE must inspect owned function/instruction text rather
than call LLVM merely to enumerate instructions. Preserve volatile/atomic
operations. Validate existing semantic suites, native execution and optimizer
CPU/RSS on the exact five runtime inputs before wiring a new compiler tier.

Previous performance policy is relevant: python-ir-passes-on-huge-memory-skip-
2026-05-27.md documents expensive full/fast passes and preset skips; compiled
default passes avoid importing the full legacy analysis/harness closure.
The new route must measure parsing/traversal/allocation overhead explicitly.

## Update: owned runtime emission pilot and Codon assessment (2026-09-07)

The owned scalar/CFG/DCE/inliner kernels now execute natively. Legacy
LLVM-verifying pass adapters reuse those kernels; explicit supported self
pipeline requests execute in-process and unsupported requests fail closed.
This remains a bounded tier, not a complete O2 replacement. Native correctness
required fixes to borrowed-local rebind ownership, native re.sub result
ownership and wide-integer projection; see the three linked investigations
in docs/knowledge/2026-09-07-native-optimizer-wip.md.

Host profiling found 319 simplifycfg module splits and 142 per-function
context reconstructions in py_obj. Sharing module function attributes reduced
the native diagnostic from 4.827 s / about 960 MiB to 1.455 s / 234.5 MiB;
all five native scalar-pass outputs equal the host output. These are single
diagnostic runs, not repeated whole-compiler performance acceptance.

The exact application PCOs are held constant in the new runtime-emission
pilot. Only five runtime members differ. Seven rotating repetitions per arm,
C100/zero wait/20,000 measured requests each, yield 28 valid runs and 560,000
validated responses. Median QPS: self-control 19,763.3; self with additional
owned passes 22,185.0; historical LLVM-O2 runtime reference 57,777.4; same-run
CPython 3.15.0rc1 asyncio 86,080.5. Self-owned gains 12.3% and reduces process
instructions 5.1%. Report: pcc-gateway/benchmarks/results/
2026-09-07-owned-runtime-emission-pilot.json/.md.

The retained gateway benchmarks/build_runtime_variants.py recreated all 12
PCOs and three programs byte-for-byte. Native optimizer/emitter calls deny
host Python/cc and PATH. Host pcc parsing/codec/linker orchestration and other
prebuilt runtime members remain; this does not qualify an independent whole
runtime build. No default-policy or installed-pcc1 promotion follows.

The gap now establishes a substantial runtime emission-path contribution
with unchanged runtime source/algorithms. It does not distinguish missing IR
transforms from target code quality, and does not by itself prove register
allocation is the largest cause. The block-local register allocator's
call-crossing/PHI spills and indexed emission's optimize=False route are
specific pending candidates. Preserve stack-map/unwind offset integrity when
evaluating existing target peepholes.

At the maintainer's request, Codon source at 8057bf9856169fad6ad7dfbb60c9e3eecbcbfd46
was inspected. See [the evidence assessment](../knowledge/codon-performance-assessment-2026-09-07.md).
Its specialization, high-level typed operations, value layouts and analysis
cache/invalidation are useful references. Unconditional dictionary fusion,
fixed-width ordinary Python ints, LLVM coroutine/codegen dependencies and
erasing observable scheduling cannot be imported into pcc's contract. There
is no measured Codon binary/gateway comparison. The next ownership study must
still distinguish dynamic operations/request from cost/operation; the old
37.0% profile belongs to the historical LLVM-runtime artifact.

Validation rerun: 14 tests pass in 27.27 s with the explicit provenance-checked
prebuilt runtime, including native optimizer execution, borrowed rebind under
five GC backends, and valueclass zero-additional-allocation plus boxed escapes.
An earlier default-runtime-building packet reached 13 passes but timed out
at 180 s; it is not counted as a completed test packet. Both repositories
remain uncommitted per the maintainer's instruction.


## Update: the open question answered — target code quality, not missing IR transforms (2026-09-08)

The previous update left this undecided: "It does not distinguish missing IR
transforms from target code quality, and does not by itself prove register
allocation is the largest cause." A same-source, same-program comparison of the
two emission backends answers it.

One compute kernel (`collatz` integer loop, sieve over a list, float harmonic
sum) compiled twice from identical source with `pcc --backend self` and
`pcc --backend llvm`, outputs byte-identical (`475716 17984 13476`):

```text
                    compile wall   binary size   marginal runtime per unit
self backend            3.70 s      2,458,968     0.0835 s
llvm backend            2.42 s      2,668,184     0.0625 s
CPython 3.15.0rc1          --             --      0.0630 s (N=20 total 1.26 s)
```

Marginal cost is (N=40 minus N=20)/20, so startup and runtime init cancel; two
repetitions agreed to three significant digits. The self backend is 1.33x
slower than the LLVM backend on this kernel, and 1.33x slower than CPython,
while the LLVM backend is level with CPython.

### The GC protocol is a shared floor, not the differentiator

Disassembling the same function from both binaries gives *identical* runtime
call inventories — LLVM eliminates none of them:

```text
both arms, user_bench_collatz_steps: 17 unpin, 12 release, 10 load_ptr,
                                      7 pin, 5 err_occurred, 4 store_root_take,
                                      4 frame_leave, 2 store_root
```

A 6-second profile of the self-backend binary puts 43% of self time in that
protocol on a pure-integer kernel (`pcc_gc_load_borrowed_ptr` 15.6%,
`pcc_gc_load_ptr` 8.4%, `store_root_take` 5.2%, `granule_is_object_start` 4.2%,
`unpin` 3.2%, `pin` 2.5%, `release` 2.2%, `store_ptr` 1.6%), with the user
function itself at 25.7% and boxed `py_int_mod`/`py_int_floordiv` at 8.4%.
Because the calls are opaque, neither backend optimizes across them. Reducing
the *number* of these calls is a frontend/value-model question and is the only
route to beating LLVM; it is not what separates the two backends today.

### What separates them: 61% of the excess is frame traffic

Same function, instruction census:

```text
                 instructions   frame load/store
self                     905        261  (28.8%)
llvm                     631         95  (15.1%)
excess in self           274        166  (61% of the excess)
```

Two distinct owners, both in target code quality:

1. **Block-local register allocation.** 166 excess frame loads/stores plus
   `mov` +130 (172 vs 42) — register shuffling and spill/reload around the
   opaque runtime calls. This confirms the previously named pending candidate
   ("the block-local register allocator's call-crossing/PHI spills").
2. **`i1` ownership flags kept in byte memory slots.** The self arm emits
   `sturb` 41 and `ldurb` 33; the LLVM arm emits **zero** of either, and
   correspondingly fewer `cmp` (+33), `cset` (+26), `and` (+23) and `cbz`
   (+17). LLVM keeps these one-bit owned/borrowed flags in flags/registers;
   the self backend materialises each one through memory. That is about 74
   memory instructions plus ~99 boolean-materialisation instructions in one
   small function, and it is independent of owner 1.

### Vertical slice this names

Owner 2 is the cheaper and better-bounded slice: promote the frontend's `i1`
owned/borrowed flag slots so the self backend never materialises them through
byte memory, and verify the count of `sturb`/`ldurb` in this kernel drops to
zero without changing the runtime call inventory. Owner 1 (call-crossing
spills) is the larger but riskier one and must not be started from profile
shape alone — the census above is the measurement to re-run after any change.

Artifacts: `scratchpad/llvmab/{bench.py,bench_self,bench_llvm,dis_self.txt,dis_llvm.txt}`.
Claim boundary: one compute kernel on Darwin arm64, host `pcc` for both arms,
identical source and identical program output. It is not a gateway QPS number,
not a pcc1 claim, and not a bootstrap fixed point.


## Update: what LLVM O2 actually buys on the runtime, measured (2026-09-08)

The maintainer rejects the recorded runtime-axis gap (self-owned-target-on
29,607 QPS / 13.16e9 instructions versus matched-llvm-runtime 57,355 QPS /
5.54e9 instructions). This update identifies the single transform responsible,
with a controlled measurement instead of an inference.

### Controlled: same IR, only the LLVM module pipeline differs

`pcc/py_runtime/build_py/py_obj.ll` emitted twice through
`ir_to_obj._emit_object_with_triple`, optimization level 0 and 2, same
llvmlite target machine, same triple (`arm64-apple-darwin25.5.0`). Census of
every function in the produced object:

```text
                 O0      O2     ratio
instructions   7,058   7,252    0.97x   (O2 is slightly LARGER)
calls          1,294   1,247    1.04x   (inlining removes 3.6%)
frame ld/st      810     427    1.90x   (halved)
```

Per function, the reduction lands exactly on the ownership helpers the
application profile named as its top leaves:

```text
                                  frame ld/st      instructions   calls
_user_py_obj__py_decref_prepare      45 ->   8      148 -> 120     9 -> 7
_pcc_gc_alloc                        47 ->  10      170 -> 146    13 -> 12
_user_py_obj__py_incref_prepare      38 ->   8      125 -> 100     8 -> 6
_pcc_gc_store_plan_commit_locked     28 ->   8      101 ->  90     8 -> 8
_pcc_gc_release                      23 ->   4       79 ->  91     9 -> 9
_pcc_gc_load_ptr                     23 ->   4       86 ->  80     7 -> 7
```

So the O2 win on this runtime is **eliminating redundant stack-slot loads and
stores in the ownership helpers — 79-85% of the frame traffic in the hottest
functions**. It is not inlining (calls fall 3.6%) and it is not smaller code
(instructions rise 3%). Static size barely moves while the gateway's dynamic
instruction count falls 2.37x, which is what removing redundant memory
operations from functions executed thousands of times per request looks like.

A cross-check on the shipped artifacts agrees: baseline `py_obj.o` versus the
frozen `runtime-ir-o2-five/build_py/py_obj.o` gives frame ld/st 810 -> 415 and
calls 1,294 -> 1,246. That pair is not same-source (73 versus 70 functions), so
the controlled emission above is the measurement of record.

### Same owner as the application-backend axis

The 2026-09-08 application-backend census in the previous update found 61% of
the self backend's excess instructions were frame load/store. This runtime-axis
result is the same owner, which is why the application axis measures almost
nothing on the gateway (self 51,056 versus llvm 51,288 QPS, instructions per
request 309,574 versus 305,041) while the runtime axis measures 2.58x: the
gateway spends its time inside these helpers, not in application code.

### The missing transform is a pass pcc does not own

`pcc/native_ir/` provides `dce`, `inline`, `instcombine`, `instsimplify`,
`simplifycfg` and `integer_fold_contract`; the selected default tier is
`mem2reg, sroa`. None of those removes a redundant load from a stack slot.
These particular slots cannot be promoted by mem2reg/SROA because their
addresses escape to `pcc_gc_store_root`/`pcc_gc_frame_*`, so what is left on
the table is redundant-load elimination and store forwarding across calls that
provably do not clobber the slot (an EarlyCSE/GVN-class transform with the
alias facts the GC protocol already guarantees).

Acceptance criterion for that work, measurable without a gateway run: emit
`py_obj.ll` with pcc's owned pipeline and require frame ld/st at or below 450
(from 810) with the call inventory unchanged, then re-run the GC0-4 production
contract and the ownership regressions before any QPS claim. Do not accept an
IR-size change as evidence; the census above shows O2 wins while getting
bigger.

Artifacts: `scratchpad/o2ab/py_obj_O{0,2}.o`. Claim boundary: one runtime
module, Darwin arm64, same-IR controlled emission through the same target
machine. It does not measure the whole archive, does not prove the gateway gap
closes proportionally, and is not a pcc1 or bootstrap claim.


## Update: the owned tier promoted nothing; first increment implemented (2026-09-08)

Two facts, measured, that redirect this whole line of work.

### 1. The gap is an unapplied pass, not a missing one

`pcc/ir_passes/` holds 69 ported passes (18,163 lines) including `early_cse`,
`gvn`, `dse`, `licm` and `loop_load_elim`, each citing its upstream LLVM file.
67 of the 69 `import llvmlite.binding`, and in `mem2reg`/`sroa` llvmlite is
used for exactly one thing: `llvm.parse_assembly`. Only `constant_lattice` and
`integer_fold_contract` are dependency-free. `early_cse` and `gvn` are not
registered in the pipeline runner at all: requesting them raises "Python IR
pass 'early_cse' has no registered IR-level implementation". Their documented
subsets also exclude the relevant transform — EarlyCSE here is "identical pure
binary expressions within a single basic block", GVN "pure binops and repeated
icmp across dominated blocks". Neither eliminates a redundant load.

**No CSE or GVN is needed to match LLVM O2 on this runtime.** On `py_obj.ll`,
`ir_passes/mem2reg` + `sroa` alone reaches 414 frame load/stores against LLVM
O2's 427, with fewer total instructions (6,732 versus 7,190):

```text
arm                          instructions   calls   frame ld/st
no passes, O0                       6,981   1,294           810
llvmlite mem2reg+sroa, O0           6,732   1,295           414
no passes, LLVM O2                  7,190   1,247           427
```

So the entire O2 win on this module is available from a pass pcc already
ported. The reason the shipped objects do not have it: the runtime archive's
per-module rule (`pcc/py_runtime/Makefile:459-461`) emits `--emit-llvm` and
feeds that pre-pass `.ll` to `ir_to_obj`, which applies no IR pass pipeline and
defaults to optimization level 0. The archive members are built from
pass-free IR.

### 2. The owned, llvmlite-free tier was a no-op on real IR

`compiled_default_passes.py` is the tier pcc1 executes and is llvmlite-free by
construction. It owned `mem2reg`, yet on `py_obj.ll` it produced a
byte-identical object (810 frame ops, 75,848 bytes) because
`_mem2reg_function` rejected any slot whose loads leave the alloca's own block:

```python
if block_ids[index] != candidate["block"]:
    candidate["safe"] = False
```

Every `%x.addr` parameter spill in a real function has exactly that shape, so
the owned tier promoted zero of the 148 non-escaping scalar slots in that
module. pcc1 was therefore llvmlite-free *and* effectively optimization-free.

### Implemented: entry-block single-store promotion (owned, llvmlite-free)

`_promote_entry_single_store` promotes a non-escaping scalar slot written
exactly once in the entry block, with the store preceding any entry-block
load. It needs no dominance analysis: the entry block dominates every block,
the address never escapes, and one store means every load observes that value.
The single-block scan is now `_mem2reg_single_block_function` and
`_mem2reg_function` composes the two; both fail closed through the existing
dangling-reference check.

Measured across 40 runtime modules:

```text
                baseline     owned      llvmlite mem2reg+sroa
allocas            2,422     1,699                        35
loads             10,789     7,492                     1,942
stores             5,604     4,881                     1,468
memory ops        16,393    12,373                     3,410
```

The owned tier now removes 24.5% of runtime memory operations where it removed
0%, which is **31% of what the llvmlite pass achieves**. On `py_obj.o` that is
frame traffic 810 -> 739 (-9%) versus llvmlite's 414 (-49%). **This does not
catch up with LLVM**, and it is not yet wired into the archive build, so no
throughput claim follows from it: real builds are unchanged.

Gates: `test_compiled_default_pass_tier.py` 19 passed, with the previous
"leaves unproved control flow unchanged" contract replaced by the stronger one
(entry-block single store is proved) plus three cases that must still be left
alone — a second store in another block, a load before the entry store, and a
single store outside the entry block. `test_owned_ir_passes.py`,
`test_runtime_ir_optimization.py`, `test_py_frontend_ir_pass_pipeline.py`:
101 passed.

### What closing the gap requires

The remaining 69% is one specific algorithm, not an unknown: phi insertion over
dominance frontiers for slots with more than one store, or a single store
outside the entry block. In `py_obj.ll` those are 49 slots with two stores, 14
with three and 3 with more, plus 29 single-store slots whose store is not in
the entry block. The owned tier has no dominator tree; `ir_passes/
dominator_tree.py` exists but is llvmlite-bound, and `native_ir/ir_mutator.py`
already provides a standard-library-only IR parser to build one against.

Order of work, each independently measurable:
1. Owned dominator tree over the owned IR model (no llvmlite).
2. Single-store promotion where the store's block dominates every load
   (covers the 29 non-entry single-store slots).
3. Phi insertion for multi-store slots; acceptance is allocas per module
   approaching the llvmlite figure (2,422 -> 35 across these 40 modules).
4. Only then wire the tier into the archive build, re-run the GC0-4 production
   contract and the ownership regressions, and measure gateway QPS.

Do not measure throughput before step 4: the archive build currently bypasses
the pass pipeline entirely, so the tier's improvements are invisible to it.

## Update: the owned tier now matches LLVM's mem2reg, and the runtime archive gets it (2026-09-08)

### What was actually wrong

Not a missing algorithm.  A routing decision.

`--backend self` became the default for `pcc` and `pcc1`.  The self request
path sets `default_raw="default"` (`pipeline_pass_driver.default_raw_for_backend`),
and the dispatcher's first branch, `_compiled_default_requested`, claimed the
exact `mem2reg,sroa` manifest for `run_compiled_default_tier` -- the textual
single-block subset in `compiled_default_passes.py`.  So the weakest of pcc's
three mem2reg implementations became the one every self compile got, including
every runtime archive member.  The stronger `run_owned_passes` branch sat
directly below it and was unreachable for that manifest.

Two secondary facts, both previously recorded here incorrectly:

- The `PCC_PYTHON_IR_PASSES=default` make variable
  (`pipeline_runtime_archive.py:782`) is **not** inert.  GNU make exports
  command-line variables into recipe environments; verified directly.  The
  passes were selected, they were just routed to the weak implementation.
- `pcc/tools/ir_to_obj.py` emitting at optimization level 0 is correct and
  should stay that way.  Adding a pass option there was tried and reverted:
  it put the fix behind llvmlite, which is the dependency this work exists to
  remove.  The fix belongs in the frontend, and that is where it now is.

### The owned pass

`pcc/native_ir/mem2reg.py`, new: the full algorithm over
`native_ir.ir_mutator`'s standard-library-only IR model.  Cooper/Harvey/Kennedy
iterative immediate dominators and dominance frontiers, Cytron phi placement at
the iterated dominance frontier of the storing blocks, and an explicit-stack
dominator-tree renaming walk.  No llvmlite, no `pcc.ir_passes` import; it
compiles clean under `--backend self --python-libpython=off`, so pcc1 can run
it.  Following upstream `Mem2Reg.cpp` it promotes only entry-block allocas,
which is 17483 of the 17870 candidates (97.8%) and makes the transform correct
by construction instead of by a loop analysis.

Measured over the 170 real archive members (the 16 `pcc_gui_*` objects still in
`build_py/` are not archive members and were excluded; an earlier revision of
this section counted them):

```
                              alloca    load   store   mem ops removed   time
weak textual tier (shipped)    17218   71035   34837             0.0%     1.5s
owned native_ir.mem2reg         1730   14781    7382            79.1%     2.3s
LLVM function(mem2reg,sroa)     1728   14777    7376            79.1%     1.0s
```

Two allocas from LLVM, on real emitted IR, with zero exceptions and zero LLVM
verification failures across all 170 modules.  90.0% of the allocas are gone.

### Throughput: the gateway, controlled

`scripts/reoptimize_runtime_ir.py` gained an `--optimizer owned` arm so a
runtime archive can be re-optimized from its recorded `.ll` without recompiling
any source.  It also learned that `--modules all` means every archive *member*,
resolved from the manifest, not every object in `build_py/`.

The arms below were built the production way instead: wipe `build_py/*.o`, then
one runtime rebuild with `PCC_RUNTIME_PYTHON_IR_PASSES=off` and one with
`default`.  Both arms' 170 receipts carry the same `codegen_checksum`
(`ae203824aa5d`), so the compiler is identical and only the pass mode differs.
`pcc-gateway/benchmarks/runtime_ab.py`, concurrency 100, delay 0, 5 repeats of
5000 requests:

```
arm                                 QPS median   instructions/request
control  (passes off)                   24890                 481998
candidate (owned mem2reg,sroa)          38747                 373890
CPython asyncio                         77150                 227080
```

+55.7% QPS and -22.4% instructions per request from the routing fix alone.
The gap to asyncio narrows from 3.10x to 1.99x.

### What this does not prove

The candidate is at LLVM parity *for mem2reg*.  The previously recorded
`matched-llvm-runtime` figure of 57355 QPS came from a runtime built with
LLVM's whole O2 pipeline, so the remaining distance is the rest of that
pipeline, not mem2reg.  pcc owns ports of instcombine, simplifycfg,
instsimplify, dce and inline under `pcc/native_ir/`, and none of them are in
the `("mem2reg", "sroa")` default manifest.  Extending that manifest is a
configuration change against existing owned code, and is the next measurement.

Also unproven here: pcc1's own compile throughput.  These numbers are the
runtime the gateway executes, measured with the host compiler.  A pcc1 number
needs a stage1 rebuild against the new archive.

### A cache gap found on the way

`PCC_RUNTIME_PYTHON_IR_PASSES` is not part of object staleness.  Switching it
and recompiling reuses the cached objects, so the archive silently keeps the
previous pass mode: the first attempt at the candidate arm "rebuilt" in 3
seconds and produced the control's IR.  Wiping `build_py/*.o` was required.
The pass mode belongs in the object identity alongside `codegen_checksum`.

## Update: self versus LLVM on the gateway, one variable (2026-09-08)

The earlier arms answered "does applying our pass beat applying nothing". They
did not answer "has our optimizer caught up with LLVM's", because no arm was
LLVM-optimized. This one is that comparison.

Both arms start from the same `PCC_RUNTIME_PYTHON_IR_PASSES=off` snapshot and
re-optimize the same five profiled modules from the same recorded `.ll` through
`scripts/reoptimize_runtime_ir.py`; everything else in both archives is
identical un-optimized IR, and both emit objects at optimization level 0. The
only variable is which optimizer ran.

IR, the five modules together:

```
                alloca    load   store
baseline          1881    8047    3881
LLVM default<O2>   185    2220    1247
owned mem2reg,sroa 178    2139     918
```

Our pass removes *more* memory traffic than LLVM's whole O2 pipeline does.

Gateway throughput, `runtime_ab.py`, concurrency 100, delay 0, 5 repeats of
5000 requests, one host compiler, self backend on both arms:

```
arm                              QPS median   instructions/request
LLVM O2 optimized runtime             29744                 410672
owned pass optimized runtime          28138                 448583
CPython asyncio                       83269                 226939
```

**Not caught up: 5.4% behind LLVM, with 9.2% more instructions per request.**
And the reason is now located. It is not memory promotion, where we are ahead.
It is the rest of O2 -- instcombine, GVN and friends -- reducing *executed*
instructions on paths our pass leaves alone. That matches the five-module
instruction-count table recorded in
[owned-simplifycfg-value-namespace](owned-simplifycfg-value-namespace.md),
where the owned four-pass set reaches or beats `default<O2>` on `py_list` and
`py_gc_backend` but stays 15% behind on `py_obj` and 5% behind on `py_class`.

These absolute numbers are lower than the full-archive arms above (38747 QPS)
because only five modules are optimized here; the comparison is valid within
this pair only. Do not compare QPS across runs at all: CPython asyncio measured
77150, 83269 and 86058 in three runs of the same command today, so only
same-run pairs carry a claim.

### The three claims, kept separate

1. mem2reg: caught up and slightly ahead of LLVM, on real emitted IR, in the
   owned llvmlite-free implementation.
2. Whole-runtime optimization: not caught up. 5.4% behind LLVM's O2 on gateway
   QPS with one variable. The remaining gap is the passes we own but cannot yet
   enable (`simplifycfg`, blocked on a name collision) plus the ones we have no
   owned kernel for at all (75 of the 82 registered pass names).
3. asyncio: not caught up. The best owned configuration measured today, the
   full archive with `mem2reg,sroa,instsimplify,instcombine,dce`, reached 40981
   QPS against asyncio's 86058 in the same run, so 2.10x behind. Before this
   work the same comparison was 3.10x behind. The gap halved; asyncio still
   leads by about 2x.

## Update: LLVM O2 cannot optimize the whole runtime archive (2026-09-08)

The "5.4% behind LLVM" figure above is a five-module result. The obvious next
question is what the full-archive comparison looks like, so
`scripts/reoptimize_runtime_ir.py` gained `--llvm-all-modules-unsafe` and the
arm was built: LLVM `default<O2>` over all 170 archive members, from the same
un-optimized snapshot, objects emitted at optimization level 0, exactly as the
owned arm.

**It does not produce a working runtime.** The gateway benchmark binary linked
against it hangs; `benchmarks/runtime_ab.py` fails with a 60 s timeout on the
control arm before any QPS is recorded. Sampling the hung process puts the
program counter inside `_bzero`, in a ~170-byte window around `+0x2c`, at full
CPU.

What is *not* established: why. O2 omitted frame pointers, so `sample` reports
the frame as a direct child of dyld's `start` and the real caller chain is not
walkable. The IR does not show the obvious mechanism either: in the O2 arm
`@memset` calls only `llvm.smin.i64` and `@bzero` calls `llvm.memset.p0.i64`,
neither of which is a literal self-call, and `@bzero` already delegated to
`@memset` in the baseline. So the recursion story the allowlist warns about is
plausible but unproven, and is recorded here as a hypothesis, not a cause.

Consequences for the comparison:

- There is no valid full-archive LLVM O2 arm. The bounded five-module run is
  the only LLVM comparison that exists, and its scope has to be stated with
  its number.
- The owned tier optimizes all 170 members and produces a working runtime at
  38747 QPS. On that axis, breadth, LLVM O2 does not compete on this runtime
  at all.
- `scripts/reoptimize_runtime_ir.py`'s five-module allowlist was protecting
  against exactly this. The new flag exists so the failure is reproducible and
  named rather than folded into a comment; it stays off by default.

## Update: the LLVM full-archive hang was pcc's missing `no-builtins` (2026-09-08)

The previous update recorded the full-archive LLVM O2 arm as hanging with an
unexplained program counter inside `_bzero`. The cause is pcc's own IR
emission, not LLVM's.

A freestanding module or runtime port *is* the libc implementation: it defines
`memset`, `memcpy`, `bzero` and `memmove`. pcc emitted **no function
attributes at all** on those definitions -- no `"no-builtins"`, no attribute
groups, zero. Any conforming optimizer is then entitled to recognize the
byte-fill loop inside `@memset` and rewrite it into a call to `memset`, which
in a freestanding link is that same function. A real compiler prevents this
with `-ffreestanding`/`-fno-builtin`; the IR spelling is the `"no-builtins"`
function attribute.

Fix: `generation_lowering._mark_freestanding_no_builtins` adds `"no-builtins"`
to every defined function of a module that declares `__pcc_freestanding__` or
`__pcc_runtime_port__`. 169 of the 170 archive members now carry it. The
attribute renders after the signature's closing paren, which the self
backend's function-header decoder ignores, so the self path is unaffected.
Contract: `tests/python/test_freestanding_no_builtins_attribute.py`.

Verified: after the fix, `default<O2>` leaves `@bzero` calling only
`llvm.smin.i64` instead of `llvm.memset.p0.i64`, and the full-archive LLVM O2
runtime **runs**. That unlocked the comparison the earlier update could not
make.

This matters beyond the LLVM arm. The owned pass tier does not recognize
memset shapes today, which is the only reason the gap went unnoticed; the first
owned pass that learns to would have hit the same self-call.

### The measurement the fix unlocked

One run, one compiler, all arms together, self backend everywhere, the three
generator switches on. The `LLVM-O2 runtime` arm differs only in who optimized
the same 170 archive members.

```
child wait  concurrency      pcc     pcc1   LLVM-O2 runtime   asyncio
0           1              27898    31728             30411      8802
0           10             39587    45762             41592     50544
0           100            39097    46551             43056     80502
100         1               10.0     10.0              10.0       9.9
100         10              99.5     99.6              99.6      98.7
100         100            972.3    977.5             974.2     976.8
```

`pcc1`, the native self-hosted compiler, is the fastest pcc arm and is **8.1%
ahead of the LLVM-O2 runtime** at concurrency 100. On the whole archive the
owned pass tier now beats LLVM's own O2 pipeline on this workload. The earlier
"5.4% behind" figure was a five-module comparison against a fully un-optimized
baseline and does not describe the shipped configuration.

Against CPython asyncio: 3.2x to 3.6x faster at concurrency 1, 1.73x slower at
concurrency 100, and every arm within 0.3% once a real child wait dominates.

### What made a pcc1 arm possible at all

Stage1 self-host had been failing. The blocking defect was unrelated to the
optimizer: a dataclass field whose default is `field(default_factory=list)`
could not be omitted by an importing module. `ParsedFunction` in
`pcc/backend/self_backend_ir.py` ends with exactly that, and one construction
site omits it, so the stage1 frontend worker rejected the call with "missing
required argument". Two other sites had already been made to pass
`aarch64_tail_call_ids=[]` explicitly, which is the shape of a workaround.

The default is an AST `Call` node and the class signature is rebuilt from a
plain dictionary, so the node did not survive and `has_default` was recomputed
from it as False. Fix: `pipeline_exports.export_default_factory_name` records
the factory as a plain string, `pipeline_context` carries it in the synthesized
`__init__` signature, and `class_gen` rebuilds the call from it. Contract:
`tests/python/test_dataclass_default_factory_across_modules.py`.

Stage1 then completed: `rc=0`, 553.7 s, a 226 MB `pcc1` that compiles and runs
a program containing a `def`, and that compiles the gateway benchmark package.
553.7 s is above the 311-434 s envelope recorded for a cold stage1, so stage1
cost is an open regression question, not a settled number.
