"""pcc.gpu_gc — CPU-only research oracle for a GPU-participating garbage collector.

FIRST SLICE (metadata-only). This package is a **CPU-owned control-plane model**
of a partitioned, mostly-non-moving, concurrent collector that could later be
assisted by GPU data-plane kernels. It exists so the correctness contract of a
GPU-participating collector can be *specified and tested on the CPU* before any
kernel, driver, or real-collector integration is written.

What this slice IS
------------------
* A blockized-heap metadata substrate: ``Region`` / ``Page`` descriptors with
  **stable block identity**, touch/free/evict bookkeeping, and layout-class
  routing (``substrate``).
* A CPU-first partitioned concurrent-collector *state machine* modelling root
  snapshots, a barrier discipline, epoch transitions, non-moving old regions,
  short-lived nursery regions, and selective evacuation points that
  **provably preserve reachability** (``cpu_collector``).
* A CPU oracle + page classifier deciding ``GPU_TRACEABLE`` / ``CPU_ONLY`` /
  ``GPU_SUMMARY_ONLY`` with fallback telemetry, so a future GPU kernel's output
  can be checked *against the oracle* rather than trusted (``assist``).
* A NO-OP Metal unified-memory residency adapter interface with residency
  modes and telemetry, that reports ``SKIPPED_WITH_REASON`` when Metal tooling
  is absent (``metal_adapter``).
* A local-only content-hash block directory for immutable restartable blocks
  with explicit recompute/fallback semantics (``tiered``).
* A five-GC-visible external-resource seam over Kernel IR's
  ``PccBufferHandle`` / ``PccFenceToken`` primitives. It records opaque GPU
  buffers and runs native release callbacks only after the protecting fence
  completes (``external_resource``).

What this slice IS **NOT** (explicit claim boundary)
----------------------------------------------------
This is a **CPU-only research oracle**. It does **not**:

* run any GPU/Metal/CUDA kernel, allocate device memory, or touch a driver;
* implement a *moving* / relocating collector — every "evacuation" here is a
  metadata transition in a model, not a real object copy;
* integrate with pcc's five production GC backends
  (``PCC_GC_KIND_*`` 0..4 in ``pcc/runtime``); it only *borrows their
  vocabulary* so a later integration has a shared state model to target;
* claim MLX / vLLM / vLLM-Metal / Mooncake interoperability;
* claim whole-Python-on-GPU execution.

Most modules here are standalone standard-library oracles. The
``external_resource`` seam deliberately imports Kernel IR's HMM/fence types so
there is one buffer/fence contract shared by GPU runtime, DLPack ownership, and
future GC backend integration. The package still does not mutate
``pcc/__init__``.

Design rationale and the full claim boundary live in
``docs/design/pcc-gpu-gc.md``.
"""
from __future__ import annotations

from .substrate import (
    BlockId,
    LayoutClass,
    Page,
    PageState,
    Region,
    RegionKind,
    Substrate,
    SubstrateError,
)
from .cpu_collector import (
    Barrier,
    BarrierKind,
    CollectorError,
    CpuCollector,
    Color,
    Epoch,
)
from .assist import (
    AssistClass,
    AssistOracle,
    AssistTelemetry,
    classify_page,
)
from .metal_adapter import (
    MetalResidencyAdapter,
    MetalTelemetry,
    ResidencyMode,
    AdapterStatus,
)
from .tiered import (
    BlockDirectory,
    DirectoryError,
    TieredEntry,
    content_hash,
)
from .external_resource import (
    ExternalResourceError,
    ExternalResourceKind,
    ExternalResourcePollResult,
    ExternalResourceRecord,
    ExternalResourceRegistry,
    ExternalResourceReleaseResult,
    ExternalResourceState,
)

__all__ = [
    # substrate
    "BlockId",
    "LayoutClass",
    "Page",
    "PageState",
    "Region",
    "RegionKind",
    "Substrate",
    "SubstrateError",
    # cpu_collector
    "Barrier",
    "BarrierKind",
    "CollectorError",
    "CpuCollector",
    "Color",
    "Epoch",
    # assist
    "AssistClass",
    "AssistOracle",
    "AssistTelemetry",
    "classify_page",
    # metal_adapter
    "MetalResidencyAdapter",
    "MetalTelemetry",
    "ResidencyMode",
    "AdapterStatus",
    # tiered
    "BlockDirectory",
    "DirectoryError",
    "TieredEntry",
    "content_hash",
    # external_resource
    "ExternalResourceError",
    "ExternalResourceKind",
    "ExternalResourcePollResult",
    "ExternalResourceRecord",
    "ExternalResourceRegistry",
    "ExternalResourceReleaseResult",
    "ExternalResourceState",
]

__version__ = "0.0.1-oracle"
