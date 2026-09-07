# Codon as a performance reference for pcc

Assessment date: 2026-09-07. Codon source inspected at
[`8057bf9856169fad6ad7dfbb60c9e3eecbcbfd46`](https://github.com/exaloop/codon/tree/8057bf9856169fad6ad7dfbb60c9e3eecbcbfd46),
commit dated 2026-08-29. pcc starts at `080c3cf7` with the uncommitted owned
optimizer work described in [the handoff](2026-09-07-native-optimizer-wip.md).
No Codon binary was executed. This is a source assessment supported by pcc
experiments, **not a Codon/pcc throughput comparison or an estimated Codon gain**.
Neither repository is to be committed/pushed during this work.

**Later correction:** the first pilot's historical LLVM archive contains three
newer source files (additional diagnostic entrypoints) than the self inputs.
Its self-control/owned-pass comparison remains matched, but its LLVM reference
was not a strictly matched-source arm. A later replacement with the five exact
paired LLVM O2 objects still measures about 57.4k QPS versus self target-on
29.6k. The same-IR matrix below supersedes attribution from that first pilot.

Codon offers useful designs for reducing dispatch, allocation and repeated
compiler work. They can be implemented in pcc's own frontend/IR/backend without
adopting Codon as a dependency. Their gateway benefit must still be measured.

## Evidence and applicability

| Technique | Verified Codon mechanism | Existing pcc evidence and decision |
|---|---|---|
| Type specialization | [`realizeFunc`](https://github.com/exaloop/codon/blob/8057bf9856169fad6ad7dfbb60c9e3eecbcbfd46/codon/parser/visitors/typecheck/infer.cpp#L346) reuses a realization keyed by its realized type name; otherwise it clones and checks the generic body. | pcc already has typed calls, exact-int lowering and a bounded [guarded buffer-loop plan](../../pcc/py_frontend/guarded_loop_plan.py). Extend those proofs and cache specializations; do not infer exact runtime types solely from Python annotations. A generic monomorphization implementation or gateway speedup has not been established by this assessment. |
| High-level operations | [CIR](https://docs.exaloop.io/developers/ir/) retains typed calls/control flow. Its [dictionary pass](https://github.com/exaloop/codon/blob/8057bf9856169fad6ad7dfbb60c9e3eecbcbfd46/codon/cir/transform/pythonic/dict.cpp) and [string pass](https://github.com/exaloop/codon/blob/8057bf9856169fad6ad7dfbb60c9e3eecbcbfd46/codon/cir/transform/pythonic/str.cpp) fuse operations before their implementation becomes opaque low-level code. | pcc has a typed AST and a bounded [LowIR](../../pcc/py_frontend/low_ir.py), plus ownership logic in codegen. Preserve exact-type, ownership, escape, exception and suspension facts at the optimization boundary. This can target **fewer operations per request**; rewriting IR text alone cannot recover all such facts. No need to discard the existing frontend. |
| Value layout | Codon describes [tuple-as-struct and class-as-reference layouts](https://docs.exaloop.io/developers/compilation/#python-types-to-llvm-types). | pcc already proves an opt-in valueclass loop adds zero heap allocations across 0 versus 1,000 iterations, with two explicit boxed escapes. [The test](../../tests/python/test_py_value_class_unboxed.py) passed again in this assessment. Reuse aggregate payloads for proven internal values; ordinary classes retain identity, dynamic attributes, mutation and finalization contracts. This is an allocation proof, not a new QPS measurement. |
| Analysis reuse and inspection | Codon's [pass manager](https://github.com/exaloop/codon/blob/8057bf9856169fad6ad7dfbb60c9e3eecbcbfd46/codon/cir/transform/manager.cpp#L96) caches analysis results, invalidates dependent results and times passes. [CLI controls](https://docs.exaloop.io/start/usage/#logging) expose intermediate representations and per-pass disabling. | pcc's profile found 319 module splits and 142 per-function context reconstructions in one `py_obj` optimization. Sharing function attributes reduced a native diagnostic from 4.827 s / about 960 MiB to 1.455 s / 234.5 MiB with equal output. This independently supports the analysis-reuse principle; it does not establish a whole CIR migration speedup. These were single diagnostic runs, not repeated acceptance measurements. |
| Generator consumption | Codon's [generator pass](https://github.com/exaloop/codon/blob/8057bf9856169fad6ad7dfbb60c9e3eecbcbfd46/codon/cir/transform/pythonic/generator.cpp) replaces eligible `sum/any/all` generator consumption with direct accumulator/control-flow operations. | Useful as a model for eliminating unnecessary protocol work when no observable suspension/escape is lost. Gateway creates child tasks, yields and performs structured cleanup; this transformation does not prove its tasks can disappear. Codon's own [await lowering](https://github.com/exaloop/codon/blob/8057bf9856169fad6ad7dfbb60c9e3eecbcbfd46/codon/cir/transform/lowering/await.cpp) retains cancellation checkpoints and wait/yield behavior. |

## What cannot be transferred unchanged

Codon's documented `int` is fixed-width, and `-numerics=py` does not make it
arbitrary precision. Static compilation also constrains runtime dynamism.
These assumptions differ from pcc's ordinary Python contract.
[Official language differences](https://docs.exaloop.io/language/overview/).

Three CPython 3.15.0rc1 witnesses were executed:

```python
class Key:
    def __init__(self): self.calls = 0
    def __hash__(self):
        self.calls += 1
        return 7
k = Key()
d = {}
d[k] = d.get(k, 0) + 1
assert k.calls == 2

class Plus(int):
    def __add__(self, other): return 12345
def add_one(x: int): return x + 1
assert add_one(Plus(3)) == 12345
assert add_one(1 << 64) == 18446744073709551617
```

The first example means an unconditional single-lookup rewrite changes an
observable call count, even for an exact built-in dictionary. Key type/effects
and intervening operations matter too. The others show why annotations alone
cannot authorize raw arithmetic. These are Python contract witnesses, not
claims that this exact program was run or miscompiled by Codon. pcc's recent
[native wide-fold bug](../investigations/native-optimizer-wide-integer-projection.md)
is additional local evidence that confusing semantic integers with raw integer
storage produces incorrect results.

Using Codon's backend directly would violate pcc's toolchain ownership goal:
its [build requires LLVM](https://github.com/exaloop/codon/blob/8057bf9856169fad6ad7dfbb60c9e3eecbcbfd46/CMakeLists.txt#L40),
and its [generator emitter](https://github.com/exaloop/codon/blob/8057bf9856169fad6ad7dfbb60c9e3eecbcbfd46/codon/cir/llvm/llvisitor.cpp#L1683)
uses LLVM coroutine intrinsics. Borrow algorithms/IR contracts and implement
them in pcc-owned code; do not add LLVM, Codon or a Python subprocess as a
pcc1 execution owner.

Codon's documented [C interface](https://docs.exaloop.io/integrations/cpp/cpp-from-codon/)
imports typed C symbols and describes ABI conversions. That is useful ABI
reference material, but does not supply the pcc-owned C preprocessing, parsing,
semantics, optimization and linking required by pcc1. Preserve C and Python
semantic distinctions in shared lower-level IR; C is still a first-class input.

## Measured bottleneck: runtime emission still matters

The latest [raw gateway comparison](../../../pcc-gateway/benchmarks/results/2026-09-07-owned-runtime-emission-pilot.json)
holds the two application objects fixed, changes five runtime object emissions,
and uses seven rotating repetitions at concurrency 100, zero wait and 20,000
measured requests per run. All 560,000 measured responses passed the payload
and count checks. One carrier, GC0, no HTTP/socket layer.

| Runtime/emitter variant | Median handler QPS | Median whole-process instructions |
|---|---:|---:|
| Self emission, bounded original IR | 19,763.3 | 15.911 billion |
| Self emission, additional owned passes | 22,185.0 | 15.106 billion |
| Historical LLVM O2 runtime reference | 57,777.4 | 5.541 billion |
| Same-run CPython 3.15.0rc1 asyncio | 86,080.5 | 3.508 billion |

Owned transforms improve QPS 12.3% and reduce process instructions 5.1%.
The reference also changes low-level optimizations and machine-code emission;
this experiment does **not** identify one particular missing pass as the cause.
The self-control/owned-pass runtime sources and application objects are unchanged;
the historical LLVM reference needs the correction above.
Thus the substantial emission-path contribution cannot be dismissed as solely
application typing or virtual-thread architecture. The runtime's freestanding
integer/pointer operations are already low-level and typed; more frontend
specialization alone is not evidence of a remedy for this gap.

Concrete next candidates are the self backend's
[block-local register allocation](../../pcc/backend/self_backend_aarch64_darwin_regalloc.py)
(call-crossing values and PHIs retain stack traffic) and the indexed emitter's
[`optimize=False` route](../../pcc/backend/self_backend_indexed_emit.py).
Neither is yet proven to account for most of the gap. Compare the existing
target optimizations on the exact same optimized IR before expanding codegen.
Do not transform final assembly after stack-map/unwind offsets have been fixed.

The old 37.0% five-hotspot leaf share was sampled on the historical LLVM-runtime
artifact. It is neither a dynamic operation count nor a profile of this slower
self-runtime artifact. Count task creation/destruction, frame saves/restores,
retain/release and pointer validation separately from uninstrumented timing.
Report counts per completed request and instructions/cycles per operation;
then decide whether a change removes work or makes each operation cheaper.

## Acceptance and open work

The current owned optimizer/native execution/borrowed rebind/valueclass packet
passed **14 tests in 27.27 s**, using the explicitly selected, provenance-checked
prebuilt runtime. An earlier invocation reached 13 passes but timed out at
180 s during the default runtime-building path; it is not counted as a green
run. Neither packet qualifies a complete independent runtime build.

The retained [runtime variant builder](../../../pcc-gateway/benchmarks/build_runtime_variants.py)
recreated all 12 PCOs and all three linked programs byte-for-byte from the
pilot's recorded inputs. Its [benchmark note](../../../pcc-gateway/benchmarks/results/2026-09-07-owned-runtime-emission-pilot.md)
documents construction, provenance and rerunning timing. Host parsing/linking
and remaining prebuilt runtime members are still explicit migration debt.

Priority is to finish useful owned low-level optimization with measured
codegen attribution, then use high-level ownership/type/suspension facts to
remove redundant operations. Preserve ordinary Python semantics, all five GC
contracts and native C capability throughout. Codon provides specific useful
design references; it does not yet establish an asyncio win for pcc.

## Later update: same-IR optimizer/codegen matrix

The maintainer explicitly requires self to reach LLVM O2 performance before
resuming system-level task/frame/refcount cost changes. The corrected matrix
holds the application PCOs and remaining archive fixed. LLVM optimized IR and
objects are checked against the same input-IR/source receipt. Both self rows
enable pcc's existing target optimizations (host-owned diagnostic emission).

| IR optimizer | Machine-code emitter | Median QPS |
|---|---|---:|
| pcc owned tier | pcc self, target optimizations on | 29,674.5 |
| LLVM O2 | pcc self, target optimizations on | 34,212.5 |
| pcc owned tier | LLVM target machine | 49,802.2 |
| LLVM O2 | LLVM target machine | 57,554.5 |
| CPython asyncio witness | CPython 3.15.0rc1 | 83,634.2 |

Seven rotating repetitions, C100/zero wait/20,000 measured requests each;
35 runs and 700,000 validated responses. Whole-process instructions for the
same LLVM-O2 IR are 11.080 billion with self versus 5.541 billion with LLVM.
This narrows the main current gap to generated machine code, while still
showing a separate owned-IR optimization gap. It is scoped to this workload.

Raw matrix is retained in gateway benchmarks/results/
2026-09-07-self-llvm-ir-codegen-matrix.json. Native pcc1 target-on qualification
and performance parity remain open. Do not count externally optimized IR as
pcc-owned optimization; it is used only to isolate backend behavior.
