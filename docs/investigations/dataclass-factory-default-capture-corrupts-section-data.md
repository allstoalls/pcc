# Investigation: dataclass factory defaults corrupt section data

## Status

Active — 2026-10-01. Source12 fixes independent container factories and the
exact C-assembly component under all five GCs. Inherited factory signatures,
ordinary-default reuse and cross-module field layout still fail in that
snapshot. New-source pcc1/bootstrap qualification remains pending. This record
does not qualify the failing source9 pcc1 artifact.

## Problem Description

The source9 pcc1 generates IR and assembly for a two-function C program, but
assembling its compact-unwind section fails with
`compact-unwind local function relocation resolves outside __TEXT,__text`.
The identical assembly succeeds in the CPython-hosted owned assembler.

The default-capture change must preserve ordinary function defaults evaluated
at definition time. Dataclass `field(default_factory=F)` has a different
contract: capture the factory's identity and call it for each omitted field
when constructing an instance. Explicitly supplied values preserve identity.

## Repro

Base revision: `ab6f29d08a9ca53034c85c35eda636b135185262`, with uncommitted
remediation and directory changes. Diagnostic source10 snapshot identity:
`d208e3b7de13189c0584636fcbf517ba416eef196485334dddcb6e5f0f9f6d2a`.
It includes 1,089 source/tool files; its full path/hash manifest is
`build/remediation-20261001/frozen-current-v10-identity.json`.

The component probes are **host pcc0 compiling native self/no-libpython
programs**, using direct indexed capture/emission, fused uses, zero fallback,
frontend release and `PCC_PYTHON_IR_PASSES=off`. They execute under GC0 with
`PCC_GC_REFCOUNT_PROVENANCE_PROBE=2`. Their matching owned pcc-Python archive is
`runtime-v10/libpy_runtime_pcc_py.a`, SHA256
`cbbf18c964e36b1be0c2028775f6c9f5e721a4551dcb13d6b372a2d2e2b70a6c`.

The C assembly input's SHA256 is
`6b12d7259b042b7ac167acb070ca0bc45c2ca9dd9a82a85bc9982244453a4750`.
The source10 buffer probe is
`build/remediation-20261001/native-v10/cu_buffer_probe.py`, SHA256
`dbad5ede40208855c2078a3843bc32c18c17810b254490e676aa6762d279660d`.
Its binary is SHA256
`514d12c99e3ae2e5491cbd1e5e96a27c99a34bd8ec68ce40c499468897ce364d`.

Exact guarded execution (from the repository root):

```bash
env -u LC_ALL \
  UV_CACHE_DIR="$PWD/build/remediation-20261001/uv-cache" \
  UV_PROJECT_ENVIRONMENT="$PWD/build/remediation-20260930/venv" \
  uv run --no-sync python \
  build/remediation-20261001/stage1-v8-retained/owned_watchdog.py \
  --timeout 240 --max-tree-rss-bytes 17179869184 \
  --result build/remediation-20261001/native-v10/result-cu-buffer.json \
  --samples build/remediation-20261001/native-v10/samples-cu-buffer.jsonl \
  --stdout build/remediation-20261001/native-v10/stdout-cu-buffer.log \
  --stderr build/remediation-20261001/native-v10/stderr-cu-buffer.log \
  -- build/remediation-20260930/venv/bin/python \
  build/remediation-20261001/native-v10/run-cu-buffer.py
```

The listed result/log paths are recorded receipts; use new output names for a
rerun. The watchdog refuses overwriting receipts. It holds the shared heavy-run
lock and observes only its own descendant tree through Darwin libproc.

## Test [CONFIRMED]

A two-`ret` assembly control has text size 8, symbols at 0/4, zero embedded
addends and a valid unwind section. The exact C assembly has text size 104,
symbols at 0/16 and succeeds under the host assembler. Native execution of
that exact assembly fails at the unchanged unwind validation guard.

The buffer probe reads back:

```text
ALIASES True True True
UNWIND_PAYLOAD 64 248
CU_ROW 32 _add 0 3 0
CU_ROW 96 _main 0 3 0
CU_ROW 0 _add 0 3 3553411906360525648
CU_ROW 32 _main 0 3 0
BUILD_FAILED EncodeError compact-unwind local function relocation resolves outside __TEXT,__text
```

The three aliases are `chunks`, `relocations` and `symbols` between stackmap
and unwind buffers. Their joins include both sections' data. The integer
`3553411906360525648` is the little-endian value of `b'PCCSMAP1'`, so the first
unwind relocation reads a stackmap header as its addend. The probe catches the
expected error for observation; its exit code 0 is not a successful assembly.

## Proposals

`class_gen._maybe_expand_dataclass` copies `field(default_factory=...)` into
the synthetic constructor's default expression. `call_expression_lowering`
executes the factory for that expression. `user_function_lowering` now
captures method-default objects once, and both `py_func` binders reuse those
objects for omitted arguments. That combination shares the three mutable
containers in `_SectionBuffer` across instances.

Use explicit synthetic factory semantics to capture the callable once and
invoke it for each omitted constructor parameter. Preserve ordinary defaults,
supplied values/None, inherited fields, first-class class calls, cross-module
calls, factory exceptions and returned-object ownership. Reject weakening
relocation validation or copying the captured container: either would hide the
same defect in other record families and custom factories.

## New-source correction [CONFIRMED]

Source12 identity is
`a3898cdd454c0cce195bf67db598651e39ee5a219170dd84fa036ba55f216050`;
its full manifest is
`build/remediation-20261001/frozen-current-v12-identity.json`. The matching
owned archive SHA256 is
`352f0953faf25240b87e61bce291440dc9e7554f320a81caeca19bbee3b03707`.
The isolated host environment contains no llvmlite. These are host pcc0 ->
native self/no-libpython component executions with the same direct-emission
and pass-off options stated above, not execution by a newly built pcc1.

The explicit factory signature captures a callable, invokes it only for an
omitted argument, and retains ordinary default objects. The executing main and
function-local class entries now consume the expanded ClassDef that contains
the capture events. Actual executions passed under GC0 through GC4 for each
of these seven shapes: independent built-in containers, function-local class
factories, failure/unwind ownership, a factory returning a generator, a
callable-object factory, the two-ret assembly control and the exact C assembly.
That is 35 executions, all with expected stdout and empty stderr. Receipts:

- `native-v12/behavior-focused.json` (buffers passed; custom stops the run).
- `native-v12/behavior-components.json` (both assembly controls passed;
  the subsequent external buffer read stops the run).
- `native-v12-projection/behavior.json` (local class passed; the subsequent
  diagnostic projection fails).
- `native-v12-factory-resume/behavior.json` (unwind/generator/callable passed;
  ordinary-default reuse in the order control stops the run).

The exact C assembly now reports text size 104, symbols at 0/16 and
`EXACT_C_UNWIND_OK 3` on all five GCs. Its native program source SHA256 is
`b529c994d6ea345970838b2537bd6ae1c43b25f8f3e24cb3fb9d1b3ff04b6083`;
binary SHA256 is
`39733348bc637515bb78b815588cfb7ff7fe7cd59307a7f5af7cf7db0db55167`.
The relocation guard remains unchanged.

The source12 matrix command is the source10 guarded command above with
`--timeout 600`, output names under `native-v12`, and the child script
`build/remediation-20261001/native-v12/run-focused.py`. The independently
selected component, local/projection and remaining-factory runs use their
respective `run-components.py`, `native-v12-projection/run.py` and
`native-v12-factory-resume/run.py`; each receipt records the exact command,
timeout, logs and sampled tree RSS. The matrix stops at its first failed
program; these component receipts are not a green full matrix.

## Remaining source12 failures [CONFIRMED]

`custom.py` fails at the Child definition with
`AttributeError: object has no attribute __defaults__`. A bounded diagnostic
of Base's published native method after class initialization finds a PyFunc
with a two-item captures tuple, a valid five-item signature, the correct
25-byte magic and a non-null defaults getter. That minimal diagnostic omitted
the original program's later factory rebinding, so it did not reproduce the
failure. Diagnostic receipt: `native-v12-inherited/behavior.json`.

The subsequent bounded diagnostic preserves exact CUSTOM and inserts only a
probe before Child. Its borrowed native-table lookup uses `c_rawptr`, avoiding
an incorrect new-reference contract for `py_class_lookup`. The signature
header is still valid, but its rows formal contains flag bits 5 (tagged integer
2, meaning factory) and a NULL default value. No error is pending before or
after the getter; its NULL result correctly rejects that missing factory.
Receipt: `native-v12-custom-items/behavior.json`, program SHA256
`1818cab3dd4a768421c6ff4254070a9dad2e871ab24a15d0e4da8f19d98f171a`.

Source inspection identifies the producer: later `make = replacement`
predeclares a NULL module-variable slot, but the preceding plain FuncDef
publishes only the module attribute and does not store the function in that
promoted slot. Capturing `make` reads NULL. The definition-publication seam
must populate the existing slot while preserving later reassignment and the
captured original factory identity. The defaults getter remains unchanged.

The ordinary default control fails `first is second`. The order control
passes its definition-time `[1, 2, 3, 4]` ledger and dataclass field-value
assertions, then fails `ordinary() is ordinary()`. Local direct-call lowering
still substitutes the default AST for an omitted opaque default; function
value creation can evaluate a throwaway signature before checking its cache.
Definition-time evaluation and reuse at calls are separate requirements.

The external buffer probe prints `ALIASES False False False`, then fails
`bytes.join`. A diagnostic prints `_SectionBuffer` for the owning objects but
`int`, value `33554432` (the unwind section flags), for `unwind.chunks`.
Source inspection confirms that appending synthetic `__init__` after the
original methods makes physical field discovery encounter earlier method
self-writes first, while exports retain declared dataclass order. Constructor
field discovery must agree with the exported layout without moving factory
publication ahead of its definition-order captures. This is an additional
layout defect, not evidence that the original shared-container defect persists.
