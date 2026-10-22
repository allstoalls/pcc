# Python frontend codegen split

## Status

Active architecture note for the `L1CodeGen` split.

## Model

`pcc.frontends.python.codegen.layer1.L1CodeGen` is the host class. The native
lowering files under `pcc.frontends.python.codegen` are contextual mixins, not
standalone compiler implementations.

The semantic type of `self` inside these mixins is `L1CodeGen`.

Current contextual mixins include:

```text
pcc.frontends.python.codegen.async_with_lowering
pcc.frontends.python.codegen.core_helpers
pcc.frontends.python.codegen.cpy_import_state
pcc.frontends.python.codegen.dynamic_type_lowering
pcc.frontends.python.codegen.exception_lowering
pcc.frontends.python.codegen.extern_lowering
pcc.frontends.python.codegen.import_lowering
pcc.frontends.python.codegen.ir_scaffold_lowering
pcc.frontends.python.codegen.lambda_callback_lowering
pcc.frontends.python.codegen.typed_int_abi
pcc.frontends.python.codegen.typing_lowering
pcc.frontends.python.codegen.unsafe_lowering
pcc.frontends.python.codegen.native_*
```

## Gates

The hard correctness gates are:

```text
multi-file closure py_cpy_* == 0
ON-mode contextual mixin py_cpy_* == 0
pcc1 must not link libpython
pcc1 -> pcc2 -> pcc3 bootstrap must pass
```

`tests/python/test_pcc_bootstrap_full.py::test_full_three_stage_bootstrap_self`
is mandatory for this split work and must remain green.

OFF-mode contextual fallback counts are legacy builder/runtime bridge
ratchets. They are useful shrinking signals, but they are not the self-host
correctness claim.

## Split rules

Keep capability boundaries explicit:

```text
async_with_lowering.py async context-manager lowering
core_helpers.py       core LLVM/helper utilities shared by lowering mixins
typed_int_abi.py       typed-int ABI safety analysis
cpy_import_state.py    CPython import fallback state and ensure-init guard
dynamic_type_lowering.py enum member and dynamic type constructor helpers
exception_lowering.py exception construction, try/except, and error exits
extern_lowering.py     pcc.extern scaffold declaration/call lowering
import_lowering.py     Import / ImportFrom statement lowering
ir_scaffold_lowering.py closed-world llvm_capi/IRBuilder scaffold lowering
lambda_callback_lowering.py native lambda callback object lowering
typing_lowering.py     typing alias and protocol helper lowering
unsafe_lowering.py     pcc.unsafe intrinsic lowering
native_*.py            native stdlib/module lowering
layer1.py              host orchestration and remaining unsplit lowering
```

Do not make mixins depend on each other through unsupported closed-world
constant imports. For example, importing a `frozenset` constant from another
mixin module can reintroduce `py_cpy_*` calls in the self-host closure.

Prefer host methods for cross-mixin queries:

```text
ImportLoweringMixin -> self._is_unsafe_intrinsic(...)
```

instead of:

```text
from .unsafe_lowering import UNSAFE_INTRINSICS
```

If a cross-module constant is needed long term, first make it a native export
shape supported by the closed-world compiler.

## Baselines

`tests/fallback_baseline.json` is the authoritative fallback ratchet.

Policy lives in `pcc.frontends.python.codegen.host_contract`, not in the baseline
JSON. New split modules must be added there and must have contextual baseline
entries.

`docs/investigations/contextual-per-module-fallback-gate.md` records the
implementation history. This file is the architecture entry point.
