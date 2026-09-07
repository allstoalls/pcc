# CPython capability interface review — P1

Native pcc1 self-hosting and five-GC correctness/performance remain P0. This
read-only audit does not authorize displacing that work with a new embedding
implementation. The maintainer requires CPython `import pcc` to provide real
public capabilities consumed by pcc-dependent libraries.

## Existing implementation

`pcc/api.py::build` and `module` already implement C-source builds and loaded
Python-callable C functions. `tests/c/test_api.py` exercises integer functions
and explicitly typed floating functions. CPython embedding was therefore not
entirely absent from the original design; its current scope is C-source
integration, not the complete owned Python-native/runtime interface now required.

The implementation still constructs `CEvaluator`, may use a system
preprocessor, and links through system cc in `_link_exe` and `_link_shared`.
Selecting backend=self does not remove that link owner. These remain migration
defects under the dependency ownership contract.

Python library-shaped emission exists in `compile_python(python_library=True)`
but requires emit-only mode. It does not by itself build/load an executable
Python-native library in CPython. The owned Mach-O writer inspected here emits
executables; no complete owned dylib/embedding implementation was identified.

## Material interface gaps

1. `Module.__getattr__` exposes an untyped ctypes function. Its return type
   defaults to C int and its argument types are unset; the tests manually set
   them for floating functions. Export names alone do not specify a callable
   ABI, especially for pointers, wide integers, floats or aggregates.
2. Loader symbol lookup is not restricted to `BuildArtifact.exports`.
3. The interface does not define pcc runtime initialization, foreign-thread
   registration, Python-exception transfer, owned-result release, callback
   retention or asynchronous resource completion.
4. CPython and pcc heap objects are different representations. A pcc object
   address cannot simply be passed as ctypes.py_object. Scalars, buffers and
   owned opaque handles need explicit conversion/lifetime contracts.
5. Accessing value-model exports currently also imports `.api` and the C
   evaluator. Root import attempts roadmap_deepwire installation and suppresses
   exceptions. Public capability loading needs better locality and diagnostics.
6. Build output names are fixed inside a requested directory, while module,
   callable, buffer and callback lifetimes are not a complete public contract.

These are concrete gaps, not evidence that the embedding direction is
fundamentally invalid. An implementation should deepen one native-module
interface that owns compilation, typed loading, identity/cache and lifetime;
otherwise every pcc-dependent package would reproduce those responsibilities.

## First subsequent P1 execution slice

CPython -> owned loadable native image -> explicitly typed freestanding
scalar/buffer function. Reuse Python library emission, the self backend,
existing C-ABI exports and standard-library ctypes as the CPython/OS adapter.
Do not add a second compiler or translate ordinary Python semantics into silent
fixed-width behavior. Retain the image behind every callable, validate exact
argument/result widths and buffers, and deny external compiler/LLVM dependencies
in the execution gate. Rich pcc object/exception/GC and Metal interfaces follow
with their own runtime lifecycle gates.

The missing owned loadable-image implementation is a real prerequisite.
Cross-process execution is not equivalent evidence for a low-overhead in-process
call interface. Likewise, a CPU oracle does not prove requested Metal execution.

## Delivery

Track #197. The separate #195/#196 tools expose real CPython APIs but do not
complete native embedding. Initial prebuilt-wheel targets are Linux x86_64,
Linux arm64 and macOS arm64; wheels retain `.py` sources for import and bundle
already-qualified pcc1/runtime artifacts so installation does not rebuild them.
The registry currently lacks a Linux arm64 self emitter. Platform qualification
must precede release claims; no wheels were built or published in this audit.

Independent exploration was read-only; no embedding implementation or heavy
gate was run for this review. See the dated public-api-tools handoff for the
actual focused tests and the historical-pcc1 inspection build failure.
