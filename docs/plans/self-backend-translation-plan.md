# Self Backend Translation Plan

Related plans:

- `docs/plans/dual-llvm-backend-compat-plan.md`
- `docs/plans/archive/llvmcapi-wire-spike-report.md`
- `docs/plans/all-pass-llvm-ir-1to1-master-plan.md`
- `docs/plans/python-frontend-plan.md`
- `docs/plans/self-backend-bootstrap-default-plan.md`
- `docs/plans/self-backend-x86_64-linux-plan.md`

## Canonical LLVM Reference Trees

Originally recorded on this machine on `2026-04-20`.

Verification note (`2026-04-27`, post-rehydration):

- the split-tree LLVM source mirror is now fully extracted from the official
  LLVM 20.1.8 release tarball
  (`llvm-20.1.8.src.tar.xz`,
  sha256 `e1363888216b455184dbb8a74a347bf5612f56a3f982369e1cba6c7e0726cde1`,
  ~73 MB compressed, ~1.1 GB extracted),
- `/private/tmp/llvm-src/llvm-20.1.8.src/` contains the full LLVM source
  subtree including `lib/Target/` (22 backends: AArch64, X86, ARM, RISCV,
  Mips, PowerPC, SPIRV, WebAssembly, …), `lib/CodeGen/`, and
  `include/llvm/CodeGen/`,
- the three primary translation anchors used by this plan are present:
  `lib/Target/AArch64/AArch64ISelLowering.cpp` (30 115 lines),
  `lib/Target/X86/X86ISelLowering.cpp` (61 049 lines),
  `lib/CodeGen/TargetLoweringBase.cpp` (2 374 lines),
- the cached tarball at
  `/private/tmp/llvm-src/llvm-20.1.8.src.tar.xz`
  is retained so the tree can be re-extracted without a fresh download if
  the working copy is later corrupted or pruned,
- the `llvm-project-20.1.8.src` monorepo path remains absent — backend work
  uses the split-tree mirror as the primary anchor instead. If a future
  task needs sibling projects (`clang/`, `lldb/`, …) under one root, a
  separate fetch of `llvm-project-20.1.8.src.tar.xz` can be added.

Use these exact absolute roots when reading upstream LLVM source for the self
backend track:

- primary backend source mirror (rehydrated 2026-04-27):
  - `/private/tmp/llvm-src/llvm-20.1.8.src`
- monorepo root (currently unpopulated; rehydrate on demand):
  - `/private/tmp/llvm-src/llvm-project-20.1.8.src`
- local clang/frontend reference tree:
  - `/private/tmp/llvm-clang-tests/clang`

Rules:

- treat the split-tree mirror as the current primary documentation anchor;
  promote back to the monorepo root once that path is populated,
- when a needed backend subtree is sparse or missing, expand/sync the
  checkout before implementation instead of guessing from memory,
- if the source file named in the anchor map is absent locally, stop and
  restore the LLVM checkout (the cached tarball above is the fastest path)
  before changing backend lowering code,
- every real self-backend subsystem change must cite at least one upstream LLVM
  source entry point in the task notes / code comments / plan updates,
- if `pcc` intentionally implements a narrower subset than LLVM, document that
  boundary explicitly instead of presenting it as full parity.

Honesty calibration:

- before 2026-04-27 the self-backend AArch64 MVP could only honestly be
  described as **behavior-/workload-driven** because the source anchors
  were absent on this machine,
- with the split-tree mirror rehydrated, new self-backend work may
  describe its translation as **source-anchored** when it cites a
  concrete upstream entry point,
- existing self-backend code that predates the rehydration should not be
  retroactively relabelled "source-anchored"; either re-verify the lowering
  against the now-present upstream code, or keep the original
  workload-driven labelling.

## Status

Proposed active roadmap for the **own machine backend** track.

Snapshot as of `2026-04-27`:

- Phase 0 safety rails are landed far enough for real backend iteration:
  - backend selector skeleton (`--backend`, `PCC_BACKEND`),
  - backend-aware compile/native/JIT cache identities,
  - focused selector/cache/API regression coverage,
- the Phase 2 asm-first AArch64 Darwin MVP is no longer only a tiny toy slice:
  - `--backend=self --emit-asm` now lowers a materially broader truthful subset
    including integer/pointer scalars, direct and indirect calls, stack args
    beyond register banks, pointer `icmp`, multiline `switch`, `fneg`, ordered
    and unordered `fcmp`, first varargs lowering, and broader
    `getelementptr`/addressing patterns,
  - the asm/object path now handles larger stack frames and large local stack
    slots without assembler-immediate overflow,
  - dead SSA trimming plus block-local stack-slot reuse now cut pathological
    frame pressure in large interpreter loops enough to run beyond the initial
    Lua smoke slice,
- the artifact/runtime closure has moved beyond "can run tiny probes":
  - `--backend=self --emit-obj` now emits a linkable Mach-O object for the
    current supported slice,
  - the self backend can now compile, link, and run the first curated real
    workload slice:
    - `projects/lua-5.5.0/onelua.c` as a single TU,
    - executing `projects/lua-5.5.0/testes/math.lua`,
    - executing `projects/lua-5.5.0/testes/calls.lua`,
    - executing `projects/lua-5.5.0/testes/all.lua` with `_port=true`,
  - a second non-Lua real-project smoke now also lands under the isolated
    self-backend track:
    - make-derived `zlib` dependency build plus
      `projects/test_zlib_main.c` runtime closure,
- a third real-project smoke now also lands under the isolated
  self-backend track:
  - make-derived `lz4` dependency build plus
    `projects/test_lz4_main.c` runtime closure,
- a fourth real-project smoke now also lands under the isolated
  self-backend track:
  - make-derived `zstd` dependency build plus
    `projects/test_zstd_main.c` runtime closure,
- a fifth real-project smoke now also lands under the isolated
  self-backend track:
  - repo-local `pcre` runtime corpus plus
    `projects/test_pcre_main.c` runtime closure,
- a sixth real-project smoke now also lands under the isolated
  self-backend track:
  - repo-local `openssl` smoke subset plus
    `projects/test_openssl_main.c` runtime closure,
- a seventh real-project smoke now also lands under the isolated
  self-backend track:
  - repo-local `readline` smoke plus
    `projects/test_readline_main.c` runtime closure,
- an eighth real-project smoke now also lands under the isolated
  self-backend track:
  - repo-local `postgresql-17.4` `libpq` client slice plus
    `projects/test_postgres_main.c` runtime closure,
  - and the `projects/test_postgres_query_main.c` query path against a native
    postgres server,
- scalar/global/data coverage has broadened materially:
  - `i64`, pointer, `float`, and `double` args/returns/loads/stores/arithmetic
    are now started,
  - local string literals, simple global string-pointer initializers, external
    global data symbols, and a much richer subset of aggregate/rodata/global
    initializers are now started,
- aggregate ABI and aggregate data movement have also moved beyond the first
  register-sized toy cases:
  - small aggregate-by-value passing/return remains supported,
  - two-register partial-tail slices (`8+{1,2,4,8}`) remain supported,
  - larger local aggregate copy/assignment now has first memory-copy-based
    support,
- implementation-debt note:
  - the current `AArch64 Darwin` backend path is still largely a
    workload-driven single-file MVP,
  - it is **not yet** a systematic subsystem-by-subsystem LLVM backend
    translation,
  - this means the existing AArch64 code must be treated as a behavior-tested
    MVP plus implementation debt, not as already-complete LLVM source
    translation,
  - before adding a second target such as `Linux x86_64`, this track must pay
    down that debt by refactoring the current emitter toward an
    LLVM-source-anchored subsystem split,
  - the first structural split is now started:
    - generic `emit_self_asm(...)` dispatch exists,
    - evaluator no longer reaches directly into the AArch64 emitter entrypoint,
    - the old single-file `self_backend.py` path is now split into:
      - a generic facade,
      - a target-dispatch layer,
      - and an explicit `AArch64 Darwin` target module,
    - target dispatch is now also moving behind an explicit target registry so
      later translated targets land as new target specs instead of new
      hand-written conditional branches,
    - the current AArch64 emitter now consumes that registry's triple matcher
      instead of carrying a second private copy of the same target predicate,
    - target-neutral IR/data-model pieces (`TypeDesc`, `Parsed*`, layout helpers)
      now live in a shared core module instead of inside the AArch64 target
      implementation,
    - target-neutral LLVM IR parsing/decoding is now also being extracted into
      a shared parser layer so future targets consume one parsed-module
      representation instead of re-implementing textual decoding,
    - parsed-function preparation (`block_map`, argument type seeding) is now
      also moving into shared prepare helpers instead of staying inside the
      AArch64 emitter entrypoint,
    - stack-slot assignment and slot-reuse preparation are now also being
      pulled into shared stackprep helpers, parameterized by target ABI rules
      instead of staying hard-coded inside the AArch64 emitter,
    - module-symbol preparation (`defined/internal` symbol sets and per-module
      internal prefix generation) is now also moving into shared helpers
      instead of staying as AArch64 file-global state,
    - module-level prep is now also being consolidated into a shared pipeline
      (`parse -> prepare -> stackprep -> module symbols`) so later targets can
      reuse one entry skeleton instead of cloning the AArch64 emitter prologue,
    - the AArch64 aggregate/arg-register ABI rules are now also being split
      out of the emitter into a target-specific ABI module so backend layering
      stops mixing ABI classification with asm emission,
    - AArch64 memory/opcode selection and aggregate-copy chunking are now also
      moving into a target-specific memory helper module so the emitter keeps
      shedding non-structural micro-lowering decisions,
    - AArch64 register/immediate/offset materialization helpers are now also
      moving into a target-specific register helper module so the emitter keeps
      shrinking toward control-flow and instruction sequencing,
    - AArch64 asm symbol and block-label formatting helpers are now also
      moving into a target-specific symbol helper module so target assembly
      naming rules stop living inline in the emitter,
    - AArch64 global/data initializer emission is now also moving into a
      target-specific data helper module so data-section formatting stops
      living inline in the emitter,
    - AArch64 slot/address/copy/zero helpers are now also moving into a
      target-specific slot helper module so emitter logic keeps shedding
      frame/addressing micro-lowering,
    - AArch64 integer/fp op-selection, cast lowering, and compare-condition
      mapping are now also moving into a target-specific ops helper module so
      the emitter keeps shedding instruction-selection rules,
    - AArch64 address materialization helpers (`global` symbol addressing,
      indexed pointer add, and GEP offset lowering) are now also moving into a
      target-specific address helper module so emitter logic keeps shedding
      target-specific address computation,
    - AArch64 value/pointer/large-aggregate materialization is now also moving
      into a target-specific materialize helper module so the emitter keeps
      shedding constant/global/alloca-address realization logic,
    - AArch64 variadic-call and fixed-stack-arg ABI helpers are now also
      moving into a target-specific call helper module so the emitter keeps
      shedding call-lowering subroutines before the remaining call path gets
      split further,
    - AArch64 bit-count intrinsic lowering and phi-assignment preparation are
      now also moving into a target-specific flow helper module so the emitter
      keeps shedding CFG-edge and intrinsic subroutines,
    - the main AArch64 call-lowering branch is now also starting to move into
      the target-specific call helper module so the emitter keeps shrinking
      toward dispatch/sequencing instead of carrying the full direct/indirect
      call ABI path inline; the remaining `llvm.ctlz/cttz` and `llvm.va_*`
      special-cases are now also being pushed down there,
    - AArch64 epilogue/branch/cond-branch/switch/unreachable lowering is now
      also moving into a target-specific terminator helper module so the
      emitter keeps shedding control-flow subroutines,
    - AArch64 scalar-return and hidden-sret aggregate-return lowering is now
      also moving into a target-specific return helper module so the emitter
      keeps shedding remaining terminator-specific ABI logic,
    - AArch64 compute-family instruction paths (`binop/fcmp/cast/gep/call`)
      are now also moving into a target-specific compute helper module so the
      emitter keeps shrinking toward `alloca/load/store` plus top-level
      dispatch,
    - and target-neutral value/liveness analysis (`used-values`,
      block-local last-use tracking, `value_has_uses`) has started moving into
      shared analysis helpers instead of staying inside the AArch64 target
      file,
- the first Phase 3 closure point has effectively started landing:
  - self backend is no longer only a micro-corpus runner,
  - one curated real workload slice now passes end-to-end without manual patching,
- broad self evidence is materially stronger now:
  - the last full focused self-backend unit run before adding the strict
    no-fallback nodes was `218 passed`,
  - a focused strict self/no-LLVM-fallback gate is now landed:
    - `test_self_backend_system_link_uses_self_emitter_not_llvm_object_path`
      proves `run_translation_units_with_system_cc(... backend="self")`
      reaches the self asm emitter and does not enter the LLVM object path,
    - `test_self_backend_emitter_failure_does_not_fallback_to_llvm` proves a
      self emitter failure propagates instead of silently falling back to LLVM,
  - the first shared exact-match `c-testsuite` broad self gate has now been
    widened all the way to the full `220-case` runtime exact-match manifest
    and is currently green at `506 passed`,
  - a second broad self gate is now landed for `gcc-torture`:
    the full `1562-case` runtime exact-match manifest is now the formal
    repository gate shape, including both self-vs-native and self-vs-LLVM
    comparisons (`3124` collected tests),
  - the `gcc-torture` self gate also now includes the full
    `212-case` returncode-only bucket, again against both native and LLVM,
    bringing the formal collected self gate shape to `3548` tests,
  - the full `3548-case` `tests/test_gcc_torture_self.py` formal gate has now
    been completed successfully on the supported host,
  - the first focused `LLVM-vs-self` exact-match bucket has also been cleaned
    through recent parser/lowering fixes (`numeric SSA`, `select`,
    `freeze`, flagged `trunc`, `icmp samesign`, boolean `i1` literals),
  - the first real-workload strict self gate is now landed on `zlib`: the
    self system-link runtime test records `emit_self_asm` and verifies every
    compiled unit goes through the self emitter instead of treating
    `allow_unimplemented_backend=True` as fallback permission,
  - an explicit supported-host promotion runner now exists:
    - `scripts/run_self_backend_promotion_gate.py --tier quick` runs focused
      self unit coverage plus the strict `zlib` workload gate,
    - `scripts/run_self_backend_promotion_gate.py --tier full` runs the formal
      broad gates plus the current non-postgres workload ladder,
    - `--include-postgres` adds the heavier postgres integration gates when
      the local server/project prerequisites are available,
- real workload evidence on the supported macOS arm64 host is now refreshed
  after the latest ABI/parser/intrinsic cleanup:
  - `zlib` self backend system-link runtime passes,
  - `lz4` self backend system-link runtime passes,
  - `zstd` self backend system-link runtime passes,
  - `pcre` self backend system-link runtime passes,
  - `openssl` self backend system-link runtime passes,
  - `readline` self backend system-link runtime passes,
  - `postgres` repo-local zlib project self backend system-link runtime passes,
  - `postgres` CLI `--backend=self --system-link` gate passes,
- current next frontier after that broad-gate cleanup is no longer the same
  small scalar/CFG family:
  - repo-local `pcre` is no longer the current promotion blocker,
  - the recent optimized wrong-code there closed by fixing narrow integer
    constant materialization and signed narrow `icmp` lowering on AArch64,
  - the `gcc-torture` tail cleanup also closed several later ABI/code-shape
    gaps: signed narrow vector `icmp`, large aggregate literal phi copies,
    vector fp/int bitcasts, outgoing aggregate-copy scratch clobbering, and
    fixed by-value large aggregate literals passed through indirect stack
    temporaries,
  - the latest zstd cleanup closed additional long-tail LLVM IR/backend shapes:
    anonymous-struct return header splitting, `zeroinitializer` indirect
    aggregate args, symbolic pointer fields inside aggregate literals,
    aggregate-literal `insertvalue` members, narrow `llvm.abs` and
    `llvm.fshl/fshr`, scalar-condition aggregate `select`, and remaining
    `llvm.ucmp` / vector reduce `or` / vector reduce `umax` intrinsics,
  - that means the remaining gap to "replace LLVM on macOS arm64" keeps
    shifting away from obvious unsupported/parser trivia and toward broader
    promotion evidence (`T12`/`T13`) plus whatever the next real optimized
    workload frontier turns out to be,
- broader aggregate ABI, richer external ABI boundaries, and wider workload
  coverage are still open work,
- `β4.0` surface tracing is no longer the next blocking prerequisite for
  isolated self-backend translation work; that adjacent seam-mapping work is
  already underway/completed on the `llvm_capi` track, while broader shared
  handoff cleanup remains separate follow-on work.

This plan is intentionally separate from the llvmlite-removal / `llvm_capi`
wire-up work. They are adjacent, but not the same problem:

- **β4 / llvm_capi text-first builder** answers: how do we stop depending on
  `llvmlite` while preserving the current LLVM-based pipeline?
- **self backend translation** answers: how do we gradually stop relying on
  LLVM's machine backend for native emission?

The first is a shared-core extraction step. The second is a new backend epic.

---

## Why This Exists

`pcc` now owns a large amount of LLVM-facing optimization logic in Python:

- the visible pass surface is no longer "LLVM does everything for us",
- the repository already has substantial LLVM-IR-level reasoning and rewrite
  machinery,
- the next long-horizon question is whether native emission can also become
  `pcc`-owned.

That does **not** mean the machine backend is "almost done". It means the
project now has enough control over the front half and middle-end to make a
self backend realistic as a staged effort.

This plan exists so that backend work can begin **without destabilizing the
current compiler**.

---

## Non-Negotiable Goal

Build a `pcc`-owned native backend that can be selected explicitly while the
current LLVM backend remains the default until the new path is proven.

The end state is:

- `pcc` can run with `--backend=llvm` and `--backend=self`,
- the self backend starts with a narrow but truthful subset,
- unsupported cases fall back or fail loudly instead of silently miscompiling,
- backend work does not fork the frontend or optimizer stacks,
- the long-term self-host path can choose between LLVM-backed emission and
  `pcc`-owned emission behind one backend contract.

---

## Hard Policy

### 1. Default behavior must not regress

Until explicitly promoted, the default backend remains the current LLVM path.
All broad regression gates continue to run against that default path.

### 2. No silent wrong-code boundary

The self backend must prefer:

- no LLVM fallback once a run has explicitly entered the self backend,
- explicit fallback to LLVM only outside that strict self gate and only when
  the caller/gate name makes it visible,
- or explicit "unsupported" failure,

rather than guessing through unsupported lowering.

`allow_unimplemented_backend=True` is only a backend resolver opt-in for the
experimental `self` backend; it is not permission for a self workload gate to
silently fall back to LLVM emission.

### 3. Backend work must reuse shared front-half infrastructure

Do not create a second isolated frontend/codegen stack just to feed the self
backend. The long-term value comes from reusing:

- parser and semantic lowering,
- internal SSA / pass information,
- artifact pipeline,
- cache discipline,
- CLI/backend selection.

### 4. Start asm-first, not object-writer-first

The first useful self backend should emit assembly for one target and rely on
system assembler/linker tooling. Object writer, relocation encoding, and wider
MC-format ownership come later.

### 5. Single target first

Do not start with "portable backend" ambitions. First closure means one real
host target works end-to-end.

Recommended first target for this repository's current environment:

- **AArch64 Darwin**

A later Linux `x86_64` track can follow once the backend contract is stable.

---

## Current Execution Order

The active promotion ladder is intentionally ordered:

1. **Refactor first**:
   finish the `1:1` backend-parity framework and LLVM-like subsystem split
   before opening a second target.
2. **ARM evidence second**:
   keep expanding and hardening the supported-host (`AArch64 Darwin`) self
   gates so the layered framework has real blocking evidence.
3. **x64 test framework third**:
   build the `Linux x86_64` test/differential harness on top of the shared
   parser/core layers, then start target-specific translation work.
   This now also includes shared exact-match `c-testsuite` corpus helpers so
   ARM broad gates and x64 baseline buckets do not drift into separate
   manifest logic.
4. **Default-flip last**:
   only discuss replacing LLVM with `self` after the layering work,
   supported-host evidence, and x64 evidence all exist in measurable form.

---

## Architecture Direction

The target long-form architecture is:

```text
frontend / semantic lowering / SSA / passes
  │
  ├─ shared builder / shared artifact pipeline
  │
  └─ backend contract
       ├─ llvm backend       (default, existing path)
       ├─ llvm_capi backend  (llvmlite replacement path)
       └─ self backend       (new machine backend path)
```

Important consequence:

- `β4` text-first builder work is **not wasted** if the project later adopts a
  self backend.
- That work is the cleanest way to detach the front half from `llvmlite` and
  make backend selection real.

The self backend should be able to consume either:

1. an LLVM-IR-like textual/structured form as a bootstrap input, or
2. a later `pcc`-owned lower machine/MIR form.

The initial implementation may choose (1) for speed, but the architecture must
not assume the self backend will always parse LLVM IR text forever.

---

## Scope Boundaries

### In scope for the first wave

- backend selector plumbing,
- backend-aware cache and artifact separation,
- single-target assembly emission,
- integer/pointer scalar lowering,
- basic calls, returns, branches, stack slots, and local frame layout,
- a minimal register-allocation strategy,
- focused parity/regression harnesses,
- explicit fallback boundary to LLVM for unsupported cases.

### Out of scope for the first wave

- multi-target support,
- debug info parity,
- custom object writer,
- exceptions/unwind,
- vector ISA exploitation,
- complete ABI corner closure for every C construct,
- replacing LLVM for all project configurations.

---

## Translation Strategy

The project is **not** allowed to drift into "invent our own backend and merely
check LLVM behavior occasionally".

Required strategy:

- translate LLVM backend subsystems from explicit upstream source anchors,
- preserve LLVM's backend layering and algorithmic boundaries unless there is a
  clearly documented reason to narrow them,
- keep any narrowing or simplification source-anchored and explicit.

This does **not** require a mechanically line-by-line C++ port.
But it **does** require source-anchored translation discipline:

- LLVM source is not just an oracle for output behavior; it is the primary
  reference for subsystem structure,
- do not replace LLVM-inspired subsystem boundaries with an ad hoc
  "more Pythonic" organization if that obscures what was actually translated,
- when the current codebase already contains a hand-written MVP path, treat it
  as implementation debt to be regularized, not as proof that source-anchored
  translation is unnecessary.

## LLVM Source Anchor Map For The First Real Self-Backend Slice

This plan is not allowed to drift into "invent a plausible backend" work.

For the first target (`AArch64 Darwin`, asm-first), use these upstream source
entry points as the primary oracle set:

### Target-independent codegen / lowering references

- target-lowering contracts and legalization:
  - `/private/tmp/llvm-src/llvm-20.1.8.src/lib/CodeGen/TargetLoweringBase.cpp`
  - `/private/tmp/llvm-src/llvm-20.1.8.src/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp`
  - `/private/tmp/llvm-src/llvm-20.1.8.src/lib/CodeGen/SelectionDAG/LegalizeDAG.cpp`
  - `/private/tmp/llvm-src/llvm-20.1.8.src/lib/CodeGen/SelectionDAG/LegalizeTypes.cpp`
- calling-convention helpers / lowering mechanics:
  - `/private/tmp/llvm-src/llvm-20.1.8.src/lib/CodeGen/SelectionDAG/CallingConvLower.cpp`
- register allocation and spill/reference algorithms:
  - `/private/tmp/llvm-src/llvm-20.1.8.src/lib/CodeGen/RegAllocFast.cpp`
  - `/private/tmp/llvm-src/llvm-20.1.8.src/lib/CodeGen/PrologEpilogInserter.cpp`
- asm emission contracts:
  - `/private/tmp/llvm-src/llvm-20.1.8.src/lib/CodeGen/AsmPrinter/AsmPrinter.cpp`

### AArch64-specific oracle set

- target lowering / instruction selection intent:
  - `/private/tmp/llvm-src/llvm-20.1.8.src/lib/Target/AArch64/AArch64ISelLowering.cpp`
  - `/private/tmp/llvm-src/llvm-20.1.8.src/lib/Target/AArch64/AArch64InstrInfo.cpp`
- frame / callee-save / stack layout intent:
  - `/private/tmp/llvm-src/llvm-20.1.8.src/lib/Target/AArch64/AArch64FrameLowering.cpp`
  - `/private/tmp/llvm-src/llvm-20.1.8.src/lib/Target/AArch64/AArch64RegisterInfo.cpp`
- calling convention and ABI details:
  - `/private/tmp/llvm-src/llvm-20.1.8.src/lib/Target/AArch64/AArch64CallingConvention.td`
- target-specific assembly emission / MC syntax:
  - `/private/tmp/llvm-src/llvm-20.1.8.src/lib/Target/AArch64/AArch64AsmPrinter.cpp`
  - `/private/tmp/llvm-src/llvm-20.1.8.src/lib/Target/AArch64/MCTargetDesc/AArch64MCAsmInfo.cpp`
  - `/private/tmp/llvm-src/llvm-20.1.8.src/lib/Target/AArch64/MCTargetDesc/AArch64MCTargetDesc.cpp`

Required discipline for this oracle map:

- do not pick algorithms from memory without checking these sources first,
- do not add a new backend subsystem without naming its primary upstream LLVM
  source anchor,
- if `pcc` deliberately chooses a smaller algorithm, record that as an
  intentional subset of a named LLVM subsystem rather than as an unrelated
  fresh design,
- before opening a second target track, reduce the current gap between the
  hand-written AArch64 MVP and an LLVM-like subsystem split.

---

## Phases

### Phase 0: Backend Selection And Safety Rails

Goal: make backend selection real before the self backend exists.

Current status (`2026-04-20`): the first selector/cache/test slice is landed;
capability-model broadening and explicit fallback result typing remain open.

Deliverables:

- `--backend=` CLI and `PCC_BACKEND` env plumbing,
- backend capability model (`jit`, `emit_obj`, `emit_asm`, `debug`, `cross`),
- backend-aware compile cache keys,
- explicit unsupported/fallback result types,
- small selector tests proving default behavior is unchanged.

Exit criteria:

- default backend behavior is identical to today,
- backend mode changes cannot reuse stale cache artifacts,
- non-default backend selection is visible and auditable.

### Phase 1: Builder / Front-Half Decoupling

Goal: stop tying the frontend directly to `llvmlite` so multiple backend
providers can consume the same front-half output.

Primary dependency:

- `β4` text-first builder / `llvm_capi` work from
  `docs/plans/archive/llvmcapi-wire-spike-report.md`

Deliverables:

- shared builder surface not owned by `llvmlite`,
- LLVM-backed backends still passing the existing gates,
- a stable handoff point where the self backend can begin consuming compiler
  output.

Exit criteria:

- front-half codegen is no longer architecturally trapped inside
  `llvmlite.ir`,
- the self backend has a real integration seam.

### Phase 2: Single-Target Asm-First MVP

Goal: produce runnable assembly for one host target.

Recommended subset:

- integer and pointer arithmetic,
- compares and conditional branches,
- direct calls and returns,
- stack locals and simple loads/stores,
- global-address references,
- simple aggregate-adjacent cases only when already represented as scalarized
  operations by the front half.

Deliverables:

- target register set model,
- calling convention subset,
- frame layout and stack alignment,
- instruction selection for the MVP opcode set,
- a simple register allocator (linear scan is acceptable),
- assembly printer,
- execution via system assembler/linker.

Exit criteria:

- a focused backend corpus runs correctly under `--backend=self`,
- unsupported shapes fail loudly or fall back,
- default LLVM backend remains unaffected.

### Phase 3: Self-Hosted Compiler Slice

Goal: make the self backend useful for a controlled slice of compiler and test
workloads.

Deliverables:

- compile a meaningful subset of repository cases with `--backend=self`,
- document precise supported C/IR subset,
- run selected self-host or compiler-internal workloads on the self backend,
- add backend differential tests against the LLVM path.

Exit criteria:

- self backend is no longer just a toy case runner,
- it can execute a curated real slice without manual patching.

### Phase 4: ABI And Coverage Expansion

Goal: reduce the unsupported subset without losing truthfulness.

Potential expansion areas:

- more aggregate passing/return cases,
- more indirect call patterns,
- better spill/reload quality,
- more robust branch lowering,
- improved lowering of addressing modes,
- broader local-memory patterns.

Current concrete expansion priorities on the frozen first target:

- keep thickening the single-target `AArch64 Darwin` supported slice before
  adding any second-target work,
- push the current real-workload ladder in order:
  - `zstd` next,
  - then only after that another harder workload or broader multi-TU slice,
- close more of the real ABI matrix on the current host target:
  - larger aggregate by-value args/returns,
  - more external ABI boundary shapes,
  - more vararg shapes,
  - remaining aggregate `phi` / call-result / local-memory merge cases,
- continue eating long-tail LLVM IR and real-project shapes as they appear in
  real workloads rather than inventing speculative synthetic coverage.

Exit criteria:

- the supported corpus grows materially,
- backend crashes/unsupported exits shrink with evidence,
- no increase in silent wrong-code risk.

### Phase 5: Decide Whether Full Machine Ownership Is Worth It

Goal: make an evidence-based decision on whether to continue beyond asm-first.

Questions to answer:

- Is the self backend already good enough for the target product shape?
- Is object-writer ownership actually worth the added complexity?
- Does the project need more than one target?
- Is LLVM still the better default even after the self backend matures?

This phase may end in any of three honest outcomes:

1. keep LLVM as default and retain self backend as a niche/experimental path,
2. promote self backend for a narrow product subset,
3. continue toward broader machine-backend ownership.

Important non-goals before that decision point:

- do **not** treat Docker, emulation, or an `i64`-only smoke as a substitute
  for real target support; the current backend still emits `AArch64 Darwin`
  assembly and ABI, not `Linux x86_64`,
- do **not** start object-writer/debug-info/exception/cross-target work while
  the first target's backend contract and ABI coverage are still visibly
  moving.

---

## Token Budget

These are planning-level estimates, not commitments.

### Phase 0 + Phase 1 (selection + decoupling prerequisites)

- `repo tokens`: `40k-120k`
- `working tokens`: `500k-1.8M`

### Phase 2 (single-target asm-first MVP)

- `repo tokens`: `180k-420k`
- `working tokens`: `3M-9M`

### Phase 3 + Phase 4 (usable backend slice + coverage growth)

- `repo tokens`: `180k-500k`
- `working tokens`: `3M-10M`

### Total through a serious first-generation self backend

- `repo tokens`: `400k-1.0M`
- `working tokens`: `6.5M-20M`

Interpretation:

- this is **not** a tiny follow-up patch after pass translation,
- but it is still within the same rough order of magnitude as a major roadmap
  epic, not an impossible rewrite of all LLVM.

---

## Validation Strategy

### 1. Backend differential testing

For every backend milestone, compare:

- `--backend=llvm`
- `--backend=self`

on:

- focused minimized C probes,
- existing sensitive runtime tests,
- curated integration slices.

Oracle policy for `1:1`:

- the primary oracle is the upstream LLVM backend behavior on the same visible
  input and target,
- use the same LLVM IR, target triple, and direct invocation conditions when
  possible,
- `llvmlite` may still be useful as an implementation/debugging aid on the
  LLVM-backed path, but it is **not** the final backend-parity oracle for the
  self backend.

### 2. Layered parity, not premature byte-identical fetish

Validation order should be:

1. semantic correctness,
2. structural/backend contract correctness,
3. textual/stylistic convergence where useful.

The self backend does **not** need to produce byte-identical LLVM-path output to
count as correct.

### 3. Explicit unsupported accounting

Track and report:

- unsupported feature exits,
- fallback frequency,
- backend-only crashes,
- wrong-code regressions separately from unsupported cases.

---

## Immediate Task Queue

### T0. Approve backend contract split

Decision to record explicitly:

- `llvm` remains default,
- `llvm_capi` is the llvmlite-removal track,
- `self` is the machine-backend track,
- `β4` is shared groundwork for both LLVM-backed and self-backed futures.

### T1. Implement selector skeleton — done

Files likely involved:

- `pcc/pcc.py`
- `pcc/frontends/c/evaluator/c_evaluator.py`
- new `pcc/backend/`

Landed slice:

- explicit `--backend` / `PCC_BACKEND`,
- default path still `llvm`,
- explicit unsupported failure for `self`.

### T2. Make cache signatures backend-aware — done

Files likely involved:

- `pcc/frontends/c/evaluator/c_evaluator.py`
- cache tests in `tests/`

Landed slice:

- compile cache signature includes backend identity,
- native cache signature includes backend identity,
- in-memory JIT cache identity includes backend identity.

### T3. Add backend selector tests — done

Examples:

- default stays `llvm`,
- `--backend=llvm` is a no-op behaviorally,
- `--backend=self` fails clearly while unimplemented,
- backend selection changes cache identity.

### T4. Land β4.0 surface trace — completed on the adjacent `llvm_capi` track

The original purpose of this task was to make the real builder/backend surface
data-driven before self-backend work expanded.

Deliverable for this task:

- a concrete map from current `pcc` call sites to LLVM / llvmlite surfaces,
- a list of which surfaces are shared prerequisites for both `llvm_capi` and
  `self`,
- an explicit seam record for future shared handoff cleanup.

Current status:

- isolated self-backend lowering has already started and advanced while this
  adjacent seam record matured on the `llvm_capi` side,
- remaining work here is shared contract cleanup, not "block all self lowering
  until no code exists".

### T5. Choose first target and freeze MVP subset — done

Recommended default choice for this repository today:

- target: `aarch64-apple-darwin`
- output: assembly first
- linker path: system toolchain

First closure target should be phrased concretely as:

- emit Mach-O-compatible AArch64 assembly accepted by the host `cc`/`clang`
  driver,
- no object-writer ownership in the MVP,
- no silent fallback inside the self backend once a function has entered the
  claimed-supported subset.

Current verified closure for that frozen target/subset:

- AArch64 Darwin,
- asm-first via host assembler/linker,
- `onelua.c` single-TU compile/link/run closure for `testes/math.lua`.

Current immediate frontier beyond that first closure:

- keep the current single target (`AArch64 Darwin`) as the only active self
  backend target,
- move the curated workload ladder forward with `zstd`,
- keep broadening truthful ABI coverage on the same target before opening a
  second target track such as `Linux x86_64`.

### T6. Keep thickening the frozen `AArch64 Darwin` target slice

Purpose:

- continue making the current host-target backend less fragile before any
  second-target effort,
- prefer deeper truthful coverage on one target over shallow support claims on
  several targets.

Work items:

- keep adding focused self-backend regressions in `tests/test_self_backend.py`,
- keep turning ad hoc real-project probes into formal runtime gates,
- only widen the supported subset when the new slice is backed by minimized IR
  or C regressions plus one real workload confirmation.

### T7. Push the next real-workload frontier: `zstd`

Purpose:

- use `zstd` as the next non-Lua/non-lz4 coverage step on the same target,
- let real workload pressure determine the next missing ABI/IR features.

Known current frontier:

- `zstd` is no longer the next pending smoke target; it now lands under the
  isolated self-backend track,
- the next active real-project frontier should be chosen after `zstd`, using
  the same rule as before:
  let the next workload expose the next truthful ABI / LLVM-IR gaps.

Exit criteria:

- make-derived `zstd` dependency build plus repo-local runtime smoke pass under
  `--backend=self` on the current host target,
- the newly exposed failures are captured as focused regressions rather than
  only as one large-project anecdote.

### T8. Expand ABI coverage on the current target until the boundary is boring

Purpose:

- reduce the class of "real project hits a slightly different ABI shape and
  dies immediately" failures.

Priority backlog:

- larger aggregate by-value args and returns across more shapes,
- more external call/data ABI boundaries,
- more vararg cases,
- more aggregate movement paths:
  - call results,
  - `phi` merges,
  - local stack storage,
  - stores/loads across nontrivial addressing.

Exit criteria:

- fewer real-project failures stop at the ABI boundary,
- unsupported exits increasingly move past ABI into deeper semantic/coverage
  work.

### T9. Continue long-tail LLVM IR / instruction coverage from workload evidence

Purpose:

- keep translating LLVM semantic contracts as they really appear in generated
  IR, not by guessing a giant instruction wishlist up front.

Priority backlog:

- long-tail terminators and CFG edges,
- long-tail intrinsics,
- remaining address/global/data initializer shapes,
- real-project-specific IR combinations that do not show up in micro tests.

Exit criteria:

- each newly added instruction or IR shape is tied to:
  - one focused regression,
  - and one real workload movement.

### T10. Defer `Linux x86_64` until the first target contract is stable

Detailed follow-up plan:

- `docs/plans/self-backend-x86_64-linux-plan.md`

Current policy:

- `Linux x86_64` remains a later follow-on track,
- Docker/emulation/simulator experiments do not count as target support,
- no second-target implementation work should start until the current
  `AArch64 Darwin` backend contract and ABI surface stop shifting rapidly.

Rationale:

- target support means real calling convention, frame lowering, assembler
  syntax, relocation model, and system-link behavior,
- that work should not compete with the still-active first-target closure
  backlog.

Framework status:

- a repo-local Docker harness for `Linux x86_64` is now being added so future
  target work can reuse one test environment and one pytest entrypoint,
- the first baseline checks in that harness are:
  - LLVM-path smoke in Linux `x86_64` Docker,
  - self-backend unsupported-target boundary in the same Docker harness.
- the next baseline step is also started:
  - a small exact-match `c-testsuite` bucket now runs under the Linux `x86_64`
    Docker harness through the LLVM path, so future x64 self-backend work can
    upgrade an existing corpus runner instead of inventing one later.
  - current x64 baseline therefore has three concrete checks:
    - LLVM-path smoke in Docker,
    - self-backend unsupported-target boundary,
    - LLVM-path exact-match `c-testsuite` mini-bucket.
  - that mini-bucket is now being widened beyond the first 8-case scout slice
    so the x64 framework proves it can carry a real small corpus before any
    target implementation starts.
  - the x64 corpus runner is now being parameterized into reusable modes so the
    same bucket harness can validate:
    - LLVM-vs-native exact-match today,
    - explicit self-backend unsupported boundaries today,
    - and later real x64 self-backend differential behavior without replacing
      the whole test entrypoint.
  - target dispatch also now has an explicit `self-x86_64-linux-v0` stub target
    identity, so later translation work can land behind a stable target ID
    instead of changing the dispatch contract again.
  - the Linux x64 Docker harness now also locks an `amd64-pc-linux-gnu` CLI
    alias gate against that same stub target, so future x64 translation work
    does not quietly fork target naming behavior.
  - this is no longer only a pure stub:
    - the first translated Linux x86_64 self slice now emits real asm/object
      output for:
      - scalar integer/pointer args and returns,
      - local scalar `alloca/load/store`,
      - pointer-valued SSA load/store,
      - integer `binop` including:
        - `add/sub/mul`,
        - `and/or/xor`,
        - `sdiv/srem/udiv/urem`,
      - integer/pointer `icmp`,
      - `switch`, `br`, and `br_cond`,
      - `phi` merge assignment on CFG edges,
      - `zext/sext/trunc`,
      - `ptrtoint/inttoptr`,
      - pointer-to-pointer `bitcast`,
      - simple `getelementptr` on alloca/global/pointer-SSA bases for array and
        struct indexing,
      - typed global/data emission for:
        - scalar globals,
        - struct/array globals,
        - `zeroinitializer`,
        - string-literal byte arrays,
      - direct calls,
      - indirect calls through function pointers,
      - `ret` / `ret void`,
    - and the Docker harness now has formal self-backed smoke gates for:
      - constant-return closure,
      - `amd64` alias-target closure,
      - direct-call + integer-binop closure,
    - plus a first Linux x64 `c-testsuite` partial-support bucket that accepts
      both:
      - exact-match successes,
      - and explicit unsupported boundaries.
    - current measured progress on that bucket is already beyond the initial
      scout slice:
      - `32/32` exact-match cases first went green in the formal Docker-gated
        path,
      - a wider `64/64` local Docker probe then went green on the same
        truthful slice,
      - and the current x64 partial-support path is now green through the full
        `128/128` exact-match bucket in Docker on the same runner,
      - and the rolling `128-case` frontier has now moved past:
        - pointer-valued SSA memory,
        - `sdiv/srem`,
        - `sext` + basic `getelementptr`,
        - struct/array global initializers,
        - `ptrtoint`,
        - pointer `bitcast`,
        - `switch`,
        - `trunc`,
        - indirect call,
        - `sitofp i32 -> float`,
        - aggregate local `zeroinitializer store`,
        - `zext i32 -> i64` wrong-asm cleanup,
      - and the current `128-case` exact-match frontier has now closed.

Current boundary:

- Linux x86_64 self backend is now a meaningful early target slice rather than
  a pure stub,
- but FP casts/ops, broader floating-point ABI, aggregate ABI, varargs,
  richer external boundaries, and broad corpus parity beyond that first
  `128-case` bucket are still open work items, so this target is still far
  from promotion-ready.

### T11. Keep toolchain ownership explicitly deferred

Current policy:

- stay asm-first and host-linker-first for now,
- do not start custom object-writer ownership,
- do not start debug-info parity,
- do not start exception/unwind support,
- do not start cross-target ownership.

Rationale:

- these are all valid later questions,
- but they are Phase 5 decision inputs, not today's highest-value blockers.

### T11a. Refactor the current AArch64 MVP toward an LLVM-like subsystem split

Purpose:

- pay down the current implementation debt before opening a second target,
- make the existing backend look more like a translated LLVM backend and less
  like a single-file workload-driven emitter.

Required direction:

- split the current backend into at least:
  - LLVM-IR parse / normalization,
  - target-neutral lowering helpers,
  - target-specific ABI / frame / register lowering,
  - target-specific asm emission,
- keep each new subsystem tied to at least one explicit upstream LLVM source
  anchor,
- do not start `Linux x86_64` target work until this split exists in a usable
  form.

Exit criteria:

- the current `AArch64 Darwin` backend no longer lives as one undifferentiated
  emitter blob,
- a second target can be added by translating new target-specific layers rather
  than by copying and mutating the whole current file.
- current structural progress on that split now includes:
  - shared core modules for IR/data model, parse, analysis, prepare,
    stack-slot prep, module-symbol prep, block/function emission
    skeleton, instruction dispatch, and terminator dispatch,
  - AArch64 target modules for:
    - `abi`,
    - `addr`,
    - `calls`,
    - `compute`,
    - `data`,
    - `flow`,
    - `materialize`,
    - `mem`,
    - `memory`,
    - `ops`,
    - `prologue`,
    - `regs`,
    - `returns`,
    - `slots`,
    - `symbols`,
    - `terminators`,
  - so the remaining AArch64 emitter is increasingly a thin function skeleton
    plus instruction/terminator dispatch instead of a single monolithic target
    file.

### T12. Add broad self-backend repository gates instead of only curated smoke

Purpose:

- move from "curated workload ladder is green" toward "self backend is
  surviving broader repository reality",
- create the first evidence class that can justify promotion beyond explicit
  opt-in.

Priority backlog:

- add at least one broad `c_files` / repository-corpus gate under
  `--backend=self` on the supported host,
- add at least one broad `c_testsuite` / torture-style gate under
  `--backend=self`, even if initially bucketed or allow-listed,
- keep the current curated real-project ladder (`lua`, `zlib`, `lz4`, `zstd`,
  `pcre`, `openssl`, `readline`, `postgres`) fully green while broad gates are
  added.

Current landing:

- `c-testsuite` is no longer bucketed: the full `220-case` runtime
  exact-match manifest is green on the supported host,
- `gcc-torture` now has a first formal self gate as well:
  the full `1562-case` exact-match bucket plus the `212-case` returncode-only
  bucket are green both self-vs-native and self-vs-LLVM (`3548` collected
  formal tests).
- `zlib` now acts as the first strict real-workload self gate by asserting
  that every compiled unit reaches `emit_self_asm`.
- `scripts/run_self_backend_promotion_gate.py` now gives the promotion ladder a
  single supported-host command surface instead of requiring ad hoc pytest
  command recall.

Exit criteria:

- self backend is no longer validated only by micro IR tests plus curated smoke,
- at least one broad repository-scale gate runs continuously on the supported
  host and produces actionable failure buckets.

### T13. Build an explicit LLVM-vs-self differential harness on the supported host

This is now the **first step** of the explicit `1:1` backend-parity track.

Purpose:

- stop treating "both paths happen to pass their own tests" as enough evidence,
- measure how close self backend is to LLVM on the same real inputs.

Priority backlog:

- define a differential corpus on the supported host:
  - curated workloads already landed on self,
  - a broad repository subset,
  - selected bootstrap/compiler-internal workloads once the shared path allows
    it,
- define the oracle discipline explicitly:
  - upstream LLVM backend behavior is the primary oracle,
  - compare on the same LLVM IR and target triple where practical,
  - treat `llvmlite` as an implementation aid for the LLVM path, not as the
    final backend oracle,
- record outcomes in three buckets:
  - both pass,
  - LLVM passes / self fails,
  - behavior mismatch,
- prefer shrinking every mismatch into:
  - one minimized regression,
  - plus one real workload confirmation.
- first concrete landing now started:
  - the `c-testsuite` exact-match broad self bucket is also being reused as
    the first ARM-host `LLVM-vs-self` differential bucket instead of only a
    self-vs-native smoke gate.
  - that differential bucket is no longer a tiny pilot slice:
    it has already been widened to the full `220-case` ARM-host exact-match
    broad gate, so promotion evidence is now repository-scale rather than a
    scout bucket.

Exit criteria:

- self-vs-LLVM gaps are tracked as a concrete queue instead of anecdotes,
- promotion discussions can use measured failure buckets rather than optimism.
- this differential harness exists before any serious `Linux x86_64` target
  expansion or default-promotion discussion proceeds further.

### T14. Make `self` viable as the default backend for bootstrap on the supported host

Purpose:

- if the long-term self-host direction is serious, the first real promotion is
  not "global default for every user";
  it is "default backend for the supported-host bootstrap path",
- prove that `pcc` can increasingly compile itself through the self backend on
  the host where that backend is actually implemented.

Priority backlog:

- define a supported-host bootstrap mode whose default native emission backend
  is `self`,
- keep LLVM available as explicit escape hatch during the transition,
- thread that mode through the Python/bootstrap track once the adjacent shared
  builder/evaluator work is ready,
- add at least one explicit self-backed bootstrap gate:
  - stage-1 native emission through self on the supported host,
  - then later stage-2/stage-3 closure evidence as the bootstrap line matures.

Exit criteria:

- bootstrap on the supported host can choose `self` as the primary native
  emission path without ad hoc manual patching,
- self backend is no longer only a sidecar experiment for bootstrap work.

Current landing:

- CLI C-mode and direct `CEvaluator()` construction now accept
  `PCC_BACKEND=self` as an explicit default-backend opt-in for the experimental
  self path, matching `--backend=self` behavior. This is not yet a global
  default flip; it is the first supported-host switch needed to run
  promotion/bootstrap jobs without repeating the backend flag.
- The detailed bootstrap-default implementation queue now lives in
  `docs/plans/self-backend-bootstrap-default-plan.md`.

### T15. Define promotion criteria for flipping the supported-host default from LLVM to self

Purpose:

- avoid an emotional or symbolic default flip,
- make the promotion decision depend on broad evidence and operational
  simplicity.

Promotion ladder:

1. current state: explicit opt-in experimental backend
2. next state: supported-host bootstrap-default backend
3. later state: supported-host general default backend

Required evidence before step 3:

- broad self gates are continuously green enough to be trusted,
- curated workload ladder remains green,
- LLVM-vs-self differential buckets are small and understandable,
- the backend config surface can honestly stop calling `self` unsupported on
  that host,
- fallback/escape-hatch policy is still explicit and auditable.

Non-goal:

- do not require self to beat LLVM everywhere before promotion,
- but do require it to be competitive enough that defaulting to self improves
  the repository's long-term independence story instead of creating a permanent
  quality tax.

### T16. Add performance acceptance gates before any wider default flip

Purpose:

- keep "behavior/workload-driven" acceptable while still making the self backend
  comparable to the LLVM backend,
- prevent a correctness-only promotion from hiding a permanent performance tax,
- separate bootstrap-default acceptance from global-default acceptance.

Baseline command family:

```bash
env -u LC_ALL uv run python benchmarks/run_benchmarks.py --opt-level 2 --runs 3
env -u LC_ALL uv run python benchmarks/run_benchmarks.py --bench nbody_shootout.c --opt-level 2 --opt-level 3 --runs 3
env -u LC_ALL uv run python bench/bench.py --opt-level 2 --runs 3 --top-passes 12
```

Required follow-up:

- add a self-vs-LLVM backend benchmark mode that holds the front half and LLVM
  IR pipeline constant, then compares only native emission/backend behavior,
- record both compile/link time and produced-binary runtime,
- report geomean plus worst-case outliers, not only single benchmark wins.

Acceptance for step 2, supported-host bootstrap-default:

- correctness promotion gate is green,
- self-backed bootstrap gate is green,
- bootstrap wall time is not more than `2.0x` the LLVM-backed bootstrap
  baseline without an explicit documented exception,
- produced bootstrap binary startup/help/runtime smoke is not more than `2.0x`
  the LLVM-backed baseline,
- any benchmark slower than `3.0x` is a blocking issue unless it is marked as a
  known non-bootstrap workload and tracked separately.

Acceptance for step 3, supported-host general default:

- correctness promotion gate is green in a clean tree,
- self-vs-LLVM benchmark geomean runtime is within `1.25x` on the in-repo
  standalone benchmark suite at `-O2`,
- self-vs-LLVM compile+link geomean is within `1.50x`,
- no individual accepted benchmark is worse than `2.0x` without an explicit
  release-blocker issue and a written reason for allowing it,
- real workload runtime smokes (`lua`, `zlib`, `lz4`, `zstd`, `pcre`) have no
  unexplained catastrophic slowdown versus the LLVM backend,
- known performance-sensitive probes such as `nbody_shootout.c` stay within
  the established LLVM-backed envelope or get a dedicated investigation note.

If performance fails:

- do not flip the global default,
- keep `self` as explicit opt-in or bootstrap-only if bootstrap-specific
  performance still meets the step-2 thresholds,
- shrink the slowdown into a focused benchmark and inspect:
  - instruction selection,
  - missed addressing modes,
  - ABI lowering and spills,
  - frame layout,
  - optimizer handoff / IR shape,
  - host assembler/linker policy,
- add a benchmark regression before retesting promotion.

---

## Definition Of Done For The First Real Milestone

The first meaningful closure point is **not** "replace LLVM".

It is:

- backend selection exists,
- default path is unaffected,
- self backend can be chosen explicitly,
- self backend emits runnable assembly for a narrow truthful subset on one
  target,
- unsupported cases are explicit,
- at least one curated real workload slice passes under `--backend=self`.

That milestone is large enough to matter and small enough to ship honestly.

Status update (`2026-04-22`):

- this first milestone is now reached for the first curated slice on the
  current host target:
  - `projects/lua-5.5.0/onelua.c`
  - `projects/lua-5.5.0/testes/math.lua`
- the same closure slice now extends one workload further without leaving the
  isolated self-backend track:
  - `projects/lua-5.5.0/testes/calls.lua`
- a stronger harness-level probe is also now green on the same host target:
  - `projects/lua-5.5.0/testes/all.lua` with `_port=true`
- the next closure step beyond single-TU Lua is also now started:
  - make-derived Lua separate-TU build under `--separate-tus`
  - `projects/lua-5.5.0/testes/all.lua` with `_port=true`
- a second real-project smoke now also passes without leaving the isolated
  self-backend track:
  - repo-local zlib dependency build from `projects/zlib-1.3.1=libz.a`
  - `projects/test_zlib_main.c`
- a third real-project smoke now also passes without leaving the isolated
  self-backend track:
  - repo-local lz4 dependency build from `projects/lz4-1.10.0/lib=lib-release`
  - `projects/test_lz4_main.c`
- a fourth real-project smoke now also passes without leaving the isolated
  self-backend track:
  - repo-local zstd dependency build from
    `projects/zstd-1.5.6/lib=libzstd.a-release`
  - `projects/test_zstd_main.c`
- a fifth real-project smoke now also passes without leaving the isolated
  self-backend track:
  - repo-local `pcre-8.45` runtime corpus
  - `projects/test_pcre_main.c`
  - including `optimize>0` runtime closure
- a sixth real-project smoke now also passes without leaving the isolated
  self-backend track:
  - repo-local `openssl-3.4.1` smoke subset
  - `projects/test_openssl_main.c`
- a seventh real-project smoke now also passes without leaving the isolated
  self-backend track:
  - repo-local `readline-8.2` smoke
  - `projects/test_readline_main.c`
- an eighth real-project smoke now also passes without leaving the isolated
  self-backend track:
  - repo-local `postgresql-17.4` `libpq` client slice
  - `projects/test_postgres_main.c`
  - `projects/test_postgres_query_main.c` against a native server
  - and the matching `pcc --backend=self --system-link` CLI entry
- the first broad repository-scale self gate is now started:
  - `tests/test_c_testsuite_self.py`
  - a 64-case exact-match bucket from `c-testsuite`
  - currently `65 passed` including the `.expected` output check
- the next obvious heavier candidate (`sqlite`) is still not an isolated
  self-backend task:
  - with the current `tests/test_sqlite.py` harness inputs, the first blocker
    remains shared builder support for `IRBuilder.store_atomic`,
  - so `sqlite` should stay out of the isolated self-backend lane until that
    adjacent shared-path gap is intentionally taken on,
- an additional local probe on the same binary currently reaches the full
  standalone `LUA_TEST_FILES` corpus under `_port=true` on the current host;
  the formal regression gate is still intentionally smaller for now.
- the next meaningful bottleneck was not a missing LLVM opcode but frame
  pressure in `luaV_execute`; dead temporary trimming plus block-local slot
  reuse reduced that function's frame from roughly `69,680` bytes to
  roughly `32,144` bytes, which was enough to stop the `calls.lua` tail-call
  path from blowing the host C stack.
