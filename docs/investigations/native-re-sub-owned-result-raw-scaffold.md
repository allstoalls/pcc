# Investigation: native re.sub loses result ownership in raw-scaffold code

## Status
active

## Problem Description
After repairing borrowed-local replacement, the owned native optimizer still
corrupts rewritten function text. A 32-function input reproduces the problem
without the full runtime: host instsimplify preserves all function definitions,
while the compiled kernel outputs missing/repeated definitions and stray
instructions. Real py_gen IR also crashes in instcombine's next_text == text.

Actual generated IR records next_text's owned flag as false after
py_re_engine_sub, despite that runtime ABI returning a fresh string reference.
The pcc.* module domain uses raw-scaffold ownership filtering, where the
inferred Dyn result can be classified as borrowed. Subsequent copies and
rebindings then do not preserve all live aliases. This is distinct from the
borrowed-slot unconditional release in borrowed-local-owned-rebind-consumes-source.md.

## Repro
tests/python/test_owned_ir_passes.py::test_compiled_simplifier_preserves_each_function
compiles the actual owned optimizer driver and compares exact output for 32
small functions. The corrected harness fails in 14.26 seconds. Its earlier
temporary driver did not close the new module's imports and failed at import;
that was a harness issue and is not the ownership regression.

## Test [CONFIRMED]
The 32-function native/host mismatch is observed before the fix. Full source
IR: /tmp/pcc_owned_optimizer_fixed_ir/self_backend_input_3.ll. Native crash
LR: user_pcc_native_ir_instcombine__rewrite_function + 605408, at py_str_eq;
log /tmp/pcc_owned_optimizer_runtime_probe_20260907/lldb-fixed.log.

## Proposals
- No.1 record the fresh regex-substitution result in the emitted owner ledger
  [pending]

## No.1 record the concrete ABI owner
### Code Change
native_text_modules._emit_native_re_sub_call records its result with
_note_owned_object_value. Assignment and temporary cleanup can then transfer
the known new reference without relying on an inferred Dyn type or guessing
ownership from the surrounding AST. Keep algorithm behavior and GC contracts.

### pending
Run the reduced native regression, regex semantic gates and the real runtime
input comparison before accepting this correction. No commit/push is allowed.

## Update: concrete owner fixes native text corruption
The 32-function comparison now passes (20.36 s including compilation). A
compiled optimizer with this fix processes the real py_gen input through
instsimplify,simplifycfg,instcombine,dce without crashing. Both host and native
results pass independent LLVM IR verification. Their remaining four return
constant differences are a distinct integer-projection defect, documented in
native-optimizer-wide-integer-projection.md; they must not be called equal.

The measured diagnostic executes with PATH=/nonexistent and host Python/cc
set to /usr/bin/false. Its 1.44 s outer duration and 223,002,624-byte sampled
peak are not a throughput win. Additional GC/native-regex coverage and fresh
pcc1 qualification remain open.
