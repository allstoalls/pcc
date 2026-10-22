# 05 · Backends, SSA Mid-Tier & Passes

Both frontends converge on a common IR layer. pcc can emit native code through **three** backend routes, and treats LLVM as an *oracle* (a correctness reference) rather than a hard runtime dependency.

```mermaid
flowchart TD
    IR["LLVM IR text"] --> SEL{"--backend"}
    SEL -->|llvm| LLVMLITE["llvmlite (default)"]
    SEL -->|llvm_capi| CAPI["pcc/ir<br/>in-repo LLVM-C builder"]
    SEL -->|self| SELF["pcc/backend<br/>LLVM-free asm emitter"]
    LLVMLITE --> OBJ["object / exe / JIT"]
    CAPI --> OBJ
    SELF --> ASM["asm → as → ld"] --> OBJ
    CAPI -. oracle/parity .-> LLVMLITE
    SELF -. oracle/parity .-> LLVMLITE
```

## `pcc/ir` — the LLVM-C IR builder

A text-first IR builder that is a drop-in for `llvmlite.ir`, so the codegen can build IR without depending on the `llvmlite` Python package. The Python codegen imports it indirectly:

```python
from pcc.ir.compat import ir   # → ir_py by default; llvmlite.ir if PCC_USE_LLVMLITE_C=1
```

- `pcc/ir/ir.py` — the text IR builder (drop-in `llvmlite.ir`).
- `pcc/ir/binding.py`, `__init__.py` — ctypes LLVM-C FFI (`LLVMContextCreate`, `LLVMBuild*`, …).
- `pcc/ir/compat.py:50` — per-subsystem fallback gates `USE_LLVMLITE_PY` / `USE_LLVMLITE_C` / `USE_LLVMLITE_PASSES`, read once at import. This is the seam that lets `llvmlite` act as an **oracle**: run the same repro with `PCC_USE_LLVMLITE_C=1` and diff IR / results to localize a builder bug.

## `pcc/backend` — the LLVM-free self-backend

Lowers LLVM **IR text directly to native assembly**, no LLVM linked. This is the execution root for the strict self-host path.

```mermaid
flowchart TD
    IRT["IR text"] --> DISP["emit_self_asm(ir_text, triple)<br/>self_backend_dispatch.py:25"]
    DISP --> REG{"target registry<br/>self_backend_targets.py:31"}
    REG -->|aarch64-darwin| A64["emit_aarch64_darwin_asm()<br/>self_backend_aarch64_darwin.py:66"]
    REG -->|x86_64-linux| X86["emit_x86_64_linux_asm()<br/>self_backend_x86_64_linux.py:2187"]
    A64 --> PASS["self_backend_target_passes.py<br/>(post-emit asm passes)"]
    X86 --> PASS
    PASS --> ASM["assembly → as → ld (+ codesign on Darwin)"]
```

- Entry: `self_backend_dispatch.py:25` `emit_self_asm()`; target registry `self_backend_targets.py:31`.
- Targets: **AArch64 Darwin** (`self_backend_aarch64_darwin.py` + ~14 `self_backend_aarch64_darwin_*.py` submodules for abi/calls/regs/prologue/…) and **x86_64 Linux** (`self_backend_x86_64_linux.py`).
- Invoked from the Python pipeline via `_emit_self_asm_via_host_python()` (`pipeline.py:5856`), which runs the emitter in a host-Python subprocess on the IR file. LLVM is **not** used at runtime on this path — only as a parity oracle during development.
- Design note (from `AGENTS.md`): the self-backend and pass framework were developed against LLVM's published IR semantics; they are validated against the LLVM-backed path, not a source port of LLVM.

## SSA mid-tier (`pcc/frontends/c/ssa`)

A small SSA IR + analysis layer used for mid-tier reasoning:

- `pcc/frontends/c/ssa/ir.py` — `SSAValue`, `SSAParam`, `SSAConstant`, `SSABinaryOp`, `SSACall`, `SSAPhi`, `SSABlock`, `SSAFunction`.
- `pcc/frontends/c/ssa/builder.py` — `SSABuilder` constructs minimal SSA from function bodies.
- Analyses: ADCE, GVN, SCCP, loop-phi (`SSAADCEAnalyzer`, `SSAGVNAnalyzer`, `SSASCCPAnalyzer`, `SSALoopPhiAnalyzer`).

## Pass framework (`pcc/frontends/c/passes`) and the Python IR pass pipeline

```mermaid
flowchart LR
    AST["AST"] --> HIGH["HighTier<br/>read-only analysis → PassContext"]
    HIGH --> CGN["codegen<br/>(MidTier = codegen reads PassContext)"]
    CGN --> LOW["LowTier<br/>IR-text shaping / metadata"]
    LOW --> BACK["BackendTier<br/>LLVM O1/O2/O3 module opt"]
```

- Tiers defined in `pcc/frontends/c/passes/base.py:86` — `run_high_tier(ast, ctx)` (`:101`), `run_low_tier(ir_text, ctx)` (`:102`). MidTier is not a pass set; it's codegen consuming `PassContext`.
- The **Python** IR pass pipeline: `pcc/frontends/python/ir_pass_pipeline.py:407` `run_python_ir_pass_pipeline()`, applied from `pipeline.py` via `_apply_python_ir_pass_pipeline()` (runs in a host-Python subprocess).
- `pcc/ir_passes/` holds the individual Python-IR pass implementations.

## IR-text rewriting policy

Semantic bugs belong in parser/codegen logic, **not** in text rewrites. The only sanctioned IR-text rewrite is the `va_arg` lowering:

- `c_codegen.py:653` `postprocess_ir_text()` → `pcc/frontends/c/codegen/c_varargs.py:53` rewrites `__pcc_va_arg_*()` helper calls to real `va_arg` IR (a gap the builder can't express directly). Anything else (CFG, type, signedness) must be fixed at the source level.

## Key files

| Path | Role |
|---|---|
| `pcc/ir/ir.py` | in-repo LLVM-C text IR builder (drop-in `llvmlite.ir`) |
| `pcc/ir/compat.py` | `ir`/`ir_c`/`ir_passes` selection + llvmlite fallback gates |
| `pcc/backend/self_backend_dispatch.py` | `emit_self_asm()` target dispatch |
| `pcc/backend/self_backend_targets.py` | target registry (aarch64-darwin, x86_64-linux) |
| `pcc/backend/self_backend_aarch64_darwin*.py` | AArch64/Darwin emitter + submodules |
| `pcc/backend/self_backend_x86_64_linux*.py` | x86_64/Linux emitter |
| `pcc/frontends/c/ssa/` | SSA IR, builder, ADCE/GVN/SCCP analyses |
| `pcc/frontends/c/passes/base.py` | High/Mid/Low/Backend tier framework |
| `pcc/frontends/python/ir_pass_pipeline.py` | Python IR pass pipeline runner |
| `pcc/ir_passes/` | individual Python-IR passes |
| `pcc/frontends/c/codegen/c_varargs.py` | the one sanctioned IR-text rewrite (`va_arg`) |
