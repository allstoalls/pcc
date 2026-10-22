# Performance gaps — pcc 2026-05-01

**Status:** open. Measured 2026-05-01 on macOS arm64. The headline
correctness milestone (Issue 1: pure-native bootstrap, no libpython)
landed today; with that out of the way these are the next-tier
performance issues that block flipping the bootstrap gate's
ratio thresholds and making `--backend self` competitive with
`--backend llvm` on real workloads.

This file is the authoritative tracker for performance issues. If a
README or plan paragraph contradicts this file, this file is right.

---

## P-1 — self/llvm bootstrap-time ratio 2.83x exceeds 2.0 gate

**State:** partially landed.

**Evidence:** `scripts/run_self_backend_bootstrap_gate.py --backend
both --stage 1` (2026-05-01):

```
backend=llvm stage=1 elapsed=9.9s  size=2.5MB
backend=self stage=1 elapsed=28.0s size=4.5MB
size_ratio        self/llvm = 1.789
bootstrap_ratio   self/llvm = 2.825   ← FAIL (gate=2.0)
```

**Root cause:** self-backend is pure Python — it parses LLVM IR text
with regex+startswith+split, runs no IR-level optimization, lowers
each instruction one-for-one. clang's backend is C++ with at least
basic register allocation and instruction selection.

**Profile of a single self-backend lowering (18MB IR → 30MB asm,
26.4s):**

```
2.4s  split_top_level()       — 144k calls
2.4s  re.Pattern.match()      — 5.7M calls
1.3s  str.startswith()        — 15.8M calls
1.2s  _parse_type()           — 618k calls
0.9s  decode_value_token()    — 302k calls
0.8s  _parse_instruction()    — 180k calls (cumulative 12s)
4.0s  posix.waitpid           — 33 sequential subprocess startups
```

>60% of self-backend time is text re-parsing of LLVM IR.

**Optimization paths, ranked by ROI:**

| ID | description | effort | compile time | runtime | binary size |
|---|---|---|---|---|---|
| P-1.A | parallelize per-module subprocesses | 1-2 days | 23s → 8-10s | — | — |
| P-1.B | replace regex IR parser with hand-rolled tokenizer | 1-2 weeks | -30% (~7s) | — | — |
| P-1.C | enable `function-attrs` + `adce` IR passes for nounwind DCE | 1-3 days | mixed | small reduction | small reduction |
| P-1.D | string-literal dedup in codegen (`py_str_new` → global PyStrObject const) | 1 week | -5MB IR → -7s parse | small reduction | small reduction |
| P-1.E | inline `py_bool_from_bit` / `py_int_from_i64` tagged fast path in codegen | 1 week | -1 to -2MB IR | small reduction | small reduction |
| P-1.F | linear-scan register allocator in self-backend | 2-4 weeks | small increase | -30 to -50% (hot loop) | -10 to -20% |
| P-1.G | asm-level peephole pass (mov elision, redundant load/store) | 1 week | small increase | -10 to -20% (hot loop) | -10 to -20% |
| P-1.H | compile self-backend with pcc itself | 2-6 months | -5x to -10x | — | — |

**Landed 2026-05-25 P-1.G subset:** AArch64 self-backend peepholes now cover
adjacent scalar/byte stack load forwarding, independent scalar phi direct
copies, forwarded `cset` branch folding, dead branch-only bool store removal,
one-intervening stack load forwarding, local branch-trampoline threading for
`b`, `b.<cond>`, and `cbz`/`cbnz` consumers, and deletion of unconditional
branches to the immediately following local label. A later small cleanup also
folds `b.<cond> L_then; b L_else; L_then:` to the inverse conditional branch
and drops unreferenced empty local labels. The latest accepted peephole folds
adjacent zero materialization plus register compare to `cmp #0`; follow-ups
fold adjacent zero-test branch outcomes and dead adjacent zero-store
materialization into zero-register stores, plus dead adjacent mov-store
sources into direct-source stores and dead adjacent mov-compare sources into
direct-source compares, conservative mov-zero-branch source forwarding, and
mov-arith self-update plus mov-mov chain folding. Latest accepted stage1
dashboard sample: LLVM pcc1 size `7770712`, self pcc1 size `13228400`,
`size_ratio self/llvm=1.702`, `bootstrap_elapsed_ratio self/llvm=1.042`, with
`libpython=False` for both backends. This is measured emitted-code and
pcc1-size progress, not proof of a stable broad runtime-speed win.

**Follow-up 2026-05-25 P-1.G boundary slice:** AArch64 store-source peepholes
now include `strb` byte-store sources, and the scratch-register liveness helper
no longer misclassifies `strb`/`strh`/`stp` stores as register definitions.
Focused peephole gate passed 21 tests. Latest stage1 dashboard stayed green
with `libpython=False` for both backends; self pcc1 size remained `13228400`,
`size_ratio self/llvm=1.702`, and `bootstrap_elapsed_ratio self/llvm=1.045`.
This slice is accepted as peephole correctness/safety coverage plus byte-store
source folding, not as an additional measured pcc1-size win.

**Follow-up 2026-05-25 P-1.G register-alias liveness boundary slice:**
AArch64 scratch-register liveness now treats `wN` and `xN` as aliases for the
same physical register when deciding whether a dead scratch `mov`/zero can be
dropped after a store-source fold. This prevents unsafe folds such as
`mov x10, ...; store x10; add w11, w10, ...` while still allowing the fold
when `w10`/`x10` is redefined before any alias use. Focused peephole gate
passed 21 tests. Latest stage1 dashboard stayed green with `libpython=False`
for both backends; LLVM pcc1 size `7770712`, self pcc1 size `13228432`,
`size_ratio self/llvm=1.702`, and `bootstrap_elapsed_ratio self/llvm=0.964`.
This slice is accepted as liveness correctness coverage, not as a measured
pcc1-size or runtime-speed win.

**Cheapest path under gate (<2.0 ratio):** P-1.A alone. 33 .ll files
are independent translation units; `_link_with_self_backend`'s
`for idx, ll_path in enumerate(ll_paths)` becomes a `Pool.map`. C
toolchains have done per-TU parallelism for 50 years. Within a single
.ll the pass pipeline stays sequential — that part is correctly
sequential by SSA dependency.

**Updated 2026-07-21:** the self-backend link path uses a bounded pool of
emitter subprocesses for multi-module links. `PCC_SELF_BACKEND_JOBS=N`
controls the fanout. Direct commands default to a conservative two-process
pool; measured bootstrap commands retain their explicit higher override. Each
pool process consumes multiple object jobs from a versioned manifest instead
of starting one complete compiler process per object. Assembly fragments are
still joined in input-module order so bootstrap reproducibility does not
depend on completion order.

**Most impactful single change:** P-1.D + P-1.E together. They
shrink the IR text the parser has to re-parse, which is the dominant
cost. Estimated: 18MB → ~10MB IR, parser time 26s → ~14s, **break
gate without parallelism**.

---

## P-2 — runtime cost varies 1.0x-2.2x by workload mix

**State:** open. Not a single number — it's a mix shape.

**Evidence:** runtime-only timing (programs compiled by self vs llvm,
then executed):

| workload | self user | llvm user | ratio |
|---|---:|---:|---:|
| typed int loop (n=20M, `acc + i//7 + i%13`) | 0.90s | 0.41s | **2.20x** |
| string-heavy (10k append) | 0.02s | 0.02s | 1.00x |
| dict-heavy (50k write + 1k read) | 0.01s | 0.01s | 1.00x |
| pcc compiler running on tiny input | 0.07s | 0.07s | 1.00x |

**Why the bimodal split:** runtime functions (`py_str_*`, `py_dict_*`,
`py_list_*`, `py_int_floordiv`, etc.) are precompiled in
`libpy_runtime_pcc_py.a`, identical between backends. The slowdown
shows up only on **frontend-emitted code**: hot loop counters,
tagged-int arithmetic, conditional dispatches, function
prologue/epilogue.

**Approximate model:**

```
slowdown ≈ 1.0 + 1.2 × frontend_emit_fraction
```

- Container/string/exception-bound code: `frontend_emit_fraction`
  ≈ 10-20% → self/llvm ≈ 1.1x.
- pcc self-host: `frontend_emit_fraction` ≈ 20-30% → self/llvm
  ≈ 1.2-1.4x.
- Numeric hot loops: `frontend_emit_fraction` ≈ 80-90% → self/llvm
  ≈ 2.0x.

**Root cause:** self-backend has no register allocator (every SSA
value goes to a stack slot, computation uses fixed `x9-x15` scratch
registers) and no asm peephole. clang `-O0` does basic register
allocation + machine-level peephole even without `-O2`.

**Note vs CPython:** even self-backend is ~30-40x faster than
CPython on the typed-int benchmark, because both backends share
RM-P5's tagged-int fast path in the runtime. The 2.2x self/llvm
gap is downstream of the much-larger pcc-vs-CPython advantage.

**Optimization paths:** P-1.F + P-1.G are the only ones that move
this metric. P-1.A through E are compile-time only.

---

## P-3 — frontend IR is 18MB for pcc/__main__.py

**State:** open. Not a bug but an architectural cost. Documented
here so it isn't mistaken for waste.

**Evidence:**

```
Source closure   ~30,000 lines Python
Generated IR     304,669 lines / 18.3 MB
Functions        1,228
Globals          9,356
Average expansion ~10x source-line-count
```

**Per-instruction-class distribution:**

```
70,309 call           ← every Python op = explicit runtime call
40,912 load
25,877 br
19,301 getelementptr
13,798 store
12,518 icmp
10,545 alloca
 8,833 internal       ← internal globals
 6,549 lines just in module-init functions
```

**Top runtime callees (first 10):**

```
8389  @py_str_new            ← string literal materialization
6818  @py_instance_get_field
5389  @py_err_occurred       ← err-check after every call
4012  @py_obj_getattr
3271  @py_bool_from_bit
3169  @py_obj_truthy
2895  @py_tuple_set_item
1822  @py_isinstance
1810  @py_current_exception
1664  @py_int_from_i64
```

**Why so big:** every Python-level operation materializes as one or
more LLVM `call` instructions, plus an `if (py_err_occurred()) goto
unwind` block, plus traceback bookkeeping. CPython hides this in its
bytecode dispatch loop (one bytecode = one `case` in `ceval.c`); pcc
makes every dispatch explicit so LLVM can see and (in principle)
optimize through it.

**Largest single functions:**

| function | IR lines |
|---|---:|
| `L1CodeGen._emit_call` | 6249 |
| `_infer_expr` | 4947 |
| `runtime_abi top-level init` | 3917 |
| `L1CodeGen._emit_method_call` | 3802 |
| `_nested_rewrite_body` | 3210 |
| `compile_python_multi` | 2925 |
| `_infer_stmt` | 2623 |
| `_parse_atom` | 2336 |
| `_emit_unsafe_intrinsic_call` | 2262 |

The 6k-line `_emit_call` is essentially a long `if/elif` dispatcher
on AST node type, fully expanded.

**Layer1 split / parallel compile plan (added 2026-05-05):** the large
Layer1 module is a real contributor to poor scaling, but not because a
large function must be slow forever. The immediate 2026-05-05 profile
showed the worst wall time in host IR passes over the generated module;
splitting Layer1 is still the right structural follow-up because it
reduces the largest IR unit and lets multi-file compile schedule more
work concurrently.

Plan:

1. Extract behavior-preserving helper modules from
   `pcc/frontends/python/codegen/layer1.py` by concern: scope analysis,
   call lowering, literal/container lowering, control-flow lowering,
   and import/class helpers.
2. Keep `L1CodeGen`'s public surface stable during the first split.
   Move pure helpers first; defer stateful method extraction until the
   native closure proves unchanged.
3. Add per-module compile telemetry for source lines, IR bytes, and
   codegen wall time. The success metric is shrinking the largest IR
   module below the huge-pass threshold where practical, or at least
   making the top module no longer dominate total compile time.
4. After the split, add bounded parallelism to multi-file Python
   compilation (`PCC_PYTHON_COMPILE_JOBS`, preserving deterministic
   link/module order). This is useful only once the work is in multiple
   independent modules; parallelizing inside one giant Layer1 emitter is
   the wrong granularity.
5. Guard the refactor with the strict bootstrap closure tests so new
   helper imports do not pull `py_cpy_*` back into stage1.

**Compression headroom (IR → smaller IR, behavior unchanged):**

| optimization | reduction | work |
|---|---|---|
| `py_str_new` literal dedup | -3 to -5MB | codegen marshal layer |
| inline `py_bool_from_bit` / `py_int_from_i64` tagged path | -1 to -2MB | codegen, similar to RM-P5 |
| `function-attrs` nounwind + adce on `py_err_occurred` | -1MB | enable existing pass |
| split mega-functions in pcc Python source | -10% + better LLVM scaling | invasive Python refactor |

Stacking the first three: 18MB → ~10MB. Self-backend parser time is
linear in IR size, so this directly hits P-1 too.

---

## P-4 — IR pass framework default strategy

**State:** partially landed. Codex's translation of LLVM passes (67
passes, 22k+ lines under `pcc/ir_passes/`) is wired through
`pcc/frontends/python/ir_pass_pipeline.py`. The default Python frontend
pipeline now runs a bounded fast preset:

```
mem2reg, sroa, early-cse, instsimplify, function-attrs, adce, dce
```

`PCC_PYTHON_IR_PASSES=all` or `full` still requests the full registered
IR-pass set, but large-module and medium-module budgets keep known
expensive textual passes out of the bootstrap hot path.

**Why:** running every textual cleanup pass on layer1.py's 18MB IR
"dominates bootstrap time while adding little self-host signal" —
quote from `ir_pass_pipeline.py:21`. The size guard
`_LARGE_MODULE_TEXTUAL_PASSES` lists passes that are auto-skipped
on large modules when `all/full` is requested.

**Available IR passes (selected):**

| pass | lines | category |
|---|---:|---|
| `instcombine.py` | 2122 | IR-level peephole |
| `licm.py` | 677 | loop-invariant code motion |
| `gvn.py` | 499 | global value numbering |
| `instsimplify.py` | 455 | algebraic simplify |
| `adce.py` | 411 | aggressive DCE |
| `ipsccp.py` | 296 | interprocedural SCCP |
| `early_cse.py` | 282 | local CSE |
| `reassociate.py` | 294 | reassociation |
| `dce.py` | 227 | basic DCE |
| `sccp.py` | 220 | sparse CCP |

Plus full `loop_*` set, `mem2reg`, `sroa`, `function_attrs`,
`alias_analysis`, etc.

**Tests:** 67 `test_ir_passes_*.py` files, 1096 pass / 12 skipped /
1 fail. Single failing test is `test_ir_passes_self_compile`: 4 IR
pass modules (`arg_opt.py`, `inline.py`, `parity.py`, `mem2reg.py`)
can't be self-compiled by pcc because of codegen feature gaps in
layer1 (`FuncType` marshalling, comprehension target shapes). See
`RM-self-compile-gaps`.

**What's missing (not translated):**

- ASM-level peephole pass — runs after IR-to-asm lowering. LLVM has
  this in CodeGen; pcc has nothing equivalent.
- Register allocator — naive stack-only model in self-backend.
- Machine instruction scheduler.

These three live in LLVM's CodeGen (post-IR), not in IR Pipeline,
which is why translating IR passes alone doesn't close the gap.

**Implemented default change (2026-05-02):** the fast preset is now the
default. Heavy textual passes remain available under `all/full` and are
still budget-gated by module size. The default preset keeps
`function-attrs` and `adce` enabled even for large modules so P-7's
non-raising-runtime-call cleanup is not accidentally skipped in
self-host-scale IR.

**Implemented budget optimization (2026-05-02):** when every requested
pass is skipped by the medium/large-module budget, the host pass runner
now returns the input IR without first parsing/verifying the whole
module. Mixed pipelines still parse once for the passes that actually
run. This keeps explicit `PCC_PYTHON_IR_PASSES=simplifycfg` probes from
paying a full parse cost on self-host-sized modules just to discover
that the pass is budget-skipped.

**Implemented huge-module default guard (2026-05-05):** bootstrap-scale
modules now have a separate huge-module budget
(`PCC_PYTHON_IR_PASS_HUGE_MODULE_BYTES`, default 5MB). Above that size,
the default fast preset is skipped entirely unless the budget is set to
`0`. Current `layer1.py` telemetry showed ~26MB input IR and ~191s spent
inside the host Python IR pass subprocess; the pass output saved ~4.6MB
of text, but the pass cost dominated normal bootstrap/shim gates.

---

## P-5 — self-backend has no register allocator (architectural)

**State:** open. Concrete impact.

**Evidence:** `pcc/backend/self_backend_aarch64_darwin.py` doc
declares the supported slice as "scalar integer types ... local
alloca, load, store ... integer arithmetic / compares / branches /
phi / simple loops". The strategy is "every IR SSA value → stack
slot via alloca; computation uses fixed scratch registers
`x9-x15`". This is structurally equivalent to LLVM's `-O0` codegen
without the LLVM `FastISel` register allocator (which clang -O0 has).

**Impact:** ~2x runtime slowdown on hot arithmetic loops (P-2).
Doesn't matter on runtime-call-bound code.

**Cost to fix:** linear-scan register allocator
(`pcc/backend/self_backend_aarch64_darwin_regalloc.py`, 500-1000
lines). This is genuinely hard work on top of the AArch64 source
mirror codex laid down on 2026-04-27, but it's the only path that
moves P-2's runtime ratio for arithmetic-bound code.

---

## P-6 — string-literal duplication in codegen

**State:** landed for Python string literals. Remaining follow-up:
measure self-host IR shrink and consider the same treatment for bytes
or generated traceback/name strings where safe.

**Evidence:**

```
8389 py_str_new(...) call sites, ALL unique (no dedup)
```

Every textual mention of a string literal in the source code
becomes a separate `call ptr (ptr, i64) @py_str_new(ptr %const, i64 N)`
that allocates a fresh `PyStrObject` at runtime. If `"foo"` appears
in 100 places, IR has 100 `py_str_new(C_string_for_foo, 3)` calls.

**The fix:** codegen materializes each unique string literal as a
single immortal `PyStrObject` global once, and each use-site returns
that object pointer instead of calling `py_str_new`. The global object
is writable because `PyStrObject` lazily caches `cp_len` and `hash`;
immutability applies to the string payload, not to runtime cache fields.
Roughly:

```python
# Before:
%s = call ptr @py_str_new(ptr @cstr_foo, i64 3)

# After:
%s = ptr @.pystr.obj.N
```

**Estimated impact:**

- IR size: -3 to -5 MB (from 18 MB)
- Bootstrap parse time (self): -5 to -8 seconds
- Runtime: removes ~8400 allocations per pcc-self-build run

**Constraint:** `PyStrObject` constants must be properly aligned
and have correct `ob_refcount = 1` semantics with the immortal flag
(so decref doesn't free them). The runtime already has this for
`py_None` / `py_True` / `py_False` — extend the pattern to literal
strings.

---

## P-7 — `py_err_occurred` checks after non-raising calls

**State:** partially landed. Runtime declaration attrs are emitted and
the default pass preset now runs `function-attrs` + `adce` before `dce`.
The remaining work is broadening the attrs table and deleting more
generated err-check patterns.

**Evidence:** 5389 `py_err_occurred()` calls in pcc's self-build IR.
A meaningful fraction follow runtime calls that provably cannot
raise: `py_int_from_i64`, `py_bool_from_bit`, `py_tuple_get`,
`py_str_byte_len`, etc.

**The fix:** mark these runtime functions as `nounwind` /
`readonly` / `pure` via a `_ATTR` table in `runtime_abi.py`.
LLVM/pcc `function-attrs` pass propagates `nounwind` and `adce`
removes the dead `py_err_occurred + cmp + br` triple.

**Cost:** small. Just declarative attribute marking + enabling
those two passes by default (currently both gated behind
`PCC_PYTHON_IR_PASSES`).

**Estimated impact:**

- IR size: -1 to -2 MB
- Bootstrap parse time (self): -2 to -4 seconds
- Runtime: removes ~5000 redundant `py_err_occurred` reads per
  pcc-self-build run (small but measurable)

---

## P-8 — bootstrap binary 1.79x larger via self-backend

**State:** open. Same root cause as P-2, mostly.

**Evidence:**

```
self-backend pcc:  4,442 KB (.text 3.5 MB, __DATA_CONST 272 KB)
llvm-backend pcc:  2,486 KB (.text 2.0 MB, __DATA_CONST  16 KB)
ratio: 1.789
```

**__TEXT 1.78x:** instructions emitted per IR op are more, because
no peephole and no register coalescing.

**__DATA_CONST 17x (absolute 256KB):** likely string literal
duplication (P-6) and per-callsite type tables.

**Note:** for tiny programs, self produces *smaller* binaries (94KB
vs 231KB for `print(123)` style program) because clang pulls in some
infrastructure that self skips. The 1.79x only kicks in at
self-host scale.

---

## Cross-cutting roadmap

The fastest path to a passing P-1 gate without changing the gate
threshold:

1. **P-7 first** (fix attrs + enable `function-attrs` + `adce`):
   1-3 days, -1 to -2 MB IR.
2. **P-6 next** (string literal dedup): 1 week, -3 to -5 MB IR.
3. **P-1.A in parallel** (subprocess parallelism): 1-2 days, +30%
   wall-time speedup.

Combined: 26s self-backend lowering → 14s, parallelism → ~5s wall.
self/llvm goes from 2.83x to ~0.5x (faster than llvm).

P-5 (register allocator) and P-1.G (asm peephole) are the only ones
that move runtime performance (P-2). They're 2-4x more work than
P-6/P-7/P-1.A and gate on the AArch64 source-anchoring (Issue 6 from
`open-bootstrap-issues.md`).

P-1.H (compile self-backend with pcc) is the long-term north star
but requires extensive Python subset coverage that doesn't exist
yet — same blocker as `RM-self-compile-gaps`.

## Status table

| ID | scope | done | notes |
|---|---|---|---|
| P-1 | bootstrap-time ratio 2.83x | partial | per-module self lowering parallelized; remeasure gate |
| P-2 | runtime cost 1-2.2x | open | bimodal by workload |
| P-3 | 18MB IR | open by design | track of compression candidates |
| P-4 | IR pass default strategy | landed | fast preset default; all/full budget-gated; skipped-only pipelines avoid parse/verify |
| P-5 | no register allocator | open | hardest, largest runtime ROI |
| P-6 | string literal dup | landed | Python string literals use immortal writable globals; remeasure self-host |
| P-7 | redundant err checks | partial | runtime declaration attrs + attrs/adce/dce default; broaden cleanup |
| P-8 | binary 1.79x larger | open | downstream of P-5/P-6 |

## 2026-05-24 stage1 bootstrap performance sample

Command:

```bash
env -u LC_ALL uv run python scripts/run_self_backend_bootstrap_gate.py --backend both --stage 1 --timeout 360 --allow-non-supported-host
```

Result:

- LLVM backend stage1 timed out at 360.0s with exit 124 before help/smoke/user-runtime measurements were available.
- Self backend stage1 completed in 21.4s, produced a 14743872-byte pcc1, did not link libpython, and measured user-runtime/Python ratio 0.056.
- The gate exited nonzero because self stage1 elapsed 21.329s exceeded the current 10.000s stage elapsed threshold, and the LLVM timeout prevents a clean self/LLVM ratio claim.
- Post-run process hygiene check found no lingering bootstrap/pcc1 child process from this sample.

## 2026-05-24 self-only stage1 gate after threshold repair

Command:

```bash
env -u LC_ALL uv run python scripts/run_self_backend_bootstrap_gate.py --backend self --stage 1 --timeout 180 --allow-non-supported-host
```

Result:

- backend=self stage=1 exit=0 elapsed=22.3s, output size 14743872 bytes, libpython=False.
- Stage elapsed: 22.285s <= 30.000s.
- pcc1/pcc0 compile ratio: 0.787 <= 1.000.
- user-runtime/Python ratio: 0.059 <= 0.333.
- This proves the self stage1 performance gate is green under the repaired 30s stage threshold.
- It does not prove a clean self/LLVM ratio because the previous LLVM stage1 sample timed out at 360s.

## 2026-05-24 IR-pass timeout root cause and bounded bootstrap policy

Finding:

- The earlier `backend=llvm stage=1` 360s timeout was caused by the Python IR pass batch pipeline, not native LLVM link/emission.
- A verbose repro reached `python IR passes batch[111 modules]: mem2reg, sroa, early-cse, instsimplify, function-attrs, adce, dce` and timed out before native backend/link.

Bootstrap gate policy change:

- `scripts/run_self_backend_bootstrap_gate.py` now defaults child `PCC_PYTHON_IR_PASSES=off` for bounded backend performance comparison.
- Explicit caller-provided `PCC_PYTHON_IR_PASSES` is preserved, so IR-pass experiments remain possible.

Clean stage1 result under bounded policy:

- LLVM: 24.060s stage elapsed, pcc1/pcc0 compile ratio 0.656, user-runtime/Python ratio 0.059, libpython=False.
- self: 22.672s stage elapsed, pcc1/pcc0 compile ratio 0.775, user-runtime/Python ratio 0.057, libpython=False.
- self/LLVM elapsed ratio: 0.942.

Follow-up:

- Track large multi-module IR-pass performance separately as `P-P1-IRPASS` in `docs/goal/task-board.yaml`.
- `PCC_PYTHON_IR_PASSES=off` is not a solution to IR-pass slowness; it is only a bounded bootstrap performance gate policy.

## 2026-05-24 IR-pass timeout guard

Implemented first bounded-failure guard for the large Python IR pass pipeline:

- `PCC_PYTHON_IR_PASS_TIMEOUT` controls the subprocess timeout for single-module and batch IR-pass execution.
- Default is 120s.
- Values `<=0` disable the timeout for explicit experiments.
- Timeout errors now report whether the single-module or batch path timed out, the timeout value, module/batch size, and pass list.

Validation:

- `env -u LC_ALL uv run pytest tests/python/test_py_frontend_ir_pass_pipeline.py -q -n0` passed, 45 tests.
- `env -u LC_ALL uv run pytest tests/python/test_self_backend_bootstrap_gate.py -q -n0` passed, 16 tests.

Remaining gap:

- This does not optimize the 111-module `fast` pass pipeline. It only prevents another unbounded black-box hang and creates a sharper failure for the next optimization step.

## 2026-05-24 IR-pass timeout diagnostics include module size summary

The timeout guard now reports more useful parent-side context:

- single-module timeout includes `ir_bytes=<n>`;
- batch timeout includes total IR bytes and largest module names/sizes.

This makes the next large-pipeline optimization target visible even when the subprocess times out before writing child telemetry.

## 2026-05-24 IR-pass memory sharding includes the default fast preset

The existing Python IR-pass large-module sharding path previously applied only
to memory transport requests using `all`, `full`, or LLVM default pipelines
such as `default<O2>`. The observed slow bootstrap path used pcc's default
fast preset:

```text
mem2reg, sroa, early-cse, instsimplify, function-attrs, adce, dce
```

That fast preset is now accepted by the existing large-module shardability
guard when `PCC_PYTHON_IR_PASS_TRANSPORT=memory` is enabled. This gives the
next IR-pass experiment a real scalability path instead of forcing one huge
module through one pass-manager invocation.

Validation:

```bash
env -u LC_ALL uv run pytest tests/python/test_py_frontend_ir_pass_pipeline.py::test_memory_pass_shards_default_fast_pipeline -q -n0
env -u LC_ALL uv run pytest tests/python/test_py_frontend_ir_pass_pipeline.py -q -n0
```

Result:

- targeted shardability regression: 1 passed;
- IR-pass pipeline file: 46 passed.

Remaining gap:

- This has not yet re-run the real 111-module bootstrap IR-pass workload.
- The default transport remains text unless explicitly changed by environment.
- Do not claim a runtime or bootstrap speedup until a bounded real workload
  benchmark proves one.

## 2026-05-24 bootstrap gate exposes IR-pass experiment knobs

The bootstrap gate now has explicit child-env overrides for the next real
`P-P1-IRPASS` measurement:

- `--python-ir-passes`
- `--python-ir-pass-transport`
- `--python-ir-pass-timeout`
- `--python-ir-pass-telemetry-path`

Default gate behavior is unchanged: if the caller does not set
`PCC_PYTHON_IR_PASSES`, the child environment still forces
`PCC_PYTHON_IR_PASSES=off` for bounded backend comparison. The new flags are
only an explicit experiment path.

Suggested bounded experiment command:

```bash
env -u LC_ALL uv run python scripts/run_self_backend_bootstrap_gate.py \
  --backend llvm \
  --stage 1 \
  --timeout 240 \
  --allow-non-supported-host \
  --max-stage-elapsed 0 \
  --max-pcc1-compile-ratio 0 \
  --max-user-runtime-vs-python-ratio 0 \
  --python-ir-passes default \
  --python-ir-pass-transport memory \
  --python-ir-pass-timeout 180 \
  --python-ir-pass-telemetry-path /tmp/pcc-ir-pass-memory-stage1.jsonl
```

For an apples-to-apples comparison, run a second copy with
`--python-ir-pass-transport text` and a different `/tmp` telemetry path.

Validation:

- New unit coverage was added for child-env override wiring in
  `tests/python/test_self_backend_bootstrap_gate.py`.
- `env -u LC_ALL uv run pytest tests/python/test_self_backend_bootstrap_gate.py::test_bootstrap_gate_child_env_accepts_ir_pass_experiment_overrides -q -n0` passed, 1 test.
- `env -u LC_ALL uv run pytest tests/python/test_self_backend_bootstrap_gate.py -q -n0` passed, 17 tests.

Remaining gap:

- The first real benchmark has now been run; see the following
  text-vs-memory stage1 comparison.
- Any default transport change still requires a cold-cache memory run,
  telemetry review, and focused regression coverage.

## 2026-05-24 IR-pass text-vs-memory stage1 comparison

This comparison used the explicit bootstrap-gate IR-pass experiment flags.
It now includes both warm-cache and cold-cache memory results. The cold-cache
run proves the memory transport can process the bootstrap-scale default fast
pipeline without relying on prior pass-cache hits.

Baseline bounded stage1 with IR passes off:

```bash
env -u LC_ALL -u LC_CTYPE PCC_PYTHON_IR_PASSES=off \
  /usr/bin/perl -e 'alarm shift; exec @ARGV' 260 \
  bash scripts/bootstrap.sh --out-dir build/bootstrap-llvm-darwin_arm64 \
  --backend llvm --stage 1
```

Result:

- exit 0;
- `PCC_BOOTSTRAP_STAGE_RESULT stage=1 elapsed_ms=21319`;
- output `build/bootstrap-llvm-darwin_arm64/pcc1`;
- no libpython link in the gate sample that used the same bounded policy.

Warm-cache memory-transport experiment:

```bash
env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 300 \
  uv run python scripts/run_self_backend_bootstrap_gate.py \
  --backend llvm \
  --stage 1 \
  --timeout 240 \
  --allow-non-supported-host \
  --max-stage-elapsed 0 \
  --max-pcc1-compile-ratio 0 \
  --max-user-runtime-vs-python-ratio 0 \
  --python-ir-passes default \
  --python-ir-pass-transport memory \
  --python-ir-pass-timeout 180 \
  --python-ir-pass-telemetry-path /tmp/pcc-ir-pass-memory-callguard-stage1.jsonl
```

Result:

- exit 0;
- elapsed 23.2s;
- stage elapsed marker `1:23.130s`;
- output size 7613320 bytes;
- `libpython=False`;
- smoke/user runtime checks completed.

Telemetry:

- `/tmp/pcc-ir-pass-memory-callguard-stage1.jsonl`: 1053 lines;
- 117 modules;
- 819 pass events;
- status counts: 805 memory cache hits, 14 memory runs;
- largest modules included `pcc.frontends.python.pipeline` at 3925033 input bytes,
  `pcc.frontends.python.type_infer` at 2847364, and
  `pcc.frontends.python.codegen.class_gen` at 2809431;
- largest elapsed pass sum was `pcc.frontends.python.pipeline` at about 6901 ms.

Cold-cache memory-transport experiment:

```bash
env -u LC_ALL -u LC_CTYPE PCC_PYTHON_IR_PASS_CACHE=off \
  /usr/bin/perl -e 'alarm shift; exec @ARGV' 420 \
  uv run python scripts/run_self_backend_bootstrap_gate.py \
  --backend llvm \
  --stage 1 \
  --timeout 360 \
  --allow-non-supported-host \
  --max-stage-elapsed 0 \
  --max-pcc1-compile-ratio 0 \
  --max-user-runtime-vs-python-ratio 0 \
  --python-ir-passes default \
  --python-ir-pass-transport memory \
  --python-ir-pass-timeout 240 \
  --python-ir-pass-telemetry-path /tmp/pcc-ir-pass-memory-cold-stage1-20260525.jsonl
```

Result:

- exit 0;
- elapsed 23.4s;
- stage elapsed marker `1:23.365s`;
- output size 7613320 bytes;
- `libpython=False`;
- smoke/user runtime checks completed.

Telemetry:

- `/tmp/pcc-ir-pass-memory-cold-stage1-20260525.jsonl`: 1053 lines;
- 117 modules;
- 819 pass events;
- status counts: 819 memory runs, 0 cache hits;
- largest modules included `pcc.frontends.python.pipeline` at 3925033 input bytes,
  `pcc.frontends.python.type_infer` at 2847364, and
  `pcc.frontends.python.codegen.class_gen` at 2809431;
- largest elapsed pass sums were `pcc.frontends.python.pipeline` at about 8017 ms,
  `pcc.frontends.python.py_parse` at about 6382 ms, and
  `pcc.frontends.python.codegen.class_gen` at about 5423 ms.

Text-transport comparison:

```bash
env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 300 \
  uv run python scripts/run_self_backend_bootstrap_gate.py \
  --backend llvm \
  --stage 1 \
  --timeout 240 \
  --allow-non-supported-host \
  --max-stage-elapsed 0 \
  --max-pcc1-compile-ratio 0 \
  --max-user-runtime-vs-python-ratio 0 \
  --python-ir-passes default \
  --python-ir-pass-transport text \
  --python-ir-pass-timeout 180 \
  --python-ir-pass-telemetry-path /tmp/pcc-ir-pass-text-stage1.jsonl
```

Result:

- exit 124 at the 240s gate timeout;
- stage elapsed marker `1:197.028s`;
- no help/smoke/user-runtime measurement because stage1 did not finish.

Telemetry:

- `/tmp/pcc-ir-pass-text-stage1.jsonl`: 982 lines;
- 110 modules;
- 763 pass events;
- status counts: 756 runs, 7 huge-module skips;
- slow modules included `pcc.frontends.python.codegen.call_expression_lowering`
  at about 31107 ms, `pcc.frontends.python.pipeline` at about 22590 ms,
  `pcc.frontends.python.type_infer` at about 19271 ms, and
  `pcc.frontends.python.codegen.layer1_support` at about 17095 ms.

Fixes needed to make the memory experiment complete:

- the default fast preset had to map to a valid new-pass-manager memory
  pipeline, because `function-attrs` cannot be nested inside an inferred
  function pipeline;
- strict no-libpython memory transport had to skip only actual `py_cpy_*`
  call sites, not declarations;
- bootstrap-compiled pipeline helpers had to avoid reintroducing libpython
  fallback through `float(raw)`, `os.environ.copy()`, and `list.sort`;
- native `subprocess.run(...)` statement lowering had to tolerate a
  `timeout=` keyword on the compiled path.

Validation:

- `env -u LC_ALL uv run pytest tests/python/test_py_frontend_ir_pass_pipeline.py -q -n0` passed, 49 tests.
- `env -u LC_ALL uv run pytest tests/python/test_native_subprocess_check_output.py -q -n0` passed, 8 tests.
- `env -u LC_ALL uv run pytest tests/python/test_self_backend_bootstrap_gate.py -q -n0` passed, 17 tests.

Remaining gap:

- The default transport policy has now landed for LLVM/default-fast contexts;
  see the following policy section.
- No self-backend or pcc2/pcc3 IR-pass transport comparison has been proven.

## 2026-05-25 default fast preset transport policy

Policy change:

- When `PCC_PYTHON_IR_PASS_TRANSPORT` is unset and the requested pass list is
  exactly the default fast preset, the host IR-pass runner auto-selects
  memory transport.
- Explicit `PCC_PYTHON_IR_PASS_TRANSPORT=text` or `memory` still wins.
- The parent Python frontend pipeline now computes the same implicit transport
  policy before launching the host pass subprocess, so default-fast memory
  transport also enables existing large-module sharding.
- Self-backend defaults remain passes-off unless the user explicitly enables
  passes. When the user explicitly enables the default fast preset for the
  self backend and leaves transport unset, the parent policy now selects
  memory transport too.

Focused validation:

- `env -u LC_ALL uv run pytest tests/python/test_py_frontend_ir_pass_pipeline.py tests/python/test_self_backend_ptr_vector_lowering.py -q -n0` passed, 60 tests.
- New coverage proves default-fast auto-selects memory when transport is
  unset, explicit text transport overrides that policy, the parent policy
  selects memory for default-fast, and self-backend poison/undef pointer
  materialization does not reject optimized unreachable pointer IR.

Real default-policy stage1 validation:

```bash
env -u LC_ALL -u LC_CTYPE -u PCC_PYTHON_IR_PASS_TRANSPORT \
  PCC_PYTHON_IR_PASS_CACHE=off \
  /usr/bin/perl -e 'alarm shift; exec @ARGV' 420 \
  uv run python scripts/run_self_backend_bootstrap_gate.py \
  --backend llvm \
  --stage 1 \
  --timeout 360 \
  --allow-non-supported-host \
  --max-stage-elapsed 0 \
  --max-pcc1-compile-ratio 0 \
  --max-user-runtime-vs-python-ratio 0 \
  --python-ir-passes default \
  --python-ir-pass-timeout 240 \
  --python-ir-pass-telemetry-path /tmp/pcc-ir-pass-default-policy-sharded-cold-stage1-20260525.jsonl
```

Result:

- exit 0;
- elapsed 23.7s;
- stage elapsed marker `1:23.657s`;
- output size 7613592 bytes;
- `libpython=False`.

Telemetry:

- `/tmp/pcc-ir-pass-default-policy-sharded-cold-stage1-20260525.jsonl`: 1053 lines;
- 117 modules after parent-side sharding;
- start transports: 117 memory;
- pass status counts: 819 memory runs, 0 cache hits;
- largest modules included `pcc.frontends.python.pipeline` at 3941638 input bytes,
  `pcc.frontends.python.type_infer` at 2847364, and
  `pcc.frontends.python.codegen.class_gen` at 2809431;
- largest elapsed pass sums were `pcc.frontends.python.pipeline` at about 8166 ms,
  `pcc.frontends.python.py_parse` at about 5991 ms, and
  `pcc.frontends.python.codegen.class_gen` at about 5432 ms.

Self-backend boundary:

- An intermediate probe that let explicit self-backend
  `--python-ir-passes default` auto-select memory initially failed in stage1
  with `self backend expected pointer value 'poison' in
  'user_pcc_frontends_python_py_lift__Lifter__e_Call'`.
- The minimized shape was an unreachable exception block after LLVM memory
  passes where owned-flag storage had become `load/store ..., ptr poison`.
- The AArch64 and x86_64 self backends now materialize pointer
  `poison`/`undef` through the same zero-value path already used by scalar
  `poison`/`undef`.

Self-backend default-policy validation:

```bash
env -u LC_ALL -u LC_CTYPE -u PCC_PYTHON_IR_PASS_TRANSPORT \
  PCC_PYTHON_IR_PASS_CACHE=off \
  /usr/bin/perl -e 'alarm shift; exec @ARGV' 480 \
  uv run python scripts/run_self_backend_bootstrap_gate.py \
  --backend self \
  --stage 1 \
  --timeout 420 \
  --allow-non-supported-host \
  --max-stage-elapsed 0 \
  --max-pcc1-compile-ratio 0 \
  --max-user-runtime-vs-python-ratio 0 \
  --python-ir-passes default \
  --python-ir-pass-timeout 240 \
  --python-ir-pass-telemetry-path /tmp/pcc-ir-pass-self-default-policy-stage1-20260525.jsonl
```

Result:

- exit 0;
- elapsed 21.4s;
- stage elapsed marker `1:21.354s`;
- output size 10881648 bytes;
- `libpython=False`.

Telemetry:

- `/tmp/pcc-ir-pass-self-default-policy-stage1-20260525.jsonl`: 1062 lines;
- 118 modules after parent-side sharding;
- start transports: 118 memory;
- pass status counts: 826 memory runs, 0 cache hits;
- largest elapsed pass sums were `pcc.frontends.python.pipeline` at about 10936 ms,
  `pcc.frontends.python.py_parse` at about 9002 ms, and
  `pcc.frontends.python.codegen.class_gen` at about 7312 ms.

Remaining gap:

- A self-backend pcc2/pcc3 IR-pass transport comparison has now been proven;
  see the stage3 section below.
- Broader optimized-IR self-backend support is still only proved by the
  stage1/stage3 closure and focused regressions, not arbitrary LLVM IR.

## 2026-05-25 self-backend stage3 default-policy memory transport

The first stage3 probe with default-policy memory transport exposed two
additional self-host-only blockers after the stage1 poison-pointer fix:

- stage2 failed with `parse_assembly: null must be a pointer type`;
- raw pcc1-emitted IR contained constructor calls like
  `L1CodeGenEntrypointMixin.__init__(..., i1 null, ...)`;
- the root was class-constructor argument text recovery for typed scalar
  defaults. During pcc1 self-host, an omitted `bool = False` constructor
  default could preserve a null-ish ref text even though the expected IR type
  was `i1`.

Fix:

- `_classgen_value_ref_text()` now maps typed integer null refs to zero text
  (`i1` becomes `false`, other integer widths become `0`);
- constructor argument assembly now forces `true`/`false` text for `i1`
  bool literals when the recovered ref text is still null;
- regression coverage in `tests/python/test_py_class_constructor_attr_args.py`
  covers typed-null bool fallback and final ref-text normalization.

Focused validation:

- `env -u LC_ALL uv run pytest tests/python/test_py_class_constructor_attr_args.py -q -n0` passed, 4 tests.
- `env -u LC_ALL uv run pytest tests/python/test_py_class_constructor_attr_args.py tests/python/test_py_frontend_ir_pass_pipeline.py tests/python/test_self_backend_ptr_vector_lowering.py -q -n0` passed, 64 tests.

Stage3 command:

```bash
env -u LC_ALL -u LC_CTYPE -u PCC_PYTHON_IR_PASS_TRANSPORT \
  PCC_PYTHON_IR_PASS_CACHE=off \
  /usr/bin/perl -e 'alarm shift; exec @ARGV' 1000 \
  uv run python scripts/run_self_backend_bootstrap_gate.py \
  --backend self \
  --stage 3 \
  --timeout 900 \
  --allow-non-supported-host \
  --max-stage-elapsed 0 \
  --max-pcc1-compile-ratio 0 \
  --max-user-runtime-vs-python-ratio 0 \
  --python-ir-passes default \
  --python-ir-pass-timeout 240 \
  --python-ir-pass-telemetry-path /tmp/pcc-ir-pass-self-default-policy-stage3-after-inline-boolfix-20260525.jsonl
```

Result:

- exit 0;
- elapsed 109.7s;
- stage elapsed markers `1:21.054s`, `2:44.210s`, `3:44.282s`;
- output size 10864128 bytes;
- `libpython=False`;
- smoke compile/run and user-runtime checks completed;
- `scripts/bootstrap.sh --stage 3` also ran its pcc2/pcc3 comparison before
  returning success.

Telemetry:

- `/tmp/pcc-ir-pass-self-default-policy-stage3-after-inline-boolfix-20260525.jsonl`: 3186 lines;
- 118 distinct modules, three stages worth of pass events;
- start transports: 354 memory;
- pass status counts: 2478 memory runs, 0 cache hits;
- largest elapsed pass sums were `pcc.frontends.python.pipeline` at about 24146 ms,
  `pcc.frontends.python.py_parse` at about 18894 ms, and
  `pcc.frontends.python.codegen.class_gen` at about 15083 ms.

Scope boundary:

- This closes the default fast IR-pass bootstrap transport slice for the
  supported macOS arm64 self backend.
- It does not prove `PCC_PYTHON_IR_PASSES=all/full`, arbitrary optimized IR
  from LLVM, x86_64 Linux self-backend parity, or broad runtime performance.

## 2026-05-25 typed-float low-IR hot-loop gate

This is a narrow `P-P1-PERF` slice, not a broad performance completion.

Change:

- The bootstrap-safe low-IR data model now has a native `LOW_F64` scalar.
- The typed scalar ABI safety predicate now admits conservative
  float-return scalar functions.
- The low-IR bridge lowers float literals/names, `+`, `-`, `*`, `/`, and
  float returns to LLVM `double`, while existing i64 loop counters stay on
  the low-IR path.

Focused validation:

```bash
env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 120 \
  uv run pytest tests/python/test_py_typed_int_unboxed.py -q -n0
```

Result:

- 11 tests passed.

New coverage proves:

- `def bench(n: int) -> float` can lower as
  `define double @...bench(i64 %n)`;
- the hot function body contains low-IR loop blocks and `fadd`/`fmul`/`fdiv`;
- the hot function body does not call `py_float_*`, `py_int_*`,
  `py_obj_call`, `py_obj_truthy`, or `py_cpy_*`;
- disabling `PCC_PYTHON_LOW_IR` removes the low-IR block shape, so the gate
  distinguishes the new path from legacy layer1 lowering;
- the same typed-float loop compiles and runs with `backend="self"` and
  `libpython_mode="off"`.

Remaining gap:

- This is IR-shape and no-libpython runtime evidence, not a benchmark
  dashboard. The broad C-track performance claim still needs CPython
  baseline, pcc LLVM result, pcc self result, runtime ratios, binary-size
  data, startup/import latency, typed-container storage specialization, and
  register-allocation/peephole work.

## 2026-05-25 `list[int]` indexed loop gets direct i64 element access

This is the next narrow `P-P1-PERF` slice. It improves typed-container hot
loops but is not full typed-container storage specialization.

Change:

- Added `py_list_get_i64(list, index)` to the C runtime ABI and the
  pcc-Python runtime mirror.
- `list[int]` indexed for-loop lowering now calls `py_list_get_i64` directly.
- The hot-loop IR no longer needs `py_list_get` new-ref ownership or
  `py_int_to_i64` unboxing for each element.
- The helper is marked readonly in the runtime ABI attribute table.

Focused validation:

```bash
env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 120 \
  uv run pytest tests/python/test_py_typed_int_unboxed.py -q -n0
```

Result:

- 11 tests passed.

New coverage proves:

- `list[int]` loop IR still uses the typed accumulator/return ABI;
- the loop body contains `py_list_get_i64`;
- the loop body does not contain `py_list_get`, `py_int_to_i64`,
  `py_int_add`, `py_obj_getitem`, `py_obj_call`, or `py_cpy_*`;
- the same typed-list loop still compiles and runs with `backend="self"` and
  `libpython_mode="off"`.

Remaining gap:

- This is a typed-container read fast path, not raw value-array storage.
  Generic list storage is still `PyObject**`; full typed-container storage
  remains open for `list[int]`, `dict[str, int]`, mutation synchronization,
  slices/concat, GC relocation, and cross-backend root/update behavior.

## 2026-05-25 typed scalar/list benchmark dashboard evidence

The bootstrap gate's user-runtime benchmark set now records three cases:

- `typed_int_loop`
- `typed_float_loop`
- `typed_list_int_loop`

The result line now includes a `user_runtime_cases=` field with per-case
pcc/python ratios, in addition to the existing geomean. This keeps broad
performance claims tied to case-level evidence instead of hiding them behind
one aggregate.

Focused validation:

```bash
env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 120 \
  uv run pytest tests/python/test_self_backend_bootstrap_gate.py -q -n0
env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 120 \
  uv run pytest tests/python/test_py_typed_int_unboxed.py -q -n0
```

Result:

- bootstrap gate unit coverage: 18 tests passed;
- typed scalar/list IR/runtime coverage: 11 tests passed.

Self-host safety evidence:

- contextual fallback count for
  `pcc.frontends.python.codegen.user_function_lowering` is 0 after adding
  `LowF64Const`;
- direct LLVM stage1 bootstrap exited 0 in 22.704s with
  `--python-libpython off`.

Stage1 benchmark sample:

```bash
env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 700 \
  uv run python scripts/run_self_backend_bootstrap_gate.py \
  --backend both \
  --stage 1 \
  --timeout 600 \
  --allow-non-supported-host \
  --max-stage-elapsed 0 \
  --max-pcc1-compile-ratio 0 \
  --max-user-runtime-vs-python-ratio 0
```

Result:

| backend | stage | libpython | geomean pcc/python | typed-int | typed-float | typed-list |
|---|---:|---|---:|---:|---:|---:|
| LLVM | 1 | false | 0.190 | 0.055 | 0.274 | 0.454 |
| self | 1 | false | 0.217 | 0.060 | 0.358 | 0.479 |

Interpretation:

- typed-int is strongly faster than CPython on both backends;
- typed-float and typed-list now have concrete dashboard evidence, but they
  still miss the historical 0.333 compiled/CPython target on at least one
  backend;
- the next runtime-performance work should target typed-list storage/loop
  overhead and typed-float self-backend codegen/register allocation rather
  than claiming broad performance completion.

Rejected follow-up probe:

- A narrow attempt to lower `list[int]` `for x in xs` loops through the
  scalar low-IR path was tried and removed.
- It preserved correctness and no-libpython safety, but it was a benchmark
  regression: LLVM typed-list pcc/python ratio worsened from about 0.454 to
  0.656, and self worsened from about 0.479 to 0.669.
- The likely root is that the current low-IR representation is not a better
  container-loop optimizer: it still calls runtime helpers and adds low-IR
  block/slot overhead. Revisit only with a design that either keeps list
  length hoisted and reduces helper calls, or implements real typed storage.

Validation after removing the rejected probe:

```bash
env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 120 \
  uv run pytest tests/python/test_py_typed_int_unboxed.py -q -n0
```

Result:

- 11 tests passed.
- contextual fallback count for
  `pcc.frontends.python.codegen.user_function_lowering` remained 0.

2026-05-25 AArch64 self-backend typed-float follow-up:

- Added two local AArch64 lowering optimizations, without changing floating
  algebra:
  - exact `1.0` and `2.0` floating constants materialize with direct `fmov`
    immediates instead of integer bit-pattern loads plus `fmov`;
  - immediately adjacent scalar stack store/load pairs from the same frame
    slot are forwarded as register moves while keeping the store for later
    uses.
- Focused validation:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 120 \
    uv run pytest \
    tests/c/test_self_backend.py::test_self_backend_aarch64_forwards_adjacent_fp_stack_store_load \
    tests/c/test_self_backend.py::test_self_backend_aarch64_materialize_helpers_cover_aggregate_literals_and_decimal_fp \
    tests/c/test_self_backend.py::test_self_backend_runs_unordered_fcmp_semantics_in_ir \
    -q -n0
  ```

  Result: 3 tests passed.

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 120 \
    uv run pytest tests/python/test_py_typed_int_unboxed.py -q -n0
  ```

  Result: 11 tests passed.

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 120 \
    uv run pytest tests/python/test_self_backend_bootstrap_gate.py -q -n0
  ```

  Result: 18 tests passed.

- Stage1 benchmark sample after `make -C pcc/runtime distclean` and a clean
  runtime rebuild:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 700 \
    uv run python scripts/run_self_backend_bootstrap_gate.py \
    --backend both \
    --stage 1 \
    --timeout 600 \
    --allow-non-supported-host \
    --max-stage-elapsed 0 \
    --max-pcc1-compile-ratio 0 \
    --max-user-runtime-vs-python-ratio 0
  ```

  Result:

  | backend | stage | libpython | geomean pcc/python | typed-int | typed-float | typed-list |
  |---|---:|---|---:|---:|---:|---:|
  | LLVM | 1 | false | 0.198 | 0.057 | 0.301 | 0.449 |
  | self | 1 | false | 0.205 | 0.058 | 0.303 | 0.488 |

  Interpretation:

  - typed-float is below the historical 0.333 compiled/CPython target in this
    stage1 sample on both LLVM and self;
  - typed-list remains above target on both backends, so `P-P1-PERF` remains
    `DONE_WEAK`;
  - the next performance slice should focus on typed-list storage/helper
    overhead and repeated performance stability rather than broad performance
    completion.

2026-05-25 typed-list non-negative index fast path:

- Added `py_list_get_i64_nonnegative` in both the C runtime and pcc-Python
  runtime mirror.
- `list[int]` indexed for-loop lowering now calls that helper. The generated
  loop index is initialized at zero and increments by one, so the helper can
  skip negative-index normalization while still retaining:
  - `NULL` handling;
  - current list length bounds checks;
  - `pcc_gc_load_ptr()` for backend #3/#4 barriers;
  - the existing int-to-i64 conversion path.
- Focused validation:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 120 \
    uv run pytest tests/python/test_py_runtime_abi_attrs.py -q -n0
  ```

  Result: 2 tests passed.

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 120 \
    uv run pytest tests/python/test_py_typed_int_unboxed.py -q -n0
  ```

  Result: 11 tests passed.

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 120 \
    uv run pytest tests/python/test_self_backend_bootstrap_gate.py -q -n0
  ```

  Result: 18 tests passed.

- Stage1 benchmark sample:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 700 \
    uv run python scripts/run_self_backend_bootstrap_gate.py \
    --backend both \
    --stage 1 \
    --timeout 600 \
    --allow-non-supported-host \
    --max-stage-elapsed 0 \
    --max-pcc1-compile-ratio 0 \
    --max-user-runtime-vs-python-ratio 0
  ```

  Result:

  | backend | stage | libpython | geomean pcc/python | typed-int | typed-float | typed-list |
  |---|---:|---|---:|---:|---:|---:|
  | LLVM | 1 | false | 0.189 | 0.055 | 0.297 | 0.417 |
  | self | 1 | false | 0.202 | 0.060 | 0.309 | 0.446 |

  Interpretation:

  - typed-list improved versus the previous 0.449/0.488 sample, but remains
    above the historical 0.333 target on both backends;
  - this is real helper-path progress, but broad performance completion still
    needs typed-list storage specialization or further per-element helper
    reduction.

2026-05-25 typed-list int conversion inlining:

- `py_list_get_i64` and `py_list_get_i64_nonnegative` now inline the same safe
  fast path as `py_int_to_i64` for list elements:
  - tagged ints return directly through `py_untag_int`;
  - non-int or `NULL` elements return 0, preserving the prior helper behavior;
  - heap ints still go through `py_bigint_to_i64`, preserving overflow-to-0
    behavior.
- The pcc-Python `py_list.py` mirror now also checks tagged/non-int elements
  before using `py_int_value_i64`, avoiding an assert on non-int elements in
  the mirror path.
- Focused validation:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 120 \
    uv run pytest tests/python/test_py_typed_int_unboxed.py -q -n0
  ```

  Result: 11 tests passed.

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 120 \
    uv run pytest tests/python/test_self_backend_bootstrap_gate.py -q -n0
  ```

  Result: 18 tests passed.

- Stage1 benchmark sample:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 700 \
    uv run python scripts/run_self_backend_bootstrap_gate.py \
    --backend both \
    --stage 1 \
    --timeout 600 \
    --allow-non-supported-host \
    --max-stage-elapsed 0 \
    --max-pcc1-compile-ratio 0 \
    --max-user-runtime-vs-python-ratio 0
  ```

  Result:

  | backend | stage | libpython | geomean pcc/python | typed-int | typed-float | typed-list |
  |---|---:|---|---:|---:|---:|---:|
  | LLVM | 1 | false | 0.203 | 0.060 | 0.344 | 0.401 |
  | self | 1 | false | 0.204 | 0.061 | 0.323 | 0.426 |

  Interpretation:

  - typed-list improved again versus the previous 0.417/0.446 sample, but
    remains above 0.333 on both backends;
  - typed-float has useful sub-target samples, but the latest LLVM sample was
    0.344, so repeated stability is still not proved;
  - next list work likely needs to remove the per-element helper call entirely
    with a carefully guarded direct item-load path, or introduce real typed
    list storage. A naive unchecked direct load would weaken mutation/bounds
    behavior and should not be used as evidence.

Rejected 2026-05-25 guarded direct item-load probe:

- Tried replacing the hot `list[int]` helper call with generated IR that:
  - loaded the current list length from the object header;
  - used `pcc_gc_load_ptr()` on the element slot;
  - decoded tagged ints directly;
  - fell back to `py_list_get_i64_nonnegative` for non-tagged elements.
- Host-side focused tests initially passed after fixing an opaque-pointer
  bitcast bug, but the stage1 pcc1 benchmark path failed before runtime
  measurement:

  ```text
  error: PCC-PY-COMPILE-001: [python-frontend] name
    note: exception_type=Exception
  ```

- The probe was removed. Current code keeps the safer
  `py_list_get_i64_nonnegative` helper path plus the helper-internal int
  conversion inlining above.
- Validation after removal:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 120 \
    uv run pytest tests/python/test_py_typed_int_unboxed.py -q -n0
  ```

  Result: 12 tests passed, including a heap-int list fallback case.

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 120 \
    uv run pytest tests/python/test_self_backend_bootstrap_gate.py -q -n0
  ```

  Result: 18 tests passed.

- Stage1 benchmark after removal:

  | backend | stage | libpython | geomean pcc/python | typed-int | typed-float | typed-list |
  |---|---:|---|---:|---:|---:|---:|
  | LLVM | 1 | false | 0.187 | 0.056 | 0.290 | 0.404 |
  | self | 1 | false | 0.203 | 0.058 | 0.344 | 0.421 |

  Interpretation:

  - helper-internal improvements remain active and typed-list remains around
    0.40-0.42 in this sample;
  - direct list item-load needs a pcc1-compatible design before it can be
    used as evidence.

Rejected 2026-05-25 direct-accumulator lowering probe:

- Tried a pcc1-compatible peephole for exact `list[int]` loops whose body is
  only `acc = acc + x` or `acc += x`.
- The probe kept the normal index loop, target binding, bounds checks, helper
  call, and safepoint, but used the freshly loaded native `x` to update the
  accumulator directly instead of lowering the body through the generic
  assignment path.
- Focused tests passed, but the stage1 benchmark did not improve typed-list:

  | backend | stage | libpython | geomean pcc/python | typed-int | typed-float | typed-list |
  |---|---:|---|---:|---:|---:|---:|
  | LLVM | 1 | false | 0.186 | 0.054 | 0.287 | 0.416 |
  | self | 1 | false | 0.191 | 0.057 | 0.288 | 0.420 |

- The probe was removed. The current accepted typed-list path remains
  `py_list_get_i64_nonnegative` plus helper-internal int conversion inlining.
- Validation after removal:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 120 \
    uv run pytest tests/python/test_py_typed_int_unboxed.py -q -n0
  ```

  Result: 12 tests passed.

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 120 \
    uv run pytest tests/python/test_self_backend_bootstrap_gate.py -q -n0
  ```

  Result: 18 tests passed.

2026-05-25 user-runtime C baseline dashboard:

- Added host-C baselines for the three user-runtime benchmark cases in
  `scripts/run_self_backend_bootstrap_gate.py`:
  - `typed_int_loop`;
  - `typed_float_loop`;
  - `typed_list_int_loop`.
- The gate compiles each C baseline with `cc -O2 -std=c99` in the per-run
  temporary benchmark directory. C baseline failures are reported as `n/a`
  instead of failing the bootstrap gate, so the existing pcc/python threshold
  semantics are unchanged.
- Result output now includes:
  - `c_runtime_geomean`;
  - `user_runtime_vs_c_ratio`;
  - per-case `c=` and `pcc_vs_c=` values inside `user_runtime_cases`.
- Focused validation:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 120 \
    uv run pytest tests/python/test_self_backend_bootstrap_gate.py -q -n0
  ```

  Result: 18 tests passed.

- Stage1 benchmark sample:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 700 \
    uv run python scripts/run_self_backend_bootstrap_gate.py \
    --backend both \
    --stage 1 \
    --timeout 600 \
    --allow-non-supported-host \
    --max-stage-elapsed 0 \
    --max-pcc1-compile-ratio 0 \
    --max-user-runtime-vs-python-ratio 0
  ```

  Result:

  | backend | stage | libpython | geomean pcc/python | geomean pcc/C | typed-int pcc/python | typed-int pcc/C | typed-float pcc/python | typed-float pcc/C | typed-list pcc/python | typed-list pcc/C |
  |---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
  | LLVM | 1 | false | 0.188 | 2.652 | 0.052 | 1.983 | 0.313 | 1.791 | 0.405 | 5.251 |
  | self | 1 | false | 0.198 | 5.834 | 0.057 | 2.389 | 0.323 | 3.933 | 0.420 | 21.131 |

  Interpretation:

  - the dashboard now has CPython, pcc LLVM, pcc self, and host-C evidence for
    the same three hot-loop cases;
  - typed-list is still far from the C baseline, especially under self
    backend, which supports keeping `P-P1-PERF` as `DONE_WEAK`;
  - this closes only the dashboard C-baseline gap for the three current cases,
    not broad C-baseline comparison across performance claims.

2026-05-25 import-runtime dashboard slice:

- Added `IMPORT_RUNTIME_BENCHMARKS` to
  `scripts/run_self_backend_bootstrap_gate.py`.
- The initial cases compile and run:

  ```python
  import math
  print(int(math.sqrt(81.0)))

  import sys
  print(sys.platform == sys.platform)

  from os import path
  print(path.join("a", "b"))
  print(path.basename("/tmp/foo.txt"))
  ```

- Result output now includes:
  - `import_runtime`;
  - `import_runtime_geomean`;
  - `python_import_runtime_geomean`;
  - `import_runtime_vs_python_ratio`;
  - per-case `import_runtime_cases`.
- Focused validation:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 120 \
    uv run pytest tests/python/test_self_backend_bootstrap_gate.py -q -n0
  ```

  Result: 19 tests passed.

- Stage1 benchmark sample:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 700 \
    uv run python scripts/run_self_backend_bootstrap_gate.py \
    --backend both \
    --stage 1 \
    --timeout 600 \
    --allow-non-supported-host \
    --max-stage-elapsed 0 \
    --max-pcc1-compile-ratio 0 \
    --max-user-runtime-vs-python-ratio 0
  ```

  Result:

  | backend | stage | libpython | geomean pcc/python | `import_math_sqrt` | `import_sys_platform` | `from_os_import_path` |
  |---|---:|---|---:|---:|---:|---:|
  | LLVM | 1 | false | 0.227 | 0.099 | 0.353 | 0.332 |
  | self | 1 | false | 0.279 | 0.222 | 0.336 | 0.293 |

  Interpretation:

  - startup/import latency now has pcc LLVM + pcc self + CPython dashboard
    slices for native `math`, `sys`, and `os.path` import shapes;
  - this does not prove broad import latency across native stdlib modules,
    dynamic import, package imports, or extension imports.

2026-05-25 import-runtime JSON roundtrip slice:

- Added `import_json_roundtrip` to `IMPORT_RUNTIME_BENCHMARKS`.
- The case compiles and runs:

  ```python
  import json
  d = json.loads('{"a": 1, "b": 2}')
  print(d["a"], d["b"])
  print(json.dumps({"x": 10}))
  ```

- Focused validation:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 120 \
    uv run pytest tests/python/test_self_backend_bootstrap_gate.py -q -n0
  ```

  Result: 20 tests passed.

- Stage1 benchmark sample:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 700 \
    uv run python scripts/run_self_backend_bootstrap_gate.py \
    --backend both \
    --stage 1 \
    --timeout 600 \
    --allow-non-supported-host \
    --max-stage-elapsed 0 \
    --max-pcc1-compile-ratio 0 \
    --max-user-runtime-vs-python-ratio 0
  ```

  Result:

  | backend | stage | libpython | import geomean pcc/python | `import_math_sqrt` | `import_sys_platform` | `from_os_import_path` | `import_json_roundtrip` |
  |---|---:|---|---:|---:|---:|---:|---:|
  | LLVM | 1 | false | 0.257 | 0.088 | 0.314 | 0.472 | 0.338 |
  | self | 1 | false | 0.190 | 0.090 | 0.249 | 0.242 | 0.244 |

  Interpretation:

  - import latency coverage now includes a `json` object-workload case in
    addition to native-light imports;
  - this still does not cover dynamic import, package import, extension import,
    or large recursive stdlib closure latency.

2026-05-25 rejected dynamic/wider-stdlib import-performance probes:

- Attempted to extend the import-runtime dashboard beyond static native-light
  imports and `json` with no-libpython self-backend probes under `/tmp`.
- Rejected `importlib.import_module("math")` as a benchmark case: the compiled
  program runs far enough to call the returned object, but `m.sqrt` raises
  `AttributeError: sqrt`; that is a dynamic-import/module-surface semantic gap,
  not a useful performance sample.
- Rejected broader static stdlib workload probes for the current performance
  dashboard:
  - `collections.deque`, `functools.reduce`, `itertools.islice`, and
    `pathlib.PurePosixPath` require libpython fallback in no-libpython mode;
  - `re.match(...).group(0)` compiles/runs far enough to expose
    `AttributeError: group`.
- Interpretation:
  - do not add these cases to `IMPORT_RUNTIME_BENCHMARKS` until the
    corresponding semantic/package/no-libpython gaps have their own focused
    gates;
  - the valid import-runtime performance claim remains limited to the existing
    `math`, `sys`, `os.path`, and `json` roundtrip cases.

2026-05-25 user-runtime artifact-size dashboard:

- Added user-runtime artifact-size evidence to
  `scripts/run_self_backend_bootstrap_gate.py`.
- The gate now records:
  - `user_runtime_artifact_size_geomean`;
  - `c_runtime_artifact_size_geomean`;
  - `user_runtime_artifact_size_ratio`;
  - per-case `user_runtime_artifact_sizes`;
  - `user_runtime_text_size_geomean`;
  - `c_runtime_text_size_geomean`;
  - `user_runtime_text_size_ratio`;
  - per-case `user_runtime_text_sizes`;
  - per-case `user_runtime_text_top_symbols`.
- `user_runtime_text_*` is parsed from the host `size` tool. On macOS this is
  the `__TEXT` segment column; on GNU-style `size` output it is the `text`
  column.
- `user_runtime_text_top_symbols` is parsed from `nm`. On Mach-O, symbol sizes
  are inferred from adjacent `(__TEXT,__text)` symbol addresses because
  `nm -S` reports zero sizes for Mach-O. The parser excludes
  `__mh_execute_header`, which is a Mach-O anchor rather than a useful function
  attribution.
- `user_runtime_text_top_symbol_sources` is parsed from `nm -A` on the current
  runtime archive. It maps top final-binary symbols back to the runtime archive
  member and source file.
- Focused validation:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 120 \
    uv run pytest tests/python/test_self_backend_bootstrap_gate.py -q -n0
  ```

  Result: 23 tests passed.

- Stage1 benchmark sample:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 700 \
    uv run python scripts/run_self_backend_bootstrap_gate.py \
    --backend both \
    --stage 1 \
    --timeout 600 \
    --allow-non-supported-host \
    --max-stage-elapsed 0 \
    --max-pcc1-compile-ratio 0 \
    --max-user-runtime-vs-python-ratio 0
  ```

  Result:

  | backend | stage | libpython | pcc artifact geomean | C artifact geomean | pcc/C size | pcc text geomean | C text geomean | pcc/C text | typed-int file pcc/C | typed-float file pcc/C | typed-list file pcc/C | typed-int text pcc/C | typed-float text pcc/C | typed-list text pcc/C |
  |---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
  | LLVM | 1 | false | 490528 | 33448 | 14.665 | 311296 | 16384 | 19.000 | 14.664 | 14.666 | 14.666 | 19.000 | 19.000 | 19.000 |
  | self | 1 | false | 507296 | 33448 | 15.167 | 311296 | 16384 | 19.000 | 15.165 | 15.167 | 15.168 | 19.000 | 19.000 | 19.000 |

  Interpretation:

  - binary-size evidence now exists for the same three user-runtime dashboard
    cases, not only for the stage compiler binaries;
  - the clean-rebuild file-size gap is about 15x, while the text/`__TEXT`
    segment gap is 19x for all three dashboard cases in this sample;
  - this means the binary-size problem is not only non-text metadata/padding:
    the generated/runtime text segment itself is much larger than the host-C
    baseline;
  - this is measurement and attribution evidence, not binary-size completion.

2026-05-25 user-runtime text-symbol/source attribution dashboard:

- Added top `__TEXT,__text` symbol attribution for each user-runtime dashboard
  artifact.
- Added archive-member/source attribution for those top symbols.
- Focused validation:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 120 \
    uv run pytest tests/python/test_self_backend_bootstrap_gate.py -q -n0
  ```

  Result: 23 tests passed.

- Stage1 benchmark sample:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 700 \
    uv run python scripts/run_self_backend_bootstrap_gate.py \
    --backend both \
    --stage 1 \
    --timeout 600 \
    --allow-non-supported-host \
    --max-stage-elapsed 0 \
    --max-pcc1-compile-ratio 0 \
    --max-user-runtime-vs-python-ratio 0
  ```

  Result:

  | backend | stage | libpython | text pcc/C | top symbols and source attribution for all three user-runtime cases |
  |---|---:|---|---:|---|
  | LLVM | 1 | false | 19.000 | `_user_py_gc_backend__relocate_copy_payload` 5452 -> `py_gc_backend.o` (`pcc/runtime/py/py_gc_backend.py`); `_pcc_gc_telemetry` 3184 -> `py_gc_backend.o` (`pcc/runtime/py/py_gc_backend.py`); `_py_str_mod` 2900 -> `py_format.o` (`pcc/runtime/src/py_format.c`); `_pcc_capi_format_message` 2820 -> `py_capi_shim.o` (`pcc/runtime/src/py_capi_shim.c`); `_py_obj_format` 2028 -> `py_format.o` (`pcc/runtime/src/py_format.c`) |
  | self | 1 | false | 19.000 | same as LLVM |

  Interpretation:

  - the largest attributed text symbols are identical across LLVM/self and
    across `typed_int_loop`, `typed_float_loop`, and `typed_list_int_loop`;
  - this points at a fixed runtime closure cost, especially GC backend
    relocation/telemetry, string formatting/modulo, C-API formatting, and
    object formatting;
  - the fixed closure now maps to three concrete archive members:
    `py_gc_backend.o`, `py_format.o`, and `py_capi_shim.o`;
  - the next useful size slice should reduce or split this fixed closure, not
    tune per-benchmark generated code first.

2026-05-25 conditional C-API shim export closure slice:

- Changed Python frontend link behavior so ordinary executables link the runtime
  archive without forcing the pcc-native extension C-API shim into every
  artifact.
- The previous unconditional path always added export flags and forced
  `PyArg_ParseTuple`, pulling `py_capi_shim.o` and its
  `_pcc_capi_format_message` text into typed-loop programs that do not import a
  pcc-native extension.
- The new behavior keeps that forced C-API shim/export path only when AST import
  scanning detects a pcc-native extension import. Native extension imports still
  need the exported shim because the extension resolves symbols only after
  `dlopen`.
- Focused validation:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 120 \
    uv run pytest \
    tests/python/test_py_multi_file_bootstrap_shim.py::MultiFileBootstrapShimTests::test_runtime_archive_link_args_only_force_capi_for_native_extensions \
    tests/python/test_py_multi_file_bootstrap_shim.py::MultiFileBootstrapShimTests::test_compile_python_backend_llvm_uses_legacy_clang_link \
    tests/python/test_py_multi_file_bootstrap_shim.py::MultiFileBootstrapShimTests::test_self_native_link_reaches_self_emitter_and_host_triple \
    -q -n0
  ```

  Result: 3 tests passed.

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 240 \
    uv run pytest \
    tests/python/test_pcc_native_extension_loader.py::test_pcc_native_extension_import_runs_under_self_backend_no_libpython \
    -q -n0
  ```

  Result: 1 test passed.

- Stage1 benchmark sample:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 700 \
    uv run python scripts/run_self_backend_bootstrap_gate.py \
    --backend both \
    --stage 1 \
    --timeout 600 \
    --allow-non-supported-host \
    --max-stage-elapsed 0 \
    --max-pcc1-compile-ratio 0 \
    --max-user-runtime-vs-python-ratio 0
  ```

  Result:

  | backend | stage | libpython | artifact pcc/C | text pcc/C | top symbols after C-API shim split |
  |---|---:|---|---:|---:|---|
  | LLVM | 1 | false | 12.197 | 15.326 | `py_capi_shim.o` no longer appears; top symbols map to `py_gc_backend.o`, `py_format.o`, and `py_class.o` |
  | self | 1 | false | 5.055 | 5.000 | `py_capi_shim.o` no longer appears; top symbols map mostly to `py_gc_backend.o`, plus `py_exc_traceback.o` or generated `_main` |

  Interpretation:

  - this is the first accepted size-reduction slice in the dashboard;
  - ordinary user-runtime artifacts no longer pay the fixed pcc-native extension
    C-API shim closure;
  - pcc-native extension import behavior remains covered by a no-libpython
    self-backend runtime test;
 - broad binary-size completion is still open because LLVM-backed user
   artifacts remain about 12.2x file / 15.3x text versus C, and self-backed
   artifacts remain about 5x file/text versus C.

2026-05-25 modulo-dispatch archive split size slice:

- Split `py_obj_mod` out of `py_obj_ops_dispatch.py` into a new
  `py_obj_ops_mod.py` runtime archive member.
- Why: `py_obj_ops_dispatch.o` is pulled by ordinary generic dispatch paths
  such as truthy/add/getitem. Keeping `%` dispatch in the same object forced
  `_py_str_mod` and the string-formatting closure into typed-loop executables
  that do not use modulo or string formatting.
- The new archive granularity keeps modulo semantics available, but only
  `py_obj_ops_mod.o` carries the `_py_str_mod` / `_py_int_mod` undefined
  dependencies.
- Focused validation:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 360 \
    uv run pytest \
    tests/python/test_py_multi_file_bootstrap_shim.py::MultiFileBootstrapShimTests::test_py_obj_ops_mod_runtime_file_compiles_without_libpython_fallback \
    tests/python/test_py_multi_file_bootstrap_shim.py::MultiFileBootstrapShimTests::test_py_obj_ops_dispatch_no_longer_forces_str_mod_closure \
    tests/python/test_py_multi_file_bootstrap_shim.py::MultiFileBootstrapShimTests::test_py_obj_mod_split_preserves_int_and_string_modulo_runtime \
    -q -n0
  ```

  Result: 3 tests passed.

  Archive-level evidence after rebuilding `libpy_runtime_pcc_py.a`:

  - `py_obj_ops_dispatch.o` has no `_py_str_mod` or `_py_int_mod` undefined
    symbols;
  - `py_obj_ops_mod.o` has `_py_str_mod` / `_py_int_mod` undefined symbols and
    exports `_py_obj_mod`.

- Stage1 benchmark sample:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 700 \
    uv run python scripts/run_self_backend_bootstrap_gate.py \
    --backend both \
    --stage 1 \
    --timeout 600 \
    --allow-non-supported-host \
    --max-stage-elapsed 0 \
    --max-pcc1-compile-ratio 0 \
    --max-user-runtime-vs-python-ratio 0
  ```

  Result:

  | backend | stage | libpython | artifact pcc/C | text pcc/C | top symbols after modulo split |
  |---|---:|---|---:|---:|---|
  | LLVM | 1 | false | 11.025 | 13.000 | `py_format.o` no longer appears; top symbols map to `py_gc_backend.o`, `py_obj_ops_compare.o`, `py_list.o`, and `py_class.o` |
  | self | 1 | false | 4.559 | 4.000 | `py_format.o` no longer appears; top symbols map mostly to `py_gc_backend.o`, plus `py_exc_traceback.o` or generated `_main` |

  Interpretation:

  - this is the second accepted archive-granularity size-reduction slice;
  - it preserves `%` behavior through a no-libpython runtime test covering int
    modulo and string `%` formatting;
  - ordinary user-runtime artifacts no longer pay the fixed string-formatting
    closure when they only need generic non-modulo dispatch;
  - broad binary-size completion is still open because LLVM-backed user
    artifacts remain about 11.0x file / 13.0x text versus C, and self-backed
    artifacts remain about 4.6x file / 4.0x text versus C.

2026-05-25 list set-slice archive split size slice:

- Split `py_list_set_slice` out of `py_list.py` into
  `py_list_set_slice.py`, and split `py_obj_set_slice` out of
  `py_obj_ops_dispatch.py` into `py_obj_ops_set_slice.py`.
- Why: ordinary list users pull `py_list.o` for `py_list_new`,
  `py_list_get`, or `py_list_append`; ordinary generic dispatch users pull
  `py_obj_ops_dispatch.o` for truthy/add/getitem/setitem. Keeping set-slice in
  those common objects made typed-loop executables carry the set-slice closure
  even when no slice assignment is used.
- Focused validation:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 360 \
    uv run pytest \
    tests/python/test_py_multi_file_bootstrap_shim.py::MultiFileBootstrapShimTests::test_list_set_slice_split_runtime_files_compile_without_libpython_fallback \
    tests/python/test_py_multi_file_bootstrap_shim.py::MultiFileBootstrapShimTests::test_common_list_and_dispatch_members_no_longer_force_set_slice_closure \
    tests/python/test_py_multi_file_bootstrap_shim.py::MultiFileBootstrapShimTests::test_list_set_slice_split_preserves_slice_assignment_runtime \
    -q -n0
  ```

  Result: 3 tests passed.

  Archive-level evidence:

  - `py_list.o` and `py_obj_ops_dispatch.o` no longer carry
    `_py_list_set_slice` / `_py_obj_set_slice`;
  - `py_list_set_slice.o` and `py_obj_ops_set_slice.o` carry those
    set-slice-only symbols.

- Stage1 benchmark sample:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 700 \
    uv run python scripts/run_self_backend_bootstrap_gate.py \
    --backend both \
    --stage 1 \
    --timeout 600 \
    --allow-non-supported-host \
    --max-stage-elapsed 0 \
    --max-pcc1-compile-ratio 0 \
    --max-user-runtime-vs-python-ratio 0
  ```

  Result:

  | backend | stage | libpython | artifact pcc/C | text pcc/C | top symbols after set-slice split |
  |---|---:|---|---:|---:|---|
  | LLVM | 1 | false | 11.022 | 13.000 | `py_list.o` no longer appears; top symbols map to `py_gc_backend.o`, `py_obj_ops_compare.o`, `py_class.o`, and `py_str_accessors.o` |
  | self | 1 | false | 4.559 | 4.000 | unchanged; top symbols remain mostly `py_gc_backend.o`, plus `py_exc_traceback.o` or generated `_main` |

  Interpretation:

  - this is a small accepted archive-granularity cleanup: it removes a
    non-used set-slice closure from ordinary typed-loop source attribution and
    gives a tiny LLVM artifact-size improvement;
  - it does not reduce the rounded text/`__TEXT` segment ratio, and self size
    stays flat;
  - broad binary-size completion remains open.

2026-05-25 generic/string slice archive split size slice:

- Split `py_obj_slice` out of `py_obj_ops_dispatch.py` into
  `py_obj_ops_slice.py`, and split `py_str_slice` out of
  `py_str_accessors.py` into `py_str_slice.py`.
- Why: ordinary generic getitem/len/truthy dispatch pulls
  `py_obj_ops_dispatch.o`, and ordinary string indexing pulls
  `py_str_accessors.o`. Keeping generic and string slicing in those common
  objects made typed-loop executables carry slice helpers even when they do not
  use slicing.
- Focused validation:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 360 \
    uv run pytest \
    tests/python/test_py_multi_file_bootstrap_shim.py::MultiFileBootstrapShimTests::test_slice_split_runtime_files_compile_without_libpython_fallback \
    tests/python/test_py_multi_file_bootstrap_shim.py::MultiFileBootstrapShimTests::test_common_string_and_dispatch_members_no_longer_force_slice_closure \
    tests/python/test_py_multi_file_bootstrap_shim.py::MultiFileBootstrapShimTests::test_slice_split_preserves_string_list_and_tuple_slice_runtime \
    -q -n0
  ```

  Result: 3 tests passed.

  Archive-level evidence:

  - `py_obj_ops_dispatch.o` and `py_str_accessors.o` no longer carry
    `_py_obj_slice` / `_py_str_slice`;
  - `py_obj_ops_slice.o` and `py_str_slice.o` carry those slice-only symbols.

- Stage1 benchmark sample:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 700 \
    uv run python scripts/run_self_backend_bootstrap_gate.py \
    --backend both \
    --stage 1 \
    --timeout 600 \
    --allow-non-supported-host \
    --max-stage-elapsed 0 \
    --max-pcc1-compile-ratio 0 \
    --max-user-runtime-vs-python-ratio 0
  ```

  Result:

  | backend | stage | libpython | artifact pcc/C | text pcc/C | top symbols after slice split |
  |---|---:|---|---:|---:|---|
  | LLVM | 1 | false | 11.020 | 13.000 | `py_str_accessors.o` no longer appears; top symbols map to `py_gc_backend.o`, `py_obj_ops_compare.o`, and `py_class.o` |
  | self | 1 | false | 4.559 | 4.000 | unchanged; top symbols remain mostly `py_gc_backend.o`, plus `py_exc_traceback.o` or generated `_main` |

  Interpretation:

  - this is another small accepted archive-granularity cleanup;
  - it removes a non-used generic/string slice closure from ordinary typed-loop
    source attribution and gives a tiny LLVM artifact-size improvement;
  - it does not reduce the rounded text/`__TEXT` segment ratio, and self size
    stays flat;
  - broad binary-size completion remains open.

2026-05-25 read-only GC telemetry archive split size slice:

- Split read-only GC telemetry dispatch out of `py_gc_backend.py` into
  `py_gc_telemetry.py`.
- Kept `pcc_gc_telemetry_reset` in `py_gc_backend.py` because reset mutates
  backend4 epoch state, reseeds relocation candidates, and clears deferred
  large-object flags. Moving reset would require a larger GC-design slice.
- Moved only:
  - `pcc_gc_telemetry`;
  - `pcc_gc_backend2_worker_buffer_score`;
  - `pcc_gc_backend2_production_score`;
  - `pcc_gc_backend3_minor_productivity_score`;
  - `pcc_gc_backend3_remembered_update_score`.
- The new telemetry archive member calls exported GC metric helpers via
  `extern(...)`; it does not duplicate backend4 relocation, remembered-set,
  zpage, or reset logic.
- Focused tests:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 420 \
    uv run pytest \
    tests/python/test_py_multi_file_bootstrap_shim.py::MultiFileBootstrapShimTests::test_py_gc_telemetry_runtime_file_compiles_without_libpython_fallback \
    tests/python/test_py_multi_file_bootstrap_shim.py::MultiFileBootstrapShimTests::test_py_gc_telemetry_split_preserves_runtime_api \
    tests/python/test_py_multi_file_bootstrap_shim.py::MultiFileBootstrapShimTests::test_py_gc_backend_no_longer_exports_read_telemetry_dispatch \
    -q -n0
  ```

  Result: 3 tests passed.

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 240 \
    uv run pytest \
    tests/python/test_py_multi_file_bootstrap_shim.py::MultiFileBootstrapShimTests::test_py_gc_backend_runtime_file_compiles_without_libpython_fallback \
    -q -n0
  ```

  Result: 1 test passed.

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 300 \
    uv run pytest tests/python/test_gc_backend23_production.py -q -n0
  ```

  Result: 3 tests passed.

- Archive evidence:
  - `py_gc_backend.o` no longer exports or references `_pcc_gc_telemetry`;
  - `py_gc_backend.o` still exports `_pcc_gc_telemetry_reset`;
  - `py_gc_telemetry.o` exports `_pcc_gc_telemetry` and the backend2/3 score
    wrappers;
  - `py_gc_telemetry.o` has undefined references to exported backend metric
    helpers, as intended, so it is pulled only when telemetry is requested.
- Stage1 dashboard after the split:

  | backend | artifact pcc/C | text pcc/C | attribution |
  |---|---:|---:|---|
  | LLVM | 11.008 | 13.000 | `_pcc_gc_telemetry` no longer appears; top symbols are still `py_gc_backend.o`, `py_obj_ops_compare.o`, `py_class.o`, and now `py_tuple.o::_py_tuple_slice` |
  | self | 4.559 | 4.000 | unchanged by rounded ratio; top symbols remain mostly `py_gc_backend.o`, plus `py_exc_traceback.o` or generated `_main` |

  Interpretation:

  - accepted as a small archive-granularity cleanup;
  - it removes the read telemetry dispatch body from ordinary user-runtime
    cases and improves LLVM artifact-size ratio from `11.020` to `11.008`;
  - it does not improve the rounded text/`__TEXT` page ratio or self size;
  - `py_gc_backend.o` remains the dominant fixed runtime closure, so broader
    binary-size completion remains open.

2026-05-25 tuple-slice archive split size slice:

- Split `py_tuple_slice` out of `py_tuple.py` into `py_tuple_slice.py`.
- `py_tuple.py` keeps ordinary tuple construction, set/get, len, concat, and
  repeat. `py_tuple_slice.py` duplicates only the minimal sanity/debug and
  slice-bound logic needed for slicing, and calls exported `py_tuple_new` /
  `py_tuple_set_item` for result construction.
- Focused tests:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 420 \
    uv run pytest \
    tests/python/test_py_multi_file_bootstrap_shim.py::MultiFileBootstrapShimTests::test_slice_split_runtime_files_compile_without_libpython_fallback \
    tests/python/test_py_multi_file_bootstrap_shim.py::MultiFileBootstrapShimTests::test_common_tuple_member_no_longer_forces_tuple_slice_closure \
    tests/python/test_py_multi_file_bootstrap_shim.py::MultiFileBootstrapShimTests::test_slice_split_preserves_string_list_and_tuple_slice_runtime \
    -q -n0
  ```

  Result: 3 tests passed.

- Archive evidence:
  - `py_tuple.o` no longer exports `_py_tuple_slice` and no longer references
    `py_obj_index_i64`;
  - `py_tuple_slice.o` exports `_py_tuple_slice`;
  - `py_tuple_slice.o` has undefined references to `py_tuple_new`,
    `py_tuple_set_item`, `py_obj_index_i64`, and `pcc_gc_load_ptr`, so the
    slice-specific closure is pulled only when tuple slicing is requested.
- Stage1 dashboard after the split:

  | backend | artifact pcc/C | text pcc/C | attribution |
  |---|---:|---:|---|
  | LLVM | 11.002 | 13.000 | `_py_tuple_slice` no longer appears; top symbols now return to `py_gc_backend.o`, `py_obj_ops_compare.o`, and `py_class.o` |
  | self | 4.559 | 4.000 | unchanged by rounded ratio; top symbols remain mostly `py_gc_backend.o`, plus `py_exc_traceback.o` or generated `_main` |

  Interpretation:

  - accepted as another small archive-granularity cleanup;
  - it improves LLVM artifact-size ratio from `11.008` to `11.002`;
  - it does not improve the rounded text/`__TEXT` page ratio or self size;
  - broad binary-size completion remains open.

2026-05-25 rejected equality-dispatch archive split probe:

- Tried splitting `py_obj_eq` out of `py_obj_ops_compare.py` into its own
  `py_obj_ops_eq.py` archive member.
- The probe was semantically viable in focused tests:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 360 \
    uv run pytest \
    tests/python/test_py_multi_file_bootstrap_shim.py::MultiFileBootstrapShimTests::test_py_obj_ops_eq_runtime_file_compiles_without_libpython_fallback \
    tests/python/test_py_multi_file_bootstrap_shim.py::MultiFileBootstrapShimTests::test_py_obj_ops_compare_no_longer_exports_eq_dispatch \
    tests/python/test_py_multi_file_bootstrap_shim.py::MultiFileBootstrapShimTests::test_py_obj_eq_split_preserves_scalar_and_container_equality_runtime \
    -q -n0
  ```

  Result: 3 tests passed.

- Stage1 dashboard result after the probe:

  | backend | artifact pcc/C | text pcc/C | interpretation |
  |---|---:|---:|---|
  | LLVM | 11.064 | 13.000 | file size slightly worsened versus the accepted 11.025/13.000 modulo-split baseline |
  | self | 4.559 | 4.000 | unchanged versus the accepted modulo-split baseline |

- The only useful effect was attribution: LLVM top `_py_obj_eq` moved from
  `py_obj_ops_compare.o` to `py_obj_ops_eq.o`. Because file/text size did not
  improve, this is not an accepted optimization slice.
- Follow-up cleanup:
  - removed `py_obj_ops_eq.py`;
  - restored `py_obj_eq` inside `py_obj_ops_compare.py`;
  - removed the probe tests;
  - rebuilt `libpy_runtime_pcc_py.a` and confirmed the archive contains
    `py_obj_ops_mod.o` and `py_obj_ops_compare.o`, but no stale
    `py_obj_ops_eq.o`;
  - reran the accepted modulo-dispatch focused tests, which still passed.

2026-05-25 rejected class/attribute dispatch archive split probe:

- Tried splitting `py_type_builtin`, `py_obj_getattr`, `py_obj_setattr`,
  `py_obj_delattr`, `py_obj_call`, `py_obj_call_method1`, and
  `py_obj_isinstance` out of `py_obj_ops_dispatch.py` into a separate
  `py_obj_ops_attr.py` archive member.
- The probe achieved the local archive boundary it was designed to test:
  `py_obj_ops_dispatch.o` no longer had `py_class*`, `py_instance*`, or
  `py_isinstance` undefineds, while `py_obj_ops_attr.o` carried those
  class-heavy dependencies and exported the moved ABI entrypoints.
- Focused semantic/wiring tests passed during the probe:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 420 \
    uv run pytest \
    tests/python/test_py_multi_file_bootstrap_shim.py::MultiFileBootstrapShimTests::test_py_obj_ops_attr_runtime_file_compiles_without_libpython_fallback \
    tests/python/test_py_multi_file_bootstrap_shim.py::MultiFileBootstrapShimTests::test_py_obj_ops_dispatch_no_longer_forces_class_attr_call_closure \
    tests/python/test_py_multi_file_bootstrap_shim.py::MultiFileBootstrapShimTests::test_obj_ops_attr_split_preserves_type_isinstance_and_method_runtime \
    -q -n0
  ```

  Result: 3 tests passed.

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 180 \
    uv run pytest \
    tests/python/test_runtime_type_builtin_native.py::test_py_type_builtin_wired_in_c_and_pcc_py_sources \
    tests/python/data_model/test_b1_b6_runtime_wiring_regression.py::test_b1_b6_runtime_symbols_stay_wired \
    -q -n0
  ```

  Result: 2 tests passed.

- Stage1 dashboard result after the probe:

  | backend | artifact pcc/C | text pcc/C | interpretation |
  |---|---:|---:|---|
  | LLVM | 11.053 | 13.000 | worsened versus the accepted 11.020/13.000 generic/string-slice baseline |
  | self | 4.559 | 4.000 | unchanged by ratio, with no meaningful size win |

- The top LLVM attribution still included `_py_class_new=>py_class.o`, so the
  probe did not remove the remaining class closure from ordinary user-runtime
  cases. It only moved some references out of the common dispatch object and
  added enough separate-object overhead to worsen LLVM artifact size.
- Follow-up cleanup:
  - removed `py_obj_ops_attr.py`;
  - restored the moved exports inside `py_obj_ops_dispatch.py`;
  - removed the probe tests and restored the source-wiring assertions;
  - reran focused touched-source tests:

    ```bash
    env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 180 \
      uv run pytest \
      tests/python/test_runtime_type_builtin_native.py::test_py_type_builtin_wired_in_c_and_pcc_py_sources \
      tests/python/data_model/test_b1_b6_runtime_wiring_regression.py::test_b1_b6_runtime_symbols_stay_wired \
      tests/python/test_py_multi_file_bootstrap_shim.py::MultiFileBootstrapShimTests::test_py_obj_ops_dispatch_no_longer_forces_str_mod_closure \
      -q -n0
    ```

    Result: 3 tests passed.

    ```bash
    env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 240 \
      uv run pytest \
      tests/python/test_py_multi_file_bootstrap_shim.py::MultiFileBootstrapShimTests::test_py_obj_mod_split_preserves_int_and_string_modulo_runtime \
      tests/python/test_py_multi_file_bootstrap_shim.py::MultiFileBootstrapShimTests::test_slice_split_preserves_string_list_and_tuple_slice_runtime \
      -q -n0
    ```

    Result: 2 tests passed.

  - reran `scripts/run_self_backend_bootstrap_gate.py --backend both --stage 1`
    and confirmed the clean state returned to the accepted baseline:
    LLVM artifact/text `11.020`/`13.000`, self artifact/text
    `4.559`/`4.000`.

2026-05-25 rejected combined lazy-exception / attr-dispatch /
class-dealloc archive split probe:

- Tried a broader version of the class-closure split:
  - moved `py_err_occurred`, `py_current_exception`, `py_clear_exception`,
    and a new ownership-aware `py_raise_exc` into `py_exc_state.py`;
  - made builtin exception object creation lazy enough that
    `py_exc_objects.o` no longer directly forced `py_exc_builtin_class`;
  - split attr/type/call dispatch into `py_obj_ops_attr.py`;
  - then split `py_class_dealloc` / `py_instance_dealloc` into
    `py_class_dealloc.py`.
- The first combined slice passed focused compile/semantic/wiring checks, and
  archive `nm` evidence showed the intended local boundary:
  `py_obj_ops_dispatch.o` no longer carried the inspected class-heavy
  undefineds, `py_obj_ops_attr.o` carried those dependencies, and
  `py_exc_state.o` exported the moved TLS/raise entries.
- Stage1 dashboard rejected the slice:

  | probe | backend | artifact pcc/C | text pcc/C | interpretation |
  |---|---|---:|---:|---|
  | lazy exception + attr/type/call split | LLVM | 11.069 | 13.000 | worsened versus accepted 11.002/13.000 tuple-slice baseline |
  | lazy exception + attr/type/call split | self | 4.591 | 4.000 | worsened versus accepted 4.559/4.000 baseline |
  | plus class-dealloc split | LLVM | 11.089 | 13.000 | worsened further |
  | plus class-dealloc split | self | 4.597 | 4.000 | worsened further |

- `_py_class_new=>py_class.o` remained in top LLVM attribution even after the
  class-dealloc follow-up, so the remaining class closure is multi-path; simple
  attr/type/call, lazy-exception, or dealloc archive splits are not enough.
- Follow-up cleanup:
  - removed `py_exc_state.py`, `py_obj_ops_attr.py`, and
    `py_class_dealloc.py`;
  - restored the moved ABI exports to their prior runtime modules;
  - restored source-wiring tests to reference the accepted split modules only;
  - rebuilt `libpy_runtime_pcc_py.a` without stale archive members;
  - reran 15 focused wiring/runtime/exception tests successfully;
  - reran `scripts/run_self_backend_bootstrap_gate.py --backend both --stage 1`:
    LLVM artifact/text `11.050`/`13.000`, self artifact/text `4.559`/`4.000`,
    both `libpython=False`. This validates the cleanup is below the rejected
    combined-split samples, but it is not evidence of a new size improvement.

2026-05-25 accepted typed-list fast-helper cleanup:

- Changed the pcc-Python `py_list_len`, `py_list_get_i64`, and
  `py_list_get_i64_nonnegative` helpers to match the C runtime fast path used
  by typed-list loops:
  - keep null checks, current-length bounds checks, int conversion, and
    `pcc_gc_load_ptr` barriers;
  - remove the extra `_list_is_sane` structural debug validation from these
    hot helpers.
- This preserves the existing generated IR shape (`for x in list[int]` still
  calls `py_list_get_i64_nonnegative`) but reduces runtime helper work.
- Focused gate:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 360 \
    uv run pytest \
    tests/python/test_py_typed_int_unboxed.py \
    tests/python/test_py_multi_file_bootstrap_shim.py::MultiFileBootstrapShimTests::test_compiled_pcc_multi_can_compile_toy_module \
    tests/python/test_gc_backend23_production.py \
    -q -n0
  ```

  Result: 17 tests passed.

- Current stage1 dashboard after rebuilding the archive back to the accepted
  direct-slot-free state:

  | backend | typed-list pcc/python | typed-list pcc/C | artifact pcc/C | text pcc/C |
  |---|---:|---:|---:|---:|
  | LLVM | 0.285 | 5.270 | 11.050 | 13.000 |
  | self | 0.302 | 8.294 | 4.559 | 4.000 |

- Interpretation:
  - accepted as a typed-list helper-call reduction;
  - it moves the latest pcc/python typed-list samples below the historical
    `0.333` target on both backends;
  - it does not solve the pcc/C gap or binary-size gap.

2026-05-25 rejected typed-list direct-slot fast path probe:

- Tried changing `py_list_get_i64` and `py_list_get_i64_nonnegative` to call
  `pcc_gc_backend()` and direct-load the list slot for non-relocating backends,
  while preserving `pcc_gc_load_ptr` for backend #3/#4.
- Focused correctness gate passed:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 420 \
    uv run pytest \
    tests/python/test_py_typed_int_unboxed.py \
    tests/python/test_py_multi_file_bootstrap_shim.py::MultiFileBootstrapShimTests::test_compiled_pcc_multi_can_compile_toy_module \
    tests/python/test_gc_backend23_production.py \
    tests/python/test_gc_backend4_production.py::test_backend4_list_get_loads_forwarded_item_slot \
    -q -n0
  ```

  Result: 18 tests passed.

- Stage1 dashboard after the probe:

  | backend | typed-list pcc/python | typed-list pcc/C | artifact pcc/C | text pcc/C |
  |---|---:|---:|---:|---:|
  | LLVM | 0.254 | 4.117 | 11.050 | 13.000 |
  | self | 0.289 | 12.148 | 4.559 | 4.000 |

- Interpretation:
  - rejected and removed;
  - it produced only a small LLVM pcc/python improvement and regressed self
    pcc/python versus the accepted `0.276` sample;
  - it did not improve artifact/text size.

2026-05-25 accepted typed-branch dashboard coverage:

- Added `typed_branch_loop` to the bootstrap gate user-runtime dashboard and
  to `USER_RUNTIME_C_BASELINES`.
- This broadens the host-C-comparable cases from three to four:
  `typed_int_loop`, `typed_float_loop`, `typed_list_int_loop`, and
  `typed_branch_loop`.
- Focused gate:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 120 \
    uv run pytest tests/python/test_self_backend_bootstrap_gate.py -q -n0
  ```

  Result: 25 tests passed.

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 180 \
    uv run pytest tests/python/test_py_typed_int_unboxed.py -q -n0
  ```

  Result: 14 tests passed, including IR-shape coverage that
  `typed_function_call_loop` lowers `bump`, `step`, and `bench` as `i64`
  direct calls without `py_obj_call`, boxed `py_int_*` helpers, or `py_cpy_*`.

- Stage1 dashboard after the coverage expansion:

  | backend | typed-branch pcc/python | typed-branch pcc/C | user-runtime pcc/python geomean | user-runtime pcc/C geomean | artifact pcc/C | text pcc/C |
  |---|---:|---:|---:|---:|---:|---:|
  | LLVM | 0.144 | 4.257 | 0.147 | 4.741 | 11.050 | 13.000 |
  | self | 0.094 | 5.584 | 0.136 | 3.480 | 4.555 | 4.000 |

- Interpretation:
  - accepted as dashboard/evidence coverage, not as an optimization;
  - the new branch/control-flow case has pcc/python ratios below the
    historical `0.333` target on both backends;
  - pcc/C ratios and artifact/text gaps remain open;
  - top-symbol attribution remains dominated by `py_gc_backend.o`,
    `py_obj_ops_compare.o`, `py_class.o`, and exception traceback/generated
    main paths.

2026-05-25 accepted typed function-call dashboard coverage:

- Added `typed_function_call_loop` to the bootstrap gate user-runtime
  dashboard and to `USER_RUNTIME_C_BASELINES`.
- This broadens the host-C-comparable cases from four to five:
  `typed_int_loop`, `typed_float_loop`, `typed_list_int_loop`,
  `typed_branch_loop`, and `typed_function_call_loop`.
- Focused gate:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 120 \
    uv run pytest tests/python/test_self_backend_bootstrap_gate.py -q -n0
  ```

  Result: 25 tests passed.

- Stage1 dashboard after the coverage expansion:

  | backend | typed-call pcc/python | typed-call pcc/C | user-runtime pcc/python geomean | user-runtime pcc/C geomean | artifact pcc/C | text pcc/C |
  |---|---:|---:|---:|---:|---:|---:|
  | LLVM | 0.074 | 2.264 | 0.136 | 3.708 | 11.050 | 13.000 |
  | self | 0.085 | 5.124 | 0.127 | 4.062 | 4.553 | 4.000 |

- Interpretation:
  - accepted as dashboard/evidence coverage, not as an optimization;
  - the new typed native function-call case has pcc/python ratios below the
    historical `0.333` target on both backends;
  - pcc/C ratios and artifact/text gaps remain open;
  - top-symbol attribution remains dominated by `py_gc_backend.o`,
    `py_obj_ops_compare.o`, `py_class.o`, and exception traceback/generated
    main paths.

2026-05-25 accepted AArch64 scalar phi direct-copy optimization:

- Changed `emit_phi_assignments()` on AArch64 Darwin to skip the temporary
  stack parallel-copy buffer for independent scalar phi assignments.
- The temp-stack path remains mandatory for aggregate phi assignments and true
  copy cycles, preserving parallel-copy semantics.
- Focused gate:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 180 \
    uv run pytest \
    tests/c/test_self_backend.py::test_self_backend_aarch64_flow_helpers_cover_bitcount_and_phi_assignment \
    tests/c/test_self_backend.py::test_self_backend_aarch64_phi_assignments_skip_temp_for_independent_scalar_sources \
    tests/c/test_self_backend.py::test_self_backend_aarch64_phi_assignments_preserve_parallel_copy_cycles \
    tests/c/test_self_backend.py::test_self_backend_aarch64_phi_assignments_support_large_aggregate_literals \
    tests/c/test_self_backend.py::test_self_backend_aarch64_phi_assignments_keep_temp_address_alive_for_large_slots \
    tests/c/test_self_backend.py::test_self_backend_aarch64_forwards_adjacent_fp_stack_store_load \
    -q -n0
  ```

  Result: 6 tests passed.

- Local asm probe for `typed_function_call_loop` confirmed that the loop
  backedge no longer emits the previous `sub sp -> str -> ldr -> add sp`
  temporary phi-copy block; it now directly loads the independent scalar
  incoming values and stores the phi destination slots.
- Stage1 dashboard after the optimization:

  | backend | pcc1 size | five-case pcc/python geomean | five-case pcc/C geomean | artifact pcc/C | text pcc/C |
  |---|---:|---:|---:|---:|---:|
  | LLVM | 7770712 | 0.113 | 2.437 | 11.050 | 13.000 |
  | self | 14087184 | 0.116 | 4.560 | 4.553 | 4.000 |

- Interpretation:
  - accepted as an emitted-asm cleanup and self pcc1 size improvement;
  - `size_ratio self/llvm` improved to `1.813`;
  - runtime microbench samples remain noisy, so this is not claimed as a
    stable runtime-speed completion.

2026-05-25 accepted AArch64 byte stack-reload peephole:

- Extended the adjacent stack store/load peephole to cover `sturb` followed by
  `ldurb` from the same frame slot.
- The replacement preserves the `ldurb` zero-extension behavior with
  `and wdst, wsrc, #0xff`, not a plain register move.
- This removes the hot conditional-branch shape `cset -> sturb stack -> ldurb
  -> cbz` from typed branch/control-flow loops.
- Focused gate:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 180 \
    uv run pytest \
    tests/c/test_self_backend.py::test_self_backend_aarch64_forwards_adjacent_byte_stack_store_load \
    tests/c/test_self_backend.py::test_self_backend_aarch64_forwards_adjacent_fp_stack_store_load \
    tests/c/test_self_backend.py::test_self_backend_aarch64_flow_helpers_cover_bitcount_and_phi_assignment \
    tests/c/test_self_backend.py::test_self_backend_aarch64_phi_assignments_skip_temp_for_independent_scalar_sources \
    tests/c/test_self_backend.py::test_self_backend_aarch64_phi_assignments_preserve_parallel_copy_cycles \
    tests/c/test_self_backend.py::test_self_backend_runs_unordered_fcmp_semantics_in_ir \
    -q -n0
  ```

  Result: 6 tests passed.

- Stage1 dashboard after the peephole:

  | backend | pcc1 size | five-case pcc/python geomean | five-case pcc/C geomean | artifact pcc/C | text pcc/C |
  |---|---:|---:|---:|---:|---:|
  | LLVM | 7770712 | 0.122 | 2.603 | 11.050 | 13.000 |
  | self | 14087184 | 0.120 | 4.088 | 4.553 | 4.000 |

- Interpretation:
  - accepted as emitted-asm cleanup on conditional hot paths;
  - self pcc1 size remained at the improved `14087184` bytes from the scalar
    phi slice, and `size_ratio self/llvm` remained `1.813`;
  - no additional binary-size win is claimed for this byte peephole.

2026-05-25 accepted AArch64 forwarded-cset branch fold:

- Added a conservative follow-up peephole for byte-forwarded conditional
  branches:
  `cset wsrc -> sturb wsrc -> and wtmp, wsrc, #0xff -> cbz/cbnz wtmp`
  becomes `cset wsrc -> sturb wsrc -> cbz/cbnz wsrc`.
- This fold is only applied when the byte source is immediately produced by
  `cset`, because `cset` writes a full-width `0` or `1`. Arbitrary
  `sturb`/`ldurb` forwarding still preserves zero-extension with `and`.
- Focused gate:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 180 \
    uv run pytest \
    tests/c/test_self_backend.py::test_self_backend_aarch64_forwards_adjacent_byte_stack_store_load \
    tests/c/test_self_backend.py::test_self_backend_aarch64_forwards_adjacent_fp_stack_store_load \
    tests/c/test_self_backend.py::test_self_backend_aarch64_flow_helpers_cover_bitcount_and_phi_assignment \
    tests/c/test_self_backend.py::test_self_backend_aarch64_phi_assignments_skip_temp_for_independent_scalar_sources \
    tests/c/test_self_backend.py::test_self_backend_aarch64_phi_assignments_preserve_parallel_copy_cycles \
    tests/c/test_self_backend.py::test_self_backend_runs_unordered_fcmp_semantics_in_ir \
    -q -n0
  ```

  Result: 6 tests passed.

- Stage1 dashboard after the peephole:

  | backend | pcc1 size | five-case pcc/python geomean | five-case pcc/C geomean | artifact pcc/C | text pcc/C |
  |---|---:|---:|---:|---:|---:|
  | LLVM | 7770712 | 0.137 | 4.688 | 11.050 | 13.000 |
  | self | 14070672 | 0.137 | 4.953 | 4.553 | 4.000 |

- Interpretation:
  - accepted as emitted-asm cleanup on conditional hot paths;
  - self pcc1 size improved again and `size_ratio self/llvm` improved to
    `1.811`;
  - runtime microbench samples remain noisy, so no stable runtime-speed win is
    claimed from this slice alone.

2026-05-25 accepted AArch64 cmp/cset direct branch fold:

- Added a narrower follow-up peephole for the focused branch shape:
  `cmp/fcmp -> cset -> sturb -> cbz/cbnz`.
- The emitted branch now uses `b.<cond>` or the inverse condition directly,
  while still retaining the `sturb` slot write for the SSA bool.
- This is only applied when the byte value was produced by adjacent `cset`;
  arbitrary byte zero-extension behavior is not changed.
- Focused gate:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 180 \
    uv run pytest \
    tests/c/test_self_backend.py::test_self_backend_aarch64_forwards_adjacent_byte_stack_store_load \
    tests/c/test_self_backend.py::test_self_backend_aarch64_forwards_adjacent_fp_stack_store_load \
    tests/c/test_self_backend.py::test_self_backend_aarch64_flow_helpers_cover_bitcount_and_phi_assignment \
    tests/c/test_self_backend.py::test_self_backend_aarch64_phi_assignments_skip_temp_for_independent_scalar_sources \
    tests/c/test_self_backend.py::test_self_backend_aarch64_phi_assignments_preserve_parallel_copy_cycles \
    tests/c/test_self_backend.py::test_self_backend_runs_unordered_fcmp_semantics_in_ir \
    -q -n0
  ```

  Result: 6 tests passed.

- Stage1 dashboard after the peephole:

  | backend | pcc1 size | five-case pcc/python geomean | five-case pcc/C geomean | artifact pcc/C | text pcc/C |
  |---|---:|---:|---:|---:|---:|
  | LLVM | 7770712 | 0.114 | 4.128 | 11.050 | 13.000 |
  | self | 14070672 | 0.136 | 4.789 | 4.553 | 4.000 |

- Interpretation:
  - accepted as emitted-asm cleanup on conditional hot paths;
  - self pcc1 size stayed at `14070672` bytes and `size_ratio self/llvm`
    stayed `1.811`;
  - no stable runtime-speed win is claimed from this slice alone.

2026-05-25 accepted AArch64 use-aware dead cset/store elimination:

- Added a function-local use-aware cleanup after direct branch folding.
- If a `cmp/fcmp -> cset -> sturb -> b.<cond>` bool slot has no `ldurb` use
  in the same function, the generated assembly now drops the dead `cset` and
  `sturb` and keeps the direct conditional branch.
- If the bool SSA is used later, for example by a successor `zext i1`, the
  store is retained.
- Focused gate:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 180 \
    uv run pytest \
    tests/c/test_self_backend.py::test_self_backend_aarch64_forwards_adjacent_byte_stack_store_load \
    tests/c/test_self_backend.py::test_self_backend_aarch64_keeps_cset_store_when_bool_slot_is_used \
    tests/c/test_self_backend.py::test_self_backend_aarch64_forwards_adjacent_fp_stack_store_load \
    tests/c/test_self_backend.py::test_self_backend_aarch64_flow_helpers_cover_bitcount_and_phi_assignment \
    tests/c/test_self_backend.py::test_self_backend_aarch64_phi_assignments_skip_temp_for_independent_scalar_sources \
    tests/c/test_self_backend.py::test_self_backend_aarch64_phi_assignments_preserve_parallel_copy_cycles \
    tests/c/test_self_backend.py::test_self_backend_runs_unordered_fcmp_semantics_in_ir \
    -q -n0
  ```

  Result: 7 tests passed.

- Stage1 dashboard after the cleanup:

  | backend | pcc1 size | five-case pcc/python geomean | five-case pcc/C geomean | artifact pcc/C | text pcc/C |
  |---|---:|---:|---:|---:|---:|
  | LLVM | 7770712 | 0.132 | 4.962 | 11.050 | 13.000 |
  | self | 14021120 | 0.128 | 4.699 | 4.553 | 4.000 |

- Interpretation:
  - accepted as emitted-code and self pcc1-size improvement;
  - self pcc1 size improved to `14021120` bytes and `size_ratio self/llvm`
    improved to `1.804`;
  - runtime microbench samples remain noisy, so no stable runtime-speed win is
    claimed from this slice alone.

2026-05-25 accepted AArch64 one-intervening stack load forwarding:

- Added a narrow stack forwarding peephole for:
  `stur src, slot; ldur other, other_slot; ldur dst, slot`.
- The slot store is retained, but the later load is replaced with a register
  move when the single intervening instruction is another frame load that does
  not clobber the stored register.
- This targets typed function-call hot paths such as
  `bl callee; stur x0, call_slot; ldur accumulator; ldur call_slot`, without
  crossing labels, branches, or calls.
- Focused gate:

  ```bash
  env -u LC_ALL -u LC_CTYPE /usr/bin/perl -e 'alarm shift; exec @ARGV' 180 \
    uv run pytest \
    tests/c/test_self_backend.py::test_self_backend_aarch64_forwards_one_intervening_stack_store_load \
    tests/c/test_self_backend.py::test_self_backend_aarch64_forwards_adjacent_byte_stack_store_load \
    tests/c/test_self_backend.py::test_self_backend_aarch64_keeps_cset_store_when_bool_slot_is_used \
    tests/c/test_self_backend.py::test_self_backend_aarch64_forwards_adjacent_fp_stack_store_load \
    tests/c/test_self_backend.py::test_self_backend_aarch64_flow_helpers_cover_bitcount_and_phi_assignment \
    tests/c/test_self_backend.py::test_self_backend_aarch64_phi_assignments_skip_temp_for_independent_scalar_sources \
    tests/c/test_self_backend.py::test_self_backend_aarch64_phi_assignments_preserve_parallel_copy_cycles \
    tests/c/test_self_backend.py::test_self_backend_runs_unordered_fcmp_semantics_in_ir \
    -q -n0
  ```

  Result: 8 tests passed.

- Stage1 dashboard after the peephole:

  | backend | pcc1 size | five-case pcc/python geomean | five-case pcc/C geomean | artifact pcc/C | text pcc/C |
  |---|---:|---:|---:|---:|---:|
  | LLVM | 7770712 | 0.130 | 2.200 | 11.050 | 13.000 |
  | self | 14021120 | 0.133 | 4.410 | 4.553 | 4.000 |

- Interpretation:
  - accepted as emitted-code cleanup on typed function-call hot paths;
  - self pcc1 size stayed at `14021120` bytes and `size_ratio self/llvm`
    stayed `1.804`;
  - no additional pcc1 size win is claimed for this slice.

2026-05-25 rejected dead-strip/section-size reduction probes:

- A link-arg-only LLVM probe compiled the same typed-int user-runtime case with
  and without `--link-arg=-Wl,-dead_strip`.
- Result: file size and `size` text output were unchanged
  (`457352` bytes and `__TEXT=278528` in both outputs), so adding the linker
  flag alone is not a valid size-reduction mechanism.
- A broader source probe added runtime `-ffunction-sections -fdata-sections`
  and made Mach-O dead-strip unconditional in the Python frontend link paths.
- Result: the probe was removed after stage1 evidence failed to improve the
  dashboard. The temporary LLVM sample showed file-size pcc/C `14.622` and
  text pcc/C `19.000`; the temporary self sample showed file-size pcc/C
  `15.167` and text pcc/C `19.000`.
- Follow-up cleanup:
  - reverted the source/link behavior changes;
  - ran `make -C pcc/runtime distclean` to remove stale probe-built runtime
    archives;
  - reran the stage1 dashboard and recorded the clean-rebuild baseline above.
- Interpretation:
  - the current binary-size gap should be attacked by runtime archive
    dependency slicing, symbol-level attribution, or smaller runtime feature
    closures, not by blindly enabling Mach-O dead-strip or section flags.
