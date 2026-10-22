# Dual Backend Plan: keep LLVM(llvmlite) + optional own backend

> Goal (this round): without affecting existing paths, start supporting an "optional backend" capability — first the switchable architecture, then phase in our own LLVM backend (runnable, but the default still goes through the current llvmlite path).

---

## 0. Current state

- In place: LLVM IR-level pass translation has reached a stable phase (the all-pass 1:1 milestone). The C front-end / parser / main pipeline / integration / cache still use llvmlite as the primary path.
- Risk: directly hard-switching to "our own backend" would destabilise `tests`.
- Conclusion: to keep "the mainline unchanged", we can only proceed via **dual backends**, **new backend off by default**, **optional fallback**.

---

## 1. Goal breakdown (options)

We need two layers of choice:

1. **Compile pipeline provider (IR + execution chain)**
   - `llvm`: the current `llvmlite` path (default)
   - `llvm_capi`: the alternative pipeline we get running first (text/IR through the LLVM-C API)

2. **Machine code backend (long-term)**
   - `builtin`: keep relying on the LLVM backend
   - `self`: our own Python-implemented backend (future, phased)

> For now we only require finishing layer 1 (dual backend selectable); `self` is wired up in stage 3.

---

## 2. Design principles

- **Default behaviour unchanged**: existing tests and the default CLI behaviour must keep going through the current stable path.
- **Estimate tasks by tokens, not person-hours.**
  - `repo token` here means the net tokens of code/tests/docs produced.
  - `working token` means the tokens consumed during execution + debugging + regression iteration; usually significantly higher than repo token.
- **Feature gating**: every new backend feature must be turned on by an explicit flag.
- **Fallback-able**: every new backend must have a "fail back to llvmlite" strategy so production isn't blocked.
- **Small granularity**: each phase touches only a few modules.

---

## 3. Task buckets (by milestone)

### Phase A: foundation (no behaviour change)

**Goal**: make "optional backend" purely a configuration knob, with no semantic changes.

#### A1. Backend selection interface (low coupling)
- Create `pcc/backend/`, define the backend protocol: `BackendKind`, `BackendConfig`, `BackendSession`.
- Add `PCC_BACKEND` env + `--backend` CLI (values: `llvm`, `llvm_capi`, `self`).
- Current default = `llvm`.
- Existing `pcc/pcc.py` / `pcc/frontends/c/evaluator/c_evaluator.py` only read this configuration.
- **Acceptance**:
  - `env PCC_BACKEND=llvm` is bit-identical to default.
  - `--backend=llvm` raises no new warnings.
- **Token estimate**:
  - `repo token`: `8k-25k`
  - `working token`: `120k-260k`

#### A2. Cache key + signature stamping (avoid pollution)
- Add to `_compile_cache_key` / local cache signature:
  - `backend_kind`
  - `backend_semver` (backend capability marker)
  - `backend_config hash`
- Switching `--backend` modes guarantees a cache miss.
- **Acceptance**: switching modes never reuses stale `*.so`/`*.json`.
- **Token estimate**:
  - `repo token`: `4k-9k`
  - `working token`: `40k-120k`

#### A3. Switch documentation + self-test
- Add a docs section: a new page under `docs/plans/` documenting default / optional / disabled.
- `tests`: add 3–5 lightweight tests that verify the env/CLI behaviour explicitly without changing existing semantics.
  - Default mode still runs the small key regression set.
  - Non-default backends report a clear error when not yet implemented.
- **Acceptance**: `pytest tests/test_backend_selector.py` (new) green.
- **Token estimate**:
  - `repo token`: `6k-12k`
  - `working token`: `60k-140k`

---

### Phase B: `llvm_capi` runnable backend (replace the llvmlite runtime entry)

**Goal**: without changing the codegen main path, let our own backend take over the runtime pipeline first.

#### B1. Backend adapter layer (object layer + execution layer)
- Add `pcc/backend/llvm_capi_backend.py` wrapping these `evaluate` calls behind one interface:
  - `llvm.parse_assembly`
  - `target_machine.emit_object`
  - `llvm.create_mcjit_compiler` / `ee.get_function_address`
- Introduce `BackendUnavailable` to clearly distinguish "interface not declared" from "execution failure".
- **Acceptance**: under `PCC_BACKEND=llvm_capi`, with no code-path changes, the behaviour boundary matches the llvm default path (when the backend implementation is complete).
- **Token estimate**:
  - `repo token`: `20k-45k`
  - `working token`: `250k-700k`

#### B2. Expand the surface of `pcc/ir`
- Fill in the minimum required declarations (no need to be complete in one pass):
  - runtime init, context/module, builder, target machine, parse/verify, object lifecycle.
- Don't aim for completeness; allow `NotImplemented` as a fallback.
- **Acceptance**: the `llvm_capi` backend covers at least the `parse + verify + emit_object` unit path.
- **Token estimate**:
  - `repo token`: `6k-18k`
  - `working token`: `80k-220k`

#### B3. Integration and fallback mechanism
- `PCC_BACKEND=llvm_capi` + missing symbols → automatic fallback to `llvm` (with warning + metrics).
- The `self-host` target (when enabled) may force the default backend to `llvm_capi`.
- **Acceptance**:
  - The backend selection is logged (auditable).
  - Fallbacks don't pollute the main cache.
- **Token estimate**:
  - `repo token`: `8k-16k`
  - `working token`: `80k-180k`

---

### Phase C: own machine backend (`self`) v1 (single target, asm-first)

> This is where "our own backend" really starts. Begin with the smallest target architecture without affecting the existing mainline.

#### C1. Scope lock (MVP)
- Support a single target only (recommend either AArch64-darwin or x86_64-linux).
- Only the following instruction range:
  - integer / pointer arithmetic, comparisons, branches, call, basic stack allocation, local array / struct read+write (only the subset our front end currently emits).
- Defer: SIMD, exceptions, DWARF, complex relocations.
- **Acceptance**: the self backend can compile the core subset of `tests/test_cli` and produce a runnable binary.
- **Token estimate**:
  - `repo token`: `90k-180k`
  - `working token`: `1.6M-4M`

#### C2. Minimum backend core (MIR / lowering)
- Lightweight parsing of LLVM IR text or direct lowering into our own intermediate form (recommend MIR).
- Implement:
  - linear-scan register allocation
  - simple ABI rules (calling convention / return value / argument passing)
  - frame layout (frame size, alignment, callee-save)
  - direct `asm` emission.
- **Acceptance**: small `while / if / call / return` programs compile and run with matching behaviour.
- **Token estimate**:
  - `repo token`: `120k-260k`
  - `working token`: `2.8M-7M`

#### C3. Backend selection + fallback
- Add `--backend=self` and `PCC_BACKEND=self`.
- Uncovered feature areas fall back / error out to `llvm_capi`.
- Add "capability labels" to tests: some run on the default backend, some skip under `self` (and that skip is explicit).
- **Acceptance**:
  - `self` can be turned on explicitly.
  - Features not yet covered by `self` no longer silently miscompile.
- **Token estimate**:
  - `repo token`: `12k-24k`
  - `working token`: `130k-320k`

---

## 4. Safeguard list — current work stays unaffected

1. `PCC_BACKEND` default = `llvm`.
2. Don't touch the existing default pass pipeline / optimisation order.
3. All existing suite gates still run on the default backend.
4. New backend features must be switch-gated:
   - When off, they don't enter any critical path.
5. Every phase keeps a `rollback`: switching back to default keeps the same commit green.

---

## 5. Relationship with existing plans (alignment)

- vs `all-pass-llvm-ir-1to1-master-plan`:
  - This plan does not duplicate pass translation; it only wires the already-translated passes into the optional backend interface.
- vs `archive/llvmcapi-wire-spike-report`:
  - This is the productionised, long-term task track of that spike.
- vs `self-backend-translation-plan`:
  - This plan turns the backend into an **optional capability**;
  - `docs/plans/self-backend-translation-plan.md` carves "our own machine backend" into its own roadmap;
  - The relation: `β4/llvm_capi` handles the early decoupling and shared builder; `self backend` lands incrementally on top of that shared boundary.
- vs `python-frontend-plan` Phase 6C:
  - This plan is the necessary substrate for Phase 6C: a shared artifact / backend pipeline.

---

## 6. Immediate executable tasks (suggested to start today)

- `Task 0`: create `docs/plans/dual-llvm-backend-compat-plan.md` (this plan)
- `Task 1`: add backend selection switch to evaluator / pcc CLI (`--backend` / `PCC_BACKEND`)
- `Task 2`: add a backend dimension to the cache fingerprint
- `Task 3`: add backend-selection self-test (2~3 tests)
- `Task 4`: start the `llvm_capi_backend` abstract interface skeleton (no behaviour change)

> The four can be done "while running the current full suite alongside": the default path is unchanged, new tests are scoped.

---

## 7. Milestone gates

- **M0 (this week)**: switch + cache isolation done, default behaviour unchanged.
- **M1 (next phase)**: `llvm_capi` can replace `llvmlite` up to emit_object / basic loading.
- **M2 (later)**: `self` backend can be selectively enabled and runs a minimal subset.
- **M3 (long-term)**: gradually expand `self` coverage and target families.
