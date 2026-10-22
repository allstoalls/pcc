# Real-module indexed x86 bridge mechanism gate

Status: UNRUN proposal. It applies no source changes. The separately preserved
bridge V2 patch is the only candidate production difference. Preparation and
every emission arm require admission and the existing coordinator supervisor.

## Correct input boundary

Use the complete real `pcc/frontends/c/ast/c_ast.py` source, SHA256
`21eacf1370cec218ea7ff97092ee47a96752505067caad2699f20239c74f2b58`.
Its only import is `sys`, and its only sys attribute is `stdout`. The driver
checks that syntax and the actual owned-builtin/provider policy. Normal
single-source library parse/lift, inference and L1 generation run with complete
module contents and ordinary export/root defaults. There is no synthetic
export table or omitted sibling provider.

Prepare one correct target-specific input for Linux x86 and one for Windows
x86. Both use the same real source filename, strict no-libpython, native
callable values, direct capture with fused uses, zero fallback, no owned IR
passes and backend optimize=False. The driver checks actual call records for
py_cpy targets and function/block names for strict no-libpython stub bodies.
The driver consumes `generator._direct_indexed_module`, the capture already
produced by normal `generate`; the empty direct-only text return is not an IR
module and is never recaptured. It does not rewrite a Linux target triple to pretend that IR was generated for
Windows. Current non-Mac Stage1 source selects IR passes off; this experiment
is still a scoped single-source library route, not the full 456-module Stage1
export context or parallel worker route.

The complete captured indexed module is written through the ordinary process
codec before target preparation changes frame/value state. Each arm decodes
that exact file afresh. The input SHA256 is required on both invocations.
Compiler imports are confined to the selected immutable baseline or candidate
source root. Targeted file pins supplement the coordinator's complete
before/after inventories; they do not replace a full source seal.

## Emission and comparison

Each arm calls the actual `emit_indexed_assembly` and
`encode_assembly_object`. Linux uses the production packed stack-map handoff;
Windows uses its ordinary symbolic metadata and COFF writer. Neither stage
builds a runtime, links an executable, invokes a subprocess or executes native
code. No FFI allowance is required.

Counters wrap the actual per-function emitter and distinguish:

- `diagnostic_instruction` wrapper construction
- extra operand-interning calls
- operand-data decoding, which remains once per instruction
- canonical arithmetic-flag reads, which also remain once per instruction
- instruction-arena projections

The asserted mechanism is N wrapper/interning calls in the baseline versus
zero in the candidate, with identical N operand decodes and N flag reads.
Block/PHI/terminator shells remain. Counter instrumentation changes elapsed
time, so the recorded phase durations cannot support a speedup claim.

Retain assembly, complete object bytes and a decoded contract containing all
sections, symbols, relocations and decoded stack maps. Section byte content is
represented by exact length/SHA256 in that JSON and remains available in the
full object. Windows unwind receives exact object-byte comparison and
.pdata/.xdata presence checks; this is not an independent unwind interpreter.

After all arms finish, the coordinator must require, per target:

1. Both result statuses PASS and the same exact indexed-input hash and CFG rows.
2. Equal assembly hashes, object hashes/bytes and decoded-contract hashes.
3. The asserted construction/decode/flag counters on both arms.
4. Complete source, input, packet and output seals plus normal wait/cleanup.

An individual arm's PASS is not the pair comparison. Multiply-invalid
instruction diagnostic precedence remains the documented V2 caveat; this
real valid-input gate does not close it. Native callback/ABI/GC behavior,
Windows-device execution, full Stage1 and fixed-point coverage remain open.

## Commands and limits

Run only through the separately hash-bound, source-reviewed single-process
bootstrap/adapter. Every invocation retains the existing 300-second total,
hard AS 4 GiB, hard NPROC=0, shared exclusive lock and 4-GiB free reserve.
No child, FFI, native runtime or general process-tree capability is added.
Preserve the live worker-tree state/budget markers, no-auto-pcc1 and
no-native-provisioning flags. All output directories must be fresh.

Preparation, once for each exact TARGET:

```text
real_module_gate.py --operation prepare --source BASELINE_SOURCE
  --arm baseline --target TARGET --input SAME_REAL_C_AST_SOURCE
  --input-sha256 21eacf1370cec218ea7ff97092ee47a96752505067caad2699f20239c74f2b58
  --output FRESH_PREPARATION
```

One baseline and one candidate emission for that prepared file:

```text
real_module_gate.py --operation emit --source ARM_SOURCE
  --arm baseline|candidate --target TARGET --input PREPARATION/input.pidx
  --input-sha256 ACTUAL_SEALED_INDEXED_INPUT_SHA256 --output FRESH_ARM
```

The only targets are `x86_64-unknown-linux-gnu` and
`x86_64-pc-windows-msvc`. Each prepare invocation requires the baseline
backend; each emission invocation pins its named arm. Stage ordering and
comparison remain coordinator-owned. Stop on any bound or contract failure.
Capacity planning reserves 1 GiB for the complete two-target group in addition
to the 4-GiB free reserve; this is an output estimate, not an enlarged process
budget. No execution has been performed when this proposal is frozen.

Bundled files: `real_module_gate.py`, `manifest.json`, and `README.md`.
