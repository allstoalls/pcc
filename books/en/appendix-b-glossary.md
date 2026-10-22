# Appendix B: Glossary

Terms as used in this book, with the pcc-specific meaning where it differs
from common usage. Code identifiers, CLI flags, and environment variables
appear verbatim throughout.

## Compiler

| Term | Meaning in pcc |
|---|---|
| lowering | Translating a higher-level construct into lower-level IR. Most pcc bugs are lowering bugs, not parser bugs. |
| translation unit (TU) | One compiled C source; directory mode merges many into one TU by default. |
| constant folding | Treated as a semantic subsystem, not a mere optimization (playbook §12). |
| usual arithmetic conversions | The C-standard conversion rules; mirrored by `_usual_arithmetic_conversion` in [pcc/frontends/c/codegen/c_codegen.py](../../pcc/frontends/c/codegen/c_codegen.py). |
| signedness | A property tracked separately from the LLVM `i32` bit pattern; losing it flips `udiv`→`sdiv` etc. |
| oracle | A known-good reference implementation compared against from identical input (native cc for C, CPython for Python, llvmlite for `llvm_capi`). |
| parity | Two implementations producing identical output for the same input. |
| fallback | The boundary where strict native lowering gives up and routes to the CPython bridge; hard error under `--python-libpython=off`. |
| scaffold (`--ir-scaffold`) | The closed-world lowering mode used by the strict self-host path. |
| fail loudly | The strict-mode policy: unsupported idioms are compile errors, never silent semantic drift. |

## Runtime and GC

| Term | Meaning in pcc |
|---|---|
| object header | `PyObjectHeader`: refcount at offset 0, 32-bit type tag at offset 8, flags. |
| owned / borrowed reference | The ownership contract: calls return owned references; callees returning borrowed values must retain. |
| write / read barrier | `pcc_gc_store_ptr` / `pcc_gc_load_ptr`; required for backends #3 and #4. |
| remembered set | Record of old→young references in the generational backend. |
| tricolor invariant | The marking invariant behind the incremental (#1) and concurrent (#2) backends. |
| safepoint | A program point where the GC may safely inspect or move state. |
| frame root | A registered local-variable slot; slot-granularity and non-LIFO in pcc, hence the hash-based frame index. |
| promotion | Moving a young object into the old generation (backend #3). |
| colored pointer | Address-embedded GC metadata, after ZGC (backend #4). |
| resurrection | A finalizer making its object reachable again; bounded by `PY_FLAG_FINALIZED`. |
| immortal | An object exempt from refcount death (`PY_FLAG_IMMORTAL`). |

## Value model

| Term | Meaning in pcc |
|---|---|
| value class | An opt-in, identity-free payload type with explicit boxing. |
| projection | The mapping from one semantic type to one of several physical representations. |
| value / object projection | E.g. `int` as tagged small-int lane vs. boxed bignum. |
| boxing bridge | The conversion between the two projections. |
| identity escape | Applying `id`/`is`/weakref to a value-class instance; diagnosed, not silently allowed. |
| deopt / promote | The only legal responses to value-lane overflow; wrapping is a bug, not a semantic. |

## Bootstrap and method

| Term | Meaning in pcc |
|---|---|
| bootstrap | The staged build pcc0 → pcc1 → pcc2 → pcc3. |
| fixed point | pcc2 and pcc3 stable and byte-identical: evidence the system reproduces itself. |
| byte identity | Equality of emitted artifacts after Mach-O signature normalization. |
| gate | A check that must stay green; per-subsystem focused gates plus bootstrap gates. |
| ratchet | A baseline that may only tighten (e.g. [tests/fallback_baseline.json](../../tests/fallback_baseline.json)). |
| claim hygiene | Every capability claim states the mode that produced it and what it does not prove. |
| mode-labeled | Annotated with host-pcc vs pcc1, libpython vs no-libpython, LLVM vs self backend, stage1 vs fixed point. |
| investigation | A written, evidence-chained record under [docs/investigations/](../../docs/investigations), one bug per file, ending in a CONFIRMED/DENIED verdict. |
| case study | The raw material of each chapter's History and Lessons section. |
| no-libpython | Not depending on the CPython runtime; it does not by itself imply zero-libc. |
| zero-libc | A Linux mode claim: no production C/libc object, dynamic interpreter/dependency, or undefined symbol. The label is inapplicable to Darwin's named-libSystem boundary. |
| compiler intrinsic | A raw-memory, atomic, syscall, or host-ABI machine operation. It must not become a hiding place for Python semantic policy. |
| freestanding pcc-Python | The strict, managed-runtime-independent subset used to implement allocation, threads, safepoints, GC, and the libc-like substrate. |
| C-API shim | The ABI surface extensions see; production ownership is migrating into `py_capi_*_runtime.py`, distinct from CPython's libpython. |

## GUI

| Term | Meaning in pcc |
|---|---|
| declarative UI | Components derive descriptors from props and state instead of mutating the committed tree every frame. |
| keyed reconciliation | Reuse, move, replacement, and removal decided by parent, key, and compatible node type. |
| atomic commit | Only a complete work-in-progress result may mutate the committed kernel tree. |
| scheduling lane | Discrete, animation, default, or background priority with aging and replay semantics. |
| effect phase | Before-mutation, mutation/layout cleanup, layout creation, passive cleanup, or passive creation. |
| managed state | Application state in the command owner with explicit slot kind and ownership, not an arbitrary Python object. |
| native bridge | The named CoreGraphics/Metal/AppKit ABI boundary; render/present reachability does not imply pixel correctness. |
