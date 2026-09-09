# Native optimizer work in progress (2026-09-07)

> Historical session snapshot, not current task instructions. HEAD, dirty state,
> blockers and artifact paths below describe the recorded session. Check current
> source, `git status` and execution before reusing them; later user instructions
> take precedence. Search relevant sections instead of loading this whole file.

## Current user direction
Do not commit or push either pcc or pcc-gateway. This overrides earlier
authorization. Continue compiler work: independently perform the useful O2
optimization in pcc1, including optimizer CPU/RSS costs; then continue beyond
asyncio by separating per-operation cost from operations per request. Keep
CPython 3.15.0rc1. No subagents have been authorized. Shared pcc1 is unchanged.

## Current implementation
Core HEAD remains 080c3cf7; gateway HEAD d651689. All work below is uncommitted.
Existing kernels moved into pcc/native_ir: integer_fold_contract, instsimplify,
dce, simplifycfg, instcombine. Old pcc/ir_passes modules retain verifier/pass
adapters and explicitly re-export the shared kernels, not duplicate algorithms.
No inline or full O2 tier is wired yet. pcc/native_ir/driver.py is a standalone
PASS_CSV INPUT.ll OUTPUT.ll entry, compiled as native pcc-opt for actual runs.

DCE enumerates owned function text instead of LLVM instructions and preserves
tail-call effects plus volatile/atomic loads. CFG backreference rewrites use
matched spans; native Pattern.finditer is unavailable, so captures use findall
or search. Existing 256 scalar/CFG tests initially passed; later focused DCE/
CFG checks passed 244 tests plus 20 subtests. Native execution, not just host
tests, exposed the compiler defects below.

## Compiler fixes and evidence
1. Borrowed local -> owned copy: store_root_take consumed an unowned old value.
   assignment_statement_lowering now excludes potentially borrowed locals
   from this fast path. The existing flag-guarded replacement preserves them.
   New tests/python/test_borrowed_local_owned_rebind.py is red-before/green-after
   on all GC0–4; exact-int protocol/promotion packet stays green (4 tests).
2. Native re.sub fresh results in raw-scaffold pcc.* code were labeled borrowed.
   native_text_modules now calls _note_owned_object_value on the emitted result.
   The 32-function compiled simplifier changed from malformed/repeated functions
   to exact host equality. Do not replace this with an optimizer source workaround.
3. Native folding silently truncated i64/i128 masks due to machine-int data.
   pcc/native_ir/integer_fold_contract now uses the existing object projection
   for wide payloads/intermediates; scalar consumers preserve those values.
   The extended native/host edge-case comparison plus scalar semantics passed
   190 tests in 24.14 s; real py_gen comparison is the immediate next check.

Read the three linked investigations for exact red cases:
borrowed-local-owned-rebind-consumes-source.md,
native-re-sub-owned-result-raw-scaffold.md,
native-optimizer-wide-integer-projection.md.

## Current artifacts / immediate next action
Latest native tool: /tmp/pcc_owned_optimizer_build6_20260907/pcc-opt, copied
from the passing native regression. origin.txt binds the original path and SHA.
Run session 65822 is/was evaluating the real py_gen IR with all four passes.
Read /tmp/pcc_owned_optimizer_runtime_probe_20260907/native-wide-fixed-result.json,
native-wide-fixed-stderr.log and py_gen.native-wide-fixed.ll. Compare against
py_gen.host.ll (same algorithms); before the wide fix there were exactly four
-1 -> 0 differences. Do not claim full O2 or QPS gains from this partial tier.

Exact input: ~/.cache/pcc/gateway-optimization-20260907/runtime-ir-o2-v2/build_py/
py_gen.input.ll. Other original runtime IR and LLVM-O2 oracle outputs remain
beside it and in runtime-ir-o2-five. Native tools execute with host Python and
cc disabled and PATH=/nonexistent. The build still links prebuilt remaining
runtime components; this is not full independent runtime construction.

Before the wide fix, full four-pass py_gen ran in 1.44 s with 223,002,624-byte
sampled peak, but had incorrect constants. Do not treat that run as an accepted
performance result. Native pcc-opt originally built in ~33 s / ~3 GB through
old pcc1; compiler-fix builds currently use host pcc until a fresh pcc1 is built.

## Diagnostic cautions
- Installed and old private pcc1 binaries do not contain the uncommitted
  compiler fixes. Use a fresh build before claiming pcc1 can build the tool.
- Standalone --emit-llvm driver.py makes optimize_ir a strict-no-libpython stub;
  it is not the linked IR. Use PCC_DEBUG_SELF_IR_DUMP_DIR on a full compile.
- LLDB's frame chain skipped the actual caller of leaf pcc_gc_unpin; x30/LR
  correctly located optimize_ir. Actual IR in /tmp/pcc_owned_optimizer_actual_ir
  and /tmp/pcc_owned_optimizer_fixed_ir; inspect matching source/artifacts.
- Importing pcc.llvm_capi.binding alone does not load LLVM (_LIB starts None).
  /tmp/pcc_no_llvm_load_guard blocks llvmlite imports and ctypes LLVM loads;
  host self compilation completed with binding._LIB still None. The older
  guard blocks that own module's import too, so it is too strict for host
  construction but remains useful for the pure transformation kernels.
- Two scratch regex probes accidentally contained a backspace instead of the
  literal backslash-b; they were corrected. They did not reproduce the large
  module's raw-scaffold ownership miss; use the real 32-function regression.
- Main README remains diagnostic-only; there are no new handler QPS results.
  The original 37% profile statistic does not yet identify operation counts.

## Remaining work
Verify real five-module outputs and compiled execution, extend native arithmetic
and GC gates, port/wire remaining useful O2 transforms (including inlining) using
existing implementations, and profile optimizer overhead. Then matched self
runtime application A/B, fresh pcc1 qualification and the requested count/cost
breakdown. Keep runtime provenance honest. No commit/push or shared installation
promotion. Regenerate investigation index/knowledge pages after doc updates.

## Later progress: native inlining, shared attributes and fresh pcc1
The earlier “no inline wired” state above is superseded. Owned ir_mutator,
inline and text_tokens modules now share the existing algorithms with legacy
external-verifier adapters. Native single-block and two-exit inlining execute;
63 combined native/IR/semantic tests pass. Local-name replacement scans exact
tokens and preserves literals/comments rather than using unsupported native
finditer/backreference replacements. include_definitions=True additionally
admits bounded strong definitions while retaining exported bodies and rejecting
weak/replaced/noinline boundaries. It honors the SemanticInterposition boundary
conservatively. Reference: LLVM 20.1.8 GlobalValue.h and LLVM Globals.cpp
isInterposable; this is still a bounded subset, not full LLVM O2.

New compiler entry pcc/py_frontend/compiled_owned_passes.py owns mem2reg, sroa,
instsimplify, simplifycfg, instcombine, dce and inline. pipeline_pass_driver
routes explicit supported self requests in-process and fails closed for
unsupported requests instead of launching host LLVM. The default two-pass
fused tier remains unchanged. 111 dispatch/default/native pipeline tests pass.
Live source now also supports inline-defined, but the frozen pcc1 below was
built just before that addition and only owns the original seven names.

Profiling py_obj exposed 319 full-module splits and 142 per-function context
reconstructions. Shared function-attribute extraction removes the unnecessary
whole-module contexts now that DCE is owned. All five runtime outputs match
host byte-for-byte. Diagnostic native wall/peak RSS:
py_gen 0.609 s/59.2 MiB, py_obj 1.455 s/234.5 MiB,
py_list 2.875 s/419.5 MiB, py_gc_backend 5.412 s/886.9 MiB,
GC index 0.879 s/103.6 MiB. Earlier py_obj was 4.827 s/960 MiB and GC exceeded
2 GiB. These single diagnostic runs are not repeated performance acceptance.
Reports: /tmp/pcc_owned_optimizer_shared_attrs_20260907. Native driver optionally
prints allocator live/capacity bytes with PCC_OPT_PROFILE_MEMORY=1.

Fresh pcc1 build SUCCEEDED at build/owned-optimizer-stage1-20260907.
Binary SHA ca4b9b9e3a75aa82e1b6b20417504a26c8efce7be8488204eb1f7115acb1ff2a.
Build time 417.23 s, process max RSS 5,181,456,384 bytes. The separate tree
watch attached late (not a full-run peak claim) and observed ~5.56 GB, capped
at 8 GiB. Source v1 is frozen at ~/.cache/pcc/owned-optimizer-20260907/source-v1
and copied into the build's source-snapshot. Help and compile/run smoke gates
pass. With PCC_HOST_PYTHON=/usr/bin/false and cc disabled, this pcc1 compiles
real py_gen using mem2reg,sroa,instsimplify,simplifycfg,instcombine,dce and then
emits its assembly. Evidence: /tmp/pcc1_owned_pipeline_check.

## Immediate frontier: native runtime comparison v2
Fresh pcc1 builds the evolving native optimizer component; latest executable
is /tmp/pcc_owned_optimizer_build11_20260907/pcc-opt. It includes stronger
defined-function inlining and candidate lookup indexing (no candidate scan /
three regex compilations for every instruction).

A first five-runtime-object pilot stopped at the self IR verifier: pointer
inlining removed a call result still referenced by a textually earlier backedge
PHI. The reduced regression fails before the correction; inlining now applies
its complete replacement map to the whole function. 44 tests pass. Do not
reuse the invalid py_gc_backend-owned IR in /tmp/pcc_owned_runtime_ab.

Run session 54745 is/was rebuilding valid v2 artifacts. Read
/tmp/pcc_owned_runtime_ab_v2/build-variants.log and build-report.json. It uses
the same two application PCOs across reference, self-control and self-owned;
five runtime members are replaced with self-emitted PCOs, the rest stay prebuilt.
The reference archive is the retained LLVM-O2 diagnostic. Optimizer passes:
instsimplify,simplifycfg,inline-defined,instsimplify,simplifycfg,instcombine,dce.
Every optimizer and native PCO worker runs without host Python/cc and with
PATH=/nonexistent. Host pcc-owned parser/codec/linker orchestration still exists;
this is not a complete dependency-free runtime rebuild.

If v2 emits and links, require its three smoke logs to validate JSON/request
counts, then run rotating repeated handler timing with same-run asyncio. No new
QPS has been accepted yet. Move a parameterized reproducer/results into the
repository before reporting results; current build_variants.py is a scratch
pilot with absolute paths. Do not commit/push. General small multi-block
inlining, remaining useful O2 transforms and backend optimization parity may
still be needed before native runtime performance matches the reference.

## Later progress: validated emission pilot and Codon reference assessment

V2 is complete. Gateway benchmarks/results/2026-09-07-owned-runtime-emission-pilot.json
contains 28 rotating runs / 560,000 validated measured requests (7 repeats,
C100, zero delay). Medians: self control 19,763.3 QPS; owned passes 22,185.0;
historical LLVM-runtime reference 57,777.4; CPython asyncio 86,080.5. The owned
tier gains 12.3%, process instructions -5.1%, but remains far below reference
and asyncio. Same application PCOs, only five runtime members changed.

The scratch construction script is now parameterized and retained as gateway
benchmarks/build_runtime_variants.py. Verification rebuild at
/tmp/pcc_owned_runtime_reproduction_20260907 reproduces all 12 PCOs and all
three linked binaries byte-for-byte. Full input hashes/build report and
manifest are copied to gateway benchmarks/results, with reproduction commands
in the matching pilot .md. The script is a partial-runtime diagnostic with
explicit prepared IR/native tools/archive inputs; it is not a clean-checkout
independent toolchain builder. Original artifact timing remains valid because
the rebuilt binary bytes are equal. No new throughput run was needed.

Latest user asks how Codon can help without weakening pcc. Read
codon-performance-assessment-2026-09-07.md for the completed source/evidence
assessment. Reference clone /tmp/pcc-codon-reference-20260907 is pinned to
8057bf9856169fad6ad7dfbb60c9e3eecbcbfd46 (2026-08-29). No Codon binary run.
Do not claim Codon supplies pcc's runtime codegen or a measured gateway win.
The next concrete low-level comparison is existing self target optimization
on/off on exact identical IR: indexed native emission currently uses
optimize=False. Its effect is NOT measured yet. Block-local regalloc also
retains call-crossing/PHI stack traffic; do not call it the proven main cause.

Latest gates: /tmp/pcc_codon_evidence_gates_prebuilt_20260907.log, 14 passed in
27.27 s, with explicit provenance-checked isolated prebuilt runtime. Includes
native owned optimizer, borrowed rebind GC0–4, valueclass zero allocation and
escapes. Earlier /tmp/pcc_codon_evidence_gates_20260907.log timed out180 s after
13 passes; default runtime rebuilding consumed its budget. That run also
regenerated tracked pcc/py_runtime/libpy_runtime_pcc_py.a.provenance.json and
the ignored shared archive; do not casually restore only the manifest and make
it disagree with archive bytes. This generated change is not an optimizer
source change or a qualified independent runtime build.

All Codon work is source analysis, pcc verification and retained reproduction
tooling; no Codon/LLVM dependency was added. No commit/push, no issue message,
no installed pcc1 promotion. The optimizer O2 parity, full runtime ownership,
dynamic operation counts and asyncio performance goal remain open.

## Current user priority and later compiler work (do not stop at partial gains)

The user explicitly objected to stopping before the performance goal. Current
order: reach same-run LLVM O2 performance with self first, then consider other
compiler designs and system-level task/frame/refcount costs, ultimately beat
asyncio. NO COMMIT/PUSH in either repository remains in force. No agents were
spawned. Continue the active work; do not finalize just because a local pass or
assessment is complete.

Critical correction: the original 19,763/22,185/57,777 pilot used three older
source files than its historical LLVM archive. Differences add diagnostic
entrypoints; nonetheless the all-arms-identical-source statement was wrong.
Matched reference now overlays the five exact original LLVM O2 objects from
runtime-ir-o2-five/build_py onto the same remaining archive/app PCOs. All object
hashes match optimizer-experiment.json. Do not reuse the first pilot for
strict LLVM/self attribution; self-control versus self-owned remains valid.

Completed same-IR matrix (/tmp/pcc_ir_codegen_matrix_v2_20260907/timing.json):
owned IR/self target-on 29,674.5; LLVM O2 IR/self target-on 34,212.5;
owned IR/LLVM codegen 49,802.2; LLVM O2 IR/LLVM codegen 57,554.5;
asyncio 83,634.2 QPS. Seven rotating repetitions, C100/zero delay, 700,000
validated responses. The target-on path is pcc's existing optimize=True;
ordinary native indexed emission was hardcoded optimize=False. This separates
IR optimization from the larger machine-code gap. Matched reference binary:
/tmp/pcc_target_opts_probe_20260907/matched-llvm-reference.

Retained uncommitted compiler improvements after that matrix:
- Self verifier accepts equal PHI incoming values for parallel CFG edges and
  enforces exact edge multiplicity. Constant conditional branches with equal
  targets retain both edges during parsing; other constant folding unchanged.
  Exact LLVM O2 IR previously failed in py_obj on this legal switch/PHI shape.
- Small nonvolatile zero memset <=128 bytes emits exact scalar stores, retaining
  SIMD aligned cases and other fallbacks. This exposed missing LDRH/STRH and
  LDURH/STURH encoding; both text and packed paths now support them. A separate
  explicit LLVM20.1.8/llvmlite0.47 byte oracle passes. The old test corpus gate
  pins llvmlite0.46 and was deselected, NOT run. /tmp/pcc_halfword_reference_20260907.json.
- Owned InstructionSimplify removes identity casts and zext-i1/icmp round trips,
  resolves arbitrarily long aliases and uses lexical token replacement. Native
  optimizer14 work below also adds inverse integer comparisons through xor true.
- AArch64 folds adjacent, sole-use constant byte-GEPs into scalar memory offsets
  using indexed records, preserving original metadata positions. Both native
  emission modes, negative/unaligned offsets, i8 index sign extension, byte guards,
  other uses and stackmap reload fragments are gated. No register allocator
  scope expansion is active.

Denied candidate: block-local integer call-result register allocation. Its first
application failed AttributeError: TaskScope; the selected slot helper didn't
publish x0 into the chosen register. A custom ABI callee x0=33/x1=99 reproduced
that error. After fixing publication and excluding intrinsics, 22 tests and
real applications passed, but owned-IR QPS regressed 29,636.2 ->29,013.3 (-2.1%)
with just -0.26% instructions. Allocation/publication changes were surgically
withdrawn, leaving useful ABI execution regression. Current regalloc.py is
unchanged from HEAD. Negative artifacts: /tmp/pcc_call_result_ab_v2_20260907;
saved patch /tmp/pcc_call_result_candidate_with_memset.patch also includes
the separate retained memset change, so never apply wholesale.

Accepted finite measurements (same-run witnesses, do not multiply gains):
- small memset: owned IR flat 29,763.5 ->29,840.6; LLVM-O2 IR/self codegen
  34,344.5 ->35,170.4 (+2.4%), -4.2% instructions.
- identity/boolean canonicalization: 29,233.1 ->31,675.7 (+8.4%), instructions
  13.162B ->11.573B (-12.1%); LLVM O2 57,525.8, asyncio83,073.7.
- GEP memory operands: 31,972.8 ->32,867.2 (+2.8%), instructions
  11.573B ->10.694B (-7.6%); LLVM-O2 IR/self35,029.2 ->35,499.1;
  LLVM reference57,915.3, asyncio85,940.2. Still NOT performance parity.

Native target-on qualification is real but not an end-to-end compiler claim:
owned_emit_driver.py exposes emit_indexed_module_file(..., optimize=bool),
default stays False. Host standard-library pcc built the native tool with
LLVM imports/loads denied. Latest /tmp/pcc_owned_emit_address_fold_20260907/pcc-emit
SHA4ad5e1cd34c90ac53205e61d8ce853ae574d5a0656a0a637d4a72a6e1c9881a0.
It emits all five native-optimized modules with PATH=/nonexistent and host
Python/cc disabled. Every PCO is byte-equal to host emission. Per-module times
~1.14/1.55/.35/4.14/.49s; reports /tmp/pcc_native_address_emission_20260907.
The canonicalization inputs are /tmp/pcc_canonicalization_ab_20260907/*.ll.
Corresponding host expected PCOs: /tmp/pcc_address_fold_ab_20260907/owned-*.pco.

Native compiler/tool construction debts found:
- Direct pcc1 build of emitter exceeded an initial6GiB cap at72.7s. Existing
  direct indexed capture/emit/require-zero-fallback/fuse-uses/release-frontend
  flags avoid the text accumulation; all codegen/PCO workers complete in the
  retry (~155s), but final link fails with the existing host helper disabled.
- Even print(42) reproduces that host-link dependency with PCC_HOST_PYTHON=false:
  /tmp/pcc_native_link_boundary_20260907. pipeline_self_backend_link.py explicitly
  invokes host Python. Native error formatting reports __init__, masking the
  subprocess failure. No claim of zero-dependency complete linking.
- owned_link_driver.py is an UNWIRED candidate wrapper over the existing pcc
  Mach-O parser/assembler/linker. Host bootstrap failed at macho_link.py's
  bytearray slice assignment, unsupported by the Python frontend. Do not rewrite
  the linker around a compiler semantic gap or weaken bytearray alias semantics.
  No bytearray runtime change has been made. Logs /tmp/pcc_owned_tools_bootstrap_20260907.
  Focus remains LLVM/self compiler performance per latest user priority.

Immediate active correction: inverse-comparison simplification exposed a latent
multi-block inliner bug. It shortened while.cond.9961 to invalid9961.i. Both
host/native outputs were equal but LLVM and self reject them. Invalid report
/tmp/pcc_inverse_cmp_ab_20260907 complete=false; timing never ran. Current
inline.py preserves complete original labels, collision-checks caller SSA/block
names, uses legal call prefixes, avoids hardcoded-r1 collisions, and handles
canonical assigned-but-unused nonvoid calls without replacing the caller's
unrelated return. Compatibility keeps upstream short block names when safe,
so the structural parity tests still check CFG edges without weakening them.
Tests: tests/python/test_owned_inline_labels.py plus legacy inline suites.
Latest gate /tmp/pcc_inline_labels_green_v4.log; read final status.
Need a fresh native optimizer after this final label fix, extend exact five-module
host/native + LLVM/self verification, and only then rerun the inverse-comparison
QPS experiment. Build13 predates the label fix and produces invalid py_gc IR.
Native regression tests compile to the pytest-current simplify_driver; copy the
successful new binary to a new /tmp/pcc_owned_optimizer_build14_20260907 path
and bind SHA/origin, as done for build12/13. Existing native emitter4ad5 can be
reused since only IR transformation kernels changed.

Potential next codegen hypothesis (NOT implemented): memory selection still
moves already allocated pointer/value registers into x9/x10, and loads into
x10 then copies/masks into the already allocated destination. Reuse the actual
allocated operands and xzr/wzr for zero stores, preserving the existing allocation
scope, type checks and alloca-address priority. This is simpler than expanding
global register allocation. Keep one candidate at a time and qualify native output.

Regenerate investigations index and distilled pages after these current docs.
Many latest detailed artifacts remain /tmp; the original generic variant builder
and artifact timing runner are retained in gateway. New matrix/canonicalization/
address construction needs a parameterized retained reproducer before publication.
Do not finalize or promote installation merely because local tests pass.

## Update — memory/ALU qualification and sized-bytes blocker

Still NO COMMIT/PUSH, no installed promotion. User insists self reach same-run
LLVM O2 before runtime-cost redesign; task remains active and far from parity.

Memory register selection passed scoped code-converge static review, 362 broad
tests plus 29 follow-up cases. Native emitter
/tmp/pcc_owned_emit_memory_register_20260907/pcc-emit SHA
44742eb02a20d28f95ab6ca4332b63fb568e4c0ba68212f21201368c6714c606 emits all five
OWNED-IR PCOs byte-equal to host. 30 application smoke cases across GC0–4 pass.
Quiet timing /tmp/pcc_memory_register_ab_20260907/timing-quiet.json:
owned 33,477.3 → 33,449.9 (flat), LLVM-IR/self 35,204.5 → 35,571.4,
LLVM O2 57,533.1, asyncio82,218.4. Memory change reduces instructions 1.4%, no
accepted owned-pipeline QPS gain. Earlier timing.json was heavily contended by
another project's golangci-lint PID83146 (~6–7 cores); retained as invalid for
throughput acceptance. User stopped it and process disappearance was checked.
Do not signal/resume anything: we did not suspend or terminate that process.

New scalar ALU selection in compute.py / ops.py: only i32/i64; use existing
assigned x1–x8 operands/results, default x9/x10/x11 unchanged for unsupported
expressions/narrow ints. Small signed -4095..4095 add/sub uses immediate;
negative literal flips opcode. Remainder quotient always x11 before final
potentially aliased MSUB. Allocation lifetimes, MADD/GC untouched.
Tests tests/c/test_self_backend_aarch64_alu_registers.py execute all13ops,
width8/16/32/64, both optimization modes, lhs/rhs/no result aliasing and
immediate edges. Old register-number assertions updated to semantic width/
opcode shape, preserving execution/no-fusion checks. 399 tests passed in8.12s
(/tmp/pcc_alu_broad_v2.log). code-converge reviews clean after rhs-alias canary.
Native emitter /tmp/pcc_owned_emit_alu_register_20260907/pcc-emit SHA
19e7a8153666fd7e3d453aee9f1a8634bc4764f8c73dab87e6db087895e6f3d4,
host bootstrap58.72s with LLVM imports/loads denied, prebuilt runtime.

Gateway NEW reproducible builder benchmarks/build_codegen_matrix.py takes
manifest pcc_root/runtime_archive/app_objects/variants; each self variant has
native emitter and runtime_ir map; reference has reference_objects and explicit
reference_provenance. Hashes inputs/source before/after, denies LLVM hosthelper
imports, native PATH nonexistent + host Python/cc false, pcc-owned linker,
validates smoke counts. /tmp/pcc_alu_codegen_reproduction_manifest_20260907.json
builds /tmp/pcc_alu_codegen_reproduction_20260907. All OWNED-IR PCOs and executable
match host /tmp/pcc_alu_register_ab_20260907, external reference exact byteequal
/tmp/pcc_target_opts_probe_20260907/matched-llvm-reference. LLVM-IR native PCOs
py_obj and py_list DO NOT match host (below), others equal. The timing finished
before mismatch triage: owned33,468.4 →33,549.6(flat), LLVM-IR/self35,460.1
→36,376.9, LLVM57,496.5, asyncio83,328.9. **Native LLVM-IR arm NOT qualified**.
Owned instructions10.395B →10.220B; LLVM-IR10.218B→9.886B. No parity claim.

### Native mismatch now being fixed — actual root is sized bytes constructors

Old memory emitter reproduces py_list mismatch too: native loads x10=0 for
18 vector constant lanes whose host values are1..15 and1..3. ASM diffs
/tmp/pcc_host_llvm_py_list.s vs /tmp/pcc_native_llvm_py_list.s. Two py_obj
functions pcc_gc_store_root and pcc_gc_collect each differ by4bytes; not yet
localized or proven same cause.

Investigation docs/investigations/self-native-aggregate-literal-masks.md:
initial mask hypothesis DENIED. Actual native serializer agrees with host for
individual i64/128 cases (note TypeDesc currently caps wide slot to8bytes;
these are host/native equality tests, not an i128 ABI claim). Array TypeDesc
reports correct array,0,4,32 in both, but native aggregate bytes is b''.
Root: py_obj_stubs.py / C py_bytes.c lack integer-count branches in
py_bytearray_from_obj and py_bytes_from_obj. _bytes_data(int)=null and
py_bytes_len(int)=0. Thus bytearray(32) becomes empty, serializer stores fail
silently. Direct before probe prints0,0,b''; after prints32,32,four zero bytes.

NEW edits not yet fully qualified:
- Both runtime mirrors add generic integer/bool count constructors, explicit
  zero filling, negative ValueError, conversion OverflowError and guarded
  allocation-size arithmetic/failure. Helper _bytes_from_integer_count.
- builtin_type_attr_lowering.py adds post-call error checks for one-arg
  bytes/bytearray constructors so exceptions actually propagate.
- Both runtime py_bytearray_setitem now normalize negative indices and raise
  IndexError / ValueError on invalid index/value (old code returned-1 silently).
  This was found by broader constructor/mutation regression.
- tests/python/test_native_bytes_count.py covers sizes,bool,zero-fill,
  independence,negative indexing,negative/huge counts and mutation errors GC0–4.
  Test originally used bytes repetition by bool, exposing an UNFIXED separate
  bytes*True bug. Changed constructor oracle to multiply by len(immutable),
  recording separate gap; do not claim boolean repetition fixed.
- pcc/backend/owned_literal_driver.py generic KIND WIDTH COUNT LITERAL prints
  TypeDesc fields/slot_size and serializedbytes; tests/python/
  test_native_aggregate_literals.py compiles it and compares host/native.
  First /tmp standalone driver lacked pcc module closure and failed import;
  real repo driver used subsequently. No serializer/mask logic changed.
- tests/python/conftest.py NEW pcc_diagnostic_runtime_archive fixture accepts
  explicit PCC_DIAGNOSTIC_RUNTIME_ARCHIVE with .diagnostic.json verifying
  archive SHA and current source hashes, otherwise delegates normal fixture.
  This does NOT qualify a production runtime manifest or change old fixtures.

Runtime build artifacts:
/tmp/pcc_bytes_count_runtime_20260907 (v1 constructors only)
/tmp/pcc_bytes_count_runtime_v2_20260907 (also negative-index/mutation errors).
Native frontend command uses frozen stage1 pcc1 with --backend self
--python-library --emit-llvm=... pcc/py_runtime/py/py_obj_stubs.py and host
Python/cc false. Current emitted IR ~1MB. Host codec prepares sidecar; above
native ALU emitter emits PCO. **Target-on fails**:
`target-final exception successor label missing for
'user_py_obj_stubs__bytes_from_integer_count'/'if.else.4039'` (v1 names).
Target-off succeeds. Cause candidate: _drop_unreferenced_empty_local_labels
only preserves L_pcc_smap anchors, not block labels referenced by exceptional
successor metadata; trampoline threading/removal erases otherwise empty block.
Must preserve/resolve metadata labels generically, never disable final check.

v2 finish.py (in /tmp folder) builds self target-off PCO and converts through
NativeObject.to_macho, reads old archive with pcc macho_archive.read_archive,
replaces py_obj_stubs.o and writes BSD ar headers itself (no ar/cc/LLVM).
170 members total,169 unchanged historical. The new isolated archive has a
.diagnostic.json receipt binding source/artifact/native-emitter identities.
It is a diagnostic overlay, no production manifest. Current provenance tools
ONLY accept LLVM object_emitter and invoke external ar; NOT modified yet.
Do not forge a LLVM receipt for the self member. compile_python explicit
runtime_archive accepts the diagnostic archive; raw link_args --native-object
are rejected by self-link contract, so earlier injection attempt was dropped.

ACTIVE TEST session78608:
gtimeout180s env -u LC_ALL
PCC_DIAGNOSTIC_RUNTIME_ARCHIVE=/tmp/pcc_bytes_count_runtime_v2_20260907/libpy_runtime_pcc_py.a
uv run pytest tests/python/test_native_bytes_count.py
 tests/python/test_native_aggregate_literals.py -x -n0 -vv --tb=short
log /tmp/pcc_bytes_count_aggregate_green_v2.log.
Prior run (/tmp/pcc_bytes_count_aggregate_green.log) failed on negative index
and boolean repetition oracle; scalar constructor counts/zero fill and
negative/overflow exceptions were correct. v2 fixes now need final result.

NEXT: finish constructor/serializer native gates; rebuild native emitter with
isolated corrected archive (explicit runtime_archive) and recheck both sets of
five PCOs. Fix target-on missing exceptional block label with minimized gate;
then restore full target-on runtime/emitter qualification. Localize remaining
py_obj4+4byte mismatch separately if it persists. Continue same-run LLVM O2
parity work; do not finalize just because one fix is done. Latest readme/main
has not been updated, result index/generated investigation pages are stale.

### Latest continuation state

Constructor/serializer native gates now **2 passed in22.86s**:
/tmp/pcc_bytes_count_aggregate_green_v2.log, includes all5 GC constructor runs.
Native emitter built using the corrected diagnostic runtime:
/tmp/pcc_owned_emit_bytes_count_20260907/pcc-emit SHA
5797acc26fc88eca97bda46e2306af2034554abf674be9e2261daeeb8dadde84 (53.98s).
Re-emits LLVM py_list **byte-equal host now**; py_obj still differs.

Py_obj ASM diff is serious **cross-function branch corruption**, not simple
instruction selection: /tmp/pcc_host_llvm_py_obj.s vs
/tmp/pcc_native_llvm_py_obj.s. Native adds b.ne from pcc_gc_store_root into
pcc_gc_collect_if.end.8.i; adds b.ne in pcc_gc_collect; and replaces a loopback
`b L_pcc_gc_collect_while.body.2057.outer` with `b.ne` into
py_gc_callbacks_list_if.end.1664.i. Looks like overwritten/dangling string
entries in target peephole lists, but this is still a hypothesis. Need trace
native pass-by-pass from common input ASM and minimize actual stage before
editing ownership. _retarget_branch returns new concatenations or borrowed
inputline; _fold_cond_branch_to_fallthrough constructs replacement fstrings.
Agent memory_integration is doing read-only source/ownership/prior-doc review
for this native conformance failure; no agent writes/heavy tools.

Separate metadata-label bug reduced and fixed (host gate):
- /tmp/pcc_exception_label_repro.py failed before source edits with
  target-final exception successor missing check/exception.
- docs/investigations/self-exception-successor-label-elision.md
- tests/c/test_self_backend_exception_labels.py executes both clear/raised
  paths and verifies ASM/indexed emission.
- self_backend_aarch64_darwin.py collects exceptional successor blocklabels
  from packed or legacy plans, seeds them into referenced sets in BOTH
  _thread_trampoline_branches and _drop_unreferenced_empty_local_labels.
  Only protecting final cleanup failed (threading already deleted blocks),
  recorded /tmp/pcc_exception_labels_green.log; both stages now pass.
- /tmp/pcc_exception_labels_green_v2.log **362 passed7.11s**, precise ABI,
  record ordering, codec and broad backend execution included.
- Fresh native emitter with label fix and corrected runtime:
  /tmp/pcc_owned_emit_exception_labels_20260907/pcc-emit SHA
  0197d6a3345f31969b19157c4b252bdd594af41959be65e0a9b3e57fd79c59e6,
  built51.86s. Active runtime target-on emission is writing
  /tmp/pcc_owned_emit_exception_labels_20260907/py_obj_stubs-target-on.pco,
  log runtime-emit.log. Check completion before claims. Target-off diagnostic
  archive remains the emitter's runtime dependency, not a default/installed
  archive. Need host-byte equality + execution for target-on emitted module.

No edits yet to ownership for cross-function branch corruption. Read
native-re-sub-owned-result-raw-scaffold.md end-to-end (same-class prior UAF,
already fixed). Relevant older tuple ownership investigation is
pcc1-tuple-unpack-self-host-str-counter-corruption.md (580lines) and should be
read end-to-end before any matching proposal; root has NOT read it in this
latest continuation yet. Do not guess from native branch text alone.

### Conditional ownership return now fixed; full helper replay green

Native runtime target-on emission with metadata-label emitter succeeded:
/tmp/pcc_owned_emit_exception_labels_20260907/py_obj_stubs-target-on.pco
672198bytes. Host-byte equality/execution not yet qualified for that object.

Cross-function branch problem is now reduced/proven/fixed at frontend owner:
- Native replay driver pcc/backend/owned_target_pass_driver.py retains stage
  snapshots. First failed on blank-line encoded empty edges; fixed artifact to
  empty file (no compiler change). Thread output was already bad and later
  snapshots stable. Synthetic16function test did NOT reproduce.
- /tmp/pcc_target_pass_trace_20260907/minimize.py reduces18,236lines to5 in
  44calls,<1s. Native `b L_target` becomes just `L_target`. Formal regression
  tests/python/test_native_target_pass_lifetimes.py uses renamed same-length
  identifiers and failed in0.11s with old driver.
- Actual driver compilation IR captured after explicitly creating dump dir
  (debug_dump_ir_texts does not mkdir; earlier missing-dir capture silently did
  nothing). /tmp/pcc_target_pass_trace_v2_20260907/compiler-ir/
  self_backend_input_1.ll plus extracted helper .ll files.
- LLDB release-caller.log proves saved fresh branch hits fatal pcc_gc_release
  from _thread_trampoline_branches+7224. Leaf watch unwind omitted _thread;
  do not misread main frame as proof pass already returned.
- Actual `_resolve_trampoline_target` IR current=target is borrowed, flagfalse;
  only loop iterations setowned. Zero-iteration return skippedretain because
  return_lowering relied on static _owned_local_names. Caller releases target
  and resolved as twoownersforone. This is concrete missingretain, not target
  algorithm bug. Error-cleanup blocks are not normal duplicate releases.
- return_lowering._retain_borrowed_return_value now consults the physical
  alloca's runtime ownedflag, retains falseedge and transfers trueedge, joins
  as explicitlyowned phi. CPython/unsafe/suppression/ledgerowned bypasses
  preserved; stale physicalflag missing=>borrowedretain. No perhelper specialcase.
- Ordinary regression tests/python/test_conditionally_owned_return.py crashed
  (-11,1.85s) before; zero/nonzero-loop string lifetime nowpasses5GC. Added
  destructorcount test detects leaks onownedbranch: cumulative1then3, all5GC.
  /tmp/pcc_conditional_return_finalizers.log 2passed3.50s.
- Fresh native5line replay + raw C ABI borrowedparameter IRgate:
  /tmp/pcc_conditional_return_native_20260907/tests.log 2passed55.76s.
  copied freshdriver to /tmp/pcc_conditional_return_native_20260907/pcc-target-pass,
  driver-receipt.json. Full18,236line replay now EXACT host equality at thread,
  fold anddrop stages (/tmp/.../full-{thread,fold,drop}.s).
- code-converge all3reviewers read conditionalreturn implementation; no code
  defect found. Validation review requested finalizercounter; it is nowgreen
  and needs follow-upreadback only. No heavyagenttests/edits.

ACTIVE native emitter rebuild with both correctness fixes +conditionalreturn:
/tmp/pcc_owned_emit_return_ownership_20260907, session80437,
source build.py uses corrected v2 diagnosticruntime archive, denied LLVMloads,
RSSlimit6GiB timeout240. Watchdog/build-report files there. Once done, redo
ALL5 OWNED +ALL5 LLVM-O2 IR nativePCO equality with host current backend;
py_obj crossfunction mismatch should nowdisappear. Native qualification still
not fullStage2/3 or installedpromotion. NO COMMIT/PUSH. Userexpectscontinued
LLVM-O2 parity work, not finalstatus stop. MainREADME andgenerateddocs stale.

Test naming correction: the new sized-constructor regression is now `tests/python/test_native_bytes_sized_constructor.py`. The existing `test_native_bytes_count.py` tests `.count()` and its original contents are preserved unchanged. Earlier local log commands used the temporary conflicting filename.

## Maintainer pause / architecture-priority steering — 2026-09-08

Latest user asks whether self can reach LLVM O2, proposes pausing this round
and resolving pcc architecture first, while insisting an independently ported
LLVM O2 should have equivalent capability. Root paused implementation/timing to
answer; do not auto-resume gateway QPS micro-optimizations from the earlier
continue messages without incorporating this newest steering. NO COMMIT/PUSH.
There is no completed LLVM parity claim or installed-toolchain promotion.

CPython capability question was answered with actual3.15.0rc1 probes:
import pcc, owned instsimplify and self AArch64 emitter work; vt.call returns42
as a synchronous host adapter. vt.spawn, unsafe.null and calling an ExternFn
all raise NotImplementedError. Compiler algorithm modules are callable from
CPython; the entire pcc native API is not an ordinary interpreted library.
User twice clarified this is a compiler project, unrelated to cyber. Keep
work on compiler semantics, ABI/GC correctness and performance; no unrelated
security workflow or framing.

### Tail-call candidate implementation and current qualification boundary

Files: new self_backend_aarch64_darwin_tail_calls.py; ParsedFunction adds
`aarch64_tail_call_ids` at the end; AArch64 emitter clears IDs before initial
complete root plans, plans finite scalar tails when optimize=True, rebuilds
only affected plans omitting ordinary post-call return-PC records, and closes
old packed plans. Native AArch64 precise planner skips only proven tail call
IDs; x86 path unchanged. Eligibility excludes allocas, aggregate caller args,
varargs, any body LLVM intrinsic/frame protocol, all root locations/reloads/
exception states, and target calls with any special flags; outgoing args<=8,
i32/i64/ptr only, matching immediate return i32/i64/ptr/void. Calls emit argument
preparation; return terminator restores frame/LR and emits B. Normal mode
retains ordinary calls. Call records cannot be retained for tails: zero-arg
entry/call anchors coalesced and strict PC validation caught it; no validator
was weakened.

External B was previously unsupported and cross-atom B was incorrectly inline.
arm64_encode now shares B/BL atom-relocation rules while retaining distinct
opcodes; text, emitted records and packed deferred fixups handle BRANCH26.
Local/recursive jumps stay relative, unknown L-local jumps still fail. Existing
old oracle test's unsupported external-B case becomes unknown-local-B; it is
still deselected by the existing unavailable LLVM gate, not counted passed.
New tests/c/test_self_backend_tail_relocations.py tests bytes/relocs and links
and executes two native objects in both input orders. No external assembler
or LLVM was used to produce the candidate objects.

Host gates: /tmp/pcc_tail_full_gates_v2.log480passed7.74s; subsequent review
packet /tmp/pcc_tail_review_gates.log11passed0.25s. Weighted8-register forwarding
now checks81234567 after reversal (the original simple sum did not test order).
Includes100k recursion, no-arg calls, internal retained-arena reuse, ABI
indirect/FP/aggregate/caller-vararg exclusions, local address/stackargs and
special protocols, valid registered-frame source retaining BL +3decoded
records including rooted call. Public indexed transport consumes native
arenas; an early reuse test incorrectly reused it after close, then changed
to the explicit close_native_tables=False internal lane. No API lifetime
contract was relaxed. Three code-converge reviewers clean after test gaps fixed.

Important file preservation correction: initial new sized-byte test reused
an EXISTING filename test_native_bytes_count.py (which tests .count()). Its
original HEAD content was restored; git diff for that file is empty. New
constructor test lives in tests/python/test_native_bytes_sized_constructor.py.
Earlier local logs use the temporary conflicting filename. Do not delete or
replace the original .count() tests.

### Most recent measurements

Fully native-qualified pre-tail matrix:
/tmp/pcc_return_codegen_reproduction_20260907/build-report.json +timing.json.
Native emitter SHA a9084302d5ecaee95458cf543aa0a0e383b7f30a50733b1f99c82635de228990.
All five OWNED-IR and all five LLVM-O2-IR native PCOs are byte-equal to host
ALU-era artifacts; external reference binary byte-equal too. Seven repeats:
self-owned33,639.1; same LLVM-O2 IR/self36,390.2; LLVM57,475.7; asyncio81,537.9.
Thus same-IR self is63.3% of LLVM QPS and needs about58% improvement. This is
fixed app PCOs/five runtime members/remaining archive, not whole toolchain
independence. No allocator/task algorithm attribution beyond the codegen layer.

Tail host A/B /tmp/pcc_tail_call_ab_v2_20260907/build-report.json +timing.json:
owned33,318.7→34,354.2 (+3.1% median), LLVM-IR36,435.2→36,834.1;
LLVM57,984.6, asyncio81,494.9. Instructions10.2208B→10.1923B (-0.28%) onowned,
9.8815B→9.8607B onLLVM IR. Ranges overlap and there are low outliers; improvement
needs repeated qualification, not a robust speed-win claim yet. First timing
attempt correctly failed on shared performance lock held briefly by a separate
pcc artifact-inspect build; it had released before retry. Do not kill others'
processes or bypass that lock. Core may have concurrent user/other-thread work.

Tail native emitter build FAILED; /tmp/pcc_owned_emit_tail_calls_20260908:
`codegen[pcc.backend.self_backend_parse]: missing required argument
'aarch64_tail_call_ids' (kwargs=22,formals=24)` and analogous indexed_codec
kwargs23/formals24. Native compiler-model construction explicitly initializes
existing mutable target fields; the new field has not been wired through the
parser/codec construction paths. Read constructors and established native-model
contract before fixing (likely missing field plumbing). Do not claim tail
transfer in pcc1 from host A/B. Build session67091 finished with failure; no
ongoing root timing/build process is needed for the pause.

### Pending architectural work, not yet implemented

Self register allocation remains block-local x1–x8, excluding cross-call and
cross-block lifetimes. Full LLVM O2 parity has not been implemented/proven:
owned tier is finite, target instruction selection and machine optimization
still have substantial gaps. Tail lowering is one incomplete candidate.
Potential next architecture audit: enumerate actual pass coverage/order,
analysis preservation, global register allocation/callee-saved registers,
call/frame lowering, instruction selection and optimization inspection.
Do not assert one cause explains the entire measured gap without attribution.
LLVM runtime reference lacks the self default's all-function PAC policy; policy
contribution was noted earlier but not separately measured. Do not silently
change policies to claim parity.

Other remaining tasks: native linker still delegates to host; owned linker
wrapper blocked by bytearray slice assignment (sized constructors now fixed).
Production runtime provenance still hardcodes LLVM emitter and external ar;
diagnostic overlays were honestly labeled, not forged as qualified manifests.
Fresh pcc1→pcc2→pcc3 and full5GC qualification remain open. Latest optimizer
build14 predates the last void-return inliner fix and newer return-owner fix.
README, newest retained reports/reproduction receipts and generated
investigation indexes/knowledge pages need consolidation. No commits/pushes.
