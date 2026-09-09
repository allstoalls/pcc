# Missed memory promotion: pipeline audit, 2026-09-08

This is the retrospective requested by the maintainer after another session
identified the missing effective memory-promotion path. The primary failure
was not converting a known implementation limitation into a measured,
prioritized check. The source and investigation already contained the clue.
The diagnostic model was incomplete; saying only "read the index next time"
would not address it.

Predecessor and measurement record:
[`runtime-module-optimizer-throughput.md`](../investigations/runtime-module-optimizer-throughput.md).
The work remains part of pcc #188. This page does not qualify LLVM O2 parity,
an asyncio win, a new bootstrap or any of the five GC backends.

## Evidence available before the new implementation

Inspect commit `080c3cf7`, rather than treating today's source as yesterday's:

```bash
git show 080c3cf7:pcc/py_frontend/compiled_default_passes.py
git show 080c3cf7:pcc/py_frontend/pipeline_pass_driver.py
git show 080c3cf7:docs/investigations/runtime-module-optimizer-throughput.md
```

1. `compiled_default_passes.py` explicitly described finite textual subsets.
   `_mem2reg_function` rejected a candidate when a use was outside the alloca's
   block. It had no dominator/PHI promotion for loop-carried slots. Selecting
   the name `mem2reg` did not establish the capability required by real IR.
2. `apply_passes` returned from `_compiled_default_requested` through
   `run_compiled_default_tier` before the general route. The effective default
   implementation therefore had to be inspected, independently of other
   implementations in the repository.
3. The investigation's "default LLVM policy withdrawn; attribution corrected"
   update already distinguished available transforms from selected transforms,
   named this bounded tier and required comparison on the same hot IR.
   The continuation recorded that stronger explicit routes still reached
   llvmlite. Missing information was not the only obstacle.
4. The later [native optimizer handoff](2026-09-07-native-optimizer-wip.md)
   explicitly said the default two-pass tier remained unchanged, while
   recording a standalone benchmark manifest of
   `instsimplify,simplifycfg,inline-defined,instsimplify,simplifycfg,instcombine,dce`.
   The manifest omits `mem2reg,sroa`; it persists in gateway's
   `benchmarks/build_runtime_variants.py`. Inputs may already have received a
   weaker tier, so omission alone is not proof that no memory pass ever ran.
   It is proof that the complete input-to-output chain needed auditing.

Commit `b9cfbce9` subsequently added `native_ir/mem2reg.py` with dominators,
dominance-frontier PHI placement and renaming, and routed the production
default to `run_owned_passes`. Its recorded 170-member census and gateway A/B
show the importance of this correction. Those measurements were produced by
the other session; they were not independently rerun for this retrospective
and were not available at the earlier decision point.

## What went wrong in the investigation

- **Implementation understanding:** a correct name and dependency-free
  execution were given too much weight. The decisive question was whether the
  selected implementation actually promoted the cross-block slots in the hot
  runtime functions. That capability was not established.
- **Route coverage:** production and standalone benchmark dispatch were
  maintained separately. A working library implementation or native compile
  did not establish that each measured entry ran the same algorithm.
- **Priority:** profiling and same-IR backend comparisons justified examining
  code generation, but did not close the known optimizer coverage gap. Local
  instruction work advanced while that larger, already documented gap stayed
  open. Valid local measurements do not justify that ordering.
- **Verification:** semantic tests and host/native output equality checked
  correctness and agreement; both arms can agree on an ineffective pass.
  They needed a separate effect check on representative production IR.
- **Knowledge use:** the warning was recorded, including in the handoff, but
  was not enforced as a prerequisite to the next experiment. The durable
  corrective action is a pipeline/effect audit, not another vague reading rule.

These are conclusions about the visible commands, source and records. They
do not require inventing an explanation about hidden model reasoning or
claiming that no earlier document was read.

## Four distinctions to keep explicit

| Question | Evidence required |
|---|---|
| Who executes the compiler step? | CPython running pcc-owned code, native pcc1, or an explicitly labeled external reference; check the actual process/import route. |
| How is IR represented or transported? | Text, owned parsed model, indexed arenas/sidecars; inspect the actual producer and consumer. |
| What does the optimization do? | Algorithm coverage and before/after IR effects, including unsupported cases. |
| What produced the measured program? | Effective dispatch, source/options, cache receipts, selected archive members and final artifact identities. |

`native_ir` does not mean "no text": current `mem2reg_text` calls
`MutableModule.parse` and serializes the result. Memory promotion removes
eligible memory operations; `PCC_PYTHON_IR_PASS_TRANSPORT=memory` selects a
transport. These are different properties. Text use itself did not cause the
promotion gap, and changing transport alone would not fix it.

## Remaining dispatch gap reproduced during this audit

At `d87f8594`, production `run_owned_passes` has the new memory tier, but
`native_ir.driver.optimize_ir` still has an independent scalar/CFG dispatch
which rejects `mem2reg`. The focused working-tree regression is
[`test_owned_optimizer_driver_memory.py`](../../tests/python/test_owned_optimizer_driver_memory.py):
a loop with index and sum slots requires two PHIs and compares the standalone
entry with production dispatch.

```bash
gtimeout 60s env -u LC_ALL uv run pytest \
  tests/python/test_owned_optimizer_driver_memory.py -x -n0 -vv --tb=short
```

Observed on 2026-09-08: **1 failed in 0.12 s**, with
`ValueError: unsupported owned IR pass: mem2reg`.
Log: `/tmp/pcc_memory_driver_audit_20260908.log`.
This is a reproduced integration gap, not a claim of a completed fix.

The [new AGENTS rule](../../AGENTS.md#compiler-performance-prove-the-selected-pipeline-first)
requires route, coverage, effect and artifact checks before further tuning.
Next implementation work must converge the driver with the canonical owned
dispatch, validate pass composition (including the already recorded
[`simplifycfg` namespace failure](../investigations/owned-simplifycfg-value-namespace.md)),
and then measure matched optimizer and backend arms with emitted-program and
five-GC correctness gates. Documentation alone does not close those tasks.
