# β4 Implementation Backlog

**Derived from:** `docs/plans/archive/llvmlite-api-surface.md` (auto-generated trace)
**Status:** β4.0 complete (2026-04-21), β4.1 ready to start
**Methodology:** live trace of pcc on py_corpus phase1+3, inline C, ir_passes tests, + one `-g` debug run

## Surface summary

| Partition | Count | Share of calls (pcc-external) |
|---|---|---|
| A — codegen-core (ir.*)  | **34 APIs** | ~66 000 calls |
| E — binding (llvm.*) | **5 APIs** | ~8 000 calls (of which parse_assembly+verify = 100%) |
| D — long-tail / llvmlite-internal | 80 APIs | ~311 000 calls (llvmlite self-calls, NOT our concern) |
| C — metadata/DWARF | 0 hits in traced workloads |

**Key insight**: pcc's actual external usage of llvmlite is **~40 APIs**, not 150. The big "long-tail" bucket is llvmlite calling itself internally during `str(module)` serialization — those APIs are llvmlite's own implementation details that our text-emitting replacement never needs to expose.

## β4.1 implementation order (by call-site hit count)

Targets chosen so that each checkpoint keeps py_corpus phase1 passing. Sizes are rough LoC + expected days.

### Tier 1 — Must have for phase1 compile (Day 1-3)

These 12 APIs touch every py_corpus test. Without them no test compiles.

| API | Calls | LoC est | Notes |
|---|---|---|---|
| `ir.Module.__init__` | 143 | 30 | Top-level container; tracks funcs, globals, metadata |
| `ir.Function.__init__` | 19 309 | 50 | Track signature, attributes, blocks |
| `ir.Function.append_basic_block` | 504 | 20 | Append to function's block list |
| `ir.FunctionType.__init__` | 19 309 | 30 | Return type + arg types |
| `ir.IntType.__init__` | (inferred) | 15 | Just width; `__str__` → `iN` |
| `ir.Type.as_pointer` | 20 350 | 5 | Trivial: returns `PointerType(self)` |
| `ir.PointerType.__init__` | 20 350 | 15 | Wraps pointee + addrspace |
| `ir.VoidType` | (static) | 5 | Singleton; `__str__` → `void` |
| `ir.Constant.__init__` | 1 636 | 50 | Type + value; dispatch to format_constant |
| `ir.IRBuilder.__init__` | 349 | 30 | Track current block + insertion pos |
| `ir.IRBuilder.position_at_end` / `position_before` | 460 | 10 | Repointing |
| `ir.Block` / `BasicBlock` | 504 | 30 | Instruction list + label |

### Tier 2 — Common instructions (Day 3-5)

| API | Calls | LoC est |
|---|---|---|
| `ir.IRBuilder.call` | 1 082 | 30 |
| `ir.IRBuilder.load` | 445 | 15 |
| `ir.IRBuilder.gep` | 384 | 30 (variadic indices) |
| `ir.IRBuilder.store` | 335 | 10 |
| `ir.IRBuilder.alloca` | 291 | 15 |
| `ir.IRBuilder.ret` / `ret_void` | 271 | 10 |
| `ir.IRBuilder.branch` / `cbranch` | 170 | 20 |
| `ir.IRBuilder.bitcast` | 85 | 10 |
| `ir.IRBuilder.icmp_signed` | 82 | 20 |
| `ir.IRBuilder.add` / `sub` / `mul` | 81 | 30 (shared helper) |
| `ir.IRBuilder.unreachable` | 32 | 5 |
| `ir.IRBuilder.select` | 31 | 15 |
| `ir.IRBuilder.neg` | 25 | 5 |
| `ir.ArrayType.__init__` | 286 | 20 |
| `ir.LiteralStructType.__init__` | 43 | 30 |
| `ir.GlobalVariable.__init__` | 326 | 40 |
| `ir.PhiInstr.add_incoming` | 18 | 10 |

### Tier 3 — Exception handling (Day 5)

| API | Calls | LoC est |
|---|---|---|
| `ir.IRBuilder.invoke` | 16 | 30 |
| `ir.IRBuilder.landingpad` | 16 | 20 |
| `ir.LandingPadInstr.add_clause` | 16 | 10 |
| `ir.IRBuilder.extract_value` | 16 | 15 |
| `ir.FunctionAttributes.add` | 12 | 15 |

### Tier 4 — Small long-tail (hits from `pcc.*`, < 10 per API) (absorb or defer)

~15 rarer methods: `xor`, `mul`, `zext`, `and_`, `srem`, `sitofp`, `sdiv`, `ptrtoint`, `or_`, `shl`, `ashr`, `fdiv`, `fadd`. All trivial arithmetic — share a binop helper, each is 2-3 lines.

**Total β4.1 target LoC: ~800-1000** (vs. our 1-1.5k estimate — tracks).

## β4.2 binding surface (Day 6-8)

Just 2 APIs dominate:

| API | Calls | Notes |
|---|---|---|
| `llvm.parse_assembly` | 657 | text → ModuleRef |
| `llvm.ModuleRef.verify` | 657 | Validator |

Plus stub support for `llvm.ModuleRef.functions` iteration (for the few passes that walk the parsed module). Already have the C bindings in `pcc.ir/__init__.py`. Work is the Python-side wrapper.

Extra bindings for actual JIT/object emit (not hit by trace but needed for `pcc foo.c` end-to-end):
- `llvm.Target.from_triple` + `create_target_machine`
- `llvm.create_mcjit_compiler`
- `llvm.TargetMachine.emit_object`
- `llvm.initialize_native_target`

~10 additional C-API bindings, same pattern as existing 32.

## β4.3 open question: is debug-info actually on the hot path?

The `-g` workload in the trace produced **zero** DI* hits. That likely means:

1. `CCodeGenerator(emit_debug=True)` path wasn't fully exercised by my test harness (needs closer look)
2. OR: pcc's default compile path doesn't emit debug info in test corpus

Decision: defer β4.3 until there's a concrete user needing `-g` — or at least until a β4.2 gate run reveals the actual gap. **β4.1 + β4.2 as scoped will carry a functioning self-host path.**

## Next concrete step

Start β4.1 Tier 1. Write `pcc/ir/ir.py` with the 12 Tier-1 classes, expose under `PCC_USE_LLVMCAPI=1` opt-in, gate on `py_corpus/phase1` parity with llvmlite.

Estimated: 3 days to first green Tier-1 parity; 5 days total through Tier 3.
