# PCC GPU Next Work Contract

Date: 2026-07-06

This document internalizes the GPU / GPU-GC / distributed / ds4 next-work
plan into the repository. It supersedes ad hoc chat notes for this route. The
source prompt read for this consolidation was
`~/Downloads/codex_pcc_gpu_next_work.md` on 2026-07-06, but that downloaded
file is not a runtime dependency; the durable authority is this document plus
`docs/goal/task-board.yaml`.

## Current State Calibration

The GPU route has moved past the first Metal demo:

- `pcc/kernel_ir/` defines a kernel-only IR boundary with PyObject escape
  rejection.
- TIRx-like freeze, TVM-shape projection, Metal source finalization, launch
  planning, host bridge artifacts, native MTLBuffer bindings, runtime-source
  command-buffer execution, CPU readback verification, and package manifests
  exist as separate claim layers.
- Runtime-source Metal execution is locally proven for a shaped copy kernel, a
  small imported TileLang scalar GEMM, strict static TileLang transposed
  scalar-GEMM variants (`transpose_A` with A(K,M), `transpose_B` with B(N,K),
  and combined transpose_A+transpose_B), `T.serial`/`T.Pipelined` start/end
  range syntax including nonzero K-loop starts for the current GEMM subset, a legal
  static finite POD `T.fill(buffer, value)` form with shared CPU/Metal dtype
  coercion (real device readback proven for f16/f32), a legal
  TileLang `T.Parallel` A/B/C tile-copy staging form, one-dimensional
  `T.vectorized` tile-copy staging metadata for the same subset, and a bounded
  canonical indexed elementwise assignment subset for static `T.Parallel`
  and one-dimensional `T.vectorized` loops (CPU oracle plus real
  runtime-source Metal readback), plus one fixed 5x7x16 f16/int16 2:4
  `T.gemm_sp` correctness shape lowered by pcc to scalar Metal metadata decode
  and proven by real f32 readback (not sparse-MMA or performance proof),
  `T.use_swizzle` no-op and enabled row/column threadblock rasterization
  metadata for scalar-GEMM tile mapping, empty
  `T.annotate_layout({})` no-op layout metadata, swizzled
  `T.annotate_layout({buf: make_swizzled_layout(buf)})` local-layout metadata
  with runtime-source Metal execution for rank-2 bank-swizzled and
  padded-fallback shared A/B tiles. The pinned TileLang
  `make_wgmma_swizzled_layout` helper is separately preserved with its
  `LayoutInference`/CUDA-SM90/WGMMA identity and rejected explicitly on Metal;
  it is not downgraded to the ordinary swizzle. Runtime execution also covers
  split-k f32 `T.atomic_add(C, C_local)`
  output accumulation for that scalar-GEMM subset including non-divisible K
  tails when the copy-index span is explicitly `T.ceildiv(K, split_k)`, and
  the first 8x8 simdgroup GEMM microkernel including enabled row/column
  `T.use_swizzle` tile remap on 2x2 and 3x2 tile grids plus nonzero K-loop
  range execution, divisible and non-8-wide/ceildiv split-k atomic f32 output
  accumulation, split-k atomic M/N edge tiles including a combined M/N-edge
  plus explicit ceildiv K-tail case, combined transposed A(K,M) / B(N,K)
  operand layouts, plus non-atomic M/N edge-tile and K-tail execution using
  zero-padded threadgroup staging, the first two-simdgroup-per-threadgroup
  N- and M-direction runtime-source shapes (`block_n=16` or `block_m=16`,
  `threads=64`), plus the first four-simdgroup 2-D runtime-source shape
  (`block_m=16`, `block_n=16`, `threads=128`) including the combined
  `transpose_A` / `transpose_B` operand-layout variant.
- One bounded dynamic-shape contract exists for a one-dimensional TileLang
  vector extent `N`. It validates resource/grid overflow and uses a
  source-and-bounds-complete cache identity before specializing through the
  strict importer to fully static Kernel IR. This is contract evidence only;
  no dynamic runtime dispatch or GPU execution is claimed.
- DLPack ownership now exports/imports a classic C-ABI `DLManagedTensor`
  through the real CPython `dltensor -> used_dltensor` capsule boundary,
  accepts foreign kDLMetal producers for contiguous/default-stream tensors,
  and defers the foreign deleter until a `PccFenceToken` completes. External
  torch/MLX/MPS round-trip, non-default stream synchronization, pcc1 capsules,
  and five-GC device execution remain separate gates.
- `pcc/dist/` and `tests/dist/` contain local session, sharding, KV, and
  collective oracles plus explicit no-fallback localhost TCP-ring and
  transport-collective owners. They are not multi-Mac execution, tensor/KV
  execution, or throughput/scaling proof.
- `pcc/gpu_gc/` remains CPU-only, but now includes a production-shaped external
  resource seam for GC backend labels 0..4: opaque `PccBufferHandle` resources,
  native release callbacks, and fence-deferred release. It is not yet wired into
  the C runtime or pcc-Python runtime GC backends.
- Offline `.air/.metallib` production remains blocked on this machine by the
  missing Xcode Metal Toolchain component.

Therefore the correct claim is:

```text
pcc has a host/device-split Kernel IR and selected runtime-source Metal device
result proofs. It does not have whole-program GPU execution, metallib-backed
launch proof, pcc1-native GPU launcher proof, five-GC GPU lifetime parity, full
TileLang/TVM pass execution, or ds4 support.
```

## Reference Trees

Reference source lives outside the repo under `~/pcc_refs` or the user's local
TileLang checkout. These are evidence/reference inputs only; they are not pcc
support claims.

| Reference | Path | Commit |
|---|---|---|
| LLVM 20.1.8 | `~/pcc_refs/llvm-project-20.1.8-full-depth1` | `87f0227cb60147a26a1eeb4fb06e3b505e9c7261` |
| Apache TVM | `~/pcc_refs/apache-tvm-full-depth1` | `cfb98e938c8d9525648c75fbebcb8944edb952fe` |
| TileLang | `~/tilelang` | `ed00dfcd7f9c200e1150896b1be59c41ff3e8d9d` |
| TileLang vendored TVM | `~/tilelang/3rdparty/tvm` | `1ecfcc2e1e1fb9f75db9ed760a97aa9687372905` |
| TileLang current architecture reference | `~/pcc_refs/tilelang-full-depth1` | `dff136d4da552389b0a41f394edfa1a9fe47a590` |
| ds4 / DwarfStar | `~/pcc_refs/antirez-ds4-depth1` | `80ebbc396aee40eedc1d829222f3362d10fa4c6c` |

Do not copy these trees into `pcc/`. When using them for a task, record the
path and commit in the evidence for that task.

Reference-derived design rules:

- LLVM is a design reference for durable IR layering, target separation,
  verification, and pass boundaries. pcc retains semantic ownership of GPU
  behavior, while an explicitly selected backend may become the execution
  owner under `docs/design/pcc-gpu-owner-backends.md`.
- TVM and TileLang are references for schedule representation, TIR/TIRx
  structure, Metal codegen shapes, swizzle/layout idioms, and simdgroup GEMM
  lowering. Import only the semantics pcc can represent in Kernel IR and
  validate; fail closed for the rest.
- TVM `s_tir` schedule state/trace and MLIR Transform's payload/transform split
  are references for PCC's replayable `KernelSchedule`. A schedule is a
  content-addressed transformation of exact Kernel IR before freeze, not attrs
  smuggled into semantic IR or an ambient provider callback.
- Apple Metal pipeline capabilities are explicit cost-model inputs. Device
  family, `threadExecutionWidth`, maximum threadgroup width, and static
  threadgroup memory must be recorded before a target adapter selects a plan.
  Metal binary archives are later content-addressed artifacts, never the sole
  semantic representation.
- ds4 is an external oracle and migration target. Use it to inventory real
  C/Metal/KV/SSD/distributed pressure, not as proof that pcc supports ds4.

## Claim Levels

GPU evidence must be mode-labeled. Do not use a lower level as proof of a
higher one.

| Level | Meaning |
|---|---|
| `GPU_LEVEL_0_METADATA` | IR, descriptor, manifest, inventory, or oracle metadata only |
| `GPU_LEVEL_1_SOURCE` | `.metal` or host bridge source emitted and inspected |
| `GPU_LEVEL_2_ARTIFACT` | `.air`, `.metallib`, `.o`, `.dylib`, or symbol validation produced |
| `GPU_LEVEL_3_RUNTIME_ABI` | Host ABI invoked or packet packed; fake/injected calls are still not GPU execution |
| `GPU_LEVEL_4_DEVICE_RESULT` | Real command buffer submitted, fence completed, readback equals CPU oracle |
| `GPU_LEVEL_5_PCC1_NATIVE` | pcc1-built no-libpython binary runs the same launcher path |
| `GPU_LEVEL_6_5GC_PARITY` | Same workload/lifetime gate passes under `PCC_GC_BACKEND=0..4` |

`DONE_STRONG` for a GPU primitive requires at least:

- CPU oracle.
- Kernel IR / TIRx freeze / source golden.
- Packed argument rejection for PyObject, wrong dtype, wrong device, and wrong
  shape.
- `GPU_LEVEL_4_DEVICE_RESULT`.
- pcc1 no-libpython launcher proof.
- five-GC lifetime proof.
- toolchain/device absence represented as `SKIPPED_WITH_REASON`, not success.

## Canonical Lowering Path

The canonical route is:

```text
@pcc.gpu.kernel / TileLang-like source / ds4 adapter
  -> pcc Kernel IR
  -> validate_kernel()
  -> optional checked KernelSchedule replay
  -> TIRx-compatible freeze
  -> target-specific finalize
  -> launch plan / package / runtime
```

`pcc.kernel_ir.entry` has no direct AST-to-Metal path. Its finite scalar/indexed
subset is imported as validated structured Kernel IR, and unsupported Python
syntax fails closed before TIRx/Metal finalization.

`KernelSchedule` is separate from semantic `KernelModule`. The first supported
instruction binds a function's Metal thread count and is guarded by the exact
input Kernel IR digest, canonical target, function selector, and expected old
binding. Both `pcc-metal` and `tvm-tilelang` apply it through the same module
before plain-TIR freeze and record its digest in artifact/owner manifests.
Scheduling a frozen module or replaying a stale plan fails closed. This is not
yet general tiling, layout scheduling, software pipelining, or autotuning.

All TileLang and ds4 adapters must produce Kernel IR first. They must not call
TileLang/TVM runtime objects or ds4 GPU APIs as semantic owners. A pinned,
explicitly selected TVM/TileLang provider may later become an execution owner
behind the common GPU driver boundary; it still consumes pcc-validated frozen
IR and cannot silently replace pcc's semantic, ABI, or lifetime contracts.

## First-class GPU execution owners

The durable owner-backend contract is
[`pcc-gpu-owner-backends.md`](pcc-gpu-owner-backends.md). The intended peer
relationship is:

```text
host_backend=self       gpu_backend=pcc-metal
host_backend=self       gpu_backend=tvm-tilelang   # explicit optional provider
```

The selected GPU backend owns compile/package/launch for that execution and
must record requested backend == actual backend with `fallback_used=false`.
pcc continues to own Kernel IR/TIRx meaning, packed arguments, buffers, fences,
diagnostics, and claim manifests. This is how Metal becomes first-class like
`self` without giving an ambient TVM/TileLang installation uncontrolled
semantic ownership.

Current route status:

- `@pcc.gpu.kernel` vector add imports as `elementwise_add`; its broader finite
  scalar assignment, indexed load/store, arithmetic, comparison, and nested
  `if/else` subset imports as structured Kernel IR before TIRx/Metal emission.
- TileLang import already produces Kernel IR for the supported matmul subset.
- Unsupported `@gpu.kernel` shapes fail closed; no legacy Metal fallback is
  available.
- ds4 remains an external reference, but its bounded tensor/command lifetime
  section now has a fail-closed mapping onto pcc-owned buffer, alias, fence,
  readback, and deferred-free concepts. This CPU state machine does not import,
  link, or execute ds4 and is not a ds4 GPU support claim; see
  `pcc-ds4-gpu-api-mapping.md`.
- The first migrated ds4-shaped primitive is the pinned contiguous f32 copy
  oracle lowered through pcc Kernel IR/TIRx/pcc-metal and proven by real Metal
  readback; see `pcc-ds4-first-primitive.md`. No broader ds4 operator or model
  support follows from that one primitive.

## Training And Inference Layers

Training and inference should share the substrate:

- Kernel IR / TIRx freeze.
- TargetMachine and Metal/CUDA/ROCm finalizers.
- `PccBufferHandle`, `PccPackedArgs`, `PccFenceToken`.
- DLPack/tensor ownership and stream/fence semantics.
- Distributed collective primitives and checkpoint/resource manifests.

Inference-specific layers are KV block management, prefix cache, paged
attention, decode scheduling, model-weight residency, SSD/offload streaming,
and request timelines.

Training-specific layers are autograd, activation checkpointing, gradient
lifetime, optimizer state, mixed precision/loss scaling, DDP/FSDP/tensor
parallel collectives, pipeline schedules, checkpoint sharding, and elastic
restart.

Current pcc proves only kernel/runtime/metadata slices, not training.

## ds4 Route

The ds4 tree is an external oracle and long-term migration target, not a current
support claim. The first downloaded pin contains:

- C/runtime: `ds4.c`, `ds4.h`, `ds4_gpu.h`, `ds4_kvstore.*`,
  `ds4_ssd.*`, `ds4_distributed.*`.
- Metal host/runtime: `ds4_metal.m`.
- Metal kernels: `metal/*.metal` including copy/bin/dense/KV/RoPE/attention/
  MoE/norm/softmax/repeat/sum rows.
- CUDA/ROCm routes: `ds4_cuda.cu`, `ds4_rocm.cu`, `rocm/*.cuh`.
- GGUF tooling: `gguf-tools/`.
- Oracles/smokes: `tests/ds4_test.c`, `tests/test_q4k_dot.c`,
  `tests/cuda_long_context_smoke.c`.

The migration sequence is:

1. `DS4-P0-INVENTORY-ORACLE`: pin commit, inventory sources/Makefile targets,
   GPU API surface, distributed protocol, GGUF/KV/SSD surfaces, and native
   smoke vectors. No pcc compile claim.
2. CPU-only compile subset: classify pcc C frontend, libc, POSIX, mmap, socket,
   GGUF, and runtime gaps.
3. GPU API mapping: the 21-function tensor/command lifetime section is mapped
   exactly onto `PccTensorSlice`/`PccBufferHandle`/fence/readback/deferred-free
   concepts. The mapping is CPU-only and fail-closed; it does not execute ds4.
4. Primitive migration: contiguous f32 copy is proven through real readback;
   continue with fill, conversion/strided copy, argmax, topk/mask, q8 matmul, RoPE/norm,
   compressed-attention indexer, KV page copy/update, MoE expert cache, then
   full layer graph. Every primitive needs CPU oracle -> Kernel IR -> Metal
   source -> real Metal output -> ds4 oracle comparison.
5. KV/SSD/cache: unify ds4 streaming with pcc block-directory concepts using
   fence-protected residency states.
6. Distributed inference: coordinator/worker, layer range ownership,
   activation send/recv, prefill/decode route, KV shard ownership, and
   topology-neutral snapshot save/load.
7. Server/agent/benchmark parity last. Throughput is not correctness proof.

## Hard Gates

Dedicated hardware gates live under `tests/gpu_hardware/` so normal CI can
report explicit `SKIPPED_WITH_REASON` while Metal runners can run with
`PCC_GPU_HARDWARE_STRICT=1` to require device execution.

Current implemented gate:

```text
tests/gpu_hardware/test_metal_claim_levels.py
```

It proves `GPU_LEVEL_4_DEVICE_RESULT` for the current runtime-source copy,
imported TileLang/TIRx scalar GEMM, and opt-in 8x8 simdgroup GEMM primitives
when strict mode is enabled on this machine. It also asserts no `.metallib`,
pcc1-native launcher, five-GC parity, or whole-program GPU claim.

Remaining dedicated gates to add:

```text
tests/gpu_hardware/test_metal_copy_runtime_real.py
tests/gpu_hardware/test_metal_gemm_runtime_real.py
tests/gpu_hardware/test_metal_fence_deferred_free_real.py
tests/gpu_hardware/test_metal_pcc1_launch_real.py
tests/gpu_hardware/test_metal_5gc_lifetime_real.py
```

The primitive parity matrix is mandatory for copy, fill, reduce_sum,
scalar_gemm, simdgroup_gemm, attention_prefill, and KV page copy/update:

```text
CPU oracle
Kernel IR golden
Metal source
real Metal run
pcc1 no-libpython run
5-GC lifetime parity
```

No CPU oracle means no GPU correctness claim.

## Active Task Mapping

The actionable rows are in `docs/goal/task-board.yaml`:

- `GPU-P0-CANONICAL-KERNEL-IR-PATH`
- `GPU-P0-HARDWARE-LEVEL-GATES`
- `GPU-P0-METAL-PCC1-LAUNCH-REAL`
- `GPU-P0-METAL-5GC-LIFETIME-REAL`
- `GPU-P0-DLPACK-EXTERNAL-CAPSULE-INTEROP`
- `GPU-P0-GC-EXTERNAL-RESOURCE-SEAM`
- `GPU-P1-BROADER-TILELANG-TIRX-PASSES`
- `GPU-P0-OWNER-BACKEND-CONTRACT`
- `GPU-P0-PCC-METAL-OWNER-DRIVER`
- `GPU-P0-TVM-TILELANG-OWNER-DRIVER`
- `GPU-P1-OWNER-SCHEDULE-THREAD-BINDING`
- `DIST-P0-LOCAL-COLLECTIVE-ORACLE-CODE`
- `DS4-P0-INVENTORY-ORACLE`
- `DS4-P1-CPU-COMPILE-SUBSET`
- `DS4-P2-GPU-API-MAPPING`
- `DS4-P3-PRIMITIVE-ORACLE`

Already landed `DONE_WEAK` rows remain useful but are not final completion:
runtime-source copy/GEMM/simdgroup, pcc-owned DLPack-shaped ownership, local
distributed oracles, and GPU-GC CPU/external-resource oracles all need
higher-level gates before production claims.

The two remaining GPU claim-level rows are deliberately split from
`GPU-P0-HARDWARE-LEVEL-GATES`: Level 4 device-result gates are not Level 5
pcc1-native launcher proof, and neither is Level 6 five-GC lifetime parity.
Keep them separate until the same workload proves each boundary.
